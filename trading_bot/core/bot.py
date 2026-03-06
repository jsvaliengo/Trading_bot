"""
BOT DE TRADING PRINCIPAL
========================
Este é o arquivo principal que executa o bot.
Ele coordena todos os módulos e executa o loop de trading.

COMO USAR:
1. Configure suas API keys no config.py ou como variáveis de ambiente
2. Ajuste os parâmetros de risco no config.py
3. Execute: python bot.py

⚠️  AVISO: Trading de criptomoedas envolve risco significativo.
    Comece sempre na TESTNET antes de usar dinheiro real!
"""

import time
import logging
import signal
import sys
import json
import os
import shutil
import fcntl
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

from .config import config
from ..infra.binance_client import BinanceConnection
from .strategy import HedgeStrategy, RiskManager
from ..services.notifications import TelegramNotifier
from ..services.pair_selector import PairSelector
from ..services.telegram_commands import TelegramCommandHandler

logger = logging.getLogger(__name__)


def _configure_logging():
    """
    Configura logging com arquivo em runtime/ e nível por ambiente.
    """
    level_name = str(getattr(config, "LOG_LEVEL", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    handlers = [logging.FileHandler(config.LOG_FILE_PATH, encoding="utf-8")]

    if bool(getattr(config, "LOG_TO_STDOUT", True)):
        handlers.append(logging.StreamHandler())

    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=handlers,
        force=True,
    )


class TradingBot:
    """
    Classe principal do bot de trading.
    
    Coordena:
    - Conexão com a exchange
    - Análise de mercado
    - Execução de trades
    - Gestão de risco
    """
    
    def __init__(self):
        """
        Inicializa o bot com todas as dependências.
        """
        _configure_logging()

        self._state_file_path = config.STATE_FILE_PATH
        self._instance_lock_path = config.LOCK_FILE_PATH
        self._migrate_legacy_runtime_files()

        logger.info("=" * 50)
        logger.info("🤖 INICIANDO BOT DE TRADING")
        logger.info("=" * 50)
        logger.info(
            f"🌍 Ambiente: {config.APP_ENV} | Runtime: {config.RUNTIME_DIR}"
        )
        
        # Valida configurações
        logger.info("📋 Validando configurações...")
        if not config.validate():
            logger.error("❌ Configuração inválida. Inicialização abortada.")
            raise ValueError(
                "Configuração inválida. Corrija os alertas exibidos antes de iniciar o bot."
            )
        
        # Inicializa componentes
        logger.info("🔌 Conectando à Binance...")
        self.exchange = BinanceConnection()
        
        logger.info("📊 Inicializando estratégia...")
        self.strategy = HedgeStrategy()
        self._strategy_engines: Dict[str, HedgeStrategy] = {"primary": self.strategy}
        self.strategy_profiles: List[Dict[str, Any]] = []
        self._reload_strategy_profiles(reason="init")
        
        logger.info("🛡️  Inicializando gerenciador de risco...")
        self.risk_manager = RiskManager()
        
        logger.info("📱 Inicializando notificações Telegram...")
        self.telegram = TelegramNotifier(
            token=config.TELEGRAM_TOKEN,
            chat_id=config.TELEGRAM_CHAT_ID,
            enabled=config.TELEGRAM_ENABLED
        )
        
        # Inicializa handler de comandos do Telegram
        logger.info("🎮 Inicializando comandos Telegram...")
        self.command_handler = TelegramCommandHandler(
            token=config.TELEGRAM_TOKEN,
            chat_id=config.TELEGRAM_CHAT_ID
        )
        self.command_handler.set_bot_reference(self, config)
        
        # Estado do bot
        self.running = False
        self.paused = False  # Quando pausado, não abre novas posições
        self.positions = {}  # Rastreia posições abertas
        self.trade_history = []  # Histórico de trades (abertura)
        
        # Contador de posições FECHADAS (não abertas)
        self.closed_trades_count = 0
        
        # Rastreamento de P&L
        self.total_pnl = 0.0  # P&L total acumulado desde o início
        self.daily_realized_pnl = 0.0  # P&L realizado do dia (soma quando fecha posição)
        self.pnl_by_symbol = {}  # P&L separado por par de moeda
        for symbol in config.TRADING_PAIRS:
            self.pnl_by_symbol[symbol] = 0.0
        
        # Estatísticas de trades (lucro vs prejuízo)
        self.trades_win_count = 0      # Quantidade de trades com lucro
        self.trades_loss_count = 0     # Quantidade de trades com prejuízo
        self.trades_win_total = 0.0    # Valor total dos lucros
        self.trades_loss_total = 0.0   # Valor total dos prejuízos
        
        # Rastreamento de taxas
        self.total_fees_paid = 0.0  # Total de taxas pagas (para relatório)
        
        # Rastreamento de trades por símbolo (para relatório detalhado)
        # Formato: {symbol: {'wins': int, 'losses': int, 'win_value': float, 'loss_value': float, 'fees': float}}
        self.trades_by_symbol = {}
        
        # Controle de metas diárias
        self.daily_target_reached = False  # Se a meta do dia foi atingida
        self.daily_target_type = None      # 'PROFIT' ou 'LOSS'
        self.last_daily_reset = datetime.now().date()  # Data do último reset
        self.last_daily_performance_report_date = ""
        
        # Histórico de evolução da carteira (para relatório)
        # Guarda snapshots: {'timestamp': datetime, 'balance': float, 'pnl': float}
        self.portfolio_history = []
        self.last_snapshot_time = None
        self.snapshot_interval_minutes = 30  # Snapshot a cada 30 minutos
        self.start_time = datetime.now()  # Hora que o bot iniciou
        
        # Capital inicial para cálculo do Stop Loss Global
        # Será preenchido com o saldo real da carteira no setup_exchange()
        self.initial_capital = None  # Dinâmico - busca da carteira

        # Rastreamento de transferências de capital (depósito/saque em Futures)
        self.last_transfer_check_ts_ms = 0
        self.processed_transfer_ids = []
        
        # Rastreamento do Trailing Stop
        # Guarda o preço máximo (LONG) ou mínimo (SHORT) atingido por cada posição
        # Chave: "symbol_side" (ex: "ETHUSDT_LONG")
        self.peak_prices = {}  # Preço máximo/mínimo atingido
        self.trailing_activated = {}  # Se o trailing já foi ativado
        
        # Rastreamento de posições conhecidas
        # Usado para detectar quando a Binance fecha posições via SL/TP
        # Chave: "symbol_side" (ex: "ETHUSDT_LONG"), Valor: dict com info da posição
        self.known_positions = {}

        # Controle de uso da regra "Double First" (primeira entrada dobrada)
        # Chaves:
        # - escopo global: LONG / SHORT
        # - escopo symbol: SYMBOL_LONG / SYMBOL_SHORT
        self.double_first_used = {}
        
        # Cache de taxas de comissão (busca da API da Binance)
        # Atualizado periodicamente para refletir mudanças (VIP, BNB, etc)
        self.commission_rates = None  # Será preenchido no setup
        self.last_commission_update = None
        
        # Seletor de pares (será inicializado no setup_exchange)
        self.pair_selector = None
        self.last_pair_update = None

        # Filtro direcional por sentimento (opcional e com fallback seguro)
        self.sentiment_mode_enabled = bool(getattr(config, "USE_MARKET_SENTIMENT_FILTER", False))
        self.sentiment_cache: Dict[str, Dict[str, Any]] = {}

        # Lock de instância única (evita dois bots simultâneos)
        self._instance_lock_handle = None

        # Observabilidade de runtime (loops/erros)
        self._runtime_stats_lock = threading.Lock()
        self._runtime_stats_since_report = self._new_runtime_stats()
        self._next_critical_health_alert_time = 0.0
        
        # Configura handler para CTRL+C
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        # Carrega estado salvo anteriormente (se existir)
        self.load_state()
        
        logger.info("✅ Bot inicializado com sucesso!")

    def _migrate_legacy_runtime_files(self):
        """
        Migra arquivo de estado legado do root para runtime/ quando necessário.
        """
        legacy_state_path = os.path.join(config.PROJECT_ROOT, "bot_state.json")
        if os.path.exists(self._state_file_path):
            return

        if not os.path.exists(legacy_state_path):
            return

        try:
            shutil.copy2(legacy_state_path, self._state_file_path)
            logger.info(
                f"📦 Estado legado migrado para runtime: {self._state_file_path}"
            )
        except Exception as e:
            logger.warning(
                f"⚠️ Falha ao migrar estado legado ({legacy_state_path}): {e}"
            )

    def _acquire_instance_lock(self) -> bool:
        """
        Tenta adquirir lock exclusivo para garantir instância única.
        Retorna False se já houver outro processo rodando o bot.
        """
        if self._instance_lock_handle is not None:
            return True

        try:
            lock_handle = open(self._instance_lock_path, 'a+')

            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_handle.seek(0)
                holder_info = lock_handle.read().strip() or "desconhecido"
                logger.error("❌ Outra instância do bot já está em execução!")
                logger.error(f"   Lock file: {self._instance_lock_path}")
                logger.error(f"   Holder: {holder_info}")
                lock_handle.close()
                return False

            lock_handle.seek(0)
            lock_handle.truncate()
            lock_handle.write(f"pid={os.getpid()} started_at={datetime.now().isoformat()}\n")
            lock_handle.flush()

            self._instance_lock_handle = lock_handle
            logger.info(f"🔒 Lock de instância adquirido: {self._instance_lock_path}")
            return True

        except Exception as e:
            logger.error(f"❌ Erro ao adquirir lock de instância: {e}")
            return False

    def _release_instance_lock(self):
        """
        Libera o lock de instância única.
        """
        if self._instance_lock_handle is None:
            return

        try:
            fcntl.flock(self._instance_lock_handle.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass

        try:
            self._instance_lock_handle.close()
        except Exception:
            pass

        self._instance_lock_handle = None
        logger.info("🔓 Lock de instância liberado")

    def get_lock_info(self) -> dict:
        """
        Retorna informações do lock de instância para inspeção/diagnóstico.
        """
        holder_info = ""
        try:
            if os.path.exists(self._instance_lock_path):
                with open(self._instance_lock_path, 'r') as f:
                    holder_info = f.read().strip()
        except Exception as e:
            holder_info = f"erro ao ler lock: {e}"

        return {
            'lock_file': self._instance_lock_path,
            'lock_acquired': self._instance_lock_handle is not None,
            'holder_info': holder_info or "vazio",
            'current_pid': os.getpid(),
            'bot_running': self.running,
            'bot_paused': self.paused,
        }

    @staticmethod
    def _new_runtime_stats() -> Dict[str, Any]:
        """Cria acumuladores base de telemetria do loop principal."""
        return {
            'monitor_cycles': 0,
            'analysis_steps': 0,
            'monitor_total_seconds': 0.0,
            'analysis_total_seconds': 0.0,
            'monitor_max_seconds': 0.0,
            'analysis_max_seconds': 0.0,
            'monitor_overruns': 0,
            'analysis_overruns': 0,
            'loop_errors': 0,
            'last_error': '',
            'slow_symbols': {}
        }

    def _record_loop_timing(
        self,
        loop_type: str,
        duration_seconds: float,
        target_interval_seconds: float,
        symbol: str = ""
    ):
        """Registra duração de ciclo e marca overrun quando ultrapassa o alvo."""
        factor = max(1.0, float(getattr(config, "LOOP_OVERRUN_FACTOR", 1.5)))
        overrun_threshold = max(0.1, target_interval_seconds) * factor
        is_overrun = duration_seconds > overrun_threshold

        with self._runtime_stats_lock:
            stats = self._runtime_stats_since_report
            if loop_type == 'monitor':
                stats['monitor_cycles'] += 1
                stats['monitor_total_seconds'] += duration_seconds
                if duration_seconds > stats['monitor_max_seconds']:
                    stats['monitor_max_seconds'] = duration_seconds
                if is_overrun:
                    stats['monitor_overruns'] += 1
            elif loop_type == 'analysis':
                stats['analysis_steps'] += 1
                stats['analysis_total_seconds'] += duration_seconds
                if duration_seconds > stats['analysis_max_seconds']:
                    stats['analysis_max_seconds'] = duration_seconds
                if is_overrun:
                    stats['analysis_overruns'] += 1
                    if symbol:
                        slow = stats['slow_symbols'].setdefault(
                            symbol,
                            {'count': 0, 'max_seconds': 0.0}
                        )
                        slow['count'] += 1
                        if duration_seconds > slow['max_seconds']:
                            slow['max_seconds'] = duration_seconds

    def _record_runtime_error(self, error: Exception):
        """Registra falhas do loop principal para alerta operacional."""
        with self._runtime_stats_lock:
            self._runtime_stats_since_report['loop_errors'] += 1
            self._runtime_stats_since_report['last_error'] = str(error)[:240]

    def get_runtime_stats_report(self, reset: bool = True) -> Dict[str, Any]:
        """
        Retorna estatísticas de runtime acumuladas desde o último report.
        """
        with self._runtime_stats_lock:
            snapshot = {
                'monitor_cycles': self._runtime_stats_since_report['monitor_cycles'],
                'analysis_steps': self._runtime_stats_since_report['analysis_steps'],
                'monitor_total_seconds': self._runtime_stats_since_report['monitor_total_seconds'],
                'analysis_total_seconds': self._runtime_stats_since_report['analysis_total_seconds'],
                'monitor_max_seconds': self._runtime_stats_since_report['monitor_max_seconds'],
                'analysis_max_seconds': self._runtime_stats_since_report['analysis_max_seconds'],
                'monitor_overruns': self._runtime_stats_since_report['monitor_overruns'],
                'analysis_overruns': self._runtime_stats_since_report['analysis_overruns'],
                'loop_errors': self._runtime_stats_since_report['loop_errors'],
                'last_error': self._runtime_stats_since_report['last_error'],
                'slow_symbols': {
                    symbol: data.copy()
                    for symbol, data in self._runtime_stats_since_report['slow_symbols'].items()
                }
            }
            if reset:
                self._runtime_stats_since_report = self._new_runtime_stats()

        monitor_cycles = snapshot['monitor_cycles']
        analysis_steps = snapshot['analysis_steps']
        monitor_avg_seconds = (
            snapshot['monitor_total_seconds'] / monitor_cycles if monitor_cycles else 0.0
        )
        analysis_avg_seconds = (
            snapshot['analysis_total_seconds'] / analysis_steps if analysis_steps else 0.0
        )

        slow_ranked = sorted(
            snapshot['slow_symbols'].items(),
            key=lambda item: (item[1]['count'], item[1]['max_seconds']),
            reverse=True
        )
        slow_symbols = []
        for symbol, data in slow_ranked:
            slow_symbols.append({
                'symbol': symbol,
                'count': data['count'],
                'max_seconds': data['max_seconds']
            })

        return {
            'monitor_cycles': monitor_cycles,
            'analysis_steps': analysis_steps,
            'monitor_avg_seconds': monitor_avg_seconds,
            'analysis_avg_seconds': analysis_avg_seconds,
            'monitor_max_seconds': snapshot['monitor_max_seconds'],
            'analysis_max_seconds': snapshot['analysis_max_seconds'],
            'monitor_overruns': snapshot['monitor_overruns'],
            'analysis_overruns': snapshot['analysis_overruns'],
            'loop_errors': snapshot['loop_errors'],
            'last_error': snapshot['last_error'],
            'slow_symbols': slow_symbols
        }

    @staticmethod
    def _empty_order_stats_report() -> Dict[str, Any]:
        """Retorna estrutura vazia de telemetria de ordens."""
        return {
            'attempts': 0,
            'successes': 0,
            'failures': 0,
            'rejections': 0,
            'failure_rate': 0.0,
            'rejection_rate': 0.0,
            'symbols': []
        }

    def _evaluate_critical_health_issue(
        self,
        api_report: Dict[str, Any],
        order_report: Dict[str, Any],
        runtime_report: Dict[str, Any]
    ) -> Tuple[bool, str]:
        """Avalia se há incidente crítico para disparo imediato."""
        reasons = []

        if api_report.get('failures', 0) >= int(config.API_HEALTH_CRITICAL_MIN_API_FAILURES):
            reasons.append(f"falhas_api={api_report.get('failures', 0)}")

        if order_report.get('failures', 0) >= int(config.API_HEALTH_CRITICAL_MIN_ORDER_FAILURES):
            reasons.append(f"falhas_ordem={order_report.get('failures', 0)}")

        if order_report.get('rejections', 0) >= int(config.API_HEALTH_CRITICAL_MIN_ORDER_REJECTIONS):
            reasons.append(f"rejeicoes_ordem={order_report.get('rejections', 0)}")

        if runtime_report.get('loop_errors', 0) >= int(config.API_HEALTH_CRITICAL_MIN_LOOP_ERRORS):
            reasons.append(f"erros_loop={runtime_report.get('loop_errors', 0)}")

        if not reasons:
            return (False, "")

        return (True, ", ".join(reasons[:4]))

    def save_state(self):
        """
        Salva o estado atual do bot em um arquivo JSON.
        Isso permite continuar de onde parou após reiniciar.
        """
        try:
            # Converte portfolio_history para formato serializável
            portfolio_history_serializable = []
            for snap in self.portfolio_history:
                portfolio_history_serializable.append({
                    'timestamp': snap['timestamp'].isoformat() if isinstance(snap['timestamp'], datetime) else snap['timestamp'],
                    'balance': snap['balance'],
                    'pnl_realized': snap['pnl_realized'],
                    'pnl_unrealized': snap['pnl_unrealized'],
                    'pnl_total': snap['pnl_total'],
                    'closed_trades': snap['closed_trades']
                })
            
            state = {
                'version': '1.5',  # Inclui overrides de pares (disable/add) + transferências de capital
                'saved_at': datetime.now().isoformat(),
                'start_time': self.start_time.isoformat() if isinstance(self.start_time, datetime) else self.start_time,
                'initial_capital': self.initial_capital,  # Capital inicial (atualiza com depósitos)
                'closed_trades_count': self.closed_trades_count,
                'total_pnl': self.total_pnl,
                'daily_realized_pnl': self.daily_realized_pnl,
                'daily_date': datetime.utcnow().strftime('%Y-%m-%d'),  # UTC como a Binance (reseta 00:00 UTC)
                'pnl_by_symbol': self.pnl_by_symbol,
                # Estatísticas de trades (lucro vs prejuízo)
                'trades_win_count': self.trades_win_count,
                'trades_loss_count': self.trades_loss_count,
                'trades_win_total': self.trades_win_total,
                'trades_loss_total': self.trades_loss_total,
                'total_fees_paid': self.total_fees_paid,  # Total de taxas pagas
                'portfolio_history': portfolio_history_serializable,
                'trade_history': self.trade_history,
                'peak_prices': self.peak_prices,
                'trailing_activated': self.trailing_activated,
                'double_first_used': self.double_first_used,
                'sentiment_mode_enabled': bool(self.sentiment_mode_enabled),
                'last_daily_performance_report_date': self.last_daily_performance_report_date,
                'last_transfer_check_ts_ms': int(self.last_transfer_check_ts_ms or 0),
                'processed_transfer_ids': self.processed_transfer_ids[
                    -max(100, int(config.CAPITAL_TRANSFER_TRACKED_IDS_LIMIT)):
                ],
                'disabled_pairs': list(getattr(config, 'DISABLED_PAIRS', []) or []),
                'binance_coin_list': list(getattr(config, 'BINANCE_COIN_LIST', []) or []),
                'strategy_profiles': list(getattr(config, 'STRATEGY_PROFILES', []) or []),
            }
            
            with open(self._state_file_path, 'w') as f:
                json.dump(state, f, indent=2)
            
            logger.info(f"💾 Estado salvo em {self._state_file_path}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Erro ao salvar estado: {e}")
            return False
    
    def load_state(self):
        """
        Carrega o estado salvo anteriormente do arquivo JSON.
        Se não existir arquivo, mantém os valores padrão.
        """
        if not os.path.exists(self._state_file_path):
            logger.info("📂 Nenhum estado anterior encontrado. Iniciando do zero.")
            return False
        
        try:
            with open(self._state_file_path, 'r') as f:
                state = json.load(f)

            # Carrega overrides de pares antes da inicialização da estratégia.
            saved_disabled_pairs = state.get('disabled_pairs')
            if saved_disabled_pairs is not None:
                config.DISABLED_PAIRS = config.normalize_pair_list(saved_disabled_pairs)

            saved_binance_coin_list = state.get('binance_coin_list')
            if saved_binance_coin_list:
                config.BINANCE_COIN_LIST = config.normalize_pair_list(saved_binance_coin_list)

            saved_strategy_profiles = state.get('strategy_profiles')
            if saved_strategy_profiles is not None and hasattr(config, "_normalize_strategy_profiles"):
                config.STRATEGY_PROFILES = config._normalize_strategy_profiles(saved_strategy_profiles)

            config.FIXED_PAIRS = config.filter_disabled_pairs(config.FIXED_PAIRS)
            config.TRADING_PAIRS = config.filter_disabled_pairs(config.TRADING_PAIRS)
            self._sync_strategy_profiles_with_trading_pairs(reason="state-load")
            
            # Verifica se é do mesmo dia (usando UTC como a Binance)
            # A Binance reseta o P&L diário às 00:00 UTC
            saved_date = state.get('daily_date', '')
            today_utc = datetime.utcnow().strftime('%Y-%m-%d')
            
            # Carrega os valores
            self.closed_trades_count = state.get('closed_trades_count', 0)
            self.total_pnl = state.get('total_pnl', 0.0)
            self.pnl_by_symbol = state.get('pnl_by_symbol', {})
            self.trade_history = state.get('trade_history', [])
            self.peak_prices = state.get('peak_prices', {})
            self.trailing_activated = state.get('trailing_activated', {})
            self.double_first_used = self._normalize_double_first_state(
                state.get('double_first_used', {})
            )
            self.sentiment_mode_enabled = bool(
                state.get('sentiment_mode_enabled', self.sentiment_mode_enabled)
            )
            self.sentiment_cache = {}
            self.last_daily_performance_report_date = str(
                state.get('last_daily_performance_report_date', '') or ''
            )
            self.last_transfer_check_ts_ms = int(state.get('last_transfer_check_ts_ms', 0) or 0)
            self.processed_transfer_ids = list(state.get('processed_transfer_ids', []) or [])
            
            # Estatísticas de trades (lucro vs prejuízo)
            self.trades_win_count = state.get('trades_win_count', 0)
            self.trades_loss_count = state.get('trades_loss_count', 0)
            self.trades_win_total = state.get('trades_win_total', 0.0)
            self.trades_loss_total = state.get('trades_loss_total', 0.0)
            self.total_fees_paid = state.get('total_fees_paid', 0.0)
            
            # P&L diário só carrega se for do mesmo dia (UTC)
            if saved_date == today_utc:
                self.daily_realized_pnl = state.get('daily_realized_pnl', 0.0)
                logger.info(f"📅 P&L diário carregado: ${self.daily_realized_pnl:.2f}")
                logger.info(f"💸 Taxas pagas (sessão): ${self.total_fees_paid:.4f}")
            else:
                # Novo dia - reseta estatísticas diárias
                self.daily_realized_pnl = 0.0
                self.trades_win_count = 0
                self.trades_loss_count = 0
                self.trades_win_total = 0.0
                self.trades_loss_total = 0.0
                self.total_fees_paid = 0.0  # Reseta taxas também
                logger.info("📅 Novo dia UTC! P&L diário e estatísticas resetados.")
            
            # Carrega o start_time original
            start_time_str = state.get('start_time')
            if start_time_str:
                self.start_time = datetime.fromisoformat(start_time_str)
            
            # Carrega o capital inicial salvo (se existir)
            # Isso preserva o capital inicial mesmo após depósitos
            saved_initial_capital = state.get('initial_capital')
            if saved_initial_capital is not None:
                self._loaded_initial_capital = saved_initial_capital
                logger.info(f"💰 Capital inicial carregado: ${saved_initial_capital:.2f}")
            
            # Carrega portfolio_history e converte timestamps
            portfolio_history_raw = state.get('portfolio_history', [])
            self.portfolio_history = []
            for snap in portfolio_history_raw:
                self.portfolio_history.append({
                    'timestamp': datetime.fromisoformat(snap['timestamp']) if isinstance(snap['timestamp'], str) else snap['timestamp'],
                    'balance': snap['balance'],
                    'pnl_realized': snap['pnl_realized'],
                    'pnl_unrealized': snap['pnl_unrealized'],
                    'pnl_total': snap['pnl_total'],
                    'closed_trades': snap['closed_trades']
                })
            
            # Garante que todos os símbolos configurados estejam no pnl_by_symbol
            for symbol in config.TRADING_PAIRS:
                if symbol not in self.pnl_by_symbol:
                    self.pnl_by_symbol[symbol] = 0.0
            
            logger.info(f"✅ Estado carregado de {self._state_file_path}")
            logger.info(f"   • Trades fechados: {self.closed_trades_count}")
            logger.info(f"   • P&L Total: ${self.total_pnl:.2f}")
            logger.info(f"   • Snapshots no histórico: {len(self.portfolio_history)}")
            logger.info(f"   • Pares desabilitados: {', '.join(config.DISABLED_PAIRS) if config.DISABLED_PAIRS else 'nenhum'}")
            if self.double_first_used:
                logger.info(f"   • Double First usados: {len(self.double_first_used)}")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Erro ao carregar estado: {e}")
            logger.info("🔄 Iniciando com valores padrão.")
            return False

    def _normalize_double_first_state(self, raw_state) -> Dict[str, bool]:
        """
        Normaliza o estado da regra Double First carregado do JSON.
        Aceita dict/list legados e mantém apenas chaves válidas.
        """
        normalized: Dict[str, bool] = {}

        if isinstance(raw_state, dict):
            items = raw_state.items()
        elif isinstance(raw_state, list):
            items = [(item, True) for item in raw_state]
        else:
            return normalized

        for key, enabled in items:
            if not enabled:
                continue
            normalized_key = str(key).strip().upper()
            if not normalized_key:
                continue
            if normalized_key in {"LONG", "SHORT"}:
                normalized[normalized_key] = True
                continue
            if normalized_key.endswith("_LONG") or normalized_key.endswith("_SHORT"):
                normalized[normalized_key] = True

        return normalized

    def _double_first_scope(self) -> str:
        scope = str(getattr(config, "DOUBLE_FIRST_SCOPE", "global") or "global").strip().lower()
        return scope if scope in {"global", "symbol"} else "global"

    @staticmethod
    def _normalize_position_side(side: str) -> str:
        return "SHORT" if str(side).upper() == "SHORT" else "LONG"

    def _is_double_first_enabled(self, side: str) -> bool:
        normalized_side = self._normalize_position_side(side)
        if normalized_side == "LONG":
            return bool(getattr(config, "DOUBLE_FIRST_LONG_ENABLED", False))
        return bool(getattr(config, "DOUBLE_FIRST_SHORT_ENABLED", False))

    def _double_first_state_key(self, symbol: str, side: str) -> str:
        normalized_side = self._normalize_position_side(side)
        if self._double_first_scope() == "symbol":
            return f"{str(symbol).upper()}_{normalized_side}"
        return normalized_side

    def _apply_double_first_order_size(self, symbol: str, side: str, order_size: float) -> Tuple[float, bool, str]:
        """
        Aplica o multiplicador de "Double First" quando elegível.
        Retorna (novo_order_size, aplicado, state_key).
        A marcação como "usado" deve acontecer apenas após a ordem abrir com sucesso.
        """
        try:
            base_order_size = float(order_size)
        except Exception:
            return order_size, False, ""

        if base_order_size <= 0:
            return base_order_size, False, ""

        if not hasattr(self, "double_first_used") or not isinstance(self.double_first_used, dict):
            self.double_first_used = {}

        normalized_side = self._normalize_position_side(side)
        if not self._is_double_first_enabled(normalized_side):
            return base_order_size, False, ""

        multiplier = float(getattr(config, "DOUBLE_FIRST_MULTIPLIER", 1.0) or 1.0)
        if multiplier <= 1.0:
            return base_order_size, False, ""

        state_key = self._double_first_state_key(symbol, normalized_side)
        if bool(self.double_first_used.get(state_key)):
            return base_order_size, False, ""

        doubled_order_size = base_order_size * multiplier
        max_margin = float(getattr(config, "DOUBLE_FIRST_MAX_MARGIN_USDT", 0.0) or 0.0)
        if max_margin > 0:
            doubled_order_size = min(doubled_order_size, max_margin)

        if doubled_order_size <= base_order_size:
            return base_order_size, False, ""

        return doubled_order_size, True, state_key

    def _mark_double_first_used(
        self,
        state_key: str,
        symbol: str,
        side: str,
        base_order_size: float,
        applied_order_size: float,
    ) -> None:
        if not state_key:
            return
        if not hasattr(self, "double_first_used") or not isinstance(self.double_first_used, dict):
            self.double_first_used = {}

        self.double_first_used[state_key] = True
        logger.info(
            "🚀 Double First confirmado em %s %s: $%.2f → $%.2f (escopo=%s)",
            str(symbol).upper(),
            self._normalize_position_side(side),
            float(base_order_size),
            float(applied_order_size),
            self._double_first_scope(),
        )
    
    def _signal_handler(self, signum, frame):
        """
        Handler para parada graceful do bot (CTRL+C).
        """
        logger.info("\n⚠️  Sinal de parada recebido...")
        self.stop()

    def _filter_disabled_pairs(self, pairs: list) -> list:
        """
        Filtra pares desabilitados preservando ordem.
        """
        if hasattr(config, "filter_disabled_pairs"):
            return config.filter_disabled_pairs(pairs)

        disabled = {str(item).upper() for item in (getattr(config, "DISABLED_PAIRS", []) or [])}
        filtered = []
        seen = set()
        for raw_symbol in pairs or []:
            symbol = str(raw_symbol).upper()
            if not symbol or symbol in disabled or symbol in seen:
                continue
            seen.add(symbol)
            filtered.append(symbol)
        return filtered

    @staticmethod
    def _normalize_strategy_entry_mode(entry_mode: str) -> str:
        """Normaliza modo de entrada por estratégia."""
        token = str(entry_mode or "").strip().lower()
        if token in {"standard", "normal", "full"}:
            return "standard"
        return "strong_only"

    def _reload_strategy_profiles(self, reason: str = "runtime"):
        """
        Recarrega perfis de estratégia a partir do config.

        Mantém compatibilidade com fluxo legado:
        - se faltar profile, cria "primary" com TRADING_PAIRS atual
        - se houver pares em TRADING_PAIRS fora dos profiles, injeta no profile primário
        """
        raw_profiles = list(getattr(config, "STRATEGY_PROFILES", []) or [])
        if not raw_profiles:
            raw_profiles = [
                {
                    "name": "primary",
                    "enabled": True,
                    "entry_mode": "strong_only",
                    "pairs": list(getattr(config, "TRADING_PAIRS", []) or []),
                }
            ]

        previous_engines = getattr(self, "_strategy_engines", {})
        if not isinstance(previous_engines, dict):
            previous_engines = {}

        runtime_profiles: List[Dict[str, Any]] = []
        assigned_pairs = set()

        for index, raw_profile in enumerate(raw_profiles, start=1):
            if not isinstance(raw_profile, dict):
                continue
            if not bool(raw_profile.get("enabled", True)):
                continue

            profile_name = str(raw_profile.get("name") or f"strategy_{index}").strip() or f"strategy_{index}"
            entry_mode = self._normalize_strategy_entry_mode(raw_profile.get("entry_mode", "strong_only"))
            pairs = self._filter_disabled_pairs(raw_profile.get("pairs", []))

            unique_pairs = []
            for symbol in pairs:
                if symbol in assigned_pairs:
                    logger.warning(
                        "⚠️ Par %s duplicado entre perfis. Ignorando no perfil %s.",
                        symbol,
                        profile_name,
                    )
                    continue
                assigned_pairs.add(symbol)
                unique_pairs.append(symbol)

            strategy_engine = previous_engines.get(profile_name)
            if strategy_engine is None:
                strategy_engine = HedgeStrategy()

            runtime_profiles.append(
                {
                    "name": profile_name,
                    "entry_mode": entry_mode,
                    "pairs": unique_pairs,
                    "strategy": strategy_engine,
                }
            )

        if not runtime_profiles:
            fallback_strategy = previous_engines.get("primary") or getattr(self, "strategy", None) or HedgeStrategy()
            runtime_profiles = [
                {
                    "name": "primary",
                    "entry_mode": "strong_only",
                    "pairs": self._filter_disabled_pairs(getattr(config, "TRADING_PAIRS", []) or []),
                    "strategy": fallback_strategy,
                }
            ]

        # Mantém pares legados no profile primário quando surgirem fora dos profiles.
        legacy_pairs = self._filter_disabled_pairs(getattr(config, "TRADING_PAIRS", []) or [])
        mapped_pairs = {symbol for profile in runtime_profiles for symbol in profile["pairs"]}
        missing_pairs = [symbol for symbol in legacy_pairs if symbol not in mapped_pairs]
        if missing_pairs:
            runtime_profiles[0]["pairs"].extend(missing_pairs)

        consolidated_pairs = []
        seen_pairs = set()
        for profile in runtime_profiles:
            dedup_pairs = []
            for symbol in profile["pairs"]:
                if symbol in seen_pairs:
                    continue
                seen_pairs.add(symbol)
                dedup_pairs.append(symbol)
                consolidated_pairs.append(symbol)
            profile["pairs"] = dedup_pairs

        config.TRADING_PAIRS = list(consolidated_pairs)
        config.STRATEGY_PROFILES = [
            {
                "name": profile["name"],
                "enabled": True,
                "entry_mode": profile["entry_mode"],
                "pairs": list(profile["pairs"]),
            }
            for profile in runtime_profiles
        ]

        self._strategy_engines = {profile["name"]: profile["strategy"] for profile in runtime_profiles}
        self.strategy_profiles = runtime_profiles
        self.strategy = runtime_profiles[0]["strategy"]

        logger.info(
            "🧠 Perfis de estratégia recarregados (%s): %s perfil(is), %s par(es).",
            reason,
            len(runtime_profiles),
            len(consolidated_pairs),
        )

    def _sync_strategy_profiles_with_trading_pairs(
        self,
        reason: str,
        primary_pairs: List[str] | None = None,
    ):
        """
        Sincroniza STRATEGY_PROFILES com TRADING_PAIRS.

        Quando primary_pairs é informado, substitui os pares do profile primário.
        """
        raw_profiles = list(getattr(config, "STRATEGY_PROFILES", []) or [])
        if not raw_profiles:
            raw_profiles = [{"name": "primary", "enabled": True, "entry_mode": "strong_only", "pairs": []}]

        normalized_profiles: List[dict] = []
        for index, raw_profile in enumerate(raw_profiles, start=1):
            if isinstance(raw_profile, dict):
                profile = dict(raw_profile)
            else:
                profile = {}
            profile.setdefault("name", f"strategy_{index}")
            profile.setdefault("enabled", True)
            profile.setdefault("entry_mode", "strong_only")
            profile.setdefault("pairs", [])
            normalized_profiles.append(profile)

        primary_index = next(
            (idx for idx, profile in enumerate(normalized_profiles) if bool(profile.get("enabled", True))),
            0,
        )
        if not normalized_profiles:
            normalized_profiles = [{"name": "primary", "enabled": True, "entry_mode": "strong_only", "pairs": []}]
            primary_index = 0

        if primary_pairs is not None:
            normalized_profiles[primary_index]["pairs"] = self._filter_disabled_pairs(primary_pairs)

        # Se TRADING_PAIRS contém pares fora dos profiles, injeta no primário.
        trading_pairs = self._filter_disabled_pairs(getattr(config, "TRADING_PAIRS", []) or [])
        profile_pairs = []
        for profile in normalized_profiles:
            profile_pairs.extend(self._filter_disabled_pairs(profile.get("pairs", [])))
        profile_pairs_set = set(profile_pairs)
        missing = [symbol for symbol in trading_pairs if symbol not in profile_pairs_set]
        if missing:
            base_pairs = self._filter_disabled_pairs(normalized_profiles[primary_index].get("pairs", []))
            normalized_profiles[primary_index]["pairs"] = base_pairs + [symbol for symbol in missing if symbol not in set(base_pairs)]

        config.STRATEGY_PROFILES = normalized_profiles
        self._reload_strategy_profiles(reason=reason)

    def _build_analysis_tasks(self) -> List[Dict[str, str]]:
        """Monta fila de análise com contexto de estratégia por par."""
        if not getattr(self, "strategy_profiles", None):
            self._reload_strategy_profiles(reason="analysis-build")

        tasks: List[Dict[str, str]] = []
        seen_pairs = set()
        for profile in list(getattr(self, "strategy_profiles", []) or []):
            profile_name = str(profile.get("name", "primary"))
            for symbol in self._filter_disabled_pairs(profile.get("pairs", [])):
                if symbol in seen_pairs:
                    continue
                seen_pairs.add(symbol)
                tasks.append({"symbol": symbol, "strategy_name": profile_name})

        if tasks:
            return tasks

        # Fallback de compatibilidade para configurações legadas.
        for symbol in self._filter_disabled_pairs(getattr(config, "TRADING_PAIRS", []) or []):
            tasks.append({"symbol": symbol, "strategy_name": "primary"})
        return tasks

    def _resolve_strategy_context(self, symbol: str, strategy_name: str | None = None) -> Dict[str, Any]:
        """Resolve engine + parâmetros do perfil para um símbolo."""
        profiles = list(getattr(self, "strategy_profiles", []) or [])
        if not profiles:
            fallback_strategy = getattr(self, "strategy", None)
            if fallback_strategy is not None:
                return {
                    "name": str(strategy_name or "primary"),
                    "entry_mode": "strong_only",
                    "pairs": [str(symbol).upper()],
                    "strategy": fallback_strategy,
                }
            self._reload_strategy_profiles(reason="analysis-resolve")
            profiles = list(getattr(self, "strategy_profiles", []) or [])

        if strategy_name:
            for profile in profiles:
                if str(profile.get("name")) == str(strategy_name):
                    return profile

        normalized_symbol = str(symbol).upper()
        for profile in profiles:
            if normalized_symbol in set(profile.get("pairs", [])):
                return profile

        fallback_strategy = getattr(self, "strategy", None) or HedgeStrategy()
        if not hasattr(self, "strategy"):
            self.strategy = fallback_strategy
        return {
            "name": str(strategy_name or "primary"),
            "entry_mode": "strong_only",
            "pairs": [normalized_symbol],
            "strategy": fallback_strategy,
        }

    def _refresh_binance_coin_universe(self, trigger_reason: str = "runtime") -> list:
        """
        Atualiza BINANCE_COIN_LIST com pares tradáveis atuais da Binance Futures.

        Retorna a lista habilitada (já sem pares desabilitados).
        Em caso de falha, mantém a última lista conhecida.
        """
        if not hasattr(self, "pair_selector") or self.pair_selector is None:
            self.pair_selector = PairSelector(self.exchange, config)

        previous_universe = list(getattr(config, "BINANCE_COIN_LIST", []) or [])
        previous_enabled = self._filter_disabled_pairs(previous_universe)

        try:
            fresh_pairs = self.pair_selector.get_all_futures_pairs()
        except Exception as e:
            logger.warning(f"⚠️ Falha ao atualizar universo Binance ({trigger_reason}): {e}")
            fresh_pairs = []

        normalized_fresh = (
            config.normalize_pair_list(fresh_pairs)
            if hasattr(config, "normalize_pair_list")
            else [str(item).upper() for item in fresh_pairs]
        )

        if normalized_fresh:
            config.BINANCE_COIN_LIST = normalized_fresh
            enabled = self._filter_disabled_pairs(normalized_fresh)
            logger.info(
                "📚 Universo Binance atualizado (%s): %s pares tradáveis (%s habilitados)",
                trigger_reason,
                len(normalized_fresh),
                len(enabled),
            )
            return enabled

        if previous_enabled:
            logger.warning(
                "⚠️ Universo Binance indisponível (%s). Mantendo lista anterior com %s pares.",
                trigger_reason,
                len(previous_enabled),
            )
            return previous_enabled

        fallback_pairs = self._filter_disabled_pairs(list(getattr(config, "TRADING_PAIRS", []) or []))
        if fallback_pairs:
            logger.warning(
                "⚠️ Universo Binance vazio (%s). Usando pares ativos atuais (%s).",
                trigger_reason,
                len(fallback_pairs),
            )
            return fallback_pairs

        logger.error("❌ Universo Binance vazio e sem fallback de pares (%s).", trigger_reason)
        return []

    def refresh_trading_pairs(self, trigger_reason: str = "manual") -> Dict[str, Any]:
        """
        Recalcula a lista ativa de pares imediatamente, respeitando pares desabilitados.
        """
        old_pairs = list(config.TRADING_PAIRS)

        if config.USE_BINANCE_STRATEGY:
            # Mantém regra atual de escolha (score), apenas filtra pares desabilitados.
            if hasattr(self, "binance_strategy") and self.binance_strategy:
                num_coins = int(self.binance_strategy.get("num_coins", len(old_pairs) or 0))
            else:
                num_coins = len(old_pairs)

            new_pairs = self.sort_binance_coins_by_score(num_coins=max(0, num_coins))
            if hasattr(self, "binance_strategy") and self.binance_strategy is not None:
                self.binance_strategy["coins"] = list(new_pairs)
        elif config.AUTO_SELECT_PAIRS and self.pair_selector is not None:
            self._refresh_binance_coin_universe(trigger_reason=f"refresh:{trigger_reason}")
            available_capital = self.exchange.get_available_balance()
            selected_pairs, _scores = self.pair_selector.select_best_pairs(
                available_capital=available_capital
            )
            new_pairs = selected_pairs
        else:
            new_pairs = list(config.TRADING_PAIRS)

        config.TRADING_PAIRS = self._filter_disabled_pairs(new_pairs)
        if config.USE_BINANCE_STRATEGY or config.AUTO_SELECT_PAIRS:
            self._sync_strategy_profiles_with_trading_pairs(
                reason=f"refresh:{trigger_reason}",
                primary_pairs=config.TRADING_PAIRS,
            )
        else:
            self._sync_strategy_profiles_with_trading_pairs(
                reason=f"refresh:{trigger_reason}",
            )

        for symbol in config.TRADING_PAIRS:
            if symbol not in self.pnl_by_symbol:
                self.pnl_by_symbol[symbol] = 0.0

        old_set = set(old_pairs)
        new_set = set(config.TRADING_PAIRS)
        added_pairs = sorted(new_set - old_set)
        removed_pairs = sorted(old_set - new_set)

        for symbol in added_pairs:
            self.exchange.set_leverage(symbol, config.LEVERAGE)

        self.cache_pairs_min_notional()
        logger.info(
            "🔄 Lista de pares atualizada (%s): %s",
            trigger_reason,
            ", ".join(config.TRADING_PAIRS) if config.TRADING_PAIRS else "nenhum par habilitado",
        )

        return {
            "old_pairs": old_pairs,
            "new_pairs": list(config.TRADING_PAIRS),
            "added_pairs": added_pairs,
            "removed_pairs": removed_pairs,
        }
    
    def setup_exchange(self):
        """
        Configura a exchange antes de começar a operar.
        
        - Ativa Hedge Mode
        - Define alavancagem para cada par
        - Busca taxas de comissão atuais
        - Define capital inicial (saldo real da carteira)
        """
        logger.info("⚙️  Configurando exchange...")
        
        # Ativa Hedge Mode (essencial para a estratégia)
        if not self.exchange.set_hedge_mode():
            logger.error("❌ Não foi possível ativar Hedge Mode!")
            return False

        # Recarrega perfis para garantir TRADING_PAIRS consistente antes do setup.
        self._reload_strategy_profiles(reason="setup-start")
        
        # Define alavancagem para cada par
        for symbol in config.TRADING_PAIRS:
            self.exchange.set_leverage(symbol, config.LEVERAGE)
        
        # Busca taxas de comissão atuais da API
        self.update_commission_rates()
        
        # Define o capital inicial
        # Se foi carregado do estado salvo, usa ele; senão, busca o saldo atual
        current_balance = self.exchange.get_account_balance()  # Saldo total da carteira
        
        # Busca P&L diário REAL da Binance para log
        daily_pnl = self.exchange.get_daily_pnl_from_binance()
        pnl_real = daily_pnl['total']
        
        if hasattr(self, '_loaded_initial_capital') and self._loaded_initial_capital is not None:
            # Usa o capital carregado do estado (preserva valor original)
            self.initial_capital = self._loaded_initial_capital
            logger.info(f"💰 Capital inicial (do estado salvo): ${self.initial_capital:.2f}")
            logger.info(f"💵 Saldo atual da carteira: ${current_balance:.2f}")
            logger.info(f"📊 P&L do dia (Binance): ${pnl_real:+.2f}")
        else:
            # Primeira execução ou sem estado salvo - usa saldo atual
            self.initial_capital = current_balance
            logger.info(f"💰 Capital inicial (saldo da carteira): ${self.initial_capital:.2f}")

        # Inicializa rastreamento de transferências (se não houver estado anterior)
        if self.last_transfer_check_ts_ms <= 0:
            self.last_transfer_check_ts_ms = int(time.time() * 1000)
        tracked_limit = max(100, int(config.CAPITAL_TRANSFER_TRACKED_IDS_LIMIT))
        if len(self.processed_transfer_ids) > tracked_limit:
            self.processed_transfer_ids = self.processed_transfer_ids[-tracked_limit:]
        
        # Mostra configuração de capital por trade (baseado no saldo ATUAL)
        trade_value = current_balance * config.MAX_POSITION_PERCENT
        logger.info("📊 Sistema de capital flexível:")
        logger.info(f"   • Saldo atual: ${current_balance:.2f}")
        logger.info(f"   • Por trade: {config.MAX_POSITION_PERCENT * 100:.0f}% = ${trade_value:.2f} (ou mínimo da moeda)")
        logger.info(f"   • Alavancagem: {config.LEVERAGE}x")

        if config.USE_BINANCE_STRATEGY or config.AUTO_SELECT_PAIRS:
            self._refresh_binance_coin_universe(trigger_reason="setup")
        
        # ============================================
        # ESTRATÉGIA BINANCE PADRÃO (por faixa de capital)
        # ============================================
        if config.USE_BINANCE_STRATEGY:
            logger.info("📊 Usando ESTRATÉGIA BINANCE PADRÃO...")
            strategy = config.get_binance_strategy_for_capital(current_balance)
            
            # Ordena as moedas da lista Binance pelo score (spread, volume, volatility, etc)
            logger.info("🔄 Ordenando moedas pelo score...")
            if not hasattr(self, "pair_selector") or self.pair_selector is None:
                self.pair_selector = PairSelector(self.exchange, config)
            sorted_coins = self.sort_binance_coins_by_score(strategy['num_coins'])
            
            # Atualiza estratégia com moedas ordenadas
            strategy['coins'] = sorted_coins
            config.TRADING_PAIRS = self._filter_disabled_pairs(sorted_coins)  # IMPORTANTE: Atualiza TRADING_PAIRS
            self.binance_strategy = strategy
            self._sync_strategy_profiles_with_trading_pairs(
                reason="setup-binance",
                primary_pairs=config.TRADING_PAIRS,
            )
            
            # Atualiza pnl_by_symbol para incluir os pares
            for symbol in config.TRADING_PAIRS:
                if symbol not in self.pnl_by_symbol:
                    self.pnl_by_symbol[symbol] = 0.0
            
            # Define alavancagem para os pares
            for symbol in config.TRADING_PAIRS:
                self.exchange.set_leverage(symbol, config.LEVERAGE)
            
            logger.info(f"   📈 Faixa de Capital: {strategy['capital_range']}")
            logger.info(f"   💵 Order Size: ${strategy['order_size']}")
            logger.info(f"   🛑 Stop Loss: ${strategy['stop_loss']}")
            logger.info(
                f"   🪙 Moedas ({len(config.TRADING_PAIRS)}): "
                f"{', '.join([c.replace('USDT', '') for c in config.TRADING_PAIRS])}"
            )
            
            # Notifica no Telegram
            coins_display = ', '.join([c.replace('USDT', '') for c in config.TRADING_PAIRS])
            self.telegram.send_message(
                f"📊 <b>ESTRATÉGIA BINANCE PADRÃO</b>\n\n"
                f"💰 <b>Saldo:</b> ${current_balance:.2f}\n"
                f"📈 <b>Faixa:</b> {strategy['capital_range']}\n"
                f"💵 <b>Order Size:</b> ${strategy['order_size']}\n"
                f"🛑 <b>Stop Loss:</b> ${strategy['stop_loss']}\n"
                f"🪙 <b>Moedas ({len(config.TRADING_PAIRS)}) - Ordenadas por Score:</b>\n{coins_display}\n\n"
                f"<i>Atualização a cada 6 horas</i>"
            )
        
        # ============================================
        # SELEÇÃO INTELIGENTE DE PARES (só se não usar estratégia Binance)
        # ============================================
        elif config.AUTO_SELECT_PAIRS:
            logger.info("🤖 Iniciando seleção inteligente de pares...")
            self.pair_selector = PairSelector(self.exchange, config)
            
            # Seleciona os melhores pares baseado no capital disponível
            selected_pairs, scores = self.pair_selector.select_best_pairs(
                available_capital=current_balance
            )
            
            # Atualiza a configuração
            config.TRADING_PAIRS = self._filter_disabled_pairs(selected_pairs)
            self.last_pair_update = datetime.now()
            self._sync_strategy_profiles_with_trading_pairs(
                reason="setup-auto-select",
                primary_pairs=config.TRADING_PAIRS,
            )
            
            # Atualiza pnl_by_symbol para incluir novos pares
            for symbol in config.TRADING_PAIRS:
                if symbol not in self.pnl_by_symbol:
                    self.pnl_by_symbol[symbol] = 0.0
            
            # Define alavancagem para os novos pares
            for symbol in config.TRADING_PAIRS:
                self.exchange.set_leverage(symbol, config.LEVERAGE)
            
            active_fixed_pairs = self._filter_disabled_pairs(config.FIXED_PAIRS)
            # Notifica no Telegram
            self.telegram.send_message(
                f"🤖 <b>SELEÇÃO DE PARES</b>\n\n"
                f"📌 <b>Fixos:</b> {', '.join(active_fixed_pairs)}\n"
                f"🔄 <b>Dinâmicos:</b> {', '.join([p for p in config.TRADING_PAIRS if p not in active_fixed_pairs])}\n\n"
                f"<i>Próxima atualização em {config.PAIR_UPDATE_INTERVAL_MINUTES // 60}h</i>"
            )
        
        if not config.USE_BINANCE_STRATEGY and not config.AUTO_SELECT_PAIRS:
            self._sync_strategy_profiles_with_trading_pairs(reason="setup-static")

        # ============================================
        # CACHEIA VALORES MÍNIMOS (DEPOIS da estratégia)
        # ============================================
        # Isso garante que usamos os pares corretos da estratégia
        self.cache_pairs_min_notional()
        logger.info(f"📋 Pares finais configurados: {len(config.TRADING_PAIRS)}")
        for symbol in config.TRADING_PAIRS:
            logger.info(f"   • {symbol}")
        
        logger.info("✅ Exchange configurada!")
        
        # ============================================
        # INICIALIZA RASTREAMENTO DE POSIÇÕES
        # ============================================
        # Carrega posições já abertas para o tracking
        # Isso evita perder rastreamento após reinício do bot
        existing_positions = self.exchange.get_open_positions()
        for pos in existing_positions:
            position_key = f"{pos['symbol']}_{pos['side']}"
            self.known_positions[position_key] = {
                'symbol': pos['symbol'],
                'side': pos['side'],
                'entry_price': pos['entry_price'],
                'quantity': pos['quantity'],
                'last_seen': datetime.now()
            }
            logger.info(f"📍 Posição existente registrada: {position_key}")
        
        if existing_positions:
            logger.info(f"📊 {len(existing_positions)} posições existentes carregadas no tracking")
        
        return True
    
    def check_daily_targets(self):
        """
        Verifica se as metas diárias foram atingidas.
        
        - Se lucro do dia >= DAILY_PROFIT_TARGET → Fecha TUDO e para
        - Se prejuízo do dia >= DAILY_LOSS_LIMIT → Fecha TUDO e para
        
        Também reseta os contadores à meia-noite.
        """
        if not config.USE_DAILY_TARGETS:
            return False  # Metas diárias desativadas
        
        # Verifica se precisa resetar (novo dia)
        today = datetime.now().date()
        if today > self.last_daily_reset:
            logger.info("🌅 Novo dia detectado! Resetando metas diárias...")
            self.daily_target_reached = False
            self.daily_target_type = None
            self.daily_realized_pnl = 0.0
            self.last_daily_reset = today
            
            # Notifica reset
            self.telegram.send_message(
                "🌅 <b>NOVO DIA DE TRADING</b>\n\n"
                f"📈 Meta de Lucro: <code>+${config.DAILY_PROFIT_TARGET:.2f}</code>\n"
                f"📉 Limite de Perda: <code>-${config.DAILY_LOSS_LIMIT:.2f}</code>\n\n"
                "<i>Bot pronto para operar!</i>"
            )
        
        # Se já atingiu a meta, retorna True
        if self.daily_target_reached:
            return True
        
        # Verifica meta de LUCRO
        if self.daily_realized_pnl >= config.DAILY_PROFIT_TARGET:
            self.daily_target_reached = True
            self.daily_target_type = 'PROFIT'
            logger.info(f"🎯 META DE LUCRO ATINGIDA! P&L: ${self.daily_realized_pnl:.2f}")
            
            # Fecha TODAS as posições abertas para garantir o lucro
            self._close_all_positions_daily_target("Meta de Lucro Diário")
            
            self.telegram.send_message(
                "🎯 <b>META DE LUCRO DIÁRIO ATINGIDA!</b> 🎉\n\n"
                f"💰 P&L do Dia: <code>+${self.daily_realized_pnl:.2f}</code>\n"
                f"🎯 Meta: <code>+${config.DAILY_PROFIT_TARGET:.2f}</code>\n\n"
                "✅ <b>Todas as posições foram fechadas!</b>\n"
                "⏸️ <b>Bot pausado até amanhã!</b>"
            )
            return True
        
        # Verifica meta de PERDA
        if self.daily_realized_pnl <= -config.DAILY_LOSS_LIMIT:
            self.daily_target_reached = True
            self.daily_target_type = 'LOSS'
            logger.info(f"🛑 LIMITE DE PERDA ATINGIDO! P&L: ${self.daily_realized_pnl:.2f}")
            
            # Fecha TODAS as posições abertas para evitar mais perdas
            self._close_all_positions_daily_target("Limite de Perda Diário")
            
            self.telegram.send_message(
                "🛑 <b>LIMITE DE PERDA DIÁRIO ATINGIDO!</b>\n\n"
                f"💸 P&L do Dia: <code>${self.daily_realized_pnl:.2f}</code>\n"
                f"🛑 Limite: <code>-${config.DAILY_LOSS_LIMIT:.2f}</code>\n\n"
                "✅ <b>Todas as posições foram fechadas!</b>\n"
                "⏸️ <b>Bot pausado até amanhã!</b>"
            )
            return True
        
        return False
    
    def _close_all_positions_daily_target(self, reason: str):
        """
        Fecha TODAS as posições abertas quando a meta diária é atingida.
        Garante que o lucro/perda seja realizado.
        """
        positions = self.exchange.get_open_positions()
        
        if not positions:
            logger.info("📭 Nenhuma posição aberta para fechar")
            return
        
        logger.info(f"🔒 Fechando {len(positions)} posições - Motivo: {reason}")
        
        for pos in positions:
            symbol = pos['symbol']
            side = pos['side']
            
            try:
                logger.info(f"   Fechando {side} {symbol}...")
                self.exchange.close_position(symbol, side)
                
                # Limpa dados de trailing
                position_key = f"{symbol}_{side}"
                self._clear_trailing_data(position_key)
                if position_key in self.known_positions:
                    del self.known_positions[position_key]
                    
            except Exception as e:
                logger.error(f"   ❌ Erro ao fechar {side} {symbol}: {e}")
        
        logger.info(f"✅ Posições fechadas - {reason}")

    @staticmethod
    def _build_transfer_event_id(item: dict) -> str:
        """Monta um ID estável para deduplicar eventos de TRANSFER."""
        for key in ('tranId', 'transactionId', 'id', 'tradeId'):
            value = item.get(key)
            if value not in (None, "", 0, "0"):
                return f"{key}:{value}"

        event_time = int(item.get('time', 0) or 0)
        asset = str(item.get('asset', 'USDT') or 'USDT').upper()
        try:
            amount = float(item.get('income', 0) or 0.0)
        except (TypeError, ValueError):
            amount = 0.0
        info = str(item.get('info', '') or '')
        return f"fallback:{event_time}:{asset}:{amount:.8f}:{info[:40]}"

    def _mark_transfer_event_processed(self, event_id: str):
        """Registra um evento como processado e limita histórico em memória."""
        if not event_id:
            return

        if event_id in self.processed_transfer_ids:
            return

        self.processed_transfer_ids.append(event_id)
        tracked_limit = max(100, int(config.CAPITAL_TRANSFER_TRACKED_IDS_LIMIT))
        if len(self.processed_transfer_ids) > tracked_limit:
            self.processed_transfer_ids = self.processed_transfer_ids[-tracked_limit:]
    
    def check_for_deposit(self):
        """
        Detecta TRANSFER (entrada/saída) na Futures e ajusta capital base.

        Usa incomeType=TRANSFER da Binance para evitar falso positivo por P&L.
        """
        if not config.CAPITAL_TRANSFER_DETECTION_ENABLED:
            return

        if self.initial_capital is None:
            return

        now_ts_ms = int(time.time() * 1000)
        if self.last_transfer_check_ts_ms <= 0:
            self.last_transfer_check_ts_ms = now_ts_ms
            return

        try:
            # Sobrepõe 60s entre janelas para não perder evento no limite.
            start_time = max(0, self.last_transfer_check_ts_ms - 60_000)
            transfer_events = self.exchange.get_income_history(
                income_type='TRANSFER',
                limit=1000,
                start_time=start_time
            )

            if not transfer_events:
                self.last_transfer_check_ts_ms = now_ts_ms
                return

            known_ids = set(self.processed_transfer_ids)
            min_abs = max(0.0, float(config.CAPITAL_TRANSFER_MIN_ABS_USDT))
            latest_event_ts = self.last_transfer_check_ts_ms
            net_transfer_usdt = 0.0
            relevant_events = []

            ordered_events = sorted(
                transfer_events,
                key=lambda event: int(event.get('time', 0) or 0)
            )

            for event in ordered_events:
                event_id = self._build_transfer_event_id(event)
                if event_id in known_ids:
                    continue

                known_ids.add(event_id)
                self._mark_transfer_event_processed(event_id)

                event_ts = int(event.get('time', 0) or 0)
                if event_ts > latest_event_ts:
                    latest_event_ts = event_ts

                asset = str(event.get('asset', 'USDT') or 'USDT').upper()
                try:
                    amount = float(event.get('income', 0) or 0.0)
                except (TypeError, ValueError):
                    continue
                if asset != 'USDT':
                    continue
                if abs(amount) < min_abs:
                    continue

                net_transfer_usdt += amount
                relevant_events.append({
                    'time': event_ts,
                    'amount': amount,
                })

            self.last_transfer_check_ts_ms = max(now_ts_ms, latest_event_ts)

            if not relevant_events:
                return

            old_initial_capital = float(self.initial_capital)
            new_initial_capital = old_initial_capital + net_transfer_usdt

            # Proteção para evitar base inválida após saque extremo.
            if new_initial_capital <= 0:
                logger.warning(
                    f"⚠️ Ajuste de capital ficou <= 0 ({new_initial_capital:.2f}). "
                    "Aplicando piso de $1.00 para manter cálculos estáveis."
                )
                new_initial_capital = 1.0

            self.initial_capital = new_initial_capital

            movement_type = "ENTRADA" if net_transfer_usdt > 0 else "SAÍDA"
            movement_emoji = "💰" if net_transfer_usdt > 0 else "💸"

            logger.info(
                f"{movement_emoji} Transferência detectada ({movement_type}): "
                f"${net_transfer_usdt:+.2f} | Capital base: "
                f"${old_initial_capital:.2f} → ${new_initial_capital:.2f}"
            )

            # Mostra até 5 eventos para contexto
            event_lines = []
            for event in relevant_events[-5:]:
                event_time = datetime.fromtimestamp(event['time'] / 1000).strftime("%d/%m %H:%M:%S")
                event_lines.append(f"• {event_time}: <code>${event['amount']:+.2f}</code>")
            events_text = "\n".join(event_lines)

            self.telegram.send_message(
                f"{movement_emoji} <b>MOVIMENTAÇÃO DE CAPITAL DETECTADA</b>\n\n"
                f"📥📤 <b>Tipo:</b> {movement_type}\n"
                f"💵 <b>Variação líquida:</b> <code>${net_transfer_usdt:+.2f}</code>\n"
                f"🧮 <b>Capital base (SL global):</b>\n"
                f"   • Anterior: <code>${old_initial_capital:.2f}</code>\n"
                f"   • Novo: <code>${new_initial_capital:.2f}</code>\n\n"
                f"<b>Últimos eventos:</b>\n{events_text}"
            )

            # Persiste imediatamente para sobreviver reinício.
            self.save_state()

        except Exception as e:
            logger.warning(f"⚠️ Erro ao detectar transferência de capital: {e}")
    
    def check_and_update_binance_strategy(self):
        """
        Verifica se o capital mudou de faixa e atualiza a estratégia Binance.
        
        Isso é chamado periodicamente para ajustar:
        - Lista de moedas (ordenadas por score)
        - Tamanho da ordem
        - Stop Loss
        
        Conforme o capital cresce ou diminui.
        """
        if not config.USE_BINANCE_STRATEGY:
            return
        
        try:
            # Busca saldo atual
            account_info = self.exchange.get_account_info()
            current_balance = account_info['wallet_balance']
            
            # Pega a estratégia atual para o capital
            new_strategy = config.get_binance_strategy_for_capital(current_balance)
            
            # Verifica se mudou de faixa
            old_strategy = getattr(self, 'binance_strategy', None)
            
            if old_strategy is None or old_strategy['capital_range'] != new_strategy['capital_range']:
                # Mudou de faixa! Atualiza
                logger.info("📊 MUDANÇA DE FAIXA DETECTADA!")
                if old_strategy:
                    logger.info(f"   Anterior: {old_strategy['capital_range']} ({old_strategy['num_coins']} moedas)")
                logger.info(f"   Nova: {new_strategy['capital_range']} ({new_strategy['num_coins']} moedas)")
                
                # Ordena as moedas pelo score
                sorted_coins = self.sort_binance_coins_by_score(new_strategy['num_coins'])
                new_strategy['coins'] = sorted_coins
                
                # Atualiza configurações
                self.binance_strategy = new_strategy
                config.TRADING_PAIRS = self._filter_disabled_pairs(sorted_coins)
                self._sync_strategy_profiles_with_trading_pairs(
                    reason="binance-tier-change",
                    primary_pairs=config.TRADING_PAIRS,
                )
                
                # Atualiza pnl_by_symbol para incluir novos pares
                for symbol in config.TRADING_PAIRS:
                    if symbol not in self.pnl_by_symbol:
                        self.pnl_by_symbol[symbol] = 0.0
                
                # Define alavancagem para os novos pares
                for symbol in config.TRADING_PAIRS:
                    self.exchange.set_leverage(symbol, config.LEVERAGE)
                
                # Re-cacheia
                self.cache_pairs_min_notional()
                
                # Notifica no Telegram
                coins_display = ', '.join([c.replace('USDT', '') for c in config.TRADING_PAIRS])
                self.telegram.send_message(
                    f"📊 <b>MUDANÇA DE FAIXA</b>\n\n"
                    f"💰 <b>Saldo Atual:</b> ${current_balance:.2f}\n"
                    f"📈 <b>Nova Faixa:</b> {new_strategy['capital_range']}\n"
                    f"💵 <b>Order Size:</b> ${new_strategy['order_size']}\n"
                    f"🛑 <b>Stop Loss:</b> ${new_strategy['stop_loss']}\n"
                    f"🪙 <b>Moedas ({len(config.TRADING_PAIRS)}) - Por Score:</b>\n{coins_display}"
                )
                
        except Exception as e:
            logger.error(f"Erro ao verificar estratégia Binance: {e}")
    
    def cache_pairs_min_notional(self):
        """
        Busca e cacheia o valor mínimo de cada par uma vez no início.
        Evita chamadas repetidas à API durante o loop principal.
        
        Também ordena os pares do menor mínimo para o maior, permitindo
        operar o máximo de pares possível com o capital disponível.
        """
        self.pairs_min_notional = {}
        pairs_with_min = []
        
        logger.info("📊 Buscando valores mínimos dos pares...")
        
        for symbol in config.TRADING_PAIRS:
            info = self.exchange.get_symbol_info(symbol)
            min_notional = info.get('minNotional', 5.0)
            self.pairs_min_notional[symbol] = min_notional
            pairs_with_min.append((symbol, min_notional))
        
        # Ordena do menor mínimo para o maior
        pairs_with_min.sort(key=lambda x: x[1])
        
        # Guarda a lista ordenada
        self.sorted_pairs = pairs_with_min
        
        # Log da ordem de processamento
        logger.info("📋 Ordem de processamento (menor mínimo primeiro):")
        for symbol, min_val in pairs_with_min:
            logger.info(f"   • {symbol}: mínimo ${min_val:.2f}")
    
    def update_trading_pairs(self):
        """
        Atualiza a lista de pares de trading usando seleção inteligente.
        
        Chamado periodicamente (configurável em config.PAIR_UPDATE_INTERVAL_MINUTES).
        Fecha posições de pares removidos e configura novos pares.
        """
        if not config.AUTO_SELECT_PAIRS or not self.pair_selector:
            return
        
        # Verifica se está na hora de atualizar
        if not self.pair_selector.should_update():
            return
        
        logger.info("🔄 Atualizando lista de pares...")
        
        # Guarda pares antigos
        old_pairs = set(config.TRADING_PAIRS)

        self._refresh_binance_coin_universe(trigger_reason="auto-update")
        
        # Busca capital disponível atual
        available_capital = self.exchange.get_available_balance()
        
        # Seleciona novos pares baseado no capital disponível
        selected_pairs, scores = self.pair_selector.select_best_pairs(
            available_capital=available_capital
        )
        new_pairs = set(selected_pairs)
        
        # Identifica mudanças
        removed_pairs = old_pairs - new_pairs
        added_pairs = new_pairs - old_pairs
        
        if not removed_pairs and not added_pairs:
            logger.info("✅ Nenhuma mudança nos pares")
            return
        
        # Log das mudanças
        if removed_pairs:
            logger.info(f"📤 Pares REMOVIDOS: {', '.join(removed_pairs)}")
        if added_pairs:
            logger.info(f"📥 Pares ADICIONADOS: {', '.join(added_pairs)}")
        
        # Fecha posições dos pares removidos
        if removed_pairs:
            positions = self.exchange.get_open_positions()
            for pos in positions:
                if pos['symbol'] in removed_pairs:
                    logger.info(f"🔴 Fechando posição em {pos['symbol']} (par removido)")
                    closed = self._close_position_with_notification(pos, "Par removido da lista")
                    if not closed:
                        logger.warning(
                            f"⚠️ Falha ao fechar posição de {pos['symbol']} durante remoção de par."
                        )
        
        # Atualiza configuração
        config.TRADING_PAIRS = self._filter_disabled_pairs(selected_pairs)
        self.last_pair_update = datetime.now()
        self._sync_strategy_profiles_with_trading_pairs(
            reason="auto-select-update",
            primary_pairs=config.TRADING_PAIRS,
        )
        
        # Atualiza pnl_by_symbol para incluir novos pares
        for symbol in config.TRADING_PAIRS:
            if symbol not in self.pnl_by_symbol:
                self.pnl_by_symbol[symbol] = 0.0
        
        # Re-cacheia com os novos pares
        self.cache_pairs_min_notional()
        
        # Define alavancagem para os novos pares
        for symbol in added_pairs:
            self.exchange.set_leverage(symbol, config.LEVERAGE)
        
        active_fixed_pairs = self._filter_disabled_pairs(config.FIXED_PAIRS)
        # Notifica no Telegram
        msg = "🔄 <b>ATUALIZAÇÃO DE PARES</b>\n\n"
        msg += f"📌 <b>Fixos:</b> {', '.join(active_fixed_pairs)}\n"
        msg += f"🔄 <b>Dinâmicos:</b> {', '.join([p for p in config.TRADING_PAIRS if p not in active_fixed_pairs])}\n\n"
        
        if removed_pairs:
            msg += f"📤 <b>Removidos:</b> {', '.join(removed_pairs)}\n"
        if added_pairs:
            msg += f"📥 <b>Adicionados:</b> {', '.join(added_pairs)}\n"
        
        msg += f"\n<i>Próxima atualização em {config.PAIR_UPDATE_INTERVAL_MINUTES // 60}h</i>"
        
        self.telegram.send_message(msg)
        
        logger.info("✅ Lista de pares atualizada!")
    
    def sort_binance_coins_by_score(self, num_coins: int) -> list:
        """
        Ordena os pares tradáveis da Binance pelo score e retorna os melhores.
        
        Usa os critérios de seleção:
        - spread: 35% (menor = melhor)
        - volume: 30% (maior = melhor)
        - volatility: 20% (maior = melhor)
        - trend: 10% (mais forte = melhor)
        - funding: 5% (menor = melhor)
        
        Args:
            num_coins: Quantidade de moedas para retornar
            
        Returns:
            Lista das melhores moedas ordenadas por score
        """
        if num_coins <= 0:
            return []

        candidate_coins = self._refresh_binance_coin_universe(
            trigger_reason=f"score:{num_coins}"
        )

        if not candidate_coins:
            logger.warning("⚠️ Sem pares candidatos para calcular score")
            return []

        logger.info(f"📊 Calculando scores para {len(candidate_coins)} moedas...")
        
        # Calcula score de cada moeda da lista Binance
        coins_with_scores = []
        
        for symbol in candidate_coins:
            try:
                # Busca métricas do par usando PairSelector
                metrics = self.pair_selector.get_pair_metrics(symbol)
                
                if metrics:
                    # Calcula o score usando a função do PairSelector
                    score = self.pair_selector.score_pair(metrics)
                    
                    if score > 0:
                        coins_with_scores.append((symbol, score))
                        logger.debug(f"   {symbol}: score {score:.2f}")
                    
            except Exception as e:
                logger.warning(f"   ⚠️ Erro ao calcular score de {symbol}: {e}")
                continue
        
        # Ordena pelo score (maior primeiro)
        coins_with_scores.sort(key=lambda x: x[1], reverse=True)
        
        # Log do ranking
        logger.info(f"🏆 Top {num_coins} moedas por score:")
        for i, (symbol, score) in enumerate(coins_with_scores[:num_coins], 1):
            logger.info(f"   {i}. {symbol.replace('USDT', '')}: {score:.2f}")
        
        # Retorna apenas os símbolos (sem os scores)
        best_coins = [coin for coin, score in coins_with_scores[:num_coins]]
        
        # Se não conseguiu scores suficientes, completa com a ordem padrão
        if len(best_coins) < num_coins:
            logger.warning(f"⚠️ Só conseguiu {len(best_coins)} scores, completando com ordem padrão")
            for coin in candidate_coins:
                if coin not in best_coins:
                    best_coins.append(coin)
                    if len(best_coins) >= num_coins:
                        break
        
        return best_coins
    
    def update_binance_strategy_coins(self):
        """
        Atualiza a ordenação das moedas da estratégia Binance pelo score.
        Chamado periodicamente para reordenar baseado nas condições de mercado.
        """
        if not config.USE_BINANCE_STRATEGY:
            return
        
        if not hasattr(self, 'binance_strategy') or self.binance_strategy is None:
            return
        
        logger.info("🔄 Atualizando ordenação das moedas Binance...")
        
        num_coins = self.binance_strategy['num_coins']
        old_coins = list(config.TRADING_PAIRS)
        
        # Reordena as moedas pelo score
        new_coins = self.sort_binance_coins_by_score(num_coins)
        
        # Verifica se mudou
        if new_coins == old_coins:
            logger.info("✅ Ordenação das moedas não mudou")
            return
        
        # Atualiza
        config.TRADING_PAIRS = self._filter_disabled_pairs(new_coins)
        self.binance_strategy['coins'] = new_coins
        self._sync_strategy_profiles_with_trading_pairs(
            reason="binance-reorder",
            primary_pairs=config.TRADING_PAIRS,
        )
        
        # Atualiza pnl_by_symbol
        for symbol in new_coins:
            if symbol not in self.pnl_by_symbol:
                self.pnl_by_symbol[symbol] = 0.0
        
        # Define alavancagem para novos pares
        for symbol in new_coins:
            if symbol not in old_coins:
                self.exchange.set_leverage(symbol, config.LEVERAGE)
        
        # Re-cacheia
        self.cache_pairs_min_notional()
        
        # Notifica
        coins_display = ', '.join([c.replace('USDT', '') for c in new_coins])
        self.telegram.send_message(
            f"🔄 <b>MOEDAS REORDENADAS POR SCORE</b>\n\n"
            f"🪙 <b>Nova Ordem ({num_coins}):</b>\n{coins_display}\n\n"
            f"<i>Baseado em: spread, volume, volatilidade, tendência, funding</i>"
        )
        
        logger.info("✅ Moedas reordenadas!")
    
    def update_commission_rates(self):
        """
        Atualiza as taxas de comissão buscando da API da Binance.
        
        As taxas são dinâmicas e podem mudar baseado em:
        - Seu nível VIP (volume de trading)
        - Se você usa BNB para pagar taxas (10% desconto)
        - Promoções temporárias
        """
        try:
            # Busca as taxas usando o primeiro par configurado
            symbol = config.TRADING_PAIRS[0] if config.TRADING_PAIRS else "BTCUSDT"
            rates = self.exchange.get_commission_rates(symbol)
            
            self.commission_rates = rates
            self.last_commission_update = datetime.now()
            
            # Calcula o breakeven (taxa de abrir + fechar)
            breakeven = (rates['taker_rate'] * 2) * 100  # Em percentual
            
            logger.info("💰 Taxas de comissão atualizadas:")
            logger.info(f"   • Maker: {rates['maker_percent']:.4f}%")
            logger.info(f"   • Taker: {rates['taker_percent']:.4f}%")
            logger.info(f"   • Breakeven (abrir + fechar): {breakeven:.4f}%")
            
        except Exception as e:
            logger.warning(f"⚠️ Erro ao atualizar taxas: {e}")
            # Usa taxas padrão
            self.commission_rates = {
                'maker_rate': 0.0002,
                'taker_rate': 0.0005,
                'maker_percent': 0.02,
                'taker_percent': 0.05
            }
    
    def get_taker_fee_rate(self) -> float:
        """
        Retorna a taxa taker atual (em formato decimal, ex: 0.0005 = 0.05%).
        Usa o cache se disponível, senão busca da API.
        """
        if self.commission_rates is None:
            self.update_commission_rates()
        return self.commission_rates.get('taker_rate', 0.0005)

    def set_sentiment_mode(self, enabled: bool, persist: bool = True) -> bool:
        """Liga/desliga o filtro de sentimento para entradas novas."""
        self.sentiment_mode_enabled = bool(enabled)
        if not self.sentiment_mode_enabled:
            # Limpa cache para evitar leitura de viés antigo quando religar.
            self.sentiment_cache = {}

        if persist:
            self.save_state()

        logger.info(
            "🧭 Modo sentimento %s",
            "ATIVADO" if self.sentiment_mode_enabled else "DESATIVADO"
        )
        return self.sentiment_mode_enabled

    def get_sentiment_snapshot(self, symbol: str, force_refresh: bool = False) -> Dict[str, Any]:
        """Retorna snapshot do viés de mercado para um par."""
        return self._get_symbol_sentiment(symbol=symbol, force_refresh=force_refresh)

    def _get_symbol_sentiment(self, symbol: str, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Calcula viés técnico por par em timeframe superior para filtrar direção.

        Direções possíveis:
        - LONG_ONLY
        - SHORT_ONLY
        - BOTH (neutro, não filtra)
        """
        normalized_symbol = str(symbol or "").upper()
        now_monotonic = time.monotonic()
        cache_ttl = max(5, int(getattr(config, "SENTIMENT_CACHE_SECONDS", 300)))

        if not force_refresh:
            cached = self.sentiment_cache.get(normalized_symbol)
            if cached and (now_monotonic - float(cached.get("cached_at_monotonic", 0.0))) < cache_ttl:
                return dict(cached)

        payload: Dict[str, Any] = {
            "symbol": normalized_symbol,
            "bias": "NEUTRAL",
            "direction": "BOTH",
            "score": 0,
            "timeframe": str(getattr(config, "SENTIMENT_TIMEFRAME", "1h")),
            "lookback": int(getattr(config, "SENTIMENT_CANDLES_LOOKBACK", 120)),
            "rsi": 50.0,
            "momentum_pct": 0.0,
            "reason": "insuficiente",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "cached_at_monotonic": now_monotonic,
        }

        try:
            timeframe = payload["timeframe"]
            lookback = max(30, int(payload["lookback"]))
            klines = self.exchange.get_klines(
                symbol=normalized_symbol,
                interval=timeframe,
                limit=lookback,
            )

            if not klines or len(klines) < 30:
                payload["reason"] = "candles insuficientes"
                self.sentiment_cache[normalized_symbol] = dict(payload)
                return dict(payload)

            closes = [float(k["close"]) for k in klines if float(k.get("close", 0) or 0) > 0]
            if len(closes) < 30:
                payload["reason"] = "closes inválidos"
                self.sentiment_cache[normalized_symbol] = dict(payload)
                return dict(payload)

            ta = self.strategy.ta if hasattr(self.strategy, "ta") else None
            if ta is None:
                payload["reason"] = "TA indisponível"
                self.sentiment_cache[normalized_symbol] = dict(payload)
                return dict(payload)

            ema_fast = float(ta.calculate_ema(closes, 20))
            ema_slow = float(ta.calculate_ema(closes, 50))
            rsi = float(ta.calculate_rsi(closes, 14))

            momentum_window = min(24, len(closes) - 1)
            reference_price = closes[-1 - momentum_window] if momentum_window > 0 else closes[0]
            if reference_price > 0:
                momentum_pct = ((closes[-1] - reference_price) / reference_price) * 100
            else:
                momentum_pct = 0.0

            min_score = max(1, int(getattr(config, "SENTIMENT_MIN_SCORE", 2)))
            min_momentum = max(0.0, float(getattr(config, "SENTIMENT_MIN_MOMENTUM_PERCENT", 0.20)))

            score = 0
            if ema_fast > ema_slow:
                score += 2
            else:
                score -= 2

            if rsi >= 60:
                score += 1
            elif rsi <= 40:
                score -= 1

            if momentum_pct >= min_momentum:
                score += 1
            elif momentum_pct <= -min_momentum:
                score -= 1

            if score >= min_score:
                bias = "BULLISH"
                direction = "LONG_ONLY"
                reason = "tendência de alta"
            elif score <= -min_score:
                bias = "BEARISH"
                direction = "SHORT_ONLY"
                reason = "tendência de baixa"
            else:
                bias = "NEUTRAL"
                direction = "BOTH"
                reason = "sem viés forte"

            payload.update({
                "bias": bias,
                "direction": direction,
                "score": int(score),
                "rsi": rsi,
                "momentum_pct": float(momentum_pct),
                "ema_fast": ema_fast,
                "ema_slow": ema_slow,
                "reason": reason,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "cached_at_monotonic": now_monotonic,
            })

        except Exception as e:
            payload["reason"] = "erro ao calcular viés"
            payload["error"] = str(e)
            logger.warning(f"⚠️ Falha ao calcular sentimento de {normalized_symbol}: {e}")

        self.sentiment_cache[normalized_symbol] = dict(payload)
        return dict(payload)

    def _apply_sentiment_direction_filter(
        self,
        symbol: str,
        should_open_long: bool,
        should_open_short: bool,
    ) -> Tuple[bool, bool]:
        """Aplica filtro direcional por sentimento quando o modo está ativo."""
        if not getattr(self, "sentiment_mode_enabled", False):
            return (should_open_long, should_open_short)

        if not hasattr(self, "sentiment_cache") or not isinstance(self.sentiment_cache, dict):
            self.sentiment_cache = {}

        sentiment = self._get_symbol_sentiment(symbol)
        direction = str(sentiment.get("direction", "BOTH")).upper()

        filtered_long = bool(should_open_long)
        filtered_short = bool(should_open_short)

        if direction == "LONG_ONLY":
            filtered_short = False
        elif direction == "SHORT_ONLY":
            filtered_long = False

        if filtered_long != should_open_long or filtered_short != should_open_short:
            logger.info(
                f"🧭 Filtro de sentimento em {symbol}: bias={sentiment.get('bias')} "
                f"score={sentiment.get('score')} direction={direction} "
                f"=> LONG={filtered_long} SHORT={filtered_short}"
            )

        return (filtered_long, filtered_short)

    def analyze_and_trade(self, symbol: str, strategy_name: str | None = None) -> bool:
        """
        Analisa um par e executa trades se houver oportunidade.

        O comportamento de entrada depende do perfil da estratégia:
        - strong_only: entra só com STRONG_BUY/STRONG_SELL
        - standard: entra com BUY/SELL e sinais fortes
        """
        # ============================================
        # VERIFICA META DIÁRIA
        # ============================================
        if self.check_daily_targets():
            logger.info("⏸️  Meta diária atingida - não abrindo novas posições")
            return False

        strategy_context = self._resolve_strategy_context(symbol=symbol, strategy_name=strategy_name)
        strategy_engine = strategy_context.get("strategy", getattr(self, "strategy", None) or HedgeStrategy())
        strategy_label = str(strategy_context.get("name", "primary"))
        entry_mode = self._normalize_strategy_entry_mode(strategy_context.get("entry_mode", "strong_only"))

        logger.info(f"🔍 [{strategy_label}] Analisando {symbol}...")
        
        # Obtém candles
        klines = self.exchange.get_klines(
            symbol=symbol,
            interval=config.TIMEFRAME,
            limit=config.CANDLES_LOOKBACK
        )
        
        if not klines:
            logger.warning(f"⚠️  Sem dados para {symbol}")
            return False
        
        # Verifica saldo DISPONÍVEL para novos trades
        available_balance = self.exchange.get_available_balance()
        logger.info(f"💰 Saldo disponível: ${available_balance:.2f}")
        
        # Busca informações do símbolo (incluindo mínimo notional)
        symbol_info = self.exchange.get_symbol_info(symbol)
        min_notional = symbol_info.get('minNotional', 5.0)
        
        # Gera setup de trade
        setup = strategy_engine.generate_trade_setup(
            symbol=symbol,
            klines=klines,
            available_capital=available_balance,
            min_notional=min_notional
        )
        
        if not setup:
            logger.info(f"⏸️  Sem setup válido para {symbol}")
            return False
        
        # ============================================
        # VERIFICA O SINAL
        # ============================================
        signal = setup.signal
        signal_name = signal.name if hasattr(signal, 'name') else str(signal)
        
        # Define se deve abrir LONG ou SHORT conforme o perfil.
        if entry_mode == "standard":
            should_open_long = signal_name in {'STRONG_BUY', 'BUY'}
            should_open_short = signal_name in {'STRONG_SELL', 'SELL'}
        else:
            should_open_long = signal_name == 'STRONG_BUY'
            should_open_short = signal_name == 'STRONG_SELL'

        # Aplica filtro direcional de sentimento (quando ativo).
        should_open_long, should_open_short = self._apply_sentiment_direction_filter(
            symbol=symbol,
            should_open_long=should_open_long,
            should_open_short=should_open_short,
        )

        # Se sinal é NEUTRAL ou foi filtrado pelo sentimento, não abre posição.
        if not should_open_long and not should_open_short:
            if getattr(self, "sentiment_mode_enabled", False) and signal_name in ['STRONG_BUY', 'STRONG_SELL']:
                logger.info(f"⏸️  Entrada bloqueada por sentimento em {symbol} (sinal={signal_name})")
            elif signal_name in ['BUY', 'SELL'] and entry_mode == "strong_only":
                logger.info(
                    f"⏸️  Sinal {signal_name} em {symbol} é fraco para entrada - "
                    "aguardando STRONG_BUY/STRONG_SELL"
                )
            else:
                logger.info(f"⏸️  Sinal {signal_name} em {symbol} - aguardando sinal de entrada")
            return False
        
        # Verifica posições abertas neste símbolo
        open_positions = self.exchange.get_open_positions()
        
        # Verifica qual lado já está aberto
        has_long = False
        has_short = False
        
        for pos in open_positions:
            if pos['symbol'] == symbol:
                if pos['side'] == 'LONG':
                    has_long = True
                elif pos['side'] == 'SHORT':
                    has_short = True
        
        # ============================================
        # DECIDE O QUE FAZER BASEADO NO SINAL
        # ============================================
        
        # Se sinal forte de compra, mas já tem LONG, não faz nada.
        if should_open_long and has_long:
            logger.info(f"⏸️  Sinal {signal_name} em {symbol} mas LONG já está aberto")
            return False
        
        # Se sinal forte de venda, mas já tem SHORT, não faz nada.
        if should_open_short and has_short:
            logger.info(f"⏸️  Sinal {signal_name} em {symbol} mas SHORT já está aberto")
            return False
        
        # Verifica limite de posições
        total_positions = len(open_positions)
        if total_positions >= config.MAX_OPEN_POSITIONS:
            logger.info("⏸️  Limite de posições atingido")
            return False
        
        # Verifica gestão de risco
        if not self.risk_manager.can_open_position(total_positions):
            logger.info("⏸️  Limite de risco atingido")
            return False
        
        # Executa o trade baseado no sinal
        return self.execute_signal_trade(
            setup=setup,
            open_long=should_open_long,
            open_short=should_open_short
        )
    
    def execute_signal_trade(self, setup, open_long: bool = False, open_short: bool = False) -> bool:
        """
        Executa um trade baseado no sinal (direcional).
        
        ESTRATÉGIA DIRECIONAL:
        - open_long=True → Abre apenas LONG
        - open_short=True → Abre apenas SHORT
        - Nunca abre ambos ao mesmo tempo (diferente do hedge)
        
        1. Abre posição na direção do sinal
        2. Configura SL/TP
        3. Registra no histórico
        """
        symbol = setup.symbol
        signal_name = setup.signal.name if hasattr(setup.signal, 'name') else str(setup.signal)
        
        # Log do funding rate (apenas informativo)
        if config.CHECK_FUNDING_RATE:
            funding_info = self.exchange.get_funding_rate(symbol)
            funding_rate = funding_info['rate_percent']
            if funding_rate > 0:
                logger.info(f"📊 Funding {symbol}: {funding_rate:+.4f}% (LONGs pagam)")
            elif funding_rate < 0:
                logger.info(f"📊 Funding {symbol}: {funding_rate:+.4f}% (SHORTs pagam)")
            else:
                logger.info(f"📊 Funding {symbol}: neutro")
        
        # Log da ação
        if open_long:
            logger.info(f"🚀 Sinal {signal_name} → Abrindo LONG em {symbol}")
        elif open_short:
            logger.info(f"🚀 Sinal {signal_name} → Abrindo SHORT em {symbol}")
        else:
            logger.info(f"⏸️  Nada a fazer em {symbol}")
            return False
        
        try:
            # Calcula quantidades
            price = self.exchange.get_symbol_price(symbol)
            info = self.exchange.get_symbol_info(symbol)

            # Tamanho mínimo (minNotional) vindo da Binance
            min_notional = float(info.get('minNotional', 5.0))

            # Alavancagem usada no cálculo de qty (order_size aqui é MARGEM em USDT)
            try:
                leverage = float(config.LEVERAGE)
            except Exception:
                leverage = 1.0

            # Garante que o notional efetivo (margem * alavancagem) respeite o mínimo
            # Buffer de 5% para evitar cair abaixo do mínimo por arredondamento/variação
            min_margin_needed = (min_notional / max(leverage, 1e-9)) * 1.05
            
            # ============================================
            # DETERMINA O TAMANHO DA ORDEM
            # ============================================
            # Se usando estratégia Binance, usa o order_size da faixa
            if config.USE_BINANCE_STRATEGY and hasattr(self, 'binance_strategy') and self.binance_strategy:
                order_size = self.binance_strategy['order_size']
                logger.info(f"💵 Usando Order Size da Estratégia Binance: ${order_size}")
            else:
                # Usa o tamanho do setup (cálculo antigo)
                order_size = setup.long_size if open_long else setup.short_size

            base_order_size = float(order_size)
            trade_side = "LONG" if open_long else "SHORT"

            # Ajuste automático do order_size (margem) para cumprir minNotional
            if order_size < min_margin_needed:
                logger.info(
                    f"🔧 Ajustando order_size para respeitar minNotional em {symbol}: "
                    f"${order_size:.2f} → ${min_margin_needed:.2f} "
                    f"(minNotional ${min_notional:.2f}, {leverage:g}x)"
                )
                order_size = min_margin_needed

            order_size, double_first_applied, double_first_state_key = self._apply_double_first_order_size(
                symbol=symbol,
                side=trade_side,
                order_size=order_size,
            )
            
            # ============================================
            # ABRE LONG (quando sinal de entrada direciona para compra)
            # ============================================
            if open_long:
                # Verifica se atende ao mínimo (minNotional é NOTIONAL; order_size é MARGEM)
                effective_notional = order_size * leverage
                if effective_notional < min_notional:
                    logger.warning(f"⚠️  Posição LONG muito pequena para {symbol}")
                    logger.warning(
                        f"   Mínimo: ${min_notional:.2f}, Notional: ${effective_notional:.2f} "
                        f"(Order Size: ${order_size:.2f} x {leverage:g}x)"
                    )
                    return False

                long_qty = (order_size * config.LEVERAGE) / price

                logger.info(f"📈 Abrindo LONG: {long_qty:.4f} {symbol} @ ${price:.4f}")
                long_order = self.exchange.place_market_order(
                    symbol=symbol,
                    side='BUY',
                    position_side='LONG',
                    quantity=long_qty
                )

                if not long_order:
                    logger.error("❌ Falha ao abrir posição LONG")
                    return False

                if double_first_applied:
                    self._mark_double_first_used(
                        state_key=double_first_state_key,
                        symbol=symbol,
                        side="LONG",
                        base_order_size=base_order_size,
                        applied_order_size=order_size,
                    )

                # Define apenas TP para o LONG (SL é gerenciado pelo Trailing Stop do bot)
                self.exchange.set_stop_loss_take_profit(
                    symbol=symbol,
                    position_side='LONG',
                    stop_loss_price=None,  # Sem SL na Binance
                    take_profit_price=setup.take_profit
                )

                # Notifica no Telegram
                self.telegram.send_trade_alert(
                    symbol=symbol,
                    action="OPEN_LONG",
                    price=price,
                    quantity=long_qty
                )

                # Registra o trade
                trade_record = {
                    'timestamp': datetime.now().isoformat(),
                    'symbol': symbol,
                    'signal': signal_name,
                    'side': 'LONG',
                    'qty': long_qty,
                    'value': order_size,
                    'entry_price': price,
                    'stop_loss': None,  # Gerenciado pelo bot
                    'take_profit': setup.take_profit,
                    'double_first': bool(double_first_applied),
                }
                self.trade_history.append(trade_record)
                
                # Adiciona ao rastreamento de posições conhecidas
                position_key = f"{symbol}_LONG"
                self.known_positions[position_key] = {
                    'symbol': symbol,
                    'side': 'LONG',
                    'entry_price': price,
                    'quantity': long_qty,
                    'last_seen': datetime.now()
                }
                
                logger.info("✅ LONG aberto com sucesso!")
                logger.info(f"   {long_qty:.4f} {symbol} @ ${price:.4f}")
                if double_first_applied:
                    logger.info(
                        f"   Order Size: ${order_size} (double first aplicado sobre ${base_order_size:.2f}) | "
                        f"TP: ${setup.take_profit:.4f}"
                    )
                else:
                    logger.info(f"   Order Size: ${order_size} | TP: ${setup.take_profit:.4f}")
                
                return True
            
            # ============================================
            # ABRE SHORT (quando sinal de entrada direciona para venda)
            # ============================================
            if open_short:
                # Verifica se atende ao mínimo (minNotional é NOTIONAL; order_size é MARGEM)
                effective_notional = order_size * leverage
                if effective_notional < min_notional:
                    logger.warning(f"⚠️  Posição SHORT muito pequena para {symbol}")
                    logger.warning(
                        f"   Mínimo: ${min_notional:.2f}, Notional: ${effective_notional:.2f} "
                        f"(Order Size: ${order_size:.2f} x {leverage:g}x)"
                    )
                    return False

                short_qty = (order_size * config.LEVERAGE) / price

                logger.info(f"📉 Abrindo SHORT: {short_qty:.4f} {symbol} @ ${price:.4f}")
                short_order = self.exchange.place_market_order(
                    symbol=symbol,
                    side='SELL',
                    position_side='SHORT',
                    quantity=short_qty
                )

                if not short_order:
                    logger.error("❌ Falha ao abrir posição SHORT")
                    return False

                if double_first_applied:
                    self._mark_double_first_used(
                        state_key=double_first_state_key,
                        symbol=symbol,
                        side="SHORT",
                        base_order_size=base_order_size,
                        applied_order_size=order_size,
                    )

                # Define apenas TP para o SHORT (SL é gerenciado pelo Trailing Stop do bot)
                # Para SHORT: TP é abaixo do preço
                short_tp = price * (1 - config.TAKE_PROFIT_PERCENT / 100)
                self.exchange.set_stop_loss_take_profit(
                    symbol=symbol,
                    position_side='SHORT',
                    stop_loss_price=None,  # Sem SL na Binance
                    take_profit_price=short_tp
                )

                # Notifica no Telegram
                self.telegram.send_trade_alert(
                    symbol=symbol,
                    action="OPEN_SHORT",
                    price=price,
                    quantity=short_qty
                )

                # Registra o trade
                trade_record = {
                    'timestamp': datetime.now().isoformat(),
                    'symbol': symbol,
                    'signal': signal_name,
                    'side': 'SHORT',
                    'qty': short_qty,
                    'value': order_size,
                    'entry_price': price,
                    'stop_loss': None,  # Gerenciado pelo bot
                    'take_profit': short_tp,
                    'double_first': bool(double_first_applied),
                }
                self.trade_history.append(trade_record)
                
                # Adiciona ao rastreamento de posições conhecidas
                position_key = f"{symbol}_SHORT"
                self.known_positions[position_key] = {
                    'symbol': symbol,
                    'side': 'SHORT',
                    'entry_price': price,
                    'quantity': short_qty,
                    'last_seen': datetime.now()
                }
                
                logger.info("✅ SHORT aberto com sucesso!")
                logger.info(f"   {short_qty:.4f} {symbol} @ ${price:.4f}")
                if double_first_applied:
                    logger.info(
                        f"   Order Size: ${order_size} (double first aplicado sobre ${base_order_size:.2f}) | "
                        f"TP: ${short_tp:.4f}"
                    )
                else:
                    logger.info(f"   Order Size: ${order_size} | TP: ${short_tp:.4f}")
                
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"❌ Erro ao executar trade: {e}")
            return False
    
    # Mantém execute_hedge_trade como alias para compatibilidade
    def execute_hedge_trade(self, setup, has_long: bool = False, has_short: bool = False) -> bool:
        """
        DEPRECATED: Use execute_signal_trade instead.
        Mantido apenas para compatibilidade.
        """
        return self.execute_signal_trade(
            setup=setup,
            open_long=not has_long,
            open_short=not has_short
        )
    
    def monitor_positions(self):
        """
        Monitora posições abertas e gerencia o risco.
        
        Verifica:
        - Posições fechadas pela Binance (via SL/TP) que o bot não detectou
        - Take Profit (SEMPRE ativo)
        - Trailing Stop (se ativado)
        - Stop Loss individual (se ativado)
        - Atualiza o P&L diário e por símbolo
        """
        positions = self.exchange.get_open_positions()
        
        # ============================================
        # DETECTA POSIÇÕES FECHADAS PELA BINANCE
        # ============================================
        # Cria set de posições atuais
        current_position_keys = set()
        for pos in positions:
            position_key = f"{pos['symbol']}_{pos['side']}"
            current_position_keys.add(position_key)
            
            # Atualiza known_positions com info atual
            self.known_positions[position_key] = {
                'symbol': pos['symbol'],
                'side': pos['side'],
                'entry_price': pos['entry_price'],
                'quantity': pos['quantity'],
                'last_seen': datetime.now()
            }
        
        # Verifica se alguma posição conhecida sumiu
        closed_by_binance = []
        for position_key in list(self.known_positions.keys()):
            if position_key not in current_position_keys:
                # Posição sumiu! Foi fechada pela Binance
                closed_by_binance.append(position_key)
        
        # Processa posições fechadas pela Binance
        for position_key in closed_by_binance:
            pos_info = self.known_positions[position_key]
            logger.warning(f"⚠️ Posição {position_key} foi FECHADA pela Binance (SL/TP)")
            
            # Busca o P&L real da Binance
            self._process_binance_closed_position(pos_info)
            
            # Remove do tracking
            del self.known_positions[position_key]
            self._clear_trailing_data(position_key)
        
        if not positions:
            return
        
        logger.info(f"📊 Monitorando {len(positions)} posições...")
        
        total_pnl = 0
        pnl_by_symbol_current = {}  # P&L não realizado atual por símbolo
        
        for pos in positions:
            pnl = pos['unrealized_pnl']
            symbol = pos['symbol']
            side = pos['side']
            entry_price = pos['entry_price']
            quantity = pos['quantity']
            total_pnl += pnl
            
            # Acumula P&L por símbolo
            if symbol not in pnl_by_symbol_current:
                pnl_by_symbol_current[symbol] = 0
            pnl_by_symbol_current[symbol] += pnl
            
            # Prioriza mark_price já retornado em get_open_positions para reduzir chamadas na API.
            try:
                current_price = float(pos.get('mark_price', 0) or 0)
            except (TypeError, ValueError):
                current_price = 0.0

            if current_price <= 0:
                try:
                    current_price = self.exchange.get_current_price(symbol)
                except Exception as e:
                    logger.warning(f"⚠️ Falha ao obter preço atual de {symbol}: {e}")
                    continue

            if not current_price or current_price <= 0:
                logger.warning(f"⚠️ Preço atual inválido para {symbol}. Pulando monitoramento deste ciclo.")
                continue
            
            # Chave única para esta posição
            position_key = f"{symbol}_{side}"
            
            # Calcula o lucro percentual
            if side == "LONG":
                profit_pct = ((current_price - entry_price) / entry_price) * 100
            else:  # SHORT
                profit_pct = ((entry_price - current_price) / entry_price) * 100
            
            logger.info(f"   {side} {symbol}: P&L ${pnl:.2f} ({profit_pct:+.2f}%) | Preço: ${current_price:.4f}")
            
            # ============================================
            # 1. VERIFICA TAKE PROFIT (SEMPRE ATIVO)
            # ============================================
            if profit_pct >= config.TAKE_PROFIT_PERCENT:
                logger.info(f"🎯 Take Profit atingido! {profit_pct:.2f}% >= {config.TAKE_PROFIT_PERCENT}%")
                pos['current_price'] = current_price
                closed = self._close_position_with_notification(
                    pos,
                    f"Take Profit ({config.TAKE_PROFIT_PERCENT}%)"
                )
                if closed:
                    self._clear_trailing_data(position_key)
                    # Remove do known_positions
                    if position_key in self.known_positions:
                        del self.known_positions[position_key]
                else:
                    logger.warning(
                        f"⚠️ Fechamento não confirmado para {position_key} em Take Profit. "
                        "Mantendo rastreamento da posição."
                    )
                continue
            
            # ============================================
            # 2. VERIFICA TRAILING STOP (se ativado)
            # ============================================
            if config.USE_TRAILING_STOP:
                should_close, reason = self._check_trailing_stop(
                    position_key=position_key,
                    side=side,
                    entry_price=entry_price,
                    current_price=current_price,
                    symbol=symbol,
                    position_amt=quantity  # Passa a quantidade para calcular lucro em USD
                )
                
                if should_close:
                    pos['current_price'] = current_price
                    closed = self._close_position_with_notification(pos, reason)
                    if closed:
                        self._clear_trailing_data(position_key)
                        # Remove do known_positions
                        if position_key in self.known_positions:
                            del self.known_positions[position_key]
                    else:
                        logger.warning(
                            f"⚠️ Fechamento não confirmado para {position_key} via trailing. "
                            "Mantendo rastreamento da posição."
                        )
                    continue
            
            # ============================================
            # 3. VERIFICA STOP LOSS INDIVIDUAL (se ativado)
            # ============================================
            if config.USE_INDIVIDUAL_STOP_LOSS:
                # Verifica se o prejuízo excede o limite
                if profit_pct <= -config.STOP_LOSS_PERCENT:
                    pos['current_price'] = current_price
                    closed = self._close_position_with_notification(
                        pos,
                        f"Stop Loss ({config.STOP_LOSS_PERCENT}%)"
                    )
                    if closed:
                        self._clear_trailing_data(position_key)
                        # Remove do known_positions
                        if position_key in self.known_positions:
                            del self.known_positions[position_key]
                    else:
                        logger.warning(
                            f"⚠️ Fechamento não confirmado para {position_key} via stop loss. "
                            "Mantendo rastreamento da posição."
                        )
                    continue
        
        logger.info(f"💵 P&L Total não realizado: ${total_pnl:.2f}")
    
    def _process_binance_closed_position(self, pos_info: dict):
        """
        Processa uma posição que foi fechada pela Binance (via SL/TP).
        Busca o P&L real na API e atualiza as estatísticas.
        """
        symbol = pos_info['symbol']
        side = pos_info['side']
        entry_price = pos_info['entry_price']
        quantity = pos_info['quantity']
        
        logger.info(f"🔍 Buscando P&L real para {side} {symbol}...")
        
        try:
            # Busca o último REALIZED_PNL deste símbolo na API
            income_list = self.exchange.get_income_history(
                income_type='REALIZED_PNL',
                symbol=symbol,
                limit=10  # Últimos 10 registros
            )
            
            if income_list:
                # Pega o mais recente (último fechamento)
                latest = income_list[-1]
                pnl_gross = float(latest.get('income', 0))
                # Calcula taxas
                taker_fee_rate = self.get_taker_fee_rate()
                notional = entry_price * quantity
                total_fees = notional * taker_fee_rate * 2  # Abertura + Fechamento
                
                # P&L líquido
                pnl_net = pnl_gross - total_fees
                
                logger.info("📊 P&L encontrado na Binance:")
                logger.info(f"   P&L Bruto: ${pnl_gross:.4f}")
                logger.info(f"   Taxas: ${total_fees:.4f}")
                logger.info(f"   P&L Líquido: ${pnl_net:.4f}")
                
                # ============================================
                # ATUALIZA CONTADORES
                # ============================================
                self.closed_trades_count += 1
                self.daily_realized_pnl += pnl_net
                self.total_pnl += pnl_net
                self.risk_manager.update_pnl(pnl_net)
                
                # Atualiza estatísticas de trades
                if pnl_net > 0:
                    self.trades_win_count += 1
                    self.trades_win_total += pnl_net
                    result = "LUCRO 🟢"
                else:
                    self.trades_loss_count += 1
                    self.trades_loss_total += pnl_net
                    result = "PREJUÍZO 🔴"
                
                # Atualiza total de taxas pagas
                self.total_fees_paid += total_fees
                
                # Atualiza trades por símbolo (para relatório detalhado)
                if symbol not in self.trades_by_symbol:
                    self.trades_by_symbol[symbol] = {'wins': 0, 'losses': 0, 'win_value': 0.0, 'loss_value': 0.0, 'fees': 0.0}
                
                if pnl_net > 0:
                    self.trades_by_symbol[symbol]['wins'] += 1
                    self.trades_by_symbol[symbol]['win_value'] += pnl_net
                else:
                    self.trades_by_symbol[symbol]['losses'] += 1
                    self.trades_by_symbol[symbol]['loss_value'] += pnl_net
                
                # Atualiza taxas por símbolo
                self.trades_by_symbol[symbol]['fees'] = self.trades_by_symbol[symbol].get('fees', 0.0) + total_fees
                
                # Atualiza P&L por símbolo
                if symbol in self.pnl_by_symbol:
                    self.pnl_by_symbol[symbol] += pnl_net
                else:
                    self.pnl_by_symbol[symbol] = pnl_net
                
                # Determina o motivo (só temos TP na Binance agora)
                if pnl_gross > 0:
                    reason = "Take Profit (Binance)"
                else:
                    reason = "Fechamento externo"  # Pode ser manual ou liquidação
                
                # Log
                logger.info(f"💰 {result}: ${pnl_net:.4f} | Motivo: {reason}")
                
                # Envia notificação no Telegram
                self.telegram.send_message(
                    f"⚠️ <b>POSIÇÃO FECHADA PELA BINANCE</b>\n\n"
                    f"📍 <b>Par:</b> {symbol.replace('USDT', '')}/USDT\n"
                    f"📊 <b>Lado:</b> {side}\n"
                    f"📝 <b>Motivo:</b> {reason}\n\n"
                    f"<b>💵 RESULTADO:</b>\n"
                    f"   • P&L Bruto: <code>${pnl_gross:+.4f}</code>\n"
                    f"   • Taxas: <code>-${total_fees:.4f}</code>\n"
                    f"   • <b>P&L Líquido: <code>${pnl_net:+.4f}</code></b>"
                )
                
            else:
                logger.warning(f"⚠️ Não encontrou REALIZED_PNL para {symbol} na API")
                
        except Exception as e:
            logger.error(f"❌ Erro ao buscar P&L da Binance: {e}")
    
    def _check_trailing_stop(
        self, 
        position_key: str, 
        side: str, 
        entry_price: float, 
        current_price: float,
        symbol: str,
        position_amt: float = 0.0
    ) -> tuple:
        """
        Verifica e gerencia o Trailing Stop para uma posição.
        
        Args:
            position_key: Identificador único da posição (ex: "ETHUSDT_LONG")
            side: "LONG" ou "SHORT"
            entry_price: Preço de entrada da posição
            current_price: Preço atual do mercado
            symbol: Par de trading
            position_amt: Quantidade da posição (para calcular lucro em USD)
        
        Returns:
            (should_close, reason): Se deve fechar e o motivo
        """
        # Calcula o lucro percentual atual
        if side == "LONG":
            profit_pct = ((current_price - entry_price) / entry_price) * 100
            profit_usd = (current_price - entry_price) * abs(position_amt)
        else:  # SHORT
            profit_pct = ((entry_price - current_price) / entry_price) * 100
            profit_usd = (entry_price - current_price) * abs(position_amt)
        
        # Inicializa o rastreamento se não existir
        if position_key not in self.peak_prices:
            self.peak_prices[position_key] = current_price
            self.trailing_activated[position_key] = False
        
        # Atualiza o preço de pico
        if side == "LONG":
            # Para LONG, queremos o preço máximo
            if current_price > self.peak_prices[position_key]:
                self.peak_prices[position_key] = current_price
        else:  # SHORT
            # Para SHORT, queremos o preço mínimo
            if current_price < self.peak_prices[position_key]:
                self.peak_prices[position_key] = current_price
        
        peak_price = self.peak_prices[position_key]
        
        # Verifica se deve ativar o trailing
        if not self.trailing_activated[position_key]:
            if profit_pct >= config.TRAILING_ACTIVATION_PERCENT:
                self.trailing_activated[position_key] = True
                
                # Calcula o preço do trailing stop
                if side == "LONG":
                    trailing_stop_price = peak_price * (1 - config.TRAILING_DISTANCE_PERCENT / 100)
                else:
                    trailing_stop_price = peak_price * (1 + config.TRAILING_DISTANCE_PERCENT / 100)
                
                logger.info(f"🔔 Trailing Stop ATIVADO para {position_key}!")
                logger.info(f"   Pico: ${peak_price:.4f} | Stop em: ${trailing_stop_price:.4f}")
                
                # Envia notificação
                self.telegram.send_trailing_stop_activated(
                    symbol=symbol,
                    side=side,
                    entry_price=entry_price,
                    current_price=current_price,
                    trailing_stop_price=trailing_stop_price,
                    current_profit_pct=profit_pct
                )
        
        # Se o trailing está ativado, verifica se foi atingido
        if self.trailing_activated[position_key]:
            if side == "LONG":
                trailing_stop_price = peak_price * (1 - config.TRAILING_DISTANCE_PERCENT / 100)
                price_hit = current_price <= trailing_stop_price
            else:  # SHORT
                trailing_stop_price = peak_price * (1 + config.TRAILING_DISTANCE_PERCENT / 100)
                price_hit = current_price >= trailing_stop_price
            
            # Verifica se o preço atingiu o trailing stop
            if price_hit:
                # Determina o lucro mínimo baseado no funding rate
                min_profit = config.TRAILING_MIN_PROFIT_USD
                
                # Se CHECK_FUNDING_RATE está ativo, verifica se funding está contra a posição
                if config.CHECK_FUNDING_RATE:
                    funding_info = self.exchange.get_funding_rate(symbol)
                    funding_rate = funding_info['rate_percent']
                    
                    # Funding POSITIVO = LONGs pagam
                    # Funding NEGATIVO = SHORTs pagam
                    funding_against = False
                    
                    if side == "LONG" and funding_rate > config.FUNDING_RATE_THRESHOLD:
                        funding_against = True
                        logger.info(f"   💸 Funding {funding_rate:+.4f}% está CONTRA LONG")
                    elif side == "SHORT" and funding_rate < -config.FUNDING_RATE_THRESHOLD:
                        funding_against = True
                        logger.info(f"   💸 Funding {funding_rate:+.4f}% está CONTRA SHORT")
                    
                    # Se funding está contra, aumenta o mínimo
                    if funding_against:
                        min_profit = config.TRAILING_MIN_PROFIT_HIGH_FUNDING
                        logger.info(f"   📈 Mínimo aumentado para ${min_profit:.2f} (funding alto)")
                
                if profit_usd >= min_profit:
                    return (True, f"Trailing Stop ({config.TRAILING_DISTANCE_PERCENT}% do pico)")
                else:
                    # Lucro muito baixo, não fecha ainda
                    logger.info(f"   ⚠️ Trailing atingido mas lucro ${profit_usd:.4f} < mínimo ${min_profit:.2f}")
                    logger.info(f"   → Mantendo posição aberta até lucro >= ${min_profit:.2f}")
            
            # Log do status do trailing
            logger.info(f"   🎯 Trailing ativo | Pico: ${peak_price:.4f} | Stop: ${trailing_stop_price:.4f} | Lucro: ${profit_usd:.4f}")
        
        return (False, "")
    
    def _clear_trailing_data(self, position_key: str):
        """
        Limpa os dados de trailing para uma posição fechada.
        """
        if position_key in self.peak_prices:
            del self.peak_prices[position_key]
        if position_key in self.trailing_activated:
            del self.trailing_activated[position_key]
    
    def _close_position_with_notification(self, pos: dict, reason: str) -> bool:
        """
        Fecha uma posição e envia notificação com P&L líquido.
        Também atualiza o contador de trades fechados e o P&L diário.

        IMPORTANTE: O P&L é calculado com base nos preços REAIS de entrada e saída,
        não no unrealized_pnl que pode estar desatualizado.

        Returns:
            True se o fechamento foi confirmado; False caso contrário.
        """
        symbol = pos['symbol']
        side = pos['side']
        entry_price = pos['entry_price']
        quantity = pos['quantity']
        logger.info(f"🚨 Fechando posição: {reason}")
        
        # Pega o preço atual ANTES de fechar (será o preço de saída aproximado)
        current_price = self.exchange.get_current_price(symbol)
        
        # Fecha a posição e só contabiliza se o fechamento for confirmado
        try:
            close_success = self.exchange.close_position(symbol, side)
        except Exception as e:
            logger.error(f"❌ Exceção ao fechar posição {side} {symbol}: {e}")
            return False

        if not close_success:
            logger.error(
                f"❌ Falha ao fechar posição {side} {symbol}. "
                "Nenhuma estatística/P&L será contabilizada."
            )
            return False
        
        # ============================================
        # CALCULA P&L REAL BASEADO NOS PREÇOS
        # ============================================
        # Valor nocional da posição
        notional_value = entry_price * quantity
        
        # Variação percentual do preço
        if side == 'LONG':
            # LONG: lucro quando preço sobe
            price_change_pct = (current_price - entry_price) / entry_price
        else:
            # SHORT: lucro quando preço desce
            price_change_pct = (entry_price - current_price) / entry_price
        
        # P&L bruto = variação × valor nocional
        # (a quantidade já está alavancada, então não multiplicamos por leverage novamente)
        pnl_gross = price_change_pct * notional_value
        
        # Calcula as taxas
        taker_fee_rate = self.get_taker_fee_rate()
        fee_open = entry_price * quantity * taker_fee_rate   # Taxa de abertura
        fee_close = current_price * quantity * taker_fee_rate  # Taxa de fechamento
        total_fees = fee_open + fee_close
        
        # P&L líquido (descontando taxas)
        pnl_net = pnl_gross - total_fees
        
        logger.info("📊 Cálculo P&L:")
        logger.info(f"   Entrada: ${entry_price:.4f} | Saída: ${current_price:.4f}")
        logger.info(f"   Quantidade: {quantity:.6f} | Nocional: ${notional_value:.2f}")
        logger.info(f"   Variação: {price_change_pct*100:.2f}% | P&L Bruto: ${pnl_gross:.4f}")
        
        # ============================================
        # ATUALIZA CONTADORES
        # ============================================
        # Incrementa contador de trades FECHADOS
        self.closed_trades_count += 1
        
        # Soma ao P&L diário realizado (acumula cada fechamento)
        self.daily_realized_pnl += pnl_net
        
        # Registra o P&L total acumulado (desde o início do bot)
        self.total_pnl += pnl_net
        self.risk_manager.update_pnl(pnl_net)
        
        # Atualiza estatísticas de trades (lucro vs prejuízo)
        if pnl_net > 0:
            self.trades_win_count += 1
            self.trades_win_total += pnl_net
        else:
            self.trades_loss_count += 1
            self.trades_loss_total += pnl_net  # Será negativo
        
        # Atualiza total de taxas pagas
        self.total_fees_paid += total_fees
        
        # Atualiza trades por símbolo (para relatório detalhado)
        if symbol not in self.trades_by_symbol:
            self.trades_by_symbol[symbol] = {'wins': 0, 'losses': 0, 'win_value': 0.0, 'loss_value': 0.0, 'fees': 0.0}
        
        if pnl_net > 0:
            self.trades_by_symbol[symbol]['wins'] += 1
            self.trades_by_symbol[symbol]['win_value'] += pnl_net
        else:
            self.trades_by_symbol[symbol]['losses'] += 1
            self.trades_by_symbol[symbol]['loss_value'] += pnl_net
        
        # Atualiza taxas por símbolo
        self.trades_by_symbol[symbol]['fees'] = self.trades_by_symbol[symbol].get('fees', 0.0) + total_fees
        
        # Atualiza P&L por símbolo
        if symbol in self.pnl_by_symbol:
            self.pnl_by_symbol[symbol] += pnl_net
        else:
            self.pnl_by_symbol[symbol] = pnl_net
        
        # Log com estatísticas
        win_rate = (self.trades_win_count / self.closed_trades_count * 100) if self.closed_trades_count > 0 else 0
        logger.info(f"💰 P&L Bruto: ${pnl_gross:.4f} | Taxas: ${total_fees:.4f} | P&L Líquido: ${pnl_net:.4f}")
        logger.info(f"📊 Trade #{self.closed_trades_count} | Win Rate: {win_rate:.1f}% | P&L Diário: ${self.daily_realized_pnl:.2f}")
        
        # Envia notificação no Telegram
        telegram_sent = self.telegram.send_position_closed(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            exit_price=current_price,
            quantity=quantity,
            pnl_gross=pnl_gross,
            fees=total_fees,
            pnl_net=pnl_net,
            reason=reason
        )
        
        if telegram_sent:
            logger.info(f"✅ Notificação Telegram enviada para trade #{self.closed_trades_count}")
        else:
            logger.error(f"❌ FALHA ao enviar notificação Telegram para trade #{self.closed_trades_count}")

        return True
    
    def check_global_stop_loss(self) -> bool:
        """
        Verifica se o Stop Loss Global foi atingido.
        
        Usa dados REAIS da Binance para calcular o P&L total.
        
        Returns:
            True se o stop loss global foi atingido, False caso contrário
        """
        # Busca informações REAIS da conta Binance
        account_info = self.exchange.get_account_info()
        total_unrealized = account_info['unrealized_pnl']
        
        # Busca P&L diário REAL da Binance
        daily_pnl = self.exchange.get_daily_pnl_from_binance()
        total_pnl = daily_pnl['total'] + total_unrealized
        
        # Proteção contra capital inicial inválido (evita divisão por zero)
        try:
            initial_capital = float(self.initial_capital or 0.0)
        except (TypeError, ValueError):
            initial_capital = 0.0

        if initial_capital <= 0:
            logger.warning(
                "⚠️ Stop Loss Global desativado neste ciclo: "
                f"initial_capital inválido ({self.initial_capital})."
            )
            return False

        # Calcula a perda percentual
        loss_percent = abs(total_pnl / initial_capital * 100) if total_pnl < 0 else 0
        
        # Verifica se atingiu o limite
        if loss_percent >= config.GLOBAL_STOP_LOSS_PERCENT:
            logger.warning(f"🚨 STOP LOSS GLOBAL ATINGIDO! Perda: {loss_percent:.1f}%")
            return True
        
        return False
    
    def execute_global_stop_loss(self):
        """
        Executa o Stop Loss Global: fecha todas as posições e para o bot.
        Usa dados REAIS da Binance.
        """
        logger.warning("=" * 60)
        logger.warning("🚨🚨🚨 EXECUTANDO STOP LOSS GLOBAL 🚨🚨🚨")
        logger.warning("=" * 60)
        
        # Pega todas as posições abertas
        positions = self.exchange.get_open_positions()
        
        # Busca dados REAIS da Binance
        account_info = self.exchange.get_account_info()
        current_balance = account_info['wallet_balance']
        total_unrealized = account_info['unrealized_pnl']
        
        daily_pnl = self.exchange.get_daily_pnl_from_binance()
        total_pnl = daily_pnl['total'] + total_unrealized

        # Proteção contra capital inicial inválido (evita divisão por zero)
        try:
            initial_capital = float(self.initial_capital or 0.0)
        except (TypeError, ValueError):
            initial_capital = 0.0

        if initial_capital <= 0:
            logger.warning(
                "⚠️ initial_capital inválido durante Stop Loss Global. "
                "Usando saldo atual como base de referência para cálculo de perda."
            )
            initial_capital = max(1.0, float(current_balance))

        loss_percent = abs(total_pnl / initial_capital * 100) if total_pnl < 0 else 0

        # Fecha todas as posições
        for pos in positions:
            logger.warning(f"Fechando {pos['side']} {pos['symbol']}...")
            closed = self._close_position_with_notification(pos, "Stop Loss Global")
            if not closed:
                logger.error(
                    f"❌ Não foi possível confirmar fechamento de {pos['side']} {pos['symbol']} "
                    "durante Stop Loss Global."
                )
        
        # Envia notificação no Telegram
        self.telegram.send_global_stop_loss_alert(
            initial_capital=initial_capital,
            current_balance=current_balance,
            total_pnl=total_pnl,
            loss_percent=loss_percent
        )
        
        # Salva estado antes de parar
        logger.info("💾 Salvando estado...")
        self.save_state()
        
        # Para o bot
        logger.warning("🛑 Bot encerrado pelo Stop Loss Global")
        self.running = False
    
    def print_status(self, send_telegram: bool = False):
        """
        Imprime o status atual do bot.
        Usa dados REAIS da Binance para P&L diário e saldo.
        
        Args:
            send_telegram: Se True, envia também para o Telegram
        """
        positions = self.exchange.get_open_positions()
        
        # Busca informações REAIS da conta Binance
        account_info = self.exchange.get_account_info()
        balance = account_info['wallet_balance']  # Saldo total da carteira
        available = account_info['available_balance']  # Saldo disponível
        total_unrealized = account_info['unrealized_pnl']  # P&L não realizado da Binance
        
        # Busca P&L diário REAL da Binance (inclui funding fees e comissões)
        daily_pnl_binance = self.exchange.get_daily_pnl_from_binance()
        daily_pnl_real = daily_pnl_binance['total']  # Este é o valor que a Binance mostra!
        
        # Debug: mostra quantos registros de income foram encontrados
        logger.info(f"📋 Income history: {daily_pnl_binance['income_count']} registros encontrados hoje")
        
        # Calcula P&L não realizado por símbolo (para detalhamento)
        unrealized_by_symbol = {}
        for pos in positions:
            symbol = pos['symbol']
            pnl = pos['unrealized_pnl']
            if symbol not in unrealized_by_symbol:
                unrealized_by_symbol[symbol] = 0
            unrealized_by_symbol[symbol] += pnl
        
        logger.info("=" * 60)
        logger.info("📊 STATUS DO BOT")
        logger.info("=" * 60)
        logger.info(f"💰 Saldo da Carteira: ${balance:.2f} USDT")
        logger.info(f"💵 Saldo Disponível: ${available:.2f} USDT")
        logger.info(f"📈 Posições abertas: {len(positions)}")
        logger.info(f"📝 Trades fechados: {self.closed_trades_count}")
        logger.info("-" * 60)
        
        # P&L Geral (dados REAIS da Binance)
        logger.info("💵 P&L GERAL (dados da Binance):")
        logger.info(f"   • P&L Diário: ${daily_pnl_real:.2f}")
        logger.info(f"      └─ Trades: ${daily_pnl_binance['realized_pnl']:.2f} | Funding: ${daily_pnl_binance['funding_fee']:.2f} | Comissões: ${daily_pnl_binance['commission']:.2f}")
        logger.info(f"   • P&L Não Realizado: ${total_unrealized:.2f}")
        logger.info(f"   • P&L Total Hoje: ${daily_pnl_real + total_unrealized:.2f}")
        logger.info("-" * 60)
        
        # P&L por Par de Moeda (com Funding Rate)
        logger.info("📈 P&L POR PAR DE MOEDA:")
        for symbol in config.TRADING_PAIRS:
            realized = self.pnl_by_symbol.get(symbol, 0)
            unrealized = unrealized_by_symbol.get(symbol, 0)
            total = realized + unrealized
            
            # Busca funding rate atual
            funding_info = self.exchange.get_funding_rate(symbol)
            funding_rate = funding_info['rate_percent']
            funding_side = "L paga" if funding_rate > 0 else "S paga" if funding_rate < 0 else "neutro"
            
            # Emoji baseado no resultado
            if total > 0:
                emoji = "🟢"
            elif total < 0:
                emoji = "🔴"
            else:
                emoji = "⚪"
            
            logger.info(f"   {emoji} {symbol}:")
            logger.info(f"      P&L: ${total:.2f} (Real: ${realized:.2f} | Aberto: ${unrealized:.2f})")
            logger.info(f"      Funding: {funding_rate:+.4f}% ({funding_side})")
        
        logger.info("=" * 60)
        
        # Envia para o Telegram se solicitado (usa dados REAIS da Binance)
        if send_telegram:
            self.telegram.send_status(
                balance=balance,
                open_positions=len(positions),
                total_trades=self.closed_trades_count,
                daily_pnl=daily_pnl_real,  # P&L diário REAL da Binance
                funding_fee=daily_pnl_binance['funding_fee'],  # Funding fee do dia
                total_pnl_realized=self.total_pnl,
                total_pnl_unrealized=total_unrealized,
                pnl_by_symbol=self.pnl_by_symbol,
                unrealized_by_symbol=unrealized_by_symbol,
                daily_profit_target=config.DAILY_PROFIT_TARGET if config.USE_DAILY_TARGETS else None,
                daily_loss_limit=config.DAILY_LOSS_LIMIT if config.USE_DAILY_TARGETS else None,
                daily_target_reached=self.daily_target_reached
            )

    def _format_usd_brl(self, value: float, decimals: int = 2, show_sign: bool = False) -> str:
        """
        Formata valores em USD + BRL reaproveitando o formatador do notifier.
        """
        try:
            if hasattr(self, 'telegram') and hasattr(self.telegram, '_format_usd_brl'):
                return self.telegram._format_usd_brl(value, decimals, show_sign)
        except Exception:
            pass

        sign = ""
        if show_sign and value > 0:
            sign = "+"
        return f"{sign}${value:.{decimals}f}"

    def _build_daily_performance_snapshot(self, lookback_hours: int) -> Dict[str, Any]:
        """
        Consolida métricas de performance em uma janela móvel (últimas N horas).
        """
        lookback_hours = max(1, int(lookback_hours))
        now_utc = datetime.now(timezone.utc)
        window_start_utc = now_utc - timedelta(hours=lookback_hours)
        window_start_ms = int(window_start_utc.timestamp() * 1000)

        income_list = self.exchange.get_income_history(limit=1000, start_time=window_start_ms)

        realized_total = 0.0
        commission_total = 0.0
        funding_total = 0.0
        realized_events = []
        symbol_pnl = defaultdict(float)

        for item in income_list:
            try:
                ts = int(item.get('time', 0) or 0)
                if ts < window_start_ms:
                    continue
                income_type = str(item.get('incomeType', '') or '')
                amount = float(item.get('income', 0) or 0)
                symbol = str(item.get('symbol', 'N/A') or 'N/A')
            except Exception:
                continue

            if income_type == 'REALIZED_PNL':
                realized_total += amount
                realized_events.append({'ts': ts, 'symbol': symbol, 'pnl': amount})
                symbol_pnl[symbol] += amount
            elif income_type == 'COMMISSION':
                commission_total += amount
            elif income_type == 'FUNDING_FEE':
                funding_total += amount

        realized_events.sort(key=lambda e: e['ts'])
        wins = [e['pnl'] for e in realized_events if e['pnl'] > 0]
        losses = [e['pnl'] for e in realized_events if e['pnl'] < 0]
        trade_count = len(wins) + len(losses)
        win_rate = (len(wins) / trade_count * 100) if trade_count else 0.0
        avg_win = (sum(wins) / len(wins)) if wins else 0.0
        avg_loss = (sum(losses) / len(losses)) if losses else 0.0
        gross_win = sum(wins)
        gross_loss = sum(losses)  # negativo
        profit_factor = (gross_win / abs(gross_loss)) if gross_loss < 0 else None
        best_win = max(wins) if wins else 0.0
        worst_loss = min(losses) if losses else 0.0

        # Drawdown em curva de P&L realizado na janela.
        equity = 0.0
        peak_equity = 0.0
        max_drawdown = 0.0
        for event in realized_events:
            equity += event['pnl']
            if equity > peak_equity:
                peak_equity = equity
            drawdown = peak_equity - equity
            if drawdown > max_drawdown:
                max_drawdown = drawdown

        top_winner_symbol = ""
        top_winner_pnl = 0.0
        top_loser_symbol = ""
        top_loser_pnl = 0.0
        if symbol_pnl:
            top_winner_symbol, top_winner_pnl = max(symbol_pnl.items(), key=lambda kv: kv[1])
            top_loser_symbol, top_loser_pnl = min(symbol_pnl.items(), key=lambda kv: kv[1])

        try:
            account_info = self.exchange.get_account_info()
            open_pnl = float(account_info.get('unrealized_pnl', 0.0))
        except Exception:
            open_pnl = 0.0

        net_after_costs = realized_total + commission_total + funding_total
        net_with_open = net_after_costs + open_pnl

        return {
            'lookback_hours': lookback_hours,
            'window_start_utc': window_start_utc,
            'window_end_utc': now_utc,
            'trade_count': trade_count,
            'win_count': len(wins),
            'loss_count': len(losses),
            'win_rate': win_rate,
            'gross_win': gross_win,
            'gross_loss': gross_loss,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'best_win': best_win,
            'worst_loss': worst_loss,
            'profit_factor': profit_factor,
            'max_drawdown': max_drawdown,
            'realized_total': realized_total,
            'commission_total': commission_total,
            'funding_total': funding_total,
            'net_after_costs': net_after_costs,
            'open_pnl': open_pnl,
            'net_with_open': net_with_open,
            'top_winner_symbol': top_winner_symbol,
            'top_winner_pnl': top_winner_pnl,
            'top_loser_symbol': top_loser_symbol,
            'top_loser_pnl': top_loser_pnl,
            'income_count': len(income_list),
        }

    def send_daily_performance_report(self, force: bool = False) -> bool:
        """
        Envia relatório diário consolidado para tomada de decisão de risco/SL.
        """
        lookback_hours = max(1, int(getattr(config, "DAILY_PERFORMANCE_REPORT_LOOKBACK_HOURS", 24)))
        snapshot = self._build_daily_performance_snapshot(lookback_hours=lookback_hours)

        if snapshot['trade_count'] == 0 and not force:
            return False

        status_emoji = "🟢" if snapshot['net_after_costs'] >= 0 else "🔴"
        status_text = "POSITIVO" if snapshot['net_after_costs'] >= 0 else "NEGATIVO"
        profit_factor = snapshot['profit_factor']
        profit_factor_text = "∞" if profit_factor is None else f"{profit_factor:.2f}"
        sl_status = "ON" if config.USE_INDIVIDUAL_STOP_LOSS else "OFF"

        start_utc = snapshot['window_start_utc'].strftime("%d/%m %H:%M")
        end_utc = snapshot['window_end_utc'].strftime("%d/%m %H:%M")
        top_winner = snapshot['top_winner_symbol'].replace('USDT', '') if snapshot['top_winner_symbol'] else "N/A"
        top_loser = snapshot['top_loser_symbol'].replace('USDT', '') if snapshot['top_loser_symbol'] else "N/A"

        message = (
            f"📅 <b>RELATÓRIO DIÁRIO ({snapshot['lookback_hours']}h)</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🕒 <b>Janela UTC:</b> {start_utc} → {end_utc}\n"
            f"🧾 <b>Registros income:</b> <code>{snapshot['income_count']}</code>\n\n"
            f"📊 <b>TRADES FECHADOS:</b>\n"
            f"• Total: <code>{snapshot['trade_count']}</code>\n"
            f"• Wins/Losses: <code>{snapshot['win_count']}/{snapshot['loss_count']}</code>\n"
            f"• Win Rate: <code>{snapshot['win_rate']:.1f}%</code>\n"
            f"• Profit Factor: <code>{profit_factor_text}</code>\n"
            f"• Média Win: <code>{self._format_usd_brl(snapshot['avg_win'], 4, True)}</code>\n"
            f"• Média Loss: <code>{self._format_usd_brl(snapshot['avg_loss'], 4, True)}</code>\n"
            f"• Maior Win: <code>{self._format_usd_brl(snapshot['best_win'], 4, True)}</code>\n"
            f"• Maior Loss: <code>{self._format_usd_brl(snapshot['worst_loss'], 4, True)}</code>\n"
            f"• Max Drawdown (realizado): <code>{self._format_usd_brl(-snapshot['max_drawdown'], 4, True)}</code>\n\n"
            f"{status_emoji} <b>RESULTADO ({status_text}):</b>\n"
            f"• Realizado (trades): <code>{self._format_usd_brl(snapshot['realized_total'], 4, True)}</code>\n"
            f"• Comissão: <code>{self._format_usd_brl(snapshot['commission_total'], 4, True)}</code>\n"
            f"• Funding: <code>{self._format_usd_brl(snapshot['funding_total'], 4, True)}</code>\n"
            f"• Líquido: <code>{self._format_usd_brl(snapshot['net_after_costs'], 4, True)}</code>\n"
            f"• Aberto agora: <code>{self._format_usd_brl(snapshot['open_pnl'], 4, True)}</code>\n"
            f"• Líquido + Aberto: <code>{self._format_usd_brl(snapshot['net_with_open'], 4, True)}</code>\n\n"
            f"🏆 <b>Top Winner:</b> <code>{top_winner} {self._format_usd_brl(snapshot['top_winner_pnl'], 4, True)}</code>\n"
            f"📉 <b>Top Loser:</b> <code>{top_loser} {self._format_usd_brl(snapshot['top_loser_pnl'], 4, True)}</code>\n\n"
            f"🛡️ <b>Risco atual:</b> SL {sl_status} ({config.STOP_LOSS_PERCENT:.2f}%) | "
            f"Trailing {config.TRAILING_ACTIVATION_PERCENT:.2f}/{config.TRAILING_DISTANCE_PERCENT:.2f}%\n"
            f"━━━━━━━━━━━━━━━━━━━━━"
        )

        return self.telegram.send_message(message)

    def _maybe_send_daily_performance_report(self) -> bool:
        """
        Dispara relatório diário automático 1x por dia no horário BRT configurado.
        """
        if not bool(getattr(config, "DAILY_PERFORMANCE_REPORT_ENABLED", True)):
            return False

        report_hour = int(getattr(config, "DAILY_PERFORMANCE_REPORT_HOUR_BRT", 23))
        report_minute = int(getattr(config, "DAILY_PERFORMANCE_REPORT_MINUTE_BRT", 55))
        now_brt = datetime.now(timezone(timedelta(hours=-3)))

        if (now_brt.hour, now_brt.minute) < (report_hour, report_minute):
            return False

        report_date = now_brt.strftime("%Y-%m-%d")
        if self.last_daily_performance_report_date == report_date:
            return False

        sent = self.send_daily_performance_report(force=True)
        if sent:
            self.last_daily_performance_report_date = report_date
            self.save_state()
            logger.info(
                f"📅 Relatório diário enviado ({report_date} BRT às "
                f"{report_hour:02d}:{report_minute:02d})"
            )
        return sent
    
    def send_trades_report(self):
        """
        Envia relatório detalhado de trades por moeda via Telegram.
        Mostra trades positivos e negativos separados, com valores por moeda.
        Inclui total de taxas pagas.
        """
        # Não envia se não tem trades
        total_trades = self.trades_win_count + self.trades_loss_count
        if total_trades == 0:
            logger.info("📊 Sem trades para relatório")
            return
        
        logger.info("📈 Enviando relatório de trades...")
        
        self.telegram.send_trades_report(
            trades_by_symbol=self.trades_by_symbol,
            total_wins=self.trades_win_count,
            total_losses=self.trades_loss_count,
            total_win_value=self.trades_win_total,
            total_loss_value=self.trades_loss_total,
            total_fees=self.total_fees_paid
        )
    
    def take_portfolio_snapshot(self):
        """
        Captura um snapshot do estado atual da carteira para histórico.
        Guarda: timestamp (UTC), saldo, P&L realizado, P&L não realizado
        
        Usa UTC para consistência com a Binance. A conversão para
        horário local (BRT) é feita na exibição.
        """
        from datetime import datetime, timezone
        
        now = datetime.now(timezone.utc)  # Usa UTC como a Binance
        
        # Verifica se já passou o intervalo desde o último snapshot
        if self.last_snapshot_time:
            # Garante que last_snapshot_time também tem timezone
            last_time = self.last_snapshot_time
            if last_time.tzinfo is None:
                last_time = last_time.replace(tzinfo=timezone.utc)
            
            elapsed = (now - last_time).total_seconds() / 60
            if elapsed < self.snapshot_interval_minutes:
                return  # Ainda não é hora de tirar snapshot
        
        # Busca dados REAIS da Binance
        account_info = self.exchange.get_account_info()
        balance = account_info['wallet_balance']  # Saldo total da carteira
        total_unrealized = account_info['unrealized_pnl']  # P&L não realizado real
        
        # Busca P&L diário real para o snapshot
        daily_pnl_binance = self.exchange.get_daily_pnl_from_binance()
        daily_pnl_real = daily_pnl_binance['total']
        
        # Cria o snapshot com dados reais
        snapshot = {
            'timestamp': now,  # UTC
            'balance': balance,
            'pnl_realized': daily_pnl_real,  # P&L diário REAL da Binance
            'pnl_unrealized': total_unrealized,
            'pnl_total': daily_pnl_real + total_unrealized,  # Total real
            'closed_trades': self.closed_trades_count
        }
        
        self.portfolio_history.append(snapshot)
        self.last_snapshot_time = now
        
        # Mantém apenas os últimos 48 snapshots (24h se for a cada 30min)
        if len(self.portfolio_history) > 48:
            self.portfolio_history = self.portfolio_history[-48:]
        
        logger.info(f"📸 Snapshot capturado: P&L Total ${snapshot['pnl_total']:.2f}")
    
    def send_portfolio_evolution(self):
        """
        Envia relatório de evolução da carteira para o Telegram.
        Usa dados REAIS da Binance e timezone correto (Brasil).
        """
        from datetime import datetime, timezone, timedelta
        
        # Timezone do Brasil (UTC-3)
        BRT = timezone(timedelta(hours=-3))
        now_brt = datetime.now(BRT)
        
        # Busca dados REAIS da Binance
        account_info = self.exchange.get_account_info()
        balance = account_info['wallet_balance']  # Saldo total da carteira
        total_unrealized = account_info['unrealized_pnl']  # P&L não realizado
        
        # Busca P&L diário REAL da Binance
        daily_pnl_binance = self.exchange.get_daily_pnl_from_binance()
        daily_pnl_real = daily_pnl_binance['total']
        
        # P&L total = P&L realizado do DIA (Binance) + não realizado
        total_pnl = daily_pnl_real + total_unrealized
        
        # Calcula variação percentual baseado no capital inicial
        pct_change = (total_pnl / self.initial_capital) * 100 if self.initial_capital > 0 else 0
        
        # Prepara dados do histórico para o Telegram (converte para horário do Brasil)
        history_data = []
        for snap in self.portfolio_history[-12:]:  # Últimos 12 snapshots
            # Converte timestamp para horário do Brasil
            snap_time = snap['timestamp']
            if snap_time.tzinfo is None:
                # Se não tem timezone, assume que é UTC e converte para BRT
                snap_time = snap_time.replace(tzinfo=timezone.utc).astimezone(BRT)
            else:
                snap_time = snap_time.astimezone(BRT)
            
            history_data.append({
                'time': snap_time.strftime("%H:%M"),
                # Histórico da evolução: usa P&L realizado (não realizado fica fora)
                'pnl': snap.get('pnl_realized', snap.get('pnl_total', 0.0))
            })
        
        # Adiciona snapshot atual se não estiver no histórico (usando horário Brasil)
        current_snap = {
            'time': now_brt.strftime("%H:%M"),
            'pnl': daily_pnl_real
        }
        if not history_data or history_data[-1]['time'] != current_snap['time']:
            history_data.append(current_snap)
        
        # Envia para o Telegram com estatísticas de trades
        self.telegram.send_portfolio_evolution(
            initial_capital=self.initial_capital,
            current_balance=balance,  # Saldo REAL da Binance
            total_pnl=total_pnl,
            pnl_realized=daily_pnl_real,  # P&L diário REAL da Binance
            pnl_unrealized=total_unrealized,
            pct_change=pct_change,
            closed_trades=self.closed_trades_count,
            trades_win_count=self.trades_win_count,
            trades_loss_count=self.trades_loss_count,
            trades_win_total=self.trades_win_total,
            trades_loss_total=self.trades_loss_total,
            history=history_data,
            bot_start_time=self.start_time if hasattr(self, 'start_time') else now_brt
        )

    def send_api_health_report(self, force: bool = False, trigger_reason: str = "") -> bool:
        """
        Envia resumo operacional consolidado (API + ordens + runtime) no Telegram.
        """
        if not hasattr(self, 'exchange') or not hasattr(self.exchange, 'get_retry_stats_report'):
            return False

        try:
            api_report = self.exchange.get_retry_stats_report(reset=True)
            order_report = (
                self.exchange.get_order_stats_report(reset=True)
                if hasattr(self.exchange, 'get_order_stats_report')
                else self._empty_order_stats_report()
            )
            runtime_report = self.get_runtime_stats_report(reset=True)

            calls = api_report.get('calls', 0)
            retries = api_report.get('retries', 0)
            failures = api_report.get('failures', 0)
            retry_rate = api_report.get('retry_rate', 0.0)
            failure_rate = api_report.get('failure_rate', 0.0)

            order_attempts = order_report.get('attempts', 0)
            order_successes = order_report.get('successes', 0)
            order_failures = order_report.get('failures', 0)
            order_rejections = order_report.get('rejections', 0)
            order_failure_rate = order_report.get('failure_rate', 0.0)
            order_rejection_rate = order_report.get('rejection_rate', 0.0)

            monitor_cycles = runtime_report.get('monitor_cycles', 0)
            analysis_steps = runtime_report.get('analysis_steps', 0)
            monitor_overruns = runtime_report.get('monitor_overruns', 0)
            analysis_overruns = runtime_report.get('analysis_overruns', 0)
            loop_errors = runtime_report.get('loop_errors', 0)
            last_error = runtime_report.get('last_error', '')

            monitor_overrun_rate = (
                monitor_overruns / monitor_cycles * 100 if monitor_cycles else 0.0
            )
            analysis_overrun_rate = (
                analysis_overruns / analysis_steps * 100 if analysis_steps else 0.0
            )

            has_api_issue = retries > 0 or failures > 0
            has_order_issue = order_failures > 0 or order_rejections > 0
            has_runtime_issue = monitor_overruns > 0 or analysis_overruns > 0 or loop_errors > 0
            has_issues = has_api_issue or has_order_issue or has_runtime_issue

            has_data = (
                calls > 0 or order_attempts > 0 or monitor_cycles > 0 or analysis_steps > 0 or loop_errors > 0
            )
            if not force and not has_data:
                return False

            # Modo silencioso: só envia quando houver instabilidade
            if not force and config.API_HEALTH_TELEGRAM_ONLY_ON_ISSUES and not has_issues:
                return False

            if failures > 0 or order_failures > 0 or loop_errors > 0:
                emoji = "🔴"
                status = "CRÍTICO"
            elif has_issues:
                emoji = "🟡"
                status = "ATENÇÃO"
            else:
                emoji = "🟢"
                status = "ESTÁVEL"

            message = (
                f"📡 <b>HEALTH REPORT</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"{emoji} <b>Status:</b> {status}\n"
                f"📞 <b>API Calls:</b> <code>{calls}</code>\n"
                f"🔁 <b>API Retries:</b> <code>{retries}</code> (<code>{retry_rate:.1f}%</code>)\n"
                f"❌ <b>API Falhas:</b> <code>{failures}</code> (<code>{failure_rate:.1f}%</code>)\n"
            )

            if trigger_reason:
                message += f"🚨 <b>Trigger:</b> <code>{trigger_reason}</code>\n"

            top_endpoints = api_report.get('endpoints', [])[:5]
            if top_endpoints:
                message += "\n<b>🔍 API endpoints:</b>\n"
                for endpoint in top_endpoints:
                    if endpoint['retries'] == 0 and endpoint['failures'] == 0 and not force:
                        continue
                    message += (
                        f"• <code>{endpoint['label']}</code>: "
                        f"{endpoint['calls']} calls | "
                        f"{endpoint['retries']} retries | "
                        f"{endpoint['failures']} falhas\n"
                    )

            message += (
                f"\n<b>🧾 Ordens:</b>\n"
                f"• Tentativas: <code>{order_attempts}</code>\n"
                f"• Sucessos: <code>{order_successes}</code>\n"
                f"• Falhas: <code>{order_failures}</code> (<code>{order_failure_rate:.1f}%</code>)\n"
                f"• Rejeições: <code>{order_rejections}</code> (<code>{order_rejection_rate:.1f}%</code>)\n"
            )

            top_order_symbols = order_report.get('symbols', [])[:5]
            if top_order_symbols:
                message += "<b>• Top símbolos com falha:</b>\n"
                for item in top_order_symbols:
                    if item['failures'] == 0 and item['rejections'] == 0 and not force:
                        continue
                    message += (
                        f"  - <code>{item['symbol']}</code>: "
                        f"{item['attempts']} tent. | "
                        f"{item['failures']} falhas | "
                        f"{item['rejections']} rejeições\n"
                    )

            message += (
                f"\n<b>⏱️ Loops:</b>\n"
                f"• Monitor: <code>{monitor_cycles}</code> ciclos | "
                f"avg <code>{runtime_report.get('monitor_avg_seconds', 0.0):.2f}s</code> | "
                f"max <code>{runtime_report.get('monitor_max_seconds', 0.0):.2f}s</code> | "
                f"overrun <code>{monitor_overruns}</code> (<code>{monitor_overrun_rate:.1f}%</code>)\n"
                f"• Análise: <code>{analysis_steps}</code> steps | "
                f"avg <code>{runtime_report.get('analysis_avg_seconds', 0.0):.2f}s</code> | "
                f"max <code>{runtime_report.get('analysis_max_seconds', 0.0):.2f}s</code> | "
                f"overrun <code>{analysis_overruns}</code> (<code>{analysis_overrun_rate:.1f}%</code>)\n"
                f"• Erros loop: <code>{loop_errors}</code>\n"
            )

            slow_symbols = runtime_report.get('slow_symbols', [])[:5]
            if slow_symbols:
                message += "<b>• Símbolos mais lentos:</b>\n"
                for item in slow_symbols:
                    message += (
                        f"  - <code>{item['symbol']}</code>: "
                        f"{item['count']} overruns | "
                        f"max <code>{item['max_seconds']:.2f}s</code>\n"
                    )

            if last_error:
                message += f"\n<b>⚠️ Último erro:</b> <code>{last_error}</code>\n"

            message += "\n━━━━━━━━━━━━━━━━━━━━━"
            self.telegram.send_message(message)
            return True

        except Exception as e:
            logger.warning(f"⚠️ Erro ao enviar health report para Telegram: {e}")
            return False

    def _get_loop_timing_profile(self) -> dict:
        """
        Retorna o perfil de timing dos loops.

        Quando a estratégia Binance está ativa, ajusta automaticamente
        conforme a quantidade de pares da faixa atual.
        """
        # Base (fallback/config manual)
        profile = {
            'monitor_interval': max(1, int(config.POSITION_MONITOR_INTERVAL)),
            'analysis_cycle_interval': max(1, int(config.CHECK_INTERVAL)),
            'analysis_symbol_delay': max(0.1, float(config.ANALYSIS_SYMBOL_DELAY)),
            'mode': 'manual',
            'pairs': len(config.TRADING_PAIRS),
        }

        if not config.USE_BINANCE_STRATEGY:
            return profile

        num_pairs = len(config.TRADING_PAIRS)

        # Preset por faixa de pares (dinâmico com a estratégia Binance)
        if num_pairs <= 3:
            profile.update({
                'monitor_interval': 2,
                'analysis_cycle_interval': 3,
                'analysis_symbol_delay': 0.5,
            })
        elif num_pairs <= 6:
            profile.update({
                'monitor_interval': 2,
                'analysis_cycle_interval': 4,
                'analysis_symbol_delay': 0.7,
            })
        elif num_pairs <= 9:
            profile.update({
                'monitor_interval': 2,
                'analysis_cycle_interval': 5,
                'analysis_symbol_delay': 0.9,
            })
        elif num_pairs <= 10:
            profile.update({
                'monitor_interval': 2,
                'analysis_cycle_interval': 6,
                'analysis_symbol_delay': 1.0,
            })
        elif num_pairs <= 11:
            profile.update({
                'monitor_interval': 3,
                'analysis_cycle_interval': 6,
                'analysis_symbol_delay': 1.1,
            })
        else:
            profile.update({
                'monitor_interval': 3,
                'analysis_cycle_interval': 7,
                'analysis_symbol_delay': 1.2,
            })

        profile['mode'] = 'dynamic_binance'
        return profile

    @staticmethod
    def _timing_profile_changed(old_profile: dict, new_profile: dict) -> bool:
        """Compara perfis de timing relevantes para detectar mudança."""
        keys = ('monitor_interval', 'analysis_cycle_interval', 'analysis_symbol_delay', 'pairs', 'mode')
        return any(old_profile.get(k) != new_profile.get(k) for k in keys)
    
    def run(self):
        """
        Loop principal com dois ciclos independentes:

        1. Ciclo de análise de entradas (mais lento)
        2. Ciclo de monitoramento de posições (mais rápido)
        """
        # Garante que apenas uma instância do bot rode por vez
        if not self._acquire_instance_lock():
            logger.error("🛑 Inicialização abortada: instância duplicada detectada.")
            return

        # Setup inicial
        if not self.setup_exchange():
            logger.error("❌ Falha no setup da exchange!")
            self._release_instance_lock()
            return
        
        self.running = True
        monitor_cycle = 0
        analysis_cycle = 0

        # Configuração inicial dos dois ciclos (com ajuste dinâmico por faixa)
        timing_profile = self._get_loop_timing_profile()
        monitor_interval = timing_profile['monitor_interval']
        analysis_cycle_interval = timing_profile['analysis_cycle_interval']
        analysis_symbol_delay = timing_profile['analysis_symbol_delay']

        logger.info(
            f"⏱️ Timing dos loops: monitor={monitor_interval}s | "
            f"ciclo_entrada={analysis_cycle_interval}s | "
            f"delay_simbolo={analysis_symbol_delay:.1f}s | "
            f"modo={timing_profile['mode']} | pares={timing_profile['pairs']}"
        )

        # Mantém as mesmas cadências históricas de relatórios/manutenção
        # (baseadas no CHECK_INTERVAL configurado manualmente)
        base_interval = max(1, int(config.CHECK_INTERVAL))
        terminal_status_interval = base_interval * 3
        state_save_interval = base_interval * 30
        commission_update_interval = base_interval * 360
        pair_update_interval = base_interval * 2160
        deposit_check_interval = base_interval * 60
        strategy_check_interval = base_interval * 60

        now = time.monotonic()
        next_monitor_time = now
        next_analysis_cycle_time = now
        next_analysis_step_time = now

        next_terminal_status_time = now + terminal_status_interval
        next_state_save_time = now + state_save_interval
        next_commission_update_time = now + commission_update_interval
        next_pair_update_time = now + pair_update_interval
        next_deposit_check_time = now + deposit_check_interval
        next_strategy_check_time = now + strategy_check_interval

        analysis_cycle_active = False
        analysis_tasks = []
        analysis_index = 0
        
        # Inicia polling de comandos do Telegram
        logger.info("🎮 Iniciando polling de comandos Telegram...")
        self.command_handler.start_polling()
        
        logger.info("🏁 Bot iniciado! Pressione CTRL+C para parar.")
        logger.info("📱 Use /help no Telegram para ver comandos disponíveis.")
        
        # Envia mensagem de início no Telegram
        self.telegram.send_startup_message(
            pairs=config.TRADING_PAIRS,
            capital=self.initial_capital,
            leverage=config.LEVERAGE
        )
        
        # Envia mensagem sobre comandos disponíveis
        self.telegram.send_message(
            "🎮 <b>COMANDOS TELEGRAM ATIVOS</b>\n\n"
            "Use /help para ver todos os comandos.\n"
            "Use /status para ver status a qualquer momento.\n"
            "Use /portfolio para evolução da carteira.\n"
            "Use /trades para relatório de trades.\n"
            "Use /dailyreport para relatório diário e controle on/off.\n"
            "Use /apihealth para relatório de saúde.\n"
            "Use /coins para listar e gerenciar moedas ativas.\n"
            "Use /sentiment para ligar/desligar o filtro de viés.\n"
            "Use /pause para pausar o bot.\n"
            "Use /stop para parar o bot."
        )
        
        while self.running:
            try:
                now = time.monotonic()

                # Atualiza timing automaticamente se a faixa/pares mudou
                new_timing_profile = self._get_loop_timing_profile()
                if self._timing_profile_changed(timing_profile, new_timing_profile):
                    old_timing_profile = timing_profile
                    timing_profile = new_timing_profile
                    monitor_interval = timing_profile['monitor_interval']
                    analysis_cycle_interval = timing_profile['analysis_cycle_interval']
                    analysis_symbol_delay = timing_profile['analysis_symbol_delay']

                    logger.info(
                        f"🔄 Timing atualizado: monitor {old_timing_profile['monitor_interval']}s→{monitor_interval}s | "
                        f"ciclo_entrada {old_timing_profile['analysis_cycle_interval']}s→{analysis_cycle_interval}s | "
                        f"delay_simbolo {old_timing_profile['analysis_symbol_delay']:.1f}s→{analysis_symbol_delay:.1f}s | "
                        f"pares={timing_profile['pairs']}"
                    )

                    # Reagenda próximos eventos para aplicar mudança sem travar
                    next_monitor_time = min(next_monitor_time, now + monitor_interval)
                    if analysis_cycle_active:
                        next_analysis_step_time = min(next_analysis_step_time, now + analysis_symbol_delay)
                    else:
                        next_analysis_cycle_time = min(next_analysis_cycle_time, now + analysis_cycle_interval)

                # ============================================
                # CICLO RÁPIDO: MONITORAMENTO DE POSIÇÕES
                # ============================================
                if now >= next_monitor_time:
                    monitor_cycle += 1
                    monitor_started_at = time.monotonic()

                    if self.paused:
                        logger.info("⏸️  Bot PAUSADO - Apenas monitorando posições")

                    # Monitora posições e risco
                    self.monitor_positions()
                    self.check_daily_targets()

                    if self.check_global_stop_loss():
                        self.execute_global_stop_loss()
                        break

                    # Snapshot periódico da carteira
                    self.take_portfolio_snapshot()

                    # Status periódico somente no terminal
                    if now >= next_terminal_status_time:
                        self.print_status(send_telegram=False)
                        next_terminal_status_time = now + terminal_status_interval

                    # Relatórios e manutenção periódica
                    self._maybe_send_daily_performance_report()

                    if now >= next_state_save_time:
                        self.save_state()
                        next_state_save_time = now + state_save_interval

                    if now >= next_commission_update_time:
                        self.update_commission_rates()
                        next_commission_update_time = now + commission_update_interval

                    if now >= next_pair_update_time:
                        if config.USE_BINANCE_STRATEGY:
                            self.update_binance_strategy_coins()
                        else:
                            self.update_trading_pairs()
                        next_pair_update_time = now + pair_update_interval

                    if now >= next_deposit_check_time:
                        self.check_for_deposit()
                        next_deposit_check_time = now + deposit_check_interval

                    if config.USE_BINANCE_STRATEGY and now >= next_strategy_check_time:
                        self.check_and_update_binance_strategy()
                        next_strategy_check_time = now + strategy_check_interval

                    next_monitor_time = now + monitor_interval
                    monitor_duration = time.monotonic() - monitor_started_at
                    self._record_loop_timing(
                        loop_type='monitor',
                        duration_seconds=monitor_duration,
                        target_interval_seconds=monitor_interval
                    )

                # ============================================
                # CICLO LENTO: ANÁLISE DE ENTRADAS
                # ============================================
                if self.paused:
                    # Cancela ciclo em andamento quando pausa
                    if analysis_cycle_active:
                        analysis_cycle_active = False
                        analysis_tasks = []
                        analysis_index = 0

                    if now >= next_analysis_cycle_time:
                        next_analysis_cycle_time = now + analysis_cycle_interval
                else:
                    # Inicia um novo ciclo completo de análise
                    if (not analysis_cycle_active) and now >= next_analysis_cycle_time:
                        analysis_tasks = self._build_analysis_tasks()
                        analysis_index = 0
                        analysis_cycle_active = True
                        analysis_cycle += 1
                        next_analysis_step_time = now

                        symbols_preview = [item["symbol"].replace("USDT", "") for item in analysis_tasks[:12]]
                        if len(analysis_tasks) > 12:
                            symbols_preview.append("...")
                        logger.info(
                            f"🔍 Iniciando ciclo de análise #{analysis_cycle} "
                            f"em {len(analysis_tasks)} tarefa(s): "
                            f"{', '.join(symbols_preview)}"
                        )

                    # Analisa um símbolo por vez (intercalado com monitoramento)
                    if analysis_cycle_active and now >= next_analysis_step_time:
                        if analysis_index < len(analysis_tasks):
                            task = analysis_tasks[analysis_index]
                            analysis_index += 1
                            symbol = task["symbol"]
                            task_strategy = task.get("strategy_name")
                            analysis_started_at = time.monotonic()
                            self.analyze_and_trade(symbol, strategy_name=task_strategy)
                            analysis_duration = time.monotonic() - analysis_started_at
                            self._record_loop_timing(
                                loop_type='analysis',
                                duration_seconds=analysis_duration,
                                target_interval_seconds=analysis_symbol_delay,
                                symbol=symbol
                            )
                            next_analysis_step_time = now + analysis_symbol_delay

                        # Finaliza ciclo quando todos os pares forem processados
                        if analysis_index >= len(analysis_tasks):
                            analysis_cycle_active = False
                            next_analysis_cycle_time = now + analysis_cycle_interval

                # Sleep curto para não ocupar CPU em busy-loop
                now = time.monotonic()
                next_events = [next_monitor_time]
                if not self.paused:
                    if analysis_cycle_active:
                        next_events.append(next_analysis_step_time)
                    else:
                        next_events.append(next_analysis_cycle_time)

                sleep_time = min(next_events) - now
                time.sleep(max(0.05, min(0.5, sleep_time if sleep_time > 0 else 0.05)))
                
            except Exception as e:
                logger.error(f"❌ Erro no loop principal: {e}")
                self._record_runtime_error(e)
                time.sleep(2)  # Pausa curta antes de tentar novamente

        # Loop finalizado (parada normal/comando/sinal): libera lock
        self._release_instance_lock()
    
    def stop(self):
        """
        Para o bot de forma segura.
        Mostra resumo completo de P&L.
        IMPORTANTE: não fecha posições automaticamente.
        """
        logger.info("🛑 Parando o bot...")
        self.running = False
        
        # Para o polling de comandos do Telegram
        if hasattr(self, 'command_handler'):
            self.command_handler.stop_polling()

        # Emite resumo final de saúde da API (retries/falhas da janela atual)
        if hasattr(self, 'exchange') and hasattr(self.exchange, 'flush_retry_stats'):
            try:
                self.exchange.flush_retry_stats()
            except Exception as e:
                logger.warning(f"⚠️ Erro ao flush de métricas de API: {e}")

        # Mantém posições abertas e apenas coleta estado atual para resumo
        logger.info("📌 Mantendo posições abertas para retomar no próximo start.")
        positions = self.exchange.get_open_positions()
        unrealized_by_symbol = {}
        total_unrealized = 0
        for pos in positions:
            symbol = pos['symbol']
            pnl = pos['unrealized_pnl']
            total_unrealized += pnl
            if symbol not in unrealized_by_symbol:
                unrealized_by_symbol[symbol] = 0
            unrealized_by_symbol[symbol] += pnl
        
        # Imprime resumo final
        logger.info("\n" + "=" * 60)
        logger.info("📊 RESUMO FINAL")
        logger.info("=" * 60)
        logger.info(f"📝 Total de trades: {len(self.trade_history)}")
        logger.info("-" * 60)
        
        # P&L Geral
        logger.info("💵 P&L FINAL:")
        logger.info(f"   • P&L Diário: ${self.risk_manager.daily_pnl:.2f}")
        logger.info(f"   • P&L Total Realizado: ${self.total_pnl:.2f}")
        logger.info(f"   • P&L Não Realizado: ${total_unrealized:.2f}")
        logger.info(f"   • P&L Total (Real + Não Real): ${self.total_pnl + total_unrealized:.2f}")
        logger.info("-" * 60)
        
        # P&L por Par de Moeda
        logger.info("📈 P&L FINAL POR PAR DE MOEDA:")
        for symbol in config.TRADING_PAIRS:
            realized = self.pnl_by_symbol.get(symbol, 0)
            unrealized = unrealized_by_symbol.get(symbol, 0)
            total = realized + unrealized
            
            if total > 0:
                emoji = "🟢"
            elif total < 0:
                emoji = "🔴"
            else:
                emoji = "⚪"
            
            logger.info(f"   {emoji} {symbol}:")
            logger.info(f"      Realizado: ${realized:.2f} | Não Real: ${unrealized:.2f} | Total: ${total:.2f}")
        
        logger.info("-" * 60)
        
        if positions:
            logger.info(f"\n📌 Posições mantidas abertas: {len(positions)}")
            for pos in positions:
                logger.info(f"   {pos['side']} {pos['symbol']}: ${pos['unrealized_pnl']:.2f}")
        
        logger.info("=" * 60)
        logger.info("👋 Bot finalizado!")
        
        # Salva o estado antes de encerrar
        logger.info("💾 Salvando estado...")
        self.save_state()
        
        # Envia mensagem de encerramento no Telegram
        self.telegram.send_shutdown_message(
            total_pnl=self.total_pnl + total_unrealized,
            total_trades=self.closed_trades_count
        )


def main():
    """
    Função principal - ponto de entrada do bot.
    """
    print("""
    ╔══════════════════════════════════════════════════╗
    ║                                                  ║
    ║   🤖 BOT DE TRADING - ESTRATÉGIA DIRECIONAL      ║
    ║      (Baseada em Sinais + Trailing Stop)         ║
    ║                                                  ║
    ╚══════════════════════════════════════════════════╝
    """)
    
    # Mostra configurações
    print("📋 Configurações:")
    print(f"   • Ambiente: {config.APP_ENV}")
    print(f"   • Runtime: {config.RUNTIME_DIR}")
    print("   • Capital: DINÂMICO (saldo da carteira de futuros)")
    print(f"   • Alavancagem: {config.LEVERAGE}x")
    print(f"   • Testnet: {'Sim' if config.USE_TESTNET else 'NÃO (DINHEIRO REAL!)'}")
    print(f"   • Pares: {', '.join(config.TRADING_PAIRS)}")
    print(f"   • Stop Loss: {config.STOP_LOSS_PERCENT}%")
    print(f"   • Take Profit: {config.TAKE_PROFIT_PERCENT}%")
    print()
    
    # Confirmação para MAINNET
    if not config.USE_TESTNET:
        auto_confirm_raw = os.getenv("TRADING_BOT_MAINNET_CONFIRM", "").strip().lower()
        auto_confirm_enabled = auto_confirm_raw in {
            "sim",
            "yes",
            "true",
            "1",
            "eu_sei_o_risco",
        }

        if auto_confirm_enabled:
            print("✅ Confirmação MAINNET via TRADING_BOT_MAINNET_CONFIRM.")
        elif sys.stdin.isatty():
            resp = input("⚠️  ATENÇÃO: Modo MAINNET (dinheiro real)! Confirma? (sim/não): ")
            if resp.strip().lower() != 'sim':
                print("Operação cancelada.")
                return
        else:
            print(
                "❌ Execução não interativa em MAINNET sem confirmação explícita.\n"
                "Defina TRADING_BOT_MAINNET_CONFIRM=eu_sei_o_risco no ambiente/.env."
            )
            return
    
    # Cria e executa o bot
    bot = TradingBot()
    bot.run()


if __name__ == "__main__":
    main()

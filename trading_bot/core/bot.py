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
from logging.handlers import RotatingFileHandler
import signal
import sys
import os
import fcntl
import threading
import concurrent.futures
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import config
from .bot_state_serde import BotStatePersistence
from .scheduler import LoopScheduler, get_loop_timing_profile, timing_profile_changed
from .double_first_policy import DoubleFirstPolicy
from .position_tracker import PositionTracker
from .state_manager import StateManager
from .trade_block_reporter import TradeBlockReporter
from .trade_ledger import TradeLedger
from .trade_store import TradeStore
from ..ai.consultive_engine import ConsultiveEngine
from ..execution import ExecutionEngine
from ..execution.engine import _below_min_trade_volume
from ..infra.binance_client import BinanceConnection
from ..observability import metrics
from .strategy import HedgeStrategy, RangeScalpingStrategy, RiskManager, TechnicalAnalysis
from ..services.kill_switch import KillSwitchMonitor
from ..services.notifications import TelegramNotifier
from ..services.pair_selector import PairSelector
from ..services.telegram_commands import TelegramCommandHandler

logger = logging.getLogger(__name__)


def _format_pair_interval(minutes: int) -> str:
    """Formata minutos como '1h', '6h', '30min' pra uso em mensagens."""
    try:
        m = max(1, int(minutes))
    except (TypeError, ValueError):
        return "?"
    if m >= 60 and m % 60 == 0:
        return f"{m // 60}h"
    return f"{m}min"


def _build_log_file_handler() -> logging.Handler:
    """Handler de arquivo com rotação (RotatingFileHandler) pra não crescer
    sem limite — a VM OCI Micro tem disco pequeno e um log gigante pode
    encher o disco e derrubar o bot. LOG_MAX_BYTES=0 desliga a rotação e
    volta ao FileHandler simples.
    """
    max_bytes = int(getattr(config, "LOG_MAX_BYTES", 0))
    backup_count = int(getattr(config, "LOG_BACKUP_COUNT", 0))
    if max_bytes > 0:
        return RotatingFileHandler(
            config.LOG_FILE_PATH,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
    return logging.FileHandler(config.LOG_FILE_PATH, encoding="utf-8")


def _configure_logging():
    """
    Configura logging com arquivo em runtime/ e nível por ambiente.
    """
    level_name = str(getattr(config, "LOG_LEVEL", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    handlers: list[logging.Handler] = [_build_log_file_handler()]

    if bool(getattr(config, "LOG_TO_STDOUT", True)):
        handlers.append(logging.StreamHandler())

    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=handlers,
        force=True,
    )


def _aggregate_realized_pnl(income_list):
    """Soma TODAS as linhas REALIZED_PNL de uma janela de income da Binance.

    Um SL/TP pode preencher em vários fills parciais, cada um gerando uma linha
    de REALIZED_PNL. Pegar só a última linha (income_list[-1]) capturava um
    parcial e subestimava — ou até invertia o sinal — do P&L real do trade.
    Robusto a linhas malformadas e a tipos de income misturados (só soma
    REALIZED_PNL; quando incomeType ausente, assume REALIZED_PNL pois a query
    já filtra por tipo no servidor).

    Retorna None quando a lista é vazia/inválida (caller cai no fallback).
    """
    if not income_list:
        return None
    total = 0.0
    counted = 0
    for row in income_list:
        if not isinstance(row, dict):
            continue
        if str(row.get('incomeType', 'REALIZED_PNL')) != 'REALIZED_PNL':
            continue
        try:
            total += float(row.get('income', 0) or 0)
            counted += 1
        except (TypeError, ValueError):
            continue
    return total if counted else None


def _implied_exit_price(side, entry_price, pnl_gross, quantity):
    """Preço de saída IMPLÍCITO pelo P&L bruto realizado.

    A Binance não devolve o preço de saída no income; derivamos o exit efetivo
    (equivalente a um VWAP) a partir do gross: para LONG, gross = (exit-entry)*qty.
    Com o gross agregado correto (ver _aggregate_realized_pnl), este exit é
    consistente com o pnl por construção. NÃO é um fill real — é o exit efetivo.
    Necessário só para o trade_history/dashboard marcarem a posição como fechada.
    """
    if not quantity:
        return entry_price
    delta = pnl_gross / quantity
    return entry_price + delta if side == "LONG" else entry_price - delta


def _retain_held_pairs(selected_pairs, held_symbols):
    """Mantém na lista qualquer par com posição aberta que cairia do score.

    Um par com posição aberta NUNCA é rotacionado para fora: segue gerido
    (análise + monitor de SL/TP/trailing) até fechar no próprio alvo. Liquidar
    só porque o par caiu no score (o antigo "Par removido da lista") corta o
    trade no meio. Os pares retidos são anexados ao fim da lista selecionada.

    Retorna (selected_pairs_final, retained_list).
    """
    new_set = set(selected_pairs)
    retained = sorted(set(held_symbols) - new_set)
    final = list(selected_pairs) + retained
    return final, retained


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

        # Migração de arquivo legado (pré-lock, single-threaded no boot)
        StateManager.migrate_legacy(
            target_path=self._state_file_path,
            legacy_path=os.path.join(config.PROJECT_ROOT, "bot_state.json"),
        )

        logger.info("=" * 50)
        logger.info("🤖 INICIANDO BOT DE TRADING")
        logger.info("=" * 50)
        logger.info(
            f"🌍 Ambiente: {config.APP_ENV} | Rede: {config.ENVIRONMENT.upper()} | Runtime: {config.RUNTIME_DIR}"
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
        self._strategy_engines: Dict[str, Any] = {"primary": self.strategy}
        self.strategy_profiles: List[Dict[str, Any]] = []
        self._reload_strategy_profiles(reason="init")
        
        logger.info("🛡️  Inicializando gerenciador de risco...")
        self.risk_manager = RiskManager()
        # Improvement 2: wire real P&L from Binance into RiskManager
        self.risk_manager._real_daily_pnl_fn = lambda: (
            self.exchange.get_daily_pnl_from_binance().get('total', 0.0)
            - getattr(self, 'daily_pnl_binance_baseline', 0.0)
        )

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
        
        # Inicializa atributos de estado (sem side effects externos)
        self._init_runtime_state()

        logger.info("🤖 Inicializando IA consultiva...")
        self.ai_consultive_engine = ConsultiveEngine(config_obj=config)

        self.kill_switch = KillSwitchMonitor(config_obj=config, telegram=self.telegram)

        # Preenche pnl_by_symbol com os pares configurados
        for symbol in config.TRADING_PAIRS:
            self.pnl_by_symbol[symbol] = 0.0

        # Observabilidade de runtime (loops/erros — depende de threading)
        self._runtime_stats_lock = threading.Lock()
        self._runtime_stats_since_report = self._new_runtime_stats()
        self._positions_lock = threading.Lock()

        # Configura handler para CTRL+C / parada graceful.
        # SIGHUP incluso para que `screen -X quit` (ou logout do terminal)
        # acione o shutdown gracioso e libere/remova o lock — senão o
        # arquivo de lock fica órfão no disco.
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, self._signal_handler)

        self.load_state()

        # Inicia exporter Prometheus (opcional, idempotente)
        if getattr(config, "METRICS_ENABLED", True):
            metrics.start_exporter(
                host=getattr(config, "METRICS_HOST", "127.0.0.1"),
                port=getattr(config, "METRICS_PORT", 9090),
            )
            metrics.set_bot_info(environment=config.ENVIRONMENT, app_env=config.APP_ENV)

        logger.info("✅ Bot inicializado com sucesso!")

    def _init_runtime_state(self):
        """
        Inicializa todos os atributos de estado do bot com valores padrão.

        Extraído do __init__ para que testes possam criar um bot leve via
        TradingBot.__new__(TradingBot) e chamar este método sem precisar
        de conexões externas (exchange, Telegram, etc.).

        Qualquer novo atributo de estado deve ser adicionado aqui.
        """
        self.running = False
        self.paused = False
        self.invert_signals = False
        self.positions = {}
        self.trade_history = []
        self.closed_trades_count = 0
        self.total_pnl = 0.0
        self.daily_realized_pnl = 0.0
        self.pnl_by_symbol = {}
        self.trades_win_count = 0
        self.trades_loss_count = 0
        self.trades_win_total = 0.0
        self.trades_loss_total = 0.0
        self.total_fees_paid = 0.0
        # Baseline do PnL diário da Binance no momento do anchor (startup com
        # state fresco OU início de novo dia UTC). Subtraído nas chamadas de
        # display pra que /portfolio comece em $0 após /reset, mantendo o
        # alinhamento entre os contadores internos (zerados) e a API da
        # Binance (que carrega resíduo do dia até 00:00 UTC).
        self.daily_pnl_binance_baseline = 0.0
        self._daily_baseline_date: str | None = None
        self.trades_by_symbol = {}
        self.trades_by_strategy = {}
        self.daily_target_reached = False
        self.last_daily_reset = datetime.now(timezone.utc).date()
        self.last_daily_performance_report_date = ""
        self.portfolio_history = []
        self.last_snapshot_time = None
        self.snapshot_interval_minutes = 10
        # Drawdown alert state — bucket cruzado mais alto no dia (% do capital).
        # Reseta automaticamente no virar do dia. Em memória só.
        self._drawdown_alert_bucket_pct: float = 0.0
        self._drawdown_alert_day: str | None = None
        self.start_time = datetime.now()
        self.initial_capital = None
        self.last_transfer_check_ts_ms = 0
        self.processed_transfer_ids = []
        self.peak_prices = {}
        self.trailing_activated = {}
        # symbol -> epoch do último fechamento negativo; barra reentrada
        # imediata no mesmo símbolo (anti-churn). Ver
        # SYMBOL_REENTRY_COOLDOWN_SECONDS e _symbol_reentry_cooldown_remaining.
        self.symbol_reentry_cooldowns: Dict[str, float] = {}
        self.peak_equity: float = 0.0
        self.peak_equity_ts = None
        self.last_known_balance: float | None = None
        self.known_positions = {}
        self.double_first_used = {}
        self.commission_rates = None
        self.pair_selector = None
        self.sentiment_mode_enabled = bool(getattr(config, "USE_MARKET_SENTIMENT_FILTER", False))
        self.sentiment_cache: Dict[str, Dict[str, Any]] = {}
        self.ai_consultive_engine = None
        self._instance_lock_handle = None
        self._strategy_engines: Dict[str, Any] = {}
        self.strategy_profiles: List[Dict[str, Any]] = []
        # Regime classifier — observações por símbolo (janela rolante de
        # HYSTERESIS_TICKS) e regime atualmente comprometido (após hysteresis).
        self._regime_observations: Dict[str, List[str]] = {}
        self._regime_committed: Dict[str, str] = {}
        # Cooldown da troca de par por regime: símbolo -> timestamp da última
        # troca (entrada ou saída), pra evitar carrossel de rotação.
        self._regime_swap_cooldowns: Dict[str, float] = {}
        # Engines reutilizáveis quando o classifier sobrepõe a estratégia
        # estática do profile (lazy-init em _get_or_create_regime_engine).
        self._regime_engine_cache: Dict[str, Any] = {}
        self._runtime_stats_lock = threading.Lock()
        self._runtime_stats_since_report = self._new_runtime_stats()
        self._positions_lock = threading.Lock()
        self._state_io_lock = threading.Lock()
        # Persistência de estado — StateManager compartilha o lock do bot.
        # Criado aqui (dentro de _init_runtime_state) pra que testes que usam
        # TradingBot.__new__() + _init_runtime_state() também tenham acesso.
        self.state_manager = StateManager(lock=self._state_io_lock)
        # (De)serialização do estado <-> payload JSON (mapeamento puro, sem I/O).
        self.state_persistence = BotStatePersistence(self)
        # Store durável de trades/equity (SQLite). Mantém histórico COMPLETO
        # fora do state JSON (que só carrega janela recente). Defensivo:
        # falha ao abrir => None, e o bot segue sem histórico durável.
        self.trade_store = self._open_trade_store()
        # Engine de execução (close/emergência). Recebe self — mantém
        # acoplamento de dados pra simplicidade; separação CÓDIGO, não DADOS.
        self.execution_engine = ExecutionEngine(self)
        # Tracker de posições — encapsula os 3 dicts correlacionados
        # (known_positions, peak_prices, trailing_activated) atrás de uma
        # API uniforme. Storage continua nos atributos do bot.
        self.positions = PositionTracker(self)
        # Policy do Double First — multiplicador da primeira entrada
        # quando habilitado. Encapsula scope/state_key/try_double/mark_used.
        self.double_first_policy = DoubleFirstPolicy(self)
        # Ledger de bookkeeping pós-trade — encapsula as mutações de
        # stats/contadores que antes ficavam espalhadas no engine.
        self.ledger = TradeLedger(self)
        # Reporter de bloqueios de execução (IA aprovou mas barrou na ordem).
        # Recebe um getter pro telegram porque self.telegram é setado depois,
        # no setup_exchange.
        self.block_reporter = TradeBlockReporter(
            telegram_provider=lambda: getattr(self, "telegram", None),
            config=config,
        )
        # Dashboard web (opt-in via DASHBOARD_ENABLED). Lazy import pra não
        # forçar Flask como dependência obrigatória em ambientes minimalistas.
        self.dashboard_server = None

    def _open_trade_store(self):
        """Abre (ou reabre) o TradeStore SQLite no path do ambiente atual.

        Fecha um store anterior se existir (caso de troca de rede via /env).
        Defensivo: qualquer falha retorna None e o bot segue — o histórico
        durável é best-effort, nunca pode bloquear o trading.
        """
        old = getattr(self, "trade_store", None)
        if old is not None:
            old.close()
        db_path = getattr(config, "TRADE_DB_PATH", "") or ""
        if not db_path:
            return None
        try:
            return TradeStore(db_path)
        except Exception:
            logger.exception("🗃️ Falha ao abrir TradeStore — seguindo sem histórico durável")
            return None

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

        # Remove o arquivo de lock pra não deixar resíduo stale no disco.
        # O flock já é liberado pelo SO no close/morte do processo, mas o
        # arquivo permanece — e ferramentas que olham só a existência dele
        # (scripts de manutenção) interpretariam como "bot rodando".
        try:
            os.unlink(self._instance_lock_path)
        except OSError:
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

    def _state_backup_file_path(self) -> str:
        """Delega pra convenção do StateManager (mantido por compat interna)."""
        return StateManager.backup_file_path(self._state_file_path)

    def _archive_state_file_for_reset(self, state_path: str) -> str:
        """
        Move o arquivo de state pra backup timestampado — usado no switch de
        ambiente pra zerar o tracking da rede nova sem perder dados antigos.

        Retorna o nome do arquivo de backup (string vazia se não havia state).
        Não levanta em caso de falha — reset deve continuar mesmo se o backup
        não for possível (logger.warning cobre o caso).
        """
        try:
            if not state_path or not os.path.exists(state_path):
                return ""
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_path = f"{state_path}.reset-{timestamp}"
            os.rename(state_path, backup_path)
            # Remove .bak residual (fica stale após renomear o principal).
            bak_path = StateManager.backup_file_path(state_path)
            if os.path.exists(bak_path):
                try:
                    os.remove(bak_path)
                except OSError:
                    pass
            logger.warning(f"🗄️ State anterior arquivado em {os.path.basename(backup_path)}")
            return os.path.basename(backup_path)
        except Exception as exc:
            logger.warning(f"⚠️ Falha ao arquivar state antes do reset: {exc}")
            return ""

    def save_state(self):
        """
        Salva o estado atual do bot em um arquivo JSON.
        Isso permite continuar de onde parou após reiniciar.

        Delega I/O atômico pro StateManager; aqui só prepara o payload.
        """
        try:
            payload = self.state_persistence.build_payload()
        except Exception as e:
            logger.error(f"❌ Erro ao montar payload de estado: {e}")
            return False
        return self.state_manager.save(payload, self._state_file_path)
    
    def load_state(self):
        """
        Carrega o estado salvo anteriormente do arquivo JSON.
        Se não existir arquivo, mantém os valores padrão.

        Delega leitura (com fallback backup) pro StateManager; a APLICAÇÃO dos
        campos no bot continua aqui — conhece a forma interna do objeto.
        """
        backup_path = self._state_backup_file_path()
        if not os.path.exists(self._state_file_path) and not os.path.exists(backup_path):
            logger.info("📂 Nenhum estado anterior encontrado. Iniciando do zero.")
            return False

        try:
            state, source_path = self.state_manager.load(self._state_file_path)
            if state is None:
                logger.info("🔄 Nenhum estado válido encontrado (principal/backup). Iniciando do zero.")
                return False
            if source_path == backup_path:
                logger.warning(f"⚠️ Estado carregado do backup: {source_path}")

            # Carrega overrides de pares antes da inicialização da estratégia.
            # disabled_pairs é UNIÃO config ∪ state: um par desabilitado no
            # config.py (safety: min-notional, par perdedor) NUNCA é re-habilitado
            # por state antigo — bug que já religou o BTC (erro -4164, 06-07). O
            # state só ADICIONA desabilitados (ex.: auto-disable runtime), nunca
            # remove os do config.
            saved_disabled_pairs = state.get('disabled_pairs')
            if saved_disabled_pairs is not None:
                merged_disabled = list(config.DISABLED_PAIRS or []) + list(saved_disabled_pairs)
                config.DISABLED_PAIRS = config.normalize_pair_list(merged_disabled)

            saved_binance_coin_list = state.get('binance_coin_list')
            if saved_binance_coin_list:
                config.BINANCE_COIN_LIST = config.normalize_pair_list(saved_binance_coin_list)

            # Restaura APENAS os `pairs` dinâmicos persistidos (warm-start da
            # seleção da Binance). risk_profile, entry_mode, strategy_type,
            # max_pairs e enabled SEMPRE vêm do config.py vivo. Antes, o state
            # sobrescrevia o config.STRATEGY_PROFILES inteiro — congelando o
            # risk_profile na primeira gravação e silenciando QUALQUER ajuste
            # de risco feito no código (ex.: RR 2.0→3.0 jamais ativou porque o
            # state colado por cima trazia o RR 2.0 antigo). Fix 2026-05-30.
            saved_strategy_profiles = state.get('strategy_profiles')
            if isinstance(saved_strategy_profiles, list) and saved_strategy_profiles:
                saved_pairs_by_name = {
                    str(p.get("name")): list(p.get("pairs", []) or [])
                    for p in saved_strategy_profiles
                    if isinstance(p, dict) and p.get("name")
                }
                if saved_pairs_by_name:
                    merged_profiles = []
                    for profile in (config.STRATEGY_PROFILES or []):
                        profile = dict(profile)
                        name = str(profile.get("name"))
                        if name in saved_pairs_by_name:
                            profile["pairs"] = saved_pairs_by_name[name]
                        merged_profiles.append(profile)
                    if hasattr(config, "_normalize_strategy_profiles"):
                        config.STRATEGY_PROFILES = config._normalize_strategy_profiles(merged_profiles)
                    else:
                        config.STRATEGY_PROFILES = merged_profiles

            config.FIXED_PAIRS = config.filter_disabled_pairs(config.FIXED_PAIRS)
            config.TRADING_PAIRS = config.filter_disabled_pairs(config.TRADING_PAIRS)
            self._sync_strategy_profiles_with_trading_pairs(reason="state-load")
            
            # Verifica se é do mesmo dia (usando UTC como a Binance)
            # A Binance reseta o P&L diário às 00:00 UTC
            saved_date = state.get('daily_date', '')
            today_utc = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            
            # Carrega os valores
            self.closed_trades_count = state.get('closed_trades_count', 0)
            self.total_pnl = state.get('total_pnl', 0.0)
            self.pnl_by_symbol = state.get('pnl_by_symbol', {})
            # `or []` blinda contra a chave presente como não-lista (ex.: {} ou
            # None) — slice em dict lançaria "unhashable type: 'slice'".
            self.trade_history = (state.get('trade_history') or [])[-500:]  # Mantém apenas os últimos 500
            self.peak_prices = state.get('peak_prices', {})
            self.trailing_activated = state.get('trailing_activated', {})
            raw_cooldowns = state.get('symbol_reentry_cooldowns', {})
            self.symbol_reentry_cooldowns = {
                str(k): float(v)
                for k, v in (raw_cooldowns.items() if isinstance(raw_cooldowns, dict) else [])
                if isinstance(v, (int, float))
            }
            self.known_positions = self.state_persistence.deserialize_known_positions(
                state.get('known_positions', {})
            )
            self.double_first_used = self._normalize_double_first_state(
                state.get('double_first_used', {})
            )
            if getattr(self, 'kill_switch', None) is not None:
                self.kill_switch.load_from_state(state.get('kill_switch', {}))
            saved_max_drawdown = state.get('max_drawdown_from_peak_percent')
            if saved_max_drawdown is not None:
                try:
                    config.MAX_DRAWDOWN_FROM_PEAK_PERCENT = max(0.0, float(saved_max_drawdown))
                except (TypeError, ValueError):
                    pass
            self.sentiment_mode_enabled = bool(
                state.get('sentiment_mode_enabled', self.sentiment_mode_enabled)
            )
            self.invert_signals = bool(
                state.get('invert_signals', getattr(self, 'invert_signals', False))
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

            # Baseline do PnL diário da Binance:
            # - Se o state foi resetado manualmente ({}), 'closed_trades_count' chega 0 aqui;
            # - Se trocou de dia UTC, daily PnL na Binance reset também (=0);
            # Em ambos os casos, queremos display em $0. Captura o snapshot atual
            # da Binance como ponto de ancoragem.
            saved_baseline_date = state.get('daily_baseline_date')
            saved_baseline_value = state.get('daily_pnl_binance_baseline')
            fresh_state = (
                saved_baseline_date != today_utc
                or saved_baseline_value is None
                or self.closed_trades_count == 0
            )
            if not fresh_state:
                self.daily_pnl_binance_baseline = float(saved_baseline_value)
                self._daily_baseline_date = saved_baseline_date
                logger.info(f"📐 Baseline diário herdado do state: ${self.daily_pnl_binance_baseline:.4f}")
            else:
                try:
                    self.daily_pnl_binance_baseline = float(
                        self.exchange.get_daily_pnl_from_binance().get('total', 0.0) or 0.0
                    )
                except Exception as exc:
                    logger.warning(f"⚠️ Falha ao ancorar baseline diário: {exc}")
                    self.daily_pnl_binance_baseline = 0.0
                self._daily_baseline_date = today_utc
                logger.info(
                    f"📐 Baseline diário ancorado: ${self.daily_pnl_binance_baseline:.4f} "
                    f"(/portfolio agora começa em $0)"
                )
            
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
            
            portfolio_history_raw = state.get('portfolio_history') or []
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

            # Histórico durável (SQLite): os arrays acima vêm do state legado
            # (vazios em saves novos, pois deixaram de ser persistidos no JSON).
            # 1) migra one-shot pro store se ele estiver vazio;
            # 2) reidrata a janela recente A PARTIR do store, que passa a ser a
            #    fonte de verdade do histórico (completo, sem o cap de 500/144).
            store = getattr(self, 'trade_store', None)
            if store is not None:
                store.migrate_from_state(self.trade_history, self.portfolio_history)
                self.trade_history = store.recent_trades(500)
                self.portfolio_history = store.recent_equity(144)
                # Ancora os contadores no SQLite (fonte de verdade). O contador
                # em memória persistido no state pode dessincronizar quando um
                # crash/kill ocorre entre o write no SQLite e o save do state —
                # daí o guard de idempotência barra o re-incremento e o off-by-one
                # vira permanente, contaminando win-rate e o gate de promoção.
                try:
                    _counters = store.closed_trade_counters()
                    self.closed_trades_count = _counters["closed"]
                    self.trades_win_count = _counters["wins"]
                    self.trades_loss_count = _counters["losses"]
                    self.total_pnl = float(store.cumulative_realized_pnl())
                except Exception:
                    logger.exception("⚠️ Falha ao ancorar contadores no SQLite — usando state")

            # Garante que todos os símbolos configurados estejam no pnl_by_symbol
            for symbol in config.TRADING_PAIRS:
                if symbol not in self.pnl_by_symbol:
                    self.pnl_by_symbol[symbol] = 0.0
            
            logger.info(f"✅ Estado carregado de {source_path}")
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

    def _desired_ws_subscriptions(self) -> set:
        """
        Conjunto de (symbol, interval) que queremos manter no WS dado o config
        atual. Inclui:
        - TIMEFRAME default (estratégia range/padrão)
        - TREND_STRONG_EXECUTION_TIMEFRAME + CONFIRM_TIMEFRAME (se trend_strong ativo)

        Normalmente reduz a 2 intervalos por par (3m + 5m).
        """
        pairs = list(config.TRADING_PAIRS or [])
        intervals = {str(getattr(config, "TIMEFRAME", "5m") or "5m")}

        # Trend strong usa 3m execução + 5m confirmação — adiciona se estratégia
        # estiver habilitada. Checagem defensiva: _strategy_engines é dict
        # preenchido em __init__.
        profiles = getattr(config, "STRATEGY_PROFILES", None) or []
        trend_strong_active = any(
            (p or {}).get("enabled", True) and (p or {}).get("name") == "trend_strong"
            for p in profiles
        ) or not profiles  # se não tem perfis, default é trend_strong

        if trend_strong_active:
            intervals.add(str(getattr(config, "TREND_STRONG_EXECUTION_TIMEFRAME", "3m") or "3m"))
            intervals.add(str(getattr(config, "TREND_STRONG_CONFIRM_TIMEFRAME", "5m") or "5m"))

        return {(sym, interval) for sym in pairs for interval in intervals if sym}

    def _sync_ws_subscriptions(self, reason: str = "sync") -> None:
        """
        Reconcilia as subscrições WS: subscribe nos que faltam, unsubscribe nos extras.

        Chamado em:
        - Startup (após setup_exchange)
        - Após update_trading_pairs / update_binance_strategy_coins
        - Após switch_environment
        """
        if not getattr(config, "WEBSOCKET_ENABLED", True):
            return

        exchange = getattr(self, "exchange", None)
        if exchange is None or not hasattr(exchange, "subscribe_klines_stream"):
            return

        desired = self._desired_ws_subscriptions()
        ws_stats = exchange.get_ws_stats() if hasattr(exchange, "get_ws_stats") else None
        if ws_stats is None:
            return  # WS desligado; nada a fazer

        current = {
            (s["symbol"], s["interval"]) for s in (ws_stats.get("streams") or [])
        }

        to_add = desired - current
        to_remove = current - desired

        if to_add or to_remove:
            logger.info(
                f"🔄 Sync WS ({reason}): +{len(to_add)} novos, -{len(to_remove)} removidos"
            )

        for symbol, interval in sorted(to_remove):
            exchange.unsubscribe_klines_stream(symbol, interval)

        for symbol, interval in sorted(to_add):
            try:
                exchange.subscribe_klines_stream(symbol, interval)
            except Exception as exc:
                logger.warning(f"⚠️ Falha ao subscribe WS {symbol}/{interval}: {exc}")

    def _check_mainnet_promotion_gate(self) -> tuple[bool, str]:
        """Bloqueia switch pra mainnet enquanto a expectativa por trade no
        testnet estiver abaixo do mínimo configurado.

        Retorna (True, "") quando os critérios estão satisfeitos, ou
        (False, mensagem) quando o switch deve ser recusado.

        Critérios:
          - closed_trades_count ≥ MAINNET_PROMOTION_MIN_TRADES
          - há wins E losses (precisa de RR estimável)
          - expectancy = WR*avg_win + (1-WR)*avg_loss ≥ MAINNET_PROMOTION_MIN_EXPECTANCY
        """
        min_trades = int(getattr(config, "MAINNET_PROMOTION_MIN_TRADES", 100))
        min_expectancy = float(getattr(config, "MAINNET_PROMOTION_MIN_EXPECTANCY", 0.10))

        closed = int(getattr(self, "closed_trades_count", 0) or 0)
        if closed < min_trades:
            return False, (
                f"❌ Promoção pra mainnet bloqueada — apenas {closed} trade(s) fechado(s) no testnet, "
                f"mínimo é {min_trades}. Continue rodando testnet até acumular amostra suficiente. "
                f"(override: TRADING_BOT_MAINNET_PROMOTION_GATE_ENABLED=0)"
            )

        win_count = int(getattr(self, "trades_win_count", 0) or 0)
        loss_count = int(getattr(self, "trades_loss_count", 0) or 0)
        win_total = float(getattr(self, "trades_win_total", 0.0) or 0.0)
        loss_total = float(getattr(self, "trades_loss_total", 0.0) or 0.0)

        if win_count <= 0 or loss_count <= 0:
            return False, (
                f"❌ Promoção pra mainnet bloqueada — sample insuficiente "
                f"(wins={win_count}, losses={loss_count}). Precisa de ambos pra estimar expectativa."
            )

        total = win_count + loss_count
        avg_win = win_total / win_count
        avg_loss = loss_total / loss_count  # já negativo
        wr = win_count / total
        expectancy = wr * avg_win + (1 - wr) * avg_loss

        if expectancy < min_expectancy:
            return False, (
                f"❌ Promoção pra mainnet bloqueada — expectativa por trade "
                f"${expectancy:+.4f} < mínimo ${min_expectancy:+.4f}.\n"
                f"   • {total} trades fechados (WR {wr:.0%})\n"
                f"   • avg win ${avg_win:+.4f}, avg loss ${avg_loss:+.4f} (RR {abs(avg_win/avg_loss):.2f})\n"
                f"Ajuste a estratégia até a expectativa virar positiva. "
                f"(override: TRADING_BOT_MAINNET_PROMOTION_GATE_ENABLED=0)"
            )

        return True, ""

    def switch_environment(self, target: str) -> tuple[bool, str]:
        """
        Troca a rede ativa entre mainnet e testnet em runtime.

        Recria a conexão Binance, salva o state atual no arquivo da rede
        anterior, recarrega (ou inicializa vazio) o state da rede nova, e
        persiste a escolha em disco para sobreviver a restart.

        Bloqueia se houver posições abertas ou credenciais faltando.
        Auto-pausa o bot — o usuário deve chamar /resume depois.

        Returns:
            (success, mensagem_para_usuario)
        """
        target_norm = str(target or "").strip().lower()
        if target_norm not in {"mainnet", "testnet"}:
            return False, f"❌ Rede inválida: {target!r}. Use 'mainnet' ou 'testnet'."

        current = config.ENVIRONMENT
        if target_norm == current:
            return False, f"ℹ️ Bot já está em {current.upper()}."

        if not config.has_credentials_for(target_norm):
            prefix = target_norm.upper()
            return False, (
                f"❌ Credenciais para {prefix} não configuradas. "
                f"Defina BINANCE_{prefix}_API_KEY e BINANCE_{prefix}_API_SECRET no .env e reinicie."
            )

        # Gate de promoção pra mainnet — exige expectativa positiva no testnet
        # antes de subir. Bloqueia o erro clássico de promover estratégia com
        # WR alto mas RR invertido. Override por env: MAINNET_PROMOTION_GATE_ENABLED=0.
        if target_norm == "mainnet" and getattr(config, "MAINNET_PROMOTION_GATE_ENABLED", False):
            ok, message = self._check_mainnet_promotion_gate()
            if not ok:
                return False, message

        with self._positions_lock:
            open_symbols = [sym for sym, pos in self.positions.items() if pos]
        if open_symbols:
            return False, (
                f"❌ Troca bloqueada: {len(open_symbols)} posição(ões) aberta(s) em {current.upper()}: "
                f"{', '.join(open_symbols[:5])}{' ...' if len(open_symbols) > 5 else ''}. "
                f"Feche com /closeall antes de trocar de rede."
            )

        logger.warning(f"🔄 Trocando rede Binance: {current.upper()} → {target_norm.upper()}")

        # Auto-pausa para evitar ticks mid-switch
        was_paused = bool(self.paused)
        self.paused = True

        try:
            # 1) Salva estado da rede atual
            self.save_state()

            # 2) Libera lock da rede atual
            self._release_instance_lock()

            # 3) Atualiza config + persiste escolha
            config.ENVIRONMENT = target_norm
            config.persist_active_environment()

            # 4) Recalcula paths dependentes da rede
            env_suffix = f"{config.APP_ENV}.{config.ENVIRONMENT}"
            runtime_dir = Path(config.RUNTIME_DIR)
            config.STATE_FILE_NAME = f"bot_state.{env_suffix}.json"
            config.LOCK_FILE_NAME = f"trading_bot.{env_suffix}.lock"
            config.TRADE_DB_NAME = f"trades.{env_suffix}.db"
            config.STATE_FILE_PATH = str(runtime_dir / config.STATE_FILE_NAME)
            config.LOCK_FILE_PATH = str(runtime_dir / config.LOCK_FILE_NAME)
            config.TRADE_DB_PATH = str(runtime_dir / config.TRADE_DB_NAME)
            self._state_file_path = config.STATE_FILE_PATH
            self._instance_lock_path = config.LOCK_FILE_PATH

            # 5) Reset runtime state + reconecta exchange
            self._init_runtime_state()
            self.paused = True  # preserva pausa após reset
            self.exchange = BinanceConnection()
            self.risk_manager._real_daily_pnl_fn = (
                lambda: self.exchange.get_daily_pnl_from_binance().get('total', 0.0)
            )

            # 6) Re-adquire lock na nova rede
            if not self._acquire_instance_lock():
                return False, (
                    f"⚠️ Rede trocada para {target_norm.upper()}, mas falha ao adquirir lock. "
                    f"Outra instância rodando nessa rede? Verifique {config.LOCK_FILE_NAME}."
                )

            # 7) Reset total no novo ambiente — renomeia o state file anterior
            #    pra backup timestampado e pula load_state. O bot começa do zero
            #    na nova rede (stats, trades, known_positions, tudo limpo em
            #    memória). Posições reais continuam na Binance; este reset é
            #    só do tracking local.
            backup_note = self._archive_state_file_for_reset(self._state_file_path)
            for symbol in config.TRADING_PAIRS:
                self.pnl_by_symbol.setdefault(symbol, 0.0)

            # 8) Atualiza label da rede no Prometheus
            metrics.set_bot_info(environment=config.ENVIRONMENT, app_env=config.APP_ENV)

            # 9) Re-assina streams WS na nova rede
            self._sync_ws_subscriptions(reason="switch-environment")

            logger.warning(f"✅ Rede ativa agora: {target_norm.upper()} (estado zerado)")

            backup_line = (
                f"• Estado anterior: <code>{backup_note}</code>\n"
                if backup_note else
                ""
            )
            return True, (
                f"✅ Rede trocada com sucesso!\n"
                f"• Anterior: <b>{current.upper()}</b>\n"
                f"• Atual: <b>{target_norm.upper()}</b>\n"
                f"• State file: <code>{config.STATE_FILE_NAME}</code>\n"
                f"• <b>Estado zerado</b> — trades, stats e P&L começam do zero.\n"
                f"{backup_line}"
                f"\n⚠️ Bot está pausado. Use /resume quando quiser voltar a operar."
            )
        except Exception as exc:
            logger.exception(f"❌ Falha crítica ao trocar rede: {exc}")
            # Restaura pausa original se falha ocorreu antes de aplicar mudanças
            self.paused = was_paused
            return False, (
                f"❌ Erro ao trocar rede: {exc}\n"
                f"Estado pode estar inconsistente — verifique os logs e considere reiniciar o bot."
            )

    def _normalize_double_first_state(self, raw_state) -> Dict[str, bool]:
        """Backward-compat: delegate à DoubleFirstPolicy."""
        return DoubleFirstPolicy.normalize_state(raw_state)

    def _set_known_position(self, position_key: str, payload: Dict[str, Any]):
        """Backward-compat: delegate ao PositionTracker."""
        self.positions.set(position_key, payload)

    def _remove_known_position(self, position_key: str):
        """Backward-compat: delegate ao PositionTracker."""
        self.positions.remove(position_key)

    def _get_known_position(self, position_key: str) -> Dict[str, Any]:
        """Backward-compat: delegate ao PositionTracker."""
        return self.positions.get(position_key)

    def _double_first_scope(self) -> str:
        """Backward-compat: delegate à DoubleFirstPolicy."""
        return self.double_first_policy.scope()

    @staticmethod
    def _normalize_position_side(side: str) -> str:
        return "SHORT" if str(side).upper() == "SHORT" else "LONG"

    def _is_double_first_enabled(self, side: str) -> bool:
        """Backward-compat: delegate à DoubleFirstPolicy."""
        return self.double_first_policy.is_enabled(side)

    def _double_first_state_key(self, symbol: str, side: str) -> str:
        """Backward-compat: delegate à DoubleFirstPolicy."""
        return self.double_first_policy.state_key(symbol, side)

    def _apply_double_first_order_size(
        self, symbol: str, side: str, order_size: float
    ) -> Tuple[float, bool, str]:
        """Backward-compat: delegate à DoubleFirstPolicy.try_double()."""
        return self.double_first_policy.try_double(symbol, side, order_size)

    def _mark_double_first_used(
        self,
        state_key: str,
        symbol: str,
        side: str,
        base_order_size: float,
        applied_order_size: float,
    ) -> None:
        """Backward-compat: delegate à DoubleFirstPolicy.mark_used()."""
        self.double_first_policy.mark_used(
            state_key, symbol, side, base_order_size, applied_order_size
        )
    
    def _signal_handler(self, _signum, _frame):
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

    def _get_primary_profile_info(self) -> Tuple[list, dict, bool]:
        """
        Retorna (enabled_profiles, primary_profile, primary_is_dynamic).
        primary_is_dynamic é True quando o perfil usa auto-seleção (max_pairs > 0)
        ou quando ainda não tem pares atribuídos. Perfis com max_pairs configurado
        são sempre dinâmicos, mesmo depois que os pares foram preenchidos em runtime.
        """
        enabled_profiles = config.get_enabled_strategy_profiles() or []
        primary_profile = enabled_profiles[0] if enabled_profiles else {}
        # Considera dinâmico se max_pairs > 0 (auto-seleção Binance) OU se não há
        # pares fixos configurados. Isso garante que desabilitar pares acione
        # nova seleção em vez de apenas filtrar a lista existente.
        primary_is_dynamic = bool(primary_profile.get("max_pairs", 0)) or not bool(primary_profile.get("pairs"))

        # Override de pares fixos: pina EXATAMENTE FIXED_PRIMARY_PAIRS e desliga a
        # seleção dinâmica, ANTES da migração que força dinâmico no modo Binance.
        # Mantém o sizing por tier; só fecha o universo (estratégia de pares fixos).
        fixed_primary = list(getattr(config, "FIXED_PRIMARY_PAIRS", []) or [])
        if fixed_primary and primary_profile:
            primary_profile["pairs"] = list(fixed_primary)
            primary_profile.pop("max_pairs", None)
            return enabled_profiles, primary_profile, False

        if (
            config.USE_BINANCE_STRATEGY
            and self._normalize_strategy_type(primary_profile.get("strategy_type", "trend_signal")) == "trend_signal"
        ):
            # Migração de runtime: estados antigos podem persistir o perfil primário
            # com pares preenchidos e sem max_pairs, o que o tornava "fixo" por engano.
            # Em modo Binance, o primário trend_signal deve seguir seleção dinâmica.
            primary_is_dynamic = True
        return enabled_profiles, primary_profile, primary_is_dynamic

    @staticmethod
    def _resolve_primary_pair_target(
        primary_profile: Dict[str, Any],
        strategy_num_coins: Any,
        fallback_num_coins: Any = 0,
    ) -> int:
        """
        Resolve quantos pares o perfil primário deve usar.

        Regra:
        - tier da Binance (strategy_num_coins) é a base;
        - max_pairs (quando definido) atua como teto, não como override.
        """
        try:
            tier_target = int(strategy_num_coins or 0)
        except (TypeError, ValueError):
            tier_target = 0

        if tier_target <= 0:
            try:
                tier_target = int(fallback_num_coins or 0)
            except (TypeError, ValueError):
                tier_target = 0

        try:
            max_pairs = int((primary_profile or {}).get("max_pairs") or 0)
        except (TypeError, ValueError):
            max_pairs = 0

        if max_pairs > 0 and tier_target > 0:
            return min(max_pairs, tier_target)
        if max_pairs > 0:
            return max_pairs
        return max(0, tier_target)

    @staticmethod
    def _get_reserved_pairs(enabled_profiles: list) -> set:
        """Retorna o conjunto de pares fixos dos perfis secundários."""
        reserved: set = set()
        for p in enabled_profiles[1:]:
            if p.get("pairs"):
                reserved.update(p["pairs"])
        return reserved

    @staticmethod
    def _normalize_strategy_entry_mode(entry_mode: str) -> str:
        """Normaliza modo de entrada por estratégia."""
        token = str(entry_mode or "").strip().lower()
        if token in {"standard", "normal", "full"}:
            return "standard"
        return "strong_only"

    @staticmethod
    def _normalize_strategy_type(strategy_type: str) -> str:
        """Normaliza o tipo de estratégia do perfil."""
        token = str(strategy_type or "").strip().lower()
        if token in {"range_scalping", "range", "scalping", "range_scalp"}:
            return "range_scalping"
        return "trend_signal"

    def _create_strategy_engine(self, strategy_type: str):
        """Cria a instância da estratégia de acordo com o tipo."""
        normalized = self._normalize_strategy_type(strategy_type)
        if normalized == "range_scalping":
            return RangeScalpingStrategy()
        return HedgeStrategy()

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
            strategy_type = self._normalize_strategy_type(raw_profile.get("strategy_type", "trend_signal"))
            entry_mode = self._normalize_strategy_entry_mode(raw_profile.get("entry_mode", "strong_only"))
            pairs = self._filter_disabled_pairs(raw_profile.get("pairs", []))
            raw_risk_profile = raw_profile.get("risk_profile")
            risk_profile = {}
            if strategy_type == "trend_signal" and raw_risk_profile is not None:
                if hasattr(config, "_normalize_trend_risk_profile"):
                    risk_profile = config._normalize_trend_risk_profile(raw_risk_profile)
                elif isinstance(raw_risk_profile, dict):
                    risk_profile = dict(raw_risk_profile)

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
                strategy_engine = self._create_strategy_engine(strategy_type)
            else:
                current_type = "range_scalping" if isinstance(strategy_engine, RangeScalpingStrategy) else "trend_signal"
                if current_type != strategy_type:
                    strategy_engine = self._create_strategy_engine(strategy_type)

            runtime_profile = {
                "name": profile_name,
                "strategy_type": strategy_type,
                "entry_mode": entry_mode,
                "pairs": unique_pairs,
                "strategy": strategy_engine,
            }
            if risk_profile:
                runtime_profile["risk_profile"] = risk_profile
            max_pairs_val = raw_profile.get("max_pairs")
            if max_pairs_val:
                runtime_profile["max_pairs"] = int(max_pairs_val)
            runtime_profiles.append(runtime_profile)

        if not runtime_profiles:
            fallback_strategy = previous_engines.get("primary") or getattr(self, "strategy", None) or HedgeStrategy()
            runtime_profiles = [
                {
                    "name": "primary",
                    "strategy_type": "trend_signal",
                    "entry_mode": "strong_only",
                    "pairs": self._filter_disabled_pairs(getattr(config, "TRADING_PAIRS", []) or []),
                    "strategy": fallback_strategy,
                }
            ]

        # Mantém pares legados no profile primário quando surgirem fora dos profiles,
        # mas apenas se o primário não tem pares fixos configurados (modo dinâmico).
        primary_runtime_has_pairs = bool(runtime_profiles[0]["pairs"]) if runtime_profiles else False
        if not primary_runtime_has_pairs:
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
        config_profiles = []
        for profile in runtime_profiles:
            serialized = {
                "name": profile["name"],
                "enabled": True,
                "strategy_type": profile["strategy_type"],
                "entry_mode": profile["entry_mode"],
                "pairs": list(profile["pairs"]),
            }
            if profile.get("risk_profile"):
                serialized["risk_profile"] = dict(profile["risk_profile"])
            if profile.get("max_pairs"):
                serialized["max_pairs"] = int(profile["max_pairs"])
            config_profiles.append(serialized)
        config.STRATEGY_PROFILES = config_profiles

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
            raw_profiles = [
                {
                    "name": "primary",
                    "enabled": True,
                    "strategy_type": "trend_signal",
                    "entry_mode": "strong_only",
                    "pairs": [],
                }
            ]

        normalized_profiles: List[dict] = []
        for index, raw_profile in enumerate(raw_profiles, start=1):
            if isinstance(raw_profile, dict):
                profile = dict(raw_profile)
            else:
                profile = {}
            profile.setdefault("name", f"strategy_{index}")
            profile.setdefault("enabled", True)
            profile.setdefault("strategy_type", "trend_signal")
            profile.setdefault("entry_mode", "strong_only")
            profile.setdefault("pairs", [])
            normalized_profiles.append(profile)

        primary_index = next(
            (idx for idx, profile in enumerate(normalized_profiles) if bool(profile.get("enabled", True))),
            0,
        )
        if not normalized_profiles:
            normalized_profiles = [
                {
                    "name": "primary",
                    "enabled": True,
                    "strategy_type": "trend_signal",
                    "entry_mode": "strong_only",
                    "pairs": [],
                }
            ]
            primary_index = 0

        existing_primary_pairs = self._filter_disabled_pairs(
            normalized_profiles[primary_index].get("pairs", [])
        )
        # Perfil com max_pairs > 0 é dinâmico — pares foram atribuídos em runtime,
        # não são fixos de config. Não deve bloquear nova seleção dinâmica.
        profile_is_dynamic = bool(normalized_profiles[primary_index].get("max_pairs", 0))
        if (
            not profile_is_dynamic
            and config.USE_BINANCE_STRATEGY
            and self._normalize_strategy_type(
                normalized_profiles[primary_index].get("strategy_type", "trend_signal")
            ) == "trend_signal"
        ):
            # Compatibilidade com estado legado: perfil primário salvo com pares
            # preenchidos e sem max_pairs deve continuar dinâmico em modo Binance.
            profile_is_dynamic = True

        # Override de pares fixos: pina o primário e impede a união de pares
        # dinâmicos legados (config.TRADING_PAIRS) — senão o universo "fixo"
        # vazava pares da seleção anterior (ZEC/HYPE/TRUMP) a cada sync.
        fixed_primary = self._filter_disabled_pairs(
            list(getattr(config, "FIXED_PRIMARY_PAIRS", []) or [])
        )
        if fixed_primary:
            normalized_profiles[primary_index]["pairs"] = list(fixed_primary)
            normalized_profiles[primary_index].pop("max_pairs", None)
            profile_is_dynamic = False
            existing_primary_pairs = list(fixed_primary)

        primary_has_fixed_pairs = bool(existing_primary_pairs) and not profile_is_dynamic

        if primary_pairs is not None and not primary_has_fixed_pairs:
            # Perfil primário sem pares fixos — usa a seleção dinâmica externa.
            reserved_pairs = set()
            for idx, profile in enumerate(normalized_profiles):
                if idx == primary_index:
                    continue
                if not bool(profile.get("enabled", True)):
                    continue
                reserved_pairs.update(self._filter_disabled_pairs(profile.get("pairs", [])))

            candidate_primary_pairs = self._filter_disabled_pairs(primary_pairs)
            normalized_profiles[primary_index]["pairs"] = [
                symbol for symbol in candidate_primary_pairs if symbol not in reserved_pairs
            ]

        # Se TRADING_PAIRS contém pares fora dos profiles, injeta no primário
        # apenas quando não há pares fixos configurados (modo dinâmico).
        if not primary_has_fixed_pairs:
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

        # Atualiza TRADING_PAIRS para refletir todos os pares dos perfis ativos,
        # garantindo que alavancagem e monitoramento cubram todos os pares configurados.
        all_profile_pairs: List[str] = []
        seen_profile_pairs: set = set()
        for profile in normalized_profiles:
            if not bool(profile.get("enabled", True)):
                continue
            for symbol in self._filter_disabled_pairs(profile.get("pairs", [])):
                if symbol not in seen_profile_pairs:
                    seen_profile_pairs.add(symbol)
                    all_profile_pairs.append(symbol)
        if all_profile_pairs:
            config.TRADING_PAIRS = all_profile_pairs

        # Garante que streams WebSocket acompanham TRADING_PAIRS em toda mudança.
        # Idempotente — só faz delta (add/remove) quando há diferença real.
        self._sync_ws_subscriptions(reason=f"profiles-sync:{reason}")

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

    def _get_or_create_regime_engine(self, strategy_type: str):
        """Engine reutilizável usado quando o classifier sobrepõe a estratégia."""
        normalized = self._normalize_strategy_type(strategy_type)
        cached = self._regime_engine_cache.get(normalized)
        if cached is not None:
            return cached
        if normalized == "range_scalping":
            engine = RangeScalpingStrategy()
        else:
            engine = HedgeStrategy()
        self._regime_engine_cache[normalized] = engine
        return engine

    def _classify_symbol_regime(self, klines: List[Dict]) -> Dict[str, Any]:
        """Wrapper sobre TechnicalAnalysis.classify_regime usando klines do tick."""
        try:
            highs = [float(k["high"]) for k in klines]
            lows = [float(k["low"]) for k in klines]
            closes = [float(k["close"]) for k in klines]
        except (KeyError, TypeError, ValueError):
            return {"regime": "neutral", "adx": 0.0, "bbw_percent": 0.0, "reason": "klines inválidos"}
        return TechnicalAnalysis.classify_regime(highs, lows, closes)

    def _update_regime_history(self, symbol: str, observation: str) -> Optional[str]:
        """
        Adiciona uma observação à janela rolante do símbolo e tenta comitar.

        Hysteresis: precisa de N observações IGUAIS consecutivas (N =
        REGIME_HYSTERESIS_TICKS) para trocar o regime comitado. Observações
        "neutral" não substituem o regime atual — ficam fora da contagem
        (mantêm o status quo até um sinal claro aparecer).

        Retorna o regime comitado APÓS esta observação (pode ser igual ao
        anterior se hysteresis ainda não bateu).
        """
        if observation not in ("trend", "range", "squeeze", "neutral"):
            observation = "neutral"

        window_size = max(1, int(getattr(config, "REGIME_HYSTERESIS_TICKS", 3)))
        current = self._regime_committed.get(symbol)

        # "neutral" não vota: limpa a janela mas mantém o regime comitado.
        if observation == "neutral":
            self._regime_observations[symbol] = []
            return current

        history = self._regime_observations.setdefault(symbol, [])
        history.append(observation)
        if len(history) > window_size:
            del history[: len(history) - window_size]

        if len(history) >= window_size and all(h == observation for h in history):
            if current != observation:
                logger.info(
                    f"🌀 Regime de {symbol}: {current or '∅'} → {observation} "
                    f"(hysteresis {window_size} ticks)"
                )
                if getattr(self, "dashboard_server", None):
                    self.dashboard_server.emit_regime_changed({
                        "symbol": symbol,
                        "regime": observation,
                        "previous": current,
                    })
            self._regime_committed[symbol] = observation
            return observation

        return current

    def _strategy_type_for_regime(self, regime: Optional[str]) -> Optional[str]:
        """Mapeia o regime comitado para o strategy_type. None = sem override."""
        if regime == "trend":
            return "trend_signal"
        if regime == "range":
            return "range_scalping"
        # "squeeze" e "neutral": não força override (squeeze é tratado abaixo
        # bloqueando entradas de range; deixa a estratégia estática rodar
        # mas tipicamente sem entrar).
        return None

    def _resolve_strategy_context(
        self,
        symbol: str,
        strategy_name: str | None = None,
        regime_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Resolve engine + parâmetros do perfil para um símbolo.

        `regime_override`, quando passado, sobrescreve o `strategy_type` do
        profile e troca o engine pelo correspondente (cache em
        `_regime_engine_cache`). Outros campos do profile (entry_mode,
        risk_profile, pairs) são preservados.
        """
        profiles = list(getattr(self, "strategy_profiles", []) or [])
        if not profiles:
            fallback_strategy = getattr(self, "strategy", None)
            if fallback_strategy is not None:
                base = {
                    "name": str(strategy_name or "primary"),
                    "strategy_type": "trend_signal",
                    "entry_mode": "strong_only",
                    "pairs": [str(symbol).upper()],
                    "strategy": fallback_strategy,
                }
                return self._apply_regime_override(base, regime_override)
            self._reload_strategy_profiles(reason="analysis-resolve")
            profiles = list(getattr(self, "strategy_profiles", []) or [])

        if strategy_name:
            for profile in profiles:
                if str(profile.get("name")) == str(strategy_name):
                    return self._apply_regime_override(profile, regime_override)

        normalized_symbol = str(symbol).upper()
        for profile in profiles:
            if normalized_symbol in set(profile.get("pairs", [])):
                return self._apply_regime_override(profile, regime_override)

        # Símbolo veio da seleção dinâmica (USE_BINANCE_STRATEGY) e não bate
        # com `pairs` de nenhum perfil. Em vez de construir um contexto vazio
        # — que descartaria `risk_profile` e jogaria o cálculo de SL/TP no
        # global STOP_LOSS_PERCENT/TAKE_PROFIT_PERCENT — herdamos do primeiro
        # perfil habilitado de mesmo strategy_type. Mantém o `risk_profile`
        # vivo pra pares dinâmicos.
        first_trend_profile = next(
            (p for p in profiles if self._normalize_strategy_type(p.get("strategy_type", "trend_signal")) == "trend_signal"),
            None,
        )
        if first_trend_profile is not None:
            inherited = dict(first_trend_profile)
            inherited["pairs"] = [normalized_symbol]
            return self._apply_regime_override(inherited, regime_override)

        fallback_strategy = getattr(self, "strategy", None) or HedgeStrategy()
        if not hasattr(self, "strategy"):
            self.strategy = fallback_strategy
        base = {
            "name": str(strategy_name or "primary"),
            "strategy_type": "trend_signal",
            "entry_mode": "strong_only",
            "pairs": [normalized_symbol],
            "strategy": fallback_strategy,
        }
        return self._apply_regime_override(base, regime_override)

    def _apply_regime_override(
        self, profile: Dict[str, Any], regime_override: Optional[str]
    ) -> Dict[str, Any]:
        """Sobrescreve strategy_type + engine no profile quando regime_override é dado."""
        if not regime_override:
            return profile
        target_strategy_type = self._strategy_type_for_regime(regime_override)
        if not target_strategy_type:
            return profile
        current_type = self._normalize_strategy_type(profile.get("strategy_type", "trend_signal"))
        if current_type == target_strategy_type:
            return profile
        engine = self._get_or_create_regime_engine(target_strategy_type)
        overridden = dict(profile)
        overridden["strategy_type"] = target_strategy_type
        overridden["strategy"] = engine
        overridden["regime_override_applied"] = regime_override
        return overridden

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

        # Whitelist do universo: restringe o scoring a uma lista curada (ex.: as
        # moedas do PDF), preservando a rotação por score dentro dela. ∩ com os
        # pares tradáveis da exchange evita selecionar símbolo inexistente/delistado.
        whitelist = list(getattr(config, "BINANCE_UNIVERSE_WHITELIST", []) or [])
        if whitelist and normalized_fresh:
            whitelist_set = set(whitelist)
            filtered = [p for p in normalized_fresh if p in whitelist_set]
            if filtered:
                normalized_fresh = filtered
            else:
                logger.warning(
                    "⚠️ Whitelist de universo não intersecta nenhum par tradável — "
                    "ignorando whitelist neste ciclo (usando universo completo)."
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
            enabled_profiles, primary_profile, primary_is_dynamic = self._get_primary_profile_info()

            strategy_num = (
                self.binance_strategy.get("num_coins")
                if hasattr(self, "binance_strategy") and self.binance_strategy
                else 0
            )
            fallback_num = len(old_pairs) or 0
            num_coins = self._resolve_primary_pair_target(
                primary_profile=primary_profile,
                strategy_num_coins=strategy_num,
                fallback_num_coins=fallback_num,
            )

            # Re-seleciona se: (a) perfil é dinâmico, ou (b) pares ativos ficaram
            # abaixo do alvo após desabilitar — garante mínimo de pares sempre preenchido.
            current_active = len(self._filter_disabled_pairs(list(primary_profile.get("pairs", []))))
            should_refill = primary_is_dynamic or (num_coins > 0 and current_active < num_coins)

            if should_refill:
                new_pairs = self.sort_binance_coins_by_score(
                    num_coins=max(0, num_coins),
                    exclude=self._get_reserved_pairs(enabled_profiles),
                )
            else:
                # Perfil com pares fixos e sem déficit — apenas filtra desabilitados
                new_pairs = self._filter_disabled_pairs(list(primary_profile.get("pairs", [])))

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
    
    def _maybe_swap_non_trend_pairs(self) -> Optional[Dict[str, Any]]:
        """Troca pares OCIOSOS em regime non-trend por melhores candidatos (abordagem A).

        Quando um par do perfil primário commita regime non-trend (squeeze/range/
        neutral) e NÃO tem posição aberta, o slot é despejado e preenchido pelo
        melhor par do universo por score que não esteja ativo nem em cooldown —
        sem esperar o rescore horário. Mantém intactos os pares em trend e os com
        posição aberta. Cooldown por símbolo evita carrossel. Retorna dict do swap
        ou None se nada mudou.
        """
        if not bool(getattr(config, "REGIME_SWAP_ENABLED", False)):
            return None
        if not bool(getattr(config, "USE_BINANCE_STRATEGY", False)):
            return None
        try:
            enabled_profiles, primary_profile, _is_dynamic = self._get_primary_profile_info()
        except Exception:
            return None
        if not primary_profile:
            return None

        active = self._filter_disabled_pairs(list(primary_profile.get("pairs", []) or []))
        if not active:
            return None

        non_trend = ("squeeze", "range", "neutral")
        now = time.time()
        cooldown_s = float(getattr(config, "REGIME_SWAP_COOLDOWN_MINUTES", 0.0) or 0.0) * 60.0

        def _in_cooldown(sym: str) -> bool:
            return cooldown_s > 0 and (now - self._regime_swap_cooldowns.get(sym, 0.0)) < cooldown_s

        def _has_position(sym: str) -> bool:
            return any(self._get_known_position(f"{sym}_{s}") for s in ("LONG", "SHORT"))

        # Despeja só os OCIOSOS com regime non-trend COMITADO (a hysteresis de 3
        # ticks já é o "3x"; par recém-adicionado ainda sem commit não é elegível).
        evict = [
            sym for sym in active
            if self._regime_committed.get(sym) in non_trend
            and not _has_position(sym)
            and not _in_cooldown(sym)
        ]
        if not evict:
            return None

        keep = [sym for sym in active if sym not in evict]
        reserved: set = set()
        try:
            reserved = set(self._get_reserved_pairs(enabled_profiles))
        except Exception:
            reserved = set()
        cooldown_syms = {s for s in self._regime_swap_cooldowns if _in_cooldown(s)}
        exclude = set(active) | reserved | cooldown_syms

        try:
            ranked = self.sort_binance_coins_by_score(
                num_coins=len(active) + len(evict) + 5,
                exclude=exclude,
            )
        except Exception as exc:
            logger.warning(f"⚠️ regime-swap: falha ao ranquear candidatos: {exc}")
            return None

        replacements = [s for s in ranked if s not in active][:len(evict)]
        if not replacements:
            logger.info(
                "🔁 regime-swap: %s em regime non-trend, sem candidato disponível — mantendo.",
                ", ".join(evict),
            )
            # Cooldown nos evicts pra não re-ranquear todo ciclo sem candidato.
            for sym in evict:
                self._regime_swap_cooldowns[sym] = now
            return None

        evicted_done = evict[:len(replacements)]
        evicted_kept = evict[len(replacements):]  # sem candidato suficiente → ficam
        new_pairs = keep + evicted_kept + replacements

        config.TRADING_PAIRS = self._filter_disabled_pairs(new_pairs)
        if hasattr(self, "binance_strategy") and self.binance_strategy is not None:
            self.binance_strategy["coins"] = list(config.TRADING_PAIRS)
        self._sync_strategy_profiles_with_trading_pairs(
            reason="regime-swap", primary_pairs=config.TRADING_PAIRS,
        )

        for sym in replacements:
            try:
                self.exchange.set_leverage(sym, config.LEVERAGE)
            except Exception:
                pass
            self.pnl_by_symbol.setdefault(sym, 0.0)

        # Cooldown nos envolvidos (despejados + entrantes) pra evitar carrossel.
        for sym in evict + replacements:
            self._regime_swap_cooldowns[sym] = now

        logger.info(
            "🔁 regime-swap: %s (non-trend) → %s",
            ", ".join(evicted_done),
            ", ".join(replacements),
        )
        # Notificação no Telegram desligada por padrão — a rotação por regime
        # acontece de hora em hora e poluía o chat. A troca continua (e fica no
        # log); só não avisa. Religar com TRADING_BOT_REGIME_SWAP_NOTIFY=true.
        if getattr(config, "REGIME_SWAP_NOTIFY", False) and getattr(self, "telegram", None):
            try:
                self.telegram.send_message(
                    "🔁 <b>ROTAÇÃO POR REGIME</b>\n\n"
                    f"♻️ Trocados (non-trend): <code>{', '.join(s.replace('USDT','') for s in evicted_done)}</code>\n"
                    f"🆕 Entraram: <code>{', '.join(s.replace('USDT','') for s in replacements)}</code>"
                )
            except Exception:
                pass

        return {"evicted": evicted_done, "added": replacements, "kept": keep + evicted_kept}

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

        self._reload_strategy_profiles(reason="setup-start")
        
        # Define alavancagem para cada par
        for symbol in config.TRADING_PAIRS:
            self.exchange.set_leverage(symbol, config.LEVERAGE)

        # Nota: sync WS é chamado automaticamente em _sync_strategy_profiles_with_trading_pairs,
        # que roda após cada mutação de TRADING_PAIRS (inclusive seleção Binance abaixo).

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
        
        trade_value = current_balance * config.MAX_POSITION_PERCENT
        logger.info("📊 Sistema de capital flexível:")
        logger.info(f"   • Saldo atual: ${current_balance:.2f}")
        logger.info(f"   • Por trade: {config.MAX_POSITION_PERCENT * 100:.0f}% = ${trade_value:.2f} (ou mínimo da moeda)")
        logger.info(f"   • Alavancagem: {config.LEVERAGE}x")

        if config.USE_BINANCE_STRATEGY or config.AUTO_SELECT_PAIRS:
            self._refresh_binance_coin_universe(trigger_reason="setup")
        
        # ESTRATÉGIA BINANCE PADRÃO (por faixa de capital)
        if config.USE_BINANCE_STRATEGY:
            logger.info("📊 Usando ESTRATÉGIA BINANCE PADRÃO...")
            strategy = config.get_binance_strategy_for_capital(current_balance)

            # Determina quantos pares selecionar para o perfil primário dinâmico.
            # Usa "max_pairs" do perfil quando definido; caso contrário, usa o tier.
            enabled_profiles, primary_profile, primary_is_dynamic = self._get_primary_profile_info()
            num_primary_pairs = self._resolve_primary_pair_target(
                primary_profile=primary_profile,
                strategy_num_coins=strategy['num_coins'],
                fallback_num_coins=len(config.TRADING_PAIRS),
            )

            if primary_is_dynamic:
                # Perfil primário em modo automático: seleciona top N por score,
                # excluindo pares reservados pelos perfis secundários fixos.
                logger.info(f"🔄 Selecionando top {num_primary_pairs} pares por score...")
                if not hasattr(self, "pair_selector") or self.pair_selector is None:
                    self.pair_selector = PairSelector(self.exchange, config)
                reserved = self._get_reserved_pairs(enabled_profiles)
                sorted_coins = self.sort_binance_coins_by_score(num_primary_pairs, exclude=reserved)
                strategy['coins'] = sorted_coins
                config.TRADING_PAIRS = self._filter_disabled_pairs(sorted_coins)
                self.binance_strategy = strategy
                self._sync_strategy_profiles_with_trading_pairs(
                    reason="setup-binance",
                    primary_pairs=config.TRADING_PAIRS,
                )
            else:
                # Todos os perfis têm pares fixos: pula seleção dinâmica por score.
                logger.info("📌 Pares fixos detectados — ignorando seleção dinâmica por score.")
                strategy['coins'] = []
                self.binance_strategy = strategy
                self._sync_strategy_profiles_with_trading_pairs(reason="setup-binance")

            # Atualiza pnl_by_symbol para incluir os pares
            for symbol in config.TRADING_PAIRS:
                if symbol not in self.pnl_by_symbol:
                    self.pnl_by_symbol[symbol] = 0.0

            # Define alavancagem para os pares
            for symbol in config.TRADING_PAIRS:
                self.exchange.set_leverage(symbol, config.LEVERAGE)

            logger.info(f"   📈 Faixa de Capital: {strategy['capital_range']}")
            logger.info(f"   💵 Order Size: ${strategy['order_size']}")
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
                f"🪙 <b>Moedas ({len(config.TRADING_PAIRS)}):</b>\n{coins_display}\n\n"
                f"<i>Atualização a cada {_format_pair_interval(config.PAIR_UPDATE_INTERVAL_MINUTES)}</i>"
            )
        
        # SELEÇÃO INTELIGENTE DE PARES (só se não usar estratégia Binance)
        elif config.AUTO_SELECT_PAIRS:
            logger.info("🤖 Iniciando seleção inteligente de pares...")
            self.pair_selector = PairSelector(self.exchange, config)
            
            # Seleciona os melhores pares baseado no capital disponível
            selected_pairs, _scores = self.pair_selector.select_best_pairs(
                available_capital=current_balance
            )
            
            config.TRADING_PAIRS = self._filter_disabled_pairs(selected_pairs)
            self._sync_strategy_profiles_with_trading_pairs(
                reason="setup-auto-select",
                primary_pairs=config.TRADING_PAIRS,
            )
            
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
                f"<i>Próxima atualização em {_format_pair_interval(config.PAIR_UPDATE_INTERVAL_MINUTES)}</i>"
            )
        
        if not config.USE_BINANCE_STRATEGY and not config.AUTO_SELECT_PAIRS:
            self._sync_strategy_profiles_with_trading_pairs(reason="setup-static")

        logger.info(f"📋 Pares finais configurados: {len(config.TRADING_PAIRS)}")
        for symbol in config.TRADING_PAIRS:
            logger.info(f"   • {symbol}")
        
        logger.info("✅ Exchange configurada!")
        
        # RECONCILIAÇÃO DE POSIÇÕES (state ↔ exchange)
        # Preserva metadata do state (custom_tp/sl, strategy, range) ao cruzar
        # com posições abertas na Binance. Drop entries do state sem contraparte
        # (foram fechadas enquanto bot estava off); cria entries novas com
        # defaults + warn quando Binance tem posição sem metadata no state.
        try:
            existing_positions = self.exchange.get_open_positions(force_refresh=True)
        except Exception as exc:
            logger.warning(
                f"⚠️ API falhou ao carregar posições existentes no setup: {exc}. "
                "Mantendo known_positions do state; será reconciliado no primeiro monitor tick."
            )
            existing_positions = []
            # Sem API, não tem como reconciliar com segurança; sai sem mexer.
            self._sync_ws_subscriptions(reason="setup-exchange-final")
            return True

        api_keys: set = set()
        positions_without_metadata: List[str] = []
        for pos in existing_positions:
            position_key = f"{pos['symbol']}_{pos['side']}"
            api_keys.add(position_key)
            state_entry = self.known_positions.get(position_key) or {}
            had_metadata = bool(
                state_entry.get('custom_take_profit') is not None
                or state_entry.get('custom_stop_loss') is not None
                or state_entry.get('range_mid_price') is not None
            )
            # Preserva metadata estratégica do state; só atualiza campos
            # voláteis (qty, preço, last_seen).
            self.known_positions[position_key] = {
                'symbol': pos['symbol'],
                'side': pos['side'],
                'entry_price': pos['entry_price'],
                'quantity': pos['quantity'],
                'last_seen': datetime.now(),
                'strategy_name': state_entry.get('strategy_name', 'primary'),
                'strategy_type': state_entry.get('strategy_type', 'trend_signal'),
                'custom_stop_loss': state_entry.get('custom_stop_loss'),
                'custom_take_profit': state_entry.get('custom_take_profit'),
                'range_mid_price': state_entry.get('range_mid_price'),
                'range_entry_side': state_entry.get('range_entry_side'),
                'trailing_activation_pct': state_entry.get('trailing_activation_pct'),
                'trailing_distance_pct': state_entry.get('trailing_distance_pct'),
            }
            if state_entry:
                logger.info(
                    f"📍 Reconciliada: {position_key} "
                    f"(metadata preservada: {had_metadata})"
                )
            else:
                logger.warning(
                    f"📍 Posição aberta sem metadata no state: {position_key} — "
                    f"usando defaults (SL/TP custom não disponíveis)"
                )
                positions_without_metadata.append(position_key)

        # Drop entries do state que não correspondem a posições abertas
        stale_known_keys = [
            k for k in list(self.known_positions.keys()) if k not in api_keys
        ]
        for key in stale_known_keys:
            self.known_positions.pop(key, None)
            logger.info(f"🧹 Removida entry obsoleta de known_positions: {key}")

        # Limpa órfãos de trailing_activated / peak_prices
        orphan_trailing = [
            k for k in list(self.trailing_activated.keys()) if k not in api_keys
        ]
        for key in orphan_trailing:
            self.trailing_activated.pop(key, None)
            self.peak_prices.pop(key, None)
            logger.info(f"🧹 Removido trailing/peak órfão: {key}")

        # Notifica Telegram se houve divergência significativa
        total_divergences = (
            len(positions_without_metadata) + len(stale_known_keys) + len(orphan_trailing)
        )
        if total_divergences > 0:
            try:
                lines = ["⚠️ <b>RECONCILIAÇÃO DE STATE</b>", ""]
                if positions_without_metadata:
                    lines.append(
                        f"📥 Posições ativas sem metadata ({len(positions_without_metadata)}):"
                    )
                    for k in positions_without_metadata:
                        lines.append(f"   • <code>{k}</code>")
                    lines.append("   <i>→ SL/TP custom indisponíveis até nova configuração.</i>")
                if stale_known_keys:
                    lines.append("")
                    lines.append(
                        f"🧹 Posições fechadas enquanto bot estava off ({len(stale_known_keys)}):"
                    )
                    for k in stale_known_keys:
                        lines.append(f"   • <code>{k}</code>")
                if orphan_trailing:
                    lines.append("")
                    lines.append(
                        f"🗑️ Trailing órfão limpo ({len(orphan_trailing)}): "
                        f"<code>{', '.join(orphan_trailing)}</code>"
                    )
                self.telegram.send_message("\n".join(lines))
            except Exception as exc:
                logger.warning(f"⚠️ Falha ao notificar divergência no Telegram: {exc}")

        if existing_positions:
            logger.info(
                f"📊 {len(existing_positions)} posições reconciliadas "
                f"(divergências: {total_divergences})"
            )

        # Safety net: garante WS sync com TRADING_PAIRS final após TODAS as
        # mutações (Binance strategy, profiles, etc). Idempotente.
        self._sync_ws_subscriptions(reason="setup-exchange-final")

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
        
        # Verifica se precisa resetar (novo dia) — usa UTC como a Binance
        today = datetime.now(timezone.utc).date()
        if today > self.last_daily_reset:
            logger.info("🌅 Novo dia detectado! Resetando metas diárias...")
            # Registra o P&L do dia que acabou no histórico para kill switches
            # ANTES de zerar o contador.
            if getattr(self, 'kill_switch', None) is not None:
                self.kill_switch.record_daily_rollover(
                    date=self.last_daily_reset.strftime('%Y-%m-%d'),
                    net_pnl=float(self.daily_realized_pnl or 0.0),
                    trades_win=0,
                    trades_loss=0,
                )
            self.daily_target_reached = False
            self.daily_realized_pnl = 0.0
            self.last_daily_reset = today
            
            # Notifica reset
            self.telegram.send_message(
                "🌅 <b>NOVO DIA DE TRADING</b>\n\n"
                f"📈 Meta de Lucro: <code>+${config.DAILY_PROFIT_TARGET:.2f}</code>\n"
                f"📉 Limite de Perda: <code>-${config.DAILY_LOSS_LIMIT:.2f}</code>\n\n"
                "<i>Bot pronto para operar!</i>"
            )
        
        if self.daily_target_reached:
            return True
        
        if self.daily_realized_pnl >= config.DAILY_PROFIT_TARGET:
            self.daily_target_reached = True
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
        
        if self.daily_realized_pnl <= -config.DAILY_LOSS_LIMIT:
            self.daily_target_reached = True
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
        """Delegador — lógica real em ExecutionEngine."""
        self.execution_engine.close_all_for_daily_target(reason)

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
    
    def check_for_deposit(self) -> bool:
        """
        Detecta TRANSFER (entrada/saída) na Futures e ajusta capital base.

        Usa incomeType=TRANSFER da Binance para evitar falso positivo por P&L.
        """
        if not config.CAPITAL_TRANSFER_DETECTION_ENABLED:
            return False

        if self.initial_capital is None:
            return False

        now_ts_ms = int(time.time() * 1000)
        if self.last_transfer_check_ts_ms <= 0:
            self.last_transfer_check_ts_ms = now_ts_ms
            return False

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
                return False

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
                return False

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

            telegram = getattr(self, "telegram", None)
            if telegram is not None and hasattr(telegram, "send_message"):
                telegram.send_message(
                    f"{movement_emoji} <b>MOVIMENTAÇÃO DE CAPITAL DETECTADA</b>\n\n"
                    f"📥📤 <b>Tipo:</b> {movement_type}\n"
                    f"💵 <b>Variação líquida:</b> <code>${net_transfer_usdt:+.2f}</code>\n"
                    f"🧮 <b>Capital base (SL global):</b>\n"
                    f"   • Anterior: <code>${old_initial_capital:.2f}</code>\n"
                    f"   • Novo: <code>${new_initial_capital:.2f}</code>\n\n"
                    f"<b>Últimos eventos:</b>\n{events_text}"
                )

            # Recalcula imediatamente a faixa/estratégia baseada no saldo novo.
            if config.USE_BINANCE_STRATEGY:
                try:
                    self.check_and_update_binance_strategy()
                except Exception as exc:
                    logger.warning(f"⚠️ Falha ao atualizar estratégia após transferência: {exc}")

            # Força snapshot para refletir o novo saldo sem esperar a janela de 30 min.
            try:
                self.last_snapshot_time = None
                self.take_portfolio_snapshot()
            except Exception as exc:
                logger.warning(f"⚠️ Falha ao capturar snapshot após transferência: {exc}")

            # Persiste imediatamente para sobreviver reinício.
            self.save_state()
            return True

        except Exception as e:
            logger.warning(f"⚠️ Erro ao detectar transferência de capital: {e}")
            return False
    
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
            account_info = self.exchange.get_account_info()
            current_balance = account_info['wallet_balance']
            
            # Pega a estratégia atual para o capital
            new_strategy = config.get_binance_strategy_for_capital(current_balance)
            
            old_strategy = getattr(self, 'binance_strategy', None)
            
            if old_strategy is None or old_strategy['capital_range'] != new_strategy['capital_range']:
                logger.info("📊 MUDANÇA DE FAIXA DETECTADA!")
                if old_strategy:
                    logger.info(f"   Anterior: {old_strategy['capital_range']} ({old_strategy['num_coins']} moedas)")
                logger.info(f"   Nova: {new_strategy['capital_range']} ({new_strategy['num_coins']} moedas)")
                
                # Reseleciona pares do perfil primário dinâmico ao mudar de faixa.
                enabled_profiles, primary_profile, primary_is_dynamic = self._get_primary_profile_info()
                if primary_is_dynamic:
                    num_primary_pairs = self._resolve_primary_pair_target(
                        primary_profile=primary_profile,
                        strategy_num_coins=new_strategy['num_coins'],
                        fallback_num_coins=len(config.TRADING_PAIRS),
                    )
                    sorted_coins = self.sort_binance_coins_by_score(
                        num_primary_pairs,
                        exclude=self._get_reserved_pairs(enabled_profiles),
                    )
                    new_strategy['coins'] = sorted_coins
                    self.binance_strategy = new_strategy
                    config.TRADING_PAIRS = self._filter_disabled_pairs(sorted_coins)
                    self._sync_strategy_profiles_with_trading_pairs(
                        reason="binance-tier-change",
                        primary_pairs=config.TRADING_PAIRS,
                    )
                else:
                    new_strategy['coins'] = []
                    self.binance_strategy = new_strategy
                    self._sync_strategy_profiles_with_trading_pairs(reason="binance-tier-change")
                
                for symbol in config.TRADING_PAIRS:
                    if symbol not in self.pnl_by_symbol:
                        self.pnl_by_symbol[symbol] = 0.0
                
                # Define alavancagem para os novos pares
                for symbol in config.TRADING_PAIRS:
                    self.exchange.set_leverage(symbol, config.LEVERAGE)
                
                # Notifica no Telegram
                coins_display = ', '.join([c.replace('USDT', '') for c in config.TRADING_PAIRS])
                self.telegram.send_message(
                    f"📊 <b>MUDANÇA DE FAIXA</b>\n\n"
                    f"💰 <b>Saldo Atual:</b> ${current_balance:.2f}\n"
                    f"📈 <b>Nova Faixa:</b> {new_strategy['capital_range']}\n"
                    f"💵 <b>Order Size:</b> ${new_strategy['order_size']}\n"
                    f"🪙 <b>Moedas ({len(config.TRADING_PAIRS)}) - Por Score:</b>\n{coins_display}"
                )
                
        except Exception as e:
            logger.error(f"Erro ao verificar estratégia Binance: {e}")
    
    def update_trading_pairs(self):
        """
        Atualiza a lista de pares de trading usando seleção inteligente.

        Chamado periodicamente (configurável em config.PAIR_UPDATE_INTERVAL_MINUTES).
        Configura os novos pares. Pares com posição ABERTA não são rotacionados
        para fora — seguem geridos até fechar no próprio SL/TP (não liquidamos
        por causa do score). Ver _retain_held_pairs.
        """
        if not config.AUTO_SELECT_PAIRS or not self.pair_selector:
            return
        
        if not self.pair_selector.should_update():
            return
        
        logger.info("🔄 Atualizando lista de pares...")
        
        # Guarda pares antigos
        old_pairs = set(config.TRADING_PAIRS)

        self._refresh_binance_coin_universe(trigger_reason="auto-update")
        
        available_capital = self.exchange.get_available_balance()
        
        # Seleciona novos pares baseado no capital disponível
        selected_pairs, _scores = self.pair_selector.select_best_pairs(
            available_capital=available_capital
        )
        # Pares com posição aberta não saem da lista: precisamos saber quais são
        # ANTES de calcular as mudanças. Se a API falhar, pulamos o rescore para
        # não desproteger nenhum par com posição.
        try:
            open_positions = self.exchange.get_open_positions()
        except Exception as exc:
            logger.warning(
                f"⚠️ API indisponível ao checar posições abertas — pulando este "
                f"rescore para não rotacionar par com posição: {exc}"
            )
            return
        held_symbols = {pos['symbol'] for pos in open_positions}

        selected_pairs, retained = _retain_held_pairs(selected_pairs, held_symbols)
        new_pairs = set(selected_pairs)
        if retained:
            logger.info(f"📌 Mantidos por posição aberta (não rotacionados): {', '.join(retained)}")

        # Identifica mudanças (removed_pairs já exclui os retidos por posição)
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

        # NÃO fechamos posições por rotação: removed_pairs já exclui pares com
        # posição aberta (ver _retain_held_pairs), então os removidos estão flat.
        # A posição retida fecha no próprio SL/TP via monitor_positions.

        config.TRADING_PAIRS = self._filter_disabled_pairs(selected_pairs)
        self._sync_strategy_profiles_with_trading_pairs(
            reason="auto-select-update",
            primary_pairs=config.TRADING_PAIRS,
        )
        self._sync_ws_subscriptions(reason="update-trading-pairs")
        
        for symbol in config.TRADING_PAIRS:
            if symbol not in self.pnl_by_symbol:
                self.pnl_by_symbol[symbol] = 0.0
        
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
        
        msg += f"\n<i>Próxima atualização em {_format_pair_interval(config.PAIR_UPDATE_INTERVAL_MINUTES)}</i>"
        
        self.telegram.send_message(msg)
        
        logger.info("✅ Lista de pares atualizada!")
    
    def sort_binance_coins_by_score(self, num_coins: int, exclude: set | None = None) -> list:
        """
        Ordena pares por score composto: spread 35%, volume 30%, volatility 20%,
        trend 10%, funding 5%. Menor spread/funding é melhor; maior volume/vol/trend.
        """
        if num_coins <= 0:
            return []

        candidate_coins = self._refresh_binance_coin_universe(
            trigger_reason=f"score:{num_coins}"
        )

        if not candidate_coins:
            logger.warning("⚠️ Sem pares candidatos para calcular score")
            return []

        if exclude:
            candidate_coins = [c for c in candidate_coins if c not in exclude]

        # ── 1. PRÉ-BUSCA BULK (paralelo) ─────────────────────────────────────
        logger.info("📡 Buscando tickers e funding rates em bulk...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as _bulk_executor:
            _ticker_fut = _bulk_executor.submit(self.exchange.get_all_tickers_24h)
            _funding_fut = _bulk_executor.submit(self.exchange.get_all_funding_rates)
            all_tickers = _ticker_fut.result()
            all_funding = _funding_fut.result()

        # ── 2. PRÉ-FILTRO POR VOLUME (evita chamadas desnecessárias) ─────────
        # Em testnet o quoteVolume do ticker é SINTÉTICO (inflado) e deixa pares
        # ilíquidos passarem por este piso → são pontuados e atribuídos ao perfil,
        # entrando em loop "IA aprova → gate de abertura barra" (caso SPACE/BSB).
        # Usa o volume REAL da mainnet de referência quando disponível, igual ao
        # gate de abertura e ao get_pair_metrics (#137). Símbolo ausente do mapa
        # de referência = ilíquido/inexistente na mainnet → volume 0 (rejeitado).
        min_volume = getattr(config, "MIN_VOLUME_24H_USD", 0)
        try:
            ref_map = self.exchange.get_reference_liquidity_map()
        except Exception:
            ref_map = None
        pre_filtered_with_vol: List[Tuple[str, float]] = []
        for symbol in candidate_coins:
            ticker = all_tickers.get(symbol)
            if not ticker:
                continue
            try:
                if ref_map is not None:
                    vol = float((ref_map.get(symbol) or {}).get("volume_24h", 0.0) or 0.0)
                else:
                    vol = float(ticker.get("quoteVolume", 0))
                if vol >= min_volume:
                    pre_filtered_with_vol.append((symbol, vol))
            except (TypeError, ValueError):
                continue

        # Se PAIR_SCORING_MAX_CANDIDATES > 0, corta no top-N por volume 24h antes
        # do scoring custoso. Útil pra viabilizar intervalos curtos de rescore
        # sem saturar a VM.
        max_candidates = int(getattr(config, "PAIR_SCORING_MAX_CANDIDATES", 0) or 0)
        before_cap = len(pre_filtered_with_vol)
        if max_candidates > 0 and before_cap > max_candidates:
            pre_filtered_with_vol.sort(key=lambda x: x[1], reverse=True)
            pre_filtered_with_vol = pre_filtered_with_vol[:max_candidates]
            logger.info(
                f"🎯 Limitado a top {max_candidates} por volume "
                f"(de {before_cap} após filtro mínimo)"
            )

        pre_filtered = [symbol for symbol, _ in pre_filtered_with_vol]
        total_candidates = len(pre_filtered)
        logger.info(
            f"📊 Calculando scores para {total_candidates} moedas "
            f"(de {len(candidate_coins)} candidatas após filtro de volume)..."
        )

        # ── 3. CALCULA MÉTRICAS EM PARALELO ──────────────────────────────────
        def _score_symbol(symbol: str):
            try:
                ticker = all_tickers.get(symbol)
                funding = all_funding.get(symbol)
                metrics = self.pair_selector.get_pair_metrics(
                    symbol,
                    prefetched_ticker=ticker,
                    prefetched_funding_rate=funding,
                )
                if metrics:
                    score = self.pair_selector.score_pair(metrics)
                    if score > 0:
                        return (symbol, score)
            except Exception as e:
                logger.warning(f"   ⚠️ Erro ao calcular score de {symbol}: {e}")
            return None

        coins_with_scores = []
        max_workers = min(10, total_candidates or 1)
        completed = 0
        _progress_steps = {max(1, int(total_candidates * p / 100)) for p in (25, 50, 75)}

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_score_symbol, sym) for sym in pre_filtered}
            for future in concurrent.futures.as_completed(futures):
                completed += 1
                result = future.result()
                if result is not None:
                    coins_with_scores.append(result)
                    logger.debug(f"   {result[0]}: score {result[1]:.2f}")
                if completed in _progress_steps:
                    pct = int(completed / total_candidates * 100)
                    remaining = total_candidates - completed
                    logger.info(
                        f"   ⏳ Progresso: {completed}/{total_candidates} ({pct}%) — faltam {remaining} moedas"
                    )

        # ── 4. ORDENA ──────────────────────────────────────────────────────────
        coins_with_scores.sort(key=lambda x: x[1], reverse=True)

        # ── 4b. OPEN INTEREST (confirmador futures-aware, só nos finalistas) ─────
        # OI subindo = dinheiro novo no par. Aplica um bônus ADITIVO no score
        # (nunca penaliza) apenas quando oi_change_score ≥ OI_CONFIRM_MIN_SCORE.
        # Busca OI só para o topo do ranking (1 chamada/par) pra não saturar a API.
        if getattr(config, "OI_ENABLED", False) and coins_with_scores:
            oi_weight = float(getattr(config, "OPEN_INTEREST_WEIGHT", 8.0))
            confirm_min = float(getattr(config, "OI_CONFIRM_MIN_SCORE", 60.0))
            max_finalists = int(getattr(config, "OI_MAX_FINALISTS", 20))
            core_total = sum(config.PAIR_SELECTION_WEIGHTS.values())
            denom = core_total + oi_weight
            finalists = coins_with_scores[:max_finalists]

            def _apply_oi(item):
                symbol, base = item
                oi_change = self.pair_selector.get_oi_change_percent(symbol)
                oi_score = self.pair_selector.oi_change_to_score(oi_change)
                if oi_score >= confirm_min and denom > 0:
                    bonus = oi_score * oi_weight / denom
                    logger.info(
                        f"   📈 OI confirm {symbol}: ΔOI={oi_change:.2f}% "
                        f"score={oi_score:.0f} +{bonus:.2f}"
                    )
                    return (symbol, base + bonus)
                return (symbol, base)

            oi_workers = min(8, len(finalists))
            with concurrent.futures.ThreadPoolExecutor(max_workers=oi_workers) as _oi_ex:
                rescored = dict(_oi_ex.map(_apply_oi, finalists))
            coins_with_scores = [
                (sym, rescored.get(sym, sc)) for sym, sc in coins_with_scores
            ]
            coins_with_scores.sort(key=lambda x: x[1], reverse=True)

        # ── 4c. RETORNA ──────────────────────────────────────────────────────────
        logger.info(f"🏆 Top {num_coins} moedas por score:")
        for i, (symbol, score) in enumerate(coins_with_scores[:num_coins], 1):
            logger.info(f"   {i}. {symbol.replace('USDT', '')}: {score:.2f}")

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

        # Pula reordenação quando o perfil primário tem pares fixos.
        enabled_profiles, primary_profile, primary_is_dynamic = self._get_primary_profile_info()
        if not primary_is_dynamic:
            return

        logger.info("🔄 Atualizando seleção de pares do trend_strong por score...")

        num_primary_pairs = self._resolve_primary_pair_target(
            primary_profile=primary_profile,
            strategy_num_coins=self.binance_strategy.get('num_coins'),
            fallback_num_coins=len(config.TRADING_PAIRS),
        )

        old_coins = list(config.TRADING_PAIRS)

        # Reseleciona os melhores pares excluindo os fixos do range_scalp_v1
        new_coins = self.sort_binance_coins_by_score(
            num_primary_pairs,
            exclude=self._get_reserved_pairs(enabled_profiles),
        )

        if new_coins == old_coins:
            logger.info("✅ Seleção de pares não mudou")
            return

        config.TRADING_PAIRS = self._filter_disabled_pairs(new_coins)
        self.binance_strategy['coins'] = new_coins
        self._sync_strategy_profiles_with_trading_pairs(
            reason="binance-reorder",
            primary_pairs=config.TRADING_PAIRS,
        )
        
        for symbol in new_coins:
            if symbol not in self.pnl_by_symbol:
                self.pnl_by_symbol[symbol] = 0.0
        
        for symbol in new_coins:
            if symbol not in old_coins:
                self.exchange.set_leverage(symbol, config.LEVERAGE)
        
        # Notifica
        coins_display = ', '.join([c.replace('USDT', '') for c in new_coins])
        self.telegram.send_message(
            f"🔄 <b>MOEDAS REORDENADAS POR SCORE</b>\n\n"
            f"🪙 <b>Nova Ordem ({num_primary_pairs}):</b>\n{coins_display}\n\n"
            f"<i>Baseado em: spread, volume, volatilidade, tendência, funding</i>"
        )
        
        logger.info("✅ Moedas reordenadas!")
    
    def trigger_pair_rescore(self) -> dict:
        """
        Executa o rescore de pares imediatamente (via comando Telegram /rescore)
        e reprograma o próximo rescore automático conforme PAIR_UPDATE_INTERVAL_MINUTES.
        """
        if config.USE_BINANCE_STRATEGY:
            self.update_binance_strategy_coins()
        else:
            self.update_trading_pairs()

        interval = getattr(
            self,
            "_pair_update_interval",
            int(getattr(config, "PAIR_UPDATE_INTERVAL_MINUTES", 360) or 360) * 60,
        )
        self.next_pair_update_time = time.monotonic() + interval

        # Sincroniza streams WS com a nova lista de pares
        self._sync_ws_subscriptions(reason="update-binance-coins")

        hours = interval / 3600
        next_in = f"{hours:.0f}h" if hours >= 1 else f"{interval / 60:.0f}min"
        return {
            "pairs": list(config.TRADING_PAIRS),
            "next_rescore_in": next_in,
        }

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
        # Limita o tamanho do cache para evitar crescimento ilimitado
        if len(self.sentiment_cache) > 200:
            oldest_keys = list(self.sentiment_cache.keys())[:len(self.sentiment_cache) - 200]
            for k in oldest_keys:
                self.sentiment_cache.pop(k, None)
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

    def _maybe_run_ai_consultive_review(
        self,
        *,
        symbol: str,
        strategy_name: str,
        strategy_type: str,
        entry_mode: str,
        signal_name: str,
        setup: Any,
        klines: List[Dict[str, Any]],
        confirmation_klines: List[Dict[str, Any]] | None,
        execution_timeframe: str,
        confirmation_timeframe: str | None,
        available_balance: float,
        open_positions: List[Dict[str, Any]],
        should_open_long: bool,
        should_open_short: bool,
        min_notional: float,
        requested_side: str | None = None,
        allowed_entry_sides: List[str] | None = None,
    ):
        """Executa revisão consultiva via IA sem alterar a lógica de execução."""
        engine = getattr(self, "ai_consultive_engine", None)
        if engine is None or not hasattr(engine, "is_enabled") or not engine.is_enabled():
            return None

        try:
            sentiment_snapshot = None
            if getattr(self, "sentiment_mode_enabled", False):
                try:
                    sentiment_snapshot = self._get_symbol_sentiment(symbol)
                except Exception as exc:
                    logger.warning(f"⚠️ Falha ao obter snapshot de sentimento para IA ({symbol}): {exc}")

            snapshot = engine.build_market_snapshot(
                symbol=symbol,
                strategy_name=strategy_name,
                strategy_type=strategy_type,
                entry_mode=entry_mode,
                signal_name=signal_name,
                setup=setup,
                klines=klines,
                confirmation_klines=confirmation_klines,
                execution_timeframe=execution_timeframe,
                confirmation_timeframe=confirmation_timeframe,
                available_balance=available_balance,
                open_positions=open_positions,
                should_open_long=should_open_long,
                should_open_short=should_open_short,
                min_notional=min_notional,
                sentiment_snapshot=sentiment_snapshot,
                requested_side=requested_side,
                allowed_entry_sides=allowed_entry_sides,
            )
            review = engine.evaluate_setup(snapshot)

            if not getattr(setup, "metadata", None):
                setup.metadata = {}
            setup.metadata["ai_consultive"] = review.compact_for_trade()

            if review.status == "ok":
                logger.info(
                    "🤖 IA consultiva [%s] %s => %s conf=%s cached=%s",
                    strategy_name,
                    symbol,
                    review.decision,
                    review.confidence,
                    review.from_cache,
                )
            elif review.status == "error":
                logger.warning(f"⚠️ IA consultiva indisponível em {symbol}: {review.error}")

            if review.should_notify and self.telegram:
                message = engine.build_telegram_message(review)
                self.telegram.send_message(message)

            return review

        except Exception as exc:
            logger.warning(f"⚠️ Erro na IA consultiva para {symbol}: {exc}")
            return None

    def _reference_quote_volume_24h(self, symbol: str):
        """Volume 24h de referência do símbolo, com a mesma fonte/fallback do
        gate de abertura: volume REAL da mainnet em testnet (o quoteVolume da
        testnet é sintético), caindo pro ticker da própria exchange quando não
        há referência (mainnet, ou leitura falha). Retorna None se ilegível —
        fail-open: um blip de API não deve barrar o fluxo.
        """
        try:
            vol = self.exchange.get_reference_volume_24h(symbol)
        except Exception:
            vol = None
        if vol is None:
            try:
                vol = (self.exchange.get_ticker_24h(symbol) or {}).get("quoteVolume")
            except Exception:
                vol = None
        return vol

    def _maybe_build_gated_ai_override_setup(
        self,
        *,
        strategy_engine: Any,
        strategy_label: str,
        strategy_type: str,
        symbol: str,
        klines: List[Dict[str, Any]],
        confirmation_klines: List[Dict[str, Any]] | None,
        available_balance: float,
        min_notional: float,
        risk_profile: Any,
    ):
        ai_mode = str(getattr(config, "AI_CONSULTIVE_MODE", "off") or "off").strip().lower()
        if ai_mode != "gated":
            return None
        if strategy_type != "trend_signal" or strategy_label != "trend_strong":
            return None
        if not confirmation_klines:
            return None
        if not hasattr(strategy_engine, "build_ai_override_candidate_setup"):
            return None

        # Rede de segurança ANTES do gate da IA: se o par está abaixo do piso de
        # liquidez de trade, o gate de abertura barraria de qualquer forma. Pular
        # aqui evita queimar chamada de IA e spammar notificação a cada ciclo —
        # caso SPACE/BSB, par ilíquido atribuído ao perfil entrava em loop
        # "IA aprova → abertura barra" indefinidamente.
        min_trade_volume = float(getattr(config, "MIN_TRADE_VOLUME_24H_USD", 0) or 0)
        if min_trade_volume > 0:
            raw_vol = self._reference_quote_volume_24h(symbol)
            if _below_min_trade_volume(raw_vol, min_trade_volume):
                logger.info(
                    f"⏭️ {symbol} pulado antes do gate da IA — volume 24h abaixo "
                    f"do piso de liquidez (${float(raw_vol):,.0f} < ${min_trade_volume:,.0f})"
                )
                return None

        candidate_setup = strategy_engine.build_ai_override_candidate_setup(
            symbol=symbol,
            execution_klines=klines,
            confirmation_klines=confirmation_klines,
            available_capital=available_balance,
            min_notional=min_notional,
            risk_profile=risk_profile,
        )
        if candidate_setup is not None:
            logger.info(
                "🤖 [%s] %s sem setup clássico; candidato %s enviado ao gate da IA",
                strategy_label,
                symbol,
                (getattr(candidate_setup, "metadata", {}) or {}).get("trend_candidate_side", "?"),
            )
        return candidate_setup

    def _notify_ai_approved_trade_block(
        self,
        *,
        symbol: str,
        side: str,
        strategy_name: str,
        reason: str,
        detail: str = "",
        setup_metadata: Dict[str, Any] | None = None,
    ) -> bool:
        """Backward-compat: delegate ao TradeBlockReporter.

        Mantido pra não quebrar fixtures de teste que monkeypatcham este
        método. Novos call sites devem usar `self.block_reporter.notify_blocked`
        direto.
        """
        return self.block_reporter.notify_blocked(
            symbol=symbol,
            side=side,
            strategy_name=strategy_name,
            reason=reason,
            detail=detail,
            setup_metadata=setup_metadata,
        )

    def _mark_symbol_reentry_cooldown(self, symbol: str) -> None:
        """Marca o símbolo em cooldown de reentrada a partir de agora.

        Chamado após um fechamento negativo para o mesmo sinal não reabrir no
        ciclo seguinte (anti-churn). No-op se o cooldown estiver desativado.
        """
        if int(getattr(config, "SYMBOL_REENTRY_COOLDOWN_SECONDS", 0) or 0) <= 0:
            return
        if symbol:
            self.symbol_reentry_cooldowns[symbol] = time.time()

    def _symbol_reentry_cooldown_remaining(self, symbol: str) -> float:
        """Segundos restantes de cooldown de reentrada (0 se livre/desativado).

        Faz prune da entrada expirada ao consultar.
        """
        window = int(getattr(config, "SYMBOL_REENTRY_COOLDOWN_SECONDS", 0) or 0)
        if window <= 0:
            return 0.0
        started = self.symbol_reentry_cooldowns.get(symbol)
        if started is None:
            return 0.0
        remaining = window - (time.time() - started)
        if remaining <= 0:
            self.symbol_reentry_cooldowns.pop(symbol, None)
            return 0.0
        return remaining

    @staticmethod
    def _mirror_setup_sl_tp_for_inversion(setup, is_long_now: bool) -> None:
        """Espelha o SL/TP do setup quando o sinal é invertido (/invert).

        O setup chega com SL/TP calculados para o sinal ORIGINAL (ex.: para um
        STRONG_SELL, TP fica ABAIXO da entrada). Ao inverter o lado sem ajustar,
        um LONG invertido recebe TP abaixo do entry e fecha instantaneamente em
        ≈entry (só pagando fee). Aqui refletimos as distâncias em torno do entry
        preservando o risk/reward e propagamos para as chaves de metadata, que o
        ExecutionEngine lê com precedência sobre setup.stop_loss/take_profit.
        """
        try:
            entry = float(getattr(setup, "entry_price", 0) or 0)
        except (TypeError, ValueError):
            return
        if entry <= 0:
            return
        meta = getattr(setup, "metadata", None)
        meta = meta if isinstance(meta, dict) else {}
        try:
            src_sl = float(meta.get("custom_stop_loss", setup.stop_loss))
            src_tp = float(meta.get("custom_take_profit", setup.take_profit))
        except (TypeError, ValueError):
            return
        if src_sl <= 0 or src_tp <= 0:
            return
        sl_dist = abs(entry - src_sl) / entry
        tp_dist = abs(src_tp - entry) / entry
        if is_long_now:
            new_sl = round(entry * (1 - sl_dist), 8)
            new_tp = round(entry * (1 + tp_dist), 8)
        else:
            new_sl = round(entry * (1 + sl_dist), 8)
            new_tp = round(entry * (1 - tp_dist), 8)
        setup.stop_loss = new_sl
        setup.take_profit = new_tp
        if "custom_stop_loss" in meta:
            meta["custom_stop_loss"] = new_sl
        if "custom_take_profit" in meta:
            meta["custom_take_profit"] = new_tp

    def analyze_and_trade(self, symbol: str, strategy_name: str | None = None) -> bool:
        """
        Analisa um par e executa trades se houver oportunidade.

        O comportamento de entrada depende do perfil da estratégia:
        - strong_only: entra só com STRONG_BUY/STRONG_SELL
        - standard: entra com BUY/SELL e sinais fortes
        """
        # VERIFICA META DIÁRIA
        if self.check_daily_targets():
            logger.info("⏸️  Meta diária atingida - não abrindo novas posições")
            return False

        # Improvement 1: verifica drawdown desde o pico de equity
        try:
            _bal_for_dd = self.exchange.get_account_balance()
            if _bal_for_dd > 0 and self._check_drawdown_from_peak(_bal_for_dd):
                logger.info("⏸️  Drawdown máximo atingido - não abrindo novas posições")
                return False
        except Exception:
            pass

        # Override de estratégia pelo regime classifier (committed após hysteresis).
        # Em tickz iniciais o committed é None — usa o profile estático.
        regime_committed = (
            self._regime_committed.get(str(symbol).upper())
            if getattr(config, "REGIME_CLASSIFIER_ENABLED", False)
            else None
        )
        strategy_context = self._resolve_strategy_context(
            symbol=symbol, strategy_name=strategy_name, regime_override=regime_committed,
        )
        strategy_engine = strategy_context.get("strategy", getattr(self, "strategy", None) or HedgeStrategy())
        strategy_label = str(strategy_context.get("name", "primary"))
        strategy_type = self._normalize_strategy_type(strategy_context.get("strategy_type", "trend_signal"))
        entry_mode = self._normalize_strategy_entry_mode(strategy_context.get("entry_mode", "strong_only"))
        risk_profile = strategy_context.get("risk_profile")
        execution_timeframe = str(config.TIMEFRAME)
        analysis_lookback = int(config.CANDLES_LOOKBACK)
        confirmation_timeframe = None
        confirmation_klines = None

        is_trend_strong = strategy_type == "trend_signal" and strategy_label == "trend_strong"
        if is_trend_strong:
            execution_timeframe = str(getattr(config, "TREND_STRONG_EXECUTION_TIMEFRAME", "3m"))
            analysis_lookback = max(
                220,
                int(getattr(config, "TREND_STRONG_CANDLES_LOOKBACK", 260)),
                int(config.CANDLES_LOOKBACK),
            )
            confirmation_timeframe = str(getattr(config, "TREND_STRONG_CONFIRM_TIMEFRAME", "5m"))

        logger.info(f"🔍 [{strategy_label}] Analisando {symbol}...")
        
        # Improvement 5: busca klines em paralelo quando há timeframe de confirmação
        if is_trend_strong and confirmation_timeframe:
            def _fetch_klines(interval, limit):
                return self.exchange.get_klines(symbol=symbol, interval=interval, limit=limit)

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as _kl_executor:
                _exec_future = _kl_executor.submit(_fetch_klines, execution_timeframe, analysis_lookback)
                _conf_future = _kl_executor.submit(_fetch_klines, confirmation_timeframe, analysis_lookback)
                klines = _exec_future.result()
                confirmation_klines = _conf_future.result()
        else:
            klines = self.exchange.get_klines(
                symbol=symbol,
                interval=execution_timeframe,
                limit=analysis_lookback
            )

        if not klines:
            logger.warning(f"⚠️  Sem dados para {symbol}")
            return False

        if is_trend_strong and confirmation_timeframe and not confirmation_klines:
            logger.warning(f"⚠️  Sem dados de confirmação ({confirmation_timeframe}) para {symbol}")
            return False

        # Regime classifier — alimenta observação do tick na janela de hysteresis.
        # O override (se houver) é aplicado no PRÓXIMO tick — neste, já estamos
        # rodando a estratégia comitada anteriormente (ou a estática se não há
        # commit). Squeeze bloqueia entradas de range scalping no tick atual.
        if getattr(config, "REGIME_CLASSIFIER_ENABLED", False):
            regime_info = self._classify_symbol_regime(klines)
            new_commit = self._update_regime_history(symbol, regime_info["regime"])
            logger.info(
                f"   🌀 Regime {symbol}: obs={regime_info['regime']} "
                f"(ADX={regime_info['adx']:.1f}, BBW={regime_info['bbw_percent']:.2f}%) "
                f"committed={new_commit or '∅'}"
            )
            if regime_info["regime"] == "squeeze" and strategy_type == "range_scalping":
                logger.info(
                    f"⏸️  {symbol}: squeeze detectado — pulando entrada range_scalping"
                )
                return False

        # Verifica saldo DISPONÍVEL para novos trades
        available_balance = self.exchange.get_available_balance()
        logger.info(f"💰 Saldo disponível: ${available_balance:.2f}")
        
        # Busca informações do símbolo (incluindo mínimo notional)
        symbol_info = self.exchange.get_symbol_info(symbol)
        min_notional = symbol_info.get('minNotional', 5.0)
        ai_mode = str(getattr(config, "AI_CONSULTIVE_MODE", "off") or "off").strip().lower()
        
        # Gera setup de trade
        setup_kwargs = {
            "symbol": symbol,
            "klines": klines,
            "available_capital": available_balance,
            "min_notional": min_notional,
            "risk_profile": risk_profile,
        }
        if confirmation_klines is not None:
            setup_kwargs["confirmation_klines"] = confirmation_klines
            setup_kwargs["execution_timeframe"] = execution_timeframe
            setup_kwargs["confirmation_timeframe"] = confirmation_timeframe

        setup = strategy_engine.generate_trade_setup(
            **setup_kwargs,
        )
        
        if not setup:
            setup = self._maybe_build_gated_ai_override_setup(
                strategy_engine=strategy_engine,
                strategy_label=strategy_label,
                strategy_type=strategy_type,
                symbol=symbol,
                klines=klines,
                confirmation_klines=confirmation_klines,
                available_balance=available_balance,
                min_notional=min_notional,
                risk_profile=risk_profile,
            )
            if not setup:
                logger.info(f"⏸️  Sem setup válido para {symbol}")
                return False
        
        # VERIFICA O SINAL
        signal = setup.signal
        signal_name = signal.name if hasattr(signal, 'name') else str(signal)
        
        # Define se deve abrir LONG ou SHORT conforme o perfil.
        if entry_mode == "standard":
            should_open_long = signal_name in {'STRONG_BUY', 'BUY'}
            should_open_short = signal_name in {'STRONG_SELL', 'SELL'}
        else:
            should_open_long = signal_name == 'STRONG_BUY'
            should_open_short = signal_name == 'STRONG_SELL'

        # Inversão global de sinais (toggle via /invert no Telegram).
        # Inverte o lado E espelha o SL/TP do setup em torno do entry — senão o
        # lado invertido herda SL/TP do sinal original (TP do lado errado fecha
        # a posição na hora). O resto do pipeline só vê o lado/SL/TP já corretos.
        if getattr(self, 'invert_signals', False) and (should_open_long or should_open_short):
            should_open_long, should_open_short = should_open_short, should_open_long
            self._mirror_setup_sl_tp_for_inversion(setup, should_open_long)
            logger.info(
                f"🔀 Sinal invertido em {symbol}: {signal_name} → "
                f"{'LONG' if should_open_long else 'SHORT'}"
            )

        # Aplica filtro direcional de sentimento (quando ativo).
        should_open_long, should_open_short = self._apply_sentiment_direction_filter(
            symbol=symbol,
            should_open_long=should_open_long,
            should_open_short=should_open_short,
        )

        # Gate de SHORTs contra-tendência: só permite SHORT quando o regime
        # comitado é "trend" (ADX ≥ REGIME_ADX_TREND_THRESHOLD). A estratégia
        # já checa direção via EMAs/VWAP — esta camada exige que a tendência
        # tenha força ADX suficiente. Em "range"/"squeeze"/"neutral" os SHORTs
        # viram whipsaw. LONG não é gateado.
        if (
            should_open_short
            and getattr(config, "BLOCK_COUNTERTREND_SHORT_ENABLED", False)
            and getattr(config, "REGIME_CLASSIFIER_ENABLED", False)
        ):
            current_regime = self._regime_committed.get(str(symbol).upper())
            if current_regime != "trend":
                logger.info(
                    f"⏸️  {symbol}: SHORT bloqueado — regime '{current_regime or '∅'}' "
                    "não confirmado como tendência (ADX abaixo do threshold)"
                )
                self.block_reporter.notify_blocked(
                    symbol=symbol,
                    side="SHORT",
                    strategy_name=strategy_label,
                    reason="SHORT contra-tendência bloqueado",
                    detail=f"Regime atual: {current_regime or 'indefinido'} (precisa de 'trend')",
                )
                should_open_short = False

        # Gate de Open Interest na entrada (soft). Bloqueia abrir quando o OI
        # está caindo forte (ΔOI ≤ OI_ENTRY_CONTRADICT_PCT): movimento sem
        # dinheiro novo (short-covering / liquidação), conviction fraca. OI
        # estável ou subindo passa. Dado de OI indisponível NÃO bloqueia — é
        # confirmador, não filtro absoluto (e a API de OI é externa/keyless).
        if (
            (should_open_long or should_open_short)
            and getattr(config, "OI_ENABLED", False)
            and getattr(config, "OI_ENTRY_GATE_ENABLED", False)
            and getattr(self, "pair_selector", None) is not None
        ):
            contradict_pct = float(getattr(config, "OI_ENTRY_CONTRADICT_PCT", -5.0))
            oi_change = self.pair_selector.get_oi_change_percent(symbol)
            if oi_change is not None and oi_change <= contradict_pct:
                side_lbl = "LONG" if should_open_long else "SHORT"
                logger.info(
                    f"⏸️  {symbol}: {side_lbl} bloqueado por OI — "
                    f"ΔOI={oi_change:.2f}% ≤ {contradict_pct:.1f}% "
                    "(movimento sem dinheiro novo)"
                )
                self.block_reporter.notify_blocked(
                    symbol=symbol,
                    side=side_lbl,
                    strategy_name=strategy_label,
                    reason="Entrada bloqueada por OI caindo forte",
                    detail=f"ΔOI={oi_change:.2f}% (limiar {contradict_pct:.1f}%)",
                )
                should_open_long = False
                should_open_short = False

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
        
        # Se API falhar, pula este símbolo — abrir sem saber o estado pode duplicar posição.
        try:
            open_positions = self.exchange.get_open_positions()
        except Exception as exc:
            logger.warning(
                f"⚠️ API indisponível ao checar posições para {symbol} — "
                f"pulando análise deste símbolo: {exc}"
            )
            return False

        has_long = False
        has_short = False
        
        for pos in open_positions:
            if pos['symbol'] == symbol:
                if pos['side'] == 'LONG':
                    has_long = True
                elif pos['side'] == 'SHORT':
                    has_short = True
        
        # DECIDE O QUE FAZER BASEADO NO SINAL
        
        # Se sinal forte de compra, mas já tem LONG, não faz nada.
        if should_open_long and has_long:
            logger.info(f"⏸️  Sinal {signal_name} em {symbol} mas LONG já está aberto")
            return False
        
        if should_open_short and has_short:
            logger.info(f"⏸️  Sinal {signal_name} em {symbol} mas SHORT já está aberto")
            return False

        # Anti-churn: símbolo que fechou negativo há pouco fica em cooldown.
        # Checado aqui (antes do review da IA) para não gastar chamada à toa.
        cooldown_remaining = self._symbol_reentry_cooldown_remaining(symbol)
        if cooldown_remaining > 0:
            logger.info(
                f"⏳ {symbol} em cooldown de reentrada "
                f"({int(cooldown_remaining)}s restantes após fechamento negativo) — pulando"
            )
            return False

        total_positions = len(open_positions)
        if total_positions >= config.MAX_OPEN_POSITIONS:
            logger.info("⏸️  Limite de posições atingido")
            return False
        
        if not self.risk_manager.can_open_position(total_positions):
            logger.info("⏸️  Limite de risco atingido")
            return False

        requested_side = "LONG" if should_open_long else "SHORT" if should_open_short else "NONE"
        allowed_entry_sides = [requested_side] if requested_side in {"LONG", "SHORT"} else []

        review = self._maybe_run_ai_consultive_review(
            symbol=symbol,
            strategy_name=strategy_label,
            strategy_type=strategy_type,
            entry_mode=entry_mode,
            signal_name=signal_name,
            setup=setup,
            klines=klines,
            confirmation_klines=confirmation_klines,
            execution_timeframe=execution_timeframe,
            confirmation_timeframe=confirmation_timeframe,
            available_balance=available_balance,
            open_positions=open_positions,
            should_open_long=should_open_long,
            should_open_short=should_open_short,
            min_notional=min_notional,
            requested_side=requested_side,
            allowed_entry_sides=allowed_entry_sides,
        )

        if ai_mode == "gated":
            if review is None:
                logger.info(f"⏸️  Entrada bloqueada por IA em {symbol} - revisão indisponível")
                return False
            if review.status != "ok":
                logger.info(
                    f"⏸️  Entrada bloqueada por IA em {symbol} - revisão inválida: "
                    f"{review.error or review.status}"
                )
                return False
            if not review.approval:
                logger.info(
                    f"⏸️  Entrada bloqueada por IA em {symbol} - decisão={review.decision} "
                    f"conf={review.confidence} min={getattr(config, 'AI_CONSULTIVE_MIN_CONFIDENCE', 80)}"
                )
                return False
            should_open_long = review.entry_side == "LONG"
            should_open_short = review.entry_side == "SHORT"
            if not should_open_long and not should_open_short:
                logger.info(f"⏸️  Entrada bloqueada por IA em {symbol} - lado inválido")
                return False
        
        return self.execute_signal_trade(
            setup=setup,
            open_long=should_open_long,
            open_short=should_open_short,
            strategy_name=strategy_label,
        )
    
    def execute_signal_trade(
        self,
        setup,
        open_long: bool = False,
        open_short: bool = False,
        strategy_name: str = "primary",
    ) -> bool:
        """Delegador — lógica real em ExecutionEngine.open_signal_trade."""
        return self.execution_engine.open_signal_trade(
            setup=setup,
            open_long=open_long,
            open_short=open_short,
            strategy_name=strategy_name,
        )

    def _should_force_exit_range_break(self, symbol: str, side: str, range_mid_price) -> bool:
        """
        Critério de saída antecipada para range scalping.

        Se duas velas consecutivas fecham além do meio do range sem repique,
        assume risco de breakout e reduz exposição.
        """
        if not bool(getattr(config, "RANGE_SCALP_EARLY_EXIT_ENABLED", True)):
            return False

        try:
            mid_price = float(range_mid_price)
        except (TypeError, ValueError):
            return False

        timeframe = str(getattr(config, "RANGE_SCALP_EARLY_EXIT_TIMEFRAME", "3m") or "3m")
        try:
            klines = self.exchange.get_klines(symbol=symbol, interval=timeframe, limit=4)
        except Exception as e:
            logger.warning(f"⚠️ Falha ao avaliar saída antecipada de range em {symbol}: {e}")
            return False

        if not klines or len(klines) < 2:
            return False

        closes = [float(item.get("close", 0.0) or 0.0) for item in klines][-2:]
        if len(closes) < 2:
            return False

        if side == "LONG":
            return (closes[0] < mid_price and closes[1] < mid_price and closes[1] <= closes[0])
        return (closes[0] > mid_price and closes[1] > mid_price and closes[1] >= closes[0])
    
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
        # Proteção contra phantom closes: se a API falhar, PULAMOS este tick.
        # Tratar erro como "todas as posições fecharam" corrompe stats e envia
        # notificações falsas. Incidente reproduzido em 2026-04-20 com erro
        # transitório -1021 (timestamp skew) — documentado em commit dedicado.
        try:
            positions = self.exchange.get_open_positions()
        except Exception as exc:
            logger.warning(
                f"⚠️ API indisponível ao listar posições — pulando monitor tick "
                f"(preservando known_positions): {exc}"
            )
            metrics.record_api_error(
                endpoint="futures_position_information",
                code=getattr(exc, "code", "unknown"),
            )
            return

        # DETECTA POSIÇÕES FECHADAS PELA BINANCE
        # Cria set de posições atuais e atualiza known_positions sob lock
        current_position_keys = set()
        with self._positions_lock:
            for pos in positions:
                position_key = f"{pos['symbol']}_{pos['side']}"
                current_position_keys.add(position_key)
                previous = self.known_positions.get(position_key, {})

                self.known_positions[position_key] = {
                    'symbol': pos['symbol'],
                    'side': pos['side'],
                    'entry_price': pos['entry_price'],
                    'quantity': pos['quantity'],
                    'last_seen': datetime.now(),
                    'strategy_name': previous.get('strategy_name', 'primary'),
                    'strategy_type': previous.get('strategy_type', 'trend_signal'),
                    'custom_stop_loss': previous.get('custom_stop_loss'),
                    'custom_take_profit': previous.get('custom_take_profit'),
                    'range_mid_price': previous.get('range_mid_price'),
                    'range_entry_side': previous.get('range_entry_side'),
                    'trailing_activation_pct': previous.get('trailing_activation_pct'),
                    'trailing_distance_pct': previous.get('trailing_distance_pct'),
                }

            # Verifica se alguma posição conhecida sumiu (snapshot sob lock).
            # CLAIM ATÔMICO: remove de known_positions JÁ aqui, dentro do lock.
            # Antes a remoção era depois do processamento (fora do lock); um
            # restart na janela entre registrar o fechamento e remover faria o
            # monitor re-disparar o mesmo close no reboot (double-count). Ao
            # remover no snapshot, o "visto como fechado" vira one-shot. A
            # limpeza de trailing (positions.close) ainda roda após processar.
            closed_by_binance = []
            for pk in list(self.known_positions.keys()):
                if pk not in current_position_keys:
                    closed_by_binance.append((pk, dict(self.known_positions[pk])))
                    self.known_positions.pop(pk, None)

        # Processa posições fechadas pela Binance (fora do lock — pode fazer chamadas de API)
        for position_key, pos_info in closed_by_binance:
            logger.warning(f"⚠️ Posição {position_key} foi FECHADA pela Binance (SL/TP)")

            # Busca o P&L real da Binance
            self._process_binance_closed_position(pos_info)

            # known_positions já foi removido no claim acima; isto limpa o
            # trailing/peak e é no-op no remove (pop com default).
            self.positions.close(position_key)
        
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
            
            if side == "LONG":
                profit_pct = ((current_price - entry_price) / entry_price) * 100
            else:  # SHORT
                profit_pct = ((entry_price - current_price) / entry_price) * 100
            
            logger.info(f"   {side} {symbol}: P&L ${pnl:.2f} ({profit_pct:+.2f}%) | Preço: ${current_price:.4f}")

            known_meta = self._get_known_position(position_key)
            custom_take_profit = known_meta.get('custom_take_profit')
            custom_stop_loss = known_meta.get('custom_stop_loss')
            strategy_type = self._normalize_strategy_type(known_meta.get('strategy_type', 'trend_signal'))
            range_mid_price = known_meta.get('range_mid_price')

            # 0. TAKE PROFIT / STOP LOSS CUSTOM (por estratégia)
            custom_tp_hit = bool(
                custom_take_profit is not None and (
                    (side == "LONG" and current_price >= float(custom_take_profit)) or
                    (side == "SHORT" and current_price <= float(custom_take_profit))
                )
            )
            if custom_tp_hit:
                pos['current_price'] = current_price
                closed = self._close_position_with_notification(
                    pos,
                    f"Take Profit custom ({float(custom_take_profit):.4f})"
                )
                if closed:
                    self.positions.close(position_key)
                else:
                    logger.warning(
                        f"⚠️ Fechamento não confirmado para {position_key} em TP custom. "
                        "Mantendo rastreamento da posição."
                    )
                continue

            custom_sl_hit = bool(
                config.USE_INDIVIDUAL_STOP_LOSS and custom_stop_loss is not None and (
                    (side == "LONG" and current_price <= float(custom_stop_loss)) or
                    (side == "SHORT" and current_price >= float(custom_stop_loss))
                )
            )
            if custom_sl_hit:
                pos['current_price'] = current_price
                closed = self._close_position_with_notification(
                    pos,
                    f"Stop Loss custom ({float(custom_stop_loss):.4f})"
                )
                if closed:
                    self.positions.close(position_key)
                else:
                    logger.warning(
                        f"⚠️ Fechamento não confirmado para {position_key} em SL custom. "
                        "Mantendo rastreamento da posição."
                    )
                continue

            if strategy_type == "range_scalping" and self._should_force_exit_range_break(
                symbol=symbol,
                side=side,
                range_mid_price=range_mid_price,
            ):
                pos['current_price'] = current_price
                closed = self._close_position_with_notification(
                    pos,
                    "Saída antecipada por possível breakout de range"
                )
                if closed:
                    self.positions.close(position_key)
                else:
                    logger.warning(
                        f"⚠️ Fechamento não confirmado para {position_key} em saída antecipada de range."
                    )
                continue
            
            # 1. VERIFICA TAKE PROFIT GLOBAL
            # Só dispara quando NÃO há custom_take_profit. Caso contrário o
            # TP do risk_profile (ex: 1.8%) seria preempted pelo TP_PERCENT
            # global (1.5%), invalidando o RR configurado.
            if custom_take_profit is None and profit_pct >= config.TAKE_PROFIT_PERCENT:
                logger.info(f"🎯 Take Profit atingido! {profit_pct:.2f}% >= {config.TAKE_PROFIT_PERCENT}%")
                pos['current_price'] = current_price
                closed = self._close_position_with_notification(
                    pos,
                    f"Take Profit ({config.TAKE_PROFIT_PERCENT}%)"
                )
                if closed:
                    self.positions.close(position_key)
                else:
                    logger.warning(
                        f"⚠️ Fechamento não confirmado para {position_key} em Take Profit. "
                        "Mantendo rastreamento da posição."
                    )
                continue
            
            # 2. VERIFICA TRAILING STOP (se ativado)
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
                        self.positions.close(position_key)
                    else:
                        logger.warning(
                            f"⚠️ Fechamento não confirmado para {position_key} via trailing. "
                            "Mantendo rastreamento da posição."
                        )
                    continue
            
            # 3. VERIFICA STOP LOSS GLOBAL
            # Só dispara quando NÃO há custom_stop_loss. Caso contrário o SL
            # global (default 5%) ignoraria o SL apertado do risk_profile
            # (ex: 0.6%) e deixaria a posição correr até -5%.
            if (
                config.USE_INDIVIDUAL_STOP_LOSS
                and custom_stop_loss is None
                and profit_pct <= -config.STOP_LOSS_PERCENT
            ):
                pos['current_price'] = current_price
                closed = self._close_position_with_notification(
                    pos,
                    f"Stop Loss ({config.STOP_LOSS_PERCENT}%)"
                )
                if closed:
                    self.positions.close(position_key)
                else:
                    logger.warning(
                        f"⚠️ Fechamento não confirmado para {position_key} via stop loss. "
                        "Mantendo rastreamento da posição."
                    )
                continue
        
        logger.info(f"💵 P&L Total não realizado: ${total_pnl:.2f}")
    
    def _fetch_realized_pnl_with_retry(self, symbol, start_time_ms, attempts=3, delay=2.0):
        """Busca o REALIZED_PNL agregado da Binance, com retry.

        O income REALIZED_PNL aparece com latência de alguns segundos após o
        fill — a 1ª query costuma vir vazia. Sem retry, o caller caía no fallback
        que estima o P&L pelo preço ATUAL, fabricando o resultado quando o preço
        se moveu após o fechamento (XRP #37, 20/06: fechou ~zero/loss, preço
        quicou pra +0.84%, fallback registrou +$0.44 "Take Profit"). Tenta algumas
        vezes antes de desistir. Retorna o gross (float) ou None se não apareceu.
        """
        for i in range(1, int(attempts) + 1):
            try:
                income_list = self.exchange.get_income_history(
                    income_type='REALIZED_PNL', symbol=symbol, limit=100,
                    start_time=start_time_ms,
                )
                gross = _aggregate_realized_pnl(income_list)
            except Exception as exc:
                logger.error(f"❌ Erro ao buscar P&L da Binance para {symbol}: {exc}")
                gross = None
            if gross is not None:
                if i > 1:
                    logger.info(f"   ✓ REALIZED_PNL de {symbol} disponível na tentativa {i}")
                return gross
            if i < int(attempts):
                time.sleep(delay)
        return None

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
        
        # Usa entry_time para filtrar income apenas deste trade (evita pegar PnL de trade anterior)
        entry_time = pos_info.get('entry_time')
        start_time_ms = int(entry_time.timestamp() * 1000) if entry_time else None

        # O REALIZED_PNL tem latência de alguns segundos após o fill — busca com
        # retry antes de cair no fallback (ver _fetch_realized_pnl_with_retry).
        pnl_gross = self._fetch_realized_pnl_with_retry(symbol, start_time_ms)
        if pnl_gross is None:
            logger.warning(
                f"⚠️ REALIZED_PNL não encontrado para {symbol} após retries — "
                f"usando estimativa por preço atual (P&L pode ficar impreciso)"
            )

        taker_fee_rate = self.get_taker_fee_rate()
        notional = entry_price * quantity
        total_fees = notional * taker_fee_rate * 2

        if pnl_gross is None:
            # Fallback: estima P&L com base no preço atual (igual ao _close_position_with_notification)
            try:
                close_price = self.exchange.get_current_price(symbol)
                if side == 'LONG':
                    pnl_gross = (close_price - entry_price) * quantity
                else:
                    pnl_gross = (entry_price - close_price) * quantity
                logger.info(f"   📐 P&L estimado via preço atual ${close_price:.4f}: ${pnl_gross:.4f}")
            except Exception:
                pnl_gross = 0.0
                logger.warning(f"   ⚠️ Não foi possível estimar P&L para {symbol} — registrando como $0")

        pnl_net = pnl_gross - total_fees

        logger.info("📊 P&L encontrado na Binance:")
        logger.info(f"   P&L Bruto: ${pnl_gross:.4f}")
        logger.info(f"   Taxas: ${total_fees:.4f}")
        logger.info(f"   P&L Líquido: ${pnl_net:.4f}")

        # exit_price IMPLÍCITO pelo gross agregado (não é fill real; ver helper).
        # Consistente com pnl por construção agora que o gross soma todos os fills.
        exit_price = _implied_exit_price(side, entry_price, pnl_gross, quantity)

        pos_meta = self._get_known_position(f"{symbol}_{side}")

        # Motivo do fechamento: infere pela proximidade do preço de saída ao TP/SL
        # da posição. Antes etiquetava só pelo sinal do P&L (gross>0 ⇒ "Take
        # Profit"), o que rotulava uma saída de trailing/breakeven como TP mesmo
        # fechando longe do alvo (caso SOL 13/06: TP 69.84, saiu em ~68.64 → era
        # trailing, não TP).
        reason = self._infer_exchange_close_reason(
            side=side,
            exit_price=exit_price,
            take_profit=pos_meta.get("custom_take_profit"),
            stop_loss=pos_meta.get("custom_stop_loss"),
            pnl_gross=pnl_gross,
        )
        strat_key = pos_meta.get('strategy_name')
        if not strat_key:
            strat_key = self._resolve_strategy_context(symbol).get('name', 'primary')

        # Bookkeeping unificado via ledger: contadores + enriquecimento do
        # trade_history (exit_price/pnl/motivo) + métrica num ponto só. Antes
        # este path duplicava os contadores inline e NÃO fechava a entrada no
        # trade_history, deixando trades "Aberta" pra sempre (fantasmas).
        with self._runtime_stats_lock:
            self.risk_manager.update_pnl(pnl_net)
            self.ledger.record_trade_closed(
                symbol=symbol,
                strategy_name=strat_key,
                pnl_net=pnl_net,
                total_fees=total_fees,
                close_reason=reason,
                side=side,
                entry_price=entry_price,
                exit_price=exit_price,
                pnl_gross=pnl_gross,
            )

        result = "LUCRO 🟢" if pnl_net > 0 else "PREJUÍZO 🔴"
        
        # Log
        logger.info(f"💰 {result}: ${pnl_net:.4f} | Motivo: {reason}")
        
        self.telegram.send_message(
            f"⚠️ <b>POSIÇÃO FECHADA PELA BINANCE</b>\n\n"
            f"📍 <b>Par:</b> {symbol.replace('USDT', '')}/USDT\n"
            f"📊 <b>Lado:</b> {side}\n"
            f"🤖 <b>Estratégia:</b> {strat_key}\n"
            f"📝 <b>Motivo:</b> {reason}\n\n"
            f"<b>💵 RESULTADO:</b>\n"
            f"   • P&L Bruto: <code>${pnl_gross:+.4f}</code>\n"
            f"   • Taxas: <code>-${total_fees:.4f}</code>\n"
            f"   • <b>P&L Líquido: <code>${pnl_net:+.4f}</code></b>"
        )
    
    def _get_position_trailing_params(self, position_key: str) -> Tuple[float, float]:
        """
        Retorna (activation_pct, distance_pct) para uma posição.

        Prefere os valores armazenados na posição (computados via ATR no open).
        Cai pra config global quando a posição não tem (reconciliação sem
        metadata, posições pré-feature, ou USE_ATR_TRAILING desabilitado).
        """
        meta = self._get_known_position(position_key)
        activation = meta.get('trailing_activation_pct')
        distance = meta.get('trailing_distance_pct')
        if activation is None or distance is None:
            return (
                float(config.TRAILING_ACTIVATION_PERCENT),
                float(config.TRAILING_DISTANCE_PERCENT),
            )
        return (float(activation), float(distance))

    def _infer_exchange_close_reason(
        self,
        *,
        side: str,
        exit_price: Optional[float],
        take_profit: Optional[float],
        stop_loss: Optional[float],
        pnl_gross: float,
    ) -> str:
        """Infere o motivo de um fechamento server-side da Binance.

        Usa a proximidade do preço de saída ao TP/SL: bateu no alvo ⇒ Take
        Profit; bateu no stop ⇒ Stop Loss; fechou ENTRE os dois ⇒ o trailing/
        breakeven moveu o stop (Trailing Stop), não foi o TP. Fallback pelo
        sinal do P&L quando não há preço/níveis confiáveis.
        """
        tol = 0.0015  # 0.15% — tolera o ruído do exit_price implícito
        try:
            ep = float(exit_price) if exit_price else None
        except (TypeError, ValueError):
            ep = None
        tp = float(take_profit) if take_profit else None
        sl = float(stop_loss) if stop_loss else None

        # Só infere por proximidade se temos preço E ao menos um nível (TP/SL).
        if ep and ep > 0 and (tp or sl):
            if side == "LONG":
                if tp and ep >= tp * (1 - tol):
                    return "Take Profit (Binance)"
                if sl and ep <= sl * (1 + tol):
                    return "Stop Loss (Binance)"
            else:  # SHORT
                if tp and ep <= tp * (1 + tol):
                    return "Take Profit (Binance)"
                if sl and ep >= sl * (1 - tol):
                    return "Stop Loss (Binance)"
            # Fechou entre SL e TP: trailing/breakeven moveu o stop server-side.
            return "Trailing Stop (Binance)"

        # Sem níveis confiáveis: não dá pra distinguir — cai no sinal do P&L.
        return "Take Profit (Binance)" if pnl_gross > 0 else "Stop Loss (Binance)"

    def _trailing_stop_price(
        self,
        side: str,
        entry_price: float,
        peak_price: float,
        distance_pct: Optional[float] = None,
    ) -> float:
        """
        Calcula o preço do trailing stop com piso de breakeven.

        Sem o piso, activation < distance deixa o stop cair abaixo da entrada —
        o trade sai com P&L bruto ≈ 0 e fees transformam em prejuízo
        (ver caso BNBUSDT 2026-04-20 20:00). O piso garante que, depois do
        trailing ativar, o pior cenário de saída é lucro ≥ fees round-trip.

        `distance_pct` (em %) sobrepõe o config global quando passado — usado
        pelo trailing por posição ancorado em ATR.
        """
        effective_pct = distance_pct if distance_pct is not None else config.TRAILING_DISTANCE_PERCENT
        distance = float(effective_pct) / 100.0
        # Piso: cobre fees round-trip + margem fixa + cushion de slippage do stop.
        # O cushion garante que, mesmo com o STOP_MARKET escorregando no fill, a
        # saída do trailing fique ≥ breakeven líquido (caso SOL 13/06).
        slip_cushion = float(getattr(config, "TRAILING_BREAKEVEN_SLIPPAGE_PERCENT", 0.0) or 0.0) / 100.0
        fee_floor = (self.get_taker_fee_rate() * 2.0) + 0.0005 + slip_cushion

        if side == "LONG":
            raw = peak_price * (1 - distance)
            breakeven_floor = entry_price * (1 + fee_floor)
            return max(raw, breakeven_floor)
        else:  # SHORT
            raw = peak_price * (1 + distance)
            breakeven_ceiling = entry_price * (1 - fee_floor)
            return min(raw, breakeven_ceiling)

    def _check_trailing_stop(
        self,
        position_key: str,
        side: str,
        entry_price: float,
        current_price: float,
        symbol: str,
        position_amt: float = 0.0
    ) -> tuple[bool, str]:
        """Retorna (should_close, reason) e atualiza peak_prices/trailing_activated."""
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
        
        if side == "LONG":
            # Para LONG, queremos o preço máximo
            if current_price > self.peak_prices[position_key]:
                self.peak_prices[position_key] = current_price
        else:  # SHORT
            # Para SHORT, queremos o preço mínimo
            if current_price < self.peak_prices[position_key]:
                self.peak_prices[position_key] = current_price
        
        peak_price = self.peak_prices[position_key]
        activation_pct, distance_pct = self._get_position_trailing_params(position_key)

        if not self.trailing_activated[position_key]:
            if profit_pct >= activation_pct:
                self.trailing_activated[position_key] = True

                trailing_stop_price = self._trailing_stop_price(
                    side, entry_price, peak_price, distance_pct=distance_pct,
                )

                logger.info(
                    f"🔔 Trailing Stop ATIVADO para {position_key}! "
                    f"(activation={activation_pct:.2f}% / distance={distance_pct:.2f}%)"
                )
                logger.info(f"   Pico: ${peak_price:.4f} | Stop em: ${trailing_stop_price:.4f}")
                
                trailing_pos_meta = self._get_known_position(position_key)
                trailing_strategy_name = trailing_pos_meta.get('strategy_name')
                if not trailing_strategy_name:
                    _tp = self._resolve_strategy_context(symbol)
                    trailing_strategy_name = _tp.get('name', 'primary')
                self.telegram.send_trailing_stop_activated(
                    symbol=symbol,
                    side=side,
                    entry_price=entry_price,
                    current_price=current_price,
                    trailing_stop_price=trailing_stop_price,
                    current_profit_pct=profit_pct,
                    strategy_name=trailing_strategy_name
                )
        
        if self.trailing_activated[position_key]:
            trailing_stop_price = self._trailing_stop_price(
                side, entry_price, peak_price, distance_pct=distance_pct,
            )
            if side == "LONG":
                price_hit = current_price <= trailing_stop_price
            else:  # SHORT
                price_hit = current_price >= trailing_stop_price

            if price_hit:
                # Fecha sempre que o stop for atingido. A activation_threshold já garante
                # que a posição estava lucrativa quando o trailing foi ativado.
                # Verificar um mínimo em USD aqui bloqueava o fechamento em posições pequenas
                # (ex: order=$3, leverage=10x → profit_usd < $0.20 na ativação) deixando a
                # posição aberta enquanto o preço continuava caindo.
                logger.info(f"   🎯 Trailing Stop atingido | lucro ${profit_usd:.4f} ({profit_pct:.3f}%)")
                return (True, f"Trailing Stop ({distance_pct:.2f}% do pico)")
            
            # Log do status do trailing
            logger.info(f"   🎯 Trailing ativo | Pico: ${peak_price:.4f} | Stop: ${trailing_stop_price:.4f} | Lucro: ${profit_usd:.4f}")
        
        return (False, "")
    
    def _clear_trailing_data(self, position_key: str):
        """Backward-compat: delegate ao PositionTracker."""
        self.positions.clear_trailing(position_key)
    
    def _close_position_with_notification(self, pos: dict, reason: str) -> bool:
        """Delegador — lógica real em ExecutionEngine (preserva API interna)."""
        return self.execution_engine.close_position_with_notification(pos, reason)

    def check_global_stop_loss(self) -> bool:
        """Delegador — lógica real em ExecutionEngine."""
        return self.execution_engine.check_global_stop_loss()

    # ------------------------------------------------------------------
    # Improvement 1: Drawdown protection from peak equity
    # ------------------------------------------------------------------

    def _update_peak_equity(self, current_balance: float) -> None:
        """Atualiza o pico histórico de equity."""
        if current_balance > self.peak_equity:
            self.peak_equity = current_balance
            self.peak_equity_ts = datetime.now(timezone.utc)

    def _check_drawdown_from_peak(self, current_balance: float) -> bool:
        """
        Retorna True se o drawdown desde o pico exceder o limite configurado.
        Quando True, novas entradas devem ser bloqueadas.
        """
        if self.peak_equity <= 0 or not getattr(config, "MAX_DRAWDOWN_FROM_PEAK_PERCENT", 0):
            return False
        drawdown_pct = (self.peak_equity - current_balance) / self.peak_equity * 100
        if drawdown_pct >= config.MAX_DRAWDOWN_FROM_PEAK_PERCENT:
            logger.warning(
                f"⛔ DRAWDOWN MÁXIMO ATINGIDO: {drawdown_pct:.1f}% desde pico "
                f"${self.peak_equity:.2f} → atual ${current_balance:.2f}"
            )
            return True
        return False

    # ------------------------------------------------------------------
    # Improvement 4: Max total notional exposure
    # ------------------------------------------------------------------

    def _get_total_open_notional_percent(self) -> float:
        """
        Retorna o notional total aberto como % do saldo da carteira.

        Em caso de erro de API, retorna **100.0** (conservador) — assim o caller
        que usa isso como gate pra abrir trade (MAX_TOTAL_NOTIONAL_PERCENT)
        automaticamente bloqueia. Retornar 0.0 em erro liberaria trades sem
        saber a exposição real — comportamento inseguro.
        """
        try:
            balance = self.exchange.get_account_balance()
            if balance <= 0:
                return 0.0
            positions = self.exchange.get_open_positions()
            total_notional = sum(
                pos['quantity'] * pos.get('mark_price', pos.get('entry_price', 0))
                for pos in positions
                if pos.get('mark_price', pos.get('entry_price', 0)) > 0
            )
            return (total_notional / balance) * 100
        except Exception as e:
            logger.warning(
                f"⚠️ Falha ao calcular notional total: {e}. "
                "Retornando 100% (conservador) — bloqueia nova entrada até API voltar."
            )
            return 100.0

    def execute_global_stop_loss(self):
        """Delegador — lógica real em ExecutionEngine."""
        self.execution_engine.execute_global_stop_loss()
    
    def print_status(self, send_telegram: bool = False):
        """
        Imprime o status atual do bot.
        Usa dados REAIS da Binance para P&L diário e saldo.

        Args:
            send_telegram: Se True, envia também para o Telegram
        """
        try:
            positions = self.exchange.get_open_positions()
        except Exception as exc:
            logger.warning(f"⚠️ API indisponível para status — pulando print: {exc}")
            return
        
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
            total_fees=self.total_fees_paid,
            trades_by_strategy=self.trades_by_strategy
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
        
        # Busca P&L diário real para o snapshot — subtrai o baseline (zera o
        # display após /reset e em rollover de dia UTC).
        daily_pnl_binance = self.exchange.get_daily_pnl_from_binance()
        daily_pnl_real = daily_pnl_binance['total'] - self.daily_pnl_binance_baseline

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

        # Persiste o snapshot no store durável (série completa de equity).
        if getattr(self, "trade_store", None) is not None:
            self.trade_store.record_equity(snapshot)

        # Mantém apenas os últimos 144 snapshots (24h se for a cada 10min)
        if len(self.portfolio_history) > 144:
            self.portfolio_history = self.portfolio_history[-144:]

        logger.info(f"📸 Snapshot capturado: P&L Total ${snapshot['pnl_total']:.2f}")

        self._maybe_send_drawdown_alert(snapshot, now)

    def _maybe_send_drawdown_alert(self, snapshot: dict, now: datetime) -> None:
        """Dispara alerta Telegram quando o drawdown intraday cruza um bucket novo.

        Existe pra dar visibilidade do drawdown ao usuário antes que ele entre
        em pânico e mande /closeall no fundo. Reseta o bucket ao virar do dia.
        """
        if not getattr(config, "DRAWDOWN_ALERT_ENABLED", True):
            return
        buckets = list(getattr(config, "DRAWDOWN_ALERT_BUCKETS_PERCENT", []) or [])
        if not buckets:
            return
        try:
            initial_capital = float(self.initial_capital or 0.0)
        except (TypeError, ValueError):
            initial_capital = 0.0
        if initial_capital <= 0:
            return

        total_pnl = float(snapshot.get("pnl_total", 0.0) or 0.0)
        # Só alerta em drawdown (PnL negativo).
        if total_pnl >= 0:
            # Reset do bucket quando volta pra positivo.
            self._drawdown_alert_bucket_pct = 0.0
            return

        drawdown_pct = abs(total_pnl) / initial_capital * 100.0

        # Reset diário automático.
        today_key = now.strftime("%Y-%m-%d")
        if self._drawdown_alert_day != today_key:
            self._drawdown_alert_day = today_key
            self._drawdown_alert_bucket_pct = 0.0

        # Maior bucket cruzado.
        crossed = [b for b in buckets if drawdown_pct >= b]
        if not crossed:
            return
        highest = max(crossed)

        if highest <= self._drawdown_alert_bucket_pct:
            return  # Já alertou esse nível ou mais alto

        self._drawdown_alert_bucket_pct = highest

        global_sl_pct = float(getattr(config, "GLOBAL_STOP_LOSS_PERCENT", 0.0) or 0.0)
        daily_loss_usd = float(getattr(config, "DAILY_LOSS_LIMIT", 0.0) or 0.0)
        global_sl_usd = (global_sl_pct / 100.0) * initial_capital if global_sl_pct > 0 else 0.0
        unrealized = float(snapshot.get("pnl_unrealized", 0.0) or 0.0)
        realized = float(snapshot.get("pnl_realized", 0.0) or 0.0)

        try:
            self.telegram.send_message(
                f"🟡 <b>DRAWDOWN INTRADAY</b>\n\n"
                f"📉 PnL total: <code>${total_pnl:+.2f}</code> "
                f"(<code>-{drawdown_pct:.2f}%</code> do capital)\n"
                f"   • Realizado: <code>${realized:+.2f}</code>\n"
                f"   • Não realizado: <code>${unrealized:+.2f}</code>\n\n"
                f"<b>Gates automáticos (ainda dentro):</b>\n"
                f"• Global SL: <code>-{global_sl_pct:.0f}%</code> "
                f"(≈ <code>-${global_sl_usd:.2f}</code>)\n"
                f"• Daily Loss: <code>-${daily_loss_usd:.2f}</code>\n\n"
                f"⏳ Drawdown intraday é normal. Aguarde a estratégia trabalhar — "
                f"fechar no fundo congela a perda.\n\n"
                f"Use /portfolio pra detalhe ou /positions pra ver posições."
            )
        except Exception as exc:
            logger.warning("Falha enviando alerta de drawdown: %s", exc)

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
        wallet_balance = account_info['wallet_balance']  # Saldo total da carteira (cappado em testnet)
        total_unrealized = account_info['unrealized_pnl']  # P&L não realizado

        # Realizado ACUMULADO (todos os dias), do TradeStore durável — não zera
        # na virada do dia UTC nem no /reset diário. MESMA fonte do dashboard
        # (web/data.py collect_summary). Antes este card usava só o realizado do
        # DIA (daily_pnl_real), então "Total"/"Atual" voltavam pro capital
        # inicial toda virada de dia e divergiam do dashboard. Fallback no
        # contador interno do bot quando não há store.
        cumulative_realized = float(getattr(self, "total_pnl", 0.0) or 0.0)
        _store = getattr(self, "trade_store", None)
        if _store is not None:
            try:
                cumulative_realized = float(_store.cumulative_realized_pnl())
            except Exception:
                pass

        # P&L total = realizado ACUMULADO + não realizado das posições abertas.
        total_pnl = cumulative_realized + total_unrealized

        # Capital ATUAL = equity efetivo. Em testnet com SIMULATED_BALANCE_USD,
        # o wallet retorna o cap fixo — adicionamos o realizado ACUMULADO +
        # unrealized pra refletir o progresso real do bot. Em mainnet ou testnet
        # sem cap, wallet_balance já reflete o realizado; adicionamos só o
        # unrealized pra ter equity total.
        sim_cap = float(getattr(config, "SIMULATED_BALANCE_USD", 0.0) or 0.0)
        if getattr(config, "USE_TESTNET", False) and sim_cap > 0:
            balance = sim_cap + cumulative_realized + total_unrealized
        else:
            balance = wallet_balance + total_unrealized

        pct_change = (total_pnl / self.initial_capital) * 100 if self.initial_capital > 0 else 0
        
        # Prepara dados do histórico para o Telegram.
        # Fonte: curva do realizado ACUMULADO (net) do TradeStore — um ponto por
        # trade fechado. O último ponto é igual ao "Realizado" do topo
        # (cumulative_realized), então o histórico FECHA com o topo. Antes a série
        # vinha de portfolio_history (income DIÁRIO da Binance, c/ taxas/baseline),
        # que divergia do topo e tinha valores repetidos/parados entre snapshots.
        history_data = []
        _store = getattr(self, "trade_store", None)
        if _store is not None:
            try:
                for point in _store.realized_curve(limit=6):
                    exit_dt = point.get("exit_at")
                    if isinstance(exit_dt, str):
                        try:
                            exit_dt = datetime.fromisoformat(exit_dt)
                        except ValueError:
                            exit_dt = None
                    if exit_dt is None:
                        label = now_brt.strftime("%H:%M")
                    else:
                        # Naive == UTC (mesma convenção dos snapshots).
                        if exit_dt.tzinfo is None:
                            exit_dt = exit_dt.replace(tzinfo=timezone.utc)
                        label = exit_dt.astimezone(BRT).strftime("%H:%M")
                    history_data.append({
                        'time': label,
                        'pnl': float(point.get("cum_pnl", 0.0) or 0.0),
                    })
            except Exception:
                logger.exception("Falha ao montar histórico de realizado para o card")
                history_data = []

        # Envia para o Telegram com estatísticas de trades
        self.telegram.send_portfolio_evolution(
            initial_capital=self.initial_capital,
            current_balance=balance,  # Saldo REAL da Binance
            total_pnl=total_pnl,
            pnl_realized=cumulative_realized,  # Realizado ACUMULADO (igual ao dashboard)
            pnl_unrealized=total_unrealized,
            pct_change=pct_change,
            closed_trades=self.closed_trades_count,
            trades_win_count=self.trades_win_count,
            trades_loss_count=self.trades_loss_count,
            trades_win_total=self.trades_win_total,
            trades_loss_total=self.trades_loss_total,
            history=history_data,
            bot_start_time=self.start_time if hasattr(self, 'start_time') else now_brt,
            trades_by_strategy=self.trades_by_strategy if self.trades_by_strategy else None
        )

    def set_strategy_enabled(self, name: str, enabled: bool) -> str:
        """
        Ativa ou desativa uma estratégia pelo nome em runtime.

        Retorna mensagem de resultado para exibir no Telegram.
        """
        profiles = list(getattr(config, "STRATEGY_PROFILES", []) or [])
        target = next((p for p in profiles if p.get("name") == name), None)
        if target is None:
            available = ", ".join(p.get("name", "?") for p in profiles)
            return f"❌ Estratégia <code>{name}</code> não encontrada.\nDisponíveis: <code>{available}</code>"

        current = bool(target.get("enabled", True))
        if current == enabled:
            state = "ativa" if enabled else "desativada"
            return f"ℹ️ Estratégia <code>{name}</code> já está {state}."

        target["enabled"] = enabled
        config.STRATEGY_PROFILES = profiles
        self._sync_strategy_profiles_with_trading_pairs(
            reason=f"strategy-{'enable' if enabled else 'disable'}"
        )
        self.save_state()

        state = "✅ ativada" if enabled else "⏸️ desativada"
        pairs = target.get("pairs") or []
        pairs_info = f"{len(pairs)} pares fixos" if pairs else "seleção automática"
        return (
            f"{state} — <b>{name}</b>\n"
            f"📋 Tipo: <code>{target.get('strategy_type', '?')}</code>\n"
            f"🪙 Pares: <code>{pairs_info}</code>"
        )

    def list_strategies(self) -> str:
        """Retorna resumo das estratégias configuradas para exibir no Telegram."""
        profiles = list(getattr(config, "STRATEGY_PROFILES", []) or [])
        if not profiles:
            return "⚠️ Nenhuma estratégia configurada."

        # Pares em uso por cada perfil ativo (runtime)
        runtime_pairs: dict = {
            rp.get("name"): rp.get("pairs", [])
            for rp in (getattr(self, "strategy_profiles", []) or [])
        }

        lines = ["📋 <b>ESTRATÉGIAS</b>\n"]
        for p in profiles:
            name = p.get("name", "?")
            enabled = bool(p.get("enabled", True))
            stype = p.get("strategy_type", "?")
            config_pairs = p.get("pairs") or []
            icon = "✅" if enabled else "⏸️"

            if enabled:
                active_pairs = runtime_pairs.get(name, [])
                display_pairs = active_pairs or config_pairs
                if display_pairs:
                    symbols = ", ".join(s.replace("USDT", "") for s in display_pairs)
                    pairs_info = f"{len(display_pairs)} pares ativos: <code>{symbols}</code>"
                else:
                    pairs_info = f"seleção automática (max {p.get('max_pairs', '?')})"
            else:
                if config_pairs:
                    symbols = ", ".join(s.replace("USDT", "") for s in config_pairs)
                    pairs_info = f"{len(config_pairs)} pares configurados: <code>{symbols}</code>"
                else:
                    pairs_info = f"seleção automática (max {p.get('max_pairs', '?')})"

            lines.append(f"{icon} <b>{name}</b> — <code>{stype}</code>\n   🪙 {pairs_info}")

        lines.append("\n<i>Use /strategy enable|disable &lt;nome&gt;</i>")
        return "\n".join(lines)

    @staticmethod
    def _classify_api_health_status(
        failures: int,
        failure_rate: float,
        order_failures: int,
        order_rejection_rate: float,
        loop_errors: int,
        has_issues: bool,
    ) -> str:
        """
        Classifica a saúde operacional em CRÍTICO / ATENÇÃO / ESTÁVEL.

        Thresholds são proporcionais ao ruído real esperado em produção:
        - API: CRÍTICO se falhas > 10 OU failure_rate >= 1.0% (uma falha solta
          em 80k+ calls não deve disparar alarme)
        - Ordens: CRÍTICO se falhas > 5 OU rejection_rate >= 5.0%
        - Runtime: CRÍTICO se loop_errors > 0 (sempre indica bug/exceção)

        Qualquer outro sinal de instabilidade (retries, overruns) → ATENÇÃO.
        """
        api_critical = failures > 10 or failure_rate >= 1.0
        orders_critical = order_failures > 5 or order_rejection_rate >= 5.0
        runtime_critical = loop_errors > 0

        if api_critical or orders_critical or runtime_critical:
            return "CRÍTICO"
        if has_issues:
            return "ATENÇÃO"
        return "ESTÁVEL"

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

            status = self._classify_api_health_status(
                failures=failures,
                failure_rate=failure_rate,
                order_failures=order_failures,
                order_rejection_rate=order_rejection_rate,
                loop_errors=loop_errors,
                has_issues=has_issues,
            )
            emoji = {"CRÍTICO": "🔴", "ATENÇÃO": "🟡", "ESTÁVEL": "🟢"}.get(status, "🟢")

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
        analysis_cycle = 0  # contador exibido no log ao iniciar cada ciclo de análise

        # Sobe o dashboard web (no-op se DASHBOARD_ENABLED=False ou credenciais
        # ausentes). Falha aqui não bloqueia o bot — apenas loga e segue.
        try:
            if getattr(config, "DASHBOARD_ENABLED", False):
                from ..web import DashboardServer
                self.dashboard_server = DashboardServer(self)
                self.dashboard_server.start()
        except Exception:
            logger.exception("📊 Falha ao iniciar dashboard — bot segue sem ele")

        # Configuração inicial dos dois ciclos (com ajuste dinâmico por faixa)
        timing_profile = get_loop_timing_profile(config, len(config.TRADING_PAIRS))
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
        # Intervalo de rescore de pares respeita config.PAIR_UPDATE_INTERVAL_MINUTES,
        # com piso de 60s pra evitar loop infinito em configs erradas.
        self._pair_update_interval = max(
            60,
            int(getattr(config, "PAIR_UPDATE_INTERVAL_MINUTES", 360) or 360) * 60,
        )

        # Tarefas periódicas simples vão pro LoopScheduler.
        # Monitor/analysis/pair_update ficam como state machine ou externos.
        self._loop_scheduler = LoopScheduler()
        self._loop_scheduler.add("terminal_status", base_interval * 3)
        self._loop_scheduler.add("state_save", base_interval * 30)
        self._loop_scheduler.add("commission_update", base_interval * 360)
        self._loop_scheduler.add(
            "deposit_check",
            max(5, int(getattr(config, "CAPITAL_TRANSFER_CHECK_INTERVAL_SECONDS", 60) or 60)),
        )
        self._loop_scheduler.add("strategy_check", base_interval * 60)

        now = time.monotonic()
        next_monitor_time = now
        next_analysis_cycle_time = now
        next_analysis_step_time = now
        self.next_pair_update_time = now + self._pair_update_interval

        analysis_cycle_active = False
        analysis_tasks = []
        analysis_index = 0
        
        # Inicia polling de comandos do Telegram
        logger.info("🎮 Iniciando polling de comandos Telegram...")
        self.command_handler.start_polling()
        
        logger.info("🏁 Bot iniciado! Pressione CTRL+C para parar.")
        logger.info("📱 Use /help no Telegram para ver comandos disponíveis.")
        
        self.telegram.send_startup_message(
            pairs=config.TRADING_PAIRS,
            capital=self.initial_capital,
            leverage=config.LEVERAGE
        )
        
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

                new_timing_profile = get_loop_timing_profile(config, len(config.TRADING_PAIRS))
                if timing_profile_changed(timing_profile, new_timing_profile):
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

                # CICLO RÁPIDO: MONITORAMENTO DE POSIÇÕES
                if now >= next_monitor_time:
                    monitor_started_at = time.monotonic()

                    if self.paused:
                        logger.info("⏸️  Bot PAUSADO - Apenas monitorando posições")

                    self.monitor_positions()
                    self.check_daily_targets()

                    # Improvement 1: atualiza peak equity e verifica drawdown
                    try:
                        _current_bal = self.exchange.get_account_balance()
                        if _current_bal > 0:
                            self.last_known_balance = _current_bal
                            self._update_peak_equity(_current_bal)
                            metrics.update_account_balance(_current_bal)
                            if self.peak_equity > 0:
                                metrics.update_drawdown(
                                    (self.peak_equity - _current_bal) / self.peak_equity * 100
                                )
                            if getattr(self, "dashboard_server", None):
                                self.dashboard_server.emit_balance_update({
                                    "balance": _current_bal,
                                    "peak_equity": self.peak_equity,
                                })
                    except Exception:
                        pass

                    # Kill switches de risco — alertas + pausa automática
                    if getattr(self, 'kill_switch', None) is not None:
                        try:
                            self.kill_switch.check_all(bot=self)
                        except Exception as _exc:
                            logger.warning(f"⚠️ KillSwitch check falhou: {_exc}")

                    # Snapshot de métricas Prometheus (gauges read-only)
                    metrics.update_bot_state(self)
                    try:
                        metrics.update_positions(self.exchange.get_open_positions())
                    except Exception:
                        pass
                    try:
                        if hasattr(self.exchange, "get_ws_stats"):
                            metrics.update_ws_stats(self.exchange.get_ws_stats())
                    except Exception:
                        pass
                    try:
                        if hasattr(self.exchange, "get_user_stream_stats"):
                            metrics.update_user_stream_stats(self.exchange.get_user_stream_stats())
                    except Exception:
                        pass

                    if self.check_global_stop_loss():
                        self.execute_global_stop_loss()
                        break

                    # Snapshot periódico da carteira
                    self.take_portfolio_snapshot()

                    # Status periódico somente no terminal
                    if self._loop_scheduler.due("terminal_status", now):
                        self.print_status(send_telegram=False)
                        self._loop_scheduler.mark_ran("terminal_status", now)

                    # Relatórios e manutenção periódica
                    self._maybe_send_daily_performance_report()

                    if self._loop_scheduler.due("state_save", now):
                        self.save_state()
                        self._loop_scheduler.mark_ran("state_save", now)

                    if self._loop_scheduler.due("commission_update", now):
                        self.update_commission_rates()
                        self._loop_scheduler.mark_ran("commission_update", now)

                    if now >= self.next_pair_update_time:
                        if config.USE_BINANCE_STRATEGY:
                            self.update_binance_strategy_coins()
                        else:
                            self.update_trading_pairs()
                        self.next_pair_update_time = now + self._pair_update_interval

                    if self._loop_scheduler.due("deposit_check", now):
                        self.check_for_deposit()
                        self._loop_scheduler.mark_ran("deposit_check", now)

                    if config.USE_BINANCE_STRATEGY and self._loop_scheduler.due("strategy_check", now):
                        self.check_and_update_binance_strategy()
                        self._loop_scheduler.mark_ran("strategy_check", now)

                    next_monitor_time = now + monitor_interval
                    monitor_duration = time.monotonic() - monitor_started_at
                    self._record_loop_timing(
                        loop_type='monitor',
                        duration_seconds=monitor_duration,
                        target_interval_seconds=monitor_interval
                    )

                # CICLO LENTO: ANÁLISE DE ENTRADAS
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

                        if analysis_index >= len(analysis_tasks):
                            analysis_cycle_active = False
                            next_analysis_cycle_time = now + analysis_cycle_interval
                            # Fim do ciclo: todos os pares foram classificados.
                            # Troca slots ociosos non-trend por melhores candidatos
                            # (throttle natural via cooldown por símbolo).
                            try:
                                self._maybe_swap_non_trend_pairs()
                            except Exception as _swap_exc:
                                logger.warning(f"⚠️ regime-swap falhou: {_swap_exc}")

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
        # Fecha o store durável (checkpoint do WAL). Best-effort.
        if getattr(self, "trade_store", None) is not None:
            self.trade_store.close()

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

        # Encerra WebSocket streams pra não vazar threads
        if hasattr(self, 'exchange') and hasattr(self.exchange, 'shutdown'):
            try:
                self.exchange.shutdown()
            except Exception as e:
                logger.warning(f"⚠️ Erro ao encerrar WebSocket: {e}")

        # Mantém posições abertas e apenas coleta estado atual para resumo
        logger.info("📌 Mantendo posições abertas para retomar no próximo start.")
        try:
            # force_refresh: resumo final — cache stale dá número errado
            positions = self.exchange.get_open_positions(force_refresh=True)
        except Exception as exc:
            logger.warning(f"⚠️ API indisponível no shutdown summary: {exc}")
            positions = []
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
        
        logger.info("💾 Salvando estado...")
        self.save_state()

        # Mensagem de shutdown é best-effort (send_message tem timeout=10 + retry).
        # Envolto em try pra NUNCA impedir o exit determinístico abaixo.
        try:
            self.telegram.send_shutdown_message(
                total_pnl=self.total_pnl + total_unrealized,
                total_trades=self.closed_trades_count
            )
        except Exception as exc:
            logger.warning(f"⚠️ Falha ao enviar shutdown message: {exc}")

        # Exit determinístico: o ThreadedWebsocketManager do python-binance
        # (asyncio + threads não-daemon) nem sempre encerra no .stop(), deixando
        # o processo ZUMBI após "Estado salvo" — o wrapper run_bot_loop.sh então
        # não respawna (#131). Estado já salvo atomicamente acima, então forçamos.
        self._force_exit(0)

    def _force_exit(self, code: int = 0) -> None:
        """Encerra o processo de forma determinística após o shutdown (#131).

        Necessário porque threads não-daemon (TWM/websocket) podem manter o
        interpretador vivo mesmo após o stop gracioso, impedindo o respawn pelo
        wrapper. Como o estado já foi persistido, `os._exit` é seguro aqui.
        Pulado sob pytest (PYTEST_CURRENT_TEST) pra não matar o test runner.
        """
        if os.environ.get("PYTEST_CURRENT_TEST"):
            logger.info("🧪 _force_exit pulado sob pytest.")
            return
        logger.info("🚪 Encerrando o processo (exit determinístico)...")
        try:
            logging.shutdown()
        except Exception:
            pass
        os._exit(code)


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
    print(f"   • Rede Binance: {config.ENVIRONMENT.upper()}{' (DINHEIRO REAL!)' if not config.USE_TESTNET else ' (dinheiro de teste)'}")
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
    
    bot = TradingBot()
    bot.run()


if __name__ == "__main__":
    main()

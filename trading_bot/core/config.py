"""
CONFIGURAÇÃO DO BOT DE TRADING
==============================
Ajuste os parâmetros abaixo de acordo com seu perfil de risco.
IMPORTANTE: Nunca compartilhe suas API keys!
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - fallback se dotenv não estiver instalado
    load_dotenv = None


logger = logging.getLogger(__name__)


def _load_environment():
    """
    Carrega variáveis de ambiente a partir de arquivo .env quando disponível.
    Prioridade:
    1) arquivo informado em TRADING_BOT_ENV_FILE
    2) .env na raiz do projeto
    3) .env.local na raiz do projeto
    """
    if load_dotenv is None:
        return

    project_root = Path(__file__).resolve().parents[2]
    explicit_env = os.getenv("TRADING_BOT_ENV_FILE", "").strip()

    if explicit_env:
        env_path = Path(explicit_env).expanduser()
        if not env_path.is_absolute():
            env_path = project_root / env_path
        load_dotenv(dotenv_path=env_path, override=False)
        return

    for candidate in (project_root / ".env", project_root / ".env.local"):
        if candidate.exists():
            load_dotenv(dotenv_path=candidate, override=False)


_load_environment()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_optional_int(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None:
        return None

    value = raw.strip()
    if not value or value == "0":
        return None

    try:
        return int(value)
    except ValueError:
        return None


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


@dataclass
class TradingConfig:
    """
    Classe de configuração principal do bot.
    Todos os parâmetros importantes ficam centralizados aqui.
    """
    
    # ============================================
    # CREDENCIAIS DA BINANCE (usar variáveis de ambiente é mais seguro)
    # ============================================
    API_KEY: str = os.getenv("BINANCE_API_KEY", "")
    API_SECRET: str = os.getenv("BINANCE_API_SECRET", "")
    
    # True = Testnet (dinheiro fake para testes)
    # False = Mainnet (dinheiro real - CUIDADO!)
    USE_TESTNET: bool = False

    # ============================================
    # AMBIENTE / RUNTIME / LOGGING
    # ============================================
    # Ambiente lógico de execução (dev, staging, prod)
    APP_ENV: str = os.getenv("TRADING_BOT_ENV", os.getenv("APP_ENV", "prod"))

    # Diretório para arquivos de runtime (state/lock/log)
    # Relativo ao root do projeto por padrão.
    RUNTIME_DIR: str = os.getenv("TRADING_BOT_RUNTIME_DIR", "runtime")

    # Nível de log (vazio = padrão do ambiente)
    LOG_LEVEL: str = os.getenv("TRADING_BOT_LOG_LEVEL", "")
    LOG_TO_STDOUT: bool = _env_bool("TRADING_BOT_LOG_TO_STDOUT", True)

    # Nomes de arquivos em runtime (vazio = padrão por ambiente)
    STATE_FILE_NAME: str = os.getenv("TRADING_BOT_STATE_FILE_NAME", "")
    LOCK_FILE_NAME: str = os.getenv("TRADING_BOT_LOCK_FILE_NAME", "")
    LOG_FILE_NAME: str = os.getenv("TRADING_BOT_LOG_FILE_NAME", "")

    # Caminhos resolvidos no __post_init__
    PROJECT_ROOT: str = ""
    STATE_FILE_PATH: str = ""
    LOCK_FILE_PATH: str = ""
    LOG_FILE_PATH: str = ""
    
    # ============================================
    # NOTIFICAÇÕES TELEGRAM
    # ============================================
    # Para configurar:
    # 1. Abra o Telegram e procure @BotFather
    # 2. Envie /newbot e siga as instruções
    # 3. Copie o TOKEN que ele te dar
    # 4. Envie uma mensagem para o seu bot
    # 5. Acesse: https://api.telegram.org/bot<TOKEN>/getUpdates
    # 6. Procure pelo "chat":{"id": XXXXXXXX} - esse é seu CHAT_ID
    
    TELEGRAM_ENABLED: bool = True  # Ative/desative notificações
    TELEGRAM_TOKEN: str = os.getenv("TELEGRAM_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
    
    # ID do usuário autorizado a enviar comandos (None = qualquer um)
    # Para descobrir seu ID, envie /start para @userinfobot no Telegram
    TELEGRAM_USER_ID: int | None = _env_optional_int("TELEGRAM_USER_ID")
    
    # Frequência das notificações de status (em iterações)
    # 60 = envia status a cada 60 iterações (10 minutos com intervalo de 10s)
    # Use /status no Telegram para ver status a qualquer momento
    TELEGRAM_STATUS_INTERVAL: int = 180  # A cada 30 minutos (use /status para ver a qualquer momento)
    
    # ============================================
    # GESTÃO DE CAPITAL
    # ============================================
    # SISTEMA INTELIGENTE: Usa o MAIOR entre X% do capital OU mínimo da moeda
    # 
    # Como funciona:
    # 1. Calcula X% do seu capital disponível
    # 2. Compara com o mínimo exigido pela Binance (+ 25% margem de segurança)
    # 3. Usa o MAIOR dos dois valores
    # 
    # Exemplo com $77 disponível, 5%, e ETH (mínimo $20):
    # - 5% de $77 = $3.85
    # - Mínimo ETH + 25% = $20 × 1.25 = $25
    # - Usa $25 (o maior) → Trade será de $25
    #
    # Exemplo com $500 disponível, 5%, e ETH (mínimo $20):
    # - 5% de $500 = $25
    # - Mínimo ETH + 25% = $25
    # - Usa $25 → Trade será de $25
    #
    # Exemplo com $1000 disponível, 5%, e ETH (mínimo $20):
    # - 5% de $1000 = $50
    # - Mínimo ETH + 25% = $25
    # - Usa $50 (o maior) → Trade escala com seu capital!
    #
    # Vantagens:
    # - Sempre respeita o mínimo da Binance
    # - Escala automaticamente quando você tem mais capital
    # - Funciona com qualquer número de pares
    # - Se não tiver capital suficiente, pula o trade (proteção)
    
    TOTAL_CAPITAL: float = 100.0  # Referência apenas (não usado no cálculo)
    
    # Percentual DESEJADO do capital por trade (sistema usa este OU o mínimo da moeda)
    # 5% = 0.05 | 10% = 0.10 | 15% = 0.15
    # Com pouco capital, o mínimo da moeda prevalece
    # Com muito capital, este percentual prevalece
    MAX_POSITION_PERCENT: float = 0.08  # 8% por trade (aumentado para maior ticket médio)
    
    # Alavancagem (1x a 20x) - MAIOR ALAVANCAGEM = MAIOR RISCO
    # Recomendo começar com 3x-5x para testes
    LEVERAGE: int = 20
    
    # ============================================
    # MOEDAS PARA OPERAR (Hedge em múltiplas moedas)
    # ============================================
    # Pares FIXOS (sempre ativos) + DINÂMICOS (selecionados por IA)
    TRADING_PAIRS: List[str] = None  # Será preenchido dinamicamente
    
    # Pares que SEMPRE serão operados (não são removidos pela seleção)
    FIXED_PAIRS: List[str] = None  # Será definido no __post_init__
    
    # Número total de pares a operar (fixos + dinâmicos)
    MAX_TRADING_PAIRS: int = 6
    
    # ============================================
    # SELEÇÃO INTELIGENTE DE PARES (IA)
    # ============================================
    # O bot analisa todos os pares disponíveis e seleciona os melhores
    # baseado em: Volatilidade, Volume, Tendência, Funding Rate, Spread
    # NOTA: Se USE_BINANCE_STRATEGY = True, esta opção é ignorada
    
    # Ativar seleção automática de pares
    AUTO_SELECT_PAIRS: bool = False  # Desativado pois USE_BINANCE_STRATEGY = True
    
    # Intervalo de atualização da lista de pares (em minutos)
    # 6 horas = 360 minutos (analisar 500+ pares demora, não precisa fazer frequentemente)
    PAIR_UPDATE_INTERVAL_MINUTES: int = 360  # 6 horas
    
    # Critérios de seleção (em ordem de prioridade - maior peso = mais importante)
    PAIR_SELECTION_WEIGHTS: dict = None  # Será definido no __post_init__
    
    # Filtros mínimos para um par ser considerado
    MIN_VOLUME_24H_USD: float = 50_000_000    # Volume mínimo $50M
    MAX_SPREAD_PERCENT: float = 0.10          # Spread máximo 0.10%
    MIN_VOLATILITY_PERCENT: float = 1.5       # Volatilidade mínima 1.5%
    MAX_MIN_NOTIONAL: float = 10.0            # Mínimo notional máximo $10 (exclui BTC $100, ETH $20)
    
    # Usar sempre o valor MÍNIMO aceitável de cada par
    USE_MIN_NOTIONAL_ONLY: bool = True
    
    # ============================================
    # ESTRATÉGIA DE TRADING
    # ============================================
    # ESTRATÉGIA DIRECIONAL BASEADA EM SINAIS:
    # - BUY / STRONG_BUY → Abre LONG
    # - SELL / STRONG_SELL → Abre SHORT
    # - NEUTRAL → Não faz nada
    #
    # Pode ter LONG e SHORT ao mesmo tempo se o sinal mudar.
    # Posições abertas seguem trailing stop, TP, SL normalmente.
    
    # Usar estratégia direcional (True) ou hedge (False)
    USE_SIGNAL_STRATEGY: bool = True
    
    # Percentual da posição para hedge (usado apenas se USE_SIGNAL_STRATEGY = False)
    HEDGE_RATIO: float = 0.5
    
    # Diferença de preço para abrir posição oposta (em %)
    # Se o preço mover 1%, abre a posição de hedge
    HEDGE_TRIGGER_PERCENT: float = 1.0
    
    # ============================================
    # DCA (Dollar Cost Averaging) - Média de Preço
    # ============================================
    # Ativar DCA automático
    DCA_ENABLED: bool = True
    
    # Número máximo de ordens DCA por posição
    DCA_MAX_ORDERS: int = 3
    
    # Queda percentual para cada ordem DCA
    # Se o preço cair 2%, adiciona mais à posição
    DCA_STEP_PERCENT: float = 2.0
    
    # Multiplicador do tamanho da ordem DCA (1.5 = 50% maior que a anterior)
    DCA_MULTIPLIER: float = 1.5
    
    # ============================================
    # GESTÃO DE RISCO
    # ============================================
    
    # Stop Loss Individual (por posição)
    # Se False, as posições não têm SL individual - só fecham por TP ou Trailing Stop
    USE_INDIVIDUAL_STOP_LOSS: bool = False  # Desativado - usa apenas Trailing Stop
    STOP_LOSS_PERCENT: float = 3.0  # Não usado quando desativado
    
    # Stop Loss Global (baseado no capital total)
    # Se o prejuízo total atingir esse % do capital inicial, fecha TUDO e para o bot
    # Exemplo: 90% com capital de $50 = para quando perder $45 (restar $5)
    GLOBAL_STOP_LOSS_PERCENT: float = 90.0
    
    # ============================================
    # ESTRATÉGIA BINANCE PADRÃO (por faixa de capital)
    # ============================================
    # Ativa a estratégia automática baseada no capital
    USE_BINANCE_STRATEGY: bool = True
    
    # Faixas de capital e configurações
    # Formato: (capital_min, capital_max, order_size, stop_loss, num_coins)
    BINANCE_STRATEGY_TIERS: list = None  # Será definido no __post_init__
    
    # Lista FIXA de moedas (em ordem de prioridade)
    # O bot vai usar as primeiras N moedas conforme a faixa de capital
    BINANCE_COIN_LIST: list = None  # Será definido no __post_init__
    
    # ============================================
    # METAS DIÁRIAS (Para de operar quando atingir)
    # ============================================
    # Ativa o controle de metas diárias
    USE_DAILY_TARGETS: bool = False  # Desativado - bot roda 24h
    
    # Meta de LUCRO diário em USD - Para de abrir novas posições quando atingir
    DAILY_PROFIT_TARGET: float = 20.0
    
    # Meta de PERDA diária em USD - Para de abrir novas posições quando atingir
    DAILY_LOSS_LIMIT: float = 10.0
    
    # Take Profit em percentual (continua ativo por posição)
    # Com trades de $6.25, 50% = $3.12 de lucro por trade
    TAKE_PROFIT_PERCENT: float = 8.0
    
    # ============================================
    # TRAILING STOP (Stop Móvel)
    # ============================================
    # O Trailing Stop protege seus lucros movendo o stop junto com o preço
    # 
    # ESTRATÉGIA CONSERVADORA:
    # - Ativa o trailing assim que cobrir as taxas (breakeven)
    # - Se o preço continuar subindo, o trailing acompanha
    # - Se reverter, sai com lucro mínimo garantido
    # - Se chegar no TP, fecha com lucro máximo
    #
    # Taxas Binance: 0.08% por ciclo (0.04% abertura + 0.04% fechamento)
    # 
    # Com a configuração abaixo:
    # - Ativa trailing quando lucro >= 0.2%
    # - Stop fica 0.12% abaixo do pico
    # - Se trailing for acionado, lucro mínimo = 0.08% (breakeven) ou mais
    # - Lucro líquido garantido = ~0% a 3.92% (dependendo de onde sair)
    #
    # Exemplo com LONG:
    # - Entrada: $100, Ativa em 0.25%, Distância 0.15%
    # - Preço sobe para $100.25 (0.25% lucro) → Trailing ativado, stop em $100.10 (breakeven)
    # - Preço sobe para $104 (4% lucro) → Stop em $103.85, mas fecha por TP
    # - Se preço reverter em $101.50 → Stop em $101.35, sai com ~1.25% lucro líquido
    # NOTA: Breakeven = 0.10% (taxa taker 0.05% × 2 ordens)
    
    USE_TRAILING_STOP: bool = True
    TRAILING_ACTIVATION_PERCENT: float = 0.20   # Ativa quando lucro >= 0.20%
    TRAILING_DISTANCE_PERCENT: float = 0.12     # Stop fica 0.12% abaixo do pico
    TRAILING_MIN_PROFIT_USD: float = 0.20       # Lucro mínimo em USD para fechar ($0.20 = 20 centavos)
    
    # ============================================
    # FUNDING RATE (Taxa de financiamento)
    # ============================================
    # O funding rate é cobrado/pago a cada 8h (00:00, 08:00, 16:00 UTC)
    # - Rate POSITIVO: LONGs pagam, SHORTs recebem
    # - Rate NEGATIVO: SHORTs pagam, LONGs recebem
    #
    # Se o funding rate estiver CONTRA a posição (vai pagar funding),
    # aumentamos o lucro mínimo do trailing para compensar.
    #
    # Exemplo: 
    # - Funding +0.05% e posição LONG → vai pagar → aumenta mínimo
    # - Funding +0.05% e posição SHORT → vai receber → mantém mínimo normal
    
    CHECK_FUNDING_RATE: bool = True
    FUNDING_RATE_THRESHOLD: float = 0.02        # Acima de 0.02% considera "alto"
    TRAILING_MIN_PROFIT_HIGH_FUNDING: float = 0.35  # Mínimo quando funding está contra ($0.35)
    
    # Perda máxima diária permitida (em % do capital)
    # Se perder 10% do capital no dia, para de operar
    MAX_DAILY_LOSS_PERCENT: float = 10.0
    
    # Número máximo de posições abertas simultaneamente
    # Com estratégia direcional, cada par pode ter LONG ou SHORT (ou ambos se sinal mudar)
    # 12 posições = permite até 12 trades direcionais ativos
    MAX_OPEN_POSITIONS: int = 12
    
    # ============================================
    # INTERVALOS E TIMING
    # ============================================
    # Intervalo entre ciclos completos de análise de ENTRADA (em segundos)
    # Ex: 10s = inicia um novo ciclo de análise a cada 10 segundos
    CHECK_INTERVAL: int = 10

    # Intervalo do loop de monitoramento de posições (trailing/TP/SL) em segundos
    # Menor = reação mais rápida, porém mais chamadas à API
    POSITION_MONITOR_INTERVAL: int = 3

    # Pausa entre análise de um símbolo e outro no mesmo ciclo (em segundos)
    # Ajuda a reduzir burst de chamadas na API
    ANALYSIS_SYMBOL_DELAY: float = 1.0
    
    # Timeframe para análise (1m, 5m, 15m, 1h, 4h)
    TIMEFRAME: str = "5m"
    
    # Número de candles para análise
    CANDLES_LOOKBACK: int = 50

    # ============================================
    # RESILIÊNCIA DE API (retry/backoff)
    # ============================================
    # Número máximo de tentativas em chamadas elegíveis para retry
    API_RETRY_ATTEMPTS: int = 4

    # Backoff exponencial inicial (segundos)
    API_RETRY_BASE_DELAY: float = 0.5

    # Teto do backoff exponencial (segundos)
    API_RETRY_MAX_DELAY: float = 5.0

    # Jitter aleatório adicionado ao backoff (segundos)
    API_RETRY_JITTER: float = 0.25

    # Intervalo (em segundos) para log agregado de retries/falhas da API
    API_RETRY_STATS_INTERVAL_SECONDS: int = 60

    # Intervalo (em segundos) para envio do resumo de saúde da API no Telegram
    API_HEALTH_TELEGRAM_INTERVAL_SECONDS: int = 1800  # 30 min

    # Se True, só envia no Telegram quando houver retries/falhas na janela
    API_HEALTH_TELEGRAM_ONLY_ON_ISSUES: bool = True

    # Habilita alerta crítico imediato (fora da janela periódica)
    API_HEALTH_CRITICAL_ALERTS_ENABLED: bool = True

    # Cooldown entre alertas críticos imediatos
    API_HEALTH_CRITICAL_COOLDOWN_SECONDS: int = 300

    # Limiares de criticidade para disparo imediato
    API_HEALTH_CRITICAL_MIN_API_FAILURES: int = 1
    API_HEALTH_CRITICAL_MIN_ORDER_FAILURES: int = 1
    API_HEALTH_CRITICAL_MIN_ORDER_REJECTIONS: int = 2
    API_HEALTH_CRITICAL_MIN_LOOP_ERRORS: int = 1

    # Relatório diário consolidado para decisão de risco/SL (Telegram)
    DAILY_PERFORMANCE_REPORT_ENABLED: bool = _env_bool("TRADING_BOT_DAILY_REPORT_ENABLED", True)
    DAILY_PERFORMANCE_REPORT_HOUR_BRT: int = _env_int("TRADING_BOT_DAILY_REPORT_HOUR_BRT", 23)
    DAILY_PERFORMANCE_REPORT_MINUTE_BRT: int = _env_int("TRADING_BOT_DAILY_REPORT_MINUTE_BRT", 55)
    DAILY_PERFORMANCE_REPORT_LOOKBACK_HOURS: int = _env_int("TRADING_BOT_DAILY_REPORT_LOOKBACK_HOURS", 24)

    # Dashboard web de monitoramento (somente leitura)
    DASHBOARD_HOST: str = os.getenv("TRADING_BOT_DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1"
    DASHBOARD_PORT: int = _env_int("TRADING_BOT_DASHBOARD_PORT", 8080)
    DASHBOARD_REFRESH_SECONDS: int = _env_int("TRADING_BOT_DASHBOARD_REFRESH_SECONDS", 5)
    DASHBOARD_AUTH_TOKEN: str = os.getenv("TRADING_BOT_DASHBOARD_AUTH_TOKEN", "").strip()

    # ============================================
    # AJUSTE AUTOMÁTICO DE CAPITAL (DEPÓSITO/SAQUE)
    # ============================================
    # Quando detectar TRANSFER na conta Futures, ajusta o capital base
    # usado no cálculo de risco global.
    CAPITAL_TRANSFER_DETECTION_ENABLED: bool = True

    # Ignora transferências muito pequenas (ruído)
    CAPITAL_TRANSFER_MIN_ABS_USDT: float = 1.0

    # Quantidade máxima de IDs de transferências mantidos para deduplicação
    CAPITAL_TRANSFER_TRACKED_IDS_LIMIT: int = 500

    # Fator para marcar "overrun" de loop (ex: 1.5 = 150% do alvo do loop)
    LOOP_OVERRUN_FACTOR: float = 1.5
    
    def __post_init__(self):
        """Inicializa valores padrão que dependem de listas"""
        # Normaliza ambiente
        env = str(self.APP_ENV or "prod").strip().lower()
        env_aliases = {
            "production": "prod",
            "prd": "prod",
            "dev": "dev",
            "development": "dev",
            "stage": "staging",
        }
        self.APP_ENV = env_aliases.get(env, env)

        # Define nível de log padrão por ambiente (se não vier do env var)
        if not self.LOG_LEVEL:
            default_levels = {
                "dev": "DEBUG",
                "staging": "INFO",
                "prod": "INFO",
                "test": "WARNING",
            }
            self.LOG_LEVEL = default_levels.get(self.APP_ENV, "INFO")
        self.LOG_LEVEL = str(self.LOG_LEVEL).upper()

        # Resolve diretório runtime e nomes de arquivos por ambiente
        project_root = Path(__file__).resolve().parents[2]
        runtime_dir = Path(self.RUNTIME_DIR).expanduser()
        if not runtime_dir.is_absolute():
            runtime_dir = project_root / runtime_dir
        runtime_dir.mkdir(parents=True, exist_ok=True)

        env_suffix = self.APP_ENV or "prod"
        if not self.STATE_FILE_NAME:
            self.STATE_FILE_NAME = f"bot_state.{env_suffix}.json"
        if not self.LOCK_FILE_NAME:
            self.LOCK_FILE_NAME = f"trading_bot.{env_suffix}.lock"
        if not self.LOG_FILE_NAME:
            self.LOG_FILE_NAME = f"trading_bot.{env_suffix}.log"

        self.PROJECT_ROOT = str(project_root)
        self.RUNTIME_DIR = str(runtime_dir)
        self.STATE_FILE_PATH = str(runtime_dir / self.STATE_FILE_NAME)
        self.LOCK_FILE_PATH = str(runtime_dir / self.LOCK_FILE_NAME)
        self.LOG_FILE_PATH = str(runtime_dir / self.LOG_FILE_NAME)

        # Pares FIXOS - VAZIO = todos serão dinâmicos
        if self.FIXED_PAIRS is None:
            self.FIXED_PAIRS = []  # Nenhum par fixo, todos dinâmicos
        
        # ============================================
        # ESTRATÉGIA BINANCE PADRÃO
        # ============================================
        # Faixas: (capital_min, capital_max, order_size, stop_loss_value, num_coins)
        if self.BINANCE_STRATEGY_TIERS is None:
            self.BINANCE_STRATEGY_TIERS = [
                # (capital_min, capital_max, order_size, stop_loss_value, num_coins)
                (90, 150, 3, 180, 3),     # Alterado: 1 → 3 moedas
                (150, 300, 3, 180, 3),    # Mantido: 3 moedas (era 2)
                (300, 500, 3, 180, 3),
                (500, 1000, 3, 180, 6),
                (1000, 2000, 3, 180, 9),
                (2000, 3000, 3, 180, 9),    # 2000: order 3, SL 180, 9 moedas
                (3000, 4000, 10, 600, 9),   # 3000: order 10, SL 600, 9 moedas
                (4000, 5000, 12, 720, 10),  # 4000: order 12, SL 720, 10 moedas
                (5000, 6000, 15, 900, 11),  # 5000: order 15, SL 900, 11 moedas
                (6000, 7000, 17, 1020, 11), # 6000: order 17, SL 1020, 11 moedas
                (7000, 8000, 20, 1200, 11), # 7000: order 20, SL 1200, 11 moedas
                (8000, 9000, 21, 1260, 12), # 8000: order 21, SL 1260, 12 moedas
                (9000, 10000, 24, 1440, 12),# 9000: order 24, SL 1440, 12 moedas
                (10000, 999999, 28, 1680, 12), # 10000+: order 28, SL 1680, 12 moedas
            ]
        
        # Lista de moedas PERMITIDAS pela estratégia Binance
        # O bot vai ordenar essas moedas pelo score (spread, volume, volatility, trend, funding)
        # e selecionar as primeiras N conforme a faixa de capital
        if self.BINANCE_COIN_LIST is None:
            self.BINANCE_COIN_LIST = [
                # Moedas da planilha Binance Padrão (serão ordenadas por score)
                "ADAUSDT",
                "TRXUSDT",
                "ARBUSDT",
                "XRPUSDT",
                "APTUSDT",
                "FILUSDT",
                "SUSHIUSDT",
                "ATOMUSDT",
                "NOTUSDT",
                "1000PEPEUSDT",
                "CFXUSDT",
                "1INCHUSDT",
                "MASKUSDT",
                "SNXUSDT",
                "THETAUSDT",
                "OPUSDT",
                "COMPUSDT",
                "SUIUSDT",
                "CHZUSDT",
                "1000SHIBUSDT",
                "DOGEUSDT",
                "NEARUSDT",
                "SANDUSDT",
                "APEUSDT",
                "SOLUSDT",
                "CRVUSDT",
                "DOTUSDT",
                "UNIUSDT",
                "BNBUSDT",
                "LTCUSDT",
                "BCHUSDT",
                "LINKUSDT",
                "ETCUSDT",
                "ETHUSDT",
                "AAVEUSDT",
                "AVAXUSDT",
            ]
        
        # Pares de trading - usa BINANCE_COIN_LIST se estratégia Binance ativa
        # Será sobrescrito no setup_exchange() com a quantidade correta baseada no capital
        if self.TRADING_PAIRS is None:
            if self.USE_BINANCE_STRATEGY:
                # Usa as primeiras 3 moedas da lista Binance (será ajustado no setup)
                self.TRADING_PAIRS = self.BINANCE_COIN_LIST[:3]
            else:
                # Lista padrão para seleção automática
                self.TRADING_PAIRS = [
                    "BNBUSDT",
                    "SOLUSDT",
                    "XRPUSDT",
                    "DOGEUSDT",
                    "ADAUSDT",
                    "AVAXUSDT",
                ]
        
        # Pesos para seleção de pares (ordem de prioridade)
        if self.PAIR_SELECTION_WEIGHTS is None:
            self.PAIR_SELECTION_WEIGHTS = {
                'spread': 35,        # 35% - Spread baixo (custo de execução)
                'volume': 30,        # 30% - Volume 24h (liquidez)
                'volatility': 20,    # 20% - Volatilidade (movimento suficiente)
                'trend': 10,         # 10% - Força da tendência (filtro)
                'funding': 5,        # 5%  - Funding (quase irrelevante no scalp)
            }
    
    def get_binance_strategy_for_capital(self, capital: float) -> dict:
        """
        Retorna a configuração da estratégia Binance para o capital atual.
        
        Args:
            capital: Saldo atual em USDT
            
        Returns:
            dict com: order_size, stop_loss, num_coins, coins
        """
        # Encontra a faixa correta
        for tier in self.BINANCE_STRATEGY_TIERS:
            min_cap, max_cap, order_size, stop_loss, num_coins = tier
            if min_cap <= capital < max_cap:
                # Seleciona as primeiras N moedas da lista
                coins = self.BINANCE_COIN_LIST[:num_coins]
                return {
                    'capital_range': f"${min_cap}-${max_cap}",
                    'order_size': order_size,
                    'stop_loss': stop_loss,
                    'num_coins': num_coins,
                    'coins': coins
                }
        
        # Se não encontrar faixa (capital muito baixo), usa a primeira
        tier = self.BINANCE_STRATEGY_TIERS[0]
        min_cap, max_cap, order_size, stop_loss, num_coins = tier
        coins = self.BINANCE_COIN_LIST[:num_coins]
        return {
            'capital_range': f"${min_cap}-${max_cap}",
            'order_size': order_size,
            'stop_loss': stop_loss,
            'num_coins': num_coins,
            'coins': coins
        }
    
    def validate(self) -> bool:
        """
        Valida se as configurações fazem sentido.
        Retorna True se tudo estiver ok.
        """
        errors = []
        
        if self.LEVERAGE > 20:
            errors.append("⚠️  ALERTA: Alavancagem acima de 20x é muito arriscada!")
        
        if self.MAX_POSITION_PERCENT > 0.10:
            errors.append("⚠️  ALERTA: Posição maior que 10% do capital é arriscada!")
        
        if self.STOP_LOSS_PERCENT > 50:
            errors.append("⚠️  ALERTA: Stop Loss muito alto, considere reduzir!")
        
        if len(self.TRADING_PAIRS) > self.MAX_TRADING_PAIRS:
            errors.append(f"⚠️  ALERTA: Mais de {self.MAX_TRADING_PAIRS} pares configurados!")

        valid_log_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if self.LOG_LEVEL not in valid_log_levels:
            errors.append(
                f"⚠️  ALERTA: LOG_LEVEL inválido ({self.LOG_LEVEL}). Use: {sorted(valid_log_levels)}"
            )

        if not self.RUNTIME_DIR:
            errors.append("⚠️  ALERTA: RUNTIME_DIR não pode ser vazio!")

        if not self.API_KEY or not self.API_SECRET:
            errors.append(
                "⚠️  ALERTA: BINANCE_API_KEY/BINANCE_API_SECRET não configuradas nas variáveis de ambiente."
            )

        if self.TELEGRAM_ENABLED and (not self.TELEGRAM_TOKEN or not self.TELEGRAM_CHAT_ID):
            errors.append(
                "⚠️  ALERTA: Telegram ativo, mas TELEGRAM_TOKEN/TELEGRAM_CHAT_ID não configurados."
            )

        if self.CHECK_INTERVAL < 1:
            errors.append("⚠️  ALERTA: CHECK_INTERVAL deve ser >= 1 segundo!")

        if self.POSITION_MONITOR_INTERVAL < 1:
            errors.append("⚠️  ALERTA: POSITION_MONITOR_INTERVAL deve ser >= 1 segundo!")

        if self.ANALYSIS_SYMBOL_DELAY <= 0:
            errors.append("⚠️  ALERTA: ANALYSIS_SYMBOL_DELAY deve ser > 0 segundo!")

        if self.API_RETRY_ATTEMPTS < 1:
            errors.append("⚠️  ALERTA: API_RETRY_ATTEMPTS deve ser >= 1!")

        if self.API_RETRY_BASE_DELAY <= 0:
            errors.append("⚠️  ALERTA: API_RETRY_BASE_DELAY deve ser > 0!")

        if self.API_RETRY_MAX_DELAY < self.API_RETRY_BASE_DELAY:
            errors.append("⚠️  ALERTA: API_RETRY_MAX_DELAY deve ser >= API_RETRY_BASE_DELAY!")

        if self.API_RETRY_JITTER < 0:
            errors.append("⚠️  ALERTA: API_RETRY_JITTER deve ser >= 0!")

        if self.API_RETRY_STATS_INTERVAL_SECONDS <= 0:
            errors.append("⚠️  ALERTA: API_RETRY_STATS_INTERVAL_SECONDS deve ser > 0!")

        if self.API_HEALTH_TELEGRAM_INTERVAL_SECONDS <= 0:
            errors.append("⚠️  ALERTA: API_HEALTH_TELEGRAM_INTERVAL_SECONDS deve ser > 0!")

        if self.API_HEALTH_CRITICAL_COOLDOWN_SECONDS <= 0:
            errors.append("⚠️  ALERTA: API_HEALTH_CRITICAL_COOLDOWN_SECONDS deve ser > 0!")

        if self.API_HEALTH_CRITICAL_MIN_API_FAILURES < 1:
            errors.append("⚠️  ALERTA: API_HEALTH_CRITICAL_MIN_API_FAILURES deve ser >= 1!")

        if self.API_HEALTH_CRITICAL_MIN_ORDER_FAILURES < 1:
            errors.append("⚠️  ALERTA: API_HEALTH_CRITICAL_MIN_ORDER_FAILURES deve ser >= 1!")

        if self.API_HEALTH_CRITICAL_MIN_ORDER_REJECTIONS < 1:
            errors.append("⚠️  ALERTA: API_HEALTH_CRITICAL_MIN_ORDER_REJECTIONS deve ser >= 1!")

        if self.API_HEALTH_CRITICAL_MIN_LOOP_ERRORS < 1:
            errors.append("⚠️  ALERTA: API_HEALTH_CRITICAL_MIN_LOOP_ERRORS deve ser >= 1!")

        if self.DAILY_PERFORMANCE_REPORT_HOUR_BRT < 0 or self.DAILY_PERFORMANCE_REPORT_HOUR_BRT > 23:
            errors.append("⚠️  ALERTA: DAILY_PERFORMANCE_REPORT_HOUR_BRT deve estar entre 0 e 23!")

        if self.DAILY_PERFORMANCE_REPORT_MINUTE_BRT < 0 or self.DAILY_PERFORMANCE_REPORT_MINUTE_BRT > 59:
            errors.append("⚠️  ALERTA: DAILY_PERFORMANCE_REPORT_MINUTE_BRT deve estar entre 0 e 59!")

        if self.DAILY_PERFORMANCE_REPORT_LOOKBACK_HOURS < 1 or self.DAILY_PERFORMANCE_REPORT_LOOKBACK_HOURS > 168:
            errors.append("⚠️  ALERTA: DAILY_PERFORMANCE_REPORT_LOOKBACK_HOURS deve estar entre 1 e 168!")

        if self.DASHBOARD_PORT < 1 or self.DASHBOARD_PORT > 65535:
            errors.append("⚠️  ALERTA: DASHBOARD_PORT deve estar entre 1 e 65535!")

        if self.DASHBOARD_REFRESH_SECONDS < 2 or self.DASHBOARD_REFRESH_SECONDS > 300:
            errors.append("⚠️  ALERTA: DASHBOARD_REFRESH_SECONDS deve estar entre 2 e 300!")

        if self.CAPITAL_TRANSFER_MIN_ABS_USDT < 0:
            errors.append("⚠️  ALERTA: CAPITAL_TRANSFER_MIN_ABS_USDT deve ser >= 0!")

        if self.CAPITAL_TRANSFER_TRACKED_IDS_LIMIT < 100:
            errors.append("⚠️  ALERTA: CAPITAL_TRANSFER_TRACKED_IDS_LIMIT deve ser >= 100!")

        if self.LOOP_OVERRUN_FACTOR < 1.0:
            errors.append("⚠️  ALERTA: LOOP_OVERRUN_FACTOR deve ser >= 1.0!")
        
        for error in errors:
            logger.warning(error)
        
        return len(errors) == 0


# Instância global de configuração
config = TradingConfig()

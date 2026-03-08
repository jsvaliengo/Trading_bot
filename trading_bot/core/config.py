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


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw.strip())
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

    # Pares desabilitados manualmente (não entram na seleção de moedas)
    DISABLED_PAIRS: List[str] = None  # Será definido no __post_init__
    
    # Número total de pares a operar (fixos + dinâmicos)
    MAX_TRADING_PAIRS: int = 20
    
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

    # Perfis de estratégia (v1 multi-estratégia em uma instância).
    # Cada item pode conter:
    # - name: identificador da estratégia
    # - enabled: ativa/desativa o perfil
    # - strategy_type: "trend_signal" (padrão) ou "range_scalping"
    # - entry_mode: "strong_only" (padrão) ou "standard"
    # - pairs: lista de pares atribuídos ao perfil
    # - risk_profile (opcional para trend_signal): limites de SL/TP e alvo de risk/reward
    STRATEGY_PROFILES: list = None  # Será definido no __post_init__

    # ============================================
    # TREND STRONG (pullback multi-timeframe)
    # ============================================
    # Execução rápida (entrada) em 1m ou 3m + confirmação de tendência em 5m.
    TREND_STRONG_EXECUTION_TIMEFRAME: str = "3m"
    TREND_STRONG_CONFIRM_TIMEFRAME: str = "5m"
    TREND_STRONG_CANDLES_LOOKBACK: int = 260
    TREND_STRONG_PULLBACK_TOLERANCE_PERCENT: float = 1.00
    TREND_STRONG_LONG_RSI_MIN: float = 25.0
    TREND_STRONG_LONG_RSI_MAX: float = 75.0
    TREND_STRONG_SHORT_RSI_MIN: float = 25.0
    TREND_STRONG_SHORT_RSI_MAX: float = 75.0
    TREND_STRONG_MIN_VOLUME_RATIO: float = 0.50

    # ============================================
    # RANGE SCALPING (segunda estratégia)
    # ============================================
    RANGE_SCALP_MIN_RANGE_PERCENT: float = 0.8
    RANGE_SCALP_MIN_RANGE_MINUTES: int = 45
    RANGE_SCALP_MIN_TOUCHES_PER_SIDE: int = 2
    RANGE_SCALP_TOUCH_TOLERANCE_RATIO: float = 0.12
    RANGE_SCALP_REJECTION_MIN_RATIO: float = 0.08
    RANGE_SCALP_EDGE_ZONE_RATIO: float = 0.30
    RANGE_SCALP_MIN_EDGE_PARTICIPATION: float = 0.30
    RANGE_SCALP_MAX_VOLUME_RATIO: float = 0.95
    RANGE_SCALP_INVALIDATE_VOLUME_STREAK: int = 3
    RANGE_SCALP_INVALIDATE_MOMENTUM_CANDLES: int = 3
    RANGE_SCALP_STOP_BUFFER_RATIO: float = 0.25
    RANGE_SCALP_STOP_BUFFER_MIN_PERCENT: float = 0.15
    RANGE_SCALP_TAKE_PROFIT_RATIO: float = 0.70
    RANGE_SCALP_MIN_POSITION_MULTIPLIER: float = 0.70
    RANGE_SCALP_MAX_POSITION_MULTIPLIER: float = 1.30
    RANGE_SCALP_MIN_RISK_REWARD: float = 1.20
    RANGE_SCALP_EARLY_EXIT_ENABLED: bool = False
    RANGE_SCALP_EARLY_EXIT_TIMEFRAME: str = "3m"
    
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

    # Drawdown máximo a partir do pico de equity (Improvement 1)
    # Se o saldo cair X% desde o pico histórico, bloqueia novas entradas
    MAX_DRAWDOWN_FROM_PEAK_PERCENT: float = float(os.getenv("TRADING_BOT_MAX_DRAWDOWN_PERCENT", "30.0"))
    
    # ============================================
    # ESTRATÉGIA BINANCE PADRÃO (por faixa de capital)
    # ============================================
    # Ativa a estratégia automática baseada no capital
    USE_BINANCE_STRATEGY: bool = True

    # Double First: dobra a primeira entrada por direção.
    # Escopo:
    # - global: primeira LONG e primeira SHORT do bot inteiro
    # - symbol: primeira LONG e primeira SHORT por símbolo
    DOUBLE_FIRST_LONG_ENABLED: bool = _env_bool("TRADING_BOT_DOUBLE_FIRST_LONG_ENABLED", False)
    DOUBLE_FIRST_SHORT_ENABLED: bool = _env_bool("TRADING_BOT_DOUBLE_FIRST_SHORT_ENABLED", False)
    DOUBLE_FIRST_MULTIPLIER: float = _env_float("TRADING_BOT_DOUBLE_FIRST_MULTIPLIER", 2.0)
    DOUBLE_FIRST_MAX_MARGIN_USDT: float = _env_float("TRADING_BOT_DOUBLE_FIRST_MAX_MARGIN_USDT", 0.0)
    DOUBLE_FIRST_SCOPE: str = os.getenv("TRADING_BOT_DOUBLE_FIRST_SCOPE", "global").strip().lower() or "global"
    
    # Faixas de capital e configurações
    # Formato: (capital_min, capital_max, order_size, stop_loss, num_coins)
    BINANCE_STRATEGY_TIERS: list = None  # Será definido no __post_init__
    
    # Universo de moedas Binance (preenchido dinamicamente em runtime)
    # O bot usa as primeiras N moedas ordenadas por score conforme a faixa de capital
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

    # Exposição total máxima em % do saldo (Improvement 4)
    # Ex: 80% = soma de todos os notionais abertos não pode exceder 80% do saldo
    MAX_TOTAL_NOTIONAL_PERCENT: float = float(os.getenv("TRADING_BOT_MAX_TOTAL_NOTIONAL_PERCENT", "80.0"))

    # Concentração máxima por posição individual em % do saldo (Improvement 10)
    # Ex: 15% = uma única posição não pode representar mais de 15% do saldo
    MAX_POSITION_CONCENTRATION_PERCENT: float = float(os.getenv("TRADING_BOT_MAX_CONCENTRATION_PERCENT", "15.0"))
    
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

    # Idade máxima de um sinal antes de ser descartado (Improvement 7)
    # Se o sinal foi gerado há mais de X segundos, pula a entrada
    MAX_SIGNAL_AGE_SECONDS: float = float(os.getenv("TRADING_BOT_MAX_SIGNAL_AGE_SECONDS", "120.0"))
    
    # Timeframe para análise (1m, 5m, 15m, 1h, 4h)
    TIMEFRAME: str = "5m"
    
    # Número de candles para análise
    CANDLES_LOOKBACK: int = 50

    # ============================================
    # FILTRO DE SENTIMENTO / VIÉS DE MERCADO
    # ============================================
    # Quando ativo, só permite entrada na direção do viés detectado:
    # - BULLISH => somente LONG
    # - BEARISH => somente SHORT
    # - NEUTRAL => mantém operação normal (LONG/SHORT conforme sinal)
    USE_MARKET_SENTIMENT_FILTER: bool = _env_bool("TRADING_BOT_SENTIMENT_FILTER_ENABLED", False)
    SENTIMENT_TIMEFRAME: str = os.getenv("TRADING_BOT_SENTIMENT_TIMEFRAME", "1h").strip() or "1h"
    SENTIMENT_CANDLES_LOOKBACK: int = _env_int("TRADING_BOT_SENTIMENT_LOOKBACK_CANDLES", 120)
    SENTIMENT_MIN_SCORE: int = _env_int("TRADING_BOT_SENTIMENT_MIN_SCORE", 2)
    SENTIMENT_MIN_MOMENTUM_PERCENT: float = _env_float("TRADING_BOT_SENTIMENT_MIN_MOMENTUM_PERCENT", 0.10)
    SENTIMENT_CACHE_SECONDS: int = _env_int("TRADING_BOT_SENTIMENT_CACHE_SECONDS", 300)

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

        # Pares desabilitados por padrão (podem ser reabilitados via Telegram)
        if self.DISABLED_PAIRS is None:
            self.DISABLED_PAIRS = ["ADAUSDT", "SIGNUSDT"]
        self.DISABLED_PAIRS = self.normalize_pair_list(self.DISABLED_PAIRS)
        
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
        
        # Universo de moedas PERMITIDAS pela estratégia Binance.
        # Agora é atualizado dinamicamente a partir dos pares tradáveis na Binance Futures.
        # Mantemos vazio aqui para ser populado após conexão com a exchange.
        if self.BINANCE_COIN_LIST is None:
            self.BINANCE_COIN_LIST = []
        self.BINANCE_COIN_LIST = self.normalize_pair_list(self.BINANCE_COIN_LIST)

        # Pares de trading - usa BINANCE_COIN_LIST se estratégia Binance ativa
        # Será sobrescrito no setup_exchange() com a quantidade correta baseada no capital
        if self.TRADING_PAIRS is None:
            if self.USE_BINANCE_STRATEGY:
                # Antes da conexão, pode estar vazio. setup_exchange() buscará os pares na Binance.
                self.TRADING_PAIRS = self.get_enabled_binance_coin_list()[:3]
            else:
                # Lista padrão para seleção automática
                self.TRADING_PAIRS = [
                    "BNBUSDT",
                    "SOLUSDT",
                    "XRPUSDT",
                    "DOGEUSDT",
                    "ADAUSDT",
                ]
        self.FIXED_PAIRS = self.filter_disabled_pairs(self.FIXED_PAIRS)
        self.TRADING_PAIRS = self.filter_disabled_pairs(self.TRADING_PAIRS)

        # Perfis de estratégia (mantém compatibilidade com TRADING_PAIRS legado)
        if self.STRATEGY_PROFILES is None:
            self.STRATEGY_PROFILES = [
                {
                    "name": "trend_strong",
                    "enabled": True,
                    "strategy_type": "trend_signal",
                    "entry_mode": "strong_only",
                    # pairs vazio = seleção automática pela Binance (top N por score)
                    "pairs": [],
                    "max_pairs": 10,
                    # Perfil equilibrado: SL 0.4%-0.6%, TP 0.8%-1.2%, RR alvo ~1:2
                    "risk_profile": {
                        "stop_loss_min_percent": 0.4,
                        "stop_loss_max_percent": 0.6,
                        "take_profit_min_percent": 0.8,
                        "take_profit_max_percent": 1.2,
                        "risk_reward_target": 2.0,
                    },
                },
                {
                    "name": "range_scalp_v1",
                    "enabled": False,
                    "strategy_type": "range_scalping",
                    "entry_mode": "strong_only",
                    "pairs": ["DOGEUSDT", "XRPUSDT", "POLUSDT", "LTCUSDT", "LINKUSDT", "DOTUSDT", "BNBUSDT"],
                },
            ]
        self.STRATEGY_PROFILES = self._normalize_strategy_profiles(self.STRATEGY_PROFILES)
        strategy_pairs = []
        for profile in self.get_enabled_strategy_profiles():
            strategy_pairs.extend(profile.get("pairs", []))
        if strategy_pairs:
            self.TRADING_PAIRS = self.filter_disabled_pairs(strategy_pairs)
        
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
                coins = self.get_enabled_binance_coin_list()[:num_coins]
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
        coins = self.get_enabled_binance_coin_list()[:num_coins]
        return {
            'capital_range': f"${min_cap}-${max_cap}",
            'order_size': order_size,
            'stop_loss': stop_loss,
            'num_coins': num_coins,
            'coins': coins
        }

    @staticmethod
    def normalize_pair_symbol(symbol: str) -> str:
        """Normaliza símbolo para o padrão XXXUSDT."""
        token = str(symbol or "").strip().upper().strip(",;")
        token = token.replace("/", "").replace("-", "").replace("_", "")
        if not token:
            return ""
        if token.endswith("USDT"):
            return token
        return f"{token}USDT"

    def normalize_pair_list(self, pairs: List[str]) -> List[str]:
        """Normaliza e deduplica lista de símbolos preservando ordem."""
        normalized = []
        seen = set()
        for raw_symbol in pairs or []:
            symbol = self.normalize_pair_symbol(raw_symbol)
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            normalized.append(symbol)
        return normalized

    def get_disabled_pairs_set(self) -> set:
        """Retorna pares desabilitados em um set para lookup rápido."""
        return set(self.normalize_pair_list(getattr(self, "DISABLED_PAIRS", []) or []))

    def is_pair_disabled(self, symbol: str) -> bool:
        """Retorna True se o par estiver desabilitado."""
        normalized = self.normalize_pair_symbol(symbol)
        return bool(normalized) and normalized in self.get_disabled_pairs_set()

    def filter_disabled_pairs(self, pairs: List[str]) -> List[str]:
        """Remove pares desabilitados de uma lista, preservando ordem."""
        disabled = self.get_disabled_pairs_set()
        filtered = []
        seen = set()
        for raw_symbol in pairs or []:
            symbol = self.normalize_pair_symbol(raw_symbol)
            if not symbol or symbol in disabled or symbol in seen:
                continue
            seen.add(symbol)
            filtered.append(symbol)
        return filtered

    def get_enabled_binance_coin_list(self) -> List[str]:
        """Retorna lista Binance com pares habilitados."""
        return self.filter_disabled_pairs(self.BINANCE_COIN_LIST or [])

    @staticmethod
    def _normalize_entry_mode(mode: str) -> str:
        """Normaliza modo de entrada por estratégia."""
        token = str(mode or "").strip().lower()
        if token in {"standard", "normal", "full"}:
            return "standard"
        return "strong_only"

    @staticmethod
    def _normalize_strategy_type(strategy_type: str) -> str:
        """Normaliza tipo de estratégia por perfil."""
        token = str(strategy_type or "").strip().lower()
        if token in {"range_scalping", "range", "scalping", "range_scalp"}:
            return "range_scalping"
        return "trend_signal"

    @staticmethod
    def _normalize_trend_risk_profile(risk_profile: dict | None) -> dict:
        """Normaliza limites de risco/retorno para perfis trend_signal."""
        source = risk_profile if isinstance(risk_profile, dict) else {}

        def _to_float(*keys: str, default: float) -> float:
            for key in keys:
                if key in source:
                    try:
                        return float(source.get(key))
                    except (TypeError, ValueError):
                        break
            return float(default)

        stop_loss_min = _to_float("stop_loss_min_percent", "stop_loss_percent_min", default=0.4)
        stop_loss_max = _to_float("stop_loss_max_percent", "stop_loss_percent_max", default=0.6)
        take_profit_min = _to_float("take_profit_min_percent", "take_profit_percent_min", default=0.8)
        take_profit_max = _to_float("take_profit_max_percent", "take_profit_percent_max", default=1.2)
        rr_target = _to_float("risk_reward_target", "risk_reward_ratio", default=2.0)

        stop_loss_min = max(0.05, stop_loss_min)
        stop_loss_max = max(stop_loss_min, stop_loss_max)
        take_profit_min = max(0.05, take_profit_min)
        take_profit_max = max(take_profit_min, take_profit_max)
        rr_target = max(1.0, rr_target)

        return {
            "stop_loss_min_percent": round(stop_loss_min, 4),
            "stop_loss_max_percent": round(stop_loss_max, 4),
            "take_profit_min_percent": round(take_profit_min, 4),
            "take_profit_max_percent": round(take_profit_max, 4),
            "risk_reward_target": round(rr_target, 4),
        }

    def _normalize_strategy_profiles(self, profiles: list | None) -> List[dict]:
        """Normaliza perfis de estratégia preservando ordem e sem sobreposição de pares."""
        source = profiles if isinstance(profiles, list) and profiles else [
            {
                "name": "primary",
                "enabled": True,
                "strategy_type": "trend_signal",
                "entry_mode": "strong_only",
                "pairs": list(self.TRADING_PAIRS),
            }
        ]

        normalized_profiles: List[dict] = []
        used_pairs = set()

        for index, raw_profile in enumerate(source, start=1):
            if not isinstance(raw_profile, dict):
                continue

            name = str(raw_profile.get("name") or f"strategy_{index}").strip() or f"strategy_{index}"
            enabled = bool(raw_profile.get("enabled", True))
            strategy_type = self._normalize_strategy_type(raw_profile.get("strategy_type", "trend_signal"))
            entry_mode = self._normalize_entry_mode(raw_profile.get("entry_mode", "strong_only"))
            pairs = self.filter_disabled_pairs(raw_profile.get("pairs", []))
            risk_profile = {}
            if strategy_type == "trend_signal" and raw_profile.get("risk_profile") is not None:
                risk_profile = self._normalize_trend_risk_profile(raw_profile.get("risk_profile"))

            unique_pairs = []
            for symbol in pairs:
                if enabled and symbol in used_pairs:
                    logger.warning(
                        "⚠️ Par %s duplicado em STRATEGY_PROFILES. Mantendo apenas a primeira ocorrência.",
                        symbol,
                    )
                    continue
                if enabled:
                    used_pairs.add(symbol)
                unique_pairs.append(symbol)

            normalized_profile = {
                "name": name,
                "enabled": enabled,
                "strategy_type": strategy_type,
                "entry_mode": entry_mode,
                "pairs": unique_pairs,
            }
            if risk_profile:
                normalized_profile["risk_profile"] = risk_profile
            normalized_profiles.append(normalized_profile)

        enabled_profiles = [profile for profile in normalized_profiles if profile.get("enabled", True)]
        if not enabled_profiles:
            fallback_pairs = self.filter_disabled_pairs(self.TRADING_PAIRS)
            normalized_profiles = [
                {
                    "name": "primary",
                    "enabled": True,
                    "strategy_type": "trend_signal",
                    "entry_mode": "strong_only",
                    "pairs": fallback_pairs,
                }
            ]

        return normalized_profiles

    def get_enabled_strategy_profiles(self) -> List[dict]:
        """Retorna apenas perfis de estratégia habilitados."""
        profiles = list(getattr(self, "STRATEGY_PROFILES", []) or [])
        return [profile for profile in profiles if bool(profile.get("enabled", True))]
    
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

        strategy_profiles = self.get_enabled_strategy_profiles()
        if not strategy_profiles:
            errors.append("⚠️  ALERTA: Nenhum perfil habilitado em STRATEGY_PROFILES.")
        else:
            pair_owner = {}
            for profile in strategy_profiles:
                profile_name = str(profile.get("name") or "strategy").strip()
                strategy_type = self._normalize_strategy_type(profile.get("strategy_type", "trend_signal"))
                if strategy_type not in {"trend_signal", "range_scalping"}:
                    errors.append(f"⚠️  ALERTA: strategy_type inválido em {profile_name}: {strategy_type}")
                entry_mode = self._normalize_entry_mode(profile.get("entry_mode", "strong_only"))
                if entry_mode not in {"strong_only", "standard"}:
                    errors.append(f"⚠️  ALERTA: entry_mode inválido em {profile_name}: {entry_mode}")
                if profile.get("risk_profile") is not None:
                    if strategy_type != "trend_signal":
                        errors.append(
                            f"⚠️  ALERTA: risk_profile só é suportado para trend_signal ({profile_name})."
                        )
                    else:
                        self._normalize_trend_risk_profile(profile.get("risk_profile"))
                profile_pairs = self.normalize_pair_list(profile.get("pairs", []))
                dynamic_profile_allowed_empty = (
                    strategy_type == "trend_signal" and
                    bool(self.USE_BINANCE_STRATEGY)
                )
                if not profile_pairs and not dynamic_profile_allowed_empty:
                    errors.append(f"⚠️  ALERTA: Perfil {profile_name} está sem pares atribuídos.")
                for symbol in profile_pairs:
                    previous_owner = pair_owner.get(symbol)
                    if previous_owner and previous_owner != profile_name:
                        errors.append(
                            f"⚠️  ALERTA: Par {symbol} está duplicado em perfis ({previous_owner} e {profile_name})."
                        )
                    pair_owner[symbol] = profile_name

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

        if self.DAILY_PERFORMANCE_REPORT_HOUR_BRT < 0 or self.DAILY_PERFORMANCE_REPORT_HOUR_BRT > 23:
            errors.append("⚠️  ALERTA: DAILY_PERFORMANCE_REPORT_HOUR_BRT deve estar entre 0 e 23!")

        if self.DAILY_PERFORMANCE_REPORT_MINUTE_BRT < 0 or self.DAILY_PERFORMANCE_REPORT_MINUTE_BRT > 59:
            errors.append("⚠️  ALERTA: DAILY_PERFORMANCE_REPORT_MINUTE_BRT deve estar entre 0 e 59!")

        if self.DAILY_PERFORMANCE_REPORT_LOOKBACK_HOURS < 1 or self.DAILY_PERFORMANCE_REPORT_LOOKBACK_HOURS > 168:
            errors.append("⚠️  ALERTA: DAILY_PERFORMANCE_REPORT_LOOKBACK_HOURS deve estar entre 1 e 168!")

        if self.SENTIMENT_TIMEFRAME not in {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"}:
            errors.append("⚠️  ALERTA: SENTIMENT_TIMEFRAME inválido!")

        if self.RANGE_SCALP_EARLY_EXIT_TIMEFRAME not in {"1m", "3m", "5m", "15m", "30m", "1h"}:
            errors.append("⚠️  ALERTA: RANGE_SCALP_EARLY_EXIT_TIMEFRAME inválido!")

        if self.TREND_STRONG_EXECUTION_TIMEFRAME not in {"1m", "3m"}:
            errors.append("⚠️  ALERTA: TREND_STRONG_EXECUTION_TIMEFRAME deve ser '1m' ou '3m'!")

        if self.TREND_STRONG_CONFIRM_TIMEFRAME != "5m":
            errors.append("⚠️  ALERTA: TREND_STRONG_CONFIRM_TIMEFRAME deve ser '5m'!")

        if self.TREND_STRONG_CANDLES_LOOKBACK < 220 or self.TREND_STRONG_CANDLES_LOOKBACK > 1000:
            errors.append("⚠️  ALERTA: TREND_STRONG_CANDLES_LOOKBACK deve estar entre 220 e 1000!")

        if self.TREND_STRONG_PULLBACK_TOLERANCE_PERCENT < 0 or self.TREND_STRONG_PULLBACK_TOLERANCE_PERCENT > 2:
            errors.append("⚠️  ALERTA: TREND_STRONG_PULLBACK_TOLERANCE_PERCENT deve estar entre 0 e 2!")

        if not (0 <= self.TREND_STRONG_LONG_RSI_MIN < self.TREND_STRONG_LONG_RSI_MAX <= 100):
            errors.append("⚠️  ALERTA: Faixa RSI LONG do trend_strong é inválida!")

        if not (0 <= self.TREND_STRONG_SHORT_RSI_MIN < self.TREND_STRONG_SHORT_RSI_MAX <= 100):
            errors.append("⚠️  ALERTA: Faixa RSI SHORT do trend_strong é inválida!")

        if self.TREND_STRONG_MIN_VOLUME_RATIO <= 0 or self.TREND_STRONG_MIN_VOLUME_RATIO > 3:
            errors.append("⚠️  ALERTA: TREND_STRONG_MIN_VOLUME_RATIO deve estar entre 0 e 3!")

        if self.SENTIMENT_CANDLES_LOOKBACK < 30 or self.SENTIMENT_CANDLES_LOOKBACK > 1000:
            errors.append("⚠️  ALERTA: SENTIMENT_CANDLES_LOOKBACK deve estar entre 30 e 1000!")

        if self.SENTIMENT_MIN_SCORE < 1 or self.SENTIMENT_MIN_SCORE > 5:
            errors.append("⚠️  ALERTA: SENTIMENT_MIN_SCORE deve estar entre 1 e 5!")

        if self.SENTIMENT_MIN_MOMENTUM_PERCENT < 0 or self.SENTIMENT_MIN_MOMENTUM_PERCENT > 10:
            errors.append("⚠️  ALERTA: SENTIMENT_MIN_MOMENTUM_PERCENT deve estar entre 0 e 10!")

        if self.SENTIMENT_CACHE_SECONDS < 5 or self.SENTIMENT_CACHE_SECONDS > 3600:
            errors.append("⚠️  ALERTA: SENTIMENT_CACHE_SECONDS deve estar entre 5 e 3600!")

        if self.DOUBLE_FIRST_SCOPE not in {"global", "symbol"}:
            errors.append("⚠️  ALERTA: DOUBLE_FIRST_SCOPE deve ser 'global' ou 'symbol'!")

        if self.DOUBLE_FIRST_MULTIPLIER < 1.0 or self.DOUBLE_FIRST_MULTIPLIER > 10.0:
            errors.append("⚠️  ALERTA: DOUBLE_FIRST_MULTIPLIER deve estar entre 1.0 e 10.0!")

        if self.DOUBLE_FIRST_MAX_MARGIN_USDT < 0:
            errors.append("⚠️  ALERTA: DOUBLE_FIRST_MAX_MARGIN_USDT deve ser >= 0!")

        if self.RANGE_SCALP_MIN_RANGE_PERCENT <= 0:
            errors.append("⚠️  ALERTA: RANGE_SCALP_MIN_RANGE_PERCENT deve ser > 0!")

        if self.RANGE_SCALP_MIN_RANGE_MINUTES < 15:
            errors.append("⚠️  ALERTA: RANGE_SCALP_MIN_RANGE_MINUTES deve ser >= 15!")

        if self.RANGE_SCALP_MIN_TOUCHES_PER_SIDE < 1:
            errors.append("⚠️  ALERTA: RANGE_SCALP_MIN_TOUCHES_PER_SIDE deve ser >= 1!")

        if self.RANGE_SCALP_EDGE_ZONE_RATIO <= 0 or self.RANGE_SCALP_EDGE_ZONE_RATIO >= 0.5:
            errors.append("⚠️  ALERTA: RANGE_SCALP_EDGE_ZONE_RATIO deve estar entre 0 e 0.5!")

        if self.RANGE_SCALP_MIN_POSITION_MULTIPLIER <= 0:
            errors.append("⚠️  ALERTA: RANGE_SCALP_MIN_POSITION_MULTIPLIER deve ser > 0!")

        if self.RANGE_SCALP_MAX_POSITION_MULTIPLIER < self.RANGE_SCALP_MIN_POSITION_MULTIPLIER:
            errors.append(
                "⚠️  ALERTA: RANGE_SCALP_MAX_POSITION_MULTIPLIER deve ser >= RANGE_SCALP_MIN_POSITION_MULTIPLIER!"
            )

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

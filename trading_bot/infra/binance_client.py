"""
CONEXÃO COM A BINANCE FUTURES API
=================================
Este módulo gerencia toda a comunicação com a exchange.
Usa a biblioteca python-binance para Python.

Instale com: pip install python-binance
"""

import logging
import random
import time
import threading
from typing import Any, Callable, Dict, List, Optional
from binance.client import Client
from binance.exceptions import BinanceAPIException, BinanceRequestException
from ..core.config import config
from ..observability import metrics
from .binance_streams import WebSocketKlineStore
from .binance_user_stream import UserStreamMonitor

logger = logging.getLogger(__name__)


# Códigos de erro da Binance que indicam rejeição estrutural — ou seja,
# que não vai se resolver retentando em segundos. O tratamento padrão é
# entrar em cooldown por símbolo (ver SYMBOL_STRUCTURAL_COOLDOWN_SECONDS).
#   -2027: Exceeded the maximum allowable position at current leverage
STRUCTURAL_REJECTION_CODES: frozenset = frozenset({-2027})


class BinanceConnection:
    """
    Classe que encapsula toda a comunicação com a Binance.
    Usa o cliente unificado da python-binance para Futures.
    """
    
    def __init__(self):
        """
        Inicializa a conexão com a Binance.
        Se USE_TESTNET for True, conecta na testnet (dinheiro fake).
        """
        self.config = config
        self.api_retry_attempts = max(1, int(getattr(self.config, "API_RETRY_ATTEMPTS", 4)))
        self.api_retry_base_delay = max(0.1, float(getattr(self.config, "API_RETRY_BASE_DELAY", 0.5)))
        self.api_retry_max_delay = max(self.api_retry_base_delay, float(getattr(self.config, "API_RETRY_MAX_DELAY", 5.0)))
        self.api_retry_jitter = max(0.0, float(getattr(self.config, "API_RETRY_JITTER", 0.25)))
        self.api_retry_stats_interval = max(
            1.0,
            float(getattr(self.config, "API_RETRY_STATS_INTERVAL_SECONDS", 60))
        )
        self._api_retry_stats_lock = threading.Lock()
        self._retry_stats_since_report = self._new_retry_stats()
        self._reset_retry_stats(time.monotonic())
        self._order_stats_lock = threading.Lock()
        self._order_stats_since_report = self._new_order_stats()

        # Cooldown por símbolo após rejeição estrutural (ex.: -2027). Map
        # symbol -> {"until_monotonic", "code", "message", "set_at_wall"}.
        self._symbol_cooldowns_lock = threading.Lock()
        self._symbol_cooldowns: Dict[str, Dict[str, Any]] = {}

        # Improvement 6: TTL cache para get_symbol_info e get_exchange_info
        self._symbol_info_cache: Dict[str, Dict] = {}
        self._symbol_info_cache_ts: Dict[str, float] = {}
        self._symbol_info_cache_ttl: float = 21600.0  # 6 horas
        self._exchange_info_cache: Optional[Dict] = None
        self._exchange_info_cache_ts: float = 0.0

        # Improvement 7: TTL cache para endpoints de alta frequência (account + funding_rate).
        # Balance TTL curto (2s): saldo muda pouco entre ticks, mas nunca fica realmente stale.
        # Funding rate TTL longo (60s): funding roda a cada 8h; a leitura é quase estável.
        # Invalidação manual: invalidate_balance_cache() após place_market_order / close_position.
        self._cache_lock = threading.Lock()
        self._balance_cache: Optional[Dict[str, float]] = None  # {"wallet","available","ts"}
        self._balance_cache_ttl: float = 2.0
        self._funding_rate_cache: Dict[str, Dict[str, Any]] = {}  # symbol -> {"data","ts"}
        # TTL longo: funding rate só muda a cada 8h no settlement. 5min dá hit rate
        # > 95% mesmo com status print a cada 30s iterando 9 pares.
        self._funding_rate_cache_ttl: float = 300.0
        # Daily PnL: quase só muda quando trade fecha. Cache longo + invalidação
        # junto do balance (via invalidate_balance_cache após place_market_order).
        self._daily_pnl_cache: Optional[Dict[str, Any]] = None  # {"data","ts"}
        self._daily_pnl_cache_ttl: float = 30.0
        # Open positions cache (Fase 3.4 C): TTL curto pra não sacrificar
        # precisão de trailing stop. Invalidado manualmente após place_market_order
        # / close_position, e via WebSocket user stream (ACCOUNT_UPDATE) em tempo real.
        self._positions_cache: Optional[Dict[str, Any]] = None  # {"data","ts"}
        self._positions_cache_ttl: float = 5.0

        # Improvement 8: WebSocket kline store (substitui REST polling em get_klines).
        # Se habilitado, start() + subscribe por par/intervalo. REST é fallback automático.
        # Kill switch via TRADING_BOT_WEBSOCKET_ENABLED=false.
        self._ws_store: Optional[WebSocketKlineStore] = None
        if bool(getattr(self.config, "WEBSOCKET_ENABLED", True)):
            try:
                self._ws_store = WebSocketKlineStore(
                    api_key=self.config.API_KEY,
                    api_secret=self.config.API_SECRET,
                    testnet=bool(self.config.USE_TESTNET),
                    rest_seed_fetcher=self._rest_get_klines,
                    staleness_seconds=float(
                        getattr(self.config, "WEBSOCKET_STALENESS_SECONDS", 30.0)
                    ),
                    # Seed 400 velas dá margem folgada sobre os 260 típicos da
                    # estratégia, evitando miss em casos de borda.
                    seed_limit=400,
                )
                if not self._ws_store.start():
                    logger.warning("⚠️ WebSocketKlineStore não iniciou; seguindo com REST")
                    self._ws_store = None
            except Exception as exc:
                logger.warning(f"⚠️ Falha ao criar WebSocketKlineStore: {exc}")
                self._ws_store = None

        # User stream (Fase 3.4 C): recebe ACCOUNT_UPDATE / ORDER_TRADE_UPDATE
        # pra invalidar positions_cache em tempo real. Compartilha o TWM do
        # ws_store — criar um segundo TWM no mesmo processo causa
        # "This event loop is already running" no asyncio do Python 3.13.
        self._user_stream: Optional[UserStreamMonitor] = None
        shared_twm = self._ws_store.get_twm() if self._ws_store is not None else None
        if shared_twm is not None:
            try:
                self._user_stream = UserStreamMonitor(
                    twm=shared_twm,
                    on_account_update=self._on_user_account_update,
                    on_order_update=self._on_user_order_update,
                )
                if not self._user_stream.start():
                    logger.warning("⚠️ UserStreamMonitor não iniciou; cache de posições só com TTL")
                    self._user_stream = None
            except Exception as exc:
                logger.warning(f"⚠️ Falha ao criar UserStreamMonitor: {exc}")
                self._user_stream = None

        # Inicializa o cliente
        if self.config.USE_TESTNET:
            # Testnet - para testes sem dinheiro real
            logger.info("🧪 Conectando na TESTNET (dinheiro de teste)")
            self.client = Client(
                api_key=self.config.API_KEY,
                api_secret=self.config.API_SECRET,
                testnet=True
            )
            # Define as URLs da testnet de futuros
            self.client.FUTURES_URL = 'https://testnet.binancefuture.com/fapi'
        else:
            # Mainnet - dinheiro real!
            logger.warning("💰 ATENÇÃO: Conectando na MAINNET (dinheiro REAL)!")
            self.client = Client(
                api_key=self.config.API_KEY,
                api_secret=self.config.API_SECRET
            )
        
        # Testa a conexão
        self._test_connection()

    @staticmethod
    def _new_retry_stats() -> Dict[str, Any]:
        """Cria estrutura base para contadores de retry/falha."""
        return {
            'calls': 0,
            'retries': 0,
            'failures': 0,
            'endpoints': {}
        }

    @staticmethod
    def _new_order_stats() -> Dict[str, Any]:
        """Cria estrutura base para contadores de execução de ordens."""
        return {
            'attempts': 0,
            'successes': 0,
            'failures': 0,
            'rejections': 0,
            'symbols': {}
        }

    def _reset_retry_stats(self, window_start: float):
        """Reseta acumuladores de retry/falha para nova janela de agregação."""
        self._retry_stats_window_start = window_start
        self._retry_stats = self._new_retry_stats()

    def _record_retry_stat(self, label: str, calls: int = 0, retries: int = 0, failures: int = 0):
        """Acumula estatísticas por endpoint para telemetria agregada."""
        with self._api_retry_stats_lock:
            endpoint = self._retry_stats['endpoints'].setdefault(
                label,
                {'calls': 0, 'retries': 0, 'failures': 0}
            )
            if calls:
                self._retry_stats['calls'] += calls
                endpoint['calls'] += calls
            if retries:
                self._retry_stats['retries'] += retries
                endpoint['retries'] += retries
            if failures:
                self._retry_stats['failures'] += failures
                endpoint['failures'] += failures

            # Também acumula no agregado "desde último report"
            endpoint_report = self._retry_stats_since_report['endpoints'].setdefault(
                label,
                {'calls': 0, 'retries': 0, 'failures': 0}
            )
            if calls:
                self._retry_stats_since_report['calls'] += calls
                endpoint_report['calls'] += calls
            if retries:
                self._retry_stats_since_report['retries'] += retries
                endpoint_report['retries'] += retries
            if failures:
                self._retry_stats_since_report['failures'] += failures
                endpoint_report['failures'] += failures

    def _maybe_log_retry_stats(self, force: bool = False):
        """
        Emite log agregado de retries/falhas por janela.
        Loga apenas quando houver retry ou falha para evitar ruído.
        """
        now = time.monotonic()
        with self._api_retry_stats_lock:
            elapsed = now - self._retry_stats_window_start
            if not force and elapsed < self.api_retry_stats_interval:
                return

            snapshot = self._retry_stats
            self._reset_retry_stats(now)

        total_calls = snapshot['calls']
        total_retries = snapshot['retries']
        total_failures = snapshot['failures']

        # Evita ruído quando a janela foi estável
        if total_calls == 0 or (total_retries == 0 and total_failures == 0):
            return

        retry_rate = (total_retries / total_calls) * 100 if total_calls else 0.0
        failure_rate = (total_failures / total_calls) * 100 if total_calls else 0.0
        window_seconds = max(1, int(round(elapsed)))

        header = (
            f"📡 API health ({window_seconds}s): calls={total_calls} | "
            f"retries={total_retries} ({retry_rate:.1f}%) | "
            f"failures={total_failures} ({failure_rate:.1f}%)"
        )
        # Só eleva para WARNING quando a taxa de falhas é relevante (>=2% ou >=3 falhas).
        significant_failure = total_failures >= 3 or failure_rate >= 2.0
        if significant_failure:
            logger.warning(header)
        else:
            logger.info(header)

        endpoints = snapshot['endpoints']
        ranked = sorted(
            endpoints.items(),
            key=lambda item: (item[1]['failures'], item[1]['retries'], item[1]['calls']),
            reverse=True
        )

        for label, data in ranked[:5]:
            if data['retries'] == 0 and data['failures'] == 0:
                continue
            msg = (
                f"   • {label}: calls={data['calls']} | "
                f"retries={data['retries']} | failures={data['failures']}"
            )
            endpoint_failure_rate = (data['failures'] / data['calls']) * 100 if data['calls'] else 0.0
            if data['failures'] >= 3 or endpoint_failure_rate >= 2.0:
                logger.warning(msg)
            else:
                logger.info(msg)

    def flush_retry_stats(self):
        """
        Força emissão do log agregado da janela atual.
        """
        self._maybe_log_retry_stats(force=True)

    def get_retry_stats_report(self, reset: bool = True) -> Dict[str, Any]:
        """
        Retorna estatísticas agregadas de API desde o último report.
        """
        with self._api_retry_stats_lock:
            snapshot = {
                'calls': self._retry_stats_since_report['calls'],
                'retries': self._retry_stats_since_report['retries'],
                'failures': self._retry_stats_since_report['failures'],
                'endpoints': {
                    label: data.copy()
                    for label, data in self._retry_stats_since_report['endpoints'].items()
                }
            }
            if reset:
                self._retry_stats_since_report = self._new_retry_stats()

        calls = snapshot['calls']
        retries = snapshot['retries']
        failures = snapshot['failures']
        retry_rate = (retries / calls * 100) if calls else 0.0
        failure_rate = (failures / calls * 100) if calls else 0.0

        ranked = sorted(
            snapshot['endpoints'].items(),
            key=lambda item: (item[1]['failures'], item[1]['retries'], item[1]['calls']),
            reverse=True
        )

        endpoints = []
        for label, data in ranked:
            endpoints.append({
                'label': label,
                'calls': data['calls'],
                'retries': data['retries'],
                'failures': data['failures']
            })

        return {
            'calls': calls,
            'retries': retries,
            'failures': failures,
            'retry_rate': retry_rate,
            'failure_rate': failure_rate,
            'endpoints': endpoints
        }

    def _record_order_stat(
        self,
        symbol: str,
        attempts: int = 0,
        successes: int = 0,
        failures: int = 0,
        rejections: int = 0
    ):
        """Acumula métricas de execução de ordens por símbolo."""
        with self._order_stats_lock:
            data = self._order_stats_since_report
            symbol_data = data['symbols'].setdefault(
                symbol,
                {'attempts': 0, 'successes': 0, 'failures': 0, 'rejections': 0}
            )
            if attempts:
                data['attempts'] += attempts
                symbol_data['attempts'] += attempts
            if successes:
                data['successes'] += successes
                symbol_data['successes'] += successes
            if failures:
                data['failures'] += failures
                symbol_data['failures'] += failures
            if rejections:
                data['rejections'] += rejections
                symbol_data['rejections'] += rejections

    def _is_order_rejection(self, error: Exception) -> bool:
        """Classifica erros de ordem rejeitada pela exchange/validação."""
        api_code = getattr(error, "code", None)
        message = str(error).lower()

        rejection_codes = {
            -2010,  # New order rejected
            -2019,  # Margin is insufficient
            -2021,  # Order would immediately trigger
            -2027,  # Exceeded the maximum allowable position at current leverage
            -1111,  # BAD_PRECISION
            -1116,  # Invalid order type
            -1110,  # BAD_INSTRUMENT_TYPE
            -4164,  # Order's notional must be no smaller than...
        }
        if api_code in rejection_codes:
            return True

        rejection_patterns = [
            "insufficient",
            "min notional",
            "notional",
            "filter failure",
            "precision",
            "invalid quantity",
            "reduceonly",
            "position side",
            "would immediately trigger",
            "new order rejected",
            "order rejected",
            "lot size",
        ]
        return any(pattern in message for pattern in rejection_patterns)

    # ------------------------------------------------------------------
    # Cooldown por símbolo (rejeições estruturais da exchange)
    # ------------------------------------------------------------------

    def _structural_cooldown_seconds(self) -> float:
        return max(
            0.0,
            float(getattr(self.config, "SYMBOL_STRUCTURAL_COOLDOWN_SECONDS", 1800)),
        )

    def _set_symbol_cooldown(self, symbol: str, code: int, message: str) -> float:
        """Marca o símbolo em cooldown estrutural. Retorna duração aplicada."""
        duration = self._structural_cooldown_seconds()
        if duration <= 0:
            return 0.0
        now_mono = time.monotonic()
        with self._symbol_cooldowns_lock:
            self._symbol_cooldowns[symbol] = {
                "until_monotonic": now_mono + duration,
                "code": int(code),
                "message": str(message),
                "set_at_wall": time.time(),
            }
        logger.warning(
            f"⏳ Cooldown estrutural ativado para {symbol}: code={code} "
            f"duração={duration:.0f}s motivo={message}"
        )
        return duration

    def is_symbol_on_cooldown(self, symbol: str) -> bool:
        with self._symbol_cooldowns_lock:
            info = self._symbol_cooldowns.get(symbol)
            if info is None:
                return False
            if time.monotonic() >= info["until_monotonic"]:
                del self._symbol_cooldowns[symbol]
                return False
            return True

    def get_symbol_cooldown_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Retorna info do cooldown ativo (ou None se expirado/inexistente)."""
        with self._symbol_cooldowns_lock:
            info = self._symbol_cooldowns.get(symbol)
            if info is None:
                return None
            remaining = info["until_monotonic"] - time.monotonic()
            if remaining <= 0:
                del self._symbol_cooldowns[symbol]
                return None
            return {
                "code": info["code"],
                "message": info["message"],
                "remaining_seconds": remaining,
                "set_at_wall": info["set_at_wall"],
            }

    def clear_symbol_cooldown(self, symbol: str) -> bool:
        with self._symbol_cooldowns_lock:
            return self._symbol_cooldowns.pop(symbol, None) is not None

    def get_order_stats_report(self, reset: bool = True) -> Dict[str, Any]:
        """
        Retorna estatísticas de execução de ordens desde o último report.
        """
        with self._order_stats_lock:
            snapshot = {
                'attempts': self._order_stats_since_report['attempts'],
                'successes': self._order_stats_since_report['successes'],
                'failures': self._order_stats_since_report['failures'],
                'rejections': self._order_stats_since_report['rejections'],
                'symbols': {
                    symbol: data.copy()
                    for symbol, data in self._order_stats_since_report['symbols'].items()
                }
            }
            if reset:
                self._order_stats_since_report = self._new_order_stats()

        attempts = snapshot['attempts']
        failures = snapshot['failures']
        rejections = snapshot['rejections']

        failure_rate = (failures / attempts * 100) if attempts else 0.0
        rejection_rate = (rejections / attempts * 100) if attempts else 0.0

        ranked = sorted(
            snapshot['symbols'].items(),
            key=lambda item: (item[1]['failures'], item[1]['rejections'], item[1]['attempts']),
            reverse=True
        )
        symbols = []
        for symbol, data in ranked:
            symbols.append({
                'symbol': symbol,
                'attempts': data['attempts'],
                'successes': data['successes'],
                'failures': data['failures'],
                'rejections': data['rejections'],
            })

        return {
            'attempts': attempts,
            'successes': snapshot['successes'],
            'failures': failures,
            'rejections': rejections,
            'failure_rate': failure_rate,
            'rejection_rate': rejection_rate,
            'symbols': symbols,
        }

    def _is_retryable_error(self, error: Exception, allow_timeout_retry: bool = True) -> bool:
        """
        Determina se um erro da API é elegível para retry.
        """
        status_code = getattr(error, "status_code", None)
        api_code = getattr(error, "code", None)
        message = str(error).lower()

        retryable_status = {418, 429, 500, 502, 503, 504}
        retryable_api_codes = {-1000, -1001, -1003, -1015}

        if status_code in retryable_status:
            return True

        if api_code in retryable_api_codes:
            return True

        if isinstance(error, BinanceRequestException):
            return True

        if isinstance(error, BinanceAPIException):
            # Se veio daqui e não caiu nas regras acima, trata como não-retry por padrão
            return False

        # Timeout/conexão: para operações não idempotentes, evitamos retry agressivo
        if allow_timeout_retry:
            timeout_patterns = [
                "timed out",
                "timeout",
                "connection aborted",
                "connection reset",
                "temporarily unavailable",
                "service unavailable",
                "internal error",
                "read timed out",
                "too many requests",
                "rate limit",
                "remote end closed",
                "network",
            ]
            if any(pattern in message for pattern in timeout_patterns):
                return True

        return False

    def _api_call(
        self,
        label: str,
        fn: Callable[..., Any],
        *args,
        retry: bool = True,
        allow_timeout_retry: bool = True,
        **kwargs,
    ) -> Any:
        """
        Executa chamada da Binance com retry exponencial + jitter.
        """
        attempts = self.api_retry_attempts if retry else 1
        last_error = None
        self._record_retry_stat(label, calls=1)

        for attempt in range(1, attempts + 1):
            try:
                result = fn(*args, **kwargs)
                self._maybe_log_retry_stats()
                return result
            except Exception as error:
                last_error = error
                is_last_attempt = attempt >= attempts
                can_retry = (
                    not is_last_attempt
                    and self._is_retryable_error(error, allow_timeout_retry=allow_timeout_retry)
                )

                if not can_retry:
                    break

                base_backoff = min(
                    self.api_retry_max_delay,
                    self.api_retry_base_delay * (2 ** (attempt - 1)),
                )
                jitter = random.uniform(0, self.api_retry_jitter)
                sleep_seconds = base_backoff + jitter

                logger.warning(
                    f"⚠️ API falhou ({label}) tentativa {attempt}/{attempts}: {error}. "
                    f"Retry em {sleep_seconds:.2f}s..."
                )
                self._record_retry_stat(label, retries=1)
                time.sleep(sleep_seconds)

        self._record_retry_stat(label, failures=1)
        self._maybe_log_retry_stats()
        raise last_error
    
    def _test_connection(self) -> bool:
        """
        Testa se a conexão com a API está funcionando.
        Retorna True se ok, levanta exceção se falhar.
        """
        try:
            # Tenta pegar o tempo do servidor
            server_time = self._api_call("futures_time", self.client.futures_time)
            logger.info(f"✅ Conexão OK! Server time: {server_time}")
            
            # Verifica o saldo da conta
            account = self._api_call("futures_account", self.client.futures_account)
            balance = float(account.get('totalWalletBalance', 0))
            logger.info(f"💵 Saldo total da carteira: ${balance:.2f} USDT")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Erro de conexão: {e}")
            raise ConnectionError(f"Não foi possível conectar à Binance: {e}")
    
    def _fetch_account_cached(self, force_refresh: bool = False) -> Dict[str, float]:
        """
        Retorna saldo wallet + available de Futuros, com cache TTL curto.

        Consolida as duas leituras num único snapshot pra evitar chamadas
        redundantes no mesmo tick (análise de 9 pares faria 18+ chamadas).

        O cache é invalidado automaticamente após place_market_order e
        close_position via `invalidate_balance_cache()`.
        """
        now = time.monotonic()
        with self._cache_lock:
            cached = self._balance_cache
            if (not force_refresh) and cached and (now - cached["ts"]) < self._balance_cache_ttl:
                metrics.record_cache_hit("balance")
                return cached

        metrics.record_cache_miss("balance")
        try:
            account = self._api_call("futures_account", self.client.futures_account)
        except Exception as e:
            logger.error(f"Erro ao obter account da Binance: {e}")
            # Se temos cache stale, devolve ele em vez de zerar tudo — preserva decisão
            # de risco razoável durante quedas transitórias de API.
            with self._cache_lock:
                if self._balance_cache:
                    return self._balance_cache
            return {"wallet": 0.0, "available": 0.0, "ts": now}

        wallet = float(account.get('totalWalletBalance', 0) or 0)
        available = 0.0
        for asset in account.get('assets', []) or []:
            if asset.get('asset') == 'USDT':
                available = float(asset.get('availableBalance', 0) or 0)
                break

        wallet, available = self._apply_simulated_cap(wallet, available)

        snapshot = {"wallet": wallet, "available": available, "ts": time.monotonic()}
        with self._cache_lock:
            self._balance_cache = snapshot
        return snapshot

    def _apply_simulated_cap(self, wallet: float, available: float) -> tuple[float, float]:
        """
        Aplica cap do SIMULATED_BALANCE_USD em testnet. No-op em mainnet
        ou quando a config é <= 0. Permite simular começar com capital pequeno
        sem precisar fazer transfer na testnet.

        Preserva a margem usada em posições reais: available simulado =
        max(0, cap - margem_usada_real).
        """
        cap = float(getattr(self.config, "SIMULATED_BALANCE_USD", 0.0) or 0.0)
        if cap <= 0:
            return wallet, available
        if not bool(getattr(self.config, "USE_TESTNET", False)):
            return wallet, available  # Segurança: nunca capa mainnet
        margin_used = max(0.0, wallet - available)
        simulated_available = max(0.0, cap - margin_used)
        return cap, simulated_available

    def get_account_balance(self, force_refresh: bool = False) -> float:
        """
        Retorna o saldo TOTAL da carteira de FUTUROS (walletBalance).

        Este é o valor que aparece na Binance como "Saldo da Carteira".
        Inclui a margem usada em posições abertas.

        Para saldo DISPONÍVEL (livre para novos trades), use get_available_balance().

        Args:
            force_refresh: Se True, ignora o cache e força chamada à API.
                           Use após ordens que mudam estado (open/close position).
        """
        return self._fetch_account_cached(force_refresh=force_refresh)["wallet"]

    def get_available_balance(self, force_refresh: bool = False) -> float:
        """
        Retorna o saldo DISPONÍVEL em USDT (livre para abrir novas posições).

        Este valor é menor que o walletBalance quando há posições abertas,
        pois parte do saldo está sendo usada como margem.

        Args:
            force_refresh: Se True, ignora o cache e força chamada à API.
                           Use após ordens que mudam estado (open/close position).
        """
        return self._fetch_account_cached(force_refresh=force_refresh)["available"]

    def invalidate_balance_cache(self) -> None:
        """
        Marca o cache de balance E daily_pnl como stale. Próxima leitura força fetch.
        Chame após qualquer ação que mude o saldo (abertura/fechamento de posição,
        ajuste de alavancagem em posição aberta, depósito/saque detectado).

        daily_pnl é invalidado junto porque fechamento de posição afeta o P&L realizado.
        """
        with self._cache_lock:
            self._balance_cache = None
            self._daily_pnl_cache = None

    # ------------------------------------------------------------------
    # WebSocket kline streams (delegados ao _ws_store)
    # ------------------------------------------------------------------

    def subscribe_klines_stream(self, symbol: str, interval: str) -> bool:
        """
        Inscreve o par/intervalo no stream WS de klines.
        No-op se WS está desligado. Safe de chamar múltiplas vezes.
        """
        if self._ws_store is None:
            return False
        return self._ws_store.subscribe(symbol, interval)

    def unsubscribe_klines_stream(self, symbol: str, interval: str) -> None:
        """Remove subscrição WS do par/intervalo. Idempotente."""
        if self._ws_store is None:
            return
        self._ws_store.unsubscribe(symbol, interval)

    def get_ws_stats(self) -> Optional[Dict[str, Any]]:
        """Snapshot das métricas WS pra exportação Prometheus. None se desligado."""
        if self._ws_store is None:
            return None
        return self._ws_store.get_stats()

    def get_user_stream_stats(self) -> Optional[Dict[str, Any]]:
        """Snapshot das métricas do user stream. None se desligado."""
        if self._user_stream is None:
            return None
        return self._user_stream.get_stats()

    def _on_user_account_update(self, msg: dict) -> None:
        """
        Callback de ACCOUNT_UPDATE via user stream. Invalida caches
        derivados (positions / balance / daily_pnl) pra próxima leitura
        refletir o estado real imediatamente.
        """
        self.invalidate_positions_cache()
        self.invalidate_balance_cache()

    def _on_user_order_update(self, msg: dict) -> None:
        """
        Callback de ORDER_TRADE_UPDATE. Só invalida caches quando a ordem
        efetivamente afeta a posição (FILLED / PARTIALLY_FILLED). Status
        puro de NEW/CANCELED não muda estado.
        """
        order_data = msg.get("o") or {}
        status = order_data.get("X")  # execution type (FILLED, PARTIALLY_FILLED, ...)
        if status in ("FILLED", "PARTIALLY_FILLED"):
            self.invalidate_positions_cache()
            self.invalidate_balance_cache()

    def shutdown(self) -> None:
        """
        Encerra conexões auxiliares (WebSocket). Chame no shutdown do bot
        pra não vazar threads.
        """
        if getattr(self, "_ws_store", None) is not None:
            try:
                self._ws_store.stop()
            except Exception as exc:
                logger.warning(f"Erro ao parar WS store: {exc}")
            self._ws_store = None
        if getattr(self, "_user_stream", None) is not None:
            try:
                self._user_stream.stop()
            except Exception as exc:
                logger.warning(f"Erro ao parar user stream: {exc}")
            self._user_stream = None
    
    def get_account_info(self) -> dict:
        """
        Retorna informações completas da conta de Futuros.
        
        Inclui:
        - walletBalance: Saldo total da carteira
        - availableBalance: Saldo disponível para novos trades
        - totalUnrealizedProfit: P&L não realizado total
        - totalMarginBalance: Saldo de margem total
        """
        try:
            account = self._api_call("futures_account", self.client.futures_account)

            wallet = float(account.get('totalWalletBalance', 0))
            available = float(account.get('availableBalance', 0))
            wallet, available = self._apply_simulated_cap(wallet, available)

            return {
                'wallet_balance': wallet,
                'available_balance': available,
                'unrealized_pnl': float(account.get('totalUnrealizedProfit', 0)),
                'margin_balance': float(account.get('totalMarginBalance', 0)),
                'total_initial_margin': float(account.get('totalInitialMargin', 0)),
            }
            
        except Exception as e:
            logger.error(f"Erro ao obter info da conta: {e}")
            return {
                'wallet_balance': 0,
                'available_balance': 0,
                'unrealized_pnl': 0,
                'margin_balance': 0,
                'total_initial_margin': 0,
            }
    
    def get_income_history(
        self,
        income_type: str = None,
        limit: int = 100,
        symbol: str = None,
        start_time: int = None
    ) -> list:
        """
        Busca o histórico de income (receitas/despesas) da conta de Futuros.
        
        Tipos:
        - REALIZED_PNL: P&L realizado de trades
        - FUNDING_FEE: Taxas de funding
        - COMMISSION: Comissões
        - TRANSFER: Transferências
        
        Útil para calcular o P&L diário real como a Binance mostra.
        """
        try:
            params = {'limit': limit}
            if income_type:
                params['incomeType'] = income_type
            if symbol:
                params['symbol'] = symbol
            if start_time:
                params['startTime'] = start_time
            
            income = self._api_call("futures_income_history", self.client.futures_income_history, **params)
            return income
            
        except Exception as e:
            logger.error(f"Erro ao obter histórico de income: {e}")
            return []
    
    def get_daily_pnl_from_binance(self, force_refresh: bool = False) -> dict:
        """
        Calcula o P&L diário REAL como a Binance mostra, com cache TTL 30s.

        Busca diretamente da API o histórico de income do dia e soma:
        - REALIZED_PNL: Lucros/perdas de trades fechados
        - FUNDING_FEE: Taxas de funding (a cada 8h)
        - COMMISSION: Comissões de trades

        O valor só muda quando trade fecha — o bot já invalida o cache
        automaticamente via invalidate_balance_cache() após place_market_order.
        TTL de 30s dá margem para refletir funding periódico.

        Args:
            force_refresh: Se True, ignora cache e força fetch.

        Returns:
            dict com realized_pnl / funding_fee / commission / total / income_count / income_types
        """
        now_mono = time.monotonic()
        with self._cache_lock:
            cached = self._daily_pnl_cache
            if (not force_refresh) and cached and (now_mono - cached["ts"]) < self._daily_pnl_cache_ttl:
                metrics.record_cache_hit("daily_pnl")
                return cached["data"]

        metrics.record_cache_miss("daily_pnl")
        try:
            from datetime import datetime, timezone

            # Início do dia UTC (00:00:00 UTC)
            now = datetime.now(timezone.utc)
            start_of_day = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
            start_timestamp = int(start_of_day.timestamp() * 1000)

            income_list = self._api_call(
                "futures_income_history_daily",
                self.client.futures_income_history,
                startTime=start_timestamp,
                limit=1000,  # Máximo permitido
            )

            realized_pnl = 0.0
            funding_fee = 0.0
            commission = 0.0
            income_types_found = set()

            for item in income_list:
                income_type = item.get('incomeType', '')
                amount = float(item.get('income', 0))
                income_types_found.add(income_type)

                if income_type == 'REALIZED_PNL':
                    realized_pnl += amount
                elif income_type == 'FUNDING_FEE':
                    funding_fee += amount
                    logger.debug(f"💸 Funding encontrado: ${amount:.4f} - {item.get('symbol', 'N/A')}")
                elif income_type == 'COMMISSION':
                    commission += amount

            if income_types_found:
                logger.debug(f"📊 Tipos de income encontrados: {', '.join(income_types_found)}")

            total = realized_pnl + funding_fee + commission
            result = {
                'realized_pnl': realized_pnl,
                'funding_fee': funding_fee,
                'commission': commission,
                'total': total,
                'income_count': len(income_list),
                'income_types': list(income_types_found),
            }
            with self._cache_lock:
                self._daily_pnl_cache = {"data": result, "ts": time.monotonic()}
            return result

        except Exception as e:
            logger.error(f"Erro ao obter P&L diário da Binance: {e}")
            # Fallback: cache stale se existir, senão dict vazio
            with self._cache_lock:
                if self._daily_pnl_cache:
                    return self._daily_pnl_cache["data"]
            return {
                'realized_pnl': 0.0,
                'funding_fee': 0.0,
                'commission': 0.0,
                'total': 0.0,
                'income_count': 0,
                'income_types': [],
            }
    
    def get_current_price(self, symbol: str) -> float:
        """
        Retorna o preço atual de um par.
        
        Args:
            symbol: Par de trading (ex: 'ETHUSDT')
        
        Returns:
            Preço atual como float
        """
        try:
            ticker = self._api_call(
                "futures_symbol_ticker",
                self.client.futures_symbol_ticker,
                symbol=symbol
            )
            return float(ticker['price'])
        except Exception as e:
            logger.error(f"Erro ao obter preço de {symbol}: {e}")
            return 0.0
    
    def get_funding_rate(self, symbol: str, force_refresh: bool = False) -> dict:
        """
        Busca o funding rate atual de um par, com cache TTL de 60s.

        O funding rate é cobrado/pago a cada 8 horas (00:00, 08:00, 16:00 UTC).
        - Rate POSITIVO: LONGs pagam para SHORTs
        - Rate NEGATIVO: SHORTs pagam para LONGs

        Args:
            symbol: Par de trading (ex: 'BTCUSDT')
            force_refresh: Se True, ignora o cache e força fetch.

        Returns:
            Dict com:
            - rate: Taxa atual (ex: 0.0001 = 0.01%)
            - rate_percent: Taxa em % (ex: 0.01)
            - long_pays: True se LONGs pagam (rate > 0)
            - next_funding_time: Próximo horário de cobrança
        """
        now = time.monotonic()
        with self._cache_lock:
            entry = self._funding_rate_cache.get(symbol)
            if (not force_refresh) and entry and (now - entry["ts"]) < self._funding_rate_cache_ttl:
                metrics.record_cache_hit("funding_rate")
                return entry["data"]

        metrics.record_cache_miss("funding_rate")
        try:
            # Busca informações do funding rate
            funding_info = self._api_call(
                "futures_funding_rate",
                self.client.futures_funding_rate,
                symbol=symbol,
                limit=1
            )

            if funding_info and len(funding_info) > 0:
                rate = float(funding_info[0].get('fundingRate', 0))
            else:
                rate = 0.0

            # Busca próximo horário de funding
            premium_info = self._api_call(
                "futures_mark_price",
                self.client.futures_mark_price,
                symbol=symbol
            )
            next_funding_time = premium_info.get('nextFundingTime', 0)
            
            # Converte timestamp para datetime
            from datetime import datetime, timezone
            if next_funding_time:
                next_funding_dt = datetime.fromtimestamp(next_funding_time / 1000, tz=timezone.utc)
            else:
                next_funding_dt = None
            
            result = {
                'rate': rate,
                'rate_percent': rate * 100,  # Em percentual
                'long_pays': rate > 0,       # True = LONGs pagam
                'short_pays': rate < 0,      # True = SHORTs pagam
                'next_funding_time': next_funding_dt,
                'favorable_side': 'SHORT' if rate > 0 else 'LONG' if rate < 0 else 'NEUTRAL'
            }
            with self._cache_lock:
                self._funding_rate_cache[symbol] = {"data": result, "ts": time.monotonic()}
            return result

        except Exception as e:
            logger.error(f"Erro ao obter funding rate de {symbol}: {e}")
            # Fallback: devolve cache stale se existir, senão valores neutros
            with self._cache_lock:
                entry = self._funding_rate_cache.get(symbol)
                if entry:
                    return entry["data"]
            return {
                'rate': 0.0,
                'rate_percent': 0.0,
                'long_pays': False,
                'short_pays': False,
                'next_funding_time': None,
                'favorable_side': 'NEUTRAL'
            }
    
    def get_commission_rates(self, symbol: str = "BTCUSDT") -> Dict:
        """
        Busca as taxas de comissão do usuário diretamente da API da Binance.
        
        Isso retorna as taxas REAIS considerando:
        - Seu nível VIP
        - Se você usa BNB para pagar taxas (desconto de 10%)
        - Promoções ativas
        
        Args:
            symbol: Par para consultar (as taxas podem variar por par)
        
        Returns:
            Dict com 'maker_rate' e 'taker_rate' em formato decimal (ex: 0.0005 = 0.05%)
        """
        try:
            # Endpoint: GET /fapi/v1/commissionRate
            response = self._api_call(
                "futures_commission_rate",
                self.client.futures_commission_rate,
                symbol=symbol
            )
            
            maker_rate = float(response.get('makerCommissionRate', 0.0002))
            taker_rate = float(response.get('takerCommissionRate', 0.0005))
            
            logger.info(f"📊 Taxas da Binance para {symbol}:")
            logger.info(f"   • Maker: {maker_rate * 100:.4f}%")
            logger.info(f"   • Taker: {taker_rate * 100:.4f}%")
            
            return {
                'maker_rate': maker_rate,
                'taker_rate': taker_rate,
                'maker_percent': maker_rate * 100,
                'taker_percent': taker_rate * 100
            }
            
        except Exception as e:
            logger.warning(f"⚠️ Erro ao obter taxas da API: {e}")
            logger.warning("   Usando taxas padrão: Maker 0.02%, Taker 0.05%")
            # Retorna taxas padrão se a API falhar
            return {
                'maker_rate': 0.0002,
                'taker_rate': 0.0005,
                'maker_percent': 0.02,
                'taker_percent': 0.05
            }
    
    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """
        Define a alavancagem para um par específico.
        IMPORTANTE: Fazer isso ANTES de abrir posições!
        """
        try:
            self._api_call(
                "futures_change_leverage",
                self.client.futures_change_leverage,
                symbol=symbol,
                leverage=leverage,
                allow_timeout_retry=False
            )
            logger.info(f"⚙️  Alavancagem {symbol}: {leverage}x")
            return True
            
        except Exception as e:
            logger.error(f"Erro ao definir alavancagem: {e}")
            return False
    
    def set_hedge_mode(self) -> bool:
        """
        Ativa o Hedge Mode na conta.
        Isso permite ter posições LONG e SHORT simultaneamente.
        ESSENCIAL para a estratégia funcionar!
        """
        try:
            # Verifica o modo atual
            position_mode = self._api_call(
                "futures_get_position_mode",
                self.client.futures_get_position_mode
            )
            
            if position_mode.get('dualSidePosition', False):
                logger.info("✅ Hedge Mode já está ativo")
                return True
            
            # Ativa o Hedge Mode
            self._api_call(
                "futures_change_position_mode",
                self.client.futures_change_position_mode,
                dualSidePosition=True,
                allow_timeout_retry=False
            )
            logger.info("✅ Hedge Mode ATIVADO com sucesso!")
            return True
            
        except Exception as e:
            # Erro comum: já tem posições abertas
            if "No need to change position side" in str(e):
                logger.info("✅ Hedge Mode já está ativo")
                return True
            logger.error(f"Erro ao ativar Hedge Mode: {e}")
            return False
    
    def get_symbol_price(self, symbol: str) -> float:
        """
        Retorna o preço atual de um par.
        """
        try:
            ticker = self._api_call(
                "futures_symbol_ticker",
                self.client.futures_symbol_ticker,
                symbol=symbol
            )
            return float(ticker['price'])
            
        except Exception as e:
            logger.error(f"Erro ao obter preço de {symbol}: {e}")
            return 0.0
    
    def get_symbol_info(self, symbol: str) -> Dict:
        """
        Retorna informações sobre um par (precisão, min qty, etc).
        Importante para formatar corretamente as ordens.
        Usa cache TTL de 6 horas para reduzir chamadas à API (Improvement 6).
        """
        # Verifica cache antes de chamar a API
        now = time.monotonic()
        cached = self._symbol_info_cache.get(symbol)
        if cached and (now - self._symbol_info_cache_ts.get(symbol, 0)) < self._symbol_info_cache_ttl:
            return cached

        try:
            info = self._api_call("futures_exchange_info", self.client.futures_exchange_info)

            for s in info.get('symbols', []):
                if s['symbol'] == symbol:
                    # Encontra os filtros relevantes
                    min_qty = 0.001
                    min_notional = 5.0

                    for f in s.get('filters', []):
                        if f['filterType'] == 'LOT_SIZE':
                            min_qty = float(f['minQty'])
                        elif f['filterType'] == 'MIN_NOTIONAL':
                            min_notional = float(f.get('notional', 5))

                    result = {
                        'symbol': symbol,
                        'pricePrecision': s['pricePrecision'],
                        'quantityPrecision': s['quantityPrecision'],
                        'minQty': min_qty,
                        'minNotional': min_notional,
                    }
                    # Armazena no cache
                    self._symbol_info_cache[symbol] = result
                    self._symbol_info_cache_ts[symbol] = time.monotonic()
                    return result

            return {}

        except Exception as e:
            logger.error(f"Erro ao obter info de {symbol}: {e}")
            return {}

    def get_exchange_info(self) -> Dict:
        """
        Retorna o exchangeInfo de futures.
        Usa cache TTL de 6 horas para reduzir chamadas à API (Improvement 6).
        """
        now = time.monotonic()
        if self._exchange_info_cache and (now - self._exchange_info_cache_ts) < self._symbol_info_cache_ttl:
            return self._exchange_info_cache
        try:
            result = self._api_call("futures_exchange_info", self.client.futures_exchange_info)
            self._exchange_info_cache = result
            self._exchange_info_cache_ts = time.monotonic()
            return result
        except Exception as e:
            logger.error(f"Erro ao obter exchange info: {e}")
            return {}

    def get_ticker_24h(self, symbol: str) -> Dict:
        """
        Retorna ticker de 24h para um símbolo.
        """
        try:
            return self._api_call(
                "futures_ticker",
                self.client.futures_ticker,
                symbol=symbol
            )
        except Exception as e:
            logger.error(f"Erro ao obter ticker 24h de {symbol}: {e}")
            return {}

    def get_all_tickers_24h(self) -> Dict[str, Dict]:
        """
        Retorna tickers de 24h de TODOS os pares em uma única chamada.
        Muito mais eficiente do que chamar get_ticker_24h() por símbolo.

        Returns:
            Dict {symbol: ticker_data} com quoteVolume, lastPrice, etc.
        """
        try:
            tickers = self._api_call(
                "futures_ticker_all",
                self.client.futures_ticker
            )
            if isinstance(tickers, list):
                return {t['symbol']: t for t in tickers if 'symbol' in t}
            if isinstance(tickers, dict):
                return {tickers['symbol']: tickers}
            return {}
        except Exception as e:
            logger.error(f"Erro ao obter todos os tickers 24h: {e}")
            return {}

    def get_all_funding_rates(self) -> Dict[str, float]:
        """
        Retorna o funding rate atual de TODOS os pares em uma única chamada.

        Returns:
            Dict {symbol: funding_rate_percent}
        """
        try:
            marks = self._api_call(
                "futures_mark_price_all",
                self.client.futures_mark_price
            )
            result = {}
            if isinstance(marks, list):
                for item in marks:
                    sym = item.get('symbol', '')
                    rate = item.get('lastFundingRate') or item.get('fundingRate')
                    if sym and rate is not None:
                        result[sym] = float(rate) * 100
            return result
        except Exception as e:
            logger.error(f"Erro ao obter todos os funding rates: {e}")
            return {}

    def get_order_book(self, symbol: str, limit: int = 5) -> Dict:
        """
        Retorna livro de ofertas para um símbolo.
        """
        try:
            return self._api_call(
                "futures_order_book",
                self.client.futures_order_book,
                symbol=symbol,
                limit=limit
            )
        except Exception as e:
            logger.error(f"Erro ao obter order book de {symbol}: {e}")
            return {}

    def get_klines_raw(self, symbol: str, interval: str, limit: int = 50) -> List[List]:
        """
        Retorna klines no formato bruto da API (lista de listas).
        Útil para módulos que já tratam o parsing.
        """
        try:
            return self._api_call(
                "futures_klines_raw",
                self.client.futures_klines,
                symbol=symbol,
                interval=interval,
                limit=limit
            )
        except Exception as e:
            logger.error(f"Erro ao obter klines raw de {symbol}: {e}")
            return []
    
    def _rest_get_klines(self, symbol: str, interval: str, limit: int = 50) -> List[Dict]:
        """
        Busca klines direto via REST API da Binance (sem passar pelo WS).

        Usado como fallback quando WS está indisponível/stale, e como
        seed inicial no WebSocketKlineStore.
        """
        try:
            klines = self._api_call(
                "futures_klines",
                self.client.futures_klines,
                symbol=symbol,
                interval=interval,
                limit=limit,
            )
            return [
                {
                    'timestamp': k[0],
                    'open': float(k[1]),
                    'high': float(k[2]),
                    'low': float(k[3]),
                    'close': float(k[4]),
                    'volume': float(k[5]),
                }
                for k in klines
            ]
        except Exception as e:
            logger.error(f"Erro ao obter klines de {symbol}: {e}")
            return []

    def get_klines(self, symbol: str, interval: str, limit: int = 50) -> List[Dict]:
        """
        Retorna os candles (OHLCV) de um par.

        Tenta WebSocket store primeiro (se habilitado e fresh); cai em REST
        automaticamente em caso contrário. Callers não precisam saber qual path
        foi usado — interface idêntica ao original.
        """
        ws_store = getattr(self, "_ws_store", None)
        miss_reason = None

        if ws_store is None:
            miss_reason = "store_disabled"
        elif not ws_store.is_fresh(symbol, interval):
            miss_reason = "stale"
        else:
            cached = ws_store.get_klines(symbol, interval, limit)
            if cached is not None:
                metrics.record_cache_hit("klines_ws")
                return cached
            miss_reason = "insufficient_buffer"

        # Telemetria: conta miss agregado + sub-motivo específico
        metrics.record_cache_miss("klines_ws")
        metrics.record_cache_miss(f"klines_ws:{miss_reason}")

        try:
            return self._rest_get_klines(symbol, interval, limit)
        except Exception as e:
            logger.error(f"Erro ao obter klines de {symbol}: {e}")
            return []
    
    def get_open_positions(self, force_refresh: bool = False) -> List[Dict]:
        """
        Retorna todas as posições abertas, com cache TTL curto (5s).

        Args:
            force_refresh: se True, ignora cache. Use em paths críticos
                (execução de SL, fechamento, /close_all) pra garantir snapshot fresco.

        Raises:
            Exception: propaga qualquer erro de API em vez de retornar [].
            Distinguir "posições realmente vazias" de "API falhou" é crítico —
            no passado, erro transitório (ex: -1021 timestamp) retornava []
            e fazia o monitor interpretar todas as posições como fechadas
            externamente ("phantom closes"), corrompendo stats, state e
            notificações Telegram.

            Callers que querem lista vazia em erro devem fazer try/except
            explícito e decidir o comportamento seguro pro seu caso.
        """
        now = time.monotonic()
        with self._cache_lock:
            cached = self._positions_cache
            if (not force_refresh) and cached and (now - cached["ts"]) < self._positions_cache_ttl:
                metrics.record_cache_hit("positions")
                return [dict(p) for p in cached["data"]]

        metrics.record_cache_miss("positions")
        positions = self._api_call(
            "futures_position_information",
            self.client.futures_position_information,
        )

        # Filtra apenas posições com quantidade > 0
        open_positions = []
        for p in positions:
            qty = float(p.get('positionAmt', 0))
            if qty != 0:
                open_positions.append({
                    'symbol': p.get('symbol', ''),
                    'side': 'LONG' if qty > 0 else 'SHORT',
                    'quantity': abs(qty),
                    'entry_price': float(p.get('entryPrice', 0)),
                    'mark_price': float(p.get('markPrice', 0)),
                    'unrealized_pnl': float(p.get('unRealizedProfit', 0)),
                    'leverage': int(p.get('leverage', self.config.LEVERAGE)),
                })

        with self._cache_lock:
            self._positions_cache = {"data": [dict(p) for p in open_positions], "ts": now}

        return open_positions

    def invalidate_positions_cache(self) -> None:
        """
        Marca o cache de posições como stale. Próxima leitura força fetch.
        Chame após qualquer ação que mude posições (abertura, fechamento) ou
        quando receber evento ACCOUNT_UPDATE via user stream.
        """
        with self._cache_lock:
            self._positions_cache = None
    
    def place_market_order(
        self, 
        symbol: str, 
        side: str,  # 'BUY' ou 'SELL'
        position_side: str,  # 'LONG' ou 'SHORT' (para Hedge Mode)
        quantity: float
    ) -> Optional[Dict]:
        """
        Coloca uma ordem a mercado.
        
        Para ABRIR posição LONG: side='BUY', position_side='LONG'
        Para FECHAR posição LONG: side='SELL', position_side='LONG'
        Para ABRIR posição SHORT: side='SELL', position_side='SHORT'
        Para FECHAR posição SHORT: side='BUY', position_side='SHORT'
        """
        # Curto-circuito: símbolos em cooldown estrutural não tentam de novo
        # até o cooldown expirar. Evita flood de ordens rejeitadas e alertas
        # de Telegram quando o erro é por limite de leverage/tier da exchange.
        cooldown_info = self.get_symbol_cooldown_info(symbol)
        if cooldown_info is not None:
            logger.info(
                f"⏳ {symbol} em cooldown estrutural — pulando ordem "
                f"({int(cooldown_info['remaining_seconds'])}s restantes, "
                f"code={cooldown_info['code']})"
            )
            self._record_order_stat(symbol, attempts=1, failures=1, rejections=1)
            return None

        self._record_order_stat(symbol, attempts=1)
        try:
            # Obtém a precisão do símbolo
            info = self.get_symbol_info(symbol)
            qty_precision = info.get('quantityPrecision', 3)
            
            # Formata a quantidade
            formatted_qty = round(quantity, qty_precision)
            if formatted_qty <= 0:
                logger.error(f"❌ Quantidade arredondada para zero em {symbol}: qty={quantity}, precision={qty_precision}")
                return None

            # Envia a ordem
            order = self._api_call(
                "futures_create_order_market",
                self.client.futures_create_order,
                symbol=symbol,
                side=side,
                positionSide=position_side,
                type='MARKET',
                quantity=formatted_qty,
                allow_timeout_retry=False
            )
            
            logger.info(f"📈 Ordem executada: {side} {position_side} {formatted_qty} {symbol}")
            self._record_order_stat(symbol, successes=1)
            # Saldo mudou: invalida cache pra próxima leitura buscar valor fresco
            self.invalidate_balance_cache()
            # Posições mudaram: invalida cache para próximo monitor/close ver o estado real
            self.invalidate_positions_cache()
            return order
            
        except Exception as e:
            logger.error(f"Erro ao enviar ordem: {e}")
            api_code = getattr(e, "code", None)
            if api_code in STRUCTURAL_REJECTION_CODES:
                self._set_symbol_cooldown(symbol, api_code, str(e))
            self._record_order_stat(
                symbol,
                failures=1,
                rejections=1 if self._is_order_rejection(e) else 0
            )
            return None
    
    def set_stop_loss_take_profit(
        self,
        symbol: str,
        position_side: str,
        stop_loss_price: float = None,
        take_profit_price: float = None
    ) -> bool:
        """
        Define Stop Loss e Take Profit para uma posição.
        """
        try:
            info = self.get_symbol_info(symbol)
            price_precision = info.get('pricePrecision', 2)
            
            # Stop Loss
            if stop_loss_price:
                sl_price = round(stop_loss_price, price_precision)
                close_side = 'SELL' if position_side == 'LONG' else 'BUY'
                
                self._api_call(
                    "futures_create_order_stop_market",
                    self.client.futures_create_order,
                    symbol=symbol,
                    side=close_side,
                    positionSide=position_side,
                    type='STOP_MARKET',
                    stopPrice=sl_price,
                    closePosition=True,
                    allow_timeout_retry=False
                )
                logger.info(f"🛑 Stop Loss definido: {symbol} @ ${sl_price}")
            
            # Take Profit
            if take_profit_price:
                tp_price = round(take_profit_price, price_precision)
                close_side = 'SELL' if position_side == 'LONG' else 'BUY'
                
                self._api_call(
                    "futures_create_order_take_profit_market",
                    self.client.futures_create_order,
                    symbol=symbol,
                    side=close_side,
                    positionSide=position_side,
                    type='TAKE_PROFIT_MARKET',
                    stopPrice=tp_price,
                    closePosition=True,
                    allow_timeout_retry=False
                )
                logger.info(f"🎯 Take Profit definido: {symbol} @ ${tp_price}")
            
            return True
            
        except Exception as e:
            logger.error(f"Erro ao definir SL/TP: {e}")
            return False
    
    def close_position(self, symbol: str, position_side: str) -> bool:
        """
        Fecha uma posição inteira a mercado.
        """
        try:
            # force_refresh: evitar agir sobre snapshot stale do cache
            positions = self.get_open_positions(force_refresh=True)
            
            for pos in positions:
                if pos['symbol'] == symbol and pos['side'] == position_side:
                    # Define o lado da ordem para fechar
                    close_side = 'SELL' if position_side == 'LONG' else 'BUY'

                    # Envia ordem para fechar — captura retorno pra detectar
                    # falha de envio (place_market_order retorna None quando
                    # a Binance rejeita ou dá -1007 Timeout/Unknown). Sem
                    # essa verificação o caller bookkeeping um close fake
                    # e o loop de monitor tenta fechar a mesma posição em
                    # toda iteração.
                    order = self.place_market_order(
                        symbol=symbol,
                        side=close_side,
                        position_side=position_side,
                        quantity=pos['quantity']
                    )
                    if order is None:
                        logger.error(
                            f"❌ Ordem de fechamento falhou: {position_side} {symbol} "
                            "(place_market_order retornou None — ver erro acima). "
                            "Posição segue aberta na corretora."
                        )
                        return False

                    logger.info(f"✅ Posição fechada: {position_side} {symbol}")
                    return True

            logger.warning(f"Posição não encontrada: {position_side} {symbol}")
            return False
            
        except Exception as e:
            logger.error(f"Erro ao fechar posição: {e}")
            return False

"""
WebSocket kline streams da Binance Futures.

Substitui polling REST de `get_klines` por stream push, drasticamente reduzindo
latência de análise. Mantém buffer em memória por (symbol, interval) atualizado
via callback do `ThreadedWebsocketManager` do python-binance.

Padrão seed + increment:
1. `subscribe(symbol, interval)` faz fetch REST inicial pra preencher buffer
2. Stream WS alimenta incrementalmente velas fechadas (`isClosed=True`)
3. Velas em formação (`isClosed=False`) são ignoradas pra manter comportamento
   atual da estratégia (decisão: só considerar velas fechadas)

O consumidor (`BinanceConnection.get_klines`) deve:
- Checar `is_fresh()` antes de ler — se False, fallback REST automático
- Ler via `get_klines()` — None significa buffer insuficiente ou sem subscribe
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from binance import ThreadedWebsocketManager

logger = logging.getLogger(__name__)

# Buffer máximo por (symbol, interval). Lookback típico da estratégia é 260;
# mantemos margem generosa pra evitar miss quando buffer exatamente iguala o ask.
_MAX_BUFFER_SIZE = 1000


class WebSocketKlineStore:
    """
    Buffer em memória de velas fechadas alimentado via stream WebSocket da Binance.

    Thread-safe. Um único Lock protege todas as operações de buffer e estado.
    Callbacks do TWM rodam em threads próprias; consumidores leem do loop do bot.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        testnet: bool,
        rest_seed_fetcher: Callable[[str, str, int], List[Dict[str, Any]]],
        staleness_seconds: float = 30.0,
        seed_limit: int = 260,
    ):
        """
        Args:
            api_key / api_secret: credenciais (rede ativa)
            testnet: True pra conectar na testnet
            rest_seed_fetcher: callable(symbol, interval, limit) -> list[dict]
                               Usado pra popular buffer inicial. Normalmente
                               `BinanceConnection._rest_get_klines`.
            staleness_seconds: acima disso, is_fresh() retorna False → fallback REST
            seed_limit: quantas velas buscar no seed inicial
        """
        self._api_key = api_key
        self._api_secret = api_secret
        self._testnet = bool(testnet)
        self._rest_seed_fetcher = rest_seed_fetcher
        # Staleness tem piso muito baixo só pra evitar valores nonsense (<0) —
        # callers em produção usam 30s, testes usam valores menores.
        self._staleness_seconds = max(0.01, float(staleness_seconds))
        # Seed limit idem: callers em produção pedem 260; testes podem querer menos.
        self._seed_limit = max(1, int(seed_limit))

        self._twm: Optional[ThreadedWebsocketManager] = None
        self._lock = threading.Lock()
        self._started = False
        self._shutdown = False

        # buffers[(sym, interval)] = list[dict] em ordem cronológica
        self._buffers: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        # socket_ids[(sym, interval)] = id retornado pelo TWM (usado em stop_socket)
        self._socket_ids: Dict[Tuple[str, str], str] = {}
        # last_message_ts[(sym, interval)] = time.monotonic() da última msg/seed
        self._last_message_ts: Dict[Tuple[str, str], float] = {}
        # Contadores por stream, expostos via get_stats pra métricas
        self._message_counts: Dict[Tuple[str, str], int] = {}
        self._reconnect_count = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """Inicia o ThreadedWebsocketManager. Idempotente."""
        with self._lock:
            if self._started:
                return True
            if self._shutdown:
                return False
            try:
                self._twm = ThreadedWebsocketManager(
                    api_key=self._api_key,
                    api_secret=self._api_secret,
                    testnet=self._testnet,
                )
                self._twm.start()
                self._started = True
                logger.info(f"🔌 WebSocketKlineStore iniciado (testnet={self._testnet})")
                return True
            except Exception as exc:
                logger.error(f"❌ Falha ao iniciar WebSocketKlineStore: {exc}")
                return False

    def stop(self) -> None:
        """Para todos os sockets + o manager. Idempotente."""
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            twm = self._twm
            sock_ids = list(self._socket_ids.values())
            self._socket_ids.clear()
            self._buffers.clear()
            self._last_message_ts.clear()
            self._message_counts.clear()
            self._started = False

        if twm is None:
            return

        for sock_id in sock_ids:
            try:
                twm.stop_socket(sock_id)
            except Exception:
                pass
        try:
            twm.stop()
        except Exception as exc:
            logger.warning(f"Erro ao parar TWM: {exc}")
        logger.info("🔌 WebSocketKlineStore parado")

    def get_twm(self) -> Optional[ThreadedWebsocketManager]:
        """Retorna o TWM subjacente (ou None se não iniciado). Usado por outros
        componentes (ex: UserStreamMonitor) que precisam compartilhar o mesmo
        event loop — múltiplos TWMs no mesmo processo colidem em Python 3.13."""
        with self._lock:
            if self._started and not self._shutdown:
                return self._twm
            return None

    # ------------------------------------------------------------------
    # Subscrição
    # ------------------------------------------------------------------

    def subscribe(self, symbol: str, interval: str) -> bool:
        """
        Inscreve num stream kline. Idempotente.

        Faz seed REST bloqueante antes de abrir o socket (evita janela em que
        buffer estaria vazio). Retorna True se inscrito ou já estava inscrito.
        """
        key = (symbol.upper(), interval)

        with self._lock:
            if self._shutdown or not self._started:
                return False
            if key in self._socket_ids:
                return True

        # Seed REST fora do lock pra não bloquear outras operações do store
        try:
            seed = self._rest_seed_fetcher(symbol, interval, self._seed_limit)
        except Exception as exc:
            logger.error(f"❌ Seed REST falhou para {symbol}/{interval}: {exc}")
            return False

        if not seed:
            logger.warning(f"⚠️ Seed REST vazio para {symbol}/{interval} — subscribe abortado")
            return False

        with self._lock:
            # Re-check após fetch (shutdown/outra thread pode ter inscrito)
            if self._shutdown:
                return False
            if key in self._socket_ids:
                return True

            self._buffers[key] = list(seed)[-_MAX_BUFFER_SIZE:]
            self._last_message_ts[key] = time.monotonic()  # seed conta como fresh
            self._message_counts[key] = 0

            # Closure captura key pra roteamento no callback compartilhado
            def _callback(msg: Any, _key: Tuple[str, str] = key) -> None:
                self._on_kline_message(_key, msg)

            twm = self._twm
            if twm is None:
                return False
            try:
                sock_id = twm.start_kline_futures_socket(
                    callback=_callback,
                    symbol=symbol.upper(),
                    interval=interval,
                )
            except Exception as exc:
                logger.error(f"❌ Falha ao abrir socket WS {symbol}/{interval}: {exc}")
                self._buffers.pop(key, None)
                self._last_message_ts.pop(key, None)
                self._message_counts.pop(key, None)
                return False

            self._socket_ids[key] = sock_id
            logger.info(
                f"📡 WS inscrito: {symbol.upper()}@kline_{interval} (seed {len(seed)} velas)"
            )
            return True

    def unsubscribe(self, symbol: str, interval: str) -> None:
        """Remove subscrição + buffer. Silencioso se não existir."""
        key = (symbol.upper(), interval)
        with self._lock:
            sock_id = self._socket_ids.pop(key, None)
            self._buffers.pop(key, None)
            self._last_message_ts.pop(key, None)
            self._message_counts.pop(key, None)
            twm = self._twm

        if sock_id and twm is not None:
            try:
                twm.stop_socket(sock_id)
                logger.info(f"📡 WS desinscrito: {symbol.upper()}@kline_{interval}")
            except Exception as exc:
                logger.warning(f"Erro ao fechar socket {key}: {exc}")

    # ------------------------------------------------------------------
    # Leitura
    # ------------------------------------------------------------------

    def get_klines(
        self, symbol: str, interval: str, limit: int
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Retorna os últimos `limit` klines do buffer ou None se insuficiente.

        Retorna cópia pra evitar que caller modifique buffer interno.
        """
        key = (symbol.upper(), interval)
        lim = max(1, int(limit))
        with self._lock:
            buf = self._buffers.get(key)
            # Buffer precisa ter ao menos o que caller pediu. Menos que isso,
            # o caller deve cair em fallback REST (que pode pegar mais histórico).
            if not buf or len(buf) < lim:
                return None
            return list(buf[-lim:])

    def is_fresh(
        self, symbol: str, interval: str, max_age_seconds: Optional[float] = None
    ) -> bool:
        """
        True se a última mensagem (ou seed) chegou dentro da janela de staleness.
        Callers devem testar isso antes de get_klines e cair em fallback REST se False.
        """
        key = (symbol.upper(), interval)
        max_age = max_age_seconds if max_age_seconds is not None else self._staleness_seconds
        with self._lock:
            ts = self._last_message_ts.get(key)
            if ts is None:
                return False
            return (time.monotonic() - ts) <= max_age

    # ------------------------------------------------------------------
    # Observabilidade
    # ------------------------------------------------------------------

    def subscriptions(self) -> List[Tuple[str, str]]:
        with self._lock:
            return list(self._socket_ids.keys())

    def get_stats(self) -> Dict[str, Any]:
        """Snapshot de métricas pra exportar em Prometheus."""
        now = time.monotonic()
        with self._lock:
            streams = []
            for (sym, interval), sock_id in self._socket_ids.items():
                ts = self._last_message_ts.get((sym, interval), now)
                streams.append({
                    "symbol": sym,
                    "interval": interval,
                    "messages": self._message_counts.get((sym, interval), 0),
                    "age_seconds": max(0.0, now - ts),
                    "buffer_size": len(self._buffers.get((sym, interval), [])),
                    "socket_id": sock_id,
                })
            return {
                "subscriptions": len(self._socket_ids),
                "reconnects": self._reconnect_count,
                "streams": streams,
            }

    # ------------------------------------------------------------------
    # Callback do WS (executado em thread do TWM)
    # ------------------------------------------------------------------

    def _on_kline_message(self, key: Tuple[str, str], msg: Dict[str, Any]) -> None:
        """
        Handler do TWM. Recebe cada mensagem do stream e atualiza buffer.
        Roda em thread própria do WS — usa lock curto pra não bloquear.
        """
        try:
            # Binance pode emitir {"e":"error", "m":"..."} em erros transitórios
            if isinstance(msg, dict) and msg.get("e") == "error":
                logger.warning(f"WS error {key}: {msg.get('m', 'unknown')}")
                return

            kline = msg.get("k") if isinstance(msg, dict) else None
            if not kline:
                return

            is_closed = bool(kline.get("x", False))
            now = time.monotonic()

            with self._lock:
                # Atualiza timestamp e contador mesmo pra velas em formação —
                # é sinal de que o stream está vivo (is_fresh continua True).
                self._last_message_ts[key] = now
                self._message_counts[key] = self._message_counts.get(key, 0) + 1

                # Por decisão de escopo: ignora velas em formação pra preservar
                # comportamento atual da estratégia (que espera velas fechadas).
                if not is_closed:
                    return

                buf = self._buffers.get(key)
                if buf is None:
                    return  # unsubscribe aconteceu entre o enqueue e o callback

                candle = {
                    "timestamp": int(kline.get("t", 0)),
                    "open": float(kline.get("o", 0) or 0),
                    "high": float(kline.get("h", 0) or 0),
                    "low": float(kline.get("l", 0) or 0),
                    "close": float(kline.get("c", 0) or 0),
                    "volume": float(kline.get("v", 0) or 0),
                }

                if candle["timestamp"] <= 0:
                    return

                # Dedupe: se última vela tem mesmo timestamp, substitui
                # (acontece em edge case de vela ser "fechada" múltiplas vezes).
                if buf and buf[-1].get("timestamp") == candle["timestamp"]:
                    buf[-1] = candle
                    return

                # Guarda ordenação: só aceita timestamp estritamente maior
                # (proteção contra mensagens fora de ordem em reconnect).
                if buf and candle["timestamp"] <= buf[-1].get("timestamp", 0):
                    return

                buf.append(candle)
                if len(buf) > _MAX_BUFFER_SIZE:
                    del buf[: len(buf) - _MAX_BUFFER_SIZE]
        except Exception as exc:
            logger.exception(f"Erro no callback WS {key}: {exc}")

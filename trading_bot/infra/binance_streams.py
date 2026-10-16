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

Recuperação: socket morto (`ReadLoopClosed`) ou buraco na sequência de velas
fechadas marcam o stream como "recovering" (is_fresh → False → REST) e uma
thread reseeda o buffer via REST (+ recria o socket) com backoff + jitter.
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from binance import ThreadedWebsocketManager
from binance.exceptions import ReadLoopClosed
from binance.ws.streams import BinanceSocketType

logger = logging.getLogger(__name__)

# Buffer máximo por (symbol, interval). Lookback típico da estratégia é 260;
# mantemos margem generosa pra evitar miss quando buffer exatamente iguala o ask.
_MAX_BUFFER_SIZE = 1000

# Erros que significam "este socket morreu e não volta sozinho": o read loop
# interno do ReconnectingWebsocket encerrou (QueueOverflow, UnableToConnect após
# 5 reconexões, exceção genérica). A única saída é recriar o socket.
_DEAD_SOCKET_ERROR_TYPES = frozenset({"ReadLoopClosed"})

# Backoff da recriação de socket / ressincronização de buffer (segundos).
# delay = min(MAX, BASE * 2**tentativa) * jitter[0.5, 1.0]
_RECOVERY_BACKOFF_BASE_SECONDS = 1.0
_RECOVERY_BACKOFF_MAX_SECONDS = 60.0

# Throttle do log de erro por stream: 1º erro e depois a cada N.
_ERROR_LOG_EVERY = 100

_INTERVAL_UNIT_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}


def _interval_to_ms(interval: str) -> Optional[int]:
    """'5m' → 300000. None pra formatos desconhecidos (ex: '1M' mensal)."""
    try:
        unit_ms = _INTERVAL_UNIT_MS.get(interval[-1])
        if unit_ms is None:
            return None
        return int(interval[:-1]) * unit_ms
    except (ValueError, IndexError, TypeError):
        return None


class ResilientThreadedWebsocketManager(ThreadedWebsocketManager):
    """
    ThreadedWebsocketManager com `start_listener` que não entra em busy-loop.

    Bug do python-binance (1.0.35/1.0.37, `ws/threaded_stream.py:72-93`): quando o
    read loop interno do socket morre, `recv()` passa a levantar `ReadLoopClosed`
    IMEDIATAMENTE, sem nunca ceder o event loop. O `start_listener` original só
    loga e repete o `recv()` — loop apertado que (a) inunda o log (~10k linhas/s,
    13/10/26: >700k linhas em ~70s, 5 rotações de 20MB) e (b) monopoliza o event
    loop compartilhado, congelando TODOS os outros sockets (klines + user stream).
    O loop só terminava quando alguém chamava `stop_socket` daquele path.

    Aqui:
    - `ReadLoopClosed` → entrega UM erro ao callback e encerra o listener (o
      dono do socket — WebSocketKlineStore / UserStreamMonitor — recria).
    - Outros erros de `recv()` → entrega ao callback e dorme um pouco (cede o
      event loop; nunca busy-loop).
    - Falha no connect inicial (`__aenter__`) → vira `ReadLoopClosed` pro
      callback, em vez de "Task exception was never retrieved" + stream mudo.
    - Dono por path (token): reabrir o mesmo path (unsubscribe→subscribe rápido
      no regime-swap) não deixa dois listeners vivos nem gera KeyError no
      `del self._socket_running[path]` do original.
    """

    # Pausa após erro não-terminal em recv(). Atributo de classe pra testes.
    ERROR_PAUSE_SECONDS = 0.5

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # path → token do listener dono daquele path
        self._listener_owner: Dict[str, object] = {}

    def _start_async_socket(
        self,
        callback: Callable,
        socket_name: str,
        params: Dict[str, Any],
        path: Optional[str] = None,
    ) -> str:
        # Mesmo fluxo do original, mas registra o token do dono ANTES de
        # agendar o listener (sem janela de corrida com o listener antigo).
        start_time = time.time()
        while not self._bsm:
            if time.time() - start_time > 5:
                raise RuntimeError(
                    "Binance Socket Manager failed to initialize after 5 seconds"
                )
            time.sleep(0.1)
        socket = getattr(self._bsm, socket_name)(**params)
        socket_path: str = path or socket._path  # noqa
        token = object()
        self._socket_running[socket_path] = True
        self._listener_owner[socket_path] = token
        self._loop.call_soon_threadsafe(
            asyncio.create_task,
            self.start_listener(socket, socket_path, callback, token),
        )
        return socket_path

    @staticmethod
    def _dispatch(callback: Callable, msg: Any) -> None:
        try:
            if asyncio.iscoroutinefunction(callback):
                asyncio.create_task(callback(msg))
            else:
                callback(msg)
        except Exception as exc:
            logger.error(f"Callback WS levantou exceção (ignorada): {exc}")

    def _owns(self, path: str, token: object) -> bool:
        return self._listener_owner.get(path) is token

    async def start_listener(
        self, socket: Any, path: str, callback: Callable, token: Optional[object] = None
    ) -> None:
        if token is None:
            token = object()
            self._listener_owner[path] = token
        error_count = 0
        try:
            async with socket as s:
                while self._socket_running.get(path) and self._owns(path, token):
                    try:
                        msg = await asyncio.wait_for(s.recv(), 3)
                    except asyncio.TimeoutError:
                        continue
                    except ReadLoopClosed as exc:
                        logger.warning(
                            f"⚠️ WS {path}: read loop fechado — listener encerrado "
                            f"(socket precisa ser recriado)"
                        )
                        self._dispatch(
                            callback,
                            {"e": "error", "type": "ReadLoopClosed", "m": f"{exc}"},
                        )
                        break
                    except Exception as exc:
                        error_count += 1
                        if error_count == 1 or error_count % _ERROR_LOG_EVERY == 0:
                            logger.error(
                                f"Erro recebendo mensagem WS {path} "
                                f"(#{error_count}): {exc.__class__.__name__}: {exc}"
                            )
                        self._dispatch(
                            callback,
                            {"e": "error", "type": exc.__class__.__name__, "m": f"{exc}"},
                        )
                        # Cede o event loop: nunca repetir recv() em loop apertado.
                        await asyncio.sleep(self.ERROR_PAUSE_SECONDS)
                        continue
                    error_count = 0
                    if msg:
                        self._dispatch(callback, msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Falha no connect inicial / __aexit__: sem isso a task morre com
            # "Task exception was never retrieved" e o stream fica mudo.
            if self._socket_running.get(path) and self._owns(path, token):
                logger.error(f"❌ Listener WS {path} encerrado por exceção: {exc}")
                self._dispatch(
                    callback,
                    {"e": "error", "type": "ReadLoopClosed", "m": f"listener encerrado: {exc}"},
                )
        finally:
            # Só o dono atual limpa o path — um listener substituído não pode
            # derrubar o flag do socket novo.
            if self._owns(path, token):
                self._listener_owner.pop(path, None)
                self._socket_running.pop(path, None)


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
        kline_stream_url: Optional[str] = None,
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
            kline_stream_url: abre os sockets de kline nesta URL em vez da rede
                              do TWM (sinais da mainnet com execução/user
                              stream no testnet, no mesmo TWM). None = rede do TWM.
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
        self._kline_stream_url = kline_stream_url

        self._twm: Optional[ResilientThreadedWebsocketManager] = None
        self._lock = threading.Lock()
        # Acorda threads de recuperação no stop() (sleep interrompível)
        self._stop_event = threading.Event()
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
        # Streams em recuperação (socket morto ou buffer com buraco): is_fresh()
        # devolve False → get_klines cai no REST até o reseed terminar.
        # key → token da thread de recuperação dona (só ela limpa a entrada).
        self._recovering: Dict[Tuple[str, str], object] = {}
        # Erros consecutivos por stream (throttle de log; zera a cada kline)
        self._error_counts: Dict[Tuple[str, str], int] = {}

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
                self._twm = ResilientThreadedWebsocketManager(
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
            self._stop_event.set()
            twm = self._twm
            sock_ids = list(self._socket_ids.values())
            self._socket_ids.clear()
            self._buffers.clear()
            self._last_message_ts.clear()
            self._message_counts.clear()
            self._recovering.clear()
            self._error_counts.clear()
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

    def get_twm(self) -> Optional[ResilientThreadedWebsocketManager]:
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

            twm = self._twm
            if twm is None:
                return False
            try:
                sock_id = self._open_kline_socket(twm, key)
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

    def _open_kline_socket(self, twm: ResilientThreadedWebsocketManager, key: Tuple[str, str]) -> str:
        symbol, interval = key
        if self._kline_stream_url is None:
            return twm.start_kline_futures_socket(
                callback=self._make_callback(key), symbol=symbol, interval=interval,
            )
        # Mesmo path do kline_futures_socket, mas na URL dada: o _get_futures_socket
        # do python-binance só escolhe a URL pela rede do TWM (testnet aqui).
        return twm._start_async_socket(
            callback=self._make_callback(key),
            socket_name="_get_socket",
            params={
                "path": f"{symbol.lower()}_perpetual@continuousKline_{interval}",
                "stream_url": self._kline_stream_url,
                "prefix": "ws/",
                "socket_type": BinanceSocketType.USD_M_FUTURES,
            },
        )

    def unsubscribe(self, symbol: str, interval: str) -> None:
        """Remove subscrição + buffer. Silencioso se não existir."""
        key = (symbol.upper(), interval)
        with self._lock:
            sock_id = self._socket_ids.pop(key, None)
            self._buffers.pop(key, None)
            self._last_message_ts.pop(key, None)
            self._message_counts.pop(key, None)
            self._error_counts.pop(key, None)
            # Cancela recuperação em andamento (a thread percebe e sai).
            self._recovering.pop(key, None)
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
            if key in self._recovering:
                return False
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
                    "recovering": (sym, interval) in self._recovering,
                })
            return {
                "subscriptions": len(self._socket_ids),
                "reconnects": self._reconnect_count,
                "streams": streams,
            }

    # ------------------------------------------------------------------
    # Callback do WS (executado em thread do TWM)
    # ------------------------------------------------------------------

    def _make_callback(self, key: Tuple[str, str]) -> Callable[[Any], None]:
        """Closure que captura key pra roteamento no callback compartilhado."""
        def _callback(msg: Any, _key: Tuple[str, str] = key) -> None:
            self._on_kline_message(_key, msg)
        return _callback

    def _handle_error_message(self, key: Tuple[str, str], msg: Dict[str, Any]) -> None:
        """Loga com throttle e, se o socket morreu, agenda recriação."""
        err_type = str(msg.get("type", "") or "")
        with self._lock:
            count = self._error_counts.get(key, 0) + 1
            self._error_counts[key] = count
        if count == 1 or count % _ERROR_LOG_EVERY == 0:
            logger.warning(
                f"WS error {key} (#{count}): {err_type or 'erro'}: {msg.get('m', 'unknown')}"
            )
        if err_type in _DEAD_SOCKET_ERROR_TYPES:
            self._schedule_recovery(key, reopen_socket=True, reason=err_type)

    # ------------------------------------------------------------------
    # Recuperação (socket morto / buffer furado)
    # ------------------------------------------------------------------

    def _schedule_recovery(self, key: Tuple[str, str], reopen_socket: bool, reason: str) -> None:
        """
        Marca o stream como em recuperação (is_fresh → False → REST) e dispara
        thread que reseeda o buffer via REST (+ recria o socket se
        `reopen_socket`) com backoff exponencial + jitter. Idempotente por key.
        """
        with self._lock:
            if self._shutdown or key not in self._socket_ids or key in self._recovering:
                return
            token = object()
            self._recovering[key] = token
        thread = threading.Thread(
            target=self._recovery_loop,
            args=(key, token, reopen_socket, reason),
            name=f"WSKlineRecovery-{key[0]}-{key[1]}",
            daemon=True,
        )
        thread.start()

    def _recovery_delay(self, attempt: int) -> float:
        base = min(_RECOVERY_BACKOFF_MAX_SECONDS, _RECOVERY_BACKOFF_BASE_SECONDS * (2 ** attempt))
        return base * (0.5 + random.random() / 2)

    def _recovery_active(self, key: Tuple[str, str], token: object) -> bool:
        with self._lock:
            return not self._shutdown and self._recovering.get(key) is token

    def _recovery_loop(
        self, key: Tuple[str, str], token: object, reopen_socket: bool, reason: str
    ) -> None:
        symbol, interval = key
        action = "recriando socket" if reopen_socket else "ressincronizando buffer"
        attempt = 0
        try:
            while self._recovery_active(key, token):
                delay = self._recovery_delay(attempt)
                attempt += 1
                logger.warning(
                    f"🔄 WS {symbol}@kline_{interval}: {reason} — {action} em "
                    f"{delay:.1f}s (tentativa #{attempt}; klines via REST até lá)"
                )
                # wait() retorna cedo se stop() for chamado
                self._stop_event.wait(delay)
                if not self._recovery_active(key, token):
                    return
                if self._try_recover_once(key, token, reopen_socket):
                    logger.info(f"✅ WS {symbol}@kline_{interval}: recuperado ({action})")
                    return
        finally:
            with self._lock:
                if self._recovering.get(key) is token:
                    del self._recovering[key]

    def _try_recover_once(self, key: Tuple[str, str], token: object, reopen_socket: bool) -> bool:
        symbol, interval = key
        try:
            seed = self._rest_seed_fetcher(symbol, interval, self._seed_limit)
        except Exception as exc:
            logger.error(f"❌ Reseed REST falhou para {symbol}/{interval}: {exc}")
            return False
        if not seed:
            return False

        with self._lock:
            # Unsubscribe/stop durante o fetch: nada a recuperar.
            if self._shutdown or self._recovering.get(key) is not token:
                return True
            if reopen_socket:
                twm = self._twm
                if twm is None:
                    return False
                old_sock = self._socket_ids.get(key)
                if old_sock:
                    try:
                        twm.stop_socket(old_sock)
                    except Exception as exc:
                        logger.debug(f"stop_socket falhou (ignorado): {exc}")
                try:
                    new_sock = self._open_kline_socket(twm, key)
                except Exception as exc:
                    logger.error(f"❌ Falha ao recriar socket WS {symbol}/{interval}: {exc}")
                    return False
                self._socket_ids[key] = new_sock
                self._reconnect_count += 1
            self._buffers[key] = list(seed)[-_MAX_BUFFER_SIZE:]
            self._last_message_ts[key] = time.monotonic()
            self._error_counts.pop(key, None)
        return True

    def _on_kline_message(self, key: Tuple[str, str], msg: Dict[str, Any]) -> None:
        """
        Handler do TWM. Recebe cada mensagem do stream e atualiza buffer.
        Roda em thread própria do WS — usa lock curto pra não bloquear.
        """
        try:
            # Binance/TWM emitem {"e":"error", "type":..., "m":"..."} no callback
            if isinstance(msg, dict) and msg.get("e") == "error":
                self._handle_error_message(key, msg)
                return

            kline = msg.get("k") if isinstance(msg, dict) else None
            if not kline:
                return

            is_closed = bool(kline.get("x", False))
            now = time.monotonic()
            gap_detected = False

            with self._lock:
                # Atualiza timestamp e contador mesmo pra velas em formação —
                # é sinal de que o stream está vivo (is_fresh continua True).
                self._last_message_ts[key] = now
                self._message_counts[key] = self._message_counts.get(key, 0) + 1
                self._error_counts.pop(key, None)

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

                # Buraco: vela fechada pulou ≥1 período (socket ficou mudo/
                # reconectou e perdeu velas). Indicadores sobre série furada
                # são lixo → marca pra reseed REST (fallback REST até lá).
                step_ms = _interval_to_ms(key[1])
                if buf and step_ms and candle["timestamp"] - buf[-1]["timestamp"] > step_ms:
                    gap_detected = True

                buf.append(candle)
                if len(buf) > _MAX_BUFFER_SIZE:
                    del buf[: len(buf) - _MAX_BUFFER_SIZE]

            if gap_detected:
                self._schedule_recovery(key, reopen_socket=False, reason="buraco nas velas")
        except Exception as exc:
            logger.exception(f"Erro no callback WS {key}: {exc}")

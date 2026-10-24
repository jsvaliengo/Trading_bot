"""
Recuperação de socket WS morto (ReadLoopClosed) — incidente 13/10/26.

O read loop interno do socket SUIUSDT@kline_5m morreu e o `start_listener` do
python-binance passou a repetir `recv()` → `ReadLoopClosed` em loop apertado:
>700k linhas de log em ~70s (5 rotações de 20MB) e o event loop compartilhado
congelado pra todos os outros sockets, até o regime-swap desinscrever o par.
"""

import asyncio
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from binance import ThreadedWebsocketManager
from binance.exceptions import ReadLoopClosed

import trading_bot.infra.binance_streams as streams
from trading_bot.infra.binance_streams import (
    ResilientThreadedWebsocketManager,
    _interval_to_ms,
)

_PATH = "suiusdt_perpetual@continuousKline_5m"


class _FakeSocket:
    """Socket fake: `recv` segue um roteiro de ações (exceção ou mensagem)."""

    def __init__(self, script=None, default=None, enter_exc=None):
        self._script = list(script or [])
        self._default = default
        self._enter_exc = enter_exc
        self.recv_calls = 0
        self.exited = False

    async def __aenter__(self):
        if self._enter_exc is not None:
            raise self._enter_exc
        return self

    async def __aexit__(self, *exc):
        self.exited = True

    async def recv(self):
        self.recv_calls += 1
        item = self._script.pop(0) if self._script else self._default
        if isinstance(item, BaseException):
            raise item
        if item is None:
            await asyncio.sleep(3600)  # silêncio
        return item


def _read_loop_closed():
    return ReadLoopClosed(
        "Read loop has been closed, please reset the websocket connection and "
        "listen to the message error."
    )


async def _run_with_heartbeat(coro, timeout=2.0):
    """Roda `coro` com uma task paralela contando ticks — mede se o event loop é cedido."""
    ticks = [0]

    async def heartbeat():
        while True:
            await asyncio.sleep(0)
            ticks[0] += 1

    hb = asyncio.create_task(heartbeat())
    try:
        await asyncio.wait_for(coro, timeout)
    finally:
        hb.cancel()
    return ticks[0]


def _new_twm(cls=ResilientThreadedWebsocketManager):
    return cls(api_key="k", api_secret="s", testnet=True)


# ---------------------------------------------------------------------------
# Repro do bug no listener original do python-binance
# ---------------------------------------------------------------------------

def test_original_listener_busy_loops_and_starves_event_loop():
    """Documenta o bug upstream: se este teste quebrar, o python-binance mudou
    o start_listener e o override do ResilientThreadedWebsocketManager pode ser
    revisto."""
    twm = _new_twm(ThreadedWebsocketManager)
    sock = _FakeSocket(default=_read_loop_closed())
    calls = []

    def cb(msg):
        calls.append(msg)
        if len(calls) >= 2000:  # simula o stop_socket do regime-swap
            twm._socket_running[_PATH] = False

    twm._socket_running[_PATH] = True
    ticks = asyncio.run(_run_with_heartbeat(twm.start_listener(sock, _PATH, cb)))

    assert len(calls) == 2000  # 2000 erros idênticos repassados ao callback
    assert all(m.get("e") == "error" for m in calls)
    if sys.version_info >= (3, 12):
        # 3.12+ (VM de produção): wait_for não cria task → recv() que levanta
        # na hora nunca cede o loop. Nenhuma outra task roda: event loop
        # monopolizado. (No 3.11 do CI o wait_for cede, mas o spam é o mesmo.)
        assert ticks == 0


# ---------------------------------------------------------------------------
# ResilientThreadedWebsocketManager.start_listener
# ---------------------------------------------------------------------------

def test_resilient_listener_exits_once_on_read_loop_closed():
    twm = _new_twm()
    sock = _FakeSocket(default=_read_loop_closed())
    calls = []
    token = object()
    twm._socket_running[_PATH] = True
    twm._listener_owner[_PATH] = token

    asyncio.run(_run_with_heartbeat(twm.start_listener(sock, _PATH, calls.append, token)))

    assert sock.recv_calls == 1
    assert calls == [{"e": "error", "type": "ReadLoopClosed", "m": str(_read_loop_closed())}]
    assert sock.exited is True  # __aexit__ roda → BSM descarta o socket morto
    assert _PATH not in twm._socket_running
    assert _PATH not in twm._listener_owner


def test_resilient_listener_yields_and_keeps_going_on_transient_error():
    twm = _new_twm()
    twm.ERROR_PAUSE_SECONDS = 0.001
    kline = {"e": "kline", "k": {"t": 1}}
    sock = _FakeSocket(
        script=[RuntimeError("boom"), RuntimeError("boom"), kline, _read_loop_closed()]
    )
    calls = []
    twm._socket_running[_PATH] = True

    ticks = asyncio.run(_run_with_heartbeat(twm.start_listener(sock, _PATH, calls.append)))

    types = [m.get("type") if m.get("e") == "error" else "msg" for m in calls]
    assert types == ["RuntimeError", "RuntimeError", "msg", "ReadLoopClosed"]
    assert ticks > 0  # cedeu o event loop entre os erros


def test_resilient_listener_reports_connect_failure_instead_of_dying_silently():
    twm = _new_twm()
    sock = _FakeSocket(enter_exc=OSError("timed out during opening handshake"))
    calls = []
    twm._socket_running[_PATH] = True

    asyncio.run(twm.start_listener(sock, _PATH, calls.append))

    assert len(calls) == 1
    assert calls[0]["type"] == "ReadLoopClosed"
    assert "opening handshake" in calls[0]["m"]
    assert _PATH not in twm._socket_running


def test_superseded_listener_exits_without_clearing_new_owner():
    """unsubscribe→subscribe rápido no mesmo path: o listener antigo sai sem
    derrubar o flag do novo (no original: 2 listeners vivos + KeyError)."""
    twm = _new_twm()
    sock = _FakeSocket(default=None)
    old_token, new_token = object(), object()
    twm._socket_running[_PATH] = True
    twm._listener_owner[_PATH] = new_token  # socket novo já registrado

    asyncio.run(asyncio.wait_for(twm.start_listener(sock, _PATH, lambda m: None, old_token), 1))

    assert twm._socket_running[_PATH] is True
    assert twm._listener_owner[_PATH] is new_token


def test_stop_socket_ends_listener_and_cleans_path():
    twm = _new_twm()
    sock = _FakeSocket(default=None)
    twm._socket_running[_PATH] = True

    async def scenario():
        task = asyncio.create_task(twm.start_listener(sock, _PATH, lambda m: None))
        await asyncio.sleep(0.01)
        twm.stop_socket(_PATH)
        # recv bloqueado é interrompido pelo wait_for(…, 3) do listener
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
    assert _PATH not in twm._socket_running


# ---------------------------------------------------------------------------
# WebSocketKlineStore: recuperação
# ---------------------------------------------------------------------------

def _seed_fetcher(symbol, interval, limit):
    step = _interval_to_ms(interval)
    base_ts = (int(time.time() * 1000) // step - limit) * step  # última vela recém-fechada
    return [
        {"timestamp": base_ts + i * step, "open": 1.0, "high": 1.0,
         "low": 1.0, "close": 1.0, "volume": 1.0}
        for i in range(limit)
    ]


def _closed_kline(ts):
    return {"e": "kline", "k": {"t": ts, "o": "2", "h": "2", "l": "2", "c": "2",
                                "v": "1", "x": True}}


@pytest.fixture
def fast_backoff(monkeypatch):
    monkeypatch.setattr(streams, "_RECOVERY_BACKOFF_BASE_SECONDS", 0.001)
    monkeypatch.setattr(streams, "_RECOVERY_BACKOFF_MAX_SECONDS", 0.005)


def _make_store(seed_fetcher=_seed_fetcher):
    return streams.WebSocketKlineStore(
        api_key="k", api_secret="s", testnet=True,
        rest_seed_fetcher=seed_fetcher, staleness_seconds=30.0, seed_limit=20,
    )


def _started_store(seed_fetcher=_seed_fetcher):
    store = _make_store(seed_fetcher)
    patcher = patch("trading_bot.infra.binance_streams.ResilientThreadedWebsocketManager")
    twm_cls = patcher.start()
    twm = MagicMock()
    twm.start_kline_futures_socket = MagicMock(return_value=_PATH)
    twm_cls.return_value = twm
    store.start()
    assert store.subscribe("SUIUSDT", "5m") is True
    return store, twm, patcher


def _wait_recovered(store, key=("SUIUSDT", "5m"), timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with store._lock:
            if key not in store._recovering:
                return True
        time.sleep(0.005)
    return False


def test_read_loop_closed_recreates_socket_and_reseeds(fast_backoff):
    store, twm, patcher = _started_store()
    try:
        key = ("SUIUSDT", "5m")
        store._on_kline_message(key, {"e": "error", "type": "ReadLoopClosed", "m": "x"})

        assert _wait_recovered(store)
        twm.stop_socket.assert_called_once_with(_PATH)
        assert twm.start_kline_futures_socket.call_count == 2  # subscribe + recriação
        assert store.is_fresh("SUIUSDT", "5m") is True
        assert store.get_stats()["reconnects"] == 1

        # Callback novo continua roteando pro buffer
        new_cb = twm.start_kline_futures_socket.call_args.kwargs["callback"]
        last_ts = store.get_klines("SUIUSDT", "5m", 1)[-1]["timestamp"]
        new_cb(_closed_kline(last_ts + 300_000))
        assert store.get_klines("SUIUSDT", "5m", 1)[-1]["timestamp"] == last_ts + 300_000
    finally:
        store.stop()
        patcher.stop()


def test_stream_is_stale_while_recovering_and_single_recovery_per_stream(fast_backoff):
    gate = threading.Event()
    seed_calls = []

    def slow_seed(symbol, interval, limit):
        seed_calls.append(1)
        if len(seed_calls) > 1:  # 1ª chamada é o subscribe
            gate.wait(2)
        return _seed_fetcher(symbol, interval, limit)

    store, twm, patcher = _started_store(slow_seed)
    try:
        key = ("SUIUSDT", "5m")
        for _ in range(500):  # rajada de erros (o que o log mostrou)
            store._on_kline_message(key, {"e": "error", "type": "ReadLoopClosed", "m": "x"})

        # Enquanto recupera: is_fresh False → BinanceConnection.get_klines usa REST
        assert store.is_fresh("SUIUSDT", "5m") is False
        assert store.get_stats()["streams"][0]["recovering"] is True

        gate.set()
        assert _wait_recovered(store)
        assert len(seed_calls) == 2  # subscribe + UMA recuperação
        assert twm.start_kline_futures_socket.call_count == 2
    finally:
        gate.set()
        store.stop()
        patcher.stop()


def test_recovery_retries_with_backoff_when_reseed_fails(fast_backoff):
    seed_calls = []

    def flaky_seed(symbol, interval, limit):
        seed_calls.append(1)
        if 2 <= len(seed_calls) <= 3:  # 2 falhas na recuperação
            raise ConnectionError("rede caiu")
        return _seed_fetcher(symbol, interval, limit)

    store, twm, patcher = _started_store(flaky_seed)
    try:
        store._on_kline_message(("SUIUSDT", "5m"), {"e": "error", "type": "ReadLoopClosed"})
        assert _wait_recovered(store)
        assert len(seed_calls) == 4
        assert twm.start_kline_futures_socket.call_count == 2
        assert store.is_fresh("SUIUSDT", "5m") is True
    finally:
        store.stop()
        patcher.stop()


def test_unsubscribe_cancels_pending_recovery(monkeypatch):
    monkeypatch.setattr(streams, "_RECOVERY_BACKOFF_BASE_SECONDS", 30.0)
    store, twm, patcher = _started_store()
    try:
        store._on_kline_message(("SUIUSDT", "5m"), {"e": "error", "type": "ReadLoopClosed"})
        store.unsubscribe("SUIUSDT", "5m")
        store.stop()  # acorda a thread (stop_event) — não pode recriar o socket
        time.sleep(0.05)
        assert twm.start_kline_futures_socket.call_count == 1
    finally:
        store.stop()
        patcher.stop()


def test_transient_error_logs_are_throttled_and_do_not_trigger_recovery(caplog):
    store, twm, patcher = _started_store()
    try:
        key = ("SUIUSDT", "5m")
        with caplog.at_level("WARNING", logger="trading_bot.infra.binance_streams"):
            for _ in range(250):
                store._on_kline_message(
                    key, {"e": "error", "type": "BinanceWebsocketClosed", "m": "x"}
                )
        ws_errors = [r for r in caplog.records if "WS error" in r.getMessage()]
        assert len(ws_errors) == 3  # #1, #100, #200
        assert store.is_fresh("SUIUSDT", "5m") is True
        assert twm.start_kline_futures_socket.call_count == 1
    finally:
        store.stop()
        patcher.stop()


def test_gap_in_closed_candles_triggers_reseed_without_reopening_socket(fast_backoff):
    store, twm, patcher = _started_store()
    try:
        key = ("SUIUSDT", "5m")
        last_ts = store.get_klines("SUIUSDT", "5m", 1)[-1]["timestamp"]
        # Pulou 2 velas (socket ficou mudo e voltou)
        store._on_kline_message(key, _closed_kline(last_ts + 3 * 300_000))

        assert _wait_recovered(store)
        twm.stop_socket.assert_not_called()
        assert twm.start_kline_futures_socket.call_count == 1
        # Buffer substituído pelo reseed REST (contíguo)
        data = store.get_klines("SUIUSDT", "5m", 20)
        diffs = {b["timestamp"] - a["timestamp"] for a, b in zip(data, data[1:])}
        assert diffs == {300_000}
    finally:
        store.stop()
        patcher.stop()


def test_contiguous_candle_does_not_trigger_recovery():
    store, twm, patcher = _started_store()
    try:
        last_ts = store.get_klines("SUIUSDT", "5m", 1)[-1]["timestamp"]
        store._on_kline_message(("SUIUSDT", "5m"), _closed_kline(last_ts + 300_000))
        with store._lock:
            assert not store._recovering
    finally:
        store.stop()
        patcher.stop()


@pytest.mark.parametrize("interval,expected", [
    ("1m", 60_000), ("3m", 180_000), ("5m", 300_000), ("1h", 3_600_000),
    ("4h", 14_400_000), ("1d", 86_400_000), ("1M", None), ("", None), ("xm", None),
])
def test_interval_to_ms(interval, expected):
    assert _interval_to_ms(interval) == expected

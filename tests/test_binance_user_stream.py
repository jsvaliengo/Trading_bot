"""Testes do UserStreamMonitor (roteamento de eventos para invalidação de cache)."""

import pytest
from unittest.mock import MagicMock

from trading_bot.infra.binance_user_stream import UserStreamMonitor


def _make_monitor(on_account=None, on_order=None, twm=None):
    """Cria um monitor sem TWM real (ou com mock)."""
    monitor = UserStreamMonitor.__new__(UserStreamMonitor)
    monitor._on_account_update = on_account
    monitor._on_order_update = on_order
    import threading
    monitor._twm = twm
    monitor._socket_id = None
    monitor._lock = threading.Lock()
    monitor._started = False
    monitor._shutdown = False
    monitor._message_count = 0
    monitor._account_update_count = 0
    monitor._order_update_count = 0
    monitor._last_message_ts = 0.0
    monitor._error_count = 0
    monitor._terminal_error_count = 0
    monitor._restart_in_progress = False
    monitor._restart_attempts = 0
    monitor._restart_success_count = 0
    monitor._restart_failure_count = 0
    monitor._last_restart_ts = 0.0
    return monitor


def test_account_update_dispatches_to_handler():
    on_account = MagicMock()
    on_order = MagicMock()
    m = _make_monitor(on_account=on_account, on_order=on_order)

    msg = {"e": "ACCOUNT_UPDATE", "a": {"B": [], "P": []}}
    m._on_message(msg)

    on_account.assert_called_once_with(msg)
    on_order.assert_not_called()
    assert m._message_count == 1
    assert m._account_update_count == 1


def test_order_update_dispatches_to_handler():
    on_account = MagicMock()
    on_order = MagicMock()
    m = _make_monitor(on_account=on_account, on_order=on_order)

    msg = {"e": "ORDER_TRADE_UPDATE", "o": {"X": "FILLED", "s": "BTCUSDT"}}
    m._on_message(msg)

    on_order.assert_called_once_with(msg)
    on_account.assert_not_called()
    assert m._order_update_count == 1


def test_listen_key_expired_does_not_dispatch():
    on_account = MagicMock()
    on_order = MagicMock()
    m = _make_monitor(on_account=on_account, on_order=on_order)

    m._on_message({"e": "listenKeyExpired"})

    on_account.assert_not_called()
    on_order.assert_not_called()
    assert m._message_count == 1


def test_error_message_does_not_dispatch_and_counts():
    m = _make_monitor(on_account=MagicMock(), on_order=MagicMock())
    m._on_message({"e": "error", "m": "connection reset"})
    assert m._error_count == 1
    assert m._message_count == 0


def test_terminal_error_triggers_restart_thread_once(monkeypatch):
    """ReadLoopClosed deve agendar restart em thread separada, sem reentrância."""
    import threading as _threading

    import trading_bot.infra.binance_user_stream as mod

    # Bloqueia o restart até o loop de erros terminar, evitando race onde o
    # restart bem-sucedido resetaria _terminal_error_count antes do assert.
    release = _threading.Event()

    def blocking_start(*args, **kwargs):
        release.wait(timeout=5.0)
        return "new-sock"

    twm = MagicMock()
    twm.start_futures_user_socket.side_effect = blocking_start
    m = _make_monitor(twm=twm)
    monkeypatch.setattr(mod, "_RESTART_BACKOFF_SECONDS", (0,))

    threads_started = []
    real_start = _threading.Thread.start

    def track_start(self):
        threads_started.append(self)
        real_start(self)

    monkeypatch.setattr("threading.Thread.start", track_start)

    # Simula tight loop de erros — só 1 thread deve ser disparada.
    for _ in range(50):
        m._on_message({"e": "error", "type": "ReadLoopClosed", "m": "x"})

    assert m._terminal_error_count == 50
    assert len(threads_started) == 1
    # Agora libera o restart e espera concluir.
    release.set()
    threads_started[0].join(timeout=2.0)
    assert m._restart_success_count == 1
    assert m._socket_id == "new-sock"
    assert m._restart_in_progress is False


def test_terminal_error_log_throttled(monkeypatch, caplog):
    import logging as _logging
    m = _make_monitor(twm=None)  # twm=None força failure -> não logamos sucesso
    # Não deixa o restart loop começar (sem twm não tem como restartar)
    # mas o handler ainda deve throttlar o log.
    with caplog.at_level(_logging.WARNING, logger="trading_bot.infra.binance_user_stream"):
        for _ in range(2500):
            m._on_message({"e": "error", "type": "ReadLoopClosed", "m": "x"})
    # Esperado: log na 1ª, 1000ª e 2000ª (3 mensagens "UserStream ReadLoopClosed").
    msgs = [r.getMessage() for r in caplog.records if "ReadLoopClosed" in r.getMessage()]
    assert len(msgs) == 3


def test_non_terminal_error_does_not_trigger_restart():
    twm = MagicMock()
    m = _make_monitor(twm=twm)
    m._on_message({"e": "error", "type": "SomeOtherError", "m": "x"})
    assert m._terminal_error_count == 0
    assert m._restart_in_progress is False
    twm.start_futures_user_socket.assert_not_called()


def test_restart_retries_until_success(monkeypatch):
    import trading_bot.infra.binance_user_stream as mod

    twm = MagicMock()
    # 1ª chamada falha, 2ª retorna socket válido.
    twm.start_futures_user_socket.side_effect = [Exception("nope"), "ok-sock"]
    m = _make_monitor(twm=twm)
    monkeypatch.setattr(mod, "_RESTART_BACKOFF_SECONDS", (0,))

    m._on_message({"e": "error", "type": "ReadLoopClosed", "m": "x"})

    # Espera a thread de restart concluir (join é determinístico, sem poll/sleep).
    m._restart_thread.join(timeout=2.0)
    assert not m._restart_thread.is_alive()
    assert m._restart_success_count == 1
    assert m._restart_failure_count == 1
    assert m._socket_id == "ok-sock"


def test_restart_aborts_on_stop(monkeypatch):
    """Ao chamar stop() durante restart, o loop deve sair sem reabrir."""
    import trading_bot.infra.binance_user_stream as mod

    twm = MagicMock()
    twm.start_futures_user_socket.side_effect = Exception("always fails")
    m = _make_monitor(twm=twm)
    monkeypatch.setattr(mod, "_RESTART_BACKOFF_SECONDS", (0,))

    m._on_message({"e": "error", "type": "ReadLoopClosed", "m": "x"})
    # Marca shutdown imediatamente — o loop deve respeitar.
    m._shutdown = True

    # Join determinístico: o loop deve sair sozinho ao ver _shutdown.
    m._restart_thread.join(timeout=2.0)
    assert not m._restart_thread.is_alive()
    assert m._restart_in_progress is False
    assert m._restart_success_count == 0


def test_get_stats_includes_restart_telemetry():
    m = _make_monitor()
    m._terminal_error_count = 7
    m._restart_success_count = 2
    m._restart_failure_count = 3
    m._restart_attempts = 4
    stats = m.get_stats()
    assert stats["terminal_error_count"] == 7
    assert stats["restart_success_count"] == 2
    assert stats["restart_failure_count"] == 3
    assert stats["restart_attempts"] == 4
    assert stats["restart_in_progress"] is False


def test_callback_exception_does_not_propagate():
    """Exceção no handler não pode matar a thread do TWM."""
    def bad_handler(_msg):
        raise RuntimeError("boom")

    m = _make_monitor(on_account=bad_handler)
    # Não deve lançar
    m._on_message({"e": "ACCOUNT_UPDATE"})


def test_non_dict_message_ignored():
    on_account = MagicMock()
    m = _make_monitor(on_account=on_account)
    m._on_message("string message")
    m._on_message(None)
    m._on_message([1, 2, 3])
    on_account.assert_not_called()


def test_get_stats_snapshot():
    m = _make_monitor()
    m._started = True
    m._message_count = 5
    m._account_update_count = 3
    m._order_update_count = 2

    stats = m.get_stats()
    assert stats["started"] is True
    assert stats["message_count"] == 5
    assert stats["account_update_count"] == 3
    assert stats["order_update_count"] == 2
    assert stats["error_count"] == 0


def test_binance_connection_invalidates_on_account_update():
    """Teste de integração: ACCOUNT_UPDATE invalida caches na BinanceConnection."""
    from trading_bot.infra.binance_client import BinanceConnection
    import threading

    conn = BinanceConnection.__new__(BinanceConnection)
    conn._cache_lock = threading.Lock()
    conn._positions_cache = {"data": [{"symbol": "BTCUSDT"}], "ts": 100.0}
    conn._balance_cache = {"wallet": 1000.0, "available": 800.0, "ts": 100.0}
    conn._daily_pnl_cache = {"data": {"total": 5.0}, "ts": 100.0}

    conn._on_user_account_update({"e": "ACCOUNT_UPDATE"})

    assert conn._positions_cache is None
    assert conn._balance_cache is None
    assert conn._daily_pnl_cache is None


def test_binance_connection_ignores_non_filled_orders():
    from trading_bot.infra.binance_client import BinanceConnection
    import threading

    conn = BinanceConnection.__new__(BinanceConnection)
    conn._cache_lock = threading.Lock()
    conn._positions_cache = {"data": [], "ts": 100.0}
    conn._balance_cache = {"wallet": 1000.0, "available": 800.0, "ts": 100.0}

    conn._on_user_order_update({"o": {"X": "NEW"}})
    assert conn._positions_cache is not None  # não invalidou

    conn._on_user_order_update({"o": {"X": "FILLED"}})
    assert conn._positions_cache is None  # invalidou


def _conn_with_realized():
    from trading_bot.infra.binance_client import BinanceConnection
    import threading
    conn = BinanceConnection.__new__(BinanceConnection)
    conn._cache_lock = threading.Lock()
    conn._positions_cache = {"data": [], "ts": 100.0}
    conn._balance_cache = {"wallet": 1000.0, "available": 800.0, "ts": 100.0}
    conn._realized_lock = threading.Lock()
    conn._realized_close_buffer = {}
    return conn


def test_realized_close_buffer_accumulates_and_pops():
    """#183: fills parciais somam o gross e o exit vira VWAP; pop consome."""
    conn = _conn_with_realized()
    # dois fills parciais fechando um LONG de XRP
    conn._record_realized_fill("XRPUSDT", "LONG", rp=-0.20, price=1.150, qty=5.0)
    conn._record_realized_fill("XRPUSDT", "LONG", rp=-0.10, price=1.140, qty=5.0)
    out = conn.pop_realized_close("XRPUSDT", "LONG")
    assert out["gross"] == pytest.approx(-0.30)
    assert out["qty"] == pytest.approx(10.0)
    assert out["exit_price"] == pytest.approx((1.150 * 5 + 1.140 * 5) / 10)  # VWAP = 1.145
    # consumido: 2ª chamada retorna None
    assert conn.pop_realized_close("XRPUSDT", "LONG") is None


def test_on_user_order_update_captures_realized_on_close_fill():
    """ORDER_TRADE_UPDATE com x=TRADE e rp!=0 (SELL fecha LONG) entra no buffer."""
    conn = _conn_with_realized()
    conn._on_user_order_update({"o": {
        "s": "XRPUSDT", "S": "SELL", "x": "TRADE", "X": "FILLED",
        "rp": "-0.30", "L": "1.1450", "l": "10",
    }})
    out = conn.pop_realized_close("XRPUSDT", "LONG")
    assert out is not None
    assert out["gross"] == pytest.approx(-0.30)
    assert out["exit_price"] == pytest.approx(1.1450)


def test_on_user_order_update_ignores_non_realizing_fill():
    """Fill de ABERTURA (rp=0) não entra no buffer de fechamento."""
    conn = _conn_with_realized()
    conn._on_user_order_update({"o": {
        "s": "XRPUSDT", "S": "BUY", "x": "TRADE", "X": "FILLED",
        "rp": "0", "L": "1.1492", "l": "10",
    }})
    assert conn.pop_realized_close("XRPUSDT", "SHORT") is None
    assert conn.pop_realized_close("XRPUSDT", "LONG") is None

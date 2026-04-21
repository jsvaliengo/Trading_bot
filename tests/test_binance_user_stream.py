"""Testes do UserStreamMonitor (roteamento de eventos para invalidação de cache)."""

from unittest.mock import MagicMock

from trading_bot.infra.binance_user_stream import UserStreamMonitor


def _make_monitor(on_account=None, on_order=None):
    """Cria um monitor sem TWM real."""
    monitor = UserStreamMonitor.__new__(UserStreamMonitor)
    monitor._on_account_update = on_account
    monitor._on_order_update = on_order
    import threading
    monitor._twm = None
    monitor._socket_id = None
    monitor._lock = threading.Lock()
    monitor._started = False
    monitor._shutdown = False
    monitor._message_count = 0
    monitor._account_update_count = 0
    monitor._order_update_count = 0
    monitor._last_message_ts = 0.0
    monitor._error_count = 0
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

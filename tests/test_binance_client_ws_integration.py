"""
Integração BinanceConnection <-> WebSocketKlineStore.

Testa que get_klines usa WS quando disponível/fresh e fallback REST quando não.
Mocka o WebSocketKlineStore pra não precisar de rede.
"""

import threading
from unittest.mock import MagicMock


def _make_client(ws_store=None):
    """Cria BinanceConnection via __new__ sem abrir conexões reais."""
    from trading_bot.infra.binance_client import BinanceConnection

    conn = BinanceConnection.__new__(BinanceConnection)
    conn.config = MagicMock()
    conn.config.API_KEY = "k"
    conn.config.API_SECRET = "s"
    conn.config.USE_TESTNET = True
    conn.config.WEBSOCKET_ENABLED = False  # Desligado pra não abrir WS real
    conn.client = MagicMock()
    conn._cache_lock = threading.Lock()
    conn._balance_cache = None
    conn._balance_cache_ttl = 2.0
    conn._funding_rate_cache = {}
    conn._funding_rate_cache_ttl = 60.0
    conn._order_stats_lock = threading.Lock()
    conn._order_stats_since_report = {
        "attempts": 0, "successes": 0, "failures": 0, "rejections": 0, "symbols": {}
    }
    conn._ws_store = ws_store
    return conn


def test_get_klines_uses_ws_when_fresh():
    """WS store fresh com dados → get_klines devolve do WS, sem REST."""
    ws_store = MagicMock()
    ws_store.is_fresh = MagicMock(return_value=True)
    ws_data = [
        {"timestamp": i, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 1}
        for i in range(50)
    ]
    ws_store.get_klines = MagicMock(return_value=ws_data)

    client = _make_client(ws_store=ws_store)
    rest_mock = MagicMock()
    client._rest_get_klines = rest_mock

    result = client.get_klines("ETHUSDT", "1m", 50)

    assert result == ws_data
    ws_store.is_fresh.assert_called_once_with("ETHUSDT", "1m")
    ws_store.get_klines.assert_called_once_with("ETHUSDT", "1m", 50)
    rest_mock.assert_not_called()


def test_get_klines_falls_back_to_rest_when_ws_stale():
    """WS stale → fallback REST."""
    ws_store = MagicMock()
    ws_store.is_fresh = MagicMock(return_value=False)
    ws_store.get_klines = MagicMock(return_value=None)

    client = _make_client(ws_store=ws_store)
    rest_data = [{"timestamp": 1, "open": 1, "high": 2, "low": 0, "close": 1, "volume": 1}]
    client._rest_get_klines = MagicMock(return_value=rest_data)

    result = client.get_klines("ETHUSDT", "1m", 50)

    assert result == rest_data
    client._rest_get_klines.assert_called_once_with("ETHUSDT", "1m", 50)
    ws_store.get_klines.assert_not_called()


def test_get_klines_falls_back_to_rest_when_ws_store_none():
    """WS desligado → sempre REST."""
    client = _make_client(ws_store=None)
    rest_data = [{"timestamp": 1, "open": 1, "high": 2, "low": 0, "close": 1, "volume": 1}]
    client._rest_get_klines = MagicMock(return_value=rest_data)

    result = client.get_klines("ETHUSDT", "1m", 50)
    assert result == rest_data


def test_get_klines_falls_back_when_ws_returns_insufficient_buffer():
    """WS fresh mas buffer < limit → fallback REST."""
    ws_store = MagicMock()
    ws_store.is_fresh = MagicMock(return_value=True)
    ws_store.get_klines = MagicMock(return_value=None)  # simula buffer insuficiente

    client = _make_client(ws_store=ws_store)
    rest_data = [{"timestamp": 1, "open": 1, "high": 2, "low": 0, "close": 1, "volume": 1}]
    client._rest_get_klines = MagicMock(return_value=rest_data)

    result = client.get_klines("ETHUSDT", "1m", 260)

    assert result == rest_data
    client._rest_get_klines.assert_called_once()


def test_subscribe_klines_stream_noop_when_ws_disabled():
    client = _make_client(ws_store=None)
    assert client.subscribe_klines_stream("ETHUSDT", "1m") is False


def test_subscribe_klines_stream_delegates_to_store():
    ws_store = MagicMock()
    ws_store.subscribe = MagicMock(return_value=True)
    client = _make_client(ws_store=ws_store)

    assert client.subscribe_klines_stream("ETHUSDT", "1m") is True
    ws_store.subscribe.assert_called_once_with("ETHUSDT", "1m")


def test_unsubscribe_klines_stream_delegates():
    ws_store = MagicMock()
    client = _make_client(ws_store=ws_store)

    client.unsubscribe_klines_stream("ETHUSDT", "1m")
    ws_store.unsubscribe.assert_called_once_with("ETHUSDT", "1m")


def test_get_ws_stats_returns_none_when_ws_disabled():
    client = _make_client(ws_store=None)
    assert client.get_ws_stats() is None


def test_get_ws_stats_delegates_to_store():
    ws_store = MagicMock()
    stats_payload = {"subscriptions": 3, "streams": []}
    ws_store.get_stats = MagicMock(return_value=stats_payload)
    client = _make_client(ws_store=ws_store)

    assert client.get_ws_stats() == stats_payload


def test_shutdown_stops_ws_store_and_clears_reference():
    ws_store = MagicMock()
    client = _make_client(ws_store=ws_store)

    client.shutdown()
    ws_store.stop.assert_called_once()
    assert client._ws_store is None


def test_shutdown_is_safe_when_ws_disabled():
    client = _make_client(ws_store=None)
    client.shutdown()  # Não explode


# ---------------------------------------------------------------------------
# Bot — _desired_ws_subscriptions / _sync_ws_subscriptions
# ---------------------------------------------------------------------------

def test_desired_ws_subscriptions_includes_all_trend_strong_timeframes(monkeypatch):
    """Com trend_strong ativo, deve subscribe em TIMEFRAME + exec + confirm."""
    from trading_bot.core import bot as bot_module
    from trading_bot.core.bot import TradingBot

    bot = TradingBot.__new__(TradingBot)
    bot._init_runtime_state()

    monkeypatch.setattr(bot_module.config, "TRADING_PAIRS", ["ETHUSDT", "BTCUSDT"])
    monkeypatch.setattr(bot_module.config, "TIMEFRAME", "5m")
    monkeypatch.setattr(bot_module.config, "TREND_STRONG_EXECUTION_TIMEFRAME", "3m")
    monkeypatch.setattr(bot_module.config, "TREND_STRONG_CONFIRM_TIMEFRAME", "5m")
    monkeypatch.setattr(bot_module.config, "STRATEGY_PROFILES", [
        {"name": "trend_strong", "enabled": True},
    ])

    desired = bot._desired_ws_subscriptions()
    # 2 pares × 2 intervalos distintos (3m + 5m — o 5m do TIMEFRAME dedupa com CONFIRM)
    assert desired == {
        ("ETHUSDT", "3m"), ("ETHUSDT", "5m"),
        ("BTCUSDT", "3m"), ("BTCUSDT", "5m"),
    }


def test_sync_ws_subscriptions_adds_and_removes_delta(monkeypatch):
    """Desired muda → sync sub os faltantes e unsub os extras."""
    from trading_bot.core import bot as bot_module
    from trading_bot.core.bot import TradingBot

    bot = TradingBot.__new__(TradingBot)
    bot._init_runtime_state()

    monkeypatch.setattr(bot_module.config, "WEBSOCKET_ENABLED", True)
    monkeypatch.setattr(bot_module.config, "TRADING_PAIRS", ["ETHUSDT"])
    monkeypatch.setattr(bot_module.config, "TIMEFRAME", "5m")
    monkeypatch.setattr(bot_module.config, "TREND_STRONG_EXECUTION_TIMEFRAME", "3m")
    monkeypatch.setattr(bot_module.config, "TREND_STRONG_CONFIRM_TIMEFRAME", "5m")
    monkeypatch.setattr(bot_module.config, "STRATEGY_PROFILES", [])

    exchange = MagicMock()
    # Já inscrito em ETHUSDT/3m + BTCUSDT/1m; desired é ETHUSDT 3m + 5m
    exchange.get_ws_stats = MagicMock(return_value={
        "subscriptions": 2,
        "streams": [
            {"symbol": "ETHUSDT", "interval": "3m"},
            {"symbol": "BTCUSDT", "interval": "1m"},
        ],
    })
    bot.exchange = exchange

    bot._sync_ws_subscriptions(reason="test")

    # Deve subscribe ETHUSDT/5m (faltava) e unsubscribe BTCUSDT/1m (extra)
    exchange.subscribe_klines_stream.assert_called_once_with("ETHUSDT", "5m")
    exchange.unsubscribe_klines_stream.assert_called_once_with("BTCUSDT", "1m")


def test_sync_ws_subscriptions_noop_when_disabled(monkeypatch):
    from trading_bot.core import bot as bot_module
    from trading_bot.core.bot import TradingBot

    bot = TradingBot.__new__(TradingBot)
    bot._init_runtime_state()
    monkeypatch.setattr(bot_module.config, "WEBSOCKET_ENABLED", False)

    exchange = MagicMock()
    bot.exchange = exchange

    bot._sync_ws_subscriptions()

    exchange.subscribe_klines_stream.assert_not_called()
    exchange.unsubscribe_klines_stream.assert_not_called()

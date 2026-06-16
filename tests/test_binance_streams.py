"""Testes do WebSocketKlineStore — mocka o ThreadedWebsocketManager."""

from unittest.mock import MagicMock, patch


def _seed_fetcher(symbol, interval, limit):
    """REST seed fake: retorna `limit` velas com timestamps crescentes."""
    base_ts = 1_700_000_000_000
    step_ms = {"1m": 60_000, "3m": 180_000, "5m": 300_000}.get(interval, 60_000)
    return [
        {
            "timestamp": base_ts + i * step_ms,
            "open": 100.0 + i * 0.1,
            "high": 100.5 + i * 0.1,
            "low": 99.5 + i * 0.1,
            "close": 100.2 + i * 0.1,
            "volume": 10.0 + i,
        }
        for i in range(limit)
    ]


def _make_store(seed_fetcher=None, staleness=30.0, seed_limit=50):
    from trading_bot.infra.binance_streams import WebSocketKlineStore

    return WebSocketKlineStore(
        api_key="k",
        api_secret="s",
        testnet=True,
        rest_seed_fetcher=seed_fetcher or _seed_fetcher,
        staleness_seconds=staleness,
        seed_limit=seed_limit,
    )


def _kline_msg(ts, open_p, high, low, close, volume, is_closed=True):
    """Simula mensagem kline do WS da Binance."""
    return {
        "e": "kline",
        "E": ts + 500,
        "s": "ETHUSDT",
        "k": {
            "t": ts,
            "T": ts + 60_000,
            "s": "ETHUSDT",
            "i": "1m",
            "o": str(open_p),
            "h": str(high),
            "l": str(low),
            "c": str(close),
            "v": str(volume),
            "x": is_closed,
        },
    }


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_start_is_idempotent_and_stops_cleanly():
    store = _make_store()
    with patch("trading_bot.infra.binance_streams.ThreadedWebsocketManager") as twm_cls:
        twm = MagicMock()
        twm_cls.return_value = twm

        assert store.start() is True
        assert store.start() is True  # idempotente
        assert twm.start.call_count == 1

        store.stop()
        store.stop()  # idempotente
        twm.stop.assert_called_once()


def test_stop_before_start_is_safe():
    store = _make_store()
    store.stop()  # não explode


# ---------------------------------------------------------------------------
# Subscribe + seed
# ---------------------------------------------------------------------------

def test_subscribe_seeds_buffer_and_starts_socket():
    store = _make_store(seed_limit=50)
    with patch("trading_bot.infra.binance_streams.ThreadedWebsocketManager") as twm_cls:
        twm = MagicMock()
        twm.start_kline_futures_socket = MagicMock(return_value="sock-1")
        twm_cls.return_value = twm

        store.start()
        ok = store.subscribe("ETHUSDT", "1m")

        assert ok is True
        assert store.subscriptions() == [("ETHUSDT", "1m")]
        twm.start_kline_futures_socket.assert_called_once()
        # Buffer tem as 50 velas do seed
        data = store.get_klines("ETHUSDT", "1m", 30)
        assert data is not None
        assert len(data) == 30  # últimas 30
        assert store.is_fresh("ETHUSDT", "1m") is True


def test_subscribe_is_idempotent_per_symbol_interval():
    store = _make_store()
    with patch("trading_bot.infra.binance_streams.ThreadedWebsocketManager") as twm_cls:
        twm = MagicMock()
        twm.start_kline_futures_socket = MagicMock(return_value="sock-1")
        twm_cls.return_value = twm

        store.start()
        store.subscribe("ETHUSDT", "1m")
        store.subscribe("ETHUSDT", "1m")  # mesma chave

        assert twm.start_kline_futures_socket.call_count == 1


def test_subscribe_without_start_fails_gracefully():
    store = _make_store()
    assert store.subscribe("ETHUSDT", "1m") is False


def test_subscribe_fails_when_seed_returns_empty():
    def empty_seed(sym, interval, limit):
        return []

    store = _make_store(seed_fetcher=empty_seed)
    with patch("trading_bot.infra.binance_streams.ThreadedWebsocketManager") as twm_cls:
        twm = MagicMock()
        twm_cls.return_value = twm
        store.start()
        assert store.subscribe("ETHUSDT", "1m") is False
        twm.start_kline_futures_socket.assert_not_called()


def test_subscribe_recovers_when_seed_fetch_raises():
    def raising_seed(sym, interval, limit):
        raise RuntimeError("API down")

    store = _make_store(seed_fetcher=raising_seed)
    with patch("trading_bot.infra.binance_streams.ThreadedWebsocketManager") as twm_cls:
        twm = MagicMock()
        twm_cls.return_value = twm
        store.start()
        assert store.subscribe("ETHUSDT", "1m") is False


# ---------------------------------------------------------------------------
# Unsubscribe
# ---------------------------------------------------------------------------

def test_unsubscribe_drops_buffer_and_stops_socket():
    store = _make_store()
    with patch("trading_bot.infra.binance_streams.ThreadedWebsocketManager") as twm_cls:
        twm = MagicMock()
        twm.start_kline_futures_socket = MagicMock(return_value="sock-42")
        twm_cls.return_value = twm

        store.start()
        store.subscribe("ETHUSDT", "1m")
        store.unsubscribe("ETHUSDT", "1m")

        assert store.subscriptions() == []
        assert store.get_klines("ETHUSDT", "1m", 10) is None
        assert store.is_fresh("ETHUSDT", "1m") is False
        twm.stop_socket.assert_called_once_with("sock-42")


def test_unsubscribe_silent_when_not_subscribed():
    store = _make_store()
    # Não explode mesmo sem start
    store.unsubscribe("ETHUSDT", "1m")


# ---------------------------------------------------------------------------
# Mensagens WS
# ---------------------------------------------------------------------------

def test_closed_kline_message_appends_to_buffer():
    store = _make_store(seed_limit=10)
    with patch("trading_bot.infra.binance_streams.ThreadedWebsocketManager") as twm_cls:
        twm = MagicMock()
        twm.start_kline_futures_socket = MagicMock(return_value="sid")
        twm_cls.return_value = twm

        store.start()
        store.subscribe("ETHUSDT", "1m")
        seed_last_ts = store.get_klines("ETHUSDT", "1m", 1)[-1]["timestamp"]

        # Vela nova, fechada, timestamp posterior
        new_ts = seed_last_ts + 60_000
        store._on_kline_message(
            ("ETHUSDT", "1m"),
            _kline_msg(new_ts, 110, 111, 109, 110.5, 5.0, is_closed=True),
        )

        data = store.get_klines("ETHUSDT", "1m", 1)
        assert data[-1]["timestamp"] == new_ts
        assert data[-1]["close"] == 110.5


def test_unclosed_kline_keeps_buffer_but_refreshes_age():
    store = _make_store(seed_limit=10)
    with patch("trading_bot.infra.binance_streams.ThreadedWebsocketManager") as twm_cls:
        twm = MagicMock()
        twm.start_kline_futures_socket = MagicMock(return_value="sid")
        twm_cls.return_value = twm

        store.start()
        store.subscribe("ETHUSDT", "1m")
        before = store.get_klines("ETHUSDT", "1m", 10)
        before_len = len(before)

        # Vela em formação — NÃO deve ser adicionada ao buffer
        store._on_kline_message(
            ("ETHUSDT", "1m"),
            _kline_msg(9_999_999_999_999, 1, 2, 0.5, 1.5, 1.0, is_closed=False),
        )

        after = store.get_klines("ETHUSDT", "1m", 10)
        assert len(after) == before_len
        # Mas is_fresh continua True (stream vivo)
        assert store.is_fresh("ETHUSDT", "1m") is True


def test_duplicate_timestamp_replaces_last_candle():
    store = _make_store(seed_limit=10)
    with patch("trading_bot.infra.binance_streams.ThreadedWebsocketManager") as twm_cls:
        twm = MagicMock()
        twm.start_kline_futures_socket = MagicMock(return_value="sid")
        twm_cls.return_value = twm

        store.start()
        store.subscribe("ETHUSDT", "1m")
        last_ts = store.get_klines("ETHUSDT", "1m", 10)[-1]["timestamp"]
        len_before = len(store.get_klines("ETHUSDT", "1m", 10))

        # Msg com MESMO timestamp da última — substitui, não duplica
        store._on_kline_message(
            ("ETHUSDT", "1m"),
            _kline_msg(last_ts, 999, 999, 999, 999, 999, is_closed=True),
        )

        data = store.get_klines("ETHUSDT", "1m", 10)
        assert len(data) == len_before
        assert data[-1]["close"] == 999


def test_out_of_order_message_is_rejected():
    store = _make_store(seed_limit=10)
    with patch("trading_bot.infra.binance_streams.ThreadedWebsocketManager") as twm_cls:
        twm = MagicMock()
        twm.start_kline_futures_socket = MagicMock(return_value="sid")
        twm_cls.return_value = twm

        store.start()
        store.subscribe("ETHUSDT", "1m")
        last_ts = store.get_klines("ETHUSDT", "1m", 1)[-1]["timestamp"]

        # Msg com timestamp ANTERIOR ao último — ignorada
        store._on_kline_message(
            ("ETHUSDT", "1m"),
            _kline_msg(last_ts - 60_000, 1, 2, 0.5, 1.5, 1.0, is_closed=True),
        )

        data = store.get_klines("ETHUSDT", "1m", 1)
        # Último timestamp inalterado
        assert data[-1]["timestamp"] == last_ts


def test_error_message_is_logged_not_raised():
    store = _make_store(seed_limit=10)
    with patch("trading_bot.infra.binance_streams.ThreadedWebsocketManager") as twm_cls:
        twm = MagicMock()
        twm.start_kline_futures_socket = MagicMock(return_value="sid")
        twm_cls.return_value = twm

        store.start()
        store.subscribe("ETHUSDT", "1m")

        # Não deve explodir
        store._on_kline_message(
            ("ETHUSDT", "1m"),
            {"e": "error", "m": "Connection lost"},
        )


# ---------------------------------------------------------------------------
# Staleness / is_fresh
# ---------------------------------------------------------------------------

def test_is_fresh_false_for_unknown_subscription():
    store = _make_store()
    assert store.is_fresh("BTCUSDT", "1m") is False


def test_is_fresh_false_when_staleness_exceeded():
    store = _make_store(seed_limit=10, staleness=0.05)  # 50ms
    with patch("trading_bot.infra.binance_streams.ThreadedWebsocketManager") as twm_cls:
        twm = MagicMock()
        twm.start_kline_futures_socket = MagicMock(return_value="sid")
        twm_cls.return_value = twm

        store.start()
        store.subscribe("ETHUSDT", "1m")
        assert store.is_fresh("ETHUSDT", "1m") is True

        # Envelhece o timestamp da última mensagem além da janela de staleness,
        # sem sleep real — testa o comparador de is_fresh de forma determinística.
        with store._lock:
            store._last_message_ts[("ETHUSDT", "1m")] -= 1.0
        assert store.is_fresh("ETHUSDT", "1m") is False


# ---------------------------------------------------------------------------
# get_klines
# ---------------------------------------------------------------------------

def test_get_klines_returns_none_for_unknown_subscription():
    store = _make_store()
    assert store.get_klines("BTCUSDT", "1m", 50) is None


def test_get_klines_returns_copy_not_reference():
    store = _make_store(seed_limit=30)
    with patch("trading_bot.infra.binance_streams.ThreadedWebsocketManager") as twm_cls:
        twm = MagicMock()
        twm.start_kline_futures_socket = MagicMock(return_value="sid")
        twm_cls.return_value = twm

        store.start()
        store.subscribe("ETHUSDT", "1m")

        snapshot = store.get_klines("ETHUSDT", "1m", 30)
        snapshot.clear()
        # Buffer original continua intacto
        assert len(store.get_klines("ETHUSDT", "1m", 30)) == 30


def test_get_klines_returns_none_when_buffer_smaller_than_requested():
    def tiny_seed(sym, interval, limit):
        return _seed_fetcher(sym, interval, 5)  # só 5 velas

    store = _make_store(seed_fetcher=tiny_seed, seed_limit=5)
    with patch("trading_bot.infra.binance_streams.ThreadedWebsocketManager") as twm_cls:
        twm = MagicMock()
        twm.start_kline_futures_socket = MagicMock(return_value="sid")
        twm_cls.return_value = twm

        store.start()
        store.subscribe("ETHUSDT", "1m")

        # Buffer tem 5 velas, caller pede 50 → None (caller cai em fallback REST)
        assert store.get_klines("ETHUSDT", "1m", 50) is None
        # Caller pede 3 → buffer tem o bastante → retorna
        assert len(store.get_klines("ETHUSDT", "1m", 3)) == 3


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def test_get_stats_exposes_stream_metadata():
    store = _make_store(seed_limit=10)
    with patch("trading_bot.infra.binance_streams.ThreadedWebsocketManager") as twm_cls:
        twm = MagicMock()
        twm.start_kline_futures_socket = MagicMock(return_value="sid-x")
        twm_cls.return_value = twm

        store.start()
        store.subscribe("ETHUSDT", "1m")
        store._on_kline_message(
            ("ETHUSDT", "1m"),
            _kline_msg(9_999_999_999_999, 1, 2, 0.5, 1.5, 1.0, is_closed=False),
        )

        stats = store.get_stats()
        assert stats["subscriptions"] == 1
        assert len(stats["streams"]) == 1
        stream = stats["streams"][0]
        assert stream["symbol"] == "ETHUSDT"
        assert stream["interval"] == "1m"
        assert stream["messages"] >= 1
        assert stream["age_seconds"] >= 0.0
        assert stream["buffer_size"] == 10  # seed inalterado (msg era is_closed=False)

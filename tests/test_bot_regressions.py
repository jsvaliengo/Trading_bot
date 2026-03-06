from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from trading_bot.core.bot import TradingBot
from trading_bot.core.config import config
from trading_bot.core.strategy import Signal, TradeSetup


def _make_light_bot():
    return TradingBot.__new__(TradingBot)


def test_stop_keeps_open_positions_and_does_not_close_them(monkeypatch):
    bot = _make_light_bot()

    open_positions = [
        {
            "symbol": "ETHUSDT",
            "side": "LONG",
            "quantity": 0.5,
            "entry_price": 100.0,
            "unrealized_pnl": 2.5,
        }
    ]

    class ExchangeStub:
        def __init__(self):
            self.flush_called = False
            self.close_attempted = False

        def flush_retry_stats(self):
            self.flush_called = True

        def get_open_positions(self):
            return list(open_positions)

        def close_position(self, *_args, **_kwargs):
            self.close_attempted = True
            raise AssertionError("stop() não deve fechar posições abertas")

    class CommandHandlerStub:
        def __init__(self):
            self.stopped = False

        def stop_polling(self):
            self.stopped = True

    class TelegramStub:
        def __init__(self):
            self.shutdown_calls = []

        def send_shutdown_message(self, total_pnl, total_trades):
            self.shutdown_calls.append(
                {"total_pnl": total_pnl, "total_trades": total_trades}
            )
            return True

    exchange = ExchangeStub()
    command_handler = CommandHandlerStub()
    telegram = TelegramStub()

    bot.running = True
    bot.exchange = exchange
    bot.command_handler = command_handler
    bot.telegram = telegram
    bot.risk_manager = SimpleNamespace(daily_pnl=1.23)
    bot.trade_history = []
    bot.total_pnl = 10.0
    bot.closed_trades_count = 4
    bot.pnl_by_symbol = {"ETHUSDT": 3.0}

    state_saved = {"called": False}

    def _save_state():
        state_saved["called"] = True
        return True

    bot.save_state = _save_state

    monkeypatch.setattr(config, "TRADING_PAIRS", ["ETHUSDT"])

    bot.stop()

    assert bot.running is False
    assert command_handler.stopped is True
    assert exchange.flush_called is True
    assert exchange.close_attempted is False
    assert state_saved["called"] is True
    assert len(telegram.shutdown_calls) == 1
    assert telegram.shutdown_calls[0]["total_trades"] == 4
    # total_pnl enviado inclui não realizado
    assert telegram.shutdown_calls[0]["total_pnl"] == 12.5


def test_trailing_stop_activates_and_closes_long_on_retrace(monkeypatch):
    bot = _make_light_bot()
    bot.peak_prices = {}
    bot.trailing_activated = {}
    bot.exchange = SimpleNamespace(
        get_funding_rate=lambda _symbol: {"rate_percent": 0.0}
    )
    bot.telegram = SimpleNamespace(
        send_trailing_stop_activated=lambda **_kwargs: True
    )

    monkeypatch.setattr(config, "CHECK_FUNDING_RATE", True)
    monkeypatch.setattr(config, "TRAILING_ACTIVATION_PERCENT", 0.20)
    monkeypatch.setattr(config, "TRAILING_DISTANCE_PERCENT", 0.12)
    monkeypatch.setattr(config, "TRAILING_MIN_PROFIT_USD", 0.20)
    monkeypatch.setattr(config, "TRAILING_MIN_PROFIT_HIGH_FUNDING", 0.35)
    monkeypatch.setattr(config, "FUNDING_RATE_THRESHOLD", 0.02)

    key = "ETHUSDT_LONG"
    # Ativa trailing
    should_close, reason = bot._check_trailing_stop(
        key, "LONG", 100.0, 100.25, "ETHUSDT", 1.0
    )
    assert should_close is False
    assert reason == ""
    assert bot.trailing_activated[key] is True

    # Novo pico
    should_close, reason = bot._check_trailing_stop(
        key, "LONG", 100.0, 100.40, "ETHUSDT", 1.0
    )
    assert should_close is False
    assert reason == ""

    # Recuo que atinge trailing e mantém lucro mínimo
    should_close, reason = bot._check_trailing_stop(
        key, "LONG", 100.0, 100.27, "ETHUSDT", 1.0
    )
    assert should_close is True
    assert "Trailing Stop" in reason


def test_trailing_stop_activates_and_closes_short_on_retrace(monkeypatch):
    bot = _make_light_bot()
    bot.peak_prices = {}
    bot.trailing_activated = {}
    bot.exchange = SimpleNamespace(
        get_funding_rate=lambda _symbol: {"rate_percent": 0.0}
    )
    bot.telegram = SimpleNamespace(
        send_trailing_stop_activated=lambda **_kwargs: True
    )

    monkeypatch.setattr(config, "CHECK_FUNDING_RATE", True)
    monkeypatch.setattr(config, "TRAILING_ACTIVATION_PERCENT", 0.20)
    monkeypatch.setattr(config, "TRAILING_DISTANCE_PERCENT", 0.12)
    monkeypatch.setattr(config, "TRAILING_MIN_PROFIT_USD", 0.20)
    monkeypatch.setattr(config, "TRAILING_MIN_PROFIT_HIGH_FUNDING", 0.35)
    monkeypatch.setattr(config, "FUNDING_RATE_THRESHOLD", 0.02)

    key = "ETHUSDT_SHORT"
    # Ativa trailing no SHORT
    should_close, reason = bot._check_trailing_stop(
        key, "SHORT", 100.0, 99.75, "ETHUSDT", 1.0
    )
    assert should_close is False
    assert reason == ""
    assert bot.trailing_activated[key] is True

    # Novo fundo
    should_close, reason = bot._check_trailing_stop(
        key, "SHORT", 100.0, 99.50, "ETHUSDT", 1.0
    )
    assert should_close is False
    assert reason == ""

    # Repique que atinge trailing no SHORT e mantém lucro mínimo
    should_close, reason = bot._check_trailing_stop(
        key, "SHORT", 100.0, 99.64, "ETHUSDT", 1.0
    )
    assert should_close is True
    assert "Trailing Stop" in reason


def test_trailing_stop_hit_does_not_close_when_profit_usd_below_minimum(monkeypatch):
    bot = _make_light_bot()
    bot.peak_prices = {}
    bot.trailing_activated = {}
    bot.exchange = SimpleNamespace(
        get_funding_rate=lambda _symbol: {"rate_percent": 0.0}
    )
    bot.telegram = SimpleNamespace(
        send_trailing_stop_activated=lambda **_kwargs: True
    )

    monkeypatch.setattr(config, "CHECK_FUNDING_RATE", False)
    monkeypatch.setattr(config, "TRAILING_ACTIVATION_PERCENT", 0.20)
    monkeypatch.setattr(config, "TRAILING_DISTANCE_PERCENT", 0.05)
    monkeypatch.setattr(config, "TRAILING_MIN_PROFIT_USD", 0.50)

    key = "ETHUSDT_LONG"
    position_amt = 0.01

    # Ativa trailing
    should_close, reason = bot._check_trailing_stop(
        key, "LONG", 100.0, 100.25, "ETHUSDT", position_amt
    )
    assert should_close is False
    assert reason == ""
    assert bot.trailing_activated[key] is True

    # Novo pico
    should_close, reason = bot._check_trailing_stop(
        key, "LONG", 100.0, 100.30, "ETHUSDT", position_amt
    )
    assert should_close is False
    assert reason == ""

    # Atinge trailing, mas lucro em USD ainda abaixo do mínimo
    should_close, reason = bot._check_trailing_stop(
        key, "LONG", 100.0, 100.24, "ETHUSDT", position_amt
    )
    assert should_close is False
    assert reason == ""


def test_analyze_and_trade_skips_reentry_when_long_is_already_open(monkeypatch):
    bot = _make_light_bot()

    monkeypatch.setattr(config, "USE_DAILY_TARGETS", False)
    monkeypatch.setattr(config, "TIMEFRAME", "5m")
    monkeypatch.setattr(config, "CANDLES_LOOKBACK", 50)

    setup = TradeSetup(
        symbol="ETHUSDT",
        signal=Signal.BUY,
        long_size=5.0,
        short_size=5.0,
        entry_price=100.0,
        stop_loss=98.0,
        take_profit=102.0,
        dca_levels=[],
    )

    bot.exchange = SimpleNamespace(
        get_klines=lambda **_kwargs: [{"close": 100.0}],
        get_available_balance=lambda: 1000.0,
        get_symbol_info=lambda _symbol: {"minNotional": 5.0},
        get_open_positions=lambda: [
            {
                "symbol": "ETHUSDT",
                "side": "LONG",
                "quantity": 0.1,
                "entry_price": 100.0,
                "unrealized_pnl": 0.0,
            }
        ],
    )
    bot.strategy = SimpleNamespace(
        generate_trade_setup=lambda **_kwargs: setup
    )
    bot.risk_manager = SimpleNamespace(can_open_position=lambda _total: True)
    bot.execute_signal_trade = MagicMock(return_value=True)

    result = bot.analyze_and_trade("ETHUSDT")

    assert result is False
    bot.execute_signal_trade.assert_not_called()


def test_analyze_and_trade_ignores_weak_buy_signal(monkeypatch):
    bot = _make_light_bot()

    monkeypatch.setattr(config, "USE_DAILY_TARGETS", False)
    monkeypatch.setattr(config, "TIMEFRAME", "5m")
    monkeypatch.setattr(config, "CANDLES_LOOKBACK", 50)

    setup = TradeSetup(
        symbol="ETHUSDT",
        signal=Signal.BUY,
        long_size=5.0,
        short_size=5.0,
        entry_price=100.0,
        stop_loss=98.0,
        take_profit=102.0,
        dca_levels=[],
    )

    bot.exchange = SimpleNamespace(
        get_klines=lambda **_kwargs: [{"close": 100.0}],
        get_available_balance=lambda: 1000.0,
        get_symbol_info=lambda _symbol: {"minNotional": 5.0},
        get_open_positions=lambda: [],
    )
    bot.strategy = SimpleNamespace(generate_trade_setup=lambda **_kwargs: setup)
    bot.risk_manager = SimpleNamespace(can_open_position=lambda _total: True)
    bot.sentiment_mode_enabled = False
    bot.execute_signal_trade = MagicMock(return_value=True)

    result = bot.analyze_and_trade("ETHUSDT")

    assert result is False
    bot.execute_signal_trade.assert_not_called()


def test_analyze_and_trade_skips_reentry_when_short_is_already_open(monkeypatch):
    bot = _make_light_bot()

    monkeypatch.setattr(config, "USE_DAILY_TARGETS", False)
    monkeypatch.setattr(config, "TIMEFRAME", "5m")
    monkeypatch.setattr(config, "CANDLES_LOOKBACK", 50)

    setup = TradeSetup(
        symbol="ETHUSDT",
        signal=Signal.SELL,
        long_size=5.0,
        short_size=5.0,
        entry_price=100.0,
        stop_loss=102.0,
        take_profit=98.0,
        dca_levels=[],
    )

    bot.exchange = SimpleNamespace(
        get_klines=lambda **_kwargs: [{"close": 100.0}],
        get_available_balance=lambda: 1000.0,
        get_symbol_info=lambda _symbol: {"minNotional": 5.0},
        get_open_positions=lambda: [
            {
                "symbol": "ETHUSDT",
                "side": "SHORT",
                "quantity": 0.1,
                "entry_price": 100.0,
                "unrealized_pnl": 0.0,
            }
        ],
    )
    bot.strategy = SimpleNamespace(
        generate_trade_setup=lambda **_kwargs: setup
    )
    bot.risk_manager = SimpleNamespace(can_open_position=lambda _total: True)
    bot.execute_signal_trade = MagicMock(return_value=True)

    result = bot.analyze_and_trade("ETHUSDT")

    assert result is False
    bot.execute_signal_trade.assert_not_called()


def test_analyze_and_trade_ignores_weak_sell_signal(monkeypatch):
    bot = _make_light_bot()

    monkeypatch.setattr(config, "USE_DAILY_TARGETS", False)
    monkeypatch.setattr(config, "TIMEFRAME", "5m")
    monkeypatch.setattr(config, "CANDLES_LOOKBACK", 50)

    setup = TradeSetup(
        symbol="ETHUSDT",
        signal=Signal.SELL,
        long_size=5.0,
        short_size=5.0,
        entry_price=100.0,
        stop_loss=102.0,
        take_profit=98.0,
        dca_levels=[],
    )

    bot.exchange = SimpleNamespace(
        get_klines=lambda **_kwargs: [{"close": 100.0}],
        get_available_balance=lambda: 1000.0,
        get_symbol_info=lambda _symbol: {"minNotional": 5.0},
        get_open_positions=lambda: [],
    )
    bot.strategy = SimpleNamespace(generate_trade_setup=lambda **_kwargs: setup)
    bot.risk_manager = SimpleNamespace(can_open_position=lambda _total: True)
    bot.sentiment_mode_enabled = False
    bot.execute_signal_trade = MagicMock(return_value=True)

    result = bot.analyze_and_trade("ETHUSDT")

    assert result is False
    bot.execute_signal_trade.assert_not_called()


def test_setup_exchange_restores_open_positions_for_reentry_tracking(monkeypatch):
    bot = _make_light_bot()

    now = datetime.now()
    open_positions = [
        {
            "symbol": "ETHUSDT",
            "side": "LONG",
            "quantity": 0.4,
            "entry_price": 100.0,
            "unrealized_pnl": 1.2,
            "last_seen": now,
        }
    ]

    bot.exchange = SimpleNamespace(
        set_hedge_mode=lambda: True,
        set_leverage=lambda _symbol, _lev: True,
        get_account_balance=lambda: 100.0,
        get_daily_pnl_from_binance=lambda: {"total": 0.0},
        get_open_positions=lambda: list(open_positions),
    )
    bot.telegram = SimpleNamespace(send_message=lambda *_args, **_kwargs: True)
    bot.update_commission_rates = lambda: None
    bot.cache_pairs_min_notional = lambda: None
    bot.pnl_by_symbol = {}
    bot.known_positions = {}
    bot.initial_capital = None
    bot.last_transfer_check_ts_ms = 0
    bot.processed_transfer_ids = []

    monkeypatch.setattr(config, "USE_BINANCE_STRATEGY", False)
    monkeypatch.setattr(config, "AUTO_SELECT_PAIRS", False)
    monkeypatch.setattr(config, "TRADING_PAIRS", ["ETHUSDT"])
    monkeypatch.setattr(config, "LEVERAGE", 20)

    ok = bot.setup_exchange()

    assert ok is True
    assert "ETHUSDT_LONG" in bot.known_positions
    assert bot.known_positions["ETHUSDT_LONG"]["quantity"] == 0.4


def test_sort_binance_coins_by_score_uses_dynamic_binance_universe(monkeypatch):
    bot = _make_light_bot()
    bot.exchange = SimpleNamespace()

    monkeypatch.setattr(config, "DISABLED_PAIRS", ["ETHUSDT"])
    monkeypatch.setattr(config, "BINANCE_COIN_LIST", ["OLDUSDT"])

    score_map = {
        "ADAUSDT": 90.0,
        "XRPUSDT": 70.0,
        "ETHUSDT": 99.0,  # desabilitado
    }

    class PairSelectorStub:
        def get_all_futures_pairs(self):
            return ["XRPUSDT", "ETHUSDT", "ADAUSDT"]

        def get_pair_metrics(self, symbol):
            return {"symbol": symbol}

        def score_pair(self, metrics):
            return score_map.get(metrics["symbol"], 0.0)

    bot.pair_selector = PairSelectorStub()

    best = bot.sort_binance_coins_by_score(2)

    assert best == ["ADAUSDT", "XRPUSDT"]
    assert config.BINANCE_COIN_LIST == ["XRPUSDT", "ETHUSDT", "ADAUSDT"]


def test_sort_binance_coins_by_score_keeps_previous_universe_on_refresh_failure(monkeypatch):
    bot = _make_light_bot()
    bot.exchange = SimpleNamespace()

    monkeypatch.setattr(config, "DISABLED_PAIRS", [])
    monkeypatch.setattr(config, "BINANCE_COIN_LIST", ["BNBUSDT", "XRPUSDT"])

    score_map = {"BNBUSDT": 80.0, "XRPUSDT": 60.0}

    class PairSelectorStub:
        def get_all_futures_pairs(self):
            return []

        def get_pair_metrics(self, symbol):
            return {"symbol": symbol}

        def score_pair(self, metrics):
            return score_map.get(metrics["symbol"], 0.0)

    bot.pair_selector = PairSelectorStub()

    best = bot.sort_binance_coins_by_score(1)

    assert best == ["BNBUSDT"]
    assert config.BINANCE_COIN_LIST == ["BNBUSDT", "XRPUSDT"]


def test_close_position_does_not_account_when_exchange_close_fails():
    bot = _make_light_bot()

    bot.exchange = SimpleNamespace(
        get_current_price=lambda _symbol: 101.0,
        close_position=lambda _symbol, _side: False,
    )
    bot.get_taker_fee_rate = lambda: 0.0005
    bot.risk_manager = SimpleNamespace(update_pnl=MagicMock())
    bot.telegram = SimpleNamespace(send_position_closed=MagicMock(return_value=True))

    bot.closed_trades_count = 0
    bot.daily_realized_pnl = 0.0
    bot.total_pnl = 0.0
    bot.trades_win_count = 0
    bot.trades_loss_count = 0
    bot.trades_win_total = 0.0
    bot.trades_loss_total = 0.0
    bot.total_fees_paid = 0.0
    bot.trades_by_symbol = {}
    bot.pnl_by_symbol = {}

    pos = {
        "symbol": "ETHUSDT",
        "side": "LONG",
        "entry_price": 100.0,
        "quantity": 0.5,
        "leverage": 20,
    }

    closed = bot._close_position_with_notification(pos, "Take Profit")

    assert closed is False
    assert bot.closed_trades_count == 0
    assert bot.daily_realized_pnl == 0.0
    assert bot.total_pnl == 0.0
    assert bot.trades_win_count == 0
    assert bot.trades_loss_count == 0
    assert bot.trades_win_total == 0.0
    assert bot.trades_loss_total == 0.0
    assert bot.total_fees_paid == 0.0
    assert bot.trades_by_symbol == {}
    assert bot.pnl_by_symbol == {}
    bot.risk_manager.update_pnl.assert_not_called()
    bot.telegram.send_position_closed.assert_not_called()


def test_check_global_stop_loss_ignores_invalid_initial_capital():
    bot = _make_light_bot()

    bot.exchange = SimpleNamespace(
        get_account_info=lambda: {"wallet_balance": 100.0, "unrealized_pnl": -5.0},
        get_daily_pnl_from_binance=lambda: {"total": -10.0},
    )
    bot.initial_capital = 0.0

    assert bot.check_global_stop_loss() is False


def test_execute_global_stop_loss_handles_invalid_initial_capital():
    bot = _make_light_bot()

    bot.exchange = SimpleNamespace(
        get_open_positions=lambda: [],
        get_account_info=lambda: {"wallet_balance": 120.0, "unrealized_pnl": -8.0},
        get_daily_pnl_from_binance=lambda: {"total": -12.0},
    )
    bot.telegram = SimpleNamespace(send_global_stop_loss_alert=MagicMock())
    bot.save_state = MagicMock(return_value=True)
    bot.initial_capital = 0.0
    bot.running = True

    bot.execute_global_stop_loss()

    assert bot.running is False
    assert bot.telegram.send_global_stop_loss_alert.call_count == 1
    call_kwargs = bot.telegram.send_global_stop_loss_alert.call_args.kwargs
    assert call_kwargs["initial_capital"] == 120.0


def test_double_first_global_applies_once_per_side(monkeypatch):
    bot = _make_light_bot()
    bot.double_first_used = {}

    monkeypatch.setattr(config, "DOUBLE_FIRST_LONG_ENABLED", True)
    monkeypatch.setattr(config, "DOUBLE_FIRST_SHORT_ENABLED", True)
    monkeypatch.setattr(config, "DOUBLE_FIRST_MULTIPLIER", 2.0)
    monkeypatch.setattr(config, "DOUBLE_FIRST_MAX_MARGIN_USDT", 0.0)
    monkeypatch.setattr(config, "DOUBLE_FIRST_SCOPE", "global")

    long_size, long_applied, long_key = bot._apply_double_first_order_size("ETHUSDT", "LONG", 3.0)
    assert long_applied is True
    assert long_key == "LONG"
    assert long_size == 6.0

    bot._mark_double_first_used(
        state_key=long_key,
        symbol="ETHUSDT",
        side="LONG",
        base_order_size=3.0,
        applied_order_size=long_size,
    )

    long_size_2, long_applied_2, long_key_2 = bot._apply_double_first_order_size("ETHUSDT", "LONG", 3.0)
    assert long_applied_2 is False
    assert long_key_2 == ""
    assert long_size_2 == 3.0

    short_size, short_applied, short_key = bot._apply_double_first_order_size("ETHUSDT", "SHORT", 3.0)
    assert short_applied is True
    assert short_key == "SHORT"
    assert short_size == 6.0


def test_double_first_symbol_scope_respects_cap_and_tracks_per_symbol(monkeypatch):
    bot = _make_light_bot()
    bot.double_first_used = {}

    monkeypatch.setattr(config, "DOUBLE_FIRST_LONG_ENABLED", True)
    monkeypatch.setattr(config, "DOUBLE_FIRST_SHORT_ENABLED", False)
    monkeypatch.setattr(config, "DOUBLE_FIRST_MULTIPLIER", 2.0)
    monkeypatch.setattr(config, "DOUBLE_FIRST_MAX_MARGIN_USDT", 5.0)
    monkeypatch.setattr(config, "DOUBLE_FIRST_SCOPE", "symbol")

    eth_size, eth_applied, eth_key = bot._apply_double_first_order_size("ETHUSDT", "LONG", 3.0)
    assert eth_applied is True
    assert eth_key == "ETHUSDT_LONG"
    assert eth_size == 5.0

    bot._mark_double_first_used(
        state_key=eth_key,
        symbol="ETHUSDT",
        side="LONG",
        base_order_size=3.0,
        applied_order_size=eth_size,
    )

    eth_size_2, eth_applied_2, _ = bot._apply_double_first_order_size("ETHUSDT", "LONG", 3.0)
    assert eth_applied_2 is False
    assert eth_size_2 == 3.0

    xrp_size, xrp_applied, xrp_key = bot._apply_double_first_order_size("XRPUSDT", "LONG", 3.0)
    assert xrp_applied is True
    assert xrp_key == "XRPUSDT_LONG"
    assert xrp_size == 5.0


def test_normalize_double_first_state_accepts_legacy_formats():
    bot = _make_light_bot()

    normalized_from_list = bot._normalize_double_first_state(
        ["long", "ETHUSDT_short", "invalid_key"]
    )
    assert normalized_from_list == {"LONG": True, "ETHUSDT_SHORT": True}

    normalized_from_dict = bot._normalize_double_first_state(
        {"short": True, "BTCUSDT_LONG": 1, "foo": True, "ADAUSDT_SHORT": False}
    )
    assert normalized_from_dict == {"SHORT": True, "BTCUSDT_LONG": True}


def test_analyze_and_trade_blocks_signal_when_sentiment_conflicts(monkeypatch):
    bot = _make_light_bot()

    monkeypatch.setattr(config, "USE_DAILY_TARGETS", False)
    monkeypatch.setattr(config, "TIMEFRAME", "5m")
    monkeypatch.setattr(config, "CANDLES_LOOKBACK", 50)

    setup = TradeSetup(
        symbol="ETHUSDT",
        signal=Signal.SELL,
        long_size=5.0,
        short_size=5.0,
        entry_price=100.0,
        stop_loss=102.0,
        take_profit=98.0,
        dca_levels=[],
    )

    bot.exchange = SimpleNamespace(
        get_klines=lambda **_kwargs: [{"close": 100.0}],
        get_available_balance=lambda: 1000.0,
        get_symbol_info=lambda _symbol: {"minNotional": 5.0},
        get_open_positions=lambda: [],
    )
    bot.strategy = SimpleNamespace(generate_trade_setup=lambda **_kwargs: setup)
    bot.risk_manager = SimpleNamespace(can_open_position=lambda _total: True)
    bot.execute_signal_trade = MagicMock(return_value=True)

    bot.sentiment_mode_enabled = True
    bot._get_symbol_sentiment = lambda _symbol, force_refresh=False: {
        "direction": "LONG_ONLY",
        "bias": "BULLISH",
        "score": 3,
    }

    result = bot.analyze_and_trade("ETHUSDT")

    assert result is False
    bot.execute_signal_trade.assert_not_called()

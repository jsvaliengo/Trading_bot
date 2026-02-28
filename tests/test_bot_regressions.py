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

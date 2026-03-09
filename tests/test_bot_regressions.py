from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from trading_bot.core.bot import TradingBot
from trading_bot.core.config import config
from trading_bot.core.strategy import HedgeStrategy, RangeScalpingStrategy, Signal, TradeSetup


def _make_light_bot():
    bot = TradingBot.__new__(TradingBot)
    bot._init_runtime_state()
    return bot


def _make_range_klines(last_close: float) -> list[dict]:
    pattern = [
        99.15, 99.82, 99.22, 99.78, 99.18, 99.88,
        99.24, 99.74, 99.16, 99.84, 99.20, last_close,
    ]
    rows = []
    for index, close_price in enumerate(pattern):
        open_price = close_price + (0.03 if index % 2 == 0 else -0.03)
        high_price = min(100.0, max(open_price, close_price) + 0.12)
        low_price = max(99.0, min(open_price, close_price) - 0.12)
        if index in {1, 5, 9}:
            high_price = 100.0
        if index in {0, 4, 8}:
            low_price = 99.0
        rows.append(
            {
                "open": round(open_price, 6),
                "high": round(high_price, 6),
                "low": round(low_price, 6),
                "close": round(close_price, 6),
                "volume": 110.0 - float(index % 4),
            }
        )
    return rows


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

    # Recuo que atinge trailing — fecha imediatamente (sem gate de lucro mínimo USD)
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


def test_trailing_stop_always_closes_when_hit_regardless_of_profit_usd(monkeypatch):
    """Trailing stop deve fechar SEMPRE ao atingir o stop, sem gate de lucro mínimo USD.
    Gate em USD bloqueava fechamento em posições pequenas (ex: $3 × 10x = $30 notional)
    onde profit_usd nunca atingia o mínimo configurado."""
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

    key = "ETHUSDT_LONG"
    position_amt = 0.01  # posição pequena: profit_usd seria centavos

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

    # Atinge trailing — deve fechar mesmo com profit_usd baixo (ex: $0.024)
    should_close, reason = bot._check_trailing_stop(
        key, "LONG", 100.0, 100.24, "ETHUSDT", position_amt
    )
    assert should_close is True
    assert "Trailing Stop" in reason


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


def test_analyze_and_trade_accepts_buy_signal_for_standard_profile(monkeypatch):
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

    strategy = SimpleNamespace(generate_trade_setup=lambda **_kwargs: setup)
    bot.strategy_profiles = [
        {
            "name": "scalper_standard",
            "entry_mode": "standard",
            "pairs": ["ETHUSDT"],
            "strategy": strategy,
        }
    ]
    bot.strategy = strategy
    bot.exchange = SimpleNamespace(
        get_klines=lambda **_kwargs: [{"close": 100.0}],
        get_available_balance=lambda: 1000.0,
        get_symbol_info=lambda _symbol: {"minNotional": 5.0},
        get_open_positions=lambda: [],
    )
    bot.risk_manager = SimpleNamespace(can_open_position=lambda _total: True)
    bot.sentiment_mode_enabled = False
    bot.execute_signal_trade = MagicMock(return_value=True)

    result = bot.analyze_and_trade("ETHUSDT", strategy_name="scalper_standard")

    assert result is True
    bot.execute_signal_trade.assert_called_once()
    call_kwargs = bot.execute_signal_trade.call_args.kwargs
    assert call_kwargs["open_long"] is True
    assert call_kwargs["open_short"] is False


def test_analyze_and_trade_passes_risk_profile_for_trend_strategy(monkeypatch):
    bot = _make_light_bot()

    monkeypatch.setattr(config, "USE_DAILY_TARGETS", False)
    monkeypatch.setattr(config, "TIMEFRAME", "5m")
    monkeypatch.setattr(config, "CANDLES_LOOKBACK", 50)

    risk_profile = {
        "stop_loss_min_percent": 0.4,
        "stop_loss_max_percent": 0.6,
        "take_profit_min_percent": 0.8,
        "take_profit_max_percent": 1.2,
        "risk_reward_target": 2.0,
    }

    setup = TradeSetup(
        symbol="ETHUSDT",
        signal=Signal.STRONG_BUY,
        long_size=5.0,
        short_size=5.0,
        entry_price=100.0,
        stop_loss=99.5,
        take_profit=101.0,
        dca_levels=[],
    )

    captured_kwargs = {}

    def _generate_trade_setup(**kwargs):
        captured_kwargs.update(kwargs)
        return setup

    strategy = SimpleNamespace(generate_trade_setup=_generate_trade_setup)
    bot.strategy_profiles = [
        {
            "name": "trend_strong",
            "strategy_type": "trend_signal",
            "entry_mode": "strong_only",
            "risk_profile": dict(risk_profile),
            "pairs": ["ETHUSDT"],
            "strategy": strategy,
        }
    ]
    bot.strategy = strategy
    bot.exchange = SimpleNamespace(
        get_klines=lambda **_kwargs: [{"close": 100.0}],
        get_available_balance=lambda: 1000.0,
        get_symbol_info=lambda _symbol: {"minNotional": 5.0},
        get_open_positions=lambda: [],
    )
    bot.risk_manager = SimpleNamespace(can_open_position=lambda _total: True)
    bot.sentiment_mode_enabled = False
    bot.execute_signal_trade = MagicMock(return_value=True)

    result = bot.analyze_and_trade("ETHUSDT", strategy_name="trend_strong")

    assert result is True
    assert captured_kwargs.get("risk_profile") == risk_profile


def test_hedge_strategy_uses_balanced_risk_profile_with_rr_target():
    strategy = HedgeStrategy()
    risk_profile = {
        "stop_loss_min_percent": 0.4,
        "stop_loss_max_percent": 0.6,
        "take_profit_min_percent": 0.8,
        "take_profit_max_percent": 1.2,
        "risk_reward_target": 2.0,
    }

    stop_loss, take_profit = strategy.calculate_stop_loss_take_profit(
        entry_price=100.0,
        signal=Signal.STRONG_BUY,
        atr=0.2,  # 3*ATR = 0.6% de risco (dentro da faixa)
        risk_profile=risk_profile,
    )

    assert stop_loss == 99.40
    assert take_profit == 101.20


def test_range_scalping_strategy_generates_setup_in_buy_zone(monkeypatch):
    strategy = RangeScalpingStrategy()

    monkeypatch.setattr(config, "TIMEFRAME", "5m")
    monkeypatch.setattr(config, "RANGE_SCALP_MIN_RANGE_PERCENT", 0.8)
    monkeypatch.setattr(config, "RANGE_SCALP_MIN_RANGE_MINUTES", 45)
    monkeypatch.setattr(config, "RANGE_SCALP_MIN_TOUCHES_PER_SIDE", 2)
    monkeypatch.setattr(config, "RANGE_SCALP_EDGE_ZONE_RATIO", 0.30)
    monkeypatch.setattr(config, "RANGE_SCALP_MIN_EDGE_PARTICIPATION", 0.30)
    monkeypatch.setattr(config, "RANGE_SCALP_MAX_VOLUME_RATIO", 1.20)
    monkeypatch.setattr(config, "RANGE_SCALP_MIN_RISK_REWARD", 1.0)
    monkeypatch.setattr(config, "MAX_POSITION_PERCENT", 0.08)

    klines = _make_range_klines(last_close=99.12)
    setup = strategy.generate_trade_setup(
        symbol="ETHUSDT",
        klines=klines,
        available_capital=300.0,
        min_notional=5.0,
    )

    assert setup is not None
    assert setup.signal == Signal.STRONG_BUY
    assert setup.long_size > 0
    assert setup.short_size == 0
    assert setup.metadata.get("strategy_type") == "range_scalping"
    assert setup.stop_loss < setup.entry_price < setup.take_profit


def test_range_scalping_strategy_skips_dead_zone_entries(monkeypatch):
    strategy = RangeScalpingStrategy()

    monkeypatch.setattr(config, "TIMEFRAME", "5m")
    monkeypatch.setattr(config, "RANGE_SCALP_MIN_RANGE_PERCENT", 0.8)
    monkeypatch.setattr(config, "RANGE_SCALP_MIN_RANGE_MINUTES", 45)
    monkeypatch.setattr(config, "RANGE_SCALP_MIN_TOUCHES_PER_SIDE", 2)
    monkeypatch.setattr(config, "RANGE_SCALP_EDGE_ZONE_RATIO", 0.30)
    monkeypatch.setattr(config, "RANGE_SCALP_MIN_EDGE_PARTICIPATION", 0.30)
    monkeypatch.setattr(config, "RANGE_SCALP_MAX_VOLUME_RATIO", 1.20)

    klines = _make_range_klines(last_close=99.50)
    setup = strategy.generate_trade_setup(
        symbol="ETHUSDT",
        klines=klines,
        available_capital=300.0,
        min_notional=5.0,
    )

    assert setup is None


def test_build_analysis_tasks_keeps_strategy_context(monkeypatch):
    bot = _make_light_bot()

    monkeypatch.setattr(config, "DISABLED_PAIRS", [])

    bot.strategy_profiles = [
        {
            "name": "strong_alpha",
            "entry_mode": "strong_only",
            "pairs": ["BTCUSDT"],
            "strategy": object(),
        },
        {
            "name": "normal_beta",
            "entry_mode": "standard",
            "pairs": ["ETHUSDT", "SOLUSDT"],
            "strategy": object(),
        },
    ]

    tasks = bot._build_analysis_tasks()

    assert tasks == [
        {"symbol": "BTCUSDT", "strategy_name": "strong_alpha"},
        {"symbol": "ETHUSDT", "strategy_name": "normal_beta"},
        {"symbol": "SOLUSDT", "strategy_name": "normal_beta"},
    ]


def test_sync_strategy_profiles_preserves_fixed_pairs_ignores_trading_pairs(monkeypatch):
    """Quando o perfil primário já tem pares fixos, TRADING_PAIRS externo não deve
    injetar novos pares no perfil — o perfil é a fonte de verdade."""
    bot = _make_light_bot()
    bot.strategy = SimpleNamespace(generate_trade_setup=lambda **_kwargs: None)
    bot._strategy_engines = {}
    bot.strategy_profiles = []

    monkeypatch.setattr(config, "DISABLED_PAIRS", [])
    monkeypatch.setattr(
        config,
        "STRATEGY_PROFILES",
        [
            {
                "name": "alpha",
                "enabled": True,
                "entry_mode": "strong_only",
                "pairs": ["BTCUSDT"],
            }
        ],
    )
    # TRADING_PAIRS externo (ex.: seleção dinâmica Binance) não deve sobrescrever pares fixos
    monkeypatch.setattr(config, "TRADING_PAIRS", ["BTCUSDT", "XRPUSDT"])

    bot._sync_strategy_profiles_with_trading_pairs(reason="test-sync")

    # Pares fixos preservados, XRPUSDT não injetado
    assert config.STRATEGY_PROFILES[0]["pairs"] == ["BTCUSDT"]
    # TRADING_PAIRS derivado dos perfis (fonte de verdade)
    assert config.TRADING_PAIRS == ["BTCUSDT"]


def test_sync_strategy_profiles_adds_legacy_pairs_when_primary_empty(monkeypatch):
    """Quando o perfil primário não tem pares, TRADING_PAIRS externo é injetado
    (modo dinâmico / compatibilidade com configuração legada)."""
    bot = _make_light_bot()
    bot.strategy = SimpleNamespace(generate_trade_setup=lambda **_kwargs: None)
    bot._strategy_engines = {}
    bot.strategy_profiles = []

    monkeypatch.setattr(config, "DISABLED_PAIRS", [])
    monkeypatch.setattr(
        config,
        "STRATEGY_PROFILES",
        [
            {
                "name": "alpha",
                "enabled": True,
                "entry_mode": "strong_only",
                "pairs": [],
            }
        ],
    )
    monkeypatch.setattr(config, "TRADING_PAIRS", ["BTCUSDT", "XRPUSDT"])

    bot._sync_strategy_profiles_with_trading_pairs(reason="test-sync")

    assert config.STRATEGY_PROFILES[0]["pairs"] == ["BTCUSDT", "XRPUSDT"]
    assert config.TRADING_PAIRS == ["BTCUSDT", "XRPUSDT"]


def test_reload_strategy_profiles_instantiates_range_engine(monkeypatch):
    bot = _make_light_bot()
    bot.strategy = SimpleNamespace(generate_trade_setup=lambda **_kwargs: None)
    bot._strategy_engines = {}
    bot.strategy_profiles = []

    monkeypatch.setattr(config, "DISABLED_PAIRS", [])
    monkeypatch.setattr(config, "TRADING_PAIRS", ["ETHUSDT", "XRPUSDT"])
    monkeypatch.setattr(
        config,
        "STRATEGY_PROFILES",
        [
            {
                "name": "trend",
                "enabled": True,
                "strategy_type": "trend_signal",
                "entry_mode": "strong_only",
                "pairs": ["ETHUSDT"],
            },
            {
                "name": "range",
                "enabled": True,
                "strategy_type": "range_scalping",
                "entry_mode": "strong_only",
                "pairs": ["XRPUSDT"],
            },
        ],
    )

    bot._reload_strategy_profiles(reason="test-range-engine")

    range_profile = next(
        profile for profile in bot.strategy_profiles if profile["name"] == "range"
    )
    assert range_profile["strategy_type"] == "range_scalping"
    assert isinstance(range_profile["strategy"], RangeScalpingStrategy)


def test_reload_strategy_profiles_preserves_trend_risk_profile(monkeypatch):
    bot = _make_light_bot()
    bot.strategy = SimpleNamespace(generate_trade_setup=lambda **_kwargs: None)
    bot._strategy_engines = {}
    bot.strategy_profiles = []

    monkeypatch.setattr(config, "DISABLED_PAIRS", [])
    monkeypatch.setattr(config, "TRADING_PAIRS", ["BTCUSDT"])
    monkeypatch.setattr(
        config,
        "STRATEGY_PROFILES",
        [
            {
                "name": "trend_strong",
                "enabled": True,
                "strategy_type": "trend_signal",
                "entry_mode": "strong_only",
                "pairs": ["BTCUSDT"],
                "risk_profile": {
                    "stop_loss_min_percent": 0.4,
                    "stop_loss_max_percent": 0.6,
                    "take_profit_min_percent": 0.8,
                    "take_profit_max_percent": 1.2,
                    "risk_reward_target": 2.0,
                },
            }
        ],
    )

    bot._reload_strategy_profiles(reason="test-trend-risk-profile")

    assert bot.strategy_profiles[0]["name"] == "trend_strong"
    assert bot.strategy_profiles[0]["risk_profile"]["risk_reward_target"] == 2.0
    assert config.STRATEGY_PROFILES[0]["risk_profile"]["stop_loss_min_percent"] == 0.4


def test_sync_strategy_profiles_excludes_reserved_pairs_from_dynamic_primary(monkeypatch):
    bot = _make_light_bot()
    bot.strategy = SimpleNamespace(generate_trade_setup=lambda **_kwargs: None)
    bot._strategy_engines = {}
    bot.strategy_profiles = []

    monkeypatch.setattr(config, "DISABLED_PAIRS", [])
    monkeypatch.setattr(
        config,
        "STRATEGY_PROFILES",
        [
            {
                "name": "trend_strong",
                "enabled": True,
                "strategy_type": "trend_signal",
                "entry_mode": "strong_only",
                "pairs": [],
            },
            {
                "name": "range_scalp_v1",
                "enabled": True,
                "strategy_type": "range_scalping",
                "entry_mode": "strong_only",
                "pairs": ["DOGEUSDT", "XRPUSDT"],
            },
        ],
    )
    monkeypatch.setattr(config, "TRADING_PAIRS", ["BTCUSDT", "DOGEUSDT", "ETHUSDT", "XRPUSDT"])

    bot._sync_strategy_profiles_with_trading_pairs(
        reason="test-dynamic-primary",
        primary_pairs=["BTCUSDT", "DOGEUSDT", "ETHUSDT", "XRPUSDT"],
    )

    primary = config.STRATEGY_PROFILES[0]
    secondary = config.STRATEGY_PROFILES[1]

    assert primary["pairs"] == ["BTCUSDT", "ETHUSDT"]
    assert secondary["pairs"] == ["DOGEUSDT", "XRPUSDT"]
    assert config.TRADING_PAIRS == ["BTCUSDT", "ETHUSDT", "DOGEUSDT", "XRPUSDT"]


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

    all_symbols = ["XRPUSDT", "ETHUSDT", "ADAUSDT"]
    fake_tickers = {s: {"quoteVolume": "1000000000", "lastPrice": "1.0"} for s in all_symbols}

    bot.exchange = SimpleNamespace(
        get_all_tickers_24h=lambda: fake_tickers,
        get_all_funding_rates=lambda: {},
    )

    monkeypatch.setattr(config, "DISABLED_PAIRS", ["ETHUSDT"])
    monkeypatch.setattr(config, "BINANCE_COIN_LIST", ["OLDUSDT"])
    monkeypatch.setattr(config, "MIN_VOLUME_24H_USD", 0)

    score_map = {
        "ADAUSDT": 90.0,
        "XRPUSDT": 70.0,
        "ETHUSDT": 99.0,  # desabilitado
    }

    class PairSelectorStub:
        def get_all_futures_pairs(self):
            return all_symbols

        def get_pair_metrics(self, symbol, prefetched_ticker=None, prefetched_funding_rate=None):
            return {"symbol": symbol}

        def score_pair(self, metrics):
            return score_map.get(metrics["symbol"], 0.0)

    bot.pair_selector = PairSelectorStub()

    best = bot.sort_binance_coins_by_score(2)

    assert best == ["ADAUSDT", "XRPUSDT"]
    assert config.BINANCE_COIN_LIST == ["XRPUSDT", "ETHUSDT", "ADAUSDT"]


def test_sort_binance_coins_by_score_keeps_previous_universe_on_refresh_failure(monkeypatch):
    bot = _make_light_bot()

    all_symbols = ["BNBUSDT", "XRPUSDT"]
    fake_tickers = {s: {"quoteVolume": "1000000000", "lastPrice": "1.0"} for s in all_symbols}

    bot.exchange = SimpleNamespace(
        get_all_tickers_24h=lambda: fake_tickers,
        get_all_funding_rates=lambda: {},
    )

    monkeypatch.setattr(config, "DISABLED_PAIRS", [])
    monkeypatch.setattr(config, "BINANCE_COIN_LIST", ["BNBUSDT", "XRPUSDT"])
    monkeypatch.setattr(config, "MIN_VOLUME_24H_USD", 0)

    score_map = {"BNBUSDT": 80.0, "XRPUSDT": 60.0}

    class PairSelectorStub:
        def get_all_futures_pairs(self):
            return []

        def get_pair_metrics(self, symbol, prefetched_ticker=None, prefetched_funding_rate=None):
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

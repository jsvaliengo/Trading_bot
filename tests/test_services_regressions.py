from types import SimpleNamespace

from trading_bot.services.pair_selector import PairSelector
from trading_bot.services.telegram_commands import TelegramCommandHandler


def _normalize_pair_symbol(symbol: str) -> str:
    token = str(symbol or "").strip().upper()
    token = token.replace("/", "").replace("-", "").replace("_", "")
    if not token:
        return ""
    if token.endswith("USDT"):
        return token
    return f"{token}USDT"


def _build_handler_with_full_stubs():
    events = []

    class ExchangeStub:
        def __init__(self):
            self.leverage_calls = []
            self.order_calls = []

        def get_account_info(self):
            return {
                "wallet_balance": 1000.0,
                "available_balance": 850.0,
                "unrealized_pnl": 12.5,
                "margin_balance": 1012.5,
            }

        def get_open_positions(self):
            return [
                {
                    "symbol": "ETHUSDT",
                    "side": "LONG",
                    "entry_price": 100.0,
                    "quantity": 0.1,
                }
            ]

        def get_symbol_price(self, symbol):
            return 102.0

        def get_daily_pnl_from_binance(self):
            return {
                "realized_pnl": 5.5,
                "funding_fee": -0.2,
                "commission": -0.3,
                "total": 5.0,
            }

        def set_leverage(self, symbol, leverage):
            self.leverage_calls.append((symbol, leverage))
            return True

        def place_market_order(self, symbol, side, position_side, quantity):
            self.order_calls.append((symbol, side, position_side, quantity))
            return {"orderId": len(self.order_calls)}

    exchange = ExchangeStub()
    config = SimpleNamespace(
        TELEGRAM_USER_ID=None,
        LEVERAGE=10,
        TAKE_PROFIT_PERCENT=2.0,
        STOP_LOSS_PERCENT=1.0,
        USE_INDIVIDUAL_STOP_LOSS=True,
        TRAILING_ACTIVATION_PERCENT=0.5,
        TRAILING_DISTANCE_PERCENT=0.25,
        SENTIMENT_TIMEFRAME="1h",
        SENTIMENT_CANDLES_LOOKBACK=120,
        SENTIMENT_MIN_SCORE=2,
        SENTIMENT_MIN_MOMENTUM_PERCENT=0.2,
        DOUBLE_FIRST_LONG_ENABLED=True,
        DOUBLE_FIRST_SHORT_ENABLED=True,
        DOUBLE_FIRST_MULTIPLIER=2.0,
        DOUBLE_FIRST_MAX_MARGIN_USDT=15.0,
        DOUBLE_FIRST_SCOPE="all",
        USE_DAILY_TARGETS=False,
        TRADING_PAIRS=["ETHUSDT", "BTCUSDT"],
        BINANCE_COIN_LIST=["ETHUSDT", "BTCUSDT", "SOLUSDT"],
        DISABLED_PAIRS=[],
        USE_BINANCE_STRATEGY=True,
        AUTO_SELECT_PAIRS=False,
        MAX_POSITION_PERCENT=0.1,
        DAILY_PERFORMANCE_REPORT_ENABLED=True,
        DAILY_PERFORMANCE_REPORT_HOUR_BRT=23,
        DAILY_PERFORMANCE_REPORT_MINUTE_BRT=55,
        DAILY_PERFORMANCE_REPORT_LOOKBACK_HOURS=24,
        normalize_pair_symbol=_normalize_pair_symbol,
    )

    def save_state():
        events.append(("save_state",))
        return True

    def refresh_trading_pairs(trigger_reason=""):
        old_pairs = list(config.TRADING_PAIRS)
        disabled = set(config.DISABLED_PAIRS)
        config.TRADING_PAIRS = [p for p in config.BINANCE_COIN_LIST if p not in disabled]
        events.append(("refresh_trading_pairs", trigger_reason))
        old_set = set(old_pairs)
        new_set = set(config.TRADING_PAIRS)
        return {
            "old_pairs": old_pairs,
            "new_pairs": list(config.TRADING_PAIRS),
            "added_pairs": sorted(new_set - old_set),
            "removed_pairs": sorted(old_set - new_set),
        }

    def send_portfolio_evolution():
        events.append(("send_portfolio_evolution",))
        return True

    def send_trades_report():
        events.append(("send_trades_report",))
        return True

    def send_api_health_report(force=False):
        events.append(("send_api_health_report", bool(force)))
        return True

    def send_daily_performance_report(force=False):
        events.append(("send_daily_performance_report", bool(force)))
        return True

    def get_lock_info():
        return {
            "lock_acquired": True,
            "lock_file": "/tmp/trading_bot.lock",
            "holder_info": "pid:123",
            "current_pid": 123,
            "bot_running": True,
            "bot_paused": False,
        }

    def set_sentiment_mode(enabled, persist=True):
        bot.sentiment_mode_enabled = bool(enabled)
        events.append(("set_sentiment_mode", bool(enabled), bool(persist)))
        return bot.sentiment_mode_enabled

    def get_sentiment_snapshot(symbol, force_refresh=False):
        events.append(("get_sentiment_snapshot", symbol, bool(force_refresh)))
        return {
            "symbol": symbol,
            "bias": "BULLISH",
            "direction": "LONG_ONLY",
            "score": 3,
            "rsi": 61.3,
            "momentum_pct": 1.1,
            "timeframe": "1h",
            "reason": "trend",
            "updated_at": "2026-03-06T10:00:00+00:00",
        }

    bot = SimpleNamespace(
        running=True,
        paused=False,
        total_pnl=9.5,
        trades_win_count=3,
        trades_loss_count=1,
        exchange=exchange,
        binance_strategy={"capital_range": "0-1k", "order_size": 5.0, "num_coins": 3},
        sentiment_mode_enabled=False,
        save_state=save_state,
        refresh_trading_pairs=refresh_trading_pairs,
        send_portfolio_evolution=send_portfolio_evolution,
        send_trades_report=send_trades_report,
        send_api_health_report=send_api_health_report,
        send_daily_performance_report=send_daily_performance_report,
        get_lock_info=get_lock_info,
        set_sentiment_mode=set_sentiment_mode,
        get_sentiment_snapshot=get_sentiment_snapshot,
    )

    handler = TelegramCommandHandler(token="token", chat_id="123")
    handler.set_bot_reference(bot, config)
    handler._get_usd_brl_rate = lambda: 5.0

    messages = []
    handler.send_message = lambda text: messages.append(text) or True
    return handler, bot, config, messages, events


def _mk_update(text: str, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": "123"},
            "from": {"id": 999},
            "text": text,
        },
    }


def _with_bot_mention(command_line: str) -> str:
    parts = str(command_line).split()
    if not parts:
        return command_line
    return " ".join([f"{parts[0]}@MeuBot", *parts[1:]])


def test_dispatches_all_registered_commands_with_bot_mention():
    handler = TelegramCommandHandler(token="token", chat_id="123")
    called = []

    command_names = list(handler.commands.keys())
    for command in command_names:
        handler.commands[command] = lambda args, cmd=command: called.append((cmd, args))

    for idx, command in enumerate(command_names, start=1):
        text = _with_bot_mention(f"{command} arg{idx}")
        handler._process_update(_mk_update(text, update_id=idx))

    assert len(called) == len(command_names)
    assert [item[0] for item in called] == command_names
    assert called[0][1] == ["arg1"]


def test_process_update_notifies_telegram_when_command_raises():
    handler = TelegramCommandHandler(token="token", chat_id="123")
    messages = []
    handler.send_message = lambda text: messages.append(text) or True
    handler.commands["/status"] = lambda _args: (_ for _ in ()).throw(RuntimeError("boom"))

    handler._process_update(_mk_update("/status", update_id=77))

    assert any("Erro ao executar comando" in text for text in messages)


def test_all_registered_commands_have_smoke_path():
    command_cases = [
        ("/start", lambda _handler, bot, _config: setattr(bot, "paused", True)),
        ("/stop", None),
        ("/pause", None),
        ("/resume", lambda _handler, bot, _config: setattr(bot, "paused", True)),
        ("/status", None),
        ("/portfolio", None),
        ("/trades", None),
        ("/lockinfo", None),
        ("/apihealth", None),
        ("/dailyreport now", None),
        ("/sentiment SOL", None),
        ("/config", None),
        ("/positions", None),
        ("/coins disable SOL", None),
        ("/balance", None),
        ("/leverage 20", None),
        ("/ordersize 6", None),
        ("/tp 3", None),
        ("/sl 1", None),
        ("/trailing 0.7 0.3", None),
        ("/closeall confirm", None),
        ("/help", None),
    ]

    for idx, (command_line, pre_setup) in enumerate(command_cases, start=1):
        handler, bot, config, messages, events = _build_handler_with_full_stubs()
        if pre_setup:
            pre_setup(handler, bot, config)

        handler._process_update(_mk_update(_with_bot_mention(command_line), update_id=1000 + idx))

        assert not any("Comando desconhecido" in text for text in messages), command_line
        assert not any("Erro ao executar comando" in text for text in messages), command_line
        assert messages or events, command_line


def test_pair_selector_accounts_fixed_pairs_capital_before_dynamic_selection():
    config = SimpleNamespace(
        FIXED_PAIRS=["FIXEDUSDT"],
        MAX_TRADING_PAIRS=2,
        PAIR_SELECTION_WEIGHTS={
            "spread": 35,
            "volume": 30,
            "volatility": 20,
            "trend": 10,
            "funding": 5,
        },
        MIN_VOLUME_24H_USD=1.0,
        MAX_SPREAD_PERCENT=1.0,
        MIN_VOLATILITY_PERCENT=0.0,
        MAX_MIN_NOTIONAL=100.0,
        AUTO_SELECT_PAIRS=True,
        PAIR_UPDATE_INTERVAL_MINUTES=60,
    )

    class ExchangeStub:
        def get_exchange_info(self):
            return {
                "symbols": [
                    {
                        "symbol": "FIXEDUSDT",
                        "contractType": "PERPETUAL",
                        "status": "TRADING",
                    },
                    {
                        "symbol": "DYNUSDT",
                        "contractType": "PERPETUAL",
                        "status": "TRADING",
                    },
                ]
            }

        def get_ticker_24h(self, symbol):
            return {"quoteVolume": "1000000", "lastPrice": "100"}

        def get_order_book(self, symbol, limit=5):
            return {"bids": [["100", "1"]], "asks": [["100.1", "1"]]}

        def get_klines_raw(self, symbol, interval, limit=24):
            rows = []
            for i in range(limit):
                rows.append([0, 0, 0, 0, 100 + i * 0.1])
            return rows

        def get_funding_rate(self, symbol):
            return {"rate_percent": 0.0}

        def get_symbol_info(self, symbol):
            if symbol == "FIXEDUSDT":
                return {"minNotional": 10.0}
            return {"minNotional": 5.0}

        def get_available_balance(self):
            return 30.0

    selector = PairSelector(exchange=ExchangeStub(), config=config)
    selected, _scores = selector.select_best_pairs(available_capital=30.0)

    # FIXEDUSDT consome ~27.50 de capital mínimo no cálculo.
    # Com isso, não sobra capital para incluir o par dinâmico.
    assert selected == ["FIXEDUSDT"]


def test_pair_selector_skips_disabled_pairs():
    config = SimpleNamespace(
        FIXED_PAIRS=[],
        DISABLED_PAIRS=["DYNUSDT"],
        MAX_TRADING_PAIRS=1,
        PAIR_SELECTION_WEIGHTS={
            "spread": 35,
            "volume": 30,
            "volatility": 20,
            "trend": 10,
            "funding": 5,
        },
        MIN_VOLUME_24H_USD=1.0,
        MAX_SPREAD_PERCENT=1.0,
        MIN_VOLATILITY_PERCENT=0.0,
        MAX_MIN_NOTIONAL=100.0,
        AUTO_SELECT_PAIRS=True,
        PAIR_UPDATE_INTERVAL_MINUTES=60,
    )

    class ExchangeStub:
        def get_exchange_info(self):
            return {
                "symbols": [
                    {
                        "symbol": "DYNUSDT",
                        "contractType": "PERPETUAL",
                        "status": "TRADING",
                    },
                    {
                        "symbol": "ENABLEDUSDT",
                        "contractType": "PERPETUAL",
                        "status": "TRADING",
                    },
                ]
            }

        def get_ticker_24h(self, symbol):
            return {"quoteVolume": "1000000", "lastPrice": "100"}

        def get_order_book(self, symbol, limit=5):
            return {"bids": [["100", "1"]], "asks": [["100.1", "1"]]}

        def get_klines_raw(self, symbol, interval, limit=24):
            rows = []
            for i in range(limit):
                rows.append([0, 0, 0, 0, 100 + i * 0.1])
            return rows

        def get_funding_rate(self, symbol):
            return {"rate_percent": 0.0}

        def get_symbol_info(self, symbol):
            return {"minNotional": 5.0}

        def get_available_balance(self):
            return 100.0

    selector = PairSelector(exchange=ExchangeStub(), config=config)
    selected, _scores = selector.select_best_pairs(available_capital=100.0)

    assert selected == ["ENABLEDUSDT"]
    assert "DYNUSDT" not in selected


def test_stop_force_reports_partial_close_failures():
    handler = TelegramCommandHandler(token="token", chat_id="123")

    class ExchangeStub:
        def get_open_positions(self):
            return [
                {"symbol": "ETHUSDT", "side": "LONG", "quantity": 0.1},
                {"symbol": "BNBUSDT", "side": "SHORT", "quantity": 0.2},
            ]

        def place_market_order(self, symbol, side, position_side, quantity):
            if symbol == "ETHUSDT":
                return {"orderId": 1}
            return None

    bot = SimpleNamespace(running=True, exchange=ExchangeStub())
    handler.set_bot_reference(bot, SimpleNamespace())

    messages = []
    handler.send_message = lambda text: messages.append(text) or True

    handler.cmd_stop(["force"])

    assert bot.running is False
    assert len(messages) >= 2
    final_message = messages[-1]
    assert "FALHAS AO FECHAR POSIÇÕES" in final_message
    assert "1/2" in final_message


def test_dailyreport_command_toggles_auto_and_sends_now():
    handler = TelegramCommandHandler(token="token", chat_id="123")

    calls = []
    bot = SimpleNamespace(
        send_daily_performance_report=lambda force=False: calls.append(force) or True
    )
    config = SimpleNamespace(
        DAILY_PERFORMANCE_REPORT_ENABLED=True,
        DAILY_PERFORMANCE_REPORT_HOUR_BRT=23,
        DAILY_PERFORMANCE_REPORT_MINUTE_BRT=55,
        DAILY_PERFORMANCE_REPORT_LOOKBACK_HOURS=24,
    )
    handler.set_bot_reference(bot, config)

    messages = []
    handler.send_message = lambda text: messages.append(text) or True

    handler.cmd_dailyreport(["off"])
    assert config.DAILY_PERFORMANCE_REPORT_ENABLED is False

    handler.cmd_dailyreport(["on"])
    assert config.DAILY_PERFORMANCE_REPORT_ENABLED is True

    handler.cmd_dailyreport(["now"])
    assert calls == [True]


def test_start_does_not_fake_restart_when_process_is_stopped():
    handler = TelegramCommandHandler(token="token", chat_id="123")

    bot = SimpleNamespace(running=False, paused=True)
    handler.set_bot_reference(bot, SimpleNamespace())

    messages = []
    handler.send_message = lambda text: messages.append(text) or True

    handler.cmd_start([])

    assert bot.running is False
    assert bot.paused is True
    assert messages
    assert "não reinicia o processo" in messages[-1]


def test_closeall_reports_partial_failures():
    handler = TelegramCommandHandler(token="token", chat_id="123")

    class ExchangeStub:
        def get_open_positions(self):
            return [
                {"symbol": "ETHUSDT", "side": "LONG", "quantity": 0.1},
                {"symbol": "XRPUSDT", "side": "SHORT", "quantity": 5.0},
            ]

        def place_market_order(self, symbol, side, position_side, quantity):
            if symbol == "ETHUSDT":
                return {"orderId": 1}
            return None

    bot = SimpleNamespace(exchange=ExchangeStub())
    handler.set_bot_reference(bot, SimpleNamespace())

    messages = []
    handler.send_message = lambda text: messages.append(text) or True

    handler.cmd_close_all(["confirm"])

    assert len(messages) >= 2
    final_message = messages[-1]
    assert "FECHAMENTO PARCIAL" in final_message
    assert "1/2" in final_message
    assert "Falhas" in final_message


def test_coins_command_disable_enable_and_add_pairs():
    handler = TelegramCommandHandler(token="token", chat_id="123")

    config = SimpleNamespace(
        TRADING_PAIRS=["ETHUSDT", "XRPUSDT", "SOLUSDT"],
        DISABLED_PAIRS=[],
        BINANCE_COIN_LIST=["ETHUSDT", "XRPUSDT", "SOLUSDT"],
        USE_BINANCE_STRATEGY=False,
        AUTO_SELECT_PAIRS=False,
    )
    bot = SimpleNamespace(save_state=lambda: True)
    handler.set_bot_reference(bot, config)

    messages = []
    handler.send_message = lambda text: messages.append(text) or True

    handler.cmd_coins(["disable", "ETH,SOL"])
    assert set(config.DISABLED_PAIRS) == {"ETHUSDT", "SOLUSDT"}
    assert config.TRADING_PAIRS == ["XRPUSDT"]

    handler.cmd_coins(["enable", "eth"])
    assert set(config.DISABLED_PAIRS) == {"SOLUSDT"}
    assert config.TRADING_PAIRS == ["XRPUSDT"]

    handler.cmd_coins(["add", "matic"])
    assert "MATICUSDT" in config.BINANCE_COIN_LIST
    assert "MATICUSDT" in config.TRADING_PAIRS


def test_process_update_accepts_command_with_bot_mention_suffix():
    handler = TelegramCommandHandler(token="token", chat_id="123")

    called = []
    handler.commands["/coins"] = lambda args: called.append(args)
    handler.send_message = lambda text: (_ for _ in ()).throw(AssertionError(text))

    update = {
        "update_id": 1,
        "message": {
            "chat": {"id": "123"},
            "from": {"id": 999},
            "text": "/coins@MeuBot disable ETH",
        },
    }

    handler._process_update(update)

    assert called == [["disable", "ETH"]]


def test_sentiment_command_toggles_mode_and_supports_normal_alias():
    handler = TelegramCommandHandler(token="token", chat_id="123")

    state = {"enabled": False}

    def set_sentiment_mode(enabled, persist=True):
        state["enabled"] = bool(enabled)
        return state["enabled"]

    def get_sentiment_snapshot(symbol, force_refresh=False):
        return {
            "symbol": symbol,
            "bias": "BULLISH",
            "direction": "LONG_ONLY",
            "score": 3,
            "rsi": 62.5,
            "momentum_pct": 1.2,
            "timeframe": "1h",
            "reason": "tendência de alta",
            "updated_at": "2026-03-03T10:00:00+00:00",
        }

    bot = SimpleNamespace(
        sentiment_mode_enabled=False,
        set_sentiment_mode=set_sentiment_mode,
        get_sentiment_snapshot=get_sentiment_snapshot,
    )
    config = SimpleNamespace(
        SENTIMENT_TIMEFRAME="1h",
        SENTIMENT_CANDLES_LOOKBACK=120,
        SENTIMENT_MIN_SCORE=2,
        SENTIMENT_MIN_MOMENTUM_PERCENT=0.2,
        normalize_pair_symbol=lambda s: f"{str(s).upper()}USDT" if not str(s).upper().endswith("USDT") else str(s).upper(),
    )
    handler.set_bot_reference(bot, config)

    messages = []
    handler.send_message = lambda text: messages.append(text) or True

    handler.cmd_sentiment(["on"])
    assert state["enabled"] is True

    handler.cmd_sentiment(["normal"])
    assert state["enabled"] is False

    handler.cmd_sentiment(["SOL"])
    assert "VIÉS DE MERCADO - SOLUSDT" in messages[-1]

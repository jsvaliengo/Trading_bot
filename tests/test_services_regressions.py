from types import SimpleNamespace

from trading_bot.services.pair_selector import PairSelector
from trading_bot.services.telegram_commands import TelegramCommandHandler


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

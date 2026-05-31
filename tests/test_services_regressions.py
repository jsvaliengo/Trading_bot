from types import SimpleNamespace

import requests

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

        def get_open_positions(self, force_refresh=False):
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

        def get_account_balance(self):
            return 900.0

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
        MAX_DRAWDOWN_FROM_PEAK_PERCENT=30.0,
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
        peak_equity=1200.0,
        peak_equity_ts=None,
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


class _ResponseStub:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


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


def test_send_message_retries_after_transient_failure(monkeypatch):
    handler = TelegramCommandHandler(token="token", chat_id="123")
    attempts = {"count": 0}

    def _fake_request(method, url, timeout=None, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise requests.exceptions.RequestException("timeout")
        return _ResponseStub(200, {"ok": True})

    monkeypatch.setattr("trading_bot.services.telegram_commands.time.sleep", lambda _secs: None)
    monkeypatch.setattr(handler._http_session, "request", _fake_request)

    assert handler.send_message("hello") is True
    assert attempts["count"] == 2


def test_get_updates_uses_configured_poll_timeouts(monkeypatch):
    handler = TelegramCommandHandler(token="token", chat_id="123")
    captured = {}

    def _fake_request(method, url, timeout=None, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["timeout"] = timeout
        captured["params"] = kwargs.get("params", {})
        return _ResponseStub(200, {"ok": True, "result": [{"update_id": 11}]})

    monkeypatch.setattr(handler._http_session, "request", _fake_request)

    updates = handler._get_updates()

    assert len(updates) == 1
    assert captured["method"] == "GET"
    assert captured["timeout"] == handler._poll_request_timeout_seconds
    assert captured["params"]["timeout"] == handler._poll_timeout_seconds


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
        ("/drawdown", None),
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
        RVOL_MIN_THRESHOLD=0.8,
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
                rows.append([0, 0, 0, 0, 100 + i * 0.1, 1000.0, 0, 1000.0])
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
        RVOL_MIN_THRESHOLD=0.8,
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
                rows.append([0, 0, 0, 0, 100 + i * 0.1, 1000.0, 0, 1000.0])
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


def _pair_selector_trend_config():
    return SimpleNamespace(
        FIXED_PAIRS=[],
        DISABLED_PAIRS=[],
        MAX_TRADING_PAIRS=2,
        PAIR_SELECTION_WEIGHTS={
            "spread": 35,
            "volume": 30,
            "volatility": 20,
            "trend": 10,
            "funding": 5,
            "rvol": 10,
        },
        MIN_VOLUME_24H_USD=1.0,
        MAX_SPREAD_PERCENT=1.0,
        MIN_VOLATILITY_PERCENT=0.0,
        MAX_MIN_NOTIONAL=100.0,
        RVOL_MIN_THRESHOLD=0.8,
        AUTO_SELECT_PAIRS=True,
        PAIR_UPDATE_INTERVAL_MINUTES=60,
        REGIME_ADX_PERIOD=14,
        REGIME_ADX_TREND_THRESHOLD=25.0,
        REGIME_ADX_RANGE_THRESHOLD=20.0,
    )


def _trend_chop_klines(n=60):
    # Klines com 8 colunas: [open_t, open, high, low, close, vol, close_t, quote_vol].
    # quote_vol uniforme (1000) → RVOL=1.0 nos dois, isolando o efeito de ADX.
    # Tendência limpa: alta monotônica, candles direcionais (abre baixo, fecha alto).
    trend = [[0, 100.0 + i, 101.2 + i, 99.8 + i, 101.0 + i, 1000.0, 0, 1000.0] for i in range(n)]
    # Whippy: oscila 100↔105 sem direção líquida (swings grandes, ADX baixo).
    chop, prev = [], 100.0
    for i in range(n):
        close = 105.0 if i % 2 == 0 else 100.0
        chop.append([0, prev, max(prev, close) + 1.0, min(prev, close) - 1.0, close, 1000.0, 0, 1000.0])
        prev = close
    return trend, chop


def _trend_chop_exchange_stub():
    trend_klines, chop_klines = _trend_chop_klines()

    class ExchangeStub:
        def get_ticker_24h(self, symbol):
            return {"quoteVolume": "1000000", "lastPrice": "100"}

        def get_order_book(self, symbol, limit=5):
            return {"bids": [["100", "1"]], "asks": [["100.1", "1"]]}

        def get_klines_raw(self, symbol, interval, limit=50):
            rows = trend_klines if symbol == "TRENDUSDT" else chop_klines
            return rows[-limit:]

        def get_funding_rate(self, symbol):
            return {"rate_percent": 0.0}

        def get_symbol_info(self, symbol):
            return {"minNotional": 5.0}

    return ExchangeStub()


def test_pair_selector_uses_adx_not_abs_change_for_trend():
    """Par em tendência limpa (ADX alto) deve superar par whippy (Δ24h grande,
    ADX baixo). Antes, abs(Δ24h) tratava o chop volátil como tendência forte."""
    config = _pair_selector_trend_config()
    selector = PairSelector(exchange=_trend_chop_exchange_stub(), config=config)
    m_trend = selector.get_pair_metrics("TRENDUSDT")
    m_chop = selector.get_pair_metrics("CHOPUSDT")

    # ADX separa tendência de chop, mesmo o chop sendo MAIS volátil.
    assert m_trend["adx"] >= config.REGIME_ADX_TREND_THRESHOLD
    assert m_chop["adx"] <= config.REGIME_ADX_RANGE_THRESHOLD
    assert m_chop["volatility"] > m_trend["volatility"]


def test_pair_selector_trend_score_rewards_adx():
    """Isolando os demais componentes, ADX maior → score maior."""
    selector = PairSelector(exchange=None, config=_pair_selector_trend_config())
    base = {
        "volume_24h": 1_000_000.0,
        "volatility": 3.0,
        "funding_rate": 0.0,
        "spread_percent": 0.01,
    }
    strong_trend = {**base, "adx": 30.0}
    weak_trend = {**base, "adx": 10.0}
    assert selector.score_pair(strong_trend) > selector.score_pair(weak_trend)


def test_pair_selector_real_config_ranks_trend_over_whippy():
    """Trava o objetivo com os PESOS REAIS de produção: com a métrica de ADX,
    tendência limpa supera o par whippy no score final (trend=20 > volatility=10).
    Guarda contra reintrodução do viés que priorizava chop volátil."""
    from trading_bot.core.config import config as prod_config

    selector = PairSelector(exchange=_trend_chop_exchange_stub(), config=prod_config)
    m_trend = selector.get_pair_metrics("TRENDUSDT")
    m_chop = selector.get_pair_metrics("CHOPUSDT")

    assert selector.score_pair(m_trend) > selector.score_pair(m_chop)


def test_pair_selector_rvol_score_rewards_fresh_volume():
    """Isolando os demais componentes, RVOL maior (fluxo entrando) → score maior."""
    selector = PairSelector(exchange=None, config=_pair_selector_trend_config())
    base = {
        "volume_24h": 1_000_000.0,
        "volatility": 3.0,
        "adx": 22.0,
        "funding_rate": 0.0,
        "spread_percent": 0.01,
    }
    fresh = {**base, "rvol": 2.5}
    stale = {**base, "rvol": 0.4}
    assert selector.score_pair(fresh) > selector.score_pair(stale)


def test_pair_selector_filters_pairs_below_rvol_floor():
    """Par sem fluxo na hora (RVOL < piso) é descartado na seleção, mesmo
    passando volume/spread/volatilidade. Guarda o gate híbrido de RVOL."""
    config = _pair_selector_trend_config()
    config.RVOL_MIN_THRESHOLD = 1.0
    config.MAX_TRADING_PAIRS = 5

    # ALIVEUSDT: última hora fechada com volume 3x a média → RVOL alto.
    # DEADUSDT: última hora fechada bem abaixo da média → RVOL < 1.0.
    alive = [[0, 100.0 + i, 101.2 + i, 99.8 + i, 101.0 + i, 1000.0, 0, 1000.0] for i in range(60)]
    alive[-2][7] = 3000.0
    dead = [[0, 100.0 + i, 101.2 + i, 99.8 + i, 101.0 + i, 1000.0, 0, 1000.0] for i in range(60)]
    dead[-2][7] = 100.0

    class ExchangeStub:
        def get_exchange_info(self):
            return {
                "symbols": [
                    {"symbol": "ALIVEUSDT", "contractType": "PERPETUAL", "status": "TRADING"},
                    {"symbol": "DEADUSDT", "contractType": "PERPETUAL", "status": "TRADING"},
                ]
            }

        def get_ticker_24h(self, symbol):
            return {"quoteVolume": "1000000", "lastPrice": "100"}

        def get_order_book(self, symbol, limit=5):
            return {"bids": [["100", "1"]], "asks": [["100.1", "1"]]}

        def get_klines_raw(self, symbol, interval, limit=50):
            rows = alive if symbol == "ALIVEUSDT" else dead
            return rows[-limit:]

        def get_funding_rate(self, symbol):
            return {"rate_percent": 0.0}

        def get_symbol_info(self, symbol):
            return {"minNotional": 5.0}

        def get_available_balance(self):
            return 1000.0

    selector = PairSelector(exchange=ExchangeStub(), config=config)
    selected, _scores = selector.select_best_pairs(available_capital=1000.0)

    assert "ALIVEUSDT" in selected
    assert "DEADUSDT" not in selected


def test_oi_change_to_score_buckets():
    """Buckets da spec: <0→0, [0,3)→30, [3,8)→60, [8,15)→85, ≥15→100."""
    f = PairSelector.oi_change_to_score
    assert f(None) == 0.0
    assert f(-1.0) == 0.0
    assert f(0.0) == 30.0
    assert f(2.9) == 30.0
    assert f(3.0) == 60.0
    assert f(7.9) == 60.0
    assert f(8.0) == 85.0
    assert f(14.9) == 85.0
    assert f(15.0) == 100.0
    assert f(40.0) == 100.0


def _oi_config(enabled=True):
    return SimpleNamespace(
        OI_ENABLED=enabled,
        OI_PERIOD="5m",
        OI_LOOKBACK_SAMPLES=6,
    )


def test_get_oi_change_percent_computes_from_hist():
    """ΔOI = (OI_novo - OI_antigo) / OI_antigo × 100, do primeiro ao último sample."""
    selector = PairSelector(exchange=None, config=_oi_config())

    class OIClientStub:
        def futures_open_interest_hist(self, symbol, period, limit):
            return [{"sumOpenInterest": str(v)} for v in (100.0, 102, 104, 106, 108, 110, 112)]

    selector._oi_public_client = OIClientStub()
    change = selector.get_oi_change_percent("FOOUSDT")
    assert change is not None
    assert round(change, 4) == 12.0  # (112-100)/100*100


def test_get_oi_change_percent_returns_none_when_disabled_or_short():
    # Desligado → None (não chama API)
    sel_off = PairSelector(exchange=None, config=_oi_config(enabled=False))
    sel_off._oi_public_client = object()  # não deve ser usado
    assert sel_off.get_oi_change_percent("FOOUSDT") is None

    # Dados insuficientes → None (neutro, não quebra)
    sel = PairSelector(exchange=None, config=_oi_config())

    class ShortStub:
        def futures_open_interest_hist(self, symbol, period, limit):
            return [{"sumOpenInterest": "100"}]

    sel._oi_public_client = ShortStub()
    assert sel.get_oi_change_percent("FOOUSDT") is None


def test_stop_force_reports_partial_close_failures():
    handler = TelegramCommandHandler(token="token", chat_id="123")

    class ExchangeStub:
        def get_open_positions(self, force_refresh=False):
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


def test_drawdown_command_reports_status_and_can_disable():
    handler, bot, config, messages, events = _build_handler_with_full_stubs()

    handler.cmd_drawdown([])
    assert "DRAWDOWN DESDE O PICO" in messages[-1]
    assert "25.00%" in messages[-1]
    assert "NÃO" in messages[-1]

    handler.cmd_drawdown(["off"])

    assert config.MAX_DRAWDOWN_FROM_PEAK_PERCENT == 0.0
    assert events[-1] == ("save_state",)
    assert "DESATIVADA" in messages[-1]


def test_drawdown_command_resets_peak_to_current_balance():
    handler, bot, _config, messages, events = _build_handler_with_full_stubs()

    handler.cmd_drawdown(["reset"])

    assert bot.peak_equity == 900.0
    assert bot.peak_equity_ts is not None
    assert events[-1] == ("save_state",)
    assert "PICO DE EQUITY RESETADO" in messages[-1]


def test_drawdown_command_updates_limit_and_warns_when_still_blocked():
    handler, _bot, config, messages, events = _build_handler_with_full_stubs()

    handler.cmd_drawdown(["20"])

    assert config.MAX_DRAWDOWN_FROM_PEAK_PERCENT == 20.0
    assert events[-1] == ("save_state",)
    assert "continuarão bloqueadas" in messages[-1]


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
        def get_open_positions(self, force_refresh=False):
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


def _build_panic_guard_handler(unrealized_pnls, initial_capital=100.0):
    """Helper para testes do panic guard. Cria handler + bot stub configurado."""
    handler = TelegramCommandHandler(token="token", chat_id="123")

    class ExchangeStub:
        def __init__(self, pnls):
            self._pnls = pnls

        def get_open_positions(self, force_refresh=False):
            return [
                {"symbol": f"PAIR{i}USDT", "side": "LONG", "quantity": 1.0,
                 "unrealized_pnl": pnl}
                for i, pnl in enumerate(self._pnls)
            ]

        def place_market_order(self, symbol, side, position_side, quantity):
            return {"orderId": 1}

    bot = SimpleNamespace(exchange=ExchangeStub(unrealized_pnls),
                         initial_capital=initial_capital)
    handler.set_bot_reference(bot, SimpleNamespace())
    messages = []
    handler.send_message = lambda text: messages.append(text) or True
    return handler, messages


def test_closeall_panic_guard_blocks_when_drawdown_exceeds_threshold():
    """/closeall confirm com drawdown profundo deve exigir frase explícita."""
    # -8% num capital de $100 = unrealized -$8 (acima do default 5%)
    handler, messages = _build_panic_guard_handler([-4.0, -4.0], initial_capital=100.0)

    handler.cmd_close_all(["confirm"])

    assert messages, "deveria ter enviado mensagem"
    blocked = messages[-1]
    assert "PANIC GUARD" in blocked
    assert "eu_sei_o_risco" in blocked
    # Não deve ter executado fechamento
    assert "POSIÇÕES FECHADAS" not in blocked


def test_closeall_panic_guard_allows_when_drawdown_shallow():
    """Drawdown raso (-2%) não dispara panic guard."""
    handler, messages = _build_panic_guard_handler([-1.0, -1.0], initial_capital=100.0)

    handler.cmd_close_all(["confirm"])

    final = messages[-1]
    assert "PANIC GUARD" not in final
    assert "POSIÇÕES FECHADAS" in final or "FECHAMENTO" in final


def test_closeall_force_phrase_bypasses_panic_guard():
    """Frase explícita força fechamento mesmo em drawdown profundo."""
    handler, messages = _build_panic_guard_handler([-15.0, -15.0], initial_capital=100.0)

    handler.cmd_close_all(["eu_sei_o_risco"])

    final = messages[-1]
    assert "PANIC GUARD" not in final
    assert "POSIÇÕES FECHADAS" in final or "FECHAMENTO" in final


def test_closeall_panic_guard_skipped_when_no_initial_capital():
    """Sem initial_capital configurado, panic guard fica inativo (não bloqueia)."""
    handler, messages = _build_panic_guard_handler([-50.0], initial_capital=0.0)

    handler.cmd_close_all(["confirm"])

    final = messages[-1]
    assert "PANIC GUARD" not in final


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


def test_coins_command_rejects_unknown_symbol_and_suggests_closest_pair():
    handler = TelegramCommandHandler(token="token", chat_id="123")

    config = SimpleNamespace(
        TRADING_PAIRS=["HYPEUSDT", "XRPUSDT", "SOLUSDT"],
        DISABLED_PAIRS=["HYPERUSDT"],
        BINANCE_COIN_LIST=["HYPEUSDT", "XRPUSDT", "SOLUSDT"],
        USE_BINANCE_STRATEGY=False,
        AUTO_SELECT_PAIRS=False,
    )
    class ExchangeStub:
        def get_exchange_info(self):
            return {
                "symbols": [
                    {"symbol": "HYPEUSDT", "contractType": "PERPETUAL", "status": "TRADING"},
                    {"symbol": "XRPUSDT", "contractType": "PERPETUAL", "status": "TRADING"},
                    {"symbol": "SOLUSDT", "contractType": "PERPETUAL", "status": "TRADING"},
                ]
            }

    bot = SimpleNamespace(save_state=lambda: True, exchange=ExchangeStub())
    handler.set_bot_reference(bot, config)

    messages = []
    handler.send_message = lambda text: messages.append(text) or True

    handler.cmd_coins(["disable", "HYPER"])

    assert config.DISABLED_PAIRS == []
    assert config.TRADING_PAIRS == ["HYPEUSDT", "XRPUSDT", "SOLUSDT"]
    assert messages
    assert "HYPER" in messages[-1]
    assert "HYPE" in messages[-1]


def test_coins_command_keeps_disabled_pairs_recognized_in_dynamic_binance_mode():
    handler = TelegramCommandHandler(token="token", chat_id="123")

    exchange_symbols = [
        "HYPEUSDT",
        "BTCUSDT",
        "PAXGUSDT",
        "XRPUSDT",
        "SOLUSDT",
        "ZECUSDT",
        "DOGEUSDT",
    ]

    config = SimpleNamespace(
        TRADING_PAIRS=list(exchange_symbols),
        DISABLED_PAIRS=[],
        BINANCE_COIN_LIST=list(exchange_symbols),
        FIXED_PAIRS=[],
        STRATEGY_PROFILES=[],
        USE_BINANCE_STRATEGY=True,
        AUTO_SELECT_PAIRS=False,
    )
    config.normalize_pair_symbol = lambda symbol: (
        (token := str(symbol or "").strip().upper().strip(",;").replace("/", "").replace("-", "").replace("_", ""))
        and (token if token.endswith("USDT") else f"{token}USDT")
    ) or ""

    def normalize_pair_list(pairs):
        normalized = []
        seen = set()
        for item in pairs or []:
            symbol = config.normalize_pair_symbol(item)
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            normalized.append(symbol)
        return normalized

    config.normalize_pair_list = normalize_pair_list
    config.filter_disabled_pairs = lambda pairs: [
        symbol
        for symbol in normalize_pair_list(pairs)
        if symbol not in set(normalize_pair_list(config.DISABLED_PAIRS))
    ]

    class ExchangeStub:
        def get_exchange_info(self):
            return {
                "symbols": [
                    {"symbol": symbol, "contractType": "PERPETUAL", "status": "TRADING"}
                    for symbol in exchange_symbols
                ]
            }

    class BotStub:
        def __init__(self):
            self.exchange = ExchangeStub()

        def save_state(self):
            return True

        def refresh_trading_pairs(self, trigger_reason="manual"):
            # Reproduz o comportamento do modo dinâmico: a lista permitida passa a refletir
            # apenas pares atualmente habilitados.
            config.BINANCE_COIN_LIST = config.filter_disabled_pairs(exchange_symbols)
            config.TRADING_PAIRS = list(config.BINANCE_COIN_LIST)
            return {"new_pairs": list(config.TRADING_PAIRS)}

    bot = BotStub()
    handler.set_bot_reference(bot, config)

    messages = []
    handler.send_message = lambda text: messages.append(text) or True

    handler.cmd_coins(["disable", "HYPE"])
    assert set(config.DISABLED_PAIRS) == {"HYPEUSDT"}
    assert "HYPEUSDT" not in config.TRADING_PAIRS

    handler.cmd_coins(["disable", "BTC"])
    assert set(config.DISABLED_PAIRS) == {"HYPEUSDT", "BTCUSDT"}
    assert "HYPEUSDT" not in config.TRADING_PAIRS
    assert "BTCUSDT" not in config.TRADING_PAIRS

    handler.cmd_coins(["disable", "HYPE", "BTC"])
    assert set(config.DISABLED_PAIRS) == {"HYPEUSDT", "BTCUSDT"}
    assert "Já estavam" in messages[-1]
    assert "HYPE, BTC" in messages[-1]
    assert "não reconhecidos" not in messages[-1].lower()

    handler.cmd_coins(["disable", "HYPE,BTC"])
    assert set(config.DISABLED_PAIRS) == {"HYPEUSDT", "BTCUSDT"}
    assert "HYPE, BTC" in messages[-1]
    assert "não reconhecidos" not in messages[-1].lower()


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

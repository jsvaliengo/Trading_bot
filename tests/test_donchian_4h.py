"""Donchian 4h (forward test, docs/FORWARD_TEST_DONCHIAN_4H.md): paridade com a
pesquisa, sinais, stop, saída, sizing, universo, integração no bot e proteções."""
from __future__ import annotations

import logging
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from trading_bot.core import donchian as d
from trading_bot.core.bot import TradingBot
from trading_bot.core.config import config
from trading_bot.core.trade_store import TradeStore, trading_day_start
from trading_bot.services.kill_switch import KillSwitchMonitor

H4 = d.H4_MS
DAY = d.DAY_MS


def _series(seed=2, n=120):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    return c * (1 + rng.uniform(0, 0.01, n)), c * (1 - rng.uniform(0, 0.01, n)), c


# ───────────── paridade com a pesquisa ─────────────
def test_atr_and_channels_match_research_golden_values():
    """Valores gerados por scripts/backtest_v2/trend4h.py (PR #215) nas mesmas barras."""
    h, lw, c = _series()
    atr = d.atr_wilder(h, lw, c, 20)
    assert np.isnan(atr[18])
    assert atr[[19, 20, 60, 119]].round(10).tolist() == [1.8771303197, 1.9116655915, 2.2457954589, 2.2708066719]
    el, es, xl, xs = d.donchian_signals(h, lw, c, 40, 20)
    assert np.nonzero(el)[0].tolist() == [55, 56, 57]
    assert np.nonzero(es)[0].tolist() == [98, 99, 100, 101]
    assert np.nonzero(xl)[0].tolist() == [29, 97, 98, 99, 100, 101]
    assert np.nonzero(xs)[0].tolist() == [26, 55, 56, 57]


def test_parity_with_research_module_when_available():
    """Roda onde o código da pesquisa existe (branch research/trend-4h, PR #215).
    Na main só valem os valores fixos do teste acima, gerados por ele."""
    t4 = pytest.importorskip("scripts.backtest_v2.trend4h")
    from scripts.backtest_v2.data import Bars
    from scripts.backtest_v2.strategies import DonchianBreakout

    h, lw, _ = _series(seed=11)
    for n in (5, 20, 40):
        np.testing.assert_array_equal(d.prior_extreme(h, n, np.max), DonchianBreakout._prior(h, n, np.max))
        np.testing.assert_array_equal(d.prior_extreme(lw, n, np.min), DonchianBreakout._prior(lw, n, np.min))

    for seed in range(5):
        h, lw, c = _series(seed=seed, n=300)
        np.testing.assert_array_equal(d.atr_wilder(h, lw, c, 20), t4.atr_wilder(h, lw, c, 20))
        b = Bars("X", "4h", np.arange(300, dtype=np.int64) * H4, c, h, lw, c, np.ones(300))
        for ours, theirs in zip(d.donchian_signals(h, lw, c, 40, 20), t4.donchian_signals(b, 40, 20)):
            np.testing.assert_array_equal(ours, theirs)
        daily = {f"S{i}USDT": _daily_np(seed + i) for i in range(4)}
        times = t4.rebalance_times(int(daily["S0USDT"][0, 0]), int(daily["S0USDT"][-1, 0]))
        syms, sc = t4.universe_scores(daily, times)
        for row, t in zip(sc, times):
            ours = {s: d.universe_score(daily[s].tolist(), int(t), min_days=60, vol_days=30) for s in syms}
            theirs = {s: (None if np.isinf(v) else pytest.approx(v)) for s, v in zip(syms, row.tolist())}
            assert ours == theirs


def _daily_np(seed):
    rng = np.random.default_rng(seed)
    n = 120
    a = np.zeros((n, 8))
    a[:, 0] = d.rebalance_start(1_700_000_000_000) + np.arange(n) * DAY
    a[:, 6] = a[:, 7] = rng.uniform(1, 100, n).round(2)
    return a


# ───────────── sinal da última barra ─────────────
def _klines(closes, start=0):
    return [{"timestamp": start + i * H4, "open": x, "high": x + 0.5, "low": x - 0.5, "close": x, "volume": 1.0}
            for i, x in enumerate(closes)]


FLAT = [100.0] * 60


def _sig(closes):
    return d.last_bar_signal(_klines(closes), n=40, m=20, atr_n=20, k=2.0)


def test_entry_long_on_breakout_close_with_k_atr_stop():
    sig = _sig(FLAT + [101.0])          # high anterior = 100,5
    assert sig.entry == "LONG"
    assert sig.stop_pct == pytest.approx(2 * d.atr_wilder(
        np.array(FLAT + [101.0]) + 0.5, np.array(FLAT + [101.0]) - 0.5, np.array(FLAT + [101.0]), 20)[-1] / 101.0)


def test_entry_short_on_breakdown_and_none_without_breakout():
    assert _sig(FLAT + [99.0]).entry == "SHORT"
    assert _sig(FLAT + [100.4]).entry is None
    assert _sig(FLAT[:41] + [101.0]).entry == "LONG"
    assert d.last_bar_signal(_klines(FLAT[:15] + [101.0]), n=10, m=5, atr_n=20, k=2.0).entry is None  # sem ATR


def test_exit_flags_use_the_opposite_20_bar_channel():
    down = _sig(FLAT + [99.0])
    assert (down.exit_long, down.exit_short) == (True, False)
    up = _sig(FLAT + [101.0])
    assert (up.exit_long, up.exit_short) == (False, True)


# ───────────── sizing ─────────────
def _size(min_notional=5.0, precision=3, **kw):
    base = dict(equity=173.0, stop_pct=0.05, price=1.0, info={"minNotional": min_notional, "quantityPrecision": precision},
                open_positions=0, gross_open=0.0, risk_pct=0.25, max_pos_mult=1.0, max_gross_mult=3.0, max_positions=10)
    base.update(kw)
    return d.size_position(**base)


def test_sizing_risks_quarter_percent_of_equity():
    s = _size()
    assert s.skip is None
    assert s.notional == pytest.approx(173.0 * 0.0025 / 0.05)       # $8,65
    assert s.risk_usd == pytest.approx(0.4325)


def test_sizing_caps_per_position_gross_and_count():
    assert _size(stop_pct=0.001).notional == pytest.approx(173.0)             # 1× equity
    assert _size(stop_pct=0.001, gross_open=500.0).notional == pytest.approx(19.0)   # sobra do 3×
    assert _size(gross_open=519.0).skip == "teto bruto 3x"
    assert _size(open_positions=10).skip == "teto de 10 posições"


def test_sizing_min_notional_bumps_up_to_2x_risk_then_skips():
    bumped = _size(stop_pct=0.10, min_notional=5.0)          # alvo $4,33 < $5; risco $0,50 ≤ 2×
    assert (bumped.skip, bumped.notional) == (None, 5.0)
    skipped = _size(stop_pct=0.035, min_notional=50.0)       # BTC testnet: risco $1,75 > 2×$0,43
    assert skipped.skip == "minNotional/step: risco $1.75 = 4.05× alvo $0.43 (qty 50)"


def test_sizing_checks_2x_risk_on_the_final_rounded_qty():
    # minNotional $8,20 sozinho dá 1,90× (passaria); o step inteiro sobe a qty para 2 → $9,40 → 2,17×
    s = _size(stop_pct=0.10, min_notional=8.2, price=4.7, precision=0)
    assert s.skip == "minNotional/step: risco $0.94 = 2.17× alvo $0.43 (qty 2)"
    # QNTUSDT LONG 2026-10-26: entrada 216,48, stop 200,96, equity ~$299 → qty 0,1 arrisca $1,55 (2,08×)
    qnt = _size(equity=299.0, stop_pct=15.52 / 216.48, price=216.48, precision=1)
    assert qnt.skip == "minNotional/step: risco $1.55 = 2.08× alvo $0.75 (qty 0.1)"


def test_sizing_returns_the_final_qty_and_its_risk():
    s = _size(stop_pct=0.10, price=2.6, precision=0)     # alvo $4,33 → qty 1,66 → 2 (step inteiro) → $5,20
    assert (s.skip, s.qty, s.notional, s.risk_usd) == (None, 2.0, pytest.approx(5.2), pytest.approx(0.52))


# ───────────── universo ─────────────
def _daily_rows(t, n, qv=1.0, last_day=None):
    end = (last_day if last_day is not None else t - DAY)
    return [[end - (n - 1 - i) * DAY, 0, 0, 0, 0, 0, end - (n - 1 - i) * DAY + DAY - 1, qv] for i in range(n)]


def test_universe_score_point_in_time_and_eligibility():
    t = d.rebalance_start(1_761_600_000_000)
    rows = _daily_rows(t, 70, qv=2.0) + [[t, 0, 0, 0, 0, 0, t + DAY - 1, 1e12]]   # barra aberta em T: fora
    assert d.universe_score(rows, t, min_days=60, vol_days=30) == 60.0
    assert d.universe_score(_daily_rows(t, 59), t, min_days=60, vol_days=30) is None
    assert d.universe_score(_daily_rows(t, 90, last_day=t - 5 * DAY), t, min_days=60, vol_days=30) is None
    assert d.top_universe({"BUSDT": 5.0, "AUSDT": 5.0, "CUSDT": 9.0}, 2) == ["CUSDT", "AUSDT"]
    assert d.top_universe({"BUSDT": 5.0, "AUSDT": 5.0, "CUSDT": 9.0}, 0) == ["CUSDT", "AUSDT", "BUSDT"]   # 0 = todos
    assert d.rebalance_start(t + 3 * DAY + 7) == t and t % (7 * DAY) == 4 * DAY


def test_refresh_universe_mainnet_volume_intersected_with_testnet_minus_disabled(monkeypatch, caplog):
    t = d.rebalance_start(1_761_600_000_000)
    vols = {"AAAUSDT": 9.0, "BBBUSDT": 8.0, "CCCUSDT": 7.0, "DDDUSDT": 6.0, "USDCUSDT": 99.0}
    perp = lambda s: {"symbol": s, "contractType": "PERPETUAL", "status": "TRADING", "quoteAsset": "USDT"}  # noqa: E731
    ex = SimpleNamespace(
        get_market_exchange_info=lambda: {"symbols": [perp(s) for s in vols]},
        get_klines_raw=MagicMock(side_effect=lambda s, _i, limit: _daily_rows(t, limit - 1, vols[s])),
        get_exchange_info=lambda: {"symbols": [perp("AAAUSDT"), perp("BBBUSDT"), perp("DDDUSDT")]},
    )
    monkeypatch.setattr(config, "DONCHIAN_UNIVERSE_TOP_N", 3)
    monkeypatch.setattr(config, "DISABLED_PAIRS", ["BBBUSDT"])
    runner = d.DonchianRunner(SimpleNamespace(exchange=ex))
    with caplog.at_level(logging.INFO):
        assert runner.refresh_universe(t + DAY) == ["AAAUSDT"]      # CCC fora do testnet, BBB desabilitado
    assert runner.universe_week == t
    assert ex.get_klines_raw.call_args.kwargs["limit"] == 68
    assert any("= 1 pares [AAAUSDT]" in m for m in caplog.messages)


def test_universe_top_n_reads_env_zero_means_all(monkeypatch):
    import importlib
    from trading_bot.core import config as config_module

    assert config.DONCHIAN_UNIVERSE_TOP_N == 30
    monkeypatch.setenv("TRADING_BOT_DONCHIAN_UNIVERSE_TOP_N", "0")
    try:
        assert importlib.reload(config_module).TradingConfig().DONCHIAN_UNIVERSE_TOP_N == 0
    finally:
        monkeypatch.delenv("TRADING_BOT_DONCHIAN_UNIVERSE_TOP_N", raising=False)
        importlib.reload(config_module)   # restaura o default (30) p/ os demais testes

# ───────────── runner: entrada, stop, saída ─────────────
BOUNDARY = 1_761_600_000_000 // H4 * H4


class _Ex:
    def __init__(self, klines, positions=None, fill=100.0):
        self.klines = klines
        self.positions = positions or []
        self.fill = fill
        self.orders, self.stops = [], []
        self.cancel_open_orders = MagicMock(return_value=True)
        self.set_leverage = MagicMock(return_value=True)
        self.close_position = MagicMock()

    def get_open_positions(self, force_refresh=False):
        return list(self.positions)

    def get_klines(self, symbol, interval, limit):
        assert interval == "4h" and limit == d.KLINES_LIMIT
        return self.klines.get(symbol, [])

    def get_account_balance(self):
        return 173.0

    def get_symbol_info(self, _s):
        return {"minNotional": 5.0, "quantityPrecision": 3, "pricePrecision": 4}

    def get_symbol_ticker(self, _s):
        return {"price": "100.0"}

    def place_market_order(self, **kw):
        self.orders.append(kw)
        return {"avgPrice": str(self.fill)}

    def set_stop_loss_take_profit(self, **kw):
        self.stops.append(kw)
        return True


def _bot(ex, known=None):
    bot = TradingBot.__new__(TradingBot)
    bot._init_runtime_state()
    bot._positions_lock = threading.Lock()
    bot.exchange = ex
    bot.known_positions = dict(known or {})
    bot.drawdown_equity = lambda wallet: wallet
    bot.check_daily_targets = lambda: False
    bot.ledger = SimpleNamespace(record_trade_opened=MagicMock())
    bot.telegram = SimpleNamespace(send_trade_alert=MagicMock())
    bot._close_position_with_notification = MagicMock(return_value=True)
    bot.trade_store = None
    return bot


def _bars_ending(closes):
    return _klines(closes, start=BOUNDARY - len(closes) * H4)


@pytest.mark.parametrize("side,closes,stop", [("LONG", FLAT + [101.0], "below"), ("SHORT", FLAT + [99.0], "above")])
def test_runner_enters_on_breakout_close_with_stop_from_fill(side, closes, stop):
    ex = _Ex({"XUSDT": _bars_ending(closes)}, fill=102.0)
    bot = _bot(ex)
    runner = d.DonchianRunner(bot)
    runner.universe, runner.universe_week = ["XUSDT"], d.rebalance_start(BOUNDARY)
    runner.tick(BOUNDARY + 60_000)

    sig = _sig(closes)
    assert ex.orders == [{"symbol": "XUSDT", "side": "BUY" if side == "LONG" else "SELL",
                          "position_side": side, "quantity": ex.orders[0]["quantity"]}]
    assert ex.orders[0]["quantity"] == pytest.approx(round(173 * 0.0025 / sig.stop_pct / 100.0, 3))
    expected = 102.0 * (1 - sig.stop_pct) if side == "LONG" else 102.0 * (1 + sig.stop_pct)
    assert ex.stops == [{"symbol": "XUSDT", "position_side": side, "stop_loss_price": round(expected, 4),
                         "take_profit_price": None}]
    assert (ex.stops[0]["stop_loss_price"] < 102.0) == (stop == "below")
    meta = bot.known_positions[f"XUSDT_{side}"]
    assert (meta["strategy_type"], meta["custom_stop_loss"], meta["custom_take_profit"]) == (
        "donchian_4h", round(expected, 4), None)
    ex.cancel_open_orders.assert_called_once_with("XUSDT")

    runner.tick(BOUNDARY + 120_000)          # mesma barra: não processa de novo
    assert len(ex.orders) == 1


def test_runner_no_entry_without_breakout_or_when_late_or_paused():
    ex = _Ex({"XUSDT": _bars_ending(FLAT + [100.2]), "YUSDT": _bars_ending(FLAT + [101.0])})
    bot = _bot(ex)
    runner = d.DonchianRunner(bot)
    runner.universe, runner.universe_week = ["XUSDT"], d.rebalance_start(BOUNDARY)
    runner.tick(BOUNDARY + 60_000)
    assert ex.orders == []                   # XUSDT sem rompimento; YUSDT fora do universo

    late = d.DonchianRunner(bot)
    late.universe, late.universe_week = ["YUSDT"], d.rebalance_start(BOUNDARY)
    late.tick(BOUNDARY + 31 * 60_000)
    bot.paused = True
    paused = d.DonchianRunner(bot)
    paused.universe, paused.universe_week = ["YUSDT"], d.rebalance_start(BOUNDARY)
    paused.tick(BOUNDARY + 60_000)
    assert ex.orders == []


def test_runner_skips_trade_when_min_notional_forces_over_2x_risk(caplog):
    ex = _Ex({"BTCUSDT": _bars_ending(FLAT + [101.0])})
    ex.get_symbol_info = lambda _s: {"minNotional": 50.0, "quantityPrecision": 3, "pricePrecision": 1}
    runner = d.DonchianRunner(_bot(ex))
    runner.universe, runner.universe_week = ["BTCUSDT"], d.rebalance_start(BOUNDARY)
    with caplog.at_level(logging.WARNING):
        runner.tick(BOUNDARY + 60_000)
    assert ex.orders == []
    assert "⏭️ Donchian: BTCUSDT LONG pulado — minNotional/step: risco $" in caplog.text


def test_runner_logs_risk_of_the_rounded_qty(caplog):
    ex = _Ex({"XUSDT": _bars_ending(FLAT + [101.0])})
    ex.get_symbol_info = lambda _s: {"minNotional": 5.0, "quantityPrecision": 1, "pricePrecision": 4}
    runner = d.DonchianRunner(_bot(ex))
    runner.universe, runner.universe_week = ["XUSDT"], d.rebalance_start(BOUNDARY)
    with caplog.at_level(logging.INFO):
        runner.tick(BOUNDARY + 60_000)
    stop_pct = _sig(FLAT + [101.0]).stop_pct
    qty = round(173 * 0.0025 / stop_pct / 100.0, 1)
    assert ex.orders[0]["quantity"] == qty
    assert f"qty {qty} (~${qty * 100:.2f}, risco ~${qty * 100 * stop_pct:.2f}, stop" in caplog.text


def test_runner_respects_10_position_cap():
    held = [{"symbol": f"H{i}USDT", "side": "LONG", "quantity": 0.01, "entry_price": 100.0, "mark_price": 100.0}
            for i in range(10)]
    ex = _Ex({"XUSDT": _bars_ending(FLAT + [101.0])}, positions=held)
    runner = d.DonchianRunner(_bot(ex))
    runner.universe, runner.universe_week = ["XUSDT"], d.rebalance_start(BOUNDARY)
    runner.tick(BOUNDARY + 60_000)
    assert ex.orders == []


def _donchian_long(symbol="XUSDT"):
    return {f"{symbol}_LONG": {"symbol": symbol, "side": "LONG", "entry_price": 100.0, "quantity": 0.1,
                               "strategy_name": "donchian_4h", "strategy_type": "donchian_4h",
                               "custom_stop_loss": 95.0, "custom_take_profit": None}}


def test_runner_exits_long_on_close_below_prior_20_low():
    pos = {"symbol": "XUSDT", "side": "LONG", "entry_price": 100.0, "quantity": 0.1, "mark_price": 99.0}
    ex = _Ex({"XUSDT": _bars_ending(FLAT + [99.0])}, positions=[pos])
    bot = _bot(ex, known=_donchian_long())
    runner = d.DonchianRunner(bot)
    runner.universe_week = d.rebalance_start(BOUNDARY)
    runner.tick(BOUNDARY + 60_000)
    bot._close_position_with_notification.assert_called_once_with(pos, "Donchian: saída no canal de 20")
    ex.cancel_open_orders.assert_called_with("XUSDT")
    assert "XUSDT_LONG" not in bot.known_positions


def test_runner_holds_long_inside_channel_and_exits_on_stale_data():
    pos = {"symbol": "XUSDT", "side": "LONG", "entry_price": 100.0, "quantity": 0.1, "mark_price": 100.0}
    ex = _Ex({"XUSDT": _bars_ending(FLAT + [100.2])}, positions=[pos])
    bot = _bot(ex, known=_donchian_long())
    runner = d.DonchianRunner(bot)
    runner.universe_week = d.rebalance_start(BOUNDARY)
    runner.tick(BOUNDARY + 60_000)
    bot._close_position_with_notification.assert_not_called()

    ex.klines["XUSDT"] = _klines(FLAT, start=BOUNDARY - 10 * H4 - len(FLAT) * H4)   # feed parado há ~40h
    runner.tick(BOUNDARY + H4 + 60_000)
    bot._close_position_with_notification.assert_called_once_with(pos, "Donchian: dados parados")


# ───────────── monitor: trailing/TP não tocam Donchian; pullback igual ─────────────
def _monitor_bot(known, mark):
    bot = TradingBot.__new__(TradingBot)
    bot._init_runtime_state()
    bot._positions_lock = threading.Lock()
    pos = {"symbol": "XUSDT", "side": "LONG", "entry_price": 100.0, "quantity": 0.1,
           "unrealized_pnl": 1.0, "mark_price": mark}
    bot.exchange = SimpleNamespace(get_open_positions=lambda *a, **kw: [dict(pos)])
    bot._close_position_with_notification = MagicMock(return_value=True)
    bot._process_binance_closed_position = MagicMock()
    bot.known_positions = known
    return bot


@pytest.mark.parametrize("strategy", ["pullback", "donchian_4h"])
def test_monitor_never_applies_tp_or_trailing_to_donchian_positions(monkeypatch, strategy):
    monkeypatch.setattr(config, "STRATEGY", strategy)
    monkeypatch.setattr(config, "USE_TRAILING_STOP", True)
    monkeypatch.setattr(config, "TAKE_PROFIT_PERCENT", 1.5)
    bot = _monitor_bot(_donchian_long(), mark=110.0)     # +10%: TP global e trailing fechariam
    bot.monitor_positions()
    bot._close_position_with_notification.assert_not_called()
    # MFE/MAE são rastreados para toda posição; o trailing nunca arma na Donchian.
    assert bot.peak_prices == {"XUSDT_LONG": 110.0} and bot.adverse_prices == {"XUSDT_LONG": 110.0}
    assert bot.trailing_activated == {"XUSDT_LONG": False}


@pytest.mark.parametrize("strategy", ["pullback", "donchian_4h"])
def test_monitor_keeps_pullback_rules_for_pullback_positions(monkeypatch, strategy):
    monkeypatch.setattr(config, "STRATEGY", strategy)
    monkeypatch.setattr(config, "TAKE_PROFIT_PERCENT", 1.5)
    known = {"XUSDT_LONG": {"symbol": "XUSDT", "side": "LONG", "entry_price": 100.0, "quantity": 0.1,
                            "strategy_name": "trend_strong", "strategy_type": "trend_signal",
                            "custom_stop_loss": None, "custom_take_profit": None}}
    bot = _monitor_bot(known, mark=102.0)
    bot.monitor_positions()
    assert bot._close_position_with_notification.call_args.args[1] == "Take Profit (1.5%)"


def test_pullback_bot_has_no_runner_and_donchian_bot_has_one(monkeypatch):
    monkeypatch.setattr(config, "STRATEGY", "pullback")
    assert _bot(_Ex({})).donchian is None
    monkeypatch.setattr(config, "STRATEGY", "donchian_4h")
    assert isinstance(_bot(_Ex({})).donchian, d.DonchianRunner)


@pytest.mark.parametrize("strategy,closes_all", [("pullback", True), ("donchian_4h", False)])
def test_daily_profit_target_only_closes_under_pullback(monkeypatch, strategy, closes_all):
    monkeypatch.setattr(config, "STRATEGY", strategy)
    monkeypatch.setattr(config, "USE_DAILY_TARGETS", True)
    monkeypatch.setattr(config, "DAILY_PROFIT_TARGET", 10.0)
    bot = _bot(_Ex({}))
    del bot.check_daily_targets
    bot.last_daily_reset = trading_day_start().date()
    bot.daily_realized_pnl = 50.0
    bot._close_all_positions_daily_target = MagicMock()
    bot.telegram = SimpleNamespace(send_message=MagicMock())
    assert bot.check_daily_targets() is closes_all
    assert bot._close_all_positions_daily_target.called is closes_all


# ───────────── kill switch / drawdown ─────────────
class _Tg:
    def __init__(self):
        self.messages = []

    def send_message(self, text):
        self.messages.append(text)
        return True


def _ks(strategy):
    cfg = SimpleNamespace(STRATEGY=strategy, KILL_SWITCH_ENABLED=True, KILL_SWITCH_LOSS_STREAK_DAYS=3,
                          KILL_SWITCH_DRAWDOWN_ALERT_PERCENT=5.0, KILL_SWITCH_WR_FLOOR_PERCENT=40.0,
                          KILL_SWITCH_WR_MIN_TRADES=20, DONCHIAN_MAX_DRAWDOWN_PERCENT=20.0)
    ks = KillSwitchMonitor(config_obj=cfg, telegram=_Tg())
    for day in ("2026-10-19", "2026-10-20", "2026-10-21"):
        ks.record_daily_rollover(date=day, net_pnl=-1.0, trades_win=0, trades_loss=1)
    return ks


def _ks_bot(equity):
    return SimpleNamespace(paused=False, peak_equity=100.0, last_drawdown_equity=equity,
                           trades_win_count=5, trades_loss_count=25)


def test_donchian_disables_loss_streak_and_win_rate_pauses():
    ks, bot = _ks("donchian_4h"), _ks_bot(90.0)          # 3 dias negativos, WR 17%, DD 10%
    ks.check_all(bot=bot)
    assert bot.paused is False and ks.telegram.messages == []
    pull, pbot = _ks("pullback"), _ks_bot(99.0)
    pull.check_all(bot=pbot)
    assert pbot.paused is True                           # pullback inalterado: sequência pausa


def test_donchian_20pct_drawdown_stop_pauses_even_with_kill_switch_disabled(caplog):
    ks, bot = _ks("donchian_4h"), _ks_bot(80.0)
    ks.config.KILL_SWITCH_ENABLED = False
    with caplog.at_level(logging.WARNING):
        ks.check_all(bot=bot)
    assert bot.paused is True
    assert "drawdown 20.00% do pico (gatilho 20.00%" in caplog.text


def test_donchian_peak_starts_at_first_donchian_trade(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STRATEGY", "donchian_4h")
    monkeypatch.setattr(config, "SIMULATED_BALANCE_USD", 173.0)
    monkeypatch.setattr(config, "ENVIRONMENT", "testnet")
    store = TradeStore(str(tmp_path / "t.db"))
    rows = [("2026-10-01T00:00:00+00:00", "trend_signal", 30.0), ("2026-10-02T00:00:00+00:00", "trend_signal", -40.0),
            ("2026-10-10T00:00:00+00:00", "donchian_4h", 5.0), ("2026-10-11T00:00:00+00:00", "donchian_4h", -2.0)]
    for at, st, pnl in rows:
        store.record_open({"timestamp": at, "symbol": "XUSDT", "side": "LONG", "strategy_type": st})
        store.record_close(symbol="XUSDT", side="LONG", entry_price=1.0, exit_price=1.0, exit_at=at.replace("T00", "T01"),
                           pnl_gross=pnl, pnl_net=pnl, fees=0.0, close_reason="x", strategy_name=st)
    bot = _bot(_Ex({}))
    bot._rebuild_peak_equity_from_store(store)
    assert bot.peak_equity == pytest.approx(173.0 - 10.0 + 5.0)    # não o pico do pullback (173 + 30)
    monkeypatch.setattr(config, "STRATEGY", "pullback")
    pull = _bot(_Ex({}))
    pull._rebuild_peak_equity_from_store(store)
    assert pull.peak_equity == pytest.approx(203.0)


def test_restore_tags_from_sqlite_when_state_lost(tmp_path):
    store = TradeStore(str(tmp_path / "t.db"))
    store.record_open({"timestamp": "2026-10-10T00:00:00+00:00", "symbol": "XUSDT", "side": "LONG",
                       "strategy_type": "donchian_4h"})
    bot = _bot(_Ex({}), known={"XUSDT_LONG": {"symbol": "XUSDT", "side": "LONG", "strategy_type": "trend_signal",
                                              "strategy_name": "primary"}})
    bot.trade_store = store
    d.DonchianRunner(bot).restore_tags()
    assert bot.known_positions["XUSDT_LONG"]["strategy_type"] == "donchian_4h"


def test_startup_log(caplog):
    with caplog.at_level(logging.INFO):
        d.DonchianRunner(_bot(_Ex({}))).log_startup()
    assert "🧭 Estratégia: Donchian 4h (n=40,m=20,k=2,risco 0,25%) — forward test" in caplog.messages
    assert any("drawdown em 20% do pico" in m for m in caplog.messages)
    assert any("DESLIGADOS pausa por dias negativos" in m for m in caplog.messages)


def test_runner_closes_position_when_exchange_rejects_the_stop():
    ex = _Ex({"XUSDT": _bars_ending(FLAT + [101.0])})
    ex.set_stop_loss_take_profit = lambda **kw: False
    bot = _bot(ex)
    runner = d.DonchianRunner(bot)
    runner.universe, runner.universe_week = ["XUSDT"], d.rebalance_start(BOUNDARY)
    runner.tick(BOUNDARY + 60_000)
    ex.close_position.assert_called_once_with("XUSDT", "LONG")
    assert bot.known_positions == {}
    bot.ledger.record_trade_opened.assert_not_called()


def test_strategy_defaults_to_pullback():
    from trading_bot.core.config import TradingConfig

    assert TradingConfig.STRATEGY == "pullback"      # sem TRADING_BOT_STRATEGY no env (CI): merge não muda nada
    assert (config.DONCHIAN_ENTRY_BARS, config.DONCHIAN_EXIT_BARS, config.DONCHIAN_STOP_ATR_MULT,
            config.DONCHIAN_RISK_PERCENT, config.DONCHIAN_MAX_POSITIONS, config.DONCHIAN_MAX_GROSS_MULT,
            config.DONCHIAN_MAX_POSITION_MULT, config.DONCHIAN_MAX_DRAWDOWN_PERCENT) == (40, 20, 2.0, 0.25, 10, 3.0, 1.0, 20.0)


def test_failed_channel_exit_is_retried_on_next_tick():
    pos = {"symbol": "XUSDT", "side": "LONG", "entry_price": 100.0, "quantity": 0.1, "mark_price": 99.0}
    ex = _Ex({"XUSDT": _bars_ending(FLAT + [99.0])}, positions=[pos])
    bot = _bot(ex, known=_donchian_long())
    bot._close_position_with_notification = MagicMock(side_effect=[False, True])
    runner = d.DonchianRunner(bot)
    runner.universe_week = d.rebalance_start(BOUNDARY)
    runner.tick(BOUNDARY + 60_000)
    assert "XUSDT_LONG" in bot.known_positions
    runner.tick(BOUNDARY + 90_000)
    assert bot._close_position_with_notification.call_count == 2
    assert "XUSDT_LONG" not in bot.known_positions


def test_restart_does_not_reenter_a_bar_that_already_traded(tmp_path):
    store = TradeStore(str(tmp_path / "t.db"))
    store.record_open({"timestamp": "2025-10-27T20:00:15+00:00", "symbol": "XUSDT", "side": "LONG",
                       "strategy_type": "donchian_4h"})
    boundary = 1_761_595_200_000            # 2025-10-27T20:00:00Z
    ex = _Ex({"XUSDT": _klines(FLAT + [101.0], start=boundary - 61 * H4)})
    bot = _bot(ex)
    bot.trade_store = store
    runner = d.DonchianRunner(bot)
    runner.universe, runner.universe_week = ["XUSDT"], d.rebalance_start(boundary)
    runner.tick(boundary + 25 * 60_000)
    assert ex.orders == []


def test_open_without_order_response_uses_live_position():
    ex = _Ex({"XUSDT": _bars_ending(FLAT + [101.0])})
    ex.place_market_order = lambda **kw: None
    live = {"symbol": "XUSDT", "side": "LONG", "entry_price": 100.5, "quantity": 0.2}
    ex.get_open_positions = MagicMock(side_effect=[[], [live]])
    bot = _bot(ex)
    runner = d.DonchianRunner(bot)
    runner.universe, runner.universe_week = ["XUSDT"], d.rebalance_start(BOUNDARY)
    runner.tick(BOUNDARY + 60_000)
    meta = bot.known_positions["XUSDT_LONG"]
    assert (meta["entry_price"], meta["quantity"], meta["strategy_type"]) == (100.5, 0.2, "donchian_4h")
    assert ex.stops[0]["stop_loss_price"] < 100.5


def test_donchian_peak_survives_restart_via_state(monkeypatch):
    from trading_bot.core.bot_state_serde import BotStatePersistence

    monkeypatch.setattr(config, "STRATEGY", "donchian_4h")
    bot = _bot(_Ex({}))
    bot.peak_equity = 215.0
    bot.kill_switch = None
    payload = BotStatePersistence(bot).build_payload()
    assert payload["donchian_peak_equity"] == 215.0
    monkeypatch.setattr(config, "STRATEGY", "pullback")
    assert BotStatePersistence(_bot(_Ex({}))).build_payload()["donchian_peak_equity"] is None


# ───────────── resumo por barra: barra quieta ≠ barra não processada ─────────────
def _bar_lines(caplog):
    return [m for m in caplog.messages if m.startswith("🕓 Donchian: barra")]


def test_quiet_bar_logs_exactly_one_summary_line(caplog):
    bot = _bot(_Ex({"XUSDT": _bars_ending(FLAT + [100.2])}))
    bot.last_drawdown_equity, bot.peak_equity = 172.81, 172.81
    runner = d.DonchianRunner(bot)
    runner.universe, runner.universe_week = ["XUSDT"], d.rebalance_start(BOUNDARY)
    with caplog.at_level(logging.INFO):
        runner.tick(BOUNDARY + 60_000)
        runner.tick(BOUNDARY + 120_000)          # mesma barra: não repete o resumo
    assert _bar_lines(caplog) == [
        "🕓 Donchian: barra 2025-10-27 20:00 UTC — 1/1 pares analisados (0 sem barra, 0 dados parados), "
        "0 rompimentos (0L/0S), 0 saídas, 0 entradas, 0 pulados; posições abertas 0/10; "
        "equity $172.81 (DD 0.0% do pico)"
    ]


def test_bar_summary_counts_entry_and_exit(caplog):
    pos = {"symbol": "XUSDT", "side": "LONG", "entry_price": 100.0, "quantity": 0.1, "mark_price": 99.0}
    ex = _Ex({"XUSDT": _bars_ending(FLAT + [99.0]), "YUSDT": _bars_ending(FLAT + [101.0])}, positions=[pos])
    bot = _bot(ex, known=_donchian_long())
    bot.last_drawdown_equity, bot.peak_equity = 180.0, 200.0
    runner = d.DonchianRunner(bot)
    runner.universe, runner.universe_week = ["YUSDT"], d.rebalance_start(BOUNDARY)
    with caplog.at_level(logging.INFO):
        runner.tick(BOUNDARY + 60_000)
    [line] = _bar_lines(caplog)
    assert ("2/2 pares analisados (0 sem barra, 0 dados parados), 1 rompimentos (1L/0S), 1 saídas, "
            "1 entradas, 0 pulados; posições abertas 1/10; equity $180.00 (DD 10.0% do pico)") in line


def test_bar_summary_lists_skip_reasons(caplog):
    ex = _Ex({"YUSDT": _bars_ending(FLAT + [101.0])})
    bot = _bot(ex)
    bot.paused = True
    runner = d.DonchianRunner(bot)
    runner.universe, runner.universe_week = ["YUSDT"], d.rebalance_start(BOUNDARY)
    with caplog.at_level(logging.INFO):
        runner.tick(BOUNDARY + 60_000)
    [line] = _bar_lines(caplog)
    assert "0 entradas, 1 pulados (bot pausado ×1); posições abertas 0/10; equity n/d" in line


def test_deferred_bar_logs_that_it_was_not_processed(caplog):
    ex = _Ex({"XUSDT": _bars_ending(FLAT + [101.0])})
    ex.get_open_positions = MagicMock(side_effect=RuntimeError("timeout"))
    runner = d.DonchianRunner(_bot(ex))
    runner.universe, runner.universe_week = ["XUSDT"], d.rebalance_start(BOUNDARY)
    with caplog.at_level(logging.INFO):
        runner.tick(BOUNDARY + 60_000)
        assert _bar_lines(caplog) == []          # ainda dentro da janela: tenta de novo
        runner.tick(BOUNDARY + 31 * 60_000)
    assert _bar_lines(caplog) == [
        "🕓 Donchian: barra 2025-10-27 20:00 UTC NÃO processada — adiada 2x, "
        "posições indisponíveis (timeout); 1 pares pendentes"
    ]


def test_bot_close_position_manual_uses_donchian_exit_path():
    held = [{"symbol": "XUSDT", "side": "LONG", "entry_price": 100.0, "quantity": 1.0}]
    ex = _Ex({}, positions=held)
    bot = _bot(ex, known={"XUSDT_LONG": {"symbol": "XUSDT", "side": "LONG", "strategy_type": "donchian_4h"}})
    assert bot.close_position_manual("XUSDT") is True
    bot._close_position_with_notification.assert_called_once_with(held[0], "Manual (dashboard)")
    assert "XUSDT_LONG" not in bot.known_positions
    ex.cancel_open_orders.assert_called_once_with("XUSDT")
    with pytest.raises(LookupError):
        bot.close_position_manual("YUSDT")

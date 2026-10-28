"""
Painel do forward test Donchian 4h (dashboard v2): métricas puras em
trading_bot/web/forward_test.py e os coletores novos de trading_bot/web/data.py.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from trading_bot.core.config import config
from trading_bot.core.trade_store import TradeStore
from trading_bot.web import data as dash
from trading_bot.web import forward_test as ft


def _trade(pnl, *, entry=100.0, stop=95.0, qty=1.0, exit_at="2026-10-26T12:00:00+00:00",
           opened_at="2026-10-25T12:00:00+00:00", fees=0.1, funding=0.0):
    return {"entry_price": entry, "stop_loss": stop, "qty": qty, "pnl_net": pnl, "fees": fees,
            "funding": funding, "opened_at": opened_at, "exit_at": exit_at}


def test_r_multiple_e_risco_inicial():
    t = _trade(-5.0)                       # risco = |100 − 95| × 1 = $5
    assert ft.initial_risk_usd(t) == 5.0
    assert ft.r_multiple(t) == -1.0
    assert ft.r_multiple(_trade(10.0, stop=None)) is None


def test_pnl_pct_e_duracao():
    t = _trade(2.0, entry=50.0, qty=2.0)   # notional 100 → 2%
    assert ft.pnl_pct_of_notional(t) == pytest.approx(2.0)
    assert ft.duration_hours(t) == 24.0


def test_bootstrap_ci_contem_media_e_e_deterministico():
    vals = [1.0, -1.0, 2.0, -0.5, 0.5, 3.0]
    ci = ft.bootstrap_ci(vals)
    assert ci == ft.bootstrap_ci(vals)
    assert ci[0] <= sum(vals) / len(vals) <= ci[1]
    assert ft.bootstrap_ci([1.0, 2.0]) is None


def test_max_drawdown_do_pico_inicial():
    dd, peak = ft.max_drawdown([290.0, 310.0, 279.0, 300.0], start_peak=300.0)
    assert dd == pytest.approx(10.0)       # 310 → 279
    assert peak == 310.0


def test_r_histogram_bins_fixos():
    h = ft.r_histogram([-1.0, -0.99, 0.2, 2.5, None, 7.0])
    assert h["n"] == 5
    assert h["labels"][0] == "<-2R" and h["labels"][-1] == "≥5R"
    assert h["counts"][-1] == 1            # 7R
    assert sum(h["counts"]) == 5


def test_daily_calendar_usa_dia_de_trading(monkeypatch):
    monkeypatch.setattr(config, "DAILY_PNL_TZ_OFFSET_HOURS", -3.0)
    rows = [_trade(1.0, exit_at="2026-10-26T01:00:00+00:00"),   # 22h BRT do dia 25
            _trade(-2.0, exit_at="2026-10-26T12:00:00+00:00")]
    cal = ft.daily_calendar(rows)
    assert [(d["day"], d["net"], d["trades"]) for d in cal] == [("2026-10-25", 1.0, 1), ("2026-10-26", -2.0, 1)]


def test_summarize_inconclusivo_com_poucos_trades():
    now = datetime(2026, 10, 28, 23, 43, 7, tzinfo=timezone.utc)
    out = ft.summarize([_trade(-5.0), _trade(12.0)], now=now, sim_balance=300.0)
    assert out["trades"] == 2 and out["trades_remaining"] == 148
    assert out["elapsed_days"] == pytest.approx(4.0)
    assert out["expectancy_usd"] == 3.5 and out["expectancy_ci95"] is None
    assert out["verdict"] == "inconclusive"
    assert out["win_rate"] == 50.0 and out["avg_r"] == pytest.approx((-1.0 + 2.4) / 2)
    assert out["trades_per_week"] == pytest.approx(2 / (4 / 7))


def test_summarize_ignora_trades_antes_do_inicio():
    rows = [_trade(100.0, exit_at="2026-10-01T00:00:00+00:00"), _trade(-1.0)]
    out = ft.summarize(rows, sim_balance=300.0)
    assert out["trades"] == 1 and out["net_usd"] == -1.0


def test_summarize_falha_por_drawdown():
    rows = [_trade(-70.0), _trade(1.0, exit_at="2026-10-27T00:00:00+00:00")]
    out = ft.summarize(rows, sim_balance=300.0)
    assert out["max_drawdown_pct"] == pytest.approx(70 / 300 * 100)
    assert out["verdict"] == "failed"


def test_summarize_usa_equity_e_pico_do_bot():
    out = ft.summarize([_trade(-5.0)], sim_balance=300.0, base_equity=299.32, equity_now=290.0, peak_equity=305.0)
    assert out["peak_equity"] == 305.0
    assert out["drawdown_pct"] == pytest.approx(15 / 305 * 100)
    assert out["dd_floor_equity"] == pytest.approx(244.0)
    assert out["dd_room_usd"] == pytest.approx(46.0)


def test_downsample_mantem_extremos():
    pts = list(range(1000))
    out = dash.downsample(pts, 600)
    assert len(out) == 600 and out[0] == 0 and out[-1] == 999
    assert dash.downsample([1, 2], 600) == [1, 2]


def _store_with_donchian(tmp_path):
    store = TradeStore(str(tmp_path / "t.db"))
    tid = store.record_open({"timestamp": "2026-10-25T12:00:00+00:00", "symbol": "ETHUSDT", "side": "LONG",
                             "qty": 0.01, "value": 40.0, "entry_price": 4000.0, "stop_loss": 3800.0,
                             "strategy_name": "donchian_4h", "strategy_type": "donchian_4h"})
    store.record_close(symbol="ETHUSDT", side="LONG", entry_price=4000.0, exit_price=4400.0,
                       exit_at="2026-10-26T12:00:00+00:00", pnl_gross=4.0, pnl_net=3.9, fees=0.1,
                       close_reason="Donchian: saída no canal de 20", strategy_name="donchian_4h")
    store.update_trade_pnl(tid, pnl_gross=4.0, fees=0.1, pnl_net=3.95, funding=0.05)
    store.record_open({"timestamp": "2026-06-01T00:00:00+00:00", "symbol": "BTCUSDT", "side": "SHORT",
                       "qty": 0.001, "value": 100.0, "entry_price": 100000.0, "strategy_name": "primary",
                       "strategy_type": "trend_signal"})
    return store


def test_store_closed_trades_filtra_por_estrategia(tmp_path):
    store = _store_with_donchian(tmp_path)
    rows = store.closed_trades("donchian_4h")
    assert len(rows) == 1
    assert rows[0]["funding"] == 0.05 and rows[0]["stop_loss"] == 3800.0 and rows[0]["status"] == "closed"
    assert store.closed_trades() == rows      # o trade trend_signal segue aberto


def test_collect_forward_test_e_recent_trades(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ENVIRONMENT", "testnet")
    monkeypatch.setattr(config, "SIMULATED_BALANCE_USD", 300.0)
    store = _store_with_donchian(tmp_path)
    bot = SimpleNamespace(trade_store=store, peak_equity=304.0)
    out = dash.collect_forward_test(bot, {"last_balance": 303.95, "unrealized_pnl": 0.0})
    assert out["trades"] == 1 and out["net_usd"] == pytest.approx(3.95)
    assert out["base_equity"] == pytest.approx(300.0)
    assert out["verdict"] == "inconclusive"
    trades = dash.collect_recent_trades(bot, limit=10)
    eth = next(t for t in trades if t["symbol"] == "ETHUSDT")
    assert eth["r_multiple"] == pytest.approx(3.95 / 2.0)
    assert eth["duration_hours"] == 24.0 and eth["strategy_type"] == "donchian_4h"
    assert dash.collect_forward_test(SimpleNamespace()) == {}


def test_collect_positions_donchian_expoe_risco_e_canal():
    bot = SimpleNamespace(
        known_positions={"ETHUSDT_LONG": {"symbol": "ETHUSDT", "side": "LONG", "entry_price": 4000.0, "quantity": 0.01,
                                          "strategy_type": "donchian_4h", "custom_stop_loss": 3800.0}},
        _positions_lock=None,
        exchange=SimpleNamespace(get_open_positions=lambda: [{"symbol": "ETHUSDT", "side": "LONG", "mark_price": 4100.0, "unrealized_pnl": 1.0}]),
        donchian=SimpleNamespace(exit_levels={"ETHUSDT": {"exit_long": 3900.0, "exit_short": 4300.0}}),
    )
    p = dash.collect_positions(bot)[0]
    assert p["risk_usd"] == pytest.approx(2.0)
    assert p["r_now"] == pytest.approx(0.5)
    assert p["stop_distance_pct"] == pytest.approx((4100 - 3800) / 4100 * 100)
    assert p["exit_level"] == 3900.0
    assert "trailing_activated" not in p


def test_collect_engine_e_health(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "PROJECT_ROOT", str(tmp_path))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "deploy_info.json").write_text('{"sha": "abc1234567890", "deployed_at": "2026-10-28T00:00:00Z"}')
    dash._deploy_cache.update({"mtime": None, "data": {}})
    bot = SimpleNamespace(
        donchian=SimpleNamespace(snapshot=lambda: {"universe": ["ETHUSDT"], "counters": {"ok": 1}}),
        exchange=SimpleNamespace(
            get_ws_stats=lambda: {"subscriptions": 2, "reconnects": 1, "streams": [{"age_seconds": 5.0, "recovering": True}, {"age_seconds": 30.0, "recovering": False}]},
            get_user_stream_stats=lambda: {"started": True, "error_count": 0, "restart_attempts": 0, "last_message_age_seconds": 3.0},
        ),
        get_runtime_stats_report=lambda reset=False: {"loop_errors": 2, "last_error": "boom", "monitor_cycles": 10},
        start_time=datetime.now(), paused=True, peak_equity=300.0, last_drawdown_equity=230.0,
    )
    assert dash.collect_engine(bot)["universe"] == ["ETHUSDT"]
    h = dash.collect_health(bot, {"paused": True})
    assert h["ws"] == {"subscriptions": 2, "reconnects": 1, "recovering": 1, "max_age_seconds": 30.0}
    assert h["runtime"]["loop_errors"] == 2 and h["runtime"]["last_error"] == "boom"
    assert h["deploy_sha"] == "abc123456789" and h["uptime_seconds"] >= 0
    assert "drawdown" in h["paused_reason"]
    assert dash.collect_engine(SimpleNamespace()) == {}


def test_donchian_runner_snapshot_e_json_safe():
    from collections import Counter

    from trading_bot.core.donchian import DonchianRunner
    runner = DonchianRunner.__new__(DonchianRunner)
    runner.universe, runner.universe_week, runner._boundary = ["BTCUSDT"], 1_761_523_200_000, 1_761_609_600_000
    runner._pending, runner.exit_levels = {"BTCUSDT"}, {"BTCUSDT": {"exit_long": 1.0, "exit_short": 2.0}}
    runner._bar = {"total": 1, "ok": 0, "skips": Counter({"minNotional": 1}), "logged": False}
    snap = runner.snapshot()
    assert snap["bar_close"] == "2025-10-28T00:00:00+00:00" and snap["next_bar_close"] == "2025-10-28T04:00:00+00:00"
    assert snap["skips"] == {"minNotional": 1} and "logged" not in snap["counters"]
    assert snap["pending"] == ["BTCUSDT"] and snap["params"]["exit_bars"] == config.DONCHIAN_EXIT_BARS

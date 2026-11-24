"""Simulações de renda (trading_bot/web/simulation.py): cálculos puros sobre stats fixos."""
from __future__ import annotations

import pytest

from trading_bot.web.simulation import MAX_DAYS, measure, quantile, simulate

# 20 dias, $1/dia na média sobre base $300 → 0,3333%/dia
STATS = {"sample_days": 20, "traded_days": 18, "cycles": 40, "wins": 30, "win_rate": 75.0, "total_net": 20.0,
         "mean_daily": 1.0, "median_daily": 1.0, "stdev_daily": 0.5, "p25_daily": 0.5, "p75_daily": 1.5,
         "cycles_per_day": 2.0, "pnl_per_cycle": 0.5, "from": "2026-11-01", "to": "2026-11-20", "series": []}


def test_target_reachable_scenarios_ordered_and_required_capital():
    sim = simulate(STATS, capital=320.0, target_brl=6000.0, fx=6.0, days_per_month=30, stop_usd=180.0)
    assert sim["base_capital"] == 300.0 and sim["target"]["daily_usd"] == pytest.approx(33.33, abs=0.01)
    assert sim["target"]["monthly_brl"] == 6000.0
    keys = [s["key"] for s in sim["scenarios"]]
    assert keys == ["conservative", "observed", "optimistic"]
    pcts = [s["daily_pct"] for s in sim["scenarios"]]
    assert pcts == sorted(pcts) and pcts[1] == pytest.approx(0.3333, abs=1e-3)
    obs = sim["scenarios"][1]
    assert obs["daily_usd"] == pytest.approx(320 / 300, abs=0.01) and obs["monthly_usd"] == pytest.approx(320 / 300 * 30, abs=0.1)
    assert obs["required_capital"] == pytest.approx(33.33 / (1 / 300), rel=1e-3)   # ~$10.000
    assert sim["scenarios"][0]["required_capital"] > obs["required_capital"] > sim["scenarios"][2]["required_capital"]
    # composição: 320 × 1,00333^d ≥ 10.000 → d ≈ 1035; otimista chega antes
    assert 1000 < obs["days_to_target"] < 1100 and sim["scenarios"][2]["days_to_target"] < obs["days_to_target"]
    assert sim["required"]["cycles_needed_per_day"] == pytest.approx(66.67, abs=0.01) and sim["required"]["cycles_observed_per_day"] == 2.0
    assert sim["required"]["sheet"]["label"] == "10.000" and sim["required"]["sheet"]["first_order"] == 28
    assert sim["risks"]["stop_pct_of_capital"] == pytest.approx(56.25) and sim["risks"]["small_sample"] is False
    assert len(obs["curve"]) == 366 and obs["curve"][0] == 320.0
    assert any("exige ~$10." in r for r in sim["recommendations"]) and any("impostos não incluídos" in r for r in sim["recommendations"])
    assert [f["fx"] for f in sim["fx_sensitivity"]] == [5.5, 6.0]


def test_target_daily_override_and_already_reached():
    sim = simulate(STATS, capital=320.0, target_daily=1.0, fx=5.94)
    assert sim["target"]["daily_usd"] == 1.0 and sim["target"]["daily_brl"] == 5.94
    assert sim["required"]["days_to_target"] == 0 and "já cobre" in " ".join(sim["recommendations"])


def test_non_positive_return_never_reaches():
    neg = {**STATS, "total_net": -20.0, "mean_daily": -1.0, "p25_daily": -2.0, "p75_daily": -0.1, "pnl_per_cycle": -0.5}
    sim = simulate(neg, capital=320.0, target_daily=33.67)
    assert all(s["required_capital"] is None and s["days_to_target"] is None for s in sim["scenarios"])
    assert sim["required"]["cycles_needed_per_day"] is None
    assert any("não positivo" in r for r in sim["recommendations"])
    assert sim["scenarios"][1]["curve"][-1] < 320.0


def test_small_sample_warns_and_tiny_return_caps_days():
    tiny = {**STATS, "sample_days": 5, "mean_daily": 0.001, "p25_daily": 0.0, "p75_daily": 0.002, "total_net": 0.005}
    sim = simulate(tiny, capital=320.0, target_daily=33.67)
    assert sim["risks"]["small_sample"] is True and any("Amostra curta" in r for r in sim["recommendations"])
    assert sim["scenarios"][1]["days_to_target"] is None   # > MAX_DAYS
    assert sim["scenarios"][0]["required_capital"] is None   # p25 = 0 → sem capital que alcance
    assert MAX_DAYS == 3650


def test_quantile_and_measure(tmp_path):
    assert quantile([1.0, 2.0, 3.0, 4.0], 0.25) == 1.75 and quantile([], 0.5) == 0.0
    from trading_bot.core.trade_store import TradeStore
    store = TradeStore(str(tmp_path / "t.db"))
    for day, pnl in (("2026-11-01", 2.0), ("2026-11-01", -0.5), ("2026-11-03", 1.0)):   # dia 02 sem trade = $0
        store.record_open({"timestamp": f"{day}T01:00:00+00:00", "symbol": "ADAUSDT", "signal": "BUY", "side": "LONG", "qty": 10.0,
                           "value": 6.0, "entry_price": 0.6, "stop_loss": 0.55, "take_profit": 0.65, "strategy_name": "primary",
                           "strategy_type": "martingale_hedge", "double_first": False, "ai_consultive": {}})
        store.record_close(symbol="ADAUSDT", side="LONG", entry_price=0.6, exit_price=0.61, exit_at=f"{day}T05:00:00+00:00",
                           pnl_gross=pnl + 0.05, pnl_net=pnl, fees=0.05, close_reason="tp", strategy_name="primary")
    st = measure(store)
    assert st["sample_days"] == 3 and st["traded_days"] == 2 and st["cycles"] == 3 and st["wins"] == 2
    assert st["total_net"] == 2.5 and st["mean_daily"] == pytest.approx(2.5 / 3, abs=1e-3) and st["median_daily"] == 1.0
    assert [s["net"] for s in st["series"]] == [1.5, 0.0, 1.0] and st["cycles_per_day"] == 1.0
    # piso do reset de banca corta os trades anteriores
    since = int(__import__("datetime").datetime(2026, 11, 2, tzinfo=__import__("datetime").timezone.utc).timestamp() * 1000)
    assert measure(store, since)["cycles"] == 1 and measure(store, since * 10) is None

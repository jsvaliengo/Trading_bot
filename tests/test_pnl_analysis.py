"""
Testes do TradeStore.pnl_analysis() e do collector collect_pnl_analysis — base
do painel "Análise de P&L" do dashboard (Total Profit/Loss, win rate, dias,
médias, volume), todos derivados do SQLite (fonte de verdade).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from trading_bot.core.trade_store import TradeStore
from trading_bot.web import data as dashboard_data


def _store(tmp_path):
    return TradeStore(str(tmp_path / "trades.test.db"))


def _close(store, *, symbol="BTCUSDT", side="LONG", entry=100.0, pnl_net, fees, value, exit_at):
    store.record_open({
        "timestamp": exit_at, "symbol": symbol, "signal": "BUY", "side": side,
        "qty": 1.0, "value": value, "entry_price": entry, "stop_loss": 0.0,
        "take_profit": 0.0, "strategy_name": "p", "strategy_type": "hedge",
        "double_first": False, "ai_consultive": {},
    })
    store.record_close(
        symbol=symbol, side=side, entry_price=entry, exit_price=entry + 1,
        exit_at=exit_at, pnl_gross=pnl_net + fees, pnl_net=pnl_net, fees=fees,
        close_reason="x", strategy_name="p",
    )


def test_pnl_analysis_empty_store_is_zeroed(tmp_path):
    an = _store(tmp_path).pnl_analysis()
    assert an["trades"] == 0
    assert an["total_profit"] == 0.0
    assert an["total_loss"] == 0.0
    assert an["net_pnl"] == 0.0
    assert an["win_rate"] == 0.0
    assert an["profit_loss_ratio"] == 0.0
    assert an["winning_days"] == 0


def test_pnl_analysis_aggregates_wins_losses_and_volume(tmp_path):
    store = _store(tmp_path)
    _close(store, pnl_net=5.0, fees=0.1, value=100.0, exit_at="2026-06-01T10:00:00")
    _close(store, pnl_net=-2.0, fees=0.1, value=100.0, exit_at="2026-06-01T12:00:00")
    _close(store, symbol="ETHUSDT", pnl_net=3.0, fees=0.1, value=200.0, exit_at="2026-06-02T10:00:00")

    an = store.pnl_analysis()
    assert an["trades"] == 3
    assert an["wins"] == 2 and an["losses"] == 1 and an["breakeven"] == 0
    assert an["win_rate"] == pytest.approx(66.67, abs=0.01)
    assert an["total_profit"] == pytest.approx(8.0)
    assert an["total_loss"] == pytest.approx(-2.0)
    assert an["net_pnl"] == pytest.approx(6.0)
    assert an["avg_profit"] == pytest.approx(4.0)
    assert an["avg_loss"] == pytest.approx(-2.0)
    assert an["profit_loss_ratio"] == pytest.approx(2.0)
    assert an["trading_volume"] == pytest.approx(400.0)


def test_pnl_analysis_counts_days_by_sign(tmp_path):
    store = _store(tmp_path)
    # dia 1: +5 -2 = +3 (ganho) ; dia 2: -4 (perda) ; dia 3: +1 -1 = 0 (breakeven)
    _close(store, pnl_net=5.0, fees=0.0, value=100.0, exit_at="2026-06-01T09:00:00")
    _close(store, pnl_net=-2.0, fees=0.0, value=100.0, exit_at="2026-06-01T18:00:00")
    _close(store, pnl_net=-4.0, fees=0.0, value=100.0, exit_at="2026-06-02T09:00:00")
    _close(store, pnl_net=1.0, fees=0.0, value=100.0, exit_at="2026-06-03T09:00:00")
    _close(store, pnl_net=-1.0, fees=0.0, value=100.0, exit_at="2026-06-03T18:00:00")

    an = store.pnl_analysis()
    assert an["winning_days"] == 1
    assert an["losing_days"] == 1
    assert an["breakeven_days"] == 1


def test_pnl_analysis_breakeven_trade_not_counted_as_win_or_loss(tmp_path):
    store = _store(tmp_path)
    _close(store, pnl_net=0.0, fees=0.0, value=100.0, exit_at="2026-06-01T09:00:00")
    an = store.pnl_analysis()
    assert an["wins"] == 0 and an["losses"] == 0 and an["breakeven"] == 1
    assert an["profit_loss_ratio"] == 0.0  # sem perdas → ratio 0 (sem divisão por zero)


def test_collect_pnl_analysis_without_store_is_empty():
    assert dashboard_data.collect_pnl_analysis(SimpleNamespace()) == {}


def test_collect_pnl_analysis_uses_store(tmp_path):
    store = _store(tmp_path)
    _close(store, pnl_net=4.0, fees=0.0, value=100.0, exit_at="2026-06-01T10:00:00")
    bot = SimpleNamespace(trade_store=store)
    an = dashboard_data.collect_pnl_analysis(bot)
    assert an["trades"] == 1 and an["net_pnl"] == pytest.approx(4.0)

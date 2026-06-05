"""
Testes do saldo/P&L ACUMULADO e do histórico por dia no dashboard.

Antes os KPIs usavam o realizado do DIA da Binance, então saldo e "P&L total"
zeravam na virada do dia UTC — escondendo o progresso do bot. Agora:
- collect_summary usa o realizado ACUMULADO (TradeStore.cumulative_realized_pnl);
- collect_daily_history expõe P&L por dia (TradeStore.daily_pnl_history).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from trading_bot.core.trade_store import TradeStore
from trading_bot.web import data as dashboard_data
from trading_bot.core.config import config as global_config


def _store(tmp_path):
    return TradeStore(str(tmp_path / "trades.test.db"))


def _open(symbol="ETHUSDT", side="LONG", entry=2500.0):
    return {
        "timestamp": "2026-06-01T10:00:00", "symbol": symbol, "signal": "BUY",
        "side": side, "qty": 1.0, "value": 100.0, "entry_price": entry,
        "stop_loss": 2400.0, "take_profit": 2700.0, "strategy_name": "primary",
        "strategy_type": "hedge", "double_first": False, "ai_consultive": {},
    }


def _close(store, *, entry, pnl_net, fees, exit_at, symbol="ETHUSDT", side="LONG"):
    store.record_open(_open(symbol, side, entry))
    store.record_close(
        symbol=symbol, side=side, entry_price=entry, exit_price=entry + 1,
        exit_at=exit_at, pnl_gross=pnl_net + fees, pnl_net=pnl_net, fees=fees,
        close_reason="x", strategy_name="primary",
    )


# ─────────────────────────── cumulative_realized_pnl ───────────────────────────

def test_cumulative_realized_sums_all_closed(tmp_path):
    store = _store(tmp_path)
    _close(store, entry=2500, pnl_net=2.0, fees=0.1, exit_at="2026-06-01T12:00:00")
    _close(store, entry=2510, pnl_net=-1.5, fees=0.1, exit_at="2026-06-02T12:00:00")
    _close(store, entry=2520, pnl_net=4.0, fees=0.1, exit_at="2026-06-03T12:00:00")
    assert store.cumulative_realized_pnl() == pytest.approx(4.5)


def test_cumulative_realized_empty_is_zero(tmp_path):
    assert _store(tmp_path).cumulative_realized_pnl() == 0.0


# ─────────────────────────── daily_pnl_history ───────────────────────────

def test_daily_history_groups_by_day_with_running_cumulative(tmp_path):
    store = _store(tmp_path)
    # dia 1: +2.0 ; dia 2: -1.5 ; dia 3: +4.0  (exatamente o exemplo do user)
    _close(store, entry=2500, pnl_net=2.0, fees=0.1, exit_at="2026-06-01T12:00:00")
    _close(store, entry=2510, pnl_net=-1.5, fees=0.1, exit_at="2026-06-02T09:00:00")
    _close(store, entry=2511, pnl_net=4.0, fees=0.1, exit_at="2026-06-03T09:00:00")

    hist = store.daily_pnl_history()
    assert [d["day"] for d in hist] == ["2026-06-01", "2026-06-02", "2026-06-03"]
    assert [d["net"] for d in hist] == [2.0, -1.5, 4.0]
    # acumulado corrido
    assert [d["cumulative"] for d in hist] == [2.0, 0.5, 4.5]


def test_daily_history_aggregates_multiple_trades_same_day(tmp_path):
    store = _store(tmp_path)
    _close(store, entry=2500, pnl_net=1.0, fees=0.1, exit_at="2026-06-01T08:00:00")
    _close(store, entry=2501, pnl_net=-0.5, fees=0.1, exit_at="2026-06-01T20:00:00")
    hist = store.daily_pnl_history()
    assert len(hist) == 1
    d = hist[0]
    assert d["trades"] == 2 and d["wins"] == 1 and d["losses"] == 1
    assert d["win_rate"] == 50.0
    assert d["net"] == pytest.approx(0.5)
    assert d["fees"] == pytest.approx(0.2)


def test_daily_history_limit_keeps_cumulative_correct(tmp_path):
    store = _store(tmp_path)
    _close(store, entry=2500, pnl_net=2.0, fees=0.0, exit_at="2026-06-01T12:00:00")
    _close(store, entry=2510, pnl_net=3.0, fees=0.0, exit_at="2026-06-02T12:00:00")
    _close(store, entry=2520, pnl_net=5.0, fees=0.0, exit_at="2026-06-03T12:00:00")
    hist = store.daily_pnl_history(limit=1)  # só o último dia
    assert len(hist) == 1
    assert hist[0]["day"] == "2026-06-03"
    # cumulative reflete TODOS os dias, não só o cortado
    assert hist[0]["cumulative"] == pytest.approx(10.0)


# ─────────────────────────── collectors do dashboard ───────────────────────────

def test_collect_daily_history_without_store_is_empty():
    bot = SimpleNamespace()  # sem trade_store
    assert dashboard_data.collect_daily_history(bot) == []


def test_collect_daily_history_uses_store(tmp_path):
    store = _store(tmp_path)
    _close(store, entry=2500, pnl_net=2.0, fees=0.1, exit_at="2026-06-01T12:00:00")
    bot = SimpleNamespace(trade_store=store)
    hist = dashboard_data.collect_daily_history(bot)
    assert len(hist) == 1 and hist[0]["net"] == 2.0


def test_collect_summary_uses_store_cumulative(tmp_path, monkeypatch):
    """Com trade_store, o saldo/P&L total usa o acumulado do store (não o do dia)."""
    monkeypatch.setattr(global_config, "SIMULATED_BALANCE_USD", 100.0, raising=False)
    store = _store(tmp_path)
    _close(store, entry=2500, pnl_net=2.0, fees=0.0, exit_at="2026-06-01T12:00:00")
    _close(store, entry=2510, pnl_net=-12.0, fees=0.0, exit_at="2026-06-04T12:00:00")
    # acumulado = -10.0
    bot = SimpleNamespace(
        initial_capital=100.0, last_known_balance=100.0, total_pnl=999.0,  # ignorado
        daily_realized_pnl=0.0, closed_trades_count=2, paused=False, running=True,
        trade_store=store,
        exchange=SimpleNamespace(
            get_daily_pnl_from_binance=lambda: {"total": 0.0},
            get_account_info=lambda: {"wallet_balance": 100.0, "unrealized_pnl": 0.0},
            get_open_positions=lambda: [],
        ),
    )
    summary = dashboard_data.collect_summary(bot)
    # total_pnl = acumulado (-10) + unrealized (0) = -10  (não o bot.total_pnl=999)
    assert summary["total_pnl"] == pytest.approx(-10.0)
    # saldo = cap 100 + acumulado (-10) = 90  (não volta pra 100 no dia novo)
    assert summary["last_balance"] == pytest.approx(90.0)
    assert summary["daily_pnl"] == 0.0  # P&L HOJE diário, separado

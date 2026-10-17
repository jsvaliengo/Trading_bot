"""
Testes da consistência da curva de equity com o card SALDO.

Bug (14/06): card SALDO mostrava $302 (realizado ACUMULADO do SQLite) e o gráfico
mostrava $299 (snapshot usava realizado do DIA, que reseta à meia-noite UTC).
A curva agora deriva do mesmo realizado acumulado do SQLite.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from trading_bot.core.config import config
from trading_bot.web.data import collect_portfolio_history


def _fake_bot(history, trades):
    store = SimpleNamespace(recent_trades=lambda _n=1000: list(trades))
    return SimpleNamespace(portfolio_history=history, trade_store=store)


def test_equity_uses_cumulative_realized_in_testnet(monkeypatch):
    monkeypatch.setattr(config, "ENVIRONMENT", "testnet")
    monkeypatch.setattr(config, "SIMULATED_BALANCE_USD", 300.0)

    trades = [
        {"exit_time": "2026-06-13T13:30:00+00:00", "pnl_net": 1.0},
        {"exit_time": "2026-06-14T08:00:00+00:00", "pnl_net": 0.61},
        {"symbol": "OPEN"},
    ]
    history = [
        {"timestamp": "2026-06-13T14:00:00+00:00", "balance": 300.0, "pnl_unrealized": 0.10},
        {"timestamp": "2026-06-14T12:00:00+00:00", "balance": 300.0, "pnl_unrealized": 0.40},
    ]
    out = collect_portfolio_history(_fake_bot(history, trades))

    # 1º ponto: só o trade de 13/06 conta → 300 + 1.0 + 0.10
    assert out[0]["equity"] == pytest.approx(301.10)
    # 2º ponto: ambos os trades → 300 + 1.61 + 0.40 (== card SALDO)
    assert out[1]["equity"] == pytest.approx(302.01)


def test_equity_ignores_daily_reset(monkeypatch):
    # Mesmo com snapshot tendo pnl_total "do dia" antigo, a curva usa o cumulativo.
    monkeypatch.setattr(config, "ENVIRONMENT", "testnet")
    monkeypatch.setattr(config, "SIMULATED_BALANCE_USD", 300.0)
    trades = [{"exit_time": "2026-06-13T13:30:00+00:00", "pnl_net": 1.61}]
    history = [
        # snapshot carrega pnl_total=-0.34 (realizado do dia) — deve ser ignorado.
        {"timestamp": "2026-06-14T12:00:00+00:00", "balance": 300.0,
         "pnl_unrealized": 0.40, "pnl_total": -0.34, "pnl_realized": -0.74},
    ]
    out = collect_portfolio_history(_fake_bot(history, trades))
    assert out[0]["equity"] == pytest.approx(302.01)  # 300 + 1.61 + 0.40


def test_equity_mainnet_uses_wallet(monkeypatch):
    # Em mainnet (sem SIMULATED_BALANCE) o wallet já reflete o realizado.
    monkeypatch.setattr(config, "ENVIRONMENT", "mainnet")
    monkeypatch.setattr(config, "SIMULATED_BALANCE_USD", 0.0)
    trades = [{"exit_time": "2026-06-13T13:30:00+00:00", "pnl_net": 5.0}]
    history = [{"timestamp": "2026-06-14T12:00:00+00:00", "balance": 1000.0, "pnl_unrealized": 2.0}]
    out = collect_portfolio_history(_fake_bot(history, trades))
    assert out[0]["equity"] == pytest.approx(1002.0)  # wallet + unrealized (sem somar realizado)


def test_equity_handles_no_store(monkeypatch):
    monkeypatch.setattr(config, "ENVIRONMENT", "testnet")
    monkeypatch.setattr(config, "SIMULATED_BALANCE_USD", 300.0)
    bot = SimpleNamespace(portfolio_history=[
        {"timestamp": "2026-06-14T12:00:00+00:00", "balance": 300.0, "pnl_unrealized": 0.5}
    ], trade_store=None)
    out = collect_portfolio_history(bot)
    assert out[0]["equity"] == pytest.approx(300.5)  # sem trades → cap + unrealized


def test_history_equity_matches_summary_with_real_store(tmp_path, monkeypatch):
    """Bug 17/10: os fakes antigos tinham `status`/`exit_at`, mas o TradeStore real
    devolve `exit_time` (sem `status`) → realizado sempre 0 e a curva ficava em
    cap + unrealized (173,53) enquanto o card mostrava 177,22."""
    from trading_bot.core.trade_store import TradeStore
    from trading_bot.web.data import collect_summary

    monkeypatch.setattr(config, "ENVIRONMENT", "testnet")
    monkeypatch.setattr(config, "SIMULATED_BALANCE_USD", 173.49)
    store = TradeStore(str(tmp_path / "t.db"))
    for sym, exit_at, pnl in (
        ("ETHUSDT", "2026-10-16T12:00:00+00:00", 1.0),
        ("SOLUSDT", "2026-10-17T04:47:56+00:00", 2.5),
    ):
        store.record_open({"timestamp": "2026-10-16T10:00:00", "symbol": sym,
                           "side": "LONG", "qty": 1.0, "entry_price": 100.0})
        store.record_close(symbol=sym, side="LONG", entry_price=100.0, exit_price=101.0,
                           exit_at=exit_at, pnl_gross=pnl, pnl_net=pnl, fees=0.0,
                           close_reason="take_profit", strategy_name="primary")
    history = [
        {"timestamp": "2026-10-16T11:00:00+00:00", "balance": 173.49, "pnl_unrealized": 0.2},
        {"timestamp": "2026-10-16T13:00:00+00:00", "balance": 173.49, "pnl_unrealized": -0.1},
        {"timestamp": "2026-10-17T09:35:00+00:00", "balance": 173.49, "pnl_unrealized": 0.11},
    ]
    exchange = SimpleNamespace(
        get_daily_pnl_from_binance=lambda: {"total": 0.0},
        get_account_info=lambda: {"wallet_balance": 173.49, "unrealized_pnl": 0.11},
    )
    bot = SimpleNamespace(portfolio_history=history, trade_store=store,
                          exchange=exchange, initial_capital=173.49)

    eq = [p["equity"] for p in collect_portfolio_history(bot)]
    assert eq == pytest.approx([173.69, 174.39, 177.10])
    assert eq[-1] == pytest.approx(collect_summary(bot)["last_balance"])

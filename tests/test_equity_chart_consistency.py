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
        {"status": "closed", "exit_at": "2026-06-13T13:30:00+00:00", "pnl_net": 1.0},
        {"status": "closed", "exit_at": "2026-06-14T08:00:00+00:00", "pnl_net": 0.61},
        {"status": "open", "exit_at": None, "pnl_net": 0.0},
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
    trades = [{"status": "closed", "exit_at": "2026-06-13T13:30:00+00:00", "pnl_net": 1.61}]
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
    trades = [{"status": "closed", "exit_at": "2026-06-13T13:30:00+00:00", "pnl_net": 5.0}]
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

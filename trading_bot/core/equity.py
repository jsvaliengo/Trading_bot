"""Equity efetivo compartilhado por dashboard, Telegram e proteções de drawdown."""

from __future__ import annotations

from typing import Any


def simulated_balance(cfg: Any) -> float:
    """SIMULATED_BALANCE_USD quando ativo (só testnet, cap > 0); 0 caso contrário."""
    cap = float(getattr(cfg, "SIMULATED_BALANCE_USD", 0.0) or 0.0)
    return cap if bool(getattr(cfg, "USE_TESTNET", False)) and cap > 0 else 0.0


def effective_equity(cfg: Any, *, wallet: float, cumulative_realized: float, unrealized: float) -> float:
    """Equity efetivo. No testnet simulado o wallet vem cappado (constante), então o
    progresso é cap + realizado acumulado (SQLite) + não-realizado; senão wallet + não-realizado."""
    sim = simulated_balance(cfg)
    return (sim + cumulative_realized if sim else wallet) + unrealized

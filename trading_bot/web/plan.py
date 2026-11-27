"""
Plano de saque (/api/plan) — módulo puro, sem Flask.

Um plano congela, no momento da criação, o alvo da simulação (meta mensal, alvo diário, capital
necessário). `plan_state()` compara esse alvo com a equity atual e a taxa %/dia observada: progresso
até o capital, dias que faltam, e, depois de atingido, quanto já dá pra sacar sem mexer no capital
(equity − capital necessário) e quando o próximo saque mensal fica pronto.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any, Dict, Optional

from .simulation import _days_to


def _eta(today: date, days: Optional[int]) -> Optional[str]:
    return (today + timedelta(days=days)).isoformat() if days is not None else None


def plan_state(plan: Dict[str, Any], equity: float, daily_pct: float, today: Optional[date] = None) -> Dict[str, Any]:
    """Estado calculado do plano. `daily_pct` é fração (0.0033 = 0,33%/dia); ≤ 0 → sem ETA."""
    today = today or date.today()
    required, monthly = float(plan["required_capital_usd"]), float(plan["monthly_target_usd"])
    withdrawable = max(0.0, equity - required)
    days_left = _days_to(equity, required, daily_pct)
    # dias até equity ≥ required + monthly no ritmo atual (saque mês a mês acima do capital)
    next_days = _days_to(equity, required + monthly, daily_pct)
    return {
        "equity": round(equity, 2),
        "progress_pct": round(min(100.0, equity / required * 100), 2) if required > 0 else None,
        "remaining_usd": round(max(0.0, required - equity), 2),
        "days_left": days_left, "eta_date": _eta(today, days_left),
        "reached": equity >= required,
        "withdrawable_usd": round(withdrawable, 2),
        "ready_to_withdraw": withdrawable >= monthly > 0,
        "next_withdraw_eta_days": next_days, "next_withdraw_eta": _eta(today, next_days),
        "daily_pct": round(daily_pct * 100, 4),
    }


def observed_daily_pct(stats: Optional[Dict[str, Any]], equity: float) -> float:
    """Mesma taxa do cenário observado da simulação: média diária ÷ capital no início da amostra."""
    if not stats:
        return 0.0
    base = max(equity - stats["total_net"], 1e-9)
    pct = stats["mean_daily"] / base
    return pct if math.isfinite(pct) else 0.0

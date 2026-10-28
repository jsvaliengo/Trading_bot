"""
Métricas do forward test Donchian 4h (docs/FORWARD_TEST_DONCHIAN_4H.md), em
funções puras sobre as linhas de `trades` do SQLite. Sem I/O: o dashboard
(`data.py`) lê o TradeStore e passa as linhas; os testes passam listas.

Regra pré-registrada: termina no que vier por último entre 6 meses e 150 trades
fechados; passa se expectativa líquida/trade > 0 e drawdown máximo < 20%.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..core.trade_store import trading_day

STRATEGY = "donchian_4h"
# Registro de início (docs/FORWARD_TEST_DONCHIAN_4H.md, "Registro de início").
START_ISO = "2026-10-24T23:43:07+00:00"
TARGET_TRADES = 150
TARGET_DAYS = 182
DD_LIMIT_PCT = 20.0
# Expectativa da pesquisa (scripts/backtest_v2, TREND4H_RELATORIO.md), para comparar.
RESEARCH = {
    "win_rate": 29.0, "avg_win_pct": 17.8, "avg_loss_pct": -6.7,
    "trades_per_week": 10.0, "expectancy_pct": 0.41, "max_dd_pct": 39.0,
}
R_EDGES = [-2.0, -1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0, 3.0, 5.0]


def _f(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def parse_utc(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def initial_risk_usd(trade: Dict[str, Any]) -> Optional[float]:
    """Risco inicial em $ = |entrada − stop| × qty. None sem stop registrado."""
    entry, stop, qty = _f(trade.get("entry_price")), _f(trade.get("stop_loss")), _f(trade.get("qty"))
    if not entry or not stop or not qty or entry <= 0 or stop <= 0:
        return None
    risk = abs(entry - stop) * qty
    return risk if risk > 0 else None


def r_multiple(trade: Dict[str, Any], pnl: Optional[float] = None) -> Optional[float]:
    """pnl_net / risco inicial. A unidade certa para julgar trend following:
    poucos trades grandes pagam muitos stops de −1R."""
    risk = initial_risk_usd(trade)
    pnl = _f(trade.get("pnl_net")) if pnl is None else pnl
    if risk is None or pnl is None:
        return None
    return pnl / risk


def duration_hours(trade: Dict[str, Any]) -> Optional[float]:
    a, b = parse_utc(trade.get("opened_at")), parse_utc(trade.get("exit_at"))
    if a is None or b is None:
        return None
    return max(0.0, (b - a).total_seconds() / 3600.0)


def pnl_pct_of_notional(trade: Dict[str, Any]) -> Optional[float]:
    """pnl_net como % do notional de entrada (a unidade da pesquisa: +17,8% / −6,7%)."""
    pnl, entry, qty = _f(trade.get("pnl_net")), _f(trade.get("entry_price")), _f(trade.get("qty"))
    if pnl is None or not entry or not qty:
        return None
    notional = entry * qty
    return pnl / notional * 100.0 if notional > 0 else None


def bootstrap_ci(values: Sequence[float], n_boot: int = 2000, level: float = 0.95,
                 seed: int = 7) -> Optional[Tuple[float, float]]:
    """IC percentil da MÉDIA por bootstrap (semente fixa: o painel não treme a cada
    poll). None com menos de 5 trades: o intervalo não diria nada."""
    vals = [float(v) for v in values]
    n = len(vals)
    if n < 5:
        return None
    rng = random.Random(seed)
    means = sorted(sum(rng.choice(vals) for _ in range(n)) / n for _ in range(n_boot))
    lo_i = int((1 - level) / 2 * n_boot)
    hi_i = min(n_boot - 1, int((1 + level) / 2 * n_boot) - 1)
    return means[lo_i], means[hi_i]


def max_drawdown(equities: Iterable[float], start_peak: float = 0.0) -> Tuple[float, float]:
    """(drawdown máximo em %, pico) sobre a sequência; o pico começa em start_peak
    (o capital simulado no início do teste, antes do 1º trade)."""
    peak = float(start_peak or 0.0)
    worst = 0.0
    for eq in equities:
        peak = max(peak, eq)
        if peak > 0:
            worst = max(worst, (peak - eq) / peak * 100.0)
    return worst, peak


def r_histogram(rs: Iterable[Optional[float]], edges: Sequence[float] = R_EDGES) -> Dict[str, Any]:
    """Contagem por faixa de R. Faixas fixas: a forma da cauda direita é a tese
    (a pesquisa paga ~2,4 stops por vencedor)."""
    counts = [0] * (len(edges) + 1)
    n = 0
    for r in rs:
        if r is None:
            continue
        n += 1
        i = sum(1 for e in edges if r >= e)
        counts[i] += 1
    labels = [f"<{edges[0]:g}R"] + [f"{a:g}…{b:g}R" for a, b in zip(edges, edges[1:])] + [f"≥{edges[-1]:g}R"]
    return {"labels": labels, "counts": counts, "edges": list(edges), "n": n}


def daily_calendar(trades: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """P&L líquido por dia de trading (fuso de DAILY_PNL_TZ_OFFSET_HOURS, o mesmo
    do kill switch e do card "hoje"), cronológico."""
    days: Dict[str, Dict[str, Any]] = {}
    for t in trades:
        dt = parse_utc(t.get("exit_at"))
        if dt is None:
            continue
        day = trading_day(dt)
        d = days.setdefault(day, {"day": day, "net": 0.0, "trades": 0, "wins": 0})
        pnl = _f(t.get("pnl_net"), 0.0) or 0.0
        d["net"] = round(d["net"] + pnl, 4)
        d["trades"] += 1
        d["wins"] += 1 if pnl > 0 else 0
    return [days[k] for k in sorted(days)]


def enrich_trade(trade: Dict[str, Any]) -> Dict[str, Any]:
    """Campos derivados que a tabela mostra: R, risco, % do notional, duração."""
    return {
        "r_multiple": r_multiple(trade),
        "risk_usd": initial_risk_usd(trade),
        "pnl_pct": pnl_pct_of_notional(trade),
        "duration_hours": duration_hours(trade),
    }


def summarize(
    trades: Sequence[Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
    start_iso: str = START_ISO,
    sim_balance: float = 0.0,
    base_equity: Optional[float] = None,
    equity_now: Optional[float] = None,
    peak_equity: Optional[float] = None,
    dd_limit_pct: float = DD_LIMIT_PCT,
    target_trades: int = TARGET_TRADES,
    target_days: int = TARGET_DAYS,
) -> Dict[str, Any]:
    """Painel do forward test a partir dos trades FECHADOS da estratégia desde o início.

    `equity_now`/`peak_equity` vêm do bot (mesma equity do drawdown: cap + realizado
    + não-realizado); sem eles, a curva realizada (base + Σ pnl_net) é usada.
    `base_equity` é a equity no início do teste (cap + realizado pré-teste); default cap.
    """
    base = float(base_equity if base_equity is not None else sim_balance)
    now = now or datetime.now(timezone.utc)
    start = parse_utc(start_iso) or now
    rows = [t for t in trades if (parse_utc(t.get("exit_at")) or start) >= start]
    pnls = [_f(t.get("pnl_net"), 0.0) or 0.0 for t in rows]
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pcts = [p for p in (pnl_pct_of_notional(t) for t in rows) if p is not None]
    rs = [r_multiple(t) for t in rows]
    rs_valid = [r for r in rs if r is not None]

    elapsed_days = max(0.0, (now - start).total_seconds() / 86400.0)
    weeks = max(elapsed_days / 7.0, 1e-9)
    expectancy = sum(pnls) / n if n else None
    ci = bootstrap_ci(pnls) if n else None

    realized = 0.0
    curve = []
    for p in pnls:
        realized += p
        curve.append(base + realized)
    max_dd, peak_realized = max_drawdown(curve, start_peak=base)
    peak = max(float(peak_equity or 0.0), peak_realized)
    eq = equity_now if equity_now is not None else (curve[-1] if curve else base)
    dd_now = (peak - eq) / peak * 100.0 if peak > 0 else 0.0
    dd_now = max(0.0, dd_now)
    max_dd = max(max_dd, dd_now)
    dd_floor = peak * (1 - dd_limit_pct / 100.0) if peak > 0 else None

    if max_dd >= dd_limit_pct:
        verdict, label = "failed", f"Falhou: drawdown {max_dd:.1f}% ≥ {dd_limit_pct:g}%"
    elif n < 5:
        verdict, label = "inconclusive", f"Inconclusivo: n={n}"
    elif ci and ci[0] > 0:
        verdict, label = "ahead", "No rumo: IC 95% da expectativa inteiro acima de zero"
    elif ci and ci[1] < 0:
        verdict, label = "behind", "Atrás: IC 95% da expectativa inteiro abaixo de zero"
    else:
        verdict, label = "inconclusive", f"Inconclusivo: n={n}, IC cruza zero"

    done = n >= target_trades and elapsed_days >= target_days
    return {
        "strategy": STRATEGY,
        "start": start.isoformat(),
        "sim_balance": sim_balance,
        "base_equity": base,
        "elapsed_days": round(elapsed_days, 2),
        "target_days": target_days,
        "end_by_time": (start + timedelta(days=target_days)).isoformat(),
        "trades": n,
        "target_trades": target_trades,
        "trades_remaining": max(0, target_trades - n),
        "complete": done,
        "expectancy_usd": expectancy,
        "expectancy_ci95": list(ci) if ci else None,
        "expectancy_pct": (sum(pcts) / len(pcts)) if pcts else None,
        "win_rate": (len(wins) / n * 100.0) if n else None,
        "avg_win_usd": (sum(wins) / len(wins)) if wins else None,
        "avg_loss_usd": (sum(losses) / len(losses)) if losses else None,
        "avg_win_pct": (sum(p for p in pcts if p > 0) / len([p for p in pcts if p > 0])) if any(p > 0 for p in pcts) else None,
        "avg_loss_pct": (sum(p for p in pcts if p < 0) / len([p for p in pcts if p < 0])) if any(p < 0 for p in pcts) else None,
        "trades_per_week": (n / weeks) if n else 0.0,
        "avg_r": (sum(rs_valid) / len(rs_valid)) if rs_valid else None,
        "net_usd": sum(pnls),
        "fees_usd": sum(_f(t.get("fees"), 0.0) or 0.0 for t in rows),
        "funding_usd": sum(_f(t.get("funding"), 0.0) or 0.0 for t in rows),
        "equity_now": eq,
        "peak_equity": peak,
        "drawdown_pct": dd_now,
        "max_drawdown_pct": max_dd,
        "dd_limit_pct": dd_limit_pct,
        "dd_floor_equity": dd_floor,
        "dd_room_usd": (eq - dd_floor) if dd_floor is not None else None,
        "verdict": verdict,
        "verdict_label": label,
        "research": dict(RESEARCH),
        "r_histogram": r_histogram(rs),
        "calendar": daily_calendar(rows),
    }

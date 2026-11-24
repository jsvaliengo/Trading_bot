"""
Simulações de renda diária/mensal (/api/simulation) — módulo puro, sem Flask.

`measure()` lê do TradeStore os trades fechados do martingale desde o reset de banca (PNL_SINCE_UTC,
o mesmo piso do relatório de P&L) e resume o P&L líquido por dia UTC. `simulate()` extrapola esse
ritmo: quanto o capital atual rende, quanto capital o alvo exige e em quantos dias se chega lá
reinvestindo. É ferramenta de simulação sobre uma amostra curta, não previsão nem recomendação de
investimento — as premissas estão em ASSUMPTIONS e vão na resposta.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import timedelta, timezone
from typing import Any, Dict, List, Optional

from ..core.martingale import STRATEGY_TYPE as MARTINGALE_STRATEGY
from ..services.martingale_pair_score import COINTECH2U_SHEET, sheet_row_index
from .forward_test import parse_utc

MAX_DAYS = 3650
MIN_SAMPLE_DAYS = 14
FX_SENSITIVITY = (5.50, 6.00)
ASSUMPTIONS = (
    "Retorno diário = P&L líquido (fees da corretora já descontadas) ÷ capital no início da amostra; impostos não incluídos.",
    "Dia sem trade conta como $0 (cripto roda todo dia); a série usa o dia UTC, como o relatório de P&L.",
    "Cenários: conservador = percentil 25 dos dias, observado = média, otimista = percentil 75.",
    "Mensal = diário × dias/mês escolhidos (30 corridos ou 22 úteis); extrapolação linear de uma amostra curta.",
    "Tempo até o alvo reinveste 100% do lucro ao retorno diário observado (juros compostos), sem saques nem depósitos.",
    "Martingale tem cauda: uma sequência sem TP leva ao stop de portfólio e apaga vários dias de lucro.",
)


def quantile(values: List[float], q: float) -> float:
    """Percentil por interpolação linear de uma lista já ordenada (q em [0, 1])."""
    if not values:
        return 0.0
    pos = (len(values) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def measure(store, since_ms: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Resumo da amostra: série diária líquida (dias corridos), média/mediana/desvio, p25/p75, ciclos
    (trades fechados) por dia, P&L por ciclo, win rate. None sem trades fechados."""
    rows = store.closed_trades(MARTINGALE_STRATEGY, limit=100000)
    stamped = [(parse_utc(r.get("exit_at")), float(r.get("pnl_net") or 0.0)) for r in rows]
    stamped = [(t, pnl) for t, pnl in stamped if t is not None and (since_ms is None or t.timestamp() * 1000 >= since_ms)]
    if not stamped:
        return None
    by_day: Dict[str, float] = defaultdict(float)
    cycles_by_day: Dict[str, int] = defaultdict(int)
    for t, pnl in stamped:
        day = t.astimezone(timezone.utc).date()
        by_day[day.isoformat()] += pnl
        cycles_by_day[day.isoformat()] += 1
    first, last = min(t for t, _ in stamped).astimezone(timezone.utc).date(), max(t for t, _ in stamped).astimezone(timezone.utc).date()
    days = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
    series = [{"day": d, "net": round(by_day.get(d, 0.0), 4), "cycles": cycles_by_day.get(d, 0)} for d in days]
    nets = sorted(s["net"] for s in series)
    total = sum(nets)
    n = len(stamped)
    return {
        "from": first.isoformat(), "to": last.isoformat(), "sample_days": len(days), "traded_days": len(by_day),
        "cycles": n, "wins": sum(1 for _, p in stamped if p > 0), "win_rate": round(sum(1 for _, p in stamped if p > 0) / n * 100, 2),
        "total_net": round(total, 4), "mean_daily": round(total / len(days), 4), "median_daily": round(statistics.median(nets), 4),
        "stdev_daily": round(statistics.pstdev(nets), 4), "p25_daily": round(quantile(nets, 0.25), 4), "p75_daily": round(quantile(nets, 0.75), 4),
        "cycles_per_day": round(n / len(days), 3), "pnl_per_cycle": round(total / n, 4),
        "series": series,
    }


def _sheet_for(capital: Optional[float]) -> Optional[Dict[str, Any]]:
    """Linha da planilha CoinTech2u pra esse capital (1ª ordem, stop, moedas); None abaixo da 1ª faixa."""
    idx = sheet_row_index(capital)
    if idx is None:
        return None
    label, min_bank, _, first_long, _, lev, stop, double_first, coins = COINTECH2U_SHEET[idx]
    return {"label": label, "min_bank": min_bank, "first_order": first_long, "leverage": lev, "stop": stop,
            "double_first": double_first, "coins": coins}


def _days_to(capital: float, required: Optional[float], daily_pct: float) -> Optional[int]:
    """Dias compondo `daily_pct` até capital ≥ required; 0 se já chegou; None se retorno ≤ 0 ou > MAX_DAYS."""
    if required is None:
        return None
    if capital >= required:
        return 0
    if daily_pct <= 0 or capital <= 0:
        return None
    days = math.ceil(math.log(required / capital) / math.log1p(daily_pct))
    return days if days <= MAX_DAYS else None


def simulate(stats: Dict[str, Any], capital: float, target_daily: Optional[float] = None, days_per_month: int = 30,
             fx: Optional[float] = None, target_brl: Optional[float] = None, stop_usd: float = 0.0,
             base_capital: Optional[float] = None) -> Dict[str, Any]:
    """Projeções a partir de `stats` (de measure()). `capital` é o capital a projetar (equity atual por
    padrão); `base_capital` é o capital sobre o qual a amostra rendeu (default: capital − P&L da amostra).
    Alvo diário em US$ = target_daily, senão target_brl ÷ fx ÷ days_per_month."""
    days_per_month = max(1, int(days_per_month))
    base = base_capital if base_capital and base_capital > 0 else max(capital - stats["total_net"], 1e-9)
    if target_daily is None:
        target_daily = (target_brl / fx / days_per_month) if (target_brl and fx) else 0.0
    to_brl = (lambda usd: round(usd * fx, 2)) if fx else (lambda usd: None)
    scenarios = []
    for key, label, usd in (("conservative", "Conservador (p25)", stats["p25_daily"]),
                            ("observed", "Observado (média)", stats["mean_daily"]),
                            ("optimistic", "Otimista (p75)", stats["p75_daily"])):
        pct = usd / base
        daily = capital * pct
        required = target_daily / pct if pct > 0 and target_daily > 0 else None
        scenarios.append({
            "key": key, "label": label, "daily_pct": round(pct * 100, 4),
            "daily_usd": round(daily, 2), "monthly_usd": round(daily * days_per_month, 2),
            "daily_brl": to_brl(daily), "monthly_brl": to_brl(daily * days_per_month),
            "required_capital": round(required, 2) if required else None,
            "days_to_target": _days_to(capital, required, pct),
            "sheet": _sheet_for(required),
            "stop_pct_of_required": round(stop_usd / required * 100, 2) if required and stop_usd > 0 else None,
        })
    obs = scenarios[1]
    horizon = max(30, min(365, obs["days_to_target"] or 365))
    for s in scenarios:   # curva de equity composta no horizonte do cenário observado (gráfico)
        r = s["daily_pct"] / 100
        s["curve"] = [round(capital * (1 + r) ** d, 2) for d in range(horizon + 1)]
    per_cycle = stats["pnl_per_cycle"]
    cycles_needed = round(target_daily / per_cycle, 2) if per_cycle > 0 and target_daily > 0 else None
    fx_values = sorted(set(FX_SENSITIVITY + ((round(fx, 4),) if fx else ())))
    out = {
        "capital": round(capital, 2), "base_capital": round(base, 2), "days_per_month": days_per_month, "fx": fx,
        "target": {"daily_usd": round(target_daily, 2), "monthly_usd": round(target_daily * days_per_month, 2),
                   "daily_brl": to_brl(target_daily), "monthly_brl": to_brl(target_daily * days_per_month),
                   "brl_input": target_brl},
        "fx_sensitivity": [{"fx": v, "daily_usd": round(target_brl / v / days_per_month, 2)} for v in fx_values] if target_brl else [],
        "scenarios": scenarios,
        "required": {"capital": obs["required_capital"], "days_to_target": obs["days_to_target"], "sheet": obs["sheet"],
                     "cycles_needed_per_day": cycles_needed, "cycles_observed_per_day": stats["cycles_per_day"],
                     "pnl_per_cycle": per_cycle},
        "risks": {"stop_usd": stop_usd, "stop_pct_of_capital": round(stop_usd / capital * 100, 2) if stop_usd > 0 and capital > 0 else None,
                  "stop_pct_of_required": obs["stop_pct_of_required"], "sample_days": stats["sample_days"],
                  "small_sample": stats["sample_days"] < MIN_SAMPLE_DAYS},
        "assumptions": list(ASSUMPTIONS),
    }
    out["recommendations"] = _recommendations(out, stats)
    return out


def _money(v: float) -> str:
    return f"${v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _recommendations(sim: Dict[str, Any], stats: Dict[str, Any]) -> List[str]:
    obs, req, risks, tgt = sim["scenarios"][1], sim["required"], sim["risks"], sim["target"]
    target = tgt["daily_usd"]
    rec: List[str] = []
    if risks["small_sample"]:
        rec.append(f"Amostra curta: {stats['sample_days']} dia(s) de trades. Com menos de {MIN_SAMPLE_DAYS} dias a média diária "
                   "muda muito a cada dia novo; trate os números como ordem de grandeza.")
    if obs["daily_pct"] <= 0:
        rec.append(f"O retorno observado é {obs['daily_pct']:.2f}%/dia (não positivo): nenhum capital alcança {_money(target)}/dia "
                   "nesse ritmo. Antes de simular capital, a estratégia precisa fechar o período no positivo.")
        return rec
    if target > 0:
        rec.append(f"Com o retorno observado de {obs['daily_pct']:.2f}%/dia, {_money(target)}/dia exige ~{_money(req['capital'])} de capital; "
                   f"no capital atual ({_money(sim['capital'])}) a expectativa é {_money(obs['daily_usd'])}/dia "
                   f"({_money(obs['monthly_usd'])}/mês em {sim['days_per_month']} dias).")
        if req["days_to_target"] == 0:
            rec.append("O capital atual já cobre o alvo no cenário observado.")
        elif req["days_to_target"] is not None:
            rec.append(f"Para chegar em {_money(req['capital'])} reinvestindo todo o lucro: ~{req['days_to_target']} dias "
                       f"(cenário observado; conservador: {sim['scenarios'][0]['days_to_target'] or 'não alcança'}, "
                       f"otimista: {sim['scenarios'][2]['days_to_target'] or 'não alcança'}).")
        else:
            rec.append(f"Reinvestindo ao ritmo observado o capital não chega a {_money(req['capital'])} em {MAX_DAYS} dias.")
        if req["cycles_needed_per_day"] is not None:
            rec.append(f"Alternativa no capital atual: {req['cycles_needed_per_day']:g} ciclos/dia a {_money(req['pnl_per_cycle'])}/ciclo, "
                       f"vs. {req['cycles_observed_per_day']:g} ciclos/dia observados.")
        if req["sheet"]:
            sh = req["sheet"]
            rec.append(f"Faixa da planilha para ~{_money(req['capital'])}: banca {sh['label']} → 1ª ordem ${sh['first_order']}, "
                       f"stop ${sh['stop']}, {sh['coins']} moeda(s).")
    if risks["stop_pct_of_capital"] is not None:
        rec.append(f"Risco: o stop de portfólio (${risks['stop_usd']:g}) vale {risks['stop_pct_of_capital']:.0f}% do capital atual"
                   + (f" e {risks['stop_pct_of_required']:.0f}% do capital necessário" if risks["stop_pct_of_required"] is not None else "")
                   + "; um stop apaga " + (f"~{risks['stop_usd'] / obs['daily_usd']:.0f} dia(s) de lucro observado." if obs["daily_usd"] > 0 else "o lucro."))
    rec.append("Valores líquidos de fees da corretora; impostos não incluídos. Simulação, não recomendação de investimento.")
    return rec

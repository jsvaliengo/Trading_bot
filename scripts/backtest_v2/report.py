"""Gera report.md (tabelas) a partir do summary.json."""
from __future__ import annotations

from pathlib import Path


def _f(x, nd=2, pct=False):
    if x is None or x != x:
        return "—"
    return f"{x:+.{nd}f}{'%' if pct else ''}" if pct else f"{x:.{nd}f}"


def write_report(summaries: dict, path: Path) -> None:
    L = ["# backtest_v2 — resultados OUT-OF-SAMPLE (walk-forward 6m treino / 3m teste)", ""]
    L.append("| estratégia | período OOS | pares | trades | win% | ganho méd | perda méd | exp/trade (IC95%) | "
             "retorno | DD máx | fees $ | funding $ | B&H eq.w. | edge? |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name, s in summaries.items():
        lo, hi = s["exp_ci95_pct"]
        L.append(
            f"| {name} | {s['oos_range'][0]}→{s['oos_range'][1]} | {s['symbols_tested']} | {s['oos_trades']} | "
            f"{s['win_rate'] * 100:.1f}% | {_f(s['avg_win_pct'], 2, True)} | {_f(s['avg_loss_pct'], 2, True)} | "
            f"{_f(s['exp_pct'], 3, True)} ({_f(lo, 3, True)}; {_f(hi, 3, True)}) | {_f(s['net_return_pct'], 1, True)} | "
            f"{s['max_dd_pct']:.1f}% | {s['fees_paid']:.0f} | {s['funding_pnl']:+.0f} | "
            f"{_f(s.get('buy_hold_eqw_pct'), 1, True)} | {'SIM' if s['evidence_of_edge'] else 'não'} |")
    L += ["", "Banca inicial $1000 por estratégia. exp/trade = retorno líquido médio por trade sobre o notional "
          "(após fees, slippage e funding). Edge = IC95% inferior > 0 e ≥ 200 trades OOS.", ""]
    for name, s in summaries.items():
        L += [f"## {name}", "",
              f"- sinais brutos: {s['raw_signals']} | saídas: {s['exits']} | candidatos barrados por teto: "
              f"{s['skipped_capacity']}",
              f"- exp bruta (antes de custos): {_f(s['gross_exp_pct'], 3, True)} | payoff: {_f(s['payoff'])}",
              "", "| fold teste | escolhido no treino | retorno treino |", "|---|---|---|"]
        for fd in s["folds"]:
            L.append(f"| {fd['test'][0]}→{fd['test'][1]} | {fd['chosen'] or '(sem parâmetros)'} | "
                     f"{fd['train_return_pct']:+.1f}% ({fd['train_trades']} tr) |")
        for title, key in (("lado", "by_side"), ("ano (saída)", "by_year"), ("símbolo", "by_symbol")):
            L += ["", f"| {title} | trades | win% | exp/trade | P&L $ |", "|---|---|---|---|---|"]
            for k, g in s[key].items():
                L.append(f"| {k} | {g['trades']} | {g['win_rate'] * 100:.0f}% | {g['exp_pct']:+.3f}% | {g['pnl']:+.1f} |")
        L.append("")
    path.write_text("\n".join(L))

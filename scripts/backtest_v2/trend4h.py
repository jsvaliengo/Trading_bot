"""Trend-following 4h pré-registrado (scripts/backtest_v2/TREND4H_PREREG.md).

    PYTHONPATH=. .venv/bin/python -I -m scripts.backtest_v2.trend4h download
    PYTHONPATH=. .venv/bin/python -I -m scripts.backtest_v2.trend4h run

Regras, grid, sizing, custos, folds e critérios vêm do pré-registro. Reusa o
motor (simulate_trade/run_portfolio), make_folds, summarize e o bootstrap em
blocos; o que é novo aqui é o universo point-in-time, ATR, os sinais das duas
variantes e as métricas de carteira.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import time
from typing import Dict, List, Sequence, Tuple

import numpy as np

from . import vision
from .data import OUT_DIR, Bars, ms_to_date, to_ms
from .engine import Costs, PortfolioConfig, SymbolData, run_portfolio, simulate_trade
from .experiments import day_block_ci
from .pipeline import N_BOOT, SEED, add_months, make_folds, summarize, write_trades_csv
from .strategies import LONG, SHORT, DonchianBreakout, SignalSet

logger = logging.getLogger("backtest_v2.trend4h")

DAY = 86_400_000
WEEK = 7 * DAY
DEV_START, HOLD_START, END = to_ms("2020-01-01"), to_ms("2025-07-01"), to_ms("2026-10-01")
TOP_N, MIN_DAYS, VOL_DAYS = 30, 60, 30
ATR_N, SIGMA_N, Z_ENTRY = 20, 180, 0.5
COSTS = Costs()
PCFG = PortfolioConfig(initial_equity=10_000.0, sizing="risk", risk_pct=0.5, max_pos_mult=1.0,
                       max_gross_mult=3.0, max_concurrent=10, sl_buffer=1.0)
GRIDS = {
    "don": [{"v": "don", "n": n, "m": m, "k": k} for n, m in ((20, 10), (40, 20), (55, 20), (100, 50)) for k in (2, 3)],
    "tsmom": [{"v": "tsmom", "L": L, "k": k} for L in (42, 84, 180, 360) for k in (3, 5)],
}
OUT = OUT_DIR / "trend4h"


def ckey(cell: dict) -> str:
    return json.dumps(cell, sort_keys=True)


# ───────────────────────── universo point-in-time ─────────────────────────
def rebalance_times(t0: int, t1: int) -> np.ndarray:
    """Segundas 00:00 UTC em [t0, t1). Epoch (1970-01-01) foi quinta: segunda = +4 dias."""
    first = t0 + (-(t0 - 4 * DAY) % WEEK)
    return np.arange(first, t1, WEEK, dtype=np.int64)


def universe_scores(daily: Dict[str, np.ndarray], times: np.ndarray) -> Tuple[List[str], np.ndarray]:
    """Score (n_times × n_syms) = quote_volume dos 30 dias antes de T; -inf se inelegível.
    Usa só barras diárias com close_time < T."""
    syms = sorted(daily)
    sc = np.full((len(times), len(syms)), -np.inf)
    for j, s in enumerate(syms):
        a = daily[s]
        if not len(a):
            continue
        ot = a[:, 0].astype(np.int64)
        ct = ot + DAY - 1
        cs = np.concatenate([[0.0], np.cumsum(a[:, 6])])
        k = np.searchsorted(ct, times, "left")                 # nº de barras com close_time < T
        last_ok = (k > 0) & (ct[np.maximum(k - 1, 0)] == times - 1)
        ok = (k >= MIN_DAYS) & last_ok
        lo = np.searchsorted(ot, times - VOL_DAYS * DAY, "left")
        sc[ok, j] = (cs[k] - cs[lo])[ok]
    return syms, sc


class Universe:
    def __init__(self, times: np.ndarray, members: List[frozenset]):
        self.times, self.members = times, members

    @classmethod
    def build(cls, daily: Dict[str, np.ndarray], t0: int, t1: int, top_n: int = TOP_N) -> "Universe":
        times = rebalance_times(t0, t1)
        syms, sc = universe_scores(daily, times)
        mem = []
        for row in sc:
            ok = np.nonzero(np.isfinite(row))[0]
            top = ok[np.argsort(-row[ok], kind="stable")[:top_n]]
            mem.append(frozenset(syms[i] for i in top))
        return cls(times, mem)

    def contains(self, sym: str, t: np.ndarray) -> np.ndarray:
        r = np.searchsorted(self.times, t, "right") - 1
        return np.array([k >= 0 and sym in self.members[k] for k in r.tolist()], dtype=bool)

    def ever(self) -> List[str]:
        return sorted(set().union(*self.members)) if self.members else []


# ───────────────────────── indicadores e sinais (barras fechadas) ─────────────────────────
def atr_wilder(h: np.ndarray, lw: np.ndarray, c: np.ndarray, n: int = ATR_N) -> np.ndarray:
    tr = np.maximum(h - lw, np.maximum(np.abs(h - np.roll(c, 1)), np.abs(lw - np.roll(c, 1))))
    if len(tr):
        tr[0] = h[0] - lw[0]
    out = np.full(len(tr), np.nan)
    if len(tr) < n:
        return out
    a = tr[:n].mean()
    out[n - 1] = a
    for i in range(n, len(tr)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def donchian_signals(b: Bars, n: int, m: int):
    """(entry_long, entry_short, exit_long, exit_short) por barra fechada."""
    prior = DonchianBreakout._prior
    c = b.close
    with np.errstate(invalid="ignore"):
        return (c > prior(b.high, n, np.max), c < prior(b.low, n, np.min),
                c < prior(b.low, m, np.min), c > prior(b.high, m, np.max))


def tsmom_z(c: np.ndarray, L: int) -> np.ndarray:
    sigma_n = SIGMA_N
    lc = np.log(c)
    z = np.full(len(c), np.nan)
    r = np.diff(lc, prepend=np.nan)
    if len(c) <= max(L, sigma_n):
        return z
    sd = np.full(len(c), np.nan)
    sd[sigma_n:] = np.lib.stride_tricks.sliding_window_view(r[1:], sigma_n).std(axis=1, ddof=1)[:len(c) - sigma_n]
    ret = np.full(len(c), np.nan)
    ret[L:] = lc[L:] - lc[:-L]
    with np.errstate(invalid="ignore", divide="ignore"):
        z = ret / (sd * np.sqrt(L))
    return z


def tsmom_signals(b: Bars, L: int):
    z = tsmom_z(b.close, L)
    zp = np.roll(z, 1)
    zp[:1] = np.nan
    with np.errstate(invalid="ignore"):
        return (z > Z_ENTRY) & (zp <= Z_ENTRY), (z < -Z_ENTRY) & (zp >= -Z_ENTRY), z < 0, z > 0


def cell_signals(b: Bars, cell: dict):
    if cell["v"] == "don":
        return donchian_signals(b, cell["n"], cell["m"])
    return tsmom_signals(b, cell["L"])


# ───────────────────────── livro: sinais de todas as células + hook do motor ─────────────────────────
class Book:
    """Sinais por célula; o portfólio recebe a UNIÃO e o hook descarta o que não é
    da célula vigente no `schedule` (o walk-forward troca de célula entre folds)."""

    def __init__(self, bars: Dict[str, Bars], funding: Dict[str, np.ndarray], uni: Universe, grid: List[dict]):
        self.bars, self.sig, self.atr = bars, {}, {}
        self.sym_data: Dict[str, SymbolData] = {}
        for s, b in bars.items():
            self.atr[s] = atr_wilder(b.high, b.low, b.close)
            member = uni.contains(s, b.close_time)
            ok = member & np.isfinite(self.atr[s])
            pairs = set()
            for cell in grid:
                el, es, xl, xs = cell_signals(b, cell)
                il, is_ = np.nonzero(el & ok)[0], np.nonzero(es & ok)[0]
                ps = {(int(i), LONG) for i in il} | {(int(i), SHORT) for i in is_}
                self.sig.setdefault(ckey(cell), {})[s] = (ps, xl, xs)
                pairs |= ps
            srt = sorted(pairs)
            self.sym_data[s] = SymbolData(b, SignalSet(np.array([p[0] for p in srt], np.int64),
                                                       np.array([p[1] for p in srt], np.int8)), funding.get(s))

    def simulate(self, sym, i, side, params, end_idx):
        ps, xl, xs = self.sig[ckey(params)][sym]
        if (i, side) not in ps:
            return None
        b = self.bars[sym]
        sl_pct = params["k"] * self.atr[sym][i] / b.close[i] * 100
        tr = simulate_trade(b, i, side, {"sl_pct": sl_pct}, COSTS, end_idx, self.sym_data[sym].funding,
                            xl if side == LONG else xs, sym)
        if tr is not None:
            tr.params = dict(params)
        return tr

    def run(self, schedule: Sequence[tuple], t0: int, t1: int, curve: bool = True):
        return run_portfolio(self.sym_data, schedule, t0, t1, COSTS, PCFG, with_curve=curve, simulate=self.simulate)


def select(book: Book, grid: List[dict], tr0: int, tr1: int) -> Tuple[dict, List[dict]]:
    """Maior retorno de carteira no treino; empate → 1ª célula do grid."""
    scores = []
    for c in grid:
        r = book.run([(tr0, tr1, c)], tr0, tr1, curve=False)
        scores.append({"cell": c, "ret_pct": (r.final_equity / r.initial_equity - 1) * 100, "n": len(r.trades)})
    best = max(range(len(grid)), key=lambda k: (scores[k]["ret_pct"], -k))
    return grid[best], scores


def walk_forward(book: Book, grid: List[dict]):
    folds = make_folds(DEV_START, HOLD_START, 12, 3)
    schedule, sel = [], []
    for tr0, tr1, te1 in folds:
        cell, scores = select(book, grid, tr0, tr1)
        schedule.append((tr1, te1, cell))
        sel.append({"train": [ms_to_date(tr0), ms_to_date(tr1)], "test": [ms_to_date(tr1), ms_to_date(te1)],
                    "chosen": cell, "grid": scores})
    return book.run(schedule, folds[0][1], HOLD_START), sel


# ───────────────────────── métricas de carteira ─────────────────────────
def daily_equity(curve: np.ndarray, t0: int, t1: int, initial: float) -> Tuple[np.ndarray, np.ndarray]:
    days = np.arange(t0, t1 + 1, DAY, dtype=np.int64)
    k = np.searchsorted(curve[:, 0], days, "right") - 1
    return days, np.where(k >= 0, curve[np.maximum(k, 0), 1], initial)


def sharpe(r: np.ndarray) -> float:
    sd = r.std(ddof=1) if len(r) > 1 else 0.0
    return float(r.mean() / sd * np.sqrt(365)) if sd > 0 else float("nan")


def sharpe_week_block_ci(r: np.ndarray, days: np.ndarray, n_boot: int = N_BOOT, seed: int = SEED):
    """IC95% do Sharpe reamostrando SEMANAS inteiras de retornos diários."""
    _, inv = np.unique(days // WEEK, return_inverse=True)
    s1, s2 = np.bincount(inv, weights=r), np.bincount(inv, weights=r * r)
    n = np.bincount(inv).astype(float)
    idx = np.random.default_rng(seed).integers(0, len(n), size=(n_boot, len(n)))
    N, S1, S2 = n[idx].sum(1), s1[idx].sum(1), s2[idx].sum(1)
    mu = S1 / N
    var = (S2 - N * mu * mu) / (N - 1)
    sh = mu / np.sqrt(np.maximum(var, 1e-300)) * np.sqrt(365)
    return float(np.quantile(sh, 0.025)), float(np.quantile(sh, 0.975))


def year_returns(days: np.ndarray, eq: np.ndarray) -> Dict[str, float]:
    out = {}
    y0, y1 = (dt.datetime.fromtimestamp(int(x) / 1000, tz=dt.timezone.utc).year for x in (days[0], days[-1]))
    for y in range(y0, y1 + 1):
        a, b = max(days[0], to_ms(f"{y}-01-01")), min(days[-1], to_ms(f"{y + 1}-01-01"))
        if b <= a:
            continue
        ea, eb = eq[np.searchsorted(days, a)], eq[np.searchsorted(days, b)]
        out[str(y)] = float((eb / ea - 1) * 100)
    return out


def metrics(res, t0: int, t1: int, label: str, lean: bool = False) -> dict:
    s = summarize(res, label)
    tr = res.trades
    nf = np.array([t.net_frac for t in tr]) if tr else np.zeros(0)
    lo, hi = day_block_ci(nf, np.array([t.entry_time // WEEK for t in tr], np.int64))
    days, eq = daily_equity(res.equity_curve, t0, t1, res.initial_equity)
    r = eq[1:] / eq[:-1] - 1
    yrs = (t1 - t0) / (365 * DAY)
    cagr = float(((eq[-1] / eq[0]) ** (1 / yrs) - 1) * 100) if eq[-1] > 0 else -100.0
    dd = float(np.max(1 - eq / np.maximum.accumulate(eq)) * 100)
    pnl = np.sort([t.pnl for t in tr])[::-1] if tr else np.zeros(0)
    nf_sorted = np.sort(nf)[::-1]
    out = {
        "trades": len(tr), "win_pct": s["win_rate"] * 100, "avg_win_pct": s["avg_win_pct"],
        "avg_loss_pct": s["avg_loss_pct"], "exp_pct": s["exp_pct"], "exp_ci95_pct": [lo * 100, hi * 100],
        "gross_exp_pct": s["gross_exp_pct"], "ret_pct": s["net_return_pct"], "cagr_pct": cagr, "max_dd_pct": dd,
        "sharpe": sharpe(r), "calmar": cagr / dd if dd > 0 else float("nan"),
        "years": year_returns(days, eq),
    }
    if lean:
        return out
    out.update({
        "sharpe_ci95": list(sharpe_week_block_ci(r, days[1:])),
        "by_side": s["by_side"], "by_year_trades": s["by_year"], "exits": s["exits"],
        "pnl": float(pnl.sum()), "pnl_ex_top5": float(pnl[5:].sum()), "top5_pnl": pnl[:5].tolist(),
        "exp_ex_top5_pct": float(nf_sorted[5:].mean() * 100) if len(nf) > 5 else float("nan"),
        "fees_paid": s["fees_paid"], "funding_pnl": s["funding_pnl"], "fee_pct_per_trade": float(np.mean([t.fee_frac for t in tr]) * 100) if tr else float("nan"),
        "funding_pct_per_trade": float(np.mean([t.funding_frac for t in tr]) * 100) if tr else float("nan"),
        "turnover_x_per_year": float(sum(2 * t.notional for t in tr) / eq.mean() / yrs),
        "avg_hold_days": float(np.mean([(t.exit_time - t.entry_time) / DAY for t in tr])) if tr else float("nan"),
        "symbols_traded": len({t.symbol for t in tr}), "skipped_capacity": res.skipped_capacity,
        "initial_equity": res.initial_equity, "final_equity": res.final_equity,
    })
    return out


def verdict(wf: dict, ho: dict, grid_rows: List[dict]) -> dict:
    yrs = wf["years"]
    c = {
        "C1_exp_ci_or_sharpe": wf["exp_ci95_pct"][0] > 0 or (wf["sharpe"] >= 0.8 and wf["sharpe_ci95"][0] > 0),
        "C2_years_majority": sum(1 for v in yrs.values() if v > 0) >= 3,
        "C3_holdout_positive": ho["ret_pct"] > 0 and ho["exp_pct"] > 0,
        "C4_dd_lt_35": wf["max_dd_pct"] < 35 and ho["max_dd_pct"] < 35,
        "C5_robust_grid": sum(1 for g in grid_rows if g["exp_pct"] > 0) >= 6
        and float(np.nanmedian([g["sharpe"] for g in grid_rows])) > 0,
        "C6_ex_top5_positive": wf["pnl_ex_top5"] > 0,
        "C7_min_200_trades": wf["trades"] >= 200,
    }
    c["PASS"] = all(c.values())
    return c


# ───────────────────────── comandos ─────────────────────────
def _daily_all() -> Dict[str, np.ndarray]:
    out = {}
    for s in vision.list_symbols():
        if vision.is_candidate(s):
            try:
                out[s] = vision.load_klines(s, "1d")
            except FileNotFoundError:
                pass
    return out


def cmd_download(args):
    cands = [s for s in vision.list_symbols() if vision.is_candidate(s)]
    vision.download(cands, "klines", "1d", args.threads)
    uni = Universe.build(_daily_all(), DEV_START, END)
    ever = uni.ever()
    logger.info("%d candidatos, %d entram no top-%d ao menos uma vez", len(cands), len(ever), TOP_N)
    vision.download(ever, "klines", "4h", args.threads)
    vision.download(ever, "fundingRate", "", args.threads)


def coverage(daily: Dict[str, np.ndarray], uni: Universe, bars: Dict[str, Bars], fund: Dict[str, np.ndarray]) -> dict:
    ever = uni.ever()
    last_day = {s: int(daily[s][-1, 0]) for s in ever if len(daily[s])}
    dead = sorted(s for s, t in last_day.items() if t < END - 2 * DAY)
    gaps = {s: int(np.sum(np.diff(b.open_time) != 4 * 3_600_000)) for s, b in bars.items()}
    return {
        "candidates_1d": len(daily), "ever_top30": len(ever), "dead_or_delisted_in_ever": len(dead),
        "dead_symbols": {s: ms_to_date(last_day[s]) for s in dead},
        "first_4h": min(ms_to_date(int(b.open_time[0])) for b in bars.values() if len(b)),
        "bars_4h_total": int(sum(len(b) for b in bars.values())),
        "symbols_4h_gaps": {s: g for s, g in gaps.items() if g}, "funding_events": int(sum(len(f) for f in fund.values())),
        "symbols_without_funding": sorted(s for s in ever if not len(fund.get(s, []))),
        "universe_weeks": len(uni.times),
        "universe_size_min_max": [min(len(m) for m in uni.members), max(len(m) for m in uni.members)],
        "universe_first_full_week": next((ms_to_date(int(t)) for t, m in zip(uni.times, uni.members) if len(m) >= TOP_N), None),
    }


def cmd_run(args):
    OUT.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    daily = _daily_all()
    uni = Universe.build(daily, DEV_START, END)
    ever = uni.ever()
    bars = {s: vision.load_bars(s, "4h") for s in ever}
    bars = {s: b for s, b in bars.items() if len(b) > max(ATR_N, 360) + 2}
    fund = {s: vision.load_funding(s) for s in bars}
    results = {"coverage": coverage(daily, uni, bars, fund), "prereg_commit": args.prereg, "variants": {}}
    oos0 = add_months(DEV_START, 12)
    for v in args.variants.split(","):
        grid = GRIDS[v]
        book = Book(bars, fund, uni, grid)
        logger.info("%s: livro pronto (%.0fs)", v, time.time() - t0)
        wf_res, sel = walk_forward(book, grid)
        wf = metrics(wf_res, oos0, HOLD_START, f"{v}_wf")
        write_trades_csv(OUT / f"wf_trades_{v}.csv", wf_res.trades)
        np.savetxt(OUT / f"wf_equity_{v}.csv", wf_res.equity_curve, delimiter=",", fmt=["%d", "%.4f"],
                   header="time_ms,equity", comments="")
        hcell, hscores = select(book, grid, add_months(HOLD_START, -12), HOLD_START)
        ho_res = book.run([(HOLD_START, END, hcell)], HOLD_START, END)
        ho = metrics(ho_res, HOLD_START, END, f"{v}_holdout")
        write_trades_csv(OUT / f"holdout_trades_{v}.csv", ho_res.trades)
        rows = []
        for c in grid:
            r = book.run([(oos0, HOLD_START, c)], oos0, HOLD_START)
            rows.append({"cell": c, **metrics(r, oos0, HOLD_START, ckey(c), lean=True)})
        results["variants"][v] = {"wf": wf, "folds": sel, "holdout_cell": hcell, "holdout_train_scores": hscores,
                                  "holdout": ho, "grid_fixed_oos": rows, "verdict": verdict(wf, ho, rows)}
        (OUT / "results.json").write_text(json.dumps(results, indent=2, sort_keys=True, default=float))
        logger.info("%s: WF %d trades exp %.3f%% [%.3f, %.3f] Sharpe %.2f DD %.1f%% | holdout ret %.1f%% | %s (%.0fs)",
                    v, wf["trades"], wf["exp_pct"], *wf["exp_ci95_pct"], wf["sharpe"], wf["max_dd_pct"],
                    ho["ret_pct"], results["variants"][v]["verdict"]["PASS"], time.time() - t0)
    print(f"saídas em {OUT}")


def _f(x, nd=2, pct=True):
    return "n/d" if x is None or x != x else f"{x:+.{nd}f}{'%' if pct else ''}"


def _row(name: str, m: dict) -> str:
    lo, hi = m["exp_ci95_pct"]
    sh = m.get("sharpe_ci95")
    sd = m.get("by_side", {})
    side = " / ".join(f"{_f(sd.get(k, {}).get('exp_pct'), 3)} ({sd.get(k, {}).get('trades', 0)})" for k in ("LONG", "SHORT"))
    return (f"| {name} | {m['trades']} | {m['win_pct']:.1f}% | {_f(m['avg_win_pct'])} / {_f(m['avg_loss_pct'])} | "
            f"**{_f(m['exp_pct'], 3)}** ({_f(lo, 3)}; {_f(hi, 3)}) | {_f(m['gross_exp_pct'], 3)} | {_f(m['cagr_pct'], 1)} | "
            f"{m['max_dd_pct']:.1f}% | {m['sharpe']:.2f}" + (f" ({sh[0]:.2f}; {sh[1]:.2f})" if sh else "")
            + f" | {_f(m['calmar'], 2, False)} | {side} |")


def cmd_report(args):
    res = json.loads((OUT / "results.json").read_text())
    head = ("| variante | trades | win% | ganho / perda méd | exp/trade (IC95% bloco-semana) | exp bruta | CAGR | DD máx | "
            "Sharpe (IC95%) | Calmar | LONG / SHORT exp (n) |\n|---|---|---|---|---|---|---|---|---|---|---|")
    cov = {k: v for k, v in res["coverage"].items() if k != "dead_symbols"}
    L = ["## Cobertura", "", "```json", json.dumps(cov, indent=1, sort_keys=True), "```", ""]
    L += ["## Walk-forward OOS (2021-01 → 2025-07)", "", head]
    L += [_row(v, r["wf"]) for v, r in res["variants"].items()]
    L += ["", "## Holdout (2025-07 → 2026-10)", "", head]
    L += [_row(f"{v} {json.dumps(r['holdout_cell'])}", r["holdout"]) for v, r in res["variants"].items()]
    for v, r in res["variants"].items():
        w = r["wf"]
        L += ["", f"### {v}: detalhes", "",
              "Retorno da carteira por ano (WF): " + ", ".join(f"{y} {_f(x, 1)}" for y, x in w["years"].items()),
              "", "Retorno por ano (holdout): " + ", ".join(f"{y} {_f(x, 1)}" for y, x in r["holdout"]["years"].items()),
              "", f"P&L WF {w['pnl']:+.0f} | sem os 5 melhores {w['pnl_ex_top5']:+.0f} | 5 melhores "
              + ", ".join(f"{x:+.0f}" for x in w["top5_pnl"]) + f" | exp sem top5 {_f(w['exp_ex_top5_pct'], 3)}",
              "", f"Fees {w['fees_paid']:.0f} ({w['fee_pct_per_trade']:.3f}%/trade) | funding {w['funding_pnl']:+.0f} "
              f"({_f(w['funding_pct_per_trade'], 3)}/trade) | turnover {w['turnover_x_per_year']:.0f}x/ano | "
              f"hold médio {w['avg_hold_days']:.1f} d | {w['symbols_traded']} pares | saídas {w['exits']}",
              "", "Célula escolhida por fold: " + "; ".join(
                  f"{f['test'][0][:7]} {json.dumps({k: x for k, x in f['chosen'].items() if k != 'v'})}" for f in r["folds"]),
              "", "Grid fixo no período OOS do WF (robustez):", "",
              "| célula | trades | win% | exp/trade (IC95%) | CAGR | DD | Sharpe | anos + |", "|---|---|---|---|---|---|---|---|"]
        for g in r["grid_fixed_oos"]:
            c = {k: x for k, x in g["cell"].items() if k != "v"}
            L.append(f"| {json.dumps(c)} | {g['trades']} | {g['win_pct']:.1f}% | {_f(g['exp_pct'], 3)} "
                     f"({_f(g['exp_ci95_pct'][0], 3)}; {_f(g['exp_ci95_pct'][1], 3)}) | {_f(g['cagr_pct'], 1)} | "
                     f"{g['max_dd_pct']:.1f}% | {g['sharpe']:.2f} | {sum(1 for x in g['years'].values() if x > 0)}/{len(g['years'])} |")
        L += ["", "Critérios: " + ", ".join(f"{k} {'OK' if ok else 'FALHA'}" for k, ok in r["verdict"].items())]
    (OUT / "tables.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))


def main(argv=None):
    p = argparse.ArgumentParser(prog="trend4h")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download")
    d.add_argument("--threads", type=int, default=8)
    d.set_defaults(fn=cmd_download)
    r = sub.add_parser("run")
    r.add_argument("--variants", default="don,tsmom")
    r.add_argument("--prereg", default="7db35cb")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("report").set_defaults(fn=cmd_report)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args.fn(args)


if __name__ == "__main__":
    main()

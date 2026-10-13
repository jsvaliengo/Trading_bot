"""Pipeline: sinais (cacheados) → walk-forward (grid só no treino) → OOS
concatenado → métricas/relatório. Determinístico (bootstrap com seed fixa)."""
from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from . import data
from .engine import Costs, PortfolioConfig, SymbolData, Trade, run_portfolio
from .strategies import SignalSet, Strategy, build_registry

logger = logging.getLogger("backtest_v2.pipeline")

SEED = 20231001
N_BOOT = 10_000
MIN_TRADES_EDGE = 200
TRAIN_MONTHS, TEST_MONTHS = 6, 3


# ───────────────────────────── utilidades de tempo ─────────────────────────────
def add_months(ms: int, k: int) -> int:
    d = dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc)
    y, m = divmod(d.month - 1 + k, 12)
    return int(d.replace(year=d.year + y, month=m + 1).timestamp() * 1000)


def make_folds(start_ms: int, end_ms: int, train_m: int = TRAIN_MONTHS, test_m: int = TEST_MONTHS
               ) -> List[Tuple[int, int, int]]:
    """[(train_ini, test_ini, test_fim)] rolando de `test_m` em `test_m` meses."""
    out = []
    t0 = start_ms
    while True:
        tr1 = add_months(t0, train_m)
        te1 = min(add_months(tr1, test_m), end_ms)
        if tr1 >= end_ms:
            break
        out.append((t0, tr1, te1))
        if te1 >= end_ms:
            break
        t0 = add_months(t0, test_m)
    return out


# ───────────────────────────── sinais (cache + paralelo) ─────────────────────────────
def _sig_path(strat: Strategy, sym: str, start_ms: int, end_ms: int) -> Path:
    tag = f"{strat.name}_{strat.cache_key()}_{data.ms_to_date(start_ms)}_{data.ms_to_date(end_ms)}"
    return data.CACHE_DIR / "signals" / tag / f"{sym}.npz"


def _load_sym_bars(strat: Strategy, sym: str, start_ms: int, end_ms: int, warmup: Dict[str, int]):
    return {tf: data.load_bars(sym, tf, start_ms, end_ms, warmup[tf]) for tf in strat.tfs}


def _signals_worker(args):
    name, n_universe, sym, start_ms, end_ms, warmup = args
    strat = build_registry(n_universe)[name]
    p = _sig_path(strat, sym, start_ms, end_ms)
    if p.exists():
        return sym, False
    t0 = time.time()
    bars = _load_sym_bars(strat, sym, start_ms, end_ms, warmup)
    ss = strat.compute(sym, bars, start_ms, end_ms)
    p.parent.mkdir(parents=True, exist_ok=True)
    extra = {}
    if ss.exit_long is not None:
        extra = {"exit_long": ss.exit_long, "exit_short": ss.exit_short}
    np.savez_compressed(p, idx=ss.idx, side=ss.side, **extra)
    logger.info("sinais %s %s: %d em %.0fs", name, sym, len(ss.idx), time.time() - t0)
    return sym, True


def load_signals(strat: Strategy, sym: str, start_ms: int, end_ms: int) -> SignalSet:
    z = np.load(_sig_path(strat, sym, start_ms, end_ms))
    return SignalSet(z["idx"], z["side"], z["exit_long"] if "exit_long" in z else None,
                     z["exit_short"] if "exit_short" in z else None)


def ensure_signals(strat: Strategy, syms: List[str], n_universe: int, start_ms: int, end_ms: int,
                   warmup: Dict[str, int], workers: int) -> None:
    todo = [(strat.name, n_universe, s, start_ms, end_ms, warmup) for s in syms
            if not _sig_path(strat, s, start_ms, end_ms).exists()]
    if not todo:
        return
    if workers <= 1:
        for a in todo:
            _signals_worker(a)
        return
    with ProcessPoolExecutor(max_workers=workers) as ex:
        list(ex.map(_signals_worker, todo))


# ───────────────────────────── métricas ─────────────────────────────
def bootstrap_ci(x: np.ndarray, n_boot: int = N_BOOT, seed: int = SEED, alpha: float = 0.05):
    if len(x) < 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    means = np.empty(n_boot)
    for k in range(0, n_boot, 1000):
        m = min(1000, n_boot - k)
        idx = rng.integers(0, len(x), size=(m, len(x)))
        means[k:k + m] = x[idx].mean(axis=1)
    return (float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2)))


def _group(trades: List[Trade], key) -> Dict[str, dict]:
    g: Dict[str, List[Trade]] = {}
    for t in trades:
        g.setdefault(key(t), []).append(t)
    out = {}
    for k in sorted(g):
        ts = g[k]
        nf = np.array([t.net_frac for t in ts])
        out[k] = {"trades": len(ts), "win_rate": float(np.mean(nf > 0)),
                  "exp_pct": float(nf.mean() * 100), "pnl": float(sum(t.pnl for t in ts))}
    return out


def summarize(res, label: str) -> dict:
    tr = res.trades
    nf = np.array([t.net_frac for t in tr]) if tr else np.zeros(0)
    wins, losses = nf[nf > 0], nf[nf <= 0]
    lo, hi = bootstrap_ci(nf)

    def year(t):
        return dt.datetime.fromtimestamp(t.exit_time / 1000, tz=dt.timezone.utc).strftime("%Y")

    s = {
        "strategy": label,
        "oos_trades": int(len(tr)),
        "win_rate": float(np.mean(nf > 0)) if len(nf) else float("nan"),
        "avg_win_pct": float(wins.mean() * 100) if len(wins) else float("nan"),
        "avg_loss_pct": float(losses.mean() * 100) if len(losses) else float("nan"),
        "payoff": float(wins.mean() / -losses.mean()) if len(wins) and len(losses) and losses.mean() < 0 else float("nan"),
        "exp_pct": float(nf.mean() * 100) if len(nf) else float("nan"),
        "exp_ci95_pct": [lo * 100, hi * 100],
        "gross_exp_pct": float(np.mean([t.gross_frac for t in tr]) * 100) if tr else float("nan"),
        "net_return_pct": (res.final_equity / res.initial_equity - 1) * 100,
        "max_dd_pct": res.max_dd * 100,
        "fees_paid": float(sum(t.notional * t.fee_frac for t in tr)),
        "funding_pnl": float(sum(t.notional * t.funding_frac for t in tr)),
        "pnl": float(sum(t.pnl for t in tr)),
        "initial_equity": res.initial_equity,
        "skipped_capacity": res.skipped_capacity,
        "exits": {r: int(sum(1 for t in tr if t.reason == r)) for r in sorted({t.reason for t in tr})},
        "by_side": _group(tr, lambda t: "LONG" if t.side > 0 else "SHORT"),
        "by_year": _group(tr, year),
        "by_symbol": _group(tr, lambda t: t.symbol),
    }
    s["evidence_of_edge"] = bool(len(nf) >= MIN_TRADES_EDGE and lo > 0)
    return s


def write_trades_csv(path: Path, trades: List[Trade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    def iso(ms):
        return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M")

    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "side", "entry_time", "entry_price", "sl", "tp", "exit_time", "exit_price",
                    "reason", "sl_pct", "rr", "notional", "gross_pct", "fee_pct", "funding_pct", "net_pct",
                    "pnl", "equity_at_entry"])
        for t in trades:
            w.writerow([t.symbol, "LONG" if t.side > 0 else "SHORT", iso(t.entry_time), f"{t.entry_price:.8g}",
                        f"{t.sl:.8g}" if t.sl else "", f"{t.tp:.8g}" if t.tp else "", iso(t.exit_time),
                        f"{t.exit_price:.8g}", t.reason, t.params.get("sl_pct", ""), t.params.get("rr", ""),
                        f"{t.notional:.4f}", f"{t.gross_frac * 100:.4f}", f"{t.fee_frac * 100:.4f}",
                        f"{t.funding_frac * 100:.4f}", f"{t.net_frac * 100:.4f}", f"{t.pnl:.4f}",
                        f"{t.equity_at_entry:.4f}"])


# ───────────────────────────── walk-forward ─────────────────────────────
def portfolio_cfg(strat: Strategy) -> PortfolioConfig:
    if strat.sizing == "fraction":
        n = round(1 / strat.fraction)
        return PortfolioConfig(sizing="fraction", fraction=strat.fraction, max_pos_mult=1.0,
                               max_gross_mult=1.0, max_concurrent=n)
    # risco 1% da banca até o SL (= RISK_PER_TRADE_PCT do bot), ≤1x por posição,
    # ≤3x bruto, até 6 posições simultâneas (num_coins máximo usado ao vivo).
    return PortfolioConfig(sizing="risk", risk_pct=1.0, max_pos_mult=1.0, max_gross_mult=3.0, max_concurrent=6)


def walk_forward(strat: Strategy, sym_data: Dict[str, SymbolData], start_ms: int, end_ms: int,
                 costs: Costs) -> Tuple[object, List[dict]]:
    pcfg = portfolio_cfg(strat)
    folds = make_folds(start_ms, end_ms)
    schedule, sel = [], []
    for tr0, tr1, te1 in folds:
        scores = []
        for cell in strat.grid:
            r = run_portfolio(sym_data, [(tr0, tr1, cell)], tr0, tr1, costs, pcfg, with_curve=False)
            scores.append((r.final_equity / r.initial_equity - 1, len(r.trades), cell))
        # melhor retorno no TREINO; empate → 1ª célula (ordem fixa do grid)
        best = max(range(len(scores)), key=lambda k: (scores[k][0], -k))
        schedule.append((tr1, te1, scores[best][2]))
        sel.append({"train": [data.ms_to_date(tr0), data.ms_to_date(tr1)],
                    "test": [data.ms_to_date(tr1), data.ms_to_date(te1)],
                    "chosen": scores[best][2],
                    "train_return_pct": scores[best][0] * 100, "train_trades": scores[best][1],
                    "grid": [{"cell": c, "ret_pct": s * 100, "n": n} for s, n, c in scores]})
    oos_start = folds[0][1]
    res = run_portfolio(sym_data, schedule, oos_start, end_ms, costs, pcfg)
    return res, sel


def buy_and_hold(syms: List[str], t0: int, t1: int) -> Optional[float]:
    rets = []
    for s in syms:
        try:
            b = data.load_bars(s, "1d", t0, t1, 0)
        except FileNotFoundError:
            continue
        b = b.slice_time(t0, t1)
        if len(b) and b.open_time[0] - t0 < 86_400_000 * 2:
            rets.append(b.close[-1] / b.open[0] - 1)
    return float(np.mean(rets) * 100) if rets else None


# ───────────────────────────── comando ─────────────────────────────
def cmd_backtest(args):
    from .run import WARMUP

    syms = args.symbols.split(",") if args.symbols else data.load_universe()
    start, end = data.to_ms(args.start), data.to_ms(args.end)
    reg = build_registry(len(syms))
    names = args.only.split(",") if args.only else list(reg)
    workers = args.workers or max(1, (os.cpu_count() or 2) - 2)
    costs = Costs()
    out_dir = data.OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    summaries = {}
    sp = out_dir / "summary.json"
    if sp.exists():
        summaries = json.loads(sp.read_text())
    for name in names:
        strat = reg[name]
        s_start = start
        if args.start_3m and strat.exec_tf in ("3m", "5m"):
            s_start = data.to_ms(args.start_3m)
        t0 = time.time()
        avail = [s for s in syms if all(_cache_ok(s, tf, s_start, end, WARMUP[tf]) for tf in strat.tfs)]
        missing = sorted(set(syms) - set(avail))
        if missing:
            logger.warning("%s: sem cache p/ %s — fora do teste", name, missing)
        ensure_signals(strat, avail, len(syms), s_start, end, WARMUP, workers)
        sym_data = {}
        for s in avail:
            b = data.load_bars(s, strat.exec_tf, s_start, end, WARMUP[strat.exec_tf])
            sym_data[s] = SymbolData(b, load_signals(strat, s, s_start, end), data.load_funding(s, start, end))
        res, sel = walk_forward(strat, sym_data, s_start, end, costs)
        summ = summarize(res, name)
        folds = make_folds(s_start, end)
        summ.update({
            "range": [data.ms_to_date(s_start), data.ms_to_date(end)],
            "oos_range": [data.ms_to_date(folds[0][1]), data.ms_to_date(end)],
            "symbols_tested": len(avail), "symbols_missing": missing,
            "raw_signals": int(sum(len(sd.signals.idx) for sd in sym_data.values())),
            "folds": sel, "costs": costs.__dict__, "portfolio": portfolio_cfg(strat).__dict__,
            "buy_hold_eqw_pct": buy_and_hold(avail, folds[0][1], end),
            "runtime_s": round(time.time() - t0, 1),
        })
        summaries[name] = summ
        write_trades_csv(out_dir / f"oos_trades_{name}.csv", res.trades)
        np.savetxt(out_dir / f"equity_{name}.csv", res.equity_curve, delimiter=",", fmt=["%d", "%.4f"],
                   header="time_ms,equity", comments="")
        sp.write_text(json.dumps(summaries, indent=2, sort_keys=True, default=str))
        logger.info("%s: %d trades OOS, exp %.3f%% [%.3f, %.3f], ret %.1f%%, DD %.1f%% (%.0fs)", name,
                    summ["oos_trades"], summ["exp_pct"], *summ["exp_ci95_pct"], summ["net_return_pct"],
                    summ["max_dd_pct"], summ["runtime_s"])
    from .report import write_report
    write_report(summaries, out_dir / "report.md")
    print(f"saídas em {out_dir}")


def _cache_ok(sym: str, tf: str, start: int, end: int, warm: int) -> bool:
    try:
        data.load_bars(sym, tf, start, end, warm)
        return True
    except FileNotFoundError:
        return False

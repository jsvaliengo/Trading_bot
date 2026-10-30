"""Martingale/DCA com hedge estilo CoinTech2u, pré-registrado (MARTINGALE_PREREG.md).

    PYTHONPATH=. python -I -m scripts.backtest_v2.martingale run [--universe 31|3] [--capital 300 3000]
    PYTHONPATH=. python -I -m scripts.backtest_v2.martingale report

Simulação acoplada de carteira em 5m: todas as pernas de todos os pares avançam barra
a barra junto com a equity, porque liquidação, stop de portfólio e rejeição por margem
dependem da conta inteira. Com numba instalado o laço é compilado; sem numba roda em
Python puro (mesmo resultado, ~100× mais lento).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import time
from typing import Dict, List

import numpy as np

from . import vision
from .data import OUT_DIR, ms_to_date, to_ms
from .scalping import UNIVERSE, member_mask

try:
    from numba import njit
except ImportError:  # pragma: no cover
    def njit(**_kw):
        return lambda f: f

logger = logging.getLogger("backtest_v2.martingale")

OUT = OUT_DIR / "martingale"
FULL = (to_ms("2022-01-01"), to_ms("2026-10-01"))
HOLDOUT = (to_ms("2026-01-01"), to_ms("2026-10-01"))
LEV, MAINT, FEE, SLIP, PERF_FEE = 10.0, 0.005, 0.0005, 0.0003, 0.20
UNIVERSE_3 = ["BTCUSDT", "ETHUSDT", "ADAUSDT"]
GRID = [{"X": x, "m": m, "tp": tp} for x in (0.01, 0.015, 0.02) for m in (1.5, 2.0) for tp in (0.006, 0.01)]
DEFAULT_CELL = {"X": 0.015, "m": 2.0, "tp": 0.01}
# (N, stop de portfólio, redimensiona B pela equity depois do stop). v3b é sensibilidade pós-hoc, declarada no relatório:
# com B fixo em $, cada stop de 20% deixa a escada relativamente maior e o próximo stop vem mais rápido.
VARIANTS = {"v1_original": (1000, 0.0, 0), "v2a_n5": (5, 0.0, 0), "v2b_n7": (7, 0.0, 0), "v3_n7_stop20": (7, 0.20, 0),
            "v3b_n7_stop20_resize": (7, 0.20, 1)}
CAPITALS = (300.0, 3000.0)
LIQ, STOP = 1.0, 2.0  # marcadores em ev[t]


def base_order(capital: float, n_pairs: int, m: float, n_ref: int) -> float:
    ladder = sum(m ** k for k in range(n_ref))
    return capital * LEV * 0.5 / (n_pairs * 2 * ladder)


# ───────────────────────── dados alinhados ─────────────────────────
def load_grid(syms: List[str], t0: int, t1: int, membership: bool):
    """Matrizes [T, S] de open/high/low/close/funding alinhadas numa grade comum de 5m;
    NaN onde o par não tem barra (ou não é membro no ano, universo A)."""
    per = {}
    for s in syms:
        b = vision.load_bars(s, "5m")
        keep = (b.open_time >= t0) & (b.open_time < t1)
        if membership:
            keep &= member_mask(s, b.close_time)
        if keep.any():
            per[s] = (b.open_time[keep], b.open[keep], b.high[keep], b.low[keep], b.close[keep])
    syms = [s for s in syms if s in per]
    grid = np.unique(np.concatenate([per[s][0] for s in syms]))
    T, S = len(grid), len(syms)
    o, h, lo, c, f = (np.full((T, S), np.nan) for _ in range(5))
    f[:] = 0.0
    for j, s in enumerate(syms):
        ot, oo, hh, ll, cc = per[s]
        i = np.searchsorted(grid, ot)
        o[i, j], h[i, j], lo[i, j], c[i, j] = oo, hh, ll, cc
        fr = vision.load_funding(s)
        if len(fr):
            k = np.searchsorted(grid, fr[:, 0].astype(np.int64), "right") - 1
            ok = (k >= 0) & (grid[np.clip(k, 0, T - 1)] + 300_000 > fr[:, 0])
            f[k[ok], j] += fr[ok, 1]
    return syms, grid, o, h, lo, c, f


# ───────────────────────── núcleo ─────────────────────────
@njit(cache=True)
def simulate(o, h, lo, c, fund, B, X, m, tp, N, capital, port_stop, resize_on_stop=0):
    T, S = c.shape
    qty = np.zeros((S, 2)); avg = np.zeros((S, 2)); last = np.zeros((S, 2)); cost = np.zeros((S, 2))
    lfee = np.zeros((S, 2)); lvl = np.zeros((S, 2), np.int64); last_close = np.full(S, np.nan)
    dirs = np.array([1.0, -1.0])
    cash, deposits, peak = capital, capital, capital
    B0 = B
    equity_prev, gross_prev = capital, 0.0
    closed = 0; wins = 0; profit_pos = 0.0; liqs = 0; first_liq = -1; stops = 0
    max_lvl = 0; max_gross = 0.0
    eq = np.empty(T); un = np.empty(T); gr = np.empty(T); dep = np.empty(T); ev = np.zeros(T)
    for t in range(T):
        gross_added = 0.0
        for s in range(S):
            px_c = c[t, s]
            active = not np.isnan(px_c)
            for g in range(2):
                d = dirs[g]
                if not active:
                    if qty[s, g] > 0.0:  # saiu do universo/deslistado: fecha no último close conhecido
                        fill = last_close[s] * (1.0 - d * SLIP)
                        pnl = d * qty[s, g] * (fill - avg[s, g]) - qty[s, g] * fill * FEE
                        cash += pnl
                        closed += 1
                        if pnl - lfee[s, g] > 0.0:
                            wins += 1; profit_pos += pnl - lfee[s, g]
                        qty[s, g] = 0.0; lvl[s, g] = 0; cost[s, g] = 0.0; lfee[s, g] = 0.0
                    continue
                if qty[s, g] == 0.0:  # abre base na abertura, taker
                    if (gross_prev + gross_added + B) / LEV <= equity_prev:
                        fill = o[t, s] * (1.0 + d * SLIP)
                        fee = B * FEE
                        cash -= fee; lfee[s, g] = fee
                        qty[s, g] = B / fill; cost[s, g] = B; avg[s, g] = fill; last[s, g] = fill; lvl[s, g] = 1
                        gross_added += B
                    continue
                added = False
                while lvl[s, g] < N:
                    trig = last[s, g] * (1.0 - d * X)
                    hit = lo[t, s] <= trig if d > 0 else h[t, s] >= trig
                    if not hit:
                        break
                    notional = B * m ** lvl[s, g]
                    if (gross_prev + gross_added + notional) / LEV > equity_prev:
                        break  # margem insuficiente: ordem rejeitada
                    fee = notional * FEE
                    cash -= fee; lfee[s, g] += fee
                    qty[s, g] += notional / trig; cost[s, g] += notional; avg[s, g] = cost[s, g] / qty[s, g]
                    last[s, g] = trig; lvl[s, g] += 1; added = True
                    gross_added += notional
                    if lvl[s, g] > max_lvl:
                        max_lvl = lvl[s, g]
                if not added:
                    tp_px = avg[s, g] * (1.0 + d * tp)
                    hit = h[t, s] >= tp_px if d > 0 else lo[t, s] <= tp_px
                    if hit:
                        pnl = d * qty[s, g] * (tp_px - avg[s, g]) - qty[s, g] * tp_px * FEE
                        cash += pnl
                        closed += 1
                        if pnl - lfee[s, g] > 0.0:
                            wins += 1; profit_pos += pnl - lfee[s, g]
                        qty[s, g] = 0.0; lvl[s, g] = 0; cost[s, g] = 0.0; lfee[s, g] = 0.0
            if active:
                last_close[s] = px_c
                if fund[t, s] != 0.0:
                    cash -= fund[t, s] * (qty[s, 0] - qty[s, 1]) * px_c
        # fim da barra: marcação a mercado
        unreal = 0.0; gross = 0.0
        for s in range(S):
            if np.isnan(c[t, s]):
                continue
            for g in range(2):
                if qty[s, g] > 0.0:
                    unreal += dirs[g] * qty[s, g] * (c[t, s] - avg[s, g])
                    gross += qty[s, g] * c[t, s]
        equity = cash + unreal
        if gross > max_gross:
            max_gross = gross
        eq[t] = equity; un[t] = unreal; gr[t] = gross; dep[t] = deposits
        force = 0
        if gross > 0.0 and equity <= MAINT * gross:
            force = 1
        elif port_stop > 0.0 and equity <= peak * (1.0 - port_stop):
            force = 2
        if force > 0:
            for s in range(S):
                if np.isnan(c[t, s]):
                    continue
                for g in range(2):
                    if qty[s, g] > 0.0:
                        fill = c[t, s] * (1.0 - dirs[g] * SLIP)
                        pnl = dirs[g] * qty[s, g] * (fill - avg[s, g]) - qty[s, g] * fill * FEE
                        cash += pnl
                        closed += 1
                        if pnl - lfee[s, g] > 0.0:
                            wins += 1; profit_pos += pnl - lfee[s, g]
                        qty[s, g] = 0.0; lvl[s, g] = 0; cost[s, g] = 0.0; lfee[s, g] = 0.0
            if force == 1:
                liqs += 1; ev[t] = LIQ
                if first_liq < 0:
                    first_liq = t
                cash = capital; deposits += capital; peak = capital  # recapitaliza
            else:
                stops += 1; ev[t] = STOP; peak = cash
                if resize_on_stop:
                    B = B0 * cash / capital
            equity, gross = cash, 0.0
        if equity > peak:
            peak = equity
        equity_prev, gross_prev = equity, gross
    stats = np.array([closed, wins, profit_pos, liqs, first_liq, stops, max_lvl, max_gross, deposits, cash])
    return eq, un, gr, dep, ev, stats


# ───────────────────────── métricas ─────────────────────────
def metrics(grid_t, eq, un, gr, dep, ev, stats, capital):
    closed, wins, profit_pos, liqs, first_liq, stops, max_lvl, max_gross, deposits, cash = stats
    adj = eq - dep  # equity descontando depósitos (inicial + recapitalizações)
    net = float(adj[-1])
    peak = np.maximum.accumulate(eq)
    dd = min(float(np.max(1.0 - eq / np.maximum(peak, 1e-9))), 1.0)  # equity negativa na barra = 100%
    months = grid_t.astype("datetime64[ms]").astype("datetime64[M]")
    _, first_rev = np.unique(months[::-1], return_index=True)
    ends = np.sort(len(months) - 1 - first_rev)  # última barra de cada mês
    monthly = np.diff(np.r_[0.0, adj[ends]])  # adj já desconta o depósito inicial
    worst_i = int(np.argmin(monthly))
    return {
        "net_usd": net, "net_pct": net / capital * 100.0,
        "closed": int(closed), "win_rate": float(wins / closed * 100.0) if closed else float("nan"),
        "max_floating_loss_usd": float(min(un.min(), 0.0)), "max_floating_loss_pct": float(min(un.min(), 0.0)) / capital * 100.0,
        "max_dd_pct": dd * 100.0, "liquidations": int(liqs),
        "first_liq_date": ms_to_date(int(grid_t[int(first_liq)])) if first_liq >= 0 else None,
        "days_to_first_liq": float((grid_t[int(first_liq)] - grid_t[0]) / 86_400_000) if first_liq >= 0 else None,
        "portfolio_stops": int(stops), "worst_month": str(months[ends][worst_i]), "worst_month_usd": float(monthly[worst_i]),
        "max_level": int(max_lvl), "max_margin_usd": float(max_gross / LEV), "max_margin_pct": float(max_gross / LEV / capital * 100.0),
        "deposits_usd": float(deposits), "perf_fee_usd": float(profit_pos * PERF_FEE),
        "net_after_perf_fee_usd": net - float(profit_pos * PERF_FEE),
        "net_after_perf_fee_pct": (net - float(profit_pos * PERF_FEE)) / capital * 100.0,
    }


def passes(full: dict, hold: dict) -> bool:
    return full["liquidations"] == 0 and full["max_dd_pct"] < 20.0 and hold["net_usd"] > 0.0


# ───────────────────────── comandos ─────────────────────────
def cmd_run(args):
    OUT.mkdir(parents=True, exist_ok=True)
    results = json.loads((OUT / "results.json").read_text()) if (OUT / "results.json").exists() and args.resume else []
    done = {(r["universe"], r["capital"], r["variant"], r["period"], r["X"], r["m"], r["tp"]) for r in results}
    for uni in args.universe:
        syms_all, membership, n_pairs = (sorted({s for v in UNIVERSE.values() for s in v}), True, 12) if uni == "31" else (UNIVERSE_3, False, 3)
        for period, (t0, t1) in (("full", FULL), ("holdout", HOLDOUT)):
            syms, grid_t, o, h, lo, c, f = load_grid(syms_all, t0, t1, membership)
            logger.info("universo %s %s: %d símbolos, %d barras, %s→%s", uni, period, len(syms), len(grid_t),
                        ms_to_date(int(grid_t[0])), ms_to_date(int(grid_t[-1])))
            for variant, (N, port_stop, resize) in VARIANTS.items():
                for capital in args.capital:
                    for cell in GRID:
                        tag = f"{capital:g}" + (f"_B{args.size_as:g}" if args.size_as else "")
                        key = (uni, tag, variant, period, cell["X"], cell["m"], cell["tp"])
                        if key in done:
                            continue
                        B = base_order(args.size_as or capital, n_pairs, cell["m"], N if N < 1000 else 7)
                        tic = time.time()
                        out = simulate(o, h, lo, c, f, B, cell["X"], cell["m"], cell["tp"], N, capital, port_stop, resize)
                        r = dict(universe=uni, capital=tag, variant=variant, period=period, base_order_usd=B, **cell,
                                 **metrics(grid_t, *out, capital))
                        results.append(r)
                        logger.info("%s cap=%s %s %s X=%.3f m=%.1f tp=%.3f B=%.2f → net=%.1f dd=%.0f%% liq=%d (%.1fs)", uni, tag,
                                    variant, period, cell["X"], cell["m"], cell["tp"], B, r["net_usd"], r["max_dd_pct"], r["liquidations"], time.time() - tic)
                        (OUT / "results.json").write_text(json.dumps(results, indent=1))
            del o, h, lo, c, f


def _fmt(x, nd=1):
    if x is None:
        return "—"
    if isinstance(x, float):
        s = f"{x:,.{nd}f}".replace(",", "_").replace(".", ",").replace("_", ".")
        return s
    return str(x)


def cmd_report(args):
    results = json.loads((OUT / "results.json").read_text())
    by = {(r["universe"], r["capital"], r["variant"], r["period"], r["X"], r["m"], r["tp"]): r for r in results}
    lines = ["# Tabelas do backtest martingale/DCA (geradas por `martingale report`)", ""]
    verdict_rows = []
    tags = sorted({r["capital"] for r in results}, key=lambda x: (len(x), x))
    for uni in ("31", "3"):
        for capital in tags:
            for variant in VARIANTS:
                rows, n_pass = [], 0
                for cell in GRID:
                    full = by.get((uni, capital, variant, "full", cell["X"], cell["m"], cell["tp"]))
                    hold = by.get((uni, capital, variant, "holdout", cell["X"], cell["m"], cell["tp"]))
                    if not full or not hold:
                        continue
                    ok = passes(full, hold)
                    n_pass += ok
                    is_def = cell == DEFAULT_CELL
                    if is_def:
                        verdict_rows.append((uni, capital, variant, full, hold, ok))
                    rows.append("| " + " | ".join([
                        ("**" if is_def else "") + f"{cell['X']*100:g}% / {cell['m']:g} / {cell['tp']*100:g}%" + ("**" if is_def else ""),
                        _fmt(full["base_order_usd"], 2), _fmt(full["net_usd"], 0), _fmt(full["net_pct"], 0) + "%",
                        _fmt(full["win_rate"], 1) + "%", _fmt(full["closed"]), _fmt(full["max_floating_loss_pct"], 0) + "%",
                        _fmt(full["max_dd_pct"], 0) + "%", _fmt(full["liquidations"]), full["first_liq_date"] or "—",
                        _fmt(full["portfolio_stops"]), f"{full['worst_month']} ({_fmt(full['worst_month_usd'], 0)})",
                        _fmt(full["max_level"]), f"{_fmt(full['max_margin_usd'], 0)} ({_fmt(full['max_margin_pct'], 0)}%)",
                        _fmt(full["net_after_perf_fee_usd"], 0), _fmt(hold["net_usd"], 0), _fmt(hold["liquidations"]), _fmt(hold["max_dd_pct"], 0) + "%",
                        "PASS" if ok else "FAIL"]) + " |")
                if not rows:
                    continue
                lines += [f"## Universo {uni} · capital ${capital} · {variant} ({n_pass}/{len(rows)} células passam)", "",
                          "| X / m / TP | B ($) | líq ($) | líq % | win% | trades | pior flutuante | DD máx | liq | 1ª liq | stops | pior mês ($) | nível máx | margem máx $ (% cap) | líq após 20% ($) | holdout líq ($) | holdout liq | holdout DD | critérios |",
                          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|", *rows, ""]
    lines += ["## Célula padrão (X=1,5% / m=2 / TP=1%) por variante", "",
              "| universo | capital | variante | líq ($) | win% | DD máx | liq | 1ª liq | pior flutuante | holdout líq ($) | critérios |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for uni, capital, variant, full, hold, ok in verdict_rows:
        lines.append(f"| {uni} | ${capital} | {variant} | {_fmt(full['net_usd'], 0)} | {_fmt(full['win_rate'], 1)}% | {_fmt(full['max_dd_pct'], 0)}% | "
                     f"{full['liquidations']} | {full['first_liq_date'] or '—'} | {_fmt(full['max_floating_loss_pct'], 0)}% | {_fmt(hold['net_usd'], 0)} | {'PASS' if ok else 'FAIL'} |")
    (OUT / "tables.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--universe", nargs="+", default=["31", "3"], choices=["31", "3"])
    r.add_argument("--capital", nargs="+", type=float, default=list(CAPITALS))
    r.add_argument("--resume", action="store_true")
    r.add_argument("--size-as", type=float, default=None, help="pós-hoc: escada dimensionada para este capital (ex. 300) rodando com --capital maior")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("report").set_defaults(fn=cmd_report)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

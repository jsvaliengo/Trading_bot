"""Experimentos sobre o pullback ao vivo 3m/5m/1h com as SAÍDAS DO BOT
(SL/TP de calculate_stop_loss_take_profit + trailing ATR com piso de breakeven).

Variantes (todas walk-forward 6m treino / 3m teste, célula escolhida só no treino):
  base   saídas do bot, sem parâmetro
  H1     time exit: MFE < X% após N barras → fecha a mercado
  H2     entrada maker (limit no close do sinal ± offset, válida K barras)
  H3     filtros de entrada: tendência 4h, ADX 1h, volume, skip dos N piores pares
  H1+H2+H3  combinação das células escolhidas por fold em cada componente
  H4     Donchian breakout 1h/4h (estratégia nova, motor padrão)

    PYTHONPATH=. .venv/bin/python -m scripts.backtest_v2.run experiments --workers 10
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor
from typing import Dict, List, Optional

import numpy as np

from . import data
from .engine import (Costs, LivePlan, MakerEntry, PortfolioConfig, SymbolData, TimeExit, Trade,
                     run_portfolio, simulate_live_trade)
from .pipeline import (MIN_TRADES_EDGE, N_BOOT, SEED, load_signals, make_folds, portfolio_cfg, summarize,
                       walk_forward, write_trades_csv, ensure_signals)
from .strategies import LONG, build_registry, ema_full

logger = logging.getLogger("backtest_v2.experiments")

SRC = "live_pullback_3m_5m_1h"
COSTS = Costs()
H1_GRID = [{"time": (n, x)} for x in (0.2, 0.3) for n in (10, 20, 40)]
H2_GRID = [{"maker": (off, k)} for off in (0.0, 0.05, 0.10) for k in (1, 3)]
H3_GRIDS = {
    "H3_trend4h": [{"trend4h": True}],
    "H3_adx1h": [{"adx": a} for a in (20, 25, 30)],
    "H3_volume": [{"vol": k} for k in (1.2, 1.5, 2.0)],
    "H3_skip_worst": [{"skip_worst": n} for n in (3, 5, 8)],
}
REASONS = ("sl", "tp", "trail", "time", "eod")


# ───────────────────────── saídas do bot (reuso das funções reais) ─────────────────────────
def bot_exit_fns(bars):
    """(plan_fn_factory, trail_fn) com as funções do bot. plan_for(i, side) devolve
    plan_fn(entry_fill) → LivePlan, igual ao fluxo ao vivo: SL/TP calculados sobre o
    close do sinal (klines[-1].close) e re-ancorados no preço do fill."""
    from trading_bot.core.bot import TradingBot
    from trading_bot.core.config import config
    from trading_bot.core.strategy import HedgeStrategy, Signal, TechnicalAnalysis
    from trading_bot.execution.engine import _reanchor

    profile = next(p for p in config.STRATEGY_PROFILES if p["name"] == "trend_strong")["risk_profile"]
    hs = HedgeStrategy()
    lookback = int(getattr(config, "STRUCTURAL_STOP_LOOKBACK", 10))
    h, lw, c = bars.high, bars.low, bars.close

    class _Fee:
        @staticmethod
        def get_taker_fee_rate():
            return COSTS.fee_rate

    def trail_fn(side, entry, peak, dist):
        return TradingBot._trailing_stop_price(_Fee, "LONG" if side == LONG else "SHORT", entry, peak, dist)

    def plan_for(i, side):
        ref = float(c[i])
        atr = TechnicalAnalysis.calculate_atr(h[i - 14:i + 1].tolist(), lw[i - 14:i + 1].tolist(),
                                              c[i - 14:i + 1].tolist())
        sl, tp = hs.calculate_stop_loss_take_profit(
            ref, Signal.STRONG_BUY if side == LONG else Signal.STRONG_SELL, atr, risk_profile=profile,
            recent_low=float(lw[i - lookback + 1:i + 1].min()), recent_high=float(h[i - lookback + 1:i + 1].max()))
        trail = TechnicalAnalysis.compute_atr_based_trailing(ref, atr)

        def plan_fn(fill):
            act, dist = trail if trail else (None, None)
            return LivePlan(_reanchor(sl, ref, fill), _reanchor(tp, ref, fill), act, dist)
        return plan_fn

    return plan_for, trail_fn


def exit_opts(cell: dict):
    te = TimeExit(*cell["time"]) if cell.get("time") else None
    mk = MakerEntry(*cell["maker"]) if cell.get("maker") else None
    return te, mk


def exit_key(cell: dict) -> str:
    return json.dumps({k: cell[k] for k in ("time", "maker") if cell.get(k)}, sort_keys=True)


# ───────────────────────── tabela de desfechos por sinal (paralelo por símbolo) ─────────────────────────
def _outcome_worker(args):
    sym, start, end, warm, cells = args
    reg = build_registry(35)
    strat = reg[SRC]
    bars = data.load_bars(sym, "3m", start, end, warm)
    sig = load_signals(strat, sym, start, end)
    fund = data.load_funding(sym, start, end)
    plan_for, trail_fn = bot_exit_fns(bars)
    n = len(bars)
    out = {}
    for cell in cells:
        te, mk = exit_opts(cell)
        rows = []
        for i, s in zip(sig.idx.tolist(), sig.side.tolist()):
            tr = simulate_live_trade(bars, i, s, plan_for(i, s), trail_fn, COSTS, n, fund, te, mk, sym)
            rows.append(tr)
        out[exit_key(cell)] = _pack(rows)
    return sym, out


def _pack(rows: List[Optional[Trade]]) -> dict:
    ok = np.array([t is not None for t in rows])

    def col(f, fill=np.nan):
        return np.array([f(t) if t is not None else fill for t in rows], dtype=float)
    return {"ok": ok, "entry_idx": col(lambda t: t.entry_idx, -1).astype(np.int64),
            "exit_idx": col(lambda t: t.exit_idx, -1).astype(np.int64),
            "entry_time": col(lambda t: t.entry_time, 0).astype(np.int64),
            "exit_time": col(lambda t: t.exit_time, 0).astype(np.int64),
            "entry_price": col(lambda t: t.entry_price), "exit_price": col(lambda t: t.exit_price),
            "sl": col(lambda t: t.sl), "tp": col(lambda t: t.tp if t.tp else np.nan),
            "reason": np.array([REASONS.index(t.reason) if t is not None else -1 for t in rows], np.int8),
            "gross": col(lambda t: t.gross_frac), "fee": col(lambda t: t.fee_frac),
            "fund": col(lambda t: t.funding_frac), "mfe": col(lambda t: t.mfe_frac)}


# ───────────────────────── features de filtro (H3) ─────────────────────────
def signal_features(sym: str, bars3m, sig, start, end, warm) -> Dict[str, np.ndarray]:
    from trading_bot.core.strategy import TechnicalAnalysis

    b4 = data.load_bars(sym, "4h", start, end, warm["4h"])
    b1 = data.load_bars(sym, "1h", start, end, warm["1h"])
    ct = bars3m.close_time[sig.idx]
    i4 = np.searchsorted(b4.close_time, ct, side="right") - 1
    e50, e200 = ema_full(b4.close, 50), ema_full(b4.close, 200)
    ok4 = i4 >= 199
    trend = np.where(ok4, np.sign(e50[np.maximum(i4, 0)] - e200[np.maximum(i4, 0)]), 0).astype(np.int8)
    i1 = np.searchsorted(b1.close_time, ct, side="right") - 1
    adx = np.array([TechnicalAnalysis.calculate_adx(b1.high[k - 99:k + 1].tolist(), b1.low[k - 99:k + 1].tolist(),
                                                    b1.close[k - 99:k + 1].tolist()) if k >= 99 else 0.0
                    for k in i1.tolist()])
    v = bars3m.volume
    vol = np.array([v[i] / v[i - 20:i].mean() if i >= 20 and v[i - 20:i].mean() > 0 else 0.0
                    for i in sig.idx.tolist()])
    return {"trend4h": trend, "adx": adx, "vol": vol}


# ───────────────────────── contexto + hook do portfólio ─────────────────────────
class Lab:
    def __init__(self, syms, start, end, workers):
        from .run import WARMUP

        self.start, self.end, self.workers, self.warm = start, end, workers, WARMUP
        strat = build_registry(len(syms))[SRC]
        ensure_signals(strat, syms, len(syms), start, end, WARMUP, workers)
        self.syms = syms
        self.sym_data: Dict[str, SymbolData] = {}
        self.pos, self.feats, self.plans = {}, {}, {}
        for s in syms:
            b = data.load_bars(s, "3m", start, end, WARMUP["3m"])
            sig = load_signals(strat, s, start, end)
            self.sym_data[s] = SymbolData(b, sig, data.load_funding(s, start, end))
            self.pos[s] = {int(i): k for k, i in enumerate(sig.idx.tolist())}
            self.feats[s] = signal_features(s, b, sig, start, end, WARMUP)
            self.plans[s] = bot_exit_fns(b)
        self.tables: Dict[str, Dict[str, dict]] = {}
        self.pcfg = PortfolioConfig(**{**portfolio_cfg(strat).__dict__, "sl_buffer": _slip_buffer()})

    def ensure_tables(self, cells: List[dict]) -> None:
        todo = []
        seen = set()
        for c in cells:
            k = exit_key(c)
            if k not in self.tables and k not in seen:
                seen.add(k)
                todo.append({kk: c[kk] for kk in ("time", "maker") if c.get(kk)})
        if not todo:
            return
        t0 = time.time()
        args = [(s, self.start, self.end, self.warm["3m"], todo) for s in self.syms]
        with ProcessPoolExecutor(max_workers=self.workers) as ex:
            for sym, out in ex.map(_outcome_worker, args):
                for k, tab in out.items():
                    self.tables.setdefault(k, {})[sym] = tab
        logger.info("tabelas %s em %.0fs", [exit_key(c) for c in todo], time.time() - t0)

    def simulate(self, sym, i, side, params, end_idx):
        if sym in params.get("exclude", ()):
            return None
        k = self.pos[sym][i]
        f = self.feats[sym]
        if params.get("trend4h") and f["trend4h"][k] != side:
            return None
        if "adx" in params and f["adx"][k] < params["adx"]:
            return None
        if "vol" in params and f["vol"][k] < params["vol"]:
            return None
        t = self.tables[exit_key(params)][sym]
        if not t["ok"][k]:
            return None
        if t["exit_idx"][k] >= end_idx:          # cruza o fim da janela: re-simula truncado (sem lookahead)
            sd = self.sym_data[sym]
            plan_for, trail_fn = self.plans[sym]
            te, mk = exit_opts(params)
            return simulate_live_trade(sd.bars, i, side, plan_for(i, side), trail_fn, COSTS, end_idx,
                                       sd.funding, te, mk, sym)
        tr = Trade(sym, side, i, int(t["entry_idx"][k]), int(t["entry_time"][k]), float(t["entry_price"][k]),
                   float(t["sl"][k]), None if np.isnan(t["tp"][k]) else float(t["tp"][k]), int(t["exit_idx"][k]),
                   int(t["exit_time"][k]), float(t["exit_price"][k]), REASONS[t["reason"][k]],
                   float(t["gross"][k]), float(t["fee"][k]), float(t["fund"][k]), dict(params))
        tr.mfe_frac = float(t["mfe"][k])
        return tr

    def run(self, params_or_schedule, t0, t1, curve=False):
        sched = params_or_schedule if isinstance(params_or_schedule, list) else [(t0, t1, params_or_schedule)]
        return run_portfolio(self.sym_data, sched, t0, t1, COSTS, self.pcfg, with_curve=curve,
                             simulate=self.simulate)

    def materialize(self, cell: dict, tr0: int, tr1: int) -> dict:
        """skip_worst: exclui os N pares de pior exp/trade no TREINO (≥10 trades)."""
        n = cell.get("skip_worst")
        if not n:
            return cell
        base = {kk: cell[kk] for kk in cell if kk != "skip_worst"}
        r = self.run(base, tr0, tr1)
        by = {}
        for t in r.trades:
            by.setdefault(t.symbol, []).append(t.net_frac)
        ranked = sorted((np.mean(v), s) for s, v in by.items() if len(v) >= 10)
        return {**cell, "exclude": sorted(s for _, s in ranked[:n])}

    def walk_forward(self, cells: List[dict], per_fold: Optional[List[dict]] = None):
        """per_fold: célula fixa por fold (combinações), sem grid."""
        folds = make_folds(self.start, self.end)
        self.ensure_tables(per_fold or cells)
        schedule, sel = [], []
        for f, (tr0, tr1, te1) in enumerate(folds):
            grid = [per_fold[f]] if per_fold else cells
            mats = [self.materialize(c, tr0, tr1) for c in grid]
            scores = []
            for m in mats:
                r = self.run(m, tr0, tr1)
                scores.append((r.final_equity / r.initial_equity - 1, len(r.trades)))
            best = max(range(len(mats)), key=lambda k: (scores[k][0], -k))
            schedule.append((tr1, te1, mats[best]))
            sel.append({"test": [data.ms_to_date(tr1), data.ms_to_date(te1)], "chosen": grid[best],
                        "materialized": mats[best], "train_return_pct": scores[best][0] * 100,
                        "train_trades": scores[best][1],
                        "grid": [{"cell": c, "ret_pct": s * 100, "n": n} for c, (s, n) in zip(grid, scores)]})
        res = self.run(schedule, folds[0][1], self.end, curve=True)
        return res, sel


def _slip_buffer() -> float:
    from trading_bot.core.config import config
    return max(1.0, float(getattr(config, "SLIPPAGE_BUFFER_MULT", 1.0) or 1.0))


# ───────────────────────── métricas ─────────────────────────
def day_block_ci(nf: np.ndarray, day: np.ndarray, n_boot: int = N_BOOT, seed: int = SEED, alpha: float = 0.05):
    """IC da média por trade reamostrando DIAS inteiros (trades do mesmo dia são correlacionados)."""
    if len(nf) < 2:
        return (float("nan"), float("nan"))
    _, inv = np.unique(day, return_inverse=True)
    sums = np.bincount(inv, weights=nf)
    cnts = np.bincount(inv).astype(float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(sums), size=(n_boot, len(sums)))
    means = sums[idx].sum(axis=1) / cnts[idx].sum(axis=1)
    return (float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2)))


def summarize_x(res, label: str) -> dict:
    s = summarize(res, label)
    tr = res.trades
    nf = np.array([t.net_frac for t in tr]) if tr else np.zeros(0)
    day = np.array([t.entry_time // 86_400_000 for t in tr], dtype=np.int64)
    lo, hi = day_block_ci(nf, day)
    years = {y: g["exp_pct"] for y, g in s["by_year"].items()}
    s.update({
        "exp_ci95_iid_pct": s.pop("exp_ci95_pct"),
        "exp_ci95_pct": [lo * 100, hi * 100],
        "fee_pct": float(np.mean([t.fee_frac for t in tr]) * 100) if tr else float("nan"),
        "funding_pct": float(np.mean([t.funding_frac for t in tr]) * 100) if tr else float("nan"),
        "years_positive": int(sum(1 for v in years.values() if v > 0)),
        "years": years,
    })
    s["evidence_of_edge"] = bool(len(nf) >= MIN_TRADES_EDGE and lo > 0 and s["years_positive"] >= 2)
    return s


def signal_level_stats(lab: Lab, key: str, t0: int, t1: int, base_key: str = "{}") -> dict:
    """Por SINAL (sem teto de portfólio), sinais com entrada em [t0, t1)."""
    out = {"filled": 0, "missed": 0, "net_filled": [], "base_net_filled": [], "base_net_missed": []}
    for s in lab.syms:
        t, b = lab.tables[key][s], lab.tables[base_key][s]
        bt = b["entry_time"]
        m = b["ok"] & (bt >= t0) & (bt < t1)
        bnet = b["gross"] - b["fee"] + b["fund"]
        net = t["gross"] - t["fee"] + t["fund"]
        f = m & t["ok"]
        ms = m & ~t["ok"]
        out["filled"] += int(f.sum())
        out["missed"] += int(ms.sum())
        out["net_filled"] += net[f].tolist()
        out["base_net_filled"] += bnet[f].tolist()
        out["base_net_missed"] += bnet[ms].tolist()
    tot = out["filled"] + out["missed"]

    def m(x):
        return float(np.mean(x) * 100) if x else float("nan")
    return {"signals": tot, "fill_rate": out["filled"] / tot if tot else float("nan"),
            "exp_filled_maker_pct": m(out["net_filled"]), "exp_filled_taker_pct": m(out["base_net_filled"]),
            "exp_missed_taker_pct": m(out["base_net_missed"])}


def time_exit_cut_stats(lab: Lab, res) -> dict:
    """Trades OOS cortados pelo time exit: quantos seriam vencedores sem o corte."""
    cut = [t for t in res.trades if t.reason == "time"]
    base_win, delta = 0, []
    for t in cut:
        b = lab.tables["{}"][t.symbol]
        k = lab.pos[t.symbol][t.signal_idx]
        bn = b["gross"][k] - b["fee"][k] + b["fund"][k]
        base_win += int(bn > 0)
        delta.append(t.net_frac - bn)
    return {"cut": len(cut), "cut_would_win": base_win, "cut_share_of_trades": len(cut) / max(1, len(res.trades)),
            "mean_delta_vs_base_pct": float(np.mean(delta) * 100) if delta else float("nan")}


# ───────────────────────── comando ─────────────────────────
def cmd_experiments(args):
    syms = args.symbols.split(",") if args.symbols else data.load_universe()
    start, end = data.to_ms(args.start), data.to_ms(args.end)
    workers = args.workers or max(1, (os.cpu_count() or 2) - 2)
    out_dir = data.OUT_DIR / "experiments"
    out_dir.mkdir(parents=True, exist_ok=True)
    groups = args.only.split(",") if args.only else ["fixed_sl_rr", "main", "H4", "report"]
    for g in groups:                      # um arquivo por grupo: grupos podem rodar em processos paralelos
        _run_group(g, syms, start, end, workers, out_dir)
    print(f"saídas em {out_dir}")


def _run_group(group, syms, start, end, workers, out_dir):
    if group == "report":
        return write_experiments_report(out_dir)
    results: Dict[str, dict] = {}
    rp = out_dir / f"experiments_{group}.json"

    def save(name, res, sel, extra=None):
        s = summarize_x(res, name)
        s["folds"] = sel
        s.update(extra or {})
        results[name] = s
        write_trades_csv(out_dir / f"oos_trades_{name}.csv", res.trades)
        rp.write_text(json.dumps(results, indent=2, sort_keys=True, default=str))
        logger.info("%s: %d trades, exp %.3f%% [%.3f, %.3f], win %.1f%%, anos+ %d, edge=%s", name,
                    s["oos_trades"], s["exp_pct"], *s["exp_ci95_pct"], s["win_rate"] * 100,
                    s["years_positive"], s["evidence_of_edge"])
        return s

    if group == "fixed_sl_rr":                         # referência: harness original (SL/RR fixos)
        from .run import WARMUP
        strat = build_registry(len(syms))[SRC]
        sd = {s: SymbolData(data.load_bars(s, "3m", start, end, WARMUP["3m"]), load_signals(strat, s, start, end),
                            data.load_funding(s, start, end)) for s in syms}
        res, sel = walk_forward(strat, sd, start, end, COSTS)
        save("fixed_sl_rr", res, sel)
        return

    if group == "H4":
        from .run import WARMUP
        reg = build_registry(len(syms))
        for name in ("donchian_1h_20_10", "donchian_4h_20_10"):
            strat = reg[name]
            ensure_signals(strat, syms, len(syms), start, end, WARMUP, workers)
            sd = {s: SymbolData(data.load_bars(s, strat.exec_tf, start, end, WARMUP[strat.exec_tf]),
                                load_signals(strat, s, start, end), data.load_funding(s, start, end)) for s in syms}
            res, sel = walk_forward(strat, sd, start, end, COSTS)
            save(f"H4_{name}", res, sel)
        return

    lab = Lab(syms, start, end, workers)
    folds = make_folds(start, end)
    oos0 = folds[0][1]
    chosen: Dict[str, List[dict]] = {}

    def run_variant(name, cells, per_fold=None, extra_fn=None):
        res, sel = lab.walk_forward(cells, per_fold)
        chosen[name] = [f["chosen"] for f in sel]
        extra = extra_fn(res) if extra_fn else None
        return save(name, res, sel, extra), sel

    run_variant("base_live_exits", [{}])
    run_variant("H1_time_exit", H1_GRID, extra_fn=lambda r: {"time_exit_cut": time_exit_cut_stats(lab, r)})
    lab.ensure_tables(H2_GRID)
    run_variant("H2_maker_entry", H2_GRID, extra_fn=lambda r: {"signal_level": {
        exit_key(c): signal_level_stats(lab, exit_key(c), oos0, end) for c in H2_GRID}})
    h3_scores = {}
    for name, grid in H3_GRIDS.items():
        _, sel = run_variant(name, grid)
        h3_scores[name] = float(np.mean([f["train_return_pct"] for f in sel]))
    # melhores H3 escolhidos pelo retorno médio de TREINO (nunca pelo OOS)
    top = sorted(h3_scores, key=lambda n: -h3_scores[n])[:2]
    merged2 = [{**a, **b} for a, b in zip(chosen[top[0]], chosen[top[1]])]
    run_variant("H3_best2_combined", [], per_fold=merged2,
                extra_fn=lambda r: {"components": top, "train_mean_ret_pct": h3_scores})
    best_h3 = chosen[top[0]]
    combo = [{**a, **b, **c} for a, b, c in zip(chosen["H1_time_exit"], chosen["H2_maker_entry"], best_h3)]
    run_variant("H1_H2_H3_combined", [], per_fold=combo,
                extra_fn=lambda r: {"h3_component": top[0], "time_exit_cut": time_exit_cut_stats(lab, r)})

    results["_meta"] = {"grid_cells_total": 1 + len(H1_GRID) + len(H2_GRID) + sum(len(g) for g in H3_GRIDS.values())
                        + 2 + 6 + 6, "oos": [data.ms_to_date(oos0), data.ms_to_date(end)],
                        "generated": dt.datetime.now(dt.timezone.utc).isoformat()}
    rp.write_text(json.dumps(results, indent=2, sort_keys=True, default=str))


ORDER = ["fixed_sl_rr", "base_live_exits", "H1_time_exit", "H2_maker_entry", "H3_trend4h", "H3_adx1h",
         "H3_volume", "H3_skip_worst", "H3_best2_combined", "H1_H2_H3_combined",
         "H4_donchian_1h_20_10", "H4_donchian_4h_20_10"]


def write_experiments_report(out_dir) -> None:
    res = {}
    for g in ("fixed_sl_rr", "main", "H4"):
        p = out_dir / f"experiments_{g}.json"
        if p.exists():
            res.update({k: v for k, v in json.loads(p.read_text()).items() if not k.startswith("_")})

    def f(x, nd=3):
        return "—" if x is None or x != x else f"{x:+.{nd}f}%"
    L = ["| variante | trades | win% | ganho/perda méd | exp líquida (IC95% bloco-dia) | exp bruta | fee/trade | "
         "funding/trade | DD máx | retorno | 2024 / 2025 / 2026 | LONG / SHORT | edge? |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for k in [k for k in ORDER if k in res]:
        s = res[k]
        y = s["years"]
        sd = s["by_side"]
        lo, hi = s["exp_ci95_pct"]
        L.append(f"| {k} | {s['oos_trades']} | {s['win_rate'] * 100:.1f}% | {f(s['avg_win_pct'], 2)} / "
                 f"{f(s['avg_loss_pct'], 2)} | **{f(s['exp_pct'])}** ({f(lo)}; {f(hi)}) | {f(s['gross_exp_pct'])} | "
                 f"{s['fee_pct']:.3f}% | {f(s['funding_pct'])} | {s['max_dd_pct']:.1f}% | {s['net_return_pct']:+.1f}% | "
                 f"{' / '.join(f(y.get(a), 2) for a in ('2024', '2025', '2026'))} | "
                 f"{f(sd.get('LONG', {}).get('exp_pct'), 2)} ({sd.get('LONG', {}).get('trades', 0)}) / "
                 f"{f(sd.get('SHORT', {}).get('exp_pct'), 2)} ({sd.get('SHORT', {}).get('trades', 0)}) | "
                 f"{'SIM' if s['evidence_of_edge'] else 'não'} |")
    L.append("")
    for k in [k for k in ORDER if k in res]:
        s = res[k]
        L.append(f"- **{k}**: saídas {s['exits']}; escolhido por fold: "
                 + ", ".join(json.dumps(fd["chosen"], sort_keys=True) for fd in s["folds"]))
        for extra in ("time_exit_cut", "components", "h3_component", "train_mean_ret_pct", "signal_level"):
            if extra in s:
                L.append(f"  - {extra}: `{json.dumps(s[extra], sort_keys=True)}`")
    (out_dir / "experiments.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))

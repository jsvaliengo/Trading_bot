"""Scalping 1m/5m pré-registrado (scripts/backtest_v2/SCALPING_PREREG.md).

    .venv/bin/python -m scripts.backtest_v2.scalping download
    .venv/bin/python -m scripts.backtest_v2.scalping run [--variants ...] [--workers N]
    .venv/bin/python -m scripts.backtest_v2.scalping report

Regras, grid, custos, universo, folds e critérios vêm do pré-registro. Reusa o
motor (run_portfolio, maker_fill, Trade, funding), make_folds, summarize e o
bootstrap em blocos de dia. Novo aqui: indicadores intradiários (VWAP de sessão,
EMA, BB, RSI), os sinais das três técnicas e um simulador de scalp com saída por
tempo e TP maker.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import vision
from .data import OUT_DIR, Bars, ms_to_date, tf_ms, to_ms
from .engine import Costs, PortfolioConfig, SymbolData, Trade, _funding_frac, maker_fill, MakerEntry, run_portfolio
from .experiments import day_block_ci
from .pipeline import make_folds, summarize, write_trades_csv
from .strategies import LONG, SHORT, SignalSet, aligned_index

logger = logging.getLogger("backtest_v2.scalping")

DAY = 86_400_000
DEV_START, HOLD_START, END = to_ms("2022-01-01"), to_ms("2026-01-01"), to_ms("2026-10-01")
MAX_HOLD_MS = 4 * 3_600_000
MIN_TRAIN_TRADES = 30
MAKER_FEE, THROUGH_PCT, MAKER_K = 0.0002, 0.01, 3
COSTS = Costs()
PCFG = PortfolioConfig(initial_equity=10_000.0, sizing="risk", risk_pct=1.0, max_pos_mult=2.0,
                       max_gross_mult=5.0, max_concurrent=3, sl_buffer=1.0, size_on_initial=True)
OUT = OUT_DIR / "scalping"

# Top-12 por quote_volume dos 30 dias antes de 1º de janeiro (dado 1d fechado antes da data). Congelado no pré-registro.
UNIVERSE: Dict[int, List[str]] = {
    2022: ["BTCUSDT", "ETHUSDT", "LUNAUSDT", "MATICUSDT", "SANDUSDT", "SOLUSDT", "GALAUSDT", "1000SHIBUSDT",
           "AVAXUSDT", "MANAUSDT", "NEARUSDT", "XRPUSDT"],
    2023: ["BTCUSDT", "ETHUSDT", "DOGEUSDT", "BNBUSDT", "XRPUSDT", "LTCUSDT", "SOLUSDT", "AXSUSDT", "MATICUSDT",
           "MASKUSDT", "APEUSDT", "LINKUSDT"],
    2024: ["BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT", "TRBUSDT", "DOGEUSDT", "ADAUSDT", "XRPUSDT", "OPUSDT",
           "TIAUSDT", "MATICUSDT", "BNBUSDT"],
    2025: ["BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT", "DOGEUSDT", "1000PEPEUSDT", "SUIUSDT", "BNBUSDT", "ADAUSDT",
           "LINKUSDT", "HBARUSDT", "ENAUSDT"],
    2026: ["BTCUSDT", "ETHUSDT", "SOLUSDT", "ZECUSDT", "XRPUSDT", "PIPPINUSDT", "DOGEUSDT", "BNBUSDT", "SUIUSDT",
           "HYPEUSDT", "LIGHTUSDT", "1000PEPEUSDT"],
}

GRIDS = {
    "s1": [{"s": "s1", "R": R, "k": k} for R in (1.0, 1.5, 2.0) for k in (1.0, 1.5)],
    "s2": [{"s": "s2", "R": R, "W": W} for R in (1.0, 1.5, 2.0) for W in (15, 30)],
    "s3": [{"s": "s3", "sig": sg, "sl": sl} for sg in (2.0, 2.5) for sl in ("3sig", "atr")],
}
# 1m primeiro e as duas S1 1m (as mais pesadas em memória) separadas na fila
VARIANTS = ["s1_1m_t", "s2_1m_t", "s3_1m_m", "s3_1m_t", "s1_1m_m", "s1_5m_t", "s1_5m_m", "s2_5m_t", "s3_5m_m", "s3_5m_t"]


def ckey(cell: dict) -> str:
    return json.dumps(cell, sort_keys=True)


def member_months(sym: str) -> set:
    """Meses "YYYY-MM" em que o par precisa de dado: os anos de que é membro + o dezembro anterior."""
    out = set()
    for y, syms in UNIVERSE.items():
        if sym in syms:
            out |= {f"{y}-{m:02d}" for m in range(1, 13)} | {f"{y - 1}-12"}
    return out


def member_mask(sym: str, close_time: np.ndarray) -> np.ndarray:
    bounds = np.array([to_ms(f"{y}-01-01") for y in range(2021, 2028)], np.int64)
    yr = 2021 + np.searchsorted(bounds, close_time, "right") - 1
    ok = np.zeros(len(close_time), dtype=bool)
    for y, syms in UNIVERSE.items():
        if sym in syms:
            ok |= yr == y
    return ok


# ───────────────────────── indicadores (barras fechadas) ─────────────────────────
def ema(x: np.ndarray, n: int) -> np.ndarray:
    out = np.empty(len(x))
    if not len(x):
        return out
    a = 2.0 / (n + 1)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = out[i - 1] + a * (x[i] - out[i - 1])
    return out


def _wilder(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    a = x[:n].mean()
    out[n - 1] = a
    for i in range(n, len(x)):
        a = (a * (n - 1) + x[i]) / n
        out[i] = a
    return out


def atr(b: Bars, n: int = 14) -> np.ndarray:
    pc = np.roll(b.close, 1)
    tr = np.maximum(b.high - b.low, np.maximum(np.abs(b.high - pc), np.abs(b.low - pc)))
    if len(tr):
        tr[0] = b.high[0] - b.low[0]
    return _wilder(tr, n)


def rsi(c: np.ndarray, n: int = 14) -> np.ndarray:
    d = np.diff(c, prepend=c[:1])
    up, dn = _wilder(np.maximum(d, 0), n), _wilder(np.maximum(-d, 0), n)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(dn == 0, 100.0, 100.0 - 100.0 / (1.0 + up / dn))


def sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = np.lib.stride_tricks.sliding_window_view(x, n).mean(axis=1)
    return out


def bollinger(c: np.ndarray, n: int = 20) -> Tuple[np.ndarray, np.ndarray]:
    """(média, desvio populacional) de n closes."""
    out = np.full(len(c), np.nan)
    if len(c) >= n:
        out[n - 1:] = np.lib.stride_tricks.sliding_window_view(c, n).std(axis=1)
    return sma(c, n), out


def session_vwap(b: Bars) -> np.ndarray:
    """VWAP acumulado desde 00:00 UTC do dia da barra (preço típico × volume)."""
    day = b.open_time // DAY
    tp = (b.high + b.low + b.close) / 3.0
    pv, v = np.cumsum(tp * b.volume), np.cumsum(b.volume)
    first = np.r_[True, day[1:] != day[:-1]]
    base_pv = np.maximum.accumulate(np.where(first, np.r_[0.0, pv[:-1]], 0.0)) if len(b) else pv
    base_v = np.maximum.accumulate(np.where(first, np.r_[0.0, v[:-1]], 0.0)) if len(b) else v
    with np.errstate(invalid="ignore", divide="ignore"):
        return (pv - base_pv) / (v - base_v)


# ───────────────────────── sinais ─────────────────────────
@dataclass
class Setup:
    """Níveis de um candidato: limit da entrada maker (ou NaN), SL e TP em preço, referidos ao
    preço de referência `ref` (close do sinal p/ taker; o limit p/ maker). Escalados ao fill real."""
    limit: float
    sl: float
    tp: float


def s1_signals(b: Bars, b5: Bars, cell: dict):
    """Pullback à EMA9/VWAP a favor da tendência 5m. Devolve (idx, side, sl_dist, tp_dist)."""
    e9_5, e21_5, vw5 = ema(b5.close, 9), ema(b5.close, 21), session_vwap(b5)
    j = aligned_index(b.close_time, b5)
    valid5 = j >= 0
    jj = np.maximum(j, 0)
    up5 = valid5 & (e9_5[jj] > e21_5[jj]) & (b5.close[jj] > vw5[jj])
    dn5 = valid5 & (e9_5[jj] < e21_5[jj]) & (b5.close[jj] < vw5[jj])
    e9, vw, a = ema(b.close, 9), session_vwap(b), atr(b)
    rng = b.high - b.low
    with np.errstate(invalid="ignore"):
        hi_ref, lo_ref = np.maximum(e9, vw), np.minimum(e9, vw)
        long_ = up5 & (b.low <= hi_ref) & (b.close > hi_ref) & (b.close > b.open) & ((b.close - b.low) >= 2 / 3 * rng)
        short = dn5 & (b.high >= lo_ref) & (b.close < lo_ref) & (b.close < b.open) & ((b.high - b.close) >= 2 / 3 * rng)
        ok = np.isfinite(a) & (a > 0) & np.isfinite(vw) & (rng > 0)
    il, is_ = np.nonzero(long_ & ok)[0], np.nonzero(short & ok)[0]
    idx = np.concatenate([il, is_])
    side = np.concatenate([np.full(len(il), LONG), np.full(len(is_), SHORT)]).astype(np.int8)
    sl_d = cell["k"] * a[idx]
    return idx, side, sl_d, cell["R"] * sl_d


def session_starts(open_time: np.ndarray) -> np.ndarray:
    """Índice da 1ª barra de cada sessão (00:00 e 13:30 UTC) presente nos dados."""
    tod = open_time % DAY
    starts = np.nonzero(np.isin(tod, [0, 13 * 3_600_000 + 30 * 60_000]))[0]
    return starts


def s2_signals(b: Bars, cell: dict):
    """ORB: range das primeiras W minutos da sessão; 1º rompimento com volume, até 4h após o range."""
    step = tf_ms(b.tf)
    nW = cell["W"] * 60_000 // step
    vol_ma = sma(b.volume, 20)
    starts = session_starts(b.open_time)
    idx, side, sl_d, tp_d = [], [], [], []
    n = len(b)
    for s0 in starts.tolist():
        s1 = s0 + nW
        if s1 > n or b.open_time[s1 - 1] != b.open_time[s0] + (nW - 1) * step:
            continue
        rh, rl = b.high[s0:s1].max(), b.low[s0:s1].min()
        if rh <= rl:
            continue
        end = min(n, int(np.searchsorted(b.open_time, b.open_time[s1 - 1] + step + MAX_HOLD_MS, "left")))
        for i in range(s1, end):
            if b.volume[i] <= vol_ma[i] or not np.isfinite(vol_ma[i]):
                continue
            if b.close[i] > rh:
                sd, ref = LONG, b.close[i]
            elif b.close[i] < rl:
                sd, ref = SHORT, b.close[i]
            else:
                continue
            mid = (rh + rl) / 2
            idx.append(i), side.append(sd), sl_d.append(abs(ref - mid)), tp_d.append(cell["R"] * (rh - rl))
            break
    return (np.array(idx, np.int64), np.array(side, np.int8), np.array(sl_d), np.array(tp_d))


def s3_signals(b: Bars, cell: dict):
    """Toque na banda σ + RSI extremo. Devolve (idx, side, limit, sl, tp) em preço."""
    mid, sd = bollinger(b.close)
    r, a = rsi(b.close), atr(b)
    sg = cell["sig"]
    lo, hi = mid - sg * sd, mid + sg * sd
    with np.errstate(invalid="ignore"):
        long_ = (b.low <= lo) & (r < 30)
        short = (b.high >= hi) & (r > 70)
        ok = np.isfinite(sd) & (sd > 0) & np.isfinite(a) & (a > 0) & np.isfinite(r)
    il, is_ = np.nonzero(long_ & ok)[0], np.nonzero(short & ok)[0]
    idx = np.concatenate([il, is_])
    side = np.concatenate([np.full(len(il), LONG), np.full(len(is_), SHORT)]).astype(np.int8)
    limit = np.where(side == LONG, lo[idx], hi[idx])
    if cell["sl"] == "3sig":
        sl = np.where(side == LONG, mid[idx] - 3 * sd[idx], mid[idx] + 3 * sd[idx])
    else:
        sl = limit - side * 1.5 * a[idx]
    return idx, side, limit, sl, mid[idx]


# ───────────────────────── simulador de scalp ─────────────────────────
def simulate_scalp(b: Bars, i: int, side: int, sl_dist: float, tp_dist: float, end_idx: int, costs: Costs,
                   maker_limit: Optional[float] = None, maker_tp: bool = False,
                   funding: Optional[np.ndarray] = None, symbol: str = "", params: Optional[dict] = None) -> Optional[Trade]:
    """Entrada taker na abertura de i+1 ou maker no `maker_limit` (trade-through, k barras).
    SL/TP a `sl_dist`/`tp_dist` do fill. Ordem intrabar: adverso antes do favorável.
    Saída por tempo após MAX_HOLD_MS (taker na abertura seguinte). `end_idx` = 1º índice fora da janela."""
    o, h, lw, c = b.open, b.high, b.low, b.close
    if maker_limit is None:
        e = i + 1
        if e >= end_idx:
            return None
        entry = o[e] * (1 + costs.entry_slip * side)
        entry_fee = costs.fee_rate
    else:
        off = (1 - maker_limit / c[i]) * side * 100
        f = maker_fill(b, i, side, MakerEntry(off, MAKER_K, MAKER_FEE, THROUGH_PCT), end_idx)
        if f is None:
            return None
        e, entry = f
        entry_fee = MAKER_FEE
    sl, tp = entry - side * sl_dist, entry + side * tp_dist
    tp_trig = tp * (1 + side * THROUGH_PCT / 100) if maker_tp else tp
    deadline = b.open_time[e] + MAX_HOLD_MS
    exit_idx, exit_px, reason = -1, 0.0, ""
    for j in range(e, end_idx):
        if b.open_time[j] >= deadline:
            exit_idx, exit_px, reason = j, o[j] * (1 - side * costs.exit_slip), "time"
            break
        p = entry if j == e else o[j]
        adverse, favorable = (lw[j], h[j]) if side == LONG else (h[j], lw[j])
        if side * (adverse - sl) <= 0:
            px = p if side * (p - sl) <= 0 else sl
            exit_idx, exit_px, reason = j, px * (1 - side * (costs.exit_slip + costs.stop_slip)), "sl"
            break
        if side * (favorable - tp_trig) >= 0:
            if maker_tp:
                px = p if side * (p - tp) >= 0 else tp
                exit_px = px
            else:
                px = p if side * (p - tp) >= 0 else tp
                exit_px = px * (1 - side * costs.exit_slip)
            exit_idx, reason = j, "tp"
            break
    if exit_idx < 0:
        exit_idx = end_idx - 1
        exit_px = c[exit_idx] * (1 - side * costs.exit_slip)
        reason = "eod"
    exit_fee = MAKER_FEE if reason == "tp" and maker_tp else costs.fee_rate
    entry_time = int(b.open_time[e])
    exit_time = int(b.open_time[exit_idx]) if reason == "time" else int(b.close_time[exit_idx])
    ratio = exit_px / entry
    return Trade(symbol, side, i, e, entry_time, float(entry), float(sl), float(tp), exit_idx, exit_time,
                 float(exit_px), reason, float((ratio - 1.0) * side), float(entry_fee + exit_fee * ratio),
                 _funding_frac(b, funding, side, entry, entry_time, exit_time), dict(params or {}))


# ───────────────────────── livro ─────────────────────────
REASONS = ("sl", "tp", "time", "eod")
_REC = np.dtype([("state", np.int8), ("reason", np.int8), ("e", np.int64), ("x", np.int64), ("entry", np.float64),
                 ("exit", np.float64), ("gross", np.float64), ("fee", np.float64), ("fund", np.float64)])


class Book:
    """Sinais de todas as células de uma variante; o portfólio recebe a UNIÃO e o hook descarta
    o que não é da célula vigente no schedule. Níveis e cache de trades em arrays por (célula, par):
    o trade é determinístico, então é simulado uma vez e reaproveitado quando termina dentro da janela."""

    def __init__(self, variant: str, bars: Dict[str, Bars], bars5: Dict[str, Bars], funding: Dict[str, np.ndarray]):
        self.strat, self.tf, entry = variant.split("_")
        self.maker = entry == "m"
        self.grid = GRIDS[self.strat]
        self.bars, self.funding = bars, funding
        self.levels: Dict[str, Dict[str, tuple]] = {}   # ckey -> sym -> (key=idx*2+long, limit, sl_dist, tp_dist, cache)
        self.sym_data: Dict[str, SymbolData] = {}
        self.attempts = self.fills = 0
        for s, b in bars.items():
            ok = member_mask(s, b.close_time)
            keys_all = []
            for cell in self.grid:
                if self.strat == "s1":
                    idx, side, sl_d, tp_d = s1_signals(b, bars5[s], cell)
                    lim = b.close[idx]
                elif self.strat == "s2":
                    idx, side, sl_d, tp_d = s2_signals(b, cell)
                    lim = np.full(len(idx), np.nan)
                else:
                    idx, side, lim, sl, tp = s3_signals(b, cell)
                    sl_d, tp_d = side * (lim - sl), side * (tp - lim)
                keep = ok[idx] & (sl_d > 0) & (tp_d > 0)
                key = idx[keep] * 2 + (side[keep] == LONG)
                order = np.argsort(key, kind="stable")
                self.levels.setdefault(ckey(cell), {})[s] = (key[order], lim[keep][order], sl_d[keep][order],
                                                            tp_d[keep][order], np.zeros(len(order), _REC))
                keys_all.append(key)
            u = np.unique(np.concatenate(keys_all)) if keys_all else np.zeros(0, np.int64)
            self.sym_data[s] = SymbolData(b, SignalSet(u // 2, np.where(u % 2 == 1, LONG, SHORT).astype(np.int8)),
                                          funding.get(s))

    def simulate(self, sym, i, side, params, end_idx):
        key, lim, sl_d, tp_d, rec = self.levels[ckey(params)][sym]
        k = int(np.searchsorted(key, i * 2 + (side == LONG)))
        if k >= len(key) or key[k] != i * 2 + (side == LONG):
            return None
        r = rec[k]
        if r["state"] == 2:
            return None
        if r["state"] == 1 and r["x"] < end_idx:
            b = self.bars[sym]
            e, x = int(r["e"]), int(r["x"])
            reason = REASONS[r["reason"]]
            return Trade(sym, side, i, e, int(b.open_time[e]), float(r["entry"]), float(r["entry"] - side * sl_d[k]),
                         float(r["entry"] + side * tp_d[k]), x, int(b.open_time[x]) if reason == "time" else int(b.close_time[x]),
                         float(r["exit"]), reason, float(r["gross"]), float(r["fee"]), float(r["fund"]), dict(params))
        self.attempts += 1
        tr = simulate_scalp(self.bars[sym], i, side, float(sl_d[k]), float(tp_d[k]), end_idx, COSTS,
                            maker_limit=float(lim[k]) if self.maker else None, maker_tp=self.maker and self.strat == "s3",
                            funding=self.funding.get(sym), symbol=sym, params=params)
        if tr is None:
            rec[k]["state"] = 2
        elif tr.reason != "eod":
            self.fills += 1
            rec[k] = (1, REASONS.index(tr.reason), tr.entry_idx, tr.exit_idx, tr.entry_price, tr.exit_price,
                      tr.gross_frac, tr.fee_frac, tr.funding_frac)
        return tr

    def run(self, schedule: Sequence[tuple], t0: int, t1: int, curve: bool = True):
        return run_portfolio(self.sym_data, schedule, t0, t1, COSTS, PCFG, with_curve=curve, simulate=self.simulate)


def select(book: Book, tr0: int, tr1: int) -> Tuple[dict, List[dict]]:
    """Maior expectativa líquida/trade no treino com ≥ MIN_TRAIN_TRADES; senão 1ª célula. Empate → 1ª."""
    scores = []
    for c in book.grid:
        r = book.run([(tr0, tr1, c)], tr0, tr1, curve=False)
        nf = [t.net_frac for t in r.trades]
        scores.append({"cell": c, "exp_pct": float(np.mean(nf) * 100) if nf else float("nan"), "n": len(nf)})
    elig = [k for k, s in enumerate(scores) if s["n"] >= MIN_TRAIN_TRADES]
    best = max(elig, key=lambda k: (scores[k]["exp_pct"], -k)) if elig else 0
    return book.grid[best], scores


def walk_forward(book: Book):
    folds = make_folds(DEV_START, HOLD_START, 6, 2)
    schedule, sel = [], []
    for tr0, tr1, te1 in folds:
        cell, scores = select(book, tr0, tr1)
        schedule.append((tr1, te1, cell))
        sel.append({"train": [ms_to_date(tr0), ms_to_date(tr1)], "test": [ms_to_date(tr1), ms_to_date(te1)],
                    "chosen": cell, "grid": scores})
    return book.run(schedule, folds[0][1], HOLD_START), sel, folds[0][1]


# ───────────────────────── métricas ─────────────────────────
def _year_label(t: Trade) -> str:
    return dt.datetime.fromtimestamp(t.entry_time / 1000, tz=dt.timezone.utc).strftime("%Y")


def metrics(res, label: str, attempts: Optional[int] = None) -> dict:
    s = summarize(res, label)
    tr = res.trades
    nf = np.array([t.net_frac for t in tr]) if tr else np.zeros(0)
    lo, hi = day_block_ci(nf, np.array([t.entry_time // DAY for t in tr], np.int64))
    years = {}
    for t in tr:
        years.setdefault(_year_label(t), []).append(t.net_frac)
    out = {
        "trades": len(tr), "win_pct": s["win_rate"] * 100, "avg_win_pct": s["avg_win_pct"],
        "avg_loss_pct": s["avg_loss_pct"], "exp_pct": s["exp_pct"], "exp_ci95_pct": [lo * 100, hi * 100],
        "gross_exp_pct": s["gross_exp_pct"], "ret_pct": s["net_return_pct"], "max_dd_pct": s["max_dd_pct"],
        "fee_pct_per_trade": float(np.mean([t.fee_frac for t in tr]) * 100) if tr else float("nan"),
        "funding_pct_per_trade": float(np.mean([t.funding_frac for t in tr]) * 100) if tr else float("nan"),
        "fee_pct_of_avg_win": float(np.mean([t.fee_frac for t in tr]) * 100 / s["avg_win_pct"]) if tr and s["avg_win_pct"] > 0 else float("nan"),
        "years": {y: {"n": len(v), "exp_pct": float(np.mean(v) * 100)} for y, v in sorted(years.items())},
        "by_side": s["by_side"], "exits": s["exits"], "skipped_capacity": res.skipped_capacity,
        "avg_hold_min": float(np.mean([(t.exit_time - t.entry_time) / 60_000 for t in tr])) if tr else float("nan"),
        "symbols_traded": len({t.symbol for t in tr}),
    }
    if attempts is not None:
        out["maker_fill_rate"] = len(tr) / attempts if attempts else float("nan")
    return out


def verdict(wf: dict, ho: dict, grid_rows: List[dict]) -> dict:
    yrs = wf["years"]
    c = {
        "C1_exp_ci_lower_gt_0": wf["exp_ci95_pct"][0] > 0,
        "C2_min_500_trades": wf["trades"] >= 500,
        "C3_years_two_thirds": sum(1 for v in yrs.values() if v["exp_pct"] > 0) >= int(np.ceil(2 / 3 * len(yrs))) if yrs else False,
        "C4_holdout_exp_gt_0": ho["exp_pct"] > 0,
        "C5_robust_grid_majority": sum(1 for g in grid_rows if g["exp_pct"] > 0) > len(grid_rows) / 2,
    }
    c["PASS"] = all(c.values())
    return c


# ───────────────────────── comandos ─────────────────────────
def all_symbols() -> List[str]:
    return sorted(set().union(*UNIVERSE.values()))


def cmd_download(args):
    syms = all_symbols()
    months = {s: member_months(s) for s in syms}
    for tf in ("5m", "1m"):
        vision.download(syms, "klines", tf, args.threads, months)
    vision.download(syms, "fundingRate", "", args.threads)


def load_all(tf: str) -> Dict[str, Bars]:
    out = {}
    for s in all_symbols():
        try:
            b = vision.load_bars(s, tf)
        except FileNotFoundError:
            continue
        b = b.slice_time(to_ms("2021-12-01"), END)
        if len(b) > 500:
            out[s] = b
    return out


def coverage(bars: Dict[str, Bars], bars5: Dict[str, Bars]) -> dict:
    def one(bb, step):
        return {s: {"first": ms_to_date(int(b.open_time[0])), "last": ms_to_date(int(b.open_time[-1])), "bars": len(b),
                    "gaps": int(np.sum(np.diff(b.open_time) != step))} for s, b in bb.items()}
    return {"symbols": len(bars), "bars_1m_total": int(sum(len(b) for b in bars.values())),
            "bars_5m_total": int(sum(len(b) for b in bars5.values())), "per_symbol_1m": one(bars, 60_000),
            "per_symbol_5m": one(bars5, 300_000)}


_DATA: Dict[str, Dict[str, Bars]] = {}   # carregado no pai antes do fork: os filhos leem copy-on-write


def run_variant(variant: str) -> dict:
    t0 = time.time()
    bars5 = _DATA.get("5m") or load_all("5m")
    bars = (_DATA.get("1m") or load_all("1m")) if variant.split("_")[1] == "1m" else bars5
    fund = {s: vision.load_funding(s) for s in bars}
    book = Book(variant, bars, bars5, fund)
    logger.info("%s: livro pronto, %d sinais (%.0fs)", variant, sum(len(sd.signals.idx) for sd in book.sym_data.values()), time.time() - t0)
    wf_res, sel, oos0 = walk_forward(book)
    wf_attempts = book.attempts
    wf = metrics(wf_res, f"{variant}_wf")
    OUT.mkdir(parents=True, exist_ok=True)
    write_trades_csv(OUT / f"wf_trades_{variant}.csv", wf_res.trades)
    hcell, hscores = select(book, to_ms("2025-07-01"), HOLD_START)
    ho_res = book.run([(HOLD_START, END, hcell)], HOLD_START, END)
    ho = metrics(ho_res, f"{variant}_holdout")
    write_trades_csv(OUT / f"holdout_trades_{variant}.csv", ho_res.trades)
    rows = []
    for c in book.grid:
        r = book.run([(oos0, HOLD_START, c)], oos0, HOLD_START)
        rows.append({"cell": c, **metrics(r, ckey(c))})
    # taxa de fill da entrada maker: ordens colocadas (1ª simulação de cada candidato) que preencheram
    fill_rate = book.fills / book.attempts if book.maker and book.attempts else None
    out = {"wf": wf, "folds": sel, "holdout_cell": hcell, "holdout_train_scores": hscores, "holdout": ho,
           "grid_fixed_oos": rows, "verdict": verdict(wf, ho, rows), "maker_fill_rate": fill_rate,
           "oos_start": ms_to_date(oos0), "seconds": time.time() - t0, "wf_attempts": wf_attempts}
    logger.info("%s: WF %d trades exp %.4f%% [%.4f, %.4f] bruta %.4f%% | holdout exp %.4f%% (%d) | PASS=%s (%.0fs)",
                variant, wf["trades"], wf["exp_pct"], *wf["exp_ci95_pct"], wf["gross_exp_pct"], ho["exp_pct"],
                ho["trades"], out["verdict"]["PASS"], time.time() - t0)
    (OUT / f"result_{variant}.json").write_text(json.dumps(out, indent=1, sort_keys=True, default=float))
    return out


def cmd_run(args):
    OUT.mkdir(parents=True, exist_ok=True)
    variants = args.variants.split(",")
    _DATA["5m"], _DATA["1m"] = load_all("5m"), load_all("1m")
    results = {"coverage": coverage(_DATA["1m"], _DATA["5m"]), "prereg_commit": args.prereg, "variants": {}}
    import multiprocessing as mp
    with ProcessPoolExecutor(max_workers=args.workers, mp_context=mp.get_context("fork")) as ex:
        for v, r in zip(variants, ex.map(run_variant, variants)):
            results["variants"][v] = r
            (OUT / "results.json").write_text(json.dumps(results, indent=1, sort_keys=True, default=float))
    print(f"saídas em {OUT}")


def _f(x, nd=3):
    return "n/d" if x is None or x != x else f"{x:+.{nd}f}%"


def slip_pct(m: dict, variant: str) -> float:
    """Slippage médio por trade (% do notional) implícito nos preços: 0,03% por perna taker + 0,10% no stop.
    Determinístico pelas saídas, por isso não precisa estar gravado por trade."""
    ex, n = m.get("exits", {}), max(1, m["trades"])
    maker_entry, maker_tp = variant.endswith("_m"), variant.startswith("s3") and variant.endswith("_m")
    legs = (0.0 if maker_entry else 0.03) + 0.03 * (n - (ex.get("tp", 0) if maker_tp else 0)) / n
    return legs + 0.10 * ex.get("sl", 0) / n


def _row(name: str, m: dict, variant: str = "") -> str:
    lo, hi = m["exp_ci95_pct"]
    sl_pct = slip_pct(m, variant)
    sd = m.get("by_side", {})
    side = " / ".join(f"{_f(sd.get(k, {}).get('exp_pct'))} ({sd.get(k, {}).get('trades', 0)})" for k in ("LONG", "SHORT"))
    ex = m.get("exits", {})
    return (f"| {name} | {m['trades']} | {m['win_pct']:.1f}% | {_f(m['avg_win_pct'])} / {_f(m['avg_loss_pct'])} | "
            f"**{_f(m['exp_pct'])}** ({_f(lo)}; {_f(hi)}) | {_f(m['gross_exp_pct'] + sl_pct)} / {_f(m['gross_exp_pct'])} | {m['fee_pct_per_trade']:.3f}% + {sl_pct:.3f}% "
            f"({m['fee_pct_of_avg_win'] if m['fee_pct_of_avg_win'] == m['fee_pct_of_avg_win'] else float('nan'):.0f}%) | "
            f"{m['max_dd_pct']:.1f}% | {side} | sl {ex.get('sl', 0)} / tp {ex.get('tp', 0)} / time {ex.get('time', 0)} |")


HEAD = ("| variante | trades | win% | ganho / perda méd | exp líq/trade (IC95% bloco-dia) | exp bruta sem custo / só c/ slippage | fee + slippage/trade (fee % do ganho méd) | "
        "DD máx | LONG / SHORT exp (n) | saídas |\n|---|---|---|---|---|---|---|---|---|---|")


def cmd_report(args):
    res = json.loads((OUT / "results.json").read_text())
    cov = res["coverage"]
    L = ["## Cobertura", "", f"{cov['symbols']} símbolos, {cov['bars_1m_total']:,} barras 1m, {cov['bars_5m_total']:,} barras 5m.", "",
         "| símbolo | 1m início | 1m fim | barras 1m | buracos 1m | barras 5m |", "|---|---|---|---|---|---|"]
    for s, c in sorted(cov["per_symbol_1m"].items()):
        c5 = cov["per_symbol_5m"].get(s, {})
        L.append(f"| {s} | {c['first']} | {c['last']} | {c['bars']:,} | {c['gaps']} | {c5.get('bars', 0):,} |")
    L += ["", "## Walk-forward OOS (2022-07 → 2026-01)", "", HEAD]
    L += [_row(v, r["wf"], v) for v, r in res["variants"].items()]
    L += ["", "## Holdout (2026-01 → 2026-10), célula escolhida no treino 2025-07 → 2026-01", "", HEAD]
    L += [_row(f"{v} {json.dumps({k: x for k, x in r['holdout_cell'].items() if k != 's'})}", r["holdout"], v) for v, r in res["variants"].items()]
    for v, r in res["variants"].items():
        w = r["wf"]
        L += ["", f"### {v}", "",
              "Expectativa líquida por ano (WF): " + ", ".join(f"{y} {_f(x['exp_pct'])} ({x['n']})" for y, x in w["years"].items()),
              "", f"Hold médio {w['avg_hold_min']:.0f} min | funding {_f(w['funding_pct_per_trade'], 4)}/trade | {w['symbols_traded']} pares | "
              f"pulados por capacidade {w['skipped_capacity']}" + (f" | fill maker {r['maker_fill_rate'] * 100:.0f}%" if r.get("maker_fill_rate") is not None else ""),
              "", "Célula por fold: " + "; ".join(f"{f['test'][0][:7]} {json.dumps({k: x for k, x in f['chosen'].items() if k != 's'})}" for f in r["folds"]),
              "", "Grid fixo no período OOS (robustez):", "",
              "| célula | trades | win% | exp líq (IC95%) | exp bruta | DD | anos + |", "|---|---|---|---|---|---|---|"]
        for g in r["grid_fixed_oos"]:
            c = {k: x for k, x in g["cell"].items() if k != "s"}
            L.append(f"| {json.dumps(c)} | {g['trades']} | {g['win_pct']:.1f}% | {_f(g['exp_pct'])} ({_f(g['exp_ci95_pct'][0])}; "
                     f"{_f(g['exp_ci95_pct'][1])}) | {_f(g['gross_exp_pct'])} | {g['max_dd_pct']:.1f}% | "
                     f"{sum(1 for x in g['years'].values() if x['exp_pct'] > 0)}/{len(g['years'])} |")
        L += ["", "Critérios: " + ", ".join(f"{k} {'OK' if ok else 'FALHA'}" for k, ok in r["verdict"].items())]
    (OUT / "tables.md").write_text("\n".join(L) + "\n")
    print("\n".join(L))


def main(argv=None):
    p = argparse.ArgumentParser(prog="scalping")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("download")
    d.add_argument("--threads", type=int, default=8)
    d.set_defaults(fn=cmd_download)
    r = sub.add_parser("run")
    r.add_argument("--variants", default=",".join(VARIANTS))
    r.add_argument("--workers", type=int, default=5)
    r.add_argument("--prereg", default="8797a2d")
    r.set_defaults(fn=cmd_run)
    sub.add_parser("report").set_defaults(fn=cmd_report)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args.fn(args)


if __name__ == "__main__":
    main()

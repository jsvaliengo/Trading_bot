#!/usr/bin/env python3
"""Market making simples de varejo em perpétuos USDT-M, por aggTrades (ver MM_PREREG.md).

Sem L2: fill "fim de fila" = trade imprime estritamente além do nosso preço;
"preço igual" = trade em preço igual também preenche (teto). Requote a cada R s com
mid de 200 ms atrás, skew por inventário, flatten taker no limite e no fim do dia.

Uso: python3 scripts/backtest_v2/market_making.py [--download-only]
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import time
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import requests

from .data import CACHE_DIR, OUT_DIR

logger = logging.getLogger("backtest_v2.market_making")

FILE_URL = "https://data.binance.vision/data/futures/um/daily/aggTrades/{sym}/{sym}-aggTrades-{day}.zip"
RAW_DIR = CACHE_DIR / "vision_raw" / "aggTrades"
BIN_DIR = CACHE_DIR / "aggtrades_100ms"
MM_OUT = OUT_DIR / "market_making"

BIN_MS = 100
BINS_PER_DAY = 86_400_000 // BIN_MS
LATENCY_BINS = 2  # 200 ms
AS_HORIZONS = {"1s": 10, "10s": 100}

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT", "DOGEUSDT", "SNXUSDT", "CHZUSDT", "COMPUSDT"]
TICK = {"BTCUSDT": 0.1, "ETHUSDT": 0.01, "SOLUSDT": 0.01, "ADAUSDT": 0.0001, "DOGEUSDT": 0.00001,
        "SNXUSDT": 0.0001, "CHZUSDT": 0.00001, "COMPUSDT": 0.01}
MIN_NOTIONAL = {"BTCUSDT": 50.0, "ETHUSDT": 20.0}
IN_SAMPLE_DAYS = [f"2025-{m:02d}-{d:02d}" for m in (3, 4, 5) for d in (1, 8, 15, 22)]
HOLDOUT_DAYS = [f"2026-02-{d:02d}" for d in (1, 8, 15, 22)]

SPREADS_BPS = [1, 2, 5, 10]
REQUOTES_S = [1, 5]
NOTIONALS = [10.0, 50.0]
INV_MULT = 5  # Q_max = 5 × notional
MAKER_FEES = {"maker_0.02": 0.0002, "maker_-0.01": -0.0001, "maker_0": 0.0}
TAKER_FEE = 0.0005
CAPITAL = 300.0


# ----------------------------------------------------------------------------- dados
def download(sym: str, day: str, session: Optional[requests.Session] = None) -> Optional[Path]:
    """Baixa o zip diário (retomável, escrita atômica). None se 404."""
    dest = RAW_DIR / sym / f"{sym}-aggTrades-{day}.zip"
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    s = session or requests.Session()
    url = FILE_URL.format(sym=sym, day=day)
    for attempt in range(5):
        try:
            r = s.get(url, timeout=120, stream=True)
        except requests.RequestException as exc:
            logger.warning("rede %s: %s", url, exc)
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 404:
            logger.warning("404 %s", url)
            return None
        if r.status_code != 200:
            time.sleep(2 ** attempt)
            continue
        tmp = dest.with_suffix(".part")
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
        tmp.rename(dest)
        return dest
    return None


BIN_KEYS = ("low", "high", "last", "last_bid", "last_ask", "n")


def _parse_zip(path: Path) -> np.ndarray:
    """Colunas (price, ts_ms, is_buyer_maker) dos trades, lidas em memória. Com ou sem cabeçalho."""
    with zipfile.ZipFile(path) as zf:
        name = zf.namelist()[0]
        raw = zf.read(name)
    skip = 0 if raw[:1].isdigit() else 1
    arr = np.loadtxt(io.BytesIO(raw), delimiter=",", skiprows=skip, usecols=(1, 5, 6), dtype=np.float64,
                     converters={6: lambda s: 1.0 if str(s).strip().lower() in ("true", "1") else 0.0})
    return arr.reshape(-1, 3)


def _ffill(x: np.ndarray) -> np.ndarray:
    idx = np.maximum.accumulate(np.where(np.isnan(x), -1, np.arange(len(x))))
    return np.where(idx >= 0, x[np.maximum(idx, 0)], np.nan)


def _last_by_bin(b: np.ndarray, price: np.ndarray) -> np.ndarray:
    out = np.full(BINS_PER_DAY, np.nan)
    if len(b) == 0:
        return out
    starts = np.flatnonzero(np.r_[True, b[1:] != b[:-1]])
    out[b[starts]] = price[np.r_[starts[1:], len(price)] - 1]
    return _ffill(out)


def bin_day(trades: np.ndarray) -> Dict[str, np.ndarray]:
    """Bins de 100 ms: low (+inf se vazio), high (−inf), last (ffill; NaN antes do 1º trade), n,
    last_bid / last_ask = último trade com agressor vendedor (is_buyer_maker) / comprador."""
    price, ts, bm = trades[:, 0], trades[:, 1], trades[:, 2] > 0.5
    day0 = (ts[0] // 86_400_000) * 86_400_000
    b = ((ts - day0) // BIN_MS).astype(np.int64)
    b = np.clip(b, 0, BINS_PER_DAY - 1)
    order = np.argsort(b, kind="stable")
    b, price, bm = b[order], price[order], bm[order]
    starts = np.flatnonzero(np.r_[True, b[1:] != b[:-1]])
    ub = b[starts]
    low = np.full(BINS_PER_DAY, np.inf)
    high = np.full(BINS_PER_DAY, -np.inf)
    n = np.zeros(BINS_PER_DAY, np.int32)
    low[ub] = np.minimum.reduceat(price, starts)
    high[ub] = np.maximum.reduceat(price, starts)
    n[ub] = np.diff(np.r_[starts, len(price)])
    return {"low": low, "high": high, "last": _last_by_bin(b, price), "n": n,
            "last_bid": _last_by_bin(b[bm], price[bm]), "last_ask": _last_by_bin(b[~bm], price[~bm])}


def load_day(sym: str, day: str) -> Optional[Dict[str, np.ndarray]]:
    out = BIN_DIR / f"{sym}-{day}.npz"
    if out.exists():
        with np.load(out) as z:
            return {k: z[k] for k in BIN_KEYS}
    path = download(sym, day)
    if path is None:
        return None
    bins = bin_day(_parse_zip(path))
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.npz")
    np.savez_compressed(tmp, **bins)
    tmp.rename(out)
    return bins


# ----------------------------------------------------------------------------- simulação
def interval_arrays(bins: Dict[str, np.ndarray], requote_s: int) -> Dict[str, np.ndarray]:
    """Agrega bins de 100 ms em intervalos de requote: L/H do intervalo, mid de referência
    ((último trade no bid + último no ask)/2 até t − 200 ms), índice do bin de fim do intervalo."""
    k = requote_s * 1000 // BIN_MS
    n_int = BINS_PER_DAY // k
    low = bins["low"][: n_int * k].reshape(n_int, k)
    high = bins["high"][: n_int * k].reshape(n_int, k)
    mid = (bins["last_bid"] + bins["last_ask"]) / 2.0
    ref_idx = np.arange(n_int) * k - LATENCY_BINS
    m_ref = np.where(ref_idx >= 0, mid[np.maximum(ref_idx, 0)], np.nan)
    return {"L": low.min(1), "H": high.max(1), "m_ref": m_ref, "end_bin": np.arange(1, n_int + 1) * k - 1,
            "mid": mid, "bid_px": bins["last_bid"], "ask_px": bins["last_ask"], "k": k}


def simulate_day(sym: str, bins: Dict[str, np.ndarray], requote_s: int, spread_bps: float,
                 notional: float, equal_fills: bool, chunk: int = 4096) -> Dict[str, float]:
    """Um dia, um símbolo, uma célula. Devolve P&L bruto (sem fee maker), notionals, markouts."""
    ia = interval_arrays(bins, requote_s)
    L, H, m_ref, end_bin, mid = ia["L"], ia["H"], ia["m_ref"], ia["end_bin"], ia["mid"]
    bid_px, ask_px = ia["bid_px"], ia["ask_px"]
    tick = TICK[sym]
    notional = max(notional, MIN_NOTIONAL.get(sym, 0.0))
    s = spread_bps / 1e4
    q_max = INV_MULT * notional
    n_int = len(L)
    n_bins = len(mid)
    q = 0.0
    cash = 0.0
    maker_notional = 0.0
    taker_notional = 0.0
    n_fills = 0
    n_flatten = 0
    max_inv = 0.0
    mark = {h: 0.0 for h in AS_HORIZONS}
    mark_mid = {h: 0.0 for h in AS_HORIZONS}
    i = 0

    def flatten(b: int) -> None:
        """Taker: vende no último preço do lado bid, compra no do lado ask (cruza o spread real)."""
        nonlocal q, cash, taker_notional
        px = bid_px[b] if q > 0 else ask_px[b]
        if np.isnan(px):
            return
        cash += q * px
        taker_notional += abs(q) * px
        q = 0.0

    while i < n_int:
        m0 = m_ref[i]
        if np.isnan(m0):
            i += 1
            continue
        inv0 = q * m0
        if abs(inv0) >= q_max:
            flatten(min(i * ia["k"] + LATENCY_BINS, n_bins - 1))
            n_flatten += 1
        # varre em blocos até o primeiro intervalo em que algum lado preenche
        j = -1
        bid_j = ask_j = 0.0
        start = i
        while start < n_int:
            sl = slice(start, min(start + chunk, n_int))
            m = m_ref[sl]
            with np.errstate(invalid="ignore"):
                ratio = np.clip(q * m / q_max, -1.0, 1.0)
                r = m * (1.0 - ratio * s)
                bid = np.floor(r * (1.0 - s) / tick) * tick
                ask = np.ceil(r * (1.0 + s) / tick) * tick
                hit_b = (L[sl] <= bid) if equal_fills else (L[sl] < bid)
                hit_a = (H[sl] >= ask) if equal_fills else (H[sl] > ask)
                hit_b &= ratio < 1.0
                hit_a &= ratio > -1.0
            hit = hit_b | hit_a
            if hit.any():
                off = int(np.argmax(hit))
                j = start + off
                bid_j, ask_j = float(bid[off]), float(ask[off])
                fb, fa = bool(hit_b[off]), bool(hit_a[off])
                break
            start += chunk
        if j < 0:
            break
        qty = notional / m_ref[j]
        e = end_bin[j]
        for side, px, hit_side in ((1.0, bid_j, fb), (-1.0, ask_j, fa)):
            if not hit_side:
                continue
            q += side * qty
            cash -= side * qty * px
            maker_notional += qty * px
            n_fills += 1
            for h, nb in AS_HORIZONS.items():
                p_after = mid[min(e + nb, n_bins - 1)]
                if not np.isnan(p_after):
                    mark[h] += side * (p_after - px) * qty
                    mark_mid[h] += side * (p_after - m_ref[j]) * qty
        max_inv = max(max_inv, abs(q * m_ref[j]))
        i = j + 1
    if q != 0.0:
        flatten(n_bins - 1)
    return {"gross": cash, "maker_notional": maker_notional, "taker_notional": taker_notional,
            "n_fills": n_fills, "n_flatten": n_flatten, "max_inv": max_inv,
            "mark_1s": mark["1s"], "mark_10s": mark["10s"],
            "mark_mid_1s": mark_mid["1s"], "mark_mid_10s": mark_mid["10s"],
            "notional_used": notional}


def net_pnl(d: Dict[str, float], maker_fee: float) -> float:
    return d["gross"] - maker_fee * d["maker_notional"] - TAKER_FEE * d["taker_notional"]


def cells() -> List[Dict]:
    return [{"spread_bps": s, "requote_s": r, "notional": n, "equal_fills": eq}
            for s in SPREADS_BPS for r in REQUOTES_S for n in NOTIONALS for eq in (False, True)]


def cell_key(c: Dict) -> str:
    return f"s{c['spread_bps']}_r{c['requote_s']}_n{int(c['notional'])}_{'equal' if c['equal_fills'] else 'eoq'}"


def run_symbol(sym: str) -> Dict[str, Dict[str, Dict[str, float]]]:
    """{cell_key: {day: métricas}} para um símbolo (processo próprio)."""
    out: Dict[str, Dict[str, Dict[str, float]]] = {cell_key(c): {} for c in cells()}
    for day in IN_SAMPLE_DAYS + HOLDOUT_DAYS:
        bins = load_day(sym, day)
        if bins is None:
            logger.warning("%s %s ausente", sym, day)
            continue
        t0 = time.time()
        for c in cells():
            out[cell_key(c)][day] = simulate_day(sym, bins, c["requote_s"], c["spread_bps"],
                                                 c["notional"], c["equal_fills"])
        logger.info("%s %s %.1fs", sym, day, time.time() - t0)
    return out


# ----------------------------------------------------------------------------- agregação
def daily_series(per_symbol: Dict[str, Dict[str, Dict[str, Dict[str, float]]]], key: str,
                 days: List[str], maker_fee: float, syms: Optional[List[str]] = None) -> Dict[str, np.ndarray]:
    syms = syms or list(per_symbol)
    pnl, fills, flat, mk, mn = [], [], [], [], []
    mark1, mark10, mkmid1, mkmid10, inv = [], [], [], [], []
    for day in days:
        rows = [per_symbol[s][key][day] for s in syms if day in per_symbol[s][key]]
        if not rows:
            continue
        pnl.append(sum(net_pnl(r, maker_fee) for r in rows))
        fills.append(sum(r["n_fills"] for r in rows))
        flat.append(sum(r["n_flatten"] for r in rows))
        mk.append(sum(r["maker_notional"] for r in rows))
        mn.append(sum(r["notional_used"] for r in rows))
        mark1.append(sum(r["mark_1s"] for r in rows))
        mark10.append(sum(r["mark_10s"] for r in rows))
        mkmid1.append(sum(r["mark_mid_1s"] for r in rows))
        mkmid10.append(sum(r["mark_mid_10s"] for r in rows))
        inv.append(max(r["max_inv"] for r in rows))
    return {k: np.asarray(v, float) for k, v in
            {"pnl": pnl, "fills": fills, "flatten": flat, "maker_notional": mk, "mark_1s": mark1,
             "mark_10s": mark10, "mark_mid_1s": mkmid1, "mark_mid_10s": mkmid10, "max_inv": inv}.items()}


def summarize(ds: Dict[str, np.ndarray]) -> Dict[str, float]:
    pnl = ds["pnl"]
    if len(pnl) == 0:
        return {}
    cum = np.cumsum(pnl)
    dd = float((np.maximum.accumulate(np.r_[0.0, cum]) - np.r_[0.0, cum]).max())
    std = float(pnl.std(ddof=1)) if len(pnl) > 1 else float("nan")
    sharpe_d = float(pnl.mean() / std) if std and std > 0 else float("nan")
    mk = float(ds["maker_notional"].sum())

    def bps(x: np.ndarray) -> float:
        return float(x.sum() / mk * 1e4) if mk > 0 else float("nan")

    return {"days": int(len(pnl)), "pnl_total": float(pnl.sum()), "pnl_per_day": float(pnl.mean()),
            "pnl_pct_capital_per_day": float(pnl.mean() / CAPITAL * 100), "sharpe_daily": sharpe_d,
            "sharpe_ann": sharpe_d * float(np.sqrt(365)) if not np.isnan(sharpe_d) else float("nan"),
            "max_dd": dd, "fills": int(ds["fills"].sum()), "flatten": int(ds["flatten"].sum()),
            "maker_notional": mk, "pnl_per_1k_filled": float(pnl.sum() / mk * 1000) if mk > 0 else float("nan"),
            "markout_1s_bps": bps(ds["mark_1s"]), "markout_10s_bps": bps(ds["mark_10s"]),
            "adverse_mid_1s_bps": bps(ds["mark_mid_1s"]), "adverse_mid_10s_bps": bps(ds["mark_mid_10s"]),
            "max_inv": float(ds["max_inv"].max()), "pos_days": int((pnl > 0).sum())}


def verdict(res: Dict) -> bool:
    a, b = res.get("in_sample", {}), res.get("holdout", {})
    return bool(a and b and a["pnl_total"] > 0 and b["pnl_total"] > 0 and a["sharpe_ann"] > 1)


def build_results(per_symbol: Dict) -> Dict:
    results: Dict = {}
    for c in cells():
        key = cell_key(c)
        for fee_name, fee in MAKER_FEES.items():
            entry: Dict = {"cell": c, "maker_fee": fee}
            for period, days in (("in_sample", IN_SAMPLE_DAYS), ("holdout", HOLDOUT_DAYS)):
                entry[period] = summarize(daily_series(per_symbol, key, days, fee))
                entry[period + "_daily_pnl"] = daily_series(per_symbol, key, days, fee)["pnl"].tolist()
                entry[period + "_by_symbol"] = {
                    s: summarize(daily_series(per_symbol, key, days, fee, [s])) for s in per_symbol}
            entry["pass"] = verdict(entry) if (not c["equal_fills"] and fee_name == "maker_0.02") else None
            results[f"{key}__{fee_name}"] = entry
    return results


def _f(x: float, nd: int = 2) -> str:
    return "n/d" if x is None or (isinstance(x, float) and np.isnan(x)) else f"{x:,.{nd}f}"


def tables_md(results: Dict) -> str:
    lines = ["# Market making: tabelas geradas", ""]
    for mode, mode_label in (("eoq", "fim de fila"), ("equal", "preço igual preenche")):
        for fee_name in MAKER_FEES:
            lines += [f"## Modelo {mode_label}, {fee_name.replace('_', ' ')}", "",
                      "| s bps | R s | $/ordem | dias IS | P&L IS $ | $/dia IS | Sharpe ann IS | DD IS | fills IS | flattens | markout 1s bps | markout 10s bps | vs mid 1s | vs mid 10s | $/1k preenchido | inv máx $ | P&L HO $ | $/dia HO | Sharpe ann HO | PASS |",
                      "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
            for c in cells():
                if (c["equal_fills"] and mode != "equal") or (not c["equal_fills"] and mode != "eoq"):
                    continue
                e = results[f"{cell_key(c)}__{fee_name}"]
                a, b = e["in_sample"], e["holdout"]
                p = "" if e["pass"] is None else ("PASS" if e["pass"] else "FAIL")
                lines.append(
                    f"| {c['spread_bps']} | {c['requote_s']} | {int(c['notional'])} | {a.get('days', 0)} | {_f(a.get('pnl_total'))} | "
                    f"{_f(a.get('pnl_per_day'))} | {_f(a.get('sharpe_ann'), 1)} | {_f(a.get('max_dd'))} | {a.get('fills', 0)} | "
                    f"{a.get('flatten', 0)} | {_f(a.get('markout_1s_bps'), 1)} | {_f(a.get('markout_10s_bps'), 1)} | "
                    f"{_f(a.get('adverse_mid_1s_bps'), 1)} | {_f(a.get('adverse_mid_10s_bps'), 1)} | {_f(a.get('pnl_per_1k_filled'))} | "
                    f"{_f(a.get('max_inv'), 0)} | {_f(b.get('pnl_total'))} | {_f(b.get('pnl_per_day'))} | {_f(b.get('sharpe_ann'), 1)} | {p} |")
            lines.append("")
    lines += ["## Por símbolo, fim de fila, maker 0,02%, $50/ordem (P&L total IS $ / markout 10s bps / fills IS)", "",
              "| s bps | R s | " + " | ".join(SYMBOLS) + " |", "|---|---|" + "---|" * len(SYMBOLS)]
    for s in SPREADS_BPS:
        for r in REQUOTES_S:
            e = results[f"s{s}_r{r}_n50_eoq__maker_0.02"]
            cellsv = []
            for sym in SYMBOLS:
                m = e["in_sample_by_symbol"].get(sym, {})
                cellsv.append(f"{_f(m.get('pnl_total'))} / {_f(m.get('markout_10s_bps'), 1)} / {m.get('fills', 0)}")
            lines.append(f"| {s} | {r} | " + " | ".join(cellsv) + " |")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--download-only", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if args.download_only:
        for sym in SYMBOLS:
            for day in IN_SAMPLE_DAYS + HOLDOUT_DAYS:
                load_day(sym, day)
        return
    from concurrent.futures import ProcessPoolExecutor
    with ProcessPoolExecutor(args.workers) as ex:
        per_symbol = dict(zip(SYMBOLS, ex.map(run_symbol, SYMBOLS)))
    results = build_results(per_symbol)
    MM_OUT.mkdir(parents=True, exist_ok=True)
    (MM_OUT / "results.json").write_text(json.dumps(results, indent=1, default=float))
    (MM_OUT / "tables.md").write_text(tables_md(results))
    logger.info("saída em %s", MM_OUT)


if __name__ == "__main__":
    main()

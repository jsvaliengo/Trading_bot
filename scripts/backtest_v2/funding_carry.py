"""Cash-and-carry de funding (spot long + perp USDT-M short), pré-registrado (FUNDING_PREREG.md).

    PYTHONPATH=. python -I -m scripts.backtest_v2.funding_carry download
    PYTHONPATH=. python -I -m scripts.backtest_v2.funding_carry run
    PYTHONPATH=. python -I -m scripts.backtest_v2.funding_carry report

Grade de 8h nos instantes de funding; preço = close da barra 4h que fecha no instante.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

from . import vision
from .data import OUT_DIR, ms_to_date, to_ms

logger = logging.getLogger("backtest_v2.funding_carry")

OUT = OUT_DIR / "funding_carry"
FULL = (to_ms("2022-01-01"), to_ms("2026-10-01"))
HOLDOUT = (to_ms("2026-01-01"), to_ms("2026-10-01"))
STEP = 8 * 3_600_000
STEPS_PER_YEAR = 1095
MAINT = 0.01
FEES = {"A_taker": (0.001, 0.0005), "B_maker": (0.001, 0.0002)}
FIXED = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT"]
TOP10 = ["BIGTIMEUSDT", "WIFUSDT", "1000SATSUSDT", "ORDIUSDT", "AGIXUSDT", "1000BONKUSDT", "RNDRUSDT",
         "1000PEPEUSDT", "CFXUSDT", "JTOUSDT"]
UNIVERSE = FIXED + TOP10
CAPITAL = 300.0
SPOT_RAW = vision.RAW_DIR / "spot_klines" / "4h"
SPOT_OUT = vision.OUT_DIR / "spot_klines_4h"


# ───────────────────────── spot (data.binance.vision, público) ─────────────────────────
def spot_symbol(sym: str):
    """Perp '1000PEPEUSDT' → ('PEPEUSDT', 1000). 1000SATS existe com esse nome no spot."""
    if sym == "1000SATSUSDT":
        return sym, 1.0
    m = re.match(r"^(1000000|1000|1M)(.+)$", sym)
    return (m.group(2), 1_000_000.0 if m.group(1) in ("1000000", "1M") else 1000.0) if m else (sym, 1.0)


def _months(t0: int, t1: int) -> List[str]:
    d = dt.datetime.fromtimestamp(t0 / 1000, tz=dt.timezone.utc).replace(day=1)
    out = []
    while int(d.timestamp() * 1000) < t1:
        out.append(d.strftime("%Y-%m"))
        d = d.replace(year=d.year + 1, month=1) if d.month == 12 else d.replace(month=d.month + 1)
    return out


def download_spot(sym: str, t0: int = FULL[0] - 31 * 86_400_000, t1: int = FULL[1]) -> int:
    """Zips mensais 4h do spot (404 = mês sem dado, gravado como vazio para não rebaixar)."""
    name, _ = spot_symbol(sym)
    if not vision._SYM_RE.match(name):
        raise ValueError(f"símbolo inválido: {name!r}")
    http, n = vision._Http(), 0
    d = SPOT_RAW / name
    d.mkdir(parents=True, exist_ok=True)
    for ym in _months(t0, t1):
        dst = d / f"{name}-4h-{ym}.zip"
        if dst.exists() or dst.with_suffix(".404").exists():
            continue
        blob = http.get(f"{vision.FILE_URL}data/spot/monthly/klines/{name}/4h/{name}-4h-{ym}.zip")
        if blob is None:
            dst.with_suffix(".404").write_bytes(b"")
            continue
        tmp = dst.with_suffix(".part")
        tmp.write_bytes(blob)
        os.replace(tmp, dst)
        n += 1
    zips = sorted(d.glob("*.zip"))
    parts = [vision.parse_klines(z.read_bytes(), "4h") for z in zips]
    a = vision._dedup_sort(np.concatenate(parts) if parts else np.zeros((0, 7)))
    SPOT_OUT.mkdir(parents=True, exist_ok=True)
    tmp = SPOT_OUT / f"{name}.tmp.npz"
    np.savez_compressed(tmp, a=a, n_zips=len(zips))
    os.replace(tmp, SPOT_OUT / f"{name}.npz")
    return n


def load_spot(sym: str) -> np.ndarray:
    name, mult = spot_symbol(sym)
    a = np.load(SPOT_OUT / f"{name}.npz")["a"]
    a = a[a[:, 5] > 0]
    a[:, 1:5] *= mult
    return a


# ───────────────────────── grade de 8h por símbolo ─────────────────────────
@dataclass
class Series:
    """Por passo t (instante de funding): close spot/perp da barra 4h que fecha em t, máxima do perp
    nas 2 barras 4h do passo, funding somado em (t-8h, t]. NaN onde falta dado."""
    symbol: str
    t: np.ndarray
    spot: np.ndarray
    perp: np.ndarray
    perp_high: np.ndarray
    funding: np.ndarray

    @property
    def ok(self) -> np.ndarray:
        return np.isfinite(self.spot) & np.isfinite(self.perp)


def _at_close(a: np.ndarray, t: np.ndarray, tf_ms: int) -> np.ndarray:
    """Close da barra cujo open_time = t - tf (fecha em t); NaN se não existe."""
    out = np.full(len(t), np.nan)
    idx = np.searchsorted(a[:, 0], t - tf_ms)
    hit = (idx < len(a)) & (a[np.minimum(idx, len(a) - 1), 0] == t - tf_ms)
    out[hit] = a[idx[hit], 4]
    return out


def build_series(sym: str, t0: int, t1: int) -> Series:
    t = np.arange(t0, t1, STEP, dtype=np.int64)
    perp = vision.load_klines(sym, "4h")
    spot = load_spot(sym)
    h4 = 4 * 3_600_000
    hi = np.full(len(t), np.nan)
    for off in (h4, 2 * h4):
        idx = np.searchsorted(perp[:, 0], t - off)
        hit = (idx < len(perp)) & (perp[np.minimum(idx, len(perp) - 1), 0] == t - off)
        hi[hit] = np.fmax(hi[hit], perp[idx[hit], 2])
    f = vision.load_funding(sym)
    fund = np.zeros(len(t))
    f = f[f[:, 0] > t0 - STEP] if len(f) else f  # evento antes da grade não pode cair no passo 0
    if len(f):
        k = np.searchsorted(t, f[:, 0], side="left")  # evento em (t-8h, t] cai no passo t
        inb = k < len(t)
        np.add.at(fund, k[inb], f[inb, 1])
    return Series(sym, t, _at_close(spot, t, h4), _at_close(perp, t, h4), hi, fund)


def signal(fund: np.ndarray, k: int) -> np.ndarray:
    """Média dos últimos k passos (inclusive o atual) × 1095; NaN até ter k passos."""
    c = np.cumsum(np.r_[0.0, fund])
    out = np.full(len(fund), np.nan)
    out[k - 1:] = (c[k:] - c[:-k]) / k * STEPS_PER_YEAR
    return out


# ───────────────────────── simulador de carteira ─────────────────────────
def simulate(series: List[Series], target: np.ndarray, n_slots: int, lev: float, fees: tuple,
             capital: float = CAPITAL) -> dict:
    """target[t, s]: querer estar no símbolo s ao fim do passo t (decidido com dados ≤ t).
    Ordem no passo: liquidação (máxima do perp) → saídas → funding (posições abertas antes de t)
    → rebalance → entradas (caixa do pool / vagas livres). Caixa parado rende 0."""
    fs, fp = fees
    T, S = target.shape
    pool, open_ = capital, {}  # sym_idx -> [q, M, Pe, last_S, last_P, opened_t]
    eq = np.zeros(T)
    st = {"entries": 0, "exits": 0, "rebalances": 0, "liquidations": 0, "fees": 0.0,
          "funding": 0.0, "steps_in": 0, "n_slots": n_slots}

    def close(i, S_, P_, liquidated=False):
        q, M, Pe, *_ = open_.pop(i)
        if liquidated:
            perp_eq = 0.0
        else:
            perp_eq = M + q * (Pe - P_) - q * P_ * fp
            st["fees"] += q * P_ * fp
        st["fees"] += q * S_ * fs
        return q * S_ * (1 - fs) + perp_eq

    for t in range(T):
        # liquidação e saídas
        for i in list(open_):
            s = series[i]
            q, M, Pe, lS, lP, t_open = open_[i]
            if not s.ok[t]:
                pool += close(i, lS, lP)
                st["exits"] += 1
                continue
            Ph = s.perp_high[t] if np.isfinite(s.perp_high[t]) else s.perp[t]
            if M + q * (Pe - Ph) <= MAINT * q * Ph:
                pool += close(i, s.spot[t], s.perp[t], liquidated=True)
                st["liquidations"] += 1
                continue
            if not target[t, i]:
                pool += close(i, s.spot[t], s.perp[t])
                st["exits"] += 1
        # funding + rebalance
        for i in list(open_):
            s = series[i]
            pos = open_[i]
            q, M, Pe, _, _, t_open = pos
            S_, P_ = s.spot[t], s.perp[t]
            if t_open < t:
                got = s.funding[t] * q * P_
                M += got
                st["funding"] += got
            E = M + q * (Pe - P_)
            if E < q * P_ / (2 * lev):
                f = (q * P_ - lev * E) / (q * (lev * S_ + P_))
                if f >= 1:
                    pos[1] = M
                    pool += close(i, S_, P_)
                    st["exits"] += 1
                    continue
                M += f * q * (Pe - P_) - f * q * P_ * fp + f * q * S_ * (1 - fs)
                st["fees"] += f * q * (P_ * fp + S_ * fs)
                q *= 1 - f
                st["rebalances"] += 1
            pos[0], pos[1], pos[3], pos[4] = q, M, S_, P_
        # entradas
        want = [i for i in range(S) if target[t, i] and series[i].ok[t] and i not in open_]
        for i in want[: n_slots - len(open_)]:
            s = series[i]
            c = pool / (n_slots - len(open_))
            pool -= c
            S_, P_ = s.spot[t], s.perp[t]
            q = c * lev / (lev + 1) / (S_ * (1 + fs))
            M = c / (lev + 1) - q * P_ * fp
            st["fees"] += q * S_ * fs + q * P_ * fp
            st["entries"] += 1
            open_[i] = [q, M, P_, S_, P_, t]
        st["steps_in"] += len(open_)
        eq[t] = pool + sum(q * series[i].spot[t] + M + q * (Pe - series[i].perp[t])
                           for i, (q, M, Pe, *_) in open_.items())
    st["equity"] = eq
    return st


def metrics(eq: np.ndarray, t: np.ndarray, capital: float, st: Optional[dict] = None) -> dict:
    eq = np.r_[capital, eq]
    r = np.diff(eq) / eq[:-1]
    n = len(r)
    sd = r.std()
    peak = np.maximum.accumulate(eq)
    months = np.array([ms_to_date(int(x))[:7] for x in t])
    mret = {}
    prev = capital
    for m in sorted(set(months)):
        last = eq[1:][months == m][-1]
        mret[m] = (last - prev) / prev
        prev = last
    worst = min(mret.items(), key=lambda kv: kv[1])
    out = {
        "net_usd": float(eq[-1] - capital), "net_pct": float(eq[-1] / capital - 1),
        "cagr": float((max(eq[-1], 1e-9) / capital) ** (STEPS_PER_YEAR / n) - 1),
        "sharpe": float(r.mean() / sd * np.sqrt(STEPS_PER_YEAR)) if sd > 0 else 0.0,
        "max_dd": float((1 - eq / peak).max()), "worst_month": worst[0], "worst_month_pct": float(worst[1]),
        "steps": int(n),
    }
    if st:
        out.update({k: st[k] for k in ("entries", "exits", "rebalances", "liquidations", "funding")})
        out["fees_usd"] = st["fees"]
        out["basis"] = out["net_usd"] - st["funding"] + st["fees"]  # descasamento spot/perp + margem perdida em liquidação
        out["time_in_pct"] = st["steps_in"] / n / st["n_slots"]
    return out


# ───────────────────────── variantes → target[t, s] ─────────────────────────
def target_threshold(sig: np.ndarray, ok: np.ndarray, theta: float) -> np.ndarray:
    out, inside = np.zeros(len(sig), dtype=bool), False
    for t in range(len(sig)):
        if not ok[t] or not np.isfinite(sig[t]):
            inside = False
        elif inside:
            inside = sig[t] >= theta / 2
        else:
            inside = sig[t] > theta
        out[t] = inside
    return out


def target_rotation(sigs: np.ndarray, oks: np.ndarray, t: np.ndarray, n: int, daily: bool) -> np.ndarray:
    """sigs/oks [T, S]. Em cada instante de rebalance: fica quem está no top-n com sinal > 0,
    vagas vão para o melhor fora da carteira com sinal > 0."""
    T, S = sigs.shape
    out = np.zeros((T, S), dtype=bool)
    held: set = set()
    for i in range(T):
        held = {s for s in held if oks[i, s]}
        if not daily or t[i] % 86_400_000 == 0:
            score = np.where(oks[i] & np.isfinite(sigs[i]) & (sigs[i] > 0), sigs[i], -np.inf)
            top = [s for s in np.argsort(-score)[:n] if score[s] > 0]
            held = {s for s in held if s in top}
            for s in top:
                if len(held) >= n:
                    break
                held.add(s)
        out[i, list(held)] = True
    return out


# ───────────────────────── execução ─────────────────────────
def _slice(series: List[Series], i0: int) -> List[Series]:
    return [Series(s.symbol, s.t[i0:], s.spot[i0:], s.perp[i0:], s.perp_high[i0:], s.funding[i0:]) for s in series]


def run_all(capital: float = CAPITAL) -> dict:
    series = [build_series(s, FULL[0] - 9 * STEP, FULL[1]) for s in UNIVERSE]
    t = series[0].t
    i_full, i_hold = int(np.searchsorted(t, FULL[0])), int(np.searchsorted(t, HOLDOUT[0]))
    sigs = {k: np.column_stack([signal(s.funding, k) for s in series]) for k in (3, 9)}
    oks = np.column_stack([s.ok for s in series])
    res = {"universe": UNIVERSE, "capital": capital, "coverage": {}, "runs": []}
    for s in series:
        ok = s.ok[i_full:]
        res["coverage"][s.symbol] = [ms_to_date(int(s.t[i_full:][ok][0])), ms_to_date(int(s.t[i_full:][ok][-1])),
                                     int(ok.sum())] if ok.any() else None

    def record(name: str, cell: dict, target: np.ndarray, n_slots: int, lev: float, idx: List[int]):
        assert target.shape[1] == len(idx)
        for fee_name, fees in FEES.items():
            for period, i0 in (("full", i_full), ("holdout", i_hold)):
                sub = _slice([series[i] for i in idx], i0)
                st = simulate(sub, target[i0:], n_slots, lev, fees, capital)
                m = metrics(st["equity"], t[i0:], capital, st)
                res["runs"].append({"variant": name, "cell": cell, "fees": fee_name, "period": period, **m})

    for j, s in enumerate(series):
        for lev in (1.0, 2.0):
            record("v0_always", {"symbol": s.symbol, "L": lev}, oks[:, [j]], 1, lev, [j])
        for k in (3, 9):
            for theta in (0.05, 0.10, 0.20):
                tg = target_threshold(sigs[k][:, j], s.ok, theta)[:, None]
                record("v1_threshold", {"symbol": s.symbol, "k": k, "theta": theta, "L": 1.0}, tg, 1, 1.0, [j])
        tg = target_threshold(sigs[9][:, j], s.ok, 0.10)[:, None]
        record("v1_threshold", {"symbol": s.symbol, "k": 9, "theta": 0.10, "L": 2.0}, tg, 1, 2.0, [j])
    allidx = list(range(len(series)))
    for n in (1, 3):
        for daily in (False, True):
            tg = target_rotation(sigs[9], oks, t, n, daily)
            record("v2_rotation", {"n": n, "rebalance": "daily" if daily else "8h"}, tg, n, 1.0, allidx)
    btc = series[UNIVERSE.index("BTCUSDT")]
    for period, i0 in (("full", i_full), ("holdout", i_hold)):
        sp = btc.spot[i0:]
        eq = capital * (1 - FEES["A_taker"][0]) * sp / sp[0]
        res["runs"].append({"variant": "bench_btc_hold", "cell": {}, "fees": "A_taker", "period": period,
                            **metrics(eq, t[i0:], capital)})
    return res


def _fmt(m: dict, capital: float) -> str:
    return (f"{m['net_usd']:+.0f} ({m['net_pct']:+.1%}; {m['cagr']:+.1%} a.a.) | {m['sharpe']:.2f} | {m['max_dd']:.1%} | "
            f"{m['worst_month']} {m['worst_month_pct']:+.1%} | {m.get('fees_usd', 0):.0f} | {m.get('funding', 0):+.0f} | "
            f"{m.get('basis', 0):+.0f} | {m.get('entries', 0)}/{m.get('rebalances', 0)}/{m.get('liquidations', 0)} | "
            f"{m.get('time_in_pct', 1):.0%} | {m['net_usd'] * 3000 / capital:+.0f}")


def report(res: dict) -> str:
    cap = res["capital"]
    hdr = ("| célula | período | líquido $300 (%; CAGR) | Sharpe | DD máx | pior mês | taxas $ | funding $ | basis $ | "
           "entradas/rebal/liq | tempo em pos | líquido $3.000 |\n|---|---|---|---|---|---|---|---|---|---|---|---|\n")
    out = ["# Tabelas: cash-and-carry de funding\n", f"Universo: {', '.join(res['universe'])}\n",
           "## Cobertura (primeiro passo, último passo, passos com spot E perp)\n"]
    out += [f"- {s}: {c}" for s, c in res["coverage"].items()]
    for fee in FEES:
        for variant in ("v0_always", "v1_threshold", "v2_rotation", "bench_btc_hold"):
            out.append(f"\n## {variant} — taxas {fee}\n\n" + hdr.rstrip("\n"))
            for r in res["runs"]:
                if r["variant"] == variant and r["fees"] == fee:
                    cell = ", ".join(f"{k}={v}" for k, v in r["cell"].items())
                    out.append(f"| {cell} | {r['period']} | {_fmt(r, cap)} |")
    return "\n".join(out) + "\n" + verdict(res)


def verdict(res: dict) -> str:
    """Critérios pré-registrados (tabela A): líquido > 0 no completo E no holdout, DD < 10%, Sharpe > 1."""
    runs = [r for r in res["runs"] if r["fees"] == "A_taker" and r["variant"] != "bench_btc_hold"]
    by = {}
    for r in runs:
        by.setdefault((r["variant"], json.dumps(r["cell"], sort_keys=True)), {})[r["period"]] = r
    passed = {}
    for (v, c), p in by.items():
        f, h = p["full"], p["holdout"]
        passed[(v, c)] = f["net_usd"] > 0 and h["net_usd"] > 0 and f["max_dd"] < 0.10 and f["sharpe"] > 1
    out = ["\n## Critérios pré-registrados (taxas A)\n"]
    for v in ("v0_always", "v1_threshold", "v2_rotation"):
        cells = [(c, ok) for (vv, c), ok in passed.items() if vv == v]
        out.append(f"- {v}: {sum(ok for _, ok in cells)}/{len(cells)} células passam")
        for c, ok in cells:
            if ok:
                out.append(f"  - PASS {c}")
    return "\n".join(out) + "\n"


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("download")
    r = sub.add_parser("run")
    r.add_argument("--capital", type=float, default=CAPITAL)
    sub.add_parser("report")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if a.cmd == "download":
        with ThreadPoolExecutor(6) as ex:
            for s, n in zip(UNIVERSE, ex.map(download_spot, UNIVERSE)):
                logger.info("%s: %d zips novos", s, n)
    elif a.cmd == "run":
        res = run_all(a.capital)
        (OUT / "results.json").write_text(json.dumps(res, indent=1))
        (OUT / "tables.md").write_text(report(res))
        logger.info("%d simulações → %s", len(res["runs"]), OUT)
    else:
        print(report(json.loads((OUT / "results.json").read_text())))


if __name__ == "__main__":
    main()

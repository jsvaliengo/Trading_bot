"""Camada de dados do backtest_v2: klines + funding históricos com cache local.

- Só endpoints PÚBLICOS da Binance USDT-M (fapi.binance.com), sem API key.
- Range FIXO (reprodutível): mesmo range → mesmos arquivos → mesmos resultados.
- Cache por (símbolo, timeframe, mês) em runtime/backtest_v2/cache/ (gitignored
  via runtime/backtest_v2/.gitignore). Mês completo baixado nunca é rebaixado.
- Respeita rate limit lendo o header X-MBX-USED-WEIGHT-1M.
- Símbolo listado depois do início: o cache simplesmente começa na 1ª barra
  existente; `coverage()` reporta a data efetiva por símbolo.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import requests

logger = logging.getLogger("backtest_v2.data")

ROOT = Path(__file__).resolve().parents[2]
BT_DIR = ROOT / "runtime" / "backtest_v2"
CACHE_DIR = BT_DIR / "cache"
OUT_DIR = BT_DIR / "out"

FAPI = "https://fapi.binance.com"
DEFAULT_START = "2023-10-01"
DEFAULT_END = "2026-10-01"

# Universo do PDF "Binance - Estratégia Padrão" (36 moedas) sem 1000PEPE = as 35
# que estavam em TRADING_BOT_BINANCE_UNIVERSE_WHITELIST na VM (13/06–21/06).
# Fallback só quando config.BINANCE_UNIVERSE_WHITELIST está vazio localmente
# (a whitelist real mora no .env da VM, que não é lido aqui).
PDF_UNIVERSE_35 = [
    "1000SHIBUSDT", "1INCHUSDT", "AAVEUSDT", "ADAUSDT", "APEUSDT", "APTUSDT",
    "ARBUSDT", "ATOMUSDT", "AVAXUSDT", "BCHUSDT", "BNBUSDT", "CFXUSDT",
    "CHZUSDT", "COMPUSDT", "CRVUSDT", "DOGEUSDT", "DOTUSDT", "ETCUSDT",
    "ETHUSDT", "FILUSDT", "LINKUSDT", "LTCUSDT", "MASKUSDT", "NEARUSDT",
    "NOTUSDT", "OPUSDT", "SANDUSDT", "SNXUSDT", "SOLUSDT", "SUIUSDT",
    "SUSHIUSDT", "THETAUSDT", "TRXUSDT", "UNIUSDT", "XRPUSDT",
]

_UNIT_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}


def tf_ms(tf: str) -> int:
    return int(tf[:-1]) * _UNIT_MS[tf[-1]]


def to_ms(date_str: str) -> int:
    d = dt.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp() * 1000)


def ms_to_date(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m-%d")


def load_universe() -> List[str]:
    """Universo = config.BINANCE_UNIVERSE_WHITELIST; fallback = 35 do PDF."""
    from trading_bot.core.config import config

    wl = list(getattr(config, "BINANCE_UNIVERSE_WHITELIST", None) or [])
    return sorted(wl) if wl else list(PDF_UNIVERSE_35)


# ───────────────────────────── HTTP com rate limit ─────────────────────────────
class _Http:
    def __init__(self, max_weight_per_min: int = 1800):
        self.s = requests.Session()
        self.max_w = max_weight_per_min

    def get(self, path: str, params: dict):
        for attempt in range(8):
            try:
                r = self.s.get(FAPI + path, params=params, timeout=30)
            except requests.RequestException as exc:
                wait = 2 ** attempt
                logger.warning("rede: %s — retry em %ss", exc, wait)
                time.sleep(wait)
                continue
            if r.status_code in (418, 429):
                wait = int(r.headers.get("Retry-After", "60"))
                logger.warning("rate limit %s — dormindo %ss", r.status_code, wait)
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            used = int(r.headers.get("X-MBX-USED-WEIGHT-1M", "0") or 0)
            if used > self.max_w:
                # espera virar o minuto do contador de peso
                time.sleep(61 - dt.datetime.now().second)
            return r.json()
        raise RuntimeError(f"falha persistente em {path} {params}")


# ───────────────────────────── klines ─────────────────────────────
KL_COLS = ("open_time", "open", "high", "low", "close", "volume")


def _month_starts(start_ms: int, end_ms: int) -> List[int]:
    d = dt.datetime.fromtimestamp(start_ms / 1000, tz=dt.timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)
    out = []
    while int(d.timestamp() * 1000) < end_ms:
        out.append(int(d.timestamp() * 1000))
        d = (d.replace(year=d.year + 1, month=1) if d.month == 12 else d.replace(month=d.month + 1))
    return out


def _month_end(ms: int) -> int:
    d = dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc)
    nxt = d.replace(year=d.year + 1, month=1) if d.month == 12 else d.replace(month=d.month + 1)
    return int(nxt.timestamp() * 1000)


def _kl_path(sym: str, tf: str, month_ms: int) -> Path:
    tag = dt.datetime.fromtimestamp(month_ms / 1000, tz=dt.timezone.utc).strftime("%Y-%m")
    return CACHE_DIR / "klines" / tf / sym / f"{tag}.npz"


def _download_kl_month(http: _Http, sym: str, tf: str, m0: int, m1: int) -> np.ndarray:
    step = tf_ms(tf)
    rows = []
    cur = m0
    while cur < m1:
        raw = http.get("/fapi/v1/klines", {
            "symbol": sym, "interval": tf, "startTime": cur, "endTime": m1 - 1, "limit": 1000})
        if not raw:
            break
        for k in raw:
            ot = int(k[0])
            if m0 <= ot < m1:
                rows.append((ot, float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])))
        last = int(raw[-1][0])
        if len(raw) < 1000:
            break
        cur = last + step
    arr = np.array(rows, dtype=np.float64).reshape(-1, 6)
    if len(arr):
        _, idx = np.unique(arr[:, 0], return_index=True)
        arr = arr[idx]
    return arr


def fetch_start_for(tf: str, start_ms: int, warmup_bars: int) -> int:
    return start_ms - (warmup_bars + 20) * tf_ms(tf)


def ensure_klines(sym: str, tf: str, start_ms: int, end_ms: int, warmup_bars: int,
                  http: Optional[_Http] = None, listing_ms: Optional[int] = None) -> int:
    """Garante cache mensal de [start - warmup, end). Retorna nº de meses baixados."""
    http = http or _Http()
    f0 = fetch_start_for(tf, start_ms, warmup_bars)
    n = 0
    for m0 in _month_starts(f0, end_ms):
        m1 = _month_end(m0)
        p = _kl_path(sym, tf, m0)
        if p.exists():
            continue
        if listing_ms is not None and m1 <= listing_ms:
            arr = np.zeros((0, 6))
        else:
            arr = _download_kl_month(http, sym, tf, m0, m1)
        if m1 > int(time.time() * 1000):
            # mês ainda aberto: não grava (cache só de meses fechados)
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp.npz")
        np.savez_compressed(tmp, a=arr)
        os.replace(tmp, p)  # escrita atômica: download interrompido não deixa arquivo corrompido
        n += 1
    return n


@dataclass
class Bars:
    """Barras de um símbolo/TF. close_time = open_time + tf - 1 (ms)."""
    symbol: str
    tf: str
    open_time: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray

    @property
    def close_time(self) -> np.ndarray:
        return self.open_time + tf_ms(self.tf) - 1

    def __len__(self) -> int:
        return len(self.open_time)

    def slice_time(self, t0: int, t1: int) -> "Bars":
        i0 = int(np.searchsorted(self.open_time, t0, "left"))
        i1 = int(np.searchsorted(self.open_time, t1, "left"))
        return Bars(self.symbol, self.tf, self.open_time[i0:i1], self.open[i0:i1], self.high[i0:i1],
                    self.low[i0:i1], self.close[i0:i1], self.volume[i0:i1])


def load_bars(sym: str, tf: str, start_ms: int, end_ms: int, warmup_bars: int) -> Bars:
    f0 = fetch_start_for(tf, start_ms, warmup_bars)
    parts = []
    for m0 in _month_starts(f0, end_ms):
        p = _kl_path(sym, tf, m0)
        if not p.exists():
            raise FileNotFoundError(f"cache ausente: {p} — rode `download` antes")
        a = np.load(p)["a"]
        if len(a):
            parts.append(a)
    a = np.concatenate(parts) if parts else np.zeros((0, 6))
    a = a[(a[:, 0] >= f0) & (a[:, 0] < end_ms)]
    ot = a[:, 0].astype(np.int64)
    return Bars(sym, tf, ot, a[:, 1].copy(), a[:, 2].copy(), a[:, 3].copy(), a[:, 4].copy(), a[:, 5].copy())


# ───────────────────────────── funding ─────────────────────────────
def _funding_path(sym: str, start_ms: int, end_ms: int) -> Path:
    return CACHE_DIR / "funding" / f"{sym}_{ms_to_date(start_ms)}_{ms_to_date(end_ms)}.npz"


def ensure_funding(sym: str, start_ms: int, end_ms: int, http: Optional[_Http] = None) -> bool:
    p = _funding_path(sym, start_ms, end_ms)
    if p.exists():
        return False
    http = http or _Http()
    rows = []
    cur = start_ms
    while cur < end_ms:
        raw = http.get("/fapi/v1/fundingRate", {
            "symbol": sym, "startTime": cur, "endTime": end_ms - 1, "limit": 1000})
        if not raw:
            break
        for r in raw:
            t = int(r["fundingTime"])
            if start_ms <= t < end_ms:
                mp = r.get("markPrice") or "nan"
                rows.append((t, float(r["fundingRate"]), float(mp) if mp != "" else np.nan))
        if len(raw) < 1000:
            break
        cur = int(raw[-1]["fundingTime"]) + 1
        time.sleep(0.3)  # endpoint tem limite próprio (500 req/5min/IP)
    arr = np.array(rows, dtype=np.float64).reshape(-1, 3)
    if len(arr):
        _, idx = np.unique(arr[:, 0], return_index=True)
        arr = arr[idx]
    p.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(p, a=arr)
    return True


def load_funding(sym: str, start_ms: int, end_ms: int) -> np.ndarray:
    """Array (n,3): [funding_time_ms, rate, mark_price]."""
    p = _funding_path(sym, start_ms, end_ms)
    if not p.exists():
        raise FileNotFoundError(f"funding ausente: {p} — rode `download` antes")
    return np.load(p)["a"]


# ───────────────────────────── orquestração ─────────────────────────────
def listing_dates(http: Optional[_Http] = None) -> Dict[str, int]:
    """onboardDate (ms) por símbolo. Cacheado em disco p/ reprodutibilidade."""
    p = CACHE_DIR / "listing_dates.json"
    if p.exists():
        return json.loads(p.read_text())
    http = http or _Http()
    info = http.get("/fapi/v1/exchangeInfo", {})
    out = {s["symbol"]: int(s.get("onboardDate", 0)) for s in info["symbols"]}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, sort_keys=True))
    return out


def download_all(symbols: List[str], tfs: Dict[str, int], start_ms: int, end_ms: int,
                 with_funding: bool = True) -> None:
    """tfs: {timeframe: warmup_bars}. Baixa TFs maiores primeiro (rápidos)."""
    http = _Http()
    lst = listing_dates(http)
    order = sorted(tfs, key=tf_ms, reverse=True)
    for sym in symbols:
        if sym not in lst:
            logger.warning("%s não existe na exchange (hoje) — pulando", sym)
    syms = [s for s in symbols if s in lst]
    if with_funding:
        for sym in syms:
            if ensure_funding(sym, start_ms, end_ms, http):
                logger.info("funding %s ok", sym)
    from concurrent.futures import ThreadPoolExecutor

    def _one(sym, tf):
        n = ensure_klines(sym, tf, start_ms, end_ms, tfs[tf], _Http(), lst.get(sym))
        if n:
            logger.info("klines %s %s: %d meses", sym, tf, n)

    for tf in order:
        t0 = time.time()
        # N threads (BT_DL_THREADS, padrão 6): o throttle por X-MBX-USED-WEIGHT-1M (peso global do IP)
        # segura o ritmo abaixo do limite de 2400/min.
        with ThreadPoolExecutor(max_workers=int(os.getenv("BT_DL_THREADS", "6"))) as ex:
            list(ex.map(lambda s: _one(s, tf), syms))
        logger.info("TF %s concluído em %.0fs", tf, time.time() - t0)


def coverage(symbols: List[str], tf: str, start_ms: int, end_ms: int, warmup_bars: int) -> Dict[str, dict]:
    out = {}
    for sym in symbols:
        try:
            b = load_bars(sym, tf, start_ms, end_ms, warmup_bars)
        except FileNotFoundError:
            out[sym] = {"first": None, "last": None, "bars": 0}
            continue
        if not len(b):
            out[sym] = {"first": None, "last": None, "bars": 0}
            continue
        gaps = int(np.sum(np.diff(b.open_time) != tf_ms(tf)))
        out[sym] = {"first": ms_to_date(int(b.open_time[0])), "last": ms_to_date(int(b.open_time[-1])),
                    "bars": int(len(b)), "gaps": gaps}
    return out


def cache_size_bytes() -> int:
    total = 0
    for dirpath, _d, files in os.walk(CACHE_DIR):
        for f in files:
            total += os.path.getsize(os.path.join(dirpath, f))
    return total

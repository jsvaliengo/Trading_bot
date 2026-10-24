"""Histórico público de data.binance.vision (USDT-M perps, inclusive deslistados).

- Lista símbolos e arquivos pela listagem S3 pública do bucket.
- Baixa os zips mensais para cache/vision_raw/ (retomável: arquivo existente não
  é baixado de novo; escrita atômica) e monta 1 npz por símbolo em cache/vision/.
- Os zips são dados não confiáveis: lidos em memória com zipfile + csv, nunca
  extraídos para disco nem executados. Nome de símbolo vira caminho só depois de
  passar pela regex `_SYM_RE`.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional
from xml.etree import ElementTree

import numpy as np
import requests

from .data import CACHE_DIR, Bars, tf_ms

logger = logging.getLogger("backtest_v2.vision")

LIST_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
FILE_URL = "https://data.binance.vision/"
RAW_DIR = CACHE_DIR / "vision_raw"
OUT_DIR = CACHE_DIR / "vision"
_SYM_RE = re.compile(r"^[A-Z0-9]{2,30}$")
_KEY_RE = re.compile(r"^data/futures/um/monthly/(klines|fundingRate)/[A-Z0-9]{2,30}/([0-9a-z]+/)?"
                     r"[A-Z0-9]{2,30}-[0-9a-zA-Z]+-\d{4}-\d{2}\.zip$")
STABLE_BASES = ("USDC", "BUSD", "TUSD", "USDP", "FDUSD", "DAI", "USDE", "PYUSD", "EUR", "GBP", "AEUR")
_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def is_candidate(sym: str) -> bool:
    """USDT perp (sem trimestral `_`), base que não é stablecoin/fiat."""
    return bool(_SYM_RE.match(sym)) and sym.endswith("USDT") and sym[:-4] not in STABLE_BASES


class _Http:
    def __init__(self, pause_s: float = 0.05):
        self.s = requests.Session()
        self.pause = pause_s

    def get(self, url: str, params: Optional[dict] = None) -> Optional[bytes]:
        for attempt in range(6):
            try:
                r = self.s.get(url, params=params, timeout=30)
            except requests.RequestException as exc:
                logger.warning("rede: %s, retry", exc)
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 404:
                return None
            if r.status_code in (403, 429, 503) or r.status_code >= 500:
                time.sleep(2 ** attempt * 2)
                continue
            r.raise_for_status()
            time.sleep(self.pause)
            return r.content
        raise RuntimeError(f"falha persistente em {url} {params}")


def _list(http: _Http, prefix: str, delimiter: bool) -> List[str]:
    """Chaves (ou subprefixos, com delimiter) sob `prefix`, paginando por marker."""
    out, marker = [], ""
    while True:
        params = {"prefix": prefix, "marker": marker}
        if delimiter:
            params["delimiter"] = "/"
        root = ElementTree.fromstring(http.get(LIST_URL, params) or b"<x/>")
        if delimiter:
            page = [e.text for e in root.findall(f"{_NS}CommonPrefixes/{_NS}Prefix") if e.text]
        else:
            page = [e.text for e in root.findall(f"{_NS}Contents/{_NS}Key") if e.text]
        out += page
        if (root.findtext(_NS + "IsTruncated") or "false") != "true" or not page:
            return out
        marker = page[-1]


def list_symbols(http: Optional[_Http] = None) -> List[str]:
    p = RAW_DIR / "symbols.json"
    if p.exists():
        return json.loads(p.read_text())
    http = http or _Http()
    pref = "data/futures/um/monthly/klines/"
    syms = sorted({x[len(pref):].rstrip("/") for x in _list(http, pref, True)})
    syms = [s for s in syms if _SYM_RE.match(s)]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(syms))
    return syms


def _prefix(kind: str, sym: str, tf: str) -> str:
    return f"data/futures/um/monthly/{kind}/{sym}/" + (f"{tf}/" if kind == "klines" else "")


def _raw_dir(kind: str, sym: str, tf: str) -> Path:
    return RAW_DIR / kind / (tf or "-") / sym


def list_keys(http: _Http, kind: str, sym: str, tf: str = "") -> List[str]:
    """Zips mensais do símbolo (manifesto cacheado: download retomável e reprodutível)."""
    p = _raw_dir(kind, sym, tf) / "_keys.json"
    if p.exists():
        return json.loads(p.read_text())
    keys = [k for k in _list(http, _prefix(kind, sym, tf), False) if _KEY_RE.match(k)]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sorted(keys)))
    return sorted(keys)


def ensure_raw(sym: str, kind: str, tf: str = "", http: Optional[_Http] = None) -> int:
    """Baixa os zips que faltam. Devolve quantos baixou."""
    if not _SYM_RE.match(sym):
        raise ValueError(f"símbolo inválido: {sym!r}")
    http = http or _Http()
    n = 0
    d = _raw_dir(kind, sym, tf)
    for key in list_keys(http, kind, sym, tf):
        dst = d / key.rsplit("/", 1)[1]
        if dst.exists():
            continue
        blob = http.get(FILE_URL + key)
        if blob is None:
            continue
        tmp = dst.with_suffix(".part")
        tmp.write_bytes(blob)
        os.replace(tmp, dst)
        n += 1
    return n


# ───────────────────────────── parsing (dado não confiável) ─────────────────────────────
def _rows(blob: bytes):
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        if z.testzip() is not None:
            raise ValueError("zip corrompido")
        for name in z.namelist():
            if not name.endswith(".csv") or "/" in name or "\\" in name:
                continue
            text = z.read(name).decode("utf-8", errors="replace")
            for row in csv.reader(io.StringIO(text)):
                if row and row[0].strip().isdigit():       # pula cabeçalho (arquivos novos têm, antigos não)
                    yield row


def _ms(x: str) -> int:
    v = int(x)
    return v // 1000 if v > 10 ** 14 else v               # alguns arquivos usam microssegundos


def parse_klines(blob: bytes, tf: str) -> np.ndarray:
    """(n,7): open_time, open, high, low, close, volume, quote_volume. Linhas inválidas caem fora."""
    step = tf_ms(tf)
    out = []
    for r in _rows(blob):
        if len(r) < 8:
            continue
        try:
            ot = _ms(r[0])
            o, h, lw, c, v, qv = (float(r[k]) for k in (1, 2, 3, 4, 5, 7))
        except ValueError:
            continue
        vals = (o, h, lw, c, v, qv)
        if ot % step or not all(np.isfinite(vals)) or min(o, h, lw, c) <= 0 or h < max(o, c, lw) or lw > min(o, c):
            continue
        out.append((ot, o, h, lw, c, v, qv))
    return np.array(out, dtype=np.float64).reshape(-1, 7)


def parse_funding(blob: bytes) -> np.ndarray:
    """(n,3): funding_time_ms, rate, mark_price (NaN: o arquivo não traz)."""
    out = []
    for r in _rows(blob):
        if len(r) < 3:
            continue
        try:
            t, rate = _ms(r[0]), float(r[2])
        except ValueError:
            continue
        if np.isfinite(rate) and abs(rate) < 0.05:
            out.append((t, rate, np.nan))
    return np.array(out, dtype=np.float64).reshape(-1, 3)


def _dedup_sort(a: np.ndarray) -> np.ndarray:
    if not len(a):
        return a
    _, idx = np.unique(a[:, 0], return_index=True)
    return a[idx]


def build(sym: str, kind: str, tf: str = "") -> Path:
    """Junta os zips do símbolo num npz (refaz se o número de zips mudou)."""
    d = _raw_dir(kind, sym, tf)
    zips = sorted(d.glob("*.zip"))
    out = OUT_DIR / (f"klines_{tf}" if kind == "klines" else "funding") / f"{sym}.npz"
    if out.exists() and int(np.load(out)["n_zips"]) == len(zips):
        return out
    parts = [parse_klines(z.read_bytes(), tf) if kind == "klines" else parse_funding(z.read_bytes()) for z in zips]
    width = 7 if kind == "klines" else 3
    a = _dedup_sort(np.concatenate(parts) if parts else np.zeros((0, width)))
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.npz")
    np.savez_compressed(tmp, a=a, n_zips=len(zips))
    os.replace(tmp, out)
    return out


def load_klines(sym: str, tf: str) -> np.ndarray:
    """Sem barras de volume zero: o arquivo de um par deslistado continua até hoje com
    barras planas de volume 0 no último preço; sem esse corte o par parece vivo."""
    a = np.load(OUT_DIR / f"klines_{tf}" / f"{sym}.npz")["a"]
    return a[a[:, 5] > 0]


def load_bars(sym: str, tf: str) -> Bars:
    a = load_klines(sym, tf)
    return Bars(sym, tf, a[:, 0].astype(np.int64), a[:, 1].copy(), a[:, 2].copy(), a[:, 3].copy(),
                a[:, 4].copy(), a[:, 5].copy())


def load_funding(sym: str) -> np.ndarray:
    p = OUT_DIR / "funding" / f"{sym}.npz"
    return np.load(p)["a"] if p.exists() else np.zeros((0, 3))


def download(syms: List[str], kind: str, tf: str = "", threads: int = 8) -> Dict[str, int]:
    def one(s):
        n = ensure_raw(s, kind, tf, _Http())
        build(s, kind, tf)
        return s, n
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=threads) as ex:
        res = dict(ex.map(one, syms))
    logger.info("%s %s: %d símbolos, %d zips novos em %.0fs", kind, tf, len(syms), sum(res.values()), time.time() - t0)
    return res

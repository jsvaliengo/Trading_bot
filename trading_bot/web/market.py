"""
Mercado BTCUSDT perpétuo (aba Mercado › BTC): velas, ticker (último/marca/índice, 24h, open
interest), funding atual/próximo/histórico e long/short ratio — tudo da API PÚBLICA da Bitget,
sem assinatura e independente da exchange do bot. Cache de MARKET_TTL_S por timeframe pra não
martelar a API com o poll do dashboard.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from ..infra.bitget_client import BAR_MAP, BASE_URL

SYMBOL = "BTCUSDT"
PRODUCT = "USDT-FUTURES"
INTERVALS = ("1m", "5m", "15m", "1h", "4h", "1d")
CANDLES = 200
MARKET_TTL_S = 10.0
TIMEOUT_S = 8.0

_lock = threading.Lock()
_cache: Dict[str, Dict[str, Any]] = {}   # interval -> {"at": monotonic, "data": payload}


def _f(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _iso(ms: Any) -> Optional[str]:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _get(path: str, params: Dict[str, Any]) -> Any:
    """GET público; devolve `data` ou levanta (chamador decide o que é opcional)."""
    r = requests.get(BASE_URL + path, params=params, timeout=TIMEOUT_S)
    payload = r.json()
    if str(payload.get("code")) != "00000":
        raise RuntimeError(f"Bitget {path}: {payload.get('code')} {payload.get('msg')}")
    return payload.get("data")


def _optional(path: str, params: Dict[str, Any]) -> Any:
    try:
        return _get(path, params)
    except Exception:
        return None


def fetch_btc_market(interval: str = "1h") -> Dict[str, Any]:
    """Payload da aba (velas + ticker + funding + long/short). Cache de 10s por timeframe;
    velas são obrigatórias, o resto é opcional (None se a API falhar)."""
    interval = interval if interval in INTERVALS else "1h"
    with _lock:
        hit = _cache.get(interval)
        if hit and time.monotonic() - hit["at"] < MARKET_TTL_S:
            return hit["data"]
    mkt = {"symbol": SYMBOL, "productType": PRODUCT}
    rows = _get("/api/v2/mix/market/candles", {**mkt, "granularity": BAR_MAP[interval], "limit": CANDLES}) or []
    candles: List[Dict[str, Any]] = sorted(
        ({"t": int(k[0]) // 1000, "o": _f(k[1]), "h": _f(k[2]), "l": _f(k[3]), "c": _f(k[4]), "v": _f(k[5], 0.0)} for k in rows),
        key=lambda c: c["t"])
    tk = (_optional("/api/v2/mix/market/ticker", mkt) or [{}])[0]
    fr = (_optional("/api/v2/mix/market/current-fund-rate", mkt) or [{}])[0]
    hist = _optional("/api/v2/mix/market/history-fund-rate", {**mkt, "pageSize": 90}) or []
    oi = (_optional("/api/v2/mix/market/open-interest", mkt) or {}).get("openInterestList") or [{}]
    ls_acc = _optional("/api/v2/mix/market/account-long-short", {**mkt, "period": "1h"}) or []
    ls_pos = _optional("/api/v2/mix/market/position-long-short", {**mkt, "period": "1h"}) or []
    last, mark, index = _f(tk.get("lastPr")), _f(tk.get("markPrice")), _f(tk.get("indexPrice"))
    oi_btc = _f(oi[0].get("size")) if oi else _f(tk.get("holdingAmount"))
    data = {
        "symbol": SYMBOL, "interval": interval, "candles": candles,
        "ticker": {
            "last": last, "mark": mark, "index": index,
            "basis_pct": (mark - index) / index * 100.0 if mark and index else None,   # marca − índice: prêmio do perpétuo
            "change_24h_pct": (_f(tk.get("change24h")) or 0.0) * 100.0 if tk else None,
            "high_24h": _f(tk.get("high24h")), "low_24h": _f(tk.get("low24h")),
            "volume_usdt_24h": _f(tk.get("usdtVolume")), "volume_btc_24h": _f(tk.get("baseVolume")),
            "open_interest_btc": oi_btc,
            "open_interest_usdt": oi_btc * mark if oi_btc and mark else None,
        },
        "funding": {
            "rate_pct": (_f(fr.get("fundingRate")) if fr else _f(tk.get("fundingRate")) or 0.0) * 100.0 if (fr or tk) else None,
            "next_at": _iso(fr.get("nextUpdate")) if fr else None,
            "interval_hours": int(_f(fr.get("fundingRateInterval"), 8) or 8) if fr else 8,
            "history": [{"t": _iso(h.get("fundingTime")), "rate_pct": (_f(h.get("fundingRate")) or 0.0) * 100.0}
                        for h in sorted(hist, key=lambda h: int(h.get("fundingTime") or 0))],
        },
        # contas (quantas carteiras estão long) e posições (quanto notional está long), última hora
        "long_short": {
            "accounts_long_pct": (_f(ls_acc[-1].get("longAccountRatio")) or 0.0) * 100.0 if ls_acc else None,
            "accounts_ratio": _f(ls_acc[-1].get("longShortAccountRatio")) if ls_acc else None,
            "positions_long_pct": (_f(ls_pos[-1].get("longPositionRatio")) or 0.0) * 100.0 if ls_pos else None,
            "positions_ratio": _f(ls_pos[-1].get("longShortPositionRatio")) if ls_pos else None,
            "history": [{"t": _iso(a.get("ts")), "accounts_long_pct": (_f(a.get("longAccountRatio")) or 0.0) * 100.0} for a in ls_acc],
        } if (ls_acc or ls_pos) else None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    with _lock:
        _cache[interval] = {"at": time.monotonic(), "data": data}
    return data

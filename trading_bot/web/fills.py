"""
Relatórios › Histórico de fills: execuções da CONTA na exchange (endpoint privado de fills),
separadas dos trades do bot (TradeStore). Cada fill ganha `bot_trade_id` quando bate com um
trade do bot (mesmo par, mesmo lado da posição, horário a até MATCH_WINDOW_S da abertura — fill
de abertura — ou do fechamento — fill de fechamento). Filtros por par/lado/período, paginação
e CSV. Cache de FILLS_TTL_S por (conta, par, período) pra não martelar a API privada.
"""

from __future__ import annotations

import csv
import io
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

FILLS_TTL_S = 30.0
MATCH_WINDOW_S = 120.0
DEFAULT_DAYS = 7
MAX_DAYS = 90
MAX_FILLS = 1000
PER_PAGE_MAX = 200
CSV_COLUMNS = ("time", "symbol", "position_side", "action", "side", "price", "qty", "notional", "fee", "fee_coin",
               "profit", "role", "order_id", "trade_id", "bot_trade_id")

_lock = threading.Lock()
_cache: Dict[tuple, Dict[str, Any]] = {}


def _day(raw: Optional[str], default: datetime) -> datetime:
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d").replace(tzinfo=timezone.utc) if raw else default
    except ValueError:
        return default


def _parse_iso(raw: Any) -> Optional[float]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _raw_fills(exchange, account_id: int, symbol: Optional[str], start_ms: int, end_ms: int) -> List[Dict[str, Any]]:
    key = (account_id, symbol or "", start_ms, end_ms)
    with _lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit["at"] < FILLS_TTL_S:
            return hit["data"]
    data = exchange.get_fills(symbol=symbol, start_ms=start_ms, end_ms=end_ms, limit=MAX_FILLS)
    with _lock:
        _cache[key] = {"at": time.monotonic(), "data": data}
    return data


def match_bot_trades(fills: List[Dict[str, Any]], trades: List[Dict[str, Any]]) -> None:
    """Preenche `bot_trade_id` in place: fill de abertura casa com opened_at, de fechamento com
    exit_at, do trade de mesmo par e lado da posição dentro de MATCH_WINDOW_S."""
    index: Dict[tuple, List[tuple]] = {}
    for t in trades:
        sym, side = str(t.get("symbol") or ""), str(t.get("side") or "").upper()
        for action, field in (("open", "opened_at"), ("close", "exit_at")):
            ts = _parse_iso(t.get(field))
            if ts is not None:
                index.setdefault((sym, side, action), []).append((ts, t.get("id")))
    for f in fills:
        f["bot_trade_id"] = None
        ts = f["time"] / 1000.0
        best = None
        for cand_ts, tid in index.get((f["symbol"], f["position_side"], f["action"]), []):
            gap = abs(cand_ts - ts)
            if gap <= MATCH_WINDOW_S and (best is None or gap < best[0]):
                best = (gap, tid)
        if best:
            f["bot_trade_id"] = best[1]


def collect_fills(bot, account_id: int, args: Dict[str, Any]) -> Dict[str, Any]:
    """Payload de /api/fills: fills filtrados e paginados (mais recente primeiro) + totais.
    args: symbol, side (LONG/SHORT), from/to (YYYY-MM-DD, dia UTC), page, per_page."""
    exchange = getattr(bot, "exchange", None)
    if exchange is None or not hasattr(exchange, "get_fills"):
        raise LookupError("a exchange desta conta não expõe fills")
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    day_to = _day(args.get("to"), today)
    day_from = _day(args.get("from"), day_to - timedelta(days=DEFAULT_DAYS - 1))
    if day_from > day_to:
        day_from, day_to = day_to, day_from
    day_from = max(day_from, day_to - timedelta(days=MAX_DAYS - 1))
    start_ms, end_ms = int(day_from.timestamp() * 1000), int((day_to + timedelta(days=1)).timestamp() * 1000) - 1
    symbol = str(args.get("symbol") or "").upper() or None
    side = str(args.get("side") or "").upper() or None
    if side not in (None, "LONG", "SHORT"):
        raise ValueError("side precisa ser LONG ou SHORT")

    fills = [dict(f) for f in _raw_fills(exchange, account_id, symbol, start_ms, end_ms)]
    store = getattr(bot, "trade_store", None)
    # closed_trades traz id/opened_at/exit_at (recent_trades devolve o formato in-memory, sem id): só trades já fechados batem
    trades = store.closed_trades(None, limit=5000) if store is not None and hasattr(store, "closed_trades") else []
    match_bot_trades(fills, trades)
    symbols = sorted({f["symbol"] for f in fills})
    if side:
        fills = [f for f in fills if f["position_side"] == side]
    fills.sort(key=lambda f: f["time"], reverse=True)

    try:
        per_page = max(1, min(int(args.get("per_page") or 50), PER_PAGE_MAX))
        page = max(1, int(args.get("page") or 1))
    except (TypeError, ValueError):
        raise ValueError("page/per_page precisam ser inteiros")
    pages = max(1, -(-len(fills) // per_page))
    page = min(page, pages)
    start = (page - 1) * per_page
    return {
        "fills": fills[start:start + per_page],
        "total": len(fills), "page": page, "per_page": per_page, "pages": pages,
        "matched": sum(1 for f in fills if f["bot_trade_id"] is not None),
        "fee_total": round(sum(f["fee"] for f in fills), 6),
        "profit_total": round(sum(f["profit"] for f in fills), 6),
        "symbols": symbols,
        "from": day_from.strftime("%Y-%m-%d"), "to": day_to.strftime("%Y-%m-%d"),
        "all": fills,   # o CSV usa; a rota remove antes do JSON
    }


def fills_csv(fills: List[Dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for f in fills:
        row = dict(f, time=datetime.fromtimestamp(f["time"] / 1000, tz=timezone.utc).isoformat())
        w.writerow([row.get(c, "") if row.get(c) is not None else "" for c in CSV_COLUMNS])
    return buf.getvalue()

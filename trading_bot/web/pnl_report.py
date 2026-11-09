"""
Relatório "Ganhos e Perdas de Futuros" (aba estilo app da Binance) — /api/pnl_report.

Fonte de verdade dos trades: TradeStore (SQLite). Income (funding/comissão/
transferências) e referências BTC/BNB vêm da Binance e podem falhar: aí voltam
vazios com flag, a página nunca quebra. O "dia" é o dia de trading do bot
(DAILY_PNL_TZ_OFFSET_HOURS), o mesmo do card "Realizado hoje".
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..core.config import config
from ..core.equity import simulated_balance
from ..core.trade_store import trading_day
from .forward_test import parse_utc
from .fx import usd_brl

REFERENCE_SYMBOLS = ("BTCUSDT", "BNBUSDT")
INCOME_TYPES = ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE", "TRANSFER")


def _f(v: Any) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _day(iso: str) -> datetime:
    return datetime.strptime(iso, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _shift(iso: str, days: int) -> str:
    return (_day(iso) + timedelta(days=days)).strftime("%Y-%m-%d")


def _valid_day(iso: Any) -> Optional[str]:
    try:
        return _day(str(iso)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def _tz() -> timezone:
    return timezone(timedelta(hours=float(getattr(config, "DAILY_PNL_TZ_OFFSET_HOURS", 0.0) or 0.0)))


def _day_bounds_ms(day_from: str, day_to: str) -> Tuple[int, int]:
    """[00:00 de day_from, 24:00 de day_to) no fuso do dia de trading, em epoch ms."""
    tz = _tz()
    start = datetime.strptime(day_from, "%Y-%m-%d").replace(tzinfo=tz)
    end = datetime.strptime(day_to, "%Y-%m-%d").replace(tzinfo=tz) + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def build_report(bot, day_from: Optional[str], day_to: Optional[str], days: Any = None) -> Dict[str, Any]:
    """Período = [day_from, day_to] em dias de trading; sem day_from, os últimos `days` (default 7) até day_to."""
    today = trading_day()
    day_to = _valid_day(day_to) or today
    try:
        span = max(1, min(int(days or 7), 3660))
    except (TypeError, ValueError):
        span = 7
    day_from = _valid_day(day_from) or _shift(day_to, -(span - 1))
    if day_from > day_to:
        day_from, day_to = day_to, day_from

    store = getattr(bot, "trade_store", None)
    rows: List[Dict[str, Any]] = []
    if store is not None and hasattr(store, "closed_trades"):
        try:
            rows = [r for r in store.closed_trades(None, limit=100000) if parse_utc(r.get("exit_at"))]
        except Exception:
            rows = []
    for r in rows:
        r["day"] = trading_day(parse_utc(r["exit_at"]))

    # % do período = USD / equity no início do período, onde equity inicial =
    # capital inicial + Σ pnl_net dos trades fechados ANTES do período.
    capital = _f(getattr(bot, "initial_capital", None)) or simulated_balance(config)

    def window(from_day: str, to_day: str) -> Dict[str, Any]:
        usd = sum(_f(r["pnl_net"]) for r in rows if from_day <= r["day"] <= to_day)
        start_equity = capital + sum(_f(r["pnl_net"]) for r in rows if r["day"] < from_day)
        return {"usd": round(usd, 4), "pct": round(usd / start_equity * 100.0, 4) if start_equity > 0 else None,
                "start_equity": round(start_equity, 4)}

    period = [r for r in rows if day_from <= r["day"] <= day_to]
    totals = _aggregate(period)
    totals.update(window(day_from, day_to))
    daily = _daily(period)
    running = 0.0
    cumulative = []
    for d in daily:
        running += d["net"]
        cumulative.append({"day": d["day"], "value": round(running, 4)})

    exchange = getattr(bot, "exchange", None)
    income, income_ok = _income(exchange, day_from, day_to)
    income_totals = {t: round(sum(i["amount"] for i in income if i["type"] == t), 4) for t in INCOME_TYPES}

    return {
        "from": day_from, "to": day_to, "today": today,
        "tz_offset_hours": float(getattr(config, "DAILY_PNL_TZ_OFFSET_HOURS", 0.0) or 0.0),
        "today_pnl": window(today, today),
        "windows": {"d7": window(_shift(today, -6), today), "d30": window(_shift(today, -29), today),
                    "all": {"usd": round(sum(_f(r["pnl_net"]) for r in rows), 4)}},
        "totals": totals,
        "daily": daily,
        "cumulative": cumulative,
        "by_symbol": _by_symbol(period),
        "trades": [{k: r.get(k) for k in ("exit_at", "symbol", "side", "qty", "entry_price", "exit_price",
                                           "pnl_net", "fees", "funding", "close_reason", "strategy_type")}
                   for r in sorted(period, key=lambda r: str(r["exit_at"]), reverse=True)],
        "income": income, "income_totals": income_totals, "income_ok": income_ok,
        "reference": _reference(exchange, day_from, day_to),
        "assets": _assets(exchange),
        "fx": usd_brl(),
    }


def _aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    pnls = [_f(r["pnl_net"]) for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    return {
        "profit": round(sum(wins), 4), "loss": round(sum(losses), 4), "net": round(sum(pnls), 4),
        "trades": len(pnls), "wins": len(wins),
        "win_rate": round(len(wins) / len(pnls) * 100.0, 2) if pnls else 0.0,
        "fees": round(sum(_f(r.get("fees")) for r in rows), 4),
        "funding": round(sum(_f(r.get("funding")) for r in rows), 4),
        "best": round(max(pnls), 4) if pnls else None, "worst": round(min(pnls), 4) if pnls else None,
    }


def _group(rows: List[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    groups: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        g = groups.setdefault(r[key], {key: r[key], "net": 0.0, "trades": 0, "wins": 0, "fees": 0.0, "funding": 0.0})
        pnl = _f(r["pnl_net"])
        g["net"] += pnl
        g["trades"] += 1
        g["wins"] += 1 if pnl > 0 else 0
        g["fees"] += _f(r.get("fees"))
        g["funding"] += _f(r.get("funding"))
    out = list(groups.values())
    for g in out:
        g["win_rate"] = round(g["wins"] / g["trades"] * 100.0, 2) if g["trades"] else 0.0
        for k in ("net", "fees", "funding"):
            g[k] = round(g[k], 4)
    return out


def _daily(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(_group(rows, "day"), key=lambda d: d["day"])


def _by_symbol(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(_group(rows, "symbol"), key=lambda s: abs(s["net"]), reverse=True)


def _income(exchange, day_from: str, day_to: str) -> Tuple[List[Dict[str, Any]], bool]:
    """Income history da Binance no período (paginado por startTime). ([], False) em falha."""
    if exchange is None or not hasattr(exchange, "get_income_history"):
        return [], False
    start_ms, end_ms = _day_bounds_ms(day_from, day_to)
    out: List[Dict[str, Any]] = []
    cursor = start_ms
    try:
        for _ in range(10):  # shortcut: 10 páginas × 1000 itens; subir se o período passar disso
            batch = exchange.get_income_history(limit=1000, start_time=cursor, raise_errors=True) or []
            for i in batch:
                t = int(i.get("time", 0) or 0)
                if start_ms <= t < end_ms:
                    out.append({"time": datetime.fromtimestamp(t / 1000, tz=timezone.utc).isoformat(),
                                "type": str(i.get("incomeType", "")), "symbol": str(i.get("symbol", "") or ""),
                                "amount": _f(i.get("income"))})
            if len(batch) < 1000:
                break
            last = max(int(i.get("time", cursor) or cursor) for i in batch)
            if last <= cursor or last >= end_ms:
                break
            cursor = last + 1
    except Exception:
        return [], False
    out.sort(key=lambda i: i["time"], reverse=True)
    return out, True


_ref_cache: Dict[str, Any] = {"key": None, "at": 0.0, "data": {}}
_REF_TTL = 60.0


def _reference(exchange, day_from: str, day_to: str) -> Dict[str, List[Dict[str, Any]]]:
    """Variação % acumulada do fechamento diário (klines 1d da fonte de sinal, mainnet
    com SIGNALS_FROM_MAINNET) desde o 1º dia do período. Cache de 60s por período."""
    key = f"{day_from}:{day_to}"
    if _ref_cache["key"] == key and time.monotonic() - _ref_cache["at"] < _REF_TTL:
        return _ref_cache["data"]
    client = getattr(exchange, "market_client", None)
    out: Dict[str, List[Dict[str, Any]]] = {s: [] for s in REFERENCE_SYMBOLS}
    if client is None or not hasattr(client, "futures_klines"):
        return out
    # shortcut: candle diário em UTC (dia de trading do bot é BRT); diferença de 3h aceitável para referência
    start_ms = int(_day(day_from).timestamp() * 1000)
    end_ms = int((_day(day_to) + timedelta(days=1)).timestamp() * 1000) - 1
    for sym in REFERENCE_SYMBOLS:
        try:
            klines = client.futures_klines(symbol=sym, interval="1d", startTime=start_ms, endTime=end_ms, limit=1000) or []
        except Exception:
            continue
        base = _f(klines[0][4]) if klines else 0.0
        out[sym] = [{"day": datetime.fromtimestamp(int(k[0]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                     "pct": round((_f(k[4]) / base - 1.0) * 100.0, 4)} for k in klines if base > 0]
    _ref_cache.update(key=key, at=time.monotonic(), data=out)
    return out


def _assets(exchange) -> Dict[str, Any]:
    """Composição da carteira de futuros. shortcut: só USDT (o bot só opera USDT-M)."""
    if exchange is None or not hasattr(exchange, "get_account_info"):
        return {"ok": False, "items": [], "total": 0.0}
    try:
        info = exchange.get_account_info() or {}
        wallet = _f(info.get("wallet_balance"))
        return {"ok": True, "items": [{"asset": "USDT", "balance": round(wallet, 4)}], "total": round(wallet, 4)}
    except Exception:
        return {"ok": False, "items": [], "total": 0.0}

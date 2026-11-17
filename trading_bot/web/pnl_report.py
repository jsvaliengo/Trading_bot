"""
Relatório "Análise do P&L de futuros" (réplica da tela da Bitget) — /api/pnl_report.

Fonte de verdade: TradeStore (trades fechados + snapshots de portfolio_history) e
income da exchange (funding/comissão). Saldo, income e referência BTC vêm da
exchange com cache de 30-60s e podem falhar: aí voltam vazios com flag, a página
nunca quebra. O "dia" é o dia de trading do bot (DAILY_PNL_TZ_OFFSET_HOURS), o
mesmo do card "Realizado hoje" — a Bitget usa UTC+0.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..core.config import config
from ..core.equity import effective_equity, simulated_balance
from ..core.trade_store import pnl_since_ms, trading_day
from .forward_test import parse_utc
from .fx import usd_brl

REFERENCE_SYMBOLS = ("BTCUSDT",)
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
    """Período = [day_from, day_to] em dias de trading; sem day_from, os últimos `days` (default 30) até day_to."""
    today = trading_day()
    day_to = _valid_day(day_to) or today
    try:
        span = max(1, min(int(days or 30), 3660))
    except (TypeError, ValueError):
        span = 30
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
    account = dict(_cached(("account",), exchange, 30.0, lambda: _account(exchange)))
    if account["ok"]:  # mesma fórmula do card SALDO: testnet simulado soma o realizado do DB ao cap
        account["equity"] = round(effective_equity(config, wallet=account.pop("wallet"), cumulative_realized=sum(_f(r["pnl_net"]) for r in rows),
                                                   unrealized=account["unrealized"]), 4)
    income, income_ok = _cached(("income", day_from, day_to), exchange, 30.0, lambda: _income(exchange, day_from, day_to))
    income_totals = {t: round(sum(i["amount"] for i in income if i["type"] == t), 4) for t in INCOME_TYPES}

    return {
        "from": day_from, "to": day_to, "today": today,
        "tz_offset_hours": float(getattr(config, "DAILY_PNL_TZ_OFFSET_HOURS", 0.0) or 0.0),
        "today_pnl": window(today, today),
        "windows": {"d7": window(_shift(today, -6), today), "d30": window(_shift(today, -29), today)},
        "totals": totals,
        "daily": daily,
        "cumulative": cumulative,
        "by_symbol": _by_symbol(period),
        "trades": [{k: r.get(k) for k in ("exit_at", "symbol", "side", "qty", "entry_price", "exit_price",
                                           "pnl_net", "fees", "funding", "close_reason", "strategy_type")}
                   for r in sorted(period, key=lambda r: str(r["exit_at"]), reverse=True)],
        "funding_daily": _funding_daily(income),
        "series": _daily_snapshots(store, rows, day_from, day_to),
        "income": income, "income_totals": income_totals, "income_ok": income_ok,
        "reference": _cached(("ref", day_from, day_to), exchange, 60.0, lambda: _reference(exchange, day_from, day_to)),
        "account": account,
        "fx": usd_brl(),
    }


def _aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    pnls = [_f(r["pnl_net"]) for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    return {
        "profit": round(sum(wins), 4), "loss": round(sum(losses), 4), "net": round(sum(pnls), 4),
        "trades": len(pnls), "wins": len(wins), "losses": len(pnls) - len(wins),
        "long": sum(1 for r in rows if r.get("side") == "LONG"), "short": sum(1 for r in rows if r.get("side") == "SHORT"),
        "win_rate": round(len(wins) / len(pnls) * 100.0, 2) if pnls else 0.0,
        "profit_factor": profit_factor(pnls),
        "fees": round(sum(_f(r.get("fees")) for r in rows), 4),
        "funding": round(sum(_f(r.get("funding")) for r in rows), 4),
        "best": round(max(pnls), 4) if pnls else None, "worst": round(min(pnls), 4) if pnls else None,
    }


def profit_factor(pnls: List[float]) -> Optional[float]:
    """Σ lucros / |Σ perdas| ("Índice de P&L" da Bitget); None sem perda (divisão por zero)."""
    gain, loss = sum(p for p in pnls if p > 0), -sum(p for p in pnls if p < 0)
    return round(gain / loss, 4) if loss > 0 else None


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


def _funding_daily(income: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Funding do income somado por dia de trading (barras "Taxas de financiamento diárias")."""
    by_day: Dict[str, float] = {}
    for i in income:
        if i["type"] == "FUNDING_FEE":
            day = trading_day(datetime.fromisoformat(i["time"]))
            by_day[day] = by_day.get(day, 0.0) + i["amount"]
    return [{"day": d, "amount": round(v, 4)} for d, v in sorted(by_day.items())]


def _daily_snapshots(store, rows: List[Dict[str, Any]], day_from: str, day_to: str) -> List[Dict[str, Any]]:
    """Último snapshot de portfolio_history de cada dia de trading do período:
    {day, equity, unrealized}. Equity pela MESMA fórmula do card SALDO (effective_equity)."""
    if store is None or not hasattr(store, "recent_equity"):
        return []
    try:
        # shortcut: assume ≤144 snapshots/dia (1 a cada 10 min) + 2 dias de folga pro fuso; subir se o intervalo do snapshot cair
        snaps = store.recent_equity((len(_days_between(day_from, day_to)) + 2) * 144) or []
    except Exception:
        return []
    closed = sorted((parse_utc(r["exit_at"]), _f(r["pnl_net"])) for r in rows)
    realized, i = 0.0, 0   # snapshots em ordem cronológica: o realizado acumulado avança com um ponteiro
    out: Dict[str, Dict[str, Any]] = {}
    for snap in snaps:
        ts = parse_utc(snap.get("timestamp"))
        if ts is None:
            continue
        while i < len(closed) and closed[i][0] <= ts:
            realized += closed[i][1]
            i += 1
        day = trading_day(ts)
        if not day_from <= day <= day_to:
            continue
        unreal = _f(snap.get("pnl_unrealized"))
        equity = effective_equity(config, wallet=_f(snap.get("balance")), cumulative_realized=realized, unrealized=unreal)
        out[day] = {"day": day, "equity": round(equity, 4), "unrealized": round(unreal, 4)}  # o último do dia vence
    return [out[d] for d in sorted(out)]


def _days_between(day_from: str, day_to: str) -> List[str]:
    return [_shift(day_from, i) for i in range((_day(day_to) - _day(day_from)).days + 1)]


_memo: Dict[Any, Tuple[float, Any, Any]] = {}


def _cached(key: Tuple, exchange, ttl: float, fn):
    """Cache leve das chamadas à exchange; guarda a referência da exchange (não vaza entre instâncias)."""
    hit = _memo.get(key)
    if hit and hit[1] is exchange and time.monotonic() - hit[0] < ttl:
        return hit[2]
    data = fn()
    _memo[key] = (time.monotonic(), exchange, data)
    return data


def _income(exchange, day_from: str, day_to: str) -> Tuple[List[Dict[str, Any]], bool]:
    """Income history da exchange no período (paginado por startTime). ([], False) em falha."""
    if exchange is None or not hasattr(exchange, "get_income_history"):
        return [], False
    start_ms, end_ms = _day_bounds_ms(day_from, day_to)
    start_ms = max(start_ms, pnl_since_ms() or 0)  # reset de banca: lançamentos anteriores ficam de fora
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


def _reference(exchange, day_from: str, day_to: str) -> Dict[str, List[Dict[str, Any]]]:
    """Variação % acumulada do fechamento diário do BTC desde o 1º dia do período
    ("Histórico de variação do BTC"). {sym: []} sem exchange ou em falha."""
    out: Dict[str, List[Dict[str, Any]]] = {s: [] for s in REFERENCE_SYMBOLS}
    if exchange is None or not hasattr(exchange, "get_klines_raw"):
        return out
    # shortcut: candle diário em UTC (dia de trading do bot é BRT); diferença de 3h aceitável para referência
    n = len(_days_between(day_from, day_to))
    for sym in REFERENCE_SYMBOLS:
        try:
            klines = exchange.get_klines_raw(sym, "1d", limit=min(1000, n + 1)) or []
        except Exception:
            continue
        klines = [k for k in klines if day_from <= datetime.fromtimestamp(int(k[0]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d") <= day_to]
        base = _f(klines[0][4]) if klines else 0.0
        out[sym] = [{"day": datetime.fromtimestamp(int(k[0]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                     "pct": round((_f(k[4]) / base - 1.0) * 100.0, 4)} for k in klines if base > 0]
    return out


def _account(exchange) -> Dict[str, Any]:
    """Saldo (equity USDT) e não realizado da conta de futuros, como o cabeçalho da Bitget."""
    if exchange is None or not hasattr(exchange, "get_account_info"):
        return {"ok": False, "equity": None, "unrealized": 0.0}
    try:
        info = exchange.get_account_info() or {}
        return {"ok": True, "wallet": _f(info.get("wallet_balance")), "unrealized": round(_f(info.get("unrealized_pnl")), 4)}
    except Exception:
        return {"ok": False, "equity": None, "unrealized": 0.0}

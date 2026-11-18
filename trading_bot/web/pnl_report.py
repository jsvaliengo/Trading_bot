"""
Relatório "Análise do P&L de futuros" (réplica da tela da Bitget) — /api/pnl_report.

Fonte de verdade: TradeStore (trades fechados + snapshots de portfolio_history) e
income da exchange (funding/comissão). Saldo, income e referência BTC vêm da
exchange com cache de 30-60s e podem falhar: aí voltam vazios com flag, a página
nunca quebra. O "dia" aqui é o dia UTC+0 (como na Bitget), NÃO o dia de trading
do bot do card "Realizado hoje". A aba "Análise da conta" segue a definição da
Bitget: P&L do dia = equity no fim do dia − equity no fim do dia anterior −
transferências (depósito/saque não é lucro); inclui o não realizado.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..core.config import config
from ..core.equity import effective_equity, simulated_balance
from ..core.trade_store import pnl_since_ms
from .forward_test import parse_utc
from .fx import usd_brl

REFERENCE_SYMBOLS = ("BTCUSDT",)
INCOME_TYPES = ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE", "TRANSFER")
PNL_INCOME = INCOME_TYPES[:3]  # o que a Bitget soma no "P&L" de um dia sem snapshot (transferência não é lucro)


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


def _utc_day(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _day_bounds_ms(day_from: str, day_to: str) -> Tuple[int, int]:
    """[00:00 de day_from, 24:00 de day_to) em UTC, em epoch ms."""
    return _ms(_day(day_from)), _ms(_day(day_to) + timedelta(days=1))


def build_report(bot, day_from: Optional[str], day_to: Optional[str], days: Any = None) -> Dict[str, Any]:
    """Período = [day_from, day_to] em dias UTC; sem day_from, os últimos `days` (default 30) até day_to."""
    now = datetime.now(timezone.utc)
    today = _utc_day(now)
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
        r["day"] = _utc_day(parse_utc(r["exit_at"]))
    # hoje/7d/30d ancoram em hoje, não no período pedido: equity e income cobrem a união das duas janelas
    wide_from, wide_to = min(day_from, _shift(today, -29)), max(day_to, today)

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

    exchange = getattr(bot, "exchange", None)
    account = dict(_cached(("account",), exchange, 30.0, lambda: _account(exchange)))
    if account["ok"]:  # mesma fórmula do card SALDO: testnet simulado soma o realizado do DB ao cap
        account["equity"] = round(effective_equity(config, wallet=account.pop("wallet"), cumulative_realized=sum(_f(r["pnl_net"]) for r in rows),
                                                   unrealized=account["unrealized"]), 4)
    wide_income, income_ok = _cached(("income", wide_from, wide_to), exchange, 30.0, lambda: _income(exchange, wide_from, wide_to))
    income = [i for i in wide_income if day_from <= i["time"][:10] <= day_to]
    income_totals = {t: round(sum(i["amount"] for i in income if i["type"] == t), 4) for t in INCOME_TYPES}

    points = _daily_snapshots(store, rows, wide_from, wide_to)
    if account["ok"]:  # hoje: equity ao vivo vale mais que o último snapshot (até 10 min atrás)
        points = points[:1] + [p for p in points[1:] if p["day"] != today] + [{"day": today, "equity": account["equity"], "unrealized": account["unrealized"], "at": _ms(now)}]
    calendar = _account_daily(points, wide_income, wide_from, wide_to)
    account_window = _account_window(calendar, points)
    cumulative, running = [], 0.0
    for d in calendar:
        if day_from <= d["day"] <= day_to:
            running += d["net"]
            cumulative.append({"day": d["day"], "value": round(running, 4)})

    return {
        "from": day_from, "to": day_to, "today": today,
        "today_pnl": account_window(today, today),
        "windows": {"d7": account_window(_shift(today, -6), today), "d30": account_window(_shift(today, -29), today)},
        "period": account_window(day_from, day_to),
        "totals": totals,
        "daily": daily,
        "calendar": [d for d in calendar if day_from <= d["day"] <= day_to],
        "cumulative": cumulative,
        "by_symbol": _by_symbol(period),
        "trades": [{k: r.get(k) for k in ("exit_at", "symbol", "side", "qty", "entry_price", "exit_price",
                                           "pnl_net", "fees", "funding", "close_reason", "strategy_type")}
                   for r in sorted(period, key=lambda r: str(r["exit_at"]), reverse=True)],
        "funding_daily": _funding_daily(income),
        "series": list({p["day"]: p for p in points if day_from <= p["day"] <= day_to}.values()),
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


def _income_by_day(income: List[Dict[str, Any]], types: Tuple[str, ...]) -> Dict[str, float]:
    by_day: Dict[str, float] = {}
    for i in income:
        if i["type"] in types:
            by_day[i["time"][:10]] = by_day.get(i["time"][:10], 0.0) + i["amount"]
    return by_day


def _funding_daily(income: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Funding do income somado por dia UTC (barras "Taxas de financiamento diárias")."""
    return [{"day": d, "amount": round(v, 4)} for d, v in sorted(_income_by_day(income, ("FUNDING_FEE",)).items())]


def _account_daily(points: List[Dict[str, Any]], income: List[Dict[str, Any]], day_from: str, day_to: str) -> List[Dict[str, Any]]:
    """P&L da conta por dia UTC, como a "Análise da conta" da Bitget: equity no fim do dia −
    equity no fim do dia anterior − transferências entre os dois pontos. Dia sem snapshot (bot
    parado) cai no income do dia (realizado + taxas + funding); o que aconteceu antes do 1º
    snapshot entra pelo income também. `source` diz qual regra o dia usou."""
    pnl_income = _income_by_day(income, PNL_INCOME)
    at = {id(i): _ms(datetime.fromisoformat(i["time"])) for i in income}
    transfers = [(at[id(i)], i["amount"]) for i in income if i["type"] == "TRANSFER"]
    by_day = {p["day"]: p for p in points}  # o último do dia vence; points[0] é o 1º snapshot (baseline)
    prev = points[0] if points else None
    # income do dia do 1º snapshot ocorrido antes dele: fora do Δ, entra por fora
    carry = sum(i["amount"] for i in income if i["type"] in PNL_INCOME and prev and i["time"][:10] == prev["day"] and at[id(i)] < prev["at"])
    since = pnl_since_ms()
    out = []
    for day in _days_between(day_from, day_to):
        if since and _ms(_day(day) + timedelta(days=1)) <= since:
            continue  # reset de banca: nada antes do piso
        p = by_day.get(day)
        if p is None or prev is None:
            net = pnl_income.get(day, 0.0)
            if prev is not None and day > prev["day"]:
                carry -= net  # dia sem snapshot depois do 1º: o próximo Δ inclui esse income, não conta 2x
            out.append({"day": day, "net": round(net, 4), "source": "income" if day in pnl_income else "none"})
            continue
        moved = sum(a for t, a in transfers if prev["at"] < t <= p["at"])
        out.append({"day": day, "net": round(p["equity"] - prev["equity"] - moved + carry, 4), "source": "equity"})
        prev, carry = p, 0.0
    return out


def _account_window(calendar: List[Dict[str, Any]], points: List[Dict[str, Any]]):
    """Fábrica de janelas hoje/7d/30d/período: USD = Σ dias; % sobre a equity no início da janela
    (último snapshot antes dela; sem snapshot anterior, o 1º que existir), None sem equity."""
    def window(from_day: str, to_day: str) -> Dict[str, Any]:
        usd = sum(d["net"] for d in calendar if from_day <= d["day"] <= to_day)
        before = [p["equity"] for p in points if p["day"] < from_day]
        start = before[-1] if before else (points[0]["equity"] if points else 0.0)
        return {"usd": round(usd, 4), "pct": round(usd / start * 100.0, 4) if start > 0 else None,
                "start_equity": round(start, 4) if start > 0 else None}
    return window


def _daily_snapshots(store, rows: List[Dict[str, Any]], day_from: str, day_to: str) -> List[Dict[str, Any]]:
    """Último snapshot de portfolio_history de cada dia UTC do período, a partir do piso PNL_SINCE_UTC:
    {day, equity, unrealized, at (epoch ms)}, precedido do 1º snapshot válido (baseline do Δ da conta).
    Equity pela MESMA fórmula do card SALDO (effective_equity)."""
    if store is None or not hasattr(store, "recent_equity"):
        return []
    try:
        # shortcut: assume ≤144 snapshots/dia (1 a cada 10 min) + 2 dias de folga pro fuso; subir se o intervalo do snapshot cair
        snaps = store.recent_equity((len(_days_between(day_from, day_to)) + 2) * 144) or []
    except Exception:
        return []
    closed = sorted((parse_utc(r["exit_at"]), _f(r["pnl_net"])) for r in rows)
    realized, i = 0.0, 0   # snapshots em ordem cronológica: o realizado acumulado avança com um ponteiro
    since = pnl_since_ms() or 0
    first: Optional[Dict[str, Any]] = None
    out: Dict[str, Dict[str, Any]] = {}
    for snap in snaps:
        ts = parse_utc(snap.get("timestamp"))
        if ts is None:
            continue
        while i < len(closed) and closed[i][0] <= ts:
            realized += closed[i][1]
            i += 1
        day = _utc_day(ts)
        if not day_from <= day <= day_to or _ms(ts) < since:
            continue
        unreal = _f(snap.get("pnl_unrealized"))
        equity = effective_equity(config, wallet=_f(snap.get("balance")), cumulative_realized=realized, unrealized=unreal)
        out[day] = {"day": day, "equity": round(equity, 4), "unrealized": round(unreal, 4), "at": _ms(ts)}  # o último do dia vence
        first = first or out[day]
    return ([first] if first and first is not out[first["day"]] else []) + [out[d] for d in sorted(out)]


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

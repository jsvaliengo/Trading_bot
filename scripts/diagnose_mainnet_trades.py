#!/usr/bin/env python3
"""
Diagnóstico do histórico real da mainnet (últimos N dias).

Roda no servidor onde a API key da mainnet está autorizada.
Puxa income history + userTrades da Binance Futures, correlaciona com
o trade_history do bot (strategy_name, side, etc.), agrega métricas e
gera relatório.

Uso (no OCI):
    cd /home/ubuntu/trading_bot
    source .venv/bin/activate
    TRADING_BOT_ENVIRONMENT=mainnet python scripts/diagnose_mainnet_trades.py --days 90

Saídas em runtime/diagnose/:
    - raw_income.json          income history bruto
    - raw_trades.json          userTrades bruto (por símbolo)
    - trades_closed.json       trades fechados reconciliados (entry+exit+pnl+strategy)
    - summary.json             métricas agregadas
    - report.md                relatório human-readable
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable

from binance.client import Client  # type: ignore
from binance.exceptions import BinanceAPIException  # type: ignore

try:
    from dotenv import load_dotenv  # type: ignore
except ImportError:
    load_dotenv = None  # type: ignore


REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "runtime" / "diagnose"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("diagnose")


# ---------- API helpers ----------

def make_client() -> Client:
    if load_dotenv is not None:
        for candidate in (REPO_ROOT / ".env", REPO_ROOT / ".env.local"):
            if candidate.exists():
                load_dotenv(candidate, override=False)

    key = os.getenv("BINANCE_MAINNET_API_KEY") or os.getenv("BINANCE_API_KEY")
    secret = os.getenv("BINANCE_MAINNET_API_SECRET") or os.getenv("BINANCE_API_SECRET")
    if not key or not secret:
        log.error("Credenciais mainnet ausentes. Defina BINANCE_MAINNET_API_KEY/SECRET.")
        sys.exit(2)
    return Client(key, secret, testnet=False)


def paginate_income(client: Client, start_ms: int, end_ms: int) -> list[dict]:
    """Income history é paginado por startTime — limit max 1000 por request."""
    out: list[dict] = []
    cursor = start_ms
    page = 0
    while cursor < end_ms:
        page += 1
        try:
            chunk = client.futures_income_history(
                startTime=cursor,
                endTime=end_ms,
                limit=1000,
            )
        except BinanceAPIException as exc:
            log.error("Falha em income (page %d): %s", page, exc)
            raise
        if not chunk:
            break
        out.extend(chunk)
        last_ts = int(chunk[-1].get("time", cursor))
        log.info("  income page %d: +%d items (last ts %s)", page, len(chunk), _fmt_ts(last_ts))
        if last_ts <= cursor or len(chunk) < 1000:
            break
        cursor = last_ts + 1
        time.sleep(0.25)  # respeita rate limit
    return out


def paginate_user_trades(client: Client, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """userTrades é paginado por fromId; limit max 1000."""
    out: list[dict] = []
    cursor_id: int | None = None
    page = 0
    while True:
        page += 1
        params: dict[str, Any] = {"symbol": symbol, "limit": 1000}
        if cursor_id is not None:
            params["fromId"] = cursor_id
        else:
            params["startTime"] = start_ms
            params["endTime"] = end_ms
        try:
            chunk = client.futures_account_trades(**params)
        except BinanceAPIException as exc:
            log.error("Falha em userTrades %s page %d: %s", symbol, page, exc)
            raise
        if not chunk:
            break
        out.extend(chunk)
        # filtra janela (fromId pode trazer trade fora da janela)
        last = chunk[-1]
        last_ts = int(last.get("time", 0))
        last_id = int(last.get("id", 0))
        if len(chunk) < 1000 or last_ts > end_ms:
            break
        cursor_id = last_id + 1
        time.sleep(0.15)
    # corta o que estiver fora da janela
    out = [t for t in out if start_ms <= int(t.get("time", 0)) <= end_ms]
    return out


# ---------- Reconciliação trades fechados ----------

def reconcile_closes(income: list[dict], trades_by_symbol: dict[str, list[dict]]) -> list[dict]:
    """
    Cada REALIZED_PNL é um fechamento (parcial ou total). Anexa:
      - commission e funding agregados do mesmo trade quando possível
      - exit/entry preços via userTrades (matching por tradeId/orderId)
    Retorna lista de fechamentos com pnl líquido (pnl - comissão de saída - funding alocado).
    """
    closes: list[dict] = []
    # Index trades por id pra lookup rápido
    trade_by_id: dict[str, dict] = {}
    for sym_trades in trades_by_symbol.values():
        for t in sym_trades:
            trade_by_id[str(t.get("id"))] = t

    # Funding por símbolo/janela — distribui proporcional ao tempo da posição
    funding_by_symbol_window: list[dict] = [
        i for i in income if i.get("incomeType") == "FUNDING_FEE"
    ]

    for ev in income:
        if ev.get("incomeType") != "REALIZED_PNL":
            continue
        symbol = ev.get("symbol", "")
        ts = int(ev.get("time", 0))
        pnl = float(ev.get("income", 0.0))
        trade_id = ev.get("tradeId") or ev.get("info") or ""
        matched = trade_by_id.get(str(trade_id))
        side = ""
        qty = 0.0
        exit_price = 0.0
        commission = 0.0
        if matched:
            side = "SHORT" if matched.get("side") == "SELL" else "LONG"
            # No fechamento o lado da ordem é oposto ao da posição:
            # SELL fecha LONG, BUY fecha SHORT.
            position_side = "LONG" if matched.get("side") == "SELL" else "SHORT"
            qty = float(matched.get("qty", 0.0))
            exit_price = float(matched.get("price", 0.0))
            commission = float(matched.get("commission", 0.0))
            side = position_side
        closes.append({
            "ts_ms": ts,
            "datetime_utc": _fmt_ts(ts),
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "exit_price": exit_price,
            "realized_pnl": pnl,
            "commission_exit": commission,
            "binance_trade_id": str(trade_id),
        })
    return closes


def attach_bot_metadata(closes: list[dict], bot_state_path: Path) -> tuple[list[dict], int]:
    """Anexa strategy_name/entry_price do bot state via match (symbol, side, ts ±10min)."""
    if not bot_state_path.exists():
        log.warning("bot_state mainnet não encontrado em %s — análise sem strategy_name", bot_state_path)
        return closes, 0
    try:
        state = json.loads(bot_state_path.read_text())
    except Exception as exc:
        log.warning("Falha ao ler %s: %s", bot_state_path, exc)
        return closes, 0
    bot_trades = state.get("trade_history", [])
    matched = 0
    for c in closes:
        c_ts = c["ts_ms"]
        best = None
        best_delta = 10 * 60 * 1000  # 10 min
        for bt in bot_trades:
            if bt.get("symbol") != c["symbol"]:
                continue
            if c["side"] and bt.get("side") and bt["side"] != c["side"]:
                continue
            try:
                bt_ts = int(datetime.fromisoformat(bt["timestamp"]).timestamp() * 1000)
            except Exception:
                continue
            # bot loga ENTRADA; fechamento da Binance é depois — janela ampla
            delta = c_ts - bt_ts
            if 0 <= delta <= 7 * 24 * 3600 * 1000 and delta < best_delta:
                best = bt
                best_delta = delta
        if best:
            c["strategy_name"] = best.get("strategy_name", "")
            c["strategy_type"] = best.get("strategy_type", "")
            c["entry_price"] = float(best.get("entry_price", 0.0) or 0.0)
            c["signal"] = best.get("signal", "")
            c["hold_seconds"] = int(best_delta / 1000)
            c["double_first"] = bool(best.get("double_first", False))
            matched += 1
    return closes, matched


# ---------- Métricas ----------

def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((pct / 100.0) * (len(s) - 1)))))
    return s[k]


def aggregate(closes: list[dict], income: list[dict]) -> dict:
    pnls = [c["realized_pnl"] for c in closes]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0

    funding_total = sum(float(i["income"]) for i in income if i.get("incomeType") == "FUNDING_FEE")
    commission_total = sum(float(i["income"]) for i in income if i.get("incomeType") == "COMMISSION")
    realized_total = sum(float(i["income"]) for i in income if i.get("incomeType") == "REALIZED_PNL")
    net_total = realized_total + funding_total + commission_total  # commission/funding já vem negativo

    # Equity curve + max drawdown por trade
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    max_dd_pct_from_peak = 0.0
    consec_loss = 0
    worst_streak = 0
    for c in sorted(closes, key=lambda x: x["ts_ms"]):
        equity += c["realized_pnl"]
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
        if peak > 0:
            dd_pct = (dd / peak) * 100.0
            if dd_pct > max_dd_pct_from_peak:
                max_dd_pct_from_peak = dd_pct
        if c["realized_pnl"] < 0:
            consec_loss += 1
            worst_streak = max(worst_streak, consec_loss)
        else:
            consec_loss = 0

    def bucket(key_fn) -> dict[str, dict]:
        b: dict[str, dict] = defaultdict(lambda: {"count": 0, "pnl": 0.0, "wins": 0, "losses": 0})
        for c in closes:
            k = key_fn(c)
            if k is None:
                continue
            b[str(k)]["count"] += 1
            b[str(k)]["pnl"] += c["realized_pnl"]
            if c["realized_pnl"] > 0:
                b[str(k)]["wins"] += 1
            elif c["realized_pnl"] < 0:
                b[str(k)]["losses"] += 1
        # adiciona win_rate
        for k, v in b.items():
            total = v["wins"] + v["losses"]
            v["win_rate_pct"] = round((v["wins"] / total * 100.0), 2) if total else 0.0
            v["pnl"] = round(v["pnl"], 4)
        return dict(b)

    by_symbol = bucket(lambda c: c["symbol"])
    by_side = bucket(lambda c: c["side"] or "UNKNOWN")
    by_strategy = bucket(lambda c: c.get("strategy_name") or "UNKNOWN")
    by_hour = bucket(lambda c: datetime.fromtimestamp(c["ts_ms"] / 1000, tz=timezone.utc).hour)
    by_dow = bucket(lambda c: datetime.fromtimestamp(c["ts_ms"] / 1000, tz=timezone.utc).strftime("%a"))

    hold_seconds = [c.get("hold_seconds") for c in closes if c.get("hold_seconds")]
    avg_hold_min = round(mean(hold_seconds) / 60, 1) if hold_seconds else None
    median_hold_min = round(median(hold_seconds) / 60, 1) if hold_seconds else None

    return {
        "window": {
            "trades_closed_count": len(closes),
            "first_close_utc": _fmt_ts(min((c["ts_ms"] for c in closes), default=0)),
            "last_close_utc": _fmt_ts(max((c["ts_ms"] for c in closes), default=0)),
        },
        "headline": {
            "realized_pnl_usdt": round(realized_total, 4),
            "funding_fee_usdt": round(funding_total, 4),
            "commission_usdt": round(commission_total, 4),
            "net_usdt": round(net_total, 4),
            "win_rate_pct": round((len(wins) / len(pnls) * 100.0), 2) if pnls else 0.0,
            "wins": len(wins),
            "losses": len(losses),
            "breakeven": sum(1 for p in pnls if p == 0),
            "profit_factor": round(profit_factor, 3) if profit_factor != float("inf") else None,
            "expectancy_usdt": round((sum(pnls) / len(pnls)), 4) if pnls else 0.0,
            "avg_win_usdt": round(mean(wins), 4) if wins else 0.0,
            "avg_loss_usdt": round(mean(losses), 4) if losses else 0.0,
            "biggest_win_usdt": round(max(pnls, default=0.0), 4),
            "biggest_loss_usdt": round(min(pnls, default=0.0), 4),
            "pnl_p10": round(percentile(pnls, 10), 4),
            "pnl_p90": round(percentile(pnls, 90), 4),
            "max_drawdown_usdt": round(max_dd, 4),
            "max_drawdown_from_peak_pct": round(max_dd_pct_from_peak, 2),
            "worst_loss_streak": worst_streak,
            "avg_hold_minutes": avg_hold_min,
            "median_hold_minutes": median_hold_min,
            "funding_burden_pct_of_gross": (
                round((abs(funding_total) / gross_win * 100.0), 2) if gross_win > 0 else None
            ),
        },
        "by_symbol": by_symbol,
        "by_side": by_side,
        "by_strategy": by_strategy,
        "by_hour_utc": by_hour,
        "by_dow_utc": by_dow,
    }


# ---------- Report ----------

def write_report(summary: dict, out: Path) -> None:
    h = summary["headline"]
    w = summary["window"]

    def table(title: str, data: dict[str, dict], top: int = 15) -> str:
        rows = sorted(data.items(), key=lambda kv: kv[1]["pnl"])
        lines = [f"### {title}", "", "| chave | count | wins | losses | win_rate% | pnl_usdt |", "|---|---:|---:|---:|---:|---:|"]
        for k, v in rows[:top]:
            lines.append(f"| {k} | {v['count']} | {v['wins']} | {v['losses']} | {v['win_rate_pct']} | {v['pnl']:.4f} |")
        if len(rows) > top:
            lines.append(f"| _… +{len(rows) - top} mais_ |  |  |  |  |  |")
        return "\n".join(lines) + "\n"

    md = [
        "# Diagnóstico Mainnet — Trading Bot",
        "",
        f"- Janela: **{w['first_close_utc']} → {w['last_close_utc']}**",
        f"- Trades fechados: **{w['trades_closed_count']}**",
        "",
        "## Headline",
        "",
        f"- Realized PnL: **{h['realized_pnl_usdt']:+.4f} USDT**",
        f"- Funding: **{h['funding_fee_usdt']:+.4f} USDT**",
        f"- Commission: **{h['commission_usdt']:+.4f} USDT**",
        f"- **Net: {h['net_usdt']:+.4f} USDT**",
        "",
        f"- Win rate: **{h['win_rate_pct']}%** ({h['wins']}W / {h['losses']}L)",
        f"- Profit factor: **{h['profit_factor']}**",
        f"- Expectancy/trade: **{h['expectancy_usdt']:+.4f}**",
        f"- Avg win: {h['avg_win_usdt']:+.4f}  |  Avg loss: {h['avg_loss_usdt']:+.4f}",
        f"- Maior win: {h['biggest_win_usdt']:+.4f}  |  Maior loss: {h['biggest_loss_usdt']:+.4f}",
        f"- P10 / P90: {h['pnl_p10']:+.4f} / {h['pnl_p90']:+.4f}",
        f"- Max drawdown: **{h['max_drawdown_usdt']:.4f} USDT** ({h['max_drawdown_from_peak_pct']}% do pico)",
        f"- Pior streak de loss: **{h['worst_loss_streak']}**",
        f"- Hold médio: {h['avg_hold_minutes']} min  |  mediana: {h['median_hold_minutes']} min",
        f"- Funding burden: {h['funding_burden_pct_of_gross']}% do gross win",
        "",
        table("PnL por símbolo (piores → melhores)", summary["by_symbol"], top=30),
        table("PnL por lado", summary["by_side"]),
        table("PnL por estratégia", summary["by_strategy"]),
        table("PnL por hora UTC", summary["by_hour_utc"], top=24),
        table("PnL por dia da semana UTC", summary["by_dow_utc"], top=7),
    ]
    out.write_text("\n".join(md))


# ---------- utils ----------

def _fmt_ts(ts_ms: int) -> str:
    if not ts_ms:
        return "-"
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, default=str))
    log.info("→ %s (%d KB)", path.relative_to(REPO_ROOT), path.stat().st_size // 1024)


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90, help="Janela em dias (default: 90)")
    ap.add_argument("--bot-state", default="runtime/bot_state.prod.mainnet.json",
                    help="Path pro state file mainnet (pra strategy_name)")
    ap.add_argument("--skip-user-trades", action="store_true",
                    help="Pula fetch de userTrades por símbolo (só income)")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    client = make_client()
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.days * 24 * 3600 * 1000

    log.info("Janela: %s → %s (%d dias)", _fmt_ts(start_ms), _fmt_ts(end_ms), args.days)

    log.info("Fetching income history…")
    income = paginate_income(client, start_ms, end_ms)
    log.info("Total income events: %d", len(income))
    _write_json(OUT_DIR / "raw_income.json", income)

    symbols = sorted({i["symbol"] for i in income if i.get("symbol")})
    log.info("Símbolos com atividade: %d → %s", len(symbols), symbols)

    trades_by_symbol: dict[str, list[dict]] = {}
    if not args.skip_user_trades:
        log.info("Fetching userTrades por símbolo…")
        for sym in symbols:
            log.info("  %s", sym)
            try:
                trades_by_symbol[sym] = paginate_user_trades(client, sym, start_ms, end_ms)
            except BinanceAPIException as exc:
                log.warning("  %s: %s — pulando", sym, exc)
                trades_by_symbol[sym] = []
        _write_json(OUT_DIR / "raw_trades.json", trades_by_symbol)

    closes = reconcile_closes(income, trades_by_symbol)
    closes, matched = attach_bot_metadata(closes, REPO_ROOT / args.bot_state)
    log.info("Closes reconciliados: %d (bot match: %d)", len(closes), matched)
    _write_json(OUT_DIR / "trades_closed.json", closes)

    summary = aggregate(closes, income)
    _write_json(OUT_DIR / "summary.json", summary)

    report_path = OUT_DIR / "report.md"
    write_report(summary, report_path)
    log.info("→ %s", report_path.relative_to(REPO_ROOT))

    log.info("Pronto. Net %s USDT em %d trades.",
             summary["headline"]["net_usdt"], summary["window"]["trades_closed_count"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

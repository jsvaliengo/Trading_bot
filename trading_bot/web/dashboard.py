"""
Dashboard web read-only para acompanhamento do bot.

Uso:
    python -m trading_bot.web.dashboard
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List
from urllib.parse import parse_qs, urlparse

import requests

from ..core.config import config
from ..infra.binance_client import BinanceConnection

logger = logging.getLogger(__name__)
BRT = timezone(timedelta(hours=-3))


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_iso_datetime(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _parse_date_yyyy_mm_dd(raw: Any) -> date | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _read_json_file(file_path: Path) -> tuple[Dict[str, Any], str]:
    if not file_path.exists():
        return ({}, f"arquivo não encontrado: {file_path}")

    try:
        with file_path.open("r", encoding="utf-8") as handle:
            return (json.load(handle), "")
    except Exception as exc:  # pragma: no cover - caminho de IO defensivo
        return ({}, f"falha ao ler {file_path}: {exc}")


def calculate_roi_percent(
    unrealized_pnl: float,
    quantity: float,
    entry_price: float,
    leverage: float,
) -> float:
    """
    ROI aproximado em cima da margem inicial da posição.

    ROI% = unrealized_pnl / (notional / leverage) * 100
    """
    qty = abs(_to_float(quantity))
    entry = _to_float(entry_price)
    lev = abs(_to_float(leverage, 1.0)) or 1.0

    if qty <= 0 or entry <= 0:
        return 0.0

    notional = qty * entry
    initial_margin = notional / lev if lev > 0 else notional
    if initial_margin <= 0:
        return 0.0
    return (_to_float(unrealized_pnl) / initial_margin) * 100.0


class UsdBrlRateProvider:
    """Cache simples de cotação USD/BRL para enriquecer a UI."""

    def __init__(self, ttl_seconds: int = 600, fallback_rate: float = 5.0):
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.fallback_rate = max(0.01, float(fallback_rate))
        self._rate = self.fallback_rate
        self._updated_at: datetime | None = None

    def get_rate(self) -> float:
        now = datetime.now(timezone.utc)

        if self._updated_at:
            elapsed = (now - self._updated_at).total_seconds()
            if elapsed < self.ttl_seconds:
                return self._rate

        try:
            response = requests.get(
                "https://economia.awesomeapi.com.br/json/last/USD-BRL",
                timeout=4,
            )
            if response.status_code == 200:
                payload = response.json()
                bid = _to_float(payload.get("USDBRL", {}).get("bid"), 0.0)
                if bid > 0:
                    self._rate = bid
                    self._updated_at = now
        except Exception as exc:  # pragma: no cover - dependente de rede externa
            logger.warning("Falha ao atualizar USD/BRL: %s", exc)

        return self._rate


class DashboardDataCollector:
    """Consolida dados de conta, posições e estado local para o dashboard."""

    def __init__(
        self,
        state_file_path: str | None = None,
        exchange: Any | None = None,
        exchange_factory: Callable[[], Any] | None = None,
        fx_rate_provider: Callable[[], float] | None = None,
    ):
        self.state_file = Path(state_file_path or config.STATE_FILE_PATH)
        self.deploy_info_file = Path(config.RUNTIME_DIR) / "deploy_info.json"
        self._exchange = exchange
        self._exchange_factory = exchange_factory or BinanceConnection
        self._fx_rate_provider = fx_rate_provider or UsdBrlRateProvider().get_rate
        self._income_cache_payload: List[Dict[str, Any]] = []
        self._income_cache_at: datetime | None = None
        self._income_cache_key: tuple[int, int] | None = None

    def _get_exchange(self):
        if self._exchange is not None:
            return self._exchange

        try:
            self._exchange = self._exchange_factory()
        except Exception as exc:
            logger.error("Falha ao iniciar cliente Binance no dashboard: %s", exc)
            self._exchange = None
        return self._exchange

    def _build_positions(
        self,
        open_positions: List[Dict[str, Any]],
        trailing_activated: Dict[str, Any],
        peak_prices: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for pos in open_positions:
            symbol = str(pos.get("symbol", "") or "")
            side = str(pos.get("side", "") or "").upper()
            quantity = _to_float(pos.get("quantity"), 0.0)
            entry_price = _to_float(pos.get("entry_price"), 0.0)
            mark_price = _to_float(pos.get("mark_price"), 0.0)
            unrealized_pnl = _to_float(pos.get("unrealized_pnl"), 0.0)
            leverage = _to_int(pos.get("leverage"), config.LEVERAGE)
            position_key = f"{symbol}_{side}"

            normalized.append(
                {
                    "symbol": symbol,
                    "side": side,
                    "quantity": quantity,
                    "entry_price": entry_price,
                    "mark_price": mark_price,
                    "unrealized_pnl": unrealized_pnl,
                    "leverage": leverage,
                    "roi_percent": calculate_roi_percent(
                        unrealized_pnl=unrealized_pnl,
                        quantity=quantity,
                        entry_price=entry_price,
                        leverage=leverage,
                    ),
                    "trailing_active": bool(trailing_activated.get(position_key, False)),
                    "peak_price": _to_float(peak_prices.get(position_key), 0.0),
                }
            )

        normalized.sort(key=lambda item: abs(item["unrealized_pnl"]), reverse=True)
        return normalized

    def _get_income_events(
        self,
        exchange: Any,
        start_ms: int,
        end_ms: int,
        cache_ttl_seconds: int = 60,
    ) -> List[Dict[str, Any]]:
        now_utc = datetime.now(timezone.utc)
        cache_key = (int(start_ms), int(end_ms))

        if self._income_cache_at is not None and self._income_cache_key == cache_key:
            elapsed = (now_utc - self._income_cache_at).total_seconds()
            if elapsed < cache_ttl_seconds:
                return self._income_cache_payload

        items: List[Dict[str, Any]] = []
        seen = set()
        cursor = int(start_ms)
        max_loops = 20

        for _ in range(max_loops):
            batch = exchange.get_income_history(limit=1000, start_time=cursor)
            if not isinstance(batch, list) or not batch:
                break

            max_batch_ts = cursor
            for item in batch:
                ts = _to_int(item.get("time"), 0)
                if ts <= 0:
                    continue
                if ts < start_ms or ts > end_ms:
                    continue

                unique_id = (
                    ts,
                    str(item.get("incomeType", "")),
                    str(item.get("symbol", "")),
                    str(item.get("income", "")),
                    str(item.get("asset", "")),
                    str(item.get("tranId", "")),
                )
                if unique_id in seen:
                    continue
                seen.add(unique_id)
                items.append(item)
                if ts > max_batch_ts:
                    max_batch_ts = ts

            if max_batch_ts <= cursor:
                break
            cursor = max_batch_ts + 1
            if cursor > end_ms:
                break

        self._income_cache_payload = items
        self._income_cache_at = now_utc
        self._income_cache_key = cache_key
        return items

    def _build_pnl_analytics(
        self,
        exchange: Any | None,
        state_payload: Dict[str, Any],
        errors: List[str],
        start_date: date,
        end_date: date,
    ) -> Dict[str, Any]:
        total_days = max(1, (end_date - start_date).days + 1)
        day_net: Dict[str, float] = defaultdict(float)
        realized_total = 0.0
        commission_total = 0.0
        funding_total = 0.0
        trades_win_count = 0
        trades_loss_count = 0
        trades_breakeven_count = 0

        if exchange is not None and hasattr(exchange, "get_income_history"):
            try:
                start_ms = int(datetime.combine(start_date, datetime.min.time(), tzinfo=BRT).astimezone(timezone.utc).timestamp() * 1000)
                end_ms = int(datetime.combine(end_date, datetime.max.time(), tzinfo=BRT).astimezone(timezone.utc).timestamp() * 1000)
                income_items = self._get_income_events(
                    exchange,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    cache_ttl_seconds=60,
                )
                for item in income_items:
                    try:
                        income_type = str(item.get("incomeType", "") or "")
                        if income_type not in {"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"}:
                            continue

                        amount = _to_float(item.get("income"), 0.0)
                        ts_ms = _to_int(item.get("time"), 0)
                        if ts_ms <= 0:
                            continue

                        day_local = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(BRT).date()
                        if day_local < start_date or day_local > end_date:
                            continue

                        day_key = day_local.strftime("%Y-%m-%d")
                        day_net[day_key] += amount
                        if income_type == "REALIZED_PNL":
                            realized_total += amount
                            if amount > 0:
                                trades_win_count += 1
                            elif amount < 0:
                                trades_loss_count += 1
                            else:
                                trades_breakeven_count += 1
                        elif income_type == "COMMISSION":
                            commission_total += amount
                        elif income_type == "FUNDING_FEE":
                            funding_total += amount
                    except Exception:
                        continue
            except Exception as exc:
                errors.append(f"erro em get_income_history (analytics): {exc}")

        trailing_days: List[Dict[str, Any]] = []
        for offset in range(total_days):
            day = start_date + timedelta(days=offset)
            key = day.strftime("%Y-%m-%d")
            trailing_days.append(
                {
                    "date": key,
                    "day": day.day,
                    "weekday": day.weekday(),  # segunda=0
                    "pnl": round(day_net.get(key, 0.0), 8),
                }
            )

        month_start = start_date.replace(day=1)
        next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
        days_in_month = (next_month - month_start).days
        month_days: List[Dict[str, Any]] = []
        for day_num in range(1, days_in_month + 1):
            day = month_start.replace(day=day_num)
            key = day.strftime("%Y-%m-%d")
            month_days.append(
                {
                    "date": key,
                    "day": day_num,
                    "weekday": day.weekday(),
                    "pnl": round(day_net.get(key, 0.0), 8),
                }
            )

        pnl_period = sum(item["pnl"] for item in trailing_days)

        initial_capital = _to_float(state_payload.get("initial_capital"), 0.0)
        lifetime_usd = _to_float(state_payload.get("total_pnl"), 0.0)

        def _pct(value: float) -> float:
            if initial_capital <= 0:
                return 0.0
            return (value / initial_capital) * 100.0

        positive_days = [item["pnl"] for item in trailing_days if item["pnl"] > 0]
        negative_days = [item["pnl"] for item in trailing_days if item["pnl"] < 0]
        breakeven_days = [item["pnl"] for item in trailing_days if item["pnl"] == 0]

        total_profit = sum(positive_days)
        total_loss = sum(negative_days)
        avg_profit = total_profit / len(positive_days) if positive_days else 0.0
        avg_loss_abs = abs(total_loss) / len(negative_days) if negative_days else 0.0
        pl_ratio = (avg_profit / avg_loss_abs) if avg_loss_abs > 0 else None

        cumulative_usd: List[Dict[str, Any]] = []
        cumulative_pct: List[Dict[str, Any]] = []
        running = 0.0
        for item in trailing_days:
            running += item["pnl"]
            cumulative_usd.append({"date": item["date"], "value": round(running, 8)})
            cumulative_pct.append({"date": item["date"], "value": round(_pct(running), 8)})

        return {
            "window_days": total_days,
            "month_label": month_start.strftime("%Y-%m"),
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d"),
            "month_days": month_days,
            "daily_series": trailing_days,
            "cumulative_usd": cumulative_usd,
            "cumulative_pct": cumulative_pct,
            "summary": {
                "total_profit_usd": total_profit,
                "total_loss_usd": total_loss,
                "net_period_usd": pnl_period,
                "winning_days": len(positive_days),
                "losing_days": len(negative_days),
                "breakeven_days": len(breakeven_days),
                "avg_profit_usd": avg_profit,
                "avg_loss_usd": -avg_loss_abs if avg_loss_abs > 0 else 0.0,
                "profit_loss_ratio": pl_ratio,
                "realized_total_usd": realized_total,
                "commission_total_usd": commission_total,
                "funding_total_usd": funding_total,
                "net_after_costs_usd": realized_total + commission_total + funding_total,
                "trades_win_count": trades_win_count,
                "trades_loss_count": trades_loss_count,
                "trades_breakeven_count": trades_breakeven_count,
                "trades_total_count": trades_win_count + trades_loss_count + trades_breakeven_count,
            },
            "pnl": {
                "period_usd": pnl_period,
                "period_pct": _pct(pnl_period),
                "lifetime_usd": lifetime_usd,
                "lifetime_pct": _pct(lifetime_usd),
            },
        }

    def collect(
        self,
        start_date_str: str = "",
        end_date_str: str = "",
        include_analytics: bool = True,
    ) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        errors: List[str] = []
        now_brt = datetime.now(BRT).date()

        parsed_start = _parse_date_yyyy_mm_dd(start_date_str)
        parsed_end = _parse_date_yyyy_mm_dd(end_date_str)

        if parsed_start is None and parsed_end is None:
            end_date = now_brt
            start_date = end_date - timedelta(days=29)
        else:
            end_date = parsed_end or now_brt
            start_date = parsed_start or (end_date - timedelta(days=29))

        if start_date > end_date:
            start_date, end_date = end_date, start_date

        max_span_days = 370
        span_days = (end_date - start_date).days + 1
        if span_days > max_span_days:
            start_date = end_date - timedelta(days=max_span_days - 1)
            errors.append("intervalo reduzido automaticamente para no máximo 370 dias")

        state_payload, state_error = _read_json_file(self.state_file)
        if state_error:
            errors.append(state_error)

        deploy_info_payload, deploy_error = _read_json_file(self.deploy_info_file)
        if deploy_error:
            deploy_info_payload = {}

        trailing_activated = state_payload.get("trailing_activated", {}) or {}
        peak_prices = state_payload.get("peak_prices", {}) or {}

        account_info = {
            "wallet_balance": 0.0,
            "available_balance": 0.0,
            "unrealized_pnl": 0.0,
            "margin_balance": 0.0,
            "total_initial_margin": 0.0,
        }
        daily_pnl = {
            "realized_pnl": 0.0,
            "funding_fee": 0.0,
            "commission": 0.0,
            "total": 0.0,
            "income_count": 0,
            "income_types": [],
        }
        open_positions: List[Dict[str, Any]] = []
        retry_report: Dict[str, Any] = {
            "calls": 0,
            "retries": 0,
            "failures": 0,
            "retry_rate": 0.0,
            "failure_rate": 0.0,
            "endpoints": [],
        }
        order_report: Dict[str, Any] = {
            "attempts": 0,
            "successes": 0,
            "failures": 0,
            "rejections": 0,
            "failure_rate": 0.0,
            "rejection_rate": 0.0,
            "symbols": [],
        }

        exchange = self._get_exchange()
        if exchange is None:
            errors.append("cliente Binance indisponível (verifique API key/secret e IP whitelist)")
        else:
            try:
                account_info = exchange.get_account_info()
            except Exception as exc:
                errors.append(f"erro em get_account_info: {exc}")

            try:
                daily_pnl = exchange.get_daily_pnl_from_binance()
            except Exception as exc:
                errors.append(f"erro em get_daily_pnl_from_binance: {exc}")

            try:
                open_positions = exchange.get_open_positions()
            except Exception as exc:
                errors.append(f"erro em get_open_positions: {exc}")

            if hasattr(exchange, "get_retry_stats_report"):
                try:
                    retry_report = exchange.get_retry_stats_report(reset=False)
                except Exception as exc:
                    errors.append(f"erro em get_retry_stats_report: {exc}")

            if hasattr(exchange, "get_order_stats_report"):
                try:
                    order_report = exchange.get_order_stats_report(reset=False)
                except Exception as exc:
                    errors.append(f"erro em get_order_stats_report: {exc}")

        positions = self._build_positions(open_positions, trailing_activated, peak_prices)
        analytics: Dict[str, Any] = {}
        if include_analytics:
            analytics = self._build_pnl_analytics(
                exchange=exchange,
                state_payload=state_payload,
                errors=errors,
                start_date=start_date,
                end_date=end_date,
            )

        longs = [pos for pos in positions if pos["side"] == "LONG"]
        shorts = [pos for pos in positions if pos["side"] == "SHORT"]
        total_open_pnl = sum(pos["unrealized_pnl"] for pos in positions)
        positions_notional = sum(abs(pos["quantity"] * pos["mark_price"]) for pos in positions)

        history = list(state_payload.get("portfolio_history", []) or [])[-24:]
        state_saved_at = _parse_iso_datetime(state_payload.get("saved_at"))
        state_age_seconds = None
        if state_saved_at is not None:
            state_age_seconds = max(0.0, (now - state_saved_at.astimezone(timezone.utc)).total_seconds())

        max_stale_seconds = max(180, int(config.CHECK_INTERVAL) * 40)
        bot_alive_guess = (
            state_age_seconds is not None and state_age_seconds <= max_stale_seconds
        )

        fx_rate = _to_float(self._fx_rate_provider(), 5.0)

        return {
            "generated_at": now.isoformat(),
            "environment": {
                "app_env": config.APP_ENV,
                "use_testnet": bool(config.USE_TESTNET),
            },
            "fx": {"usd_brl": fx_rate},
            "runtime": {
                "state_file_path": str(self.state_file),
                "state_saved_at": state_payload.get("saved_at", ""),
                "state_age_seconds": state_age_seconds,
                "bot_alive_guess": bool(bot_alive_guess),
                "deploy_info": deploy_info_payload,
                "deploy_info_available": bool(deploy_info_payload),
            },
            "account": account_info,
            "daily": daily_pnl,
            "trades": {
                "closed_trades_count": _to_int(state_payload.get("closed_trades_count"), 0),
                "realized_total_session": _to_float(state_payload.get("total_pnl"), 0.0),
                "trades_win_count": _to_int(state_payload.get("trades_win_count"), 0),
                "trades_loss_count": _to_int(state_payload.get("trades_loss_count"), 0),
                "trades_win_total": _to_float(state_payload.get("trades_win_total"), 0.0),
                "trades_loss_total": _to_float(state_payload.get("trades_loss_total"), 0.0),
                "total_fees_paid": _to_float(state_payload.get("total_fees_paid"), 0.0),
                "daily_realized_pnl": _to_float(state_payload.get("daily_realized_pnl"), 0.0),
                "history_points": history,
            },
            "risk": {
                "stop_loss_percent": _to_float(config.STOP_LOSS_PERCENT, 0.0),
                "take_profit_percent": _to_float(config.TAKE_PROFIT_PERCENT, 0.0),
                "trailing_activation_percent": _to_float(config.TRAILING_ACTIVATION_PERCENT, 0.0),
                "trailing_distance_percent": _to_float(config.TRAILING_DISTANCE_PERCENT, 0.0),
                "stop_loss_enabled": bool(config.USE_INDIVIDUAL_STOP_LOSS),
            },
            "positions_summary": {
                "count": len(positions),
                "long_count": len(longs),
                "short_count": len(shorts),
                "trailing_active_count": sum(1 for pos in positions if pos["trailing_active"]),
                "total_open_pnl": total_open_pnl,
                "total_notional": positions_notional,
            },
            "positions": positions,
            "health": {
                "api": retry_report,
                "orders": order_report,
            },
            "analytics": analytics,
            "errors": errors,
        }


_DASHBOARD_HTML_TEMPLATE = """<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Trading Bot Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-1: #08121f;
      --bg-2: #0f2534;
      --panel: rgba(16, 34, 48, 0.82);
      --panel-strong: rgba(12, 26, 37, 0.95);
      --line: rgba(148, 180, 203, 0.26);
      --text: #f0f5f8;
      --muted: #91a6b6;
      --good: #3fe27f;
      --bad: #ff6f61;
      --warn: #f8c14b;
      --accent: #47d3ff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: "Space Grotesk", system-ui, sans-serif;
      color: var(--text);
      background:
        radial-gradient(1200px 600px at 90% -20%, rgba(71, 211, 255, 0.20), transparent 70%),
        radial-gradient(900px 500px at -10% 120%, rgba(63, 226, 127, 0.14), transparent 70%),
        linear-gradient(160deg, var(--bg-1), var(--bg-2));
      padding: 24px;
    }
    .wrap {
      max-width: 1300px;
      margin: 0 auto;
      display: grid;
      gap: 18px;
    }
    .hero {
      background: linear-gradient(135deg, rgba(71, 211, 255, 0.16), rgba(15, 37, 52, 0.70));
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 20px;
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
      backdrop-filter: blur(6px);
    }
    .hero h1 {
      margin: 0;
      font-size: clamp(1.3rem, 1.2rem + 1.2vw, 2rem);
      letter-spacing: .4px;
    }
    .hero p { margin: 6px 0 0 0; color: var(--muted); }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 8px 12px;
      font-size: .85rem;
      background: rgba(8, 18, 31, 0.55);
      color: var(--muted);
      white-space: nowrap;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 14px;
      backdrop-filter: blur(4px);
    }
    .kpi-title {
      color: var(--muted);
      margin-bottom: 8px;
      font-size: .85rem;
      text-transform: uppercase;
      letter-spacing: .08em;
    }
    .kpi-value {
      font-weight: 700;
      font-size: clamp(0.95rem, 0.68rem + 0.48vw, 1.2rem);
      line-height: 1.12;
      letter-spacing: -0.01em;
      overflow-wrap: normal;
      word-break: keep-all;
    }
    .kpi-money {
      font-size: clamp(0.88rem, 0.58rem + 0.34vw, 1.05rem);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .muted { color: var(--muted); }
    .good { color: var(--good); }
    .bad { color: var(--bad); }
    .warn { color: var(--warn); }
    .section {
      display: grid;
      gap: 12px;
      grid-template-columns: 1.2fr .8fr;
    }
    .table-wrap {
      background: var(--panel-strong);
      border: 1px solid var(--line);
      border-radius: 14px;
      overflow: hidden;
    }
    table {
      width: 100%;
      border-collapse: collapse;
    }
    th, td {
      padding: 11px 12px;
      border-bottom: 1px solid rgba(148, 180, 203, 0.17);
      text-align: left;
      font-size: .92rem;
    }
    th { color: var(--muted); font-weight: 500; }
    tbody tr:hover { background: rgba(71, 211, 255, 0.06); }
    .tag {
      border-radius: 999px;
      padding: 4px 9px;
      font-size: .75rem;
      border: 1px solid currentColor;
      display: inline-block;
      line-height: 1.1;
    }
    .tag.long { color: var(--good); }
    .tag.short { color: var(--bad); }
    .panel-grid {
      display: grid;
      gap: 12px;
    }
    .info-list {
      margin: 0;
      padding: 0;
      list-style: none;
      display: grid;
      gap: 8px;
      font-size: .92rem;
    }
    .info-list li {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      color: var(--muted);
      border-bottom: 1px dashed rgba(148, 180, 203, 0.2);
      padding-bottom: 6px;
    }
    .bar-row {
      margin-top: 8px;
      display: grid;
      grid-auto-flow: column;
      grid-auto-columns: minmax(6px, 1fr);
      gap: 3px;
      align-items: end;
      min-height: 48px;
      position: relative;
    }
    .bar {
      border-radius: 4px 4px 2px 2px;
      background: linear-gradient(180deg, rgba(71, 211, 255, 0.9), rgba(71, 211, 255, 0.2));
      min-height: 4px;
      cursor: pointer;
    }
    .history-tooltip {
      position: fixed;
      z-index: 1000;
      pointer-events: none;
      background: rgba(235, 241, 247, 0.96);
      color: #172230;
      border: 1px solid rgba(23, 34, 48, 0.18);
      border-radius: 8px;
      padding: 8px 10px;
      font-size: .82rem;
      line-height: 1.3;
      box-shadow: 0 10px 28px rgba(0, 0, 0, 0.25);
      display: none;
      max-width: 260px;
      white-space: nowrap;
    }
    .error-box {
      background: rgba(255, 111, 97, 0.10);
      border: 1px solid rgba(255, 111, 97, 0.35);
      border-radius: 10px;
      padding: 10px 12px;
      font-size: .88rem;
      color: #ffd2cc;
      display: none;
    }
    .footer {
      color: var(--muted);
      font-size: .82rem;
      text-align: right;
      margin-top: -2px;
    }
    .stats-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
    }
    .mini-stat {
      padding: 10px;
      border: 1px solid rgba(148, 180, 203, 0.20);
      border-radius: 10px;
      background: rgba(8, 18, 31, 0.35);
    }
    .mini-title {
      color: var(--muted);
      font-size: .78rem;
      margin-bottom: 4px;
    }
    .mini-value {
      font-weight: 700;
      font-size: 1.1rem;
      line-height: 1.1;
    }
    .mini-sub {
      color: var(--muted);
      font-size: .82rem;
      margin-top: 2px;
    }
    .analytics-block {
      display: grid;
      grid-template-columns: 1.05fr .95fr;
      gap: 12px;
    }
    .calendar-wrap {
      display: grid;
      gap: 6px;
    }
    .calendar-head {
      display: grid;
      grid-template-columns: repeat(7, minmax(0, 1fr));
      gap: 6px;
      color: var(--muted);
      font-size: .78rem;
      text-align: center;
      text-transform: uppercase;
      letter-spacing: .08em;
    }
    .calendar-grid {
      display: grid;
      grid-template-columns: repeat(7, minmax(0, 1fr));
      gap: 6px;
    }
    .calendar-cell {
      min-height: 58px;
      border-radius: 8px;
      border: 1px solid rgba(148, 180, 203, 0.20);
      padding: 6px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      background: rgba(148, 180, 203, 0.08);
    }
    .calendar-empty {
      background: transparent;
      border: 1px dashed rgba(148, 180, 203, 0.12);
    }
    .calendar-cell.pos { background: rgba(63, 226, 127, 0.16); border-color: rgba(63, 226, 127, 0.35); }
    .calendar-cell.neg { background: rgba(255, 111, 97, 0.14); border-color: rgba(255, 111, 97, 0.35); }
    .calendar-day { font-size: .84rem; color: var(--text); }
    .calendar-pnl { font-size: .76rem; color: var(--muted); }
    .chart-wrap {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    .chart-svg {
      width: 100%;
      height: 230px;
      border-radius: 10px;
      background: rgba(8, 18, 31, 0.35);
      border: 1px solid rgba(148, 180, 203, 0.20);
    }
    .chart-caption {
      color: var(--muted);
      font-size: .8rem;
      margin-top: 6px;
    }
    @media (max-width: 1080px) {
      .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .section { grid-template-columns: 1fr; }
      .stats-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .analytics-block { grid-template-columns: 1fr; }
      .chart-wrap { grid-template-columns: 1fr; }
    }
    @media (max-width: 640px) {
      body { padding: 12px; }
      .hero { flex-direction: column; align-items: flex-start; }
      .grid { grid-template-columns: 1fr; }
      th, td { padding: 10px 8px; font-size: .84rem; }
      .stats-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <header class="hero">
      <div>
        <h1>Trading Bot Monitor</h1>
        <p>Acompanhamento em tempo real das operações, risco e saúde do bot.</p>
      </div>
      <div class="pill" id="status-pill">Carregando...</div>
    </header>

    <div id="errors" class="error-box"></div>

    <section class="card" style="display:grid;gap:10px">
      <div class="kpi-title">Período de Consulta</div>
      <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:center">
        <button class="tag" id="preset-7d" type="button">7D</button>
        <button class="tag" id="preset-1m" type="button">1M</button>
        <button class="tag" id="preset-3m" type="button">3M</button>
        <button class="tag" id="preset-1y" type="button">1Y</button>
        <label class="muted" for="start-date">Início</label>
        <input id="start-date" type="date" style="background:rgba(8,18,31,.55);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:6px 8px;" />
        <label class="muted" for="end-date">Fim</label>
        <input id="end-date" type="date" style="background:rgba(8,18,31,.55);color:var(--text);border:1px solid var(--line);border-radius:8px;padding:6px 8px;" />
        <button id="apply-range" type="button" class="tag">Aplicar</button>
        <button id="refresh-analytics" type="button" class="tag">Atualizar análise</button>
      </div>
      <div id="range-label" class="muted">Período: -- | clique em "Atualizar análise" para recalcular</div>
    </section>

    <section class="grid">
      <article class="card">
        <div class="kpi-title">Saldo Carteira</div>
        <div class="kpi-value kpi-money" id="wallet-balance">$0.00</div>
        <div class="muted" id="available-balance">Disponível $0.00</div>
      </article>
      <article class="card">
        <div class="kpi-title" id="period-pnl-title">P&L Diário (tempo real)</div>
        <div class="kpi-value kpi-money" id="daily-total">$0.00</div>
        <div class="muted" id="daily-breakdown">Realizado/Funding/Comissão</div>
      </article>
      <article class="card">
        <div class="kpi-title">P&L Aberto</div>
        <div class="kpi-value kpi-money" id="open-pnl">$0.00</div>
        <div class="muted" id="positions-count">0 posições abertas</div>
      </article>
      <article class="card">
        <div class="kpi-title" id="period-trades-title">Trades (sessão)</div>
        <div class="kpi-value" id="closed-trades">0</div>
        <div class="muted" id="win-loss">Wins 0 | Losses 0</div>
      </article>
    </section>

    <section class="section">
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Par</th>
              <th>Lado</th>
              <th>Qtd</th>
              <th>Entrada</th>
              <th>Mark</th>
              <th>P&L</th>
              <th>ROI</th>
              <th>Trailing</th>
            </tr>
          </thead>
          <tbody id="positions-body">
            <tr><td colspan="8" class="muted">Sem dados...</td></tr>
          </tbody>
        </table>
      </div>

      <div class="panel-grid">
        <article class="card">
          <div class="kpi-title">Saúde Operacional</div>
          <ul class="info-list">
            <li><span>API Calls</span><strong id="api-calls">0</strong></li>
            <li><span>API Falhas</span><strong id="api-failures">0</strong></li>
            <li><span>API Retries</span><strong id="api-retries">0</strong></li>
            <li><span>Ordens Falhas</span><strong id="order-failures">0</strong></li>
            <li><span>Ordens Rejeições</span><strong id="order-rejections">0</strong></li>
          </ul>
        </article>

        <article class="card">
          <div class="kpi-title">Risco</div>
          <ul class="info-list">
            <li><span>Stop Loss</span><strong id="risk-sl">-</strong></li>
            <li><span>Take Profit</span><strong id="risk-tp">-</strong></li>
            <li><span>Trailing</span><strong id="risk-trailing">-</strong></li>
            <li><span>Trailing ativos</span><strong id="risk-trailing-active">0</strong></li>
            <li><span>Notional aberto</span><strong id="risk-notional">$0.00</strong></li>
          </ul>
        </article>

        <article class="card">
          <div class="kpi-title">Evolução (realizado)</div>
          <div id="history-label" class="muted">Sem histórico</div>
          <div id="history-bars" class="bar-row"></div>
        </article>
      </div>
    </section>

    <section class="analytics-block">
      <article class="card">
        <div class="kpi-title">Profit And Loss Analysis (30d)</div>
        <div class="stats-grid">
          <div class="mini-stat">
            <div class="mini-title" id="mini-title-1">Período</div>
            <div class="mini-value" id="stat-today">$0.00</div>
            <div class="mini-sub" id="stat-today-pct">0.00%</div>
          </div>
          <div class="mini-stat">
            <div class="mini-title" id="mini-title-2">Média / dia</div>
            <div class="mini-value" id="stat-7d">$0.00</div>
            <div class="mini-sub" id="stat-7d-pct">0.00%</div>
          </div>
          <div class="mini-stat">
            <div class="mini-title" id="mini-title-3">Melhor dia</div>
            <div class="mini-value" id="stat-30d">$0.00</div>
            <div class="mini-sub" id="stat-30d-pct">0.00%</div>
          </div>
          <div class="mini-stat">
            <div class="mini-title" id="mini-title-4">Lifetime</div>
            <div class="mini-value" id="stat-life">$0.00</div>
            <div class="mini-sub" id="stat-life-pct">0.00%</div>
          </div>
        </div>
        <ul class="info-list" style="margin-top:10px">
          <li><span>Total Profit</span><strong id="sum-profit">$0.00</strong></li>
          <li><span>Total Loss</span><strong id="sum-loss">$0.00</strong></li>
          <li><span>Winning Days</span><strong id="sum-win-days">0</strong></li>
          <li><span>Losing Days</span><strong id="sum-loss-days">0</strong></li>
          <li><span>Breakeven Days</span><strong id="sum-flat-days">0</strong></li>
          <li><span>Profit/Loss Ratio</span><strong id="sum-ratio">-</strong></li>
        </ul>
      </article>

      <article class="card calendar-wrap">
        <div class="kpi-title">Daily PNL Calendar (<span id="calendar-month">----</span>)</div>
        <div class="calendar-head">
          <div>Seg</div><div>Ter</div><div>Qua</div><div>Qui</div><div>Sex</div><div>Sáb</div><div>Dom</div>
        </div>
        <div id="calendar-grid" class="calendar-grid"></div>
      </article>
    </section>

    <section class="chart-wrap">
      <article class="card">
        <div class="kpi-title">Cumulative PNL (USD)</div>
        <svg id="chart-cum-usd" class="chart-svg" viewBox="0 0 640 230" preserveAspectRatio="none"></svg>
        <div class="chart-caption" id="chart-cum-usd-caption">Sem dados</div>
      </article>
      <article class="card">
        <div class="kpi-title">Cumulative PNL %</div>
        <svg id="chart-cum-pct" class="chart-svg" viewBox="0 0 640 230" preserveAspectRatio="none"></svg>
        <div class="chart-caption" id="chart-cum-pct-caption">Sem dados</div>
      </article>
    </section>

    <div class="footer" id="footer">Aguardando atualização...</div>
  </div>

  <script>
    const REFRESH_SECONDS = __REFRESH_SECONDS__;
    const TOKEN_REQUIRED = __TOKEN_REQUIRED__;
    const rangeState = { start: "", end: "" };
    let analyticsState = null;

    function formatMoney(value, rate, decimals = 2) {
      const usd = Number(value || 0);
      const brl = usd * rate;
      const sign = usd > 0 ? "+" : "";
      return `${sign}$${usd.toFixed(decimals)} (R$ ${brl.toFixed(decimals)})`;
    }

    function formatPlain(value, decimals = 2) {
      const n = Number(value || 0);
      const sign = n > 0 ? "+" : "";
      return `${sign}$${n.toFixed(decimals)}`;
    }

    function escapeHtml(text) {
      return String(text)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
    }

    function tokenHeaders(token) {
      if (!token) return {};
      return { "X-Auth-Token": token };
    }

    function buildApiUrl(path, includeRange = false) {
      const params = new URLSearchParams(window.location.search);
      const token = params.get("token");
      const q = new URLSearchParams();
      if (token) q.set("token", token);
      if (includeRange && rangeState.start) q.set("start", rangeState.start);
      if (includeRange && rangeState.end) q.set("end", rangeState.end);
      const qs = q.toString();
      return qs ? `${path}?${qs}` : path;
    }

    function toYmd(d) {
      const y = d.getFullYear();
      const m = String(d.getMonth() + 1).padStart(2, "0");
      const day = String(d.getDate()).padStart(2, "0");
      return `${y}-${m}-${day}`;
    }

    function shiftDays(base, delta) {
      const d = new Date(base.getTime());
      d.setDate(d.getDate() + delta);
      return d;
    }

    function setRange(start, end) {
      rangeState.start = start;
      rangeState.end = end;
      const startInput = document.getElementById("start-date");
      const endInput = document.getElementById("end-date");
      if (startInput) startInput.value = start;
      if (endInput) endInput.value = end;
      const label = document.getElementById("range-label");
      if (label) label.textContent = `Período: ${start} → ${end} | clique em "Atualizar análise" para recalcular`;
    }

    function setupRangeControls() {
      const end = new Date();
      const today = toYmd(end);
      setRange(toYmd(shiftDays(end, -29)), today); // padrão 30 dias

      const query = new URLSearchParams(window.location.search);
      const qStart = query.get("start");
      const qEnd = query.get("end");
      if (qStart && qEnd) {
        setRange(qStart, qEnd);
      }

      const mapPreset = (days) => {
        const e = new Date();
        const s = shiftDays(e, -(days - 1));
        setRange(toYmd(s), toYmd(e));
      };

      document.getElementById("preset-7d").addEventListener("click", () => mapPreset(7));
      document.getElementById("preset-1m").addEventListener("click", () => mapPreset(30));
      document.getElementById("preset-3m").addEventListener("click", () => mapPreset(90));
      document.getElementById("preset-1y").addEventListener("click", () => mapPreset(365));

      document.getElementById("apply-range").addEventListener("click", () => {
        const start = document.getElementById("start-date").value;
        const endDate = document.getElementById("end-date").value;
        if (!start || !endDate) return;
        if (start <= endDate) {
          setRange(start, endDate);
        } else {
          setRange(endDate, start);
        }
      });

      document.getElementById("refresh-analytics").addEventListener("click", () => {
        refreshAnalytics();
      });
    }

    function setClassByValue(el, value) {
      el.classList.remove("good", "bad", "warn");
      if (value > 0) {
        el.classList.add("good");
      } else if (value < 0) {
        el.classList.add("bad");
      } else {
        el.classList.add("warn");
      }
    }

    function ensureHistoryTooltip() {
      let tip = document.getElementById("history-tooltip");
      if (!tip) {
        tip = document.createElement("div");
        tip.id = "history-tooltip";
        tip.className = "history-tooltip";
        document.body.appendChild(tip);
      }
      return tip;
    }

    function showHistoryTooltip(evt, title, valueText) {
      const tip = ensureHistoryTooltip();
      tip.innerHTML = `<div>${escapeHtml(title)}</div><div><strong>${escapeHtml(valueText)}</strong></div>`;
      tip.style.display = "block";
      const pad = 12;
      const rect = tip.getBoundingClientRect();
      let x = evt.clientX + 14;
      let y = evt.clientY - rect.height - 14;
      if (x + rect.width + pad > window.innerWidth) {
        x = evt.clientX - rect.width - 14;
      }
      if (x < pad) x = pad;
      if (y < pad) y = evt.clientY + 18;
      tip.style.left = `${x}px`;
      tip.style.top = `${y}px`;
    }

    function hideHistoryTooltip() {
      const tip = document.getElementById("history-tooltip");
      if (tip) tip.style.display = "none";
    }

    function renderHistory(points, rate) {
      const bars = document.getElementById("history-bars");
      const label = document.getElementById("history-label");
      bars.innerHTML = "";
      hideHistoryTooltip();

      if (!Array.isArray(points) || points.length === 0) {
        label.textContent = "Sem snapshots no state file";
        return;
      }

      const values = points.map(p => Number((p && p.pnl_realized) || 0));
      const maxAbs = Math.max(0.5, ...values.map(v => Math.abs(v)));

      points.slice(-18).forEach((p) => {
        const val = Number((p && p.pnl_realized) || 0);
        const h = Math.max(4, Math.round((Math.abs(val) / maxAbs) * 44));
        const bar = document.createElement("div");
        bar.className = "bar";
        bar.style.height = `${h}px`;
        bar.style.opacity = val === 0 ? "0.4" : "1";
        bar.style.background = val >= 0
          ? "linear-gradient(180deg, rgba(63,226,127,0.92), rgba(63,226,127,0.2))"
          : "linear-gradient(180deg, rgba(255,111,97,0.92), rgba(255,111,97,0.2))";

        let when = "Snapshot";
        if (p && p.timestamp) {
          const dt = new Date(String(p.timestamp));
          if (!Number.isNaN(dt.getTime())) {
            when = dt.toLocaleString("pt-BR");
          }
        }
        const valText = formatMoney(val, rate, 2);
        bar.addEventListener("mouseenter", (evt) => showHistoryTooltip(evt, when, valText));
        bar.addEventListener("mousemove", (evt) => showHistoryTooltip(evt, when, valText));
        bar.addEventListener("mouseleave", () => hideHistoryTooltip());

        bars.appendChild(bar);
      });

      const last = Number(values[values.length - 1] || 0);
      label.textContent = `Último realizado: ${formatMoney(last, rate, 2)}`;
      setClassByValue(label, last);
    }

    function renderCalendar(days, rate) {
      const container = document.getElementById("calendar-grid");
      container.innerHTML = "";

      if (!Array.isArray(days) || days.length === 0) {
        container.innerHTML = `<div class="calendar-cell calendar-empty" style="grid-column: span 7">Sem dados para calendário</div>`;
        return;
      }

      const firstWeekday = Number(days[0].weekday || 0); // seg=0
      for (let i = 0; i < firstWeekday; i += 1) {
        const empty = document.createElement("div");
        empty.className = "calendar-cell calendar-empty";
        container.appendChild(empty);
      }

      days.forEach((item) => {
        const pnl = Number(item.pnl || 0);
        const cls = pnl > 0 ? "pos" : pnl < 0 ? "neg" : "";
        const cell = document.createElement("div");
        cell.className = `calendar-cell ${cls}`.trim();
        cell.innerHTML = `
          <div class="calendar-day">${Number(item.day || 0)}</div>
          <div class="calendar-pnl">${formatMoney(pnl, rate, 2)}</div>
        `;
        container.appendChild(cell);
      });
    }

    function drawLineChart(
      svgId,
      captionId,
      points,
      color,
      valueKey = "value",
      asPercent = false,
      seriesLabel = "Cumulative PNL"
    ) {
      const svg = document.getElementById(svgId);
      const caption = document.getElementById(captionId);

      if (!svg) return;
      const width = 640;
      const height = 230;
      const padX = 28;
      const padY = 20;

      if (!Array.isArray(points) || points.length === 0) {
        svg.innerHTML = "";
        if (caption) caption.textContent = "Sem dados";
        return;
      }

      const values = points.map((p) => Number((p && p[valueKey]) || 0));
      const minV = Math.min(...values, 0);
      const maxV = Math.max(...values, 0);
      const span = Math.max(1e-9, maxV - minV);
      const innerW = width - padX * 2;
      const innerH = height - padY * 2;

      const toX = (i) => padX + ((innerW * i) / Math.max(1, values.length - 1));
      const toY = (v) => padY + (maxV - v) / span * innerH;

      const yZero = toY(0);
      let d = "";
      values.forEach((v, i) => {
        const x = toX(i);
        const y = toY(v);
        d += `${i === 0 ? "M" : "L"} ${x.toFixed(2)} ${y.toFixed(2)} `;
      });

      const lines = [];
      for (let i = 0; i <= 4; i += 1) {
        const y = padY + (innerH / 4) * i;
        lines.push(`<line x1="${padX}" y1="${y}" x2="${width - padX}" y2="${y}" stroke="rgba(148,180,203,0.16)" stroke-width="1" />`);
      }

      svg.innerHTML = `
        <rect x="0" y="0" width="${width}" height="${height}" fill="transparent" />
        ${lines.join("")}
        <line x1="${padX}" y1="${yZero.toFixed(2)}" x2="${width - padX}" y2="${yZero.toFixed(2)}" stroke="rgba(248,193,75,0.35)" stroke-width="1.2" />
        <path d="${d.trim()}" fill="none" stroke="${color}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" />
      `;

      const ns = "http://www.w3.org/2000/svg";
      const hoverGroup = document.createElementNS(ns, "g");
      hoverGroup.style.display = "none";

      const hoverLine = document.createElementNS(ns, "line");
      hoverLine.setAttribute("stroke", "rgba(240,245,248,0.68)");
      hoverLine.setAttribute("stroke-width", "1.5");
      hoverLine.setAttribute("stroke-dasharray", "4 4");

      const hoverDot = document.createElementNS(ns, "circle");
      hoverDot.setAttribute("r", "6.5");
      hoverDot.setAttribute("fill", color);
      hoverDot.setAttribute("stroke", "rgba(240,245,248,0.92)");
      hoverDot.setAttribute("stroke-width", "2");

      const tipRect = document.createElementNS(ns, "rect");
      tipRect.setAttribute("rx", "8");
      tipRect.setAttribute("ry", "8");
      tipRect.setAttribute("width", "220");
      tipRect.setAttribute("height", "56");
      tipRect.setAttribute("fill", "rgba(235,241,247,0.95)");

      const tipDate = document.createElementNS(ns, "text");
      tipDate.setAttribute("font-size", "14");
      tipDate.setAttribute("font-family", "Space Grotesk, sans-serif");
      tipDate.setAttribute("fill", "#172230");

      const tipValue = document.createElementNS(ns, "text");
      tipValue.setAttribute("font-size", "14");
      tipValue.setAttribute("font-family", "Space Grotesk, sans-serif");
      tipValue.setAttribute("fill", "#172230");

      hoverGroup.appendChild(hoverLine);
      hoverGroup.appendChild(hoverDot);
      hoverGroup.appendChild(tipRect);
      hoverGroup.appendChild(tipDate);
      hoverGroup.appendChild(tipValue);
      svg.appendChild(hoverGroup);

      function formatHoverValue(v) {
        const sign = v > 0 ? "+" : "";
        if (asPercent) return `${sign}${v.toFixed(2)}%`;
        return `${sign}${v.toFixed(2)} USD`;
      }

      function showAtIndex(index) {
        const clamped = Math.max(0, Math.min(values.length - 1, index));
        const px = toX(clamped);
        const py = toY(values[clamped]);
        const pDate = String((points[clamped] && points[clamped].date) || "-");
        const pValue = Number((points[clamped] && points[clamped][valueKey]) || 0);

        hoverLine.setAttribute("x1", String(px));
        hoverLine.setAttribute("x2", String(px));
        hoverLine.setAttribute("y1", String(padY));
        hoverLine.setAttribute("y2", String(height - padY));

        hoverDot.setAttribute("cx", String(px));
        hoverDot.setAttribute("cy", String(py));

        let tx = px + 12;
        let ty = py - 62;
        const tw = 220;
        const th = 56;
        if (tx + tw > width - 6) tx = px - tw - 12;
        if (tx < 6) tx = 6;
        if (ty < 6) ty = py + 10;
        if (ty + th > height - 6) ty = height - th - 6;

        tipRect.setAttribute("x", String(tx));
        tipRect.setAttribute("y", String(ty));
        tipDate.setAttribute("x", String(tx + 10));
        tipDate.setAttribute("y", String(ty + 21));
        tipDate.textContent = pDate;
        tipValue.setAttribute("x", String(tx + 10));
        tipValue.setAttribute("y", String(ty + 42));
        tipValue.textContent = `${seriesLabel}: ${formatHoverValue(pValue)}`;

        hoverGroup.style.display = "block";
      }

      svg.addEventListener("mousemove", (evt) => {
        const rect = svg.getBoundingClientRect();
        if (!rect.width || !rect.height) return;
        const xSvg = ((evt.clientX - rect.left) / rect.width) * width;
        const step = innerW / Math.max(1, values.length - 1);
        const idx = Math.round((xSvg - padX) / Math.max(step, 1e-9));
        showAtIndex(idx);
      });

      svg.addEventListener("mouseleave", () => {
        hoverGroup.style.display = "none";
      });

      const first = values[0];
      const last = values[values.length - 1];
      const delta = last - first;
      const suffix = asPercent ? "%" : "";
      const sign = delta > 0 ? "+" : "";
      if (caption) {
        caption.textContent = `Início ${first.toFixed(2)}${suffix} → Atual ${last.toFixed(2)}${suffix} | Δ ${sign}${delta.toFixed(2)}${suffix}`;
        caption.className = `chart-caption ${delta >= 0 ? "good" : "bad"}`;
      }
    }

    function applyAnalyticsToUI(analytics, rate) {
      if (!analytics || typeof analytics !== "object") return;
      const analyticsPnl = analytics.pnl || {};
      const analyticsSummary = analytics.summary || {};

      const periodUsd = Number(analyticsPnl.period_usd || 0);
      const periodDays = Math.max(1, Number(analytics.window_days || 1));
      const avgDayUsd = periodUsd / periodDays;
      const dailySeries = Array.isArray(analytics.daily_series) ? analytics.daily_series : [];
      const bestDay = dailySeries.reduce((acc, item) => {
        const val = Number((item && item.pnl) || 0);
        return val > acc.pnl ? { date: item.date || "-", pnl: val } : acc;
      }, { date: "-", pnl: Number.NEGATIVE_INFINITY });
      const worstDay = dailySeries.reduce((acc, item) => {
        const val = Number((item && item.pnl) || 0);
        return val < acc.pnl ? { date: item.date || "-", pnl: val } : acc;
      }, { date: "-", pnl: Number.POSITIVE_INFINITY });

      const bestDayUsd = (bestDay.pnl === Number.NEGATIVE_INFINITY) ? 0 : bestDay.pnl;
      const worstDayUsd = (worstDay.pnl === Number.POSITIVE_INFINITY) ? 0 : worstDay.pnl;
      const lifeUsd = Number(analyticsPnl.lifetime_usd || 0);

      const statToday = document.getElementById("stat-today");
      const stat7d = document.getElementById("stat-7d");
      const stat30d = document.getElementById("stat-30d");
      const statLife = document.getElementById("stat-life");

      statToday.textContent = formatMoney(periodUsd, rate, 2);
      stat7d.textContent = formatMoney(avgDayUsd, rate, 2);
      stat30d.textContent = formatMoney(bestDayUsd, rate, 2);
      statLife.textContent = formatMoney(lifeUsd, rate, 2);

      setClassByValue(statToday, periodUsd);
      setClassByValue(stat7d, avgDayUsd);
      setClassByValue(stat30d, bestDayUsd);
      setClassByValue(statLife, lifeUsd);

      document.getElementById("mini-title-1").textContent = `Período (${periodDays}d)`;
      document.getElementById("mini-title-2").textContent = "Média / dia";
      document.getElementById("mini-title-3").textContent = "Melhor dia";
      document.getElementById("mini-title-4").textContent = "Lifetime";

      document.getElementById("stat-today-pct").textContent = `${Number(analyticsPnl.period_pct || 0).toFixed(2)}%`;
      document.getElementById("stat-7d-pct").textContent = `${(avgDayUsd >= 0 ? "+" : "")}${avgDayUsd.toFixed(2)} USD/dia`;
      document.getElementById("stat-30d-pct").textContent = `${bestDay.date || "-"} | pior: ${worstDayUsd.toFixed(2)} USD`;
      document.getElementById("stat-life-pct").textContent = `${Number(analyticsPnl.lifetime_pct || 0).toFixed(2)}%`;

      document.getElementById("sum-profit").textContent = formatMoney(analyticsSummary.total_profit_usd || 0, rate, 2);
      document.getElementById("sum-loss").textContent = formatMoney(analyticsSummary.total_loss_usd || 0, rate, 2);
      document.getElementById("sum-win-days").textContent = String(analyticsSummary.winning_days || 0);
      document.getElementById("sum-loss-days").textContent = String(analyticsSummary.losing_days || 0);
      document.getElementById("sum-flat-days").textContent = String(analyticsSummary.breakeven_days || 0);
      const ratio = analyticsSummary.profit_loss_ratio;
      document.getElementById("sum-ratio").textContent = (ratio === null || ratio === undefined) ? "-" : Number(ratio).toFixed(2);

      const periodStart = String(analytics.start_date || rangeState.start || "-");
      const periodEnd = String(analytics.end_date || rangeState.end || "-");

      document.getElementById("period-pnl-title").textContent = `P&L Período (${periodStart} → ${periodEnd})`;
      document.getElementById("period-trades-title").textContent = "Trades (Período)";
      const periodNet = Number(analyticsSummary.net_after_costs_usd || 0);
      const periodRealized = Number(analyticsSummary.realized_total_usd || 0);
      const periodFunding = Number(analyticsSummary.funding_total_usd || 0);
      const periodCommission = Number(analyticsSummary.commission_total_usd || 0);
      const dailyEl = document.getElementById("daily-total");
      dailyEl.textContent = formatMoney(periodNet, rate, 2);
      setClassByValue(dailyEl, periodNet);
      document.getElementById("daily-breakdown").textContent =
        `Real: ${formatPlain(periodRealized)} | Funding: ${formatPlain(periodFunding)} | Comissão: ${formatPlain(periodCommission)}`;

      document.getElementById("closed-trades").textContent = String(analyticsSummary.trades_total_count || 0);
      document.getElementById("win-loss").textContent =
        `Wins ${analyticsSummary.trades_win_count || 0} | Losses ${analyticsSummary.trades_loss_count || 0}`;

      document.getElementById("calendar-month").textContent = `${periodStart} → ${periodEnd}`;
      renderCalendar(analytics.daily_series || [], rate);
      drawLineChart(
        "chart-cum-usd",
        "chart-cum-usd-caption",
        analytics.cumulative_usd || [],
        "#f8c14b",
        "value",
        false,
        "Cumulative PNL"
      );
      drawLineChart(
        "chart-cum-pct",
        "chart-cum-pct-caption",
        analytics.cumulative_pct || [],
        "#47d3ff",
        "value",
        true,
        "Cumulative PNL %"
      );
    }

    async function refreshAnalytics() {
      const params = new URLSearchParams(window.location.search);
      const token = params.get("token") || "";
      const apiUrl = buildApiUrl("/api/dashboard/analytics", true);
      const errorsEl = document.getElementById("errors");
      const button = document.getElementById("refresh-analytics");

      try {
        button.disabled = true;
        button.textContent = "Atualizando...";

        const response = await fetch(apiUrl, {
          cache: "no-store",
          headers: tokenHeaders(token)
        });
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }

        const data = await response.json();
        const rate = Number((data.fx && data.fx.usd_brl) || 5.0);
        analyticsState = data.analytics || null;
        applyAnalyticsToUI(analyticsState, rate);

        const errorList = Array.isArray(data.errors) ? data.errors : [];
        if (errorList.length > 0) {
          errorsEl.style.display = "block";
          errorsEl.innerHTML = "<strong>Atenção:</strong><br>" + errorList.map(escapeHtml).join("<br>");
        } else {
          errorsEl.style.display = "none";
          errorsEl.textContent = "";
        }
      } catch (err) {
        errorsEl.style.display = "block";
        errorsEl.textContent = `Erro ao atualizar análise: ${err}`;
      } finally {
        button.disabled = false;
        button.textContent = "Atualizar análise";
      }
    }

    async function refreshDashboard() {
      const params = new URLSearchParams(window.location.search);
      const token = params.get("token") || "";
      const apiUrl = buildApiUrl("/api/dashboard", false);
      const errorsEl = document.getElementById("errors");

      try {
        const response = await fetch(apiUrl, {
          cache: "no-store",
          headers: tokenHeaders(token)
        });
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }
        const data = await response.json();
        const rate = Number((data.fx && data.fx.usd_brl) || 5.0);

        const env = data.environment || {};
        const runtime = data.runtime || {};
        const account = data.account || {};
        const daily = data.daily || {};
        const positionsSummary = data.positions_summary || {};
        const trades = data.trades || {};
        const health = data.health || {};
        const api = health.api || {};
        const orders = health.orders || {};
        const risk = data.risk || {};
        const positions = Array.isArray(data.positions) ? data.positions : [];

        const wallet = Number(account.wallet_balance || 0);
        const available = Number(account.available_balance || 0);
        const dailyNet = Number(daily.total || 0);
        const dailyRealized = Number(daily.realized_pnl || 0);
        const dailyFunding = Number(daily.funding_fee || 0);
        const dailyCommission = Number(daily.commission || 0);
        const openPnl = Number(account.unrealized_pnl || 0);

        const statusPill = document.getElementById("status-pill");
        const alive = Boolean(runtime.bot_alive_guess);
        const testnet = Boolean(env.use_testnet);
        statusPill.textContent = `${alive ? "BOT ATIVO" : "BOT POSSIVELMENTE PARADO"} | ${testnet ? "TESTNET" : "MAINNET"}`;
        statusPill.style.color = alive ? "#a7f3c2" : "#ffd1ca";
        statusPill.style.borderColor = alive ? "rgba(63,226,127,.55)" : "rgba(255,111,97,.55)";

        const walletEl = document.getElementById("wallet-balance");
        walletEl.textContent = formatMoney(wallet, rate, 2);
        setClassByValue(walletEl, wallet);
        document.getElementById("available-balance").textContent = `Disponível ${formatMoney(available, rate, 2)}`;

        const dailyEl = document.getElementById("daily-total");
        dailyEl.textContent = formatMoney(dailyNet, rate, 2);
        setClassByValue(dailyEl, dailyNet);
        document.getElementById("daily-breakdown").textContent =
          `Real: ${formatPlain(dailyRealized)} | Funding: ${formatPlain(dailyFunding)} | Comissão: ${formatPlain(dailyCommission)}`;

        const openEl = document.getElementById("open-pnl");
        openEl.textContent = formatMoney(openPnl, rate, 2);
        setClassByValue(openEl, openPnl);
        document.getElementById("positions-count").textContent =
          `${positionsSummary.count || 0} abertas (${positionsSummary.long_count || 0} LONG / ${positionsSummary.short_count || 0} SHORT)`;

        document.getElementById("closed-trades").textContent = String(trades.closed_trades_count || 0);
        document.getElementById("win-loss").textContent =
          `Wins ${trades.trades_win_count || 0} | Losses ${trades.trades_loss_count || 0}`;

        document.getElementById("api-calls").textContent = String(api.calls || 0);
        document.getElementById("api-failures").textContent = String(api.failures || 0);
        document.getElementById("api-retries").textContent = String(api.retries || 0);
        document.getElementById("order-failures").textContent = String(orders.failures || 0);
        document.getElementById("order-rejections").textContent = String(orders.rejections || 0);

        document.getElementById("risk-sl").textContent =
          `${risk.stop_loss_enabled ? "ON" : "OFF"} (${Number(risk.stop_loss_percent || 0).toFixed(2)}%)`;
        document.getElementById("risk-tp").textContent = `${Number(risk.take_profit_percent || 0).toFixed(2)}%`;
        document.getElementById("risk-trailing").textContent =
          `${Number(risk.trailing_activation_percent || 0).toFixed(2)} / ${Number(risk.trailing_distance_percent || 0).toFixed(2)}%`;
        document.getElementById("risk-trailing-active").textContent = String(positionsSummary.trailing_active_count || 0);
        document.getElementById("risk-notional").textContent = formatMoney(positionsSummary.total_notional || 0, rate, 2);

        const tbody = document.getElementById("positions-body");
        if (positions.length === 0) {
          tbody.innerHTML = `<tr><td colspan="8" class="muted">Sem posições abertas no momento.</td></tr>`;
        } else {
          tbody.innerHTML = positions.map((pos) => {
            const pnl = Number(pos.unrealized_pnl || 0);
            const roi = Number(pos.roi_percent || 0);
            const side = String(pos.side || "");
            const sideClass = side === "LONG" ? "long" : "short";
            return `
              <tr>
                <td><strong>${escapeHtml(pos.symbol || "-")}</strong></td>
                <td><span class="tag ${sideClass}">${escapeHtml(side)}</span></td>
                <td>${Number(pos.quantity || 0).toFixed(4)}</td>
                <td>${Number(pos.entry_price || 0).toFixed(5)}</td>
                <td>${Number(pos.mark_price || 0).toFixed(5)}</td>
                <td class="${pnl >= 0 ? "good" : "bad"}">${formatMoney(pnl, rate, 2)}</td>
                <td class="${roi >= 0 ? "good" : "bad"}">${roi.toFixed(2)}%</td>
                <td>${pos.trailing_active ? "ON" : "OFF"}</td>
              </tr>
            `;
          }).join("");
        }

        renderHistory(trades.history_points || [], rate);

        if (analyticsState) {
          applyAnalyticsToUI(analyticsState, rate);
        }

        const errorList = Array.isArray(data.errors) ? data.errors : [];
        if (errorList.length > 0) {
          errorsEl.style.display = "block";
          errorsEl.innerHTML = "<strong>Atenção:</strong><br>" + errorList.map(escapeHtml).join("<br>");
        } else {
          errorsEl.style.display = "none";
          errorsEl.textContent = "";
        }

        document.getElementById("footer").textContent =
          `Atualizado: ${new Date().toLocaleString("pt-BR")} | refresh ${REFRESH_SECONDS}s | env ${env.app_env || "-"}`;
      } catch (err) {
        errorsEl.style.display = "block";
        errorsEl.textContent = `Erro ao carregar dashboard: ${err}`;
      }
    }

    if (TOKEN_REQUIRED && !new URLSearchParams(window.location.search).get("token")) {
      const errorsEl = document.getElementById("errors");
      errorsEl.style.display = "block";
      errorsEl.textContent = "Token obrigatório. Abra a URL com ?token=SEU_TOKEN";
    }

    setupRangeControls();
    refreshDashboard();
    setInterval(refreshDashboard, REFRESH_SECONDS * 1000);
  </script>
</body>
</html>
"""


def _build_html(refresh_seconds: int, token_required: bool) -> str:
    html = _DASHBOARD_HTML_TEMPLATE.replace("__REFRESH_SECONDS__", str(refresh_seconds))
    html = html.replace("__TOKEN_REQUIRED__", "true" if token_required else "false")
    return html


def _build_handler(
    collector: DashboardDataCollector,
    refresh_seconds: int,
    auth_token: str,
):
    class DashboardHandler(BaseHTTPRequestHandler):
        def _write_json(self, status: HTTPStatus, payload: Dict[str, Any]):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _write_html(self, status: HTTPStatus, html: str):
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _extract_token(self, query: Dict[str, List[str]]) -> str:
            query_token = (query.get("token") or [""])[0]
            if query_token:
                return query_token

            header_token = self.headers.get("X-Auth-Token", "")
            if header_token:
                return header_token

            auth_header = self.headers.get("Authorization", "")
            if auth_header.lower().startswith("bearer "):
                return auth_header[7:].strip()
            return ""

        def _is_authorized(self, query: Dict[str, List[str]]) -> bool:
            if not auth_token:
                return True
            provided = self._extract_token(query)
            return provided == auth_token

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            if path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return

            if not self._is_authorized(query):
                if path.startswith("/api/"):
                    self._write_json(
                        HTTPStatus.UNAUTHORIZED,
                        {"ok": False, "error": "unauthorized"},
                    )
                else:
                    self._write_html(
                        HTTPStatus.UNAUTHORIZED,
                        "<h1>401 unauthorized</h1><p>Adicione o token: ?token=SEU_TOKEN</p>",
                    )
                return

            if path == "/":
                self._write_html(
                    HTTPStatus.OK,
                    _build_html(refresh_seconds=refresh_seconds, token_required=bool(auth_token)),
                )
                return

            if path == "/api/healthz":
                self._write_json(HTTPStatus.OK, {"ok": True})
                return

            if path == "/api/dashboard":
                payload = collector.collect(include_analytics=False)
                self._write_json(HTTPStatus.OK, payload)
                return

            if path == "/api/dashboard/analytics":
                start = (query.get("start") or [""])[0]
                end = (query.get("end") or [""])[0]
                payload = collector.collect(
                    start_date_str=start,
                    end_date_str=end,
                    include_analytics=True,
                )
                self._write_json(HTTPStatus.OK, payload)
                return

            self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

        # Reduz ruído no terminal, mantendo log em INFO para requests úteis.
        def log_message(self, fmt: str, *args):  # pragma: no cover - só IO de runtime
            logger.info("dashboard %s - %s", self.address_string(), fmt % args)

    return DashboardHandler


def run_dashboard_server(
    host: str,
    port: int,
    refresh_seconds: int,
    auth_token: str = "",
):
    collector = DashboardDataCollector()
    handler_cls = _build_handler(
        collector=collector,
        refresh_seconds=refresh_seconds,
        auth_token=auth_token,
    )
    server = ThreadingHTTPServer((host, port), handler_cls)

    mode = "TESTNET" if config.USE_TESTNET else "MAINNET"
    logger.info("🌐 Dashboard ativo em http://%s:%s", host, port)
    logger.info("🔒 Token auth: %s", "ON" if auth_token else "OFF")
    logger.info("📡 Binance mode: %s", mode)

    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - controle manual
        logger.info("Encerrando dashboard...")
    finally:
        server.server_close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dashboard web read-only do Trading Bot")
    parser.add_argument("--host", default=config.DASHBOARD_HOST, help="Host de bind do servidor")
    parser.add_argument("--port", type=int, default=config.DASHBOARD_PORT, help="Porta HTTP")
    parser.add_argument(
        "--refresh-seconds",
        type=int,
        default=config.DASHBOARD_REFRESH_SECONDS,
        help="Intervalo de refresh da UI em segundos",
    )
    parser.add_argument(
        "--token",
        default=config.DASHBOARD_AUTH_TOKEN,
        help="Token opcional para proteger o acesso",
    )
    return parser


def main():
    parser = _build_parser()
    args = parser.parse_args()

    refresh_seconds = max(2, min(int(args.refresh_seconds), 300))
    port = max(1, min(int(args.port), 65535))

    logging.basicConfig(
        level=getattr(logging, str(config.LOG_LEVEL).upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    run_dashboard_server(
        host=str(args.host).strip() or "127.0.0.1",
        port=port,
        refresh_seconds=refresh_seconds,
        auth_token=str(args.token or "").strip(),
    )


if __name__ == "__main__":
    main()

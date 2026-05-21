"""
Coleta de dados do bot para o dashboard. Lê estruturas in-memory do
TradingBot respeitando os locks existentes (_positions_lock, _state_io_lock).

Princípio: não fazer chamadas pesadas à Binance aqui — apenas LER o que o
bot já cacheou. O loop principal do bot é quem alimenta esses caches. Se
um dado não estiver disponível (ex: posições nunca foram listadas ainda),
retornamos campos vazios em vez de bloquear.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from ..core.config import config


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _iso(dt: Any) -> Optional[str]:
    if isinstance(dt, datetime):
        return dt.isoformat()
    if isinstance(dt, str):
        return dt
    return None


def collect_snapshot(bot) -> Dict[str, Any]:
    """Snapshot completo para a primeira carga e fallback de polling."""
    return {
        "summary": collect_summary(bot),
        "positions": collect_positions(bot),
        "recent_trades": collect_recent_trades(bot, limit=20),
        "regime": collect_regime(bot),
        "portfolio_history": collect_portfolio_history(bot, limit=200),
        "server_time": datetime.utcnow().isoformat() + "Z",
    }


def collect_summary(bot) -> Dict[str, Any]:
    """KPIs do topo da página."""
    initial_capital = _safe_float(getattr(bot, "initial_capital", 0.0))
    total_pnl = _safe_float(getattr(bot, "total_pnl", 0.0))
    daily_pnl = _safe_float(getattr(bot, "daily_realized_pnl", 0.0))
    closed_trades = int(getattr(bot, "closed_trades_count", 0) or 0)
    last_balance = _safe_float(getattr(bot, "last_known_balance", 0.0) or 0.0)
    paused = bool(getattr(bot, "paused", False))
    running = bool(getattr(bot, "running", False))

    # ROI desde initial_capital (se temos valor de inicial coerente)
    if initial_capital > 0:
        roi_percent = (total_pnl / initial_capital) * 100.0
    else:
        roi_percent = 0.0

    return {
        "initial_capital": initial_capital,
        "last_balance": last_balance,
        "total_pnl": total_pnl,
        "daily_pnl": daily_pnl,
        "roi_percent": round(roi_percent, 4),
        "closed_trades": closed_trades,
        "paused": paused,
        "running": running,
        "environment": "testnet" if getattr(config, "USE_TESTNET", False) else "mainnet",
        "ai_mode": str(getattr(config, "AI_CONSULTIVE_MODE", "off") or "off"),
    }


def collect_positions(bot) -> List[Dict[str, Any]]:
    """Posições conhecidas (snapshot sob o lock do bot)."""
    positions: List[Dict[str, Any]] = []
    lock = getattr(bot, "_positions_lock", None)
    known = getattr(bot, "known_positions", {}) or {}

    if lock is not None:
        try:
            with lock:
                snapshot = dict(known)
        except Exception:
            snapshot = dict(known)
    else:
        snapshot = dict(known)

    for position_key, payload in snapshot.items():
        if not isinstance(payload, dict):
            continue
        positions.append(
            {
                "key": position_key,
                "symbol": payload.get("symbol", ""),
                "side": payload.get("side", ""),
                "entry_price": _safe_float(payload.get("entry_price")),
                "quantity": _safe_float(payload.get("quantity")),
                "strategy_name": payload.get("strategy_name", "primary"),
                "strategy_type": payload.get("strategy_type", "trend_signal"),
                "custom_stop_loss": payload.get("custom_stop_loss"),
                "custom_take_profit": payload.get("custom_take_profit"),
                "trailing_activation_pct": payload.get("trailing_activation_pct"),
                "trailing_distance_pct": payload.get("trailing_distance_pct"),
                "last_seen": _iso(payload.get("last_seen")),
                "entry_time": _iso(payload.get("entry_time")),
            }
        )

    positions.sort(key=lambda p: (p["symbol"], p["side"]))
    return positions


def collect_recent_trades(bot, limit: int = 20) -> List[Dict[str, Any]]:
    """Últimos trades fechados (do trade_history do bot)."""
    history = getattr(bot, "trade_history", []) or []
    out: List[Dict[str, Any]] = []
    for entry in history[-limit:]:
        if not isinstance(entry, dict):
            continue
        out.append(
            {
                "timestamp": _iso(entry.get("timestamp")) or entry.get("timestamp"),
                "symbol": entry.get("symbol", ""),
                "side": entry.get("side", ""),
                "signal": entry.get("signal", ""),
                "entry_price": _safe_float(entry.get("entry_price")),
                "exit_price": _safe_float(entry.get("exit_price")),
                "pnl_net": _safe_float(entry.get("pnl_net")),
                "pnl_gross": _safe_float(entry.get("pnl_gross")),
                "fees": _safe_float(entry.get("fees")),
                "strategy_name": entry.get("strategy_name", "primary"),
                "close_reason": entry.get("close_reason", ""),
            }
        )
    # Mais recentes primeiro
    out.reverse()
    return out


def collect_regime(bot) -> Dict[str, Any]:
    """Estado atual do regime classifier por símbolo."""
    committed = dict(getattr(bot, "_regime_committed", {}) or {})
    observations = {
        sym: list(window or [])
        for sym, window in (getattr(bot, "_regime_observations", {}) or {}).items()
    }
    return {
        "committed": committed,
        "observations": observations,
        "enabled": bool(getattr(config, "REGIME_CLASSIFIER_ENABLED", False)),
    }


def collect_portfolio_history(bot, limit: int = 200) -> List[Dict[str, Any]]:
    """Série de equity para o gráfico — últimos N snapshots."""
    history = getattr(bot, "portfolio_history", []) or []
    out: List[Dict[str, Any]] = []
    for snap in history[-limit:]:
        if not isinstance(snap, dict):
            continue
        out.append(
            {
                "timestamp": _iso(snap.get("timestamp")) or snap.get("timestamp"),
                "balance": _safe_float(snap.get("balance")),
                "pnl_realized": _safe_float(snap.get("pnl_realized")),
                "pnl_unrealized": _safe_float(snap.get("pnl_unrealized")),
                "pnl_total": _safe_float(snap.get("pnl_total")),
                "closed_trades": int(snap.get("closed_trades", 0) or 0),
            }
        )
    return out

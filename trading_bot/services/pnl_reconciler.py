"""Auto-reconciliação do P&L dos trades fechados com o income real da Binance.

Mesmo após #181/#185, quando o user-stream perde o fill do fechamento o bot cai
no fallback por preço atual e grava P&L FABRICADO (caso #67 ETH 21/06: DB +1,28
vs real −0,67). Este módulo roda periodicamente no loop do bot: para cada trade
fechado recente, casa com os REALIZED_PNL+COMMISSION reais da Binance (por símbolo
+ janela em torno do exit_at) e corrige pnl_gross/fees/pnl_net/exit_price quando
divergem. O DB se cura sozinho — mesma lógica do scripts/backfill_real_pnl.py,
mas embutida e incremental (só a janela recente).

Idempotente: trades já corretos são pulados (delta ≤ MIN_DELTA).
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

WINDOW_BEFORE_S = 15 * 60   # income pode preceder o exit_at detectado
WINDOW_AFTER_S = 5 * 60
MIN_DELTA = 0.01            # só corrige divergência > 1 centavo


def _to_ms(iso: str) -> int:
    d = dt.datetime.fromisoformat(iso)
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp() * 1000)


def _empty(checked: int = 0) -> Dict[str, Any]:
    return {"checked": checked, "corrected": 0, "total_delta": 0.0, "details": []}


def reconcile_recent_pnl(bot, lookback_hours: float = 48.0) -> Dict[str, Any]:
    """Corrige no DB os trades fechados cujo P&L diverge do income real da Binance.

    Retorna {checked, corrected, total_delta, details:[{id,symbol,side,old,new}]}.
    Fail-safe: qualquer erro é logado e retorna o parcial — nunca derruba o loop.
    """
    store = getattr(bot, "trade_store", None)
    exchange = getattr(bot, "exchange", None)
    if store is None or exchange is None:
        return _empty()

    trades = store.closed_trades_since(lookback_hours=lookback_hours)
    if not trades:
        return _empty()

    # Janela de income a buscar: do trade mais antigo (− buffer) até agora.
    try:
        oldest = min(_to_ms(t["opened_at"] or t["exit_at"]) for t in trades)
    except Exception:
        logger.exception("🔁 reconcile: exit_at/opened_at inválido")
        return _empty(len(trades))
    start_ms = oldest - WINDOW_BEFORE_S * 1000

    try:
        income = exchange.get_income_history(start_time=start_ms, limit=1000) or []
    except Exception:
        logger.exception("🔁 reconcile: falha ao buscar income da Binance")
        return _empty(len(trades))

    realized = [i for i in income if i.get("incomeType") == "REALIZED_PNL"]
    commission = [i for i in income if i.get("incomeType") == "COMMISSION"]
    used_r: set = set()
    used_c: set = set()

    def claim(pool: List[dict], used: set, symbol: str, center_ms: int):
        """Soma e consome os registros do símbolo na janela em torno do exit."""
        total = 0.0
        hits = 0
        for idx, r in enumerate(pool):
            if idx in used or r.get("symbol") != symbol:
                continue
            t = int(r["time"])
            if center_ms - WINDOW_BEFORE_S * 1000 <= t <= center_ms + WINDOW_AFTER_S * 1000:
                total += float(r["income"])
                used.add(idx)
                hits += 1
        return total, hits

    corrected = 0
    total_delta = 0.0
    details: List[Dict[str, Any]] = []

    # Ordena por exit_at p/ consumo determinístico (igual ao backfill).
    for tr in sorted(trades, key=lambda x: x.get("exit_at") or ""):
        if not tr.get("exit_at"):
            continue
        center = _to_ms(tr["exit_at"])
        gross, gh = claim(realized, used_r, tr["symbol"], center)
        if gh == 0:
            continue  # sem income casável (latência) → deixa intacto p/ próxima rodada
        comm, _ = claim(commission, used_c, tr["symbol"], center)
        net = gross + comm  # comm é negativo
        old_net = float(tr.get("pnl_net") or 0.0)
        if abs(net - old_net) <= MIN_DELTA:
            continue  # já está correto

        qty = tr.get("qty") or 0
        exit_price = None
        if qty and tr.get("side") and tr.get("entry_price"):
            move = gross / qty
            exit_price = tr["entry_price"] + (move if tr["side"] == "LONG" else -move)

        ok = store.update_trade_pnl(
            tr["id"],
            pnl_gross=round(gross, 6),
            fees=round(abs(comm), 6),
            pnl_net=round(net, 6),
            exit_price=exit_price,
        )
        if ok:
            corrected += 1
            total_delta += (net - old_net)
            details.append({
                "id": tr["id"], "symbol": tr["symbol"], "side": tr["side"],
                "old": round(old_net, 4), "new": round(net, 4),
            })

    if corrected:
        logger.warning(
            f"🔁 Reconciliação P&L: {corrected}/{len(trades)} trades corrigidos "
            f"(Δnet {total_delta:+.2f}) — fabricação ao vivo detectada e curada."
        )
    else:
        logger.debug(f"🔁 Reconciliação P&L: {len(trades)} trades OK, nada a corrigir.")

    return {
        "checked": len(trades),
        "corrected": corrected,
        "total_delta": round(total_delta, 4),
        "details": details,
    }

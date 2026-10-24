"""Auto-reconciliação do P&L dos trades fechados com os fills reais da Binance.

Quando o user-stream perde o fill do fechamento o bot cai no fallback por preço
atual e grava P&L FABRICADO (caso #67 ETH 21/06: DB +1,28 vs real −0,67). Este
módulo roda periodicamente no loop do bot e corrige pnl_gross/fees/funding/
pnl_net/exit_price de cada trade fechado recente a partir da Binance. A mesma
atribuição é usada pelo scripts/backfill_real_pnl.py no histórico.

Atribuição exata, sem janela de tempo que roube taxa de outro trade:
  - Os fills (/fapi/v1/userTrades) de cada símbolo+positionSide são cortados em
    round trips: abre a partir de posição zerada, fecha quando a qty volta a 0.
  - Cada trade do DB casa com o round trip cujo 1º fill está a ≤ MATCH_SLACK_S
    do opened_at (mesmo símbolo/lado), cada round trip uma vez só.
  - gross = realizedPnl dos fills de saída; fees = comissão da entrada + saída;
    funding = FUNDING_FEE do símbolo com o round trip aberto.

Definição única de net (DB, dashboard, kill switch, P&L de hoje):
  pnl_net = pnl_gross − fees + funding

Idempotente: trades que já batem são pulados.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

MATCH_SLACK_S = 60          # 1º fill observado 1,5-8s antes do opened_at
MIN_DELTA = 0.01            # divergência de gross acima disso = fabricação
EPS = 1e-6                  # valores arredondados a 6 casas
_USER_TRADES_MAX_SPAN_MS = 7 * 24 * 3600 * 1000 - 1   # limite do /userTrades
_PAGE = 1000


def _to_ms(iso: str) -> int:
    # Ingênuo = horário local do processo (legado do datetime.now() na VM).
    return int(dt.datetime.fromisoformat(iso).timestamp() * 1000)


def _empty(checked: int = 0) -> Dict[str, Any]:
    return {"checked": checked, "corrected": 0, "fabricated": 0, "total_delta": 0.0, "details": []}


def round_trips(fills: Iterable[dict]) -> List[Dict[str, Any]]:
    """Corta fills em round trips por (symbol, positionSide).

    Fill de saída sem round trip aberto (posição aberta antes da janela buscada)
    é descartado. Comissão em outro ativo (BNB) deixa a perna desconhecida.
    shortcut: positionSide BOTH (one-way) é ignorado; o bot usa hedge mode.
    """
    by_key: Dict[tuple, List[dict]] = {}
    for f in fills:
        if f.get("positionSide") in ("LONG", "SHORT"):
            by_key.setdefault((f["symbol"], f["positionSide"]), []).append(f)

    trips: List[Dict[str, Any]] = []
    for (symbol, pside), fs in by_key.items():
        open_side = "BUY" if pside == "LONG" else "SELL"
        trip: Optional[Dict[str, Any]] = None
        pos = 0.0
        for f in sorted(fs, key=lambda x: (int(x["time"]), int(x["id"]))):
            qty = float(f["qty"])
            usdt = f.get("commissionAsset", "USDT") == "USDT"
            comm = float(f["commission"])
            if f["side"] == open_side:
                if trip is None:
                    trip = {"symbol": symbol, "side": pside, "start_ms": int(f["time"]),
                            "end_ms": None, "entry_fee": 0.0, "exit_fee": 0.0,
                            "entry_ok": True, "exit_ok": True, "gross": 0.0,
                            "exit_qty": 0.0, "exit_notional": 0.0, "funding": 0.0}
                pos += qty
                trip["entry_fee"] += comm
                trip["entry_ok"] &= usdt
                continue
            if trip is None:
                continue
            pos -= qty
            trip["exit_fee"] += comm
            trip["exit_ok"] &= usdt
            trip["gross"] += float(f["realizedPnl"])
            trip["exit_qty"] += qty
            trip["exit_notional"] += qty * float(f["price"])
            if pos <= 1e-9 * max(1.0, trip["exit_qty"]):
                trip["end_ms"] = int(f["time"])
                trips.append(trip)
                trip, pos = None, 0.0
        if trip is not None:
            trips.append(trip)  # ainda aberto
    return trips


def attribute(
    trades: Iterable[dict], fills: Iterable[dict], funding: Optional[Iterable[dict]]
) -> Dict[int, Dict[str, Any]]:
    """trade id -> {gross, fees, funding, exit_price} reais da Binance.

    Só trades casados com um round trip FECHADO aparecem. `fees` é None quando
    alguma perna é desconhecida (não sobrescrever com match parcial); `funding`
    é None quando o income não pôde ser buscado (funding=None na entrada).
    """
    trips = [t for t in round_trips(fills) if t["end_ms"] is not None]
    if funding is not None:
        for inc in funding:
            if inc.get("incomeType") != "FUNDING_FEE":
                continue
            t_ms = int(inc["time"])
            open_ = [t for t in trips if t["symbol"] == inc.get("symbol")
                     and t["start_ms"] <= t_ms <= t["end_ms"]]
            if len(open_) == 1:
                open_[0]["funding"] += float(inc["income"])
            elif open_:
                # shortcut: LONG e SHORT abertos juntos no símbolo → funding ambíguo, não atribui.
                logger.info(f"🔁 funding {inc.get('symbol')} @ {t_ms} ambíguo (LONG+SHORT), ignorado")

    out: Dict[int, Dict[str, Any]] = {}
    claimed: set = set()
    for tr in sorted(trades, key=lambda x: x.get("opened_at") or ""):
        if not tr.get("opened_at") or not tr.get("exit_at"):
            continue
        o = _to_ms(tr["opened_at"])
        cands = [
            (abs(t["start_ms"] - o), i) for i, t in enumerate(trips)
            if i not in claimed and t["symbol"] == tr["symbol"] and t["side"] == tr["side"]
            and abs(t["start_ms"] - o) <= MATCH_SLACK_S * 1000
        ]
        if not cands:
            continue
        i = min(cands)[1]
        claimed.add(i)
        t = trips[i]
        out[tr["id"]] = {
            "gross": round(t["gross"], 6),
            "fees": round(t["entry_fee"] + t["exit_fee"], 6) if t["entry_ok"] and t["exit_ok"] else None,
            "funding": round(t["funding"], 6) if funding is not None else None,
            "exit_price": t["exit_notional"] / t["exit_qty"] if t["exit_qty"] else None,
        }
    return out


def fetch_fills(exchange, symbols: Iterable[str], start_ms: int, end_ms: int) -> List[dict]:
    """userTrades de cada símbolo em [start_ms, end_ms], em fatias de 7 dias."""
    out: Dict[tuple, dict] = {}
    for sym in sorted(set(symbols)):
        st = start_ms
        while st <= end_ms:
            en = min(st + _USER_TRADES_MAX_SPAN_MS, end_ms)
            rows = exchange.get_account_trades(symbol=sym, start_time=st, end_time=en, limit=_PAGE) or []
            new = [r for r in rows if (sym, r["id"]) not in out]
            out.update(((sym, r["id"]), r) for r in new)
            # Página cheia retoma no ms do último fill (inclusive; dedup por id).
            st = int(rows[-1]["time"]) if len(rows) >= _PAGE and new else en + 1
    return list(out.values())


def fetch_funding(exchange, start_ms: int) -> List[dict]:
    """FUNDING_FEE desde start_ms, paginado. Propaga erro (não confundir falha com 'sem funding')."""
    out: List[dict] = []
    st = start_ms
    while True:
        rows = exchange.get_income_history(
            income_type="FUNDING_FEE", start_time=st, limit=_PAGE, raise_errors=True
        ) or []
        out.extend(rows)
        if len(rows) < _PAGE:
            return out
        st = int(rows[-1]["time"]) + 1


def plan_update(tr: dict, real: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Novos valores do trade (ou None se já bate). Perna/funding desconhecidos mantêm o DB."""
    old_gross = float(tr.get("pnl_gross") or 0.0)
    old_fees = float(tr.get("fees") or 0.0)
    old_funding = float(tr.get("funding") or 0.0)
    old_net = float(tr.get("pnl_net") or 0.0)
    gross = real["gross"]
    fees = real["fees"] if real["fees"] is not None else old_fees
    funding = real["funding"] if real["funding"] is not None else tr.get("funding")
    net = round(gross - fees + float(funding or 0.0), 6)
    if all(abs(a - b) <= EPS for a, b in (
        (gross, old_gross), (fees, old_fees), (float(funding or 0.0), old_funding), (net, old_net),
    )):
        return None
    return {
        "pnl_gross": gross, "fees": fees, "funding": funding, "pnl_net": net,
        "exit_price": real["exit_price"], "old_net": old_net,
        "gross_fixed": abs(gross - old_gross) > MIN_DELTA,
    }


def reconcile_recent_pnl(bot, lookback_hours: float = 48.0) -> Dict[str, Any]:
    """Corrige no DB os trades fechados cujo P&L diverge dos fills reais da Binance.

    Retorna {checked, corrected, fabricated, total_delta,
    details:[{id,symbol,side,old,new,gross_fixed}]}; `fabricated` conta só os
    trades com gross divergente (fabricação), não ajustes de taxa/funding.
    Fail-safe: qualquer erro é logado e retorna o parcial, nunca derruba o loop.
    """
    store = getattr(bot, "trade_store", None)
    exchange = getattr(bot, "exchange", None)
    if store is None or exchange is None:
        return _empty()

    trades = store.closed_trades_since(lookback_hours=lookback_hours)
    if not trades:
        return _empty()

    try:
        start_ms = min(_to_ms(t["opened_at"] or t["exit_at"]) for t in trades) - MATCH_SLACK_S * 1000
        end_ms = max(_to_ms(t["exit_at"]) for t in trades) + MATCH_SLACK_S * 1000
    except Exception:
        logger.exception("🔁 reconcile: exit_at/opened_at inválido")
        return _empty(len(trades))

    try:
        fills = fetch_fills(exchange, (t["symbol"] for t in trades), start_ms, end_ms)
    except Exception:
        logger.exception("🔁 reconcile: falha ao buscar fills da Binance")
        return _empty(len(trades))
    try:
        funding: Optional[List[dict]] = fetch_funding(exchange, start_ms)
    except Exception:
        logger.warning("🔁 reconcile: falha ao buscar funding; mantém o funding gravado", exc_info=True)
        funding = None

    real = attribute(trades, fills, funding)
    result = _empty(len(trades))
    for tr in trades:
        if tr["id"] not in real:
            continue  # sem round trip casável (latência) → próxima rodada
        plan = plan_update(tr, real[tr["id"]])
        if plan is None:
            continue
        ok = store.update_trade_pnl(
            tr["id"], pnl_gross=plan["pnl_gross"], fees=plan["fees"],
            funding=plan["funding"], pnl_net=plan["pnl_net"], exit_price=plan["exit_price"],
        )
        if not ok:
            continue
        result["corrected"] += 1
        result["fabricated"] += plan["gross_fixed"]
        result["total_delta"] += plan["pnl_net"] - plan["old_net"]
        result["details"].append({
            "id": tr["id"], "symbol": tr["symbol"], "side": tr["side"],
            "old": round(plan["old_net"], 4), "new": round(plan["pnl_net"], 4),
            "gross_fixed": plan["gross_fixed"],
        })
    result["total_delta"] = round(result["total_delta"], 4)

    if result["fabricated"]:
        logger.warning(
            f"🔁 Reconciliação P&L: {result['fabricated']}/{len(trades)} trades com gross "
            f"divergente da Binance (Δnet total {result['total_delta']:+.2f}) — "
            "fabricação ao vivo detectada e curada."
        )
    elif result["corrected"]:
        logger.info(
            f"🔁 Reconciliação P&L: {result['corrected']} trades com taxa/funding ajustados "
            f"(Δnet {result['total_delta']:+.4f})."
        )
    else:
        logger.debug(f"🔁 Reconciliação P&L: {len(trades)} trades OK, nada a corrigir.")
    return result

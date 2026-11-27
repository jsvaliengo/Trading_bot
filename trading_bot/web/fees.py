"""
Aba "Taxas" da Análise do P&L — /api/fees.

O que a exchange cobra AGORA desta conta nos contratos que o bot opera: maker/taker por par e
funding atual + próximo horário. Taxa efetiva da conta (com VIP/desconto) quando o cliente expõe
`get_account_fee_rates` (Bitget: /api/v2/common/trade-rate, autenticado); senão a padrão do contrato
(`get_commission_rates`), rotulada como tal. Cache em memória de 5 min por par (taxa muda raro,
funding a cada ~8h); em falha devolve o último valor conhecido com aviso, nunca derruba a página.
O comparativo com o realizado (fees pagos ÷ volume) é feito no front com o /api/pnl_report já carregado.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..core.config import config

logger = logging.getLogger(__name__)

TTL_S = 300.0
_memo: Dict[Tuple[int, str], Tuple[float, Dict[str, Any]]] = {}  # (id(exchange), símbolo) → (monotonic, linha)


def active_symbols(bot) -> List[str]:
    """Pares do set atual (TRADING_PAIRS/MARTINGALE_PAIRS) ∪ pares com posição aberta (known_positions)."""
    syms = set()
    if getattr(bot, "trade_store", None) is not None:  # config é do bot deste processo, não de conta de terceiro
        for key in ("TRADING_PAIRS", "MARTINGALE_PAIRS"):
            syms.update(str(p).upper() for p in (getattr(config, key, None) or []))
    for pk in (getattr(bot, "known_positions", {}) or {}).keys():
        if isinstance(pk, str) and "_" in pk:
            syms.add(pk.rsplit("_", 1)[0].upper())
    return sorted(s for s in syms if s)


def _fetch(exchange, symbol: str) -> Dict[str, Any]:
    row: Dict[str, Any] = {"symbol": symbol, "maker_pct": None, "taker_pct": None, "fee_source": None,
                           "funding_pct": None, "next_funding": None}
    if hasattr(exchange, "get_account_fee_rates"):
        try:
            r = exchange.get_account_fee_rates(symbol)
            row.update(maker_pct=r["maker_rate"] * 100, taker_pct=r["taker_rate"] * 100, fee_source="conta")
        except Exception as exc:
            logger.warning(f"Taxa efetiva da conta em {symbol} indisponível ({exc}); usando a padrão do contrato")
    if row["fee_source"] is None:
        r = exchange.get_commission_rates(symbol)  # clientes já caem no default interno em falha
        row.update(maker_pct=r["maker_rate"] * 100, taker_pct=r["taker_rate"] * 100, fee_source="contrato")
    f = exchange.get_funding_rate(symbol)
    nxt = f.get("next_funding_time")
    row.update(funding_pct=f.get("rate_percent"), next_funding=nxt.isoformat() if isinstance(nxt, datetime) else None,
               at=datetime.now(timezone.utc).replace(microsecond=0).isoformat())
    logger.info(f"Taxas de {symbol} via {row['fee_source']}: maker {row['maker_pct']:.4f}% | taker {row['taker_pct']:.4f}% "
                f"| funding {row['funding_pct']}%")
    return row


def build_fees(bot, symbols: Optional[List[str]] = None) -> Dict[str, Any]:
    exchange = getattr(bot, "exchange", None)
    symbols = symbols if symbols is not None else active_symbols(bot)
    rows, stale = [], False
    now = time.monotonic()
    for sym in symbols:
        key = (id(exchange), sym)
        hit = _memo.get(key)
        if hit and now - hit[0] < TTL_S:
            rows.append(hit[1])
            continue
        try:
            if exchange is None:
                raise RuntimeError("conta sem exchange")
            row = _fetch(exchange, sym)
            _memo[key] = (now, row)
        except Exception as exc:
            logger.warning(f"Taxas de {sym} indisponíveis: {exc}")
            stale = True
            row = {**hit[1], "stale": True} if hit else {"symbol": sym, "maker_pct": None, "taker_pct": None,
                                                         "fee_source": None, "funding_pct": None, "next_funding": None, "stale": True}
        rows.append(row)
    ats = [r["at"] for r in rows if r.get("at")]
    return {"ok": not stale, "rows": rows, "updated_at": min(ats) if ats else None,
            "warning": "a exchange não respondeu; mostrando o último valor conhecido" if stale else None}

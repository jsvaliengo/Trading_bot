"""
TradeLedger — encapsula a lógica de bookkeeping pós-fechamento.

Motivação: o ExecutionEngine tinha ~35 acessos espalhados a atributos
estatísticos do bot (`bot.trades_by_symbol`, `bot.closed_trades_count`,
`bot.pnl_by_symbol`, `bot.trades_win_count`, etc.) que misturavam lógica
de execução com bookkeeping. Esta classe agrupa essas mutações em um
ponto único, testável isoladamente, e deixa engine.py chamando UM método.

Escopo deliberadamente pragmático: a ledger MANTÉM os atributos no bot
(via referência) — não migra storage. Isso preserva compatibilidade com
todos os leitores existentes (telegram, métricas, testes, state_manager)
sem mudar a forma como o bot serializa o estado. Uma migração futura
pode mover storage pra dentro da própria ledger se houver motivo concreto.

A ledger NÃO toca:
- risk_manager.update_pnl (lógica de risco, não bookkeeping)
- dashboard_server.emit_* (notificação, fica explícita em engine)
- telegram.send_position_closed (notificação, fica explícita em engine)
- métricas Prometheus de trade_closed (move pra cá — é parte do
  pós-fechamento e fica natural ao lado do contador interno)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from ..observability import metrics

logger = logging.getLogger(__name__)


class TradeLedger:
    """Bookkeeping pós-fechamento de trade. Opera sobre os contadores do bot."""

    def __init__(self, bot):
        self._bot = bot

    def record_trade_opened(
        self,
        *,
        symbol: str,
        signal: str,
        side: str,
        quantity: float,
        order_size: float,
        entry_price: float,
        stop_loss: Optional[float],
        take_profit: Optional[float],
        strategy_name: str,
        strategy_type: str,
        double_first: bool = False,
        ai_consultive: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Append do trade no histórico (open path). Constrói o dict com o
        schema canônico — antes era replicado em engine.py em dois blocos
        13-fields quase idênticos (LONG e SHORT), com risco de divergir.
        Retorna o trade_record (caso o caller queira inspecionar).
        """
        bot = self._bot
        record = {
            "timestamp": datetime.now().isoformat(),
            "symbol": symbol,
            "signal": signal,
            "side": side,
            "qty": quantity,
            "value": order_size,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "strategy_name": str(strategy_name or "primary"),
            "strategy_type": strategy_type,
            "double_first": bool(double_first),
            "ai_consultive": dict(ai_consultive or {}),
        }
        bot.trade_history.append(record)
        return record

    def record_trade_closed(
        self,
        *,
        symbol: str,
        strategy_name: str,
        pnl_net: float,
        total_fees: float,
        close_reason: str = "",
    ) -> Dict[str, Any]:
        """
        Atualiza todos os contadores e dicts de estatísticas pós-trade,
        emite a métrica Prometheus correspondente, e retorna um resumo
        (closed_trades_count, win_rate, daily_pnl, total_pnl) que o caller
        usa pra log.
        """
        bot = self._bot

        bot.closed_trades_count += 1
        bot.daily_realized_pnl += pnl_net
        bot.total_pnl += pnl_net
        bot.total_fees_paid += total_fees

        if pnl_net > 0:
            bot.trades_win_count += 1
            bot.trades_win_total += pnl_net
        else:
            bot.trades_loss_count += 1
            bot.trades_loss_total += pnl_net  # negativo

        self._bump_stats_bucket(bot.trades_by_symbol, symbol, pnl_net, total_fees)
        self._bump_stats_bucket(bot.trades_by_strategy, strategy_name, pnl_net, total_fees)

        if symbol in bot.pnl_by_symbol:
            bot.pnl_by_symbol[symbol] += pnl_net
        else:
            bot.pnl_by_symbol[symbol] = pnl_net

        metrics.record_trade_closed(
            symbol=symbol,
            strategy=strategy_name,
            result="win" if pnl_net > 0 else "loss",
            pnl_usd=pnl_net,
            fees_usd=total_fees,
            close_reason=close_reason,
        )

        win_rate = (
            (bot.trades_win_count / bot.closed_trades_count * 100.0)
            if bot.closed_trades_count > 0
            else 0.0
        )

        return {
            "closed_trades_count": bot.closed_trades_count,
            "win_rate": win_rate,
            "daily_pnl": bot.daily_realized_pnl,
            "total_pnl": bot.total_pnl,
        }

    @staticmethod
    def _bump_stats_bucket(
        stats_dict: Dict[str, Dict[str, Any]],
        key: str,
        pnl_net: float,
        fees: float,
    ) -> None:
        """Incrementa o bucket de stats (por símbolo ou por estratégia)."""
        bucket = stats_dict.setdefault(
            key,
            {"wins": 0, "losses": 0, "win_value": 0.0, "loss_value": 0.0, "fees": 0.0},
        )
        if pnl_net > 0:
            bucket["wins"] += 1
            bucket["win_value"] += pnl_net
        else:
            bucket["losses"] += 1
            bucket["loss_value"] += pnl_net
        bucket["fees"] = bucket.get("fees", 0.0) + fees

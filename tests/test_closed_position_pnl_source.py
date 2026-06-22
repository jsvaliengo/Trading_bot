"""Fonte do P&L ao fechar posição server-side (_process_binance_closed_position).

Caminho que mordeu 3× (#181→#183→#67): quando as fontes REAIS de P&L falham, o
bot caía na estimativa por PREÇO ATUAL e fabricava o resultado. Estes testes
FIXAM a ordem de prioridade das fontes, pra um refactor não reintroduzir a
fabricação como caminho padrão:

  1. user-stream (pop_realized_close) — exato, instantâneo (#183)
  2. income REST com retry (#181/#182)
  3. estimativa por preço atual — ÚLTIMO recurso (a auto-reconciliação cobre)

A asserção é o pnl_gross entregue ao ledger.record_trade_closed.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock

from trading_bot.core.bot import TradingBot


def _make_bot():
    bot = TradingBot.__new__(TradingBot)
    bot.exchange = MagicMock()
    bot.commission_rates = {"taker_rate": 0.0005}  # evita chamar a API
    bot._runtime_stats_lock = threading.Lock()
    bot.risk_manager = MagicMock()
    bot.ledger = MagicMock()
    bot.telegram = MagicMock()
    # posição com metadata (evita _resolve_strategy_context)
    bot._get_known_position = lambda key: {
        "strategy_name": "trend_strong",
        "custom_take_profit": None,
        "custom_stop_loss": None,
    }
    return bot


def _pos_info():
    return {
        "symbol": "ETHUSDT", "side": "LONG", "entry_price": 1700.0, "quantity": 0.05,
        "entry_time": datetime(2026, 6, 21, 16, 0, tzinfo=timezone.utc),
    }


def _recorded_gross(bot):
    assert bot.ledger.record_trade_closed.called, "ledger não registrou o fechamento"
    return bot.ledger.record_trade_closed.call_args.kwargs["pnl_gross"]


def test_tier1_user_stream_tem_prioridade():
    """Com user-stream disponível, usa o gross real e NEM consulta o income."""
    bot = _make_bot()
    bot.exchange.pop_realized_close.return_value = {
        "gross": -0.666, "exit_price": 1690.0, "qty": 0.05,
    }
    bot._process_binance_closed_position(_pos_info())
    assert _recorded_gross(bot) == -0.666
    bot.exchange.get_income_history.assert_not_called()   # não caiu pro tier 2
    bot.exchange.get_current_price.assert_not_called()    # nem pro tier 3 (fabricação)


def test_tier2_income_quando_user_stream_vazio():
    """Sem user-stream, usa o REALIZED_PNL do income — não a estimativa por preço."""
    bot = _make_bot()
    bot.exchange.pop_realized_close.return_value = None
    bot.exchange.get_income_history.return_value = [
        {"income": "-0.50", "incomeType": "REALIZED_PNL"},
    ]
    bot._process_binance_closed_position(_pos_info())
    assert _recorded_gross(bot) == -0.50
    bot.exchange.get_current_price.assert_not_called()    # NÃO fabricou


def test_tier3_preco_atual_e_ultimo_recurso(monkeypatch):
    """Só quando user-stream E income falham cai na estimativa por preço atual.

    Documenta a fabricação (#67): aqui o preço quicou pra 1690 após um fechamento
    real ruim, e o tier-3 estima −$0.50. A auto-reconciliação corrige isso depois.
    """
    import trading_bot.core.bot as bot_mod
    monkeypatch.setattr(bot_mod.time, "sleep", lambda *_a, **_k: None)  # sem esperar os retries
    bot = _make_bot()
    bot.exchange.pop_realized_close.return_value = None
    bot.exchange.get_income_history.return_value = []     # income nunca popula
    bot.exchange.get_current_price.return_value = 1690.0  # LONG 1700→1690
    bot._process_binance_closed_position(_pos_info())
    # gross estimado = (1690 - 1700) * 0.05 = -0.50
    assert _recorded_gross(bot) == -0.50
    bot.exchange.get_current_price.assert_called_once()   # confirmou: caiu no fallback

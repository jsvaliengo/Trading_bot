"""
Testes da idempotência de fechamento (corrida de double-close).

Bug (investigado em 04/06): bookkeeping de fechamento e remoção de
known_positions não são atômicos e não há chave de idempotência. Um restart na
janela entre registrar o close e remover do tracking faz o monitor re-disparar
o MESMO fechamento no reboot — dobrando contadores/P&L e criando uma linha
`closed` duplicada.

Trava 2 (esta): guarda no início de record_trade_closed usando o store
persistido como fonte de verdade (is_duplicate_close). A trava 1 (claim atômico
no monitor) é mecânica e verificada por leitura.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

from trading_bot.core.trade_store import TradeStore
from trading_bot.core.trade_ledger import TradeLedger


def _store(tmp_path):
    return TradeStore(str(tmp_path / "trades.test.db"))


def _open_record(symbol="ETHUSDT", side="LONG", entry_price=2500.0, **over):
    rec = {
        "timestamp": "2026-05-31T10:00:00", "symbol": symbol, "signal": "BUY",
        "side": side, "qty": 1.5, "value": 100.0, "entry_price": entry_price,
        "stop_loss": 2400.0, "take_profit": 2700.0, "strategy_name": "primary",
        "strategy_type": "hedge", "double_first": False, "ai_consultive": {},
    }
    rec.update(over)
    return rec


# ─────────────────────────── is_duplicate_close ───────────────────────────

def test_open_row_present_is_not_duplicate(tmp_path):
    store = _store(tmp_path)
    store.record_open(_open_record())
    # há open para fechar → fechamento legítimo
    assert store.is_duplicate_close(symbol="ETHUSDT", side="LONG", entry_price=2500.0) is False


def test_no_open_no_closed_is_not_duplicate(tmp_path):
    # reconciliação legítima: posição sem open registrado e sem closed casando
    store = _store(tmp_path)
    assert store.is_duplicate_close(symbol="ETHUSDT", side="LONG", entry_price=2500.0) is False


def test_already_closed_same_entry_is_duplicate(tmp_path):
    store = _store(tmp_path)
    store.record_open(_open_record(entry_price=2500.0))
    store.record_close(
        symbol="ETHUSDT", side="LONG", entry_price=2500.0, exit_price=2600.0,
        exit_at=None, pnl_gross=150.0, pnl_net=149.0, fees=1.0,
        close_reason="TP", strategy_name="primary",
    )
    # sem open restante + closed com mesmo entry → duplicata
    assert store.is_duplicate_close(symbol="ETHUSDT", side="LONG", entry_price=2500.0) is True


def test_closed_with_different_entry_is_not_duplicate(tmp_path):
    store = _store(tmp_path)
    store.record_open(_open_record(entry_price=2500.0))
    store.record_close(
        symbol="ETHUSDT", side="LONG", entry_price=2500.0, exit_price=2600.0,
        exit_at=None, pnl_gross=150.0, pnl_net=149.0, fees=1.0,
        close_reason="TP", strategy_name="primary",
    )
    # novo trade no mesmo par/lado com OUTRO entry → não é duplicata
    assert store.is_duplicate_close(symbol="ETHUSDT", side="LONG", entry_price=2480.0) is False


def test_none_entry_price_is_not_duplicate(tmp_path):
    store = _store(tmp_path)
    assert store.is_duplicate_close(symbol="ETHUSDT", side="LONG", entry_price=None) is False


def test_side_none_matches_any_side(tmp_path):
    store = _store(tmp_path)
    store.record_open(_open_record(entry_price=2500.0))
    store.record_close(
        symbol="ETHUSDT", side="LONG", entry_price=2500.0, exit_price=2600.0,
        exit_at=None, pnl_gross=1.0, pnl_net=1.0, fees=0.0,
        close_reason="TP", strategy_name="primary",
    )
    assert store.is_duplicate_close(symbol="ETHUSDT", side=None, entry_price=2500.0) is True


# ─────────────────── ledger record_trade_closed idempotente ───────────────────

def _ledger_bot(store):
    return SimpleNamespace(
        closed_trades_count=0, daily_realized_pnl=0.0, total_pnl=0.0,
        total_fees_paid=0.0, trades_win_count=0, trades_loss_count=0,
        trades_win_total=0.0, trades_loss_total=0.0, trades_by_symbol={},
        trades_by_strategy={}, pnl_by_symbol={}, trade_history=[],
        _mark_symbol_reentry_cooldown=Mock(), trade_store=store,
    )


def test_record_trade_closed_is_idempotent_on_reprocess(tmp_path):
    store = _store(tmp_path)
    store.record_open(_open_record(entry_price=2500.0))
    bot = _ledger_bot(store)
    ledger = TradeLedger(bot)

    with patch("trading_bot.core.trade_ledger.metrics.record_trade_closed"):
        # 1º fechamento: contabiliza normalmente
        ledger.record_trade_closed(
            symbol="ETHUSDT", strategy_name="primary", pnl_net=-5.0,
            total_fees=0.3, close_reason="SL", side="LONG", entry_price=2500.0,
            exit_price=2480.0, pnl_gross=-4.7,
        )
        assert bot.closed_trades_count == 1
        assert bot.total_pnl == -5.0
        assert bot.trades_loss_count == 1

        # 2º (re-processamento pós-restart): MESMO close → no-op
        ledger.record_trade_closed(
            symbol="ETHUSDT", strategy_name="primary", pnl_net=-5.0,
            total_fees=0.3, close_reason="SL", side="LONG", entry_price=2500.0,
            exit_price=2480.0, pnl_gross=-4.7,
        )

    # contadores e P&L NÃO dobraram
    assert bot.closed_trades_count == 1
    assert bot.total_pnl == -5.0
    assert bot.trades_loss_count == 1
    assert bot.total_fees_paid == 0.3
    # e não criou linha closed duplicada no store
    closed = [t for t in store.recent_trades() if t.get("exit_price") is not None]
    assert len(closed) == 1


def test_no_store_falls_back_to_normal_counting(tmp_path):
    # bot sem trade_store → guarda é pulada, contabiliza normalmente
    bot = _ledger_bot(None)
    ledger = TradeLedger(bot)
    with patch("trading_bot.core.trade_ledger.metrics.record_trade_closed"):
        ledger.record_trade_closed(
            symbol="ETHUSDT", strategy_name="primary", pnl_net=3.0,
            total_fees=0.1, close_reason="TP", side="LONG", entry_price=2500.0,
            exit_price=2600.0, pnl_gross=3.1,
        )
    assert bot.closed_trades_count == 1

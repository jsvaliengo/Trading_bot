"""
Testes do TradeLedger (Phase 4).

Cobre as 4 mutações que o ledger encapsulou (antes espalhadas no engine):
- Counters globais (closed_trades_count, daily/total PnL, total_fees_paid)
- Win/loss counters e totais
- Buckets por símbolo e por estratégia (com primeira-aparição cria bucket)
- pnl_by_symbol acumulativo

E os dois retornos exigidos pelo caller: closed_trades_count e win_rate.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from trading_bot.core.trade_ledger import TradeLedger


def _make_bot() -> SimpleNamespace:
    """Mock minimalista — só os atributos que o ledger toca."""
    return SimpleNamespace(
        closed_trades_count=0,
        daily_realized_pnl=0.0,
        total_pnl=0.0,
        total_fees_paid=0.0,
        trades_win_count=0,
        trades_loss_count=0,
        trades_win_total=0.0,
        trades_loss_total=0.0,
        trades_by_symbol={},
        trades_by_strategy={},
        pnl_by_symbol={},
        trade_history=[],
    )


def test_record_winning_trade_increments_win_counters():
    bot = _make_bot()
    ledger = TradeLedger(bot)
    with patch("trading_bot.core.trade_ledger.metrics.record_trade_closed"):
        summary = ledger.record_trade_closed(
            symbol="BTCUSDT", strategy_name="trend_strong",
            pnl_net=10.0, total_fees=0.5, close_reason="TP",
        )

    assert bot.closed_trades_count == 1
    assert bot.trades_win_count == 1
    assert bot.trades_loss_count == 0
    assert bot.trades_win_total == 10.0
    assert bot.trades_loss_total == 0.0
    assert bot.total_pnl == 10.0
    assert bot.daily_realized_pnl == 10.0
    assert bot.total_fees_paid == 0.5

    assert summary["closed_trades_count"] == 1
    assert summary["win_rate"] == 100.0
    assert summary["daily_pnl"] == 10.0


def test_record_losing_trade_increments_loss_counters_with_negative_total():
    bot = _make_bot()
    ledger = TradeLedger(bot)
    with patch("trading_bot.core.trade_ledger.metrics.record_trade_closed"):
        ledger.record_trade_closed(
            symbol="ETHUSDT", strategy_name="trend_strong",
            pnl_net=-5.0, total_fees=0.3,
        )

    assert bot.trades_loss_count == 1
    assert bot.trades_win_count == 0
    assert bot.trades_loss_total == -5.0  # acumula negativo
    assert bot.total_pnl == -5.0


def test_first_record_for_symbol_creates_stats_bucket():
    bot = _make_bot()
    ledger = TradeLedger(bot)
    with patch("trading_bot.core.trade_ledger.metrics.record_trade_closed"):
        ledger.record_trade_closed(
            symbol="SOLUSDT", strategy_name="range_scalp",
            pnl_net=3.0, total_fees=0.1,
        )

    bucket = bot.trades_by_symbol["SOLUSDT"]
    assert bucket["wins"] == 1
    assert bucket["losses"] == 0
    assert bucket["win_value"] == 3.0
    assert bucket["loss_value"] == 0.0
    assert bucket["fees"] == 0.1


def test_subsequent_records_for_same_symbol_accumulate():
    bot = _make_bot()
    ledger = TradeLedger(bot)
    with patch("trading_bot.core.trade_ledger.metrics.record_trade_closed"):
        ledger.record_trade_closed(symbol="BTCUSDT", strategy_name="x", pnl_net=10.0, total_fees=0.2)
        ledger.record_trade_closed(symbol="BTCUSDT", strategy_name="x", pnl_net=-4.0, total_fees=0.15)
        ledger.record_trade_closed(symbol="BTCUSDT", strategy_name="x", pnl_net=6.0, total_fees=0.1)

    bucket = bot.trades_by_symbol["BTCUSDT"]
    assert bucket["wins"] == 2
    assert bucket["losses"] == 1
    assert bucket["win_value"] == 16.0
    assert bucket["loss_value"] == -4.0
    assert bucket["fees"] == pytest.approx(0.45)


def test_strategy_bucket_is_separate_from_symbol_bucket():
    bot = _make_bot()
    ledger = TradeLedger(bot)
    with patch("trading_bot.core.trade_ledger.metrics.record_trade_closed"):
        ledger.record_trade_closed(symbol="BTCUSDT", strategy_name="trend", pnl_net=1.0, total_fees=0.0)
        ledger.record_trade_closed(symbol="ETHUSDT", strategy_name="trend", pnl_net=2.0, total_fees=0.0)
        ledger.record_trade_closed(symbol="BTCUSDT", strategy_name="range", pnl_net=3.0, total_fees=0.0)

    assert bot.trades_by_strategy["trend"]["wins"] == 2
    assert bot.trades_by_strategy["range"]["wins"] == 1
    assert bot.trades_by_strategy["trend"]["win_value"] == 3.0
    assert bot.trades_by_symbol["BTCUSDT"]["wins"] == 2  # de duas estratégias


def test_pnl_by_symbol_accumulates_across_wins_and_losses():
    bot = _make_bot()
    ledger = TradeLedger(bot)
    with patch("trading_bot.core.trade_ledger.metrics.record_trade_closed"):
        ledger.record_trade_closed(symbol="BTCUSDT", strategy_name="x", pnl_net=10.0, total_fees=0.0)
        ledger.record_trade_closed(symbol="BTCUSDT", strategy_name="x", pnl_net=-3.0, total_fees=0.0)
        ledger.record_trade_closed(symbol="ETHUSDT", strategy_name="x", pnl_net=5.0, total_fees=0.0)

    assert bot.pnl_by_symbol["BTCUSDT"] == 7.0
    assert bot.pnl_by_symbol["ETHUSDT"] == 5.0


def test_win_rate_calculation_with_mixed_trades():
    bot = _make_bot()
    ledger = TradeLedger(bot)
    with patch("trading_bot.core.trade_ledger.metrics.record_trade_closed"):
        for pnl in (1.0, 1.0, 1.0, -1.0):  # 3 wins, 1 loss
            summary = ledger.record_trade_closed(
                symbol="X", strategy_name="x", pnl_net=pnl, total_fees=0.0
            )
    assert summary["closed_trades_count"] == 4
    assert summary["win_rate"] == 75.0


def test_metrics_emitted_with_correct_result_classification():
    bot = _make_bot()
    ledger = TradeLedger(bot)
    with patch("trading_bot.core.trade_ledger.metrics.record_trade_closed") as mock_metric:
        ledger.record_trade_closed(symbol="BTCUSDT", strategy_name="x", pnl_net=2.0, total_fees=0.1, close_reason="TP")
        ledger.record_trade_closed(symbol="BTCUSDT", strategy_name="x", pnl_net=-1.0, total_fees=0.1, close_reason="SL")

    assert mock_metric.call_count == 2
    win_call = mock_metric.call_args_list[0]
    loss_call = mock_metric.call_args_list[1]
    assert win_call.kwargs["result"] == "win"
    assert win_call.kwargs["close_reason"] == "TP"
    assert loss_call.kwargs["result"] == "loss"
    assert loss_call.kwargs["close_reason"] == "SL"


def test_zero_pnl_classifies_as_loss():
    """Borderline: pnl_net == 0 entra no caminho de loss (pnl_net > 0 é False)."""
    bot = _make_bot()
    ledger = TradeLedger(bot)
    with patch("trading_bot.core.trade_ledger.metrics.record_trade_closed"):
        ledger.record_trade_closed(symbol="X", strategy_name="x", pnl_net=0.0, total_fees=0.0)
    assert bot.trades_loss_count == 1
    assert bot.trades_win_count == 0


# ---------- record_trade_opened ----------


def test_record_trade_opened_appends_to_trade_history():
    bot = _make_bot()
    ledger = TradeLedger(bot)
    record = ledger.record_trade_opened(
        symbol="BTCUSDT", signal="STRONG_BUY", side="LONG",
        quantity=0.01, order_size=300.0, entry_price=50000.0,
        stop_loss=49500.0, take_profit=51000.0,
        strategy_name="trend_strong", strategy_type="trend_signal",
    )
    assert len(bot.trade_history) == 1
    assert bot.trade_history[0] is record
    assert record["symbol"] == "BTCUSDT"
    assert record["signal"] == "STRONG_BUY"
    assert record["side"] == "LONG"
    assert record["qty"] == 0.01
    assert record["value"] == 300.0
    assert record["entry_price"] == 50000.0
    assert record["stop_loss"] == 49500.0
    assert record["take_profit"] == 51000.0
    assert record["strategy_name"] == "trend_strong"
    assert record["strategy_type"] == "trend_signal"
    assert record["double_first"] is False
    assert record["ai_consultive"] == {}
    # timestamp deve ser ISO format string
    assert isinstance(record["timestamp"], str)
    assert "T" in record["timestamp"]


def test_record_trade_opened_normalizes_empty_strategy_to_primary():
    bot = _make_bot()
    ledger = TradeLedger(bot)
    record = ledger.record_trade_opened(
        symbol="X", signal="BUY", side="LONG",
        quantity=1.0, order_size=10.0, entry_price=1.0,
        stop_loss=None, take_profit=None,
        strategy_name="", strategy_type="trend_signal",
    )
    assert record["strategy_name"] == "primary"


def test_record_trade_opened_copies_ai_consultive_metadata():
    bot = _make_bot()
    ledger = TradeLedger(bot)
    ai_meta = {"approval": True, "confidence": 85, "decision": "ENTER_NOW"}
    record = ledger.record_trade_opened(
        symbol="X", signal="BUY", side="LONG",
        quantity=1.0, order_size=10.0, entry_price=1.0,
        stop_loss=None, take_profit=None,
        strategy_name="x", strategy_type="trend_signal",
        ai_consultive=ai_meta,
    )
    assert record["ai_consultive"] == ai_meta
    # Defensive copy — mutar o original não afeta o registro
    ai_meta["approval"] = False
    assert record["ai_consultive"]["approval"] is True


def test_record_trade_opened_handles_none_ai_consultive():
    bot = _make_bot()
    ledger = TradeLedger(bot)
    record = ledger.record_trade_opened(
        symbol="X", signal="BUY", side="LONG",
        quantity=1.0, order_size=10.0, entry_price=1.0,
        stop_loss=None, take_profit=None,
        strategy_name="x", strategy_type="trend_signal",
        ai_consultive=None,
    )
    assert record["ai_consultive"] == {}


def test_record_trade_opened_accepts_double_first_flag():
    bot = _make_bot()
    ledger = TradeLedger(bot)
    record = ledger.record_trade_opened(
        symbol="X", signal="BUY", side="LONG",
        quantity=2.0, order_size=20.0, entry_price=1.0,
        stop_loss=None, take_profit=None,
        strategy_name="x", strategy_type="trend_signal",
        double_first=True,
    )
    assert record["double_first"] is True


def test_record_trade_opened_multiple_trades_accumulate_in_history():
    bot = _make_bot()
    ledger = TradeLedger(bot)
    for i in range(3):
        ledger.record_trade_opened(
            symbol=f"PAIR{i}", signal="BUY", side="LONG",
            quantity=1.0, order_size=10.0, entry_price=1.0,
            stop_loss=None, take_profit=None,
            strategy_name="x", strategy_type="trend_signal",
        )
    assert len(bot.trade_history) == 3
    assert [r["symbol"] for r in bot.trade_history] == ["PAIR0", "PAIR1", "PAIR2"]


def test_fees_accumulate_per_symbol_and_strategy():
    bot = _make_bot()
    ledger = TradeLedger(bot)
    with patch("trading_bot.core.trade_ledger.metrics.record_trade_closed"):
        ledger.record_trade_closed(symbol="X", strategy_name="s1", pnl_net=1.0, total_fees=0.1)
        ledger.record_trade_closed(symbol="X", strategy_name="s2", pnl_net=2.0, total_fees=0.2)
        ledger.record_trade_closed(symbol="Y", strategy_name="s1", pnl_net=3.0, total_fees=0.3)

    assert bot.trades_by_symbol["X"]["fees"] == pytest.approx(0.3)
    assert bot.trades_by_symbol["Y"]["fees"] == pytest.approx(0.3)
    assert bot.trades_by_strategy["s1"]["fees"] == pytest.approx(0.4)
    assert bot.trades_by_strategy["s2"]["fees"] == pytest.approx(0.2)
    assert bot.total_fees_paid == pytest.approx(0.6)

"""
Testes da contagem de trades fechados (dashboard).

Bug (14/06): card "Trades fechados" mostrava 6, mas havia 7 fechados (a lista e o
SQLite). O contador em memória (closed_trades_count) dessincronizou dos
fechamentos server-side. A contagem passa a vir do SQLite.
"""
from __future__ import annotations

from types import SimpleNamespace

from trading_bot.core.trade_store import TradeStore
from trading_bot.web.data import collect_summary


def _store(tmp_path):
    return TradeStore(str(tmp_path / "trades.test.db"))


def _open(symbol, side="LONG"):
    return {
        "timestamp": "2026-06-14T10:00:00", "symbol": symbol, "signal": "BUY",
        "side": side, "qty": 1.0, "value": 100.0, "entry_price": 100.0,
        "stop_loss": 99.0, "take_profit": 101.0, "strategy_name": "primary",
        "strategy_type": "trend_signal", "double_first": False, "ai_consultive": None,
    }


def test_count_closed_trades_only_counts_closed(tmp_path):
    store = _store(tmp_path)
    # 2 fechados + 1 aberto
    for sym in ("ETHUSDT", "SOLUSDT"):
        store.record_open(_open(sym))
        store.record_close(
            symbol=sym, side="LONG", entry_price=100.0, exit_price=101.0,
            exit_at="2026-06-14T11:00:00", pnl_gross=1.0, pnl_net=0.9, fees=0.1,
            close_reason="Take Profit (Binance)", strategy_name="primary",
        )
    store.record_open(_open("XRPUSDT", side="SHORT"))  # fica aberto

    assert store.count_closed_trades() == 2
    assert store.count_trades() == 3  # inclui o aberto


def test_collect_summary_uses_sqlite_count_over_memory(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.record_open(_open("ETHUSDT"))
    store.record_close(
        symbol="ETHUSDT", side="LONG", entry_price=100.0, exit_price=101.0,
        exit_at="2026-06-14T11:00:00", pnl_gross=1.0, pnl_net=0.9, fees=0.1,
        close_reason="Take Profit (Binance)", strategy_name="primary",
    )
    # Contador em memória DESSINCRONIZADO (0) — o card deve usar o SQLite (1).
    bot = SimpleNamespace(
        initial_capital=300.0, closed_trades_count=0, paused=False, running=True,
        trade_store=store, portfolio_history=[],
        exchange=SimpleNamespace(
            get_account_info=lambda: {"wallet_balance": 300.0, "unrealized_pnl": 0.0},
            get_daily_pnl_from_binance=lambda: {"total": 0.0},
        ),
        daily_pnl_binance_baseline=0.0, total_pnl=0.0,
    )
    summary = collect_summary(bot)
    assert summary["closed_trades"] == 1


def test_closed_trade_counters_wins_losses(tmp_path):
    store = _store(tmp_path)
    # 2 wins, 1 loss, 1 aberto
    specs = [("ETHUSDT", 0.9), ("SOLUSDT", 0.5), ("SUIUSDT", -0.4)]
    for sym, pnl in specs:
        store.record_open(_open(sym))
        store.record_close(
            symbol=sym, side="LONG", entry_price=100.0, exit_price=101.0,
            exit_at="2026-06-14T11:00:00", pnl_gross=pnl + 0.1, pnl_net=pnl, fees=0.1,
            close_reason="x", strategy_name="primary",
        )
    store.record_open(_open("XRPUSDT", side="SHORT"))  # aberto

    counters = store.closed_trade_counters()
    assert counters == {"closed": 3, "wins": 2, "losses": 1}

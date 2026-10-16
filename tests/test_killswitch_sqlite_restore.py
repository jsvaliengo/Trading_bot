"""
Startup reconstrói do SQLite o histórico diário do kill switch e os contadores
derivados de trades (16/10): o state JSON dizia 14/10 +1.50 e 15/10 +0.92, mas o
SQLite (backfill com o P&L real da Binance) dizia -2.41 e -0.96 — o loss-streak
nunca via a sequência real.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from trading_bot.core.bot import TradingBot
from trading_bot.core.config import config
from trading_bot.core.trade_store import TradeStore
from trading_bot.services.kill_switch import KillSwitchMonitor


def _ks_config():
    return SimpleNamespace(
        KILL_SWITCH_ENABLED=True,
        KILL_SWITCH_LOSS_STREAK_DAYS=3,
        KILL_SWITCH_DRAWDOWN_ALERT_PERCENT=0.0,
        KILL_SWITCH_WR_FLOOR_PERCENT=0.0,
        KILL_SWITCH_WR_MIN_TRADES=0,
    )


def _close(store, symbol, exit_at, pnl_net, fees):
    store.record_open({
        "timestamp": exit_at, "symbol": symbol, "signal": "BUY", "side": "LONG",
        "qty": 1.0, "value": 100.0, "entry_price": 100.0, "stop_loss": 99.0,
        "take_profit": 101.0, "strategy_name": "primary",
        "strategy_type": "trend_signal", "double_first": False, "ai_consultive": None,
    })
    store.record_close(
        symbol=symbol, side="LONG", entry_price=100.0, exit_price=100.0,
        exit_at=exit_at, pnl_gross=pnl_net + fees, pnl_net=pnl_net, fees=fees,
        close_reason="SL", strategy_name="primary",
    )


TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _day(offset):
    return (date.fromisoformat(TODAY) - timedelta(days=offset)).isoformat()


# 4 dias atrás: dia SEM trade (só o JSON conhece; net 0 quebra sequência).
# 3..1 dias atrás: o JSON diz verde, o SQLite (real) diz vermelho.
D_EMPTY, D1, D2, D3 = _day(4), _day(3), _day(2), _day(1)


def _seed_store(store):
    _close(store, "BTCUSDT", f"{D1}T10:00:00", -0.30, 0.05)
    _close(store, "ETHUSDT", f"{D2}T09:00:00", -3.00, 0.10)
    _close(store, "BTCUSDT", f"{D2}T15:00:00", 0.59, 0.07)
    _close(store, "ETHUSDT", f"{D3}T23:59:00", -0.96, 0.08)
    _close(store, "SOLUSDT", f"{TODAY}T00:01:00", 0.25, 0.02)


def _make_bot(tmp_path, store):
    bot = TradingBot.__new__(TradingBot)
    bot._init_runtime_state()
    bot.trade_store = store
    bot.telegram = MagicMock()
    bot.kill_switch = KillSwitchMonitor(config_obj=_ks_config(), telegram=bot.telegram)
    bot._state_file_path = str(tmp_path / "bot_state.json")
    bot.exchange = MagicMock()
    bot.exchange.get_daily_pnl_from_binance.return_value = {"total": 0.0}
    return bot


def _write_stale_state(tmp_path):
    stale_history = [
        {"date": D_EMPTY, "net_pnl": 0.0, "trades_win": 0, "trades_loss": 0},
        {"date": D1, "net_pnl": 0.40, "trades_win": 0, "trades_loss": 0},
        {"date": D2, "net_pnl": 1.50, "trades_win": 0, "trades_loss": 0},
        {"date": D3, "net_pnl": 0.92, "trades_win": 0, "trades_loss": 0},
    ]
    (tmp_path / "bot_state.json").write_text(json.dumps({
        "daily_date": TODAY,
        "daily_realized_pnl": 9.99,
        "closed_trades_count": 1,
        "pnl_by_symbol": {"BTCUSDT": 7.0, "XRPUSDT": 3.0},
        "trades_win_total": 10.0,
        "trades_loss_total": -1.0,
        "total_fees_paid": 0.01,
        "kill_switch": {"daily_pnl_history": stale_history, "alerted_events": {}},
    }), encoding="utf-8")


@pytest.fixture
def restored(tmp_path):
    store = TradeStore(str(tmp_path / "trades.db"))
    _seed_store(store)
    _write_stale_state(tmp_path)
    bot = _make_bot(tmp_path, store)
    assert bot.load_state() is True
    return bot, store


def test_kill_switch_history_comes_from_sqlite_and_streak_fires(restored):
    bot, _ = restored
    history = {e.date: round(e.net_pnl, 2) for e in bot.kill_switch.daily_pnl_history}
    assert history == {D_EMPTY: 0.0, D1: -0.30, D2: -2.41, D3: -0.96}
    assert TODAY not in history  # dia corrente ainda não fechou

    bot.paused = False
    bot.kill_switch.check_all(bot=bot)
    assert bot.paused is True


def test_trade_counters_equal_sqlite_aggregates(restored):
    bot, store = restored
    # pares configurados entram com 0.0; o stale XRPUSDT=3.0 do JSON some
    traded = {s: v for s, v in bot.pnl_by_symbol.items() if v}
    assert traded == pytest.approx({"BTCUSDT": 0.29, "ETHUSDT": -3.96, "SOLUSDT": 0.25})
    assert bot.trades_win_total == pytest.approx(0.84)
    assert bot.trades_loss_total == pytest.approx(-4.26)
    assert bot.total_fees_paid == pytest.approx(0.32)
    assert bot.daily_realized_pnl == pytest.approx(store.realized_pnl_today())


def test_close_after_restore_does_not_double_count_and_restore_is_idempotent(restored, tmp_path):
    bot, store = restored
    with patch("trading_bot.core.trade_ledger.metrics.record_trade_closed"):
        bot.ledger.record_trade_closed(
            symbol="ETHUSDT", strategy_name="primary", pnl_net=-0.50, total_fees=0.04,
            close_reason="SL", side="LONG", entry_price=123.0, exit_price=122.0,
        )
    assert bot.pnl_by_symbol["ETHUSDT"] == pytest.approx(-4.46)
    assert bot.trades_loss_total == pytest.approx(-4.76)
    assert bot.total_fees_paid == pytest.approx(0.36)

    before = (dict(bot.pnl_by_symbol), bot.trades_win_total, bot.trades_loss_total,
              bot.total_fees_paid, [e.to_dict() for e in bot.kill_switch.daily_pnl_history])
    bot.save_state()
    again = _make_bot(tmp_path, store)
    assert again.load_state() is True
    after = (dict(again.pnl_by_symbol), again.trades_win_total, again.trades_loss_total,
             again.total_fees_paid, [e.to_dict() for e in again.kill_switch.daily_pnl_history])
    assert after[0] == pytest.approx(before[0])
    assert after[1:4] == pytest.approx(before[1:4])
    assert after[4] == before[4]


def test_daily_rollover_records_sqlite_day_not_drifted_memory(restored, monkeypatch):
    bot, _ = restored
    monkeypatch.setattr(config, "USE_DAILY_TARGETS", True)
    bot.kill_switch.daily_pnl_history = []
    bot.last_daily_reset = date.fromisoformat(D3)
    bot.daily_realized_pnl = 0.92  # contador em memória que o reconciler não corrigiu
    bot.check_daily_targets()
    assert [(e.date, round(e.net_pnl, 2)) for e in bot.kill_switch.daily_pnl_history] == [(D3, -0.96)]


def test_empty_db_keeps_json_history(tmp_path):
    store = TradeStore(str(tmp_path / "trades.db"))
    _write_stale_state(tmp_path)
    bot = _make_bot(tmp_path, store)
    assert bot.load_state() is True
    assert [round(e.net_pnl, 2) for e in bot.kill_switch.daily_pnl_history] == [0.0, 0.40, 1.50, 0.92]
    assert not any(bot.pnl_by_symbol.values())  # stale 7.0/3.0 do JSON somem
    assert (bot.trades_win_total, bot.trades_loss_total, bot.total_fees_paid) == (0.0, 0.0, 0.0)
    assert bot.daily_realized_pnl == 0.0

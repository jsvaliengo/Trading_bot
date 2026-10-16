"""Um único "dia de trading" (DAILY_PNL_TZ_OFFSET_HOURS) e timestamps sem
ambiguidade (16/10): o DB gravava exit_at ingênuo no fuso da VM (BRT) e cada
leitor o interpretava de um jeito — histórico/kill switch/P&L de hoje como UTC,
card como local. Trade fechado às 22:30 BRT caía em dias diferentes."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from conftest import set_process_tz
from trading_bot.core.bot import TradingBot
from trading_bot.core.config import config
from trading_bot.core.trade_store import TradeStore, trading_day, trading_day_start
from trading_bot.services.kill_switch import KillSwitchMonitor
from trading_bot.services.pnl_reconciler import reconcile_recent_pnl

UTC = timezone.utc
# 22:30 BRT de 15/01 = 01:30 UTC de 16/01. Legado ingênuo (VM em BRT) + linha nova.
LEGACY_EXIT_BRT = "2026-01-15T22:30:00.123456"
EXIT_UTC = datetime(2026, 1, 16, 1, 30, 0, 123456, tzinfo=UTC)
NEW_EXIT_UTC = datetime(2026, 1, 16, 1, 40, tzinfo=UTC)  # 22:40 BRT


@pytest.fixture
def brt():
    set_process_tz("America/Sao_Paulo")
    yield
    set_process_tz("UTC")


def _insert_legacy(path, exit_at=LEGACY_EXIT_BRT, opened_at="2026-01-15T21:00:00", pnl=-1.0):
    TradeStore(path).close()
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO trades (opened_at, symbol, side, qty, entry_price, exit_at, "
        "pnl_gross, pnl_net, fees, status) VALUES (?,?,?,?,?,?,?,?,?, 'closed')",
        (opened_at, "BTCUSDT", "LONG", 1.0, 100.0, exit_at, pnl + 0.1, pnl, 0.1),
    )
    conn.commit()
    conn.close()


def _legacy_plus_new(tmp_path):
    path = str(tmp_path / "trades.db")
    _insert_legacy(path)
    store = TradeStore(path)
    store.record_close(
        symbol="ETHUSDT", side="LONG", entry_price=100.0, exit_price=101.0,
        exit_at=NEW_EXIT_UTC.isoformat(), pnl_gross=2.1, pnl_net=2.0, fees=0.1,
        close_reason="TP", strategy_name="primary",
    )
    return store


def _ks_config():
    return SimpleNamespace(
        KILL_SWITCH_ENABLED=True, KILL_SWITCH_LOSS_STREAK_DAYS=3,
        KILL_SWITCH_DRAWDOWN_ALERT_PERCENT=0.0, KILL_SWITCH_WR_FLOOR_PERCENT=0.0,
        KILL_SWITCH_WR_MIN_TRADES=0,
    )


@pytest.mark.parametrize("offset, day", [(-3.0, "2026-01-15"), (0.0, "2026-01-16")])
def test_trade_2230_brt_lands_on_same_day_everywhere(brt, tmp_path, monkeypatch, offset, day):
    monkeypatch.setattr(config, "DAILY_PNL_TZ_OFFSET_HOURS", offset)
    monkeypatch.setattr(config, "USE_DAILY_TARGETS", True)
    store = _legacy_plus_new(tmp_path)
    assert trading_day(EXIT_UTC) == day

    history = {r["day"]: round(r["net"], 2) for r in store.daily_pnl_history(limit=0)}
    assert history == {day: 1.0}
    assert store.realized_pnl_today(day=day) == pytest.approx(1.0)
    card_now = EXIT_UTC + timedelta(minutes=30)
    assert store.count_closed_since(trading_day_start(card_now)) == 2

    ks = KillSwitchMonitor(config_obj=_ks_config(), telegram=MagicMock())
    ks.rebuild_history_from_store(store.daily_pnl_history(limit=0), today="2026-02-01")
    rebuilt = [(e.date, round(e.net_pnl, 2)) for e in ks.daily_pnl_history]

    bot = TradingBot.__new__(TradingBot)
    bot._init_runtime_state()
    bot.trade_store = store
    bot.telegram = MagicMock()
    bot.kill_switch = KillSwitchMonitor(config_obj=_ks_config(), telegram=bot.telegram)
    bot.last_daily_reset = date.fromisoformat(day)
    bot.check_daily_targets()
    rolled = [(e.date, round(e.net_pnl, 2)) for e in bot.kill_switch.daily_pnl_history]
    assert rebuilt == rolled == [(day, 1.0)]


def test_legacy_naive_rows_are_migrated_to_utc_idempotently(brt, tmp_path):
    store = _legacy_plus_new(tmp_path)
    path = store.db_path
    store.close()

    def rows():
        conn = sqlite3.connect(path)
        out = conn.execute("SELECT opened_at, exit_at FROM trades ORDER BY id").fetchall()
        conn.close()
        return out

    expected = [
        ("2026-01-16T00:00:00+00:00", "2026-01-16T01:30:00.123456+00:00"),
        ("2026-01-16T01:40:00+00:00", "2026-01-16T01:40:00+00:00"),
    ]
    TradeStore(path).close()
    assert rows() == expected
    TradeStore(path).close()
    assert rows() == expected


def test_first_trade_time_ms_reads_legacy_brt_as_local(brt, tmp_path):
    path = str(tmp_path / "trades.db")
    _insert_legacy(path, opened_at="2026-10-13T23:57:11")
    expected = datetime(2026, 10, 14, 2, 57, 11, tzinfo=UTC)
    assert TradeStore(path).first_trade_time_ms() == int(expected.timestamp() * 1000)


def test_reconciler_matches_income_of_legacy_brt_row(brt, tmp_path):
    path = str(tmp_path / "trades.db")
    _insert_legacy(path, pnl=1.28)
    store = TradeStore(path)
    fill_ms = int((EXIT_UTC + timedelta(seconds=5)).timestamp() * 1000)
    seen = {}

    def income(**kw):
        seen.update(kw)
        return [
            {"symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": "-0.64", "time": fill_ms},
            {"symbol": "BTCUSDT", "incomeType": "COMMISSION", "income": "-0.03", "time": fill_ms},
        ]

    bot = SimpleNamespace(trade_store=store, exchange=SimpleNamespace(get_income_history=income))
    res = reconcile_recent_pnl(bot, lookback_hours=24 * 365 * 5)
    assert res["corrected"] == 1
    opened_ms = int(datetime(2026, 1, 16, 0, 0, tzinfo=UTC).timestamp() * 1000)
    assert seen["start_time"] == opened_ms - 15 * 60 * 1000
    assert store.realized_pnl_today(day=trading_day(EXIT_UTC)) == pytest.approx(-0.67)

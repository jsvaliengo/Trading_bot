from types import SimpleNamespace

from trading_bot.services.kill_switch import (
    DailyPnlEntry,
    KillSwitchMonitor,
)


def _make_config(**overrides):
    base = {
        "KILL_SWITCH_ENABLED": True,
        "KILL_SWITCH_LOSS_STREAK_DAYS": 3,
        "KILL_SWITCH_DRAWDOWN_ALERT_PERCENT": 5.0,
        "KILL_SWITCH_WR_FLOOR_PERCENT": 40.0,
        "KILL_SWITCH_WR_MIN_TRADES": 20,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeTelegram:
    def __init__(self):
        self.messages: list[str] = []

    def send_message(self, text: str) -> bool:
        self.messages.append(text)
        return True


def _make_bot(**overrides):
    base = dict(
        paused=False,
        peak_equity=100.0,
        last_known_balance=100.0,
        trades_win_count=0,
        trades_loss_count=0,
        exchange=SimpleNamespace(get_account_info=lambda: {"wallet_balance": 100.0}),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_loss_streak_pauses_and_alerts_after_3_red_days():
    cfg = _make_config()
    tg = _FakeTelegram()
    ks = KillSwitchMonitor(config_obj=cfg, telegram=tg)

    for date, pnl in [("2026-04-20", -1.0), ("2026-04-21", -0.5), ("2026-04-22", -2.0)]:
        ks.record_daily_rollover(date=date, net_pnl=pnl, trades_win=0, trades_loss=0)

    bot = _make_bot()
    ks.check_all(bot=bot)

    assert bot.paused is True
    assert len(tg.messages) == 1
    assert "SEQUÊNCIA NEGATIVA" in tg.messages[0]
    assert "2026-04-22" in tg.messages[0]


def test_loss_streak_does_not_trigger_with_mixed_days():
    cfg = _make_config()
    tg = _FakeTelegram()
    ks = KillSwitchMonitor(config_obj=cfg, telegram=tg)

    # 2 vermelhos intercalados com 1 verde — não é streak.
    for date, pnl in [("2026-04-20", -1.0), ("2026-04-21", +0.5), ("2026-04-22", -2.0)]:
        ks.record_daily_rollover(date=date, net_pnl=pnl, trades_win=0, trades_loss=0)

    bot = _make_bot()
    ks.check_all(bot=bot)

    assert bot.paused is False
    assert tg.messages == []


def test_loss_streak_alert_fires_only_once_per_episode():
    cfg = _make_config()
    tg = _FakeTelegram()
    ks = KillSwitchMonitor(config_obj=cfg, telegram=tg)

    for date, pnl in [("2026-04-20", -1.0), ("2026-04-21", -0.5), ("2026-04-22", -2.0)]:
        ks.record_daily_rollover(date=date, net_pnl=pnl, trades_win=0, trades_loss=0)

    bot = _make_bot()
    ks.check_all(bot=bot)
    ks.check_all(bot=bot)
    ks.check_all(bot=bot)

    assert len(tg.messages) == 1


def test_drawdown_alert_pauses_when_threshold_crossed():
    cfg = _make_config(KILL_SWITCH_DRAWDOWN_ALERT_PERCENT=5.0)
    tg = _FakeTelegram()
    ks = KillSwitchMonitor(config_obj=cfg, telegram=tg)

    bot = _make_bot(peak_equity=100.0, last_known_balance=94.0)  # drawdown = 6%
    ks.check_all(bot=bot)

    assert bot.paused is True
    assert len(tg.messages) == 1
    assert "DRAWDOWN DO PICO" in tg.messages[0]
    assert "6.00%" in tg.messages[0]


def test_drawdown_alert_silent_below_threshold():
    cfg = _make_config(KILL_SWITCH_DRAWDOWN_ALERT_PERCENT=5.0)
    tg = _FakeTelegram()
    ks = KillSwitchMonitor(config_obj=cfg, telegram=tg)

    bot = _make_bot(peak_equity=100.0, last_known_balance=96.0)  # drawdown = 4%
    ks.check_all(bot=bot)

    assert bot.paused is False
    assert tg.messages == []


def test_drawdown_alert_rearms_after_recovery():
    cfg = _make_config(KILL_SWITCH_DRAWDOWN_ALERT_PERCENT=5.0)
    tg = _FakeTelegram()
    ks = KillSwitchMonitor(config_obj=cfg, telegram=tg)

    bot = _make_bot(peak_equity=100.0, last_known_balance=94.0)
    ks.check_all(bot=bot)  # dispara
    assert len(tg.messages) == 1

    # Bot recupera
    bot.last_known_balance = 97.0
    ks.check_all(bot=bot)
    assert len(tg.messages) == 1

    # Bot cai de novo abaixo do threshold
    bot.last_known_balance = 93.0
    ks.check_all(bot=bot)
    assert len(tg.messages) == 2


def test_win_rate_alert_fires_below_floor_with_enough_sample():
    cfg = _make_config(KILL_SWITCH_WR_FLOOR_PERCENT=40.0, KILL_SWITCH_WR_MIN_TRADES=20)
    tg = _FakeTelegram()
    ks = KillSwitchMonitor(config_obj=cfg, telegram=tg)

    # 25 trades com 30% WR
    bot = _make_bot(trades_win_count=7, trades_loss_count=18)
    ks.check_all(bot=bot)

    assert bot.paused is False  # WR só alerta, não pausa
    assert len(tg.messages) == 1
    assert "WIN RATE BAIXO" in tg.messages[0]
    assert "28.0%" in tg.messages[0]


def test_win_rate_alert_silent_with_insufficient_sample():
    cfg = _make_config(KILL_SWITCH_WR_FLOOR_PERCENT=40.0, KILL_SWITCH_WR_MIN_TRADES=20)
    tg = _FakeTelegram()
    ks = KillSwitchMonitor(config_obj=cfg, telegram=tg)

    bot = _make_bot(trades_win_count=2, trades_loss_count=15)  # só 17 trades
    ks.check_all(bot=bot)

    assert tg.messages == []


def test_win_rate_alert_silent_when_above_floor():
    cfg = _make_config(KILL_SWITCH_WR_FLOOR_PERCENT=40.0, KILL_SWITCH_WR_MIN_TRADES=20)
    tg = _FakeTelegram()
    ks = KillSwitchMonitor(config_obj=cfg, telegram=tg)

    bot = _make_bot(trades_win_count=15, trades_loss_count=10)  # 60% WR
    ks.check_all(bot=bot)

    assert tg.messages == []


def test_kill_switch_disabled_skips_all_checks():
    cfg = _make_config(KILL_SWITCH_ENABLED=False)
    tg = _FakeTelegram()
    ks = KillSwitchMonitor(config_obj=cfg, telegram=tg)

    for date, pnl in [("2026-04-20", -1.0), ("2026-04-21", -0.5), ("2026-04-22", -2.0)]:
        ks.record_daily_rollover(date=date, net_pnl=pnl, trades_win=0, trades_loss=0)

    bot = _make_bot(peak_equity=100.0, last_known_balance=50.0, trades_win_count=1, trades_loss_count=30)
    ks.check_all(bot=bot)

    assert bot.paused is False
    assert tg.messages == []


def test_state_roundtrip_preserves_history_and_alerted_events():
    cfg = _make_config()
    tg = _FakeTelegram()
    ks = KillSwitchMonitor(config_obj=cfg, telegram=tg)

    ks.record_daily_rollover(date="2026-04-20", net_pnl=-1.0, trades_win=0, trades_loss=0)
    ks.record_daily_rollover(date="2026-04-21", net_pnl=-0.5, trades_win=0, trades_loss=0)
    ks.alerted_events["drawdown"] = "peak=100|thr=5"

    state = ks.to_state()

    ks2 = KillSwitchMonitor(config_obj=cfg, telegram=tg)
    ks2.load_from_state(state)

    assert len(ks2.daily_pnl_history) == 2
    assert ks2.daily_pnl_history[0].date == "2026-04-20"
    assert ks2.daily_pnl_history[1].net_pnl == -0.5
    assert ks2.alerted_events["drawdown"] == "peak=100|thr=5"


def test_daily_history_capped_at_max():
    from trading_bot.services.kill_switch import DAILY_HISTORY_MAX_ENTRIES

    cfg = _make_config()
    tg = _FakeTelegram()
    ks = KillSwitchMonitor(config_obj=cfg, telegram=tg)

    for i in range(DAILY_HISTORY_MAX_ENTRIES + 5):
        ks.record_daily_rollover(
            date=f"2026-04-{i+1:02d}", net_pnl=-0.1, trades_win=0, trades_loss=0
        )

    assert len(ks.daily_pnl_history) == DAILY_HISTORY_MAX_ENTRIES


def test_record_daily_rollover_dedups_same_date():
    cfg = _make_config()
    ks = KillSwitchMonitor(config_obj=cfg, telegram=_FakeTelegram())

    ks.record_daily_rollover(date="2026-04-22", net_pnl=-1.0, trades_win=0, trades_loss=0)
    ks.record_daily_rollover(date="2026-04-22", net_pnl=-2.0, trades_win=0, trades_loss=0)

    assert len(ks.daily_pnl_history) == 1
    assert ks.daily_pnl_history[0].net_pnl == -2.0


def test_daily_pnl_entry_from_dict_rejects_invalid():
    assert DailyPnlEntry.from_dict("not-a-dict") is None
    assert DailyPnlEntry.from_dict({"date": "2026-04-22", "net_pnl": "abc"}) is None
    good = DailyPnlEntry.from_dict({"date": "2026-04-22", "net_pnl": -1.0})
    assert good is not None
    assert good.net_pnl == -1.0

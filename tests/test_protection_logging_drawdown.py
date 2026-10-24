"""Proteções (24/10): pausas do kill switch no log, drawdown com equity simulada
no testnet e token do Telegram fora do log."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from trading_bot.core.bot import TradingBot
from trading_bot.core.config import config
from trading_bot.core.trade_store import TradeStore
from trading_bot.observability.log_redaction import RedactSecretsFilter
from trading_bot.services.kill_switch import DailyPnlEntry, KillSwitchMonitor
from trading_bot.services.telegram_commands import TelegramCommandHandler

STREAK = [("2026-10-19", -2.73), ("2026-10-20", -2.90), ("2026-10-21", -1.87)]


class _Tg:
    def __init__(self):
        self.messages: list[str] = []

    def send_message(self, text):
        self.messages.append(text)
        return True


def _ks(**overrides):
    cfg = SimpleNamespace(
        KILL_SWITCH_ENABLED=True,
        KILL_SWITCH_LOSS_STREAK_DAYS=3,
        KILL_SWITCH_DRAWDOWN_ALERT_PERCENT=5.0,
        KILL_SWITCH_WR_FLOOR_PERCENT=0.0,
        KILL_SWITCH_WR_MIN_TRADES=0,
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    tg = _Tg()
    return KillSwitchMonitor(config_obj=cfg, telegram=tg), tg


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_loss_streak_pause_logs_warning_with_days_and_values(caplog):
    ks, _ = _ks(KILL_SWITCH_DRAWDOWN_ALERT_PERCENT=0.0)
    for day, net in STREAK:
        ks.record_daily_rollover(date=day, net_pnl=net, trades_win=0, trades_loss=1)
    bot = SimpleNamespace(paused=False, peak_equity=0.0, trades_win_count=0, trades_loss_count=0)
    with caplog.at_level(logging.WARNING):
        ks.check_all(bot=bot)
    assert bot.paused is True
    assert _warnings(caplog) == [
        "🛑 Kill switch: 3 dias seguidos negativos (2026-10-19: $-2.73 | 2026-10-20: $-2.90 | "
        "2026-10-21: $-1.87; total $-7.50) → bot pausado; retome com /resume"
    ]


def test_manual_resume_not_undone_when_streak_net_changes_in_cents():
    ks, tg = _ks(KILL_SWITCH_DRAWDOWN_ALERT_PERCENT=0.0)
    for day, net in STREAK:
        ks.record_daily_rollover(date=day, net_pnl=net, trades_win=0, trades_loss=1)
    bot = SimpleNamespace(paused=False, peak_equity=0.0, trades_win_count=0, trades_loss_count=0)
    ks.check_all(bot=bot)
    bot.paused = False  # /resume manual
    # reconciler/rebuild do restart corrige o 21/10 no 2º decimal, mesmo dia
    ks.daily_pnl_history[-1] = DailyPnlEntry("2026-10-21", -1.88, 0, 1)
    ks.check_all(bot=bot)
    assert bot.paused is False
    assert len(tg.messages) == 1


def test_drawdown_pause_logs_and_uses_bot_drawdown_equity(caplog):
    ks, tg = _ks(KILL_SWITCH_LOSS_STREAK_DAYS=99)
    # wallet cappado (constante) não serve: vale a equity simulada
    bot = SimpleNamespace(paused=False, peak_equity=110.0, last_known_balance=173.49,
                          last_drawdown_equity=100.0, trades_win_count=0, trades_loss_count=0)
    with caplog.at_level(logging.WARNING):
        ks.check_all(bot=bot)
    assert bot.paused is True
    assert _warnings(caplog) == [
        "🛑 Kill switch: drawdown 9.09% do pico (gatilho 5.00%; pico $110.00 → atual $100.00) "
        "→ bot pausado; retome com /resume"
    ]
    bot.paused = False
    bot.peak_equity = 110.004  # pico reconstruído no restart, centavos diferentes
    ks.check_all(bot=bot)
    assert bot.paused is False
    assert len(tg.messages) == 1


def test_telegram_resume_logs_source(caplog):
    handler = TelegramCommandHandler(token="token", chat_id="123")
    bot = SimpleNamespace(running=True, paused=True)
    handler.set_bot_reference(bot, config)
    handler.send_message = lambda text: True
    with caplog.at_level(logging.WARNING):
        handler.cmd_resume([])
    assert bot.paused is False
    assert "▶️ Bot retomado via Telegram /resume" in _warnings(caplog)


class _Exchange:
    def __init__(self, unrealized):
        self.unrealized = unrealized
        self.calls = 0

    def get_account_info(self):
        self.calls += 1
        return {"wallet_balance": 173.49, "unrealized_pnl": self.unrealized}


def _close(store, exit_at, pnl_net):
    store.record_open({
        "timestamp": exit_at, "symbol": "BTCUSDT", "signal": "BUY", "side": "LONG",
        "qty": 1.0, "value": 100.0, "entry_price": 100.0, "stop_loss": 99.0,
        "take_profit": 101.0, "strategy_name": "primary",
        "strategy_type": "trend_signal", "double_first": False, "ai_consultive": None,
    })
    store.record_close(
        symbol="BTCUSDT", side="LONG", entry_price=100.0, exit_price=100.0,
        exit_at=exit_at, pnl_gross=pnl_net, pnl_net=pnl_net, fees=0.0,
        close_reason="SL", strategy_name="primary",
    )


@pytest.fixture
def sim_bot(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ENVIRONMENT", "testnet", raising=False)
    monkeypatch.setattr(config, "SIMULATED_BALANCE_USD", 100.0)
    monkeypatch.setattr(config, "MAX_DRAWDOWN_FROM_PEAK_PERCENT", 30.0)
    store = TradeStore(str(tmp_path / "trades.db"))
    _close(store, "2026-10-20T10:00:00", 10.0)   # pico realizado: 110
    _close(store, "2026-10-21T10:00:00", -35.0)  # realizado: 75
    bot = TradingBot.__new__(TradingBot)
    bot.trade_store = store
    bot.total_pnl = 0.0
    bot.peak_equity = 0.0
    bot.exchange = _Exchange(unrealized=-2.0)
    return bot


def test_testnet_simulated_drawdown_trips_gate_and_kill_switch(sim_bot):
    assert config.USE_TESTNET is True
    sim_bot._rebuild_peak_equity_from_store(sim_bot.trade_store)
    assert sim_bot.peak_equity == pytest.approx(110.0)
    # wallet cappado = 100 sempre; equity = 100 + (10 - 35) - 2 = 73 → 33.6% do pico
    equity = sim_bot.drawdown_equity(100.0)
    assert equity == pytest.approx(73.0)
    assert sim_bot._check_drawdown_from_peak(equity) is True

    ks, tg = _ks(KILL_SWITCH_LOSS_STREAK_DAYS=99)
    sim_bot.paused = False
    sim_bot.trades_win_count = sim_bot.trades_loss_count = 0
    sim_bot.last_drawdown_equity = equity
    ks.check_all(bot=sim_bot)
    assert sim_bot.paused is True
    assert "DRAWDOWN DO PICO" in tg.messages[0]


def test_mainnet_drawdown_equity_is_wallet_and_peak_not_rebuilt(sim_bot, monkeypatch):
    monkeypatch.setattr(config, "ENVIRONMENT", "mainnet", raising=False)
    assert sim_bot.drawdown_equity(250.0) == 250.0
    assert sim_bot.exchange.calls == 0
    sim_bot._rebuild_peak_equity_from_store(sim_bot.trade_store)
    assert sim_bot.peak_equity == 0.0


def test_redaction_masks_telegram_token_in_formatted_exception():
    token = "8123456789:AAHk3xQ_zP-9vL2mN4bR7tY1uW6sE0cD5fG"
    stream_records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            stream_records.append(self.format(record))

    handler = _Capture()
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    handler.addFilter(RedactSecretsFilter())
    log = logging.getLogger("test.redaction")
    log.addHandler(handler)
    try:
        try:
            raise ConnectionError(
                f"HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded "
                f"with url: /bot{token}/getUpdates?offset=1&timeout=30"
            )
        except ConnectionError as exc:
            log.exception("Erro ao buscar updates: %s", exc)
            log.error(
                "binance: GET https://testnet.binancefuture.com/fapi/v2/account?timestamp=1&signature=%s",
                "ab12" * 16,
            )
    finally:
        log.removeHandler(handler)

    joined = "\n".join(stream_records)
    assert token not in joined
    assert "ab12ab12" not in joined
    assert "/bot<telegram-token>/getUpdates" in stream_records[0]
    assert "signature=<redacted>" in stream_records[1]
    assert "Traceback" in stream_records[0]

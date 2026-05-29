import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from trading_bot.core.bot import TradingBot
from trading_bot.core.config import config
from trading_bot.core.strategy import HedgeStrategy, RangeScalpingStrategy, Signal, TradeSetup


def _make_light_bot():
    bot = TradingBot.__new__(TradingBot)
    bot._init_runtime_state()
    return bot


def _make_range_klines(last_close: float) -> list[dict]:
    pattern = [
        99.15, 99.82, 99.22, 99.78, 99.18, 99.88,
        99.24, 99.74, 99.16, 99.84, 99.20, last_close,
    ]
    rows = []
    for index, close_price in enumerate(pattern):
        open_price = close_price + (0.03 if index % 2 == 0 else -0.03)
        high_price = min(100.0, max(open_price, close_price) + 0.12)
        low_price = max(99.0, min(open_price, close_price) - 0.12)
        if index in {1, 5, 9}:
            high_price = 100.0
        if index in {0, 4, 8}:
            low_price = 99.0
        rows.append(
            {
                "open": round(open_price, 6),
                "high": round(high_price, 6),
                "low": round(low_price, 6),
                "close": round(close_price, 6),
                "volume": 110.0 - float(index % 4),
            }
        )
    return rows


def test_format_pair_interval_handles_hours_minutes_and_bad_input():
    from trading_bot.core.bot import _format_pair_interval

    assert _format_pair_interval(60) == "1h"
    assert _format_pair_interval(360) == "6h"
    assert _format_pair_interval(120) == "2h"
    assert _format_pair_interval(30) == "30min"
    assert _format_pair_interval(90) == "90min"  # não múltiplo de 60
    assert _format_pair_interval(0) == "1min"  # piso
    assert _format_pair_interval("abc") == "?"
    assert _format_pair_interval(None) == "?"


def test_stop_keeps_open_positions_and_does_not_close_them(monkeypatch):
    bot = _make_light_bot()

    open_positions = [
        {
            "symbol": "ETHUSDT",
            "side": "LONG",
            "quantity": 0.5,
            "entry_price": 100.0,
            "unrealized_pnl": 2.5,
        }
    ]

    class ExchangeStub:
        def __init__(self):
            self.flush_called = False
            self.close_attempted = False

        def flush_retry_stats(self):
            self.flush_called = True

        def get_open_positions(self, force_refresh=False):
            return list(open_positions)

        def close_position(self, *_args, **_kwargs):
            self.close_attempted = True
            raise AssertionError("stop() não deve fechar posições abertas")

    class CommandHandlerStub:
        def __init__(self):
            self.stopped = False

        def stop_polling(self):
            self.stopped = True

    class TelegramStub:
        def __init__(self):
            self.shutdown_calls = []

        def send_shutdown_message(self, total_pnl, total_trades):
            self.shutdown_calls.append(
                {"total_pnl": total_pnl, "total_trades": total_trades}
            )
            return True

    exchange = ExchangeStub()
    command_handler = CommandHandlerStub()
    telegram = TelegramStub()

    bot.running = True
    bot.exchange = exchange
    bot.command_handler = command_handler
    bot.telegram = telegram
    bot.risk_manager = SimpleNamespace(daily_pnl=1.23)
    bot.trade_history = []
    bot.total_pnl = 10.0
    bot.closed_trades_count = 4
    bot.pnl_by_symbol = {"ETHUSDT": 3.0}

    state_saved = {"called": False}

    def _save_state():
        state_saved["called"] = True
        return True

    bot.save_state = _save_state

    monkeypatch.setattr(config, "TRADING_PAIRS", ["ETHUSDT"])

    bot.stop()

    assert bot.running is False
    assert command_handler.stopped is True
    assert exchange.flush_called is True
    assert exchange.close_attempted is False
    assert state_saved["called"] is True
    assert len(telegram.shutdown_calls) == 1
    assert telegram.shutdown_calls[0]["total_trades"] == 4
    # total_pnl enviado inclui não realizado
    assert telegram.shutdown_calls[0]["total_pnl"] == 12.5


def test_save_state_writes_atomic_file_and_backup(tmp_path):
    bot = _make_light_bot()
    state_file = tmp_path / "bot_state.json"
    state_file.write_text('{"closed_trades_count": 1}', encoding="utf-8")
    bot._state_file_path = str(state_file)

    assert bot.save_state() is True

    backup_path = tmp_path / "bot_state.json.bak"
    assert backup_path.exists()
    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert "version" in persisted
    assert (tmp_path / "bot_state.json.tmp").exists() is False


def test_save_state_persists_known_positions_with_custom_metadata(tmp_path):
    """Regression: known_positions era descartado no save → custom_tp/sl perdidos no restart."""
    bot = _make_light_bot()
    state_file = tmp_path / "bot_state.json"
    bot._state_file_path = str(state_file)

    bot.known_positions = {
        "ETHUSDT_LONG": {
            "symbol": "ETHUSDT",
            "side": "LONG",
            "entry_price": 2500.0,
            "quantity": 0.1,
            "last_seen": datetime(2026, 4, 22, 10, 30, 0, tzinfo=timezone.utc),
            "strategy_name": "trend_strong",
            "strategy_type": "trend_signal",
            "custom_take_profit": 2537.5,
            "custom_stop_loss": 2487.5,
            "range_mid_price": None,
            "range_entry_side": None,
        },
        "SOLUSDT_SHORT": {
            "symbol": "SOLUSDT",
            "side": "SHORT",
            "entry_price": 85.0,
            "quantity": 1.0,
            "last_seen": datetime(2026, 4, 22, 10, 30, 0, tzinfo=timezone.utc),
            "strategy_name": "range_scalping",
            "strategy_type": "range_scalping",
            "custom_take_profit": None,
            "custom_stop_loss": None,
            "range_mid_price": 85.5,
            "range_entry_side": "resistance",
        },
    }

    assert bot.save_state() is True

    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert "known_positions" in persisted
    assert set(persisted["known_positions"].keys()) == {"ETHUSDT_LONG", "SOLUSDT_SHORT"}
    # datetime virou ISO string
    assert isinstance(persisted["known_positions"]["ETHUSDT_LONG"]["last_seen"], str)
    # custom_tp/sl preservados
    assert persisted["known_positions"]["ETHUSDT_LONG"]["custom_take_profit"] == 2537.5
    assert persisted["known_positions"]["SOLUSDT_SHORT"]["range_mid_price"] == 85.5


def test_load_state_restores_known_positions_with_custom_metadata(tmp_path):
    bot = _make_light_bot()
    state_file = tmp_path / "bot_state.json"
    bot._state_file_path = str(state_file)

    state_file.write_text(
        json.dumps(
            {
                "daily_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "known_positions": {
                    "ETHUSDT_LONG": {
                        "symbol": "ETHUSDT",
                        "side": "LONG",
                        "entry_price": 2500.0,
                        "quantity": 0.1,
                        "last_seen": "2026-04-22T10:30:00+00:00",
                        "strategy_name": "trend_strong",
                        "strategy_type": "trend_signal",
                        "custom_take_profit": 2537.5,
                        "custom_stop_loss": 2487.5,
                        "range_mid_price": None,
                        "range_entry_side": None,
                    }
                },
                "strategy_profiles": list(getattr(config, "STRATEGY_PROFILES", []) or []),
            }
        ),
        encoding="utf-8",
    )

    assert bot.load_state() is True
    assert "ETHUSDT_LONG" in bot.known_positions
    entry = bot.known_positions["ETHUSDT_LONG"]
    assert entry["custom_take_profit"] == 2537.5
    assert entry["custom_stop_loss"] == 2487.5
    assert isinstance(entry["last_seen"], datetime)


def test_load_state_tolerates_missing_known_positions(tmp_path):
    """State antigo sem o campo known_positions não pode quebrar o load."""
    bot = _make_light_bot()
    state_file = tmp_path / "bot_state.json"
    bot._state_file_path = str(state_file)
    state_file.write_text(
        json.dumps(
            {
                "daily_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "closed_trades_count": 3,
                "strategy_profiles": list(getattr(config, "STRATEGY_PROFILES", []) or []),
            }
        ),
        encoding="utf-8",
    )

    assert bot.load_state() is True
    assert bot.known_positions == {}


def test_deserialize_known_positions_handles_malformed_entries():
    bot = _make_light_bot()
    raw = {
        "GOOD_LONG": {
            "symbol": "GOOD",
            "side": "LONG",
            "entry_price": 10.0,
            "quantity": 1.0,
            "last_seen": "2026-04-22T10:30:00+00:00",
        },
        "BAD_LAST_SEEN": {
            "symbol": "BAD",
            "side": "LONG",
            "last_seen": "not-a-date",  # ValueError no fromisoformat
        },
        "NOT_A_DICT": "oops",
    }

    result = bot._deserialize_known_positions(raw)

    assert "GOOD_LONG" in result
    assert isinstance(result["GOOD_LONG"]["last_seen"], datetime)
    # Entry ruim ainda é carregada, mas last_seen vira datetime.now() (não quebra)
    assert "BAD_LAST_SEEN" in result
    assert isinstance(result["BAD_LAST_SEEN"]["last_seen"], datetime)
    # Não-dict é ignorado
    assert "NOT_A_DICT" not in result


def test_save_state_persists_runtime_drawdown_limit(tmp_path, monkeypatch):
    bot = _make_light_bot()
    state_file = tmp_path / "bot_state.json"
    bot._state_file_path = str(state_file)

    monkeypatch.setattr(config, "MAX_DRAWDOWN_FROM_PEAK_PERCENT", 42.5)

    assert bot.save_state() is True

    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert persisted["max_drawdown_from_peak_percent"] == 42.5


def test_load_state_uses_backup_when_primary_is_corrupted(tmp_path):
    bot = _make_light_bot()
    state_file = tmp_path / "bot_state.json"
    backup_file = tmp_path / "bot_state.json.bak"
    bot._state_file_path = str(state_file)

    state_file.write_text("{invalid-json", encoding="utf-8")
    backup_file.write_text(
        json.dumps(
            {
                "daily_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "closed_trades_count": 9,
                "total_pnl": 12.34,
                "strategy_profiles": list(getattr(config, "STRATEGY_PROFILES", []) or []),
            }
        ),
        encoding="utf-8",
    )

    assert bot.load_state() is True
    assert bot.closed_trades_count == 9
    assert bot.total_pnl == 12.34


def test_load_state_restores_runtime_drawdown_limit(tmp_path, monkeypatch):
    bot = _make_light_bot()
    state_file = tmp_path / "bot_state.json"
    bot._state_file_path = str(state_file)

    monkeypatch.setattr(config, "MAX_DRAWDOWN_FROM_PEAK_PERCENT", 30.0)
    state_file.write_text(
        json.dumps(
            {
                "daily_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "max_drawdown_from_peak_percent": 55.0,
            }
        ),
        encoding="utf-8",
    )

    assert bot.load_state() is True
    assert config.MAX_DRAWDOWN_FROM_PEAK_PERCENT == 55.0


def test_load_state_ignores_backup_when_primary_is_empty(tmp_path):
    bot = _make_light_bot()
    state_file = tmp_path / "bot_state.json"
    backup_file = tmp_path / "bot_state.json.bak"
    bot._state_file_path = str(state_file)

    state_file.write_text("", encoding="utf-8")
    backup_file.write_text(
        json.dumps(
            {
                "daily_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "closed_trades_count": 9,
                "total_pnl": 12.34,
            }
        ),
        encoding="utf-8",
    )

    assert bot.load_state() is True
    assert bot.closed_trades_count == 0
    assert bot.total_pnl == 0.0


def test_execute_signal_trade_skips_fixed_sl_on_exchange_when_individual_sl_disabled(monkeypatch):
    bot = _make_light_bot()
    sltp_calls = []

    bot.exchange = SimpleNamespace(
        get_symbol_price=lambda _symbol: 100.0,
        get_symbol_info=lambda _symbol: {"minNotional": 5.0, "pricePrecision": 2},
        place_market_order=lambda **_kwargs: {"orderId": 1},
        set_stop_loss_take_profit=lambda **kwargs: sltp_calls.append(kwargs) or True,
        get_account_balance=lambda: 1000.0,
        get_symbol_cooldown_info=lambda _symbol: None,
    )
    bot.telegram = SimpleNamespace(send_trade_alert=lambda **_kwargs: True)
    bot._get_total_open_notional_percent = lambda: 0.0

    monkeypatch.setattr(config, "CHECK_FUNDING_RATE", False)
    monkeypatch.setattr(config, "USE_BINANCE_STRATEGY", False)
    monkeypatch.setattr(config, "USE_INDIVIDUAL_STOP_LOSS", False)
    monkeypatch.setattr(config, "LEVERAGE", 20)
    monkeypatch.setattr(config, "MAX_TOTAL_NOTIONAL_PERCENT", 999.0)
    monkeypatch.setattr(config, "MAX_POSITION_CONCENTRATION_PERCENT", 100.0)

    long_setup = TradeSetup(
        symbol="ETHUSDT",
        signal=Signal.STRONG_BUY,
        long_size=3.0,
        short_size=3.0,
        entry_price=100.0,
        stop_loss=99.0,
        take_profit=102.0,
        dca_levels=[],
    )
    assert bot.execute_signal_trade(long_setup, open_long=True, strategy_name="trend_strong") is True
    assert sltp_calls[-1]["position_side"] == "LONG"
    assert sltp_calls[-1]["stop_loss_price"] is None
    assert sltp_calls[-1]["take_profit_price"] == 102.0

    short_setup = TradeSetup(
        symbol="SOLUSDT",
        signal=Signal.STRONG_SELL,
        long_size=3.0,
        short_size=3.0,
        entry_price=100.0,
        stop_loss=101.0,
        take_profit=98.0,
        dca_levels=[],
    )
    assert bot.execute_signal_trade(short_setup, open_short=True, strategy_name="trend_strong") is True
    assert sltp_calls[-1]["position_side"] == "SHORT"
    assert sltp_calls[-1]["stop_loss_price"] is None
    assert sltp_calls[-1]["take_profit_price"] == 98.0


def test_execute_signal_trade_notifies_when_gated_ai_trade_is_blocked_by_exposure(monkeypatch):
    bot = _make_light_bot()
    send_trade_alert = MagicMock(return_value=True)
    notify_block = MagicMock(return_value=True)

    bot.exchange = SimpleNamespace(
        get_symbol_price=lambda _symbol: 100.0,
        get_symbol_info=lambda _symbol: {"minNotional": 5.0, "pricePrecision": 2},
        get_account_balance=lambda: 213.49,
        get_symbol_cooldown_info=lambda _symbol: None,
    )
    bot.telegram = SimpleNamespace(send_trade_alert=send_trade_alert, send_message=MagicMock(return_value=True))
    bot._get_total_open_notional_percent = lambda: 450.0
    # Engine agora chama bot.block_reporter.notify_blocked direto. O método
    # antigo bot._notify_ai_approved_trade_block ainda existe como delegate
    # mas mockar ele não é mais o ponto certo de interceptação.
    bot.block_reporter.notify_blocked = notify_block

    monkeypatch.setattr(config, "CHECK_FUNDING_RATE", False)
    monkeypatch.setattr(config, "USE_BINANCE_STRATEGY", False)
    monkeypatch.setattr(config, "USE_INDIVIDUAL_STOP_LOSS", False)
    monkeypatch.setattr(config, "LEVERAGE", 20)
    monkeypatch.setattr(config, "MAX_TOTAL_NOTIONAL_PERCENT", 300.0)
    monkeypatch.setattr(config, "MAX_POSITION_CONCENTRATION_PERCENT", 100.0)
    monkeypatch.setattr(config, "AI_CONSULTIVE_MODE", "gated")

    setup = TradeSetup(
        symbol="XRPUSDT",
        signal=Signal.STRONG_SELL,
        long_size=6.0,
        short_size=6.0,
        entry_price=100.0,
        stop_loss=100.5,
        take_profit=98.6,
        dca_levels=[],
    )
    setup.metadata = {
        "ai_consultive": {
            "approval": True,
            "confidence": 85,
            "decision": "ENTER_NOW",
        }
    }

    assert bot.execute_signal_trade(setup, open_short=True, strategy_name="trend_strong") is False
    send_trade_alert.assert_not_called()
    notify_block.assert_called_once()
    assert notify_block.call_args.kwargs["symbol"] == "XRPUSDT"
    assert notify_block.call_args.kwargs["side"] == "SHORT"
    assert notify_block.call_args.kwargs["strategy_name"] == "trend_strong"
    assert notify_block.call_args.kwargs["reason"] == "Exposição total excedida"
    assert "450.0%" in notify_block.call_args.kwargs["detail"]
    assert notify_block.call_args.kwargs["setup_metadata"]["ai_consultive"]["confidence"] == 85


def test_notify_ai_approved_trade_block_sends_telegram_message(monkeypatch):
    bot = _make_light_bot()
    send_message = MagicMock(return_value=True)
    bot.telegram = SimpleNamespace(send_message=send_message)

    monkeypatch.setattr(config, "AI_CONSULTIVE_MODE", "gated")
    monkeypatch.setattr("trading_bot.core.bot.time.monotonic", lambda: 100.0)

    result = bot._notify_ai_approved_trade_block(
        symbol="XRPUSDT",
        side="SHORT",
        strategy_name="trend_strong",
        reason="Exposição total excedida",
        detail="450.0% acima do limite de 300%",
        setup_metadata={
            "ai_consultive": {
                "approval": True,
                "confidence": 85,
                "decision": "ENTER_NOW",
            }
        },
    )

    assert result is True
    send_message.assert_called_once()
    message = send_message.call_args.args[0]
    assert "ENTRADA CANCELADA" in message
    assert "Exposição total excedida" in message
    assert "ENTER_NOW (85/100)" in message


def test_notify_ai_approved_trade_block_does_not_suppress_first_notification_when_monotonic_is_low(monkeypatch):
    bot = _make_light_bot()
    send_message = MagicMock(return_value=True)
    bot.telegram = SimpleNamespace(send_message=send_message)

    monkeypatch.setattr(config, "AI_CONSULTIVE_MODE", "gated")
    monkeypatch.setattr("trading_bot.core.bot.time.monotonic", lambda: 12.0)

    result = bot._notify_ai_approved_trade_block(
        symbol="SOLUSDT",
        side="LONG",
        strategy_name="trend_strong",
        reason="Exposição total excedida",
        detail="450.0% acima do limite de 300%",
        setup_metadata={
            "ai_consultive": {
                "approval": True,
                "confidence": 81,
                "decision": "ENTER_NOW",
            }
        },
    )

    assert result is True
    send_message.assert_called_once()


def test_monitor_positions_ignores_custom_stop_loss_when_individual_sl_disabled(monkeypatch):
    bot = _make_light_bot()

    position = {
        "symbol": "XRPUSDT",
        "side": "SHORT",
        "entry_price": 1.3720,
        "quantity": 43.7,
        "unrealized_pnl": -0.4,
        "mark_price": 1.3806,  # Acima do custom SL (1.3800)
    }

    bot.exchange = SimpleNamespace(
        get_open_positions=lambda *a, **kw: [dict(position)],
    )
    bot._close_position_with_notification = MagicMock(return_value=True)
    bot._process_binance_closed_position = MagicMock()

    bot.known_positions = {
        "XRPUSDT_SHORT": {
            "symbol": "XRPUSDT",
            "side": "SHORT",
            "entry_price": 1.3720,
            "quantity": 43.7,
            "strategy_name": "trend_strong",
            "strategy_type": "trend_signal",
            "custom_stop_loss": 1.3800,
            "custom_take_profit": None,
            "range_mid_price": None,
            "range_entry_side": "SHORT",
        }
    }

    monkeypatch.setattr(config, "USE_INDIVIDUAL_STOP_LOSS", False)
    monkeypatch.setattr(config, "USE_TRAILING_STOP", False)
    monkeypatch.setattr(config, "TAKE_PROFIT_PERCENT", 8.0)

    bot.monitor_positions()

    bot._close_position_with_notification.assert_not_called()


def test_trailing_stop_activates_and_closes_long_on_retrace(monkeypatch):
    bot = _make_light_bot()
    bot.peak_prices = {}
    bot.trailing_activated = {}
    bot.exchange = SimpleNamespace(
        get_funding_rate=lambda _symbol: {"rate_percent": 0.0}
    )
    bot.telegram = SimpleNamespace(
        send_trailing_stop_activated=lambda **_kwargs: True
    )

    monkeypatch.setattr(config, "CHECK_FUNDING_RATE", True)
    monkeypatch.setattr(config, "TRAILING_ACTIVATION_PERCENT", 0.20)
    monkeypatch.setattr(config, "TRAILING_DISTANCE_PERCENT", 0.12)

    key = "ETHUSDT_LONG"
    # Ativa trailing
    should_close, reason = bot._check_trailing_stop(
        key, "LONG", 100.0, 100.25, "ETHUSDT", 1.0
    )
    assert should_close is False
    assert reason == ""
    assert bot.trailing_activated[key] is True

    # Novo pico
    should_close, reason = bot._check_trailing_stop(
        key, "LONG", 100.0, 100.40, "ETHUSDT", 1.0
    )
    assert should_close is False
    assert reason == ""

    # Recuo que atinge trailing — fecha imediatamente (sem gate de lucro mínimo USD)
    should_close, reason = bot._check_trailing_stop(
        key, "LONG", 100.0, 100.27, "ETHUSDT", 1.0
    )
    assert should_close is True
    assert "Trailing Stop" in reason


def test_trailing_stop_activates_and_closes_short_on_retrace(monkeypatch):
    bot = _make_light_bot()
    bot.peak_prices = {}
    bot.trailing_activated = {}
    bot.exchange = SimpleNamespace(
        get_funding_rate=lambda _symbol: {"rate_percent": 0.0}
    )
    bot.telegram = SimpleNamespace(
        send_trailing_stop_activated=lambda **_kwargs: True
    )

    monkeypatch.setattr(config, "CHECK_FUNDING_RATE", True)
    monkeypatch.setattr(config, "TRAILING_ACTIVATION_PERCENT", 0.20)
    monkeypatch.setattr(config, "TRAILING_DISTANCE_PERCENT", 0.12)

    key = "ETHUSDT_SHORT"
    # Ativa trailing no SHORT
    should_close, reason = bot._check_trailing_stop(
        key, "SHORT", 100.0, 99.75, "ETHUSDT", 1.0
    )
    assert should_close is False
    assert reason == ""
    assert bot.trailing_activated[key] is True

    # Novo fundo
    should_close, reason = bot._check_trailing_stop(
        key, "SHORT", 100.0, 99.50, "ETHUSDT", 1.0
    )
    assert should_close is False
    assert reason == ""

    # Repique que atinge trailing no SHORT e mantém lucro mínimo
    should_close, reason = bot._check_trailing_stop(
        key, "SHORT", 100.0, 99.64, "ETHUSDT", 1.0
    )
    assert should_close is True
    assert "Trailing Stop" in reason


def test_trailing_stop_always_closes_when_hit_regardless_of_profit_usd(monkeypatch):
    """Trailing stop deve fechar SEMPRE ao atingir o stop, sem gate de lucro mínimo USD.
    Gate em USD bloqueava fechamento em posições pequenas (ex: $3 × 10x = $30 notional)
    onde profit_usd nunca atingia o mínimo configurado."""
    bot = _make_light_bot()
    bot.peak_prices = {}
    bot.trailing_activated = {}
    bot.exchange = SimpleNamespace(
        get_funding_rate=lambda _symbol: {"rate_percent": 0.0}
    )
    bot.telegram = SimpleNamespace(
        send_trailing_stop_activated=lambda **_kwargs: True
    )

    monkeypatch.setattr(config, "CHECK_FUNDING_RATE", False)
    monkeypatch.setattr(config, "TRAILING_ACTIVATION_PERCENT", 0.20)
    monkeypatch.setattr(config, "TRAILING_DISTANCE_PERCENT", 0.05)

    key = "ETHUSDT_LONG"
    position_amt = 0.01  # posição pequena: profit_usd seria centavos

    # Ativa trailing
    should_close, reason = bot._check_trailing_stop(
        key, "LONG", 100.0, 100.25, "ETHUSDT", position_amt
    )
    assert should_close is False
    assert reason == ""
    assert bot.trailing_activated[key] is True

    # Novo pico
    should_close, reason = bot._check_trailing_stop(
        key, "LONG", 100.0, 100.30, "ETHUSDT", position_amt
    )
    assert should_close is False
    assert reason == ""

    # Atinge trailing — deve fechar mesmo com profit_usd baixo (ex: $0.024)
    should_close, reason = bot._check_trailing_stop(
        key, "LONG", 100.0, 100.24, "ETHUSDT", position_amt
    )
    assert should_close is True
    assert "Trailing Stop" in reason


def test_trailing_stop_breakeven_floor_prevents_loss_from_fees(monkeypatch):
    """Regressão BNBUSDT 2026-04-20: activation 0.40% + distance 0.50% deixava
    o stop ~0.06% abaixo da entrada, permitindo saída em breakeven bruto que
    fees transformavam em prejuízo.

    Com breakeven-lock, o stop nunca fica abaixo de entry × (1 + fees_roundtrip),
    então saídas por trailing sempre cobrem as fees.
    """
    bot = _make_light_bot()
    bot.peak_prices = {}
    bot.trailing_activated = {}
    bot.commission_rates = {"taker_rate": 0.0004, "maker_rate": 0.0002}
    bot.exchange = SimpleNamespace(
        get_funding_rate=lambda _symbol: {"rate_percent": 0.0}
    )
    bot.telegram = SimpleNamespace(
        send_trailing_stop_activated=lambda **_kwargs: True
    )

    monkeypatch.setattr(config, "CHECK_FUNDING_RATE", False)
    monkeypatch.setattr(config, "TRAILING_ACTIVATION_PERCENT", 0.40)
    monkeypatch.setattr(config, "TRAILING_DISTANCE_PERCENT", 0.50)

    entry_price = 628.68
    # Piso esperado: entry × (1 + 2×0.0004 + 0.0005) = entry × 1.0013 = 629.497
    expected_floor = entry_price * (1 + 2 * 0.0004 + 0.0005)

    # Pico apenas 0.44% acima — raw trail seria 0.06% abaixo da entrada
    peak = entry_price * 1.0044
    stop = bot._trailing_stop_price("LONG", entry_price, peak)
    assert stop == pytest.approx(expected_floor)
    assert stop > entry_price, "Stop nunca pode ficar abaixo da entrada após ativar trailing"

    # Pico alto o suficiente: raw trail domina o piso
    high_peak = entry_price * 1.02  # +2%
    stop_high = bot._trailing_stop_price("LONG", entry_price, high_peak)
    raw_expected = high_peak * (1 - 0.005)
    assert stop_high == pytest.approx(raw_expected)

    # SHORT espelhado
    stop_short = bot._trailing_stop_price("SHORT", entry_price, entry_price * 0.9956)
    expected_ceiling = entry_price * (1 - 2 * 0.0004 - 0.0005)
    assert stop_short == pytest.approx(expected_ceiling)


def test_analyze_and_trade_skips_reentry_when_long_is_already_open(monkeypatch):
    bot = _make_light_bot()

    monkeypatch.setattr(config, "USE_DAILY_TARGETS", False)
    monkeypatch.setattr(config, "TIMEFRAME", "5m")
    monkeypatch.setattr(config, "CANDLES_LOOKBACK", 50)

    setup = TradeSetup(
        symbol="ETHUSDT",
        signal=Signal.BUY,
        long_size=5.0,
        short_size=5.0,
        entry_price=100.0,
        stop_loss=98.0,
        take_profit=102.0,
        dca_levels=[],
    )

    bot.exchange = SimpleNamespace(
        get_klines=lambda **_kwargs: [{"close": 100.0}],
        get_available_balance=lambda: 1000.0,
        get_symbol_info=lambda _symbol: {"minNotional": 5.0},
        get_open_positions=lambda *a, **kw: [
            {
                "symbol": "ETHUSDT",
                "side": "LONG",
                "quantity": 0.1,
                "entry_price": 100.0,
                "unrealized_pnl": 0.0,
            }
        ],
    )
    bot.strategy = SimpleNamespace(
        generate_trade_setup=lambda **_kwargs: setup
    )
    bot.risk_manager = SimpleNamespace(can_open_position=lambda _total: True)
    bot.execute_signal_trade = MagicMock(return_value=True)

    result = bot.analyze_and_trade("ETHUSDT")

    assert result is False
    bot.execute_signal_trade.assert_not_called()


def test_analyze_and_trade_ignores_weak_buy_signal(monkeypatch):
    bot = _make_light_bot()

    monkeypatch.setattr(config, "USE_DAILY_TARGETS", False)
    monkeypatch.setattr(config, "TIMEFRAME", "5m")
    monkeypatch.setattr(config, "CANDLES_LOOKBACK", 50)

    setup = TradeSetup(
        symbol="ETHUSDT",
        signal=Signal.BUY,
        long_size=5.0,
        short_size=5.0,
        entry_price=100.0,
        stop_loss=98.0,
        take_profit=102.0,
        dca_levels=[],
    )

    bot.exchange = SimpleNamespace(
        get_klines=lambda **_kwargs: [{"close": 100.0}],
        get_available_balance=lambda: 1000.0,
        get_symbol_info=lambda _symbol: {"minNotional": 5.0},
        get_open_positions=lambda *a, **kw: [],
    )
    bot.strategy = SimpleNamespace(generate_trade_setup=lambda **_kwargs: setup)
    bot.risk_manager = SimpleNamespace(can_open_position=lambda _total: True)
    bot.sentiment_mode_enabled = False
    bot.execute_signal_trade = MagicMock(return_value=True)

    result = bot.analyze_and_trade("ETHUSDT")

    assert result is False
    bot.execute_signal_trade.assert_not_called()


def test_analyze_and_trade_skips_reentry_when_short_is_already_open(monkeypatch):
    bot = _make_light_bot()

    monkeypatch.setattr(config, "USE_DAILY_TARGETS", False)
    monkeypatch.setattr(config, "TIMEFRAME", "5m")
    monkeypatch.setattr(config, "CANDLES_LOOKBACK", 50)

    setup = TradeSetup(
        symbol="ETHUSDT",
        signal=Signal.SELL,
        long_size=5.0,
        short_size=5.0,
        entry_price=100.0,
        stop_loss=102.0,
        take_profit=98.0,
        dca_levels=[],
    )

    bot.exchange = SimpleNamespace(
        get_klines=lambda **_kwargs: [{"close": 100.0}],
        get_available_balance=lambda: 1000.0,
        get_symbol_info=lambda _symbol: {"minNotional": 5.0},
        get_open_positions=lambda *a, **kw: [
            {
                "symbol": "ETHUSDT",
                "side": "SHORT",
                "quantity": 0.1,
                "entry_price": 100.0,
                "unrealized_pnl": 0.0,
            }
        ],
    )
    bot.strategy = SimpleNamespace(
        generate_trade_setup=lambda **_kwargs: setup
    )
    bot.risk_manager = SimpleNamespace(can_open_position=lambda _total: True)
    bot.execute_signal_trade = MagicMock(return_value=True)

    result = bot.analyze_and_trade("ETHUSDT")

    assert result is False
    bot.execute_signal_trade.assert_not_called()


def test_analyze_and_trade_ignores_weak_sell_signal(monkeypatch):
    bot = _make_light_bot()

    monkeypatch.setattr(config, "USE_DAILY_TARGETS", False)
    monkeypatch.setattr(config, "TIMEFRAME", "5m")
    monkeypatch.setattr(config, "CANDLES_LOOKBACK", 50)

    setup = TradeSetup(
        symbol="ETHUSDT",
        signal=Signal.SELL,
        long_size=5.0,
        short_size=5.0,
        entry_price=100.0,
        stop_loss=102.0,
        take_profit=98.0,
        dca_levels=[],
    )

    bot.exchange = SimpleNamespace(
        get_klines=lambda **_kwargs: [{"close": 100.0}],
        get_available_balance=lambda: 1000.0,
        get_symbol_info=lambda _symbol: {"minNotional": 5.0},
        get_open_positions=lambda *a, **kw: [],
    )
    bot.strategy = SimpleNamespace(generate_trade_setup=lambda **_kwargs: setup)
    bot.risk_manager = SimpleNamespace(can_open_position=lambda _total: True)
    bot.sentiment_mode_enabled = False
    bot.execute_signal_trade = MagicMock(return_value=True)

    result = bot.analyze_and_trade("ETHUSDT")

    assert result is False
    bot.execute_signal_trade.assert_not_called()


def test_analyze_and_trade_accepts_buy_signal_for_standard_profile(monkeypatch):
    bot = _make_light_bot()

    monkeypatch.setattr(config, "USE_DAILY_TARGETS", False)
    monkeypatch.setattr(config, "TIMEFRAME", "5m")
    monkeypatch.setattr(config, "CANDLES_LOOKBACK", 50)

    setup = TradeSetup(
        symbol="ETHUSDT",
        signal=Signal.BUY,
        long_size=5.0,
        short_size=5.0,
        entry_price=100.0,
        stop_loss=98.0,
        take_profit=102.0,
        dca_levels=[],
    )

    strategy = SimpleNamespace(generate_trade_setup=lambda **_kwargs: setup)
    bot.strategy_profiles = [
        {
            "name": "scalper_standard",
            "entry_mode": "standard",
            "pairs": ["ETHUSDT"],
            "strategy": strategy,
        }
    ]
    bot.strategy = strategy
    bot.exchange = SimpleNamespace(
        get_klines=lambda **_kwargs: [{"close": 100.0}],
        get_available_balance=lambda: 1000.0,
        get_symbol_info=lambda _symbol: {"minNotional": 5.0},
        get_open_positions=lambda *a, **kw: [],
    )
    bot.risk_manager = SimpleNamespace(can_open_position=lambda _total: True)
    bot.sentiment_mode_enabled = False
    bot.execute_signal_trade = MagicMock(return_value=True)

    result = bot.analyze_and_trade("ETHUSDT", strategy_name="scalper_standard")

    assert result is True
    bot.execute_signal_trade.assert_called_once()
    call_kwargs = bot.execute_signal_trade.call_args.kwargs
    assert call_kwargs["open_long"] is True
    assert call_kwargs["open_short"] is False


def test_analyze_and_trade_passes_risk_profile_for_trend_strategy(monkeypatch):
    bot = _make_light_bot()

    monkeypatch.setattr(config, "USE_DAILY_TARGETS", False)
    monkeypatch.setattr(config, "TIMEFRAME", "5m")
    monkeypatch.setattr(config, "CANDLES_LOOKBACK", 50)

    risk_profile = {
        "stop_loss_min_percent": 0.4,
        "stop_loss_max_percent": 0.6,
        "take_profit_min_percent": 0.8,
        "take_profit_max_percent": 1.2,
        "risk_reward_target": 2.0,
    }

    setup = TradeSetup(
        symbol="ETHUSDT",
        signal=Signal.STRONG_BUY,
        long_size=5.0,
        short_size=5.0,
        entry_price=100.0,
        stop_loss=99.5,
        take_profit=101.0,
        dca_levels=[],
    )

    captured_kwargs = {}

    def _generate_trade_setup(**kwargs):
        captured_kwargs.update(kwargs)
        return setup

    strategy = SimpleNamespace(generate_trade_setup=_generate_trade_setup)
    bot.strategy_profiles = [
        {
            "name": "trend_strong",
            "strategy_type": "trend_signal",
            "entry_mode": "strong_only",
            "risk_profile": dict(risk_profile),
            "pairs": ["ETHUSDT"],
            "strategy": strategy,
        }
    ]
    bot.strategy = strategy
    bot.exchange = SimpleNamespace(
        get_klines=lambda **_kwargs: [{"close": 100.0}],
        get_available_balance=lambda: 1000.0,
        get_symbol_info=lambda _symbol: {"minNotional": 5.0},
        get_open_positions=lambda *a, **kw: [],
    )
    bot.risk_manager = SimpleNamespace(can_open_position=lambda _total: True)
    bot.sentiment_mode_enabled = False
    bot.execute_signal_trade = MagicMock(return_value=True)

    result = bot.analyze_and_trade("ETHUSDT", strategy_name="trend_strong")

    assert result is True
    assert captured_kwargs.get("risk_profile") == risk_profile


def test_analyze_and_trade_runs_ai_consultive_review_without_blocking_execution(monkeypatch):
    bot = _make_light_bot()

    monkeypatch.setattr(config, "USE_DAILY_TARGETS", False)
    monkeypatch.setattr(config, "TIMEFRAME", "5m")
    monkeypatch.setattr(config, "CANDLES_LOOKBACK", 50)
    monkeypatch.setattr(config, "AI_CONSULTIVE_MODE", "consultive")

    setup = TradeSetup(
        symbol="ETHUSDT",
        signal=Signal.STRONG_BUY,
        long_size=5.0,
        short_size=5.0,
        entry_price=100.0,
        stop_loss=99.0,
        take_profit=102.0,
        dca_levels=[],
    )

    bot.exchange = SimpleNamespace(
        get_klines=lambda **_kwargs: [{"open": 99.0, "high": 101.0, "low": 98.5, "close": 100.0, "volume": 10.0}],
        get_available_balance=lambda: 1000.0,
        get_symbol_info=lambda _symbol: {"minNotional": 5.0},
        get_open_positions=lambda *a, **kw: [],
    )
    bot.strategy = SimpleNamespace(generate_trade_setup=lambda **_kwargs: setup)
    bot.risk_manager = SimpleNamespace(can_open_position=lambda _total: True)
    bot.sentiment_mode_enabled = False
    class AIStub:
        def is_enabled(self):
            return True

        def build_market_snapshot(self, **kwargs):
            return {
                "symbol": kwargs["symbol"],
                "strategy_name": kwargs["strategy_name"],
                "signal": kwargs["signal_name"],
                "side": "LONG",
            }

        def evaluate_setup(self, _snapshot):
            return SimpleNamespace(
                status="ok",
                decision="WAIT_PULLBACK",
                entry_side="LONG",
                approval=False,
                confidence=74,
                timing_score=7,
                risk_grade="B",
                entry_window_min=99.2,
                entry_window_max=99.8,
                wait_seconds=120,
                reasons=["pullback mais limpo esperado"],
                invalidators=["perda da EMA 21"],
                telegram_summary="Melhor aguardar pullback.",
                providers=[],
                from_cache=False,
                should_notify=True,
                error="",
                mode="consultive",
                compact_for_trade=lambda: {
                    "decision": "WAIT_PULLBACK",
                    "approval": False,
                    "confidence": 74,
                },
            )

        def build_telegram_message(self, _review):
            return "ai-message"

    bot.ai_consultive_engine = AIStub()
    bot.telegram = SimpleNamespace(send_message=MagicMock())
    bot.execute_signal_trade = MagicMock(return_value=True)

    result = bot.analyze_and_trade("ETHUSDT")

    assert result is True
    bot.telegram.send_message.assert_called_once_with("ai-message")
    bot.execute_signal_trade.assert_called_once()
    executed_setup = bot.execute_signal_trade.call_args.kwargs["setup"]
    assert executed_setup.metadata["ai_consultive"]["decision"] == "WAIT_PULLBACK"
    assert executed_setup.metadata["ai_consultive"]["confidence"] == 74


def test_analyze_and_trade_blocks_execution_when_ai_gated_review_is_not_positive(monkeypatch):
    bot = _make_light_bot()

    monkeypatch.setattr(config, "USE_DAILY_TARGETS", False)
    monkeypatch.setattr(config, "TIMEFRAME", "5m")
    monkeypatch.setattr(config, "CANDLES_LOOKBACK", 50)
    monkeypatch.setattr(config, "AI_CONSULTIVE_MODE", "gated")
    monkeypatch.setattr(config, "AI_CONSULTIVE_MIN_CONFIDENCE", 80)

    setup = TradeSetup(
        symbol="ETHUSDT",
        signal=Signal.STRONG_BUY,
        long_size=5.0,
        short_size=5.0,
        entry_price=100.0,
        stop_loss=99.0,
        take_profit=102.0,
        dca_levels=[],
    )

    class AIStub:
        def is_enabled(self):
            return True

        def build_market_snapshot(self, **kwargs):
            return {"symbol": kwargs["symbol"]}

        def evaluate_setup(self, _snapshot):
            return SimpleNamespace(
                status="ok",
                decision="WAIT_PULLBACK",
                entry_side="LONG",
                approval=False,
                confidence=74,
                timing_score=7,
                risk_grade="B",
                entry_window_min=99.2,
                entry_window_max=99.8,
                wait_seconds=120,
                reasons=["pullback mais limpo esperado"],
                invalidators=["perda da EMA 21"],
                telegram_summary="Melhor aguardar pullback.",
                providers=[],
                from_cache=False,
                should_notify=False,
                error="",
                mode="gated",
                compact_for_trade=lambda: {
                    "decision": "WAIT_PULLBACK",
                    "approval": False,
                    "confidence": 74,
                },
            )

        def build_telegram_message(self, _review):
            return "ai-message"

    bot.exchange = SimpleNamespace(
        get_klines=lambda **_kwargs: [{"open": 99.0, "high": 101.0, "low": 98.5, "close": 100.0, "volume": 10.0}],
        get_available_balance=lambda: 1000.0,
        get_symbol_info=lambda _symbol: {"minNotional": 5.0},
        get_open_positions=lambda *a, **kw: [],
    )
    bot.strategy = SimpleNamespace(generate_trade_setup=lambda **_kwargs: setup)
    bot.risk_manager = SimpleNamespace(can_open_position=lambda _total: True)
    bot.sentiment_mode_enabled = False
    bot.ai_consultive_engine = AIStub()
    bot.telegram = SimpleNamespace(send_message=MagicMock())
    bot.execute_signal_trade = MagicMock(return_value=True)

    result = bot.analyze_and_trade("ETHUSDT")

    assert result is False
    bot.execute_signal_trade.assert_not_called()


def test_analyze_and_trade_allows_execution_when_ai_gated_review_is_positive(monkeypatch):
    bot = _make_light_bot()

    monkeypatch.setattr(config, "USE_DAILY_TARGETS", False)
    monkeypatch.setattr(config, "TIMEFRAME", "5m")
    monkeypatch.setattr(config, "CANDLES_LOOKBACK", 50)
    monkeypatch.setattr(config, "AI_CONSULTIVE_MODE", "gated")
    monkeypatch.setattr(config, "AI_CONSULTIVE_MIN_CONFIDENCE", 80)

    setup = TradeSetup(
        symbol="ETHUSDT",
        signal=Signal.STRONG_BUY,
        long_size=5.0,
        short_size=5.0,
        entry_price=100.0,
        stop_loss=99.0,
        take_profit=102.0,
        dca_levels=[],
    )

    class AIStub:
        def is_enabled(self):
            return True

        def build_market_snapshot(self, **kwargs):
            return {"symbol": kwargs["symbol"]}

        def evaluate_setup(self, _snapshot):
            return SimpleNamespace(
                status="ok",
                decision="ENTER_NOW",
                entry_side="LONG",
                approval=True,
                confidence=82,
                timing_score=8,
                risk_grade="B",
                entry_window_min=99.2,
                entry_window_max=99.8,
                wait_seconds=0,
                reasons=["setup alinhado"],
                invalidators=["perda da EMA 21"],
                telegram_summary="Entrar agora.",
                providers=[],
                from_cache=False,
                should_notify=False,
                error="",
                mode="gated",
                compact_for_trade=lambda: {
                    "decision": "ENTER_NOW",
                    "approval": True,
                    "confidence": 82,
                },
            )

        def build_telegram_message(self, _review):
            return "ai-message"

    bot.exchange = SimpleNamespace(
        get_klines=lambda **_kwargs: [{"open": 99.0, "high": 101.0, "low": 98.5, "close": 100.0, "volume": 10.0}],
        get_available_balance=lambda: 1000.0,
        get_symbol_info=lambda _symbol: {"minNotional": 5.0},
        get_open_positions=lambda *a, **kw: [],
    )
    bot.strategy = SimpleNamespace(generate_trade_setup=lambda **_kwargs: setup)
    bot.risk_manager = SimpleNamespace(can_open_position=lambda _total: True)
    bot.sentiment_mode_enabled = False
    bot.ai_consultive_engine = AIStub()
    bot.telegram = SimpleNamespace(send_message=MagicMock())
    bot.execute_signal_trade = MagicMock(return_value=True)

    result = bot.analyze_and_trade("ETHUSDT")

    assert result is True
    bot.execute_signal_trade.assert_called_once()


def test_analyze_and_trade_allows_gated_ai_override_when_trend_strong_setup_is_neutral(monkeypatch):
    bot = _make_light_bot()

    monkeypatch.setattr(config, "USE_DAILY_TARGETS", False)
    monkeypatch.setattr(config, "TIMEFRAME", "5m")
    monkeypatch.setattr(config, "CANDLES_LOOKBACK", 50)
    monkeypatch.setattr(config, "AI_CONSULTIVE_MODE", "gated")
    monkeypatch.setattr(config, "AI_CONSULTIVE_MIN_CONFIDENCE", 80)
    monkeypatch.setattr(config, "TREND_STRONG_EXECUTION_TIMEFRAME", "3m")
    monkeypatch.setattr(config, "TREND_STRONG_CONFIRM_TIMEFRAME", "5m")
    monkeypatch.setattr(config, "TREND_STRONG_CANDLES_LOOKBACK", 220)

    candidate_setup = TradeSetup(
        symbol="ETHUSDT",
        signal=Signal.STRONG_BUY,
        long_size=5.0,
        short_size=5.0,
        entry_price=100.0,
        stop_loss=99.0,
        take_profit=102.0,
        dca_levels=[],
        metadata={
            "source_signal": "NEUTRAL",
            "ai_override_from_neutral": True,
            "trend_candidate_side": "LONG",
        },
    )

    klines = [
        {"open": 99.0, "high": 101.0, "low": 98.5, "close": 100.0, "volume": 10.0}
    ] * 240

    captured = {"override_called": 0}

    class StrategyStub:
        def generate_trade_setup(self, **_kwargs):
            return None

        def build_ai_override_candidate_setup(self, **kwargs):
            captured["override_called"] += 1
            assert kwargs["symbol"] == "ETHUSDT"
            return candidate_setup

    class AIStub:
        def is_enabled(self):
            return True

        def build_market_snapshot(self, **kwargs):
            assert kwargs["requested_side"] == "LONG"
            assert kwargs["allowed_entry_sides"] == ["LONG"]
            return {"symbol": kwargs["symbol"], "allowed_entry_sides": ["LONG"]}

        def evaluate_setup(self, _snapshot):
            return SimpleNamespace(
                status="ok",
                decision="ENTER_NOW",
                entry_side="LONG",
                approval=True,
                confidence=84,
                timing_score=8,
                risk_grade="B",
                entry_window_min=99.5,
                entry_window_max=100.2,
                wait_seconds=0,
                reasons=["tendência alinhada"],
                invalidators=["perda da EMA 21"],
                telegram_summary="Entrar agora.",
                providers=[],
                from_cache=False,
                should_notify=False,
                error="",
                mode="gated",
                compact_for_trade=lambda: {
                    "decision": "ENTER_NOW",
                    "entry_side": "LONG",
                    "approval": True,
                    "confidence": 84,
                },
            )

        def build_telegram_message(self, _review):
            return "ai-message"

    strategy = StrategyStub()
    bot.strategy_profiles = [
        {
            "name": "trend_strong",
            "strategy_type": "trend_signal",
            "entry_mode": "strong_only",
            "risk_profile": None,
            "pairs": ["ETHUSDT"],
            "strategy": strategy,
        }
    ]
    bot.strategy = strategy
    bot.exchange = SimpleNamespace(
        get_klines=lambda **_kwargs: klines,
        get_available_balance=lambda: 1000.0,
        get_symbol_info=lambda _symbol: {"minNotional": 5.0},
        get_open_positions=lambda *a, **kw: [],
    )
    bot.risk_manager = SimpleNamespace(can_open_position=lambda _total: True)
    bot.sentiment_mode_enabled = False
    bot.ai_consultive_engine = AIStub()
    bot.telegram = SimpleNamespace(send_message=MagicMock())
    bot.execute_signal_trade = MagicMock(return_value=True)

    result = bot.analyze_and_trade("ETHUSDT", strategy_name="trend_strong")

    assert result is True
    assert captured["override_called"] == 1
    bot.execute_signal_trade.assert_called_once()
    assert bot.execute_signal_trade.call_args.kwargs["open_long"] is True
    assert bot.execute_signal_trade.call_args.kwargs["open_short"] is False
    executed_setup = bot.execute_signal_trade.call_args.kwargs["setup"]
    assert executed_setup.metadata["ai_consultive"]["entry_side"] == "LONG"


def test_hedge_strategy_uses_balanced_risk_profile_with_rr_target():
    strategy = HedgeStrategy()
    risk_profile = {
        "stop_loss_min_percent": 0.4,
        "stop_loss_max_percent": 0.6,
        "take_profit_min_percent": 0.8,
        "take_profit_max_percent": 1.2,
        "risk_reward_target": 2.0,
    }

    stop_loss, take_profit = strategy.calculate_stop_loss_take_profit(
        entry_price=100.0,
        signal=Signal.STRONG_BUY,
        atr=0.4,  # 1.5*ATR = 0.6% de risco (bate no max do perfil)
        risk_profile=risk_profile,
    )

    assert stop_loss == 99.40
    assert take_profit == 101.20


def test_calculate_position_sizes_uses_single_leg_capital_in_directional_mode(monkeypatch):
    strategy = HedgeStrategy()
    monkeypatch.setattr(config, "USE_SIGNAL_STRATEGY", True)
    monkeypatch.setattr(config, "USE_MIN_NOTIONAL_ONLY", True)

    long_size, short_size = strategy.calculate_position_sizes(
        signal=Signal.STRONG_BUY,
        available_capital=7.0,  # cobre LONG mínimo + fees, mas não hedge completo
        min_notional=5.0,
    )

    assert long_size > 0
    assert short_size > 0


def test_calculate_position_sizes_requires_full_capital_in_hedge_mode(monkeypatch):
    strategy = HedgeStrategy()
    monkeypatch.setattr(config, "USE_SIGNAL_STRATEGY", False)
    monkeypatch.setattr(config, "USE_MIN_NOTIONAL_ONLY", True)

    long_size, short_size = strategy.calculate_position_sizes(
        signal=Signal.STRONG_BUY,
        available_capital=7.0,  # insuficiente para LONG+SHORT + fees
        min_notional=5.0,
    )

    assert long_size == 0.0
    assert short_size == 0.0


def test_calculate_position_sizes_marks_neutral_reason_in_directional_mode(monkeypatch):
    strategy = HedgeStrategy()
    monkeypatch.setattr(config, "USE_SIGNAL_STRATEGY", True)
    monkeypatch.setattr(config, "USE_MIN_NOTIONAL_ONLY", True)

    long_size, short_size = strategy.calculate_position_sizes(
        signal=Signal.NEUTRAL,
        available_capital=100.0,
        min_notional=5.0,
    )

    assert long_size == 0.0
    assert short_size == 0.0
    assert strategy._last_sizing_decision == "neutral_signal_directional"


def test_range_scalping_strategy_generates_setup_in_buy_zone(monkeypatch):
    strategy = RangeScalpingStrategy()

    monkeypatch.setattr(config, "TIMEFRAME", "5m")
    monkeypatch.setattr(config, "RANGE_SCALP_MIN_RANGE_PERCENT", 0.8)
    monkeypatch.setattr(config, "RANGE_SCALP_MIN_RANGE_MINUTES", 45)
    monkeypatch.setattr(config, "RANGE_SCALP_MIN_TOUCHES_PER_SIDE", 2)
    monkeypatch.setattr(config, "RANGE_SCALP_EDGE_ZONE_RATIO", 0.30)
    monkeypatch.setattr(config, "RANGE_SCALP_MIN_EDGE_PARTICIPATION", 0.30)
    monkeypatch.setattr(config, "RANGE_SCALP_MAX_VOLUME_RATIO", 1.20)
    monkeypatch.setattr(config, "RANGE_SCALP_MIN_RISK_REWARD", 1.0)
    monkeypatch.setattr(config, "MAX_POSITION_PERCENT", 0.08)

    klines = _make_range_klines(last_close=99.12)
    setup = strategy.generate_trade_setup(
        symbol="ETHUSDT",
        klines=klines,
        available_capital=300.0,
        min_notional=5.0,
    )

    assert setup is not None
    assert setup.signal == Signal.STRONG_BUY
    assert setup.long_size > 0
    assert setup.short_size == 0
    assert setup.metadata.get("strategy_type") == "range_scalping"
    assert setup.stop_loss < setup.entry_price < setup.take_profit


def test_range_scalping_strategy_skips_dead_zone_entries(monkeypatch):
    strategy = RangeScalpingStrategy()

    monkeypatch.setattr(config, "TIMEFRAME", "5m")
    monkeypatch.setattr(config, "RANGE_SCALP_MIN_RANGE_PERCENT", 0.8)
    monkeypatch.setattr(config, "RANGE_SCALP_MIN_RANGE_MINUTES", 45)
    monkeypatch.setattr(config, "RANGE_SCALP_MIN_TOUCHES_PER_SIDE", 2)
    monkeypatch.setattr(config, "RANGE_SCALP_EDGE_ZONE_RATIO", 0.30)
    monkeypatch.setattr(config, "RANGE_SCALP_MIN_EDGE_PARTICIPATION", 0.30)
    monkeypatch.setattr(config, "RANGE_SCALP_MAX_VOLUME_RATIO", 1.20)

    klines = _make_range_klines(last_close=99.50)
    setup = strategy.generate_trade_setup(
        symbol="ETHUSDT",
        klines=klines,
        available_capital=300.0,
        min_notional=5.0,
    )

    assert setup is None


def test_build_analysis_tasks_keeps_strategy_context(monkeypatch):
    bot = _make_light_bot()

    monkeypatch.setattr(config, "DISABLED_PAIRS", [])

    bot.strategy_profiles = [
        {
            "name": "strong_alpha",
            "entry_mode": "strong_only",
            "pairs": ["BTCUSDT"],
            "strategy": object(),
        },
        {
            "name": "normal_beta",
            "entry_mode": "standard",
            "pairs": ["ETHUSDT", "SOLUSDT"],
            "strategy": object(),
        },
    ]

    tasks = bot._build_analysis_tasks()

    assert tasks == [
        {"symbol": "BTCUSDT", "strategy_name": "strong_alpha"},
        {"symbol": "ETHUSDT", "strategy_name": "normal_beta"},
        {"symbol": "SOLUSDT", "strategy_name": "normal_beta"},
    ]


def test_get_primary_profile_info_forces_dynamic_primary_on_binance_mode(monkeypatch):
    bot = _make_light_bot()

    monkeypatch.setattr(config, "USE_BINANCE_STRATEGY", True)
    monkeypatch.setattr(
        config,
        "STRATEGY_PROFILES",
        [
            {
                "name": "trend_strong",
                "enabled": True,
                "strategy_type": "trend_signal",
                "entry_mode": "strong_only",
                # Estado antigo: pares preenchidos e sem max_pairs.
                "pairs": ["HYPEUSDT", "ETHUSDT", "ZECUSDT"],
            }
        ],
    )

    _profiles, _primary, primary_is_dynamic = bot._get_primary_profile_info()

    assert primary_is_dynamic is True


def test_resolve_primary_pair_target_uses_tier_as_base():
    target = TradingBot._resolve_primary_pair_target(
        primary_profile={"max_pairs": 10},
        strategy_num_coins=6,
        fallback_num_coins=3,
    )

    assert target == 6


def test_sync_strategy_profiles_preserves_fixed_pairs_ignores_trading_pairs(monkeypatch):
    """Quando o perfil primário já tem pares fixos, TRADING_PAIRS externo não deve
    injetar novos pares no perfil — o perfil é a fonte de verdade."""
    bot = _make_light_bot()
    bot.strategy = SimpleNamespace(generate_trade_setup=lambda **_kwargs: None)
    bot._strategy_engines = {}
    bot.strategy_profiles = []

    monkeypatch.setattr(config, "USE_BINANCE_STRATEGY", False)
    monkeypatch.setattr(config, "DISABLED_PAIRS", [])
    monkeypatch.setattr(
        config,
        "STRATEGY_PROFILES",
        [
            {
                "name": "alpha",
                "enabled": True,
                "entry_mode": "strong_only",
                "pairs": ["BTCUSDT"],
            }
        ],
    )
    # TRADING_PAIRS externo (ex.: seleção dinâmica Binance) não deve sobrescrever pares fixos
    monkeypatch.setattr(config, "TRADING_PAIRS", ["BTCUSDT", "XRPUSDT"])

    bot._sync_strategy_profiles_with_trading_pairs(reason="test-sync")

    # Pares fixos preservados, XRPUSDT não injetado
    assert config.STRATEGY_PROFILES[0]["pairs"] == ["BTCUSDT"]
    # TRADING_PAIRS derivado dos perfis (fonte de verdade)
    assert config.TRADING_PAIRS == ["BTCUSDT"]


def test_sync_strategy_profiles_adds_legacy_pairs_when_primary_empty(monkeypatch):
    """Quando o perfil primário não tem pares, TRADING_PAIRS externo é injetado
    (modo dinâmico / compatibilidade com configuração legada)."""
    bot = _make_light_bot()
    bot.strategy = SimpleNamespace(generate_trade_setup=lambda **_kwargs: None)
    bot._strategy_engines = {}
    bot.strategy_profiles = []

    monkeypatch.setattr(config, "DISABLED_PAIRS", [])
    monkeypatch.setattr(
        config,
        "STRATEGY_PROFILES",
        [
            {
                "name": "alpha",
                "enabled": True,
                "entry_mode": "strong_only",
                "pairs": [],
            }
        ],
    )
    monkeypatch.setattr(config, "TRADING_PAIRS", ["BTCUSDT", "XRPUSDT"])

    bot._sync_strategy_profiles_with_trading_pairs(reason="test-sync")

    assert config.STRATEGY_PROFILES[0]["pairs"] == ["BTCUSDT", "XRPUSDT"]
    assert config.TRADING_PAIRS == ["BTCUSDT", "XRPUSDT"]


def test_sync_strategy_profiles_treats_primary_as_dynamic_on_binance_mode(monkeypatch):
    bot = _make_light_bot()
    bot.strategy = SimpleNamespace(generate_trade_setup=lambda **_kwargs: None)
    bot._strategy_engines = {}
    bot.strategy_profiles = []

    monkeypatch.setattr(config, "USE_BINANCE_STRATEGY", True)
    monkeypatch.setattr(config, "DISABLED_PAIRS", [])
    monkeypatch.setattr(
        config,
        "STRATEGY_PROFILES",
        [
            {
                "name": "trend_strong",
                "enabled": True,
                "strategy_type": "trend_signal",
                "entry_mode": "strong_only",
                # Estado legado sem max_pairs.
                "pairs": ["HYPEUSDT", "ETHUSDT", "ZECUSDT"],
            }
        ],
    )
    monkeypatch.setattr(config, "TRADING_PAIRS", ["HYPEUSDT", "ETHUSDT", "ZECUSDT"])

    desired_pairs = ["DOGEUSDT", "ZECUSDT", "1000PEPEUSDT", "XRPUSDT", "HYPEUSDT", "ETHUSDT"]
    bot._sync_strategy_profiles_with_trading_pairs(
        reason="test-sync-binance-dynamic",
        primary_pairs=desired_pairs,
    )

    assert config.STRATEGY_PROFILES[0]["pairs"] == desired_pairs
    assert config.TRADING_PAIRS == desired_pairs


def test_reload_strategy_profiles_instantiates_range_engine(monkeypatch):
    bot = _make_light_bot()
    bot.strategy = SimpleNamespace(generate_trade_setup=lambda **_kwargs: None)
    bot._strategy_engines = {}
    bot.strategy_profiles = []

    monkeypatch.setattr(config, "DISABLED_PAIRS", [])
    monkeypatch.setattr(config, "TRADING_PAIRS", ["ETHUSDT", "XRPUSDT"])
    monkeypatch.setattr(
        config,
        "STRATEGY_PROFILES",
        [
            {
                "name": "trend",
                "enabled": True,
                "strategy_type": "trend_signal",
                "entry_mode": "strong_only",
                "pairs": ["ETHUSDT"],
            },
            {
                "name": "range",
                "enabled": True,
                "strategy_type": "range_scalping",
                "entry_mode": "strong_only",
                "pairs": ["XRPUSDT"],
            },
        ],
    )

    bot._reload_strategy_profiles(reason="test-range-engine")

    range_profile = next(
        profile for profile in bot.strategy_profiles if profile["name"] == "range"
    )
    assert range_profile["strategy_type"] == "range_scalping"
    assert isinstance(range_profile["strategy"], RangeScalpingStrategy)


def test_reload_strategy_profiles_preserves_trend_risk_profile(monkeypatch):
    bot = _make_light_bot()
    bot.strategy = SimpleNamespace(generate_trade_setup=lambda **_kwargs: None)
    bot._strategy_engines = {}
    bot.strategy_profiles = []

    monkeypatch.setattr(config, "DISABLED_PAIRS", [])
    monkeypatch.setattr(config, "TRADING_PAIRS", ["BTCUSDT"])
    monkeypatch.setattr(
        config,
        "STRATEGY_PROFILES",
        [
            {
                "name": "trend_strong",
                "enabled": True,
                "strategy_type": "trend_signal",
                "entry_mode": "strong_only",
                "pairs": ["BTCUSDT"],
                "risk_profile": {
                    "stop_loss_min_percent": 0.4,
                    "stop_loss_max_percent": 0.6,
                    "take_profit_min_percent": 0.8,
                    "take_profit_max_percent": 1.2,
                    "risk_reward_target": 2.0,
                },
            }
        ],
    )

    bot._reload_strategy_profiles(reason="test-trend-risk-profile")

    assert bot.strategy_profiles[0]["name"] == "trend_strong"
    assert bot.strategy_profiles[0]["risk_profile"]["risk_reward_target"] == 2.0
    assert config.STRATEGY_PROFILES[0]["risk_profile"]["stop_loss_min_percent"] == 0.4


def test_sync_strategy_profiles_excludes_reserved_pairs_from_dynamic_primary(monkeypatch):
    bot = _make_light_bot()
    bot.strategy = SimpleNamespace(generate_trade_setup=lambda **_kwargs: None)
    bot._strategy_engines = {}
    bot.strategy_profiles = []

    monkeypatch.setattr(config, "DISABLED_PAIRS", [])
    monkeypatch.setattr(
        config,
        "STRATEGY_PROFILES",
        [
            {
                "name": "trend_strong",
                "enabled": True,
                "strategy_type": "trend_signal",
                "entry_mode": "strong_only",
                "pairs": [],
            },
            {
                "name": "range_scalp_v1",
                "enabled": True,
                "strategy_type": "range_scalping",
                "entry_mode": "strong_only",
                "pairs": ["DOGEUSDT", "XRPUSDT"],
            },
        ],
    )
    monkeypatch.setattr(config, "TRADING_PAIRS", ["BTCUSDT", "DOGEUSDT", "ETHUSDT", "XRPUSDT"])

    bot._sync_strategy_profiles_with_trading_pairs(
        reason="test-dynamic-primary",
        primary_pairs=["BTCUSDT", "DOGEUSDT", "ETHUSDT", "XRPUSDT"],
    )

    primary = config.STRATEGY_PROFILES[0]
    secondary = config.STRATEGY_PROFILES[1]

    assert primary["pairs"] == ["BTCUSDT", "ETHUSDT"]
    assert secondary["pairs"] == ["DOGEUSDT", "XRPUSDT"]
    assert config.TRADING_PAIRS == ["BTCUSDT", "ETHUSDT", "DOGEUSDT", "XRPUSDT"]


def test_refresh_trading_pairs_uses_tier_count_when_max_pairs_is_higher(monkeypatch):
    bot = _make_light_bot()
    bot.strategy = SimpleNamespace(generate_trade_setup=lambda **_kwargs: None)
    bot._strategy_engines = {}
    bot.strategy_profiles = []
    bot.exchange = SimpleNamespace(set_leverage=lambda *_args, **_kwargs: True)

    monkeypatch.setattr(config, "USE_BINANCE_STRATEGY", True)
    monkeypatch.setattr(config, "AUTO_SELECT_PAIRS", False)
    monkeypatch.setattr(config, "DISABLED_PAIRS", [])
    monkeypatch.setattr(config, "TRADING_PAIRS", ["HYPEUSDT", "ETHUSDT", "ZECUSDT"])
    monkeypatch.setattr(
        config,
        "STRATEGY_PROFILES",
        [
            {
                "name": "trend_strong",
                "enabled": True,
                "strategy_type": "trend_signal",
                "entry_mode": "strong_only",
                "pairs": ["HYPEUSDT", "ETHUSDT", "ZECUSDT"],
                "max_pairs": 10,
            }
        ],
    )

    bot._reload_strategy_profiles(reason="test-refresh-tier")
    bot.binance_strategy = {"num_coins": 6}

    called = {}

    def _fake_sort(num_coins, exclude=None):
        called["num_coins"] = num_coins
        return [f"COIN{i}USDT" for i in range(1, num_coins + 1)]

    bot.sort_binance_coins_by_score = _fake_sort

    result = bot.refresh_trading_pairs(trigger_reason="test-tier")

    assert called["num_coins"] == 6
    assert len(result["new_pairs"]) == 6
    assert len(config.TRADING_PAIRS) == 6


def test_setup_exchange_restores_open_positions_for_reentry_tracking(monkeypatch):
    bot = _make_light_bot()

    now = datetime.now()
    open_positions = [
        {
            "symbol": "ETHUSDT",
            "side": "LONG",
            "quantity": 0.4,
            "entry_price": 100.0,
            "unrealized_pnl": 1.2,
            "last_seen": now,
        }
    ]

    bot.exchange = SimpleNamespace(
        set_hedge_mode=lambda: True,
        set_leverage=lambda _symbol, _lev: True,
        get_account_balance=lambda: 100.0,
        get_daily_pnl_from_binance=lambda: {"total": 0.0},
        get_open_positions=lambda *a, **kw: list(open_positions),
    )
    bot.telegram = SimpleNamespace(send_message=lambda *_args, **_kwargs: True)
    bot.update_commission_rates = lambda: None
    bot.pnl_by_symbol = {}
    bot.known_positions = {}
    bot.initial_capital = None
    bot.last_transfer_check_ts_ms = 0
    bot.processed_transfer_ids = []

    monkeypatch.setattr(config, "USE_BINANCE_STRATEGY", False)
    monkeypatch.setattr(config, "AUTO_SELECT_PAIRS", False)
    monkeypatch.setattr(config, "TRADING_PAIRS", ["ETHUSDT"])
    monkeypatch.setattr(config, "LEVERAGE", 20)

    ok = bot.setup_exchange()

    assert ok is True
    assert "ETHUSDT_LONG" in bot.known_positions
    assert bot.known_positions["ETHUSDT_LONG"]["quantity"] == 0.4


def test_check_for_deposit_updates_capital_and_refreshes_snapshot(monkeypatch):
    bot = _make_light_bot()

    strategy_refresh_calls = []
    save_state_calls = []
    telegram_messages = []

    bot.initial_capital = 200.0
    bot.last_transfer_check_ts_ms = 1_000
    bot.processed_transfer_ids = []
    bot.telegram = SimpleNamespace(
        send_message=lambda message: telegram_messages.append(message) or True
    )
    bot.exchange = SimpleNamespace(
        get_income_history=lambda **_kwargs: [
            {"time": 2_000, "asset": "USDT", "income": "100.0", "tranId": "abc123"}
        ],
        get_account_info=lambda: {
            "wallet_balance": 300.0,
            "unrealized_pnl": 0.0,
        },
        get_daily_pnl_from_binance=lambda: {"total": 0.0},
    )
    bot.check_and_update_binance_strategy = lambda: strategy_refresh_calls.append(True)
    bot.save_state = lambda: save_state_calls.append(True) or True

    monkeypatch.setattr(config, "CAPITAL_TRANSFER_DETECTION_ENABLED", True)
    monkeypatch.setattr(config, "CAPITAL_TRANSFER_MIN_ABS_USDT", 1.0)
    monkeypatch.setattr(config, "CAPITAL_TRANSFER_TRACKED_IDS_LIMIT", 500)
    monkeypatch.setattr(config, "USE_BINANCE_STRATEGY", True)

    detected = bot.check_for_deposit()

    assert detected is True
    assert bot.initial_capital == 300.0
    assert bot.processed_transfer_ids == ["tranId:abc123"]
    assert strategy_refresh_calls == [True]
    assert save_state_calls == [True]
    assert len(bot.portfolio_history) == 1
    assert bot.portfolio_history[0]["balance"] == 300.0
    assert telegram_messages


def test_sort_binance_coins_by_score_uses_dynamic_binance_universe(monkeypatch):
    bot = _make_light_bot()

    all_symbols = ["XRPUSDT", "ETHUSDT", "ADAUSDT"]
    fake_tickers = {s: {"quoteVolume": "1000000000", "lastPrice": "1.0"} for s in all_symbols}

    bot.exchange = SimpleNamespace(
        get_all_tickers_24h=lambda: fake_tickers,
        get_all_funding_rates=lambda: {},
    )

    monkeypatch.setattr(config, "DISABLED_PAIRS", ["ETHUSDT"])
    monkeypatch.setattr(config, "BINANCE_COIN_LIST", ["OLDUSDT"])
    monkeypatch.setattr(config, "MIN_VOLUME_24H_USD", 0)

    score_map = {
        "ADAUSDT": 90.0,
        "XRPUSDT": 70.0,
        "ETHUSDT": 99.0,  # desabilitado
    }

    class PairSelectorStub:
        def get_all_futures_pairs(self):
            return all_symbols

        def get_pair_metrics(self, symbol, prefetched_ticker=None, prefetched_funding_rate=None):
            return {"symbol": symbol}

        def score_pair(self, metrics):
            return score_map.get(metrics["symbol"], 0.0)

    bot.pair_selector = PairSelectorStub()

    best = bot.sort_binance_coins_by_score(2)

    assert best == ["ADAUSDT", "XRPUSDT"]
    assert config.BINANCE_COIN_LIST == ["XRPUSDT", "ETHUSDT", "ADAUSDT"]


def test_sort_binance_coins_by_score_keeps_previous_universe_on_refresh_failure(monkeypatch):
    bot = _make_light_bot()

    all_symbols = ["BNBUSDT", "XRPUSDT"]
    fake_tickers = {s: {"quoteVolume": "1000000000", "lastPrice": "1.0"} for s in all_symbols}

    bot.exchange = SimpleNamespace(
        get_all_tickers_24h=lambda: fake_tickers,
        get_all_funding_rates=lambda: {},
    )

    monkeypatch.setattr(config, "DISABLED_PAIRS", [])
    monkeypatch.setattr(config, "BINANCE_COIN_LIST", ["BNBUSDT", "XRPUSDT"])
    monkeypatch.setattr(config, "MIN_VOLUME_24H_USD", 0)

    score_map = {"BNBUSDT": 80.0, "XRPUSDT": 60.0}

    class PairSelectorStub:
        def get_all_futures_pairs(self):
            return []

        def get_pair_metrics(self, symbol, prefetched_ticker=None, prefetched_funding_rate=None):
            return {"symbol": symbol}

        def score_pair(self, metrics):
            return score_map.get(metrics["symbol"], 0.0)

    bot.pair_selector = PairSelectorStub()

    best = bot.sort_binance_coins_by_score(1)

    assert best == ["BNBUSDT"]
    assert config.BINANCE_COIN_LIST == ["BNBUSDT", "XRPUSDT"]


def test_close_position_does_not_account_when_exchange_close_fails():
    bot = _make_light_bot()

    bot.exchange = SimpleNamespace(
        get_current_price=lambda _symbol: 101.0,
        close_position=lambda _symbol, _side: False,
    )
    bot.get_taker_fee_rate = lambda: 0.0005
    bot.risk_manager = SimpleNamespace(update_pnl=MagicMock())
    bot.telegram = SimpleNamespace(send_position_closed=MagicMock(return_value=True))

    bot.closed_trades_count = 0
    bot.daily_realized_pnl = 0.0
    bot.total_pnl = 0.0
    bot.trades_win_count = 0
    bot.trades_loss_count = 0
    bot.trades_win_total = 0.0
    bot.trades_loss_total = 0.0
    bot.total_fees_paid = 0.0
    bot.trades_by_symbol = {}
    bot.pnl_by_symbol = {}

    pos = {
        "symbol": "ETHUSDT",
        "side": "LONG",
        "entry_price": 100.0,
        "quantity": 0.5,
        "leverage": 20,
    }

    closed = bot._close_position_with_notification(pos, "Take Profit")

    assert closed is False
    assert bot.closed_trades_count == 0
    assert bot.daily_realized_pnl == 0.0
    assert bot.total_pnl == 0.0
    assert bot.trades_win_count == 0
    assert bot.trades_loss_count == 0
    assert bot.trades_win_total == 0.0
    assert bot.trades_loss_total == 0.0
    assert bot.total_fees_paid == 0.0
    assert bot.trades_by_symbol == {}
    assert bot.pnl_by_symbol == {}
    bot.risk_manager.update_pnl.assert_not_called()
    bot.telegram.send_position_closed.assert_not_called()


def test_check_global_stop_loss_ignores_invalid_initial_capital():
    bot = _make_light_bot()

    bot.exchange = SimpleNamespace(
        get_account_info=lambda: {"wallet_balance": 100.0, "unrealized_pnl": -5.0},
        get_daily_pnl_from_binance=lambda: {"total": -10.0},
    )
    bot.initial_capital = 0.0

    assert bot.check_global_stop_loss() is False


def test_execute_global_stop_loss_handles_invalid_initial_capital():
    bot = _make_light_bot()

    bot.exchange = SimpleNamespace(
        get_open_positions=lambda *a, **kw: [],
        get_account_info=lambda: {"wallet_balance": 120.0, "unrealized_pnl": -8.0},
        get_daily_pnl_from_binance=lambda: {"total": -12.0},
    )
    bot.telegram = SimpleNamespace(send_global_stop_loss_alert=MagicMock())
    bot.save_state = MagicMock(return_value=True)
    bot.initial_capital = 0.0
    bot.running = True

    bot.execute_global_stop_loss()

    assert bot.running is False
    assert bot.telegram.send_global_stop_loss_alert.call_count == 1
    call_kwargs = bot.telegram.send_global_stop_loss_alert.call_args.kwargs
    assert call_kwargs["initial_capital"] == 120.0


def test_double_first_global_applies_once_per_side(monkeypatch):
    bot = _make_light_bot()
    bot.double_first_used = {}

    monkeypatch.setattr(config, "DOUBLE_FIRST_LONG_ENABLED", True)
    monkeypatch.setattr(config, "DOUBLE_FIRST_SHORT_ENABLED", True)
    monkeypatch.setattr(config, "DOUBLE_FIRST_MULTIPLIER", 2.0)
    monkeypatch.setattr(config, "DOUBLE_FIRST_MAX_MARGIN_USDT", 0.0)
    monkeypatch.setattr(config, "DOUBLE_FIRST_SCOPE", "global")

    long_size, long_applied, long_key = bot._apply_double_first_order_size("ETHUSDT", "LONG", 3.0)
    assert long_applied is True
    assert long_key == "LONG"
    assert long_size == 6.0

    bot._mark_double_first_used(
        state_key=long_key,
        symbol="ETHUSDT",
        side="LONG",
        base_order_size=3.0,
        applied_order_size=long_size,
    )

    long_size_2, long_applied_2, long_key_2 = bot._apply_double_first_order_size("ETHUSDT", "LONG", 3.0)
    assert long_applied_2 is False
    assert long_key_2 == ""
    assert long_size_2 == 3.0

    short_size, short_applied, short_key = bot._apply_double_first_order_size("ETHUSDT", "SHORT", 3.0)
    assert short_applied is True
    assert short_key == "SHORT"
    assert short_size == 6.0


def test_double_first_symbol_scope_respects_cap_and_tracks_per_symbol(monkeypatch):
    bot = _make_light_bot()
    bot.double_first_used = {}

    monkeypatch.setattr(config, "DOUBLE_FIRST_LONG_ENABLED", True)
    monkeypatch.setattr(config, "DOUBLE_FIRST_SHORT_ENABLED", False)
    monkeypatch.setattr(config, "DOUBLE_FIRST_MULTIPLIER", 2.0)
    monkeypatch.setattr(config, "DOUBLE_FIRST_MAX_MARGIN_USDT", 5.0)
    monkeypatch.setattr(config, "DOUBLE_FIRST_SCOPE", "symbol")

    eth_size, eth_applied, eth_key = bot._apply_double_first_order_size("ETHUSDT", "LONG", 3.0)
    assert eth_applied is True
    assert eth_key == "ETHUSDT_LONG"
    assert eth_size == 5.0

    bot._mark_double_first_used(
        state_key=eth_key,
        symbol="ETHUSDT",
        side="LONG",
        base_order_size=3.0,
        applied_order_size=eth_size,
    )

    eth_size_2, eth_applied_2, _ = bot._apply_double_first_order_size("ETHUSDT", "LONG", 3.0)
    assert eth_applied_2 is False
    assert eth_size_2 == 3.0

    xrp_size, xrp_applied, xrp_key = bot._apply_double_first_order_size("XRPUSDT", "LONG", 3.0)
    assert xrp_applied is True
    assert xrp_key == "XRPUSDT_LONG"
    assert xrp_size == 5.0


def test_normalize_double_first_state_accepts_legacy_formats():
    bot = _make_light_bot()

    normalized_from_list = bot._normalize_double_first_state(
        ["long", "ETHUSDT_short", "invalid_key"]
    )
    assert normalized_from_list == {"LONG": True, "ETHUSDT_SHORT": True}

    normalized_from_dict = bot._normalize_double_first_state(
        {"short": True, "BTCUSDT_LONG": 1, "foo": True, "ADAUSDT_SHORT": False}
    )
    assert normalized_from_dict == {"SHORT": True, "BTCUSDT_LONG": True}


def test_analyze_and_trade_blocks_signal_when_sentiment_conflicts(monkeypatch):
    bot = _make_light_bot()

    monkeypatch.setattr(config, "USE_DAILY_TARGETS", False)
    monkeypatch.setattr(config, "TIMEFRAME", "5m")
    monkeypatch.setattr(config, "CANDLES_LOOKBACK", 50)

    setup = TradeSetup(
        symbol="ETHUSDT",
        signal=Signal.SELL,
        long_size=5.0,
        short_size=5.0,
        entry_price=100.0,
        stop_loss=102.0,
        take_profit=98.0,
        dca_levels=[],
    )

    bot.exchange = SimpleNamespace(
        get_klines=lambda **_kwargs: [{"close": 100.0}],
        get_available_balance=lambda: 1000.0,
        get_symbol_info=lambda _symbol: {"minNotional": 5.0},
        get_open_positions=lambda *a, **kw: [],
    )
    bot.strategy = SimpleNamespace(generate_trade_setup=lambda **_kwargs: setup)
    bot.risk_manager = SimpleNamespace(can_open_position=lambda _total: True)
    bot.execute_signal_trade = MagicMock(return_value=True)

    bot.sentiment_mode_enabled = True
    bot._get_symbol_sentiment = lambda _symbol, force_refresh=False: {
        "direction": "LONG_ONLY",
        "bias": "BULLISH",
        "score": 3,
    }

    result = bot.analyze_and_trade("ETHUSDT")

    assert result is False
    bot.execute_signal_trade.assert_not_called()


# ----------------------------------------------------------------------------
# Environment switch (mainnet/testnet) — guards
# ----------------------------------------------------------------------------

def test_switch_environment_rejects_invalid_target(monkeypatch):
    bot = _make_light_bot()
    monkeypatch.setattr(config, "ENVIRONMENT", "testnet")

    ok, message = bot.switch_environment("prod")

    assert ok is False
    assert "inválida" in message.lower()


def test_switch_environment_rejects_when_already_on_target(monkeypatch):
    bot = _make_light_bot()
    monkeypatch.setattr(config, "ENVIRONMENT", "testnet")

    ok, message = bot.switch_environment("testnet")

    assert ok is False
    assert "já está" in message.lower()


def test_switch_environment_rejects_when_credentials_missing(monkeypatch):
    bot = _make_light_bot()
    monkeypatch.setattr(config, "ENVIRONMENT", "testnet")
    monkeypatch.setattr(config, "MAINNET_API_KEY", "")
    monkeypatch.setattr(config, "MAINNET_API_SECRET", "")

    ok, message = bot.switch_environment("mainnet")

    assert ok is False
    assert "credenciais" in message.lower()
    assert "MAINNET" in message


def test_switch_environment_rejects_when_positions_are_open(monkeypatch):
    bot = _make_light_bot()
    monkeypatch.setattr(config, "ENVIRONMENT", "testnet")
    monkeypatch.setattr(config, "MAINNET_API_KEY", "abc")
    monkeypatch.setattr(config, "MAINNET_API_SECRET", "xyz")
    monkeypatch.setattr(config, "MAINNET_PROMOTION_GATE_ENABLED", False)

    bot.positions = {"ETHUSDT": {"side": "LONG", "quantity": 1.0}}

    ok, message = bot.switch_environment("mainnet")

    assert ok is False
    assert "bloqueada" in message.lower()
    assert "ETHUSDT" in message


def test_switch_environment_blocks_when_expectancy_below_threshold(monkeypatch):
    bot = _make_light_bot()
    monkeypatch.setattr(config, "ENVIRONMENT", "testnet")
    monkeypatch.setattr(config, "MAINNET_API_KEY", "abc")
    monkeypatch.setattr(config, "MAINNET_API_SECRET", "xyz")
    monkeypatch.setattr(config, "MAINNET_PROMOTION_GATE_ENABLED", True)
    monkeypatch.setattr(config, "MAINNET_PROMOTION_MIN_TRADES", 10)
    monkeypatch.setattr(config, "MAINNET_PROMOTION_MIN_EXPECTANCY", 0.10)

    # 20 trades, 80% WR mas RR invertido — expectativa quase zero.
    bot.closed_trades_count = 20
    bot.trades_win_count = 16
    bot.trades_loss_count = 4
    bot.trades_win_total = 16 * 0.10  # avg win 0.10
    bot.trades_loss_total = -4 * 1.20  # avg loss -1.20  (RR 0.083)

    ok, message = bot.switch_environment("mainnet")

    assert ok is False
    assert "expectativa" in message.lower()


def test_switch_environment_blocks_when_not_enough_trades(monkeypatch):
    bot = _make_light_bot()
    monkeypatch.setattr(config, "ENVIRONMENT", "testnet")
    monkeypatch.setattr(config, "MAINNET_API_KEY", "abc")
    monkeypatch.setattr(config, "MAINNET_API_SECRET", "xyz")
    monkeypatch.setattr(config, "MAINNET_PROMOTION_GATE_ENABLED", True)
    monkeypatch.setattr(config, "MAINNET_PROMOTION_MIN_TRADES", 100)

    bot.closed_trades_count = 5

    ok, message = bot.switch_environment("mainnet")

    assert ok is False
    assert "5" in message and "100" in message


def test_has_credentials_for_returns_correctly(monkeypatch):
    monkeypatch.setattr(config, "MAINNET_API_KEY", "key-m")
    monkeypatch.setattr(config, "MAINNET_API_SECRET", "sec-m")
    monkeypatch.setattr(config, "TESTNET_API_KEY", "")
    monkeypatch.setattr(config, "TESTNET_API_SECRET", "")

    assert config.has_credentials_for("mainnet") is True
    assert config.has_credentials_for("testnet") is False
    assert config.has_credentials_for("invalid") is False


def test_active_api_key_follows_environment(monkeypatch):
    monkeypatch.setattr(config, "MAINNET_API_KEY", "mainnet-key")
    monkeypatch.setattr(config, "MAINNET_API_SECRET", "mainnet-secret")
    monkeypatch.setattr(config, "TESTNET_API_KEY", "testnet-key")
    monkeypatch.setattr(config, "TESTNET_API_SECRET", "testnet-secret")

    monkeypatch.setattr(config, "ENVIRONMENT", "testnet")
    assert config.API_KEY == "testnet-key"
    assert config.API_SECRET == "testnet-secret"
    assert config.USE_TESTNET is True

    monkeypatch.setattr(config, "ENVIRONMENT", "mainnet")
    assert config.API_KEY == "mainnet-key"
    assert config.API_SECRET == "mainnet-secret"
    assert config.USE_TESTNET is False


def test_persist_active_environment_writes_file(monkeypatch, tmp_path):
    target_file = tmp_path / "active_environment.txt"
    monkeypatch.setattr(config, "ACTIVE_ENV_FILE_PATH", str(target_file))
    monkeypatch.setattr(config, "ENVIRONMENT", "mainnet")

    config.persist_active_environment()

    assert target_file.read_text(encoding="utf-8") == "mainnet"


# ----------------------------------------------------------------------------
# API Health classification (threshold CRÍTICO / ATENÇÃO / ESTÁVEL)
# ----------------------------------------------------------------------------

def test_health_classification_two_failures_in_many_calls_is_not_critical():
    """Regressão: 2 falhas em 82k calls (0.0024%) não deve disparar CRÍTICO."""
    status = TradingBot._classify_api_health_status(
        failures=2, failure_rate=0.0024,
        order_failures=0, order_rejection_rate=0.0,
        loop_errors=0, has_issues=True,
    )
    assert status == "ATENÇÃO"


def test_health_classification_many_failures_is_critical():
    status = TradingBot._classify_api_health_status(
        failures=15, failure_rate=0.5,
        order_failures=0, order_rejection_rate=0.0,
        loop_errors=0, has_issues=True,
    )
    assert status == "CRÍTICO"


def test_health_classification_high_failure_rate_is_critical():
    status = TradingBot._classify_api_health_status(
        failures=5, failure_rate=2.5,  # > 1% → crítico
        order_failures=0, order_rejection_rate=0.0,
        loop_errors=0, has_issues=True,
    )
    assert status == "CRÍTICO"


def test_health_classification_loop_error_is_always_critical():
    """Erro de loop indica bug — sempre crítico, independente de outros sinais."""
    status = TradingBot._classify_api_health_status(
        failures=0, failure_rate=0.0,
        order_failures=0, order_rejection_rate=0.0,
        loop_errors=1, has_issues=True,
    )
    assert status == "CRÍTICO"


def test_health_classification_order_rejections_over_threshold_is_critical():
    status = TradingBot._classify_api_health_status(
        failures=0, failure_rate=0.0,
        order_failures=0, order_rejection_rate=8.0,  # > 5% → crítico
        loop_errors=0, has_issues=True,
    )
    assert status == "CRÍTICO"


def test_health_classification_all_clean_is_stable():
    status = TradingBot._classify_api_health_status(
        failures=0, failure_rate=0.0,
        order_failures=0, order_rejection_rate=0.0,
        loop_errors=0, has_issues=False,
    )
    assert status == "ESTÁVEL"


def test_health_classification_minor_issues_is_atencao():
    """Retries sem falhas → ATENÇÃO, não CRÍTICO."""
    status = TradingBot._classify_api_health_status(
        failures=0, failure_rate=0.0,
        order_failures=0, order_rejection_rate=0.0,
        loop_errors=0, has_issues=True,  # só retries/overruns
    )
    assert status == "ATENÇÃO"


def _make_bot_with_drawdown_alert_stubs(initial_capital=100.0):
    """Bot mínimo com telegram stub pra testar _maybe_send_drawdown_alert."""
    bot = _make_light_bot()
    bot.initial_capital = initial_capital
    sent = []
    bot.telegram = SimpleNamespace(send_message=lambda text: sent.append(text) or True)
    return bot, sent


def test_drawdown_alert_fires_on_first_bucket_crossed():
    """Drawdown cruzando o primeiro bucket (3%) dispara 1 alerta."""
    bot, sent = _make_bot_with_drawdown_alert_stubs(initial_capital=100.0)
    now = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
    snap = {"pnl_total": -4.0, "pnl_realized": -1.0, "pnl_unrealized": -3.0}

    bot._maybe_send_drawdown_alert(snap, now)

    assert len(sent) == 1
    assert "DRAWDOWN" in sent[0]
    assert bot._drawdown_alert_bucket_pct == 3.0


def test_drawdown_alert_does_not_repeat_same_bucket():
    """Mesmo bucket não dispara segundo alerta."""
    bot, sent = _make_bot_with_drawdown_alert_stubs(initial_capital=100.0)
    now = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
    snap = {"pnl_total": -4.0, "pnl_realized": -1.0, "pnl_unrealized": -3.0}

    bot._maybe_send_drawdown_alert(snap, now)
    bot._maybe_send_drawdown_alert(snap, now)
    bot._maybe_send_drawdown_alert(snap, now)

    assert len(sent) == 1


def test_drawdown_alert_fires_on_higher_bucket():
    """Drawdown piorando pra próximo bucket dispara novo alerta."""
    bot, sent = _make_bot_with_drawdown_alert_stubs(initial_capital=100.0)
    now = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)

    bot._maybe_send_drawdown_alert(
        {"pnl_total": -4.0, "pnl_realized": 0, "pnl_unrealized": -4.0}, now
    )  # bucket 3%
    bot._maybe_send_drawdown_alert(
        {"pnl_total": -9.0, "pnl_realized": 0, "pnl_unrealized": -9.0}, now
    )  # bucket 8%

    assert len(sent) == 2
    assert bot._drawdown_alert_bucket_pct == 8.0


def test_drawdown_alert_resets_on_new_day():
    """Bucket reseta ao virar do dia."""
    bot, sent = _make_bot_with_drawdown_alert_stubs(initial_capital=100.0)
    day1 = datetime(2026, 5, 1, 23, 0, 0, tzinfo=timezone.utc)
    day2 = datetime(2026, 5, 2, 1, 0, 0, tzinfo=timezone.utc)
    snap = {"pnl_total": -4.0, "pnl_realized": 0, "pnl_unrealized": -4.0}

    bot._maybe_send_drawdown_alert(snap, day1)
    bot._maybe_send_drawdown_alert(snap, day2)

    assert len(sent) == 2  # dispara em cada dia


def test_drawdown_alert_skipped_when_positive_pnl():
    """PnL positivo não dispara alerta e zera bucket."""
    bot, sent = _make_bot_with_drawdown_alert_stubs(initial_capital=100.0)
    now = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
    bot._drawdown_alert_bucket_pct = 5.0  # estado simulado de alerta anterior

    bot._maybe_send_drawdown_alert(
        {"pnl_total": 2.0, "pnl_realized": 1.0, "pnl_unrealized": 1.0}, now
    )

    assert sent == []
    assert bot._drawdown_alert_bucket_pct == 0.0


def test_drawdown_alert_skipped_when_no_initial_capital():
    """Sem initial_capital, não há % de referência — alerta inativo."""
    bot, sent = _make_bot_with_drawdown_alert_stubs(initial_capital=0.0)
    now = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)

    bot._maybe_send_drawdown_alert(
        {"pnl_total": -50.0, "pnl_realized": -50.0, "pnl_unrealized": 0.0}, now
    )

    assert sent == []


def test_process_binance_closed_position_enriches_trade_history():
    """Regressão (fantasmas): um SL/TP executado server-side pela Binance
    (USE_INDIVIDUAL_STOP_LOSS=True) deve FECHAR a entrada aberta no
    trade_history — antes este path só bumpava contadores e deixava a
    entrada como 'Aberta' pra sempre, divergindo do closed_trades_count."""
    bot = _make_light_bot()

    bot.trade_history = [
        {
            "timestamp": datetime.now().isoformat(),
            "symbol": "ETHUSDT",
            "side": "LONG",
            "entry_price": 100.0,
            "qty": 1.0,
            "strategy_name": "primary",
        }
    ]
    bot.known_positions = {
        "ETHUSDT_LONG": {
            "symbol": "ETHUSDT",
            "side": "LONG",
            "strategy_name": "primary",
        }
    }
    bot.risk_manager = SimpleNamespace(update_pnl=lambda *_a, **_k: None)
    bot.telegram = SimpleNamespace(send_message=lambda *_a, **_k: True)
    bot.get_taker_fee_rate = lambda: 0.0004

    class ExchangeStub:
        def get_income_history(self, **_kwargs):
            return [{"income": "1.50"}]  # gross realizado +$1.50

        def get_current_price(self, *_a, **_k):
            return 101.5

    bot.exchange = ExchangeStub()

    closed_before = bot.closed_trades_count
    bot._process_binance_closed_position(
        {
            "symbol": "ETHUSDT",
            "side": "LONG",
            "entry_price": 100.0,
            "quantity": 1.0,
            "entry_time": datetime.now(timezone.utc),
        }
    )

    entry = bot.trade_history[0]
    assert entry.get("exit_price") and entry["exit_price"] > 0
    assert entry.get("exit_time")
    assert entry.get("close_reason") == "Take Profit (Binance)"
    assert entry.get("pnl_gross") == pytest.approx(1.50)
    # Contador sobe exatamente 1 (sem dupla contagem) e não sobra fantasma.
    assert bot.closed_trades_count == closed_before + 1
    assert all(t.get("exit_price") for t in bot.trade_history)

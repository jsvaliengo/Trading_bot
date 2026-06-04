"""Testes do BotStatePersistence — (de)serialização pura do state, sem TradingBot."""

from datetime import datetime, timezone
from types import SimpleNamespace

from trading_bot.core.bot_state_serde import BotStatePersistence


def test_serialize_converts_last_seen_to_iso_and_skips_non_dict():
    kp = {
        "ETHUSDT_LONG": {"symbol": "ETHUSDT", "side": "LONG",
                          "last_seen": datetime(2026, 5, 31, 10, 0, 0)},
        "BAD": "not-a-dict",
    }
    out = BotStatePersistence.serialize_known_positions(kp)
    assert "BAD" not in out
    assert out["ETHUSDT_LONG"]["last_seen"] == "2026-05-31T10:00:00"


def test_serialize_deserialize_roundtrip():
    original = {
        "ETHUSDT_LONG": {
            "symbol": "ETHUSDT", "side": "LONG", "entry_price": 2500.0,
            "quantity": 1.0, "last_seen": datetime(2026, 5, 31, 10, 0, 0),
            "custom_stop_loss": 2400.0,
        }
    }
    ser = BotStatePersistence.serialize_known_positions(original)
    deser = BotStatePersistence.deserialize_known_positions(ser)
    assert isinstance(deser["ETHUSDT_LONG"]["last_seen"], datetime)
    assert deser["ETHUSDT_LONG"]["entry_price"] == 2500.0
    assert deser["ETHUSDT_LONG"]["custom_stop_loss"] == 2400.0


def test_deserialize_handles_non_dict_input():
    assert BotStatePersistence.deserialize_known_positions("oops") == {}
    assert BotStatePersistence.deserialize_known_positions(None) == {}


def _fake_bot():
    """Bot duck-typed com só os atributos que build_payload lê."""
    return SimpleNamespace(
        start_time=datetime(2026, 5, 1, 0, 0, 0),
        initial_capital=130.0,
        closed_trades_count=42,
        total_pnl=12.5,
        daily_realized_pnl=3.0,
        pnl_by_symbol={"ETHUSDT": 12.5},
        trades_win_count=30,
        trades_loss_count=12,
        trades_win_total=50.0,
        trades_loss_total=-37.5,
        total_fees_paid=4.2,
        daily_pnl_binance_baseline=1.0,
        _daily_baseline_date="2026-05-31",
        peak_prices={"ETHUSDT": 2600.0},
        trailing_activated={"ETHUSDT_LONG": True},
        symbol_reentry_cooldowns={"BCHUSDT": 1717500000.0},
        known_positions={"ETHUSDT_LONG": {"symbol": "ETHUSDT", "side": "LONG",
                                          "last_seen": datetime(2026, 5, 31, 10, 0, 0)}},
        double_first_used={},
        kill_switch=None,
        sentiment_mode_enabled=False,
        invert_signals=False,
        last_daily_performance_report_date="",
        last_transfer_check_ts_ms=0,
        processed_transfer_ids=[],
    )


def test_build_payload_shape_and_excludes_history_arrays():
    persistence = BotStatePersistence(_fake_bot())
    payload = persistence.build_payload()

    # Versão nova + sem os arrays movidos pro SQLite
    assert payload["version"] == "1.9"
    assert "trade_history" not in payload
    assert "portfolio_history" not in payload

    # Contadores e estado quente presentes
    assert payload["closed_trades_count"] == 42
    assert payload["total_pnl"] == 12.5
    assert payload["pnl_by_symbol"] == {"ETHUSDT": 12.5}

    # known_positions serializado (last_seen vira ISO string)
    assert payload["known_positions"]["ETHUSDT_LONG"]["last_seen"] == "2026-05-31T10:00:00"

    # kill_switch None => {}
    assert payload["kill_switch"] == {}

    # cooldowns de reentrada persistidos (sobrevivem a deploy)
    assert payload["symbol_reentry_cooldowns"] == {"BCHUSDT": 1717500000.0}

    # daily_date é a data UTC de hoje (formato YYYY-MM-DD)
    assert payload["daily_date"] == datetime.now(timezone.utc).strftime("%Y-%m-%d")

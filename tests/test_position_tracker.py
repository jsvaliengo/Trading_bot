"""
Testes do PositionTracker (Phase 4 — terceiro pedaço).

Cobre as 5 operações da API + o schema completo do payload de `open()`.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace


from trading_bot.core.position_tracker import PositionTracker


def _make_bot() -> SimpleNamespace:
    return SimpleNamespace(
        known_positions={},
        peak_prices={},
        trailing_activated={},
        _positions_lock=threading.Lock(),
    )


# ---------- set / get / remove ----------


def test_set_writes_defensive_copy_of_payload():
    bot = _make_bot()
    tracker = PositionTracker(bot)
    payload = {"symbol": "BTCUSDT", "side": "LONG", "entry_price": 50000.0}
    tracker.set("BTCUSDT_LONG", payload)

    assert bot.known_positions["BTCUSDT_LONG"] == payload
    # Mutar o payload externo NÃO deve afetar o armazenado
    payload["entry_price"] = 999.0
    assert bot.known_positions["BTCUSDT_LONG"]["entry_price"] == 50000.0


def test_get_returns_defensive_copy():
    bot = _make_bot()
    tracker = PositionTracker(bot)
    tracker.set("BTCUSDT_LONG", {"symbol": "BTCUSDT", "entry_price": 50000.0})

    snap = tracker.get("BTCUSDT_LONG")
    snap["entry_price"] = 0.0
    # Mutar o snapshot NÃO afeta o storage
    assert bot.known_positions["BTCUSDT_LONG"]["entry_price"] == 50000.0


def test_get_returns_empty_dict_for_unknown_key():
    bot = _make_bot()
    tracker = PositionTracker(bot)
    assert tracker.get("UNKNOWN") == {}


def test_remove_is_idempotent():
    bot = _make_bot()
    tracker = PositionTracker(bot)
    tracker.set("X_LONG", {"symbol": "X"})
    tracker.remove("X_LONG")
    assert "X_LONG" not in bot.known_positions
    # Segunda remoção não falha
    tracker.remove("X_LONG")


# ---------- clear_trailing ----------


def test_clear_trailing_removes_both_peak_and_activation():
    bot = _make_bot()
    bot.peak_prices["BTCUSDT_LONG"] = 51000.0
    bot.trailing_activated["BTCUSDT_LONG"] = True
    tracker = PositionTracker(bot)

    tracker.clear_trailing("BTCUSDT_LONG")
    assert "BTCUSDT_LONG" not in bot.peak_prices
    assert "BTCUSDT_LONG" not in bot.trailing_activated


def test_clear_trailing_is_idempotent_when_keys_missing():
    bot = _make_bot()
    tracker = PositionTracker(bot)
    # Não deve raise
    tracker.clear_trailing("NEVER_OPENED")


# ---------- close (composto) ----------


def test_close_removes_known_position_and_clears_trailing():
    bot = _make_bot()
    tracker = PositionTracker(bot)
    tracker.set("BTCUSDT_LONG", {"symbol": "BTCUSDT"})
    bot.peak_prices["BTCUSDT_LONG"] = 52000.0
    bot.trailing_activated["BTCUSDT_LONG"] = True

    tracker.close("BTCUSDT_LONG")

    assert "BTCUSDT_LONG" not in bot.known_positions
    assert "BTCUSDT_LONG" not in bot.peak_prices
    assert "BTCUSDT_LONG" not in bot.trailing_activated


def test_close_works_when_only_known_position_exists():
    bot = _make_bot()
    tracker = PositionTracker(bot)
    tracker.set("X_LONG", {"symbol": "X"})
    # Trailing nunca foi ativado — close ainda funciona
    tracker.close("X_LONG")
    assert "X_LONG" not in bot.known_positions


# ---------- open (schema completo) ----------


def test_open_builds_full_payload_with_required_fields():
    bot = _make_bot()
    tracker = PositionTracker(bot)
    key = tracker.open(
        symbol="ETHUSDT", side="LONG", entry_price=3000.0, quantity=0.1,
        strategy_name="trend_strong", strategy_type="trend_signal",
    )
    assert key == "ETHUSDT_LONG"
    pos = bot.known_positions["ETHUSDT_LONG"]
    assert pos["symbol"] == "ETHUSDT"
    assert pos["side"] == "LONG"
    assert pos["entry_price"] == 3000.0
    assert pos["quantity"] == 0.1
    assert pos["strategy_name"] == "trend_strong"
    assert pos["strategy_type"] == "trend_signal"
    # Campos opcionais ficam None por default
    assert pos["custom_stop_loss"] is None
    assert pos["custom_take_profit"] is None
    assert pos["range_mid_price"] is None
    assert pos["range_entry_side"] is None
    assert pos["trailing_activation_pct"] is None
    assert pos["trailing_distance_pct"] is None


def test_open_includes_entry_time_and_last_seen():
    bot = _make_bot()
    tracker = PositionTracker(bot)
    tracker.open(
        symbol="BTCUSDT", side="LONG", entry_price=50000.0, quantity=0.01,
        strategy_name="x", strategy_type="trend_signal",
    )
    pos = bot.known_positions["BTCUSDT_LONG"]
    assert pos["entry_time"] is not None
    assert pos["last_seen"] is not None
    # entry_time e last_seen são criados no mesmo instante
    assert pos["entry_time"] == pos["last_seen"]


def test_open_propagates_all_optional_fields():
    bot = _make_bot()
    tracker = PositionTracker(bot)
    tracker.open(
        symbol="SOLUSDT", side="SHORT", entry_price=100.0, quantity=10.0,
        strategy_name="range_scalp", strategy_type="range_scalping",
        custom_stop_loss=102.0,
        custom_take_profit=98.0,
        range_mid_price=100.5,
        range_entry_side="SHORT",
        trailing_activation_pct=0.75,
        trailing_distance_pct=0.45,
    )
    pos = bot.known_positions["SOLUSDT_SHORT"]
    assert pos["custom_stop_loss"] == 102.0
    assert pos["custom_take_profit"] == 98.0
    assert pos["range_mid_price"] == 100.5
    assert pos["range_entry_side"] == "SHORT"
    assert pos["trailing_activation_pct"] == 0.75
    assert pos["trailing_distance_pct"] == 0.45


def test_open_normalizes_empty_strategy_name_to_primary():
    bot = _make_bot()
    tracker = PositionTracker(bot)
    tracker.open(
        symbol="X", side="LONG", entry_price=1.0, quantity=1.0,
        strategy_name="", strategy_type="trend_signal",
    )
    assert bot.known_positions["X_LONG"]["strategy_name"] == "primary"


# ---------- Lock behavior ----------


def test_set_and_remove_use_positions_lock():
    """Sanity: as operações de write tomam o lock pelo menos uma vez."""
    bot = _make_bot()

    acquired = {"count": 0}
    real_lock = bot._positions_lock

    class CountingLock:
        def __enter__(self):
            acquired["count"] += 1
            return real_lock.__enter__()
        def __exit__(self, *a):
            return real_lock.__exit__(*a)

    bot._positions_lock = CountingLock()
    tracker = PositionTracker(bot)

    tracker.set("X", {"symbol": "X"})
    tracker.get("X")
    tracker.remove("X")
    assert acquired["count"] == 3

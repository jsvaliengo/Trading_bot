"""
Testes do DoubleFirstPolicy (Phase 4 — quinto pedaço).

Cobre: scope (global/symbol), is_enabled (LONG/SHORT independentes),
state_key, try_double (eligibility + cap + idempotência), mark_used,
normalize_state (dict, list legada, sanitização).
"""

from __future__ import annotations

from types import SimpleNamespace


from trading_bot.core.config import config
from trading_bot.core.double_first_policy import DoubleFirstPolicy


def _make_bot() -> SimpleNamespace:
    return SimpleNamespace(double_first_used={})


# ---------- scope ----------


def test_scope_defaults_to_global(monkeypatch):
    monkeypatch.delattr(config, "DOUBLE_FIRST_SCOPE", raising=False)
    policy = DoubleFirstPolicy(_make_bot())
    assert policy.scope() == "global"


def test_scope_accepts_global_and_symbol(monkeypatch):
    policy = DoubleFirstPolicy(_make_bot())
    monkeypatch.setattr(config, "DOUBLE_FIRST_SCOPE", "symbol")
    assert policy.scope() == "symbol"
    monkeypatch.setattr(config, "DOUBLE_FIRST_SCOPE", "global")
    assert policy.scope() == "global"


def test_scope_invalid_value_falls_back_to_global(monkeypatch):
    policy = DoubleFirstPolicy(_make_bot())
    monkeypatch.setattr(config, "DOUBLE_FIRST_SCOPE", "garbage")
    assert policy.scope() == "global"


# ---------- is_enabled ----------


def test_is_enabled_long_and_short_independent(monkeypatch):
    policy = DoubleFirstPolicy(_make_bot())
    monkeypatch.setattr(config, "DOUBLE_FIRST_LONG_ENABLED", True)
    monkeypatch.setattr(config, "DOUBLE_FIRST_SHORT_ENABLED", False)
    assert policy.is_enabled("LONG") is True
    assert policy.is_enabled("SHORT") is False


def test_normalize_side_maps_casing_and_defaults_non_short_to_long():
    policy = DoubleFirstPolicy(_make_bot())
    # _normalize_side: case-insensitive; qualquer valor != SHORT vira LONG.
    assert policy._normalize_side("long") == "LONG"
    assert policy._normalize_side("SHORT") == "SHORT"
    assert policy._normalize_side("buy") == "LONG"  # default não-SHORT vira LONG


# ---------- state_key ----------


def test_state_key_global_uses_only_side(monkeypatch):
    monkeypatch.setattr(config, "DOUBLE_FIRST_SCOPE", "global")
    policy = DoubleFirstPolicy(_make_bot())
    assert policy.state_key("BTCUSDT", "LONG") == "LONG"
    assert policy.state_key("ETHUSDT", "LONG") == "LONG"
    assert policy.state_key("BTCUSDT", "SHORT") == "SHORT"


def test_state_key_symbol_scope_combines_pair_and_side(monkeypatch):
    monkeypatch.setattr(config, "DOUBLE_FIRST_SCOPE", "symbol")
    policy = DoubleFirstPolicy(_make_bot())
    assert policy.state_key("BTCUSDT", "LONG") == "BTCUSDT_LONG"
    assert policy.state_key("ethusdt", "short") == "ETHUSDT_SHORT"


# ---------- try_double ----------


def _enable_global(monkeypatch, multiplier: float = 2.0, max_margin: float = 0.0):
    monkeypatch.setattr(config, "DOUBLE_FIRST_LONG_ENABLED", True)
    monkeypatch.setattr(config, "DOUBLE_FIRST_SHORT_ENABLED", True)
    monkeypatch.setattr(config, "DOUBLE_FIRST_MULTIPLIER", multiplier)
    monkeypatch.setattr(config, "DOUBLE_FIRST_MAX_MARGIN_USDT", max_margin)
    monkeypatch.setattr(config, "DOUBLE_FIRST_SCOPE", "global")


def test_try_double_doubles_first_eligible_call(monkeypatch):
    _enable_global(monkeypatch)
    policy = DoubleFirstPolicy(_make_bot())
    size, applied, key = policy.try_double("BTCUSDT", "LONG", 3.0)
    assert applied is True
    assert key == "LONG"
    assert size == 6.0


def test_try_double_idempotent_after_mark_used(monkeypatch):
    _enable_global(monkeypatch)
    bot = _make_bot()
    policy = DoubleFirstPolicy(bot)
    _, _, key = policy.try_double("BTCUSDT", "LONG", 3.0)
    policy.mark_used(key, "BTCUSDT", "LONG", 3.0, 6.0)
    # Segunda chamada não dobra
    size, applied, _ = policy.try_double("BTCUSDT", "LONG", 3.0)
    assert applied is False
    assert size == 3.0


def test_try_double_skips_when_disabled_for_side(monkeypatch):
    monkeypatch.setattr(config, "DOUBLE_FIRST_LONG_ENABLED", False)
    monkeypatch.setattr(config, "DOUBLE_FIRST_SHORT_ENABLED", True)
    monkeypatch.setattr(config, "DOUBLE_FIRST_MULTIPLIER", 2.0)
    monkeypatch.setattr(config, "DOUBLE_FIRST_MAX_MARGIN_USDT", 0.0)
    monkeypatch.setattr(config, "DOUBLE_FIRST_SCOPE", "global")
    policy = DoubleFirstPolicy(_make_bot())
    _, long_applied, _ = policy.try_double("X", "LONG", 3.0)
    _, short_applied, _ = policy.try_double("X", "SHORT", 3.0)
    assert long_applied is False
    assert short_applied is True


def test_try_double_skips_when_multiplier_le_one(monkeypatch):
    _enable_global(monkeypatch, multiplier=1.0)
    policy = DoubleFirstPolicy(_make_bot())
    _, applied, _ = policy.try_double("X", "LONG", 3.0)
    assert applied is False


def test_try_double_respects_max_margin_cap(monkeypatch):
    _enable_global(monkeypatch, multiplier=10.0, max_margin=5.0)
    policy = DoubleFirstPolicy(_make_bot())
    size, applied, _ = policy.try_double("X", "LONG", 3.0)
    # base=3 × 10 = 30, capado em 5
    assert applied is True
    assert size == 5.0


def test_try_double_skips_when_cap_blocks_doubling(monkeypatch):
    """Cap menor ou igual ao base: nada muda, aplica=False."""
    _enable_global(monkeypatch, multiplier=2.0, max_margin=3.0)
    policy = DoubleFirstPolicy(_make_bot())
    size, applied, _ = policy.try_double("X", "LONG", 3.0)
    # base=3, multiplied=6, capado em 3 → resultado igual ao base → not applied
    assert applied is False
    assert size == 3.0


def test_try_double_with_invalid_size_returns_unchanged(monkeypatch):
    _enable_global(monkeypatch)
    policy = DoubleFirstPolicy(_make_bot())
    size, applied, _ = policy.try_double("X", "LONG", -1.0)
    assert applied is False
    assert size == -1.0


def test_try_double_symbol_scope_tracks_per_pair(monkeypatch):
    _enable_global(monkeypatch)
    monkeypatch.setattr(config, "DOUBLE_FIRST_SCOPE", "symbol")
    bot = _make_bot()
    policy = DoubleFirstPolicy(bot)
    _, applied_btc, key_btc = policy.try_double("BTCUSDT", "LONG", 3.0)
    policy.mark_used(key_btc, "BTCUSDT", "LONG", 3.0, 6.0)
    # BTC já usado, mas ETH ainda elegível
    _, applied_eth, _ = policy.try_double("ETHUSDT", "LONG", 3.0)
    assert applied_btc is True
    assert applied_eth is True


# ---------- mark_used ----------


def test_mark_used_records_state(monkeypatch):
    _enable_global(monkeypatch)
    bot = _make_bot()
    policy = DoubleFirstPolicy(bot)
    policy.mark_used("LONG", "X", "LONG", 3.0, 6.0)
    assert bot.double_first_used["LONG"] is True


def test_mark_used_empty_key_is_noop():
    bot = _make_bot()
    policy = DoubleFirstPolicy(bot)
    policy.mark_used("", "X", "LONG", 3.0, 6.0)
    assert bot.double_first_used == {}


def test_mark_used_creates_state_dict_when_missing():
    bot = SimpleNamespace()  # sem double_first_used
    policy = DoubleFirstPolicy(bot)
    policy.mark_used("LONG", "X", "LONG", 3.0, 6.0)
    assert hasattr(bot, "double_first_used")
    assert bot.double_first_used["LONG"] is True


# ---------- normalize_state ----------


def test_normalize_state_accepts_dict_format():
    raw = {"LONG": True, "SHORT": False, "BTCUSDT_LONG": True}
    result = DoubleFirstPolicy.normalize_state(raw)
    assert result == {"LONG": True, "BTCUSDT_LONG": True}


def test_normalize_state_accepts_legacy_list_format():
    raw = ["LONG", "BTCUSDT_SHORT"]
    result = DoubleFirstPolicy.normalize_state(raw)
    assert result == {"LONG": True, "BTCUSDT_SHORT": True}


def test_normalize_state_filters_invalid_keys():
    raw = {"LONG": True, "MAYBE": True, "": True, "btc_LONG": True, "ETH_SHORT": True}
    result = DoubleFirstPolicy.normalize_state(raw)
    assert "MAYBE" not in result
    assert "" not in result
    assert result["LONG"] is True
    assert result["BTC_LONG"] is True  # uppercased
    assert result["ETH_SHORT"] is True


def test_normalize_state_returns_empty_for_garbage_input():
    assert DoubleFirstPolicy.normalize_state(None) == {}
    assert DoubleFirstPolicy.normalize_state("not a dict") == {}
    assert DoubleFirstPolicy.normalize_state(42) == {}

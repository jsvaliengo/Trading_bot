"""
Testes do whitelist do universo de seleção dinâmica (BINANCE_UNIVERSE_WHITELIST).

Contexto (13/06): o usuário quer manter a ROTAÇÃO por score, mas restrita às
moedas do PDF (Estratégia Padrão) — não à exchange inteira (que trazia lixo
ilíquido tipo SPACE/BSB pro scoring). O whitelist intersecta o universo de
candidatos; a rotação continua dentro dele.
"""
from __future__ import annotations

from types import SimpleNamespace

from trading_bot.core.bot import TradingBot
from trading_bot.core.config import config


def _make_light_bot():
    bot = TradingBot.__new__(TradingBot)
    bot._init_runtime_state()
    return bot


def test_whitelist_restricts_candidate_universe(monkeypatch):
    monkeypatch.setattr(config, "BINANCE_UNIVERSE_WHITELIST", ["SOLUSDT", "ADAUSDT"])
    monkeypatch.setattr(config, "DISABLED_PAIRS", [])
    bot = _make_light_bot()
    bot.pair_selector = SimpleNamespace(
        get_all_futures_pairs=lambda: ["SOLUSDT", "ADAUSDT", "SPACEUSDT", "BSBUSDT", "ZECUSDT"]
    )
    result = bot._refresh_binance_coin_universe(trigger_reason="test")
    assert set(result) == {"SOLUSDT", "ADAUSDT"}  # lixo ilíquido fora do scoring


def test_empty_whitelist_keeps_full_universe(monkeypatch):
    monkeypatch.setattr(config, "BINANCE_UNIVERSE_WHITELIST", [])
    monkeypatch.setattr(config, "DISABLED_PAIRS", [])
    bot = _make_light_bot()
    bot.pair_selector = SimpleNamespace(
        get_all_futures_pairs=lambda: ["SOLUSDT", "SPACEUSDT"]
    )
    result = bot._refresh_binance_coin_universe(trigger_reason="test")
    assert set(result) == {"SOLUSDT", "SPACEUSDT"}


def test_whitelist_without_intersection_falls_back_to_full(monkeypatch):
    # Se a whitelist não casa nenhum par tradável, não zera o universo (fail-open).
    monkeypatch.setattr(config, "BINANCE_UNIVERSE_WHITELIST", ["NONEXISTENTUSDT"])
    monkeypatch.setattr(config, "DISABLED_PAIRS", [])
    bot = _make_light_bot()
    bot.pair_selector = SimpleNamespace(
        get_all_futures_pairs=lambda: ["SOLUSDT", "ADAUSDT"]
    )
    result = bot._refresh_binance_coin_universe(trigger_reason="test")
    assert set(result) == {"SOLUSDT", "ADAUSDT"}


def test_config_parses_universe_whitelist_from_env(monkeypatch):
    monkeypatch.setenv("TRADING_BOT_BINANCE_UNIVERSE_WHITELIST", "solusdt, ADAUSDT ,bnbusdt")
    from trading_bot.core.config import TradingConfig
    c = TradingConfig()
    assert c.BINANCE_UNIVERSE_WHITELIST == ["SOLUSDT", "ADAUSDT", "BNBUSDT"]

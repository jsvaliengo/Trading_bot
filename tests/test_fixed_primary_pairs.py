"""
Testes do override de pares fixos do perfil primário (FIXED_PRIMARY_PAIRS).

Contexto (13/06): o usuário quis fixar 12 pares (estratégia do PDF). Mas
USE_BINANCE_STRATEGY=True força seleção dinâmica do primário trend_signal por
design (bot._get_primary_profile_info). FIXED_PRIMARY_PAIRS é o override: pina o
universo exato e desliga a rotação, mantendo o sizing por tier.
"""
from __future__ import annotations

from trading_bot.core.bot import TradingBot
from trading_bot.core.config import TradingConfig, config


def _make_light_bot():
    bot = TradingBot.__new__(TradingBot)
    bot._init_runtime_state()
    return bot


def _profiles():
    return [{
        "name": "trend_strong",
        "enabled": True,
        "strategy_type": "trend_signal",
        "pairs": ["ZECUSDT", "HYPEUSDT"],  # pares "dinâmicos" antigos
        "max_pairs": 6,
    }]


def test_config_pins_primary_pairs_when_fixed_set():
    raw = "SOLUSDT,DOGEUSDT,xrpusdt"
    c = TradingConfig.__new__(TradingConfig)
    # exercita só o parsing + accessor sem rodar __post_init__ inteiro
    c.STRATEGY_PROFILES = _profiles()
    c.FIXED_PRIMARY_PAIRS = c.normalize_pair_list([p for p in raw.split(",")])
    profs = c.get_enabled_strategy_profiles()
    assert profs[0]["pairs"] == ["SOLUSDT", "DOGEUSDT", "XRPUSDT"]
    assert "max_pairs" not in profs[0]  # removido → não é mais dinâmico


def test_fixed_primary_pairs_disables_dynamic_even_in_binance_mode(monkeypatch):
    pairs12 = [
        "SOLUSDT", "DOGEUSDT", "XRPUSDT", "BNBUSDT", "NEARUSDT", "ADAUSDT",
        "FILUSDT", "SUIUSDT", "DOTUSDT", "BCHUSDT", "AVAXUSDT", "LINKUSDT",
    ]
    monkeypatch.setattr(config, "USE_BINANCE_STRATEGY", True)
    monkeypatch.setattr(config, "STRATEGY_PROFILES", _profiles())
    monkeypatch.setattr(config, "FIXED_PRIMARY_PAIRS", list(pairs12))

    bot = _make_light_bot()
    enabled, primary, is_dynamic = bot._get_primary_profile_info()

    assert is_dynamic is False                  # rotação desligada
    assert primary["pairs"] == pairs12          # universo exato pinado
    assert "max_pairs" not in primary


def test_sync_does_not_union_legacy_pairs_when_fixed(monkeypatch):
    # Regressão: _sync unia config.TRADING_PAIRS (pares dinâmicos antigos) ao
    # primário mesmo com universo fixo → ZEC/HYPE/TRUMP vazavam pro perfil.
    pairs12 = [
        "SOLUSDT", "DOGEUSDT", "XRPUSDT", "BNBUSDT", "NEARUSDT", "ADAUSDT",
        "FILUSDT", "SUIUSDT", "DOTUSDT", "BCHUSDT", "AVAXUSDT", "LINKUSDT",
    ]
    monkeypatch.setattr(config, "USE_BINANCE_STRATEGY", True)
    monkeypatch.setattr(config, "FIXED_PRIMARY_PAIRS", list(pairs12))
    monkeypatch.setattr(config, "DISABLED_PAIRS", [])
    monkeypatch.setattr(config, "STRATEGY_PROFILES", [{
        "name": "trend_strong", "enabled": True, "strategy_type": "trend_signal",
        "entry_mode": "strong_only", "pairs": list(pairs12),
    }])
    # pares dinâmicos legados ainda em TRADING_PAIRS (fonte da união indevida)
    monkeypatch.setattr(config, "TRADING_PAIRS", ["ZECUSDT", "HYPEUSDT", "TRUMPUSDT"])

    bot = _make_light_bot()
    monkeypatch.setattr(bot, "_reload_strategy_profiles", lambda **_k: None, raising=False)
    bot._sync_strategy_profiles_with_trading_pairs(reason="test")

    primary = config.STRATEGY_PROFILES[0]
    assert primary["pairs"] == pairs12
    for stale in ("ZECUSDT", "HYPEUSDT", "TRUMPUSDT"):
        assert stale not in primary["pairs"]


def test_binance_mode_stays_dynamic_without_fixed_override(monkeypatch):
    # Sem FIXED_PRIMARY_PAIRS, o comportamento legado (dinâmico) é preservado.
    monkeypatch.setattr(config, "USE_BINANCE_STRATEGY", True)
    monkeypatch.setattr(config, "STRATEGY_PROFILES", _profiles())
    monkeypatch.setattr(config, "FIXED_PRIMARY_PAIRS", [])

    bot = _make_light_bot()
    _enabled, _primary, is_dynamic = bot._get_primary_profile_info()
    assert is_dynamic is True

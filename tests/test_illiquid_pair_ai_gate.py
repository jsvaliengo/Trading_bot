"""
Testes da rede de segurança que impede par ilíquido de entrar em loop no gate
da IA.

Bug original (13/06): SPACEUSDT/BSBUSDT (volume real ~$22-38M, abaixo do piso de
$150M) foram atribuídos ao perfil trend_strong porque o pré-filtro de score usava
o quoteVolume SINTÉTICO da testnet. A cada ciclo a IA aprovava (cache) e o gate de
abertura barrava → 112 notificações/dia. A correção:

1. `_reference_quote_volume_24h`: fonte única de volume (real da mainnet em
   testnet, com fallback pro ticker), compartilhada pelo gate de abertura e pelo
   gate da IA.
2. `_maybe_build_gated_ai_override_setup`: pula o candidato ANTES do gate da IA
   quando o volume real está abaixo do piso — sem chamar IA, sem notificar.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from trading_bot.core.bot import TradingBot
from trading_bot.core.config import config


def _make_light_bot():
    bot = TradingBot.__new__(TradingBot)
    bot._init_runtime_state()
    return bot


def test_reference_quote_volume_prefers_reference_over_ticker():
    bot = _make_light_bot()
    bot.exchange = SimpleNamespace(
        get_reference_volume_24h=lambda s: 38_000_000.0,
        get_ticker_24h=lambda s: {"quoteVolume": "999999999999"},  # sintético testnet
    )
    assert bot._reference_quote_volume_24h("SPACEUSDT") == 38_000_000.0


def test_reference_quote_volume_falls_back_to_ticker_when_no_reference():
    bot = _make_light_bot()
    bot.exchange = SimpleNamespace(
        get_reference_volume_24h=lambda s: None,  # mainnet / sem referência
        get_ticker_24h=lambda s: {"quoteVolume": "200000000"},
    )
    assert bot._reference_quote_volume_24h("ETHUSDT") == "200000000"


def test_reference_quote_volume_is_none_when_unreadable():
    bot = _make_light_bot()

    def _boom(_s):
        raise RuntimeError("api down")

    bot.exchange = SimpleNamespace(
        get_reference_volume_24h=_boom,
        get_ticker_24h=_boom,
    )
    assert bot._reference_quote_volume_24h("XUSDT") is None


def test_ai_gate_skips_candidate_below_liquidity_floor(monkeypatch):
    monkeypatch.setattr(config, "AI_CONSULTIVE_MODE", "gated")
    monkeypatch.setattr(config, "MIN_TRADE_VOLUME_24H_USD", 150_000_000)

    bot = _make_light_bot()
    bot.exchange = SimpleNamespace(
        get_reference_volume_24h=lambda s: 38_000_000.0,  # < piso
        get_ticker_24h=lambda s: {"quoteVolume": "999999999999"},
    )
    builder = MagicMock(name="build_ai_override_candidate_setup")
    strategy_engine = SimpleNamespace(build_ai_override_candidate_setup=builder)

    result = bot._maybe_build_gated_ai_override_setup(
        strategy_engine=strategy_engine,
        strategy_label="trend_strong",
        strategy_type="trend_signal",
        symbol="SPACEUSDT",
        klines=[{"close": 1.0}],
        confirmation_klines=[{"close": 1.0}],
        available_balance=100.0,
        min_notional=5.0,
        risk_profile=None,
    )

    # Pulou antes do gate: nenhum candidato construído (logo, nenhuma chamada de IA).
    assert result is None
    builder.assert_not_called()


def test_ai_gate_builds_candidate_when_volume_above_floor(monkeypatch):
    monkeypatch.setattr(config, "AI_CONSULTIVE_MODE", "gated")
    monkeypatch.setattr(config, "MIN_TRADE_VOLUME_24H_USD", 150_000_000)

    bot = _make_light_bot()
    bot.exchange = SimpleNamespace(
        get_reference_volume_24h=lambda s: 5_000_000_000.0,  # >> piso
        get_ticker_24h=lambda s: {"quoteVolume": "5000000000"},
    )
    sentinel = object()
    builder = MagicMock(return_value=sentinel)
    strategy_engine = SimpleNamespace(build_ai_override_candidate_setup=builder)

    result = bot._maybe_build_gated_ai_override_setup(
        strategy_engine=strategy_engine,
        strategy_label="trend_strong",
        strategy_type="trend_signal",
        symbol="ETHUSDT",
        klines=[{"close": 1.0}],
        confirmation_klines=[{"close": 1.0}],
        available_balance=100.0,
        min_notional=5.0,
        risk_profile=None,
    )

    assert result is sentinel
    builder.assert_called_once()


def test_score_prefilter_rejects_illiquid_pair_by_real_volume(monkeypatch):
    # Fonte do bug: o pré-filtro de volume do scoring usava o quoteVolume
    # SINTÉTICO da testnet (inflado) e atribuía pares ilíquidos ao perfil.
    # Com o mapa de liquidez real, SPACE (volume real baixo) é rejeitada antes
    # de ser pontuada — mesmo com ticker fake-alto.
    monkeypatch.setattr(config, "MIN_VOLUME_24H_USD", 150_000_000)
    monkeypatch.setattr(config, "OI_ENABLED", False)
    monkeypatch.setattr(config, "PAIR_SCORING_MAX_CANDIDATES", 0)

    bot = _make_light_bot()
    monkeypatch.setattr(
        bot, "_refresh_binance_coin_universe",
        lambda **_k: ["ETHUSDT", "BNBUSDT", "SPACEUSDT"], raising=False,
    )

    synthetic = {s: {"quoteVolume": "999999999999"} for s in
                 ["ETHUSDT", "BNBUSDT", "SPACEUSDT"]}
    real_liquidity = {
        "ETHUSDT": {"volume_24h": 5_000_000_000.0},
        "BNBUSDT": {"volume_24h": 215_000_000.0},
        # SPACEUSDT ausente do mapa real → volume 0 → rejeitada
    }
    bot.exchange = SimpleNamespace(
        get_all_tickers_24h=lambda: synthetic,
        get_all_funding_rates=lambda: {},
        get_reference_liquidity_map=lambda: real_liquidity,
    )

    scored_symbols: set[str] = set()

    def _metrics(symbol, prefetched_ticker=None, prefetched_funding_rate=None):
        scored_symbols.add(symbol)
        return {"symbol": symbol}

    bot.pair_selector = SimpleNamespace(
        get_pair_metrics=_metrics,
        score_pair=lambda _m: 10.0,
    )

    result = bot.sort_binance_coins_by_score(num_coins=2)

    assert "SPACEUSDT" not in result
    assert "SPACEUSDT" not in scored_symbols  # nem chegou a ser pontuada
    assert set(result) == {"ETHUSDT", "BNBUSDT"}

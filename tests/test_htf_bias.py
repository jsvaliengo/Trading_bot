"""Testes do viés de timeframe maior (#168) no trend_strong.

O LONG sangrava ao entrar contra a maré (repique curto dentro de uma queda
maior → MFE~0 → stop). O filtro exige que a tendência do HTF concorde com a
direção do sinal: só LONG se macro de alta, só SHORT se de baixa. É fail-open
(faltando candle do HTF, não barra).
"""
from __future__ import annotations

from trading_bot.core.strategy import HedgeStrategy, Signal
from trading_bot.core.config import config as global_config


def _candles(closes):
    """Lista de candles a partir de uma série de closes (só close importa p/ HTF)."""
    return [{"close": c, "high": c, "low": c, "open": c, "volume": 1.0} for c in closes]


# ─────────────────────────── _htf_bias (unidade) ───────────────────────────

def test_htf_bias_long_when_rising_and_price_above_ema(monkeypatch):
    monkeypatch.setattr(global_config, "TREND_STRONG_HTF_EMA_PERIOD", 50, raising=False)
    monkeypatch.setattr(global_config, "TREND_STRONG_HTF_SLOPE_LOOKBACK", 5, raising=False)
    s = HedgeStrategy()
    closes = [100.0 + i * 0.5 for i in range(60)]  # série de alta
    assert s._htf_bias(_candles(closes)) == "LONG"


def test_htf_bias_short_when_falling_and_price_below_ema(monkeypatch):
    monkeypatch.setattr(global_config, "TREND_STRONG_HTF_EMA_PERIOD", 50, raising=False)
    monkeypatch.setattr(global_config, "TREND_STRONG_HTF_SLOPE_LOOKBACK", 5, raising=False)
    s = HedgeStrategy()
    closes = [200.0 - i * 0.5 for i in range(60)]  # série de baixa
    assert s._htf_bias(_candles(closes)) == "SHORT"


def test_htf_bias_neutral_when_flat(monkeypatch):
    monkeypatch.setattr(global_config, "TREND_STRONG_HTF_EMA_PERIOD", 50, raising=False)
    monkeypatch.setattr(global_config, "TREND_STRONG_HTF_SLOPE_LOOKBACK", 5, raising=False)
    s = HedgeStrategy()
    closes = [100.0] * 60  # lateral → nem sobe nem desce
    assert s._htf_bias(_candles(closes)) == "NEUTRAL"


def test_htf_bias_none_when_insufficient_candles(monkeypatch):
    monkeypatch.setattr(global_config, "TREND_STRONG_HTF_EMA_PERIOD", 50, raising=False)
    monkeypatch.setattr(global_config, "TREND_STRONG_HTF_SLOPE_LOOKBACK", 5, raising=False)
    s = HedgeStrategy()
    # menos que period+slope → None (fail-open: chamador não barra por falta de dado)
    assert s._htf_bias(_candles([100.0] * 20)) is None
    assert s._htf_bias(None) is None
    assert s._htf_bias([]) is None


# ─────────────── gate na analyze_market_pullback (integração leve) ───────────────

def _force_long_setup(monkeypatch, strategy):
    """Faz a estratégia ver um sinal LONG válido (contexto + pullback + candle)."""
    long_ctx = {
        "price": 100.0, "ema9": 100.0, "ema21": 99.0, "ema200": 90.0,
        "vwap": 99.5, "rsi": 50.0, "direction": "LONG", "volume_ok": True,
    }
    monkeypatch.setattr(strategy, "_build_trend_context", lambda *_a, **_k: long_ctx)
    monkeypatch.setattr(strategy, "_is_pullback_to_ema_long", lambda *_a, **_k: True)
    monkeypatch.setattr(strategy, "_is_bullish_rejection", lambda *_a, **_k: True)
    # RSI 50 dentro da faixa LONG default; garante o range independente do config.
    monkeypatch.setattr(global_config, "TREND_STRONG_LONG_RSI_MIN", 40.0, raising=False)
    monkeypatch.setattr(global_config, "TREND_STRONG_LONG_RSI_MAX", 60.0, raising=False)


def test_pullback_long_blocked_when_htf_bias_is_short(monkeypatch):
    monkeypatch.setattr(global_config, "TREND_STRONG_HTF_BIAS_ENABLED", True, raising=False)
    s = HedgeStrategy()
    _force_long_setup(monkeypatch, s)
    monkeypatch.setattr(s, "_htf_bias", lambda *_a, **_k: "SHORT")  # macro contra
    klines = _candles([100.0, 100.0])
    assert s.analyze_market_pullback(klines, klines, htf_klines=klines) == Signal.NEUTRAL


def test_pullback_long_allowed_when_htf_bias_is_long(monkeypatch):
    monkeypatch.setattr(global_config, "TREND_STRONG_HTF_BIAS_ENABLED", True, raising=False)
    s = HedgeStrategy()
    _force_long_setup(monkeypatch, s)
    monkeypatch.setattr(s, "_htf_bias", lambda *_a, **_k: "LONG")  # macro a favor
    klines = _candles([100.0, 100.0])
    assert s.analyze_market_pullback(klines, klines, htf_klines=klines) == Signal.STRONG_BUY


def test_pullback_long_allowed_when_htf_bias_none_failopen(monkeypatch):
    monkeypatch.setattr(global_config, "TREND_STRONG_HTF_BIAS_ENABLED", True, raising=False)
    s = HedgeStrategy()
    _force_long_setup(monkeypatch, s)
    monkeypatch.setattr(s, "_htf_bias", lambda *_a, **_k: None)  # sem dado de HTF
    klines = _candles([100.0, 100.0])
    # fail-open: não barra por falta de candle do HTF
    assert s.analyze_market_pullback(klines, klines, htf_klines=None) == Signal.STRONG_BUY


def test_pullback_long_allowed_when_filter_disabled(monkeypatch):
    monkeypatch.setattr(global_config, "TREND_STRONG_HTF_BIAS_ENABLED", False, raising=False)
    s = HedgeStrategy()
    _force_long_setup(monkeypatch, s)
    # mesmo com macro contra, filtro desligado não barra
    monkeypatch.setattr(s, "_htf_bias", lambda *_a, **_k: "SHORT")
    klines = _candles([100.0, 100.0])
    assert s.analyze_market_pullback(klines, klines, htf_klines=klines) == Signal.STRONG_BUY

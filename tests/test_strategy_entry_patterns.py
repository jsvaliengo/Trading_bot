"""Primitivos de entrada do trend_strong: pullback + candles de reversão.

São as funções puras que DISPARAM um trade (rejeição/engolfo + pullback à EMA).
Estavam sem cobertura. Bug aqui = entrada em padrão que não existe.
"""
from __future__ import annotations

from trading_bot.core.strategy import HedgeStrategy


def _c(o, h, low, c):
    return {"open": o, "high": h, "low": low, "close": c}


S = HedgeStrategy()


# ─────────────────────────── rejeição (pin bar) ───────────────────────────

def test_bullish_rejection_hammer():
    # martelo: corpo pequeno em cima, pavio inferior longo
    assert S._is_bullish_rejection(_c(100, 101.2, 98.0, 101.0)) is True


def test_bullish_rejection_recusa_candle_sem_pavio():
    # candle verde "cheio", sem pavio inferior → não é rejeição
    assert S._is_bullish_rejection(_c(100, 101.1, 99.9, 101.0)) is False


def test_bullish_rejection_recusa_candle_vermelho():
    # fecha abaixo da abertura → não é rejeição de ALTA
    assert S._is_bullish_rejection(_c(101, 101.2, 98.0, 100.0)) is False


def test_bearish_rejection_estrela_cadente():
    # corpo embaixo, pavio superior longo, fecha em baixa
    assert S._is_bearish_rejection(_c(100, 103.0, 99.8, 99.9)) is True


def test_bearish_rejection_recusa_candle_verde():
    assert S._is_bearish_rejection(_c(100, 103.0, 99.8, 102.5)) is False


# ─────────────────────────── engolfo ───────────────────────────

def test_bullish_engulfing():
    prev = _c(101, 101.2, 99.8, 100.0)   # vermelho
    curr = _c(99.9, 101.6, 99.8, 101.5)  # verde engolindo o corpo anterior
    assert S._is_bullish_engulfing(prev, curr) is True


def test_bullish_engulfing_recusa_quando_nao_engole():
    prev = _c(101, 101.2, 99.8, 100.0)
    curr = _c(100.2, 100.8, 100.0, 100.6)  # verde mas dentro do corpo anterior
    assert S._is_bullish_engulfing(prev, curr) is False


def test_bearish_engulfing():
    prev = _c(100, 101.2, 99.8, 101.0)   # verde
    curr = _c(101.2, 101.4, 99.5, 99.7)  # vermelho engolindo
    assert S._is_bearish_engulfing(prev, curr) is True


# ─────────────────────────── pullback à EMA ───────────────────────────

def test_pullback_long_quando_low_toca_ema():
    # low encosta na EMA9 (com tolerância) → pullback válido p/ LONG
    assert S._is_pullback_to_ema_long(_c(100.2, 100.5, 100.05, 100.3), ema9=100.0, ema21=99.0) is True


def test_pullback_long_recusa_quando_longe_da_ema():
    # low bem acima das EMAs → não é pullback
    assert S._is_pullback_to_ema_long(_c(102, 102.5, 101.8, 102.3), ema9=100.0, ema21=99.0) is False


def test_pullback_short_quando_high_toca_ema():
    assert S._is_pullback_to_ema_short(_c(98.7, 98.95, 98.5, 98.6), ema9=99.0, ema21=100.0) is True


def test_pullback_short_recusa_quando_longe_da_ema():
    assert S._is_pullback_to_ema_short(_c(97, 97.4, 96.8, 97.1), ema9=99.0, ema21=100.0) is False

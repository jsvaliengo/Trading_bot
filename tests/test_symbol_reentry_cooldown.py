"""
Testes do cooldown de reentrada por símbolo (anti-churn).

Bug original (04/06): após um stop-out, o único gate de abertura era "tem
posição aberta agora?" — então o símbolo era liberado no ciclo seguinte e o
mesmo sinal reabria na hora. Em mercado lateral isso virou churn (20 trades de
BCHUSDT em ~16h, quase todos stop-out sangrando fee).

O cooldown pausa o símbolo por SYMBOL_REENTRY_COOLDOWN_SECONDS após um
fechamento NEGATIVO (loss/breakeven). Um win não ativa o cooldown.

Os métodos vivem em TradingBot mas só tocam self.symbol_reentry_cooldowns +
config + time, então são exercitados aqui com um self duck-typed.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from trading_bot.core import bot as bot_module
from trading_bot.core.bot import TradingBot
from trading_bot.core.config import config


@pytest.fixture
def fake_clock(monkeypatch):
    """Relógio controlável para o time.time() usado em bot.py."""
    state = {"t": 1000.0}
    monkeypatch.setattr(bot_module.time, "time", lambda: state["t"])
    return state


@pytest.fixture
def window_300(monkeypatch):
    monkeypatch.setattr(config, "SYMBOL_REENTRY_COOLDOWN_SECONDS", 300, raising=False)
    return 300


def _stub():
    return SimpleNamespace(symbol_reentry_cooldowns={})


def test_mark_sets_now_and_remaining_counts_down(fake_clock, window_300):
    stub = _stub()
    TradingBot._mark_symbol_reentry_cooldown(stub, "BCHUSDT")
    assert stub.symbol_reentry_cooldowns["BCHUSDT"] == 1000.0

    # logo após marcar: janela cheia
    assert TradingBot._symbol_reentry_cooldown_remaining(stub, "BCHUSDT") == pytest.approx(300.0)

    # metade do caminho
    fake_clock["t"] = 1150.0
    assert TradingBot._symbol_reentry_cooldown_remaining(stub, "BCHUSDT") == pytest.approx(150.0)


def test_remaining_zero_and_prunes_after_expiry(fake_clock, window_300):
    stub = _stub()
    TradingBot._mark_symbol_reentry_cooldown(stub, "BCHUSDT")
    fake_clock["t"] = 1000.0 + 301  # passou da janela
    assert TradingBot._symbol_reentry_cooldown_remaining(stub, "BCHUSDT") == 0.0
    # entrada expirada é removida ao consultar
    assert "BCHUSDT" not in stub.symbol_reentry_cooldowns


def test_unknown_symbol_is_free(window_300):
    stub = _stub()
    assert TradingBot._symbol_reentry_cooldown_remaining(stub, "ETHUSDT") == 0.0


def test_other_symbols_not_affected(fake_clock, window_300):
    stub = _stub()
    TradingBot._mark_symbol_reentry_cooldown(stub, "BCHUSDT")
    assert TradingBot._symbol_reentry_cooldown_remaining(stub, "ETHUSDT") == 0.0
    assert TradingBot._symbol_reentry_cooldown_remaining(stub, "BCHUSDT") > 0


@pytest.mark.parametrize("disabled", [0, -1])
def test_disabled_window_is_noop(monkeypatch, fake_clock, disabled):
    monkeypatch.setattr(config, "SYMBOL_REENTRY_COOLDOWN_SECONDS", disabled, raising=False)
    stub = _stub()
    TradingBot._mark_symbol_reentry_cooldown(stub, "BCHUSDT")
    assert stub.symbol_reentry_cooldowns == {}  # mark é no-op
    assert TradingBot._symbol_reentry_cooldown_remaining(stub, "BCHUSDT") == 0.0


def test_remark_resets_the_window(fake_clock, window_300):
    stub = _stub()
    TradingBot._mark_symbol_reentry_cooldown(stub, "BCHUSDT")
    fake_clock["t"] = 1200.0  # 200s depois, restam 100s
    assert TradingBot._symbol_reentry_cooldown_remaining(stub, "BCHUSDT") == pytest.approx(100.0)
    # novo loss reinicia a janela cheia
    TradingBot._mark_symbol_reentry_cooldown(stub, "BCHUSDT")
    assert TradingBot._symbol_reentry_cooldown_remaining(stub, "BCHUSDT") == pytest.approx(300.0)

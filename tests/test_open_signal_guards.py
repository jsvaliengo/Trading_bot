"""Guards de abertura em open_signal_trade (proteção contra ordens indevidas).

Estratégia direcional: no máximo UMA posição por par/lado, sem hedge, sem
pirâmide, e nada de reabrir em cooldown estrutural. Bugs aqui = ordens
duplicadas/hedge = dinheiro real (#130 pirâmide, #173 hedge, cooldown -2027).
Os guards retornam False ANTES de tocar funding/sizing/ordem.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from trading_bot.execution.engine import ExecutionEngine
from trading_bot.core.config import config


def _setup(symbol="BTCUSDT"):
    return SimpleNamespace(
        symbol=symbol,
        signal=SimpleNamespace(name="STRONG_BUY"),
        metadata={},
        stop_loss=100.0,
        take_profit=110.0,
    )


def _bot(positions=None, cooldown=None):
    bot = MagicMock()
    bot.positions = dict(positions or {})           # .get real
    bot.exchange.get_symbol_cooldown_info.return_value = cooldown
    bot._normalize_strategy_type.side_effect = lambda x: x
    return bot


def test_cooldown_estrutural_bloqueia():
    bot = _bot(cooldown={"remaining_seconds": 600, "code": -2027})
    engine = ExecutionEngine(bot)
    assert engine.open_signal_trade(_setup(), open_long=True) is False
    bot.block_reporter.notify_blocked.assert_called_once()
    # não avançou pro funding/ordem
    bot.exchange.get_funding_rate.assert_not_called()


def test_anti_piramide_mesmo_lado():
    """Já existe LONG aberto → não empilha outro LONG no mesmo par."""
    bot = _bot(positions={"BTCUSDT_LONG": {"symbol": "BTCUSDT"}})
    engine = ExecutionEngine(bot)
    assert engine.open_signal_trade(_setup(), open_long=True) is False
    bot.block_reporter.notify_blocked.assert_called_once()
    bot.exchange.get_funding_rate.assert_not_called()


def test_anti_hedge_lado_oposto():
    """Já existe SHORT aberto → não abre LONG no mesmo par (sem hedge)."""
    bot = _bot(positions={"BTCUSDT_SHORT": {"symbol": "BTCUSDT"}})
    engine = ExecutionEngine(bot)
    assert engine.open_signal_trade(_setup(), open_long=True) is False
    bot.block_reporter.notify_blocked.assert_called_once()
    bot.exchange.get_funding_rate.assert_not_called()


def test_outro_par_aberto_nao_bloqueia(monkeypatch):
    """Posição em OUTRO par não dispara guard — a abertura segue além deles.

    Prova que os 3 guards passaram: chega na linha do funding (sentinela) e
    NENHUM bloqueio foi reportado.
    """
    monkeypatch.setattr(config, "CHECK_FUNDING_RATE", True, raising=False)
    bot = _bot(positions={"ETHUSDT_LONG": {"symbol": "ETHUSDT"}})

    class _ReachedFunding(Exception):
        pass

    bot.exchange.get_funding_rate.side_effect = _ReachedFunding
    engine = ExecutionEngine(bot)
    with pytest.raises(_ReachedFunding):
        engine.open_signal_trade(_setup(), open_long=True)
    bot.block_reporter.notify_blocked.assert_not_called()

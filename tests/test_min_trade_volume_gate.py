"""
Testes do piso de liquidez no momento da abertura (engine._below_min_trade_volume).

Bug original (04/06): LABUSDT (micro-cap) foi negociado e o stop preencheu ~12%
contra a posição (-$3,73). O filtro de volume roda na SELEÇÃO de pares, mas um
par escolhido quando líquido pode secar antes do trade. Este gate é a última
linha de defesa, checado no open_signal_trade.

Política fail-open: piso <= 0 desativa; ticker ilegível/ausente NÃO bloqueia
(a seleção já vetou o par; um blip de API não derruba trade). Só bloqueia com
volume confirmado abaixo do piso.
"""
from __future__ import annotations

import pytest

from trading_bot.execution.engine import _below_min_trade_volume

FLOOR = 150_000_000


def test_blocks_when_volume_below_floor():
    assert _below_min_trade_volume(50_000_000, FLOOR) is True


def test_allows_when_volume_at_or_above_floor():
    assert _below_min_trade_volume(150_000_000, FLOOR) is False
    assert _below_min_trade_volume(500_000_000, FLOOR) is False


def test_string_volume_is_parsed():
    # Binance devolve quoteVolume como string
    assert _below_min_trade_volume("49999999", FLOOR) is True
    assert _below_min_trade_volume("200000000", FLOOR) is False


@pytest.mark.parametrize("floor", [0, -1])
def test_disabled_floor_never_blocks(floor):
    assert _below_min_trade_volume(1, floor) is False


@pytest.mark.parametrize("raw", [None, "", "n/a", "abc", {}])
def test_unreadable_volume_is_fail_open(raw):
    # ticker ausente/ilegível não bloqueia (fail-open)
    assert _below_min_trade_volume(raw, FLOOR) is False


def test_invalid_floor_is_fail_open():
    assert _below_min_trade_volume(1, None) is False
    assert _below_min_trade_volume(1, "xyz") is False


def test_zero_volume_blocks():
    # volume zero é um número válido e está abaixo do piso → bloqueia
    assert _below_min_trade_volume(0, FLOOR) is True
    assert _below_min_trade_volume("0", FLOOR) is True

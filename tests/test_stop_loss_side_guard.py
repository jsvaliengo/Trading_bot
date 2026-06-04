"""
Testes da trava de SL do lado errado (engine._validate_stop_loss_side).

Bug original: o custom_stop_loss é calculado no setup contra um preço mais
antigo; o slippage até o fill pode jogá-lo para o lado errado da entrada real
(LONG com SL acima da entrada, SHORT com SL abaixo). Enviar um STOP_MARKET já do
lado errado faz a Binance dispará-lo na hora — stop instantâneo sangrando fee.
Caso real observado: BCHUSDT LONG entry=246.54 / SL=246.55.

A trava recai no SL percentual (sempre do lado certo) só quando detecta inversão.
"""
from __future__ import annotations

import pytest

from trading_bot.execution.engine import _validate_stop_loss_side


def test_long_sl_below_entry_is_unchanged():
    sl, corrected = _validate_stop_loss_side("LONG", 100.0, 97.0, 3.0, 2)
    assert corrected is False
    assert sl == 97.0


def test_short_sl_above_entry_is_unchanged():
    sl, corrected = _validate_stop_loss_side("SHORT", 100.0, 103.0, 3.0, 2)
    assert corrected is False
    assert sl == 103.0


def test_long_sl_above_entry_is_corrected_to_percent_below():
    # Caso real BCH: entry 246.54, SL 246.55 (acima → inválido p/ LONG).
    sl, corrected = _validate_stop_loss_side("LONG", 246.54, 246.55, 3.0, 2)
    assert corrected is True
    assert sl == pytest.approx(246.54 * 0.97, abs=0.01)
    assert sl < 246.54


def test_long_sl_equal_to_entry_is_corrected():
    sl, corrected = _validate_stop_loss_side("LONG", 100.0, 100.0, 3.0, 2)
    assert corrected is True
    assert sl < 100.0


def test_short_sl_below_entry_is_corrected_to_percent_above():
    sl, corrected = _validate_stop_loss_side("SHORT", 100.0, 99.5, 3.0, 2)
    assert corrected is True
    assert sl == pytest.approx(103.0, abs=0.01)
    assert sl > 100.0


def test_short_sl_equal_to_entry_is_corrected():
    sl, corrected = _validate_stop_loss_side("SHORT", 100.0, 100.0, 2.0, 2)
    assert corrected is True
    assert sl > 100.0


def test_corrected_value_respects_price_precision():
    sl, corrected = _validate_stop_loss_side("LONG", 0.06070, 0.06075, 1.5, 5)
    assert corrected is True
    # arredondado para 5 casas (pricePrecision do par)
    assert sl == round(0.06070 * 0.985, 5)


@pytest.mark.parametrize("stop_loss", [0.0, None, -1.0])
def test_non_positive_stop_loss_is_passthrough(stop_loss):
    sl, corrected = _validate_stop_loss_side("LONG", 100.0, stop_loss, 3.0, 2)
    assert corrected is False
    assert sl == stop_loss


@pytest.mark.parametrize("entry_price", [0.0, None, -5.0])
def test_invalid_entry_price_is_passthrough(entry_price):
    sl, corrected = _validate_stop_loss_side("LONG", entry_price, 97.0, 3.0, 2)
    assert corrected is False
    assert sl == 97.0

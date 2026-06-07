"""Regressão: arredondamento de qty não pode cair abaixo do minNotional.

Bug observado em 2026-06-07: BTC LONG com qty 0.000845 era arredondada para
0.0008 (~$49.7), abaixo do mínimo de $50 da Binance → rejeição -4164 em loop.
"""

import pytest

from trading_bot.execution.engine import _round_qty_for_min_notional


def test_bumps_qty_up_when_round_would_drop_below_min_notional():
    # BTC: 0.000845 * 62155 = ~$52.5, mas round(.,4) = 0.0008 = ~$49.7 < $50.
    info = {"quantityPrecision": 4, "minNotional": 50.0}
    qty = _round_qty_for_min_notional(0.000845, 62155.0, info)
    assert qty * 62155.0 >= 50.0
    assert qty == pytest.approx(0.0009)  # subiu um step em vez de truncar p/ 0.0008


def test_keeps_qty_when_already_above_min_notional():
    # Caso normal: notional bem acima do mínimo → comportamento = round() puro.
    info = {"quantityPrecision": 1, "minNotional": 5.0}
    qty = _round_qty_for_min_notional(10.74, 5.0, info)
    assert qty == pytest.approx(10.7)


def test_noop_without_min_notional():
    info = {"quantityPrecision": 3}
    qty = _round_qty_for_min_notional(1.2345, 100.0, info)
    assert qty == pytest.approx(1.234)  # round(1.2345, 3); só arredonda, não bumpa


def test_noop_on_invalid_price():
    info = {"quantityPrecision": 4, "minNotional": 50.0}
    assert _round_qty_for_min_notional(0.0008, 0.0, info) == pytest.approx(0.0008)
    assert _round_qty_for_min_notional(0.0008, None, info) == 0.0008

"""
Testes da contabilidade de P&L de posições fechadas pela Binance.

Bug original (04/06): _process_binance_closed_position pegava só income_list[-1]
(uma linha de REALIZED_PNL). Um SL pode preencher em vários fills parciais, cada
um gerando uma linha — pegar a última subestimava (ou invertia) o P&L real. E o
exit_price derivado de um gross parcial ficava incoerente com o pnl (caso LAB:
pnl_net=-3,73 mas exit_price gravado ACIMA da entrada, como se o preço tivesse
subido a favor do LONG).

Fix: somar TODAS as linhas REALIZED_PNL (gross agregado correto) e derivar o
exit_price implícito desse gross — consistente com o pnl por construção.
"""
from __future__ import annotations

import pytest

from trading_bot.core.bot import _aggregate_realized_pnl, _implied_exit_price


# ─────────────────────────── _aggregate_realized_pnl ───────────────────────────

def test_empty_or_none_returns_none():
    assert _aggregate_realized_pnl([]) is None
    assert _aggregate_realized_pnl(None) is None


def test_single_row():
    assert _aggregate_realized_pnl([{"income": "-3.69", "incomeType": "REALIZED_PNL"}]) == pytest.approx(-3.69)


def test_sums_all_partial_fills():
    # O cerne do fix: 3 fills parciais. income_list[-1] daria só -0.1.
    rows = [
        {"income": "-2.0", "incomeType": "REALIZED_PNL"},
        {"income": "-1.5", "incomeType": "REALIZED_PNL"},
        {"income": "-0.1", "incomeType": "REALIZED_PNL"},
    ]
    assert _aggregate_realized_pnl(rows) == pytest.approx(-3.6)


def test_ignores_non_realized_pnl_rows():
    rows = [
        {"income": "-3.69", "incomeType": "REALIZED_PNL"},
        {"income": "-0.50", "incomeType": "FUNDING_FEE"},
        {"income": "-0.02", "incomeType": "COMMISSION"},
    ]
    assert _aggregate_realized_pnl(rows) == pytest.approx(-3.69)


def test_missing_income_type_is_assumed_realized():
    # query já filtra por tipo no servidor; linha sem incomeType conta
    assert _aggregate_realized_pnl([{"income": "1.25"}]) == pytest.approx(1.25)


def test_skips_malformed_rows_but_keeps_valid():
    rows = [
        {"income": "2.0", "incomeType": "REALIZED_PNL"},
        "not-a-dict",
        {"income": "lixo", "incomeType": "REALIZED_PNL"},
        {"income": None, "incomeType": "REALIZED_PNL"},
        {"income": "1.0", "incomeType": "REALIZED_PNL"},
    ]
    assert _aggregate_realized_pnl(rows) == pytest.approx(3.0)


def test_all_malformed_returns_none():
    assert _aggregate_realized_pnl(["x", {"income": "abc"}]) is None


# ─────────────────────────── _implied_exit_price ───────────────────────────

def test_long_loss_exit_is_below_entry():
    # Caso LAB: LONG, gross negativo → exit ABAIXO da entrada (não acima!)
    exit_price = _implied_exit_price("LONG", 17.506, -3.693726, 1.713698)
    assert exit_price < 17.506
    assert exit_price == pytest.approx(15.3506, abs=1e-3)


def test_long_profit_exit_is_above_entry():
    assert _implied_exit_price("LONG", 100.0, 10.0, 2.0) == pytest.approx(105.0)


def test_short_loss_exit_is_above_entry():
    # SHORT perde quando o preço sobe → exit ACIMA da entrada
    assert _implied_exit_price("SHORT", 100.0, -10.0, 2.0) == pytest.approx(105.0)


def test_short_profit_exit_is_below_entry():
    assert _implied_exit_price("SHORT", 100.0, 10.0, 2.0) == pytest.approx(95.0)


@pytest.mark.parametrize("qty", [0, None])
def test_zero_quantity_returns_entry(qty):
    assert _implied_exit_price("LONG", 50.0, -3.0, qty) == 50.0


def test_exit_is_consistent_with_gross():
    # (exit - entry) * qty == gross  (LONG), por construção
    entry, gross, qty = 250.0, -1.234, 0.4
    exit_price = _implied_exit_price("LONG", entry, gross, qty)
    assert (exit_price - entry) * qty == pytest.approx(gross)

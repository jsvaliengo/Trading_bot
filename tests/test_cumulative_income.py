"""Funding/comissão ACUMULADOS via income history (todos os dias, com paginação)."""

import threading
import time
import types

import pytest

from trading_bot.infra.binance_client import BinanceConnection


class _FakeClient:
    def __init__(self, records):
        self.records = sorted(records, key=lambda r: r["time"])
        self.calls = 0

    def futures_income_history(self, startTime=None, limit=1000):
        self.calls += 1
        batch = [r for r in self.records if r["time"] >= (startTime or 0)]
        return batch[:limit]


def _fake_conn(records):
    fake = types.SimpleNamespace()
    fake._cache_lock = threading.Lock()
    fake._api_call = lambda _name, fn, **kw: fn(**kw)
    fake.client = _FakeClient(records)
    return fake


def test_sums_funding_commission_realized_separately():
    now = int(time.time() * 1000)
    records = [
        {"incomeType": "FUNDING_FEE", "income": "-0.10", "time": now - 5000},
        {"incomeType": "FUNDING_FEE", "income": "0.25", "time": now - 4000},
        {"incomeType": "COMMISSION", "income": "-0.42", "time": now - 3000},
        {"incomeType": "REALIZED_PNL", "income": "1.0", "time": now - 2000},
    ]
    res = BinanceConnection.get_cumulative_income_from_binance(_fake_conn(records))
    assert res["funding_fee"] == pytest.approx(0.15)
    assert res["commission"] == pytest.approx(-0.42)
    assert res["realized_pnl"] == pytest.approx(1.0)
    assert res["income_count"] == 4


def test_paginates_beyond_1000_records():
    now = int(time.time() * 1000)
    # 1000 funding de $0.001 (= $1.0) na 1a página + 1 funding de $2.0 depois.
    records = [
        {"incomeType": "FUNDING_FEE", "income": "0.001", "time": now - 1_000_000 + i}
        for i in range(1000)
    ]
    records.append({"incomeType": "FUNDING_FEE", "income": "2.0", "time": now - 1})
    conn = _fake_conn(records)
    res = BinanceConnection.get_cumulative_income_from_binance(conn)
    assert conn.client.calls >= 2  # precisou paginar
    assert res["funding_fee"] == pytest.approx(3.0)
    assert res["income_count"] == 1001


def test_start_ms_limits_window_to_current_period():
    now = int(time.time() * 1000)
    records = [
        {"incomeType": "FUNDING_FEE", "income": "-5.0", "time": now - 100_000},  # antigo (pré-reset)
        {"incomeType": "FUNDING_FEE", "income": "0.30", "time": now - 1_000},     # período atual
        {"incomeType": "COMMISSION", "income": "-0.50", "time": now - 500},       # período atual
    ]
    anchor = now - 50_000  # corta o registro antigo
    res = BinanceConnection.get_cumulative_income_from_binance(_fake_conn(records), start_ms=anchor)
    assert res["funding_fee"] == pytest.approx(0.30)   # ignora o -5.0 antigo
    assert res["commission"] == pytest.approx(-0.50)
    assert res["income_count"] == 2


def test_empty_history_returns_zeros():
    res = BinanceConnection.get_cumulative_income_from_binance(_fake_conn([]))
    assert res["funding_fee"] == 0.0
    assert res["commission"] == 0.0
    assert res["income_count"] == 0

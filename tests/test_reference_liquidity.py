"""Filtro de liquidez com dados REAIS da mainnet (referência) em testnet.

Em testnet, volume/spread reportados pela própria exchange são sintéticos e
deixam pares ilíquidos passarem pelos gates de liquidez → slippage brutal no
stop (caso LABUSDT/XRP/BCH). O bot passa a usar volume/spread REAIS da mainnet
pública como referência:
- BinanceConnection.get_reference_liquidity_map() / get_reference_volume_24h()
- pair_selector.get_pair_metrics sobrescreve volume/spread (gate de seleção)
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest


# ─────────────────────────── exchange: reference liquidity ───────────────────────────

class _FakePublicClient:
    """Simula o cliente público mainnet (python-binance)."""
    def __init__(self):
        self.ticker_calls = 0
        self.book_calls = 0

    def futures_ticker(self):
        self.ticker_calls += 1
        return [
            {"symbol": "ETHUSDT", "quoteVolume": "2000000000"},
            {"symbol": "LABUSDT", "quoteVolume": "500000"},  # ilíquido na mainnet
        ]

    def futures_orderbook_ticker(self):
        self.book_calls += 1
        return [
            {"symbol": "ETHUSDT", "bidPrice": "2500.0", "askPrice": "2500.25"},
            {"symbol": "LABUSDT", "bidPrice": "10.0", "askPrice": "10.5"},  # spread 5%
        ]


def _make_conn(*, testnet=True, enabled=True, ref_client=None, ttl=300.0):
    from trading_bot.infra.binance_client import BinanceConnection
    conn = BinanceConnection.__new__(BinanceConnection)
    conn.config = SimpleNamespace(
        USE_TESTNET=testnet,
        USE_REAL_LIQUIDITY_ON_TESTNET=enabled,
        REFERENCE_LIQUIDITY_TTL_S=ttl,
    )
    conn._public_ref_client = ref_client
    conn._ref_liq_cache = None
    conn._ref_liq_ts = 0.0
    return conn


def test_reference_map_none_em_mainnet():
    conn = _make_conn(testnet=False, ref_client=_FakePublicClient())
    assert conn.get_reference_liquidity_map() is None


def test_reference_map_none_quando_desligado():
    conn = _make_conn(testnet=True, enabled=False, ref_client=_FakePublicClient())
    assert conn.get_reference_liquidity_map() is None


def test_reference_map_constroi_volume_e_spread():
    conn = _make_conn(ref_client=_FakePublicClient())
    m = conn.get_reference_liquidity_map()
    assert m["ETHUSDT"]["volume_24h"] == pytest.approx(2_000_000_000)
    assert m["LABUSDT"]["volume_24h"] == pytest.approx(500_000)
    # spread ETH = (2500.25-2500)/2500*100 = 0.01% ; LAB = 5%
    assert m["ETHUSDT"]["spread_percent"] == pytest.approx(0.01, abs=1e-6)
    assert m["LABUSDT"]["spread_percent"] == pytest.approx(5.0)


def test_reference_map_cacheia(monkeypatch):
    fake = _FakePublicClient()
    conn = _make_conn(ref_client=fake, ttl=300.0)
    conn.get_reference_liquidity_map()
    conn.get_reference_liquidity_map()  # 2ª chamada deve vir do cache
    assert fake.ticker_calls == 1 and fake.book_calls == 1


def test_reference_volume_24h_lookup():
    conn = _make_conn(ref_client=_FakePublicClient())
    assert conn.get_reference_volume_24h("ETHUSDT") == pytest.approx(2_000_000_000)
    assert conn.get_reference_volume_24h("labusdt") == pytest.approx(500_000)  # case-insensitive
    assert conn.get_reference_volume_24h("INEXISTENTEUSDT") is None


def test_reference_volume_24h_none_em_mainnet():
    conn = _make_conn(testnet=False, ref_client=_FakePublicClient())
    assert conn.get_reference_volume_24h("ETHUSDT") is None


# ─────────────────────────── pair_selector: override no gate de seleção ───────────────────────────

def _klines(n=50, base=100.0):
    """Klines mínimos válidos: índices usados são [2]=high,[3]=low,[4]=close,[7]=qvol."""
    out = []
    for i in range(n):
        c = base + (i % 5) * 0.1
        out.append([0, c, c + 0.5, c - 0.5, c, 1000.0, 0, 1_000_000.0])
    return out


class _StubExchange:
    def __init__(self, ref_map):
        self._ref_map = ref_map

    def get_order_book(self, symbol, limit=5):
        # spread testnet "bom" (0.01%) — deve ser SOBRESCRITO pela referência
        return {"bids": [[100.0, 1]], "asks": [[100.01, 1]]}

    def get_klines_raw(self, symbol, interval, limit):
        return _klines()

    def get_symbol_info(self, symbol):
        return {"minNotional": 5}

    def get_reference_liquidity_map(self):
        return self._ref_map


def _selector(ref_map):
    from trading_bot.services.pair_selector import PairSelector
    cfg = SimpleNamespace(REGIME_ADX_PERIOD=14)
    return PairSelector(_StubExchange(ref_map), cfg)


# ticker/funding pré-buscados pra pular get_ticker_24h/get_funding_rate
_PREFETCH_TICKER = {"quoteVolume": "999999999999", "lastPrice": "100.0"}  # volume testnet FAKE alto


def test_get_pair_metrics_sobrescreve_com_referencia():
    sel = _selector({"LABUSDT": {"volume_24h": 500_000, "spread_percent": 5.0}})
    m = sel.get_pair_metrics("LABUSDT", prefetched_ticker=_PREFETCH_TICKER, prefetched_funding_rate=0.0)
    # volume real (500k) substitui o fake da testnet (999bi); spread real (5%) substitui 0.01%
    assert m["volume_24h"] == pytest.approx(500_000)
    assert m["spread_percent"] == pytest.approx(5.0)


def test_get_pair_metrics_rejeita_simbolo_ausente_na_mainnet():
    sel = _selector({"ETHUSDT": {"volume_24h": 2e9, "spread_percent": 0.01}})
    # LAB não está no mapa de referência → sentinelas que os filtros rejeitam
    m = sel.get_pair_metrics("LABUSDT", prefetched_ticker=_PREFETCH_TICKER, prefetched_funding_rate=0.0)
    assert m["volume_24h"] == 0.0
    assert m["spread_percent"] == 999.0


def test_get_pair_metrics_sem_referencia_mantem_testnet():
    # ref_map None (mainnet/feature off) → mantém volume/spread da exchange
    sel = _selector(None)
    m = sel.get_pair_metrics("ETHUSDT", prefetched_ticker=_PREFETCH_TICKER, prefetched_funding_rate=0.0)
    assert m["volume_24h"] == pytest.approx(999999999999)
    assert m["spread_percent"] == pytest.approx(0.01, abs=1e-3)

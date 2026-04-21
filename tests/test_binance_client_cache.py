"""Testes do cache TTL em binance_client (balance + funding_rate)."""

from unittest.mock import MagicMock



def _make_client():
    """Cria um BinanceConnection sem conectar de fato, via __new__."""
    # Import aqui pra ver o estado atual do módulo após patches.
    from trading_bot.infra.binance_client import BinanceConnection

    conn = BinanceConnection.__new__(BinanceConnection)
    # Inicializa só o que os métodos de cache precisam — não chama __init__ real
    # (que abriria conexão de rede).
    import threading

    conn.config = MagicMock()
    # MagicMock.__float__ retorna 1.0 — explicitar desligado evita cap acidental
    conn.config.SIMULATED_BALANCE_USD = 0.0
    conn.config.USE_TESTNET = False
    conn.client = MagicMock()
    conn._cache_lock = threading.Lock()
    conn._balance_cache = None
    conn._balance_cache_ttl = 2.0
    conn._funding_rate_cache = {}
    conn._funding_rate_cache_ttl = 300.0
    conn._daily_pnl_cache = None
    conn._daily_pnl_cache_ttl = 30.0
    conn._positions_cache = None
    conn._positions_cache_ttl = 5.0
    conn._order_stats_lock = threading.Lock()
    conn._order_stats_since_report = {
        "attempts": 0, "successes": 0, "failures": 0, "rejections": 0, "symbols": {}
    }
    return conn


# ---------------------------------------------------------------------------
# Balance cache
# ---------------------------------------------------------------------------

def test_balance_cache_hit_within_ttl_avoids_second_api_call():
    client = _make_client()
    api_mock = MagicMock(return_value={
        "totalWalletBalance": "1000.0",
        "assets": [{"asset": "USDT", "availableBalance": "800.0"}],
    })
    client._api_call = api_mock

    # Primeira leitura: miss → 1 chamada à API
    assert client.get_account_balance() == 1000.0
    # Segunda leitura imediata: hit → sem chamada adicional
    assert client.get_account_balance() == 1000.0
    assert client.get_available_balance() == 800.0

    assert api_mock.call_count == 1


def test_balance_cache_expires_after_ttl():
    client = _make_client()
    client._balance_cache_ttl = 0.1  # 100ms para não atrasar o teste
    api_mock = MagicMock(return_value={
        "totalWalletBalance": "500.0",
        "assets": [{"asset": "USDT", "availableBalance": "450.0"}],
    })
    client._api_call = api_mock

    client.get_account_balance()
    assert api_mock.call_count == 1

    import time
    time.sleep(0.15)

    # Após TTL expirar, deve refazer a chamada
    client.get_account_balance()
    assert api_mock.call_count == 2


def test_balance_force_refresh_ignores_cache():
    client = _make_client()
    api_mock = MagicMock(return_value={
        "totalWalletBalance": "100.0",
        "assets": [{"asset": "USDT", "availableBalance": "90.0"}],
    })
    client._api_call = api_mock

    client.get_account_balance()
    client.get_account_balance(force_refresh=True)
    client.get_available_balance(force_refresh=True)
    assert api_mock.call_count == 3


def test_balance_cache_survives_api_failure_with_stale_value():
    client = _make_client()
    good_response = {
        "totalWalletBalance": "2000.0",
        "assets": [{"asset": "USDT", "availableBalance": "1900.0"}],
    }
    side_effects = [good_response, RuntimeError("API down")]
    client._api_call = MagicMock(side_effect=side_effects)

    # Primeiro fetch bem-sucedido
    assert client.get_account_balance() == 2000.0
    # Força refresh enquanto API está down: devolve valor stale em vez de zerar
    assert client.get_account_balance(force_refresh=True) == 2000.0


def test_invalidate_balance_cache_forces_next_call_to_api():
    client = _make_client()
    api_mock = MagicMock(return_value={
        "totalWalletBalance": "100.0",
        "assets": [{"asset": "USDT", "availableBalance": "90.0"}],
    })
    client._api_call = api_mock

    client.get_account_balance()
    assert api_mock.call_count == 1

    # Invalidação explícita (simula o que place_market_order faz)
    client.invalidate_balance_cache()
    client.get_account_balance()
    assert api_mock.call_count == 2


def test_place_market_order_invalidates_balance_cache():
    client = _make_client()
    # Pré-popula o cache
    client._balance_cache = {"wallet": 500.0, "available": 450.0, "ts": 999999.0}

    # Mocka o que place_market_order chama internamente
    client.get_symbol_info = MagicMock(return_value={"quantityPrecision": 3})
    client._api_call = MagicMock(return_value={"orderId": 1, "status": "FILLED"})
    client._record_order_stat = MagicMock()
    client._is_order_rejection = MagicMock(return_value=False)

    from trading_bot.infra.binance_client import BinanceConnection

    BinanceConnection.place_market_order(
        client,
        symbol="ETHUSDT", side="BUY", position_side="LONG", quantity=0.01,
    )

    assert client._balance_cache is None


# ---------------------------------------------------------------------------
# Funding rate cache
# ---------------------------------------------------------------------------

def test_funding_rate_cache_hit_within_ttl():
    client = _make_client()

    # funding_rate faz 2 _api_call chamadas (futures_funding_rate + futures_mark_price)
    def _api_side_effect(endpoint, *args, **kwargs):
        if endpoint == "futures_funding_rate":
            return [{"fundingRate": "0.0001"}]
        if endpoint == "futures_mark_price":
            return {"nextFundingTime": 1700000000000}
        return {}

    api_mock = MagicMock(side_effect=_api_side_effect)
    client._api_call = api_mock

    result1 = client.get_funding_rate("ETHUSDT")
    result2 = client.get_funding_rate("ETHUSDT")

    assert result1 == result2
    assert result1["rate"] == 0.0001
    # 1ª chamada fez 2 API calls, 2ª chamada usou cache → total 2 API calls
    assert api_mock.call_count == 2


def test_funding_rate_cache_per_symbol():
    client = _make_client()

    def _api_side_effect(endpoint, *args, **kwargs):
        if endpoint == "futures_funding_rate":
            sym = kwargs.get("symbol")
            rate = "0.0002" if sym == "BTCUSDT" else "0.0001"
            return [{"fundingRate": rate}]
        if endpoint == "futures_mark_price":
            return {"nextFundingTime": 1700000000000}
        return {}

    client._api_call = MagicMock(side_effect=_api_side_effect)

    eth = client.get_funding_rate("ETHUSDT")
    btc = client.get_funding_rate("BTCUSDT")

    assert eth["rate"] == 0.0001
    assert btc["rate"] == 0.0002


def test_funding_rate_force_refresh_ignores_cache():
    client = _make_client()
    call_count = {"n": 0}

    def _api_side_effect(endpoint, *args, **kwargs):
        if endpoint == "futures_funding_rate":
            call_count["n"] += 1
            return [{"fundingRate": "0.0001"}]
        return {"nextFundingTime": 1700000000000}

    client._api_call = MagicMock(side_effect=_api_side_effect)

    client.get_funding_rate("ETHUSDT")
    client.get_funding_rate("ETHUSDT", force_refresh=True)

    assert call_count["n"] == 2


def test_funding_rate_falls_back_to_stale_on_api_failure():
    client = _make_client()

    calls = {"n": 0}

    def _api_side_effect(endpoint, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            # Primeira passada bem-sucedida (2 calls internas)
            if endpoint == "futures_funding_rate":
                return [{"fundingRate": "0.0003"}]
            return {"nextFundingTime": 1700000000000}
        raise RuntimeError("API down")

    client._api_call = MagicMock(side_effect=_api_side_effect)

    first = client.get_funding_rate("ETHUSDT")
    second = client.get_funding_rate("ETHUSDT", force_refresh=True)

    # Após falha, retorna valor stale (não valores neutros zerados)
    assert second["rate"] == first["rate"] == 0.0003


# ---------------------------------------------------------------------------
# Daily PnL cache
# ---------------------------------------------------------------------------

def test_daily_pnl_cache_hit_within_ttl_avoids_second_api_call():
    client = _make_client()
    api_mock = MagicMock(return_value=[
        {"incomeType": "REALIZED_PNL", "income": "5.0"},
        {"incomeType": "COMMISSION", "income": "-0.2"},
    ])
    client._api_call = api_mock

    first = client.get_daily_pnl_from_binance()
    second = client.get_daily_pnl_from_binance()

    assert first == second
    assert first["realized_pnl"] == 5.0
    assert first["commission"] == -0.2
    assert api_mock.call_count == 1  # segunda leitura pegou do cache


def test_daily_pnl_cache_expires_after_ttl():
    client = _make_client()
    client._daily_pnl_cache_ttl = 0.1
    client._api_call = MagicMock(return_value=[])

    client.get_daily_pnl_from_binance()
    import time
    time.sleep(0.15)
    client.get_daily_pnl_from_binance()

    assert client._api_call.call_count == 2


def test_daily_pnl_force_refresh_bypasses_cache():
    client = _make_client()
    client._api_call = MagicMock(return_value=[])

    client.get_daily_pnl_from_binance()
    client.get_daily_pnl_from_binance(force_refresh=True)
    assert client._api_call.call_count == 2


def test_daily_pnl_falls_back_to_stale_on_api_failure():
    client = _make_client()
    calls = {"n": 0}

    def side(endpoint, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"incomeType": "REALIZED_PNL", "income": "3.0"}]
        raise RuntimeError("API down")

    client._api_call = MagicMock(side_effect=side)

    first = client.get_daily_pnl_from_binance()
    second = client.get_daily_pnl_from_binance(force_refresh=True)

    # Stale survive — não zera o P&L quando a API cai
    assert second["realized_pnl"] == 3.0
    assert second == first


def test_invalidate_balance_cache_also_invalidates_daily_pnl():
    client = _make_client()
    client._balance_cache = {"wallet": 100.0, "available": 90.0, "ts": 123.0}
    client._daily_pnl_cache = {"data": {"total": 5.0}, "ts": 123.0}

    client.invalidate_balance_cache()

    assert client._balance_cache is None
    assert client._daily_pnl_cache is None


# ---------------------------------------------------------------------------
# Phantom-close regression
# ---------------------------------------------------------------------------

def test_get_open_positions_raises_on_api_error_not_empty_list():
    """Regressão: erro transitório (-1021 timestamp, rede, etc) NÃO pode
    retornar [] pois monitor_positions interpretaria como "todas fechadas"
    e dispararia phantom closes com P&L falso."""
    client = _make_client()
    client._api_call = MagicMock(side_effect=RuntimeError("APIError(-1021): Timestamp for this request is outside of the recvWindow"))

    try:
        client.get_open_positions()
        assert False, "get_open_positions deveria ter propagado a exceção"
    except RuntimeError as e:
        assert "-1021" in str(e)


def test_get_open_positions_returns_empty_only_when_truly_empty():
    """Lista vazia apenas quando API responde sem posições — nunca por erro."""
    client = _make_client()
    client._api_call = MagicMock(return_value=[
        {"symbol": "BTCUSDT", "positionAmt": "0", "entryPrice": "0"},
        {"symbol": "ETHUSDT", "positionAmt": "0.0", "entryPrice": "0"},
    ])

    result = client.get_open_positions()
    assert result == []


def test_simulated_balance_cap_testnet_applies_override():
    """Em testnet com SIMULATED_BALANCE_USD > 0, wallet e available são cappados."""
    client = _make_client()
    client.config.SIMULATED_BALANCE_USD = 173.49
    client.config.USE_TESTNET = True
    client._api_call = MagicMock(return_value={
        "totalWalletBalance": "4750.0",
        "assets": [{"asset": "USDT", "availableBalance": "4204.0"}],
    })

    # Wallet vira exatamente o cap
    assert client.get_account_balance() == 173.49
    # Available = cap − margem_usada (4750-4204=546) → 0 (cap < margem)
    assert client.get_available_balance(force_refresh=True) == 0.0


def test_simulated_balance_cap_preserves_margin_when_cap_exceeds_usage():
    """Quando cap > margem, available = cap − margem."""
    client = _make_client()
    client.config.SIMULATED_BALANCE_USD = 1000.0
    client.config.USE_TESTNET = True
    client._api_call = MagicMock(return_value={
        "totalWalletBalance": "4750.0",
        "assets": [{"asset": "USDT", "availableBalance": "4204.0"}],
    })

    assert client.get_account_balance() == 1000.0
    # Margem real = 546; available simulado = 1000 - 546 = 454
    assert client.get_available_balance(force_refresh=True) == 454.0


def test_simulated_balance_cap_ignored_on_mainnet():
    """Por segurança, cap é ignorado em mainnet."""
    client = _make_client()
    client.config.SIMULATED_BALANCE_USD = 173.49
    client.config.USE_TESTNET = False
    client._api_call = MagicMock(return_value={
        "totalWalletBalance": "4750.0",
        "assets": [{"asset": "USDT", "availableBalance": "4204.0"}],
    })

    assert client.get_account_balance() == 4750.0
    assert client.get_available_balance(force_refresh=True) == 4204.0


def test_simulated_balance_cap_disabled_when_zero():
    """SIMULATED_BALANCE_USD = 0 é no-op."""
    client = _make_client()
    client.config.SIMULATED_BALANCE_USD = 0.0
    client.config.USE_TESTNET = True
    client._api_call = MagicMock(return_value={
        "totalWalletBalance": "4750.0",
        "assets": [{"asset": "USDT", "availableBalance": "4204.0"}],
    })

    assert client.get_account_balance() == 4750.0
    assert client.get_available_balance(force_refresh=True) == 4204.0


def test_get_open_positions_filters_zero_quantity():
    """Só retorna posições com quantity != 0."""
    client = _make_client()
    client.config.LEVERAGE = 10
    client._api_call = MagicMock(return_value=[
        {"symbol": "BTCUSDT", "positionAmt": "0.5", "entryPrice": "50000",
         "markPrice": "51000", "unRealizedProfit": "500", "leverage": "10"},
        {"symbol": "ETHUSDT", "positionAmt": "0", "entryPrice": "0",
         "markPrice": "0", "unRealizedProfit": "0", "leverage": "10"},
        {"symbol": "SOLUSDT", "positionAmt": "-10", "entryPrice": "100",
         "markPrice": "98", "unRealizedProfit": "20", "leverage": "10"},
    ])

    result = client.get_open_positions()
    assert len(result) == 2
    assert result[0]["symbol"] == "BTCUSDT"
    assert result[0]["side"] == "LONG"
    assert result[1]["symbol"] == "SOLUSDT"
    assert result[1]["side"] == "SHORT"

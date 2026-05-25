"""Testes do cooldown estrutural por símbolo após rejeições da Binance (ex.: -2027)."""

import threading
import time
from unittest.mock import MagicMock


def _make_client(cooldown_seconds: int = 1800):
    """Cria um BinanceConnection sem conectar de fato, via __new__."""
    from trading_bot.infra.binance_client import BinanceConnection

    conn = BinanceConnection.__new__(BinanceConnection)
    conn.config = MagicMock()
    conn.config.SIMULATED_BALANCE_USD = 0.0
    conn.config.USE_TESTNET = False
    conn.config.SYMBOL_STRUCTURAL_COOLDOWN_SECONDS = cooldown_seconds
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
    conn._symbol_cooldowns_lock = threading.Lock()
    conn._symbol_cooldowns = {}
    return conn


class _FakeApiError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def test_structural_error_minus_2027_triggers_cooldown():
    client = _make_client()
    client.get_symbol_info = MagicMock(return_value={"quantityPrecision": 3})
    client._api_call = MagicMock(
        side_effect=_FakeApiError(-2027, "Exceeded the maximum allowable position at current leverage.")
    )

    from trading_bot.infra.binance_client import BinanceConnection

    result = BinanceConnection.place_market_order(
        client, symbol="ESPUSDT", side="BUY", position_side="LONG", quantity=400.0
    )

    assert result is None
    assert client.is_symbol_on_cooldown("ESPUSDT") is True
    info = client.get_symbol_cooldown_info("ESPUSDT")
    assert info is not None
    assert info["code"] == -2027
    assert info["remaining_seconds"] > 0


def test_cooldown_short_circuits_subsequent_orders_without_api_call():
    client = _make_client()
    client.get_symbol_info = MagicMock(return_value={"quantityPrecision": 3})
    # 1ª chamada dispara -2027
    client._api_call = MagicMock(
        side_effect=_FakeApiError(-2027, "Exceeded the maximum allowable position at current leverage.")
    )

    from trading_bot.infra.binance_client import BinanceConnection

    BinanceConnection.place_market_order(
        client, symbol="ESPUSDT", side="BUY", position_side="LONG", quantity=400.0
    )
    assert client._api_call.call_count == 1

    # 2ª, 3ª e 4ª: cooldown ativo, não devem nem tocar a API
    for _ in range(3):
        result = BinanceConnection.place_market_order(
            client, symbol="ESPUSDT", side="BUY", position_side="LONG", quantity=400.0
        )
        assert result is None

    assert client._api_call.call_count == 1  # nenhuma chamada extra à exchange

    # Stats devem refletir as 4 tentativas, todas como rejeições
    stats = client._order_stats_since_report["symbols"]["ESPUSDT"]
    assert stats["attempts"] == 4
    assert stats["rejections"] == 4
    assert stats["successes"] == 0


def test_cooldown_does_not_block_other_symbols():
    client = _make_client()
    client.get_symbol_info = MagicMock(return_value={"quantityPrecision": 3})

    # ESPUSDT falha com -2027; ETHUSDT sucede
    def _side_effect(*args, **kwargs):
        sym = kwargs.get("symbol") or (args[1] if len(args) > 1 else "")
        if sym == "ESPUSDT":
            raise _FakeApiError(-2027, "max position")
        return {"orderId": 1, "status": "FILLED"}

    client._api_call = MagicMock(side_effect=_side_effect)
    client.invalidate_balance_cache = MagicMock()
    client.invalidate_positions_cache = MagicMock()

    from trading_bot.infra.binance_client import BinanceConnection

    BinanceConnection.place_market_order(
        client, symbol="ESPUSDT", side="BUY", position_side="LONG", quantity=400.0
    )
    assert client.is_symbol_on_cooldown("ESPUSDT") is True

    # ETHUSDT não deve estar em cooldown
    assert client.is_symbol_on_cooldown("ETHUSDT") is False
    eth_result = BinanceConnection.place_market_order(
        client, symbol="ETHUSDT", side="BUY", position_side="LONG", quantity=0.01
    )
    assert eth_result == {"orderId": 1, "status": "FILLED"}


def test_cooldown_expires_after_duration():
    # Cooldown muito curto pra forçar expiração natural em teste
    client = _make_client(cooldown_seconds=0)  # duração=0 ⇒ não persiste


    # Com duração 0 o cooldown não é registrado
    client._set_symbol_cooldown("ESPUSDT", -2027, "max position")
    assert client.is_symbol_on_cooldown("ESPUSDT") is False

    # Duração positiva: registra, depois expira
    client.config.SYMBOL_STRUCTURAL_COOLDOWN_SECONDS = 60
    client._set_symbol_cooldown("ESPUSDT", -2027, "max position")
    assert client.is_symbol_on_cooldown("ESPUSDT") is True

    # Força expiração manipulando o until_monotonic
    client._symbol_cooldowns["ESPUSDT"]["until_monotonic"] = time.monotonic() - 1
    assert client.is_symbol_on_cooldown("ESPUSDT") is False
    # E após consulta, a entrada foi limpa
    assert "ESPUSDT" not in client._symbol_cooldowns


def test_non_structural_error_does_not_trigger_cooldown():
    client = _make_client()
    client.get_symbol_info = MagicMock(return_value={"quantityPrecision": 3})
    # -2019: margin insufficient — é rejection, mas não estrutural
    client._api_call = MagicMock(side_effect=_FakeApiError(-2019, "Margin is insufficient"))

    from trading_bot.infra.binance_client import BinanceConnection

    BinanceConnection.place_market_order(
        client, symbol="ESPUSDT", side="BUY", position_side="LONG", quantity=400.0
    )
    assert client.is_symbol_on_cooldown("ESPUSDT") is False


def test_clear_symbol_cooldown_removes_active_cooldown():
    client = _make_client()
    client._set_symbol_cooldown("ESPUSDT", -2027, "max position")
    assert client.is_symbol_on_cooldown("ESPUSDT") is True

    assert client.clear_symbol_cooldown("ESPUSDT") is True
    assert client.is_symbol_on_cooldown("ESPUSDT") is False
    # Idempotente
    assert client.clear_symbol_cooldown("ESPUSDT") is False


def test_close_position_returns_false_when_order_fails():
    """
    Regressão: -1007 (Timeout/Unknown) na ordem de fechamento fazia
    close_position retornar True mesmo com place_market_order retornando
    None. Isso causava bookkeeping fantasma e loop de close (TEST/PROD
    em 2026-05-25, +200 closes fake da mesma posição DOGE em 1 min).
    """
    client = _make_client()
    client.get_open_positions = MagicMock(return_value=[
        {"symbol": "DOGEUSDT", "side": "LONG", "quantity": 291.0}
    ])
    # Simula falha de envio (timeout / rejection) — place_market_order
    # captura a exceção e retorna None.
    client.get_symbol_info = MagicMock(return_value={"quantityPrecision": 0})
    client._api_call = MagicMock(side_effect=_FakeApiError(-1007, "Timeout"))

    from trading_bot.infra.binance_client import BinanceConnection

    result = BinanceConnection.close_position(client, "DOGEUSDT", "LONG")
    assert result is False


def test_close_position_returns_true_when_order_succeeds():
    client = _make_client()
    client.get_open_positions = MagicMock(return_value=[
        {"symbol": "DOGEUSDT", "side": "LONG", "quantity": 291.0}
    ])
    client.get_symbol_info = MagicMock(return_value={"quantityPrecision": 0})
    client._api_call = MagicMock(return_value={"orderId": 1, "status": "FILLED"})

    from trading_bot.infra.binance_client import BinanceConnection

    result = BinanceConnection.close_position(client, "DOGEUSDT", "LONG")
    assert result is True

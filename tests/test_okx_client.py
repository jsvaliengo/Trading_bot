"""OkxConnection com HTTP mockado (session.request): assinatura, símbolo/contratos,
posições em hedge, ordens LONG/SHORT abrir e fechar, symbol_info, klines e bills."""

import base64
import hashlib
import hmac
import json
import time
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest

from trading_bot.core.config import config
from trading_bot.infra import okx_client as okx
from trading_bot.infra.okx_client import OkxConnection, coins_to_contracts, contracts_to_coins, from_inst_id, to_inst_id

ADA = {"instId": "ADA-USDT-SWAP", "ctVal": "100", "lotSz": "0.1", "minSz": "0.1", "tickSz": "0.0001", "state": "live",
       "ctValCcy": "ADA"}
TRX = {"instId": "TRX-USDT-SWAP", "ctVal": "1000", "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.00001", "state": "live"}
BNB = {"instId": "BNB-USDT-SWAP", "ctVal": "0.01", "lotSz": "1", "minSz": "1", "tickSz": "0.01", "state": "live"}
SPOT = {"instId": "BTC-USDT", "ctVal": "", "lotSz": "0.0001", "minSz": "0.0001", "tickSz": "0.1", "state": "live"}
BALANCE = {"details": [{"ccy": "USDT", "cashBal": "300.5", "availBal": "250.25", "upl": "-1.5", "eq": "299", "imr": "50"}]}


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeHttp:
    """Roteia (MÉTODO, path) → payload ou função(query, body) → payload. Guarda as chamadas."""

    def __init__(self):
        self.routes = {
            ("GET", "/api/v5/account/balance"): {"code": "0", "data": [BALANCE]},
            ("GET", "/api/v5/public/instruments"): {"code": "0", "data": [ADA, TRX, BNB, SPOT]},
            ("GET", "/api/v5/market/ticker"): {"code": "0", "data": [{"last": "0.5", "ts": "1700000000000"}]},
        }
        self.calls = []

    def __call__(self, method, url, data=None, headers=None, timeout=None):
        u = urlparse(url)
        query = {k: v[0] for k, v in parse_qs(u.query).items()}
        body = json.loads(data) if data else None
        self.calls.append({"method": method, "path": u.path, "query": query, "body": body, "headers": headers})
        route = self.routes.get((method, u.path))
        if route is None:
            return _Resp({"code": "51001", "msg": f"sem rota {method} {u.path}", "data": []})
        return _Resp(route(query, body) if callable(route) else route)


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setattr(config, "OKX_API_KEY", "key", raising=False)
    monkeypatch.setattr(config, "OKX_API_SECRET", "secret", raising=False)
    monkeypatch.setattr(config, "OKX_PASSPHRASE", "pass", raising=False)
    monkeypatch.setattr(config, "OKX_DEMO", True, raising=False)
    monkeypatch.setattr(config, "SIMULATED_BALANCE_USD", 0.0)
    http = FakeHttp()
    monkeypatch.setattr(okx.requests.Session, "request", lambda self, *a, **k: http(*a, **k))
    c = OkxConnection()
    c.http = http
    return c


# ---- assinatura / headers ----
def test_private_request_is_signed_per_okx_v5(conn):
    conn._request("GET", "/api/v5/account/balance", {"ccy": "USDT"})
    h = conn.http.calls[-1]["headers"]
    assert h["OK-ACCESS-KEY"] == "key" and h["OK-ACCESS-PASSPHRASE"] == "pass" and h["x-simulated-trading"] == "1"
    ts = h["OK-ACCESS-TIMESTAMP"]
    assert ts.endswith("Z") and len(ts) == len("2020-12-08T09:08:57.715Z")
    expected = base64.b64encode(hmac.new(b"secret", f"{ts}GET/api/v5/account/balance?ccy=USDT".encode(),
                                         hashlib.sha256).digest()).decode()
    assert h["OK-ACCESS-SIGN"] == expected


def test_post_signs_compact_json_body(conn):
    conn.http.routes[("POST", "/api/v5/account/set-leverage")] = {"code": "0", "data": [{}]}
    assert conn.set_leverage("ADAUSDT", 20) is True
    call = conn.http.calls[-1]
    assert call["body"] == {"instId": "ADA-USDT-SWAP", "lever": "20", "mgnMode": "cross"}
    assert "posSide" not in call["body"]  # cross: posSide só é aceito em isolated


def test_public_request_has_no_auth_headers_and_api_error_raises(conn):
    conn.get_symbol_ticker("ADAUSDT")
    assert "OK-ACCESS-SIGN" not in conn.http.calls[-1]["headers"]
    with pytest.raises(okx.OkxApiError) as exc:
        conn._request("GET", "/api/v5/nao/existe")
    assert exc.value.code == "51001"


# ---- conversões ----
def test_symbol_round_trip():
    assert to_inst_id("ADAUSDT") == "ADA-USDT-SWAP" and from_inst_id("ADA-USDT-SWAP") == "ADAUSDT"
    assert to_inst_id("ADA-USDT-SWAP") == "ADA-USDT-SWAP"
    with pytest.raises(ValueError):
        to_inst_id("BTCUSD")


def test_coins_to_contracts_floors_on_lot_and_checks_min():
    assert coins_to_contracts(34, ADA) == Decimal("0.3")        # 34 ADA → 0,3 ct (30 ADA), floor no lotSz
    assert coins_to_contracts(10, ADA) == Decimal("0.1")        # exato: 10/100/0.1 não pode cair pra 0 por float
    assert coins_to_contracts(9.9, ADA) == 0                    # abaixo do minSz
    assert coins_to_contracts(25_000, TRX) == Decimal("25")
    assert coins_to_contracts(0.0199, BNB) == Decimal("1")
    assert coins_to_contracts(0.005, BNB) == 0
    assert contracts_to_coins("0.3", ADA) == 30.0 and contracts_to_coins("2", BNB) == 0.02


# ---- symbol info / exchange info ----
def test_symbol_info_derives_min_notional_and_precision(conn):
    info = conn.get_symbol_info("ADAUSDT")
    assert info["minQty"] == 10.0 and info["minNotional"] == pytest.approx(5.0)   # 0.1 ct × 100 ADA × $0.5
    assert info["quantityPrecision"] == 0 and info["pricePrecision"] == 4 and info["stepSize"] == 10.0
    assert conn.get_symbol_info("BNBUSDT")["quantityPrecision"] == 2
    assert conn.get_symbol_info("NAOEXISTEUSDT") == {}
    # instrumentos vêm do cache: só 1 GET de instruments para as 3 consultas
    assert sum(c["path"] == "/api/v5/public/instruments" for c in conn.http.calls) == 1


def test_exchange_info_lists_only_usdt_perpetuals(conn):
    syms = {s["symbol"]: s for s in conn.get_exchange_info()["symbols"]}
    assert set(syms) == {"ADAUSDT", "TRXUSDT", "BNBUSDT"}
    assert syms["ADAUSDT"]["status"] == "TRADING" and syms["ADAUSDT"]["contractType"] == "PERPETUAL"


# ---- conta / posições ----
def test_account_balance_and_info(conn):
    assert conn.get_account_balance() == 300.5 and conn.get_available_balance() == 250.25
    info = conn.get_account_info()
    assert info == {"wallet_balance": 300.5, "available_balance": 250.25, "unrealized_pnl": -1.5,
                    "margin_balance": 299.0, "total_initial_margin": 50.0}


def test_open_positions_in_hedge_mode_convert_contracts_to_coins(conn):
    conn.http.routes[("GET", "/api/v5/account/positions")] = {"code": "0", "data": [
        {"instId": "ADA-USDT-SWAP", "posSide": "long", "pos": "0.3", "avgPx": "0.49", "markPx": "0.5", "upl": "0.3", "lever": "20"},
        {"instId": "ADA-USDT-SWAP", "posSide": "short", "pos": "0.1", "avgPx": "0.51", "markPx": "0.5", "upl": "0.1", "lever": "20"},
        {"instId": "TRX-USDT-SWAP", "posSide": "long", "pos": "0", "avgPx": "", "markPx": "", "upl": "", "lever": "20"},
    ]}
    pos = conn.get_open_positions(force_refresh=True)
    assert [(p["symbol"], p["side"], p["quantity"]) for p in pos] == [("ADAUSDT", "LONG", 30.0), ("ADAUSDT", "SHORT", 10.0)]
    assert pos[0]["entry_price"] == 0.49 and pos[0]["unrealized_pnl"] == 0.3 and pos[0]["leverage"] == 20


def test_open_positions_propagates_api_error(conn):
    with pytest.raises(okx.OkxApiError):
        conn.get_open_positions()


def test_set_hedge_mode_only_posts_when_needed(conn):
    conn.http.routes[("GET", "/api/v5/account/config")] = {"code": "0", "data": [{"posMode": "net_mode"}]}
    conn.http.routes[("POST", "/api/v5/account/set-position-mode")] = {"code": "0", "data": [{}]}
    assert conn.set_hedge_mode() is True
    assert conn.http.calls[-1]["body"] == {"posMode": "long_short_mode"}
    conn.http.routes[("GET", "/api/v5/account/config")] = {"code": "0", "data": [{"posMode": "long_short_mode"}]}
    n = len(conn.http.calls)
    assert conn.set_hedge_mode() is True and len(conn.http.calls) == n + 1


# ---- ordens ----
def _order_routes(conn, avg_px="0.5", acc_fill="0.3", pnl="0", s_code="0"):
    conn.http.routes[("POST", "/api/v5/trade/order")] = {"code": "0" if s_code == "0" else "1",
                                                          "data": [{"ordId": "123", "sCode": s_code, "sMsg": "x"}]}
    conn.http.routes[("GET", "/api/v5/trade/order")] = {"code": "0", "data": [
        {"ordId": "123", "avgPx": avg_px, "accFillSz": acc_fill, "state": "filled", "pnl": pnl, "fee": "-0.01"}]}


def test_open_long_sends_contracts_with_pos_side(conn):
    _order_routes(conn)
    order = conn.place_market_order(symbol="ADAUSDT", side="BUY", position_side="LONG", quantity=34)
    post = next(c for c in conn.http.calls if c["path"] == "/api/v5/trade/order" and c["method"] == "POST")
    assert post["body"] == {"instId": "ADA-USDT-SWAP", "tdMode": "cross", "side": "buy", "posSide": "long",
                            "ordType": "market", "sz": "0.3"}
    assert order["orderId"] == "123" and order["avgPrice"] == "0.5" and order["executedQty"] == "30.0"
    assert conn.pop_realized_close("ADAUSDT", "LONG") is None   # abertura não gera P&L realizado


def test_open_short_and_close_short_use_sell_then_buy(conn):
    _order_routes(conn, acc_fill="0.1")
    conn.place_market_order(symbol="ADAUSDT", side="SELL", position_side="SHORT", quantity=10)
    assert conn.http.calls[-2]["body"]["side"] == "sell" and conn.http.calls[-2]["body"]["posSide"] == "short"
    _order_routes(conn, avg_px="0.48", acc_fill="0.1", pnl="0.2")
    conn.http.routes[("GET", "/api/v5/account/positions")] = {"code": "0", "data": [
        {"instId": "ADA-USDT-SWAP", "posSide": "short", "pos": "0.1", "avgPx": "0.5", "markPx": "0.48", "upl": "0.2", "lever": "20"}]}
    order = conn.close_position("ADAUSDT", "SHORT")
    post = [c for c in conn.http.calls if c["path"] == "/api/v5/trade/order" and c["method"] == "POST"][-1]
    assert post["body"]["side"] == "buy" and post["body"]["posSide"] == "short" and post["body"]["sz"] == "0.1"
    assert order["avgPrice"] == "0.48"
    realized = conn.pop_realized_close("ADAUSDT", "SHORT", since_ms=int(time.time() * 1000) - 5000)
    assert realized == {"gross": 0.2, "exit_price": 0.48, "qty": 10.0}
    assert conn.pop_realized_close("ADAUSDT", "SHORT") is None    # consumido uma vez


def test_close_long_not_found_returns_none(conn):
    conn.http.routes[("GET", "/api/v5/account/positions")] = {"code": "0", "data": []}
    assert conn.close_position("ADAUSDT", "LONG") is None


def test_rejected_order_returns_none(conn):
    _order_routes(conn, s_code="51008")
    assert conn.place_market_order(symbol="ADAUSDT", side="BUY", position_side="LONG", quantity=34) is None


def test_qty_below_min_size_is_bumped_to_min_not_dropped(conn):
    _order_routes(conn, acc_fill="0.1")
    conn.place_market_order(symbol="ADAUSDT", side="BUY", position_side="LONG", quantity=9.6)  # preço subiu após o start
    post = [c for c in conn.http.calls if c["path"] == "/api/v5/trade/order" and c["method"] == "POST"][-1]
    assert post["body"]["sz"] == "0.1"


def test_cancel_open_orders_batches_pending(conn):
    conn.http.routes[("GET", "/api/v5/trade/orders-pending")] = {"code": "0", "data": [{"ordId": "1"}, {"ordId": "2"}]}
    conn.http.routes[("POST", "/api/v5/trade/cancel-batch-orders")] = {"code": "0", "data": []}
    assert conn.cancel_open_orders("ADAUSDT") is True
    assert conn.http.calls[-1]["body"] == [{"instId": "ADA-USDT-SWAP", "ordId": "1"}, {"instId": "ADA-USDT-SWAP", "ordId": "2"}]


# ---- klines ----
def test_klines_raw_is_ascending_binance_like(conn):
    # OKX: decrescente, [ts, o, h, l, c, vol(ct), volCcy, volCcyQuote, confirm]
    conn.http.routes[("GET", "/api/v5/market/candles")] = {"code": "0", "data": [
        ["1700006400000", "0.50", "0.52", "0.49", "0.51", "1000", "100000", "50500", "0"],
        ["1700003600000", "0.48", "0.50", "0.47", "0.50", "2000", "200000", "97000", "1"],
    ]}
    rows = conn.get_klines_raw("ADAUSDT", "1h", limit=2)
    assert conn.http.calls[-1]["query"] == {"instId": "ADA-USDT-SWAP", "bar": "1H", "limit": "2"}
    assert [r[0] for r in rows] == [1700003600000, 1700006400000]
    assert rows[0][1:6] == ["0.48", "0.50", "0.47", "0.50", "200000"]   # volume em moedas (volCcy)
    assert rows[0][6] == 1700003600000 + 3_600_000 - 1 and rows[0][7] == "97000"
    assert len(rows[0]) == 12


def test_klines_raw_paginates_with_after_and_maps_daily_to_utc(conn):
    pages = []

    def route(query, _body):
        pages.append(query)
        n = int(query["limit"])
        start = int(query.get("after", "1700000000000"))
        return {"code": "0", "data": [[str(start - 86_400_000 * (i + 1)), "1", "1", "1", "1", "1", "1", "1", "1"]
                                      for i in range(n)]}

    conn.http.routes[("GET", "/api/v5/market/candles")] = route
    rows = conn.get_klines_raw("ADAUSDT", "1d", limit=350)
    assert len(rows) == 350 and rows[0][0] < rows[-1][0]
    assert pages[0]["bar"] == "1Dutc" and pages[0]["limit"] == "300" and pages[1]["limit"] == "50" and "after" in pages[1]


def test_get_klines_drops_open_candle(conn, monkeypatch):
    now_ms = 1700008000000   # 1700003600000 fecha em 1700007199999 (fechada); 1700006400000 ainda aberta
    monkeypatch.setattr(okx.time, "time", lambda: now_ms / 1000)
    conn.http.routes[("GET", "/api/v5/market/candles")] = {"code": "0", "data": [
        ["1700006400000", "0.50", "0.52", "0.49", "0.51", "1000", "100000", "50500", "0"],
        ["1700003600000", "0.48", "0.50", "0.47", "0.50", "2000", "200000", "97000", "1"],
    ]}
    assert conn.get_klines("ADAUSDT", "1h", limit=1) == [
        {"timestamp": 1700003600000, "open": 0.48, "high": 0.5, "low": 0.47, "close": 0.5, "volume": 200000.0}]


# ---- bills → income ----
BILLS = [
    {"billId": "3", "type": "2", "subType": "5", "instId": "ADA-USDT-SWAP", "pnl": "0.35", "fee": "-0.01", "balChg": "0.34", "ts": "1700000300000", "ccy": "USDT"},
    {"billId": "2", "type": "8", "subType": "173", "instId": "ADA-USDT-SWAP", "pnl": "0", "fee": "0", "balChg": "-0.02", "ts": "1700000200000", "ccy": "USDT"},
    {"billId": "1", "type": "2", "subType": "3", "instId": "ADA-USDT-SWAP", "pnl": "0", "fee": "-0.01", "balChg": "-0.01", "ts": "1700000100000", "ccy": "USDT"},
]


def test_income_history_maps_bills_ascending(conn, monkeypatch):
    monkeypatch.setattr(okx.time, "time", lambda: 1700000400.0)   # start_time recente → /account/bills
    conn.http.routes[("GET", "/api/v5/account/bills")] = {"code": "0", "data": BILLS}
    rows = conn.get_income_history(limit=100)
    assert [(r["incomeType"], r["income"], r["time"]) for r in rows] == [
        ("COMMISSION", "-0.01", 1700000100000), ("FUNDING_FEE", "-0.02", 1700000200000),
        ("REALIZED_PNL", "0.35", 1700000300000), ("COMMISSION", "-0.01", 1700000300000)]
    assert rows[0]["symbol"] == "ADAUSDT"
    pnl = conn.get_income_history(income_type="REALIZED_PNL", symbol="ADAUSDT", start_time=1700000000000)
    assert [r["income"] for r in pnl] == ["0.35"]
    q = conn.http.calls[-1]["query"]
    assert q["type"] == "2" and q["instId"] == "ADA-USDT-SWAP" and q["begin"] == "1700000000000"


def test_old_start_time_uses_archive_and_daily_pnl_sums(conn, monkeypatch):
    monkeypatch.setattr(okx.time, "time", lambda: 1700000400.0)
    conn.http.routes[("GET", "/api/v5/account/bills")] = {"code": "0", "data": BILLS}
    daily = conn.get_daily_pnl_from_binance()
    assert daily["realized_pnl"] == 0.35 and daily["funding_fee"] == -0.02 and daily["commission"] == pytest.approx(-0.02)
    assert daily["total"] == pytest.approx(0.31) and daily["income_count"] == 4
    conn.http.routes[("GET", "/api/v5/account/bills-archive")] = {"code": "0", "data": BILLS}
    cum = conn.get_cumulative_income_from_binance(start_ms=1700000400000 - 30 * 86_400_000)
    assert cum["funding_fee"] == -0.02 and conn.http.calls[-1]["path"] == "/api/v5/account/bills-archive"


def test_income_history_fail_open_or_raise(conn):
    assert conn.get_income_history() == []
    with pytest.raises(okx.OkxApiError):
        conn.get_income_history(raise_errors=True)


def test_account_trades_map_fills(conn):
    conn.http.routes[("GET", "/api/v5/trade/fills-history")] = {"code": "0", "data": [
        {"tradeId": "t2", "ordId": "o2", "billId": "b2", "fillPx": "0.51", "fillSz": "0.3", "fillPnl": "0.3", "fee": "-0.01",
         "side": "sell", "posSide": "long", "execType": "T", "ts": "1700000200000", "feeCcy": "USDT"},
        {"tradeId": "t1", "ordId": "o1", "billId": "b1", "fillPx": "0.50", "fillSz": "0.3", "fillPnl": "0", "fee": "-0.01",
         "side": "buy", "posSide": "long", "execType": "T", "ts": "1700000100000", "feeCcy": "USDT"},
    ]}
    rows = conn.get_account_trades("ADAUSDT", 1700000000000, 1700000300000)
    assert [r["id"] for r in rows] == ["t1", "t2"]
    assert rows[1] == {"id": "t2", "orderId": "o2", "symbol": "ADAUSDT", "time": 1700000200000, "price": "0.51", "qty": "30.0",
                       "realizedPnl": "0.3", "commission": "0.01", "commissionAsset": "USDT", "side": "SELL",
                       "positionSide": "LONG", "maker": False}


def test_commission_and_funding_rate(conn):
    conn.http.routes[("GET", "/api/v5/account/trade-fee")] = {"code": "0", "data": [{"maker": "-0.0002", "taker": "-0.0005"}]}
    assert conn.get_commission_rates()["taker_rate"] == 0.0005
    conn.http.routes[("GET", "/api/v5/public/funding-rate")] = {"code": "0", "data": [{"fundingRate": "0.0001", "nextFundingTime": "1700000000000"}]}
    fr = conn.get_funding_rate("ADAUSDT")
    assert fr["long_pays"] and fr["favorable_side"] == "SHORT" and fr["next_funding_time"].year == 2023


def test_factory_picks_okx_by_env(monkeypatch):
    from trading_bot.infra import exchange as factory
    monkeypatch.setattr(config, "EXCHANGE", "okx", raising=False)
    monkeypatch.setattr(okx.OkxConnection, "__init__", lambda self: None)
    assert isinstance(factory.create_exchange(), okx.OkxConnection)


def test_instruments_load_even_when_host_uptime_is_below_ttl(monkeypatch, conn):
    """Host recém-ligado: monotonic() < TTL. Sentinela 0.0 fazia o cache vazio parecer fresco (CI falhava)."""
    monkeypatch.setattr(okx.time, "monotonic", lambda: 10.0)
    assert conn.get_exchange_info()["symbols"]

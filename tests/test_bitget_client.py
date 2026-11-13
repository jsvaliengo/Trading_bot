"""BitgetConnection com HTTP mockado (session.request): assinatura, símbolo demo, passo de qty,
posições em hedge, ordens LONG/SHORT abrir e fechar, symbol_info, candles, bills e fills."""

import base64
import hashlib
import hmac
import json
import time
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest

from trading_bot.core.config import config
from trading_bot.infra import bitget_client as bg
from trading_bot.infra.bitget_client import BitgetConnection, floor_qty

ADA = {"symbol": "ADAUSDT", "minTradeNum": "1", "sizeMultiplier": "1", "volumePlace": "0", "pricePlace": "4",
       "priceEndStep": "1", "minTradeUSDT": "5", "makerFeeRate": "0.0002", "takerFeeRate": "0.0006", "maxLever": "75",
       "symbolStatus": "normal", "symbolType": "perpetual"}
BNB = {"symbol": "BNBUSDT", "minTradeNum": "0.01", "sizeMultiplier": "0.01", "volumePlace": "2", "pricePlace": "2",
       "priceEndStep": "1", "minTradeUSDT": "5", "makerFeeRate": "0.0002", "takerFeeRate": "0.0006", "maxLever": "75",
       "symbolStatus": "normal", "symbolType": "perpetual"}
DELIVERY = {**ADA, "symbol": "BTCUSDT1231", "symbolType": "delivery"}
ACCOUNTS = [{"marginCoin": "USDT", "accountEquity": "299", "unrealizedPL": "-1.5", "available": "250.25",
             "crossedMargin": "50", "locked": "0"}]


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeHttp:
    """Roteia (MÉTODO, path) → payload ou função(query, body) → payload. Guarda as chamadas."""

    def __init__(self):
        self.routes = {
            ("GET", "/api/v2/mix/account/accounts"): {"code": "00000", "data": ACCOUNTS},
            ("GET", "/api/v2/mix/market/contracts"): {"code": "00000", "data": [ADA, BNB, DELIVERY]},
            ("GET", "/api/v2/mix/market/ticker"): {"code": "00000", "data": [{"lastPr": "0.5", "ts": "1700000000000"}]},
        }
        self.calls = []

    def __call__(self, method, url, data=None, headers=None, timeout=None):
        u = urlparse(url)
        query = {k: v[0] for k, v in parse_qs(u.query).items()}
        body = json.loads(data) if data else None
        self.calls.append({"method": method, "path": u.path, "query": query, "body": body, "headers": headers})
        route = self.routes.get((method, u.path))
        if route is None:
            return _Resp({"code": "40404", "msg": f"sem rota {method} {u.path}", "data": None})
        return _Resp(route(query, body) if callable(route) else route)


def _make(monkeypatch, demo=False):
    monkeypatch.setattr(config, "BITGET_API_KEY", "key", raising=False)
    monkeypatch.setattr(config, "BITGET_API_SECRET", "secret", raising=False)
    monkeypatch.setattr(config, "BITGET_PASSPHRASE", "pass", raising=False)
    monkeypatch.setattr(config, "BITGET_DEMO", demo, raising=False)
    monkeypatch.setattr(config, "SIMULATED_BALANCE_USD", 0.0)
    http = FakeHttp()
    monkeypatch.setattr(bg.requests.Session, "request", lambda self, *a, **k: http(*a, **k))
    c = BitgetConnection()
    c.http = http
    return c


@pytest.fixture
def conn(monkeypatch):
    return _make(monkeypatch)


# ---- assinatura / headers ----
def test_private_request_is_signed_per_bitget_v2(conn):
    conn._request("GET", "/api/v2/mix/account/accounts", {"productType": "USDT-FUTURES"})
    h = conn.http.calls[-1]["headers"]
    assert h["ACCESS-KEY"] == "key" and h["ACCESS-PASSPHRASE"] == "pass" and h["locale"] == "en-US"
    assert "paptrading" not in h
    ts = h["ACCESS-TIMESTAMP"]
    assert ts.isdigit() and len(ts) == 13
    expected = base64.b64encode(hmac.new(b"secret", f"{ts}GET/api/v2/mix/account/accounts?productType=USDT-FUTURES".encode(),
                                         hashlib.sha256).digest()).decode()
    assert h["ACCESS-SIGN"] == expected


def test_post_signs_compact_json_body(conn):
    conn.http.routes[("POST", "/api/v2/mix/account/set-margin-mode")] = {"code": "00000", "data": {}}
    conn.http.routes[("POST", "/api/v2/mix/account/set-leverage")] = {"code": "00000", "data": {}}
    assert conn.set_leverage("ADAUSDT", 20) is True
    call = conn.http.calls[-1]
    assert call["body"] == {"symbol": "ADAUSDT", "productType": "USDT-FUTURES", "marginCoin": "USDT", "leverage": "20"}
    assert "holdSide" not in call["body"]  # cross: holdSide não se aplica
    assert conn.http.calls[-2]["body"]["marginMode"] == "crossed"
    ts = call["headers"]["ACCESS-TIMESTAMP"]
    raw = json.dumps(call["body"], separators=(",", ":"))
    expected = base64.b64encode(hmac.new(b"secret", f"{ts}POST/api/v2/mix/account/set-leverage{raw}".encode(),
                                         hashlib.sha256).digest()).decode()
    assert call["headers"]["ACCESS-SIGN"] == expected


def test_public_request_has_no_auth_headers_and_api_error_raises(conn):
    conn.get_symbol_ticker("ADAUSDT")
    assert "ACCESS-SIGN" not in conn.http.calls[-1]["headers"]
    with pytest.raises(bg.BitgetApiError) as exc:
        conn._request("GET", "/api/v2/nao/existe")
    assert exc.value.code == "40404"


# ---- demo ----
def test_demo_sends_paptrading_only_on_private_calls_and_keeps_prod_symbols(monkeypatch):
    c = _make(monkeypatch, demo=True)
    assert c.http.calls[-1]["headers"]["paptrading"] == "1"
    assert c.http.calls[-1]["query"]["productType"] == "USDT-FUTURES"
    assert c.to_bitget("ADAUSDT") == "ADAUSDT" and c.from_bitget("ADAUSDT") == "ADAUSDT"
    c.get_symbol_ticker("BTCUSDT")
    assert "paptrading" not in c.http.calls[-1]["headers"]  # público com o header → vazio/40034 ao vivo
    assert c.http.calls[-1]["query"] == {"symbol": "BTCUSDT", "productType": "USDT-FUTURES"}


# ---- conversões ----
def test_floor_qty_on_size_multiplier_and_min():
    assert floor_qty(34.7, ADA) == Decimal("34")
    assert floor_qty(0.9, ADA) == 0                      # abaixo do minTradeNum
    assert floor_qty(0.03, BNB) == Decimal("0.03")       # 0.03/0.01 = 2.9999… em binário não pode cair
    assert floor_qty(0.0199, BNB) == Decimal("0.01")
    assert floor_qty(0.005, BNB) == 0


# ---- symbol info / exchange info ----
def test_symbol_info_exposes_bitget_steps_as_binance_fields(conn):
    info = conn.get_symbol_info("ADAUSDT")
    assert info["minQty"] == 1.0 and info["minNotional"] == 5.0 and info["stepSize"] == 1.0
    assert info["quantityPrecision"] == 0 and info["pricePrecision"] == 4 and info["tickSize"] == pytest.approx(0.0001)
    assert info["maxLeverage"] == 75
    bnb = conn.get_symbol_info("BNBUSDT")
    assert bnb["quantityPrecision"] == 2 and bnb["tickSize"] == pytest.approx(0.01)
    assert conn.get_symbol_info("NAOEXISTEUSDT") == {}
    # contratos vêm do cache: só 1 GET de contracts para as 3 consultas
    assert sum(c["path"] == "/api/v2/mix/market/contracts" for c in conn.http.calls) == 1


def test_exchange_info_lists_only_perpetuals(conn):
    syms = {s["symbol"]: s for s in conn.get_exchange_info()["symbols"]}
    assert set(syms) == {"ADAUSDT", "BNBUSDT"}
    assert syms["ADAUSDT"]["status"] == "TRADING" and syms["ADAUSDT"]["contractType"] == "PERPETUAL"
    assert syms["ADAUSDT"]["baseAsset"] == "ADA"


# ---- conta / posições ----
def test_account_balance_and_info(conn):
    assert conn.get_account_balance() == 300.5 and conn.get_available_balance() == 250.25
    assert conn.get_account_info() == {"wallet_balance": 300.5, "available_balance": 250.25, "unrealized_pnl": -1.5,
                                       "margin_balance": 299.0, "total_initial_margin": 50.0}


def test_open_positions_in_hedge_mode(conn):
    conn.http.routes[("GET", "/api/v2/mix/position/all-position")] = {"code": "00000", "data": [
        {"symbol": "ADAUSDT", "holdSide": "long", "total": "30", "openPriceAvg": "0.49", "markPrice": "0.5",
         "unrealizedPL": "0.3", "leverage": "20"},
        {"symbol": "ADAUSDT", "holdSide": "short", "total": "10", "openPriceAvg": "0.51", "markPrice": "0.5",
         "unrealizedPL": "0.1", "leverage": "20"},
        {"symbol": "BNBUSDT", "holdSide": "long", "total": "0", "openPriceAvg": "", "markPrice": "", "unrealizedPL": "",
         "leverage": "20"},
    ]}
    pos = conn.get_open_positions(force_refresh=True)
    assert [(p["symbol"], p["side"], p["quantity"]) for p in pos] == [("ADAUSDT", "LONG", 30.0), ("ADAUSDT", "SHORT", 10.0)]
    assert pos[0]["entry_price"] == 0.49 and pos[0]["unrealized_pnl"] == 0.3 and pos[0]["leverage"] == 20
    assert conn.http.calls[-1]["query"] == {"productType": "USDT-FUTURES", "marginCoin": "USDT"}


def test_open_positions_propagates_api_error(conn):
    with pytest.raises(bg.BitgetApiError):
        conn.get_open_positions()


def test_set_hedge_mode_posts_hedge_mode(conn):
    conn.http.routes[("POST", "/api/v2/mix/account/set-position-mode")] = {"code": "00000", "data": {"posMode": "hedge_mode"}}
    assert conn.set_hedge_mode() is True
    assert conn.http.calls[-1]["body"] == {"productType": "USDT-FUTURES", "posMode": "hedge_mode"}
    conn.http.routes[("POST", "/api/v2/mix/account/set-position-mode")] = {"code": "40xxx", "msg": "position exists"}
    assert conn.set_hedge_mode() is False


# ---- ordens ----
def _order_routes(conn, price_avg="0.5", base_vol="34", profit="0", ok=True):
    conn.http.routes[("POST", "/api/v2/mix/order/place-order")] = (
        {"code": "00000", "data": {"orderId": "123", "clientOid": ""}} if ok
        else {"code": "40762", "msg": "The order size is greater than the max open size", "data": None})
    conn.http.routes[("GET", "/api/v2/mix/order/detail")] = {"code": "00000", "data": {
        "orderId": "123", "priceAvg": price_avg, "baseVolume": base_vol, "state": "filled", "totalProfits": profit,
        "fee": "-0.01"}}


def _last_post(conn):
    return [c for c in conn.http.calls if c["path"] == "/api/v2/mix/order/place-order" and c["method"] == "POST"][-1]


def test_open_long_sends_buy_open_in_coins(conn):
    _order_routes(conn)
    order = conn.place_market_order(symbol="ADAUSDT", side="BUY", position_side="LONG", quantity=34.7)
    assert _last_post(conn)["body"] == {"symbol": "ADAUSDT", "productType": "USDT-FUTURES", "marginMode": "crossed",
                                        "marginCoin": "USDT", "size": "34", "side": "buy", "tradeSide": "open",
                                        "orderType": "market"}
    assert order["orderId"] == "123" and order["avgPrice"] == "0.5" and order["executedQty"] == "34.0"
    assert order["positionSide"] == "LONG" and order["side"] == "BUY"
    assert conn.pop_realized_close("ADAUSDT", "LONG") is None   # abertura não gera P&L realizado


def test_close_long_is_side_buy_trade_side_close(conn):
    _order_routes(conn, price_avg="0.52", base_vol="30", profit="0.9")
    conn.http.routes[("GET", "/api/v2/mix/position/all-position")] = {"code": "00000", "data": [
        {"symbol": "ADAUSDT", "holdSide": "long", "total": "30", "openPriceAvg": "0.49", "markPrice": "0.52",
         "unrealizedPL": "0.9", "leverage": "20"}]}
    order = conn.close_position("ADAUSDT", "LONG")
    body = _last_post(conn)["body"]
    assert body["side"] == "buy" and body["tradeSide"] == "close" and body["size"] == "30"
    assert order["avgPrice"] == "0.52"
    realized = conn.pop_realized_close("ADAUSDT", "LONG", since_ms=int(time.time() * 1000) - 5000)
    assert realized == {"gross": 0.9, "exit_price": 0.52, "qty": 30.0}
    assert conn.pop_realized_close("ADAUSDT", "LONG") is None    # consumido uma vez


def test_open_short_and_close_short_use_sell(conn):
    _order_routes(conn, base_vol="10")
    conn.place_market_order(symbol="ADAUSDT", side="SELL", position_side="SHORT", quantity=10)
    assert _last_post(conn)["body"]["side"] == "sell" and _last_post(conn)["body"]["tradeSide"] == "open"
    _order_routes(conn, price_avg="0.48", base_vol="10", profit="0.2")
    conn.http.routes[("GET", "/api/v2/mix/position/all-position")] = {"code": "00000", "data": [
        {"symbol": "ADAUSDT", "holdSide": "short", "total": "10", "openPriceAvg": "0.5", "markPrice": "0.48",
         "unrealizedPL": "0.2", "leverage": "20"}]}
    order = conn.close_position("ADAUSDT", "SHORT")
    assert _last_post(conn)["body"]["side"] == "sell" and _last_post(conn)["body"]["tradeSide"] == "close"
    assert order["avgPrice"] == "0.48"
    assert conn.pop_realized_close("ADAUSDT", "SHORT") == {"gross": 0.2, "exit_price": 0.48, "qty": 10.0}


def test_close_long_not_found_returns_none(conn):
    conn.http.routes[("GET", "/api/v2/mix/position/all-position")] = {"code": "00000", "data": []}
    assert conn.close_position("ADAUSDT", "LONG") is None


def test_rejected_order_returns_none(conn):
    _order_routes(conn, ok=False)
    assert conn.place_market_order(symbol="ADAUSDT", side="BUY", position_side="LONG", quantity=34) is None


def test_qty_below_min_is_bumped_to_min_not_dropped(conn):
    _order_routes(conn, base_vol="0.01")
    conn.place_market_order(symbol="BNBUSDT", side="BUY", position_side="LONG", quantity=0.006)  # preço subiu após o start
    assert _last_post(conn)["body"]["size"] == "0.01"


def test_demo_order_uses_prod_symbol_and_usdt_margin(monkeypatch):
    c = _make(monkeypatch, demo=True)
    c.http.routes[("GET", "/api/v2/mix/market/contracts")] = {"code": "00000", "data": [
        {**ADA, "symbol": "BTCUSDT", "minTradeNum": "0.0001", "sizeMultiplier": "0.0001", "volumePlace": "4"}]}
    _order_routes(c, price_avg="100000", base_vol="0.0001")
    order = c.place_market_order(symbol="BTCUSDT", side="BUY", position_side="LONG", quantity=0.0001)
    body = _last_post(c)["body"]
    assert body["symbol"] == "BTCUSDT" and body["productType"] == "USDT-FUTURES" and body["marginCoin"] == "USDT"
    assert _last_post(c)["headers"]["paptrading"] == "1"
    assert order["symbol"] == "BTCUSDT"
    assert c.get_account_balance() == 300.5


def test_cancel_open_orders_batches_pending(conn):
    conn.http.routes[("GET", "/api/v2/mix/order/orders-pending")] = {"code": "00000", "data": {
        "entrustedList": [{"orderId": "1"}, {"orderId": "2"}], "endId": "2"}}
    conn.http.routes[("POST", "/api/v2/mix/order/batch-cancel-orders")] = {"code": "00000", "data": {"successList": []}}
    assert conn.cancel_open_orders("ADAUSDT") is True
    assert conn.http.calls[-1]["body"] == {"symbol": "ADAUSDT", "productType": "USDT-FUTURES", "marginCoin": "USDT",
                                           "orderIdList": [{"orderId": "1"}, {"orderId": "2"}]}


# ---- klines ----
def test_klines_raw_is_ascending_binance_like(conn):
    # Bitget: ascendente, [ts, o, h, l, c, baseVol, quoteVol]
    conn.http.routes[("GET", "/api/v2/mix/market/candles")] = {"code": "00000", "data": [
        ["1700003600000", "0.48", "0.50", "0.47", "0.50", "200000", "97000"],
        ["1700006400000", "0.50", "0.52", "0.49", "0.51", "100000", "50500"],
    ]}
    rows = conn.get_klines_raw("ADAUSDT", "1h", limit=2)
    assert conn.http.calls[-1]["query"] == {"symbol": "ADAUSDT", "productType": "USDT-FUTURES", "granularity": "1H", "limit": "2"}
    assert [r[0] for r in rows] == [1700003600000, 1700006400000]
    assert rows[0][1:6] == ["0.48", "0.50", "0.47", "0.50", "200000"]
    assert rows[0][6] == 1700003600000 + 3_600_000 - 1 and rows[0][7] == "97000"
    assert len(rows[0]) == 12


def test_klines_daily_maps_to_utc_and_caps_limit(conn):
    conn.http.routes[("GET", "/api/v2/mix/market/candles")] = {"code": "00000", "data": []}
    conn.get_klines_raw("ADAUSDT", "1d", limit=5000)
    assert conn.http.calls[-1]["query"]["granularity"] == "1Dutc" and conn.http.calls[-1]["query"]["limit"] == "1000"


def test_klines_raw_unknown_symbol_40034_returns_empty_without_error_log(conn, caplog):
    # símbolo no formato Binance (1000PEPEUSDT) não existe na Bitget: [] silencioso no scan de universo
    conn.http.routes[("GET", "/api/v2/mix/market/candles")] = {"code": "40034", "msg": "Parameter 1000PEPEUSDT does not exist", "data": None}
    with caplog.at_level("ERROR"):
        assert conn.get_klines_raw("1000PEPEUSDT", "1d", limit=5) == []
    assert not caplog.records


def test_get_klines_drops_open_candle(conn, monkeypatch):
    now_ms = 1700008000000   # 1700003600000 fecha em 1700007199999 (fechada); 1700006400000 ainda aberta
    monkeypatch.setattr(bg.time, "time", lambda: now_ms / 1000)
    conn.http.routes[("GET", "/api/v2/mix/market/candles")] = {"code": "00000", "data": [
        ["1700003600000", "0.48", "0.50", "0.47", "0.50", "200000", "97000"],
        ["1700006400000", "0.50", "0.52", "0.49", "0.51", "100000", "50500"],
    ]}
    assert conn.get_klines("ADAUSDT", "1h", limit=1) == [
        {"timestamp": 1700003600000, "open": 0.48, "high": 0.5, "low": 0.47, "close": 0.5, "volume": 200000.0}]


# ---- bills → income ----
BILLS = [
    {"billId": "3", "symbol": "ADAUSDT", "amount": "0.35", "fee": "-0.01", "businessType": "close_long", "coin": "USDT", "cTime": "1700000300000"},
    {"billId": "2", "symbol": "ADAUSDT", "amount": "-0.02", "fee": "0", "businessType": "contract_settle_fee", "coin": "USDT", "cTime": "1700000200000"},
    {"billId": "1", "symbol": "ADAUSDT", "amount": "0", "fee": "-0.01", "businessType": "open_long", "coin": "USDT", "cTime": "1700000100000"},
    {"billId": "0", "symbol": "", "amount": "100", "fee": "0", "businessType": "trans_from_exchange", "coin": "USDT", "cTime": "1700000000000"},
]


def test_income_history_maps_bills_ascending(conn, monkeypatch):
    monkeypatch.setattr(bg.time, "time", lambda: 1700000400.0)
    conn.http.routes[("GET", "/api/v2/mix/account/bill")] = {"code": "00000", "data": {"bills": BILLS, "endId": "0"}}
    rows = conn.get_income_history(limit=100)
    assert [(r["incomeType"], r["income"], r["time"]) for r in rows] == [
        ("COMMISSION", "-0.01", 1700000100000), ("FUNDING_FEE", "-0.02", 1700000200000),
        ("REALIZED_PNL", "0.35", 1700000300000), ("COMMISSION", "-0.01", 1700000300000)]
    assert rows[0]["symbol"] == "ADAUSDT"   # transferência (sem open/close) ignorada
    pnl = conn.get_income_history(income_type="REALIZED_PNL", symbol="ADAUSDT", start_time=1700000000000)
    assert [r["income"] for r in pnl] == ["0.35"]
    q = conn.http.calls[-1]["query"]
    assert q["startTime"] == "1700000000000" and q["endTime"] == "1700000400000" and q["limit"] == "100"


def test_bills_split_in_30_day_windows_and_paginate(conn, monkeypatch):
    now = 1700000000000 + 45 * 86_400_000
    monkeypatch.setattr(bg.time, "time", lambda: now / 1000)
    pages = []

    def route(query, _body):
        pages.append(query)
        if "idLessThan" in query:
            return {"code": "00000", "data": {"bills": [], "endId": ""}}
        return {"code": "00000", "data": {"bills": [dict(b, cTime=str(int(query["startTime"]) + 1)) for b in BILLS[:1] * 100],
                                          "endId": "x"}}

    conn.http.routes[("GET", "/api/v2/mix/account/bill")] = route
    cum = conn.get_cumulative_income_from_binance(start_ms=1700000000000)
    assert cum["realized_pnl"] == pytest.approx(0.35 * 200) and cum["income_count"] == 400
    starts = [int(p["startTime"]) for p in pages if "idLessThan" not in p]
    assert starts == [1700000000000, 1700000000000 + 30 * 86_400_000]
    assert int(pages[-1]["endTime"]) == now and sum("idLessThan" in p for p in pages) == 2


def test_daily_pnl_sums(conn, monkeypatch):
    from trading_bot.core import trade_store
    monkeypatch.setattr(bg.time, "time", lambda: 1700000400.0)
    monkeypatch.setattr(trade_store, "daily_pnl_start_ms", lambda: 1700000000000)
    conn.http.routes[("GET", "/api/v2/mix/account/bill")] = {"code": "00000", "data": {"bills": BILLS, "endId": "0"}}
    daily = conn.get_daily_pnl_from_binance()
    assert daily["realized_pnl"] == 0.35 and daily["funding_fee"] == -0.02 and daily["commission"] == pytest.approx(-0.02)
    assert daily["total"] == pytest.approx(0.31) and daily["income_count"] == 4
    # o dashboard decompõe o total nessas três parcelas: têm que fechar com ele
    assert daily["realized_pnl"] + daily["funding_fee"] + daily["commission"] == pytest.approx(daily["total"])


def test_income_history_fail_open_or_raise(conn):
    assert conn.get_income_history() == []
    with pytest.raises(bg.BitgetApiError):
        conn.get_income_history(raise_errors=True)


def test_account_trades_map_fills(conn):
    conn.http.routes[("GET", "/api/v2/mix/order/fills")] = {"code": "00000", "data": {"endId": "t1", "fillList": [
        {"tradeId": "t2", "orderId": "o2", "price": "0.51", "baseVolume": "30", "profit": "0.3", "totalFee": "-0.01",
         "side": "buy", "tradeSide": "close", "tradeScope": "taker", "cTime": "1700000200000", "feeCoin": "USDT"},
        {"tradeId": "t1", "orderId": "o1", "price": "0.50", "baseVolume": "30", "profit": "0", "totalFee": "-0.01",
         "side": "buy", "tradeSide": "open", "tradeScope": "maker", "cTime": "1700000100000", "feeCoin": "USDT"},
    ]}}
    rows = conn.get_account_trades("ADAUSDT", 1700000000000, 1700000300000)
    assert [r["id"] for r in rows] == ["t1", "t2"]
    assert rows[0]["side"] == "BUY" and rows[0]["positionSide"] == "LONG" and rows[0]["maker"] is True
    assert rows[1] == {"id": "t2", "orderId": "o2", "symbol": "ADAUSDT", "time": 1700000200000, "price": "0.51", "qty": "30",
                       "realizedPnl": "0.3", "commission": "0.01", "commissionAsset": "USDT", "side": "SELL",
                       "positionSide": "LONG", "maker": False}


def test_commission_and_funding_rate(conn):
    assert conn.get_commission_rates("ADAUSDT")["taker_rate"] == 0.0006
    conn.http.routes[("GET", "/api/v2/mix/market/current-fund-rate")] = {"code": "00000", "data": [
        {"fundingRate": "0.0001", "nextUpdate": "1700000000000"}]}
    fr = conn.get_funding_rate("ADAUSDT")
    assert fr["long_pays"] and fr["favorable_side"] == "SHORT" and fr["next_funding_time"].year == 2023


def test_factory_picks_bitget_by_env(monkeypatch):
    from trading_bot.infra import exchange as factory
    monkeypatch.setattr(config, "EXCHANGE", "bitget", raising=False)
    monkeypatch.setattr(bg.BitgetConnection, "__init__", lambda self: None)
    assert isinstance(factory.create_exchange(), bg.BitgetConnection)


def test_config_validate_requires_bitget_credentials(monkeypatch, caplog):
    monkeypatch.setattr(config, "EXCHANGE", "bitget", raising=False)
    for k in ("BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_PASSPHRASE"):
        monkeypatch.setattr(config, k, "", raising=False)
    config.validate()
    assert "BITGET_API_KEY" in caplog.text


def test_contracts_load_even_when_host_uptime_is_below_ttl(monkeypatch):
    """Host recém-ligado: monotonic() < TTL. Sentinela 0.0 fazia o cache vazio parecer fresco (CI falhava)."""
    monkeypatch.setattr(bg.time, "monotonic", lambda: 10.0)
    conn = _make(monkeypatch)
    assert {s["symbol"] for s in conn.get_exchange_info()["symbols"]} == {"ADAUSDT", "BNBUSDT"}

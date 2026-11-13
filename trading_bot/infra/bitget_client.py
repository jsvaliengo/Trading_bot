"""
CONEXÃO COM A BITGET (futuros USDT-M, API v2 "classic")
=======================================================
Adaptador com os MESMOS nomes de método e formatos de retorno da BinanceConnection
(subconjunto do caminho martingale + dashboard + telegram; ver docs/EXCHANGE_PORT.md).

Símbolo interno `ADAUSDT` É o símbolo da Bitget (sem conversão), inclusive no demo:
com o header `paptrading: 1` a Bitget usa `USDT-FUTURES`/`USDT` e os símbolos normais
(verificado ao vivo em 11/11/26: `SUSDT-FUTURES`/`SBTCSUSDT` respondem 40034/40778 e
o header zera os endpoints públicos). Quantidade em MOEDAS (passo `sizeMultiplier`), sem
contratos. `requests` puro + HMAC-SHA256 base64 (sem dependência nova).
"""

import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import requests

from ..core.config import config

logger = logging.getLogger(__name__)

BASE_URL = "https://api.bitget.com"
TIMEOUT_S = 10.0
OK = "00000"
# Binance → Bitget. `1D` abre em UTC+8; `1Dutc` é o dia UTC como na Binance.
BAR_MAP = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H", "4h": "4H",
           "6h": "6Hutc", "12h": "12Hutc", "1d": "1Dutc", "1w": "1Wutc"}
INTERVAL_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
               "1h": 3_600_000, "4h": 14_400_000, "6h": 21_600_000, "12h": 43_200_000,
               "1d": 86_400_000, "1w": 604_800_000}
CANDLES_MAX = 1000
PAGE = 100
BILL_WINDOW_MS = 30 * 86_400_000  # /account/bill: startTime..endTime ≤ 30 dias; histórico total 90 dias
CONTRACTS_TTL_S = 21_600.0
FUNDING_TYPE = "contract_settle_fee"


class BitgetApiError(Exception):
    def __init__(self, code: str, msg: str, path: str):
        super().__init__(f"Bitget {path} code={code}: {msg}")
        self.code = code


def floor_qty(qty: float, contract: Dict[str, Any]) -> Decimal:
    """Moedas arredondadas PARA BAIXO no sizeMultiplier. Abaixo do minTradeNum devolve 0."""
    step, min_qty = Decimal(contract["sizeMultiplier"]), Decimal(contract["minTradeNum"])
    # +1e-9: 0.03 / 0.01 dá 2.9999… em binário e cairia um passo
    q = ((Decimal(str(qty)) / step) + Decimal("1e-9")).to_integral_value(ROUND_FLOOR) * step
    return q.quantize(step) if q >= min_qty else Decimal(0)


def _decimals(value: Decimal) -> int:
    return max(0, -value.normalize().as_tuple().exponent)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _is_close(side: str, position_side: str) -> bool:
    return (side, position_side) in (("SELL", "LONG"), ("BUY", "SHORT"))


class BitgetConnection:
    def __init__(self):
        self.config = config
        self.api_key = str(getattr(config, "BITGET_API_KEY", "") or "")
        self.api_secret = str(getattr(config, "BITGET_API_SECRET", "") or "")
        self.passphrase = str(getattr(config, "BITGET_PASSPHRASE", "") or "")
        self.demo = bool(getattr(config, "BITGET_DEMO", True))
        self.product_type = "USDT-FUTURES"
        self.margin_coin = "USDT"
        self.session = requests.Session()
        self._lock = threading.Lock()
        self._contracts: Dict[str, Dict[str, Any]] = {}
        self._contracts_ts = float("-inf")  # 0.0 parece "fresco" se o host subiu há < TTL
        self._realized_close_buffer: Dict[str, List[Dict[str, float]]] = {}
        self._daily_pnl_cache: Optional[Dict[str, Any]] = None
        self._cumulative_cache: Optional[Dict[str, Any]] = None
        logger.warning("🧪 Bitget em DEMO trading (header paptrading)" if self.demo
                       else "💰 ATENÇÃO: Bitget em PRODUÇÃO (dinheiro REAL)!")
        self._test_connection()

    # ---- símbolos ----
    def to_bitget(self, symbol: str) -> str:
        return str(symbol).upper()

    from_bitget = to_bitget

    # ---- HTTP ----
    def _sign(self, ts: str, method: str, path_query: str, body: str) -> str:
        digest = hmac.new(self.api_secret.encode(), f"{ts}{method}{path_query}{body}".encode(), hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
                 body: Optional[Any] = None, auth: bool = True) -> Any:
        query = f"?{urlencode(params)}" if params else ""
        raw_body = json.dumps(body, separators=(",", ":")) if body is not None else ""
        headers = {"Content-Type": "application/json", "locale": "en-US"}
        if auth:
            ts = str(int(time.time() * 1000))
            headers.update({"ACCESS-KEY": self.api_key, "ACCESS-PASSPHRASE": self.passphrase,
                            "ACCESS-TIMESTAMP": ts, "ACCESS-SIGN": self._sign(ts, method, path + query, raw_body)})
            if self.demo:  # só em chamadas privadas: nos endpoints públicos o header devolve vazio/40034
                headers["paptrading"] = "1"
        attempts = 2 if method == "GET" else 1  # POST não repete: ordem pode ter entrado
        for attempt in range(1, attempts + 1):
            try:
                resp = self.session.request(method, BASE_URL + path + query, data=raw_body or None,
                                            headers=headers, timeout=TIMEOUT_S)
                payload = resp.json()
                break
            except (requests.RequestException, ValueError) as exc:
                if attempt >= attempts:
                    raise
                logger.warning(f"⚠️ Bitget {path} falhou ({exc}); tentando de novo")
                time.sleep(0.5)
        if str(payload.get("code")) != OK:
            raise BitgetApiError(str(payload.get("code")), str(payload.get("msg")), path)
        return payload.get("data")

    def _test_connection(self) -> bool:
        try:
            wallet = self._fetch_account()["wallet"]
            logger.info(f"✅ Conexão Bitget OK! Saldo da carteira: ${wallet:.2f} {self.margin_coin}")
            return True
        except Exception as e:
            logger.error(f"❌ Erro de conexão: {e}")
            raise ConnectionError(f"Não foi possível conectar à Bitget: {e}")

    # ---- contratos / símbolos ----
    def _contract(self, symbol: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            if time.monotonic() - self._contracts_ts > CONTRACTS_TTL_S:
                data = self._request("GET", "/api/v2/mix/market/contracts", {"productType": self.product_type}, auth=False)
                self._contracts = {c["symbol"]: c for c in data or [] if c.get("symbolType") == "perpetual"}
                self._contracts_ts = time.monotonic()
            return self._contracts.get(self.to_bitget(symbol))

    def get_symbol_info(self, symbol: str) -> Dict:
        try:
            c = self._contract(symbol)
            if not c:
                return {}
            tick = Decimal(c["priceEndStep"]) * (Decimal(10) ** -int(c["pricePlace"]))
            return {
                "symbol": symbol,
                "pricePrecision": int(c["pricePlace"]),
                "quantityPrecision": int(c.get("volumePlace") or _decimals(Decimal(c["sizeMultiplier"]))),
                "minQty": float(c["minTradeNum"]),
                "minNotional": _f(c.get("minTradeUSDT"), 5.0),
                "stepSize": float(c["sizeMultiplier"]),
                "tickSize": float(tick),
                "maxLeverage": int(_f(c.get("maxLever"), 0)),
            }
        except Exception as e:
            logger.error(f"Erro ao obter info de {symbol}: {e}")
            return {}

    def get_exchange_info(self) -> Dict:
        try:
            self._contract("BTCUSDT")
            symbols = []
            for c in self._contracts.values():
                symbols.append({
                    "symbol": self.from_bitget(c["symbol"]),
                    "status": "TRADING" if c.get("symbolStatus") == "normal" else str(c.get("symbolStatus", "")).upper(),
                    "contractType": "PERPETUAL",
                    "baseAsset": self.from_bitget(c["symbol"])[:-4],
                    "quoteAsset": "USDT",
                    "pricePrecision": int(c["pricePlace"]),
                    "quantityPrecision": int(c.get("volumePlace") or 0),
                    "filters": [
                        {"filterType": "LOT_SIZE", "minQty": c["minTradeNum"], "stepSize": c["sizeMultiplier"]},
                        {"filterType": "MIN_NOTIONAL", "notional": c.get("minTradeUSDT", "5")},
                    ],
                })
            return {"symbols": symbols}
        except Exception as e:
            logger.error(f"Erro ao obter exchange info: {e}")
            return {}

    # ---- mercado ----
    def _mkt(self, symbol: str) -> Dict[str, str]:
        return {"symbol": self.to_bitget(symbol), "productType": self.product_type}

    def get_symbol_ticker(self, symbol: str) -> Dict:
        try:
            data = self._request("GET", "/api/v2/mix/market/ticker", self._mkt(symbol), auth=False)
            return {"price": data[0]["lastPr"], "time": int(data[0]["ts"])} if data else {}
        except Exception as e:
            logger.error(f"Erro ao obter preço de {symbol}: {e}")
            return {}

    def get_symbol_price(self, symbol: str) -> float:
        return _f(self.get_symbol_ticker(symbol).get("price"))

    get_current_price = get_symbol_price

    def get_klines_raw(self, symbol: str, interval: str, limit: int = 50) -> List[List]:
        """Lista de listas Binance-like, ascendente, última = vela aberta."""
        try:
            step_ms = INTERVAL_MS[interval]
            # shortcut: 1 página de até 1000 velas; para mais, paginar history-candles por endTime
            params = {**self._mkt(symbol), "granularity": BAR_MAP[interval], "limit": min(CANDLES_MAX, limit)}
            rows = self._request("GET", "/api/v2/mix/market/candles", params, auth=False) or []
            rows.sort(key=lambda k: int(k[0]))
            return [[int(k[0]), k[1], k[2], k[3], k[4], k[5], int(k[0]) + step_ms - 1, k[6], 0, "0", "0", "0"]
                    for k in rows]
        except Exception as e:
            if isinstance(e, BitgetApiError) and e.code == "40034":  # símbolo não existe (ex.: 1000PEPEUSDT): sem log
                return []
            logger.error(f"Erro ao obter klines raw de {symbol}: {e}")
            return []

    def get_klines(self, symbol: str, interval: str, limit: int = 50) -> List[Dict]:
        """Só velas fechadas (closeTime no passado), como o caminho REST da Binance."""
        now_ms = int(time.time() * 1000)
        return [
            {"timestamp": k[0], "open": float(k[1]), "high": float(k[2]), "low": float(k[3]),
             "close": float(k[4]), "volume": float(k[5])}
            for k in self.get_klines_raw(symbol, interval, limit + 1) if k[6] < now_ms
        ][-limit:]

    def get_funding_rate(self, symbol: str, force_refresh: bool = False) -> dict:
        try:
            data = self._request("GET", "/api/v2/mix/market/current-fund-rate", self._mkt(symbol), auth=False)
            rate = _f(data[0].get("fundingRate")) if data else 0.0
            nxt = int(data[0].get("nextUpdate") or 0) if data else 0
            return {"rate": rate, "rate_percent": rate * 100, "long_pays": rate > 0, "short_pays": rate < 0,
                    "next_funding_time": datetime.fromtimestamp(nxt / 1000, tz=timezone.utc) if nxt else None,
                    "favorable_side": "SHORT" if rate > 0 else "LONG" if rate < 0 else "NEUTRAL"}
        except Exception as e:
            logger.error(f"Erro ao obter funding rate de {symbol}: {e}")
            return {"rate": 0.0, "rate_percent": 0.0, "long_pays": False, "short_pays": False,
                    "next_funding_time": None, "favorable_side": "NEUTRAL"}

    def get_commission_rates(self, symbol: str = "BTCUSDT") -> Dict:
        try:
            c = self._contract(symbol) or {}
            maker, taker = abs(_f(c.get("makerFeeRate"), 0.0002)), abs(_f(c.get("takerFeeRate"), 0.0006))
            logger.info(f"📊 Taxas da Bitget ({symbol}): maker {maker * 100:.4f}% | taker {taker * 100:.4f}%")
            return {"maker_rate": maker, "taker_rate": taker, "maker_percent": maker * 100, "taker_percent": taker * 100}
        except Exception as e:
            logger.warning(f"⚠️ Erro ao obter taxas da API: {e} — usando Maker 0.02%, Taker 0.06%")
            return {"maker_rate": 0.0002, "taker_rate": 0.0006, "maker_percent": 0.02, "taker_percent": 0.06}

    # ---- conta ----
    def _fetch_account(self) -> Dict[str, float]:
        data = self._request("GET", "/api/v2/mix/account/accounts", {"productType": self.product_type}) or []
        acct = next((a for a in data if a.get("marginCoin") == self.margin_coin), {})
        equity, upl = _f(acct.get("accountEquity")), _f(acct.get("unrealizedPL"))
        wallet, available = equity - upl, _f(acct.get("available"))
        cap = _f(getattr(self.config, "SIMULATED_BALANCE_USD", 0.0))
        if cap > 0 and bool(getattr(self.config, "USE_TESTNET", False)):  # nunca capa produção
            wallet, available = cap, max(0.0, cap - max(0.0, wallet - available))
        return {"wallet": wallet, "available": available, "unrealized": upl, "margin_balance": equity,
                "initial_margin": _f(acct.get("crossedMargin") or acct.get("locked"))}

    def get_account_balance(self, force_refresh: bool = False) -> float:
        try:
            return self._fetch_account()["wallet"]
        except Exception as e:
            logger.error(f"Erro ao obter account da Bitget: {e}")
            return 0.0

    def get_available_balance(self, force_refresh: bool = False) -> float:
        try:
            return self._fetch_account()["available"]
        except Exception as e:
            logger.error(f"Erro ao obter account da Bitget: {e}")
            return 0.0

    def get_account_info(self) -> dict:
        try:
            a = self._fetch_account()
            return {"wallet_balance": a["wallet"], "available_balance": a["available"],
                    "unrealized_pnl": a["unrealized"], "margin_balance": a["margin_balance"],
                    "total_initial_margin": a["initial_margin"]}
        except Exception as e:
            logger.error(f"Erro ao obter info da conta: {e}")
            return {"wallet_balance": 0, "available_balance": 0, "unrealized_pnl": 0,
                    "margin_balance": 0, "total_initial_margin": 0}

    def invalidate_balance_cache(self) -> None:
        pass

    def invalidate_positions_cache(self) -> None:
        pass

    def set_hedge_mode(self) -> bool:
        """Idempotente na Bitget (já em hedge → sucesso); falha com posição aberta, como na Binance."""
        try:
            self._request("POST", "/api/v2/mix/account/set-position-mode",
                          body={"productType": self.product_type, "posMode": "hedge_mode"})
            logger.info("✅ Hedge Mode ativo")
            return True
        except Exception as e:
            logger.error(f"Erro ao ativar Hedge Mode: {e}")
            return False

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        base = {"symbol": self.to_bitget(symbol), "productType": self.product_type, "marginCoin": self.margin_coin}
        try:
            # place-order exige marginMode=crossed igual ao da conta do símbolo; só muda sem posição/ordem
            self._request("POST", "/api/v2/mix/account/set-margin-mode", body={**base, "marginMode": "crossed"})
        except Exception as e:
            logger.warning(f"⚠️ {symbol}: não mudou para cross ({e}); seguindo com o modo atual")
        try:
            self._request("POST", "/api/v2/mix/account/set-leverage", body={**base, "leverage": str(int(leverage))})
            logger.info(f"⚙️  Alavancagem {symbol}: {leverage}x")
            return True
        except Exception as e:
            logger.error(f"Erro ao definir alavancagem: {e}")
            return False

    # ---- posições / ordens ----
    def get_open_positions(self, force_refresh: bool = False) -> List[Dict]:
        """Propaga erro de API (como a Binance): [] significa 'sem posição', nunca 'API caiu'."""
        out = []
        data = self._request("GET", "/api/v2/mix/position/all-position",
                             {"productType": self.product_type, "marginCoin": self.margin_coin}) or []
        for p in data:
            qty = _f(p.get("total"))
            if qty == 0:
                continue
            out.append({"symbol": self.from_bitget(p["symbol"]), "side": str(p.get("holdSide", "long")).upper(),
                        "quantity": qty, "entry_price": _f(p.get("openPriceAvg")),
                        "mark_price": _f(p.get("markPrice")), "unrealized_pnl": _f(p.get("unrealizedPL")),
                        "leverage": int(_f(p.get("leverage"), self.config.LEVERAGE))})
        return out

    def place_market_order(self, symbol: str, side: str, position_side: str, quantity: float) -> Optional[Dict]:
        """side BUY/SELL + position_side LONG/SHORT (hedge). Devolve dict Binance-like
        (orderId, avgPrice, executedQty) ou None se falhou."""
        try:
            c = self._contract(symbol)
            if not c:
                logger.error(f"❌ Contrato Bitget não encontrado para {symbol}")
                return None
            qty = floor_qty(quantity, c)
            if qty <= 0:
                if _f(quantity) <= 0:
                    return None
                # shortcut: o caller já garantiu notional ≥ minNotional no preço dele; o floor só
                # cai abaixo do minTradeNum por deriva de preço. Sobe ao mínimo em vez de falhar todo tick.
                qty = Decimal(c["minTradeNum"])
                logger.warning(f"⚠️ {symbol}: {quantity} moedas < minTradeNum — ordem no mínimo ({qty})")
            closing = _is_close(side, position_side)
            # hedge na Bitget: `side` é a DIREÇÃO DA POSIÇÃO (buy=long, sell=short); abrir/fechar vai em tradeSide
            body = {"symbol": c["symbol"], "productType": self.product_type, "marginMode": "crossed",
                    "marginCoin": self.margin_coin, "size": str(qty), "side": "buy" if position_side == "LONG" else "sell",
                    "tradeSide": "close" if closing else "open", "orderType": "market"}
            data = self._request("POST", "/api/v2/mix/order/place-order", body=body) or {}
            if not data.get("orderId"):
                raise BitgetApiError("?", "sem orderId na resposta", "/api/v2/mix/order/place-order")
            order = self._fetch_fill(c["symbol"], data["orderId"])
            order.update({"symbol": symbol, "side": side, "positionSide": position_side})
            logger.info(f"📈 Ordem executada: {side} {position_side} {order['executedQty']} {symbol}")
            if closing and _f(order.get("executedQty")) > 0:
                with self._lock:
                    self._realized_close_buffer.setdefault(f"{symbol}_{position_side}", []).append({
                        "gross": _f(order.get("pnl")), "price": _f(order.get("avgPrice")),
                        "qty": _f(order.get("executedQty")), "ts_ms": int(time.time() * 1000)})
            return order
        except Exception as e:
            logger.error(f"Erro ao enviar ordem: {e}")
            return None

    def _fetch_fill(self, bitget_symbol: str, order_id: str) -> Dict[str, Any]:
        """Consulta a ordem até ter priceAvg (market enche em ms; 3 tentativas). Nunca levanta:
        a ordem JÁ entrou e o caller precisa do orderId."""
        order: Dict[str, Any] = {"orderId": order_id, "avgPrice": "0", "executedQty": "0", "status": "NEW"}
        for attempt in range(3):
            if attempt:
                time.sleep(0.3)
            try:
                o = self._request("GET", "/api/v2/mix/order/detail",
                                  {"symbol": bitget_symbol, "productType": self.product_type, "orderId": order_id})
            except Exception as e:
                logger.warning(f"⚠️ Falha ao consultar fill da ordem {order_id}: {e}")
                continue
            if o and _f(o.get("priceAvg")) > 0:
                return {"orderId": order_id, "avgPrice": o["priceAvg"], "executedQty": str(_f(o.get("baseVolume"))),
                        "status": "FILLED" if o.get("state") == "filled" else str(o.get("state", "")).upper(),
                        "pnl": o.get("totalProfits") or "0", "fee": o.get("fee") or "0"}
        logger.warning(f"⚠️ Ordem {order_id} sem fill confirmado (priceAvg ausente)")
        return order

    def close_position(self, symbol: str, position_side: str) -> Optional[Dict]:
        try:
            for pos in self.get_open_positions(force_refresh=True):
                if pos["symbol"] == symbol and pos["side"] == position_side:
                    order = self.place_market_order(symbol=symbol, side="SELL" if position_side == "LONG" else "BUY",
                                                    position_side=position_side, quantity=pos["quantity"])
                    if order is None:
                        logger.error(f"❌ Ordem de fechamento falhou: {position_side} {symbol}. Posição segue aberta.")
                        return None
                    logger.info(f"✅ Posição fechada: {position_side} {symbol}")
                    return order
            logger.warning(f"Posição não encontrada: {position_side} {symbol}")
            return None
        except Exception as e:
            logger.error(f"Erro ao fechar posição: {e}")
            return None

    def pop_realized_close(self, symbol: str, side: str, since_ms: Optional[int] = None) -> Optional[Dict[str, float]]:
        """Mesma semântica da Binance; alimentado pelo fill da própria ordem de fechamento (sem
        WebSocket). Fechamentos externos caem no income REST (get_income_history)."""
        with self._lock:
            fills = self._realized_close_buffer.pop(f"{symbol}_{side}", None) or []
        if since_ms is not None:
            fills = [f for f in fills if f["ts_ms"] >= since_ms]
        qty = sum(f["qty"] for f in fills)
        if qty <= 0:
            return None
        return {"gross": sum(f["gross"] for f in fills),
                "exit_price": sum(f["price"] * f["qty"] for f in fills) / qty, "qty": qty}

    def cancel_open_orders(self, symbol: str) -> bool:
        try:
            sym = self.to_bitget(symbol)
            data = self._request("GET", "/api/v2/mix/order/orders-pending",
                                 {"symbol": sym, "productType": self.product_type}) or {}
            pending = data.get("entrustedList") or []
            if pending:
                self._request("POST", "/api/v2/mix/order/batch-cancel-orders",
                              body={"symbol": sym, "productType": self.product_type, "marginCoin": self.margin_coin,
                                    "orderIdList": [{"orderId": o["orderId"]} for o in pending]})
            return True
        except Exception as e:
            logger.warning(f"⚠️ Falha ao cancelar ordens abertas de {symbol}: {e}")
            return False

    # ---- income / P&L (bills) ----
    def _bills(self, start_ms: Optional[int], limit: int) -> List[Dict[str, Any]]:
        """Bills de start_ms até agora em janelas de 30 dias (limite da API), paginadas por
        idLessThan=endId, devolvidos em ordem ASCENDENTE de cTime."""
        now_ms = int(time.time() * 1000)
        windows = [(None, None)] if start_ms is None else [
            (s, min(s + BILL_WINDOW_MS, now_ms)) for s in range(int(start_ms), now_ms, BILL_WINDOW_MS)]
        rows: List[Dict[str, Any]] = []
        for w_start, w_end in windows:
            params: Dict[str, Any] = {"productType": self.product_type, "limit": PAGE}
            if w_start:
                params.update({"startTime": w_start, "endTime": w_end})
            while len(rows) < limit:
                data = self._request("GET", "/api/v2/mix/account/bill", params) or {}
                page = data.get("bills") or []
                rows.extend(page)
                if len(page) < PAGE or not data.get("endId"):
                    break
                params["idLessThan"] = data["endId"]
        rows.sort(key=lambda r: int(r.get("cTime") or 0))
        return rows[-limit:] if start_ms is None else rows[:limit]

    def get_income_history(self, income_type: str = None, limit: int = 100, symbol: str = None,
                           start_time: int = None, raise_errors: bool = False) -> list:
        """Lista Binance-like {incomeType, income, symbol, time, asset}, ascendente.
        Bill de trade (open_*/close_*) vira COMMISSION (fee) e, nos close_*, REALIZED_PNL (amount);
        contract_settle_fee vira FUNDING_FEE (amount)."""
        try:
            out = []
            for b in self._bills(start_time, limit):
                sym = self.from_bitget(b.get("symbol") or "")
                if symbol and sym != symbol:
                    continue
                base = {"symbol": sym, "time": int(b.get("cTime") or 0), "asset": "USDT", "tranId": b.get("billId", "")}
                bt = str(b.get("businessType") or "")
                items = []
                if bt == FUNDING_TYPE:
                    items.append(("FUNDING_FEE", b.get("amount") or "0"))
                elif "open" in bt or "close" in bt:
                    if "close" in bt and _f(b.get("amount")) != 0:
                        items.append(("REALIZED_PNL", b.get("amount")))
                    if _f(b.get("fee")) != 0:
                        items.append(("COMMISSION", b.get("fee")))
                out.extend({**base, "incomeType": t, "income": str(v)} for t, v in items
                           if not income_type or t == income_type)
            return out[:limit]
        except Exception as e:
            if raise_errors:
                raise
            logger.error(f"Erro ao obter histórico de income: {e}")
            return []

    def _sum_income(self, start_ms: int, limit: int) -> Dict[str, Any]:
        rows = self.get_income_history(limit=limit, start_time=start_ms, raise_errors=True)
        sums = {"REALIZED_PNL": 0.0, "FUNDING_FEE": 0.0, "COMMISSION": 0.0}
        for r in rows:
            sums[r["incomeType"]] = sums.get(r["incomeType"], 0.0) + _f(r["income"])
        return {"realized_pnl": sums["REALIZED_PNL"], "funding_fee": sums["FUNDING_FEE"],
                "commission": sums["COMMISSION"], "income_count": len(rows),
                "income_types": sorted({r["incomeType"] for r in rows})}

    def get_daily_pnl_from_binance(self, force_refresh: bool = False) -> dict:
        """Nome mantido pelos callers (dashboard/telegram/RiskManager): P&L de HOJE na Bitget."""
        with self._lock:
            c = self._daily_pnl_cache
            if not force_refresh and c and time.monotonic() - c["ts"] < 30.0:
                return c["data"]
        try:
            from ..core.trade_store import daily_pnl_start_ms
            s = self._sum_income(daily_pnl_start_ms(), 1000)
            result = {**s, "total": s["realized_pnl"] + s["funding_fee"] + s["commission"]}
            with self._lock:
                self._daily_pnl_cache = {"data": result, "ts": time.monotonic()}
            return result
        except Exception as e:
            logger.error(f"Erro ao obter P&L diário da Bitget: {e}")
            return {"realized_pnl": 0.0, "funding_fee": 0.0, "commission": 0.0, "total": 0.0,
                    "income_count": 0, "income_types": []}

    def get_cumulative_income_from_binance(self, force_refresh: bool = False, lookback_days: int = 200,
                                          start_ms: int = None) -> dict:
        with self._lock:
            c = self._cumulative_cache
            if not force_refresh and c and time.monotonic() - c["ts"] < 300.0 and c.get("start_ms") == start_ms:
                return c["data"]
        try:
            # shortcut: bills cobrem 90 dias; antes disso o acumulado fica truncado
            start = start_ms if start_ms is not None else int((time.time() - min(lookback_days, 90) * 86400) * 1000)
            result = self._sum_income(start, 5000)
            with self._lock:
                self._cumulative_cache = {"data": result, "ts": time.monotonic(), "start_ms": start_ms}
            return result
        except Exception as e:
            logger.error(f"Erro ao obter income acumulado da Bitget: {e}")
            return {"funding_fee": 0.0, "commission": 0.0, "realized_pnl": 0.0, "income_count": 0}

    def get_account_trades(self, symbol: str, start_time: int, end_time: int, limit: int = 1000) -> list:
        """Fills Binance-like (userTrades) do símbolo na janela, ascendente. Propaga erro."""
        params: Dict[str, Any] = {**self._mkt(symbol), "startTime": int(start_time), "endTime": int(end_time),
                                  "limit": PAGE}
        rows: List[Dict[str, Any]] = []
        while len(rows) < limit:
            data = self._request("GET", "/api/v2/mix/order/fills", params) or {}
            page = data.get("fillList") or []
            rows.extend(page)
            if len(page) < PAGE or not data.get("endId"):
                break
            params["idLessThan"] = data["endId"]
        rows.sort(key=lambda r: int(r.get("cTime") or 0))
        out = []
        for r in rows[:limit]:
            # hedge: side = direção da posição; tradeSide open/close diz se comprou ou vendeu de fato
            pos_side = "LONG" if str(r.get("side", "")).lower() == "buy" else "SHORT"
            closing = "close" in str(r.get("tradeSide", ""))
            out.append({"id": r.get("tradeId"), "orderId": r.get("orderId"), "symbol": symbol,
                        "time": int(r.get("cTime") or 0), "price": r.get("price"), "qty": r.get("baseVolume"),
                        "realizedPnl": r.get("profit") or "0", "commission": str(abs(_f(r.get("totalFee")))),
                        "commissionAsset": r.get("feeCoin", "USDT"),
                        "side": ("SELL" if closing else "BUY") if pos_side == "LONG" else ("BUY" if closing else "SELL"),
                        "positionSide": pos_side, "maker": r.get("tradeScope") == "maker"})
        return out

    # ---- stubs do que o caminho martingale chama sem hasattr ----
    def get_symbol_cooldown_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        return None

    def shutdown(self) -> None:
        self.session.close()

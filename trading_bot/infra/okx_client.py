"""
CONEXÃO COM A OKX (perpétuos USDT-M, API v5)
============================================
Adaptador com os MESMOS nomes de método e formatos de retorno da BinanceConnection
(subconjunto do caminho martingale + dashboard + telegram; ver docs/EXCHANGE_PORT.md).

Símbolos entram e saem no formato interno do bot (`ADAUSDT`); a tradução para
`ADA-USDT-SWAP` e de moedas para contratos (`ctVal`/`lotSz`) fica só aqui.
`requests` puro + assinatura HMAC-SHA256 base64 (sem dependência nova).
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

BASE_URL = "https://www.okx.com"
TIMEOUT_S = 10.0
# Binance → OKX. `1D` da OKX abre em UTC+8; `1Dutc` é o dia UTC como na Binance.
BAR_MAP = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H", "2h": "2H",
           "4h": "4H", "6h": "6Hutc", "12h": "12Hutc", "1d": "1Dutc", "1w": "1Wutc"}
INTERVAL_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
               "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
               "12h": 43_200_000, "1d": 86_400_000, "1w": 604_800_000}
CANDLES_PAGE = 300
BILLS_PAGE = 100
BILLS_RECENT_MS = 6 * 86_400_000  # /account/bills cobre 7 dias; além disso, bills-archive (3 meses)
INSTRUMENTS_TTL_S = 21_600.0
SIDES = {"LONG": "long", "SHORT": "short"}


class OkxApiError(Exception):
    def __init__(self, code: str, msg: str, path: str):
        super().__init__(f"OKX {path} code={code}: {msg}")
        self.code = code


def to_inst_id(symbol: str) -> str:
    """ADAUSDT → ADA-USDT-SWAP."""
    s = str(symbol).upper()
    if s.endswith("-SWAP"):
        return s
    if not s.endswith("USDT"):
        raise ValueError(f"símbolo sem sufixo USDT: {symbol}")
    return f"{s[:-4]}-USDT-SWAP"


def from_inst_id(inst_id: str) -> str:
    """ADA-USDT-SWAP → ADAUSDT."""
    return str(inst_id).upper().replace("-SWAP", "").replace("-", "")


def _decimals(value: Decimal) -> int:
    return max(0, -value.normalize().as_tuple().exponent)


def coins_to_contracts(qty: float, inst: Dict[str, Any]) -> Decimal:
    """Moedas → contratos, arredondando PARA BAIXO no lotSz. Abaixo do minSz devolve 0."""
    ct_val, lot, min_sz = Decimal(inst["ctVal"]), Decimal(inst["lotSz"]), Decimal(inst["minSz"])
    # +1e-9: 10 ADA / 100 / 0.1 dá 0.9999… em binário e cairia para 0 contratos
    contracts = ((Decimal(str(qty)) / ct_val / lot) + Decimal("1e-9")).to_integral_value(ROUND_FLOOR) * lot
    return contracts.quantize(lot) if contracts >= min_sz else Decimal(0)


def contracts_to_coins(contracts: Any, inst: Dict[str, Any]) -> float:
    return float(Decimal(str(contracts)) * Decimal(inst["ctVal"]))


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _is_close(side: str, position_side: str) -> bool:
    return (side, position_side) in (("SELL", "LONG"), ("BUY", "SHORT"))


class OkxConnection:
    def __init__(self):
        self.config = config
        self.api_key = str(getattr(config, "OKX_API_KEY", "") or "")
        self.api_secret = str(getattr(config, "OKX_API_SECRET", "") or "")
        self.passphrase = str(getattr(config, "OKX_PASSPHRASE", "") or "")
        self.demo = bool(getattr(config, "OKX_DEMO", True))
        self.session = requests.Session()
        self._lock = threading.Lock()
        self._instruments: Dict[str, Dict[str, Any]] = {}
        self._instruments_ts = float("-inf")  # 0.0 parece "fresco" se o host subiu há < TTL
        self._realized_close_buffer: Dict[str, List[Dict[str, float]]] = {}
        self._daily_pnl_cache: Optional[Dict[str, Any]] = None
        self._cumulative_cache: Optional[Dict[str, Any]] = None
        logger.warning("🧪 OKX em DEMO trading (x-simulated-trading)" if self.demo
                       else "💰 ATENÇÃO: OKX em PRODUÇÃO (dinheiro REAL)!")
        self._test_connection()

    # ---- HTTP ----
    def _sign(self, ts: str, method: str, path: str, body: str) -> str:
        digest = hmac.new(self.api_secret.encode(), f"{ts}{method}{path}{body}".encode(), hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def _request(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
                 body: Optional[Any] = None, auth: bool = True) -> List[Any]:
        query = f"?{urlencode(params)}" if params else ""
        raw_body = json.dumps(body, separators=(",", ":")) if body is not None else ""
        headers = {"Content-Type": "application/json"}
        if auth:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            headers.update({"OK-ACCESS-KEY": self.api_key, "OK-ACCESS-PASSPHRASE": self.passphrase,
                            "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-SIGN": self._sign(ts, method, path + query, raw_body)})
        if self.demo:
            headers["x-simulated-trading"] = "1"
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
                logger.warning(f"⚠️ OKX {path} falhou ({exc}); tentando de novo")
                time.sleep(0.5)
        if str(payload.get("code")) != "0":
            raise OkxApiError(str(payload.get("code")), str(payload.get("msg")), path)
        return payload.get("data") or []

    def _test_connection(self) -> bool:
        try:
            wallet = self._fetch_account()["wallet"]
            logger.info(f"✅ Conexão OKX OK! Saldo da carteira: ${wallet:.2f} USDT")
            return True
        except Exception as e:
            logger.error(f"❌ Erro de conexão: {e}")
            raise ConnectionError(f"Não foi possível conectar à OKX: {e}")

    # ---- instrumentos / símbolos ----
    def _instrument(self, symbol: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            if time.monotonic() - self._instruments_ts > INSTRUMENTS_TTL_S:
                data = self._request("GET", "/api/v5/public/instruments", {"instType": "SWAP"}, auth=False)
                self._instruments = {d["instId"]: d for d in data if str(d.get("instId", "")).endswith("-USDT-SWAP")}
                self._instruments_ts = time.monotonic()
            return self._instruments.get(to_inst_id(symbol))

    def get_symbol_info(self, symbol: str) -> Dict:
        try:
            inst = self._instrument(symbol)
            if not inst:
                return {}
            step = Decimal(inst["lotSz"]) * Decimal(inst["ctVal"])
            min_qty = Decimal(inst["minSz"]) * Decimal(inst["ctVal"])
            price = self.get_symbol_price(symbol)
            return {
                "symbol": symbol,
                "pricePrecision": _decimals(Decimal(inst["tickSz"])),
                "quantityPrecision": _decimals(step),
                "minQty": float(min_qty),
                "minNotional": float(min_qty) * price,
                "stepSize": float(step),
                "tickSize": float(inst["tickSz"]),
                "ctVal": float(inst["ctVal"]),
                "lotSz": float(inst["lotSz"]),
                "minSz": float(inst["minSz"]),
            }
        except Exception as e:
            logger.error(f"Erro ao obter info de {symbol}: {e}")
            return {}

    def get_exchange_info(self) -> Dict:
        try:
            self._instrument("BTCUSDT")
            symbols = []
            for inst in self._instruments.values():
                step = Decimal(inst["lotSz"]) * Decimal(inst["ctVal"])
                symbols.append({
                    "symbol": from_inst_id(inst["instId"]),
                    "status": "TRADING" if inst.get("state") == "live" else str(inst.get("state", "")).upper(),
                    "contractType": "PERPETUAL",
                    "baseAsset": str(inst.get("ctValCcy") or inst["instId"].split("-")[0]),
                    "quoteAsset": "USDT",
                    "pricePrecision": _decimals(Decimal(inst["tickSz"])),
                    "quantityPrecision": _decimals(step),
                    "filters": [
                        {"filterType": "LOT_SIZE", "minQty": str(Decimal(inst["minSz"]) * Decimal(inst["ctVal"])),
                         "stepSize": str(step)},
                        {"filterType": "PRICE_FILTER", "tickSize": inst["tickSz"]},
                    ],
                })
            return {"symbols": symbols}
        except Exception as e:
            logger.error(f"Erro ao obter exchange info: {e}")
            return {}

    # ---- mercado ----
    def get_symbol_ticker(self, symbol: str) -> Dict:
        try:
            data = self._request("GET", "/api/v5/market/ticker", {"instId": to_inst_id(symbol)}, auth=False)
            return {"price": data[0]["last"], "time": int(data[0]["ts"])} if data else {}
        except Exception as e:
            logger.error(f"Erro ao obter preço de {symbol}: {e}")
            return {}

    def get_symbol_price(self, symbol: str) -> float:
        return _f(self.get_symbol_ticker(symbol).get("price"))

    get_current_price = get_symbol_price

    def get_klines_raw(self, symbol: str, interval: str, limit: int = 50) -> List[List]:
        """Lista de listas Binance-like, ascendente, última = vela aberta."""
        try:
            bar, step_ms = BAR_MAP[interval], INTERVAL_MS[interval]
            inst_id, rows, after = to_inst_id(symbol), [], None
            while len(rows) < limit:
                params = {"instId": inst_id, "bar": bar, "limit": min(CANDLES_PAGE, limit - len(rows))}
                if after:
                    params["after"] = after
                page = self._request("GET", "/api/v5/market/candles", params, auth=False)
                rows.extend(page)
                if len(page) < params["limit"]:
                    break
                after = page[-1][0]
            return [[int(k[0]), k[1], k[2], k[3], k[4], k[6], int(k[0]) + step_ms - 1, k[7], 0, "0", "0", "0"]
                    for k in reversed(rows)]
        except Exception as e:
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
            data = self._request("GET", "/api/v5/public/funding-rate", {"instId": to_inst_id(symbol)}, auth=False)
            rate = _f(data[0].get("fundingRate")) if data else 0.0
            nxt = int(data[0].get("nextFundingTime") or 0) if data else 0
            return {"rate": rate, "rate_percent": rate * 100, "long_pays": rate > 0, "short_pays": rate < 0,
                    "next_funding_time": datetime.fromtimestamp(nxt / 1000, tz=timezone.utc) if nxt else None,
                    "favorable_side": "SHORT" if rate > 0 else "LONG" if rate < 0 else "NEUTRAL"}
        except Exception as e:
            logger.error(f"Erro ao obter funding rate de {symbol}: {e}")
            return {"rate": 0.0, "rate_percent": 0.0, "long_pays": False, "short_pays": False,
                    "next_funding_time": None, "favorable_side": "NEUTRAL"}

    def get_commission_rates(self, symbol: str = "BTCUSDT") -> Dict:
        try:
            data = self._request("GET", "/api/v5/account/trade-fee", {"instType": "SWAP"})
            maker, taker = abs(_f(data[0].get("maker"), 0.0002)), abs(_f(data[0].get("taker"), 0.0005))
            logger.info(f"📊 Taxas da OKX: maker {maker * 100:.4f}% | taker {taker * 100:.4f}%")
            return {"maker_rate": maker, "taker_rate": taker, "maker_percent": maker * 100, "taker_percent": taker * 100}
        except Exception as e:
            logger.warning(f"⚠️ Erro ao obter taxas da API: {e} — usando Maker 0.02%, Taker 0.05%")
            return {"maker_rate": 0.0002, "taker_rate": 0.0005, "maker_percent": 0.02, "taker_percent": 0.05}

    # ---- conta ----
    def _fetch_account(self) -> Dict[str, float]:
        data = self._request("GET", "/api/v5/account/balance", {"ccy": "USDT"})
        acct = data[0] if data else {}
        usdt = next((d for d in acct.get("details", []) if d.get("ccy") == "USDT"), {})
        wallet, available = _f(usdt.get("cashBal")), _f(usdt.get("availBal"))
        cap = _f(getattr(self.config, "SIMULATED_BALANCE_USD", 0.0))
        if cap > 0 and bool(getattr(self.config, "USE_TESTNET", False)):  # nunca capa produção
            wallet, available = cap, max(0.0, cap - max(0.0, wallet - available))
        return {"wallet": wallet, "available": available, "unrealized": _f(usdt.get("upl")),
                "margin_balance": _f(usdt.get("eq")), "initial_margin": _f(usdt.get("imr") or acct.get("imr"))}

    def get_account_balance(self, force_refresh: bool = False) -> float:
        try:
            return self._fetch_account()["wallet"]
        except Exception as e:
            logger.error(f"Erro ao obter account da OKX: {e}")
            return 0.0

    def get_available_balance(self, force_refresh: bool = False) -> float:
        try:
            return self._fetch_account()["available"]
        except Exception as e:
            logger.error(f"Erro ao obter account da OKX: {e}")
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
        try:
            cfg = self._request("GET", "/api/v5/account/config")
            if cfg and cfg[0].get("posMode") == "long_short_mode":
                logger.info("✅ Hedge Mode já está ativo")
                return True
            self._request("POST", "/api/v5/account/set-position-mode", body={"posMode": "long_short_mode"})
            logger.info("✅ Hedge Mode ATIVADO com sucesso!")
            return True
        except Exception as e:
            logger.error(f"Erro ao ativar Hedge Mode: {e}")
            return False

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            self._request("POST", "/api/v5/account/set-leverage",
                          body={"instId": to_inst_id(symbol), "lever": str(int(leverage)), "mgnMode": "cross"})
            logger.info(f"⚙️  Alavancagem {symbol}: {leverage}x")
            return True
        except Exception as e:
            logger.error(f"Erro ao definir alavancagem: {e}")
            return False

    # ---- posições / ordens ----
    def get_open_positions(self, force_refresh: bool = False) -> List[Dict]:
        """Propaga erro de API (como a Binance): [] significa 'sem posição', nunca 'API caiu'."""
        out = []
        for p in self._request("GET", "/api/v5/account/positions", {"instType": "SWAP"}):
            pos = _f(p.get("pos"))
            inst = self._instrument(from_inst_id(p.get("instId", "")))
            if pos == 0 or not inst:
                continue
            side = {"long": "LONG", "short": "SHORT"}.get(p.get("posSide"), "LONG" if pos > 0 else "SHORT")
            out.append({"symbol": from_inst_id(p["instId"]), "side": side,
                        "quantity": contracts_to_coins(abs(pos), inst), "entry_price": _f(p.get("avgPx")),
                        "mark_price": _f(p.get("markPx")), "unrealized_pnl": _f(p.get("upl")),
                        "leverage": int(_f(p.get("lever"), self.config.LEVERAGE))})
        return out

    def place_market_order(self, symbol: str, side: str, position_side: str, quantity: float) -> Optional[Dict]:
        """side BUY/SELL + position_side LONG/SHORT (hedge). Devolve dict Binance-like
        (orderId, avgPrice, executedQty) ou None se falhou."""
        try:
            inst = self._instrument(symbol)
            if not inst:
                logger.error(f"❌ Instrumento OKX não encontrado para {symbol}")
                return None
            contracts = coins_to_contracts(quantity, inst)
            if contracts <= 0:
                if _f(quantity) <= 0:
                    return None
                # shortcut: o caller já garantiu notional ≥ minNotional no preço dele; o floor só
                # cai abaixo do minSz por deriva de preço. Sobe ao mínimo em vez de falhar todo tick.
                contracts = Decimal(inst["minSz"])
                logger.warning(f"⚠️ {symbol}: {quantity} moedas < minSz — ordem no mínimo ({contracts} ct)")
            body = {"instId": inst["instId"], "tdMode": "cross", "side": side.lower(),
                    "posSide": SIDES[position_side], "ordType": "market", "sz": str(contracts)}
            data = self._request("POST", "/api/v5/trade/order", body=body)
            if not data or str(data[0].get("sCode")) != "0":
                raise OkxApiError(str(data[0].get("sCode")) if data else "?", str(data[0].get("sMsg")) if data else "sem resposta",
                                  "/api/v5/trade/order")
            order = self._fetch_fill(inst, data[0]["ordId"])
            order.update({"symbol": symbol, "side": side, "positionSide": position_side})
            logger.info(f"📈 Ordem executada: {side} {position_side} {order['executedQty']} {symbol} ({contracts} ct)")
            if _is_close(side, position_side) and _f(order.get("executedQty")) > 0:
                with self._lock:
                    self._realized_close_buffer.setdefault(f"{symbol}_{position_side}", []).append({
                        "gross": _f(order.get("pnl")), "price": _f(order.get("avgPrice")),
                        "qty": _f(order.get("executedQty")), "ts_ms": int(time.time() * 1000)})
            return order
        except Exception as e:
            logger.error(f"Erro ao enviar ordem: {e}")
            return None

    def _fetch_fill(self, inst: Dict[str, Any], ord_id: str) -> Dict[str, Any]:
        """Consulta a ordem até ter avgPx (market enche em ms; 3 tentativas). Nunca levanta:
        a ordem JÁ entrou e o caller precisa do orderId."""
        order: Dict[str, Any] = {"orderId": ord_id, "avgPrice": "0", "executedQty": "0", "status": "NEW"}
        for attempt in range(3):
            if attempt:
                time.sleep(0.3)
            try:
                data = self._request("GET", "/api/v5/trade/order", {"instId": inst["instId"], "ordId": ord_id})
            except Exception as e:
                logger.warning(f"⚠️ Falha ao consultar fill da ordem {ord_id}: {e}")
                continue
            if data and _f(data[0].get("avgPx")) > 0:
                o = data[0]
                return {"orderId": ord_id, "avgPrice": o["avgPx"],
                        "executedQty": str(contracts_to_coins(o.get("accFillSz") or 0, inst)),
                        "status": "FILLED" if o.get("state") == "filled" else str(o.get("state", "")).upper(),
                        "pnl": o.get("pnl", "0"), "fee": o.get("fee", "0")}
        logger.warning(f"⚠️ Ordem {ord_id} sem fill confirmado (avgPx ausente)")
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
        """Mesma semântica da Binance; fase 1 alimentado pelo fill da própria ordem de fechamento
        (sem WebSocket). Fechamentos externos caem no income REST (get_income_history)."""
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
            inst_id = to_inst_id(symbol)
            pending = self._request("GET", "/api/v5/trade/orders-pending", {"instType": "SWAP", "instId": inst_id})
            if pending:
                self._request("POST", "/api/v5/trade/cancel-batch-orders",
                              body=[{"instId": inst_id, "ordId": o["ordId"]} for o in pending])
            return True
        except Exception as e:
            logger.warning(f"⚠️ Falha ao cancelar ordens abertas de {symbol}: {e}")
            return False

    # ---- income / P&L (bills) ----
    def _bills(self, bill_type: Optional[str], inst_id: Optional[str], start_ms: Optional[int],
               end_ms: Optional[int], limit: int) -> List[Dict[str, Any]]:
        """Bills paginados por billId, devolvidos em ordem ASCENDENTE de ts."""
        now_ms = int(time.time() * 1000)
        recent = start_ms is None or start_ms >= now_ms - BILLS_RECENT_MS
        path = "/api/v5/account/bills" if recent else "/api/v5/account/bills-archive"
        params: Dict[str, Any] = {"instType": "SWAP", "limit": BILLS_PAGE}
        if bill_type:
            params["type"] = bill_type
        if inst_id:
            params["instId"] = inst_id
        if start_ms:
            params["begin"] = int(start_ms)
        if end_ms:
            params["end"] = int(end_ms)
        rows: List[Dict[str, Any]] = []
        while len(rows) < limit:
            page = self._request("GET", path, params)
            rows.extend(page)
            if len(page) < BILLS_PAGE:
                break
            params["after"] = page[-1]["billId"]
        rows.sort(key=lambda r: int(r.get("ts") or 0))
        return rows[-limit:] if start_ms is None else rows[:limit]

    def get_income_history(self, income_type: str = None, limit: int = 100, symbol: str = None,
                           start_time: int = None, raise_errors: bool = False) -> list:
        """Lista Binance-like {incomeType, income, symbol, time, asset}, ascendente.
        Bill de trade (type 2) vira REALIZED_PNL (pnl≠0) e COMMISSION (fee); type 8 vira FUNDING_FEE."""
        try:
            bill_type = {"FUNDING_FEE": "8", "REALIZED_PNL": "2", "COMMISSION": "2"}.get(income_type or "")
            out = []
            for b in self._bills(bill_type, to_inst_id(symbol) if symbol else None, start_time, None, limit):
                base = {"symbol": from_inst_id(b.get("instId", "")), "time": int(b.get("ts") or 0),
                        "asset": b.get("ccy", "USDT"), "tranId": b.get("billId", "")}
                items = []
                if b.get("type") == "8":
                    items.append(("FUNDING_FEE", b.get("balChg") or b.get("pnl") or "0"))
                elif b.get("type") == "2":
                    if _f(b.get("pnl")) != 0:
                        items.append(("REALIZED_PNL", b.get("pnl")))
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
        """Nome mantido pelos callers (dashboard/telegram/RiskManager): P&L de HOJE na OKX."""
        with self._lock:
            c = self._daily_pnl_cache
            if not force_refresh and c and time.monotonic() - c["ts"] < 30.0:
                return c["data"]
        try:
            from ..core.trade_store import trading_day_start
            s = self._sum_income(int(trading_day_start().timestamp() * 1000), 1000)
            result = {**s, "total": s["realized_pnl"] + s["funding_fee"] + s["commission"]}
            with self._lock:
                self._daily_pnl_cache = {"data": result, "ts": time.monotonic()}
            return result
        except Exception as e:
            logger.error(f"Erro ao obter P&L diário da OKX: {e}")
            return {"realized_pnl": 0.0, "funding_fee": 0.0, "commission": 0.0, "total": 0.0,
                    "income_count": 0, "income_types": []}

    def get_cumulative_income_from_binance(self, force_refresh: bool = False, lookback_days: int = 200,
                                          start_ms: int = None) -> dict:
        with self._lock:
            c = self._cumulative_cache
            if not force_refresh and c and time.monotonic() - c["ts"] < 300.0 and c.get("start_ms") == start_ms:
                return c["data"]
        try:
            # shortcut: bills-archive cobre 3 meses; antes disso o acumulado fica truncado
            start = start_ms if start_ms is not None else int((time.time() - min(lookback_days, 90) * 86400) * 1000)
            result = self._sum_income(start, 5000)
            with self._lock:
                self._cumulative_cache = {"data": result, "ts": time.monotonic(), "start_ms": start_ms}
            return result
        except Exception as e:
            logger.error(f"Erro ao obter income acumulado da OKX: {e}")
            return {"funding_fee": 0.0, "commission": 0.0, "realized_pnl": 0.0, "income_count": 0}

    def get_account_trades(self, symbol: str, start_time: int, end_time: int, limit: int = 1000) -> list:
        """Fills Binance-like (userTrades) do símbolo na janela, ascendente. Propaga erro."""
        inst = self._instrument(symbol) or {}
        params: Dict[str, Any] = {"instType": "SWAP", "instId": to_inst_id(symbol), "begin": int(start_time),
                                  "end": int(end_time), "limit": BILLS_PAGE}
        rows: List[Dict[str, Any]] = []
        while len(rows) < limit:
            page = self._request("GET", "/api/v5/trade/fills-history", params)
            rows.extend(page)
            if len(page) < BILLS_PAGE:
                break
            params["after"] = page[-1]["billId"]
        rows.sort(key=lambda r: int(r.get("ts") or 0))
        return [{"id": r.get("tradeId"), "orderId": r.get("ordId"), "symbol": symbol, "time": int(r.get("ts") or 0),
                 "price": r.get("fillPx"), "qty": str(contracts_to_coins(r.get("fillSz") or 0, inst)) if inst else r.get("fillSz"),
                 "realizedPnl": r.get("fillPnl", "0"), "commission": str(abs(_f(r.get("fee")))),
                 "commissionAsset": r.get("feeCcy", "USDT"), "side": str(r.get("side", "")).upper(),
                 "positionSide": str(r.get("posSide", "")).upper(), "maker": r.get("execType") == "M"}
                for r in rows[:limit]]

    # ---- stubs do que o caminho martingale chama sem hasattr ----
    def get_symbol_cooldown_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        return None

    def shutdown(self) -> None:
        self.session.close()

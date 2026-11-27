"""
Cotação dólar→real para o dashboard. Fontes públicas sem chave, tentadas em ordem
até a primeira responder. Primeiro os tickers USDT/BRL das exchanges (24/7,
intradiários, respondem da VM); depois o USD/BRL da awesomeapi (intradiário, mas
devolve 429 QuotaExceeded pra IPs de cloud) e as fontes diárias (er-api,
frankfurter). O TTL é por fonte: 60 s nas intradiárias, 10 min nas diárias (não
muda mais rápido que isso). Em falha de todas devolve o último valor conhecido
(ou None) e tenta de novo em 1 min, nunca derruba a página nem segura o poll.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict

import requests

logger = logging.getLogger(__name__)

TTL_INTRADAY = 60.0
TTL_DAILY = 600.0
RETRY_AFTER_FAILURE = 60.0
TIMEOUT = 3.0
USDT = "USDT/BRL"
USD = "USD/BRL"


def _iso(ts: Any) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()


# (nome, url, parser, par, ttl) — parser devolve (cotação, horário da cotação); chave ausente ou status != 200 → próxima fonte.
# Ticker de preço da Binance não traz horário: a cotação vale no instante do fetch.
SOURCES = (
    ("binance", "https://api.binance.com/api/v3/ticker/price?symbol=USDTBRL",
     lambda j: (j["price"], _now_iso()), USDT, TTL_INTRADAY),
    ("bitget", "https://api.bitget.com/api/v2/spot/market/tickers?symbol=USDTBRL",
     lambda j: (j["data"][0]["lastPr"], _iso(int(j["data"][0]["ts"]) // 1000)), USDT, TTL_INTRADAY),
    ("awesomeapi", "https://economia.awesomeapi.com.br/json/last/USD-BRL",
     lambda j: (j["USDBRL"]["bid"], _iso(j["USDBRL"]["timestamp"])), USD, TTL_INTRADAY),
    ("er-api", "https://open.er-api.com/v6/latest/USD",
     lambda j: (j["rates"]["BRL"], _iso(j["time_last_update_unix"])), USD, TTL_DAILY),
    ("frankfurter", "https://api.frankfurter.dev/v1/latest?from=USD&to=BRL",
     lambda j: (j["rates"]["BRL"], j["date"]), USD, TTL_DAILY),
)

EMPTY: Dict[str, Any] = {"usd_brl": None, "source": None, "pair": None, "fetched_at": None}
_cache: Dict[str, Any] = {"next_at": 0.0, "data": dict(EMPTY)}


def usd_brl() -> Dict[str, Any]:
    """{usd_brl, source, pair, fetched_at}; fetched_at é o horário da cotação na fonte (ISO UTC);
    usd_brl=None enquanto nunca houve cotação."""
    now = time.monotonic()
    if now < _cache["next_at"]:
        return _cache["data"]
    for name, url, parse, pair, ttl in SOURCES:
        try:
            resp = requests.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            rate, at = parse(resp.json())
            rate = float(rate)
            if rate <= 0:
                raise ValueError("cotação inválida")
        except Exception as exc:
            logger.warning(f"Cotação dólar/BRL via {name} falhou ({exc}); tentando próxima fonte")
            continue
        _cache["data"] = {"usd_brl": rate, "source": name, "pair": pair, "fetched_at": at}
        _cache["next_at"] = now + ttl
        return _cache["data"]
    logger.warning("Cotação dólar/BRL indisponível em todas as fontes; mantendo último valor")
    _cache["next_at"] = now + RETRY_AFTER_FAILURE
    return _cache["data"]

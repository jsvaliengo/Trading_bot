"""
Cotação USD→BRL para o dashboard. Fontes públicas sem chave, tentadas em ordem
até a primeira responder (awesomeapi devolve 429 QuotaExceeded pra IPs de cloud,
por isso fica por último). Cache de 10 min em memória; em falha de todas devolve
o último valor conhecido (ou None) e tenta de novo em 1 min, nunca derruba a
página nem segura o poll com timeouts repetidos.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict

import requests

logger = logging.getLogger(__name__)

TTL = 600.0
RETRY_AFTER_FAILURE = 60.0
TIMEOUT = 4.0


def _iso(ts: Any) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


# (nome, url, parser) — parser devolve (cotação, horário da cotação); chave ausente ou status != 200 → próxima fonte.
SOURCES = (
    ("er-api", "https://open.er-api.com/v6/latest/USD",
     lambda j: (j["rates"]["BRL"], _iso(j["time_last_update_unix"]))),
    ("frankfurter", "https://api.frankfurter.dev/v1/latest?from=USD&to=BRL",
     lambda j: (j["rates"]["BRL"], j["date"])),
    ("awesomeapi", "https://economia.awesomeapi.com.br/json/last/USD-BRL",
     lambda j: (j["USDBRL"]["bid"], _iso(j["USDBRL"]["timestamp"]))),
)

_cache: Dict[str, Any] = {"next_at": 0.0, "data": {"usd_brl": None, "source": None, "fetched_at": None}}


def usd_brl() -> Dict[str, Any]:
    """{usd_brl, source, fetched_at}; usd_brl=None enquanto nunca houve cotação."""
    now = time.monotonic()
    if now < _cache["next_at"]:
        return _cache["data"]
    for name, url, parse in SOURCES:
        try:
            resp = requests.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            rate, at = parse(resp.json())
            rate = float(rate)
            if rate <= 0:
                raise ValueError("cotação inválida")
        except Exception as exc:
            logger.warning(f"Cotação USD/BRL via {name} falhou ({exc}); tentando próxima fonte")
            continue
        _cache["data"] = {"usd_brl": rate, "source": name, "fetched_at": at}
        _cache["next_at"] = now + TTL
        return _cache["data"]
    logger.warning("Cotação USD/BRL indisponível em todas as fontes; mantendo último valor")
    _cache["next_at"] = now + RETRY_AFTER_FAILURE
    return _cache["data"]

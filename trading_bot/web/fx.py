"""
Cotação USD→BRL para o dashboard (awesomeapi, sem chave). Cache de 10 min em
memória; em falha devolve o último valor conhecido (ou None) e tenta de novo em
1 min, nunca derruba a página nem segura o poll com timeouts repetidos.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict

import requests

logger = logging.getLogger(__name__)

URL = "https://economia.awesomeapi.com.br/json/last/USD-BRL"
TTL = 600.0
RETRY_AFTER_FAILURE = 60.0
TIMEOUT = 4.0

_cache: Dict[str, Any] = {"next_at": 0.0, "data": {"usd_brl": None, "source": "awesomeapi", "fetched_at": None}}


def usd_brl() -> Dict[str, Any]:
    """{usd_brl, source, fetched_at}; usd_brl=None enquanto nunca houve cotação."""
    now = time.monotonic()
    if now < _cache["next_at"]:
        return _cache["data"]
    try:
        q = requests.get(URL, timeout=TIMEOUT).json()["USDBRL"]
        rate = float(q["bid"])
        if rate <= 0:
            raise ValueError("bid inválido")
        at = datetime.fromtimestamp(int(q["timestamp"]), tz=timezone.utc).isoformat()
        _cache["data"] = {"usd_brl": rate, "source": "awesomeapi", "fetched_at": at}
        _cache["next_at"] = now + TTL
    except Exception as exc:
        logger.warning(f"Cotação USD/BRL indisponível ({exc}); mantendo último valor")
        _cache["next_at"] = now + RETRY_AFTER_FAILURE
    return _cache["data"]

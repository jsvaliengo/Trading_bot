import os
import sys
import time
from pathlib import Path

import pytest

# Garante imports do pacote local durante pytest.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def set_process_tz(name):
    if name is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = name
    time.tzset()


@pytest.fixture(autouse=True)
def _process_tz_utc():
    """Fuso do processo fixo em UTC: timestamps ingênuos dos testes = UTC, igual
    no Mac (BRT) e no CI. Testes de fuso trocam via set_process_tz."""
    old = os.environ.get("TZ")
    set_process_tz("UTC")
    yield
    set_process_tz(old)


@pytest.fixture(autouse=True)
def _fx_offline(monkeypatch):
    """Cotação USD/BRL do dashboard nunca sai pra rede nos testes: HTTP falha e o
    cache começa vazio (testes de câmbio mockam requests.get por cima)."""
    from trading_bot.web import fx

    def offline(*a, **k):
        raise ConnectionError("offline nos testes")

    monkeypatch.setattr(fx.requests, "get", offline)
    monkeypatch.setitem(fx._cache, "next_at", 0.0)
    monkeypatch.setitem(fx._cache, "data", {"usd_brl": None, "source": "awesomeapi", "fetched_at": None})

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

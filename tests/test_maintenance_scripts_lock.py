"""Regressão: detecção de bot vivo nos scripts de manutenção.

Os scripts `scripts/reconcile_phantom_trades.py` e `scripts/reset_bot_state.py`
NÃO podem decidir "bot rodando" só pela existência do arquivo de lock — o bot
usa fcntl.flock (advisory) e o arquivo permanece no disco após a morte do
processo. A checagem correta é um probe do flock.
"""

import fcntl
import importlib.util
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = PROJECT_ROOT / "scripts"


def _load(module_name: str):
    spec = importlib.util.spec_from_file_location(
        module_name, SCRIPTS / f"{module_name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SCRIPT_MODULES = ["reconcile_phantom_trades", "reset_bot_state"]


@pytest.fixture(params=SCRIPT_MODULES)
def script_mod(request):
    return _load(request.param)


def test_no_lock_file_means_not_running(script_mod, monkeypatch, tmp_path):
    lock = tmp_path / "trading_bot.test.lock"
    monkeypatch.setattr(script_mod.config, "LOCK_FILE_PATH", str(lock))
    assert script_mod._bot_is_running() is False


def test_stale_lock_file_means_not_running(script_mod, monkeypatch, tmp_path):
    # Arquivo existe mas NINGUÉM segura o flock (PID morto) -> stale.
    lock = tmp_path / "trading_bot.test.lock"
    lock.write_text("pid=999999 started_at=2020-01-01T00:00:00\n")
    monkeypatch.setattr(script_mod.config, "LOCK_FILE_PATH", str(lock))
    assert script_mod._bot_is_running() is False


def test_held_flock_means_running(script_mod, monkeypatch, tmp_path):
    # Simula o bot vivo: mantemos o flock exclusivo durante a checagem.
    lock = tmp_path / "trading_bot.test.lock"
    lock.write_text("pid=1234 started_at=2026-01-01T00:00:00\n")
    monkeypatch.setattr(script_mod.config, "LOCK_FILE_PATH", str(lock))

    holder = open(lock, "a+")
    try:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert script_mod._bot_is_running() is True
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()

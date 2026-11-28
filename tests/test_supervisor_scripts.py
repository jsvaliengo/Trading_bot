"""Supervisor (fase 3, PR 3): update_server.sh sobe um screen bot-accN por
runtime/accounts/N/worker.enabled via run_bot_loop.sh, e a conta 1 continua igual.

Roda os scripts de verdade com screen/pgrep/pkill/python falsos no PATH (nada de
pip/pytest/processo real); cada chamada ao screen fica registrada em um arquivo.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UPDATE_SERVER = PROJECT_ROOT / "scripts" / "update_server.sh"
RUN_BOT_LOOP = PROJECT_ROOT / "scripts" / "run_bot_loop.sh"


def _write_exec(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.mark.parametrize("script", [UPDATE_SERVER, RUN_BOT_LOOP])
def test_scripts_parse(script):
    subprocess.run(["bash", "-n", str(script)], check=True)


@pytest.fixture
def fake_env(tmp_path):
    """PROJECT_DIR isolado + binários falsos; devolve (env, screen_log)."""
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    screen_log = tmp_path / "screen.log"
    _write_exec(fakebin / "screen", f'printf "%s\\n" "$*" >> "{screen_log}"\n')
    # health check da conta 1 usa `pgrep -af` → vivo; stop_pattern usa `pgrep -f` → nada rodando.
    _write_exec(fakebin / "pgrep", '[[ "$1" == "-af" ]] && exit 0 || exit 1\n')
    _write_exec(fakebin / "pkill", "exit 0\n")
    python = _write_exec(fakebin / "python", "exit 0\n")
    (tmp_path / "requirements.txt").write_text("")
    env = {
        **os.environ,
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        "PROJECT_DIR": str(tmp_path),
        "VENV_DIR": str(tmp_path),
        "PYTHON_BIN": str(python),
        "SKIP_GIT_PULL": "1",
        "SKIP_TESTS": "1",
        "HEALTHCHECK_TIMEOUT": "1",
    }
    return env, screen_log


def _run_update_server(env):
    return subprocess.run(
        ["bash", str(UPDATE_SERVER)], env=env, capture_output=True, text=True, timeout=60
    )


def _screen_starts(screen_log: Path):
    lines = screen_log.read_text().splitlines() if screen_log.exists() else []
    return [ln for ln in lines if ln.startswith("-dmS ")]


def test_no_worker_enabled_starts_only_account_1(fake_env, tmp_path):
    env, screen_log = fake_env
    (tmp_path / "runtime" / "accounts" / "2").mkdir(parents=True)  # dir sem marcador
    res = _run_update_server(env)
    assert res.returncode == 0, res.stdout + res.stderr

    starts = _screen_starts(screen_log)
    assert len(starts) == 1
    assert starts[0].startswith("-dmS bot bash -lc ")
    assert "BOT_MODULE='trading_bot.core.bot'" in starts[0]
    assert "BOT_ARGS" not in starts[0]
    assert "-S bot-acc2 -X quit" in screen_log.read_text()  # parado, não relançado
    assert "Conta 2 sem worker.enabled" in res.stdout


def test_worker_enabled_starts_bot_acc2_via_wrapper(fake_env, tmp_path):
    env, screen_log = fake_env
    acc2 = tmp_path / "runtime" / "accounts" / "2"
    acc2.mkdir(parents=True)
    (acc2 / "worker.enabled").touch()
    (tmp_path / "runtime" / "accounts" / "1").mkdir()  # conta 1 nunca vira worker
    (tmp_path / "runtime" / "accounts" / "lixo").mkdir()
    res = _run_update_server(env)
    assert res.returncode == 0, res.stdout + res.stderr

    starts = _screen_starts(screen_log)
    assert [s.split()[1] for s in starts] == ["bot", "bot-acc2"]
    worker = starts[1]
    assert "BOT_MODULE='trading_bot.core.worker'" in worker
    assert "BOT_ARGS='2'" in worker
    assert f"RESTART_LOG='{acc2}/restart.log'" in worker
    assert f"'{tmp_path}/scripts/run_bot_loop.sh' >> '{acc2}/worker.out.log' 2>&1" in worker


def test_run_bot_loop_passes_module_and_args(tmp_path):
    # python falso grava os argumentos e mata o wrapper (senão o loop é infinito).
    args_file = tmp_path / "args.txt"
    python = _write_exec(tmp_path / "python", f'printf "%s\\n" "$@" > "{args_file}"\nkill $PPID\n')
    restart_log = tmp_path / "acc" / "restart.log"
    env = {
        **os.environ,
        "PROJECT_DIR": str(tmp_path),
        "PYTHON_BIN": str(python),
        "BOT_MODULE": "trading_bot.core.worker",
        "BOT_ARGS": "2",
        "RESTART_LOG": str(restart_log),
        "DIAG_PROCESS_PATTERN": "nunca-casa-com-nada-" + tmp_path.name,
    }
    subprocess.run(["bash", str(RUN_BOT_LOOP)], env=env, capture_output=True, timeout=30)
    assert args_file.read_text().split() == ["-m", "trading_bot.core.worker", "2"]
    assert "Iniciando bot" in restart_log.read_text()

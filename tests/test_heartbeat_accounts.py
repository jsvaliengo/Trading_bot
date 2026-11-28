"""scripts/heartbeat.py (fase 3, PR 4): vigia cada runtime/accounts/N/worker.enabled
(processo worker N + mtime do log) com estado e alerta por conta; conta 1 inalterada.

pgrep e o Telegram são substituídos por funções falsas; nada sai do processo.
"""
import importlib.util
import json
import os
import re
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "heartbeat.py"


@pytest.fixture
def hb(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("heartbeat", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.STATE_PATH = tmp_path / "heartbeat_state.json"
    mod.ACCOUNTS_DIR = tmp_path / "accounts"
    mod.sent = []
    mod.send_telegram = mod.sent.append
    mod.alive_cmds = set()  # strings que o pgrep falso considera vivas

    def fake_run(cmd, **_):
        class R:
            returncode = 0 if any(re.search(cmd[2], c) for c in mod.alive_cmds) else 1
        return R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    # conta 1 saudável por padrão
    mod.alive_cmds.add("python -m trading_bot.core.bot")
    mod.log_age_minutes = lambda: 0.1
    return mod


def _enable(hb, n, log_fresh=True):
    d = hb.ACCOUNTS_DIR / str(n)
    d.mkdir(parents=True, exist_ok=True)
    (d / "worker.enabled").touch()
    if log_fresh:
        (d / "trading_bot.prod.mainnet.log").write_text("x")
    return d


def _state(hb):
    return json.loads(hb.STATE_PATH.read_text())


def test_sem_accounts_dir_igual_a_hoje(hb):
    assert hb.main() == 0
    assert hb.sent == []
    assert set(_state(hb)) == {"healthy", "last_alert_ts", "ts"}

    hb.alive_cmds.clear()
    hb.main()
    assert hb.sent == [f"🔴 <b>BOT CAIU</b> [{hb.BADGE}]\n⚠️ processo MORTO\nVerifique a VM (oci-bot)."]
    assert "accounts" not in _state(hb)


def test_conta_2_sem_processo_alerta_so_ela(hb):
    _enable(hb, 2)
    hb.main()
    assert len(hb.sent) == 1
    assert hb.sent[0].startswith(f"🔴 <b>BOT CAIU</b> [{hb.BADGE}] conta 2\n⚠️ processo MORTO")
    st = _state(hb)
    assert st["healthy"] is True  # conta 1 intocada
    assert st["accounts"]["2"]["healthy"] is False

    hb.main()  # ainda caída, dentro da janela de re-aviso: silêncio
    assert len(hb.sent) == 1


def test_conta_2_log_parado_e_recuperacao(hb):
    d = _enable(hb, 2)
    hb.alive_cmds.add("python -m trading_bot.core.worker 2")
    hb.main()
    assert hb.sent == []  # viva + log fresco

    old = time.time() - 60 * (hb.STALE_MINUTES + 1)

    os.utime(d / "trading_bot.prod.mainnet.log", (old, old))
    hb.main()
    assert "conta 2\n⚠️ sem ciclar há" in hb.sent[-1]

    (d / "trading_bot.prod.mainnet.log").write_text("y")
    hb.main()
    assert hb.sent[-1] == f"🟢 <b>BOT RECUPERADO</b> [{hb.BADGE}] conta 2\nVoltou a ciclar normalmente."


def test_marcador_removido_nao_alerta_e_esquece_estado(hb):
    d = _enable(hb, 2)
    hb.main()
    assert len(hb.sent) == 1
    (d / "worker.enabled").unlink()
    hb.main()
    assert len(hb.sent) == 1
    assert "accounts" not in _state(hb)


def test_sem_log_ainda_e_caido(hb):
    _enable(hb, 3, log_fresh=False)
    hb.alive_cmds.add("python -m trading_bot.core.worker 3")
    hb.main()
    assert "conta 3" in hb.sent[0]


def test_padrao_nao_casa_worker_22_nem_conta_1(hb):
    _enable(hb, 2)
    _enable(hb, 1)
    hb.alive_cmds.add("python -m trading_bot.core.worker 22")
    hb.main()
    assert [m for m in hb.sent if "conta 2" in m]
    assert not [m for m in hb.sent if "conta 1" in m]
    assert list(_state(hb)["accounts"]) == ["2"]

    hb.sent.clear()
    hb.alive_cmds.add("python -m trading_bot.core.worker 2")
    hb.main()
    assert "conta 2" in hb.sent[0] and "RECUPERADO" in hb.sent[0]

"""Testes do StateManager — I/O puro, sem dependência do TradingBot."""

import json

from trading_bot.core.state_manager import StateManager


def test_save_writes_file_atomically_and_creates_backup(tmp_path):
    sm = StateManager()
    path = str(tmp_path / "state.json")
    # Estado inicial pré-existente pra validar que backup é criado
    with open(path, "w") as f:
        f.write('{"version": "old"}')

    assert sm.save({"version": "new", "value": 42}, path) is True

    # Arquivo principal tem valor novo
    loaded = json.load(open(path))
    assert loaded["version"] == "new"
    assert loaded["value"] == 42

    # Backup preservou o valor antigo
    backup = open(StateManager.backup_file_path(path)).read()
    assert json.loads(backup)["version"] == "old"

    # Tmp foi limpo
    assert not (tmp_path / "state.json.tmp").exists()


def test_save_creates_directory_if_missing(tmp_path):
    sm = StateManager()
    path = str(tmp_path / "nested" / "deep" / "state.json")

    assert sm.save({"x": 1}, path) is True
    assert json.load(open(path))["x"] == 1


def test_save_returns_false_on_io_error(tmp_path, monkeypatch):
    sm = StateManager()
    # Path impossível de escrever (diretório que não pode ser criado)
    path = "/dev/null/impossible.json"
    assert sm.save({"x": 1}, path) is False


def test_load_returns_primary_when_valid(tmp_path):
    sm = StateManager()
    path = str(tmp_path / "state.json")
    with open(path, "w") as f:
        json.dump({"value": 10}, f)

    payload, source = sm.load(path)
    assert payload == {"value": 10}
    assert source == path


def test_load_falls_back_to_backup_when_primary_corrupted(tmp_path):
    sm = StateManager()
    path = str(tmp_path / "state.json")
    backup = StateManager.backup_file_path(path)

    # Primary corrompido
    with open(path, "w") as f:
        f.write("{not valid json")
    # Backup válido
    with open(backup, "w") as f:
        json.dump({"from": "backup"}, f)

    payload, source = sm.load(path)
    assert payload == {"from": "backup"}
    assert source == backup


def test_load_ignores_backup_when_primary_is_empty_reset(tmp_path):
    """Reset manual (arquivo vazio/{}): não deve restaurar backup."""
    sm = StateManager()
    path = str(tmp_path / "state.json")
    backup = StateManager.backup_file_path(path)

    with open(path, "w") as f:
        f.write("{}")  # reset manual
    with open(backup, "w") as f:
        json.dump({"from": "backup"}, f)

    payload, source = sm.load(path)
    assert payload == {}
    assert source == path  # lê do principal vazio, ignora backup


def test_load_returns_none_when_nothing_exists(tmp_path):
    sm = StateManager()
    path = str(tmp_path / "ghost.json")

    payload, source = sm.load(path)
    assert payload is None
    assert source == ""


def test_migrate_legacy_copies_when_target_missing(tmp_path):
    legacy = tmp_path / "bot_state.json"
    legacy.write_text('{"old": true}')
    target = str(tmp_path / "runtime" / "bot_state.prod.testnet.json")

    StateManager.migrate_legacy(target_path=target, legacy_path=str(legacy))

    assert json.load(open(target)) == {"old": True}


def test_migrate_legacy_noop_when_target_exists(tmp_path):
    legacy = tmp_path / "bot_state.json"
    legacy.write_text('{"old": true}')
    target = tmp_path / "target.json"
    target.write_text('{"existing": true}')

    StateManager.migrate_legacy(target_path=str(target), legacy_path=str(legacy))

    # Target não foi sobrescrito
    assert json.load(open(target)) == {"existing": True}


def test_migrate_legacy_noop_when_legacy_missing(tmp_path):
    target = tmp_path / "target.json"
    StateManager.migrate_legacy(
        target_path=str(target),
        legacy_path=str(tmp_path / "nonexistent.json"),
    )
    assert not target.exists()


def test_backup_file_path_convention():
    assert StateManager.backup_file_path("/tmp/state.json") == "/tmp/state.json.bak"
    assert StateManager.backup_file_path("file") == "file.bak"

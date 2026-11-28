"""control.json (trading_bot/core/control_file.py): leitura tolerante, merge, consumo e ack atômicos."""
from __future__ import annotations

import json
import os

from trading_bot.core import control_file as cf


def _control(tmp_path):
    return json.loads((tmp_path / cf.CONTROL_FILE).read_text())


def test_read_control_missing_file_is_all_none(tmp_path):
    assert cf.read_control(str(tmp_path)) == {"paused": None, "new_cycles": None, "commands": []}


def test_read_control_invalid_json_warns_once_per_content(tmp_path, caplog):
    (tmp_path / cf.CONTROL_FILE).write_text("{nope")
    with caplog.at_level("WARNING"):
        for _ in range(3):
            assert cf.read_control(str(tmp_path))["paused"] is None
    assert sum("JSON inválido" in r.message for r in caplog.records) == 1
    (tmp_path / cf.CONTROL_FILE).write_text("[1,")          # conteúdo inválido diferente: avisa de novo
    with caplog.at_level("WARNING"):
        cf.read_control(str(tmp_path))
    assert sum("JSON inválido" in r.message for r in caplog.records) == 2


def test_read_control_normalizes_and_drops_bad_entries(tmp_path):
    (tmp_path / cf.CONTROL_FILE).write_text(json.dumps({
        "paused": "yes", "new_cycles": False,
        "commands": [{"id": 7, "cmd": "close_all"},
                     {"id": "b", "cmd": "close_position", "symbol": "ADAUSDT", "side": "LONG", "reason": "x"},
                     {"cmd": "close_all"}, {"id": "c", "cmd": "format_disk"}, "lixo"],
    }))
    got = cf.read_control(str(tmp_path))
    assert got["paused"] is None and got["new_cycles"] is False
    assert got["commands"] == [
        {"id": "7", "cmd": "close_all", "symbol": None, "side": None, "reason": "control.json"},
        {"id": "b", "cmd": "close_position", "symbol": "ADAUSDT", "side": "LONG", "reason": "x"},
    ]


def test_write_control_merges_fields_and_creates_dir(tmp_path):
    d = str(tmp_path / "accounts" / "2")
    cf.write_control(d, paused=True)
    cf.write_control(d, new_cycles=False)
    assert json.loads(open(os.path.join(d, cf.CONTROL_FILE)).read()) == {"paused": True, "new_cycles": False}
    assert not os.path.exists(os.path.join(d, cf.CONTROL_FILE + ".tmp"))


def test_consume_commands_removes_ids_and_dropped_keys_preserving_the_rest(tmp_path):
    cf.write_control(str(tmp_path), paused=True, new_cycles=False, extra="fica",
                     commands=[{"id": "a", "cmd": "close_all"}, {"id": "b", "cmd": "close_all"}])
    cf.consume_commands(str(tmp_path), ["a"], drop=("paused",))
    assert _control(tmp_path) == {"new_cycles": False, "extra": "fica", "commands": [{"id": "b", "cmd": "close_all"}]}
    cf.consume_commands(str(tmp_path), ["b"])
    assert _control(tmp_path)["commands"] == [] and _control(tmp_path)["new_cycles"] is False


def test_write_ack_appends_and_keeps_last_n(tmp_path):
    cf.write_ack(str(tmp_path), [{"id": "a", "ok": True, "error": "", "at": "t"}])
    cf.write_ack(str(tmp_path), [{"id": str(i), "ok": False, "error": "e", "at": "t"} for i in range(cf.ACK_KEEP)])
    acks = json.loads((tmp_path / cf.ACK_FILE).read_text())
    assert len(acks) == cf.ACK_KEEP and acks[0]["id"] == "0" and acks[-1]["id"] == str(cf.ACK_KEEP - 1)
    assert not os.path.exists(tmp_path / (cf.ACK_FILE + ".tmp"))

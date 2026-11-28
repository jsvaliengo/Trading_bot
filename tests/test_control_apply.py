"""TradingBot._apply_control_file: control.json → paused/allow_new_cycles/comandos + ack + consumo."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from trading_bot.core import control_file as cf
from trading_bot.core.bot import TradingBot
from trading_bot.core.config import config


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONTROL_FILE_ENABLED", True)
    monkeypatch.setattr(config, "RUNTIME_DIR", str(tmp_path))
    b = TradingBot.__new__(TradingBot)
    b._init_runtime_state()
    b._close_all_positions_daily_target = MagicMock()
    b.close_position_manual = MagicMock(return_value=True)
    return b


def _control(tmp_path):
    return json.loads((tmp_path / cf.CONTROL_FILE).read_text())


def _acks(tmp_path):
    return json.loads((tmp_path / cf.ACK_FILE).read_text())


def test_defaults_allow_new_cycles_and_no_file_is_noop(bot, tmp_path):
    assert bot.allow_new_cycles is True and bot.paused is False
    bot._apply_control_file()
    assert bot.paused is False and not (tmp_path / cf.ACK_FILE).exists()


def test_paused_is_applied_and_consumed(bot, tmp_path):
    cf.write_control(str(tmp_path), paused=True, new_cycles=True)
    bot._apply_control_file()
    assert bot.paused is True
    assert _control(tmp_path) == {"new_cycles": True, "commands": []}   # paused consumido, new_cycles fica
    bot.paused = True                                                   # ex.: stop de portfólio pausou
    bot._apply_control_file()
    assert bot.paused is True                                           # arquivo sem paused não religa
    cf.write_control(str(tmp_path), paused=False)
    bot._apply_control_file()
    assert bot.paused is False


def test_new_cycles_false_blocks_and_true_releases(bot, tmp_path):
    cf.write_control(str(tmp_path), new_cycles=False)
    bot._apply_control_file()
    assert bot.allow_new_cycles is False
    bot._apply_control_file()                                           # idempotente
    assert bot.allow_new_cycles is False
    cf.write_control(str(tmp_path), new_cycles=True)
    bot._apply_control_file()
    assert bot.allow_new_cycles is True


def test_close_all_command_runs_acks_ok_and_is_removed(bot, tmp_path):
    cf.write_control(str(tmp_path), new_cycles=False,
                     commands=[{"id": "c1", "cmd": "close_all", "reason": "painel"}])
    bot._apply_control_file()
    bot._close_all_positions_daily_target.assert_called_once_with("painel")
    (ack,) = _acks(tmp_path)
    assert ack["id"] == "c1" and ack["ok"] is True and ack["error"] == "" and ack["at"]
    assert _control(tmp_path) == {"new_cycles": False, "commands": []}


def test_close_position_passes_symbol_reason_side(bot, tmp_path):
    cf.write_control(str(tmp_path), commands=[
        {"id": "p1", "cmd": "close_position", "symbol": "ADAUSDT", "side": "SHORT", "reason": "manual"}])
    bot._apply_control_file()
    bot.close_position_manual.assert_called_once_with("ADAUSDT", "manual", "SHORT")
    assert _acks(tmp_path)[0]["ok"] is True


def test_failing_command_acks_error_and_is_removed_without_raising(bot, tmp_path):
    bot.close_position_manual.side_effect = LookupError("ADAUSDT não tem posição aberta")
    cf.write_control(str(tmp_path), commands=[
        {"id": "p2", "cmd": "close_position", "symbol": "ADAUSDT"},
        {"id": "c2", "cmd": "close_all"}])
    bot._apply_control_file()
    acks = {a["id"]: a for a in _acks(tmp_path)}
    assert acks["p2"]["ok"] is False and "não tem posição" in acks["p2"]["error"]
    assert acks["c2"]["ok"] is True                                     # o 2º comando ainda roda
    assert _control(tmp_path)["commands"] == []


def test_flag_off_reads_nothing(bot, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONTROL_FILE_ENABLED", False)
    cf.write_control(str(tmp_path), paused=True)
    # o run() só chama _apply_control_file sob a flag; aqui reproduzimos o gate do loop
    if config.CONTROL_FILE_ENABLED:
        bot._apply_control_file()
    assert bot.paused is False and _control(tmp_path) == {"paused": True}


def test_paused_via_control_saves_state_at_once(bot, tmp_path):
    """O painel da conta lê `paused` do bot_state.json: a pausa/retomada via control.json salva o state na hora
    (não espera o save periódico). Sem state_file configurado, o apply segue e consome o paused mesmo assim."""
    cf.write_control(str(tmp_path), paused=True)
    bot._apply_control_file()                                           # bot de teste sem _state_file_path: só avisa
    assert bot.paused is True and _control(tmp_path) == {"commands": []}
    bot._state_file_path = str(tmp_path / "bot_state.test.json")
    cf.write_control(str(tmp_path), paused=False)
    bot._apply_control_file()
    assert json.loads((tmp_path / "bot_state.test.json").read_text())["paused"] is False

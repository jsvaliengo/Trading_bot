"""Regressão: reset_bot_state.py zera coleções com o TIPO correto.

Bug (2026-06-03, prod/testnet): trade_history/portfolio_history deixaram de ser
persistidos no JSON desde o Phase 1 (vivem no SQLite TradeStore). O reset antigo
inferia o tipo-vazio de `state.get(k)`; com a chave ausente isso dava None -> {},
e o load do bot quebrava ao fatiar `trade_history[-500:]` (unhashable type:
'slice'), caindo nos valores padrão. O reset deve produzir LISTA pra essas chaves
mesmo quando ausentes do state, e preservar a config.
"""

import importlib.util
import json
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


@pytest.fixture
def reset_mod():
    return _load("reset_bot_state")


def _write_state(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def _run_reset(reset_mod, monkeypatch, tmp_path, state_payload):
    state_path = tmp_path / "bot_state.test.json"
    _write_state(state_path, state_payload)
    # Sem lock => _bot_is_running() False, reset prossegue.
    monkeypatch.setattr(reset_mod.config, "STATE_FILE_PATH", str(state_path))
    monkeypatch.setattr(reset_mod.config, "LOCK_FILE_PATH", str(tmp_path / "nope.lock"))

    rc = reset_mod.reset(dry_run=False, force=False)
    assert rc == 0
    return json.loads(state_path.read_text())


def test_history_arrays_become_lists_when_absent(reset_mod, monkeypatch, tmp_path):
    """A regressão central: state SEM trade_history/portfolio_history."""
    state = {
        "version": "1.9",
        "closed_trades_count": 14,
        "total_pnl": -2.54,
        "known_positions": {"ETHUSDT_LONG": {"symbol": "ETHUSDT"}},
        "pnl_by_symbol": {"ETHUSDT": -2.54},
        # trade_history / portfolio_history AUSENTES de propósito (Phase 1).
    }
    out = _run_reset(reset_mod, monkeypatch, tmp_path, state)

    assert out["trade_history"] == []
    assert isinstance(out["trade_history"], list)
    assert out["portfolio_history"] == []
    assert isinstance(out["portfolio_history"], list)

    # O que quebrava antes: fatiar a coleção. Agora é list -> seguro.
    assert out["trade_history"][-500:] == []


def test_dict_collections_become_dicts_and_stats_zeroed(reset_mod, monkeypatch, tmp_path):
    state = {
        "closed_trades_count": 14,
        "total_pnl": -2.54,
        "total_fees_paid": 0.41,
        "trades_win_count": 3,
        "trades_loss_count": 11,
        "known_positions": {"ETHUSDT_LONG": {"symbol": "ETHUSDT"}},
        "pnl_by_symbol": {"ETHUSDT": -2.54},
        "peak_prices": {"ETHUSDT": 2600.0},
        "trailing_activated": {"ETHUSDT_LONG": True},
        "double_first_used": {"ETHUSDT": True},
    }
    out = _run_reset(reset_mod, monkeypatch, tmp_path, state)

    for k in ("known_positions", "pnl_by_symbol", "peak_prices",
              "trailing_activated", "double_first_used"):
        assert out[k] == {}, k
        assert isinstance(out[k], dict), k

    for k in ("closed_trades_count", "total_pnl", "total_fees_paid",
              "trades_win_count", "trades_loss_count"):
        assert out[k] == 0


def test_config_is_preserved(reset_mod, monkeypatch, tmp_path):
    state = {
        "closed_trades_count": 14,
        "disabled_pairs": ["BTCUSDT", "RIVERUSDT"],
        "strategy_profiles": [{"name": "trend_strong", "enabled": True}],
        "initial_capital": 100.0,
        "invert_signals": True,
        "kill_switch": {"daily_pnl_history": [{"date": "2026-06-02", "net_pnl": -1.0}]},
    }
    out = _run_reset(reset_mod, monkeypatch, tmp_path, state)

    assert out["disabled_pairs"] == ["BTCUSDT", "RIVERUSDT"]
    assert out["strategy_profiles"] == [{"name": "trend_strong", "enabled": True}]
    assert out["initial_capital"] == 100.0
    # invert_signals e kill_switch NÃO são tocados pelo reset (por design).
    assert out["invert_signals"] is True
    assert out["kill_switch"]["daily_pnl_history"]

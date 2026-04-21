"""Testes do LoopScheduler e funções de timing profile."""

import time
from types import SimpleNamespace

import pytest

from trading_bot.core.scheduler import (
    LoopScheduler,
    get_loop_timing_profile,
    timing_profile_changed,
)


# ---------------------------------------------------------------------------
# LoopScheduler
# ---------------------------------------------------------------------------

def test_add_with_initial_delay_none_starts_after_one_interval():
    sched = LoopScheduler()
    sched.add("foo", interval=5.0)
    # Sem initial_delay explícito → começa devida em 5s
    now = time.monotonic()
    assert sched.due("foo", now) is False
    assert sched.due("foo", now + 5.0) is True


def test_add_with_initial_delay_zero_starts_due_immediately():
    sched = LoopScheduler()
    sched.add("foo", interval=5.0, initial_delay=0.0)
    assert sched.due("foo", time.monotonic()) is True


def test_due_false_for_unknown_task():
    sched = LoopScheduler()
    assert sched.due("unknown", time.monotonic()) is False


def test_mark_ran_reschedules_task_to_now_plus_interval():
    sched = LoopScheduler()
    sched.add("foo", interval=10.0, initial_delay=0.0)
    now = time.monotonic()
    assert sched.due("foo", now) is True

    sched.mark_ran("foo", now)
    # Após mark_ran, tarefa só é devida em +10s
    assert sched.due("foo", now) is False
    assert sched.due("foo", now + 9.99) is False
    assert sched.due("foo", now + 10.01) is True


def test_set_interval_changes_future_scheduling():
    sched = LoopScheduler()
    sched.add("foo", interval=5.0, initial_delay=0.0)
    now = time.monotonic()

    sched.mark_ran("foo", now)
    # Novo interval
    sched.set_interval("foo", 20.0)
    sched.mark_ran("foo", now)

    assert sched.due("foo", now + 10.0) is False  # novo interval é 20s
    assert sched.due("foo", now + 20.01) is True


def test_advance_next_time_forces_future_scheduling():
    sched = LoopScheduler()
    sched.add("foo", interval=5.0, initial_delay=0.0)
    future = time.monotonic() + 100.0
    sched.advance_next_time("foo", future)

    assert sched.due("foo", time.monotonic()) is False
    assert sched.due("foo", future + 0.01) is True


def test_remove_removes_task():
    sched = LoopScheduler()
    sched.add("foo", interval=5.0, initial_delay=0.0)
    sched.remove("foo")
    assert sched.due("foo", time.monotonic()) is False


def test_remove_unknown_is_safe():
    sched = LoopScheduler()
    sched.remove("ghost")  # no-op


def test_invalid_interval_raises():
    sched = LoopScheduler()
    with pytest.raises(ValueError):
        sched.add("foo", interval=0)
    with pytest.raises(ValueError):
        sched.add("foo", interval=-1)
    with pytest.raises(ValueError):
        sched.set_interval("foo", 0)


def test_multiple_tasks_independent():
    sched = LoopScheduler()
    sched.add("a", interval=5.0, initial_delay=0.0)
    sched.add("b", interval=10.0, initial_delay=0.0)

    now = time.monotonic()
    sched.mark_ran("a", now)
    # a já foi, b ainda pendente
    assert sched.due("a", now) is False
    assert sched.due("b", now) is True


# ---------------------------------------------------------------------------
# get_loop_timing_profile — paridade 1:1 com bot original
# ---------------------------------------------------------------------------

def _manual_config(**overrides):
    return SimpleNamespace(
        POSITION_MONITOR_INTERVAL=overrides.get("POSITION_MONITOR_INTERVAL", 5),
        CHECK_INTERVAL=overrides.get("CHECK_INTERVAL", 10),
        ANALYSIS_SYMBOL_DELAY=overrides.get("ANALYSIS_SYMBOL_DELAY", 2.0),
        USE_BINANCE_STRATEGY=overrides.get("USE_BINANCE_STRATEGY", False),
    )


def test_profile_manual_mode_when_binance_strategy_disabled():
    profile = get_loop_timing_profile(_manual_config(), num_pairs=5)
    assert profile == {
        'monitor_interval': 5,
        'analysis_cycle_interval': 10,
        'analysis_symbol_delay': 2.0,
        'mode': 'manual',
        'pairs': 5,
    }


@pytest.mark.parametrize("num_pairs,expected", [
    (2, {'monitor_interval': 2, 'analysis_cycle_interval': 3, 'analysis_symbol_delay': 0.5}),
    (5, {'monitor_interval': 2, 'analysis_cycle_interval': 4, 'analysis_symbol_delay': 0.7}),
    (9, {'monitor_interval': 2, 'analysis_cycle_interval': 5, 'analysis_symbol_delay': 0.9}),
    (10, {'monitor_interval': 2, 'analysis_cycle_interval': 6, 'analysis_symbol_delay': 1.0}),
    (11, {'monitor_interval': 3, 'analysis_cycle_interval': 6, 'analysis_symbol_delay': 1.1}),
    (20, {'monitor_interval': 3, 'analysis_cycle_interval': 7, 'analysis_symbol_delay': 1.2}),
])
def test_profile_binance_tiers(num_pairs, expected):
    profile = get_loop_timing_profile(
        _manual_config(USE_BINANCE_STRATEGY=True),
        num_pairs=num_pairs,
    )
    assert profile['monitor_interval'] == expected['monitor_interval']
    assert profile['analysis_cycle_interval'] == expected['analysis_cycle_interval']
    assert profile['analysis_symbol_delay'] == expected['analysis_symbol_delay']
    assert profile['mode'] == 'dynamic_binance'
    assert profile['pairs'] == num_pairs


# ---------------------------------------------------------------------------
# timing_profile_changed
# ---------------------------------------------------------------------------

def test_profile_changed_detects_monitor_interval_diff():
    old = {'monitor_interval': 2, 'analysis_cycle_interval': 5,
           'analysis_symbol_delay': 0.9, 'pairs': 9, 'mode': 'dynamic_binance'}
    new = dict(old, monitor_interval=3)
    assert timing_profile_changed(old, new) is True


def test_profile_changed_detects_pair_count_diff():
    old = {'monitor_interval': 2, 'analysis_cycle_interval': 5,
           'analysis_symbol_delay': 0.9, 'pairs': 9, 'mode': 'dynamic_binance'}
    new = dict(old, pairs=10)
    assert timing_profile_changed(old, new) is True


def test_profile_unchanged_when_same_values():
    old = {'monitor_interval': 2, 'analysis_cycle_interval': 5,
           'analysis_symbol_delay': 0.9, 'pairs': 9, 'mode': 'dynamic_binance'}
    assert timing_profile_changed(old, dict(old)) is False

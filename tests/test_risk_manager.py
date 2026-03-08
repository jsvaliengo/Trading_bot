"""Tests for RiskManager (strategy.py) — Improvement 11."""
import pytest
from types import SimpleNamespace
from trading_bot.core.strategy import RiskManager


def _make_config(
    max_daily_loss_percent=5.0,
    max_open_positions=10,
):
    return SimpleNamespace(
        MAX_DAILY_LOSS_PERCENT=max_daily_loss_percent,
        MAX_OPEN_POSITIONS=max_open_positions,
    )


def _make_rm(max_daily_loss=5.0, max_positions=10, initial_capital=1000.0):
    cfg = _make_config(max_daily_loss, max_positions)
    rm = RiskManager(cfg, initial_capital)
    return rm


class TestCanOpenPosition:
    def test_allows_when_no_losses(self):
        rm = _make_rm()
        assert rm.can_open_position(open_positions=0) is True

    def test_blocks_when_too_many_positions(self):
        rm = _make_rm(max_positions=2)
        assert rm.can_open_position(open_positions=2) is False

    def test_blocks_when_daily_loss_exceeded(self):
        rm = _make_rm(max_daily_loss=5.0, initial_capital=1000.0)
        rm.update_pnl(-60.0)  # 6% loss on 1000
        assert rm.can_open_position(open_positions=0) is False

    def test_allows_when_daily_loss_just_below_limit(self):
        rm = _make_rm(max_daily_loss=5.0, initial_capital=1000.0)
        rm.update_pnl(-49.9)  # 4.99% loss
        assert rm.can_open_position(open_positions=0) is True

    def test_real_pnl_fn_overrides_accumulated(self):
        rm = _make_rm(max_daily_loss=5.0, initial_capital=1000.0)
        rm.update_pnl(-10.0)  # Small internal loss
        # Real P&L says we lost 6% — should block
        rm._real_daily_pnl_fn = lambda: -60.0
        assert rm.can_open_position(open_positions=0) is False

    def test_real_pnl_fn_fallback_on_exception(self):
        rm = _make_rm(max_daily_loss=5.0, initial_capital=1000.0)
        rm.update_pnl(-10.0)  # Small internal loss, below limit

        def _raising():
            raise RuntimeError("API error")

        rm._real_daily_pnl_fn = _raising
        # Should fall back to internal pnl (10 < 50 limit) — allow
        assert rm.can_open_position(open_positions=0) is True

    def test_backward_compat_current_positions_arg(self):
        """can_open_position still works with positional current_positions arg."""
        rm = _make_rm(max_positions=3)
        assert rm.can_open_position(2) is True
        assert rm.can_open_position(3) is False


class TestUpdatePnl:
    def test_accumulates_positive_pnl(self):
        rm = _make_rm()
        rm.update_pnl(100.0)
        assert rm.daily_pnl == pytest.approx(100.0)

    def test_accumulates_negative_pnl(self):
        rm = _make_rm()
        rm.update_pnl(-50.0)
        rm.update_pnl(-20.0)
        assert rm.daily_pnl == pytest.approx(-70.0)

    def test_mixed_pnl(self):
        rm = _make_rm()
        rm.update_pnl(100.0)
        rm.update_pnl(-30.0)
        assert rm.daily_pnl == pytest.approx(70.0)

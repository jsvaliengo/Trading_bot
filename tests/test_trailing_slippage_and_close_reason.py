"""
Testes do cushion de slippage no piso de breakeven do trailing e da inferência
do motivo de fechamento server-side.

Caso SOL/USDT 13/06: trade fechou em ~+0.02% (TP estava em +1.78%), rotulado
"Take Profit" só porque o P&L era positivo por um triz. Era saída de trailing,
e o slippage do STOP_MARKET furou o piso de breakeven → líquido negativo.
"""
from __future__ import annotations

import pytest

from trading_bot.core.bot import TradingBot
from trading_bot.core.config import config


def _make_light_bot():
    bot = TradingBot.__new__(TradingBot)
    bot._init_runtime_state()
    bot.commission_rates = {"taker_rate": 0.0004, "maker_rate": 0.0002}
    return bot


def test_breakeven_floor_includes_slippage_cushion(monkeypatch):
    monkeypatch.setattr(config, "TRAILING_DISTANCE_PERCENT", 0.50)
    bot = _make_light_bot()
    entry = 100.0
    # Pico baixo → o piso (não o raw trail) domina.
    peak = entry * 1.0044

    monkeypatch.setattr(config, "TRAILING_BREAKEVEN_SLIPPAGE_PERCENT", 0.0)
    floor_sem = bot._trailing_stop_price("LONG", entry, peak)

    monkeypatch.setattr(config, "TRAILING_BREAKEVEN_SLIPPAGE_PERCENT", 0.10)
    floor_com = bot._trailing_stop_price("LONG", entry, peak)

    # Cushion de 0.10% sobe o piso (trailing exige um pouco mais de lucro antes
    # de poder fechar, pra que o slippage do stop ainda deixe ≥ breakeven).
    assert floor_com > floor_sem
    assert floor_com == pytest.approx(entry * (1 + 2 * 0.0004 + 0.0005 + 0.001))


def test_close_reason_take_profit_when_exit_near_tp():
    bot = _make_light_bot()
    r = bot._infer_exchange_close_reason(
        side="LONG", exit_price=69.84, take_profit=69.84, stop_loss=68.21, pnl_gross=2.0
    )
    assert r == "Take Profit (Binance)"


def test_close_reason_stop_loss_when_exit_near_sl():
    bot = _make_light_bot()
    r = bot._infer_exchange_close_reason(
        side="LONG", exit_price=68.21, take_profit=69.84, stop_loss=68.21, pnl_gross=-0.7
    )
    assert r == "Stop Loss (Binance)"


def test_close_reason_trailing_when_exit_between_sl_and_tp():
    # Caso SOL: saiu em 68.64, TP 69.84, SL 68.21 → trailing, NÃO take profit.
    bot = _make_light_bot()
    r = bot._infer_exchange_close_reason(
        side="LONG", exit_price=68.64, take_profit=69.84, stop_loss=68.21, pnl_gross=0.027
    )
    assert r == "Trailing Stop (Binance)"


def test_close_reason_short_mirrored():
    bot = _make_light_bot()
    assert bot._infer_exchange_close_reason(
        side="SHORT", exit_price=68.21, take_profit=68.21, stop_loss=69.84, pnl_gross=2.0
    ) == "Take Profit (Binance)"
    assert bot._infer_exchange_close_reason(
        side="SHORT", exit_price=69.20, take_profit=68.21, stop_loss=69.84, pnl_gross=0.01
    ) == "Trailing Stop (Binance)"


def test_close_reason_falls_back_to_pnl_sign_without_levels():
    bot = _make_light_bot()
    assert bot._infer_exchange_close_reason(
        side="LONG", exit_price=None, take_profit=None, stop_loss=None, pnl_gross=1.0
    ) == "Take Profit (Binance)"
    assert bot._infer_exchange_close_reason(
        side="LONG", exit_price=None, take_profit=None, stop_loss=None, pnl_gross=-1.0
    ) == "Stop Loss (Binance)"

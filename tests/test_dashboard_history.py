"""
Testes do saldo/P&L ACUMULADO e do histórico por dia no dashboard.

Antes os KPIs usavam o realizado do DIA da Binance, então saldo e "P&L total"
zeravam na virada do dia UTC — escondendo o progresso do bot. Agora:
- collect_summary usa o realizado ACUMULADO (TradeStore.cumulative_realized_pnl);
- collect_daily_history expõe P&L por dia (TradeStore.daily_pnl_history).
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from trading_bot.core.trade_store import TradeStore
from trading_bot.web import data as dashboard_data
from trading_bot.core.config import config as global_config


def _store(tmp_path):
    return TradeStore(str(tmp_path / "trades.test.db"))


def _freeze_trade_store_today(monkeypatch, *, year, month, day):
    """Congela o datetime.now() do trade_store numa data fixa.

    Sem isso, testes que escrevem um trade em `datetime.now()` e depois conferem
    `realized_pnl_today()` (que recomputa o "hoje" internamente) flakam ~1x/dia se
    a virada de meia-noite UTC cai entre as duas chamadas. Retorna o "YYYY-MM-DD".
    """
    from trading_bot.core import trade_store as ts_mod

    class _FixedNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(year, month, day, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(ts_mod, "datetime", _FixedNow)
    return f"{year:04d}-{month:02d}-{day:02d}"


def _open(symbol="ETHUSDT", side="LONG", entry=2500.0):
    return {
        "timestamp": "2026-06-01T10:00:00", "symbol": symbol, "signal": "BUY",
        "side": side, "qty": 1.0, "value": 100.0, "entry_price": entry,
        "stop_loss": 2400.0, "take_profit": 2700.0, "strategy_name": "primary",
        "strategy_type": "hedge", "double_first": False, "ai_consultive": {},
    }


def _close(store, *, entry, pnl_net, fees, exit_at, symbol="ETHUSDT", side="LONG"):
    store.record_open(_open(symbol, side, entry))
    store.record_close(
        symbol=symbol, side=side, entry_price=entry, exit_price=entry + 1,
        exit_at=exit_at, pnl_gross=pnl_net + fees, pnl_net=pnl_net, fees=fees,
        close_reason="x", strategy_name="primary",
    )


# ─────────────────────────── cumulative_realized_pnl ───────────────────────────

def test_cumulative_realized_sums_all_closed(tmp_path):
    store = _store(tmp_path)
    _close(store, entry=2500, pnl_net=2.0, fees=0.1, exit_at="2026-06-01T12:00:00")
    _close(store, entry=2510, pnl_net=-1.5, fees=0.1, exit_at="2026-06-02T12:00:00")
    _close(store, entry=2520, pnl_net=4.0, fees=0.1, exit_at="2026-06-03T12:00:00")
    assert store.cumulative_realized_pnl() == pytest.approx(4.5)


def test_cumulative_realized_empty_is_zero(tmp_path):
    assert _store(tmp_path).cumulative_realized_pnl() == 0.0


# ─────────────────────────── daily_pnl_history ───────────────────────────

def test_daily_history_groups_by_day_with_running_cumulative(tmp_path):
    store = _store(tmp_path)
    # dia 1: +2.0 ; dia 2: -1.5 ; dia 3: +4.0  (exatamente o exemplo do user)
    _close(store, entry=2500, pnl_net=2.0, fees=0.1, exit_at="2026-06-01T12:00:00")
    _close(store, entry=2510, pnl_net=-1.5, fees=0.1, exit_at="2026-06-02T09:00:00")
    _close(store, entry=2511, pnl_net=4.0, fees=0.1, exit_at="2026-06-03T09:00:00")

    hist = store.daily_pnl_history()
    assert [d["day"] for d in hist] == ["2026-06-01", "2026-06-02", "2026-06-03"]
    assert [d["net"] for d in hist] == [2.0, -1.5, 4.0]
    # acumulado corrido
    assert [d["cumulative"] for d in hist] == [2.0, 0.5, 4.5]


def test_daily_history_groups_by_local_tz(tmp_path):
    """Com offset -3 (BRT), trades antes das 03:00 UTC caem no dia BRT anterior."""
    store = _store(tmp_path)
    # 01:00 UTC = 22:00 BRT (2026-06-20) ; 05:00 UTC = 02:00 BRT (2026-06-21)
    _close(store, entry=2500, pnl_net=1.0, fees=0.0, exit_at="2026-06-21T01:00:00")
    _close(store, entry=2510, pnl_net=2.0, fees=0.0, exit_at="2026-06-21T05:00:00")
    # UTC (offset 0): ambos no mesmo dia
    utc = store.daily_pnl_history()
    assert [d["day"] for d in utc] == ["2026-06-21"]
    assert utc[0]["net"] == pytest.approx(3.0)
    # BRT (offset -3): dias separados
    brt = store.daily_pnl_history(tz_offset_hours=-3.0)
    assert [d["day"] for d in brt] == ["2026-06-20", "2026-06-21"]
    assert brt[0]["net"] == pytest.approx(1.0)
    assert brt[1]["net"] == pytest.approx(2.0)
    # acumulado segue correto no novo agrupamento
    assert [d["cumulative"] for d in brt] == [pytest.approx(1.0), pytest.approx(3.0)]


def test_update_trade_pnl_corrects_row(tmp_path):
    store = _store(tmp_path)
    _close(store, entry=1700, pnl_net=1.28, fees=0.02, exit_at="2026-06-21T17:00:00",
           symbol="ETHUSDT", side="LONG")
    tid = store.closed_trades_since(lookback_hours=24 * 3650)[0]["id"]
    assert store.update_trade_pnl(tid, pnl_gross=-0.64, fees=0.03, pnl_net=-0.67) is True
    row = store.closed_trades_since(lookback_hours=24 * 3650)[0]
    assert row["pnl_net"] == pytest.approx(-0.67)
    assert row["pnl_gross"] == pytest.approx(-0.64)
    assert row["fees"] == pytest.approx(0.03)


def test_closed_trades_since_filters_by_lookback(tmp_path):
    store = _store(tmp_path)
    # um trade antigo (fora da janela) e um recente — só o recente volta
    from datetime import datetime, timezone, timedelta
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    _close(store, entry=2500, pnl_net=1.0, fees=0.0, exit_at="2020-01-01T00:00:00")
    _close(store, entry=2510, pnl_net=2.0, fees=0.0, exit_at=recent)
    out = store.closed_trades_since(lookback_hours=48)
    assert len(out) == 1 and out[0]["pnl_net"] == pytest.approx(2.0)


def test_realized_pnl_today_respects_tz(tmp_path):
    store = _store(tmp_path)
    _close(store, entry=2500, pnl_net=1.0, fees=0.0, exit_at="2026-06-21T01:00:00")
    _close(store, entry=2510, pnl_net=2.0, fees=0.0, exit_at="2026-06-21T05:00:00")
    # BRT: 01:00 UTC pertence ao dia 20; 05:00 UTC ao dia 21
    assert store.realized_pnl_today("2026-06-20", tz_offset_hours=-3.0) == pytest.approx(1.0)
    assert store.realized_pnl_today("2026-06-21", tz_offset_hours=-3.0) == pytest.approx(2.0)
    # UTC: ambos no dia 21
    assert store.realized_pnl_today("2026-06-21", tz_offset_hours=0.0) == pytest.approx(3.0)


def test_daily_history_aggregates_multiple_trades_same_day(tmp_path):
    store = _store(tmp_path)
    _close(store, entry=2500, pnl_net=1.0, fees=0.1, exit_at="2026-06-01T08:00:00")
    _close(store, entry=2501, pnl_net=-0.5, fees=0.1, exit_at="2026-06-01T20:00:00")
    hist = store.daily_pnl_history()
    assert len(hist) == 1
    d = hist[0]
    assert d["trades"] == 2 and d["wins"] == 1 and d["losses"] == 1
    assert d["win_rate"] == 50.0
    assert d["net"] == pytest.approx(0.5)
    assert d["fees"] == pytest.approx(0.2)


def test_daily_history_limit_keeps_cumulative_correct(tmp_path):
    store = _store(tmp_path)
    _close(store, entry=2500, pnl_net=2.0, fees=0.0, exit_at="2026-06-01T12:00:00")
    _close(store, entry=2510, pnl_net=3.0, fees=0.0, exit_at="2026-06-02T12:00:00")
    _close(store, entry=2520, pnl_net=5.0, fees=0.0, exit_at="2026-06-03T12:00:00")
    hist = store.daily_pnl_history(limit=1)  # só o último dia
    assert len(hist) == 1
    assert hist[0]["day"] == "2026-06-03"
    # cumulative reflete TODOS os dias, não só o cortado
    assert hist[0]["cumulative"] == pytest.approx(10.0)


# ─────────────────────────── realized_pnl_today ───────────────────────────

def test_realized_pnl_today_filtra_por_dia(tmp_path):
    store = _store(tmp_path)
    _close(store, entry=2500, pnl_net=2.0, fees=0.0, exit_at="2026-06-07T12:00:00")
    _close(store, entry=2510, pnl_net=-3.0, fees=0.0, exit_at="2026-06-08T09:00:00")
    _close(store, entry=2511, pnl_net=1.5, fees=0.0, exit_at="2026-06-08T20:00:00")
    # só os dois trades do dia 08 somam (-3.0 + 1.5 = -1.5)
    assert store.realized_pnl_today("2026-06-08") == pytest.approx(-1.5)
    assert store.realized_pnl_today("2026-06-07") == pytest.approx(2.0)
    assert store.realized_pnl_today("2026-06-09") == 0.0


def test_realized_pnl_today_default_usa_hoje_utc(tmp_path, monkeypatch):
    hoje = _freeze_trade_store_today(monkeypatch, year=2026, month=6, day=8)
    store = _store(tmp_path)
    _close(store, entry=2500, pnl_net=4.0, fees=0.0, exit_at=f"{hoje}T10:00:00")
    _close(store, entry=2510, pnl_net=-1.0, fees=0.0, exit_at="2020-01-01T10:00:00")
    assert store.realized_pnl_today() == pytest.approx(4.0)


# ─────────────────────────── collectors do dashboard ───────────────────────────

def test_collect_daily_history_without_store_is_empty():
    bot = SimpleNamespace()  # sem trade_store
    assert dashboard_data.collect_daily_history(bot) == []


def test_collect_daily_history_uses_store(tmp_path):
    store = _store(tmp_path)
    _close(store, entry=2500, pnl_net=2.0, fees=0.1, exit_at="2026-06-01T12:00:00")
    bot = SimpleNamespace(trade_store=store)
    hist = dashboard_data.collect_daily_history(bot)
    assert len(hist) == 1 and hist[0]["net"] == 2.0


# ─────────────────────────── trailing live state (barra) ───────────────────────────

def test_trailing_live_state_short_armed():
    """SHORT armado: activation_price abaixo da entrada, stop atual vindo do bot."""
    key = "DOGEUSDT_SHORT"
    bot = SimpleNamespace(
        peak_prices={key: 0.0825},
        trailing_activated={key: True},
        _trailing_stop_price=lambda side, entry, peak, distance_pct=None: 0.08283,
    )
    payload = {"trailing_activation_pct": 0.5, "trailing_distance_pct": 0.4}
    out = dashboard_data._trailing_live_state(bot, key, "SHORT", 0.0830, payload)
    assert out["trailing_activated"] is True
    assert out["trailing_peak_price"] == pytest.approx(0.0825)
    assert out["trailing_stop_price"] == pytest.approx(0.08283)
    # SHORT arma quando o preço cai 0.5% abaixo da entrada
    assert out["trailing_activation_price"] == pytest.approx(0.0830 * (1 - 0.005))


def test_trailing_live_state_long_not_armed():
    """LONG sem trailing armado: só o activation_price (acima da entrada); sem stop."""
    key = "ETHUSDT_LONG"
    bot = SimpleNamespace(peak_prices={}, trailing_activated={})
    payload = {"trailing_activation_pct": 1.0, "trailing_distance_pct": 0.5}
    out = dashboard_data._trailing_live_state(bot, key, "LONG", 2000.0, payload)
    assert out["trailing_activated"] is False
    assert out["trailing_peak_price"] is None
    assert out["trailing_stop_price"] is None
    assert out["trailing_activation_price"] == pytest.approx(2000.0 * 1.01)


def test_collect_summary_uses_store_cumulative(tmp_path, monkeypatch):
    """Com trade_store, o saldo/P&L total usa o acumulado do store (não o do dia)."""
    monkeypatch.setattr(global_config, "SIMULATED_BALANCE_USD", 100.0, raising=False)
    store = _store(tmp_path)
    _close(store, entry=2500, pnl_net=2.0, fees=0.0, exit_at="2026-06-01T12:00:00")
    _close(store, entry=2510, pnl_net=-12.0, fees=0.0, exit_at="2026-06-04T12:00:00")
    # acumulado = -10.0
    bot = SimpleNamespace(
        initial_capital=100.0, last_known_balance=100.0, total_pnl=999.0,  # ignorado
        daily_realized_pnl=0.0, closed_trades_count=2, paused=False, running=True,
        trade_store=store,
        exchange=SimpleNamespace(
            get_daily_pnl_from_binance=lambda: {"total": 0.0},
            get_account_info=lambda: {"wallet_balance": 100.0, "unrealized_pnl": 0.0},
            get_open_positions=lambda: [],
        ),
    )
    summary = dashboard_data.collect_summary(bot)
    # total_pnl = acumulado (-10) + unrealized (0) = -10  (não o bot.total_pnl=999)
    assert summary["total_pnl"] == pytest.approx(-10.0)
    # saldo = cap 100 + acumulado (-10) = 90  (não volta pra 100 no dia novo)
    assert summary["last_balance"] == pytest.approx(90.0)
    assert summary["daily_pnl"] == 0.0  # P&L HOJE diário, separado


def test_collect_summary_pnl_vem_da_binance_nao_do_db(tmp_path, monkeypatch):
    """P&L HOJE e P&L TOTAL (cards) vêm da BINANCE (fonte de verdade do dinheiro),
    NÃO do DB — o DB tinha valores fabricados/estimados (#181) e divergia do
    extrato. daily = income do dia; total = equity − capital inicial.
    """
    # SIMULATED_BALANCE_USD=0 → caminho mainnet (effective_balance = wallet real).
    monkeypatch.setattr(global_config, "SIMULATED_BALANCE_USD", 0.0, raising=False)
    hoje = _freeze_trade_store_today(monkeypatch, year=2026, month=6, day=8)
    store = _store(tmp_path)
    # DB tem valores (potencialmente fabricados) que NÃO devem aparecer nos cards.
    _close(store, entry=2500, pnl_net=1.24, fees=0.0, exit_at=f"{hoje}T09:00:00")
    _close(store, entry=2510, pnl_net=-5.20, fees=0.0, exit_at=f"{hoje}T16:00:00")
    bot = SimpleNamespace(
        initial_capital=100.0, last_known_balance=100.0, total_pnl=0.0,
        daily_realized_pnl=0.0, closed_trades_count=2, paused=False, running=True,
        trade_store=store,
        exchange=SimpleNamespace(
            # Binance: dia real -1.99 (líquido); wallet real 94.64.
            get_daily_pnl_from_binance=lambda: {"total": -1.99, "funding_fee": -0.01, "commission": -0.66},
            get_account_info=lambda: {"wallet_balance": 94.64, "unrealized_pnl": 0.0},
            get_open_positions=lambda: [],
        ),
        daily_pnl_binance_baseline=0.0,
    )
    summary = dashboard_data.collect_summary(bot)
    # daily = income do dia da Binance (-1.99), NÃO o do DB (-3.96)
    assert summary["daily_pnl"] == pytest.approx(-1.99)
    # total = equity (wallet 94.64) − inicial 100 = -5.36 (real), NÃO o do DB
    assert summary["total_pnl"] == pytest.approx(-5.36)
    assert summary["last_balance"] == pytest.approx(94.64)

"""Testes do TradeStore — persistência SQLite de trades/equity, sem TradingBot."""

from datetime import datetime

import pytest

from trading_bot.core.trade_store import TradeStore


def _store(tmp_path):
    return TradeStore(str(tmp_path / "trades.test.db"))


def _open_record(symbol="ETHUSDT", side="LONG", **over):
    rec = {
        "timestamp": "2026-05-31T10:00:00",
        "symbol": symbol,
        "signal": "BUY",
        "side": side,
        "qty": 1.5,
        "value": 100.0,
        "entry_price": 2500.0,
        "stop_loss": 2400.0,
        "take_profit": 2700.0,
        "strategy_name": "primary",
        "strategy_type": "hedge",
        "double_first": True,
        "ai_consultive": {"decision": "ENTER_NOW", "confidence": 0.8},
    }
    rec.update(over)
    return rec


def test_open_then_recent_trades_roundtrip(tmp_path):
    store = _store(tmp_path)
    tid = store.record_open(_open_record())
    assert isinstance(tid, int) and tid > 0

    trades = store.recent_trades()
    assert len(trades) == 1
    t = trades[0]
    assert t["symbol"] == "ETHUSDT"
    assert t["side"] == "LONG"
    assert t["entry_price"] == 2500.0
    # bool e json round-trip
    assert t["double_first"] is True
    assert t["ai_consultive"]["decision"] == "ENTER_NOW"
    # ainda aberto: sem campos de fechamento
    assert "exit_price" not in t


def test_close_updates_matching_open(tmp_path):
    store = _store(tmp_path)
    store.record_open(_open_record())

    assert store.record_close(
        symbol="ETHUSDT", side="LONG", entry_price=2500.0, exit_price=2600.0,
        exit_at="2026-05-31T11:00:00", pnl_gross=10.0, pnl_net=8.5, fees=1.5,
        close_reason="take_profit", strategy_name="primary",
    ) is True

    trades = store.recent_trades()
    assert len(trades) == 1  # atualizou, não inseriu novo
    t = trades[0]
    assert t["exit_price"] == 2600.0
    assert t["pnl_net"] == 8.5
    assert t["fees"] == 1.5
    assert t["close_reason"] == "take_profit"
    assert t["exit_time"] == "2026-05-31T11:00:00"


def test_close_persists_mfe_pct(tmp_path):
    """MFE (excursão favorável) é gravado no fechamento e volta no read."""
    store = _store(tmp_path)
    store.record_open(_open_record())

    assert store.record_close(
        symbol="ETHUSDT", side="LONG", entry_price=2500.0, exit_price=2480.0,
        exit_at="2026-05-31T11:00:00", pnl_gross=-20.0, pnl_net=-21.5, fees=1.5,
        close_reason="Stop Loss (Binance)", strategy_name="primary",
        mfe_pct=0.48,
    ) is True

    t = store.recent_trades()[0]
    assert t["mfe_pct"] == 0.48  # pico +0.48% mesmo fechando no loss


def test_close_mfe_pct_defaults_to_none(tmp_path):
    """Sem mfe_pct informado, persiste None (compat com chamadas antigas)."""
    store = _store(tmp_path)
    store.record_open(_open_record())
    store.record_close(
        symbol="ETHUSDT", side="LONG", entry_price=2500.0, exit_price=2600.0,
        exit_at="2026-05-31T11:00:00", pnl_gross=10.0, pnl_net=8.5, fees=1.5,
        close_reason="take_profit", strategy_name="primary",
    )
    assert store.recent_trades()[0]["mfe_pct"] is None


def _mk_close(store, symbol, side, pnl, mfe=None):
    store.record_open(_open_record(symbol=symbol, side=side))
    store.record_close(
        symbol=symbol, side=side, entry_price=100.0, exit_price=101.0,
        exit_at="2026-06-18T11:00:00", pnl_gross=pnl, pnl_net=pnl, fees=0.0,
        close_reason="x", strategy_name="primary", mfe_pct=mfe,
    )


def test_pnl_by_symbol_aggregates_and_sorts(tmp_path):
    store = _store(tmp_path)
    _mk_close(store, "BNBUSDT", "LONG", -2.0)
    _mk_close(store, "BNBUSDT", "LONG", -0.18)
    _mk_close(store, "ETHUSDT", "SHORT", 0.51)
    rows = store.pnl_by_symbol()
    by = {r["symbol"]: r for r in rows}
    assert by["BNBUSDT"]["net"] == -2.18 and by["BNBUSDT"]["trades"] == 2
    assert by["ETHUSDT"]["net"] == 0.51
    # ordena por |net| desc → BNB primeiro
    assert rows[0]["symbol"] == "BNBUSDT"


def test_mfe_distribution_buckets_and_avg(tmp_path):
    store = _store(tmp_path)
    for v in [0.1, 0.3, 0.4, 0.6, 1.2, 2.0]:
        _mk_close(store, "ETHUSDT", "LONG", 0.1, mfe=v)
    _mk_close(store, "ETHUSDT", "LONG", 0.1, mfe=None)  # ignorado (sem MFE)
    d = store.mfe_distribution(edges=[0.25, 0.5, 0.75, 1.0, 1.5])
    assert d["n"] == 6
    # buckets: [<.25]=0.1 →1 ; [.25-.5]=0.3,0.4 →2 ; [.5-.75]=0.6 →1 ; [.75-1]=0 ; [1-1.5]=1.2 →1 ; [1.5+]=2.0 →1
    assert d["counts"] == [1, 2, 1, 0, 1, 1]
    assert d["avg"] == pytest.approx((0.1 + 0.3 + 0.4 + 0.6 + 1.2 + 2.0) / 6, abs=0.001)
    assert len(d["labels"]) == 6


def test_close_without_open_inserts_close_only(tmp_path):
    store = _store(tmp_path)
    assert store.record_close(
        symbol="BTCUSDT", side="SHORT", entry_price=60000.0, exit_price=59000.0,
        exit_at=None, pnl_gross=None, pnl_net=12.0, fees=2.0,
        close_reason="reconcile", strategy_name="primary",
    ) is True

    trades = store.recent_trades()
    assert len(trades) == 1
    assert trades[0]["symbol"] == "BTCUSDT"
    assert trades[0]["pnl_net"] == 12.0
    assert trades[0]["exit_time"] is not None  # store carimbou o exit_at


def test_close_picks_latest_open_not_already_closed(tmp_path):
    store = _store(tmp_path)
    # 1º open ETH LONG, fecha
    store.record_open(_open_record())
    store.record_close(
        symbol="ETHUSDT", side="LONG", entry_price=2500.0, exit_price=2600.0,
        exit_at=None, pnl_gross=None, pnl_net=5.0, fees=1.0,
        close_reason="tp", strategy_name="primary",
    )
    # 2º open ETH LONG (reabriu o par+side), fecha
    store.record_open(_open_record(entry_price=2550.0))
    store.record_close(
        symbol="ETHUSDT", side="LONG", entry_price=2550.0, exit_price=2700.0,
        exit_at=None, pnl_gross=None, pnl_net=7.0, fees=1.0,
        close_reason="tp", strategy_name="primary",
    )

    trades = store.recent_trades()
    assert len(trades) == 2  # nenhum close-only espúrio
    # ambos fechados, pnl distintos no trade certo
    pnls = sorted(t["pnl_net"] for t in trades)
    assert pnls == [5.0, 7.0]
    # o trade com entry 2550 tem pnl 7.0
    by_entry = {t["entry_price"]: t["pnl_net"] for t in trades}
    assert by_entry[2550.0] == 7.0
    assert by_entry[2500.0] == 5.0


def test_equity_roundtrip_parses_datetime(tmp_path):
    store = _store(tmp_path)
    snap = {
        "timestamp": datetime(2026, 5, 31, 12, 0, 0),
        "balance": 130.0,
        "pnl_realized": 5.0,
        "pnl_unrealized": -2.0,
        "pnl_total": 3.0,
        "closed_trades": 42,
    }
    assert store.record_equity(snap) is True

    hist = store.recent_equity()
    assert len(hist) == 1
    h = hist[0]
    assert isinstance(h["timestamp"], datetime)
    assert h["balance"] == 130.0
    assert h["pnl_total"] == 3.0
    assert h["closed_trades"] == 42


def test_recent_trades_limit_and_chronological(tmp_path):
    store = _store(tmp_path)
    for i in range(5):
        store.record_open(_open_record(symbol=f"SYM{i}USDT"))

    last3 = store.recent_trades(limit=3)
    assert len(last3) == 3
    # cronológico: os 3 mais recentes em ordem de inserção
    assert [t["symbol"] for t in last3] == ["SYM2USDT", "SYM3USDT", "SYM4USDT"]


def test_migrate_from_state_is_idempotent(tmp_path):
    store = _store(tmp_path)
    legacy_trades = [
        _open_record(symbol="AUSDT"),  # aberto (sem exit_price)
        {**_open_record(symbol="BUSDT"), "exit_price": 10.0, "exit_time":
         "2026-05-30T09:00:00", "pnl_net": 3.0, "fees": 0.5, "close_reason": "tp"},
    ]
    legacy_equity = [{
        "timestamp": "2026-05-30T08:00:00", "balance": 100.0, "pnl_realized": 1.0,
        "pnl_unrealized": 0.0, "pnl_total": 1.0, "closed_trades": 1,
    }]

    assert store.migrate_from_state(legacy_trades, legacy_equity) is True
    assert store.count_trades() == 2
    assert store.count_equity() == 1
    # status derivado do exit_price
    trades = store.recent_trades()
    statuses = {t["symbol"]: ("exit_price" in t) for t in trades}
    assert statuses["BUSDT"] is True   # fechado
    assert statuses["AUSDT"] is False  # aberto

    # 2ª chamada NÃO duplica (idempotente)
    assert store.migrate_from_state(legacy_trades, legacy_equity) is False
    assert store.count_trades() == 2
    assert store.count_equity() == 1


def test_persists_across_reopen(tmp_path):
    db = str(tmp_path / "trades.test.db")
    s1 = TradeStore(db)
    s1.record_open(_open_record())
    s1.close()

    s2 = TradeStore(db)
    assert s2.count_trades() == 1
    s2.close()


def test_reset_clears_trades_and_equity(tmp_path):
    store = _store(tmp_path)
    # 1 trade fechado + 1 snapshot de equity
    store.record_open(_open_record())
    store.record_close(
        symbol="ETHUSDT", side="LONG", entry_price=2500.0, exit_price=2600.0,
        exit_at=None, pnl_gross=100.0, pnl_net=99.0, fees=1.0,
        close_reason="TP", strategy_name="primary",
    )
    store.record_equity({
        "timestamp": datetime(2026, 6, 1, 12, 0, 0), "balance": 100.0,
        "pnl_realized": 99.0, "pnl_unrealized": 0.0, "pnl_total": 99.0,
        "closed_trades": 1,
    })
    assert store.count_trades() == 1
    assert store.count_equity() == 1

    removed = store.reset()
    assert removed == {"trades": 1, "equity": 1}
    assert store.count_trades() == 0
    assert store.count_equity() == 0

    # store segue utilizável após o reset
    store.record_open(_open_record())
    assert store.count_trades() == 1


def test_reset_on_empty_store_is_safe(tmp_path):
    store = _store(tmp_path)
    assert store.reset() == {"trades": 0, "equity": 0}
    assert store.count_trades() == 0


def _close(store, symbol, pnl_net, exit_at):
    store.record_open(_open_record(symbol=symbol))
    store.record_close(
        symbol=symbol, side="LONG", entry_price=2500.0, exit_price=2600.0,
        exit_at=exit_at, pnl_gross=pnl_net, pnl_net=pnl_net, fees=0.0,
        close_reason="tp", strategy_name="primary",
    )


def test_realized_curve_last_point_equals_cumulative(tmp_path):
    """O último ponto da curva == cumulative_realized_pnl(): o histórico do card
    EVOLUÇÃO fecha exatamente com o 'Realizado' do topo."""
    store = _store(tmp_path)
    _close(store, "AUSDT", 1.0, "2026-06-01T10:00:00")
    _close(store, "BUSDT", -0.5, "2026-06-01T10:10:00")
    _close(store, "CUSDT", 2.0, "2026-06-01T10:20:00")

    curve = store.realized_curve(limit=6)
    assert [round(p["cum_pnl"], 4) for p in curve] == [1.0, 0.5, 2.5]
    assert abs(curve[-1]["cum_pnl"] - store.cumulative_realized_pnl()) < 1e-9


def test_realized_curve_orders_by_exit_and_limits(tmp_path):
    """Ordena por exit_at e retorna só os últimos N — mas a soma acumulada ainda
    reflete TODOS os trades anteriores (não reinicia do zero na janela)."""
    store = _store(tmp_path)
    # Insere fora de ordem cronológica de exit_at de propósito.
    _close(store, "AUSDT", 1.0, "2026-06-01T10:00:00")
    _close(store, "DUSDT", 4.0, "2026-06-01T10:30:00")
    _close(store, "BUSDT", 1.0, "2026-06-01T10:10:00")
    _close(store, "CUSDT", 1.0, "2026-06-01T10:20:00")

    curve = store.realized_curve(limit=2)
    assert len(curve) == 2
    # ordenado por exit_at: ...10:20 (cum 3.0), 10:30 (cum 7.0)
    assert [round(p["cum_pnl"], 4) for p in curve] == [3.0, 7.0]
    assert curve[-1]["exit_at"] == "2026-06-01T10:30:00"


def test_realized_curve_empty_when_no_closed_trades(tmp_path):
    store = _store(tmp_path)
    store.record_open(_open_record())  # aberto, não fechado
    assert store.realized_curve() == []

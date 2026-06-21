"""Auto-reconciliação do P&L com o income real da Binance.

Cura a fabricação que escapa quando o user-stream perde o fill do fechamento
(#67 ETH: DB +1,28 vs real −0,67). Compara cada trade fechado recente com os
REALIZED_PNL+COMMISSION reais e corrige o DB quando diverge.
"""
from __future__ import annotations

from types import SimpleNamespace

from trading_bot.services.pnl_reconciler import reconcile_recent_pnl


def _ms(iso):
    import datetime as dt
    return int(dt.datetime.fromisoformat(iso).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


class _FakeStore:
    def __init__(self, trades):
        self._trades = trades
        self.updates = []

    def closed_trades_since(self, lookback_hours=48.0):
        return [dict(t) for t in self._trades]

    def update_trade_pnl(self, trade_id, *, pnl_gross, fees, pnl_net, exit_price=None):
        self.updates.append({
            "id": trade_id, "pnl_gross": pnl_gross, "fees": fees,
            "pnl_net": pnl_net, "exit_price": exit_price,
        })
        return True


def _income(symbol, itype, amount, iso):
    return {"symbol": symbol, "incomeType": itype, "income": str(amount), "time": _ms(iso)}


def _bot(trades, income):
    store = _FakeStore(trades)
    exchange = SimpleNamespace(get_income_history=lambda **kw: income)
    return SimpleNamespace(trade_store=store, exchange=exchange), store


def test_corrige_trade_fabricado():
    """#67-style: DB grava +1.28, real é -0.67 → corrige."""
    trades = [{
        "id": 67, "symbol": "ETHUSDT", "side": "LONG",
        "opened_at": "2026-06-21T16:00:00", "exit_at": "2026-06-21T17:00:00",
        "qty": 0.05, "entry_price": 1700.0, "pnl_net": 1.28, "pnl_gross": 1.30, "fees": 0.02,
    }]
    income = [
        _income("ETHUSDT", "REALIZED_PNL", -0.64, "2026-06-21T17:00:05"),
        _income("ETHUSDT", "COMMISSION", -0.03, "2026-06-21T17:00:05"),
    ]
    bot, store = _bot(trades, income)
    res = reconcile_recent_pnl(bot)
    assert res["corrected"] == 1
    assert len(store.updates) == 1
    u = store.updates[0]
    assert u["id"] == 67
    assert u["pnl_net"] == -0.67          # -0.64 + (-0.03)
    assert u["pnl_gross"] == -0.64
    assert u["fees"] == 0.03


def test_nao_mexe_em_trade_correto():
    """Trade já batendo com a Binance (delta ≤ 1c) é pulado — idempotência."""
    trades = [{
        "id": 1, "symbol": "SOLUSDT", "side": "SHORT",
        "opened_at": "2026-06-21T10:00:00", "exit_at": "2026-06-21T11:00:00",
        "qty": 1.0, "entry_price": 150.0, "pnl_net": -0.13, "pnl_gross": -0.10, "fees": 0.03,
    }]
    income = [
        _income("SOLUSDT", "REALIZED_PNL", -0.10, "2026-06-21T11:00:02"),
        _income("SOLUSDT", "COMMISSION", -0.03, "2026-06-21T11:00:02"),
    ]
    bot, store = _bot(trades, income)
    res = reconcile_recent_pnl(bot)
    assert res["corrected"] == 0
    assert store.updates == []


def test_sem_income_casavel_deixa_intacto():
    """Income ainda não populou (latência) → não corrige, tenta na próxima."""
    trades = [{
        "id": 5, "symbol": "XRPUSDT", "side": "LONG",
        "opened_at": "2026-06-21T10:00:00", "exit_at": "2026-06-21T11:00:00",
        "qty": 10.0, "entry_price": 1.1, "pnl_net": 0.5, "pnl_gross": 0.52, "fees": 0.02,
    }]
    income = []  # nada da Binance ainda
    bot, store = _bot(trades, income)
    res = reconcile_recent_pnl(bot)
    assert res["corrected"] == 0
    assert store.updates == []


def test_nao_casa_simbolo_errado():
    """Income de outro símbolo não contamina o trade."""
    trades = [{
        "id": 9, "symbol": "ETHUSDT", "side": "LONG",
        "opened_at": "2026-06-21T10:00:00", "exit_at": "2026-06-21T11:00:00",
        "qty": 0.05, "entry_price": 1700.0, "pnl_net": 1.28, "pnl_gross": 1.30, "fees": 0.02,
    }]
    income = [_income("BTCUSDT", "REALIZED_PNL", -5.0, "2026-06-21T11:00:02")]
    bot, store = _bot(trades, income)
    res = reconcile_recent_pnl(bot)
    assert res["corrected"] == 0


def test_sem_store_ou_exchange_nao_quebra():
    assert reconcile_recent_pnl(SimpleNamespace())["corrected"] == 0
    assert reconcile_recent_pnl(SimpleNamespace(trade_store=None, exchange=None))["checked"] == 0

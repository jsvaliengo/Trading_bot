"""Auto-reconciliação do P&L com os fills reais da Binance.

Cada trade do DB casa com o seu round trip (fills de entrada + saída do mesmo
símbolo/positionSide). gross = realizedPnl dos fills de saída, fees = comissão
da entrada + da saída, funding = FUNDING_FEE com a posição aberta.
net = gross − fees + funding.
"""
from __future__ import annotations

import datetime as dt
import itertools
import sqlite3
from types import SimpleNamespace

from trading_bot.core.trade_store import TradeStore
from trading_bot.services.pnl_reconciler import reconcile_recent_pnl

_ids = itertools.count(1)


def _ms(hms):
    return int(dt.datetime.fromisoformat(f"2026-10-20T{hms}+00:00").timestamp() * 1000)


class _FakeStore:
    def __init__(self, trades):
        self._trades = trades
        self.updates = []

    def closed_trades_since(self, lookback_hours=48.0):
        return [dict(t) for t in self._trades]

    def update_trade_pnl(self, trade_id, *, pnl_gross, fees, pnl_net, funding=None, exit_price=None):
        self.updates.append({
            "id": trade_id, "pnl_gross": pnl_gross, "fees": fees,
            "pnl_net": pnl_net, "funding": funding, "exit_price": exit_price,
        })
        return True


def _fill(symbol, position_side, side, qty, price, commission, at, realized=0.0, asset="USDT"):
    return {
        "symbol": symbol, "id": next(_ids), "orderId": next(_ids), "side": side,
        "positionSide": position_side, "qty": str(qty), "price": str(price),
        "realizedPnl": str(realized), "commission": str(commission),
        "commissionAsset": asset, "time": _ms(at),
    }


def _funding(symbol, amount, at):
    return {"symbol": symbol, "incomeType": "FUNDING_FEE", "income": str(amount),
            "time": _ms(at), "tradeId": ""}


def _bot(trades, fills, funding=()):
    store = _FakeStore(trades)
    exchange = SimpleNamespace(
        get_account_trades=lambda symbol, **kw: [f for f in fills if f["symbol"] == symbol],
        get_income_history=lambda **kw: list(funding),
    )
    return SimpleNamespace(trade_store=store, exchange=exchange), store


def _trade(id_, symbol, side, opened, exit_, *, gross, fees, qty=1.0, entry=100.0, funding=None):
    return {
        "id": id_, "symbol": symbol, "side": side,
        "opened_at": f"2026-10-20T{opened}+00:00", "exit_at": f"2026-10-20T{exit_}+00:00",
        "qty": qty, "entry_price": entry, "pnl_gross": gross, "fees": fees,
        "funding": funding, "pnl_net": round(gross - fees + (funding or 0.0), 6),
    }


_SUI_135MIN = [
    _fill("SUIUSDT", "SHORT", "SELL", 25, 2.40, 0.024, "09:59:59.500"),
    _fill("SUIUSDT", "SHORT", "BUY", 25, 2.3981, 0.0239, "12:15:00.900", realized=0.0474),
]


def test_hold_de_135min_mantem_as_duas_comissoes():
    """Bug 24/10: a janela −15/+5min do exit só via a taxa da saída e trocava
    fees=0.0479 (entrada+saída) por 0.0239, inflando o net em +0.024."""
    trades = [_trade(42, "SUIUSDT", "SHORT", "10:00:01", "12:15:01", gross=0.0474, fees=0.0479)]
    bot, store = _bot(trades, _SUI_135MIN)
    res = reconcile_recent_pnl(bot)
    assert store.updates == []
    assert res["corrected"] == 0


def test_corrige_fees_so_da_saida_para_entrada_mais_saida():
    """DB já corrompido pelo reconciliador antigo (fees = só a saída) → volta p/ as duas."""
    trades = [_trade(42, "SUIUSDT", "SHORT", "10:00:01", "12:15:01", gross=0.0474, fees=0.0239)]
    bot, store = _bot(trades, _SUI_135MIN)
    res = reconcile_recent_pnl(bot)
    assert store.updates == [{
        "id": 42, "pnl_gross": 0.0474, "fees": 0.0479, "pnl_net": -0.0005,
        "funding": 0.0, "exit_price": 2.3981,
    }]
    assert (res["corrected"], res["fabricated"]) == (1, 0)  # só taxa: não é fabricação


def test_trades_colados_no_mesmo_simbolo_nao_roubam_taxas():
    """#89/#95: saída a <5min da entrada seguinte pegava a comissão da entrada do
    próximo trade. Cada trade fica só com os fills do próprio round trip."""
    trades = [
        _trade(89, "SOLUSDT", "LONG", "10:00:02", "11:00:01", gross=0.0, fees=0.0),
        _trade(90, "SOLUSDT", "LONG", "11:01:02", "13:00:01", gross=0.0, fees=0.0),
    ]
    fills = [
        _fill("SOLUSDT", "LONG", "BUY", 0.4, 150.0, 0.030, "10:00:00"),
        _fill("SOLUSDT", "LONG", "SELL", 0.4, 151.0, 0.031, "11:00:00", realized=0.40),
        _fill("SOLUSDT", "LONG", "BUY", 0.4, 151.0, 0.032, "11:01:00"),
        _fill("SOLUSDT", "LONG", "SELL", 0.4, 149.0, 0.033, "13:00:00", realized=-0.80),
    ]
    bot, store = _bot(trades, fills)
    reconcile_recent_pnl(bot)
    by_id = {u["id"]: u for u in store.updates}
    assert (by_id[89]["pnl_gross"], by_id[89]["fees"]) == (0.40, 0.061)
    assert (by_id[90]["pnl_gross"], by_id[90]["fees"]) == (-0.80, 0.065)


def test_long_e_short_simultaneos_no_mesmo_simbolo_ficam_separados():
    trades = [
        _trade(1, "ETHUSDT", "LONG", "10:00:02", "12:00:01", gross=0.0, fees=0.0),
        _trade(2, "ETHUSDT", "SHORT", "10:30:02", "11:00:01", gross=0.0, fees=0.0),
    ]
    fills = [
        _fill("ETHUSDT", "LONG", "BUY", 0.02, 2400, 0.020, "10:00:00"),
        _fill("ETHUSDT", "SHORT", "SELL", 0.02, 2410, 0.021, "10:30:00"),
        _fill("ETHUSDT", "SHORT", "BUY", 0.02, 2405, 0.022, "11:00:00", realized=0.10),
        _fill("ETHUSDT", "LONG", "SELL", 0.02, 2420, 0.023, "12:00:00", realized=0.40),
    ]
    bot, store = _bot(trades, fills)
    reconcile_recent_pnl(bot)
    by_id = {u["id"]: u for u in store.updates}
    assert (by_id[1]["pnl_gross"], by_id[1]["fees"]) == (0.40, 0.043)
    assert (by_id[2]["pnl_gross"], by_id[2]["fees"]) == (0.10, 0.043)


def test_perna_com_comissao_desconhecida_nao_sobrescreve_fees():
    """Entrada paga em BNB → só a perna de saída é conhecida: mantém o fee do DB
    (não troca 0.048 por só a saída) e ainda corrige o gross fabricado."""
    trades = [_trade(7, "XRPUSDT", "SHORT", "10:00:02", "11:00:01", gross=0.50, fees=0.048)]
    fills = [
        _fill("XRPUSDT", "SHORT", "SELL", 50, 1.2, 0.00004, "10:00:00", asset="BNB"),
        _fill("XRPUSDT", "SHORT", "BUY", 50, 1.21, 0.0242, "11:00:00", realized=-0.50),
    ]
    bot, store = _bot(trades, fills)
    res = reconcile_recent_pnl(bot)
    assert store.updates == [{
        "id": 7, "pnl_gross": -0.50, "fees": 0.048, "pnl_net": -0.548,
        "funding": 0.0, "exit_price": 1.21,
    }]
    assert res["fabricated"] == 1


def test_funding_vai_para_o_trade_aberto_naquele_horario():
    trades = [
        _trade(1, "DOGEUSDT", "SHORT", "07:00:02", "09:00:01", gross=0.0, fees=0.0),
        _trade(2, "DOGEUSDT", "SHORT", "15:00:02", "17:00:01", gross=0.0, fees=0.0),
    ]
    fills = [
        _fill("DOGEUSDT", "SHORT", "SELL", 700, 0.08, 0.02, "07:00:00"),
        _fill("DOGEUSDT", "SHORT", "BUY", 700, 0.08, 0.02, "09:00:00"),
        _fill("DOGEUSDT", "SHORT", "SELL", 700, 0.08, 0.02, "15:00:00"),
        _fill("DOGEUSDT", "SHORT", "BUY", 700, 0.08, 0.02, "17:00:00"),
    ]
    funding = [
        _funding("DOGEUSDT", 0.003, "08:00:00"),
        _funding("DOGEUSDT", 0.005, "16:00:00"),
        _funding("SUIUSDT", 0.9, "16:00:00"),   # outro símbolo
        _funding("DOGEUSDT", 0.7, "00:00:00"),  # sem posição aberta
    ]
    bot, store = _bot(trades, fills, funding)
    reconcile_recent_pnl(bot)
    by_id = {u["id"]: u for u in store.updates}
    assert (by_id[1]["funding"], by_id[1]["pnl_net"]) == (0.003, -0.037)
    assert (by_id[2]["funding"], by_id[2]["pnl_net"]) == (0.005, -0.035)


def test_falha_ao_buscar_funding_nao_zera_funding_salvo():
    trades = [_trade(1, "DOGEUSDT", "SHORT", "07:00:02", "09:00:01", gross=0.0, fees=0.0, funding=0.003)]
    fills = [
        _fill("DOGEUSDT", "SHORT", "SELL", 700, 0.08, 0.02, "07:00:00"),
        _fill("DOGEUSDT", "SHORT", "BUY", 700, 0.08, 0.02, "09:00:00"),
    ]
    bot, store = _bot(trades, fills)

    def boom(**kw):
        raise RuntimeError("timeout")
    bot.exchange.get_income_history = boom
    reconcile_recent_pnl(bot)
    assert store.updates == [{
        "id": 1, "pnl_gross": 0.0, "fees": 0.04, "pnl_net": -0.037,
        "funding": 0.003, "exit_price": 0.08,
    }]


def test_corrige_trade_fabricado():
    """#67-style: DB grava +1.28, real é −0.67 → corrige e conta como fabricação."""
    trades = [_trade(67, "ETHUSDT", "LONG", "16:00:02", "17:00:01", gross=1.30, fees=0.02, qty=0.05)]
    fills = [
        _fill("ETHUSDT", "LONG", "BUY", 0.05, 1700, 0.015, "16:00:00"),
        _fill("ETHUSDT", "LONG", "SELL", 0.05, 1687.2, 0.015, "17:00:00", realized=-0.64),
    ]
    bot, store = _bot(trades, fills)
    res = reconcile_recent_pnl(bot)
    assert (res["corrected"], res["fabricated"]) == (1, 1)
    assert store.updates[0]["pnl_net"] == -0.67
    assert res["details"][0]["gross_fixed"] is True


def test_sem_fills_deixa_intacto():
    trades = [_trade(5, "XRPUSDT", "LONG", "10:00:02", "11:00:01", gross=0.52, fees=0.02)]
    bot, store = _bot(trades, [])
    assert reconcile_recent_pnl(bot)["corrected"] == 0
    assert store.updates == []


def test_falha_ao_buscar_fills_deixa_intacto():
    trades = [_trade(5, "XRPUSDT", "LONG", "10:00:02", "11:00:01", gross=0.52, fees=0.02)]
    bot, store = _bot(trades, [])

    def boom(**kw):
        raise RuntimeError("timeout")
    bot.exchange.get_account_trades = boom
    assert reconcile_recent_pnl(bot)["corrected"] == 0
    assert store.updates == []


def test_sem_store_ou_exchange_nao_quebra():
    assert reconcile_recent_pnl(SimpleNamespace())["corrected"] == 0
    assert reconcile_recent_pnl(SimpleNamespace(trade_store=None, exchange=None))["checked"] == 0


def test_migracao_funding_idempotente(tmp_path):
    db = str(tmp_path / "t.db")
    TradeStore(db)
    TradeStore(db)  # 2ª abertura não pode falhar nem duplicar a coluna
    cols = [r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(trades)")]
    assert cols.count("funding") == 1


def test_historico_diario_e_realizado_de_hoje_usam_net_com_funding(tmp_path):
    """Kill switch (daily_pnl_history) e P&L de hoje somam o net reconciliado."""
    store = TradeStore(str(tmp_path / "t.db"))
    store.record_open({"timestamp": "2026-10-20T10:00:00+00:00", "symbol": "DOGEUSDT",
                       "side": "SHORT", "qty": 1.0, "entry_price": 1.0})
    store.record_close(symbol="DOGEUSDT", side="SHORT", entry_price=1.0, exit_price=1.0,
                       exit_at="2026-10-20T12:00:00+00:00", pnl_gross=0.14, pnl_net=0.10,
                       fees=0.04, close_reason="tp", strategy_name="primary")
    tid = store.closed_trades_since(lookback_hours=24 * 3650)[0]["id"]
    assert store.update_trade_pnl(tid, pnl_gross=0.14, fees=0.048, funding=0.5, pnl_net=0.592)
    row = store.closed_trades_since(lookback_hours=24 * 3650)[0]
    assert (row["fees"], row["funding"], row["pnl_net"]) == (0.048, 0.5, 0.592)
    hist = store.daily_pnl_history(limit=0, tz_offset_hours=0)
    assert [(d["day"], d["net"]) for d in hist] == [("2026-10-20", 0.592)]
    assert store.realized_pnl_today(day="2026-10-20", tz_offset_hours=0) == 0.592

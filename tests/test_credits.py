"""Créditos pré-pagos (fase 3, PR 6): consumo derivado dos ciclos positivos do trades.db da conta (carência 1 h,
idempotente), recarga admin via API, trava new_cycles no control.json, conta 1 isenta, throttle do /api/live."""
from datetime import datetime, timedelta, timezone

import pytest

from trading_bot.core import control_file as cf
from trading_bot.core.trade_store import TradeStore
from trading_bot.web import credits
from tests.test_dashboard import _dashboard_config, _login, _post, _worker_account  # noqa: F401  (autouse: platform.db no tmp)


def _cycle(ts, pnl, hours_ago, symbol="ADAUSDT"):
    now = datetime.now(timezone.utc)
    ts.record_open({"timestamp": (now - timedelta(hours=hours_ago + 2)).isoformat(), "symbol": symbol, "side": "LONG",
                    "entry_price": 0.5, "qty": 100.0, "strategy_name": "martingale_hedge", "strategy_type": "martingale_hedge"})
    ts.record_close(symbol=symbol, side="LONG", entry_price=0.5, exit_price=0.51, exit_at=(now - timedelta(hours=hours_ago)).isoformat(),
                    pnl_gross=pnl, pnl_net=pnl, fees=0.0, close_reason="Martingale TP", strategy_name="martingale_hedge")


def _setup(monkeypatch, tmp_path):
    """Conta da maria com worker: 2 ciclos positivos antigos (3.0 e 1.25), 1 negativo antigo, 1 positivo recente (<1 h)."""
    monkeypatch.setattr(credits, "_last_settle", {})
    app, client, aid, plain, d, _ = _worker_account(monkeypatch, tmp_path, trades=False)
    ts = TradeStore(str(d / "trades.prod.mainnet.db"))
    for pnl, hours_ago in ((3.0, 5), (1.25, 3), (-2.0, 2), (9.0, 0.5)):
        _cycle(ts, pnl, hours_ago)
    ts.close()
    return app, client, aid, plain, d


def test_settle_consumes_only_old_positive_cycles_and_is_idempotent(monkeypatch, tmp_path):
    app, client, aid, plain, d = _setup(monkeypatch, tmp_path)
    store = app.config["PLATFORM"]
    assert store.account(aid)["credit_pct"] == 20.0 and store.account(1)["credit_pct"] == 0.0
    out = credits.settle_credits(store, aid, str(d))
    consumed = sorted((e["ref"].split("@")[0], e["amount"]) for e in out["ledger"] if e["kind"] == "consume")
    assert consumed == [("1", pytest.approx(0.6)), ("2", pytest.approx(0.25))]   # 20% de 3.0 e de 1.25; negativo e recente ficam fora
    assert all("@20" in e["ref"] for e in out["ledger"])   # ref = id@opened_at: um trades.db resetado não reaproveita a chave
    assert out["balance"] == pytest.approx(-0.85) and out["exhausted"] is True
    again = credits.settle_credits(store, aid, str(d))
    assert len([e for e in again["ledger"] if e["kind"] == "consume"]) == 2 and again["balance"] == pytest.approx(-0.85)


def test_topup_api_admin_only_and_balance(monkeypatch, tmp_path):
    app, client, aid, plain, d = _setup(monkeypatch, tmp_path)
    assert _post(client, f"/api/accounts/{aid}/credits", {"amount": 10}).status_code == 403   # maria não é admin
    admin = app.test_client()
    _login(admin)
    r = _post(admin, f"/api/accounts/{aid}/credits", {"amount": 10, "note": "PIX", "credit_pct": 25})
    assert r.status_code == 200 and r.get_json()["credit_pct"] == 25.0
    assert r.get_json()["balance"] == pytest.approx(10 - 0.75 - 0.3125)   # recarga liquida na hora, já com 25%
    assert _post(admin, f"/api/accounts/{aid}/credits", {"amount": -1}).status_code == 400
    assert _post(admin, f"/api/accounts/{aid}/credits", {"amount": 0, "credit_pct": 101}).status_code == 400
    g = client.get(f"/api/accounts/{aid}/credits").get_json()   # dona vê o próprio saldo e extrato
    assert g["balance"] == pytest.approx(8.9375) and [e["kind"] for e in g["ledger"]] == ["consume", "consume", "topup"]
    assert g["ledger"][-1]["note"] == "PIX" and g["low"] is False and g["exhausted"] is False


def test_gate_writes_new_cycles_only_when_it_changes(monkeypatch, tmp_path):
    app, client, aid, plain, d = _setup(monkeypatch, tmp_path)
    store = app.config["PLATFORM"]
    writes = []
    real = cf.write_control
    monkeypatch.setattr(cf, "write_control", lambda rd, **f: writes.append(f) or real(rd, **f))
    credits.settle_credits(store, aid, str(d))
    assert writes == [{"new_cycles": False}] and cf.read_control(str(d))["new_cycles"] is False
    credits.settle_credits(store, aid, str(d))
    assert len(writes) == 1   # já estava False: não reescreve
    admin = app.test_client()
    _login(admin)
    assert _post(admin, f"/api/accounts/{aid}/credits", {"amount": 5}).get_json()["exhausted"] is False
    assert writes[-1] == {"new_cycles": True} and cf.read_control(str(d))["new_cycles"] is True and len(writes) == 2
    credits.settle_credits(store, aid, str(d))
    assert len(writes) == 2


def test_account_one_and_zero_pct_never_consume_nor_gate(monkeypatch, tmp_path):
    app, client, aid, plain, d = _setup(monkeypatch, tmp_path)
    store = app.config["PLATFORM"]
    out = credits.settle_credits(store, 1, str(d))   # mesmo com um runtime_dir cheio de ciclos
    assert out == {"balance": 0.0, "credit_pct": 0.0, "exhausted": False, "low": False, "ledger": []}
    store.set_credit_pct(aid, 0)
    assert credits.settle_credits(store, aid, str(d))["ledger"] == [] and cf.read_control(str(d))["new_cycles"] is None
    admin = app.test_client()
    _login(admin)
    assert "credits" not in admin.get("/api/live").get_json()   # conta 1 (bot do processo): sem payload de créditos


def test_live_includes_credits_and_settles_once_per_minute(monkeypatch, tmp_path):
    app, client, aid, plain, d = _setup(monkeypatch, tmp_path)
    clock = [1000.0]
    monkeypatch.setattr(credits, "_clock", lambda: clock[0])
    calls = []
    real = credits.settle_credits
    monkeypatch.setattr(credits, "settle_credits", lambda *a, **k: calls.append(1) or real(*a, **k))
    c = client.get(f"/api/live?account={aid}").get_json()["credits"]
    assert c["exhausted"] is True and c["credit_pct"] == 20.0 and c["balance"] == pytest.approx(-0.85)
    clock[0] += 30
    assert client.get(f"/api/live?account={aid}").get_json()["credits"]["balance"] == pytest.approx(-0.85)
    assert len(calls) == 1
    clock[0] += 31
    client.get(f"/api/live?account={aid}")
    assert len(calls) == 2
    assert "credits" not in client.get(f"/api/live?account={plain}").get_json()   # conta sem worker: nada


def test_low_balance_flag_and_settle_all_skips_account_one(monkeypatch, tmp_path):
    app, client, aid, plain, d = _setup(monkeypatch, tmp_path)
    store = app.config["PLATFORM"]
    store.add_credit(aid, "topup", 5.5, "manual", "")
    (tmp_path / "rt" / "accounts" / "1").mkdir()
    (tmp_path / "rt" / "accounts" / "1" / "worker.enabled").write_text("")
    credits.settle_all(store)
    out = credits.summary(store, aid)
    assert out["balance"] == pytest.approx(4.65) and out["low"] is True and out["exhausted"] is False   # < max(5, 2×0.6)
    assert store.credit_ledger(1) == [] and cf.read_control(str(d))["new_cycles"] is True

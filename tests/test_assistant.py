"""Assistente da conta (PR A): flag, loop tool-use com provider fake, escopo por account_bot, cota diária,
ferramentas sobre WorkerAccount, erro do provedor, redação de segredos."""
from types import SimpleNamespace

import pytest

from trading_bot.ai import assistant as ai
from trading_bot.core.config import config as global_config
from trading_bot.web import assistant_routes
from tests.test_dashboard import _dashboard_config, _login, _post, _worker_account  # noqa: F401  (autouse: platform.db no tmp)


def _text(text, in_t=100, out_t=20):
    return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)],
                           usage=SimpleNamespace(input_tokens=in_t, output_tokens=out_t))


def _tool_use(name, args=None, uid="tu_1"):
    return SimpleNamespace(stop_reason="tool_use", content=[SimpleNamespace(type="tool_use", id=uid, name=name, input=args or {})],
                           usage=SimpleNamespace(input_tokens=200, output_tokens=30))


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(global_config, "ASSISTANT_ENABLED", True, raising=False)
    monkeypatch.setattr(assistant_routes, "_busy", set())


def _setup(monkeypatch, tmp_path, provider):
    app, client, aid, plain, d, _ = _worker_account(monkeypatch, tmp_path)
    app.config["ASSISTANT_PROVIDER"] = provider
    return app, client, aid, plain, d


def test_flag_off_routes_404_and_no_button(monkeypatch, tmp_path):
    app, client, aid, plain, d = _setup(monkeypatch, tmp_path, ai.FakeProvider([_text("oi")]))
    assert _post(client, f"/api/assistant?account={aid}", {"message": "oi"}).status_code == 404
    assert client.get(f"/api/assistant/history?account={aid}").status_code == 404
    html = client.get("/").get_data(as_text=True)
    assert "assistant-btn" not in html and "assistant.js" not in html


def test_post_runs_tool_loop_and_persists_history(monkeypatch, tmp_path, on):
    provider = ai.FakeProvider([_tool_use("estado"), _text("Pausado, equity 96,50 USDT.")])
    app, client, aid, plain, d = _setup(monkeypatch, tmp_path, provider)
    html = client.get("/").get_data(as_text=True)
    assert 'id="assistant-btn"' in html and "assistant.js" in html
    r = _post(client, f"/api/assistant?account={aid}", {"message": "como está a conta?"})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["reply"] == "Pausado, equity 96,50 USDT." and body["tool_calls"] == ["estado"]
    assert body["usage"] == {"input_tokens": 300, "output_tokens": 50} and body["remaining_today"] == 19
    # 1ª chamada: system da conta certa + 6 ferramentas; 2ª chamada: tool_result com JSON do estado
    first, second = provider.calls
    assert f"assistente da conta {aid}" in first["system"] and [t["name"] for t in first["tools"]] == ["estado", "trades", "pnl", "taxas_planilha", "creditos", "docs"]
    result = second["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "tu_1" and '"paused": true' in result["content"]
    h = client.get(f"/api/assistant/history?account={aid}&conversation_id={body['conversation_id']}").get_json()
    assert [(m["role"], m["tool_calls"]) for m in h["messages"]] == [("user", []), ("assistant", ["estado"])]
    # 2ª mensagem na mesma conversa reenvia o histórico
    provider._replies.append(_text("Sim."))
    r2 = _post(client, f"/api/assistant?account={aid}", {"message": "e os trades?", "conversation_id": body["conversation_id"]})
    assert r2.status_code == 200 and r2.get_json()["conversation_id"] == body["conversation_id"]
    assert [m["role"] for m in provider.calls[-1]["messages"]] == ["user", "assistant", "user"]
    assert len(client.get(f"/api/assistant/history?account={aid}&conversation_id={body['conversation_id']}").get_json()["messages"]) == 4


def test_scope_other_users_account_is_404_before_provider(monkeypatch, tmp_path, on):
    provider = ai.FakeProvider([_text("não devia chegar aqui")])
    app, client, aid, plain, d = _setup(monkeypatch, tmp_path, provider)
    assert _post(client, "/api/assistant?account=1", {"message": "oi"}).status_code == 404   # maria não é dona da conta 1
    assert client.get("/api/assistant/history?account=1&conversation_id=1").status_code == 404
    assert provider.calls == []
    # conversa de outra conta do mesmo usuário também não vaza
    r = _post(client, f"/api/assistant?account={aid}", {"message": "oi"})
    cid = r.get_json()["conversation_id"]
    assert client.get(f"/api/assistant/history?account={plain}&conversation_id={cid}").status_code == 404
    assert _post(client, f"/api/assistant?account={plain}", {"message": "oi", "conversation_id": cid}).status_code == 404


def test_daily_quota_429_on_21st_message(monkeypatch, tmp_path, on):
    provider = ai.FakeProvider([_text("ok")] * 21)
    app, client, aid, plain, d = _setup(monkeypatch, tmp_path, provider)
    store = app.config["PLATFORM"]
    uid = store.user_by_name("maria")["id"]
    cid = store.create_conversation(uid, aid)
    for _ in range(19):
        store.add_message(cid, "user", "x")
    r = _post(client, f"/api/assistant?account={aid}", {"message": "20ª"})
    assert r.status_code == 200 and r.get_json()["remaining_today"] == 0
    r = _post(client, f"/api/assistant?account={aid}", {"message": "21ª"})
    assert r.status_code == 429 and len(provider.calls) == 1
    assert "mensagens por dia" in r.get_json()["error"]


def test_busy_user_gets_409(monkeypatch, tmp_path, on):
    app, client, aid, plain, d = _setup(monkeypatch, tmp_path, ai.FakeProvider([_text("ok")]))
    uid = app.config["PLATFORM"].user_by_name("maria")["id"]
    assistant_routes._busy.add(uid)
    assert _post(client, f"/api/assistant?account={aid}", {"message": "oi"}).status_code == 409


def test_provider_error_502_and_nothing_persisted(monkeypatch, tmp_path, on):
    class Boom:
        def create(self, **kw):
            raise RuntimeError("api key sk-ant-SEGREDO inválida")
    app, client, aid, plain, d = _setup(monkeypatch, tmp_path, Boom())
    r = _post(client, f"/api/assistant?account={aid}", {"message": "oi"})
    assert r.status_code == 502 and "SEGREDO" not in r.get_data(as_text=True)
    store = app.config["PLATFORM"]
    assert store.user_messages_today(store.user_by_name("maria")["id"]) == 0
    assert store._select("SELECT COUNT(*) AS n FROM assistant_conversations")[0]["n"] == 0


def test_tools_on_worker_account(monkeypatch, tmp_path, on):
    app, client, aid, plain, d = _setup(monkeypatch, tmp_path, ai.FakeProvider([]))
    with app.test_request_context(f"/?account={aid}"):
        from flask import session
        session["auth"], session["user_id"] = True, app.config["PLATFORM"].user_by_name("maria")["id"]
        bot = app.config["ACCOUNT_BOT"]()
    tools = ai.AccountTools(bot, app.config["PLATFORM"], aid)
    estado = tools.tool_estado()
    assert estado["paused"] is True and estado["initial_capital"] == 150.0 and estado["peak_equity"] == 160.0
    assert estado["positions"][0]["symbol"] == "ADAUSDT" and estado["positions"][0]["mark_price"] == 0.51
    trades = tools.tool_trades(limit=500)["trades"]
    assert len(trades) == 2 and trades[0]["pnl_net"] == 1.2 and trades[1]["pnl_net"] == -0.4 and set(trades[0]) >= {"symbol", "side", "exit_at", "pnl_net", "close_reason"}
    assert tools.tool_trades(limit=1, symbol="adausdt")["trades"][0]["pnl_net"] == 1.2
    assert tools.tool_trades(limit="x", symbol="BTCUSDT")["trades"] == []
    pnl = tools.tool_pnl()
    assert set(pnl) == {"from", "to", "today_pnl", "windows", "period", "totals", "daily"}
    tp = tools.tool_taxas_planilha()
    assert tp["equity"] == 96.5 and tp["faixa_planilha"]["banca"] == "90–150" and tp["faixa_planilha"]["alavancagem"] == 20
    assert tp["taxas"][0]["symbol"] == "ADAUSDT" and tp["taxas"][0]["stale"] is True   # exchange fake sem taxas: fail-open
    cred = tools.tool_creditos()
    assert cred["credit_pct"] == 20.0 and "balance" in cred and "ledger" in cred
    assert ai.AccountTools(bot, app.config["PLATFORM"], 1).tool_creditos() == {}
    docs = tools.tool_docs("créditos pré-pagos ciclo positivo")
    assert docs["trechos"] and docs["trechos"][0]["doc"] == "PLATAFORMA.md" and sum(len(t["trecho"]) for t in docs["trechos"]) <= ai.RESULT_CAP
    assert tools.tool_docs("xyzzy") == {"trechos": []}
    with pytest.raises(LookupError):
        tools.run("pausar", {})


def test_tool_results_are_redacted_and_capped(monkeypatch, tmp_path, on):
    app, client, aid, plain, d = _setup(monkeypatch, tmp_path, ai.FakeProvider([]))
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "MARTINGALE_HEDGE.md").write_text("## Martingale\nconfig: api_key=sk-live-ABCDEF123 token=987654:" + "x" * 40 + "\n"
                                                  + "martingale " * 1000, encoding="utf-8")
    monkeypatch.setattr(ai, "_DOCS", docs_dir)
    monkeypatch.setattr(ai, "DOC_FILES", ("MARTINGALE_HEDGE.md",))
    out = ai.AccountTools(SimpleNamespace(), app.config["PLATFORM"], aid).run("docs", {"topico": "martingale"})
    assert "sk-live-ABCDEF123" not in out and "987654:" not in out and "api_key=<redacted>" in out
    assert len(out) <= ai.RESULT_CAP + 1


def test_loop_caps_tool_calls_and_handles_tool_errors():
    class Tools:
        account_id = 7
        def run(self, name, args):
            if name == "trades":
                raise RuntimeError("db travado token=abc123")
            return '{"ok": true}'
    provider = ai.FakeProvider([_tool_use("estado", uid="a"), _tool_use("trades", uid="b"), _tool_use("pnl", uid="c"),
                                _tool_use("docs", {"topico": "x"}, uid="d"), _tool_use("estado", uid="e")])
    out = ai.Assistant(provider, Tools()).ask([], "oi")
    assert out["tool_calls"] == ["estado", "trades", "pnl", "docs"] and len(provider.calls) == 5
    assert out["reply"].startswith("Não consegui reunir")
    err = provider.calls[2]["messages"][-1]["content"][0]
    assert err["is_error"] is True and "token=<redacted>" in err["content"] and "abc123" not in err["content"]
    assert out["usage"] == {"input_tokens": 1000, "output_tokens": 150}
    refused = ai.Assistant(ai.FakeProvider([SimpleNamespace(stop_reason="refusal", content=[], usage=None)]), Tools()).ask([], "oi")
    assert refused["reply"] == "Não posso ajudar com esse pedido." and refused["tool_calls"] == []

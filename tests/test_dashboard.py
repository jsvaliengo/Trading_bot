"""
Testes do dashboard Flask (Phase 3).

Cobre:
- create_app recusa subir sem credenciais
- /api/healthz é público
- Rotas autenticadas exigem Basic Auth válido
- /api/snapshot retorna estrutura esperada
- /api/control/pause e /resume mutam bot.paused
- /api/control/close_all chama _close_all_positions_daily_target com reason
- collect_summary / collect_positions / collect_regime: estruturas
"""
from __future__ import annotations

import base64
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import re
import requests

from trading_bot.core.config import config as global_config, exchange_label
from trading_bot.web import data as dashboard_data


@pytest.fixture(autouse=True)
def _dashboard_config(monkeypatch, tmp_path):
    """Aplica DASHBOARD_* defaults no config global pra cada teste.

    USE_TESTNET é @property derivada de ENVIRONMENT — monkeypatcham ENVIRONMENT.
    platform.db (users/accounts) vai pro tmp: nunca no runtime/ do repo.
    """
    monkeypatch.setattr(global_config, "PLATFORM_DB_PATH", str(tmp_path / "platform.db"), raising=False)
    monkeypatch.setattr(global_config, "PLATFORM_SECRET", "", raising=False)
    monkeypatch.setattr(global_config, "DASHBOARD_ENABLED", True, raising=False)
    monkeypatch.setattr(global_config, "DASHBOARD_USERNAME", "admin", raising=False)
    monkeypatch.setattr(global_config, "DASHBOARD_PASSWORD", "secret", raising=False)
    monkeypatch.setattr(global_config, "DASHBOARD_SECRET_KEY", "test-secret-key", raising=False)
    monkeypatch.setattr(global_config, "DASHBOARD_HOST", "127.0.0.1", raising=False)
    monkeypatch.setattr(global_config, "DASHBOARD_PORT", 5050, raising=False)
    monkeypatch.setattr(global_config, "DASHBOARD_POLL_INTERVAL_SECONDS", 5, raising=False)
    monkeypatch.setattr(global_config, "ENVIRONMENT", "testnet", raising=False)
    monkeypatch.setattr(global_config, "AI_CONSULTIVE_MODE", "off", raising=False)
    monkeypatch.setattr(global_config, "REGIME_CLASSIFIER_ENABLED", True, raising=False)


# ---------- Bot mock factory ----------


def _make_bot(*, username: str = "admin", password: str = "secret", **overrides) -> SimpleNamespace:
    """Bot mínimo aceito pelo dashboard. Use overrides pra customizar estado."""
    # Aplicar overrides no config global se passados via kwargs
    if username != "admin":
        global_config.DASHBOARD_USERNAME = username
    if password != "secret":
        global_config.DASHBOARD_PASSWORD = password

    bot = SimpleNamespace(
        initial_capital=overrides.get("initial_capital", 100.0),
        last_known_balance=overrides.get("last_known_balance", 110.0),
        total_pnl=overrides.get("total_pnl", 10.0),
        daily_realized_pnl=overrides.get("daily_realized_pnl", 2.5),
        closed_trades_count=overrides.get("closed_trades_count", 3),
        paused=overrides.get("paused", False),
        running=overrides.get("running", True),
        known_positions=overrides.get("known_positions", {}),
        trade_history=overrides.get("trade_history", []),
        portfolio_history=overrides.get("portfolio_history", []),
        _regime_committed=overrides.get("_regime_committed", {}),
        _regime_observations=overrides.get("_regime_observations", {}),
        _positions_lock=None,
        _close_all_positions_daily_target=MagicMock(),
        close_position_manual=MagicMock(return_value=True),
    )
    return bot


def _basic_auth(user: str, pw: str) -> dict:
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def _csrf(client) -> str:
    with client.session_transaction() as s:
        return s.get("csrf", "")


def _login(client, user="admin", pw="secret", next_="/", **extra):
    """GET /login (pega o CSRF da sessão) e POST com as credenciais."""
    client.get("/login")
    data = {"username": user, "password": pw, "csrf": _csrf(client), "next": next_, "remember": "1"}
    return client.post("/login", data=data, **extra)


# ---------- create_app ----------


def test_create_app_refuses_without_username():
    from trading_bot.web.app import create_app
    bot = _make_bot(username="")
    with pytest.raises(RuntimeError, match="DASHBOARD_USERNAME"):
        create_app(bot)


def test_create_app_refuses_without_password():
    from trading_bot.web.app import create_app
    bot = _make_bot(password="")
    with pytest.raises(RuntimeError, match="DASHBOARD_USERNAME"):
        create_app(bot)


def test_create_app_succeeds_with_credentials():
    from trading_bot.web.app import create_app
    bot = _make_bot()
    app, socketio = create_app(bot)
    assert app is not None
    assert socketio is not None
    assert app.config["DASHBOARD_USERNAME"] == "admin"


# ---------- Plataforma: users/accounts ----------


def test_platform_seed_from_env_is_idempotent_and_hashes_password(tmp_path):
    from trading_bot.web.platform import PlatformStore
    monkeypatch_cfg = SimpleNamespace(DASHBOARD_USERNAME="admin", DASHBOARD_PASSWORD="secret", EXCHANGE="bitget",
                                      BITGET_API_KEY="k1", BITGET_API_SECRET="s1", BITGET_PASSPHRASE="p1")
    store = PlatformStore(str(tmp_path / "p.db"), secret="segredo")
    first = store.seed_from_env(monkeypatch_cfg)
    assert first == {"user_id": 1, "account_id": 1}
    assert store.seed_from_env(monkeypatch_cfg) == first   # 2º boot: nada duplicado
    user = store.user_by_name("admin")
    assert user["password_hash"] != "secret" and user["password_hash"].startswith(("scrypt:", "pbkdf2:"))
    assert store.verify_login("admin", "secret")["id"] == 1 and store.verify_login("admin", "errada") is None
    assert store.verify_login("ninguem", "secret") is None
    acc = store.account(1)
    assert acc["user_id"] == 1 and acc["exchange"] == "bitget" and "api_key" not in acc
    # chaves cifradas no disco, decifradas só por account_credentials com o mesmo segredo
    raw = store._conn.execute("SELECT api_key_enc, passphrase_enc FROM accounts WHERE id = 1").fetchone()
    # texto cifrado é base64 aleatório: checar substring ("k1" in ...) era flaky (CI de 4b1fc16)
    assert raw["api_key_enc"].startswith("gAAAA") and raw["passphrase_enc"].startswith("gAAAA")
    assert store.account_credentials(1) == {"api_key": "k1", "api_secret": "s1", "passphrase": "p1"}
    other = PlatformStore(str(tmp_path / "p.db"), secret="outro-segredo")
    assert other.account_credentials(1) == {"api_key": "", "api_secret": "", "passphrase": ""}


def test_platform_seed_without_secret_keeps_account_but_no_keys(tmp_path):
    from trading_bot.web.platform import PlatformStore
    cfg = SimpleNamespace(DASHBOARD_USERNAME="admin", DASHBOARD_PASSWORD="secret", EXCHANGE="bitget",
                          BITGET_API_KEY="k1", BITGET_API_SECRET="s1", BITGET_PASSPHRASE="p1")
    store = PlatformStore(str(tmp_path / "p.db"), secret="")
    assert store.seed_from_env(cfg)["account_id"] == 1
    assert store.account_credentials(1) == {"api_key": "", "api_secret": "", "passphrase": ""}
    with pytest.raises(RuntimeError):
        store.encrypt("x")


def test_login_uses_users_table_and_env_only_as_fallback():
    """Senha trocada na tabela vale no login; o .env deixa de ser segunda chance para quem existe lá.
    Usuário que NÃO existe na tabela ainda entra pelo .env (compatibilidade)."""
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    store = app.config["PLATFORM"]
    client = app.test_client()
    assert _login(client).status_code == 302
    with client.session_transaction() as s:
        assert s["user_id"] == 1 and s["username"] == "admin"
    client.get("/")   # renova o CSRF da sessão (o login limpa a sessão) antes do logout
    client.post("/logout", data={"csrf": _csrf(client)})
    with store._lock, store._conn:
        from werkzeug.security import generate_password_hash
        store._conn.execute("UPDATE users SET password_hash = ? WHERE id = 1", (generate_password_hash("nova"),))
    assert client.get("/", headers=_basic_auth("admin", "secret")).status_code == 302   # fora de /api/* nunca
    assert client.get("/api/summary", headers=_basic_auth("admin", "secret")).status_code == 401
    assert client.get("/api/summary", headers=_basic_auth("admin", "nova")).status_code == 200
    r = _login(client, pw="secret")
    assert r.status_code == 200 and "inválidos" in r.data.decode()
    assert _login(client, pw="nova").status_code == 302


def test_api_routes_resolve_account_of_logged_user():
    """Rotas de dados recebem ?account=N: default = 1ª conta do usuário; conta de outro usuário,
    inexistente ou sem bot neste processo → 404; valor inválido → 400. /api/me lista as contas."""
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    _login(client)
    me = client.get("/api/me").get_json()
    assert me["user"]["username"] == "admin" and [a["id"] for a in me["accounts"]] == [1]
    assert me["accounts"][0]["exchange"] and "api_key" not in me["accounts"][0]
    assert client.get("/api/live").status_code == 200
    assert client.get("/api/live?account=1").status_code == 200
    assert client.get("/api/history?account=2").status_code == 404
    assert client.get("/api/live?account=x").status_code == 400
    assert client.post("/api/control/pause?account=2").status_code == 404
    store = app.config["PLATFORM"]
    uid = store.create_user("outro", "senha2")
    acc = store.create_account(uid, "bitget", "conta do outro")
    assert client.get(f"/api/summary?account={acc}").status_code == 404   # conta existe, mas não é do admin
    client.get("/")   # renova o CSRF da sessão antes do logout
    client.post("/logout", data={"csrf": _csrf(client)})
    assert client.get("/").status_code == 302
    _login(client, user="outro", pw="senha2")
    assert client.get("/api/live").status_code == 404   # conta do outro não tem bot neste processo
    assert client.get("/api/me").get_json()["accounts"][0]["label"] == "conta do outro"


def test_index_shows_logged_user_and_platform_placeholders():
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    _login(client)
    html = client.get("/").data.decode()
    assert 'id="profile"' in html and ">admin<" in html and "1 conta(s)" in html
    nav = html.split('<nav class="nav" id="nav">')[1].split("</nav>")[0]
    assert 'href="#accounts"' in nav and 'href="#users"' in nav and nav.count('aria-disabled="true"') == 0
    assert 'data-page="users"' in html and 'id="profile-btn"' in html and 'data-role="admin"' in html


# ---------- Plataforma › Usuários (admin) e Meu perfil ----------


def _post(client, path, body):
    """POST JSON com o CSRF da sessão no header (o token nasce no render da página, como no browser)."""
    if not _csrf(client):
        client.get("/")
    return client.post(path, json=body, headers={"X-CSRF-Token": _csrf(client)})


def test_platform_migration_adds_role_and_active_and_user_1_is_admin(tmp_path):
    """Banco da fase 1 (sem role/active) ganha as colunas no boot; usuário 1 vira admin; login recusa inativo."""
    import sqlite3
    from trading_bot.web.platform import PlatformStore
    from werkzeug.security import generate_password_hash
    db = tmp_path / "p.db"
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,"
                      " display_name TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);")
    con.execute("INSERT INTO users VALUES (1, 'admin', ?, 'admin', '2026-01-01')", (generate_password_hash("secret"),))
    con.commit()
    con.close()
    store = PlatformStore(str(db), secret="s")
    assert store.user(1)["role"] == "admin" and store.user(1)["active"] == 1
    uid = store.create_user("maria", "senha-forte", role="user")
    assert store.verify_login("maria", "senha-forte")["role"] == "user"
    store.set_active(uid, False)
    assert store.verify_login("maria", "senha-forte") is None   # desativado não loga
    store.set_active(uid, True)
    store.set_role(uid, "admin")
    assert store.verify_login("maria", "senha-forte")["role"] == "admin"
    with pytest.raises(ValueError):
        store.set_role(uid, "root")
    assert [u["username"] for u in store.users()] == ["admin", "maria"] and store.users()[0]["accounts"] == 0
    assert PlatformStore(str(db), secret="s").user(1)["role"] == "admin"   # 2º boot: migração idempotente


def test_users_api_admin_only_csrf_and_self_protection():
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    _login(client)
    # CSRF obrigatório nos POSTs da plataforma
    assert client.post("/api/users", json={"username": "x"}).status_code == 400
    r = _post(client, "/api/users", {"username": "maria", "display_name": "Maria", "role": "user"})
    assert r.status_code == 201 and r.get_json()["user"]["role"] == "user"
    generated = r.get_json()["password"]
    assert generated and len(generated) >= 8
    assert _post(client, "/api/users", {"username": "maria"}).status_code == 409
    assert _post(client, "/api/users", {"username": "a b"}).status_code == 400
    assert _post(client, "/api/users", {"username": "joao", "password": "curta"}).status_code == 400
    users = client.get("/api/users").get_json()["users"]
    assert [u["username"] for u in users] == ["admin", "maria"] and "password_hash" not in users[0]
    uid = users[1]["id"]
    # admin não mexe no próprio papel/status
    assert _post(client, "/api/users/1/active", {"active": False}).status_code == 400
    assert _post(client, "/api/users/1/role", {"role": "user"}).status_code == 400
    assert _post(client, f"/api/users/{uid}/role", {"role": "admin"}).get_json()["user"]["role"] == "admin"
    assert _post(client, f"/api/users/{uid}/role", {"role": "user"}).status_code == 200
    assert _post(client, "/api/users/999/password", {}).status_code == 404
    # senha definida pelo admin não volta na resposta; gerada volta uma vez
    assert _post(client, f"/api/users/{uid}/password", {"password": "nova-senha-1"}).get_json()["password"] is None
    client.get("/")
    client.post("/logout", data={"csrf": _csrf(client)})
    assert _login(client, user="maria", pw=generated).status_code == 200   # senha antiga já não vale
    assert _login(client, user="maria", pw="nova-senha-1").status_code == 302
    # user comum: sem menu Usuários, 403 na API, página não existe no HTML
    html = client.get("/").data.decode()
    assert 'href="#users"' not in html and 'data-page="users"' not in html and 'data-role="user"' in html
    assert client.get("/api/users").status_code == 403
    assert _post(client, "/api/users", {"username": "z"}).status_code == 403
    # meu perfil: senha atual confere, nova 2x
    assert _post(client, "/api/me/password", {"current": "errada", "new": "outra-senha", "again": "outra-senha"}).status_code == 403
    assert _post(client, "/api/me/password", {"current": "nova-senha-1", "new": "outra-senha", "again": "diferente"}).status_code == 400
    assert _post(client, "/api/me/password", {"current": "nova-senha-1", "new": "outra-senha", "again": "outra-senha"}).status_code == 200
    client.get("/")
    client.post("/logout", data={"csrf": _csrf(client)})
    assert _login(client, user="maria", pw="outra-senha").status_code == 302


def test_deactivated_user_loses_open_session_and_cannot_login():
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    store = app.config["PLATFORM"]
    uid = store.create_user("maria", "senha-forte")
    admin, maria = app.test_client(), app.test_client()
    _login(admin)
    _login(maria, user="maria", pw="senha-forte")
    assert maria.get("/api/me").status_code == 200
    assert _post(admin, f"/api/users/{uid}/active", {"active": False}).status_code == 200
    assert maria.get("/api/me").status_code == 401            # sessão aberta cai na hora
    assert maria.get("/").status_code == 302
    r = _login(maria, user="maria", pw="senha-forte")
    assert r.status_code == 200 and b"inv" in r.data           # não loga mais
    assert maria.get("/api/me", headers=_basic_auth("maria", "senha-forte")).status_code == 401


def test_login_rate_limit_per_username_too():
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    for _ in range(5):
        _login(client, pw="errada", environ_base={"REMOTE_ADDR": f"10.0.0.{_}"})
    r = _login(client, pw="secret", environ_base={"REMOTE_ADDR": "10.0.0.99"})
    assert r.status_code == 429   # IPs diferentes, mesmo usuário: bloqueado


# ---------- Relatórios › Histórico de fills (endpoint privado da conta, com cache) ----------

_FILLS = [
    {"time": 1795255260000, "symbol": "ADAUSDT", "side": "buy", "action": "open", "position_side": "LONG", "price": 0.1428,
     "qty": 130.0, "notional": 18.564, "fee": -0.0111, "fee_coin": "USDT", "profit": 0.0, "role": "taker", "order_id": "o1", "trade_id": "t1"},
    {"time": 1795258860000, "symbol": "ADAUSDT", "side": "sell", "action": "close", "position_side": "LONG", "price": 0.1442,
     "qty": 130.0, "notional": 18.746, "fee": -0.0112, "fee_coin": "USDT", "profit": 0.182, "role": "taker", "order_id": "o2", "trade_id": "t2"},
    {"time": 1795262400000, "symbol": "BNBUSDT", "side": "sell", "action": "open", "position_side": "SHORT", "price": 612.0,
     "qty": 0.02, "notional": 12.24, "fee": -0.0073, "fee_coin": "USDT", "profit": 0.0, "role": "maker", "order_id": "o3", "trade_id": "t3"},
]


def _bot_with_fills(tmp_path, fills=None):
    from trading_bot.core.trade_store import TradeStore
    store = TradeStore(str(tmp_path / "f.db"))
    # trade do bot aberto 20 s antes do fill de abertura e fechado 30 s depois do fill de fechamento → os dois batem
    store.record_open({"timestamp": "2026-11-21T10:00:40+00:00", "symbol": "ADAUSDT", "signal": "BUY", "side": "LONG", "qty": 130.0,
                       "value": 18.56, "entry_price": 0.1428, "stop_loss": None, "take_profit": None, "strategy_name": "primary",
                       "strategy_type": "martingale_hedge", "double_first": False, "ai_consultive": {}})
    store.record_close(symbol="ADAUSDT", side="LONG", entry_price=0.1428, exit_price=0.1442, exit_at="2026-11-21T11:01:30+00:00",
                       pnl_gross=0.182, pnl_net=0.16, fees=0.022, close_reason="tp", strategy_name="primary")
    bot = _make_bot()
    bot.trade_store = store
    calls = []

    def get_fills(symbol=None, start_ms=None, end_ms=None, limit=500):
        calls.append({"symbol": symbol, "start_ms": start_ms, "end_ms": end_ms, "limit": limit})
        return [f for f in (fills if fills is not None else _FILLS) if not symbol or f["symbol"] == symbol]

    bot.exchange = SimpleNamespace(get_fills=get_fills, get_open_positions=lambda: [])
    return bot, calls


@pytest.fixture(autouse=True)
def _fills_cache_clear(monkeypatch):
    from trading_bot.web import fills
    monkeypatch.setattr(fills, "_cache", {})


def test_fills_route_matches_bot_trades_filters_and_paginates(tmp_path):
    from trading_bot.web.app import create_app
    bot, calls = _bot_with_fills(tmp_path)
    app, _ = create_app(bot)
    client = app.test_client()
    assert client.get("/api/fills").status_code == 401
    _login(client)
    d = client.get("/api/fills?from=2026-11-20&to=2026-11-21").get_json()
    assert d["total"] == 3 and [f["trade_id"] for f in d["fills"]] == ["t3", "t2", "t1"]   # mais recente primeiro
    assert d["matched"] == 2 and {f["trade_id"]: f["bot_trade_id"] for f in d["fills"]} == {"t3": None, "t2": 1, "t1": 1}
    assert d["symbols"] == ["ADAUSDT", "BNBUSDT"] and d["fee_total"] == pytest.approx(-0.0296) and d["profit_total"] == pytest.approx(0.182)
    assert d["from"] == "2026-11-20" and d["to"] == "2026-11-21"
    assert calls[0]["start_ms"] == 1795132800000 and calls[0]["end_ms"] == 1795305599999 and calls[0]["symbol"] is None
    # filtros: lado e par (o par vai pra exchange; o lado filtra aqui)
    assert [f["trade_id"] for f in client.get("/api/fills?from=2026-11-20&to=2026-11-21&side=SHORT").get_json()["fills"]] == ["t3"]
    assert client.get("/api/fills?from=2026-11-20&to=2026-11-21&symbol=adausdt").get_json()["total"] == 2 and calls[-1]["symbol"] == "ADAUSDT"
    assert client.get("/api/fills?side=UP").status_code == 400 and client.get("/api/fills?page=x").status_code == 400
    # ordenação por coluna (lista completa, antes de paginar); empate → mais recente primeiro; None por último
    assert [f["trade_id"] for f in client.get("/api/fills?from=2026-11-20&to=2026-11-21&sort=profit&dir=desc").get_json()["fills"]] == ["t2", "t3", "t1"]
    assert [f["trade_id"] for f in client.get("/api/fills?from=2026-11-20&to=2026-11-21&sort=symbol&dir=asc").get_json()["fills"]] == ["t2", "t1", "t3"]
    assert [f["bot_trade_id"] for f in client.get("/api/fills?from=2026-11-20&to=2026-11-21&sort=bot_trade_id&dir=asc").get_json()["fills"]] == [1, 1, None]
    assert client.get("/api/fills?sort=order_id").status_code == 400 and client.get("/api/fills?dir=up").status_code == 400
    # paginação
    p = client.get("/api/fills?from=2026-11-20&to=2026-11-21&per_page=2&page=2").get_json()
    assert p["pages"] == 2 and p["page"] == 2 and [f["trade_id"] for f in p["fills"]] == ["t1"]
    # cache: mesma consulta não volta na exchange
    n = len(calls)
    client.get("/api/fills?from=2026-11-20&to=2026-11-21")
    assert len(calls) == n


def test_match_bot_trades_covers_leg_lifetime_partial_closes_and_open_leg():
    from trading_bot.web.fills import match_bot_trades
    h = 3600_000
    # trade #7: perna XRP SHORT de 10:00 a 12:00 (exit gravado 40 s depois do último fill); trade #9: perna ainda aberta desde 13:00
    trades = [{"id": 7, "symbol": "XRPUSDT", "side": "SHORT", "opened_at": "2026-11-25T10:00:00+00:00", "exit_at": "2026-11-25T12:00:40+00:00", "qty": 1207.0},
              {"id": 9, "symbol": "XRPUSDT", "side": "SHORT", "opened_at": "2026-11-25T13:00:00+00:00", "exit_at": None, "qty": 11.0}]
    t0 = 1795600800000   # 2026-11-25T10:00:00Z
    def mk(ms, action, qty, side="SHORT", sym="XRPUSDT"):
        return {"time": ms, "symbol": sym, "position_side": side, "action": action, "qty": qty}
    fills = [mk(t0 + 5000, "open", 11.0),                  # abre a base
             mk(t0 + h, "open", 22.0),                     # adiciona nível 1 no meio da perna
             mk(t0 + 2 * h, "close", 816.0),               # fechamento em 2 fills parciais da mesma ordem
             mk(t0 + 2 * h + 1000, "close", 391.0),
             mk(t0 + 2 * h + 1000, "close", 392.0, side="LONG"),   # outro lado: nada do bot
             mk(t0 + 3 * h + 2000, "open", 11.0),          # perna #9 ainda aberta
             mk(t0 + 2 * h + 30 * 60_000, "open", 11.0)]   # entre as pernas: manual / fora do bot
    match_bot_trades(fills, trades)
    assert [f["bot_trade_id"] for f in fills] == [7, 7, 7, 7, None, 9, None]
    # qty com 1 unidade de diferença em relação ao trade gravado também bate (a regra é a vida da perna, não a qty)
    one_off = [mk(t0 + 2 * h, "close", 70.0, sym="ADAUSDT")]
    match_bot_trades(one_off, [{"id": 3, "symbol": "ADAUSDT", "side": "SHORT", "opened_at": "2026-11-25T11:00:00+00:00", "exit_at": "2026-11-25T12:00:10+00:00", "qty": 69.0}])
    assert one_off[0]["bot_trade_id"] == 3


def test_fills_csv_and_exchange_without_fills(tmp_path):
    from trading_bot.web.app import create_app
    bot, _ = _bot_with_fills(tmp_path)
    app, _ = create_app(bot)
    client = app.test_client()
    _login(client)
    r = client.get("/api/fills?from=2026-11-20&to=2026-11-21&format=csv&per_page=1")
    assert r.status_code == 200 and r.mimetype == "text/csv" and "attachment" in r.headers["Content-Disposition"]
    lines = r.data.decode().splitlines()
    assert lines[0].startswith("time,symbol,position_side,action,side,price,qty") and len(lines) == 4   # CSV traz tudo, não só a página
    assert lines[1].startswith("2026-11-21T12:00:00+00:00,BNBUSDT,SHORT,open,sell,612.0,0.02") and lines[2].endswith(",o2,t2,1")
    bot.exchange = SimpleNamespace(get_open_positions=lambda: [])
    assert client.get("/api/fills").status_code == 501


def test_bitget_get_fills_paginates_and_normalizes(monkeypatch):
    import tests.test_bitget_client as tb
    conn = tb._make(monkeypatch)
    pages = {None: {"fillList": [{"tradeId": "t%d" % i, "orderId": "o%d" % i, "symbol": "ADAUSDT", "price": "0.14", "baseVolume": "10",
                                  "quoteVolume": "1.4", "side": "buy", "tradeSide": "open", "profit": "0", "tradeScope": "taker",
                                  "feeDetail": [{"feeCoin": "USDT", "totalFee": "-0.00084"}], "cTime": str(1795255260000 - i)} for i in range(100)],
                    "endId": "t99"},
             "t99": {"fillList": [{"tradeId": "t100", "orderId": "o100", "symbol": "ADAUSDT", "price": "0.15", "baseVolume": "10",
                                   "quoteVolume": "", "side": "sell", "tradeSide": "close", "profit": "0.1", "tradeScope": "maker",
                                   "feeDetail": [{"feeCoin": "BGB", "totalFee": "-0.001"}, {"feeCoin": "USDT", "totalFee": "-0.0002"}],
                                   "cTime": "1795255000000"}], "endId": "t100"}}
    conn.http.routes[("GET", "/api/v2/mix/order/fills")] = lambda q, b: {"code": "00000", "data": pages[q.get("idLessThan")]}
    out = conn.get_fills(symbol="ADAUSDT", start_ms=1, end_ms=2, limit=500)
    assert len(out) == 101 and conn.http.calls[-2]["query"]["symbol"] == "ADAUSDT" and conn.http.calls[-1]["query"]["idLessThan"] == "t99"
    assert conn.http.calls[-2]["query"]["startTime"] == "1" and "ACCESS-KEY" in conn.http.calls[-1]["headers"]   # privado: assinado
    f0, f100 = out[0], out[100]
    assert f0 == {"time": 1795255260000, "symbol": "ADAUSDT", "side": "buy", "action": "open", "position_side": "LONG", "price": 0.14,
                  "qty": 10.0, "notional": 1.4, "fee": pytest.approx(-0.00084), "fee_coin": "USDT", "profit": 0.0, "role": "taker",
                  "order_id": "o0", "trade_id": "t0"}
    assert f100["action"] == "close" and f100["position_side"] == "LONG" and f100["notional"] == pytest.approx(1.5)   # sell+close = fecha LONG; sem quoteVolume → qty×preço
    assert f100["fee"] == pytest.approx(-0.0012) and f100["fee_coin"] == "BGB" and f100["profit"] == 0.1 and f100["role"] == "maker"
    # 2ª chamada com limite menor pára na 1ª página
    assert len(conn.get_fills(limit=50)) == 50


def test_fills_page_and_menu_item_are_wired(tmp_path):
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    _login(client)
    html = client.get("/").data.decode()
    assert re.search(r'<a class="nav-item" href="#fills" data-page="fills" title="Histórico de fills">.*?<span>Histórico de fills</span></a>', html)
    for i in ("fills-table", "fills-filters", "fl-symbol", "fl-side", "fl-from", "fl-to", "fills-csv", "fills-pager"):
        assert f'id="{i}"' in html, i
    js = open("trading_bot/web/static/js/main.js", encoding="utf-8").read()
    assert "function fetchFills(" in js and "fillsVisible(name === 'fills')" in js and "format: 'csv'" in js


# ---------- Mercado › BTC (API pública da Bitget, com cache) ----------

_MK_ROUTES = {
    "/api/v2/mix/market/candles": [["1795258800000", "57905.3", "58625.4", "57834.2", "58184.1", "2204.9", "1.28e8"],
                                   ["1795255200000", "57800.0", "57950.0", "57700.0", "57905.3", "1800.0", "1.0e8"]],
    "/api/v2/mix/market/ticker": [{"lastPr": "58314.6", "markPrice": "58314.6", "indexPrice": "58337.02", "change24h": "-0.05099",
                                   "high24h": "62150", "low24h": "55098.5", "usdtVolume": "6389433244.35", "baseVolume": "109476.43",
                                   "fundingRate": "-0.000021", "holdingAmount": "33826.58"}],
    "/api/v2/mix/market/current-fund-rate": [{"fundingRate": "-0.000021", "fundingRateInterval": "8", "nextUpdate": "1795276800000"}],
    "/api/v2/mix/market/history-fund-rate": [{"fundingRate": "-0.000029", "fundingTime": "1795248000000"}, {"fundingRate": "0.000010", "fundingTime": "1795219200000"}],
    "/api/v2/mix/market/open-interest": {"openInterestList": [{"symbol": "BTCUSDT", "size": "33826.58"}], "ts": "1795263440061"},
    "/api/v2/mix/market/account-long-short": [{"longAccountRatio": "0.8136", "shortAccountRatio": "0.1864", "longShortAccountRatio": "4.3648", "ts": "1795158000000"}],
    "/api/v2/mix/market/position-long-short": [{"longPositionRatio": "0.4976", "shortPositionRatio": "0.5024", "longShortPositionRatio": "0.9904", "ts": "1795158000000"}],
}


@pytest.fixture
def _bitget_public(monkeypatch):
    """requests.get do módulo market responde com fixtures (nada de rede); conta as chamadas por path."""
    from trading_bot.web import market
    calls = []

    def fake_get(url, params=None, timeout=None):
        path = url.replace(market.BASE_URL, "")
        calls.append((path, dict(params or {})))
        if path not in _MK_ROUTES:
            return SimpleNamespace(json=lambda: {"code": "40404", "msg": "Request URL NOT FOUND", "data": None})
        return SimpleNamespace(json=lambda: {"code": "00000", "msg": "success", "data": _MK_ROUTES[path]})

    monkeypatch.setattr(market.requests, "get", fake_get)
    monkeypatch.setattr(market, "_cache", {})
    return calls


def test_market_btc_route_parses_public_bitget_payloads(_bitget_public):
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    assert client.get("/api/market/BTCUSDT").status_code == 401   # exige login/Basic como o resto de /api
    _login(client)
    assert client.get("/api/market/btc").status_code == 400 and client.get("/api/market/ADA-USDT").status_code == 400
    assert client.get("/api/market/ETHBTC").status_code == 400   # só perpétuos USDT-M
    d = client.get("/api/market/BTCUSDT?interval=4h").get_json()
    assert d["symbol"] == "BTCUSDT" and d["interval"] == "4h" and [c["t"] for c in d["candles"]] == [1795255200, 1795258800]   # ascendente, em segundos
    assert d["candles"][1] == {"t": 1795258800, "o": 57905.3, "h": 58625.4, "l": 57834.2, "c": 58184.1, "v": 2204.9}
    t = d["ticker"]
    assert t["last"] == 58314.6 and t["change_24h_pct"] == pytest.approx(-5.099) and t["open_interest_btc"] == 33826.58
    assert t["open_interest_usdt"] == pytest.approx(33826.58 * 58314.6) and t["basis_pct"] == pytest.approx((58314.6 - 58337.02) / 58337.02 * 100)
    f = d["funding"]
    assert f["rate_pct"] == pytest.approx(-0.0021) and f["interval_hours"] == 8 and f["next_at"].startswith("2026-11-21T16:00:00")
    assert [h["rate_pct"] for h in f["history"]] == pytest.approx([0.001, -0.0029])   # cronológico
    ls = d["long_short"]
    assert ls["accounts_long_pct"] == pytest.approx(81.36) and ls["positions_ratio"] == 0.9904 and len(ls["history"]) == 1
    paths = [p for p, _ in _bitget_public]
    assert paths[0] == "/api/v2/mix/market/candles" and _bitget_public[0][1] == {"symbol": "BTCUSDT", "productType": "USDT-FUTURES", "granularity": "4H", "limit": 200}
    assert all("ACCESS-KEY" not in str(c) for c in _bitget_public)   # público: sem assinatura


def test_market_caches_per_symbol_and_interval(_bitget_public):
    from trading_bot.web import market
    a = market.fetch_market("ADAUSDT", "1h")
    n = len(_bitget_public)
    assert a["symbol"] == "ADAUSDT" and all(p[1].get("symbol") == "ADAUSDT" for p in _bitget_public)
    assert market.fetch_market("adausdt", "1h") is a and len(_bitget_public) == n           # cache (símbolo normalizado)
    assert market.fetch_market("ADAUSDT", "xx")["interval"] == "1h" and len(_bitget_public) == n   # intervalo inválido → 1h
    market.fetch_market("ADAUSDT", "1m")
    m = len(_bitget_public)
    assert m > n                                                                             # outro timeframe busca de novo
    market.fetch_market("BTCUSDT", "1h")
    assert len(_bitget_public) > m                                                           # outro par busca de novo
    with pytest.raises(ValueError):
        market.fetch_market("BTC", "1h")


def test_market_btc_optional_parts_fail_soft_and_candles_fail_hard(_bitget_public, monkeypatch):
    from trading_bot.web import market
    from trading_bot.web.app import create_app
    monkeypatch.setitem(_MK_ROUTES, "/api/v2/mix/market/account-long-short", None)
    monkeypatch.delitem(_MK_ROUTES, "/api/v2/mix/market/position-long-short")
    d = market.fetch_market("BTCUSDT", "1h")
    assert d["long_short"] is None and d["ticker"]["last"] == 58314.6
    monkeypatch.delitem(_MK_ROUTES, "/api/v2/mix/market/candles")
    monkeypatch.setattr(market, "_cache", {})
    app, _ = create_app(_make_bot())
    client = app.test_client()
    _login(client)
    r = client.get("/api/market/BTCUSDT")
    assert r.status_code == 502 and r.get_json()["ok"] is False


def test_market_page_and_menu_item_are_wired():
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    _login(client)
    html = client.get("/").data.decode()
    assert re.search(r'<a class="nav-item" href="#market" data-page="market" title="Par ativo">.*?<span>Par ativo</span></a>', html)
    for i in ("mk-pairs", "mk-chart", "mk-tf", "mk-funding-chart", "mk-ls-chart", "mk-last", "mk-mark", "mk-funding", "mk-oi", "mk-ls"):
        assert f'id="{i}"' in html, i
    assert html.count("lightweight-charts@") == 1   # a mesma lib do modal 1m, carregada uma vez
    js = open("trading_bot/web/static/js/main.js", encoding="utf-8").read()
    # mesmo gráfico do modal 1m: buildChart/chartFill/chartLevels (velas + volume + EMAs + linhas das pernas)
    assert "function buildChart(" in js and js.count("buildChart(") == 3 and "chartLevels(mk.g, mk.sym)" in js
    assert "function renderMarket(" in js and "/api/market/${encodeURIComponent(mk.sym)}?interval=" in js and "marketVisible(name === 'market')" in js
    # chips de par: pares ativos do martingale (mesma fonte do anel) + BTC de referência; padrão = 1º ativo
    assert "function renderMarketPairs(" in js and "[...active, 'BTCUSDT']" in js and "renderMarketPairs(l);" in js


# ---------- Rotas HTTP ----------


def test_healthz_is_public():
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    r = client.get("/api/healthz")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True}


def test_snapshot_requires_auth():
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    r = client.get("/api/snapshot")
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


def test_snapshot_rejects_wrong_password():
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    r = client.get("/api/snapshot", headers=_basic_auth("admin", "wrong"))
    assert r.status_code == 401


def test_snapshot_accepts_valid_auth():
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    r = client.get("/api/snapshot", headers=_basic_auth("admin", "secret"))
    assert r.status_code == 200
    payload = r.get_json()
    assert "summary" in payload
    assert "positions" in payload
    assert "recent_trades" in payload
    assert "regime" in payload
    assert "portfolio_history" in payload
    assert payload["tz_offset_hours"] == float(global_config.DAILY_PNL_TZ_OFFSET_HOURS or 0.0)


def test_index_requires_auth_returns_html():
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    r = client.get("/")
    assert r.status_code == 302 and r.headers["Location"] == "/login?next=/"
    # Basic Auth não vale fora de /api/*: a página continua mandando pro login.
    assert client.get("/", headers=_basic_auth("admin", "secret")).status_code == 302
    assert _login(client).status_code == 302
    r = client.get("/")
    assert r.status_code == 200
    assert b"Forward test" in r.data
    assert b"socket.io" in r.data.lower() or b"socket.io" in r.data
    # Topo: relógio (main.js atualiza a cada segundo), toggle de tema e Sair (POST com CSRF).
    assert b'id="clock"' in r.data and b'id="theme-toggle"' in r.data
    assert b'action="/logout"' in r.data and b'name="csrf"' in r.data
    # Versão na URL dos assets: sem isso o navegador usa JS antigo após deploy.
    assert b"/static/js/main.js?v=" in r.data and b"/static/css/style.css?v=" in r.data


def test_static_version_vem_do_deploy_sha_lido_uma_vez_no_boot(monkeypatch, tmp_path):
    """HTML e assets têm que ter a mesma versão pra um processo: rsync troca o main.js
    ~10s antes do restart e o mtime por request fazia o template antigo puxar o JS novo."""
    from trading_bot.web import data as dash
    from trading_bot.web.app import create_app
    monkeypatch.setattr(global_config, "PROJECT_ROOT", str(tmp_path))
    (tmp_path / "runtime").mkdir()
    info = tmp_path / "runtime" / "deploy_info.json"
    info.write_text('{"deploy_sha": "b8740eb1234567890abc"}')
    dash._deploy_cache.update({"mtime": None, "data": {}})
    app, _ = create_app(_make_bot())
    assert app.config["STATIC_VERSION"] == "b8740eb12345"
    client = app.test_client()
    _login(client)
    assert b"/static/js/main.js?v=b8740eb12345" in client.get("/").data
    # Deploy novo no disco com o processo vivo: a versão NÃO muda até o restart.
    info.write_text('{"deploy_sha": "ffff00001111"}')
    dash._deploy_cache.update({"mtime": None, "data": {}})
    assert b"/static/js/main.js?v=b8740eb12345" in client.get("/").data

    # Sem deploy_info (dev/testes): versão estável e não vazia.
    info.unlink()
    dash._deploy_cache.update({"mtime": None, "data": {}})
    app2, _ = create_app(_make_bot())
    v = app2.config["STATIC_VERSION"]
    assert v and v == create_app(_make_bot())[0].config["STATIC_VERSION"]


def test_martingale_dashboard_is_ring_legs_sheet_pnl_health():
    """Sem card "Escada martingale"; no martingale o main.js esconde ft/pos/rdist/calendário (Equity ×
    Performance ficam) e move a linha de tiles pra dentro do anel (tiles Equity, Não realizado e Realizado hoje
    somem: saldo no centro do anel, P&L nos KPIs do topo)."""
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    _login(client)
    html = client.get("/").data.decode()
    assert "mg-card" not in html and "mg-params" not in html and "Escada martingale" not in html
    for i in ("status", "tile-equity", "tile-unreal", "tile-today", "pos-card", "eqperf-row", "rdist-row", "legs-sum"):
        assert f'id="{i}"' in html, i
    assert html.index('id="eqperf-row"') < html.index('id="eq-card"') < html.index('id="perf-card"') < html.index("</section>", html.index('id="eqperf-row"'))
    assert html.index('id="rdist-row"') < html.index('id="cal-card"') < html.index("</section>", html.index('id="rdist-row"'))
    js = open("trading_bot/web/static/js/main.js", encoding="utf-8").read()
    assert "'tradingbot-open:' + d.id" in js
    # Não realizado/Realizado hoje somem no martingale (duplicam os KPIs do topo): ficam 4 tiles, 2×2 ao lado do anel
    assert "$('ft-card').hidden = $('pos-card').hidden = $('tile-equity').hidden = $('tile-unreal').hidden = $('tile-today').hidden = $('rdist-row').hidden = !!m" in js
    css = open("trading_bot/web/static/css/shell.css", encoding="utf-8").read()
    assert ".ring-card .status { flex:none; display:grid; grid-template-columns:repeat(2, minmax(0,1fr)); grid-auto-rows:1fr;" in css
    assert "$('eqperf-row').hidden" not in js   # Equity × Performance aparecem também no martingale
    assert "rc.appendChild(st)" in js and "$('mg-list').addEventListener('click', onClose)" in js
    # pernas como cards: cabeçalho, escada em barra, régua com rótulos ancorados, grade de métricas; perna vazia compacta
    for marker in ('class="leg-h"', 'class="leg-ladder"', 'class="ruler-anchors"', 'class="leg-grid"', 'class="leg wait"'):
        assert marker in js, marker
    assert "WebSocket de klines" not in js


def test_collect_health_has_no_klines_ws_line():
    """A linha "WebSocket de klines" saiu do card Saúde: o payload não traz mais `ws` e o badge
    "tudo ok" não depende dela (get_ws_stats da exchange nem é consultado)."""
    bot = _make_bot()
    bot.exchange = SimpleNamespace(get_ws_stats=lambda: (_ for _ in ()).throw(AssertionError("não devia consultar")),
                                   get_user_stream_stats=lambda: {"started": True, "error_count": 0})
    h = dashboard_data.collect_health(bot, {"paused": False})
    assert "ws" not in h and h["user_stream"]["started"] is True


def test_ring_card_badges_and_legs_list():
    """Anel da carteira (SVG com gradientes + badges por par) sempre aberto no topo; pernas e ações em "Pernas ativas"."""
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    _login(client)
    html = client.get("/").data.decode()
    ring = html.split('id="ring-card"')[1].split("</section>")[0]
    assert html.index('id="ring-card"') < html.index('class="status"')
    assert '<svg class="ring"' in ring and 'id="rg4"' in ring and 'id="ring-segs"' in ring
    assert 'id="ring-badges"' in ring and 'id="ring-center"' in ring and 'id="ring-state"' in ring
    legs = html.split('id="legs-card"')[1].split("</section>")[0]
    assert "Posições abertas" in legs and 'id="legs-sum"' in legs and 'id="mg-list"' in legs
    assert 'id="legs-card"' in html.split('<div class="page" data-page="overview"')[1].split('<div class="page"')[0]
    for b in ("btn-pause", "btn-resume", "btn-close-all"):
        assert f'id="{b}"' in legs and html.count(f'id="{b}"') == 1
    js = open("trading_bot/web/static/js/main.js", encoding="utf-8").read()
    assert "function renderRing(" in js and "ring-badge" in js


def test_all_cards_start_closed_and_have_id_for_persistence():
    """Todo <details class="card"> nasce fechado (sem `open`) e tem id, para o localStorage lembrar o que o usuário abriu."""
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    _login(client)
    html = client.get("/").data.decode()
    tags = re.findall(r'<details class="card[^"]*"[^>]*>', html)
    assert len(tags) >= 9
    assert not [t for t in tags if re.search(r"\bopen\b", t)]
    assert not [t for t in tags if 'id="' not in t]


def test_equity_range_defaults_to_all_and_overview_keeps_cards_closed():
    """Equity: a aba "Teste" (janela do forward test Donchian) saiu; "Tudo" é a padrão.
    Na visão geral (hash #overview) o eq-card não é aberto automaticamente: só páginas
    compostas apenas de cards recolhíveis abrem o primeiro."""
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    _login(client)
    html = client.get("/").data.decode()
    assert 'data-range="test"' not in html and "Teste</button>" not in html
    assert '<button class="chip on" role="tab" data-range="all" aria-selected="true">Tudo</button>' in html
    js = open("trading_bot/web/static/js/main.js", encoding="utf-8").read()
    assert "chartRange: 'all'" in js and "range === 'test'" not in js
    assert "cards.every(c => c.tagName === 'DETAILS')" in js


def test_mobile_first_pwa_metas_hero_and_tabbar():
    """Celular: metas de PWA/iOS, hero no topo fixo, barra inferior com as mesmas páginas do menu lateral; login com input 16px."""
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    login = client.get("/login").data.decode()
    _login(client)
    html = client.get("/").data.decode()
    for page in (html, login):
        assert 'viewport-fit=cover' in page and 'name="apple-mobile-web-app-capable" content="yes"' in page
        assert 'rel="apple-touch-icon"' in page and 'rel="manifest"' in page
    assert 'id="hd-equity"' in html and 'id="hd-today"' in html
    tabbar = re.findall(r'<nav class="tabbar"[^>]*>(.*?)</nav>', html, re.S)[0]
    for page in re.findall(r'data-page="([^"]+)"', tabbar):
        assert f'<div class="page" data-page="{page}"' in html
    assert client.get("/static/manifest.webmanifest").status_code == 200
    assert client.get("/static/icon-180.png").status_code == 200
    css = open("trading_bot/web/static/css/login.css", encoding="utf-8").read()
    assert "font-size:16px" in css.split(".login-field input {")[1].split("}")[0]


def test_theme_toggle_applies_before_first_paint_and_never_follows_os():
    """Tema escuro (padrão, Ébano) ou claro só pelo botão (tradingbot-theme no localStorage, aplicado no
    <head> antes do primeiro paint); nunca pelo prefers-color-scheme do sistema. O login continua só escuro."""
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    login_page = client.get("/login").data
    _login(client)
    html = client.get("/").data
    assert b'<meta name="color-scheme" content="dark light">' in html
    assert html.index(b"tradingbot-theme") < html.index(b"</head>") and b'id="theme-toggle"' in html
    assert b"prefers-color-scheme" not in html and b"prefers-color-scheme" not in login_page
    assert b"tradingbot-theme" not in login_page
    css = open("trading_bot/web/static/css/style.css", encoding="utf-8").read()
    assert "prefers-color-scheme" not in css and 'color-scheme: dark' in css.split("[data-theme=")[0]
    assert 'color-scheme: light' in css.split('[data-theme="light"]')[1].split("}")[0]


def test_shell_sidebar_kpis_and_pages():
    """Shell CryptoZone: menu lateral com as páginas, 3 KPIs com sparkline, cada card existente dentro de
    uma única página (o main.js só troca o `hidden` das páginas; ids/rotas/APIs não mudam)."""
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    _login(client)
    html = client.get("/").data.decode()
    assert 'id="sidebar"' in html and 'id="sidebar-toggle"' in html and 'id="sidebar-backdrop"' in html
    assert 'id="sidebar-collapse"' not in html and 'nav-toggle' not in html   # rail fixa (wallet-rail.tsx): sem recolher nem accordion
    assert html.count('class="nav-item" href=') == 11 and html.count('title="') >= 12   # 11 itens; nome no tooltip (rail só com ícones)
    assert 'href="#system" data-page="system" title="Sistema"' in html and 'href="#system/eng-card"' not in html   # Motor Donchian fica oculto fora do Donchian: um tile só
    pages = re.findall(r'<div class="page" data-page="([^"]+)"', html)
    assert pages == ["overview", "pnl", "sheet", "market", "fills", "sim", "accounts", "users", "system"]
    nav = html.split('<nav class="nav" id="nav">')[1].split("</nav>")[0]
    assert set(re.findall(r'data-page="([^"]+)"', nav)) == set(pages)
    # um cartão por grupo (sempre visível), itens apontando para página/card existentes
    assert re.findall(r'data-group="([^"]+)"', nav) == ["main", "trading", "market", "reports", "system", "platform"]
    for target in re.findall(r'data-target="([^"]+)"', nav):
        assert f'id="{target}"' in html, target
    assert html.count('class="page" data-page="overview"') == 1 and ' hidden' in re.findall(r'<div class="page" data-page="pnl"[^>]*>', html)[0]
    for k in ("equity", "margin", "unreal", "pnl", "funding"):
        assert f'id="kpi-{k}"' in html, k
    for k in ("equity", "pnl", "funding"):
        assert f'id="kpi-{k}-spark"' in html, k
    assert 'id="kpi-margin-bar"' in html and 'id="kpi-unreal-legs"' in html
    assert "Depósito" not in html and "Buy Coin" not in html   # conta de perpétuos, não spot
    # cada card segue existindo exatamente uma vez, agora dentro de uma página
    for i in ("ring-card", "legs-card", "status", "ft-card", "pos-card", "pnl-card", "eqperf-row", "sheet-card", "trades-card", "rdist-row", "eng-card", "health-card"):
        assert html.count(f'id="{i}"') == 1, i
    body = html.split("<main")[1]
    assert body.index('id="pnl-card"') > body.index('data-page="pnl"') and body.index('id="sheet-card"') > body.index('data-page="sheet"')
    assert client.get("/static/css/shell.css").status_code == 200
    js = open("trading_bot/web/static/js/main.js", encoding="utf-8").read()
    assert "function showPage(" in js and "function renderKpis(" in js and "hashchange" in js
    assert "function futLine(" in js and js.count("${futLine(") == 1   # posições Donchian; as pernas do martingale têm a própria grade


def test_live_always_carries_sheet_and_js_renders_it_without_runner():
    """Planilha CoinTech2u é estática: /api/live traz `sheet` (faixa da equity atual) mesmo sem runner
    do martingale, e o main.js renderiza o card sempre (o runner só acrescenta score e pares ativos)."""
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot(last_known_balance=320.0))
    client = app.test_client()
    _login(client)
    live = client.get("/api/live").get_json()
    assert live["martingale"] is None
    sheet = live["sheet"]
    assert sheet["rows"] and sheet["current"] == 1 and sheet["rows"][1][1] == 300   # $320 → faixa 300
    assert sheet["scores"] == {} and sheet["green"] and sheet["red"]
    html = client.get("/").data.decode()
    assert html.count('class="empty page-empty"') == 6
    js = open("trading_bot/web/static/js/main.js", encoding="utf-8").read()
    assert "$('sheet-card').hidden = !sheet; renderSheet(sheet" in js and "function pageEmpties(" in js


def test_collect_positions_exposes_perp_fields_from_exchange():
    """Perpétuo: alavancagem, notional (qty × marca), margem, liquidação e ROE (P&L / margem) vêm da
    exchange; sem exchange ficam None (nunca inventa)."""
    known = {"ADAUSDT_LONG": {"symbol": "ADAUSDT", "side": "LONG", "entry_price": 0.50, "quantity": 100.0}}
    bot = _make_bot(known_positions=known)
    p = dashboard_data.collect_positions(bot)[0]
    assert p["leverage"] is None and p["liquidation_price"] is None and p["roe_percent"] is None
    assert p["margin_usd"] is None and p["notional"] == pytest.approx(50.0)   # qty × entrada sem marca
    bot.exchange = SimpleNamespace(get_open_positions=lambda: [
        {"symbol": "ADAUSDT", "side": "LONG", "quantity": 100.0, "entry_price": 0.50, "mark_price": 0.52,
         "unrealized_pnl": 2.0, "leverage": 20, "liquidation_price": 0.4761, "margin": 2.5}])
    p = dashboard_data.collect_positions(bot)[0]
    assert p["leverage"] == 20 and p["notional"] == pytest.approx(52.0) and p["margin_usd"] == 2.5
    assert p["liquidation_price"] == 0.4761 and p["roe_percent"] == pytest.approx(80.0)
    # exchange sem marginSize (Binance/OKX): margem = notional / alavancagem
    bot.exchange = SimpleNamespace(get_open_positions=lambda: [
        {"symbol": "ADAUSDT", "side": "LONG", "quantity": 100.0, "entry_price": 0.50, "mark_price": 0.50,
         "unrealized_pnl": 0.0, "leverage": 10}])
    p = dashboard_data.collect_positions(bot)[0]
    assert p["margin_usd"] == pytest.approx(5.0) and p["liquidation_price"] is None and p["roe_percent"] == 0.0


# ---------- Login por sessão ----------


def test_login_page_renders_form():
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    r = app.test_client().get("/login?next=/x")
    assert r.status_code == 200
    assert b'autocomplete="username"' in r.data and b'autocomplete="current-password"' in r.data
    assert b'name="next" value="/x"' in r.data


def test_login_wrong_password_shows_error_and_logs(caplog):
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    with caplog.at_level("WARNING"):
        r = _login(client, pw="wrong")
    assert r.status_code == 200 and "Usuário ou senha inválidos" in r.get_data(as_text=True)
    assert client.get("/").status_code == 302
    msgs = [rec.getMessage() for rec in caplog.records if rec.levelname == "WARNING"]
    assert any("Login do dashboard falhou" in m and "'admin'" in m for m in msgs)
    assert not any("wrong" in m for m in msgs)


def test_login_success_redirects_to_next_and_grants_access():
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    r = _login(client, next_="/?tab=pnl")
    assert r.status_code == 302 and r.headers["Location"] == "/?tab=pnl"
    assert client.get("/").status_code == 200
    assert client.get("/api/live").status_code == 200
    assert client.post("/api/control/pause").status_code == 200
    cookie = client.get_cookie("session")
    assert cookie is not None and cookie.http_only and cookie.same_site == "Lax" and not cookie.secure
    assert cookie.expires is not None  # "lembrar-me": cookie persistente (30 dias)


def test_login_ignores_absolute_next():
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    for bad in ("https://evil.example/", "//evil.example/x", "/\\evil.example", "javascript:alert(1)"):
        client = app.test_client()
        r = _login(client, next_=bad)
        assert r.status_code == 302 and r.headers["Location"] == "/", bad


def test_login_requires_csrf_token():
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    r = client.post("/login", data={"username": "admin", "password": "secret", "csrf": "forged"})
    assert r.status_code == 400
    assert client.get("/").status_code == 302


def test_api_without_auth_is_401_json_and_basic_still_works():
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    r = client.get("/api/live")
    assert r.status_code == 401 and r.get_json()["ok"] is False
    assert client.post("/api/control/pause").status_code == 401
    assert client.get("/api/live", headers=_basic_auth("admin", "secret")).status_code == 200


def test_login_cookie_is_session_only_unless_remember():
    """Sem 'remember' o cookie morre com o navegador; com ele ganha Expires/Max-Age."""
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    client.get("/login")
    r = client.post("/login", data={"username": "admin", "password": "secret", "csrf": _csrf(client), "next": "/"})
    cookie = r.headers.get("Set-Cookie", "")
    assert r.status_code == 302 and "session=" in cookie
    assert "Expires=" not in cookie and "Max-Age=" not in cookie
    r = _login(app.test_client())  # helper manda remember=1
    cookie = r.headers.get("Set-Cookie", "")
    assert "Expires=" in cookie  # Werkzeug emite só Expires (sem Max-Age) p/ sessão permanente
    # A página de login nasce com o "manter conectado" desmarcado.
    assert b'name="remember" value="1" checked' not in client.get("/login").data


def test_logout_clears_session():
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    _login(client)
    assert client.get("/").status_code == 200
    # Sem CSRF o logout é ignorado (SameSite=Lax + token).
    client.post("/logout", data={})
    assert client.get("/").status_code == 200
    r = client.post("/logout", data={"csrf": _csrf(client)})
    assert r.status_code == 302 and r.headers["Location"] == "/login"
    assert client.get("/").status_code == 302


def test_login_rate_limit_blocks_sixth_attempt():
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    for _ in range(5):
        assert _login(client, pw="wrong").status_code == 200
    r = _login(client, pw="secret")  # até a senha certa é barrada
    assert r.status_code == 429 and "Muitas tentativas" in r.get_data(as_text=True)
    assert client.get("/").status_code == 302


def test_login_rate_limit_uses_forwarded_for_only_with_trust_proxy(monkeypatch):
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    for _ in range(5):
        _login(client, pw="wrong", headers={"X-Forwarded-For": "10.0.0.1"})
    # Sem TRUST_PROXY o header é ignorado: o IP real (test client) já está bloqueado.
    assert client.post("/login", data={"username": "admin", "password": "secret", "csrf": _csrf(client)}, headers={"X-Forwarded-For": "10.0.0.2"}).status_code == 429

    monkeypatch.setattr(global_config, "DASHBOARD_TRUST_PROXY", True, raising=False)
    app2, _ = create_app(_make_bot())
    client2 = app2.test_client()
    for _ in range(5):
        _login(client2, pw="wrong", headers={"X-Forwarded-For": "10.0.0.1"})
    assert client2.post("/login", data={"username": "admin", "password": "wrong", "csrf": _csrf(client2)}, headers={"X-Forwarded-For": "10.0.0.1"}).status_code == 429
    # outro IP e outro usuário: passa (o mesmo usuário de outro IP continua bloqueado: limite também por nome)
    assert client2.post("/login", data={"username": "outro", "password": "wrong", "csrf": _csrf(client2)}, headers={"X-Forwarded-For": "10.0.0.2"}).status_code == 200


def test_socket_rejects_connection_without_session():
    from trading_bot.web.app import create_app
    app, socketio = create_app(_make_bot())
    anon = socketio.test_client(app)
    assert anon.is_connected() is False
    flask_client = app.test_client()
    _login(flask_client)
    logged = socketio.test_client(app, flask_test_client=flask_client)
    assert logged.is_connected() is True
    assert any(e["name"] == "snapshot" for e in logged.get_received())


# ---------- Controls ----------


def test_pause_sets_paused_true():
    from trading_bot.web.app import create_app
    bot = _make_bot(paused=False)
    app, _ = create_app(bot)
    client = app.test_client()
    r = client.post("/api/control/pause", headers=_basic_auth("admin", "secret"))
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "paused": True}
    assert bot.paused is True


def test_resume_sets_paused_false(caplog):
    from trading_bot.web.app import create_app
    bot = _make_bot(paused=True)
    app, _ = create_app(bot)
    client = app.test_client()
    with caplog.at_level("WARNING"):
        r = client.post("/api/control/resume", headers=_basic_auth("admin", "secret"))
    assert r.status_code == 200
    assert bot.paused is False
    assert "Bot retomado via dashboard /api/control/resume" in [r.getMessage() for r in caplog.records if r.levelname == "WARNING"][0]


def test_close_all_invokes_bot_method_with_reason():
    from trading_bot.web.app import create_app
    bot = _make_bot()
    app, _ = create_app(bot)
    client = app.test_client()
    r = client.post(
        "/api/control/close_all",
        headers={**_basic_auth("admin", "secret"), "Content-Type": "application/json"},
        json={"reason": "test reason"},
    )
    assert r.status_code == 200
    bot._close_all_positions_daily_target.assert_called_once_with("test reason")


def _close_position(client, symbol, body):
    return client.post(
        f"/api/positions/{symbol}/close",
        headers={**_basic_auth("admin", "secret"), "Content-Type": "application/json"},
        json=body,
    )


def test_close_position_requires_matching_confirm():
    from trading_bot.web.app import create_app
    bot = _make_bot()
    app, _ = create_app(bot)
    client = app.test_client()
    for body in ({}, {"confirm": "ETHUSDT"}):
        r = _close_position(client, "BTCUSDT", body)
        assert r.status_code == 400 and r.get_json()["ok"] is False
    bot.close_position_manual.assert_not_called()


def test_close_position_rejects_symbol_not_open():
    from trading_bot.web.app import create_app
    bot = _make_bot()
    bot.close_position_manual = MagicMock(side_effect=LookupError("BTCUSDT não tem posição aberta"))
    app, _ = create_app(bot)
    r = _close_position(app.test_client(), "BTCUSDT", {"confirm": "BTCUSDT"})
    assert r.status_code == 404 and "BTCUSDT" in r.get_json()["error"]


def test_close_position_calls_bot_with_symbol_and_manual_reason():
    from trading_bot.web.app import create_app
    bot = _make_bot()
    app, _ = create_app(bot)
    r = _close_position(app.test_client(), "BTCUSDT", {"confirm": "BTCUSDT"})
    assert r.status_code == 200 and r.get_json() == {"ok": True, "symbol": "BTCUSDT"}
    bot.close_position_manual.assert_called_once_with("BTCUSDT", "Manual (dashboard)", side=None)


def test_close_position_passes_side_and_rejects_bad_side():
    """Hedge: LONG e SHORT do mesmo símbolo; o dashboard manda o lado da perna que o usuário clicou."""
    from trading_bot.web.app import create_app
    bot = _make_bot()
    app, _ = create_app(bot)
    r = _close_position(app.test_client(), "ADAUSDT", {"confirm": "ADAUSDT", "side": "SHORT"})
    assert r.status_code == 200
    bot.close_position_manual.assert_called_once_with("ADAUSDT", "Manual (dashboard)", side="SHORT")
    r = _close_position(app.test_client(), "ADAUSDT", {"confirm": "ADAUSDT", "side": "both"})
    assert r.status_code == 400 and bot.close_position_manual.call_count == 1


def test_close_position_reports_unconfirmed_close():
    from trading_bot.web.app import create_app
    bot = _make_bot()
    bot.close_position_manual = MagicMock(return_value=False)
    app, _ = create_app(bot)
    r = _close_position(app.test_client(), "BTCUSDT", {"confirm": "BTCUSDT"})
    assert r.status_code == 502 and r.get_json()["ok"] is False


def test_close_all_returns_500_when_bot_method_fails():
    from trading_bot.web.app import create_app
    bot = _make_bot()
    bot._close_all_positions_daily_target = MagicMock(side_effect=RuntimeError("boom"))
    app, _ = create_app(bot)
    client = app.test_client()
    r = client.post(
        "/api/control/close_all",
        headers={**_basic_auth("admin", "secret"), "Content-Type": "application/json"},
        json={},
    )
    assert r.status_code == 500
    payload = r.get_json()
    assert payload["ok"] is False


def test_control_endpoints_require_auth():
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    assert client.post("/api/control/pause").status_code == 401
    assert client.post("/api/control/resume").status_code == 401
    assert client.post("/api/control/close_all").status_code == 401


# ---------- Coletor de dados ----------


def test_collect_summary_includes_kpis(monkeypatch):
    """Summary com fonte Binance (mockada). Testnet sem SIMULATED_BALANCE_USD
    significa equity = wallet + unrealized."""
    monkeypatch.setattr(global_config, "SIMULATED_BALANCE_USD", 0.0, raising=False)
    bot = _make_bot(initial_capital=100.0, last_known_balance=120.0, total_pnl=20.0)
    bot.exchange = SimpleNamespace(
        get_daily_pnl_from_binance=lambda: {"total": 5.0},
        get_account_info=lambda: {"wallet_balance": 120.0, "unrealized_pnl": 2.0},
        get_open_positions=lambda: [],
    )
    summary = dashboard_data.collect_summary(bot)
    assert summary["initial_capital"] == 100.0
    # total_pnl agora é ACUMULADO: sem trade_store cai no bot_total_pnl (20.0)
    # + unrealized (2.0) = 22.0. (Antes usava só o realizado do DIA e zerava.)
    assert summary["total_pnl"] == 22.0
    # P&L HOJE continua sendo o realizado diário da Binance
    assert summary["daily_pnl"] == 5.0
    assert summary["unrealized_pnl"] == 2.0
    # Equity = wallet + unrealized = 122.0 (não-simulado, inalterado)
    assert summary["last_balance"] == 122.0
    # Bot counters preservados pra debug
    assert summary["bot_total_pnl"] == 20.0
    assert summary["paused"] is False
    assert summary["running"] is True
    assert summary["environment"] == "testnet"
    assert summary["exchange"] == exchange_label(global_config)


def test_collect_summary_daily_pnl_breakdown(monkeypatch):
    """Subtítulo do card "Realizado hoje": parcelas por tipo somam o total."""
    monkeypatch.setattr(global_config, "SIMULATED_BALANCE_USD", 0.0, raising=False)
    bot = _make_bot()
    bot.exchange = SimpleNamespace(
        get_daily_pnl_from_binance=lambda: {"total": 0.31, "realized_pnl": 0.35, "funding_fee": -0.02, "commission": -0.02},
        get_account_info=lambda: {"wallet_balance": 120.0, "unrealized_pnl": 0.0},
        get_open_positions=lambda: [],
    )
    summary = dashboard_data.collect_summary(bot)
    assert summary["daily_pnl_breakdown"] == {"trades": 0.35, "funding": -0.02, "commission": -0.02}
    assert sum(summary["daily_pnl_breakdown"].values()) == pytest.approx(summary["daily_pnl"])


def test_collect_summary_uses_simulated_cap_in_testnet(monkeypatch):
    """Com SIMULATED_BALANCE_USD ativo, equity = cap + ACUMULADO + unrealized.

    Antes somava só o realizado do DIA (zerava na virada). Agora soma o
    acumulado (sem trade_store, cai no bot_total_pnl=10.0 default)."""
    monkeypatch.setattr(global_config, "SIMULATED_BALANCE_USD", 130.0, raising=False)
    bot = _make_bot(initial_capital=130.0)  # total_pnl default = 10.0
    bot.exchange = SimpleNamespace(
        get_daily_pnl_from_binance=lambda: {"total": 1.5},
        get_account_info=lambda: {"wallet_balance": 130.0, "unrealized_pnl": -0.20},
        get_open_positions=lambda: [],
    )
    summary = dashboard_data.collect_summary(bot)
    # 130 (cap) + 10.0 (acumulado) - 0.20 (unrealized) = 139.80
    assert summary["last_balance"] == pytest.approx(139.80, abs=0.001)


def test_collect_positions_serializes_known_positions():
    positions = {
        "BTCUSDT_LONG": {
            "symbol": "BTCUSDT",
            "side": "LONG",
            "entry_price": 50000.0,
            "quantity": 0.01,
            "strategy_name": "trend_strong",
            "strategy_type": "trend_signal",
            "custom_stop_loss": 49000.0,
            "custom_take_profit": 51000.0,
            "trailing_activation_pct": 0.7,
            "trailing_distance_pct": 0.4,
        }
    }
    bot = _make_bot(known_positions=positions)
    out = dashboard_data.collect_positions(bot)
    assert len(out) == 1
    p = out[0]
    assert p["symbol"] == "BTCUSDT"
    assert p["side"] == "LONG"
    assert p["entry_price"] == 50000.0
    assert p["trailing_activation_pct"] == 0.7


def test_collect_positions_exposes_effective_trailing_from_bot(monkeypatch):
    """NEAR 16/10: o painel mostrava 'trail 0.7/0.4%' (toFixed(1) de 0.7311/0.40,
    os valores ATR) e parecia o fallback do .env. O snapshot traz os MESMOS
    valores que o bot usa (_get_position_trailing_params), e o .env só quando a
    posição não tem ATR."""
    from trading_bot.core.bot import TradingBot

    monkeypatch.setattr(global_config, "TRAILING_ACTIVATION_PERCENT", 0.70, raising=False)
    monkeypatch.setattr(global_config, "TRAILING_DISTANCE_PERCENT", 0.40, raising=False)
    positions = {
        "NEARUSDT_SHORT": {
            "symbol": "NEARUSDT", "side": "SHORT", "entry_price": 4.9945, "quantity": 12.0,
            "trailing_activation_pct": 0.7311, "trailing_distance_pct": 0.40,
        },
        "ETHUSDT_LONG": {  # reconciliada, sem ATR → fallback do .env
            "symbol": "ETHUSDT", "side": "LONG", "entry_price": 2000.0, "quantity": 0.1,
            "trailing_activation_pct": None, "trailing_distance_pct": None,
        },
    }
    bot = _make_bot(known_positions=positions)
    bot.positions = SimpleNamespace(get=lambda k: positions.get(k, {}))
    bot._get_known_position = lambda k: TradingBot._get_known_position(bot, k)
    bot._get_position_trailing_params = lambda k: TradingBot._get_position_trailing_params(bot, k)

    out = {p["key"]: p for p in dashboard_data.collect_positions(bot)}
    near = out["NEARUSDT_SHORT"]
    assert (near["trailing_activation_pct"], near["trailing_distance_pct"]) == (0.7311, 0.40)
    assert near["trailing_activation_price"] == pytest.approx(4.9945 * (1 - 0.007311))
    eth = out["ETHUSDT_LONG"]
    assert (eth["trailing_activation_pct"], eth["trailing_distance_pct"]) == (0.70, 0.40)
    assert eth["trailing_activation_price"] == pytest.approx(2000.0 * 1.007)


def test_collect_regime_filters_to_active_trading_pairs(monkeypatch):
    """Regime classifier acumula obs de TODOS os pares analisados; o painel
    só deve mostrar os pares ATIVOS (em TRADING_PAIRS ou com posição aberta)."""
    monkeypatch.setattr(global_config, "TRADING_PAIRS", ["BTCUSDT", "ETHUSDT"], raising=False)
    bot = _make_bot(
        _regime_committed={"BTCUSDT": "trend", "DOGEUSDT": "range", "FILUSDT": "squeeze"},
        _regime_observations={
            "BTCUSDT": ["trend", "trend"],
            "ETHUSDT": ["range"],
            "DOGEUSDT": ["range", "range"],
            "FILUSDT": ["squeeze"],
        },
    )
    regime = dashboard_data.collect_regime(bot)
    # Só BTCUSDT/ETHUSDT estão em TRADING_PAIRS
    assert regime["committed"] == {"BTCUSDT": "trend"}
    assert set(regime["observations"].keys()) == {"BTCUSDT", "ETHUSDT"}
    assert "DOGEUSDT" not in regime["observations"]
    assert "FILUSDT" not in regime["observations"]


def test_collect_regime_includes_pairs_with_open_positions(monkeypatch):
    """Mesmo fora de TRADING_PAIRS, pares com posição aberta entram no painel."""
    monkeypatch.setattr(global_config, "TRADING_PAIRS", ["BTCUSDT"], raising=False)
    bot = _make_bot(
        known_positions={"XRPUSDT_LONG": {"symbol": "XRPUSDT"}},
        _regime_committed={"XRPUSDT": "trend", "OTHERUSDT": "range"},
        _regime_observations={"XRPUSDT": ["trend"], "OTHERUSDT": ["range"]},
    )
    regime = dashboard_data.collect_regime(bot)
    assert "XRPUSDT" in regime["committed"]
    assert "OTHERUSDT" not in regime["committed"]


def test_collect_recent_trades_reverses_order_and_limits():
    history = [
        {"timestamp": "2026-05-01T10:00:00", "symbol": "BTCUSDT", "side": "LONG", "pnl_net": 1.0},
        {"timestamp": "2026-05-02T10:00:00", "symbol": "ETHUSDT", "side": "SHORT", "pnl_net": -0.5},
        {"timestamp": "2026-05-03T10:00:00", "symbol": "SOLUSDT", "side": "LONG", "pnl_net": 0.3},
    ]
    bot = _make_bot(trade_history=history)
    trades = dashboard_data.collect_recent_trades(bot, limit=2)
    assert len(trades) == 2
    # Mais recentes primeiro
    assert trades[0]["symbol"] == "SOLUSDT"
    assert trades[1]["symbol"] == "ETHUSDT"


def test_collect_martingale_none_without_runner_and_present_with_it():
    bot = _make_bot()
    assert dashboard_data.collect_martingale(bot, [], {"last_balance": 100.0}) is None
    calls = []
    bot.martingale = SimpleNamespace(ladder=lambda positions, equity: calls.append((positions, equity)) or {"legs": []})
    out = dashboard_data.collect_martingale(bot, [{"symbol": "ADAUSDT"}], {"last_balance": 100.0})
    assert out["legs"] == [] and out["sheet"]["current"] == 0
    assert calls == [([{"symbol": "ADAUSDT"}], 100.0)]
    bot.martingale = SimpleNamespace(ladder=lambda positions, equity: 1 / 0)
    assert dashboard_data.collect_martingale(bot, [], {}) is None


def test_project_next_bank_math():
    from datetime import date
    out = dashboard_data.project_next_bank(307.17, 500, 0.49, date(2026, 11, 10))
    assert out["gap"] == pytest.approx(192.83) and out["eta_days"] == 394 and out["eta_date"] == "2027-12-09"
    assert dashboard_data.project_next_bank(307.17, 500, -0.2, date(2026, 11, 10))["eta_days"] is None
    assert dashboard_data.project_next_bank(600.0, 500, 1.0, date(2026, 11, 10))["eta_days"] == 0
    assert dashboard_data.project_next_bank(307.17, None, 1.0, date(2026, 11, 10)) == {"next_bank": None, "gap": None, "eta_days": None, "eta_date": None}


def test_martingale_projection_uses_realized_trades_window():
    """Ritmo = P&L realizado do martingale / dias desde o 1º trade (máx. 14), sem contar hoje se hoje não fechou trade."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    rows = [{"exit_at": (now - timedelta(days=1, hours=2)).isoformat(), "pnl_net": 2.0, "strategy_type": "martingale_hedge"},
            {"exit_at": (now - timedelta(days=1, hours=1)).isoformat(), "pnl_net": -0.5, "strategy_type": "martingale_hedge"}]
    bot = _make_bot()
    bot.trade_store = SimpleNamespace(closed_trades=lambda strategy_type=None, limit=2000: rows)
    bot.martingale = SimpleNamespace(ladder=lambda positions, equity: {"legs": []})
    pj = dashboard_data.collect_martingale(bot, [], {"last_balance": 310.0})["sheet"]["projection"]
    assert pj["trades"] == 2 and pj["next_bank"] == 500 and pj["gap"] == pytest.approx(190.0)
    assert pj["days_elapsed"] in (1, 2) and pj["avg_daily"] == pytest.approx(1.5 / pj["days_elapsed"])
    assert pj["eta_days"] == pj["days_elapsed"] * 127  # ceil(190 / (1.5/d))
    bot.trade_store = SimpleNamespace(closed_trades=lambda strategy_type=None, limit=2000: [])
    assert dashboard_data.collect_martingale(bot, [], {"last_balance": 310.0})["sheet"]["projection"] is None


# ---------- collect_forward_test: card "Desde o início" por estratégia ----------


def _fake_store(rows):
    calls = []

    def closed_trades(strategy_type=None, limit=2000):
        calls.append(strategy_type)
        return [r for r in rows if strategy_type is None or r["strategy_type"] == strategy_type]

    return SimpleNamespace(closed_trades=closed_trades, calls=calls)


_ROWS = [
    # anterior ao início do forward test Donchian (24/10): fora do Donchian conta mesmo assim
    {"strategy_type": "martingale_hedge", "symbol": "ADAUSDT", "exit_at": "2026-06-01T10:00:00+00:00",
     "pnl_net": 1.5, "fees": 0.10, "funding": -0.02},
    {"strategy_type": "martingale_hedge", "symbol": "ADAUSDT", "exit_at": "2026-11-08T10:00:00+00:00",
     "pnl_net": 2.0, "fees": 0.20, "funding": 0.01},
    {"strategy_type": "martingale_hedge", "symbol": "ADAUSDT", "exit_at": "2026-11-08T11:00:00+00:00",
     "pnl_net": 2.5, "fees": 0.30, "funding": 0.0},
    {"strategy_type": "donchian_4h", "symbol": "ETHUSDT", "exit_at": "2026-11-01T00:00:00+00:00",
     "pnl_net": -4.0, "fees": 0.50, "funding": 0.05},
]


def test_collect_forward_test_martingale_soma_todos_os_trades_do_store(monkeypatch):
    monkeypatch.setattr(global_config, "STRATEGY", "martingale_hedge", raising=False)
    monkeypatch.setattr(global_config, "FORWARD_TEST_RESTART_AT", "", raising=False)
    store = _fake_store(_ROWS)
    bot = SimpleNamespace(trade_store=store, peak_equity=110.0)
    out = dashboard_data.collect_forward_test(bot, {"last_balance": 110.0, "unrealized_pnl": 0.0})
    assert store.calls == [None]
    assert out["trades"] == 4
    assert out["net_usd"] == pytest.approx(2.0)
    assert out["fees_usd"] == pytest.approx(1.10)
    assert out["funding_usd"] == pytest.approx(0.04)
    assert out["start"] == "2026-06-01T10:00:00+00:00"
    assert out["restart"] is None


def test_collect_forward_test_donchian_mantem_filtro_de_estrategia_e_janela(monkeypatch):
    monkeypatch.setattr(global_config, "STRATEGY", "donchian_4h", raising=False)
    monkeypatch.setattr(global_config, "FORWARD_TEST_RESTART_AT", "", raising=False)
    store = _fake_store(_ROWS)
    bot = SimpleNamespace(trade_store=store, peak_equity=110.0)
    out = dashboard_data.collect_forward_test(bot, {"last_balance": 110.0, "unrealized_pnl": 0.0})
    assert store.calls == ["donchian_4h"]
    assert out["trades"] == 1
    assert out["net_usd"] == pytest.approx(-4.0)
    assert out["start"] == "2026-10-24T23:43:07+00:00"


# ---------- /api/pnl_report: card "Análise do P&L de futuros" (réplica da Bitget) ----------


_PNL_ROWS = [
    # 02/11 23:30 UTC = 20:30 BRT → dia 2026-11-02 em BRT, 2026-11-02 em UTC
    {"symbol": "ADAUSDT", "side": "LONG", "exit_at": "2026-11-02T23:30:00+00:00", "pnl_net": 3.0, "fees": 0.2, "funding": 0.0, "qty": 10, "entry_price": 0.5, "exit_price": 0.53, "close_reason": "TP"},
    # 03/11 01:00 UTC = 02/11 22:00 BRT → dia 2026-11-02 em BRT (virada de dia muda o bucket)
    {"symbol": "ADAUSDT", "side": "LONG", "exit_at": "2026-11-03T01:00:00+00:00", "pnl_net": -1.0, "fees": 0.1, "funding": -0.05, "qty": 10, "entry_price": 0.5, "exit_price": 0.49, "close_reason": "stop"},
    {"symbol": "ETHUSDT", "side": "SHORT", "exit_at": "2026-11-05T12:00:00+00:00", "pnl_net": -4.0, "fees": 0.5, "funding": 0.0, "qty": 0.1, "entry_price": 2000, "exit_price": 2040, "close_reason": "stop"},
    # fora do período pedido (antes): entra só no "acumulado" e na equity inicial
    {"symbol": "BTCUSDT", "side": "LONG", "exit_at": "2026-10-20T12:00:00+00:00", "pnl_net": 10.0, "fees": 1.0, "funding": 0.0, "qty": 0.001, "entry_price": 60000, "exit_price": 70000, "close_reason": "TP"},
]


def _pnl_bot(exchange=None, rows=_PNL_ROWS):
    store = SimpleNamespace(closed_trades=lambda strategy_type=None, limit=2000: [dict(r) for r in rows])
    return SimpleNamespace(trade_store=store, initial_capital=100.0, exchange=exchange)


@pytest.fixture
def _brt(monkeypatch):
    monkeypatch.setattr(global_config, "DAILY_PNL_TZ_OFFSET_HOURS", -3.0, raising=False)
    monkeypatch.setattr(global_config, "SIMULATED_BALANCE_USD", 0.0, raising=False)


def test_pnl_report_aggregates_period_in_utc_day(_brt):
    from trading_bot.web.pnl_report import build_report

    out = build_report(_pnl_bot(), "2026-11-01", "2026-11-07")
    t = out["totals"]
    assert (t["profit"], t["loss"], t["net"], t["trades"], t["wins"]) == (3.0, -5.0, -2.0, 3, 1)
    assert t["fees"] == pytest.approx(0.8) and t["funding"] == pytest.approx(-0.05)
    assert (t["best"], t["worst"]) == (3.0, -4.0)
    # equity inicial = capital 100 + 10 do trade anterior; % = −2 / 110
    assert t["start_equity"] == 110.0 and t["pct"] == pytest.approx(-2 / 110 * 100, rel=1e-4)
    # dia UTC (como na Bitget), mesmo com o bot em BRT: o trade de 03/11 01:00 UTC fica em 03/11
    assert [(d["day"], d["net"], d["trades"]) for d in out["daily"]] == [("2026-11-02", 3.0, 1), ("2026-11-03", -1.0, 1), ("2026-11-05", -4.0, 1)]
    # sem income da exchange, o extrato da conta é zero dia a dia (calendário/acumulado)
    assert out["calendar"][:2] == [{"day": "2026-11-01", "net": 0.0}, {"day": "2026-11-02", "net": 0.0}]
    assert len(out["calendar"]) == len(out["cumulative"]) == 7 and all(c["value"] == 0.0 for c in out["cumulative"])
    assert out["period"] == {"usd": 0.0, "pct": None, "start_equity": None}
    assert [(s["symbol"], s["net"], s["trades"], s["win_rate"]) for s in out["by_symbol"]] == [("ETHUSDT", -4.0, 1, 0.0), ("ADAUSDT", 2.0, 2, 50.0)]
    assert [tr["symbol"] for tr in out["trades"]] == ["ETHUSDT", "ADAUSDT", "ADAUSDT"]  # saída desc
    # Análise de trading: N lucro / N perda, long : short, índice de P&L = 3 / |−5|
    assert (t["losses"], t["long"], t["short"]) == (2, 2, 1) and t["profit_factor"] == pytest.approx(0.6)


def test_pnl_report_without_exchange_or_store_is_empty_but_ok(_brt):
    from trading_bot.web.pnl_report import build_report

    out = build_report(SimpleNamespace(), None, None)
    assert out["totals"]["trades"] == 0 and out["daily"] == [] and out["trades"] == []
    assert out["income"] == [] and out["income_ok"] is False
    assert out["reference"] == {"BTCUSDT": []} and out["account"]["ok"] is False
    assert out["series"] == [] and out["funding_daily"] == [] and out["totals"]["profit_factor"] is None
    assert out["from"] <= out["to"] == out["today"]


def test_pnl_report_income_failure_keeps_page_alive(_brt):
    from trading_bot.web.pnl_report import build_report

    def boom(**kw):
        raise RuntimeError("binance down")

    ex = SimpleNamespace(get_income_history=boom, get_account_info=lambda: {"wallet_balance": 55.5, "unrealized_pnl": -1.25})
    out = build_report(_pnl_bot(ex), "2026-11-01", "2026-11-07")
    assert out["income_ok"] is False and out["income"] == []
    assert out["account"] == {"ok": True, "equity": 54.25, "unrealized": -1.25}  # saldo = wallet + não realizado


def test_pnl_report_income_and_reference_from_binance(_brt):
    from trading_bot.web import pnl_report

    pnl_report._memo.clear()
    income = [
        {"time": 1793664000000, "incomeType": "FUNDING_FEE", "symbol": "ADAUSDT", "income": "-0.01"},  # 03/11 00:00 UTC
        {"time": 1793664000000, "incomeType": "COMMISSION", "symbol": "ADAUSDT", "income": "-0.2"},
        {"time": 1700000000000, "incomeType": "TRANSFER", "symbol": "", "income": "100"},  # fora da janela
    ]
    # 31/10 fica fora do período (o 1º dia dentro é a base do %); a vela aberta de hoje entra como qualquer outra
    klines = [[1793404800000, 0, 0, 0, "90", 0], [1793491200000, 0, 0, 0, "100", 0], [1793577600000, 0, 0, 0, "110", 0]]
    calls = []
    ex = SimpleNamespace(
        get_income_history=lambda **kw: income,
        get_account_info=lambda: {"wallet_balance": 10.0},
        get_klines_raw=lambda sym, interval, limit: calls.append((sym, interval, limit)) or klines,
    )
    out = pnl_report.build_report(_pnl_bot(ex), "2026-11-01", "2026-11-07")
    assert out["income_ok"] is True and len(out["income"]) == 2
    assert out["income_totals"] == {"REALIZED_PNL": 0.0, "COMMISSION": -0.2, "FUNDING_FEE": -0.01, "TRANSFER": 0.0}
    assert calls == [("BTCUSDT", "1d", 8)]
    assert [(r["day"], r["pct"]) for r in out["reference"]["BTCUSDT"]] == [("2026-11-01", 0.0), ("2026-11-02", 10.0)]
    # funding por dia UTC (como na Bitget), não pelo dia de trading BRT (seria 02/11)
    assert out["funding_daily"] == [{"day": "2026-11-03", "amount": -0.01}]
    # cache de 30-60s: a mesma exchange no mesmo período não bate de novo na API
    pnl_report.build_report(_pnl_bot(ex), "2026-11-01", "2026-11-07")
    assert len(calls) == 1


def test_pnl_report_income_floors_on_pnl_since(_brt, monkeypatch):
    # Reset de banca no meio do período: a aba lista e soma só o que veio depois do piso.
    from trading_bot.web.pnl_report import build_report

    monkeypatch.setattr(global_config, "PNL_SINCE_UTC", "2026-11-03T12:00:00Z", raising=False)
    income = [
        {"time": 1793664000000, "incomeType": "REALIZED_PNL", "symbol": "ADAUSDT", "income": "50.9"},  # 03/11 00:00 UTC
        {"time": 1793750400000, "incomeType": "REALIZED_PNL", "symbol": "BNBUSDT", "income": "1.5"},  # 04/11 00:00 UTC
    ]
    seen = []

    def fake_income(**kw):
        seen.append(kw["start_time"])
        return [i for i in income if i["time"] >= kw["start_time"]]

    ex = SimpleNamespace(get_income_history=fake_income, get_account_info=lambda: {})
    out = build_report(_pnl_bot(ex), "2026-11-01", "2026-11-07")
    assert seen == [1793707200000]
    assert [i["symbol"] for i in out["income"]] == ["BNBUSDT"]
    assert out["income_totals"]["REALIZED_PNL"] == 1.5


def test_pnl_report_series_last_snapshot_per_utc_day(_brt, monkeypatch):
    # "P&L total" / "Tendência de ativos": 1 ponto por dia UTC = último snapshot do dia,
    # equity = wallet + não realizado (sem cap simulado), realizado acumulado só entra no testnet simulado.
    from trading_bot.web import pnl_report
    from trading_bot.web.pnl_report import build_report

    # "hoje" fixo: a janela de snapshots é min(período, hoje−29d); com a data real o snapshot de 20/10
    # saía da janela a partir de 19/11 e a % do período caía no 1º snapshot (101.0) em vez do de 20/10
    class _FixedNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 7, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(pnl_report, "datetime", _FixedNow)

    snaps = [
        {"timestamp": "2026-11-02T10:00:00+00:00", "balance": 100.0, "pnl_unrealized": 1.0},
        {"timestamp": "2026-11-02T23:00:00+00:00", "balance": 103.0, "pnl_unrealized": -0.5},   # último de 02/11 UTC, vence
        {"timestamp": "2026-11-03T02:59:00+00:00", "balance": 104.0, "pnl_unrealized": 0.25},   # 23:59 BRT de 02/11, mas 03/11 em UTC
        {"timestamp": "2026-11-05T12:00:00+00:00", "balance": 99.0, "pnl_unrealized": 0.0},
        {"timestamp": "2026-10-20T12:00:00+00:00", "balance": 50.0, "pnl_unrealized": 0.0},     # fora do período
    ]
    asked = []
    store = SimpleNamespace(closed_trades=lambda strategy_type=None, limit=2000: [dict(r) for r in _PNL_ROWS],
                            recent_equity=lambda limit: asked.append(limit) or snaps)
    out = build_report(SimpleNamespace(trade_store=store, initial_capital=100.0), "2026-11-01", "2026-11-07")
    assert [(p["day"], p["equity"], p["unrealized"]) for p in out["series"]] == [("2026-11-02", 102.5, -0.5), ("2026-11-03", 104.25, 0.25), ("2026-11-05", 99.0, 0.0)]
    # snapshots cobrem a união do período com a janela de 30d de hoje (que ancora o 7d/30d)
    wide_days = (date.fromisoformat(out["today"]) - min(date(2026, 11, 1), date.fromisoformat(out["today"]) - timedelta(days=29))).days + 1
    assert asked == [(wide_days + 2) * 144]
    # Análise da conta não usa snapshot: sem income o P&L é zero; a % usa a equity do último snapshot antes do período (20/10)
    assert all(d["net"] == 0.0 for d in out["calendar"])
    assert out["period"] == {"usd": 0.0, "pct": 0.0, "start_equity": 50.0}


def _snap(ts, balance, unreal=0.0):
    return {"timestamp": ts, "balance": balance, "pnl_unrealized": unreal}


def _inc(ts_ms, kind, amount):
    return {"time": ts_ms, "incomeType": kind, "symbol": "" if kind == "TRANSFER" else "ADAUSDT", "income": str(amount)}


def _account_bot(snaps, income, wallet=None, unreal=0.0, rows=()):
    from trading_bot.web import pnl_report

    pnl_report._memo.clear()
    store = SimpleNamespace(closed_trades=lambda strategy_type=None, limit=2000: [dict(r) for r in rows], recent_equity=lambda limit: snaps)
    ex = SimpleNamespace(get_income_history=lambda **kw: [i for i in income if i["time"] >= kw["start_time"]])
    if wallet is not None:
        ex.get_account_info = lambda: {"wallet_balance": wallet, "unrealized_pnl": unreal}
    return SimpleNamespace(trade_store=store, initial_capital=0.0, exchange=ex)


def test_pnl_report_account_day_is_income_sum_per_utc_day(_brt):
    # Caso real de 16-18/11 (app da Bitget): P&L do dia = realizado + taxas + funding do extrato; depósito de $60 em 17/11
    # não é lucro e o não realizado em aberto na virada não entra (o Δequity dava 17: +0,52 / 18: +1,21)
    from trading_bot.web.pnl_report import build_report

    snaps = [_snap("2026-11-16T19:39:00+00:00", 90.5), _snap("2026-11-16T23:50:00+00:00", 90.0, 1.0), _snap("2026-11-17T23:50:00+00:00", 151.75)]
    income = [_inc(1794816000000, "COMMISSION", -0.0246),                                             # 16/11 08:00 UTC: só a taxa de abertura
              _inc(1794916800000, "TRANSFER", 60.0),                                                 # 17/11 12:00 UTC: depósito
              _inc(1794920400000, "REALIZED_PNL", 1.1704), _inc(1794920400000, "COMMISSION", -0.1863),  # 17/11 13:00 UTC
              _inc(1795003200000, "REALIZED_PNL", 0.8), _inc(1795003200000, "COMMISSION", -0.0332),  # 18/11 12:00 UTC
              _inc(1795003200000, "FUNDING_FEE", -0.01)]
    out = build_report(_account_bot(snaps, income), "2026-11-15", "2026-11-18")
    assert [(d["day"], d["net"]) for d in out["calendar"]] == [("2026-11-15", 0.0), ("2026-11-16", -0.0246), ("2026-11-17", 0.9841), ("2026-11-18", 0.7568)]
    assert out["period"] == {"usd": 1.7163, "pct": pytest.approx(1.7163 / 90.5 * 100, rel=1e-4), "start_equity": 90.5}
    assert [(c["day"], c["value"]) for c in out["cumulative"]] == [("2026-11-15", 0.0), ("2026-11-16", -0.0246), ("2026-11-17", 0.9595), ("2026-11-18", 1.7163)]
    assert out["income_totals"]["TRANSFER"] == 60.0 and out["funding_daily"] == [{"day": "2026-11-18", "amount": -0.01}]


def test_pnl_report_today_is_partial_income_and_windows_anchor_on_utc_today(_brt):
    from trading_bot.web.pnl_report import build_report

    today = datetime.now(timezone.utc).date()
    yday = (today - timedelta(days=1)).isoformat()
    snaps = [_snap(f"{yday}T23:50:00+00:00", 100.0), _snap(f"{today.isoformat()}T00:10:00+00:00", 100.4)]
    day_ms = int(datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000)
    income = [_inc(day_ms - 3600000, "REALIZED_PNL", 1.0), _inc(day_ms, "REALIZED_PNL", 2.5), _inc(day_ms, "FUNDING_FEE", -0.01)]  # ontem 23:00 UTC / hoje 00:00 UTC
    out = build_report(_account_bot(snaps, income, wallet=102.0, unreal=0.5), None, None, days=7)
    assert out["today"] == today.isoformat()
    # Tendência de ativos: hoje usa a equity ao vivo, não o snapshot de 00:10
    assert out["series"][-1] == {"day": out["today"], "equity": 102.5, "unrealized": 0.5, "at": out["series"][-1]["at"]}
    # hoje = parcial do dia UTC pelo extrato; % sobre a equity do último snapshot antes de hoje
    assert out["today_pnl"] == {"usd": 2.49, "pct": 2.49, "start_equity": 100.0}
    assert out["windows"]["d7"]["usd"] == 3.49 and out["windows"]["d30"]["usd"] == 3.49
    assert out["calendar"][-1] == {"day": out["today"], "net": 2.49}


def test_pnl_report_account_respects_pnl_since_floor(_brt, monkeypatch):
    from trading_bot.web.pnl_report import build_report

    monkeypatch.setattr(global_config, "PNL_SINCE_UTC", "2026-11-16T12:00:00Z", raising=False)
    snaps = [_snap("2026-11-15T23:50:00+00:00", 50.0), _snap("2026-11-16T10:00:00+00:00", 80.0), _snap("2026-11-16T23:50:00+00:00", 85.0)]
    income = [_inc(1794819960000, "REALIZED_PNL", 30.0),   # 16/11 09:06 UTC: antes do piso, a exchange nem devolve
              _inc(1794840000000, "REALIZED_PNL", 1.0)]    # 16/11 14:40 UTC: depois do piso
    out = build_report(_account_bot(snaps, income), "2026-11-15", "2026-11-17")
    assert [p["day"] for p in out["series"]] == ["2026-11-16"] and out["series"][0]["equity"] == 85.0
    # 15/11 some (antes do piso); 16/11 = só o income depois do piso
    assert out["calendar"] == [{"day": "2026-11-16", "net": 1.0}, {"day": "2026-11-17", "net": 0.0}]


def test_profit_factor_pure():
    from trading_bot.web.pnl_report import profit_factor

    assert profit_factor([3.0, -1.0, -4.0]) == pytest.approx(0.6)
    assert profit_factor([3.0, 2.0]) is None and profit_factor([]) is None
    assert profit_factor([-2.0]) == 0.0


def test_pnl_report_route_requires_auth_and_ignores_bad_dates(_brt):
    from trading_bot.web.app import create_app

    bot = _make_bot()
    bot.trade_store = _pnl_bot().trade_store
    app, _ = create_app(bot)
    client = app.test_client()
    assert client.get("/api/pnl_report").status_code == 401
    r = client.get("/api/pnl_report?from=lixo&to=2026-11-07", headers=_basic_auth("admin", "secret"))
    assert r.status_code == 200
    body = r.get_json()
    assert body["to"] == "2026-11-07" and body["from"] == "2026-10-09"  # default 30d
    assert body["totals"]["trades"] == 4  # 30d pega o trade de 20/10 também


def test_pnl_report_days_preset_anchors_on_bot_trading_day(_brt):
    from trading_bot.web.pnl_report import build_report

    out = build_report(_pnl_bot(), None, None, days="30")
    assert out["to"] == out["today"] and out["from"] == (__import__("datetime").date.fromisoformat(out["today"]) - __import__("datetime").timedelta(days=29)).isoformat()
    assert build_report(_pnl_bot(), None, None, days="lixo")["from"] == out["from"]  # default 30d


# ---------- cotação USD/BRL (trading_bot/web/fx.py) ----------


def _fx_response(price="5.508", status=200):
    """Resposta da 1ª fonte (ticker USDT/BRL da Binance); status != 200 levanta em raise_for_status como o requests."""
    body = {"symbol": "USDTBRL", "price": price} if status == 200 else {"code": -1003, "msg": "Too many requests"}
    return _resp(body, status)


def _resp(body, status=200):
    def raise_for_status():
        if status != 200:
            raise requests.HTTPError(f"{status} Error")

    return SimpleNamespace(status_code=status, json=lambda: body, raise_for_status=raise_for_status)


def _mock_get(monkeypatch, by_url, calls=None):
    """requests.get que responde por URL (valor pode ser Exception) e registra (url, timeout)."""
    from trading_bot.web import fx

    def get(url, timeout):
        if calls is not None:
            calls.append((url, timeout))
        out = by_url[url]
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(fx.requests, "get", get)


def test_fx_fetches_and_caches(monkeypatch):
    from trading_bot.web import fx

    calls = []
    clock = {"t": 1000.0}
    monkeypatch.setattr(fx.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(fx, "_now_iso", lambda: "2026-11-27T14:20:43+00:00")
    monkeypatch.setattr(fx.requests, "get", lambda url, timeout: calls.append(timeout) or _fx_response())
    out = fx.usd_brl()
    assert out == {"usd_brl": 5.508, "source": "binance", "pair": "USDT/BRL", "fetched_at": "2026-11-27T14:20:43+00:00"}
    clock["t"] += fx.TTL_INTRADAY - 1
    assert fx.usd_brl() is out and calls == [fx.TIMEOUT]  # dentro do TTL de 60 s não vai à rede
    clock["t"] += 2
    monkeypatch.setattr(fx.requests, "get", lambda url, timeout: calls.append(timeout) or _fx_response("5.61"))
    assert fx.usd_brl()["usd_brl"] == 5.61 and len(calls) == 2  # TTL vencido: cotação nova


def test_fx_sources_fallback_in_order(monkeypatch):
    """Ticker da Binance falha → usa a Bitget (horário vem do ticker); chave ausente/exceção pulam pra próxima
    até as fontes USD (awesomeapi) e diárias (er-api, com TTL de 10 min)."""
    from trading_bot.web import fx

    bn, bg, aw, er, fr = (u for _, u, _, _, _ in fx.SOURCES)
    calls = []
    bitget = _resp({"code": "00000", "data": [{"symbol": "USDTBRL", "lastPr": "5.5729", "ts": "1795788420323"}]})
    _mock_get(monkeypatch, {bn: _fx_response(status=429), bg: bitget}, calls)
    assert fx.usd_brl() == {"usd_brl": 5.5729, "source": "bitget", "pair": "USDT/BRL", "fetched_at": "2026-11-27T14:07:00+00:00"}
    assert [u for u, _ in calls] == [bn, bg] and all(t == fx.TIMEOUT for _, t in calls)

    monkeypatch.setitem(fx._cache, "next_at", 0.0)
    _mock_get(monkeypatch, {bn: _resp({"code": -1121}), bg: TimeoutError("lento"),
                            aw: _resp({"USDBRL": {"bid": "5.5394", "timestamp": "1795788194", "create_date": "2026-11-27 11:03:14"}})})
    assert fx.usd_brl() == {"usd_brl": 5.5394, "source": "awesomeapi", "pair": "USD/BRL", "fetched_at": "2026-11-27T14:03:14+00:00"}

    clock = {"t": 5000.0}
    monkeypatch.setattr(fx.time, "monotonic", lambda: clock["t"])
    monkeypatch.setitem(fx._cache, "next_at", 0.0)
    _mock_get(monkeypatch, {bn: ConnectionError(), bg: ConnectionError(), aw: _fx_response(status=429),
                            er: _resp({"rates": {"BRL": 5.5046}, "time_last_update_unix": 1794182551})})
    assert fx.usd_brl() == {"usd_brl": 5.5046, "source": "er-api", "pair": "USD/BRL", "fetched_at": "2026-11-09T00:02:31+00:00"}
    assert fx._cache["next_at"] == clock["t"] + fx.TTL_DAILY  # fonte diária: cache de 10 min


def test_fx_failure_returns_none_then_last_known(monkeypatch):
    from trading_bot.web import fx

    assert fx.usd_brl()["usd_brl"] is None  # conftest: HTTP falha e nada em cache
    monkeypatch.setitem(fx._cache, "next_at", 0.0)
    monkeypatch.setattr(fx.requests, "get", lambda url, timeout: _fx_response("5.6"))
    assert fx.usd_brl()["usd_brl"] == 5.6
    monkeypatch.setitem(fx._cache, "next_at", 0.0)

    calls = []
    monkeypatch.setattr(fx.requests, "get", lambda url, timeout: calls.append(url) or _fx_response(status=429))
    assert fx.usd_brl()["usd_brl"] == 5.6  # todas falham: mantém o último valor conhecido
    assert calls == [u for _, u, _, _, _ in fx.SOURCES]  # tentou todas as fontes, em ordem
    assert fx.usd_brl()["usd_brl"] == 5.6 and len(calls) == len(fx.SOURCES)  # retry negativo: 60s sem ir à rede


@pytest.mark.parametrize("equity,expected", [(306.0, 1), (89.0, None), (6500.0, 8), (7200.0, 9), (10500.0, 12), (None, None)])
def test_collect_martingale_sheet_row_for_equity(equity, expected):
    bot = _make_bot()
    bot.martingale = SimpleNamespace(ladder=lambda positions, eq: {"legs": [], "pairs": {"ADAUSDT": {}}})
    out = dashboard_data.collect_martingale(bot, [], {"last_balance": equity})
    sheet = out["sheet"]
    assert sheet["current"] == expected
    assert [r[1] for r in sheet["rows"]] == [90, 300, 500, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000]
    assert sheet["rows"][1][8] == 2 and sheet["rows"][8][6:9] == (1020, True, 11)
    assert sheet["rows"][0][3:9] == (5, 5, 20, 300, True, 1) and sheet["rows"][4][3:9] == (5, 5, 20, 300, True, 9)
    assert all(r[3] >= 5 and r[4] >= 5 for r in sheet["rows"])  # mínimo de ordem da Bitget ($5 notional)
    assert sheet["rows"][12][3:9] == (28, 28, 20, 1680, True, 12)
    assert "ADA" in sheet["green"] and "AVAX" in sheet["green"] and "DOGE" in sheet["red"]
    assert {r[2] for r in sheet["rows"]} == {"Valiengo's Strategy"}


def test_api_live_exposes_sheet_scores_from_cache(monkeypatch):
    from trading_bot.web.app import create_app

    score = {"color": "red", "vol_pct": 5.2, "adx": 38.0, "max_move_pct": 9.0, "volume_usd": 90e6,
             "reasons": ["adx 38 (>35)"], "updated_at": "2026-11-09T12:00:00+00:00"}
    monkeypatch.setattr(dashboard_data, "_score_cache",
                        SimpleNamespace(get=lambda: {"scores": {"ADAUSDT": score}, "updated_at": "2026-11-09T12:00:00+00:00"}))
    bot = _make_bot()
    bot.martingale = SimpleNamespace(ladder=lambda positions, eq: {"legs": [], "pairs": {"ADAUSDT": {}}})
    app, _ = create_app(bot)
    sheet = app.test_client().get("/api/live", headers=_basic_auth("admin", "secret")).get_json()["martingale"]["sheet"]
    assert sheet["scores"] == {"ADAUSDT": score} and sheet["scores_updated_at"] == "2026-11-09T12:00:00+00:00"
    assert "ADA" in sheet["green"]


def test_rank_pairs_orders_by_color_then_score_with_sheet_reds_last():
    def sc(color, vol, adx, mv, volu=100e6):
        return {"color": color, "vol_pct": vol, "adx": adx, "max_move_pct": mv, "volume_usd": volu}
    scores = {
        "AAAUSDT": sc("yellow", 5.5, 10.0, 3.0),
        "BBBUSDT": sc("green", 4.0, 20.0, 8.0),      # verde pior score que CCC
        "CCCUSDT": sc("green", 1.0, 10.0, 2.0),
        "DDDUSDT": sc("red", 9.0, 40.0, 30.0),
        "EEEUSDT": {"color": "unknown"},
        "DOGEUSDT": sc("green", 0.5, 5.0, 1.0),     # vermelha fixa da planilha: melhor número, mas vai pro fim
        "FFFUSDT": sc("green", 1.0, 10.0, 2.0, volu=2.5e6),  # volume abaixo do piso (5M) penaliza
    }
    assert dashboard_data.rank_pairs(scores) == ["CCCUSDT", "BBBUSDT", "FFFUSDT", "AAAUSDT", "DDDUSDT", "EEEUSDT", "DOGEUSDT"]
    assert dashboard_data.rank_score(scores["CCCUSDT"]) == pytest.approx(1.0 / 5 + 10 / 25 + 2.0 / 10.5)  # move_green = 1,5 × 7
    assert dashboard_data.rank_score(scores["FFFUSDT"]) == pytest.approx(dashboard_data.rank_score(scores["CCCUSDT"]) + 2.0)
    assert dashboard_data.rank_score({"color": "unknown"}) is None


def test_api_live_exposes_sheet_ranking(monkeypatch):
    from trading_bot.web.app import create_app

    def sc(color, vol, adx, mv):
        return {"color": color, "vol_pct": vol, "adx": adx, "max_move_pct": mv, "volume_usd": 100e6, "reasons": []}
    monkeypatch.setattr(dashboard_data, "_score_cache", SimpleNamespace(get=lambda: {
        "scores": {"ADAUSDT": sc("red", 6, 40, 30), "TRXUSDT": sc("yellow", 3, 21, 14), "BNBUSDT": sc("green", 2, 15, 5)},
        "updated_at": "2026-11-09T12:00:00+00:00"}))
    bot = _make_bot()
    bot.martingale = SimpleNamespace(ladder=lambda positions, eq: {"legs": [], "pairs": {"ADAUSDT": {}, "TRXUSDT": {}}})
    app, _ = create_app(bot)
    ranking = app.test_client().get("/api/live", headers=_basic_auth("admin", "secret")).get_json()["martingale"]["sheet"]["ranking"]
    assert len(ranking) == 36 and [r["rank"] for r in ranking] == list(range(1, 37))
    assert [r["symbol"] for r in ranking[:3]] == ["BNBUSDT", "TRXUSDT", "ADAUSDT"]
    assert ranking[0] == {"symbol": "BNBUSDT", "rank": 1, "color": "green", "score": pytest.approx(2 / 5 + 15 / 25 + 5 / 10.5),
                          "active": False, "sheet_red": False}
    assert ranking[1]["active"] and ranking[2]["active"]
    assert ranking[3]["color"] == "unknown" and ranking[3]["score"] is None and not ranking[3]["sheet_red"]
    assert all(r["sheet_red"] for r in ranking[-8:]) and {r["symbol"] for r in ranking[-8:]} == {c + "USDT" for c in dashboard_data.COINTECH2U_RED}


def test_pair_scores_builds_cache_once_from_bot_exchange(monkeypatch):
    monkeypatch.setattr(dashboard_data, "_score_cache", None)
    bot = _make_bot()
    bot.exchange = SimpleNamespace()  # sem get_klines_raw: nenhuma thread, scores vazios
    assert dashboard_data._pair_scores(bot) == {"scores": {}, "updated_at": None}
    cache = dashboard_data._score_cache
    assert len(cache.symbols) == 36 and "1000PEPEUSDT" in cache.symbols and cache.symbols[0] == "ADAUSDT"
    dashboard_data._pair_scores(bot)
    assert dashboard_data._score_cache is cache


def test_index_has_cointech2u_sheet_card_and_hidden_cards():
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    _login(client)
    html = client.get("/").data.decode()
    assert 'id="sheet-card" hidden' in html and "Planilha — Estratégia Padrão" in html and "CoinTech2u" not in html
    assert 'id="eng-card"' in html and 'id="trades-card"' in html
    for i in ("tile-last-bar", "tile-next-bar", "rdist-card", "cal-sub"):
        assert f'id="{i}"' in html


def test_live_and_pnl_report_expose_fx(_brt, monkeypatch):
    from trading_bot.web import fx
    from trading_bot.web.app import create_app

    monkeypatch.setattr(fx.requests, "get", lambda url, timeout: _fx_response("5.5"))
    bot = _make_bot()
    bot.trade_store = _pnl_bot().trade_store
    app, _ = create_app(bot)
    client = app.test_client()
    for path in ("/api/live", "/api/pnl_report"):
        body = client.get(path, headers=_basic_auth("admin", "secret")).get_json()
        assert body["fx"]["usd_brl"] == 5.5 and body["fx"]["fetched_at"], path


def test_js_formatters_treat_rounded_zero_as_neutral():
    """fmt.money (USD/BRL)/pct/r e cls() derivam sinal/cor do valor ARREDONDADO: −0,001 vira "$0,00" sem sinal e sem classe."""
    js = open("trading_bot/web/static/js/main.js", encoding="utf-8").read()
    assert "const sgn = (v, d = 2) => Math.abs(v) < 0.5 * 10 ** -d ? 0 : Math.sign(v);" in js
    assert "const s = sgn(v), sign = s < 0 ? '−' : (signed && s > 0 ? '+' : '');" in js
    assert "const b = usd * fxRate(), s = sgn(b), sign = s < 0 ? '−' : (signed && s > 0 ? '+' : '')" in js
    assert "const z = sgn(v, d), s = z < 0 ? '−' : (signed && z > 0 ? '+' : '')" in js
    assert "const cls = (v) => { const z = sgn(v); return z > 0 ? 'pos' : z < 0 ? 'neg' : ''; };" in js
    assert "v < 0 ? '−'" not in js.split("const cls")[0].split("const fmt")[1]


def test_snapshot_martingale_legs_carry_exit_marks_in_price(monkeypatch):
    """Barra de saída: cada perna traz alvo, empate (média ± taker ida e volta), próximo degrau
    e stop (folga até o piso ÷ exposição líquida do par) em PREÇO, conferidos à mão."""
    from trading_bot.core import martingale as mg
    for k, v in dict(MARTINGALE_TP_PERCENT=1.0, MARTINGALE_STEP_PERCENT=2.0, MARTINGALE_MAX_LEVELS=9,
                     MARTINGALE_STOP_LOSS_USDT=100.0, SIMULATED_BALANCE_USD=0.0).items():
        monkeypatch.setattr(global_config, k, v, raising=False)
    live = [{"symbol": "ADAUSDT", "side": "LONG", "entry_price": 0.174, "quantity": 2000.0, "mark_price": 0.17, "unrealized_pnl": -8.0},
            {"symbol": "ADAUSDT", "side": "SHORT", "entry_price": 0.172, "quantity": 500.0, "mark_price": 0.17, "unrealized_pnl": 1.0}]
    bot = _make_bot(last_known_balance=300.0,
                    known_positions={f"ADAUSDT_{p['side']}": {k: p[k] for k in ("symbol", "side", "entry_price", "quantity")} for p in live})
    bot.exchange = SimpleNamespace(get_open_positions=lambda **kw: live)
    bot.commission_rates = {"taker_rate": 0.0005}
    runner = mg.MartingaleRunner(bot)
    runner.peak = 300.0
    runner.pairs = {"ADAUSDT": mg.PairPlan("ADAUSDT", {}, {
        "LONG": mg.Leg("ADAUSDT", "LONG", level=3, last_fill=0.17), "SHORT": mg.Leg("ADAUSDT", "SHORT", level=1, last_fill=0.172)})}
    bot.martingale = runner
    legs = {leg["side"]: leg for leg in dashboard_data.collect_snapshot(bot)["martingale"]["legs"]}
    long, short = legs["LONG"], legs["SHORT"]
    assert long["mark_price"] == 0.17 and long["avg_price"] == 0.174
    assert long["tp_price"] == pytest.approx(0.17574)            # 0,174 × 1,01
    assert long["breakeven_price"] == pytest.approx(0.174174)    # 0,174 × (1 + 2×0,0005)
    assert long["next_add_price"] == pytest.approx(0.1666)       # último fill 0,170 × 0,98
    assert long["stop_price"] == pytest.approx(0.17 - 100.0 / 1500.0)   # folga $100 (300−200) ÷ (2000−500) un
    assert short["tp_price"] == pytest.approx(0.17028) and short["breakeven_price"] == pytest.approx(0.171828)
    assert short["next_add_price"] == pytest.approx(0.17544)
    assert short["stop_price"] is None                           # LONG maior: subida não leva ao piso


def test_max_drawdown_is_worst_floating_dd_since_reset(monkeypatch):
    # Card "Drawdown do pico": pior (pico acumulado − equity), % sobre o pico, hora do fundo.
    ts = [f"2026-11-14T0{i}:00:00+00:00" for i in range(5)]
    pts = [{"timestamp": t, "equity": e} for t, e in zip(ts, [90, 95, 60, 80, 70])]
    monkeypatch.setattr(global_config, "PNL_SINCE_UTC", "", raising=False)
    out = dashboard_data.max_drawdown(pts)
    assert out == {"usd": 35.0, "pct": pytest.approx(36.84, abs=0.01), "at": ts[2]}
    assert dashboard_data.max_drawdown(pts[:2]) is None          # só subiu: sem drawdown
    # piso do reset de banca: o pico de 95 (antes do reset) não conta
    monkeypatch.setattr(global_config, "PNL_SINCE_UTC", "2026-11-14T03:00:00Z", raising=False)
    assert dashboard_data.max_drawdown(pts) == {"usd": 10.0, "pct": 12.5, "at": ts[4]}
    # a chave viaja no /api/history junto com a curva
    assert "max_drawdown" in dashboard_data.collect_history(_make_bot())
    html = open("trading_bot/web/templates/dashboard.html", encoding="utf-8").read()
    assert 'id="st-dd-w"' in html


def test_pnl_details_and_income_tabs_paginate_client_side():
    """Abas "Detalhes" e "Financiamento e Transações": 10 linhas por página, pager "‹ Anterior · página X de Y · Próxima ›"
    (escondido com 1 página), volta à página 1 ao trocar aba/período ou quando a lista muda de tamanho."""
    html = open("trading_bot/web/templates/dashboard.html", encoding="utf-8").read()
    for tab in ("details", "income"):
        pager = html.split(f'id="pnl-{tab}-pager"')[1].split("</div>")[0]
        assert f'id="pnl-{tab}-prev">‹ Anterior<' in pager and f'id="pnl-{tab}-next">Próxima ›<' in pager and f'id="pnl-{tab}-page"' in pager
    js = open("trading_bot/web/static/js/main.js", encoding="utf-8").read()
    assert "const PNL_PAGE = 10;" in js
    assert "if (pnlState.len[tab] !== rows.length) { pnlState.len[tab] = rows.length; pnlState.page[tab] = 1; }" in js
    assert "$(`pnl-${tab}-pager`).hidden = pages <= 1; $(`pnl-${tab}-page`).textContent = `página ${page} de ${pages}`;" in js
    assert "pnlPaginate('details', [...(p.trades || [])].sort(byKey(pnlState.trKey, pnlState.trDir)))" in js and "pnlPaginate('income', list.sort(byKey(pnlState.inKey, pnlState.inDir)))" in js
    assert "pnlState.tab = b.dataset.tab; pnlState.page = {};" in js


def test_klines_endpoint_serves_exchange_candles_for_chart():
    """/api/klines: velas da exchange do bot (get_klines_raw, formato Binance-like) como {t,o,h,l,c,v};
    exige auth, valida símbolo/intervalo; o modal do gráfico usa Lightweight Charts (versão pinada) em vez do iframe."""
    from trading_bot.web.app import create_app
    raw = [[1_700_000_000_000, "0.50", "0.52", "0.49", "0.51", "1234.5", 1_700_000_059_999, "0", 0, "0", "0", "0"]]
    bot = _make_bot()
    bot.exchange = SimpleNamespace(get_klines_raw=MagicMock(return_value=raw))
    app, _ = create_app(bot)
    client = app.test_client()
    assert client.get("/api/klines?symbol=ADAUSDT").status_code == 401
    r = client.get("/api/klines?symbol=adausdt&limit=5000", headers=_basic_auth("admin", "secret"))
    assert r.status_code == 200
    assert r.get_json() == {"symbol": "ADAUSDT", "interval": "1m",
                            "candles": [{"t": 1_700_000_000, "o": 0.5, "h": 0.52, "l": 0.49, "c": 0.51, "v": 1234.5}]}
    bot.exchange.get_klines_raw.assert_called_once_with("ADAUSDT", "1m", 1000)
    assert client.get("/api/klines?symbol=ADA/USDT", headers=_basic_auth("admin", "secret")).status_code == 400
    assert client.get("/api/klines?symbol=ADAUSDT&interval=2m", headers=_basic_auth("admin", "secret")).status_code == 400
    html = open("trading_bot/web/templates/dashboard.html", encoding="utf-8").read()
    assert 'src="https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"' in html
    assert 'id="chart-box"' in html and "chart-frame" not in html
    js = open("trading_bot/web/static/js/main.js", encoding="utf-8").read()
    assert "fetch(`/api/klines?symbol=${encodeURIComponent(cg.sym)}&interval=1m&limit=300`" in js
    assert "try { chartSync(); }" in js and "if (cg) { clearInterval(cg.timer); cg.api.remove(); cg = null;" in js


# ---------- Plataforma › Contas (fase 2, PR B) ----------


class _FakeExchange:
    """Cliente só-leitura fake (o que ReadOnlyAccount/collect_* chamam), sem rede."""

    def __init__(self, creds, environment="demo"):
        self.creds = creds
        self.environment = environment
        self.calls = 0

    def _fetch_account(self):
        return {"wallet": 95.0, "available": 80.0, "unrealized": 1.5, "margin_balance": 96.5, "initial_margin": 15.0}

    def get_account_info(self):
        return {"wallet_balance": 95.0, "available_balance": 80.0, "unrealized_pnl": 1.5, "margin_balance": 96.5, "total_initial_margin": 15.0}

    def get_open_positions(self, force_refresh=False):
        self.calls += 1
        return [{"symbol": "ADAUSDT", "side": "LONG", "quantity": 100.0, "entry_price": 0.5, "mark_price": 0.51,
                 "unrealized_pnl": 1.0, "leverage": 10, "liquidation_price": 0.45, "margin": 5.0}]

    def get_daily_pnl_from_binance(self):
        return {"total": 0.4, "realized_pnl": 0.5, "funding_fee": -0.05, "commission": -0.05}

    def get_cumulative_income_from_binance(self, start_ms=0):
        return {"funding_fee": -0.1, "commission": -0.2}

    def get_fills(self, symbol=None, start_ms=None, end_ms=None, limit=500):
        return [{"time": end_ms - 3600000, "symbol": "ADAUSDT", "side": "sell", "action": "close", "position_side": "LONG",
                 "price": 0.52, "qty": 100.0, "notional": 52.0, "fee": -0.031, "fee_coin": "USDT", "profit": 2.0, "role": "taker",
                 "order_id": "o1", "trade_id": "t1"}]

    def get_income_history(self, **kw):
        day_ms = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        return [i for i in (_inc(day_ms - 86400000 * 40, "REALIZED_PNL", 9.0),                      # fora dos 30 dias
                            _inc(day_ms - 86400000 * 3, "REALIZED_PNL", -1.5), _inc(day_ms - 86400000 * 3, "COMMISSION", -0.05),
                            _inc(day_ms + 3600000, "REALIZED_PNL", 2.0), _inc(day_ms + 3600000, "COMMISSION", -0.031),
                            _inc(day_ms + 3600000, "FUNDING_FEE", -0.01), _inc(day_ms + 7200000, "TRANSFER", 50.0))
                if i["time"] >= kw["start_time"]]


_FAKE_KEYS = {"api_key": "bg_fake_key_AB12", "api_secret": "fake-secret", "passphrase": "fake-pass"}


def _app_with_secret(monkeypatch):
    from trading_bot.web import accounts as acc_mod
    from trading_bot.web.app import create_app
    monkeypatch.setattr(global_config, "PLATFORM_SECRET", "segredo-de-teste", raising=False)
    monkeypatch.setattr(global_config, "BITGET_API_KEY", "k1", raising=False)
    monkeypatch.setattr(global_config, "BITGET_API_SECRET", "s1", raising=False)
    monkeypatch.setattr(global_config, "BITGET_PASSPHRASE", "p1", raising=False)
    monkeypatch.setattr(global_config, "EXCHANGE", "bitget", raising=False)
    made = []
    monkeypatch.setattr(acc_mod, "exchange_client", lambda exchange, creds, environment="demo": made.append(_FakeExchange(creds, environment)) or made[-1])
    app, _ = create_app(_make_bot())
    return app, made


def test_platform_accounts_migration_public_rows_and_key_replacement(tmp_path):
    """Banco da fase 1 ganha active/last_check_*; linha pública nunca traz secret/passphrase, só os 4 últimos da key."""
    import sqlite3
    from trading_bot.web.platform import PlatformStore
    db = tmp_path / "p.db"
    first = PlatformStore(str(db), secret="s")
    uid = first.create_user("admin", "secret")
    aid = first.create_account(uid, "bitget", "principal", **_FAKE_KEYS)
    con = sqlite3.connect(db)
    con.executescript("ALTER TABLE accounts RENAME TO accounts_old;"
                      "CREATE TABLE accounts AS SELECT id, user_id, exchange, label, api_key_enc, api_secret_enc, passphrase_enc, created_at FROM accounts_old;"
                      "DROP TABLE accounts_old;")
    con.commit()
    con.close()
    store = PlatformStore(str(db), secret="s")   # migra as colunas novas
    acc = store.account(aid)
    assert acc["active"] is True and acc["has_keys"] is True and acc["key_last4"] == "AB12" and acc["owner"] == "admin"
    assert acc["environment"] == "demo" and store._environment_added is True
    store.set_account_environment(aid, "prod")
    assert store.account(aid)["environment"] == "prod"
    with pytest.raises(ValueError):
        store.set_account_environment(aid, "staging")
    assert PlatformStore(str(db), secret="s")._environment_added is False
    assert not {"api_key_enc", "api_secret_enc", "passphrase_enc", "api_key"} & set(acc)
    store.record_account_check(aid, False, "apikey inválida")
    assert store.account(aid)["last_check_ok"] is False and store.account(aid)["last_check_msg"] == "apikey inválida"
    store.set_account_keys(aid, "nova_key_ZZ99", "s2", "p2")
    acc = store.account(aid)
    assert acc["key_last4"] == "ZZ99" and acc["last_check_ok"] is None and store.account_credentials(aid)["api_secret"] == "s2"
    store.set_account_label(aid, "renomeada")
    store.set_account_active(aid, False)
    assert store.account(aid)["label"] == "renomeada" and store.account(aid)["active"] is False
    assert [a["id"] for a in store.all_accounts()] == [aid]


def test_accounts_api_crud_ownership_test_connection_and_no_secret_leak(monkeypatch):
    from trading_bot.web import accounts as acc_mod
    app, made = _app_with_secret(monkeypatch)
    store = app.config["PLATFORM"]
    uid = store.create_user("maria", "senha-forte")
    admin, maria = app.test_client(), app.test_client()
    _login(admin)
    _login(maria, user="maria", pw="senha-forte")
    assert admin.post("/api/accounts", json={}).status_code == 400   # CSRF
    assert _post(admin, "/api/accounts", {"label": "x", "api_key": "k"}).status_code == 400   # chaves incompletas
    assert _post(admin, "/api/accounts", {"exchange": "okx", **_FAKE_KEYS}).status_code == 400
    # admin cria conta para a maria
    r = _post(admin, "/api/accounts", {"label": "Bitget da Maria", "user_id": uid, **_FAKE_KEYS})
    assert r.status_code == 201
    acc = r.get_json()["account"]
    aid = acc["id"]
    assert acc["owner"] == "maria" and acc["key_last4"] == "AB12" and "api_secret" not in acc and "fake-secret" not in r.data.decode()
    # user comum só vê as próprias; admin vê todas com o dono; a conta do bot vem marcada
    mine = maria.get("/api/accounts").get_json()["accounts"]
    assert [a["id"] for a in mine] == [aid] and mine[0]["has_bot"] is False
    everything = admin.get("/api/accounts").get_json()["accounts"]
    assert [(a["id"], a["owner"], a["has_bot"]) for a in everything] == [(1, "admin", True), (aid, "maria", False)]
    # maria não mexe na conta 1 (do admin); mexe na dela
    assert _post(maria, "/api/accounts/1/label", {"label": "hack"}).status_code == 404
    assert _post(maria, f"/api/accounts/{aid}/label", {"label": "Minha Bitget"}).get_json()["account"]["label"] == "Minha Bitget"
    assert _post(maria, f"/api/accounts/{aid}/label", {"label": ""}).status_code == 400
    # testar conexão: read-only fake; resultado gravado como última checagem
    r = _post(maria, f"/api/accounts/{aid}/test", {})
    assert r.status_code == 200 and r.get_json()["ok"] is True and r.get_json()["positions"] == 1 and r.get_json()["equity"] == 96.5
    assert made[-1].creds == _FAKE_KEYS and r.get_json()["account"]["last_check_ok"] is True
    # erro da corretora volta como mensagem, sem a chave
    monkeypatch.setattr(acc_mod, "exchange_client", lambda exchange, creds, environment="demo": (_ for _ in ()).throw(ConnectionError("Não foi possível conectar à Bitget: apikey inválida")))
    r = _post(maria, f"/api/accounts/{aid}/test", {})
    assert r.get_json()["ok"] is False and "apikey inválida" in r.get_json()["error"] and "bg_fake" not in r.data.decode()
    assert r.get_json()["account"]["last_check_ok"] is False
    # substituir chaves: só os 4 últimos mudam na resposta; checagem zera
    r = _post(maria, f"/api/accounts/{aid}/keys", {"api_key": "outra_key_CD34", "api_secret": "s2", "passphrase": "p2"})
    assert r.get_json()["account"]["key_last4"] == "CD34" and r.get_json()["account"]["last_check_ok"] is None
    assert _post(maria, f"/api/accounts/{aid}/keys", {"api_key": "só-key"}).status_code == 400
    # desativar: some de /api/me e das rotas de dados; a conta do bot não desativa
    assert _post(admin, "/api/accounts/1/active", {"active": False}).status_code == 400
    assert _post(maria, f"/api/accounts/{aid}/active", {"active": False}).status_code == 200
    assert maria.get("/api/me").get_json()["accounts"][0]["active"] is False
    assert maria.get(f"/api/live?account={aid}").status_code == 404
    assert _post(maria, f"/api/accounts/{aid}/active", {"active": True}).status_code == 200


def test_account_without_bot_serves_readonly_snapshot_and_blocks_controls(monkeypatch):
    """Conta cadastrada sem bot neste processo: /api/live, /api/summary e /api/fills leem da exchange com as
    chaves da conta (handle só-leitura, cache de posições); Pausar/Retomar/Fechar respondem 409."""
    from trading_bot.web import accounts as acc_mod
    app, made = _app_with_secret(monkeypatch)
    store = app.config["PLATFORM"]
    uid = store.create_user("maria", "senha-forte")
    aid = store.create_account(uid, "bitget", "da maria", **_FAKE_KEYS)
    empty = store.create_account(uid, "bitget", "sem chaves")
    client = app.test_client()
    _login(client, user="maria", pw="senha-forte")
    html = client.get("/").data.decode()
    assert 'id="acct-sel"' in html and 'data-has-bot="0"' in html and html.split('id="acct-sel"')[1].split("</select>")[0].count("<option") == 2
    live = client.get("/api/live").get_json()   # default: 1ª conta ativa da maria (sem bot) → só-leitura
    assert len(made) == 1 and made[0].creds == _FAKE_KEYS
    assert live["summary"]["running"] is False and live["summary"]["unrealized_pnl"] == 1.5
    assert [p["symbol"] for p in live["positions"]] == ["ADAUSDT"] and live["positions"][0]["mark_price"] == 0.51
    assert live["martingale"] is None
    assert client.get(f"/api/summary?account={aid}").status_code == 200
    assert len(made) == 1 and made[0].calls <= 3   # handle reaproveitado; posições em cache de 5 s
    assert client.get(f"/api/fills?account={aid}").status_code == 200
    assert client.get(f"/api/live?account={empty}").status_code == 404   # sem chaves: nada a ler
    for path in ("/api/control/pause", "/api/control/resume", "/api/control/close_all"):
        assert client.post(f"{path}?account={aid}").status_code == 409
    assert client.post(f"/api/positions/ADAUSDT/close?account={aid}", json={"confirm": "ADAUSDT"}).status_code == 409
    # trocar as chaves descarta o handle: o próximo poll cria outro cliente com as chaves novas
    _post(client, f"/api/accounts/{aid}/keys", {"api_key": "outra_key_CD34", "api_secret": "s2", "passphrase": "p2"})
    client.get(f"/api/live?account={aid}")
    assert len(made) == 2 and made[1].creds["api_key"] == "outra_key_CD34"
    assert acc_mod.probe("bitget", {"api_key": "", "api_secret": "", "passphrase": ""})["ok"] is False


def test_readonly_account_pnl_report_and_fills_come_from_exchange(monkeypatch, _brt):
    """Conta sem bot: /api/pnl_report usa o extrato da corretora como fonte de trades (hoje/7d/30d, por símbolo,
    fechamentos) e ignora o piso PNL_SINCE_UTC do bot; /api/fills lê os fills com as chaves da conta."""
    from trading_bot.web import pnl_report
    pnl_report._memo.clear()
    monkeypatch.setattr(global_config, "PNL_SINCE_UTC", "2030-01-01T00:00:00Z", raising=False)   # piso do bot: não vale aqui
    app, made = _app_with_secret(monkeypatch)
    store = app.config["PLATFORM"]
    aid = store.create_account(store.create_user("maria", "senha-forte"), "bitget", "da maria", **_FAKE_KEYS)
    maria = app.test_client()
    _login(maria, user="maria", pw="senha-forte")
    p = maria.get(f"/api/pnl_report?account={aid}").get_json()
    assert p["income_ok"] is True and p["today_pnl"]["usd"] == pytest.approx(1.959)
    assert p["windows"]["d7"]["usd"] == pytest.approx(0.409) and p["windows"]["d30"]["usd"] == pytest.approx(0.409)
    assert p["today_pnl"]["pct"] == pytest.approx(1.959 / 96.5 * 100, rel=1e-3)   # sem snapshot: % sobre a equity ao vivo
    assert p["by_symbol"] == [{"symbol": "ADAUSDT", "net": 0.409, "trades": 2, "wins": 1, "fees": 0.081, "funding": -0.01, "win_rate": 50.0}]
    assert p["totals"]["trades"] == 2 and p["totals"]["net"] == 0.5 and p["totals"]["best"] == 2.0
    assert [t["pnl_net"] for t in p["trades"]] == [2.0, -1.5] and p["trades"][0]["qty"] is None
    assert p["income_totals"]["TRANSFER"] == 50.0 and len(p["calendar"]) == 30
    f = maria.get(f"/api/fills?account={aid}").get_json()
    assert f["total"] == 1 and f["fills"][0]["symbol"] == "ADAUSDT" and f["fills"][0]["bot_trade_id"] is None and f["profit_total"] == 2.0
    # a conta do bot (com TradeStore) continua respeitando o piso: nada do extrato entra
    admin = app.test_client()
    _login(admin)
    assert admin.get("/api/pnl_report").get_json()["today_pnl"]["usd"] == 0.0


def test_socket_connect_only_for_owner_of_the_bot_account(monkeypatch):
    """Eventos do socket são broadcast do bot deste processo: usuário sem conta com bot não conecta (fica no polling)."""
    from trading_bot.web.app import create_app
    app, socketio = create_app(_make_bot())
    store = app.config["PLATFORM"]
    store.create_user("maria", "senha-forte")
    admin, maria = app.test_client(), app.test_client()
    _login(admin)
    _login(maria, user="maria", pw="senha-forte")
    assert socketio.test_client(app, flask_test_client=admin).is_connected()
    assert not socketio.test_client(app, flask_test_client=maria).is_connected()


def test_bitget_connection_with_platform_credentials_skips_boot_probe(monkeypatch):
    from trading_bot.infra import bitget_client as bg
    monkeypatch.setattr(bg.config, "BITGET_API_KEY", "env-key", raising=False)
    monkeypatch.setattr(bg.config, "BITGET_DEMO", False, raising=False)
    monkeypatch.setattr(bg.requests.Session, "request", lambda self, *a, **k: (_ for _ in ()).throw(AssertionError("sem rede no construtor")))
    c = bg.BitgetConnection(credentials={"api_key": "acct-key", "api_secret": "acct-secret", "passphrase": "acct-pass"})
    assert c.api_key == "acct-key" and c.passphrase == "acct-pass" and c.demo is False
    # ambiente por conta: `demo` explícito vence o BITGET_DEMO do processo; None segue o processo
    assert bg.BitgetConnection(credentials={"api_key": "k", "api_secret": "s", "passphrase": "p"}, demo=True).demo is True
    assert bg.BitgetConnection(credentials={"api_key": "k", "api_secret": "s", "passphrase": "p"}, demo=None).demo is False


# ---------- Plataforma › ambiente por conta + cache por conta (fase 2b, PR C) ----------


def test_seed_sets_bot_account_environment_from_process(monkeypatch, tmp_path):
    """Conta 1 (bot) nasce no ambiente do processo (BITGET_DEMO); num banco antigo a coluna migra e o seed ajusta."""
    import sqlite3
    from trading_bot.web.platform import PlatformStore
    cfg = SimpleNamespace(DASHBOARD_USERNAME="admin", DASHBOARD_PASSWORD="secret", EXCHANGE="bitget", BITGET_DEMO=False,
                          BITGET_API_KEY="k", BITGET_API_SECRET="s", BITGET_PASSPHRASE="p")
    db = tmp_path / "p.db"
    store = PlatformStore(str(db), secret="s")
    seeded = store.seed_from_env(cfg)
    assert store.account(seeded["account_id"])["environment"] == "prod"
    # banco da fase 2 (sem a coluna) com a conta já criada: ALTER + seed põem a conta do bot no ambiente do processo
    con = sqlite3.connect(db)
    con.executescript("ALTER TABLE accounts RENAME TO o;"
                      "CREATE TABLE accounts AS SELECT id, user_id, exchange, label, api_key_enc, api_secret_enc, passphrase_enc, created_at, active FROM o;"
                      "DROP TABLE o;")
    con.commit()
    con.close()
    store = PlatformStore(str(db), secret="s")
    assert store.account(1)["environment"] == "demo"   # default da coluna, antes do seed
    store.seed_from_env(cfg)
    assert store.account(1)["environment"] == "prod"
    cfg.BITGET_DEMO = True
    store.seed_from_env(cfg)   # coluna já existia: o seed não mexe mais (idempotente)
    assert store.account(1)["environment"] == "prod"


def test_account_environment_api_probe_and_handle_use_account_environment(monkeypatch):
    from trading_bot.web import accounts as acc_mod
    app, made = _app_with_secret(monkeypatch)
    store = app.config["PLATFORM"]
    store.create_user("maria", "senha-forte")
    maria = app.test_client()
    _login(maria, user="maria", pw="senha-forte")
    assert _post(maria, "/api/accounts", {"label": "x", "environment": "staging", **_FAKE_KEYS}).status_code == 400
    r = _post(maria, "/api/accounts", {"label": "Bitget real", "environment": "prod", **_FAKE_KEYS})
    aid = r.get_json()["account"]["id"]
    assert r.get_json()["account"]["environment"] == "prod"
    store.create_account(store.user_by_name("maria")["id"], "bitget", "Bitget demo", **_FAKE_KEYS)   # 2 contas → seletor no topo
    html = maria.get("/").data.decode()
    assert 'data-env="prod">Bitget real · PROD' in html and 'data-env="demo">Bitget demo · DEMO' in html
    # Testar conexão e o handle só-leitura criam o cliente no ambiente DA CONTA
    _post(maria, f"/api/accounts/{aid}/test", {})
    assert made[-1].environment == "prod"
    maria.get(f"/api/live?account={aid}")
    assert made[-1].environment == "prod"
    # trocar o ambiente zera a checagem, descarta o handle e o próximo poll usa demo
    r = _post(maria, f"/api/accounts/{aid}/environment", {"environment": "demo"})
    assert r.status_code == 200 and r.get_json()["account"]["environment"] == "demo" and r.get_json()["account"]["last_check_ok"] is None
    maria.get(f"/api/live?account={aid}")
    assert made[-1].environment == "demo"
    assert _post(maria, f"/api/accounts/{aid}/environment", {"environment": "x"}).status_code == 400
    # a conta do bot segue o processo: não troca pelo painel
    admin = app.test_client()
    _login(admin)
    assert _post(admin, "/api/accounts/1/environment", {"environment": "prod"}).status_code == 400
    assert acc_mod.probe("bitget", _FAKE_KEYS, "prod")["ok"] is True and made[-1].environment == "prod"


def test_binance_summary_is_cached_per_account_and_rate_limits_failures(monkeypatch):
    """Duas contas (dois objetos exchange) têm caches independentes; conta com falha não é chamada de novo antes de 5 s."""
    from trading_bot.web import data as dash
    calls = {"a": 0, "b": 0}

    def ex(name, fail=False):
        def info():
            calls[name] += 1
            if fail:
                raise ConnectionError("fora")
            return {"wallet_balance": 10.0 if name == "a" else 20.0, "unrealized_pnl": 0.0}
        return SimpleNamespace(get_daily_pnl_from_binance=lambda: {"total": 1.0}, get_account_info=info)
    t = [1000.0]
    monkeypatch.setattr(dash.time, "monotonic", lambda: t[0])
    dash._binance_cache.clear()
    a, b = ex("a"), ex("b", fail=True)
    assert dash._binance_summary(a, None)["wallet_balance"] == 10.0
    assert "wallet_balance" not in dash._binance_summary(b, None)
    assert dash._binance_summary(a, None)["wallet_balance"] == 10.0 and calls == {"a": 1, "b": 1}   # cada conta no seu cache
    dash._binance_summary(b, None)
    assert calls["b"] == 1                      # falha: gap mínimo de 5 s, sem martelar a API
    t[0] += 6
    dash._binance_summary(b, None)
    assert calls["b"] == 2 and calls["a"] == 1  # b tenta de novo; a ainda dentro dos 15 s
    t[0] += 20
    dash._binance_summary(a, None)
    assert calls["a"] == 2 and id(b) not in dash._binance_cache   # entradas velhas são descartadas


def test_platform_store_reads_are_thread_safe(tmp_path):
    """Flask atende em threads com UMA conexão sqlite: leituras paralelas sem lock devolviam None (401 aleatório)."""
    import threading
    from trading_bot.web.platform import PlatformStore
    store = PlatformStore(str(tmp_path / "p.db"), secret="s")
    uid = store.create_user("admin", "secret")
    store.create_account(uid, "bitget", "a", api_key="k", api_secret="s", passphrase="p")
    misses = []

    def worker():
        for _ in range(200):
            if store.user(uid) is None or not store.accounts_for(uid) or store.user_by_name("admin") is None:
                misses.append(1)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not misses


def test_pnl_details_table_headers_are_sortable():
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    _login(client)
    thead = client.get("/").data.decode().split('id="pnl-trades"')[1].split("</thead>")[0]
    for k in ("exit_at", "symbol", "side", "qty", "entry_price", "exit_price", "pnl_net", "fees", "close_reason"):
        assert f'data-sort="{k}"' in thead and thead.count('<button>') == 9, k
    assert 'data-sort="exit_at" aria-sort="descending"' in thead
    js = open("trading_bot/web/static/js/main.js", encoding="utf-8").read()
    assert "sort(byKey(pnlState.trKey, pnlState.trDir))" in js and "#pnl-trades th[data-sort]" in js


def test_pnl_income_table_has_filters_and_sortable_headers():
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    _login(client)
    html = client.get("/").data.decode()
    tab = html.split('data-tab="income" hidden>')[1].split("</thead>")[0]
    for t in ("all", "REALIZED_PNL", "COMMISSION", "FUNDING_FEE", "TRANSFER"):
        assert f'data-type="{t}"' in tab, t
    assert 'id="pnl-income-sym"' in tab
    thead = tab.split('id="pnl-income"')[1]
    for k in ("time", "type", "symbol", "amount"):
        assert f'data-sort="{k}"' in thead, k
    assert thead.count("<button>") == 4 and 'data-sort="time" aria-sort="descending"' in thead
    js = open("trading_bot/web/static/js/main.js", encoding="utf-8").read()
    assert "sort(byKey(pnlState.inKey, pnlState.inDir))" in js and "#pnl-income th[data-sort]" in js
    assert "k === 'time'" in js and "'type']" in js  # byKey ordena a data por timestamp e Tipo como texto


# ---------- /api/simulation: página Trading › Simulações ----------


def test_simulation_route_requires_store_and_returns_scenarios(tmp_path, monkeypatch):
    from trading_bot.core.trade_store import TradeStore
    from trading_bot.web.app import create_app
    monkeypatch.setattr(global_config, "MARTINGALE_STOP_LOSS_USDT", 180.0, raising=False)
    monkeypatch.setattr(global_config, "PNL_SINCE_UTC", "", raising=False)
    bot = _make_bot(last_known_balance=325.0)
    app, _ = create_app(bot)
    client = app.test_client()
    assert client.get("/api/simulation").status_code == 401
    _login(client)
    r = client.get("/api/simulation")   # bot sem TradeStore → 400 com mensagem
    assert r.status_code == 400 and "sem trades" in r.get_json()["error"]
    store = TradeStore(str(tmp_path / "t.db"))
    for i in range(20):
        day = f"2026-11-{i + 1:02d}"
        store.record_open({"timestamp": f"{day}T01:00:00+00:00", "symbol": "ADAUSDT", "signal": "BUY", "side": "LONG", "qty": 10.0,
                           "value": 6.0, "entry_price": 0.6, "stop_loss": 0.55, "take_profit": 0.65, "strategy_name": "primary",
                           "strategy_type": "martingale_hedge", "double_first": False, "ai_consultive": {}})
        store.record_close(symbol="ADAUSDT", side="LONG", entry_price=0.6, exit_price=0.61, exit_at=f"{day}T05:00:00+00:00",
                           pnl_gross=1.05, pnl_net=1.0, fees=0.05, close_reason="tp", strategy_name="primary")
    bot.trade_store = store
    d = client.get("/api/simulation?target_brl=6000&fx=6&days=30&capital=320").get_json()
    assert d["ok"] and d["stats"]["sample_days"] == 20 and d["capital"] == 320.0 and d["fx"] == 6.0
    assert d["target"]["daily_usd"] == pytest.approx(33.33, abs=0.01) and [s["key"] for s in d["scenarios"]] == ["conservative", "observed", "optimistic"]
    assert d["required"]["capital"] == pytest.approx(10000.0, rel=1e-3) and d["risks"]["stop_usd"] == 180.0
    assert client.get("/api/simulation?target_daily=50&fx=6").get_json()["target"]["daily_usd"] == 50.0   # override
    assert client.get("/api/simulation?fx=abc").status_code == 400
    # página e menu
    html = client.get("/").data.decode()
    assert re.search(r'<a class="nav-item" href="#sim" data-page="sim" title="Simulações"><svg[^>]*>.*?</svg><span>Simulações</span></a>', html)
    for i in ("sim-form", "sim-brl", "sim-fx", "sim-days", "sim-capital", "sim-kpis", "sim-chart", "sim-table", "sim-recs"):
        assert f'id="{i}"' in html, i
    js = open("trading_bot/web/static/js/main.js", encoding="utf-8").read()
    assert "function fetchSim(" in js and "simVisible(name === 'sim')" in js
    # resposta em passos + bloco de progresso "Rumo ao capital necessário" (estado verde é só JS: capital ≥ necessário)
    for txt in ("você precisa ganhar", "O bot ganha hoje por dia", "Capital necessário na corretora", "Dias reinvestindo até lá",
                "Ciclos por dia necessários", 'id="sim-goal"', "Rumo ao capital necessário", "Capital atingido"):
        assert txt in js, txt
    assert "Precisa ganhar por dia" in html and "Dias reinvestindo</th>" in html


# ---------- /api/plan: plano de saque criado a partir da simulação ----------


def _martingale_store(tmp_path, days=20):
    from trading_bot.core.trade_store import TradeStore
    store = TradeStore(str(tmp_path / "plan.db"))
    for i in range(days):
        day = f"2026-11-{i + 1:02d}"
        store.record_open({"timestamp": f"{day}T01:00:00+00:00", "symbol": "ADAUSDT", "signal": "BUY", "side": "LONG", "qty": 10.0,
                           "value": 6.0, "entry_price": 0.6, "stop_loss": 0.55, "take_profit": 0.65, "strategy_name": "primary",
                           "strategy_type": "martingale_hedge", "double_first": False, "ai_consultive": {}})
        store.record_close(symbol="ADAUSDT", side="LONG", entry_price=0.6, exit_price=0.61, exit_at=f"{day}T05:00:00+00:00",
                           pnl_gross=1.05, pnl_net=1.0, fees=0.05, close_reason="tp", strategy_name="primary")
    return store


def test_plan_lifecycle_progress_reached_ready_withdraw_delete(tmp_path, monkeypatch):
    from trading_bot.web.app import create_app
    monkeypatch.setattr(global_config, "PNL_SINCE_UTC", "", raising=False)
    # equity = o que o bot reporta (em testnet o summary deriva o saldo do TradeStore; aqui o foco é o plano)
    monkeypatch.setattr("trading_bot.web.app.collect_summary", lambda b: {"last_balance": b.last_known_balance})
    bot = _make_bot(last_known_balance=320.0)
    bot.trade_store = _martingale_store(tmp_path)
    bot.telegram = MagicMock()
    app, _ = create_app(bot)
    client = app.test_client()
    assert client.get("/api/plan").status_code == 401
    _login(client)
    client.get("/")   # o token CSRF nasce no render da página
    hdr = {"X-CSRF-Token": _csrf(client)}
    assert client.get("/api/plan").get_json() == {"ok": True, "plan": None}
    assert client.post("/api/plan", json={"daily_target_usd": 33.33}).status_code == 400          # sem CSRF
    assert client.post("/api/plan", json={"daily_target_usd": 33.33}, headers=hdr).status_code == 400   # payload incompleto
    payload = {"monthly_target_brl": 6000, "fx": 6.0, "days_per_month": 30, "daily_target_usd": 33.33, "monthly_target_usd": 1000.0,
               "required_capital_usd": 10000.0}
    assert client.post("/api/plan", json={**payload, "required_capital_usd": 0}, headers=hdr).status_code == 400
    d = client.post("/api/plan", json=payload, headers=hdr).get_json()
    p = d["plan"]
    assert p["status"] == "active" and p["required_capital_usd"] == 10000.0 and p["monthly_target_brl"] == 6000.0 and p["withdrawals"] == []
    st = p["state"]   # equity 320 < 10000; taxa observada = 1/(320-20) = 0,333%/dia (mesma da simulação)
    assert st["equity"] == 320.0 and st["reached"] is False and st["progress_pct"] == 3.2 and st["remaining_usd"] == 9680.0
    assert st["daily_pct"] == pytest.approx(0.3333, abs=1e-3) and 1000 < st["days_left"] < 1100 and st["eta_date"]
    assert st["withdrawable_usd"] == 0.0 and st["ready_to_withdraw"] is False and st["next_withdraw_eta_days"] > st["days_left"]
    bot.telegram.send_message.assert_not_called()
    # criar de novo substitui (um plano ativo por conta)
    d2 = client.post("/api/plan", json={**payload, "required_capital_usd": 9000.0}, headers=hdr).get_json()
    assert d2["plan"]["id"] != p["id"] and d2["plan"]["required_capital_usd"] == 9000.0
    # equity acima do capital: vira reached (uma vez), avisa no Telegram uma vez, ainda não dá pra sacar a meta
    bot.last_known_balance = 9050.0
    g = client.get("/api/plan").get_json()["plan"]
    assert g["status"] == "reached" and g["reached_at"] and g["state"]["reached"] and g["state"]["days_left"] == 0
    assert g["state"]["withdrawable_usd"] == 50.0 and g["state"]["ready_to_withdraw"] is False and g["state"]["next_withdraw_eta_days"] > 0
    reached_at = g["reached_at"]
    assert client.get("/api/plan").get_json()["plan"]["reached_at"] == reached_at
    assert bot.telegram.send_message.call_count == 1 and "atingido" in bot.telegram.send_message.call_args[0][0]
    # pronto pra sacar: excedente ≥ meta mensal
    bot.last_known_balance = 10100.0
    g = client.get("/api/plan").get_json()["plan"]
    assert g["state"]["ready_to_withdraw"] is True and g["state"]["withdrawable_usd"] == 1100.0 and g["state"]["next_withdraw_eta_days"] == 0
    client.get("/api/plan")
    assert bot.telegram.send_message.call_count == 2 and "sacar" in bot.telegram.send_message.call_args[0][0]
    # registrar saque zera o ciclo (last_withdraw_at, histórico, aviso rearmado)
    assert client.post("/api/plan/withdraw", json={"amount_usd": -1}, headers=hdr).status_code == 400
    bot.last_known_balance = 9100.0   # o saque saiu da corretora
    w = client.post("/api/plan/withdraw", json={"amount_usd": 1000, "at": "2026-11-26T12:00:00+00:00"}, headers=hdr).get_json()["plan"]
    assert w["last_withdraw_at"] == "2026-11-26T12:00:00+00:00" and w["withdrawals"] == [{"at": "2026-11-26T12:00:00+00:00", "amount_usd": 1000.0}]
    assert w["status"] == "reached" and w["state"]["ready_to_withdraw"] is False and w["notified_ready"] == 0
    bot.last_known_balance = 10100.0
    client.get("/api/plan")
    assert bot.telegram.send_message.call_count == 3   # novo ciclo, novo aviso
    # cancelar
    assert client.delete("/api/plan").status_code == 400   # sem CSRF
    assert client.delete("/api/plan", headers=hdr).get_json() == {"ok": True, "plan": None}
    assert client.get("/api/plan").get_json()["plan"] is None
    assert client.post("/api/plan/withdraw", json={"amount_usd": 1}, headers=hdr).status_code == 404


def test_plan_without_store_has_no_rate_but_still_tracks_equity(tmp_path, monkeypatch):
    from trading_bot.web.app import create_app
    monkeypatch.setattr("trading_bot.web.app.collect_summary", lambda b: {"last_balance": b.last_known_balance})
    bot = _make_bot(last_known_balance=500.0)   # conta sem TradeStore (ex.: só-leitura): sem taxa, sem ETA
    app, _ = create_app(bot)
    client = app.test_client()
    _login(client)
    client.get("/")
    hdr = {"X-CSRF-Token": _csrf(client)}
    d = client.post("/api/plan", json={"days_per_month": 30, "daily_target_usd": 10, "monthly_target_usd": 300, "required_capital_usd": 1000},
                    headers=hdr).get_json()
    st = d["plan"]["state"]
    assert st["equity"] == 500.0 and st["progress_pct"] == 50.0 and st["days_left"] is None and st["eta_date"] is None and st["daily_pct"] == 0


def test_plan_ui_elements_present():
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    _login(client)
    html = client.get("/").data.decode()
    for i in ("plan-banner", "plan-card", "plan-alert", "plan-kpis", "plan-withdrawals", "plan-cancel", "sim-plan-btn"):
        assert f'id="{i}"' in html, i
    assert "Criar plano com esta simulação" in html and "Cancelar plano" in html and "Meu plano" in html
    js = open("trading_bot/web/static/js/main.js", encoding="utf-8").read()
    for txt in ("function fetchPlan(", "function renderPlan(", "/api/plan/withdraw", "apiPost('/api/plan', null, 'DELETE')", "Registrar saque",
                "Pronto para sacar", "Capital atingido em", "Próximo saque de", "Rumo ao capital necessário", "sim-plan-btn"):
        assert txt in js, txt

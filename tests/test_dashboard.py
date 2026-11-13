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
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import re
import requests

from trading_bot.core.config import config as global_config, exchange_label
from trading_bot.web import data as dashboard_data


@pytest.fixture(autouse=True)
def _dashboard_config(monkeypatch):
    """Aplica DASHBOARD_* defaults no config global pra cada teste.

    USE_TESTNET é @property derivada de ENVIRONMENT — monkeypatcham ENVIRONMENT.
    """
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
    # Topo sem Sair nem toggle de tema; relógio no lugar (main.js atualiza a cada segundo).
    assert b'action="/logout"' not in r.data and b'theme-toggle' not in r.data
    assert b'id="clock"' in r.data and b'tradingbot-theme' not in r.data


def test_martingale_dashboard_is_ring_legs_sheet_pnl_health():
    """Sem card "Escada martingale"; no martingale o main.js esconde ft/pos/equity/perf/rdist/calendário
    e move a linha de tiles pra dentro do anel (tile Equity some: o saldo já está no centro)."""
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    _login(client)
    html = client.get("/").data.decode()
    assert "mg-card" not in html and "mg-params" not in html and "Escada martingale" not in html
    for i in ("status", "tile-equity", "pos-card", "eqperf-row", "rdist-row", "legs-sum"):
        assert f'id="{i}"' in html, i
    assert html.index('id="eqperf-row"') < html.index('id="eq-card"') < html.index('id="perf-card"') < html.index("</section>", html.index('id="eqperf-row"'))
    assert html.index('id="rdist-row"') < html.index('id="cal-card"') < html.index("</section>", html.index('id="rdist-row"'))
    js = open("trading_bot/web/static/js/main.js", encoding="utf-8").read()
    assert "'tradingbot-open:' + d.id" in js
    assert "$('ft-card').hidden = $('pos-card').hidden = $('tile-equity').hidden = $('eqperf-row').hidden = $('rdist-row').hidden = !!m" in js
    assert "rc.appendChild(st)" in js and "$('mg-list').addEventListener('click', onClose)" in js


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
    assert '<a href="#legs-card">Posições</a>' in html
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


def test_mobile_first_pwa_metas_hero_and_tabbar():
    """Celular: metas de PWA/iOS, hero no topo fixo, barra inferior com âncoras para cards existentes; login com input 16px."""
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
    for href in re.findall(r'<nav class="tabbar"[^>]*>(.*?)</nav>', html, re.S)[0].split('href="')[1:]:
        assert f'id="{href.split(chr(34))[0][1:]}"' in html
    assert client.get("/static/manifest.webmanifest").status_code == 200
    assert client.get("/static/icon-180.png").status_code == 200
    css = open("trading_bot/web/static/css/login.css", encoding="utf-8").read()
    assert "font-size:16px" in css.split(".login-field input {")[1].split("}")[0]


def test_pages_are_dark_only_without_theme_script():
    """Escuro é o único tema: nenhuma página lê tradingbot-theme do localStorage nem tem toggle."""
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    login_page = client.get("/login").data
    _login(client)
    for page in (login_page, client.get("/").data):
        assert b'<meta name="color-scheme" content="dark">' in page
        assert b"prefers-color-scheme" not in page
        assert b"tradingbot-theme" not in page and b"theme-toggle" not in page
    css = open("trading_bot/web/static/css/style.css", encoding="utf-8").read()
    assert "prefers-color-scheme" not in css and 'color-scheme: dark' in css.split("[data-theme=")[0]


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
    assert client2.post("/login", data={"username": "admin", "password": "wrong", "csrf": _csrf(client2)}, headers={"X-Forwarded-For": "10.0.0.2"}).status_code == 200


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


# ---------- /api/pnl_report: aba "Ganhos e Perdas de Futuros" ----------


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


def test_pnl_report_aggregates_period_in_trading_day(_brt):
    from trading_bot.web.pnl_report import build_report

    out = build_report(_pnl_bot(), "2026-11-01", "2026-11-07")
    t = out["totals"]
    assert (t["profit"], t["loss"], t["net"], t["trades"], t["wins"]) == (3.0, -5.0, -2.0, 3, 1)
    assert t["fees"] == pytest.approx(0.8) and t["funding"] == pytest.approx(-0.05)
    assert (t["best"], t["worst"]) == (3.0, -4.0)
    # equity inicial = capital 100 + 10 do trade anterior; % = −2 / 110
    assert t["start_equity"] == 110.0 and t["pct"] == pytest.approx(-2 / 110 * 100, rel=1e-4)
    # dia de trading BRT: o trade de 03/11 01:00 UTC cai em 02/11
    assert [(d["day"], d["net"], d["trades"]) for d in out["daily"]] == [("2026-11-02", 2.0, 2), ("2026-11-05", -4.0, 1)]
    assert [(c["day"], c["value"]) for c in out["cumulative"]] == [("2026-11-02", 2.0), ("2026-11-05", -2.0)]
    assert [(s["symbol"], s["net"], s["trades"], s["win_rate"]) for s in out["by_symbol"]] == [("ETHUSDT", -4.0, 1, 0.0), ("ADAUSDT", 2.0, 2, 50.0)]
    assert [tr["symbol"] for tr in out["trades"]] == ["ETHUSDT", "ADAUSDT", "ADAUSDT"]  # saída desc
    assert out["windows"]["all"]["usd"] == 8.0


def test_pnl_report_without_exchange_or_store_is_empty_but_ok(_brt):
    from trading_bot.web.pnl_report import build_report

    out = build_report(SimpleNamespace(), None, None)
    assert out["totals"]["trades"] == 0 and out["daily"] == [] and out["trades"] == []
    assert out["income"] == [] and out["income_ok"] is False
    assert out["reference"] == {"BTCUSDT": [], "BNBUSDT": []} and out["assets"]["ok"] is False
    assert out["from"] <= out["to"] == out["today"]


def test_pnl_report_income_failure_keeps_page_alive(_brt):
    from trading_bot.web.pnl_report import build_report

    def boom(**kw):
        raise RuntimeError("binance down")

    ex = SimpleNamespace(get_income_history=boom, get_account_info=lambda: {"wallet_balance": 55.5}, market_client=None)
    out = build_report(_pnl_bot(ex), "2026-11-01", "2026-11-07")
    assert out["income_ok"] is False and out["income"] == []
    assert out["assets"] == {"ok": True, "items": [{"asset": "USDT", "balance": 55.5}], "total": 55.5}


def test_pnl_report_income_and_reference_from_binance(_brt):
    from trading_bot.web import pnl_report

    pnl_report._ref_cache.update(key=None)
    income = [
        {"time": 1793664000000, "incomeType": "FUNDING_FEE", "symbol": "ADAUSDT", "income": "-0.01"},  # 03/11 00:00 UTC
        {"time": 1793664000000, "incomeType": "COMMISSION", "symbol": "ADAUSDT", "income": "-0.2"},
        {"time": 1700000000000, "incomeType": "TRANSFER", "symbol": "", "income": "100"},  # fora da janela
    ]
    klines = [[1793491200000, 0, 0, 0, "100", 0], [1793577600000, 0, 0, 0, "110", 0]]
    ex = SimpleNamespace(
        get_income_history=lambda **kw: income,
        get_account_info=lambda: {"wallet_balance": 10.0},
        market_client=SimpleNamespace(futures_klines=lambda **kw: klines),
    )
    out = pnl_report.build_report(_pnl_bot(ex), "2026-11-01", "2026-11-07")
    assert out["income_ok"] is True and len(out["income"]) == 2
    assert out["income_totals"] == {"REALIZED_PNL": 0.0, "COMMISSION": -0.2, "FUNDING_FEE": -0.01, "TRANSFER": 0.0}
    assert [r["pct"] for r in out["reference"]["BTCUSDT"]] == [0.0, 10.0]


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

    ex = SimpleNamespace(get_income_history=fake_income, get_account_info=lambda: {}, market_client=None)
    out = build_report(_pnl_bot(ex), "2026-11-01", "2026-11-07")
    assert seen == [1793707200000]
    assert [i["symbol"] for i in out["income"]] == ["BNBUSDT"]
    assert out["income_totals"]["REALIZED_PNL"] == 1.5


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
    assert body["to"] == "2026-11-07" and body["from"] == "2026-11-01"
    assert body["totals"]["trades"] == 3


def test_pnl_report_days_preset_anchors_on_bot_trading_day(_brt):
    from trading_bot.web.pnl_report import build_report

    out = build_report(_pnl_bot(), None, None, days="30")
    assert out["to"] == out["today"] and out["from"] == (__import__("datetime").date.fromisoformat(out["today"]) - __import__("datetime").timedelta(days=29)).isoformat()
    assert build_report(_pnl_bot(), None, None, days="lixo")["from"] == (__import__("datetime").date.fromisoformat(out["today"]) - __import__("datetime").timedelta(days=6)).isoformat()


# ---------- cotação USD/BRL (trading_bot/web/fx.py) ----------


def _fx_response(bid="5.508", ts="1794234043", status=200):
    """Resposta da 1ª fonte (er-api); status != 200 levanta em raise_for_status como o requests."""
    body = {"rates": {"BRL": bid}, "time_last_update_unix": ts} if status == 200 else {"status": status, "code": "QuotaExceeded"}
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
    monkeypatch.setattr(fx.requests, "get", lambda url, timeout: calls.append(timeout) or _fx_response())
    out = fx.usd_brl()
    assert out == {"usd_brl": 5.508, "source": "er-api", "fetched_at": "2026-11-09T14:20:43+00:00"}
    assert fx.usd_brl() is out and calls == [fx.TIMEOUT]  # 2ª chamada dentro do TTL não vai à rede


def test_fx_sources_fallback_in_order(monkeypatch):
    """1ª fonte 429 (caso real da VM: awesomeapi QuotaExceeded) → usa a 2ª e guarda source."""
    from trading_bot.web import fx

    er, fr, aw = (u for _, u, _ in fx.SOURCES)
    calls = []
    _mock_get(monkeypatch, {er: _fx_response(status=429), fr: _resp({"rates": {"BRL": 5.5167}, "date": "2026-11-09"})}, calls)
    assert fx.usd_brl() == {"usd_brl": 5.5167, "source": "frankfurter", "fetched_at": "2026-11-09"}
    assert [u for u, _ in calls] == [er, fr] and all(t == fx.TIMEOUT for _, t in calls)

    monkeypatch.setitem(fx._cache, "next_at", 0.0)
    _mock_get(monkeypatch, {er: _resp({"rates": {"BRL": 5.5046}, "time_last_update_unix": 1794182551})})
    assert fx.usd_brl() == {"usd_brl": 5.5046, "source": "er-api", "fetched_at": "2026-11-09T00:02:31+00:00"}  # 1ª OK: não chama as outras

    monkeypatch.setitem(fx._cache, "next_at", 0.0)  # chave ausente (JSON de erro) e exceção também pulam pra próxima
    _mock_get(monkeypatch, {er: _resp({"result": "error"}), fr: TimeoutError("lento"), aw: _resp({"USDBRL": {"bid": "5.6", "timestamp": "1794234043"}})})
    assert fx.usd_brl() == {"usd_brl": 5.6, "source": "awesomeapi", "fetched_at": "2026-11-09T14:20:43+00:00"}


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
    assert calls == [u for _, u, _ in fx.SOURCES]  # tentou as 3 fontes, em ordem
    assert fx.usd_brl()["usd_brl"] == 5.6 and len(calls) == 3  # retry negativo: 60s sem ir à rede


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
        "FFFUSDT": sc("green", 1.0, 10.0, 2.0, volu=20e6),  # volume abaixo do piso penaliza
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
    assert 'id="sheet-card" hidden' in html and "Planilha — Binance Estratégia Padrão" in html and "CoinTech2u" not in html
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

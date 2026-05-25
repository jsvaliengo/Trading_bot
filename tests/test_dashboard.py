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

from trading_bot.core.config import config as global_config
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
    )
    return bot


def _basic_auth(user: str, pw: str) -> dict:
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


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


def test_index_requires_auth_returns_html():
    from trading_bot.web.app import create_app
    app, _ = create_app(_make_bot())
    client = app.test_client()
    r = client.get("/")
    assert r.status_code == 401
    r = client.get("/", headers=_basic_auth("admin", "secret"))
    assert r.status_code == 200
    assert b"Trading Bot" in r.data
    assert b"socket.io" in r.data.lower() or b"socket.io" in r.data


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


def test_resume_sets_paused_false():
    from trading_bot.web.app import create_app
    bot = _make_bot(paused=True)
    app, _ = create_app(bot)
    client = app.test_client()
    r = client.post("/api/control/resume", headers=_basic_auth("admin", "secret"))
    assert r.status_code == 200
    assert bot.paused is False


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
    # Binance daily_realized (5.0) + unrealized (2.0) = 7.0
    assert summary["total_pnl"] == 7.0
    assert summary["daily_pnl"] == 5.0
    assert summary["unrealized_pnl"] == 2.0
    # Equity = wallet + unrealized = 122.0
    assert summary["last_balance"] == 122.0
    # Bot counters preservados pra debug
    assert summary["bot_total_pnl"] == 20.0
    assert summary["paused"] is False
    assert summary["running"] is True
    assert summary["environment"] == "testnet"


def test_collect_summary_uses_simulated_cap_in_testnet(monkeypatch):
    """Com SIMULATED_BALANCE_USD ativo, equity = cap + daily_realized + unrealized."""
    monkeypatch.setattr(global_config, "SIMULATED_BALANCE_USD", 130.0, raising=False)
    bot = _make_bot(initial_capital=130.0)
    bot.exchange = SimpleNamespace(
        get_daily_pnl_from_binance=lambda: {"total": 1.5},
        get_account_info=lambda: {"wallet_balance": 130.0, "unrealized_pnl": -0.20},
        get_open_positions=lambda: [],
    )
    summary = dashboard_data.collect_summary(bot)
    # 130 + 1.5 - 0.20 = 131.30
    assert summary["last_balance"] == pytest.approx(131.30, abs=0.001)


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

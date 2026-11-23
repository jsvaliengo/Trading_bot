"""
Flask app factory + rotas + Socket.IO setup.

Arquitetura:
- Endpoints `/` (HTML) e `/api/snapshot` (JSON) servem leitura.
- Endpoints `/api/control/*` recebem POST para ações sensíveis (pause/resume/
  close_all). Cada um exige sessão (ou Basic Auth em /api/*) e modifica o bot.
- Socket.IO emite eventos `snapshot`, `position_opened`, `position_closed`,
  `regime_changed` quando o bot fala com `DashboardServer.emit_*()`.

O bot é passado como argumento da factory e fica em `app.config['BOT']`.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import timedelta

from flask import Flask, Response, abort, jsonify, redirect, render_template, request, session, url_for
from flask_socketio import SocketIO

from ..core.config import config, exchange_label
from . import accounts, auth, klines, users
from .auth import login_required
from .data import _deploy_info, collect_history, collect_live, collect_snapshot, collect_summary
from .platform import PlatformStore

logger = logging.getLogger(__name__)


def _static_version(static_folder: str) -> str:
    sha = str(_deploy_info().get("deploy_sha") or "")[:12]
    if sha:
        return sha
    mtimes = [
        os.path.getmtime(os.path.join(root, f)) for root, _, files in os.walk(static_folder) for f in files
    ]
    return str(int(max(mtimes))) if mtimes else "0"


def create_app(bot) -> tuple[Flask, SocketIO]:
    """Cria o app Flask + SocketIO ligado ao bot dado."""
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )

    # Validação de configuração — recusa subir sem credenciais.
    username = str(getattr(config, "DASHBOARD_USERNAME", "") or "").strip()
    password = str(getattr(config, "DASHBOARD_PASSWORD", "") or "").strip()
    if not username or not password:
        raise RuntimeError(
            "Dashboard requer DASHBOARD_USERNAME e DASHBOARD_PASSWORD definidos. "
            "Defina via env vars antes de habilitar DASHBOARD_ENABLED."
        )

    secret_key = str(getattr(config, "DASHBOARD_SECRET_KEY", "") or "").strip()
    if not secret_key:
        secret_key = secrets.token_hex(32)
        logger.warning(
            "DASHBOARD_SECRET_KEY não definida — gerada uma nova chave aleatória "
            "(sessões invalidam a cada restart)."
        )

    app.config.update(
        SECRET_KEY=secret_key,
        DASHBOARD_USERNAME=username,
        DASHBOARD_PASSWORD=password,
        DASHBOARD_TRUST_PROXY=bool(getattr(config, "DASHBOARD_TRUST_PROXY", False)),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(getattr(config, "DASHBOARD_HTTPS", False)),
        PERMANENT_SESSION_LIFETIME=timedelta(days=int(getattr(config, "DASHBOARD_SESSION_DAYS", 30) or 30)),
        LOGIN_LIMITER=auth.LoginRateLimiter(),
        BOT=bot,
        # SocketIO em modo threading (sem eventlet/gevent) pra coexistir com
        # o threading nativo do bot sem monkey-patch do stdlib.
    )

    socketio = SocketIO(
        app,
        async_mode="threading",
        cors_allowed_origins="*",  # bind 127.0.0.1 default já restringe origem
        logger=False,
        engineio_logger=False,
    )

    # ?v= fixo por processo: lido UMA vez no boot (SHA do deploy; sem deploy_info,
    # o maior mtime da pasta static). Ler o mtime a cada request fazia o processo
    # antigo (template em memória) apontar pro JS novo que o rsync acabou de trocar
    # no disco, ~10s antes do restart — boot do JS quebrava até o reload.
    app.config["STATIC_VERSION"] = _static_version(app.static_folder)

    # Plataforma: users/accounts em SQLite próprio; usuário 1 / conta 1 vêm do .env (seed idempotente).
    # Hoje o único bot do processo é a conta 1; `BOTS` é o mapa conta → bot que as rotas resolvem.
    db_path = str(getattr(config, "PLATFORM_DB_PATH", "") or "").strip() or os.path.join(
        str(getattr(config, "RUNTIME_DIR", "runtime")), "platform.db")
    platform = PlatformStore(db_path, str(getattr(config, "PLATFORM_SECRET", "") or "") or secret_key)
    seeded = platform.seed_from_env(config)
    app.config["PLATFORM"] = platform
    app.config["BOTS"] = {seeded["account_id"] or 1: bot}

    @app.context_processor
    def _static_url():
        def static_url(filename):
            return url_for("static", filename=filename, v=app.config["STATIC_VERSION"])
        return {"static_url": static_url}

    _register_routes(app, socketio)
    klines.register(app)
    users.register(app)
    accounts.register(app)
    _register_sockets(socketio)
    return app, socketio


# ----------------------------------------------------------------------
# Rotas HTTP
# ----------------------------------------------------------------------

def _register_routes(app: Flask, socketio: SocketIO) -> None:

    def account_bot(control: bool = False):
        """Bot da conta pedida (?account=N; default = a 1ª conta ativa do usuário logado). 404 se a conta
        não existe, está inativa ou não é do usuário. Conta sem bot neste processo → handle só-leitura
        (saldo/posições/fills direto da exchange); com `control=True` essa conta responde 409."""
        user = auth.current_user()
        mine = [a for a in app.config["PLATFORM"].accounts_for(user["id"]) if a["active"]]
        raw = request.args.get("account")
        try:
            account_id = int(raw) if raw else (mine[0]["id"] if mine else 0)
        except ValueError:
            abort(400)
        acc = next((a for a in mine if a["id"] == account_id), None)
        if acc is None:
            abort(404)
        if account_id in app.config["BOTS"]:
            return app.config["BOTS"][account_id]
        if control:
            abort(409)
        handles = app.config["HANDLES"]
        if account_id not in handles:
            creds = app.config["PLATFORM"].account_credentials(account_id)
            if not all(creds.values()):
                abort(404)   # sem chaves não há o que ler
            handles[account_id] = accounts.ReadOnlyAccount(accounts.exchange_client(acc["exchange"], creds, acc["environment"]))
        return handles[account_id]

    @app.route("/")
    @login_required
    def index():
        user = auth.current_user()
        return render_template(
            "dashboard.html",
            environment="testnet" if getattr(config, "USE_TESTNET", False) else "mainnet",
            exchange_label=exchange_label(config, with_network=True),
            poll_interval=int(getattr(config, "DASHBOARD_POLL_INTERVAL_SECONDS", 5)),
            strategy=str(getattr(config, "STRATEGY", "pullback") or "pullback"),
            csrf_token=auth.csrf_token(),
            user=user,
            accounts=[a for a in app.config["PLATFORM"].accounts_for(user["id"]) if a["active"]],
            bots=set(app.config["BOTS"]),
        )

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if auth.has_session():
            return redirect(auth.safe_next(request.args.get("next")))
        error, status = None, 200
        if request.method == "POST":
            ip = auth.client_ip()
            limiter = app.config["LOGIN_LIMITER"]
            user = request.form.get("username", "")[:128]
            if not auth.csrf_ok():
                error, status = "Sessão expirada, tente de novo.", 400
            elif limiter.blocked(ip) or limiter.blocked(f"user:{user}"):
                logger.warning(f"Login do dashboard bloqueado por rate limit: ip={ip} usuário={user!r}")
                error, status = "Muitas tentativas. Aguarde 15 minutos.", 429
            elif (found := auth.authenticate(user, request.form.get("password", ""))) is not None:
                limiter.reset(ip)
                limiter.reset(f"user:{user}")
                session.clear()
                session[auth.SESSION_KEY] = True
                session["user_id"] = int(found["id"])
                session["username"] = found["username"]
                session["display_name"] = found.get("display_name") or found["username"]
                session.permanent = bool(request.form.get("remember"))
                logger.info(f"Login no dashboard: usuário={user!r} ip={ip}")
                return redirect(auth.safe_next(request.form.get("next")))
            else:
                limiter.record_failure(ip)
                limiter.record_failure(f"user:{user}")
                logger.warning(f"Login do dashboard falhou: usuário={user!r} ip={ip}")
                error, status = "Usuário ou senha inválidos.", 200
        return render_template(
            "login.html",
            error=error,
            next=auth.safe_next(request.values.get("next")),
            csrf_token=auth.csrf_token(),
            username=request.form.get("username", "")[:128],
            environment="testnet" if getattr(config, "USE_TESTNET", False) else "mainnet",
        ), status

    @app.route("/logout", methods=["POST"])
    def logout():
        if auth.csrf_ok():
            session.clear()
        return redirect("/login")

    @app.route("/api/healthz")
    def healthz():
        # Health check público (sem auth) — útil para readiness probes.
        return jsonify({"ok": True})

    @app.route("/api/me")
    @login_required
    def me():
        user = auth.current_user()
        accounts = [{**a, "has_bot": a["id"] in app.config["BOTS"]} for a in app.config["PLATFORM"].accounts_for(user["id"])]
        return jsonify({"user": user, "accounts": accounts})

    @app.route("/api/snapshot")
    @login_required
    def snapshot():
        return jsonify(collect_snapshot(account_bot()))

    @app.route("/api/live")
    @login_required
    def live():
        return jsonify(collect_live(account_bot()))

    @app.route("/api/history")
    @login_required
    def history():
        return jsonify(collect_history(account_bot()))

    @app.route("/api/summary")
    @login_required
    def summary():
        return jsonify(collect_summary(account_bot()))

    @app.route("/api/market/<symbol>")
    @login_required
    def market(symbol: str):
        from .market import fetch_market
        try:
            return jsonify(fetch_market(symbol, request.args.get("interval", "1h")))
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            logger.warning(f"Mercado {symbol} indisponível: {exc}")
            return jsonify({"ok": False, "error": "Bitget não respondeu"}), 502

    @app.route("/api/fills")
    @login_required
    def fills():
        from .fills import collect_fills, fills_csv
        bot = account_bot()
        try:
            account_id = int(request.args.get("account") or 0) or next(iter(app.config["BOTS"]))
            data, everything = collect_fills(bot, account_id, request.args)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except LookupError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 501
        except Exception as exc:
            logger.warning(f"Fills indisponíveis: {exc}")
            return jsonify({"ok": False, "error": "a exchange não respondeu"}), 502
        if request.args.get("format") == "csv":
            body = fills_csv(everything)
            return Response(body, mimetype="text/csv",
                            headers={"Content-Disposition": f"attachment; filename=fills_{data['from']}_{data['to']}.csv"})
        return jsonify(data)

    @app.route("/api/pnl_report")
    @login_required
    def pnl_report():
        from .pnl_report import build_report
        return jsonify(build_report(account_bot(), request.args.get("from"), request.args.get("to"), request.args.get("days")))

    @app.route("/api/control/pause", methods=["POST"])
    @login_required
    def pause():
        bot = account_bot(control=True)
        bot.paused = True
        logger.info("⏸️  Bot pausado via dashboard")
        socketio.emit("control_changed", {"paused": True})
        return jsonify({"ok": True, "paused": True})

    @app.route("/api/control/resume", methods=["POST"])
    @login_required
    def resume():
        bot = account_bot(control=True)
        bot.paused = False
        logger.warning("▶️ Bot retomado via dashboard /api/control/resume")
        socketio.emit("control_changed", {"paused": False})
        return jsonify({"ok": True, "paused": False})

    @app.route("/api/control/close_all", methods=["POST"])
    @login_required
    def close_all():
        bot = account_bot(control=True)
        payload = request.get_json(silent=True) or {}
        reason = str(payload.get("reason", "Close all via dashboard"))[:120]
        # Mesmo método usado pelo /closeall do Telegram.
        try:
            bot._close_all_positions_daily_target(reason)
        except Exception as exc:
            logger.exception("Falha no close_all via dashboard")
            return jsonify({"ok": False, "error": str(exc)}), 500
        logger.warning(f"🚨 close_all via dashboard: {reason}")
        return jsonify({"ok": True, "reason": reason})

    @app.route("/api/positions/<symbol>/close", methods=["POST"])
    @login_required
    def close_position(symbol: str):
        bot = account_bot(control=True)
        payload = request.get_json(silent=True) or {}
        if payload.get("confirm") != symbol:
            return jsonify({"ok": False, "error": "confirm precisa ser igual ao símbolo"}), 400
        side = payload.get("side") or request.args.get("side") or None
        if side not in (None, "LONG", "SHORT"):
            return jsonify({"ok": False, "error": "side precisa ser LONG ou SHORT"}), 400
        # Mesmo caminho do exit do Donchian: fecha a mercado, registra, cancela o stop órfão.
        try:
            closed = bot.close_position_manual(symbol, "Manual (dashboard)", side=side)
        except LookupError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except Exception as exc:
            logger.exception(f"Falha no fechamento manual de {symbol} via dashboard")
            return jsonify({"ok": False, "error": str(exc)}), 500
        if not closed:
            return jsonify({"ok": False, "error": "a exchange não confirmou o fechamento"}), 502
        return jsonify({"ok": True, "symbol": symbol})


# ----------------------------------------------------------------------
# Socket.IO event handlers
# ----------------------------------------------------------------------

def _register_sockets(socketio: SocketIO) -> None:

    @socketio.on("connect")
    def on_connect():
        # Sem sessão o handshake é recusado (o cookie da sessão vem no polling do socket.io).
        # Os eventos do socket são do bot deste processo (broadcast): só quem é dono da conta dele conecta;
        # os demais usuários ficam no polling HTTP, que resolve a conta deles.
        from flask import current_app
        if not auth.has_session():
            return False
        mine = {a["id"] for a in current_app.config["PLATFORM"].accounts_for(auth.current_user()["id"])}
        if not mine & set(current_app.config["BOTS"]):
            return False
        # No connect, envia o estado vivo; o histórico o client pede por HTTP.
        bot = current_app.config["BOT"]
        socketio.emit("snapshot", collect_live(bot))

    @socketio.on("request_snapshot")
    def on_request_snapshot():
        from flask import current_app
        bot = current_app.config["BOT"]
        socketio.emit("snapshot", collect_live(bot))

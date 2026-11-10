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
import secrets
from datetime import timedelta

from flask import Flask, jsonify, redirect, render_template, request, session
from flask_socketio import SocketIO

from ..core.config import config
from . import auth
from .auth import login_required
from .data import collect_history, collect_live, collect_snapshot, collect_summary

logger = logging.getLogger(__name__)


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

    _register_routes(app, socketio)
    _register_sockets(socketio)
    return app, socketio


# ----------------------------------------------------------------------
# Rotas HTTP
# ----------------------------------------------------------------------

def _register_routes(app: Flask, socketio: SocketIO) -> None:

    @app.route("/")
    @login_required
    def index():
        return render_template(
            "dashboard.html",
            environment="testnet" if getattr(config, "USE_TESTNET", False) else "mainnet",
            poll_interval=int(getattr(config, "DASHBOARD_POLL_INTERVAL_SECONDS", 5)),
            strategy=str(getattr(config, "STRATEGY", "pullback") or "pullback"),
            csrf_token=auth.csrf_token(),
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
            elif limiter.blocked(ip):
                logger.warning(f"Login do dashboard bloqueado por rate limit: ip={ip}")
                error, status = "Muitas tentativas. Aguarde 15 minutos.", 429
            elif auth._credentials_match(user, request.form.get("password", "")):
                limiter.reset(ip)
                session.clear()
                session[auth.SESSION_KEY] = True
                session.permanent = bool(request.form.get("remember"))
                logger.info(f"Login no dashboard: usuário={user!r} ip={ip}")
                return redirect(auth.safe_next(request.form.get("next")))
            else:
                limiter.record_failure(ip)
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

    @app.route("/api/snapshot")
    @login_required
    def snapshot():
        return jsonify(collect_snapshot(app.config["BOT"]))

    @app.route("/api/live")
    @login_required
    def live():
        return jsonify(collect_live(app.config["BOT"]))

    @app.route("/api/history")
    @login_required
    def history():
        return jsonify(collect_history(app.config["BOT"]))

    @app.route("/api/summary")
    @login_required
    def summary():
        return jsonify(collect_summary(app.config["BOT"]))

    @app.route("/api/pnl_report")
    @login_required
    def pnl_report():
        from .pnl_report import build_report
        return jsonify(build_report(app.config["BOT"], request.args.get("from"), request.args.get("to"), request.args.get("days")))

    @app.route("/api/control/pause", methods=["POST"])
    @login_required
    def pause():
        bot = app.config["BOT"]
        bot.paused = True
        logger.info("⏸️  Bot pausado via dashboard")
        socketio.emit("control_changed", {"paused": True})
        return jsonify({"ok": True, "paused": True})

    @app.route("/api/control/resume", methods=["POST"])
    @login_required
    def resume():
        bot = app.config["BOT"]
        bot.paused = False
        logger.warning("▶️ Bot retomado via dashboard /api/control/resume")
        socketio.emit("control_changed", {"paused": False})
        return jsonify({"ok": True, "paused": False})

    @app.route("/api/control/close_all", methods=["POST"])
    @login_required
    def close_all():
        bot = app.config["BOT"]
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
        bot = app.config["BOT"]
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
        if not auth.has_session():
            return False
        # No connect, envia o estado vivo; o histórico o client pede por HTTP.
        from flask import current_app
        bot = current_app.config["BOT"]
        socketio.emit("snapshot", collect_live(bot))

    @socketio.on("request_snapshot")
    def on_request_snapshot():
        from flask import current_app
        bot = current_app.config["BOT"]
        socketio.emit("snapshot", collect_live(bot))

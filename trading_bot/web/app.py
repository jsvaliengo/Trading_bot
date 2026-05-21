"""
Flask app factory + rotas + Socket.IO setup.

Arquitetura:
- Endpoints `/` (HTML) e `/api/snapshot` (JSON) servem leitura.
- Endpoints `/api/control/*` recebem POST para ações sensíveis (pause/resume/
  close_all). Cada um exige Basic Auth e modifica o bot diretamente.
- Socket.IO emite eventos `snapshot`, `position_opened`, `position_closed`,
  `regime_changed` quando o bot fala com `DashboardServer.emit_*()`.

O bot é passado como argumento da factory e fica em `app.config['BOT']`.
"""

from __future__ import annotations

import logging
import secrets

from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO

from ..core.config import config
from .auth import require_basic_auth
from .data import collect_snapshot, collect_summary

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
    @require_basic_auth
    def index():
        return render_template(
            "dashboard.html",
            environment="testnet" if getattr(config, "USE_TESTNET", False) else "mainnet",
            poll_interval=int(getattr(config, "DASHBOARD_POLL_INTERVAL_SECONDS", 5)),
        )

    @app.route("/api/healthz")
    def healthz():
        # Health check público (sem auth) — útil para readiness probes.
        return jsonify({"ok": True})

    @app.route("/api/snapshot")
    @require_basic_auth
    def snapshot():
        return jsonify(collect_snapshot(app.config["BOT"]))

    @app.route("/api/summary")
    @require_basic_auth
    def summary():
        return jsonify(collect_summary(app.config["BOT"]))

    @app.route("/api/control/pause", methods=["POST"])
    @require_basic_auth
    def pause():
        bot = app.config["BOT"]
        bot.paused = True
        logger.info("⏸️  Bot pausado via dashboard")
        socketio.emit("control_changed", {"paused": True})
        return jsonify({"ok": True, "paused": True})

    @app.route("/api/control/resume", methods=["POST"])
    @require_basic_auth
    def resume():
        bot = app.config["BOT"]
        bot.paused = False
        logger.info("▶️  Bot resumido via dashboard")
        socketio.emit("control_changed", {"paused": False})
        return jsonify({"ok": True, "paused": False})

    @app.route("/api/control/close_all", methods=["POST"])
    @require_basic_auth
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


# ----------------------------------------------------------------------
# Socket.IO event handlers
# ----------------------------------------------------------------------

def _register_sockets(socketio: SocketIO) -> None:

    @socketio.on("connect")
    def on_connect():
        # No connect, envia um snapshot inicial pro client renderizar.
        from flask import current_app
        bot = current_app.config["BOT"]
        socketio.emit("snapshot", collect_snapshot(bot))

    @socketio.on("request_snapshot")
    def on_request_snapshot():
        from flask import current_app
        bot = current_app.config["BOT"]
        socketio.emit("snapshot", collect_snapshot(bot))

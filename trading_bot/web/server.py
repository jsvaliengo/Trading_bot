"""
DashboardServer — thread runner que sobe o Flask + SocketIO em background
e expõe métodos para o bot empurrar eventos em tempo real.

Lifecycle:
    server = DashboardServer(bot)
    server.start()                    # spawn thread (no-op se ENABLED=False)
    server.emit_position_opened(...)  # chamado pelo ExecutionEngine
    server.emit_position_closed(...)
    server.emit_regime_changed(...)
    server.stop()                     # join thread no shutdown
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

from ..core.config import config

logger = logging.getLogger(__name__)


class DashboardServer:
    """Wraps Flask+SocketIO numa thread daemon ligada a uma instância do bot."""

    def __init__(self, bot):
        self._bot = bot
        self._app = None
        self._socketio = None
        self._thread: Optional[threading.Thread] = None
        self._started = False
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(getattr(config, "DASHBOARD_ENABLED", False))

    def start(self) -> bool:
        """Sobe o Flask numa thread daemon. Retorna True se iniciou, False se skipped."""
        with self._lock:
            if self._started:
                return True
            if not self.enabled:
                logger.info("📊 Dashboard desabilitado (DASHBOARD_ENABLED=False) — pulando")
                return False

            try:
                from .app import create_app
                self._app, self._socketio = create_app(self._bot)
            except RuntimeError as exc:
                # Credenciais ausentes — bot continua sem dashboard.
                logger.error(f"📊 Dashboard NÃO SUBIU: {exc}")
                return False
            except Exception:
                logger.exception("📊 Falha inesperada ao criar Flask app — dashboard desabilitado")
                return False

            host = str(getattr(config, "DASHBOARD_HOST", "127.0.0.1"))
            port = int(getattr(config, "DASHBOARD_PORT", 5050))

            def _run():
                try:
                    self._socketio.run(
                        self._app,
                        host=host,
                        port=port,
                        debug=False,
                        use_reloader=False,
                        allow_unsafe_werkzeug=True,
                    )
                except Exception:
                    logger.exception("📊 Dashboard server crashed")

            self._thread = threading.Thread(target=_run, name="dashboard-server", daemon=True)
            self._thread.start()
            self._started = True
            logger.info(f"📊 Dashboard ativo em http://{host}:{port}/ (auth Basic obrigatória)")
            return True

    def stop(self) -> None:
        """No-op gracioso. Como a thread é daemon, encerra junto com o processo."""
        # Flask-SocketIO em modo threading não expõe shutdown limpo sem
        # eventlet/gevent. Para o uso real (thread daemon do bot), o sistema
        # operacional encerra junto. Mantido como hook futuro.
        return

    # ------------------------------------------------------------------
    # Hooks para o bot empurrar eventos em tempo real
    # ------------------------------------------------------------------

    def _emit(self, event: str, payload: Dict[str, Any]) -> None:
        if not self._started or self._socketio is None:
            return
        try:
            self._socketio.emit(event, payload)
        except Exception:
            # Falha de WebSocket não pode derrubar o trading.
            logger.exception(f"📊 Falha ao emitir {event} no SocketIO")

    def emit_position_opened(self, payload: Dict[str, Any]) -> None:
        self._emit("position_opened", payload)

    def emit_position_closed(self, payload: Dict[str, Any]) -> None:
        self._emit("position_closed", payload)

    def emit_regime_changed(self, payload: Dict[str, Any]) -> None:
        self._emit("regime_changed", payload)

    def emit_balance_update(self, payload: Dict[str, Any]) -> None:
        self._emit("balance_update", payload)

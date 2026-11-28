"""
Rotas do assistente da conta (docs/PLATAFORMA.md §6): POST /api/assistant e GET /api/assistant/history.

Guardrails aqui, antes de qualquer chamada ao modelo: flag `TRADING_BOT_ASSISTANT_ENABLED` (off → 404),
`login_required` + CSRF no POST, escopo da conta por `account_bot()` (404 se a conta não é do usuário),
cota de DAILY_QUOTA mensagens/usuário/dia UTC (429), 1 pedido em voo por usuário (409). A chamada ao
modelo roda num ThreadPoolExecutor(2) do módulo: mais que 2 ao mesmo tempo enfileira, sem ocupar
todos os workers do Flask. Erro do provedor → 502 e nada é gravado (a mensagem não conta na cota).
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

from flask import Flask, abort, current_app, jsonify, request

from ..ai.assistant import AccountTools, AnthropicProvider, Assistant
from ..core.config import config
from ..observability.log_redaction import redact
from . import auth
from .auth import login_required

logger = logging.getLogger(__name__)

DAILY_QUOTA = 20          # mensagens do usuário por dia UTC (PR D troca por débito no ledger)
MESSAGE_MAX_CHARS = 2000

_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="assistant")
_busy: set = set()
_busy_lock = threading.Lock()


def _bad(msg: str, status: int) -> Any:
    return jsonify({"ok": False, "error": msg}), status


def _provider() -> Optional[Any]:
    """Provider injetado (testes) ou o SDK com a chave do env; None sem chave."""
    injected = current_app.config.get("ASSISTANT_PROVIDER")
    if injected is not None:
        return injected
    key = str(getattr(config, "ANTHROPIC_API_KEY", "") or "")
    return AnthropicProvider(key) if key else None


def register(app: Flask) -> None:

    def scoped() -> tuple:
        """(bot, account_id, user) já validados; 404 com a flag desligada ou conta que não é do usuário."""
        if not bool(getattr(config, "ASSISTANT_ENABLED", False)):
            abort(404)
        user = auth.current_user()
        bot = app.config["ACCOUNT_BOT"]()          # 400/404 como as demais rotas de conta
        raw = request.args.get("account")
        account_id = int(raw) if raw else next(a["id"] for a in app.config["PLATFORM"].accounts_for(user["id"]) if a["active"])
        return bot, account_id, user

    @app.route("/api/assistant", methods=["POST"])
    @login_required
    def assistant_post():
        bot, account_id, user = scoped()
        if not auth.csrf_ok():
            return _bad("Sessão expirada, recarregue a página.", 400)
        body: Dict[str, Any] = request.get_json(silent=True) or {}
        message = str(body.get("message") or "").strip()[:MESSAGE_MAX_CHARS]
        if not message:
            return _bad("mensagem vazia", 400)
        platform = app.config["PLATFORM"]
        conversation_id = body.get("conversation_id")
        if conversation_id is not None:
            try:
                conversation_id = int(conversation_id)
            except (TypeError, ValueError):
                return _bad("conversation_id inválido", 400)
            if platform.conversation(conversation_id, user["id"], account_id) is None:
                return _bad("conversa não existe", 404)
        if platform.user_messages_today(user["id"]) >= DAILY_QUOTA:
            return _bad(f"limite de {DAILY_QUOTA} mensagens por dia atingido; volte amanhã", 429)
        provider = _provider()
        if provider is None:
            return _bad("assistente indisponível (sem chave configurada)", 502)
        with _busy_lock:
            if user["id"] in _busy:
                return _bad("aguarde a resposta anterior", 409)
            _busy.add(user["id"])
        try:
            history = platform.messages(conversation_id) if conversation_id is not None else []
            assistant = Assistant(provider, AccountTools(bot, platform, account_id))
            try:
                out = _EXECUTOR.submit(assistant.ask, history, message).result()
            except Exception as exc:
                logger.warning(f"🤖 Assistente conta {account_id} usuário {user['id']}: provedor falhou: {redact(str(exc))[:300]}")
                return _bad("o assistente não respondeu; tente de novo em instantes", 502)
        finally:
            with _busy_lock:
                _busy.discard(user["id"])
        if conversation_id is None:
            conversation_id = platform.create_conversation(user["id"], account_id, message[:80])
        platform.add_message(conversation_id, "user", message)
        platform.add_message(conversation_id, "assistant", out["reply"], out["tool_calls"],
                             out["usage"]["input_tokens"], out["usage"]["output_tokens"])
        logger.info(f"🤖 Assistente conta {account_id} usuário {user['id']}: conversa {conversation_id} ferramentas={out['tool_calls']} "
                    f"tokens in={out['usage']['input_tokens']} out={out['usage']['output_tokens']}")
        return jsonify({"ok": True, "conversation_id": conversation_id, "reply": out["reply"], "tool_calls": out["tool_calls"],
                        "usage": out["usage"], "remaining_today": max(0, DAILY_QUOTA - platform.user_messages_today(user["id"]))})

    @app.route("/api/assistant/history")
    @login_required
    def assistant_history():
        _, account_id, user = scoped()
        raw = request.args.get("conversation_id")
        if not raw:
            return jsonify({"ok": True, "conversation_id": None, "messages": []})
        try:
            conversation_id = int(raw)
        except ValueError:
            return _bad("conversation_id inválido", 400)
        if app.config["PLATFORM"].conversation(conversation_id, user["id"], account_id) is None:
            return _bad("conversa não existe", 404)
        rows = app.config["PLATFORM"].messages(conversation_id, 50)
        return jsonify({"ok": True, "conversation_id": conversation_id,
                        "messages": [{k: m[k] for k in ("id", "ts", "role", "content", "tool_calls")} for m in rows]})

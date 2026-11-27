"""
Plataforma › Usuários (admin) e Meu perfil (todos): rotas JSON do painel.

POSTs são JSON com o header `X-CSRF-Token` (o mesmo token dos forms; auth.csrf_ok). Senha gerada
pelo servidor volta UMA vez na resposta de criação/redefinição e nunca vai pro log.
"""

from __future__ import annotations

import logging
import re
import secrets

from flask import Flask, current_app, jsonify, request

from . import auth
from .auth import admin_required, login_required

logger = logging.getLogger(__name__)

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")
PASSWORD_MIN = 8


def _bad(msg: str, status: int = 400):
    return jsonify({"ok": False, "error": msg}), status


def _password_or_generated(raw) -> tuple[str, bool]:
    """(senha, gerada?): vazia → gera uma; senão valida o tamanho (ValueError)."""
    pw = str(raw or "")
    if not pw:
        return secrets.token_urlsafe(9), True
    if len(pw) < PASSWORD_MIN:
        raise ValueError(f"senha precisa ter pelo menos {PASSWORD_MIN} caracteres")
    return pw, False


def _target_user(store, user_id: int):
    """Usuário alvo de uma ação do admin; None se não existe. O admin não age sobre si mesmo."""
    target = store.user(user_id)
    if target is None:
        return None, _bad("usuário não existe", 404)
    if int(target["id"]) == int(auth.current_user()["id"]):
        return None, _bad("você não pode alterar o próprio papel ou status", 400)
    return target, None


def register(app: Flask) -> None:

    @app.before_request
    def _csrf_on_platform_posts():
        if request.method in ("POST", "DELETE") and request.path.startswith(("/api/users", "/api/me/", "/api/plan")) and not auth.csrf_ok():
            return _bad("Sessão expirada, recarregue a página.", 400)

    @app.route("/api/users")
    @admin_required
    def list_users():
        return jsonify({"users": current_app.config["PLATFORM"].users()})

    @app.route("/api/users", methods=["POST"])
    @admin_required
    def create_user():
        store = current_app.config["PLATFORM"]
        body = request.get_json(silent=True) or {}
        username = str(body.get("username") or "").strip()
        if not USERNAME_RE.match(username):
            return _bad("nome: 3 a 32 caracteres, letras, números, ponto, hífen ou _")
        if store.user_by_name(username) is not None:
            return _bad("já existe um usuário com esse nome", 409)
        try:
            password, generated = _password_or_generated(body.get("password"))
        except ValueError as exc:
            return _bad(str(exc))
        role = "admin" if body.get("role") == "admin" else "user"
        user_id = store.create_user(username, password, str(body.get("display_name") or "").strip()[:64], role=role)
        logger.info(f"👤 Usuário {username!r} ({role}) criado por {auth.current_user()['username']!r}")
        return jsonify({"ok": True, "user": store.user(user_id), "password": password if generated else None}), 201

    @app.route("/api/users/<int:user_id>/password", methods=["POST"])
    @admin_required
    def reset_password(user_id: int):
        store = current_app.config["PLATFORM"]
        if store.user(user_id) is None:
            return _bad("usuário não existe", 404)
        try:
            password, generated = _password_or_generated((request.get_json(silent=True) or {}).get("password"))
        except ValueError as exc:
            return _bad(str(exc))
        store.set_password(user_id, password)
        logger.info(f"👤 Senha do usuário {user_id} redefinida por {auth.current_user()['username']!r}")
        return jsonify({"ok": True, "password": password if generated else None})

    @app.route("/api/users/<int:user_id>/active", methods=["POST"])
    @admin_required
    def set_active(user_id: int):
        store = current_app.config["PLATFORM"]
        target, err = _target_user(store, user_id)
        if err:
            return err
        active = bool((request.get_json(silent=True) or {}).get("active"))
        store.set_active(user_id, active)
        logger.info(f"👤 Usuário {target['username']!r} {'ativado' if active else 'desativado'} por {auth.current_user()['username']!r}")
        return jsonify({"ok": True, "user": store.user(user_id)})

    @app.route("/api/users/<int:user_id>/role", methods=["POST"])
    @admin_required
    def set_role(user_id: int):
        store = current_app.config["PLATFORM"]
        target, err = _target_user(store, user_id)
        if err:
            return err
        role = str((request.get_json(silent=True) or {}).get("role") or "")
        try:
            store.set_role(user_id, role)
        except ValueError as exc:
            return _bad(str(exc))
        logger.info(f"👤 Usuário {target['username']!r} agora é {role} (por {auth.current_user()['username']!r})")
        return jsonify({"ok": True, "user": store.user(user_id)})

    @app.route("/api/me/password", methods=["POST"])
    @login_required
    def change_my_password():
        store = current_app.config["PLATFORM"]
        user = auth.current_user()
        body = request.get_json(silent=True) or {}
        new, again = str(body.get("new") or ""), str(body.get("again") or "")
        if not store.verify_password(int(user["id"]), str(body.get("current") or "")):
            return _bad("senha atual não confere", 403)
        if len(new) < PASSWORD_MIN:
            return _bad(f"a nova senha precisa ter pelo menos {PASSWORD_MIN} caracteres")
        if new != again:
            return _bad("as duas senhas novas não são iguais")
        store.set_password(int(user["id"]), new)
        logger.info(f"👤 Usuário {user['username']!r} trocou a própria senha")
        return jsonify({"ok": True})

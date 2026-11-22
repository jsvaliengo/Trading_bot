"""
Autenticação do dashboard: sessão Flask (login próprio) + Basic Auth só em /api/*.

O dashboard escuta por default só em 127.0.0.1 e o acesso real acontece via
túnel SSH ou proxy TLS. A tela de login substitui o popup de Basic Auth do
navegador; Basic Auth continua aceito em /api/* para scripts/curl.

A senha é comparada com hmac.compare_digest pra evitar timing attacks.
"""

from __future__ import annotations

import hmac
import secrets
import time
from collections import defaultdict, deque
from functools import wraps
from typing import Callable, Optional
from urllib.parse import quote

from flask import Response, abort, current_app, jsonify, redirect, request, session

SESSION_KEY = "auth"
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60


def _credentials_match(username: str, password: str) -> bool:
    expected_user = current_app.config.get("DASHBOARD_USERNAME", "")
    expected_pass = current_app.config.get("DASHBOARD_PASSWORD", "")
    # compare_digest exige strings de mesmo comprimento para ser realmente
    # constant-time, então usamos com bytes.
    user_ok = hmac.compare_digest(
        username.encode("utf-8"), expected_user.encode("utf-8")
    )
    pass_ok = hmac.compare_digest(
        password.encode("utf-8"), expected_pass.encode("utf-8")
    )
    # AVALIA AMBOS para não vazar comprimento via short-circuit.
    return user_ok and pass_ok


def authenticate(username: str, password: str) -> Optional[dict]:
    """Usuário autenticado ou None. Tabela `users` (hash) primeiro; se o nome não existe lá,
    compara com DASHBOARD_USERNAME/PASSWORD do .env (compatibilidade; vira o usuário 1 no seed)."""
    store = current_app.config.get("PLATFORM")
    if store is not None:
        user = store.verify_login(username, password)
        if user is not None:
            return user
        if store.user_by_name(username) is not None:
            return None   # existe na tabela e a senha não bateu: o .env não é segunda chance
    if _credentials_match(username, password):
        return {"id": 1, "username": username, "display_name": username}
    return None


def has_session() -> bool:
    return bool(session.get(SESSION_KEY))


ANON = {"id": 0, "username": "", "display_name": "", "role": "", "active": False}


def current_user() -> dict:
    """{id, username, display_name, role, active} da sessão ou do Basic Auth já validado. Com a tabela,
    relê a linha a cada request: papel trocado ou usuário desativado valem na hora, sem esperar o logout."""
    store = current_app.config.get("PLATFORM")
    if session.get(SESSION_KEY):
        user_id = int(session.get("user_id") or 1)
        fresh = store.user(user_id) if store is not None else None
        if fresh is not None:
            return {**fresh, "active": bool(fresh["active"])}
        return {"id": user_id, "username": str(session.get("username") or ""),
                "display_name": str(session.get("display_name") or session.get("username") or ""),
                "role": "admin" if user_id == 1 else "user", "active": True}
    auth = request.authorization
    user = authenticate(auth.username, auth.password) if auth and auth.username and auth.password else None
    if user is None:
        return dict(ANON)
    return {**user, "role": user.get("role") or ("admin" if int(user["id"]) == 1 else "user"), "active": True}


def is_admin() -> bool:
    return current_user().get("role") == "admin"


def _basic_ok() -> bool:
    auth = request.authorization
    return bool(auth and auth.username and auth.password and authenticate(auth.username, auth.password))


def _unauthorized_api() -> Response:
    response = jsonify({"ok": False, "error": "Autenticação requerida."})
    response.status_code = 401
    response.headers["WWW-Authenticate"] = 'Basic realm="Trading Bot Dashboard"'
    return response


def login_required(view: Callable) -> Callable:
    """Sessão válida em qualquer rota; Basic Auth só vale em /api/* (scripts/curl)."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if has_session():
            if not current_user()["active"]:   # desativado pelo admin com sessão aberta
                session.clear()
                return _unauthorized_api() if request.path.startswith("/api/") else redirect("/login")
            return view(*args, **kwargs)
        if request.path.startswith("/api/"):
            if _basic_ok():
                return view(*args, **kwargs)
            return _unauthorized_api()
        return redirect("/login?next=" + quote(request.full_path.rstrip("?"), safe="/"))

    return wrapped


def admin_required(view: Callable) -> Callable:
    """login_required + papel admin; 403 para os demais."""

    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not is_admin():
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def safe_next(raw: str | None) -> str:
    """Só caminho relativo interno; URL absoluta, '//host' ou '\\' viram '/'."""
    raw = (raw or "").strip()
    if not raw.startswith("/") or raw.startswith("//") or "\\" in raw or ":" in raw.split("?", 1)[0]:
        return "/"
    return raw


def client_ip() -> str:
    if current_app.config.get("DASHBOARD_TRUST_PROXY"):
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.remote_addr or "?"


def csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_hex(16)
        session["csrf"] = token
    return token


def csrf_ok() -> bool:
    """Token do form (campo `csrf`) ou do header `X-CSRF-Token` (POST JSON do painel) igual ao da sessão."""
    sent = request.form.get("csrf", "") or request.headers.get("X-CSRF-Token", "")
    return bool(sent) and hmac.compare_digest(sent, session.get("csrf", ""))


class LoginRateLimiter:
    """Falhas por chave (IP e `user:<nome>`) em memória: >= LOGIN_MAX_FAILURES na janela bloqueia o login."""

    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, ip: str, now: float) -> deque[float]:
        q = self._failures[ip]
        while q and now - q[0] > LOGIN_WINDOW_SECONDS:
            q.popleft()
        return q

    def blocked(self, ip: str) -> bool:
        return len(self._prune(ip, time.monotonic())) >= LOGIN_MAX_FAILURES

    def record_failure(self, ip: str) -> None:
        now = time.monotonic()
        self._prune(ip, now).append(now)

    def reset(self, ip: str) -> None:
        self._failures.pop(ip, None)

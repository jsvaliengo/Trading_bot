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
from typing import Callable
from urllib.parse import quote

from flask import Response, current_app, jsonify, redirect, request, session

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


def has_session() -> bool:
    return bool(session.get(SESSION_KEY))


def _basic_ok() -> bool:
    auth = request.authorization
    return bool(auth and auth.username and auth.password and _credentials_match(auth.username, auth.password))


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
            return view(*args, **kwargs)
        if request.path.startswith("/api/"):
            if _basic_ok():
                return view(*args, **kwargs)
            return _unauthorized_api()
        return redirect("/login?next=" + quote(request.full_path.rstrip("?"), safe="/"))

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
    sent = request.form.get("csrf", "")
    return bool(sent) and hmac.compare_digest(sent, session.get("csrf", ""))


class LoginRateLimiter:
    """Falhas por IP em memória: >= LOGIN_MAX_FAILURES na janela bloqueia o login."""

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

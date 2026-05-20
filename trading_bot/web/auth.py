"""
Autenticação simples via HTTP Basic Auth para o dashboard.

Por que Basic Auth e não JWT/OAuth: o dashboard escuta por default só em
127.0.0.1 e o acesso real acontece via túnel SSH ('ssh -L 5050:...'). O
adversário precisaria já estar dentro da OCI para falar com a porta. Basic
Auth + bind localhost + HTTPS via SSH é defesa em profundidade adequada
para esse modelo de ameaça.

A senha é comparada com hmac.compare_digest pra evitar timing attacks.
"""

from __future__ import annotations

import hmac
from functools import wraps
from typing import Callable

from flask import Response, current_app, request


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


def _challenge() -> Response:
    response = Response(
        "Autenticação requerida.\n", status=401, mimetype="text/plain"
    )
    response.headers["WWW-Authenticate"] = 'Basic realm="Trading Bot Dashboard"'
    return response


def require_basic_auth(view: Callable) -> Callable:
    """Decorator: protege uma rota Flask com Basic Auth contra config.DASHBOARD_*."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.authorization
        if not auth or not auth.username or not auth.password:
            return _challenge()
        if not _credentials_match(auth.username, auth.password):
            return _challenge()
        return view(*args, **kwargs)

    return wrapped

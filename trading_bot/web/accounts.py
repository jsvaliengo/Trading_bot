"""
Plataforma › Contas: rotas JSON do painel + cliente só-leitura da Bitget para contas sem bot.

Segredos: secret/passphrase nunca voltam ao browser (só `key_last4`); "Testar conexão" devolve a
mensagem da corretora, nunca a chave. Conta sem bot neste processo vira um `ReadOnlyAccount`:
o mesmo shape que `collect_*` lê do bot (exchange, known_positions, paused...), alimentado pela
Bitget com as chaves da conta. Só leitura: Pausar/Retomar/Fechar respondem 409 nessas contas.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

from flask import Flask, current_app, jsonify, request

from . import auth
from .auth import login_required

logger = logging.getLogger(__name__)

EXCHANGES = ("bitget",)
POSITIONS_TTL_S = 5.0


def _bad(msg: str, status: int = 400):
    return jsonify({"ok": False, "error": msg}), status


def exchange_client(exchange: str, creds: Dict[str, str]):
    """Cliente privado da exchange com as chaves da conta (sem testar no construtor)."""
    if exchange != "bitget":
        raise LookupError(f"exchange {exchange!r} sem cliente de plataforma")
    from ..infra.bitget_client import BitgetConnection
    return BitgetConnection(credentials=creds)


def probe(exchange: str, creds: Dict[str, str]) -> Dict[str, Any]:
    """Chamada read-only (saldo USDT-M + nº de posições). {ok, equity, available, positions} ou {ok: False, error}."""
    if not all(creds.get(k) for k in ("api_key", "api_secret", "passphrase")):
        return {"ok": False, "error": "conta sem chaves cadastradas"}
    try:
        client = exchange_client(exchange, creds)
        acct = client._fetch_account()
        positions = client.get_open_positions()
    except Exception as exc:   # BitgetApiError/ConnectionError/requests: a mensagem da corretora vai pro usuário
        return {"ok": False, "error": str(exc)[:200] or exc.__class__.__name__}
    return {"ok": True, "equity": acct["margin_balance"], "available": acct["available"], "positions": len(positions)}


class ReadOnlyAccount:
    """Handle de uma conta sem bot: o que `collect_live/summary/positions/fills` leem de um bot, mas vindo
    direto da exchange. `known_positions` = posições abertas na corretora (cache de 5 s)."""

    readonly = True
    running = False
    paused = False
    trade_store = None
    initial_capital = 0.0
    last_known_balance = 0.0
    _positions_lock = None

    def __init__(self, exchange_client) -> None:
        self.exchange = exchange_client
        self._lock = threading.Lock()
        self._positions: Dict[str, Dict[str, Any]] = {}
        self._positions_at = float("-inf")

    @property
    def known_positions(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            if time.monotonic() - self._positions_at > POSITIONS_TTL_S:
                try:
                    live = self.exchange.get_open_positions() or []
                except Exception as exc:
                    logger.warning(f"Posições da conta só-leitura indisponíveis: {exc}")
                    live = []
                self._positions = {f"{p['symbol']}_{p['side']}": {"symbol": p["symbol"], "side": p["side"],
                                   "entry_price": p.get("entry_price"), "quantity": p.get("quantity"),
                                   "strategy_name": "exchange", "strategy_type": "exchange"} for p in live}
                self._positions_at = time.monotonic()
            return dict(self._positions)


def _owned(store, account_id: int) -> Optional[Dict[str, Any]]:
    """Conta se existe e é do usuário logado (admin: qualquer); None caso contrário."""
    acc = store.account(account_id)
    user = auth.current_user()
    if acc is None or (acc["user_id"] != user["id"] and user["role"] != "admin"):
        return None
    return acc


def _keys(body: Dict[str, Any]) -> Optional[Dict[str, str]]:
    keys = {k: str(body.get(k) or "").strip() for k in ("api_key", "api_secret", "passphrase")}
    return keys if all(keys.values()) else None


def register(app: Flask) -> None:
    app.config.setdefault("HANDLES", {})   # account_id → ReadOnlyAccount (descartado ao trocar as chaves)

    @app.before_request
    def _csrf_on_account_posts():
        if request.method == "POST" and request.path.startswith("/api/accounts") and not auth.csrf_ok():
            return _bad("Sessão expirada, recarregue a página.", 400)

    @app.route("/api/accounts")
    @login_required
    def list_accounts():
        store, user = current_app.config["PLATFORM"], auth.current_user()
        rows = store.all_accounts() if user["role"] == "admin" else store.accounts_for(user["id"])
        bots = current_app.config["BOTS"]
        return jsonify({"accounts": [{**a, "has_bot": a["id"] in bots} for a in rows]})

    @app.route("/api/accounts", methods=["POST"])
    @login_required
    def create_account():
        store, user = current_app.config["PLATFORM"], auth.current_user()
        body = request.get_json(silent=True) or {}
        exchange = str(body.get("exchange") or "bitget").lower()
        if exchange not in EXCHANGES:
            return _bad("exchange não suportada")
        keys = _keys(body)
        if keys is None:
            return _bad("API key, secret e passphrase são obrigatórios")
        owner = int(body.get("user_id") or user["id"]) if user["role"] == "admin" else int(user["id"])
        if store.user(owner) is None:
            return _bad("dono não existe", 404)
        try:
            account_id = store.create_account(owner, exchange, str(body.get("label") or "").strip()[:64], **keys)
        except RuntimeError as exc:   # sem segredo da plataforma: não dá pra cifrar
            return _bad(str(exc), 503)
        logger.info(f"🧩 Conta {account_id} ({exchange}) criada para o usuário {owner} por {user['username']!r}")
        return jsonify({"ok": True, "account": store.account(account_id)}), 201

    @app.route("/api/accounts/<int:account_id>/label", methods=["POST"])
    @login_required
    def account_label(account_id: int):
        store = current_app.config["PLATFORM"]
        if _owned(store, account_id) is None:
            return _bad("conta não existe", 404)
        label = str((request.get_json(silent=True) or {}).get("label") or "").strip()[:64]
        if not label:
            return _bad("rótulo vazio")
        store.set_account_label(account_id, label)
        return jsonify({"ok": True, "account": store.account(account_id)})

    @app.route("/api/accounts/<int:account_id>/keys", methods=["POST"])
    @login_required
    def account_keys(account_id: int):
        store = current_app.config["PLATFORM"]
        if _owned(store, account_id) is None:
            return _bad("conta não existe", 404)
        keys = _keys(request.get_json(silent=True) or {})
        if keys is None:
            return _bad("API key, secret e passphrase são obrigatórios")
        try:
            store.set_account_keys(account_id, **keys)
        except RuntimeError as exc:
            return _bad(str(exc), 503)
        current_app.config["HANDLES"].pop(account_id, None)
        logger.info(f"🔐 Chaves da conta {account_id} substituídas por {auth.current_user()['username']!r}")
        return jsonify({"ok": True, "account": store.account(account_id)})

    @app.route("/api/accounts/<int:account_id>/active", methods=["POST"])
    @login_required
    def account_active(account_id: int):
        store = current_app.config["PLATFORM"]
        if _owned(store, account_id) is None:
            return _bad("conta não existe", 404)
        active = bool((request.get_json(silent=True) or {}).get("active"))
        if not active and account_id in current_app.config["BOTS"]:
            return _bad("esta conta é a do bot deste processo; não dá pra desativar pelo painel")
        store.set_account_active(account_id, active)
        logger.info(f"🧩 Conta {account_id} {'ativada' if active else 'desativada'} por {auth.current_user()['username']!r}")
        return jsonify({"ok": True, "account": store.account(account_id)})

    @app.route("/api/accounts/<int:account_id>/test", methods=["POST"])
    @login_required
    def account_test(account_id: int):
        store = current_app.config["PLATFORM"]
        acc = _owned(store, account_id)
        if acc is None:
            return _bad("conta não existe", 404)
        result = probe(acc["exchange"], store.account_credentials(account_id))
        store.record_account_check(account_id, result["ok"], "" if result["ok"] else result["error"])
        return jsonify({**result, "account": store.account(account_id)})

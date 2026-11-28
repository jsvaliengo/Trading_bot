"""
Plataforma › Contas: rotas JSON do painel + cliente só-leitura da Bitget para contas sem bot.

Segredos: secret/passphrase nunca voltam ao browser (só `key_last4`); "Testar conexão" devolve a
mensagem da corretora, nunca a chave. Conta sem bot neste processo vira um `ReadOnlyAccount`:
o mesmo shape que `collect_*` lê do bot (exchange, known_positions, paused...), alimentado pela
Bitget com as chaves da conta. Só leitura: Pausar/Retomar/Fechar respondem 409 nessas contas.
Conta com worker próprio (`runtime/accounts/N/`, fase 3) vira um `WorkerAccount`: estado e trades vêm dos
arquivos do worker e Pausar/Retomar/Fechar escrevem `control.json` (docs/PLATAFORMA.md §4).
"""

from __future__ import annotations

import glob
import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional

from flask import Flask, current_app, jsonify, request

from ..core import control_file
from ..core.bot_state_serde import BotStatePersistence
from ..core.config import config
from . import auth
from .auth import login_required
from .platform import ENVIRONMENTS

logger = logging.getLogger(__name__)

EXCHANGES = ("bitget",)
POSITIONS_TTL_S = 5.0
STATE_TTL_S = 2.0        # releitura do bot_state.*.json do worker
WORKER_STALE_S = 300.0   # log do worker parado há mais que isto = não está rodando (mesmo piso do heartbeat)
ACK_TIMEOUT_S = 15.0     # espera pelo ack de um comando em control_ack.json
ACK_POLL_S = 0.5


def _bad(msg: str, status: int = 400):
    return jsonify({"ok": False, "error": msg}), status


def exchange_client(exchange: str, creds: Dict[str, str], environment: str = "demo"):
    """Cliente privado da exchange com as chaves da conta (sem testar no construtor), no ambiente DA CONTA."""
    if exchange != "bitget":
        raise LookupError(f"exchange {exchange!r} sem cliente de plataforma")
    from ..infra.bitget_client import BitgetConnection
    return BitgetConnection(credentials=creds, demo=environment != "prod")


def probe(exchange: str, creds: Dict[str, str], environment: str = "demo") -> Dict[str, Any]:
    """Chamada read-only (saldo USDT-M + nº de posições). {ok, equity, available, positions} ou {ok: False, error}."""
    if not all(creds.get(k) for k in ("api_key", "api_secret", "passphrase")):
        return {"ok": False, "error": "conta sem chaves cadastradas"}
    try:
        client = exchange_client(exchange, creds, environment)
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


def worker_runtime_dir(account_id: int) -> Optional[str]:
    """`runtime/accounts/N` se a conta tem worker (marcador worker.enabled ou bot_state já gravado), senão None."""
    d = os.path.join(str(getattr(config, "RUNTIME_DIR", "runtime")), "accounts", str(account_id))
    if os.path.exists(os.path.join(d, "worker.enabled")) or glob.glob(os.path.join(d, "bot_state.*.json")):
        return d
    return None


def has_bot(account_id: int) -> bool:
    """Conta com bot neste processo ou worker próprio: o painel mostra os controles (data-has-bot)."""
    return account_id in current_app.config["BOTS"] or worker_runtime_dir(account_id) is not None


def _newest(pattern: str) -> Optional[str]:
    paths = glob.glob(pattern)
    return max(paths, key=os.path.getmtime) if paths else None


class WorkerAccount(ReadOnlyAccount):
    """Handle de uma conta com worker próprio (`runtime/accounts/N/`): o que `collect_*`/pnl_report leem de
    um bot, vindo dos arquivos do worker — estado do `bot_state.*.json` (cache de 2 s), trades do
    `trades.*.db` (mesmo TradeStore) — e exchange com as chaves da conta (saldo, mark price, extrato), como
    a só-leitura. Pausar/Retomar escrevem `paused` no control.json (sem ack: o worker aplica no próximo ciclo
    e salva o state); fechar posição/tudo vira comando com ack em até ACK_TIMEOUT_S."""

    readonly = False

    def __init__(self, exchange_client, runtime_dir: str) -> None:
        super().__init__(exchange_client)
        self.runtime_dir = runtime_dir
        self._state_cache: Dict[str, Any] = {}
        self._state_at = float("-inf")
        self._store = None

    @property
    def _state(self) -> Dict[str, Any]:
        with self._lock:
            if time.monotonic() - self._state_at > STATE_TTL_S:
                path = _newest(os.path.join(self.runtime_dir, "bot_state.*.json"))
                try:
                    with open(path, encoding="utf-8") as f:
                        raw = json.load(f)
                except (TypeError, OSError, ValueError):   # sem state ainda / escrita em curso
                    raw = None
                if isinstance(raw, dict):
                    self._state_cache = raw
                self._state_at = time.monotonic()
            return self._state_cache

    @property
    def trade_store(self):
        if self._store is None:
            path = _newest(os.path.join(self.runtime_dir, "trades.*.db"))
            if path is not None:   # não cria o .db no lugar do worker: sem arquivo = sem histórico ainda
                from ..core.trade_store import TradeStore
                self._store = TradeStore(path)
        return self._store

    @property
    def paused(self) -> bool:
        return bool(self._state.get("paused", True))   # worker nasce pausado (START_PAUSED)

    @paused.setter
    def paused(self, value: bool) -> None:
        control_file.write_control(self.runtime_dir, paused=bool(value))

    @property
    def running(self) -> bool:
        log = _newest(os.path.join(self.runtime_dir, "trading_bot.*.log"))
        return log is not None and time.time() - os.path.getmtime(log) < WORKER_STALE_S

    @property
    def known_positions(self) -> Dict[str, Dict[str, Any]]:
        return BotStatePersistence.deserialize_known_positions(self._state.get("known_positions"))

    @property
    def start_time(self) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(self._state["start_time"])
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def peak_equity(self) -> float:
        return float(self._state.get("martingale_peak_equity") or self._state.get("donchian_peak_equity") or 0.0)

    initial_capital = property(lambda self: float(self._state.get("initial_capital") or 0.0))
    total_pnl = property(lambda self: float(self._state.get("total_pnl") or 0.0))
    daily_realized_pnl = property(lambda self: float(self._state.get("daily_realized_pnl") or 0.0))
    closed_trades_count = property(lambda self: int(self._state.get("closed_trades_count") or 0))
    peak_prices = property(lambda self: dict(self._state.get("peak_prices") or {}))
    trail_floors = property(lambda self: dict(self._state.get("trail_floors") or {}))
    trailing_activated = property(lambda self: dict(self._state.get("trailing_activated") or {}))

    def _command(self, cmd: str, **fields: Any) -> None:
        """Escreve o comando no control.json e espera o ack do worker. RuntimeError com o erro do worker;
        TimeoutError se não respondeu em ACK_TIMEOUT_S (worker parado ou ciclo lento)."""
        cmd_id = control_file.append_command(self.runtime_dir, cmd, **fields)
        deadline = time.monotonic() + ACK_TIMEOUT_S
        while True:
            ack = control_file.find_ack(self.runtime_dir, cmd_id)
            if ack is not None:
                if not ack.get("ok"):
                    raise RuntimeError(str(ack.get("error") or "o worker recusou o comando"))
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"o worker da conta não respondeu em {ACK_TIMEOUT_S:.0f} s (comando {cmd} fica na fila)")
            time.sleep(ACK_POLL_S)

    def _close_all_positions_daily_target(self, reason: str) -> None:
        self._command("close_all", reason=reason)

    def close_position_manual(self, symbol: str, reason: str = "Manual (dashboard)", side: Optional[str] = None) -> bool:
        self._command("close_position", symbol=symbol, side=side, reason=reason)
        return True


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
        return jsonify({"accounts": [{**a, "has_bot": has_bot(a["id"])} for a in rows]})

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
        environment = str(body.get("environment") or "demo").lower()
        if environment not in ENVIRONMENTS:
            return _bad("ambiente precisa ser demo ou prod")
        try:
            account_id = store.create_account(owner, exchange, str(body.get("label") or "").strip()[:64],
                                              environment=environment, **keys)
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

    @app.route("/api/accounts/<int:account_id>/environment", methods=["POST"])
    @login_required
    def account_environment(account_id: int):
        store = current_app.config["PLATFORM"]
        if _owned(store, account_id) is None:
            return _bad("conta não existe", 404)
        if has_bot(account_id):
            return _bad("conta com bot rodando segue o ambiente do processo (BITGET_DEMO); pare o worker antes")
        environment = str((request.get_json(silent=True) or {}).get("environment") or "").lower()
        if environment not in ENVIRONMENTS:
            return _bad("ambiente precisa ser demo ou prod")
        store.set_account_environment(account_id, environment)
        current_app.config["HANDLES"].pop(account_id, None)   # o próximo poll cria o cliente no ambiente novo
        logger.info(f"🧩 Conta {account_id} agora em {environment} por {auth.current_user()['username']!r}")
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
        if not active and has_bot(account_id):
            return _bad("esta conta tem bot rodando; pare o worker antes de desativar pelo painel")
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
        result = probe(acc["exchange"], store.account_credentials(account_id), acc["environment"])
        store.record_account_check(account_id, result["ok"], "" if result["ok"] else result["error"])
        return jsonify({**result, "account": store.account(account_id)})

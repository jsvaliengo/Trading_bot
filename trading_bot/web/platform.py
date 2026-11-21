"""
Plataforma (fase 1): tabelas `users` e `accounts` em runtime/platform.db.

Hoje só existe o usuário 1 / conta 1, populados a partir do .env (seed idempotente no
boot do dashboard). O login lê a senha com hash daqui; as chaves da exchange ficam
cifradas (Fernet, chave derivada de TRADING_BOT_PLATFORM_SECRET ou, na falta, de
DASHBOARD_SECRET_KEY). Nenhuma tela de cadastro ainda — ver docs/PLATAFORMA.md.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from cryptography.fernet import Fernet, InvalidToken
from werkzeug.security import check_password_hash, generate_password_hash

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    exchange TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    api_key_enc TEXT NOT NULL DEFAULT '',
    api_secret_enc TEXT NOT NULL DEFAULT '',
    passphrase_enc TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


def fernet_from_secret(secret: str) -> Optional[Fernet]:
    """Chave Fernet determinística a partir de um segredo textual; None sem segredo."""
    secret = (secret or "").strip()
    if not secret:
        return None
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest()))


class PlatformStore:
    def __init__(self, db_path: str, secret: str = "") -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._fernet = fernet_from_secret(secret)

    # ---- cifra ----
    def encrypt(self, plain: str) -> str:
        if not plain:
            return ""
        if self._fernet is None:
            raise RuntimeError("sem TRADING_BOT_PLATFORM_SECRET/DASHBOARD_SECRET_KEY: não dá pra cifrar chaves")
        return self._fernet.encrypt(plain.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        if not token or self._fernet is None:
            return ""
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken:
            logger.error("🔐 Chave da conta não decifra com o segredo atual (segredo mudou?)")
            return ""

    # ---- usuários ----
    def create_user(self, username: str, password: str, display_name: str = "") -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO users (username, password_hash, display_name, created_at) VALUES (?, ?, ?, ?)",
                (username, generate_password_hash(password), display_name or username, now),
            )
            return int(cur.lastrowid)

    def user_by_name(self, username: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None

    def verify_login(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        """Usuário (sem o hash) se a senha bate; None se não existe ou não bate."""
        row = self.user_by_name(username)
        if not row or not check_password_hash(row["password_hash"], password):
            return None
        return {k: v for k, v in row.items() if k != "password_hash"}

    # ---- contas ----
    def create_account(self, user_id: int, exchange: str, label: str = "", api_key: str = "",
                       api_secret: str = "", passphrase: str = "") -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO accounts (user_id, exchange, label, api_key_enc, api_secret_enc, passphrase_enc, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, exchange, label or exchange, self.encrypt(api_key), self.encrypt(api_secret),
                 self.encrypt(passphrase), now),
            )
            return int(cur.lastrowid)

    def account(self, account_id: int) -> Optional[Dict[str, Any]]:
        row = self._conn.execute("SELECT id, user_id, exchange, label, created_at FROM accounts WHERE id = ?",
                                 (account_id,)).fetchone()
        return dict(row) if row else None

    def accounts_for(self, user_id: int) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, user_id, exchange, label, created_at FROM accounts WHERE user_id = ? ORDER BY id", (user_id,))
        return [dict(r) for r in rows]

    def account_credentials(self, account_id: int) -> Dict[str, str]:
        row = self._conn.execute("SELECT api_key_enc, api_secret_enc, passphrase_enc FROM accounts WHERE id = ?",
                                 (account_id,)).fetchone()
        if not row:
            return {}
        return {"api_key": self.decrypt(row["api_key_enc"]), "api_secret": self.decrypt(row["api_secret_enc"]),
                "passphrase": self.decrypt(row["passphrase_enc"])}

    # ---- seed ----
    def seed_from_env(self, config) -> Dict[str, int]:
        """Migração da fase 1: usuário 1 = login do dashboard (.env), conta 1 = exchange ativa com as
        chaves do .env. Idempotente: só cria o que falta; nunca sobrescreve senha ou chaves já gravadas."""
        username = str(getattr(config, "DASHBOARD_USERNAME", "") or "").strip()
        password = str(getattr(config, "DASHBOARD_PASSWORD", "") or "")
        user = self.user_by_name(username) if username else None
        user_id = int(user["id"]) if user else (self.create_user(username, password) if username and password else 0)
        if not user_id:
            return {"user_id": 0, "account_id": 0}
        existing = self.accounts_for(user_id)
        if existing:
            return {"user_id": user_id, "account_id": int(existing[0]["id"])}
        exchange = str(getattr(config, "EXCHANGE", "binance") or "binance").lower()
        prefix = exchange.upper()
        creds = {k: str(getattr(config, f"{prefix}_{k.upper()}", "") or "") for k in ("api_key", "api_secret", "passphrase")}
        if self._fernet is None and any(creds.values()):
            logger.warning("🔐 Sem segredo da plataforma: conta 1 criada sem as chaves (defina TRADING_BOT_PLATFORM_SECRET)")
            creds = {k: "" for k in creds}
        account_id = self.create_account(user_id, exchange, label=f"{exchange} principal", **creds)
        logger.info(f"🧩 Plataforma: usuário {user_id} ({username}) e conta {account_id} ({exchange}) criados do .env")
        return {"user_id": user_id, "account_id": account_id}

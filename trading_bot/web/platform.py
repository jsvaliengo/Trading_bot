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
    created_at TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    exchange TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    api_key_enc TEXT NOT NULL DEFAULT '',
    api_secret_enc TEXT NOT NULL DEFAULT '',
    passphrase_enc TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    last_check_at TEXT,
    last_check_ok INTEGER,
    last_check_msg TEXT NOT NULL DEFAULT ''
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
        self._migrate()
        self._fernet = fernet_from_secret(secret)

    def _migrate(self) -> None:
        """Colunas novas em bancos da fase 1 (ALTER idempotente); o usuário 1 é sempre admin."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(users)")}
        with self._conn:
            if "role" not in cols:
                self._conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
            if "active" not in cols:
                self._conn.execute("ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
            self._conn.execute("UPDATE users SET role = 'admin' WHERE id = 1")
            acols = {r["name"] for r in self._conn.execute("PRAGMA table_info(accounts)")}
            for col, ddl in (("active", "INTEGER NOT NULL DEFAULT 1"), ("last_check_at", "TEXT"),
                             ("last_check_ok", "INTEGER"), ("last_check_msg", "TEXT NOT NULL DEFAULT ''")):
                if col not in acols:
                    self._conn.execute(f"ALTER TABLE accounts ADD COLUMN {col} {ddl}")

    def _select(self, sql: str, params: tuple = ()) -> list:
        """Leitura serializada: a conexão é uma só (check_same_thread=False) e o Flask atende em threads —
        duas consultas em paralelo no mesmo cursor devolviam None uma pra outra (401 aleatório no painel)."""
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

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
    def create_user(self, username: str, password: str, display_name: str = "", role: str = "user") -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO users (username, password_hash, display_name, created_at, role) VALUES (?, ?, ?, ?, ?)",
                (username, generate_password_hash(password), display_name or username, now, role),
            )
            user_id = int(cur.lastrowid)
            if user_id == 1:
                self._conn.execute("UPDATE users SET role = 'admin' WHERE id = 1")
            return user_id

    def user_by_name(self, username: str) -> Optional[Dict[str, Any]]:
        rows = self._select("SELECT * FROM users WHERE username = ?", (username,))
        return dict(rows[0]) if rows else None

    def user(self, user_id: int) -> Optional[Dict[str, Any]]:
        """Usuário sem o hash; None se não existe."""
        rows = self._select("SELECT id, username, display_name, created_at, role, active FROM users WHERE id = ?", (user_id,))
        return dict(rows[0]) if rows else None

    def users(self) -> List[Dict[str, Any]]:
        rows = self._select(
            "SELECT u.id, u.username, u.display_name, u.created_at, u.role, u.active,"
            " (SELECT COUNT(*) FROM accounts a WHERE a.user_id = u.id) AS accounts FROM users u ORDER BY u.id")
        return [dict(r) for r in rows]

    def verify_login(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        """Usuário (sem o hash) se a senha bate e está ativo; None se não existe, não bate ou inativo."""
        row = self.user_by_name(username)
        if not row or not row["active"] or not check_password_hash(row["password_hash"], password):
            return None
        return {k: v for k, v in row.items() if k != "password_hash"}

    def verify_password(self, user_id: int, password: str) -> bool:
        rows = self._select("SELECT password_hash FROM users WHERE id = ?", (user_id,))
        return bool(rows) and check_password_hash(rows[0]["password_hash"], password)

    def set_password(self, user_id: int, password: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(password), user_id))

    def set_active(self, user_id: int, active: bool) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE users SET active = ? WHERE id = ?", (1 if active else 0, user_id))

    def set_role(self, user_id: int, role: str) -> None:
        if role not in ("admin", "user"):
            raise ValueError("papel precisa ser admin ou user")
        with self._lock, self._conn:
            self._conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))

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

    _ACCOUNT_COLS = ("a.id, a.user_id, a.exchange, a.label, a.created_at, a.active, a.last_check_at, a.last_check_ok,"
                     " a.last_check_msg, a.api_key_enc, u.username AS owner")

    def _account_row(self, row) -> Dict[str, Any]:
        """Linha pública da conta: nunca secret/passphrase; da key só os 4 últimos caracteres."""
        key = self.decrypt(row["api_key_enc"])
        out = {k: row[k] for k in row.keys() if k != "api_key_enc"}
        out.update(active=bool(row["active"]), has_keys=bool(key), key_last4=key[-4:] if key else "",
                   last_check_ok=None if row["last_check_ok"] is None else bool(row["last_check_ok"]))
        return out

    def account(self, account_id: int) -> Optional[Dict[str, Any]]:
        rows = self._select(f"SELECT {self._ACCOUNT_COLS} FROM accounts a JOIN users u ON u.id = a.user_id WHERE a.id = ?",
                            (account_id,))
        return self._account_row(rows[0]) if rows else None

    def accounts_for(self, user_id: int) -> List[Dict[str, Any]]:
        rows = self._select(f"SELECT {self._ACCOUNT_COLS} FROM accounts a JOIN users u ON u.id = a.user_id"
                            " WHERE a.user_id = ? ORDER BY a.id", (user_id,))
        return [self._account_row(r) for r in rows]

    def all_accounts(self) -> List[Dict[str, Any]]:
        rows = self._select(f"SELECT {self._ACCOUNT_COLS} FROM accounts a JOIN users u ON u.id = a.user_id ORDER BY a.id")
        return [self._account_row(r) for r in rows]

    def set_account_label(self, account_id: int, label: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE accounts SET label = ? WHERE id = ?", (label, account_id))

    def set_account_keys(self, account_id: int, api_key: str, api_secret: str, passphrase: str) -> None:
        """Substitui as 3 chaves de uma vez (cifradas) e zera a última checagem."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE accounts SET api_key_enc = ?, api_secret_enc = ?, passphrase_enc = ?, last_check_at = NULL,"
                " last_check_ok = NULL, last_check_msg = '' WHERE id = ?",
                (self.encrypt(api_key), self.encrypt(api_secret), self.encrypt(passphrase), account_id))

    def set_account_active(self, account_id: int, active: bool) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE accounts SET active = ? WHERE id = ?", (1 if active else 0, account_id))

    def record_account_check(self, account_id: int, ok: bool, msg: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE accounts SET last_check_at = ?, last_check_ok = ?, last_check_msg = ? WHERE id = ?",
                               (datetime.now(timezone.utc).isoformat(), 1 if ok else 0, msg[:200], account_id))

    def account_credentials(self, account_id: int) -> Dict[str, str]:
        rows = self._select("SELECT api_key_enc, api_secret_enc, passphrase_enc FROM accounts WHERE id = ?", (account_id,))
        if not rows:
            return {}
        row = rows[0]
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

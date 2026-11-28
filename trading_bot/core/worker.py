"""
Worker por conta (fase 3): um processo do bot para uma conta do platform.db.

    python -m trading_bot.core.worker <account_id>      # ou TRADING_BOT_ACCOUNT_ID=<id>
    TRADING_BOT_ACCOUNT_DRY=1 python -m trading_bot.core.worker 2   # só imprime o env (mascarado)

O `config` do bot é um singleton montado do `os.environ` no import, e `load_dotenv(override=False)`
faz o que já está no ambiente vencer o .env. Então basta: ler as chaves da conta no platform.db
(decifradas só em memória), popular `os.environ` e SÓ DEPOIS importar `trading_bot.core.bot`.
Por isso este módulo não importa `config` nem `bot` no topo.

O worker força: chaves Bitget da conta, runtime isolado em `runtime/accounts/<id>/` (lock, state,
trades.db, log), Telegram/relatório diário/dashboard/métricas desligados e nasce pausado. O resto do
perfil (MARTINGALE_*, STRATEGY, TRADING_BOT_ENVIRONMENT...) herda do .env, igual à conta 1.
A conta 1 continua no processo principal (`python -m trading_bot.core.bot`) e é recusada aqui.
Nunca escreve segredo em disco nem no log.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SECRET_KEYS = ("BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_PASSPHRASE")


def _load_env_file() -> None:
    """Mesma resolução de config._load_environment(): TRADING_BOT_ENV_FILE, senão .env e .env.local."""
    explicit = os.getenv("TRADING_BOT_ENV_FILE", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        load_dotenv(dotenv_path=path if path.is_absolute() else PROJECT_ROOT / path, override=False)
        return
    for candidate in (PROJECT_ROOT / ".env", PROJECT_ROOT / ".env.local"):
        if candidate.exists():
            load_dotenv(dotenv_path=candidate, override=False)


def _mask(value: str) -> str:
    return f"***{value[-4:]}" if value else ""


def build_env(account_id: int) -> dict[str, str]:
    """Env que o processo da conta precisa por cima do .env. Aborta (SystemExit) se a conta não serve."""
    from trading_bot.web.platform import PlatformStore   # não importa config

    if account_id == 1:
        raise SystemExit("conta 1 roda no processo principal (python -m trading_bot.core.bot), não no worker")
    runtime_base = os.getenv("TRADING_BOT_RUNTIME_DIR", "runtime")
    db_path = os.getenv("TRADING_BOT_PLATFORM_DB_PATH", "").strip() or os.path.join(runtime_base, "platform.db")
    secret = os.getenv("TRADING_BOT_PLATFORM_SECRET", "").strip() or os.getenv("TRADING_BOT_DASHBOARD_SECRET_KEY", "").strip()
    store = PlatformStore(db_path, secret)
    acc = store.account(account_id)
    if acc is None:
        raise SystemExit(f"conta {account_id} não existe em {db_path}")
    if not acc["active"]:
        raise SystemExit(f"conta {account_id} está inativa")
    if acc["exchange"] != "bitget":
        raise SystemExit(f"conta {account_id} é {acc['exchange']}; o worker só suporta bitget")
    creds = store.account_credentials(account_id)
    if not all(creds.get(k) for k in ("api_key", "api_secret", "passphrase")):
        raise SystemExit(f"conta {account_id} sem chaves (vazias ou segredo da cifra errado)")

    runtime_dir = os.path.join(runtime_base, "accounts", str(account_id))
    # shortcut: perfil por conta = perfil do .env; upgrade = coluna accounts.profile_json → os.environ
    return {
        "BITGET_API_KEY": creds["api_key"],
        "BITGET_API_SECRET": creds["api_secret"],
        "BITGET_PASSPHRASE": creds["passphrase"],
        "BITGET_DEMO": "false" if acc["environment"] == "prod" else "true",
        "TRADING_BOT_EXCHANGE": "bitget",
        "TRADING_BOT_RUNTIME_DIR": runtime_dir,
        "TRADING_BOT_TELEGRAM_ENABLED": "false",
        "TELEGRAM_TOKEN": "",
        "TELEGRAM_CHAT_ID": "",
        "TRADING_BOT_DAILY_REPORT_ENABLED": "false",
        "TRADING_BOT_DASHBOARD_ENABLED": "false",
        "TRADING_BOT_METRICS_ENABLED": "false",
        "TRADING_BOT_START_PAUSED": "true",
        "TRADING_BOT_CONTROL_FILE_ENABLED": "true",   # sem efeito ainda; o canal control.json vem no PR 2
    }


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    raw = argv[0] if argv else os.getenv("TRADING_BOT_ACCOUNT_ID", "")
    try:
        account_id = int(raw)
    except ValueError:
        raise SystemExit("uso: python -m trading_bot.core.worker <account_id>  (ou TRADING_BOT_ACCOUNT_ID)")

    _load_env_file()
    env = build_env(account_id)
    os.makedirs(env["TRADING_BOT_RUNTIME_DIR"], exist_ok=True)
    os.environ.update(env)

    if os.getenv("TRADING_BOT_ACCOUNT_DRY", "").strip().lower() in {"1", "true", "yes", "on"}:
        for key, value in env.items():
            print(f"{key}={_mask(value) if key in SECRET_KEYS else value}")
        return 0

    from trading_bot.core.bot import main as bot_main   # só agora: config lê o env já populado
    bot_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

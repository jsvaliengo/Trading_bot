#!/usr/bin/env python3
"""
Heartbeat / watchdog EXTERNO do trading bot.

Roda via cron (a cada ~2min) FORA do processo do bot — assim detecta tanto
processo morto quanto processo ZUMBI (vivo, mas sem ciclar; ver incidente
15/06/2026, em que o bot ficou ~2h travado e ninguém soube). Avisa no Telegram
só na TRANSIÇÃO de estado (saudável↔caído), com re-aviso a cada RENOTIFY_MINUTES
enquanto seguir caído. Sem dependência das libs do bot — lê o .env direto, então
funciona mesmo se o código do bot estiver quebrado.

Sinais de saúde (precisa dos dois):
  1. processo `-m trading_bot.core.bot` vivo (pgrep);
  2. o log ativo foi escrito nos últimos STALE_MINUTES (o bot escreve a cada
     poucos segundos quando saudável → mtime velho = travado/zumbi).

Workers por conta (fase 3): para cada `runtime/accounts/N/worker.enabled` vale a
mesma regra (processo `-m trading_bot.core.worker N` + mtime do log mais novo em
`runtime/accounts/N/`), com estado e re-aviso independentes e o rótulo "conta N"
na mensagem. Sem marcador = não vigia (e esquece o estado); sem `runtime/accounts/`
nada muda.

Uso:
  python scripts/heartbeat.py          # checagem (pro cron)
  python scripts/heartbeat.py --test   # envia uma mensagem de teste e sai
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STALE_MINUTES = float(os.getenv("HEARTBEAT_STALE_MINUTES", "5"))
RENOTIFY_MINUTES = float(os.getenv("HEARTBEAT_RENOTIFY_MINUTES", "30"))
STATE_PATH = ROOT / "runtime" / "heartbeat_state.json"
ACCOUNTS_DIR = ROOT / "runtime" / "accounts"


def read_env() -> dict:
    """Parser mínimo do .env (sem depender de libs do bot)."""
    env = {}
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            env[key.strip()] = val.split("#", 1)[0].strip()
    except OSError:
        pass
    return env


ENV = read_env()
TOKEN = ENV.get("TELEGRAM_TOKEN", "")
CHAT = ENV.get("TELEGRAM_CHAT_ID", "")
APP_ENV = ENV.get("TRADING_BOT_ENV", "prod") or "prod"
NET = ENV.get("TRADING_BOT_ENVIRONMENT", "testnet") or "testnet"
BADGE = (ENV.get("TRADING_BOT_EXCHANGE", "binance") or "binance").capitalize()
LOG_PATH = ROOT / "runtime" / f"trading_bot.{APP_ENV}.{NET}.log"


def send_telegram(text: str) -> None:
    if not TOKEN or not CHAT:
        print("heartbeat: TELEGRAM_TOKEN/CHAT_ID ausentes — não enviei.", file=sys.stderr)
        return
    data = urllib.parse.urlencode(
        {"chat_id": CHAT, "text": text, "parse_mode": "HTML"}
    ).encode()
    try:
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data, timeout=10
        )
    except Exception as exc:  # noqa: BLE001
        print(f"heartbeat: falha ao enviar Telegram: {exc}", file=sys.stderr)


def bot_process_alive() -> bool:
    return subprocess.run(
        ["pgrep", "-f", r"trading_bot\.core\.bot"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def log_age_minutes() -> float:
    try:
        return (time.time() - LOG_PATH.stat().st_mtime) / 60.0
    except OSError:
        return 1e9  # log inexistente = problema


def enabled_accounts() -> list[int]:
    return sorted(
        int(p.parent.name)
        for p in ACCOUNTS_DIR.glob("[0-9]*/worker.enabled")
        if p.parent.name.isdigit() and p.parent.name != "1"  # 1 = conta do processo principal
    )


def worker_process_alive(account_id: int) -> bool:
    # `(\s|$)` evita que "worker 2" case "worker 22"; sem "-m" na frente: pgrep leria como opção.
    return subprocess.run(
        ["pgrep", "-f", rf"trading_bot\.core\.worker +{account_id}(\s|$)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode == 0


def worker_log_age_minutes(account_dir: Path) -> float:
    mtimes = [p.stat().st_mtime for p in account_dir.glob("trading_bot.*.log")]
    return (time.time() - max(mtimes)) / 60.0 if mtimes else 1e9


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def check(state: dict, alive: bool, age: float, now: float, label: str = "") -> dict:
    """Avisa na transição (e re-avisa caído); devolve o novo estado. label="" = conta 1."""
    healthy = alive and age <= STALE_MINUTES
    was_healthy = state.get("healthy", True)
    last_alert = float(state.get("last_alert_ts", 0) or 0)

    if not healthy:
        reason = "processo MORTO" if not alive else f"sem ciclar há ~{age:.0f} min (travado/zumbi)"
        transition = was_healthy
        renotify = (now - last_alert) >= RENOTIFY_MINUTES * 60.0
        if transition or renotify:
            send_telegram(
                f"🔴 <b>BOT CAIU</b> [{BADGE}]{label}\n"
                f"⚠️ {reason}\n"
                f"Verifique a VM (oci-bot)."
            )
            last_alert = now
    elif not was_healthy:
        send_telegram(f"🟢 <b>BOT RECUPERADO</b> [{BADGE}]{label}\nVoltou a ciclar normalmente.")
    return {"healthy": healthy, "last_alert_ts": last_alert, "ts": now}


def main() -> int:
    if "--test" in sys.argv:
        send_telegram(f"🩺 <b>Heartbeat ativo</b> [{BADGE}]\nMonitorando o bot — você será avisado se ele cair ou travar.")
        print("heartbeat: mensagem de teste enviada.")
        return 0

    now = time.time()
    state = load_state()
    alive = bot_process_alive()
    age = log_age_minutes()
    new_state = check(state, alive, age, now)
    print(f"heartbeat: {'OK' if new_state['healthy'] else 'DOWN'} | proc={alive} log_age={age:.1f}min net={NET}")

    accounts = {}
    for n in enabled_accounts():
        a_alive = worker_process_alive(n)
        a_age = worker_log_age_minutes(ACCOUNTS_DIR / str(n))
        accounts[str(n)] = check(state.get("accounts", {}).get(str(n), {}), a_alive, a_age, now, f" conta {n}")
        print(f"heartbeat: conta {n} {'OK' if accounts[str(n)]['healthy'] else 'DOWN'} | proc={a_alive} log_age={a_age:.1f}min")
    if accounts:
        new_state["accounts"] = accounts
    save_state(new_state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

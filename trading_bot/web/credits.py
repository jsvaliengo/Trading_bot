"""
Créditos pré-pagos ("gás") por conta com worker (docs/PLATAFORMA.md §4, PR 6).

Consumo = `accounts.credit_pct`% do lucro LÍQUIDO de cada ciclo positivo do martingale. Um ciclo é uma
linha de `trades` (a base abre a linha em `record_trade_opened`; o TP fecha a mesma linha), então a
chave do ciclo é `"{id}@{opened_at}"` (o opened_at protege contra ids reiniciados por um reset do
trades.db). O ledger é DERIVADO das linhas fechadas do `trades.*.db` da conta (fonte
da verdade), idempotente por (account_id, ref=id), e só olha ciclos fechados há mais de GRACE_S: o
pnl_reconciler do worker ainda reescreve pnl_net/fees nos primeiros minutos.

Trava: saldo ≤ 0 → `new_cycles=false` no control.json da conta (o martingale não abre ciclo novo;
os abertos terminam); saldo > 0 → `true`. Só escreve quando o valor desejado difere do arquivo.
Conta 1 e conta com credit_pct 0 nunca consomem nem são travadas. Tudo fail-open: exceção vira warning.
"""
from __future__ import annotations

import glob
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..core import control_file
from ..core.config import config
from ..core.martingale import STRATEGY_TYPE as MARTINGALE

logger = logging.getLogger(__name__)

GRACE_S = 3600          # carência pro reconciler acertar pnl_net/fees antes de cobrar
SETTLE_EVERY_S = 60.0   # /api/live liquida no máximo 1× por minuto por conta
DAEMON_EVERY_S = 300.0  # thread do dashboard: todas as contas com worker, mesmo sem ninguém no painel
LOW_FLOOR_USD = 5.0     # aviso amarelo: saldo < max(5, 2 × último consumo)

_clock = time.monotonic
_last_settle: Dict[int, float] = {}


def _positive_cycles(runtime_dir: str, now: datetime) -> List[Tuple[str, float]]:
    """[(ref, pnl_net)] dos ciclos martingale fechados há > GRACE_S com lucro líquido > 0, lidos só-leitura
    do trades.*.db mais novo da conta (sem abrir TradeStore: nada de migração/log no DB do worker)."""
    paths = glob.glob(os.path.join(runtime_dir, "trades.*.db"))
    if not paths:
        return []
    path = max(paths, key=os.path.getmtime)
    cutoff = (now - timedelta(seconds=GRACE_S)).astimezone(timezone.utc).isoformat()
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT id, opened_at, pnl_net FROM trades WHERE status = 'closed' AND strategy_type = ?"
                           " AND pnl_net > 0 AND exit_at IS NOT NULL AND exit_at <= ? ORDER BY id", (MARTINGALE, cutoff)).fetchall()
    finally:
        con.close()
    return [(f"{r[0]}@{r[1]}", float(r[2])) for r in rows]


def summary(platform, account_id: int) -> Dict[str, Any]:
    """{balance, credit_pct, exhausted, low, ledger (últimos 50)} da conta."""
    acc = platform.account(account_id) or {}
    pct = float(acc.get("credit_pct") or 0.0)
    balance = round(platform.credit_balance(account_id), 4)
    ledger = platform.credit_ledger(account_id)
    last = next((e["amount"] for e in ledger if e["kind"] == "consume"), 0.0)
    exhausted = pct > 0 and balance <= 0
    return {"balance": balance, "credit_pct": pct, "exhausted": exhausted,
            "low": pct > 0 and not exhausted and balance < max(LOW_FLOOR_USD, 2 * last), "ledger": ledger}


def settle_credits(platform, account_id: int, runtime_dir: Optional[str], now: Optional[datetime] = None) -> Dict[str, Any]:
    """Consome os ciclos positivos novos e aplica a trava new_cycles; devolve summary(). Conta 1, pct 0 ou
    sem runtime_dir: só devolve o resumo (sem consumo, sem trava)."""
    now = now or datetime.now(timezone.utc)
    acc = platform.account(account_id)
    pct = float((acc or {}).get("credit_pct") or 0.0)
    if acc is None or account_id == 1 or pct <= 0 or not runtime_dir:
        return summary(platform, account_id)
    for ref, net in _positive_cycles(runtime_dir, now):
        if platform.add_credit(account_id, "consume", round(net * pct / 100.0, 4), ref, f"ciclo {ref}: líquido {net:.4f}"):
            logger.info(f"🪙 Conta {account_id}: ciclo {ref} líquido {net:.4f} → consumo {net * pct / 100.0:.4f} ({pct:g}%)")
    out = summary(platform, account_id)
    want = out["balance"] > 0
    if control_file.read_control(runtime_dir)["new_cycles"] is not want:
        control_file.write_control(runtime_dir, new_cycles=want)
        logger.warning(f"🪫 Conta {account_id}: saldo {out['balance']:.2f} USDT → new_cycles={want}")
    return out


def settle_throttled(platform, account_id: int, runtime_dir: Optional[str]) -> Optional[Dict[str, Any]]:
    """settle_credits no máximo 1× por SETTLE_EVERY_S por conta (chamado pelo poll do painel); nunca levanta."""
    try:
        if _clock() - _last_settle.get(account_id, float("-inf")) >= SETTLE_EVERY_S:
            _last_settle[account_id] = _clock()
            return settle_credits(platform, account_id, runtime_dir)
        return summary(platform, account_id)
    except Exception as exc:
        logger.warning(f"⚠️ Créditos da conta {account_id} indisponíveis: {exc}")
        return None


def settle_all(platform) -> None:
    """Todas as contas com worker (runtime/accounts/N com worker.enabled ou bot_state), menos a 1."""
    from .accounts import worker_runtime_dir
    base = os.path.join(str(getattr(config, "RUNTIME_DIR", "runtime")), "accounts")
    for name in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        if not name.isdigit() or int(name) == 1:
            continue
        d = worker_runtime_dir(int(name))
        if d is not None:
            try:
                settle_credits(platform, int(name), d)
            except Exception as exc:
                logger.warning(f"⚠️ Créditos da conta {name}: liquidação falhou: {exc}")


def start_daemon(platform, every_s: float = DAEMON_EVERY_S) -> threading.Thread:
    """Thread daemon do processo do dashboard: a trava vale mesmo com o painel fechado."""
    def run():
        while True:
            time.sleep(every_s)
            settle_all(platform)
    t = threading.Thread(target=run, name="credits-settle", daemon=True)
    t.start()
    return t

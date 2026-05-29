"""
RECONCILIAÇÃO DE TRADES FANTASMA
================================
Conserta entradas em `trade_history` que ficaram "Abertas" pra sempre
porque foram fechadas server-side pela Binance (ordens SL/TP com
USE_INDIVIDUAL_STOP_LOSS=True). O path `_process_binance_closed_position`
incrementava os contadores no momento do fechamento, mas NÃO enriquecia
o trade_history — então o dashboard mostra a posição como aberta.

O QUE FAZ:
  • Faz backup do state file antes de qualquer escrita.
  • Acha as entradas de trade_history sem exit_time (os fantasmas).
  • Pra cada uma, busca o REALIZED_PNL real na Binance a partir do
    timestamp de entrada, deriva exit_price/fees e preenche os campos
    de fechamento (exit_price, exit_time, pnl_gross, pnl_net, fees,
    close_reason).
  • Se a Binance não tiver mais o income (retenção): marca como fechado
    com fallback neutro e um marcador de auditoria (reconciled=unmatched).

O QUE *NÃO* FAZ:
  • NÃO mexe em contadores (closed_trades_count, total_pnl, etc.). Esses
    já foram somados no momento do fechamento — re-tocar = double count.

SEGURANÇA:
  • Recusa rodar se o bot estiver de pé (lock file ativo), pra evitar
    que o autosave do bot sobrescreva nossas mudanças. Rode em janela
    de manutenção, com o bot parado.
  • Use --dry-run pra inspecionar sem escrever.

USO:
  python scripts/reconcile_phantom_trades.py --dry-run
  python scripts/reconcile_phantom_trades.py            # aplica
  python scripts/reconcile_phantom_trades.py --force    # ignora lock (perigoso)
"""

import argparse
import fcntl
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from trading_bot.core.config import config  # noqa: E402
from trading_bot.infra.binance_client import BinanceConnection  # noqa: E402

# Janela de tolerância ao casar a entrada com o income realizado.
# A entrada não tem o exit_time, então pegamos o primeiro REALIZED_PNL
# desse símbolo a partir do timestamp de entrada.
INCOME_LOOKBACK_LIMIT = 1000


def log(*a):
    print(*a)
    sys.stdout.flush()


def _entry_ms(entry: dict) -> int | None:
    ts = entry.get("timestamp")
    if not ts:
        return None
    try:
        return int(datetime.fromisoformat(ts).timestamp() * 1000)
    except (ValueError, TypeError):
        return None


def _is_phantom(entry: dict) -> bool:
    return (
        isinstance(entry, dict)
        and entry.get("entry_price") is not None
        and entry.get("exit_time") is None
        and entry.get("exit_price") is None
    )


def _bot_is_running() -> bool:
    """Detecta o bot vivo via probe do flock — não pela existência do arquivo.

    O bot segura o lock com fcntl.flock (advisory). O SO libera o flock
    quando o processo morre, mas o arquivo permanece no disco (o bot não
    faz unlink em mortes não-graciosas). Logo, checar `.exists()` daria
    falso positivo com lock stale. Aqui tentamos adquirir o flock: se
    conseguirmos, ninguém o segura (arquivo stale) ⇒ bot NÃO está rodando.
    """
    lock_path = Path(config.LOCK_FILE_PATH)
    if not lock_path.exists():
        return False
    try:
        fh = open(lock_path, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            return False
        finally:
            fh.close()
    except BlockingIOError:
        return True


def reconcile(dry_run: bool, force: bool) -> int:
    state_path = Path(config.STATE_FILE_PATH)
    log("=" * 64)
    log("RECONCILIAÇÃO DE TRADES FANTASMA")
    log("=" * 64)
    log(f"State file : {state_path}")
    log(f"Lock file  : {config.LOCK_FILE_PATH}")
    log(f"Modo       : {'DRY-RUN (sem escrita)' if dry_run else 'APLICAR'}")
    log("")

    if not state_path.exists():
        log(f"❌ State file não encontrado: {state_path}")
        return 1

    if _bot_is_running():
        msg = "⚠️  Lock file presente — o bot parece estar rodando."
        if force:
            log(msg + " Continuando mesmo assim (--force).")
        else:
            log(msg)
            log("   Pare o bot antes de rodar (o autosave sobrescreveria as mudanças).")
            log("   Ou use --force se tem certeza que o bot está parado.")
            return 2

    state = json.loads(state_path.read_text())
    history = state.get("trade_history", [])
    phantoms = [(i, e) for i, e in enumerate(history) if _is_phantom(e)]

    log(f"trade_history total       : {len(history)}")
    log(f"fantasmas (sem exit_time) : {len(phantoms)}")
    log("")

    if not phantoms:
        log("✅ Nada a reconciliar.")
        return 0

    exchange = BinanceConnection()

    # Taker rate por símbolo (cache), igual ao que o bot usa pra estimar fees.
    _rate_cache: dict[str, float] = {}

    def taker_rate_for(sym: str) -> float:
        if sym not in _rate_cache:
            try:
                rates = exchange.get_commission_rates(symbol=sym)
                _rate_cache[sym] = float(rates.get("taker_rate", 0.0005))
            except Exception:  # noqa: BLE001
                _rate_cache[sym] = 0.0005
        return _rate_cache[sym]

    matched = 0
    unmatched = 0

    for idx, entry in phantoms:
        symbol = entry.get("symbol")
        side = entry.get("side")
        entry_price = float(entry.get("entry_price") or 0)
        qty = entry.get("qty")
        qty = float(qty) if qty else 0.0
        start_ms = _entry_ms(entry)

        income = []
        if start_ms is not None:
            try:
                income = exchange.get_income_history(
                    income_type="REALIZED_PNL",
                    symbol=symbol,
                    start_time=start_ms,
                    limit=INCOME_LOOKBACK_LIMIT,
                )
            except Exception as e:  # noqa: BLE001
                log(f"  [{symbol}] erro ao buscar income: {e!r}")
            # Throttle leve pra não estourar rate limit.
            time.sleep(0.15)

        # Pega o primeiro REALIZED_PNL desse símbolo após a entrada.
        realized = None
        close_ms = None
        for item in income:
            if item.get("symbol") == symbol and item.get("incomeType") == "REALIZED_PNL":
                realized = float(item.get("income") or 0)
                close_ms = item.get("time")
                break

        if realized is None:
            # Sem income disponível (retenção/expirado). Fallback neutro.
            close_fields = {
                "exit_price": entry_price,
                "exit_time": datetime.now().isoformat(),
                "pnl_gross": 0.0,
                "pnl_net": 0.0,
                "fees": 0.0,
                "close_reason": "Fechado (não reconciliado)",
                "reconciled": "unmatched",
            }
            unmatched += 1
            log(f"  [{symbol} {side}] sem income — fallback neutro")
        else:
            pnl_gross = realized
            notional = entry_price * qty if qty else 0.0
            fees = notional * taker_rate_for(symbol) * 2 if notional else 0.0
            pnl_net = pnl_gross - fees
            if qty:
                delta = pnl_gross / qty
                exit_price = entry_price + delta if side == "LONG" else entry_price - delta
            else:
                exit_price = entry_price
            reason = "Take Profit (Binance)" if pnl_gross > 0 else "Stop Loss (Binance)"
            exit_time = (
                datetime.fromtimestamp(close_ms / 1000).isoformat()
                if close_ms
                else datetime.now().isoformat()
            )
            close_fields = {
                "exit_price": exit_price,
                "exit_time": exit_time,
                "pnl_gross": pnl_gross,
                "pnl_net": pnl_net,
                "fees": fees,
                "close_reason": reason,
                "reconciled": "binance_income",
            }
            matched += 1
            log(
                f"  [{symbol} {side}] gross={pnl_gross:+.4f} fees={fees:.4f} "
                f"net={pnl_net:+.4f} → {reason}"
            )

        if not dry_run:
            history[idx].update(close_fields)

    log("")
    log(f"reconciliados via Binance : {matched}")
    log(f"fallback (sem income)     : {unmatched}")
    log("")

    if dry_run:
        log("DRY-RUN: nenhuma escrita feita.")
        return 0

    # Backup antes de escrever.
    backup_path = state_path.with_suffix(
        state_path.suffix + f".bak.{datetime.now():%Y%m%d_%H%M%S}"
    )
    shutil.copy2(state_path, backup_path)
    log(f"Backup    : {backup_path}")

    state["trade_history"] = history
    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp_path.replace(state_path)
    log(f"✅ State atualizado: {state_path}")
    log("   (contadores NÃO foram tocados — só o trade_history)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcilia trades fantasma no trade_history.")
    parser.add_argument("--dry-run", action="store_true", help="Inspeciona sem escrever.")
    parser.add_argument("--force", action="store_true", help="Ignora o lock file do bot.")
    args = parser.parse_args()
    return reconcile(dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())

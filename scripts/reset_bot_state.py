"""
RESET DO ESTADO DO BOT (ZERAR HISTÓRICO E ESTATÍSTICAS)
=======================================================
Zera o histórico de trades e os contadores internos do bot pra começar
"do zero" no dashboard, PRESERVANDO toda a configuração (disabled_pairs,
strategy_profiles, kill_switch, capital, etc.).

O QUE ZERA:
  • trade_history            -> []
  • known_positions          -> {}   (assume conta FLAT — feche posições antes!)
  • contadores de PnL/fees/trades, pnl_by_symbol, portfolio_history,
    drawdown e dicts de gestão de posição (peak_prices/trailing/double).

O QUE *NÃO* TOCA:
  • disabled_pairs, strategy_profiles, binance_coin_list, initial_capital,
    kill_switch, invert_signals, sentiment_mode_enabled, version, e os
    campos de transfer/baseline.
  • O SALDO da carteira e o PnL-do-dia dos cards vêm AO VIVO da Binance —
    este script não mexe neles (só zeram resetando a conta testnet / à
    meia-noite UTC).

SEGURANÇA:
  • Recusa rodar se o bot estiver de pé (lock file). Pare o bot antes.
    --force ignora o lock (use só com o processo comprovadamente morto).
  • IMPORTANTE: rode com a conta FLAT (sem posições abertas na Binance).
    Se houver posição aberta e você zerar known_positions, o bot perde o
    rastro dela. Feche tudo via /closeall (ou /stop force) antes.
  • --dry-run mostra o que mudaria sem escrever.

USO:
  python scripts/reset_bot_state.py --dry-run
  python scripts/reset_bot_state.py
  python scripts/reset_bot_state.py --force
"""

import argparse
import fcntl
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from trading_bot.core.config import config  # noqa: E402

# Contadores escalares -> zero.
STAT_SCALARS = [
    "closed_trades_count",
    "total_pnl",
    "daily_realized_pnl",
    "daily_pnl_binance_baseline",
    "total_fees_paid",
    "trades_win_count",
    "trades_loss_count",
    "trades_win_total",
    "trades_loss_total",
    "max_drawdown_from_peak_percent",
]

# Coleções -> vazias. Tipo explícito por chave (NÃO inferir de state.get(k)):
# trade_history/portfolio_history deixaram de ser persistidos no JSON desde o
# Phase 1 (vivem no SQLite TradeStore — ver trading_bot/core/bot.py:778). Quando
# ausentes do state, state.get(k) é None e inferir o tipo daria {} (dict); o load
# do bot então quebraria ao fatiar `trade_history[-500:]` (unhashable type: 'slice').
# Por isso fixamos o tipo-vazio aqui, independente de a chave existir ou não.
EMPTY_COLLECTIONS = {
    "trade_history": list,
    "known_positions": dict,
    "pnl_by_symbol": dict,
    "portfolio_history": list,
    "peak_prices": dict,
    "trailing_activated": dict,
    "double_first_used": dict,
}


def log(*a):
    print(*a)
    sys.stdout.flush()


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


def reset(dry_run: bool, force: bool) -> int:
    state_path = Path(config.STATE_FILE_PATH)
    log("=" * 64)
    log("RESET DO ESTADO DO BOT")
    log("=" * 64)
    log(f"State file : {state_path}")
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
            log("   Pare o bot antes (o autosave sobrescreveria as mudanças).")
            return 2

    state = json.loads(state_path.read_text())

    # Snapshot do que será zerado (pra log/auditoria).
    open_positions = state.get("known_positions", {}) or {}
    if open_positions:
        log(f"⚠️  known_positions tem {len(open_positions)} posição(ões): "
            f"{list(open_positions.keys())}")
        log("    Certifique-se que a conta está FLAT na Binance antes de aplicar.")
        log("")

    log("ANTES:")
    log(f"  trade_history       = {len(state.get('trade_history', []))}")
    for k in ("closed_trades_count", "total_pnl", "total_fees_paid",
              "trades_win_count", "trades_loss_count"):
        log(f"  {k:<19} = {state.get(k)}")
    log("")

    # Aplica reset (em memória).
    for k in STAT_SCALARS:
        state[k] = 0 if isinstance(state.get(k), int) else 0.0
    for k, empty_factory in EMPTY_COLLECTIONS.items():
        state[k] = empty_factory()

    # Datas de baseline diário -> None pra forçar re-baseline limpo.
    for k in ("daily_date", "daily_baseline_date", "last_daily_performance_report_date"):
        if k in state:
            state[k] = None

    # Recomeça a contagem de uptime.
    state["start_time"] = datetime.now().isoformat()

    log("DEPOIS:")
    log(f"  trade_history       = {len(state.get('trade_history', []))}")
    for k in ("closed_trades_count", "total_pnl", "total_fees_paid",
              "trades_win_count", "trades_loss_count"):
        log(f"  {k:<19} = {state.get(k)}")
    log("")
    log("PRESERVADO: disabled_pairs, strategy_profiles, kill_switch, "
        "initial_capital, invert_signals, sentiment_mode_enabled, version.")
    log("")

    if dry_run:
        log("DRY-RUN: nenhuma escrita feita.")
        return 0

    backup_path = state_path.with_suffix(
        state_path.suffix + f".bak.{datetime.now():%Y%m%d_%H%M%S}"
    )
    shutil.copy2(state_path, backup_path)
    log(f"Backup    : {backup_path}")

    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp_path.replace(state_path)
    log(f"✅ State zerado: {state_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Zera histórico e stats do bot.")
    parser.add_argument("--dry-run", action="store_true", help="Inspeciona sem escrever.")
    parser.add_argument("--force", action="store_true", help="Ignora o lock file do bot.")
    args = parser.parse_args()
    return reset(dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())

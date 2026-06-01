#!/usr/bin/env bash
# Auto-restart wrapper. Reinicia o bot se cair (OOM, crash, etc).
# Pra parar de verdade: pkill -f "run_bot_loop.sh" ANTES do pkill do python,
# senão o wrapper relança em ~5s.
#
# Backoff exponencial pra evitar loop quente quando o erro é persistente
# (ex.: import error, config bad). Após 60s consecutivos o backoff estabiliza.
set -u

PROJECT_DIR="${PROJECT_DIR:-/home/ubuntu/trading_bot}"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_DIR/.venv/bin/python}"
BOT_MODULE="${BOT_MODULE:-trading_bot.core.bot}"
RESTART_LOG="$PROJECT_DIR/runtime/restart.log"
BACKOFF_BASE=5
BACKOFF_MAX=60
backoff=$BACKOFF_BASE

mkdir -p "$(dirname "$RESTART_LOG")"
cd "$PROJECT_DIR"

# Limpeza defensiva única antes de subir: mata one-liners de diagnóstico
# (python -c "...BinanceConnection..." / pos_diag) que ficam presos consumindo
# CPU/RAM na VM Micro. NÃO mata instâncias do bot (-m trading_bot.core.bot):
# o módulo é o mesmo p/ testnet e mainnet, e o flock já garante instância única
# por ambiente. Quem encerra instâncias antigas é o update_server.sh (deploy).
DIAG_PROCESS_PATTERN="${DIAG_PROCESS_PATTERN:-python[0-9]*.*(BinanceConnection|pos_diag)}"
if pgrep -f "$DIAG_PROCESS_PATTERN" >/dev/null 2>&1; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Limpando órfãos de diagnóstico antes de subir" >> "$RESTART_LOG"
    pkill -KILL -f "$DIAG_PROCESS_PATTERN" 2>/dev/null || true
fi

while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Iniciando bot (backoff=${backoff}s)" >> "$RESTART_LOG"
    start_ts=$(date +%s)
    "$PYTHON_BIN" -m "$BOT_MODULE"
    exit_code=$?
    run_secs=$(( $(date +%s) - start_ts ))
    # Se o bot rodou > 60s era uma instância estável (não crash-loop): reseta o
    # backoff pra base, pra próxima recuperação ser rápida. Senão dobra até o
    # teto, evitando loop quente quando o erro é persistente (import/config bad).
    if [ "$run_secs" -gt 60 ]; then
        backoff=$BACKOFF_BASE
    else
        backoff=$((backoff * 2))
        if [ "$backoff" -gt "$BACKOFF_MAX" ]; then backoff=$BACKOFF_MAX; fi
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Bot saiu code=$exit_code após ${run_secs}s. Reinício em ${backoff}s." >> "$RESTART_LOG"
    sleep "$backoff"
done

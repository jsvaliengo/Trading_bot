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

while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Iniciando bot (backoff=${backoff}s)" >> "$RESTART_LOG"
    "$PYTHON_BIN" -m "$BOT_MODULE"
    exit_code=$?
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Bot saiu code=$exit_code. Reinicio em ${backoff}s." >> "$RESTART_LOG"
    sleep "$backoff"
    # Backoff exponencial — reseta pra base se o bot rodou > 60s
    backoff=$((backoff * 2))
    if [ "$backoff" -gt "$BACKOFF_MAX" ]; then backoff=$BACKOFF_MAX; fi
done

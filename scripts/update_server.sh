#!/usr/bin/env bash
set -euo pipefail

# Atualização segura do bot no servidor:
# 1) Atualiza código (git pull, se houver repositório)
# 2) Atualiza dependências
# 3) Roda pytest
# 4) Reinicia o bot em screen somente se os testes passarem

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
VENV_DIR="${VENV_DIR:-$PROJECT_DIR/.venv}"
SCREEN_NAME="${SCREEN_NAME:-bot}"
BOT_MODULE="${BOT_MODULE:-trading_bot.core.bot}"
BOT_PROCESS_PATTERN="${BOT_PROCESS_PATTERN:-python.*-m[[:space:]]+${BOT_MODULE//./\\.}}"
PYTHON_BIN="${PYTHON_BIN:-$VENV_DIR/bin/python}"
SKIP_GIT_PULL="${SKIP_GIT_PULL:-0}"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

cd "$PROJECT_DIR"

log "Projeto: $PROJECT_DIR"

if [[ "$SKIP_GIT_PULL" == "1" || "$SKIP_GIT_PULL" == "true" ]]; then
  log "SKIP_GIT_PULL ativo. Pulando git pull."
elif [[ -d .git ]]; then
  log "Atualizando código (git pull --ff-only)..."
  git pull --ff-only
else
  log "Diretório sem .git. Pulando etapa de git pull."
fi

if [[ ! -d "$VENV_DIR" ]]; then
  log "Criando virtualenv em $VENV_DIR..."
  python3 -m venv "$VENV_DIR"
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
  log "Python do virtualenv não encontrado em $PYTHON_BIN"
  exit 1
fi

log "Atualizando pip e dependências..."
"$PYTHON_BIN" -m pip install --upgrade pip
"$PYTHON_BIN" -m pip install -r requirements.txt

log "Executando testes (pytest)..."
"$PYTHON_BIN" -m pytest -q

log "Testes passaram. Reiniciando bot..."

# Tenta parada graciosa via screen (Ctrl+C), sem fechar posições no bot.
if screen -list | grep -q "[[:space:]]${SCREEN_NAME}[[:space:]]"; then
  log "Enviando Ctrl+C para sessão screen '$SCREEN_NAME'..."
  screen -S "$SCREEN_NAME" -X stuff $'\003'
  sleep 3
fi

# Se ainda existir processo antigo do bot, encerra com SIGTERM.
if pgrep -af "$BOT_PROCESS_PATTERN" >/dev/null; then
  log "Processo antigo detectado. Enviando SIGTERM..."
  pkill -f "$BOT_PROCESS_PATTERN" || true
  sleep 2
fi

# Fecha sessão screen anterior (se sobrou) para evitar duplicidade.
screen -S "$SCREEN_NAME" -X quit >/dev/null 2>&1 || true

log "Subindo nova sessão screen '$SCREEN_NAME'..."
screen -dmS "$SCREEN_NAME" bash -lc "cd '$PROJECT_DIR' && '$PYTHON_BIN' -m '$BOT_MODULE'"

sleep 1
if pgrep -af "$BOT_PROCESS_PATTERN" >/dev/null; then
  log "Bot iniciado com sucesso."
else
  log "Falha ao iniciar bot."
  exit 1
fi

log "Concluído."

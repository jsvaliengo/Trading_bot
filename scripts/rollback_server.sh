#!/usr/bin/env bash
set -euo pipefail

# Rollback seguro no servidor:
# 1) Escolhe backup (informado ou último disponível)
# 2) Restaura código no PROJECT_DIR sem sobrescrever .env/runtime/venv
# 3) Reaplica update_server.sh (sem git pull) para validar e reiniciar

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BACKUP_ROOT="${BACKUP_ROOT:-$HOME/deploy_backups/trading_bot}"
BACKUP_DIR="${BACKUP_DIR:-}"
SKIP_RESTART="${SKIP_RESTART:-0}"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

if [[ ! -d "$PROJECT_DIR" ]]; then
  log "Diretório do projeto não encontrado: $PROJECT_DIR"
  exit 1
fi

if [[ -z "$BACKUP_DIR" ]]; then
  if [[ ! -d "$BACKUP_ROOT" ]]; then
    log "BACKUP_ROOT não encontrado: $BACKUP_ROOT"
    exit 1
  fi
  BACKUP_DIR="$(ls -1dt "$BACKUP_ROOT"/* 2>/dev/null | head -n1 || true)"
fi

if [[ -z "$BACKUP_DIR" || ! -d "$BACKUP_DIR" ]]; then
  log "Backup inválido ou inexistente: ${BACKUP_DIR:-<vazio>}"
  exit 1
fi

log "Projeto: $PROJECT_DIR"
log "Backup selecionado: $BACKUP_DIR"

rsync -a --delete \
  --exclude ".git/" \
  --exclude ".venv/" \
  --exclude "venv/" \
  --exclude "__pycache__/" \
  --exclude ".pytest_cache/" \
  --exclude ".env" \
  --exclude "runtime/" \
  --exclude "deploy_backups/" \
  "$BACKUP_DIR/" "$PROJECT_DIR/"

if [[ "$SKIP_RESTART" == "1" || "$SKIP_RESTART" == "true" ]]; then
  log "SKIP_RESTART ativo. Código restaurado sem restart."
  exit 0
fi

UPDATE_SCRIPT=""
if [[ -f "$PROJECT_DIR/scripts/update_server.sh" ]]; then
  UPDATE_SCRIPT="$PROJECT_DIR/scripts/update_server.sh"
elif [[ -f "$PROJECT_DIR/update_server.sh" ]]; then
  UPDATE_SCRIPT="$PROJECT_DIR/update_server.sh"
fi

if [[ -z "$UPDATE_SCRIPT" ]]; then
  log "update_server.sh não encontrado após rollback."
  exit 1
fi

chmod +x "$UPDATE_SCRIPT"
log "Executando update_server.sh após rollback..."
SKIP_GIT_PULL=1 "$UPDATE_SCRIPT"

log "Rollback concluído com sucesso."

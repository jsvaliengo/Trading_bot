#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
RUNTIME_DIR="${RUNTIME_DIR:-$PROJECT_DIR/runtime}"
STATE_FILE="${STATE_FILE:-$RUNTIME_DIR/.notes_sync_hash}"
LOG_FILE="${LOG_FILE:-$RUNTIME_DIR/notes_autosync.log}"
PYTHON_BIN="${PYTHON_BIN:-}"
FORCE="${1:-}"

mkdir -p "$RUNTIME_DIR"

if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x "$PROJECT_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$PROJECT_DIR/.venv/bin/python"
  elif [[ -x "$PROJECT_DIR/venv/bin/python" ]]; then
    PYTHON_BIN="$PROJECT_DIR/venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi

hash_of_file() {
  local file="$1"
  if [[ -f "$file" ]]; then
    shasum "$file" | awk '{print $1}'
  else
    echo "missing"
  fi
}

config_hash_input=$(
  cat <<EOF
.env:$(hash_of_file "$PROJECT_DIR/.env")
.env.local:$(hash_of_file "$PROJECT_DIR/.env.local")
core_config:$(hash_of_file "$PROJECT_DIR/trading_bot/core/config.py")
notes_sync_script:$(hash_of_file "$PROJECT_DIR/scripts/sync_apple_notes.py")
EOF
)
current_hash="$(printf "%s" "$config_hash_input" | shasum | awk '{print $1}')"
previous_hash="$(cat "$STATE_FILE" 2>/dev/null || true)"

timestamp() {
  date '+%Y-%m-%d %H:%M:%S'
}

if [[ "$FORCE" != "--force" && "$current_hash" == "$previous_hash" ]]; then
  printf '[%s] no changes, skip sync\n' "$(timestamp)" >>"$LOG_FILE"
  exit 0
fi

{
  printf '[%s] syncing Apple Notes...\n' "$(timestamp)"
  cd "$PROJECT_DIR"
  "$PYTHON_BIN" "$PROJECT_DIR/scripts/sync_apple_notes.py"
  printf '%s\n' "$current_hash" >"$STATE_FILE"
  printf '[%s] sync done\n' "$(timestamp)"
} >>"$LOG_FILE" 2>&1


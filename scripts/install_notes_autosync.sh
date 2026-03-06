#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
RUNTIME_DIR="${RUNTIME_DIR:-$PROJECT_DIR/runtime}"
AGENT_NAME="${AGENT_NAME:-com.tradingbot.notes-sync}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-300}"
PLIST_PATH="$HOME/Library/LaunchAgents/${AGENT_NAME}.plist"
UID_VALUE="$(id -u)"

mkdir -p "$RUNTIME_DIR" "$HOME/Library/LaunchAgents"

cat >"$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${AGENT_NAME}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>cd '${PROJECT_DIR}' && '${PROJECT_DIR}/scripts/notes_autosync.sh'</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>${INTERVAL_SECONDS}</integer>
  <key>StandardOutPath</key>
  <string>${RUNTIME_DIR}/notes_autosync.launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>${RUNTIME_DIR}/notes_autosync.launchd.err.log</string>
</dict>
</plist>
EOF

chmod 644 "$PLIST_PATH"

# Reload agent
launchctl bootout "gui/${UID_VALUE}" "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl bootstrap "gui/${UID_VALUE}" "$PLIST_PATH"
launchctl enable "gui/${UID_VALUE}/${AGENT_NAME}" >/dev/null 2>&1 || true
launchctl kickstart -k "gui/${UID_VALUE}/${AGENT_NAME}" >/dev/null 2>&1 || true

echo "Installed: $PLIST_PATH"
echo "Interval: ${INTERVAL_SECONDS}s"
echo "Project: $PROJECT_DIR"
echo "Logs: ${RUNTIME_DIR}/notes_autosync.log"


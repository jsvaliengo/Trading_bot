#!/usr/bin/env bash
#
# Abre/fecha o SSH tunnel que expõe o /metrics do bot OCI no Mac, pra
# Prometheus/Grafana local rasparem sem precisar abrir porta na internet.
#
# Uso:
#   scripts/oci_metrics_tunnel.sh start   # sobe o tunnel
#   scripts/oci_metrics_tunnel.sh stop    # derruba
#   scripts/oci_metrics_tunnel.sh status  # mostra se está ativo
#   scripts/oci_metrics_tunnel.sh restart # stop + start
#
# Config via env vars (com defaults sensatos):
#   OCI_SSH_HOST       SSH host alias (default: oci-bot — conforme ~/.ssh/config)
#   LOCAL_PORT         Porta local a expor (default: 9090)
#   REMOTE_PORT        Porta do /metrics na VM (default: 9090)
#   USE_AUTOSSH        1 pra usar autossh (reconecta sozinho) se disponível
#                      (default: auto — usa se `autossh` estiver no PATH)
#
# Auto-detecta autossh; se não estiver instalado, cai pra ssh padrão (requer
# reabrir o tunnel manualmente se a conexão cair).

set -euo pipefail

OCI_SSH_HOST="${OCI_SSH_HOST:-oci-bot}"
LOCAL_PORT="${LOCAL_PORT:-9090}"
REMOTE_PORT="${REMOTE_PORT:-9090}"
USE_AUTOSSH="${USE_AUTOSSH:-auto}"

PIDFILE="${TMPDIR:-/tmp}/oci_metrics_tunnel.${LOCAL_PORT}.pid"
FORWARD_SPEC="${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}"

log() {
  printf '[oci-tunnel] %s\n' "$*" >&2
}

# Detecta autossh conforme política (auto | 1 | 0).
resolve_ssh_binary() {
  case "${USE_AUTOSSH}" in
    1|true|yes)
      command -v autossh >/dev/null 2>&1 || {
        log "❌ USE_AUTOSSH=1 mas autossh não está no PATH. Instale com 'brew install autossh'."
        exit 1
      }
      echo "autossh"
      ;;
    0|false|no)
      echo "ssh"
      ;;
    auto|*)
      if command -v autossh >/dev/null 2>&1; then
        echo "autossh"
      else
        echo "ssh"
      fi
      ;;
  esac
}

pid_alive() {
  [[ -f "${PIDFILE}" ]] || return 1
  local pid
  pid="$(cat "${PIDFILE}" 2>/dev/null || true)"
  [[ -n "${pid}" ]] || return 1
  kill -0 "${pid}" 2>/dev/null
}

cmd_start() {
  if pid_alive; then
    log "ℹ️  Tunnel já está ativo (PID $(cat "${PIDFILE}")). Use 'restart' pra reabrir."
    return 0
  fi

  # Se a porta local já está ocupada por outro processo, avisa.
  if lsof -nP -iTCP:"${LOCAL_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
    log "❌ Porta ${LOCAL_PORT} já está em uso no Mac. Rode 'lsof -i :${LOCAL_PORT}' pra investigar."
    exit 1
  fi

  local ssh_bin
  ssh_bin="$(resolve_ssh_binary)"
  log "🔌 Abrindo tunnel ${FORWARD_SPEC} via ${ssh_bin} (host=${OCI_SSH_HOST})..."

  # -f background, -N sem comando, -N forward-only.
  # ServerAliveInterval/CountMax derruba conexão zumbi pra autossh reabrir.
  if [[ "${ssh_bin}" == "autossh" ]]; then
    # autossh precisa de AUTOSSH_PIDFILE pra gravar o PID do próprio wrapper.
    AUTOSSH_PIDFILE="${PIDFILE}" AUTOSSH_GATETIME=0 \
      autossh -M 0 -f -N \
        -o "ServerAliveInterval=30" \
        -o "ServerAliveCountMax=3" \
        -o "ExitOnForwardFailure=yes" \
        -L "${FORWARD_SPEC}" \
        "${OCI_SSH_HOST}"
  else
    ssh -f -N \
      -o "ServerAliveInterval=30" \
      -o "ServerAliveCountMax=3" \
      -o "ExitOnForwardFailure=yes" \
      -L "${FORWARD_SPEC}" \
      "${OCI_SSH_HOST}"
    # ssh -f não imprime o PID; descobre via pgrep pelo forward spec exato.
    local pid
    pid="$(pgrep -f "ssh .*-L ${FORWARD_SPEC} ${OCI_SSH_HOST}" | head -n1 || true)"
    if [[ -z "${pid}" ]]; then
      log "❌ Tunnel subiu mas PID não foi encontrado. Verifique manualmente com 'lsof -i :${LOCAL_PORT}'."
      exit 1
    fi
    echo "${pid}" > "${PIDFILE}"
  fi

  # Pequena sanidade: pergunta o /metrics.
  sleep 1
  if curl -sf "http://127.0.0.1:${LOCAL_PORT}/metrics" >/dev/null; then
    log "✅ Tunnel ativo. Métricas do OCI acessíveis em http://127.0.0.1:${LOCAL_PORT}/metrics"
  else
    log "⚠️  Tunnel subiu mas /metrics não respondeu ainda. Confira com:"
    log "     curl http://127.0.0.1:${LOCAL_PORT}/metrics | head"
  fi
}

cmd_stop() {
  if ! pid_alive; then
    log "ℹ️  Tunnel não está ativo (sem PID registrado)."
    # Limpa PID stale, se existir.
    rm -f "${PIDFILE}"
    return 0
  fi
  local pid
  pid="$(cat "${PIDFILE}")"
  log "🛑 Derrubando tunnel (PID ${pid})..."
  kill "${pid}" 2>/dev/null || true
  # Espera até 3s pelo processo sair.
  for _ in 1 2 3; do
    sleep 1
    if ! kill -0 "${pid}" 2>/dev/null; then
      break
    fi
  done
  rm -f "${PIDFILE}"
  log "✅ Tunnel derrubado."
}

cmd_status() {
  if pid_alive; then
    local pid
    pid="$(cat "${PIDFILE}")"
    log "🟢 Ativo (PID ${pid}, porta local ${LOCAL_PORT})."
    if curl -sf "http://127.0.0.1:${LOCAL_PORT}/metrics" >/dev/null; then
      log "   /metrics responde OK."
    else
      log "   ⚠️  /metrics não respondeu — conexão OCI pode estar zumbi."
    fi
  else
    log "🔴 Inativo."
    exit 1
  fi
}

cmd_restart() {
  cmd_stop || true
  cmd_start
}

case "${1:-}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  status)  cmd_status ;;
  restart) cmd_restart ;;
  *)
    cat >&2 <<USAGE
Uso: $0 {start|stop|status|restart}

Env vars (opcionais):
  OCI_SSH_HOST   SSH host alias (default: oci-bot)
  LOCAL_PORT     Porta local (default: 9090)
  REMOTE_PORT    Porta do /metrics na VM (default: 9090)
  USE_AUTOSSH    auto|1|0 (default: auto)
USAGE
    exit 1
    ;;
esac

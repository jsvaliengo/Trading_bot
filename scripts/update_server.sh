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
BOT_WRAPPER="${BOT_WRAPPER:-$PROJECT_DIR/scripts/run_bot_loop.sh}"
WRAPPER_PROCESS_PATTERN="${WRAPPER_PROCESS_PATTERN:-run_bot_loop\\.sh}"
# Órfãos de diagnóstico: one-liners manuais (python -c "...BinanceConnection...")
# ou scripts pos_diag que às vezes ficam presos consumindo CPU/RAM na VM Micro.
# Padrão NÃO casa com o bot (-m trading_bot.core.bot) nem com pytest.
DIAG_PROCESS_PATTERN="${DIAG_PROCESS_PATTERN:-python[0-9]*.*(BinanceConnection|pos_diag)}"
PYTHON_BIN="${PYTHON_BIN:-$VENV_DIR/bin/python}"
SKIP_GIT_PULL="${SKIP_GIT_PULL:-0}"
SKIP_TESTS="${SKIP_TESTS:-0}"
RUNTIME_DIR="${RUNTIME_DIR:-$PROJECT_DIR/runtime}"
DEPLOY_SHA="${DEPLOY_SHA:-local}"
DEPLOY_REF="${DEPLOY_REF:-manual}"
DEPLOY_ACTOR="${DEPLOY_ACTOR:-manual}"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

# Encerra todos os processos que casam com um padrão, escalando SIGTERM -> SIGKILL.
# Verifica que morreram de fato antes de retornar — evita relançar o bot enquanto
# uma instância antiga ainda segura o lock (causa de duplicidade/crash-loop).
# Args: <label> <pgrep-pattern> [grace_seconds]
stop_pattern() {
  local label="$1"
  local pattern="$2"
  local grace="${3:-8}"

  if ! pgrep -f "$pattern" >/dev/null; then
    return 0
  fi

  log "$label detectado. Enviando SIGTERM..."
  pkill -TERM -f "$pattern" || true

  local waited=0
  while pgrep -f "$pattern" >/dev/null && [[ "$waited" -lt "$grace" ]]; do
    sleep 1
    waited=$((waited + 1))
  done

  if pgrep -f "$pattern" >/dev/null; then
    log "$label não encerrou em ${grace}s. Forçando SIGKILL..."
    pkill -KILL -f "$pattern" || true
    sleep 1
  fi

  if pgrep -f "$pattern" >/dev/null; then
    log "ALERTA: $label ainda presente após SIGKILL — verifique manualmente."
    return 1
  fi

  log "$label encerrado."
  return 0
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

if [[ "$SKIP_TESTS" == "1" || "$SKIP_TESTS" == "true" ]]; then
  log "SKIP_TESTS ativo. Pulando pytest (a CI do GitHub Actions já validou)."
else
  log "Executando testes (pytest) com ambiente isolado do .env de produção..."
  TRADING_BOT_ENV_FILE=/dev/null \
  TRADING_BOT_ENV=test \
  APP_ENV=test \
  "$PYTHON_BIN" -m pytest -q
fi

log "Reiniciando bot..."

# Tenta parada graciosa via screen (Ctrl+C), sem fechar posições no bot.
if screen -list | grep -q "[[:space:]]${SCREEN_NAME}[[:space:]]"; then
  log "Enviando Ctrl+C para sessão screen '$SCREEN_NAME'..."
  screen -S "$SCREEN_NAME" -X stuff $'\003'
  sleep 3
fi

# Premissa operacional: UM bot por VM. BOT_PROCESS_PATTERN casa com QUALQUER
# instância de trading_bot.core.bot — a rede (testnet/mainnet) vem do .env, não
# da linha de comando. Se houver >1 instância (ex.: testnet e mainnet rodando ao
# mesmo tempo), abortamos ANTES de matar qualquer coisa para não derrubar o
# ambiente errado. O `|| true` é necessário: sob `set -o pipefail`, o pgrep sem
# match retorna 1 e abortaria a atribuição.
bot_instances="$(pgrep -f "$BOT_PROCESS_PATTERN" | wc -l | tr -d ' ' || true)"
if [[ "${bot_instances:-0}" -gt 1 ]]; then
  log "ALERTA: $bot_instances instâncias de '$BOT_MODULE' rodando — esperado no máximo 1 (um bot por VM)."
  log "Abortando deploy para não encerrar o ambiente errado. Investigue: pgrep -af \"$BOT_PROCESS_PATTERN\""
  exit 1
fi

# Mata o wrapper PRIMEIRO — senão ele relança o bot logo após o kill abaixo.
# `|| log ...` evita que o set -e derrube o deploy se stop_pattern retornar !=0
# (straggler imortal): preferimos seguir e relançar a deixar o bot fora do ar.
stop_pattern "Wrapper de auto-restart antigo" "$WRAPPER_PROCESS_PATTERN" 5 \
  || log "AVISO: wrapper sobreviveu ao SIGKILL — seguindo com o deploy mesmo assim."

# Encerra processo(s) antigo(s) do bot com escalonamento até SIGKILL.
# Sem isso, uma instância presa (event loop morto) sobrevive ao SIGTERM e a
# nova aborta por lock duplicado — exatamente a falha que derrubou o Telegram.
# Idem: não deixamos um straggler imortal abortar o deploy (o wrapper já foi
# morto acima, então abortar aqui significaria bot 100% fora do ar).
stop_pattern "Processo antigo do bot" "$BOT_PROCESS_PATTERN" 8 \
  || log "AVISO: instância antiga sobreviveu ao SIGKILL — seguindo mesmo assim; bot novo pode entrar em crash-loop por lock duplicado até o straggler morrer (wrapper reinicia com backoff)."

# Limpa órfãos de diagnóstico (one-liners BinanceConnection / pos_diag presos).
if pgrep -f "$DIAG_PROCESS_PATTERN" >/dev/null; then
  log "Órfãos de diagnóstico detectados. Encerrando (SIGKILL)..."
  pkill -KILL -f "$DIAG_PROCESS_PATTERN" || true
  sleep 1
fi

# Fecha sessão screen anterior (se sobrou) para evitar duplicidade.
screen -S "$SCREEN_NAME" -X quit >/dev/null 2>&1 || true

log "Subindo nova sessão screen '$SCREEN_NAME' via wrapper de auto-restart..."
chmod +x "$BOT_WRAPPER" 2>/dev/null || true
screen -dmS "$SCREEN_NAME" bash -lc "PROJECT_DIR='$PROJECT_DIR' PYTHON_BIN='$PYTHON_BIN' BOT_MODULE='$BOT_MODULE' '$BOT_WRAPPER'"

# Health check com polling. Em VM Micro o import do Python (logo após
# pip install/pytest) costuma levar mais que 2s, então esperar uma checagem
# única gera falso "Falha ao iniciar bot". Faz polling até o timeout.
HEALTHCHECK_TIMEOUT="${HEALTHCHECK_TIMEOUT:-20}"
# Blinda contra valor não-numérico: senão `[[ -lt ]]` o trata como 0, pula o
# polling e regride pro bug (checagem única) que estamos consertando.
if ! [[ "$HEALTHCHECK_TIMEOUT" =~ ^[0-9]+$ ]]; then
  log "HEALTHCHECK_TIMEOUT inválido ('$HEALTHCHECK_TIMEOUT') — usando 20s."
  HEALTHCHECK_TIMEOUT=20
fi
hc_waited=0
while [[ "$hc_waited" -lt "$HEALTHCHECK_TIMEOUT" ]]; do
  if pgrep -af "$WRAPPER_PROCESS_PATTERN" >/dev/null && pgrep -af "$BOT_PROCESS_PATTERN" >/dev/null; then
    break
  fi
  sleep 1
  hc_waited=$((hc_waited + 1))
done

if pgrep -af "$WRAPPER_PROCESS_PATTERN" >/dev/null && pgrep -af "$BOT_PROCESS_PATTERN" >/dev/null; then
  log "Bot iniciado com sucesso (wrapper ativo após ${hc_waited}s)."
else
  log "Falha ao iniciar bot (wrapper ou python não detectado em ${HEALTHCHECK_TIMEOUT}s)."
  exit 1
fi

mkdir -p "$RUNTIME_DIR"
DEPLOY_INFO_FILE="$RUNTIME_DIR/deploy_info.json"
cat > "$DEPLOY_INFO_FILE" <<EOF
{
  "deployed_at_utc": "$(date -u '+%Y-%m-%dT%H:%M:%SZ')",
  "deploy_sha": "$DEPLOY_SHA",
  "deploy_ref": "$DEPLOY_REF",
  "deploy_actor": "$DEPLOY_ACTOR",
  "host": "$(hostname)"
}
EOF
log "Metadados de deploy salvos em $DEPLOY_INFO_FILE"

log "Concluído."

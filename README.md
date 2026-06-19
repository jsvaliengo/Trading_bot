# 🤖 Trading Bot — Estratégia Direcional (sinais + trailing)

Bot de trading automatizado para **Binance Futures** (mainnet/testnet), em Python. Opera de forma **direcional** (LONG **ou** SHORT por par, sem hedge), com entradas por pullback de tendência, gestão de risco baseada em risco-por-trade, trailing stop ancorado em ATR e um gate consultivo opcional por IA. Inclui dashboard web em tempo real, controle por Telegram, persistência durável em SQLite e deploy automatizado.

---

## ⚠️ Aviso importante

**Este bot envolve risco real de perda de capital.**

- Trading de cripto com alavancagem é extremamente arriscado.
- Nunca invista mais do que pode perder.
- Resultados passados não garantem resultados futuros.
- **Comece sempre na Testnet** antes de usar dinheiro real.
- Código com fins educacionais.

---

## 📊 Como funciona a estratégia

> Estratégia **direcional** (`USE_SIGNAL_STRATEGY=True`): no máximo **uma posição por par** — sem hedge (LONG+SHORT no mesmo símbolo) e sem pirâmide (empilhar no mesmo lado).

### 1. Entrada — pullback de tendência (`trend_strong`)
Exige tendência **alinhada em dois timeframes** (execução + confirmação):
- EMA9 > EMA21, preço > EMA200 e preço > VWAP (espelhado para SHORT);
- **pullback** à EMA9/21, com RSI na faixa (LONG 25–60 / SHORT 40–75);
- candle de **rejeição** ou engolfo na direção;
- volume ≥ 0,80× a média.

Indicadores: EMA (9/21/200), RSI(14), Bollinger(20, 2σ), ATR(14), ADX(14), VWAP.

### 2. Stop Loss / Take Profit
Calculados no momento da entrada e enviados como ordens **server-side** na Binance (`workingType=MARK_PRICE`, evita disparo por pavio). Três modos, nesta ordem:
1. **Estrutural** — SL ancorado no último fundo/topo ± buffer (clamp até 2,5%);
2. **ATR** — SL = ATR×1,5, clampado na banda do perfil;
3. **Fixo** — fallback por percentual.

O perfil `trend_strong` usa banda de **SL 1,0–1,5%**, **RR 3,0** (TP = SL × 3, cap 4,5%).

### 3. Sizing baseado em risco
`nocional = (capital × RISK_PER_TRADE_PCT) / (SL% × SLIPPAGE_BUFFER_MULT)` — a perda no stop fica ≈ ao risco-alvo **mesmo com slippage** do STOP_MARKET. Stop mais largo ⇒ posição menor (risco em $ constante).

### 4. Trailing stop ("breakeven cedo")
Ancorado em ATR: `activation = ATR×2,0` (piso 0,5%), `distance = ATR×0,5` (piso 0,4%). Quando o lucro atinge a ativação, o stop passa a perseguir o pico a `distance` dele, com um **piso de breakeven** (entrada + taxas + cushion) que garante saída ≥ taxas após armar — **não vira prejuízo**.

### 5. MFE (Maximum Favorable Excursion)
Cada trade registra `mfe_pct` — o quanto andou a favor (pico) antes de fechar. Usado para **calibrar o trailing com dado**, não com impressão (dashboard mostra a distribuição).

### 6. Seleção de pares e regime
Rotação dinâmica restrita a um **universo curado** (`BINANCE_UNIVERSE_WHITELIST`), por score (volume, tendência/ADX, funding, spread, volatilidade, RVOL), com piso de moedas. Um **classificador de regime** (ADX + Bollinger width) rotaciona um par ocioso em regime non-trend pelo melhor candidato.

### 7. Gate consultivo de IA (opcional)
`AI_MODE` = `off` / `shadow` (avalia, não bloqueia) / `gated` (bloqueia entradas "esticadas"). Envia um snapshot de mercado à OpenAI e recebe uma decisão estruturada. Em falha da API, é **fail-open** (entra com base nas regras técnicas).

### 8. Proteções
- **DCA** opcional (reforços em níveis ancorados em ATR).
- **Cooldown de reentrada** por símbolo após loss (anti-churn).
- **Kill switch** — pausa em loss-streak / drawdown do pico; alerta em win-rate baixo.
- **Stop global** (circuit breaker) e **metas diárias** de lucro/perda.
- **Panic guard** — `/closeall` em drawdown exige confirmação.

---

## 🏗️ Arquitetura

Monolito Python de **processo único** com loop contínuo. Camadas:

| Módulo | Responsabilidade |
|---|---|
| `core/` | Orquestração (`bot.py`), estratégia/sinais (`strategy.py`), config, persistência (`trade_store.py`, SQLite), bookkeeping (`trade_ledger.py`), tracker de posições, scheduler |
| `execution/` | Abrir/fechar posições com checagens de risco; P&L real; stop global |
| `infra/` | Cliente Binance (REST) + WebSocket de klines + user-stream |
| `services/` | Comandos Telegram, seleção de pares, kill switch, notificações |
| `ai/` | Gate consultivo (OpenAI) |
| `web/` | Dashboard Flask + SocketIO |
| `observability/` | Métricas Prometheus |

**Fonte de verdade:** o **SQLite** (`TradeStore`) guarda trades, P&L acumulado e histórico de equity; os contadores em memória são ancorados nele no boot. O estado operacional (posições abertas, kill switch, pares) fica em JSON.

**Fluxo de um ciclo:** `LoopScheduler` alterna *análise* (gera sinal → gate IA → `ExecutionEngine` abre) e *monitoramento* (atualiza pico/MFE, avalia trailing/SL/TP/DCA, fecha). Todo fechamento passa por `record_trade_closed` (idempotente) → `record_close` no SQLite.

---

## 📋 Pré-requisitos

- **Python 3.11+**
- Conta na Binance Futures (Testnet para começar)
- (Opcional) Bot do Telegram, chave OpenAI, Docker (observability)

---

## 🚀 Instalação

```bash
git clone <repo> trading_bot && cd trading_bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # edite com suas chaves
```

---

## 🔧 Configuração (`.env`)

Toda config tem default no `config.py` e pode ser sobrescrita por env (`TRADING_BOT_*`). Principais grupos:

**Credenciais / rede**
```
BINANCE_MAINNET_API_KEY / _SECRET
BINANCE_TESTNET_API_KEY / _SECRET
TRADING_BOT_ENVIRONMENT=testnet        # ou mainnet
TRADING_BOT_SIMULATED_BALANCE_USD=130  # cap de saldo só em testnet
```

**Telegram**
```
TELEGRAM_TOKEN= / TELEGRAM_CHAT_ID=
```

**Risco / estratégia** (defaults atuais)
```
LEVERAGE=20
RISK_PER_TRADE_PCT=1.0                  # risco por trade (% do capital)
# risk_profile do trend_strong (no config.py): SL 1.0–1.5%, RR 3.0, TP_max 4.5%
TRADING_BOT_TRAILING_ACTIVATION_MIN_PERCENT / _DISTANCE_MIN_PERCENT  # pisos do trailing
TRADING_BOT_KILL_SWITCH_ENABLED=true
TRADING_BOT_KILL_SWITCH_LOSS_STREAK_DAYS=3
```

**IA (gate consultivo)**
```
TRADING_BOT_AI_MODE=off|shadow|gated
TRADING_BOT_AI_MODEL=gpt-5-mini
OPENAI_API_KEY=
```

**Dashboard web** (opt-in — só sobe com usuário+senha)
```
TRADING_BOT_DASHBOARD_ENABLED=true
TRADING_BOT_DASHBOARD_PORT=5050
TRADING_BOT_DASHBOARD_USERNAME= / _PASSWORD=
```

**Observabilidade**
```
TRADING_BOT_METRICS_ENABLED=true
TRADING_BOT_METRICS_PORT=9090
TRADING_BOT_WEBSOCKET_ENABLED=true      # false = REST puro
```

---

## ▶️ Executando

```bash
# Testnet (recomendado para começar)
TRADING_BOT_ENVIRONMENT=testnet .venv/bin/python -m trading_bot.core.bot

# Mainnet (DINHEIRO REAL — exige confirmação)
TRADING_BOT_ENVIRONMENT=mainnet TRADING_BOT_MAINNET_CONFIRM=sim .venv/bin/python -m trading_bot.core.bot
```

Em produção o bot roda sob um wrapper de auto-restart (`scripts/run_bot_loop.sh`) dentro de uma sessão `screen`. A troca de rede também pode ser feita em runtime via `/env` no Telegram (passa por um gate de promoção por expectância antes de ir pra mainnet).

---

## 📈 Dashboard web

Painel de observação e controle em tempo real (Flask + SocketIO), **opt-in** e protegido por HTTP Basic Auth (`127.0.0.1:5050`).

- **Overview single-screen:** equity, posições abertas (com barra visual `SL ─ entrada ─ preço ─ TP`), trades recentes (com régua de MFE), win/loss, P&L por moeda, **distribuição de MFE**, drawdown e regime.
- **Controles:** Pausar / Retomar / Fechar tudo (com panic guard).
- Tempo real via SocketIO, com fallback para polling em `/api/snapshot`.

> ⚠️ Hoje bind em localhost + basic auth. **Antes de expor remotamente**, configure TLS + proxy reverso (e idealmente 2FA).

---

## 📊 Observabilidade (Prometheus + Grafana)

Stack opcional em Docker (`trading_bot/observability/docker-compose.yml`): Prometheus + Grafana com datasource auto-provisionado e dashboards prontos (visão geral + diagnóstico operacional). O bot expõe `/metrics` (gauges de estado/P&L + counters de trades/ordens/erros/streams).

```bash
cd trading_bot/observability && docker compose up -d   # roda local; não cabe na VM Micro
```

---

## ✅ CI e Deploy

- **CI** (`.github/workflows/ci.yml`): `ruff` + `mypy` (escopo tipado) + `pytest`.
- **Deploy automático** (`deploy-oracle.yml`) no push pra `main`: roda os testes, faz **backup**, **rsync** do código pra VM Oracle (preserva `.env`/`runtime/`), reinicia o bot via `update_server.sh` e tem **rollback automático** em falha. Rollback manual em `rollback-oracle.yml`.

> A VM **não é um repo git** — recebe código por rsync. O encerramento do bot é determinístico (`os._exit` após salvar o estado), então o wrapper respawna sem `kill -9` manual.

---

## 📱 Comandos Telegram (principais)

**Controle:** `/start` `/pause` `/resume` `/stop` `/stop force` `/closeall`
**Leitura:** `/status` `/portfolio` `/trades` `/positions` `/balance` `/config` `/dailyreport` `/apihealth` `/lockinfo`
**Risco em runtime:** `/sl [min] [max]` · `/tp [min] [max]` · `/trailing [ativação] [distância]` · `/leverage [N]` · `/ordersize [usd]` · `/drawdown [pct]`
**Estratégia/pares:** `/coins [lista]` · `/strategy` · `/rescore` · `/invert on|off` · `/double` · `/env [testnet|mainnet confirmar]`

> `/sl` e `/tp` editam a banda do `risk_profile`; `/trailing` edita os pisos reais do ATR (MIN/MAX). Ajustes valem para **novas posições** e não persistem no restart (use `.env`/config para fixar).

---

## 📁 Estrutura

```
trading_bot/
├── core/         # bot, strategy, config, trade_store (SQLite), trade_ledger, scheduler, ...
├── execution/    # engine.py (abre/fecha, stop global)
├── infra/        # binance_client, binance_streams, binance_user_stream
├── services/     # telegram_commands, pair_selector, kill_switch, notifications
├── ai/           # consultive_engine (gate IA)
├── web/          # server, app, data, auth + templates/static (dashboard)
└── observability/# metrics + docker-compose (Prometheus/Grafana)
scripts/          # run_bot_loop.sh, update_server.sh, heartbeat.py, ...
tests/            # pytest (suíte de regressão)
```

---

## 🔑 Obtendo API Keys

- **Testnet:** https://testnet.binancefuture.com → gere API key/secret de Futures.
- **Mainnet:** Binance → API Management → habilite **Futures**, restrinja por IP, **nunca** habilite saques.

---

## ❓ FAQ

- **Abre LONG e SHORT no mesmo par?** Não — é direcional; guards bloqueiam hedge e pirâmide (uma posição por par).
- **O que acontece se a OpenAI cair (modo gated)?** Fail-open: entra com base nas regras técnicas.
- **De onde vêm os números do dashboard?** Saldo/P&L da Binance; contadores/histórico do SQLite (fonte de verdade).
- **Posso usar sem Telegram/IA/dashboard?** Sim — todos são opcionais (a IA por `AI_MODE=off`, o dashboard por `DASHBOARD_ENABLED=false`).

---

## 📜 Licença

Uso educacional. Veja `LICENSE` (se presente). Use por sua conta e risco.

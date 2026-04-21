# 🤖 Bot de Trading - Estratégia Hedge + DCA

Bot de trading automatizado para Binance Futures.

## ⚠️ AVISO IMPORTANTE

**Este bot envolve risco real de perda de capital.**

- Trading de criptomoedas com alavancagem é extremamente arriscado
- Nunca invista mais do que pode perder
- Resultados passados não garantem resultados futuros
- Comece SEMPRE na Testnet antes de usar dinheiro real
- Este código é para fins educacionais

---

## 📋 Pré-requisitos

1. Python 3.9 ou superior
2. Conta na Binance (ou Binance Testnet para testes)
3. API Key e Secret da Binance

---

## 🚀 Instalação

### 1. Clone ou baixe os arquivos

```bash
# Crie uma pasta para o projeto
mkdir trading_bot
cd trading_bot
```

### 2. Crie o ambiente virtual e instale dependências

```bash
python3 -m venv .venv
source .venv/bin/activate  # Linux/Mac
pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Configure suas API Keys

**Opção A (recomendada) - arquivo `.env`:**

```bash
cp .env.example .env
```

Preencha o `.env` com seus dados. As credenciais da Binance são separadas por rede —
a rede ativa é controlada por `TRADING_BOT_ENVIRONMENT` (ou pelo comando `/env` no Telegram):

```bash
BINANCE_MAINNET_API_KEY="sua_api_key_mainnet"
BINANCE_MAINNET_API_SECRET="sua_api_secret_mainnet"
BINANCE_TESTNET_API_KEY="sua_api_key_testnet"
BINANCE_TESTNET_API_SECRET="sua_api_secret_testnet"
TRADING_BOT_ENVIRONMENT=testnet
TELEGRAM_TOKEN="seu_token_aqui"
TELEGRAM_CHAT_ID="seu_chat_id_aqui"
```

---

## 🔧 Configuração

O fluxo recomendado é ajustar via `.env` (o bot também suporta variáveis de ambiente no shell).
Os valores abaixo são os defaults atuais em `trading_bot/core/config.py`.

### Parâmetros Essenciais

| Parâmetro | Padrão | Descrição |
|-----------|--------|-----------|
| `TRADING_BOT_ENVIRONMENT` | `testnet` | Rede ativa (`mainnet` ou `testnet`). Trocável via `/env` no Telegram. |
| `TOTAL_CAPITAL` | `100.0` | Referência de capital (não é o saldo real da conta) |
| `LEVERAGE` | `20` | Alavancagem por posição |
| `MAX_POSITION_PERCENT` | `0.08` | % do capital por trade (8%) |

### Parâmetros de Risco

| Parâmetro | Padrão | Descrição |
|-----------|--------|-----------|
| `STOP_LOSS_PERCENT` | `3.0` | Stop Loss individual (%) quando habilitado |
| `USE_INDIVIDUAL_STOP_LOSS` | `False` | Liga/desliga stop loss individual por posição |
| `TAKE_PROFIT_PERCENT` | `8.0` | Take Profit em % |
| `MAX_DAILY_LOSS_PERCENT` | `10.0` | Perda máxima diária em % |
| `MAX_OPEN_POSITIONS` | `12` | Máximo de posições simultâneas |

### Parâmetros de DCA

| Parâmetro | Padrão | Descrição |
|-----------|--------|-----------|
| `DCA_ENABLED` | `True` | Ativa/desativa DCA |
| `DCA_MAX_ORDERS` | `3` | Número máximo de ordens DCA |
| `DCA_STEP_PERCENT` | `2.0` | Queda % para cada DCA |
| `DCA_MULTIPLIER` | `1.5` | Multiplicador do tamanho |

### Ambiente e Runtime

Você pode padronizar execução por ambiente com variáveis:

```bash
export TRADING_BOT_ENV=prod
export TRADING_BOT_RUNTIME_DIR=runtime
export TRADING_BOT_LOG_LEVEL=INFO
export TRADING_BOT_DAILY_REPORT_ENABLED=true
export TRADING_BOT_DAILY_REPORT_HOUR_BRT=23
export TRADING_BOT_DAILY_REPORT_MINUTE_BRT=55
export TRADING_BOT_DAILY_REPORT_LOOKBACK_HOURS=24
export TRADING_BOT_DOUBLE_FIRST_LONG_ENABLED=false
export TRADING_BOT_DOUBLE_FIRST_SHORT_ENABLED=false
export TRADING_BOT_DOUBLE_FIRST_MULTIPLIER=2.0
export TRADING_BOT_DOUBLE_FIRST_MAX_MARGIN_USDT=0
export TRADING_BOT_DOUBLE_FIRST_SCOPE=global
export TRADING_BOT_SENTIMENT_FILTER_ENABLED=false
export TRADING_BOT_SENTIMENT_TIMEFRAME=1h
export TRADING_BOT_SENTIMENT_LOOKBACK_CANDLES=120
export TRADING_BOT_SENTIMENT_MIN_SCORE=2
export TRADING_BOT_SENTIMENT_MIN_MOMENTUM_PERCENT=0.20
export TRADING_BOT_SENTIMENT_CACHE_SECONDS=300
export TRADING_BOT_MAINNET_CONFIRM=eu_sei_o_risco

# Observabilidade (Prometheus/Grafana)
export TRADING_BOT_METRICS_ENABLED=true
export TRADING_BOT_METRICS_HOST=127.0.0.1
export TRADING_BOT_METRICS_PORT=9090

# WebSocket kline streams (kill switch = false para forçar REST puro)
export TRADING_BOT_WEBSOCKET_ENABLED=true
export TRADING_BOT_WEBSOCKET_STALENESS_SECONDS=30
```

Arquivos de runtime ficam em `runtime/` por ambiente e rede:
- `bot_state.<env>.<network>.json` — estado persistido
- `trading_bot.<env>.<network>.lock` — instância única
- `trading_bot.<env>.<network>.log` — log rotativo
- `active_environment.txt` — rede ativa (atualizado por `/env`)

O bot carrega automaticamente `.env` (e `.env.local`, se existir).
Para usar outro arquivo, defina:

```bash
export TRADING_BOT_ENV_FILE=/caminho/para/seu.env
```

---

## ▶️ Executando

### Modo Testnet (recomendado para começar)

1. Configure `BINANCE_TESTNET_API_KEY` e `BINANCE_TESTNET_API_SECRET` no `.env`
2. Defina `TRADING_BOT_ENVIRONMENT=testnet` (default se omitido)
3. Obtenha API keys da Testnet: https://testnet.binancefuture.com/
4. Execute:

```bash
python -m trading_bot.core.bot
```

### Modo Mainnet (CUIDADO — dinheiro real)

1. Configure `BINANCE_MAINNET_API_KEY` e `BINANCE_MAINNET_API_SECRET` no `.env`
2. Defina `TRADING_BOT_ENVIRONMENT=mainnet` (ou troque em runtime via `/env mainnet confirmar`)
3. **IMPORTANTE**: Crie API keys apenas com permissão de Trade, SEM permissão de Withdraw
4. Execute:

```bash
python -m trading_bot.core.bot
```

### Trocar de rede em runtime (sem restart)

Pelo Telegram:
- `/env` — mostra rede ativa e credenciais configuradas
- `/env testnet` — troca para testnet
- `/env mainnet confirmar` — troca para mainnet (exige confirmação explícita)

State files são separados por rede (`bot_state.{app_env}.{mainnet|testnet}.json`), evitando mistura de métricas.

Em MAINNET sem terminal interativo (server/CI), defina antes:

```bash
TRADING_BOT_MAINNET_CONFIRM=eu_sei_o_risco
```

### Monitoramento

Controle e status rápido: comandos do Telegram (`/status`, `/portfolio`, `/positions`, `/balance`, `/config`, `/apihealth` — use `/help`).

Métricas em tempo real e histórico: Prometheus + Grafana (ver seção abaixo).

### 📈 Métricas Prometheus + Grafana

O bot sobe um exporter HTTP embutido em `http://METRICS_HOST:METRICS_PORT/metrics` quando `TRADING_BOT_METRICS_ENABLED=true` (padrão).

Métricas expostas (prefixo `trading_bot_`):
- **Estado**: `running`, `paused`, `daily_target_reached`, `positions_open_count`
- **Financeiro**: `account_balance_usd`, `pnl_realized_total_usd`, `pnl_realized_daily_usd`, `peak_equity_usd`, `drawdown_from_peak_percent`, `fees_paid_total_usd`
- **Por posição**: `position_pnl_unrealized_usd{symbol,side}`, `position_notional_usd{symbol,side}`
- **Eventos** (counters): `trades_closed_total{result,strategy,symbol,close_reason}`, `trades_pnl_usd_total{result,close_reason}`, `orders_placed_total{side,result}`, `binance_api_errors_total{endpoint,code}`
- **Cache** (counters): `cache_hits_total{method}`, `cache_misses_total{method}` — método: `balance`, `funding_rate`, `daily_pnl`, `klines_ws`
- **WebSocket**: `ws_subscriptions_active`, `ws_stream_age_seconds{symbol,interval}`, `ws_stream_messages_total{symbol,interval}`, `ws_stream_buffer_size{symbol,interval}`
- **Info**: `trading_bot_info{environment,app_env}` — labels da rede ativa

Exemplo de `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: trading_bot
    scrape_interval: 15s
    static_configs:
      - targets: ['127.0.0.1:9090']
```

**Stack completa em Docker** (pronta pra subir): [`trading_bot/observability/docker-compose.yml`](trading_bot/observability/docker-compose.yml) sobe Prometheus (porta 9091) + Grafana (porta 3000) com datasource auto-provisionado e 2 dashboards prontos:

- **Trading Bot** ([`grafana_dashboard.json`](trading_bot/observability/grafana_dashboard.json)) — visão geral: status, P&L realizado/hoje/acumulado, equity vs pico, drawdown, P&L não realizado por posição, trades por motivo, win rate, ordens, erros API, WebSocket streams
- **Trading Bot — Diagnóstico** ([`grafana_dashboard_diagnostico.json`](trading_bot/observability/grafana_dashboard_diagnostico.json)) — operacional: saúde geral, streams WS stale, cache hit rates (4 gauges), cache ops rate, tabela de streams, P&L por motivo de fechamento (SL/TP/Trailing/Outros), net por estratégia

```bash
cd trading_bot/observability
docker compose up -d
open http://127.0.0.1:3000   # admin / admin
```

Ver [`PROMETHEUS_QUERIES.md`](PROMETHEUS_QUERIES.md) para cheatsheet de queries úteis.

### ⚡ Performance — WebSocket + Caches

A camada de integração com Binance usa dois mecanismos pra reduzir latência e volume de REST calls:

**WebSocket Kline Streams** ([`trading_bot/infra/binance_streams.py`](trading_bot/infra/binance_streams.py))
- Stream em tempo real de velas Futures via `ThreadedWebsocketManager`
- Padrão **seed + increment**: fetch REST inicial (400 velas) + atualização incremental via WS
- Só aceita velas fechadas (preserva comportamento da estratégia)
- **Fallback REST automático** se WS stale (>30s sem msg), buffer insuficiente ou store desligado
- Sincronização automática com `TRADING_PAIRS` em toda mudança de pares
- Kill switch: `TRADING_BOT_WEBSOCKET_ENABLED=false`

**TTL Caches** ([`trading_bot/infra/binance_client.py`](trading_bot/infra/binance_client.py))

| Endpoint | TTL | Invalidação |
|---|---|---|
| `get_account_balance` / `get_available_balance` | 2s | Automática após `place_market_order` |
| `get_daily_pnl_from_binance` | 30s | Automática após `place_market_order` |
| `get_funding_rate` | 300s (5min) | Manual via `force_refresh=True` |
| `klines_ws` (via WebSocket) | efêmero | — |
| `get_symbol_info` / `get_exchange_info` | 6h | Manual |

Todo cache tem fallback **stale-on-failure** — se a API da Binance falhar, retorna a última leitura boa em vez de zerar valores críticos (balance, P&L).

**Impacto medido na prática** (comparação 16h vs 18min pós-fix, em rate/hora):

| Endpoint | Antes | Depois | Redução |
|---|---|---|---|
| `futures_klines` | 938/h | 93/h | **-90%** |
| `futures_funding_rate` | 244/h | ~0/h | **~100%** |
| `futures_income_history_daily` | 760/h | ~0/h | **~100%** |

**Latência de análise por símbolo**: avg 1.46s → 0.12s, max 14.99s → 0.88s.

### 🎯 Threshold do Health Report

O status em `/apihealth` é classificado em **CRÍTICO / ATENÇÃO / ESTÁVEL** via [`TradingBot._classify_api_health_status`](trading_bot/core/bot.py):

| Status | Condições |
|---|---|
| **CRÍTICO** | `failures > 10` **OU** `failure_rate >= 1%` **OU** `order_failures > 5` **OU** `order_rejection_rate >= 5%` **OU** `loop_errors > 0` |
| **ATENÇÃO** | Qualquer instabilidade abaixo dos thresholds de CRÍTICO (retries, overruns, falhas pontuais) |
| **ESTÁVEL** | Zero indicadores de problema |

Ajustado pra não disparar alarme falso com 1-2 falhas pontuais em dezenas de milhares de chamadas.

---

## ✅ CI e Update Seguro no Servidor

Para automatizar testes a cada atualização e só reiniciar o bot se tudo passar:

```bash
./scripts/update_server.sh
```

O script faz:
1. `git pull --ff-only` (se a pasta for um repositório git)
2. instala/atualiza dependências da `.venv`
3. roda `pytest -q`
4. reinicia o bot na sessão `screen` somente se os testes passarem

Variáveis opcionais:

```bash
SCREEN_NAME=bot PROJECT_DIR=/home/ubuntu/trading_bot TRADING_BOT_ENV=prod BOT_MODULE=trading_bot.core.bot ./scripts/update_server.sh
```

Se rodar em mainnet no servidor, garanta no `.env`:

```bash
TRADING_BOT_ENVIRONMENT=mainnet
TRADING_BOT_MAINNET_CONFIRM=eu_sei_o_risco
```

Comandos úteis no Telegram:
- `/dailyreport now` envia relatório de performance das últimas N horas
- `/dailyreport on` e `/dailyreport off` ligam/desligam envio automático diário

Também foi adicionado CI no GitHub Actions em:

`/.github/workflows/ci.yml`

Ele roda `pytest` em todo `push` e `pull request`.

### Deploy Oracle com rollback automático

Workflow:

`/.github/workflows/deploy-oracle.yml`

Ele:
1. roda `pytest` no GitHub Actions
2. cria backup do código atual no servidor (`/home/ubuntu/deploy_backups/trading_bot/...`)
3. sincroniza o novo código
4. executa `scripts/update_server.sh`
5. em falha, faz rollback automático do backup
6. coleta diagnóstico (processo do bot, `runtime/deploy_info.json` e tail de log)

### Rollback manual (quando quiser voltar rápido)

Workflow manual:

`/.github/workflows/rollback-oracle.yml`

No GitHub, abra **Actions > Rollback Oracle > Run workflow** e:
- deixe `backup_dir` vazio para usar o último backup
- ou informe o caminho completo de um backup específico

Também dá para executar direto no servidor:

```bash
cd ~/trading_bot
./scripts/rollback_server.sh
```

Para escolher backup específico:

```bash
BACKUP_DIR=/home/ubuntu/deploy_backups/trading_bot/20260228-120000-abc1234 ./scripts/rollback_server.sh
```

---

## 📊 Como Funciona a Estratégia

### 1. Hedge Mode

O bot abre posições LONG e SHORT simultaneamente:
- Reduz o risco de movimentos bruscos
- Se o mercado cair, a posição SHORT compensa parte da perda da LONG
- Se o mercado subir, a posição LONG lucra mais que a perda da SHORT

Com o **filtro de sentimento** ativo, novas entradas podem ser limitadas para apenas uma direção por par (LONG-only ou SHORT-only), conforme o viés detectado.

### 2. Análise Técnica

O bot usa múltiplos indicadores para decidir a direção:
- **EMA (9 e 21)**: Identifica tendência
- **RSI (14)**: Mede momentum e sobrecompra/sobrevenda
- **Bollinger Bands**: Detecta volatilidade

### 3. Tamanho das Posições

Baseado na força do sinal:
- **Sinal forte de alta**: 70% LONG / 30% SHORT
- **Sinal moderado de alta**: 60% LONG / 40% SHORT
- **Neutro**: 50% LONG / 50% SHORT
- **Sinal moderado de baixa**: 40% LONG / 60% SHORT
- **Sinal forte de baixa**: 30% LONG / 70% SHORT

Também há o recurso **Double First** (opcional), que dobra a primeira entrada LONG e/ou SHORT (escopo global ou por símbolo), com limite de margem configurável.

### 4. DCA (Dollar Cost Averaging)

Se o preço vai contra a posição principal:
- Adiciona mais à posição em níveis pré-definidos
- Reduz o preço médio de entrada
- Aumenta o potencial de lucro na recuperação

---

## 📁 Estrutura dos Arquivos

```
trading_bot/
├── runtime/                  # State/lock/log por ambiente e rede
├── trading_bot/              # Código principal
│   ├── __init__.py
│   ├── core/
│   │   ├── bot.py            # Orquestrador: lifecycle, monitor, analysis loop
│   │   ├── config.py         # Centralização de configurações
│   │   ├── strategy.py       # Indicadores, sinais, geração de setups
│   │   ├── state_manager.py  # Persistência atômica + backup + migração
│   │   └── scheduler.py      # LoopScheduler + timing profile do loop
│   ├── execution/
│   │   └── engine.py         # Fechamento de posições + Global Stop Loss
│   ├── infra/
│   │   ├── binance_client.py # API REST + caches TTL
│   │   └── binance_streams.py# WebSocket kline streams
│   ├── ai/
│   │   └── consultive_engine.py
│   ├── observability/
│   │   ├── metrics.py                       # Métricas Prometheus
│   │   ├── docker-compose.yml               # Stack Prom + Grafana
│   │   ├── prometheus.yml                   # Scrape config
│   │   ├── grafana_dashboard.json           # Dashboard principal
│   │   ├── grafana_dashboard_diagnostico.json # Diagnóstico operacional
│   │   └── grafana_provisioning/            # Datasources + dashboards auto-load
│   └── services/
│       ├── notifications.py  # Telegram push (envio)
│       ├── pair_selector.py  # Scoring de pares
│       └── telegram_commands.py  # Comandos /status, /env, /trades, etc
├── tests/
│   ├── test_bot_regressions.py
│   ├── test_services_regressions.py
│   ├── test_metrics.py
│   ├── test_state_manager.py
│   ├── test_scheduler.py
│   ├── test_binance_client_cache.py
│   ├── test_binance_client_ws_integration.py
│   ├── test_binance_streams.py
│   └── test_risk_manager.py
├── scripts/
│   ├── test_connection.py
│   ├── test_config.py
│   ├── test_funding.py
│   ├── rollback_server.sh
│   └── update_server.sh
├── PROMETHEUS_QUERIES.md     # Cheatsheet de queries PromQL
├── pytest.ini
├── .github/workflows/ci.yml
├── .github/workflows/deploy-oracle.yml
├── .github/workflows/rollback-oracle.yml
├── requirements.txt
└── README.md
```

---

## 🏗️ Arquitetura

O código é organizado por **responsabilidade** em camadas. `TradingBot` (em `core/bot.py`) é o orquestrador — ele instancia e coordena os outros módulos. Essa separação permite testar cada peça isoladamente e evoluir sem tocar no fluxo principal.

### Camada `core/`

| Módulo | Responsabilidade | Exposto por |
|---|---|---|
| **`bot.py`** | Orquestrador: lifecycle, monitor loop, analysis loop, integração com Telegram/risk/estratégia | `TradingBot` |
| **`config.py`** | Singleton de configuração (credenciais, timings, thresholds, estratégias) | `config` |
| **`strategy.py`** | Indicadores técnicos (EMA, RSI, Bollinger), geração de sinais, `RiskManager` | `HedgeStrategy`, `RangeScalpingStrategy`, `RiskManager` |
| **`state_manager.py`** | I/O atômico de state: escrita tmp+rename, backup `.bak`, leitura com fallback, migração legacy | `StateManager` |
| **`scheduler.py`** | `LoopScheduler` (agenda tarefas periódicas do loop principal) + timing profile dinâmico por faixa de pares | `LoopScheduler`, `get_loop_timing_profile`, `timing_profile_changed` |

### Camada `execution/`

| Módulo | Responsabilidade | Exposto por |
|---|---|---|
| **`engine.py`** | Abertura direcional de trade (com checagens de risco: exposição, concentração, idade do sinal, minNotional, double_first), fechamento de posições com cálculo de P&L, bulk close para meta diária, checagem e execução de Global Stop Loss | `ExecutionEngine` |

O `ExecutionEngine` mantém referência ao `TradingBot` — o acoplamento de DADOS (stats, known_positions, trade_history, telegram) permanece ali, mas o CÓDIGO está separado para isolamento.

### Camada `infra/`

| Módulo | Responsabilidade | Exposto por |
|---|---|---|
| **`binance_client.py`** | Cliente REST da Binance Futures + TTL caches (balance 2s, funding 5min, daily_pnl 30s) + delegação pra WebSocket | `BinanceConnection` |
| **`binance_streams.py`** | WebSocket kline store com seed REST + incremento WS, buffer thread-safe por `(symbol, interval)` | `WebSocketKlineStore` |

### Camada `services/`

| Módulo | Responsabilidade |
|---|---|
| **`notifications.py`** | `TelegramNotifier` — envio de mensagens push (startup, position opened/closed, alerts) |
| **`telegram_commands.py`** | `TelegramCommandHandler` — polling e handlers dos comandos `/status`, `/env`, `/trades`, `/apihealth`, `/sl`, `/tp`, `/leverage`, `/closeall`, etc |
| **`pair_selector.py`** | `PairSelector` — scoring de pares por volume/volatilidade/spread/trend/funding |

### Camada `observability/`

Métricas Prometheus + stack Docker pronta (Prometheus + Grafana com 2 dashboards auto-provisionados). Ver seção [📈 Métricas Prometheus + Grafana](#-métricas-prometheus--grafana).

### Camada `ai/`

`consultive_engine.py` — engine de consulta à LLM (modo off/consultive/gated) para segunda opinião em sinais antes da execução.

### Fluxo típico de um tick

```
 TradingBot.run()
       │
       ├─── LoopScheduler.due("terminal_status") ──► print_status()
       │                            ("state_save") ──► StateManager.save()
       │                            ("commission")  ──► update_commission_rates()
       │                            ("deposit_check")── check_for_deposit()
       │                            ("strategy_check") check_and_update_binance_strategy()
       │
       ├─── monitor_positions()  ──► exchange.get_open_positions() ──► WS cache
       │                              │
       │                              └► ExecutionEngine.close_position_with_notification()
       │                                  ├─► exchange.close_position()
       │                                  ├─► telegram.send_position_closed()
       │                                  └─► metrics.record_trade_closed()
       │
       └─── analyze_and_trade(symbol)  ──► strategy.generate_trade_setup()
                                            └─► ExecutionEngine.open_signal_trade()
                                                 ├─► checagens de risco (exposição, concentração, idade)
                                                 ├─► exchange.place_market_order()
                                                 ├─► exchange.set_stop_loss_take_profit()
                                                 └─► telegram.send_trade_alert() + metrics.record_order()
```

### Histórico de refatorações

O código foi evoluído em fases, cada uma entregando valor sem quebrar comportamento (todas as extrações preservaram 100% dos testes de regressão existentes).

**Fase 1 — Limpeza + Observabilidade**
- Removido dashboard web (2.952 linhas), substituído por Prometheus + Grafana
- Toggle `testnet` ↔ `mainnet` via comando `/env` no Telegram
- Dead code removal (vulture + ruff): ~15% do código Python eliminado

**Fase 3.1/3.2 — Performance (reduziu latência de análise em ~12×)**
- TTL caches em endpoints REST de alta frequência (balance, funding_rate, daily_pnl)
- WebSocket kline streams com fallback REST transparente
- Resultado: `futures_klines` calls caíram de 938/h → 93/h; latência avg de análise 1.46s → 0.12s

**Fase 2 — Refatoração do god class `bot.py`**
- `bot.py`: 5.551 → 5.133 linhas
- Extraído `StateManager` (I/O de state — 160 linhas, 11 testes novos)
- Extraído `LoopScheduler` (timing do loop — 146 linhas, 20 testes novos)
- Extraído `ExecutionEngine` (close logic — 329 linhas)

**Fase 2.5 — Abertura de trade no `ExecutionEngine`**
- `bot.py`: 5.133 → 4.688 linhas (**-863 linhas desde início da Fase 2**, -15.5%)
- `execute_signal_trade` (450+ linhas) migrado pra `ExecutionEngine.open_signal_trade`
- Engine agora cobre open + close + bulk close + Global SL
- Delegador preservado em `bot.execute_signal_trade()` → zero quebra externa

---

## 🛠️ Obtendo API Keys

### Binance Testnet (para testes)

1. Acesse https://testnet.binancefuture.com/
2. Faça login com GitHub
3. Vá em "API Management"
4. Crie uma nova API key
5. Copie API Key e Secret

### Binance Real

1. Acesse https://www.binance.com/
2. Vá em Perfil > API Management
3. Crie uma nova API
4. **IMPORTANTE**: 
   - ✅ Habilite apenas "Enable Futures"
   - ❌ NÃO habilite "Enable Withdrawals"
   - Configure restrição de IP se possível
5. Copie API Key e Secret

---

## 🔍 Monitoramento

O bot gera logs detalhados:

- **No terminal**: Logs em tempo real
- **Arquivo `runtime/trading_bot.<env>.log`**: Histórico completo

### Exemplo de log:

```
2024-01-15 10:30:00 - INFO - 🔍 Analisando BTCUSDT...
2024-01-15 10:30:01 - INFO - 📊 Trade Setup para BTCUSDT:
        ├── Sinal: BUY
        ├── Preço: $42000.00
        ├── LONG: $1.75 USDT
        ├── SHORT: $0.75 USDT
        ├── Stop Loss: $39900.00
        └── Take Profit: $43260.00
```

---

## 📱 Comandos Telegram (principais)

Controle operacional:
- `/start` retoma se estiver pausado (não sobe processo parado)
- `/stop` para o bot **mantendo posições abertas**
- `/stop force` para e tenta fechar todas as posições
- `/pause` pausa novas entradas e mantém gerenciamento das posições
- `/resume` retoma entradas

Relatórios sob demanda:
- `/status` status geral (capital, P&L total realizado, trades, config)
- `/portfolio` evolução da carteira
- `/trades` relatório de trades fechados
- `/apihealth` saúde operacional (API/retries/falhas/loops)

Relatório diário:
- `/dailyreport` mostra status do agendamento
- `/dailyreport now` envia o relatório imediatamente
- `/dailyreport on` / `/dailyreport off` liga/desliga envio automático

Filtro de sentimento (direção de entrada):
- `/sentiment` ou `/sentiment status` mostra estado atual
- `/sentiment on` ativa filtro direcional por viés
- `/sentiment off` (ou `/sentiment normal`) volta ao modo normal
- `/sentiment SOL` consulta viés do par (`SOLUSDT`)

Rede Binance (mainnet / testnet):
- `/env` mostra rede ativa + credenciais configuradas
- `/env testnet` troca pra testnet (sem confirmação)
- `/env mainnet confirmar` troca pra mainnet (exige confirmação explícita)

Ajustes de risco em runtime:
- `/leverage [N]` altera alavancagem
- `/sl [pct]` / `/sl on` / `/sl off` controla Stop Loss individual
- `/tp [pct]` altera Take Profit
- `/trailing [ativ] [dist]` configura trailing stop
- `/drawdown [pct]` / `/drawdown reset` limite de drawdown desde pico
- `/ordersize [usd]` tamanho fixo de ordem
- `/closeall confirm` fecha todas as posições (irreversível)

---

## ⚙️ Personalizações

### Seleção de moedas (agora dinâmica)

O bot busca automaticamente na Binance Futures os pares `USDT` com status `TRADING`
e usa essa lista como universo para ranking/seleção.

Para controlar o que fica ativo, use os comandos do Telegram:
- `/coins` para ver ativas, desabilitadas e universo atual
- `/coins disable ETH SOL ADA` para remover pares da seleção
- `/coins enable ETH` para reabilitar pares

### Ajustar indicadores

No `trading_bot/core/strategy.py`, modifique o método `analyze_market()` para usar diferentes indicadores ou pesos.

### Criar novas estratégias

Crie uma nova classe que herda de `HedgeStrategy` e sobrescreva os métodos de análise.

---

## ❓ FAQ

**P: É seguro usar este bot?**
R: O bot nunca tem acesso para sacar fundos (se você configurar as API keys corretamente). Porém, ele pode perder dinheiro em trades ruins.

**P: Qual o lucro esperado?**
R: Não há garantia de lucro. O mercado de cripto é volátil e imprevisível.

**P: Posso rodar 24/7?**
R: Sim, mas recomendo usar um servidor (VPS) em vez do seu computador pessoal.

**P: O bot funciona em qualquer mercado?**
R: A estratégia de hedge funciona melhor em mercados laterais ou com volatilidade moderada. Em tendências muito fortes, uma das posições pode ter perdas significativas.

---

## 📞 Suporte

Este é um projeto educacional. Não há garantias ou suporte oficial.

**Lembre-se**: Você é responsável por suas próprias decisões de investimento.

---

## 📜 Licença

Este código é fornecido "como está", sem garantias de qualquer tipo. Use por sua conta e risco.

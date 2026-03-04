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

Preencha o `.env` com seus dados:

```bash
BINANCE_API_KEY="sua_api_key_aqui"
BINANCE_API_SECRET="sua_api_secret_aqui"
TELEGRAM_TOKEN="seu_token_aqui"
TELEGRAM_CHAT_ID="seu_chat_id_aqui"
```

**Opção B - variáveis de ambiente no shell:**

```bash
# Linux/Mac
export BINANCE_API_KEY="sua_api_key_aqui"
export BINANCE_API_SECRET="sua_api_secret_aqui"
export TELEGRAM_TOKEN="seu_token_aqui"
export TELEGRAM_CHAT_ID="seu_chat_id_aqui"

# Windows (PowerShell)
$env:BINANCE_API_KEY="sua_api_key_aqui"
$env:BINANCE_API_SECRET="sua_api_secret_aqui"
$env:TELEGRAM_TOKEN="seu_token_aqui"
$env:TELEGRAM_CHAT_ID="seu_chat_id_aqui"
```

---

## 🔧 Configuração

O fluxo recomendado é ajustar via `.env` (o bot também suporta variáveis de ambiente no shell).
Os valores abaixo são os defaults atuais em `trading_bot/core/config.py`.

### Parâmetros Essenciais

| Parâmetro | Padrão | Descrição |
|-----------|--------|-----------|
| `USE_TESTNET` | `False` | `True` para Testnet, `False` para Mainnet |
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
export TRADING_BOT_DASHBOARD_HOST=127.0.0.1
export TRADING_BOT_DASHBOARD_PORT=8080
export TRADING_BOT_DASHBOARD_REFRESH_SECONDS=5
export TRADING_BOT_DASHBOARD_AUTH_TOKEN=
export TRADING_BOT_MAINNET_CONFIRM=eu_sei_o_risco
```

Arquivos de runtime ficam em `runtime/` por ambiente:
- `bot_state.<env>.json`
- `trading_bot.<env>.lock`
- `trading_bot.<env>.log`

O bot carrega automaticamente `.env` (e `.env.local`, se existir).
Para usar outro arquivo, defina:

```bash
export TRADING_BOT_ENV_FILE=/caminho/para/seu.env
```

---

## ▶️ Executando

### Modo Testnet (Recomendado para começar)

1. Certifique-se que `USE_TESTNET = True` no `trading_bot/core/config.py`
2. Obtenha API keys da Testnet: https://testnet.binancefuture.com/
3. Execute:

```bash
python -m trading_bot.core.bot
```

### Modo Real (CUIDADO!)

1. Mude `USE_TESTNET = False` no `trading_bot/core/config.py`
2. Use suas API keys reais da Binance
3. **IMPORTANTE**: Crie API keys apenas com permissão de Trade, SEM permissão de Withdraw
4. Execute:

```bash
python -m trading_bot.core.bot
```

Em MAINNET sem terminal interativo (server/CI), defina antes:

```bash
TRADING_BOT_MAINNET_CONFIRM=eu_sei_o_risco
```

### Dashboard Web (monitoramento em tempo real)

Você pode subir uma página web read-only para acompanhar posições, P&L e saúde do bot:

```bash
python -m trading_bot.web.dashboard
```

Parâmetros úteis:

```bash
python -m trading_bot.web.dashboard --host 127.0.0.1 --port 8080 --refresh-seconds 5
```

Para proteger com token:

```bash
python -m trading_bot.web.dashboard --token "SEU_TOKEN_FORTE"
```

Depois acesse:
- sem token: `http://127.0.0.1:8080`
- com token: `http://127.0.0.1:8080/?token=SEU_TOKEN_FORTE`

### Dashboard em modo seguro (rápido + análise sob demanda)

O dashboard usa dois níveis de atualização:
- **refresh rápido** (`/api/dashboard`): dados operacionais de conta, posições e risco
- **análise sob demanda** (`/api/dashboard/analytics`): calendário, cumulativo e métricas históricas

Na interface, use o botão **Atualizar análise** quando quiser recalcular analytics pesado para o período selecionado.

Na tabela de pares:
- coluna **Distância** com barra visual por posição
- selo de severidade (`CRÍTICO`, `ALERTA`, `ATENÇÃO`, `NORMAL`, `CONFORTÁVEL`)
- coluna **Status / Stop** com badges separados

Recomendação para Oracle:
- mantenha bind em `127.0.0.1`
- exponha via Nginx/Cloudflare Tunnel com autenticação
- evite publicar porta aberta sem token

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
5. reinicia o dashboard (sessão `screen`) por padrão

Variáveis opcionais:

```bash
SCREEN_NAME=bot PROJECT_DIR=/home/ubuntu/trading_bot TRADING_BOT_ENV=prod BOT_MODULE=trading_bot.core.bot ./scripts/update_server.sh
```

Para controlar restart do dashboard no deploy:

```bash
DASHBOARD_ENABLED=0 ./scripts/update_server.sh
```

Se `USE_TESTNET=False`, garanta no `.env` do servidor:

```bash
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
├── runtime/                  # Arquivos de runtime (state/lock/log)
├── trading_bot/              # Código principal organizado
│   ├── __init__.py
│   ├── core/
│   │   ├── bot.py
│   │   ├── config.py
│   │   └── strategy.py
│   ├── infra/
│   │   └── binance_client.py
│   ├── web/
│   │   ├── __init__.py
│   │   └── dashboard.py
│   └── services/
│       ├── notifications.py
│       ├── pair_selector.py
│       └── telegram_commands.py
├── tests/
│   ├── test_bot_regressions.py
│   ├── test_dashboard_regressions.py
│   └── test_services_regressions.py
├── scripts/
│   ├── test_connection.py
│   ├── test_config.py
│   ├── test_funding.py
│   ├── rollback_server.sh
│   └── update_server.sh
├── pytest.ini
├── .github/workflows/ci.yml
├── .github/workflows/deploy-oracle.yml
├── .github/workflows/rollback-oracle.yml
├── requirements.txt
└── README.md
```

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

---

## ⚙️ Personalizações

### Adicionar mais moedas

No `trading_bot/core/config.py`, edite `TRADING_PAIRS`:

```python
TRADING_PAIRS = [
    "BTCUSDT",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "XRPUSDT",  # Adicione novas moedas
]
```

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

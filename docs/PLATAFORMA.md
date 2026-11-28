# Plataforma de trading para terceiros — arquitetura alvo e fases

> Destino: cada usuário com sua conta Bitget (futuros perpétuos USDT-M), suas posições e seu bot,
> num único painel. Hoje existe um processo, um bot e uma conta (Josias). Este documento é o plano
> para o shell atual não virar beco sem saída. Nada aqui executa ordem para terceiros ainda.

## 1. Arquitetura alvo

```
                 ┌──────────────── processo web (Flask + Socket.IO) ────────────────┐
browser ─login──▶│ auth (users) ─▶ rotas /api/*?account=N ─▶ resolve conta ─▶ coleta │
                 │   │                                            │                  │
                 │   └── PlatformStore (runtime/platform.db)      ├─ bot da conta    │
                 │        users / accounts (chaves cifradas)      │   (hoje: in-proc)│
                 └────────────────────────────────────────────────┼──────────────────┘
                                                                  │
                        fase 2/3 ─────────────────────────────────┼─ worker por conta
                                                                  │   (processo próprio,
                                                                  │    TradeStore próprio)
                                                                  └─ Bitget (chaves da conta)
```

Camadas e quem é dono de quê:

| Camada | Hoje (fase 1) | Alvo |
|---|---|---|
| Identidade | `users` (username, senha com hash scrypt/pbkdf2 do Werkzeug) | idem + papéis (admin/cliente) |
| Contas de exchange | `accounts` (user_id, exchange, label, chaves cifradas com Fernet) | idem + status (ativa/pausada), limites por conta |
| Resolução conta → bot | `app.config["BOTS"] = {account_id: bot}`; só a conta 1 tem bot (o do processo) | registro de workers por conta (processo/container por conta) |
| Coleta (`collect_live/history/snapshot`, fills, posições) | recebem o **bot da conta** resolvido pela rota (`?account=N`) | mesma assinatura; o bot vira um *handle* do worker (RPC/arquivo/DB) |
| Dados de trading | `TradeStore` (SQLite) do processo | um TradeStore por conta (`trades.<account>.db`) |
| UI | shell CryptoZone com menu, KPIs de perpétuo, páginas | + seletor de conta no topo, telas Contas/Usuários |

Decisões que valem desde já:

- **`account_id` é o eixo.** Toda rota de dados e de controle recebe `?account=N` (default: a 1ª conta
  do usuário logado). Conta de outro usuário, inexistente ou sem bot no processo → 404. Assim as
  rotas não assumem bot global; trocar o mapa `BOTS` por um registro de workers não muda o contrato.
- **Segredos nunca em claro no banco.** Chaves de exchange ficam cifradas com Fernet; a chave vem de
  `TRADING_BOT_PLATFORM_SECRET` (ou, na falta, de `TRADING_BOT_DASHBOARD_SECRET_KEY`). Trocar o
  segredo invalida as chaves gravadas (recadastrar); é deliberado.
- **Login pela tabela, .env como compatibilidade.** `users` é consultada primeiro; o .env só vale
  para um nome que não existe na tabela (e é dele que o seed cria o usuário 1).
- **Nenhum caminho de ordem por terceiros.** `/api/control/*` e `/api/positions/*/close` continuam
  agindo só sobre o bot da conta resolvida — hoje só a conta 1 tem bot.

## 2. Fase 1 (este PR e os dois seguintes)

Entregue aqui:

- `trading_bot/web/platform.py`: `PlatformStore` (SQLite `runtime/platform.db`), tabelas `users`
  e `accounts`, cifra Fernet, `seed_from_env(config)` idempotente (usuário 1 = login do dashboard;
  conta 1 = exchange ativa com as chaves do .env, cifradas).
- Login: `auth.authenticate()` (hash da tabela; .env como fallback), sessão guarda `user_id`/`username`.
- Rotas: `account_bot()` resolve `?account=N`; `/api/me` lista usuário e contas (sem chaves).
- Shell: menu lateral (Visão geral, P&L, Planilha, Sistema; "Contas" e "Usuários" como *em breve*),
  perfil com o usuário logado, tema claro/escuro, KPIs de **perpétuo** (equity, margem usada ×
  disponível, P&L não realizado por perna, realizado do dia UTC, funding acumulado) e, por posição,
  alavancagem, notional, margem, marca, liquidação e ROE.

PR 2 (Mercado BTC) e PR 3 (fills da conta) seguem o mesmo eixo: fills são **da conta** (`?account=N`),
mercado é público (sem conta).

Fora da fase 1: telas de cadastro de usuário/conta, seletor de conta no topo (só há uma),
papéis/permissões, multi-processo.

## 3. Fase 2 — contas de terceiros, só leitura (feita: PRs #316 e o seguinte)

Entregue:

1. **Usuários** (`web/users.py`, página `#users`, só admin): papéis `admin`/`user` (coluna `role`; usuário 1 é
   sempre admin), `active` (desativado não loga e a sessão aberta cai), criar usuário com senha gerada
   mostrada **uma** vez ou definida pelo admin, redefinir senha, promover/rebaixar; admin não age sobre si.
   **Meu perfil** (avatar do topo): trocar a própria senha. CSRF nos POSTs JSON pelo header `X-CSRF-Token`
   (mesmo token da sessão); rate limit do login por IP **e** por usuário.
2. **Contas** (`web/accounts.py`, página `#accounts`): cadastrar exchange + rótulo + chaves (cifradas na
   gravação; nunca devolvidas — só `key_last4`), editar rótulo, **substituir chaves** (as 3 juntas),
   **Testar conexão** (`/api/v2/mix/account/accounts` + posições com as chaves da conta; resultado gravado em
   `last_check_*`), ativar/desativar (a conta do bot deste processo não desativa). Admin vê todas as contas com
   o dono e pode criar conta para outro usuário; `user` só as próprias.
3. **Seletor de conta no topo** (só com >1 conta ativa; lembrado em `localStorage`): o `main.js` anexa
   `?account=N` a toda rota de dados (`/api/me|users|accounts|market|klines|healthz` ficam fora).
4. **Conta sem bot = só leitura**: `account_bot()` devolve um `ReadOnlyAccount` (`web/accounts.py`) com o
   mesmo shape que `collect_*` lê do bot — `exchange` = `BitgetConnection(credentials=...)` da conta,
   `known_positions` = posições abertas na corretora (cache 5 s), `trade_store=None`. Assim `/api/live`,
   `/api/summary`, `/api/fills` funcionam (saldo, margem, não realizado, realizado do dia, funding, posições,
   fills); `/api/history`, `/api/pnl_report` respondem vazios (sem TradeStore). Pausar/Retomar/Fechar → **409**.
   Conta sem chaves → 404. A etiqueta "só leitura" aparece no topo e os botões de controle somem.
5. Socket.IO só conecta para quem é dono da conta do bot (os eventos são broadcast do bot deste processo);
   os demais usuários ficam no polling HTTP, que resolve a conta deles.

## 3b. Fase 2b — ambiente por conta, cache por conta (PR C)

Entregue:

1. **Ambiente por conta**: coluna `accounts.environment` (`demo`|`prod`, ALTER idempotente no boot). A conta do
   bot (conta 1) segue o processo: o seed grava `BITGET_DEMO` do `.env` nela (na criação e na migração da coluna)
   e `POST /api/accounts/<id>/environment` responde 400 para ela. As outras escolhem no cadastro (campo
   Ambiente) ou em **Editar**; trocar o ambiente zera a última checagem e descarta o handle só-leitura.
   `BitgetConnection(credentials=..., demo=...)`: `demo` por conta (header `paptrading`); `None` = processo.
   Badge DEMO (amarelo) / PROD (vermelho) na tabela de Contas e ao lado do seletor do topo.
2. **Cache dos KPIs por conta**: `data._binance_summary` guarda uma entrada por objeto exchange (bot e cada
   handle só-leitura têm o seu) com TTL de 15 s; **rate limit** simples: mesmo com falha da corretora, no mínimo
   5 s entre rajadas de chamadas privadas da mesma conta (antes, a falha era re-tentada a cada poll de 5 s).
   Entradas vencidas são descartadas a cada chamada (handle trocado não vaza).

## 3c. Fase 2b — histórico/P&L de conta sem bot (PR D)

Entregue (sem TradeStore por conta):

1. **`/api/pnl_report?account=N`** em conta sem bot usa o **extrato da corretora** como fonte de trades
   (`web/pnl_report.py`): cada lançamento `REALIZED_PNL` vira um "trade" (fechamento na corretora, sem
   qty/preços/lado: o bill não traz), o por-símbolo soma realizado + taxas + funding por par, hoje/7d/30d e o
   calendário vêm do extrato como já vinham. O piso `PNL_SINCE_UTC` (reset de banca do bot) **não** se aplica a
   essas contas; sem snapshots de equity, a % usa a equity ao vivo.
2. **Card "P&L da conta"** na visão geral da conta só-leitura (`#ro-pnl-card`: hoje/7d/30d + por símbolo), com
   link para a análise completa em P&L; a página P&L inteira (abas da conta, trading, detalhes, símbolo, income)
   funciona para a conta.
3. `/api/fills?account=N` já lia os fills com as chaves da conta; coberto por teste com respostas fake.

Fica para depois:

- Outras exchanges no cadastro (só Bitget hoje).
- Round-trips (qty/preços/lado) de conta sem bot a partir dos fills, se a lista de "Detalhes" precisar deles.

## 4. Fase 3 — um bot por conta (multi-processo)

**Feito (PR 1): worker por conta.** `python -m trading_bot.core.worker N` (ou `TRADING_BOT_ACCOUNT_ID=N`)
lê a conta N no `platform.db`, decifra as chaves em memória, popula `os.environ` e só então importa
`trading_bot.core.bot` (o `config` é montado do env no import; `.env` não sobrescreve o que já está no
ambiente). Força: `BITGET_API_KEY/SECRET/PASSPHRASE` da conta, `BITGET_DEMO` (prod → false),
`TRADING_BOT_EXCHANGE=bitget`, `TRADING_BOT_RUNTIME_DIR=runtime/accounts/N` (lock, state, trades.db e log
isolados), Telegram/relatório diário/dashboard/métricas desligados, `TRADING_BOT_START_PAUSED=true` e
`TRADING_BOT_CONTROL_FILE_ENABLED=true` (sem efeito até o PR 2). O resto (MARTINGALE_*, STRATEGY,
TRADING_BOT_ENVIRONMENT...) herda do `.env`, igual à conta 1. Recusa conta 1, inexistente, inativa, não-bitget
ou sem chaves. Conferir antes de subir: `TRADING_BOT_ACCOUNT_DRY=1 .venv/bin/python -m trading_bot.core.worker N`
imprime o env com os segredos mascarados e sai sem importar o bot. Não subir na VM antes do PR 3
(o `update_server.sh` atual não conhece o worker).

**Feito (PR 2): canal de controle `control.json`.** O dashboard escreve `runtime/accounts/N/control.json`
(`trading_bot/core/control_file.py: write_control`); o worker lê a cada ciclo do monitor
(`TradingBot._apply_control_file`, só com `TRADING_BOT_CONTROL_FILE_ENABLED=true` — a conta 1 nunca lê).
Formato: `{"paused": bool, "new_cycles": bool, "commands": [{"id", "cmd": "close_all"|"close_position",
"symbol", "side", "reason"}]}`. `paused` e os comandos são consumidos (somem do arquivo depois de
aplicados; assim o stop de portfólio/kill switch pausam sem o arquivo religar); cada comando ganha um ack
`{"id", "ok", "error", "at"}` em `control_ack.json`. `new_cycles` é estado e fica: `false` = conta
pré-paga sem crédito — o martingale não abre ciclo novo (base de uma perna zerada), mas ciclos abertos
continuam adicionando níveis e fecham no TP normalmente; `true` libera. Latência = ciclo do monitor.
Retomar uma conta à mão: acrescentar `"paused": false` em `runtime/accounts/N/control.json` (editar, não
sobrescrever: `new_cycles` deve ser preservado).

**Feito (PR 3): supervisor no `update_server.sh`.** Além do screen `bot` da conta 1 (inalterado), o
deploy (e o restart manual `SKIP_GIT_PULL=1 SKIP_TESTS=1 bash scripts/update_server.sh`) sobe um screen
`bot-accN` para cada `runtime/accounts/N/worker.enabled` (marcador criado à mão; N ≠ 1), rodando
`scripts/run_bot_loop.sh` (mesmo wrapper de auto-restart com backoff) com `BOT_MODULE=trading_bot.core.worker
BOT_ARGS=N RESTART_LOG=runtime/accounts/N/restart.log`; stdout/stderr do worker vão para
`runtime/accounts/N/worker.out.log`, o log do bot continua em `runtime/accounts/N/trading_bot.*.log`. A cada
rodada, os workers existentes são parados como a conta 1 (SIGTERM → SIGKILL por padrão de processo
`trading_bot\.core\.worker N`, nunca `pkill -f run_bot_loop`) e relançados só se o marcador ainda existir;
sem marcador = parado e não relançado. Sem `runtime/accounts/` nada muda. Caminho mínimo da 2ª conta na VM:

1. Cadastrar a conta N no painel (prod, Testar conexão OK).
2. `TRADING_BOT_ACCOUNT_DRY=1 .venv/bin/python -m trading_bot.core.worker N` — conferir o env mascarado
   (isso cria `runtime/accounts/N/`).
3. `touch runtime/accounts/N/worker.enabled`
4. `cd /home/ubuntu/trading_bot && SKIP_GIT_PULL=1 SKIP_TESTS=1 bash scripts/update_server.sh`
   (reinicia a conta 1 também, como qualquer deploy).
5. `screen -ls` deve listar `bot` e `bot-accN`; `tail runtime/accounts/N/worker.out.log
   runtime/accounts/N/trading_bot.*.log` — lock, "Bitget em PRODUÇÃO", pausado.
6. Retomar: acrescentar `"paused": false` em `runtime/accounts/N/control.json` (PR 2).

**Feito (PR 4): heartbeat por conta.** O mesmo `scripts/heartbeat.py` do cron (a cada 2 min, entrada do
cron inalterada) vigia, além da conta 1, cada `runtime/accounts/N/worker.enabled`: processo
`-m trading_bot.core.worker N` vivo (pgrep ancorado — `worker 2` não casa `worker 22`) e mtime do
`trading_bot.*.log` mais novo do diretório dentro de `HEARTBEAT_STALE_MINUTES` (sem log = caído). Estado e
re-aviso (30 min) independentes por conta em `heartbeat_state.json["accounts"][N]`; o aviso vai para o mesmo
Telegram do operador com o rótulo ` conta N` (as mensagens da conta 1 não mudam). Sem marcador = não vigia e
esquece o estado; sem `runtime/accounts/` nada muda.
   Parar de vez: `rm runtime/accounts/N/worker.enabled` e rerodar o passo 4.

**Feito (PR 5): `WorkerAccount` no dashboard.** `account_bot()` (`trading_bot/web/app.py`) devolve, para uma
conta com `runtime/accounts/N/` (marcador `worker.enabled` ou um `bot_state.*.json` já gravado), um
`WorkerAccount` (`trading_bot/web/accounts.py`, subclasse do `ReadOnlyAccount`) em vez do handle só-leitura.
Estado (`paused`, posições, capital inicial, pico, contadores, `start_time`) vem do `bot_state.*.json` mais novo
do diretório (releitura a cada 2 s); trades/P&L/calendário/ranking do `trades.*.db` do worker (mesmo
`TradeStore` da conta 1, aberto no processo do dashboard — SQLite em WAL aceita o leitor); saldo, mark price e
extrato continuam vindo da Bitget com as chaves da conta, como na só-leitura. `running` = log
`trading_bot.*.log` do worker escrito há < 5 min. Controles: Pausar/Retomar escrevem `paused` no `control.json`
(merge; `new_cycles` preservado) e voltam na hora — o worker aplica no ciclo seguinte e agora salva o state
imediatamente (o payload do state ganhou a chave `paused`), então o painel reflete a pausa em segundos; Fechar
posição / Fechar tudo acrescentam um comando com id e esperam o ack em `control_ack.json` por até 15 s
(erro do worker → 500 com a mensagem; sem resposta → 500 "não respondeu", o comando fica na fila). `has_bot`
(`/api/me`, `/api/accounts`, `data-has-bot` do painel) passa a valer para contas com worker — os botões aparecem;
trocar ambiente/desativar uma conta com worker é recusado. Conta sem `runtime/accounts/N/` segue só-leitura
(409 nos controles); conta 1 e `?account` ausente continuam no bot do processo. Fora: painel do martingale
(escada/pernas) para a conta com worker — precisa do runner, fica `null` (a planilha renderiza sem score).
**Primeiro PR da fase que mexe no processo do dashboard: o deploy reinicia a conta 1 (~1 min de 502).**

**Feito (PR 6): créditos pré-pagos ("gás").** Cada conta tem `accounts.credit_pct` (default 20; conta 1 = 0,
editável pelo admin) e um extrato `credit_ledger(id, account_id, ts, kind 'topup'|'consume', amount > 0, ref, note)`
no `platform.db`; saldo = Σ topup − Σ consume. O consumo é DERIVADO do `trades.*.db` da conta
(`trading_bot/web/credits.py`): cada ciclo do martingale é uma linha de `trades` (a base abre, o TP fecha a mesma
linha), então `ref` = `"{trades.id}@{opened_at}"`; cobra `credit_pct`% do `pnl_net` de cada ciclo fechado POSITIVO há mais de 1 h
(carência pro `pnl_reconciler` acertar fees/funding); ciclo negativo não consome nem compensa; idempotente por
`(account_id, ref)` (índice único), por isso pode rodar quantas vezes quiser. O arquivo é lido só-leitura
(`mode=ro`, sem abrir `TradeStore`). Roda em dois lugares, ambos fail-open: no `GET /api/live?account=N` de uma
conta com worker (no máximo 1× por minuto por conta; a resposta ganha `credits: {balance, credit_pct, exhausted,
low, ledger}`) e numa thread daemon do processo do dashboard a cada 5 min para todas as `runtime/accounts/N/`
(a trava vale com o painel fechado). **Trava:** saldo ≤ 0 → `new_cycles: false` no `control.json` da conta (PR 2:
o martingale não abre ciclo novo, os abertos terminam no TP); saldo > 0 → `true`; só escreve quando o valor
desejado difere do que está no arquivo. O saldo pode ficar negativo uma vez (ciclo fechando com saldo pequeno) —
aceito, a recarga seguinte cobre. Conta 1, conta com `credit_pct` 0 e conta sem `runtime/accounts/N/` nunca
consomem nem são travadas. Painel: chip no topo da conta com worker — verde `créditos $X · 20%`, amarelo quando
saldo < max($5, 2× o último consumo), vermelho `sem crédito — ciclos novos não abrem`; conta 1 não mostra nada.
Admin: botão **Créditos** na página Contas (saldo, últimos lançamentos, recarga, percentual).

Operação: créditos. A recarga é manual, depois do PIX/USDT fora da plataforma (a API da Bitget não transfere
entre contas de terceiros). Pelo painel: Contas › Créditos › valor + observação › Lançar. Por API, com o cookie
de sessão de um admin e o CSRF da página (`X-CSRF-Token`, o mesmo das outras rotas `/api/accounts`):

```bash
# saldo, percentual e extrato (dono da conta ou admin)
curl -b cookies.txt https://trader.adityve.com.br/api/accounts/2/credits
# recarga de 50 USDT (admin); credit_pct é opcional, amount 0 só troca o percentual
curl -b cookies.txt -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
     -d '{"amount": 50, "note": "PIX 28/11", "credit_pct": 20}' https://trader.adityve.com.br/api/accounts/2/credits
```

A recarga liquida na hora: se o saldo voltar a > 0 o `control.json` recebe `new_cycles: true` na mesma resposta e o
worker libera a base no ciclo seguinte do monitor. Para zerar a cobrança de uma conta (ex. a sua), `credit_pct: 0`.
`reset_bot_state.py` apaga o `trades.db` da conta, mas o extrato vive no `platform.db` e sobrevive; um `trades.db`
novo recomeça os ids em 1, por isso o `ref` leva o `opened_at` junto — ciclos novos continuam sendo cobrados.

Ainda exige (fora do código): **faturamento/termos** — responsabilidade por ordens em conta de terceiros é
jurídica antes de técnica; definir antes de abrir a plataforma a terceiros. Opcional (PR 7): Prometheus/Telegram
por conta, perfil por conta, Socket.IO para workers, systemd no lugar do screen.

## 5. Variáveis novas

| Env | Para quê | Default |
|---|---|---|
| `TRADING_BOT_PLATFORM_DB_PATH` | caminho do `platform.db` | `runtime/platform.db` |
| `TRADING_BOT_PLATFORM_SECRET` | segredo da cifra das chaves | cai em `TRADING_BOT_DASHBOARD_SECRET_KEY` |

Sem segredo nenhum, o seed cria a conta 1 **sem** as chaves e avisa no log (o bot continua usando o
.env normalmente; só a tabela fica sem elas).

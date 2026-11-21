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

## 3. Fase 2 — contas de terceiros, só leitura

Exige:

1. UI de **Contas**: cadastrar exchange + chaves (cifradas na gravação; nunca devolvidas ao browser),
   testar conexão (`/api/v2/mix/account/accounts` com as chaves da conta), rótulo, ativar/desativar.
2. UI de **Usuários** (admin): criar usuário, redefinir senha, vincular contas.
3. **Coleta por conta sem bot**: `collect_positions/fills/summary` passam a aceitar um *cliente de
   exchange da conta* (`BitgetConnection` construído com `account_credentials(id)`) quando não há bot
   no processo — leitura de posições, saldo, fills e P&L da Bitget de qualquer conta cadastrada.
4. Seletor de conta no topo; `?account=N` já existe nas rotas.
5. Rate limit por conta nas chamadas privadas (Bitget: 10 req/s por chave) e cache curto por conta.

Sem execução de ordens: botões Pausar/Retomar/Fechar aparecem só para contas com bot.

## 4. Fase 3 — um bot por conta (multi-processo)

Exige:

1. **Worker por conta**: processo próprio (`python -m trading_bot --account N`) com `.env` derivado da
   conta (chaves decifradas em memória, nunca em arquivo), `TradeStore` e `bot_state` por conta
   (`runtime/<account>/`), lock por conta.
2. **Supervisor**: sobe/derruba/reinicia workers (systemd template unit ou um supervisor Python),
   heartbeat por conta (hoje `scripts/heartbeat.py` é de um processo só).
3. **Canal web ↔ worker**: o dashboard deixa de importar o bot; lê snapshots/eventos do worker
   (arquivo JSON + SQLite por conta, ou um Redis pequeno). `BOTS[account_id]` vira um handle com
   `collect_*` e `pause/resume/close` via esse canal.
4. **Controle de risco por conta**: limites da planilha e stop de portfólio já são por bot; falta
   impedir que uma conta derrube o processo das outras (isolamento é o motivo do multi-processo).
5. **Faturamento/termos** (fora do código): responsabilidade por ordens em conta de terceiros é
   jurídica antes de técnica; definir antes de ligar qualquer execução.

## 5. Variáveis novas

| Env | Para quê | Default |
|---|---|---|
| `TRADING_BOT_PLATFORM_DB_PATH` | caminho do `platform.db` | `runtime/platform.db` |
| `TRADING_BOT_PLATFORM_SECRET` | segredo da cifra das chaves | cai em `TRADING_BOT_DASHBOARD_SECRET_KEY` |

Sem segredo nenhum, o seed cria a conta 1 **sem** as chaves e avisa no log (o bot continua usando o
.env normalmente; só a tabela fica sem elas).

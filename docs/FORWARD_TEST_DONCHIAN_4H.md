# Pré-registro: forward test Donchian 4h (testnet, sinais da mainnet)

Data do pré-registro: 2026-10-24. Escrito e commitado antes do código da estratégia.
Pesquisa de origem: `scripts/backtest_v2/TREND4H_PREREG.md` e `TREND4H_RELATORIO.md`
(branch `research/trend-4h`, PR #215). O backtest não passou no critério
pré-registrado. O relatório recomendou este forward test como única evidência
nova e não contaminada.

## Configuração fixa

Uma única configuração. Nenhum parâmetro muda durante o teste.

| item | valor |
|---|---|
| estratégia | `TRADING_BOT_STRATEGY=donchian_4h` |
| timeframe | barras 4h fechadas, dados da mainnet (`TRADING_BOT_SIGNAL_DATA_SOURCE=mainnet`) |
| entrada | close da barra 4h acima da máxima das 40 barras anteriores (LONG) ou abaixo da mínima das 40 anteriores (SHORT), ordem a mercado logo após o fechamento |
| stop inicial | 2 × ATR(20, Wilder) / close da barra do sinal, aplicado ao preço do fill. STOP_MARKET na exchange (MARK_PRICE, closePosition). Sem take profit |
| saída | close da barra 4h abaixo da mínima das 20 anteriores (LONG) ou acima da máxima das 20 anteriores (SHORT), ordem a mercado logo após o fechamento. Também sai com dados parados (deslistagem) |
| risco por trade | 0,25% da equity até o stop |
| tetos | 1× equity de notional por posição, 3× equity de notional bruto, no máximo 10 posições |
| equity | equity simulada do testnet (`trading_bot/core/equity.py`, PR #211) |
| minNotional | se o mínimo da exchange forçar risco acima de 2× o alvo, o trade é pulado e logado |
| universo | top 30 perpétuos USDT por quote volume dos últimos 30 dias da mainnet, ≥ 60 dias de histórico, refeito toda segunda 00:00 UTC, ∩ símbolos negociáveis no testnet, menos `DISABLED_PAIRS` |
| drawdown | parada dura em 20% do pico da equity simulada (pausa o bot) |
| fora | trailing, regime swap, viés HTF, gate de IA, análise 3m/5m, scoring de pares, pausa por sequência de dias negativos, piso de win rate, meta de lucro diário, stop loss global |
| mantido | limite de perda diária (sanidade) |

Os valores ficam em `trading_bot/core/config.py` (`DONCHIAN_*`). Só o limite de
drawdown lê variável de ambiente, e ele não muda durante o teste.

## Início

O teste começa quando o usuário liga a estratégia na VM (`.env` + restart). A data
e a hora UTC desse restart são registradas aqui, num commit, no mesmo dia:

- Início: 2026-10-24 23:43:07 UTC (ver Registro de início)
- Equity simulada no início: $299,32 (capital simulado $300,00; ajustado em
  2026-10-25 01:42 UTC antes do 1º trade; ver Registro de início)

Posições abertas da estratégia pullback no momento da troca não contam. Elas
terminam pelas regras antigas e ficam fora das métricas.

## Medição

- Trades do teste: linhas do SQLite (`trades`) com `strategy_type = 'donchian_4h'`,
  fechadas depois do início.
- P&L: `pnl_net` depois do reconciliador do PR #213 (fills reais da Binance:
  `pnl_net = pnl_gross − fees + funding`).
- Expectativa líquida por trade: média do `pnl_net` em USD dos trades fechados.
- Drawdown: do pico da equity simulada desde o início do teste, como o bot mede.

## Regra de sucesso

O teste termina no que vier **por último** entre 6 meses corridos desde o início e
150 trades fechados. Os dois precisam ter acontecido. Nesse ponto, passa se as duas
condições valem:

1. Expectativa líquida por trade > 0 (P&L reconciliado com a exchange).
2. Drawdown máximo < 20% durante todo o teste.

## Regra de falha

- Drawdown de 20% do pico a qualquer momento encerra o teste. O bot pausa sozinho.
  Não há retomada com a mesma configuração.
- Expectativa líquida por trade ≤ 0 no fim do teste.

Se falhar, a conclusão é que não há edge capturável com este universo e estes
custos. O próximo trabalho é reduzir custo (maker, menos turnover) antes de testar
outra ideia de sinal.

## Relatório semanal

Toda semana, a partir do SQLite e do log:

- trades fechados na semana e no acumulado, LONG e SHORT;
- P&L líquido da semana e acumulado, fees e funding;
- expectativa por trade acumulada, win rate, ganho médio e perda média;
- drawdown atual e máximo desde o início;
- posições abertas com stop;
- trades pulados e o motivo (minNotional, teto de posições, teto bruto, par
  ausente no testnet);
- universo da semana e quantos do top 30 faltam no testnet;
- tempo decorrido e trades restantes até a regra de término.

## Sem mudança de parâmetro

Nenhum parâmetro muda durante o teste: canais, ATR, k, risco, tetos, universo e
limite de drawdown. Bug que invalide o teste pode ser corrigido. A correção fica
registrada neste arquivo com a data, e os trades afetados são listados.

Fechamento manual pelo botão "Fechar" do dashboard sai pelo mesmo caminho do exit
do Donchian, fica com `close_reason` "Manual (dashboard)" no SQLite e é contado
separadamente no relatório (`manual_closes`).

## Ressalvas

- Fills do testnet são aproximados. Livro, slippage e preço do testnet diferem da
  mainnet, e o stop é ancorado no preço do fill do testnet.
- O funding do testnet não é o da mainnet.
- Com equity de ~$173, 0,25% é ~$0,43 de risco. Parte dos sinais cai abaixo do
  minNotional e é pulada. Isso muda a amostra em relação ao backtest.
- A pesquisa teve ~2485 trades em 4,5 anos (~10 por semana com 0,5% de risco e
  equity grande). Aqui a frequência esperada é de alguns por semana, então 150
  trades podem levar mais de 6 meses.

## Registro de início

Registro factual do restart que ligou a estratégia. Nenhuma regra acima mudou.

- Início: **2026-10-24 23:43:07 UTC**, primeira linha do log com o banner
  `🧭 Estratégia: Donchian 4h (n=40,m=20,k=2,risco 0,25%) — forward test`
  (o log da VM grava em BRT: `2026-10-24 20:43:07,845`).
- Deploy: `16a2734` (merge do PR #216), conferido em `runtime/deploy_info.json`.
- `.env` da VM: `TRADING_BOT_STRATEGY=donchian_4h`, `TRADING_BOT_SIGNAL_DATA_SOURCE=mainnet`,
  `TRADING_BOT_START_PAUSED=false`, `TRADING_BOT_ENVIRONMENT=testnet`.
- Equity simulada no início: **$172,81** (`last_balance` do `/api/summary`: cap $173,49
  + realizado acumulado −$0,68; não-realizado $0,00).
- Posições pullback abertas na troca: nenhuma.
- Bot não pausado no início.
- Universo da semana 2026-10-19: 27 pares (top 30 da mainnet; fora do testnet:
  PUMPUSDT, BTWUSDT; desabilitado: BNBUSDT): BTCUSDT, ETHUSDT, SOLUSDT, ZECUSDT,
  XRPUSDT, NEARUSDT, HYPEUSDT, DOGEUSDT, SUIUSDT, QNTUSDT, UNIUSDT, ENAUSDT, WLDUSDT,
  1000PEPEUSDT, ADAUSDT, AVAXUSDT, TAOUSDT, LINKUSDT, STRKUSDT, ONDOUSDT, BCHUSDT,
  FILUSDT, AAVEUSDT, AKEUSDT, LTCUSDT, SANDUSDT, ARBUSDT. A lista foi recalculada com
  as mesmas funções do bot (o log só traz a contagem) e bate com ela.

### Ajuste de capital antes do 1º trade (2026-10-25 01:42 UTC)

Registro factual. Nenhuma regra acima mudou.

- Quando: **2026-10-25 01:42 UTC** (2026-10-24 22:42 BRT), com **0 trades** da
  estratégia Donchian até então.
- O que: capital simulado de **$173,49 → $300,00**
  (`TRADING_BOT_SIMULATED_BALANCE_USD=300` no `.env` da VM, bot reiniciado).
- Equity simulada logo após o restart: **$299,32** (300,00 + histórico realizado
  pré-teste de −$0,68 da estratégia pullback).
- Pico de drawdown reconstruído pelo bot em **$300,00** (o pico conta a partir do
  1º trade Donchian; antes dele, vale o capital simulado). O stop de 20% passa a
  corresponder a equity ≈ $240.
- Motivo: a $173, 7–11% dos sinais eram pulados por minNotional e 17–27% eram
  elevados acima do risco-alvo; $300 reduz ambos. Risco por trade segue 0,25% da
  equity (≈ $0,75).
- Nada mais mudou. As regras pré-registradas seguem iguais.

### Alteração: universo top 30 → todos os pares (2026-10-29 10:28 UTC)

**Desvio do pré-registro**, por decisão do usuário. Nenhuma outra regra mudou.

- O que: o universo deixa de ser o top 30 por volume 30d e passa a ser **todos** os
  perpétuos USDT da mainnet com ≥ 60 dias de histórico ∩ negociáveis no testnet
  (`TRADING_BOT_DONCHIAN_UNIVERSE_TOP_N=0` no `.env` da VM; `0` = sem teto).
  Rebalance semanal, filtro de 60 dias e DISABLED_PAIRS seguem iguais.
- Quando vale: a partir do restart de **2026-10-29 10:28:08 UTC** (deploy
  `1dd905b4355aa05df71403010b8aa5a476cb18ff`, PR #224, via `update_server.sh`).
  Primeiro universo com a variável: semana 2026-10-26 = **448 pares** (refresh em
  ~19 s); 7 posições SHORT reconciliadas sem divergência.
- Efeito na medição: o teste deixa de ser o pré-registrado. O relatório semanal deve
  separar **trades antes** (universo top 30) e **trades depois** (universo completo)
  e não somar os dois na regra de sucesso/falha. Esperado: mais sinais por barra, mais
  pulos por minNotional em pares de baixo preço/liquidez com risco ≈ $0,75.

### Reversão: universo de volta ao top 30 (2026-10-30 13:40 UTC)

Decisão do usuário após 10 trades fechados (todos stop, −$7,85), 6 deles em
pares que só entraram com o universo completo. `TRADING_BOT_DONCHIAN_UNIVERSE_TOP_N=30`
no `.env` da VM, restart 13:39:49 UTC pelo `update_server.sh` (deploy d3d3995),
universo 26 pares, 10 posições reconciliadas. Posições abertas em pares fora do
top 30 (ARC, BAT, MOG, CAT, HUMA, BEAMX) continuam geridas até a saída pelo
canal ou stop; só não há entradas novas fora do top 30. Trades abertos entre
2026-10-29 10:28 e 2026-10-30 13:40 UTC ficam fora da regra de sucesso.

### Reinício: variante com trailing R (2026-10-31)

**Desvio do pré-registro**, por decisão do usuário. O forward test **reinicia** na
data/hora do deploy desta regra; o que foi medido antes vira histórico.

- Regra de saída adicional, avaliada a **cada tick do monitor** (não só no fechamento
  da barra 4h), com R = P&L não realizado ÷ risco inicial em $ (|entrada − stop| × qty,
  o mesmo R do dashboard):
  - quando o R atinge **+0,60R**, arma um piso em 0,60R;
  - a cada novo máximo o piso sobe em degraus de **0,05R** (piso = maior múltiplo de
    0,05R ≤ pico de R; nunca desce);
  - se o R atual cai **abaixo do piso**, fecha a mercado com `close_reason`
    `Trailing R (<piso>R)`.
  - O stop original de 2×ATR continua na exchange e vale enquanto o trailing não armou.
  - Env: `TRADING_BOT_DONCHIAN_TRAIL_R_ENABLED` (default `true`),
    `TRADING_BOT_DONCHIAN_TRAIL_R_ARM` (`0.60`), `TRADING_BOT_DONCHIAN_TRAIL_R_STEP` (`0.05`).
    O piso armado é persistido no state (`trail_floors`) e sobrevive a restart.
- Reinício da contagem: `TRADING_BOT_FORWARD_TEST_RESTART_AT=<ISO 8601 UTC do deploy>` no
  `.env` da VM (em código o default é `None` = sem reinício). Os trades fechados antes
  dessa data — **16 fechados**, 6 deles manuais via dashboard em 2026-10-30 21:25 UTC —
  ficam como histórico e **não contam** no critério de 150 trades nem na regra de
  sucesso/falha; o painel mostra quantos ficaram de fora. A equity-base e o prazo de
  6 meses contam a partir do reinício.
- Aviso honesto: as variantes de saída cedo falharam no backtest de 24/10 (PR #214) e,
  no backtest Donchian, os poucos trades de +3R a +5R pagam os ~70% de stops. Este
  trailing corta exatamente esses. Não foi rodado backtest desta variante antes de
  ligar; o forward test é a medição.
- Quando vale / deploy: 2026-10-30 23:30 UTC, deploy `f922e3c` (PR #233); `TRADING_BOT_FORWARD_TEST_RESTART_AT=2026-10-30T23:28:00Z` na `.env` da VM. Posições abertas no reinício: 1000000MOG, 1000CAT (SHORT), BEAMX, HUMA (LONG).

### Alteração: universo completa 30 após filtro + núcleo BTC/ETH/BNB (2026-10-31)

Decisão do usuário, em meio ao forward test reiniciado acima. Nenhuma outra regra mudou.

- Antes: top 30 por volume e **depois** o corte por testnet/`DISABLED_PAIRS` — a semana
  2026-10-26 ficou com 26 pares (PUMP, BTW, IRYS fora do testnet; BNB desabilitado).
- Agora: filtra primeiro e corta depois, então o universo **completa 30**. BTC, ETH e
  BNB são **núcleo fixo** (`TRADING_BOT_DONCHIAN_UNIVERSE_CORE`, default
  `BTCUSDT,ETHUSDT,BNBUSDT`): entram primeiro, ignorando ranking e `DISABLED_PAIRS`,
  desde que negociáveis no testnet; as 27 vagas restantes vêm por volume. Total segue 30.
- BNBUSDT sai do `DISABLED_PAIRS` padrão (estava off desde 2026-06-18 por stop-outs do
  pullback na mainnet — não se aplica ao Donchian).
- Quando vale / deploy: 2026-10-31 09:45 UTC, deploy `a998287` (PR #235). Primeiro refresh já deu 30 pares (núcleo BTC/ETH/BNB + ARB, GTC, AGT entraram no lugar dos 4 cortados). Posições abertas no deploy: 1000CAT (SHORT), HUMA (LONG).

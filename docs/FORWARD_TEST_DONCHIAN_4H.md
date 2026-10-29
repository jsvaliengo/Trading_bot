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

### Alteração: universo top 30 → todos os pares (2026-10-29 ~10:00 UTC)

**Desvio do pré-registro**, por decisão do usuário. Nenhuma outra regra mudou.

- O que: o universo deixa de ser o top 30 por volume 30d e passa a ser **todos** os
  perpétuos USDT da mainnet com ≥ 60 dias de histórico ∩ negociáveis no testnet
  (`TRADING_BOT_DONCHIAN_UNIVERSE_TOP_N=0` no `.env` da VM; `0` = sem teto).
  Rebalance semanal, filtro de 60 dias e DISABLED_PAIRS seguem iguais.
- Quando vale: a partir do restart com essa variável (registrar aqui o horário real
  do restart e o deploy quando acontecer).
- Efeito na medição: o teste deixa de ser o pré-registrado. O relatório semanal deve
  separar **trades antes** (universo top 30) e **trades depois** (universo completo)
  e não somar os dois na regra de sucesso/falha. Esperado: mais sinais por barra, mais
  pulos por minNotional em pares de baixo preço/liquidez com risco ≈ $0,75.

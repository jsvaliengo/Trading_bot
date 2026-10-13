# backtest_v2: harness reprodutível e teste do "timeframe maior"

Data do relatório: 2026-10-13. Nada neste diretório altera o código de produção (`trading_bot/`) nem a VM.

## Veredito

**Nenhuma das 5 estratégias mostrou edge out-of-sample.** Todas têm expectativa líquida negativa por trade. Em 4 das 5, o IC95% inteiro fica abaixo de zero. A exceção é o 1h/4h/1d, cujo IC cruza zero: não há evidência de edge, só falta de significância para afirmar perda. Subir o timeframe da entrada atual (pullback trend_strong) **não resolve**. Nos três TFs a expectativa **bruta, antes de qualquer custo**, já é negativa (−0,07% a −0,09% por trade). Ou seja, o sinal não tem vantagem nem antes das fees. O trend diário EMA50/200 também perdeu nesse período, embora tenha perdido menos que o buy & hold do universo.

## Resultados OOS (walk-forward 6 meses treino / 3 meses teste, 10 folds, OOS = 2024-04-01 → 2026-10-01)

| estratégia | pares | trades OOS | win% | ganho méd | perda méd | exp/trade líquida (IC95%) | exp bruta | retorno | DD máx | edge? |
|---|---|---|---|---|---|---|---|---|---|---|
| pullback ao vivo 3m/5m/1h (baseline) | 35 | 11.420 | 28,8% | +3,61% | −1,73% | **−0,194%** (−0,238; −0,149) | −0,090% | −100% | 100% | não |
| pullback ao vivo 15m/1h/4h | 35 | 4.797 | 35,5% | +4,47% | −2,74% | **−0,177%** (−0,276; −0,078) | −0,068% | −99,1% | 99,3% | não |
| pullback ao vivo 1h/4h/1d | 35 | 1.468 | 31,9% | +8,41% | −4,24% | **−0,206%** (−0,513; +0,105) | −0,090% | −71,8% | 81,2% | não |
| trend diário EMA50/200 long-only | 35 | 107 | 15,9% | +48,0% | −31,0% | **−18,4%** (−25,3; −10,4) | −16,6% | −45,9% | 55,2% | não |
| trend diário EMA50/200 long/short | 35 | 204 | 28,9% | +42,9% | −29,0% | **−8,2%** (−13,6; −2,7) | −7,7% | −47,7% | 56,2% | não |

Buy & hold equal-weight dos 35 pares no mesmo período OOS: **−51,2%**.

- **exp/trade** é o retorno líquido médio sobre o notional do trade, depois de fee, slippage e funding. Ele não depende do sizing. O IC é bootstrap com 10.000 reamostras e seed fixa.
- Critério de "edge": IC95% inferior > 0 **e** ≥ 200 trades OOS.
- O retorno e o DD são de uma carteira de $1.000 com 1% de risco por trade (pullback), no máximo 6 posições e notional ≤ 1x a banca por posição. Para o trend, cada posição recebe 1/35 da banca, sem alavancagem. Com expectativa negativa e centenas de trades, a carteira do pullback vai a zero, e por isso os valores em $ de fee e funding no `summary.json` encolhem junto com a banca. Use os percentuais.

Detalhes que importam:
- **Pullback por lado:** o LONG é sempre pior que o SHORT, nos três TFs. No 3m o LONG dá −0,26%/trade e o SHORT −0,13%. No 1h o LONG dá −0,50% e o SHORT −0,04%. O SHORT fica perto de zero só em TF maior, e nunca positivo de forma significativa.
- **Pullback por ano:** é negativo em 2024, 2025 e 2026 no 3m e no 15m, então não é um regime ruim isolado. No 1h, 2024 e 2026 ficaram ≈ 0 e 2025 em −0,48%.
- **Por símbolo:** no 3m só 2 dos 35 pares ficaram positivos (SOL +0,07%, ARB +0,03%), ambos com ruído. ETH ficou negativo em todos os TFs. SOL ficou positivo no 1h (+0,83%, só 47 trades), o que não basta para ser evidência.
- **Grid escolhido no treino:** no 3m e no 1h o treino escolheu quase sempre a célula de **maior SL do grid** (1,5% e 4,0%). Mesmo assim, o retorno de treino da melhor célula foi negativo em 10/10 folds no 3m e em 8/10 no 1h. Stop mais largo perde *menos*, mas não vira lucro. Não ampliei o grid para "achar" algo positivo, porque isso seria tuning ex-post.
- **Trend diário:** o long-only comprou no topo de abril/2024 (quase todo o universo estava com EMA50>EMA200 no início do OOS) e saiu nos cruzamentos de baixa com −30% a −40%. 2026 foi positivo (+14,8%/trade), mas 2024 e 2025 afundaram. Funding pago no long-only: cerca de −$37, por exemplo −19,7% do notional num XRP segurado por 1 ano em mercado de alta.

### Checagem do trend diário (os números são grandes, mas estão corretos)
1. A exp/trade é calculada sobre o notional. Não há risco nem alavancagem envolvidos, porque o trend usa sizing por fração fixa (1/35) e não tem SL.
2. Conferi 5 trades à mão contra os klines 1d do cache: entrada e saída são as aberturas do dia seguinte ao sinal, o % bate com a variação crua do preço (descontado o slippage de 0,03%), e EMA50>EMA200 vale na véspera da entrada e falha na véspera da saída.
3. As 11 saídas `eod` são todas no fim do OOS (2026-09-30), nunca em fronteira de fold. O OOS é uma simulação contínua.
4. O funding de −$37 tem o sinal certo: long paga quando a taxa > 0. A magnitude também é plausível: ~10–20%/ano do notional em mercado de alta, sobre uma exposição média bem abaixo de 1x.
5. Recalculei de forma independente (loop simples sobre os closes, −0,1% por round trip, sem funding): long-only deu 107 trades, win 15,9% e média −17,7%/trade. Long/short deu 204 trades, win 28,4% e média −7,4%. Isso bate com o motor (−18,4% / −8,2% com funding e slippage). O payoff de 1,55 é consistente com a exp de −16,6%: 0,16×48 − 0,84×31 ≈ −18.
6. Sensibilidade: entrando só em cruzamentos novos (sem comprar a tendência já em curso no início do OOS), o long-only ainda dá −10%/trade e o long/short −2%/trade. Conclusão: o resultado é real. As altcoins do universo caíram ~51% no período, e o cruzamento EMA50/200 sai tarde demais para proteger.

Há um teste de regressão para isso: `test_daily_ema_trend_matches_independent_loop`.

## Dados e cobertura

- Range fixo: **2023-10-01 → 2026-10-01** (3 anos), com aquecimento antes do início (300 barras por TF; 400 no 1d).
- Universo: `config.BINANCE_UNIVERSE_WHITELIST`. Ele está **vazio no código local**, porque a whitelist real mora no `.env` da VM, que não li. Então o harness usou como fallback as **35 moedas do PDF "Binance - Estratégia Padrão" sem 1000PEPE**, a mesma lista que esteve na VM entre 13/06 e 21/06. Para passar outra lista, use `--symbols A,B,C` ou a env `TRADING_BOT_BINANCE_UNIVERSE_WHITELIST`.
- Cobertura: 35/35 pares em todos os TFs (3m, 5m, 15m, 1h, 4h, 1d), **sem gaps**. A única listagem posterior ao início é a **NOTUSDT (2024-05-16)**. Ela só opera depois de ter a janela completa de todos os TFs (no 1h/4h/1d, isso significa ~255 dias de 1d), sem fail-open. Detalhe em `runtime/backtest_v2/out/coverage.json`.
- Funding: histórico real de `/fapi/v1/fundingRate`, usando `markPrice` quando disponível.
- O **3m rodou nos 3 anos completos** (não precisou encurtar). Cache total: **~622 MB**.

## Como rodar

```bash
cd trading_bot  # raiz do repo
# 1) baixar/cachear (idempotente; meses já baixados não são rebaixados). ~1h40 na 1ª vez.
PYTHONPATH=. .venv/bin/python -m scripts.backtest_v2.run download
# 2) cobertura
PYTHONPATH=. .venv/bin/python -m scripts.backtest_v2.run coverage
# 3) backtest de tudo (o 3m leva ~1h; os outros, segundos a ~1 min com sinais em cache)
PYTHONPATH=. .venv/bin/python -m scripts.backtest_v2.run backtest --workers 6
#    só algumas:  --only live_pullback_1h_4h_1d,daily_ema50_200_long_only
#    3m/5m numa janela menor:  --start-3m 2025-10-01
# 4) testes
.venv/bin/python -m pytest tests/test_backtest_v2_engine.py -q
```

Saídas em `runtime/backtest_v2/out/`: `summary.json` (todas as métricas, folds, grid de treino, por ano/símbolo/lado), `report.md`, `oos_trades_<estratégia>.csv` e `equity_<estratégia>.csv`. O diretório `runtime/backtest_v2/` é ignorado pelo git por um `.gitignore` próprio e versionado (`*` + `!.gitignore`). O `.gitignore` da raiz **não foi editado**.

Reprodutibilidade: rodei duas vezes, e o `summary.json` e os CSVs de trades saíram idênticos.

## O que o motor faz (decisões)

- **Sinal causal:** a função REAL `HedgeStrategy().analyze_market_pullback` é chamada barra a barra com as mesmas janelas do bot (260 exec/confirm, 255 HTF). A barra de confirmação ou HTF só entra depois de **fechar** (alinhamento por `close_time ≤ t`). O aquecimento é calculado por TF, o que corrige de forma geral o bug "≥210 barras" que zerava o 1h/4h/1d. Um pré-filtro vetorizado (candle, volume, RSI) só pula barras em que a função certamente devolveria NEUTRAL. A paridade com a força bruta está testada.
- **Entrada** na abertura da barra seguinte + 0,03% de slippage. O sinal só existe depois do close, e o bot envia market logo em seguida.
- **SL/TP intrabar** a partir do preço de entrada efetivo. Se a mesma barra toca os dois, conta como SL. Se há gap através do SL, a saída é na abertura.
- **Custos:** fee taker de 0,05% por perna. O stop paga 0,13% (0,03% + 0,10% extra de STOP_MARKET). TP e saídas por sinal ou no fim pagam 0,03%.
- **Funding** em cada `fundingTime` estritamente dentro do trade: o long paga se a taxa > 0, o short recebe.
- **Uma posição por símbolo**, teto de 6 simultâneas (pullback) e risco de 1% da banca até o SL, com notional ≤ 1x a banca e bruto ≤ 3x. A posição aberta no fim é fechada no último close. Nos treinos, os trades são truncados no fim da janela, para não olhar o período de teste.
- **Walk-forward:** grid de 6 células (SL% × RR ∈ {1,5; 2,5}; SL 3m = {0,6; 1,0; 1,5}, 15m = {1,0; 1,5; 2,5}, 1h = {1,5; 2,5; 4,0}). A melhor célula é escolhida pelo retorno **no treino** e aplicada no trimestre seguinte. Só o OOS concatenado é reportado. O trend diário não tem parâmetro ajustado.

## Ressalvas

- **Survivorship:** o universo é a lista de hoje, com moedas que ainda existem e foram escolhidas olhando para trás. Isso tende a *favorecer* os resultados, e mesmo assim deu negativo.
- **O bot ao vivo avalia o candle em formação** (o `get_klines` inclui a barra aberta). O backtest usa só barras fechadas, porque o histórico não guarda o estado intrabar. É a diferença de fidelidade mais relevante.
- **Ficaram de fora** gate de IA, trailing/breakeven, kill switch, cooldown, regime swap, rotação por score e viés de sentimento. O teste é da **entrada + SL/TP fixo**. É bem possível que os filtros cortem trades ruins, mas eles não transformam uma expectativa bruta negativa em positiva.
- O bootstrap supõe trades independentes. Trades simultâneos em alts correlacionadas tornam o IC otimista (estreito demais). Isso reforça o "não".
- O DD é marcado a mercado só no fechamento diário (UTC), então o DD intradiário fica subestimado. O funding entra no P&L no fechamento do trade, não no dia em que é pago.
- O teto de 6 posições barra muitos candidatos no pullback (77.770 no 3m). Isso afeta o retorno da carteira, não a exp/trade.
- Slippage fixo por perna. Em alts ilíquidas (memória: STG, BNB) o slippage real foi maior, então os números reais tendem a ser **piores**.

# Relatório: trend-following 4h pré-registrado

Data: 2026-10-24. Pesquisa apenas. Nada aqui entra no bot, na VM ou no deploy.

Pré-registro: `scripts/backtest_v2/TREND4H_PREREG.md`, commit `7db35cb`, feito antes do código e de qualquer backtest. Código: `scripts/backtest_v2/vision.py` (dados) e `scripts/backtest_v2/trend4h.py` (universo, sinais, walk-forward, holdout, métricas). Para reproduzir:

    PYTHONPATH=. .venv/bin/python -I -m scripts.backtest_v2.trend4h download
    PYTHONPATH=. .venv/bin/python -I -m scripts.backtest_v2.trend4h run
    PYTHONPATH=. .venv/bin/python -I -m scripts.backtest_v2.trend4h report

## Veredito

**Nenhuma das duas variantes passa no critério pré-registrado.** As duas falham por pouco, e em mais de um critério:

| critério | don (Donchian/turtle) | tsmom (momentum) |
|---|---|---|
| C1 IC da expectativa > 0 ou Sharpe ≥ 0,8 com IC > 0 | FALHA: exp +0,41% (−0,66; +1,59), Sharpe 0,85 (−0,01; 1,66) | FALHA: exp +1,28% (−0,14; +2,95), Sharpe 0,82 (−0,04; 1,66) |
| C2 ≥ 3 de 5 anos positivos | OK (4/5) | OK (4/5) |
| C3 holdout positivo | OK (+158%) | OK (+15%) |
| C4 DD < 35% no WF e no holdout | FALHA: 39,1% | FALHA: 35,3% |
| C5 ≥ 6/8 células positivas e Sharpe mediano > 0 | OK (7/8) | OK (8/8) |
| C6 P&L > 0 sem os 5 melhores trades | FALHA: −5.927 | OK: +6.567 |
| C7 ≥ 200 trades | OK | OK |

Leitura honesta: há sinal de que trend-following no 4h é melhor que tudo o que veio antes (o pullback 3m tinha expectativa bruta negativa em todas as formas). Aqui a expectativa bruta é positiva nas 16 células e a líquida é positiva em 15 de 16. Mas a evidência não alcança o nível pré-registrado. Os ICs incluem zero, o drawdown com 0,5% de risco passa de 35%, e o Donchian depende de meia dúzia de trades (XRP nov/2024 sozinho vale 55% do P&L). Pela regra do pré-registro, isso é falha, e não há rodada extra para ajustar regra.

## Dados e cobertura

- Fonte: `data.binance.vision`, zips mensais de 1d, 4h e funding. 894 USDT perps candidatos (todos os que existem ou existiram, sem trimestrais e sem stablecoin/fiat como base). 19.794 zips de 1d. 12.985 zips de 4h e 12.604 de funding para os 316 símbolos que entraram no top-30 ao menos uma vez.
- **Deslistados incluídos: 54 dos 316** pararam de negociar antes do fim (LUNA, FTT, SRM, MATIC, FTM, AGIX, OCEAN, WAVES, EOS, MKR, TON e outros; a lista completa está em `runtime/backtest_v2/out/trend4h/results.json`, `coverage.dead_symbols`). 193 pares foram negociados no walk-forward do Donchian.
- 4h desde 2020-01-01. O universo só tem pares elegíveis (≥ 60 dias) a partir de março/2020 e chega a 30 pares em 2020-08-31. O primeiro fold de treino (2020) tem, portanto, menos de 12 meses úteis.
- 2,34 milhões de barras 4h e 1,47 milhão de eventos de funding. Todo símbolo com 4h tem funding. Buracos de 4h são raros (1 a 4 barras por símbolo, manutenções da exchange).

### Bug encontrado e corrigido (a primeira rodada foi descartada)

A primeira rodada (`results_run1_bug_zero_volume.json`, não usada) tinha um erro de dados. **O arquivo de um par deslistado continua até hoje com barras planas de volume zero no último preço.** O FTTUSDT, por exemplo, tem barras desde 2022-11-15 até 2026-09-30, todas com O=H=L=C=1,59 e volume 0. Efeitos: o par parecia vivo (a contagem dava 15 deslistados em vez de 54), continuava elegível no universo, e uma posição aberta na deslistagem nunca saía, ocupando vaga e notional até o fim da janela. Em 11 pares deslistados e depois relistados (CVC, TLM, CVX, SLP, ...) as barras de volume zero ficam no meio da série.

Correção: `vision.load_klines` descarta barras com volume 0. `Book.simulate` fecha um trade no último close antes de um buraco maior que 7 dias, conforme a regra do pré-registro para par deslistado. As duas mudanças têm teste. Depois disso rodei tudo de novo, uma vez. Os números abaixo são dessa segunda rodada. Nenhuma regra de estratégia mudou.

A correção mexeu pouco no walk-forward (Donchian 2463 → 2485 trades, exp +0,426% → +0,407%), mas mudou muito o holdout do Donchian (+34% → +158%). Isso não é o bug "inflando" o resultado. O motivo é dependência de caminho: com 10 vagas, liberar uma vaga muda quais trades entram, e no holdout um único trade (MYX, set/2025, +1.663%) vale 8.358 sobre uma equity de 9.487. Esse grau de sensibilidade é, por si, um alerta.

## Walk-forward OOS (2021-01 → 2025-07, 18 folds, treino móvel de 12 meses, teste de 3)

| variante | trades | win% | ganho / perda méd | exp/trade (IC95% bloco-semana) | exp bruta | CAGR | DD máx | Sharpe (IC95%) | Calmar | LONG / SHORT exp (n) |
|---|---|---|---|---|---|---|---|---|---|---|
| don | 2485 | 29.1% | +17.79% / -6.74% | **+0.407%** (-0.656%; +1.594%) | +0.653% | +36.2% | 39.1% | 0.85 (-0.01; 1.66) | +0.92 | +0.894% (1383) / -0.204% (1102) |
| tsmom | 1796 | 33.1% | +18.43% / -7.20% | **+1.275%** (-0.136%; +2.948%) | +1.759% | +27.4% | 35.3% | 0.82 (-0.04; 1.66) | +0.78 | +2.037% (917) / +0.480% (879) |

Expectativa por trade é % do notional, líquida de fee, slippage e funding. CAGR, DD e Sharpe são da carteira com o sizing pré-registrado: 0,5% de risco por trade, 10 posições no máximo, equity diária marcada a mercado.

| | don | tsmom |
|---|---|---|
| retorno por ano | 2021 +42,3%, 2022 +23,3%, 2023 +137,5%, 2024 +35,7%, 2025-H1 −29,1% | 2021 +98,2%, 2022 +3,0%, 2023 +32,5%, 2024 +30,4%, 2025-H1 −15,8% |
| P&L total / sem os 5 melhores | +30.115 / **−5.927** | +19.705 / +6.567 |
| 5 melhores trades | XRP +16.504, DOGE +5.896, SOL +4.661, APT +4.620, FTM +4.360 | DOGE +3.815, BNB +3.380, DOGE +2.042, LUNA +2.006, DOGE +1.894 |
| exp/trade sem os 5 melhores | −0,146% | +0,160% |
| fees | 5.493 (0,100%/trade) | 2.309 (0,101%/trade) |
| funding | −5.732 (−0,146%/trade) | −4.604 (−0,384%/trade) |
| turnover | 84×/ano | 42×/ano |
| hold médio | 5,8 dias | 8,9 dias |
| saídas | stop 1323, canal 1153, fim 9 | sinal 1196, stop 588, fim 12 |

Células escolhidas por fold. O Donchian ficou em `n40/m20/k2` em 12 de 18 folds. O tsmom alternou entre L=42, 84 e 180 (k=3 sempre) e foi para L=360 no último fold.

## Holdout (2025-07 → 2026-10, uma rodada, célula escolhida no treino 2024-07 → 2025-07)

| variante | trades | win% | ganho / perda méd | exp/trade (IC95% bloco-semana) | exp bruta | CAGR | DD máx | Sharpe (IC95%) | Calmar | LONG / SHORT exp (n) |
|---|---|---|---|---|---|---|---|---|---|---|
| don `n40 m20 k2` | 743 | 31.9% | +26.65% / -6.87% | **+3.820%** (-0.714%; +10.162%) | +2.219% | +113.0% | 30.0% | 1.41 (-0.27; 2.64) | +3.76 | +8.592% (343) / -0.272% (400) |
| tsmom `L180 k3` | 338 | 29.9% | +19.08% / -7.76% | **+0.261%** (-1.669%; +2.191%) | +0.384% | +11.7% | 17.0% | 0.52 (-1.28; 2.21) | +0.69 | -2.314% (163) / +2.659% (175) |

Os dois ficam positivos no holdout, mas sem os 5 melhores trades os dois ficam negativos (don −1.698, tsmom −761). O holdout do Donchian vem quase todo de dois pumps de small cap (MYX +1.663% e AKE +1.323% por trade).

## Robustez: as 8 células de cada variante, fixas no período OOS do WF

Donchian:

| célula | trades | win% | exp/trade (IC95%) | CAGR | DD | Sharpe | anos + |
|---|---|---|---|---|---|---|---|
| n20 m10 k2 | 3986 | 32.3% | +0.033% (-0.657%; +0.810%) | +9.8% | 46.0% | 0.43 | 4/5 |
| n20 m10 k3 | 3582 | 35.4% | -0.030% (-0.740%; +0.776%) | +5.9% | 36.3% | 0.34 | 4/5 |
| n40 m20 k2 | 2458 | 30.2% | +0.656% (-0.479%; +1.980%) | +48.5% | 37.3% | 1.02 | 4/5 |
| n40 m20 k3 | 2071 | 34.7% | +0.713% (-0.665%; +2.307%) | +31.4% | 28.3% | 0.93 | 4/5 |
| n55 m20 k2 | 2357 | 28.9% | +0.728% (-0.553%; +2.228%) | +40.8% | 41.5% | 0.91 | 3/5 |
| n55 m20 k3 | 1969 | 34.5% | +0.918% (-0.663%; +2.768%) | +30.8% | 32.8% | 0.90 | 3/5 |
| n100 m50 k2 | 1373 | 24.2% | +1.869% (-0.106%; +4.083%) | +39.9% | 46.8% | 0.91 | 3/5 |
| n100 m50 k3 | 1082 | 31.8% | +1.763% (-0.675%; +4.503%) | +22.5% | 33.4% | 0.74 | 3/5 |

TSMOM:

| célula | trades | win% | exp/trade (IC95%) | CAGR | DD | Sharpe | anos + |
|---|---|---|---|---|---|---|---|
| L42 k3 | 3195 | 34.3% | +0.215% (-0.619%; +1.179%) | +19.8% | 30.2% | 0.68 | 4/5 |
| L42 k5 | 3021 | 34.8% | +0.503% (-0.425%; +1.622%) | +19.3% | 21.4% | 0.88 | 4/5 |
| L84 k3 | 1898 | 34.4% | +0.708% (-0.682%; +2.266%) | +25.4% | 39.2% | 0.79 | 5/5 |
| L84 k5 | 1633 | 37.2% | +1.740% (-0.146%; +3.903%) | +25.2% | 27.5% | 1.03 | 4/5 |
| L180 k3 | 1133 | 35.7% | +3.732% (+1.395%; +6.245%) | +51.1% | 36.5% | 1.25 | 5/5 |
| L180 k5 | 957 | 41.0% | +3.974% (+1.061%; +7.217%) | +27.2% | 24.7% | 1.04 | 4/5 |
| L360 k3 | 752 | 28.6% | +1.342% (-1.525%; +4.674%) | +9.7% | 52.6% | 0.44 | 3/5 |
| L360 k5 | 553 | 36.2% | +3.222% (-0.677%; +8.007%) | +8.8% | 42.1% | 0.47 | 3/5 |

O padrão é consistente nos dois grids. Canais e lookbacks mais lentos (n40 a n100, L84 a L180) são melhores que os rápidos, e as células vizinhas andam juntas, sem um pico isolado. A grade inteira do tsmom é positiva. Só duas células (L180 k3 e L180 k5) têm IC > 0. Escolher uma delas agora seria escolher pelo OOS, com 16 células testadas nesta rodada, e é exatamente o que o pré-registro proíbe.

## O que pesa contra

- **Custos e funding comem metade do bruto.** No Donchian, bruto +0,65%/trade vira +0,41% depois de fee (0,10%) e funding (−0,15%). No tsmom, o funding é −0,38%/trade: carregar LONG por dias em cripto paga funding positivo.
- **Cauda direita concentrada.** Win rate de 29 a 33% com ganho médio 2,6× a perda média é o perfil esperado de trend-following, mas aqui a cauda é estreita demais. O Donchian sem os 5 melhores trades de 4,5 anos perde dinheiro.
- **Pior ano é o mais recente.** 2025-H1 é −29% (don) e −16% (tsmom). O holdout positivo vem de poucos pumps de small cap.
- **SHORT não paga no Donchian** (−0,20%/trade no WF, −0,27% no holdout). No tsmom o SHORT é positivo no WF (+0,48%) e no holdout (+2,66%), mas o LONG do holdout é −2,31%: os lados se alternam.
- **Dependência de caminho.** Uma correção de dados que mexeu em poucas dezenas de trades mudou o holdout do Donchian de +34% para +158%.

## Ressalvas

- Múltiplos testes: 2 variantes, 16 células nesta rodada, mais 12 variantes e 37 células em `EXPERIMENTOS.md`. A família Donchian 4h já tinha sido olhada em 2023-10 → 2026-10, período que cobre o holdout. Para estas regras exatas (stop ATR, sizing por vol, universo dinâmico) o holdout é inédito, mas para a família não é.
- Vela em formação vs fechada: todos os sinais usam a barra 4h fechada, e a entrada é na abertura seguinte. No 4h o efeito de agir alguns segundos depois do fechamento é desprezível perto do stop (k × ATR 4h).
- Viés de sobrevivência residual: só existe o que está no arquivo público. A liquidação real de um par deslistado pode diferir do último close negociado.
- Empate de candidatos no mesmo instante segue a ordem alfabética do motor, e o teto de 10 posições corta trades em dias de rompimento geral.
- Funding sem mark price no arquivo: uso a abertura da barra 4h que contém o evento.
- 2020 entra só como treino, com universo parcial até agosto.

## Próximo passo recomendado

Não integrar no bot. O backtest não sustenta edge com a confiança pré-registrada.

Se quiser continuar nessa linha, a única evidência nova e não contaminada que resta é o futuro. A proposta é um teste forward (paper ou testnet com sinais da mainnet), com regra e critério fixados antes:

1. Uma única configuração: Donchian `n40/m20/k2` (a escolhida em 12 de 18 folds e no holdout). Escolher o tsmom L180 seria escolher pelo OOS, então não entra.
2. Risco por trade de 0,25% (não 0,5%). Isso é decisão de risco, não é resgate do critério de DD que falhou.
3. Critério de parada e sucesso escrito antes: por exemplo, 6 meses ou 150 trades, expectativa líquida > 0 e DD < 20%.

Se essa forward falhar, a conclusão é que não há edge capturável com este universo e estes custos, e o próximo trabalho seria reduzir custo (maker, menos turnover) antes de testar outra ideia de sinal.

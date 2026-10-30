# Relatório: cash-and-carry de funding (spot long + perp USDT-M short), pré-registrado

Data: 2026-10-30. Pesquisa apenas. Nada aqui entra no bot, na VM ou no deploy. O forward test Donchian 4h não foi tocado.

Pré-registro: `scripts/backtest_v2/FUNDING_PREREG.md`, commit `2cd7547`, escrito antes de rodar qualquer simulação. Código: `scripts/backtest_v2/funding_carry.py` (grade de 8h nos instantes de funding; posição delta-neutra com margem isolada, rebalance, liquidação da perna short pela máxima de 4h). Testes: `tests/test_backtest_v2_funding.py` (sinal, histerese, short recebe/paga funding, basis, rebalance, liquidação intrapasso, rotação, funding anterior à grade). Para reproduzir (~15 s, sem numba):

    PYTHONPATH=. python -m scripts.backtest_v2.funding_carry download   # spot 4h dos 17 símbolos (data.binance.vision)
    PYTHONPATH=. python -m scripts.backtest_v2.funding_carry run
    PYTHONPATH=. python -m scripts.backtest_v2.funding_carry report

Saídas em `runtime/backtest_v2/out/funding_carry/` (`results.json` com as 630 simulações, `tables.md` com todas as tabelas e a checagem automática dos critérios).

## Veredito

**FAIL nas três variantes pelos critérios pré-registrados.** O carry existe e é positivo na maioria dos pares e dos anos, mas é pequeno: com $300 em BTC, "sempre dentro" rende **+$44 em 4,75 anos (+2,9% a.a.)** e **+$3 nos 9 meses de 2026 (+1,1% a.a.)**. Entrar e sair por limiar de funding corta o rendimento pela metade (taxas + tempo fora); rodar para o par de maior funding destrói a conta em taxas (−34% a −72%). Com $3.000 é exatamente ×10 (nenhum piso de notional modelado): +$437 em 4,75 anos no BTC.

| variante | célula padrão | crit. 1: líquido > 0 (completo E holdout) | crit. 2: DD < 10% | crit. 3: Sharpe > 1 | células que passam | resultado |
|---|---|---|---|---|---|---|
| V0 sempre dentro | BTC, L = 1 | OK: +$44 / +$3 | OK: 0,2% | OK: 9,3 | BTC passa, mas só **7 de 17** símbolos passam (exigia 9) | **FAIL** |
| V1 limiar | BTC, k = 9, θ = 10% | OK por margem zero: +$19 / **+$0,03** | OK: 1,4% | OK: 3,3 | BTC passa, mas só **3 de 17** símbolos passam na célula padrão (8 de 119 no grid) | **FAIL** |
| V2 rotação | top-3 diário | FALHA: **−$101 / −$20** | FALHA: 33,6% | FALHA: −4,6 | 0 de 4 | **FAIL** |

H1 rejeitada no sentido pré-registrado. H0 parcialmente confirmada: o retorno não é dominado por basis nem por rebalances (basis ≈ $0 nos pares grandes), é dominado pelo tamanho do funding, que em 2025–2026 caiu para 1–5% a.a. nos pares grandes, e pelas taxas quando há giro.

Por que o teto é baixo: com `L = 1` só metade do capital está no spot ganhando funding (a outra metade é margem). BTC pagou em média 6,6% a.a. de funding em 2022→2026, logo o teto bruto é ~3,3% a.a. sobre o capital; a simulação dá 2,9%. Com `L = 2` (dois terços no spot) o teto é ~4,4% e sai 3,7%, com 4 rebalances forçados por rali. Os pares de maior funding (ORDI, SATS, PEPE, CFX) ficam em 1,7–2,3% a.a. sobre o capital no período completo porque o funding alto deles foi um episódio (2023–2024) e em 2025–2026 caiu ou virou negativo.

## Dados e cobertura

- Funding real por evento (`cache/vision/funding/`, `data.binance.vision`), somado por passo de 8h (pares com funding a cada 4h ficam corretos). Perp 4h já em cache; spot 4h baixado para os 17 símbolos (único download desta rodada, ~700 zips mensais, ~4 MB). Grade de 8h: 5.202 passos em 2022-01-01 → 2026-09-30; holdout 2026-01-01 → 2026-09-30, 819 passos.
- Universo (17, seleção pré-registrada): BTC, ETH, SOL, ADA, BNB, XRP, DOGE (cobertura completa) + BIGTIME (2025-04→), WIF (2024-03→), 1000SATS (2023-12→), ORDI (2023-11→), AGIX (2023-02→2024-06, deslistado), 1000BONK (2023-12→), RNDR (2023-02→2024-07, renomeado), 1000PEPE (2023-05→), CFX (2023-02→), JTO (2023-12→). AGIX e RNDR não têm dado em 2026, então nunca poderiam passar o critério 1 no holdout; mesmo descontando os dois, 7 de 15 continua abaixo de 9.
- Basis spot−perp no close de 4h: mediana +4 a +5 bp nos pares grandes (p5/p95 ±6 a ±12 bp); ±45 bp em PEPE e SATS. Alinhamento spot/perp verificado passo a passo (ambas as pernas precisam ter barra no mesmo instante).

### Funding anualizado por ano (soma dos eventos / dias × 365)

| par | 2022 | 2023 | 2024 | 2025 | 2026 (9m) |
|---|---|---|---|---|---|
| BTC | +4,2% | +7,9% | +11,9% | +5,1% | +2,9% |
| ETH | +0,8% | +8,3% | +13,0% | +4,9% | +1,9% |
| SOL | −38,6% | +1,3% | +13,6% | +0,3% | −1,1% |
| ADA | −1,0% | +7,2% | +14,0% | +4,1% | +1,4% |
| BNB | −12,6% | −8,3% | −3,3% | −2,1% | +2,9% |
| XRP | −0,1% | +8,2% | +14,2% | +3,5% | −0,3% |
| DOGE | +3,4% | +10,0% | +14,0% | +4,3% | +3,0% |
| ORDI | — | +46,2% | +19,0% | +5,6% | +4,9% |
| 1000SATS | — | — | +20,4% | +7,5% | +7,6% |
| 1000PEPE | — | +10,2% | +20,1% | +3,8% | −0,9% |
| WIF | — | — | +20,2% | +0,9% | −3,8% |
| 1000BONK | — | — | +18,3% | +0,8% | −7,1% |
| JTO | — | — | +17,7% | +0,8% | −32,6% |
| CFX | — | +11,0% | +17,0% | +5,9% | +2,9% |

2024 foi o ano do carry (12–20% a.a. bruto, ou 6–10% sobre o capital a 1×). 2022 teve funding negativo em SOL e BNB (o short paga). 2026 é o pior ano para o carry nos pares grandes, e JTO pagou −32,6% a.a.

### Mudanças depois do pré-registro (declaradas)

Nenhuma regra, grid, custo, período, universo ou critério mudou. Quatro mudanças:

1. **Bug corrigido antes da primeira rodada completa:** eventos de funding anteriores ao início da grade caíam todos no passo 0 (BTC mostrava 16,7% a.a. em vez de 6,6%). Encontrado na checagem de sanidade dos dados, corrigido com teste de regressão, antes de qualquer resultado de estratégia.
2. **Métrica redefinida depois de ver resultado:** "basis" era só o P&L realizado da perna perp (não fechava com o líquido); passou a ser o resíduo `líquido − funding + taxas`, que é exatamente o descasamento spot/perp mais a margem perdida em liquidação. Não entra em critério.
3. **Bug de exibição corrigido:** "tempo em posição" da rotação top-3 somava os 3 slots (mostrava 292%); passou a dividir pelo número de slots. Não entra em critério.
4. **Checagem automática dos critérios** adicionada ao `report` para não contar células à mão.

## Resultados: V0 "sempre dentro", L = 1, taxas A (taker), $300

| par | completo: líquido (CAGR) | Sharpe | DD máx | taxas | funding | entradas/rebal/liq | tempo em pos | holdout 2026: líquido (CAGR) | passa? |
|---|---|---|---|---|---|---|---|---|---|
| BTC | +$44 (+2,9% a.a.) | 9,3 | 0,2% | $0,2 | +$44 | 1/2/0 | 100% | +$3 (+1,1%) | sim |
| ETH | +$33 (+2,2%) | 7,7 | 0,4% | $0,2 | +$33 | 1/0/0 | 100% | +$2 (+0,7%) | sim |
| SOL | +$16 (+1,1%) | 1,4 | 3,3% | $1 | +$17 | 3/2/0 | 100% | −$1 (−0,4%) | não (holdout) |
| ADA | +$17 (+1,2%) | 2,9 | 0,4% | $0,2 | +$18 | 1/0/0 | 100% | +$1 (+0,5%) | sim |
| BNB | **−$24 (−1,7%)** | −2,7 | 8,8% | $0,2 | −$23 | 1/3/0 | 100% | +$2 (+1,0%) | não |
| XRP | +$30 (+2,0%) | 4,8 | 0,6% | $1 | +$32 | 3/4/0 | 100% | −$0,4 (−0,2%) | não (holdout) |
| DOGE | +$34 (+2,3%) | 5,6 | 0,2% | $0,2 | +$34 | 1/2/0 | 100% | +$2 (+1,1%) | sim |
| BIGTIME | +$3 (+0,2%) | 0,5 | 0,5% | $0,2 | +$3 | 1/0/0 | 31% | +$2 (+1,1%) | não (Sharpe) |
| WIF | +$20 (+1,4%) | 4,0 | 0,2% | $1 | +$20 | 1/3/0 | 54% | −$2 (−0,9%) | não (holdout) |
| 1000SATS | +$25 (+1,7%) | 3,6 | 0,8% | $0,2 | +$28 | 1/1/0 | 59% | +$4 (+1,8%) | sim |
| ORDI | +$35 (+2,3%) | 4,9 | 0,4% | $1 | +$35 | 1/6/0 | 61% | +$11 (+5,0%), 1 liquidação | sim (ver nota) |
| AGIX | +$25 (+1,7%) | 4,0 | 0,2% | $1 | +$26 | 1/3/0 | 28% | sem dado | não |
| 1000BONK | +$27 (+1,8%) | 3,9 | 0,7% | $0,2 | +$27 | 1/1/0 | 59% | −$3 (−1,4%) | não (holdout) |
| RNDR | +$26 (+1,7%) | 2,9 | 1,2% | $1 | +$26 | 1/6/0 | 31% | sem dado | não |
| 1000PEPE | +$30 (+2,0%) | 1,3 | 0,7% | $1 | +$32 | 1/5/0 | 72% | −$0,4 (−0,1%) | não (holdout) |
| CFX | +$25 (+1,7%) | 3,9 | 0,2% | $0,2 | +$26 | 1/1/0 | 76% | +$2 (+1,0%) | sim |
| JTO | +$21 (+1,4%) | 3,4 | 1,1% | $0,2 | +$21 | 1/1/0 | 59% | **−$31 (−13,5%)**, DD 10,7% | não |

Tempo em posição < 100% = par listado depois de 2022 (o capital fica parado a 0% antes). "Líquido $3.000" = ×10 em todas as linhas.

Notas: (a) o Sharpe é alto porque a equity de uma posição neutra quase não oscila (funding é acréscimo suave); Sharpe 9 com 2,9% a.a. é um sinal de que o critério 3 mede pouco aqui. (b) ORDI no holdout: o +$11 vem de uma **liquidação** da perna short num rali (perde a margem toda, $150, mas o spot continuou subindo e foi vendido no close acima do preço de liquidação: +$9 de "basis"). É um ganho de aposta direcional involuntária, não de carry; com azar o sinal é o oposto. (c) JTO 2026: funding −32,6% a.a. durante 9 meses, o short pagou $31. "Sempre dentro" não tem defesa contra funding negativo prolongado. (d) BNB: funding negativo em 4 dos 5 anos.

`L = 2` (sensibilidade): BTC +$57 (+3,7% a.a.), 4 rebalances, 0 liquidações; WIF teve 1 liquidação no completo e 1000SATS teve 3 na V1 a 2×. Os memecoins com máximas de +40–70% num passo de 8h (SATS 1,72×, WIF 1,48×, CFX 1,49×) estão na faixa em que 2× liquida (+49%).

Taxas B (maker): resultados iguais ao centavo na V0 (uma entrada, taxas de $0,2); BTC V1 padrão sobe de +$19 para +$22; rotação top-3 diário melhora de −$101 para −$72 e continua negativa.

## Resultados: V1 limiar (k = 9, θ = 10%, L = 1, taxas A), $300

| par | completo: líquido (CAGR) | Sharpe | DD | taxas | funding | entradas | tempo em pos | holdout: líquido |
|---|---|---|---|---|---|---|---|---|
| BTC | +$19 (+1,3%) | 3,3 | 1,4% | $15 | +$33 | 33 | 35% | +$0,03 |
| ETH | +$21 (+1,4%) | 3,6 | 0,8% | $13 | +$33 | 29 | 33% | −$0,2 |
| SOL | +$15 (+1,1%) | 2,1 | 1,4% | $18 | +$32 | 39 | 29% | −$1 |
| ADA | +$21 (+1,4%) | 2,3 | 1,1% | $20 | +$39 | 43 | 42% | −$1 |
| BNB | +$3 (+0,2%) | 0,6 | 0,7% | $10 | +$10 | 21 | 9% | −$2 |
| XRP | +$20 (+1,4%) | 2,6 | 1,0% | $17 | +$36 | 37 | 34% | −$1 |
| DOGE | +$19 (+1,3%) | 2,3 | 1,3% | $22 | +$41 | 48 | 40% | −$2 |
| ORDI | +$33 (+2,2%) | 3,7 | 1,0% | $11 | +$44 | 23 | 29% | −$1 |
| 1000SATS | +$31 (+2,1%) | 0,9 | 1,2% | $17 | +$43 | 36 | 43% | +$7 |
| 1000PEPE | +$29 (+1,9%) | 0,6 | 1,9% | $20 | +$46 | 40 | 30% | −$1 |
| WIF | +$21 (+1,4%) | 2,5 | 0,7% | $9 | +$28 | 19 | 22% | +$1 |
| 1000BONK | +$23 (+1,6%) | 2,4 | 0,9% | $13 | +$34 | 27 | 23% | +$0,2 |
| CFX | +$23 (+1,5%) | 1,8 | 2,1% | $21 | +$45 | 45 | 41% | −$0,4 |
| JTO | +$24 (+1,6%) | 2,9 | 0,7% | $10 | +$32 | 22 | 27% | −$1 |

O filtro protege de BNB e JTO (+$3 e +$24 em vez de −$24 e +$21 com JTO sangrando em 2026), mas nos pares grandes recolhe o mesmo funding que "sempre dentro" (+$33 em BTC nos dois casos) pagando $15 de giro em 33 entradas e ficando 65% do tempo fora. No holdout 2026 fica entre −$2 e +$7: o sinal quase nunca passa de 10% a.a. em 2026.

Grid completo em BTC (completo / holdout): k = 3 gira demais (θ = 5%: 185 entradas, $78 de taxas, **−$33** / −$8; θ = 10%: −$14 / −$4); k = 3 e θ = 20% quase nunca entra (+$7, 6% do tempo); k = 9 e θ = 5% é a melhor célula de BTC (+$23 / +$1, 56 entradas, 69% do tempo); k = 9 e θ = 20% fica 6% do tempo dentro (+$10 / $0). 8 das 119 células do grid passam os três critérios (BTC ×3, WIF ×2, BONK ×2, SATS ×1).

## Resultados: V2 rotação (L = 1), $300

| célula | taxas | completo: líquido | Sharpe | DD | taxas $ | funding $ | entradas | holdout: líquido |
|---|---|---|---|---|---|---|---|---|
| top-1, 8h | A | **−$217 (−72%)** | −7,2 | 72% | $270 | +$48 | 1.066 | −$46 |
| top-1, diário | A | −$149 (−50%) | −4,0 | 50% | $214 | +$59 | 662 | −$24 |
| top-3, 8h | A | −$164 (−55%) | −8,7 | 55% | $224 | +$57 | 2.136 | −$34 |
| **top-3, diário (padrão)** | A | **−$101 (−34%)** | −4,6 | 34% | $169 | +$64 | 1.349 | −$20 |
| top-3, diário | B (maker) | −$72 (−24%) | −3,2 | 24% | $144 | +$67 | 1.349 | −$14 |

O funding capturado pela rotação (+$48 a +$67) é parecido com o do BTC parado (+$44), e as taxas são 3 a 6 vezes maiores. O ranking por média de 3 dias troca o líder quase todo dia entre pares com funding parecido; a regra "fica enquanto está no top-n" não segura o giro.

## Benchmarks ($300, mesmo período)

| | completo | Sharpe | DD máx | pior mês | holdout 2026 |
|---|---|---|---|---|---|
| BTC spot comprado e segurado (taxa 0,1%) | +$246 (+82%; +13,4% a.a.) | 0,5 | 67% | 2022-06 −40% | −$12 (−4,1%), DD 40% |
| USDT parado | $0 | — | 0% | 0% | $0 |
| Carry BTC sempre dentro (V0, L = 1) | +$44 (+14,6%; +2,9% a.a.) | 9,3 | 0,2% | −0,1% | +$3 (+0,8%) |

O carry fica entre os dois: muito menos retorno que segurar BTC, com um drawdown de 0,2% em vez de 67%. Comparar com USDT a 0% era o pré-registrado; qualquer rendimento de USDT em poupança da própria corretora (não modelado, não verificado aqui) compete diretamente com 1–3% a.a.

## Viabilidade prática com $300 (nota obrigatória)

- Precisa de duas carteiras fundadas (spot e futures) e transferência manual entre elas a cada rebalance. O bot hoje só opera futures: fazer carry exigiria conta spot, ordens spot e lógica de transferência, nada disso existe.
- Notional mínimo: $5 por ordem no spot e na maioria dos perps; **$100 em BTCUSDT perp**. Com $300 a 1×, BTC fica em $150/$150: cabe, mas só um par, e nenhum rebalance parcial pequeno é possível (fração abaixo de $100 não executa). Top-3 ($50 + $50 por par) não cabe em BTC.
- O resultado em $ é o problema: +$9 por ano em BTC em média no período, +$3 em 9 meses de 2026. Duas transferências erradas de carteira ou um rebalance tardio num rali custam mais que isso. A $3.000 são +$92/ano médios, +$25 em 2026.
- O risco real não está no backtest: liquidação intrapasso antes do rebalance (modelado só pela máxima de 4h), funding negativo prolongado (JTO 2026: −$31 em 9 meses, −13,5% a.a.), e deslistagem do perp com spot na mão.

## O que ficou de fora

- Rendimento do spot (Earn/staking) e do USDT ocioso: zero nos dois. Spread/slippage além do close de 4h. Piso de notional. Juros de margem.
- Rebalance para o outro lado (preço caindo, margem sobrando): a posição só encolhe, então o yield de L = 1 e 2 é levemente subestimado em quedas longas.
- Universo além dos 17 (o perp tem 316 símbolos com funding em cache; a rotação só olhou os 17). Com mais símbolos a rotação teria mais giro, não menos.
- Seleção dos 10 pares por funding do próprio período (declarada no pré-registro): torna o completo um teto otimista, e mesmo assim só 7 de 17 passam. O holdout 2026 não tem esse viés e é o pior ano.
- Variante "sai quando cai abaixo de 0" separada da "abaixo de θ/2": só a segunda foi rodada (inclui a primeira).
- Cross margin / conta unificada (Portfolio Margin), que elimina o rebalance entre carteiras: não modelado; mudaria a operação, não o tamanho do funding.

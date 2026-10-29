# Relatório: scalping popular em 1m e 5m pré-registrado

Data: 2026-10-28. Pesquisa apenas. Nada aqui entra no bot, na VM ou no deploy. O forward test Donchian 4h não foi tocado.

Pré-registro: `scripts/backtest_v2/SCALPING_PREREG.md`, commit `8797a2d`, escrito antes de baixar dado intradiário e antes de qualquer backtest. Código: `scripts/backtest_v2/scalping.py` (indicadores, as três técnicas, simulador de scalp, walk-forward, holdout, grid, métricas) sobre o motor existente (`engine.py`, `pipeline.py`, `experiments.py`, `vision.py`). Para reproduzir:

    PYTHONPATH=. .venv/bin/python -I -m scripts.backtest_v2.scalping download
    PYTHONPATH=. .venv/bin/python -I -m scripts.backtest_v2.scalping run --workers 5
    PYTHONPATH=. .venv/bin/python -I -m scripts.backtest_v2.scalping report

Saídas em `runtime/backtest_v2/out/scalping/` (`results.json`, `result_<variante>.json`, `wf_trades_*.csv`, `holdout_trades_*.csv`, `tables.md` com todas as tabelas, inclusive o grid completo por variante).

## Veredito

**Nenhuma das 10 variantes passa. Nenhuma das 52 células do grid tem expectativa líquida positiva fora da amostra.** Não é "edge pequeno que o custo come": a expectativa bruta, antes de fee e slippage, fica entre −0,014% e +0,015% por trade em todas as variantes. O sinal das três técnicas é ruído com custo em cima.

| variante | C1 IC95% da exp líq > 0 | C2 ≥ 500 trades | C3 ≥ 3/4 anos positivos | C4 holdout > 0 | C5 maioria do grid > 0 | resultado |
|---|---|---|---|---|---|---|
| s1_1m_t (VWAP+EMA, 1m, taker) | FALHA (−0,223; −0,221) | OK (699.777) | FALHA (0/4) | FALHA (−0,222%) | FALHA (0/6) | não passa |
| s1_1m_m (VWAP+EMA, 1m, maker) | FALHA (−0,168; −0,166) | OK (700.430) | FALHA (0/4) | FALHA (−0,171%) | FALHA (0/6) | não passa |
| s1_5m_t | FALHA (−0,223; −0,215) | OK (135.432) | FALHA (0/4) | FALHA (−0,215%) | FALHA (0/6) | não passa |
| s1_5m_m | FALHA (−0,168; −0,161) | OK (153.877) | FALHA (0/4) | FALHA (−0,167%) | FALHA (0/6) | não passa |
| s2_1m_t (ORB, 1m) | FALHA (−0,252; −0,211) | OK (13.760) | FALHA (0/4) | FALHA (−0,231%) | FALHA (0/6) | não passa |
| s2_5m_t (ORB, 5m) | FALHA (−0,250; −0,214) | OK (15.309) | FALHA (0/4) | FALHA (−0,212%) | FALHA (0/6) | não passa |
| s3_1m_m (Bollinger, 1m, maker) | FALHA (−0,148; −0,143) | OK (296.754) | FALHA (0/4) | FALHA (−0,143%) | FALHA (0/4) | não passa |
| s3_1m_t | FALHA (−0,229; −0,225) | OK (344.272) | FALHA (0/4) | FALHA (−0,224%) | FALHA (0/4) | não passa |
| s3_5m_m | FALHA (−0,137; −0,115) | OK (57.364) | FALHA (0/4) | FALHA (−0,146%) | FALHA (0/4) | não passa |
| s3_5m_t | FALHA (−0,221; −0,201) | OK (60.469) | FALHA (0/4) | FALHA (−0,217%) | FALHA (0/4) | não passa |

H1 (pelo menos uma técnica com expectativa líquida OOS > 0) é rejeitada. H0 confirmada com folga: o custo de ida e volta não é o único problema, porque nem antes do custo há edge.

## Dados e cobertura

- Fonte: zips mensais de `data.binance.vision` (futuros USDT-M), 1m e 5m, mais funding. Os zips ficam em `runtime/backtest_v2/cache/vision_raw/` e são lidos só como CSV em memória; nada extraído nem executado.
- Universo point-in-time do pré-registro: 31 símbolos distintos, 12 por ano, cada um só nos anos em que era top-12 por volume. Período 2021-12 (aquecimento) → 2026-09. 738 zips de 1m e 738 de 5m. Dos 31, 26 operaram no walk-forward: os 4 membros só de 2026 (HYPE, LIGHT, PIPPIN, ZEC) aparecem só no holdout, e LUNA morreu em 2022-05, dentro do primeiro treino, antes do primeiro teste.
- 30.880.590 barras 1m e 6.176.891 barras 5m, depois de descartar barras de volume zero. Buracos de 1m são raros (0 a 34 barras por símbolo), salvo AXS (256), MASK (230), PIPPIN (1.142) e LIGHT (1.594), pares ilíquidos com minutos sem negócio. LUNA termina em 2022-05-12 e MATIC em 2024-09-04 (deslistados), como previsto no pré-registro.
- Funding real do arquivo: em scalps de 5 a 50 minutos ficou em ±0,0002% por trade, irrelevante.

### Incidente de dados: a primeira rodada completa foi descartada

A sessão anterior morreu no meio da rodada. Ao retomar, a primeira rodada completa (todas as 10 variantes) rodou com 27 símbolos no 1m: os arquivos `.npz` consolidados de BTC, ETH, SOL e XRP não existiam (os zips estavam no cache, a consolidação não tinha sido feita) e `load_all` ignora `FileNotFoundError` em silêncio. Os quatro maiores pares ficaram fora das variantes de 1m, enquanto as de 5m tinham os 31. Detectado pela tabela de cobertura do `report` (27 símbolos no 1m contra 31 no 5m). Os quatro `.npz` foram reconstruídos a partir dos zips em cache (BTC: 2.541.292 barras 1m em 58 meses) e **todas as 10 variantes foram rodadas de novo** com os 31 símbolos. Este relatório usa só a segunda rodada. As variantes de 5m deram resultado idêntico nas duas rodadas (mesmo número de trades e mesma expectativa), o que confirma que o dado de 5m era o mesmo.

Com os 4 pares de volta, S1 em 1m foi de 589 mil para 700 mil trades OOS e S3 1m de 251 mil para 297 mil (maker) e de 258 mil para 344 mil (taker). As expectativas mudaram menos de 0,002 ponto percentual. A conclusão não depende dos pares que faltavam.

### Mudanças no harness depois do pré-registro (declaradas)

Nenhuma regra, grid, custo, janela ou critério mudou. Duas mudanças de infraestrutura, ambas depois de ver resultado, nenhuma afeta trade ou métrica de decisão:

1. `--resume`: reaproveita `result_<variante>.json` já existente para retomar uma rodada morta. Não foi usado na rodada final (tudo rodou do zero).
2. Correção de unidade na métrica `fee_pct_of_avg_win` (fee como % do ganho médio): estava em fração e era impressa como porcentagem (mostrava "1%" onde era 67%). Só afeta essa coluna do relatório; não entra em nenhum critério.

## Walk-forward OOS (2022-07-01 → 2026-01-01, 21 folds, treino 6 meses, teste 2)

Expectativas em % do notional por trade. IC95% por bootstrap em blocos de dia (10.000 reamostragens, seed fixa). "Bruta sem custo" = antes de fee e slippage; "bruta só c/ slippage" = com slippage no preço mas sem fee. Fee + slippage é o custo médio realizado por trade; entre parênteses, a fee sozinha como % do ganho médio.

| variante | trades | win% | ganho / perda méd | exp líq/trade (IC95%) | exp bruta sem custo / só c/ slippage | fee + slippage/trade (fee % do ganho méd) | hold méd | saídas sl / tp / tempo |
|---|---|---|---|---|---|---|---|---|
| s1_1m_t | 699.777 | 28,3% | +0,150% / −0,368% | **−0,222%** (−0,223; −0,221) | −0,001% / −0,122% | 0,100% + 0,121% (67%) | 6 min | 428.305 / 271.458 / 11 |
| s1_1m_m | 700.430 | 34,9% | +0,141% / −0,332% | **−0,167%** (−0,168; −0,166) | −0,012% / −0,097% | 0,070% + 0,085% (49%) | 5 min | 383.292 / 317.126 / 9 |
| s1_5m_t | 135.432 | 42,7% | +0,394% / −0,676% | **−0,219%** (−0,223; −0,215) | −0,004% / −0,119% | 0,100% + 0,115% (25%) | 34 min | 75.105 / 59.880 / 440 |
| s1_5m_m | 153.877 | 46,3% | +0,346% / −0,604% | **−0,164%** (−0,168; −0,161) | −0,012% / −0,094% | 0,070% + 0,082% (20%) | 28 min | 80.196 / 73.423 / 252 |
| s2_1m_t | 13.760 | 31,4% | +0,851% / −0,728% | **−0,232%** (−0,252; −0,211) | −0,006% / −0,132% | 0,100% + 0,126% (12%) | 49 min | 9.069 / 3.785 / 906 |
| s2_5m_t | 15.309 | 37,9% | +0,679% / −0,789% | **−0,232%** (−0,250; −0,214) | −0,014% / −0,133% | 0,100% + 0,119% (15%) | 51 min | 9.004 / 5.445 / 860 |
| s3_1m_m | 296.754 | 33,9% | +0,402% / −0,427% | **−0,145%** (−0,148; −0,143) | +0,000% / −0,086% | 0,060% + 0,086% (15%) | 10 min | 195.815 / 100.735 / 201 |
| s3_1m_t | 344.272 | 25,6% | +0,365% / −0,431% | **−0,227%** (−0,229; −0,225) | +0,006% / −0,127% | 0,100% + 0,133% (27%) | 9 min | 251.013 / 93.019 / 235 |
| s3_5m_m | 57.364 | 37,2% | +0,933% / −0,754% | **−0,126%** (−0,137; −0,115) | +0,015% / −0,067% | 0,060% + 0,082% (6%) | 48 min | 35.633 / 19.859 / 1.869 |
| s3_5m_t | 60.469 | 33,4% | +0,890% / −0,763% | **−0,211%** (−0,221; −0,201) | +0,014% / −0,112% | 0,100% + 0,126% (11%) | 49 min | 39.737 / 18.569 / 2.160 |

Leituras:

- A expectativa líquida é, em todas as variantes, a expectativa bruta (≈ 0) menos o custo realizado. A diferença entre variante taker e maker da mesma técnica (0,055 a 0,085 p.p.) é exatamente a fee de entrada mais o slippage que a entrada maker evita. Não há edge sendo revelado pelo custo menor; há menos custo sendo cobrado de um sinal nulo.
- LONG e SHORT são igualmente negativos em todas as variantes (diferença ≤ 0,06 p.p.), ver `tables.md`. Não há lado "bom".
- Os ICs são estreitos porque há centenas de milhares de trades. Com 13 mil trades (S2) o IC ainda fica inteiramente abaixo de −0,21%.
- Taxa de fill da entrada maker: 87% (s1_1m_m), 95% (s1_5m_m), 90% (s3_1m_m), 92% (s3_5m_m). O modelo de trade-through é otimista (assume que a fila some ao atravessar 0,01%); se o fill real for pior, as variantes maker ficam ainda mais negativas, porque os fills perdidos tendem a ser os que teriam dado lucro.
- Pulados por capacidade (máximo 3 posições): de 17 mil (S2 5m) a 1,1 milhão (S1 1m). A carteira só entra para drawdown e capacidade; a métrica principal, expectativa por trade, não depende disso.

### Drawdown e carteira

O pré-registro fixa risco de 1% da equity inicial por trade, sem compounding. Com expectativa de −0,13% a −0,23% por trade e dezenas a centenas de milhares de trades, toda variante leva a conta a zero e segue abaixo: o drawdown máximo reportado em `tables.md` vai de 4.965% (s2_1m_t) a 255.020% (s1_1m_t) da equity inicial. Em qualquer sizing real isso é ruína; o número exato não tem leitura além dessa.

### Por ano-calendário (expectativa líquida, WF)

| variante | 2022-H2 | 2023 | 2024 | 2025 | anos positivos |
|---|---|---|---|---|---|
| s1_1m_t | −0,222% | −0,224% | −0,220% | −0,220% | 0/4 |
| s1_1m_m | −0,164% | −0,166% | −0,168% | −0,168% | 0/4 |
| s1_5m_t | −0,227% | −0,222% | −0,210% | −0,219% | 0/4 |
| s1_5m_m | −0,166% | −0,166% | −0,156% | −0,167% | 0/4 |
| s2_1m_t | −0,249% | −0,218% | −0,227% | −0,242% | 0/4 |
| s2_5m_t | −0,267% | −0,217% | −0,234% | −0,229% | 0/4 |
| s3_1m_m | −0,142% | −0,148% | −0,143% | −0,147% | 0/4 |
| s3_1m_t | −0,222% | −0,233% | −0,224% | −0,227% | 0/4 |
| s3_5m_m | −0,116% | −0,120% | −0,135% | −0,131% | 0/4 |
| s3_5m_t | −0,201% | −0,204% | −0,222% | −0,215% | 0/4 |

A estabilidade ano a ano (variação de centésimos) é a assinatura de um sinal sem informação: o resultado é o custo, e o custo é constante.

## Holdout (2026-01-01 → 2026-10-01)

Uma rodada por variante, com a célula que a regra do pré-registro escolhe no treino 2025-07 → 2026-01, fixa no período inteiro. Inclui os 5 pares novos de 2026.

| variante | célula | trades | win% | exp líq/trade (IC95%) | exp bruta sem custo | fee + slippage |
|---|---|---|---|---|---|---|
| s1_1m_t | R 1,0, k 1,5 | 161.721 | 26,3% | **−0,222%** (−0,224; −0,219) | +0,001% | 0,100% + 0,122% |
| s1_1m_m | R 1,0, k 1,0 | 217.165 | 26,4% | **−0,171%** (−0,173; −0,170) | −0,014% | 0,070% + 0,087% |
| s1_5m_t | R 1,0, k 1,5 | 31.856 | 43,3% | **−0,215%** (−0,224; −0,206) | +0,000% | 0,100% + 0,115% |
| s1_5m_m | R 1,0, k 1,0 | 51.133 | 43,9% | **−0,167%** (−0,172; −0,162) | −0,013% | 0,070% + 0,083% |
| s2_1m_t | R 1,0, W 30 | 3.236 | 34,4% | **−0,231%** (−0,272; −0,189) | −0,008% | 0,100% + 0,123% |
| s2_5m_t | R 2,0, W 30 | 3.022 | 29,8% | **−0,212%** (−0,267; −0,155) | +0,013% | 0,100% + 0,125% |
| s3_1m_m | σ 2,0, SL ATR | 70.762 | 34,3% | **−0,143%** (−0,148; −0,139) | +0,001% | 0,060% + 0,085% |
| s3_1m_t | σ 2,5, SL ATR | 60.763 | 24,6% | **−0,224%** (−0,229; −0,219) | +0,010% | 0,100% + 0,134% |
| s3_5m_m | σ 2,5, SL ATR | 11.071 | 34,3% | **−0,146%** (−0,173; −0,118) | −0,000% | 0,061% + 0,085% |
| s3_5m_t | σ 2,5, SL ATR | 12.385 | 31,9% | **−0,217%** (−0,241; −0,193) | +0,010% | 0,100% + 0,127% |

O holdout repete o walk-forward quase ao centésimo. 2026 não é diferente de 2022 a 2025 para estas regras.

## Robustez: grid fixo no período OOS

Cada célula do grid rodou fixa em 2022-07 → 2026-01. Resumo (tabela completa por célula, com IC e anos, em `tables.md`):

| variante | células | células com exp líq > 0 | faixa de exp líq | faixa de exp bruta (c/ slippage) |
|---|---|---|---|---|
| s1_1m_t | 6 | 0 | −0,234% a −0,222% | −0,134% a −0,122% |
| s1_1m_m | 6 | 0 | −0,180% a −0,167% | −0,110% a −0,097% |
| s1_5m_t | 6 | 0 | −0,231% a −0,218% | −0,131% a −0,118% |
| s1_5m_m | 6 | 0 | −0,178% a −0,165% | −0,108% a −0,095% |
| s2_1m_t | 6 | 0 | −0,244% a −0,220% | −0,144% a −0,120% |
| s2_5m_t | 6 | 0 | −0,261% a −0,219% | −0,161% a −0,119% |
| s3_1m_m | 4 | 0 | −0,170% a −0,145% | −0,105% a −0,085% |
| s3_1m_t | 4 | 0 | −0,238% a −0,226% | −0,138% a −0,126% |
| s3_5m_m | 4 | 0 | −0,152% a −0,127% | −0,087% a −0,067% |
| s3_5m_t | 4 | 0 | −0,214% a −0,209% | −0,115% a −0,109% |

0 de 52 células positivas; 0 de 208 células-ano positivas. O walk-forward escolheu quase sempre a célula de menor alvo (R = 1; σ = 2; SL por ATR), que é a que perde menos porque perde menos vezes no stop, não porque ganhe.

## O piso de custo: quanto edge bruto um scalp precisa

Custos do pré-registro (os que o bot mediu em produção): taker 0,05% por lado, maker 0,02%, slippage 0,03% por perna taker, mais 0,10% quando a saída é o stop (STOP_MARKET escorrega), maker sem slippage.

| configuração | ida e volta normal | ida e volta saindo no stop |
|---|---|---|
| taker + taker (S1 taker, S2, S3 taker) | 0,10% fee + 0,06% slippage = **0,16%** | **0,26%** |
| maker + taker (S1 maker) | 0,07% fee + 0,03% slippage = **0,10%** | **0,20%** |
| maker + maker (S3 maker, TP limit) | 0,04% fee = **0,04%** | **0,20%** (stop é sempre taker) |

Como 52 a 73% dos trades saem no stop, o custo médio realizado fica acima do caso normal: 0,142% (S3 maker 5m) a 0,233% (S3 taker 1m) por trade, coluna "fee + slippage" acima. É esse o edge bruto mínimo que um scalp com estas taxas de stop precisa gerar só para empatar. Em 1m, o ganho médio quando acerta é de 0,14 a 0,15% (S1): o custo de um trade vale mais que um trade vencedor inteiro, e a fee sozinha já é 49 a 67% do ganho médio. Em 5m e no ORB o alvo é maior (0,35 a 0,93%), o custo cai para 6 a 25% do ganho médio, mas a expectativa bruta continua em zero. Mesmo com fee zero e slippage zero, nenhuma das 10 variantes teria expectativa acima de +0,015% por trade, e 6 delas seriam negativas.

Em outras palavras: para scalping neste mercado com fees de varejo, o problema não é a execução. É que os sinais populares não preveem o próximo movimento nem o suficiente para pagar a si mesmos.

## Ressalvas de múltiplos testes

- Esta rodada: 10 variantes e 52 células (S1 24, S2 12, S3 16), todas pré-registradas. Nenhuma rodada extra, nenhuma regra ajustada depois de ver resultado.
- Acumulado com as rodadas anteriores (`EXPERIMENTOS.md`, 3m; `TREND4H_RELATORIO.md`, 4h): 24 variantes e 105 células testadas neste repositório, nenhuma passou. O trend 4h chegou perto (bruta positiva, IC cruzando zero); o scalping nem isso.
- Com 52 células, por acaso, alguma positiva seria esperada se a expectativa verdadeira fosse zero e as células independentes. Zero positivas em 52 (e zero em 208 células-ano) indica expectativa líquida verdadeira negativa, não falta de sorte.
- Limites do dado: klines agregam o que houve dentro da barra; a ordem intrabar adotada (adverso antes do favorável) é conservadora e penaliza barras que tocam SL e TP. Isso explica parte da taxa alta de stop em 1m, mas não a expectativa bruta nula: a bruta também é ≈ 0 em 5m e no ORB, onde esse efeito é menor.
- O slippage de 0,03% é a média medida pelo bot em pares líquidos, não o pior caso; em pares como LIGHT e PIPPIN seria maior, e o resultado real seria pior que o reportado.
- O fill maker por trade-through é otimista (ver acima). Vale na direção de deixar o resultado maker melhor do que seria ao vivo.

## O que isto diz para o bot

- Não há técnica de scalping aqui que mereça forward test. Nem com entrada maker.
- Qualquer tentativa futura em 1m ou 5m precisa primeiro mostrar expectativa bruta positiva e maior que o piso de custo da sua configuração (tabela acima), antes de qualquer discussão de execução.
- O forward test Donchian 4h continua sendo a única linha com expectativa bruta positiva no repositório; nada deste relatório muda o que está rodando.

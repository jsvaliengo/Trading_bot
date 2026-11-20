# Experimentos de melhoria da estratégia (backtest_v2)

Data: 2026-10-24. Pesquisa apenas. Nada aqui altera o código de produção (`trading_bot/`), a VM ou o deploy. O bot é só importado, para reusar as funções de saída.

## Veredito

**Nenhuma variante mostrou edge out-of-sample.** Testei 12 variantes (37 células de grid no total). Todas as 10 variantes do pullback 3m ficaram com IC95% inteiro abaixo de zero e negativas nos três anos. A melhor delas (entrada maker + time exit + filtro de volume) chegou a −0,116%/trade. Ou seja, perde menos, mas continua perdendo. O único resultado médio positivo é o Donchian 4h (+0,092%/trade), e ele não passa no critério: o IC vai de −0,51% a +0,78%, o resultado depende de 2024 (+0,75%) e a carteira termina em −52%.

O motivo é o mesmo do RELATORIO.md. A expectativa **bruta** (antes de fee, slippage e funding) do sinal pullback é negativa em todas as variantes, de −0,04% a −0,07% por trade. Filtro e saída mexem na distribuição, mas nenhum transforma o sinal em positivo antes dos custos.

## Resultados OOS (walk-forward 6m treino / 3m teste, 10 folds, OOS 2024-04-01 → 2026-10-01, 35 pares)

| variante | trades | win% | ganho/perda méd | exp líquida (IC95% bloco-dia) | exp bruta | fee/trade | funding/trade | DD máx | retorno | 2024 / 2025 / 2026 | LONG / SHORT (n) | edge? |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| referência: SL/RR fixos (harness original) | 11420 | 28.8% | +3.61% / −1.73% | **−0.194%** (−0.257; −0.129) | −0.090% | 0.100% | −0.004% | 100% | −100% | −0.17 / −0.22 / −0.19 | −0.26 (5599) / −0.13 (5821) | não |
| **baseline: saídas do bot** | 39328 | 60.2% | +0.59% / −1.30% | **−0.163%** (−0.179; −0.147) | −0.062% | 0.100% | −0.001% | 100% | −100% | −0.14 / −0.17 / −0.18 | −0.18 (18845) / −0.14 (20483) | não |
| H1 time exit | 47769 | 56.7% | +0.58% / −1.14% | **−0.166%** (−0.181; −0.151) | −0.065% | 0.100% | −0.001% | 100% | −100% | −0.14 / −0.17 / −0.18 | −0.18 / −0.15 | não |
| H2 entrada maker | 32705 | 60.2% | +0.65% / −1.28% | **−0.119%** (−0.137; −0.101) | −0.048% | 0.070% | −0.001% | 100% | −100% | −0.09 / −0.13 / −0.14 | −0.14 / −0.10 | não |
| H3 tendência 4h | 30730 | 59.8% | +0.59% / −1.30% | **−0.165%** (−0.185; −0.146) | −0.064% | 0.100% | −0.001% | 100% | −100% | −0.14 / −0.17 / −0.18 | −0.19 / −0.15 | não |
| H3 ADX 1h | 20218 | 59.7% | +0.63% / −1.31% | **−0.151%** (−0.174; −0.128) | −0.050% | 0.100% | −0.001% | 100% | −100% | −0.12 / −0.16 / −0.17 | −0.17 / −0.13 | não |
| H3 volume | 12624 | 62.7% | +0.51% / −1.28% | **−0.157%** (−0.180; −0.134) | −0.056% | 0.100% | −0.001% | 100% | −100% | −0.14 / −0.17 / −0.16 | −0.16 / −0.15 | não |
| H3 skip N piores pares | 36373 | 60.3% | +0.57% / −1.29% | **−0.166%** (−0.183; −0.149) | −0.065% | 0.100% | −0.001% | 100% | −100% | −0.15 / −0.17 / −0.19 | −0.19 / −0.14 | não |
| H3 volume + ADX | 4448 | 62.8% | +0.55% / −1.30% | **−0.138%** (−0.178; −0.099) | −0.037% | 0.100% | −0.001% | 98.6% | −98.5% | −0.11 / −0.16 / −0.14 | −0.14 / −0.14 | não |
| H1 + H2 + H3 (volume) | 6542 | 60.1% | +0.59% / −1.19% | **−0.116%** (−0.146; −0.085) | −0.045% | 0.070% | −0.000% | 99.4% | −99.3% | −0.10 / −0.12 / −0.12 | −0.12 / −0.11 | não |
| H4 Donchian 1h (20/10) | 5162 | 32.5% | +4.41% / −2.55% | **−0.292%** (−0.483; −0.092) | −0.184% | 0.100% | −0.008% | 97.8% | −97.1% | −0.28 / −0.32 / −0.27 | −0.36 / −0.22 | não |
| H4 Donchian 4h (20/10) | 1428 | 32.6% | +8.41% / −3.92% | **+0.092%** (−0.512; +0.775) | +0.228% | 0.100% | −0.036% | 84.4% | −52.1% | +0.75 / −0.42 / +0.31 | +0.05 / +0.14 | não |

- **exp** é o retorno líquido médio por trade sobre o notional, depois de fee, slippage e funding. Os anos e lados também estão em %/trade (o ano é o da saída).
- O **IC95% bloco-dia** reamostra dias inteiros, não trades soltos (10.000 reamostras, seed fixa), porque trades do mesmo dia em alts correlacionadas não são independentes. Ele fica mais largo que o IID (no SL/RR fixo: −0,257; −0,129 contra −0,238; −0,149).
- **Critério de edge:** IC inferior > 0, ≥ 200 trades OOS e positivo em pelo menos 2 dos 3 anos. Nenhuma variante passa.
- **Retorno e DD** vêm de uma carteira de $1.000 com 1% de risco até o SL (×1,5 de buffer de slippage, como o `SLIPPAGE_BUFFER_MULT` do bot), no máximo 6 posições e notional ≤ 1x por posição. Com expectativa negativa e dezenas de milhares de trades, a carteira vai a zero. Use os percentuais por trade.
- **Teste múltiplo:** foram 12 variantes e 37 células de grid. Com tantas tentativas, um IC positivo isolado por acaso seria esperado, e mesmo assim nenhum apareceu.

## Passo 1: baseline com as saídas do bot

As saídas são as do bot ao vivo, e o simulador chama as funções reais em vez de reimplementá-las:
- **SL/TP:** `HedgeStrategy.calculate_stop_loss_take_profit` com o `risk_profile` do `trend_strong` em `config.py` (SL 1,0–1,5%, RR 3, TP ≤ 4,5%), ATR de `TechnicalAnalysis.calculate_atr` nas 15 últimas barras de 3m e stop estrutural (`STRUCTURAL_STOP_ENABLED=True` por padrão, mínimo/máximo das 10 últimas barras). Os níveis são calculados sobre o close do sinal e re-ancorados no fill com o `_reanchor` de `execution/engine.py`, como o bot faz. Na prática o SL ficou com mediana de 1,00% (p10–p90 = 0,91–1,26%). O fundo estrutural no 3m quase sempre está a menos de 1%, então vale o piso. A dispersão em torno de 1% vem do `_round_price`, que guarda só 3 casas decimais para preços entre $0,10 e $1 (≈0,6% de degrau no DOGE a $0,16). Por isso há 108 trades com SL acima do teto de 2,5% (CFX, CRV, NOT, THETA, DOT...). É comportamento real do bot (o `execution/engine.py` envia esse mesmo preço arredondado), e vale corrigir à parte.
- **Trailing:** `TechnicalAnalysis.compute_atr_based_trailing` (activation clamp(2×ATR%, 0,50–3,50), distance clamp(0,5×ATR%, 0,40–2,50)) e o preço do stop vem de `TradingBot._trailing_stop_price`, com o piso de breakeven fee×2 + 0,05% + 0,10% de cushion (= +0,25% da entrada).
- **Ordem intrabar conservadora:** o extremo adverso vem antes do favorável (LONG: O→L→H→C; SHORT: O→H→L→C). Isso generaliza o "SL primeiro" do motor original e deixa o trailing disparar quando a barra devolve o pico. Não há 1m no cache, então a simulação é sobre barras de 3m.
- **Custos:** taker de 0,05% por perna, 0,03% de slippage na entrada e na saída, +0,10% no SL (STOP_MARKET) e funding real. A saída por trailing paga só 0,03%, porque o bot fecha a mercado com checagem a cada 10s.

O resultado é **−0,163%/trade**, com win 60,2%, ganho médio +0,59% e perda média −1,30%. A simulação anterior deu −0,18% (61%, +0,51%, −1,29%) e o testnet deu win 63%, +0,72%, −1,27%. Ficou perto. O formato bate (muitos ganhos pequenos de trailing e perdas cheias de SL), e a pequena diferença vem de detalhes de slippage do trailing. Há 39 mil trades OOS contra 11 mil do SL/RR fixo porque o trailing fecha rápido e libera vaga no teto de 6 posições.

## Passo 2: o que cada hipótese mostrou

**H1, time exit** (fechar se MFE < X% após N barras; grid X ∈ {0,2; 0,3} × N ∈ {10, 20, 40}). O treino escolheu (N=40, X=0,2) em 10/10 folds, o corte mais frouxo. **Não ajuda: −0,166% contra −0,163%.** Cortou 3.879 trades (8%), e **1.797 deles (46%) teriam sido vencedores** sem o corte. O delta médio por trade cortado ficou em +0,003%, zero na prática. O "perdedor que morre na entrada" existe (MFE mediano 0,18% no testnet), mas, quando o trade já ficou 2 h sem andar, sair a mercado ou esperar o SL dá no mesmo em média.

**H2, entrada maker** (limit no close do sinal com offset a favor ∈ {0; 0,05; 0,10}%, válida K ∈ {1, 3} barras; só preenche se o preço atravessar o limite em 1 bp; maker 0,02% na entrada; depois do fill o caminho intrabar segue até o extremo adverso). O treino escolheu offset 0,10% e K=1 em 10/10 folds. **É a maior melhora isolada (−0,163 → −0,119), mas continua negativa.** No nível de sinal, sem o teto do portfólio:
| offset / K | fill | exp preenchidos (maker) | mesmos sinais via taker | não preenchidos (taker) |
|---|---|---|---|---|
| 0 / 1 | 87% | −0,119% | −0,183% | +0,013% |
| 0,05 / 3 | 79% | −0,117% | −0,233% | +0,116% |
| 0,10 / 1 | 46% | −0,118% | −0,286% | −0,050% |
| 0,10 / 3 | 65% | −0,117% | −0,282% | +0,075% |

A **seleção adversa** é clara. Os sinais que preenchem são os piores (−0,29% via taker), e os que fogem sem preencher são os melhores. O maker recupera ~0,17% por trade (preço melhor + fee + slippage), mas só nos sinais ruins. O modelo de fill não tem fila nem latência, então o ganho real do maker tende a ser **menor** que este.

**H3, filtros de entrada** (um de cada vez): tendência 4h EMA50/200 a favor (−0,165%, nada), ADX 1h ≥ {20, 25, 30} (escolheu 30: −0,151%), volume ≥ {1,2; 1,5; 2,0}× a média de 20 (escolheu 2,0: −0,157%) e pular os N ∈ {3, 5, 8} piores pares do treino (escolheu 8: −0,166%). **Nenhum filtro mexe mais que 0,015%.** O treino sempre escolheu a célula mais restritiva do grid, o que é sinal de que só está cortando volume de trades perdedores. A lista de "piores pares" muda inteira de fold para fold (ARB/CRV/ETC/FIL/... → SHIB/1INCH/APT/CHZ/... → SHIB/AAVE/APE/BCH/...): não existe par ruim persistente, é ruído. A combinação dos 2 melhores no treino (volume + ADX, escolhidos pelo retorno médio de treino, não pelo OOS) dá −0,138% com só 4.448 trades.

**H1 + H2 + H3 (volume)**, com as células escolhidas por fold em cada componente: **−0,116%** (−0,146; −0,085). Os ganhos não somam além do que o maker já dá sozinho. O filtro corta trades e o time exit não faz diferença.

**H4, estrutural** (Donchian 20 barras com saída pelo canal oposto de 10, os dois lados, SL % fixo do grid: 1h {2, 3, 5}%, 4h {3, 5, 8}%). No 1h fica claramente negativo (−0,29%). No 4h a média é positiva (+0,09%, bruta +0,23%), mas: o IC é enorme e cruza zero; 2025 deu −0,42%; o resultado depende de 2024; a escolha do SL oscilou entre 3% e 8% nos folds; e a carteira perdeu 52% com DD de 84%, porque os folds com SL 3% (notional maior) perderam. É o único lugar em que a expectativa **bruta** saiu positiva. Isso merece um teste próprio e mais cuidadoso, mas não é evidência de edge. Simplifiquei o "ATR stop" para um SL % fixo por TF, para usar o motor padrão sem código novo.

## Recomendação

1. **Não religar o pullback 3m** em nenhuma das formas testadas. As saídas do bot já tiram o que dá da entrada (−0,06% bruto), e nenhum filtro ou saída simples muda o sinal. Dá para reduzir o custo (maker), mas não o bastante.
2. Se for continuar, o próximo passo de pesquisa é **trend-following em TF alto** (4h/1d), testado com rigor próprio: mais anos de dados (o OOS de 2,5 anos dá só ~1.400 trades no 4h), universo sem survivorship, sizing por volatilidade e um único grid pequeno decidido antes de rodar. Para isso valer, o IC inferior precisa sair de −0,5% para > 0, e isso não é ajuste fino.
3. A entrada maker é uma redução de custo válida para **qualquer** estratégia futura (−0,03% de fee mais o slippage evitado), mas é preciso modelar fila e latência antes de confiar no número.

## Ressalvas

- O backtest usa barras de **3m** (não há 1m no cache) com ordem intrabar pessimista. Isso pode subestimar um pouco o trailing, mas não explica uma expectativa bruta negativa.
- O bot ao vivo avalia o candle em formação; aqui só entram barras fechadas (igual ao RELATORIO.md).
- Ficaram de fora gate de IA, kill switch, cooldown, regime swap e rotação por score.
- O `.env` da VM não foi lido. Os parâmetros são os defaults do `config.py` (stop estrutural ligado, `SLIPPAGE_BUFFER_MULT=1.5`, trailing ATR ligado).
- O universo são as 35 moedas de hoje (survivorship), o que *favorece* o resultado.
- Retorno de treino: todas as células ficaram negativas em todos os folds (de −90% a −100%). O "melhor" no treino é sempre o "menos ruim".
- **Compute:** o gargalo era `Bars.close_time`, uma property que recalculava o array inteiro a cada candidato do portfólio. Virou `cached_property`, e uma rodada de portfólio caiu de 4,9 s para 0,04 s (o SL/RR fixo reproduz exatamente os −0,194% do relatório anterior). Com isso, tudo rodou nos **3 anos completos**, sem reduzir janela, em poucos minutos com 10 workers.

## Como rodar

```bash
PYTHONPATH=. .venv/bin/python -m scripts.backtest_v2.run experiments --workers 10
#   grupos: --only fixed_sl_rr,main,H4,report   (cada grupo grava experiments_<grupo>.json)
.venv/bin/python -m pytest tests/test_backtest_v2_engine.py -q
```

Saídas em `runtime/backtest_v2/out/experiments/`: `experiments_*.json` (métricas, folds, grid de treino, por ano/lado/símbolo), `experiments.md` (tabela) e `oos_trades_<variante>.csv`.

Código: `engine.py` (`simulate_live_trade`, `TimeExit`, `MakerEntry`, `maker_fill`, hook `simulate=` e `sl_buffer` em `run_portfolio`), `experiments.py` (tabela de desfechos por sinal, filtros, walk-forward, bootstrap por bloco-dia) e `strategies.py` (`DonchianBreakout`). Testes novos em `tests/test_backtest_v2_engine.py`.

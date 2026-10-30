# Pré-registro: cash-and-carry de funding (spot long + perp USDT-M short, Binance)

Data: 2026-10-30. Escrito e commitado ANTES de rodar qualquer simulação. Regras, grid, custos, período, universo e critérios abaixo não mudam depois de ver resultado. Bug que invalide resultado pode ser corrigido, mas a correção é declarada no relatório e tudo é rodado de novo.

Pesquisa apenas. Nada aqui entra no bot, na VM ou no deploy. O forward test Donchian 4h não é tocado.

## Pergunta e hipóteses

Pergunta: comprar o ativo no spot e vender o mesmo notional no perpétuo USDT-M (posição delta-neutra) para receber o funding a cada 8h rende, líquido de taxas, basis e risco de liquidação da perna short, algo que valha a pena com $300 (e com $3.000) na Binance em 2022→2026?

- H0: o funding médio líquido, depois de taxas de giro e do descasamento spot/perp, fica perto de zero ou abaixo de "USDT parado"; o retorno é dominado por poucos episódios de funding alto e o drawdown do descasamento (basis) + rebalances come o carry.
- H1: existe variante com retorno líquido > 0 no período completo e no holdout 2026, drawdown < 10% e Sharpe > 1.

## Dados

- Funding real por evento: `runtime/backtest_v2/cache/vision/funding/<SYM>.npz` (já em cache, `data.binance.vision`, 316 símbolos). Alguns pares passaram a ter funding a cada 4h (ou 1h); o simulador soma todos os eventos dentro de cada passo de 8h, então o intervalo real não importa.
- Perp: klines 4h já em cache (`cache/vision/klines_4h/`). Spot: klines 4h baixadas de `data.binance.vision` (`/data/spot/monthly/klines/<SYM>/4h/`) só para o universo abaixo, em `cache/vision_raw/spot_klines/` + `cache/vision/spot_klines_4h/` (não versionado). Pares "1000X" no perp mapeiam para `XUSDT` no spot com preço ×1000 (1000PEPE→PEPE, 1000BONK→BONK); 1000SATS existe com esse nome no spot.
- Grade: passos de 8h nos instantes de funding (00:00, 08:00, 16:00 UTC). Preço do passo = close da barra 4h que fecha no instante (perp e spot). Máxima do perp no passo = máx das duas barras 4h (para liquidação). Passo sem barra em uma das pernas: símbolo indisponível nesse passo (posição aberta é fechada no último preço disponível).
- Período completo: 2022-01-01 → 2026-10-01. Holdout: 2026-01-01 → 2026-10-01, conta nova em 2026-01-01 (aquecimento de `k` passos com dados de dezembro/2025).
- Universo (17): fixos BTC, ETH, SOL, ADA, BNB, XRP, DOGE + os 10 de maior funding anualizado médio em 2022-01→2025-12 entre os símbolos com ≥ 365 dias de funding na janela e par spot em `data.binance.vision`: BIGTIME, WIF, 1000SATS, ORDI, AGIX, 1000BONK, RNDR, 1000PEPE, CFX, JTO. Seleção feita ANTES de qualquer simulação, mas usa o funding do próprio período: os resultados do período completo são teto otimista (viés de seleção declarado); o holdout 2026 não tem esse viés. AGIX (deslistado 2024) e RNDR (renomeado RENDER 2024) ficam: o símbolo opera só enquanto spot E perp têm barra. Sem reconstrução de deslistagens futuras além disso.

## Posição e margem

- Capital `C` por posição (slot). Alavancagem nominal do perp `L ∈ {1, 2}`: spot compra `C·L/(L+1)` em $, perp recebe margem `C/(L+1)` e vende a MESMA quantidade `q` do spot (notional ≈ igual). `L = 1` → $150 + $150; `L = 2` → $200 + $100. Margem isolada.
- Marcação: equity = `q·S_t` (spot) + `M + q·(P_entrada − P_t)` (perp) + caixa. O descasamento spot/perp (basis) entra integralmente na equity e no drawdown.
- Funding: a cada passo `t`, posição aberta ANTES de `t` recebe `rate_t · q · P_t` (short recebe se `rate > 0`, paga se `< 0`). Entrar em `t` não recebe o funding de `t`.
- Manutenção: 1,0% do notional para todos (BTC/ETH são 0,4% no tier 1; usar 1% é conservador). Liquidação da perna short: se em algum momento do passo `M + q·(P_entrada − P_max) ≤ 0,01·q·P_max`, a margem `M` é perdida inteira (aproximação: a Binance fica com a manutenção como taxa de liquidação), o spot é vendido no close do passo (taker) e a posição é contada como liquidação. A estratégia pode reabrir no passo seguinte.
- Rebalance: no close de cada passo, se o patrimônio do perp `E = M + q·(P_e − P_t)` cair abaixo de metade do alvo (`E < q·P_t/(2L)`), vende-se a fração `f` do spot e recompra-se a mesma fração do perp, transferindo o caixa para a margem, com `f` tal que a margem volte ao alvo `1/L`: `f = (q·P_t − L·E) / (q·(L·S_t + P_t))`. Se `f ≥ 1`, a posição fecha inteira. Fees das duas pernas sobre a fração. Rebalance para o outro lado (preço caindo, margem sobrando) não é feito: a posição só encolhe, nunca cresce (declarado; só reduz o yield).
- Taxas (duas tabelas, tudo roda nas duas): A taker 0,10% spot / 0,05% perp; B maker 0,10% spot / 0,02% perp. Cobradas sobre o notional de cada perna na entrada, na saída, no rebalance e na liquidação (spot). Spread não modelado além do que está no close das duas pernas (declarado).

## Variantes

- V0 "sempre dentro": por símbolo, posição aberta no primeiro passo com dado e mantida até o fim (ou até liquidação, reabrindo no passo seguinte). `L = 1` e `L = 2`.
- V1 limiar: por símbolo. Sinal = média dos últimos `k` passos de funding × 1095 (anualizado). Entra quando sinal > `θ`; sai quando sinal < `θ/2` (o que inclui negativo). Grid `k ∈ {3, 9}` × `θ ∈ {5%, 10%, 20%}` a.a., `L = 1`. Célula padrão: `k = 9`, `θ = 10%`. Sensibilidade: célula padrão com `L = 2`.
- V2 rotação: carteira com `n ∈ {1, 3}` slots de `C/n` cada; a cada instante de rebalance (a cada 8h, ou só às 00:00 UTC = diário) ordena o universo pelo sinal `k = 9`; símbolo em carteira fica enquanto está no top-`n` e sinal > 0; vaga é preenchida pelo melhor fora da carteira com sinal > 0 (senão fica em caixa a 0%). `L = 1`. Células: `{1, 3} × {8h, diário}`. Célula padrão: top-3 diário.

## Métricas (por variante × célula × tabela de taxas × período)

Retorno líquido ($ e % de `C`, anualizado como CAGR), Sharpe (retornos por passo de 8h da equity, × √1095, sem taxa livre de risco), drawdown máximo da equity (inclui basis), custo total de giro ($ em taxas), número de entradas/saídas, rebalances, liquidações, pior mês-calendário, tempo em posição (%), decomposição funding recebido vs basis vs taxas, resultado em $ para `C = 300` e `C = 3.000` (×10 exato: nenhum piso de notional é modelado, declarado). Benchmarks no mesmo período: BTC spot comprado no primeiro passo com 0,10% de taxa; USDT parado a 0%.

## Critérios de sucesso (todos, por célula)

1. Retorno líquido > 0 no período completo E no holdout 2026 (tabela A, taker).
2. Drawdown máximo < 10% no período completo.
3. Sharpe > 1 no período completo.

Veredito por variante: V0 e V1 (por símbolo): PASS se BTC (o par que alguém com $300 operaria) passa nos três critérios na célula padrão E pelo menos 9 dos 17 símbolos passam. V2: PASS se a célula padrão (top-3 diário) passa nos três. Qualquer outra coisa é FAIL. A tabela B (maker) é sensibilidade, fora do veredito.

## Viabilidade com $300 (nota obrigatória no relatório)

Notional mínimo spot $5 por ordem; perp $5 na maioria dos pares e $100 em BTCUSDT; precisa de saldo em spot E em futures ao mesmo tempo (duas carteiras, transferência manual entre elas no rebalance); o bot hoje só opera futures. Top-3 com $300 = $50 spot + $50 perp por símbolo: cabe em alts, não em BTC perp.

## Limites declarados

- Close de 4h como preço de execução das duas pernas no mesmo instante; na prática as duas ordens não preenchem no mesmo milissegundo e o basis instantâneo pode ser pior.
- Liquidação checada com a máxima de 4h do perp (não tick a tick); rebalance só a cada 8h. Spike intrapasso que liquidaria antes do rebalance é capturado pela máxima; spike que liquida e volta dentro da barra também conta como liquidação (correto).
- Sem juros sobre caixa ocioso, sem rendimento do spot (Earn), sem spread, sem piso de notional, sem limite de posição.
- Viés de seleção do universo (10 de maior funding do próprio período): declarado acima.

# Market making simples de varejo em perpétuos USDT-M: relatório

Data: 2026-10-30. Pré-registro: `MM_PREREG.md` (commit `b94c777`, antes de baixar dado). Código: `market_making.py`, testes `tests/test_backtest_v2_mm.py`. Saída completa (96 combinações, por símbolo e por dia) em `runtime/backtest_v2/out/market_making/{results.json,tables.md}` (não versionado); a rodada v1 pré-correção em `.../market_making_v1/`.

Pesquisa apenas. Nada entra no bot, na VM ou no deploy.

## Veredito

**FAIL nas 16 células.** Nenhuma célula (spread 1/2/5/10 bps × requote 1/5 s × $10/$50) passa os critérios 1 e 3 (P&L líquido > 0 e Sharpe > 1 nos 12 dias em amostra) no modelo fim de fila com maker 0,02%. O critério 2 (holdout > 0) é satisfeito só nas 4 células de 10 bps, e inteiramente por SNXUSDT em fevereiro de 2026 (ver abaixo), onde o modelo é menos confiável; nos outros 7 símbolos o holdout é negativo em todas as células.

O resultado replica o da pesquisa de scalping por outro caminho: **o bruto é ≈ 0 ou negativo antes de qualquer fee**, porque só somos preenchidos quando o preço atravessa a nossa cotação e, em média, ele não volta (markout vs mid de −2 a −10 bps a 10 s, igual à meia-distância cotada). O que sobra, a fee maker consome. Nem o teto (preço igual preenche + maker 0%) fica positivo em amostra em célula alguma.

| Célula (fim de fila, maker 0,02%, $50/ordem) | P&L/dia em amostra | P&L/dia holdout | Sharpe ann IS | $/1k preenchido | markout 10 s vs fill | vs mid |
|---|---|---|---|---|---|---|
| 1 bps, 1 s | −$2.778 | −$2.879 | −53,8 | −$0,41 | −1,3 bps | −2,6 bps |
| 2 bps, 1 s | −$1.448 | −$1.678 | −47,3 | −$0,42 | −1,2 bps | −3,6 bps |
| 5 bps, 1 s | −$291 | −$518 | −33,3 | −$0,37 | −0,7 bps | −6,0 bps |
| 10 bps, 1 s | −$53 | +$171 (SNX +$228/dia; outros 7: −$57/dia) | −17,5 | −$0,29 | +0,1 bps | −9,7 bps |
| 10 bps, 5 s | −$67 | +$97 (SNX +$186; outros: −$89) | −22,4 | −$0,29 | −0,4 bps | −10,1 bps |

Para uma conta de $300 os números em $ são nominais: a simulação não para na equity zero. A 1-2 bps a conta seria liquidada no primeiro dia (−$2.778/dia girando $6,7 M/dia de notional preenchido com $2.000 de notional bruto máximo). A leitura correta é por $1.000 preenchido: **−$0,29 a −$0,42 por $1.000 girado em todas as células**, com maker 0,02%.

## Dados e cobertura

- 8 símbolos × 16 dias (12 em amostra: 1/8/15/22 de mar-mai/2025; 4 holdout: 1/8/15/22 de fev/2026), todos os 128 arquivos diários de aggTrades existem e foram usados. 2,7 GB zipados; bins de 100 ms em cache.
- Trades/dia: BTC 0,37-3,2 M, ETH 0,5-4,4 M, SNX 10-57 k, CHZ e COMP na mesma ordem de SNX.
- Spread real (proxy: último trade no ask − último no bid, mediana): BTC/ETH ≈ 0-0,1 bps (1 tick); SNX 11-16 bps em 2025 e **24-34 bps em fev/2026** (preço caiu de $0,85 para $0,30 com tick fixo de 0,0001 = 3 bps).

### Mudanças depois do pré-registro (declaradas)

1. **Mid de referência.** O pré-registro definia mid = último trade impresso. Na primeira rodada (v1) isso quebrou nos pares ilíquidos: o último print alterna entre o bid e o ask reais (bounce), então qualquer cotação a até 10 bps dele ficava *dentro* do spread real e era "preenchida" por todo print alternado. Sintoma: SNX com exatamente 56.887 fills em s = 1, 2 e 5 bps. Correção: mid = (último trade com agressor vendedor + último com agressor comprador) / 2, usando a coluna `is_buyer_maker` dos aggTrades; o markout também é medido contra esse mid. Tudo foi rodado de novo; v1 fica em `out/market_making_v1/` e dá o mesmo veredito (FAIL nas 16, holdout negativo nas 16).
2. **Preço do flatten taker.** Pré-registro: último trade. Agora: vende no último preço do lado bid e compra no do lado ask (cruza o spread real). Consequência da mudança 1; conservador.
3. Nada mais mudou: grid, custos, período, critérios e regra de fila são os do pré-registro.

## Resultados em amostra: as 16 células, fim de fila, maker 0,02%

| s bps | R s | $/ordem | P&L IS $ | $/dia | Sharpe ann | fills | flattens | markout 1 s | markout 10 s | vs mid 10 s | $/1k | P&L HO $ | PASS |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | 10 | −9.259 | −772 | −48,9 | 1.610.281 | 33.349 | −0,8 | −1,1 | −2,3 | −0,37 | −3.292 | FAIL |
| 1 | 1 | 50 | −33.333 | −2.778 | −53,8 | 1.610.239 | 33.319 | −0,9 | −1,3 | −2,6 | −0,41 | −11.517 | FAIL |
| 1 | 5 | 10 | −5.390 | −449 | −66,7 | 935.901 | 15.375 | −1,0 | −1,2 | −2,4 | −0,37 | −1.863 | FAIL |
| 1 | 5 | 50 | −19.099 | −1.592 | −72,9 | 935.899 | 15.360 | −1,1 | −1,4 | −2,8 | −0,41 | −6.361 | FAIL |
| 2 | 1 | 10 | −4.362 | −363 | −43,3 | 829.513 | 14.392 | −0,7 | −1,1 | −3,4 | −0,38 | −1.731 | FAIL |
| 2 | 1 | 50 | −17.373 | −1.448 | −47,3 | 829.490 | 14.369 | −0,7 | −1,2 | −3,6 | −0,42 | −6.713 | FAIL |
| 2 | 5 | 10 | −3.306 | −275 | −53,3 | 634.901 | 7.604 | −0,9 | −1,2 | −3,4 | −0,36 | −1.186 | FAIL |
| 2 | 5 | 50 | −12.497 | −1.041 | −57,5 | 634.901 | 7.590 | −1,0 | −1,3 | −3,6 | −0,39 | −4.273 | FAIL |
| 5 | 1 | 10 | −787 | −66 | −33,0 | 189.663 | 1.765 | +0,1 | −0,8 | −5,9 | −0,36 | −459 | FAIL |
| 5 | 1 | 50 | −3.496 | −291 | −33,3 | 189.666 | 1.761 | +0,3 | −0,7 | −6,0 | −0,37 | −2.072 | FAIL |
| 5 | 5 | 10 | −917 | −76 | −34,4 | 222.367 | 896 | −0,5 | −0,9 | −5,9 | −0,33 | −373 | FAIL |
| 5 | 5 | 50 | −3.790 | −316 | −35,6 | 222.366 | 896 | −0,5 | −0,9 | −6,1 | −0,34 | −1.467 | FAIL |
| 10 | 1 | 10 | −144 | −12 | −19,1 | 43.750 | 144 | +1,2 | 0,0 | −9,7 | −0,30 | +133 | FAIL |
| 10 | 1 | 50 | −637 | −53 | −17,5 | 43.750 | 144 | +1,3 | +0,1 | −9,7 | −0,29 | +684 | FAIL |
| 10 | 5 | 10 | −186 | −16 | −22,6 | 55.428 | 81 | +0,1 | −0,4 | −10,0 | −0,29 | +63 | FAIL |
| 10 | 5 | 50 | −804 | −67 | −22,4 | 55.428 | 81 | +0,3 | −0,4 | −10,1 | −0,29 | +386 | FAIL |

Markouts em bps do notional preenchido. "vs fill" ≈ 0 a 10 bps quer dizer que o preço fica onde cruzou: nem continua muito, nem volta. "vs mid" ≈ −s quer dizer que todo fill custou a meia-distância inteira em seleção adversa: o spread que "capturamos" é exatamente o que o mercado andou contra nós para nos preencher. Os 12 dias em amostra são negativos em todas as 16 células (0 dias positivos de 12 nas células de 10 bps; o melhor dia é −$2,8).

## Seleção adversa por símbolo (10 bps, 1 s, $50, em amostra; P&L $ / markout 10 s bps / fills)

| BTC | ETH | SOL | ADA | DOGE | SNX | CHZ | COMP |
|---|---|---|---|---|---|---|---|
| −8,7 / −1,2 / 306 | −45,8 / −1,5 / 2.297 | −20,1 / −1,6 / 1.410 | −14,6 / −2,7 / 1.279 | −23,0 / −1,5 / 1.524 | −260,9 / +1,2 / 25.785 | −67,2 / −1,3 / 3.263 | −197,2 / −1,3 / 7.886 |

Os pares líquidos quase não preenchem a 10 bps (BTC: 306 fills em 12 dias) e, quando preenchem, é porque o preço andou ≥ 10 bps em 1 s e seguiu. Os ilíquidos preenchem muito mais (o spread real deles é da ordem da nossa cotação) e o giro paga fee.

## O teto: preço igual preenche e maker 0% / rebate −0,01%

| Modelo / fee | 10 bps 1 s $50: IS $/dia | HO $/dia | HO sem SNX $/dia | 1 bps 1 s $50: IS $/dia |
|---|---|---|---|---|
| fim de fila, maker 0,02% (principal) | −$53 | +$171 | −$57 | −$2.778 |
| fim de fila, rebate −0,01% | +$1,6 | +$361 | — | −$398 (v1) |
| fim de fila, maker 0% | −$17 | +$298 | −$32 | −$1.436 |
| preço igual, maker 0% (teto) | −$5,7 | +$392 | −$7 | −$1.482 |

Mesmo no teto (todo toque preenche, fee zero), as 16 células são negativas em amostra; a melhor é 10 bps / 1 s / $10 com −$1,90/dia. O único quadrante positivo em amostra é 10 bps com rebate VIP de −0,01% (+$0,15 a +$2,31/dia, Sharpe 1,1-1,9 anualizado, girando $180 k/dia): isso é ser pago para girar, não capturar spread, e exige volume de VIP que $300 não geram.

## O holdout positivo a 10 bps é SNX e não é confiável

Nas 4 células de 10 bps o holdout soma +$63 a +$684, mas SNXUSDT contribui +$743 a +$912 e os outros 7 símbolos somam −$228 a −$357. Em fev/2026 o SNX valia $0,30 com tick 0,0001 (3 bps) e spread real mediano de 24-34 bps; a nossa cotação a 10 bps fica dentro do spread real e todo print do lado oposto a "preenche" (markout +7 bps: o bounce). A simulação registra 10 k fills/dia de SNX num par com 10-16 k trades/dia: seríamos contraparte de mais da metade de todos os prints, sem mover o preço nem atrair concorrência. Isso não é um resultado de market making; é o modelo sem L2 atribuindo a nós o spread inteiro de um par morto. Em amostra (spread real 11-16 bps) o mesmo SNX perde $261.

## O que isto diz para o bot

- Nada a implementar. Market making de varejo com requote de 1-5 s e 200 ms de latência, sem book, não tem edge mensurável em perpétuos USDT-M: o bruto já é negativo em todas as células antes da fee.
- A conclusão é a mesma do scalping (`SCALPING_RELATORIO.md`): a microestrutura de 1 s a 1 min não paga o custo de operar, independente de a ordem ser maker ou taker.
- Se um dia houver book (L2 por websocket) e requote < 100 ms, isto teria de ser refeito do zero; os números daqui não extrapolam.

## O que ficou de fora

- Book (L2), fila, competição por nível e impacto das nossas ordens (SNX mostra o tamanho do erro que isso pode causar: centenas de dólares em 4 dias).
- Funding (inventário zerado por dia, mas até $250/símbolo de notional pode pegar 1-3 eventos de funding por dia).
- Rate limit, rejeição de ordem, cancelamento que chega tarde e preenche "fantasma", desconexão: todos contra nós.
- Volume mínimo por fill: assumimos que toda varrida leva a ordem inteira.
- Margem/liquidação: a simulação continua depois da equity zero. Nas células de 1-5 bps a conta de $300 não sobrevive ao primeiro dia.
- 16 dias amostrados em 4 meses: nenhum Sharpe aqui tem significância; o sinal do bruto, consistente em 16 de 16 dias × 16 células, tem.
- Latência de 200 ms só no mid de referência; a latência de cancelar e repor (duas viagens) não foi somada.

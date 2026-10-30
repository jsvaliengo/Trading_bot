# Pré-registro: market making simples de varejo (Binance USDT-M, aggTrades)

Data: 2026-10-30. Escrito e commitado ANTES de baixar ou processar qualquer dado. Regras, grid, custos, período e critérios não mudam depois de ver resultado. Bug que invalide resultado pode ser corrigido, mas a correção é declarada no relatório e tudo é rodado de novo.

Pesquisa apenas. Nada aqui entra no bot, na VM ou no deploy. O forward test Donchian 4h não é tocado.

## Pergunta e hipóteses

Pergunta: um market maker de varejo (uma ordem limit de cada lado do mid, requote a cada 1-5 s, de uma VM na OCI com ~200 ms até a Binance, conta de $300) captura spread líquido positivo em perpétuos USDT-M depois de fee maker e seleção adversa?

- H0: o spread capturado é consumido pela seleção adversa (o preço anda contra logo depois do fill) e pela fee maker; P&L líquido ≤ 0 nos pares líquidos e nos ilíquidos.
- H1: existe célula (spread × requote × tamanho) com P&L líquido > 0 no período e no holdout, na versão fim-de-fila e com maker 0,02%.

## Limitação central (declarada)

Não há book (L2) nem fila de ordens. Market making sem L2 só pode ser aproximado. A aproximação usa trades impressos (aggTrades) e dois modelos de fill:

- **Fim de fila (principal):** a ordem bid em `b` só preenche quando um trade imprime em preço **estritamente menor** que `b` (o mercado varreu o nível inteiro; posição na fila não importa). Ask espelhado (trade > `a`). É a aproximação pessimista-realista para varejo, que chega por último em cada nível.
- **Preço igual preenche (teto):** bid preenche quando um trade imprime em preço ≤ `b` (como se estivéssemos na frente da fila). Otimista; reportado só como teto.

Em ambos: o fill é sempre da ordem inteira (não checamos se o volume que cruzou era ≥ nosso tamanho; otimista, mas $10-50 é pequeno frente ao volume agregado por nível). Ordens de outros participantes no mesmo preço não competem conosco além do que o modelo de fila captura.

## Dados

- Fonte: `data.binance.vision/data/futures/um/daily/aggTrades/{SYM}/{SYM}-aggTrades-{YYYY-MM-DD}.zip`, lidos em memória (zipfile + numpy), nunca extraídos nem executados. Cache local não versionado em `runtime/backtest_v2/cache/vision_raw/aggTrades/` (zips) e `runtime/backtest_v2/cache/aggtrades_100ms/` (bins de 100 ms: low, high, último preço, nº de trades).
- Símbolos (8): BTCUSDT, ETHUSDT, SOLUSDT, ADAUSDT, DOGEUSDT (líquidos) + SNXUSDT, CHZUSDT, COMPUSDT (os 3 de menor arquivo diário de aggTrades, proxy de menor liquidez, dentro do universo do PDF de 35 moedas em `data.PDF_UNIVERSE_35`).
- Período em amostra: 2025-03, 2025-04, 2025-05, dias 1, 8, 15 e 22 de cada mês (12 dias). Holdout: 2026-02, dias 1, 8, 15 e 22 (4 dias). Dias amostrados em vez de meses inteiros porque um dia de aggTrades de BTC tem ~50 MB zipado; 16 dias × 8 símbolos ≈ 2,7 GB. Se um arquivo não existir, o dia é pulado para aquele símbolo e declarado.
- Cada dia é simulado de forma independente: começa flat, termina flat (flatten taker no último preço do dia). Os dias não são contíguos, então não há carry de inventário entre dias.
- Resolução: trades agregados em bins de 100 ms (low, high, último preço, contagem). Bin sem trade: low = +inf, high = −inf, último preço = forward fill.
- Tick size e notional mínimo da Binance (consultados em 2026-10-30 em `fapi/v1/exchangeInfo`): BTC 0,1 / $50; ETH 0,01 / $20; SOL 0,01 / $5; ADA 0,0001 / $5; DOGE 0,00001 / $5; SNX 0,0001 / $5; CHZ 0,00001 / $5; COMP 0,01 / $5. Bid arredondado para baixo e ask para cima no tick: a meia-distância efetiva nunca é menor que um tick.

## Estratégia simulada (por símbolo, independente; carteira = soma)

1. Requote a cada `R ∈ {1 s, 5 s}`, alinhado ao relógio. No instante `t` de requote, o mid de referência `m` é o último trade impresso até `t − 200 ms` (latência VM OCI → Binance: a decisão usa dado de 200 ms atrás; o ack da ordem está dobrado nessa mesma defasagem). As ordens ficam vivas de `t` até o próximo requote.
2. Preço de reserva (Avellaneda-Stoikov simplificado): `r = m × (1 − inv_ratio × s)`, onde `inv_ratio = (q × m) / Q_max ∈ [−1, 1]` (inventário em $ sobre o limite) e `s` é a meia-distância em fração. Com inventário comprado, as duas cotações descem (vender fica mais fácil, comprar mais difícil). Bid `b = r × (1 − s)` arredondado para baixo no tick; ask `a = r × (1 + s)` arredondado para cima.
3. Meia-distância `s ∈ {1, 2, 5, 10} bps` (ou seja, spread total cotado de 2, 4, 10 e 20 bps).
4. Tamanho por ordem: `notional ∈ {$10, $50}`, `qty = notional / m`. Quando `notional` é menor que o mínimo da Binance (BTC $50, ETH $20) usa-se o mínimo e isto é declarado na tabela (a célula "$10" opera BTC a $50 e ETH a $20).
5. Inventário: `Q_max = 5 × notional` (em $). Ao requote, se `|q × m| ≥ Q_max`, o inventário inteiro é zerado a mercado (taker) no último preço impresso em `t` e as duas cotações são recolocadas a partir de flat. Quando `|q × m| ≥ Q_max` nunca se cota o lado que aumentaria o inventário (redundante com o flatten, mantido por segurança).
6. Fill dentro do intervalo `[t, t + R)`: bid preenche se `min(low dos bins) < b` (fim de fila) ou `≤ b` (preço igual); ask se `max(high) > a` ou `≥ a`. Os dois lados podem preencher no mesmo intervalo. Preço do fill = preço cotado. Uma ordem preenche no máximo uma vez por intervalo (não é reposta até o próximo requote).
7. Fim do dia: flatten taker no último preço do dia.
8. Capital: $300 por conta, 8 símbolos simultâneos, margem cross 10×. Pior caso de uso de margem: 8 × $250 = $2.000 de notional bruto (6,7× o capital) na célula de $50. Não há checagem de margem por instante (declarado: sem alavancagem acima de 10× em nenhuma célula, então nunca falta margem); P&L reportado em $ e em % do capital.

### Custos

- Fee maker sobre o notional de cada fill limit: 0,02% (principal); −0,01% (rebate VIP, sensibilidade); 0% (teto).
- Fee taker 0,05% nos flattens (limite de inventário e fim do dia), fill no último preço impresso (sem slippage além do próprio cruzamento do spread; declarado otimista, tamanho pequeno).
- Funding ignorado (declarado: inventário médio perto de zero e flatten diário; o funding de 8h sobre ≤ $250 por símbolo é ≤ $0,03 por evento, mas pode somar).
- Latência modelada só como defasagem do mid de referência (item 1). Não modelamos rejeição de ordem, rate limit nem falha de cancelamento (ordem que não cancela a tempo e preenche "fantasma"), todos contra nós.

## Variantes e métricas

Grid por símbolo e carteira: `s` (4) × `R` (2) × `notional` (2) = 16 células, cada uma nos dois modelos de fill e nas três fees maker (a fee maker é um termo linear sobre o notional preenchido, então as três são calculadas da mesma simulação). Sem seleção de célula: todas reportadas.

Por célula × modelo × fee, carteira e por símbolo:

- P&L líquido por dia ($ e % de $300); total em amostra e holdout.
- P&L líquido por $1.000 de notional preenchido (fills limit).
- Seleção adversa: para cada fill limit, `sinal × (último preço 1 s depois − preço do fill) × qty`, idem 10 s depois; somado por dia e expresso em bps do notional preenchido. Negativo = o preço andou contra nós depois do fill. "Depois do fill" é medido a partir do fim do intervalo de requote em que o fill ocorreu (resolução do modelo; declarado).
- Nº de fills limit, nº de flattens por limite, inventário máximo em $ (|q × m| máximo), drawdown máximo do P&L acumulado diário, Sharpe diário = média / desvio do P&L diário × √365 (anualizado; 12 dias em amostra é pouco e isto é declarado), e o mesmo sem anualizar.

## Critérios de sucesso (por célula, carteira de 8 símbolos, modelo fim de fila, maker 0,02%)

1. P&L líquido total > 0 nos 12 dias em amostra.
2. P&L líquido total > 0 nos 4 dias de holdout.
3. Sharpe diário anualizado > 1 em amostra.

PASS exige os três. Qualquer outra coisa é FAIL. O teto (preço igual preenche, maker 0%) é reportado ao lado para mostrar quanto do resultado depende das hipóteses otimistas; não entra no veredito.

## Limites declarados

- Sem L2: não sabemos a profundidade no nosso preço nem quantos estavam na fila. "Fim de fila" é uma regra, não uma medição; em pares ilíquidos com ticks largos o nível pode ser varrido parcialmente e a regra ainda nos preenche.
- Dias amostrados (1, 8, 15, 22) de 3 meses de 2025 e 1 de 2026: 16 dias no total. Qualquer Sharpe aqui é indicativo. O período não cobre um crash de verdade dentro dos dias escolhidos a menos que ele caia num deles.
- Sem funding, sem rate limit, sem falha de cancelamento, sem checagem de volume mínimo por fill: tudo isso é contra nós no real.
- Não há otimização nem seleção: o grid é fixo e inteiro reportado.

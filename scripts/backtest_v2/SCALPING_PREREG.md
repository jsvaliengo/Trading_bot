# Pré-registro: scalping popular em 1m e 5m (Binance USDT-M)

Data: 2026-10-28. Escrito e commitado ANTES de baixar dado intradiário ou rodar qualquer backtest desta rodada. As regras abaixo não mudam depois de ver resultado OOS ou do holdout. Bug que invalide resultado pode ser corrigido, mas a correção é declarada no relatório e tudo é rodado de novo.

Pesquisa apenas. Nada aqui entra no bot, na VM ou no deploy. O forward test Donchian 4h em curso não é tocado.

## Pergunta e hipótese

Pergunta: alguma técnica popular de scalping tem edge real em perpétuos USDT-M da Binance depois de custos realistas?

H1: pelo menos uma das três técnicas abaixo tem expectativa líquida por trade > 0 fora da amostra (OOS), com fee, slippage e funding modelados.

H0: nenhuma tem. Em scalps com alvo de 0,3 a 0,8%, o custo de ida e volta (0,16% taker+taker com slippage, 0,26% quando sai no stop) come de 15 a 30% do alvo. A hipótese nula é que o sinal bruto não cobre isso.

## Dados

- Fonte: arquivos públicos `data.binance.vision`, klines mensais `futures/um/monthly/klines/<SYM>/{1m,5m}/` e funding mensal já em cache (`fundingRate`). Os zips são dados não confiáveis: ficam no diretório próprio do cache (`runtime/backtest_v2/cache/vision_raw/`), são lidos só como CSV em memória, nada é extraído nem executado. A regex de símbolo já existente valida o nome antes de virar caminho.
- Período total: 2022-01-01 → 2026-10-01 (mais dezembro de 2021 como aquecimento de indicadores).
- Universo point-in-time anual, decidido antes de baixar 1m: no dia 1º de janeiro de cada ano Y (2022 a 2026), os 12 pares com maior `quote_volume` diário somado nos 30 dias anteriores, usando só barras diárias com `close_time < Y-01-01` e exigindo ≥ 60 barras diárias. Um par só opera no ano em que é membro. Lista calculada a partir do cache 1d (comando `trend4h.universe_scores`) e congelada aqui:
  - 2022: BTC, ETH, LUNA, MATIC, SAND, SOL, GALA, 1000SHIB, AVAX, MANA, NEAR, XRP
  - 2023: BTC, ETH, DOGE, BNB, XRP, LTC, SOL, AXS, MATIC, MASK, APE, LINK
  - 2024: BTC, ETH, SOL, AVAX, TRB, DOGE, ADA, XRP, OP, TIA, MATIC, BNB
  - 2025: BTC, ETH, XRP, SOL, DOGE, 1000PEPE, SUI, BNB, ADA, LINK, HBAR, ENA
  - 2026: BTC, ETH, SOL, ZEC, XRP, PIPPIN, DOGE, BNB, SUI, HYPE, LIGHT, 1000PEPE
  - 31 símbolos distintos. LUNA (colapso em 2022-05) e MATIC (renomeado em 2024) ficam porque eram top-12 na data da escolha: é isso que remove o viés de sobrevivência. Par que morre no meio do ano fecha a posição aberta no último close disponível.
- Baixa-se por símbolo só os meses do(s) ano(s) em que ele é membro, mais o dezembro anterior. 1m e 5m dos mesmos meses.
- Barras com volume zero são descartadas (par morto fica plano no arquivo).
- Funding: cada evento estritamente dentro de (entrada, saída), preço = abertura da barra que contém o instante. Em scalps de minutos o funding quase não aparece, mas entra na conta.

## Convenções de execução (motor existente, `engine.py`)

- Sinal na barra i FECHADA. Entrada taker na ABERTURA de i+1 com slippage 0,03% contra. Entrada maker: limit no preço da regra, preenche só se a barra ATRAVESSA o limite em 0,01% dentro de `k` barras (modelo H2 de `experiments.py`, `engine.maker_fill`); gap além do limite na abertura preenche na abertura. Sem fill em `k` barras → ordem cancelada, sem trade.
- Ordem intrabar conservadora: o extremo ADVERSO vem antes do favorável. Barra que toca SL e TP conta como SL.
- SL é sempre STOP_MARKET taker: fee 0,05% + slippage 0,03% + 0,10% extra. Gap através do SL preenche na abertura.
- TP taker: fee 0,05% + slippage 0,03%. TP maker (só onde dito): limit no alvo, fee 0,02%, sem slippage, exige atravessar o alvo em 0,01% (mesmo modelo de trade-through da entrada).
- Saída por tempo: se o trade não saiu em 4 horas de barras (240 barras de 1m, 48 de 5m), sai taker na abertura da barra seguinte. Scalp que não resolve em 4h não é scalp.
- Fee de entrada: taker 0,05% ou maker 0,02% conforme a variante. Funding real do arquivo.

## Indicadores (barras fechadas do TF de execução, salvo onde dito)

- EMA(n): exponencial padrão, `alpha = 2/(n+1)`, semente na 1ª barra.
- ATR(14): Wilder, true range com o close anterior.
- VWAP de sessão: `Σ(preço típico × volume) / Σ volume` desde 00:00 UTC do dia corrente, reiniciando a cada dia UTC.
- Bollinger(20, σ): média simples de 20 closes ± σ × desvio padrão populacional de 20 closes.
- RSI(14): Wilder.
- Volume médio: média simples de 20 barras.
- Variante 1m com filtro de 5m: a barra 5m só é visível depois que FECHOU (`strategies.aligned_index`, sem lookahead).

## S1: pullback a VWAP + EMA (tendência intradiária)

- Tendência (sempre no 5m): LONG se `EMA9 > EMA21` e `close > VWAP` da barra 5m fechada mais recente; SHORT se `EMA9 < EMA21` e `close < VWAP`.
- Gatilho (no TF de execução, 1m ou 5m), LONG: a barra toca a EMA9 ou o VWAP (`low ≤ max(EMA9, VWAP)`), fecha acima dos dois (`close > EMA9` e `close > VWAP`) e é candle de rejeição: `close > open` e `close` no terço superior da barra (`(close − low) ≥ 2/3 × (high − low)`). SHORT espelhado.
- Entrada taker na abertura seguinte. Variante maker: limit no close da barra do sinal, `k = 3` barras.
- SL = `entry − k_sl × ATR14` (LONG). TP = `entry + R × (entry − SL)`.
- Grid (6 células): `R ∈ {1, 1,5, 2}` × `k_sl ∈ {1, 1,5}`.
- Variantes: 1m-taker, 1m-maker, 5m-taker, 5m-maker.

## S2: rompimento do range de abertura (ORB)

- Duas sessões por dia UTC: 00:00 (abertura do dia) e 13:30 (abertura de NY). O range é o `max(high)` e `min(low)` das primeiras `W` barras da sessão, `W ∈ {15 min, 30 min}` (15 ou 30 barras de 1m; 3 ou 6 barras de 5m).
- Gatilho: primeira barra fechada depois do range com `close > range_high` (LONG) ou `close < range_low` (SHORT), e volume da barra > média de 20 barras. Só até 4 horas depois do fim do range. Um trade por sessão por par (o primeiro rompimento; o lado oposto não entra depois).
- Entrada taker na abertura seguinte (rompimento é ordem stop, sempre taker). Sem variante maker.
- SL no ponto médio do range. TP = `entry + R × (range_high − range_low)` (LONG). Trade com `range = 0` não existe.
- Grid (6 células): `R ∈ {1, 1,5, 2}` × `W ∈ {15, 30}`.
- Variantes: 1m-taker, 5m-taker.

## S3: reversão à média Bollinger com entrada maker

- Gatilho LONG: `low ≤ banda inferior(σ)` e `RSI14 < 30` na barra fechada. SHORT: `high ≥ banda superior` e `RSI14 > 70`.
- Entrada maker (principal): limit na banda inferior calculada na barra do sinal, `k = 3` barras, modelo de trade-through. Variante taker: entrada na abertura seguinte.
- TP: banda média (SMA20) calculada na barra do sinal, fixa. TP maker (limit) na variante maker; taker na variante taker.
- SL: `3σ` (banda de 3 desvios da barra do sinal) ou `entry − 1,5 × ATR14`, conforme a célula. STOP_MARKET.
- Grid (4 células): `σ ∈ {2, 2,5}` × `SL ∈ {3σ, ATR×1,5}`.
- Variantes: 1m-maker, 1m-taker, 5m-maker, 5m-taker.

## S4: market-making (fora do escopo)

Um proxy de market-making precisa de book de ofertas (profundidade, fila, cancelamentos). Com klines não dá para saber se uma ordem passiva teria sido executada antes do preço virar. Fica fora. O modelo de trade-through aqui já é a aproximação mais honesta que klines permitem para ordem passiva.

## Sizing e carteira

- Equity inicial 10.000 (escala irrelevante).
- Risco fixo: 1% da equity na entrada até o stop. `notional = equity × 0,01 / dist_stop`.
- Teto por posição 2× equity. Teto bruto 5× equity. Máximo 3 posições simultâneas. Uma posição por par. Candidatos no mesmo instante: ordem alfabética do símbolo (regra do motor).
- A métrica principal é expectativa por trade (fração do notional), que não depende do sizing. A carteira entra só para drawdown e capacidade.

## Custos

- Taker 0,05% por lado. Maker 0,02% por lado.
- Slippage 0,03% na entrada taker e 0,03% na saída taker. Stop paga 0,10% extra. Maker: sem slippage, mas só preenche com trade-through de 0,01%.
- Funding real do arquivo.
- Piso de custo (reportado): custo médio efetivo por trade = fee + slippage realizados. É o edge bruto mínimo que um scalp precisaria para empatar.

## Validação

- Desenvolvimento: 2022-01-01 → 2026-01-01. Walk-forward com treino de 6 meses, teste de 2 meses, passo de 2 meses. Primeiro treino 2022-01 → 2022-07. OOS concatenado 2022-07-01 → 2026-01-01 (21 folds).
- Escolha da célula em cada fold: maior expectativa líquida por trade no treino, com no mínimo 30 trades no treino; sem célula com 30 trades, usa a 1ª do grid. Empate: a primeira na ordem do grid.
- Holdout intocado: 2026-01-01 → 2026-10-01. Uma rodada por variante, com a célula que a mesma regra escolhe no treino 2025-07-01 → 2026-01-01, fixa no holdout inteiro.
- Robustez: cada célula do grid roda fixa no período OOS do walk-forward. Tabela completa no relatório.
- Trades simulados uma vez por (par, barra, lado, célula) e reaproveitados entre treino, teste e grid fixo quando o trade termina dentro da janela (o trade é determinístico; a truncagem no fim da janela é o único caso recalculado).

## Métricas reportadas (por variante: técnica × TF × tipo de entrada)

Trades OOS, win%, ganho e perda médios, expectativa líquida por trade com IC95% (bootstrap em blocos de DIA, agrupando trades pelo dia de entrada, 10.000 reamostragens, seed fixa), expectativa bruta (antes de fee, slippage e funding), fee+slippage médio por trade e como % do ganho médio, drawdown máximo da carteira, retorno por ano-calendário, LONG vs SHORT, saídas por motivo (sl/tp/time), taxa de fill da entrada maker, holdout, grid fixo.

## Critérios de sucesso (todos, por variante)

1. Walk-forward OOS: limite inferior do IC95% (bloco-dia) da expectativa líquida por trade > 0.
2. ≥ 500 trades OOS no walk-forward.
3. Expectativa líquida positiva em ≥ 2/3 dos anos OOS (2022-H2, 2023, 2024, 2025: ≥ 3 de 4).
4. Holdout: expectativa líquida por trade > 0.
5. Robustez: maioria das células fixas do grid com expectativa OOS > 0.

Falhou qualquer um → a variante não passa. Não há rodada extra para "consertar" regra. A expectativa bruta é reportada ao lado para separar "sinal sem edge" de "sinal com edge que o custo come".

## Ressalvas de múltiplos testes

- Esta rodada testa 10 variantes e 52 células: S1 2 TF × 2 entradas × 6 = 24; S2 2 TF × 1 × 6 = 12; S3 2 TF × 2 × 4 = 16.
- Rodadas anteriores (`EXPERIMENTOS.md`, `TREND4H_RELATORIO.md`): 14 variantes e 53 células em 3m e 4h, nenhuma passou. O holdout 2026 é virgem para estas regras.
- O modelo de fill maker com trade-through é otimista num ponto: assume que a fila na frente da ordem some quando o preço atravessa 0,01%. Em pares líquidos no 1m isso é razoável; não está provado.
- Klines agregam o que aconteceu dentro da barra. Em 1m a ordem intrabar adotada (adverso antes do favorável) é conservadora, e o slippage de 0,03% é a média que o bot mediu, não o pior caso.

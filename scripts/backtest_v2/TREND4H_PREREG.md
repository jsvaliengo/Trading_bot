# Pré-registro: trend-following 4h (Binance USDT-M)

Data: 2026-10-24. Escrito e commitado ANTES de qualquer backtest desta rodada. As regras abaixo não mudam depois de ver resultado OOS ou do holdout. Bug que invalide resultado pode ser corrigido, mas a correção é declarada no relatório e tudo é rodado de novo.

Pesquisa apenas. Nada aqui entra no bot, na VM ou no deploy.

## Hipótese

Em perpétuos USDT-M da Binance, rompimentos e momentum de série temporal no 4h capturam tendências de vários dias. A cauda direita desses movimentos paga as muitas perdas pequenas, mesmo depois de fee, slippage e funding, quando o tamanho da posição é dado pela volatilidade (ATR).

H0: a expectativa líquida por trade é ≤ 0 e o Sharpe da carteira é ≤ 0.

## Dados

- Fonte: arquivos públicos `data.binance.vision`. Klines mensais `futures/um/monthly/klines/<SYM>/{1d,4h}/` e funding mensal `futures/um/monthly/fundingRate/<SYM>/`. Os arquivos baixados são dados não confiáveis. Ficam em diretório próprio do cache, são lidos só como CSV e nada deles é executado.
- Período total: 2020-01-01 → 2026-10-01.
- Lista de símbolos: todos os prefixos em `futures/um/monthly/klines/` (inclui deslistados), que terminam em `USDT` e não têm `_` (sem contratos trimestrais). Ficam de fora as bases stablecoin ou fiat: USDC, BUSD, TUSD, USDP, FDUSD, DAI, USDE, PYUSD, EUR, GBP, AEUR.
- 1d de todos os candidatos (para o universo). 4h e funding só dos símbolos que entram no top-30 pelo menos uma vez.
- Funding: cada evento estritamente dentro de (entrada, saída). LONG paga `rate × preço/entrada` (recebe se o rate for negativo), SHORT o oposto. O arquivo não traz mark price, então o preço é a abertura da barra 4h que contém o instante.

## Universo point-in-time (menos viés de sobrevivência)

- Rebalanceamento semanal, segunda 00:00 UTC (T).
- Score = soma do `quote_volume` das barras diárias com `close_time < T` nos 30 dias anteriores a T.
- Elegível em T: ≥ 60 barras diárias com `close_time < T` e a última barra diária é a do dia imediatamente anterior a T (o par ainda negocia).
- Universo da semana [T, T+7d) = top-30 elegíveis por score. Só se usa dado fechado antes de T.
- Um sinal só vira entrada se o símbolo está no universo vigente no fechamento da barra do sinal. Posição aberta segue as próprias saídas, mesmo se o par sair do universo.
- Par deslistado com posição aberta: fecha no close da última barra disponível (aproximação da liquidação da Binance).

## Indicadores (barras 4h fechadas)

- ATR: Wilder, 20 barras, true range com o close anterior.
- Sinal na barra i FECHADA → entrada na ABERTURA de i+1 (o mesmo que o bot faria logo após o fechamento). No 4h, a diferença entre usar o candle em formação e o fechado é pequena, mas aqui é sempre o fechado.

## Variante A: Donchian / turtle (`don`)

- LONG quando `close_i > max(high_{i-N..i-1})`. SHORT quando `close_i < min(low_{i-N..i-1})`.
- Stop inicial intrabar (STOP_MARKET), a `k × ATR_i / close_i` do preço de entrada efetivo.
- Saída trailing pelo canal de saída: LONG sai quando `close_i < min(low_{i-M..i-1})`, SHORT quando `close_i > max(high_{i-M..i-1})`, na abertura de i+1.
- Sem TP. Os dois lados.
- Grid (8 células, defaults de literatura turtle adaptados a 4h): `(N, M) ∈ {(20,10), (40,20), (55,20), (100,50)}` × `k ∈ {2, 3}`.

## Variante B: momentum de série temporal (`tsmom`)

- `z_i = ln(C_i / C_{i-L}) / (σ_i × √L)`, com `σ_i` = desvio padrão dos log-retornos 4h das últimas 180 barras. Esse z é o filtro de volatilidade: o retorno só conta quando é grande perto do ruído do próprio par.
- LONG quando z cruza para cima de +0,5 (`z_i > 0,5` e `z_{i-1} ≤ 0,5`). SHORT quando cruza para baixo de −0,5.
- Saída: LONG sai quando `z_i < 0`, SHORT quando `z_i > 0`, na abertura de i+1.
- Stop catastrófico intrabar a `k × ATR_i / close_i`, que também define o tamanho.
- Grid (8 células): `L ∈ {42, 84, 180, 360}` barras 4h (7, 14, 30 e 60 dias) × `k ∈ {3, 5}`. Limiar 0,5 e janela de σ 180 fixos.

## Sizing e carteira

- Equity inicial 10.000 (escala irrelevante).
- Risco por trade: 0,5% da equity no momento da entrada até o stop. `notional = equity × 0,005 / dist_stop`.
- Teto por posição 1,0× equity. Teto bruto 3,0× equity. Máximo 10 posições simultâneas (risco aberto máximo 5%).
- Candidatos no mesmo instante: ordem alfabética do símbolo (regra do motor). Um par só tem uma posição por vez.
- Motor existente (`engine.run_portfolio` + `engine.simulate_trade`): SL tem prioridade se a barra toca stop e saída, gap através do stop preenche na abertura.

## Custos

- Taker 0,05% por lado sobre o notional de cada perna.
- Slippage 0,03% na entrada e 0,03% na saída. Stop paga 0,10% extra (STOP_MARKET escorrega).
- Funding real do arquivo.

## Validação

- Período de desenvolvimento: 2020-01-01 → 2025-07-01.
- Walk-forward: janela de treino móvel de 12 meses, teste de 3 meses, passo de 3 meses. Primeiro treino 2020-01 → 2021-01. OOS concatenado 2021-01-01 → 2025-07-01 (18 folds).
- Escolha da célula em cada fold: maior retorno da carteira no treino. Empate: a primeira célula na ordem do grid.
- Holdout intocado: 2025-07-01 → 2026-10-01. Uma única rodada por variante, com a célula que a mesma regra escolhe no treino 2024-07-01 → 2025-07-01, fixa no holdout inteiro. Reportado em separado.
- Robustez: cada uma das 8 células roda fixa no período OOS do walk-forward (2021-01 → 2025-07). A tabela completa vai no relatório, não só a célula escolhida.

## Métricas reportadas (por variante)

Trades OOS, win%, ganho e perda médios, expectativa líquida por trade com IC95% (bootstrap em blocos de semana, agrupando trades pela semana de entrada, 10.000 reamostragens, seed fixa), expectativa bruta, CAGR, drawdown máximo, Sharpe (retornos diários da equity marcada a mercado, ×√365) com IC95% por bootstrap em blocos de semana, Calmar, retorno por ano-calendário, LONG vs SHORT, P&L sem os 5 melhores trades, turnover (notional negociado / equity média / ano), fees e funding totais.

## Critérios de sucesso (todos, por variante)

1. Walk-forward OOS: limite inferior do IC95% da expectativa por trade > 0, OU Sharpe da carteira ≥ 0,8 com limite inferior do IC95% do Sharpe > 0.
2. Retorno da carteira positivo na maioria dos anos OOS: ≥ 3 de 5 (2021, 2022, 2023, 2024, 2025-H1).
3. Holdout: retorno da carteira > 0 e expectativa por trade > 0.
4. Drawdown máximo < 35% no walk-forward OOS e no holdout, com 0,5% de risco por trade.
5. Robustez: ≥ 6 das 8 células fixas com expectativa OOS > 0, e Sharpe mediano das 8 células > 0.
6. Sem dependência de poucos trades: P&L do walk-forward OOS continua > 0 sem os 5 melhores trades.
7. Mínimo de 200 trades OOS.

Falhou qualquer um → a variante não passa. Não há rodada extra para "consertar" regra.

## Ressalvas de múltiplos testes

- Esta rodada testa 2 variantes, 16 células.
- Rodadas anteriores (`EXPERIMENTOS.md`): 12 variantes, 37 células, incluindo Donchian 4h 20/10 com SL fixo no período 2023-10 → 2026-10. Esse período cobre o holdout desta rodada, então o holdout não é virgem para a família Donchian. Ele é virgem para estas regras exatas (stop ATR, sizing por volatilidade, universo dinâmico).
- Universo ainda tem um resto de viés: só existe o que está no arquivo público, e a liquidação real de um par deslistado pode diferir do último close.

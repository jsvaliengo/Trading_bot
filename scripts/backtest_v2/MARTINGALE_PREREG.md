# Pré-registro: martingale/DCA com hedge estilo CoinTech2u (Binance USDT-M, 5m)

Data: 2026-10-30. Escrito e commitado ANTES de rodar qualquer simulação. As regras, o grid, os custos, o período e os critérios abaixo não mudam depois de ver resultado. Bug que invalide resultado pode ser corrigido, mas a correção é declarada no relatório e tudo é rodado de novo.

Pesquisa apenas. Nada aqui entra no bot, na VM ou no deploy. O forward test Donchian 4h não é tocado.

## Pergunta e hipóteses

Pergunta: a estratégia que o app CoinTech2u mostra (LONG e SHORT simultâneos por par, adicionando ordens maiores quando o preço anda contra, fechando no preço médio + take profit pequeno, sem stop) sobrevive a 2022→2026 em perpétuos USDT-M da Binance com uma conta de $300? E de $3.000?

- H0: a taxa de acerto por trade fechado é ~99% (por construção: só fecha no lucro), mas o prejuízo flutuante acumulado nas pernas abertas liquida a conta em algum momento do período, ou gera drawdown de equity (realizado + não realizado) acima de 20%.
- H1: existe célula do grid em que a conta de $300 passa o período inteiro sem liquidação, com drawdown de equity máximo < 20% e retorno líquido positivo no holdout de 2026.

## Dados

- Fonte: `.npz` consolidados de 5m já em cache (`runtime/backtest_v2/cache/vision/klines_5m/`, montados pelo `vision.py` na pesquisa de scalping a partir de `data.binance.vision`), mais funding real mensal (`cache/vision/funding/`). Nenhum dado novo é baixado.
- Período completo: 2022-01-01 → 2026-10-01. Holdout: 2026-01-01 → 2026-10-01, simulado como conta nova que começa em 2026-01-01 (a estratégia não tem fase de ajuste, então "holdout" aqui significa só período separado, não dado fora da amostra de otimização; não há otimização).
- Universo A ("31"): o universo point-in-time anual do pré-registro de scalping (12 pares por ano, 31 símbolos distintos, `scalping.UNIVERSE`). Um par opera só nos anos em que é membro; ao sair do universo (virada de ano ou deslistagem/morte, caso LUNA 2022-05 e MATIC 2024-09) as duas pernas fecham no último close disponível, taker. Isso mantém o viés de sobrevivência fora.
- Universo B ("3"): BTCUSDT, ETHUSDT, ADAUSDT, com todo o 5m disponível no cache (ADA só a partir de 2023-12; BTC e ETH desde 2021-12). ADA é o par que o usuário viu no app.
- Barras de volume zero descartadas (par morto fica plano no arquivo). Barra ausente para um par naquele instante: o par é pulado nesse instante.

## Estratégia simulada (por par, duas pernas independentes, LONG e SHORT)

1. Abertura: cada perna abre a ordem base `B` (notional em $) na abertura da primeira barra em que o par está ativo, taker.
2. Adição (nível k ≥ 1): quando o preço anda `X%` contra o ÚLTIMO preço de preenchimento da perna (LONG: `low ≤ último × (1 − X)`; SHORT: `high ≥ último × (1 + X)`), a perna compra mais `B × m^k` no preço do gatilho (ordem limite já colocada no nível). Vários níveis podem disparar na mesma barra, em sequência, enquanto o extremo da barra alcançar o próximo gatilho. Preço médio e quantidade recalculados a cada adição.
3. Take profit: a perna fecha inteira quando o preço atinge `média × (1 + TP)` (LONG; espelhado para SHORT), no preço do alvo. Ordem intrabar conservadora: se a barra disparou alguma adição, o TP só pode acontecer a partir da barra seguinte (não assumimos que o favorável veio depois do adverso dentro da mesma barra).
4. Reabertura: depois do TP a perna reabre no nível base na abertura da barra seguinte, taker.
5. Sem stop loss por perna (versão original). Limite de níveis `N`: a partir do nível `N − 1` a perna não adiciona mais e espera o TP (ou a liquidação).
6. Margem: alavancagem 10×, cross. Adição que levaria `Σ notional / 10 > equity` é rejeitada ("margem insuficiente"), a perna fica onde está. A margem máxima por par é implícita no sizing abaixo.
7. Liquidação (aproximação declarada): quando `equity ≤ 0,5% × Σ notional` (taxa de manutenção do menor tier da Binance para alts, 0,4% para BTC; usamos 0,5% para todos), todas as posições fecham no close da barra com fee taker e slippage, equity vai para zero. Conta-se uma liquidação, registra-se o instante, a conta é RECAPITALIZADA com o mesmo capital inicial (novo depósito) e tudo recomeça no nível base na barra seguinte. Retorno líquido = equity final − total depositado. Sem taxa de clearance de liquidação (a equity já é ~zero).
8. Stop de portfólio (só na variante com trava): quando `equity ≤ pico × 0,80`, todas as posições fecham no close da barra (taker + slippage), conta-se um stop, o pico é reiniciado para a equity atual e tudo recomeça no nível base na barra seguinte.

### Sizing (fixo em $, sem compounding)

`B = capital × 10 × 0,5 / (n_pares × 2 × S)`, onde `S = Σ_{k=0}^{N_ref − 1} m^k` é o notional total de uma escada completa em múltiplos de `B`, `N_ref = N` na variante com limite e `N_ref = 7` na original. Ou seja: se todos os pares tiverem as duas pernas na escada completa de referência ao mesmo tempo, a margem usada é 50% do capital inicial. `n_pares` = 12 no universo A (membros por ano) e 3 no universo B. O relatório mostra o `B` resultante e compara com o notional mínimo da Binance ($5 na maioria dos pares; $100 em BTC): se `B` ficar abaixo, a conta real não conseguiria nem abrir a escada nesse tamanho, e o resultado real seria mais concentrado (pior) que o simulado. Não aplicamos piso de notional mínimo na simulação (declarado como otimista).

### Custos

- Fee taker 0,05% sobre o notional de cada preenchimento (entrada base, adições, TP, fechamentos forçados). As adições e o TP são ordens limite na prática (0,02% maker); usar taker é conservador e declarado.
- Slippage 0,03% contra só nas execuções a mercado: abertura/reabertura da base e fechamentos forçados (liquidação, stop de portfólio, saída do universo). Adições e TP preenchem no preço do nível.
- Funding real do arquivo: em cada instante de funding, LONG paga `rate × notional_long`, SHORT recebe `rate × notional_short` (rate negativo inverte). Notional no close da barra que contém o instante.
- Taxa de performance do CoinTech2u: 20% sobre o lucro líquido de cada trade fechado com lucro (o modelo cobra sobre o realizado por trade, não sobre a equity; a perda flutuante não abate a taxa). Reportado como linha separada.

## Variantes (todas rodam para capital $300 e $3.000, universos A e B, período completo e holdout)

- V1 original: sem stop, `N = ∞` (na prática até a rejeição por margem e a liquidação).
- V2a travas: `N = 5`.
- V2b travas: `N = 7`.
- V3 travas + stop de portfólio: `N = 7` e stop de portfólio em drawdown de 20% do pico.

Grid (12 células, igual em todas as variantes): passo `X ∈ {1,0%, 1,5%, 2,0%}` × multiplicador `m ∈ {1,5, 2,0}` × `TP ∈ {0,6%, 1,0%}`. Célula padrão (a mais próxima do que os reviews descrevem): `X = 1,5%`, `m = 2,0`, `TP = 1,0%`. Não há seleção de célula: todas são reportadas.

## Métricas (por variante × célula × capital × universo × período)

Retorno líquido ($ e % do capital inicial, depois de fee e funding, descontando recapitalizações), taxa de acerto de trades fechados (TP = acerto; fechamento forçado com P&L < 0 = erro), número de trades fechados, prejuízo flutuante máximo (mínimo da soma do não realizado, em $ e % do capital), drawdown máximo da equity (realizado + não realizado), número de liquidações, tempo até a primeira liquidação, número de stops de portfólio (V3), pior mês (variação da equity em mês-calendário, incluindo não realizado), margem máxima usada (`Σ notional / 10 / equity`), nível máximo atingido por perna, retorno líquido depois da taxa de 20% sobre lucro realizado.

## Critérios de sucesso (todos, por variante, na célula padrão; robustez: quantas das 12 células passam)

1. Zero liquidações no período completo com capital de $300.
2. Drawdown máximo da equity (realizado + não realizado) < 20% no período completo com $300.
3. Retorno líquido > 0 no holdout de 2026 com $300.

PASS exige a célula padrão passando nos três critérios E pelo menos 6 das 12 células passando nos três. Qualquer outra coisa é FAIL. Os mesmos números são reportados para $3.000 como sensibilidade, sem entrar no veredito.

## Limites declarados

- Klines de 5m não dizem a ordem dos extremos dentro da barra; a convenção (adições antes, TP só na barra seguinte) é conservadora para o TP mas assume que toda ordem limite no nível preenche no preço do nível (sem gap). Em barras de gap grande (ex.: LUNA), o preenchimento real seria pior que o simulado.
- Liquidação por aproximação de margem cross em uma conta inteira; a Binance liquida por tier de manutenção e com clearance fee; a aproximação é levemente otimista.
- Sizing fixo em $, sem compounding, para que o resultado dependa só do caminho de preço e não da sequência de ganhos.
- Universo A reproduz "top-12 point-in-time", não a lista exata que o CoinTech2u oferece; universo B cobre o caso concreto visto pelo usuário (ADA).

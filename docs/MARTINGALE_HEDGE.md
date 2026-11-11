# Martingale/DCA com hedge (estilo CoinTech2u)

Estratégia opcional em `trading_bot/core/martingale.py`, ligada por `TRADING_BOT_STRATEGY=martingale_hedge`.
Nada muda quando a env não está setada (padrão segue o de hoje). Pullback e Donchian não foram alterados.

## ⚠️ AVISO OBRIGATÓRIO — leia antes de ligar

A pesquisa pré-registrada (`scripts/backtest_v2/MARTINGALE_PREREG.md` e `MARTINGALE_RELATORIO.md`, 2022→2026,
5m, 31 moedas e BTC/ETH/ADA) deu **FAIL em 0/360 células**:

- **V1 (original, sem stop):** conta de **$300 liquidada 48 vezes** no universo de 31 moedas (17× em BTC/ETH/ADA);
  1ª liquidação no dia 35.
- **Nenhuma célula** (variantes, grids, capitais) teve drawdown de equity **< 20%**.
- **V3 (N=7 + stop de portfólio 20%)**, a única que não liquidou, **sangra até zero** em 44–73 stops
  quando reinicia sozinha. Taxa de acerto de 97–100% convive com a perda do capital inteiro.

Esta implementação é a V3 **sem reinício automático**: o stop de portfólio fecha tudo e **pausa o bot**; só um
humano retoma (`/resume`). Esse stop é a única coisa entre a estratégia e a liquidação e **não pode ser
desligado** (a env é limitada a 5–25%).

- **Só testnet** até o usuário decidir o contrário.
- **NUNCA na mesma conta do forward test Donchian 4h**: as duas estratégias dividem equity, pico e posições;
  o stop de portfólio fecharia posições Donchian e o drawdown do martingale invalidaria o forward test.

## Como liga

```
TRADING_BOT_STRATEGY=martingale_hedge
TRADING_BOT_MARTINGALE_PAIRS=ADAUSDT          # conjunto inicial/fallback (ver "Seleção automática de pares")
```

Requisito: conta em **Hedge Mode** (dual-side). O bot já o ativa no setup; se a Binance recusar (há posição
aberta em one-way), a estratégia **não inicia** e o bot fica pausado, com log + Telegram.

## Regras (por par, pernas LONG e SHORT independentes e simultâneas)

1. Abre a ordem base `B` a mercado em cada perna.
2. Preço anda `X%` contra o **último fill** da perna → adiciona `B×m^k` a mercado (o runner lê o ticker a cada
   tick do monitor e envia MARKET quando o gatilho cruza; não há ordens limite pré-colocadas).
3. Preço atinge `média × (1 ± TP)` → fecha a perna inteira (`close_reason="Martingale TP nível k"`) e reabre
   na base no tick seguinte.
4. A partir do nível `N−1` não adiciona mais; espera o TP.
5. **Stop de portfólio:** equity ≤ pico × (1 − STOP) → fecha **todas** as posições a mercado
   (`close_reason="Stop de portfólio"`), Telegram e `paused=True`. O pico é persistido no state
   (`martingale_peak_equity`) para um restart não zerar um drawdown aberto.
6. Sizing: `B = capital × alavancagem × 0,5 / (n_pares × 2 × S)`, `S = Σ_{k=0}^{N−1} m^k`, capital = saldo da
   carteira no start. Par cuja `B` fica abaixo do `minNotional` é pulado com log.

Com o bot **pausado** (manual ou pelo stop): TPs ainda fecham pernas; nada abre nem adiciona.

## Parâmetros (env `TRADING_BOT_MARTINGALE_*`)

| env | padrão | o que é |
|---|---|---|
| `PAIRS` | `ADAUSDT` | pares (máx. 3) |
| `STEP_PERCENT` | 1.5 | X: passo contra o último fill |
| `MULTIPLIER` | 2.0 | m |
| `TP_PERCENT` | 1.0 | take profit sobre a média |
| `MAX_LEVELS` | 7 | N |
| `PORTFOLIO_STOP_PERCENT` | 20 | stop de portfólio (limitado a 5–25) |
| `LEVERAGE` | 10 | alavancagem por par (margem segue a da conta) |
| `MIN_ADD_INTERVAL_SECONDS` | 60 | intervalo mínimo entre adições na mesma perna (freio da cascata de 08/11/26) |
| `FIRST_ORDER_USDT` | 0 (off) | > 0 troca a fórmula por capital por **sizing fixo**: B = valor × `LEVERAGE` (margem) — ver perfil planilha |
| `FIRST_ORDER_IS_NOTIONAL` | false | lê `FIRST_ORDER_USDT` como valor da ordem (notional), não como margem |
| `DOUBLE_FIRST` | false | CoinTech2u "Double First Order": só a 1ª ordem vale 2B (2B, 2B, 4B, 8B, …) |
| `STOP_LOSS_USDT` | 0 (off) | > 0 prevalece sobre o %: fecha tudo quando equity ≤ pico − valor; **sem clamp** (log de aviso se > 25% da banca) |

## Score dinâmico dos pares — regras

Classificação por regras (sem IA) das 36 moedas da planilha (verdes ∪ vermelhas), mostrada no card
"Planilha CoinTech2u" do dashboard (`martingale.sheet.scores` em `/api/live`). **Só leitura**: não troca
par, não pausa nada. Klines da fonte de sinal (mainnet), candles fechados, recalculado em thread de fundo
a cada `TRADING_BOT_MARTINGALE_SCORE_REFRESH_MINUTES` (720). Falha de rede mantém o último resultado;
sem resultado anterior a moeda fica cinza (`unknown`). Código: `services/martingale_pair_score.py`.

| critério | como | verde | vermelho | env `TRADING_BOT_MARTINGALE_SCORE_*` |
|---|---|---|---|---|
| `vol_pct` | média de (high−low)/close dos últimos 14 candles diários, em % | < 5,0 | > 8,0 | `VOL_GREEN`, `VOL_RED` |
| `adx` | ADX(14) no 4h, último valor | < 25 | > 35 | `ADX_GREEN`, `ADX_RED` |
| `max_move_pct` | maior variação close-to-close numa só direção (pico-a-vale) nos últimos 30 dias | < alcance da escada (`STEP_PERCENT × MAX_LEVELS`; `MOVE_GREEN` > 0 fixa outro valor) | > 20 | `MOVE_GREEN`, `MOVE_RED` |
| `volume_usd` | média do quoteVolume diário 14d | — | < 40M | `MIN_VOLUME_USD` |

Cor final: **vermelho** se qualquer critério vermelho; **verde** se todos verdes; senão **amarelo**. `reasons`
lista em pt-BR os critérios fora do verde (ex.: `vol 5,2% (>4,0%)`). As vermelhas fixas da planilha (NOT,
1000PEPE, SNX, SUI, 1000SHIB, DOGE, APE, SOL) também são avaliadas, com o marcador "planilha: evitar".
Par ativo vermelho gera um aviso em linha no card.

**Ranking** (`sheet.ranking`, `web/data.py:rank_pairs`): ordena as 36 moedas por cor (verde < amarelo < vermelho
< sem dado) e, dentro da cor, por `rank_score` = `vol_pct/VOL_GREEN + adx/ADX_GREEN + max_move_pct/MOVE_GREEN`
(+ `MIN_VOLUME/volume_usd` se abaixo do piso; menor = melhor). As vermelhas fixas da planilha vão sempre pro fim
(`sheet_red`). O card mostra "Melhores agora" com N = moedas da faixa atual e onde os pares ativos caíram no ranking.

Limiares calibrados em 09/11/26 contra a mainnet: com vol 4/7 só BNB saía verde; 5/8 porque a faixa diária
normal de alts é 4–6 % (≈3 degraus de 1,5 %/dia, o que o martingale quer). No mês da queda (ADA −30 % em
30 d) 34/36 ficaram vermelhas por `adx`/`max_move_pct` — leitura correta, não defeito do score.

## Seleção automática de pares pelo ranking

`TRADING_BOT_MARTINGALE_AUTO_PAIRS=true` (default). O runner é o dono do `PairScoreCache` (thread de fundo,
mesmo sem dashboard aberto; o dashboard lê dele) e, a cada tick, antes de mexer nas pernas, roda
`MartingaleRunner._rebalance` (`core/martingale.py`):

1. **Escada em andamento não é trocada.** Um par só muda de vaga quando **ambas** as pernas estão zeradas:
   exchange sem posição no símbolo **e** perna local já resetada (uma leitura vazia transitória da exchange não troca)
   — ou pela **saída no zero-a-zero** (regra 8), já que a perna que bate TP reabre na hora e o par quase nunca zera.
2. **Vagas** = coluna `moedas` da faixa atual da planilha pela equity (`sheet_row_index`; $300–500 → 2). Abaixo da 1ª
   faixa mantém o número atual. Faixa menor solta as vagas excedentes (só pares zerados, pior colocado primeiro);
   faixa maior ocupa as vagas livres com os melhores candidatos.
3. **Candidatos** = `COINTECH2U_GREEN` (28 moedas; as vermelhas fixas da planilha nunca entram) com cor verde ou
   amarela, na ordem de `rank_pairs`, pulando quem já ocupa vaga, quem tem posição aberta na exchange (órfã) e
   quem tem `minNotional` acima da 1ª ordem (`level_notional(base, …, 0)`).
4. **Troca**: par zerado **vermelho** → melhor candidato; par verde/amarelo fica (evita churn); sem score
   (`unknown`/cache vazio) nada muda. Antes da 1ª ordem o par novo recebe `set_leverage` (hedge mode é da conta).
5. **Cooldown**: a vaga recém-ocupada não troca de novo por `TRADING_BOT_MARTINGALE_PAIR_SWAP_COOLDOWN_MINUTES`
   (360); pares iniciais/restaurados não têm cooldown.
6. **Persistência**: `martingale_pairs` no `bot_state`; no boot prevalece sobre `MARTINGALE_PAIRS` (a env vira
   o conjunto inicial/fallback). Com `AUTO_PAIRS=false` a env é fixa e o state é ignorado.
7. Cada troca gera log INFO `🎰 Martingale: trocando slot ADAUSDT → BNBUSDT (ADA vermelho: …; BNB verde, 1º no
   ranking)` e a mesma mensagem no Telegram. O dashboard mostra os pares ativos contornados na planilha.
8. **Saída no zero-a-zero** (`TRADING_BOT_MARTINGALE_RED_EXIT_AT_BREAKEVEN=true`, default): par **vermelho** com
   perna(s) aberta(s), cooldown vencido, **candidato** verde/amarelo disponível (mesma lista da regra 3; só espiado
   até o fechamento confirmar) e soma do `unrealized_pnl` das pernas presentes na leitura ≥
   `TRADING_BOT_MARTINGALE_RED_EXIT_MIN_PNL_USDT` (0.0) → fecha as pernas a mercado (`bot.close_position`, ordem
   reduce-only com `positionSide` da perna, registrada no TradeStore/Telegram como "Martingale: par vermelho saindo
   no zero-a-zero"), reseta as pernas, tira o par da vaga e ocupa-a com o candidato no mesmo tick (a base do par
   novo abre neste tick; o par fechado não reabre). Se um fechamento falhar, o par fica na vaga. Não roda com o
   bot pausado. **Atenção**: o `unRealizedProfit` da Binance é bruto (mark − média × qty), sem a taxa de abertura
   já paga nem a de fechamento; `0.0` é zero-a-zero bruto (≈ −0,1% do notional líquido no taker) — suba o piso
   pra cobrir fees. Log/Telegram: `🎰 Martingale: fechando ADAUSDT no zero-a-zero (+0.03$) para liberar vaga: ADA
   vermelho (…); candidato BNB verde 1º` seguido do `trocando slot`.

Limites: o `B` por capital (sem `FIRST_ORDER_USDT`) é calculado com o nº de pares do boot e não é refeito ao
mudar o conjunto; o cooldown não é persistido (restart zera). Desligar: `TRADING_BOT_MARTINGALE_AUTO_PAIRS=false`.

### Drenagem de par (`TRADING_BOT_MARTINGALE_DRAIN_PAIRS`)

Pra tirar um par sem fechar as pernas na mão (ex.: "ADA para de comprar, BNB entra agora"):
`TRADING_BOT_MARTINGALE_DRAIN_PAIRS=ADAUSDT` (lista separada por vírgula; default vazio) ou, em runtime,
`/drain ADAUSDT` no Telegram (`/undrain ADAUSDT` tira; `/drain` sem argumento lista). O conjunto é persistido em
`martingale_drain_pairs` no `bot_state` e unido à env no boot (a env sempre volta no restart; pra parar de drenar
de vez, tire da env). Um par drenando:

- **não abre base nem adiciona nível** (perna fechada fica fechada; perna aberta segue até o TP, ou até a saída
  no zero-a-zero da regra 8 se ficar vermelho — nesse caso a vaga não é reocupada de novo);
- **não conta pra vaga da planilha**: `_rebalance` vê só os pares não drenados, então a vaga é reocupada pelo
  melhor candidato no mesmo tick, ainda com as pernas do par drenado abertas;
- **nunca é candidato** enquanto estiver na lista;
- com **ambas as pernas zeradas** sai de `self.pairs` (`🎰 Martingale: ADAUSDT drenado: ambas as pernas zeradas,
  par removido`, também no Telegram). Roda independente de `AUTO_PAIRS`.

Dashboard: `draining` no JSON do martingale, chip "drenando" ao lado do símbolo em Pernas ativas e badge do
anel esmaecido.

## O que fica desligado sob esta estratégia

TP/SL/trailing por ordem, IA, kill switches (sequência negativa, win rate, drawdown do pullback), meta de
lucro diária, stop loss global, regime swap, scoring/rotação de pares do pullback (a seleção pelo ranking acima é própria). Mesmo gate do Donchian em `bot.py`
(`strategy_runner is not None`).

## Limites conhecidos

- O registro de abertura no SQLite guarda preço/qty da **base**; o P&L do fechamento usa a média e a quantidade
  reais da exchange (corretos). Adições não geram linha própria.
- Estado da perna (nível, último fill, média/qty locais) é persistido no `bot_state`; sem state o nível é
  inferido do notional na exchange e o último fill = média da exchange.
- Não há fechamento intrabar: o gatilho é avaliado no intervalo do monitor, a mercado (slippage real > simulado).

## Incidente 2026-11-08: cascata de adições (nível 3→9 em 18 s)

Às 18:55 BRT a perna LONG ADAUSDT adicionou um nível a cada tick (~2,5 s) até o teto: notional $1.562 com ADA
caindo 1,9% em vez dos ~12% da escada. Cada fill foi reportado em 0,1762 (a média da posição) com o ticker em
0,1731, então o gatilho seguinte (0,1762 × 0,985 = 0,17356) já nascia satisfeito e o tick seguinte adicionava
de novo. Fix: o degrau é medido do **pior** entre fill e mark do momento da ordem; média/qty da perna são
mantidas localmente a partir do próprio fill e a leitura da exchange só as substitui quando já reflete a qty (ou 30 s após o fill);
intervalo mínimo entre adições (`MIN_ADD_INTERVAL_SECONDS`, 60 s); no restart o último fill = média da exchange
(não o preço atual) e nível/último fill/média são persistidos no `bot_state`.

## Perfil planilha CoinTech2u — banca $300

Linha "banca 300 USDT" da planilha *Binance – Estratégia Padrão* do CoinTech2u: **Millionaire Strategy 9**,
Long/Short 1st Order Size **3**, Leverage **20**, Stop Loss **180**, Double First LONG/SHORT **ON**, **2 moedas**.
**Escala Bitget (2026-11-11):** a ordem mínima da Bitget é **$5 notional**, então as linhas 90–2.000 da
`COINTECH2U_SHEET` foram escaladas × 5/3: 1ª ordem **5**, stop **300**; banca mínima e nº de moedas inalterados
(linhas ≥ 3.000 já tinham 1ª ordem ≥ 5 e ficaram iguais). Os números abaixo (3/180) são a planilha original.
Moedas verdes da planilha: ADAUSDT TRXUSDT ARBUSDT XRPUSDT APTUSDT FILUSDT SUSHIUSDT ATOMUSDT CFXUSDT 1INCHUSDT
MASKUSDT THETAUSDT OPUSDT COMPUSDT CHZUSDT NEARUSDT SANDUSDT CRVUSDT DOTUSDT (vermelhas, evitar: NOTUSDT
1000PEPEUSDT SNXUSDT SUIUSDT 1000SHIBUSDT DOGEUSDT APEUSDT SOLUSDT).

### O que a pesquisa confirmou (2026-11-08) e o que ficou como premissa

| Item | Status | Fonte |
|---|---|---|
| Ordens dobram a cada nível (m = 2): "2u, 4u, 8u, 16u"; "5U, 10U, 20U, 40U" | **confirmado** (help center oficial) | [Double first order](https://cointech2u.tawk.help/article/double-first-order-button), [Change first order size](https://cointech2u.tawk.help/article/what-would-happen-to-my-trades-if-i-change-the-first-order-size) |
| "Double First" dobra **só a 1ª ordem**: "4u, 4u, 8u, 16u"; existe para cumprir o mínimo da exchange | **confirmado** | mesmo artigo |
| Millionaire 9 = **9 níveis** ("Auto 9 Layer Up & Down", "9-layer DCA grid") | **confirmado** (material do vendedor) | [Four AI Strategies](https://www.cointech2vip.com/resource?id=25), [Vídeos](https://www.cointech2vip.com/videos) |
| Reabre a base após o TP ("Cycle Strategy") | **confirmado** | [Cycle Strategy](https://cointech2u.tawk.help/article/what-is-the-function-of-cycle-strategy) |
| "First Order Size" é **margem** ou **notional** ("u"/"U" = USDT, base não dita) | **NÃO confirmado** → premissa: margem (notional = 3 × 20 = $60); env `FIRST_ORDER_IS_NOTIONAL=true` lê como notional ($3) | artigos acima só dizem "5U" |
| "Stop Loss 180" = perda total em USDT (equity) vs % vs por moeda | **NÃO confirmado** → premissa: perda em $ desde o pico de equity, fecha tudo (coerente com o "Equity Guard" do vendedor) | [Change stop loss](https://cointech2u.tawk.help/article/how-to-change-stop-loss-value-for-an-activated-coin-which-is-currently-running), [Tutorial hub / Equity Guard](https://www.cointech2vip.com/resource?id=85) |
| Passo (%) entre adições e TP (%) da Strategy 9 | **NÃO encontrado** → premissa: `STEP_PERCENT=1.5`, `TP_PERCENT=1.0` (knobs atuais) | — |
| Tabela de multiplicador/passo **por nível** | **NÃO encontrado** → não implementado (só m fixo + double first) | — |

Observação: a leitura "notional" é a única em que a planilha fecha com a banca: o "Double First" existe para
cumprir o mínimo da exchange ($5 na Binance), o que só faz sentido se $3 é o valor da ordem (3 → 6 ≥ 5); com
margem ($60 → $120) a opção seria inútil. Ainda assim, isto é inferência, não confirmação.

### Envs exatas para a VM (ainda NÃO aplicadas)

```
TRADING_BOT_STRATEGY=martingale_hedge
TRADING_BOT_MARTINGALE_PAIRS=ADAUSDT,TRXUSDT
TRADING_BOT_MARTINGALE_LEVERAGE=20
TRADING_BOT_MARTINGALE_FIRST_ORDER_USDT=5              # planilha escalada (Bitget mín. $5); original 3
TRADING_BOT_MARTINGALE_FIRST_ORDER_IS_NOTIONAL=false   # premissa "margem"; true = ler como notional ($5)
TRADING_BOT_MARTINGALE_DOUBLE_FIRST=true
TRADING_BOT_MARTINGALE_STOP_LOSS_USDT=300              # escalado de 180 (× 5/3); 100% da banca de $300: start() loga aviso
TRADING_BOT_MARTINGALE_MAX_LEVELS=9
TRADING_BOT_MARTINGALE_MULTIPLIER=2.0
TRADING_BOT_MARTINGALE_STEP_PERCENT=1.5                # premissa (não achado)
TRADING_BOT_MARTINGALE_TP_PERCENT=1.0                  # premissa (não achado)
```

### Escada resultante (m = 2, 9 níveis, 1ª ordem dobrada) vs capital × alavancagem = $300 × 20 = $6.000

Pior caso = 2 pares × 2 pernas todas no nível máximo ao mesmo tempo (irreal: a perna oposta bate o TP na
mesma direção, mas é o teto); "2 pernas" = uma perna profunda por par.

**Leitura margem (`FIRST_ORDER_IS_NOTIONAL=false`, B = $60, 1ª ordem $120):**

| nível k | ordem | acumulado/perna | 2 pernas | 4 pernas (pior caso) |
|---|---|---|---|---|
| 0 | 120 | 120 | 240 | 480 |
| 1 | 120 | 240 | 480 | 960 |
| 2 | 240 | 480 | 960 | 1.920 |
| 3 | 480 | 960 | 1.920 | 3.840 |
| 4 | 960 | 1.920 | 3.840 | **7.680 — estoura** |
| 5 | 1.920 | 3.840 | **7.680 — estoura** | 15.360 |
| 6 | 3.840 | 7.680 | 15.360 | 30.720 |
| 7 | 7.680 | 15.360 | 30.720 | 61.440 |
| 8 | 15.360 | 30.720 | 61.440 | 122.880 |

**NÃO cabe:** estoura no 5º fill (k=4) com 4 pernas e no 6º (k=5) com 2 pernas; a escada completa precisa de
$30.720 de notional por perna (20× a banca). Alternativas, a decidir pelo usuário: `MAX_LEVELS=4` (4 pernas,
$3.840) ou `MAX_LEVELS=5` (só 2 pernas profundas, $3.840); ou ler a planilha como notional (abaixo).

**Leitura notional (`FIRST_ORDER_IS_NOTIONAL=true`, B = $3, 1ª ordem $6):**

| nível k | ordem | acumulado/perna | 2 pernas | 4 pernas (pior caso) |
|---|---|---|---|---|
| 0 | 6 | 6 | 12 | 24 |
| 1 | 6 | 12 | 24 | 48 |
| 2 | 12 | 24 | 48 | 96 |
| 3 | 24 | 48 | 96 | 192 |
| 4 | 48 | 96 | 192 | 384 |
| 5 | 96 | 192 | 384 | 768 |
| 6 | 192 | 384 | 768 | 1.536 |
| 7 | 384 | 768 | 1.536 | 3.072 |
| 8 | 768 | 1.536 | 3.072 | **6.144 — estoura por 2,4%** |

**Cabe** com 2 pernas profundas (3.072 = 51% de $6.000); no pior caso de 4 pernas cheias passa de $6.000 só
no 9º fill (k=8), por $144. Margem da escada cheia: $1.536/20 = $76,80 por perna ≈ $307 nas 4 pernas — a banca
inteira, sem folga para o P&L negativo que a escada carrega nesse ponto (liquidação antes do stop de $180 é
possível). O stop de $180 = 60% da banca: o stop em % de hoje (20%) dispararia bem antes.

**Planilha escalada (B = $5, 1ª ordem $10):** multiplique as duas tabelas por 5/3 — 9ª perna $1.280,
acumulado $2.560/perna, 2 pernas $5.120 (cabe), 4 pernas $10.240 (estoura no 9º fill; no 8º, $5.120, cabe).
Stop $300 = 100% da banca de $300 (e 3× uma banca de $100): na prática a liquidação chega antes do stop.

minNotional: ADAUSDT/TRXUSDT têm $5 na Binance; a 1ª ordem ($6 notional ou $120 margem×lev) cumpre. Se uma
moeda da lista exigir mais, `start()` sobe a base para cumprir (log ⚠️) em vez de pular o par.

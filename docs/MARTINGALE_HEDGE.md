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
TRADING_BOT_MARTINGALE_PAIRS=ADAUSDT          # até 3 pares, separados por vírgula
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

## O que fica desligado sob esta estratégia

TP/SL/trailing por ordem, IA, kill switches (sequência negativa, win rate, drawdown do pullback), meta de
lucro diária, stop loss global, regime swap, scoring/rotação de pares. Mesmo gate do Donchian em `bot.py`
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
TRADING_BOT_MARTINGALE_FIRST_ORDER_USDT=3
TRADING_BOT_MARTINGALE_FIRST_ORDER_IS_NOTIONAL=false   # premissa "margem"; true = ler como notional ($3)
TRADING_BOT_MARTINGALE_DOUBLE_FIRST=true
TRADING_BOT_MARTINGALE_STOP_LOSS_USDT=180              # 60% da banca: start() loga aviso
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

minNotional: ADAUSDT/TRXUSDT têm $5 na Binance; a 1ª ordem ($6 notional ou $120 margem×lev) cumpre. Se uma
moeda da lista exigir mais, `start()` sobe a base para cumprir (log ⚠️) em vez de pular o par.

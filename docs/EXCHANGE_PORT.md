# Porte para outra exchange (perpétuos USDT) — OKX e Bitget

Contexto: a Binance bloqueou a API de futuros para contas brasileiras. O bot roda a estratégia
`martingale_hedge` (`trading_bot/core/martingale.py`) e toda a fala com a exchange passa pela fachada
`BinanceConnection` (`trading_bot/infra/binance_client.py` + streams, ~3.000 linhas). O porte adiciona
fachadas alternativas com **os mesmos nomes de método e os mesmos formatos de retorno**, escolhidas por env:

- `OkxConnection` (`trading_bot/infra/okx_client.py`) — desenho original, seções 1–6 abaixo.
- `BitgetConnection` (`trading_bot/infra/bitget_client.py`) — **escolhida para produção** (seção 7): ordens em
  moedas com passo fino, sem a distorção de contratos da OKX.

O caminho Binance não muda.

## 1. Como a conexão é criada e injetada

- `trading_bot/core/bot.py:211` (`TradingBot.__init__`) e `bot.py:1159` (troca de rede via `/env`) eram os
  dois únicos pontos que instanciavam `BinanceConnection()`. Agora chamam `create_exchange()`
  (`trading_bot/infra/exchange.py`), que lê `config.EXCHANGE` (`binance` | `okx` | `bitget`) e devolve a
  fachada correspondente.
- Todo o resto do bot recebe `bot.exchange` (duck typing; vários callers usam `hasattr`). Scripts Binance-only
  (`scripts/test_connection.py`, `backfill_real_pnl.py`, `reconcile_phantom_trades.py`) seguem com
  `BinanceConnection` explícito — não foram tocados.

## 2. Métodos que o caminho martingale + dashboard + telegram consomem

Formato de retorno é o que o **caller** lê (já normalizado pela fachada; nada de `positionAmt`/`unRealizedProfit`
cru chega ao bot). Conversões gerais (todas internas à `OkxConnection`):

| Conceito | Binance (interno, mantido em todo o bot) | OKX v5 |
|---|---|---|
| Símbolo | `ADAUSDT` | `ADA-USDT-SWAP` (`to_inst_id`/`from_inst_id`) |
| Quantidade | moedas (`34.0` ADA) | contratos: `sz = floor(qty / ctVal / lotSz) × lotSz` (ADA `ctVal=100`, `lotSz=0.1` → 34 ADA = 0,3 ct = 30 ADA). Volta: `coins = pos × ctVal` |
| Lado | `positionSide` LONG/SHORT + `side` BUY/SELL | `posSide` long/short + `side` buy/sell, `tdMode=cross` |
| minNotional | filtro `MIN_NOTIONAL.notional` | derivado: `minSz × ctVal × último preço` |
| quantityPrecision | decimais do `stepSize` | decimais de `lotSz × ctVal` (ADA/TRX = 0, BNB = 2, ETH = 1) |
| Candles | asc, `[openTime,o,h,l,c,vol,closeTime,quoteVol,…]` | desc, `[ts,o,h,l,c,vol(ct),volCcy(moeda),volCcyQuote,confirm]` → invertido e remapeado |
| Intervalo | `4h`, `1d` | `4H`, `1Dutc` (`1D` da OKX abre em UTC+8; `1Dutc` = dia UTC como na Binance) |

| Método (caller) | Campos consumidos | Endpoint OKX v5 | Conversão / nota |
|---|---|---|---|
| `get_open_positions(force_refresh)` (martingale `tick`, `close_position`, dashboard, telegram) | lista de `{symbol, side 'LONG'/'SHORT', quantity, entry_price, mark_price, unrealized_pnl, leverage}`; **propaga exceção** em erro (não devolve `[]`) | `GET /account/positions?instType=SWAP` | `pos≠0`; `side` do `posSide` (ou sinal de `pos` em net); `quantity = |pos|×ctVal`; `avgPx`, `markPx`, `upl`, `lever` |
| `get_account_balance()` / `get_available_balance()` (martingale equity, telegram) | float USDT | `GET /account/balance?ccy=USDT` | `details[USDT].cashBal` (= walletBalance, sem upl) / `availBal`; cap `SIMULATED_BALANCE_USD` em testnet igual à Binance |
| `get_account_info()` (dashboard, kill switch, drawdown) | `{wallet_balance, available_balance, unrealized_pnl, margin_balance, total_initial_margin}` | idem | `cashBal`, `availBal`, `upl`, `eq`, `imr`; fail-open `{0…}` |
| `set_hedge_mode()` (setup) | bool | `GET /account/config` → `posMode`; `POST /account/set-position-mode {posMode: long_short_mode}` | falha com posição aberta, como na Binance |
| `set_leverage(symbol, lev)` (martingale `_add_pair`, telegram) | bool | `POST /account/set-leverage {instId, lever, mgnMode: cross}` | **cross não leva `posSide`** (doc OKX: `posSide` só em isolated long/short); vale para as duas pernas |
| `place_market_order(symbol, side, position_side, quantity)` (martingale `_open`, `close_position`, telegram) | dict com `avgPrice` (obrigatório p/ `_fill_price`), `executedQty`, `orderId`; `None` em falha | `POST /trade/order {instId, tdMode: cross, side, posSide, ordType: market, sz}` + `GET /trade/order?instId&ordId` (até 3×) | `sz` em contratos; `avgPrice=avgPx`, `executedQty=accFillSz×ctVal`; `sCode≠0` → `None`. Se o floor der 0 ct mas `qty>0`, sobe para `minSz` (o caller já garantiu notional ≥ minNotional no preço dele; ver riscos) |
| `close_position(symbol, side)` | ordem dict ou `None` | reutiliza os dois acima | qty da posição em moedas → contratos exatos (múltiplo de lotSz) |
| `get_symbol_info(symbol)` (martingale start/`_add_pair`, telegram) | `minNotional`, `quantityPrecision`, `pricePrecision`, `minQty` | `GET /public/instruments?instType=SWAP` (cache 6h) + `GET /market/ticker` (preço vivo) | extras `ctVal, lotSz, minSz, stepSize, tickSize`; `{}` se não existe |
| `get_exchange_info()` (telegram `/coins`) | `symbols[].symbol/status 'TRADING'/contractType 'PERPETUAL'/quoteAsset` | `GET /public/instruments?instType=SWAP` | só `*-USDT-SWAP`; `state=='live'` → `TRADING` |
| `get_klines_raw(symbol, interval, limit)` (score da planilha: `score_pair(d1, h4)` lê `[1..5]` e `[7]`) | lista de listas Binance-like, asc, última = vela aberta | `GET /market/candles?instId&bar&limit` (≤300/página; pagina por `after`) | `volume = volCcy` (moedas), `quoteVolume = volCcyQuote`, `closeTime = ts + intervalo − 1` |
| `get_klines(symbol, interval, limit)` | lista de dicts `{timestamp, open, high, low, close, volume}` só de velas fechadas | idem | `confirm == '1'` |
| `get_symbol_ticker(symbol)` (martingale tick) / `get_symbol_price` / `get_current_price` | `{'price', 'time'}` / float | `GET /market/ticker?instId` | `last`, `ts` |
| `get_income_history(income_type, limit, symbol, start_time, raise_errors)` (P&L realizado por trade, reconciler, relatório) | lista asc de `{incomeType, income, symbol, time, asset}` | `GET /account/bills` (7 d) ou `/account/bills-archive` (3 m), `type=2` (trade) / `8` (funding), pagina por `after=billId` | bill tipo 2 vira `REALIZED_PNL` (`pnl≠0`) **e** `COMMISSION` (`fee`); tipo 8 vira `FUNDING_FEE` (`balChg`) |
| `get_daily_pnl_from_binance()` / `get_cumulative_income_from_binance(start_ms)` (dashboard, telegram, RiskManager) | `{realized_pnl, funding_fee, commission, total, income_count, income_types}` / `{funding_fee, commission, realized_pnl, income_count}` | agregação do anterior | nome mantido (callers não mudam); cache 30 s / 300 s |
| `get_funding_rate(symbol)` (pair_selector; não martingale) | `{rate, rate_percent, long_pays, short_pays, next_funding_time, favorable_side}` | `GET /public/funding-rate?instId` | fail-open neutro |
| `pop_realized_close(symbol, side, since_ms)` (bot `_close_position_with_notification`) | `{gross, exit_price, qty}` ou `None` | **fase 1: sem WebSocket** — `place_market_order` detecta ordem de fechamento (buy+short / sell+long) e guarda `pnl`, `avgPx`, `accFillSz` do `GET /trade/order` no buffer | fechamentos externos (manuais na OKX) caem no 2º caminho, income REST (`REALIZED_PNL` dos bills), como já acontece na Binance |
| `cancel_open_orders(symbol)` (bot `close_position`) | bool | `GET /trade/orders-pending?instId` → `POST /trade/cancel-batch-orders` | só ordens normais; o martingale não coloca algo-orders (stop) |
| `get_commission_rates(symbol)` (setup) | `{maker_rate, taker_rate, maker_percent, taker_percent}` | `GET /account/trade-fee?instType=SWAP` | OKX devolve negativo (`"-0.0005"`) → `abs` |
| `get_account_trades(symbol, start, end, limit)` (reconciler) | lista asc de `{id, time, price, qty, realizedPnl, commission, side, positionSide, orderId}` | `GET /trade/fills-history?instType=SWAP&instId&begin&end` | `qty = fillSz × ctVal`; `commission = abs(fee)` |
| `shutdown`, `flush_retry_stats`, `get_retry_stats_report`, `get_order_stats_report`, `get_symbol_cooldown_info`, `invalidate_*_cache`, `get_ws_stats`, `subscribe_klines_stream` | — | — | callers usam `hasattr`; a OkxConnection só define `shutdown`, `get_symbol_cooldown_info` (→ `None`) e `invalidate_*_cache` (no-op), porque esses são chamados sem guarda no caminho martingale |

## 3. Decisões

- **Seleção**: `TRADING_BOT_EXCHANGE=binance|okx` (default `binance`). Nada muda sem a env.
- **Credenciais OKX**: `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_PASSPHRASE`; `OKX_DEMO=true` (default **true**)
  manda o header `x-simulated-trading: 1` e exige chaves criadas em *Demo trading*. Com `okx`, `config.validate()`
  cobra essas três envs em vez das da Binance. Recomendado rodar demo com `TRADING_BOT_ENVIRONMENT=testnet`
  (state/DB/lock separados e `SIMULATED_BALANCE_USD` funcionando) e produção com `mainnet` + `OKX_DEMO=false`.
- **Símbolos**: formato interno `ADAUSDT` em todo o bot, state, DB e Telegram. Tradução só dentro da `OkxConnection`.
- **Margem**: cross. **Alavancagem**: por `instId` em cross (uma chamada cobre long e short).
- **Biblioteca**: `requests` puro (já é dependência) + HMAC-SHA256 base64 (OKX v5: `ts ISO ms + MÉTODO + path?query + body`;
  headers `OK-ACCESS-KEY/SIGN/TIMESTAMP/PASSPHRASE`). `ccxt` não está instalado e não foi adicionado.
- **Sem WebSocket na fase 1** (klines e user stream): REST a cada tick. O martingale só lê ticker + posições
  por tick (≈ 2 + N chamadas / `CHECK_INTERVAL`), dentro dos limites da OKX (10 req/2 s por endpoint privado).
- **Alternativa rejeitada**: herdar de `BinanceConnection` e trocar só as chamadas. O `__init__` dela constrói
  o `Client` python-binance, o WS store e o user stream; e os caches/cooldowns são acoplados a códigos de erro
  Binance. Um adaptador independente é menor e não arrisca o caminho Binance.

## 4. O que NÃO entra na fase 1 e como cada caller degrada

| Fora | Caller | Degradação |
|---|---|---|
| Stream WS de klines (`subscribe_klines_stream`, `get_ws_stats`) | `bot._sync_ws_subscriptions`, métricas | `hasattr` → pulado; `get_klines` vai de REST |
| User stream WS (`get_user_stream_stats`, invalidação em tempo real) | métricas, caches | `hasattr` → pulado; `pop_realized_close` vem do fill da própria ordem |
| `pair_selector` Donchian/pullback (`get_order_book`, `get_ticker_24h`, `get_all_tickers_24h`, `get_reference_liquidity_map`, `get_market_exchange_info`) | só estratégias pullback/donchian | não definidos; com `STRATEGY=martingale_hedge` ninguém chama. Rodar pullback/donchian na OKX quebra na seleção de pares (AttributeError) — fase 2 |
| `set_stop_loss_take_profit` | pullback/donchian | idem |
| Histórico de funding/comissão fora de 3 meses (`bills-archive`) | `get_cumulative_income_from_binance(lookback 200 d)` | janela truncada a 3 meses; log |
| Cooldown por símbolo e contagem de retries/ordens | `execution/engine.py` (pullback), relatório de API | `get_symbol_cooldown_info` → `None`; relatórios usam `hasattr` |
| Telegram `/env` (troca testnet↔mainnet) | `bot.switch_environment` | continua trocando `config.ENVIRONMENT` e recria a conexão via fábrica; na OKX só muda os paths de runtime, **não** o modo demo (`OKX_DEMO` é env fixa) |

## 5. Fase 2 (não feita)

WebSocket privado `orders`/`positions` (substituir o buffer de fills por push), `candle` público, `pair_selector`
para OKX (tickers 24h, book), cooldown por código de erro OKX (`51xxx`), `bills-archive` paginado além de 3 meses.

## 6. Riscos vistos

1. **Granularidade dos contratos distorce a progressão**: ADA (`ctVal=100`, `lotSz=0.1`) e TRX (`ctVal=1000`,
   `lotSz=0.01`) só aceitam múltiplos de 10 moedas (≈ $5 / ≈ $3); BNB (`ctVal=0.01`, `minSz=1`) tem mínimo de
   1 contrato (≈ $9 — a base de $3 da planilha **não cabe**; o runner sobe a base para o minNotional). Com base
   $5–$6, o nível k arredondado para baixo pode ficar bem abaixo de `B×2^k` (ex.: $10 a 0,52 = 19,2 ADA → 10 ADA,
   metade). Mitigação: `TRADING_BOT_MARTINGALE_FIRST_ORDER_USDT` maior ou pares com passo pequeno; conferir no
   demo antes de ligar.
2. **Ordem mínima quando o preço sobe**: o caller calcula `minNotional` no preço do start; se o preço subir, a
   base pedida fica < `minSz` em contratos. A `OkxConnection` sobe para `minSz` em vez de falhar para sempre
   (documentado no código). Em closes a qty é a própria posição, múltiplo exato — nunca estoura.
3. **P&L histórico**: o DB SQLite (fonte de verdade do dashboard) fica com o histórico Binance; na OKX o
   `first_trade_time_ms` ancora o acumulado de funding/comissão nos bills da OKX. Recomendado começar com DB
   novo (`TRADING_BOT_ENVIRONMENT` distinto ou reset).
4. **Funding/comissão nos bills**: comissão vem no mesmo bill do trade (`fee`), funding em bill tipo 8 (`balChg`).
   Validar no demo que os sinais batem com o app da OKX antes de confiar no card de P&L.
5. **`1Dutc` vs `1D`**: a planilha usa candle diário; usei `1Dutc` para bater com a Binance (dia UTC). Se a
   planilha original for em UTC+8, trocar o mapa em `okx_client.BAR_MAP`.
6. **Demo ≠ produção**: fills e liquidez do demo são sintéticos; o header `x-simulated-trading` em chaves de
   produção dá `50xxx`. Chave de produção precisa de permissão *Trade* e IP whitelist da VM.

## 7. Bitget (futuros USDT-M, API v2 "classic") — `BitgetConnection`

Mesmo subconjunto de métodos e formatos da seção 2; o que muda é a tabela de conversão. Conferido na doc
pública (`/api-doc/contract/intro`) e em `GET /api/v2/mix/market/contracts` ao vivo em 11/11/26.

| Conceito | Binance (interno) | Bitget v2 |
|---|---|---|
| Símbolo | `ADAUSDT` | `ADAUSDT` (igual). **Demo**: `SADASUSDT` + `productType=SUSDT-FUTURES` + `marginCoin=SUSDT` (prefixo encapsulado em `to_bitget`/`from_bitget`) |
| Quantidade | moedas | **moedas** (`size`), floor no `sizeMultiplier` (ADA/TRX `1`, BNB `0.01`), mínimo `minTradeNum`; abaixo do mínimo por deriva de preço sobe para `minTradeNum` (mesmo shortcut da OKX) |
| minNotional | `MIN_NOTIONAL.notional` | `minTradeUSDT` (= $5 em todos os contratos vistos) |
| stepSize / tickSize | filtros | `sizeMultiplier` / `priceEndStep × 10^-pricePlace`; `quantityPrecision = volumePlace`, `pricePrecision = pricePlace`; extra `maxLeverage = maxLever` (ADA 75) |
| Lado em hedge | `side` BUY/SELL + `positionSide` | **`side` = direção da POSIÇÃO** (`buy`=long, `sell`=short) e `tradeSide` = `open`/`close`. Fechar LONG é `side=buy, tradeSide=close` (não `sell`) |
| Margem | cross | `marginMode=crossed` na ordem; `set_leverage` chama `set-margin-mode crossed` antes (falha com posição aberta → segue no modo atual, com warning) e `set-leverage {leverage}` sem `holdSide` |
| Hedge mode | `set_hedge_mode` | `POST /account/set-position-mode {posMode: hedge_mode}` (idempotente; falha com posição aberta) |
| Candles | asc Binance | `GET /market/candles` **já ascendente**, `[ts,o,h,l,c,baseVol,quoteVol]` (7 campos), `limit ≤ 1000` numa página (shortcut: sem paginação por `history-candles`); `1d` → `1Dutc` |
| Ticker | `{price, time}` | `GET /market/ticker` → `lastPr`, `ts` |
| Conta | wallet / available / upl | `GET /account/accounts` → item `marginCoin==USDT`: `wallet = accountEquity − unrealizedPL`, `available`, `margin_balance = accountEquity`, `total_initial_margin = crossedMargin` |
| Posições | lista normalizada | `GET /position/all-position?productType&marginCoin` → `holdSide`, `total`, `openPriceAvg`, `markPrice`, `unrealizedPL`, `leverage` |
| Fill da ordem | `avgPrice`, `executedQty` | `POST /order/place-order` → `orderId`; `GET /order/detail` (3×) → `priceAvg`, `baseVolume`, `state`, `totalProfits` (gross do close), `fee` |
| Income | `REALIZED_PNL` / `COMMISSION` / `FUNDING_FEE` | `GET /account/bill` (90 dias; janelas de ≤30 dias, paginação `idLessThan=endId`): `businessType` `close_*` → `REALIZED_PNL=amount` + `COMMISSION=fee`; `open_*` → `COMMISSION=fee`; `contract_settle_fee` → `FUNDING_FEE=amount`; transferências ignoradas. Sem filtro de símbolo na API → filtrado aqui |
| Fills (reconciler) | userTrades | `GET /order/fills` (3 meses) → `fillList`: `price`, `baseVolume`, `profit`, `totalFee` (abs), `tradeScope`; `positionSide` do `side` e BUY/SELL do `tradeSide` |
| Taxas | `get_commission_rates` | do próprio contrato (`makerFeeRate 0.0002`, `takerFeeRate 0.0006`) — sem endpoint privado |
| Funding | `get_funding_rate` | `GET /market/current-fund-rate` → `fundingRate`, `nextUpdate` |
| Cancelar ordens | `cancel_open_orders` | `GET /order/orders-pending?symbol` → `POST /order/batch-cancel-orders {symbol, orderIdList}` |
| Autenticação | HMAC | `ts ms + MÉTODO + path?query + body`, HMAC-SHA256 base64; headers `ACCESS-KEY/SIGN/TIMESTAMP/PASSPHRASE`, `locale`; sucesso = `code "00000"` |
| Demo | — | header `paptrading: 1` + chave criada em *Demo trading* + `SUSDT-FUTURES`. **Só existem `SBTCSUSDT`, `SETHSUSDT`, `SXRPSUSDT`** — ADA/TRX/BNB não têm demo (`get_symbol_info` → `{}`) |

Envs: `TRADING_BOT_EXCHANGE=bitget`, `BITGET_API_KEY`, `BITGET_API_SECRET`, `BITGET_PASSPHRASE`, `BITGET_DEMO`
(default `true`). `config.validate()` cobra as três chaves. Smoke: `scripts/bitget_smoke.py` (ordem só com `--order`).

Diferenças práticas vs OKX: (1) sem contratos — ADA/TRX a 1 moeda e BNB a 0,01 cabem na progressão da planilha
com base ≥ $5 (`minTradeUSDT`); (2) `minNotional` fixo $5 em vez de derivado do preço; (3) demo cobre só 3
moedas, então o teste de ponta a ponta com ADA/TRX só acontece em produção com ordem mínima.

Riscos específicos: `close_position` usa ordem market `tradeSide=close` (não `flash-close-position`) para ter
`orderId`/fill; se o close exceder a posição por ordens limit pendentes a Bitget ajusta a quantidade. Sinais de
`amount`/`fee` nos bills e `totalFee` nos fills foram assumidos pela doc (fee negativo) — validar no 1º dia. O
mapeamento de `businessType` ignora tipos sem `open`/`close` (ADL `adl_close_*` e liquidação `force_close_*`
entram como realizado). Formato de candles do demo não foi testado ao vivo.

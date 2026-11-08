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

## O que fica desligado sob esta estratégia

TP/SL/trailing por ordem, IA, kill switches (sequência negativa, win rate, drawdown do pullback), meta de
lucro diária, stop loss global, regime swap, scoring/rotação de pares. Mesmo gate do Donchian em `bot.py`
(`strategy_runner is not None`).

## Limites conhecidos

- O registro de abertura no SQLite guarda preço/qty da **base**; o P&L do fechamento usa a média e a quantidade
  reais da exchange (corretos). Adições não geram linha própria.
- Estado da perna (nível, último fill) vive em memória; após restart o nível é inferido do notional na exchange
  e o próximo gatilho parte do preço atual.
- Não há fechamento intrabar: o gatilho é avaliado no intervalo do monitor, a mercado (slippage real > simulado).

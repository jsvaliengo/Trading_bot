# Prometheus Queries — Trading Bot

Cheatsheet de PromQL úteis pra inspecionar o bot em runtime. Acesso: **http://127.0.0.1:9091/graph**.

> **Nota**: Prometheus guarda métricas, não logs de texto. Cada query aqui retorna valor instantâneo (tabela) ou série temporal (graph). Use "+ Add query" no Prometheus UI pra rodar múltiplas ao mesmo tempo.

## 🟢 Status operacional

```promql
# Bot rodando? (1 = ON, 0 = OFF)
trading_bot_running

# Bot pausado?
trading_bot_paused

# Rede ativa (testnet/mainnet) + app_env
trading_bot_info

# Quantas posições abertas agora?
trading_bot_positions_open_count

# "Está tudo bem?" — one-liner de saúde
(trading_bot_running == 1) * (trading_bot_drawdown_from_peak_percent < 15) * (trading_bot_positions_open_count < 12)
```

## 💵 P&L

```promql
# Saldo da carteira
trading_bot_account_balance_usd

# P&L realizado total (desde início do bot process)
trading_bot_pnl_realized_total_usd

# P&L realizado hoje
trading_bot_pnl_realized_daily_usd

# Drawdown atual do pico (%)
trading_bot_drawdown_from_peak_percent

# Pico histórico de equity
trading_bot_peak_equity_usd

# Taxas acumuladas pagas
trading_bot_fees_paid_total_usd
```

## 🎯 P&L por motivo de fechamento

```promql
# P&L somado POR MOTIVO DE FECHAMENTO (acumulado desde restart)
sum by (close_reason) (trading_bot_trades_pnl_usd_total{result="win"})
sum by (close_reason) (trading_bot_trades_pnl_usd_total{result="loss"})

# Total perdido especificamente em SL
-sum(trading_bot_trades_pnl_usd_total{result="loss", close_reason="stop_loss"})

# Total ganho em TP
sum(trading_bot_trades_pnl_usd_total{result="win", close_reason="take_profit"})

# Net P&L via Trailing Stop (tolera um lado vazio)
(sum(trading_bot_trades_pnl_usd_total{close_reason="trailing_stop", result="win"}) or vector(0))
  - (sum(trading_bot_trades_pnl_usd_total{close_reason="trailing_stop", result="loss"}) or vector(0))

# Contagem de trades por motivo
sum by (close_reason) (trading_bot_trades_closed_total)
```

## 📊 Estatísticas de trades

```promql
# Total de trades fechados
sum(trading_bot_trades_closed_total)

# Win/Loss counts
trading_bot_trades_win_count
trading_bot_trades_loss_count

# Win rate instantâneo (%)
100 * trading_bot_trades_win_count / (trading_bot_trades_win_count + trading_bot_trades_loss_count)

# Win rate janela 1h
100 * sum(rate(trading_bot_trades_closed_total{result="win"}[1h]))
    / sum(rate(trading_bot_trades_closed_total[1h]))

# Taxa de trades fechados (por minuto, janela 5min)
60 * sum(rate(trading_bot_trades_closed_total[5m]))

# Top 10 pares por número de trades
topk(10, sum by (symbol) (trading_bot_trades_closed_total))

# Net P&L por estratégia (tolera um lado vazio)
(sum by (strategy) (trading_bot_trades_pnl_usd_total{result="win"}) or on() vector(0))
  - (sum by (strategy) (trading_bot_trades_pnl_usd_total{result="loss"}) or on() vector(0))
```

## ⚡ Cache — hit rate por método

```promql
# Hit rate do balance (meta > 70%)
100 * rate(trading_bot_cache_hits_total{method="balance"}[5m])
    / clamp_min(rate(trading_bot_cache_hits_total{method="balance"}[5m])
       + rate(trading_bot_cache_misses_total{method="balance"}[5m]), 1e-9)

# Hit rate do funding_rate (meta > 95%)
100 * rate(trading_bot_cache_hits_total{method="funding_rate"}[5m])
    / clamp_min(rate(trading_bot_cache_hits_total{method="funding_rate"}[5m])
       + rate(trading_bot_cache_misses_total{method="funding_rate"}[5m]), 1e-9)

# Hit rate do klines (WebSocket vs REST fallback — meta > 90%)
100 * rate(trading_bot_cache_hits_total{method="klines_ws"}[5m])
    / clamp_min(rate(trading_bot_cache_hits_total{method="klines_ws"}[5m])
       + rate(trading_bot_cache_misses_total{method="klines_ws"}[5m]), 1e-9)

# Hit rate do daily_pnl (meta > 90%)
100 * rate(trading_bot_cache_hits_total{method="daily_pnl"}[5m])
    / clamp_min(rate(trading_bot_cache_hits_total{method="daily_pnl"}[5m])
       + rate(trading_bot_cache_misses_total{method="daily_pnl"}[5m]), 1e-9)

# Hits e misses por método (rate 5min)
sum by (method) (rate(trading_bot_cache_hits_total[5m]))
sum by (method) (rate(trading_bot_cache_misses_total[5m]))

# Diagnóstico de miss em klines (sub-motivo: stale / store_disabled / insufficient_buffer)
sum by (method) (rate(trading_bot_cache_misses_total{method=~"klines_ws:.*"}[5m]))
```

## 📡 WebSocket health

```promql
# Quantos streams ativos (esperado: N pares × 2 TFs)
trading_bot_ws_subscriptions_active

# Idade da última mensagem por stream (ALERTA se > 30s)
trading_bot_ws_stream_age_seconds

# Streams com problema (age > 30s = WS desconectado, caindo em REST)
trading_bot_ws_stream_age_seconds > 30

# Mensagens recebidas por stream (útil pra ver quem tá ativo)
trading_bot_ws_stream_messages_total

# Buffer atual (seed = 400; estável entre 400-1000)
trading_bot_ws_stream_buffer_size

# Stream mais stale no momento
topk(3, trading_bot_ws_stream_age_seconds)

# Rate de mensagens por stream (útil pra detectar streams lentos)
rate(trading_bot_ws_stream_messages_total[1m])
```

## 🔌 API Binance

```promql
# Erros de API por endpoint (últimos 5min)
sum by (endpoint) (rate(trading_bot_binance_api_errors_total[5m]))

# Ordens por lado e resultado na última hora
sum by (side, result) (increase(trading_bot_orders_placed_total[1h]))

# Taxa de sucesso de ordens (%)
100 * rate(trading_bot_orders_placed_total{result="success"}[5m])
    / clamp_min(rate(trading_bot_orders_placed_total[5m]), 1e-9)
```

## 🚨 Diagnóstico rápido

```promql
# Quais streams tão com problema AGORA
trading_bot_ws_stream_age_seconds > 30

# Quanto já perdi em SL (USD)
-sum(trading_bot_trades_pnl_usd_total{result="loss", close_reason="stop_loss"})

# Quanto ganhei em TP (USD)
sum(trading_bot_trades_pnl_usd_total{result="win", close_reason="take_profit"})

# Listar TODAS as métricas do bot
{__name__=~"trading_bot.*"}
```

## Dicas de uso

- **`rate()` precisa de janela > scrape_interval**. Com scrape_interval=15s, mínimo seguro é `[1m]`. Janela `[5m]` é suave, `[1h]` ótima pra dashboards.
- **Counters zeram no restart** — o `rate()` detecta isso automaticamente, mas query absoluta (`sum(...)`) reflete só desde o último restart.
- **Labels novas** (como `close_reason`) só existem em séries pós-restart. Séries antigas continuam no Prometheus mas com label vazia.
- **`or vector(0)`** nas queries de net P&L tolera lado vazio (ex: só wins sem losses), evitando painel mostrar "No data".
- **Comentários `#`** só servem como referência — Prometheus não aceita múltiplas queries na mesma execução. Use "+ Add query" ou rode uma de cada vez.

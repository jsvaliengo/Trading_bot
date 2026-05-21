"""
Métricas Prometheus do trading bot.

Exporter HTTP embutido (biblioteca prometheus_client) exposto em /metrics.
Grafana e Prometheus fazem scrape nessa URL.

Uso:
    from trading_bot.observability import metrics
    metrics.start_exporter(host="127.0.0.1", port=9090)
    metrics.update_bot_state(bot)
    metrics.record_trade_closed(symbol="ETHUSDT", strategy="hedge", result="win", pnl_usd=1.23, fees_usd=0.05)
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from prometheus_client import Counter, Gauge, Info, start_http_server

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Info estática (labels — rede, app_env)
# ---------------------------------------------------------------------------
bot_info = Info("trading_bot_info", "Metadados estáticos do bot (rede, app_env)")

# ---------------------------------------------------------------------------
# Estado (gauges atualizados periodicamente a partir do bot)
# ---------------------------------------------------------------------------
bot_running = Gauge("trading_bot_running", "1 se o bot está rodando, 0 caso contrário")
bot_paused = Gauge("trading_bot_paused", "1 se o bot está pausado")
daily_target_reached = Gauge(
    "trading_bot_daily_target_reached",
    "1 se atingiu meta diária (lucro ou perda) e parou de abrir posições",
)

positions_open_count = Gauge(
    "trading_bot_positions_open_count", "Número de posições abertas no momento"
)
position_pnl_unrealized_usd = Gauge(
    "trading_bot_position_pnl_unrealized_usd",
    "PNL não realizado por posição, em USD",
    ["symbol", "side"],
)
position_notional_usd = Gauge(
    "trading_bot_position_notional_usd",
    "Notional (preço × quantidade) por posição aberta, em USD",
    ["symbol", "side"],
)

pnl_realized_total_usd = Gauge(
    "trading_bot_pnl_realized_total_usd",
    "PNL realizado acumulado desde o início do bot, em USD",
)
pnl_realized_daily_usd = Gauge(
    "trading_bot_pnl_realized_daily_usd", "PNL realizado no dia corrente, em USD"
)
account_balance_usd = Gauge(
    "trading_bot_account_balance_usd", "Saldo disponível da conta futures, em USD"
)
peak_equity_usd = Gauge(
    "trading_bot_peak_equity_usd", "Pico histórico de equity registrado pelo bot"
)
drawdown_from_peak_percent = Gauge(
    "trading_bot_drawdown_from_peak_percent",
    "Drawdown atual em relação ao pico de equity (%)",
)
fees_paid_total_usd = Gauge(
    "trading_bot_fees_paid_total_usd", "Taxas acumuladas pagas desde o início, em USD"
)
trades_win_count = Gauge("trading_bot_trades_win_count", "Total de trades ganhadores")
trades_loss_count = Gauge("trading_bot_trades_loss_count", "Total de trades perdedores")

# ---------------------------------------------------------------------------
# Eventos (counters incrementados nos pontos de execução)
# ---------------------------------------------------------------------------
trades_closed_total = Counter(
    "trading_bot_trades_closed_total",
    "Total de trades fechados, rotulados por resultado/estratégia/símbolo/motivo",
    ["result", "strategy", "symbol", "close_reason"],
)
trades_pnl_usd_total = Counter(
    "trading_bot_trades_pnl_usd_total",
    "Soma absoluta do PNL de trades fechados. Use com result=win/loss e close_reason=stop_loss/take_profit/etc.",
    ["result", "close_reason"],
)
trades_fees_usd_total = Counter(
    "trading_bot_trades_fees_usd_total",
    "Soma das taxas (taker + funding etc.) pagas em trades fechados, por estratégia/símbolo. "
    "Útil pra ver custo total operacional no Grafana.",
    ["strategy", "symbol"],
)
orders_placed_total = Counter(
    "trading_bot_orders_placed_total",
    "Total de ordens enviadas, rotuladas por lado e resultado",
    ["side", "result"],
)
binance_api_errors_total = Counter(
    "trading_bot_binance_api_errors_total",
    "Erros retornados pela Binance (pós-retries), por endpoint e código",
    ["endpoint", "code"],
)

cache_hits_total = Counter(
    "trading_bot_cache_hits_total",
    "Cache hits nos endpoints Binance com TTL",
    ["method"],
)
cache_misses_total = Counter(
    "trading_bot_cache_misses_total",
    "Cache misses nos endpoints Binance com TTL (força fetch)",
    ["method"],
)

# ---------------------------------------------------------------------------
# WebSocket streams (kline)
# ---------------------------------------------------------------------------
ws_subscriptions_active = Gauge(
    "trading_bot_ws_subscriptions_active",
    "Número de subscrições WebSocket kline ativas",
)
ws_stream_age_seconds = Gauge(
    "trading_bot_ws_stream_age_seconds",
    "Idade em segundos da última mensagem recebida por stream",
    ["symbol", "interval"],
)
ws_stream_messages_total = Gauge(
    "trading_bot_ws_stream_messages_total",
    "Mensagens recebidas por stream (desde o start do bot)",
    ["symbol", "interval"],
)
ws_stream_buffer_size = Gauge(
    "trading_bot_ws_stream_buffer_size",
    "Quantidade de velas atualmente armazenadas no buffer do stream",
    ["symbol", "interval"],
)

# ---------------------------------------------------------------------------
# WebSocket user stream (ACCOUNT_UPDATE / ORDER_TRADE_UPDATE)
# ---------------------------------------------------------------------------
user_stream_started = Gauge(
    "trading_bot_user_stream_started",
    "1 se o user stream está conectado, 0 caso contrário",
)
user_stream_messages_total = Gauge(
    "trading_bot_user_stream_messages_total",
    "Total de mensagens recebidas via user stream desde o start do bot",
)
user_stream_account_updates_total = Gauge(
    "trading_bot_user_stream_account_updates_total",
    "Total de eventos ACCOUNT_UPDATE recebidos",
)
user_stream_order_updates_total = Gauge(
    "trading_bot_user_stream_order_updates_total",
    "Total de eventos ORDER_TRADE_UPDATE recebidos",
)
user_stream_errors_total = Gauge(
    "trading_bot_user_stream_errors_total",
    "Total de erros reportados pelo user stream",
)
user_stream_last_message_age_seconds = Gauge(
    "trading_bot_user_stream_last_message_age_seconds",
    "Idade em segundos da última mensagem do user stream",
)

# ---------------------------------------------------------------------------
# Startup do exporter
# ---------------------------------------------------------------------------
_exporter_lock = threading.Lock()
_exporter_started = False


def start_exporter(host: str, port: int) -> bool:
    """
    Sobe o HTTP exporter Prometheus em `host:port`.
    Idempotente — chamadas subsequentes são no-op.

    Returns:
        True se o exporter iniciou (ou já estava rodando), False em caso de falha.
    """
    global _exporter_started
    with _exporter_lock:
        if _exporter_started:
            return True
        try:
            start_http_server(port=int(port), addr=str(host))
            _exporter_started = True
            logger.info(f"📈 Prometheus exporter em http://{host}:{port}/metrics")
            return True
        except OSError as exc:
            logger.error(f"❌ Falha ao subir Prometheus exporter em {host}:{port}: {exc}")
            return False


def set_bot_info(environment: str, app_env: str) -> None:
    """Define labels estáticas do bot (rede ativa, app env)."""
    bot_info.info({"environment": str(environment or ""), "app_env": str(app_env or "")})


# ---------------------------------------------------------------------------
# Atualizadores de estado
# ---------------------------------------------------------------------------

def update_bot_state(bot: Any) -> None:
    """
    Snapshot do estado do bot nos gauges.
    Deve ser chamado dentro do loop de monitoramento (ticks periódicos).

    Lê atributos do bot de forma defensiva — nunca lança exceção.

    NOTA: posições abertas são atualizadas em update_positions() (fonte única:
    lista vinda de exchange.get_open_positions()). Não ler de bot.positions aqui.
    """
    try:
        bot_running.set(1 if getattr(bot, "running", False) else 0)
        bot_paused.set(1 if getattr(bot, "paused", False) else 0)
        daily_target_reached.set(1 if getattr(bot, "daily_target_reached", False) else 0)

        pnl_realized_total_usd.set(float(getattr(bot, "total_pnl", 0.0) or 0.0))
        pnl_realized_daily_usd.set(float(getattr(bot, "daily_realized_pnl", 0.0) or 0.0))
        peak_equity_usd.set(float(getattr(bot, "peak_equity", 0.0) or 0.0))
        fees_paid_total_usd.set(float(getattr(bot, "total_fees_paid", 0.0) or 0.0))
        trades_win_count.set(int(getattr(bot, "trades_win_count", 0) or 0))
        trades_loss_count.set(int(getattr(bot, "trades_loss_count", 0) or 0))
    except Exception as exc:
        logger.debug(f"metrics.update_bot_state falhou: {exc}")


def update_positions(positions: list[dict[str, Any]]) -> None:
    """
    Atualiza gauges por posição + contador total. Limpa labels antigas via `clear()`
    (senão posições fechadas ficariam reportando valores stale).

    Fonte única da verdade: lista vinda de exchange.get_open_positions().
    Mantém `trading_bot_positions_open_count` consistente com os gauges por posição.

    Args:
        positions: lista de dicts com keys: symbol, side, unrealized_pnl, quantity, mark_price
    """
    try:
        position_pnl_unrealized_usd.clear()
        position_notional_usd.clear()
        count = 0
        for pos in positions or []:
            symbol = str(pos.get("symbol", "UNKNOWN"))
            side = str(pos.get("side", "UNKNOWN"))
            pnl = float(pos.get("unrealized_pnl", 0.0) or 0.0)
            qty = abs(float(pos.get("quantity", 0.0) or 0.0))
            mark = float(pos.get("mark_price", pos.get("entry_price", 0.0)) or 0.0)
            position_pnl_unrealized_usd.labels(symbol=symbol, side=side).set(pnl)
            position_notional_usd.labels(symbol=symbol, side=side).set(qty * mark)
            count += 1
        positions_open_count.set(count)
    except Exception as exc:
        logger.debug(f"metrics.update_positions falhou: {exc}")


def update_account_balance(balance_usd: float) -> None:
    """Atualiza o saldo da conta futures."""
    try:
        account_balance_usd.set(float(balance_usd))
    except (TypeError, ValueError):
        pass


def update_drawdown(percent: float) -> None:
    """Atualiza o drawdown atual desde o pico (%)."""
    try:
        drawdown_from_peak_percent.set(float(percent))
    except (TypeError, ValueError):
        pass


# ---------------------------------------------------------------------------
# Eventos discretos
# ---------------------------------------------------------------------------

_CLOSE_REASON_KEYWORDS = [
    ("stop_loss", ("stop loss", "stop_loss", "sl ", "sl(")),
    ("take_profit", ("take profit", "take_profit", "tp ", "tp(")),
    ("trailing_stop", ("trailing",)),
    ("daily_target", ("meta", "daily", "limite de perda", "target")),
    ("pair_removed", ("par removido", "pair_removed")),
    ("external", ("externo", "external", "manual", "liquidação", "liquidation")),
]


def normalize_close_reason(raw: Any) -> str:
    """
    Reduz a string livre de motivo pra um dos rótulos canônicos:
    stop_loss, take_profit, trailing_stop, daily_target, pair_removed, external, other.
    """
    text = str(raw or "").strip().lower()
    if not text:
        return "other"
    for label, keywords in _CLOSE_REASON_KEYWORDS:
        for kw in keywords:
            if kw in text:
                return label
    return "other"


def record_trade_closed(
    symbol: str,
    strategy: str,
    result: str,
    pnl_usd: float,
    fees_usd: float = 0.0,
    close_reason: Any = None,
) -> None:
    """
    Incrementa counter de trades fechados. `result` deve ser 'win' ou 'loss'.
    `close_reason` pode ser string livre (será normalizada) ou None → "other".
    """
    try:
        result_norm = "win" if str(result).lower() == "win" else "loss"
        reason_norm = normalize_close_reason(close_reason)
        strategy_norm = str(strategy or "unknown")
        symbol_norm = str(symbol or "UNKNOWN")
        trades_closed_total.labels(
            result=result_norm,
            strategy=strategy_norm,
            symbol=symbol_norm,
            close_reason=reason_norm,
        ).inc()
        # Counter precisa de valor positivo — usa abs do pnl
        trades_pnl_usd_total.labels(
            result=result_norm, close_reason=reason_norm
        ).inc(abs(float(pnl_usd)))
        # Fees são sempre positivas — Counter aceita direto. Acumula ao longo
        # da vida do processo, pra inspeção no Grafana (rate() sobre janela).
        fees_value = max(0.0, float(fees_usd or 0.0))
        if fees_value > 0:
            trades_fees_usd_total.labels(
                strategy=strategy_norm, symbol=symbol_norm
            ).inc(fees_value)
    except Exception as exc:
        logger.debug(f"metrics.record_trade_closed falhou: {exc}")


def record_order(side: str, success: bool) -> None:
    """Incrementa counter de ordens enviadas."""
    try:
        orders_placed_total.labels(
            side=str(side or "unknown").upper(),
            result="success" if success else "failure",
        ).inc()
    except Exception as exc:
        logger.debug(f"metrics.record_order falhou: {exc}")


def record_api_error(endpoint: str, code: str | int) -> None:
    """Incrementa counter de erros da Binance (após esgotar retries)."""
    try:
        binance_api_errors_total.labels(
            endpoint=str(endpoint or "unknown"),
            code=str(code or "unknown"),
        ).inc()
    except Exception as exc:
        logger.debug(f"metrics.record_api_error falhou: {exc}")


def record_cache_hit(method: str) -> None:
    """Incrementa counter de cache hit (evitou uma chamada à API)."""
    try:
        cache_hits_total.labels(method=str(method or "unknown")).inc()
    except Exception as exc:
        logger.debug(f"metrics.record_cache_hit falhou: {exc}")


def record_cache_miss(method: str) -> None:
    """Incrementa counter de cache miss (forçou fetch na API)."""
    try:
        cache_misses_total.labels(method=str(method or "unknown")).inc()
    except Exception as exc:
        logger.debug(f"metrics.record_cache_miss falhou: {exc}")


def update_ws_stats(stats: Any) -> None:
    """
    Atualiza gauges de WebSocket a partir do snapshot retornado por
    BinanceConnection.get_ws_stats(). Chamar no tick de monitor.

    Aceita None (WS desligado) — apenas no-op nesse caso.
    """
    if not stats:
        return
    try:
        ws_subscriptions_active.set(int(stats.get("subscriptions", 0) or 0))
        # Limpa labels antigas antes de repopular (evita streams removidos virarem stale)
        ws_stream_age_seconds.clear()
        ws_stream_messages_total.clear()
        ws_stream_buffer_size.clear()
        for s in stats.get("streams", []) or []:
            sym = str(s.get("symbol", "?"))
            interval = str(s.get("interval", "?"))
            ws_stream_age_seconds.labels(symbol=sym, interval=interval).set(
                float(s.get("age_seconds", 0.0) or 0.0)
            )
            ws_stream_messages_total.labels(symbol=sym, interval=interval).set(
                int(s.get("messages", 0) or 0)
            )
            ws_stream_buffer_size.labels(symbol=sym, interval=interval).set(
                int(s.get("buffer_size", 0) or 0)
            )
    except Exception as exc:
        logger.debug(f"metrics.update_ws_stats falhou: {exc}")


def update_user_stream_stats(stats: Any) -> None:
    """
    Atualiza gauges do user stream a partir de BinanceConnection.get_user_stream_stats().
    Aceita None (stream desligado) → zera started.
    """
    try:
        if not stats:
            user_stream_started.set(0)
            return
        user_stream_started.set(1 if stats.get("started") else 0)
        user_stream_messages_total.set(int(stats.get("message_count", 0) or 0))
        user_stream_account_updates_total.set(int(stats.get("account_update_count", 0) or 0))
        user_stream_order_updates_total.set(int(stats.get("order_update_count", 0) or 0))
        user_stream_errors_total.set(int(stats.get("error_count", 0) or 0))
        last_age = stats.get("last_message_age_seconds")
        if last_age is not None:
            user_stream_last_message_age_seconds.set(float(last_age))
    except Exception as exc:
        logger.debug(f"metrics.update_user_stream_stats falhou: {exc}")

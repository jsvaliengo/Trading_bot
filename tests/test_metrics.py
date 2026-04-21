"""Testes do módulo de métricas Prometheus."""

from types import SimpleNamespace

from prometheus_client import REGISTRY

from trading_bot.observability import metrics


def _get_sample(metric_name: str, labels: dict | None = None) -> float | None:
    """Lê o valor atual de uma métrica direto do REGISTRY (sem HTTP scrape)."""
    for family in REGISTRY.collect():
        if family.name != metric_name and not family.name.startswith(metric_name):
            continue
        for sample in family.samples:
            if sample.name != metric_name and sample.name != f"{metric_name}_total":
                continue
            if labels is None or all(sample.labels.get(k) == v for k, v in labels.items()):
                return sample.value
    return None


def test_update_bot_state_snapshots_gauges():
    bot = SimpleNamespace(
        running=True,
        paused=False,
        daily_target_reached=False,
        total_pnl=12.34,
        daily_realized_pnl=3.21,
        peak_equity=1000.0,
        total_fees_paid=0.55,
        trades_win_count=5,
        trades_loss_count=2,
    )

    metrics.update_bot_state(bot)

    assert _get_sample("trading_bot_running") == 1
    assert _get_sample("trading_bot_paused") == 0
    assert _get_sample("trading_bot_pnl_realized_total_usd") == 12.34
    assert _get_sample("trading_bot_pnl_realized_daily_usd") == 3.21
    assert _get_sample("trading_bot_peak_equity_usd") == 1000.0
    assert _get_sample("trading_bot_trades_win_count") == 5
    assert _get_sample("trading_bot_trades_loss_count") == 2


def test_update_positions_sets_open_count_from_list():
    """positions_open_count deve refletir o tamanho da lista de exchange."""
    metrics.update_positions([
        {"symbol": "SOLUSDT", "side": "LONG", "unrealized_pnl": 1.0, "quantity": 10, "mark_price": 85.0},
        {"symbol": "XRPUSDT", "side": "LONG", "unrealized_pnl": -0.5, "quantity": 500, "mark_price": 1.42},
    ])
    assert _get_sample("trading_bot_positions_open_count") == 2

    # Posições fecharam → contador volta a zero, gauges por posição desaparecem
    metrics.update_positions([])
    assert _get_sample("trading_bot_positions_open_count") == 0


def test_update_positions_clears_stale_and_sets_current():
    # Primeiro: abre duas posições
    positions_a = [
        {"symbol": "ETHUSDT", "side": "LONG", "unrealized_pnl": 1.5, "quantity": 0.1, "mark_price": 2000.0},
        {"symbol": "SOLUSDT", "side": "SHORT", "unrealized_pnl": -0.25, "quantity": 2.0, "mark_price": 100.0},
    ]
    metrics.update_positions(positions_a)
    assert _get_sample("trading_bot_position_pnl_unrealized_usd", {"symbol": "ETHUSDT", "side": "LONG"}) == 1.5
    assert _get_sample("trading_bot_position_notional_usd", {"symbol": "ETHUSDT", "side": "LONG"}) == 200.0
    assert _get_sample("trading_bot_position_pnl_unrealized_usd", {"symbol": "SOLUSDT", "side": "SHORT"}) == -0.25

    # Depois: apenas uma posição — stale deve desaparecer
    positions_b = [
        {"symbol": "ETHUSDT", "side": "LONG", "unrealized_pnl": 2.0, "quantity": 0.1, "mark_price": 2050.0},
    ]
    metrics.update_positions(positions_b)
    assert _get_sample("trading_bot_position_pnl_unrealized_usd", {"symbol": "ETHUSDT", "side": "LONG"}) == 2.0
    assert _get_sample("trading_bot_position_pnl_unrealized_usd", {"symbol": "SOLUSDT", "side": "SHORT"}) is None


def test_record_trade_closed_increments_counter():
    before = _get_sample(
        "trading_bot_trades_closed",
        {"result": "win", "strategy": "hedge", "symbol": "BTCUSDT"},
    ) or 0.0

    metrics.record_trade_closed("BTCUSDT", "hedge", "win", pnl_usd=3.5, fees_usd=0.1)

    after = _get_sample(
        "trading_bot_trades_closed",
        {"result": "win", "strategy": "hedge", "symbol": "BTCUSDT"},
    )
    assert after is not None
    assert after == before + 1


def test_record_order_increments_counter_by_result():
    before_success = _get_sample(
        "trading_bot_orders_placed", {"side": "LONG", "result": "success"}
    ) or 0.0
    before_failure = _get_sample(
        "trading_bot_orders_placed", {"side": "LONG", "result": "failure"}
    ) or 0.0

    metrics.record_order("LONG", success=True)
    metrics.record_order("LONG", success=False)

    assert _get_sample("trading_bot_orders_placed", {"side": "LONG", "result": "success"}) == before_success + 1
    assert _get_sample("trading_bot_orders_placed", {"side": "LONG", "result": "failure"}) == before_failure + 1


def test_update_account_balance_and_drawdown_tolerate_bad_input():
    # Valores válidos gravam
    metrics.update_account_balance(500.0)
    metrics.update_drawdown(7.5)
    assert _get_sample("trading_bot_account_balance_usd") == 500.0
    assert _get_sample("trading_bot_drawdown_from_peak_percent") == 7.5

    # Entrada inválida não explode
    metrics.update_account_balance("garbage")  # type: ignore[arg-type]
    metrics.update_drawdown(None)  # type: ignore[arg-type]
    # Valores anteriores continuam preservados
    assert _get_sample("trading_bot_account_balance_usd") == 500.0
    assert _get_sample("trading_bot_drawdown_from_peak_percent") == 7.5


def test_set_bot_info_sets_labels():
    metrics.set_bot_info(environment="testnet", app_env="test")

    # Info metrics expõem uma amostra "trading_bot_info_info" com labels + valor 1.0
    matches = []
    for family in REGISTRY.collect():
        if family.name != "trading_bot_info":
            continue
        for sample in family.samples:
            if sample.name == "trading_bot_info_info":
                matches.append(sample.labels)

    assert any(
        labels.get("environment") == "testnet" and labels.get("app_env") == "test"
        for labels in matches
    )

import json

from trading_bot.web.dashboard import DashboardDataCollector, calculate_roi_percent


def test_calculate_roi_percent_uses_margin_and_handles_zero_values():
    # 1 contrato de notional 100 com 20x => margem 5
    # PnL +1 => ROI 20%
    roi = calculate_roi_percent(
        unrealized_pnl=1.0,
        quantity=1.0,
        entry_price=100.0,
        leverage=20,
    )
    assert round(roi, 2) == 20.0

    # Proteções para dados inválidos
    assert calculate_roi_percent(1.0, 0.0, 100.0, 20) == 0.0
    assert calculate_roi_percent(1.0, 1.0, 0.0, 20) == 0.0


def test_dashboard_collector_includes_trailing_flags_from_state(tmp_path):
    state_file = tmp_path / "bot_state.test.json"
    state_payload = {
        "saved_at": "2026-02-28T12:00:00+00:00",
        "closed_trades_count": 9,
        "total_pnl": 4.25,
        "trades_win_count": 7,
        "trades_loss_count": 2,
        "trailing_activated": {"ETHUSDT_LONG": True},
        "peak_prices": {"ETHUSDT_LONG": 2800.0},
        "portfolio_history": [
            {"timestamp": "2026-02-28T11:00:00+00:00", "pnl_realized": 2.0},
            {"timestamp": "2026-02-28T12:00:00+00:00", "pnl_realized": 3.0},
        ],
    }
    state_file.write_text(json.dumps(state_payload), encoding="utf-8")

    class ExchangeStub:
        def get_account_info(self):
            return {
                "wallet_balance": 100.0,
                "available_balance": 75.0,
                "unrealized_pnl": -2.5,
                "margin_balance": 97.5,
                "total_initial_margin": 20.0,
            }

        def get_daily_pnl_from_binance(self):
            return {
                "realized_pnl": 1.2,
                "funding_fee": -0.1,
                "commission": -0.2,
                "total": 0.9,
                "income_count": 12,
                "income_types": ["REALIZED_PNL", "COMMISSION", "FUNDING_FEE"],
            }

        def get_open_positions(self):
            return [
                {
                    "symbol": "ETHUSDT",
                    "side": "LONG",
                    "quantity": 0.25,
                    "entry_price": 2500.0,
                    "mark_price": 2480.0,
                    "unrealized_pnl": -1.0,
                    "leverage": 20,
                }
            ]

        def get_retry_stats_report(self, reset=False):
            return {
                "calls": 10,
                "retries": 1,
                "failures": 0,
                "retry_rate": 10.0,
                "failure_rate": 0.0,
                "endpoints": [],
            }

        def get_order_stats_report(self, reset=False):
            return {
                "attempts": 3,
                "successes": 3,
                "failures": 0,
                "rejections": 0,
                "failure_rate": 0.0,
                "rejection_rate": 0.0,
                "symbols": [],
            }

    collector = DashboardDataCollector(
        state_file_path=str(state_file),
        exchange=ExchangeStub(),
        fx_rate_provider=lambda: 5.25,
    )
    data = collector.collect()

    assert data["account"]["wallet_balance"] == 100.0
    assert data["daily"]["total"] == 0.9
    assert data["trades"]["closed_trades_count"] == 9
    assert data["positions_summary"]["count"] == 1
    assert data["positions"][0]["symbol"] == "ETHUSDT"
    assert data["positions"][0]["trailing_active"] is True
    assert data["positions"][0]["peak_price"] == 2800.0
    assert data["fx"]["usd_brl"] == 5.25


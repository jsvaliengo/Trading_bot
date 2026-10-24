"""Execução segura: fill real, dados velhos e par abaixo do piso de volume.

20/10/26: NEAR #74/#76/#77 gravaram entrada 4.772 com fills reais 4.710/4.690/4.676
(SOL #75: 104.48 vs 103.88). A resposta da ordem veio sem avgPrice, o bot caiu no
ticker pré-ordem e ancorou SL/TP ~1% longe do fill (stop efetivo 2,4–3,1%).
24/10/26: "Abrindo SHORT em BCHUSDT" 129× em 2h, cada uma barrada pelo piso de
volume ($74,6M < $150M), porque o filler da seleção ignorava o filtro de volume.
"""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import trading_bot.infra.binance_client as bc
from trading_bot.core.bot import TradingBot
from trading_bot.core.config import config
from trading_bot.core.strategy import Signal, TradeSetup
from trading_bot.infra.binance_client import BinanceConnection
from trading_bot.infra.binance_streams import WebSocketKlineStore


def _now_ms() -> int:
    return int(time.time() * 1000)


def _conn(order_reply, get_order_replies=(), trades=()):
    """BinanceConnection real só no place_market_order; resto stubado."""
    conn = BinanceConnection.__new__(BinanceConnection)
    conn.config = config
    conn.client = MagicMock(name="testnet")
    conn.client.futures_create_order.return_value = dict(order_reply)
    conn.client.futures_get_order.side_effect = [dict(r) for r in get_order_replies] or [{}] * 5
    conn.client.futures_account_trades.return_value = list(trades)

    def _api_call(_label, fn, *a, retry=True, allow_timeout_retry=True, **kw):
        return fn(*a, **kw)

    conn._api_call = _api_call
    conn._record_order_stat = lambda *a, **k: None
    conn.get_symbol_cooldown_info = lambda _s: None
    conn.get_symbol_info = lambda _s: {"quantityPrecision": 1, "pricePrecision": 4, "minNotional": 5.0}
    conn.invalidate_balance_cache = lambda: None
    conn.invalidate_positions_cache = lambda: None
    return conn


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(bc.time, "sleep", lambda _s: None)


# ─────────────────────────── 1. Fill real quando falta avgPrice ───────────────────────────

def test_avgprice_zero_consulta_a_ordem():
    conn = _conn(
        {"orderId": 7, "status": "NEW", "avgPrice": "0.00", "executedQty": "0"},
        get_order_replies=[{"orderId": 7, "status": "FILLED", "avgPrice": "4.7100", "executedQty": "10"}],
    )
    order = conn.place_market_order("NEARUSDT", "BUY", "LONG", 10)
    assert float(order["avgPrice"]) == pytest.approx(4.71)
    conn.client.futures_get_order.assert_called_with(symbol="NEARUSDT", orderId=7)


def test_avgprice_zero_sem_fill_na_ordem_usa_vwap_dos_trades():
    conn = _conn(
        {"orderId": 7, "status": "NEW", "avgPrice": "0"},
        get_order_replies=[{"orderId": 7, "avgPrice": "0"}] * 3,
        trades=[{"price": "4.70", "qty": "4"}, {"price": "4.72", "qty": "6"}],
    )
    order = conn.place_market_order("NEARUSDT", "BUY", "LONG", 10)
    assert float(order["avgPrice"]) == pytest.approx((4.70 * 4 + 4.72 * 6) / 10)
    conn.client.futures_account_trades.assert_called_with(symbol="NEARUSDT", orderId=7)


def _open_on_conn(monkeypatch, conn, *, side, ticker_price, sl, tp, ticker_time_ms=None):
    bot = TradingBot.__new__(TradingBot)
    bot._init_runtime_state()
    placed: dict = {}
    conn.get_symbol_ticker = lambda _s: {
        "price": str(ticker_price), "time": ticker_time_ms if ticker_time_ms is not None else _now_ms(),
    }
    conn.set_stop_loss_take_profit = lambda **k: placed.update(k) or True
    conn.get_account_balance = lambda: 1000.0
    bot.exchange = conn
    bot.telegram = SimpleNamespace(send_trade_alert=lambda **_k: True, send_message=MagicMock(return_value=True))
    bot._get_total_open_notional_percent = lambda: 0.0
    for name, value in {
        "CHECK_FUNDING_RATE": False, "USE_BINANCE_STRATEGY": False,
        "MAX_TOTAL_NOTIONAL_PERCENT": 999.0, "MAX_POSITION_CONCENTRATION_PERCENT": 100.0,
        "USE_INDIVIDUAL_STOP_LOSS": True, "STOP_LOSS_PERCENT": 3.0,
        "MIN_TRADE_VOLUME_24H_USD": 0, "ENVIRONMENT": "testnet", "SIGNAL_DATA_SOURCE": "mainnet",
    }.items():
        monkeypatch.setattr(config, name, value)
    signal = Signal.STRONG_BUY if side == "LONG" else Signal.STRONG_SELL
    setup = TradeSetup(
        symbol="NEARUSDT", signal=signal, long_size=3.0, short_size=3.0,
        entry_price=ticker_price, stop_loss=sl, take_profit=tp, dca_levels=[],
    )
    kwargs = {"open_long": True} if side == "LONG" else {"open_short": True}
    ok = bot.execute_signal_trade(setup, strategy_name="trend_strong", **kwargs)
    return ok, bot, placed


@pytest.mark.parametrize("side,sl,tp,want_sl,want_tp", [
    # ticker velho 4.772, fill real 4.710: SL 1% / TP 2% do FILL, não do ticker
    ("LONG", 4.772 * 0.99, 4.772 * 1.02, 4.71 * 0.99, 4.71 * 1.02),
    ("SHORT", 4.772 * 1.01, 4.772 * 0.98, 4.71 * 1.01, 4.71 * 0.98),
])
def test_sl_tp_ancorados_no_fill_real_quando_falta_avgprice(monkeypatch, side, sl, tp, want_sl, want_tp):
    conn = _conn(
        {"orderId": 7, "status": "NEW", "avgPrice": "0.00"},
        get_order_replies=[{"orderId": 7, "status": "FILLED", "avgPrice": "4.7100", "executedQty": "10"}],
    )
    ok, bot, placed = _open_on_conn(monkeypatch, conn, side=side, ticker_price=4.772, sl=sl, tp=tp)
    assert ok is True
    assert placed["stop_loss_price"] == pytest.approx(want_sl, abs=1e-6)
    assert placed["take_profit_price"] == pytest.approx(want_tp, abs=1e-6)
    pos = bot.known_positions[f"NEARUSDT_{side}"]
    assert pos["entry_price"] == pytest.approx(4.71)
    assert pos["custom_stop_loss"] == pytest.approx(want_sl, abs=1e-6)


# ─────────────────────────── 2. Dados velhos não abrem trade ───────────────────────────

def test_ticker_velho_nao_abre(monkeypatch):
    monkeypatch.setattr(config, "MAX_TICKER_AGE_SECONDS", 60.0)
    conn = _conn({"orderId": 7, "avgPrice": "4.71"})
    ok, _bot, placed = _open_on_conn(
        monkeypatch, conn, side="LONG", ticker_price=4.772, sl=4.70, tp=4.90,
        ticker_time_ms=_now_ms() - 3 * 3600 * 1000,
    )
    assert ok is False
    conn.client.futures_create_order.assert_not_called()
    assert placed == {}


def _analyze_bot(monkeypatch, klines):
    bot = TradingBot.__new__(TradingBot)
    bot._init_runtime_state()
    monkeypatch.setattr(config, "USE_DAILY_TARGETS", False)
    monkeypatch.setattr(config, "TIMEFRAME", "5m")
    monkeypatch.setattr(config, "CANDLES_LOOKBACK", 50)
    setup = TradeSetup(
        symbol="ETHUSDT", signal=Signal.STRONG_BUY, long_size=5.0, short_size=5.0,
        entry_price=100.0, stop_loss=98.0, take_profit=102.0, dca_levels=[],
    )
    generate = MagicMock(return_value=setup)
    bot.exchange = SimpleNamespace(
        get_klines=lambda **_k: klines,
        get_available_balance=lambda: 1000.0,
        get_account_balance=lambda: 1000.0,
        get_symbol_info=lambda _s: {"minNotional": 5.0},
        get_open_positions=lambda *a, **kw: [],
    )
    bot.strategy = SimpleNamespace(generate_trade_setup=generate)
    bot.risk_manager = SimpleNamespace(can_open_position=lambda _total: True)
    bot.sentiment_mode_enabled = False
    bot.execute_signal_trade = MagicMock(return_value=True)
    return bot, generate


def test_kline_velha_nao_gera_trade(monkeypatch, caplog):
    stale_open = _now_ms() - 3 * 3600 * 1000  # feed parado ~3h (20/10)
    bot, generate = _analyze_bot(monkeypatch, [{"timestamp": stale_open, "close": 100.0}])
    with caplog.at_level(logging.WARNING):
        assert bot.analyze_and_trade("ETHUSDT") is False
        assert bot.analyze_and_trade("ETHUSDT") is False
    generate.assert_not_called()
    bot.execute_signal_trade.assert_not_called()
    assert sum("velas paradas" in r.getMessage() for r in caplog.records) == 1


def test_kline_recente_segue_analise(monkeypatch):
    last_closed_open = (_now_ms() // 300_000 - 1) * 300_000
    bot, generate = _analyze_bot(monkeypatch, [{"timestamp": last_closed_open, "close": 100.0}])
    bot.analyze_and_trade("ETHUSDT")
    generate.assert_called_once()


def test_ws_com_mensagens_mas_sem_vela_fechada_nova_nao_e_fresh(monkeypatch):
    """Stream entregando só velas em formação mantinha is_fresh=True com buffer congelado."""
    store = WebSocketKlineStore(
        api_key="k", api_secret="s", testnet=True, rest_seed_fetcher=lambda s, i, n: [],
    )
    key = ("ETHUSDT", "3m")
    store._buffers[key] = [{"timestamp": _now_ms() - 3600 * 1000, "close": 1.0}]
    store._socket_ids[key] = "sid"
    store._on_kline_message(key, {"k": {"t": _now_ms(), "x": False, "c": "1"}})
    scheduled = []
    monkeypatch.setattr(store, "_schedule_recovery", lambda *a, **k: scheduled.append(a))
    assert store.is_fresh("ETHUSDT", "3m") is False
    assert scheduled and scheduled[0][0] == key


# ─────────────────────────── 3. Par abaixo do piso de volume ───────────────────────────

def test_par_abaixo_do_piso_loga_uma_vez_e_nao_anuncia_abertura(monkeypatch, caplog):
    conn = _conn({"orderId": 7, "avgPrice": "277.0"})
    conn.get_symbol_ticker = lambda _s: {"price": "277.0", "time": _now_ms()}
    bot = TradingBot.__new__(TradingBot)
    bot._init_runtime_state()
    bot.exchange = conn
    bot._reference_quote_volume_24h = lambda _s: 74_600_474.0
    monkeypatch.setattr(config, "MIN_TRADE_VOLUME_24H_USD", 150_000_000)
    monkeypatch.setattr(config, "CHECK_FUNDING_RATE", False)
    setup = TradeSetup(
        symbol="BCHUSDT", signal=Signal.STRONG_SELL, long_size=3.0, short_size=3.0,
        entry_price=277.0, stop_loss=280.0, take_profit=270.0, dca_levels=[],
    )
    with caplog.at_level(logging.INFO):
        for _ in range(5):
            assert bot.execute_signal_trade(setup, strategy_name="trend_strong", open_short=True) is False
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("abaixo do volume mínimo" in m for m in msgs) == 1
    assert not any("Abrindo SHORT" in m for m in msgs)
    conn.client.futures_create_order.assert_not_called()


def test_filler_da_selecao_respeita_piso_de_volume(monkeypatch):
    """Só 1 par pontua: o filler completava com a ordem padrão (BCH $74M incluso)."""
    bot = TradingBot.__new__(TradingBot)
    bot._init_runtime_state()
    universe = ["DOGEUSDT", "BCHUSDT", "ETHUSDT"]
    bot._refresh_binance_coin_universe = lambda trigger_reason: list(universe)
    bot.exchange = SimpleNamespace(
        get_all_tickers_24h=lambda: {s: {"quoteVolume": "1e12"} for s in universe},
        get_all_funding_rates=lambda: {},
        get_reference_liquidity_map=lambda: {
            "DOGEUSDT": {"volume_24h": 900e6}, "BCHUSDT": {"volume_24h": 74.6e6},
            "ETHUSDT": {"volume_24h": 9e9},
        },
    )
    bot.pair_selector = SimpleNamespace(
        get_pair_metrics=lambda symbol, **_k: {"symbol": symbol},
        score_pair=lambda m: 70.0 if m["symbol"] == "DOGEUSDT" else 0.0,
    )
    monkeypatch.setattr(config, "MIN_VOLUME_24H_USD", 150_000_000)
    monkeypatch.setattr(config, "OI_ENABLED", False)
    assert bot.sort_binance_coins_by_score(3) == ["DOGEUSDT", "ETHUSDT"]

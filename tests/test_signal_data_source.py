"""Sinais com dados da MAINNET, execução no TESTNET (TRADING_BOT_SIGNAL_DATA_SOURCE).

Klines de testnet correlacionam só 0,72–0,92 com a mainnet e o volume não tem
relação (0,03×–431×): replay na mainnet bateu 9/40 trades ao vivo, no testnet
37/40. Com SIGNAL_DATA_SOURCE=mainnet em testnet, klines/ticker/book/funding vêm
da mainnet pública; ordens, conta, posições e preço de execução seguem no testnet.
SL/TP do sinal (preço mainnet) viram % e são re-ancorados no fill do testnet.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import trading_bot.infra.binance_client as bc
from trading_bot.core.bot import TradingBot
from trading_bot.core.config import config
from trading_bot.core.strategy import Signal, TradeSetup
from trading_bot.infra.binance_client import BinanceConnection

MAINNET_FSTREAM = "wss://fstream.binance.com/market/"


def _set_source(monkeypatch, environment: str, source: str) -> None:
    monkeypatch.setattr(config, "ENVIRONMENT", environment)
    monkeypatch.setattr(config, "SIGNAL_DATA_SOURCE", source)


@pytest.mark.parametrize("environment,source,expected", [
    ("testnet", "mainnet", True),
    ("testnet", "exchange", False),
    ("mainnet", "mainnet", False),  # no-op em mainnet
])
def test_signals_from_mainnet(monkeypatch, environment, source, expected):
    _set_source(monkeypatch, environment, source)
    assert config.SIGNALS_FROM_MAINNET is expected


# ─────────────────────────── BinanceConnection: wiring ───────────────────────────

class _FakeClient:
    instances: list = []

    def __init__(self, api_key=None, api_secret=None, testnet=False, ping=True):
        self.kwargs = {"api_key": api_key, "api_secret": api_secret, "testnet": testnet, "ping": ping}
        _FakeClient.instances.append(self)


class _FakeStore:
    kwargs: dict = {}

    def __init__(self, **kwargs):
        _FakeStore.kwargs = kwargs

    def start(self):
        return False


def _build_conn(monkeypatch, source):
    _set_source(monkeypatch, "testnet", source)
    _FakeClient.instances = []
    monkeypatch.setattr(bc, "Client", _FakeClient)
    monkeypatch.setattr(bc, "WebSocketKlineStore", _FakeStore)
    monkeypatch.setattr(config, "WEBSOCKET_ENABLED", True)
    monkeypatch.setattr(BinanceConnection, "_test_connection", lambda self: True)
    return BinanceConnection()


def test_flag_on_usa_cliente_publico_mainnet_e_ws_mainnet(monkeypatch):
    conn = _build_conn(monkeypatch, "mainnet")
    assert conn.client.kwargs["testnet"] is True  # execução/conta seguem no testnet
    assert conn.market_client is not conn.client
    assert conn.market_client.kwargs == {
        "api_key": None, "api_secret": None, "testnet": False, "ping": False,
    }
    assert _FakeStore.kwargs["testnet"] is True  # TWM (user stream) no testnet
    assert _FakeStore.kwargs["kline_stream_url"] == MAINNET_FSTREAM


def test_flag_off_mantem_tudo_no_testnet(monkeypatch):
    conn = _build_conn(monkeypatch, "exchange")
    assert conn.market_client is conn.client
    assert _FakeStore.kwargs["kline_stream_url"] is None
    assert len(_FakeClient.instances) == 1


def test_cliente_publico_sem_chave_aponta_para_fapi_mainnet():
    from binance.client import Client
    assert Client(ping=False).FUTURES_URL == "https://fapi.binance.com/fapi"


# ─────────────────────────── BinanceConnection: routing ───────────────────────────

def _routing_conn():
    conn = BinanceConnection.__new__(BinanceConnection)
    conn.config = config
    conn.client = MagicMock(name="testnet")
    conn.market_client = MagicMock(name="mainnet")
    conn._api_call = lambda _name, fn, **kw: fn(**kw)
    conn._ws_store = None
    conn._cache_lock = MagicMock()
    conn._funding_rate_cache = {}
    conn._funding_rate_cache_ttl = 0.0
    conn.market_client.futures_klines.return_value = []
    conn.market_client.futures_ticker.return_value = [{"symbol": "ETHUSDT"}]
    conn.market_client.futures_funding_rate.return_value = [{"fundingRate": "0.0001"}]
    conn.market_client.futures_mark_price.return_value = {"nextFundingTime": 0}
    return conn


def test_dados_de_sinal_vao_para_mainnet():
    conn = _routing_conn()
    conn.get_klines("ETHUSDT", "3m", 10)
    conn.get_klines_raw("ETHUSDT", "1h", 10)
    conn.get_ticker_24h("ETHUSDT")
    conn.get_all_tickers_24h()
    conn.get_order_book("ETHUSDT")
    conn.get_funding_rate("ETHUSDT")
    conn.get_all_funding_rates()

    called = {c[0] for c in conn.market_client.method_calls}
    assert {
        "futures_klines", "futures_ticker", "futures_order_book",
        "futures_funding_rate", "futures_mark_price",
    } <= called
    assert conn.client.method_calls == []


def test_execucao_e_preco_vivo_ficam_no_testnet():
    conn = _routing_conn()
    conn.client.futures_symbol_ticker.return_value = {"price": "97.5"}
    assert conn.get_symbol_price("ETHUSDT") == pytest.approx(97.5)
    conn.client.futures_symbol_ticker.assert_called_once()
    assert conn.market_client.method_calls == []


# ─────────────────────────── WS: socket de klines na mainnet ───────────────────────────

def _store(kline_stream_url):
    from trading_bot.infra.binance_streams import WebSocketKlineStore
    return WebSocketKlineStore(
        api_key="k", api_secret="s", testnet=True,
        rest_seed_fetcher=lambda s, i, n: [{"timestamp": 1, "close": 1.0}],
        kline_stream_url=kline_stream_url,
    )


def test_ws_klines_abrem_no_fstream_mainnet(monkeypatch):
    import trading_bot.infra.binance_streams as bs
    twm = MagicMock()
    twm._start_async_socket.return_value = "sid"
    monkeypatch.setattr(bs, "ResilientThreadedWebsocketManager", lambda **kw: twm)
    store = _store(MAINNET_FSTREAM)
    store.start()
    assert store.subscribe("ETHUSDT", "3m") is True

    twm.start_kline_futures_socket.assert_not_called()
    params = twm._start_async_socket.call_args.kwargs["params"]
    assert twm._start_async_socket.call_args.kwargs["socket_name"] == "_get_socket"
    assert params["stream_url"] == MAINNET_FSTREAM
    assert params["path"] == "ethusdt_perpetual@continuousKline_3m"


def test_ws_klines_sem_url_usam_rede_do_twm(monkeypatch):
    import trading_bot.infra.binance_streams as bs
    twm = MagicMock()
    twm.start_kline_futures_socket.return_value = "sid"
    monkeypatch.setattr(bs, "ResilientThreadedWebsocketManager", lambda **kw: twm)
    store = _store(None)
    store.start()
    assert store.subscribe("ETHUSDT", "3m") is True
    twm.start_kline_futures_socket.assert_called_once()
    twm._start_async_socket.assert_not_called()


# ─────────────────────────── Execução: SL/TP re-ancorados ───────────────────────────

def _open_with_divergence(monkeypatch, *, side, testnet_price, fill, sl, tp):
    bot = TradingBot.__new__(TradingBot)
    bot._init_runtime_state()
    placed = {}
    bot.exchange = SimpleNamespace(
        get_symbol_price=lambda _s: testnet_price,
        get_symbol_info=lambda _s: {"minNotional": 5.0, "pricePrecision": 4},
        place_market_order=lambda **k: {"orderId": 1, "status": "FILLED", "avgPrice": str(fill)},
        set_stop_loss_take_profit=lambda **k: placed.update(k) or True,
        get_account_balance=lambda: 1000.0,
        get_symbol_cooldown_info=lambda _s: None,
    )
    bot.telegram = SimpleNamespace(send_trade_alert=lambda **_k: True, send_message=MagicMock(return_value=True))
    bot._get_total_open_notional_percent = lambda: 0.0
    for name, value in {
        "CHECK_FUNDING_RATE": False, "USE_BINANCE_STRATEGY": False,
        "MAX_TOTAL_NOTIONAL_PERCENT": 999.0, "MAX_POSITION_CONCENTRATION_PERCENT": 100.0,
        "USE_INDIVIDUAL_STOP_LOSS": True, "STOP_LOSS_PERCENT": 3.0,
    }.items():
        monkeypatch.setattr(config, name, value)

    signal = Signal.STRONG_BUY if side == "LONG" else Signal.STRONG_SELL
    setup = TradeSetup(
        symbol="ETHUSDT", signal=signal, long_size=3.0, short_size=3.0,
        entry_price=100.0, stop_loss=sl, take_profit=tp, dca_levels=[],
    )
    kwargs = {"open_long": True} if side == "LONG" else {"open_short": True}
    assert bot.execute_signal_trade(setup, strategy_name="trend_strong", **kwargs) is True
    return bot, placed


@pytest.mark.parametrize("side,testnet_price,fill,sl,tp,want_sl,want_tp", [
    # testnet 2,5% ABAIXO da mainnet: SL LONG cru (98,8) ficaria acima do preço
    ("LONG", 97.5, 97.6, 98.8, 102.0, 97.6 * 0.988, 97.6 * 1.02),
    # testnet 2,5% ACIMA: SL SHORT cru (101,2) ficaria abaixo do preço
    ("SHORT", 102.5, 102.4, 101.2, 98.0, 102.4 * 1.012, 102.4 * 0.98),
])
def test_sl_tp_reancorados_no_fill_do_testnet(
    monkeypatch, side, testnet_price, fill, sl, tp, want_sl, want_tp
):
    _set_source(monkeypatch, "testnet", "mainnet")
    bot, placed = _open_with_divergence(
        monkeypatch, side=side, testnet_price=testnet_price, fill=fill, sl=sl, tp=tp,
    )
    assert placed["stop_loss_price"] == pytest.approx(want_sl, abs=1e-4)
    assert placed["take_profit_price"] == pytest.approx(want_tp, abs=1e-4)
    if side == "LONG":
        assert placed["stop_loss_price"] < fill < placed["take_profit_price"]
    else:
        assert placed["take_profit_price"] < fill < placed["stop_loss_price"]
    pos = bot.known_positions[f"ETHUSDT_{side}"]
    assert pos["entry_price"] == pytest.approx(fill)
    assert pos["custom_stop_loss"] == pytest.approx(want_sl, abs=1e-4)
    assert pos["custom_take_profit"] == pytest.approx(want_tp, abs=1e-4)


def test_flag_off_sl_tp_inalterados(monkeypatch):
    """Comportamento atual: TP cru; SL no lado errado vira o fallback de 3%."""
    _set_source(monkeypatch, "testnet", "exchange")
    _bot, placed = _open_with_divergence(
        monkeypatch, side="LONG", testnet_price=97.5, fill=97.6, sl=98.8, tp=102.0,
    )
    assert placed["take_profit_price"] == pytest.approx(102.0)
    assert placed["stop_loss_price"] == pytest.approx(round(97.5 * 0.97, 4))


# ─────────────────────────── Universo: par ausente no testnet ───────────────────────────

def test_par_ausente_no_testnet_nao_e_selecionado(monkeypatch):
    from trading_bot.services.pair_selector import PairSelector

    bot = TradingBot.__new__(TradingBot)
    bot._init_runtime_state()
    testnet_symbols = ["ETHUSDT", "SOLUSDT"]  # XYZUSDT só existe na mainnet
    bot.exchange = SimpleNamespace(
        get_exchange_info=lambda: {"symbols": [
            {"symbol": s, "contractType": "PERPETUAL", "status": "TRADING"} for s in testnet_symbols
        ]},
        get_all_tickers_24h=lambda: {
            s: {"quoteVolume": "1e9", "lastPrice": "1.0"} for s in ["ETHUSDT", "SOLUSDT", "XYZUSDT"]
        },
        get_all_funding_rates=lambda: {},
        get_reference_liquidity_map=lambda: None,
    )
    selector = PairSelector(bot.exchange, config)
    selector.get_pair_metrics = lambda symbol, **_k: {"symbol": symbol}
    selector.score_pair = lambda m: {"XYZUSDT": 99.0, "ETHUSDT": 80.0, "SOLUSDT": 70.0}[m["symbol"]]
    bot.pair_selector = selector
    monkeypatch.setattr(config, "DISABLED_PAIRS", [])
    monkeypatch.setattr(config, "BINANCE_UNIVERSE_WHITELIST", ["ETHUSDT", "SOLUSDT", "XYZUSDT"])
    monkeypatch.setattr(config, "MIN_VOLUME_24H_USD", 0)
    monkeypatch.setattr(config, "OI_ENABLED", False)

    assert bot.sort_binance_coins_by_score(3) == ["ETHUSDT", "SOLUSDT"]


# ─────────────────────────── Dashboard ───────────────────────────

@pytest.mark.parametrize("environment,source,expected", [
    ("testnet", "mainnet", "mainnet"),
    ("testnet", "exchange", "testnet"),
    ("mainnet", "mainnet", "mainnet"),
])
def test_dashboard_mostra_fonte_dos_sinais(tmp_path, monkeypatch, environment, source, expected):
    from trading_bot.core.trade_store import TradeStore
    from trading_bot.web.data import collect_summary

    _set_source(monkeypatch, environment, source)
    bot = SimpleNamespace(
        initial_capital=300.0, closed_trades_count=0, paused=False, running=True,
        trade_store=TradeStore(str(tmp_path / "t.db")), portfolio_history=[],
        exchange=SimpleNamespace(
            get_account_info=lambda: {"wallet_balance": 300.0, "unrealized_pnl": 0.0},
            get_daily_pnl_from_binance=lambda: {"total": 0.0},
        ),
        daily_pnl_binance_baseline=0.0, total_pnl=0.0,
    )
    assert collect_summary(bot)["signal_data_source"] == expected

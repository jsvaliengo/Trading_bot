"""A janela do P&L diário da Binance deve seguir o fuso local configurado.

O app da Binance mostra "Realizados de Hoje" no fuso da conta (ex: Brasília
UTC-3). O dashboard/Telegram usavam 00:00 UTC e divergiam (~trades entre 00:00
e o offset caíam no dia errado). DAILY_PNL_TZ_OFFSET_HOURS alinha a janela.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

from trading_bot.infra.binance_client import BinanceConnection
from trading_bot.core.config import config


def _conn(monkeypatch, offset_hours):
    conn = BinanceConnection.__new__(BinanceConnection)
    conn.config = config
    conn.client = MagicMock()
    conn._cache_lock = threading.Lock()
    conn._daily_pnl_cache = None
    conn._daily_pnl_cache_ttl = 0
    captured = {}

    def fake_api_call(_name, _fn, **kw):
        captured.update(kw)
        return []  # sem registros → soma zero, mas a janela já foi capturada

    conn._api_call = fake_api_call
    monkeypatch.setattr(config, "DAILY_PNL_TZ_OFFSET_HOURS", offset_hours, raising=False)
    return conn, captured


def _expected_start_ms(offset_hours):
    tz = timezone(timedelta(hours=offset_hours))
    n = datetime.now(tz)
    return int(datetime(n.year, n.month, n.day, tzinfo=tz).timestamp() * 1000)


def test_window_starts_at_local_midnight_brt(monkeypatch):
    conn, captured = _conn(monkeypatch, -3.0)
    conn.get_daily_pnl_from_binance(force_refresh=True)
    assert captured["startTime"] == _expected_start_ms(-3.0)


def test_window_starts_at_utc_when_offset_zero(monkeypatch):
    conn, captured = _conn(monkeypatch, 0.0)
    conn.get_daily_pnl_from_binance(force_refresh=True)
    assert captured["startTime"] == _expected_start_ms(0.0)


def test_brt_and_utc_windows_differ_by_three_hours(monkeypatch):
    conn_brt, cap_brt = _conn(monkeypatch, -3.0)
    conn_brt.get_daily_pnl_from_binance(force_refresh=True)
    conn_utc, cap_utc = _conn(monkeypatch, 0.0)
    conn_utc.get_daily_pnl_from_binance(force_refresh=True)
    # BRT começa 3h DEPOIS do UTC (a não ser que estejamos entre 00:00-03:00 UTC,
    # quando o "dia BRT" é o anterior → diferença de 21h). Ambos múltiplos de 3h.
    diff_h = abs(cap_brt["startTime"] - cap_utc["startTime"]) / 3_600_000
    assert diff_h in (3.0, 21.0)

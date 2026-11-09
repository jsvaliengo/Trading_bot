"""Score por regras das moedas da planilha CoinTech2u (services/martingale_pair_score.py):
lateral calmo → verde; tendência forte → vermelho por adx/movimento; volume baixo → vermelho;
misto → amarelo; dados curtos → unknown; cache mantém o último resultado em falha de rede."""
from __future__ import annotations

import pytest

from trading_bot.core.config import config
from trading_bot.services import martingale_pair_score as ps


def _kline(h, lo, c, qv=100e6):
    # formato bruto da Binance: [open_time, open, high, low, close, volume, close_time, quoteVolume, ...]
    return [0, str(c), str(h), str(lo), str(c), "0", 0, str(qv), 0, "0", "0", "0"]


def _flat(n, amp=0.01, qv=100e6, base=1.0):
    """Lateral calmo: close oscila ±amp em torno de base, range do candle ~2·amp."""
    return [_kline(base * (1 + amp), base * (1 - amp), base * (1 + amp * (1 if i % 2 else -1) * 0.5), qv) for i in range(n)]


def _trend(n, step=0.02, qv=100e6):
    """Alta sustentada: cada candle fecha `step` acima do anterior."""
    out, c = [], 1.0
    for _ in range(n):
        c *= 1 + step
        out.append(_kline(c * 1.005, c / (1 + step), c, qv))
    return out


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    for k, v in dict(MARTINGALE_SCORE_VOL_GREEN=4.0, MARTINGALE_SCORE_VOL_RED=7.0, MARTINGALE_SCORE_ADX_GREEN=25.0,
                     MARTINGALE_SCORE_ADX_RED=35.0, MARTINGALE_SCORE_MOVE_GREEN=0.0, MARTINGALE_SCORE_MOVE_RED=20.0,
                     MARTINGALE_SCORE_MIN_VOLUME_USD=40e6, MARTINGALE_STEP_PERCENT=1.5, MARTINGALE_MAX_LEVELS=9).items():
        monkeypatch.setattr(config, k, v, raising=False)


def test_max_move_is_peak_to_valley_in_either_direction():
    assert ps.max_move_pct([100, 110, 90, 95]) == pytest.approx(18.1818, abs=1e-3)  # 110 → 90
    assert ps.max_move_pct([100, 80, 120]) == pytest.approx(50.0)  # 80 → 120
    assert ps.max_move_pct([100]) == 0.0


def test_calm_range_is_green_with_no_reasons():
    s = ps.score_pair(_flat(30), _flat(60))
    assert s["color"] == "green" and s["reasons"] == []
    assert s["vol_pct"] == pytest.approx(2.0, abs=0.1) and s["adx"] < 25 and s["max_move_pct"] < 13.5
    assert s["volume_usd"] == pytest.approx(100e6)


def test_strong_trend_is_red_by_adx_and_move():
    s = ps.score_pair(_trend(30), _trend(60))
    assert s["color"] == "red" and s["adx"] > 35 and s["max_move_pct"] > 20
    assert any(r.startswith("adx ") and "(>35)" in r for r in s["reasons"])
    assert any(r.startswith("movimento 30d ") and "(>20,0%)" in r for r in s["reasons"])


def test_low_volume_alone_is_red():
    s = ps.score_pair(_flat(30, qv=10e6), _flat(60))
    assert s["color"] == "red" and s["reasons"] == ["volume 10M (<40M)"]


def test_middle_band_is_yellow_with_pt_br_reason():
    s = ps.score_pair(_flat(30, amp=0.025), _flat(60))  # range ~5% do candle: entre 4 e 7
    assert s["color"] == "yellow" and 4.0 < s["vol_pct"] < 7.0
    assert s["reasons"] == [f"vol {s['vol_pct']:.1f}% (>4,0%)".replace(".", ",")]


def test_move_green_threshold_follows_ladder_reach(monkeypatch):
    # 1,5% × 9 níveis = 13,5%: um rali de 14% passa a amarelo; com MOVE_GREEN explícito de 15 fica verde
    d = _flat(30)
    d[-1] = _kline(1.145, 1.13, 1.14)  # closes ~1.0 → 1.14
    assert ps.score_pair(d, _flat(60))["color"] == "yellow"
    monkeypatch.setattr(config, "MARTINGALE_SCORE_MOVE_GREEN", 15.0)
    assert ps.score_pair(d, _flat(60))["color"] == "green"


def test_insufficient_data_is_unknown():
    assert ps.score_pair(_flat(10), _flat(60)) == {"color": "unknown", "vol_pct": None, "adx": None, "max_move_pct": None,
                                                   "volume_usd": None, "reasons": ["dados insuficientes"]}
    assert ps.score_pair(_flat(30), _flat(20))["color"] == "unknown"
    assert ps.score_pair([], [])["color"] == "unknown"


class _Exchange:
    def __init__(self):
        self.fail = False
        self.calls = []

    def get_klines_raw(self, symbol, interval, limit=50):
        self.calls.append((symbol, interval, limit))
        if self.fail:
            return []  # comportamento real do BinanceConnection em erro
        return (_flat(limit) if interval == "1d" else _flat(limit)) + []


def test_cache_refresh_drops_open_candle_and_keeps_last_result_on_failure():
    ex = _Exchange()
    cache = ps.PairScoreCache(ex, ["ADAUSDT"], refresh_minutes=720)
    assert cache.refresh_seconds == 720 * 60
    cache.refresh()
    assert ex.calls == [("ADAUSDT", "1d", 31), ("ADAUSDT", "4h", 61)]
    first = cache.get()
    assert first["scores"]["ADAUSDT"]["color"] == "green" and first["updated_at"]
    ex.fail = True
    cache.refresh()
    assert cache.get()["scores"]["ADAUSDT"] == first["scores"]["ADAUSDT"]  # entrada anterior preservada
    fresh = ps.PairScoreCache(ex, ["TRXUSDT"], refresh_minutes=1)
    fresh.refresh()
    assert fresh.get()["scores"]["TRXUSDT"]["color"] == "unknown" and fresh.refresh_seconds == 60


def test_cache_get_without_exchange_returns_empty_and_starts_no_thread():
    cache = ps.PairScoreCache(None, ["ADAUSDT"], 720)
    assert cache.get() == {"scores": {}, "updated_at": None} and cache._thread is None

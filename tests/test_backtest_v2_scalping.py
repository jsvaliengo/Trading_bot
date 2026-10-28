"""Testes do scalping: VWAP de sessão, limites de sessão do ORB, sinais sem lookahead,
simulador de scalp (SL primeiro, saída por tempo, fill maker com trade-through, TP maker)."""
import numpy as np
import pytest

from scripts.backtest_v2 import scalping as sc
from scripts.backtest_v2.data import Bars, to_ms
from scripts.backtest_v2.engine import Costs
from scripts.backtest_v2.strategies import LONG, SHORT

MIN = 60_000
T0 = to_ms("2024-03-04")


def _bars(n, tf="1m", start=T0, seed=0, drift=0.0):
    rng = np.random.default_rng(seed)
    step = sc.tf_ms(tf)
    c = 100 * np.exp(np.cumsum(rng.normal(drift, 0.001, n)))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + rng.uniform(0, 0.001, n))
    lw = np.minimum(o, c) * (1 - rng.uniform(0, 0.001, n))
    v = rng.uniform(1, 10, n)
    return Bars("XUSDT", tf, start + np.arange(n, dtype=np.int64) * step, o, h, lw, c, v)


def _flat(n, px=100.0, tf="1m", start=T0):
    a = np.full(n, px)
    return Bars("XUSDT", tf, start + np.arange(n, dtype=np.int64) * sc.tf_ms(tf), a.copy(), a.copy(), a.copy(), a.copy(), np.ones(n))


# ───────────── indicadores e sessões ─────────────
def test_session_vwap_resets_at_utc_midnight():
    b = _flat(1440 + 3)
    b.close[:] = b.high[:] = b.low[:] = 100.0
    b.volume[:] = 1.0
    b.close[1440:] = b.high[1440:] = b.low[1440:] = 200.0    # 1ª barra do dia seguinte
    vw = sc.session_vwap(b)
    assert vw[1439] == pytest.approx(100.0)
    assert vw[1440] == pytest.approx(200.0)                   # não carrega o dia anterior
    assert vw[1442] == pytest.approx(200.0)


def test_session_vwap_is_typical_price_weighted_by_volume():
    b = _flat(2)
    b.high, b.low, b.close = np.array([110.0, 90.0]), np.array([90.0, 70.0]), np.array([100.0, 80.0])
    b.volume = np.array([1.0, 3.0])
    vw = sc.session_vwap(b)
    assert vw[0] == pytest.approx(100.0)
    assert vw[1] == pytest.approx((100 * 1 + 80 * 3) / 4)


def test_session_starts_are_0000_and_1330_utc():
    b = _flat(2 * 1440)
    s = sc.session_starts(b.open_time)
    assert [int(b.open_time[i] % sc.DAY) // MIN for i in s] == [0, 13 * 60 + 30, 0, 13 * 60 + 30]


def test_ema_rsi_sanity():
    x = np.full(50, 5.0)
    assert np.allclose(sc.ema(x, 9), 5.0)
    up = np.arange(50, dtype=float)
    assert sc.rsi(up)[-1] == pytest.approx(100.0)
    assert sc.rsi(up[::-1])[-1] == pytest.approx(0.0)


# ───────────── ORB ─────────────
def test_orb_range_uses_exactly_first_w_bars_and_breaks_after():
    b = _flat(300, 100.0)
    b.volume[:] = 1.0
    b.high[:15] = 101.0
    b.low[:15] = 99.0
    b.high[14] = 103.0            # última barra do range: entra no range
    b.high[15] = 110.0            # 1ª barra depois do range: NÃO entra no range
    b.close[20] = 103.5           # fecha acima de range_high=103 → LONG
    b.volume[20] = 5.0
    idx, side, sl_d, tp_d = sc.s2_signals(b, {"s": "s2", "R": 2.0, "W": 15})
    assert idx.tolist() == [20] and side.tolist() == [LONG]
    assert sl_d[0] == pytest.approx(103.5 - (103 + 99) / 2)
    assert tp_d[0] == pytest.approx(2.0 * (103 - 99))


def test_orb_one_trade_per_session_and_volume_filter():
    b = _flat(300, 100.0)
    b.volume[:] = 1.0
    b.high[:15], b.low[:15] = 101.0, 99.0
    b.close[20] = 102.0           # rompe, mas volume = média → barrado
    b.close[25] = 102.0
    b.volume[25] = 9.0            # rompe com volume → sinal
    b.close[30] = 97.0
    b.volume[30] = 9.0            # lado oposto depois: ignorado
    idx, side, *_ = sc.s2_signals(b, {"s": "s2", "R": 1.0, "W": 15})
    assert idx.tolist() == [25] and side.tolist() == [LONG]


def test_orb_skips_session_with_missing_bars():
    b = _flat(300, 100.0)
    keep = np.ones(300, bool)
    keep[5] = False               # buraco dentro do range
    b = Bars(b.symbol, b.tf, b.open_time[keep], b.open[keep], b.high[keep], b.low[keep], b.close[keep], b.volume[keep])
    b.high[:14], b.low[:14] = 101.0, 99.0
    b.close[30], b.volume[30] = 105.0, 9.0
    idx, *_ = sc.s2_signals(b, {"s": "s2", "R": 1.0, "W": 15})
    assert idx.tolist() == []


# ───────────── sem lookahead ─────────────
@pytest.mark.parametrize("strat,cell", [("s1", sc.GRIDS["s1"][0]), ("s2", sc.GRIDS["s2"][0]), ("s3", sc.GRIDS["s3"][0])])
def test_signals_have_no_lookahead(strat, cell):
    b = _bars(3000, seed=3)
    b5 = _bars(600, tf="5m", seed=4)
    cut = 2000

    def run(bb, bb5):
        if strat == "s1":
            return sc.s1_signals(bb, bb5, cell)
        if strat == "s2":
            return sc.s2_signals(bb, cell)
        return sc.s3_signals(bb, cell)

    full = run(b, b5)
    pre = run(b.slice_time(0, int(b.open_time[cut])), b5.slice_time(0, int(b.open_time[cut])))
    m = full[0] < cut - 1           # a última barra do prefixo ainda pode faltar contexto (RSI/ATR não; ORB até 4h depois sim)
    for a, p in zip(full[1:], pre[1:]):
        np.testing.assert_allclose(np.asarray(a)[m], np.asarray(p)[pre[0] < cut - 1])
    assert full[0][m].tolist() == pre[0][pre[0] < cut - 1].tolist()


def test_s1_uses_only_closed_5m_bars():
    """Tendência 5m vista pela barra 1m só depois do close da barra 5m."""
    b = _flat(20)
    b5 = _flat(4, tf="5m")
    j = sc.aligned_index(b.close_time, b5)
    assert j.tolist() == [-1] * 4 + [0] * 5 + [1] * 5 + [2] * 5 + [3]


# ───────────── simulador ─────────────
def test_scalp_sl_wins_when_both_touched_and_pays_stop_slip():
    b = _flat(10)
    b.high[2], b.low[2] = 105.0, 95.0
    tr = sc.simulate_scalp(b, 0, LONG, 1.0, 2.0, len(b), Costs())
    assert tr.reason == "sl" and tr.exit_idx == 2
    assert tr.exit_price == pytest.approx(tr.sl * (1 - 0.0003 - 0.0010))
    assert tr.fee_frac == pytest.approx(0.0005 * (1 + tr.exit_price / tr.entry_price))


def test_scalp_time_exit_after_4h():
    b = _flat(400)
    tr = sc.simulate_scalp(b, 0, LONG, 1.0, 1.0, len(b), Costs())
    assert tr.reason == "time" and tr.exit_idx == 1 + 240
    assert tr.exit_time == int(b.open_time[241])


def test_scalp_tp_taker_vs_maker():
    b = _flat(10)
    b.high[3] = 103.0                              # atravessa o TP dos dois
    costs = Costs()
    b.low[1] = 99.9                                # maker preenche no limit (100) na barra 1
    tk = sc.simulate_scalp(b, 0, LONG, 1.0, 2.0, len(b), costs)
    mk = sc.simulate_scalp(b, 0, LONG, 1.0, 2.0, len(b), costs, maker_limit=100.0, maker_tp=True)
    assert tk.reason == mk.reason == "tp"
    assert tk.exit_price == pytest.approx(tk.tp * (1 - 0.0003))
    assert mk.exit_price == pytest.approx(mk.tp)
    assert mk.fee_frac == pytest.approx(0.0002 + 0.0002 * mk.exit_price / mk.entry_price)


def test_maker_entry_needs_trade_through_and_fills_at_limit():
    b = _flat(10)
    b.low[1] = 99.0 * (1 - 0.00005)               # não atravessa 0,01%
    assert sc.simulate_scalp(b, 0, LONG, 1.0, 1.0, len(b), Costs(), maker_limit=99.0) is None
    b.low[2] = 99.0 * (1 - 0.0002)                # atravessa
    tr = sc.simulate_scalp(b, 0, LONG, 1.0, 1.0, len(b), Costs(), maker_limit=99.0)
    assert tr.entry_idx == 2 and tr.entry_price == pytest.approx(99.0)
    assert tr.sl == pytest.approx(98.0) and tr.tp == pytest.approx(100.0)


def test_maker_entry_cancelled_after_k_bars():
    b = _flat(10)
    b.low[1 + sc.MAKER_K] = 90.0                   # só atravessa na barra k+1
    assert sc.simulate_scalp(b, 0, LONG, 1.0, 1.0, len(b), Costs(), maker_limit=99.0) is None


def test_maker_tp_needs_trade_through():
    b = _flat(10)
    b.low[1] = 99.9
    b.high[3] = 102.0 * (1 + 0.00005)              # toca o TP mas não atravessa 0,01%
    mk = sc.simulate_scalp(b, 0, LONG, 1.0, 2.0, len(b), Costs(), maker_limit=100.0, maker_tp=True)
    assert mk.reason == "time" or mk.reason == "eod"


def test_short_levels_mirrored():
    b = _flat(10)
    b.high[2] = 101.5
    tr = sc.simulate_scalp(b, 0, SHORT, 1.0, 2.0, len(b), Costs())
    assert tr.sl > tr.entry_price > tr.tp and tr.reason == "sl"


# ───────────── universo ─────────────
def test_member_mask_and_months():
    ct = np.array([to_ms("2022-06-01"), to_ms("2023-06-01"), to_ms("2024-06-01")], np.int64)
    assert sc.member_mask("LUNAUSDT", ct).tolist() == [True, False, False]
    assert sc.member_mask("DOGEUSDT", ct).tolist() == [False, True, True]
    m = sc.member_months("LUNAUSDT")
    assert "2021-12" in m and "2022-05" in m and "2023-01" not in m


# ───────────── download filtrado por mês ─────────────
def test_ensure_raw_downloads_only_requested_months(tmp_path, monkeypatch):
    from scripts.backtest_v2 import vision

    monkeypatch.setattr(vision, "RAW_DIR", tmp_path)
    keys = [f"data/futures/um/monthly/klines/XUSDT/1m/XUSDT-1m-2022-{m:02d}.zip" for m in (1, 2, 3)]
    monkeypatch.setattr(vision, "list_keys", lambda http, kind, sym, tf="": keys)
    got = []

    class H:
        def get(self, url, params=None):
            got.append(url)
            return b"zip"

    n = vision.ensure_raw("XUSDT", "klines", "1m", H(), months={"2022-02"})
    assert n == 1 and got == [vision.FILE_URL + keys[1]]
    assert sorted(p.name for p in (tmp_path / "klines" / "1m" / "XUSDT").glob("*.zip")) == ["XUSDT-1m-2022-02.zip"]

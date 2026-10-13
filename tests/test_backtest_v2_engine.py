"""Testes do motor do scripts/backtest_v2 (fees, SL-first, funding, sem lookahead)."""
import numpy as np
import pytest

from scripts.backtest_v2.data import Bars, tf_ms
from scripts.backtest_v2.engine import (
    Costs,
    PortfolioConfig,
    SymbolData,
    run_portfolio,
    simulate_trade,
)
from scripts.backtest_v2.strategies import (
    LONG,
    SHORT,
    LivePullback,
    SignalSet,
    aligned_index,
)

T0 = 1_700_000_000_000 - (1_700_000_000_000 % 3_600_000)
H = tf_ms("1h")


def _bars(o, h, lw, c, tf="1h", v=None, sym="TESTUSDT"):
    n = len(o)
    return Bars(sym, tf, T0 + np.arange(n, dtype=np.int64) * tf_ms(tf), np.asarray(o, float),
                np.asarray(h, float), np.asarray(lw, float), np.asarray(c, float),
                np.asarray(v if v is not None else np.ones(n), float))


ZERO = Costs(fee_rate=0.0, entry_slip=0.0, exit_slip=0.0, stop_slip=0.0)


def test_fees_charged_on_both_legs():
    b = _bars([100] * 5, [100] * 5, [100] * 5, [100] * 5)
    c = Costs(fee_rate=0.0005, entry_slip=0.0, exit_slip=0.0, stop_slip=0.0)
    tr = simulate_trade(b, 0, LONG, {}, c, end_idx=5)
    assert tr.reason == "eod"                      # aberto no fim → fechado, não descartado
    assert tr.gross_frac == pytest.approx(0.0)
    assert tr.fee_frac == pytest.approx(0.001)     # 0,05% × 2 pernas
    assert tr.net_frac == pytest.approx(-0.001)


def test_entry_is_next_bar_open_with_slippage():
    b = _bars([100, 110, 110], [100, 111, 111], [100, 109, 109], [100, 110, 110])
    tr = simulate_trade(b, 0, LONG, {}, Costs(0, 0.001, 0, 0), end_idx=3)
    assert tr.entry_idx == 1
    assert tr.entry_price == pytest.approx(110 * 1.001)


def test_sl_first_when_bar_touches_both():
    # entrada 100 (abertura da barra 1); SL 1% = 99, TP 2% = 102; barra 2 toca os dois
    b = _bars([100, 100, 100.5], [100, 100.5, 103], [100, 99.5, 98], [100, 100.4, 100])
    tr = simulate_trade(b, 0, LONG, {"sl_pct": 1.0, "rr": 2.0}, ZERO, end_idx=3)
    assert tr.reason == "sl" and tr.exit_idx == 2
    assert tr.exit_price == pytest.approx(99.0)
    # short espelhado
    tr = simulate_trade(b, 0, SHORT, {"sl_pct": 1.0, "rr": 2.0}, ZERO, end_idx=3)
    assert tr.reason == "sl" and tr.exit_price == pytest.approx(101.0)


def test_stop_slippage_and_gap_fill_at_open():
    # gap: barra 2 abre em 97, abaixo do SL 99 → preenche na abertura, menos stop_slip
    b = _bars([100, 100, 97], [100, 100.5, 97.5], [100, 99.5, 96], [100, 100, 97])
    c = Costs(fee_rate=0, entry_slip=0, exit_slip=0.0003, stop_slip=0.001)
    tr = simulate_trade(b, 0, LONG, {"sl_pct": 1.0, "rr": 2.0}, c, end_idx=3)
    assert tr.reason == "sl"
    assert tr.exit_price == pytest.approx(97 * (1 - 0.0013))


def test_window_end_truncates_without_lookahead():
    # TP só seria tocado na barra 4, mas a janela termina em 3 → fecha eod no close da barra 2
    b = _bars([100] * 5, [100, 100, 100, 100, 110], [100] * 5, [100, 100, 101, 100, 100])
    tr = simulate_trade(b, 0, LONG, {"sl_pct": 1.0, "rr": 2.0}, ZERO, end_idx=3)
    assert tr.reason == "eod" and tr.exit_idx == 2 and tr.exit_price == pytest.approx(101)


def test_funding_sign_long_pays_short_receives():
    n = 20
    b = _bars([100] * n, [100] * n, [100] * n, [100] * n)
    # funding na abertura da barra 1 (= instante da entrada: NÃO conta) e na barra 9 (conta)
    funding = np.array([[T0 + 1 * H, 0.001, np.nan], [T0 + 9 * H, 0.001, np.nan]])
    long_tr = simulate_trade(b, 0, LONG, {}, ZERO, end_idx=n, funding=funding)
    short_tr = simulate_trade(b, 0, SHORT, {}, ZERO, end_idx=n, funding=funding)
    assert long_tr.funding_frac == pytest.approx(-0.001)
    assert short_tr.funding_frac == pytest.approx(+0.001)
    neg = funding.copy()
    neg[:, 1] = -0.002
    assert simulate_trade(b, 0, LONG, {}, ZERO, end_idx=n, funding=neg).funding_frac == pytest.approx(0.002)


def test_exit_flag_exits_at_next_open():
    b = _bars([100, 100, 105, 107, 108], [101, 101, 106, 108, 109], [99.5, 99.5, 104, 106, 107],
              [100, 100, 105, 107, 108])
    flags = np.array([False, False, True, False, False])
    tr = simulate_trade(b, 0, LONG, {}, ZERO, end_idx=5, exit_flags=flags)
    assert tr.reason == "signal" and tr.exit_idx == 3 and tr.exit_price == pytest.approx(107)
    assert tr.exit_time == int(b.open_time[3])


def test_aligned_index_has_no_lookahead():
    ex = _bars([1] * 40, [1] * 40, [1] * 40, [1] * 40, tf="3m")
    htf = _bars([1] * 3, [1] * 3, [1] * 3, [1] * 3, tf="1h")
    idx = aligned_index(ex.close_time, htf)
    # 3m barras 0..19 fecham antes da 1ª barra de 1h fechar → nenhum HTF visível
    assert (idx[:19] == -1).all()
    # barra 3m nº19 fecha junto com a 1ª de 1h (00:59:59.999) → visível
    assert idx[19] == 0
    assert idx[20] == 0 and idx[39] == 1


def test_portfolio_closes_open_position_at_end_and_sizes_by_risk():
    n = 10
    b = _bars([100] * n, [100.5] * n, [99.5] * n, [100] * n)
    sd = {"TESTUSDT": SymbolData(b, SignalSet(np.array([0]), np.array([LONG], np.int8)), None)}
    pc = PortfolioConfig(initial_equity=1000, risk_pct=1.0, max_pos_mult=1.0, max_gross_mult=3.0)
    res = run_portfolio(sd, [(T0, T0 + n * H, {"sl_pct": 2.0, "rr": 3.0})], T0, T0 + n * H, ZERO, pc)
    assert len(res.trades) == 1 and res.trades[0].reason == "eod"
    assert res.trades[0].notional == pytest.approx(500.0)  # 1% de 1000 / 2% de stop


def _synthetic_1m(seed, n):
    rng = np.random.default_rng(seed)
    drift = np.repeat(rng.choice([-1, 1], size=n // 2000 + 1) * 4e-4, 2000)[:n]
    r = drift + rng.normal(0, 1.5e-3, n)
    close = 100 * np.exp(np.cumsum(r))
    op = np.concatenate([[100], close[:-1]])
    hi = np.maximum(op, close) * (1 + np.abs(rng.normal(0, 8e-4, n)))
    lo = np.minimum(op, close) * (1 - np.abs(rng.normal(0, 8e-4, n)))
    vol = rng.lognormal(0, 0.5, n)
    return op, hi, lo, close, vol


def _agg(o, h, lw, c, v, k, tf):
    m = len(o) // k

    def sl(x):
        return x[: m * k].reshape(m, k)

    return Bars("SYN", tf, T0 + np.arange(m, dtype=np.int64) * tf_ms(tf), sl(o)[:, 0], sl(h).max(1),
                sl(lw).min(1), sl(c)[:, -1], sl(v).sum(1))


def test_live_pullback_prefilter_is_lossless():
    """Pré-filtro vetorizado não pode descartar sinal que a função real daria."""
    o, h, lw, c, v = _synthetic_1m(7, 60 * 400)
    bars = {"3m": _agg(o, h, lw, c, v, 3, "3m"), "5m": _agg(o, h, lw, c, v, 5, "5m"),
            "1h": _agg(o, h, lw, c, v, 60, "1h")}
    st = LivePullback("3m", "5m", "1h", [{}])
    t_a = int(bars["3m"].open_time[5200])
    t_b = int(bars["3m"].open_time[7600])
    fast = st.compute("SYN", bars, t_a, t_b, use_prefilter=True)
    slow = st.compute("SYN", bars, t_a, t_b, use_prefilter=False)
    assert fast.idx.tolist() == slow.idx.tolist()
    assert fast.side.tolist() == slow.side.tolist()
    assert len(slow.idx) > 0, "dados sintéticos deveriam gerar ao menos 1 sinal"


def test_daily_ema_trend_matches_independent_loop():
    """Trend diário via motor == loop independente (entra/sai na abertura seguinte)."""
    from scripts.backtest_v2.strategies import DailyEMATrend, ema_full

    rng = np.random.default_rng(3)
    n = 900
    c = 100 * np.exp(np.cumsum(np.sin(np.arange(n) / 60) * 0.004 + rng.normal(0, 0.02, n)))
    o = np.concatenate([[100], c[:-1]])
    b = Bars("SYN", "1d", T0 + np.arange(n, dtype=np.int64) * tf_ms("1d"), o, np.maximum(o, c) * 1.01,
             np.minimum(o, c) * 0.99, c, np.ones(n))
    st = DailyEMATrend(long_short=False, n_universe=1)
    t_end = int(b.open_time[-1]) + tf_ms("1d")
    ss = st.compute("SYN", {"1d": b}, int(b.open_time[0]), t_end)
    pc = PortfolioConfig(sizing="fraction", fraction=1.0, max_pos_mult=1.0, max_gross_mult=1.0, max_concurrent=1)
    res = run_portfolio({"SYN": SymbolData(b, ss, None)}, [(0, t_end, {})], 0, t_end, ZERO, pc)
    f, s = ema_full(c, 50), ema_full(c, 200)
    up = (np.arange(n) >= 199) & (f > s)
    exp, pos = [], False
    for i in range(n - 1):
        if not pos and up[i]:
            pos, ent = True, o[i + 1]
        elif pos and not up[i]:
            pos = False
            exp.append(o[i + 1] / ent - 1)
    if pos:
        exp.append(c[-1] / ent - 1)
    assert len(exp) >= 2
    assert [t.gross_frac for t in res.trades] == pytest.approx(exp)

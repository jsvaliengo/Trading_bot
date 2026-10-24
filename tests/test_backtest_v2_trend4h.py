"""Testes do trend4h: universo point-in-time, ATR, sinais sem lookahead, stop ATR,
sizing por volatilidade e parsing dos arquivos do data.binance.vision."""
import io
import zipfile

import numpy as np
import pytest

from scripts.backtest_v2 import trend4h as t4
from scripts.backtest_v2 import vision
from scripts.backtest_v2.data import Bars, to_ms
from scripts.backtest_v2.strategies import LONG, SHORT

DAY = t4.DAY
H4 = 4 * 3_600_000


def _daily(start_ms, n, qv=1.0):
    ot = start_ms + np.arange(n, dtype=np.int64) * DAY
    a = np.ones((n, 7))
    a[:, 0] = ot
    a[:, 6] = qv
    return a


# ───────────── universo ─────────────
def test_rebalance_times_are_mondays():
    ts = t4.rebalance_times(to_ms("2020-01-01"), to_ms("2020-02-01"))
    assert [int(t) for t in ts] == [to_ms(d) for d in ("2020-01-06", "2020-01-13", "2020-01-20", "2020-01-27")]


def test_universe_uses_only_bars_closed_before_rebalance():
    T = to_ms("2020-03-09")                      # segunda
    start = T - 100 * DAY
    a, b = _daily(start, 120, 1.0), _daily(start, 120, 2.0)
    # A explode de volume no dia que ABRE em T (fecha depois de T): não pode contar em T
    a[100, 6] = 1e9
    assert int(a[100, 0]) == T
    syms, sc = t4.universe_scores({"AUSDT": a, "BUSDT": b}, np.array([T, T + 7 * DAY]))
    assert sc[0].tolist() == [30.0, 60.0]        # 30 dias × volume, sem o pico
    assert sc[1, 0] > 1e9                         # na semana seguinte o pico já fechou
    uni = t4.Universe.build({"AUSDT": a, "BUSDT": b}, T, T + 14 * DAY, top_n=1)
    assert uni.members == [frozenset({"BUSDT"}), frozenset({"AUSDT"})]
    assert uni.contains("AUSDT", np.array([T + DAY, T + 8 * DAY, T - DAY])).tolist() == [False, True, False]


def test_universe_eligibility_needs_history_and_live_trading():
    T = to_ms("2020-06-01")
    young = _daily(T - 59 * DAY, 59)             # só 59 dias antes de T
    dead = _daily(T - 200 * DAY, 150)            # parou de negociar 50 dias antes de T
    ok = _daily(T - 60 * DAY, 60)
    _, sc = t4.universe_scores({"AUSDT": young, "BUSDT": dead, "CUSDT": ok}, np.array([T]))
    assert np.isinf(sc[0, 0]) and np.isinf(sc[0, 1]) and sc[0, 2] == 30.0


# ───────────── indicadores e sinais ─────────────
def test_atr_wilder_matches_hand_computation():
    h = np.array([10.0, 11, 12, 13])
    lw = np.array([9.0, 10, 10, 12])
    c = np.array([9.5, 10.5, 11, 12.5])
    # TR = [1, 1.5, 2, 2]; ATR(2): seed mean(1,1.5)=1.25; (1.25+2)/2=1.625; (1.625+2)/2=1.8125
    assert t4.atr_wilder(h, lw, c, n=2)[1:].tolist() == [1.25, 1.625, 1.8125]
    assert np.isnan(t4.atr_wilder(h, lw, c, n=2)[0])


def _bars(c, h=None, lw=None):
    c = np.asarray(c, float)
    h = c + 0.5 if h is None else np.asarray(h, float)
    lw = c - 0.5 if lw is None else np.asarray(lw, float)
    ot = to_ms("2021-01-04") + np.arange(len(c), dtype=np.int64) * H4
    return Bars("XUSDT", "4h", ot, c.copy(), h, lw, c, np.ones(len(c)))


def _truncate(b, n):
    return Bars(b.symbol, b.tf, b.open_time[:n], b.open[:n], b.high[:n], b.low[:n], b.close[:n], b.volume[:n])


@pytest.mark.parametrize("cell", [{"v": "don", "n": 5, "m": 3}, {"v": "tsmom", "L": 4}])
def test_signals_have_no_lookahead(cell, monkeypatch):
    monkeypatch.setattr(t4, "SIGMA_N", 10)
    rng = np.random.default_rng(1)
    b = _bars(100 * np.exp(np.cumsum(rng.normal(0, 0.02, 300))))
    full = t4.cell_signals(b, cell)
    for n in (50, 120, 299):
        for f, p in zip(full, t4.cell_signals(_truncate(b, n), cell)):
            assert f[:n].tolist() == p.tolist()
    assert int(full[0].sum()) > 0 and int(full[1].sum()) > 0


def test_donchian_breakout_uses_prior_bars_only():
    c = [10, 10, 10, 10, 11, 10, 9, 8, 7]
    el, es, xl, xs = t4.donchian_signals(_bars(c), n=3, m=2)
    # barra 4: close 11 > máx das 3 máximas anteriores (10.5)
    assert np.nonzero(el)[0].tolist() == [4]
    # barra 6: close 9 < mín das 3 mínimas anteriores (9.5) → SHORT; saída LONG (canal 2) já na 6
    assert np.nonzero(es)[0].tolist() == [6, 7, 8]
    assert xl[6] and not xl[5]


def test_tsmom_cross_needs_previous_bar_below_threshold(monkeypatch):
    monkeypatch.setattr(t4, "SIGMA_N", 5)
    c = np.array([100, 101, 99, 100, 101, 100, 99, 100, 106, 112, 118, 119], float)
    el, es, xl, xs = t4.tsmom_signals(_bars(c), L=2)
    z = t4.tsmom_z(c, 2)
    hits = np.nonzero(el)[0].tolist()
    assert hits and all(z[i] > 0.5 and z[i - 1] <= 0.5 for i in hits)
    assert not el[hits[0] + 1]                   # continua acima: não é novo cruzamento


# ───────────── stop ATR + sizing por volatilidade ─────────────
def _book_one_trade(monkeypatch, c, h, lw, k, atr_val, members=("XUSDT",)):
    monkeypatch.setattr(t4, "COSTS", t4.Costs(0.0, 0.0, 0.0, 0.0))
    monkeypatch.setattr(t4, "atr_wilder", lambda hh, ll, cc: np.full(len(cc), atr_val))
    b = Bars("XUSDT", "4h", to_ms("2021-01-04") + np.arange(len(c), dtype=np.int64) * H4,
             np.asarray(c, float), np.asarray(h, float), np.asarray(lw, float), np.asarray(c, float), np.ones(len(c)))
    t0, t1 = int(b.open_time[0]), int(b.open_time[-1]) + H4
    uni = t4.Universe(np.array([t0 - DAY]), [frozenset(members)])
    cell = {"v": "don", "n": 2, "m": 2, "k": k}
    book = t4.Book({"XUSDT": b}, {}, uni, [cell])
    return book, book.run([(t0, t1, cell)], t0, t1)


def test_atr_stop_and_risk_sizing(monkeypatch):
    # breakout na barra 2 (close 104 > 101); entrada 104 na abertura da 3; ATR 2, k=2 → stop 100
    c = [100, 100, 104, 104, 103, 101, 101]
    h = [101, 101, 104.5, 104.5, 104, 102, 102]
    lw = [99, 99, 103, 103, 102, 99.5, 100]
    _, res = _book_one_trade(monkeypatch, c, h, lw, k=2, atr_val=2.0)
    tr = res.trades[0]
    assert (tr.side, tr.entry_idx, tr.entry_price) == (LONG, 3, 104.0)
    assert tr.sl == pytest.approx(100.0)                          # k×ATR do sinal, medido a partir do fill
    assert tr.reason == "sl" and tr.exit_idx == 5 and tr.exit_price == pytest.approx(100.0)
    # risco 0,5% de 10.000 = 50 até o stop; distância 4/104 → notional 1300
    assert tr.notional == pytest.approx(1300.0)
    assert tr.pnl == pytest.approx(-50.0)


def test_position_cap_binds_when_stop_is_tiny(monkeypatch):
    c = [100, 100, 104, 104, 104, 104]
    h = [101, 101, 104.5, 104.5, 104.5, 104.5]
    lw = [99, 99, 103.97, 103.97, 103.97, 103.97]
    _, res = _book_one_trade(monkeypatch, c, h, lw, k=1, atr_val=0.05)   # stop a 0,05% → 10x pelo risco
    assert res.trades[0].notional == pytest.approx(10_000.0)              # teto 1,0× equity


def test_book_ignores_signals_outside_universe(monkeypatch):
    c = [100, 100, 104, 104, 104]
    book, res = _book_one_trade(monkeypatch, c, np.add(c, 0.5), np.subtract(c, 0.5), k=2, atr_val=2.0,
                                members=("OTHERUSDT",))
    assert len(book.sym_data["XUSDT"].signals.idx) == 0 and res.trades == []


def test_short_stop_above_entry(monkeypatch):
    c = [100, 100, 96, 96, 97, 99, 99]
    h = [101, 101, 97, 97, 98, 100.5, 100]
    lw = [99, 99, 95.5, 95.5, 96, 98, 98]
    _, res = _book_one_trade(monkeypatch, c, h, lw, k=2, atr_val=2.0)
    tr = res.trades[0]
    assert tr.side == SHORT and tr.sl == pytest.approx(100.0) and tr.reason == "sl" and tr.exit_idx == 5


# ───────────── métricas ─────────────
def test_year_returns_and_sharpe_ci():
    days = np.arange(to_ms("2021-01-01"), to_ms("2023-01-01") + 1, DAY, dtype=np.int64)
    eq = np.where(days < to_ms("2022-01-01"), 100.0, 110.0)
    eq[-1] = 99.0
    yr = t4.year_returns(days, eq)
    assert yr["2021"] == pytest.approx(10.0) and yr["2022"] == pytest.approx(-10.0)
    r = np.random.default_rng(0).normal(0.001, 0.01, 730)
    lo, hi = t4.sharpe_week_block_ci(r, days[1:731], n_boot=500)
    assert lo < t4.sharpe(r) < hi


# ───────────── parsing do data.binance.vision ─────────────
def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


def test_parse_klines_with_and_without_header_and_bad_rows():
    t = to_ms("2020-01-01")
    rows = [f"{t},10,11,9,10.5,100,{t + H4 - 1},1050,1,0,0,0",
            f"{(t + H4) * 1000},10.5,12,10,11,50,0,550,1,0,0,0",       # microssegundos
            f"{t + 2 * H4},10,9,11,10,1,0,10,1,0,0,0",                 # high < low → fora
            f"{t + 3 * H4 + 5},10,11,9,10,1,0,10,1,0,0,0",             # desalinhado → fora
            f"{t + 4 * H4},abc,11,9,10,1,0,10,1,0,0,0"]                # lixo → fora
    header = "open_time,open,high,low,close,volume,close_time,quote_volume,count,tbv,tbqv,ignore\n"
    for blob in (_zip({"X-4h.csv": header + "\n".join(rows)}), _zip({"X-4h.csv": "\n".join(rows)})):
        a = vision.parse_klines(blob, "4h")
        assert a[:, 0].astype(np.int64).tolist() == [t, t + H4]
        assert a[:, 6].tolist() == [1050.0, 550.0]


def test_parse_ignores_non_csv_and_nested_members():
    t = to_ms("2020-01-01")
    row = f"{t},10,11,9,10.5,100,0,1050,1,0,0,0"
    blob = _zip({"../evil.csv": row, "a/b.csv": row, "run.py": row, "ok.csv": row})
    assert len(vision.parse_klines(blob, "4h")) == 1


def test_parse_funding():
    blob = _zip({"F.csv": "calc_time,funding_interval_hours,last_funding_rate\n"
                          "1577836800000,8,-0.00012359\n1577865600005,8,0.0001\n1577894400000,8,nan\n"})
    a = vision.parse_funding(blob)
    assert a[:, 0].astype(np.int64).tolist() == [1577836800000, 1577865600005]
    assert a[:, 1].tolist() == [-0.00012359, 0.0001] and np.isnan(a[:, 2]).all()


def test_candidate_filter():
    assert vision.is_candidate("BTCUSDT") and vision.is_candidate("1000PEPEUSDT")
    assert not vision.is_candidate("BTCUSDT_230331")
    assert not vision.is_candidate("USDCUSDT") and not vision.is_candidate("ETHBTC")
    assert not vision.is_candidate("../x/USDT")

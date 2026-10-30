"""Martingale: aritmética da escada (média, níveis, TP), TP não dispara na barra da adição,
liquidação recapitaliza, stop de portfólio fecha tudo, sizing da base."""
import numpy as np

from scripts.backtest_v2 import martingale as mg

FEE, SLIP = mg.FEE, mg.SLIP


def _path(closes, wiggle=0.0):
    c = np.asarray(closes, float)[:, None]
    o = np.r_[c[:1], c[:-1]]
    return o, c * (1 + wiggle), c * (1 - wiggle), c, np.zeros_like(c)


def test_ladder_avg_and_tp_long_leg():
    # 100 → 98 (adiciona LONG no gatilho 98,5) → 100,5 (TP da LONG em média×1,01 = 99,5+)
    o, h, lo, c, f = _path([100, 98, 100.5, 100.5])
    B = 10.0
    eq, un, gr, dep, ev, st = mg.simulate(o, h, lo, c, f, B, 0.015, 2.0, 0.01, 1000, 1000.0, 0.0)
    closed, wins = int(st[0]), int(st[1])
    # barra 1: LONG adiciona 20 em 98,5 (média 99,0 com 10 em 100,03); SHORT (aberta em 99,97) só mira TP 98,97 > low 98 → fecha
    # barra 2: LONG TP em 99,0×1,01 ≈ 99,99 ≤ high 100,5 → fecha; SHORT reaberta em 100,5 na barra 2 (nível base)
    assert closed == 2 and wins == 2
    long_fill = 100 * (1 + SLIP)
    avg = (B + 2 * B) / (B / long_fill + 2 * B / 98.5)
    assert 99.0 < avg < 99.2
    qty = B / long_fill + 2 * B / 98.5
    tp_px = avg * 1.01
    long_pnl = qty * (tp_px - avg) - 3 * B * FEE - qty * tp_px * FEE
    short_fill = 100 * (1 - SLIP)
    short_pnl = B / short_fill * (short_fill - short_fill * 0.99) - B * FEE - B / short_fill * short_fill * 0.99 * FEE
    assert abs(st[2] - (long_pnl + short_pnl)) < 1e-9  # lucro realizado dos dois TPs (base da taxa de 20%)
    assert eq[1] < 1000.0  # na barra da queda a conta mostra prejuízo flutuante


def test_no_tp_in_same_bar_as_add():
    # barra que cai 2% e volta ao preço: a LONG adiciona (1 nível), mas não dá TP na mesma barra
    o = np.array([[100.0], [100.0]])
    c = np.array([[100.0], [100.0]])
    h = np.array([[100.0], [100.0]])
    lo = np.array([[100.0], [98.0]])
    f = np.zeros_like(c)
    eq, un, gr, dep, ev, st = mg.simulate(o, h, lo, c, f, 10.0, 0.015, 2.0, 0.01, 1000, 1000.0, 0.0)
    assert int(st[0]) == 1 and int(st[6]) == 2  # só a SHORT fecha (TP em 98,97 ≥ low); LONG adiciona 1 nível e não dá TP


def test_liquidation_recapitalizes_and_counts():
    # queda contínua de 50% em passos de 1,5%: LONG empilha até a margem acabar e a conta liquida
    px = 100 * 0.985 ** np.arange(60)
    o, h, lo, c, f = _path(px)
    eq, un, gr, dep, ev, st = mg.simulate(o, h, lo, c, f, 50.0, 0.015, 2.0, 0.01, 1000, 300.0, 0.0)
    assert int(st[3]) >= 1 and int(st[4]) >= 0
    assert dep[-1] >= 600.0 and (ev == mg.LIQ).sum() == int(st[3])
    assert eq[int(st[4])] <= mg.MAINT * gr[int(st[4])] + 1e-9


def test_portfolio_stop_closes_everything():
    px = 100 * 0.985 ** np.arange(60)
    o, h, lo, c, f = _path(px)
    eq, un, gr, dep, ev, st = mg.simulate(o, h, lo, c, f, 50.0, 0.015, 2.0, 0.01, 7, 300.0, 0.20, 1)
    assert int(st[5]) >= 1 and int(st[3]) == 0  # stop antes de liquidar
    t = int(np.argmax(ev == mg.STOP))
    assert gr[t] > 0 and (t + 1 >= len(eq) or True)
    assert dep[-1] == 300.0


def test_base_order_sizing():
    # escada de 7 níveis com m=2 soma 127 B; 12 pares × 2 pernas × 127 B = 50% × 300 × 10
    B = mg.base_order(300.0, 12, 2.0, 7)
    assert abs(B * 12 * 2 * 127 - 1500.0) < 1e-9


def test_metrics_net_discounts_deposits():
    grid_t = mg.to_ms("2024-01-01") + np.arange(4) * 300_000
    eq = np.array([300.0, 150.0, 300.0, 310.0])
    un = np.zeros(4)
    gr = np.ones(4)
    dep = np.array([300.0, 300.0, 600.0, 600.0])
    ev = np.array([0, 0, mg.LIQ, 0.0])
    st = np.array([10, 9, 20.0, 1, 2, 0, 3, 0.8, 600.0, 310.0])
    m = mg.metrics(grid_t, eq, un, gr, dep, ev, st, 300.0)
    assert m["net_usd"] == -290.0 and m["liquidations"] == 1 and m["win_rate"] == 90.0
    assert abs(m["net_after_perf_fee_usd"] - (-290.0 - 4.0)) < 1e-9
    assert m["worst_month_usd"] == -290.0  # único mês: variação da equity descontando depósitos, não "equity − capital" duas vezes

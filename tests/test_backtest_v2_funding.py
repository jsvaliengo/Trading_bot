"""Cash-and-carry: sinal de funding, histerese de entrada/saída, contabilidade da posição
(funding recebido pelo short, basis, taxas), rebalance e liquidação da perna short."""
import numpy as np

from scripts.backtest_v2 import funding_carry as fc

FEES = fc.FEES["A_taker"]


def _series(spot, perp, funding=None, high=None):
    n = len(spot)
    t = np.arange(n, dtype=np.int64) * fc.STEP
    spot, perp = np.asarray(spot, float), np.asarray(perp, float)
    hi = perp.copy() if high is None else np.asarray(high, float)
    f = np.zeros(n) if funding is None else np.asarray(funding, float)
    return fc.Series("X", t, spot, perp, hi, f)


def test_signal_annualizes_trailing_mean():
    sig = fc.signal(np.array([0.0001, 0.0002, 0.0003, 0.0004]), 3)
    assert np.isnan(sig[:2]).all()
    assert abs(sig[2] - 0.0002 * fc.STEPS_PER_YEAR) < 1e-12
    assert abs(sig[3] - 0.0003 * fc.STEPS_PER_YEAR) < 1e-12


def test_threshold_hysteresis():
    sig = np.array([0.04, 0.12, 0.08, 0.06, 0.04, 0.12])
    ok = np.ones(6, dtype=bool)
    tg = fc.target_threshold(sig, ok, 0.10)
    # entra acima de 10%, fica enquanto >= 5%, sai abaixo de 5%, reentra acima de 10%
    assert tg.tolist() == [False, True, True, True, False, True]


def test_spot_symbol_mapping():
    assert fc.spot_symbol("1000PEPEUSDT") == ("PEPEUSDT", 1000.0)
    assert fc.spot_symbol("1000SATSUSDT") == ("1000SATSUSDT", 1.0)
    assert fc.spot_symbol("BTCUSDT") == ("BTCUSDT", 1.0)


def test_short_receives_positive_funding_and_pays_basis():
    # preços parados, funding +0,01% por passo: só funding e taxas de entrada
    s = _series([100] * 4, [101] * 4, funding=[0, 0.0001, 0.0001, 0.0001])
    st = fc.simulate([s], np.ones((4, 1), dtype=bool), 1, 1.0, FEES, 300.0)
    fs, fp = FEES
    q = 150 / (100 * (1 + fs))
    assert abs(st["funding"] - 3 * 0.0001 * q * 101) < 1e-9  # entrou em t=0, recebe t=1..3
    assert st["entries"] == 1 and st["liquidations"] == 0 and st["rebalances"] == 0
    expected = 300 - q * 100 * fs - q * 101 * fp + st["funding"]
    assert abs(st["equity"][-1] - expected) < 1e-9
    # funding negativo: short paga
    s2 = _series([100] * 2, [100] * 2, funding=[0, -0.001])
    st2 = fc.simulate([s2], np.ones((2, 1), dtype=bool), 1, 1.0, FEES, 300.0)
    assert st2["funding"] < 0


def test_delta_neutral_ignores_price_level_but_not_basis():
    # spot e perp sobem 60% juntos: equity só muda pelas taxas (e rebalance). Basis abre 2%: perde
    s = _series([100, 160], [100, 160])
    st = fc.simulate([s], np.ones((2, 1), dtype=bool), 1, 1.0, FEES, 300.0)
    assert st["rebalances"] == 1  # perp a 1× subindo 60%: margem cai abaixo da metade → rebalance encolhe a posição
    assert st["liquidations"] == 0
    s2 = _series([100, 100], [100, 102])
    st2 = fc.simulate([s2], np.ones((2, 1), dtype=bool), 1, 1.0, FEES, 300.0)
    assert st2["equity"][-1] < st2["equity"][0]


def test_rebalance_restores_target_margin():
    s = _series([100, 130], [100, 130])
    st = fc.simulate([s], np.ones((2, 1), dtype=bool), 1, 2.0, FEES, 300.0)
    assert st["rebalances"] == 1
    # com a posição encolhida a margem volta a 1/L: equity ≈ capital − taxas (sem funding, sem basis)
    assert 290 < st["equity"][-1] < 300


def test_liquidation_on_intrastep_high():
    # L=2: margem 100 para notional 200; alta de +60% na máxima liquida mesmo que o close volte
    s = _series([100, 100, 100], [100, 100, 100], high=[100, 160, 100])
    st = fc.simulate([s], np.ones((3, 1), dtype=bool), 1, 2.0, FEES, 300.0)
    assert st["liquidations"] == 1
    assert st["entries"] == 2  # reabre no passo seguinte
    assert st["equity"][1] < 220  # perdeu a margem do perp (~$100)


def test_rotation_keeps_holder_while_in_top_n():
    sigs = np.array([[0.3, 0.2, 0.1], [0.2, 0.3, 0.1], [0.1, 0.3, 0.2], [-1, -1, -1.0]])
    oks = np.ones_like(sigs, dtype=bool)
    t = np.arange(4, dtype=np.int64) * fc.STEP
    tg = fc.target_rotation(sigs, oks, t, 1, daily=False)
    assert tg.tolist() == [[True, False, False], [False, True, False], [False, True, False], [False, False, False]]
    tg2 = fc.target_rotation(sigs, oks, t, 2, daily=False)
    assert tg2[2].tolist() == [False, True, True]  # 0 caiu pro 3º lugar → sai; 2 entra


def test_metrics_basic():
    t = np.arange(4, dtype=np.int64) * fc.STEP
    m = fc.metrics(np.array([303.0, 300.0, 306.0, 309.0]), t, 300.0)
    assert abs(m["net_usd"] - 9) < 1e-9 and abs(m["max_dd"] - 1 / 101) < 1e-9
    assert m["sharpe"] > 0


def test_build_series_drops_funding_before_grid(monkeypatch):
    t0 = 10 * fc.STEP
    kl = np.array([[t0 - 4 * 3_600_000 + i * 4 * 3_600_000, 1, 1, 1, 1, 1, 1] for i in range(6)], float)
    fund = np.array([[t0 - 3 * fc.STEP, 0.5, 0], [t0 - fc.STEP, 0.5, 0], [t0, 0.001, 0], [t0 + fc.STEP, 0.002, 0]])
    monkeypatch.setattr(fc.vision, "load_klines", lambda s, tf: kl)
    monkeypatch.setattr(fc, "load_spot", lambda s: kl)
    monkeypatch.setattr(fc.vision, "load_funding", lambda s: fund)
    se = fc.build_series("X", t0, t0 + 3 * fc.STEP)
    assert se.funding.tolist() == [0.001, 0.002, 0.0]

"""Market making: binning de aggTrades, fill fim-de-fila vs preço igual, skew por inventário,
flatten no limite e no fim do dia, fee linear, critério PASS."""
import numpy as np

from scripts.backtest_v2 import market_making as mm

SYM = "SOLUSDT"  # tick 0,01, sem notional mínimo acima de $10


def _bins(prices_by_bin):
    """Bins de 100 ms a partir de um dicionário {bin: [preços]}; o resto vazio."""
    n = mm.BINS_PER_DAY
    low = np.full(n, np.inf)
    high = np.full(n, -np.inf)
    last = np.full(n, np.nan)
    cnt = np.zeros(n, np.int32)
    for b, ps in sorted(prices_by_bin.items()):
        low[b], high[b], last[b], cnt[b] = min(ps), max(ps), ps[-1], len(ps)
    idx = np.maximum.accumulate(np.where(np.isnan(last), -1, np.arange(n)))
    last = np.where(idx >= 0, last[np.maximum(idx, 0)], np.nan)
    # fixture sem bounce: último trade no bid = no ask = último trade (mid = last)
    return {"low": low, "high": high, "last": last, "last_bid": last, "last_ask": last, "n": cnt}


def test_bin_day_matches_manual_bins():
    day0 = 1_740_787_200_000  # 2025-03-01 00:00 UTC
    # (preço, ts, is_buyer_maker): 1 = agressor vendedor (trade no bid), 0 = comprador (no ask)
    trades = np.array([[100.0, day0 + 10, 0], [99.0, day0 + 50, 1], [101.0, day0 + 150, 0],
                       [100.5, day0 + 86_399_999, 1]])
    b = mm.bin_day(trades)
    assert b["low"][0] == 99.0 and b["high"][0] == 100.0 and b["last"][0] == 99.0 and b["n"][0] == 2
    assert b["low"][1] == 101.0 and b["n"][1] == 1
    assert b["last"][2] == 101.0  # forward fill
    assert b["last"][-1] == 100.5 and np.isinf(b["low"][2])
    assert b["last_bid"][0] == 99.0 and b["last_ask"][0] == 100.0
    assert b["last_bid"][2] == 99.0 and b["last_ask"][2] == 101.0  # mid no bin 2 = 100,0, sem bounce
    assert b["last_bid"][-1] == 100.5 and b["last_ask"][-1] == 101.0


def test_bounce_does_not_fill_inside_real_spread_and_taker_crosses_it():
    # SNX-like: prints alternam 0,990 (bid) / 1,010 (ask); mid = 1,000; s = 10 bps → bid 0,999, ask 1,001
    n = mm.BINS_PER_DAY
    last_bid = np.full(n, 0.990)
    last_ask = np.full(n, 1.010)
    low = np.full(n, np.inf)
    high = np.full(n, -np.inf)
    cnt = np.zeros(n, np.int32)
    for b in range(0, 200, 10):
        low[b], high[b], cnt[b] = 0.990, 1.010, 2
    bins = {"low": low, "high": high, "last": last_ask, "last_bid": last_bid, "last_ask": last_ask, "n": cnt}
    r = mm.simulate_day("ADAUSDT", bins, 1, 10, 10.0, equal_fills=False)  # tick 0,0001 = 1 bp aqui
    # prints cruzam as duas cotações (0,990 < 0,999 e 1,010 > 1,001) em todo intervalo: compra e vende
    assert r["n_fills"] > 0 and r["n_fills"] % 2 == 0 and r["taker_notional"] == 0.0
    # ... e cada par captura ~2 × 10 bps de $10 (± 1 tick de arredondamento): bruto positivo
    per_pair = r["gross"] / (r["n_fills"] / 2)
    assert 10.0 * 0.0018 <= per_pair <= 10.0 * 0.0023
    # inventário comprado zerado a mercado no fim do dia vende no bid (0,990), não no mid
    low2 = np.full(n, np.inf)
    low2[15] = 0.990
    bins2 = dict(bins, low=low2, high=np.full(n, -np.inf))
    r2 = mm.simulate_day("ADAUSDT", bins2, 1, 10, 10.0, equal_fills=False)
    assert r2["n_fills"] == 1 and r2["n_flatten"] == 0
    assert -10.0 * 0.0102 <= r2["gross"] <= -10.0 * 0.0088  # ≈ 10 × (0,990 − 0,999)


def test_end_of_queue_needs_strict_cross_equal_fills_at_touch():
    # mid 100 em t−200ms; s = 10 bps → bid 99,90, ask 100,10; trade a 99,90 no bin 10 (intervalo 1 s nº 1)
    # depois do toque o preço fica em 99,90 (mid de referência passa a ser 99,90, sem novo cruzamento)
    bins = _bins({0: [100.0], 8: [100.0], 15: [99.90], 25: [99.90]})
    eoq = mm.simulate_day(SYM, bins, 1, 10, 10.0, equal_fills=False)
    eq = mm.simulate_day(SYM, bins, 1, 10, 10.0, equal_fills=True)
    assert eoq["n_fills"] == 0 and eoq["gross"] == 0.0
    assert eq["n_fills"] == 1
    # comprou 0,1 SOL a 99,90 e zerou a 99,90 no fim do dia: bruto 0, só paga fees
    qty = 10.0 / 100.0
    assert abs(eq["gross"]) < 1e-9
    assert abs(eq["maker_notional"] - qty * 99.90) < 1e-9 and abs(eq["taker_notional"] - qty * 99.90) < 1e-9
    assert mm.net_pnl(eq, 0.0002) < 0
    # markout 1 s: preço 1 s depois do fim do intervalo = 99,90 → 0 vs fill; −0,1 × 0,1 = −0,01 vs mid (adverso)
    assert abs(eq["mark_1s"]) < 1e-9 and abs(eq["mark_mid_1s"] + 0.01) < 1e-9


def test_strict_cross_fills_both_sides_and_net_fee_is_linear():
    bins = _bins({0: [100.0], 8: [100.0], 15: [99.80, 100.20], 25: [100.20]})
    r = mm.simulate_day(SYM, bins, 1, 10, 10.0, equal_fills=False)
    assert r["n_fills"] == 2 and r["taker_notional"] == 0.0
    qty = 10.0 / 100.0
    assert abs(r["gross"] - qty * (100.10 - 99.90)) < 1e-9
    assert abs(mm.net_pnl(r, 0.0002) - (r["gross"] - 0.0002 * r["maker_notional"])) < 1e-12
    assert mm.net_pnl(r, 0.0) > mm.net_pnl(r, 0.0002) > mm.net_pnl(r, 0.0005)


def test_inventory_skew_lowers_both_quotes_when_long():
    # 1º intervalo: compra (trade varre 99,8). 2º intervalo: com inventário comprado, ask fica abaixo de 100,10
    bins = _bins({0: [100.0], 8: [100.0], 15: [99.80], 18: [100.0], 25: [100.09], 38: [100.0]})
    r = mm.simulate_day(SYM, bins, 1, 10, 10.0, equal_fills=False)
    # inv = $10 de $50 → ratio 0,2 → r = 100×(1 − 0,2×0,001) = 99,98 → ask = ceil(100,08) = 100,08 < 100,09
    assert r["n_fills"] == 2 and r["taker_notional"] == 0.0


def test_flatten_at_inventory_limit_then_requotes_from_flat():
    # 5 compras seguidas ($10 cada, limite $50) → no requote seguinte zera a mercado (taker)
    marks = {0: [100.0]}
    for i in range(1, 6):
        marks[i * 10 + 5] = [99.0]  # cada intervalo varre bem abaixo do bid
        marks[i * 10 + 8] = [100.0]
    marks[70] = [100.0]
    bins = _bins(marks)
    r = mm.simulate_day(SYM, bins, 1, 10, 10.0, equal_fills=False)
    assert r["n_fills"] == 5 and r["n_flatten"] == 1
    assert r["max_inv"] >= 50.0 - 1e-9
    assert abs(r["taker_notional"] - 5 * 0.1 * 100.0) < 1e-6


def test_min_notional_overrides_small_orders_for_btc():
    bins = _bins({0: [100.0], 8: [100.0], 15: [99.0], 25: [100.0]})
    r = mm.simulate_day("BTCUSDT", bins, 1, 10, 10.0, equal_fills=False)
    assert r["notional_used"] == 50.0


def test_summary_and_verdict():
    ds = {"pnl": np.array([1.0, 2.0, 1.5]), "fills": np.array([10, 10, 10]), "flatten": np.zeros(3),
          "maker_notional": np.array([1000.0, 1000.0, 1000.0]), "mark_1s": np.array([0.1, 0.1, 0.1]),
          "mark_10s": np.zeros(3), "mark_mid_1s": np.zeros(3), "mark_mid_10s": np.zeros(3),
          "max_inv": np.array([10.0, 20.0, 5.0])}
    s = mm.summarize(ds)
    assert s["pnl_total"] == 4.5 and s["max_inv"] == 20.0 and s["max_dd"] == 0.0
    assert abs(s["markout_1s_bps"] - 1.0) < 1e-9 and abs(s["pnl_per_1k_filled"] - 1.5) < 1e-9
    assert s["sharpe_ann"] > 1
    assert mm.verdict({"in_sample": s, "holdout": s})
    neg = dict(s, pnl_total=-1.0)
    assert not mm.verdict({"in_sample": s, "holdout": neg})

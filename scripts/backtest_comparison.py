#!/usr/bin/env python3
"""
Backtest comparativo: Configuração ANTIGA vs NOVA
Baixa dados reais da Binance Futures (sem API key) e simula ambas as configs.

Uso:
    python scripts/backtest_comparison.py
    python scripts/backtest_comparison.py --symbols SOLUSDT XRPUSDT --hours 48
"""

import argparse
import json
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np


# ============================================================
# CONFIGURAÇÕES A COMPARAR
# ============================================================

@dataclass
class BacktestConfig:
    name: str
    pullback_tolerance_pct: float
    rsi_min: float
    rsi_max: float
    volume_ratio: float
    sl_min_pct: float
    sl_max_pct: float
    tp_min_pct: float
    tp_max_pct: float
    rr_target: float
    trailing_activation_pct: float
    trailing_distance_pct: float


OLD = BacktestConfig(
    name="ANTIGA  (live -3.76%)",
    pullback_tolerance_pct=1.00,
    rsi_min=25.0, rsi_max=75.0,
    volume_ratio=0.50,
    sl_min_pct=0.4, sl_max_pct=0.6,
    tp_min_pct=0.8, tp_max_pct=1.2,
    rr_target=2.0,
    trailing_activation_pct=0.50,
    trailing_distance_pct=0.25,
)

NEW = BacktestConfig(
    name="NOVA    (ajustada)",
    pullback_tolerance_pct=0.25,
    rsi_min=38.0, rsi_max=62.0,
    volume_ratio=0.80,
    sl_min_pct=0.5, sl_max_pct=1.5,
    tp_min_pct=1.5, tp_max_pct=4.5,
    rr_target=3.0,
    trailing_activation_pct=1.20,
    trailing_distance_pct=0.50,
)

DEFAULT_SYMBOLS = ["SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "BNBUSDT"]
NOTIONAL = 60.0       # $3 margem × 20x = $60 notional (tier mínimo)
FEE_RATE = 0.0005     # 0.05% taker por lado
MAX_CANDLES_HOLD = 100  # Timeout: fecha após 100 candles (~5h em 3m)


# ============================================================
# FETCH — Binance Futures API pública (sem autenticação)
# ============================================================

def fetch_klines(symbol: str, interval: str, limit: int) -> List[Dict]:
    url = (
        f"https://fapi.binance.com/fapi/v1/klines"
        f"?symbol={symbol}&interval={interval}&limit={limit}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "backtest/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read())
        return [
            {
                "open":   float(k[1]),
                "high":   float(k[2]),
                "low":    float(k[3]),
                "close":  float(k[4]),
                "volume": float(k[5]),
            }
            for k in raw
        ]
    except Exception as exc:
        print(f"    ⚠️  {symbol} {interval}: {exc}")
        return []


# ============================================================
# INDICADORES TÉCNICOS (replicando strategy.py)
# ============================================================

def _ema(prices: List[float], period: int) -> float:
    if not prices:
        return 0.0
    if len(prices) < period:
        return prices[-1]
    mult = 2.0 / (period + 1)
    val = prices[0]
    for p in prices[1:]:
        val = (p - val) * mult + val
    return val


def _rsi(prices: List[float], period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    deltas = np.diff(prices[-(period + 1):])
    gains  = float(np.mean(np.where(deltas > 0, deltas, 0)))
    losses = float(np.mean(np.where(deltas < 0, -deltas, 0)))
    if gains == 0 and losses == 0:
        return 50.0
    if losses == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + gains / losses))


def _atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    if len(highs) < period + 1:
        return (highs[-1] - lows[-1]) if highs else 0.0
    trs = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, len(highs))
    ]
    return float(np.mean(trs[-period:]))


def _vwap(highs, lows, closes, volumes) -> float:
    tp  = (np.array(highs) + np.array(lows) + np.array(closes)) / 3.0
    vol = np.array(volumes)
    total = float(np.sum(vol))
    return float(np.sum(tp * vol) / total) if total > 0 else closes[-1]


# ============================================================
# LÓGICA DE SINAL (replicando HedgeStrategy.analyze_market_pullback)
# ============================================================

def _trend_context(klines: List[Dict]) -> Optional[Dict]:
    if len(klines) < 210:
        return None
    closes  = [k["close"]  for k in klines]
    highs   = [k["high"]   for k in klines]
    lows    = [k["low"]    for k in klines]
    volumes = [k["volume"] for k in klines]
    price = closes[-1]
    if price <= 0:
        return None
    e9   = _ema(closes, 9)
    e21  = _ema(closes, 21)
    e200 = _ema(closes, 200)
    r    = _rsi(closes, 14)
    v    = _vwap(highs, lows, closes, volumes)
    if price > e200 and e9 > e21 and price > v:
        direction = "LONG"
    elif price < e200 and e9 < e21 and price < v:
        direction = "SHORT"
    else:
        direction = "NEUTRAL"
    avg_vol  = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else float(np.mean(volumes))
    curr_vol = volumes[-1]
    return {
        "ema9": e9, "ema21": e21, "rsi": r,
        "direction": direction,
        "avg_volume": avg_vol, "current_volume": curr_vol,
    }


def _bullish_rejection(c: Dict) -> bool:
    o, cl, h, l = c["open"], c["close"], c["high"], c["low"]
    if h <= l or cl <= o:
        return False
    body  = abs(cl - o)
    rng   = max(h - l, 1e-9)
    lo_wk = max(0.0, min(o, cl) - l)
    up_wk = max(0.0, h - max(o, cl))
    return lo_wk >= max(body * 1.2, rng * 0.30) and lo_wk > up_wk * 1.1


def _bearish_rejection(c: Dict) -> bool:
    o, cl, h, l = c["open"], c["close"], c["high"], c["low"]
    if h <= l or cl >= o:
        return False
    body  = abs(cl - o)
    rng   = max(h - l, 1e-9)
    up_wk = max(0.0, h - max(o, cl))
    lo_wk = max(0.0, min(o, cl) - l)
    return up_wk >= max(body * 1.2, rng * 0.30) and up_wk > lo_wk * 1.1


def _bullish_engulfing(p: Dict, c: Dict) -> bool:
    return (p["close"] < p["open"] and c["close"] > c["open"]
            and c["open"] <= p["close"] and c["close"] >= p["open"])


def _bearish_engulfing(p: Dict, c: Dict) -> bool:
    return (p["close"] > p["open"] and c["close"] < c["open"]
            and c["open"] >= p["close"] and c["close"] <= p["open"])


def get_signal(
    exec_klines: List[Dict],
    conf_klines: List[Dict],
    cfg: BacktestConfig,
) -> Optional[str]:
    if len(exec_klines) < 2:
        return None
    ec = _trend_context(exec_klines)
    cc = _trend_context(conf_klines)
    if not ec or not cc:
        return None
    if ec["direction"] == "NEUTRAL" or ec["direction"] != cc["direction"]:
        return None

    prev, curr = exec_klines[-2], exec_klines[-1]
    r   = ec["rsi"]
    vol_ok = ec["avg_volume"] <= 0 or ec["current_volume"] >= ec["avg_volume"] * cfg.volume_ratio
    tol = cfg.pullback_tolerance_pct / 100.0

    if ec["direction"] == "LONG":
        pullback = curr["low"] <= ec["ema9"] * (1 + tol) or curr["low"] <= ec["ema21"] * (1 + tol)
        rsi_ok   = cfg.rsi_min <= r <= cfg.rsi_max
        candle   = _bullish_rejection(curr) or _bullish_engulfing(prev, curr)
        return "LONG" if (pullback and rsi_ok and candle and vol_ok) else None
    else:
        pullback = curr["high"] >= ec["ema9"] * (1 - tol) or curr["high"] >= ec["ema21"] * (1 - tol)
        rsi_ok   = cfg.rsi_min <= r <= cfg.rsi_max
        candle   = _bearish_rejection(curr) or _bearish_engulfing(prev, curr)
        return "SHORT" if (pullback and rsi_ok and candle and vol_ok) else None


# ============================================================
# SIMULAÇÃO DE TRADE
# ============================================================

@dataclass
class Trade:
    direction: str
    entry: float
    exit: float
    pnl_usd: float
    reason: str   # SL | TP | TRAILING | TIMEOUT
    held: int     # candles


def simulate_trade(
    direction: str,
    entry: float,
    futures: List[Dict],
    cfg: BacktestConfig,
    atr_val: float,
) -> Trade:
    # Calcula SL/TP pelo ATR (mesma lógica de calculate_stop_loss_take_profit)
    base_sl = (atr_val * 3.0 / entry * 100.0) if atr_val > 0 and entry > 0 else (cfg.sl_min_pct + cfg.sl_max_pct) / 2
    sl_pct = max(cfg.sl_min_pct, min(base_sl, cfg.sl_max_pct))
    tp_pct = max(cfg.tp_min_pct, min(sl_pct * cfg.rr_target, cfg.tp_max_pct))
    adj_sl = tp_pct / cfg.rr_target
    if cfg.sl_min_pct <= adj_sl <= cfg.sl_max_pct:
        sl_pct = adj_sl

    if direction == "LONG":
        sl_price = entry * (1 - sl_pct / 100)
        tp_price = entry * (1 + tp_pct / 100)
    else:
        sl_price = entry * (1 + sl_pct / 100)
        tp_price = entry * (1 - tp_pct / 100)

    peak          = entry
    trail_active  = False
    trail_stop    = None
    fees          = FEE_RATE * 2

    for i, c in enumerate(futures[:MAX_CANDLES_HOLD]):
        h, l = c["high"], c["low"]

        if direction == "LONG":
            if h > peak:
                peak = h
            if h >= tp_price:
                return Trade(direction, entry, tp_price, (tp_pct / 100 - fees) * NOTIONAL, "TP", i + 1)
            profit_pct = (peak - entry) / entry * 100
            if not trail_active and profit_pct >= cfg.trailing_activation_pct:
                trail_active = True
                trail_stop   = peak * (1 - cfg.trailing_distance_pct / 100)
            if trail_active:
                new_ts = peak * (1 - cfg.trailing_distance_pct / 100)
                if trail_stop is None or new_ts > trail_stop:
                    trail_stop = new_ts
                if l <= trail_stop:
                    pnl = ((trail_stop - entry) / entry - fees) * NOTIONAL
                    return Trade(direction, entry, trail_stop, pnl, "TRAILING", i + 1)
            if l <= sl_price:
                return Trade(direction, entry, sl_price, (-sl_pct / 100 - fees) * NOTIONAL, "SL", i + 1)

        else:  # SHORT
            if l < peak:
                peak = l
            if l <= tp_price:
                return Trade(direction, entry, tp_price, (tp_pct / 100 - fees) * NOTIONAL, "TP", i + 1)
            profit_pct = (entry - peak) / entry * 100
            if not trail_active and profit_pct >= cfg.trailing_activation_pct:
                trail_active = True
                trail_stop   = peak * (1 + cfg.trailing_distance_pct / 100)
            if trail_active:
                new_ts = peak * (1 + cfg.trailing_distance_pct / 100)
                if trail_stop is None or new_ts < trail_stop:
                    trail_stop = new_ts
                if h >= trail_stop:
                    pnl = ((entry - trail_stop) / entry - fees) * NOTIONAL
                    return Trade(direction, entry, trail_stop, pnl, "TRAILING", i + 1)
            if h >= sl_price:
                return Trade(direction, entry, sl_price, (-sl_pct / 100 - fees) * NOTIONAL, "SL", i + 1)

    # Timeout
    last = futures[min(MAX_CANDLES_HOLD - 1, len(futures) - 1)]["close"]
    if direction == "LONG":
        pnl = ((last - entry) / entry - fees) * NOTIONAL
    else:
        pnl = ((entry - last) / entry - fees) * NOTIONAL
    return Trade(direction, entry, last, pnl, "TIMEOUT", min(MAX_CANDLES_HOLD, len(futures)))


# ============================================================
# BACKTEST
# ============================================================

@dataclass
class Stats:
    name: str
    trades: List[Trade] = field(default_factory=list)

    @property
    def n(self):
        return len(self.trades)

    @property
    def wins(self):
        return [t for t in self.trades if t.pnl_usd > 0]

    @property
    def losses(self):
        return [t for t in self.trades if t.pnl_usd <= 0]

    @property
    def wr(self):
        return len(self.wins) / self.n * 100 if self.n else 0.0

    @property
    def total_pnl(self):
        return sum(t.pnl_usd for t in self.trades)

    @property
    def avg_win(self):
        return sum(t.pnl_usd for t in self.wins) / len(self.wins) if self.wins else 0.0

    @property
    def avg_loss(self):
        return sum(t.pnl_usd for t in self.losses) / len(self.losses) if self.losses else 0.0

    @property
    def rr(self):
        return abs(self.avg_win / self.avg_loss) if self.losses and self.avg_loss != 0 else float("inf")

    @property
    def ev(self):
        return self.total_pnl / self.n if self.n else 0.0

    @property
    def min_wr_for_profit(self):
        return 1 / (1 + self.rr) * 100 if self.rr != float("inf") else 0.0

    @property
    def by_reason(self):
        d: Dict[str, int] = {}
        for t in self.trades:
            d[t.reason] = d.get(t.reason, 0) + 1
        return d


def run_backtest(k3: Dict, k5: Dict, cfg: BacktestConfig, symbols: List[str]) -> Stats:
    stats = Stats(name=cfg.name)
    WINDOW = 260

    for sym in symbols:
        klines3 = k3.get(sym, [])
        klines5 = k5.get(sym, [])
        if len(klines3) < WINDOW + 10 or len(klines5) < WINDOW + 10:
            continue

        busy_until = -1
        for i in range(WINDOW, len(klines3) - MAX_CANDLES_HOLD - 1):
            if i <= busy_until:
                continue

            w3 = klines3[i - WINDOW: i + 1]
            # Mapeia índice 3m → 5m (mesmo período histórico, arrays mesma length)
            j  = round(i * len(klines5) / len(klines3))
            j  = max(WINDOW, min(j, len(klines5) - 1))
            w5 = klines5[j - WINDOW: j + 1]

            if len(w5) < WINDOW:
                continue

            sig = get_signal(w3, w5, cfg)
            if sig is None:
                continue

            entry = klines3[i]["close"]
            if entry <= 0:
                continue

            h_hist = [k["high"]  for k in w3[-16:]]
            l_hist = [k["low"]   for k in w3[-16:]]
            c_hist = [k["close"] for k in w3[-16:]]
            atr_val = _atr(h_hist, l_hist, c_hist)

            future = klines3[i + 1: i + MAX_CANDLES_HOLD + 1]
            if not future:
                continue

            t = simulate_trade(sig, entry, future, cfg, atr_val)
            stats.trades.append(t)
            busy_until = i + t.held

    return stats


# ============================================================
# RELATÓRIO
# ============================================================

def _bar(value: float, max_val: float, width: int = 20, char_pos="█", char_neg="░") -> str:
    if max_val == 0:
        return " " * width
    ratio = min(abs(value) / max_val, 1.0)
    filled = round(ratio * width)
    ch = char_pos if value >= 0 else char_neg
    return ch * filled + " " * (width - filled)


def print_report(old: Stats, new: Stats, hours: float):
    W = 60
    sep = "─" * W

    def row(label, old_val, new_val, better_when="higher", fmt="{:.1f}"):
        o_s = fmt.format(old_val)
        n_s = fmt.format(new_val)
        diff = new_val - old_val
        good = (diff > 0) == (better_when == "higher")
        icon = "✅" if good and diff != 0 else ("❌" if not good and diff != 0 else "  ")
        print(f"  {label:<22} {o_s:>9}  →  {n_s:>9}  {icon}")

    print()
    print("=" * W)
    print(f"{'  BACKTEST COMPARATIVO':^{W}}")
    print(f"{'  config ANTIGA vs NOVA':^{W}}")
    print("=" * W)
    print(f"\n  Período simulado: ~{hours:.0f}h  |  Notional por trade: ${NOTIONAL:.0f}")

    # ---- Equity curves simples ----
    print(f"\n{sep}")
    print("  EQUITY CURVE (acumulado)")
    print(sep)
    for cfg_stats, label in [(old, "ANTIGA"), (new, "NOVA  ")]:
        cum = 0.0
        print(f"  {label}: ", end="")
        segments = []
        for t in cfg_stats.trades:
            cum += t.pnl_usd
            segments.append(cum)
        if segments:
            mn, mx = min(segments), max(segments)
            rng = max(abs(mx), abs(mn), 0.01)
            last = segments[-1]
            bar = _bar(last, rng)
            sign = "+" if last >= 0 else ""
            print(f"${sign}{last:.2f}  [{bar}]  (pico ${max(segments):.2f}, vale ${min(segments):.2f})")
        else:
            print("sem trades")

    # ---- Tabela principal ----
    print(f"\n{sep}")
    print("  ESTATÍSTICAS COMPARADAS")
    print(sep)
    print(f"  {'Métrica':<22} {'ANTIGA':>9}     {'NOVA':>9}")
    print(sep)

    row("Trades totais",      old.n,       new.n,       "lower",   "{:.0f}")
    row("Trades/hora",        old.n / hours, new.n / hours, "lower", "{:.2f}")
    row("Win Rate (%)",       old.wr,      new.wr,      "higher",  "{:.1f}")
    row("Avg Win ($)",        old.avg_win, new.avg_win, "higher",  "{:+.3f}")
    row("Avg Loss ($)",       old.avg_loss, new.avg_loss, "higher", "{:+.3f}")
    row("RR Ratio (x)",       old.rr,      new.rr,      "higher",  "{:.2f}")
    row("P&L Total ($)",      old.total_pnl, new.total_pnl, "higher", "{:+.2f}")
    row("EV por trade ($)",   old.ev,      new.ev,      "higher",  "{:+.3f}")

    # ---- Saídas por motivo ----
    print(f"\n{sep}")
    print("  SAÍDAS POR MOTIVO")
    print(sep)
    all_reasons = {"SL", "TP", "TRAILING", "TIMEOUT"}
    print(f"  {'Motivo':<12} {'ANTIGA':>9}     {'NOVA':>9}")
    for r in sorted(all_reasons):
        o_cnt = old.by_reason.get(r, 0)
        n_cnt = new.by_reason.get(r, 0)
        o_pct = o_cnt / old.n * 100 if old.n else 0
        n_pct = n_cnt / new.n * 100 if new.n else 0
        print(f"  {r:<12} {o_cnt:>4} ({o_pct:4.0f}%)     {n_cnt:>4} ({n_pct:4.0f}%)")

    # ---- Veredicto ----
    print(f"\n{sep}")
    print("  VEREDICTO")
    print(sep)
    for s in [old, new]:
        ok = s.wr > s.min_wr_for_profit
        status = "LUCRATIVO" if ok else "NÃO LUCRATIVO"
        icon = "✅" if ok else "❌"
        print(f"  {icon} {s.name}")
        print(f"      WR atual: {s.wr:.1f}%  |  WR mínimo p/ lucro: {s.min_wr_for_profit:.1f}%")
        print(f"      P&L: ${s.total_pnl:+.2f}  |  EV/trade: ${s.ev:+.3f}  →  {status}")

    print(f"\n{'=' * W}")
    print("  NOTA: Backtest usa dados históricos reais (Binance Futures).")
    print("  Resultados não garantem performance futura.")
    print("  Slippage e liquidez não são modelados.")
    print(f"{'=' * W}\n")


# ============================================================
# MAIN
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Backtest comparativo ANTIGA vs NOVA config")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS, help="Pares a testar")
    parser.add_argument("--hours",   type=float, default=25.0, help="Horas de histórico (default: 25h)")
    args = parser.parse_args()

    symbols = [s.upper() for s in args.symbols]
    candles_3m = round(args.hours * 60 / 3)
    candles_5m = round(args.hours * 60 / 5)
    # Garante mínimo para EMA200 + buffer
    candles_3m = max(candles_3m, 300)
    candles_5m = max(candles_5m, 300)

    print(f"\n{'=' * 60}")
    print(f"  BACKTEST — {', '.join(symbols)}")
    print(f"  Período: ~{args.hours:.0f}h  |  3m limit={candles_3m}  |  5m limit={candles_5m}")
    print(f"{'=' * 60}")
    print("\n📥 Baixando dados da Binance Futures (sem API key)...")

    k3: Dict[str, List] = {}
    k5: Dict[str, List] = {}

    for sym in symbols:
        print(f"  {sym:<12}", end="", flush=True)
        t0 = time.time()
        k3_data = fetch_klines(sym, "3m", candles_3m)
        k5_data = fetch_klines(sym, "5m", candles_5m)
        elapsed = time.time() - t0
        if k3_data and k5_data:
            k3[sym] = k3_data
            k5[sym] = k5_data
            print(f"✓  {len(k3_data)} × 3m  |  {len(k5_data)} × 5m  ({elapsed:.1f}s)")
        else:
            print("✗  falhou")
        time.sleep(0.35)  # rate limit gentil

    if not k3:
        print("\n❌  Nenhum dado disponível. Verifique sua conexão.")
        sys.exit(1)

    print(f"\n⚙️  Simulando {len(k3)} símbolo(s)...")
    print("  [ANTIGA]...", end="", flush=True)
    old_stats = run_backtest(k3, k5, OLD, symbols)
    print(f" {old_stats.n} trades")

    print("  [NOVA  ]...", end="", flush=True)
    new_stats = run_backtest(k3, k5, NEW, symbols)
    print(f" {new_stats.n} trades")

    print_report(old_stats, new_stats, args.hours)


if __name__ == "__main__":
    main()

"""Estratégias plugáveis do backtest_v2.

Contrato: uma estratégia produz, por símbolo, uma lista CAUSAL de sinais
(índice da barra de execução FECHADA, lado) + opcionalmente flags de saída por
barra. O motor entra na ABERTURA da barra seguinte. Parâmetros de saída
(SL/RR) vêm do grid e são escolhidos no walk-forward (só no treino).

Para adicionar uma estratégia: subclasse de `Strategy`, implemente
`compute(sym, bars_by_tf)` e registre em `build_registry()`.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from .data import Bars, tf_ms

LONG, SHORT = 1, -1


@dataclass
class SignalSet:
    idx: np.ndarray                 # índices (barra de execução) do sinal, crescente
    side: np.ndarray                # +1 LONG / -1 SHORT
    exit_long: Optional[np.ndarray] = None   # bool por barra: sair do LONG na próxima abertura
    exit_short: Optional[np.ndarray] = None


@dataclass
class Strategy:
    name: str
    exec_tf: str
    tfs: List[str]                       # todos os TFs necessários (inclui exec)
    grid: List[dict]                     # células {sl_pct, rr} ou [{}]
    sizing: str = "risk"                 # "risk" (risco fixo % por SL) | "fraction"
    persistent: bool = False             # sinal = "estado desejado" (reentra quando livre)
    meta: dict = field(default_factory=dict)

    def compute(self, sym: str, bars: Dict[str, Bars], start_ms: int, end_ms: int) -> SignalSet:
        raise NotImplementedError

    def cache_key(self) -> str:
        return hashlib.sha1(json.dumps(self.meta, sort_keys=True, default=str).encode()).hexdigest()[:12]


# ───────────────────────── janela causal multi-TF ─────────────────────────
def aligned_index(exec_close_time: np.ndarray, other: Bars) -> np.ndarray:
    """Para cada barra de execução, o índice da ÚLTIMA barra do outro TF com
    close_time <= close_time da barra de execução (-1 se nenhuma). Sem lookahead:
    uma barra de 1h só fica visível depois que FECHOU."""
    return np.searchsorted(other.close_time, exec_close_time, side="right") - 1


def _rolling_mean(x: np.ndarray, n: int) -> np.ndarray:
    """Média móvel por janela explícita (sem cumsum: janela toda zero → 0 exato,
    como o np.mean do bot)."""
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = np.lib.stride_tricks.sliding_window_view(x, n).mean(axis=1)
    return out


# ───────────────────────── estratégia ao vivo (trend_strong) ─────────────────────────
class LivePullback(Strategy):
    """HedgeStrategy().analyze_market_pullback(exec, confirm, htf) — a função
    REAL do bot, chamada barra a barra com as mesmas janelas do bot ao vivo
    (260 exec/confirm, 255 HTF). Um pré-filtro vetorizado (condições
    NECESSÁRIAS: candle de rejeição/engolfo, volume, RSI) só evita chamar a
    função onde ela com certeza devolveria NEUTRAL; teste de paridade em
    tests/test_backtest_v2_engine.py."""

    def __init__(self, exec_tf: str, confirm_tf: str, htf_tf: str, grid: List[dict]):
        from trading_bot.core.config import config

        self.win = max(220, int(getattr(config, "TREND_STRONG_CANDLES_LOOKBACK", 260)),
                       int(getattr(config, "CANDLES_LOOKBACK", 50)))
        self.htf_win = max(250, int(getattr(config, "TREND_STRONG_HTF_EMA_PERIOD", 200))
                           + int(getattr(config, "TREND_STRONG_HTF_SLOPE_LOOKBACK", 5)) + 50)
        knobs = {k: getattr(config, k) for k in dir(config) if k.startswith("TREND_STRONG_")}
        super().__init__(
            name=f"live_pullback_{exec_tf}_{confirm_tf}_{htf_tf}",
            exec_tf=exec_tf, tfs=[exec_tf, confirm_tf, htf_tf], grid=grid, sizing="risk",
            meta={"kind": "live_pullback", "tfs": [exec_tf, confirm_tf, htf_tf], "win": self.win,
                  "htf_win": self.htf_win, "knobs": knobs, "v": 2},
        )
        self.confirm_tf, self.htf_tf = confirm_tf, htf_tf
        self.knobs = knobs

    # ---- pré-filtro (superconjunto das condições exigidas pela função real) ----
    def prefilter(self, b: Bars) -> np.ndarray:
        o, h, lw, c, v = b.open, b.high, b.low, b.close, b.volume
        n = len(c)
        body = np.abs(c - o)
        rng = np.maximum(h - lw, 1e-9)
        lower = np.maximum(0.0, np.minimum(o, c) - lw)
        upper = np.maximum(0.0, h - np.maximum(o, c))
        valid = h > lw
        bull_rej = valid & (c > o) & (lower >= np.maximum(body * 1.2, rng * 0.30)) & (lower > upper * 1.1)
        bear_rej = valid & (c < o) & (upper >= np.maximum(body * 1.2, rng * 0.30)) & (upper > lower * 1.1)
        po, pc = np.roll(o, 1), np.roll(c, 1)
        bull_eng = (pc < po) & (c > o) & (o <= pc) & (c >= po)
        bear_eng = (pc > po) & (c < o) & (o >= pc) & (c <= po)
        bull_eng[0] = bear_eng[0] = False
        candle = bull_rej | bear_rej | bull_eng | bear_eng
        ratio = max(0.1, float(self.knobs.get("TREND_STRONG_MIN_VOLUME_RATIO", 0.9)))
        avg20 = _rolling_mean(v, 20)
        vol_ok = ~(v < avg20 * ratio * (1 - 1e-9))     # NaN (início) passa
        # RSI simples (média de 14 deltas) — mesma fórmula do bot; faixa = união LONG∪SHORT
        d = np.diff(c, prepend=c[0])
        g = _rolling_mean(np.where(d > 0, d, 0.0), 14)
        ls = _rolling_mean(np.where(d < 0, -d, 0.0), 14)
        with np.errstate(divide="ignore", invalid="ignore"):
            rsi = np.where(ls == 0, np.where(g == 0, 50.0, 100.0), 100 - 100 / (1 + g / ls))
        lo = min(self.knobs["TREND_STRONG_LONG_RSI_MIN"], self.knobs["TREND_STRONG_SHORT_RSI_MIN"]) - 0.5
        hi = max(self.knobs["TREND_STRONG_LONG_RSI_MAX"], self.knobs["TREND_STRONG_SHORT_RSI_MAX"]) + 0.5
        rsi_ok = ~((rsi < lo) | (rsi > hi))
        out = candle & vol_ok & rsi_ok
        out[:1] = False
        return out if n else np.zeros(0, bool)

    @staticmethod
    def _dicts(b: Bars) -> List[dict]:
        return [{"open": o, "high": h, "low": lw, "close": c, "volume": v}
                for o, h, lw, c, v in zip(b.open.tolist(), b.high.tolist(), b.low.tolist(),
                                         b.close.tolist(), b.volume.tolist())]

    def compute(self, sym, bars, start_ms, end_ms, use_prefilter: bool = True) -> SignalSet:
        from trading_bot.core.strategy import HedgeStrategy, Signal

        ex, cf, ht = bars[self.exec_tf], bars[self.confirm_tf], bars[self.htf_tf]
        ect = ex.close_time
        ci = aligned_index(ect, cf)
        hi = aligned_index(ect, ht)
        in_range = (ex.open_time >= start_ms) & (ex.open_time < end_ms)
        full = (np.arange(len(ex)) >= self.win - 1) & (ci >= self.win - 1) & (hi >= self.htf_win - 1)
        cand = in_range & full
        if use_prefilter:
            cand &= self.prefilter(ex)
        idxs = np.nonzero(cand)[0]
        S = HedgeStrategy()
        exd, cfd, htd = self._dicts(ex), self._dicts(cf), self._dicts(ht)
        W, HW = self.win, self.htf_win
        out_i, out_s = [], []
        for i in idxs.tolist():
            c_i, h_i = int(ci[i]), int(hi[i])
            sig = S.analyze_market_pullback(exd[i - W + 1:i + 1], cfd[c_i - W + 1:c_i + 1],
                                            htd[h_i - HW + 1:h_i + 1])
            if sig == Signal.STRONG_BUY:
                out_i.append(i)
                out_s.append(LONG)
            elif sig == Signal.STRONG_SELL:
                out_i.append(i)
                out_s.append(SHORT)
        return SignalSet(np.array(out_i, dtype=np.int64), np.array(out_s, dtype=np.int8))


# ───────────────────────── trend diário EMA ─────────────────────────
def ema_full(x: np.ndarray, period: int) -> np.ndarray:
    """EMA recursiva sobre todo o histórico disponível (semente = 1º valor)."""
    a = 2.0 / (period + 1)
    out = np.empty(len(x))
    e = x[0] if len(x) else 0.0
    for k, p in enumerate(x):
        e = (p - e) * a + e
        out[k] = e
    return out


class DailyEMATrend(Strategy):
    """Cruzamento EMA rápida/lenta no diário. Estado desejado: LONG se
    EMA50>EMA200, SHORT se < (só no modo long/short). Sem SL/TP: sai quando o
    estado muda (na abertura do dia seguinte). Sizing: fração fixa da banca por
    posição (1/N do universo, 1x), sem alavancagem."""

    def __init__(self, long_short: bool, fast: int = 50, slow: int = 200, n_universe: int = 35):
        super().__init__(
            name=f"daily_ema{fast}_{slow}_{'long_short' if long_short else 'long_only'}",
            exec_tf="1d", tfs=["1d"], grid=[{}], sizing="fraction", persistent=True,
            meta={"kind": "daily_ema", "fast": fast, "slow": slow, "ls": long_short, "v": 1},
        )
        self.long_short, self.fast, self.slow = long_short, fast, slow
        self.fraction = 1.0 / max(1, n_universe)

    def compute(self, sym, bars, start_ms, end_ms) -> SignalSet:
        b = bars["1d"]
        n = len(b)
        if n == 0:
            return SignalSet(np.zeros(0, np.int64), np.zeros(0, np.int8), np.zeros(0, bool), np.zeros(0, bool))
        f, s = ema_full(b.close, self.fast), ema_full(b.close, self.slow)
        ready = np.arange(n) >= self.slow - 1          # EMA lenta só vale após `slow` barras
        up = ready & (f > s)
        dn = ready & (f < s)
        in_range = (b.open_time >= start_ms) & (b.open_time < end_ms)
        want_long = up & in_range
        want_short = (dn & in_range) if self.long_short else np.zeros(n, bool)
        idx = np.nonzero(want_long | want_short)[0]
        side = np.where(want_long[idx], LONG, SHORT).astype(np.int8)
        return SignalSet(idx.astype(np.int64), side, exit_long=~up, exit_short=~dn)


# ───────────────────────── registro ─────────────────────────
# Grid pequeno (6 células) de SL% × RR por TF de execução. Faixa do 3m cobre a
# banda ao vivo (1,0–1,5%); TFs maiores escalam o stop com a volatilidade típica.
SL_GRID = {"3m": [0.6, 1.0, 1.5], "15m": [1.0, 1.5, 2.5], "1h": [1.5, 2.5, 4.0]}
RR_GRID = [1.5, 2.5]


def _grid(exec_tf: str) -> List[dict]:
    return [{"sl_pct": sl, "rr": rr} for sl in SL_GRID[exec_tf] for rr in RR_GRID]


def build_registry(n_universe: int) -> Dict[str, Strategy]:
    strats: List[Strategy] = [
        LivePullback("3m", "5m", "1h", _grid("3m")),
        LivePullback("15m", "1h", "4h", _grid("15m")),
        LivePullback("1h", "4h", "1d", _grid("1h")),
        DailyEMATrend(long_short=False, n_universe=n_universe),
        DailyEMATrend(long_short=True, n_universe=n_universe),
    ]
    return {s.name: s for s in strats}


def tf_minutes(tf: str) -> float:
    return tf_ms(tf) / 60_000

"""Motor do backtest_v2: simulação de trade (SL/TP intrabar, fees, slippage,
funding) e portfólio (sizing por risco, teto de posições, curva de equity).

Convenções (todas conservadoras):
- Sinal na barra i FECHADA → entrada na ABERTURA de i+1 (o sinal só existe após
  o close; o bot ao vivo envia market logo depois) + `entry_slip` contra nós.
- SL/TP medidos a partir do preço de entrada efetivo.
- Na barra em que SL e TP são ambos tocados, assume SL (ordem intrabar
  desconhecida). Gap através do SL na abertura → preenche na abertura.
- Stop paga `stop_slip` extra (STOP_MARKET escorrega); TP, saída por flag e
  fechamento no fim pagam `exit_slip`.
- Fee taker em ambas as pernas, sobre o notional de cada perna.
- Funding: para cada funding_time estritamente dentro de (entrada, saída),
  LONG paga notional×rate (recebe se rate<0); SHORT o oposto.
- Posição aberta no fim da janela é fechada no último close (não descartada).
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np

from .data import Bars
from .strategies import LONG, SignalSet


@dataclass(frozen=True)
class Costs:
    fee_rate: float = 0.0005      # taker por lado
    entry_slip: float = 0.0003
    exit_slip: float = 0.0003
    stop_slip: float = 0.0010     # extra do STOP_MARKET (além do exit normal)


@dataclass
class Trade:
    symbol: str
    side: int
    signal_idx: int
    entry_idx: int
    entry_time: int
    entry_price: float
    sl: Optional[float]
    tp: Optional[float]
    exit_idx: int
    exit_time: int
    exit_price: float
    reason: str
    gross_frac: float
    fee_frac: float
    funding_frac: float
    params: dict = field(default_factory=dict)
    notional: float = 0.0
    pnl: float = 0.0
    equity_at_entry: float = 0.0

    @property
    def net_frac(self) -> float:
        return self.gross_frac - self.fee_frac + self.funding_frac


# ───────────────────────────── trade individual ─────────────────────────────
def _first_true(mask: np.ndarray) -> int:
    k = int(np.argmax(mask)) if len(mask) else 0
    return k if len(mask) and mask[k] else -1


def simulate_trade(bars: Bars, sig_idx: int, side: int, params: dict, costs: Costs,
                   end_idx: int, funding: Optional[np.ndarray] = None,
                   exit_flags: Optional[np.ndarray] = None, symbol: str = "") -> Optional[Trade]:
    """Simula 1 trade. `end_idx` = 1º índice FORA da janela (não olha além dele)."""
    e = sig_idx + 1
    if e >= end_idx:
        return None
    o, h, lw, c = bars.open, bars.high, bars.low, bars.close
    entry = o[e] * (1 + costs.entry_slip * side)
    sl_pct = params.get("sl_pct")
    rr = params.get("rr")
    sl = tp = None
    if sl_pct:
        sl = entry * (1 - side * sl_pct / 100.0)
        if rr:
            tp = entry * (1 + side * sl_pct * rr / 100.0)

    exit_idx, exit_px, reason = -1, 0.0, ""
    j0 = e
    chunk = 256
    while j0 < end_idx and exit_idx < 0:
        j1 = min(end_idx, j0 + chunk)
        hh, ll = h[j0:j1], lw[j0:j1]
        k_sl = k_tp = k_fl = -1
        if sl is not None:
            k_sl = _first_true(ll <= sl if side == LONG else hh >= sl)
        if tp is not None:
            k_tp = _first_true(hh >= tp if side == LONG else ll <= tp)
        if exit_flags is not None:
            # flag na barra f (fechada) → sai na abertura de f+1; flag em f>=e
            k_fl = _first_true(exit_flags[j0:j1])
        cands = []
        if k_sl >= 0:
            cands.append((j0 + k_sl, 1, "sl"))      # intrabar de j
        if k_tp >= 0:
            cands.append((j0 + k_tp, 2, "tp"))      # mesma barra: SL vence (prioridade 1)
        if k_fl >= 0 and j0 + k_fl + 1 < end_idx:
            cands.append((j0 + k_fl + 1, 0, "signal"))  # abertura de f+1 vem antes do intrabar
        if cands:
            j, _, why = min(cands)
            exit_idx, reason = j, why
            if why == "sl":
                gap = (o[j] <= sl) if side == LONG else (o[j] >= sl)
                base = o[j] if (gap and j > e) else sl
                exit_px = base * (1 - side * (costs.exit_slip + costs.stop_slip))
            elif why == "tp":
                exit_px = tp * (1 - side * costs.exit_slip)
            else:
                exit_px = o[j] * (1 - side * costs.exit_slip)
        j0 = j1
        chunk *= 4

    if exit_idx < 0:  # fim da janela: fecha no último close disponível
        exit_idx = end_idx - 1
        exit_px = c[exit_idx] * (1 - side * costs.exit_slip)
        reason = "eod"

    entry_time = int(bars.open_time[e])
    exit_time = int(bars.open_time[exit_idx]) if reason == "signal" else int(bars.close_time[exit_idx])
    ratio = exit_px / entry
    gross = (ratio - 1.0) * side
    fee = costs.fee_rate * (1.0 + ratio)
    fund = 0.0
    if funding is not None and len(funding):
        ft = funding[:, 0]
        a = int(np.searchsorted(ft, entry_time, "right"))
        b = int(np.searchsorted(ft, exit_time, "left"))
        if b > a:
            rates = funding[a:b, 1]
            mp = funding[a:b, 2]
            # preço no funding: markPrice se houver; senão abertura da barra que contém o instante
            pos = np.clip(np.searchsorted(bars.open_time, ft[a:b], "right") - 1, 0, len(o) - 1)
            px = np.where(np.isfinite(mp) & (mp > 0), mp, o[pos])
            fund = float(np.sum(-side * rates * px / entry))
    return Trade(symbol, side, sig_idx, e, entry_time, float(entry), sl, tp, exit_idx, exit_time,
                 float(exit_px), reason, float(gross), float(fee), fund, dict(params))


# ───────────────────────────── portfólio ─────────────────────────────
@dataclass
class PortfolioConfig:
    initial_equity: float = 1000.0
    sizing: str = "risk"             # "risk" | "fraction"
    risk_pct: float = 1.0            # % da banca arriscada até o SL
    fraction: float = 0.1            # p/ sizing "fraction": notional = equity × fraction
    max_pos_mult: float = 1.0        # teto do notional por posição (× equity) — trava de alavancagem
    max_gross_mult: float = 3.0      # teto da soma dos notionals abertos (× equity)
    max_concurrent: int = 6


@dataclass
class SymbolData:
    bars: Bars
    signals: SignalSet
    funding: Optional[np.ndarray]


@dataclass
class PortfolioResult:
    trades: List[Trade]
    equity_curve: np.ndarray          # (n,2): [time_ms, equity MTM]
    final_equity: float
    initial_equity: float
    max_dd: float
    skipped_capacity: int


def _param_for(time_ms: int, schedule: Sequence[tuple]) -> Optional[dict]:
    """schedule = [(t_ini, t_fim, params)]; devolve params cuja janela contém time_ms."""
    for t0, t1, p in schedule:
        if t0 <= time_ms < t1:
            return p
    return None


def run_portfolio(sym_data: Dict[str, SymbolData], schedule: Sequence[tuple], t_start: int, t_end: int,
                  costs: Costs, pcfg: PortfolioConfig, mtm_step_ms: int = 86_400_000,
                  with_curve: bool = True) -> PortfolioResult:
    """Portfólio event-driven. Sinais com barra de sinal fechando em [t_start, t_end)
    (e cujo período tenha params no `schedule`) viram candidatos em ordem de
    tempo de entrada (empate: símbolo). Trades são simulados sob demanda e
    truncados em t_end (nada além da janela é olhado)."""
    cands = []
    end_idx = {}
    for sym, sd in sym_data.items():
        b = sd.bars
        end_idx[sym] = int(np.searchsorted(b.open_time, t_end, "left"))
        if not len(sd.signals.idx):
            continue
        ct = b.close_time[sd.signals.idx]
        ok = (ct >= t_start) & (ct < t_end) & (sd.signals.idx + 1 < end_idx[sym])
        for i, s in zip(sd.signals.idx[ok].tolist(), sd.signals.side[ok].tolist()):
            cands.append((int(b.open_time[i + 1]), sym, i, s))
    cands.sort(key=lambda x: (x[0], x[1], x[2]))

    equity = pcfg.initial_equity
    open_heap: list = []         # (exit_time, seq, trade)
    busy_until: Dict[str, int] = {}
    gross_open = 0.0
    trades: List[Trade] = []
    skipped = 0
    seq = 0

    def _realize_until(t):
        nonlocal equity, gross_open
        while open_heap and open_heap[0][0] <= t:
            _, _, tr = heapq.heappop(open_heap)
            equity += tr.pnl
            gross_open -= tr.notional

    for entry_time, sym, i, side in cands:
        if busy_until.get(sym, -1) > entry_time:
            continue
        _realize_until(entry_time)
        if busy_until.get(sym, -1) > entry_time:
            continue
        params = _param_for(int(sym_data[sym].bars.close_time[i]), schedule)
        if params is None:
            continue
        if len(open_heap) >= pcfg.max_concurrent:
            skipped += 1
            continue
        if equity <= 0:
            break
        sd = sym_data[sym]
        flags = None
        if sd.signals.exit_long is not None:
            flags = sd.signals.exit_long if side == LONG else sd.signals.exit_short
        tr = simulate_trade(sd.bars, i, side, params, costs, end_idx[sym], sd.funding, flags, sym)
        if tr is None:
            continue
        if pcfg.sizing == "risk" and params.get("sl_pct"):
            notional = equity * (pcfg.risk_pct / 100.0) / (params["sl_pct"] / 100.0)
        else:
            notional = equity * pcfg.fraction
        notional = min(notional, equity * pcfg.max_pos_mult)
        room = equity * pcfg.max_gross_mult - gross_open
        if room <= 0:
            skipped += 1
            continue
        notional = min(notional, room)
        tr.notional = notional
        tr.pnl = notional * tr.net_frac
        tr.equity_at_entry = equity
        busy_until[sym] = tr.exit_time
        gross_open += notional
        heapq.heappush(open_heap, (tr.exit_time, seq, tr))
        seq += 1
        trades.append(tr)
    _realize_until(float("inf"))

    if not with_curve:
        return PortfolioResult(trades, np.zeros((0, 2)), equity, pcfg.initial_equity, float("nan"), skipped)
    curve = equity_curve(trades, sym_data, t_start, t_end, pcfg.initial_equity, mtm_step_ms)
    peak = np.maximum.accumulate(curve[:, 1]) if len(curve) else np.array([pcfg.initial_equity])
    dd = float(np.max((peak - curve[:, 1]) / peak)) if len(curve) else 0.0
    return PortfolioResult(trades, curve, equity, pcfg.initial_equity, dd, skipped)


def equity_curve(trades: List[Trade], sym_data: Dict[str, SymbolData], t_start: int, t_end: int,
                 initial: float, step_ms: int) -> np.ndarray:
    """Equity marcada a mercado numa grade (diária por padrão) + pontos de saída.
    Não-realizado usa o close da última barra de execução fechada ≤ t."""
    grid = np.arange(t_start - t_start % step_ms + step_ms, t_end + 1, step_ms, dtype=np.int64)
    exits = np.array(sorted({t.exit_time for t in trades}), dtype=np.int64)
    times = np.unique(np.concatenate([grid, exits, np.array([t_start, t_end], np.int64)]))
    realized = np.full(len(times), initial, dtype=float)
    if trades:
        et = np.array([t.exit_time for t in trades])
        pnl = np.array([t.pnl for t in trades])
        order = np.argsort(et, kind="stable")
        cum = np.cumsum(pnl[order])
        k = np.searchsorted(et[order], times, "right")
        realized += np.where(k > 0, cum[np.maximum(k - 1, 0)], 0.0)
    unreal = np.zeros(len(times))
    for t in trades:
        b = sym_data[t.symbol].bars
        m = (times > t.entry_time) & (times < t.exit_time)
        if not m.any():
            continue
        idx = np.searchsorted(b.close_time, times[m], "right") - 1
        idx = np.clip(idx, t.entry_idx, len(b) - 1)
        px = b.close[idx]
        unreal[m] += t.notional * ((px / t.entry_price - 1.0) * t.side - _entry_fee_frac(t))
    return np.column_stack([times, realized + unreal])


def _entry_fee_frac(t: Trade) -> float:
    """Fee de entrada já paga enquanto a posição está aberta (fração do notional)."""
    return t.fee_frac / 2.0

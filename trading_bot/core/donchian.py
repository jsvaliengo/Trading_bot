"""Donchian 4h: forward test pré-registrado (docs/FORWARD_TEST_DONCHIAN_4H.md).

As regras são as da pesquisa (scripts/backtest_v2/trend4h.py, PR #215): sinal no
close da barra 4h FECHADA, stop k×ATR(Wilder)/close aplicado ao fill, saída pelo
canal oposto, sizing por risco e universo point-in-time por volume da mainnet. As
funções puras abaixo são portes 1:1 e têm teste de paridade com a pesquisa.

`DonchianRunner` liga isso ao bot: roda no tick do monitor, processa cada barra
4h uma vez e substitui todo o caminho do pullback (análise 3m/5m, IA, trailing,
regime, scoring de pares).
"""
from __future__ import annotations

import logging
import re
import time
from collections import Counter
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

import numpy as np

from ..execution.engine import _fill_price, _round_qty_for_min_notional
from ..infra.binance_streams import stale_kline_age_seconds
from .config import config

if TYPE_CHECKING:
    from .bot import TradingBot

logger = logging.getLogger(__name__)

STRATEGY_TYPE = "donchian_4h"
H4_MS = 4 * 3_600_000
DAY_MS = 86_400_000
WEEK_MS = 7 * DAY_MS
KLINES_LIMIT = 200          # ATR de Wilder converge: (19/20)^180 ≈ 1e-4 do seed
SETTLE_MS = 10_000          # espera a barra fechada aparecer na REST
ENTRY_MAX_DELAY_MS = 30 * 60_000   # entrada só "logo após o close", não em restart horas depois
STABLE_BASES = ("USDC", "BUSD", "TUSD", "USDP", "FDUSD", "DAI", "USDE", "PYUSD", "EUR", "GBP", "AEUR")
_SYM_RE = re.compile(r"^[A-Z0-9]{2,30}$")


def is_donchian(strategy: Optional[str] = None) -> bool:
    return (strategy if strategy is not None else config.STRATEGY) == STRATEGY_TYPE


def is_candidate(sym: str) -> bool:
    """USDT perp (sem trimestral `_`), base que não é stablecoin/fiat."""
    return bool(_SYM_RE.match(sym)) and sym.endswith("USDT") and sym[:-4] not in STABLE_BASES


# ───────────────────────── indicadores (portes da pesquisa) ─────────────────────────
def prior_extreme(x: np.ndarray, n: int, fn) -> np.ndarray:
    """fn das n barras ANTERIORES a cada índice (DonchianBreakout._prior)."""
    out = np.full(len(x), np.nan)
    if len(x) > n:
        out[n:] = fn(np.lib.stride_tricks.sliding_window_view(x, n)[:-1], axis=1)
    return out


def atr_wilder(h: np.ndarray, lw: np.ndarray, c: np.ndarray, n: int) -> np.ndarray:
    tr = np.maximum(h - lw, np.maximum(np.abs(h - np.roll(c, 1)), np.abs(lw - np.roll(c, 1))))
    if len(tr):
        tr[0] = h[0] - lw[0]
    out = np.full(len(tr), np.nan)
    if len(tr) < n:
        return out
    a = tr[:n].mean()
    out[n - 1] = a
    for i in range(n, len(tr)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def donchian_signals(h: np.ndarray, lw: np.ndarray, c: np.ndarray, n: int, m: int):
    """(entry_long, entry_short, exit_long, exit_short) por barra fechada."""
    with np.errstate(invalid="ignore"):
        return (c > prior_extreme(h, n, np.max), c < prior_extreme(lw, n, np.min),
                c < prior_extreme(lw, m, np.min), c > prior_extreme(h, m, np.max))


@dataclass(frozen=True)
class BarSignal:
    entry: Optional[str]        # "LONG" | "SHORT" | None
    exit_long: bool
    exit_short: bool
    stop_pct: Optional[float]   # k × ATR / close, fração do preço


def last_bar_signal(klines: List[Dict[str, Any]], *, n: int, m: int, atr_n: int, k: float) -> BarSignal:
    """Sinal da ÚLTIMA barra fechada. Entrada exige ATR válido (como na pesquisa)."""
    h = np.array([float(x["high"]) for x in klines])
    lw = np.array([float(x["low"]) for x in klines])
    c = np.array([float(x["close"]) for x in klines])
    if not len(c):
        return BarSignal(None, False, False, None)
    el, es, xl, xs = (bool(a[-1]) for a in donchian_signals(h, lw, c, n, m))
    atr = atr_wilder(h, lw, c, atr_n)[-1]
    stop_pct = float(k * atr / c[-1]) if np.isfinite(atr) and c[-1] > 0 else None
    entry = None
    if stop_pct:
        entry = "LONG" if el else "SHORT" if es else None
    return BarSignal(entry, xl, xs, stop_pct)


# ───────────────────────── universo point-in-time ─────────────────────────
def rebalance_start(ms: int) -> int:
    """Segunda 00:00 UTC ≤ ms (epoch foi quinta: segunda = +4 dias)."""
    return ms - ((ms - 4 * DAY_MS) % WEEK_MS)


def universe_score(daily: List[List[Any]], t: int, *, min_days: int, vol_days: int) -> Optional[float]:
    """Quote volume dos `vol_days` dias antes de t, só barras diárias com close < t.
    None se inelegível: < min_days barras fechadas ou a última não é a de ontem."""
    closed = [row for row in daily if int(row[0]) + DAY_MS - 1 < t]
    if len(closed) < min_days or int(closed[-1][0]) != t - DAY_MS:
        return None
    return float(sum(float(row[7]) for row in closed if int(row[0]) >= t - vol_days * DAY_MS))


def top_universe(scores: Dict[str, float], top_n: int) -> List[str]:
    """Top-N por score (top_n <= 0 = todos); empate desempata pelo símbolo (argsort estável)."""
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [s for s, _ in (ranked[:top_n] if top_n > 0 else ranked)]


# ───────────────────────── sizing ─────────────────────────
@dataclass(frozen=True)
class Sizing:
    qty: float
    notional: float
    risk_usd: float
    skip: Optional[str] = None


def size_position(*, equity: float, stop_pct: float, price: float, info: Dict[str, Any], open_positions: int,
                  gross_open: float, risk_pct: float, max_pos_mult: float, max_gross_mult: float,
                  max_positions: int) -> Sizing:
    """Notional = equity × risco / distância do stop, com os tetos da pesquisa. O risco e os
    tetos são conferidos na qty FINAL (step do símbolo + minNotional, como a ordem sai):
    se passar de 2× o risco alvo, pula."""
    if open_positions >= max_positions:
        return Sizing(0.0, 0.0, 0.0, f"teto de {max_positions} posições")
    if equity <= 0 or stop_pct <= 0 or price <= 0:
        return Sizing(0.0, 0.0, 0.0, "equity, stop ou preço inválido")
    target_risk = equity * risk_pct / 100.0
    room = equity * max_gross_mult - gross_open
    if room <= 0:
        return Sizing(0.0, 0.0, 0.0, f"teto bruto {max_gross_mult:g}x")
    cap = min(equity * max_pos_mult, room)
    qty = _round_qty_for_min_notional(min(target_risk / stop_pct, cap) / price, price, info)
    notional = qty * price
    risk = notional * stop_pct
    if risk > 2 * target_risk:
        return Sizing(0.0, 0.0, 0.0, (
            f"minNotional/step: risco ${risk:.2f} = {risk / target_risk:.2f}× alvo ${target_risk:.2f} (qty {qty:g})"
        ))
    if notional > cap + 1e-9:
        return Sizing(0.0, 0.0, 0.0, f"qty mínima ${notional:.2f} não cabe nos tetos")
    return Sizing(qty, notional, risk)


# ───────────────────────── runner ao vivo ─────────────────────────
class DonchianRunner:
    """Uma passada por barra 4h fechada: saídas primeiro, depois entradas em ordem
    alfabética (a ordem do motor da pesquisa). Idempotente: o que já está aberto na
    exchange não abre de novo, e uma barra só é processada uma vez por símbolo."""

    def __init__(self, bot: "TradingBot"):
        self.bot = bot
        self.universe: List[str] = []
        self.universe_week: Optional[int] = None
        self._boundary: Optional[int] = None
        self._pending: Set[str] = set()
        self._retry_universe_at = 0
        self._bar: Dict[str, Any] = {}
        self._bar_logged_at: Optional[float] = None   # epoch s da última barra processada
        # Níveis do canal de saída da última barra processada por símbolo (só leitura,
        # para o dashboard): {sym: {"exit_long": min low m, "exit_short": max high m}}.
        self.exit_levels: Dict[str, Dict[str, float]] = {}

    def snapshot(self) -> Dict[str, Any]:
        """Estado do motor para o dashboard (JSON-safe). Não toca na exchange."""
        b = self._bar or {}
        boundary = self._boundary

        def iso(ms: int) -> str:
            return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()

        # Só contagens: a lista de ~450 pares e os níveis de saída não vão no JSON
        # (o dashboard lê exit_levels direto do runner para as posições abertas).
        return {
            "universe_size": len(self.universe),
            "universe_week": iso(self.universe_week) if self.universe_week else None,
            "bar_close": iso(boundary) if boundary else None,
            "next_bar_close": iso(boundary + H4_MS) if boundary else None,
            "last_bar_processed_at": iso(int(self._bar_logged_at * 1000)) if self._bar_logged_at else None,
            "pending_count": len(self._pending),
            "counters": {k: v for k, v in b.items() if k not in ("skips", "logged")},
            "skips": dict(b.get("skips") or {}),
            "params": {
                "entry_bars": config.DONCHIAN_ENTRY_BARS, "exit_bars": config.DONCHIAN_EXIT_BARS,
                "atr_bars": config.DONCHIAN_ATR_BARS, "stop_atr_mult": config.DONCHIAN_STOP_ATR_MULT,
                "risk_percent": config.DONCHIAN_RISK_PERCENT, "max_positions": config.DONCHIAN_MAX_POSITIONS,
                "max_gross_mult": config.DONCHIAN_MAX_GROSS_MULT,
                "max_drawdown_percent": config.DONCHIAN_MAX_DRAWDOWN_PERCENT,
            },
        }

    # ---- startup / universo ----
    def start(self) -> None:
        self.log_startup()
        self.restore_tags()
        self.refresh_universe()

    def restore_tags(self) -> None:
        """Posição Donchian sem a etiqueta no state (state perdido) recupera do SQLite.
        Sem isso o monitor a trataria como pullback (TP global/trailing)."""
        store = getattr(self.bot, "trade_store", None)
        with self.bot._positions_lock:
            for key, meta in self.bot.known_positions.items():
                if store is not None and meta.get("strategy_type") != STRATEGY_TYPE and store.open_strategy_type(
                    meta.get("symbol"), meta.get("side")
                ) == STRATEGY_TYPE:
                    meta["strategy_type"] = meta["strategy_name"] = STRATEGY_TYPE
                    logger.info(f"🏷️ Donchian: {key} reetiquetada pelo SQLite")
            other = [k for k, m in self.bot.known_positions.items() if m.get("strategy_type") != STRATEGY_TYPE]
        if other:
            logger.warning(
                f"⚠️ Donchian: {len(other)} posição(ões) de outra estratégia abertas ({', '.join(other)}). "
                "Seguem com as regras antigas (TP/trailing/SL) até fechar e contam nos tetos."
            )

    def log_startup(self) -> None:
        c = config
        logger.info(
            f"🧭 Estratégia: Donchian 4h (n={c.DONCHIAN_ENTRY_BARS},m={c.DONCHIAN_EXIT_BARS},"
            f"k={c.DONCHIAN_STOP_ATR_MULT:g},risco {c.DONCHIAN_RISK_PERCENT:g}%) — forward test".replace(".", ",")
        )
        logger.info(
            f"🧭 Donchian: tetos {c.DONCHIAN_MAX_POSITIONS} posições, {c.DONCHIAN_MAX_POSITION_MULT:g}x "
            f"por posição, {c.DONCHIAN_MAX_GROSS_MULT:g}x bruto; universo "
            f"{f'top {c.DONCHIAN_UNIVERSE_TOP_N}' if c.DONCHIAN_UNIVERSE_TOP_N > 0 else 'completo (sem teto)'} "
            f"por volume 30d da mainnet ∩ testnet; sinais da {'mainnet' if c.SIGNALS_FROM_MAINNET else c.ENVIRONMENT}"
        )
        logger.info(
            f"🛡️ Donchian: parada por drawdown em {c.DONCHIAN_MAX_DRAWDOWN_PERCENT:g}% do pico "
            f"(pausa o bot); limite de perda diária ${c.DAILY_LOSS_LIMIT:g} mantido"
        )
        logger.info(
            "🛡️ Donchian: DESLIGADOS pausa por dias negativos, piso de win rate, meta de lucro "
            "diário, stop loss global, trailing, regime swap, viés HTF, IA e scoring de pares"
        )

    def refresh_universe(self, now_ms: Optional[int] = None) -> List[str]:
        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        t = rebalance_start(now_ms)
        ex = self.bot.exchange
        started = time.monotonic()
        mainnet = [
            s["symbol"] for s in (ex.get_market_exchange_info().get("symbols") or [])
            if s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING"
            and s.get("quoteAsset") == "USDT" and is_candidate(s.get("symbol", ""))
        ]
        if not mainnet:
            logger.warning("⚠️ Donchian: exchangeInfo da mainnet vazio — universo mantido")
            return self.universe
        # +8: a semana pode estar no meio (restart no sábado ainda usa a segunda T).
        # ~500 símbolos: em paralelo o refresh cai de ~150s para ~20s de loop bloqueado.
        with ThreadPoolExecutor(max_workers=8) as pool:
            daily = dict(zip(mainnet, pool.map(
                lambda sym: ex.get_klines_raw(sym, "1d", limit=config.DONCHIAN_UNIVERSE_MIN_DAYS + 8), mainnet,
            )))
        failed = [sym for sym, rows in daily.items() if not rows]
        partial = len(failed) > 0.1 * len(mainnet)
        if partial:
            self._retry_universe_at = now_ms + 15 * 60_000
            logger.warning(f"⚠️ Donchian: {len(failed)}/{len(mainnet)} klines diárias falharam — "
                           f"{'universo anterior mantido' if self.universe else 'universo parcial'}, "
                           "nova tentativa em 15min")
            if self.universe:
                return self.universe
        scores: Dict[str, float] = {}
        for sym, rows in daily.items():
            score = universe_score(rows, t, min_days=config.DONCHIAN_UNIVERSE_MIN_DAYS,
                                   vol_days=config.DONCHIAN_UNIVERSE_VOLUME_DAYS)
            if score is not None:
                scores[sym] = score
        top = top_universe(scores, config.DONCHIAN_UNIVERSE_TOP_N)
        tradable = {
            s["symbol"] for s in (ex.get_exchange_info().get("symbols") or [])
            if s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING"
        }
        disabled = set(config.DISABLED_PAIRS or [])
        missing = [s for s in top if s not in tradable]
        off = [s for s in top if s in tradable and s in disabled]
        self.universe = [s for s in top if s in tradable and s not in disabled]
        self.universe_week = None if partial else t
        logger.info(
            f"🌐 Donchian: universo da semana {time.strftime('%Y-%m-%d', time.gmtime(t / 1000))} "
            f"= {len(self.universe)} pares [{', '.join(self.universe)}] (top {len(top)} da mainnet; fora do testnet: "
            f"{len(missing)} {missing}; desabilitados: {off}; klines com falha: {len(failed)}) "
            f"em {time.monotonic() - started:.0f}s"
        )
        return self.universe

    # ---- loop ----
    def tick(self, now_ms: Optional[int] = None) -> None:
        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        boundary = (now_ms - SETTLE_MS) // H4_MS * H4_MS
        if boundary != self._boundary:
            self._boundary = boundary
            self._pending = set(self.universe) | set(self._donchian_positions())
            self._bar = {"total": len(self._pending), "ok": 0, "stale": 0, "LONG": 0, "SHORT": 0, "exits": 0,
                         "entries": 0, "skips": Counter(), "deferred": 0, "defer_reason": "", "open": None,
                         "logged": False}
        if self._pending:
            self._process(boundary, now_ms)
        bar_done = not self._pending or now_ms - boundary > ENTRY_MAX_DELAY_MS   # 1 par travado não segura o rebalance
        if bar_done and not self._bar["logged"]:
            self._log_bar(boundary)
        if bar_done and rebalance_start(now_ms) != self.universe_week and now_ms >= self._retry_universe_at:
            self.refresh_universe(now_ms)

    def _donchian_positions(self) -> Dict[str, Dict[str, Any]]:
        with self.bot._positions_lock:
            return {
                str(p.get("symbol")): dict(p) for p in self.bot.known_positions.values()
                if isinstance(p, dict) and p.get("strategy_type") == STRATEGY_TYPE
            }

    def _fetch(self, symbol: str, boundary: int):
        """(status, klines): ok = última barra fechada é a esperada; stale = feed parado
        (> 2 intervalos, regra do PR #212); wait = ainda não chegou ou erro de API."""
        kl = self.bot.exchange.get_klines(symbol=symbol, interval="4h", limit=KLINES_LIMIT)
        if not kl:
            return "wait", kl
        last = int(kl[-1]["timestamp"])
        if last == boundary - H4_MS:
            return "ok", kl
        if stale_kline_age_seconds(last, "4h") is not None:
            return "stale", kl
        return "wait", kl

    def _process(self, boundary: int, now_ms: int) -> None:
        ex = self.bot.exchange
        try:
            positions = ex.get_open_positions(force_refresh=True)
        except Exception as exc:
            logger.warning(f"⚠️ Donchian: posições indisponíveis ({exc}) — barra adiada")
            self._bar["deferred"] += 1
            self._bar["defer_reason"] = f"posições indisponíveis ({exc})"
            return
        open_by_sym = {p["symbol"]: p for p in positions}
        tagged = self._donchian_positions()
        c = config
        ready: Dict[str, BarSignal] = {}
        for sym in sorted(self._pending):
            status, kl = self._fetch(sym, boundary)
            if status == "wait":
                continue
            pos = open_by_sym.get(sym) if sym in tagged else None
            if status == "stale":
                if pos:
                    logger.warning(f"⚠️ Donchian: {sym} sem barra 4h nova (dados parados) — saindo a mercado")
                    if not self._close(pos, "Donchian: dados parados"):
                        continue          # fica pendente: tenta de novo no próximo tick
                    open_by_sym.pop(sym, None)
                    self._bar["exits"] += 1
                self._bar["stale"] += 1
                self._pending.discard(sym)
                continue
            sig = last_bar_signal(kl, n=c.DONCHIAN_ENTRY_BARS, m=c.DONCHIAN_EXIT_BARS,
                                  atr_n=c.DONCHIAN_ATR_BARS, k=c.DONCHIAN_STOP_ATR_MULT)
            tail = kl[-c.DONCHIAN_EXIT_BARS:]
            self.exit_levels[sym] = {"exit_long": min(float(x["low"]) for x in tail),
                                     "exit_short": max(float(x["high"]) for x in tail)}
            if pos and (sig.exit_long if pos["side"] == "LONG" else sig.exit_short):
                if not self._close(pos, f"Donchian: saída no canal de {c.DONCHIAN_EXIT_BARS}"):
                    continue              # fica pendente: tenta de novo no próximo tick
                open_by_sym.pop(sym, None)
                self._bar["exits"] += 1
            self._pending.discard(sym)
            ready[sym] = sig
            self._bar["ok"] += 1
            if sig.entry and sym in self.universe:
                self._bar[sig.entry] += 1
        self._bar["open"] = len(open_by_sym)

        store = getattr(self.bot, "trade_store", None)
        bar_close = datetime.fromtimestamp(boundary / 1000, tz=timezone.utc).isoformat()
        entries = [(s, sig) for s, sig in sorted(ready.items())
                   if sig.entry and s in self.universe and s not in open_by_sym
                   # restart dentro da janela: a barra já gerou entrada (talvez já stopada)
                   and not (store is not None and (store.last_opened_at(s, STRATEGY_TYPE) or "") >= bar_close)]
        if not entries:
            return
        late = now_ms - boundary > ENTRY_MAX_DELAY_MS
        blocked = (
            "barra fechou há mais de 30min" if late
            else "bot pausado" if self.bot.paused
            else "limite diário atingido" if self.bot.check_daily_targets()
            else None
        )
        if blocked:
            logger.info(f"⏸️ Donchian: {len(entries)} sinal(is) sem entrada ({blocked}): "
                        f"{', '.join(f'{s} {sig.entry}' for s, sig in entries)}")
            self._bar["skips"][blocked] += len(entries)
            return
        equity = float(self.bot.drawdown_equity(ex.get_account_balance()))
        gross = sum(float(p["quantity"]) * float(p.get("mark_price") or p["entry_price"])
                    for p in open_by_sym.values())
        count = len(open_by_sym)
        for sym, sig in entries:
            info = ex.get_symbol_info(sym)
            try:
                price = float(ex.get_symbol_ticker(sym).get("price") or 0)
            except (TypeError, ValueError):
                price = 0.0
            sizing = size_position(
                equity=equity, stop_pct=sig.stop_pct, price=price, info=info, open_positions=count,
                gross_open=gross, risk_pct=c.DONCHIAN_RISK_PERCENT,
                max_pos_mult=c.DONCHIAN_MAX_POSITION_MULT, max_gross_mult=c.DONCHIAN_MAX_GROSS_MULT,
                max_positions=c.DONCHIAN_MAX_POSITIONS,
            )
            if sizing.skip:
                logger.warning(f"⏭️ Donchian: {sym} {sig.entry} pulado — {sizing.skip} "
                               f"(equity ${equity:.2f}, stop {sig.stop_pct * 100:.2f}%)")
                self._bar["skips"][sizing.skip] += 1
                continue
            notional = self._open(sym, str(sig.entry), sig.stop_pct, sizing, info, price)
            if notional:
                count += 1
                gross += notional
                self._bar["entries"] += 1
            else:
                self._bar["skips"]["falha na abertura"] += 1
        self._bar["open"] = count

    def _log_bar(self, boundary: int) -> None:
        """Uma linha por barra 4h: barra quieta e barra não processada não se confundem no log."""
        b = self._bar
        b["logged"] = True
        self._bar_logged_at = time.time()
        when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(boundary / 1000))
        if not b["total"]:
            logger.info(f"🕓 Donchian: barra {when} UTC não processada — universo vazio e sem posições")
            return
        if b["deferred"] and not b["ok"] and not b["stale"]:
            logger.warning(f"🕓 Donchian: barra {when} UTC NÃO processada — adiada {b['deferred']}x, "
                           f"{b['defer_reason']}; {len(self._pending)} pares pendentes")
            return
        cap = config.DONCHIAN_MAX_POSITIONS
        held = b["open"] if b["open"] is not None else len(self.bot.known_positions)
        eq, peak = getattr(self.bot, "last_drawdown_equity", None), float(getattr(self.bot, "peak_equity", 0) or 0)
        equity = "equity n/d" if eq is None else (
            f"equity ${eq:.2f} (DD {max(0.0, (peak - eq) / peak * 100) if peak > 0 else 0.0:.1f}% do pico)")
        skips = sum(b["skips"].values())
        why = f" ({'; '.join(f'{r} ×{n}' for r, n in b['skips'].items())})" if skips else ""
        defer = f"adiada {b['deferred']}x antes ({b['defer_reason']}); " if b["deferred"] else ""
        logger.info(
            f"🕓 Donchian: barra {when} UTC — {defer}{b['ok']}/{b['total']} pares analisados "
            f"({len(self._pending)} sem barra, {b['stale']} dados parados), "
            f"{b['LONG'] + b['SHORT']} rompimentos ({b['LONG']}L/{b['SHORT']}S), {b['exits']} saídas, "
            f"{b['entries']} entradas, {skips} pulados{why}; posições abertas {held}/{cap}; {equity}"
        )

    # ---- execução ----
    def _close(self, pos: Dict[str, Any], reason: str) -> bool:
        return self.bot.close_position(pos, reason)

    def _open(self, symbol: str, side: str, stop_pct: float, sizing: Sizing, info: Dict[str, Any],
              price: float) -> float:
        stop: Optional[float]
        bot, ex = self.bot, self.bot.exchange
        qty = sizing.qty
        ex.cancel_open_orders(symbol)
        ex.set_leverage(symbol, config.LEVERAGE)
        logger.info(f"🚀 Donchian: abrindo {side} {symbol} qty {qty} (~${qty * price:.2f}, "
                    f"risco ~${sizing.risk_usd:.2f}, stop {stop_pct * 100:.2f}%)")
        order = ex.place_market_order(symbol=symbol, side="BUY" if side == "LONG" else "SELL",
                                      position_side=side, quantity=qty)
        if order:
            fill = _fill_price(order, price, f"Abertura Donchian {side} {symbol}")
        else:
            # -1007 (timeout) devolve None mesmo com a ordem executada: confere na exchange.
            live = [p for p in ex.get_open_positions(force_refresh=True)
                    if p["symbol"] == symbol and p["side"] == side]
            if not live:
                logger.error(f"❌ Donchian: ordem de abertura {side} {symbol} falhou")
                return 0.0
            fill, qty = float(live[0]["entry_price"]), float(live[0]["quantity"])
            logger.warning(f"⚠️ Donchian: abertura {side} {symbol} sem resposta, mas a posição existe — seguindo")
        stop = round(fill * (1 - stop_pct) if side == "LONG" else fill * (1 + stop_pct),
                     int(info.get("pricePrecision", 4)))
        if not ex.set_stop_loss_take_profit(symbol=symbol, position_side=side, stop_loss_price=stop,
                                            take_profit_price=None):
            if ex.close_position(symbol, side):
                logger.error(f"❌ Donchian: stop não aceito em {symbol} — posição fechada a mercado")
                return 0.0
            # Sem stop e sem fechar: registra como Donchian (o monitor não aplica regras do
            # pullback) e avisa; a saída pelo canal segue valendo.
            logger.error(f"🚨 Donchian: {side} {symbol} SEM STOP na exchange e o fechamento falhou")
            bot.telegram.send_message(
                f"🚨 <b>DONCHIAN SEM STOP</b>\n{side} {symbol}: stop recusado e fechamento falhou. "
                "Verifique na exchange."
            )
            stop = None
        bot.ledger.record_trade_opened(
            symbol=symbol, signal=f"DONCHIAN_{side}", side=side, quantity=qty,
            order_size=qty * fill / max(float(config.LEVERAGE), 1.0), entry_price=fill,
            stop_loss=stop, take_profit=None, strategy_name=STRATEGY_TYPE, strategy_type=STRATEGY_TYPE,
        )
        bot.positions.open(symbol=symbol, side=side, entry_price=fill, quantity=qty,
                           strategy_name=STRATEGY_TYPE, strategy_type=STRATEGY_TYPE, custom_stop_loss=stop)
        bot.telegram.send_trade_alert(symbol=symbol, action=f"OPEN_{side}", price=fill, quantity=qty,
                                      strategy_name=STRATEGY_TYPE)
        logger.info(f"✅ Donchian: {side} {symbol} @ {fill} | stop {stop} | saída no canal de "
                    f"{config.DONCHIAN_EXIT_BARS}")
        return qty * fill

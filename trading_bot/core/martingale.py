"""Martingale/DCA com hedge estilo CoinTech2u (docs/MARTINGALE_HEDGE.md).

Regras da pesquisa pré-registrada (scripts/backtest_v2/MARTINGALE_PREREG.md, variante V3,
a única que não liquidou a conta): por par, duas pernas independentes (LONG e SHORT) em
hedge mode. Cada perna abre a ordem base B a mercado; quando o preço anda X% contra o
ÚLTIMO fill, adiciona B×m^k a mercado; fecha a perna inteira em média×(1±TP) e reabre na
base. A partir do nível N−1 não adiciona mais. Stop de portfólio OBRIGATÓRIO: equity ≤
pico×(1−STOP) fecha tudo e PAUSA o bot (humano retoma; a simulação reiniciava sozinha e
mesmo assim foi a zero em 44–73 stops).

`MartingaleRunner` liga isso ao bot no tick do monitor e substitui todo o caminho do
pullback (análise, IA, TP/SL/trailing por ordem, kill switches, rotação de pares).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from ..execution.engine import _fill_price, _round_qty_for_min_notional
from .config import config

if TYPE_CHECKING:
    from .bot import TradingBot

logger = logging.getLogger(__name__)

STRATEGY_TYPE = "martingale_hedge"
SIDES = ("LONG", "SHORT")


# ───────────────────────── matemática da escada (puras, testadas) ─────────────────────────
def ladder_sum(m: float, n: int, double_first: bool = False) -> float:
    """S = Σ_{k=0}^{n−1} m^k: notional da escada completa em múltiplos de B.
    `double_first` (CoinTech2u "Double First Order"): a 1ª ordem vale 2B, as demais seguem."""
    n = max(int(n), 0)
    return float(sum(m ** k for k in range(n))) + (1.0 if double_first and n > 0 else 0.0)


def base_notional(*, capital: float, leverage: float, n_pairs: int, m: float, n: int,
                  double_first: bool = False) -> float:
    """B = capital × alavancagem × 0,5 / (n_pares × 2 × S): todas as escadas cheias ao
    mesmo tempo usam 50% do capital como margem (sizing do pré-registro)."""
    if capital <= 0 or n_pairs <= 0 or n <= 0:
        return 0.0
    return capital * leverage * 0.5 / (n_pairs * 2 * ladder_sum(m, n, double_first))


def fixed_base_notional(first_order_usdt: float, leverage: float, is_notional: bool) -> float:
    """Perfil planilha: B fixo a partir da "1ª ordem" em USDT (margem × alavancagem, ou o
    próprio valor quando a planilha é lida como notional)."""
    return float(first_order_usdt) * (1.0 if is_notional else float(leverage))


def level_notional(base: float, m: float, k: int, double_first: bool = False) -> float:
    return base * (2.0 if double_first and k == 0 else m ** k)


def add_fill(avg: float, qty: float, price: float, add_qty: float) -> Tuple[float, float]:
    """Novo (preço médio, quantidade) depois de um fill a `price` de `add_qty`."""
    total = qty + add_qty
    if total <= 0:
        return avg, qty
    return (avg * qty + price * add_qty) / total, total


def next_trigger(last_fill: float, side: str, step_pct: float) -> float:
    """Preço que dispara a próxima adição: X% contra o último fill."""
    x = step_pct / 100.0
    return last_fill * (1 - x) if side == "LONG" else last_fill * (1 + x)


def add_triggered(price: float, side: str, trigger: float) -> bool:
    return price <= trigger if side == "LONG" else price >= trigger


def tp_price(avg: float, side: str, tp_pct: float) -> float:
    t = tp_pct / 100.0
    return avg * (1 + t) if side == "LONG" else avg * (1 - t)


def tp_hit(price: float, side: str, target: float) -> bool:
    return price >= target if side == "LONG" else price <= target


def infer_level(notional: float, base: float, m: float, n: int, double_first: bool = False) -> int:
    """Fills já feitos numa perna encontrada na exchange sem estado (restart): menor
    k com B×S_k ≥ 90% do notional (tolera arredondamento de step/minNotional)."""
    if base <= 0:
        return 1
    for k in range(1, max(int(n), 1) + 1):
        if base * ladder_sum(m, k, double_first) >= 0.9 * notional:
            return k
    return int(n)


def portfolio_stop_floor(peak: float, stop_pct: float, stop_usdt: float = 0.0) -> float:
    """Equity que dispara o stop: pico − STOP_LOSS_USDT quando setado (planilha), senão pico × (1 − %)."""
    return peak - stop_usdt if stop_usdt > 0 else peak * (1 - stop_pct / 100.0)


def portfolio_stop_hit(equity: float, peak: float, stop_pct: float, stop_usdt: float = 0.0) -> bool:
    return peak > 0 and equity <= portfolio_stop_floor(peak, stop_pct, stop_usdt)


def _pct_away(px: Optional[float], mark: Optional[float]) -> Optional[float]:
    """Distância % do mark até `px` (negativa = abaixo do preço atual)."""
    return (px - mark) / mark * 100.0 if (px and mark) else None


@dataclass
class Leg:
    symbol: str
    side: str
    level: int = 0            # fills já feitos (0 = sem posição)
    last_fill: float = 0.0


@dataclass
class PairPlan:
    symbol: str
    info: Dict[str, Any]
    legs: Dict[str, Leg] = field(default_factory=dict)


# ───────────────────────── runner ao vivo ─────────────────────────
class MartingaleRunner:
    """Uma passada por tick do monitor: stop de portfólio primeiro, depois por par e
    perna: TP → adição → abertura da base. Idempotente: posição existente na exchange
    não abre de novo; estado de perna perdido (restart) é inferido do notional."""

    def __init__(self, bot: "TradingBot"):
        self.bot = bot
        self.enabled = False
        self.base: float = 0.0
        self.capital: float = 0.0
        self.peak: float = 0.0
        self.pairs: Dict[str, PairPlan] = {}
        self.stops = 0

    def snapshot(self) -> Dict[str, Any]:
        c = config
        return {
            "enabled": self.enabled, "base_notional": self.base, "capital": self.capital,
            "peak_equity": self.peak, "portfolio_stops": self.stops,
            "pairs": {s: {side: {"level": leg.level, "last_fill": leg.last_fill} for side, leg in p.legs.items()}
                      for s, p in self.pairs.items()},
            "params": {
                "step_percent": c.MARTINGALE_STEP_PERCENT, "multiplier": c.MARTINGALE_MULTIPLIER,
                "tp_percent": c.MARTINGALE_TP_PERCENT, "max_levels": c.MARTINGALE_MAX_LEVELS,
                "portfolio_stop_percent": c.MARTINGALE_PORTFOLIO_STOP_PERCENT, "leverage": c.MARTINGALE_LEVERAGE,
                "first_order_usdt": c.MARTINGALE_FIRST_ORDER_USDT, "double_first": c.MARTINGALE_DOUBLE_FIRST,
                "stop_loss_usdt": c.MARTINGALE_STOP_LOSS_USDT,
            },
        }

    def ladder(self, positions: List[Dict[str, Any]], equity: Optional[float] = None) -> Dict[str, Any]:
        """snapshot() + escada por perna pro dashboard: nível, média, próxima adição e TP
        (com % até o mark), stop de portfólio em $ e folga em %. `positions` no formato do
        dashboard (symbol, side, entry_price, quantity, mark_price, unrealized_pnl_usd)."""
        c = config
        out = self.snapshot()
        stop_eq = portfolio_stop_floor(self.peak, c.MARTINGALE_PORTFOLIO_STOP_PERCENT, c.MARTINGALE_STOP_LOSS_USDT)
        out["portfolio_stop_equity"] = stop_eq
        out["portfolio_stop_room_pct"] = (equity - stop_eq) / equity * 100.0 if equity else None
        by_key = {f"{p.get('symbol')}_{p.get('side')}": p for p in positions}
        legs = []
        for sym, plan in self.pairs.items():
            for side in SIDES:
                leg, pos = plan.legs[side], by_key.get(f"{sym}_{side}")
                row: Dict[str, Any] = {"symbol": sym, "side": side, "level": leg.level, "max_levels": c.MARTINGALE_MAX_LEVELS,
                                       "avg_price": None, "quantity": None, "notional": None, "mark_price": None,
                                       "unrealized_pnl_usd": None, "next_add_price": None, "next_add_pct": None,
                                       "tp_price": None, "tp_pct": None}
                if pos is not None:
                    avg, qty = float(pos.get("entry_price") or 0), float(pos.get("quantity") or 0)
                    mark = float(pos.get("mark_price") or 0) or None
                    level = leg.level or infer_level(avg * qty, self.base, c.MARTINGALE_MULTIPLIER, c.MARTINGALE_MAX_LEVELS,
                                                     c.MARTINGALE_DOUBLE_FIRST)
                    tp = tp_price(avg, side, c.MARTINGALE_TP_PERCENT) if avg else None
                    nxt = (next_trigger(leg.last_fill or avg, side, c.MARTINGALE_STEP_PERCENT)
                           if avg and level < c.MARTINGALE_MAX_LEVELS else None)
                    row.update(level=level, avg_price=avg or None, quantity=qty or None,
                               notional=qty * (mark or avg) if qty else None, mark_price=mark,
                               unrealized_pnl_usd=pos.get("unrealized_pnl_usd"),
                               next_add_price=nxt, next_add_pct=_pct_away(nxt, mark), tp_price=tp, tp_pct=_pct_away(tp, mark))
                legs.append(row)
        out["legs"] = legs
        return out

    # ---- startup ----
    def start(self) -> bool:
        c = config
        ex = self.bot.exchange
        logger.warning(
            f"🎰 Estratégia: martingale/DCA hedge (X={c.MARTINGALE_STEP_PERCENT:g}%, m={c.MARTINGALE_MULTIPLIER:g}, "
            f"TP={c.MARTINGALE_TP_PERCENT:g}%, N={c.MARTINGALE_MAX_LEVELS}, stop de portfólio "
            f"{c.MARTINGALE_PORTFOLIO_STOP_PERCENT:g}%, {c.MARTINGALE_LEVERAGE}x) — pesquisa: FAIL 0/360 células, "
            "só testnet (docs/MARTINGALE_HEDGE.md)"
        )
        logger.info("🛡️ Martingale: DESLIGADOS TP/SL/trailing por ordem, IA, kill switches, meta diária, "
                    "stop global, regime, rotação de pares. Única proteção: stop de portfólio.")
        # Binance recusa trocar o modo com posição aberta: set_hedge_mode devolve False
        # e a estratégia não inicia (LONG e SHORT no mesmo par exigem dual-side).
        if not ex.set_hedge_mode():
            self._refuse("Hedge mode (dual-side) indisponível — feche as posições abertas e reinicie")
            return False
        self.capital = float(ex.get_account_balance())
        if self.peak <= 0:
            self.peak = float(self.bot.drawdown_equity(self.capital))
        symbols = list(c.MARTINGALE_PAIRS or [])
        fixed = c.MARTINGALE_FIRST_ORDER_USDT > 0
        if fixed:
            self.base = fixed_base_notional(c.MARTINGALE_FIRST_ORDER_USDT, c.MARTINGALE_LEVERAGE,
                                            c.MARTINGALE_FIRST_ORDER_IS_NOTIONAL)
            logger.info(f"🎰 Martingale: sizing fixo (planilha) — 1ª ordem ${c.MARTINGALE_FIRST_ORDER_USDT:g} "
                        f"{'notional' if c.MARTINGALE_FIRST_ORDER_IS_NOTIONAL else f'de margem × {c.MARTINGALE_LEVERAGE}x'}"
                        f" = B ${self.base:.2f}{' (1ª ordem dobrada)' if c.MARTINGALE_DOUBLE_FIRST else ''}")
        else:
            self.base = base_notional(capital=self.capital, leverage=c.MARTINGALE_LEVERAGE, n_pairs=len(symbols),
                                      m=c.MARTINGALE_MULTIPLIER, n=c.MARTINGALE_MAX_LEVELS,
                                      double_first=c.MARTINGALE_DOUBLE_FIRST)
        if c.MARTINGALE_STOP_LOSS_USDT > 0.25 * self.capital:
            logger.warning(f"⚠️ Martingale: STOP_LOSS_USDT ${c.MARTINGALE_STOP_LOSS_USDT:g} é "
                           f"{c.MARTINGALE_STOP_LOSS_USDT / max(self.capital, 1e-9) * 100:.0f}% da banca "
                           f"${self.capital:.2f} (acima dos 25% do clamp do stop em %)")
        for sym in symbols:
            info = ex.get_symbol_info(sym) or {}
            min_notional = float(info.get("minNotional", 0) or 0)
            first_fill = level_notional(self.base, c.MARTINGALE_MULTIPLIER, 0, c.MARTINGALE_DOUBLE_FIRST)
            if fixed and first_fill < min_notional:
                # planilha: não pula o par, sobe a base até a 1ª ordem cobrir o minNotional
                self.base = min_notional / (2.0 if c.MARTINGALE_DOUBLE_FIRST else 1.0)
                logger.warning(f"⚠️ Martingale: {sym} 1ª ordem ${first_fill:.2f} < minNotional ${min_notional:.2f} — "
                               f"base subida para ${self.base:.2f} (vale para todos os pares)")
            elif first_fill < min_notional:
                logger.warning(f"⏭️ Martingale: {sym} pulado — ordem base ${self.base:.2f} < minNotional "
                               f"${min_notional:.2f} (capital ${self.capital:.2f})")
                continue
            ex.set_leverage(sym, c.MARTINGALE_LEVERAGE)
            self.pairs[sym] = PairPlan(sym, info, {side: Leg(sym, side) for side in SIDES})
        if not self.pairs:
            self._refuse(f"nenhum par cabe na ordem base ${self.base:.2f} com capital ${self.capital:.2f}")
            return False
        self.enabled = True
        logger.info(f"🎰 Martingale: capital ${self.capital:.2f}, ordem base ${self.base:.2f}, "
                    f"pares {list(self.pairs)}, pico ${self.peak:.2f}")
        return True

    def _refuse(self, why: str) -> None:
        self.enabled = False
        self.bot.paused = True
        logger.error(f"❌ Martingale NÃO iniciada: {why}. Bot pausado.")
        self.bot.telegram.send_message(f"❌ <b>MARTINGALE NÃO INICIADA</b>\n{why}\nBot pausado.")

    # ---- loop ----
    def tick(self) -> None:
        if not self.enabled:
            return
        bot, ex, c = self.bot, self.bot.exchange, config
        try:
            positions = ex.get_open_positions(force_refresh=True)
            equity = float(bot.drawdown_equity(ex.get_account_balance()))
        except Exception as exc:
            logger.warning(f"⚠️ Martingale: exchange indisponível ({exc}) — tick adiado")
            return
        self.peak = max(self.peak, equity)
        if portfolio_stop_hit(equity, self.peak, c.MARTINGALE_PORTFOLIO_STOP_PERCENT, c.MARTINGALE_STOP_LOSS_USDT):
            self._portfolio_stop(positions, equity)
            return
        by_key = {f"{p['symbol']}_{p['side']}": p for p in positions}
        for sym, plan in self.pairs.items():
            try:
                price = float(ex.get_symbol_ticker(sym).get("price") or 0)
            except Exception as exc:
                logger.warning(f"⚠️ Martingale: preço de {sym} indisponível ({exc})")
                continue
            if price <= 0:
                continue
            for side in SIDES:
                self._leg_tick(plan, plan.legs[side], by_key.get(f"{sym}_{side}"), price)

    def _leg_tick(self, plan: PairPlan, leg: Leg, pos: Optional[Dict[str, Any]], price: float) -> None:
        c = config
        if pos is None:
            leg.level, leg.last_fill = 0, 0.0
            if not self.bot.paused:
                self._open(plan, leg, price, k=0)
            return
        avg, qty = float(pos["entry_price"]), float(pos["quantity"])
        if leg.level == 0:          # restart: posição existe, estado da perna perdido
            leg.level = infer_level(avg * qty, self.base, c.MARTINGALE_MULTIPLIER, c.MARTINGALE_MAX_LEVELS,
                                    c.MARTINGALE_DOUBLE_FIRST)
            leg.last_fill = price
            logger.info(f"🔁 Martingale: {leg.side} {plan.symbol} recuperada da exchange (nível {leg.level}, "
                        f"média {avg}, qty {qty})")
        if tp_hit(price, leg.side, tp_price(avg, leg.side, c.MARTINGALE_TP_PERCENT)):
            if self.bot.close_position(pos, f"Martingale TP nível {leg.level}"):
                leg.level, leg.last_fill = 0, 0.0
            return
        if (not self.bot.paused and leg.level < c.MARTINGALE_MAX_LEVELS
                and add_triggered(price, leg.side, next_trigger(leg.last_fill, leg.side, c.MARTINGALE_STEP_PERCENT))):
            self._open(plan, leg, price, k=leg.level)

    def _portfolio_stop(self, positions: List[Dict[str, Any]], equity: float) -> None:
        c = config
        self.stops += 1
        floor = portfolio_stop_floor(self.peak, c.MARTINGALE_PORTFOLIO_STOP_PERCENT, c.MARTINGALE_STOP_LOSS_USDT)
        logger.error(f"🛑 Martingale: STOP DE PORTFÓLIO — equity ${equity:.2f} ≤ piso ${floor:.2f} "
                     f"(pico ${self.peak:.2f}). Fechando {len(positions)} posição(ões) e pausando o bot.")
        failed = [p for p in positions if not self.bot.close_position(p, "Stop de portfólio")]
        for plan in self.pairs.values():
            for leg in plan.legs.values():
                leg.level, leg.last_fill = 0, 0.0
        self.bot.paused = True
        self.peak = equity       # próximo ciclo (se um humano retomar) mede do zero
        self.bot.telegram.send_message(
            f"🛑 <b>MARTINGALE — STOP DE PORTFÓLIO</b>\nEquity ${equity:.2f} caiu ${self.peak - equity:.2f} "
            f"do pico ${self.peak:.2f}. {len(positions) - len(failed)} posição(ões) fechadas a mercado"
            + (f", {len(failed)} FALHARAM ({', '.join(p['symbol'] + ' ' + p['side'] for p in failed)}) — verifique na exchange"
               if failed else "")
            + ".\nBot PAUSADO: retome com /resume só depois de decidir se continua."
        )

    # ---- execução ----
    def _open(self, plan: PairPlan, leg: Leg, price: float, *, k: int) -> bool:
        """Fill do nível k (0 = base) a mercado. Atualiza o estado da perna; a média e a
        quantidade vivem na exchange (hedge mode agrega os fills da mesma perna)."""
        bot, ex, c = self.bot, self.bot.exchange, config
        sym, side = plan.symbol, leg.side
        notional = level_notional(self.base, c.MARTINGALE_MULTIPLIER, k, c.MARTINGALE_DOUBLE_FIRST)
        qty = _round_qty_for_min_notional(notional / price, price, plan.info)
        if qty <= 0:
            return False
        what = "abrindo base" if k == 0 else f"adicionando nível {k}"
        logger.info(f"🎰 Martingale: {what} {side} {sym} qty {qty} (~${qty * price:.2f}) @ ~{price}")
        order = ex.place_market_order(symbol=sym, side="BUY" if side == "LONG" else "SELL",
                                      position_side=side, quantity=qty)
        if not order:
            logger.error(f"❌ Martingale: ordem {what} {side} {sym} falhou")
            return False
        fill = _fill_price(order, price, f"Martingale {what} {side} {sym}")
        leg.level, leg.last_fill = k + 1, fill
        if k == 0:
            bot.ledger.record_trade_opened(
                symbol=sym, signal=f"MARTINGALE_{side}", side=side, quantity=qty,
                order_size=qty * fill / max(float(c.MARTINGALE_LEVERAGE), 1.0), entry_price=fill,
                stop_loss=None, take_profit=tp_price(fill, side, c.MARTINGALE_TP_PERCENT),
                strategy_name=STRATEGY_TYPE, strategy_type=STRATEGY_TYPE,
            )
            bot.positions.open(symbol=sym, side=side, entry_price=fill, quantity=qty,
                               strategy_name=STRATEGY_TYPE, strategy_type=STRATEGY_TYPE)
            bot.telegram.send_trade_alert(symbol=sym, action=f"OPEN_{side}", price=fill, quantity=qty,
                                          strategy_name=STRATEGY_TYPE)
        logger.info(f"✅ Martingale: {side} {sym} nível {leg.level}/{c.MARTINGALE_MAX_LEVELS} @ {fill}; "
                    f"próxima adição em {next_trigger(fill, side, c.MARTINGALE_STEP_PERCENT):.6g}")
        return True

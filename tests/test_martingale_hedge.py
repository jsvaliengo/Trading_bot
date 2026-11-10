"""Martingale/DCA hedge (docs/MARTINGALE_HEDGE.md): escada, gatilhos, teto de níveis,
stop de portfólio, recusa sem hedge mode, pular par abaixo do minNotional e integração."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from trading_bot.core import martingale as mg
from trading_bot.core.bot import TradingBot
from trading_bot.core.config import config
from trading_bot.services.kill_switch import KillSwitchMonitor


# ───────────── matemática da escada ─────────────
def test_ladder_sum_and_base_notional_match_prereg():
    assert mg.ladder_sum(2.0, 7) == 127.0
    # $300 × 10 × 0,5 / (3 pares × 2 × 127) = $1,97 (tabela do relatório, universo 3)
    assert mg.base_notional(capital=300, leverage=10, n_pairs=3, m=2.0, n=7) == pytest.approx(1.9685, abs=1e-3)
    assert mg.base_notional(capital=0, leverage=10, n_pairs=3, m=2.0, n=7) == 0.0
    assert mg.level_notional(2.0, 2.0, 3) == 16.0


def test_add_fill_recomputes_average():
    avg, qty = mg.add_fill(100.0, 1.0, 98.0, 2.0)
    assert (avg, qty) == (pytest.approx(98.6667, abs=1e-4), 3.0)
    assert mg.add_fill(100.0, 0.0, 98.0, 0.0) == (100.0, 0.0)


@pytest.mark.parametrize("side,trigger,tp", [("LONG", 98.5, 101.0), ("SHORT", 101.5, 99.0)])
def test_next_trigger_and_tp_are_mirrored(side, trigger, tp):
    assert mg.next_trigger(100.0, side, 1.5) == pytest.approx(trigger)
    assert mg.tp_price(100.0, side, 1.0) == pytest.approx(tp)
    assert mg.add_triggered(trigger, side, trigger) and not mg.add_triggered(100.0, side, trigger)
    assert mg.tp_hit(tp, side, tp) and not mg.tp_hit(100.0, side, tp)


def test_infer_level_from_exchange_notional():
    assert mg.infer_level(2.0, 2.0, 2.0, 7) == 1          # só a base
    assert mg.infer_level(14.0, 2.0, 2.0, 7) == 3         # B(1+2+4)
    assert mg.infer_level(13.0, 2.0, 2.0, 7) == 3         # arredondamento de step
    assert mg.infer_level(1000.0, 2.0, 2.0, 7) == 7       # acima da escada: teto
    assert mg.infer_level(5.0, 0.0, 2.0, 7) == 1


def test_portfolio_stop_hit_at_20_percent():
    assert mg.portfolio_stop_hit(80.0, 100.0, 20.0)
    assert not mg.portfolio_stop_hit(80.01, 100.0, 20.0)
    assert not mg.portfolio_stop_hit(50.0, 0.0, 20.0)


# ───────────── runner com fakes ─────────────
@pytest.fixture(autouse=True)
def _no_add_interval(monkeypatch):
    # os testes de escada dão vários ticks seguidos; o freio de intervalo tem teste próprio
    monkeypatch.setattr(config, "MARTINGALE_MIN_ADD_INTERVAL_SECONDS", 0)


class _Ex:
    def __init__(self, price=100.0, hedge=True, balance=300.0, min_notional=5.0, stale=False, fill_price=None):
        self.price, self.hedge, self.balance, self.min_notional = price, hedge, balance, min_notional
        self.stale, self.fill_price = stale, fill_price   # stale: posição não reflete o fill; fill_price: avgPrice forçado
        self.positions = []
        self.orders = []
        self.set_leverage = MagicMock(return_value=True)
        self.cancel_open_orders = MagicMock(return_value=True)
        self.min_notionals = {}   # minNotional por símbolo (override do default)

    def set_hedge_mode(self):
        return self.hedge

    def get_account_balance(self, force_refresh=False):
        return self.balance

    def get_open_positions(self, force_refresh=False):
        return [dict(p) for p in self.positions]

    def get_symbol_info(self, s):
        return {"minNotional": self.min_notionals.get(s, self.min_notional), "quantityPrecision": 2, "pricePrecision": 4}

    def get_symbol_ticker(self, _s):
        return {"price": str(self.price)}

    def place_market_order(self, **kw):
        self.orders.append(kw)
        fill = self.fill_price or self.price
        # hedge mode agrega fills da mesma perna: simula a média da exchange
        for p in self.positions:
            if p["symbol"] == kw["symbol"] and p["side"] == kw["position_side"]:
                if not self.stale:
                    p["entry_price"], p["quantity"] = mg.add_fill(p["entry_price"], p["quantity"], fill, kw["quantity"])
                return {"avgPrice": str(fill)}
        self.positions.append({"symbol": kw["symbol"], "side": kw["position_side"], "entry_price": fill,
                               "quantity": kw["quantity"], "unrealized_pnl": 0.0, "mark_price": self.price})
        return {"avgPrice": str(fill)}


def _bot(ex, pairs=("ADAUSDT",)):
    config.MARTINGALE_PAIRS = list(pairs)
    bot = TradingBot.__new__(TradingBot)
    bot._init_runtime_state()
    bot._positions_lock = threading.Lock()
    bot.exchange = ex
    bot.known_positions = {}
    bot.drawdown_equity = lambda wallet: wallet
    bot.ledger = SimpleNamespace(record_trade_opened=MagicMock())
    bot.telegram = SimpleNamespace(send_trade_alert=MagicMock(), send_message=MagicMock())
    bot.trade_store = None
    closed = []

    def close(pos, reason):
        closed.append((pos["symbol"], pos["side"], reason))
        ex.positions = [p for p in ex.positions if (p["symbol"], p["side"]) != (pos["symbol"], pos["side"])]
        return True

    bot.close_position = close
    bot.closed = closed
    return bot


def test_start_sizes_base_and_opens_both_legs_on_first_tick():
    ex = _Ex(balance=300.0, min_notional=1.0)
    bot = _bot(ex)
    r = mg.MartingaleRunner(bot)
    assert r.start() and r.enabled
    assert r.base == pytest.approx(300 * 10 * 0.5 / (1 * 2 * 127))     # $5,91 com 1 par
    ex.set_leverage.assert_called_once_with("ADAUSDT", config.MARTINGALE_LEVERAGE)
    r.tick()
    assert [(o["side"], o["position_side"]) for o in ex.orders] == [("BUY", "LONG"), ("SELL", "SHORT")]
    assert {k: m["strategy_type"] for k, m in bot.known_positions.items()} == {
        "ADAUSDT_LONG": "martingale_hedge", "ADAUSDT_SHORT": "martingale_hedge"}
    assert bot.ledger.record_trade_opened.call_count == 2
    r.tick()                                   # idempotente: posições existem
    assert len(ex.orders) == 2


def test_adds_level_on_step_against_last_fill_and_caps_at_n(monkeypatch):
    monkeypatch.setattr(config, "MARTINGALE_MAX_LEVELS", 3)
    ex = _Ex(min_notional=1.0)
    bot = _bot(ex)
    r = mg.MartingaleRunner(bot)
    r.start()
    r.tick()                                   # base em 100
    long_leg = r.pairs["ADAUSDT"].legs["LONG"]
    ex.price = 98.6                            # −1,4%: ainda não
    r.tick()
    assert long_leg.level == 1
    ex.price = 98.5                            # −1,5% do último fill: nível 1
    r.tick()
    assert long_leg.level == 2 and long_leg.last_fill == 98.5
    add = [o for o in ex.orders if o["position_side"] == "LONG"][-1]
    assert add["side"] == "BUY" and add["quantity"] == pytest.approx(round(r.base * 2 / 98.5, 2))
    ex.price = 97.0                            # −1,52% de 98,5: nível 2 (último permitido com N=3)
    r.tick()
    assert long_leg.level == 3
    ex.price = 90.0                            # N atingido: espera o TP
    r.tick()
    assert long_leg.level == 3 and len([o for o in ex.orders if o["position_side"] == "LONG"]) == 3
    # só a base vai para o SQLite como abertura (a perna SHORT bateu o TP na queda e reabriu)
    assert sum(c.kwargs["side"] == "LONG" for c in bot.ledger.record_trade_opened.call_args_list) == 1


@pytest.mark.parametrize("side,price", [("LONG", 101.0), ("SHORT", 99.0)])
def test_tp_closes_leg_at_average_plus_tp_and_reopens_next_tick(side, price):
    ex = _Ex(min_notional=1.0)
    bot = _bot(ex)
    r = mg.MartingaleRunner(bot)
    r.start()
    r.tick()
    ex.price = price
    r.tick()
    assert bot.closed == [("ADAUSDT", side, "Martingale TP nível 1")]
    assert r.pairs["ADAUSDT"].legs[side].level == 0
    r.tick()                                   # reabre a base da perna fechada
    assert len(ex.positions) == 2 and len(ex.orders) == 3


def test_paused_bot_still_takes_profit_but_opens_nothing():
    ex = _Ex(min_notional=1.0)
    bot = _bot(ex)
    r = mg.MartingaleRunner(bot)
    r.start()
    r.tick()
    bot.paused = True
    ex.price = 98.5                            # SHORT bate o TP (fecha); LONG não adiciona
    r.tick()
    ex.price = 101.0                           # LONG bate o TP
    r.tick()
    r.tick()
    assert [c[1] for c in bot.closed] == ["SHORT", "LONG"]
    assert len(ex.orders) == 2                 # sem adição nem reabertura


def test_portfolio_stop_closes_everything_pauses_and_does_not_restart():
    ex = _Ex(min_notional=1.0)
    bot = _bot(ex)
    r = mg.MartingaleRunner(bot)
    r.start()
    r.tick()
    assert r.peak == 300.0
    ex.balance = 240.0                         # −20% do pico
    r.tick()
    assert sorted(bot.closed) == [("ADAUSDT", "LONG", "Stop de portfólio"), ("ADAUSDT", "SHORT", "Stop de portfólio")]
    assert bot.paused and r.stops == 1 and ex.positions == []
    assert "STOP DE PORTFÓLIO" in bot.telegram.send_message.call_args.args[0]
    r.tick()
    assert len(ex.orders) == 2                 # pausado: não reabre sozinho


def test_portfolio_stop_floor_is_enforced_by_config():
    assert 5.0 <= config.MARTINGALE_PORTFOLIO_STOP_PERCENT <= 25.0


def test_refuses_to_start_without_hedge_mode():
    ex = _Ex(hedge=False)
    bot = _bot(ex)
    r = mg.MartingaleRunner(bot)
    assert r.start() is False and not r.enabled and bot.paused
    assert "Hedge mode" in bot.telegram.send_message.call_args.args[0]
    r.tick()
    assert ex.orders == []


def test_skips_pair_when_base_is_below_min_notional(caplog):
    ex = _Ex(balance=50.0, min_notional=5.0)   # B = 50×10×0,5/(2×127) ≈ $0,98
    bot = _bot(ex)
    r = mg.MartingaleRunner(bot)
    assert r.start() is False and bot.paused
    assert "minNotional" in caplog.text and "nenhum par cabe" in caplog.text


def test_restart_recovers_leg_level_from_exchange_notional():
    ex = _Ex(min_notional=1.0)
    bot = _bot(ex)
    r = mg.MartingaleRunner(bot)
    r.start()
    # médias longe do TP (LONG 101 → TP 102,01; SHORT 99 → TP 98,01) para só medir a recuperação
    ex.positions = [{"symbol": "ADAUSDT", "side": "LONG", "entry_price": 101.0, "quantity": round(r.base * 3 / 101, 2)},
                    {"symbol": "ADAUSDT", "side": "SHORT", "entry_price": 99.0, "quantity": round(r.base / 99, 2)}]
    r.tick()
    assert r.pairs["ADAUSDT"].legs["LONG"].level == 2 and r.pairs["ADAUSDT"].legs["SHORT"].level == 1
    assert ex.orders == []
    # último fill = média da exchange, não o preço atual (100): degrau não desloca a cada restart
    assert (r.pairs["ADAUSDT"].legs["LONG"].last_fill, r.pairs["ADAUSDT"].legs["SHORT"].last_fill) == (101.0, 99.0)


# ───────────── incidente 2026-11-08: cascata de adições (nível 3→9 em 18 s) ─────────────
def test_stale_exchange_read_and_fill_above_mark_do_not_cascade(monkeypatch):
    monkeypatch.setattr(config, "MARTINGALE_MAX_LEVELS", 9)
    now = [1_000.0]
    monkeypatch.setattr(mg.time, "time", lambda: now[0])
    ex = _Ex(min_notional=1.0, stale=True)
    bot = _bot(ex)
    r = mg.MartingaleRunner(bot)
    r.start()
    r.tick()                                   # base @ 100 nas duas pernas
    leg = r.pairs["ADAUSDT"].legs["LONG"]
    base_qty = leg.qty
    ex.price, ex.fill_price = 98.2, 100.0      # ticker −1,8%; fill reportado em 100 (igual ao log: @ 0.1762 com mark 0.1731)
    r.tick()
    assert leg.level == 2
    assert leg.last_fill == 98.2               # degrau ancorado no pior entre fill e mark, não no fill acima do mercado
    assert leg.qty > base_qty and leg.avg == pytest.approx(100.0)
    for _ in range(5):                         # exchange segue devolvendo avg/qty antigos: NÃO adiciona de novo
        r.tick()
    assert leg.level == 2 and len([o for o in ex.orders if o["position_side"] == "LONG"]) == 2
    assert ex.positions[0]["quantity"] == base_qty   # exchange stale (qty < local) não sobrescreve o estado local
    ex.positions[0]["quantity"], ex.positions[0]["entry_price"] = leg.qty, 99.0
    r.tick()                                   # exchange reflete o fill (qty ≥ local): reconcilia
    assert (leg.avg, leg.qty) == (99.0, ex.positions[0]["quantity"])
    ex.positions[0]["quantity"], now[0] = base_qty, now[0] + 31   # passada a janela, a exchange volta a ser a verdade
    r.tick()
    assert leg.qty == base_qty and leg.level == 2


def test_min_interval_between_adds_on_same_leg(monkeypatch):
    monkeypatch.setattr(config, "MARTINGALE_MIN_ADD_INTERVAL_SECONDS", 60)
    now = [1_000.0]
    monkeypatch.setattr(mg.time, "time", lambda: now[0])
    ex = _Ex(min_notional=1.0)
    bot = _bot(ex)
    r = mg.MartingaleRunner(bot)
    r.start()
    r.tick()                                   # base em t=1000
    leg = r.pairs["ADAUSDT"].legs["LONG"]
    ex.price = 97.0                            # −3%: gatilho satisfeito, mas há 10 s da base
    now[0] += 10
    r.tick()
    assert leg.level == 1
    now[0] += 50                               # 60 s: adiciona UMA vez por tick
    r.tick()
    assert leg.level == 2
    r.tick()
    assert leg.level == 2
    ex.price, now[0] = 95.0, now[0] + 60          # próximo degrau (−2% de 97) e intervalo vencido
    r.tick()
    assert leg.level == 3


def test_leg_state_survives_restart_via_state():
    from trading_bot.core.bot_state_serde import BotStatePersistence

    ex = _Ex(min_notional=1.0)
    bot = _bot(ex)
    r = mg.MartingaleRunner(bot)
    r.start()
    r.tick()
    ex.price = 98.5
    r.tick()                                   # LONG nível 2, último fill 98,5
    bot.martingale, bot.kill_switch = r, None
    r.drain.add("TRXUSDT")
    payload = BotStatePersistence(bot).build_payload()
    legs = payload["martingale_legs"]
    assert payload["martingale_drain_pairs"] == ["TRXUSDT"]
    assert legs["ADAUSDT"]["LONG"]["level"] == 2 and legs["ADAUSDT"]["LONG"]["last_fill"] == 98.5
    bot2 = _bot(ex)
    r2 = mg.MartingaleRunner(bot2)
    r2.saved_legs = legs                       # bot.load_state() guarda state['martingale_legs'] aqui
    r2.start()
    r2.tick()
    leg = r2.pairs["ADAUSDT"].legs["LONG"]
    assert (leg.level, leg.last_fill) == (2, 98.5)                        # restaurado do state, não inferido
    assert len([o for o in ex.orders if o["position_side"] == "LONG"]) == 2   # sem reabrir/adicionar (SHORT bateu TP e reabriu)


# ───────────── seleção automática de pares pelo ranking ─────────────
def _sc(color, reasons=()):
    # números coerentes com a cor pra rank_score ordenar; adx varia pra desempatar na ordem dada
    vol, adx, mv = {"green": (2.0, 15.0, 5.0), "yellow": (4.0, 28.0, 12.0), "red": (6.0, 40.0, 30.0)}[color]
    return {"color": color, "vol_pct": vol, "adx": adx, "max_move_pct": mv, "volume_usd": 200e6, "reasons": list(reasons)}


def _scored(r, **colors):
    """Instala um cache de score falso no runner: BNB="green", ADA="red"…; adx cresce na ordem dada (desempate)."""
    scores = {}
    for i, (base, color) in enumerate(colors.items()):
        sc = _sc(color, ["adx 40 (>35)"] if color == "red" else [])
        sc["adx"] += i * 0.1
        scores[base + "USDT"] = sc
    r.score_cache = SimpleNamespace(get=lambda: {"scores": scores, "updated_at": "2026-11-09T12:00:00+00:00"})
    return scores


def _tp_closed_all(ex, r):
    """Simula as pernas fechadas por TP: exchange sem posição e pernas locais resetadas."""
    ex.positions = []
    for plan in r.pairs.values():
        for leg in plan.legs.values():
            leg.reset()


@pytest.fixture
def _one_slot_auto(monkeypatch):
    # banca 100 → faixa 90–150 = 1 moeda; auto ligado, sem cooldown
    monkeypatch.setattr(config, "MARTINGALE_AUTO_PAIRS", True)
    monkeypatch.setattr(config, "MARTINGALE_PAIR_SWAP_COOLDOWN_MINUTES", 0)


def _runner(ex, pairs=("ADAUSDT",)):
    bot = _bot(ex, pairs)
    r = mg.MartingaleRunner(bot)
    bot.martingale = r
    assert r.start()
    return bot, r


def test_swaps_red_pair_for_best_green_when_both_legs_are_flat(_one_slot_auto, caplog):
    ex = _Ex(balance=100.0, min_notional=1.0)
    bot, r = _runner(ex)
    _scored(r, BNB="green", ADA="red", TRX="yellow")
    caplog.set_level("INFO")
    r.tick()
    assert list(r.pairs) == ["BNBUSDT"]
    assert {o["symbol"] for o in ex.orders} == {"BNBUSDT"} and len(ex.orders) == 2      # base LONG+SHORT no par novo
    ex.set_leverage.assert_any_call("BNBUSDT", config.MARTINGALE_LEVERAGE)
    assert "trocando slot ADAUSDT → BNBUSDT" in caplog.text and "ADA vermelho: adx 40 (>35)" in caplog.text
    assert "BNB verde, 1º no ranking" in caplog.text
    assert "ADAUSDT → BNBUSDT" in bot.telegram.send_message.call_args[0][0]


def test_never_swaps_while_one_leg_is_open_below_breakeven(_one_slot_auto):
    ex = _Ex(balance=100.0, min_notional=1.0)
    bot, r = _runner(ex)
    r.tick()                                                   # abre LONG e SHORT em ADA
    ex.positions = [p for p in ex.positions if p["side"] == "SHORT"]   # LONG fechada fora; SHORT segue aberta
    ex.positions[0]["unrealized_pnl"] = -0.5                   # no prejuízo: a saída no zero-a-zero não se aplica
    _scored(r, BNB="green", ADA="red")
    r.tick()
    assert list(r.pairs) == ["ADAUSDT"] and all(o["symbol"] == "ADAUSDT" for o in ex.orders)
    bot.telegram.send_message.assert_not_called()


def test_transient_empty_positions_read_does_not_swap(_one_slot_auto):
    ex = _Ex(balance=100.0, min_notional=1.0)
    bot, r = _runner(ex)
    r.tick()                                                   # ADA LONG+SHORT abertas (pernas locais nível 1)
    real = ex.positions
    ex.positions = []                                          # leitura vazia transitória da exchange
    _scored(r, BNB="green", ADA="red")
    r.tick()
    assert list(r.pairs) == ["ADAUSDT"]                        # pernas locais ainda não zeradas: não troca
    ex.positions = real


@pytest.mark.parametrize("current", ["green", "yellow"])
def test_keeps_current_pair_when_green_or_yellow(_one_slot_auto, current):
    ex = _Ex(balance=100.0, min_notional=1.0)
    bot, r = _runner(ex)
    _scored(r, BNB="green", ADA=current)        # BNB tem número melhor, mas ADA não está vermelho
    r.tick()
    assert list(r.pairs) == ["ADAUSDT"]


def test_keeps_current_pair_without_score(_one_slot_auto):
    ex = _Ex(balance=100.0, min_notional=1.0)
    bot, r = _runner(ex)
    r.tick()
    assert list(r.pairs) == ["ADAUSDT"]        # cache vazio (1ª rodada ainda não terminou)
    r.score_cache = SimpleNamespace(get=lambda: {"scores": {"BNBUSDT": _sc("green")}, "updated_at": None})
    _tp_closed_all(ex, r)
    r.tick()
    assert list(r.pairs) == ["ADAUSDT"]        # ADA sem score (unknown): mantém


def test_swap_cooldown_blocks_ping_pong(_one_slot_auto, monkeypatch):
    monkeypatch.setattr(config, "MARTINGALE_PAIR_SWAP_COOLDOWN_MINUTES", 360)
    ex = _Ex(balance=100.0, min_notional=1.0)
    bot, r = _runner(ex)
    _scored(r, BNB="green", ADA="red")
    r.tick()
    assert list(r.pairs) == ["BNBUSDT"]        # par inicial (since=0) troca na hora
    _tp_closed_all(ex, r)
    _scored(r, ETH="green", BNB="red")
    r.tick()
    assert list(r.pairs) == ["BNBUSDT"]        # BNB acabou de ocupar a vaga: 6h de cooldown
    r.pairs["BNBUSDT"].since -= 361 * 60
    _tp_closed_all(ex, r)
    r.tick()
    assert list(r.pairs) == ["ETHUSDT"]


def test_fills_slots_of_current_sheet_row_and_shrinks_when_row_drops(_one_slot_auto):
    ex = _Ex(balance=300.0, min_notional=1.0)   # faixa 300 → 3 moedas
    bot, r = _runner(ex)
    _scored(r, TRX="green", BNB="yellow", ADA="red")
    r.tick()
    assert list(r.pairs) == ["TRXUSDT", "BNBUSDT"]   # ADA vermelho sai, TRX entra; 2ª vaga vai pro próximo (BNB)
    _tp_closed_all(ex, r)
    ex.balance = 250.0                               # faixa 90–300 = 1 moeda (sem bater o stop de 20%): solta o pior zerado
    r.tick()
    assert list(r.pairs) == ["TRXUSDT"]


def test_skips_candidates_already_in_slot_with_open_position_or_below_min_notional(_one_slot_auto):
    ex = _Ex(balance=300.0, min_notional=0.5)   # faixa 300 → 3 vagas, 2 ocupadas
    ex.min_notionals["ETHUSDT"] = 1000.0
    bot, r = _runner(ex, pairs=("ADAUSDT", "TRXUSDT"))
    ex.positions = [{"symbol": "BNBUSDT", "side": "LONG", "entry_price": 1.0, "quantity": 1.0, "unrealized_pnl": 0.0, "mark_price": 1.0}]
    _scored(r, TRX="green", BNB="green", ETH="green", LINK="green", ADA="red")
    r.tick()
    # TRX já ocupa vaga, BNB tem posição órfã na exchange, ETH não cabe na 1ª ordem → LINK
    assert "LINKUSDT" in r.pairs and "ADAUSDT" not in r.pairs and "ETHUSDT" not in r.pairs and "BNBUSDT" not in r.pairs


def _red_with_open_legs(pnl=(0.02, 0.01), **colors):
    """ADA vermelho com LONG+SHORT abertas e P&L não realizado dado por perna; BNB verde como candidato."""
    ex = _Ex(balance=100.0, min_notional=1.0)
    bot, r = _runner(ex)
    r.tick()                                                   # abre LONG e SHORT em ADA
    for p, u in zip(ex.positions, pnl):
        p["unrealized_pnl"] = u
    _scored(r, **(colors or {"BNB": "green", "ADA": "red"}))
    return ex, bot, r


def test_red_pair_exits_at_breakeven_and_frees_slot_for_green(_one_slot_auto, caplog):
    ex, bot, r = _red_with_open_legs(pnl=(0.02, 0.01))
    caplog.set_level("INFO")
    r.tick()
    assert [c[:2] for c in bot.closed] == [("ADAUSDT", "LONG"), ("ADAUSDT", "SHORT")]
    assert all(c[2] == "Martingale: par vermelho saindo no zero-a-zero" for c in bot.closed)
    assert list(r.pairs) == ["BNBUSDT"]
    assert [o["symbol"] for o in ex.orders][-2:] == ["BNBUSDT", "BNBUSDT"]     # base do par novo no mesmo tick
    assert "fechando ADAUSDT no zero-a-zero (+0.03$) para liberar vaga: ADA vermelho (adx 40 (>35)); candidato BNB verde 1º" in caplog.text
    assert "trocando slot ADAUSDT → BNBUSDT" in caplog.text
    assert any("zero-a-zero" in c[0][0] for c in bot.telegram.send_message.call_args_list)


def test_red_pair_with_negative_pnl_stays(_one_slot_auto):
    ex, bot, r = _red_with_open_legs(pnl=(-0.6, 0.1))
    r.tick()
    assert bot.closed == [] and list(r.pairs) == ["ADAUSDT"]


def test_red_pair_never_closes_into_an_empty_slot(_one_slot_auto):
    ex, bot, r = _red_with_open_legs(pnl=(0.02, 0.01), ADA="red", TRX="red")   # nenhum candidato verde/amarelo
    r.tick()
    assert bot.closed == [] and list(r.pairs) == ["ADAUSDT"]


def test_red_exit_respects_feature_flag(_one_slot_auto, monkeypatch):
    monkeypatch.setattr(config, "MARTINGALE_RED_EXIT_AT_BREAKEVEN", False)
    ex, bot, r = _red_with_open_legs(pnl=(0.02, 0.01))
    r.tick()
    assert bot.closed == [] and list(r.pairs) == ["ADAUSDT"]


def test_red_exit_respects_swap_cooldown(_one_slot_auto, monkeypatch):
    monkeypatch.setattr(config, "MARTINGALE_PAIR_SWAP_COOLDOWN_MINUTES", 360)
    ex, bot, r = _red_with_open_legs(pnl=(0.02, 0.01))
    r.pairs["ADAUSDT"].since = time.time()                     # acabou de ocupar a vaga
    r.tick()
    assert bot.closed == [] and list(r.pairs) == ["ADAUSDT"]
    r.pairs["ADAUSDT"].since -= 361 * 60
    r.tick()
    assert len(bot.closed) == 2 and list(r.pairs) == ["BNBUSDT"]


def test_red_exit_keeps_pair_when_a_close_fails(_one_slot_auto):
    ex, bot, r = _red_with_open_legs(pnl=(0.02, 0.01))
    bot.close_position = lambda pos, reason: False
    r.tick()
    assert list(r.pairs) == ["ADAUSDT"] and r.pairs["ADAUSDT"].legs["LONG"].level == 1


def test_pairs_persist_in_state_and_win_over_env_on_restart(_one_slot_auto):
    from trading_bot.core.bot_state_serde import BotStatePersistence

    ex = _Ex(balance=100.0, min_notional=1.0)
    bot, r = _runner(ex)
    _scored(r, BNB="green", ADA="red")
    r.tick()
    bot.kill_switch = None
    payload = BotStatePersistence(bot).build_payload()
    assert payload["martingale_pairs"] == ["BNBUSDT"]
    bot2 = _bot(_Ex(balance=100.0, min_notional=1.0), pairs=("ADAUSDT",))
    r2 = mg.MartingaleRunner(bot2)
    r2.saved_pairs = payload["martingale_pairs"]   # bot.load_state() guarda state['martingale_pairs'] aqui
    assert r2.start() and list(r2.pairs) == ["BNBUSDT"]


def test_auto_pairs_off_pins_env_pairs(monkeypatch):
    monkeypatch.setattr(config, "MARTINGALE_AUTO_PAIRS", False)
    monkeypatch.setattr(config, "MARTINGALE_PAIR_SWAP_COOLDOWN_MINUTES", 0)
    ex = _Ex(balance=100.0, min_notional=1.0)
    bot = _bot(ex)
    r = mg.MartingaleRunner(bot)
    r.saved_pairs = ["BNBUSDT"]
    assert r.start() and list(r.pairs) == ["ADAUSDT"] and r.score_cache is None
    _scored(r, BNB="green", ADA="red")
    r.tick()
    assert list(r.pairs) == ["ADAUSDT"]


def test_dashboard_reads_scores_from_runner_cache(_one_slot_auto):
    from trading_bot.web import data as dashboard_data

    bot, r = _runner(_Ex(balance=100.0, min_notional=1.0))
    scores = _scored(r, ADA="green")
    assert dashboard_data._pair_scores(bot)["scores"] == scores


# ───────────── integração no bot ─────────────
def test_bot_builds_runner_only_for_martingale_strategy(monkeypatch):
    for strategy, has in (("pullback", False), ("donchian_4h", False), ("martingale_hedge", True)):
        monkeypatch.setattr(config, "STRATEGY", strategy)
        bot = _bot(_Ex())
        assert isinstance(bot.martingale, mg.MartingaleRunner) is has
        assert (bot.strategy_runner is not None) is (strategy != "pullback")


def test_monitor_never_applies_tp_or_trailing_to_martingale_positions(monkeypatch):
    monkeypatch.setattr(config, "STRATEGY", "martingale_hedge")
    monkeypatch.setattr(config, "USE_TRAILING_STOP", True)
    monkeypatch.setattr(config, "TAKE_PROFIT_PERCENT", 1.5)
    bot = TradingBot.__new__(TradingBot)
    bot._init_runtime_state()
    bot._positions_lock = threading.Lock()
    pos = {"symbol": "ADAUSDT", "side": "LONG", "entry_price": 100.0, "quantity": 1.0,
           "unrealized_pnl": 10.0, "mark_price": 110.0}
    bot.exchange = SimpleNamespace(get_open_positions=lambda *a, **kw: [dict(pos)])
    bot._close_position_with_notification = MagicMock(return_value=True)
    bot._process_binance_closed_position = MagicMock()
    bot.telegram = SimpleNamespace(send_message=MagicMock(), send_trade_alert=MagicMock())
    bot.known_positions = {"ADAUSDT_LONG": {**pos, "strategy_name": "martingale_hedge",
                                            "strategy_type": "martingale_hedge"}}
    bot.monitor_positions()
    bot._close_position_with_notification.assert_not_called()


def test_kill_switch_is_off_under_martingale(monkeypatch):
    monkeypatch.setattr(config, "STRATEGY", "martingale_hedge")
    ks = KillSwitchMonitor(config_obj=config, telegram=SimpleNamespace(send_message=MagicMock()))
    ks._check_loss_streak = ks._check_drawdown_alert = ks._check_win_rate_floor = MagicMock()
    ks.check_all(bot=SimpleNamespace(paused=False))
    assert not ks._check_loss_streak.called and not ks._check_drawdown_alert.called


def test_validate_accepts_martingale_strategy(monkeypatch):
    # Regressão: validate() rejeitava 'martingale_hedge' e o bot abortava no boot da VM (08/11/26).
    monkeypatch.setattr(config, "STRATEGY", "martingale_hedge")
    assert not [e for e in _validation_errors() if "STRATEGY inválida" in e]
    monkeypatch.setattr(config, "STRATEGY", "foo")
    assert [e for e in _validation_errors() if "STRATEGY inválida" in e]


def _validation_errors():
    import logging
    msgs = []
    class _H(logging.Handler):
        def emit(self, rec):
            msgs.append(rec.getMessage())
    h = _H()
    root = logging.getLogger()
    root.addHandler(h)
    try:
        config.validate()
    finally:
        root.removeHandler(h)
    return msgs


# ───────────── escada pro dashboard ─────────────
def test_ladder_exposes_next_add_tp_and_portfolio_stop(monkeypatch):
    monkeypatch.setattr(config, "MARTINGALE_MAX_LEVELS", 3)
    ex = _Ex(price=100.0, balance=300.0, min_notional=1.0)
    bot = _bot(ex)
    r = mg.MartingaleRunner(bot)
    assert r.start()
    r.tick()                                   # abre LONG e SHORT @ 100
    ex.price = 99.0
    positions = [{"symbol": p["symbol"], "side": p["side"], "entry_price": p["entry_price"], "quantity": p["quantity"],
                  "mark_price": 99.0, "unrealized_pnl_usd": -0.05 if p["side"] == "LONG" else 0.05} for p in ex.positions]
    out = r.ladder(positions, equity=300.0)
    assert out["portfolio_stop_equity"] == pytest.approx(240.0)          # pico 300 × 0,8
    assert out["portfolio_stop_room_pct"] == pytest.approx(20.0)
    assert out["params"]["max_levels"] == 3
    long, short = {leg["side"]: leg for leg in out["legs"]}["LONG"], {leg["side"]: leg for leg in out["legs"]}["SHORT"]
    assert (long["level"], long["max_levels"], long["avg_price"], long["mark_price"]) == (1, 3, 100.0, 99.0)
    assert long["next_add_price"] == pytest.approx(98.5) and long["next_add_pct"] == pytest.approx(-0.5 / 99 * 100)
    assert long["tp_price"] == pytest.approx(101.0) and long["tp_pct"] == pytest.approx(2.0 / 99 * 100)
    assert long["unrealized_pnl_usd"] == -0.05 and long["notional"] == pytest.approx(long["quantity"] * 99.0)
    assert short["next_add_price"] == pytest.approx(101.5) and short["tp_price"] == pytest.approx(99.0)
    # teto da escada: sem próxima adição; perna sem posição: só o nível
    r.pairs["ADAUSDT"].legs["LONG"].level = 3
    out = r.ladder(positions, equity=None)
    assert out["portfolio_stop_room_pct"] is None
    assert {leg["side"]: leg["next_add_price"] for leg in out["legs"]}["LONG"] is None
    empty = r.ladder([], equity=300.0)
    assert all(leg["avg_price"] is None and leg["level"] == lvl for leg, lvl in zip(empty["legs"], (3, 1)))


# ───────────── perfil planilha CoinTech2u (sizing fixo, double first, stop em USDT) ─────────────
def test_double_first_doubles_only_the_first_order():
    assert mg.ladder_sum(2.0, 9, double_first=True) == 512.0          # 2 + (2^9 − 2)
    assert mg.ladder_sum(2.0, 0, double_first=True) == 0.0
    assert [mg.level_notional(3.0, 2.0, k, double_first=True) for k in range(4)] == [6.0, 6.0, 12.0, 24.0]
    assert mg.base_notional(capital=300, leverage=20, n_pairs=2, m=2.0, n=9, double_first=True) == pytest.approx(
        300 * 20 * 0.5 / (2 * 2 * 512))
    assert mg.infer_level(12.0, 3.0, 2.0, 9, double_first=True) == 2   # 2B + 2B


def test_fixed_base_notional_margin_vs_notional():
    assert mg.fixed_base_notional(3.0, 20, is_notional=False) == 60.0
    assert mg.fixed_base_notional(3.0, 20, is_notional=True) == 3.0


def test_stop_loss_usdt_prevails_over_percent_without_clamp():
    assert mg.portfolio_stop_floor(300.0, 20.0) == pytest.approx(240.0)
    assert mg.portfolio_stop_floor(300.0, 20.0, stop_usdt=180.0) == pytest.approx(120.0)
    assert mg.portfolio_stop_hit(120.0, 300.0, 20.0, 180.0) and not mg.portfolio_stop_hit(120.01, 300.0, 20.0, 180.0)
    assert not mg.portfolio_stop_hit(240.0, 300.0, 20.0, 180.0)    # −20% não dispara quando o $ manda


def test_start_uses_fixed_first_order_and_warns_on_big_usdt_stop(monkeypatch, caplog):
    monkeypatch.setattr(config, "MARTINGALE_FIRST_ORDER_USDT", 3.0)
    monkeypatch.setattr(config, "MARTINGALE_LEVERAGE", 20)
    monkeypatch.setattr(config, "MARTINGALE_DOUBLE_FIRST", True)
    monkeypatch.setattr(config, "MARTINGALE_STOP_LOSS_USDT", 180.0)
    ex = _Ex(price=100.0, balance=300.0, min_notional=5.0)
    bot = _bot(ex, pairs=("ADAUSDT", "TRXUSDT"))
    r = mg.MartingaleRunner(bot)
    assert r.start() and r.base == 60.0                                  # $3 margem × 20x, não a fórmula
    assert "60% da banca" in caplog.text
    r.tick()
    assert [o["quantity"] for o in ex.orders] == [pytest.approx(1.2)] * 4   # 1ª ordem dobrada: $120 @ 100
    ex.price = 98.5
    r.tick()
    add = [o for o in ex.orders if o["position_side"] == "LONG" and o["symbol"] == "ADAUSDT"][-1]
    assert add["quantity"] == pytest.approx(round(120 / 98.5, 2))        # nível 1 = 2B, igual à 1ª
    assert r.ladder([], equity=300.0)["portfolio_stop_equity"] == pytest.approx(120.0)
    ex.balance = 120.0                                                   # pico 300 − $180
    r.tick()
    assert bot.paused and r.stops == 1 and ex.positions == []


def test_fixed_mode_bumps_base_to_min_notional_instead_of_skipping(monkeypatch, caplog):
    monkeypatch.setattr(config, "MARTINGALE_FIRST_ORDER_USDT", 3.0)
    monkeypatch.setattr(config, "MARTINGALE_FIRST_ORDER_IS_NOTIONAL", True)
    monkeypatch.setattr(config, "MARTINGALE_DOUBLE_FIRST", False)
    ex = _Ex(min_notional=5.0)
    bot = _bot(ex)
    r = mg.MartingaleRunner(bot)
    assert r.start() and r.base == 5.0 and "ADAUSDT" in r.pairs
    assert "base subida para $5.00" in caplog.text
    monkeypatch.setattr(config, "MARTINGALE_DOUBLE_FIRST", True)
    r2 = mg.MartingaleRunner(_bot(_Ex(min_notional=5.0)))
    assert r2.start() and r2.base == 3.0                                 # 1ª ordem dobrada = $6 ≥ $5: sem bump
    monkeypatch.setattr(config, "MARTINGALE_FIRST_ORDER_USDT", 2.0)
    r3 = mg.MartingaleRunner(_bot(_Ex(min_notional=5.0)))
    assert r3.start() and r3.base == 2.5                                 # $4 dobrado < $5: base = 5/2


# ───────────── drenagem de par (MARTINGALE_DRAIN_PAIRS / /drain) ─────────────
@pytest.fixture
def _drain_ada(monkeypatch):
    monkeypatch.setattr(config, "MARTINGALE_DRAIN_PAIRS", ["ADAUSDT"])


def test_draining_pair_neither_adds_nor_reopens_but_still_takes_profit(monkeypatch):
    ex = _Ex(min_notional=1.0)
    bot, r = _runner(ex)
    monkeypatch.setattr(config, "MARTINGALE_AUTO_PAIRS", False)
    r.tick()
    ex.price = 98.5
    r.tick()
    ex.price = 97.0
    r.tick()                                                   # LONG nível 3
    assert r.pairs["ADAUSDT"].legs["LONG"].level == 3
    r.drain.add("ADAUSDT")
    n = len(ex.orders)
    ex.price = 95.5
    r.tick()                                                   # cruzou a próxima adição: nada
    assert r.pairs["ADAUSDT"].legs["LONG"].level == 3 and len(ex.orders) == n
    ex.price = 96.0                                            # SHORT (reaberta a 97 após TP) → TP em 96.03
    r.tick()
    assert ("ADAUSDT", "SHORT", "Martingale TP nível 1") in bot.closed
    r.tick()                                                   # SHORT fechada não reabre
    assert len(ex.orders) == n and r.pairs["ADAUSDT"].legs["SHORT"].level == 0
    assert r.snapshot()["draining"] == ["ADAUSDT"]


def test_draining_pair_frees_its_slot_immediately(_one_slot_auto, _drain_ada):
    ex = _Ex(balance=300.0, min_notional=1.0)                  # faixa 300 → 3 vagas
    bot, r = _runner(ex, pairs=("ADAUSDT", "TRXUSDT"))
    r.drain = set(config.MARTINGALE_DRAIN_PAIRS)
    r.pairs["ADAUSDT"].legs["LONG"].level = 2                  # ADA com perna aberta
    ex.positions = [{"symbol": "ADAUSDT", "side": "LONG", "entry_price": 100.0, "quantity": 1.0, "unrealized_pnl": -0.2, "mark_price": 100.0}]
    _scored(r, BNB="green", TRX="green", ADA="green")
    r.tick()
    assert list(r.pairs) == ["ADAUSDT", "TRXUSDT", "BNBUSDT"]  # BNB entra com ADA ainda aberta
    assert {o["symbol"] for o in ex.orders} == {"TRXUSDT", "BNBUSDT"}
    r.tick()
    assert "ADAUSDT" in r.pairs and len(r.pairs) == 3          # vaga não é duplicada


def test_draining_pair_is_removed_once_flat_and_never_re_added(_one_slot_auto, _drain_ada, caplog):
    ex = _Ex(balance=100.0, min_notional=0.5)                  # 1 vaga
    bot, r = _runner(ex, pairs=("ADAUSDT", "TRXUSDT"))
    r.drain = set(config.MARTINGALE_DRAIN_PAIRS)
    _scored(r, ADA="green", TRX="yellow")
    caplog.set_level("INFO")
    r.tick()
    assert list(r.pairs) == ["TRXUSDT"] and "ADAUSDT drenado" in caplog.text
    assert all(o["symbol"] == "TRXUSDT" for o in ex.orders)
    _tp_closed_all(ex, r)
    _scored(r, ADA="green", TRX="red")                         # ADA 1º no ranking, mas drenando: não volta
    r.tick()
    assert "ADAUSDT" not in r.pairs


def test_pair_outside_drain_list_behaves_as_before(_drain_ada):
    ex = _Ex(min_notional=1.0)
    bot, r = _runner(ex, pairs=("TRXUSDT",))
    r.tick()
    assert [o["symbol"] for o in ex.orders] == ["TRXUSDT", "TRXUSDT"] and r.snapshot()["draining"] == []

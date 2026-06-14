"""
Testes da troca de par por regime (abordagem A): despeja slot OCIOSO em regime
non-trend e preenche pelo melhor candidato por score, mantendo pares em trend e
com posição aberta. Cooldown por símbolo evita carrossel.
"""
from __future__ import annotations

from types import SimpleNamespace

from trading_bot.core.bot import TradingBot
from trading_bot.core.config import config


def _make_bot(monkeypatch, active, committed, positions=(), ranked=None, cooldowns=None):
    bot = TradingBot.__new__(TradingBot)
    bot._init_runtime_state()
    profile = {
        "name": "trend_strong", "enabled": True,
        "strategy_type": "trend_signal", "pairs": list(active),
    }
    monkeypatch.setattr(bot, "_get_primary_profile_info",
                        lambda: ([profile], profile, True), raising=False)
    monkeypatch.setattr(bot, "_get_reserved_pairs", lambda *_a, **_k: set(), raising=False)
    monkeypatch.setattr(bot, "_filter_disabled_pairs", lambda lst: list(lst), raising=False)
    monkeypatch.setattr(bot, "_sync_strategy_profiles_with_trading_pairs",
                        lambda **_k: None, raising=False)
    pos_map = {f"{s}_LONG": {"symbol": s} for s in positions}
    monkeypatch.setattr(bot, "_get_known_position", lambda key: pos_map.get(key), raising=False)
    monkeypatch.setattr(bot, "sort_binance_coins_by_score",
                        lambda num_coins, exclude=None: list(ranked or []), raising=False)
    bot._regime_committed = dict(committed)
    bot._regime_swap_cooldowns = dict(cooldowns or {})
    bot.exchange = SimpleNamespace(set_leverage=lambda *_a, **_k: True)
    bot.binance_strategy = {"coins": list(active)}
    monkeypatch.setattr(config, "TRADING_PAIRS", list(active))
    monkeypatch.setattr(config, "REGIME_SWAP_ENABLED", True)
    monkeypatch.setattr(config, "USE_BINANCE_STRATEGY", True)
    monkeypatch.setattr(config, "REGIME_SWAP_COOLDOWN_MINUTES", 30.0)
    return bot, profile


def test_swaps_idle_non_trend_keeps_trend(monkeypatch):
    bot, profile = _make_bot(
        monkeypatch,
        active=["SOLUSDT", "ETHUSDT", "DOGEUSDT"],
        committed={"SOLUSDT": "trend", "ETHUSDT": "squeeze", "DOGEUSDT": "trend"},
        ranked=["BNBUSDT", "LINKUSDT"],
    )
    res = bot._maybe_swap_non_trend_pairs()
    assert res is not None
    assert res["evicted"] == ["ETHUSDT"]
    assert res["added"] == ["BNBUSDT"]
    # SOL/DOGE (trend) ficam; ETH (squeeze) sai; BNB entra.
    assert set(config.TRADING_PAIRS) == {"SOLUSDT", "DOGEUSDT", "BNBUSDT"}
    assert "ETHUSDT" not in config.TRADING_PAIRS


def test_keeps_non_trend_pair_with_open_position(monkeypatch):
    bot, _ = _make_bot(
        monkeypatch,
        active=["SOLUSDT", "ETHUSDT"],
        committed={"SOLUSDT": "trend", "ETHUSDT": "squeeze"},
        positions=("ETHUSDT",),  # ETH squeeze MAS com posição aberta
        ranked=["BNBUSDT"],
    )
    res = bot._maybe_swap_non_trend_pairs()
    assert res is None  # não despeja par com posição
    assert "ETHUSDT" in config.TRADING_PAIRS


def test_cooldown_blocks_reswap(monkeypatch):
    import time
    bot, _ = _make_bot(
        monkeypatch,
        active=["SOLUSDT", "ETHUSDT"],
        committed={"SOLUSDT": "trend", "ETHUSDT": "squeeze"},
        ranked=["BNBUSDT"],
        cooldowns={"ETHUSDT": time.time()},  # ETH trocado há pouco
    )
    res = bot._maybe_swap_non_trend_pairs()
    assert res is None  # em cooldown → não troca
    assert "ETHUSDT" in config.TRADING_PAIRS


def test_disabled_flag_is_noop(monkeypatch):
    bot, _ = _make_bot(
        monkeypatch,
        active=["SOLUSDT", "ETHUSDT"],
        committed={"SOLUSDT": "trend", "ETHUSDT": "squeeze"},
        ranked=["BNBUSDT"],
    )
    monkeypatch.setattr(config, "REGIME_SWAP_ENABLED", False)
    assert bot._maybe_swap_non_trend_pairs() is None
    assert set(config.TRADING_PAIRS) == {"SOLUSDT", "ETHUSDT"}


def test_no_candidate_keeps_non_trend(monkeypatch):
    bot, _ = _make_bot(
        monkeypatch,
        active=["SOLUSDT", "ETHUSDT"],
        committed={"SOLUSDT": "squeeze", "ETHUSDT": "squeeze"},
        ranked=[],  # universo sem candidato disponível
    )
    res = bot._maybe_swap_non_trend_pairs()
    assert res is None
    assert set(config.TRADING_PAIRS) == {"SOLUSDT", "ETHUSDT"}  # mantém


def test_fresh_pair_without_commit_not_evicted(monkeypatch):
    # Par recém-entrado (sem regime comitado ainda) NÃO é despejado.
    bot, _ = _make_bot(
        monkeypatch,
        active=["SOLUSDT", "NEWUSDT"],
        committed={"SOLUSDT": "trend"},  # NEWUSDT sem commit
        ranked=["BNBUSDT"],
    )
    res = bot._maybe_swap_non_trend_pairs()
    assert res is None
    assert "NEWUSDT" in config.TRADING_PAIRS

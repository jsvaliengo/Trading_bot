"""
Testes da Fase 2: Classificador de Regime de Mercado (ADX + BBW).

Cobre:
- TechnicalAnalysis.calculate_adx: dados insuficientes → 0; tendência forte
  → ADX alto; mercado lateral → ADX baixo.
- TechnicalAnalysis.calculate_bb_width_percent: cálculo correto; preço zerado.
- TechnicalAnalysis.classify_regime: regras (trend / range / squeeze / neutral).
- TradingBot._update_regime_history: hysteresis (3 ticks); neutral não comita;
  troca limpa de regime após histórico estável.
- TradingBot._apply_regime_override: substitui strategy_type + engine.
"""

import numpy as np

from trading_bot.core.bot import TradingBot
from trading_bot.core.config import config
from trading_bot.core.strategy import (
    HedgeStrategy,
    RangeScalpingStrategy,
    TechnicalAnalysis,
)


# ---------- Geradores de séries sintéticas ----------


def _trending_series(n: int = 60, start: float = 100.0, slope: float = 1.0) -> tuple:
    """Série fortemente trending — ADX deve ser alto."""
    closes = [start + i * slope for i in range(n)]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    return highs, lows, closes


def _ranging_series(n: int = 60, mid: float = 100.0, amp: float = 1.0) -> tuple:
    """Série lateral com leve oscilação — ADX deve ser baixo."""
    np.random.seed(42)
    closes = [mid + np.random.uniform(-amp, amp) for _ in range(n)]
    highs = [c + 0.3 for c in closes]
    lows = [c - 0.3 for c in closes]
    return highs, lows, closes


def _squeeze_series(n: int = 60, mid: float = 100.0) -> tuple:
    """Série lateral MUITO comprimida — BBW deve estar abaixo do threshold."""
    np.random.seed(7)
    closes = [mid + np.random.uniform(-0.05, 0.05) for _ in range(n)]
    highs = [c + 0.02 for c in closes]
    lows = [c - 0.02 for c in closes]
    return highs, lows, closes


# ---------- calculate_adx ----------


def test_adx_returns_zero_when_data_insufficient():
    assert TechnicalAnalysis.calculate_adx([1.0], [0.9], [0.95]) == 0.0
    # period=14 precisa de pelo menos 29 candles
    short_series = [float(i) for i in range(20)]
    assert TechnicalAnalysis.calculate_adx(short_series, short_series, short_series) == 0.0


def test_adx_is_high_on_strong_uptrend():
    highs, lows, closes = _trending_series(n=60, slope=1.0)
    adx = TechnicalAnalysis.calculate_adx(highs, lows, closes)
    assert adx > 40, f"ADX em uptrend forte deveria ser alto, got {adx}"


def test_adx_is_low_on_ranging_market():
    highs, lows, closes = _ranging_series(n=60, amp=1.0)
    adx = TechnicalAnalysis.calculate_adx(highs, lows, closes)
    assert adx < 25, f"ADX em range deveria ser baixo, got {adx}"


# ---------- calculate_bb_width_percent ----------


def test_bb_width_percent_basic():
    # Série com volatilidade conhecida
    prices = [100.0] * 19 + [105.0]
    bbw = TechnicalAnalysis.calculate_bb_width_percent(prices)
    # std > 0, width% > 0
    assert bbw > 0


def test_bb_width_percent_zero_when_middle_zero():
    # Sanity guard (não deveria acontecer em produção mas testa o early return)
    assert TechnicalAnalysis.calculate_bb_width_percent([0.0] * 25) == 0.0


def test_bb_width_percent_compressed_on_flat_series():
    """Série quase flat → BBW próximo de zero (squeeze)."""
    np.random.seed(0)
    prices = [100.0 + np.random.uniform(-0.01, 0.01) for _ in range(30)]
    bbw = TechnicalAnalysis.calculate_bb_width_percent(prices)
    assert bbw < 0.5, f"BBW de série flat deveria ser <0.5%, got {bbw}"


# ---------- classify_regime ----------


def test_classify_regime_returns_trend_on_strong_uptrend(monkeypatch):
    monkeypatch.setattr(config, "REGIME_ADX_TREND_THRESHOLD", 25.0)
    monkeypatch.setattr(config, "REGIME_ADX_RANGE_THRESHOLD", 20.0)
    monkeypatch.setattr(config, "REGIME_BBW_SQUEEZE_PERCENT", 4.0)

    highs, lows, closes = _trending_series(n=60, slope=1.0)
    result = TechnicalAnalysis.classify_regime(highs, lows, closes)
    assert result["regime"] == "trend"
    assert result["adx"] > 25


def test_classify_regime_returns_range_on_lateral_with_normal_volatility(monkeypatch):
    monkeypatch.setattr(config, "REGIME_ADX_TREND_THRESHOLD", 25.0)
    monkeypatch.setattr(config, "REGIME_ADX_RANGE_THRESHOLD", 20.0)
    monkeypatch.setattr(config, "REGIME_BBW_SQUEEZE_PERCENT", 0.5)

    highs, lows, closes = _ranging_series(n=60, amp=1.0)
    result = TechnicalAnalysis.classify_regime(highs, lows, closes)
    # Em range com amp=1% e squeeze_thr=0.5%, BBW fica acima e classifica range
    assert result["regime"] == "range"
    assert result["adx"] < 20


def test_classify_regime_returns_squeeze_on_compressed_lateral(monkeypatch):
    monkeypatch.setattr(config, "REGIME_ADX_TREND_THRESHOLD", 25.0)
    monkeypatch.setattr(config, "REGIME_ADX_RANGE_THRESHOLD", 20.0)
    monkeypatch.setattr(config, "REGIME_BBW_SQUEEZE_PERCENT", 4.0)

    highs, lows, closes = _squeeze_series(n=60)
    result = TechnicalAnalysis.classify_regime(highs, lows, closes)
    assert result["regime"] == "squeeze"
    assert result["bbw_percent"] < 4.0


def test_classify_regime_returns_neutral_on_insufficient_data():
    result = TechnicalAnalysis.classify_regime([1.0], [0.9], [0.95])
    assert result["regime"] == "neutral"


# ---------- TradingBot._update_regime_history (hysteresis) ----------


def _make_bot() -> TradingBot:
    bot = TradingBot.__new__(TradingBot)
    bot._init_runtime_state()
    return bot


def test_regime_hysteresis_requires_n_consecutive_observations(monkeypatch):
    monkeypatch.setattr(config, "REGIME_HYSTERESIS_TICKS", 3)
    bot = _make_bot()

    # Primeira observação: ainda não comita
    assert bot._update_regime_history("BTCUSDT", "trend") is None
    # Segunda: ainda não
    assert bot._update_regime_history("BTCUSDT", "trend") is None
    # Terceira: comita
    assert bot._update_regime_history("BTCUSDT", "trend") == "trend"


def test_regime_hysteresis_breaks_on_mixed_observations(monkeypatch):
    monkeypatch.setattr(config, "REGIME_HYSTERESIS_TICKS", 3)
    bot = _make_bot()

    bot._update_regime_history("BTCUSDT", "trend")
    bot._update_regime_history("BTCUSDT", "trend")
    # Quebra com range — apenas as últimas N observações iguais contam.
    bot._update_regime_history("BTCUSDT", "range")
    # Agora a janela tem [trend, trend, range] — não comita ainda.
    assert bot._regime_committed.get("BTCUSDT") is None


def test_regime_neutral_does_not_change_committed(monkeypatch):
    """Observações 'neutral' não votam — preservam status quo."""
    monkeypatch.setattr(config, "REGIME_HYSTERESIS_TICKS", 3)
    bot = _make_bot()

    # Comita trend após 3 ticks
    for _ in range(3):
        bot._update_regime_history("BTCUSDT", "trend")
    assert bot._regime_committed["BTCUSDT"] == "trend"

    # Várias observações neutrais não trocam o regime
    for _ in range(5):
        result = bot._update_regime_history("BTCUSDT", "neutral")
        assert result == "trend"
    assert bot._regime_committed["BTCUSDT"] == "trend"


def test_regime_switches_after_full_window_of_new_regime(monkeypatch):
    monkeypatch.setattr(config, "REGIME_HYSTERESIS_TICKS", 3)
    bot = _make_bot()

    for _ in range(3):
        bot._update_regime_history("BTCUSDT", "trend")
    assert bot._regime_committed["BTCUSDT"] == "trend"

    # Precisa de 3 ranges seguidos pra trocar — não 2.
    bot._update_regime_history("BTCUSDT", "range")
    assert bot._regime_committed["BTCUSDT"] == "trend"
    bot._update_regime_history("BTCUSDT", "range")
    assert bot._regime_committed["BTCUSDT"] == "trend"
    bot._update_regime_history("BTCUSDT", "range")
    assert bot._regime_committed["BTCUSDT"] == "range"


# ---------- _apply_regime_override ----------


def test_apply_regime_override_swaps_strategy_type_and_engine():
    bot = _make_bot()
    base_profile = {
        "name": "trend_strong",
        "strategy_type": "trend_signal",
        "entry_mode": "strong_only",
        "pairs": ["BTCUSDT"],
        "strategy": HedgeStrategy(),
    }

    overridden = bot._apply_regime_override(base_profile, "range")
    assert overridden["strategy_type"] == "range_scalping"
    assert isinstance(overridden["strategy"], RangeScalpingStrategy)
    assert overridden["regime_override_applied"] == "range"


def test_apply_regime_override_no_op_when_already_matches():
    bot = _make_bot()
    base_profile = {
        "name": "trend_strong",
        "strategy_type": "trend_signal",
        "entry_mode": "strong_only",
        "pairs": ["BTCUSDT"],
        "strategy": HedgeStrategy(),
    }

    overridden = bot._apply_regime_override(base_profile, "trend")
    # Já é trend_signal — não muda nada
    assert overridden is base_profile
    assert "regime_override_applied" not in overridden


def test_apply_regime_override_no_op_when_regime_is_none_or_neutral():
    bot = _make_bot()
    base = {"strategy_type": "trend_signal", "strategy": HedgeStrategy()}
    assert bot._apply_regime_override(base, None) is base
    assert bot._apply_regime_override(base, "squeeze") is base  # squeeze não força override
    assert bot._apply_regime_override(base, "neutral") is base


def test_regime_change_emits_to_dashboard_server(monkeypatch):
    """Quando hysteresis comita um regime NOVO, dashboard_server.emit_regime_changed é chamado."""
    from unittest.mock import MagicMock

    monkeypatch.setattr(config, "REGIME_HYSTERESIS_TICKS", 3)
    bot = _make_bot()
    bot.dashboard_server = MagicMock()

    # 3 ticks consecutivos comitam o regime — emit deve disparar 1x
    for _ in range(3):
        bot._update_regime_history("BTCUSDT", "trend")
    bot.dashboard_server.emit_regime_changed.assert_called_once()
    payload = bot.dashboard_server.emit_regime_changed.call_args[0][0]
    assert payload["symbol"] == "BTCUSDT"
    assert payload["regime"] == "trend"
    assert payload["previous"] is None

    # Mais 3 ticks IGUAIS — não emite de novo (regime não mudou)
    bot.dashboard_server.emit_regime_changed.reset_mock()
    for _ in range(3):
        bot._update_regime_history("BTCUSDT", "trend")
    bot.dashboard_server.emit_regime_changed.assert_not_called()

    # 3 ticks de range — emite mudança trend → range
    for _ in range(3):
        bot._update_regime_history("BTCUSDT", "range")
    bot.dashboard_server.emit_regime_changed.assert_called_once()
    payload = bot.dashboard_server.emit_regime_changed.call_args[0][0]
    assert payload["regime"] == "range"
    assert payload["previous"] == "trend"


def test_regime_change_no_emit_when_dashboard_disabled(monkeypatch):
    """Sem dashboard_server, hysteresis não falha — só atualiza estado."""
    monkeypatch.setattr(config, "REGIME_HYSTERESIS_TICKS", 3)
    bot = _make_bot()
    bot.dashboard_server = None  # explicit

    for _ in range(3):
        bot._update_regime_history("BTCUSDT", "trend")
    assert bot._regime_committed["BTCUSDT"] == "trend"  # estado atualizou


def test_regime_engine_cache_reuses_instance():
    bot = _make_bot()
    e1 = bot._get_or_create_regime_engine("range_scalping")
    e2 = bot._get_or_create_regime_engine("range_scalping")
    assert e1 is e2
    assert isinstance(e1, RangeScalpingStrategy)

    e3 = bot._get_or_create_regime_engine("trend_signal")
    assert isinstance(e3, HedgeStrategy)
    assert e3 is not e1

"""
Testes da Fase 1: DCA e Trailing Stop ancorados em ATR.

Cobre:
- calculate_dca_levels: modo ATR (BASE + (i-1)*INCREMENT) com clamp em
  [MIN_STEP, MAX_STEP], e fallback legado quando ATR é None / USE_ATR_DCA=False.
- compute_atr_based_trailing: cálculo, clamp em [MIN_PERCENT, MAX_PERCENT],
  invariante activation >= distance + 0.15% fee_floor, retorna None quando
  desabilitado ou ATR inválido.
"""

import pytest

from trading_bot.core.config import config
from trading_bot.core.strategy import HedgeStrategy, Signal, TechnicalAnalysis


# ---------- calculate_dca_levels (ATR mode) ----------


def _make_strategy() -> HedgeStrategy:
    return HedgeStrategy()


def test_dca_levels_atr_mode_uses_progressive_multipliers(monkeypatch):
    """LONG, ATR=1% do preço: níveis em 1.5%, 2.5%, 3.5%."""
    monkeypatch.setattr(config, "USE_ATR_DCA", True)
    monkeypatch.setattr(config, "DCA_ATR_MULTIPLIER_BASE", 1.5)
    monkeypatch.setattr(config, "DCA_ATR_STEP_INCREMENT", 1.0)
    monkeypatch.setattr(config, "DCA_ATR_MIN_STEP_PERCENT", 0.5)
    monkeypatch.setattr(config, "DCA_ATR_MAX_STEP_PERCENT", 8.0)
    monkeypatch.setattr(config, "DCA_ENABLED", True)
    monkeypatch.setattr(config, "DCA_MAX_ORDERS", 3)
    monkeypatch.setattr(config, "DCA_MULTIPLIER", 1.5)

    levels = _make_strategy().calculate_dca_levels(
        entry_price=100.0, signal=Signal.STRONG_BUY, atr=1.0
    )

    assert len(levels) == 3
    # 1.5% → 98.5, 2.5% → 97.5, 3.5% → 96.5
    assert levels[0]["price"] == pytest.approx(98.5, abs=0.01)
    assert levels[1]["price"] == pytest.approx(97.5, abs=0.01)
    assert levels[2]["price"] == pytest.approx(96.5, abs=0.01)
    # Todos LONG
    assert all(lvl["position_side"] == "LONG" for lvl in levels)


def test_dca_levels_atr_mode_short_uses_upper_prices(monkeypatch):
    """SHORT espelha: DCA em preços ACIMA do entry."""
    monkeypatch.setattr(config, "USE_ATR_DCA", True)
    monkeypatch.setattr(config, "DCA_ATR_MULTIPLIER_BASE", 1.5)
    monkeypatch.setattr(config, "DCA_ATR_STEP_INCREMENT", 1.0)
    monkeypatch.setattr(config, "DCA_ATR_MIN_STEP_PERCENT", 0.5)
    monkeypatch.setattr(config, "DCA_ATR_MAX_STEP_PERCENT", 8.0)
    monkeypatch.setattr(config, "DCA_ENABLED", True)
    monkeypatch.setattr(config, "DCA_MAX_ORDERS", 3)
    monkeypatch.setattr(config, "DCA_MULTIPLIER", 1.5)

    levels = _make_strategy().calculate_dca_levels(
        entry_price=100.0, signal=Signal.STRONG_SELL, atr=1.0
    )

    assert all(lvl["position_side"] == "SHORT" for lvl in levels)
    assert levels[0]["price"] == pytest.approx(101.5, abs=0.01)
    assert levels[2]["price"] == pytest.approx(103.5, abs=0.01)


def test_dca_levels_atr_clamps_high_volatility_to_max(monkeypatch):
    """ATR muito alto (10% do preço) deve ser limitado a MAX_STEP_PERCENT."""
    monkeypatch.setattr(config, "USE_ATR_DCA", True)
    monkeypatch.setattr(config, "DCA_ATR_MULTIPLIER_BASE", 1.5)
    monkeypatch.setattr(config, "DCA_ATR_STEP_INCREMENT", 1.0)
    monkeypatch.setattr(config, "DCA_ATR_MIN_STEP_PERCENT", 0.5)
    monkeypatch.setattr(config, "DCA_ATR_MAX_STEP_PERCENT", 8.0)
    monkeypatch.setattr(config, "DCA_ENABLED", True)
    monkeypatch.setattr(config, "DCA_MAX_ORDERS", 3)
    monkeypatch.setattr(config, "DCA_MULTIPLIER", 1.5)

    # ATR = 10 (10% do preço de 100) × 1.5 = 15% → clamp em 8%
    levels = _make_strategy().calculate_dca_levels(
        entry_price=100.0, signal=Signal.STRONG_BUY, atr=10.0
    )

    # Todos os níveis batem o teto: 100 * (1 - 0.08) = 92.0
    assert levels[0]["price"] == pytest.approx(92.0, abs=0.01)
    assert levels[1]["price"] == pytest.approx(92.0, abs=0.01)
    assert levels[2]["price"] == pytest.approx(92.0, abs=0.01)


def test_dca_levels_atr_clamps_low_volatility_to_min(monkeypatch):
    """ATR muito baixo deve elevar para MIN_STEP_PERCENT."""
    monkeypatch.setattr(config, "USE_ATR_DCA", True)
    monkeypatch.setattr(config, "DCA_ATR_MULTIPLIER_BASE", 1.5)
    monkeypatch.setattr(config, "DCA_ATR_STEP_INCREMENT", 1.0)
    monkeypatch.setattr(config, "DCA_ATR_MIN_STEP_PERCENT", 0.5)
    monkeypatch.setattr(config, "DCA_ATR_MAX_STEP_PERCENT", 8.0)
    monkeypatch.setattr(config, "DCA_ENABLED", True)
    monkeypatch.setattr(config, "DCA_MAX_ORDERS", 3)
    monkeypatch.setattr(config, "DCA_MULTIPLIER", 1.5)

    # ATR = 0.1 (0.1% do preço de 100) × 1.5 = 0.15% → clamp em 0.5%
    levels = _make_strategy().calculate_dca_levels(
        entry_price=100.0, signal=Signal.STRONG_BUY, atr=0.1
    )

    # Nível 1: clampado ao mínimo 0.5%; Nível 2: 0.25% × 1.5 = ainda abaixo, clamp; etc.
    assert levels[0]["price"] == pytest.approx(99.5, abs=0.01)
    # Nível 2: 2.5 × 0.1% = 0.25% < 0.5%, ainda clampa
    assert levels[1]["price"] == pytest.approx(99.5, abs=0.01)


def test_dca_levels_falls_back_to_legacy_when_atr_disabled(monkeypatch):
    """USE_ATR_DCA=False: cai pro DCA_STEP_PERCENT * i (modo legado)."""
    monkeypatch.setattr(config, "USE_ATR_DCA", False)
    monkeypatch.setattr(config, "DCA_STEP_PERCENT", 2.0)
    monkeypatch.setattr(config, "DCA_ENABLED", True)
    monkeypatch.setattr(config, "DCA_MAX_ORDERS", 3)
    monkeypatch.setattr(config, "DCA_MULTIPLIER", 1.5)

    levels = _make_strategy().calculate_dca_levels(
        entry_price=100.0, signal=Signal.STRONG_BUY, atr=1.0
    )

    # 2%, 4%, 6% (linear) — não 1.5, 2.5, 3.5
    assert levels[0]["price"] == pytest.approx(98.0, abs=0.01)
    assert levels[1]["price"] == pytest.approx(96.0, abs=0.01)
    assert levels[2]["price"] == pytest.approx(94.0, abs=0.01)


def test_dca_levels_falls_back_when_atr_is_none(monkeypatch):
    """ATR=None com USE_ATR_DCA=True ainda usa modo legado por segurança."""
    monkeypatch.setattr(config, "USE_ATR_DCA", True)
    monkeypatch.setattr(config, "DCA_STEP_PERCENT", 2.0)
    monkeypatch.setattr(config, "DCA_ENABLED", True)
    monkeypatch.setattr(config, "DCA_MAX_ORDERS", 3)
    monkeypatch.setattr(config, "DCA_MULTIPLIER", 1.5)

    levels = _make_strategy().calculate_dca_levels(
        entry_price=100.0, signal=Signal.STRONG_BUY, atr=None
    )

    assert levels[0]["price"] == pytest.approx(98.0, abs=0.01)


def test_dca_levels_returns_empty_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "DCA_ENABLED", False)
    assert _make_strategy().calculate_dca_levels(100.0, Signal.STRONG_BUY, atr=1.0) == []


# ---------- compute_atr_based_trailing ----------


def test_trailing_atr_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(config, "USE_ATR_TRAILING", False)
    assert TechnicalAnalysis.compute_atr_based_trailing(100.0, 1.0) is None


def test_trailing_atr_invalid_atr_returns_none(monkeypatch):
    monkeypatch.setattr(config, "USE_ATR_TRAILING", True)
    assert TechnicalAnalysis.compute_atr_based_trailing(100.0, 0) is None
    assert TechnicalAnalysis.compute_atr_based_trailing(100.0, None) is None
    assert TechnicalAnalysis.compute_atr_based_trailing(0, 1.0) is None


def test_trailing_atr_normal_volatility(monkeypatch):
    """ATR=1% do preço, mults default (act=2.0, dist=1.0): act=2%, dist=1%."""
    monkeypatch.setattr(config, "USE_ATR_TRAILING", True)
    monkeypatch.setattr(config, "TRAILING_ACTIVATION_ATR_MULT", 2.0)
    monkeypatch.setattr(config, "TRAILING_DISTANCE_ATR_MULT", 1.0)
    monkeypatch.setattr(config, "TRAILING_ACTIVATION_MIN_PERCENT", 0.40)
    monkeypatch.setattr(config, "TRAILING_ACTIVATION_MAX_PERCENT", 2.50)
    monkeypatch.setattr(config, "TRAILING_DISTANCE_MIN_PERCENT", 0.20)
    monkeypatch.setattr(config, "TRAILING_DISTANCE_MAX_PERCENT", 1.50)

    result = TechnicalAnalysis.compute_atr_based_trailing(100.0, 1.0)

    assert result is not None
    activation, distance = result
    assert activation == pytest.approx(2.0, abs=0.001)
    assert distance == pytest.approx(1.0, abs=0.001)


def test_trailing_atr_clamps_high_volatility_to_max(monkeypatch):
    """ATR=5% → act=10%, dist=5% → clamp em 2.5% / 1.5%."""
    monkeypatch.setattr(config, "USE_ATR_TRAILING", True)
    monkeypatch.setattr(config, "TRAILING_ACTIVATION_ATR_MULT", 2.0)
    monkeypatch.setattr(config, "TRAILING_DISTANCE_ATR_MULT", 1.0)
    monkeypatch.setattr(config, "TRAILING_ACTIVATION_MIN_PERCENT", 0.40)
    monkeypatch.setattr(config, "TRAILING_ACTIVATION_MAX_PERCENT", 2.50)
    monkeypatch.setattr(config, "TRAILING_DISTANCE_MIN_PERCENT", 0.20)
    monkeypatch.setattr(config, "TRAILING_DISTANCE_MAX_PERCENT", 1.50)

    activation, distance = TechnicalAnalysis.compute_atr_based_trailing(100.0, 5.0)

    assert activation == pytest.approx(2.5, abs=0.001)
    assert distance == pytest.approx(1.5, abs=0.001)


def test_trailing_atr_clamps_low_volatility_to_min(monkeypatch):
    """ATR=0.05% → act=0.1%, dist=0.05% → clamp em 0.40% / 0.20%."""
    monkeypatch.setattr(config, "USE_ATR_TRAILING", True)
    monkeypatch.setattr(config, "TRAILING_ACTIVATION_ATR_MULT", 2.0)
    monkeypatch.setattr(config, "TRAILING_DISTANCE_ATR_MULT", 1.0)
    monkeypatch.setattr(config, "TRAILING_ACTIVATION_MIN_PERCENT", 0.40)
    monkeypatch.setattr(config, "TRAILING_ACTIVATION_MAX_PERCENT", 2.50)
    monkeypatch.setattr(config, "TRAILING_DISTANCE_MIN_PERCENT", 0.20)
    monkeypatch.setattr(config, "TRAILING_DISTANCE_MAX_PERCENT", 1.50)

    activation, distance = TechnicalAnalysis.compute_atr_based_trailing(100.0, 0.05)

    assert activation == pytest.approx(0.40, abs=0.001)
    assert distance == pytest.approx(0.20, abs=0.001)


def test_trailing_atr_enforces_breakeven_invariant(monkeypatch):
    """
    Bounds em que activation < distance + 0.15%: deve PUXAR activation pra cima.

    Cenário: bounds desbalanceados (act_max=0.5, dist_min=0.4) com ATR pequeno.
    Sem o enforcement, activation=0.4 e distance=0.4 violam o invariante.
    """
    monkeypatch.setattr(config, "USE_ATR_TRAILING", True)
    monkeypatch.setattr(config, "TRAILING_ACTIVATION_ATR_MULT", 2.0)
    monkeypatch.setattr(config, "TRAILING_DISTANCE_ATR_MULT", 2.0)
    monkeypatch.setattr(config, "TRAILING_ACTIVATION_MIN_PERCENT", 0.40)
    monkeypatch.setattr(config, "TRAILING_ACTIVATION_MAX_PERCENT", 2.0)
    monkeypatch.setattr(config, "TRAILING_DISTANCE_MIN_PERCENT", 0.20)
    monkeypatch.setattr(config, "TRAILING_DISTANCE_MAX_PERCENT", 1.0)

    # ATR=0.1% → act=0.2% → clamp em 0.40%
    #        → dist=0.2% → clamp em 0.20%
    # activation (0.40) ≥ distance (0.20) + 0.15% = 0.35% ✓ (já cumpre)
    activation, distance = TechnicalAnalysis.compute_atr_based_trailing(100.0, 0.1)
    assert activation >= distance + 0.15 - 0.001  # margem de float

    # Cenário pior: ATR=0.5%, dist sobe pra 1.0%, act ficaria em 1.0% (= dist) — viola.
    activation, distance = TechnicalAnalysis.compute_atr_based_trailing(100.0, 0.5)
    assert activation >= distance + 0.15 - 0.001


def test_trailing_atr_uses_default_config_values():
    """Sanity: com config default do projeto, valores devem estar nos bounds."""
    activation, distance = TechnicalAnalysis.compute_atr_based_trailing(100.0, 0.5)
    assert 0.40 <= activation <= 2.50
    assert 0.20 <= distance <= 1.50
    # Invariante de breakeven
    assert activation >= distance + 0.15 - 0.001

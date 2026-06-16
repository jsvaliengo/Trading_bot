"""
Testes da escala de num_coins por banca (PDF Estratégia Padrão) e do leverage.

13/06: user pediu que num_coins escale conforme o PDF (1 par em banca pequena
→ 12 acima de $9k) e leverage 20 (PDF). Antes num_coins era fixo em 6 e
leverage 10.
"""
from __future__ import annotations

from trading_bot.core.config import TradingConfig, config


def test_num_coins_scales_per_pdf():
    expected = {
        100: 1,     # PDF 90-150
        300: 3,     # PDF diz 2; subido p/ 3 (mais trades) em 13/06
        450: 3,     # PDF 500
        600: 3,     # PDF (500-1000)
        1200: 6,    # PDF 1000
        2000: 9,    # PDF 2000
        3000: 9,    # PDF 3000
        5000: 10,   # PDF 4000-6000
        8000: 11,   # PDF 6000-9000
        12000: 12,  # PDF 9000-15000
        50000: 12,  # teto
    }
    for capital, n in expected.items():
        got = config.get_binance_strategy_for_capital(capital)["num_coins"]
        assert got == n, f"capital ${capital}: esperado {n}, veio {got}"


def test_num_coins_is_monotonic_non_decreasing():
    tiers = config.BINANCE_STRATEGY_TIERS
    nums = [t[4] for t in tiers]
    assert nums == sorted(nums), f"num_coins deve ser não-decrescente: {nums}"


def test_leverage_default_is_20():
    c = TradingConfig()
    assert c.LEVERAGE == 20


def test_leverage_env_override_via_reload(monkeypatch):
    # LEVERAGE é default de campo (lido no import, como RISK_PER_TRADE_PCT etc.),
    # então o override só vale com a env setada ANTES do módulo carregar — que é
    # como o .env funciona em produção. Recarregar o módulo com a env setada
    # reproduz isso de forma rápida e determinística (sem abrir um subprocesso);
    # o finally restaura o default para não vazar para os outros testes.
    import importlib

    from trading_bot.core import config as config_module

    monkeypatch.setenv("TRADING_BOT_LEVERAGE", "15")
    try:
        reloaded = importlib.reload(config_module)
        assert reloaded.TradingConfig().LEVERAGE == 15
    finally:
        monkeypatch.delenv("TRADING_BOT_LEVERAGE", raising=False)
        importlib.reload(config_module)  # restaura o default (20) p/ os demais testes

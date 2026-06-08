"""Regressão: o card 'EVOLUÇÃO DA CARTEIRA' (Telegram) deve usar o realizado
ACUMULADO do TradeStore — a MESMA fonte do dashboard — e não o realizado do DIA.

Antes, send_portfolio_evolution usava daily_pnl_real (P&L do dia da Binance),
então o "Total"/"Atual" voltavam pro capital inicial toda virada de dia e
divergiam do dashboard (que usa cumulative_realized_pnl()). Veja bot.py
send_portfolio_evolution e web/data.py collect_summary.
"""

from types import SimpleNamespace

from trading_bot.core.bot import TradingBot
from trading_bot.core.config import config


class _ExchangeStub:
    def get_account_info(self):
        # wallet cappado (testnet) e unrealized das posições abertas
        return {"wallet_balance": 100.0, "unrealized_pnl": 0.46}

    def get_daily_pnl_from_binance(self):
        # P&L do DIA: hoje fechou no negativo (-2.04), mesmo com acumulado +
        return {"total": -2.04, "funding_fee": -0.01, "commission": -0.3}


class _TradeStoreStub:
    def __init__(self, cumulative):
        self._cumulative = cumulative

    def cumulative_realized_pnl(self):
        return self._cumulative


class _TelegramSpy:
    def __init__(self):
        self.kwargs = None

    def send_portfolio_evolution(self, **kwargs):
        self.kwargs = kwargs


def _make_fake_bot(cumulative_realized):
    telegram = _TelegramSpy()
    fake = SimpleNamespace(
        exchange=_ExchangeStub(),
        trade_store=_TradeStoreStub(cumulative_realized),
        telegram=telegram,
        daily_pnl_binance_baseline=0.0,
        initial_capital=100.0,
        total_pnl=0.0,  # fallback interno; não deve ser usado quando há store
        portfolio_history=[],
        closed_trades_count=79,
        trades_win_count=36,
        trades_loss_count=43,
        trades_win_total=18.98,
        trades_loss_total=-17.62,
        trades_by_strategy=None,
    )
    return fake, telegram


def test_portfolio_evolution_usa_realizado_acumulado(monkeypatch):
    # Cenário do bug reportado: acumulado +1.36, dia -2.04, aberto +0.46.
    # USE_TESTNET é property da classe; SIMULATED_BALANCE_USD é atributo da instância.
    monkeypatch.setattr(type(config), "USE_TESTNET", True, raising=False)
    monkeypatch.setattr(config, "SIMULATED_BALANCE_USD", 100.0, raising=False)

    fake, telegram = _make_fake_bot(cumulative_realized=1.36)
    TradingBot.send_portfolio_evolution(fake)

    kw = telegram.kwargs
    assert kw is not None, "deveria ter chamado telegram.send_portfolio_evolution"

    # "Realizado" = acumulado (não o -2.04 do dia)
    assert abs(kw["pnl_realized"] - 1.36) < 1e-9
    # "Total" = acumulado + aberto (igual ao dashboard P&L TOTAL)
    assert abs(kw["total_pnl"] - (1.36 + 0.46)) < 1e-9
    # "Atual" = cap + acumulado + aberto = 100 + 1.36 + 0.46 ≈ 101.82
    assert abs(kw["current_balance"] - (100.0 + 1.36 + 0.46)) < 1e-9


def test_portfolio_evolution_mainnet_usa_wallet(monkeypatch):
    # Sem SIMULATED_BALANCE: saldo = wallet + unrealized; total ainda acumulado.
    monkeypatch.setattr(type(config), "USE_TESTNET", False, raising=False)
    monkeypatch.setattr(config, "SIMULATED_BALANCE_USD", 0.0, raising=False)

    fake, telegram = _make_fake_bot(cumulative_realized=1.36)
    TradingBot.send_portfolio_evolution(fake)

    kw = telegram.kwargs
    assert abs(kw["pnl_realized"] - 1.36) < 1e-9
    assert abs(kw["total_pnl"] - (1.36 + 0.46)) < 1e-9
    # wallet (100) + unrealized (0.46)
    assert abs(kw["current_balance"] - (100.0 + 0.46)) < 1e-9

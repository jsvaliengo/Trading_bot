"""Regressão: o card '/status' (STATUS DO BOT) deve usar o realizado ACUMULADO
net do TradeStore — MESMA fonte do card EVOLUÇÃO (/portfolio) e do dashboard —
tanto no Saldo quanto no 'P&L Total Realizado'.

Antes, cmd_status usava o income DIÁRIO da Binance (com taxas/funding, sem
baseline), então o mesmo bot mostrava Saldo e Realizado diferentes entre /status
e /portfolio (ex.: STATUS -1.87 vs EVOLUÇÃO +1.91). Veja telegram_commands
cmd_status e bot.send_portfolio_evolution.
"""

from types import SimpleNamespace

from trading_bot.services.telegram_commands import TelegramCommandHandler


class _ExchangeStub:
    def get_account_info(self):
        return {"wallet_balance": 100.0, "unrealized_pnl": 0.46}

    def get_open_positions(self):
        return []

    def get_daily_pnl_from_binance(self):
        # Se for chamado, o teste falha de propósito: a fonte NÃO deve ser esta.
        raise AssertionError("cmd_status não deve usar o income diário da Binance")


def _fmt(amount, decimals=2, signed=False):
    # Marcador parseável p/ asserts (ignora o BRL real).
    return f"USD:{amount:.4f}"


def _make_handler(cumulative_realized, *, testnet=True, sim_cap=100.0):
    sent = []
    bot = SimpleNamespace(
        exchange=_ExchangeStub(),
        trade_store=SimpleNamespace(
            cumulative_realized_pnl=lambda: cumulative_realized
        ),
        running=True,
        paused=False,
        trades_win_count=4,
        trades_loss_count=3,
        sentiment_mode_enabled=False,
        invert_signals=False,
        total_pnl=999.0,  # fallback interno; não deve ser usado quando há store
    )
    config = SimpleNamespace(
        SIMULATED_BALANCE_USD=sim_cap,
        USE_TESTNET=testnet,
        LEVERAGE=10,
        TRADING_PAIRS=["A", "B", "C", "D", "E", "F"],
    )
    handler = SimpleNamespace(
        bot=bot,
        config=config,
        _format_usd_brl=_fmt,
        send_message=sent.append,
    )
    return handler, sent


def test_status_usa_realizado_acumulado_no_saldo_e_no_pnl():
    # Cenário do bug: acumulado +1.91, aberto +0.46.
    handler, sent = _make_handler(cumulative_realized=1.91)
    TelegramCommandHandler.cmd_status(handler, [])

    assert len(sent) == 1, f"esperava 1 mensagem, veio: {sent}"
    msg = sent[0]
    assert "❌" not in msg, f"cmd_status quebrou: {msg}"

    # Saldo = cap + acumulado + aberto = 100 + 1.91 + 0.46 = 102.37
    assert "USD:102.3700" in msg
    # P&L Total Realizado = acumulado net (não o income diário)
    assert "USD:1.9100" in msg


def test_status_saldo_bate_com_formula_da_evolucao():
    """Saldo do /status == fórmula do card EVOLUÇÃO (cap + acumulado + aberto)."""
    cumulative, unrealized, cap = 1.91, 0.46, 100.0
    handler, sent = _make_handler(cumulative_realized=cumulative, sim_cap=cap)
    TelegramCommandHandler.cmd_status(handler, [])

    expected_balance = cap + cumulative + unrealized
    assert f"USD:{expected_balance:.4f}" in sent[0]


def test_status_mainnet_usa_wallet():
    # Sem SIMULATED_BALANCE: saldo = wallet + unrealized; realizado ainda acumulado.
    handler, sent = _make_handler(cumulative_realized=1.91, testnet=False, sim_cap=0.0)
    TelegramCommandHandler.cmd_status(handler, [])

    msg = sent[0]
    assert "USD:100.4600" in msg  # wallet (100) + unrealized (0.46)
    assert "USD:1.9100" in msg    # realizado acumulado

"""exchange_label: nome da exchange (e rede) para rótulos de UI do dashboard/Telegram."""

from types import SimpleNamespace

import pytest

from trading_bot.core.config import exchange_badge, exchange_label


def test_exchange_label_by_exchange_and_network():
    bitget_demo = SimpleNamespace(EXCHANGE="bitget", BITGET_DEMO=True)
    bitget_prod = SimpleNamespace(EXCHANGE="bitget", BITGET_DEMO=False)
    okx = SimpleNamespace(EXCHANGE="okx", OKX_DEMO=True)
    binance = SimpleNamespace(EXCHANGE="binance", ENVIRONMENT="mainnet")
    assert exchange_label(bitget_demo) == "Bitget"
    assert exchange_label(bitget_demo, with_network=True) == "Bitget demo"
    assert exchange_label(bitget_prod, with_network=True) == "Bitget"
    assert exchange_label(okx, with_network=True) == "OKX demo"
    assert exchange_label(binance) == "Binance"
    assert exchange_label(binance, with_network=True) == "Binance mainnet"
    assert exchange_label(SimpleNamespace()) == "Binance"  # sem EXCHANGE (mocks) → default


@pytest.mark.parametrize(
    "cfg, expected",
    [
        (SimpleNamespace(EXCHANGE="bitget", BITGET_DEMO=True, ENVIRONMENT="testnet"), "🧪 <b>[Bitget demo]</b>"),
        (SimpleNamespace(EXCHANGE="bitget", BITGET_DEMO=False, ENVIRONMENT="testnet"), "🌐 <b>[Bitget]</b>"),
        (SimpleNamespace(EXCHANGE="binance", ENVIRONMENT="testnet"), "🧪 <b>[Binance testnet]</b>"),
        (SimpleNamespace(EXCHANGE="binance", ENVIRONMENT="mainnet"), "🌐 <b>[Binance mainnet]</b>"),
    ],
)
def test_exchange_badge(cfg, expected):
    assert exchange_badge(cfg) == expected


def test_telegram_prefix_and_reconcile_message_use_exchange_label(monkeypatch):
    """Na Bitget demo nada no Telegram pode dizer "Binance" nem "[TESTNET]"."""
    import trading_bot.core.bot as bot_mod
    from trading_bot.services.notifications import TelegramNotifier
    from trading_bot.services.telegram_commands import TelegramCommandHandler

    cfg = SimpleNamespace(EXCHANGE="bitget", BITGET_DEMO=True, ENVIRONMENT="testnet")
    monkeypatch.setattr(bot_mod, "config", cfg)
    monkeypatch.setattr("trading_bot.core.config.config", cfg)

    prefixed = TelegramNotifier._apply_env_prefix("oi")
    handler = TelegramCommandHandler.__new__(TelegramCommandHandler)
    handler.config = cfg
    assert prefixed == handler._apply_env_prefix("oi") == "🧪 <b>[Bitget demo]</b>\noi"

    sent = []
    bot = bot_mod.TradingBot.__new__(bot_mod.TradingBot)
    bot.telegram = SimpleNamespace(send_message=sent.append)
    monkeypatch.setattr(
        "trading_bot.services.pnl_reconciler.reconcile_recent_pnl",
        lambda *a, **k: {"details": [
            {"symbol": "BNBUSDT", "side": "LONG", "old": 0.0, "new": 4.05, "gross_fixed": True}
        ]},
    )
    bot.reconcile_recent_pnl()
    assert "divergia da Bitget" in sent[0]
    for text in (prefixed, sent[0]):
        assert "Binance" not in text and "TESTNET" not in text

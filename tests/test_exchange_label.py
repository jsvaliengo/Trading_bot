"""exchange_label: nome da exchange (e rede) para rótulos de UI do dashboard/Telegram."""

from types import SimpleNamespace

from trading_bot.core.config import exchange_label


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

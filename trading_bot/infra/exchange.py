"""Fábrica da conexão com a exchange: TRADING_BOT_EXCHANGE=binance (default) | okx."""

from ..core.config import config


def create_exchange():
    exchange = str(getattr(config, "EXCHANGE", "binance") or "binance").lower()
    if exchange == "okx":
        from .okx_client import OkxConnection
        return OkxConnection()
    from .binance_client import BinanceConnection
    return BinanceConnection()

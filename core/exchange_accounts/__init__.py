"""交易所账户适配器；按需加载，避免无关 SDK 阻塞独立模块测试。"""

__all__ = [
    "BaseExchangeAccount",
    "BinanceExchangeAccount",
    "GateExchangeAccount",
    "KucoinExchangeAccount",
    "OkxExchangeAccount",
]


def __getattr__(name):
    if name == "BaseExchangeAccount":
        from .base import BaseExchangeAccount

        return BaseExchangeAccount
    if name == "BinanceExchangeAccount":
        from .binance import BinanceExchangeAccount

        return BinanceExchangeAccount
    if name == "GateExchangeAccount":
        from .gate import GateExchangeAccount

        return GateExchangeAccount
    if name == "KucoinExchangeAccount":
        from .kucoin import KucoinExchangeAccount

        return KucoinExchangeAccount
    if name == "OkxExchangeAccount":
        from .okx import OkxExchangeAccount

        return OkxExchangeAccount
    raise AttributeError(name)

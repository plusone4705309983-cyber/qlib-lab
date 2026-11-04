"""券商适配器工厂。

当前默认使用 Alpaca Paper。后续可按 market/exchange 切换。
"""

from __future__ import annotations

from .alpaca import AlpacaBroker

_broker = AlpacaBroker()


def get_broker():
    return _broker

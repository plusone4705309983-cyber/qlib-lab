"""券商适配器工厂。

当前默认使用 Alpaca Paper。后续可按 market/exchange 切换。
"""

from __future__ import annotations

from .alpaca import AlpacaBroker


def get_broker(config_obj=None):
    """返回券商适配器；传入用户配置时使用该用户的密钥。"""
    return AlpacaBroker(config_obj)

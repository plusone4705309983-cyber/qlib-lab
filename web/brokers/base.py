"""券商抽象接口（最小可用集合）。

只定义 MVP 需要的方法，后续切换券商无需大改上层路由。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BrokerBase(ABC):
    @abstractmethod
    def get_account(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def get_positions(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def get_orders(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    def get_order(self, order_id: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def place_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, order_id: str) -> dict[str, Any]:
        raise NotImplementedError

"""Alpaca Paper REST 适配器。

仅使用 paper-api.alpaca.markets。用 requests 实现，零新增重型依赖。
"""

from __future__ import annotations

import json
from typing import Any

try:
    import requests
except Exception as exc:  # noqa: BLE001
    requests = None

from broker_config import BrokerConfigError, config


class AlpacaBroker:
    def __init__(self) -> None:
        self.config = config

    def _request(self, method: str, path: str, json_body: dict | None = None, params: dict | None = None) -> Any:
        if requests is None:
            raise RuntimeError("缺少 requests 模块，请在 venv 安装：pip install requests")
        self.config.require_paper()
        url = f"{self.config.base_url}{path}"
        try:
            resp = requests.request(
                method=method,
                url=url,
                headers=self.config.headers(),
                json=json_body,
                params=params,
                timeout=30,
            )
        except Exception as exc:
            raise RuntimeError(f"Alpaca 请求失败: {exc}") from exc

        if resp.status_code >= 400:
            text = resp.text
            try:
                data = resp.json()
                if isinstance(data, dict):
                    msg = data.get("message") or data.get("error") or json.dumps(data, ensure_ascii=False)
                else:
                    msg = json.dumps(data, ensure_ascii=False)
            except Exception:
                msg = text.strip()
            raise RuntimeError(f"Alpaca API {resp.status_code}: {msg}")

        if resp.text.strip() == "":
            return {}
        try:
            return resp.json()
        except Exception:
            return resp.text

    def get_account(self) -> dict[str, Any]:
        return self._request("GET", "/v2/account")

    def get_positions(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/v2/positions")
        return data if isinstance(data, list) else []

    def get_orders(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        params = {"limit": limit}
        if status:
            params["status"] = status
        data = self._request("GET", "/v2/orders", params=params)
        return data if isinstance(data, list) else []

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v2/orders/{order_id}")

    def place_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = dict(payload or {})
        body.setdefault("time_in_force", "day")
        return self._request("POST", "/v2/orders", json_body=body)

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        return self._request("DELETE", f"/v2/orders/{order_id}")

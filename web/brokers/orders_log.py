"""本地订单日志（仅用于 UI 展示，gitignore）。

不替代 Alpaca 订单历史，只记录「从本页发出的」预览/下单操作，便于调试。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PRIVATE_DIR = ROOT / "data" / "private"
LOG_FILE = PRIVATE_DIR / "broker_orders.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def log_order(event: dict) -> dict:
    PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(LOG_FILE.read_text(encoding="utf-8")) if LOG_FILE.exists() else {"orders": []}
        if not isinstance(data, dict):
            data = {"orders": []}
        if not isinstance(data.get("orders"), list):
            data["orders"] = []
    except Exception:
        data = {"orders": []}
    entry = {"ts": _now(), **event}
    data["orders"].insert(0, entry)
    data["orders"] = data["orders"][:200]
    tmp = LOG_FILE.with_name(LOG_FILE.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        import os

        os.replace(tmp, LOG_FILE)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
    return entry


def get_log(limit: int = 50) -> list[dict]:
    try:
        if not LOG_FILE.exists():
            return []
        data = json.loads(LOG_FILE.read_text(encoding="utf-8"))
        orders = data.get("orders") if isinstance(data, dict) else []
        return orders[:limit] if isinstance(orders, list) else []
    except Exception:
        return []

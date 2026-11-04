"""Alpaca Paper 配置加载（仅允许 paper 环境）。

密钥从 .env 读取：APCA_API_KEY_ID / APCA_API_SECRET_KEY / APCA_BASE_URL。
强制校验 base_url 必须是 https://paper-api.alpaca.markets，避免误连实盘。
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PAPER_BASE_URL = "https://paper-api.alpaca.markets"


def _load_env_file() -> None:
    """最小 env 加载（不依赖 python-dotenv，零新增依赖）。"""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        # env 加载失败不阻塞应用，后续接口会给出清晰报错
        return


_load_env_file()


class BrokerConfigError(RuntimeError):
    pass


class BrokerConfig:
    def __init__(self) -> None:
        self.key_id = os.environ.get("APCA_API_KEY_ID") or os.environ.get("ALPACA_API_KEY_ID") or ""
        self.secret_key = (
            os.environ.get("APCA_API_SECRET_KEY")
            or os.environ.get("ALPACA_API_SECRET_KEY")
            or ""
        )
        self.base_url = (
            os.environ.get("APCA_BASE_URL")
            or os.environ.get("ALPACA_BASE_URL")
            or PAPER_BASE_URL
        ).rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.key_id and self.secret_key)

    def require_paper(self) -> None:
        if self.base_url != PAPER_BASE_URL:
            raise BrokerConfigError(
                f"仅允许 Alpaca Paper：必须使用 {PAPER_BASE_URL}，当前 {self.base_url}"
            )

    def headers(self) -> dict:
        self.require_paper()
        if not self.configured:
            raise BrokerConfigError("缺少 Alpaca Paper 密钥：请在 .env 设置 APCA_API_KEY_ID / APCA_API_SECRET_KEY")
        return {
            "APCA-API-KEY-ID": self.key_id,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Content-Type": "application/json",
        }

    def summary(self) -> dict:
        return {
            "configured": self.configured,
            "base_url": self.base_url,
            "paper": self.base_url == PAPER_BASE_URL,
            "key_id_prefix": self.key_id[:6] + "..." if self.key_id else None,
        }


config = BrokerConfig()

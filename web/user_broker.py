"""按用户托管 Alpaca Paper 密钥（BYOK）。

密钥用 Fernet 加密后写入 SQLite 的 `user_broker` 表；主密钥优先读
`QLIB_MASTER_KEY`，没有时自动生成到 `data/private/master_key`（已 gitignore）。
接口只返回掩码，不返回完整 Key/Secret。
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

import broker_config
import db

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PRIVATE_DIR = ROOT / "data" / "private"
MASTER_KEY_FILE = PRIVATE_DIR / "master_key"

PAPER_BASE_URL = broker_config.PAPER_BASE_URL


class UserBrokerError(ValueError):
    """用户密钥缺失、格式错误或无法解密。"""


def _load_master_key() -> bytes:
    env_key = (os.environ.get("QLIB_MASTER_KEY") or "").strip()
    if env_key:
        return env_key.encode("ascii")
    if MASTER_KEY_FILE.exists():
        value = MASTER_KEY_FILE.read_text(encoding="ascii").strip()
        if value:
            return value.encode("ascii")
    PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
    value = Fernet.generate_key()
    MASTER_KEY_FILE.write_text(value.decode("ascii"), encoding="ascii")
    try:
        os.chmod(MASTER_KEY_FILE, 0o600)
    except OSError:
        pass
    return value


def _fernet() -> Fernet:
    try:
        return Fernet(_load_master_key())
    except Exception as exc:  # noqa: BLE001
        raise UserBrokerError(
            "QLIB_MASTER_KEY 格式不正确；应为 Fernet.generate_key() 生成的 44 字符密钥"
        ) from exc


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise UserBrokerError(
            "密钥无法解密：主密钥可能已更换。请在「设置」里重新填写 Alpaca 密钥。"
        ) from exc


def mask_key(key_id: str) -> str:
    key_id = key_id or ""
    if len(key_id) <= 10:
        return key_id[:2] + "…" if key_id else ""
    return f"{key_id[:6]}…{key_id[-4:]}"


def _paper_url(base_url: str | None) -> str:
    value = (base_url or PAPER_BASE_URL).strip().rstrip("/")
    if value != PAPER_BASE_URL:
        raise UserBrokerError(f"仅允许 Alpaca Paper：必须使用 {PAPER_BASE_URL}")
    return PAPER_BASE_URL


def save(
    user_id: int,
    key_id: str,
    secret_key: str,
    base_url: str | None = None,
) -> dict:
    key_id = (key_id or "").strip()
    secret_key = (secret_key or "").strip()
    if not key_id:
        raise UserBrokerError("请填写 Alpaca Key ID")
    if not secret_key:
        raise UserBrokerError("请填写 Alpaca Secret Key")
    paper_url = _paper_url(base_url)
    db.upsert_user_broker(
        int(user_id),
        encrypt(key_id),
        encrypt(secret_key),
        paper_url,
    )
    return status(user_id)


def status(user_id: int) -> dict:
    row = db.get_user_broker(int(user_id))
    if row is None:
        return {
            "configured": False,
            "paper": True,
            "base_url": PAPER_BASE_URL,
            "key_hint": None,
            "updated_at": None,
        }
    key_id = decrypt(row["api_key_enc"])
    secret_key = decrypt(row["secret_enc"])
    return {
        "configured": bool(key_id and secret_key),
        "paper": True,
        "base_url": row["base_url"],
        "key_hint": mask_key(key_id),
        "updated_at": row["updated_at"],
    }


def config_for_user(user_id: int) -> broker_config.BrokerConfig:
    row = db.get_user_broker(int(user_id))
    if row is None:
        raise UserBrokerError("未配置 Alpaca Paper 密钥，请到「设置」填写")
    key_id = decrypt(row["api_key_enc"])
    secret_key = decrypt(row["secret_enc"])
    if not key_id or not secret_key:
        raise UserBrokerError("Alpaca 密钥不完整，请到「设置」重新填写")
    return broker_config.BrokerConfig(
        key_id=key_id,
        secret_key=secret_key,
        base_url=row["base_url"],
    )


def delete(user_id: int) -> bool:
    return db.delete_user_broker(int(user_id))

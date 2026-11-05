"""安全审计日志：登录、邀请码、管理员操作、BYOK 和下单。

写入失败绝不能影响主流程，所以所有异常都在这里吞掉。
日志只记录动作和元数据，**不记录密码、Alpaca Secret、完整 Key**。
"""

from __future__ import annotations

import json

from flask import has_request_context, request
from flask_login import current_user

import db


def _request_meta() -> tuple[str | None, str | None]:
    if not has_request_context():
        return None, None
    ip = request.headers.get("CF-Connecting-IP") or request.headers.get("X-Forwarded-For") or request.remote_addr
    if ip and "," in ip:
        ip = ip.split(",", 1)[0].strip()
    ua = request.headers.get("User-Agent") or None
    return (ip[:100] if ip else None), (ua[:300] if ua else None)


def log(
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict | str | None = None,
    actor_user_id: int | None = None,
    actor_email: str | None = None,
) -> None:
    try:
        ip, user_agent = _request_meta()
        if has_request_context() and getattr(current_user, "is_authenticated", False):
            if actor_user_id is None:
                actor_user_id = getattr(current_user, "id", None)
            if actor_email is None:
                actor_email = getattr(current_user, "email", None)

        if isinstance(detail, str):
            detail_text = detail
        elif detail is None:
            detail_text = None
        else:
            detail_text = json.dumps(detail, ensure_ascii=False, separators=(",", ":"))

        db.add_audit(
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            detail=(detail_text[:2000] if detail_text else None),
            ip=ip,
            user_agent=user_agent,
        )
    except Exception:  # noqa: BLE001  审计失败不阻塞业务
        return

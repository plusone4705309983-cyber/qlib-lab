"""应用内身份与准入：Cloudflare Access Header -> users 表 -> Flask-Login。

信任边界（重要）：
- 只有 `QLIB_TRUST_CF_HEADER` 开启时才读取 `Cf-Access-Authenticated-User-Email`。
- 应用默认只监听 127.0.0.1，公网请求必须经 Cloudflare Tunnel；不要把端口直接暴露公网。
- 本地开发需同时设置 `QLIB_ALLOW_DEV_LOGIN=1` 与 `QLIB_DEV_EMAIL` 才能模拟身份；生产环境不要设置。
- `QLIB_AUTH_DISABLED=1` 仅供 selftest 使用，会以本地管理员身份放行所有请求。
"""

from __future__ import annotations

import os
import secrets
import time
from functools import wraps
from pathlib import Path

from flask import jsonify, redirect, render_template, request, session, url_for
from flask_login import LoginManager, UserMixin, current_user, login_user, logout_user

import db

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SECRET_FILE = ROOT / "data" / "private" / "session_secret"

LOGIN_TOUCH_SECONDS = 300
PUBLIC_ENDPOINTS = {
    "static",
    "healthz",
    "login_page",
    "invite_page",
    "redeem_invite",
    "logout",
}


# ---------------------------------------------------------------- 配置
def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "off", "")


def trust_cf_header() -> bool:
    return _env_bool("QLIB_TRUST_CF_HEADER", True)


def auth_disabled() -> bool:
    return _env_bool("QLIB_AUTH_DISABLED", False)


def dev_email() -> str:
    return db.normalize_email(os.environ.get("QLIB_DEV_EMAIL", ""))


def dev_login_allowed() -> bool:
    return _env_bool("QLIB_ALLOW_DEV_LOGIN", False)


def admin_emails() -> set[str]:
    raw = os.environ.get("QLIB_ADMIN_EMAIL", "")
    return {db.normalize_email(x) for x in raw.replace(";", ",").split(",") if x.strip()}


def load_secret_key() -> str:
    env = os.environ.get("QLIB_SECRET_KEY")
    if env:
        return env
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    if SECRET_FILE.exists():
        value = SECRET_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = secrets.token_hex(32)
    SECRET_FILE.write_text(value, encoding="utf-8")
    return value


# ---------------------------------------------------------------- 用户模型
class User(UserMixin):
    def __init__(self, row: dict):
        self._row = dict(row)
        self.id = int(row["id"])
        self.email = row["email"]
        self.role = row["role"]
        self.status = row["status"]

    @property
    def is_active(self) -> bool:  # Flask-Login 会据此拒绝禁用用户
        return self.status == "active"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def get_id(self) -> str:
        return str(self.id)


login_manager = LoginManager()
login_manager.login_view = "login_page"


@login_manager.user_loader
def _load_user(user_id: str):
    try:
        row = db.get_user(int(user_id))
    except (TypeError, ValueError):
        return None
    if row is None or row.get("status") != "active":
        return None
    return User(row)


@login_manager.unauthorized_handler
def _unauthorized():
    if request.path.startswith("/api/"):
        return jsonify({"error": "未登录", "login_url": url_for("login_page")}), 401
    return redirect(url_for("login_page"))


def init_app(app) -> None:
    app.secret_key = load_secret_key()
    app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
    app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
    login_manager.init_app(app)
    app.before_request(_gate)


# ---------------------------------------------------------------- 身份解析
def request_email() -> str:
    """按信任配置从请求中取邮箱。"""
    if trust_cf_header():
        email = request.headers.get("Cf-Access-Authenticated-User-Email", "")
        if email:
            return db.normalize_email(email)
    if dev_login_allowed():
        return dev_email()
    return ""


def _touch_login(user: User) -> None:
    now = time.time()
    last = float(session.get("_login_touched") or 0)
    if now - last > LOGIN_TOUCH_SECONDS:
        db.touch_login(user.id)
        session["_login_touched"] = now


def _bootstrap_admin(email: str) -> dict:
    """QLIB_ADMIN_EMAIL 中的邮箱首次出现时自动建管理员，并继承现有全局股票池。"""
    try:
        row = db.create_user(email, role="admin", status="active")
    except Exception:  # noqa: BLE001  并发首登时可能已被另一个请求创建
        row = db.get_user_by_email(email)
        if row is None:
            raise
    try:
        import view

        items = [{"market": s["market"], "symbol": s["qlib"]} for s in view.symbols()]
        db.seed_watchlist(row["id"], items)
    except Exception:  # noqa: BLE001  股票池初始化失败不应阻塞管理员登录
        pass
    return row


def _dev_admin() -> dict:
    email = dev_email() or "selftest@local"
    row = db.get_user_by_email(email)
    if row is None:
        row = _bootstrap_admin(email)
    elif row["role"] != "admin":
        # 测试身份固定为管理员，避免 selftest 被权限挡住
        db.set_role(row["id"], "admin")
        row = db.get_user(row["id"]) or row
    return row


def _refresh_identity() -> User | None:
    if auth_disabled():
        row = _dev_admin()
        user = User(row)
        login_user(user)
        return user

    email = request_email()
    if not email:
        return None

    row = db.get_user_by_email(email)
    if row is None:
        if email in admin_emails():
            row = _bootstrap_admin(email)
        else:
            # 未准入：记住邮箱，引导去邀请码页面
            session["pending_email"] = email
            logout_user()
            return None

    if row.get("status") != "active":
        session.pop("pending_email", None)
        logout_user()
        return None

    # QLIB_ADMIN_EMAIL 中的邮箱始终拥有管理员权限（即使之前以普通用户注册）
    if email in admin_emails() and row.get("role") != "admin":
        db.set_role(row["id"], "admin")
        row = db.get_user(row["id"]) or row

    session.pop("pending_email", None)
    user = User(row)
    login_user(user)
    _touch_login(user)
    return user


def _wants_json() -> bool:
    return request.path.startswith("/api/")


def _gate():
    endpoint = request.endpoint or ""
    if not endpoint:
        return None  # 交给 Flask 返回 404
    if endpoint in PUBLIC_ENDPOINTS:
        if endpoint in ("login_page", "invite_page", "redeem_invite"):
            _refresh_identity()
        return None

    user = _refresh_identity()
    if user is not None:
        return None

    pending = bool(session.get("pending_email"))
    target = url_for("invite_page") if pending else url_for("login_page")
    if _wants_json():
        return jsonify(
            {
                "error": "需要邀请码" if pending else "未登录",
                "need_invite": pending,
                "login_url": target,
            }
        ), 401
    return redirect(target)


# ---------------------------------------------------------------- 页面/接口
def _login_context(**extra) -> dict:
    ctx = {
        "trust_header": trust_cf_header(),
        "dev_email": dev_email(),
        "dev_login_allowed": dev_login_allowed(),
        "admin_configured": bool(admin_emails()),
        "logged_out": False,
    }
    ctx.update(extra)
    return ctx


def login_page():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if session.get("pending_email"):
        return redirect(url_for("invite_page"))
    return render_template("login.html", **_login_context())


def invite_page():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    pending = session.get("pending_email") or ""
    if not pending:
        # 没有 CF 身份时先回到说明页
        return redirect(url_for("login_page"))
    return render_template(
        "invite.html",
        pending_email=pending,
        preset_code=(request.args.get("code") or "").strip().upper(),
    )


def redeem_invite():
    if current_user.is_authenticated:
        return jsonify({"ok": True, "redirect": url_for("index"), "email": current_user.email})
    payload = request.get_json(silent=True) or request.form or {}
    code = str(payload.get("code") or "").strip()
    # 优先使用当前请求的 CF 身份，避免会话里的 pending_email 被旧标签页复用
    email = request_email() or session.get("pending_email")
    if not email:
        return jsonify({"error": "无法识别身份，请通过 Cloudflare Access 重新进入"}), 400
    try:
        row = db.redeem_invite(code, email)
    except db.InviteError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"核销失败: {exc}"}), 500

    session.pop("pending_email", None)
    login_user(User(row))
    db.touch_login(row["id"])
    return jsonify({"ok": True, "redirect": url_for("index"), "email": row["email"]})


def logout():
    logout_user()
    session.clear()
    # 不重定向到 /login：那里会再次读取 CF Header 自动登录，看起来像“退不出去”
    return render_template("login.html", **_login_context(logged_out=True))


def api_me():
    return jsonify(
        {
            "authenticated": current_user.is_authenticated,
            "email": getattr(current_user, "email", None),
            "role": getattr(current_user, "role", None),
            "admin": bool(getattr(current_user, "is_admin", False)),
            "pending_email": session.get("pending_email"),
            "trust_cf_header": trust_cf_header(),
        }
    )


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            if request.path.startswith("/api/"):
                return jsonify({"error": "未登录"}), 401
            return redirect(url_for("login_page"))
        if not getattr(current_user, "is_admin", False):
            if request.path.startswith("/api/"):
                return jsonify({"error": "需要管理员权限"}), 403
            return (
                "<!doctype html><meta charset='utf-8'>"
                "<h1>403 · 需要管理员权限</h1><p><a href='/'>返回首页</a></p>",
                403,
            )
        return fn(*args, **kwargs)

    return wrapper

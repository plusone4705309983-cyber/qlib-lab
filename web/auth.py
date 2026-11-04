"""应用内身份与准入：邮箱 + 密码 + 邀请码 -> users 表 -> Flask-Login。

安全边界：
- 应用只监听 127.0.0.1，公网 HTTPS 由 Cloudflare Tunnel 转发；Tunnel 不负责登录。
- 登录密码只存 Werkzeug 哈希，不存明文。
- 邀请码一次性使用；普通用户由管理员在 /admin/invites 生成邀请码后注册。
- `QLIB_AUTH_DISABLED=1` 仅供 selftest 使用，会以本地管理员身份放行所有请求。
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from functools import wraps
from pathlib import Path

from flask import jsonify, redirect, render_template, request, session, url_for
from flask_login import LoginManager, UserMixin, current_user, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

import db

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SECRET_FILE = ROOT / "data" / "private" / "session_secret"

LOGIN_TOUCH_SECONDS = 300
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128
LOGIN_MAX_FAILURES = int(os.environ.get("QLIB_LOGIN_MAX_FAILURES", "10") or 10)
LOGIN_FAILURE_WINDOW = 15 * 60
PUBLIC_ENDPOINTS = {
    "static",
    "healthz",
    "login_page",
    "login_submit",
    "invite_page",
    "invite_submit",
    "logout",
    "api_me",
}

_FAILURES: dict[str, list[float]] = {}
_FAILURES_LOCK = threading.Lock()


# ---------------------------------------------------------------- 配置
def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "off", "")


def auth_disabled() -> bool:
    return _env_bool("QLIB_AUTH_DISABLED", False)


def cookie_secure() -> bool:
    return _env_bool("QLIB_COOKIE_SECURE", True)


def admin_emails() -> set[str]:
    raw = os.environ.get("QLIB_ADMIN_EMAIL", "")
    return {db.normalize_email(x) for x in raw.replace(";", ",").split(",") if x.strip()}


def admin_password() -> str:
    return os.environ.get("QLIB_ADMIN_PASSWORD", "")


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
    # Flask 自身已有默认值，必须直接覆盖；setdefault 不会改掉默认的 Secure=False。
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = cookie_secure()
    login_manager.init_app(app)
    app.before_request(_gate)
    ensure_admin()


# ---------------------------------------------------------------- 密码 / 限速
def validate_password(password: str) -> str:
    password = password or ""
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"密码至少 {PASSWORD_MIN_LENGTH} 位"
    if len(password) > PASSWORD_MAX_LENGTH:
        return f"密码最多 {PASSWORD_MAX_LENGTH} 位"
    return ""


def hash_password(password: str) -> str:
    return generate_password_hash(password, method="pbkdf2:sha256")


def _rate_key(email: str) -> str:
    return f"{db.normalize_email(email)}|{request.remote_addr or ''}"


def _recent_failures(key: str) -> list[float]:
    cutoff = time.time() - LOGIN_FAILURE_WINDOW
    return [t for t in _FAILURES.get(key, []) if t >= cutoff]


def _check_rate_limit(key: str) -> bool:
    with _FAILURES_LOCK:
        recent = _recent_failures(key)
        _FAILURES[key] = recent
        return len(recent) < LOGIN_MAX_FAILURES


def _record_failure(key: str) -> None:
    with _FAILURES_LOCK:
        recent = _recent_failures(key)
        recent.append(time.time())
        _FAILURES[key] = recent


def _clear_failures(key: str) -> None:
    with _FAILURES_LOCK:
        _FAILURES.pop(key, None)


def reset_login_failures() -> None:
    """测试/运维用：清空进程内登录失败计数。"""
    with _FAILURES_LOCK:
        _FAILURES.clear()


# ---------------------------------------------------------------- 管理员初始化
def _seed_admin_watchlist(row: dict) -> None:
    """首次创建管理员时，把现有全局股票池复制为他的自选，避免升级后空白。"""
    try:
        import view

        items = [{"market": s["market"], "symbol": s["qlib"]} for s in view.symbols()]
        db.seed_watchlist(row["id"], items)
    except Exception:  # noqa: BLE001  股票池初始化失败不应阻塞管理员登录
        pass


def _ensure_admin_email(email: str) -> dict | None:
    """确保指定邮箱是管理员；没有密码时用 QLIB_ADMIN_PASSWORD 初始化。"""
    email = db.normalize_email(email)
    if not email:
        return None
    row = db.get_user_by_email(email)
    password = admin_password()
    if row is None:
        if not password:
            return None
        row = db.create_user(
            email,
            role="admin",
            status="active",
            password_hash=hash_password(password),
        )
        _seed_admin_watchlist(row)
        return row

    if row["role"] != "admin":
        db.set_role(row["id"], "admin")
        row = db.get_user(row["id"]) or row

    auth_row = db.get_user_auth(email)
    if auth_row is not None and not auth_row.get("password_hash") and password:
        db.set_password(row["id"], hash_password(password))
    return row


def ensure_admin() -> None:
    for email in admin_emails():
        try:
            _ensure_admin_email(email)
        except Exception:  # noqa: BLE001  启动时不要让管理员初始化问题拖垮整个应用
            pass


def _dev_admin() -> dict:
    email = db.normalize_email(os.environ.get("QLIB_DEV_EMAIL", "")) or "selftest@local"
    row = db.get_user_by_email(email)
    if row is None:
        row = db.create_user(
            email,
            role="admin",
            status="active",
            password_hash=hash_password("selftest-only-password"),
        )
        _seed_admin_watchlist(row)
    elif row["role"] != "admin":
        db.set_role(row["id"], "admin")
        row = db.get_user(row["id"]) or row
    return row


def _touch_login(user: User) -> None:
    now = time.time()
    last = float(session.get("_login_touched") or 0)
    if now - last > LOGIN_TOUCH_SECONDS:
        db.touch_login(user.id)
        session["_login_touched"] = now


# ---------------------------------------------------------------- 请求门禁
def _wants_json() -> bool:
    return request.path.startswith("/api/")


def _safe_next(target: str | None) -> str:
    target = (target or "").strip()
    if not target.startswith("/") or target.startswith("//"):
        return ""
    return target


def _gate():
    if auth_disabled():
        row = _dev_admin()
        login_user(User(row))
        return None

    endpoint = request.endpoint or ""
    if not endpoint:
        return None  # 交给 Flask 返回 404
    if endpoint in PUBLIC_ENDPOINTS:
        return None
    if current_user.is_authenticated:
        return None

    target = url_for("login_page", next=request.path) if request.method == "GET" else url_for("login_page")
    if _wants_json():
        return jsonify({"error": "未登录", "login_url": target}), 401
    return redirect(target)


# ---------------------------------------------------------------- 页面/接口
def _login_context(**extra) -> dict:
    ctx = {
        "admin_configured": bool(admin_emails()),
        "admin_ready": db.has_admin_with_password(),
        "email": "",
        "error": "",
        "next": "",
        "logged_out": False,
    }
    ctx.update(extra)
    return ctx


def login_page():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    return render_template(
        "login.html",
        **_login_context(
            next=_safe_next(request.args.get("next")),
            logged_out=request.args.get("logged_out") == "1",
        ),
    )


def login_submit():
    payload = request.form or request.get_json(silent=True) or {}
    email = db.normalize_email(str(payload.get("email") or ""))
    password = str(payload.get("password") or "")
    next_url = _safe_next(str(payload.get("next") or ""))
    ctx = _login_context(email=email, next=next_url)

    if not email or "@" not in email or not password:
        ctx["error"] = "请输入邮箱和密码"
        return render_template("login.html", **ctx), 400

    key = _rate_key(email)
    if not _check_rate_limit(key):
        ctx["error"] = "登录失败次数过多，请 15 分钟后再试"
        return render_template("login.html", **ctx), 429

    row = db.get_user_auth(email)
    if row is None or not row.get("password_hash") or not check_password_hash(row["password_hash"], password):
        _record_failure(key)
        ctx["error"] = "邮箱或密码不正确"
        return render_template("login.html", **ctx), 401

    if row.get("status") != "active":
        ctx["error"] = "该账号已被禁用，请联系管理员"
        return render_template("login.html", **ctx), 403

    # QLIB_ADMIN_EMAIL 中的邮箱始终拥有管理员权限（即使之前以普通用户注册）
    if email in admin_emails() and row.get("role") != "admin":
        db.set_role(row["id"], "admin")
        row = db.get_user(row["id"]) or row

    _clear_failures(key)
    session.clear()
    login_user(User(row))
    db.touch_login(row["id"])
    return redirect(next_url or url_for("index"))


def invite_page():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    return render_template(
        "invite.html",
        preset_code=(request.args.get("code") or "").strip().upper(),
        email="",
        error="",
    )


def invite_submit():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    payload = request.form or request.get_json(silent=True) or {}
    code = str(payload.get("code") or "").strip().upper()
    email = db.normalize_email(str(payload.get("email") or ""))
    password = str(payload.get("password") or "")
    confirm = str(payload.get("confirm") or "")

    def fail(message: str, status: int = 400):
        return (
            render_template(
                "invite.html",
                preset_code=code,
                email=email,
                error=message,
            ),
            status,
        )

    if not code:
        return fail("请输入邀请码")
    if not email or "@" not in email:
        return fail("请输入有效邮箱")
    password_error = validate_password(password)
    if password_error:
        return fail(password_error)
    if password != confirm:
        return fail("两次输入的密码不一致")

    try:
        row = db.redeem_invite(code, email, hash_password(password))
    except db.InviteError as exc:
        return fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        return fail(f"核销失败: {exc}", 500)

    session.clear()
    login_user(User(row))
    db.touch_login(row["id"])
    return redirect(url_for("index"))


def logout():
    logout_user()
    session.clear()
    return redirect(url_for("login_page", logged_out=1))


def api_me():
    return jsonify(
        {
            "authenticated": current_user.is_authenticated,
            "email": getattr(current_user, "email", None),
            "role": getattr(current_user, "role", None),
            "admin": bool(getattr(current_user, "is_admin", False)),
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

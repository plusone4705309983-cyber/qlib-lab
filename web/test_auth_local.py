"""应用内登录/邀请码回归测试：python web/test_auth_local.py

不依赖 pytest，不访问网络，不写真实 data/private/qlib_lab.db。
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="qlib_lab_auth_test_")
os.environ["QLIB_DB_PATH"] = str(Path(_TMP) / "auth.db")
os.environ["QLIB_SECRET_KEY"] = "auth-test-only-secret"
os.environ["QLIB_AUTH_DISABLED"] = "0"
os.environ["QLIB_ADMIN_EMAIL"] = "admin@example.com"
os.environ["QLIB_ADMIN_PASSWORD"] = "admin-pass-123"
os.environ["QLIB_COOKIE_SECURE"] = "0"
os.environ["QLIB_LOGIN_MAX_FAILURES"] = "3"

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from flask import Flask  # noqa: E402

import auth  # noqa: E402
import db  # noqa: E402
from web import app as webapp  # noqa: E402

webapp.app.config.update(TESTING=True)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def client():
    return webapp.app.test_client()


def login(c, email, password):
    return c.post(
        "/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )


def main():
    print("[1] 管理员初始化 / 登录")
    row = db.get_user_auth("admin@example.com")
    check("管理员已创建", row is not None)
    check("管理员密码已哈希", bool(row and row.get("password_hash")), str(row))
    check("密码不是明文", bool(row and row["password_hash"] != "admin-pass-123"))

    c = client()
    r = c.get("/login")
    check("登录页 200", r.status_code == 200 and b"/login" in r.data)
    me = c.get("/api/auth/me").get_json()
    check("未登录 /api/auth/me", me.get("authenticated") is False, str(me))

    r = login(c, "admin@example.com", "wrong-password")
    check("错误密码 401", r.status_code == 401, str(r.status_code))
    r = login(c, "admin@example.com", "admin-pass-123")
    check("管理员登录 302", r.status_code == 302, str(r.status_code))
    check("管理员登录后跳首页", r.headers.get("Location", "").endswith("/"), r.headers.get("Location"))
    me = c.get("/api/auth/me").get_json()
    check("登录后 /api/auth/me", me.get("authenticated") is True and me.get("admin") is True, str(me))
    check("管理员可访问 /admin/invites", c.get("/admin/invites").status_code == 200)

    print("[2] 邀请码注册")
    r = c.post("/api/admin/invites", json={"max_uses": 1, "expires_hours": 24})
    d = r.get_json() or {}
    check("管理员生成邀请码 200", r.status_code == 200, str(r.status_code))
    code = (d.get("invite") or {}).get("code", "")
    check("邀请码格式", code.startswith("INV-") and len(code) == 13, code)
    check("邀请链接包含邀请码", f"/invite?code={code}" in str(d.get("link", "")), str(d.get("link")))
    with webapp.app.test_request_context(
        "/",
        headers={"X-Forwarded-Proto": "https", "Host": "qlib.logoslab.cloud"},
    ):
        public_link = webapp._external_url("invite_page", code=code)
        check("公网邀请链接是 https", public_link.startswith("https://qlib.logoslab.cloud/"), public_link)

    c.get("/logout")
    u = client()
    r = u.get(f"/invite?code={code}")
    check("邀请页 200", r.status_code == 200 and b"INV-" in r.data)

    r = u.post(
        "/invite",
        data={"code": code, "email": "friend@example.com", "password": "short", "confirm": "short"},
        follow_redirects=False,
    )
    check("短密码被拒 400", r.status_code == 400, str(r.status_code))

    r = u.post(
        "/invite",
        data={
            "code": code,
            "email": "friend@example.com",
            "password": "friend-pass-123",
            "confirm": "different-pass",
        },
        follow_redirects=False,
    )
    check("两次密码不一致 400", r.status_code == 400, str(r.status_code))

    r = u.post(
        "/invite",
        data={
            "code": code,
            "email": "friend@example.com",
            "password": "friend-pass-123",
            "confirm": "friend-pass-123",
        },
        follow_redirects=False,
    )
    check("邀请码注册 302", r.status_code == 302, str(r.status_code))
    me = u.get("/api/auth/me").get_json()
    check("普通用户已登录", me.get("authenticated") is True and me.get("admin") is False, str(me))
    check("普通用户自选为空", u.get("/api/symbols").get_json() == [])
    check("普通用户 /admin/invites 403", u.get("/admin/invites").status_code == 403)
    check("普通用户 /api/positions 403", u.get("/api/positions").status_code == 403)
    check("普通用户 /api/broker/status 403", u.get("/api/broker/status").status_code == 403)

    u.get("/logout")
    reuse = client()
    r = reuse.post(
        "/invite",
        data={
            "code": code,
            "email": "other@example.com",
            "password": "other-pass-123",
            "confirm": "other-pass-123",
        },
        follow_redirects=False,
    )
    check("邀请码不能重复使用 400", r.status_code == 400, str(r.status_code))

    print("[3] 用户自选隔离")
    admin = client()
    login(admin, "admin@example.com", "admin-pass-123")
    admin.post("/api/watchlist", json={"market": "cn", "symbol": "sh600519"})
    admin_symbols = admin.get("/api/symbols").get_json()
    check("管理员自选包含 sh600519", any(x.get("qlib") == "sh600519" for x in admin_symbols))

    friend = client()
    login(friend, "friend@example.com", "friend-pass-123")
    friend_symbols = friend.get("/api/symbols").get_json()
    check("普通用户看不到管理员自选", friend_symbols == [], str(friend_symbols))

    print("[4] 登录失败限速")
    auth.reset_login_failures()
    bad = client()
    codes = []
    for _ in range(4):
        codes.append(login(bad, "friend@example.com", "bad-password").status_code)
    check("前 3 次失败 401", codes[:3] == [401, 401, 401], str(codes))
    check("第 4 次触发 429", codes[3] == 429, str(codes))

    print("[5] 管理员用户管理")
    auth.reset_login_failures()
    friend_row = db.get_user_by_email("friend@example.com")
    check("测试用户存在", friend_row is not None)
    fid = friend_row["id"] if friend_row else 0

    r = admin.get(f"/api/admin/users/{fid}/watchlist")
    check("管理员查看用户自选 200", r.status_code == 200 and r.get_json().get("symbols") == [], str(r.status_code))

    r = admin.post(
        f"/api/admin/users/{fid}/password",
        json={"password": "friend-new-pass-123", "confirm": "friend-new-pass-123"},
    )
    check("管理员重置密码 200", r.status_code == 200, str(r.status_code))

    old = client()
    check("旧密码失效 401", login(old, "friend@example.com", "friend-pass-123").status_code == 401)
    new = client()
    check("新密码登录 302", login(new, "friend@example.com", "friend-new-pass-123").status_code == 302)

    r = admin.post(f"/api/admin/users/{fid}/status", json={"status": "disabled"})
    check("禁用用户 200", r.status_code == 200, str(r.status_code))
    check("禁用后现有会话失效", new.get("/api/auth/me").get_json().get("authenticated") is False)
    disabled = client()
    check("禁用用户不能登录 403", login(disabled, "friend@example.com", "friend-new-pass-123").status_code == 403)

    r = admin.post(f"/api/admin/users/{fid}/status", json={"status": "active"})
    check("重新启用用户 200", r.status_code == 200, str(r.status_code))
    reenabled = client()
    check("启用后可以登录 302", login(reenabled, "friend@example.com", "friend-new-pass-123").status_code == 302)

    admin_row = db.get_user_by_email("admin@example.com")
    aid = admin_row["id"] if admin_row else 0
    check("管理员不能删除自己 400", admin.delete(f"/api/admin/users/{aid}").status_code == 400)
    check(
        "管理员不能禁用自己 400",
        admin.post(f"/api/admin/users/{aid}/status", json={"status": "disabled"}).status_code == 400,
    )

    r = admin.delete(f"/api/admin/users/{fid}")
    check("删除用户 200", r.status_code == 200, str(r.status_code))
    check("删除后用户不存在", db.get_user_by_email("friend@example.com") is None)
    deleted = client()
    check("删除后不能登录 401", login(deleted, "friend@example.com", "friend-new-pass-123").status_code == 401)

    print("[6] Cookie 安全配置")
    os.environ["QLIB_COOKIE_SECURE"] = "1"
    probe = Flask("cookie_probe")
    auth.init_app(probe)
    check("生产 cookie Secure", probe.config["SESSION_COOKIE_SECURE"] is True)
    check("cookie HttpOnly", probe.config["SESSION_COOKIE_HTTPONLY"] is True)
    check("cookie SameSite=Lax", probe.config["SESSION_COOKIE_SAMESITE"] == "Lax")
    os.environ["QLIB_COOKIE_SECURE"] = "0"

    print(f"\n结果: {PASS} PASS / {FAIL} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

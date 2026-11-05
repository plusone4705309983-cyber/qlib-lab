"""SQLite 持久化：应用内用户、邀请码、每人自选股（P0）。

数据库固定在 `data/private/qlib_lab.db`（已被 .gitignore 忽略），
与持仓账本同目录，绝不进仓库。

设计原则：
- 每次操作新建连接，避免 waitress 多线程共享 sqlite3 连接。
- 写操作使用短事务；邀请码核销用 `BEGIN IMMEDIATE` 防止并发重复使用。
- 时间统一存 UTC ISO 字符串，前端按本地时区显示。
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PRIVATE_DIR = ROOT / "data" / "private"
# QLIB_DB_PATH 仅供测试/临时环境覆盖，正常部署不需要设置
DB_PATH = Path(os.environ.get("QLIB_DB_PATH") or (PRIVATE_DIR / "qlib_lab.db"))

# 去掉容易混淆的 I/O/0/1
INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
INVITE_PREFIX = "INV-"
MAX_INVITE_USES = 50
MAX_INVITE_HOURS = 24 * 30
DEFAULT_INVITE_HOURS = 72


class InviteError(ValueError):
    """邀请码无效 / 过期 / 已用完。"""


# ---------------------------------------------------------------- 基础
def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=20, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 20000")
    return con


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as con:
        con.execute("PRAGMA journal_mode = WAL")
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
                role          TEXT NOT NULL DEFAULT 'user'
                              CHECK (role IN ('user', 'admin')),
                status        TEXT NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active', 'pending', 'disabled')),
                password_hash TEXT,
                created_at    TEXT NOT NULL,
                last_login_at TEXT
            );

            CREATE TABLE IF NOT EXISTS invites (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                code        TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_by  INTEGER REFERENCES users(id),
                used_by     INTEGER REFERENCES users(id),
                max_uses    INTEGER NOT NULL DEFAULT 1,
                used_count  INTEGER NOT NULL DEFAULT 0,
                expires_at  TEXT,
                created_at  TEXT NOT NULL,
                used_at     TEXT
            );

            CREATE TABLE IF NOT EXISTS user_watchlist (
                user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                market   TEXT NOT NULL,
                symbol   TEXT NOT NULL,
                added_at TEXT NOT NULL,
                PRIMARY KEY (user_id, market, symbol)
            );

            CREATE TABLE IF NOT EXISTS user_broker (
                user_id      INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                api_key_enc  TEXT NOT NULL,
                secret_enc   TEXT NOT NULL,
                base_url     TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT NOT NULL,
                actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                actor_email   TEXT,
                action        TEXT NOT NULL,
                target_type   TEXT,
                target_id     TEXT,
                detail        TEXT,
                ip            TEXT,
                user_agent    TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_invites_code ON invites(code);
            CREATE INDEX IF NOT EXISTS idx_watchlist_user ON user_watchlist(user_id);
            CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
            CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
            CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_email);
            """
        )
        # 兼容 P0 早期数据库：users 表最初没有 password_hash。
        columns = {row["name"] for row in con.execute("PRAGMA table_info(users)").fetchall()}
        if "password_hash" not in columns:
            con.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")


# ---------------------------------------------------------------- 用户
def _user_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "email": row["email"],
        "role": row["role"],
        "status": row["status"],
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
    }


def get_user(user_id: int) -> dict | None:
    with _connect() as con:
        return _user_dict(con.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def get_user_by_email(email: str) -> dict | None:
    with _connect() as con:
        return _user_dict(
            con.execute("SELECT * FROM users WHERE email = ?", (normalize_email(email),)).fetchone()
        )


def create_user(
    email: str,
    role: str = "user",
    status: str = "active",
    password_hash: str | None = None,
) -> dict:
    email = normalize_email(email)
    if not email or "@" not in email:
        raise ValueError("邮箱格式不正确")
    if role not in ("user", "admin"):
        raise ValueError("role 只能是 user 或 admin")
    if status not in ("active", "pending", "disabled"):
        raise ValueError("status 只能是 active/pending/disabled")
    with _connect() as con:
        cur = con.execute(
            "INSERT INTO users (email, role, status, password_hash, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (email, role, status, password_hash, now_iso()),
        )
        return _user_dict(con.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone())


def get_user_auth(email: str) -> dict | None:
    """登录专用：返回包含 password_hash 的用户行，不要给模板/API 使用。"""
    with _connect() as con:
        row = con.execute(
            "SELECT * FROM users WHERE email = ?", (normalize_email(email),)
        ).fetchone()
        if row is None:
            return None
        data = _user_dict(row) or {}
        data["password_hash"] = row["password_hash"]
        return data


def set_password(user_id: int, password_hash: str) -> None:
    if not password_hash:
        raise ValueError("password_hash 不能为空")
    with _connect() as con:
        con.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (password_hash, int(user_id)),
        )


def has_admin_with_password() -> bool:
    with _connect() as con:
        row = con.execute(
            "SELECT 1 FROM users WHERE role = 'admin' AND password_hash IS NOT NULL LIMIT 1"
        ).fetchone()
        return row is not None


# ---------------------------------------------------------------- 用户券商密钥
def get_user_broker(user_id: int) -> dict | None:
    with _connect() as con:
        row = con.execute(
            "SELECT * FROM user_broker WHERE user_id = ?", (int(user_id),)
        ).fetchone()
        if row is None:
            return None
        return {
            "user_id": row["user_id"],
            "api_key_enc": row["api_key_enc"],
            "secret_enc": row["secret_enc"],
            "base_url": row["base_url"],
            "updated_at": row["updated_at"],
        }


def upsert_user_broker(
    user_id: int,
    api_key_enc: str,
    secret_enc: str,
    base_url: str,
) -> None:
    with _connect() as con:
        con.execute(
            """
            INSERT INTO user_broker (user_id, api_key_enc, secret_enc, base_url, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                api_key_enc = excluded.api_key_enc,
                secret_enc = excluded.secret_enc,
                base_url = excluded.base_url,
                updated_at = excluded.updated_at
            """,
            (int(user_id), api_key_enc, secret_enc, base_url, now_iso()),
        )


def delete_user_broker(user_id: int) -> bool:
    with _connect() as con:
        cur = con.execute("DELETE FROM user_broker WHERE user_id = ?", (int(user_id),))
        return cur.rowcount > 0


def touch_login(user_id: int) -> None:
    with _connect() as con:
        con.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now_iso(), user_id))


def set_role(user_id: int, role: str) -> None:
    if role not in ("user", "admin"):
        raise ValueError("role 只能是 user 或 admin")
    with _connect() as con:
        con.execute("UPDATE users SET role = ? WHERE id = ?", (role, int(user_id)))


def set_status(user_id: int, status: str) -> bool:
    if status not in ("active", "pending", "disabled"):
        raise ValueError("status 只能是 active/pending/disabled")
    with _connect() as con:
        cur = con.execute(
            "UPDATE users SET status = ? WHERE id = ?", (status, int(user_id))
        )
        return cur.rowcount > 0


def count_active_admins(exclude_user_id: int | None = None) -> int:
    sql = "SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND status = 'active'"
    params: tuple = ()
    if exclude_user_id is not None:
        sql += " AND id <> ?"
        params = (int(exclude_user_id),)
    with _connect() as con:
        return int(con.execute(sql, params).fetchone()["n"])


def delete_user(user_id: int) -> bool:
    """硬删除用户及其自选；邀请码记录保留，但解除 created_by/used_by 引用。"""
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("UPDATE invites SET used_by = NULL WHERE used_by = ?", (int(user_id),))
        con.execute("UPDATE invites SET created_by = NULL WHERE created_by = ?", (int(user_id),))
        con.execute("DELETE FROM user_watchlist WHERE user_id = ?", (int(user_id),))
        cur = con.execute("DELETE FROM users WHERE id = ?", (int(user_id),))
        con.execute("COMMIT")
        return cur.rowcount > 0
    except Exception:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        con.close()


def list_users() -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            """
            SELECT u.*, COUNT(w.symbol) AS watchlist_count
            FROM users u
            LEFT JOIN user_watchlist w ON w.user_id = u.id
            GROUP BY u.id
            ORDER BY u.id
            """
        ).fetchall()
        out = []
        for r in rows:
            d = _user_dict(r) or {}
            d["watchlist_count"] = r["watchlist_count"]
            out.append(d)
        return out


# ---------------------------------------------------------------- 邀请码
def _new_code() -> str:
    raw = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(8))
    return f"{INVITE_PREFIX}{raw[:4]}-{raw[4:]}"


def _invite_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    used_count = int(row["used_count"] or 0)
    max_uses = int(row["max_uses"] or 1)
    expires_at = row["expires_at"]
    exp = parse_iso(expires_at)
    if used_count >= max_uses:
        status = "used"
    elif exp is not None and exp <= datetime.now(timezone.utc):
        status = "expired"
    else:
        status = "active"
    return {
        "id": row["id"],
        "code": row["code"],
        "created_by": row["created_by"],
        "created_by_email": row["created_by_email"] if "created_by_email" in row.keys() else None,
        "used_by": row["used_by"],
        "used_by_email": row["used_by_email"] if "used_by_email" in row.keys() else None,
        "max_uses": max_uses,
        "used_count": used_count,
        "expires_at": expires_at,
        "created_at": row["created_at"],
        "used_at": row["used_at"],
        "status": status,
    }


_INVITE_SELECT = """
    SELECT i.*, cu.email AS created_by_email, uu.email AS used_by_email
    FROM invites i
    LEFT JOIN users cu ON cu.id = i.created_by
    LEFT JOIN users uu ON uu.id = i.used_by
"""


def create_invite(created_by: int, max_uses: int = 1, expires_hours: int = DEFAULT_INVITE_HOURS) -> dict:
    try:
        max_uses = int(max_uses)
        expires_hours = int(expires_hours)
    except (TypeError, ValueError):
        raise ValueError("max_uses / expires_hours 必须是整数") from None
    if not 1 <= max_uses <= MAX_INVITE_USES:
        raise ValueError(f"max_uses 必须在 1~{MAX_INVITE_USES} 之间")
    if not 1 <= expires_hours <= MAX_INVITE_HOURS:
        raise ValueError(f"expires_hours 必须在 1~{MAX_INVITE_HOURS} 之间")

    expires_at = (datetime.now(timezone.utc) + timedelta(hours=expires_hours)).replace(
        microsecond=0
    ).isoformat()
    with _connect() as con:
        for _ in range(20):
            code = _new_code()
            try:
                cur = con.execute(
                    "INSERT INTO invites (code, created_by, max_uses, used_count, expires_at, created_at)"
                    " VALUES (?, ?, ?, 0, ?, ?)",
                    (code, created_by, max_uses, expires_at, now_iso()),
                )
            except sqlite3.IntegrityError:
                continue
            row = con.execute(_INVITE_SELECT + " WHERE i.id = ?", (cur.lastrowid,)).fetchone()
            return _invite_dict(row) or {}
    raise RuntimeError("生成邀请码失败，请重试")


def list_invites(limit: int = 200) -> list[dict]:
    limit = max(1, min(int(limit or 200), 1000))
    with _connect() as con:
        rows = con.execute(_INVITE_SELECT + " ORDER BY i.id DESC LIMIT ?", (limit,)).fetchall()
        return [_invite_dict(r) or {} for r in rows]


def get_invite_by_code(code: str) -> dict | None:
    code = (code or "").strip().upper()
    if not code:
        return None
    with _connect() as con:
        row = con.execute(_INVITE_SELECT + " WHERE i.code = ?", (code,)).fetchone()
        return _invite_dict(row)


def revoke_invite(invite_id: int) -> bool:
    with _connect() as con:
        cur = con.execute("DELETE FROM invites WHERE id = ?", (int(invite_id),))
        return cur.rowcount > 0


def count_invites_since(user_id: int, hours: int = 24) -> int:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0).isoformat()
    with _connect() as con:
        row = con.execute(
            "SELECT COUNT(*) AS n FROM invites WHERE created_by = ? AND created_at >= ?",
            (user_id, since),
        ).fetchone()
        return int(row["n"])


def redeem_invite(code: str, email: str, password_hash: str | None = None) -> dict:
    """核销邀请码并创建普通用户。失败抛 InviteError。"""
    code = (code or "").strip().upper()
    email = normalize_email(email)
    if not code:
        raise InviteError("请输入邀请码")
    if not email or "@" not in email:
        raise InviteError("邮箱格式不正确")
    if not password_hash:
        raise InviteError("请设置登录密码")

    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM invites WHERE code = ?", (code,)).fetchone()
        if row is None:
            raise InviteError("邀请码无效")
        invite = _invite_dict(row) or {}
        if invite["status"] == "used":
            raise InviteError("邀请码已被用完")
        if invite["status"] == "expired":
            raise InviteError("邀请码已过期")

        existing = con.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if existing is not None:
            raise InviteError("该邮箱已是用户，请直接访问首页")

        now = now_iso()
        cur = con.execute(
            "INSERT INTO users (email, role, status, password_hash, created_at)"
            " VALUES (?, 'user', 'active', ?, ?)",
            (email, password_hash, now),
        )
        user_id = cur.lastrowid
        con.execute(
            "UPDATE invites SET used_count = used_count + 1, used_by = ?, used_at = ? WHERE id = ?",
            (user_id, now, row["id"]),
        )
        con.execute("COMMIT")
        return _user_dict(con.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()) or {}
    except Exception:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        con.close()


# ---------------------------------------------------------------- 自选股
def list_watchlist(user_id: int) -> list[dict]:
    with _connect() as con:
        rows = con.execute(
            "SELECT market, symbol, added_at FROM user_watchlist WHERE user_id = ? ORDER BY added_at, symbol",
            (int(user_id),),
        ).fetchall()
        return [{"market": r["market"], "symbol": r["symbol"], "added_at": r["added_at"]} for r in rows]


def add_watchlist(user_id: int, market: str, symbol: str) -> None:
    with _connect() as con:
        con.execute(
            "INSERT OR IGNORE INTO user_watchlist (user_id, market, symbol, added_at) VALUES (?, ?, ?, ?)",
            (int(user_id), str(market).lower(), str(symbol).lower(), now_iso()),
        )


def remove_watchlist(user_id: int, market: str, symbol: str) -> bool:
    with _connect() as con:
        cur = con.execute(
            "DELETE FROM user_watchlist WHERE user_id = ? AND market = ? AND symbol = ?",
            (int(user_id), str(market).lower(), str(symbol).lower()),
        )
        return cur.rowcount > 0


def seed_watchlist(user_id: int, items: list[dict]) -> None:
    """首次创建管理员时，把现有全局股票池复制为他的自选，避免升级后空白。"""
    now = now_iso()
    with _connect() as con:
        con.executemany(
            "INSERT OR IGNORE INTO user_watchlist (user_id, market, symbol, added_at) VALUES (?, ?, ?, ?)",
            [(int(user_id), str(x["market"]).lower(), str(x["symbol"]).lower(), now) for x in items],
        )


# ---------------------------------------------------------------- 审计日志
def add_audit(
    actor_user_id: int | None,
    actor_email: str | None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    with _connect() as con:
        con.execute(
            """
            INSERT INTO audit_log
                (ts, actor_user_id, actor_email, action, target_type, target_id, detail, ip, user_agent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_iso(),
                int(actor_user_id) if actor_user_id is not None else None,
                actor_email,
                action,
                target_type,
                target_id,
                detail,
                ip,
                user_agent,
            ),
        )


def list_audit(
    limit: int = 100,
    offset: int = 0,
    action: str | None = None,
    actor: str | None = None,
) -> list[dict]:
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 100
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0

    clauses = []
    params: list = []
    action = (action or "").strip()
    actor = (actor or "").strip()
    if action:
        clauses.append("action LIKE ?")
        params.append(f"%{action}%")
    if actor:
        clauses.append("actor_email LIKE ?")
        params.append(f"%{actor}%")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([limit, offset])

    with _connect() as con:
        rows = con.execute(
            f"""
            SELECT id, ts, actor_user_id, actor_email, action, target_type, target_id,
                   detail, ip, user_agent
            FROM audit_log
            {where}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]


init_db()

"""重置应用内用户密码。

用法：
    .venv\\Scripts\\python web\\set_password.py you@example.com
    .venv\\Scripts\\python web\\set_password.py you@example.com --password "new-password"

不传 --password 时会交互式输入，避免密码出现在命令历史里。
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import broker_config  # noqa: E402,F401  先加载 .env
import auth  # noqa: E402
import db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="重置 qlib-lab 用户密码")
    parser.add_argument("email", help="用户邮箱")
    parser.add_argument("--password", help="新密码；不填则交互输入")
    args = parser.parse_args()

    email = db.normalize_email(args.email)
    row = db.get_user_by_email(email)
    if row is None:
        print(f"用户不存在: {email}")
        return 1

    password = args.password
    if password is None:
        password = getpass.getpass("新密码（至少 8 位）: ")
        confirm = getpass.getpass("再输入一次: ")
        if password != confirm:
            print("两次输入的密码不一致")
            return 1

    error = auth.validate_password(password)
    if error:
        print(error)
        return 1

    db.set_password(row["id"], auth.hash_password(password))
    print(f"已重置密码: {email}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

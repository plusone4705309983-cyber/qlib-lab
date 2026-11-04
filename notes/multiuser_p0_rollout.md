# 多用户 P0 上线与验收手册（应用内登录）

> 适用版本：2026-11-04 之后的 P0 改动（应用内用户 / 邀请码 / 每人自选）。
> 生产入口：`https://qlib.logoslab.cloud`
> 应用：`127.0.0.1:8853`，计划任务 `Qlib_App`，隧道服务 `Cloudflared`。
> 相关设计：`notes/multiuser_personal_space_invite_plan.md`。

---

## 0. 架构：Cloudflare 只做 Tunnel

```text
浏览器
  │ HTTPS
  ▼
Cloudflare Tunnel
  │ http://localhost:8853
  ▼
Flask 应用
  ├─ 邮箱 + 密码登录
  ├─ 邀请码注册
  ├─ admin / user 权限
  └─ 每人一套 user_watchlist
```

关键变化：

| 项目 | 旧方案 | 现在 |
|---|---|---|
| 公网 HTTPS | Cloudflare Tunnel | Cloudflare Tunnel（不变） |
| 登录 | Cloudflare Access 邮箱 OTP | **应用内邮箱 + 密码** |
| 新用户准入 | Cloudflare 策略 + 邀请码 | **只用应用内邀请码** |
| 管理员 | `QLIB_ADMIN_EMAIL` 首次登录自动建 | `QLIB_ADMIN_EMAIL` + `QLIB_ADMIN_PASSWORD` 首次启动自动建 |
| 每人自选 | `user_watchlist` | `user_watchlist`（不变） |

**不再需要**：

- Cloudflare Access 应用；
- Cloudflare 邮箱验证码；
- Gmail / Google 账号；
- 把朋友邮箱提前加进 Cloudflare 策略。

Cloudflare 只负责 HTTPS 和隐藏源站。登录、邀请码、用户数据全部在 Flask + SQLite 里。

---

## 1. 上线前备份

```powershell
Set-Location C:\Work_Data\Source_Code_New\qlib-lab

$stamp  = Get-Date -Format "yyyyMMdd-HHmmss"
$backup = "C:\Work_Data\Source_Code_New\qlib-lab-backup-$stamp"
New-Item -ItemType Directory -Force $backup | Out-Null

Copy-Item .env                              $backup\.env -ErrorAction SilentlyContinue
Copy-Item config\symbols.yaml               $backup\symbols.yaml
Copy-Item data\private\positions.json       $backup\positions.json -ErrorAction SilentlyContinue
Copy-Item data\private\qlib_lab.db          $backup\qlib_lab.db -ErrorAction SilentlyContinue
Copy-Item data\private\qlib_lab.db-wal      $backup\qlib_lab.db-wal -ErrorAction SilentlyContinue
Copy-Item data\private\qlib_lab.db-shm      $backup\qlib_lab.db-shm -ErrorAction SilentlyContinue
Copy-Item data\private\session_secret       $backup\session_secret -ErrorAction SilentlyContinue

Get-ChildItem $backup
```

如果应用正在运行，直接复制 SQLite 可能漏掉 WAL 里的最新写入。更稳妥的做法是先停应用，或用在线备份：

```powershell
.venv\Scripts\python.exe -c "import sqlite3; src=sqlite3.connect(r'data/private/qlib_lab.db'); dst=sqlite3.connect(r'data/private/qlib_lab.backup.db'); src.backup(dst); dst.close(); src.close()"
```

---

## 2. 配置 `.env`

`.env` 必须在仓库根目录，不是 `web\.env`。它已被 gitignore。

```ini
# 管理员邮箱，逗号分隔
QLIB_ADMIN_EMAIL=you@example.com

# 管理员初始密码，至少 8 位
QLIB_ADMIN_PASSWORD=change-me-to-a-long-random-password

# 生产环境走 https，必须为 1；本机 http://127.0.0.1 调试时改为 0
QLIB_COOKIE_SECURE=1

# 可选：不填会自动生成 data/private/session_secret
# QLIB_SECRET_KEY=change-me-to-a-long-random-string
```

说明：

- `QLIB_ADMIN_PASSWORD` 只在管理员**还没有密码**时写入数据库。
- 之后修改 `.env` 里的密码不会覆盖已有密码；重置请用第 8 节的命令。
- `QLIB_TRUST_CF_HEADER`、`QLIB_ALLOW_DEV_LOGIN`、`QLIB_DEV_EMAIL` 已不再需要。
- Alpaca 的 `APCA_*` 在 P0 阶段保持注释；模拟交易在 P1 前仍只对 admin 开放。

---

## 3. 本地回归（不碰生产数据库）

```powershell
Set-Location C:\Work_Data\Source_Code_New\qlib-lab

# 应用内登录 / 邀请码 / 权限 / 自选隔离 / 限速
.venv\Scripts\python.exe web\test_auth_local.py

# 原有功能回归
.venv\Scripts\python.exe web\selftest.py
```

预期：

- `web\test_auth_local.py`：`31 PASS / 0 FAIL`；
- `web\selftest.py`：原有 108 项全部 PASS。

两个脚本都使用临时 SQLite，不会写 `data/private/qlib_lab.db`。

---

## 4. 重启应用

```powershell
schtasks /End /TN Qlib_App
schtasks /Run /TN Qlib_App
timeout /t 10 /nobreak

# 健康检查
Invoke-WebRequest http://127.0.0.1:8853/healthz -UseBasicParsing

# 看启动日志
Get-Content web\logs\app.log -Tail 50
```

日志里不应再出现：

- `未配置 QLIB_ADMIN_EMAIL`
- `管理员还没有密码`

如果出现，检查 `.env` 是否在仓库根目录，然后重启。

---

## 5. Cloudflare 侧：只保留 Tunnel

### 5.1 保留 Tunnel

在 Cloudflare Zero Trust → Networks → Tunnels 里，保留原来的 Tunnel 和路由：

```text
qlib.logoslab.cloud  →  http://localhost:8853
```

不要改端口，不要把 8853 直接暴露到公网。

### 5.2 删除或禁用 Access 应用

进入 Zero Trust → Access → Applications，找到 `qlib-lab`：

- **推荐**：删除这个 Access 应用；
- 或者：把它的策略改成 `Bypass`；
- 不要保留 `Allow + 邮箱 OTP`，否则朋友会先被 Cloudflare 验证码挡住，根本到不了应用登录页。

删除 Access 应用不会删除 Tunnel，也不会影响 HTTPS。

### 5.3 不需要再做的事

- 不需要在 Access 里维护邮箱列表；
- 不需要配置 One-time PIN；
- 不需要让朋友收 Cloudflare 6 位验证码；
- 不需要 Gmail / Google 账号。

---

## 6. 管理员首次登录

1. 打开 `https://qlib.logoslab.cloud/login`。
2. 输入 `QLIB_ADMIN_EMAIL` 和 `QLIB_ADMIN_PASSWORD`。
3. 首次启动会自动创建 admin 用户，并把 `config/symbols.yaml` 里现有股票复制为管理员自选。
4. 右上角出现「邀请码」入口，说明 admin 权限正常。

检查：

```powershell
Invoke-WebRequest https://qlib.logoslab.cloud/healthz -UseBasicParsing
```

浏览器里确认：

- `/admin/invites` 能打开；
- `/api/auth/me` 返回 `authenticated: true`、`admin: true`；
- 侧栏能看到管理员自选。

---

## 7. 邀请朋友

1. 管理员进入 `/admin/invites`。
2. 生成邀请码：默认 1 次、72 小时有效。
3. 复制邀请链接，发给朋友。
4. 朋友打开链接，填写：
   - 邀请码；
   - 自己的邮箱（QQ、163、139、Outlook 都可以）；
   - 密码（至少 8 位，自己设置）。
5. 注册成功后自动登录，新用户自选为空。

邀请码规则：

- 一次性使用；
- 默认 72 小时过期；
- 24 小时内最多生成 50 个；
- 不绑定邮箱，谁先兑换谁使用；
- 已使用/过期会明确提示。

朋友**不需要**：

- Gmail；
- Google 账号；
- Cloudflare 邮箱验证码；
- 提前把邮箱告诉管理员。

---

## 8. 重置密码

```powershell
Set-Location C:\Work_Data\Source_Code_New\qlib-lab
.venv\Scripts\python.exe web\set_password.py you@example.com
```

按提示输入两次新密码。也可以非交互：

```powershell
.venv\Scripts\python.exe web\set_password.py you@example.com --password "new-password"
```

> 非交互方式会把密码留在命令历史里，只建议在受控终端使用。

---

## 9. 验收清单

管理员：

- [ ] `https://qlib.logoslab.cloud/login` 能打开；
- [ ] 管理员邮箱 + 密码能登录；
- [ ] `/admin/invites` 能打开；
- [ ] 能生成邀请码；
- [ ] 邀请链接是 `https://`；
- [ ] 管理员自选包含原有股票。

普通用户：

- [ ] 用邀请码 + 邮箱 + 密码能注册；
- [ ] 注册后自动登录；
- [ ] 自选为空；
- [ ] 能看到行情、K 线、异常清单、信号回测、股东穿透、因子实验室；
- [ ] `/admin/invites` 返回 403；
- [ ] `/api/positions` 返回 403；
- [ ] `/api/broker/status` 返回 403；
- [ ] 看不到管理员的自选；
- [ ] 退出后回到 `/login`。

安全：

- [ ] 同一邀请码第二次使用失败；
- [ ] 错误密码连续多次后返回 429；
- [ ] 生产环境 `QLIB_COOKIE_SECURE=1`；
- [ ] `data/private/qlib_lab.db` 和 `.env` 没有进 git。

---

## 10. 回滚

代码回滚：

```powershell
git log --oneline
git checkout <上一个稳定提交>
schtasks /End /TN Qlib_App
schtasks /Run /TN Qlib_App
```

数据库是**增量迁移**：新代码给 `users` 增加了 `password_hash` 列，旧代码会忽略它。
回滚代码不需要删库。

如果回滚到依赖 Cloudflare Access Header 的旧版本，需要同时：

1. 重新启用 Cloudflare Access 应用；
2. 在 `.env` 恢复 `QLIB_TRUST_CF_HEADER=1`；
3. 重启应用。

---

## 11. 故障排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 打开站点一直跳 Cloudflare 验证码 | Access 应用还在 | 删除 Access 应用或设 Bypass |
| `/login` 打不开，502 | Tunnel 或应用没起来 | 检查 `Cloudflared`、`Qlib_App`、`web\logs\app.log` |
| 管理员登录提示密码错误 | `.env` 密码不是当前数据库密码 | 用 `web\set_password.py` 重置 |
| 登录页提示“管理员还没有密码” | `QLIB_ADMIN_PASSWORD` 未设置 | 写入 `.env` 后重启 |
| 本机 `http://127.0.0.1:8853` 登录后仍跳登录页 | `QLIB_COOKIE_SECURE=1` 但用的是 http | 本机调试临时设为 `0`，生产保持 `1` |
| 邀请码提示无效/过期 | 已使用、过期或输错 | 重新生成 |
| 普通用户打开 `/admin/invites` 403 | 正常权限隔离 | 不需要处理 |
| 普通用户看不到持仓/模拟交易 | P1 前仅 admin | 正常 |
| 忘记管理员邮箱 | `QLIB_ADMIN_EMAIL` 被改 | 改回原邮箱或直接改数据库 `users.role` |

日志：

```powershell
Get-Content web\logs\app.log -Tail 100
```

数据库：

```powershell
.venv\Scripts\python.exe -c "import sqlite3; con=sqlite3.connect(r'data/private/qlib_lab.db'); print(con.execute('select id,email,role,status from users').fetchall())"
```

# 多用户 P0 上线与验收手册

> 适用版本：2026-11-04 的 P0 改动（应用内用户 / 邀请码 / 每人自选）。
> 生产入口：`https://qlib.logoslab.cloud`
> 应用：`127.0.0.1:8853`，计划任务 `Qlib_App`，隧道服务 `Cloudflared`。
> 相关设计：`notes/multiuser_personal_space_invite_plan.md`。
> Cloudflare 详细步骤：`notes/cloudflare_access_step_by_step.md`。

---

## 0. 这次上线改变了什么

| 项目 | 上线前 | 上线后 |
|---|---|---|
| 身份 | 只有 Cloudflare 门禁 | Cloudflare 门禁 + 应用内 `users` 表 |
| 管理员 | 无 | `QLIB_ADMIN_EMAIL` 首次登录自动建 admin |
| 新朋友 | 能进就能用 | 必须输入邀请码，创建普通用户后才可用 |
| 自选股 | 全局一套 | 每人一套 `user_watchlist` |
| 模拟交易 | 所有能访问的人都能用共享 Alpaca 密钥 | **P1 前仅 admin** |
| 持仓账本 | 全局共享 | **P1 前仅 admin**；朋友看到 P1 提示 |

朋友在 P0 阶段仍可正常使用：行情 K 线、异常清单、信号回测、股东穿透、因子实验室，以及自己的「我的自选」。

### 0.1 必须分两台机器吗？

**不是必须。** P0 可以继续在一台机器上跑，但要区分“开发”和“生产”：

| 方案 | 适合场景 | 做法 | 风险 |
|---|---|---|---|
| 单机 | 只有你自己维护、朋友很少、机器长期开机 | 生产跑 8853；开发预览跑 8854 + 临时 DB；改完先自检再重启 | 重启/改代码会影响正在使用的朋友；机器休眠/重启会中断 |
| 双机（推荐长期） | 开始频繁改代码、朋友变多、希望稳定 | 开发机写代码；生产机只跑稳定分支/tag + Tunnel | 需要迁移数据和维护两台机器 |

单机的最低要求：

- 不要直接改正在被计划任务使用的代码；先在 8854 + 临时 DB 验证。
- 不要同时启动两个抢 8853 的进程。
- 改完 `.env` / 代码后，按第 6 节重启，并看日志。

双机的基本原则：

- **生产机**跑 `Cloudflared` + `Qlib_App`，只部署稳定分支或 tag。
- **开发机**只在本机 `127.0.0.1:8854` 开发，不跑生产 Tunnel。
- **不要在两台机器上同时运行同一个 Tunnel token**；同一个 tunnel 多个连接器会分流流量，可能把公网请求送到开发机。
- 开发机如果要公网访问，另建一个 Tunnel 和 hostname（如 `qlib-dev.logoslab.cloud`），并单独建 Access 应用。

结论：**P0 阶段可以先单机跑通；等朋友开始常用、或你需要频繁改代码时，再拆成两台机器。**

---

## 1. 上线前备份（必做）

### 1.1 备份本机文件

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

说明：

- 首次上线时 `data/private/qlib_lab.db` 还不存在，复制不到是正常的。
- 如果应用正在运行，直接复制 SQLite 文件可能漏掉 WAL 里的最新写入。更稳妥的做法是先停应用再复制，或用下面的在线备份命令：

```powershell
.venv\Scripts\python.exe -c "import sqlite3; src=sqlite3.connect(r'data/private/qlib_lab.db'); dst=sqlite3.connect(r'data/private/qlib_lab.backup.db'); src.backup(dst); dst.close(); src.close()"
```

### 1.2 备份 Cloudflare Access 策略

登录 [one.dash.cloudflare.com](https://one.dash.cloudflare.com/)：

1. `Access` → `Applications` → 找到 `qlib-lab`。
2. 打开当前 `Allow` 策略，把现有邮箱列表截图或复制到本地。
3. 不要改 Tunnel 的外部路由；它应保持 `qlib.logoslab.cloud` → `http://localhost:8853`。

### 1.3 代码备份 / 提交

本次 P0 代码已在 2026-11-04 提交并推送到 `origin/main`。以后改动仍建议先提交，再上线：

```powershell
git status
git diff --stat

git add .gitignore .env.example README.md requirements.txt web notes
git commit -m "feat(web): ..."
git tag pre-deploy-$(Get-Date -Format yyyyMMdd)
```

> `web/broker_config.py` 和 `web/brokers/` 是模拟交易相关文件，已随 P0 一起提交。
> 以后如果不想把某些文件带进提交，先 `git add -p` 逐个确认，不要无脑 `git add -A`。

---

## 2. 安装依赖

```powershell
Set-Location C:\Work_Data\Source_Code_New\qlib-lab
.venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
```

如果只想装 P0 新增的包：

```powershell
.venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple `
  Flask==3.1.3 Flask-Login==0.6.3 waitress==3.0.2 cryptography==46.0.7
```

确认：

```powershell
.venv\Scripts\python.exe -c "import flask, flask_login, waitress, cryptography; print('deps ok')"
```

---

## 3. 配置 `.env`

### 3.1 创建或编辑

```powershell
Set-Location C:\Work_Data\Source_Code_New\qlib-lab
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

至少设置：

```ini
# 你的 Cloudflare Access 邮箱，必须和实际登录邮箱一致
QLIB_ADMIN_EMAIL=you@example.com

# 应用在 Cloudflare Tunnel 后面，保持 1
QLIB_TRUST_CF_HEADER=1

# 生产环境不要开启下面两项
# QLIB_ALLOW_DEV_LOGIN=1
# QLIB_DEV_EMAIL=you@example.com

# 可选：固定会话密钥；不设则自动写入 data/private/session_secret
# QLIB_SECRET_KEY=change-me-to-a-long-random-string
```

### 3.2 Alpaca 占位符要处理

`.env.example` 里的 `APCA_*` 是占位符：

```ini
APCA_API_KEY_ID=your_paper_key_id
APCA_API_SECRET_KEY=your_paper_secret_key
```

如果暂时不用模拟交易，**把这三行注释掉**：

```ini
# APCA_API_KEY_ID=...
# APCA_API_SECRET_KEY=...
# APCA_BASE_URL=https://paper-api.alpaca.markets
```

否则应用会以为“已配置”，实际请求 Alpaca 时才报认证失败。

### 3.3 检查

```powershell
Get-Content .env | Select-String 'QLIB_|APCA_'
```

不要把真实密钥贴到聊天或截图里。

> `.env` 必须在仓库根目录，不是 `web\.env`。
> `.env` 已被 gitignore；`.env.example` 只放占位符，可以提交。

---

## 4. 本地预演（不碰生产数据库）

这一步用临时 SQLite 和临时端口，不会影响 `data/private/qlib_lab.db`。

### 4.1 启动管理员预览实例

打开一个 PowerShell 窗口：

```powershell
Set-Location C:\Work_Data\Source_Code_New\qlib-lab

$env:QLIB_DB_PATH          = "$env:TEMP\qlib_p0_preview.db"
$env:QLIB_SECRET_KEY       = "preview-only-secret"
$env:QLIB_ALLOW_DEV_LOGIN  = "1"
$env:QLIB_DEV_EMAIL        = "admin@example.com"
$env:QLIB_ADMIN_EMAIL      = "admin@example.com"

Remove-Item -Force $env:QLIB_DB_PATH, "$env:QLIB_DB_PATH-wal", "$env:QLIB_DB_PATH-shm" -ErrorAction SilentlyContinue

.venv\Scripts\python.exe web\app.py --port 8854
```

浏览器打开 `http://127.0.0.1:8854/`：

- 应自动以 `admin@example.com` 登录；
- 侧栏应显示当前 `config/symbols.yaml` 的全部股票；
- 右上角能看到邮箱和「邀请码」入口。

进入 `http://127.0.0.1:8854/admin/invites` 生成一个邀请码，复制出来备用。

> 注意：本地预览只隔离了**用户库**（`QLIB_DB_PATH`）；持仓账本仍指向真实的
> `data/private/positions.json`。预演时不要点持仓的保存/删除；如果要把持仓也隔离，
> 先把 `data/private/positions.json` 备份并临时改名，或只在预览里测试自选/邀请码。

### 4.2 启动朋友预览实例（可选）

再开一个 PowerShell 窗口，用**同一个临时数据库**，但不同端口和不同开发邮箱：

```powershell
Set-Location C:\Work_Data\Source_Code_New\qlib-lab

$env:QLIB_DB_PATH          = "$env:TEMP\qlib_p0_preview.db"
$env:QLIB_SECRET_KEY       = "preview-only-secret"
$env:QLIB_ALLOW_DEV_LOGIN  = "1"
$env:QLIB_DEV_EMAIL        = "friend@example.com"
$env:QLIB_ADMIN_EMAIL      = "admin@example.com"

.venv\Scripts\python.exe web\app.py --port 8855
```

浏览器用 **`http://localhost:8855/`**（注意是 `localhost`，不是 `127.0.0.1`，这样两个站点的 cookie 不会互相干扰）：

1. 应跳到 `/invite`，显示 `friend@example.com`；
2. 输入刚才的邀请码；
3. 进入首页后侧栏应为空；
4. 点「我的自选」右侧 `＋`，从全量池添加 1~2 只股票；
5. 访问 `http://localhost:8855/admin/invites` 应返回 403；
6. 首页不应出现「模拟交易」，持仓位置应显示「P1 按用户隔离后开放」。

### 4.3 清理预览环境

两个窗口分别按 `Ctrl+C` 停止，然后：

```powershell
Remove-Item -Force "$env:TEMP\qlib_p0_preview.db", "$env:TEMP\qlib_p0_preview.db-wal", "$env:TEMP\qlib_p0_preview.db-shm" -ErrorAction SilentlyContinue
Remove-Item Env:QLIB_DB_PATH, Env:QLIB_SECRET_KEY, Env:QLIB_ALLOW_DEV_LOGIN, Env:QLIB_DEV_EMAIL, Env:QLIB_ADMIN_EMAIL -ErrorAction SilentlyContinue
```

---

## 5. Cloudflare Zero Trust 配置

Cloudflare 的完整逐步操作（含 Tunnel 检查、Access 应用、策略、OTP、测试、排查、回滚）
已经单独写成：

> **[`notes/cloudflare_access_step_by_step.md`](cloudflare_access_step_by_step.md)**

这里只放最短路径：

### 5.1 第一次上线：先小范围

1. 确认 `Cloudflared` 服务运行，Tunnel 状态 `Healthy`。
2. 确认 Tunnel 的 Public Hostname 是：
   `qlib.logoslab.cloud` → `http://localhost:8853`。
3. `Access` → `Applications` → `qlib-lab` → `Policies`。
4. 先删掉/停用旧的 `Bypass` 或 `Allow Everyone`。
5. 新建策略：
   - Action: `Allow`
   - Include: `Emails`
   - 填入你的管理员邮箱 + 1 个测试朋友邮箱
6. `One-time PIN` **不在 Policies 里**；到 `Integrations` → `Identity providers`
   添加，再回到应用的 `Authentication` 里选中。
7. 用无痕窗口测试：管理员进首页，测试朋友进 `/invite`。

### 5.2 确认没问题后：开放自助邀请

1. 编辑 `Allow` 策略。
2. 把 `Include` 从 `Emails` 改成 `Everyone`。
3. 保存。
4. 确认没有 `Bypass` 策略。
5. 无痕窗口用另一个邮箱测试：应进入 `/invite`，没有邀请码看不到数据。

> 改成 `Everyone` 后，任何能通过邮箱 OTP 的人都能到达应用，但**没有邀请码仍然看不到数据**。
> 如果出问题，把 `Include` 改回原来的 `Emails` 列表即可回滚。

### 5.3 不要动的东西

- Tunnel 外部路由：`qlib.logoslab.cloud` → `http://localhost:8853`。
- 应用监听地址：`127.0.0.1:8853`。
- 不要把 8853 直接映射到公网。
- 不要用 `Bypass` 策略；它会让 Flask 收不到邮箱头。

### 5.4 预期结果

| 访问者 | 预期 |
|---|---|
| 管理员邮箱 | 通过 CF 验证 → 直接进入首页 |
| 未准入邮箱 | 通过 CF 验证 → 跳到 `/invite` 输入邀请码 |
| 未通过 CF 验证 | 停在 Cloudflare 登录页，到不了应用 |
| 已准入普通用户 | 直接进入首页，只看到自己的自选 |
| 普通用户访问 `/admin/invites` | 403 |

---

## 6. 部署与重启

### 6.1 先跑自检

```powershell
Set-Location C:\Work_Data\Source_Code_New\qlib-lab
.venv\Scripts\python.exe web\selftest.py
```

期望：

```text
108 PASS / 0 FAIL
```

自检使用临时数据库和临时持仓目录，不会污染生产数据。

### 6.2 重启应用

```powershell
# 1) 杀掉旧的 app.py 进程
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'app.py' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# 2) 通过计划任务重新拉起
schtasks /End /TN Qlib_App
schtasks /Run /TN Qlib_App

# 3) 等 10 秒
Start-Sleep -Seconds 10
```

### 6.3 本机检查

```powershell
curl.exe -s -o NUL -w "%{http_code} %{redirect_url}`n" http://127.0.0.1:8853/healthz
curl.exe -s -o NUL -w "%{http_code} %{redirect_url}`n" http://127.0.0.1:8853/
```

预期：

- `/healthz` 返回 `200`。
- `/` 在没有 Cloudflare Header 的本机请求下返回 `302`，跳到 `/login`。
  如果你看到 `200` 且页面是登录说明页，也正常。

### 6.4 看启动日志

```powershell
Get-Content web\logs\app.log -Tail 60
```

重点看有没有这些警告：

| 日志 | 含义 | 处理 |
|---|---|---|
| `未配置 QLIB_ADMIN_EMAIL` | 没有管理员，无法生成邀请码 | 在 `.env` 设置后重启 |
| `已开启本地开发登录` | 生产环境误开了 dev login | 删除 `QLIB_ALLOW_DEV_LOGIN` / `QLIB_DEV_EMAIL` 后重启 |
| `非回环监听且信任 Cf-Access Header` | 应用直接暴露，Header 可能被伪造 | 改回 `127.0.0.1` 或关闭 Header 信任 |
| `Address already in use` | 8853 被占用 | 杀掉旧进程后重启 |

### 6.5 检查进程

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'app.py' } |
  Select-Object ProcessId, CreationDate, CommandLine
```

---

## 7. 首次管理员登录

1. 用正常浏览器打开 `https://qlib.logoslab.cloud`。
2. 通过 Cloudflare Access 用 `QLIB_ADMIN_EMAIL` 里的邮箱验证。
3. 预期直接进入首页，右上角显示你的邮箱。
4. 侧栏「我的自选」应显示当前全部股票（首次建 admin 时从 `config/symbols.yaml` 继承）。
5. 打开 `https://qlib.logoslab.cloud/admin/invites`，应能看到生成邀请码页面。

### 7.1 检查数据库

```powershell
.venv\Scripts\python.exe -c "import sqlite3; con=sqlite3.connect(r'data/private/qlib_lab.db'); print('users:', con.execute('select id,email,role,status from users').fetchall()); print('watchlist:', con.execute('select user_id,count(*) from user_watchlist group by user_id').fetchall())"
```

预期：

- 你的邮箱是 `role=admin`、`status=active`；
- 你的 `watchlist` 数量等于当前股票池数量。

### 7.2 如果没自动建 admin

按顺序检查：

1. `.env` 里 `QLIB_ADMIN_EMAIL` 是否和 CF 登录邮箱完全一致（大小写不敏感，但地址要一致）。
2. 修改 `.env` 后是否重启了应用（env 只在进程启动时读取）。
3. 是否从仓库根目录启动（`.env` 在根目录）。
4. 是否误设了 `QLIB_TRUST_CF_HEADER=0`。
5. 看 `web\logs\app.log` 是否有启动警告。

---

## 8. 邀请朋友并验收

### 8.1 生成邀请码

从 **`https://qlib.logoslab.cloud/admin/invites`** 生成，不要从 `127.0.0.1` 生成，否则复制出来的链接是内网地址。

建议：

- 可使用次数：`1`
- 有效期：`72 小时` 或更短

生成后复制链接，发给朋友。

### 8.2 朋友侧流程

1. 朋友打开链接，先通过 Cloudflare Access 邮箱验证。
2. 应用识别到新邮箱，跳到 `/invite`。
3. 输入邀请码，提交。
4. 自动创建普通用户并进入首页。
5. 侧栏为空，点 `＋` 从全量股票池添加自己的自选。

### 8.3 验收清单

用朋友的账号确认：

- [ ] 能正常看 K 线、异常、信号、股东、因子。
- [ ] 侧栏只显示自己添加的股票。
- [ ] `/admin/invites` 返回 403。
- [ ] `/api/positions` 返回 403。
- [ ] `/api/broker/status` 返回 403。
- [ ] 首页没有「模拟交易」区块。
- [ ] 持仓位置显示「P1 按用户隔离后开放」。

用管理员账号确认：

- [ ] 原来的持仓账本还在。
- [ ] 模拟交易区块还在。
- [ ] `/admin/invites` 能看到邀请码列表和用户列表。
- [ ] 邀请码用过一次后不能再次核销。

### 8.4 邀请码常见状态

| 状态 | 含义 | 处理 |
|---|---|---|
| 有效 | 还没过期、还没用完 | 可以继续用 |
| 已用完 | `used_count >= max_uses` | 重新生成 |
| 已过期 | 超过 `expires_at` | 重新生成 |
| 不存在 | 输错 / 被作废 | 核对或重新生成 |

---

## 9. 回滚方案

### 9.1 回滚应用代码

如果已经提交：

```powershell
git log --oneline -5
git revert <P0-commit>
# 然后按第 6 节重启
```

如果还没提交，直接用第 1 节的备份恢复 `web/` 和配置，再重启。

### 9.2 回滚 Cloudflare

把 `Access` → `Applications` → `qlib-lab` → `Policies` 的 `Include` 改回原来的 `Emails` 列表，保存。

### 9.3 重置应用用户数据

只在测试或确认不需要现有用户/邀请码时做：

```powershell
# 先停应用
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'app.py' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# 备份后删除
Copy-Item data\private\qlib_lab.db "data\private\qlib_lab.db.bak-$(Get-Date -Format yyyyMMdd-HHmmss)" -ErrorAction SilentlyContinue
Remove-Item -Force data\private\qlib_lab.db, data\private\qlib_lab.db-wal, data\private\qlib_lab.db-shm -ErrorAction SilentlyContinue

# 重新启动后，管理员下次登录会重新创建，并重新继承股票池
```

注意：

- 删除 DB 不会删除 `data/private/positions.json`，持仓账本不受影响。
- 删除 DB 会丢失所有用户、邀请码和每人的自选。
- 删除 `data/private/session_secret` 并重启应用，会让所有人退出登录，但不丢数据。

### 9.4 管理员被锁在外面

把 `QLIB_ADMIN_EMAIL` 改成你当前 CF 登录邮箱，重启应用。应用会在下次请求时把该邮箱提升为 admin（如果用户已存在）或直接创建 admin。

**不要**在生产环境设置 `QLIB_AUTH_DISABLED=1`；它只供 selftest 使用，会让任何请求都变成管理员。

---

## 10. 常见问题

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 本机访问 `/` 显示「未识别到访问身份」 | 没有 CF Header，且没开 dev login | 正常；通过公网 CF 访问，或本地按第 4 节开 dev login |
| 通过 CF 登录后仍显示「未识别到访问身份」 | `QLIB_TRUST_CF_HEADER=0`；Access 应用没覆盖该域名；应用没重启 | 设为 `1`，检查 Access 应用，重启 |
| 朋友一直停在 `/invite` | 没邀请码 / 邀请码过期 / 已用完 | 让管理员重新生成 |
| 朋友能进首页但侧栏为空 | 新用户默认空自选 | 点「我的自选」右侧 `＋` 添加 |
| 朋友也能看到持仓/模拟交易 | 应用没重启，还是旧代码 | 按第 6 节重启，确认进程启动时间 |
| 邀请链接是 `127.0.0.1` | 从本机地址生成了链接 | 从 `https://qlib.logoslab.cloud/admin/invites` 重新生成 |
| 公网 502 | 应用没起来 / 8853 没监听 | 看 `web\logs\app.log`，重启计划任务 |
| 管理员能看到「未配置 Alpaca」 | `.env` 没有真实 key 或 key 被注释 | 需要模拟交易就填真实 Paper key；不需要就忽略 |
| 退出后马上又自动登录 | Cloudflare 身份仍然有效，应用会话退出不等于 CF 退出 | 这是预期；要完全退出需清 CF Access 会话或关浏览器 |

---

## 11. 后续 P1 / P2

P0 只解决「用户 / 邀请码 / 每人自选」。下一步：

- **P1（建议优先）**
  - `user_broker` 加密存储 Alpaca Paper 密钥（Fernet + `QLIB_MASTER_KEY`）。
  - `/api/broker/*` 按当前用户取密钥，彻底移除全局 `.env` 兜底。
  - 持仓账本按用户隔离（`data/private/users/{user_id}/positions.json` 或 DB 表）。
  - 完成后移除 broker / positions 的 admin-only 限制。
- **P2**
  - `audit_log`：登录、生成/核销邀请码、下单、撤单、密钥变更。
  - 日志脱敏，确保不打印完整密钥和邀请码。

### 一个需要你决定的可选加固

当前版本**所有登录用户**都可以使用「添加股票」：它会写入全局 `config/symbols.yaml` 并触发抓数，新股票只自动进入发起人的自选，不会自动进入其他人的侧栏。

如果你希望 P0 阶段只有 admin 能修改全局股票池，需要再把 `/api/add_stock` 和前端「添加股票」按钮限制为 admin。这个不在当前 P0 代码里，需要额外改一处。

---

## 12. 本次涉及的主要文件

新增：

- `web/auth.py`：身份解析、Flask-Login、邀请码核销、admin 守卫。
- `web/db.py`：SQLite 用户 / 邀请码 / 自选表。
- `web/templates/login.html`：身份说明 / 退出页。
- `web/templates/invite.html`：邀请码输入页。
- `web/templates/admin_invites.html`：邀请码与用户管理。
- `.env.example`：新增 `QLIB_*` 配置示例。

修改：

- `web/app.py`：路由接入、自选 API、邀请码 API、admin-only 持仓/交易。
- `web/stocks.py`：添加股票任务归属、自动加入发起人自选。
- `web/templates/index.html`：用户信息、我的自选、管理弹窗、admin-only 区块。
- `web/static/app.js`：自选管理、401 跳转、持仓/交易区块守卫。
- `web/static/style.css`：P0 样式。
- `web/selftest.py`：临时 DB / 临时持仓目录，保持自检不污染生产。
- `README.md`、`requirements.txt`、`.gitignore`。

数据文件（均不进 git）：

- `data/private/qlib_lab.db`
- `data/private/session_secret`
- `data/private/positions.json`（P1 前仍为全局，仅 admin 可访问）

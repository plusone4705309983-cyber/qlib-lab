# Cloudflare Access 配置手册（qlib-lab 多用户 P0）

> 目标：让 `https://qlib.logoslab.cloud` 在到达 Flask 之前先经过 Cloudflare 登录，
> 并把登录邮箱通过 `Cf-Access-Authenticated-User-Email` 请求头传给应用。
>
> 适用环境：Windows + Cloudflare Tunnel + 自托管 Flask。
> 后台入口可能叫 **Zero Trust**，也可能叫 **Cloudflare One**；不同时间 UI 文案会略有变化，认准关键词即可。

---

## 0. 先理解三个东西

```text
浏览器
  │ 1. 访问 https://qlib.logoslab.cloud
  ▼
Cloudflare Access（登录页）
  │ 2. 邮箱 OTP / Google / GitHub 登录
  ▼
Cloudflare 边缘
  │ 3. 加上 Cf-Access-Authenticated-User-Email 请求头
  ▼
Cloudflare Tunnel（cloudflared，本机主动出站连接）
  │ 4. 转发到 http://localhost:8853
  ▼
Flask 应用（读请求头，决定 admin / 普通用户 / 邀请页）
```

- **域名 / DNS**：`qlib.logoslab.cloud` 由 Cloudflare 管理。
- **Tunnel**：本机 Windows 服务 `Cloudflared` 主动连 Cloudflare，不需要公网 IP / 80 / 443。
- **Access**：Cloudflare 的“门禁”，负责登录；登录成功后把邮箱放进请求头。
- **应用**：Flask 只信任这个请求头（`QLIB_TRUST_CF_HEADER=1`），不自己存密码。

**最重要的一句话**：

> 如果 Access 没有覆盖 `qlib.logoslab.cloud`，或者策略用了 `Bypass`，
> Flask 就收不到邮箱头，页面会显示“未识别到访问身份”。

---

## 1. 登录 Cloudflare 后台

1. 打开 <https://dash.cloudflare.com/>。
2. 用你的 Cloudflare 账号登录。
3. 如果账号下有多个域名，先点 `logoslab.cloud`。
4. 打开 <https://one.dash.cloudflare.com/>（Zero Trust / Cloudflare One）。
5. 如果第一次进要求设置 team name，使用现有的；不要为了这次改动新建第二个组织。

---

## 2. 检查 Tunnel 是否正常（只检查，不要改）

路径：

`Zero Trust` → `Networks` → `Tunnels`
（也可能在 `Cloudflare One` → `Networks` → `Tunnels`）

你要看到：

- 有一个隧道，状态是 `Healthy` / `Connected`。
- 点进去，`Public Hostname` 里有：
  - **Hostname**：`qlib.logoslab.cloud`
  - **Service**：`http://localhost:8853`（或 `http://127.0.0.1:8853`）
- **不要改**这里的域名和端口。
- **不要**手动加 A 记录；Tunnel 的 Public Hostname 会自动管理 DNS。

如果 Tunnel 不是 Healthy：

```powershell
Get-Service Cloudflared
Restart-Service Cloudflared
```

- token 在 `C:\ProgramData\cloudflared\token`，不要外传、不要提交 git。
- 如果服务启动失败，先看 Windows 服务日志；不要先改 Access。

---

## 3. 找到或创建 Access 应用

路径：

`Zero Trust` → `Access` → `Applications`
（也可能在 `Access controls` → `Applications`）

### 3.1 如果已经有 `qlib-lab` / `qlib.logoslab.cloud`

点进去，逐项检查：

| 项目 | 建议值 |
|---|---|
| Application name | `qlib-lab` |
| Application domain | `qlib.logoslab.cloud` |
| Path | 留空，或 `/*` |
| Session duration | `24 hours` 或 `1 week` |
| Identity providers | 至少勾选 `One-time PIN` |

如果域名或路径不对，点 `Edit` 修正并保存。

### 3.2 如果没有，创建 Self-hosted 应用

1. `Applications` → `Add an application`。
2. 选 **Self-hosted**。
3. Application name：`qlib-lab`。
4. Session duration：`24 hours`。
5. Application domain：
   - Subdomain：`qlib`
   - Domain：`logoslab.cloud`
   - Path：留空（推荐）或 `/*`
6. 如果界面有 `Identity providers` / `Authentication`，勾选 `One-time PIN`；
   如果没有这一项，不用卡住，先保存应用，后面到
   `Zero Trust` → `Settings` → `Authentication` 启用。
7. 保存。

> **注意：`One-time PIN` 不是 Access policy 里的选项。**
> Policy 管的是“谁可以进”（Allow + Include Emails），
> `One-time PIN` 管的是“用什么方式登录”（邮箱验证码），在 `Settings` → `Authentication` 里。

注意：

- 不要选 `SaaS`；qlib-lab 是自托管应用。
- 路径不要只写 `/admin`，否则首页可能不受保护。
- 应用保存后，状态应是 `Saved` / `Published`。

---

## 4. 配置 Access 策略

Access 策略决定“谁可以登录”。先分清三种动作：

| 动作 | 含义 | qlib-lab 是否使用 |
|---|---|---|
| `Allow` | 允许登录，登录后应用能收到邮箱头 | ✅ 使用 |
| `Bypass` | 完全跳过登录，**不会**有邮箱头 | ❌ 不要用 |
| `Service Auth` | 给机器/脚本用的服务令牌 | ❌ 浏览器用户不用 |

> **新手最容易混淆的地方：**
> `Access policies` 里选的是“谁可以进”，不是“用什么方式登录”。
> 第一次测试就选 `Allow` + `Include: Emails`，填管理员和测试朋友的邮箱。
> `One-time PIN` 是登录方式，不在这里选，去 `Settings` → `Authentication` 启用。

### 方案 A：先小范围测试（推荐第一次上线）

只允许管理员和 1 个测试朋友。

1. 进入应用 → `Policies`。
2. 如果已有 `Allow Everyone` 或 `Bypass` 策略，**先删除或停用**；否则它们会覆盖白名单。
3. `Add a policy`：
   - Policy name：`allow-admin-and-testers`
   - Action：`Allow`
   - Include：`Emails`
   - 邮箱框里填：
     ```text
     你的管理员邮箱@example.com
     测试朋友邮箱@example.com
     ```
   - 保存。
4. 确认应用已保存/发布。

### 方案 B：开放给朋友自助邀请（最终形态）

1. 进入应用 → `Policies`。
2. 删除/停用旧的 `Bypass` 和旧的 `Allow Everyone`，避免重复。
3. `Add a policy`：
   - Policy name：`allow-everyone`
   - Action：`Allow`
   - Include：`Everyone`
   - 保存。
4. 确认 `One-time PIN` 已启用（见第 5 节）。

> `Everyone` 不是“没有登录”。它表示“任何能通过你启用的身份验证方式登录的人”。
> 如果只启用 One-time PIN，就是任何有邮箱的人都能请求验证码。
> 应用层的邀请码仍然是第二道门：没有邀请码只能到 `/invite`，看不到数据。

---

## 5. 启用 One-time PIN

路径：

`Zero Trust` → `Settings` → `Authentication`
（也可能在 `Access` → `Authentication`）

1. 找到 `One-time PIN` / `OTP` / `Email one-time PIN`。
2. 打开开关。
3. 保存。
4. 如果使用 Google / GitHub 登录，也在这里配置；不配也可以，OTP 最简单。

> **不要和 `Authenticator application` 混淆。**
> `Authenticator application` 是 TOTP 验证器 App（Google Authenticator / Microsoft Authenticator 等），
> 需要用户扫码绑定，不是邮箱验证码。
> 你要找的是 `One-time PIN`，描述通常是“通过邮箱接收一次性验证码”。
>
> 如果列表里没有 `One-time PIN`，点 `Add new` / `Add an identity provider`，
> 在内置登录方式里选择 `One-time PIN`。启用后，还要回到 Access 应用的
> `Authentication` / `Identity providers` 里确认它被选中。

测试：

1. 打开**无痕窗口**。
2. 访问 `https://qlib.logoslab.cloud`。
3. 应出现 Cloudflare 登录页。
4. 输入邮箱，点发送验证码。
5. 收邮件，输入验证码（位数以邮件为准）。
6. 登录后：
   - 管理员邮箱 → 应用首页；
   - 未邀请邮箱 → 应用 `/invite`；
   - 不在白名单（方案 A）→ Cloudflare 拒绝页。

如果没收到验证码：

- 检查垃圾邮件；
- 确认邮箱拼写；
- 确认 `One-time PIN` 已启用；
- 换一个邮箱再试。

---

## 6. 测试矩阵

| 测试 | 操作 | 预期 |
|---|---|---|
| Tunnel | 打开 `https://qlib.logoslab.cloud` | 出现 Cloudflare 登录页，不是 502 |
| 管理员 | 用 `QLIB_ADMIN_EMAIL` 邮箱登录 | 进入首页，右上角显示邮箱 |
| 未邀请朋友（Everyone） | 用另一个邮箱登录 | 跳到 `/invite` |
| 白名单外（方案 A） | 用未列出的邮箱登录 | Cloudflare 拒绝 |
| 邀请码 | 输入管理员生成的码 | 进入首页，侧栏为空 |
| 普通用户权限 | 访问 `/admin/invites` | 403 |
| 普通用户交易 | 访问 `/api/positions` | 403 |
| 邀请链接 | 从公网后台复制 | 域名是 `https://qlib.logoslab.cloud/...`，不是 `127.0.0.1` |

### 清除旧登录态

Cloudflare Access 的登录态和应用登录态是两层。测试新策略时：

- 用无痕窗口；或
- 访问 `https://qlib.logoslab.cloud/cdn-cgi/access/logout` 退出 Access；或
- 清除 `qlib.logoslab.cloud` 的 cookie。

---

## 7. 排查

### 7.1 Cloudflare 登录成功，但应用显示“未识别到访问身份”

按顺序检查：

1. Access 应用的域名是不是 `qlib.logoslab.cloud`，路径是不是空或 `/*`。
2. 策略动作是不是 `Allow`，不是 `Bypass`。
3. 应用 `.env` 是不是 `QLIB_TRUST_CF_HEADER=1`。
4. 改完 `.env` 后有没有重启应用。
5. 你是不是直接访问了 `http://127.0.0.1:8853`（本机直连没有 CF 头）。
6. 看 `web\logs\app.log` 是否有启动警告。

### 7.2 一直回到 Cloudflare 登录页

- 清除 cookie / 换无痕。
- 检查 `One-time PIN` 是否启用。
- 检查 Access 应用是否保存。
- 看 `Zero Trust` → `Logs` → `Access` 的请求记录。

### 7.3 502 Bad Gateway

- 应用没启动：`Get-Content web\logs\app.log -Tail 60`。
- 端口不对：Tunnel 的 Service 应是 `http://localhost:8853`。
- 应用没监听：

  ```powershell
  curl.exe -s -o NUL -w "%{http_code}`n" http://127.0.0.1:8853/healthz
  ```

- 计划任务没拉起：

  ```powershell
  schtasks /Run /TN Qlib_App
  ```

### 7.4 Cloudflare 显示 Access denied

- 邮箱不在 `Include` 列表（方案 A）。
- 策略没保存。
- 有更早的 `Bypass` / `Allow Everyone` 策略覆盖。
- 登录的邮箱和你以为的不一样（Google 账号可能是另一个邮箱）。

### 7.5 邀请链接是 127.0.0.1

- 你是从 `http://127.0.0.1:8853/admin/invites` 生成的。
- 从 `https://qlib.logoslab.cloud/admin/invites` 重新生成。

---

## 8. 回滚

1. `Access` → `Applications` → `qlib-lab` → `Policies`。
2. 把策略改回原来的邮箱列表；或删除 `allow-everyone`，恢复 `allow-admin-and-testers`。
3. 保存。
4. 用无痕窗口验证。
5. 不需要停 Tunnel，也不需要改 DNS。

如果只是想临时关闭 Access：

- **不建议直接删应用**。删了应用，Flask 收不到邮箱头，所有人会看到“未识别到访问身份”。
- 正确做法是恢复策略，而不是删应用。

---

## 9. 双机部署时怎么配

### 9.1 单机（当前）

- 生产应用和 Tunnel 都在同一台 Windows。
- 开发预览用另一个端口（如 8854）+ 临时 DB，不要和 8853 共用。
- 不要让开发进程和计划任务抢 8853。

### 9.2 双机（推荐长期）

- **生产机**：跑 `Cloudflared` + `Qlib_App`，只跑稳定分支 / tag。
- **开发机**：只在本机 `127.0.0.1:8854` 开发，不跑生产 Tunnel。
- **不要在两台机器上同时运行同一个 Tunnel token**；同一个 tunnel 多个连接器会分流流量，可能把请求送到开发机。
- 如果开发机也要公网访问，创建**另一个** Tunnel 和**另一个** hostname，例如
  `qlib-dev.logoslab.cloud`，并单独建 Access 应用。
- 生产数据迁移：复制 `config/symbols.yaml`、`data/selfmade`、`data/_raw_cache`、
  `data/private`（`positions.json`、`qlib_lab.db`、`session_secret`）和 `.env` 到新机器；
  或只迁移行情数据，用户库重新开始。

---

## 10. 上线检查清单

- [ ] `Cloudflared` 服务运行，Tunnel `Healthy`
- [ ] Public Hostname `qlib.logoslab.cloud` → `http://localhost:8853`
- [ ] Access 应用域名 `qlib.logoslab.cloud`，路径空或 `/*`
- [ ] 策略动作 `Allow`
- [ ] 方案 A：`Include: Emails` 包含管理员和测试朋友
- [ ] 方案 B：`Include: Everyone`，且没有 `Bypass`
- [ ] `One-time PIN` 已启用
- [ ] `QLIB_ADMIN_EMAIL` 与登录邮箱一致
- [ ] `.env` 里 `QLIB_TRUST_CF_HEADER=1`
- [ ] 生产没有 `QLIB_ALLOW_DEV_LOGIN` / `QLIB_DEV_EMAIL`
- [ ] 无痕窗口测试管理员和普通用户
- [ ] 邀请链接从公网后台生成
- [ ] 应用日志无启动警告

---

## 11. 名词速查

| 名词 | 说明 |
|---|---|
| Zero Trust / Cloudflare One | Cloudflare 的访问控制后台 |
| Access Application | 保护某个域名的“应用”配置 |
| Policy | 谁可以登录、允许还是绕过 |
| Include | 策略里的“包含条件”，如 Emails / Everyone |
| One-time PIN | 邮箱一次性验证码登录 |
| Tunnel | 本机 `cloudflared` 到 Cloudflare 的出站连接 |
| Public Hostname | Tunnel 暴露给公网的域名和本地服务 |
| `Cf-Access-Authenticated-User-Email` | Cloudflare 登录后注入的邮箱请求头 |
| Bypass | 跳过登录，不会有邮箱头；qlib-lab 不要用 |

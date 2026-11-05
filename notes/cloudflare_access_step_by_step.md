# Cloudflare 只保留 Tunnel（不再使用 Access）

> 本文件替代旧的 Cloudflare Access 配置手册。
> 从应用内登录上线后，Cloudflare 只负责 HTTPS 转发；登录由 Flask 自己做。

---

## 1. 目标架构

```text
浏览器
  │ https://qlib.logoslab.cloud
  ▼
Cloudflare Tunnel
  │ http://localhost:8853
  ▼
Flask 应用
  └─ 邮箱 + 密码 + 邀请码
```

Cloudflare 侧只需要：

- 一个 Tunnel；
- 一个公网 hostname；
- 一条到 `http://localhost:8853` 的路由。

不需要：

- Access 应用；
- Access 策略；
- One-time PIN；
- 邮箱验证码；
- Gmail / Google 账号。

---

## 2. 保留 Tunnel

进入 Cloudflare Zero Trust：

```text
Networks → Tunnels
```

确认原来的 Tunnel 仍在运行，且 Public Hostname 是：

```text
qlib.logoslab.cloud  →  http://localhost:8853
```

不要改成本机的公网 IP，也不要把 8853 直接暴露到公网。

本机检查：

```powershell
Get-Service Cloudflared
Invoke-WebRequest http://127.0.0.1:8853/healthz -UseBasicParsing
```

---

## 3. 删除或禁用 Access 应用

进入：

```text
Access → Applications
```

找到 `qlib-lab`（或绑定 `qlib.logoslab.cloud` 的那个应用）：

- **推荐**：删除；
- 或者：把策略改成 `Bypass`。

### 为什么必须处理

如果保留 `Allow + 邮箱 OTP`：

1. 朋友打开站点；
2. 先被 Cloudflare 要求收 6 位验证码；
3. 大陆网络下 Gmail 经常收不到；
4. 即使收到，也只是到了应用登录页，还要再输入应用密码。

两套登录叠加，既慢又容易失败。P0 不需要它。

### 删除 Access 后安全吗

安全边界变成：

- Cloudflare Tunnel 隐藏源站 IP；
- HTTPS 由 Cloudflare 提供；
- 应用自己校验邮箱 + 密码；
- 密码只存哈希；
- 登录失败有限速；
- 邀请码一次性；
- admin 路由和持仓/模拟交易接口有权限校验。

对 P0 的小范围朋友使用足够。后续如果需要更强的企业级门禁，再单独评估。

---

## 4. 验证

删除 Access 应用后，用无痕窗口打开：

```text
https://qlib.logoslab.cloud/login
```

预期：

- 直接看到应用登录页；
- 不再出现 Cloudflare 邮箱验证码；
- 输入管理员邮箱 + 密码能进入；
- `/admin/invites` 能打开；
- 普通用户访问 `/admin/invites` 返回 403；`/api/positions` 返回 200（仅自己的账本）；`/api/broker/status` 在未配置自己的 Alpaca 密钥时返回 400。

---

## 5. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 仍然跳 Cloudflare 登录 | Access 应用没删干净 | 检查 Applications 里是否还有覆盖该域名的应用 |
| 502 Bad Gateway | Tunnel 或应用没起来 | 检查 `Cloudflared`、`Qlib_App`、`web\logs\app.log` |
| 登录后马上跳回 `/login` | 本机用 http 但 `QLIB_COOKIE_SECURE=1` | 本机调试临时设 `0`；生产保持 `1` |
| 管理员密码错误 | `.env` 密码与数据库不一致 | 用 `web\set_password.py` 重置 |
| 邀请码无效 | 已使用/过期/输错 | 在 `/admin/invites` 重新生成 |

---

## 6. 可选：保留 Access 但设 Bypass

如果暂时不想删除应用，可以把策略改成：

```text
Action: Bypass
Include: Everyone
```

效果是 Cloudflare 不再要求登录，请求直接到 Flask。

但更推荐删除，避免以后忘记这条 Bypass 策略，也避免误以为 Cloudflare 还在做登录保护。

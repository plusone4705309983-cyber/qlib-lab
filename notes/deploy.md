# 部署与发布手册（Windows 原生 + Cloudflare 隧道）

> ⚠️ 本文件为 **2026-10-19 重建版**（原版丢失，从未提交 git）。
> 所有命令均在 2026-10-19 实际验证过。**这份文件已提交 git，以后改动记得一起提交。**

域名固定为 **`https://qlib.logoslab.cloud`**。
本机无需公网 IP / 80 / 443 端口：`cloudflared` 主动**出站**连到 Cloudflare 边缘，CF 再把流量转发回 `127.0.0.1:8853`。
关掉这台机器的访问入口 = 停掉 `Cloudflared` 服务。

---

## 0. 现状速查（2026-10-19 核实）

| 项 | 值 | 说明 |
|---|---|---|
| 域名 / 入口 | `https://qlib.logoslab.cloud` | 每次改后端/静态资源后**必须重启应用**（见 1.2） |
| 应用端口 | `http://127.0.0.1:8853` | 只监听回环，外网只能走隧道 |
| 应用进程 | 计划任务 `Qlib_App`（登录时触发）→ `wscript web\start_hidden.vbs` → `web\run.bat` → waitress `127.0.0.1:8853` | vbs 是"发射后不管"（Run 0,False），任务本身瞬间结束，python 进程独立存活 |
| 隧道服务 | Windows 服务 `Cloudflared`，开机自启 | 二进制 `C:\Program Files (x86)\cloudflared\cloudflared.exe`，token 在 `C:\ProgramData\cloudflared\token` |
| 隧道外部路由 | `qlib.logoslab.cloud` Type HTTP → `http://localhost:8853` | 在 CF Zero Trust 后台看，**别乱动** |
| SSL | Cloudflare 没有做 SSL-拉平（拉平值为空） | 只记录，不建议改 |
| 后端日志 | `web\logs\app.log` | `run.bat` 重定向 waitress/Flask/loguru 全部输出 |
| 真实数据 | `data/selfmade/{cn,us}/*.csv`（**CSV = 唯一事实来源**） | bin 由 CSV 确定性重建；bin 不进 git |
| 原始缓存 | `data/_raw_cache/*.json` | **有意提交**，离线可复现 CSV |
| Python | `%ROOT%\.venv\Scripts\python.exe` | `run.bat` 自动优先选 venv |

---

## 1. 常用命令

（PowerShell；涉及网络/qlib 的命令超时一律设长，如 `-TimeoutSec 300`）

### 1.1 先看当前是否活着

```powershell
Set-Location C:\Work_Data\Source_Code_New\qlib-lab

# 应用是否在跑
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'app.py' } | Select-Object ProcessId, CreationDate, CommandLine

# 隧道是否在跑（STATE 应为 4 RUNNING）
sc.exe query Cloudflared

# 本机 / 公网入口
Invoke-WebRequest "http://127.0.0.1:8853/" -UseBasicParsing -TimeoutSec 30
Invoke-WebRequest "https://qlib.logoslab.cloud/" -UseBasicParsing -TimeoutSec 60
```

### 1.2 重启应用（**必须**，改完代码/静态资源必须重启）

```powershell
# 1) 杀掉旧的 waitress/app.py 进程
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'app.py' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# 2) 通过计划任务重新拉起（先 /End 清残留状态再 /Run）
schtasks /End /TN Qlib_App
schtasks /Run /TN Qlib_App

# 3) 等 10 秒左右，验证本机 200 + 公网 200
Start-Sleep -Seconds 10
Invoke-WebRequest "http://127.0.0.1:8853/" -UseBasicParsing -TimeoutSec 30
Invoke-WebRequest "https://qlib.logoslab.cloud/" -UseBasicParsing -TimeoutSec 60
```

### 1.3 隧道（一般不用碰）

```powershell
sc.exe query Cloudflared
# 重启隧道（重启后公网会有约 1 分钟 200/ERR 交替抖动，属正常）：
# sc.exe stop Cloudflared & sc.exe start Cloudflared
```

### 1.4 自检（发布前后各跑一遍）

```powershell
.venv\Scripts\python.exe web\selftest.py --timeout 300000
# 期望全 PASS（2026-10-19 基线：56 PASS / 0 FAIL）
```

---

## 2. 发布流程（改代码后）

1. `git add` → **代码一个 commit、数据一个 commit**（`data: ...`），一起 push
2. 跑 `web\selftest.py`（见 1.4）
3. 重启应用（见 1.2）
4. 公网抽查 `https://qlib.logoslab.cloud`

> GitHub push 在这台机器上**经常连接超时/被重置**，失败就隔几秒重试，通常 1~10 次内能成功。不做重试机制，手动重试即可。

---

## 3. 数据刷新（CSV → bin）

```powershell
Set-Location C:\Work_Data\Source_Code_New\qlib-lab

# 1) 重建 CSV（价格源=腾讯，vwap 成交额源=东财；全部走本地缓存可离线复现）
.venv\Scripts\python.exe -u scripts\east.py --market cn --source tencent --throttle 0
.venv\Scripts\python.exe -u scripts\east.py --market us --source tencent --throttle 0

# 2) 重建 bin（dump_all 会自动带上 CSV 新增列，如 vwap）
.venv\Scripts\python.exe -u scripts\dump_bin.py dump_all --data_path data/selfmade/cn --qlib_dir data/selfmade/cn_bin --freq day --exclude_fields date,symbol --file_suffix .csv --max_workers 4
.venv\Scripts\python.exe -u scripts\dump_bin.py dump_all --data_path data/selfmade/us --qlib_dir data/selfmade/us_bin --freq day --exclude_fields date,symbol --file_suffix .csv --max_workers 4

# 3) 自检 + 重启（bin 变了网页读的是新文件，仍需重启生效）
```

新增股票走网页 `/api/add_stock`（`web\stocks.py` 后台线程会自动：拉数据 → 写 CSV → dump_bin → 重建 provider），同时记得在 `config/symbols.yaml` 登记。

---

## 4. 坑与注意

- **缓存策略**：`app.py` 设了 `SEND_FILE_MAX_AGE_DEFAULT = 0` + `_no_cache` 响应头 → 浏览器不缓存，但**后端进程内**的模块级缓存（股东数据 6h 等）只有重启才清。所以"改完必须重启"是铁律。
- **东财 `push2*` 系列会被限流**：`push2his`/`push2` 等直接 RemoteDisconnected，`datacenter-web` 通常还活着（股东穿透用它）。`east.py` 对成交额请求有快速熔断（`_AMOUNT_DOWN`），失败后剩余股票直接走典型价回退，不会每只卡 2 分钟。恢复后重跑 east.py + dump_bin 即可把 vwap 升级为真实成交均价。
- **数据口径**：CSV 是后复权（HFQ）价，`volume` 是股数（A股已由手换算成股），`trade = price / factor`，`vwap` 也是后复权口径。qlib 里不要再除 `$factor`。
- **别手改 bin**：一切从 CSV 重建。bin 在 `.gitignore` 里。
- **`data/_raw_cache/` 有意提交**：保证离线可复现、审计数据来源，不要清理。
- 计划任务 `Qlib_App` 的触发器是"登录时"：机器重启后**必须有人登录**应用才会拉起（隧道服务是 AUTO_START，会自己起来，但没应用可转发就是 502）。

# qlib-lab

Windows 原生的 [Qlib](https://github.com/microsoft/qlib) 基础实验环境。从零自建 A 股 / 美股日线数据，
`dump_bin` 入库后可直接查询，用于理解 Qlib 的数据结构和表达式引擎。

纯 CPU，8 GB 内存可跑，不需要 WSL、不需要管理员权限。

---

## 快速开始

```powershell
git clone https://github.com/plusone4705309983-cyber/qlib-lab.git
cd qlib-lab

python -m venv .venv
.venv\Scripts\activate.bat
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# 验证数据完整性
python explore\00_verify.py
```

预期输出：CN / US 各 4 只标的，0 NaN，`SH600519` 的 `$close/$factor` 还原为真实成交价。

---

## 数据

4 只 A 股 + 4 只美股，全历史日线（1991 ~ 2026）。

| 市场 | 标的 | 行数 | 区间 |
|---|---|---|---|
| CN | `SZ000001` 平安银行 | 8542 | 1991-01-02 ~ 2026-09-28 |
| CN | `SH600519` 贵州茅台 | 6012 | 2001-08-27 ~ 2026-09-28 |
| CN | `SH601318` 中国平安 | 4694 | 2007-03-01 ~ 2026-09-28 |
| CN | `SZ300750` 宁德时代 | 2016 | 2018-06-11 ~ 2026-09-28 |
| US | `AAPL` | 4912 | 2007-03-19 ~ 2026-09-25 |
| US | `MSFT` | 4912 | 2007-03-19 ~ 2026-09-25 |
| US | `NVDA` | 4277 | 2009-09-16 ~ 2026-09-25 |
| US | `TSLA` | 3911 | 2011-01-26 ~ 2026-09-25 |

数据取自腾讯和东方财富的公开行情接口，不依赖任何需要 API key 的服务。

### 目录结构

```
data/
├── selfmade/
│   ├── cn/  us/        # 行情 CSV —— 数据本体，唯一事实来源
│   └── cn_bin/ us_bin/ # Qlib provider，由 CSV 确定性重建（已 gitignore）
└── _raw_cache/         # API 原始 JSON 响应，故意提交（见下）
```

---

## 复权：最容易踩的坑

Qlib 的字段契约要求 **OHLC 四个字段全部是复权价**：

| 字段 | 含义 |
|---|---|
| `$open` `$high` `$low` `$close` | **后复权价**，跨越分红送转可比 |
| `$factor` | 后复权价 / 原始价 |
| `任意价格字段 / $factor` | **真实成交价**，与券商 App 一致 |

两个必须知道的事实：

**1. 必须用后复权，不能用前复权。** 实测东方财富和腾讯的前复权算法在长历史上会算出**负价格**
（`SH600519` 6013 行里有 3529 行 `close ≤ 0`）—— 两家是同一套「累减累计分红」实现，
分红史一长就溢出成负数。后复权是乘法累乘，不会变负。

**2. OHLC 必须与 `$close` 同口径。** 混用会得到**静默错误**——不报错、不崩溃，只是结果偏小。
本项目曾犯过这个错：只把 `close` 复权而 O/H/L 取 raw，导致：

```
振幅   ($high-$low)/$close     0.381%   <- 错
正确                             2.143%          偏小 5.6 倍
成交额 $close*$volume        338.6 亿   <- 错
正确 ($close/$factor)*vol      54.2 亿   <- 对
```

`explore/00_verify.py` 现在有一条自动检测：**`$close` 必须落在 `[$low, $high]` 区间内**。
这个不变式在两种口径混用时必然被破坏，实测能检出 1999/2000 行。

### 涉及绝对价格的指标

复权价是历史价格重标定后的结果，与当下的钱无关。凡是计算**成交额、市值、均价**的地方，
必须先用 `$field/$factor` 还原成真实价，否则会虚高 `factor` 倍（茅台 6.2 倍）。

### 已知限制

- **美股 `factor` 恒为 1.0**。腾讯和东财的美股接口都不提供复权数据，只给原始价。
  涉及分红回补的美股策略需要另找数据源。
- **日历不是正式交易日历**。`calendars/day.txt` 是所有 CSV 日期的并集，不做节假日校正
  （所以会出现 `1991-01-05` 这种周六）。用于回测前需自行补正式交易日历。

---

## 概念脚本

```powershell
python explore\01_calendar.py       # 交易日历：.bin 长度 = 日历长度
python explore\02_instruments.py    # 股票池：all.txt 与 fname_to_code 映射
python explore\03_features.py       # 特征字段：原始字段 vs 表达式
python explore\04_expressions.py    # 表达式引擎：时序 / 截面算子
python explore\05_cross_market.py   # 多市场：region 是全局配置，须分两次 init
```

路径统一从 `explore/_common.py` 派生，数据源和 provider 目录读自 `config/symbols.yaml`，
代码中没有硬编码绝对路径，**目录可任意搬移**。

### 三个容易写错的地方

```python
D.features(["SH600519"], ["$close/Ref($close,1)-1"])   # ✓ 收益率
D.features(["SH600519"], ["Ref($close,1)/$close-1"])   # ✗ 恒等于 0

D.features(syms, ["Rank($close/Ref($close,1)-1, 4)"]) # ✓ Rank 必须显式给 N
D.features(syms, ["Rank($close/Ref($close,1)-1)"])    # ✗ TypeError
```

`Ref($close, 1)` 是「日历往前挪 1 位」，不是「昨天」—— 有停牌和节假日时与自然日不等价。

另外，Qlib 内部用 joblib 多进程，**Windows 上凡是用到 `Rank` / `Mean` 就必须加
`if __name__ == "__main__":` 守卫**，否则 spawn 会重复导入 `__main__` 导致启动即崩。

---

## 数据检视网站

`web/` 是一个以只读浏览为主的 Web 应用：把 CSV 数据画成 K 线，并自动标注异常——
比逐行对数字更容易发现问题。另有一个受控的「添加股票」入口（见下）。

```powershell
.venv\Scripts\python.exe web\app.py       # 或双击 web\run.bat
# 浏览器打开 http://127.0.0.1:8000
```

三个视图：**真实价 K 线**（`$price / $factor`，与券商 App 一致，可直接和东财/腾讯网页对照）、
**后复权 K 线**、**因子阶梯图**（后复权因子应单调不减，向下跳变即标红）。左侧切换股票，
工具栏可切「真实价 / 后复权」、对数轴、显示/隐藏异常，并可点「校验 .bin」抽查
provider 与 CSV 是否一致。

自动检测的异常：

| 类型 | 判据 | 级别 |
|---|---|---|
| `factor_drop` | 复权因子环比下调 > 1% | 错误 |
| `close_out_of_range` | `$close` 不在 `[$low, $high]` | 错误 |
| `missing_day` | 区间内该股缺该交易日（可能停牌） | 提示 |
| `big_move` | 真实价单日涨跌超阈值（A股 10.5% / 美股 25%） | 提示 |
| `us_unadjusted_split` | 美股 factor≡1 且价格跳变（疑似未复权拆股） | 提示 |

自检：`.venv\Scripts\python.exe web\selftest.py`

### 添加股票

工具栏「＋ 添加股票」：输入代码或名称（如 `600519` / `AAPL` / `茅台`）搜索，
东方财富 suggest 接口会自动带出 `secid`、市场、名称与腾讯代码，确认后即可加入。
后台依次执行：写入 `config/symbols.yaml` → 抓取全历史行情（腾讯源，行数异常时自动
改走东财源）→ `dump_bin` 重建 provider；进度实时回显，约需 1~2 分钟。

- 写入前校验代码/名称/`secid` 格式；重名或 `secid` 已存在会拒绝；任一步失败自动回滚配置。
- 美股腾讯代码按交易所自动加后缀（NASDAQ `.OQ` / NYSE `.N` / AMEX `.A`）。
- 仅本机 `127.0.0.1` 监听，写入接口同样只经 Cloudflare Access 登录后方可访问。

> 默认只监听 `127.0.0.1`（回环）。公网访问经 Cloudflare Tunnel 转发，
> 由 Cloudflare Access 做邮箱登录，不直接暴露端口。

---

## 重新抓取数据

CSV 已随仓库提供，通常不需要重跑。需要更新到最新行情时：

```powershell
# 抓取（有磁盘缓存，重复运行不会重复请求）
python scripts\east.py --market cn --source tencent
python scripts\east.py --market us --source tencent

# 重建 Qlib provider
python scripts\dump_bin.py dump_all --data_path data\selfmade\cn --qlib_dir data\selfmade\cn_bin `
    --freq day --exclude_fields date,symbol --file_suffix .csv --max_workers 4
```

`scripts/east.py` 处理了三层网络问题：运行时 DoH 绕开 DNS 污染、IPv4 优先（本地 IPv6 出口
常不可用）、IP 池轮换加退避重试。东财按 IP 限流较严，故接入腾讯作为备用源；
美股代码需带交易所后缀（`usAAPL.OQ`）。

### 为什么提交 `_raw_cache`

那 7.66 MB 的 JSON 是本项目**唯一不可再生的数据层**。丢了就只能重调 API，
而数据源随时可能追溯修订历史数据（分红送转会调整），届时拿到的是另一份数据。
留着它才能在 `east.py` 的复权逻辑出问题时**离线重算 CSV**，不必赌「API 今天返回的
和上次一样」。

---

## 环境说明

`pyqlib==0.9.7` + `numpy 2.2.6` + `pandas 2.3.3`，Windows 原生 wheel，无需编译。

直接 `pip install pyqlib` 会在 `mlflow<3.13` 这类松散约束上依赖回溯、卡死十几分钟。
若遇到，请分批安装：先装无约束核心包，再单独装 mlflow，最后 `pip install --no-deps pyqlib`。

---

## 已知取舍

- 只做日线，4 只标的，够理解数据结构但**不能用来训模型**。
- 没有特征工程、模型训练、回测框架 —— 本仓库的目标是把 Qlib 的数据层和查询层讲清楚。
- 官方数据集（`SunsetWolf/qlib_dataset`）已弃用且停留在 2024-05，未使用。
  真正要训模型时建议改用 [chenditc/investment_data](https://github.com/chenditc/investment_data)。
- `.venv/`（833 MB）和 `*_bin/`（1.06 MB，由 CSV 确定性重建）不入库。

---

## 目录导航

| 路径 | 说明 |
|---|---|
| `config/symbols.yaml` | 标的清单，A股/美股双源 secid |
| `scripts/east.py` | 抓数层：DoH、IPv4 优先、限流重试、磁盘缓存 |
| `scripts/dump_bin.py` | Qlib 官方 v0.9.7 脚本 |
| `explore/_common.py` | 路径与股票池的单一来源 |
| `explore/00~05_*.py` | 六个概念脚本 |
| `web/` | 数据检视网站（Flask + ECharts，含「添加股票」入口） |
| `notes/concepts.md` | 踩坑记录与概念详解 |
| `notes/architecture.md` | Qlib 库架构、模块划分与可扩展点 |
| `data/_raw_cache/` | API 原始响应，可复现性的最后保险 |

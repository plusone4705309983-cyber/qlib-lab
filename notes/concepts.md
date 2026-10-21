# Qlib 基础概念笔记

环境：`C:\Work_Data\Source_Code_New\qlib-lab`，Windows 原生 + Python 3.10.11 + `pyqlib==0.9.7`，纯 CPU，8GB 内存够用。

> 本文讲**本项目怎么用 Qlib**。Qlib 库本身的架构（Provider 抽象、表达式引擎、五大模块、
> 可扩展点、本环境模型层可用性）见 **[architecture.md](architecture.md)**。

**目录可任意搬移**：所有路径都从 `Path(__file__).resolve().parent.parent` 派生，
数据源、股票池、provider 路径集中在 `config/symbols.yaml` + `explore/_common.py`，
代码里没有一处硬编码绝对路径。venv 本身不可搬移（`pyvenv.cfg` 和 launcher 写死了路径），搬目录要重建。

---

## 1. 数据在磁盘上长什么样

`dump_bin.py dump_all` 之后一个 provider 目录长这样：

```
data/selfmade/cn_bin/
├── calendars/day.txt              # 交易日历，每行一个 YYYY-MM-DD
├── instruments/all.txt            # 每行: SYMBOL<TAB>起始日<TAB>结束日
└── features/<INSTRUMENT>/<field>.day.bin
    ├── close.day.bin  high.day.bin  low.day.bin
    ├── open.day.bin   volume.day.bin  factor.day.bin
    ├── vwap.day.bin                         # 成交均价 (Alpha360/158 要)
```

- `.bin` 是**裸 float32 数组**，无文件头，长度 = 该股票生效区间内的日历长度。
- qlib 读取方式是 `np.frombuffer(fp.read(4 * count), dtype="<f")`，**按需切片**，不是 `np.memmap`。
  这就是 8GB 内存能跑全市场日线的原因——查询多长就只读多长。
- 文件名 → instrument 的映射走 `fname_to_code(文件名.lower()).upper()`。
  在 0.9.7 里 `fname_to_code` 已是**空操作**，所以 `aapl.csv` → `AAPL`，`sh600519.csv` → `SH600519`。
  **instrument 名一律大写**，查询时写 `SH600519`，写小写查不到。
- `all.txt` 里的生效区间是 `dump_bin` 从 CSV 日期范围自动算的，不用手写。
- CN 和 US **必须是两个独立 provider 目录**，不能混放。

## 2. 复权：最容易踩的坑

qlib 的字段契约（官方文档 `component/data.rst` 对每个字段的定义）：

| 字段 | 官方定义 | 含义 |
|---|---|---|
| `$open` | The adjusted opening price | **后复权价** |
| `$high` | The adjusted highest price | **后复权价** |
| `$low` | The adjusted lowest price | **后复权价** |
| `$close` | The adjusted closing price | **后复权价** |
| `$factor` | factor = adjusted_price / original_price | 后复权价 / 原始价 |
| `$vwap` | The adjusted vwap price | **后复权成交均价**（本项目 CSV 的 `vwap` 列） |
| 任意价格 / `$factor` | — | **真实成交价**，和券商 App 一致 |

关键点：**OHLC 四个字段必须全部是复权价**，不只是 `close`。

**必须用后复权（hfq），不能用前复权（qfq）。** 实测东财和腾讯的 `qfq` 对长历史标的都会
算出**负价格**：茅台 2001～2016 共 3529 行 close ≤ 0（`sh600519` 6013 行里有 3529 行）。
两家是同一套「累减累计分红」的算法，分红史一长就溢出成负数。后复权是乘法累乘，不会变负。

后验校验：`SH600519` 2024-01-02 的 `$close/$factor` = **1685.01**，与真实收盘价一致。

### 口径混用是静默错误

本项目踩过这个坑：只复权了 `close`，`open`/`high`/`low` 取了 raw。
后果是 `($high-$low)/$close` 这类表达式**分子分母不同口径**——不报错、不崩溃，
只是结果偏小 5～6 倍：

```
振幅   ($high-$low)/$close     0.381%   错
正确                             2.143%          偏小 5.6 倍
成交额 $close*$volume        338.6 亿   错
正确 ($close/$factor)*vol      54.2 亿   对
```

注意成交额这条**即使 OHLC 全对齐了也还是错的**——复权价是历史价格重标定后的结果，
与当下的钱无关。凡是成交额 / 市值 / 均价，必须先 `$field/$factor` 还原真实价。

自动检测方法：**`$close` 必须落在 `[$low, $high]` 区间内**。
这个不变式在两种口径混用时必然破坏（实测检出 1999/2000 行），已加进 `explore/00_verify.py`。

**美股 factor 恒为 1.0**——腾讯的美股接口只给不复权（`day`），没有 `qfqday`/`hfqday`，
东财美股同样要试三种复权参数但结果完全一致。做美股策略时要注意这一点。

## 3. 日历不是「真实交易日历」

`calendars/day.txt` = **所有 CSV 日期的并集**，由数据本身反推，不做任何节假日校正。
所以自建数据的日历会包含 `1991-01-05`（周六）这种日子——因为 1991 年那批数据里就是有这些日期。
官方数据集的日历是单独生成的，质量更高。

`dump_bin` 的 `data_merge_calendar` 会把每只股票 reindex 到日历上，日历里没被该股票覆盖的
位置填 **NaN**。所以停牌的票在 bin 里是 NaN 而不是缺行。

## 4. `D.features` 的两层结构

传进去的字符串分两类：

- **原始字段**（对应磁盘上的 `.bin`）：`$open $high $low $close $volume $factor $vwap`
- **表达式**（qlib 的算子解析器现场算，不落盘）：
  - 时序：`Ref(x, N)` `Mean(x, N)` `Sum/Std/Max/Min/Slope/Corr(x, N)` `Quantile(x, N)`
  - 逻辑：`If(c, a, b)` `Greater/Less`
  - `Rank(x, N)` 是**滚动分位**（当前值在过去 N 日中的百分位，0~1），不是截面排名
  - `列[1]` / `列[2]` 取的是**未来** 1/2 期，只能给 label 用，特征里用了就是前视偏差

几个具体错误（都实际踩过）：

- `Rank(expr)` 少传 `N` → `TypeError: Rank.__init__() missing 1 required positional argument: 'N'`
- 收益率方向：`$close / Ref($close,1) - 1` 才是收益率。写成 `Ref/$close - 1` 会恒等于 0
- `Ref($close, 1)` 是「日历往前挪 1 位」，不是「昨天」。有停牌/节假日时和自然日不等价
- 截面算子的排名范围只是**你传进去的那几个 instrument**，不是全市场

## 5. Windows 上必须加 `if __name__ == "__main__"` 守卫

qlib 内部用 joblib 起多进程，Windows 的 `spawn` 会重新导入 `__main__`，
直接跑就报 `RuntimeError: An attempt has been made to start a new process before the
current process has finished its bootstrapping phase`。

只要脚本里用到 `Rank` / `Mean` 这类算子、或 `qlib.init` 后取多股票数据，就必须包一层 `main()`。

## 6. 多市场（CN / US）

`region` 是**全局**配置，`qlib.init` 一次只能指向一个 `provider_uri`。所以：

```python
qlib.init(provider_uri=.../cn_bin, region="cn");  df_cn = D.features(...)
qlib.init(provider_uri=.../us_bin, region="us");  df_us = D.features(...)
allmkt = pd.concat([df_cn, df_us], axis=0)
```

- 两次 init 后 `D.calendar()` 返回的也是当前 provider 的，所以要在 init 当下就把日历取出来存好。
- index 顺序是 `(instrument, datetime)`，用 `get_level_values("instrument")` 取，别用位置 `1`。
- **两边节假日不同**，实测 2024 上半年 cn 有 232 个交易日、us 有 244 个。
  跨市场比较前必须取交集，否则 `pct_change` 会把「A 股休市美股交易」当成 0 收益或 NaN。
- instrument 命名天然不冲突（`SH600519` vs `AAPL`），可以直接 concat。

## 7. 安装踩坑

`pip install pyqlib` 直接装会**卡死十几分钟**——`mlflow<3.13` 这类松散约束导致 pip 依赖回溯。
分批装：

1. 无约束核心包（pandas/numpy/pyarrow/...）——几分钟
2. `mlflow<3.13` 单独装 ——十几分钟，主要是慢不是回溯
3. `pip install --no-deps pyqlib==0.9.7` ——秒装

`pyqlib==0.9.7` 有 `cp310-cp310-win_amd64.whl`，Windows 原生可用，不需要 WSL。
和 `numpy 2.2.6` / `pandas 2.3.3` 兼容，import 无警告。

## 8. 表达式引擎：可用算子一览（qlib 0.9.7 实测）

`qlib.data.ops` 注册的算子就是全集。「因子实验室」开放的是其中去掉复数参数
（`TResample` / `Rolling` / `PairRolling` / `ChangeInstrument`）后的白名单：

| 类别 | 算子 | 用法/说明 |
|---|---|---|
| 时序滚动 | `Ref Mean Std Var Med Min Max Sum Count Quantile Rank IdxMax IdxMin Slope Rsquare Resi EMA WMA Mad Kurt Skew Corr Cov` | `Op(x, N)`；`Rank` 返回 0~1 分位；`Slope/Rsquare/Resi` 是滚动线性回归 |
| 逐元素 | `Abs Log Sign Delta Power` | `Delta(x,N) = x - Ref(x,N)` |
| 条件 | `If(c, a, b)` `Mask(x, cond)` | |
| 比较 | `Gt Ge Lt Le Eq Ne Greater Less` | 返回 0/1 |
| 逻辑 | `And Or Not` | |
| 算术 | `Add Sub Mul Div` | 也可以直接写 `+ - * /` |
| 特征 | `$open $high $low $close $volume $factor $vwap` | 磁盘上的 7 个 `.bin` 字段 |

注意：

- 不能传字符串参数（`TResample` 的频率、`ChangeInstrument` 的 instrument 名），白名单里直接没开。
- `Ref(x, -N)` 会取到**未来** N 期（qlib 允许负数）。构造 label 时有用，当特征用就是前视偏差。
- qlib 内部对表达式是 `eval(parse_field(...))`（`qlib/data/data.py`），因此 web 端必须
  先做白名单校验，绝不能把用户输入原样丢进去 —— 见 `web/factor_eval.py` 的 `validate()`。

### 因子实验室（Web 页）

链路：`GET /api/factor` → `web/factorlab.py`（分析 + 进程内缓存）→ 子进程
`web/factor_eval.py`（白名单校验 + `D.features` 求值）。

- **分组**：因子再套一层 `Rank(因子, 250)`，按滚动分位切 5 组；前 249 天窗口不满直接留空
  —— 没有未来函数。
- **指标**：未来 5/10/20 日收益（真实价口径）、上涨概率、样本数、全样本 Spearman IC。
- **当前读数**：最新因子值的历史分位 → 落在哪一组 → 该组历史表现 → 看多/看空/中性。

实测（`SH600519`，6011 个交易日）：

| 因子 | IC(5日) | IC(10日) | IC(20日) |
|---|---|---|---|
| `Mean($close,5)/Mean($close,20)` | 0.036 | 0.081 | 0.073 |
| `($vwap-$close)/$close` | 0.029 | 0.033 | 0.010 |
| `Corr($close,$volume,20)` | 0.000 | 0.025 | 0.054 |

（IC 是样本内的描述性统计，不等于策略收益；单票 6000 天量级下 |IC| ≥ 0.03 已值得继续研究。）

---

## 抓数链路（`scripts/east.py`）

### 网络上的三个坑

1. **DNS 污染**：`push2his.eastmoney.com` 明文 DNS 解析失败。解法是运行时 DoH
   （`dns.alidns.com`）+ 包装 `socket.getaddrinfo`，无管理员权限也能用。
2. **IPv6 是死的**：关掉 VPN 后系统 DNS 返回 IPv4 + IPv6，但联通的 IPv6 到东财 CDN 不通，
   `curl` 0.18 秒就被重置（curl 56）。必须 `PREFER_IPV4`，强制 `AF_INET`。
   ——这就是「关了 VPN 还是 ConnectionError」的真凶，不是 IP 限流。
3. **东财按 IP 限流**：8 次请求就把一批 IP 的配额打满，`curl 56` / `RemoteDisconnected`。
   靠「大 IP 池 + 每 IP 最小间隔 + 失败退避」缓解，但仍不可靠。

### 因此接了腾讯做备用源

`web.ifzq.gtimg.cn/appstock/app/fqkline/get`，A 股美股都支持，是国内基础设施，彻底绕开东财限流。

- 格式：`param={code},day,,{end},{count},{fq}`，最多 **640 条/页**，靠把 `end` 往前推来分页。
- **美股必须带交易所后缀**：`usAAPL.OQ`（纳斯达克）/ `us.N` / `us.A`。
  只写 `usAAPL` 会只返回 2 行。
- A 股有 `qfqday`（前复权），也有 `hfqday`。**只用 hfq**，原因见第 2 节。
- 美股只有 `day`（不复权），所以 factor = 1.0。
- 美股历史留存到 **2007-03-19**（AAPL/MSFT 同一日期，是服务端上限，不是各自 IPO）。

### 用法

```powershell
# 抓取（有磁盘缓存，重跑不会重复请求）
.venv\Scripts\python scripts\east.py --market cn --source tencent
.venv\Scripts\python scripts\east.py --market us --source tencent

# 入库
.venv\Scripts\python scripts\dump_bin.py dump_all `
  --data_path data\selfmade\cn --qlib_dir data\selfmade\cn_bin `
  --freq day --exclude_fields date,symbol --file_suffix .csv --max_workers 4
```

`data/_raw_cache/` 存了 API 原始 JSON（key = `md5(secid|fqt|beg|end)`），
所以抓取脚本可以放心重复跑。**清缓存前先确认 CSV 已生成。**

### 成交量单位

- A 股东财/腾讯的 volume 是**手**，入库时 ×100 换算成**股**（`--vol-mult 100`）
- 美股本来就是**股**（`--vol-mult 1`）
- 校验：茅台 2024-01-02 成交额 = **54.2 亿元**（`$close/$factor × $volume`，真实口径）

### `$vwap` 列怎么来的

Alpha360 有 **60 列**表达式引用 `$vwap`、Alpha158 有 1 列。CSV 里没有 vwap 时，
这些列会被 `Fillna` 整列填成 0（等于白给 60 个废特征），所以 CSV 补了第 9 列
`vwap`，口径是**后复权成交均价**，与 OHLC 同基——`$vwap/$close` 里 factor 自然约掉。

- 真实值 = `成交额 / 成交量(股) * factor`。CSV 里的 volume 已经是**股**，所以直接
  `amount / volume * factor`。
- 成交额**只有东财日线 f57 给**（腾讯日线只有 6 段，实测无成交额），因此
  `east.py` 价格继续走腾讯、成交额单独找东财要，独立缓存键 `AMT|{secid}|...`。
- 自校验：真实均价必落在当日 `[low, high]` 内（0.5% 容差吃两边源的舍入），
  越界 / 缺成交额 / 零成交一律退回典型价 `(H+L+C)/3`（同为后复权口径，不产生 NaN）。
- 东财 `push2*` 若被限流（表现为所有 IP 立即 `RemoteDisconnected`，而
  `datacenter-web` 正常），本次运行内直接降级，不逐股死等；恢复后重跑
  `east.py`（见上「用法」）+ `dump_bin.py dump_all` 即可就地升级为真实成交额。
- 验收口径：`$vwap` 的缺失格必须与 `$close` **完全同格**；Alpha360 里 vwap 列的
  平均 NaN 数要等于非 vwap 列的平均 NaN 数。实测 = **113.1 vs 113.1 / 列**（cn）、
  **29.5 vs 29.5 / 列**（us），说明 60 列不再是废列。
- 备注：首次补列（2026-10-19）时东财 `push2his` 正被限流，15 只票走的是典型价回落。

---

## 官方数据集：已决定暂缓

Qlib 官方 README 现在写着 *"Due to more restrict data security policy. The official dataset
is disabled temporarily"*，并推荐改用社区源。三条理由：

1. 微软已声明官方数据集停用
2. `SunsetWolf/qlib_dataset` 的 v3 是 **2024-05** 的快照，比自建数据旧两年
3. GitHub 实测只有 12～23 KB/s，690MB 要 **12～14 小时**（关 VPN 直连 16～23KB/s，
   美国住宅 IP 出口 12～16KB/s，而同一出口打 Cloudflare 有 514KB/s —— 说明是到 GitHub
   的路由差，不是"国内 vs 海外"的问题，换香港/新加坡节点才可能改善）

真要训模型时的正确选择是 **`chenditc/investment_data` 的 `qlib_bin.tar.gz`**
（540MB，2026-09-26 发布，官方当前推荐），而且只需要 A 股，美股自建即可。

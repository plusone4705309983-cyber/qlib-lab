# Qlib 架构说明

本文记录 **Qlib 0.9.7 库本身的架构**，探查自本环境实际安装包
（`.venv\Lib\site-packages\qlib`），非文档摘抄。

与 `concepts.md` 的分工：那份讲**本项目怎么用**，这份讲**这个库长什么样、能扩展什么**。

代码规模（按行数）：

| 模块 | 行数 | 职责 |
|---|---|---|
| `contrib` | 22489 | 模型 zoo、策略、报告、调参（**占全库 41%**） |
| `data` | 8713 | 表达式引擎、`.bin` IO、缓存 |
| `rl` | 6206 | 强化学习抽象 |
| `backtest` | 5661 | 撮合、账户、持仓、执行器、归因 |
| `workflow` | 5283 | 任务编排、walk-forward、MLflow 记录 |
| `utils` | 3452 | 并行、序列化、通用工具 |
| `model` | 1870 | 训练框架基类（基类本身很小） |
| `strategy` / `cli` / `tests` | 1063 | 策略基类、命令行、测试 |

**`contrib` 占 41% 是个信息量很大的信号**：微软把「框架内核」和「现成方案」明确分开了。
内核小而稳定，外围厚且可弃用。所以读 Qlib 源码的正确顺序是 `data/` → `model/` →
`backtest/`，而不是从 `contrib/` 入门。

---

## 1. 分层结构

```
                      workflow/          任务编排: 记录、walk-forward、回传
                  ┌───────────────┐
   contrib/       │  SignalRecord  │
   22489 行       │  PortAnaRecord │   模型 zoo + 策略 + 报告 + 调参
   (最大模块)     └───────┬───────┘
                          │
   backtest/  5661 行  ┌───┴────┐        撮合/账户/持仓/执行器/归因
                      │Exchange │
                      │Account  │
                      └────┬─────┘
   model/     1870 行  ┌────┴──────┐      训练框架抽象 (基类很小)
                      │  Model    │
                      └────┬──────┘
   data/      8713 行  ┌────┴──────┐      ★ 核心: Provider + 表达式引擎
                      │ Expression│
                      │  .bin IO  │
                      └───────────┘
```

`data/` 是地基，上面各层全部可替换。往下依赖严格，反向不依赖。

## 2. 三个核心抽象

### 2.1 Provider —— 数据访问的唯一入口

`qlib.data.base` 里只有三个基类，且抽象面极窄：

```python
Expression.__abstractmethods__    = ['_load_internal', 'get_extended_window_size',
                                      'get_longest_back_rolling']
DatasetProvider.__abstractmethods__ = ['dataset']
```

**只有 4 个抽象方法**，这是 Qlib 最重要的设计决策——数据源完全可插拔。
本项目的 `cn_bin` / `us_bin` 只是 `LocalDatasetProvider` 的一个实例；
换成数据库、Parquet、S3、远程服务，都是实现同一组接口。

缓存层同样是可插拔的（`qlib.data.cache`）：

```
BaseProviderCache
├── ExpressionCache
│   └── DiskExpressionCache
└── DatasetCache
    ├── DiskDatasetCache          # 落盘, 跨进程复用
    ├── SimpleDatasetCache
    └── CalendarCache
        └── MemoryCalendarCache
```

`explore/_common.py` 里 `qlib.init(..., expression_cache=None, dataset_cache=None)`
就是关掉这两层缓存，保证每次读原始 `.bin`、结果可复现。

### 2.2 表达式引擎 —— 用字符串写因子

```python
D.features(["SH600519"], ["$close/Ref($close,1)-1", "Mean($close,5)/Mean($close,20)-1"])
```

字符串由 Qlib 自己的算子解析器计算，不是查表。算子分两大类：

- **时序算子** —— 沿时间轴：`Ref` `Mean` `Std` `Sum` `Max` `Min` `Corr` `Slope` `Rsquare` `IdxMax` `WMA` `EMA`
- **截面算子** —— 沿 instrument 轴：`Rank` `Quantile` `Resi`

`data/ops.py` 里的类继承 `ExpressionOps` / `ElemOperator` / `NpElemOperator` / `PairOperator` 等。

> **坑：`Operators` 用 `__getattr__` 动态派发，没有显式注册表。**
> 算子名拼错、或参数个数不对时**不会在解析阶段报错**，而是当普通变量名一路传下去，
> 直到运行时才抛 `TypeError`。写表达式时参数个数要自己核对
> （如 `Quantile($close, 4, 0.5)` 要三个参数，`Rank(x, N)` 要两个）。

### 2.3 Model —— 训练框架抽象

`qlib/model/` 只有 1870 行，因为基类极简：定义 `fit` / `predict` 契约。
`contrib/model/` 里 35 个模型文件全都遵循这个模式 —— 8 个传统模型
（`linear` / `gbdt` / `catboost_model` / `xgboost` / `tcn` / `double_ensemble` /
`highfreq_gdbt_model`）+ 27 个 PyTorch 深度模型
（LSTM / ALSTM / GRU / Transformer / TABNet / SAN / TCN / GATs / KRNN / Nn 等）。

---

## 3. 五大功能模块

### data —— 表达式引擎与存储

除上面已述，还有：

- `data/pit.py` —— Point-In-Time，**防未来函数**的关键（财务数据按公告日而非报告期对齐）
- `data/filter.py` —— `ExpressionFilter`，用表达式定义股票池
- `data/inst_processor.py` —— 股票池级预处理

### backtest —— 回测

| 文件 | 关键类 | 职责 |
|---|---|---|
| `exchange.py` | `Exchange` | 撮合：限价/市价/阈值、成交量限制、滑点、冲击成本 |
| `account.py` | `Account` | 资金与持仓 |
| `position.py` | `Position` `BasePosition` | 持仓明细 |
| `executor.py` | `BaseExecutor` `BaseStrategy` | 执行流程；策略基类在这里 |
| `decision.py` | `BaseTradeDecision` | 交易决策对象 |
| `profit_attribution.py` | 收益归因 | 拆解超额收益来源 |
| `report.py` | `Indicator` `BaseOrderIndicator` | 绩效指标 |
| `high_performance_ds.py` | 高频数据源 | 更高频的回测 |

> **注意：`BaseStrategy` 在 `executor.py`，不在 `decision.py`。**
> `decision.py` 只有 `BaseTradeDecision` 等决策数据类。按名字猜模块位置会找错。

### workflow —— 任务编排

```
SignalRecord    训练信号
SigAnaRecord    信号分析（IC / ICIR 等）
PortAnaRecord   组合分析（收益、回撤）
HFSignalRecord  高频信号
RollingGen      walk-forward 滚动训练/回测切分
```

配合 MLflow 做实验记录，`qlib.workflow` 提供 `R.start()` 上下文管理器。

### rl —— 强化学习（当前不可用，见下节）

`Simulator` / `Policy` / `Reward` / `DataProcessor` 抽象，用于深度强化学习交易。

---

## 4. 本环境的实际可用性

**源码里有文件 ≠ 能用**，取决于可选依赖。实测结果：

| 组件 | 状态 | 说明 |
|---|---|---|
| `data` / `backtest` / `workflow` | ✅ 可用 | 纯 numpy/pandas |
| `model/linear.py` | ✅ 可用 | 无额外依赖 |
| `model/gbdt.py` | ✅ 可用 | LightGBM **4.7.0 已装** |
| `model/xgboost.py` | ❌ | XGBoost 未安装 |
| `model/catboost_model.py` | ❌ | CatBoost 未安装 |
| 27 个 `pytorch_*` 模型 | ❌ | PyTorch 未安装 |
| `qlib/rl` 整个模块 | ❌ | 见下 |

### 静默降级：缺失的模型被设为 `None`

导入 `qlib.contrib.model` 时 Qlib 打印：

```
ModuleNotFoundError. CatBoostModel are skipped.
ModuleNotFoundError. XGBModel is skipped(optional: ...).
ModuleNotFoundError.  PyTorch models are skipped (optional: ...).
```

但**导入过程不会中断**，而且这里有个容易踩的细节：

```python
>>> import qlib.contrib.model as m
>>> hasattr(m, "XGBModel")
True                          # 名字存在!
>>> m.XGBModel
<type 'NoneType'>            # 实际是 None
>>> m.XGBModel()
TypeError: 'NoneType' object is not callable
```

Qlib 用 `try/except` 捕获依赖缺失，然后把该名字**绑定为 `None`**。
后果是：

- `hasattr(m, "XGBModel")` 返回 `True`，**用 hasattr 探测依赖会得到错误结论**
- 报错信息是 `'NoneType' object is not callable`，**完全不提示"缺 xgboost"**，很误导
- 正确做法是 `m.XGBModel is not None` 判断

同理，27 个 `pytorch_*` 模型对应的类也全是 `None`。

### RL 模块有硬伤

导入时警告：

> `Gym has been unmaintained since 2022 and does not support NumPy 2.0`
> `Please upgrade to Gymnasium, the maintained drop-in replacement`

Gym 已停止维护，且不兼容本环境的 **NumPy 2.2.6**。`qlib/rl` 依赖 Gym，
**现在跑不起来**。要用需先卸载 gym 改装 `gymnasium`，再处理其余 API 差异。

---

## 5. 可以扩展什么

按改动成本从低到高：

### 5.1 换数据源（最容易，收益最大）

实现 `DatasetProvider.dataset()`，你的数据就进 Qlib 全套表达式引擎。两条路子：

- **转 `.bin`**（本项目采用）：写 CSV → `dump_bin.py dump_all`。简单可靠，QLib 原生路径。
- **直接实现 Provider** 读 Parquet / ClickHouse / S3：跳过转录环节，但要自己实现缓存与日历对齐。

### 5.2 自定义算子

在 `data/ops.py` 加类，或用 `register_all_ops` 注册。
适合把业务指标（如「剔除停牌后的换手率」）做进表达式语言，
而不是在 pandas 里算完再塞回去。

### 5.3 自定义模型

继承 `Model` 实现 `fit` / `predict`。照抄 `linear.py` 最省事。

### 5.4 自定义策略 / 撮合

`BaseStrategy` 决定信号如何变成订单，`Exchange` 决定如何撮合。
加 T+1 限制、涨跌停不成交、开收盘集合竞价等规则**必须动这一层**——
这也是回测可信度的关键，改撮合逻辑比改策略逻辑重要得多。

### 5.5 RL 方向

`qlib/rl` 提供了完整的 `Simulator` / `Policy` / `Reward` 抽象，
深度强化学习交易不必从零搭环境。**但要先解决 Gym 依赖问题**（见上）。

---

## 6. 当前规模的限制

本项目目前 4 只标的（CN 4 + US 4）：

- **截面研究做不了** —— 截面算子 `Rank(x, N)` 的 N 就是参与排名的股票数，4 只没有统计意义
- **训不出有意义的模型** —— 几千行样本对 GBDT/深度模型都是噪声
- 美股数据另有拆股未回补问题（详见 `concepts.md` 与 `00_verify.py`）

要往下走，**数据层必须先扩到几百只标的**。
官方 `qlib_dataset` 已弃用（见 `concepts.md` 末节），当前选择是
`chenditc/investment_data` 的 `qlib_bin.tar.gz`（540MB，仅 A 股，美股可继续自建）。

---

## 相关文件

| 路径 | 说明 |
|---|---|
| `notes/concepts.md` | 本项目的用法与踩坑记录 |
| `explore/_common.py` | 路径与股票池的单一来源 |
| `explore/04_expressions.py` | 表达式引擎的可运行示例 |
| `scripts/dump_bin.py` | Qlib 官方 v0.9.7 脚本，转 CSV → `.bin` |
| `.venv/Lib/site-packages/qlib/` | 源码本体，可直接读 |

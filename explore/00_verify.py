"""验证自建数据的完整性：目录结构 / 日历 / 股票池 / 特征查询 / 复权因子。"""

import pandas as pd
import qlib
from qlib.data import D

from _common import CN_BIN, SYM_CN, SYM_US, US_BIN


def check(label, path, region, symbols, probe):
    print("=" * 70)
    print(f"  {label}  provider={path}")
    print("=" * 70)
    qlib.init(provider_uri=path, region=region,
              expression_cache=None, dataset_cache=None)

    cal = pd.DatetimeIndex(D.calendar())
    print(f"日历     : {len(cal)} 个交易日  {cal[0].date()} .. {cal[-1].date()}")
    inst = D.list_instruments(D.instruments("all"), as_list=True)
    print(f"股票池   : {len(inst)} 只 -> {sorted(inst)}")

    df = D.features(symbols, ["$close", "$factor", "$open", "$volume"],
                    start_time="2024-01-01", end_time="2024-01-10")
    print(f"特征形状 : {df.shape}  (index=MultiIndex[instrument, datetime])")
    print(f"NaN 数量 : {int(df.isna().sum().sum())}")
    print()
    print(df.head(6).to_string())
    print()
    ok = probe(symbols)
    print()
    return ok


def check_adjust_consistency(symbols):
    """OHLC 必须与 $close 同为复权口径 (Qlib 字段契约)。

    历史 bug: 早期版本只把 close 复权, O/H/L 仍取 raw,
    导致 ($high-$low)/$close 这类表达式分子分母不同口径, 结果偏小 6 倍且不报错。
    这里用「$close 必须落在 [$low, $high] 区间内」来检测 ——
    若 O/H/L 是 raw 而 close 是复权价, 这个不变式必然被破坏。
    """
    print("-" * 70)
    print("复权口径一致性 (OHLC vs $close):")
    df = D.features(symbols, ["$high", "$low", "$close", "$factor"],
                    start_time="2023-01-01", end_time="2024-12-31")
    df = df.dropna()
    bad = df[(df["$close"] > df["$high"] + 1e-6) | (df["$close"] < df["$low"] - 1e-6)]
    ok = len(bad) == 0
    print(f"  样本 {len(df)} 行, $close 落在 [$low,$high] 之外: {len(bad)} 行")
    print(f"  OHLC 与 $close 同口径: {'是' if ok else '否  <-- 口径不一致!'}")
    if not ok:
        print(bad.head(5).to_string())
    # 复权还原到真实价的价格区间应与茅台实际股价量级相符
    p = df["$close"] / df["$factor"]
    print(f"  $close/$factor 区间: {p.min():.2f} .. {p.max():.2f} (真实成交价)")
    print()
    return ok


def probe_cn(symbols):
    ok = check_adjust_consistency(symbols)
    s = symbols[0]
    raw = D.features([s], ["$close", "$factor"], start_time="2024-01-01", end_time="2024-01-05")
    raw["$raw_price"] = raw["$close"] / raw["$factor"]
    print(f"{s} 复权价 / 原始价 对照:")
    print(raw.head(3).to_string())
    print("  -> $close 是后复权价, $close/$factor 是真实成交价")
    print()
    print("涉及绝对价格的指标须用真实价, 否则虚高 factor 倍:")
    a = D.features([s], ["$high", "$low", "$close", "$factor", "$volume"],
                   start_time="2024-01-02", end_time="2024-01-02").iloc[0]
    print(f"  振幅 ($high-$low)/$close = {(a['$high']-a['$low'])/a['$close']*100:.3f}%")
    print(f"  成交额 用复权价 = {a['$close']*a['$volume']/1e8:.1f} 亿  (错)")
    print(f"  成交额 用真实价 = {a['$close']/a['$factor']*a['$volume']/1e8:.1f} 亿  (对)")
    return ok


def probe_us(symbols):
    ok = check_adjust_consistency(symbols)
    s = symbols[0]
    f = D.features([s], ["$close", "$factor"], start_time="2024-01-01", end_time="2024-01-05")
    print(f"{s} factor 全为 1.0 (腾讯美股源不复权): {bool((f['$factor'] == 1.0).all())}")
    return ok


if __name__ == "__main__":
    import sys
    results = [check("A股", CN_BIN, "cn", SYM_CN, probe_cn),
               check("美股", US_BIN, "us", SYM_US, probe_us)]
    print("=" * 70)
    if all(results):
        print("  全部通过")
    else:
        print("  校验失败: 复权口径不一致, 相关指标不可用")
        sys.exit(1)
    print("=" * 70)

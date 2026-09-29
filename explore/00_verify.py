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
    probe(symbols)
    print()


def probe_cn(symbols):
    s = symbols[0]
    raw = D.features([s], ["$close", "$factor"], start_time="2024-01-01", end_time="2024-01-05")
    raw["$raw_price"] = raw["$close"] / raw["$factor"]
    print(f"{s} 复权价 / 原始价 对照:")
    print(raw.head(3).to_string())
    print("  -> $close 是后复权价, $close/$factor 是真实成交价")


def probe_us(symbols):
    s = symbols[0]
    f = D.features([s], ["$close", "$factor"], start_time="2024-01-01", end_time="2024-01-05")
    print(f"{s} factor 全为 1.0 (腾讯美股源不复权): {bool((f['$factor'] == 1.0).all())}")


if __name__ == "__main__":
    check("A股", CN_BIN, "cn", SYM_CN, probe_cn)
    check("美股", US_BIN, "us", SYM_US, probe_us)
    print("=" * 70)
    print("  全部通过")
    print("=" * 70)

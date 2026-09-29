"""概念 5: 多市场 (CN / US) 的数据结构

qlib 的 region 是「全局」配置，init 一次只能指向一个 provider_uri。
CN 和 US 数据不能混在一个 provider 里，必须分两次 init，两份 DataFrame 手工对齐拼接。

注意: qlib 内部用 joblib 多进程，Windows 上必须加 main 守卫。
"""

import pandas as pd
import qlib
from qlib.data import D

from _common import CN_BIN, SYM_CN, SYM_US, US_BIN


def main():
    frames = {}
    calendars = {}
    for label, path, region, syms in (
        ("cn", CN_BIN, "cn", SYM_CN),
        ("us", US_BIN, "us", SYM_US),
    ):
        qlib.init(provider_uri=path, region=region)
        df = D.features(syms, ["$close", "$factor", "$volume"],
                        start_time="2024-01-01", end_time="2024-03-31")
        df["$raw"] = df["$close"] / df["$factor"]
        frames[label] = df
        calendars[label] = D.calendar()
        span = df.index.get_level_values("datetime")
        print(f"{label}: shape={df.shape}  {pd.Timestamp(span[0]).date()} .. {pd.Timestamp(span[-1]).date()}"
              f"  日历全长={len(calendars[label])}")

    print("\n两个市场的 index 语义相同: MultiIndex[datetime, instrument]")
    print("  差别在 instrument 命名: CN 用 SH/SZ 前缀，US 直接是全大写 ticker")
    print("  所以拼起来不会冲突，可以直接 concat:")

    allmkt = pd.concat([frames["cn"], frames["us"]], axis=0).sort_index()
    print(f"  合并后 shape={allmkt.shape}")
    print(f"  index levels={allmkt.index.names}")
    print(f"  instrument={sorted(set(allmkt.index.get_level_values('instrument')))}")

    print("\n两个市场日历覆盖范围不同 (cn 1991 起 / us 2007 起):")
    day_sets = {}
    for label, cal in calendars.items():
        days = {pd.Timestamp(d).date() for d in cal}
        day_sets[label] = days
        print(f"  {label}: {min(days)} .. {max(days)}  共 {len(days)} 天")

    cn_days = day_sets["cn"]
    us_days = day_sets["us"]
    only_cn = sorted(cn_days - us_days)
    only_us = sorted(us_days - cn_days)
    print(f"\n  只在 cn 日历里的日子: {len(only_cn)} 天 (示例 {only_cn[:3]})")
    print(f"  只在 us 日历里的日子: {len(only_us)} 天 (示例 {only_us[:3]})")
    print("  中国和美国节假日不同 -> 跨市场对比前必须先取交集，否则收益率会算错")

    print("\n正确做法: 只在共同交易日上算收益率")
    common = cn_days & us_days
    sub = frames["us"]
    stamps = {pd.Timestamp(d) for d in common}
    sub = sub[sub.index.get_level_values("datetime").isin(stamps)].sort_index().copy()
    sub["$ret"] = sub.groupby("instrument")["$raw"].pct_change()
    print(sub.tail(8).to_string())

    print("\n" + "=" * 62)
    print("  结论: 跨市场分析要自己做两件事 —— 1) 两次 init 取数据 2) pandas 对齐日历")
    print("  Qlib 没有内置的多市场抽象，Alpha158 之类处理器也都是单市场的。")
    print("=" * 62)


if __name__ == "__main__":
    main()

"""概念 6: 数据检视 —— 列股票池 / 看元信息 / 查实际数据 / 检查字段.

回答两个常见问题:
  1. 我加的股票进去了吗? 有效期对吗?
  2. 这只股票到底有哪些数据?

注: Qlib 不是 SQL 数据库, 没有表结构可看。
    数据本体是 data/selfmade/cn/*.csv, 经 dump_bin 转成 .bin,
    通过 D.list_instruments / D.features 查询。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import CN_BIN, SYM_CN, SYM_US, US_BIN, launch  # noqa: E402

FIELDS = ["$open", "$high", "$low", "$close", "$volume", "$factor"]


def inspect_market(label, region, provider, symbols):
    print("=" * 78)
    print(f"  {label}   provider={provider}")
    print("=" * 78)
    launch(region)
    import pandas as pd
    from qlib.data import D

    cal = pd.DatetimeIndex(D.calendar())
    inst = D.list_instruments(D.instruments("all"), as_list=True)

    # ---- 1) 股票池 + 有效期 (最常用: 确认新股票是否进来了) ----
    cal_end = str(cal[-1].date())
    print(f"\n[1] 股票池 {len(inst)} 只   日历 {len(cal)} 天  "
          f"{cal[0].date()} .. {cal[-1].date()}")
    print("                    生效区间                     状态")
    pool = {s.upper() for s in symbols}
    for name in inst:
        start, end = _range(name, provider)
        status = "OK" if name in pool else "!! provider 有, 但配置里没有"
        # dump_fix 用 df.reindex(现有日历) 对齐, 超出日历的日期被静默丢弃,
        # 而 all.txt 的区间直接取自 CSV -> 两者会不一致, 这里查出来
        if end != "?" and end > cal_end:
            status = f"!! all.txt end={end} 超过日历末 {cal_end} (超出部分无数据)"
        print(f"    {name:11} {start} .. {end}   {status}")
    missing = pool - set(inst)
    if missing:
        for s in sorted(missing):
            print(f"    {s:11} {'':26} !! 配置里有, 但 provider 没有")

    # ---- 2) 字段与每个字段的数值范围 ----
    print(f"\n[2] 可用字段 {len(FIELDS)} 个 (2024 全年)")
    df = D.features(symbols, FIELDS, start_time="2024-01-01", end_time="2024-12-31")
    print(f"    shape={df.shape}  NaN={int(df.isna().sum().sum())}")
    print("    " + "".join(f"{f:>14}" for f in FIELDS))
    print("    " + "".join(f"{df[f].max():>14.2f}" for f in FIELDS))
    print("    " + "".join(f"{df[f].min():>14.2f}" for f in FIELDS))
    print("     ^max")
    print("     |min")

    # ---- 3) 复权还原 (真实成交价区间, 最有参考价值) ----
    if (df["$factor"] == 1.0).all():
        print("    factor 全为 1.0 -> 未复权, $close 即真实价")
    else:
        for f in ["$open", "$close"]:
            p = df[f] / df["$factor"]
            print(f"    {f}/$factor 区间 {p.min():.2f} .. {p.max():.2f}  (真实成交价)")

    # ---- 4) 单只股票最近数据 ----
    s = symbols[0]
    print(f"\n[3] {s} 最近 5 个交易日")
    last = str(cal[-1].date())[:10]
    recent = cal[cal >= pd.Timestamp(last) - pd.Timedelta(days=21)][-5:]
    rows = D.features([s], FIELDS, start_time=str(recent[0].date()),
                      end_time=str(recent[-1].date()))
    print(rows.to_string(float_format=lambda v: f"{v:,.2f}"))

    # ---- 5) 真实价视图 (最常见的用途) ----
    print(f"\n[4] {s} 同期换成真实成交价")
    raw = rows.copy()
    for f in ["$open", "$high", "$low", "$close"]:
        raw[f] = raw[f] / raw["$factor"]
    raw["turnover_yi"] = raw["$close"] * raw["$volume"] / 1e8
    print(raw[["$open", "$high", "$low", "$close", "turnover_yi"]]
          .to_string(float_format=lambda v: f"{v:,.2f}"))
    print("     ^ real price            ^ 成交额(亿)")

    # ---- 6) 数据质量: 后复权因子异常下调 ----
    print(f"\n[5] 数据质量检查 (全股票池, 近 3 个月)")
    bad = _hfq_gap_check(symbols)
    if bad:
        print("    !! 后复权因子 factor 出现下调 —— 除息只会导致上调, 下调无对应事件")
        for sym, day, a, b, df_, gh, gr in bad:
            print(f"       {sym} {day}  factor {df_:+.2f}%"
                  f"   hfq {a:,.2f} -> {b:,.2f} ({gh:+.2f}%)"
                  f"   但真实价 {gr:+.2f}%")
        print("       该日 $close 不可用于收益计算; 可用 $close/$factor 还原真实价")
    else:
        print("    OK  因子无下调, 后复权关系成立")
    print()


def _hfq_gap_check(symbols, days=64, thresh=1.0):
    """检测后复权因子 factor 的异常**下调**。

    判据推导 (前两版都错, 留档避免重蹈):

        v1  "|Δhfq|>4% 就告警"       -> 3 条里 2 条是正常行情, 误报
        v2  "hfq 与 raw 方向背离"     -> 全历史 2 条里 1 条是除息日, 误报

    事实是三件事:

    1) hfq = raw * factor 是定义, 故
       Δhfq = Δraw + Δfactor 恒成立 —— 单看任何一侧的跳空都查不出问题。

    2) 除息日 hfq 与 raw **必然方向背离**, 这是正常的:
       raw 因分红下跌, factor 相应上调, hfq 保持连续。
       实测 sh600519 25 次 factor 单日上调 >1%, 其中 60% hfq/raw 反向,
       如 2004-07-01 factor +31.03% / hfq +3.47% / raw -21.03% —— 正常除息。
       所以 v2 的"反向=异常"不成立。

    3) 后复权因子**只在除息日上调, 永远不该下调**。
       (上调时 raw 同步下跌, 两者抵消; 下调没有对应的经济事件。)
       实测 6011 天中 factor 单日下调 >1% 仅 5 天, 其中
       2026-09-28 -9.76%: hfq -9.26% 而 raw +0.56%,
       且 raw 被成交量佐证 (28218手 / 34.89亿 -> 均价 1236.3, 落在 1228~1244),
       故错的是 hfq 那一行。

    阈值 1%: factor 日噪声中位数 0.047%、99 分位 0.60%, 取 1% 可避开噪声。
    美股 factor 恒为 1.0, 无下调可查, 直接跳过。
    """
    import pandas as pd
    from qlib.data import D

    probe = D.features(symbols, ["$factor"], start_time="2024-01-01",
                       end_time="2024-12-31")
    if (probe["$factor"] == 1.0).all():
        print("    (factor 恒为 1.0 -> 未复权, 无复权因子可查, 跳过)")
        return []

    cal = pd.DatetimeIndex(D.calendar())
    end = str(cal[-1].date())
    start = str((cal[-1] - pd.Timedelta(days=days)).date())
    df = D.features(symbols, ["$close", "$factor"], start_time=start, end_time=end)
    out = []
    for sym in symbols:
        if sym not in df.index.get_level_values(0):
            continue
        s = df.xs(sym, level=0)
        if len(s) < 2:
            continue
        d_fac = s["$factor"].pct_change()
        raw = s["$close"] / s["$factor"]
        g_h = s["$close"].pct_change()
        g_r = raw.pct_change()
        for pos, v in enumerate(d_fac):
            if pd.isna(v) or v >= -thresh / 100:   # 只看下调; 上调 = 除息日, 正常
                continue
            out.append((sym, str(s.index[pos].date()),
                        s["$close"].iloc[pos - 1], s["$close"].iloc[pos],
                        v * 100, g_h.iloc[pos] * 100, g_r.iloc[pos] * 100))
    return out


def _range(name, provider):
    """从该 provider 的 instruments/all.txt 读某只股票的生效区间。"""
    p = Path(provider) / "instruments" / "all.txt"
    if not p.exists():
        return "?", "?"
    for line in p.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if parts and parts[0].upper() == name.upper():
            return parts[1], parts[2]
    return "?", "?"


def main():
    inspect_market("A股", "cn", CN_BIN, SYM_CN)
    inspect_market("美股", "us", US_BIN, SYM_US)
    print("=" * 78)
    print("  提示: 新加股票若在这里没出现, 说明没跑 dump_bin 重建 provider")
    print("=" * 78)


if __name__ == "__main__":
    main()

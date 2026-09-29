"""概念 1: 交易日历 (Calendar)

日历是 Qlib 里时间维度的唯一基准。所有 .bin 特征文件的长度都等于日历长度，
某只股票当天没交易 -> 该位置是 NaN。日历由 dump_bin 自动生成 = 所有 CSV 日期的并集。
"""

import qlib
from qlib.data import D

from _common import CN_BIN

qlib.init(provider_uri=CN_BIN, region="cn")

cal = D.calendar()
print(f"总交易日数: {len(cal)}")
print(f"区间      : {cal[0].date()} .. {cal[-1].date()}")
print(f"类型      : {type(cal[0])}  元素={cal[0]}")

print("\n最前 8 个交易日 (注意 1991-01-05 是周六 —— 日历来自数据本身，不做节假日校正):")
for d in cal[:8]:
    print(f"  {d.date()}  {d.strftime('%A')}")

print("\n按年统计交易日数量:")
import collections

by_year = collections.Counter(d.year for d in cal)
recent = sorted(by_year.items())[-8:]
for y, n in recent:
    print(f"  {y}: {n} 天")

print("\nD.calendar 区间切片 (取 2024 全年):")
cal_2024 = D.calendar(start_time="2024-01-01", end_time="2024-12-31")
print(f"  {len(cal_2024)} 天  {cal_2024[0].date()} .. {cal_2024[-1].date()}")

print("\n对账: calendars/day.txt 与 D.calendar() 是同一份数据")
print("  day.txt 存的是 YYYY-MM-DD 文本，读取时转成 Timestamp")

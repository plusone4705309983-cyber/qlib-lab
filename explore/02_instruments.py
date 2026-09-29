"""概念 2: 股票池 (Instruments)

instruments/all.txt 每行: INSTRUMENT<TAB>start_datetime<TAB>end_datetime
存的是「大写 instrument 名 + 有效期」，有效期来自该股票 CSV 的日期范围。
文件名 -> instrument 的映射: fname_to_code(文件名.lower()).upper()，所以 aapl.csv -> AAPL。
"""

import qlib
from pathlib import Path
from qlib.data import D

from _common import CN_BIN

qlib.init(provider_uri=CN_BIN, region="cn")

all_cfg = D.instruments("all")
print(f"股票池配置: {all_cfg}")
print("  filter_pipe 为空 -> 'all' 不做任何过滤，时间区间参数对它无效")

insts = D.list_instruments(all_cfg, as_list=True)
print(f"\n全部股票 ({len(insts)}): {sorted(insts)}")

print("\n各股票的生效区间直接看 instruments/all.txt:")
path = str(Path(CN_BIN) / "instruments" / "all.txt")
with open(path, encoding="utf-8") as fh:
    for line in fh:
        sym, beg, end = line.strip().split("\t")
        print(f"  {sym}: {beg} .. {end}")

print("\n关键点:")
print("  1. instrument 是大写的，查询时必须用 'SH600519' 而不是 'sh600519'")
print("  2. 生效区间由 CSV 的日期范围决定，dump_bin 自动写入")
print("  3. 官方数据集还有 csi300 / csi500 等指数成分池；自建数据没有，只能用 'all'")
print("  4. 查不存在的股票不会报错，而是返回空 DataFrame:")

empty = D.features(["SH999999"], ["$close"], start_time="2024-01-01", end_time="2024-01-10")
print(f"     shape={empty.shape}")

"""概念 3: 基础特征字段 (Features)

.bin 里每个字段一个文件，float32，长度 = 日历长度。
D.features 取的是「字段名 + 表达式」两层：
  - $close/$open/$high/$low/$volume/$factor  = 直接读 .bin
  - 任何以 $ 开头的算术组合 = 表达式引擎现场算，不落盘
"""

import qlib

from _common import CN_BIN, SYM_CN

from qlib.data import D

qlib.init(provider_uri=CN_BIN, region="cn")

print("落盘的字段 (features/<instrument>/*.bin):")
raw = D.features([SYM_CN[0]], ["$open", "$high", "$low", "$close", "$volume", "$factor"],
                 start_time="2024-01-02", end_time="2024-01-05")
print(raw.to_string())
print("\n列名带 $ 前缀 = 原始字段；不带 $ = 算出来的。上面 6 列全是原始字段。")

print("\n原始价 vs 复权价 (qlib 最重要的一条约定):")
print("  $close          = 后复权价，跨越分红送转可比")
print("  $close/$factor  = 真实成交价")
print("  $factor         = 后复权价 / 原始价")
d = D.features([SYM_CN[0]], ["$close", "$factor"], start_time="2024-01-02", end_time="2024-01-05")
d["$raw"] = d["$close"] / d["$factor"]
print(d.to_string())

print("\n用原始价算收益率才和券商 App 一致:")
p = D.features([SYM_CN[0]], ["$close", "$factor"], start_time="2023-12-25", end_time="2024-01-10")
p["$raw"] = p["$close"] / p["$factor"]
p["$ret"] = p["$raw"] / p["$raw"].shift(1) - 1
print(p.tail(6).to_string())

print("\n注意: $volume 已把 A股的「手」换算成「股」(x100)，可直接和总市值口径比较")
print("成交额必须用「真实成交价」算, 不能用复权价 —— 复权价是历史价格重标定后的结果,")
print("与当下的钱无关, 直接乘会算出 6 倍以上的虚高值。")
mv = D.features([SYM_CN[0]], ["$close", "$factor", "$volume"],
                start_time="2024-01-02", end_time="2024-01-02")
r = mv.iloc[0]
adj = r["$close"] * r["$volume"] / 1e8
raw = r["$close"] / r["$factor"] * r["$volume"] / 1e8
print(f"  真实收盘价 = {r['$close'] / r['$factor']:.2f}   复权价 = {r['$close']:.2f}")
print(f"  成交额 $close*$volume       = {adj:.1f} 亿元   <- 错, 用了复权价")
print(f"  成交额 ($close/$factor)*vol = {raw:.1f} 亿元   <- 对")
print()
print("同理, 振幅等所有涉及「绝对价格」的指标都不能混用两种口径:")
amp = D.features([SYM_CN[0]], ["$high", "$low", "$close"],
                 start_time="2024-01-02", end_time="2024-01-02").iloc[0]
print(f"  ($high-$low)/$close = {(amp['$high'] - amp['$low']) / amp['$close'] * 100:.3f}%")


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
mv = D.features([SYM_CN[0]], ["$close", "$volume"], start_time="2024-01-02", end_time="2024-01-02")
print(f"  单日成交额 ≈ {mv['$close'].iloc[0] * mv['$volume'].iloc[0] / 1e8:.1f} 亿元")

"""概念 4: 表达式引擎 (Expression Engine)

D.features 的字符串参数不是简单查表，qlib 会用自己的算子解析器计算。
支持算术/比较/逻辑/三元/时间窗口/截面排名等，命名规则:
  $字段          基本字段
  Ref(列, N)     N 期前的值        -> 上一日收盘 = Ref($close, 1)
  Mean(列, N)    N 期均值
  Sum/Std/Max/Min/Rank/Quantile/Corr/Slope/...
  列[1] / 列[2]  未来 1/2 期（只在有 label 时用，会引入前视偏差）

注意: Rank 等截面算子会起多进程，Windows 上必须用 main 守卫，否则 spawn 会重复导入。
"""

import qlib

from _common import CN_BIN, SYM_CN

from qlib.data import D


def main():
    qlib.init(provider_uri=CN_BIN, region="cn")

    print("1) 时序算子: 均线与收益率")
    df = D.features([SYM_CN[0]],
                    ["$close", "Ref($close, 1)", "Mean($close, 5)", "Mean($close, 20)"],
                    start_time="2024-01-20", end_time="2024-02-10")
    df["$ret1"] = df["$close"] / df["Ref($close, 1)"] - 1
    print(df.tail(6).to_string())

    print("\n2) 完整表达式写法 (不预先算列，直接在字符串里组合)")
    ex = D.features(SYM_CN[:2],
                    ["$close/Ref($close,1)-1", "Mean($close,5)/Mean($close,20)-1",
                     "($high-$low)/Ref($close,1)", "If($close>Ref($close,1),1,0)"],
                    start_time="2024-01-20", end_time="2024-01-31")
    print(ex.tail(6).to_string())

    print("\n3) 截面算子: 同一天横向比较。Rank(feature, N) 沿 instrument 轴排序")
    cs = D.features(SYM_CN,
                    ["$close/Ref($close,1)-1", "Rank($close/Ref($close,1)-1, 4)"],
                    start_time="2024-01-25", end_time="2024-01-31")
    print(cs.tail(8).to_string())
    print("\n  Rank 需要显式给 N(参与排名的股票数)。取值 1..N，1 = 当日涨幅最小。")
    print("  注意: 截面算子的排名范围是「当前传入的 instrument 列表」，不是全市场。")

    print("\n4) 陷阱: Ref($close, 1) 是「1 个日历位置前」，不是「昨天」")
    print("  停牌日和节假日会跳过。连续交易日的条件下等价于 shift(1)。")
    print("  另外 列[1] / 列[2] 取的是「未来」值，特征里绝对不能用，会前视偏差。")


if __name__ == "__main__":
    main()

"""用 qlib 表达式引擎计算常用技术指标, 输出一行 JSON。

由 app.py 以子进程调用, 把 qlib 的全局 init / Windows 多进程隔离在主进程之外
(与 bincheck.py 同一套路)。

    python web/indicators.py cn sh600519 real
    python web/indicators.py us aapl hfq

mode: real = 真实成交价 (price/factor),  hfq = 后复权价 (price)
所有指标均用 qlib 的 D.features 表达式字符串计算 (Mean/EMA/Std/Ref/If/Abs 组合),
输出以 qlib 交易日为索引; 前端按日期对齐到 K 线。
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "explore"))

NODES = [
    "ma5", "ma10", "ma20", "ma60",
    "ema12", "ema26",
    "boll_up", "boll_mid", "boll_low",
    "macd_dif", "macd_dea", "macd_hist",
    "rsi6", "rsi12", "rsi24",
    "atr14",
]


def _tok(mode: str):
    # 价格 token: 真实价 = $f/$factor (与图表"真实价"口径一致); 后复权 = $f
    if mode == "real":
        return ("($close/$factor)", "($high/$factor)", "($low/$factor)")
    return ("$close", "$high", "$low")


def _exprs(mode: str) -> dict:
    c, h, l = _tok(mode)
    pc = f"Ref({c},1)"
    up = f"If({c}>{pc},{c}-{pc},0)"
    dn = f"If({c}<{pc},{pc}-{c},0)"
    # True Range: 逐元素 Max(H-L, |H-RefC|, |L-RefC|) —— Max/Min 是滚动算子, 逐元素极大用嵌套 If 实现
    tr = (
        f"If(({h}-{l})>Abs({h}-{pc}),"
        f"If(({h}-{l})>Abs({l}-{pc}),({h}-{l}),Abs({l}-{pc})),"
        f"If(Abs({h}-{pc})>Abs({l}-{pc}),Abs({h}-{pc}),Abs({l}-{pc})))"
    )
    macd = f"EMA({c},12)-EMA({c},26)"
    return {
        "ma5": f"Mean({c},5)",
        "ma10": f"Mean({c},10)",
        "ma20": f"Mean({c},20)",
        "ma60": f"Mean({c},60)",
        "ema12": f"EMA({c},12)",
        "ema26": f"EMA({c},26)",
        "boll_up": f"Mean({c},20)+2*Std({c},20)",
        "boll_mid": f"Mean({c},20)",
        "boll_low": f"Mean({c},20)-2*Std({c},20)",
        "macd_dif": macd,
        "macd_dea": f"EMA({macd},9)",
        "macd_hist": f"({macd}-EMA({macd},9))*2",
        "rsi6": f"100-100/(1+Mean({up},6)/(Mean({dn},6)+1e-9))",
        "rsi12": f"100-100/(1+Mean({up},12)/(Mean({dn},12)+1e-9))",
        "rsi24": f"100-100/(1+Mean({up},24)/(Mean({dn},24)+1e-9))",
        "atr14": f"EMA({tr},14)",
    }


def _num(x, nd: int):
    if x is None or (isinstance(x, float) and (x != x or x in (float("inf"), float("-inf")))):
        return None
    return round(float(x), nd)


def main() -> None:
    market, symbol = sys.argv[1], sys.argv[2].lower()
    mode = sys.argv[3] if len(sys.argv) > 3 else "real"
    if mode not in ("real", "hfq"):
        mode = "real"

    import _common

    _common.launch(market)
    from qlib.data import D

    sym = symbol.upper()
    exprs = _exprs(mode)
    # 相同表达式串 (如 ma20 与 boll_mid) 会产出重复列, 这里先去重
    order, feats = [], []
    for n in NODES:
        e = exprs[n]
        if e not in feats:
            feats.append(e)
            order.append(n)
    df = D.features([sym], ["$close"] + feats)
    df = df[df["$close"].notna()]  # 仅保留该标的实际交易日 (缺数据日历行剔除)

    dates = [str(t.date()) for t in df.index.get_level_values("datetime")]
    series = {n: [_num(v, 4) for v in df.iloc[:, 1 + feats.index(exprs[n])].tolist()] for n in NODES}

    out = {
        "market": market,
        "symbol": symbol,
        "mode": mode,
        "node": NODES,
        "count": len(dates),
        "dates": dates,
        "series": series,
    }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
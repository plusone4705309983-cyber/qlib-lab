# Tech signals
from __future__ import annotations

from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "explore"))

# 信号规则阈值 (集中配置, 便于调整)
RSI_PERIOD = "rsi6"
RSI_OVERBOUGHT = 80.0
RSI_OVERSOLD = 20.0
EPS = 1e-6

# 回测持有期 (交易日)
HORIZONS = (5, 10, 20)

# 信号类型 -> 中文标签
SIGNAL_LABELS = {
    "ma_gold_cross": "MA金叉",
    "ma_death_cross": "MA死叉",
    "macd_cross_up": "MACD翻红",
    "macd_cross_down": "MACD翻绿",
    "rsi_overbought": "RSI超买",
    "rsi_oversold": "RSI超卖",
}

# 看多(info) / 看空(warn)
BULLISH = {"ma_gold_cross", "macd_cross_up", "rsi_oversold"}


def _num_safe(x):
    if x is None:
        return None
    try:
        v = float(x)
    except Exception:
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def _view():
    try:
        import view
    except Exception:
        sys.path.insert(0, str(HERE))
        import view
    return view


def _backtest(dates, closes, sigs, horizons=HORIZONS):
    """按信号类型 + 持有期统计: n(样本), win_rate(方向正确比例), avg_ret(平均收益)。"""
    idx = {d: i for i, d in enumerate(dates)}
    acc = {}
    for s in sigs:
        i = idx.get(s["date"])
        if i is None:
            continue
        bullish = s["type"] in BULLISH
        for h in horizons:
            j = i + h
            if j >= len(closes):
                continue
            c0, c1 = closes[i], closes[j]
            if not c0 or c0 != c0 or c1 is None or c1 != c1:
                continue
            ret = c1 / c0 - 1.0
            win = (ret > 0.0) if bullish else (ret < 0.0)
            cell = acc.setdefault(s["type"], {}).setdefault(h, {"n": 0, "win": 0, "sum": 0.0})
            cell["n"] += 1
            cell["win"] += 1 if win else 0
            cell["sum"] += ret
    out = {}
    for t, hs in acc.items():
        out[t] = {}
        for h, c in hs.items():
            n = c["n"]
            out[t][h] = {
                "n": n,
                "win_rate": round(c["win"] / n, 4) if n else None,
                "avg_ret": round(c["sum"] / n, 4) if n else None,
                "bullish": t in BULLISH,
            }
    return out


def compute_signals(market, qlib_sym, mode="real"):
    """基于指标计算技术信号 (单股) + 回测统计。

    规则 (表达式等价):
      - MA 金叉/死叉:   MA5 上穿/下穿 MA20
      - MACD 翻红/翻绿: 柱状线 (DIF-DEA)*2 上穿/下穿 0
      - RSI 超买/超卖:  RSI6 进入 >=80 / <=20 区域 (状态切换时触发)
    回测: 信号发生后 N 个交易日的真实价收益, N ∈ HORIZONS。
    统计口径: win = 方向正确 (看多: 涨; 看空: 跌)。
    返回 {market, symbol, mode, dates, signals:[{date,type,level,msg}], count,
          backtest:{type:{h:{n,win_rate,avg_ret,bullish}}}, horizons, labels}
    """
    qlib_sym = qlib_sym.lower()
    if mode not in ("real", "hfq"):
        mode = "real"
    view = _view()
    ind = view.compute_indicators(market, qlib_sym, mode)
    pl = view.payload(market, qlib_sym, include_signals=False)
    dates = pl["dates"]
    closes = pl["real"]["close"]
    idates = {d: i for i, d in enumerate(ind["dates"])}
    ss = ind["series"]

    def g(key, i):
        if i < 0 or i >= len(dates):
            return None
        idi = idates.get(dates[i])
        if idi is None:
            return None
        arr = ss.get(key)
        if not arr or idi >= len(arr):
            return None
        return _num_safe(arr[idi])

    sigs = []
    for i in range(len(dates)):
        if i >= 1:
            ma5p, ma20p = g("ma5", i - 1), g("ma20", i - 1)
            ma5, ma20 = g("ma5", i), g("ma20", i)
            if None not in (ma5, ma20, ma5p, ma20p):
                if ma5p <= ma20p + EPS and ma5 > ma20 + EPS:
                    sigs.append({"date": dates[i], "type": "ma_gold_cross", "level": "info", "msg": "MA5 上穿 MA20（金叉）"})
                if ma5p >= ma20p - EPS and ma5 < ma20 - EPS:
                    sigs.append({"date": dates[i], "type": "ma_death_cross", "level": "warn", "msg": "MA5 下穿 MA20（死叉）"})
            mhp, mh = g("macd_hist", i - 1), g("macd_hist", i)
            if None not in (mhp, mh):
                if mhp <= 0.0 and mh > 0.0:
                    sigs.append({"date": dates[i], "type": "macd_cross_up", "level": "info", "msg": "MACD 柱状线翻红（零轴上穿）"})
                if mhp >= 0.0 and mh < 0.0:
                    sigs.append({"date": dates[i], "type": "macd_cross_down", "level": "warn", "msg": "MACD 柱状线翻绿（零轴下穿）"})
        r6, r6p = g(RSI_PERIOD, i), g(RSI_PERIOD, i - 1)
        if r6 is not None:
            if r6 >= RSI_OVERBOUGHT and (r6p is None or r6p < RSI_OVERBOUGHT):
                sigs.append({"date": dates[i], "type": "rsi_overbought", "level": "warn", "msg": f"RSI6 进入超买区 ({r6:.1f})"})
            elif r6 <= RSI_OVERSOLD and (r6p is None or r6p > RSI_OVERSOLD):
                sigs.append({"date": dates[i], "type": "rsi_oversold", "level": "info", "msg": f"RSI6 进入超卖区 ({r6:.1f})"})

    seen = set()
    dedup = []
    for x in sorted(sigs, key=lambda z: (z["date"], z["type"])):
        k = (x["date"], x["type"])
        if k not in seen:
            seen.add(k)
            dedup.append(x)

    backtest = _backtest(dates, closes, dedup)
    return {
        "market": market,
        "symbol": qlib_sym,
        "mode": mode,
        "dates": dates,
        "signals": dedup,
        "count": len(dedup),
        "backtest": backtest,
        "horizons": list(HORIZONS),
        "labels": SIGNAL_LABELS,
    }

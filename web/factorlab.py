"""因子实验室 —— 分析层 (主进程, 纯 Python)。

把用户的自定义 Qlib 表达式变成可读的研究结论:
    1. 因子值与真实价的时间序列 (前端画图)
    2. 分组(5 组, 按过去 250 日**滚动分位**, 无未来函数)的未来收益 / 上涨概率
    3. 全样本 Spearman IC (因子值 vs 未来 N 日收益)
    4. 当前读数: 最新因子值落在哪一组 -> 历史上该组的后续表现
    5. 信号卡: 分位 + 该组条件收益 + IC + 均线趋势 -> 偏多/中性/偏空 + 置信度 + 失效条件

qlib 求值放在常驻 worker 里 (factor_eval.py + factor_runtime.py), 这里只做统计,
方便自检与复用; worker 崩溃/超时不会影响 Flask 主进程。
"""

from __future__ import annotations

import json
import math
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import factor_eval  # noqa: E402  仅用 validate / ExprError (模块级不 import qlib)
import factor_runtime  # noqa: E402  常驻 qlib worker

HORIZONS = (5, 10, 20)  # 与 signals.py 的信号回测口径一致
HIST_WIN = factor_eval.HIST_WIN  # 250 日滚动分位窗口
GROUPS = 5
HEADLINE_H = 10  # 结论用 10 日
MIN_SAMPLES = 30  # 少于该样本数的分组, 结论标注"样本偏少"
_CACHE: dict = {}  # 原始 qlib 序列 (dates/factor/pct/close_real)
_RESULT_CACHE: dict = {}  # compute() 的完整分析结果 (含 signal/current)
_CACHE_MAX = 32
_RESULT_CACHE_MAX = 16
# qlib 子进程内存占用不低, 串行化求值, 避免并发请求同时起多个 qlib 进程
_LOCK = threading.Lock()


def _run(market: str, symbol: str, expr: str, force: bool = False) -> dict:
    key = (market, symbol, expr)
    if not force and key in _CACHE:
        return _CACHE[key]
    with _LOCK:
        if not force and key in _CACHE:  # 等锁期间可能已被别的线程算好
            return _CACHE[key]
        data = factor_runtime.evaluate(market, symbol, expr)
        if len(_CACHE) >= _CACHE_MAX:  # 简单 FIFO, 防止内存无限增长
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = data
        return data


def _avg(vals):
    return sum(vals) / len(vals) if vals else None


def _ranks(a):
    order = sorted(range(len(a)), key=lambda i: a[i])
    r = [0.0] * len(a)
    i = 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and a[order[j + 1]] == a[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            r[order[k]] = avg
        i = j + 1
    return r


def _spearman(xs, ys):
    """秩相关 (并列取平均秩), 样本不足或方差为 0 时返回 None。"""
    n = len(xs)
    if n < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx)
    dy = sum((b - my) ** 2 for b in ry)
    den = math.sqrt(dx * dy)
    return num / den if den else None


def _quantile(vals, q):
    """线性插值分位数 (q=0.1/0.5/0.9)。"""
    if not vals:
        return None
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _cell(idxs, ret_h):
    rets = [ret_h[i] for i in idxs if ret_h[i] is not None]
    if not rets:
        return {"n": 0, "avg_ret": None, "win_rate": None,
                "q10": None, "q50": None, "q90": None}
    win = sum(1 for r in rets if r > 0) / len(rets)
    return {
        "n": len(rets),
        "avg_ret": round(_avg(rets), 6),
        "win_rate": round(win, 4),
        "q10": round(_quantile(rets, 0.10), 6),
        "q50": round(_quantile(rets, 0.50), 6),
        "q90": round(_quantile(rets, 0.90), 6),
    }


def _pct_span(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None, None
    return round(min(vals), 4), round(max(vals), 4)


def _pct_text(x):
    return f"{x * 100:.1f}%"


def _ma(vals, n):
    if len(vals) < n:
        return None
    window = vals[-n:]
    if any(v is None for v in window):
        return None
    return sum(window) / n


def _build_signal(dates, close, pct, factor, buckets, overall, ic):
    """把「当前分位 + 该组历史统计 + IC + 均线」压成一张可解释的信号卡。

    只做规则化提示, 不预测涨跌; 置信度按有效样本/胜率优势/超额/IC/期限一致性打分。
    """
    last = next(
        (i for i in range(len(pct) - 1, -1, -1) if pct[i] is not None and factor[i] is not None),
        None,
    )
    if last is None:
        return None

    h = HEADLINE_H
    g = min(GROUPS - 1, max(0, int(pct[last] * GROUPS)))
    cell = buckets[g]["h"][str(h)]
    base = overall[str(h)]
    if (
        not cell
        or cell.get("avg_ret") is None
        or cell.get("win_rate") is None
        or not base
        or base.get("avg_ret") is None
    ):
        return None

    avg, win, n = cell["avg_ret"], cell["win_rate"], cell["n"]
    base_avg = base["avg_ret"]
    excess = avg - base_avg
    n_eff = max(1, int(round(n / h))) if n else 0
    win_edge = round(abs(win - 0.5), 4)
    ic10 = (ic.get(str(h)) or {}).get("ic")

    direction = 0
    if abs(excess) >= 0.002 or win_edge >= 0.03:
        if avg > 0 and win > 0.5 and excess > 0:
            direction = 1
        elif avg < 0 and win < 0.5 and excess < 0:
            direction = -1

    close_ok = [v for v in close if v is not None]
    ma20 = _ma(close, 20)
    ma60 = _ma(close, 60)
    last_close = close[last] if last < len(close) else None
    low20 = min(close_ok[-20:]) if len(close_ok) >= 20 else None
    high20 = max(close_ok[-20:]) if len(close_ok) >= 20 else None

    if last_close is None or ma20 is None or ma60 is None:
        trend = "unknown"
    elif last_close > ma20 and ma20 > ma60:
        trend = "up"
    elif last_close < ma20 and ma20 < ma60:
        trend = "down"
    else:
        trend = "flat"

    reasons = [
        f"当前分位 {_pct_text(pct[last])}（{buckets[g]['name']}），因子值 {factor[last]:.4g}。",
        f"该组未来 {h} 日：均值 {avg * 100:+.2f}%，胜率 {win * 100:.0f}%，"
        f"相对全样本超额 {excess * 100:+.2f}%；有效样本约 {n_eff}"
        f"（原始 n={n}，按 {h} 日重叠折算）。",
    ]

    same = 0
    if direction == 0:
        level, confidence, score = "neutral", None, 0
        reasons.append("该分位的超额收益/胜率优势不明确，暂不给方向。")
    else:
        score = 0
        if n_eff >= 50:
            score += 2
        elif n_eff >= 20:
            score += 1
        if win_edge >= 0.10:
            score += 2
        elif win_edge >= 0.05:
            score += 1
        if abs(excess) >= 0.03:
            score += 2
        elif abs(excess) >= 0.01:
            score += 1
        if ic10 is not None and ic10 * direction > 0:
            if abs(ic10) >= 0.08:
                score += 2
            elif abs(ic10) >= 0.04:
                score += 1
        for hh in HORIZONS:
            c = buckets[g]["h"][str(hh)]
            b = overall[str(hh)]
            if c.get("avg_ret") is not None and b.get("avg_ret") is not None:
                if (c["avg_ret"] - b["avg_ret"]) * direction > 0:
                    same += 1
        if same >= 2:
            score += 1
        if direction > 0 and trend == "down":
            score -= 1
        elif direction < 0 and trend == "up":
            score -= 1

        if score >= 7:
            level, confidence = ("bullish" if direction > 0 else "bearish"), "strong"
        elif score >= 5:
            level, confidence = ("bullish" if direction > 0 else "bearish"), "medium"
        elif score >= 3:
            level, confidence = ("bullish" if direction > 0 else "bearish"), "weak"
        else:
            level, confidence = "neutral", None
            reasons.append("综合评分不足（样本/胜率/超额/IC 偏弱），降级为中性。")

        if confidence and ic10 is not None:
            if ic10 * direction > 0:
                reasons.append(f"IC({h}日) = {ic10:+.3f}，与分组方向一致。")
            else:
                reasons.append(f"IC({h}日) = {ic10:+.3f}，与分组方向不一致，置信度下调。")
        if confidence:
            if same >= 2:
                reasons.append("5/10/20 日中有 2 个以上期限方向一致。")
            else:
                reasons.append("不同期限方向不一致，稳定性偏弱。")
            if n_eff < 15:
                reasons.append("有效样本偏少，统计不确定性大。")

    if trend == "up":
        reasons.append(
            f"趋势：收盘 {last_close:.4g} 高于 MA20 {ma20:.4g}、MA20 高于 MA60 {ma60:.4g}（上行）。"
        )
    elif trend == "down":
        reasons.append(
            f"趋势：收盘 {last_close:.4g} 低于 MA20 {ma20:.4g}、MA20 低于 MA60 {ma60:.4g}（下行）。"
        )
    elif trend == "flat":
        reasons.append("趋势：均线交织，方向不明。")

    if level == "bullish" and trend == "down":
        reasons.append("注意：价格处于均线空头排列，偏多信号属于逆势。")
    if level == "bearish" and trend == "up":
        reasons.append("注意：价格处于均线多头排列，偏空信号属于逆势。")

    if level == "bullish" and low20 is not None and last_close:
        inv = (
            f"若收盘跌破近 20 日低点 {low20:.4g}（约 {(low20 / last_close - 1) * 100:+.1f}%）"
            f"或分位回到 50% 以上，偏多判断失效。"
        )
    elif level == "bearish" and high20 is not None and last_close:
        inv = (
            f"若收盘突破近 20 日高点 {high20:.4g}（约 {(high20 / last_close - 1) * 100:+.1f}%）"
            f"或分位回到 50% 以下，偏空判断失效。"
        )
    else:
        inv = "等待分位进入 ≤20% 或 ≥80% 的极端区间，且该组出现明确超额收益后再看方向。"

    return {
        "date": dates[last],
        "horizon": h,
        "group": g,
        "name": buckets[g]["name"],
        "pct": pct[last],
        "factor": factor[last],
        "level": level,
        "confidence": confidence,
        "score": score,
        "stats": {
            "avg_ret": avg,
            "win_rate": win,
            "n": n,
            "n_eff": n_eff,
            "excess": round(excess, 6),
            "q10": cell.get("q10"),
            "q50": cell.get("q50"),
            "q90": cell.get("q90"),
        },
        "baseline": {"avg_ret": base_avg, "win_rate": base.get("win_rate")},
        "ic": ic10,
        "trend": {
            "label": trend,
            "close": last_close,
            "ma20": ma20,
            "ma60": ma60,
            "low20": low20,
            "high20": high20,
        },
        "reasons": reasons,
        "invalidate": inv,
        "disclaimer": "历史统计 + 规则判断，不构成投资建议。",
    }


def cached(market: str, symbol: str, expr: str) -> dict | None:
    """只读 compute() 的完整结果缓存, 不触发 qlib。"""
    try:
        expr = factor_eval.validate(expr)
    except Exception:  # noqa: BLE001
        return None
    return _RESULT_CACHE.get((market, symbol, expr))


def compute(market: str, symbol: str, expr: str, force: bool = False) -> dict:
    expr = factor_eval.validate(expr)
    key = (market, symbol, expr)
    if not force:
        hit = _RESULT_CACHE.get(key)
        if hit is not None:
            return hit
    d = _run(market, symbol, expr, force=force)
    dates, factor, close = d["dates"], d["factor"], d["close_real"]
    n = len(dates)
    # Rank(x, 250) 的 min_periods=1, 前 249 天窗口不满 -> 分位不可靠, 剔除
    pct = list(d["pct"])
    for i in range(min(HIST_WIN - 1, n)):
        pct[i] = None

    if not any(v is not None for v in factor):
        raise factor_eval.ExprError("表达式结果全为空 (NaN): 检查字段/窗口是否合理")

    # ---- 前向收益 (真实价口径, 与信号回测一致) ----
    fwd = {}
    for h in HORIZONS:
        arr = [None] * n
        for i in range(n - h):
            a, b = close[i], close[i + h]
            if a and b:
                arr[i] = b / a - 1.0
        fwd[h] = arr

    # ---- 分组: 按滚动分位 (0~1) 等宽切 5 组, 组 0 = 因子最弱 ----
    members = [[] for _ in range(GROUPS)]
    for i in range(n):
        if factor[i] is None or pct[i] is None:
            continue
        g = min(GROUPS - 1, max(0, int(pct[i] * GROUPS)))
        members[g].append(i)

    buckets = []
    for g, idxs in enumerate(members):
        fvals = [factor[i] for i in idxs]
        pvals = [pct[i] for i in idxs]
        lo_p, hi_p = _pct_span(pvals)
        lo_f, hi_f = _pct_span(fvals)
        buckets.append(
            {
                "i": g,
                "name": f"第{g + 1}组",
                "n": len(idxs),
                "pct_lo": lo_p,
                "pct_hi": hi_p,
                "factor_lo": lo_f,
                "factor_hi": hi_f,
                "h": {str(h): _cell(idxs, fwd[h]) for h in HORIZONS},
            }
        )

    # ---- 全样本 IC (因子值 vs 未来 h 日收益) ----
    ic = {}
    for h in HORIZONS:
        xs = [factor[i] for i in range(n) if factor[i] is not None and fwd[h][i] is not None]
        ys = [fwd[h][i] for i in range(n) if factor[i] is not None and fwd[h][i] is not None]
        rho = _spearman(xs, ys)
        ic[str(h)] = {"ic": None if rho is None else round(rho, 4), "n": len(xs)}

    all_idx = [i for i in range(n) if factor[i] is not None]
    overall = {str(h): _cell(all_idx, fwd[h]) for h in HORIZONS}

    note = None
    if not any(members):
        note = f"有效样本不足 {HIST_WIN} 个交易日，无法计算滚动分位分组（因子序列与 IC 仍有效）"

    # ---- 当前读数 ----
    cur = None
    last = next(
        (i for i in range(n - 1, -1, -1) if factor[i] is not None and pct[i] is not None),
        None,
    )
    if last is not None:
        g = min(GROUPS - 1, max(0, int(pct[last] * GROUPS)))
        cells = buckets[g]["h"]
        head = cells[str(HEADLINE_H)]
        avg, win, cnt = head["avg_ret"], head["win_rate"], head["n"]
        base_avg = overall[str(HEADLINE_H)]["avg_ret"]
        excess = None if avg is None or base_avg is None else avg - base_avg
        if avg is None or win is None or excess is None:
            level = "neutral"
        elif avg > 0 and win >= 0.5 and excess > 0:
            level = "info"
        elif avg < 0 and win < 0.5 and excess < 0:
            level = "warn"
        else:
            level = "neutral"

        parts = [f"当前因子 {factor[last]:.4g}, 处于过去 {HIST_WIN} 日分位 {_pct_text(pct[last])}（{buckets[g]['name']}）。"]
        if excess is not None:
            parts.append(f"该组未来 {HEADLINE_H} 日相对全样本超额 {excess * 100:+.2f}%。")
        for h in HORIZONS:
            c = cells[str(h)]
            if c["avg_ret"] is not None:
                parts.append(f"未来{h}日: 均值 {c['avg_ret'] * 100:+.2f}% / 上涨概率 {c['win_rate'] * 100:.0f}%（n={c['n']}）。")
        if level == "info":
            parts.append(f"历史上该分位后续 {HEADLINE_H} 日偏多。")
        elif level == "warn":
            parts.append(f"历史上该分位后续 {HEADLINE_H} 日偏空。")
        else:
            parts.append(f"历史上该分位后续 {HEADLINE_H} 日无明确方向。")
        if cnt is not None and cnt < MIN_SAMPLES:
            parts.append("样本偏少，结论仅作参考。")
        parts.append("以上为历史统计描述，不构成投资建议。")

        cur = {
            "date": dates[last],
            "factor": factor[last],
            "pct": pct[last],
            "group": g,
            "name": buckets[g]["name"],
            "h": cells,
            "level": level,
            "msg": "".join(parts),
        }

    signal = _build_signal(dates, close, pct, factor, buckets, overall, ic)

    result = {
        "market": market,
        "symbol": symbol,
        "expr": expr,
        "count": n,
        "hist_win": HIST_WIN,
        "groups": GROUPS,
        "horizons": list(HORIZONS),
        "dates": dates,
        "factor": factor,
        "pct": pct,
        "close_real": close,
        "ic": ic,
        "buckets": buckets,
        "overall": overall,
        "current": cur,
        "signal": signal,
        "note": note,
    }
    with _LOCK:
        if len(_RESULT_CACHE) >= _RESULT_CACHE_MAX:
            _RESULT_CACHE.pop(next(iter(_RESULT_CACHE)))
        _RESULT_CACHE[key] = result
    return result


if __name__ == "__main__":
    # 自检: python web/factorlab.py cn sh600519 "Mean($close,5)/Mean($close,20)"
    if len(sys.argv) < 4:
        print("用法: factorlab.py <market> <symbol> <expr>")
        sys.exit(2)
    try:
        out = compute(sys.argv[1], sys.argv[2].lower(), sys.argv[3])
        print(json.dumps(out, ensure_ascii=False, indent=2)[:2000])
        print("...")
        print("count =", out["count"], "| ic =", out["ic"], "| current =", out["current"]["group"], out["current"]["name"])
    except factor_eval.ExprError as exc:
        print("表达式错误:", exc)
        sys.exit(2)

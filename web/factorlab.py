"""因子实验室 —— 分析层 (主进程, 纯 Python)。

把用户的自定义 Qlib 表达式变成可读的研究结论:
    1. 因子值与真实价的时间序列 (前端画图)
    2. 分组(5 组, 按过去 250 日**滚动分位**, 无未来函数)的未来收益 / 上涨概率
    3. 全样本 Spearman IC (因子值 vs 未来 N 日收益)
    4. 当前读数: 最新因子值落在哪一组 -> 历史上该组的后续表现

qlib 求值放在子进程 (factor_eval.py), 这里只做统计, 方便自检与复用。
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import factor_eval  # noqa: E402  仅用 validate / ExprError (模块级不 import qlib)

HORIZONS = (5, 10, 20)  # 与 signals.py 的信号回测口径一致
HIST_WIN = factor_eval.HIST_WIN  # 250 日滚动分位窗口
GROUPS = 5
HEADLINE_H = 10  # 结论用 10 日
MIN_SAMPLES = 30  # 少于该样本数的分组, 结论标注"样本偏少"
_CACHE: dict = {}
_CACHE_MAX = 32
# qlib 子进程内存占用不低, 串行化求值, 避免并发请求同时起多个 qlib 进程
_LOCK = threading.Lock()


def _run(market: str, symbol: str, expr: str) -> dict:
    key = (market, symbol, expr)
    if key in _CACHE:
        return _CACHE[key]
    with _LOCK:
        if key in _CACHE:  # 等锁期间可能已被别的线程算好
            return _CACHE[key]
        cmd = [sys.executable, "-u", str(HERE / "factor_eval.py"), market, symbol, expr]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=180)
        except subprocess.TimeoutExpired:
            raise RuntimeError("因子计算超时 (180s), 请缩小表达式窗口")
        lines = [ln for ln in (proc.stdout or "").splitlines() if ln.strip().startswith("{")]
        if not lines:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise RuntimeError(tail[-1] if tail else "因子计算失败")
        data = json.loads(lines[-1])
        if "error" in data:
            raise factor_eval.ExprError(data["error"])
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


def _cell(idxs, ret_h):
    rets = [ret_h[i] for i in idxs if ret_h[i] is not None]
    if not rets:
        return {"n": 0, "avg_ret": None, "win_rate": None}
    win = sum(1 for r in rets if r > 0) / len(rets)
    return {"n": len(rets), "avg_ret": round(_avg(rets), 6), "win_rate": round(win, 4)}


def _pct_span(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None, None
    return round(min(vals), 4), round(max(vals), 4)


def _pct_text(x):
    return f"{x * 100:.1f}%"


def compute(market: str, symbol: str, expr: str) -> dict:
    expr = factor_eval.validate(expr)
    d = _run(market, symbol, expr)
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
        if avg is None or win is None:
            level = "neutral"
        elif avg > 0 and win >= 0.5:
            level = "info"
        elif avg < 0 and win < 0.5:
            level = "warn"
        else:
            level = "neutral"

        parts = [f"当前因子 {factor[last]:.4g}, 处于过去 {HIST_WIN} 日分位 {_pct_text(pct[last])}（{buckets[g]['name']}）。"]
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

    return {
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
        "note": note,
    }


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

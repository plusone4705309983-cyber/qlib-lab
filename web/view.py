"""数据检视器的数据层: 读取 CSV、计算真实价、检测异常。

纯函数, 不依赖 Flask / qlib, 可以直接跑自检:
    .venv\\Scripts\\python.exe web\\view.py

数据来源 = data/selfmade/{cn,us}/*.csv (数据本体, 唯一事实来源)。
每个 CSV 的列: date,symbol,open,high,low,close,volume,factor
其中 open/high/low/close 是**后复权价**, trade = price / factor 为真实成交价。
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "explore"))

import _common  # noqa: E402

# --- 异常判定阈值 ---------------------------------------------------------
# 后复权因子只应单调不减 (分红/送转只会抬高)。阈值与 scripts/east.py 的
# MAX_FACTOR_DROP 一致: 环比跌幅 > 1% 记为错误。
# 更小的下探 (< 1%) 是腾讯 raw / hfq 两个接口各自的舍入误差造成的噪声
# (实测中位数 0.05%), 不标注; 但因子阶梯图仍能直接看出这些微小凹陷。
FACTOR_DROP_ERROR = 0.01

# 真实价单日涨跌的提示阈值 (A股涨跌停 10%, 新股/ST 会突破; 美股无限制)
BIG_MOVE = {"cn": 0.105, "us": 0.25}
# 美股 factor 恒 1 (未复权), 单日跳变超过此值多半是拆股
SPLIT_LIKE = 0.40

MARKET_LABEL = {"cn": "A股", "us": "美股"}
CURRENCY = {"cn": "CNY", "us": "USD"}

_price_round = 4
_factor_round = 6


# --- 配置 -----------------------------------------------------------------
def markets() -> list[str]:
    return list(_common.CFG["markets"].keys())


def reload() -> None:
    """重新读取 config/symbols.yaml 并清空日历缓存 (常驻服务添加股票后调用)。"""
    import yaml

    with (ROOT / "config" / "symbols.yaml").open(encoding="utf-8") as fh:
        _common.CFG = yaml.safe_load(fh)
    _cal_cache.clear()


def symbols() -> list[dict]:
    """按配置顺序返回全部标的: {market, qlib, secid, name}。"""
    out = []
    for market in markets():
        for s in _common.CFG["markets"][market]["symbols"]:
            out.append(
                {
                    "market": market,
                    "qlib": s["qlib"],
                    "secid": s.get("secid", ""),
                    "name": s.get("name", s["qlib"]),
                }
            )
    return out


def find(market: str, qlib: str) -> dict | None:
    q = qlib.lower()
    for s in symbols():
        if s["market"] == market and s["qlib"] == q:
            return s
    return None


def _csv_path(market: str, qlib: str) -> Path:
    csv_dir = ROOT / _common.CFG["markets"][market]["csv_dir"]
    return csv_dir / f"{qlib}{'' if qlib.endswith('.csv') else '.csv'}"


# --- 读取 -----------------------------------------------------------------
def load_rows(market: str, qlib: str) -> list[dict]:
    """读 CSV, 返回按日期升序的行 (数值字段已是 float)。"""
    path = _csv_path(market, qlib)
    rows: list[dict] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            if not r.get("date"):
                continue
            rows.append(
                {
                    "date": r["date"],
                    "open": float(r["open"]),
                    "high": float(r["high"]),
                    "low": float(r["low"]),
                    "close": float(r["close"]),
                    "volume": float(r["volume"]),
                    "factor": float(r["factor"]),
                }
            )
    rows.sort(key=lambda x: x["date"])
    return rows


_cal_cache: dict[str, list[str]] = {}


def market_calendar(market: str) -> list[str]:
    """该市场所有 CSV 日期的并集 (与 dump_bin 建日历的口径一致)。"""
    if market not in _cal_cache:
        dates = set()
        for s in symbols():
            if s["market"] != market:
                continue
            for row in load_rows(market, s["qlib"]):
                dates.add(row["date"])
        _cal_cache[market] = sorted(dates)
    return _cal_cache[market]


# --- 检测 -----------------------------------------------------------------
def _rng_factor(prev: float, cur: float) -> float:
    return cur / prev - 1.0 if prev else 0.0


def detect(rows: list[dict], calendar: list[str], market: str) -> list[dict]:
    """返回异常列表, 每条 {date, type, level, msg}。level ∈ error|warn|info。"""
    out: list[dict] = []
    limit = BIG_MOVE.get(market, 0.25)

    for i, r in enumerate(rows):
        # 1) OHLC 口径混用: close 必须落在 [low, high] 内
        if not (r["low"] - 1e-9 <= r["close"] <= r["high"] + 1e-9):
            out.append(
                {
                    "date": r["date"],
                    "type": "close_out_of_range",
                    "level": "error",
                    "msg": f"close {r['close']:.4f} 不在 [low {r['low']:.4f}, high {r['high']:.4f}] 内",
                }
            )

        if i == 0:
            continue

        prev = rows[i - 1]

        # 2) 复权因子异常下调 (后复权因子应单调不减)
        d = _rng_factor(prev["factor"], r["factor"])
        if d < -FACTOR_DROP_ERROR:
            out.append(
                {
                    "date": r["date"],
                    "type": "factor_drop",
                    "level": "error",
                    "msg": f"因子 {prev['factor']:.4f} -> {r['factor']:.4f} ({d * 100:+.2f}%)",
                }
            )

        # 3) 真实价单日异动
        real_prev = prev["close"] / prev["factor"] if prev["factor"] else prev["close"]
        real_cur = r["close"] / r["factor"] if r["factor"] else r["close"]
        if real_prev:
            ret = real_cur / real_prev - 1.0
            if abs(ret) > limit:
                out.append(
                    {
                        "date": r["date"],
                        "type": "big_move",
                        "level": "info",
                        "msg": f"真实价单日 {ret * 100:+.1f}% (阈值 ±{limit * 100:.1f}%)",
                    }
                )

    # 4) 区间内缺失交易日 (可能停牌, 也可能是数据缺口)
    if rows:
        lo, hi = rows[0]["date"], rows[-1]["date"]
        have = {r["date"] for r in rows}
        for d in calendar:
            if lo <= d <= hi and d not in have:
                out.append(
                    {
                        "date": d,
                        "type": "missing_day",
                        "level": "info",
                        "msg": "缺失交易日 (可能是停牌, 也可能是数据缺口)",
                    }
                )

    out.sort(key=lambda x: (x["date"], x["type"]))
    return out


def _stats(rows, anomalies):
    counts: dict[str, int] = {}
    for a in anomalies:
        counts[a["type"]] = counts.get(a["type"], 0) + 1
    # 美股未复权拆股提示
    return {
        "rows": len(rows),
        "start": rows[0]["date"] if rows else None,
        "end": rows[-1]["date"] if rows else None,
        "anomaly_counts": counts,
    }


# --- 组装 payload ---------------------------------------------------------
def payload(market: str, qlib: str) -> dict:
    qlib = qlib.lower()
    meta = find(market, qlib)
    if meta is None:
        raise KeyError(f"未配置的标的: {market}/{qlib}")

    rows = load_rows(market, qlib)
    cal = market_calendar(market)
    anomalies = detect(rows, cal, market)

    # 美股: factor 恒 1 且出现拆股式跳变 -> 追加提示
    if market == "us" and rows and all(abs(r["factor"] - 1.0) < 1e-9 for r in rows):
        for i in range(1, len(rows)):
            c0, c1 = rows[i - 1]["close"], rows[i]["close"]
            if c0 and abs(c1 / c0 - 1.0) > SPLIT_LIKE:
                anomalies.append(
                    {
                        "date": rows[i]["date"],
                        "type": "us_unadjusted_split",
                        "level": "info",
                        "msg": f"疑似未复权拆股 ({c0:.2f} -> {c1:.2f}), factor 恒为 1",
                    }
                )
        anomalies.sort(key=lambda x: (x["date"], x["type"]))

    def arr(field, nd):
        return [round(r[field], nd) for r in rows]

    return {
        "meta": {
            "market": market,
            "market_label": MARKET_LABEL.get(market, market),
            "symbol": qlib,
            "name": meta["name"],
            "currency": CURRENCY.get(market, ""),
        },
        "dates": [r["date"] for r in rows],
        "hfq": {
            "open": arr("open", _price_round),
            "high": arr("high", _price_round),
            "low": arr("low", _price_round),
            "close": arr("close", _price_round),
        },
        "real": {
            "open": [round(r["open"] / r["factor"], _price_round) for r in rows],
            "high": [round(r["high"] / r["factor"], _price_round) for r in rows],
            "low": [round(r["low"] / r["factor"], _price_round) for r in rows],
            "close": [round(r["close"] / r["factor"], _price_round) for r in rows],
        },
        "volume": [int(r["volume"]) for r in rows],
        "factor": arr("factor", _factor_round),
        "anomalies": anomalies,
        "stats": _stats(rows, anomalies),
    }


def catalog() -> list[dict]:
    """给前端的股票清单: 每只带起止/行数/异常计数。"""
    out = []
    for s in symbols():
        rows = load_rows(s["market"], s["qlib"])
        anomalies = detect(rows, market_calendar(s["market"]), s["market"])
        counts = _stats(rows, anomalies)["anomaly_counts"]
        counts["error"] = sum(1 for a in anomalies if a["level"] == "error")
        out.append(
            {
                **s,
                "market_label": MARKET_LABEL.get(s["market"], s["market"]),
                "rows": len(rows),
                "start": rows[0]["date"] if rows else None,
                "end": rows[-1]["date"] if rows else None,
                "anomaly_counts": counts,
            }
        )
    return out


if __name__ == "__main__":
    print(f"标的 {len(symbols())} 只")
    for s in catalog():
        print(
            f"  [{s['market_label']}] {s['qlib']:<9} {s['name']:<8} "
            f"{s['rows']:>5} 行  {s['start']} .. {s['end']}"
        )
    print()
    for s in symbols():
        p = payload(s["market"], s["qlib"])
        c = p["stats"]["anomaly_counts"]
        print(f"  {s['market']}/{s['qlib']:<9} 异常 {c or '无'}")

"""用 pandas 直接计算常用技术指标, 输出一行 JSON。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "web"))
sys.path.insert(0, str(ROOT / "explore"))

import view  # noqa: E402

NODES = [
    "ma5", "ma10", "ma20", "ma60",
    "ema12", "ema26",
    "boll_up", "boll_mid", "boll_low",
    "macd_dif", "macd_dea", "macd_hist",
    "rsi6", "rsi12", "rsi24",
    "atr14",
]


def _num(x, nd: int):
    if x is None:
        return None
    try:
        if pd.isna(x):
            return None
    except Exception:
        pass
    try:
        v = float(x)
    except Exception:
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return round(v, nd)


def compute_series(market: str, symbol: str, mode: str = "real") -> dict:
    symbol = symbol.lower()
    if mode not in ("real", "hfq"):
        mode = "real"
    rows = view.load_rows(market, symbol)
    if not rows:
        return {"market": market, "symbol": symbol, "mode": mode, "node": NODES, "count": 0, "dates": [], "series": {n: [] for n in NODES}}

    df = pd.DataFrame(rows)
    df["date"] = df["date"].astype(str)
    if mode == "real":
        df["c"] = df["close"] / df["factor"]
        df["h"] = df["high"] / df["factor"]
        df["l"] = df["low"] / df["factor"]
    else:
        df["c"] = df["close"]
        df["h"] = df["high"]
        df["l"] = df["low"]

    c = df["c"]
    h = df["h"]
    l = df["l"]
    pc = c.shift(1)

    ma5 = c.rolling(5, min_periods=1).mean()
    ma10 = c.rolling(10, min_periods=1).mean()
    ma20 = c.rolling(20, min_periods=1).mean()
    ma60 = c.rolling(60, min_periods=1).mean()
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    std20 = c.rolling(20, min_periods=1).std()
    boll_mid = ma20
    boll_up = ma20 + 2 * std20
    boll_low = ma20 - 2 * std20

    macd_dif = ema12 - ema26
    macd_dea = macd_dif.ewm(span=9, adjust=False).mean()
    macd_hist = (macd_dif - macd_dea) * 2

    up = (c - pc).clip(lower=0).fillna(0.0)
    dn = (pc - c).clip(lower=0).fillna(0.0)

    def rsi(n):
        rs = up.rolling(n, min_periods=1).mean() / (dn.rolling(n, min_periods=1).mean() + 1e-9)
        return 100.0 - 100.0 / (1.0 + rs)

    rsi6 = rsi(6)
    rsi12 = rsi(12)
    rsi24 = rsi(24)

    tr1 = h - l
    tr2 = (h - pc).abs()
    tr3 = (l - pc).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr14 = tr.ewm(span=14, adjust=False).mean()

    series = {
        "ma5": [_num(v, 4) for v in ma5.tolist()],
        "ma10": [_num(v, 4) for v in ma10.tolist()],
        "ma20": [_num(v, 4) for v in ma20.tolist()],
        "ma60": [_num(v, 4) for v in ma60.tolist()],
        "ema12": [_num(v, 4) for v in ema12.tolist()],
        "ema26": [_num(v, 4) for v in ema26.tolist()],
        "boll_up": [_num(v, 4) for v in boll_up.tolist()],
        "boll_mid": [_num(v, 4) for v in boll_mid.tolist()],
        "boll_low": [_num(v, 4) for v in boll_low.tolist()],
        "macd_dif": [_num(v, 4) for v in macd_dif.tolist()],
        "macd_dea": [_num(v, 4) for v in macd_dea.tolist()],
        "macd_hist": [_num(v, 4) for v in macd_hist.tolist()],
        "rsi6": [_num(v, 4) for v in rsi6.tolist()],
        "rsi12": [_num(v, 4) for v in rsi12.tolist()],
        "rsi24": [_num(v, 4) for v in rsi24.tolist()],
        "atr14": [_num(v, 4) for v in atr14.tolist()],
    }

    return {
        "market": market,
        "symbol": symbol,
        "mode": mode,
        "node": NODES,
        "count": len(df),
        "dates": df["date"].tolist(),
        "series": series,
    }


def main() -> None:
    market, symbol = sys.argv[1], sys.argv[2].lower()
    mode = sys.argv[3] if len(sys.argv) > 3 else "real"
    out = compute_series(market, symbol, mode)
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()

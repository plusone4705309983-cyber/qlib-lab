"""用 qlib 读 .bin provider, 与配置对照后输出一行 JSON。

由 app.py 以子进程调用, 目的是把 qlib 的全局 init / Windows 多进程隔离在
主进程之外 (服务进程里反复 init 不同 region 容易出问题)。

    python web/bincheck.py cn sh600519
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "explore"))


def main() -> None:
    market, symbol = sys.argv[1], sys.argv[2].lower()

    import _common

    _common.launch(market)

    import pandas as pd
    from qlib.data import D

    sym = symbol.upper()
    df = D.features([sym], ["$close", "$factor"])
    df = df[df["$close"].notna()]
    cal = pd.DatetimeIndex(D.calendar())

    dt = df.index.get_level_values("datetime") if len(df) else []
    out = {
        "market": market,
        "symbol": symbol,
        "bin": {
            "rows": int(len(df)),
            "start": str(dt.min().date()) if len(df) else None,
            "end": str(dt.max().date()) if len(df) else None,
            "last_close": round(float(df["$close"].iloc[-1]), 4) if len(df) else None,
            "last_factor": round(float(df["$factor"].iloc[-1]), 6) if len(df) else None,
            "calendar_end": str(cal[-1].date()),
        },
    }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()

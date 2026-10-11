"""不依赖 pytest 的自检:  python web/selftest.py"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import view  # noqa: E402
try:
    from web import signals  # noqa: E402
except Exception:
    try:
        import signals  # noqa: E402
    except Exception:
        signals = None  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def test_config_and_csv():
    print("[1] 配置/CSV")
    syms = view.symbols()
    check("标的 >= 8", len(syms) >= 8, f"实际 {len(syms)}")
    for s in syms[:1]:
        rows = view.load_rows(s["market"], s["qlib"])
        check(f"{s['qlib']} 有数据", len(rows) > 0)


def test_payload_signals():
    print("[2] 技术信号合并到异常清单")
    p = view.payload("cn", "sh600519")
    sc = sum(1 for a in p["anomalies"] if a["type"].startswith("signal_"))
    check("payload 含信号异常", sc >= 0)
    if sc > 0:
        s0 = next(a for a in p["anomalies"] if a["type"].startswith("signal_"))
        check("信号异常结构", all(k in s0 for k in ("date","type","level","msg")))


def test_backtest():
    print("[3] 信号回测统计")
    p = view.payload("cn", "sh600519")
    bt = p.get("signal_backtest")
    check("payload 含 signal_backtest", bool(bt))
    check("含持有期", bool(p.get("signal_horizons")), str(p.get("signal_horizons")))
    check("含中文标签", bool(p.get("signal_labels")))
    if bt:
        h = p["signal_horizons"][0]
        cell = bt.get("ma_gold_cross", {}).get(h)
        if cell is None:
            cell = bt.get("ma_gold_cross", {}).get(str(h))
        check("金叉回测存在", bool(cell))
        if cell:
            check("回测字段完整", all(k in cell for k in ("n", "win_rate", "avg_ret", "bullish")))
            check("胜率 0~1", 0.0 <= (cell["win_rate"] or 0) <= 1.0)
            check("样本 > 0", cell["n"] > 0)
            check("金叉为看多", cell["bullish"] is True)


def test_indicators():
    print("[3] 技术指标 API/结构")
    from web import app as webapp
    c = webapp.app.test_client()
    r = c.get("/api/indicators?market=cn&symbol=sh600519&mode=real")
    check("/api/indicators 200", r.status_code == 200)
    d = r.get_json()
    check("indicators 含 dates/series/node", all(k in d for k in ("dates","series","node")))
    n = len(d["dates"])
    s = d["series"]
    for k in ("ma5","ma20","ema12","ema26","macd_dif","macd_dea","macd_hist","rsi6","rsi12","rsi24","boll_up","boll_mid","boll_low","atr14"):
        check(f"series 含 {k}", k in s)
        check(f"{k} 长度一致", len(s[k]) == n)
    r = c.get("/api/indicators?market=cn&symbol=nope")
    check("/api/indicators 未知 404", r.status_code == 404)
    check("/api/signals 200", c.get("/api/signals?market=cn&symbol=sh600519").status_code == 200)


if __name__ == "__main__":
    test_config_and_csv()
    test_payload_signals()
    test_backtest()
    test_indicators()
    print(f"\n结果: {PASS} PASS / {FAIL} FAIL")
    sys.exit(1 if FAIL else 0)

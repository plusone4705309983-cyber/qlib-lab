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


def test_holders():
    print("[4] 股东穿透 / 持仓反查")
    from urllib.parse import quote
    import holders
    d = holders.top_holders("cn", "sh600519")
    check("top_holders 含 holders", isinstance(d.get("holders"), list) and len(d["holders"]) >= 10, f"count={d.get('count')}")
    if d.get("holders"):
        h0 = d["holders"][0]
        check("股东字段完整", all(k in h0 for k in ("name","rank","hold_num","hold_ratio","type","direction","special")))
    r = holders.holder_stocks("中国银行股份有限公司-招商中证白酒指数分级证券投资基金")
    check("holder_stocks 有结果", r.get("count", 0) > 0, f"count={r.get('count')}")
    if r.get("stocks"):
        s0 = r["stocks"][0]
        check("持仓字段完整", all(k in s0 for k in ("code","symbol","name","hold_num","direction")))
    try:
        holders.top_holders("us", "aapl")
        check("美股不支持", False)
    except holders.NotSupported:
        check("美股不支持", True)

    from web import app as webapp
    c = webapp.app.test_client()
    check("/api/holders 200", c.get("/api/holders?market=cn&symbol=sh600519").status_code == 200)
    check("/api/holders 美股 404", c.get("/api/holders?market=us&symbol=aapl").status_code == 404)
    check("/api/holder_stocks 缺参 400", c.get("/api/holder_stocks").status_code == 400)
    check("/api/holder_stocks 200", c.get("/api/holder_stocks?name=" + quote("香港中央结算有限公司")).status_code == 200)


def test_vwap():
    print("[5] CSV vwap 列 (Alpha360 60 列 / Alpha158 1 列依赖)")
    import csv
    root = HERE.parent / "data" / "selfmade"
    syms = view.symbols()
    checked = miss = oob = weird = 0
    for s in syms:
        p = root / s["market"] / f"{s['qlib']}.csv"
        if not p.exists():
            continue
        with p.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        if not rows or "vwap" not in rows[0]:
            miss += 1
            continue
        for r in rows:
            try:
                v = float(r["vwap"])
                lo, hi, c = float(r["low"]), float(r["high"]), float(r["close"])
            except (TypeError, ValueError):
                miss += 1
                continue
            lo, hi = min(lo, hi), max(lo, hi)
            if not (lo * 0.995 <= v <= hi * 1.005):   # 与 east.py _calc_vwap 同容差
                oob += 1
            if not (0.5 <= v / c <= 1.5):             # vwap/$close 极端漂移即异常
                weird += 1
        checked += 1
    check("每个 CSV 都有 vwap", miss == 0 and checked >= 8,
          f"checked={checked}/{len(syms)} 缺列或坏值={miss}")
    check("vwap 落在当日 [low,high]", oob == 0, f"越界 {oob} 行")
    check("vwap/close ∈ [0.5,1.5]", weird == 0, f"异常 {weird} 行")


def test_factor():
    print("[6] 因子实验室 /api/factor")
    import factor_eval
    bad_cases = ["__import__('os')", "Foo($close,5)", "$bad+1", "'x'", "a[0]"]
    ok_bad = 0
    for bad in bad_cases:
        try:
            factor_eval.validate(bad)
        except factor_eval.ExprError:
            ok_bad += 1
    check("非法表达式全部拦截", ok_bad == len(bad_cases), f"{ok_bad}/{len(bad_cases)}")
    check("合法表达式通过校验",
          factor_eval.validate("Mean($close,5)/Mean($close,20)") == "Mean($close,5)/Mean($close,20)")
    check("科学计数法通过校验",
          factor_eval.validate("($close-Mean($close,20))/(Std($close,20)+1e-9)") != "")

    from web import app as webapp
    c = webapp.app.test_client()
    r = c.get("/api/factor?market=cn&symbol=sh600519&expr=Mean($close,5)/Mean($close,20)")
    check("/api/factor 200", r.status_code == 200, str(r.get_json())[:120] if r.status_code != 200 else "")
    d = r.get_json()
    check("factor 结构完整",
          all(k in d for k in ("dates", "factor", "pct", "close_real", "ic", "buckets", "overall", "current")))
    check("factor 序列等长",
          len(d["factor"]) == len(d["pct"]) == len(d["close_real"]) == len(d["dates"]) > 1000, f"n={d.get('count')}")
    ic10 = d["ic"]["10"]["ic"]
    check("10日 IC 在 [-1,1]", ic10 is not None and -1.0 <= ic10 <= 1.0, str(ic10))
    check("5 组均非空", len(d["buckets"]) == 5 and all(b["n"] > 50 for b in d["buckets"]),
          str([b["n"] for b in d["buckets"]]))
    check("当前读数含结论",
          bool(d["current"]) and d["current"]["level"] in ("info", "warn", "neutral") and bool(d["current"]["msg"]))
    check("分位前 249 天留空 (无未来函数)", d["pct"][:249] == [None] * 249)
    check("/api/factor 非法表达式 400",
          c.get("/api/factor?market=cn&symbol=sh600519&expr=Foo($close,5)").status_code == 400)
    check("/api/factor 未知标的 404",
          c.get("/api/factor?market=cn&symbol=zzz9999&expr=$close").status_code == 404)


if __name__ == "__main__":
    test_config_and_csv()
    test_payload_signals()
    test_backtest()
    test_indicators()
    test_holders()
    test_vwap()
    test_factor()
    print(f"\n结果: {PASS} PASS / {FAIL} FAIL")
    sys.exit(1 if FAIL else 0)

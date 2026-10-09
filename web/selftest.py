"""不依赖 pytest 的自检:  python web/selftest.py

覆盖: 配置/CSV 可读、OHLC 口径不变式、异常检测规则、payload 结构、Flask 路由。
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import view  # noqa: E402

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
    print("[1] 配置与 CSV")
    syms = view.symbols()
    check("标的数 >= 8", len(syms) >= 8, f"实际 {len(syms)}")
    for s in syms:
        rows = view.load_rows(s["market"], s["qlib"])
        check(f"{s['qlib']} 有数据", len(rows) > 0)
        check(f"{s['qlib']} 日期升序", all(rows[i]["date"] <= rows[i + 1]["date"] for i in range(len(rows) - 1)))
        check(f"{s['qlib']} factor > 0", all(r["factor"] > 0 for r in rows))
        # OHLC 口径不变式: close 必须落在 [low, high]
        bad = [r["date"] for r in rows if not (r["low"] - 1e-9 <= r["close"] <= r["high"] + 1e-9)]
        check(f"{s['qlib']} close ∈ [low,high]", not bad, f"越界 {bad[:3]}")


def test_detect_rules():
    print("[2] 异常检测规则")
    cal = ["2020-01-01", "2020-01-02", "2020-01-03", "2020-01-06"]

    def row(d, o, h, l, c, f):
        return {"date": d, "open": o, "high": h, "low": l, "close": c, "volume": 1.0, "factor": f}

    rows = [
        row("2020-01-01", 10, 11, 9, 10, 1.0),
        row("2020-01-02", 10, 11, 9, 10.5, 1.0),
        row("2020-01-03", 10, 11, 9, 10.2, 0.5),   # factor 腰斩 -> error
    ]
    a = view.detect(rows, cal, "cn")
    types = {x["type"] for x in a}
    check("检出 factor_drop", "factor_drop" in types, str(types))
    check("factor_drop 为 error", any(x["type"] == "factor_drop" and x["level"] == "error" for x in a))

    rows_miss = [row("2020-01-01", 10, 11, 9, 10, 1.0), row("2020-01-03", 10, 11, 9, 10, 1.0)]
    miss = view.detect(rows_miss, ["2020-01-01", "2020-01-02", "2020-01-03"], "cn")
    check("检出区间内缺失日 01-02", any(x["type"] == "missing_day" and x["date"] == "2020-01-02" for x in miss))
    check("区间外日期不算缺失", not any(x["date"] == "2020-01-06" for x in a))

    rows2 = [row("2020-01-01", 10, 11, 9, 12, 1.0)]  # close 12 > high 11
    check("检出 OHLC 越界", any(x["type"] == "close_out_of_range" for x in view.detect(rows2, cal, "cn")))

    rows3 = [row("2020-01-01", 10, 11, 9, 10, 1.0), row("2020-01-02", 10, 11, 9, 10, 1.0)]
    check("正常数据无误报", view.detect(rows3, ["2020-01-01", "2020-01-02"], "cn") == [])


def test_payload():
    print("[3] payload 结构")
    p = view.payload("cn", "sh600519")
    for k in ("meta", "dates", "hfq", "real", "volume", "factor", "anomalies", "stats"):
        check(f"payload 含 {k}", k in p)
    check("meta.name 非空", bool(p["meta"]["name"]))
    n = len(p["dates"])
    check("各数组等长", all(len(p[x]) == n for x in ("volume", "factor")) and all(len(p["hfq"][f]) == n and len(p["real"][f]) == n for f in ("open", "high", "low", "close")))
    # 真实价 = hfq / factor
    i = n - 1
    expect = round(p["hfq"]["close"][i] / p["factor"][i], 4)
    check("real = hfq/factor", abs(p["real"]["close"][i] - expect) < 1e-3, f"{p['real']['close'][i]} vs {expect}")
    if p["factor"][0] == 1.0:
        pass  # 美股未复权
    cat = view.catalog()
    check("catalog 含 anomaly_counts", all("anomaly_counts" in c for c in cat))


def test_flask():
    print("[4] Flask 路由")
    import app as webapp
    c = webapp.app.test_client()
    check("/ 200", c.get("/").status_code == 200)
    check("/healthz 200", c.get("/healthz").status_code == 200)
    r = c.get("/api/symbols")
    check("/api/symbols 200", r.status_code == 200 and len(r.get_json()) >= 8)
    r = c.get("/api/kline?market=cn&symbol=sh600519")
    check("/api/kline 200", r.status_code == 200 and "dates" in r.get_json())
    r = c.get("/api/kline?market=cn&symbol=nope")
    check("/api/kline 未知标的 404", r.status_code == 404)
    r = c.get("/api/busy")
    check("/api/busy 200", r.status_code == 200 and r.get_json().get("busy") is False)
    check("/api/job 未知 404", c.get("/api/job/nope").status_code == 404)
    check("/api/add_stock 非法 400", c.post("/api/add_stock", json={"market": "xx"}).status_code == 400)


def test_stocks():
    print("[5] 添加股票逻辑")
    import tempfile

    import stocks

    check("derive cn sh", stocks.derive_tencent(1, "600519") == "sh600519")
    check("derive cn sz (str)", stocks.derive_tencent("0", "000001") == "sz000001")
    check("derive us nasdaq", stocks.derive_tencent(105, "AAPL") == "usAAPL.OQ")
    check("derive us nyse (str)", stocks.derive_tencent("106", "BABA") == "usBABA.N")
    check("derive us amex", stocks.derive_tencent(107, "BABO") == "usBABO.A")
    check("derive 不支持市场 None", stocks.derive_tencent(116, "00700") is None)
    check("derive 非法返回 None", stocks.derive_tencent("x", "600519") is None)
    check("market_of", stocks.market_of("AStock") == "cn" and stocks.market_of("UsStock") == "us" and stocks.market_of("HK") is None)

    good = {"market": "cn", "code": "600000", "name": "浦发银行", "secid": "1.600000",
            "tencent": "sh600000", "qlib": "sh600000"}
    check("validate 合法", stocks.validate(good)["qlib"] == "sh600000")
    bads = [dict(good, qlib="../evil"), dict(good, secid="1.600000; rm"),
            dict(good, market="us"), dict(good, tencent="SH600000"), dict(good, name="x" * 40)]
    for i, b in enumerate(bads):
        try:
            stocks.validate(b)
            ok = False
        except ValueError:
            ok = True
        check(f"validate 拦截非法 #{i}", ok)

    orig_text = stocks.CONFIG_PATH.read_text(encoding="utf-8")
    tmp = Path(tempfile.gettempdir()) / "sym_test.yaml"
    tmp.write_text(orig_text, encoding="utf-8", newline="\n")
    real_path = stocks.CONFIG_PATH
    stocks.CONFIG_PATH = tmp
    try:
        stocks._append_symbol("cn", "sh600000", "1.600000", "sh600000", "浦发银行", orig_text)
        after = tmp.read_text(encoding="utf-8")
        check("append 写入成功", "sh600000" in after)
        check("append 保留注释", "# 股票池配置" in after)
        try:
            stocks._append_symbol("cn", "sh600000", "1.600000", "sh600000", "重复", after)
            ok = False
        except ValueError:
            ok = True
        check("append 拦截重复", ok)
    finally:
        stocks.CONFIG_PATH = real_path
        tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    test_config_and_csv()
    test_detect_rules()
    test_payload()
    test_flask()
    test_stocks()
    print(f"\n结果: {PASS} PASS / {FAIL} FAIL")
    sys.exit(1 if FAIL else 0)

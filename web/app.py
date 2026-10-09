"""Qlib 数据检视器 —— Flask 应用 (只读)。

    python web/app.py                 # 默认 127.0.0.1:8853 (供 Cloudflare Tunnel)
    python web/app.py --port 8080
    python web/app.py --debug         # Flask 自带调试服务器

默认只监听 127.0.0.1 (回环), 公网访问经 cloudflared 隧道转发, 不直接暴露端口。
"""

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import stocks  # noqa: E402
import view  # noqa: E402

from flask import Flask, jsonify, render_template, request  # noqa: E402

app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


@app.after_request
def _no_cache(resp):
    # 静态资源改由浏览器带 ETag 重新校验; HTML 不缓存, 避免手机上拿到旧页面
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/symbols")
def api_symbols():
    return jsonify(view.catalog())


@app.get("/api/kline")
def api_kline():
    market = request.args.get("market", "cn")
    symbol = request.args.get("symbol", "").lower()
    try:
        return jsonify(view.payload(market, symbol))
    except KeyError as exc:
        return jsonify({"error": str(exc).strip("'\"")}), 404
    except FileNotFoundError as exc:
        return jsonify({"error": f"CSV 不存在: {exc.filename}"}), 404


@app.get("/api/check_bin")
def api_check_bin():
    market = request.args.get("market", "cn")
    symbol = request.args.get("symbol", "").lower()
    if view.find(market, symbol) is None:
        return jsonify({"error": f"未配置的标的: {market}/{symbol}"}), 404

    try:
        proc = subprocess.run(
            [sys.executable, str(HERE / "bincheck.py"), market, symbol],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "qlib 读取 .bin 超时"}), 504

    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "bincheck 失败").strip().splitlines()
        return jsonify({"error": msg[-1] if msg else "bincheck 失败"}), 500

    lines = [x for x in proc.stdout.splitlines() if x.strip().startswith("{")]
    if not lines:
        return jsonify({"error": "bincheck 无输出"}), 500
    result = json.loads(lines[-1])

    rows = view.load_rows(market, symbol)
    csv = {
        "rows": len(rows),
        "start": rows[0]["date"] if rows else None,
        "end": rows[-1]["date"] if rows else None,
        "last_close": round(rows[-1]["close"], 4) if rows else None,
        "last_factor": round(rows[-1]["factor"], 6) if rows else None,
    }
    b = result["bin"]
    result["csv"] = csv
    result["match"] = {
        "rows": csv["rows"] == b["rows"],
        "start": csv["start"] == b["start"],
        "end": csv["end"] == b["end"],
        "last_close_diff": round((b["last_close"] or 0.0) - (csv["last_close"] or 0.0), 6),
    }
    return jsonify(result)


@app.get("/healthz")
def healthz():
    return "ok"


@app.get("/api/search")
def api_search():
    q = request.args.get("q", "")
    try:
        return jsonify(stocks.search(q))
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"搜索失败: {exc}"}), 502


@app.post("/api/add_stock")
def api_add_stock():
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(stocks.start_add(payload))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409


@app.get("/api/job/<job_id>")
def api_job(job_id):
    job = stocks.get_job(job_id)
    if job is None:
        return jsonify({"error": "无此任务"}), 404
    return jsonify(job)


@app.get("/api/busy")
def api_busy():
    return jsonify({"busy": stocks.busy()})


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Qlib 数据检视器")
    ap.add_argument("--host", default="127.0.0.1", help="默认 127.0.0.1 (回环, 供隧道转发)")
    ap.add_argument("--port", type=int, default=8853)
    ap.add_argument("--debug", action="store_true", help="用 Flask 调试服务器")
    args = ap.parse_args()

    if args.debug:
        app.run(host=args.host, port=args.port, debug=True)
        return

    from waitress import serve

    print(f"数据检视器 -> http://{args.host}:{args.port}   (Ctrl+C 退出)")
    serve(app, host=args.host, port=args.port, threads=8)


if __name__ == "__main__":
    main()

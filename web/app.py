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

import broker_config  # noqa: E402,F401  先加载 .env，供 auth / db 读取 QLIB_* 配置
import auth  # noqa: E402
import db  # noqa: E402
import stocks  # noqa: E402
import view  # noqa: E402
import portfolio  # noqa: E402

from flask import Flask, Response, jsonify, render_template, request, url_for  # noqa: E402
from flask_login import current_user  # noqa: E402

app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
auth.init_app(app)

app.add_url_rule("/login", "login_page", auth.login_page, methods=["GET"])
app.add_url_rule("/login", "login_submit", auth.login_submit, methods=["POST"])
app.add_url_rule("/invite", "invite_page", auth.invite_page, methods=["GET"])
app.add_url_rule("/invite", "invite_submit", auth.invite_submit, methods=["POST"])
app.add_url_rule("/logout", "logout", auth.logout, methods=["GET", "POST"])
app.add_url_rule("/api/auth/me", "api_me", auth.api_me, methods=["GET"])



def _external_url(endpoint: str, **values) -> str:
    """生成公网链接：Cloudflare Tunnel 下 Flask 看到的是 http，链接要还原成 https。"""
    scheme = request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
    if scheme not in ("http", "https"):
        host = request.host.split(":")[0]
        scheme = "http" if host in ("127.0.0.1", "localhost", "::1") else "https"
    return url_for(endpoint, _external=True, _scheme=scheme, **values)


@app.after_request
def _no_cache(resp):
    # 静态资源改由浏览器带 ETag 重新校验; HTML 不缓存, 避免手机上拿到旧页面
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------- 自选股
def _watchlist_for_user(user_id: int) -> list[dict]:
    """指定用户的自选股 + 行情元信息；顺序沿用 config/symbols.yaml。"""
    keys = {(x["market"], x["symbol"]) for x in db.list_watchlist(user_id)}
    return [s for s in view.catalog() if (s["market"], s["qlib"]) in keys]


def _watchlist_catalog() -> list[dict]:
    """当前用户自选股 + 行情元信息；顺序沿用 config/symbols.yaml。"""
    return _watchlist_for_user(current_user.id)


@app.get("/api/symbols")
def api_symbols():
    """兼容旧前端：现在返回当前用户的自选股，而不是全局股票池。"""
    return jsonify(_watchlist_catalog())


@app.get("/api/catalog")
def api_catalog():
    """全量已下载标的，供「管理自选」挑选。"""
    keys = {(x["market"], x["symbol"]) for x in db.list_watchlist(current_user.id)}
    out = []
    for s in view.catalog():
        item = dict(s)
        item["in_watchlist"] = (s["market"], s["qlib"]) in keys
        out.append(item)
    return jsonify(out)


@app.get("/api/watchlist")
def api_watchlist():
    return jsonify({"symbols": _watchlist_catalog()})


@app.post("/api/watchlist")
def api_watchlist_add():
    payload = request.get_json(silent=True) or {}
    market = str(payload.get("market") or "").strip().lower()
    symbol = str(payload.get("symbol") or "").strip().lower()
    meta = view.find(market, symbol)
    if meta is None:
        return jsonify({"error": f"未配置的标的: {market}/{symbol}"}), 404
    db.add_watchlist(current_user.id, meta["market"], meta["qlib"])
    return jsonify({"ok": True, "symbols": _watchlist_catalog()})


@app.delete("/api/watchlist/<market>/<symbol>")
def api_watchlist_remove(market, symbol):
    removed = db.remove_watchlist(current_user.id, market, symbol)
    return jsonify({"ok": True, "removed": removed, "symbols": _watchlist_catalog()})


# ---------------------------------------------------------------- 管理后台
@app.get("/admin/invites")
@auth.admin_required
def admin_invites_page():
    return render_template("admin_invites.html")


@app.get("/api/admin/invites")
@auth.admin_required
def api_admin_invites():
    users = db.list_users()
    for user in users:
        user["is_self"] = user["id"] == current_user.id
    return jsonify(
        {
            "invites": db.list_invites(),
            "users": users,
            "quota": {
                "window_hours": 24,
                "max": 50,
                "used": db.count_invites_since(current_user.id, 24),
            },
        }
    )


@app.post("/api/admin/invites")
@auth.admin_required
def api_admin_invite_create():
    payload = request.get_json(silent=True) or {}
    used = db.count_invites_since(current_user.id, 24)
    if used >= 50:
        return jsonify({"error": "24 小时内生成邀请码过多，请稍后再试"}), 429
    try:
        invite = db.create_invite(
            current_user.id,
            max_uses=payload.get("max_uses", 1),
            expires_hours=payload.get("expires_hours", db.DEFAULT_INVITE_HOURS),
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    link = _external_url("invite_page", code=invite["code"])
    return jsonify({"invite": invite, "link": link})


@app.delete("/api/admin/invites/<int:invite_id>")
@auth.admin_required
def api_admin_invite_revoke(invite_id):
    if not db.revoke_invite(invite_id):
        return jsonify({"error": "邀请码不存在"}), 404
    return jsonify({"ok": True})


def _admin_target(user_id: int):
    user = db.get_user(user_id)
    if user is None:
        return None, (jsonify({"error": "用户不存在"}), 404)
    return user, None


@app.get("/api/admin/users")
@auth.admin_required
def api_admin_users():
    users = db.list_users()
    for user in users:
        user["is_self"] = user["id"] == current_user.id
    return jsonify({"users": users})


@app.get("/api/admin/users/<int:user_id>/watchlist")
@auth.admin_required
def api_admin_user_watchlist(user_id):
    user, error = _admin_target(user_id)
    if error:
        return error
    return jsonify({"user": user, "symbols": _watchlist_for_user(user_id)})


@app.post("/api/admin/users/<int:user_id>/status")
@auth.admin_required
def api_admin_user_status(user_id):
    user, error = _admin_target(user_id)
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    status = str(payload.get("status") or "").strip().lower()
    if status not in ("active", "disabled"):
        return jsonify({"error": "status 只能是 active 或 disabled"}), 400
    if user_id == current_user.id and status != "active":
        return jsonify({"error": "不能禁用当前登录的管理员账号"}), 400
    if (
        user["role"] == "admin"
        and status != "active"
        and db.count_active_admins(exclude_user_id=user_id) == 0
    ):
        return jsonify({"error": "至少需要保留一个启用中的管理员"}), 400
    try:
        changed = db.set_status(user_id, status)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not changed:
        return jsonify({"error": "用户不存在"}), 404
    return jsonify({"ok": True, "user": db.get_user(user_id)})


@app.post("/api/admin/users/<int:user_id>/password")
@auth.admin_required
def api_admin_user_password(user_id):
    user, error = _admin_target(user_id)
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    password = str(payload.get("password") or "")
    confirm = str(payload.get("confirm") or "")
    password_error = auth.validate_password(password)
    if password_error:
        return jsonify({"error": password_error}), 400
    if password != confirm:
        return jsonify({"error": "两次输入的密码不一致"}), 400
    db.set_password(user_id, auth.hash_password(password))
    return jsonify({"ok": True, "email": user["email"]})


@app.delete("/api/admin/users/<int:user_id>")
@auth.admin_required
def api_admin_user_delete(user_id):
    user, error = _admin_target(user_id)
    if error:
        return error
    if user_id == current_user.id:
        return jsonify({"error": "不能删除当前登录的管理员账号"}), 400
    if user["role"] == "admin" and db.count_active_admins(exclude_user_id=user_id) == 0:
        return jsonify({"error": "至少需要保留一个启用中的管理员"}), 400
    if not db.delete_user(user_id):
        return jsonify({"error": "用户不存在"}), 404
    return jsonify({"ok": True})


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


@app.get("/api/indicators")
def api_indicators():
    market = request.args.get("market", "cn")
    symbol = request.args.get("symbol", "").lower()
    mode = request.args.get("mode", "real")
    if view.find(market, symbol) is None:
        return jsonify({"error": f"未配置的标的: {market}/{symbol}"}), 404
    try:
        return jsonify(view.compute_indicators(market, symbol, mode))
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/factor")
def api_factor():
    """因子实验室: 自定义 Qlib 表达式 -> 分位/IC/前向收益/当前读数。"""
    market = request.args.get("market", "cn")
    symbol = request.args.get("symbol", "").lower()
    expr = (request.args.get("expr") or "").strip()
    if view.find(market, symbol) is None:
        return jsonify({"error": f"未配置的标的: {market}/{symbol}"}), 404
    if not expr:
        return jsonify({"error": "缺少 expr 参数"}), 400
    try:
        import factorlab
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"因子模块不可用: {exc}"}), 500
    try:
        return jsonify(factorlab.compute(market, symbol, expr))
    except factorlab.factor_eval.ExprError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500


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


@app.get("/api/signals")
def api_signals():
    market = request.args.get("market", "cn")
    symbol = request.args.get("symbol", "").lower()
    mode = request.args.get("mode", "real")
    if view.find(market, symbol) is None:
        return jsonify({"error": f"未配置的标的: {market}/{symbol}"}), 404
    try:
        import signals as sigmod
        return jsonify(sigmod.compute_signals(market, symbol, mode))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/holders")
def api_holders():
    market = request.args.get("market", "cn")
    symbol = request.args.get("symbol", "").lower()
    if view.find(market, symbol) is None:
        return jsonify({"error": f"未配置的标的: {market}/{symbol}"}), 404
    try:
        import holders
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"股东模块不可用: {exc}"}), 500
    try:
        return jsonify(holders.top_holders(market, symbol))
    except holders.NotSupported as exc:
        return jsonify({"error": str(exc)}), 404
    except KeyError as exc:
        return jsonify({"error": str(exc).strip("'\"")}), 404
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.get("/api/holder_stocks")
def api_holder_stocks():
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"error": "缺少 name 参数"}), 400
    try:
        import holders
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"股东模块不可用: {exc}"}), 500
    try:
        return jsonify(holders.holder_stocks(name))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.get("/api/positions")
@auth.admin_required
def api_positions():
    try:
        return jsonify(portfolio.list_positions())
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/positions")
@auth.admin_required
def api_positions_add():
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(portfolio.add_lot(payload))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500


@app.patch("/api/positions/<pos_id>")
@auth.admin_required
def api_positions_patch(pos_id):
    payload = request.get_json(silent=True) or {}
    try:
        return jsonify(portfolio.update_settings(pos_id, payload))
    except KeyError:
        return jsonify({"error": "无此持仓"}), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500


@app.delete("/api/positions/<pos_id>")
@auth.admin_required
def api_positions_delete(pos_id):
    try:
        return jsonify(portfolio.delete_position(pos_id))
    except KeyError:
        return jsonify({"error": "无此持仓"}), 404
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500


@app.delete("/api/positions/<pos_id>/lots/<lot_id>")
@auth.admin_required
def api_positions_delete_lot(pos_id, lot_id):
    try:
        return jsonify(portfolio.delete_lot(pos_id, lot_id))
    except KeyError:
        return jsonify({"error": "无此持仓或买入记录"}), 404
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/positions/evaluate")
@auth.admin_required
def api_positions_evaluate():
    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids")
    if ids is not None and (not isinstance(ids, list) or not all(isinstance(x, str) for x in ids)):
        return jsonify({"error": "ids 必须是字符串数组"}), 400
    try:
        return jsonify(portfolio.evaluate(ids))
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"因子提醒刷新失败: {exc}"}), 500


@app.get("/api/quote")
def api_quote():
    market = request.args.get("market", "cn")
    symbol = request.args.get("symbol", "").lower()
    try:
        return jsonify(portfolio.quote(market, symbol))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/positions/export")
@app.get("/api/positions/export.csv")
@auth.admin_required
def api_positions_export():
    fmt = request.args.get("fmt", "json").lower()
    if fmt in ("csv", "xlsx"):
        body = portfolio.export_csv()
        return Response(
            body,
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=positions.csv"},
        )
    try:
        body = portfolio.export_json()
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    return Response(
        body,
        mimetype="application/json; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=positions.json"},
    )


# ---------------------------------------------------------------- 模拟交易（Alpaca Paper）
@app.get("/api/broker/status")
@auth.admin_required
def api_broker_status():
    try:
        from broker_config import BrokerConfigError, config
        from brokers import get_broker

        config.require_paper()
        broker = get_broker()
        account = broker.get_account()
        return jsonify({"configured": config.configured, "paper": True, "account": account, "config": config.summary()})
    except BrokerConfigError as exc:
        return jsonify({"error": str(exc), "configured": False}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.get("/api/broker/positions")
@auth.admin_required
def api_broker_positions():
    try:
        from brokers import get_broker

        return jsonify({"positions": get_broker().get_positions()})
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.get("/api/broker/orders")
@auth.admin_required
def api_broker_orders():
    try:
        from brokers import get_broker

        status = request.args.get("status")
        limit = request.args.get("limit", 50)
        try:
            limit = int(limit)
        except Exception:
            limit = 50
        return jsonify({"orders": get_broker().get_orders(status=status, limit=limit)})
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.get("/api/broker/log")
@auth.admin_required
def api_broker_log():
    try:
        from brokers.orders_log import get_log

        limit = request.args.get("limit", 30)
        try:
            limit = int(limit)
        except Exception:
            limit = 30
        return jsonify({"log": get_log(limit)})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.post("/api/broker/preview")
@auth.admin_required
def api_broker_preview():
    payload = request.get_json(silent=True) or {}
    symbol = str(payload.get("symbol") or "").strip().upper()
    side = str(payload.get("side") or "buy").strip().lower()
    qty = payload.get("qty")
    notional = payload.get("notional")
    if side not in ("buy", "sell"):
        return jsonify({"error": "side 只能是 buy 或 sell"}), 400
    if symbol == "":
        return jsonify({"error": "缺少 symbol"}), 400
    try:
        from broker_config import BrokerConfigError, config
        from brokers import get_broker

        config.require_paper()
        if not config.configured:
            raise BrokerConfigError("缺少 Alpaca Paper 密钥：请在 .env 设置 APCA_API_KEY_ID / APCA_API_SECRET_KEY")
        account = get_broker().get_account()
        buying_power = account.get("buying_power")
        cash = account.get("cash")
        preview = {
            "symbol": symbol,
            "side": side,
            "type": "market",
            "time_in_force": "day",
            "qty": qty,
            "notional": notional,
            "account": {"buying_power": buying_power, "cash": cash},
        }
        return jsonify({"preview": preview, "note": "市价单将在美股交易时段撮合，实际成交价可能与点击时价格略有偏差"})
    except BrokerConfigError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.post("/api/broker/order")
@auth.admin_required
def api_broker_order():
    payload = request.get_json(silent=True) or {}
    symbol = str(payload.get("symbol") or "").strip().upper()
    side = str(payload.get("side") or "buy").strip().lower()
    qty = payload.get("qty")
    notional = payload.get("notional")
    order_type = str(payload.get("type") or "market").strip().lower()
    tif = str(payload.get("time_in_force") or "day").strip().lower()
    if side not in ("buy", "sell"):
        return jsonify({"error": "side 只能是 buy 或 sell"}), 400
    if symbol == "":
        return jsonify({"error": "缺少 symbol"}), 400
    if order_type != "market":
        return jsonify({"error": "MVP 仅支持市价单（type=market）"}), 400
    if tif != "day":
        return jsonify({"error": "MVP 仅支持 day（time_in_force=day）"}), 400
    if qty is None and notional is None:
        return jsonify({"error": "需提供 qty（股数）或 notional（金金额）"}), 400
    try:
        from brokers import get_broker
        from brokers.orders_log import log_order

        body = {"symbol": symbol, "side": side, "type": order_type, "time_in_force": tif}
        if qty is not None:
            # Alpaca 接受字符串或数字，尽量用数值但兼容
            try:
                body["qty"] = float(qty)
            except Exception:
                body["qty"] = qty
        if notional is not None:
            try:
                body["notional"] = float(notional)
            except Exception:
                body["notional"] = notional
        order = get_broker().place_order(body)
        log_order({"action": "place_order", "body": body, "result": order})
        return jsonify({"order": order})
    except RuntimeError as exc:
        try:
            from brokers.orders_log import log_order

            log_order({"action": "place_order_error", "body": payload, "error": str(exc)})
        except Exception:
            pass
        return jsonify({"error": str(exc)}), 500
    except Exception as exc:  # noqa: BLE001
        try:
            from brokers.orders_log import log_order

            log_order({"action": "place_order_error", "body": payload, "error": str(exc)})
        except Exception:
            pass
        return jsonify({"error": str(exc)}), 500


@app.post("/api/broker/orders/<order_id>/cancel")
@auth.admin_required
def api_broker_cancel(order_id):
    try:
        from brokers import get_broker
        from brokers.orders_log import log_order

        res = get_broker().cancel_order(order_id)
        log_order({"action": "cancel_order", "order_id": order_id, "result": res})
        return jsonify({"result": res})
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


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
        return jsonify(stocks.start_add(payload, user_id=current_user.id))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409


@app.get("/api/job/<job_id>")
def api_job(job_id):
    job = stocks.get_job(job_id, user_id=current_user.id)
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

    if not auth.admin_emails():
        print("警告: 未配置 QLIB_ADMIN_EMAIL，无法自动创建管理员/生成邀请码。")
    elif not db.has_admin_with_password():
        print("警告: 管理员还没有密码，请在 .env 设置 QLIB_ADMIN_PASSWORD 后重启。")

    if args.debug:
        app.run(host=args.host, port=args.port, debug=True)
        return

    from waitress import serve

    print(f"数据检视器 -> http://{args.host}:{args.port}   (Ctrl+C 退出)")
    serve(app, host=args.host, port=args.port, threads=8)


if __name__ == "__main__":
    main()

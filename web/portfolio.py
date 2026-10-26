"""持仓账本：本地 JSON 持久化 + 成本/浮盈 + 止损止盈/因子转弱提醒。

数据只存在本机 `data/private/positions.json`（已被 .gitignore 忽略，不会进仓库）。
所有写入都先写临时文件再 os.replace，并保留一份 `.bak`，避免断电/并发写坏账本。

设计边界：
- 价格用最新一根**收盘**真实价（`$close/$factor`），不是盘中实时价。
- 止损/止盈是规则化提醒，不自动下单，也不构成投资建议。
- 因子提醒需要显式点「刷新因子提醒」，因为 qlib 首次启动要 20~60 秒；
  没刷新时只显示上次检查结果，并标注是否落后于行情。
"""

from __future__ import annotations

import copy
import csv
import io
import json
import math
import os
import shutil
import threading
import uuid
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

import factor_eval  # noqa: E402  仅用 validate / ExprError
import factorlab  # noqa: E402  复用因子分析 / 进程内缓存
import view  # noqa: E402

PRIVATE_DIR = ROOT / "data" / "private"
POS_FILE = PRIVATE_DIR / "positions.json"
LOCK = threading.RLock()
EVAL_LOCK = threading.Lock()

DEFAULT_EXPR = "Mean($close,5)/Mean($close,20)"
DEFAULT_SETTINGS = {
    "stop_pct": 0.08,
    "take_profit_pct": 0.20,
    "trail_pct": 0.08,
    "time_stop_days": 20,
    "break_even_trigger": 0.10,
    "atr_mult": 2.0,
}

ALERT_RANK = {"danger": 0, "warn": 1, "info": 2, "ok": 3}


# ---------------------------------------------------------------- 文件读写
def _empty() -> dict:
    return {"version": 1, "positions": []}


def _bak_path() -> Path:
    return POS_FILE.with_name(POS_FILE.name + ".bak")


def _read_file(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        raw = {"version": 1, "positions": raw}
    if not isinstance(raw, dict) or not isinstance(raw.get("positions"), list):
        raise ValueError("positions.json 结构不正确")
    return raw


def _read_unlocked() -> dict:
    if not POS_FILE.exists():
        return _empty()
    try:
        return _read_file(POS_FILE)
    except Exception as exc:
        # 主文件损坏时尝试 .bak，仍失败就把具体错误抛给前端，绝不悄悄覆盖
        if _bak_path().exists():
            try:
                return _read_file(_bak_path())
            except Exception:
                pass
        raise RuntimeError(f"持仓文件损坏，无法读取: {POS_FILE} ({exc})") from exc


def _write_unlocked(positions: list[dict]) -> None:
    PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
    if POS_FILE.exists():
        try:
            shutil.copy2(POS_FILE, _bak_path())
        except OSError:
            pass
    tmp = POS_FILE.with_name(POS_FILE.name + ".tmp")
    data = {"version": 1, "positions": positions}
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, POS_FILE)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _load() -> dict:
    with LOCK:
        return copy.deepcopy(_read_unlocked())


def _save(positions: list[dict]) -> None:
    with LOCK:
        _write_unlocked(positions)


# ---------------------------------------------------------------- 校验
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _meta(market: str, symbol: str) -> dict:
    market = (market or "").strip().lower()
    symbol = (symbol or "").strip().lower()
    meta = view.find(market, symbol)
    if meta is None:
        raise ValueError(f"未配置的标的: {market}/{symbol}")
    return meta


def _float(value, label: str, minimum: float | None = None, maximum: float | None = None) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}必须是数字") from None
    if not math.isfinite(v):
        raise ValueError(f"{label}必须是有限数字")
    if minimum is not None and v < minimum:
        raise ValueError(f"{label}不能小于 {minimum}")
    if maximum is not None and v > maximum:
        raise ValueError(f"{label}不能大于 {maximum}")
    return v


def _date(value) -> str:
    s = str(value or "").strip()
    try:
        d = datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("买入日期格式应为 YYYY-MM-DD") from None
    if d > date.today():
        raise ValueError("买入日期不能晚于今天")
    return d.isoformat()


def _expr(value) -> str:
    s = str(value or "").strip()
    if not s:
        s = DEFAULT_EXPR
    try:
        return factor_eval.validate(s)
    except factor_eval.ExprError as exc:
        raise ValueError(str(exc)) from exc


def _settings(payload: dict, base: dict | None = None) -> dict:
    src = base or DEFAULT_SETTINGS
    out = {k: src.get(k, v) for k, v in DEFAULT_SETTINGS.items()}
    if "stop_pct" in payload:
        out["stop_pct"] = _float(payload.get("stop_pct"), "止损比例", 0.01, 0.50)
    if "take_profit_pct" in payload:
        out["take_profit_pct"] = _float(payload.get("take_profit_pct"), "止盈比例", 0.01, 5.00)
    if "trail_pct" in payload:
        out["trail_pct"] = _float(payload.get("trail_pct"), "移动止盈回撤", 0.01, 0.50)
    if "time_stop_days" in payload:
        out["time_stop_days"] = int(_float(payload.get("time_stop_days"), "时间止损天数", 1, 500))
    if "break_even_trigger" in payload:
        out["break_even_trigger"] = _float(payload.get("break_even_trigger"), "保本触发比例", 0.01, 2.00)
    if "atr_mult" in payload:
        out["atr_mult"] = _float(payload.get("atr_mult"), "ATR 倍数", 0.5, 10.0)
    return out


def _lot(payload: dict) -> dict:
    return {
        "id": _new_id(),
        "date": _date(payload.get("date")),
        "price": _float(payload.get("price"), "买入价", 0.000001),
        "qty": _float(payload.get("qty"), "数量", 0.000001),
        "fee": _float(payload.get("fee", 0), "手续费", 0.0),
        "note": str(payload.get("note") or "")[:300],
        "created_at": _now(),
    }


def _find(positions: list[dict], pos_id: str) -> dict:
    for p in positions:
        if p.get("id") == pos_id:
            return p
    raise KeyError(pos_id)


# ---------------------------------------------------------------- 增删改
def add_lot(payload: dict) -> list[dict]:
    """新增一笔买入。已有同标的持仓时追加到该持仓，不改动原设置。"""
    meta = _meta(payload.get("market"), payload.get("symbol"))
    lot = _lot(payload)

    with LOCK:
        doc = _read_unlocked()
        positions = doc["positions"]
        pos = next(
            (p for p in positions if p.get("market") == meta["market"] and p.get("symbol") == meta["qlib"]),
            None,
        )
        if pos is None:
            pos = {
                "id": _new_id(),
                "market": meta["market"],
                "symbol": meta["qlib"],
                "name": meta.get("name") or meta["qlib"],
                "expr": _expr(payload.get("expr")),
                **DEFAULT_SETTINGS,
                **_settings(payload),
                "lots": [],
                "last_signal": None,
                "last_signal_error": None,
                "created_at": _now(),
                "updated_at": _now(),
            }
            positions.append(pos)
        pos["lots"].append(lot)
        pos["updated_at"] = _now()
        _write_unlocked(positions)
    return list_positions()


def update_settings(pos_id: str, payload: dict) -> list[dict]:
    with LOCK:
        doc = _read_unlocked()
        positions = doc["positions"]
        pos = _find(positions, pos_id)
        if "expr" in payload:
            pos["expr"] = _expr(payload.get("expr"))
            # 因子表达式变了，旧信号不再适用
            pos["last_signal"] = None
            pos["last_signal_error"] = None
        pos.update(_settings(payload, pos))
        pos["updated_at"] = _now()
        _write_unlocked(positions)
    return list_positions()


def delete_position(pos_id: str) -> list[dict]:
    with LOCK:
        doc = _read_unlocked()
        positions = doc["positions"]
        _find(positions, pos_id)
        positions[:] = [p for p in positions if p.get("id") != pos_id]
        _write_unlocked(positions)
    return list_positions()


def delete_lot(pos_id: str, lot_id: str) -> list[dict]:
    with LOCK:
        doc = _read_unlocked()
        positions = doc["positions"]
        pos = _find(positions, pos_id)
        before = len(pos["lots"])
        pos["lots"] = [x for x in pos["lots"] if x.get("id") != lot_id]
        if len(pos["lots"]) == before:
            raise KeyError(lot_id)
        if not pos["lots"]:
            positions.remove(pos)
        else:
            pos["updated_at"] = _now()
        _write_unlocked(positions)
    return list_positions()


# ---------------------------------------------------------------- 因子检查
def _signal_summary(data: dict, expr: str) -> dict:
    s = data.get("signal")
    dates = data.get("dates") or []
    last_date = dates[-1] if dates else None
    if not s:
        return {
            "expr": expr,
            "level": "unknown",
            "confidence": None,
            "score": None,
            "date": last_date,
            "reason": data.get("note") or "样本不足，无法计算 250 日滚动分位",
            "evaluated_at": _now(),
        }
    st = s.get("stats") or {}
    return {
        "expr": expr,
        "level": s.get("level"),
        "confidence": s.get("confidence"),
        "score": s.get("score"),
        "date": s.get("date"),
        "pct": s.get("pct"),
        "horizon": s.get("horizon"),
        "avg_ret": st.get("avg_ret"),
        "win_rate": st.get("win_rate"),
        "excess": st.get("excess"),
        "invalidate": s.get("invalidate"),
        "evaluated_at": _now(),
    }


def evaluate(ids: list[str] | None = None) -> list[dict]:
    """显式重新计算持仓的因子信号（force=True，绕过因子实验室缓存）。"""
    # 计算可能要几十秒；这里直接持有文件锁，避免计算期间新增的买入被旧快照覆盖。
    with EVAL_LOCK, LOCK:
        doc = _read_unlocked()
        positions = doc["positions"]
        targets = [p for p in positions if not ids or p.get("id") in ids]
        cache: dict[tuple, tuple] = {}
        for p in targets:
            key = (p.get("market"), p.get("symbol"), p.get("expr"))
            if key not in cache:
                try:
                    cache[key] = ("ok", factorlab.compute(key[0], key[1], key[2], force=True))
                except Exception as exc:  # noqa: BLE001
                    cache[key] = ("err", str(exc))
            status, value = cache[key]
            if status == "ok":
                p["last_signal"] = _signal_summary(value, key[2])
                p["last_signal_error"] = None
            else:
                p["last_signal_error"] = value
                p["last_signal_error_at"] = _now()
        _write_unlocked(positions)
    return list_positions()


def cached_factor(meta: dict, expr: str) -> dict | None:
    try:
        return factorlab.cached(meta["market"], meta["qlib"], expr)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- 行情/指标
def quote(market: str, symbol: str) -> dict:
    meta = _meta(market, symbol)
    rows = view.load_rows(meta["market"], meta["qlib"])
    if not rows:
        raise RuntimeError(f"{meta['qlib']} 无行情数据")
    last = rows[-1]
    fac = last.get("factor") or 1.0
    return {
        "market": meta["market"],
        "symbol": meta["qlib"],
        "name": meta.get("name") or meta["qlib"],
        "market_label": view.MARKET_LABEL.get(meta["market"], meta["market"]),
        "currency": view.CURRENCY.get(meta["market"], ""),
        "date": last["date"],
        "price": round(last["close"] / fac, 4),
        "rows": len(rows),
        "start": rows[0]["date"],
        "end": last["date"],
    }


def _last_indicators(market: str, symbol: str) -> dict:
    try:
        ind = view.compute_indicators(market, symbol, "real")
        ser = ind.get("series") or {}

        def last(key):
            arr = ser.get(key) or []
            return arr[-1] if arr else None

        return {"atr14": last("atr14"), "ma20": last("ma20"), "ma60": last("ma60")}
    except Exception:  # noqa: BLE001  指标失败不应该让整个账本不可用
        return {"atr14": None, "ma20": None, "ma60": None}


def _factor_block(pos: dict, quote_date: str) -> dict:
    expr = pos.get("expr") or DEFAULT_EXPR
    cached = cached_factor(
        {"market": pos.get("market"), "qlib": pos.get("symbol")},
        expr,
    )
    stored = pos.get("last_signal")
    if stored and stored.get("expr") != expr:
        stored = None

    best = None
    if cached:
        best = (_signal_summary(cached, expr), "cache")
    if stored:
        # 日期相同则优先用刚在因子实验室算出的缓存结果
        if best is None or (stored.get("date") or "") > (best[0].get("date") or ""):
            best = (stored, "stored")

    error = pos.get("last_signal_error")
    if best is None:
        status = "error" if error else "missing"
        sig = None
        source = None
    else:
        sig, source = best
        if sig.get("level") == "unknown":
            status = "unknown"
        elif sig.get("date") and quote_date and sig["date"] < quote_date:
            status = "stale"
        else:
            status = "ok"

    return {
        "expr": expr,
        "status": status,
        "source": source,
        "signal": sig,
        "error": error,
    }


# ---------------------------------------------------------------- 计算
def _alert(level: str, typ: str, title: str, msg: str) -> dict:
    return {"level": level, "type": typ, "title": title, "msg": msg}


def _pct(v) -> str:
    return f"{v * 100:+.1f}%"


def _compute_position(raw: dict) -> dict:
    pos = copy.deepcopy(raw)
    market = pos.get("market")
    symbol = pos.get("symbol")
    lots = pos.get("lots") or []
    if not lots:
        raise RuntimeError("持仓没有买入记录")
    qty = sum(float(x.get("qty") or 0) for x in lots)
    cost = sum(float(x.get("price") or 0) * float(x.get("qty") or 0) + float(x.get("fee") or 0) for x in lots)
    if qty <= 0 or cost <= 0:
        raise RuntimeError("持仓数量或成本无效")
    avg_cost = cost / qty
    first_date = min(x.get("date") for x in lots if x.get("date"))
    last_lot = max(lots, key=lambda x: x.get("date") or "")

    q = quote(market, symbol)
    rows = view.load_rows(market, symbol)
    ind = _last_indicators(market, symbol)
    last_price = q["price"]
    last_date = q["date"]
    market_value = last_price * qty
    pnl = market_value - cost
    pnl_pct = pnl / cost

    since = []
    for r in rows:
        if r["date"] >= first_date:
            fac = r.get("factor") or 1.0
            since.append(r["close"] / fac)
    peak_price = max(since) if since else last_price
    trough_price = min(since) if since else last_price
    peak_pnl = peak_price / avg_cost - 1.0
    trough_pnl = trough_price / avg_cost - 1.0
    holding_td = sum(1 for r in rows if r["date"] > first_date)
    try:
        holding_days = max(0, (date.fromisoformat(last_date) - date.fromisoformat(first_date)).days)
    except ValueError:
        holding_days = 0

    stop_pct = float(pos.get("stop_pct", DEFAULT_SETTINGS["stop_pct"]))
    take_pct = float(pos.get("take_profit_pct", DEFAULT_SETTINGS["take_profit_pct"]))
    trail_pct = float(pos.get("trail_pct", DEFAULT_SETTINGS["trail_pct"]))
    time_days = int(pos.get("time_stop_days", DEFAULT_SETTINGS["time_stop_days"]))
    be_trigger = float(pos.get("break_even_trigger", DEFAULT_SETTINGS["break_even_trigger"]))
    atr_mult = float(pos.get("atr_mult", DEFAULT_SETTINGS["atr_mult"]))

    stop_fixed = avg_cost * (1.0 - stop_pct)
    atr = ind.get("atr14")
    stop_atr = avg_cost - atr_mult * atr if atr and atr > 0 else None
    stop_price = max([x for x in (stop_fixed, stop_atr) if x is not None])
    stop_kind = "ATR" if stop_atr is not None and stop_atr >= stop_fixed else "固定"
    if peak_pnl >= be_trigger and avg_cost > stop_price:
        stop_price = avg_cost
        stop_kind = "保本"
    trail_price = None
    if peak_pnl >= take_pct:
        trail_price = peak_price * (1.0 - trail_pct)
        if trail_price > stop_price:
            stop_price = trail_price
            stop_kind = "移动止盈"

    take_profit_price = avg_cost * (1.0 + take_pct)

    factor = _factor_block(pos, last_date)
    sig = factor.get("signal") or {}
    sig_level = sig.get("level")
    alerts: list[dict] = []

    # 行情日期与买入日期
    if first_date > last_date:
        alerts.append(
            _alert(
                "warn",
                "data",
                "行情未覆盖买入日",
                f"买入日 {first_date} 晚于最新行情 {last_date}，当前无法计算真实浮盈。",
            )
        )
    else:
        stale_days = 0
        try:
            stale_days = max(0, (date.today() - date.fromisoformat(last_date)).days)
        except ValueError:
            pass
        if stale_days > 5:
            alerts.append(
                _alert(
                    "info",
                    "stale",
                    "行情数据陈旧",
                    f"最新行情截至 {last_date}（{stale_days} 天前），止损/止盈提醒基于旧收盘价，请先刷新行情。",
                )
            )

    # 价格规则
    if last_price <= stop_price:
        if stop_kind == "移动止盈":
            alerts.append(
                _alert(
                    "danger",
                    "trail",
                    "移动止盈",
                    f"现价 {last_price:.4g} 已跌破移动止盈线 {stop_price:.4g}"
                    f"（峰值 {peak_price:.4g} 回撤超过 {trail_pct * 100:.0f}%），考虑减仓/离场。",
                )
            )
        elif stop_kind == "保本":
            alerts.append(
                _alert(
                    "danger",
                    "breakeven",
                    "保本离场",
                    f"现价 {last_price:.4g} 已回到成本 {avg_cost:.4g} 附近，此前最高浮盈 {_pct(peak_pnl)}，考虑保本减仓。",
                )
            )
        else:
            alerts.append(
                _alert(
                    "danger",
                    "stop",
                    "触发止损",
                    f"现价 {last_price:.4g} ≤ 止损线 {stop_price:.4g}（{stop_kind}，成本 {avg_cost:.4g}），"
                    f"浮亏 {_pct(pnl_pct)}，考虑减仓/清仓。",
                )
            )
    elif last_price >= take_profit_price:
        alerts.append(
            _alert(
                "warn",
                "take_profit",
                "达到止盈线",
                f"现价 {last_price:.4g} ≥ 止盈线 {take_profit_price:.4g}"
                f"（浮盈 {_pct(pnl_pct)}），考虑分批止盈并把止损上移。",
            )
        )

    # 时间止损：只有因子明确偏空/中性时才提示，避免把正常洗盘当失败
    if (
        holding_td >= time_days
        and pnl <= 0
        and sig_level in ("bearish", "neutral")
        and factor["status"] in ("ok", "stale")
    ):
        alerts.append(
            _alert(
                "warn",
                "time",
                "时间止损",
                f"已持有 {holding_td} 个交易日（≥{time_days}），浮盈 {_pct(pnl_pct)}，"
                "且监控因子未转多，考虑换仓。",
            )
        )

    # 因子规则
    if factor.get("error"):
        # 旧信号还在时仍要明确提示“这次刷新失败了”，不能让它看起来是最新结果
        alerts.append(
            _alert(
                "warn",
                "factor_error",
                "因子刷新失败",
                f"上次刷新失败：{factor.get('error')}；表中信号可能仍是旧结果。",
            )
        )
    if factor["status"] == "error":
        if not factor.get("error"):
            alerts.append(
                _alert("info", "factor_error", "因子检查失败", "因子计算失败，请重新刷新。"),
            )
    elif factor["status"] == "missing":
        alerts.append(
            _alert("info", "factor_missing", "因子未检查", "点上方『刷新因子提醒』后，才会给出因子转弱提醒。"),
        )
    elif factor["status"] == "unknown":
        alerts.append(
            _alert(
                "info",
                "factor_unknown",
                "因子样本不足",
                f"{sig.get('reason') or '样本不足 250 个交易日，无法计算滚动分位'}；暂不能给因子转弱提醒。",
            )
        )
    elif factor["status"] == "stale":
        alerts.append(
            _alert(
                "info",
                "factor_stale",
                "因子信号需刷新",
                f"因子信号日期 {sig.get('date')} 早于行情 {last_date}，请点『刷新因子提醒』。",
            )
        )
    elif sig_level == "bearish":
        conf = sig.get("confidence")
        if conf in ("strong", "medium"):
            alerts.append(
                _alert(
                    "warn",
                    "factor",
                    "因子转弱",
                    f"监控因子转为偏空（{ {'strong': '强', 'medium': '中等'}.get(conf, conf) }置信，"
                    f"分位 {(sig.get('pct') or 0) * 100:.1f}%），考虑减仓/收紧止损。",
                )
            )
        else:
            alerts.append(
                _alert(
                    "info",
                    "factor_weak",
                    "因子偏空（弱）",
                    f"监控因子偏空但置信度弱（分位 {(sig.get('pct') or 0) * 100:.1f}%），先观察，不单独作为减仓依据。",
                )
            )

    alerts.sort(key=lambda a: (ALERT_RANK.get(a["level"], 9), a["type"]))
    primary = alerts[0] if alerts else _alert("ok", "none", "正常", "未触发止损/止盈/时间/因子转弱规则。")

    return {
        **pos,
        "name": q["name"],
        "market_label": q["market_label"],
        "currency": q["currency"],
        "qty": round(qty, 6),
        "cost": round(cost, 2),
        "avg_cost": round(avg_cost, 4),
        "last_price": last_price,
        "last_date": last_date,
        "market_value": round(market_value, 2),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 6),
        "peak_price": round(peak_price, 4),
        "peak_pnl": round(peak_pnl, 6),
        "trough_pnl": round(trough_pnl, 6),
        "holding_td": holding_td,
        "holding_days": holding_days,
        "first_date": first_date,
        "last_lot_date": last_lot.get("date"),
        "atr14": atr,
        "ma20": ind.get("ma20"),
        "ma60": ind.get("ma60"),
        "stop_price": round(stop_price, 4),
        "stop_kind": stop_kind,
        "take_profit_price": round(take_profit_price, 4),
        "trail_price": None if trail_price is None else round(trail_price, 4),
        "factor": factor,
        "alerts": alerts,
        "primary_alert": primary,
        "status": primary["level"],
    }


def _summary(positions: list[dict]) -> dict:
    by_cur: dict[str, dict] = {}
    danger = warn = 0
    for p in positions:
        cur = p.get("currency") or ""
        b = by_cur.setdefault(cur, {"count": 0, "cost": 0.0, "value": 0.0, "pnl": 0.0})
        b["count"] += 1
        b["cost"] += float(p.get("cost") or 0)
        b["value"] += float(p.get("market_value") or 0)
        b["pnl"] += float(p.get("pnl") or 0)
        if p.get("status") == "danger":
            danger += 1
        elif p.get("status") == "warn":
            warn += 1
    for b in by_cur.values():
        b["cost"] = round(b["cost"], 2)
        b["value"] = round(b["value"], 2)
        b["pnl"] = round(b["pnl"], 2)
        b["pnl_pct"] = round(b["pnl"] / b["cost"], 6) if b["cost"] else None
    return {"count": len(positions), "by_currency": by_cur, "danger": danger, "warn": warn}


def list_positions() -> dict:
    doc = _load()
    out = []
    for raw in doc["positions"]:
        try:
            out.append(_compute_position(raw))
        except Exception as exc:  # noqa: BLE001  单只失败不影响其他持仓
            out.append(
                {
                    **raw,
                    "name": raw.get("name") or raw.get("symbol"),
                    "currency": view.CURRENCY.get(raw.get("market"), ""),
                    "status": "warn",
                    "alerts": [_alert("warn", "calc", "计算失败", str(exc))],
                    "primary_alert": _alert("warn", "calc", "计算失败", str(exc)),
                    "factor": {"expr": raw.get("expr"), "status": "missing", "signal": None, "error": None},
                }
            )
    flat = []
    for p in out:
        for a in p.get("alerts") or []:
            if a.get("level") in ("danger", "warn"):
                flat.append(
                    {
                        **a,
                        "position_id": p.get("id"),
                        "name": p.get("name"),
                        "symbol": p.get("symbol"),
                    }
                )
    flat.sort(key=lambda a: (ALERT_RANK.get(a.get("level"), 9), a.get("type")))
    return {
        "positions": out,
        "summary": _summary(out),
        "alerts": flat,
        "file": str(POS_FILE),
        "asof": max((p.get("last_date") or "" for p in out), default=None),
    }


# ---------------------------------------------------------------- 导出
def export_json() -> str:
    doc = _load()
    return json.dumps(doc, ensure_ascii=False, indent=2)


def export_csv() -> str:
    doc = _load()
    buf = io.StringIO()
    buf.write("\ufeff")  # Excel 识别 UTF-8
    cols = [
        "market", "symbol", "name", "buy_date", "price", "qty", "fee", "note",
        "expr", "stop_pct", "take_profit_pct", "trail_pct", "time_stop_days",
        "break_even_trigger", "atr_mult",
    ]
    wr = csv.DictWriter(buf, fieldnames=cols)
    wr.writeheader()
    for p in doc["positions"]:
        for lot in p.get("lots") or []:
            wr.writerow(
                {
                    "market": p.get("market"),
                    "symbol": p.get("symbol"),
                    "name": p.get("name"),
                    "buy_date": lot.get("date"),
                    "price": lot.get("price"),
                    "qty": lot.get("qty"),
                    "fee": lot.get("fee"),
                    "note": lot.get("note"),
                    "expr": p.get("expr"),
                    "stop_pct": p.get("stop_pct"),
                    "take_profit_pct": p.get("take_profit_pct"),
                    "trail_pct": p.get("trail_pct"),
                    "time_stop_days": p.get("time_stop_days"),
                    "break_even_trigger": p.get("break_even_trigger"),
                    "atr_mult": p.get("atr_mult"),
                }
            )
    return buf.getvalue()

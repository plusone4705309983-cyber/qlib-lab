# 股东穿透 / 持仓反查 (东方财富数据中心, 仅 A 股)
#
# 功能:
#   1. top_holders(market, qlib): 某只股票的「前十大股东 ∪ 前十大流通股东」
#      (官方只披露前 10, 两者合并去重后通常 11~20 个主体)。
#   2. holder_stocks(name):   按股东名反查其出现在哪些股票的十大(流通)股东里,
#      并给出最新报告期持股 + 增持/减持方向。
#
# 数据源为东财公开数据中心接口 (reportName), 与 scripts/east.py 同源; 季度披露,
# 变化慢, 因此做 6 小时内存缓存。接口为非官方, 可能变动。
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

EM_BASE = "https://datacenter-web.eastmoney.com/api/data/v1/get"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 通道 / 国家队主体: 出现在几乎每只股票里, 反查噪声大, 前端单列提示
SPECIAL_HOLDERS = {
    "香港中央结算有限公司",
    "中央汇金资产管理有限责任公司",
    "中央汇金投资有限责任公司",
    "中国证券金融股份有限公司",
}

CACHE_TTL = 6 * 3600  # 秒; 季度数据
_cache: dict = {}


class NotSupported(Exception):
    """该市场/标的暂不支持股东穿透。"""


def _num(x):
    try:
        v = float(x)
    except Exception:
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


def _view():
    import view
    return view


def _get(report, filt, sort_col="", page_size=20, page=1):
    key = (report, filt, sort_col, page_size, page)
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    q = {
        "reportName": report,
        "columns": "ALL",
        "filter": filt,
        "pageNumber": page,
        "pageSize": page_size,
        "source": "WEB",
        "client": "WEB",
    }
    if sort_col:
        q["sortColumns"] = sort_col
        q["sortTypes"] = -1
    url = EM_BASE + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Referer": "https://data.eastmoney.com/"}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    _cache[key] = (now, data)
    return data


def _rows(report, filt, sort_col="", page_size=20, page=1):
    data = _get(report, filt, sort_col, page_size, page)
    if not isinstance(data, dict):
        return []
    if data.get("success") is False:
        return []
    return (data.get("result") or {}).get("data") or []


def _code(market, qlib_sym):
    """取 6 位证券代码: 优先用配置里的 secid, 否则从 sh/sz/bj 前缀解析。"""
    view = _view()
    meta = view.find(market, qlib_sym)
    if meta and str(meta.get("secid", "")).count(".") == 1:
        return str(meta["secid"]).split(".")[1]
    q = qlib_sym.lower()
    for p in ("sh", "sz", "bj"):
        if q.startswith(p) and q[len(p):].isdigit():
            return q[len(p):]
    return q


def _direction(r):
    """增持 / 减持 / 新进 / 不变 ..."""
    for k in ("DIRECTION", "HOLD_NUM_CHANGE", "HOLD_CHANGE", "HOLDNUM_CHANGE_NAME", "NEW_CHANGE_RATIO"):
        v = r.get(k)
        if not isinstance(v, str):
            continue
        s = v.strip()
        if not s or any(ch.isdigit() for ch in s):
            continue  # 含数字的是变动数量/比例, 不是方向
        return s
    return None


def _change_num(r):
    """持股变动数量 (股), 取不到返回 None。"""
    for k in ("HOLD_NUM_CHANGE", "HOLD_CHANGE", "HOLD_CHANGE_ABBR"):
        v = r.get(k)
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str):
            s = v.strip().replace(",", "")
            if s and (s[0] in "+-" or s.isdigit()):
                try:
                    return int(float(s))
                except ValueError:
                    pass
    return None


def _qlib(code):
    c = str(code or "")
    if not c.isdigit():
        return c.lower()
    if c[0] in ("6", "9"):
        return "sh" + c
    if c[0] in ("4", "8"):
        return "bj" + c
    return "sz" + c


def _secid(code):
    q = _qlib(code)
    pre = {"sh": "1", "sz": "0", "bj": "0"}.get(q[:2], "0")
    return f"{pre}.{code}"


def _latest(rows):
    end = max((r.get("END_DATE") or "" for r in rows), default="")
    return end, [r for r in rows if (r.get("END_DATE") or "") == end]


def _holder(r, in_free):
    is_org = str(r.get("IS_HOLDORG") or "") == "1"
    htype = r.get("HOLDER_TYPE") or r.get("HOLDER_NEWTYPE") or ("机构" if is_org else "自然人")
    name = r.get("HOLDER_NAME")
    ratio = r.get("HOLD_NUM_RATIO")
    if ratio is None:
        ratio = r.get("FREE_HOLDNUM_RATIO")
    direction = _direction(r)
    change = _change_num(r)
    if direction is None and change is not None:
        direction = "增持" if change > 0 else ("减持" if change < 0 else "不变")
    return {
        "name": name,
        "rank": r.get("HOLDER_RANK"),
        "hold_num": r.get("HOLD_NUM"),
        "hold_ratio": _num(ratio),
        "free_ratio": _num(r.get("FREE_HOLDNUM_RATIO")),
        "type": htype,
        "is_org": is_org,
        "direction": direction,
        "change": change,
        "in_free": in_free,
        "special": name in SPECIAL_HOLDERS,
    }


def top_holders(market, qlib_sym):
    """返回 {market, symbol, code, end_date, holders:[...], count}。"""
    if market != "cn":
        raise NotSupported("股东穿透暂仅支持 A 股")
    view = _view()
    meta = view.find(market, qlib_sym)
    if meta is None:
        raise KeyError(f"未配置的标的: {market}/{qlib_sym}")
    code = _code(market, qlib_sym)

    top = _rows("RPT_F10_EH_HOLDERS", f'(SECURITY_CODE="{code}")', "END_DATE", 40)
    free = _rows("RPT_F10_EH_FREEHOLDERS", f'(SECURITY_CODE="{code}")', "END_DATE", 40)
    if not top and not free:
        raise NotSupported("未获取到股东数据 (接口可能已变动)")

    end_top, top = _latest(top)
    _, free = _latest(free)
    end = end_top or (free[0].get("END_DATE") if free else "")

    free_names = {r.get("HOLDER_NAME") for r in free}
    holders, seen = [], set()
    for r in top:  # 十大股东优先
        nm = r.get("HOLDER_NAME")
        if not nm or nm in seen:
            continue
        seen.add(nm)
        holders.append(_holder(r, nm in free_names))
    for r in free:  # 补充仅在十大流通股东里出现的主体
        nm = r.get("HOLDER_NAME")
        if not nm or nm in seen:
            continue
        seen.add(nm)
        holders.append(_holder(r, True))

    holders.sort(key=lambda x: (x["rank"] or 99))
    return {
        "market": market,
        "symbol": qlib_sym.lower(),
        "code": code,
        "name": meta.get("name", qlib_sym),
        "end_date": (end or "")[:10],
        "holders": holders,
        "count": len(holders),
    }


def holder_stocks(name, page_size=50, max_pages=10):
    """按股东名反查其持仓股票 (取每只股票的最新报告期)。

    注: 东财数据中心 pageSize 上限约 50, 故分页抓取。
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("股东名不能为空")
    filt = '(HOLDER_NAME="%s")' % name.replace('"', "")
    rows = []
    for p in range(1, max_pages + 1):
        part = _rows("RPT_DMSK_HOLDERS", filt, "END_DATE", page_size, p)
        rows.extend(part)
        if len(part) < page_size:
            break
    if not rows:
        return {"name": name, "count": 0, "stocks": [], "special": name in SPECIAL_HOLDERS}

    latest = {}
    for r in rows:
        code = r.get("SECURITY_CODE")
        if not code:
            continue
        cur = latest.get(code)
        if cur is None or (r.get("END_DATE") or "") > (cur.get("END_DATE") or ""):
            latest[code] = r

    stocks = []
    for r in latest.values():
        code = r.get("SECURITY_CODE")
        direction = _direction(r)
        change = _change_num(r)
        if direction is None and change is not None:
            direction = "增持" if change > 0 else ("减持" if change < 0 else "不变")
        stocks.append(
            {
                "code": code,
                "symbol": _qlib(code),
                "secid": _secid(code),
                "name": r.get("SECURITY_NAME_ABBR") or r.get("SECURITY_NAME") or code,
                "end_date": (r.get("END_DATE") or "")[:10],
                "hold_num": r.get("HOLD_NUM"),
                "hold_ratio": _num(r.get("HOLD_RATIO")),
                "direction": direction,
                "change": change,
            }
        )
    stocks.sort(key=lambda x: (x["hold_num"] or 0), reverse=True)
    return {
        "name": name,
        "count": len(stocks),
        "stocks": stocks,
        "special": name in SPECIAL_HOLDERS,
        "truncated": len(rows) >= page_size * max_pages,
    }


if __name__ == "__main__":
    print("== 前十大股东 ∪ 前十大流通股东: sh600519 ==")
    d = top_holders("cn", "sh600519")
    print(f"报告期 {d['end_date']}  共 {d['count']} 个主体")
    for h in d["holders"]:
        print(
            f"  #{h['rank']:<2} {h['name']}  持{h['hold_num']}  "
            f"{h['hold_ratio']}%  {h['direction']}"
            + ("  [特殊]" if h["special"] else "")
        )
    print()
    name = "中国银行股份有限公司-招商中证白酒指数分级证券投资基金"
    print(f"== 反查持仓: {name} ==")
    r = holder_stocks(name)
    print(f"共 {r['count']} 只")
    for s in r["stocks"][:10]:
        print(f"  {s['symbol']} {s['name']}  {s['end_date']}  持{s['hold_num']}  {s['direction']}")

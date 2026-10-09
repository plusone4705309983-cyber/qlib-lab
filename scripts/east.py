"""东方财富直调取数层：DoH 解析绕过 DNS 污染，输出 qlib dump_bin 兼容 CSV。"""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml

KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
FIELDS1 = "f1,f2,f3"
FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58"
KLT = "101"
UT = "fa5fd1943c7b386f172d6893dbfba10b"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
HDR = {
    "User-Agent": UA,
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json, text/plain, */*",
}

DOH_PROVIDERS = (
    "https://dns.alidns.com/resolve?name={host}&type=A",
    "https://223.5.5.5/resolve?name={host}&type=A",
    "https://1.1.1.1/dns-query?name={host}&type=A",
)

_cache: dict[str, tuple[list[str], float]] = {}
_pinned: dict[str, str] = {}
_last_use: dict[str, float] = {}
PREFER_IPV4 = True
_orig_gai = socket.getaddrinfo

_session = requests.Session()
_session.trust_env = False


def _is_ip(text: str) -> bool:
    parts = text.replace(":", ".").split(".")
    if len(parts) not in (4, 8):
        return False
    return all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def _doh_query(url: str, timeout: float) -> list[str]:
    resp = _session.get(
        url, timeout=timeout, headers={"accept": "application/dns-json", "User-Agent": UA}
    )
    resp.raise_for_status()
    out = []
    for ans in resp.json().get("Answer") or []:
        if str(ans.get("type")) in ("1", "A") and ans.get("data"):
            out.append(str(ans["data"]).strip())
    return out


def _system_ips(host: str) -> list[str]:
    out = []
    for fam in (socket.AF_INET, socket.AF_INET6):
        try:
            infos = _orig_gai(host, None, fam)
        except OSError:
            continue
        for info in infos:
            ip = info[4][0]
            if fam is socket.AF_INET or not PREFER_IPV4:
                if ip not in out:
                    out.append(ip)
    return out


def resolve(host: str, ttl: float = 600.0) -> list[str]:
    host = host.lower()
    hit = _cache.get(host)
    fresh = hit is None or time.time() - hit[1] >= ttl
    doh: list[str] = []
    if fresh:
        for provider in DOH_PROVIDERS:
            for attempt in range(2):
                try:
                    doh = _doh_query(provider.format(host=host), timeout=6)
                    if doh:
                        break
                except Exception:
                    time.sleep(0.4 * (attempt + 1))
            if doh:
                break
        _cache[host] = (doh, time.time())
    else:
        doh = hit[0]

    merged: list[str] = []
    for ip in _system_ips(host) + list(doh):
        if ip not in merged:
            merged.append(ip)
    return merged


def _patched_gai(host, port, family=0, type=0, proto=0, flags=0):
    if isinstance(host, str) and not _is_ip(host):
        key = host.lower()
        pin = _pinned.get(key)
        if pin:
            try:
                return _orig_gai(pin, port, family, type, proto, flags)
            except OSError:
                _pinned.pop(key, None)
        want = socket.AF_INET if PREFER_IPV4 and family == 0 else family
        for ip in resolve(key):
            try:
                return _orig_gai(ip, port, want, type, proto, flags)
            except OSError:
                if want != family:
                    try:
                        return _orig_gai(ip, port, family, type, proto, flags)
                    except OSError:
                        pass
                continue
    return _orig_gai(host, port, family, type, proto, flags)


def install_resolver() -> None:
    if not getattr(socket.getaddrinfo, "_qlab_patched", False):
        socket.getaddrinfo = _patched_gai
        socket.getaddrinfo._qlab_patched = True


def collect_ips(host: str, samples: int = 6) -> list[str]:
    """Multiple DoH queries to expand the candidate pool; DoH caches itself, so repeated sampling is needed to get a diverse set of IPs."""
    host = host.lower()
    pool = list(_system_ips(host))
    for _ in range(samples):
        got = resolve(host, ttl=0.0)
        fresh = [ip for ip in got if ip not in pool]
        pool.extend(fresh)
        if not fresh:
            time.sleep(1.0)
    return pool


def _take_ip(host: str, pool: list[str], min_gap: float) -> str | None:
    if not pool:
        return None
    now = time.time()
    ip = min(pool, key=lambda x: _last_use.get(x, 0.0))
    wait = min_gap - (now - _last_use.get(ip, 0.0))
    if wait > 0:
        time.sleep(wait)
    _last_use[ip] = time.time()
    return ip


def fetch_klines(secid: str, fqt: int, beg: str, end: str,
                 tries: int = 8, min_gap: float = 2.0) -> dict:
    host = urlparse(KLINE_URL).hostname or ""
    params = {
        "secid": secid, "ut": UT, "fields1": FIELDS1, "fields2": FIELDS2,
        "klt": KLT, "fqt": fqt, "beg": beg, "end": end, "lmt": 1000000,
    }
    pool = collect_ips(host, samples=6)
    last = "unknown"
    for t in range(tries):
        ip = _take_ip(host, pool, min_gap)
        if ip is None:
            _pinned.pop(host, None)
        else:
            _pinned[host] = ip
        try:
            resp = _session.get(KLINE_URL, params=params, timeout=12, headers=HDR)
            if resp.status_code == 200:
                data = resp.json().get("data")
                if data and data.get("klines"):
                    print(f"      [{secid} fqt={fqt}] via {ip} -> {len(data['klines'])} 行")
                    return data
                last = f"http=200 data={'None' if data is None else 'no-klines'}"
            else:
                last = f"{ip}: http={resp.status_code}"
        except Exception as exc:
            last = f"{ip}: {exc.__class__.__name__}"
        pool = [x for x in pool if x != ip] or collect_ips(host, samples=3)
        time.sleep(min(3.0 * (t + 1), 20.0))
    raise RuntimeError(f"fetch failed secid={secid} fqt={fqt}: {last}")


def _cache_path(key: str) -> Path:
    d = Path(__file__).resolve().parent.parent / "data" / "_raw_cache"
    d.mkdir(parents=True, exist_ok=True)
    h = hashlib.md5(key.encode()).hexdigest()[:16]
    return d / f"{h}.json"


USE_CACHE = True


def fetch_cached(secid: str, fqt: int, beg: str, end: str, min_gap: float = 2.5) -> dict:
    key = f"{secid}|{fqt}|{beg}|{end}|{KLT}"
    path = _cache_path(key)
    if USE_CACHE and path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    data = fetch_klines(secid, fqt, beg, end, min_gap=min_gap)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def _rows(data: dict) -> list[tuple[str, float, float, float, float, float]]:
    parsed = []
    for line in data["klines"]:
        p = line.split(",")
        parsed.append((p[0], float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5])))
    return parsed


MAX_FACTOR_DROP = 0.01


def _drop_factor_drops(rows: list[list], name: str = "") -> list[list]:
    """剔除 factor 异常下调的交易日 (后复权 factor 只在除息日上调, 永不下降)。

    实测下调行均为数据错误:
      - 腾讯 hfq 源对"最新交易日"返回未完成复权的行
        (sz300750 末行 factor 恰为 1.0 = 完全未复权; 四只 A 股末行 -9%~-48%);
      - 历史个别交易日同样出错 (如 sh601318 2023-04-27 涨停日被记成 +7.61%)。
    阈值 1% 来自实测: 正常日 factor 变动中位数 0.0000%、99 分位 0.60%,
    实测剔除量 sh600519=5 / sh601318=28 / sz000001=17 / sz300750=0 天。
    """
    kept: list[list] = []
    dropped: list[tuple] = []
    prev = None
    for r in rows:
        f = r[6]
        if prev is not None and f < prev * (1.0 - MAX_FACTOR_DROP):
            dropped.append((r[0], prev, f))
        else:
            kept.append(r)
        # 无论剔除与否都前进: 否则一个下调日会让其后所有偏低行被连锁剔除
        # (实测连锁会误删 69/421/678 天)。
        prev = f
    if dropped:
        head = ", ".join(f"{d} {p:.4f}->{f:.4f}" for d, p, f in dropped[:3])
        tail = "" if len(dropped) <= 3 else f" 等共 {len(dropped)} 天"
        print(f"WARN {name:<10} 剔除 factor 下调 {len(dropped)} 天: {head}{tail}")
    return kept


def build_csv(secid: str, beg: str, end: str, vol_mult: float, min_gap: float = 2.5) -> list[list]:
    raw = {r[0]: r for r in _rows(fetch_cached(secid, 0, beg, end, min_gap))}
    adj = {r[0]: r for r in _rows(fetch_cached(secid, 2, beg, end, min_gap))}

    out = []
    for day in sorted(raw.keys() & adj.keys()):
        _, adj_o, adj_c, adj_h, adj_l, _ = adj[day]
        c = raw[day][2]
        factor = 1.0 if c == 0 else adj_c / c
        # fqt=2 是后复权, OHLC 全部取该口径, 与 $close 保持一致 (Qlib 字段契约)。
        # 成交量取 raw (复权不影响成交量), 手 -> 股。
        out.append([day, adj_o, adj_h, adj_l, adj_c,
                    raw[day][5] * vol_mult, factor])
    return _drop_factor_drops(out, secid)


def load_config(root: Path) -> dict:
    with (root / "config" / "symbols.yaml").open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


TENCENT_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TEN_HDR = {"User-Agent": UA, "Referer": "https://gu.qq.com/"}
TEN_PAGE = 640


def _tencent_page(code: str, end: str, fq: str) -> list[list]:
    url = f"{TENCENT_URL}?param={code},day,,{end},{TEN_PAGE},{fq}"
    for t in range(4):
        try:
            node = (_session.get(url, timeout=15, headers=TEN_HDR).json().get("data") or {}).get(code) or {}
            for key in ("qfqday", "hfqday", "day"):
                if node.get(key):
                    return node[key]
            return []
        except Exception:
            time.sleep(1.0 * (t + 1))
    return []


def fetch_tencent(code: str, fq: str, beg: str, end: str = "", max_pages: int = 40) -> dict:
    """抓腾讯日线。end 格式 YYYYMMDD; 缺省或晚于今天 = 取到最新, 否则截到该日。

    分页游标 (cursor) 用腾讯要求的 YYYY-MM-DD; 首次请求由 end 换算而来,
    之后每页用本页最早日往前推一天 (腾讯 param 的第 4 段 = 截止日)。
    """
    beg_d = datetime.strptime(beg, "%Y%m%d").date()
    today = date.today()
    if end:
        try:
            end_d = min(datetime.strptime(end, "%Y%m%d").date(), today)
        except ValueError:
            raise ValueError(f"--end 需为 YYYYMMDD, 收到 {end!r}")
    else:
        end_d = today
    # 与旧行为逐字节一致: end 缺省/未来时游标留空 (= 腾讯"最新"), 不显式传日期
    cursor = "" if end_d >= today else end_d.strftime("%Y-%m-%d")
    rows: dict[str, list] = {}
    for _ in range(max_pages):
        page = _tencent_page(code, cursor, fq)
        if not page:
            break
        fresh = [p for p in page if p[0] not in rows]
        for p in fresh:
            rows[p[0]] = p
        earliest = min(p[0] for p in page)
        stop = datetime.strptime(earliest, "%Y-%m-%d").date()
        if stop <= beg_d or len(fresh) < TEN_PAGE:
            break
        cursor = (stop - timedelta(days=1)).strftime("%Y-%m-%d")
    keep = sorted(d for d in rows if beg_d <= datetime.strptime(d, "%Y-%m-%d").date() <= end_d)
    if not keep:
        raise RuntimeError(f"腾讯源无数据 code={code} fq={fq} end={end or 'latest'}")
    return {"klines": [",".join(str(x) for x in (rows[d][0], rows[d][1], rows[d][2], rows[d][3],
                                                 rows[d][4], float(rows[d][5]))) for d in keep]}


def _tencent_cached(code: str, fq: str, beg: str, end: str) -> dict:
    path = _cache_path(f"TEN|{code}|{fq}|{beg}|{end}")
    if USE_CACHE and path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    data = fetch_tencent(code, fq, beg, end)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def build_csv_tencent(item: dict, beg: str, end: str, vol_mult: float) -> list[list]:
    code = item["tencent"]
    raw = {r[0]: r for r in _rows(_tencent_cached(code, "", beg, end))}
    adj = {r[0]: r for r in _rows(_tencent_cached(code, "hfq", beg, end))}
    out = []
    for day in sorted(raw.keys() & adj.keys()):
        _, adj_o, adj_c, adj_h, adj_l, _ = adj[day]
        c = raw[day][2]
        factor = 1.0 if c == 0 else adj_c / c
        # OHLC 全部取后复权口径, 与 $close 保持一致 (Qlib 字段契约)。
        # 成交量用 raw 的手数换算, 复权不影响成交量。
        out.append([day, adj_o, adj_h, adj_l, adj_c,
                    raw[day][5] * vol_mult, factor])
    return _drop_factor_drops(out, code)


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    cfg = load_config(root)
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", required=True, choices=list(cfg["markets"]))
    ap.add_argument("--beg", default="19900101")
    ap.add_argument("--end", default="20500101")
    ap.add_argument("--vol-mult", type=float, default=None,
                    help="成交量换算倍数; CN=100(手->股), US=1")
    ap.add_argument("--throttle", type=float, default=4.0, help="每只股票之间的间隔秒数")
    ap.add_argument("--min-gap", type=float, default=2.5, help="同一 IP 两次请求的最小间隔秒数")
    ap.add_argument("--source", default="eastmoney", choices=["eastmoney", "tencent"])
    ap.add_argument("--no-cache", action="store_true",
                    help="忽略本地 _raw_cache, 强制重新拉取 (刷新到最新交易日用)")
    ap.add_argument("--symbol", action="append", default=None, metavar="QLIB_NAME",
                    help="只重建指定股票, 可重复: --symbol sh600519 --symbol sz000001; "
                         "缺省 = 重建整个 --market (会全量重跑该市场所有股票)")
    args = ap.parse_args()

    global USE_CACHE
    if args.no_cache:
        USE_CACHE = False

    install_resolver()
    market = cfg["markets"][args.market]
    vol_mult = args.vol_mult if args.vol_mult is not None else (100.0 if args.market == "cn" else 1.0)
    out_dir = root / market["csv_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)

    symbols = market["symbols"]
    if args.symbol:
        wanted = {s.strip().lower() for s in args.symbol}
        symbols = [it for it in symbols if it["qlib"].strip().lower() in wanted]
        missing = wanted - {it["qlib"].strip().lower() for it in symbols}
        if missing:
            raise SystemExit(
                f"--symbol 不在 config markets['{args.market}'].symbols 中: "
                f"{sorted(missing)}\n"
                f"  先在 config/symbols.yaml 的 {args.market}.symbols 下加一行, 再重跑"
            )
        print(f"--symbol 命中 {len(symbols)}/{len(market['symbols'])} 只")

    for idx, item in enumerate(symbols):
        try:
            if args.source == "tencent":
                rows = build_csv_tencent(item, args.beg, args.end, vol_mult)
            else:
                rows = build_csv(item["secid"], args.beg, args.end, vol_mult, args.min_gap)
            path = out_dir / f"{item['qlib']}.csv"
            with path.open("w", encoding="utf-8", newline="\n") as fh:
                fh.write("date,symbol,open,high,low,close,volume,factor\n")
                for day, o, h, l, c, v, f in rows:
                    fh.write(f"{day},{item['qlib']},{o:.4f},{h:.4f},{l:.4f},{c:.4f},{v:.0f},{f:.6f}\n")
            print(f"OK   {item['name']:<8} {item['secid']:<12} {len(rows):>5} 行  {rows[0][0]}~{rows[-1][0]}  -> {path.name}")
        except Exception as exc:
            print(f"FAIL {item['name']:<8} {item['secid']:<12} {exc}")
        if idx < len(symbols) - 1:      # 最后一只不再白等 throttle 秒
            time.sleep(args.throttle)


if __name__ == "__main__":
    main()

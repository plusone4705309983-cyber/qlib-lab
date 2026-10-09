"""添加股票 (写操作): 搜索候选 -> 改 config/symbols.yaml -> 抓数 -> 重建 .bin。

设计:
  * 搜索走东方财富 suggest 接口, 直接拿到 secid / 市场 / 名称, 免去手工填 secid。
  * 腾讯美股的交易所后缀 (.OQ 纳斯达克 / .N 纽交所) 由 MktNum 推导。
  * 抓数后校验 CSV 行数; 腾讯源若异常 (美股代码后缀错会只返回 1 行) 自动改用东财源。
  * 任一步失败 -> 回滚 config/symbols.yaml (先备份原文)。
  * 全程后台线程执行, 前端轮询 /api/job/<id> 看日志。
"""

from __future__ import annotations

import io
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

CONFIG_PATH = ROOT / "config" / "symbols.yaml"
LOGS_DIR = ROOT / "web" / "logs"

SEARCH_URL = (
    "https://searchapi.eastmoney.com/api/suggest/get"
    "?input={q}&type=14&token=D43BF722C8E33BDC906FB84D85E326E8&count=10"
)
US_SUFFIX = {105: ".OQ", 106: ".N", 107: ".A"}
CN_PREFIX = {1: "sh", 0: "sz"}

RE_QLIB = re.compile(r"^[a-z][a-z0-9.]{0,19}$")
RE_CODE = re.compile(r"^[A-Za-z0-9.]{1,10}$")
RE_SECID = re.compile(r"^\d{1,3}\.[A-Za-z0-9.]{1,12}$")
RE_TEN = re.compile(r"^[a-z]{2}[A-Za-z0-9.]{1,12}$")

_jobs: dict[str, dict] = {}
_active = {"running": False}
_lock = threading.Lock()
_net_ready = False


# ---------------------------------------------------------------- 网络
def _ensure_net() -> None:
    global _net_ready
    if _net_ready:
        return
    import east

    east.install_resolver()
    _net_ready = True


def _session():
    import east

    return east._session, east.UA


# ---------------------------------------------------------------- 搜索
def derive_tencent(mktnum, code: str) -> str | None:
    try:
        mktnum = int(mktnum)
    except (TypeError, ValueError):
        return None
    if mktnum in CN_PREFIX:
        return CN_PREFIX[mktnum] + code
    if mktnum in US_SUFFIX:
        return "us" + code + US_SUFFIX[mktnum]
    return None


def market_of(classify: str) -> str | None:
    return {"AStock": "cn", "UsStock": "us"}.get(classify)


def search(q: str) -> list[dict]:
    q = (q or "").strip()
    if not q:
        return []
    _ensure_net()
    from urllib.parse import quote

    sess, ua = _session()
    resp = sess.get(SEARCH_URL.format(q=quote(q)), timeout=15,
                    headers={"User-Agent": ua, "Referer": "https://www.eastmoney.com/"})
    rows = (resp.json().get("QuotationCodeTable") or {}).get("Data") or []
    out = []
    for d in rows:
        market = market_of(d.get("Classify", ""))
        ten = derive_tencent(d.get("MktNum"), d.get("Code", ""))
        if not market or not ten:
            continue
        out.append(
            {
                "code": d.get("Code"),
                "name": d.get("Name"),
                "secid": d.get("QuoteID"),
                "market": market,
                "market_label": "A股" if market == "cn" else "美股",
                "type": d.get("SecurityTypeName", ""),
                "tencent": ten,
                "qlib": ten.lower() if market == "cn" else d.get("Code", "").lower(),
            }
        )
    return out


# ---------------------------------------------------------------- 校验
def validate(p: dict) -> dict:
    market = (p.get("market") or "").strip()
    if market not in ("cn", "us"):
        raise ValueError("市场必须是 cn 或 us")
    code = (p.get("code") or "").strip()
    name = (p.get("name") or "").strip()
    secid = (p.get("secid") or "").strip()
    tencent = (p.get("tencent") or "").strip()
    qlib = (p.get("qlib") or "").strip().lower()

    if not RE_CODE.match(code):
        raise ValueError("代码格式不对")
    if not (1 <= len(name) <= 30) or any(c in name for c in "\r\n\t"):
        raise ValueError("名称必须是 1~30 个字符且不含换行")
    if not RE_SECID.match(secid):
        raise ValueError("secid 格式不对 (形如 1.600519 / 105.AAPL)")
    if not RE_TEN.match(tencent):
        raise ValueError("tencent 代码格式不对 (形如 sh600519 / usAAPL.OQ)")
    if not RE_QLIB.match(qlib):
        raise ValueError("qlib 名必须以小写字母开头, 只含小写字母/数字/点")
    if market == "cn" and not secid.startswith(("0.", "1.")):
        raise ValueError("A股 secid 前缀应为 0. 或 1.")
    if market == "us" and not secid.startswith(("105.", "106.", "107.")):
        raise ValueError("美股 secid 前缀应为 105./106./107.")
    return {"market": market, "code": code, "name": name, "secid": secid,
            "tencent": tencent, "qlib": qlib}


# ---------------------------------------------------------------- 配置文件
def _load_config_text() -> str:
    return CONFIG_PATH.read_text(encoding="utf-8")


def _append_symbol(market: str, qlib: str, secid: str, tencent: str, name: str,
                   text: str) -> None:
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap

    y = YAML()
    y.preserve_quotes = True
    doc = y.load(text)
    syms = doc["markets"][market]["symbols"]
    for s in syms:
        if str(s.get("qlib", "")).lower() == qlib or str(s.get("secid", "")) == secid:
            raise ValueError(f"已存在: {s.get('name')} ({s.get('qlib')})")
    m = CommentedMap([("qlib", qlib), ("secid", secid), ("tencent", tencent), ("name", name)])
    m.fa.set_flow_style()
    syms.append(m)
    buf = io.StringIO()
    y.dump(doc, buf)
    CONFIG_PATH.write_text(buf.getvalue(), encoding="utf-8", newline="\n")


def _market_dir(market: str, key: str) -> Path:
    from ruamel.yaml import YAML

    doc = YAML().load(_load_config_text())
    return ROOT / doc["markets"][market][key]


# ---------------------------------------------------------------- 子进程
def _run(cmd: list[str], log, timeout: int = 600) -> tuple[int, str]:
    log("$ " + " ".join(str(c) for c in cmd[1:]))
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          timeout=timeout, cwd=str(ROOT))
    out = (proc.stdout or "") + (proc.stderr or "")
    for line in out.splitlines():
        if line.strip():
            log("  " + line.strip())
    return proc.returncode, out


def _count_csv(market: str, qlib: str) -> int:
    path = _market_dir(market, "csv_dir") / f"{qlib}.csv"
    if not path.exists():
        return 0
    with path.open(encoding="utf-8-sig") as fh:
        return max(0, sum(1 for _ in fh) - 1)


def _fetch(market: str, qlib: str, log, source: str = "tencent") -> int:
    cmd = [sys.executable, str(ROOT / "scripts" / "east.py"),
           "--market", market, "--source", source, "--symbol", qlib, "--throttle", "0"]
    _run(cmd, log)
    return _count_csv(market, qlib)


def _dump(market: str, log) -> None:
    cmd = [sys.executable, str(ROOT / "scripts" / "dump_bin.py"), "dump_all",
           "--data_path", str(_market_dir(market, "csv_dir")),
           "--qlib_dir", str(_market_dir(market, "bin_dir")),
           "--freq", "day", "--exclude_fields", "date,symbol",
           "--file_suffix", ".csv", "--max_workers", "4"]
    rc, _ = _run(cmd, log)
    if rc != 0:
        raise RuntimeError("dump_bin 重建失败")


# ---------------------------------------------------------------- 任务
def _run_job(job_id: str, p: dict) -> None:
    job = _jobs[job_id]
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logpath = LOGS_DIR / f"add_stock-{job_id.split('-', 1)[-1]}.log"
    fh = logpath.open("w", encoding="utf-8")

    def log(msg):
        s = str(msg)
        job["log"].append(s)
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {s}\n")
        fh.flush()

    try:
        market, qlib = p["market"], p["qlib"]
        log(f"目标: [{market}] {p['name']} ({p['code']})  secid={p['secid']}  tencent={p['tencent']}  qlib={qlib}")

        backup = _load_config_text()
        _append_symbol(market, qlib, p["secid"], p["tencent"], p["name"], backup)
        log("已写入 config/symbols.yaml")
        try:
            log("抓取行情 (腾讯源)…")
            rows = _fetch(market, qlib, log, "tencent")
            if rows < 60:
                log(f"腾讯源仅 {rows} 行, 改用东财源重试…")
                rows = _fetch(market, qlib, log, "eastmoney")
            if rows < 60:
                raise RuntimeError(f"抓到的数据过少 ({rows} 行), 请核对代码")
            log(f"抓取完成: {rows} 行")
            log("重建 Qlib provider (dump_bin)…")
            _dump(market, log)
            import view

            view.reload()
            log("已刷新服务端配置缓存")
        except Exception:
            log("失败 -> 回滚 config/symbols.yaml")
            CONFIG_PATH.write_text(backup, encoding="utf-8", newline="\n")
            raise

        job["result"] = {"market": market, "qlib": qlib, "name": p["name"], "rows": rows}
        job["status"] = "ok"
        log("完成 ✓  (刷新页面即可看到新股票)")
    except Exception as exc:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = str(exc)
        log(f"错误: {exc}")
    finally:
        log(f"日志已保存: {logpath}")
        fh.close()
        job["done"] = True
        with _lock:
            _active["running"] = False


def start_add(payload: dict) -> dict:
    p = validate(payload)
    with _lock:
        if _active["running"]:
            raise RuntimeError("已有添加任务正在进行, 请稍候")
        _active["running"] = True
        job_id = f"add-{int(time.time() * 1000)}"
        _jobs[job_id] = {"status": "running", "log": [], "done": False,
                         "error": None, "result": None}
    threading.Thread(target=_run_job, args=(job_id, p), daemon=True).start()
    return {"job_id": job_id}


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def busy() -> bool:
    return _active["running"]

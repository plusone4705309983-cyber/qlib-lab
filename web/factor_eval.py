"""因子实验室 —— qlib 表达式求值 (子进程 / 常驻 worker)。

两种用法:

    # 1) 一次性 CLI (调试用)
    python web/factor_eval.py cn sh600519 "Mean($close,5)/Mean($close,20)"

    # 2) 常驻 worker (web 服务用, 由 factor_runtime.py 拉起)
    python -u web/factor_eval.py --worker cn
    # stdin 每行一个 JSON: {"id":1,"symbol":"sh600519","expr":"..."}
    # stdout 每行一个 JSON: {"id":1,"ok":true,"data":{...}}

常驻模式下 qlib 只 init 一次, 之后每个新表达式约 1 秒, 而不是每次 20~60 秒。
qlib 仍在独立进程里, 崩溃/超时不会拖垮 Flask 主进程。

安全: qlib 内部对表达式字符串直接 eval (qlib/data/data.py: eval(parse_field(...))),
所以这里先做**白名单校验** —— 只允许注册过的算子名、只允许 7 个原始字段,
其余字符仅限算术/比较/逻辑符号, 杜绝任意代码执行。

输出 JSON: {expr, dates, factor, pct, close_real}
  factor     = 用户表达式 (后复权口径)
  pct        = Rank(expr, 250), 过去 250 日滚动分位 (0~1, 无未来函数)
  close_real = 真实成交价 ($close/$factor), 用于计算前向收益
"""

import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "explore"))

# 可用原始字段 (cn/us bin 均已落盘, 见 notes/concepts.md)
FIELDS = {"open", "high", "low", "close", "volume", "factor", "vwap"}

# 允许调用的算子 (qlib.data.ops 注册表中的安全子集; 复数参数的
# TResample/Rolling/PairRolling/ChangeInstrument 不开放, 避免语法歧义;
# Mask(x, instrument) 需要字符串参数, 也不开放)
OPS = {
    # 滚动
    "Ref", "Mean", "Std", "Var", "Med", "Min", "Max", "Sum", "Count",
    "Quantile", "Rank", "IdxMax", "IdxMin", "Slope", "Rsquare", "Resi",
    "EMA", "WMA", "Mad", "Kurt", "Skew", "Corr", "Cov",
    # 逐元素
    "Abs", "Log", "Sign", "Delta", "Power",
    # 条件 / 逻辑 / 比较
    "If", "Gt", "Ge", "Lt", "Le", "Eq", "Ne",
    "Greater", "Less", "And", "Or", "Not",
    # 算术
    "Add", "Sub", "Mul", "Div",
}

_FIELD_RE = re.compile(r"\$([A-Za-z0-9_]+)")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# 数字字面量 (含 1e-9 这类科学计数法; 先替换掉, 避免 e/E 被当成标识符)
_NUM_RE = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
# 除字段/算子名之外允许出现的字符 (算术/比较/逻辑/括号/逗号/小数点)
_CHAR_OK = set("0123456789 \t\r\n+-*/()<>=&|!.,%")

MAX_LEN = 300
HIST_WIN = 250  # 滚动分位窗口


class ExprError(ValueError):
    """表达式不合法 (返回给前端 400)。"""


def validate(expr: str) -> str:
    if not isinstance(expr, str):
        raise ExprError("表达式必须是字符串")
    expr = expr.strip()
    if not expr:
        raise ExprError("表达式为空")
    if len(expr) > MAX_LEN:
        raise ExprError(f"表达式过长 (最多 {MAX_LEN} 字符)")

    # 1) $field 必须全部可识别
    names = _FIELD_RE.findall(expr)
    rest = _FIELD_RE.sub("", expr)
    if "$" in rest:
        raise ExprError("$ 后必须紧跟字段名")
    bad = sorted({f for f in names if f not in FIELDS})
    if bad:
        raise ExprError(
            "未知字段: " + ", ".join("$" + b for b in bad) + "; 可用: " + ", ".join("$" + f for f in sorted(FIELDS))
        )

    # 2) 标识符必须全部是白名单算子 (数字字面量先剔除, 兼容 1e-9)
    no_num = _NUM_RE.sub("0", rest)
    idents = _IDENT_RE.findall(no_num)
    bad_ops = sorted({i for i in idents if i not in OPS})
    if bad_ops:
        raise ExprError("未知算子: " + ", ".join(bad_ops))

    # 3) 剩余字符只能是运算符/数字
    leftover = _IDENT_RE.sub("", no_num)
    bad_chars = sorted({c for c in leftover if c not in _CHAR_OK})
    if bad_chars:
        raise ExprError("包含不允许的字符: " + " ".join(repr(c) for c in bad_chars))
    return expr


def _num(v, nd=6):
    if v is None:
        return None
    try:
        f = float(v)
    except Exception:
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return round(f, nd)


def evaluate(market: str, symbol: str, expr: str, init: bool = True) -> dict:
    """求值一个表达式, 返回统一 JSON 结构。

    init=False 供常驻 worker 使用 (qlib 已在进程内初始化)。
    """
    expr = validate(expr)
    if init:
        import _common

        _common.launch(market)

    from qlib.data import D

    sym = symbol.upper()
    pct_expr = f"Rank(({expr}),{HIST_WIN})"
    fields = [expr]
    for f in (pct_expr, "$close", "$factor"):
        if f not in fields:
            fields.append(f)
    df = D.features([sym], fields, freq="day")
    df = df[df["$close"].notna()]  # 仅保留实际交易日
    if df.empty:
        raise RuntimeError(f"{sym} 无可用数据")

    col_ix = {name: i for i, name in enumerate(fields)}
    factor_vals = df.iloc[:, 0].tolist()
    pct_vals = df.iloc[:, col_ix[pct_expr]].tolist()
    close_vals = df["$close"].tolist()
    fac_vals = df["$factor"].tolist()

    dates = [t.strftime("%Y-%m-%d") for t in df.index.get_level_values("datetime")]
    close_real = []
    for c, f in zip(close_vals, fac_vals):
        c, f = _num(c), _num(f)
        close_real.append(None if c is None or not f else round(c / f, 6))

    return {
        "market": market,
        "symbol": symbol,
        "expr": expr,
        "hist_win": HIST_WIN,
        "dates": dates,
        "factor": [_num(v) for v in factor_vals],
        "pct": [_num(v, 6) for v in pct_vals],
        "close_real": close_real,
    }


def worker_main(market: str) -> None:
    """常驻 worker: 初始化一次 qlib, 然后按行处理 JSON 请求。"""
    import traceback

    try:
        import _common

        _common.launch(market)
        from qlib.data import D  # noqa: F401  确认 qlib 可用

        print(json.dumps({"ready": True, "market": market}, ensure_ascii=False), flush=True)
    except Exception:
        print(json.dumps({"ready": False, "error": traceback.format_exc()}, ensure_ascii=False), flush=True)
        return

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        rid = None
        try:
            req = json.loads(line)
            rid = req.get("id")
            data = evaluate(
                market,
                str(req.get("symbol", "")).lower(),
                req.get("expr", ""),
                init=False,
            )
            out = {"id": rid, "ok": True, "data": data}
        except Exception as exc:  # 包含 ExprError / qlib 报错
            out = {"id": rid, "ok": False, "error": str(exc)}
        try:
            print(json.dumps(out, ensure_ascii=False), flush=True)
        except Exception:
            return


def main() -> None:
    if len(sys.argv) >= 3 and sys.argv[1] == "--worker":
        worker_main(sys.argv[2].lower())
        return

    if len(sys.argv) < 4:
        print(json.dumps({"error": "用法: factor_eval.py <market> <symbol> <expr>"}, ensure_ascii=False))
        sys.exit(2)
    market, symbol, expr = sys.argv[1], sys.argv[2].lower(), sys.argv[3]
    try:
        data = evaluate(market, symbol, expr)
    except ExprError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        sys.exit(2)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        sys.exit(1)
    print(json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()

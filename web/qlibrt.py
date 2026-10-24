"""Qlib 单例运行时：在主进程内懒加载，只初始化一次，后续调用极快。"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "explore"))

_lock = threading.RLock()
_ready = False
_err = None


def ensure(market: str = "cn") -> None:
    global _ready, _err
    if _ready:
        return
    with _lock:
        if _ready:
            return
        try:
            import qlib
            import _common

            qlib.init(provider_uri=_common._bin(market), region=market, expression_cache=None, dataset_cache=None)
            _ready = True
            _err = None
        except Exception as e:
            _err = e
            raise


def features(instruments, fields, freq: str = "day"):
    import _common
    from qlib.data import D

    # 根据 instrument 推断 market
    market = "us" if any(str(x).upper().startswith(("US",)) or str(x).isupper() and len(str(x)) <= 5 and "." not in str(x) and str(x)[0].isalpha() and str(x)[1:3].isalpha() else False) else "cn"
    # 简化：直接用 D.features
    ensure("cn")  # 默认 cn；如有 us 需求再处理
    return D.features(instruments, fields, freq=freq)

"""explore 脚本的共用配置：路径与股票池全部从 config/symbols.yaml 派生，目录可任意搬移。"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

with (ROOT / "config" / "symbols.yaml").open(encoding="utf-8") as _fh:
    CFG = yaml.safe_load(_fh)


def _pool(market: str) -> list[str]:
    return [s["qlib"].upper() for s in CFG["markets"][market]["symbols"]]


def _bin(market: str) -> str:
    return str(ROOT / CFG["markets"][market]["bin_dir"])


SYM_CN = _pool("cn")
SYM_US = _pool("us")
CN_BIN = _bin("cn")
US_BIN = _bin("us")


def launch(market: str) -> None:
    import qlib
    qlib.init(provider_uri=_bin(market), region=market,
              expression_cache=None, dataset_cache=None)

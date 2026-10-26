"""常驻 qlib worker 池。

每个市场一个子进程: 第一次请求时启动并 init qlib (约 20~30 秒), 之后同一市场
的新表达式只需 ~1 秒。qlib 仍在子进程里, 崩溃/超时不会影响 Flask 主进程。

对外只暴露:

    evaluate(market, symbol, expr) -> dict   # 与 factor_eval.evaluate 返回一致
    shutdown()                              # 进程退出时清理

设计要点:
- 每市场一个 worker, 请求串行化 (qlib 本身也不是线程安全的)。
- stdout 只走 JSON 协议; qlib 日志重定向到 data/logs/factor_<market>.log。
- 父进程退出 / 被杀 -> stdin 关闭 -> worker 读到 EOF 自动退出, 不留孤儿。
- worker 崩溃时重试一次; 超时不重试 (避免用户再等 180 秒)。
"""

from __future__ import annotations

import atexit
import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE.parent / "data" / "logs"
INIT_TIMEOUT = 180  # 冷启动 qlib 的上限 (实测 20~60s)
CALL_TIMEOUT = 180  # 单次表达式计算上限


class WorkerError(RuntimeError):
    """worker 启动/通信/计算失败。"""


class WorkerCrashed(WorkerError):
    """worker 进程异常退出, 可以重启后重试一次。"""


class _Worker:
    def __init__(self, market: str):
        self.market = market
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.queue: queue.Queue = queue.Queue()
        self.reader: threading.Thread | None = None
        self.log = None
        self.next_id = 0
        self.last_used = 0.0

    # ---------------------------------------------------------------- 生命周期
    def _start(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / f"factor_{self.market}.log"
        self.log = open(log_path, "a", encoding="utf-8")
        cmd = [sys.executable, "-u", str(HERE / "factor_eval.py"), "--worker", self.market]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.log,
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=flags,
        )
        self.queue = queue.Queue()
        self.reader = threading.Thread(
            target=self._read_stdout,
            name=f"factor-worker-{self.market}",
            daemon=True,
        )
        self.reader.start()

        try:
            msg = self.queue.get(timeout=INIT_TIMEOUT)
        except queue.Empty:
            self.stop()
            raise WorkerError(f"qlib 初始化超时 ({INIT_TIMEOUT}s), 详见 {log_path}") from None
        if not msg.get("ready"):
            err = str(msg.get("error") or "unknown")
            self.stop()
            raise WorkerError(f"qlib 初始化失败: {err.splitlines()[-1] if err else 'unknown'}")

        self.last_used = time.time()

    def _read_stdout(self) -> None:
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 非协议输出 (理论上不该有)
                self.queue.put(msg)
        finally:
            self.queue.put({"_eof": True})

    def stop(self) -> None:
        proc = self.proc
        self.proc = None
        if proc is not None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if self.log is not None:
            try:
                self.log.close()
            except Exception:
                pass
            self.log = None

    def _alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    # ---------------------------------------------------------------- 调用
    def call(self, symbol: str, expr: str) -> dict:
        with self.lock:
            if not self._alive():
                self.stop()
                self._start()

            self.next_id += 1
            rid = self.next_id
            payload = json.dumps({"id": rid, "symbol": symbol, "expr": expr}, ensure_ascii=False)
            try:
                assert self.proc is not None and self.proc.stdin is not None
                self.proc.stdin.write(payload + "\n")
                self.proc.stdin.flush()
            except Exception as exc:
                self.stop()
                raise WorkerCrashed(f"写入 qlib worker 失败: {exc}") from exc

            deadline = time.time() + CALL_TIMEOUT
            while True:
                remain = deadline - time.time()
                if remain <= 0:
                    self.stop()
                    raise WorkerError(f"因子计算超时 ({CALL_TIMEOUT}s)")
                try:
                    msg = self.queue.get(timeout=remain)
                except queue.Empty:
                    self.stop()
                    raise WorkerError(f"因子计算超时 ({CALL_TIMEOUT}s)") from None

                if msg.get("_eof"):
                    self.stop()
                    raise WorkerCrashed("qlib worker 已退出")
                if msg.get("id") != rid:
                    continue  # 丢弃上一次超时请求的迟到响应
                self.last_used = time.time()
                if not msg.get("ok"):
                    raise WorkerError(str(msg.get("error") or "因子计算失败"))
                return msg["data"]


class _Pool:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.workers: dict[str, _Worker] = {}
        self.closed = False

    def _get(self, market: str) -> _Worker:
        with self.lock:
            if self.closed:
                raise WorkerError("服务正在关闭")
            worker = self.workers.get(market)
            if worker is None:
                worker = _Worker(market)
                self.workers[market] = worker
            return worker

    def evaluate(self, market: str, symbol: str, expr: str) -> dict:
        worker = self._get(market)
        try:
            return worker.call(symbol, expr)
        except WorkerCrashed as exc:
            # 进程异常退出: 重启一次再试, 仍失败则把错误抛给前端
            try:
                return worker.call(symbol, expr)
            except WorkerCrashed as exc2:
                raise WorkerError(f"{exc2} (重启后仍失败)") from exc2

    def shutdown(self) -> None:
        with self.lock:
            self.closed = True
            workers = list(self.workers.values())
            self.workers.clear()
        for worker in workers:
            with worker.lock:
                worker.stop()


_POOL = _Pool()


def evaluate(market: str, symbol: str, expr: str) -> dict:
    return _POOL.evaluate(market, symbol, expr)


def shutdown() -> None:
    _POOL.shutdown()


atexit.register(shutdown)

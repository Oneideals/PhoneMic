#!/usr/bin/env python3
"""PhoneMic 临时诊断日志（不暴露到 UI，只落盘供排查）。

用法：
    import debuglog
    debuglog.install("engine")          # 装载全局异常/退出钩子
    debuglog.log("engine", "启动")       # 普通事件
    debuglog.log("engine", "出错了", exc=True)   # 附带当前异常堆栈

日志位置：项目根 `.debug.log`（超过 4MB 自动滚动为 .debug.log.1）
排查完问题后，删掉这个模块 + 两处 import 即可完全移除。
"""
import atexit
import os
import sys
import threading
import time
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent
LOG_FILE = BASE / ".debug.log"
MAX_BYTES = 4 * 1024 * 1024      # 单文件上限，超出滚动一份备份
BACKUP = BASE / ".debug.log.1"

_lock = threading.Lock()
_start = time.time()
_tag = "?"


def _rotate():
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_BYTES:
            if BACKUP.exists():
                BACKUP.unlink()
            LOG_FILE.rename(BACKUP)
    except Exception:
        pass


def log(tag: str, msg, exc: bool = False) -> None:
    """写一行日志。exc=True 时附带当前异常的堆栈。"""
    try:
        now = time.time()
        ts = time.strftime("%m-%d %H:%M:%S", time.localtime(now))
        up = now - _start
        line = f"{ts} +{up:8.1f}s [{tag}] {msg}"
        if exc:
            line += "\n" + "".join(traceback.format_exc()).rstrip()
        with _lock:
            _rotate()
            with open(LOG_FILE, "a") as fh:
                fh.write(line + "\n")
                fh.flush()
    except Exception:
        pass


def dump_thread(tag: str, ident: int, label: str) -> None:
    """打印指定线程的当前调用栈——定位"卡死在哪一行"的关键手段。"""
    try:
        frames = sys._current_frames()
        frame = frames.get(ident)
        if frame is None:
            log(tag, f"{label} 线程已结束（不在活跃帧中）")
            return
        stack = "".join(traceback.format_stack(frame)).rstrip()
        log(tag, f"{label} 线程栈：\n{stack}")
    except Exception:
        pass


def _thread_excepthook(args):
    log(_tag, f"线程异常 {args.thread.name}：{args.exc_type.__name__}: {args.exc_value}")
    try:
        tb = "".join(traceback.format_exception(args.exc_type, args.exc_value,
                                                args.exc_traceback)).rstrip()
        log(_tag, "线程异常堆栈：\n" + tb)
    except Exception:
        pass


def _excepthook(etype, value, tb):
    log(_tag, f"未捕获异常 {etype.__name__}: {value}")
    try:
        log(_tag, "异常堆栈：\n" + "".join(traceback.format_exception(etype, value, tb)).rstrip())
    except Exception:
        pass


def install(tag: str) -> None:
    """装载全局钩子：未捕获异常、子线程异常、进程退出、关键信号。"""
    global _tag
    _tag = tag
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    atexit.register(lambda: log(tag, f"进程正常退出 pid={os.getpid()}"))

    def _on_signal(signum, _frame):
        import signal
        try:
            name = signal.Signals(signum).name
        except Exception:
            name = str(signum)
        log(tag, f"收到信号 {name}({signum})，准备退出\n"
                 + "".join(traceback.format_stack(_frame)).rstrip())

    for s in ("SIGTERM", "SIGINT", "SIGHUP"):
        sig = getattr(__import__("signal"), s, None)
        if sig is None:
            continue
        try:
            import signal as _sig
            _sig.signal(sig, _on_signal)
        except Exception:
            pass

    log(tag, f"=== 启动 pid={os.getpid()} ppid={os.getppid()} "
             f"argv={sys.argv} ===")

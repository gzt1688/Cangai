"""渲染调试日志（排查 3D 视口原生崩溃用，默认关闭）。

单一文件句柄、行缓冲、每行立即 flush —— 原生段错误发生后，日志的最后几行
即崩溃前最后执行的阶段，可精确定位。

默认 ``_enabled = False``：``dbg()`` 直接返回，不建文件、不写盘、开销为零。
需排查时调 ``_renderdbg.enable()``（或置 ``_enabled = True``）再复现即可。
"""
from __future__ import annotations

import os
import time

_LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "render_debug.log",
)

_file = None
_enabled = True           # 开启：dbg() 只在渲染异常/回退等罕见事件时写文件，开销可忽略；
                          # 复现"闪/透明"等问题后把项目根目录 render_debug.log 发来即可定位


def enable(on: bool = True) -> None:
    """打开/关闭渲染调试日志（关闭时 dbg() 为空操作，无文件 I/O）。"""
    global _enabled
    _enabled = bool(on)


# 逐事件级流水账（每个 objectChanged / 每次操作轴刷新都一行）默认不写：
# dbg() 是 flush 到磁盘的同步写，浮雕笔刷期每秒 30–120 个事件会把
# 几十次阻塞写盘塞进鼠标事件链里（防病毒软件下更贵），表现为拖动
# 明显滞后。需抓这类流水时调 verbose(True) 再复现。
_verbose = False


def verbose(on: bool = True) -> None:
    """打开/关闭逐事件级日志（不影响异常/回退等诊断行）。"""
    global _verbose
    _verbose = bool(on)


def vdbg(msg: str) -> None:
    """逐事件级日志：仅在 verbose 打开时写（默认空操作，零开销）。"""
    if not _enabled or not _verbose:
        return
    dbg(msg)


def dbg(msg: str) -> None:
    """写一行调试日志并立即 flush（异常静默吞掉，绝不影响渲染）。"""
    global _file
    if not _enabled:
        return
    try:
        if _file is None:
            _file = open(_LOG_PATH, "w", encoding="utf-8", buffering=1)
        _file.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        _file.flush()
    except Exception:
        pass


def path() -> str:
    return _LOG_PATH

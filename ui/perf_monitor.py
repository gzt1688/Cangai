"""交互期性能打点（专门为"界面很卡但看不出卡在哪"准备）。

三层打点，全部写入项目根目录 ``render_debug.log``（与渲染诊断同一份，便于用户
一次把日志发回）：

1. **慢事件**（:class:`CangaiApplication`）：在 ``QApplication.notify`` 里给每个
   事件计时，超过 ``SLOW_EVENT_MS`` 的记一行。用来抓"鼠标移动/滚轮/重绘里做了
   重活"——例如拖动塑形时每笔都重建整份网格，会直接显示成一条
   ``slow event 420ms MouseMove``。
2. **慢间隙**：连续两个事件之间空转超过 ``SLOW_GAP_MS`` 时记一行，用来抓
   "事件很快但两次之间被同步等待拖住"（GPU 提交/纹理上传/FBO 回读）。
3. **慢重建**（:func:`log_slow`）：业务侧主动上报（网格全量重建、ROI 增量
   更新等），带规模参数，超过阈值才写。

设计约束：正常路径只多两次 ``perf_counter`` 取样（≈0.1µs/事件）；写盘只在超阈值
时发生，且每类都有行数上限，不会把日志刷爆、也不会拖慢交互。
"""
from __future__ import annotations

import time

from PySide6.QtWidgets import QApplication

from .view_3d._renderdbg import dbg

SLOW_EVENT_MS = 30.0      # 单个事件处理超过这么久 → 记一行
SLOW_GAP_MS = 60.0        # 输入事件之后空转超过这么久 → 记一行（界面"反应慢"）
_MAX_EVENT_LINES = 40     # 慢事件总行数上限（防刷爆）
_MAX_GAP_LINES = 10
_SLOW_LOG_CAP = 40        # log_slow 每类上限

# 只有"用户输入"之后的空转才算卡：启动预热、定时器空跑、窗口空闲都会产生
# 很长的 gap，不设这个过滤会把日志刷满噪声（实测启动阶段全是 200~1800ms 的
# 空闲 gap，与"卡"无关）。
_INPUT_EVENT_NAMES = (
    "MouseMove", "MouseButtonPress", "MouseButtonRelease",
    "MouseButtonDblClick", "Wheel", "KeyPress", "KeyRelease",
)
_INPUT_EVENTS = frozenset(_INPUT_EVENT_NAMES)

_event_lines = 0
_gap_lines = 0
_slow_counts: dict[str, int] = {}
_last_ts = 0.0
_prev_type = -1          # 上一个事件类型（int，热路径不解析名字）
_name_cache: dict[int, str] = {}
_trace_left = 0          # >0 时逐事件写日志（崩溃定位：记下按 Ctrl+Z 之后的事件序列）


def trace_next(n: int = 8) -> None:
    """开启接下来 n 个事件的逐条日志（排查原生闪退用，默认不开）。"""
    global _trace_left
    _trace_left = int(n)


def _input_types() -> frozenset:
    """``QEvent.Type`` 里属于"用户输入"的那些数值（只算一次，惰性）。"""
    try:
        from PySide6.QtCore import QEvent
        return frozenset(int(getattr(QEvent.Type, n))
                         for n in _INPUT_EVENT_NAMES
                         if hasattr(QEvent.Type, n))
    except Exception:
        return frozenset()


_INPUT_TYPES = _input_types()


def _ev_type(event) -> int:
    """事件对象 → 类型整数（notify 热路径唯一要做的解析）。"""
    try:
        return int(event.type())
    except Exception:
        return -1


def _type_name(t: int) -> str:
    """事件类型数字 → 短名（'MouseMove'），带缓存。

    ``notify`` 是**每个事件**都要经过的热路径，而 PySide6 枚举的 ``.name``
    每次访问都会新建一个 str（还有一层 getattr）；鼠标/滚轮密集时按 1kHz
    算就是纯浪费。改为：热路径只存 int，名字只在真正要写日志时解析一次。
    """
    n = _name_cache.get(t)
    if n is not None:
        return n
    name = "?"
    try:
        from PySide6.QtCore import QEvent
        name = QEvent.Type(t).name
    except Exception:
        try:
            name = str(t)
        except Exception:
            pass
    _name_cache[t] = name
    return name


def _ev_name(event) -> str:
    """事件类型短名（'MouseMove'）。日志/测试用；热路径请直接用 ``_ev_type``。"""
    return _type_name(_ev_type(event))


def note_event(dt_ms: float, event, receiver) -> None:
    """notify 里调用：单事件耗时。"""
    global _event_lines
    if dt_ms < SLOW_EVENT_MS or _event_lines >= _MAX_EVENT_LINES:
        return
    _event_lines += 1
    try:
        rc = "-" if receiver is None else type(receiver).__name__
        dbg("slow event %6.1fms  %-18s recv=%s"
            % (dt_ms, _ev_name(event), rc))
    except Exception:
        pass


def note_gap(gap_ms: float, prev) -> None:
    """notify 里调用：输入事件与下一个事件之间的空转（同步等待/合成阻塞）。

    只在**上一个事件是鼠标/键盘输入**时记录：那正是用户能感知的"点了没反应"。
    ``prev`` 可以是事件类型整数（热路径）或类型名（测试/外部调用）。
    """
    global _gap_lines
    if isinstance(prev, int):
        if prev not in _INPUT_TYPES:
            return
        pname = _type_name(prev)
    else:
        if prev not in _INPUT_EVENTS:
            return
        pname = str(prev)
    if gap_ms < SLOW_GAP_MS or _gap_lines >= _MAX_GAP_LINES:
        return
    _gap_lines += 1
    try:
        dbg("slow gap   %6.1fms  after=%s（输入后界面停顿：同步等待/GPU 回读）"
            % (gap_ms, pname))
    except Exception:
        pass


def log_slow(key: str, dt_ms: float, msg: str, thr: float = 60.0) -> None:
    """业务侧上报慢操作（网格重建/ROI 更新等）。``key`` 用来分类限行。"""
    if dt_ms < thr:
        return
    n = _slow_counts.get(key, 0)
    if n >= _SLOW_LOG_CAP:
        return
    _slow_counts[key] = n + 1
    try:
        dbg("slow %-10s %6.1fms  %s" % (key, dt_ms, msg))
    except Exception:
        pass


class CangaiApplication(QApplication):
    """带慢事件打点的 QApplication（main.py 用它代替 QApplication）。"""

    def notify(self, receiver, event):     # noqa: N802 (Qt 命名)
        global _last_ts, _prev_type, _trace_left
        t0 = time.perf_counter()
        gap = (t0 - _last_ts) * 1e3 if _last_ts else 0.0
        prev = _prev_type
        if _trace_left > 0:
            _trace_left -= 1
            try:
                dbg("event %-16s recv=%s" % (
                    _type_name(_ev_type(event)),
                    "-" if receiver is None else type(receiver).__name__))
            except Exception:
                pass
        try:
            return super().notify(receiver, event)
        finally:
            t1 = time.perf_counter()
            _last_ts = t1
            _prev_type = _ev_type(event)
            try:
                if prev >= 0:
                    note_gap(gap, prev)
                note_event((t1 - t0) * 1e3, event, receiver)
            except Exception:
                pass

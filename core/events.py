"""轻量事件总线：插件与 AI 自动化的感知通道（发布/订阅，处理器异常隔离）。

设计要点：
- 纯 Python、不依赖 Qt：core 层可直接使用；
- 处理器异常被捕获并回传给 on_error（默认打印），保证一个订阅者出错
  不影响其他订阅者与发布方——开放生态的基本容错要求；
- 订阅返回退订闭包，避免插件卸载后残留连接。
"""
from __future__ import annotations
import traceback
from typing import Callable, Dict, List


class EventBus:
    """按主题字符串的同步发布/订阅。"""

    def __init__(self, on_error: Callable[[str, BaseException], None] | None = None) -> None:
        self._subs: Dict[str, List[Callable]] = {}
        self._on_error = on_error or self._defaultOnError

    @staticmethod
    def _defaultOnError(topic: str, exc: BaseException) -> None:
        print(f"[events] 订阅处理器出错 topic={topic}: {exc}")
        traceback.print_exc()

    def subscribe(self, topic: str, handler: Callable) -> Callable[[], None]:
        """订阅主题；返回退订函数。handler 签名 handler(payload)。"""
        self._subs.setdefault(topic, []).append(handler)

        def _unsubscribe() -> None:
            try:
                self._subs.get(topic, []).remove(handler)
            except (KeyError, ValueError):
                pass
        return _unsubscribe

    def unsubscribe(self, topic: str, handler: Callable) -> None:
        try:
            self._subs.get(topic, []).remove(handler)
        except (KeyError, ValueError):
            pass

    def emit(self, topic: str, payload=None) -> None:
        """发布事件；逐个调用订阅者，单个异常不影响其余订阅者。"""
        for handler in list(self._subs.get(topic, ())):
            try:
                handler(payload)
            except Exception as exc:   # 隔离订阅者错误（含插件代码）
                self._on_error(topic, exc)
            except BaseException as exc:   # SystemExit 等也不允许打断发布方
                self._on_error(topic, exc)

    def subscriberCount(self, topic: str | None = None) -> int:
        if topic is None:
            return sum(len(v) for v in self._subs.values())
        return len(self._subs.get(topic, ()))

"""轻量事件总线：插件与 AI 自动化的感知通道（发布/订阅，处理器异常隔离）。

设计要点：
- 纯 Python、不依赖 Qt：core 层可直接使用；
- 处理器签名自适应（订阅时探测一次）：
  * ``handler(payload)`` —— 标准约定，载荷恒传入（无载荷主题传 None）；
  * ``handler()``        —— 零参处理器同样可用，发布时自动不传参；
  * 无法内省的处理器按标准约定传 payload；
- 处理器异常被捕获并回传 on_error（默认打印），保证一个订阅者出错
  不影响其他订阅者与发布方——开放生态的基本容错要求；
- 订阅返回退订闭包，避免插件卸载后残留连接。
"""
from __future__ import annotations
import inspect
import traceback
from typing import Callable, Dict, List, Tuple


def _makeAdapter(handler: Callable) -> Callable:
    """按处理器签名生成适配调用：能收位置参数就传 payload，否则零参调用。"""
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return lambda payload: handler(payload)
    capacity = 0
    for p in sig.parameters.values():
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL):
            capacity += 1
    if capacity >= 1:
        return lambda payload: handler(payload)
    return lambda payload: handler()


class _Subscription:
    __slots__ = ("handler", "adapter")

    def __init__(self, handler: Callable, adapter: Callable) -> None:
        self.handler = handler
        self.adapter = adapter


class EventBus:
    """按主题字符串的同步发布/订阅。"""

    def __init__(self, on_error: Callable[[str, BaseException], None] | None = None) -> None:
        self._subs: Dict[str, List[_Subscription]] = {}
        self._on_error = on_error or self._defaultOnError

    @staticmethod
    def _defaultOnError(topic: str, exc: BaseException) -> None:
        print(f"[events] 订阅处理器出错 topic={topic}: {exc}")
        traceback.print_exc()

    def subscribe(self, topic: str, handler: Callable) -> Callable[[], None]:
        """订阅主题；返回退订函数。处理器签名自适应（见模块 docstring）。"""
        sub = _Subscription(handler, _makeAdapter(handler))
        self._subs.setdefault(topic, []).append(sub)

        def _unsubscribe() -> None:
            try:
                self._subs.get(topic, []).remove(sub)
            except (KeyError, ValueError):
                pass
        return _unsubscribe

    def unsubscribe(self, topic: str, handler: Callable) -> None:
        """按原处理器对象退订（与 subscribe 等价）。"""
        for sub in list(self._subs.get(topic, ())):
            if sub.handler is handler:
                self._subs.get(topic, []).remove(sub)

    def emit(self, topic: str, payload=None) -> None:
        """发布事件；逐个调用订阅者，单个异常不影响其余订阅者。"""
        for sub in list(self._subs.get(topic, ())):
            try:
                sub.adapter(payload)
            except Exception as exc:   # 隔离订阅者错误（含插件代码）
                self._on_error(topic, exc)
            except BaseException as exc:   # SystemExit 等也不允许打断发布方
                self._on_error(topic, exc)

    def subscriberCount(self, topic: str | None = None) -> int:
        if topic is None:
            return sum(len(v) for v in self._subs.values())
        return len(self._subs.get(topic, ()))

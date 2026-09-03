"""插件瞬时动作命令：把插件注册的纯函数包装成命令生命周期。

有宿主时经 CommandManager 执行（获得撤销分组/历史记录/重复执行等
与内置命令一致的待遇）；函数签名 fn(context: dict) -> Any，context 为
注册时 meta 里的 "context"（缺省空 dict）。
"""
from __future__ import annotations
import traceback

from core.vector.geometry_types import Point3D
from ..command_instance import CommandInstance


class PluginActionCommand(CommandInstance):
    def __init__(self, name: str, fn, meta: dict) -> None:
        super().__init__()
        self._name = name
        self._fn = fn
        self._meta = dict(meta or {})

    def name(self) -> str:
        return self._name

    def commandTitle(self) -> str:
        return self._meta.get("title", self._name)

    def prompt(self) -> str:
        return self._meta.get("prompt", "执行中…")

    def showSnapHints(self) -> bool:
        return False

    def start(self) -> None:
        super().start()
        try:
            self._fn(dict(self._meta.get("context", {})))
        except Exception as e:
            traceback.print_exc()
            if self._manager is not None:
                self._manager.notifyResult(f"插件动作 {self._name} 失败: {e}")
        self.finish()   # 瞬时动作：执行即完成

    def onMousePress(self, wp: Point3D, mods) -> None:
        pass

    def onMouseMove(self, wp: Point3D, mods) -> None:
        pass

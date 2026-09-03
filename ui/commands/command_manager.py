"""命令调度中心：维护活动命令、上下文与提示信号。"""
from __future__ import annotations
from typing import Dict, Optional, Callable
from PySide6.QtCore import QObject, Signal

from core.vector.geometry_types import Point3D
from .command_instance import CommandInstance


class CommandManager(QObject):
    activeCommandChanged = Signal(str)
    commandFinished = Signal(str)
    promptChanged = Signal(str)
    resultMessage = Signal(str)
    commandRepeated = Signal(str)

    _instance = None

    def __init__(self) -> None:
        super().__init__()
        self._factories: Dict[str, Callable[[], CommandInstance]] = {}
        self._active: Optional[CommandInstance] = None
        self._lastCommandName = ""
        self._doc = None
        self._view = None

    @classmethod
    def instance(cls) -> "CommandManager":
        if cls._instance is None:
            cls._instance = CommandManager()
        return cls._instance

    def setContext(self, doc, view) -> None:
        self._doc = doc
        self._view = view

    def register(self, name: str, factory: Callable[[], CommandInstance]) -> None:
        self._factories[name] = factory

    def notifyResult(self, text: str) -> None:
        """发布操作结果反馈（由视图左下角悬浮提示区显示）。"""
        self.resultMessage.emit(text)

    def hasActive(self) -> bool:
        return self._active is not None and self._active.isAlive()

    def activeInstance(self) -> Optional[CommandInstance]:
        """当前活动命令实例（供主窗口拉取参数面板），无活动命令返回 None。"""
        if self._active is not None and self._active.isAlive():
            return self._active
        return None

    def activeName(self) -> str:
        if self._active is None:
            return ""
        return self._active.name()

    def executeCommand(self, name: str, *args) -> bool:
        """启动命令；额外参数透传给工厂（如 Image 命令的图片路径）。"""
        if self._active is not None and self._active.isAlive():
            return False
        factory = self._factories.get(name)
        if factory is None:
            return False
        cmd = factory(*args)
        cmd.setContext(self._doc, self._view)
        cmd._manager = self   # 面板直接调 finish 时能回通知收尾
        self._active = cmd
        self._lastCommandName = name   # 供右键单击重复上一命令
        if self._doc is not None:
            self._doc.beginEdit()   # 命令生命周期内所有文档修改合并为一次撤销
        cmd.start()
        self.activeCommandChanged.emit(name)
        self.promptChanged.emit(cmd.prompt())
        return True

    def repeatLastCommand(self) -> bool:
        """重复上一次执行的命令（画布无活动命令时右键单击触发，CAD 惯例）。
        无历史命令或已有活动命令时为空操作。"""
        if self.hasActive() or not self._lastCommandName:
            return False
        if self.executeCommand(self._lastCommandName):
            self.commandRepeated.emit(self._lastCommandName)
            return True
        return False

    def lastCommandName(self) -> str:
        return self._lastCommandName

    def onMousePress(self, wp: Point3D, mods) -> None:
        if self._active is None or not self._active.isAlive():
            return
        self._active.onMousePress(wp, mods)
        self._checkDone()

    def onRightClick(self, wp: Point3D) -> None:
        self._onRight(wp)

    def onMouseMove(self, wp: Point3D, mods) -> None:
        if self._active is None or not self._active.isAlive():
            return
        self._active.onMouseMove(wp, mods)

    def onMouseRelease(self, mods) -> None:
        if self._active is None or not self._active.isAlive():
            return
        self._active.onMouseRelease(mods)

    def _onRight(self, wp: Point3D) -> None:
        if self._active is None:
            return
        if self._active.onRightClick(wp):
            self._checkDone()
            return
        self.cancelActive()

    def onEnter(self) -> None:
        if self._active is None or not self._active.isAlive():
            return
        if self._active.onEnter():
            self._checkDone()

    def onKeyPress(self, key, mods) -> bool:
        """转发按键给活动命令（如 C 闭合）；命令消费返回 True。"""
        if self._active is None or not self._active.isAlive():
            return False
        if self._active.onKeyPress(key, mods):
            self._checkDone()
            return True
        return False

    def cancelActive(self) -> None:
        if self._active is not None:
            self._active.cancel()
            self._active = None
            self.activeCommandChanged.emit("")
            self.promptChanged.emit("提示信息显示区")
            if self._doc is not None:
                self._doc.endEdit()

    def _checkDone(self) -> None:
        if self._active is None:
            return
        if self._active.isComplete() or self._active.isCanceled():
            name = self._active.name()
            self._active = None
            self.activeCommandChanged.emit("")
            self.promptChanged.emit("提示信息显示区")
            if not name:
                return
            if self._doc is not None:
                self._doc.endEdit()
            self.commandFinished.emit(name)
        else:
            self.promptChanged.emit(self._active.prompt())

    def prompt(self) -> str:
        if self._active is None or not self._active.isAlive():
            return "提示信息显示区"
        return self._active.prompt()
"""命令调度中心：维护活动命令、上下文与提示信号。"""
from __future__ import annotations
from typing import Dict, Optional, Callable
from PySide6.QtCore import QObject, Signal

from core.vector.geometry_types import Point3D
from .command_instance import CommandInstance
from .mode_policy import modeAllows, refusalHint


class CommandManager(QObject):
    activeCommandChanged = Signal(str)
    commandFinished = Signal(str)
    promptChanged = Signal(str)
    resultMessage = Signal(str)
    commandRepeated = Signal(str)
    # 预览失效：命令的画布预览（笔刷圈等）变化但不经鼠标事件时发出，
    # 由可见视口连接并立即重绘（否则要等下次鼠标移动才刷新）
    previewInvalidated = Signal()

    _instance = None

    def __init__(self) -> None:
        super().__init__()
        self._factories: Dict[str, Callable[[], CommandInstance]] = {}
        self._meta: Dict[str, dict] = {}   # 命令元数据（命令清单/AI 工具表用）
        self._active: Optional[CommandInstance] = None
        self._lastCommandName = ""
        self._doc = None
        self._view = None
        self._viewProbe = None   # 活动画布视口（3D 模式下提供 canvasMode/viewMode）
        self._modeProbe = None   # 画布模式探测（返回 "2d"/"relief"/"3d"/"cnc"）
        self._lastRefusal = ""   # 最近一次被模式门禁拒绝的原因（空=无）

    @classmethod
    def instance(cls) -> "CommandManager":
        if cls._instance is None:
            cls._instance = CommandManager()
        return cls._instance

    def setContext(self, doc, view) -> None:
        self._doc = doc
        self._view = view

    def setViewProbe(self, probe) -> None:
        """注入活动画布视口（如 Viewport3D）：命令判断视图方向（前视/右视
        侧视平面绘制）时查询它；上下文 _view 始终是 2D 画布（预览图元宿主），
        两者职责分离。"""
        self._viewProbe = probe

    def setModeProbe(self, probe) -> None:
        """注入画布模式探测（无参可调用，返回 "2d"/"relief"/"3d"/"cnc"）：
        executeCommand 据此按 mode_policy 允许矩阵做中央门禁——按钮置灰之外的
        兜底（右键重复命令/AI API/快捷键等旁路入口全部经过这里）。"""
        self._modeProbe = probe

    def lastRefusal(self) -> str:
        """最近一次被模式门禁拒绝的提示；无拒绝时为空串。"""
        return self._lastRefusal

    def register(self, name: str, factory: Callable[[], CommandInstance],
                 meta: Optional[dict] = None) -> None:
        """注册命令工厂；meta 为可选元数据（title/description/group 等），
        供命令清单（AI 工具表）与插件管理使用。"""
        self._factories[name] = factory
        if meta:
            self._meta[name] = dict(meta)

    def unregister(self, name: str) -> bool:
        """注销命令（插件停用用）；活动命令为该命令时先取消。返回是否删除。"""
        existed = self._factories.pop(name, None) is not None
        self._meta.pop(name, None)
        if existed and self._active is not None and self._active.name() == name:
            self.cancelActive()
        return existed

    def commandMeta(self, name: str) -> dict:
        """命令元数据；未注册返回 None。"""
        return self._meta.get(name)

    def listCommands(self) -> list:
        """已注册命令清单（name + meta），按名称排序——AI 工具表数据源。"""
        out = []
        for name in sorted(self._factories):
            m = dict(self._meta.get(name, {}))
            m["name"] = name
            out.append(m)
        return out

    def notifyResult(self, text: str) -> None:
        """发布操作结果反馈（由视图左下角悬浮提示区显示）。"""
        self.resultMessage.emit(text)

    def invalidatePreview(self) -> None:
        """命令预览（笔刷圈等）已变化：让可见视口立即重绘。

        命令的预览路径挂在 2D 场景图元上，快捷键/面板改参时命令只能
        刷到隐藏的 2D 视图；浮雕模式真正可见的是 3D 视口，由它连接
        本信号即时重绘（修复"改笔刷大小圆圈不更新"）。
        """
        self.previewInvalidated.emit()

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
        """启动命令；额外参数透传给工厂（如 Image 命令的图片路径）。
        启动前按画布模式允许矩阵（mode_policy）门禁：当前模式不允许的命令
        直接拒绝（发布提示并返回 False），不创建命令实例。"""
        if self._active is not None and self._active.isAlive():
            return False
        factory = self._factories.get(name)
        if factory is None:
            return False
        self._lastRefusal = ""
        mode = ""
        try:
            mode = self._modeProbe() if self._modeProbe is not None else ""
        except Exception:
            mode = ""
        if mode and not modeAllows(name, mode):
            self._lastRefusal = refusalHint(name, mode)
            self.resultMessage.emit(self._lastRefusal)
            return False
        cmd = factory(*args)
        cmd.setContext(self._doc, self._view)
        cmd.setModeView(self._viewProbe)   # 活动画布视口（3D 侧视判定用）
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
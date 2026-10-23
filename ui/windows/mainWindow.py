"""主窗口：负责整体布局（Dock 停靠、工具条）与全局信号接线。

所有业务交互在此汇聚：命令请求、捕捉开关、模式切换、选择联动等。
"""
from __future__ import annotations

import math

from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                               QStackedWidget, QFileDialog, QMessageBox,
                               QScrollArea, QInputDialog, QSplitter)
from PySide6.QtCore import (Qt, QCoreApplication, QTimer, QEvent, QObject,
                            Signal)
from PySide6.QtGui import QShowEvent, QKeyEvent, QTransform, QShortcut, QKeySequence, QIcon

from PySide6QtAds import (CDockManager, CDockWidget, CDockAreaWidget,
                          LeftDockWidgetArea, RightDockWidgetArea,
                          BottomDockWidgetArea, CenterDockWidgetArea)
from resource_path import resource_path

from core.vector.geometry_types import Point3D, BoundingBox3D, ObjectType
from core.document import Document
from core import app_settings
from core.snap.snap_manager import SnapManager
from core.snap.snap_types import SnapType
from core.grid_manager import GridManager
import numpy as np
from ui.widgets.paint_state import PaintState
from ui.styles import theme
from ui.view_2d.graphics_scene import GraphicsScene
from ui.view_2d.graphics_view import GraphicsView
from ui.view_2d.scene_bridge import SceneBridge
from ui.view_2d.curve_edit_controller import CurveEditController
from ui.view_3d.viewport_3d import Viewport3D
from ui.commands.command_manager import CommandManager
from ui.commands.concrete.draw_commands import registerAll
from ui.commands.concrete.shape_commands import registerAll as registerShapes
from ui.commands.concrete.edit_commands import registerAll as registerEdits
from ui.commands.concrete.text_command import registerAll as registerText
from ui.commands.concrete.relief_commands import registerAll as registerRelief
from ui.commands.concrete.solid_commands import registerAll as registerSolids
from ui.commands.concrete.primitive_commands import registerAll as registerPrimitives
from ui.commands.concrete.thread_command import registerAll as registerThread
from ui.commands.concrete.brep_wall_command import \
    registerAll as registerWall
from ui.commands.concrete.push_pull_commands import \
    registerAll as registerPushPull
from ui.commands.concrete.boolean_commands import registerAll as registerBoolean
from ui.commands.concrete.cnc_commands import registerAll as registerCnc
from ui.commands.concrete.revcloud_command import registerAll as registerRevcloud

from ui.commands.concrete.image_command import ImagePlaceCommand
from ui.commands.concrete.image_trace_command import ImageTraceCommand
from ui.commands.concrete.plugin_action_command import PluginActionCommand
from core.api import CangaiAPI
from core.plugin_manager import PluginManager
from ui.commands.concrete.dimension_commands import registerAll as registerDims
from ui.commands.concrete.dimension_commands import TEXT_TO_OBJ_GAP
from core.vector.dimension_object import DimensionObject
from ui.gizmo.gizmo_manager import GizmoManager, View2DAdapter, View3DAdapter

from ui.widgets.status_bar import StatusBarWidget
from ui.widgets.top_dock import TopDock
from ui.widgets.title_bar import TitleBar
from ui.widgets.left_panel import LeftPanelWidget
from ui.widgets.layer_panel import LayerPanel
from ui.widgets.property_panel import PropertyPanel
from ui.widgets.transform_panel import TransformPanel
from ui.widgets.capture_toolbar import CaptureToolbar
from ui.widgets.view_switch_bar import ViewSwitchBar
from ui.widgets.canvas_mode_bar import CanvasModeBar
from ui.widgets.command_param_host import CommandParamHost
from ui.widgets.file_tab_bar import FileTabBar
from ui.widgets.settings_dialog import SettingsDialog
from i18n import tr, languageChanged


_SNAP_KEY_MAP = {
    "endpoint": SnapType.ENDPOINT, "nearest": SnapType.NEAREST,
    "point": SnapType.POINT, "midpoint": SnapType.MIDPOINT,
    "center": SnapType.CENTER, "intersection": SnapType.INTERSECTION,
    "perpendicular": SnapType.PERPENDICULAR, "tangent": SnapType.TANGENT,
    "quarter": SnapType.QUARTER, "vertex": SnapType.VERTEX,
}


def _desktopDir() -> str:
    """当前用户真实桌面路径。

    注册表优先（兼容 OneDrive/组策略/自定义位置把桌面重定向到非 C 盘），
    其次 SHGetFolderPathW，最后 ~/Desktop、~/桌面 兜底。
    注册表值为 REG_EXPAND_SZ（可能含 %USERPROFILE%），统一先展开环境变量。
    """
    import os
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                r"\User Shell Folders") as key:
            val, _ = winreg.QueryValueEx(key, "Desktop")
            val = os.path.expandvars(str(val))
            if val and os.path.isdir(val):
                return val
    except Exception:
        pass
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        # CSIDL_DESKTOP = 0x0000
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0, None, 0, buf) == 0:
            val = os.path.expandvars(buf.value)
            if val and os.path.isdir(val):
                return val
    except Exception:
        pass
    home = os.path.expanduser("~")
    for name in ("Desktop", "桌面"):
        candidate = os.path.join(home, name)
        if os.path.isdir(candidate):
            return candidate
    return home


def _uniqueExportPath(path: str) -> str:
    """目标文件已存在时在主名后追加序号 (1)、(2)…，避免覆盖已有文件。"""
    import os
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while True:
        candidate = f"{base}({i}){ext}"
        if not os.path.exists(candidate):
            return candidate
        i += 1


class _MainThreadInvoker(QObject):
    """工作线程 → 主线程任务投递（跨线程信号自动排队到宿主所在线程）。
    插件/AI 的工作线程经 api.runOnMainThread(fn) 安全改动 Qt 界面。"""
    taskQueued = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.taskQueued.connect(self._run)

    def post(self, fn) -> None:
        self.taskQueued.emit(fn)

    def _run(self, fn) -> None:
        try:
            fn()
        except Exception:
            import traceback
            traceback.print_exc()


class MainWindow(QMainWindow):
    # 启动加载页：后台预热进度（0~100, 当前加载项）与完成通知
    startupProgress = Signal(int, str)
    startupFinished = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(tr("Cangai 浮雕设计"))
        self.setWindowIcon(QIcon(resource_path("icons/Cangai.ico")))
        self.resize(1280, 800)
        QCoreApplication.instance().setStyleSheet(theme.globalQss())

        self._doc = Document()
        # 多文件系统：打开的文档列表与当前索引；每个文档独立对象集/撤销历史。
        # self._doc 始终指向当前活动文档，切换时整体重绑（见 _switchToDocument）。
        self._openDocs: list = [self._doc]
        self._docIndex: int = 0
        self._untitledSeq: int = 1
        self._doc.setTitle(tr("未命名1"))
        self._clipboard: list = []   # 复制/粘贴：保存选中对象的克隆
        # 浮雕编辑态：锁定的模型（不可选中/变换，仅浮雕/Z 向操作）及其 id 恢复号
        self._reliefLockedModel = None
        self._reliefRestoreId: str | None = None
        self._scene = GraphicsScene()
        self._view = GraphicsView(self._scene)
        self._view.setDocument(self._doc)
        self._bridge = SceneBridge(self._scene, self._doc)
        # CNC 刀路叠加层：路径条目渲染到 2D 画布（监听全局 CncState，无需随文档切换重绑）
        from ui.view_2d.cnc_path_overlay import CncToolpathOverlay
        self._cncOverlay = CncToolpathOverlay(self._scene, self)
        # 供操作轴 Alt+克隆时同步场景图元选中态使用（见 GizmoManager._cloneSelection）
        self._doc._bridge = self._bridge
        # 视图缩放变化时重算固定屏幕尺寸的点标记；初始同步一次当前缩放
        self._view.scaleChanged.connect(self._bridge.updatePixelScale)
        self._bridge.updatePixelScale(self._view.zoomScale())

        self._cm = CommandManager.instance()
        self._cm.setContext(self._doc, self._view)
        registerAll(self._cm)
        registerShapes(self._cm)
        registerEdits(self._cm)
        registerText(self._cm)
        registerRelief(self._cm)
        registerSolids(self._cm)   # 3D 实体命令（挤出等）
        registerPrimitives(self._cm)   # 3D 标准体（立方体/圆柱体/圆环/圆锥）
        registerThread(self._cm)       # 3D 螺纹（圆柱面内/外螺纹 + 独立螺柱）
        registerWall(self._cm)          # 墙线（曲线双侧偏移+挤出）
        registerPushPull(self._cm)      # 推拉（面推拉=挤出/凹陷，线推拉=倒角）
        registerBoolean(self._cm)       # 3D 布尔（并/差/交，实时预览）
        registerCnc(self._cm)           # CNC 轮廓铣/口袋/钻孔（复用 pymillcam）
        registerRevcloud(self._cm)      # 修订云线（曲线编辑类目）
        self._cm.register("Image", ImagePlaceCommand)   # 图片放置（参数：路径）
        self._cm.register("ImageTrace", ImageTraceCommand)   # 图片转线稿
        registerDims(self._cm)   # 尺寸标注命令
        self._view.setCommandManager(self._cm)


        self._statusBar = StatusBarWidget()
        self.setStatusBar(self._statusBar)

        self._topDock = TopDock()
        # 命令行历史移除后，原 appendHistory 的 41 处反馈（含"G 代码生成
        # 失败"等必须可见的错误）接到 StepHint，不再静默丢弃
        self._topDock.setHistoryHandler(self._notifyTip)
        # Win11 无边框窗口：自绘标题栏，分类标签（曲线绘制/曲线编辑…）
        # 从工具区上移嵌入标题栏，节约一行垂直空间；切换逻辑不变
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self._preMaxGeo = None   # 最大化前几何（还原时恢复）
        self._navTabBar = self._topDock.detachNavBar()
        self._titleBar = TitleBar(self, self._navTabBar)
        # 标题栏工具按钮：成本计算器 / 设置（原在左侧竖条，已上移）
        self._titleBar.calcRequested.connect(self._onCalcCost)
        self._titleBar.settingsRequested.connect(self._onShowSettings)
        # 无边框窗口无系统边框：全局事件过滤实现边缘按住拖拽缩放
        QCoreApplication.instance().installEventFilter(self)
        self._leftPanel = LeftPanelWidget()
        self._layerPanel = LayerPanel()
        self._propertyPanel = PropertyPanel()
        self._transformPanel = TransformPanel()
        self._transformPanel.setDocument(self._doc)
        self._leftPanel.setCncDocument(self._doc)
        self._captureToolbar = CaptureToolbar()

        self._buildDockLayout()
        self._setupGizmo()
        self._connectSignals()

        self._view.zoomToFit()
        self._statusBar.setLengthArea(0.0, 0.0)
        self._updateFileTabs()   # 初始标签：未命名1

        # 自动保存定时器（设置-文件页开启后按间隔把未保存文档另存恢复副本）
        self._autosaveTimer = QTimer(self)
        self._autosaveTimer.timeout.connect(self._onAutosaveTimeout)
        self._restartAutosaveTimer()

        # 开放平台：API 门面 + 插件装载（UI 就绪后挂宿主，插件按钮/命令即刻生效）
        self._api = CangaiAPI()
        self._mainThreadInvoker = _MainThreadInvoker(self)
        self._plugins = PluginManager(self._api)
        self._api.attachHost(self)
        self._plugins.discover()
        self._plugins.loadAll()
        self._api.events.emit("app.ready", {
            "plugins": [p.pluginId for p in self._plugins.infos() if p.loaded]})
        # 语言变更：级联重绘所有子组件文本
        languageChanged().connect(self.retranslateUi)
        # 注意：重模块预热不在此自动启动，由启动加载页调用
        # beginStartupPreload()，全部就绪后才显示主窗口。

    def beginStartupPreload(self) -> None:
        """启动加载页调用：全量预热重模块，完成后发 startupFinished。

        加载页先于主窗口显示，用户此时无法操作；预热期间主线程只负责
        刷新加载页进度，避免"启动后直接使用、模块尚未加载导致卡死"。
        """
        self._preloadHeavyModules()

    def _preloadHeavyModules(self) -> None:
        """全量预热 3D/绘图相关模块（启动加载页期间执行）。

        后台线程按依赖序分步导入，每步之间 time.sleep 让出 GIL，并
        通过 startupProgress 汇报进度。注意 CPython 导入本身持 GIL
        （单个大库如 OCP 一次 2~4s），加载页在单步导入期间可能短暂
        停顿，但主窗口尚未显示，不会出现可操作的卡死界面。
        """
        if getattr(self, "_heavyPreloading", False):
            return
        self._heavyPreloading = True
        from PySide6.QtCore import QThread

        class _HeavyPreloader(QThread):
            def run(self):
                import os as _os
                import time
                w = self._win

                def step(pct, text):
                    try:
                        w.startupProgress.emit(int(pct), text)
                    except Exception:
                        pass
                    time.sleep(0.01)

                try:
                    # 3D 内核（build123d/OCP）与 OCCT 预热也在加载页内完成：
                    # 否则首次切到 3D 模式才后台导入，导入持 GIL，用户此时
                    # 在 3D 画图会被间歇性冻住（实测直接表现为“卡死”）。
                    # 放进加载页：卡顿发生在主窗口出现之前，用户看不到。
                    step(10, "正在加载字体与文字模块…")
                    try:
                        from core.text.font import ttf_reader
                        ttf_reader._loadFontTools()
                        from core.text.font import FontManager
                        _sgl = resource_path(
                            "resources/fonts/细圆单线.sgl")
                        if _os.path.isfile(_sgl):
                            FontManager.instance().get(_sgl)
                    except Exception:
                        pass
                    # 本项目几何薄层（顶层不触发 build123d 导入）
                    step(28, "正在加载几何计算模块…")
                    import core.vector.thread_generator  # noqa: F401
                    import core.vector.brep_object       # noqa: F401
                    step(40, "正在加载 3D 建模内核（build123d / OCCT）…")
                    w._bdKernelLoading = True
                    try:
                        w._load3DKernelBlocking()
                        w._bdKernelLoaded = True
                    finally:
                        w._bdKernelLoading = False
                    # 2D/CNC/样条路径重库（多数已随主窗口模块导入缓存，近乎零耗时）
                    step(82, "正在加载二维轮廓与 CNC 模块…")
                    for mod in ("shapely", "pyclipper", "ezdxf",
                                "scipy.interpolate"):
                        try:
                            __import__(mod)
                        except Exception:
                            pass
                    step(96, "正在完成最后的准备工作…")
                except Exception:
                    import traceback
                    traceback.print_exc()
                finally:
                    w._heavyPreloading = False
                    w._heavyPreloaded = True
                    try:
                        w.startupProgress.emit(100, "加载完成")
                        w.startupFinished.emit()
                    except Exception:
                        pass

        loader = _HeavyPreloader()
        loader._win = self
        self._heavyLoader = loader
        loader.start()

    def retranslateUi(self) -> None:
        """语言切换时重设主窗口及所有子组件可见文本。"""
        self.setWindowTitle(tr("Cangai 浮雕设计"))
        self._centerDock.setWindowTitle(tr("画布"))
        self._leftDock.setWindowTitle(tr("文件"))
        self._transformDock.setWindowTitle(tr("变换"))
        self._layerDock.setWindowTitle(tr("图层"))
        self._propDock.setWindowTitle(tr("属性"))
        # 级联调用各子组件
        for comp in (self._topDock, self._statusBar, self._leftPanel,
                     self._layerPanel, self._transformPanel,
                     self._captureToolbar, self._viewSwitchBar,
                     self._canvasModeBar, self._fileTabBar):
            if hasattr(comp, "retranslateUi"):
                comp.retranslateUi()
        # 未命名文档标题随语言刷新（未命名1 ↔ Untitled1）
        import re
        for d in self._openDocs:
            m = re.fullmatch(r"未命名(\d+)", d.title()) or \
                re.fullmatch(r"Untitled(\d+)", d.title())
            if m:
                d.setTitle(f"{tr('未命名')}{m.group(1)}")
        # 刷新窗口标题（含文档名拼接）
        self._updateFileTabs()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not getattr(self, "_dwmRounded", False):
            self._dwmRounded = True
            self._applyDwmRoundCorners()
            self._enableTaskbarMinMax()
        if not getattr(self, "_dockAdjusted", False):
            self._dockAdjusted = True
            QTimer.singleShot(0, self._adjustDockHeights)
            # 注意：不在这里后台预热 build123d/OCC 与 sgl 字体——
            # CPython 导入持 GIL，后台线程导入期间主线程照样冻结
            # （实测 OCP 最长卡 1.7s、sgl 字体持续 220ms×24），
            # 启动"卡十来秒"即来源于此。改由首次进入 3D 模式时
            # 同步加载内核（一次 3~4s，带等待光标），首次文字命令
            # 时加载单线字体，保证启动即丝滑。

    def _preloadFontTools(self) -> None:
        """后台线程预热 fontTools 与内置单线字体，避免首次文字命令卡顿。"""
        from PySide6.QtCore import QThread

        class _Preloader(QThread):
            def run(self):
                try:
                    from core.text.font import ttf_reader
                    ttf_reader._loadFontTools()
                    from core.text.font import FontManager
                    import os
                    here = os.path.dirname(os.path.abspath(ttf_reader.__file__))
                    for _ in range(3):
                        here = os.path.dirname(here)
                    sgl = os.path.join(here, "resources", "fonts", "细圆单线.sgl")
                    if os.path.isfile(sgl):
                        FontManager.instance().get(sgl)
                except Exception:
                    pass

        self._preloader = _Preloader()
        self._preloader.start()

    def _preloadBuild123d(self) -> None:
        """后台线程预热 build123d/OCC：首次导入约 2.6-4.7s，提前到
        启动空闲期完成，避免首次挤出命令一次性等待。线程失败静默
        （主线程命令处仍会兜底同步导入）。"""
        from PySide6.QtCore import QThread

        class _Preloader(QThread):
            def run(self):
                try:
                    from core.vector.brep_shape import _import_bd
                    _import_bd()
                except Exception:
                    pass

        self._bdPreloader = _Preloader()
        self._bdPreloader.start()

    def _adjustDockHeights(self) -> None:
        self._leftPanel.setExpandedWidth(240)
        self._resizeLeftDock(self._leftPanel.isExpanded())
        self._narrowRightDockArea(252)
        self._positionViewSwitchBar()

    def _narrowRightDockArea(self, width: int = 215) -> None:
        """启动时把右侧面板区（变换/图层/属性）宽度设为指定值，
        差值与画布区互补（面板内容过窄时自身出滚动条，不影响可用性）。
        delta 支持负值：自然宽度小于目标时从画布区让宽，保证精确到位。"""
        area = self._transformDock.dockAreaWidget()
        if area is None:
            return
        # transform 区的直接父 splitter 是右区竖向分割（变换/图层上下堆叠），
        # 需要向上找到横向根 splitter，钳制其中右区所占的宽度
        splitter = area.parentSplitter()
        while splitter is not None and not (isinstance(splitter, QSplitter)
                                            and splitter.orientation() == Qt.Horizontal):
            splitter = splitter.parentWidget()
        if splitter is None:
            return
        node = area
        idx = -1
        while node is not None:
            idx = splitter.indexOf(node)
            if idx >= 0:
                break
            node = node.parentWidget()
        if idx < 0:
            return
        sizes = splitter.sizes()
        if len(sizes) < 2 or sizes[idx] == width:
            return
        delta = sizes[idx] - width   # 正=右区让给画布区；负=画布区让给右区
        sizes[idx] = width
        center = self._centerDock.dockAreaWidget()
        cidx = splitter.indexOf(center) if center is not None else -1
        if 0 <= cidx < len(sizes) and cidx != idx:
            sizes[cidx] += delta
        else:
            others = [i for i in range(len(sizes)) if i != idx and sizes[i] > 0]
            if others:
                sizes[others[-1]] += delta
        splitter.setSizes(sizes)


    def _onLeftPanelExpanded(self, expanded: bool) -> None:
        QTimer.singleShot(0, lambda: self._resizeLeftDock(expanded))

    def _resizeLeftDock(self, expanded: bool) -> None:
        """展开/收起时同步左侧停靠区宽度（竖排标签栏宽度 / 展开宽度）。"""
        area = self._leftDock.dockAreaWidget()
        if area is None:
            return
        splitter = area.parentSplitter()
        if splitter is None:
            return
        index = splitter.indexOf(area)
        if index < 0:
            return
        sizes = splitter.sizes()
        if len(sizes) < 2:
            return
        tabWidth = self._leftPanel.tabBarWidth()
        if expanded:
            target = max(self._leftPanel.expandedWidth(), tabWidth)
            self._leftDock.setMinimumSize(target, 100)
        else:
            # 仅当收起前确实处于展开宽度（≥ 展开最小宽）才记忆；
            # 启动即收起时左栏只有标签栏宽，不能覆盖预置的展开宽度
            if sizes[index] >= tabWidth + self._leftPanel.STACK_MIN_WIDTH:
                self._leftPanel.setExpandedWidth(sizes[index])
            target = tabWidth
            self._leftDock.setMinimumSize(tabWidth, 100)

        # 保持分隔器总宽不变，差值在其余分区间按权重分摊
        delta = target - sizes[index]
        others = [i for i in range(len(sizes)) if i != index]
        rest = sum(sizes[i] for i in others)
        if delta and rest > 0:
            taken = 0
            for i in others:
                share = delta * sizes[i] // rest
                sizes[i] -= share
                taken += share
            sizes[others[-1]] -= delta - taken
        sizes[index] = target
        splitter.setSizes(sizes)


    # ---------------- 开放平台：API 宿主钩子（协议见 core/api.py） ----------------

    def registerActionCommand(self, name: str, fn, meta: dict) -> None:
        """插件瞬时动作 → 包装成命令进入命令系统（撤销分组/历史/重复执行）。"""
        self._cm.register(name, lambda: PluginActionCommand(name, fn, meta), meta)

    def registerInteractiveCommand(self, name: str, factory, meta: dict) -> None:
        self._cm.register(name, factory, meta)

    def unregisterCommand(self, name: str) -> None:
        self._cm.unregister(name)

    def commandMetaList(self) -> list:
        """全部内置+插件命令的元数据清单（API 命令清单数据源）。"""
        return self._cm.listCommands()

    def addToolButton(self, spec) -> None:
        self._topDock.addToolButton(spec.group, spec.key, spec.label,
                                    spec.tooltip, spec.icon,
                                    pluginId=spec.pluginId,
                                    divider=bool(spec.meta.get("divider")))

    def removeToolButtons(self, keys) -> int:
        return self._topDock.removeToolButtons(keys)

    def notify(self, text: str) -> None:
        self._cm.notifyResult(text)

    def setStatus(self, text: str, timeoutMs: int = 4000) -> None:
        self._statusBar.showMessage(text, timeoutMs)

    def appendHistory(self, text: str) -> None:
        self._topDock.appendHistory(text)

    def executeCommand(self, name: str, *args) -> bool:
        if self._cm.hasActive():
            return False
        ok = self._cm.executeCommand(name, *args)
        if ok:
            self._topDock.appendHistory(f"执行: {name}")
        return ok

    def cancelActiveCommand(self) -> None:
        if self._cm.hasActive():
            self._cm.cancelActive()

    def activeDocument(self):
        return self._doc

    def documents(self) -> list:
        return list(self._openDocs)

    def openDocument(self, path: str):
        self._newDocument(path)
        return self._doc

    def newDocument(self):
        self._newDocument()
        return self._doc

    def wireApiEvents(self, bus) -> None:
        """把命令生命周期与当前文档信号接入事件总线（文档切换时经
        relayDocumentEvents 重绑）。"""
        self._cm.commandFinished.connect(
            lambda name: bus.emit("command.finished", name))
        self._cm.activeCommandChanged.connect(
            lambda name: bus.emit("command.started" if name else "command.ended", name))
        self._api.relayDocumentEvents(self._doc)

    def runOnMainThread(self, fn) -> None:
        self._mainThreadInvoker.post(fn)

    def _setupGizmo(self) -> None:
        """操作轴（复刻原项目）：选中对象后显示，拖动移动/旋转/缩放；
        底部“操作轴”按钮控制全局开关（2D 画布与 3D/浮雕视口共用同一管理器）。"""
        self._gizmo = GizmoManager(self._doc, self._cm, self)
        ad2d = View2DAdapter(self._view)
        ad3d = View3DAdapter(self._viewport3D)
        self._gizmo.attachView(ad2d)
        self._gizmo.attachView(ad3d)
        self._view.setGizmo(self._gizmo, ad2d)
        self._viewport3D.setGizmo(self._gizmo, ad3d)
        # 曲线编辑：底部“编辑曲线”开关驱动；控制点选择变化时刷新操作轴基准
        self._curveEdit = CurveEditController(self._doc, self)
        self._curveEdit.attach(ad2d)
        self._curveEdit.attach(ad3d)
        self._view.setCurveEdit(self._curveEdit, ad2d)
        self._viewport3D.setCurveEdit(self._curveEdit, ad3d)
        self._gizmo.setCurveEdit(self._curveEdit)
        self._curveEdit.changed.connect(self._gizmo.refresh)
        self._statusBar._btnGizmo.toggled.connect(self._gizmo.setEnabled)
        self._gizmo.setEnabled(self._statusBar._btnGizmo.isChecked())
        # 编辑曲线开关状态决定是否显示控制点（默认关闭，由状态栏按钮决定）
        self._curveEdit.setEnabled(self._statusBar.isEditCurveChecked())

    def _buildDockLayout(self) -> None:
        self._dockManager = CDockManager()
        self._dockManager.setStyleSheet(theme.dockManagerQss())

        container = QWidget()
        cl = QVBoxLayout(container)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)

        cl.addWidget(self._topDock)
        cl.addWidget(self._dockManager)

        # 顶部自绘标题栏（含分类标签）+ 原工具区/停靠区
        outer = QWidget()
        ol = QVBoxLayout(outer)
        ol.setContentsMargins(0, 0, 0, 0)
        ol.setSpacing(0)
        ol.addWidget(self._titleBar)
        ol.addWidget(container)
        self.setCentralWidget(outer)

        self._view.setMinimumSize(400, 300)
        self._viewport3D = Viewport3D()
        # 3D/浮雕视口也支持命令取点（默认画在 XY 平面）：接入文档与命令调度，
        # 对象与命令预览由视口自行投影绘制；对象变化信号驱动重绘已在视口内连接
        self._viewport3D.setDocument(self._doc)
        self._viewport3D.setCommandManager(self._cm)
        # 活动画布视口探测：命令判断视图方向（3D 前视/右视侧视平面绘制）时
        # 查询 Viewport3D 的 canvasMode/viewMode；上下文 _view 仍是 2D 画布
        self._cm.setViewProbe(self._viewport3D)
        # 浮雕模式全局操作（空格填色）的状态反馈接入历史栏
        self._viewport3D.statusMessage.connect(self._topDock.appendHistory)
        # 浮雕模式鼠标坐标接入底部状态栏（Z = 鼠标处模型高度）
        self._viewport3D.mouseMoved.connect(self._onMouseMoved)
        self._active3D = False
        canvasContainer = QWidget()
        canvasLayout = QVBoxLayout(canvasContainer)
        canvasLayout.setContentsMargins(0, 0, 0, 0)
        canvasLayout.setSpacing(0)
        self._viewStack = QStackedWidget()
        self._viewStack.addWidget(self._view)          # 0: 2D 画布
        self._viewStack.addWidget(self._viewport3D)    # 1: 3D/浮雕视口
        # 实体模拟面板（左侧竖条：上方 G 代码、下方播放条；点击"开始模拟"后显示）
        from ui.widgets.cnc_sim_panel import CncSimulationPanel
        self._cncSimPanel = CncSimulationPanel(canvasContainer)
        self._cncSimPanel.hide()
        bodyRow = QHBoxLayout()
        bodyRow.setContentsMargins(0, 0, 0, 0)
        bodyRow.setSpacing(4)
        bodyRow.addWidget(self._cncSimPanel)
        bodyRow.addWidget(self._viewStack, 1)
        canvasLayout.addLayout(bodyRow, 1)
        self._canvasContainer = canvasContainer
        self._viewSwitchBar = ViewSwitchBar(canvasContainer)
        self._canvasModeBar = CanvasModeBar(canvasContainer)
        self._paramHost = CommandParamHost(canvasContainer)
        self._paramHost.hide()
        # 多文件标签栏：画布右下角悬浮（切换/关闭/“＋”新建）
        self._fileTabBar = FileTabBar(canvasContainer)
        # 悬浮面板先隐藏，待 _positionViewSwitchBar 首次定位后再显示，
        # 避免主窗口出现时它们在画布左上角闪现一帧。
        self._viewSwitchBar.hide()
        self._canvasModeBar.hide()
        self._fileTabBar.hide()
        self._layerPanel.setDocument(self._doc)
        # 视图切换栏（4 方向 + 全观察）：2D 与 3D/浮雕"各自独立"。
        # 左栏只有 1 条，所以为每条模式分组各自保存 (current, locked)：
        #   - "2d" 分组：2D 画布 + CNC 占位 → 固定顶视 1 + 锁定（用户要求 2D 锁顶视图，
        #     不允许方向栏切到前/右/透视；缩放与平移仍自由保留）
        #   - "3d" 分组：3D 预览 + 浮雕编辑态 → 默认透视 0 + 不锁定（可自由切视角）
        self._vswSaved: dict[str, tuple[int, bool]] = {
            "2d": (1, True),
            "3d": (0, False),
            "cnc": (0, False),
        }
        # 画布模式上一次切换"前"的模式。必须手动记录，因为 CanvasModeBar 在发出
        # modeChanged 信号前就已经把 self._current 更新为新模式：若直接在回调内
        # 用 bar.current() 当"旧模式"存 saved，会把 2D 的 (idx=1,locked=True) 写
        # 进 saved["3d"]，造成“切到 3D/浮雕 左栏仍只有顶视且被锁定”。
        self._lastCanvasMode: str = "2d"
        # 启动时默认处于"2D"模式：① 左栏方向固定顶视+锁定；② 实际 2D 视口方向也强
        # 制设为顶视（setViewMode(1)，不调用 zoomToFit，保留后续缩放/平移）。
        self._viewSwitchBar.setCurrent(1)
        self._viewSwitchBar.setLocked(True)
        self._view.setViewMode(1)
        canvasContainer.installEventFilter(self)

        centerDock = CDockWidget(tr("画布"))
        centerDock.setWidget(canvasContainer)
        self._dockManager.addDockWidget(CenterDockWidgetArea, centerDock)
        centerArea = centerDock.dockAreaWidget()
        if centerArea is not None:
            centerArea.setDockAreaFlag(CDockAreaWidget.HideSingleWidgetTitleBar, True)
            centerArea.setContentsMargins(0, 0, 0, 0)

        self._leftPanel.setMinimumHeight(200)
        self._leftDock = CDockWidget(tr("文件"))
        self._leftDock.setWidget(self._leftPanel)
        self._leftDock.setMinimumSizeHintMode(
            CDockWidget.MinimumSizeHintFromDockWidgetMinimumSize)
        self._leftDock.setMinimumSize(self._leftPanel.tabBarWidth(), 100)
        self._dockManager.addDockWidget(LeftDockWidgetArea, self._leftDock)
        leftArea = self._leftDock.dockAreaWidget()
        if leftArea is not None:
            leftArea.setDockAreaFlag(CDockAreaWidget.HideSingleWidgetTitleBar, True)
            leftArea.setContentsMargins(0, 0, 0, 0)
        # 面板收起（仅竖排标签栏）时内容比视口略宽，会冒出水平滚动条，这里禁用它
        for sa in self._leftDock.findChildren(QScrollArea):
            sa.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        F = CDockWidget.DockWidgetFeature
        self._transformPanel.setMinimumWidth(240)
        self._transformPanel.setMinimumHeight(150)
        transformDock = CDockWidget(tr("变换"))
        transformDock.setWidget(self._transformPanel)
        transformDock.setFeature(F.DockWidgetClosable, False)
        self._dockManager.addDockWidget(RightDockWidgetArea, transformDock)

        self._layerPanel.setMinimumWidth(240)
        self._layerPanel.setMinimumHeight(150)
        layerDock = CDockWidget(tr("图层"))
        layerDock.setWidget(self._layerPanel)
        layerDock.setFeature(F.DockWidgetClosable, False)
        # 右侧区域上下堆叠：变换在上，图层停靠在其下方
        self._dockManager.addDockWidget(BottomDockWidgetArea, layerDock,
                                        transformDock.dockAreaWidget())

        self._propertyPanel.setMinimumWidth(240)
        self._propertyPanel.setMinimumHeight(150)
        propDock = CDockWidget(tr("属性"))
        propDock.setWidget(self._propertyPanel)
        propDock.setFeature(F.DockWidgetClosable, False)
        self._propDock = propDock
        self._dockManager.addDockWidgetTabToArea(propDock, layerDock.dockAreaWidget())
        layerDock.dockAreaWidget().setCurrentIndex(0)

        # 初始高度比例：变换在上、图层（含属性 tab）在下，图层面板初始高度加一些
        layerArea = layerDock.dockAreaWidget()
        splitter = layerArea.parentSplitter() if layerArea else None
        if splitter is not None:
            splitter.setSizes([280, 180])

        # 保存 dock 引用以便 retranslateUi 重设标题
        self._centerDock = centerDock
        self._transformDock = transformDock
        self._layerDock = layerDock



        self.addToolBar(Qt.BottomToolBarArea, self._captureToolbar)

    _CNC_STEP_TO_COMMAND = {
        "machine": "CncMachine",
        "stock": "CncStock",
        "tools": "CncTools",
        "toolpath": "CncProfile",
        "collision": "CncCollision",
        "simulate": "CncSimulate",
        "output": "CncOutput",
    }

    def _onCncStepRequested(self, step: str) -> None:
        """向导面板点击步骤 → 路由到对应 CNC 命令。"""
        if step == "simulate":
            self._onCncSimulateRequested()
            return
        cmd = self._CNC_STEP_TO_COMMAND.get(step)
        if cmd is None:
            return
        self._onCommandRequested(cmd)

    def _onCncSimulateRequested(self) -> None:
        """开始模拟：只模拟实体模拟步骤勾选的路径 → 生成 G 代码 → 面板播放。"""
        if self._cm.hasActive():
            self._cm.cancelActive()
        from core.cnc.state import CncState
        state = CncState.instance()
        active = [e for e in state.pathEntries if e.simEnabled and e.toolpaths]
        if not active:
            self._topDock.appendHistory("请先勾选要模拟的路径")
            return
        paths = []
        for e in active:
            paths.extend(e.toolpaths)
        from pymillcam.post import get_post
        try:
            post = get_post(state.machine.controller)
            state.gcode = post.post_program(paths, macros=state.machine.macros)
        except Exception as ex:
            self._topDock.appendHistory(f"G 代码生成失败: {ex}")
            return
        state.simLines = state.gcode.splitlines()
        state.simulationDone = True
        state.notifyChanged()
        # 模拟面板显示时隐藏文档标签栏，避免右下角悬浮标签遮挡
        self._fileTabBar.hide()
        self._cncSimPanel.showPanel(state.simLines)
        self._cncSimPanel.play()
        self._topDock.appendHistory("开始实体模拟播放")

    def _onCncSimPanelClosed(self) -> None:
        """模拟面板关闭：隐藏面板，恢复被隐藏的悬浮控件（文档标签栏等）。"""
        self._cncSimPanel.hide()
        # 恢复模拟开始时被隐藏的文档标签栏，避免关闭后无法切换/新建文档
        if hasattr(self, "_fileTabBar") and not self._fileTabBar.isVisible():
            self._fileTabBar.show()
        self._positionViewSwitchBar()

    def _onCncEditPathRequested(self, entry) -> None:
        """单击路径标题 → 打开编辑对话框（回填参数），确定后更新该条目。"""
        if self._cm.hasActive():
            self._cm.cancelActive()
        from PySide6.QtWidgets import QDialog
        from ui.widgets.cnc_dialogs import CncToolpathDialog
        dlg = CncToolpathDialog(self._doc, self._view, entry=entry)
        if dlg.exec() == QDialog.Accepted:
            self._topDock.appendHistory(f"已更新路径: {entry.title}")

    def _connectSignals(self) -> None:
        self._topDock.commandRequested.connect(self._onCommandRequested)
        self._cm.commandRepeated.connect(
            lambda name: self._topDock.appendHistory(f"右键重复命令: {name}"))
        self._view.mouseMoved.connect(self._onMouseMoved)
        # 画布 / 3D 视口直接拖入文件（路径列表 + 落点世界坐标）
        self._view.fileDropped.connect(self._onCanvasFileDrop)
        self._viewport3D.fileDropped.connect(self._onCanvasFileDrop)
        self._captureToolbar.snapTypeToggled.connect(self._onSnapToggled)
        self._captureToolbar.clearColorsRequested.connect(self._onClearColors)
        self._captureToolbar.autoColorSeparateRequested.connect(
            self._onAutoColorSeparate)
        self._statusBar.orthoToggled.connect(self._onOrthoToggled)
        GridManager.instance().setOrthoEnabled(self._statusBar.isOrthoChecked())
        self._statusBar.editCurveToggled.connect(self._onEditCurveToggled)
        quickBar = self._topDock.quickBar()
        # 两套相互独立的撤销/重做：
        #   顶部 撤销/重做（2 个按钮）      → 曲线历史（undoCurve/redoCurve）；
        #   浮雕组 浮雕撤销/浮雕重做（2 个）→ 浮雕历史（undoRelief/redoRelief）。
        # 撤销/重做作用于当前活动文档（多文件切换后 self._doc 变更），用中转。
        quickBar.undoRequested.connect(lambda: self._doc.undoCurve())
        quickBar.redoRequested.connect(lambda: self._doc.redoCurve())
        quickBar.reliefUndoRequested.connect(lambda: self._doc.undoRelief())
        quickBar.reliefRedoRequested.connect(lambda: self._doc.redoRelief())
        # 固化/流化（精雕流动层概念）：固化按钮左键=固化、右键=流化
        quickBar.cureRequested.connect(self._onCureBase)
        quickBar.flowRequested.connect(self._onFlowToBase)
        # 文档侧信号（历史可用状态/选择/对象/图层/未保存）随文档切换重绑
        self._bindDocSignals(quickBar)
        self._statusBar.lineWidthChanged.connect(self._onLineWidthChanged)
        self._statusBar.measureToggled.connect(self._updateMeasure)
        self._leftPanel.expandedChanged.connect(self._onLeftPanelExpanded)
        self._leftPanel.cncStepRequested.connect(self._onCncStepRequested)
        self._leftPanel.cncEditPathRequested.connect(self._onCncEditPathRequested)
        self._cncSimPanel.closeRequested.connect(self._onCncSimPanelClosed)

        self._leftPanel.settingsRequested.connect(self._onShowSettings)
        self._leftPanel.importRequested.connect(self._onImportFile)
        self._leftPanel.openRequested.connect(self._onOpenFile)
        self._leftPanel.saveRequested.connect(self._onSaveFile)
        self._leftPanel.saveAsRequested.connect(self._onSaveFileAs)
        self._leftPanel.quickSaveRequested.connect(self._onQuickSave)
        self._leftPanel.exportRequested.connect(self._onExport)
        self._leftPanel.calcRequested.connect(self._onCalcCost)

        # 多文件标签栏：切换 / 关闭 / ＋新建
        self._fileTabBar.currentChanged.connect(self._switchToDocument)
        self._fileTabBar.renameRequested.connect(self._renameDocument)
        self._fileTabBar.closeRequested.connect(self._closeDocument)
        self._fileTabBar.newRequested.connect(lambda: self._newDocument())
        self._transformPanel.mirrorRequested.connect(self._onMirrorRequested)
        self._transformPanel.zHeightCommitRequested.connect(self._onModelZCommit)
        self._transformPanel.zClipRequested.connect(self._onModelZClip)
        self._transformPanel.dimensionRequested.connect(self._onDimensionRequested)
        self._transformPanel.dimensionTextBiggerRequested.connect(
            lambda: self._onDimensionTextResize(1.25))
        self._transformPanel.dimensionTextSmallerRequested.connect(
            lambda: self._onDimensionTextResize(1.0 / 1.25))
        self._transformPanel.dimensionBindToggled.connect(
            self._onDimensionBindToggled)
        self._transformPanel.moveRequested.connect(
            lambda: self._onPanelXform("PanelMove"))
        self._transformPanel.rotateRequested.connect(
            lambda: self._onPanelXform("PanelRotate"))
        self._transformPanel.scaleRequested.connect(
            lambda: self._onPanelXform("PanelScale"))
        self._transformPanel.measureRequested.connect(self._onMeasure)
        self._transformPanel.solidDisplayModeRequested.connect(
            self._onSolidDisplayModeRequested)
        self._transformPanel.solidPerspectiveRequested.connect(
            self._onSolidPerspectiveRequested)
        self._transformPanel.meshHeightColorRequested.connect(
            self._onMeshHeightColorRequested)
        self._transformPanel.solidLightRequested.connect(
            self._onSolidLightRequested)


        self._viewSwitchBar.fitAllClicked.connect(self._onFitAll)
        self._viewSwitchBar.viewChanged.connect(self._onViewChanged)
        self._viewSwitchBar.paramPanelClicked.connect(self._onParamBtnClicked)
        # 图片按钮：选图后进入“两点框放”命令
        self._viewSwitchBar.imageSelected.connect(self._onImageSelected)
        self._paramHost.closed.connect(self._paramHost.hide)
        self._paramHost.resized.connect(self._positionParamHost)
        self._cm.activeCommandChanged.connect(self._onActiveCommandChanged)
        self._canvasModeBar.modeChanged.connect(self._onCanvasModeChanged)
        # 命令模式门禁：CommandManager 中央兜底（右键重复/AI API 等旁路入口）
        # + 顶部按钮按模式置灰（矩阵见 ui/commands/mode_policy.py）
        self._cm.setModeProbe(self._canvasModeBar.current)
        self._topDock.setCanvasMode(self._canvasModeBar.current())

        self._syncReliefButtons()   # 初始（2D 模式）禁用浮雕按钮

        # Tab 快捷键：循环切换模型显示模式
        #   2D 模式 → 等高线 ↔ 灰度图
        #   浮雕模式 → 地图 ↔ 颜色（仅这两者循环，不含渲染）
        # 用 QShortcut 全局捕获：QApplication 默认会用 Tab 切换控件焦点，
        # keyPressEvent 路径不可靠（焦点在子控件上时收不到），QShortcut
        # 在主窗口层级监听，不依赖焦点位置，激活窗口内随时响应。
        self._tabShortcut = QShortcut(QKeySequence(Qt.Key_Tab), self)
        self._tabShortcut.activated.connect(self._onCycleDisplayMode)

    def _onCycleDisplayMode(self) -> None:
        """Tab 快捷键：按当前画布模式分派到 TransformPanel 的循环切换。

        浮雕模式下切换显示模式是查看操作，与命令激活与否无关，始终允许；
        2D 模式下命令激活期间不响应（避免干扰画线/缩放数字输入等命令）；
        3D 模式下循环切换实体显示模式（线框 ↔ 渲染），始终允许。
        """
        mode = self._canvasModeBar.current()
        if mode == "relief":
            self._transformPanel.cycleDisplay3DMode()
        elif mode == "2d":
            if self._cm.hasActive():
                return
            self._transformPanel.cycleDisplayMode()
        elif mode == "3d":
            if self._viewport3D is not None:
                self._viewport3D.cycleSolidDisplayMode()
                self._transformPanel.setSolidDisplayMode(
                    self._viewport3D.solidDisplayMode())

    def _onSolidDisplayModeRequested(self, mode: str) -> None:
        """变换面板 3D 实体显示模式单选按钮点击：切换视口显示模式。"""
        if self._viewport3D is not None:
            self._viewport3D.setSolidDisplayMode(mode)

    def _onSolidPerspectiveRequested(self, on: bool) -> None:
        """变换面板 3D 线框透视开关点击：切换视口透视状态。"""
        if self._viewport3D is not None:
            self._viewport3D.setSolidPerspective(on)

    def _onMeshHeightColorRequested(self, on: bool) -> None:
        """变换面板"高度色"开关点击：网格 STL 实体按高度地形色显示。"""
        if self._viewport3D is not None:
            self._viewport3D.setMeshHeightColor(on)

    def _onSolidLightRequested(self, az: int, alt: int) -> None:
        """变换面板光影滑块：调整 3D 实体光照方向（阴影随之变化）。"""
        if self._viewport3D is not None:
            self._viewport3D.setSolidLight(az, alt)

    def _onFitAll(self) -> None:
        if self._active3D:
            if self._canvasModeBar.current() == "relief":
                # 浮雕模式：适配到当前编辑模型的包围盒
                # （resetView 只重置默认距离，不会缩放到模型）
                self._viewport3D.fitReliefModel()
            else:
                # 3D/CNC 模式：适配到全部可见实体的联合包围盒——
                # resetView 只回默认视角（网格原点），移出原点的模型
                # 看不见（用户实测"全观察只显示网格不显示模型"）
                bb = None
                if self._doc is not None:
                    from core.vector.brep_object import BRepObject
                    for o in self._doc.visibleObjects():
                        if not isinstance(o, BRepObject):
                            continue
                        try:
                            b = o.getBoundingBox()
                        except Exception:
                            continue
                        if not b.isValid():
                            continue
                        if bb is None:
                            bb = b
                        else:
                            bb.expand(b.min)
                            bb.expand(b.max)
                if bb is not None:
                    self._viewport3D.fitToBounds(bb)
                else:
                    self._viewport3D.resetView()
        else:
            self._view.zoomToFit()

    def _onActiveCommandChanged(self, name: str) -> None:
        """命令激活/结束：同步右侧参数面板，并联动视图与浮雕编辑态。

        - 需要 2D 画布交互的命令激活时切回 2D；
        - NewModel 命令结束后，新建的浮雕模型立即进入编辑态（锁定、脱离选中、
          绑定视口与变换面板、等高显示），符合“新建即编辑该模型”的语义。
        """
        prev = getattr(self, "_activeCmdName", None)
        self._activeCmdName = name
        inst = self._cm.activeInstance()
        panel = inst.paramPanel() if inst is not None else None
        if panel is not None:
            self._paramHost.setContentWidget(panel, inst.commandTitle())
            self._paramHost.show()
            self._positionViewSwitchBar()
        else:
            self._paramHost.clearContent()
            self._paramHost.hide()
        if not name:
            # 命令结束（name 为空字符串）：新建浮雕模型后，浮雕模式下进入编辑态。
            # 显示模式一律由变换面板控制，此处不再改写（模型自身默认即等高线）
            if prev == "NewModel":
                if self._canvasModeBar.current() == "relief":
                    # 重新选中曲线再新建模型时，需切换到新模型编辑：
                    # 先解锁旧锁定模型（不重新选中，新模型已是选中态），
                    # 这样 _enterReliefEdit 的 activeReliefModel 才会返回新建的选中模型，
                    # 旧模型随后在 _enterReliefEdit 里被隐藏。
                    old = self._reliefLockedModel
                    if old is not None:
                        old.setLocked(False)
                        self._reliefLockedModel = None
                    self._enterReliefEdit()

    def _onParamBtnClicked(self) -> None:
        """画布右侧“参数”按钮：切换参数面板显隐。"""
        if self._paramHost.isHidden():
            self._paramHost.show()
            self._positionViewSwitchBar()
        else:
            self._paramHost.hide()

    def _onViewChanged(self, idx: int) -> None:
        # 用户点击左侧方向栏（透视 / 顶 / 前 / 右）：当前视口切换视角并自动铺满。
        # 同时立即把 {当前画布模式分组} 下的 saved state 更新为 (idx, locked=False)，
        # 保证下次切画布模式时，2D 与 3D/浮雕"各自记得自己的方向"——不会混为一条。
        try:
            cur_mode = self._canvasModeBar.current()
            grp = self._vswGroup(cur_mode)
            self._vswSaved[grp] = (int(idx), bool(self._viewSwitchBar.isLocked()))
        except Exception:
            pass

        if self._active3D:
            self._viewport3D.setViewMode(idx)
        else:
            self._view.setViewMode(idx)
            self._view.zoomToFit()

    def _onCanvasModeChanged(self, mode: str) -> None:
        # 切换画布模式即结束仍在进行的交互命令（如浮雕取点/绘制取点）：
        # 命令取消后 _onActiveCommandChanged("") 会自动清空并收起参数面板，
        # 避免切到 2D 后浮雕命令面板仍残留在画布上。
        self._cm.cancelActive()

        # 左侧面板 tab 联动：CNC 模式切到"CNC向导"tab，其他模式切回"文件保存"tab
        try:
            self._leftPanel.setActiveTab(1 if mode == "cnc" else 0)
        except Exception:
            pass

        # --- 视图切换栏（左栏 4 方向）：各自独立保存 / 恢复 ---
        # 左栏只有 1 条，但 2D/3D 两类视口要"各自独立"：切模式前先把当前模式分组
        # 下的 (方向索引, 锁定) 快照保存；所有状态切换完成后再"恢复"目标模式分组
        # 下的 saved 快照。ViewSwitchBar.setCurrent/setLocked 都不发 viewChanged 信号，
        # 所以恢复动作不会改变 2D/3D 视口的实际相机角度——"原来是什么就是什么"。
        #
        # 注意：prev_mode 必须从我们自己记录的 _lastCanvasMode 取，不能用
        # canvasModeBar.current() —— 因为发 modeChanged 信号前 bar 已经更新了
        # _current，直接用会把"旧模式 saved"写成"新模式的值"，造成 3D/浮雕 分组
        # saved 被 2D 的 (idx=1, locked=True) 污染 → 左栏永远"只有顶视"还禁用。
        prev_mode = self._lastCanvasMode
        try:
            self._saveVSW(prev_mode)
        except Exception:
            pass

        # 变换面板浮雕显示区随画布模式切换 2D 预览组 / 浮雕(3D)组
        self._transformPanel.setCanvasMode(mode)
        if mode == "3d" and self._viewport3D is not None:
            self._transformPanel.setSolidDisplayMode(
                self._viewport3D.solidDisplayMode())
            self._transformPanel.setSolidPerspective(
                self._viewport3D.solidPerspective())

        # 空间隔离：3D 模式使用独立的 3D 曲线空间，CNC 模式使用独立的 CNC 空间，
        # 2D/浮雕共用 2D 空间；三者互不可见、数据完全独立。切换时选中集清空。
        self._doc.setSpace({"3d": "3d", "cnc": "cnc"}.get(mode, "2d"))
        # CNC 刀路叠加层仅 CNC 模式在 2D 画布上显示（2D/浮雕/3D 模式保持清空）
        try:
            self._cncOverlay.setEnabled(mode == "cnc")
        except Exception:
            pass

        # 关键：切换模式时 **不改变** 当前激活视口的相机/视角/缩放/平移 ——
        # "原来是什么就是什么"。仅切换 QStackedWidget 显示页、以及模式关联的
        # 浮雕锁定/解锁、命令按钮可用性、网格显示开关、历史提示等状态。
        #
        # 已移除：resetView / setViewMode / viewSwitchBar.setCurrent(...)
        #         viewSwitchBar.setLocked(...) / view.setViewMode(...) / zoomToFit
        # 否则每次切换都会把用户在 2D 上的自由缩放 / 在 3D 上的旋转角度归零。
        if mode == "3d":
            # 3D 画布：三维视口（与浮雕共享 viewport3D）。独立空间：
            # 只显示 3D 空间的对象（三维曲线 + 挤出实体），不显示 2D/浮雕内容
            self._exitReliefLock()
            self._active3D = True
            # 3D 内核（build123d/OCP）首次进入时同步加载：启动不预导入，
            # 避免启动卡顿；此处一次性导入约 3~4s，之后 import 缓存零开销。
            self._ensure3DKernel()
            self._viewport3D.setCanvasMode("3d")
            self._viewport3D.setHideReferenceGrid(False)   # 3D 模式显示参考网格（地面/坐标轴定位辅助）
            self._viewStack.setCurrentWidget(self._viewport3D)
            self._topDock.appendHistory("3D 模式：三维画布（挤出实体 / 三维曲线）")
        elif mode == "relief":
            # 浮雕：编辑态（锁定放置变换，视图可自由旋转翻面）
            self._active3D = True
            self._viewport3D.setCanvasMode("relief")
            self._viewport3D.setHideReferenceGrid(True)   # 浮雕模式隐藏地面/网格线，专注曲面本身
            self._viewStack.setCurrentWidget(self._viewport3D)
            self._enterReliefEdit()
            # 用户要求：进入"浮雕模式"默认显示【地图(高度色带)】。
            # 若锁定到了一个浮雕模型，就强制它的 display3DMode = "map"（色带 terrain LUT），
            # 并 emit objectChanged + 重新 setReliefModel，让右侧变换面板 3D 组的"地图"
            # 单选按钮也同步高亮；按 Tab 就是"地图↔颜色"二循环（不绕渲染）。
            locked = self._reliefLockedModel
            if locked is not None:
                if locked.display3DMode() != "map":
                    locked.setDisplay3DMode("map")
                    try:
                        self._doc.notifyObjectChanged(locked.id())
                    except Exception:
                        pass
                # 强制同步面板按钮 + 视图参数：setReliefModel 内部会把 _mode3DButtons
                # 的 checked 状态改为 model.display3DMode 对应的那一颗（这里是 "map"）。
                self._transformPanel.setReliefModel(locked)
        elif mode == "cnc":
            # CNC：3D 视口自由查看（与 3D 模式一致），显示 CNC 独立空间的
            # 几何 + 刀路轨迹/刀具。毛坯不自动创建——在“安装几何体”中
            # 勾选“显示毛坯”或按模型尺寸设置后才出现。
            # 顶部 CNC 类目下提供轮廓铣/口袋/钻孔等命令（见 top_dock.py）。
            self._exitReliefLock()
            self._active3D = True
            self._viewport3D.setCanvasMode("cnc")
            self._viewport3D.setHideReferenceGrid(False)
            self._viewStack.setCurrentWidget(self._viewport3D)
            self._topDock.appendHistory("CNC 模式：3D 视图自由查看，选择曲线生成刀路")
        else:
            # 2D：平面画布 —— 方向锁定为顶视（setViewMode 仅切方向，不 zoomToFit，
            # 原来的缩放/平移仍然完整保留）
            self._exitReliefLock()
            self._active3D = False
            self._viewport3D.setCanvasMode("2d")
            self._viewport3D.setHideReferenceGrid(True)
            self._viewStack.setCurrentWidget(self._view)
            self._view.setViewMode(1)

        # 浮雕命令按钮仅在浮雕模式下可用
        self._syncReliefButtons()
        # 顶部工具按钮按模式门禁置灰（2D 禁 3D 实体命令等，见 mode_policy）
        self._topDock.setCanvasMode(mode)

        # 空间切换后刷新图层面板数量（2D/3D/浮雕/CNC 活动对象表不同）
        self._layerPanel.refreshLater()

        # 模式切换结束后，应用目标模式分组 saved 下的视图切换栏状态（方向 + 锁定）。
        try:
            self._applyVSW(mode)
        except Exception:
            pass
        # 只有在 saved + apply 都完成后，才更新 _lastCanvasMode：确保下次再切时
        # saveVSW 会保存当前"新 mode"对应的真实左栏状态。
        self._lastCanvasMode = mode

        # 模式 → 上面类目联动（用户需求：切模式时顶部类目按钮跟随切换，
        # 方便用户继续操作对应类目下的命令）
        _modeToCategory = {
            "2d": "曲线绘制",
            "relief": "浮雕",
            "3d": "3D",
            "cnc": "CNC",
        }
        cat = _modeToCategory.get(mode)
        if cat is not None:
            try:
                self._topDock.setCurrentCategory(cat)
            except Exception:
                pass

    def _syncReliefButtons(self) -> None:
        """浮雕命令按钮仅在“浮雕”画布模式下可用，其它模式置灰禁用；
        浮雕撤销/重做按钮叠加各自历史状态（可为空历史而置灰）。"""
        qb = self._topDock.quickBar()
        qb.setReliefButtonsEnabled(self._canvasModeBar.current() == "relief")
        qb.setReliefUndoEnabled(self._doc.canReliefUndo())
        qb.setReliefRedoEnabled(self._doc.canReliefRedo())

    def _ensure2DCanvas(self) -> None:
        """命令激活时确保可见视图是 2D 平面画布（曲线点选/起浮雕都在 2D 上进行），
        但不改变当前画布模式 / 不改变 2D 视图的缩放平移视角（“原来是什么就是什么”）。

        之前会额外 setViewMode(1) 强制切到"顶视"，现已移除——若 2D 视图当时是别的
        视图方向也保留，仅保证显示页切换为 2D 画布。
        """
        if self._viewStack.currentWidget() is not self._view:
            self._viewStack.setCurrentWidget(self._view)

    # ---------- 视图切换栏：2D 分组 / 3D 分组 各自独立保存 / 恢复 ----------

    @staticmethod
    def _vswGroup(mode: str) -> str:
        """画布模式 → 视图切换栏分组。2D 锁顶视；CNC 不锁定可自由切视角；3D/浮雕共用"3d"。"""
        if mode in ("3d", "relief"):
            return "3d"
        if mode == "cnc":
            return "cnc"
        return "2d"

    def _saveVSW(self, canvas_mode: str) -> None:
        """把当前视图切换栏的 (current, locked) 保存到 {canvas_mode} 对应的分组里。"""
        grp = self._vswGroup(canvas_mode)
        self._vswSaved[grp] = (int(self._viewSwitchBar.current()),
                               bool(self._viewSwitchBar.isLocked()))

    def _applyVSW(self, canvas_mode: str) -> None:
        """从 saved 中取出 {canvas_mode} 对应分组的 (current, locked)，应用到左侧视图切换栏。

        只改视图切换栏外观/可点击状态，不触发 viewChanged：
          * ViewSwitchBar.setCurrent() 只刷新 _current，不发送 viewChanged；
          * setLocked() 只刷新样式并拦截点击，不产生任何信号。
        所以"恢复目标分组 saved 状态"不会改 2D/3D 的实际视图角度——只有用户手动
        点击方向标签时 _onViewChanged 才调用 setViewMode/zoomToFit，符合用户
        "原来是什么就是什么，切换模式不改视图视角"的要求。
        """
        grp = self._vswGroup(canvas_mode)
        saved = self._vswSaved.get(grp)
        if saved is None:
            return
        idx, locked = saved
        # ViewSwitchBar.setCurrent 与 setLocked 都不发信号，无需 blockSignals。
        self._viewSwitchBar.setCurrent(int(idx))
        self._viewSwitchBar.setLocked(bool(locked))

    def _enterReliefEdit(self) -> None:
        """进入浮雕编辑态：烘焙未落定的放置变换并锁定模型。

        放置变换（移动/缩放/旋转）无损累积在 ``_place``；进入编辑态前若有 pending
        变换则自动重构（烘焙进网格、复位 _place），保证雕刻在轴对齐主网格上进行。
        模型锁定为编辑目标（isLocked）：拾取、框选、操作轴均排除锁定对象，
        不可再选中/移动/改尺寸，仅浮雕/高度场/Z 向操作；曲线不受影响，与 2D 一致。
        """
        model = self._doc.activeReliefModel()
        if model is None:
            self._topDock.appendHistory("请先在 2D 选中浮雕模型")
        elif model.hasPendingTransform():
            self._doc.beginEdit()
            model.bake()
            self._doc.notifyObjectChanged(model.id())
            self._doc.endEdit()
            self._topDock.appendHistory("进入浮雕编辑：已重构模型网格")
        else:
            self._topDock.appendHistory("浮雕模式：模型已锁定编辑，曲线操作与 2D 一致")
        # 锁定该模型为浮雕编辑目标：视口/面板以它为准（与选中解耦），
        # 拾取与操作轴按 isLocked 排除
        self._applyReliefTarget(model)

    def _load3DKernelBlocking(self) -> None:
        """同步加载 3D 内核 + 预热 OCCT（可在加载页的后台线程里调用）。

        正常路径下本方法只在启动加载页里跑一次；若启动页兜底超时被跳过，
        首次进 3D 时由 _ensure3DKernel 的线程再调一次。"""
        import time
        from core.vector.brep_shape import _stagedImportBd
        _stagedImportBd()
        # 命令层热路径 OCP 子模块（OCP 二进制已载，薄封装）
        for mod in ("OCP.BRep", "OCP.BRepAlgoAPI",
                    "OCP.BRepBuilderAPI", "OCP.BRepClass3d",
                    "OCP.BRepExtrema", "OCP.BRepOffsetAPI",
                    "OCP.BRepTools", "OCP.Geom", "OCP.gp",
                    "OCP.ShapeFix", "OCP.TopAbs", "OCP.TopLoc",
                    "OCP.TopoDS", "OCP.TopTools"):
            try:
                __import__(mod)
            except Exception:
                pass
            time.sleep(0.005)
        # OCCT 内部预热：建模+体积+三角化+布尔融合/切割，
        # 首次挤出/布尔不再付惰性初始化的一次性代价（~2s）
        try:
            import build123d as bd
            from core.vector.brep_shape import BrepShape
            bs = BrepShape(bd.Box(1, 1, 1))
            _ = bs.part().volume
            _ = bs.triangulate()
            b1 = bd.Box(2, 2, 2)
            b2 = bd.Pos(1, 1, 1) * bd.Box(2, 2, 2)
            _ = (b1 + b2).volume   # 融合预热
            _ = (b1 - b2).volume   # 切割预热
        except Exception:
            pass

    def _ensure3DKernel(self) -> None:
        """首次进入 3D 模式：不等待内核，立即切换画布。

        内核（build123d/OCP）正常已在启动加载页内加载完毕（
        _load3DKernelBlocking），此方法只是兜底；未完成时后台分步导入
        （_stagedImportBd 每步让出 GIL），期间 3D 画布正常显示网格/曲线。"""
        if getattr(self, "_bdKernelLoaded", False):
            return
        if getattr(self, "_bdKernelLoading", False):
            return
        self._bdKernelLoading = True
        from PySide6.QtCore import QThread

        class _KernelLoader(QThread):
            def run(self):
                try:
                    self._win._load3DKernelBlocking()
                    self._win._bdKernelLoaded = True
                except Exception:
                    import traceback
                    traceback.print_exc()
                finally:
                    self._win._bdKernelLoading = False

        loader = _KernelLoader(self)
        loader._win = self
        self._bdLoader = loader
        loader.start()

    def _warmupBuild123d(self) -> None:
        """后台预热 build123d/OCC（首次导入约 2.6s，提前到 3D 模式切入后
        空闲时完成；此后挤出/倒角/渲染都不再等待）。"""
        try:
            from core.vector.brep_shape import _import_bd
            _import_bd()
        except Exception:
            pass

    def _fitReliefOnce(self, model) -> None:
        """浮雕编辑目标**变化**后自动适配相机一次（同一目标重复进入不改视角）。

        用户流程"2D 导入/新建模型 → 切浮雕"时模型从未被 3D 视口取景，高模型
        （STL 圆柱等）会顶出画面；浮雕模式里新生成模型时相机可能还对着上一个
        模型（或远离新模型的世界坐标），不重新取景就只看到空白/参考图——
        "切到 2D 再回浮雕才正常"正是那条路径。

        只按"上一个适配过的目标 id"判断（不是累积集合）：目标 A→B→A 切换时
        A 也会重新取景，避免切回旧目标后画面停在 B 的取景范围。同一目标
        反复进入浮雕模式仍保留用户调整过的视角（切模式不改相机的既有约定），
        要重新取景用"全部观察"。
        """
        if model is None:
            return
        last = getattr(self._viewport3D, "_reliefFittedId", None)
        if last == model.id():
            return
        self._viewport3D._reliefFittedId = model.id()
        self._viewport3D.fitReliefModel()

    def _exitReliefLock(self) -> None:
        """离开浮雕编辑态：解锁**所有**浮雕模型，恢复可见/可选中；默认重新选中便于继续变换。

        浮雕编辑态会隐藏非编辑模型并加编辑锁，离开时必须全部还原——只解锁
        跟踪到的那一个模型会让此前多次生成遗留的编辑锁留在文档里（表现为
        切回 2D 后模型点不中、再切回浮雕显示的是旧模型）。
        """
        model = self._reliefLockedModel
        self._applyReliefTarget(None)
        if model is None or self._doc is None:
            return
        if self._doc.object(model.id()) is not model:
            return
        if not self._doc.selectedObjects():
            self._doc.selectObject(model.id())

    def _onObjectRemoved(self, oid: str) -> None:
        """锁定的浮雕模型被移除（含撤销/重做的快照替换）：记录 id 等待可能的重建。"""
        m = self._reliefLockedModel
        if m is not None and m.id() == oid:
            self._reliefRestoreId = oid
            self._applyReliefTarget(None)

    def _applyReliefTarget(self, obj) -> None:
        """把 obj 设为浮雕编辑目标——唯一的锁定 + 唯一的可见浮雕模型。

        进入浮雕模式、撤销恢复、浮雕模式下新生成模型共用这一个入口，保证
        状态一次性收敛，不留中间态：
        - 其余浮雕模型一律**解除编辑锁**（残留编辑锁会让
          ``Document.activeReliefModel`` 返回旧模型：浮雕里显示的不是刚生成的
          模型、切到 2D 后模型因仍被锁而选不中，来回切模式才慢慢自愈）；
        - 目标可见且锁定，其余浮雕模型隐藏（隐藏后框选/点选也不会命中）；
        - 目标脱离选中（编辑态不占选择集），视口/变换面板同步到目标。

        obj=None：解锁全部、恢复全部可见、清空视口目标（离开浮雕/目标被删）。
        """
        relief = self._doc.reliefModels() if self._doc is not None else []
        changed = []
        for o in relief:
            if o is obj:
                continue
            if o.isEditLocked():
                o.setLocked(False)
            if obj is not None:
                if o.isVisible():
                    o.setVisible(False)
                    changed.append(o.id())
            elif not o.isVisible():
                o.setVisible(True)
                changed.append(o.id())
        if obj is not None:
            obj.setLocked(True)
            if not obj.isVisible():
                obj.setVisible(True)
                changed.append(obj.id())
            if obj.isSelected():
                obj.setSelected(False)
                self._doc.selectionChanged.emit()
        self._reliefLockedModel = obj
        for cid in changed:
            self._doc.notifyObjectChanged(cid)
        self._transformPanel.setReliefModel(obj)
        self._viewport3D.setReliefModel(obj)
        if obj is not None:
            self._fitReliefOnce(obj)
        try:
            self._viewport3D.update()
        except Exception:
            pass

    def _pinReliefModelAsTarget(self, obj) -> None:
        """把 obj 设为浮雕编辑目标：锁定（不可移动）+ 只显示它 + 脱离选中。

        进入浮雕模式、撤销恢复、以及浮雕模式下新生成模型时共用。锁定后
        拾取/框选/操作轴均排除该模型，位置由高度场 origin 决定。"""
        if obj is None or obj.type() != ObjectType.SURFACE:
            return
        self._applyReliefTarget(obj)

    def _onObjectAdded(self, oid: str) -> None:
        """撤销/重做按 id 重建对象：浮雕模式下同 id 回来则恢复锁定编辑态。"""
        if self._reliefRestoreId != oid:
            return
        self._reliefRestoreId = None
        if self._canvasModeBar.current() != "relief":
            return
        self._pinReliefModelAsTarget(self._doc.object(oid))


    def _onCommandRequested(self, name: str, *args) -> None:
        # 浮雕撤销/重做/固化：动作类（非命令），直接转调；仅浮雕模式可用
        # （与快捷条 RELIEF_COMMAND_KEYS 门禁一致，矩阵见 mode_policy.py）
        if name in ("ReliefUndo", "ReliefRedo", "Solidify"):
            from ui.commands.mode_policy import modeAllows, refusalHint
            if not modeAllows(name, self._canvasModeBar.current()):
                self._topDock.appendHistory(
                    refusalHint(name, self._canvasModeBar.current()))
                return
        if name == "ReliefUndo":
            self._doc.undoRelief()
            return
        if name == "ReliefRedo":
            self._doc.redoRelief()
            return
        if name == "Solidify":
            self._onCureBase()
            return
        if name == "ToggleCurveLayer":
            self._toggleCurveLayerVisible()
            return
        if self._cm.hasActive():
            self._cm.cancelActive()
        # 2D 画布绘制命令：若实体模拟面板还开着（遮挡画布左侧），先关闭模拟，
        # 避免用户"模拟之后画曲线"时点击落在面板上无响应
        _DRAW2D_COMMANDS = {"Point", "Line", "Curve", "Circle", "Arc", "Ellipse",
                            "Polyline", "Rectangle", "Polygon", "Text", "Dimension",
                            "Image", "ImageTrace", "Outline", "Offset", "WallLine"}
        if name in _DRAW2D_COMMANDS and getattr(self, "_cncSimPanel", None) is not None \
                and self._cncSimPanel.isVisible():
            self._onCncSimPanelClosed()
        # 图片转线稿是 2D 曲线工具：预览与结果都画在 2D 画布上
        if name == "ImageTrace":
            self._ensure2DCanvas()
        # 浮雕类命令（新建/区域浮雕）均在当前画布交互：新建后自动进编辑态并切到
        # 3D 视口，区域浮雕直接在 3D 视口点选曲线，右键用于旋转查看，不切回 2D
        if self._cm.executeCommand(name, *args):
            self._topDock.appendHistory(f"启动命令: {name}")
        else:
            # 被模式门禁拒绝 → 显示拒绝原因；否则为未实现/已有活动命令
            ref = self._cm.lastRefusal()
            self._topDock.appendHistory(ref if ref else f"命令未实现: {name}")

    def _toggleCurveLayerVisible(self) -> None:
        """浮雕模式下 E：显示/隐藏曲线图层（固定根层，级联到全部曲线子图层）。"""
        from core.document import LAYER_CURVES_ID
        if self._canvasModeBar.current() != "relief":
            self._topDock.appendHistory("E：仅在浮雕模式下切换曲线图层显示")
            return
        layer = self._doc.layer(LAYER_CURVES_ID)
        if layer is None:
            return
        show = not layer.visible
        self._doc.setLayerFlag(LAYER_CURVES_ID, visible=show)
        # 2D 场景与 3D/浮雕视口都需重绘（视口按对象可见性过滤曲线）
        self._scene.update()
        self._viewport3D.update()
        self._topDock.appendHistory(f"曲线图层：{'显示' if show else '隐藏'}")

    def _onMouseMoved(self, wp: "Point3D") -> None:
        self._statusBar.setCoordinates(wp.x, wp.y, wp.z)
        self._updateMeasure()

    def _updateMeasure(self) -> None:
        """状态栏长度/面积：
        - 任一开关关闭 → 对应项显示 "--"
        - 单对象选中 → 长度 = obj.pathLength()，面积 = obj.enclosedArea()（未闭合显示 "--"）
        - 多对象选中 → 长度 = 各对象 pathLength 之和；面积不计算（多选合并面积会歧义，显示 "--"）
        - 0 个对象选中 → 长度、面积均不显示
        """
        if not self._statusBar.measureEnabled():
            self._statusBar.setLengthArea(None, None)
            return
        sel = self._doc.selectedObjects()
        if len(sel) == 1:
            obj = sel[0]
            self._statusBar.setLengthArea(obj.pathLength(), obj.enclosedArea())
            return
        if len(sel) > 1:
            # 合计长度，面积留空（多对象的「总面积」语义不明：可能求和、可能要布尔合并轮廓）
            totalLen = 0.0
            for o in sel:
                try:
                    v = o.pathLength() or 0.0
                    totalLen += float(v)
                except Exception:
                    pass
            self._statusBar.setLengthArea(totalLen, None)
            return
        # 0 选中
        self._statusBar.setLengthArea(None, None)

    def _onSnapToggled(self, key: str, checked: bool) -> None:
        sm = SnapManager.instance()
        if key == "none":
            if checked:
                sm.setEnabledTypes(set())
            return
        st = _SNAP_KEY_MAP.get(key)
        if st is not None:
            sm.setTypeEnabled(st, checked)

    def _onOrthoToggled(self, on: bool) -> None:
        GridManager.instance().setOrthoEnabled(on)

    def _onEditCurveToggled(self, on: bool) -> None:
        """底部“编辑曲线”开关：开启后选中曲线即显示控制点，
        点选/框选控制点后可直接拖动或配合操作轴移动/旋转/缩放。"""
        self._curveEdit.setEnabled(on)
        self._gizmo.refresh()

    def _onLineWidthChanged(self, w: float) -> None:
        # 底部线宽：全局控制——实时更新文档内全部曲线对象（2D/3D 同刷），
        # 同时也是新建对象的默认线宽
        self._doc.setDefaultLineWidth(w)
        for obj in self._doc.allObjects():
            if abs(obj.lineWidth() - w) > 1e-9:
                obj.setLineWidth(w)
                self._doc.notifyObjectChanged(obj.id())

    def _onSelectionChanged(self) -> None:
        sel = self._doc.selectedObjects()
        obj = sel[0] if sel else None
        self._propertyPanel.showObject(obj)
        self._transformPanel.showObjects(sel)
        if obj is not None:
            self._statusBar.setLineWidth(obj.lineWidth())
        self._updateMeasure()
        # 画布左下角：无命令活动时显示选中摘要（开放/闭合曲线、模型、图片、标注…）
        # 2D 画布与 3D/浮雕视口都同步（3D 视口对齐 2D 同一套判定与显示）
        summary = self._formatSelectionSummary(sel)
        if self._view is not None:
            self._view.setSelectionSummary(summary)
        try:
            self._viewport3D.setSelectionSummary(summary)
        except Exception:
            pass

    # ---------- 选中摘要 ----------

    @staticmethod
    def _samplePointsFor(obj) -> list:
        """仅用于判定几何闭合：返回首尾两个采样点（尽量轻量）。
        返回 List[Point3D]；无法取采样点返回空列表。"""
        t = obj.type()
        try:
            if t == ObjectType.LINE:
                return list(obj.points()) or []
            if t in (ObjectType.CURVE, ObjectType.NURBS_CURVE):
                pts = list(obj.getSampledPoints())
                if len(pts) >= 2:
                    return [pts[0], pts[-1]]
                return pts
            if t in (ObjectType.CIRCLE, ObjectType.ELLIPSE, ObjectType.ARC):
                poly = obj.toPolycurve()
                segs = list(poly.segments())
                if not segs:
                    return []
                first = segs[0].get("points", [])
                last = segs[-1].get("points", [])
                out = []
                if first:
                    p = first[0]
                    out.append(Point3D(p[0], p[1], p[2] if len(p) > 2 else 0.0))
                if last:
                    p = last[-1]
                    out.append(Point3D(p[0], p[1], p[2] if len(p) > 2 else 0.0))
                return out
            if t == ObjectType.POLYCURVE:
                segs = list(obj.segments())
                if not segs:
                    return []
                first = segs[0].get("points", [])
                last = segs[-1].get("points", [])
                out = []
                if first:
                    p = first[0]
                    out.append(Point3D(p[0], p[1], p[2] if len(p) > 2 else 0.0))
                if last:
                    p = last[-1]
                    out.append(Point3D(p[0], p[1], p[2] if len(p) > 2 else 0.0))
                return out
            if t == ObjectType.RECTANGLE:
                pts = list(obj.points()) if hasattr(obj, "points") else []
                return pts or []
        except Exception:
            return []
        return []

    @classmethod
    def _isCurveClosed(cls, obj) -> bool:
        """几何闭合检测：同时用 isClosed 标志和首末采样点距离。
        CIRCLE / ELLIPSE / RECTANGLE 天然闭合，直接 True。"""
        t = obj.type()
        if t in (ObjectType.CIRCLE, ObjectType.ELLIPSE, ObjectType.RECTANGLE):
            return True
        if t not in (ObjectType.LINE, ObjectType.CURVE, ObjectType.POLYCURVE,
                     ObjectType.ARC, ObjectType.NURBS_CURVE):
            return False
        if hasattr(obj, "isClosed") and callable(obj.isClosed):
            try:
                if obj.isClosed():
                    return True
            except Exception:
                pass
        pts = cls._samplePointsFor(obj)
        if len(pts) >= 2 and pts[0].distanceTo(pts[-1]) < 1e-6:
            return True
        return False

    @classmethod
    def _formatSelectionSummary(cls, selected) -> str:
        """按用户要求分类计数：
        - N 条开放曲线 / N 条闭合曲线（对 LINE/CURVE/NURBS/POLYCURVE/ARC/CIRCLE/ELLIPSE/RECT）
        - N 个模型（SURFACE）
        - N 个实体（BREP）
        - N 张图片（IMAGE）
        - N 个标注（DIMENSION）
        - N 个群组（GROUP）
        - N 个点（POINT）
        - N 个其他（FACE/SOLID 等保留类型）
        无选中返回空串（StepHint 回到默认「提示信息显示区」）。
        """
        if not selected:
            return ""
        openCurve = 0
        closedCurve = 0
        model = 0
        brep = 0
        image = 0
        dim = 0
        group = 0
        point = 0
        other = 0
        for o in selected:
            t = o.type()
            if t == ObjectType.SURFACE:
                model += 1
            elif t == ObjectType.BREP:
                brep += 1
            elif t == ObjectType.IMAGE:
                image += 1
            elif t == ObjectType.DIMENSION:
                dim += 1
            elif t == ObjectType.GROUP:
                group += 1
            elif t == ObjectType.POINT:
                point += 1
            elif t in (ObjectType.LINE, ObjectType.CURVE,
                       ObjectType.POLYCURVE, ObjectType.ARC,
                       ObjectType.NURBS_CURVE, ObjectType.CIRCLE,
                       ObjectType.ELLIPSE, ObjectType.RECTANGLE):
                if cls._isCurveClosed(o):
                    closedCurve += 1
                else:
                    openCurve += 1
            else:
                other += 1
        parts = []
        if openCurve:
            parts.append(f"{openCurve} 条开放曲线")
        if closedCurve:
            parts.append(f"{closedCurve} 条闭合曲线")
        if model:
            parts.append(f"{model} 个模型")
        if brep:
            parts.append(f"{brep} 个实体")
        if image:
            parts.append(f"{image} 张图片")
        if dim:
            parts.append(f"{dim} 个标注")
        if group:
            parts.append(f"{group} 个群组")
        if point:
            parts.append(f"{point} 个点")
        if other:
            parts.append(f"{other} 个其他")
        if not parts:
            return ""
        return "选中：" + ", ".join(parts)

    _MIRROR_SCALE = {"X": (-1, 1, 1), "Y": (1, -1, 1), "Z": (1, 1, -1)}

    def _dimStyle(self) -> tuple:
        """标注样式：世界字高按“设置的文字屏幕高度 / 当前视口缩放”换算，净空随字高。"""
        scale = 1.0
        if self._view is not None:
            scale = max(self._view.zoomScale(), 1e-9)
        fs = app_settings.dim_text_px / scale
        return (fs, fs * TEXT_TO_OBJ_GAP)

    def _onDimensionTextResize(self, factor: float) -> None:
        """增大/缩小选中标注的读数文字（含图形净空，整体比例缩放）。

        仅作用于 Dimension 对象；未选中标注时提示（新标注默认字号可在
        设置 → 通用 → 标注 中调整）。
        """
        dims = [o for o in self._doc.selectedObjects()
                if o.type() == ObjectType.DIMENSION]
        if not dims:
            self._notifyTip(
                "请先选中标注对象（也可在设置→通用→标注 调整默认字号）")
            return
        self._doc.beginEdit()
        for o in dims:
            o.rescaleText(factor)
            self._doc.notifyObjectChanged(o.id())
        self._doc.endEdit()
        self._onSelectionChanged()

    def _onDimensionBindToggled(self) -> None:
        """变换面板“绑定”开关被点击：解除选中 XY/Z 标注与源对象的绑定。

        绑定解除即“脱离关系”：目标记录被清空且不再提供重新绑定入口，
        之后物体尺寸变化不再影响该标注。
        """
        dims = [o for o in self._doc.selectedObjects()
                if o.type() == ObjectType.DIMENSION and o.kind() in ("xy", "z")
                and o.boundTargetIds()]
        if not dims:
            return
        self._doc.beginEdit()
        try:
            dims[0].unbind()
            self._doc.notifyObjectChanged(dims[0].id())
        finally:
            self._doc.endEdit()
        self._topDock.appendHistory("已解除标注与物体的绑定")
        self._notifyTip(
            "已解除标注与物体的绑定：物体改变尺寸不再影响该标注")
        self._onSelectionChanged()

    def _onDimensionRequested(self, kind: str) -> None:
        """变换面板“尺寸标注”区按钮：XY标注 / 对齐标注 / Z标注。"""
        if kind == "xy":
            self._createXYDimension()
        elif kind == "align":
            self._startAlignDim()
        elif kind == "z":
            self._createZDimension()

    def _startAlignDim(self) -> None:
        """对齐标注：2D 模式切 2D 画布，3D 模式留在 3D 视口，进入"点两点"命令。"""
        if self._cm.hasActive():
            self._cm.cancelActive()
        if self._canvasModeBar.current() != "3d":
            self._ensure2DCanvas()
        if self._cm.executeCommand("AlignDim"):
            self._topDock.appendHistory("对齐标注：点击两个点创建间距标注")
        else:
            self._topDock.appendHistory("命令未实现: AlignDim")

    def _createXYDimension(self) -> None:
        """XY 标注：对选中对象（单个/多个）的联合包围盒直接标注 X 宽与 Y 高。

        标注对象自动落入“标注”固定层，并默认与所选源对象“绑定”——源对象
        尺寸/位置变化时标注实时跟随更新；如需固定标注不再随动，可在变换面板
        点“绑定”解除（解除后脱离关系，无法重新绑定）。
        """
        meas = [o for o in self._doc.selectedObjects()
                if o.type() != ObjectType.DIMENSION]
        if not meas:
            self._topDock.appendHistory("XY 标注：请先选中要标注的图形对象")
            return
        bb = BoundingBox3D()
        for obj in meas:
            b = obj.getBoundingBox()
            if b.isValid():
                bb.expand(b.min)
                bb.expand(b.max)
        if not bb.isValid():
            self._topDock.appendHistory("XY 标注：选中对象无有效包围盒")
            return
        w = bb.max.x - bb.min.x
        h = bb.max.y - bb.min.y
        if w < 1e-9 and h < 1e-9:
            self._topDock.appendHistory("XY 标注：包围盒过小，无法标注")
            return
        fs, gap = self._dimStyle()
        dim = DimensionObject()
        dim.setXYRect(bb.min.x, bb.min.y, bb.max.x, bb.max.y, fs, gap)
        dim.bindTo([o.id() for o in meas])   # 默认绑定所选源对象，随物体实时更新
        self._doc.beginEdit()
        self._doc.addObject(dim)
        self._doc.endEdit()
        # 创建后自动选中标注本体：变换面板的“绑定”状态/字号按钮立即可见可用，
        # 也便于直接拖动 X/Y 读数把标注翻到物体的上/右侧
        self._doc.selectObject(dim.id())
        parts = []
        if w >= 1e-9:
            parts.append(f"X {w:.2f}")
        if h >= 1e-9:
            parts.append(f"Y {h:.2f}")
        self._topDock.appendHistory(f"XY 标注已创建（{'，'.join(parts)}）")

    def _createZDimension(self) -> None:
        """Z 标注：对选中对象（单个/多个）的联合 3D 包围盒标注 Z 高度。

        仅 3D 模式可用——Z 标注是竖直尺寸线，标注被测对象的 Z 范围
        （顶到底的高度），尺寸线位于对象右侧（XY 平面内）。标注对象自动
        落入“标注”固定层，并默认与所选源对象绑定——源对象几何变化时
        标注实时跟随更新。
        """
        if self._canvasModeBar.current() != "3d":
            self._topDock.appendHistory("Z 标注：仅在 3D 模式下可用")
            self._notifyTip("Z 标注仅在 3D 模式下可用，请先切换到 3D 模式")
            return
        meas = [o for o in self._doc.selectedObjects()
                if o.type() != ObjectType.DIMENSION]
        if not meas:
            self._topDock.appendHistory("Z 标注：请先选中要标注的对象")
            self._notifyTip("请先选中要标注高度的对象，再点 Z 标注")
            return
        bb = BoundingBox3D()
        for obj in meas:
            b = obj.getBoundingBox()
            if b.isValid():
                bb.expand(b.min)
                bb.expand(b.max)
        if not bb.isValid():
            self._topDock.appendHistory("Z 标注：选中对象无有效包围盒")
            return
        zh = bb.max.z - bb.min.z
        if zh < 1e-9:
            self._topDock.appendHistory("Z 标注：对象 Z 高度过小，无法标注")
            self._notifyTip("选中对象的 Z 高度为 0，无法创建 Z 标注")
            return
        fs, gap = self._dimStyle()
        dim = DimensionObject("z")
        dim.setZRange(bb.min.x, bb.min.y, bb.max.x, bb.max.y,
                      bb.min.z, bb.max.z, fs, gap)
        dim.bindTo([o.id() for o in meas])   # 默认绑定所选源对象，随物体实时更新
        self._doc.beginEdit()
        self._doc.addObject(dim)
        self._doc.endEdit()
        self._doc.selectObject(dim.id())
        self._topDock.appendHistory(f"Z 标注已创建（Z {zh:.2f}）")

    def _onMirrorRequested(self, axis: str) -> None:
        """镜像快捷操作。

        - 浮雕模式下：X/Y 镜像仍作用于选中的曲线（与 2D 一致）；**Z 镜像只对
          锁定的浮雕模型生效**——做高度场 Z 镜像（山↔谷），不对曲线起作用
          （曲线是平面轮廓，无需 Z 镜像）。
        - 其余模式：整体镜像选中对象，以所有选中对象的联合包围盒中心为基准
          一起翻转，保持彼此相对布局（如“模型 + 其上的参考曲线”作为一个整体）。

        注意不能用“各自包围盒中心”作基准：曲线（圆、对称参考线等）绕自身中心
        翻转后形状不变、位置不动，看起来就像没被镜像。
        """
        if axis == "Z" and self._canvasModeBar.current() == "relief":
            # 浮雕模式：Z 镜像键对高度场镜像（曲线不受影响）
            self._mirrorReliefHeight()
            return
        scale = self._MIRROR_SCALE.get(axis)
        if scale is None:
            return
        sel = self._doc.selectedObjects()
        if not sel:
            return
        bb = BoundingBox3D()
        for obj in sel:
            b = obj.getBoundingBox()
            if b.isValid():
                bb.expand(b.min)
                bb.expand(b.max)
        center = bb.center() if bb.isValid() else Point3D()
        self._doc.beginEdit()
        for obj in sel:
            obj.scaleAround(*scale, center)
            self._doc.notifyObjectChanged(obj.id())
        self._doc.endEdit()
        self._onSelectionChanged()

    def _mirrorReliefHeight(self) -> None:
        """浮雕模式 Z 镜像 = 高度场 Z 镜像：把锁定模型的曲面沿 Z 上下颠倒。

        z' = (z_min + z_max) - z：绕模型高度范围中面翻转、整体高度范围不变；
        模型底面在 Z=0（常规浮雕 min=0）时镜像后**最低点仍保持在 Z=0**（贴地），
        实现阳刻↔阴刻互换。只改网格高度，XY 布局与色号蒙版不变，曲线不受影响。
        入浮雕撤销历史，可逐步撤销/重做。
        """
        model = self._reliefLockedModel
        if model is None:
            self._topDock.appendHistory("Z 镜像仅对浮雕模型生效：请先进入浮雕编辑态")
            return
        hf = model.heightField()
        data = hf.data
        if data.size == 0:
            return
        mn = float(data.min())
        mx = float(data.max())
        if mx <= mn:
            self._topDock.appendHistory("Z 镜像：模型为平面，无高度可翻转")
            return
        self._doc.beginEdit()
        try:
            hf.setData((mn + mx) - data)
            model.bumpVersion()
            self._doc.notifyObjectChanged(model.id())
            self._topDock.appendHistory(
                "Z 镜像：浮雕高度场已翻转（最低点保持 Z=0）")
        finally:
            self._doc.endEdit()
        self._scene.update()
        self._viewport3D.update()

    def _onPanelXform(self, cmdName: str) -> None:
        """变换面板移动/旋转/缩放快捷命令：2D 模式切 2D 画布，3D 模式留在 3D 视口。"""
        if self._cm.hasActive():
            self._cm.cancelActive()
        if self._canvasModeBar.current() != "3d":
            self._ensure2DCanvas()
        titles = {"PanelMove": "移动", "PanelRotate": "旋转", "PanelScale": "缩放"}
        prompts = {
            "PanelMove": "移动：点基准点 → 点目标点（实时预览）",
            "PanelRotate": "旋转：点中心点 → 点参考起点 → 点目标角度确认",
            "PanelScale": "缩放：点基准点 → 点参考起点 → 点目标距离确认"}
        if self._cm.executeCommand(cmdName):
            self._topDock.appendHistory(prompts.get(cmdName, titles.get(cmdName, cmdName)))
        else:
            self._topDock.appendHistory(f"命令未实现: {cmdName}")

    def _onMeasure(self) -> None:
        """变换面板“测量”按钮：切 2D 画布并启动 PanelMeasure。"""
        if self._cm.hasActive():
            self._cm.cancelActive()
        self._ensure2DCanvas()
        if self._cm.executeCommand("PanelMeasure"):
            self._topDock.appendHistory("测量：点两个点，显示两点距离")
        else:
            self._topDock.appendHistory("命令未实现: PanelMeasure")

    def _notifyTip(self, text: str) -> None:
        """把临时提示信息输出到画布左下角的 StepHint（结果行，5 秒自动淡出）。
        不再走底部 QStatusBar.showMessage；所有业务提示都用这个方法。"""
        if self._cm is not None:
            self._cm.notifyResult(text)

    # ---------------- 颜色栏：清除颜色 / 自动分色 ----------------

    def _onCureBase(self) -> None:
        """固化（固化按钮左键）：把当前高度快照为底座（基准）。

        之后的流动叠加等操作相对底座计算（交点/重复导动不重复叠加）。"""
        model = self._doc.activeReliefModel() if self._doc is not None else None
        if model is None:
            self._notifyTip("请先在浮雕模式选中浮雕模型")
            return
        if model.cureBase():
            self._notifyTip("已固化：当前高度保存为基准，后续导动以此为参考")
        else:
            self._notifyTip("固化失败：模型高度场为空")

    def _onFlowToBase(self) -> None:
        """流化（固化按钮右键）：高度恢复到固化底座（一次可撤销操作）。"""
        model = self._doc.activeReliefModel() if self._doc is not None else None
        if model is None:
            self._notifyTip("请先在浮雕模式选中浮雕模型")
            return
        if not model.hasCuredBase():
            self._notifyTip("尚未固化：请先左键【固化】保存基准")
            return
        self._doc.beginEdit()
        try:
            changed = model.flowToBase()
            if changed:
                model.bumpVersion()
                self._doc.notifyObjectChanged(model.id())
        finally:
            self._doc.endEdit()
        self._notifyTip("已流化：高度恢复至固化基准"
                        if changed else "高度与固化基准一致，无变化")

    def _onClearColors(self) -> None:
        """清除颜色：清空活动浮雕模型的全部色号（一次可撤销操作）。"""
        model = self._doc.activeReliefModel() if self._doc is not None else None
        if model is None:
            self._notifyTip("请先在浮雕模式选中浮雕模型")
            return
        self._doc.beginEdit()
        try:
            cleared = model.clearColorMask()
            if cleared:
                self._doc.notifyObjectChanged(model.id())
        finally:
            self._doc.endEdit()
        self._notifyTip("已清除模型颜色" if cleared else "模型没有已填颜色")

    def _onAutoColorSeparate(self) -> None:
        """自动分色：识别全部选中曲线围成的闭合区域，逐块填不同颜色。

        开放曲线也参与分割（如一条直线横穿圆 → 圆被分成 2 个闭合区域，
        各填一色）。色号从调色板第 1 色起循环取用；曲线压线格子归入
        2 格内最近区域（线上不残留杂色）。一次可撤销操作。
        """
        model = self._doc.activeReliefModel() if self._doc is not None else None
        if model is None:
            self._notifyTip("请先在浮雕模式选中浮雕模型")
            return
        from core.relief.auto_separate import compute_region_colors
        curves = [o for o in self._doc.selectedObjects()
                  if o.type() != ObjectType.SURFACE and not o.isLocked()]
        if not curves:
            self._notifyTip("请先选中曲线，再自动分色")
            return
        # 曲线为世界坐标：模型若有未烘焙的放置变换，先烘焙对齐（与空格填色同一约定）
        baked = model.hasPendingTransform()
        if baked:
            self._doc.beginEdit()
            try:
                model.bake()
                self._doc.notifyObjectChanged(model.id())
            finally:
                self._doc.endEdit()
        values, regions = compute_region_colors(model.heightField(), curves,
                                                PaintState.count())
        if values is None:
            self._notifyTip("未识别到闭合区域：曲线需在模型上围出封闭范围")
            return
        self._doc.beginEdit()
        try:
            model.fillColorMaskValues(values)
            self._doc.notifyObjectChanged(model.id())
        finally:
            self._doc.endEdit()
        self._notifyTip(f"自动分色完成：{regions} 个闭合区域")

    def _onModelZCommit(self, target: float) -> None:
        """变换面板“模型Z”确认：把浮雕模型高度场缩放到目标 Z 尺寸。

        目标值是最高点到最低点的高度差（不是 Z 正方向尺寸）；缩放系数 =
        目标值 / 当前高度差，以最低点为锚点（最低点位置不变），XY 布局与
        色号蒙版不变，可经浮雕撤销逐步回退。3D 模式下则作用于当前选中的
        SURFACE 模型。
        """
        model = self._reliefLockedModel
        if model is None:
            sel = self._doc.selectedObjects()
            model = next((o for o in sel
                          if o.type() == ObjectType.SURFACE), None)
        if model is None or target <= 0.0:
            if model is None:
                self._topDock.appendHistory("模型Z：未找到浮雕模型，请先在浮雕模式下打开模型")
            return
        hf = model.heightField()
        data = hf.data
        if data.size == 0:
            return
        lo = float(data.min())
        cur = float(data.max()) - lo
        if cur <= 0.0:
            self._topDock.appendHistory("模型Z：当前模型无高度（平面），无法缩放")
            return
        factor = target / cur
        if abs(factor - 1.0) < 1e-9:
            return
        self._doc.beginEdit()
        try:
            hf.setData(lo + (data.astype(np.float64) - lo) * factor)
            model.bumpVersion()
            self._doc.notifyObjectChanged(model.id())
            self._topDock.appendHistory(f"模型Z：Z 尺寸已缩放到 {target:g}")
        finally:
            self._doc.endEdit()
        self._scene.update()
        self._viewport3D.update()

    def _onModelZClip(self, z_min: float, z_max: float) -> None:
        """变换面板“限高保底”：把浮雕模型高度场 clip 到 [z_min, z_max]。

        低于 z_min 的点强制 = z_min，高于 z_max 的点强制 = z_max。
        """
        model = self._reliefLockedModel
        if model is None:
            sel = self._doc.selectedObjects()
            model = next((o for o in sel
                          if o.type() == ObjectType.SURFACE), None)
        if model is None:
            self._topDock.appendHistory("限高保底：未找到浮雕模型，请先在浮雕模式下打开模型")
            return
        hf = model.heightField()
        data = hf.data
        if data.size == 0:
            return

        clipped = np.clip(data, z_min, z_max)
        if np.array_equal(clipped, data):
            # 明确告知为何“没变化”：截断只削掉超出区间的峰/谷，
            # 输入区间完整覆盖当前高度时模型本就不会变。
            self._topDock.appendHistory(
                f"限高保底：数值未改变模型（当前范围 {float(data.min()):g} ~ "
                f"{float(data.max()):g}，截断区间 [{z_min:g}, {z_max:g}]）")
            return
        self._doc.beginEdit()
        try:
            hf.setData(clipped)
            model.bumpVersion()
            self._doc.notifyObjectChanged(model.id())
            self._topDock.appendHistory(
                f"限高保底：高度场已截断到 [{z_min:g}, {z_max:g}]")
        finally:
            self._doc.endEdit()
        self._scene.update()
        self._viewport3D.update()

    def _onShowSettings(self) -> None:
        dlg = SettingsDialog(self, self._plugins)
        dlg.appearanceChanged.connect(self._refreshStyle)
        dlg.gridVisibleChanged.connect(self._scene.setGridVisible)
        dlg.axisVisibleChanged.connect(self._scene.setAxisVisible)
        dlg.selectionBoxChanged.connect(self._onSelectionBoxChanged)
        dlg.fileSlotsChanged.connect(self._leftPanel.refreshSavePage)
        dlg.renderSettingsChanged.connect(self._onRenderSettingsChanged)
        # 标注样式（字体/单位）变化：重绘场景让已有标注立即采用新样式
        dlg.dimensionSettingsChanged.connect(self._scene.update)
        dlg.exec()
        dlg.deleteLater()   # 用完即弃：避免控件树与信号连接随打开次数累积
        # 键盘页可能改了快捷键：立即重建映射
        self._reloadShortcuts()
        # 文件页设置（自动保存开关/间隔）可能已改变：按新设置重启自动保存定时器
        self._restartAutosaveTimer()

    def _onSelectionBoxChanged(self, on: bool) -> None:
        """选中物体虚线框开关：写入 app_settings 并刷新 2D/3D 场景。"""
        app_settings.show_selection_box = bool(on)
        self._scene.update()
        self._viewport3D.update()

    def _restartAutosaveTimer(self) -> None:
        """按文件页设置启动/重启自动保存定时器（未启用则停止）。"""
        if not hasattr(self, "_autosaveTimer"):
            return
        if getattr(app_settings, "autosave_enabled", False):
            interval = max(1, int(getattr(app_settings, "autosave_interval_min", 5)))
            self._autosaveTimer.start(interval * 60 * 1000)
        else:
            self._autosaveTimer.stop()

    def _onAutosaveTimeout(self) -> None:
        """自动保存：把“有文件路径且未保存”的文档另存恢复副本。

        副本写到原文件旁的 <文件名>.cai.autosave，不改动文档的未保存标记；
        原文件保存（_saveTo）成功后删除对应恢复副本。
        """
        import os
        for d in self._openDocs:
            if not d.isDirty() or not d.filePath():
                continue
            try:
                d.save(d.filePath() + ".autosave")
            except Exception:
                continue

    def _onRenderSettingsChanged(self) -> None:
        """渲染性能设置变更：3D 视口重建缓存/切换 GPU 路径，并刷新 2D 预览。"""
        try:
            self._viewport3D.applyRenderSettings()
        except Exception:
            import traceback
            traceback.print_exc()
        self._scene.update()
        self._viewport3D.update()

    # ---------------- 多文件系统（标签栏见画布右下角） ----------------

    def _bindDocSignals(self, quickBar=None) -> None:
        """把文档侧信号接到当前 self._doc（切换文档时断开旧文档重连新文档）。"""
        if quickBar is None:
            quickBar = self._topDock.quickBar()
        old = getattr(self, "_boundDoc", None)
        if old is not None:
            try:
                old.curveUndoChanged.disconnect(quickBar.setUndoEnabled)
                old.curveRedoChanged.disconnect(quickBar.setRedoEnabled)
                old.reliefUndoChanged.disconnect(quickBar.setReliefUndoEnabled)
                old.reliefRedoChanged.disconnect(quickBar.setReliefRedoEnabled)
                old.selectionChanged.disconnect(self._onSelectionChanged)
                old.objectRemoved.disconnect(self._onObjectRemoved)
                old.objectAdded.disconnect(self._onObjectAdded)
                old.layerChanged.disconnect(self._layerPanel.refreshLater)
                old.dirtyChanged.disconnect(self._onDocDirtyChanged)
            except (RuntimeError, TypeError):
                pass
        doc = self._doc
        doc.curveUndoChanged.connect(quickBar.setUndoEnabled)
        doc.curveRedoChanged.connect(quickBar.setRedoEnabled)
        doc.reliefUndoChanged.connect(quickBar.setReliefUndoEnabled)
        doc.reliefRedoChanged.connect(quickBar.setReliefRedoEnabled)
        doc.selectionChanged.connect(self._onSelectionChanged)
        doc.objectRemoved.connect(self._onObjectRemoved)
        doc.objectAdded.connect(self._onObjectAdded)
        doc.layerChanged.connect(self._layerPanel.refreshLater)
        doc.dirtyChanged.connect(self._onDocDirtyChanged)
        quickBar.setUndoEnabled(doc.canCurveUndo())
        quickBar.setRedoEnabled(doc.canCurveRedo())
        quickBar.setReliefUndoEnabled(doc.canReliefUndo())
        quickBar.setReliefRedoEnabled(doc.canReliefRedo())
        # 开放平台：把当前文档信号接入 API 事件总线（多文件切换自动重绑）
        if getattr(self, "_api", None) is not None:
            self._api.relayDocumentEvents(doc)
        self._boundDoc = doc

    def _onDocDirtyChanged(self, dirty: bool) -> None:
        self._updateFileTabs()

    def _newDocument(self, path: str = "") -> None:
        """新建文档标签（始终追加，保留已有标签）；path 非空时从文件载入。"""
        doc = Document()
        if path:
            doc.load(path)
            doc.setFilePath(path)
        else:
            self._untitledSeq += 1
            doc.setTitle(f"未命名{self._untitledSeq}")
        self._openDocs.append(doc)
        self._switchToDocument(len(self._openDocs) - 1)

    def _switchToDocument(self, index: int) -> None:
        """切换到 _openDocs[index]：保存旧视图状态 → 整体重绑各组件 → 恢复视图。"""
        if not (0 <= index < len(self._openDocs)):
            return
        if index == self._docIndex and self._doc is self._openDocs[index]:
            self._updateFileTabs()
            return
        old = self._doc
        try:
            # 每文档视图状态（变换矩阵 + 视野中心）挂在文档实例上，切走保存、切回恢复
            old._savedViewState = (
                QTransform(self._view.transform()),
                self._view.mapToScene(self._view.viewport().rect().center()))
        except RuntimeError:
            pass
        # 每文档画布模式（2d/relief）与浮雕编辑锁定的模型 id 也挂在文档实例上：
        # 切走时记录当前模式，切回时按保存值恢复——“原来是什么样子就什么样子”。
        old._canvasMode = self._canvasModeBar.current()
        old._reliefLockedModelId = (
            self._reliefLockedModel.id()
            if self._reliefLockedModel is not None else None)
        self._cm.cancelActive()      # 结束活动命令（清理命令预览图元）
        self._exitReliefLock()       # 退出旧文档的浮雕编辑态
        self._reliefRestoreId = None
        self._doc = self._openDocs[index]
        self._docIndex = index
        # 各组件换绑到新文档
        self._view.setDocument(self._doc)
        self._bridge.setDocument(self._doc)
        self._doc._bridge = self._bridge
        self._cm.setContext(self._doc, self._view)
        self._gizmo.setDocument(self._doc)
        self._curveEdit.setDocument(self._doc)
        self._viewport3D.setDocument(self._doc)
        self._layerPanel.setDocument(self._doc)
        self._transformPanel.setDocument(self._doc)
        # CNC 向导面板也要换绑（否则毛坯显示/Stock 框永远作用于第一个文档）
        self._leftPanel.setCncDocument(self._doc)
        self._bindDocSignals()
        # 恢复该文档的画布模式（不再统一强制回 2D）：每文档各自记住自己的模式。
        # - 目标模式取文档保存的 _canvasMode；新文档/未保存过 → "2d"；
        # - 目标为 relief 但该文档当前没有浮雕模型（模型被删除等）→ 退回 2D，
        #   避免切回后停在空浮雕视口；
        # - 需要跨模式切换时走 setCurrent(notify=True) 完整切换链路
        #   （_onCanvasModeChanged 用 _lastCanvasMode 当旧模式保存左栏状态）；
        #   若 bar 已经停在目标模式（如两个文档都在浮雕），则手工等效收尾：
        #   同步 _lastCanvasMode、应用左栏 saved、重新进入浮雕编辑态。
        target = getattr(self._doc, "_canvasMode", None)
        if target not in ("2d", "relief", "3d", "cnc"):
            target = "2d"
        if target == "relief" and not self._doc.reliefModels():
            target = "2d"
        if target == "relief" and self._doc.activeReliefModel() is None:
            # 之前锁定的浮雕模型仍在文档里，就预选它，保证 _enterReliefEdit
            # 恢复锁定到同一模型（activeReliefModel 取编辑锁优先、其次首个选中的 SURFACE）
            lid = getattr(self._doc, "_reliefLockedModelId", None)
            if lid is not None:
                obj = self._doc.object(lid)
                if obj is not None and obj.type() == ObjectType.SURFACE:
                    self._doc.selectObject(lid)
        if self._canvasModeBar.current() != target:
            self._canvasModeBar.setCurrent(target, notify=True)
        else:
            self._lastCanvasMode = target
            self._applyVSW(target)
            if target == "2d":
                # bar 本来就是 2D：仍把 2D 视口方向纠正为顶视（缩放/平移保留）
                self._view.setViewMode(1)
            elif target == "relief":
                # 只有浮雕模式需要重进编辑态；3d/cnc 模式经 _applyVSW 恢复
                # 左栏即可，误入 _enterReliefEdit 会锁模型/隐藏其他模型
                self._enterReliefEdit()
        # 恢复该文档的视图状态（首次打开则整体观察）
        st = getattr(self._doc, "_savedViewState", None)
        if st is not None:
            self._view.setTransform(st[0])
            self._view.centerOn(st[1])
            self._view._growSceneRect()
        else:
            self._view.zoomToFit()
        self._onSelectionChanged()
        self._layerPanel.refreshLater()
        self._updateFileTabs()
        self._topDock.appendHistory(f"切换文件: {self._doc.title()}")

    def closeEvent(self, event) -> None:
        """关闭软件前检查：有非空且未保存的文档时弹窗提示。
        - 保存全部：逐个保存（无路径弹另存为），任一取消/失败则中止退出；
        - 不保存：直接退出，放弃修改；
        - 取消：返回软件。"""
        dirty = [d for d in self._openDocs if d.isDirty() and not d.isEmpty()]
        if not dirty:
            self._waitBackgroundThreads()
            event.accept()
            return
        names = "\n".join(f"  • {d.title()}" for d in dirty)
        msg = QMessageBox(self)
        msg.setWindowTitle("未保存的修改")
        msg.setIcon(QMessageBox.Question)
        msg.setStyleSheet(
            "QPushButton { min-width: 96px; padding: 6px 18px; }")
        msg.setText(
            f"以下 {len(dirty)} 个文件有未保存的修改：\n{names}\n\n是否在退出前保存？")
        btnSaveAll = msg.addButton("保存全部", QMessageBox.AcceptRole)
        msg.addButton("不保存", QMessageBox.DestructiveRole)
        btnCancel = msg.addButton("取消", QMessageBox.RejectRole)
        msg.setDefaultButton(btnSaveAll)
        msg.exec()
        clicked = msg.clickedButton()
        if clicked == btnCancel:
            event.ignore()
            return
        if clicked == btnSaveAll:
            for d in dirty:
                if not self._saveDoc(d):
                    event.ignore()   # 另存为被取消或保存失败：中止退出
                    return
        self._waitBackgroundThreads()
        event.accept()

    def _waitBackgroundThreads(self, ms: int = 3000) -> None:
        """退出前等待后台 QThread 结束：启动后 10 秒内关窗时 OCCT 预热/
        布尔预热线程可能仍在跑，线程随窗口销毁会触发 "QThread: Destroyed
        while thread is still running" 原生崩溃退出。"""
        for name in ("_heavyLoader", "_preloader", "_bdPreloader", "_bdLoader"):
            t = getattr(self, name, None)
            if t is None:
                continue
            try:
                if t.isRunning():
                    t.requestInterruption()
                    if not t.wait(ms):
                        t.terminate()
                        t.wait(500)
            except Exception:
                pass

    def _renameDocument(self, index: int) -> None:
        """重命名标签文档：未保存文档只改显示标题；已保存文档同步重命名
        磁盘文件（连同同名 .bak/.autosave 副本），保持标题与文件名一致。"""
        import os
        if not (0 <= index < len(self._openDocs)):
            return
        doc = self._openDocs[index]
        old = doc.title()
        name, ok = QInputDialog.getText(self, tr("重命名"), tr("文档名称："),
                                        text=old)
        if not ok:
            return
        name = name.strip()
        if not name or name == old:
            return
        path = doc.filePath()
        if path:
            new_path = os.path.join(os.path.dirname(path),
                                    name + os.path.splitext(path)[1])
            if os.path.exists(new_path):
                QMessageBox.warning(self, tr("重命名失败"),
                                    tr("同名文件已存在:\n%s") % new_path)
                return
            try:
                os.rename(path, new_path)
            except OSError as e:
                QMessageBox.warning(self, tr("重命名失败"),
                                    tr("无法重命名文件:\n%s") % e)
                return
            # 顺带迁移同名备份/自动保存副本（缺失或占用则忽略）
            for ext in (".bak", ".autosave"):
                try:
                    if os.path.exists(path + ext) and \
                            not os.path.exists(new_path + ext):
                        os.rename(path + ext, new_path + ext)
                except OSError:
                    pass
            doc.setFilePath(new_path)   # 更新路径与标题（取新文件主名）
        else:
            doc.setTitle(name)
        self._updateFileTabs()

    def _closeDocument(self, index: int) -> None:
        """关闭标签：未保存先确认；全部关闭后回到新的未命名文档。
        仅剩一个空文档时不允许关闭（菜单已禁用，此处兜底）。"""
        if not (0 <= index < len(self._openDocs)):
            return
        doc = self._openDocs[index]
        if len(self._openDocs) == 1 and doc.isEmpty():
            QMessageBox.information(
                self, "关闭文件", "这是最后一个空文档，不能关闭。")
            return
        if doc.isDirty():
            msg = QMessageBox(self)
            msg.setWindowTitle("关闭文件")
            msg.setIcon(QMessageBox.Question)
            msg.setStyleSheet(
                "QPushButton { min-width: 96px; padding: 6px 18px; }")
            msg.setText(f"“{doc.title()}”有未保存的修改，是否保存？")
            btnSave = msg.addButton("保存", QMessageBox.AcceptRole)
            msg.addButton("不保存", QMessageBox.DestructiveRole)
            btnCancel = msg.addButton("取消", QMessageBox.RejectRole)
            msg.setDefaultButton(btnSave)
            msg.exec()
            clicked = msg.clickedButton()
            if clicked == btnCancel:
                return
            if clicked == btnSave and not self._saveDoc(doc):
                return   # 保存失败/取消另存为：放弃关闭
        self._openDocs.pop(index)
        if not self._openDocs:
            self._docIndex = -1
            self._newDocument()
            return
        if index == self._docIndex:
            self._docIndex = -1   # 强制走切换重绑
            self._switchToDocument(min(index, len(self._openDocs) - 1))
        elif index < self._docIndex:
            self._docIndex -= 1
            self._updateFileTabs()

    def _saveDoc(self, doc) -> bool:
        """保存指定文档（无路径时弹另存为）；返回是否成功（用户取消返回 False）。"""
        if doc.filePath():
            try:
                doc.save(doc.filePath())
                self._notifyTip(f"已保存: {doc.filePath()}")
                self._updateFileTabs()
                return True
            except Exception as e:
                QMessageBox.warning(self, "保存失败", f"无法保存文件:\n{e}")
                return False
        path, _ = QFileDialog.getSaveFileName(
            self, "另存为", "", "Cangai 文件 (*.cai);;所有文件 (*)")
        if not path:
            return False
        if not path.lower().endswith(".cai"):
            path += ".cai"
        try:
            doc.save(path)
            doc.setFilePath(path)
            self._notifyTip(f"已保存: {path}")
            self._updateFileTabs()
            return True
        except Exception as e:
            QMessageBox.warning(self, "保存失败", f"无法保存文件:\n{e}")
            return False

    def _updateFileTabs(self) -> None:
        """刷新右下角文件标签栏与窗口标题。"""
        # 最后一个且为空的文档不可关闭（非空的可关闭，关闭后自动留一个空文档）
        self._fileTabBar.setTabs(
            [d.title() for d in self._openDocs],
            [d.isDirty() for d in self._openDocs],
            self._docIndex,
            closableFlags=[len(self._openDocs) > 1 or not d.isEmpty()
                           for d in self._openDocs])
        title = ("*" if self._doc.isDirty() else "") + self._doc.title()
        self.setWindowTitle(f"{title} - Cangai")   # 标题栏短格式（未命名1 - Cangai）
        self._positionViewSwitchBar()   # 标签宽度变化后重新贴右下角

    def _onImageSelected(self, path: str) -> None:
        """右侧“图片”按钮：选图 → 两点框放命令（先验证图片可解码）。

        3D/CNC 模式下不再切回 2D 画布：直接在当前 3D 视口两点放置，
        图片进入当前模式空间（视口可显示参考图片）；此前会静默把可见
        视图换成 2D 画布，导致图片进了 3D 空间却看不见、方向栏点击
        全部作用在隐藏视口上（观感即“卡住”）。"""
        from core.vector.image_object import ImageObject
        try:
            ImageObject.fromFile(path)
        except Exception as e:
            QMessageBox.warning(self, "图片导入失败", f"无法读取图片:\n{e}")
            return
        if self._cm.hasActive():
            self._cm.cancelActive()
        if self._canvasModeBar.current() not in ("3d", "cnc"):
            self._ensure2DCanvas()
        if self._cm.executeCommand("Image", path):
            self._topDock.appendHistory(f"放置图片: {path}")
        else:
            self._topDock.appendHistory("命令未实现: Image")

    def _onImportFile(self) -> None:
        """左下角导入按钮：DXF 曲线 / SVG 矢量图 / STL 网格 / STEP-BREP 3D 模型
        合并进当前文档；Cangai 文件在新标签中载入。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "导入文件", "",
            "支持的文件 (*.dxf *.svg *.stl *.step *.stp *.brep *.cai *.cag);;"
            "DXF 文件 (*.dxf);;SVG 矢量图 (*.svg);;STL 文件 (*.stl);;"
            "STEP/BREP 3D 模型 (*.step *.stp *.brep);;"
            "Cangai 文件 (*.cai *.cag);;所有文件 (*)")
        if not path:
            return
        low = path.lower()
        if low.endswith(".dxf"):
            self._onImportDXF(path)
            return
        if low.endswith(".svg"):
            self._onImportSVG(path)
            return
        if low.endswith(".stl"):
            self._onImportSTL(path)
            return
        if low.endswith((".step", ".stp", ".brep")):
            self._onImportModel(path)
            return
        self._openFileAsDocument(path)

    def _onImportDXF(self, path: str) -> None:
        """导入 DXF：曲线实体合并进当前文档（一条撤销记录），导入后缩放适配。"""
        if self._cm.hasActive():
            QMessageBox.information(
                self, "无法导入", "请先结束当前命令，再导入 DXF 文件。")
            return
        try:
            from core.export.dxf_importer import importDXF
            stats = importDXF(self._doc, path)
        except Exception as e:
            QMessageBox.warning(self, "导入失败", f"无法导入 DXF 文件:\n{e}")
            return
        if stats["imported"] > 0:
            self._onFitAll()
            msg = f"已导入 DXF：{stats['imported']} 个对象"
            if stats["skipped"]:
                kinds = "、".join(sorted(stats["skippedTypes"])) or "不支持"
                msg += f"；跳过 {stats['skipped']} 个实体（{kinds}）"
            self._notifyTip(msg)
        else:
            QMessageBox.information(
                self, "导入结果", "该 DXF 文件中没有可导入的曲线实体。")

    def _onImportSVG(self, path: str) -> None:
        """导入 SVG：路径/形状元素转为曲线对象合并进当前文档（一条撤销记录），
        导入后缩放适配。"""
        if self._cm.hasActive():
            QMessageBox.information(
                self, "无法导入", "请先结束当前命令，再导入 SVG 文件。")
            return
        try:
            from core.export.svg_importer import importSVG
            stats = importSVG(self._doc, path)
        except Exception as e:
            QMessageBox.warning(self, "导入失败", f"无法导入 SVG 文件:\n{e}")
            return
        if stats["imported"] > 0:
            self._onFitAll()
            msg = f"已导入 SVG：{stats['imported']} 个对象"
            if stats["skipped"]:
                msg += f"；跳过 {stats['skipped']} 个不支持的元素"
            self._notifyTip(msg)
        else:
            QMessageBox.information(
                self, "导入结果", "该 SVG 文件中没有可导入的矢量图形。")

    def _onImportSTL(self, path: str) -> None:
        """导入 STL：按当前画布模式分流——3D 模式导入为 3D 网格模型
        （BRepObject，入 3D 空间）；2D/浮雕/CNC 模式光栅化为浮雕模型合并
        进当前文档。均一条撤销记录，导入后视图适配。"""
        if self._cm.hasActive():
            QMessageBox.information(
                self, "无法导入", "请先结束当前命令，再导入 STL 文件。")
            return
        if self._canvasModeBar.current() == "3d":
            self._importSTLAsModel3D(path)
            return
        before = {o.id() for o in self._doc.reliefModels()}
        try:
            from core.export.stl_importer import importSTL
            stats = importSTL(self._doc, path)
        except Exception as e:
            QMessageBox.warning(self, "导入失败", f"无法导入 STL 文件:\n{e}")
            return
        if stats["imported"] > 0:
            # 自动选中新模型：浮雕模式只渲染 activeReliefModel，
            # 不选中则切到浮雕模式视口空白（必须先 2D 选中/重构才可见）
            new_models = [o for o in self._doc.reliefModels()
                          if o.id() not in before]
            if new_models:
                self._doc.selectObject(new_models[-1].id())
                if self._canvasModeBar.current() == "relief":
                    # 已在浮雕模式：解锁旧编辑目标后锁定新模型，立即显示
                    self._exitReliefLock()
                    self._enterReliefEdit()
            self._onFitAll()
            msg = (f"已导入 STL 浮雕：{stats['triangles']} 个三角面，"
                   f"栅格 {stats['dims'][0]}×{stats['dims'][1]}")
            if stats["skipped"]:
                msg += f"；跳过 {stats['skipped']} 个退化面"
            self._notifyTip(msg)

    def _importSTLAsModel3D(self, path: str) -> None:
        """STL → 3D 网格模型（3D 模式导入）：build123d import_stl（RWStl
        直读三角网格）→ BRepObject 并入 3D 空间（一条撤销记录），
        相机适配到模型包围盒整体观察。"""
        try:
            from core.vector.brep_shape import BrepShape, _import_bd
            from core.vector.brep_object import BRepObject
            from core.vector.geometry_types import Color3D
            bd = _import_bd()
            part = bd.import_stl(path)
            obj = BRepObject()
            obj.setShape(BrepShape(part))
            obj.setColor(Color3D(0.2, 0.71, 0.9, 1.0))
            self._doc.beginEdit()
            try:
                self._doc.addObject(obj, space="3d")
            finally:
                self._doc.endEdit()
        except Exception as e:
            QMessageBox.warning(self, "导入失败", f"无法导入 STL 文件:\n{e}")
            return
        try:
            bb = obj.getBoundingBox()
            if bb is not None:
                self._viewport3D.fitToBounds(bb)
        except Exception:
            self._onFitAll()
        self._doc.selectObject(obj.id())
        self._notifyTip(f"已导入 STL 3D 模型：{path}")

    # ---------------- 画布拖入导入（图片 png/jpg / DXF / STL） ----------------

    def _onCanvasFileDrop(self, paths: list, worldPt) -> None:
        """画布/视口拖入文件：图片在落点放置；DXF/STL/STEP/BREP 走对应导入；
        CAI/CAG 工程文件打开为新文档。"""
        images, dxfs, stls, models, docs = [], [], [], [], []
        for p in paths:
            low = p.lower()
            if low.endswith((".png", ".jpg", ".jpeg")):
                images.append(p)
            elif low.endswith(".dxf"):
                dxfs.append(p)
            elif low.endswith(".stl"):
                stls.append(p)
            elif low.endswith((".step", ".stp", ".brep")):
                models.append(p)
            elif low.endswith((".cai", ".cag")):
                docs.append(p)
        for p in dxfs:
            self._onImportDXF(p)
        for p in stls:
            self._onImportSTL(p)
        for p in models:
            self._onImportModel(p)
        for p in docs:
            self._openFileAsDocument(p)
        if images:
            self._placeImagesAt(images, worldPt)

    def _onImportModel(self, path: str) -> None:
        """导入 STEP/BREP 3D 模型：读入 build123d → BRepObject 合并进当前文档，
        并切到 3D 视口整体观察。"""
        if self._cm.hasActive():
            QMessageBox.information(
                self, "无法导入", "请先结束当前命令，再导入 3D 模型。")
            return
        try:
            from core.vector.brep_shape import BrepShape, _import_bd
            from core.vector.brep_object import BRepObject
            from core.vector.geometry_types import Color3D
            bd = _import_bd()
            if path.lower().endswith(".brep"):
                part = bd.import_brep(path)
            else:
                part = bd.import_step(path)
            obj = BRepObject()
            obj.setShape(BrepShape(part))
            obj.setColor(Color3D(0.2, 0.71, 0.9, 1.0))
            self._doc.addObject(obj)
        except Exception as e:
            QMessageBox.warning(self, "导入失败", f"无法导入 3D 模型:\n{e}")
            return
        if self._canvasModeBar.current() != "3d":
            self._canvasModeBar.setCurrent("3d", notify=True)
        self._onFitAll()
        self._notifyTip(f"已导入 3D 模型：{path}")

    def _openFileAsDocument(self, path: str) -> bool:
        """把 Cangai 工程文件打开为文档（已打开则切换过去），成功返回 True。"""
        try:
            exist = next((d for d in self._openDocs
                          if d.filePath() == path), None)
            if exist is not None:
                self._switchToDocument(self._openDocs.index(exist))
                return True
            self._newDocument(path)
            return True
        except Exception as e:
            QMessageBox.warning(self, "打开失败", f"无法打开文件 {path}:\n{e}")
            return False

    def _placeImagesAt(self, paths: list, worldPt) -> None:
        """把拖入图片直接落到落点（默认宽 100 等比，多张依次错开），
        一次撤销记录。

        3D/CNC 模式：不切回 2D 画布，图片进当前模式空间，落点取拖入
        的地面点（无效时取 3D 视口中心的地面交点）；2D/浮雕模式维持
        原行为（落点无效时放 2D 视图中心）。"""
        if self._cm.hasActive():
            self._cm.cancelActive()
        mode = self._canvasModeBar.current()
        center = worldPt
        if mode in ("3d", "cnc"):
            if center is None:
                try:
                    vp = self._viewport3D
                    g = vp._screenToGround(
                        vp.width() / 2.0, vp.height() / 2.0,
                        vp._camera(), float(vp.width()), float(vp.height()))
                    center = Point3D(g[0], g[1], 0.0) if g \
                        else Point3D(0.0, 0.0, 0.0)
                except Exception:
                    center = Point3D(0.0, 0.0, 0.0)
        else:
            self._ensure2DCanvas()
            if center is None:
                vp = self._view.viewport()
                sp = self._view.mapToScene(vp.rect().center())
                center = Point3D(sp.x(), sp.y(), 0.0)
        from core.vector.image_object import ImageObject
        failed = []
        self._doc.beginEdit()
        try:
            for i, p in enumerate(paths):
                try:
                    obj = ImageObject.fromFile(p)
                except Exception as e:
                    failed.append(f"{p}（{e}）")
                    continue
                iw, ih = obj.pixelSize()
                w = 100.0
                h = w * (ih / max(iw, 1))
                obj.setGeometry(Point3D(center.x + i * 25.0,
                                        center.y - i * 25.0, 0.0), w, h, 0.0)
                self._doc.addObject(obj)
        finally:
            self._doc.endEdit()
        if failed:
            QMessageBox.warning(self, "图片导入失败",
                                "无法读取图片:\n" + "\n".join(failed))
        self._notifyTip(f"已放置 {len(paths) - len(failed)} 张图片"
                        "（默认尺寸，可拖动调整）")

    def _onOpenFile(self) -> None:
        """打开文件：支持多选，每个文件一个标签；已打开的文件直接切换过去。"""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "打开文件", "", "Cangai 文件 (*.cai *.cag);;所有文件 (*)")
        if not paths:
            return
        last = None
        for path in paths:
            if self._openFileAsDocument(path):
                last = self._docIndex
        if last is not None:
            self._switchToDocument(last)

    def _onCalcCost(self) -> None:
        """材料成本计算器：体积 × 密度 × 单价。"""
        from ui.widgets.material_calc_dialog import MaterialCalcDialog
        dlg = MaterialCalcDialog(self)
        dlg.exec()

    def _onSaveFile(self) -> None:
        self._saveDoc(self._doc)

    def _onSaveFileAs(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "另存为", "", "Cangai 文件 (*.cai);;所有文件 (*)")
        if not path:
            return
        if not path.lower().endswith(".cai"):
            path += ".cai"
        self._saveTo(path)

    def _saveTo(self, path: str) -> None:
        import os
        import shutil
        # 保存前备份：覆盖已有文件前先复制 <文件名>.bak（设置-文件页可关）
        if getattr(app_settings, "backup_on_save", True) and os.path.exists(path):
            try:
                shutil.copy2(path, path + ".bak")
            except Exception:
                pass
        try:
            self._doc.save(path)
            self._doc.setFilePath(path)
            # 落盘成功即删除对应的自动保存恢复副本
            autosave = path + ".autosave"
            if os.path.exists(autosave):
                try:
                    os.remove(autosave)
                except OSError:
                    pass
            self._notifyTip(f"已保存: {path}")
            self._updateFileTabs()
        except Exception as e:
            QMessageBox.warning(self, "保存失败", f"无法保存文件:\n{e}")

    def _onQuickSave(self, slotIndex: int, filename: str) -> None:
        import core.app_settings as app_settings
        import os
        slot = app_settings.file_save_slots[slotIndex]
        dirPath = slot["path"]
        if not dirPath:
            QMessageBox.warning(self, "保存失败", "请先在设置中配置保存路径")
            return
        if not filename:
            filename = slot["name"]
        if not filename.lower().endswith(".cai"):
            filename += ".cai"
        path = os.path.join(dirPath, filename)
        self._saveTo(path)

    def _onExport(self, fmt: str, filename: str) -> None:
        import os
        ext = fmt.lower()
        if not filename:
            filename = f"export.{ext}"
        # 清理输入框文件名中的非法字符
        for ch in '<>:"/\\|?*':
            filename = filename.replace(ch, "_")
        filename = filename.strip().strip(".") or f"export.{ext}"
        if not filename.lower().endswith(f".{ext}"):
            filename += f".{ext}"
        if fmt in ("DXF", "SVG", "STEP", "STL"):
            # 直接导出到桌面，不弹保存对话框；
            # 同名文件自动在主名后加序号 (1)、(2)…，避免覆盖
            path = _uniqueExportPath(os.path.join(_desktopDir(), filename))
        else:
            path, _ = QFileDialog.getSaveFileName(
                self, f"导出 {fmt}", filename,
                f"{fmt} 文件 (*.{ext});;所有文件 (*)")
            if not path:
                return
        try:
            if fmt == "DXF":
                from core.export.dxf_exporter import exportDXF
                exportDXF(self._doc, path)
            elif fmt == "SVG":
                # SVG：曲线对象采样为矢量路径（描边轮廓，供看图/雕刻软件用）
                from core.export.svg_exporter import exportSVG
                n = exportSVG(self._doc, path)
                self._notifyTip(f"已导出 {n} 条曲线: {path}")
                return
            elif fmt == "STEP":
                # STEP：3D B-Rep 实体精确导出（挤出/布尔等）。
                # 优先导出选中的实体；无选中时导出全部实体。
                from core.export.step_exporter import exportSTEP
                solids = None
                if self._doc.space() == "3d":
                    sel = [o for o in self._doc.selectedObjects()
                           if o.type() in (ObjectType.SOLID,
                                            ObjectType.BREP)]
                    if sel:
                        solids = sel
                    else:
                        all_sol = [o for o in self._doc.allObjects()
                                   if o.type() in (ObjectType.SOLID,
                                                    ObjectType.BREP)]
                        if all_sol:
                            solids = all_sol
                exportSTEP(self._doc, path, solids=solids)
            elif fmt == "STL":
                from core.export.stl_exporter import exportSTL
                # 3D 模式下优先导出选中的挤出实体（闭合网格）；
                # 无选中或非 3D 模式时回退到导出全部浮雕模型
                solids = None
                if self._doc.space() == "3d":
                    sel = [o for o in self._doc.selectedObjects()
                           if o.type() in (ObjectType.SOLID,
                                            ObjectType.BREP)]
                    if sel:
                        solids = sel
                    else:
                        # 3D 无选中：导出全部实体（含挤出圆环等），
                        # 而不是回退浮雕（3D 文档可能没有浮雕模型）
                        all_sol = [o for o in self._doc.allObjects()
                                   if o.type() in (ObjectType.SOLID,
                                                    ObjectType.BREP)]
                        if all_sol:
                            solids = all_sol
                exportSTL(self._doc, path, solids=solids)
            self._notifyTip(f"已导出: {path}")
        except ImportError:
            QMessageBox.information(self, "暂未实现", f"{fmt} 导出功能尚未实现")
        except Exception as e:
            QMessageBox.warning(self, "导出失败", f"无法导出文件:\n{e}")

    def _refreshStyle(self) -> None:
        QCoreApplication.instance().setStyleSheet(theme.globalQss())
        self._dockManager.setStyleSheet(theme.dockManagerQss())
        self._titleBar.refreshStyle()
        for btn in self._leftPanel.tabButtons():
            btn.updateHighlightColor(theme.PRIMARY)
        self._leftPanel.refreshFileButtonsStyle()
        self._topDock.refreshStyle()
        self._captureToolbar.refreshStyle()
        self._statusBar.refreshStyle()
        self._viewSwitchBar.refreshStyle()
        self._canvasModeBar.refreshStyle()
        self._transformPanel.refreshStyle()
        # 选中高亮色变化后重绘 2D 画布与 3D 视口
        self._scene.update()
        self._viewport3D.update()

    def _applyDwmRoundCorners(self) -> None:
        """Windows 11：给无边框窗口启用系统圆角（Win10 / 失败时静默跳过）。"""
        try:
            import ctypes
            hwnd = int(self.winId())
            pref = ctypes.c_int(2)   # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(pref), 4)   # 33 = DWMWA_WINDOW_CORNER_PREFERENCE
        except Exception:
            pass

    def _enableTaskbarMinMax(self) -> None:
        """无边框窗口补回 WS_THICKFRAME/MINIMIZEBOX/MAXIMIZEBOX/SYSMENU 样式位：
        - WS_THICKFRAME：没有它 Windows 不执行边缘拖拽缩放（WM_NCHITTEST
          的 HT* 命中码只报告位置，真正进入缩放循环需要厚边框样式；
          非客户区视觉由 nativeEvent 的 WM_NCCALCSIZE=0 抹平）；
        - MINIMIZEBOX/MAXIMIZEBOX/SYSMENU：否则单击任务栏图标无法
          最小化/还原（FramelessWindowHint 移除了这些位）。"""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            GWL_STYLE = -16
            WS_THICKFRAME = 0x00040000
            WS_MAXIMIZEBOX = 0x00010000
            WS_MINIMIZEBOX = 0x00020000
            WS_SYSMENU = 0x00080000
            GetWLP = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
            SetWLP = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            hwnd = int(self.winId())
            style = GetWLP(hwnd, GWL_STYLE)
            style |= WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU
            SetWLP(hwnd, GWL_STYLE, style)
            # SWP_FRAMECHANGED 让新样式立即生效（不移动不缩放）
            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                                0x2 | 0x1 | 0x4 | 0x20)
        except Exception:
            pass

    # ---- 无边框窗口原生边框缩放（WM_NCHITTEST）----
    # 返回 HT* 命中码后 Windows 直接进入系统缩放循环：自带缩放光标、
    # 原生边框拖拽与贴边分屏，Qt 层不会收到边缘区的鼠标按下事件。
    _HT = {"L": 10, "R": 11, "T": 12, "TL": 13, "TR": 14,
           "B": 15, "BL": 16, "BR": 17}   # HTLEFT..HTBOTTOMRIGHT

    def nativeEvent(self, eventType, message):
        if eventType == b"windows_generic_MSG":
            try:
                import ctypes
                from ctypes import wintypes
                msg = wintypes.MSG.from_address(int(message))
                # 0x0083 = WM_NCCALCSIZE：WS_THICKFRAME 的非客户区（粗边框）
                # 整体抹平为 0 —— 整个窗口即客户区，外观仍是全无边框，
                # 但系统缩放循环（HT* 命中 → 拖拽缩放）得以生效
                if msg.message == 0x0083 and msg.wParam:
                    return True, 0
                # 0x0084 = WM_NCHITTEST；最大化/全屏时不提供缩放边框
                if msg.message == 0x0084 and \
                        not self.isMaximized() and not self.isFullScreen():
                    x = msg.lParam & 0xFFFF
                    y = (msg.lParam >> 16) & 0xFFFF
                    if x >= 0x8000:
                        x -= 0x10000
                    if y >= 0x8000:
                        y -= 0x10000
                    rect = wintypes.RECT()
                    if ctypes.windll.user32.GetWindowRect(
                            int(self.winId()), ctypes.byref(rect)):
                        m = max(6, round(6 * self.devicePixelRatioF()))
                        l = x - rect.left <= m
                        r = rect.right - x <= m
                        t = y - rect.top <= m
                        b = rect.bottom - y <= m
                        ht = None
                        if l and t:
                            ht = self._HT["TL"]
                        elif r and t:
                            ht = self._HT["TR"]
                        elif l and b:
                            ht = self._HT["BL"]
                        elif r and b:
                            ht = self._HT["BR"]
                        elif l:
                            ht = self._HT["L"]
                        elif r:
                            ht = self._HT["R"]
                        elif t:
                            ht = self._HT["T"]
                        elif b:
                            ht = self._HT["B"]
                        if ht is not None:
                            return True, ht
            except Exception:
                pass
        return super().nativeEvent(eventType, message)

    def changeEvent(self, event: QEvent) -> None:
        """窗口状态变化：
        1) 无边框窗口被系统最大化时会盖住任务栏 → 修正到工作区几何；
        2) 还原时恢复最大化前几何；3) 同步标题栏最大化/还原图标。"""
        if event.type() == QEvent.WindowStateChange:
            st = self.windowState()
            maximized = bool(st & Qt.WindowMaximized)
            if maximized:
                avail = self.screen().availableGeometry()
                if self.geometry() != avail:
                    self.setGeometry(avail)
            elif not (st & (Qt.WindowFullScreen | Qt.WindowMinimized)):
                if self._preMaxGeo is not None:
                    self.setGeometry(self._preMaxGeo)
                    self._preMaxGeo = None
            if getattr(self, "_titleBar", None) is not None:
                self._titleBar.syncMaxButton(maximized)
        super().changeEvent(event)

    # 无边框窗口边缘热区宽度（像素）：靠边按住即触发系统缩放
    RESIZE_MARGIN = 5

    def _windowEdgeAt(self, globalPos) -> "Qt.Edges":
        """全局坐标 → 无边框窗口边缘方向组合（空 Edges = 不在边缘）。"""
        if self.isMaximized() or self.isFullScreen():
            return Qt.Edges()
        g = self.geometry()
        m = self.RESIZE_MARGIN
        x, y = globalPos.x() - g.left(), globalPos.y() - g.top()
        w, h = g.width(), g.height()
        edges = Qt.Edges()
        if x <= m:
            edges |= Qt.LeftEdge
        if w - x <= m:
            edges |= Qt.RightEdge
        if y <= m:
            edges |= Qt.TopEdge
        if h - y <= m:
            edges |= Qt.BottomEdge
        return edges

    def eventFilter(self, obj: object, event: QEvent) -> bool:
        et = event.type()
        # 设置 → 键盘 里登记的快捷键（浮雕类）：全局拦截按键并触发命令
        if et == QEvent.KeyPress and self._tryShortcut(event):
            return True
        # 无边框窗口边缘缩放：任何子控件上靠窗缘按下左键 → 原生缩放循环
        if (et == QEvent.MouseButtonPress and isinstance(obj, QWidget)
                and obj.window() is self
                and not self.isMaximized() and not self.isFullScreen()
                and event.button() == Qt.LeftButton):
            edge = self._windowEdgeAt(event.globalPosition().toPoint())
            if edge:
                handle = self.windowHandle()
                if handle is not None:
                    handle.startSystemResize(edge)
                    return True
        if (obj is getattr(self, "_canvasContainer", None)
                and et == QEvent.Resize):
            self._positionViewSwitchBar()
        return super().eventFilter(obj, event)

    # ---------------- 设置 → 键盘：快捷键映射 ----------------
    def _reloadShortcuts(self) -> None:
        """按 app_settings.shortcuts 重建“按键 → 命令”映射。

        只有功能名登记在 app_settings.shortcut_commands 里的行才会生效；
        撤销/重做/正交等由主窗口既有逻辑处理，不在此重复绑定。
        """
        self._shortcutMap = {}
        cmds = getattr(app_settings, "shortcut_commands", {})
        for row in getattr(app_settings, "shortcuts", []):
            if len(row) >= 4:
                func, key = row[1], row[2]
            elif len(row) == 3:
                func, key = row[0], row[1]
            else:
                continue
            key = (key or "").strip().replace(" ", "")
            cmd = cmds.get(func)
            if key and cmd:
                # 值可以是 "命令名" 或 ("命令名", 初始参数, ...)：
                # 后者用于同一命令的不同变体（堆料/去料）
                self._shortcutMap[key] = (cmd,) if isinstance(cmd, str) else tuple(cmd)

    @staticmethod
    def _eventKeyText(event: QKeyEvent) -> str:
        """把按键事件格式化为与设置页捕获框一致的字符串（Ctrl+Shift+A）。"""
        mods = event.modifiers()
        parts = []
        if mods & Qt.ControlModifier:
            parts.append("Ctrl")
        if mods & Qt.ShiftModifier:
            parts.append("Shift")
        if mods & Qt.AltModifier:
            parts.append("Alt")
        parts.append(QKeySequence(event.key()).toString())
        return "+".join(parts)

    def _tryShortcut(self, event: QKeyEvent) -> bool:
        """命中设置页登记的快捷键则执行命令。焦点在文本输入控件、
        或主窗口不是活动窗口（弹出了设置等对话框）时不拦截。"""
        if not hasattr(self, "_shortcutMap"):
            self._reloadShortcuts()
        if not self._shortcutMap:
            return False
        from PySide6.QtWidgets import (QApplication, QLineEdit, QAbstractSpinBox,
                                       QTextEdit, QPlainTextEdit, QComboBox)
        if QApplication.activeWindow() is not self:
            return False
        fw = QApplication.focusWidget()
        if isinstance(fw, (QLineEdit, QAbstractSpinBox, QTextEdit,
                           QPlainTextEdit, QComboBox)):
            return False
        if event.key() in (Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt,
                           Qt.Key_Meta):
            return False
        hit = self._shortcutMap.get(self._eventKeyText(event))
        if not hit:
            return False
        self._onCommandRequested(hit[0], *hit[1:])
        event.accept()
        return True

    def _positionViewSwitchBar(self) -> None:
        c = self._canvasContainer
        bar = self._viewSwitchBar
        bar.move(c.width() - bar.width(), 20)
        bar.raise_()
        bar.show()
        modeBar = self._canvasModeBar
        modeBar.move((c.width() - modeBar.width()) // 2, 2)
        modeBar.raise_()
        modeBar.show()
        # 文件标签栏：画布右下角（左下角已被步骤提示条占用）
        tb = self._fileTabBar
        tb.move(c.width() - tb.width() - 10, c.height() - tb.height() - 4)
        tb.raise_()
        tb.show()
        # 参数面板：贴画布右缘、参数按钮左侧，顶部对齐（宽度固定，高度随内容）
        self._positionParamHost()

    def _positionParamHost(self) -> None:
        c = self._canvasContainer
        bar = self._viewSwitchBar
        host = self._paramHost
        # 贴画布右缘：紧挨右侧功能栏（仅留 2px 缝），顶部与功能栏对齐
        host.move(max(2, c.width() - bar.width() - host.width() - 2), bar.y())
        host.raise_()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key_Escape:
            self._cm.cancelActive()
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self._cm.onEnter()
            return
        # 撤销 / 重做（Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y）；先结束活动命令再恢复。
        # 快捷键作用于“当前模式对应的那套历史”：2D = 曲线历史，浮雕模式 = 浮雕历史。
        if event.modifiers() & Qt.ControlModifier and \
                event.key() in (Qt.Key_Z, Qt.Key_Y):
            if self._cm.hasActive():
                self._cm.cancelActive()
            if self._canvasModeBar.current() == "relief":
                docUndo, docRedo = self._doc.undoRelief, self._doc.redoRelief
            else:
                docUndo, docRedo = self._doc.undoCurve, self._doc.redoCurve
            if event.key() == Qt.Key_Z and \
                    not (event.modifiers() & Qt.ShiftModifier):
                docUndo()
            else:
                docRedo()
            event.accept()
            return
        # 复制（Ctrl+C）：克隆选中对象到内部剪贴板
        if event.modifiers() & Qt.ControlModifier and event.key() == Qt.Key_C:
            self._copySelection()
            event.accept()
            return
        # 粘贴（Ctrl+V）：把剪贴板对象克隆入文档并偏移
        if event.modifiers() & Qt.ControlModifier and event.key() == Qt.Key_V:
            self._pasteClipboard()
            event.accept()
            return
        # 正交切换（F8）
        if event.key() == Qt.Key_F8:
            self._statusBar.setOrthoChecked(
                not self._statusBar.isOrthoChecked())
            event.accept()
            return
        # 删除键：删除选中对象（可撤销）。命令激活期间默认禁止（命令可能
        # 持有目标引用）；但"点击即生效"类命令在空闲态放行（其结果被自动
        # 选中，禁止会让用户碰到"实体选中却删不掉"），拒绝时给出提示。
        if event.key() == Qt.Key_Delete:
            inst = (self._cm.activeInstance()
                    if self._cm is not None and self._cm.hasActive() else None)
            if inst is None or inst.allowsSelectionDelete():
                objs = list(self._doc.selectedObjects())
                if not objs and self._viewport3D is not None:
                    # 3D 子元素选中（体/面/边/点高亮）不写文档选中态：
                    # Del 删除其宿主对象（同"选中实体按 Del 删除"直觉）
                    objs = self._viewport3D.subSelectedObjects()
                if objs:
                    self._doc.beginEdit()
                    for o in objs:
                        self._doc.removeObject(o.id())
                    self._doc.endEdit()
                    if self._viewport3D is not None:
                        self._viewport3D.clearSubSelection()
                else:
                    self._notifyTip("没有可删除的选中对象")
            else:
                self._notifyTip("命令进行中：Esc 结束命令后即可删除选中对象")
            event.accept()
            return
        # 命令激活期间的扩展按键（如画线 C 键闭合）
        if self._cm.onKeyPress(event.key(), event.modifiers()):
            return
        super().keyPressEvent(event)

    def _copySelection(self) -> None:
        """Ctrl+C：克隆选中对象到内部剪贴板（可重复粘贴）。"""
        sel = self._doc.selectedObjects()
        self._clipboard = [o.clone() for o in sel]

    def _pasteClipboard(self) -> None:
        """Ctrl+V：把剪贴板对象克隆入文档并偏移，作为一次可撤销操作。"""
        if not self._clipboard or self._cm.hasActive():
            return
        self._doc.beginEdit()
        for obj in self._clipboard:
            clone = obj.clone()
            clone.translate(10.0, 10.0, 0.0)
            self._doc.addObject(clone)
        self._doc.endEdit()
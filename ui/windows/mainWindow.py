"""主窗口：负责整体布局（Dock 停靠、工具条）与全局信号接线。

所有业务交互在此汇聚：命令请求、捕捉开关、模式切换、选择联动等。
"""
from __future__ import annotations

import math

from PySide6.QtWidgets import (QMainWindow, QWidget, QVBoxLayout,
                               QStackedWidget, QFileDialog, QMessageBox,
                               QScrollArea)
from PySide6.QtCore import Qt, QCoreApplication, QTimer, QEvent
from PySide6.QtGui import QShowEvent, QKeyEvent, QTransform

from PySide6QtAds import (CDockManager, CDockWidget, CDockAreaWidget,
                          LeftDockWidgetArea, RightDockWidgetArea,
                          BottomDockWidgetArea, CenterDockWidgetArea)

from core.vector.geometry_types import Point3D, BoundingBox3D, ObjectType
from core.document import Document
from core import app_settings
from core.snap.snap_manager import SnapManager
from core.snap.snap_types import SnapType
from core.grid_manager import GridManager
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
from ui.commands.concrete.image_command import ImagePlaceCommand
from ui.commands.concrete.image_trace_command import ImageTraceCommand
from ui.commands.concrete.dimension_commands import registerAll as registerDims
from ui.commands.concrete.dimension_commands import TEXT_TO_OBJ_GAP
from core.vector.dimension_object import DimensionObject
from ui.gizmo.gizmo_manager import GizmoManager, View2DAdapter, View3DAdapter

from ui.widgets.status_bar import StatusBarWidget
from ui.widgets.top_dock import TopDock
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


_SNAP_KEY_MAP = {
    "endpoint": SnapType.ENDPOINT, "nearest": SnapType.NEAREST,
    "point": SnapType.POINT, "midpoint": SnapType.MIDPOINT,
    "center": SnapType.CENTER, "intersection": SnapType.INTERSECTION,
    "perpendicular": SnapType.PERPENDICULAR, "tangent": SnapType.TANGENT,
    "quarter": SnapType.QUARTER, "vertex": SnapType.VERTEX,
}


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Cangai-Relief 浮雕设计")
        self.resize(1280, 800)
        QCoreApplication.instance().setStyleSheet(theme.globalQss())

        self._doc = Document()
        # 多文件系统：打开的文档列表与当前索引；每个文档独立对象集/撤销历史。
        # self._doc 始终指向当前活动文档，切换时整体重绑（见 _switchToDocument）。
        self._openDocs: list = [self._doc]
        self._docIndex: int = 0
        self._untitledSeq: int = 1
        self._doc.setTitle("未命名1")
        self._clipboard: list = []   # 复制/粘贴：保存选中对象的克隆
        # 浮雕编辑态：锁定的模型（不可选中/变换，仅浮雕/Z 向操作）及其 id 恢复号
        self._reliefLockedModel = None
        self._reliefRestoreId: str | None = None
        self._scene = GraphicsScene()
        self._view = GraphicsView(self._scene)
        self._view.setDocument(self._doc)
        self._bridge = SceneBridge(self._scene, self._doc)
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
        self._cm.register("Image", ImagePlaceCommand)   # 图片放置（参数：路径）
        self._cm.register("ImageTrace", ImageTraceCommand)   # 图片转线稿
        registerDims(self._cm)   # 尺寸标注命令
        self._view.setCommandManager(self._cm)


        self._statusBar = StatusBarWidget()
        self.setStatusBar(self._statusBar)

        self._topDock = TopDock()
        self._leftPanel = LeftPanelWidget()
        self._layerPanel = LayerPanel()
        self._propertyPanel = PropertyPanel()
        self._transformPanel = TransformPanel()
        self._transformPanel.setDocument(self._doc)
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

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not getattr(self, "_dockAdjusted", False):
            self._dockAdjusted = True
            QTimer.singleShot(0, self._adjustDockHeights)

    def _adjustDockHeights(self) -> None:
        self._leftPanel.setExpandedWidth(240)
        self._resizeLeftDock(self._leftPanel.isExpanded())
        self._positionViewSwitchBar()


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
        self.setCentralWidget(container)

        self._view.setMinimumSize(400, 300)
        self._viewport3D = Viewport3D()
        # 3D/浮雕视口也支持命令取点（默认画在 XY 平面）：接入文档与命令调度，
        # 对象与命令预览由视口自行投影绘制；对象变化信号驱动重绘已在视口内连接
        self._viewport3D.setDocument(self._doc)
        self._viewport3D.setCommandManager(self._cm)
        # 浮雕模式全局操作（空格填色）的状态反馈接入历史栏
        self._viewport3D.statusMessage.connect(self._topDock.appendHistory)
        self._active3D = False
        canvasContainer = QWidget()
        canvasLayout = QVBoxLayout(canvasContainer)
        canvasLayout.setContentsMargins(0, 0, 0, 0)
        canvasLayout.setSpacing(0)
        self._viewStack = QStackedWidget()
        self._viewStack.addWidget(self._view)          # 0: 2D 画布
        self._viewStack.addWidget(self._viewport3D)    # 1: 3D/浮雕视口
        canvasLayout.addWidget(self._viewStack)
        self._canvasContainer = canvasContainer
        self._viewSwitchBar = ViewSwitchBar(canvasContainer)
        self._canvasModeBar = CanvasModeBar(canvasContainer)
        self._paramHost = CommandParamHost(canvasContainer)
        self._paramHost.hide()
        # 多文件标签栏：画布右下角悬浮（切换/关闭/“＋”新建）
        self._fileTabBar = FileTabBar(canvasContainer)
        self._layerPanel.setDocument(self._doc)
        self._viewSwitchBar.setLocked(True)  # 默认 2D 模式：视图锁定顶视图
        canvasContainer.installEventFilter(self)

        centerDock = CDockWidget("画布")
        centerDock.setWidget(canvasContainer)
        self._dockManager.addDockWidget(CenterDockWidgetArea, centerDock)
        centerArea = centerDock.dockAreaWidget()
        if centerArea is not None:
            centerArea.setDockAreaFlag(CDockAreaWidget.HideSingleWidgetTitleBar, True)
            centerArea.setContentsMargins(0, 0, 0, 0)

        self._leftPanel.setMinimumHeight(200)
        self._leftDock = CDockWidget("文件")
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
        self._transformPanel.setMinimumWidth(220)
        self._transformPanel.setMinimumHeight(150)
        transformDock = CDockWidget("变换")
        transformDock.setWidget(self._transformPanel)
        transformDock.setFeature(F.DockWidgetClosable, False)
        self._dockManager.addDockWidget(RightDockWidgetArea, transformDock)

        self._layerPanel.setMinimumWidth(220)
        self._layerPanel.setMinimumHeight(150)
        layerDock = CDockWidget("图层")
        layerDock.setWidget(self._layerPanel)
        layerDock.setFeature(F.DockWidgetClosable, False)
        # 右侧区域上下堆叠：变换在上，图层停靠在其下方
        self._dockManager.addDockWidget(BottomDockWidgetArea, layerDock,
                                        transformDock.dockAreaWidget())

        self._propertyPanel.setMinimumWidth(220)
        self._propertyPanel.setMinimumHeight(150)
        propDock = CDockWidget("属性")
        propDock.setWidget(self._propertyPanel)
        propDock.setFeature(F.DockWidgetClosable, False)
        self._propDock = propDock
        self._dockManager.addDockWidgetTabToArea(propDock, layerDock.dockAreaWidget())
        layerDock.dockAreaWidget().setCurrentIndex(0)

        # 初始高度比例：变换在上、图层（含属性 tab）在下，变换占更多高度
        layerArea = layerDock.dockAreaWidget()
        splitter = layerArea.parentSplitter() if layerArea else None
        if splitter is not None:
            splitter.setSizes([300, 140])



        self.addToolBar(Qt.BottomToolBarArea, self._captureToolbar)

    def _connectSignals(self) -> None:
        self._topDock.commandRequested.connect(self._onCommandRequested)
        self._cm.commandRepeated.connect(
            lambda name: self._topDock.appendHistory(f"右键重复命令: {name}"))
        self._view.mouseMoved.connect(self._onMouseMoved)
        self._captureToolbar.snapTypeToggled.connect(self._onSnapToggled)
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
        # 文档侧信号（历史可用状态/选择/对象/图层/未保存）随文档切换重绑
        self._bindDocSignals(quickBar)
        self._statusBar.lineWidthChanged.connect(self._onLineWidthChanged)
        self._statusBar.measureToggled.connect(self._updateMeasure)
        self._leftPanel.expandedChanged.connect(self._onLeftPanelExpanded)
        self._leftPanel.clearRecentRequested.connect(
            self._leftPanel.recentList().clear)

        self._leftPanel.settingsRequested.connect(self._onShowSettings)
        self._leftPanel.importRequested.connect(self._onImportFile)
        self._leftPanel.openRequested.connect(self._onOpenFile)
        self._leftPanel.saveRequested.connect(self._onSaveFile)
        self._leftPanel.saveAsRequested.connect(self._onSaveFileAs)
        self._leftPanel.quickSaveRequested.connect(self._onQuickSave)
        self._leftPanel.exportRequested.connect(self._onExport)

        # 多文件标签栏：切换 / 关闭 / ＋新建
        self._fileTabBar.currentChanged.connect(self._switchToDocument)
        self._fileTabBar.closeRequested.connect(self._closeDocument)
        self._fileTabBar.newRequested.connect(lambda: self._newDocument())
        self._transformPanel.mirrorRequested.connect(self._onMirrorRequested)
        self._transformPanel.zHeightCommitRequested.connect(self._onModelZCommit)
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


        self._viewSwitchBar.fitAllClicked.connect(self._onFitAll)
        self._viewSwitchBar.viewChanged.connect(self._onViewChanged)
        self._viewSwitchBar.paramPanelClicked.connect(self._onParamBtnClicked)
        # 图片按钮：选图后进入“两点框放”命令
        self._viewSwitchBar.imageSelected.connect(self._onImageSelected)
        self._paramHost.closed.connect(self._paramHost.hide)
        self._paramHost.resized.connect(self._positionParamHost)
        self._cm.activeCommandChanged.connect(self._onActiveCommandChanged)
        self._canvasModeBar.modeChanged.connect(self._onCanvasModeChanged)
        self._syncReliefButtons()   # 初始（2D 模式）禁用浮雕按钮

    def _onFitAll(self) -> None:
        if self._active3D:
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
                    self._enterReliefEdit()

    def _onParamBtnClicked(self) -> None:
        """画布右侧“参数”按钮：切换参数面板显隐。"""
        if self._paramHost.isHidden():
            self._paramHost.show()
            self._positionViewSwitchBar()
        else:
            self._paramHost.hide()

    def _onViewChanged(self, idx: int) -> None:
        # 方向栏只在 3D/浮雕下可点，切换视角模式；2D 下被锁定不会触发
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
        # 变换面板浮雕显示区随画布模式切换 2D 预览组 / 浮雕(3D)组
        self._transformPanel.setCanvasMode(mode)
        if mode == "3d":
            # 3D 预览：三维视口透视占位，后期换真实渲染
            self._exitReliefLock()
            self._active3D = True
            self._viewport3D.setHideReferenceGrid(True)
            self._viewStack.setCurrentWidget(self._viewport3D)
            self._viewSwitchBar.setLocked(False)
            self._viewSwitchBar.setCurrent(0)
            self._viewport3D.setViewMode(0)
            self._topDock.appendHistory("3D 模式：渲染视口尚未实现，当前为三维线框预览")
        elif mode == "relief":
            # 浮雕：单个模型的编辑态（锁定放置变换，视图可自由旋转翻面）
            self._active3D = True
            # 浮雕模式不显示参考地面/网格线，仅留浮雕曲面
            self._viewport3D.setHideReferenceGrid(True)
            self._viewStack.setCurrentWidget(self._viewport3D)
            self._viewSwitchBar.setLocked(False)
            self._viewSwitchBar.setCurrent(0)
            self._viewport3D.setViewMode(0)
            self._viewport3D.resetView()
            self._enterReliefEdit()
        else:
            # 2D：回到平面画布，视图锁定顶视图
            self._exitReliefLock()
            self._active3D = False
            self._viewport3D.setHideReferenceGrid(True)
            self._viewStack.setCurrentWidget(self._view)
            self._viewSwitchBar.setLocked(True)
            self._viewSwitchBar.setCurrent(1)
            self._view.setViewMode(1)
            self._view.zoomToFit()
        # 浮雕命令按钮仅在浮雕模式下可用
        self._syncReliefButtons()

    def _syncReliefButtons(self) -> None:
        """浮雕命令按钮仅在“浮雕”画布模式下可用，其它模式置灰禁用；
        浮雕撤销/重做按钮叠加各自历史状态（可为空历史而置灰）。"""
        qb = self._topDock.quickBar()
        qb.setReliefButtonsEnabled(self._canvasModeBar.current() == "relief")
        qb.setReliefUndoEnabled(self._doc.canReliefUndo())
        qb.setReliefRedoEnabled(self._doc.canReliefRedo())

    def _ensure2DCanvas(self) -> None:
        """命令激活时确保可见视图是 2D 平面画布（曲线点选/起浮雕都在 2D 上进行），
        但不改变当前画布模式（浮雕模式下按钮仍可用、语义保持一致）。"""
        if self._viewStack.currentWidget() is not self._view:
            self._viewStack.setCurrentWidget(self._view)
            self._view.setViewMode(1)

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
        self._reliefLockedModel = model
        self._transformPanel.setReliefModel(model)
        self._viewport3D.setReliefModel(model)
        if model is not None:
            model.setLocked(True)
            # 脱离“选中”：编辑态不占选择集，曲线选择完全自由
            if model.isSelected():
                model.setSelected(False)
                self._doc.selectionChanged.emit()

    def _exitReliefLock(self) -> None:
        """离开浮雕编辑态：解锁模型，恢复可选中/变换；默认重新选中便于继续变换。"""
        model = self._reliefLockedModel
        self._reliefLockedModel = None
        self._viewport3D.setReliefModel(None)
        self._transformPanel.setReliefModel(None)
        if model is not None:
            model.setLocked(False)
            if not self._doc.selectedObjects():
                self._doc.selectObject(model.id())

    def _onObjectRemoved(self, oid: str) -> None:
        """锁定的浮雕模型被移除（含撤销/重做的快照替换）：记录 id 等待可能的重建。"""
        m = self._reliefLockedModel
        if m is not None and m.id() == oid:
            self._reliefRestoreId = oid
            self._reliefLockedModel = None
            self._viewport3D.setReliefModel(None)
            self._transformPanel.setReliefModel(None)

    def _onObjectAdded(self, oid: str) -> None:
        """撤销/重做按 id 重建对象：浮雕模式下同 id 回来则恢复锁定编辑态。"""
        if self._reliefRestoreId != oid:
            return
        self._reliefRestoreId = None
        if self._canvasModeBar.current() != "relief":
            return
        obj = self._doc.object(oid)
        if obj is None or obj.type() != ObjectType.SURFACE:
            return
        self._reliefLockedModel = obj
        obj.setLocked(True)
        if obj.isSelected():
            obj.setSelected(False)
            self._doc.selectionChanged.emit()
        self._transformPanel.setReliefModel(obj)
        self._viewport3D.setReliefModel(obj)


    def _onCommandRequested(self, name: str) -> None:
        if self._cm.hasActive():
            self._cm.cancelActive()
        # 图片转线稿是 2D 曲线工具：预览与结果都画在 2D 画布上
        if name == "ImageTrace":
            self._ensure2DCanvas()
        # 浮雕类命令（新建/区域浮雕）均在当前画布交互：新建后自动进编辑态并切到
        # 3D 视口，区域浮雕直接在 3D 视口点选曲线，右键用于旋转查看，不切回 2D
        if self._cm.executeCommand(name):
            self._topDock.appendHistory(f"启动命令: {name}")
        else:
            self._topDock.appendHistory(f"命令未实现: {name}")

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
        for obj in self._doc.objects():
            if abs(obj.lineWidth() - w) > 1e-9:
                obj.setLineWidth(w)
                self._doc.notifyObjectChanged(obj.id())

    def _onSelectionChanged(self) -> None:
        sel = self._doc.selectedObjects()
        obj = sel[0] if sel else None
        self._propertyPanel.showObject(obj)
        self._transformPanel.showObject(obj)
        if obj is not None:
            self._statusBar.setLineWidth(obj.lineWidth())
        self._updateMeasure()
        # 画布左下角：无命令活动时显示选中摘要（开放/闭合曲线、模型、图片、标注…）
        summary = self._formatSelectionSummary(sel)
        if self._view is not None:
            self._view.setSelectionSummary(summary)

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
        image = 0
        dim = 0
        group = 0
        point = 0
        other = 0
        for o in selected:
            t = o.type()
            if t == ObjectType.SURFACE:
                model += 1
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
        """变换面板“绑定”开关被点击：解除选中 XY 标注与源对象的绑定。

        绑定解除即“脱离关系”：目标记录被清空且不再提供重新绑定入口，
        之后物体尺寸变化不再影响该标注。
        """
        dims = [o for o in self._doc.selectedObjects()
                if o.type() == ObjectType.DIMENSION and o.kind() == "xy"
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
        """变换面板“尺寸标注”区按钮：XY标注 / 对齐标注 / Z标注（占位）。"""
        if kind == "xy":
            self._createXYDimension()
        elif kind == "align":
            self._startAlignDim()
        elif kind == "z":
            self._topDock.appendHistory(
                "Z 标注占位：后续版本用于为模型标注高度")
            self._notifyTip("Z 标注尚未开放：后续用于为模型标注高度")

    def _startAlignDim(self) -> None:
        """对齐标注：切到 2D 平面画布并进入“点两点”命令。"""
        if self._cm.hasActive():
            self._cm.cancelActive()
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
        """变换面板移动/旋转/缩放快捷命令：切到 2D 画布并启动对应命令。"""
        if self._cm.hasActive():
            self._cm.cancelActive()
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

    def _onModelZCommit(self, target: float) -> None:
        """变换面板“模型Z”确认：把浮雕模型高度场整体缩放到目标高度 Z。

        缩放系数 = 目标值 / 当前最高高度；只动网格数据（最低点保持 Z=0，
        XY 布局与色号蒙版不变），可经浮雕撤销逐步回退。3D 模式下则作用于
        当前选中的 SURFACE 模型。
        """
        model = self._reliefLockedModel
        if model is None:
            sel = self._doc.selectedObjects()
            model = next((o for o in sel
                          if o.type() == ObjectType.SURFACE), None)
        if model is None or target <= 0.0:
            return
        hf = model.heightField()
        data = hf.data
        if data.size == 0:
            return
        cur = float(data.max())
        if cur <= 0.0:
            self._topDock.appendHistory("模型Z：当前模型无高度（平面），无法缩放")
            return
        factor = target / cur
        if abs(factor - 1.0) < 1e-9:
            return
        self._doc.beginEdit()
        try:
            hf.setData(data * factor)
            model.bumpVersion()
            self._doc.notifyObjectChanged(model.id())
            self._topDock.appendHistory(f"模型Z：高度场已缩放到 {target:g}")
        finally:
            self._doc.endEdit()
        self._scene.update()
        self._viewport3D.update()

    def _onShowSettings(self) -> None:
        dlg = SettingsDialog(self)
        dlg.appearanceChanged.connect(self._refreshStyle)
        dlg.gridVisibleChanged.connect(self._scene.setGridVisible)
        dlg.axisVisibleChanged.connect(self._scene.setAxisVisible)
        dlg.fileSlotsChanged.connect(self._leftPanel.refreshSavePage)
        dlg.renderSettingsChanged.connect(self._onRenderSettingsChanged)
        # 标注样式（字体/单位）变化：重绘场景让已有标注立即采用新样式
        dlg.dimensionSettingsChanged.connect(self._scene.update)
        dlg.exec()
        dlg.deleteLater()   # 用完即弃：避免控件树与信号连接随打开次数累积
        # 文件页设置（自动保存开关/间隔）可能已改变：按新设置重启自动保存定时器
        self._restartAutosaveTimer()

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
        self._bindDocSignals()
        # 3D/浮雕模式锚定旧文档的模型，切文档统一回到 2D 平面画布
        if self._canvasModeBar.current() != "2d":
            self._canvasModeBar.setCurrent("2d", notify=True)
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
            event.accept()
            return
        names = "\n".join(f"  • {d.title()}" for d in dirty)
        ret = QMessageBox.question(
            self, "未保存的修改",
            f"以下 {len(dirty)} 个文件有未保存的修改：\n{names}\n\n"
            f"是否在退出前保存？",
            QMessageBox.SaveAll | QMessageBox.Discard | QMessageBox.Cancel)
        if ret == QMessageBox.Cancel:
            event.ignore()
            return
        if ret == QMessageBox.SaveAll:
            for d in dirty:
                if not self._saveDoc(d):
                    event.ignore()   # 另存为被取消或保存失败：中止退出
                    return
        event.accept()

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
            ret = QMessageBox.question(
                self, "关闭文件", f"“{doc.title()}”有未保存的修改，是否保存？",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
            if ret == QMessageBox.Cancel:
                return
            if ret == QMessageBox.Save and not self._saveDoc(doc):
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
        self.setWindowTitle(f"{title} - Cangai-Relief 浮雕设计")
        self._positionViewSwitchBar()   # 标签宽度变化后重新贴右下角

    def _onImageSelected(self, path: str) -> None:
        """右侧“图片”按钮：选图 → 两点框放命令（先验证图片可解码）。"""
        from core.vector.image_object import ImageObject
        try:
            ImageObject.fromFile(path)
        except Exception as e:
            QMessageBox.warning(self, "图片导入失败", f"无法读取图片:\n{e}")
            return
        if self._cm.hasActive():
            self._cm.cancelActive()
        self._ensure2DCanvas()
        if self._cm.executeCommand("Image", path):
            self._topDock.appendHistory(f"放置图片: {path}")
        else:
            self._topDock.appendHistory("命令未实现: Image")

    def _onImportFile(self) -> None:
        """左下角导入按钮：选择 Cangai 文件在新标签中载入（已打开则直接切换）。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "导入文件", "", "Cangai 文件 (*.cai *.cag);;所有文件 (*)")
        if not path:
            return
        exist = next((d for d in self._openDocs if d.filePath() == path), None)
        if exist is not None:
            self._switchToDocument(self._openDocs.index(exist))
            return
        try:
            self._newDocument(path)
            self._notifyTip(f"已导入: {path}")
        except Exception as e:
            QMessageBox.warning(self, "导入失败", f"无法导入文件:\n{e}")

    def _onOpenFile(self) -> None:
        """打开文件：支持多选，每个文件一个标签；已打开的文件直接切换过去。"""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "打开文件", "", "Cangai 文件 (*.cai *.cag);;所有文件 (*)")
        if not paths:
            return
        last = None
        for path in paths:
            try:
                exist = next((d for d in self._openDocs
                              if d.filePath() == path), None)
                if exist is not None:
                    last = self._openDocs.index(exist)
                    continue
                self._newDocument(path)
                last = self._docIndex
            except Exception as e:
                QMessageBox.warning(self, "打开失败", f"无法打开文件 {path}:\n{e}")
        if last is not None:
            self._switchToDocument(last)

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
        if not filename.lower().endswith(f".{ext}"):
            filename += f".{ext}"
        path, _ = QFileDialog.getSaveFileName(
            self, f"导出 {fmt}", filename, f"{fmt} 文件 (*.{ext});;所有文件 (*)")
        if not path:
            return
        try:
            if fmt == "DXF":
                from core.export.dxf_exporter import exportDXF
                exportDXF(self._doc, path)
            elif fmt == "STL":
                from core.export.stl_exporter import exportSTL
                exportSTL(self._doc, path)
            self._notifyTip(f"已导出: {path}")
        except ImportError:
            QMessageBox.information(self, "暂未实现", f"{fmt} 导出功能尚未实现")
        except Exception as e:
            QMessageBox.warning(self, "导出失败", f"无法导出文件:\n{e}")

    def _refreshStyle(self) -> None:
        QCoreApplication.instance().setStyleSheet(theme.globalQss())
        self._dockManager.setStyleSheet(theme.dockManagerQss())
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

    def eventFilter(self, obj: object, event: QEvent) -> bool:
        if (obj is getattr(self, "_canvasContainer", None)
                and event.type() == QEvent.Resize):
            self._positionViewSwitchBar()
        return super().eventFilter(obj, event)

    def _positionViewSwitchBar(self) -> None:
        c = self._canvasContainer
        bar = self._viewSwitchBar
        bar.move(c.width() - bar.width(), 20)
        bar.raise_()
        modeBar = self._canvasModeBar
        modeBar.move((c.width() - modeBar.width()) // 2, 2)
        modeBar.raise_()
        # 文件标签栏：画布右下角（左下角已被步骤提示条占用）
        tb = self._fileTabBar
        tb.move(c.width() - tb.width() - 10, c.height() - tb.height() - 4)
        tb.raise_()
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
        # 删除键：删除选中对象（可撤销）
        if event.key() == Qt.Key_Delete and not self._cm.hasActive():
            self._doc.beginEdit()
            for o in list(self._doc.selectedObjects()):
                self._doc.removeObject(o.id())
            self._doc.endEdit()
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
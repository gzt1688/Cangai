"""2D 画布视图：鼠标交互、捕捉、命令输入转发。"""
from __future__ import annotations
import math
from typing import Optional
from PySide6.QtCore import Qt, QPoint, QPointF, QRectF, Signal
from PySide6.QtGui import (QMouseEvent, QWheelEvent, QKeyEvent, QPainter,
                           QPalette, QColor, QBrush, QPen, QFont, QPolygonF)
from PySide6.QtWidgets import QGraphicsView, QGraphicsItem

from core.grid_manager import GridManager
import core.app_settings as app_settings
from ui.styles import theme
from ui.widgets.step_hint import StepHintOverlay
from core.snap.snap_manager import SnapManager
from core.snap.snap_types import SnapResult, snapLabel, SnapType
from core.vector.geometry_types import Point3D, ObjectType
from ui.view_2d.items.dimension_item import DimensionGraphicsItem
from ui.view_drop import droppedFilePaths, acceptIfFiles


# "沿线行走"型吸附：候选点落在对象几何上（而非固定特征点），自身正在
# 绘制的对象上离最后输入点很近的这类候选会把光标往回拽，由视口按距离过滤
_WALKING_SNAPS = frozenset({SnapType.NEAREST, SnapType.MIDPOINT,
                            SnapType.PERPENDICULAR, SnapType.TANGENT,
                            SnapType.INTERSECTION})


def _segIntersectsRect(p, q, r: QRectF) -> bool:
    """Liang-Barsky 线段裁剪：线段与矩形相交（含在内部）返回 True。"""
    dx, dy = q.x - p.x, q.y - p.y
    t0, t1 = 0.0, 1.0
    for pp, qq in ((-dx, p.x - r.left()), (dx, r.right() - p.x),
                   (-dy, p.y - r.top()), (dy, r.bottom() - p.y)):
        if abs(pp) < 1e-12:
            if qq < 0:
                return False
            continue
        t = qq / pp
        if pp < 0:
            if t > t1:
                return False
            if t > t0:
                t0 = t
        else:
            if t < t0:
                return False
            if t < t1:
                t1 = t
        if t0 > t1:
            return False
    return True


def paintSnapMarker(painter, snap, sp, cursorPos) -> None:
    """捕捉标记（按类型不同形状）+ 气泡提示类型名（2D 画布与 3D 视口共用）。

    标记画在 effectiveMarker（markerPos 或 position），气泡跟随标记；
    sp 为标记的屏幕坐标，cursorPos 为当前光标屏幕坐标。"""
    painter.save()
    painter.resetTransform()
    # HiDPI：QPainter 初始 transform 含设备像素比缩放（QOpenGLWidget 与
    # 高 DPI 光栅视图同理），resetTransform 会把它一并清掉——此后所有
    # 绘制落在设备像素上，缩放屏（125%/150%）时标记缩小、气泡文字又小
    # 又糊。按设备像素比补回缩放，恢复 1:1 逻辑像素绘制。
    try:
        dpr = painter.device().devicePixelRatioF() if \
            painter.device() is not None else 1.0
    except Exception:
        dpr = 1.0
    if dpr and dpr > 0:
        painter.scale(dpr, dpr)
    pen = QPen(QColor("#15b6c9"), 1.4)
    pen.setCosmetic(True)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    r = 6
    t = snap.type
    if t == SnapType.ENDPOINT:
        painter.drawRect(sp.x() - r, sp.y() - r, 2 * r, 2 * r)
    elif t == SnapType.MIDPOINT:
        painter.drawPolygon(QPolygonF([QPointF(sp.x(), sp.y() - r),
                                       QPointF(sp.x() + r, sp.y()),
                                       QPointF(sp.x(), sp.y() + r)]))
    elif t == SnapType.CENTER:
        # 智能圆心识别：画虚线半径（圆心 → 光标）提示来源
        dashPen = QPen(QColor("#15b6c9"), 1.0, Qt.PenStyle.DashLine)
        dashPen.setCosmetic(True)
        painter.setPen(dashPen)
        painter.drawLine(sp, cursorPos)
        painter.setPen(pen)
        painter.drawEllipse(sp, r - 1, r - 1)
        painter.drawLine(sp.x() - r, sp.y(), sp.x() + r, sp.y())
        painter.drawLine(sp.x(), sp.y() - r, sp.x(), sp.y() + r)
    elif t == SnapType.POINT:
        painter.drawLine(sp.x() - r, sp.y() - r, sp.x() + r, sp.y() + r)
        painter.drawLine(sp.x() - r, sp.y() + r, sp.x() + r, sp.y() - r)
    elif t == SnapType.QUARTER:
        painter.drawPolygon(QPolygonF([QPointF(sp.x(), sp.y() - r),
                                       QPointF(sp.x() + r, sp.y()),
                                       QPointF(sp.x(), sp.y() + r),
                                       QPointF(sp.x() - r, sp.y())]))
    elif t == SnapType.INTERSECTION:
        painter.drawLine(sp.x() - r, sp.y() - r, sp.x() + r, sp.y() + r)
        painter.drawLine(sp.x() - r, sp.y() + r, sp.x() + r, sp.y() - r)
    elif t == SnapType.PERPENDICULAR:
        painter.drawLine(sp.x(), sp.y(), sp.x() + r, sp.y())
        painter.drawLine(sp.x() + r, sp.y(), sp.x() + r, sp.y() - r)
        painter.drawLine(sp.x() + r, sp.y() - r, sp.x(), sp.y() - r)
    elif t == SnapType.TANGENT:
        painter.drawEllipse(sp, r - 2, r - 2)
        painter.drawLine(sp.x() + r, sp.y() - r, sp.x() - r, sp.y() + r)
    elif t == SnapType.NEAREST:
        painter.drawLine(sp.x() - r, sp.y(), sp.x() + r, sp.y())
    elif t == SnapType.VERTEX:
        painter.drawRect(sp.x() - r + 1, sp.y() - r + 1, 2 * r - 2, 2 * r - 2)
    # 气泡提示
    label = snapLabel(t)
    if label:
        painter.setFont(QFont("Microsoft YaHei", 9, QFont.Bold))
        fm = painter.fontMetrics()
        tw = int(fm.horizontalAdvance(label)) + 10
        th = int(fm.height()) + 4
        bx = int(sp.x()) + 12
        by = int(sp.y()) - th - 4
        painter.setBrush(QColor("#15b6c9"))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(bx, by, tw, th, 3, 3)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setPen(QPen(QColor("#ffffff")))
        painter.drawText(bx + 5, by + fm.ascent() + 2, label)
    painter.restore()


class GraphicsView(QGraphicsView):
    worldClicked = Signal(object)
    mouseMoved = Signal(object)
    snapChanged = Signal(object)
    scaleChanged = Signal(float)
    # 画布拖入文件：路径列表 + 落点世界坐标（主窗口接入导入）
    fileDropped = Signal(list, object)

    def __init__(self, scene) -> None:
        super().__init__(scene)
        self.setAcceptDrops(True)   # 支持把 png/jpg/dxf/stl 直接拖进画布
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setRenderHint(QPainter.SmoothPixmapTransform, True)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        # 视图获取键盘焦点（StrongFocus=点击/Tab 都能聚焦），确保鼠标点画布后
        # 数字键、C 闭合、缩放键盘输入等能通过命令管理器分发；若命令不消费
        # （非活动命令 / 不关心的键）则通过 super() 向上冒泡到 MainWindow 的
        # keyPressEvent（处理 Esc/Enter/Ctrl+Z/F8/Delete 等全局快捷键）。
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # 隐藏 QGraphics 默认选中虚线（颜色与背景一致），选中高亮由图元自绘
        pal = self.palette()
        pal.setColor(QPalette.Highlight, QColor(theme.BG_VIEWPORT))
        self.setPalette(pal)
        self.setCacheMode(QGraphicsView.CacheBackground)
        # BoundingRect 更新：MinimalViewportUpdate 在 cosmetic 笔 + 抗锯齿下有
        # 残留残影（画线拖动时旧帧清理不干净导致闪动）的已知瑕疵，用包围盒更新兼顾稳定与性能
        self.setViewportUpdateMode(QGraphicsView.BoundingRectViewportUpdate)
        self.setOptimizationFlag(QGraphicsView.DontAdjustForAntialiasing, True)
        self._commandManager = None
        self._grid = GridManager.instance()
        self._snap = SnapManager.instance()
        self._doc = None
        # 左下角悬浮步骤/结果提示条（无背景）
        self._stepHint = StepHintOverlay(self)
        self._layoutStepHint()
        # 选中摘要：mainWindow 每次选择变化时填入（如 "选中 1 条开放曲线, 2 条闭合曲线"）；
        # 无活动命令且有选中摘要时，步骤行优先显示摘要，否则显示默认「提示信息显示区」。
        self._selectionSummary: str = ""
        # 初始无命令：步骤行显示「提示信息显示区」（与 CommandManager 默认 prompt 一致）
        self._stepHint.setStep("提示信息显示区")
        self._gizmo = None
        self._gizmoAdapter = None
        self._curveEdit = None
        self._curveEditAdapter = None
        self._panning = False
        self._rotating = False
        self._panStart = QPoint()
        self._rightPressPos: Optional[QPoint] = None
        # 右键按下位置：无活动命令时按下即进入平移/旋转，若释放前未拖动
        # （≤4px，同框选起框阈值）则视为右键单击 → 重复上一次命令
        self._boxStart = None       # 左键潜在框选起点（None=无）；位移超阈值才成框
        self._boxActive = False
        self._boxRect = QRectF()
        self._boxLTR = True         # 左→右=包含选择，右→左=相交选择（同原版）
        self._boxMods = Qt.KeyboardModifier.NoModifier
        self._lastSnap: Optional[SnapResult] = None
        self._cursorPos = QPoint()
        self._viewMode = 1
        # XY 尺寸标注“子标注单独拖动”（X 宽沿 Y 上下 / Y 高沿 X 左右）
        self._dimDragObj = None
        self._dimDragAxis = ""          # 'x'=拖动 X 宽读数(改 yOff)，'y'=拖动 Y 高读数(改 xOff)
        self._dimStartWorld = Point3D()
        self._dimStartOx = 0.0
        self._dimStartOy = 0.0
        self._dimCursorSet = False      # 标注悬停光标是否已设置（便于还原）
        self.scale(1, -1)

    def _growSceneRect(self) -> None:
        """无限画布：场景矩形保持不小于可见场景区域的 2 倍。
        否则缩小后视口盖满固定场景矩形，滚动条范围归零，
        平移被钳制在场景矩形内无法移动视图。"""
        sr = self.scene().sceneRect()
        vis = self.mapToScene(self.viewport().rect()).boundingRect()
        nr = sr.united(vis.adjusted(-vis.width() * 0.5,
                                    -vis.height() * 0.5,
                                    vis.width() * 0.5, vis.height() * 0.5))
        if nr == sr:
            return
        keep = self.mapToScene(self.viewport().rect().center())
        self.scene().setSceneRect(nr)
        self.updateScene([nr])
        self.centerOn(keep)   # 扩区不得改变当前视野位置

    def setCommandManager(self, cm) -> None:
        self._commandManager = cm
        if cm is not None:
            cm.activeCommandChanged.connect(self._onActiveCommandChanged)
            cm.promptChanged.connect(self._onPromptChanged)
            cm.resultMessage.connect(self._onResultMessage)

    def _onPromptChanged(self, text) -> None:
        # 空闲态（CommandManager 给的是默认「提示信息显示区」）且当前有选中摘要时：
        # 优先显示选中摘要；否则用命令给出的 text（活动命令步骤提示）。
        display = text
        if text == "提示信息显示区" and self._selectionSummary:
            display = self._selectionSummary
        self._stepHint.setStep(display)
        self._layoutStepHint()

    def _onResultMessage(self, text) -> None:
        self._stepHint.setResult(text)
        self._layoutStepHint()

    def setSelectionSummary(self, summary: str) -> None:
        """mainWindow 在选中变化时调用：传入选中摘要（空串=无选中或不提示）。
        若当前处于空闲态（步骤行正显示默认「提示信息显示区」），则立即把步骤行
        更新为摘要；否则等命令结束后由 promptChanged 自然切换回摘要。"""
        self._selectionSummary = summary or ""
        if self._commandManager is None or \
                self._commandManager.activeInstance() is None:
            # 空闲态：立即刷新
            if self._selectionSummary:
                self._stepHint.setStep(self._selectionSummary)
            else:
                self._stepHint.setStep("提示信息显示区")
            self._layoutStepHint()

    def _onActiveCommandChanged(self, name: str) -> None:
        """命令结束（name 为空）时清空捕捉标记，避免气泡残留；
        新命令开始时清除上一条操作结果。

        注意：不在这里改写步骤行（StepHint.setStep）—— 命令活动状态变化时，
        CommandManager 会紧接着发 promptChanged：
          - 新命令启动：prompt = 命令的首步提示 → _onPromptChanged 写入步骤行
          - 命令结束/取消：prompt = "提示信息显示区" → _onPromptChanged 把步骤行恢复"提示信息显示区"
        如果这里强行 setStep("")，会把刚写的 "提示信息显示区" 覆盖为空。"""
        if not name:
            self._lastSnap = None
            self.viewport().update()
        else:
            # 仅当命令真正进入活动态时才清上一条结果：一次性命令
            # （start 内即完成并已发结果消息，如群组/解组）此刻已结束，
            # 不应把刚显示的结果提示清掉
            if self._commandManager is not None \
                    and self._commandManager.activeInstance() is not None:
                self._stepHint.clearResult()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layoutStepHint()

    def _layoutStepHint(self) -> None:
        """把悬浮提示条固定在视图左下角，紧贴底部。
        每条信息单行显示（不换行），宽度用 QTextDocument 按文本精确计算；
        文本过长时保持单行向右侧延伸（步骤/结果各占一行）。"""
        hint = self._stepHint
        hint.setWordWrap(False)
        from PySide6.QtGui import QTextDocument
        doc = QTextDocument()
        doc.setDefaultFont(hint.font())
        doc.setHtml(hint.text())
        doc.setTextWidth(-1)
        pad_w, pad_h = 16, 8  # styleSheet padding 8px 4px 的两倍
        w = max(int(doc.idealWidth()) + pad_w, 20)
        h = max(int(doc.size().height()) + pad_h, 20)
        hint.setFixedSize(w, h)
        hint.move(10, self.height() - hint.height() - 2)

    def clearSnapMarker(self) -> None:
        """主动清空捕捉标记（命令启动时调用，避免上一操作的吸附气泡残留）。"""
        self._lastSnap = None
        self.viewport().update()

    def setGizmo(self, gizmo, adapter) -> None:
        self._gizmo = gizmo
        self._gizmoAdapter = adapter

    def setCurveEdit(self, ctrl, adapter) -> None:
        self._curveEdit = ctrl
        self._curveEditAdapter = adapter

    def setDocument(self, doc) -> None:
        self._doc = doc

    def screenToWorld(self, pos) -> Point3D:
        sp = self.mapToScene(pos) if isinstance(pos, QPoint) else pos
        return Point3D(sp.x(), sp.y(), 0.0)

    def snapAt(self, worldPos: Point3D) -> SnapResult:
        if self._doc is None:
            return SnapResult.invalid()
        r = self._snap.snapRadius()
        worldR = r / max(self.transform().m11(), 1e-6)
        ref = None
        previewIds: set = set()
        partialIds: set = set()
        excludedTypes: set = set()
        extraObjs: list = []
        inst = None
        if self._commandManager is not None and self._commandManager.hasActive():
            inst = self._commandManager.activeInstance()
            ref = inst.lastInputPoint()
            previewIds = inst.previewObjectIds()
            partialIds = inst.partialSnapObjectIds()
            excludedTypes = inst.excludedSnapTypesForPartial()
            extraObjs = inst.extraSnapObjects()
        objs = [o for o in self._doc.visibleObjects() if o.id() not in previewIds]
        objs.extend(extraObjs)
        res = self._snap.snap(worldPos, objs, worldR, ref)
        # 部分排除：命中自身对象、且属于命令指定要排除的吸附类型 → 视为未命中
        if res.isValid and res.objectId in partialIds and \
                res.type in excludedTypes:
            return SnapResult.invalid()
        # 自身正在画的几何：刚点下的那段的中/最近点/垂足就在光标背后，
        # 光标稍动就被拽回去（手感发黏）。只过滤"距最后输入点 2 个捕捉
        # 半径以内"的沿线候选，稍远处的自身中点/最近点照常可吸附。
        if res.isValid and res.objectId in partialIds and ref is not None \
                and res.type in _WALKING_SNAPS:
            dx = res.position.x - ref.x
            dy = res.position.y - ref.y
            if dx * dx + dy * dy < (2.0 * worldR) ** 2:
                return SnapResult.invalid()
        return res

    def _resolvePoint(self, pos) -> Point3D:
        wp = self.screenToWorld(pos)
        # 命令可声明不显示吸附气泡（如修剪、区域浮雕等点击即生效型）
        if self._commandManager is not None and self._commandManager.hasActive():
            inst = self._commandManager.activeInstance()
            if inst is not None and not inst.showSnapHints():
                self._lastSnap = None
                return wp
        res = self.snapAt(wp)
        if res.isValid:
            self._lastSnap = res
            return res.position
        self._lastSnap = None
        if self._grid.config().snapEnabled:
            gx, gy = self._grid.snapToGrid((wp.x, wp.y))
            return Point3D(gx, gy, 0.0)
        return wp

    # ---- 尺寸标注：XY 子标注可单独拖动（X 宽读数沿 Y / Y 高读数沿 X） ----

    def _dimHandleAt(self, pos) -> tuple | None:
        """光标下的可拖动 XY 子标注 (obj, axis)。axis='x' 表示下方 X 宽标注
        （上下拖动改 yOff），'y' 表示左侧 Y 高标注（左右拖动改 xOff）。
        仅当该标注已被选中且未锁定；读数文字中心即拖动柄（命中半径 12px）。"""
        if self._doc is None:
            return None
        best = None
        bestDist = 12.0
        for it in self.items(pos):
            if not isinstance(it, DimensionGraphicsItem):
                continue
            if not it.isVisible():
                continue
            obj = it.object()
            if obj is None or obj.isLocked() or not obj.isSelected():
                continue
            if getattr(obj, "kind", lambda: "")() != "xy":
                continue
            lay = obj.layout()
            for t in lay["texts"]:
                sc = self.mapFromScene(QPointF(t["cx"], t["cy"]))
                d = math.hypot(sc.x() - pos.x(), sc.y() - pos.y())
                if d <= bestDist:
                    bestDist = d
                    a = abs(t["angle"] % 180.0)
                    axis = "y" if abs(a - 90.0) < 1.0 else "x"
                    best = (obj, axis)
        return best

    def _updateDimCursor(self, pos) -> None:
        """悬停读数文字时给出上下/左右可拖光标提示。"""
        if self._dimDragObj is not None:
            return
        hit = self._dimHandleAt(pos)
        if hit is not None:
            self._dimCursorSet = True
            self.setCursor(Qt.SizeVerCursor if hit[1] == "x"
                           else Qt.SizeHorCursor)
        elif self._dimCursorSet:
            self._dimCursorSet = False
            self.setCursor(Qt.ArrowCursor)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """键盘事件优先交给活动命令消费（缩放数字输入、画线 C 闭合等）；
        命令不消费的键（Esc/Enter/Ctrl+Z/F8/Delete 等全局快捷键），再让
        QGraphicsView 默认处理，若仍未接受则转发给顶层窗口（MainWindow）
        的 keyPressEvent 统一处理，防止焦点在 GraphicsView 时快捷键失效。

        注意：Tab 快捷键不在此处理，由主窗口的 QShortcut 全局监听，
        避免被 QGraphicsView 默认的焦点切换机制吞掉。"""
        cm = self._commandManager
        # Esc / Enter：保留给 MainWindow 走 cancelActive / onEnter 专门分支
        if cm is not None \
                and event.key() not in (Qt.Key_Return, Qt.Key_Enter,
                                       Qt.Key_Escape) \
                and cm.onKeyPress(event.key(), event.modifiers()):
            event.accept()
            return
        # QGraphicsView 默认（主要是方向键 / Tab 等）
        super().keyPressEvent(event)
        if event.isAccepted():
            return
        # 仍未处理：交给顶层窗口（MainWindow）处理（Ctrl+Z/C/V、F8、Del、Esc、Enter 等）
        win = self.window()
        if win is not None and win is not self:
            # 直接转发同一个事件（此路径上命令管理器与 QGraphicsView 都未 accept，
            # 即使再次经过 accept 判断也不会冲突）
            win.keyPressEvent(event)

    # ---------------- 文件拖入画布 ----------------

    def dragEnterEvent(self, event) -> None:
        acceptIfFiles(event)

    def dragMoveEvent(self, event) -> None:
        acceptIfFiles(event)

    def dropEvent(self, event) -> None:
        paths = droppedFilePaths(event)
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        wp = self.screenToWorld(
            self.viewport().mapFrom(self, event.position().toPoint()))
        self.fileDropped.emit(paths, wp)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MiddleButton:
            self._panning = True
            self._panStart = event.pos()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        if event.button() == Qt.RightButton and \
           (self._commandManager is None or not self._commandManager.hasActive()):
            self._rightPressPos = event.pos()
            if self._viewMode == 0:
                self._rotating = True
                self._panStart = event.pos()
                self._savedAnchor = self.transformationAnchor()
                self.setTransformationAnchor(QGraphicsView.AnchorViewCenter)
                self.setCursor(Qt.ClosedHandCursor)
            else:
                self._panning = True
                self._panStart = event.pos()
                self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        if event.button() == Qt.LeftButton and self._commandManager is not None:
            if self._commandManager.hasActive():
                inst = self._commandManager.activeInstance()
                if inst is not None and inst.wantsSelectionOnPress():
                    # 命令让出左键给画布做点选/框选（如阵列选取对象）：
                    # 走无活动命令时的框选流程，选择变化经 selectionChanged
                    # 回通知命令刷新预览
                    self._boxStart = event.pos()
                    self._boxMods = event.modifiers()
                    self._boxActive = False
                    self.setFocus()
                    event.accept()
                    return
                wp = self._resolvePoint(event.pos())
                self._commandManager.onMousePress(wp, event.modifiers())
                event.accept()
                return
        if event.button() == Qt.RightButton and self._commandManager is not None:
            if self._commandManager.hasActive():
                wp = self._resolvePoint(event.pos())
                self._commandManager.onRightClick(wp)
                event.accept()
                return
        if event.button() == Qt.LeftButton:
            # 无活动命令：左键按下由框选状态机接管（点选延迟到释放判定，
            # 避免默认按下选中在 Shift 框选加选前抢先改选择集）
            # 控制点优先于操作轴：命中控制点即选中/拖动（未开启编辑开关时自动开启）
            if self._curveEdit is not None and \
                    self._curveEdit.tryPressControlPoint(
                        self._curveEditAdapter, event.pos(),
                        event.modifiers()):
                self._boxStart = None   # 控制点拖动优先，禁止残留框选态
                event.accept()
                return
            if self._gizmo is not None:
                # 优先命中操作轴（未命中则继续框选/点选流程）
                if self._gizmo.handleMousePress(self._gizmoAdapter, event.pos(),
                                                event.modifiers()):
                    self._boxStart = None   # 操作轴拖动优先，禁止残留框选态
                    event.accept()
                    return
            # XY 标注子标注拖动：已选中标注的读数文字按下即开始（可在 Y/X 向单独拉动）
            hit = self._dimHandleAt(event.pos())
            if hit is not None:
                obj, axis = hit
                self._dimDragObj = obj
                self._dimDragAxis = axis
                self._dimStartWorld = self.screenToWorld(event.pos())
                self._dimStartOx, self._dimStartOy = obj.xyOffsets()
                self._doc.beginEdit()
                self._boxStart = None
                self.setCursor(Qt.SizeVerCursor if axis == "x"
                               else Qt.SizeHorCursor)
                event.accept()
                return
            self._boxStart = event.pos()
            self._boxMods = event.modifiers()
            self._boxActive = False
            self.setFocus()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._panning:
            delta = event.pos() - self._panStart
            self._panStart = event.pos()
            self._growSceneRect()   # 保证滚动条始终有可移动范围
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
            return
        if self._rotating:
            delta = event.pos() - self._panStart
            self._panStart = event.pos()
            self.rotate(-delta.x() * 0.5)
            event.accept()
            return
        if self._curveEdit is not None and self._curveEdit.isDragging():
            self._curveEdit.handleMouseMove(self._curveEditAdapter,
                                            event.pos(), event.modifiers())
            event.accept()
            return
        if self._dimDragObj is not None:
            # XY 子标注拖动：X 宽标注只取 Y 分量、Y 高标注只取 X 分量
            wpt = self.screenToWorld(event.pos())
            if self._dimDragAxis == "x":
                oy = self._dimStartOy - (wpt.y - self._dimStartWorld.y)
                self._dimDragObj.setXYOffset(self._dimStartOx, oy)
            else:
                ox = self._dimStartOx - (wpt.x - self._dimStartWorld.x)
                self._dimDragObj.setXYOffset(ox, self._dimStartOy)
            self._doc.notifyObjectChanged(self._dimDragObj.id())
            self.viewport().update()
            event.accept()
            return
        if self._boxStart is not None:
            d = event.pos() - self._boxStart
            if self._boxActive or abs(d.x()) > 4 or abs(d.y()) > 4:
                self._boxActive = True
                self._boxLTR = event.pos().x() >= self._boxStart.x()
                self._boxRect = QRectF(QPointF(self._boxStart),
                                       QPointF(event.pos())).normalized()
                self.viewport().update()
                event.accept()
                return
        if self._commandManager is not None and self._commandManager.hasActive():
            # 先走基类更新视图内部鼠标跟踪（AnchorUnderMouse/悬停依赖它），再转命令
            super().mouseMoveEvent(event)
            wp = self._resolvePoint(event.pos())
            self._cursorPos = event.pos()
            self._commandManager.onMouseMove(wp, event.modifiers())
            self.mouseMoved.emit(wp)
            self.viewport().update()
            event.accept()
            return
        super().mouseMoveEvent(event)
        wp = self.screenToWorld(event.pos())
        self.mouseMoved.emit(wp)
        gizmoBusy = False
        if self._gizmo is not None:
            # 拖动中消费事件；未拖动时仅做悬停高亮（返回 False）
            gizmoBusy = self._gizmo.handleMouseMove(self._gizmoAdapter,
                                                    event.pos(),
                                                    event.modifiers())
        if not gizmoBusy:
            self._updateDimCursor(event.pos())

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton and self._dimDragObj is not None:
            # XY 子标注拖动结束（无位移单击则不产生撤销记录）
            self._dimDragObj = None
            self._dimDragAxis = ""
            self._dimCursorSet = False
            self.setCursor(Qt.ArrowCursor)
            self._doc.endEdit()
            self.viewport().update()
            event.accept()
            return
        if event.button() == Qt.LeftButton and self._boxStart is not None:
            start, mods, active = self._boxStart, self._boxMods, self._boxActive
            self._boxStart = None
            self._boxActive = False
            self._boxRect = QRectF()
            self.viewport().update()
            if active:
                rect = QRectF(QPointF(start), QPointF(event.pos())).normalized()
                # 编辑目标激活时框选控制点，否则框选对象
                # （开关开着但未选中曲线时仍须能框选对象，故不能用 isEnabled）
                if self._curveEdit is not None and self._curveEdit.isActive():
                    self._curveEdit.boxSelect(self._curveEditAdapter, rect, mods)
                else:
                    self._applyBoxSelect(rect, mods)
            else:
                self._applyClickSelect(event.pos(), mods)
            event.accept()
            return
        if event.button() == Qt.LeftButton and self._curveEdit is not None:
            if self._curveEdit.handleMouseRelease(self._curveEditAdapter,
                                                  event.pos()):
                event.accept()
                return
        if event.button() == Qt.MiddleButton and self._panning:
            self._panning = False
            self.setCursor(Qt.ArrowCursor)
            event.accept()
            return
        if event.button() == Qt.RightButton and \
                (self._rotating or self._panning):
            pressPos = self._rightPressPos
            self._rightPressPos = None
            if self._rotating:
                self._rotating = False
                self.setTransformationAnchor(self._savedAnchor)
            self._panning = False
            self.setCursor(Qt.ArrowCursor)
            # 未拖动的右键单击：重复上一次命令；按住拖动仍是平移（2D）/旋转（3D 视角）
            if pressPos is not None and self._commandManager is not None:
                d = event.pos() - pressPos
                if abs(d.x()) <= 4 and abs(d.y()) <= 4:
                    self._commandManager.repeatLastCommand()
            event.accept()
            return
        if event.button() == Qt.LeftButton and self._gizmo is not None:
            if self._gizmo.handleMouseRelease(self._gizmoAdapter, event.pos()):
                event.accept()
                return
        if event.button() == Qt.LeftButton and self._commandManager is not None:
            if self._commandManager.hasActive():
                self._commandManager.onMouseRelease(event.modifiers())
                event.accept()
                return
        super().mouseReleaseEvent(event)

    def drawForeground(self, painter, rect) -> None:
        if self._boxActive and not self._boxRect.isNull():
            self._paintSelectBox(painter)
        if self._curveEdit is not None and self._curveEditAdapter is not None:
            self._curveEdit.paint(painter, self._curveEditAdapter)
        if self._gizmo is not None and self._gizmoAdapter is not None:
            self._gizmo.paint(painter, self._gizmoAdapter)
        self._paintSnapMarker(painter)

    def _paintSnapMarker(self, painter) -> None:
        """捕捉标记：换算到屏幕坐标后调用共用的 paintSnapMarker。"""
        snap = self._lastSnap
        if snap is None or not snap.isValid:
            return
        mp = snap.effectiveMarker
        sp = self.mapFromScene(QPointF(mp.x, mp.y))
        paintSnapMarker(painter, snap, sp, self._cursorPos)

    def _paintSelectBox(self, painter) -> None:
        """框选矩形：左→右包含选择蓝色实线，右→左相交选择绿色虚线（同原版）。"""
        sr = self.mapToScene(self._boxRect.toRect()).boundingRect()
        if self._boxLTR:
            border, fill = QColor(74, 144, 217, 200), QColor(74, 144, 217, 40)
            pen = QPen(border, 1, Qt.PenStyle.SolidLine)
        else:
            border, fill = QColor(95, 184, 95, 200), QColor(95, 184, 95, 40)
            pen = QPen(border, 1, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(QBrush(fill))
        painter.drawRect(sr)

    def _applyClickSelect(self, pos, mods) -> None:
        """左键点选：空白清选；Ctrl 切换、Shift 加选（同原版选择约定）。

        锁定对象（含所在图层被锁定）不可被点选：与操作轴/变换/框选的
        排除规则一致，避免“选中了却没有操作轴”的困惑。"""
        item = None
        # 从顶层往下找第一个可选中图元（坐标轴等参考线不可选中，需跳过；
        # 隐藏图层图元不可选中）
        for it in self.items(pos):
            if not it.isVisible():
                continue
            if not (it.flags() & QGraphicsItem.GraphicsItemFlag.ItemIsSelectable):
                continue
            # 图元映射到文档对象：锁定对象跳过（等同未点中，继续找下层图元）
            oid = it.objectId() if hasattr(it, "objectId") else None
            if oid is not None:
                obj = self._doc.object(oid) if self._doc is not None else None
                if obj is not None and obj.isLocked():
                    continue
            item = it
            break
        scene = self.scene()
        if item is None:
            scene.clearSelection()
            return
        if mods & Qt.KeyboardModifier.ControlModifier:
            item.setSelected(not item.isSelected())
        elif mods & Qt.KeyboardModifier.ShiftModifier:
            item.setSelected(True)
        else:
            sel = scene.selectedItems()
            # 单击已单独选中的对象保持原状（同原版），否则替换选择
            if not (len(sel) == 1 and sel[0] is item):
                scene.clearSelection()
                item.setSelected(True)
        # 群组联动：非 Ctrl 点选命中群组成员时，同组其它未锁定成员一并选中
        if not (mods & Qt.KeyboardModifier.ControlModifier):
            self._selectGroupSiblings(item)

    def _selectGroupSiblings(self, item) -> None:
        """点选命中群组成员时，把同组其它未锁定成员一并选中（整组联动）。
        仅加选（普通/Shift 点选）时调用；Ctrl 减选不联动，便于单独摘出组员。"""
        if self._doc is None:
            return
        oid = item.objectId() if hasattr(item, "objectId") else None
        if not oid:
            return
        obj = self._doc.object(oid)
        if obj is None or not obj.group():
            return
        gid = obj.group()
        for it in self.scene().items():
            o2id = it.objectId() if hasattr(it, "objectId") else None
            if not o2id or o2id == oid:
                continue
            o2 = self._doc.object(o2id)
            if o2 is not None and o2.group() == gid and not o2.isLocked() \
                    and not it.isSelected():
                it.setSelected(True)

    def _applyBoxSelect(self, viewRect: QRectF, mods) -> None:
        """框选落选：左→右包含选择（整体在框内），右→左相交选择；
        Shift 加选 / Ctrl 减选（同原版选择约定）。"""
        if self._doc is None:
            return
        wr = self.mapToScene(viewRect.toRect()).boundingRect().normalized()
        from ui.view_2d.item_factory import POINT_MARKER_PX
        pad = POINT_MARKER_PX / self.zoomScale()   # 点对象按屏幕标记大小容差
        hits = set()
        for obj in self._doc.visibleObjects():
            if obj.isLocked():
                continue   # 锁定对象不参与框选（与 3D 视口/操作轴规则一致）
            if self._objectInRect(obj, wr, self._boxLTR, pad):
                hits.add(obj.id())
        cur = {o.id() for o in self._doc.objects() if o.isSelected()}
        if mods & Qt.KeyboardModifier.ControlModifier:
            newSel = cur - hits
        elif mods & Qt.KeyboardModifier.ShiftModifier:
            newSel = cur | hits
        else:
            if not hits:
                return   # 同原版：空框选保留现有选择
            newSel = hits
        # 群组展开：框选命中组内任一成员则整组选中（跳过锁定对象）；
        # Ctrl 减选不联动，便于单独摘出组员
        if newSel and not (mods & Qt.KeyboardModifier.ControlModifier):
            groups = {o.group() for o in self._doc.objects()
                      if o.id() in newSel and o.group()}
            if groups:
                newSel = {o.id() for o in self._doc.objects()
                          if o.id() in newSel
                          or (o.group() in groups and not o.isLocked())}
        for o in self._doc.objects():
            o.setSelected(o.id() in newSel)
        self._doc.selectionChanged.emit()

    def _objectInRect(self, obj, wr: QRectF, leftToRight: bool,
                      pad: float) -> bool:
        if obj.type() == ObjectType.POINT:
            p = obj.position()
            return wr.adjusted(-pad, -pad, pad, pad).contains(
                QPointF(p.x, p.y))
        pts = obj.points() if hasattr(obj, "points") else None
        if pts is not None and len(pts) >= 2:
            if leftToRight:
                # 包含选择：全部顶点在框内（同原版逐点判定）
                return all(wr.contains(QPointF(p.x, p.y)) for p in pts)
            # 相交选择：任一顶点在框内或任一线段与框相交
            if any(wr.contains(QPointF(p.x, p.y)) for p in pts):
                return True
            n = len(pts)
            closed = bool(getattr(obj, "isClosed", lambda: False)())
            segCount = n if closed else n - 1
            for i in range(segCount):
                if _segIntersectsRect(pts[i], pts[(i + 1) % n], wr):
                    return True
            return False
        # 圆/弧/椭圆等解析对象：按包围盒判定（近似，同原版 3D 选择器做法）
        bb = obj.getBoundingBox()
        if not bb.isValid():
            return False
        br = QRectF(QPointF(bb.min.x, bb.min.y), QPointF(bb.max.x, bb.max.y))
        return wr.contains(br) if leftToRight else bool(wr.intersects(br))

    def wheelEvent(self, event: QWheelEvent) -> None:
        angle = event.angleDelta().y() / 120.0
        if abs(angle) < 1e-6:
            event.accept()
            return
        if app_settings.invert_wheel_zoom:
            angle = -angle   # 设置中开启“反向滚轮缩放”
        factor = 1.2 ** angle
        anchorPt = event.position().toPoint()
        scenePt = self.mapToScene(anchorPt)
        oldAnchor = self.transformationAnchor()
        self.setTransformationAnchor(QGraphicsView.NoAnchor)
        self.scale(factor, factor)
        self.setTransformationAnchor(oldAnchor)
        sceneAfter = self.mapToScene(anchorPt)
        center = self.mapToScene(self.viewport().rect().center())
        self.centerOn(center + scenePt - sceneAfter)
        self._growSceneRect()   # 缩小时同步扩展场景矩形，防止平移被钳制
        self.scaleChanged.emit(self.zoomScale())
        event.accept()

    def zoomScale(self) -> float:
        """当前视图缩放系数（用于像素↔世界尺寸换算，如固定大小的点标记）。"""
        return max(abs(self.transform().m11()), 1e-9)

    def setViewMode(self, mode: int) -> None:
        self._viewMode = mode
        self.resetTransform()
        self.scale(1, -1)
        self.scene().setViewMode(mode)
        self.scaleChanged.emit(self.zoomScale())

    def zoomToFit(self) -> None:
        from PySide6.QtCore import QRectF
        rect = self.scene().itemsBoundingRect()
        if rect.isNull():
            rect = QRectF(-200, -150, 400, 300)
        else:
            rect.adjust(-20, -20, 20, 20)
        self.fitInView(rect, Qt.KeepAspectRatio)
        self._growSceneRect()
        self.scaleChanged.emit(self.zoomScale())
"""操作轴管理器：复刻原项目 ViewportGizmoManager（移动/旋转/缩放）。

复刻要点（与 C++ 原版一致）：
- 选中对象后显示，中心为选择包围盒中心，屏幕固定大小（约视口高度 15%）
- 移动：三轴箭头（拖拽=轴向约束）、田字格（拖拽=平面约束）、单击轴=输入距离
- 旋转：90° 圆弧（首尾各剪 15%），拖拽按屏幕角度增量累积，Shift=15° 吸附，
  拖动时绘制扇形 + 角度文字反馈；单击圆弧=输入角度
- 缩放：负方向虚线轴 + 末端小方块（长度=包围盒一半+固定延伸 10），
  拖拽比例=当前轴向投影距离/初始距离（无量纲、自适应缩放级别），
  Shift=等比；单击方块=输入尺寸（绝对/增量/比例 三种模式）
- Alt+拖拽：先克隆选中对象，变换应用于副本（与 Rhino 一致）
- 高亮：悬停/拖动中的部件变黑（原版 highlightColor = black）

当前对象模型仅支持绕 Z 轴旋转（曲线均为 XY 平面几何），
故旋转圆弧只提供 Z 轴；X/Y 旋转弧待三维实体对象引入后扩展。
"""
from __future__ import annotations
import math
import time
from PySide6.QtCore import QObject, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QFont, QPolygonF

from core.vector.geometry_types import Point3D, BoundingBox3D
from .gizmo_dialog import GizmoInputDialog

# ---------------- 常量（与原项目 GizmoConst / 各 Geometry 头文件一致） ----------------
PICK_RADIUS_AXIS = 8.0
PICK_TOLERANCE_PLANE = 2.0
PICK_RADIUS_ROTATE = 10.0
PICK_RADIUS_SCALE = 10.0
ARROW_SIZE_RATIO = 0.08
ARROW_HEIGHT_RATIO = 0.15
GRID_SIZE_RATIO = 0.3
GRID_OFFSET_RATIO = 0.8
ARC_RADIUS_RATIO = 0.6
ARC_SEGMENTS = 64   # 90° 弧每段约 1.4°，抗锯齿后平滑无棱角（拾取与绘制共用）
SCALE_AXIS_OFFSET_RATIO = 0.05
FIXED_EXTENSION = 10.0
TARGET_RATIO = 0.15          # 操作轴占视口高度的 15%（原版 calculateScaleForFixedSize）
MIN_SCALE, MAX_SCALE = 0.5, 50000.0
CLICK_THRESHOLD_MS = 300
MOVE_THRESHOLD = 0.001
SCALE_MIN, SCALE_MAX = 0.01, 100.0
# 视角可见性阈值（与原项目 ViewportGizmoVisibility 一致）
PLANE_VISIBLE_THRESHOLD = 0.3     # |视线·平面法线| > 0.3（约 72°）田字格才显示，侧对隐藏
AXIS_VISIBLE_THRESHOLD = 0.9397   # cos(20°)：视线与直线轴夹角 < 20° 时隐藏（近端视难拾取）
AXIS_STRICT_HIDE = 0.9962         # cos(5°)：几乎平行时严格强制隐藏（防浮点误差误显示）
ROTATE_VISIBLE_THRESHOLD = 0.5    # cos(60°)：视线与弧轴夹角 < 60° 时旋转弧才显示

_DIRS = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
# 柔化轴色（原版：X 红 / Y 绿 / Z 蓝）
_AXIS_COLORS = (QColor(255, 77, 77), QColor(77, 255, 77), QColor(77, 153, 255))
_HIGHLIGHT = QColor(0, 0, 0)
_ANGLE45 = math.pi / 4.0


def _add(p, d, k):
    return (p[0] + d[0] * k, p[1] + d[1] * k, p[2] + d[2] * k)


def _sub3(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot3(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _len3(v):
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def _ptSegDist(px, py, ax, ay, bx, by) -> float:
    abx, aby = bx - ax, by - ay
    ab2 = abx * abx + aby * aby
    if ab2 < 1e-9:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * abx + (py - ay) * aby) / ab2))
    return math.hypot(px - (ax + t * abx), py - (ay + t * aby))


# ---------------- 视图适配器 ----------------
class View2DAdapter:
    """2D 画布（QGraphicsView）：正交俯视，世界坐标 = 场景坐标。"""
    full3D = False

    def __init__(self, view) -> None:
        self._view = view

    def viewportSize(self):
        vp = self._view.viewport()
        return float(vp.width()), float(vp.height())

    def viewportWidget(self):
        """2D 画布的视口 QWidget（用于把像素坐标换算成全局屏幕坐标）。"""
        return self._view.viewport()

    def project(self, p):
        q = self._view.mapFromScene(QPointF(p[0], p[1]))
        return (float(q.x()), float(q.y()))

    def screenToPlane(self, mx, my, planePt):
        q = self._view.mapToScene(QPoint(int(round(mx)), int(round(my))))
        return (q.x(), q.y(), 0.0)

    def dragPlanePoint(self, mx, my, axis=-1, plane=-1, center=None):
        """拖动取点：2D 画布即 XY 平面（z=0）；axis/plane/center 仅 3D
        适配器使用（Z 向拖动取焦点平面），此处接收并忽略以统一调用签名。"""
        return self.screenToPlane(mx, my, None)

    def worldHeight(self, center) -> float:
        return self.viewportSize()[1] / max(self._view.zoomScale(), 1e-9)

    def viewDirection(self, center):
        """视线方向（从操作轴指向相机）：2D 为正交俯视，固定 +Z。"""
        return (0.0, 0.0, 1.0)

    def redraw(self) -> None:
        self._view.viewport().update()


class View3DAdapter:
    """3D/浮雕视口：复用 Viewport3D 的软渲染相机做投影/反投影。"""
    full3D = True

    def __init__(self, vp) -> None:
        self._vp = vp

    def viewportSize(self):
        return float(self._vp.width()), float(self._vp.height())

    def viewportWidget(self):
        """3D/浮雕视口的 QWidget（用于把像素坐标换算成全局屏幕坐标）。"""
        return self._vp

    def project(self, p):
        w, h = self.viewportSize()
        if w < 2 or h < 2:
            return None
        s = self._vp._project(p, self._vp._camera(), w, h)
        return None if s is None else (float(s[0]), float(s[1]))

    def screenToPlane(self, mx, my, planePt):
        """屏幕点反投影到“过 planePt、垂直视线”的焦点平面（原版 getPointOnFocalPlane）。"""
        vp = self._vp
        w, h = self.viewportSize()
        if w < 2 or h < 2:
            return None
        eye, right, up, fwd, ortho = vp._camera()
        cx, cy = w / 2.0, h / 2.0
        if ortho:
            scale = vp._orthoScale(w, h)
            if scale < 1e-12:
                return None
            x = (mx - cx) / scale
            y = -(my - cy) / scale
            return (planePt[0] + x * right[0] + y * up[0],
                    planePt[1] + x * right[1] + y * up[1],
                    planePt[2] + x * right[2] + y * up[2])
        focal = vp._focal(h)
        sx = (mx - cx) / focal
        sy = -(my - cy) / focal
        d = (sx * right[0] + sy * up[0] + fwd[0],
             sx * right[1] + sy * up[1] + fwd[1],
             sx * right[2] + sy * up[2] + fwd[2])
        denom = _dot3(d, fwd)
        if abs(denom) < 1e-12:
            return None
        num = _dot3(_sub3(planePt, eye), fwd)
        t = num / denom
        return (eye[0] + t * d[0], eye[1] + t * d[1], eye[2] + t * d[2])

    def dragPlanePoint(self, mx, my, axis=-1, plane=-1, center=None):
        """拖动取点。Z 向拖动（Z 轴 / XZ / YZ 平面约束）取"过操作轴中心、
        垂直视线"的焦点平面——否则射线与 z=0 平面求交 Z 分量恒 0，
        Z 轴移动完全失效；其余保持 z=0 平面（透视下焦点平面映射非线性，
        XY 平面对象拖动必须落 z=0 才跟手），视线近平行时退回焦点平面。"""
        if axis == 2 or plane in (1, 2):
            pt = center if center is not None else (0.0, 0.0, 0.0)
            return self.screenToPlane(mx, my, pt)
        p = self._vp._screenToXYPlane(float(mx), float(my))
        if p is not None:
            return (p.x, p.y, p.z)
        return self.screenToPlane(mx, my, (0.0, 0.0, 0.0))

    def worldHeight(self, center) -> float:
        from ui.view_3d.viewport_3d import FOV_DEG
        vp = self._vp
        w, h = self.viewportSize()
        eye, right, up, fwd, ortho = vp._camera()
        if ortho:
            return h / max(vp._orthoScale(w, h), 1e-12)
        dist = _len3(_sub3(center, eye))
        return 2.0 * dist * math.tan(math.radians(FOV_DEG) / 2.0)

    def viewDirection(self, center):
        """视线方向（从操作轴指向相机，原版 calculateViewDirection）。"""
        eye, _, _, _, _ = self._vp._camera()
        d = _sub3(eye, center)
        n = _len3(d)
        if n > 0.001:
            return (d[0] / n, d[1] / n, d[2] / n)
        return (0.0, 0.0, -1.0)

    def redraw(self) -> None:
        self._vp.update()


# ---------------- 管理器 ----------------
class GizmoManager(QObject):
    def __init__(self, doc, cm=None, parent=None) -> None:
        super().__init__(parent)
        self._doc = doc
        self._cm = cm
        self._enabled = True
        self._visible = False
        self._adapters = []
        self._editCtrl = None   # 曲线编辑控制器：选中控制点时操作轴作用于控制点
        self._center = (0.0, 0.0, 0.0)
        self._bboxSize = (1.0, 1.0, 1.0)
        # 交互状态
        self._mode = None               # "move" / "rotate" / "scale"
        self._dragging = False
        self._activeAxis = -1
        self._activePlane = -1
        self._activeAdapter = None
        self._lastScreen = None
        self._pressTime = 0
        self._dragStartCenter = (0.0, 0.0, 0.0)
        # 移动
        self._totalDelta = (0.0, 0.0, 0.0)
        # 旋转
        self._lastAngle = 0.0
        self._worldRotA = None
        self._accumDeg = 0.0
        self._prevDisplayDeg = 0.0
        self._feedbackStart = 0.0
        self._feedbackDeg = 0.0
        # 缩放
        self._startAxisDist = 0.0
        self._curScale = 1.0
        self._uniform = False
        self._origBbox = (1.0, 1.0, 1.0)
        # 悬停高亮
        self._hover = None
        # 单击精确输入小浮层（非模态，需持有引用防止提前回收）
        self._inputDialog = None

        # 信号槽保存引用：setDocument 换绑文档时需按引用断开（lambda 直接断开会误伤）
        self._onDocAdded = lambda *_: self.refresh()
        self._onDocRemoved = lambda *_: self.refresh()
        self._onObjChanged = lambda *_: self.refresh()
        doc.selectionChanged.connect(self.refresh)
        doc.objectChanged.connect(self._onObjChanged)
        doc.objectAdded.connect(self._onDocAdded)
        doc.objectRemoved.connect(self._onDocRemoved)

    # ---------------- 对外接口 ----------------

    def setDocument(self, doc) -> None:
        """换绑文档（多文件切换）：断开旧文档信号，复位悬停态后刷新。"""
        if doc is self._doc:
            return
        try:
            self._doc.selectionChanged.disconnect(self.refresh)
            self._doc.objectChanged.disconnect(self._onObjChanged)
            self._doc.objectAdded.disconnect(self._onDocAdded)
            self._doc.objectRemoved.disconnect(self._onDocRemoved)
        except (RuntimeError, TypeError):
            pass
        self._doc = doc
        doc.selectionChanged.connect(self.refresh)
        doc.objectChanged.connect(self._onObjChanged)
        doc.objectAdded.connect(self._onDocAdded)
        doc.objectRemoved.connect(self._onDocRemoved)
        self._hover = None
        self.refresh()
    def attachView(self, adapter) -> None:
        self._adapters.append(adapter)

    def setEnabled(self, on: bool) -> None:
        self._enabled = bool(on)
        self._redrawAll()

    def setCurveEdit(self, ctrl) -> None:
        self._editCtrl = ctrl

    def isEnabled(self) -> bool:
        return self._enabled

    def isVisible(self) -> bool:
        return self._isActive()

    def isDragging(self) -> bool:
        return self._dragging

    def refresh(self) -> None:
        """按当前选择重算中心与包围盒尺寸（拖动中不移动中心，避免抖动）。

        编辑曲线开启时：优先以选中的控制点包围盒为基准，其次以目标曲线
        包围盒为基准，操作轴变换即作用于控制点。"""
        if self._editCtrl is not None and self._editCtrl.isActive():
            # 已选中控制点：操作轴以控制点包围盒为基准，作用于控制点
            if self._editCtrl.hasSelectedControls():
                bb = self._editCtrl.selectedBBox()
                if bb is not None and bb.isValid():
                    c = bb.center()
                    if not self._dragging:
                        self._center = (c.x, c.y, c.z)
                    self._bboxSize = (max(bb.width(), 1e-9),
                                      max(bb.height(), 1e-9),
                                      max(bb.depth(), 1e-9))
                    self._visible = True
                    self._redrawAll()
                    return
            # 编辑模式开启但未选中控制点：照常按对象包围盒显示操作轴
        sel = [o for o in self._doc.selectedObjects() if not o.isLocked()]
        if not sel:
            self._visible = False
            self._redrawAll()
            return
        bb = BoundingBox3D()
        for o in sel:
            ob = o.getBoundingBox()
            if ob.isValid():
                bb.expand(ob.min)
                bb.expand(ob.max)
        if not bb.isValid():
            self._visible = False
            self._redrawAll()
            return
        c = bb.center()
        if not self._dragging:
            self._center = (c.x, c.y, c.z)
        self._bboxSize = (max(bb.width(), 1e-9),
                          max(bb.height(), 1e-9),
                          max(bb.depth(), 1e-9))
        self._visible = True
        self._redrawAll()

    def _isActive(self) -> bool:
        if not self._enabled or not self._visible:
            return False
        if self._cm is not None and self._cm.hasActive():
            return False
        return True

    def _redrawAll(self) -> None:
        for ad in self._adapters:
            ad.redraw()

    def _gizmoScale(self, adapter) -> float:
        return min(MAX_SCALE, max(MIN_SCALE, TARGET_RATIO * adapter.worldHeight(self._center)))

    def _axisList(self, adapter):
        return (0, 1, 2) if adapter.full3D else (0, 1)

    def _planeList(self, adapter):
        return (0, 1, 2) if adapter.full3D else (0,)

    # ---------------- 视角可见性（复刻原版 ViewportGizmoVisibility） ----------------
    @staticmethod
    def _linearAxisVisible(viewDir, axis: int) -> bool:
        """直线轴（移动/缩放）：视线近于沿轴时隐藏（含 5° 严格隐藏）。"""
        d = abs(viewDir[axis])
        return d < AXIS_VISIBLE_THRESHOLD and d <= AXIS_STRICT_HIDE

    @staticmethod
    def _rotateAxisVisible(viewDir, axis: int) -> bool:
        """圆弧轴（旋转）：视线与轴夹角 < 60° 时显示（侧视时弧变线，隐藏）。"""
        return abs(viewDir[axis]) > ROTATE_VISIBLE_THRESHOLD

    @staticmethod
    def _planeVisible(viewDir, plane: int) -> bool:
        """田字格：|视线·平面法线| > 0.3 时显示（XY→Z / XZ→Y / YZ→X，法线索引 = 2 - 平面索引）。"""
        return abs(viewDir[2 - plane]) > PLANE_VISIBLE_THRESHOLD

    def _visibleAxes(self, adapter):
        """当前视角下可见的直线轴/旋转弧/田字格索引（拾取与绘制共用）。
        旋转弧候选为全轴集合：2D 下直线轴不含 Z，但 Z 弧仍需参与判断。"""
        vd = adapter.viewDirection(self._center)
        lin = tuple(i for i in self._axisList(adapter) if self._linearAxisVisible(vd, i))
        rot = tuple(i for i in (0, 1, 2) if self._rotateAxisVisible(vd, i))
        pl = tuple(pi for pi in self._planeList(adapter) if self._planeVisible(vd, pi))
        return lin, rot, pl

    # ---------------- 拾取（屏幕像素空间，与原版半径一致） ----------------
    def pickMoveAxis(self, adapter, pos, scale) -> int:
        c = self._center
        linAxes, _, _ = self._visibleAxes(adapter)
        for i in linAxes:
            s0 = adapter.project(_add(c, _DIRS[i], 0.1 * scale))
            s1 = adapter.project(_add(c, _DIRS[i], 1.0 * scale))
            if s0 is None or s1 is None:
                continue
            if _ptSegDist(pos.x(), pos.y(), s0[0], s0[1], s1[0], s1[1]) < PICK_RADIUS_AXIS:
                return i
        return -1

    def pickMovePlane(self, adapter, pos, scale) -> int:
        c = self._center
        axisLen = 1.0 * scale
        offsetDist = axisLen * GRID_OFFSET_RATIO
        gridSize = axisLen * GRID_SIZE_RATIO
        half = gridSize / 2.0
        ox = offsetDist * math.cos(_ANGLE45)
        oz = offsetDist * math.sin(_ANGLE45)
        # (平面中心偏移, topLeft 增量, bottomRight 增量)
        defs = (
            ((ox, ox, 0.0), (-half, half, 0.0), (half, -half, 0.0)),   # XY
            ((ox, 0.0, oz), (-half, 0.0, half), (half, 0.0, -half)),   # XZ
            ((0.0, ox, oz), (0.0, half, half), (0.0, -half, -half)),   # YZ
        )
        _, _, visPlanes = self._visibleAxes(adapter)
        for pi in visPlanes:
            off, tl, br = defs[pi]
            pc = (c[0] + off[0], c[1] + off[1], c[2] + off[2])
            s0 = adapter.project((pc[0] + tl[0], pc[1] + tl[1], pc[2] + tl[2]))
            s1 = adapter.project((pc[0] + br[0], pc[1] + br[1], pc[2] + br[2]))
            if s0 is None or s1 is None:
                continue
            minX = min(s0[0], s1[0]) - PICK_TOLERANCE_PLANE
            maxX = max(s0[0], s1[0]) + PICK_TOLERANCE_PLANE
            minY = min(s0[1], s1[1]) - PICK_TOLERANCE_PLANE
            maxY = max(s0[1], s1[1]) + PICK_TOLERANCE_PLANE
            if minX <= pos.x() <= maxX and minY <= pos.y() <= maxY:
                return pi
        return -1

    def _arcAngles(self):
        totalArc = math.pi * 0.5
        trim = totalArc * 0.15
        start = math.pi + trim
        end = start + totalArc - 2.0 * trim
        return start, end

    def pickRotateArc(self, adapter, pos, scale) -> int:
        """只有 Z 轴圆弧（对象模型仅支持绕 Z 旋转）；返回 2 或 -1。"""
        _, rotAxes, _ = self._visibleAxes(adapter)
        if 2 not in rotAxes:
            return -1
        c = self._center
        r = ARC_RADIUS_RATIO * scale
        start, end = self._arcAngles()
        prev = adapter.project(_add(c, (math.cos(start), math.sin(start), 0.0), r))
        for i in range(1, ARC_SEGMENTS + 1):
            a = start + i / ARC_SEGMENTS * (end - start)
            cur = adapter.project(_add(c, (math.cos(a), math.sin(a), 0.0), r))
            if prev is not None and cur is not None and \
               _ptSegDist(pos.x(), pos.y(), prev[0], prev[1], cur[0], cur[1]) < PICK_RADIUS_ROTATE:
                return 2
            prev = cur
        return -1

    def scaleAxisLength(self, axis: int, scaleFactor: float = 1.0) -> float:
        """缩放轴长度 = (原始包围盒尺寸 × 当前缩放) 的一半 + 固定延伸（原版统一公式）。"""
        size = self._origBbox[axis] if self._dragging and self._mode == "scale" \
            else self._bboxSize[axis]
        return size * scaleFactor * 0.5 + FIXED_EXTENSION

    def pickScaleHandle(self, adapter, pos) -> int:
        c = self._center
        linAxes, _, _ = self._visibleAxes(adapter)
        for i in linAxes:
            L = self.scaleAxisLength(i)
            s = adapter.project(_add(c, _DIRS[i], -L))
            if s is None:
                continue
            if math.hypot(pos.x() - s[0], pos.y() - s[1]) < PICK_RADIUS_SCALE:
                return i
        return -1

    # ---------------- 事件入口 ----------------
    def handleMousePress(self, adapter, pos, mods) -> bool:
        if not self._isActive():
            return False
        scale = self._gizmoScale(adapter)
        axis = self.pickMoveAxis(adapter, pos, scale)
        plane = self.pickMovePlane(adapter, pos, scale) if axis < 0 else -1
        # 编辑曲线模式变换控制点时 Alt+克隆无意义，跳过克隆直接变换
        altClone = bool(mods & Qt.AltModifier) and self._editTarget() is None
        if axis >= 0 or plane >= 0:
            self._doc.beginEdit()
            if altClone:
                self._cloneSelection()
            self._startMoveDrag(adapter, pos, axis, plane)
            return True
        rAxis = self.pickRotateArc(adapter, pos, scale)
        if rAxis >= 0:
            self._doc.beginEdit()
            if altClone:
                self._cloneSelection()
            self._startRotateDrag(adapter, pos, rAxis)
            return True
        sAxis = self.pickScaleHandle(adapter, pos)
        if sAxis >= 0:
            self._doc.beginEdit()
            if altClone:
                self._cloneSelection()
            self._startScaleDrag(adapter, pos, sAxis, mods)
            return True
        return False

    def handleMouseMove(self, adapter, pos, mods) -> bool:
        if not self._isActive():
            return False
        if not self._dragging:
            self._updateHover(adapter, pos)
            return False
        ad = self._activeAdapter or adapter
        if self._mode == "move":
            self._doMoveDrag(ad, pos)
        elif self._mode == "rotate":
            self._doRotateDrag(ad, pos, mods)
        elif self._mode == "scale":
            self._doScaleDrag(ad, pos)
        return True

    def handleMouseRelease(self, adapter, pos) -> bool:
        if not self._dragging:
            return False
        if self._mode == "move":
            self._finishMoveDrag(adapter, pos)
        elif self._mode == "rotate":
            self._finishRotateDrag(adapter, pos)
        elif self._mode == "scale":
            self._finishScaleDrag(adapter, pos)
        self._doc.endEdit()
        return True

    def _updateHover(self, adapter, pos) -> None:
        hover = None
        scale = self._gizmoScale(adapter)
        axis = self.pickMoveAxis(adapter, pos, scale)
        if axis >= 0:
            hover = ("axis", axis)
        else:
            plane = self.pickMovePlane(adapter, pos, scale)
            if plane >= 0:
                hover = ("plane", plane)
            else:
                rAxis = self.pickRotateArc(adapter, pos, scale)
                if rAxis >= 0:
                    hover = ("rot", rAxis)
                else:
                    sAxis = self.pickScaleHandle(adapter, pos)
                    if sAxis >= 0:
                        hover = ("scale", sAxis)
        if hover != self._hover:
            self._hover = hover
            adapter.redraw()

    # ---------------- 移动 ----------------
    def _startMoveDrag(self, adapter, pos, axis, plane) -> None:
        self._mode = "move"
        self._dragging = True
        self._activeAxis = axis
        self._activePlane = plane
        self._activeAdapter = adapter
        self._lastScreen = pos
        self._pressTime = time.monotonic() * 1000.0
        self._totalDelta = (0.0, 0.0, 0.0)
        self._dragStartCenter = self._center
        self._hover = None

    def _constrain(self, delta):
        if self._activeAxis >= 0:
            d = _DIRS[self._activeAxis]
            proj = _dot3(delta, d)
            return (d[0] * proj, d[1] * proj, d[2] * proj)
        if self._activePlane == 0:
            return (delta[0], delta[1], 0.0)
        if self._activePlane == 1:
            return (delta[0], 0.0, delta[2])
        if self._activePlane == 2:
            return (0.0, delta[1], delta[2])
        return delta

    def _doMoveDrag(self, adapter, pos) -> None:
        # Z 向拖动（Z 轴 / XZ / YZ 平面约束）取"过拖动起始中心、垂直视线"
        # 的焦点平面（锚点固定为拖动开始时的中心，透视下平面不随位移漂移）；
        # 其余保持 z=0 平面（XY 平面对象拖动必须落 z=0 才跟手）
        anchor = self._dragStartCenter
        prev = adapter.dragPlanePoint(self._lastScreen.x(),
                                      self._lastScreen.y(),
                                      self._activeAxis, self._activePlane,
                                      anchor)
        cur = adapter.dragPlanePoint(pos.x(), pos.y(),
                                     self._activeAxis, self._activePlane,
                                     anchor)
        self._lastScreen = pos
        if prev is None or cur is None:
            return
        delta = self._constrain(_sub3(cur, prev))
        if _len3(delta) < 1e-12:
            return
        self._totalDelta = (self._totalDelta[0] + delta[0],
                            self._totalDelta[1] + delta[1],
                            self._totalDelta[2] + delta[2])
        self._translateSelection(delta)
        self._center = _add(self._center, delta, 1.0)
        adapter.redraw()

    def _finishMoveDrag(self, adapter=None, pos=None) -> None:
        duration = time.monotonic() * 1000.0 - self._pressTime
        axis = self._activeAxis
        dist = _len3(self._totalDelta)
        self._cleanupDrag()
        if duration < CLICK_THRESHOLD_MS and dist < MOVE_THRESHOLD and axis >= 0:
            self._showMoveDialog(axis, adapter, pos)
            return
        self.refresh()

    def _showMoveDialog(self, axis: int, adapter=None, pos=None) -> None:
        dlg = GizmoInputDialog("移动距离", "距离", 0.0, -9999.9, 9999.9,
                               decimals=1, spacingTitle=True,
                               persistKey="move")
        self._showDialogAt(dlg, adapter, pos)
        dlg.accepted.connect(lambda: self._applyMoveDialog(axis, dlg))
        self._openInputDialog(dlg)

    def _applyMoveDialog(self, axis: int, dlg) -> None:
        # 单击弹出的精确输入在鼠标释放（endEdit）之后应用，需自带撤销事务
        self._doc.beginEdit()
        v = dlg.value()
        d = (_DIRS[axis][0] * v, _DIRS[axis][1] * v, _DIRS[axis][2] * v)
        if not dlg.copyRequested():
            self._translateSelection(d)
            self._doc.endEdit()
            return
        # 间距模式：复制步距 = 净间距 + 物体在该轴尺寸（相邻份边到边净距 = 输入值）
        if dlg.spacingMode():
            size = self._bboxSize[axis] if self._bboxSize else 0.0
            v += size if v >= 0 else -size
            d = (_DIRS[axis][0] * v, _DIRS[axis][1] * v, _DIRS[axis][2] * v)
        # 连续复制：每轮克隆当前选中的最新一份，再沿轴平移一个间隔
        for _ in range(dlg.copyCount()):
            self._cloneSelection()
            self._translateSelection(d)
        self._doc.endEdit()

    # ---------------- 旋转 ----------------
    def _screenAngle(self, adapter, pos) -> float:
        cs = adapter.project(self._center)
        if cs is None:
            return 0.0
        return -math.atan2(pos.y() - cs[1], pos.x() - cs[0])

    def _startRotateDrag(self, adapter, pos, axis) -> None:
        self._mode = "rotate"
        self._dragging = True
        self._activeAxis = axis
        self._activePlane = -1
        self._activeAdapter = adapter
        self._lastScreen = pos
        self._pressTime = time.monotonic() * 1000.0
        a0 = self._screenAngle(adapter, pos)
        self._lastAngle = a0
        # 世界角度基准：透视下投影中心不是圆弧视觉中心，屏幕角会失真，
        # 改用按下点相对包围盒中心的世界角累加（任意视角精确）
        pw = adapter.dragPlanePoint(pos.x(), pos.y())
        if pw is not None:
            self._worldRotA = math.atan2(pw[1] - self._center[1],
                                         pw[0] - self._center[0])
        else:
            self._worldRotA = None
        self._accumDeg = 0.0
        self._prevDisplayDeg = 0.0
        self._feedbackStart = a0
        self._feedbackDeg = 0.0
        self._hover = None

    def _doRotateDrag(self, adapter, pos, mods) -> None:
        if self._worldRotA is not None:
            # 世界角累加：透视/斜视角下与鼠标拖动方向、幅度严格一致
            pw = adapter.dragPlanePoint(pos.x(), pos.y())
            if pw is None:
                return
            curA = math.atan2(pw[1] - self._center[1], pw[0] - self._center[0])
            d = curA - self._worldRotA
            if d > math.pi:
                d -= 2.0 * math.pi
            elif d < -math.pi:
                d += 2.0 * math.pi
            self._worldRotA = curA
        else:
            cur = self._screenAngle(adapter, pos)
            d = cur - self._lastAngle
            if d > math.pi:
                d -= 2.0 * math.pi
            elif d < -math.pi:
                d += 2.0 * math.pi
            self._lastAngle = cur
        self._accumDeg += math.degrees(d)
        while self._accumDeg >= 360.0:
            self._accumDeg -= 360.0
        while self._accumDeg < -360.0:
            self._accumDeg += 360.0
        display = self._accumDeg
        if mods & Qt.ShiftModifier:
            display = round(display / 15.0) * 15.0
        ddeg = display - self._prevDisplayDeg
        if abs(ddeg) > 1e-9:
            self._rotateSelectionZ(math.radians(ddeg))
        self._prevDisplayDeg = display
        self._feedbackDeg = display
        adapter.redraw()

    def _finishRotateDrag(self, adapter=None, pos=None) -> None:
        duration = time.monotonic() * 1000.0 - self._pressTime
        absRotation = abs(self._accumDeg)
        self._feedbackDeg = 0.0
        self._cleanupDrag()
        # 单击检测（与原版一致）：时间短且旋转角度小 → 弹出输入框
        if duration < CLICK_THRESHOLD_MS and absRotation < MOVE_THRESHOLD:
            self._showRotateDialog(adapter, pos)
            return
        self.refresh()

    def _showRotateDialog(self, adapter=None, pos=None) -> None:
        dlg = GizmoInputDialog("旋转角度", "角度", 0.0, -9999.999, 9999.999,
                               suffix="°", decimals=1, persistKey="rotate")
        self._showDialogAt(dlg, adapter, pos)
        dlg.accepted.connect(lambda: self._applyRotateDialog(dlg))
        self._openInputDialog(dlg)

    def _applyRotateDialog(self, dlg) -> None:
        self._doc.beginEdit()
        rad = math.radians(dlg.value())
        if not dlg.copyRequested():
            self._rotateSelectionZ(rad)
            self._doc.endEdit()
            return
        # 连续复制：每轮克隆最新一份再绕中心旋转一个角度（环列）
        for _ in range(dlg.copyCount()):
            self._cloneSelection()
            self._rotateSelectionZ(rad)
        self._doc.endEdit()

    # ---------------- 缩放 ----------------
    def _startScaleDrag(self, adapter, pos, axis, mods) -> None:
        self._mode = "scale"
        self._dragging = True
        self._activeAxis = axis
        self._activePlane = -1
        self._activeAdapter = adapter
        self._lastScreen = pos
        self._pressTime = time.monotonic() * 1000.0
        self._uniform = bool(mods & Qt.ShiftModifier)
        self._origBbox = self._bboxSize
        self._curScale = 1.0
        sw = adapter.dragPlanePoint(pos.x(), pos.y())
        neg = tuple(-v for v in _DIRS[axis])
        self._startAxisDist = _dot3(_sub3(sw, self._center), neg) if sw else 0.0
        self._hover = None

    def _doScaleDrag(self, adapter, pos) -> None:
        cur = adapter.dragPlanePoint(pos.x(), pos.y(),
                                     self._activeAxis, -1, self._center)
        if cur is None or abs(self._startAxisDist) < 1e-6:
            return
        neg = tuple(-v for v in _DIRS[self._activeAxis])
        factor = _dot3(_sub3(cur, self._center), neg) / self._startAxisDist
        factor = min(SCALE_MAX, max(SCALE_MIN, factor))
        inc = factor / self._curScale if self._curScale > 1e-12 else 1.0
        if abs(inc - 1.0) > 1e-9:
            if self._uniform:
                self._scaleSelection(inc, inc, inc)
            else:
                sx = sy = sz = 1.0
                if self._activeAxis == 0:
                    sx = inc
                elif self._activeAxis == 1:
                    sy = inc
                else:
                    sz = inc
                self._scaleSelection(sx, sy, sz)
        self._curScale = factor
        adapter.redraw()

    def _finishScaleDrag(self, adapter=None, pos=None) -> None:
        duration = time.monotonic() * 1000.0 - self._pressTime
        isClick = duration < CLICK_THRESHOLD_MS and abs(self._curScale - 1.0) < 0.005
        axis = self._activeAxis
        self._cleanupDrag()
        if isClick and axis >= 0:
            self._showScaleDialog(axis, adapter, pos)
            return
        self.refresh()

    def _showScaleDialog(self, axis: int, adapter=None, pos=None) -> None:
        dlg = GizmoInputDialog("缩放尺寸", "尺寸", 1.0, -999999.0, 999999.0,
                               decimals=3, scaleModes=True,
                               persistKey="scale")
        self._showDialogAt(dlg, adapter, pos)
        dlg.accepted.connect(lambda: self._applyScaleDialog(axis, dlg))
        self._openInputDialog(dlg)

    def _applyScaleDialog(self, axis: int, dlg) -> None:
        self._doc.beginEdit()
        size = self._bboxSize[axis]
        v = dlg.value()
        mode = dlg.scaleMode()
        if mode == 0:
            factor = v / size if size > 1e-6 else 1.0
        elif mode == 1:
            newSize = size + v
            factor = newSize / size if size > 1e-6 and newSize > 1e-6 else 1.0
        else:
            factor = v
        if abs(factor) < 1e-9:
            factor = 1e-9
        uniform = dlg.uniformAxis()
        if not dlg.copyRequested():
            self._scaleByFactor(factor, axis, uniform)
            self._doc.endEdit()
            return
        # 连续复制：每轮克隆最新一份再应用同一比例（副本逐份等比递进）
        for _ in range(dlg.copyCount()):
            self._cloneSelection()
            self._scaleByFactor(factor, axis, uniform)
        self._doc.endEdit()

    def _scaleByFactor(self, factor: float, axis: int, uniform: bool) -> None:
        if uniform:
            self._scaleSelection(factor, factor, factor)
            return
        sx = sy = sz = 1.0
        if axis == 0:
            sx = factor
        elif axis == 1:
            sy = factor
        else:
            sz = factor
        self._scaleSelection(sx, sy, sz)

    def _showDialogAt(self, dlg, adapter, pos) -> None:
        """把单击精确输入小浮层定位到鼠标右下方。

        adapter/pos 为视口像素坐标（释放鼠标时的位置）；先换算成全局屏幕坐标，
        再交给对话框按边缘自动翻转定位。两者为空时保持默认位置。
        """
        if adapter is not None and pos is not None:
            vw = adapter.viewportWidget()
            gp = vw.mapToGlobal(QPoint(int(round(pos.x())), int(round(pos.y()))))
            dlg.showAt(gp)

    def _openInputDialog(self, dlg) -> None:
        """非模态显示输入小浮层（确认/取消在对话框内部通过外部点击/按键处理）。"""
        if self._inputDialog is not None:
            self._inputDialog.reject()
        self._inputDialog = dlg
        dlg.finished.connect(lambda _r: self._closeInputDialog(dlg))
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _closeInputDialog(self, dlg) -> None:
        if self._inputDialog is dlg:
            self._inputDialog = None
        self.refresh()

    def _cleanupDrag(self) -> None:
        self._dragging = False
        self._mode = None
        self._activeAxis = -1
        self._activePlane = -1
        self._activeAdapter = None
        self._hover = None
        self._redrawAll()

    # ---------------- 变换应用 ----------------
    def _transformable(self):
        return [o for o in self._doc.selectedObjects() if not o.isLocked()]

    def _editTarget(self):
        """编辑曲线开启且选中控制点时返回控制器，否则 None（变换作用于对象）。"""
        if self._editCtrl is not None and self._editCtrl.hasSelectedControls():
            return self._editCtrl
        return None

    def _translateSelection(self, d) -> None:
        ctrl = self._editTarget()
        if ctrl is not None:
            ctrl.translateSelected(d[0], d[1], d[2])
            return
        for obj in self._transformable():
            obj.translate(d[0], d[1], d[2])
            self._doc.notifyObjectChanged(obj.id())

    def _rotateSelectionZ(self, rad: float) -> None:
        ctrl = self._editTarget()
        if ctrl is not None:
            cx, cy, cz = self._center
            ctrl.rotateSelected(rad, cx, cy)
            return
        cx, cy, cz = self._center
        for obj in self._transformable():
            obj.translate(-cx, -cy, -cz)
            obj.rotate(rad, 0.0, 0.0, 1.0)
            obj.translate(cx, cy, cz)
            self._doc.notifyObjectChanged(obj.id())

    def _scaleSelection(self, sx: float, sy: float, sz: float) -> None:
        ctrl = self._editTarget()
        if ctrl is not None:
            cx, cy, cz = self._center
            ctrl.scaleSelected(sx, sy, cx, cy)
            return
        c = Point3D(*self._center)
        for obj in self._transformable():
            obj.scaleAround(sx, sy, sz, c)
            self._doc.notifyObjectChanged(obj.id())

    def _cloneSelection(self) -> None:
        """Alt+复制：克隆选中对象，变换应用于副本（与原版 cloneSelectedObjects 一致）。"""
        bridge = getattr(self._doc, "_bridge", None)
        clones = []
        for obj in list(self._doc.selectedObjects()):
            c = obj.clone()
            color, lw = c.color(), c.lineWidth()
            obj.setSelected(False)
            # 同步取消场景图元选中（阻断桥同步，防止残留选中被回写文档）
            if bridge is not None:
                bridge._syncing = True
                try:
                    it0 = bridge.item(obj.id())
                    if it0 is not None and it0.isSelected():
                        it0.setSelected(False)
                    self._doc.addObject(c)
                    itc = bridge.item(c.id())
                    if itc is not None:
                        itc.setSelected(True)
                finally:
                    bridge._syncing = False
            else:
                self._doc.addObject(c)
            c.setColor(color)
            c.setLineWidth(lw)
            c.setSelected(True)
            self._doc.notifyObjectChanged(c.id())
            clones.append(c)
        if clones:
            self._doc.selectionChanged.emit()
            self.refresh()

    # ---------------- 绘制 ----------------
    def paint(self, painter, adapter) -> None:
        """在指定视口叠加绘制操作轴（屏幕像素空间，绘制前重置画家变换）。"""
        if not self._isActive():
            return
        w, h = adapter.viewportSize()
        if w < 2 or h < 2:
            return
        scale = self._gizmoScale(adapter)
        painter.save()
        painter.resetTransform()
        linAxes, rotAxes, visPlanes = self._visibleAxes(adapter)
        self._drawScaleAxes(painter, adapter, linAxes)
        self._drawMoveAxes(painter, adapter, scale, linAxes)
        self._drawGrids(painter, adapter, scale, visPlanes)
        self._drawRotateArc(painter, adapter, scale, rotAxes)
        self._drawOriginMarker(painter, adapter)
        if self._dragging and self._mode == "move":
            self._drawMovePreviewLine(painter, adapter)
        if self._dragging and self._mode == "rotate":
            self._drawRotateFeedback(painter, adapter, scale)
        painter.restore()

    def _pen(self, color, width=2.0, dash=False):
        from PySide6.QtGui import QPen
        pen = QPen(color, width)
        pen.setCosmetic(True)
        if dash:
            pen.setStyle(Qt.PenStyle.DashLine)
        return pen

    def _hl(self, kind, index, color):
        """悬停/拖动中的部件用黑色高亮（与原版 highlightColor 一致）。"""
        active = self._dragging and \
            ((kind == "axis" and self._mode == "move" and self._activeAxis == index) or
             (kind == "plane" and self._mode == "move" and self._activePlane == index) or
             (kind == "rot" and self._mode == "rotate" and self._activeAxis == index) or
             (kind == "scale" and self._mode == "scale" and self._activeAxis == index))
        if active or self._hover == (kind, index):
            return _HIGHLIGHT
        return color

    def _drawMoveAxes(self, painter, adapter, scale, axes) -> None:
        c = self._center
        arrowH = ARROW_HEIGHT_RATIO * scale
        arrowHalf = ARROW_SIZE_RATIO * scale * 0.5
        perps = ((0.0, 1.0, 0.0), (1.0, 0.0, 0.0), (1.0, 0.0, 0.0))
        for i in axes:
            color = self._hl("axis", i, _AXIS_COLORS[i])
            s0 = adapter.project(_add(c, _DIRS[i], 0.1 * scale))
            s1 = adapter.project(_add(c, _DIRS[i], scale - arrowH))
            if s0 is None or s1 is None:
                continue
            painter.setPen(self._pen(color, 2.0))
            painter.drawLine(QPointF(*s0), QPointF(*s1))
            # 箭头（三维空间三角形投影，任意视角正确）
            tip = adapter.project(_add(c, _DIRS[i], scale))
            b0 = adapter.project(_add(_add(c, _DIRS[i], scale - arrowH), perps[i], arrowHalf))
            b1 = adapter.project(_add(_add(c, _DIRS[i], scale - arrowH), perps[i], -arrowHalf))
            if tip is None or b0 is None or b1 is None:
                continue
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawPolygon(QPolygonF([QPointF(*tip), QPointF(*b0), QPointF(*b1)]))

    def _drawGrids(self, painter, adapter, scale, planes) -> None:
        """田字格：每个平面 3×3 线（横竖两色），中心在轴间 45° 对角线上。"""
        c = self._center
        offsetDist = scale * GRID_OFFSET_RATIO
        half = scale * GRID_SIZE_RATIO / 2.0
        ox = offsetDist * math.cos(_ANGLE45)
        oz = offsetDist * math.sin(_ANGLE45)
        # (中心偏移, 横线方向, 竖线方向, 横色, 竖色)
        defs = (
            ((ox, ox, 0.0), (1, 0, 0), (0, 1, 0), _AXIS_COLORS[0], _AXIS_COLORS[1]),
            ((ox, 0.0, oz), (1, 0, 0), (0, 0, 1), _AXIS_COLORS[0], QColor(77, 77, 255)),
            ((0.0, ox, oz), (0, 1, 0), (0, 0, 1), _AXIS_COLORS[1], QColor(77, 77, 255)),
        )
        for pi in planes:
            off, hdir, vdir, hc, vc = defs[pi]
            pc = (c[0] + off[0], c[1] + off[1], c[2] + off[2])
            hl = self._hl("plane", pi, None) is _HIGHLIGHT
            hcol = _HIGHLIGHT if hl else hc
            vcol = _HIGHLIGHT if hl else vc
            for k in (-1, 0, 1):
                painter.setPen(self._pen(hcol, 2.0))
                m = _add(pc, vdir, k * half)
                a = adapter.project(_add(m, hdir, -half))
                b = adapter.project(_add(m, hdir, half))
                if a is not None and b is not None:
                    painter.drawLine(QPointF(*a), QPointF(*b))
                painter.setPen(self._pen(vcol, 2.0))
                m = _add(pc, hdir, k * half)
                a = adapter.project(_add(m, vdir, -half))
                b = adapter.project(_add(m, vdir, half))
                if a is not None and b is not None:
                    painter.drawLine(QPointF(*a), QPointF(*b))

    def _drawRotateArc(self, painter, adapter, scale, rotAxes) -> None:
        if 2 not in rotAxes:
            return
        c = self._center
        r = ARC_RADIUS_RATIO * scale
        start, end = self._arcAngles()
        color = self._hl("rot", 2, _AXIS_COLORS[2])
        painter.setPen(self._pen(color, 2.0))
        prev = None
        for i in range(ARC_SEGMENTS + 1):
            a = start + i / ARC_SEGMENTS * (end - start)
            s = adapter.project(_add(c, (math.cos(a), math.sin(a), 0.0), r))
            if s is not None and prev is not None:
                painter.drawLine(QPointF(*prev), QPointF(*s))
            prev = s

    def _drawScaleAxes(self, painter, adapter, axes) -> None:
        """缩放轴：负方向虚线 + 末端小方块（世界坐标长度，不随 gizmoScale 缩放）。"""
        c = self._center
        for i in axes:
            factor = self._curScale if (self._dragging and self._mode == "scale" and
                                        (self._uniform or self._activeAxis == i)) else 1.0
            L = self.scaleAxisLength(i, factor)
            off = L * SCALE_AXIS_OFFSET_RATIO
            color = self._hl("scale", i, _AXIS_COLORS[i])
            s0 = adapter.project(_add(c, _DIRS[i], -off))
            s1 = adapter.project(_add(c, _DIRS[i], -L))
            if s0 is None or s1 is None:
                continue
            painter.setPen(self._pen(color, 1.0, dash=True))
            painter.drawLine(QPointF(*s0), QPointF(*s1))
            painter.setPen(self._pen(color, 1.0))
            painter.setBrush(color)
            painter.drawRect(s1[0] - 4.0, s1[1] - 4.0, 8.0, 8.0)

    def _drawOriginMarker(self, painter, adapter) -> None:
        s = adapter.project(self._center)
        if s is None:
            return
        painter.setPen(self._pen(_HIGHLIGHT, 2.0))
        painter.setBrush(QColor(255, 255, 255))
        painter.drawEllipse(QPointF(*s), 4.0, 4.0)

    def _drawMovePreviewLine(self, painter, adapter) -> None:
        """拖动预览线：起点中心 → 当前中心（轴色，宽 3，与原版一致）。"""
        a = adapter.project(self._dragStartCenter)
        b = adapter.project(self._center)
        if a is None or b is None:
            return
        color = _AXIS_COLORS[self._activeAxis] if self._activeAxis >= 0 \
            else QColor(255, 255, 0)
        painter.setPen(self._pen(color, 3.0))
        painter.drawLine(QPointF(*a), QPointF(*b))

    def _drawRotateFeedback(self, painter, adapter, scale) -> None:
        """旋转反馈：扇形（半透明轴色）+ 角度文字；扇形起点用按下时的固定屏幕角。"""
        cs = adapter.project(self._center)
        if cs is None or abs(self._feedbackDeg) < 1e-6:
            return
        r = ARC_RADIUS_RATIO * scale * self._viewPxRatio(adapter, scale)
        # 扇形起点：按下时按下点的屏幕角；后续以投影中心屏幕角+累积角度展开，
        # 与当前屏幕方向（世界角投影）保持一致，透视下也贴合圆弧位置
        a0 = self._feedbackStart
        a1 = a0 + math.radians(self._feedbackDeg)
        poly = [QPointF(cs[0], cs[1])]
        n = max(2, int(abs(a1 - a0) / 0.02))
        for i in range(n + 1):
            a = a0 + (a1 - a0) * i / n
            poly.append(QPointF(cs[0] + r * math.cos(a), cs[1] - r * math.sin(a)))
        color = QColor(_AXIS_COLORS[2])
        color.setAlpha(60)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawPolygon(QPolygonF(poly))
        painter.setPen(self._pen(_AXIS_COLORS[2], 2.0))
        painter.setBrush(Qt.NoBrush)
        painter.drawLine(poly[1], poly[-1])
        text = f"{self._feedbackDeg:.1f}°"
        font = QFont()
        font.setPixelSize(13)
        painter.setFont(font)
        painter.setPen(self._pen(_HIGHLIGHT, 1.0))
        painter.drawText(QPointF(cs[0] + r + 8.0, cs[1] - 8.0), text)

    def _viewPxRatio(self, adapter, scale) -> float:
        """扇形屏幕半径：把世界半径换算成像素（正交按投影比例，透视近似同值）。"""
        c = self._center
        s0 = adapter.project(c)
        s1 = adapter.project(_add(c, (1.0, 0.0, 0.0), 1.0))
        if s0 is None or s1 is None:
            return 1.0
        px = math.hypot(s1[0] - s0[0], s1[1] - s0[1])
        return px if px > 1e-9 else 1.0

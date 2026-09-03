"""3D 视口：软渲染透视/正交投影，轨道相机，支持旋转翻面。"""
from __future__ import annotations

import math

import numpy as np

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QImage,
                           QKeyEvent, QMouseEvent, QPainter, QPainterPath, QPen,
                           QPolygonF, QSurfaceFormat, QTransform, QWheelEvent)
from PySide6.QtOpenGL import QOpenGLFunctions_3_3_Compatibility
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from core.vector.geometry_types import ObjectType, Point3D
from core.grid_manager import GridManager
import core.app_settings as app_settings
from core.snap.snap_manager import SnapManager
from core.snap.snap_types import SnapResult
from core.relief import mesh3d, shade, paint_palette
from core.relief.region_relief import sample_curve_points, build_inside_mask
from ui.styles import theme
from ui.widgets.paint_state import PaintState
from ui.view_2d.graphics_view import paintSnapMarker
from ui.view_3d.gl_surface import GLSurface, view_from_basis, perspective, ortho_proj
from ui.view_3d._renderdbg import dbg

# 申请桌面 OpenGL 3.3 兼容上下文：ModernGL 着色器(#version 330)可用，同时保证
# QPainter 叠加层在 OpenGL 上正常工作（Core Profile 下 QPainter 画笔引擎易出问题）。
_glFormat = QSurfaceFormat()
_glFormat.setVersion(3, 3)
_glFormat.setProfile(QSurfaceFormat.CompatibilityProfile)
_glFormat.setSamples(int(app_settings.msaa_samples))   # MSAA（设置页可调，改后重启生效）
QSurfaceFormat.setDefaultFormat(_glFormat)

GRID_RANGE = 90.0
GRID_STEP = 10.0
MAJOR_EVERY = 5
NEAR = 1.0
DEFAULT_DIST = 260.0
FOV_DEG = 45.0
PICK_RADIUS = 10.0      # 对象拾取屏幕半径（像素）
PITCH_LIMIT = math.radians(85.0)  # 俯仰限幅：避免近平顶视时 up 向量突变闪跳
# 投影屏幕坐标钳制上限：Qt6 光栅化引擎用 QFixed(16.16)，坐标超 ±~32768
# 会整数溢出→原生崩溃（放大时贴近相机的顶点 depth≈NEAR 会投出巨值）。
# 钳到 ±15000（远超任何真实屏幕尺寸，在屏几何不受影响；两端点差<32768）。
_COORD_CLAMP = 15000.0
# 浮雕 3D 曲面网格密度：自适应（按模型在屏幕上的像素尺寸取格），下限保底。
# 软件兜底路径用 100（逐三角形循环上限）；GPU 路径用更高密度（每次 draw call，
# 不逐面循环），曲面更细腻、旋转丝滑。密度跟随模型真实分辨率（新建模型“精度”
# 参数，默认 0.1mm），只在模型分辨率超出屏幕可显示像素时才降采样，避免“太粗糙”。
_RELIEF_MESH_MIN = 32
_RELIEF_MESH_MAX = 100   # 软件软光栅化逐三角形循环上限（Gouraud 下 100 已足够细腻）
_RELIEF_MESH_MAX_GL = 256  # GPU 路径：单次 draw call，256 仍轻松 60fps
# 浮雕编辑态模型显示透明度（X-Ray）：锁定模型后曲面半透明显示，参考曲线在
# 任何视角都透过模型清晰可见——否则实心曲面会淹没曲线，没法点选做区域浮雕/填色。
# 注意不能太透：叠加层（曲线）已通过"深度清除+置顶"保证可见，半透明只负责保留
# "曲线嵌在模型内部"的视觉层次；若透明度太低（如 0.55），每个像素都要和近白
# 背景(~#fafafa)混合 45%，模型会整体发灰、失去明暗对比。
_RELIEF_XRAY_OPACITY = 0.85

_AXIS_STYLE = {
    "X": (QColor("#e53935"), (1.0, 0.0, 0.0)),
    "Y": (QColor("#43a047"), (0.0, 1.0, 0.0)),
    "Z": (QColor("#1e88e5"), (0.0, 0.0, 1.0)),
}


def _norm(v):
    length = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    if length < 1e-12:
        return (0.0, 0.0, 0.0)
    return (v[0] / length, v[1] / length, v[2] / length)


def _clampCoord(v: float) -> float:
    """将单个屏幕坐标钳到安全范围并消除 inf/nan（防止 QPainter 定点溢出崩溃）。"""
    if not math.isfinite(v):
        return 0.0
    if v > _COORD_CLAMP:
        return _COORD_CLAMP
    if v < -_COORD_CLAMP:
        return -_COORD_CLAMP
    return v


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _lerp(a, b, t):
    return (a[0] + t * (b[0] - a[0]),
            a[1] + t * (b[1] - a[1]),
            a[2] + t * (b[2] - a[2]))


def _segIntersectsRect(p, q, r: QRectF) -> bool:
    """Liang-Barsky 线段裁剪：线段与矩形相交（含在内部）返回 True。
    与 2D 画布框选同规则。"""
    dx, dy = q.x() - p.x(), q.y() - p.y()
    t0, t1 = 0.0, 1.0
    for pp, qq in ((-dx, p.x() - r.left()), (dx, r.right() - p.x()),
                   (-dy, p.y() - r.top()), (dy, r.bottom() - p.y())):
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


class Viewport3D(QOpenGLWidget):
    """可旋转的三维视口（GPU 加速）。

    浮雕曲面用 ModernGL 在 GPU 上渲染（深度测试 + 片元 Phong 光照），2D 叠加层
    （对象/操作轴/选择框/曲面边框）仍用 QPainter 绘制在 GL 帧之上。旋转/翻面只更新
    MVP 矩阵 uniform，不重建网格，丝滑无卡顿。

    模式与 ViewSwitchBar 一致：
        0 = 透视（自由轨道旋转，可翻面）
        1 = 顶视（正交，XY 平面）
        2 = 前视（正交，XZ 平面）
        3 = 右视（正交，YZ 平面）
    """

    # 浮雕模式全局操作（空格填色等）的状态反馈，主窗口接入历史/状态栏
    statusMessage = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumSize(400, 300)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self._mode = 0

        self._yaw = math.radians(135.0)
        self._pitch = math.radians(35.0)
        self._perspYaw = self._yaw
        self._perspPitch = self._pitch
        self._dist = DEFAULT_DIST
        self._pan = (0.0, 0.0)
        self._drag = None  # (button, lastPos)
        self._doc = None
        self._cm = None
        self._gizmo = None
        self._gizmoAdapter = None
        self._curveEdit = None
        self._curveEditAdapter = None
        self._boxStart = None       # 左键潜在框选起点（屏幕坐标）；位移超阈值才成框
        self._boxActive = False
        self._boxRect = QRectF()
        self._boxLTR = True         # 左→右=包含选择，右→左=相交选择（同 2D）
        self._boxMods = Qt.KeyboardModifier.NoModifier
        # 浮雕模式锁定的模型（进入浮雕态 pin 住，渲染不依赖瞬时选中）+ 网格缓存
        self._reliefModel = None
        self._reliefMesh = None
        self._reliefMeshKey = None
        self._reliefMeshLodDim = None    # 构建当前 mesh 用的 LOD 长边上限（色号蒙版对齐）
        self._terrainLutCache = None
        self._hideReferenceGrid = True    # 默认隐藏参考地面/网格线，仅留浮雕曲面
        # GPU 渲染状态：GL 渲染器、网格/底色缓冲缓存 key、边框叠加所需投影
        self._gl = None
        self._glKey = None
        self._surfKey = None
        self._reliefProj = None
        # 逐顶点缓冲（位置/法线/底色/索引）按 surfKey 缓存：旋转时不变 → 不重算
        self._glCache = None
        self._glCacheKey = None
        c = QColor(theme.BG_VIEWPORT)
        self._bgFloat = (c.redF(), c.greenF(), c.blueF(), 1.0)
        # 捕捉（与 2D 画布同规则：命令取点先对象捕捉，未命中再网格吸附）
        self._grid = GridManager.instance()
        self._snap = SnapManager.instance()
        self._lastSnap = None
        self._cursorPos = QPointF()

    # ---------------- 文档 / 命令 ----------------
    def setDocument(self, doc) -> None:
        """绑定/换绑文档（多文件切换）：先按引用断开旧绑定（含同一文档重复
        绑定的场景，避免旧 lambda 泄漏、同一信号触发多份槽），再接新文档。"""
        if self._doc is not None:
            try:
                self._doc.objectAdded.disconnect(self._onDocAdded)
                self._doc.objectRemoved.disconnect(self._onDocRemoved)
                self._doc.objectChanged.disconnect(self._onDocChanged)
                self._doc.layerChanged.disconnect(self._onDocLayerChanged)
                self._doc.selectionChanged.disconnect(self._onDocSelChanged)
            except (RuntimeError, TypeError):
                pass
        if doc is None:
            self._doc = None
            return
        self._doc = doc
        # 槽保存为实例属性，保证换绑时可按引用精确断开
        self._onDocAdded = lambda *_: self.update()
        self._onDocRemoved = lambda *_: self.update()
        self._onDocChanged = lambda *_: self.update()
        self._onDocLayerChanged = lambda: self.update()
        self._onDocSelChanged = lambda: self.update()
        doc.objectAdded.connect(self._onDocAdded)
        doc.objectRemoved.connect(self._onDocRemoved)
        doc.objectChanged.connect(self._onDocChanged)
        doc.layerChanged.connect(self._onDocLayerChanged)
        doc.selectionChanged.connect(self._onDocSelChanged)
        self.update()

    def setHideReferenceGrid(self, hide: bool) -> None:
        """浮雕模式下隐藏参考地面/网格线（透明网格），仅显示浮雕曲面。"""
        if self._hideReferenceGrid != hide:
            self._hideReferenceGrid = hide
            self.update()

    def setCommandManager(self, cm) -> None:
        self._cm = cm
        if cm is not None:
            cm.activeCommandChanged.connect(self._onActiveCommandChanged)

    def _onActiveCommandChanged(self, name: str) -> None:
        """命令结束（name 为空）时清空捕捉标记，避免气泡残留。"""
        if not name:
            self._lastSnap = None
            self.update()

    def setGizmo(self, gizmo, adapter) -> None:
        self._gizmo = gizmo
        self._gizmoAdapter = adapter

    def setCurveEdit(self, ctrl, adapter) -> None:
        self._curveEdit = ctrl
        self._curveEditAdapter = adapter

    def setReliefModel(self, model) -> None:
        """锁定/解锁浮雕编辑的目标模型（进入浮雕模式时 pin 住，离开时传 None）。

        锁定后 3D 曲面渲染以该模型为准，不再依赖瞬时选中——避免视口左键
        清空选择导致网格消失。锁定即进入 X-Ray 半透明显示，解锁恢复不透明。
        """
        self._reliefModel = model
        self._reliefMeshKey = None      # 强制下次重建网格缓存
        self.update()

    def _reliefOpacity(self) -> float:
        """浮雕编辑态曲面透明度：锁定模型（浮雕模式）半透明显示。

        半透明让被曲面覆盖/位于曲面下方的参考曲线在任何视角都透过模型可见，
        便于点选做填色、区域浮雕等操作；普通 3D 预览仍为不透明实心曲面。
        """
        return _RELIEF_XRAY_OPACITY if self._reliefModel is not None else 1.0

    def _screenToXYPlane(self, mx: float, my: float):
        """屏幕坐标反投影到 XY 平面（z=0）。浮雕模式取点默认落在此平面。"""
        w, h = float(self.width()), float(self.height())
        if w < 2 or h < 2:
            return None
        cam = self._camera()
        eye, right, up, fwd, ortho = cam
        cx, cy = w / 2.0, h / 2.0
        if ortho:
            scale = self._orthoScale(w, h)
            if scale < 1e-12:
                return None
            x = (mx - cx) / scale
            if self._mode == 2:      # 前视：XY 平面侧看成线，Y 不可辨，取 0
                return Point3D(eye[0] + x, 0.0, 0.0)
            if self._mode == 3:      # 右视：屏幕横向对应世界 Y
                return Point3D(0.0, eye[1] + x, 0.0)
            return Point3D(eye[0] + x, eye[1] - (my - cy) / scale, 0.0)
        focal = self._focal(h)
        sx = (mx - cx) / focal
        sy = -(my - cy) / focal
        dx = sx * right[0] + sy * up[0] + fwd[0]
        dy = sx * right[1] + sy * up[1] + fwd[1]
        dz = sx * right[2] + sy * up[2] + fwd[2]
        if abs(dz) < 1e-12:
            return None
        t = -eye[2] / dz
        if t < 0:
            return None
        return Point3D(eye[0] + t * dx, eye[1] + t * dy, 0.0)

    # ---------------- 捕捉（与 2D 画布同规则） ----------------
    def _worldPerPixel(self, wp) -> float:
        """指定世界点处的每像素世界尺寸（正交恒定，透视随深度变化）。"""
        w, h = float(self.width()), float(self.height())
        eye, _, _, fwd, ortho = self._camera()
        if ortho:
            return 1.0 / max(self._orthoScale(w, h), 1e-12)
        depth = _dot(_sub((wp.x, wp.y, 0.0), eye), fwd)
        if depth < NEAR:
            depth = NEAR
        return depth / self._focal(h)

    def _snapAt(self, worldPos: Point3D) -> SnapResult:
        if self._doc is None:
            return SnapResult.invalid()
        ref = None
        previewIds: set = set()
        partialIds: set = set()
        excludedTypes: set = set()
        extraObjs: list = []
        inst = None
        if self._cm is not None and self._cm.hasActive():
            inst = self._cm.activeInstance()
            ref = inst.lastInputPoint()
            previewIds = inst.previewObjectIds()
            partialIds = inst.partialSnapObjectIds()
            excludedTypes = inst.excludedSnapTypesForPartial()
            extraObjs = inst.extraSnapObjects()
        objs = [o for o in self._doc.visibleObjects() if o.id() not in previewIds]
        objs.extend(extraObjs)
        worldR = self._snap.snapRadius() * self._worldPerPixel(worldPos)
        res = self._snap.snap(worldPos, objs, worldR, ref)
        if res.isValid and res.objectId in partialIds and \
                res.type in excludedTypes:
            return SnapResult.invalid()
        return res

    def _resolvePoint(self, pos) -> Point3D | None:
        """命令取点：先对象捕捉，未命中再按网格吸附（与 2D 画布一致）。"""
        wp = self._screenToXYPlane(pos.x(), pos.y())
        if wp is None:
            return None
        # 命令可声明不显示吸附气泡（如区域浮雕点击即起浮雕，无需精确吸附）：
        # 直接返回原世界点且不记录 _lastSnap，避免鼠标旁出现吸附气泡
        if self._cm is not None and self._cm.hasActive():
            inst = self._cm.activeInstance()
            if inst is not None and not inst.showSnapHints():
                self._lastSnap = None
                return wp
        res = self._snapAt(wp)
        if res.isValid:
            self._lastSnap = res
            return res.position
        self._lastSnap = None
        if self._grid.config().snapEnabled:
            gx, gy = self._grid.snapToGrid((wp.x, wp.y))
            return Point3D(gx, gy, 0.0)
        return wp

    # ---------------- 相机 ----------------
    def _camera(self):
        """返回 (eye, right, up, fwd, isOrtho)。"""
        px, py = self._pan
        if self._mode == 0:
            cp = math.cos(self._pitch)
            sp = math.sin(self._pitch)
            cy = math.cos(self._yaw)
            sy = math.sin(self._yaw)
            direction = (cp * sy, cp * cy, sp)
            eye = (px + self._dist * direction[0],
                   py + self._dist * direction[1],
                   self._dist * direction[2])
            target = (px, py, 0.0)
            fwd = _norm(_sub(target, eye))
            up = (0.0, 0.0, 1.0)
            if abs(fwd[2]) > 0.999:
                up = (0.0, 1.0, 0.0)
            right = _norm(_cross(fwd, up))
            up = _norm(_cross(right, fwd))
            return eye, right, up, fwd, False
        if self._mode == 1:      # 顶视：从 +Z 看
            return (px, py, self._dist), (1, 0, 0), (0, 1, 0), (0, 0, -1), True
        if self._mode == 2:      # 前视：从 +Y 看
            return (px, self._dist, py), (1, 0, 0), (0, 0, 1), (0, -1, 0), True
        return (self._dist, px, py), (0, 1, 0), (0, 0, 1), (-1, 0, 0), True

    def _orthoScale(self, w: float, h: float) -> float:
        return (min(w, h) * 0.45 / GRID_RANGE) * (DEFAULT_DIST / self._dist)

    def _focal(self, h: float) -> float:
        return (h / 2.0) / math.tan(math.radians(FOV_DEG) / 2.0)

    def _project(self, p, cam, w: float, h: float):
        eye, right, up, fwd, ortho = cam
        v = _sub(p, eye)
        depth = _dot(v, fwd)
        if not ortho and depth < NEAR:
            return None
        x = _dot(v, right)
        y = _dot(v, up)
        cx, cy = w / 2.0, h / 2.0
        if ortho:
            scale = self._orthoScale(w, h)
            return (_clampCoord(cx + x * scale), _clampCoord(cy - y * scale), depth)
        return (_clampCoord(cx + x / depth * self._focal(h)),
                _clampCoord(cy - y / depth * self._focal(h)), depth)

    def _projectSegment(self, p1, p2, cam, w: float, h: float):
        """投影线段，做近平面裁剪。返回两个屏幕点或 None。"""
        eye, right, up, fwd, ortho = cam
        v1 = _sub(p1, eye)
        v2 = _sub(p2, eye)
        d1 = _dot(v1, fwd)
        d2 = _dot(v2, fwd)
        if ortho:
            return (self._project(p1, cam, w, h)[:2],
                    self._project(p2, cam, w, h)[:2])
        if d1 < NEAR and d2 < NEAR:
            return None
        if d1 < NEAR:
            t = (NEAR - d1) / (d2 - d1)
            q = _lerp(p1, p2, t)
            a = self._project(q, cam, w, h)
            b = self._project(p2, cam, w, h)
            return None if a is None or b is None else (a[:2], b[:2])
        if d2 < NEAR:
            t = (NEAR - d2) / (d1 - d2)
            q = _lerp(p2, p1, t)
            a = self._project(p1, cam, w, h)
            b = self._project(q, cam, w, h)
            return None if a is None or b is None else (a[:2], b[:2])
        a = self._project(p1, cam, w, h)
        b = self._project(p2, cam, w, h)
        return None if a is None or b is None else (a[:2], b[:2])

    # ---------------- 绘制（GPU 主路径 + 软件兜底） ----------------
    def initializeGL(self) -> None:
        """准备 GPU 渲染器对象。

        注意：ModernGL 上下文延后到 paintGL 首次调用时创建——那时 QOpenGLWidget 已把
        视口自身的 FBO 绑定为“当前帧缓冲”，ModernGL 才能正确检测到它；若在 initializeGL
        里提前创建，会误记当时的默认缓冲，导致绘制到错误 FBO 而黑屏。
        """
        self._gl = GLSurface()

    def resizeGL(self, w: int, h: int) -> None:
        pass

    def paintGL(self) -> None:
        w, h = float(self.width()), float(self.height())
        dpr = self.devicePixelRatioF()
        W = max(1, int(round(w * dpr)))
        H = max(1, int(round(h * dpr)))
        # 取 Qt GL 上下文与默认 FBO：若上下文 id 变化或失效，说明 Qt 重建了上下文
        # ——ModernGL 缓存的对象会悬垂，下面据此丢弃并重建（避免 render 访问违例）。
        qctx = self.context()
        qid = id(qctx) if qctx is not None else None
        qvalid = bool(qctx.isValid()) if qctx is not None else False
        try:
            fbo = int(self.defaultFramebufferObject())
        except Exception:
            fbo = 0
        self._curFbo = fbo
        if self._gl is None:
            self._paintSoftware(w, h)
            return
        # 上下文无效：绝不进 GL（避免 vao.render 访问违例），直接软件兜底。
        if not qvalid:
            dbg("paintGL: Qt GL context INVALID -> software fallback")
            self._gl = None
            self._paintSoftware(w, h)
            return
        # Qt 上下文被重建（id 变化）：丢弃悬垂的 ModernGL 资源，重新创建。
        prev = getattr(self, "_glCtxId", None)
        if prev is not None and qid != prev:
            dbg(f"paintGL: Qt GL context CHANGED {prev}->{qid} -> recreate ModernGL")
            try:
                self._gl.release()
            except Exception:
                pass
            self._gl = GLSurface()
            self._reliefMesh = None
            self._reliefMeshKey = None
            self._glCache = None
            self._glCacheKey = None
        # 1) GPU 清屏 + 浮雕曲面：必须包在 Qt 的 beginNativePainting/endNativePainting
        #    之间——QOpenGLWidget 混用原生 GL(ModernGL) 与 QPainter 时，只有用这对括号
        #    隔离，Qt 画笔引擎才不会与 ModernGL 争用/污染 GL 状态；否则首帧 ModernGL
        #    画完后 Qt 引擎接管，次帧复用 VAO 时 vao.render() 会访问违例闪退。
        painter = QPainter(self)
        painter.beginNativePainting()
        try:
            self._gl.ensure()
            self._glCtxId = qid
            cam = self._camera()
            model = self._reliefTarget() if w >= 2 and h >= 2 else None
            if model is not None:
                self._drawReliefSurfaceGL(cam, w, h, W, H)
            else:
                self._gl.frame(W, H, self._bgFloat, fbo)
        except Exception:
            import traceback
            traceback.print_exc()
            dbg("paintGL: GL exception -> software fallback\n" + traceback.format_exc())
            try:
                painter.endNativePainting()
                painter.end()
            except Exception:
                pass
            self._gl = None
            self._paintSoftware(w, h)
            return
        painter.endNativePainting()
        # 2) QPainter 叠加层（对象/操作轴/命令预览/捕捉/曲面边框/选择框）
        painter.setRenderHint(QPainter.Antialiasing, True)
        if w >= 2 and h >= 2:
            if not self._hideReferenceGrid:
                self._drawGround(painter, cam, w, h)
                self._drawGrid(painter, cam, w, h)
                self._drawAxes(painter, cam, w, h)
            if self._doc is not None:
                self._drawObjects(painter, cam, w, h)
            self._drawReliefBorderNow(painter, cam, w, h)
            self._drawCommandPreview(painter, cam, w, h)
            self._paintSnapMarker(painter, cam, w, h)
            if self._curveEdit is not None and self._curveEditAdapter is not None:
                self._curveEdit.paint(painter, self._curveEditAdapter)
            if self._gizmo is not None and self._gizmoAdapter is not None:
                self._gizmo.paint(painter, self._gizmoAdapter)
            if self._boxActive and not self._boxRect.isNull():
                self._paintSelectBox(painter)
        painter.end()

    def _paintSoftware(self, w: float, h: float) -> None:
        """无 GPU 上下文时的软件渲染兜底（沿用原 QPainter 逐面/逐像素逻辑）。"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(theme.BG_VIEWPORT))
        if w >= 2 and h >= 2:
            cam = self._camera()
            if not self._hideReferenceGrid:
                self._drawGround(painter, cam, w, h)
                self._drawGrid(painter, cam, w, h)
                self._drawAxes(painter, cam, w, h)
            if self._doc is not None:
                self._drawReliefSurfaceSoftware(painter, cam, w, h)
                self._drawObjects(painter, cam, w, h)
            self._drawCommandPreview(painter, cam, w, h)
            self._paintSnapMarker(painter, cam, w, h)
            if self._curveEdit is not None and self._curveEditAdapter is not None:
                self._curveEdit.paint(painter, self._curveEditAdapter)
            if self._gizmo is not None and self._gizmoAdapter is not None:
                self._gizmo.paint(painter, self._gizmoAdapter)
            if self._boxActive and not self._boxRect.isNull():
                self._paintSelectBox(painter)
        painter.end()

    # ---------------- GPU 曲面渲染 ----------------
    def _drawReliefBorderNow(self, painter, cam, w: float, h: float) -> None:
        """GL 帧之上的曲面外轮廓叠加（边框环，选中高亮）。"""
        if self._reliefProj is None:
            return
        mesh, sx, sy, valid = self._reliefProj
        self._reliefProj = None
        mdl = self._reliefTarget()
        if mdl is None:
            return
        self._drawReliefBorder(painter, mesh, sx, sy, valid, mdl)

    def _drawReliefSurfaceGL(self, cam, w: float, h: float, W: int, H: int) -> None:
        """GPU 路径：上传（缓存）网格顶点/法线/底色，单次 draw call 渲染。

        旋转期间网格 key 不变 → 只重传 MVP/相机 uniform；显示模式/材质/模型版本变化
        才重建顶点缓冲。深度测试做遮挡，片元 Phong 光照，无网格纹、丝滑。
        """
        model = self._reliefTarget()
        if model is None:
            return
        mesh = self._reliefMeshFor(model, max_dim=self._gpuMeshDim())
        if mesh is None or mesh.faceCount == 0:
            return
        # surfKey 随 模型/显示模式/材质/颜色/色号蒙版 变化，不随相机(旋转)变化
        # → 旋转时不重算顶点数组；填色只让底色缓存失效重建（maskStamp），几何网格缓存不动
        surfKey = (self._reliefMeshKey, model.display3DMode(), model.material(),
                   tuple(model.color() or ()), model.maskStamp())
        if self._glCacheKey == surfKey:
            verts, vn, base, idx = self._glCache
        else:
            # 逐顶点底色（仅随模型/模式/材质变化）+ 逐顶点法线（面法线平均）
            base = self._baseColorsVert(mesh, model)
            vn = self._scatterFaceToVert(mesh, mesh.normals)
            nlen = np.linalg.norm(vn, axis=1, keepdims=True)
            vn = vn / np.maximum(nlen, 1e-12)
            # 四边形面 → 两个三角形索引
            faces = mesh.faces
            v00, v01, v11, v10 = faces[:, 0], faces[:, 1], faces[:, 2], faces[:, 3]
            F = faces.shape[0]
            idx = np.empty(F * 6, dtype=np.uint32)
            idx[0::6] = v00; idx[1::6] = v01; idx[2::6] = v11
            idx[3::6] = v00; idx[4::6] = v11; idx[5::6] = v10
            verts = mesh.verts
            self._glCache = (verts, vn, base, idx)
            self._glCacheKey = surfKey
        eye = cam[0]
        light = mesh3d.lightDirection(model.lightAzimuth(), model.lightAltitude())
        params = self._lightParams(model.display3DMode(), model.material())
        mvp = self._buildMVP(cam, w, h, W, H)
        self._gl.draw(surfKey, verts, vn, base, idx, mvp, eye, light, params,
                      W, H, self._bgFloat, getattr(self, "_curFbo", 0),
                      opacity=self._reliefOpacity())
        # 曲面已写入深度缓冲。QPainter 随后在同一默认 FBO 上绘制 2D 叠加层
        # （参考曲线/操作轴/捕捉标记），若沿用深度测试会被曲面深度剔除——
        # 顶视图下模型占满屏幕时尤其明显（曲线完全看不见）。这里把测试/混合关掉
        # 并只清深度缓冲，让叠加层始终置顶。
        # 注意：绝不能调 moderngl 的 ctx.clear(depth=...)：它的 clear() 默认同时把
        # 颜色清成 (0,0,0,0) 透明黑，会把刚渲染好的曲面一并抹掉导致整屏黑。
        _c = self._gl.ctx
        _c.disable(_c.DEPTH_TEST)
        _c.disable(_c.BLEND)
        try:
            _glf = QOpenGLFunctions_3_3_Compatibility(self.context())
            _glf.initializeOpenGLFunctions()
            _glf.glClear(0x00000100)   # GL_DEPTH_BUFFER_BIT：仅清深度，不碰颜色
        except Exception:
            pass   # 清深度失败只影响叠加层遮挡兜底，不影响曲面本身
        # 记录投影坐标，供边框叠加使用
        sx, sy, depth, valid = self._projectVerts(mesh.verts, cam, w, h)
        self._reliefProj = (mesh, sx, sy, valid)

    def _baseColorsVert(self, mesh, model) -> np.ndarray:
        """逐顶点底色（未打光，[0,1]）：map→地形 LUT、color→用户色、material→程序化底。"""
        V = mesh.verts.shape[0]
        mode = model.display3DMode()
        material = model.material()
        base_color = model.color()
        if mode == "map":
            lut = shade.make_lut("terrain")                 # (256,3) uint8
            vh = self._scatterFaceToVert(mesh, mesh.hnorm[:, None])[:, 0]
            vi = np.clip((vh * 255.0).astype(np.int32), 0, 255)
            return lut[vi].astype(np.float64) / 255.0
        if mode == "color":
            nodeRgb = self._colorMaskNodeRgb(mesh, model)
            if nodeRgb is not None:
                return nodeRgb / 255.0
            col = np.array(base_color if base_color else (21, 182, 201),
                           dtype=np.float64) / 255.0
            return np.tile(col, (V, 1))
        fb = mesh3d._materialBase(mesh, material, base_color).astype(np.float64) / 255.0
        return self._scatterFaceToVert(mesh, fb)

    def _colorMaskNodeRgb(self, mesh, model):
        """颜色块模式：逐节点底色 ``(V,3)`` float 0..255（已填色→调色板色、
        未填色→模型底色）。非 color 模式 / 无蒙版 / 形状不匹配返回 None。

        蒙版分辨率与构建 mesh 的 LOD 一致（``_reliefMeshLodDim``），故
        ``lodColorMask`` 的 (gny,gnx) 与行优先网格节点一一对齐，``ravel()`` 即 (V,)。
        """
        if model.display3DMode() != "color":
            return None
        dim = getattr(self, "_reliefMeshLodDim", None)
        if dim is None:
            return None
        cm = model.lodColorMask(dim)
        V = mesh.verts.shape[0]
        if cm is None or cm.size != V:
            return None
        pal = paint_palette.PALETTE
        n = len(pal)
        base_rgb = model.color() or (21, 182, 201)
        lut = np.empty((n + 1, 3), dtype=np.float64)
        lut[0] = base_rgb                       # EMPTY(0) → 模型底色
        for i, c in enumerate(pal):
            lut[i + 1] = c                      # 值 v → PALETTE[v-1]
        vals = np.clip(cm.ravel().astype(np.int32), 0, n)
        return lut[vals]                        # (V,3) 0..255

    def _scatterFaceToVert(self, mesh, faceArr: np.ndarray) -> np.ndarray:
        """把逐面数组按顶点索引平均到共享顶点（Gouraud 顶点色/法线用）。"""
        V = mesh.verts.shape[0]
        C = faceArr.shape[1]
        out = np.zeros((V, C), dtype=np.float64)
        fidx = mesh.faces.ravel()
        np.add.at(out, fidx, faceArr.repeat(4, axis=0))
        cnt = np.zeros(V, dtype=np.float64)
        np.add.at(cnt, fidx, 1.0)
        return out / np.maximum(cnt, 1.0)[:, None]

    def _lightParams(self, mode: str, material: str) -> dict:
        """着色器光照系数（与 mesh3d.colorFaces 一一对应）：ambient + 漫反射/高光。"""
        amb = mesh3d._AMBIENT
        if mode == "map":
            # 地图/高度色：更平的光照(高环境光)+弱高光，保色带鲜艳清晰（精雕式）
            return dict(mode=0, ambient=mesh3d._AMBIENT_MAP, kd=0.0, add_amb=0.0,
                        spec_gain=26.0 / 255.0, spec_pow=32.0)
        if mode == "color":
            return dict(mode=1, ambient=amb, kd=0.0, add_amb=0.0,
                        spec_gain=50.0 / 255.0, spec_pow=32.0)
        if material == "plastic":
            return dict(mode=5, ambient=amb, kd=0.58, add_amb=0.42,
                        spec_gain=150.0 / 255.0, spec_pow=60.0)
        if material == "metal":
            return dict(mode=6, ambient=amb, kd=0.45, add_amb=0.25,
                        spec_gain=210.0 / 255.0, spec_pow=80.0)
        if material == "wood":
            return dict(mode=3, ambient=amb, kd=0.0, add_amb=0.0,
                        spec_gain=26.0 / 255.0, spec_pow=16.0)
        if material == "stone":
            return dict(mode=4, ambient=amb, kd=0.0, add_amb=0.0,
                        spec_gain=18.0 / 255.0, spec_pow=20.0)
        return dict(mode=2, ambient=amb, kd=0.0, add_amb=0.0,   # clay
                    spec_gain=70.0 / 255.0, spec_pow=30.0)

    def _buildMVP(self, cam, w: float, h: float, W: int, H: int) -> np.ndarray:
        """构造 world→clip 的 MVP（标准数学约定，行主序），与 _project 投影一致。"""
        eye, right, up, fwd, ortho = cam
        eye = np.asarray(eye, dtype=np.float64)
        view = view_from_basis(eye, right, up, fwd)
        if ortho:
            scale = self._orthoScale(w, h)
            proj = ortho_proj(scale, w, h, 0.1, 4000.0)
        else:
            fovy = math.radians(FOV_DEG)
            aspect = (W / H) if H > 0 else 1.0
            proj = perspective(fovy, aspect, NEAR, 4000.0)
        return proj @ view

    def _gridAxes(self):
        """返回网格平面的 (uAxis, vAxis) 索引，根据当前视图模式。"""
        if self._mode == 2:      # 前视：XZ 平面
            return 0, 2
        if self._mode == 3:      # 右视：YZ 平面
            return 1, 2
        return 0, 1              # 透视/顶视：XY 平面

    def _gridPoint(self, u, v, axes):
        p = [0.0, 0.0, 0.0]
        p[axes[0]] = u
        p[axes[1]] = v
        return (p[0], p[1], p[2])

    def _drawGround(self, painter, cam, w: float, h: float) -> None:
        axes = self._gridAxes()
        corners = [self._gridPoint(-GRID_RANGE, -GRID_RANGE, axes),
                   self._gridPoint(GRID_RANGE, -GRID_RANGE, axes),
                   self._gridPoint(GRID_RANGE, GRID_RANGE, axes),
                   self._gridPoint(-GRID_RANGE, GRID_RANGE, axes)]
        pts = []
        for c in corners:
            p = self._project(c, cam, w, h)
            if p is None:
                return
            pts.append(QPointF(p[0], p[1]))
        painter.setBrush(QColor(230, 240, 245, 60))
        painter.setPen(Qt.NoPen)
        painter.drawPolygon(QPolygonF(pts))

    def _drawGrid(self, painter, cam, w: float, h: float) -> None:
        axes = self._gridAxes()
        major = QPen(QColor(theme.GRID_MAJOR), 1.0)
        major.setCosmetic(True)
        minor = QPen(QColor(theme.GRID_MINOR), 0)
        minor.setCosmetic(True)
        n = int(GRID_RANGE / GRID_STEP)
        for i in range(-n, n + 1):
            x = i * GRID_STEP
            pen = major if i % MAJOR_EVERY == 0 else minor
            painter.setPen(pen)
            seg = self._projectSegment(self._gridPoint(x, -GRID_RANGE, axes),
                                       self._gridPoint(x, GRID_RANGE, axes), cam, w, h)
            if seg is not None:
                painter.drawLine(QPointF(*seg[0]), QPointF(*seg[1]))
            seg = self._projectSegment(self._gridPoint(-GRID_RANGE, x, axes),
                                       self._gridPoint(GRID_RANGE, x, axes), cam, w, h)
            if seg is not None:
                painter.drawLine(QPointF(*seg[0]), QPointF(*seg[1]))

    def _drawAxes(self, painter, cam, w: float, h: float) -> None:
        length = GRID_RANGE
        for name, (color, direction) in _AXIS_STYLE.items():
            if name == "Z" and self._mode == 0:
                continue
            end = (direction[0] * length,
                   direction[1] * length,
                   direction[2] * length)
            pen = QPen(color, 2.0)
            pen.setCosmetic(True)
            painter.setPen(pen)
            seg = self._projectSegment((0.0, 0.0, 0.0), end, cam, w, h)
            if seg is None:
                continue
            painter.drawLine(QPointF(*seg[0]), QPointF(*seg[1]))

        # 原点
        p = self._project((0.0, 0.0, 0.0), cam, w, h)
        if p is not None:
            painter.setBrush(QColor(theme.PRIMARY))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QPointF(p[0], p[1]), 3.0, 3.0)

    # ---------------- 文档对象与命令预览 ----------------
    def _terrainLut(self):
        """缓存地图模式的地形 LUT（避免逐帧重建）。"""
        if self._terrainLutCache is None:
            self._terrainLutCache = shade.make_lut("terrain")
        return self._terrainLutCache

    def _reliefTarget(self):
        """当前应渲染的浮雕模型：浮雕模式锁定者优先，否则活动（选中）模型。

        模型所在图层被隐藏时返回 None（灯泡关掉后 3D 视口也不显示该模型）。
        """
        model = self._reliefModel
        if model is None and self._doc is not None:
            model = self._doc.activeReliefModel()
        if model is None:
            return None
        if self._doc is not None:
            layer = self._doc.layer(model.layerId())
            if layer is not None and not layer.visible:
                return None
        return model

    def _reliefMeshFor(self, model, max_dim: int = None):
        """按 (版本, 放置, 垂直夸张, 固定网格密度) 缓存 3D 面网格。

        显示网格 LOD 与相机/缩放无关（专业做法）：缩放只改 MVP 矩阵，几何不重建
        →不重传 GPU 缓冲（避免每次缩放重建显存导致的卡顿/段错误）。密度只随模型
        真实分辨率与 ``max_dim``（设置页：GPU/软件各一档）变化。
        """
        if max_dim is None:
            max_dim = _RELIEF_MESH_MAX
        dim = max(_RELIEF_MESH_MIN,
                  min(max_dim,
                      max(model.heightField().nx, model.heightField().ny)))
        dim = min(dim, max_dim)
        self._reliefMeshLodDim = dim     # 记录本次网格 LOD，供色号蒙版同分辨率对齐
        key = (id(model), model.version(), model.placement().tobytes(),
               round(float(model.zExaggeration()), 5), dim)
        if self._reliefMeshKey == key and self._reliefMesh is not None:
            return self._reliefMesh
        mesh = mesh3d.buildMesh(model.heightField(), model.placement(),
                                model.zExaggeration(), dim)
        self._reliefMesh = mesh
        self._reliefMeshKey = key
        return mesh

    def _gpuMeshDim(self) -> int:
        """GPU 路径显示网格密度上限（设置页可调，默认 256）。"""
        return int(app_settings.mesh_max_dim_gpu)

    def _cpuMeshDim(self) -> int:
        """软件兜底路径显示网格密度上限（设置页可调，默认 100）。"""
        return int(app_settings.mesh_max_dim_cpu)

    def applyRenderSettings(self) -> None:
        """设置页“渲染性能”变更后调用：重置网格缓存并按 GPU 开关切换渲染路径。

        MSAA 在启动时写入默认 QSurfaceFormat，需重启生效；此处只处理可即时生效的项。
        """
        # 网格密度可能变了 → 丢弃旧 mesh/GL 缓存，下次绘制按新设置重建。
        self._reliefMesh = None
        self._reliefMeshKey = None
        self._glCache = None
        self._glCacheKey = None
        if app_settings.gpu_accelerated:
            if self._gl is None:
                self._gl = GLSurface()   # 下一次 paintGL 的 ensure() 会在正确 FBO 下建上下文
        else:
            if self._gl is not None:
                try:
                    self._gl.release()
                except Exception:
                    pass
            self._gl = None
        self.update()

    def _projectVerts(self, verts, cam, w, h):
        """向量化投影 (V,3) 世界点 → (sx, sy, depth, valid)。"""
        eye, right, up, fwd, ortho = cam
        v = verts - np.asarray(eye, dtype=np.float64)
        depth = v @ np.asarray(fwd, dtype=np.float64)
        x = v @ np.asarray(right, dtype=np.float64)
        y = v @ np.asarray(up, dtype=np.float64)
        cx, cy = w / 2.0, h / 2.0
        if ortho:
            scale = self._orthoScale(w, h)
            sx = np.clip(cx + x * scale, -_COORD_CLAMP, _COORD_CLAMP)
            sy = np.clip(cy - y * scale, -_COORD_CLAMP, _COORD_CLAMP)
            return (sx, sy, depth,
                    np.ones(depth.shape, dtype=bool))
        focal = self._focal(h)
        with np.errstate(divide="ignore", invalid="ignore"):
            sx = cx + x / depth * focal
            sy = cy - y / depth * focal
        # 消除 inf/nan 并钳到安全范围（防 QPainter 定点溢出崩溃）
        sx = np.clip(np.nan_to_num(sx, nan=0.0, posinf=_COORD_CLAMP,
                                   neginf=-_COORD_CLAMP),
                     -_COORD_CLAMP, _COORD_CLAMP)
        sy = np.clip(np.nan_to_num(sy, nan=0.0, posinf=_COORD_CLAMP,
                                   neginf=-_COORD_CLAMP),
                     -_COORD_CLAMP, _COORD_CLAMP)
        return sx, sy, depth, depth >= NEAR

    def _drawReliefSurfaceSoftware(self, painter, cam, w: float, h: float) -> None:
        """软件兜底渲染：真实高度位移网格 + z-buffer Gouraud 平滑着色（GPU 不可用时）。

        不再逐面平涂——那会让相邻面颜色差一步、整片呈现“网格/格子”纹，且抗锯齿接缝
        进一步加重。改为逐像素插值顶点颜色的光栅化：曲面连续、无网格纹、旋转真实“浮起”。
        密度取固定档（见 :meth:`_reliefMeshFor`），跟随模型真实分辨率（新建模型“精度”
        参数，默认 0.1mm）。光栅化失败则退回逐面平涂兜底，保证模型始终可见。
        """
        model = self._reliefTarget()
        if model is None:
            return
        mesh = self._reliefMeshFor(model, max_dim=self._cpuMeshDim())
        if mesh is None or mesh.faceCount == 0:
            return

        out, sx, sy, valid = self._rasterizeRelief(mesh, model, cam, w, h)
        if out is None or not bool(out[..., 3].any()):
            # 光栅化未产出像素：退回逐面平涂（虽有余纹，但保证模型可见）
            self._drawReliefSurfaceFlat(painter, mesh, model, cam, w, h)
            self._drawReliefBorder(painter, mesh, sx, sy, valid, model)
            return

        H, W = out.shape[0], out.shape[1]
        qimg = QImage(out.tobytes(), W, H, W * 4, QImage.Format_RGBA8888).copy()
        # 浮雕编辑态半透明显示（与 GPU 路径一致的 X-Ray）
        painter.save()
        painter.setOpacity(self._reliefOpacity())
        painter.drawImage(0, 0, qimg)
        painter.restore()
        self._drawReliefBorder(painter, mesh, sx, sy, valid, model)

    def _rasterizeRelief(self, mesh, model, cam, w, h):
        """z-buffer Gouraud 光栅化：返回 ``(RGBA ndarray, sx, sy, valid)``。

        逐顶点颜色由“逐面色平均到共享顶点”得到（Gouraud）：相邻面共享顶点→颜色连续，
        面间台阶/网格纹消失；逐像素做透视正确的重心插值 + 深度测试，遮挡正确。
        """
        verts = mesh.verts
        V = verts.shape[0]
        sx, sy, depth, valid = self._projectVerts(verts, cam, w, h)

        # ---- 逐顶点颜色（Gouraud）----
        light = mesh3d.lightDirection(model.lightAzimuth(), model.lightAltitude())
        nodeRgb = self._colorMaskNodeRgb(mesh, model)
        faceBase = nodeRgb[mesh.faces].mean(axis=1) if nodeRgb is not None else None
        fc = mesh3d.colorFaces(mesh, model.display3DMode(), model.material(),
                               model.color(), light, cam[0], self._terrainLut(),
                               face_base=faceBase)
        fc = fc.astype(np.float64)
        fidx = mesh.faces.ravel()                               # (F*4,) 顶点索引（散布目标）
        vsum = np.zeros((V, 3))
        np.add.at(vsum, fidx, fc.repeat(4, axis=0))             # 每面颜色重复 4 份，对应 4 个顶点
        vcnt = np.zeros(V)
        np.add.at(vcnt, fidx, 1.0)
        vcol = vsum / np.maximum(vcnt, 1.0)[:, None]            # (V,3) 0..255

        W = max(1, int(round(w))); H = max(1, int(round(h)))
        Wm1, Hm1 = W - 1, H - 1
        zbuf = np.full((H, W), np.inf, dtype=np.float64)
        out = np.zeros((H, W, 4), dtype=np.uint8)               # 未覆盖处 alpha=0（透明）

        # 背面剔除
        eye = np.asarray(cam[0], dtype=np.float64)
        toEye = eye[None, :] - mesh.centers
        facing = (mesh.normals[:, 0] * toEye[:, 0]
                  + mesh.normals[:, 1] * toEye[:, 1]
                  + mesh.normals[:, 2] * toEye[:, 2]) > 0.0

        faces = mesh.faces
        fxs = sx[faces]; fys = sy[faces]; fzs = depth[faces]; fvc = valid[faces]
        F = faces.shape[0]
        for fi in range(F):
            if not (fvc[fi, 0] and fvc[fi, 1] and fvc[fi, 2] and fvc[fi, 3] and facing[fi]):
                continue
            self._rasterTri(out, zbuf, fxs[fi], fys[fi], fzs[fi], vcol, faces[fi], Wm1, Hm1)
            self._rasterTri(out, zbuf, fxs[fi, (0, 2, 3)], fys[fi, (0, 2, 3)],
                            fzs[fi, (0, 2, 3)], vcol, faces[fi, (0, 2, 3)], Wm1, Hm1)
        return out, sx, sy, valid

    def _rasterTri(self, out, zbuf, xs, ys, zs, vcol, vi, Wm1, Hm1) -> None:
        """单三角形光栅化：重心插值颜色 + 深度测试写入 out/zbuf。"""
        x0, x1, x2 = float(xs[0]), float(xs[1]), float(xs[2])
        y0, y1, y2 = float(ys[0]), float(ys[1]), float(ys[2])
        z0, z1, z2 = float(zs[0]), float(zs[1]), float(zs[2])
        minx = int(math.floor(min(x0, x1, x2)))
        maxx = int(math.ceil(max(x0, x1, x2)))
        miny = int(math.floor(min(y0, y1, y2)))
        maxy = int(math.ceil(max(y0, y1, y2)))
        if minx < 0: minx = 0
        if miny < 0: miny = 0
        if maxx > Wm1: maxx = Wm1
        if maxy > Hm1: maxy = Hm1
        if maxx < minx or maxy < miny:
            return
        px = np.arange(minx, maxx + 1)
        py = np.arange(miny, maxy + 1)
        PX, PY = np.meshgrid(px, py)
        denom = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(denom) < 1e-9:
            return
        w0 = ((y1 - y2) * (PX - x2) + (x2 - x1) * (PY - y2)) / denom
        w1 = ((y2 - y0) * (PX - x2) + (x0 - x2) * (PY - y2)) / denom
        w2 = 1.0 - w0 - w1
        mask = (w0 >= -1e-6) & (w1 >= -1e-6) & (w2 >= -1e-6)
        if not bool(mask.any()):
            return
        zz = w0 * z0 + w1 * z1 + w2 * z2
        yy = PY[mask]; xx = PX[mask]; zv = zz[mask]
        cur = zbuf[yy, xx]
        upd = zv < cur
        if not bool(upd.any()):
            return
        yy = yy[upd]; xx = xx[upd]
        w0m = w0[mask][upd]; w1m = w1[mask][upd]; w2m = w2[mask][upd]
        c0 = vcol[vi[0]]; c1 = vcol[vi[1]]; c2 = vcol[vi[2]]
        out[yy, xx, 0] = (w0m * c0[0] + w1m * c1[0] + w2m * c2[0]).clip(0, 255).astype(np.uint8)
        out[yy, xx, 1] = (w0m * c0[1] + w1m * c1[1] + w2m * c2[1]).clip(0, 255).astype(np.uint8)
        out[yy, xx, 2] = (w0m * c0[2] + w1m * c1[2] + w2m * c2[2]).clip(0, 255).astype(np.uint8)
        out[yy, xx, 3] = 255

    def _drawReliefSurfaceFlat(self, painter, mesh, model, cam, w: float, h: float) -> None:
        """兜底：逐面平涂（保留原逻辑，光栅化失败时使用）。"""
        sx, sy, _, valid = self._projectVerts(mesh.verts, cam, w, h)
        faces = mesh.faces
        faceValid = valid[faces].all(axis=1)
        if not bool(faceValid.any()):
            return
        light = mesh3d.lightDirection(model.lightAzimuth(), model.lightAltitude())
        nodeRgb = self._colorMaskNodeRgb(mesh, model)
        faceBase = nodeRgb[mesh.faces].mean(axis=1) if nodeRgb is not None else None
        colors = mesh3d.colorFaces(mesh, model.display3DMode(), model.material(),
                                   model.color(), light, cam[0], self._terrainLut(),
                                   face_base=faceBase)
        eye = np.asarray(cam[0], dtype=np.float64)
        toEye = eye[None, :] - mesh.centers
        facing = (mesh.normals[:, 0] * toEye[:, 0]
                  + mesh.normals[:, 1] * toEye[:, 1]
                  + mesh.normals[:, 2] * toEye[:, 2]) > 0.0
        dist2 = (toEye[:, 0] ** 2 + toEye[:, 1] ** 2 + toEye[:, 2] ** 2)
        order = np.argsort(dist2)[::-1]
        fx = sx[faces]; fy = sy[faces]
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setOpacity(self._reliefOpacity())   # 浮雕编辑态半透明（X-Ray）
        for f in order:
            fi = int(f)
            if not (faceValid[fi] and facing[fi]):
                continue
            col = QColor(int(colors[fi, 0]), int(colors[fi, 1]), int(colors[fi, 2]))
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(col))
            painter.drawPolygon(QPolygonF([
                QPointF(float(fx[fi, 0]), float(fy[fi, 0])),
                QPointF(float(fx[fi, 1]), float(fy[fi, 1])),
                QPointF(float(fx[fi, 2]), float(fy[fi, 2])),
                QPointF(float(fx[fi, 3]), float(fy[fi, 3])),
            ]))
        painter.restore()

    def _drawReliefBorder(self, painter, mesh, sx, sy, valid, model) -> None:
        """曲面外轮廓（网格边界环）：选中橙色加粗，否则对象色，强化范围可读性。"""
        gnx, gny = mesh.gnx, mesh.gny
        top = np.arange(gnx)
        bottom = (gny - 1) * gnx + np.arange(gnx)
        left = np.arange(gny) * gnx
        right = np.arange(gny) * gnx + (gnx - 1)
        ring = np.concatenate([top, right[1:], bottom[::-1][1:], left[::-1][1:]])
        ring = ring[valid[ring]]
        if ring.size < 3:
            return
        pts = [QPointF(float(sx[i]), float(sy[i])) for i in ring]
        if model.isSelected():
            col = QColor(theme.selectionColorFor(model))
            width = 2.5
        else:
            col = QColor(*model.color())
            width = 1.5
        bpen = QPen(col, width)
        bpen.setCosmetic(True)
        painter.setPen(bpen)
        painter.setBrush(Qt.NoBrush)
        painter.drawPolygon(QPolygonF(pts))

    def _drawObjects(self, painter, cam, w: float, h: float) -> None:
        """把文档对象投影绘制到 3D 视口。

        线宽/颜色与 2D 画布（base_item.BaseGraphicsItem）完全一致：普通态＝对象本色
        + lineWidth()；选中态＝高亮色 + max(宽+1.5, 3.0)。浮雕模式下也不额外加粗、
        不加描边——曲线可见性由“深度清除后置顶绘制”保证，无需特殊强调。
        """

        def stroke_width(obj, selected) -> float:
            w = obj.lineWidth()
            if obj.type() == ObjectType.POINT:
                w = max(w, 1.0) + 0.5      # 同 2D：点描边稍加粗保证可见
            if selected:
                w = max(w + 1.5, 3.0)      # 同 2D：选中加粗到至少 3px
            return w

        def draw_lines(lines, pen):
            painter.setPen(pen)
            for x1, y1, x2, y2 in lines:
                painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))

        for obj in self._doc.visibleObjects():
            selected = obj.isSelected()
            hl = theme.selectionColorFor(obj) if selected else None
            col = QColor(hl) if hl else QColor(*obj.color())
            pen = QPen(col, stroke_width(obj, selected))
            pen.setCosmetic(True)
            if obj.type() == ObjectType.POINT:
                pos = obj.position()
                p = self._project((pos.x, pos.y, pos.z), cam, w, h)
                if p is None:
                    continue
                painter.setPen(pen)
                painter.setBrush(QColor(Qt.GlobalColor.white))
                painter.drawEllipse(QPointF(p[0], p[1]), 3.0, 3.0)
                continue
            if obj.type() == ObjectType.POLYCURVE:
                lines = []
                for seg in obj.segments():
                    spts = seg.get("points", [])
                    if len(spts) < 2:
                        continue
                    for i in range(len(spts) - 1):
                        a = self._projectSegment(
                            (spts[i][0], spts[i][1], spts[i][2]),
                            (spts[i + 1][0], spts[i + 1][1], spts[i + 1][2]),
                            cam, w, h)
                        if a is not None:
                            lines.append((a[0][0], a[0][1], a[1][0], a[1][1]))
                draw_lines(lines, pen)
                continue
            if obj.type() == ObjectType.DIMENSION:
                # 标注在所有 3D 视口（含浮雕模式）与曲线同层可见
                self._drawDimension3D(painter, obj, col, pen, cam, w, h)
                continue
            pts = self._objectPolyline(obj)
            if len(pts) < 2:
                continue
            closed = False
            isClosed = getattr(obj, "isClosed", None)
            if callable(isClosed):
                closed = bool(isClosed())
            n = len(pts)
            segN = n if closed else n - 1
            lines = []
            for i in range(segN):
                seg = self._projectSegment(pts[i], pts[(i + 1) % n], cam, w, h)
                if seg is not None:
                    lines.append((seg[0][0], seg[0][1], seg[1][0], seg[1][1]))
            draw_lines(lines, pen)

    def _drawDimension3D(self, painter, obj, col, pen, cam, w: float, h: float) -> None:
        """尺寸标注在 3D 视口的投影绘制：尺寸线/延伸线/箭头与读数文字。

        标注本身位于 XY 平面（z=0）。尺寸线与箭头直接投影绘制；读数文字在
        屏幕空间绘制：字号按中心点处的世界→像素比例换算，方向取文本右向
        向量投影后的屏幕角度，斜视角下文字仍横平竖直可读。
        """
        lay = obj.layout()

        # ---- 尺寸线 / 延伸线 ----
        segs = []
        for x1, y1, x2, y2 in lay["lines"]:
            a = self._projectSegment((x1, y1, 0.0), (x2, y2, 0.0), cam, w, h)
            if a is not None:
                segs.append((a[0][0], a[0][1], a[1][0], a[1][1]))
        painter.setPen(pen)
        for x1, y1, x2, y2 in segs:
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))

        # ---- 箭头三角（实心） ----
        if lay["fills"]:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(col)
            for tri in lay["fills"]:
                pts = []
                ok = True
                for px, py in tri:
                    pr = self._project((px, py, 0.0), cam, w, h)
                    if pr is None:
                        ok = False
                        break
                    pts.append(QPointF(pr[0], pr[1]))
                if ok and len(pts) == 3:
                    painter.drawPolygon(QPolygonF(pts))

        # ---- 读数文字（屏幕空间矢量字形，随投影缩放） ----
        texts = lay["texts"]
        fs = obj.fontHeight()
        if not texts or fs <= 1e-9:
            return
        _K = 64.0                       # 字形放大倍数（与 2D 图元一致保证小字清晰）
        font = QFont(app_settings.dim_font or "Microsoft YaHei")
        for t in texts:
            pc = self._project((t["cx"], t["cy"], 0.0), cam, w, h)
            if pc is None:
                continue
            a = math.radians(t["angle"])
            pr = self._project((t["cx"] + math.cos(a),
                                t["cy"] + math.sin(a), 0.0), cam, w, h)
            if pr is None:
                continue
            unit = math.hypot(pr[0] - pc[0], pr[1] - pc[1])  # 1 世界单位 ≈ 多少像素
            fsPx = fs * unit
            if fsPx < 2.0:
                continue            # 太小（侧视角）省略，避免糊成一团
            scrAng = math.degrees(math.atan2(pr[1] - pc[1], pr[0] - pc[0]))
            font.setPixelSize(max(1, int(round(fsPx * _K))))
            fm = QFontMetricsF(font)
            path = QPainterPath()
            tw = fm.horizontalAdvance(t["text"])
            th = fm.height()
            asc = fm.ascent()
            path.addText(-tw * 0.5, asc - th * 0.5, font, t["text"])
            painter.save()
            painter.translate(pc[0], pc[1])
            painter.rotate(scrAng)
            painter.scale(1.0 / _K, 1.0 / _K)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(col)
            painter.drawPath(path)
            painter.restore()

    @staticmethod
    def _objectPolyline(obj):
        """取对象的三维折线采样点（圆/弧/椭圆按角度采样）。"""
        t = obj.type()
        if t == ObjectType.LINE:
            return [(p.x, p.y, p.z) for p in obj.points()]
        if t == ObjectType.CIRCLE:
            c, r = obj.center(), obj.radius()
            return [(c.x + r * math.cos(a), c.y + r * math.sin(a), c.z)
                    for a in (i * 2 * math.pi / 48 for i in range(49))]
        if t == ObjectType.ARC:
            c, r = obj.center(), obj.radius()
            sa, ea = obj.startAngle(), obj.endAngle()
            span = ea - sa
            if abs(span) < 1e-9:
                span = 2 * math.pi   # 起止角重合视为整圆；负值为顺时针扫掠
            return [(c.x + r * math.cos(sa + span * i / 48),
                     c.y + r * math.sin(sa + span * i / 48), c.z)
                    for i in range(49)]
        if t == ObjectType.ELLIPSE:
            c, a, b = obj.center(), obj.majorAxis(), obj.minorAxis()
            cr, sr = math.cos(obj.rotation()), math.sin(obj.rotation())
            out = []
            for i in range(49):
                th = i * 2 * math.pi / 48
                ex, ey = a * math.cos(th), b * math.sin(th)
                out.append((c.x + ex * cr - ey * sr,
                            c.y + ex * sr + ey * cr, c.z))
            return out
        if t in (ObjectType.CURVE, ObjectType.NURBS_CURVE):
            return [(p.x, p.y, p.z) for p in obj.getSampledPoints()]
        return []

    def _drawCommandPreview(self, painter, cam, w: float, h: float) -> None:
        """叠加绘制活动命令的预览（与 2D 预览同风格：青色虚线）。"""
        if self._cm is None:
            return
        inst = self._cm.activeInstance()
        if inst is None:
            return
        path = inst.previewPath()
        if path is not None:
            pen = QPen(QColor("#15b6c9"), 1.0, Qt.DashLine)
            pen.setCosmetic(True)
            painter.setPen(pen)
            # 拍平为折线，支持预览中的圆弧等曲线元素（arcTo）
            for poly in path.toSubpathPolygons():
                if len(poly) < 2:
                    continue
                prev = (poly[0].x(), poly[0].y(), 0.0)
                for i in range(1, len(poly)):
                    q = poly[i]
                    cur = (q.x(), q.y(), 0.0)
                    seg = self._projectSegment(prev, cur, cam, w, h)
                    if seg is not None:
                        painter.drawLine(QPointF(*seg[0]), QPointF(*seg[1]))
                    prev = cur
        # 光标预览点：投影后以固定屏幕尺寸白底实心圆绘制（与 2D 一致）
        mk = inst.previewMarker()
        if mk is not None:
            pr = self._project((mk[0], mk[1], 0.0), cam, w, h)
            if pr is not None:
                from ui.view_2d.item_factory import POINT_MARKER_PX
                painter.setPen(QPen(QColor("#15b6c9"), 1.5))
                painter.setBrush(QColor(Qt.GlobalColor.white))
                painter.drawEllipse(QPointF(pr[0], pr[1]),
                                    POINT_MARKER_PX, POINT_MARKER_PX)

    def _paintSnapMarker(self, painter, cam, w: float, h: float) -> None:
        """捕捉标记：世界点投影到屏幕后调用共用的 paintSnapMarker（同 2D）。"""
        snap = self._lastSnap
        if snap is None or not snap.isValid:
            return
        mp = snap.effectiveMarker
        p = self._project((mp.x, mp.y, 0.0), cam, w, h)
        if p is None:
            return
        paintSnapMarker(painter, snap, QPointF(p[0], p[1]), self._cursorPos)

    # ---------------- 交互 ----------------
    def _pixelToWorld(self, dx: float, dy: float, cam, w: float, h: float):
        eye, right, up, fwd, ortho = cam
        if ortho:
            scale = 1.0 / self._orthoScale(w, h)
        else:
            scale = self._dist / self._focal(h)
        # 按模式取 pan 对应的两个世界分量（前视/右视的垂直轴是 Z）
        if self._mode == 2:      # 前视：pan=(X, Z)
            return ((dx * right[0] - dy * up[0]) * scale,
                    (dx * right[2] - dy * up[2]) * scale)
        if self._mode == 3:      # 右视：pan=(Y, Z)
            return ((dx * right[1] - dy * up[1]) * scale,
                    (dx * right[2] - dy * up[2]) * scale)
        # 透视 / 顶视：pan=(X, Y)
        return ((dx * right[0] - dy * up[0]) * scale,
                (dx * right[1] - dy * up[1]) * scale)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        # 命令激活时：左键取点（解析到 XY 平面，含捕捉）；中键拖拽旋转视图；
        # 右键完成/结束命令（与 2D 画布一致）
        if self._cm is not None and self._cm.hasActive():
            if event.button() == Qt.LeftButton:
                wp = self._resolvePoint(event.position())
                if wp is not None:
                    self._cm.onMousePress(wp, event.modifiers())
                    self.update()
                    event.accept()
                    return
            if event.button() == Qt.MiddleButton:
                # 中键拖拽旋转视图（命令期间也可自由查看模型）
                self._drag = (event.button(), event.position())
                event.accept()
                return
            if event.button() == Qt.RightButton:
                # 右键完成命令（区域浮雕右键结束；解析世界点供需要的命令使用）
                wp = self._resolvePoint(event.position())
                self._cm.onRightClick(
                    wp if wp is not None else Point3D(0.0, 0.0, 0.0))
                self.update()
                event.accept()
                return
            return
        # 无活动命令时优先命中控制点（命中即自动开启编辑），再命中操作轴，
        # 未命中则继续框选/点选/相机拖拽
        if event.button() == Qt.LeftButton and self._curveEdit is not None:
            if self._curveEdit.tryPressControlPoint(
                    self._curveEditAdapter, event.position(),
                    event.modifiers()):
                event.accept()
                return
        if self._gizmo is not None and event.button() == Qt.LeftButton and \
           self._gizmo.handleMousePress(self._gizmoAdapter, event.position(),
                                        event.modifiers()):
            event.accept()
            return
        # 左键：由框选状态机接管（点击延迟到释放判定；控制点/操作轴命中
        # 已在上方分支提前返回）。空白处按下立即取消选择，操作轴同步隐藏。
        # 浮雕锁定态同样放行：模型 isLocked 排除拾取，pin 保证曲面渲染不受
        # 选择变化影响；空白点击仅清空曲线选择（与 2D 一致）。
        if event.button() == Qt.LeftButton and self._doc is not None:
            if self._doc.selectedObjects():
                self._doc.clearSelection()
            self._boxStart = event.position()
            self._boxMods = event.modifiers()
            self._boxActive = False
            event.accept()
            return
        self._drag = (event.button(), event.position())
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._cm is not None and self._cm.hasActive():
            # 命令期间：中键拖拽旋转视图（右键已用于完成命令），不转发命令预览
            if self._drag is not None and self._drag[0] == Qt.MiddleButton:
                self._cameraDrag(event)
                event.accept()
                return
            self._cursorPos = event.position()
            wp = self._resolvePoint(event.position())
            if wp is not None:
                self._cm.onMouseMove(wp, event.modifiers())
            self.update()
            event.accept()
            return
        if self._curveEdit is not None and self._curveEdit.isDragging():
            self._curveEdit.handleMouseMove(self._curveEditAdapter,
                                            event.position(),
                                            event.modifiers())
            event.accept()
            return
        if self._gizmo is not None and self._gizmo.isDragging():
            # 操作轴拖动中（按下时已被操作轴消费，_drag 仍为 None）
            self._gizmo.handleMouseMove(self._gizmoAdapter,
                                        event.position(), event.modifiers())
            event.accept()
            return
        if self._boxStart is not None:
            # 框选拖拽：位移超阈值成框，实时更新选择矩形（屏幕坐标）
            pos = event.position()
            d = pos - self._boxStart
            if self._boxActive or abs(d.x()) > 4 or abs(d.y()) > 4:
                self._boxActive = True
                self._boxLTR = pos.x() >= self._boxStart.x()
                self._boxRect = QRectF(QPointF(self._boxStart),
                                       QPointF(pos)).normalized()
                self.update()
                event.accept()
                return
        if self._drag is None:
            if self._gizmo is not None:
                # 未拖动时仅悬停高亮（返回 False）
                self._gizmo.handleMouseMove(self._gizmoAdapter,
                                            event.position(), event.modifiers())
            return
        self._cameraDrag(event)
        event.accept()

    # ---------------- 拾取与选择 ----------------
    def _surfacePolyline(self, obj):
        """浮雕模型（SURFACE）拾取用底面四角折线（闭合）。"""
        bb = obj.getBoundingBox()
        if not bb.isValid():
            return []
        return [(bb.min.x, bb.min.y, 0.0), (bb.max.x, bb.min.y, 0.0),
                (bb.max.x, bb.max.y, 0.0), (bb.min.x, bb.max.y, 0.0)]

    @staticmethod
    def _pointInPoly(px: float, py: float, poly) -> bool:
        """射线法点在多边形内判定（屏幕坐标）。"""
        inside = False
        n = len(poly)
        for i in range(n):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % n]
            if (y1 > py) != (y2 > py):
                xi = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
                if px < xi:
                    inside = not inside
        return inside

    def _pickObject(self, mx: float, my: float):
        """屏幕点拾取：可见对象投影折线与点的最近屏幕距离 < 半径则命中。
        浮雕模型（SURFACE）按投影底面矩形内部命中（边界附近按线段距离）。"""
        if self._doc is None:
            return None
        w, h = float(self.width()), float(self.height())
        if w < 2 or h < 2:
            return None
        cam = self._camera()
        best = None
        bestD = PICK_RADIUS
        for obj in self._doc.visibleObjects():
            if obj.isLocked():
                continue
            if obj.type() == ObjectType.POINT:
                pos = obj.position()
                p = self._project((pos.x, pos.y, pos.z), cam, w, h)
                if p is not None and math.hypot(mx - p[0], my - p[1]) < bestD:
                    best, bestD = obj, math.hypot(mx - p[0], my - p[1])
                continue
            isSurface = obj.type() == ObjectType.SURFACE
            pts = self._surfacePolyline(obj) if isSurface \
                else self._objectPolyline(obj)
            if isSurface and len(pts) >= 3:
                sp = [self._project(pt, cam, w, h) for pt in pts]
                if all(s is not None for s in sp) and \
                        self._pointInPoly(mx, my, [(s[0], s[1]) for s in sp]):
                    best, bestD = obj, 0.0
                    continue
            closed = isSurface
            isClosed = getattr(obj, "isClosed", None)
            if callable(isClosed):
                closed = bool(isClosed())
            n = len(pts)
            if n < 2:
                continue
            segN = n if closed else n - 1
            for i in range(segN):
                s1 = self._project(pts[i], cam, w, h)
                s2 = self._project(pts[(i + 1) % n], cam, w, h)
                if s1 is None or s2 is None:
                    continue
                abx, aby = s2[0] - s1[0], s2[1] - s1[1]
                ab2 = abx * abx + aby * aby
                if ab2 < 1e-9:
                    d = math.hypot(mx - s1[0], my - s1[1])
                else:
                    t = max(0.0, min(1.0, ((mx - s1[0]) * abx + (my - s1[1]) * aby) / ab2))
                    d = math.hypot(mx - (s1[0] + t * abx), my - (s1[1] + t * aby))
                if d < bestD:
                    best, bestD = obj, d
        return best

    def _applyPickSelection(self, obj, mods) -> None:
        """拾取结果写入文档选择：Ctrl=追加/反选，Shift=加选，否则单选；点空白=清空。"""
        if mods & Qt.ControlModifier:
            if obj is not None:
                obj.setSelected(not obj.isSelected())
                self._doc.selectionChanged.emit()
            return
        if mods & Qt.ShiftModifier:
            if obj is not None:
                obj.setSelected(True)
                self._doc.selectionChanged.emit()
            return
        sel = self._doc.selectedObjects()
        if obj is not None and len(sel) == 1 and sel[0] is obj:
            return
        for o in sel:
            o.setSelected(False)
        if obj is not None:
            obj.setSelected(True)
        self._doc.selectionChanged.emit()

    # ---------------- 框选（屏幕空间，规则同 2D 画布） ----------------

    def _paintSelectBox(self, painter) -> None:
        """框选矩形：左→右包含选择蓝色实线，右→左相交选择绿色虚线（同 2D）。"""
        if self._boxLTR:
            border, fill = QColor(74, 144, 217, 200), QColor(74, 144, 217, 40)
            pen = QPen(border, 1, Qt.PenStyle.SolidLine)
        else:
            border, fill = QColor(95, 184, 95, 200), QColor(95, 184, 95, 40)
            pen = QPen(border, 1, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(QBrush(fill))
        painter.drawRect(self._boxRect)

    def _applyBoxSelection(self, screenRect: QRectF, mods) -> None:
        """框选落选：左→右包含选择（整体在框内），右→左相交选择；
        Shift 加选 / Ctrl 减选（同 2D 选择约定）。"""
        if self._doc is None:
            return
        hits = set()
        for obj in self._doc.visibleObjects():
            if obj.isLocked():
                continue
            if self._objectInScreenRect(obj, screenRect, self._boxLTR):
                hits.add(obj.id())
        cur = {o.id() for o in self._doc.objects() if o.isSelected()}
        if mods & Qt.KeyboardModifier.ControlModifier:
            newSel = cur - hits
        elif mods & Qt.KeyboardModifier.ShiftModifier:
            newSel = cur | hits
        else:
            if not hits:
                return   # 同 2D：空框选保留现有选择
            newSel = hits
        for o in self._doc.objects():
            o.setSelected(o.id() in newSel)
        self._doc.selectionChanged.emit()

    def _objectInScreenRect(self, obj, sr: QRectF, leftToRight: bool) -> bool:
        """对象投影到屏幕后的包含/相交判定（与 2D 同规则）。"""
        w, h = float(self.width()), float(self.height())
        cam = self._camera()
        if obj.type() == ObjectType.POINT:
            pos = obj.position()
            p = self._project((pos.x, pos.y, pos.z), cam, w, h)
            return p is not None and sr.contains(QPointF(p[0], p[1]))
        isSurface = obj.type() == ObjectType.SURFACE
        pts = self._surfacePolyline(obj) if isSurface \
            else self._objectPolyline(obj)
        if len(pts) < 2:
            return False
        sp = [self._project(pt, cam, w, h) for pt in pts]
        if leftToRight:
            # 包含选择：全部投影点在框内（投影失败的点视为不可见，不参与）
            return all(q is not None and sr.contains(QPointF(q[0], q[1]))
                       for q in sp)
        # 相交选择：任一投影点在框内，或任一线段与框相交
        if any(q is not None and sr.contains(QPointF(q[0], q[1])) for q in sp):
            return True
        closed = isSurface
        isClosed = getattr(obj, "isClosed", None)
        if callable(isClosed):
            closed = bool(isClosed())
        n = len(pts)
        segN = n if closed else n - 1
        for i in range(segN):
            a = sp[i]
            b = sp[(i + 1) % n]
            if a is None or b is None:
                continue
            if _segIntersectsRect(QPointF(a[0], a[1]), QPointF(b[0], b[1]), sr):
                return True
        return False

    def _cameraDrag(self, event: QMouseEvent) -> None:
        button, last = self._drag
        pos = event.position()
        dx = pos.x() - last.x()
        dy = pos.y() - last.y()
        self._drag = (button, pos)
        w, h = float(self.width()), float(self.height())
        cam = self._camera()
        if button == Qt.MiddleButton and self._mode == 0:
            # 中键拖拽：透视轨道旋转（右移增大 yaw → 内容右转跟随鼠标）
            self._yaw += math.radians(dx * 0.4)
            # 俯仰限幅：越过 ±87.5° 时 up 向量会在 (0,0,1)/(0,1,0) 间突变，
            # 画面闪跳到另一视角；限幅后轨道旋转始终连续。顶视请用方向栏模式 1。
            self._pitch = max(-PITCH_LIMIT,
                              min(PITCH_LIMIT, self._pitch + math.radians(dy * 0.4)))
            # 同步记忆值：否则切走再回透视时 setViewMode(0) 会恢复到旧角度（闪跳）
            self._perspYaw, self._perspPitch = self._yaw, self._pitch
            self.update()
        elif button in (Qt.MiddleButton, Qt.RightButton):
            # 平移：右键拖拽（任意视角）/ 正交视图中键拖拽（正交不能轨道旋转）
            # 内容跟随鼠标（pan 反向补偿，保证任意视角方向一致）
            wx, wy = self._pixelToWorld(dx, dy, cam, w, h)
            self._pan = (self._pan[0] - wx, self._pan[1] - wy)
            self.update()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # 命令激活时接管回车/取消（焦点在本视口时主窗口收不到按键）
        if self._cm is not None and self._cm.hasActive():
            if event.key() == Qt.Key_Escape:
                self._cm.cancelActive()
                self.update()
                return
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                self._cm.onEnter()
                self.update()
                return
            # 命令扩展按键（如画线 C 键闭合）
            if self._cm.onKeyPress(event.key(), event.modifiers()):
                self.update()
                return
        # 浮雕模式全局按键（不限区域浮雕命令内）：“先填色再启动命令”工作流
        #   W    —— 循环切换当前颜色（底部调色板高亮随之刷新）
        #   空格 —— 把选中的闭合曲线内部填成当前颜色（区域划分蒙版）
        if self._reliefModel is not None:
            if event.key() == Qt.Key_W:
                PaintState.instance().cycle()
                self.update()
                event.accept()
                return
            if event.key() == Qt.Key_Space:
                self._fillSelectedColor()
                self.update()
                event.accept()
                return
        super().keyPressEvent(event)

    def _isClosedCurve(self, obj) -> bool:
        """判定曲线是否闭合：优先 isClosed()，否则采样首尾重合（同区域浮雕拾取）。"""
        closed = getattr(obj, "isClosed", None)
        if callable(closed):
            return bool(closed())
        pts = sample_curve_points(obj, 128)
        if pts.shape[0] < 3:
            return False
        d_end = float(np.linalg.norm(pts[0] - pts[-1]))
        perim = float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum())
        return not (perim > 1e-9 and d_end > 1e-3 * perim)

    def _fillSelectedColor(self) -> None:
        """浮雕模式空格填色：把选中的闭合曲线内部填成当前颜色（区域划分蒙版）。

        与区域浮雕命令解耦——填色是“先填色再启动命令”工作流的一步，故在浮雕
        模式全局可用（左键选中闭合曲线→空格填色）。每次填色自成一次可撤销
        操作：区域浮雕命令内的“应用”已各自独立出栈，填色同样独立（不再并入
        命令会话），保证撤销粒度一致、且命令中途的填色不会被漏记历史。
        """
        model = self._reliefModel
        if model is None or self._doc is None:
            return
        curves = [o for o in self._doc.selectedObjects()
                  if o.type() != ObjectType.SURFACE and not o.isLocked()
                  and self._isClosedCurve(o)]
        if not curves:
            self.statusMessage.emit("请先用左键选中闭合曲线，再按空格填当前色")
            return
        # 曲线为世界坐标：模型若有未烘焙的放置变换，先烘焙对齐（与起浮雕同约定）
        baked = model.hasPendingTransform()
        if baked:
            model.bake()
        hf = model.heightField()
        grid = (hf.origin[0], hf.origin[1], hf.spacing, hf.nx, hf.ny)
        value = PaintState.instance().currentValue()
        self._doc.beginEdit()
        try:
            total = 0
            for o in curves:
                poly = sample_curve_points(o)
                if poly.shape[0] < 3:
                    continue
                inside = build_inside_mask(poly, grid)
                total += int(model.fillColorMask(inside, value))
            if total > 0 or baked:
                self._doc.notifyObjectChanged(model.id())
        finally:
            self._doc.endEdit()
        if total > 0:
            self.statusMessage.emit(
                f"已填色 {total} 格（{PaintState.instance().currentHex()}）")
        else:
            self.statusMessage.emit("所选区域已是当前颜色或未命中模型范围")

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton and self._boxStart is not None:
            start, mods, active = self._boxStart, self._boxMods, self._boxActive
            self._boxStart = None
            self._boxActive = False
            self._boxRect = QRectF()
            self.update()
            if active:
                rect = QRectF(QPointF(start), QPointF(event.position())).normalized()
                # 编辑目标激活时框选控制点，否则框选对象
                # （开关开着但未选中曲线时仍须能框选对象，故不能用 isEnabled）
                if self._curveEdit is not None and self._curveEdit.isActive():
                    self._curveEdit.boxSelect(self._curveEditAdapter, rect, mods)
                else:
                    self._applyBoxSelection(rect, mods)
            else:
                picked = self._pickObject(event.position().x(),
                                          event.position().y())
                self._applyPickSelection(picked, mods)
            event.accept()
            return
        if self._curveEdit is not None and \
           self._curveEdit.handleMouseRelease(self._curveEditAdapter,
                                              event.position()):
            event.accept()
            return
        if self._gizmo is not None and \
           self._gizmo.handleMouseRelease(self._gizmoAdapter, event.position()):
            event.accept()
            return
        if self._drag is not None and self._drag[0] == event.button():
            self._drag = None
        event.accept()

    def _screenToGround(self, mx, my, cam, w, h):
        """屏幕坐标反投影到 z=0 平面，返回世界点或 None。"""
        eye, right, up, fwd, ortho = cam
        cx, cy = w / 2.0, h / 2.0
        if ortho:
            scale = self._orthoScale(w, h)
            if scale < 1e-12:
                return None
            x = (mx - cx) / scale
            y = -(my - cy) / scale
            return (eye[0] + x * right[0] + y * up[0],
                    eye[1] + x * right[1] + y * up[1],
                    eye[2] + x * right[2] + y * up[2])
        focal = self._focal(h)
        sx = (mx - cx) / focal
        sy = -(my - cy) / focal
        dx = sx * right[0] + sy * up[0] + fwd[0]
        dy = sx * right[1] + sy * up[1] + fwd[1]
        dz = sx * right[2] + sy * up[2] + fwd[2]
        if abs(dz) < 1e-12:
            return None
        t = -eye[2] / dz
        if t < 0:
            return None
        return (eye[0] + t * dx, eye[1] + t * dy, eye[2] + t * dz)

    def wheelEvent(self, event: QWheelEvent) -> None:
        angle = event.angleDelta().y() / 120.0
        factor = 1.2 ** -angle
        w, h = float(self.width()), float(self.height())
        if w < 2 or h < 2:
            return
        cam = self._camera()
        mx = event.position().x()
        my = event.position().y()
        world = self._screenToGround(mx, my, cam, w, h)
        self._dist = max(40.0, min(2000.0, self._dist * factor))
        if world is not None:
            cam2 = self._camera()
            p = self._project(world, cam2, w, h)
            if p is not None:
                dx = mx - p[0]
                dy = my - p[1]
                wx, wy = self._pixelToWorld(dx, dy, cam2, w, h)
                self._pan = (self._pan[0] - wx, self._pan[1] - wy)
        self.update()
        event.accept()

    # ---------------- 对外接口 ----------------
    def setViewMode(self, mode: int) -> None:
        """切换视角模式（与方向栏一致）。"""
        if mode == self._mode:
            return
        if mode == 0:
            self._yaw, self._pitch = self._perspYaw, self._perspPitch
        else:
            self._perspYaw, self._perspPitch = self._yaw, self._pitch
        self._mode = mode
        self.update()

    def viewMode(self) -> int:
        return self._mode

    def resetView(self) -> None:
        """重置相机到当前模式的默认视角。"""
        self._dist = DEFAULT_DIST
        self._pan = (0.0, 0.0)
        if self._mode == 0:
            self._yaw = math.radians(135.0)
            self._pitch = math.radians(35.0)
            self._perspYaw, self._perspPitch = self._yaw, self._pitch
        self.update()


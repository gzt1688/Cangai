"""3D 视口：软渲染透视/正交投影，轨道相机，支持旋转翻面。"""
from __future__ import annotations

import gc
import math
import time

import numpy as np

from PySide6.QtCore import QPointF, QLineF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QGuiApplication,
                           QImage, QKeyEvent, QLinearGradient, QMouseEvent,
                           QPainter, QPainterPath, QPen, QPolygonF, QRegion,
                           QSurfaceFormat, QTransform, QWheelEvent)
from PySide6.QtOpenGL import QOpenGLFunctions_3_3_Compatibility
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QWidget

from core.vector.geometry_types import ObjectType, Point3D, BoundingBox3D
from core.vector.brep_object import BRepObject
from core.vector.brep_shape import BrepShape
from core.grid_manager import GridManager
import core.app_settings as app_settings
from core.snap.snap_manager import SnapManager
from core.snap.snap_types import SnapResult, SnapType
from core.relief import mesh3d, shade, paint_palette
from core.relief.region_relief import sample_curve_points, build_inside_mask
from core.relief.sweep import SweepParams, apply_brush_stamp
from ui.styles import theme
from ui.widgets.paint_state import PaintState
from ui.widgets.smear_state import (SmearState, SMEAR_DIAMETER,
                                    SMEAR_HALF_LIFE, SMEAR_HEIGHT,
                                    MODES as _SMEAR_MODES,
                                    MODE_LABELS as _SMEAR_LABELS)
from ui.widgets.step_hint import StepHintOverlay
from ui.view_2d.graphics_view import paintSnapMarker
from ui.view_drop import droppedFilePaths, acceptIfFiles
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
FAR = 100000.0
DEFAULT_DIST = 260.0
FOV_DEG = 45.0
PICK_RADIUS = 10.0      # 对象拾取屏幕半径（像素）
# "沿线行走"型吸附（与 2D 画布 graphics_view 定义一致）：自身正在绘制的
# 对象上离最后输入点很近的这类候选会把光标往回拽，由 _snapAt 按距离过滤
_WALKING_SNAPS = frozenset({SnapType.NEAREST, SnapType.MIDPOINT,
                            SnapType.PERPENDICULAR, SnapType.TANGENT,
                            SnapType.INTERSECTION})
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
# 为与“3D 模式”观感完全一致，默认实心 (1.0)，不透亮不发灰。叠加层(曲线)已通
# 过“深度清除 + 置顶”保证可见，即使 1.0 也能点选；需要 X-Ray 层次时改此值
# 或在右侧变换面板手动调低（推荐 0.85~0.92）。
_RELIEF_XRAY_OPACITY = 1.0  # 浮雕编辑态曲面透明度：默认 1.0（实心、不透亮），
                             # 观感与“3D模式”完全一致。需要 X-Ray 半透明叠加
                             # 显示参考曲线时，可在此处或变换面板降到 0.80~0.90。

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
    # 鼠标移动坐标（Point3D，Z=鼠标处模型高度），主窗口接入底部状态栏
    mouseMoved = Signal(object)
    # 视口拖入文件：路径列表 + 落点世界坐标（XY 平面，主窗口接入导入）
    fileDropped = Signal(list, object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)   # 支持把 png/jpg/dxf/stl 直接拖进视口
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
        # 相机注视点高度：透视下注视地面会让有高度的模型投影到屏幕
        # 中心上方，且缩放时偏移随距离变化（越近越偏），表现为"缩放
        # 模型会跑"。注视点抬到模型高度中心后，模型垂直居中且缩放稳定。
        self._targetZ = 0.0
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
        # 浮雕模式全局 Ctrl+左键涂抹（堆料/去料，模式见 ui/widgets/smear_state）：
        # 按下先"待定"，移动超过阈值才成笔画——原地点击仍走点选（Ctrl=追加/反选）
        self._smearPending = None       # 按下时的屏幕坐标（QPointF）或 None
        self._smearMods = Qt.KeyboardModifier.NoModifier
        self._smearActive = False       # 涂抹笔画进行中
        self._smearLast = None          # 最近一次盖章的世界坐标 (x, y)
        self._smearLastNotify = 0.0     # 上次推进版本+通知视口的时刻（节流用）
        self._smearStamps = 0           # 本笔盖章次数（松开时反馈）

        # XY 标注子标注拖动（3D 视口，与 2D 画布一致）：
        # 选中 XY 标注后，拖动读数文字可自由摆放 X 宽/Y 高子标注位置
        self._dimDragObj = None
        self._dimDragAxis = ""          # 'x'=拖动 X 宽读数(改 yOff)，'y'=拖动 Y 高读数(改 xOff)
        self._dimStartWorld = None      # 按下时的世界点（z=0 平面）
        self._dimStartOx = 0.0
        self._dimStartOy = 0.0
        self._dimCursorSet = False      # 标注悬停光标是否已设置（便于还原）

        # 3D 模式实体显示模式："wireframe"=仅结构线，"rendered"=面填充+线框
        self._solidDisplayMode = "rendered"
        # 渲染模式透视开关（变换面板"透视"按钮）：True=X-Ray 透视
        # （表面半透明、背面线可见），False=实体不透明 + 隐藏线消除
        # （只画可见特征边）。线框模式不受影响（始终显示全部结构线）。
        self._solidPerspective = False
        # 3D 实体光影（变换面板滑块）：方位角/仰角（世界坐标，度）。
        # 顶视图默认方位 0=画面正上方，仰角越低阴影越长。
        self._solidLightAz = 0.0
        self._solidLightAlt = 60.0
        # 网格 STL 实体"高度着色"开关（变换面板"高度色"按钮，默认关）：
        # True 时此类实体按高度取地形色带逐顶点上色——台阶平台/浅纹在
        # 顶视图也能辨（纯光照对法向全同的平台无信号）。默认保持单色
        # 实体观感。
        self._meshHeightColor = False

        # 子元素选择（Plasticity 风格）：点/线/面/实体 过滤任意组合（1/2/3/4 增减）
        self._selFilters = {"body"}      # ⊆ {'body','face','edge','vertex'}
        self._subSel = []                # [(obj, kind, idx)] kind∈{'face','edge','vertex'}
        self._subHover = None            # 悬停中的子元素 (obj, kind, idx)
        # 浮雕模式锁定的模型（进入浮雕态 pin 住，渲染不依赖瞬时选中）+ 网格缓存
        self._reliefModel = None
        self._reliefDrawn = False   # 本帧 GPU 浮雕是否成功绘制（CNC 兜底用）
        self._cncDiagNote = None    # CNC 渲染兜底异常备注（诊断红字用）
        self._cncDbgTick = 0        # CNC 渲染日志节流计数
        self._reliefMesh = None
        self._reliefMeshKey = None
        self._reliefMeshLodDim = None    # 构建当前 mesh 用的 LOD 长边上限（色号蒙版对齐）
        # 脏区域增量更新：上次构建网格所用的降采样源场快照 + 元数据
        # （id/放置/夸张/密度一致且仅高度变化 → 笔画期只重算变化区域）
        self._meshSrcGrid = None
        self._meshSrcMeta = None
        self._terrainLutCache = None
        self._hideReferenceGrid = True    # 默认隐藏参考地面/网格线，仅留浮雕曲面
        # GPU 渲染状态：GL 渲染器、网格/底色缓冲缓存 key、边框叠加所需投影
        self._gl = None
        self._glKey = None
        self._surfKey = None
        # B-Rep GPU 表面连续"空帧"计数：>=8 时判定该通道不可靠，
        # 粘滞走 CPU z-buffer 光栅化（实体绝不因 GPU 静默空转而消失）
        self._brepGpuBad = 0
        try:
            self.setUpdateBehavior(QOpenGLWidget.NoPartialUpdate)
        except Exception:
            pass
        self._reliefProj = None
        # B-Rep 表面光栅化帧间缓存（key 命中直接复用，不重算 13ms 光栅化）
        self._zbCacheKey = None
        self._zbCacheQImg = None
        self._zbCacheFrame = None
        self._zbLowRes = False
        self._zbLastKeyT = 0.0
        # _rasterTriZB 全局网格缓存（按 (W,H) 每帧只建一次）
        self._zbGridKey = None
        self._zbGridX = None
        self._zbGridY = None
        # 逐顶点缓冲（位置/法线/底色/索引）按 surfKey 缓存：旋转时不变 → 不重算
        self._glCache = None
        self._glCacheKey = None
        # 笔画期增量同步：缓存对应的网格实例 / 已同步到的版本 key，
        # 以及自上次同步以来累计变化的顶点 ROI（含端；None=无变化）
        self._glMeshRef = None
        self._glMeshKey = None
        self._roiBoxSinceGl = None
        self._geoDirtySinceGl = False   # 自上次 GL 同步以来出现过高度改动（兜底标记）
        self._hnormDirty = False        # 笔画期推迟的高度归一化待补算
        self._glBaseDirty = False       # 底色需整份刷新（笔画结束后的精确归一化）
        self._glRoiPending = None       # 本帧需写进 GPU 的顶点块（None=整份重传）
        self._mapColorNorm = None       # map 模式底色归一化区间（ROI 刷新沿用）
        self._mapLutF32 = None          # 地形 LUT 的 float32 预乘缓存
        self._reliefRefreshTimer = None
        # 三角形索引按网格拓扑 (gnx,gny) 缓存：拓扑在雕刻/磨光笔画期间不变，
        # 每次版本刷新只重算顶点/法线/底色，索引直接复用（省 ~1.5MB 重算）
        self._triIdxCache = None
        self._triIdxCacheKey = None
        self._triIdxMeshRef = None   # 持有网格防 id 复用（见 _triIndexFor）
        # 边框投影帧间缓存（网格 key + 相机 + 尺寸不变时复用）
        self._projKey = None
        self._projCache = None
        # 笔画期间暂停 Python 循环 GC（重建产生大量临时数组，gen2 回收会造成
        # 偶发上百毫秒的全局停顿）；笔画开始前先清一次，结束时恢复
        self._gcWasEnabled = True
        c = QColor(theme.BG_VIEWPORT)
        self._bgFloat = (c.redF(), c.greenF(), c.blueF(), 1.0)
        # 捕捉（与 2D 画布同规则：命令取点先对象捕捉，未命中再网格吸附）
        self._grid = GridManager.instance()
        self._snap = SnapManager.instance()
        self._lastSnap = None
        self._cursorPos = QPointF()
        # 左下角信息区（与 2D 画布同一 StepHintOverlay：步骤行 + 结果行），
        # 浮雕模式的命令反馈（结果/步骤提示）都显示在这里
        self._stepHint = StepHintOverlay(self)
        self._layoutStepHint()
        self._stepHint.setStep("提示信息显示区")
        # 画布模式（"2d"/"relief"/"3d"/"cnc"）：3D 模式下只显示 3D 空间曲线、
        # 不渲染浮雕模型；命令取点走构造平面/侧视平面，可带 Z（无限制）
        self._canvasMode = "2d"
        self._lastSolidFocused = False
        self._drawZ = 0.0            # 3D 模式构造平面高度（顶视/透视取点 Z）
        self._lastResolved = None    # 最近一次命令取点的三维结果（预览用）
        self._buildSelModeBar()
        # CNC 刀路轨迹：监听全局 CncState，路径变化时刷新视口（仅 cnc 模式绘制）
        try:
            from core.cnc.state import CncState
            _st = CncState.instance()
            _st.stateChanged.connect(self._onCncStateChanged)
            # 模拟播放轻量推进：只重绘 3D 视口，不触发 2D/向导全量重建
            _st.simChanged.connect(self._onCncSimChanged)
        except Exception:
            pass

    def _onCncStateChanged(self) -> None:
        try:
            solid = False
            try:
                from core.cnc.state import CncState
                solid = bool(CncState.instance().showSolidSim)
            except Exception:
                pass
            # 实体勾选只在 CNC 模式聚焦毛坯；其他模式不碰相机（避免
            # 切到浮雕/3D 时视角被 CNC 状态污染）
            if (solid and self._canvasMode == "cnc"
                    and not self._lastSolidFocused):
                self._lastSolidFocused = True
                self._focusStock()
            elif not solid:
                self._lastSolidFocused = False
            self.update()
        except Exception:
            pass

    def _onCncSimChanged(self) -> None:
        """模拟播放每 tick 的轻量重绘（刀具移动/实体切削推进）。"""
        try:
            self.update()
        except Exception:
            pass

    def _focusStock(self) -> None:
        """勾选"实体"后相机聚焦毛坯：标准 3/4 俯视，坯料占画面约 60%。"""
        try:
            from core.cnc.state import CncState
            st = CncState.instance()
            r = max(float(st.stock.width), float(st.stock.height),
                    float(st.stock.thickness)) * 0.65
            self._pan = (0.0, 0.0)
            self._targetZ = 0.0
            self._mode = 0
            self._pitch = 0.5          # 约 29° 俯视，看到顶面 + 两个侧面
            self._yaw = 0.0
            self._dist = r / (0.62 * math.tan(math.radians(FOV_DEG) / 2.0))
            lo, _hi = self._distLimits()
            if self._dist < lo:
                self._dist = lo
        except Exception:
            pass

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
        self._onDocRemoved = self._onDocRemovedSlot
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

    def solidDisplayMode(self) -> str:
        """3D 模式实体显示模式："wireframe"=仅结构线，"rendered"=面填充+线框。"""
        return self._solidDisplayMode

    def setSolidDisplayMode(self, mode: str) -> None:
        """设置 3D 模式实体显示模式（wireframe/rendered），刷新视口。"""
        if mode not in ("wireframe", "rendered"):
            return
        if self._solidDisplayMode != mode:
            self._solidDisplayMode = mode
            self.update()

    def cycleSolidDisplayMode(self) -> None:
        """Tab 键循环切换 3D 实体显示模式：线框 ↔ 渲染。"""
        nxt = "wireframe" if self._solidDisplayMode == "rendered" else "rendered"
        self.setSolidDisplayMode(nxt)
        self.statusMessage.emit(
            "线框模式" if nxt == "wireframe" else "渲染模式")

    def solidPerspective(self) -> bool:
        """渲染模式透视开关：True=X-Ray 透视，False=不透明+隐藏线消除。"""
        return self._solidPerspective

    def setSolidPerspective(self, on: bool) -> None:
        """设置渲染模式透视开关，刷新视口。"""
        if self._solidPerspective != on:
            self._solidPerspective = on
            self.update()

    def setMeshHeightColor(self, on: bool) -> None:
        """设置网格 STL 实体"高度着色"开关（仅影响此类实体），刷新视口。"""
        if self._meshHeightColor != on:
            self._meshHeightColor = on
            self.update()

    def meshHeightColor(self) -> bool:
        return self._meshHeightColor

    def setSolidLight(self, az: float, alt: float) -> None:
        """设置 3D 实体光影方向（世界坐标方位角/仰角），刷新视口。"""
        az = float(np.clip(az, 0.0, 360.0))
        alt = float(np.clip(alt, 5.0, 85.0))
        if (self._solidLightAz, self._solidLightAlt) != (az, alt):
            self._solidLightAz, self._solidLightAlt = az, alt
            self.update()

    # ---------------- 子元素选择过滤（体/面/边/点，Plasticity 风格） ----------------

    _SEL_MODE_LABELS = {"body": "体", "face": "面", "edge": "边", "vertex": "点"}

    def selFilters(self) -> set:
        """当前选择过滤集合（'body'/'face'/'edge'/'vertex' 任意组合）。"""
        return set(self._selFilters)

    def selectionMode(self) -> str:
        """兼容旧接口：过滤集合只有一个元素时返回它，否则 'body'。"""
        if len(self._selFilters) == 1:
            return next(iter(self._selFilters))
        return "body"

    def _autoPushPullReady(self) -> bool:
        """推拉常驻：3D 模式且过滤器含面或线时，点面/线自动出推拉手柄。"""
        return self._canvasMode in ("3d", "cnc") \
            and bool({"face", "edge"} & self._selFilters)

    def _autoStartPushPull(self, mx: float, my: float, mods) -> None:
        """点击面/线自动进入推拉（免按钮）：启动命令并注入该次点击。"""
        cm = self._cm
        if cm is None:
            return
        if not cm.hasActive():
            cm.executeCommand("PushPull")
        inst = cm.activeInstance()
        if inst is None:
            return
        wp = self._screenToXYPlane(mx, my)
        if wp is None:
            return
        inst.onMouseMove(wp, mods)
        inst.onMousePress(wp, mods)
        self.update()

    def _syncAutoPushPull(self) -> None:
        """推拉常驻同步：过滤器不再含面/线时，结束自动推拉命令。"""
        if self._cm is None or not self._cm.hasActive():
            return
        if self._cm.activeName() != "PushPull":
            return
        if not self._autoPushPullReady():
            self._cm.cancelActive()

    def setSelectionMode(self, mode: str) -> None:
        """设置唯一选择过滤（替换整个集合）；兼容旧单选接口。"""
        if mode not in self._SEL_MODE_LABELS:
            return
        if self._selFilters != {mode}:
            self._selFilters = {mode}
            self._subSel = []
            self._subHover = None
            self.update()
        # 总是同步按钮选中态：Qt 的 checkable 按钮被点击时会自动 toggle checked，
        # 若已是该模式（集合未变）不刷新会导致按钮视觉上被取消选中。
        self._syncSelModeBar()
        self._syncAutoPushPull()
        self.statusMessage.emit(
            f"选择过滤：{self._filterLabel()}（Shift+点击/数字键 加选）")

    def toggleSelectionMode(self, mode: str) -> None:
        """增/减一个选择过滤（Shift+按钮点击、Shift+快捷键 1/2/3/4）；任意组合。

        集合不会为空：减掉最后一项时回退到体选择。"""
        if mode not in self._SEL_MODE_LABELS:
            return
        if mode in self._selFilters:
            self._selFilters.discard(mode)
            if not self._selFilters:
                self._selFilters = {"body"}
        else:
            self._selFilters.add(mode)
        self._subSel = []
        self._subHover = None
        self._syncSelModeBar()
        self._syncAutoPushPull()
        self.update()
        self.statusMessage.emit(f"选择过滤：{self._filterLabel()}")

    def _onSelModeButtonClicked(self, mode: str) -> None:
        """过滤按钮点击：普通点击=切换(单选，再点不取消)；Shift+点击=加选/减选。"""
        if QGuiApplication.queryKeyboardModifiers() & Qt.ShiftModifier:
            self.toggleSelectionMode(mode)
        else:
            self.setSelectionMode(mode)

    def cycleSelectionMode(self) -> None:
        order = ["body", "face", "edge", "vertex"]
        self.setSelectionMode(order[(order.index(self.selectionMode()) + 1) % 4])

    def _filterLabel(self) -> str:
        order = ["body", "face", "edge", "vertex"]
        return "+".join(self._SEL_MODE_LABELS[m]
                        for m in order if m in self._selFilters) or "体"

    def subSelection(self):
        """当前子元素选择 [(obj, kind, idx)]（供 B-Rep 命令读取）。"""
        return list(self._subSel)

    def subSelectedObjects(self):
        """子元素选中态引用的宿主对象去重列表（体/面/边/点高亮的对象）。

        子元素选中只存在视口（不写文档选中态），Del 等按"选中对象"
        操作的入口用它与文档选中互补。"""
        out, seen = [], set()
        for obj, _kind, _idx in self._subSel:
            if obj.id() not in seen:
                seen.add(obj.id())
                out.append(obj)
        return out

    def clearSubSelection(self) -> None:
        if self._subSel or self._subHover is not None:
            self._subSel = []
            self._subHover = None
            self.update()

    def _brepPickCandidates(self):
        """参与子元素拾取的 B-Rep 对象（可见未锁定）。"""
        if self._doc is None:
            return []
        return [o for o in self._doc.visibleObjects()
                if isinstance(o, BRepObject) and not o.isLocked()
                and o.shape() is not None]

    def _convertSolidInDoc(self, solid):
        """挤出实体原地转 B-Rep（一次撤销）；失败返回 None。"""
        try:
            brep = solid.toBRep()
            if brep is None or brep.shape() is None:
                return None
            brep._color = solid.color()
            brep._lineWidth = solid.lineWidth()
            doc = self._doc
            doc.beginEdit()
            try:
                doc.addObject(brep)
                doc.removeObject(solid.id())
            finally:
                try:
                    doc.endEdit()
                except Exception:
                    pass
            return brep
        except Exception:
            return None

    def _pickSubElement(self, mx: float, my: float, kinds=None):
        """子元素拾取：所有可见 B-Rep 对象上按 kind 集合找最近命中。

        kinds 为 None 时用当前过滤集合的子元素部分。返回屏幕距离最近的
        (obj, kind, idx) 或 None。向量化实现（顶点单次投影 + numpy），
        悬停时每帧调用也不卡。面：投影三角形命中（屏内距离 0，同距取
        深度最近）；边：离散折线屏幕距离；点：顶点屏幕距离。
        多 kind 时取屏幕距离最近者，同距(±4px)偏向更细元素（点>线>面）。
        """
        if kinds is None:
            kinds = {k for k in self._selFilters if k != "body"}
        kinds = {k for k in (kinds or ()) if k in ("face", "edge", "vertex")}
        if not kinds:
            return None
        w, h = float(self.width()), float(self.height())
        if w < 2 or h < 2:
            return None
        cam = self._camera()
        bests = {"face": None, "edge": None, "vertex": None}
        for obj in self._brepPickCandidates():
            top = obj.topology()
            mesh = obj.mesh()
            if mesh is None:
                continue
            verts = mesh["verts"]
            if len(verts) == 0:
                continue
            # 顶点一次性投影（向量化）：逐点调 _project 是悬停每帧的卡顿源
            # （10 万顶点 ≈ 数万次 Python 调用）。depth 为沿视线深度，
            # 相对比较与欧氏距离同序，取最近面的判定不变。
            sx, sy, dep, vok = self._projectVerts(
                np.asarray(verts, dtype=np.float64), cam, w, h)
            P = np.column_stack((sx, sy))
            P[~vok] = (-1e9, -1e9)
            D = np.where(vok, dep, np.inf)
            if "face" in kinds:
                tris = mesh["tris"]
                if len(tris):
                    T = tris
                    A = P[T[:, 0]]
                    Bp = P[T[:, 1]]
                    C = P[T[:, 2]]
                    # 有效性用投影 valid 标记：钳位坐标恒为有限值，
                    # isfinite 判不住近平面后/背面的顶点，会产生横跨
                    # 相机前后的大三角形误命中
                    ok = vok[T[:, 0]] & vok[T[:, 1]] & vok[T[:, 2]]
                    # 重心坐标（/screen 空间）与内外判定（方向无关）
                    den = ((Bp[:, 1] - C[:, 1]) * (A[:, 0] - C[:, 0])
                           + (C[:, 0] - Bp[:, 0]) * (A[:, 1] - C[:, 1]))
                    with np.errstate(divide="ignore", invalid="ignore"):
                        w0 = ((Bp[:, 1] - C[:, 1]) * (mx - C[:, 0])
                              + (C[:, 0] - Bp[:, 0]) * (my - C[:, 1])) / den
                        w1 = ((C[:, 1] - A[:, 1]) * (mx - C[:, 0])
                              + (A[:, 0] - C[:, 0]) * (my - C[:, 1])) / den
                    inside = ok & (den != 0) & (w0 >= 0) & (w1 >= 0)                         & (w0 + w1 <= 1)
                    if inside.any():
                        # 屏内命中：深度最近者
                        depth = np.where(inside,
                                         (D[T[:, 0]] + D[T[:, 1]]
                                          + D[T[:, 2]]) / 3.0, np.inf)
                        fi = int(np.argmin(depth))
                        b, bd = (obj, fi), 0.0
                        old = bests["face"]
                        if old is None or depth[fi] < old[2]:
                            bests["face"] = (b[0], b[1], depth[fi])
                    else:
                        # 屏内未命中：取所有三角形边的最近屏幕距离（限前 2000 个
                        # 三角形，避免超大网格逐边算）
                        cnt = min(len(T), 2000)
                        bd_best, bi_best = PICK_RADIUS, -1
                        for ti in range(cnt):
                            ia, ib, ic = T[ti]
                            if not (np.isfinite(P[ia, 0]) and
                                    np.isfinite(P[ib, 0]) and
                                    np.isfinite(P[ic, 0])):
                                continue
                            for pa, pb in ((P[ia], P[ib]), (P[ib], P[ic]),
                                           (P[ic], P[ia])):
                                dd = self._screenSegDist(mx, my,
                                                         (pa[0], pa[1]),
                                                         (pb[0], pb[1]))
                                if dd < bd_best:
                                    bd_best, bi_best = dd, ti
                        if bi_best >= 0:
                            bests["face"] = (obj, bi_best, bd_best)
            if "edge" in kinds:
                for ei, poly in enumerate(top["edgePolylines"]):
                    m = len(poly)
                    if m < 2:
                        continue   # 单点折线：空段数组上 .min() 会抛错
                    ex_, ey_, _, eok = self._projectVerts(
                        np.asarray(poly, dtype=np.float64), cam, w, h)
                    PX = np.where(eok, ex_, np.nan)
                    PY = np.where(eok, ey_, np.nan)
                    ax_, ay_ = PX[:-1], PY[:-1]
                    bx_, by_ = PX[1:], PY[1:]
                    fin = np.isfinite(ax_) & np.isfinite(bx_)
                    abx = bx_ - ax_
                    aby = by_ - ay_
                    len2 = abx * abx + aby * aby
                    len2[len2 < 1e-18] = 1e-18
                    t = ((mx - ax_) * abx + (my - ay_) * aby) / len2
                    np.clip(t, 0.0, 1.0, out=t)
                    dx2 = mx - (ax_ + t * abx)
                    dy2 = my - (ay_ + t * aby)
                    dists = np.sqrt(dx2 * dx2 + dy2 * dy2)
                    dists[~fin] = np.inf
                    dmin = float(dists.min())
                    if dmin <= PICK_RADIUS and (
                            bests["edge"] is None or dmin < bests["edge"][2]):
                        bests["edge"] = (obj, ei, dmin)
            if "vertex" in kinds:
                fin = vok
                dists = np.sqrt((P[:, 0] - mx) ** 2 + (P[:, 1] - my) ** 2)
                dists[~fin] = np.inf
                vi = int(np.argmin(dists))
                if dists[vi] <= PICK_RADIUS and (
                        bests["vertex"] is None
                        or dists[vi] < bests["vertex"][2]):
                    bests["vertex"] = (obj, vi, float(dists[vi]))
        cand = [(kind, v[0], v[1], v[2]) for kind, v in bests.items()
                if v is not None]
        if not cand:
            return None
        minD = min(c[3] for c in cand)
        for kind in ("vertex", "edge", "face"):
            for kname, obj, idx, d in cand:
                if kname == kind and d <= minD + 4.0:
                    return (obj, kname, idx)
        return None

    def _applySubPick(self, hit, mods) -> None:
        """子元素拾取结果写入选择：Ctrl=追加/反选，Shift=加选，否则单选。"""
        if hit is None:
            self._subSel = []
            self.update()
            return
        if hit in self._subSel:
            if mods & Qt.ControlModifier:
                self._subSel.remove(hit)
        elif mods & (Qt.ControlModifier | Qt.ShiftModifier):
            self._subSel.append(hit)
        else:
            self._subSel = [hit]
        self.update()

    def _drawSubSelection(self, painter, cam, w: float, h: float) -> None:
        """子元素高亮：选中面（半透明黄）+ 选中边（加粗黄）+ 选中点（黄点）；
        悬停子元素用白色细高亮。"""
        entries = list(self._subSel)
        hover = self._subHover
        if hover is not None:
            entries.append(hover)
        for obj, kind, idx in entries:
            top = obj.topology()
            isHover = (hover is not None and obj is hover[0]
                       and kind == hover[1] and idx == hover[2])
            if kind == "face":
                tris = obj.triangles()
                if idx >= len(top["faceRanges"]):
                    continue
                rng = top["faceRanges"][idx]
                start, cnt = rng[0], rng[1]
                # 完整面填充：不描三角边，避免高亮时露出结构线；
                # 悬停浅黄（明显可辨），选中深黄半透明
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(255, 213, 79, 110) if isHover
                                 else QColor(255, 193, 7, 160))
                for ti in range(start, start + cnt):
                    t = tris[ti]
                    sp = [self._project(tuple(t[k]), cam, w, h) for k in range(3)]
                    if any(s is None for s in sp):
                        continue
                    painter.drawPolygon(QPolygonF(
                        [QPointF(s[0], s[1]) for s in sp]))
            elif kind == "edge":
                if idx >= len(top["edgePolylines"]):
                    continue
                poly = top["edgePolylines"][idx]
                pen = QPen(QColor("#ff9800") if isHover else QColor("#ffd54f"),
                           3.0 if isHover else 2.0)
                pen.setCosmetic(True)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                for i in range(len(poly) - 1):
                    s1 = self._project(poly[i], cam, w, h)
                    s2 = self._project(poly[i + 1], cam, w, h)
                    if s1 is not None and s2 is not None:
                        painter.drawLine(QPointF(s1[0], s1[1]),
                                         QPointF(s2[0], s2[1]))
            elif kind == "region":
                # 曲线分割区域（推拉命令）：loops=世界 3D 折线环列表，
                # 奇偶填充——外环+内孔（圆环面）能正确镂空
                path = QPainterPath()
                drew = False
                for loop in idx:
                    pg = QPolygonF()
                    for p in loop:
                        s = self._project(tuple(p), cam, w, h)
                        if s is not None:
                            pg.append(QPointF(s[0], s[1]))
                    if pg.size() >= 3:
                        path.addPolygon(pg)
                        path.closeSubpath()
                        drew = True
                if drew:
                    pen = QPen(QColor("#ffffff") if isHover
                               else QColor("#ffb300"),
                               1.8 if isHover else 1.4)
                    pen.setCosmetic(True)
                    painter.setPen(pen)
                    painter.setBrush(QColor(255, 213, 79, 110) if isHover
                                     else QColor(255, 193, 7, 160))
                    painter.drawPath(path)
            else:
                if idx >= len(top["vertices"]):
                    continue
                pr = self._project(top["vertices"][idx], cam, w, h)
                if pr is None:
                    continue
                painter.setPen(QPen(QColor("#ffd54f"), 1.5))
                painter.setBrush(QColor("#ffd54f"))
                painter.drawEllipse(QPointF(pr[0], pr[1]), 4.0, 4.0)

    def setCommandManager(self, cm) -> None:
        self._cm = cm
        if cm is not None:
            cm.activeCommandChanged.connect(self._onActiveCommandChanged)
            cm.promptChanged.connect(self._onPromptChanged)
            cm.resultMessage.connect(self._onResultMessage)
            # 命令预览（笔刷圈）不经鼠标事件变化（如 A/S 改笔刷大小）时，
            # 立即重绘本视口——修复"改大小圆圈不更新"
            cm.previewInvalidated.connect(self.update)

    def _onActiveCommandChanged(self, name: str) -> None:
        """命令结束（name 为空）时清空捕捉标记，避免气泡残留。"""
        if not name:
            self._lastSnap = None
            self.update()

    def _onPromptChanged(self, text) -> None:
        """命令步骤提示（左下角信息区步骤行）。
        空闲态（默认提示）且有选中摘要时优先显示选中摘要，与 2D 画布一致。"""
        display = text
        if text == "提示信息显示区" and getattr(self, "_selectionSummary", ""):
            display = self._selectionSummary
        self._stepHint.setStep(display)
        self._layoutStepHint()

    def setSelectionSummary(self, summary: str) -> None:
        """mainWindow 选中变化时调用（对齐 2D 画布左下角）：
        无命令活动时立即显示开放/闭合曲线等选中摘要；命令活动期间
        由命令结束后的 promptChanged 自然恢复显示。"""
        self._selectionSummary = str(summary or "")
        cm = getattr(self, "_cm", None)
        if cm is None or not cm.hasActive():
            self._stepHint.setStep(self._selectionSummary or "提示信息显示区")
            self._layoutStepHint()

    def _onResultMessage(self, text) -> None:
        """操作结果反馈（左下角信息区结果行，自动淡出）。"""
        self._stepHint.setResult(text)
        self._layoutStepHint()

    def _layoutStepHint(self) -> None:
        """把信息区固定在视口左下角（与 2D 画布同布局：按文本精确算宽高）。"""
        hint = self._stepHint
        hint.setWordWrap(False)
        from PySide6.QtGui import QTextDocument
        doc = QTextDocument()
        doc.setDefaultFont(hint.font())
        doc.setHtml(hint.text())
        doc.setTextWidth(-1)
        pad_w, pad_h = 16, 8
        w = max(int(doc.idealWidth()) + pad_w, 20)
        h = max(int(doc.size().height()) + pad_h, 20)
        hint.setFixedSize(w, h)
        hint.move(10, self.height() - hint.height() - 2)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._layoutStepHint()
        self._layoutSelModeBar()

    # ---------------- 画布模式与 3D 绘制高度 ----------------
    def setCanvasMode(self, mode: str) -> None:
        """同步画布模式："3d" = 只显示 3D 空间曲线、不渲染浮雕模型、
        取点支持 Z；其余模式保持原行为（2D/浮雕空间曲线 + 浮雕模型）。"""
        if mode not in ("2d", "relief", "3d", "cnc"):
            mode = "2d"
        if self._canvasMode == mode:
            return
        try:
            from ui.view_3d._renderdbg import dbg
            dbg(f"setCanvasMode {self._canvasMode} -> {mode}")
        except Exception:
            pass
        self._canvasMode = mode
        self._lastSnap = None
        self._lastResolved = None
        self._updateSelModeBarVisibility()
        self.update()

    def canvasMode(self) -> str:
        return self._canvasMode

    def drawZ(self) -> float:
        """3D 模式构造平面高度（顶视/透视取点落在此 Z；无任何限制）。"""
        return self._drawZ

    def _buildSelModeBar(self) -> None:
        """左上角子元素选择过滤条（仅 3D 模式显示）：点/线/面/实体 单选，
        与快捷键 1/2/3/4（体/面/边/点）共用 setSelectionMode。"""
        from PySide6.QtWidgets import QPushButton
        bar = QWidget(self)
        bar.setObjectName("selModeBar")
        bar.setStyleSheet(
            f"QWidget#selModeBar {{ background: {theme.BG_PANEL};"
            f" border: 1px solid {theme.BORDER}; border-radius: 4px; }}"
            f"QWidget#selModeBar QLabel {{ color: {theme.TEXT_DIM};"
            f" background: transparent; border: none; }}"
            f"QWidget#selModeBar QPushButton {{ background: transparent;"
            f" color: {theme.TEXT}; border: none; border-radius: 3px;"
            f" padding: 2px 8px; }}"
            f"QWidget#selModeBar QPushButton:checked {{"
            f" background: {theme.PRIMARY}; color: #ffffff; }}")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(2)
        # 顺序：点-线-面-实体；普通点击切换(单选)，Shift+点击加选/减选
        self._selModeButtons = {}
        for label, mode, key in (("点", "vertex", "1"), ("线", "edge", "2"),
                                 ("面", "face", "3"), ("实体", "body", "4")):
            btn = QPushButton(label, bar)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(f"{label}选择（快捷键 {key} 切换；Shift+点击/数字键 加选）")
            btn.clicked.connect(lambda checked=False, m=mode:
                                self._onSelModeButtonClicked(m))
            lay.addWidget(btn)
            self._selModeButtons[mode] = btn
        self._selModeBar = bar
        self._syncSelModeBar()
        bar.setVisible(self._canvasMode == "3d")

    def _syncSelModeBar(self) -> None:
        """选择过滤变化后同步按钮选中态。"""
        buttons = getattr(self, "_selModeButtons", None)
        if not buttons:
            return
        for mode, btn in buttons.items():
            btn.blockSignals(True)
            btn.setChecked(mode in self._selFilters)
            btn.blockSignals(False)

    def _layoutSelModeBar(self) -> None:
        """把选择过滤条放在视口左上角（尺寸自适应内容）。"""
        bar = getattr(self, "_selModeBar", None)
        if bar is None:
            return
        bar.adjustSize()
        bar.move(10, 2)

    def _updateSelModeBarVisibility(self) -> None:
        bar = getattr(self, "_selModeBar", None)
        if bar is not None:
            bar.setVisible(self._canvasMode == "3d")
            self._layoutSelModeBar()

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

        目标切换必须清空**全部**浮雕渲染缓存：缓存键含 ``id(model)``/``id(mesh)``，
        而 Python 对象 id 会被回收复用（旧模型释放后新模型可能拿到同一地址），
        只按 id 比较会命中上一模型的陈旧网格+索引 → 画面显示旧模型或空白，
        切换画布模式（重建对象/上下文）才恢复。同一目标重复 pin 则保留缓存。
        """
        if model is self._reliefModel:
            self.update()
            return
        self._reliefModel = model
        self._resetReliefRenderCache()
        self.update()

    def _resetReliefRenderCache(self) -> None:
        """清空浮雕网格/GPU 缓冲/投影等全部渲染缓存（目标模型切换时调用）。"""
        self._reliefMesh = None
        self._reliefMeshKey = None
        self._meshSrcGrid = None
        self._meshSrcMeta = None
        self._glCache = None
        self._glCacheKey = None
        self._glMeshRef = None
        self._glMeshKey = None
        self._roiBoxSinceGl = None
        self._geoDirtySinceGl = False
        self._hnormDirty = False
        self._glBaseDirty = False
        self._glRoiPending = None
        self._triIdxCache = None
        self._triIdxCacheKey = None
        self._triIdxMeshRef = None
        self._reliefProj = None
        self._projKey = None
        self._projCache = None

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
    def _snapAxes(self):
        """捕捉平面轴：3D 模式随视图方向（前视 XZ / 右视 YZ / 其余 XY），
        侧视下吸附距离按屏幕平面度量，避免竖直方向误吸到 XY 投影重合点。
        浮雕等其余画布模式取点固定 XY 平面，不随视角切换。"""
        if self._canvasMode in ("3d", "cnc"):
            if self._mode == 2:
                return (0, 2)
            if self._mode == 3:
                return (1, 2)
        return (0, 1)

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
        # 传相机位置：顶视下圆柱上下两圆投影重合等纯投影歧义时，
        # 取离视线更近（看得见的）圆心
        res = self._snap.snap(worldPos, objs, worldR, ref,
                              axes=self._snapAxes(),
                              eye=self._camera()[0])
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

    def _resolvePoint(self, pos) -> Point3D | None:
        """命令取点：按画布模式分派（浮雕模式走 XY 平面，3D 模式支持 Z）。"""
        if self._canvasMode in ("3d", "cnc"):
            return self._resolvePoint3D(pos)
        return self._resolvePoint2D(pos)

    def _resolvePoint2D(self, pos) -> Point3D | None:
        """命令取点（浮雕模式）：先对象捕捉，未命中再按网格吸附（与 2D 画布一致）。"""
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

    def _resolvePoint3D(self, pos) -> Point3D | None:
        """命令取点（3D 模式）：Z 无限制。

        * 透视/顶视：取点落在"Z 高度"构造平面（z = drawZ）上；
        * 前视/右视：直接在 XZ/YZ 侧视平面取点，Z 由鼠标位置确定；
        * 对象捕捉优先：捕捉到 3D 曲线端点/节点时带其真实 Z；
        * 网格吸附仅在透视/顶视启用（吸附 XY，Z 取构造平面高度）。
        """
        wp = self._planePoint(pos.x(), pos.y())
        if wp is None:
            return None
        if self._cm is not None and self._cm.hasActive():
            inst = self._cm.activeInstance()
            if inst is not None and not inst.showSnapHints():
                self._lastSnap = None
                self._lastResolved = wp
                return wp
        res = self._snapAt(wp)
        if res.isValid:
            self._lastSnap = res
            p = res.position
            self._lastResolved = Point3D(p.x, p.y, p.z)
            return self._lastResolved
        self._lastSnap = None
        if self._mode in (0, 1) and self._grid.config().snapEnabled:
            gx, gy = self._grid.snapToGrid((wp.x, wp.y))
            self._lastResolved = Point3D(gx, gy, self._drawZ)
            return self._lastResolved
        self._lastResolved = wp
        return wp

    def _planePoint(self, mx: float, my: float) -> Point3D | None:
        """屏幕坐标 → 3D 模式取点平面上的世界点。

        透视/顶视 → z = drawZ 的水平构造平面；前视 → XZ 平面(y=0)；
        右视 → YZ 平面(x=0)。侧视平面取点 Z 随鼠标（3D 曲线 Z 无限制）。
        """
        w, h = float(self.width()), float(self.height())
        if w < 2 or h < 2:
            return None
        eye, _right, _up, fwd, ortho = self._camera()
        cx, cy = w / 2.0, h / 2.0
        if self._mode == 2:      # 前视：XZ 平面（y=0），Z 由屏幕纵向确定
            scale = self._orthoScale(w, h)
            if scale < 1e-12:
                return None
            return Point3D(eye[0] + (mx - cx) / scale, 0.0,
                           eye[2] - (my - cy) / scale)
        if self._mode == 3:      # 右视：YZ 平面（x=0），Z 由屏幕纵向确定
            scale = self._orthoScale(w, h)
            if scale < 1e-12:
                return None
            return Point3D(0.0, eye[1] + (mx - cx) / scale,
                           eye[2] - (my - cy) / scale)
        if ortho:                # 顶视：水平构造平面
            scale = self._orthoScale(w, h)
            if scale < 1e-12:
                return None
            return Point3D(eye[0] + (mx - cx) / scale,
                           eye[1] - (my - cy) / scale, self._drawZ)
        # 透视：屏幕坐标反投影成射线，与构造平面 z = drawZ 求交
        focal = self._focal(h)
        if focal < 1e-9:
            return None
        sx = (mx - cx) / focal          # 相机空间 x/depth
        sy = (cy - my) / focal          # 相机空间 y/depth（屏幕 y 向下）
        ray = (fwd[0] + sx * _right[0] + sy * _up[0],
               fwd[1] + sx * _right[1] + sy * _up[1],
               fwd[2] + sx * _right[2] + sy * _up[2])
        if abs(ray[2]) < 1e-9:
            return None                  # 视线平行于构造平面
        depth = (self._drawZ - eye[2]) / ray[2]
        if depth < 0:
            return None                  # 构造平面在相机后面
        return Point3D(eye[0] + depth * ray[0],
                       eye[1] + depth * ray[1],
                       self._drawZ)

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
                   self._targetZ + self._dist * direction[2])
            target = (px, py, self._targetZ)
            fwd = _norm(_sub(target, eye))
            up = (0.0, 0.0, 1.0)
            if abs(fwd[2]) > 0.999:
                up = (0.0, 1.0, 0.0)
            right = _norm(_cross(fwd, up))
            up = _norm(_cross(right, fwd))
            return eye, right, up, fwd, False
        if self._mode == 1:      # 顶视：从 +Z 看
            # 正交眼距 = _dist + 1000：缩放只改 _orthoScale（由 _dist 反解），
            # 眼始终在场景上方 1m 外。原先眼高=_dist——滚轮放大把眼一路压
            # 进模型内部（fit 后 _dist 可小于模型高），整屏实体填充、模型
            # "消失"（用户实测：缩放一下模型就看不见了）。
            return (px, py, self._dist + 1000.0), (1, 0, 0), (0, 1, 0), \
                (0, 0, -1), True
        if self._mode == 2:      # 前视：从 +Y 看
            return (px, self._dist + 1000.0, py), (1, 0, 0), (0, 0, 1), \
                (0, -1, 0), True
        return (self._dist + 1000.0, px, py), (0, 1, 0), (0, 0, 1), \
            (-1, 0, 0), True


    def _orthoScale(self, w: float, h: float) -> float:
        return (min(w, h) * 0.45 / GRID_RANGE) * (DEFAULT_DIST / self._dist)

    def _focal(self, h: float) -> float:
        return (h / 2.0) / math.tan(math.radians(FOV_DEG) / 2.0)

    def _sceneRadius(self) -> float:
        """当前场景近似包围球半径：网格半径与浮雕模型/毛坯尺寸取较大者。"""
        r = GRID_RANGE
        try:
            model = self._reliefModel
            if model is None and self._doc is not None:
                model = self._doc.activeReliefModel()
            if model is not None:
                bb = model.getBoundingBox()
                rr = 0.5 * math.hypot(bb.max.x - bb.min.x,
                                      bb.max.y - bb.min.y,
                                      bb.max.z - bb.min.z)
                if rr > 1e-9:
                    r = max(r, rr)
        except Exception:
            pass
        try:
            from core.cnc.state import CncState
            st = CncState.instance()
            r = max(r, 0.65 * max(float(st.stock.width),
                                  float(st.stock.height),
                                  float(st.stock.thickness)))
        except Exception:
            pass
        return r

    def _distLimits(self):
        """滚轮缩放距离上下限，随场景尺寸自适应：小模型可贴近细看（下限
        保持在透视近平面 NEAR 之外，避免软件投影剔除），大毛坯可拉远。

        下限再按内容包围盒收紧（用户实测"缩放一下模型就消失"的根因）：
        - 正交视野宽 ≈ 0.8·_dist——深放大后视野只剩模型表面一小块平坦
          区域，看起来就是"模型没了/全是同色"；下限 = 对角线 5% 封顶
          约 20 倍放大。
        - 透视眼 = target + _dist·方向，_dist 小过模型尺寸时眼钻进模型
          内部，近裁剪面把表面整块剔除；下限 = 眼到包围盒出射面 + 边距，
          "缩到模型表面附近即停"。"""
        r = self._sceneRadius()
        lo = max(1.5, r * 0.01)
        hi = max(2000.0, r * 40.0)
        try:
            bb = self._contentBounds()
            if bb is not None and bb.isValid():
                diag = math.sqrt(bb.width() ** 2 + bb.height() ** 2
                                 + bb.depth() ** 2)
                if diag > 1e-9:
                    lo = max(lo, diag * 0.05)
                    t_exit = self._boundsExitDist(bb)
                    if t_exit is not None:
                        lo = max(lo, t_exit + diag * 0.05)
        except Exception:
            pass
        return lo, hi

    def _contentBounds(self):
        """当前空间实体（B-Rep/网格 STL）联合包围盒（0.5s 缓存；无实体
        返回 None）。"""
        import time as _t
        now = _t.monotonic()
        c = getattr(self, "_cbCache", None)
        if c is not None and (now - c[0]) < 0.5:
            return c[1]
        bb = None
        if self._doc is not None:
            for obj in self._doc.objects():
                if not isinstance(obj, BRepObject):
                    continue
                try:
                    b = obj.getBoundingBox()
                except Exception:
                    continue
                if not b.isValid():
                    continue
                if bb is None:
                    bb = b
                else:
                    bb = BoundingBox3D(
                        Point3D(min(bb.min.x, b.min.x),
                                min(bb.min.y, b.min.y),
                                min(bb.min.z, b.min.z)),
                        Point3D(max(bb.max.x, b.max.x),
                                max(bb.max.y, b.max.y),
                                max(bb.max.z, b.max.z)))
        self._cbCache = (now, bb)
        return bb

    def _boundsExitDist(self, bb):
        """透视模式下，眼射线（target→眼方向）离开内容包围盒的距离。

        目标点不在盒内且射线不穿盒返回 None（眼不会进盒）。dist 小于该
        值时眼在盒内（模型内部）。"""
        if self._mode != 0:
            return None
        ox, oy, oz = self._pan[0], self._pan[1], self._targetZ
        cp, sp = math.cos(self._pitch), math.sin(self._pitch)
        d = (cp * math.sin(self._yaw), cp * math.cos(self._yaw), sp)
        t_enter, t_exit = 0.0, math.inf
        for o, lo_, hi_, dv in ((ox, bb.min.x, bb.max.x, d[0]),
                                (oy, bb.min.y, bb.max.y, d[1]),
                                (oz, bb.min.z, bb.max.z, d[2])):
            if abs(dv) < 1e-12:
                if o < lo_ or o > hi_:
                    return None
                continue
            t1 = (lo_ - o) / dv
            t2 = (hi_ - o) / dv
            t_enter = max(t_enter, min(t1, t2))
            t_exit = min(t_exit, max(t1, t2))
        if t_exit < t_enter or t_exit <= 0.0:
            return None
        return max(t_exit, 0.0)

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

    def _syncGLContext(self):
        """把 ModernGL 资源与 Qt 的 GL 上下文生命周期对齐（3D/浮雕共用）。

        QOpenGLWidget 可能在运行时销毁并重建 GL 上下文；ModernGL 缓存的
        ctx/program/VAO/FBO 都属于旧上下文，跨上下文继续使用是未定义行为
        （回读粉色 #FF00FF/黑帧，甚至 render 访问违例）。每次 paintGL 先
        在此检测：上下文失效 → 释放并置空 _gl；上下文 id 变化 → 释放并重建
        GLSurface，保证后续所有 GL 调用都发生在 Qt 当前上下文——进程内只
        存在 Qt 这一个 GL 上下文（曾用独立 standalone 上下文，与 QOpenGLWidget
        上下文在同一线程互踩 current/FBO 状态，是粉色花帧的根源）。

        返回 (qctx, qid, qvalid, fbo)；self._curFbo 亦在此更新。
        """
        qctx = self.context()
        qid = id(qctx) if qctx is not None else None
        qvalid = bool(qctx.isValid()) if qctx is not None else False
        try:
            fbo = int(self.defaultFramebufferObject())
        except Exception:
            fbo = 0
        self._curFbo = fbo
        if self._gl is not None:
            if not qvalid:
                dbg("_syncGLContext: Qt GL context INVALID -> drop ModernGL")
                try:
                    self._gl.release()
                except Exception:
                    pass
                self._gl = None
            else:
                prev = getattr(self, "_glCtxId", None)
                if prev is not None and qid != prev:
                    dbg(f"_syncGLContext: Qt GL context CHANGED "
                        f"{prev}->{qid} -> recreate ModernGL")
                    try:
                        self._gl.release()
                    except Exception:
                        pass
                    self._gl = GLSurface()
                    # 旧上下文内建的 GPU 缓冲/网格缓存全部失效：下帧重建
                    self._reliefMesh = None
                    self._reliefMeshKey = None
                    self._glCache = None
                    self._glCacheKey = None
                    self._glMeshRef = None
                    self._glMeshKey = None
                    self._roiBoxSinceGl = None
                    self._geoDirtySinceGl = False
                    self._hnormDirty = False
                    self._glBaseDirty = False
                    self._glRoiPending = None
                self._glCtxId = qid
        if self._gl is None and qvalid and app_settings.gpu_accelerated:
            # Qt 可能晚于首次 paint 才回调 initializeGL（3D 视口动态创建/切换
            # tab 时尤其常见）：提前惰性建好 GLSurface 对象，首次 paint 即能
            # ensure 创建上下文，避免数十帧空转全屏 CPU 软渲染——那会让用户
            # 看到实体"消失/卡顿/闪烁"（观测：GPU 上下文迟 4 秒才就绪时，
            # 前 50+ 帧每帧都走 CPU z-buffer，画面一帧一帧蹦）。
            dbg("_syncGLContext: 惰性初始化 GLSurface (initializeGL 尚未回调)")
            self._gl = GLSurface()
        return qctx, qid, qvalid, fbo

    def _syncQtFboTracking(self) -> None:
        """把 moderngl 的帧缓冲跟踪对齐到 Qt 当前内部 FBO（模式隔离关键一环）。

        moderngl 的 ``Context.clear()`` 作用在它原生跟踪的 ``mglo.fbo`` 上，
        而 QOpenGLWidget 呈现的是自己的内部 FBO（``_syncGLContext`` 捕获到
        ``_curFbo``）。GLSurface 的 ``depth_brep``/``render_brep`` 离场时执行
        ``ctx.screen.use()``——moderngl 的 screen 恒为 FBO 0（真实默认帧缓冲，
        见 moderngl create_context：``_screen = detect_framebuffer(0)``），
        与 Qt 内部 FBO 不是同一目标；跟踪被留在 FBO 0 后，浮雕路径
        ``frame()``/``draw()`` 的清屏落进不可见的默认帧缓冲，Qt 呈现的内部
        FBO 残留上一模式画面——实测"3D 挤出实体后切浮雕，画布仍是 3D 那一帧"
        （圆柱残留；FBO 新建未初始化时即整屏黑）。同一机制也会让 3D 模式
        depth_brep 之后的 QPainter 叠加层（曲线/操作轴/坐标轴）画丢。

        每次 paintGL 入口（以及离场渲染之后）重绑 Qt FBO 并让 moderngl
        重新检测包装，"真实绑定"与"moderngl 跟踪"两个概念始终一致。
        """
        fbo = int(getattr(self, "_curFbo", 0) or 0)
        if not fbo or self._gl is None or self._gl.ctx is None:
            return
        try:
            ref = getattr(self._gl, "_qtFboRef", None)
            if ref is None or ref.glo != fbo:
                # detect_framebuffer() 包装的是"当前绑定"：先按 id 绑定再检测
                glf = QOpenGLFunctions_3_3_Compatibility(self.context())
                glf.initializeOpenGLFunctions()
                glf.glBindFramebuffer(0x8CA6, fbo)   # GL_FRAMEBUFFER
                ref = self._gl.ctx.detect_framebuffer()
                self._gl._qtFboRef = ref
            # use() = 绑定 + moderngl 原生跟踪对齐；每帧执行，把上一模式
            # （depth_brep/render_brep 的 ctx.screen.use()）泄漏的 FBO 0 改回来
            ref.use()
        except Exception:
            pass

    def paintGL(self) -> None:
        w, h = float(self.width()), float(self.height())
        dpr = self.devicePixelRatioF()
        W = max(1, int(round(w * dpr)))
        H = max(1, int(round(h * dpr)))
        # ModernGL 资源与 Qt GL 上下文生命周期同步（3D 与浮雕共用）：进程内
        # 只允许存在 Qt 的这一个 GL 上下文——独立 standalone 上下文与 Qt 上下文
        # 在同一线程互相 makeCurrent、FBO 对象跨上下文使用即未定义行为，回读全
        # 为垃圾内容（粉色 #FF00FF/黑帧）。Qt 重建上下文时这里统一重建 GLSurface。
        _qctx, _qid, _qvalid, fbo = self._syncGLContext()
        # moderngl 帧缓冲跟踪对齐 Qt 内部 FBO：3D 路径 depth_brep/render_brep
        # 离场会把跟踪留在 FBO 0（ctx.screen.use()），不修复则浮雕路径的清屏
        # 落进不可见默认帧缓冲、Qt 呈现残留的上一模式画面（黑屏/花屏根源）
        self._syncQtFboTracking()
        # 3D 画布模式：纯 QPainter 合成路径——B-Rep 表面经**同一 Qt GL 上下文**
        # 离屏渲染成图像（GPU 光栅化 + 深度测试）再叠加网格/曲线/标注。GL 调用
        # 包在 beginNativePainting 括号内，不污染 QPainter 画笔引擎的 GL 状态。
        if self._canvasMode in ("3d", "cnc"):
            self._paint3D(w, h, W, H)
            return
        # 无 GPU：设置关闭、或上下文失效已被 _syncGLContext 置空 _gl → 整帧软渲染
        if self._gl is None:
            self._paintSoftware(w, h)
            return
        # Qt 上下文失效/重建的 ModernGL 同步已由 paintGL 开头的 _syncGLContext()
        # 完成（失效 → 置空 _gl；id 变化 → 释放并重建 GLSurface）。
        # 1) GPU 清屏 + 浮雕曲面：必须包在 Qt 的 beginNativePainting/endNativePainting
        #    之间——QOpenGLWidget 混用原生 GL(ModernGL) 与 QPainter 时，只有用这对括号
        #    隔离，Qt 画笔引擎才不会与 ModernGL 争用/污染 GL 状态；否则首帧 ModernGL
        #    画完后 Qt 引擎接管，次帧复用 VAO 时 vao.render() 会访问违例闪退。
        painter = QPainter(self)
        painter.beginNativePainting()
        self._reliefDrawn = False   # 本帧 GPU 浮雕是否成功绘制
        try:
            self._gl.ensure()
            cam = self._camera()
            model = self._reliefTarget() if w >= 2 and h >= 2 else None
            if self._canvasMode == "cnc":
                self._cncDbg("paintGL target=%s" % ("Y" if model is not None else "N"))
            if model is not None:
                self._drawReliefSurfaceGL(cam, w, h, W, H)
            else:
                self._gl.frame(W, H, self._bgFloat, fbo)
        except Exception:
            import traceback
            traceback.print_exc()
            dbg("paintGL: GL exception -> software fallback this frame\n"
                + traceback.format_exc())
            try:
                painter.endNativePainting()
                painter.end()
            except Exception:
                pass
            # 瞬态容错：单次 GL 异常只回退本帧（软渲染兜底保证画面完整），
            # 下一帧重试 GL——偶发驱动抖动不再把渲染器永久打成软路径
            #（外观完全不同，正是"偶尔闪成另一副样子"的来源）。连续失败
            # 超上限才判定 GL 环境不可用，粘滞到软渲染。
            self._glFailCount = getattr(self, "_glFailCount", 0) + 1
            if self._glFailCount >= 30:
                dbg("paintGL: GL 连续失败 30 帧 -> 永久软渲染路径")
                self._gl = None
            self._paintSoftware(w, h)
            return
        painter.endNativePainting()
        self._glFailCount = 0
        # 2) QPainter 叠加层（对象/操作轴/命令预览/捕捉/曲面边框/选择框）
        # QOpenGLWidget(NoPartialUpdate) 下，paint 中途抛异常会让**未画完的
        # 帧以未初始化显存内容直接呈现**——偶发一帧"深蓝色画面"。故叠加层
        # 整段兜底：某个叠加层本帧异常就跳过它（下一帧自动恢复）并落日志，
        # painter 保证正确 end，绝不呈现半成品帧。
        painter.setRenderHint(QPainter.Antialiasing, True)
        try:
            if w >= 2 and h >= 2:
                if not self._hideReferenceGrid:
                    self._drawGround(painter, cam, w, h)
                    self._drawGrid(painter, cam, w, h)
                    self._drawAxes(painter, cam, w, h)
                # CNC 模式浮雕模型兜底：GPU 未成功时依次尝试软渲染实体、
                # 线框投影，各自独立 try（任何一层异常不阻断下一层）；
                # B-Rep 表面：Qt 上下文内离屏 GPU 渲染 + QPainter 叠加；
                # 同时得到隐藏线深度图。
                # 透视按钮只对渲染模式作用：渲染+透视关=隐藏背面线
                # （深度图透视校正，任意视角正确），透视开=显示背面线
                # （表面半透明）；线框模式固定全部显示。
                qimg, _unused, _why = self._renderBrepGLData(
                    cam, w, h, W, H, painter)
                depthBuf = None
                if qimg is not None:
                    painter.drawImage(QRectF(0, 0, w, h), qimg)
                    if not self._solidPerspective:
                        depthBuf = getattr(self, "_zbFrame", None)
                elif self._canvasMode == "cnc" and self._solidSimActive():
                    # GPU 帧缺失（bad_empty/sticky/error）：实体模拟的体素坯料
                    # 走软 z-buffer 光栅化兜底，保证"实体"勾选后一定可见
                    try:
                        self._paintBrepZBuffer(painter, cam, w, h)
                    except Exception:
                        import traceback
                        traceback.print_exc()
                        dbg("voxel soft fallback err\n" + traceback.format_exc())
                if self._doc is not None:
                    self._drawObjects(painter, cam, w, h, depthBuf)
                    if self._canvasMode == "cnc":
                        self._cncDbg("overlay cnc drawn=%d"
                                     % (1 if self._reliefDrawn else 0))
                        # CNC 模型：曲线同一 QPainter 投影机制，无条件画线框，
                        # 保证与曲线一样一定可见；GPU/软渲染实体失败才显示诊断。
                        if not self._reliefDrawn:
                            try:
                                self._drawReliefSurfaceSoftware(painter, cam, w, h)
                            except Exception:
                                import traceback
                                traceback.print_exc()
                                self._cncDiagNote = "soft err"
                        try:
                            self._drawCncModelWire(painter, cam, w, h)
                        except Exception:
                            import traceback
                            traceback.print_exc()
                            self._cncDiagNote = "wire err"
                        self._drawSolidStockFrame(painter, cam, w, h)
                    self._drawSubSelection(painter, cam, w, h)
                self._drawReliefBorderNow(painter, cam, w, h)
                self._drawCommandPreview(painter, cam, w, h)
                self._paintSnapMarker(painter, cam, w, h)
                if self._curveEdit is not None and self._curveEditAdapter is not None:
                    self._curveEdit.paint(painter, self._curveEditAdapter)
                if self._gizmo is not None and self._gizmoAdapter is not None:
                    self._gizmo.paint(painter, self._gizmoAdapter)
                if self._boxActive and not self._boxRect.isNull():
                    self._paintSelectBox(painter)
        except Exception:
            import traceback
            traceback.print_exc()
            dbg("paintGL: overlay exception -> frame kept without that layer\n"
                + traceback.format_exc())
            if self._canvasMode == "cnc":
                try:
                    painter.save()
                    painter.setPen(QColor(235, 70, 70, 255))
                    last = traceback.format_exc(limit=2).strip().splitlines()
                    msg = "CNC overlay err: " + (last[-1] if last else "?")
                    painter.drawText(QRectF(12, 10, w - 24, 60), msg)
                    painter.restore()
                except Exception:
                    pass
        finally:
            painter.end()

    def _solidLight(self, cam):
        """实体灯光方向：世界坐标光（方位/仰角由变换面板滑块控制）。

        默认方位 0（正北=顶视图画面正上方）/仰角 30°——顶视图光从画面
        上方斜射，坡面两侧一明一暗，高度差直接显形为阴影；透视同光，
        旋转视口即可改变受光角。原先随视角头灯浅纹对比度仅 2-5%。"""
        return mesh3d.lightDirection(self._solidLightAz,
                                     self._solidLightAlt)

    def _paint3D(self, w: float, h: float, W: int, H: int) -> None:
        """3D 画布模式（B-Rep 实体建模）主绘制。

        合成顺序：背景/网格（QPainter）→ B-Rep 表面 + 特征边（GPU 直绘，
        ModernGL 深度测试，隐藏线天然消除）→ 曲线/标注/UI 叠加（QPainter）。
        透视开关（_solidPerspective）：True=X-Ray（表面半透明、背面线可见、
        曲线透过模型显示）；False=实体不透明，边线深度测试隐藏背面线，
        曲线被实体遮挡的部分不绘制（depth_brep 视距图隐藏线消除）。
        线框模式只画边线（无表面，曲线全部可见）。
        GL 不可用（无 GPU / 离屏测试）时回退软件 z-buffer 光栅化（原逻辑）。
        """
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(theme.BG_VIEWPORT))
        if w < 2 or h < 2:
            painter.end()
            return
        self._reliefDrawn = False   # CNC 模型是否已由 B-Rep 管线渲染（本帧）
        cam = self._camera()
        if self._gl is None:
            # ---- 无 GPU：软件 z-buffer 路径（离屏测试 / 无 GPU 机器） ----
            try:
                if not self._hideReferenceGrid:
                    self._drawGround(painter, cam, w, h)
                    self._drawGrid(painter, cam, w, h)
                depthBuf = None
                if self._doc is not None:
                    if self._canvasMode == "cnc":
                        self._drawCncModelLayer(painter, cam, w, h)
                    if self._solidDisplayMode == "rendered":
                        self._paintBrepZBuffer(painter, cam, w, h)
                        if not self._solidPerspective:
                            depthBuf = getattr(self, "_zbFrame", None)
                    self._drawObjects(painter, cam, w, h, depthBuf)
                    self._drawSubSelection(painter, cam, w, h)
                self._drawCncToolpaths(painter, cam, w, h)
                self._drawCncTools(painter, cam, w, h)
                self._drawSolidStockFrame(painter, cam, w, h)
                self._drawCncSimCursor(painter, cam, w, h)
                self._drawCommandPreview(painter, cam, w, h)
                self._paintSnapMarker(painter, cam, w, h)
                if self._curveEdit is not None and self._curveEditAdapter is not None:
                    self._curveEdit.paint(painter, self._curveEditAdapter)
                if self._gizmo is not None and self._gizmoAdapter is not None:
                    self._gizmo.paint(painter, self._gizmoAdapter)
                if self._boxActive and not self._boxRect.isNull():
                    self._paintSelectBox(painter)
                if not self._hideReferenceGrid:
                    self._drawAxes(painter, cam, w, h, depthBuf)
            except Exception:
                import traceback
                traceback.print_exc()
                dbg("_paint3D(soft): overlay exception\n"
                    + traceback.format_exc())
            finally:
                painter.end()
            return
        # ---- GPU 主路径 ----
        try:
            # 1) QPainter：背景/网格（轴在叠加层最后画，避免被实体遮挡）
            if not self._hideReferenceGrid:
                self._drawGround(painter, cam, w, h)
                self._drawGrid(painter, cam, w, h)
            # 2) 原生 GL：B-Rep 表面 + 特征边（GPU 深度测试）
            painter.beginNativePainting()
            try:
                self._gl.ensure()
                ents = self._brepGLEntries()
                mvp = self._buildMVP(cam, w, h, W, H)
                eye, right, up, fwd, ortho = cam
                # 实体光照：正交=屏幕上方 30° 掠射（顶视高度差显形为
                # 明暗阴影），透视=固定世界光 315°/35°（旋转改受光角）。
                # 原先随视角头灯浅纹对比度仅 2-5%（详见 _solidLight）。
                light = self._solidLight(cam)
                if self._solidDisplayMode == "rendered":
                    if ents:
                        # 画面前只清深度位（原生 GL，绝不清颜色），否则
                        # QPainter 刚画好的白背景会被深度残留旧值挡住的
                        # 三角形剔除；moderngl ctx.clear 会连带清颜色成黑。
                        try:
                            _glf0 = QOpenGLFunctions_3_3_Compatibility(
                                self.context())
                            _glf0.initializeOpenGLFunctions()
                            _glf0.glClear(0x00000100)  # GL_DEPTH_BUFFER_BIT
                        except Exception:
                            pass
                        # 普通实体与预览实体分组绘制：预览体加
                        # GL_POLYGON_OFFSET 负偏移，浮在原面上，
                        # 消除推拉预览与原实体共面的 z-fighting 条纹
                        _norm = [e for e in ents if len(e) < 5 or not e[4]]
                        _prev = [e for e in ents if len(e) >= 5 and e[4]]
                        self._gl.draw_brep(_norm, mvp, eye, light, W, H)
                        if _prev:
                            try:
                                _glf0.glEnable(0x8037)  # GL_POLYGON_OFFSET_FILL
                                _glf0.glPolygonOffset(-1.0, -1.0)
                            except Exception:
                                pass
                            self._gl.draw_brep(_prev, mvp, eye, light, W, H)
                            try:
                                _glf0.glDisable(0x8037)
                                _glf0.glPolygonOffset(0.0, 0.0)
                            except Exception:
                                pass
                # 实体模拟：高度场坯料（纹理位移 + 片元着色专用管线）
                if self._solidSimActive():
                    try:
                        self._drawCncStockGL(mvp, eye, light, W, H)
                    except Exception:
                        import traceback
                        traceback.print_exc()
                # 曲线/点/实体线框的隐藏线消除深度图：仅渲染模式且透视关时
                # 生成（透视开 = X-Ray，曲线透过模型可见）。depth-only 离屏
                # 渲染开销小；视距图约定与 _renderBrepGLData 的 _zbFrame
                # 一致：像素值 = 沿视线到最近表面的世界距离。
                depthBuf = None
                if (self._solidDisplayMode == "rendered"
                        and not self._solidPerspective and ents):
                    try:
                        # near/far 必须与 _buildMVP 的投影一致
                        # （正交 near=0.1 / 透视 near=NEAR），换算的视距才准
                        eye_depth = self._gl.depth_brep(
                            ents, mvp, W, H, ortho,
                            near=(-1100.0 if ortho else NEAR),
                            far=self._dist + 6000.0)
                        depthBuf = (eye_depth, W, H)
                        self._zbScale = (W / w) if w >= 2 else 1.0
                    except Exception:
                        import traceback
                        traceback.print_exc()
                # depth_brep 离场时 ctx.screen.use() 把绑定/跟踪留在 FBO 0：
                # 先对齐回 Qt 内部 FBO，否则下面的 QPainter 叠加层（曲线/
                # 操作轴/坐标轴/选择框）画进不可见的默认帧缓冲而丢失
                self._syncQtFboTracking()
                edges = self._brepGLEdges(cam, w, h)
                if edges:
                    self._gl.draw_brep_edges(edges, mvp, W, H)
                # 交还 QPainter：关深度/混合并只清深度（不清颜色），
                # 叠加层不被 GL 深度剔除
                _c = self._gl.ctx
                _c.disable(_c.DEPTH_TEST)
                _c.disable(_c.BLEND)
                try:
                    _glf = QOpenGLFunctions_3_3_Compatibility(self.context())
                    _glf.initializeOpenGLFunctions()
                    _glf.glClear(0x00000100)   # GL_DEPTH_BUFFER_BIT
                except Exception:
                    pass
            finally:
                painter.endNativePainting()
            # 3) QPainter 叠加：CNC 模型层 + 曲线/点/标注/捕捉/选择框
            #    （depthBuf 非空时曲线被实体遮挡的部分不绘制）
            if self._doc is not None:
                if self._canvasMode == "cnc":
                    self._drawCncModelLayer(painter, cam, w, h)
                self._drawObjects(painter, cam, w, h, depthBuf,
                                  glBrepEdges=True)
                self._drawSubSelection(painter, cam, w, h)
            self._drawCncToolpaths(painter, cam, w, h,
                                   self._gl, mvp, W, H)
            self._drawCncTools(painter, cam, w, h)
            self._drawCncSimCursor(painter, cam, w, h)
            self._drawCommandPreview(painter, cam, w, h)
            self._paintSnapMarker(painter, cam, w, h)
            if self._curveEdit is not None and self._curveEditAdapter is not None:
                self._curveEdit.paint(painter, self._curveEditAdapter)
            if self._gizmo is not None and self._gizmoAdapter is not None:
                self._gizmo.paint(painter, self._gizmoAdapter)
            if self._boxActive and not self._boxRect.isNull():
                self._paintSelectBox(painter)
            if not self._hideReferenceGrid:
                self._drawAxes(painter, cam, w, h, depthBuf)
        except Exception:
            import traceback
            traceback.print_exc()
            dbg("_paint3D: GL exception -> software fallback this frame\n"
                + traceback.format_exc())
            self._glFailCount = getattr(self, "_glFailCount", 0) + 1
            if self._glFailCount >= 30:
                dbg("_paint3D: GL 连续失败 -> 永久软件渲染")
                self._gl = None
            self._paintSoftware(w, h)
            return
        self._glFailCount = 0
        painter.end()

    def _cncReliefMeshData(self, mesh):
        """浮雕网格 → (展开三角 (M,3), 顶点法线 (V,3))。

        面法线按共享顶点累加平均得平滑顶点法线（Gouraud），四边形每面拆 2 三角。
        """
        faces = mesh.faces
        a = faces[:, 0]; b = faces[:, 1]
        c = faces[:, 2]; d = faces[:, 3]
        tris = np.stack([a, b, c, a, c, d], axis=1).reshape(-1, 3)
        V = mesh.verts.shape[0]
        fn = np.asarray(mesh.normals, dtype=np.float64)
        vp = np.zeros((V, 3), dtype=np.float64)
        vc = np.zeros(V, dtype=np.float64)
        np.add.at(vp, faces.reshape(-1), np.repeat(fn, 4, axis=0))
        np.add.at(vc, faces.reshape(-1), 1.0)
        vn = vp / np.maximum(vc, 1e-9)[:, None]
        nrm = np.linalg.norm(vn, axis=1, keepdims=True)
        vn = vn / np.maximum(nrm, 1e-9)
        return tris, vn

    def _cncReliefBrepEntry(self):
        """CNC 浮雕模型转 B-Rep 渲染数据（与毛坯同管线，保证实体显示）。

        GPU 条目 ``(pos, nrm, base, alpha)``；软渲染用 ``(mesh_dict, base, alpha)``。
        返回 None 表示无 CNC 模型或网格不可用。
        """
        if self._canvasMode != "cnc":
            return None
        model = self._cncReliefModelForDraw()[0]
        if model is None:
            return None
        mesh = self._reliefMeshFor(model, max_dim=self._gpuMeshDim())
        if mesh is None or mesh.faceCount == 0:
            return None
        tris, vn = self._cncReliefMeshData(mesh)
        pos = np.ascontiguousarray(
            mesh.verts[tris].reshape(-1, 3), dtype=np.float32)
        nrm = np.ascontiguousarray(vn[tris].reshape(-1, 3), dtype=np.float32)
        # 逐顶点底色（[0,1]）：map=高度地形色带（凹凸一眼可见）、
        # color=用户色、material=程序化材质底——与浮雕模式显示一致
        base = self._baseColorsVert(mesh, model)
        baseE = np.ascontiguousarray(
            np.asarray(base, dtype=np.float64)[tris].reshape(-1, 3),
            dtype=np.float32)
        self._reliefDrawn = True    # B-Rep 管线将渲染模型（叠加层不再补画）
        return (pos, nrm, baseE, 255)

    def _isMeshReliefSolid(self, obj) -> bool:
        """网格 STL 实体（无解析曲面的导入网格）：高度着色作用对象。

        BrepShape 纯网格（3D 模式直接导入 STL）或网格快照恢复的
        PolySolid（保存→重开）。挤出/STEP 等真 B-Rep 实体返回 False。
        """
        ps = obj.shape()
        if isinstance(ps, BrepShape):
            return bool(getattr(ps, "_meshOnly", False))
        return bool(getattr(obj, "_fromMesh", False))

    def _meshTerrainBase(self, mesh) -> np.ndarray:
        """网格 STL 实体 → 逐顶点地形色带（高度编码，三角形展开）。

        返回 (3N,3) float32 [0,1]，与 pos/nrm 逐顶点对齐（draw_brep 支持
        逐顶点色）。仅在用户打开"高度色"开关时使用。
        """
        from core.relief import shade
        lut = shade.make_lut("terrain")
        z = mesh["verts"][:, 2]
        lo, hi = float(z.min()), float(z.max())
        vh = np.full(z.shape, 0.5) if hi - lo < 1e-9 else (z - lo) / (hi - lo)
        vi = np.clip((vh * 255.0).astype(np.int32), 0, 255)
        cv = lut[vi].astype(np.float32) / 255.0          # (V,3)
        tris = np.asarray(mesh["tris"]).reshape(-1)
        return np.ascontiguousarray(cv[tris], dtype=np.float32)

    def _brepGLEntries(self):
        """GL B-Rep 面数据：[(pos (N,3) f4, nrm (N,3) f4, base(3,) 0-255, alpha), ...]。

        面顶点=三角形展开（每三角形 3 个独立顶点，面间硬边自然呈现）；
        X-Ray 半透明 140，实体不透明 255；命令预览实体青色 130。"""
        if self._doc is None:
            return []
        solid_sim = self._solidSimActive()
        ents = []
        for obj in self._doc.visibleObjects():
            if solid_sim and self._isStockObject(obj):
                continue   # 实体模拟：毛坯由体素坯料网格呈现，跳过半透明盒
            if not isinstance(obj, BRepObject) or obj.shape() is None:
                continue   # 锁定只挡拾取/编辑，不影响渲染可见性
            mesh = obj.mesh(0.05)
            if mesh is None or len(mesh["tris"]) == 0:
                continue
            pos = np.ascontiguousarray(
                mesh["verts"][mesh["tris"]].reshape(-1, 3), dtype=np.float32)
            nrm = np.ascontiguousarray(
                mesh["normals"][mesh["tris"]].reshape(-1, 3), dtype=np.float32)
            if self._meshHeightColor and self._isMeshReliefSolid(obj):
                base = self._meshTerrainBase(mesh)
            elif obj.isSelected():
                col = QColor(theme.selectionColorFor(obj))
                base = (col.red(), col.green(), col.blue())
            else:
                col = QColor(*obj.color())
                base = (col.red(), col.green(), col.blue())
            ents.append((pos, nrm, base,
                         140 if self._solidPerspective else 255))
        if self._cm is not None and self._cm.hasActive():
            inst = self._cm.activeInstance()
            mk = getattr(inst, "previewSolid", None)
            pobj = mk() if callable(mk) else None
            if isinstance(pobj, BRepObject):
                # 预览体用粗网格：拖动倒角/推拉实时重建时大幅提速
                # （应用后实体仍走 0.05 精网格）
                pm = pobj.mesh(0.18)
                if pm is not None and len(pm["tris"]) > 0:
                    pos = np.ascontiguousarray(
                        pm["verts"][pm["tris"]].reshape(-1, 3),
                        dtype=np.float32)
                    nrm = np.ascontiguousarray(
                        pm["normals"][pm["tris"]].reshape(-1, 3),
                        dtype=np.float32)
                    ents.append((pos, nrm, (21, 182, 201), 130, True))
        cncEnt = self._cncReliefBrepEntry()
        if cncEnt is not None:
            ents.append(cncEnt)
        # 实体模拟坯料不并入通用 ents：走 draw_stock 专用高度场管线
        # （纹理位移 + 片元着色，见 _drawCncStockGL）
        return ents

    def _brepGLEdges(self, cam, w: float, h: float):
        """GL B-Rep 特征边线段组：[(verts (N,3) f4, (r,g,b,a) 0-1, depth_test), ...]。

        rendered 实体模式：非选中不画（纯表面着色）；选中画特征边且深度
        测试（背面线隐藏）。X-Ray：全部边线、不测深度（背面线可见）。
        线框模式：全部边线、不测深度（无表面填充）。"""
        if self._doc is None:
            return []
        groups = []
        rendered = self._solidDisplayMode == "rendered"
        xray = bool(self._solidPerspective)
        solid_sim = self._solidSimActive()
        for obj in self._doc.visibleObjects():
            if not isinstance(obj, BRepObject) or obj.shape() is None:
                continue   # 锁定只挡拾取/编辑，不影响渲染可见性
            if solid_sim and self._isStockObject(obj):
                continue   # 实体模拟：毛坯盒边线隐藏（体素坯料替代）
            selected = obj.isSelected()
            if rendered and not selected:
                continue
            top = obj.topology(self._angularFor(obj, cam, w, h),
                               edges_only=True)
            polys = top.get("edgePolylines") or []
            segs = []
            for poly in polys:
                n = len(poly)
                for k in range(n - 1):
                    segs.append(poly[k])
                    segs.append(poly[k + 1])
            if not segs:
                continue
            hl = theme.selectionColorFor(obj) if selected else None
            col = QColor(hl) if hl else QColor(*obj.color())
            depth_test = rendered and not xray
            groups.append((np.ascontiguousarray(segs, dtype=np.float32),
                           (col.red() / 255.0, col.green() / 255.0,
                            col.blue() / 255.0, 1.0), depth_test))
        return groups


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
                if self._canvasMode == "cnc":
                    # CNC 模式：软渲染实体、线框兜底各自独立（异常不阻断）
                    try:
                        self._drawReliefSurfaceSoftware(painter, cam, w, h)
                    except Exception:
                        import traceback
                        traceback.print_exc()
                        self._cncDiagNote = "soft err"
                    if not self._reliefDrawn:
                        try:
                            self._drawCncModelWire(painter, cam, w, h)
                        except Exception:
                            import traceback
                            traceback.print_exc()
                            self._cncDiagNote = "wire err"
                else:
                    self._drawReliefSurfaceSoftware(painter, cam, w, h)
                depthBuf = None
                if (self._canvasMode in ("3d", "cnc")
                        and self._solidDisplayMode == "rendered"
                        and not self._solidPerspective):
                    depthBuf = self._buildDepthBuffer(cam, w, h)
                if self._doc is not None:
                    self._drawObjects(painter, cam, w, h, depthBuf)
                self._drawSubSelection(painter, cam, w, h)
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

    def _reliefModelsForCnc(self) -> list:
        """CNC 模式应渲染的浮雕模型：CNC 独立空间中导入的所有 SURFACE 对象。

        2D 导入到 CNC 的模型（克隆）落在这个空间；图层被隐藏的模型不显示。
        """
        models = []
        if self._doc is None:
            return models
        try:
            from core.vector.geometry_types import ObjectType
            for o in self._doc.objects():
                if o.type() == ObjectType.SURFACE:
                    layer = self._doc.layer(o.layerId())
                    if layer is not None and not layer.visible:
                        continue
                    models.append(o)
        except Exception:
            pass
        return models

    def _drawReliefSurfaceGL(self, cam, w: float, h: float, W: int, H: int) -> None:
        """GPU 路径：上传（缓存）网格顶点/法线/底色，单次 draw call 渲染。

        旋转期间网格 key 不变 → 只重传 MVP/相机 uniform；显示模式/材质/模型版本变化
        才重建顶点缓冲。深度测试做遮挡，片元 Phong 光照，无网格纹、丝滑。
        """
        model = self._reliefTarget()
        if model is None:
            return
        # ---- 网格/缓存重建（数据层）：失败沿用上一份缓存，绝不切换渲染路径 ----
        # 雕刻/磨光笔画期每次版本刷新都会走这里，若重建异常导致本帧走软渲染
        # 兜底、下一帧又回 GL——两条路径外观完全不同，逐帧交替就是"一直闪"
        # （软渲染观感偏透，即"模型变透明"）。故重建异常只记日志，本帧显示
        # 旧数据，下一帧数据更新自然追上。
        try:
            mesh = self._reliefMeshFor(model, max_dim=self._gpuMeshDim())
            if mesh is None or mesh.faceCount == 0:
                return
            # styleKey 只含**形状/外观**：网格实例、显示模式、材质、颜色、
            # 色号蒙版、网格尺寸。高度数据变化（雕刻/磨光笔画）不再使整份
            # 顶点法线/底色重建——旧实现每帧全场 bincount 散射（3000² 栅格
            # ~95ms）+ 地形 LUT 全场 gather（~25ms）是拖动塑形卡死的根因。
            styleKey = (id(mesh), model.display3DMode(), model.material(),
                        tuple(model.color() or ()), model.maskStamp(),
                        mesh.gnx, mesh.gny, mesh.verts.shape[0])
            if self._glCache is None or self._glCacheKey != styleKey:
                # 逐顶点底色（仅随模型/模式/材质变化）+ 逐顶点法线（面法线平均）
                base = self._baseColorsVert(mesh, model)
                vn = self._scatterWallAware(mesh, mesh.normals)
                nlen = np.linalg.norm(vn, axis=1, keepdims=True)
                vn = vn / np.maximum(nlen, 1e-12)
                # 四边形面 → 两个三角形索引（拓扑不变 → 按 (gnx,gny) 缓存复用）
                idx = self._triIndexFor(mesh)
                # 缓存即预转成 GPU 上传格式（f4/u4 连续）：每帧 draw 的上传
                # 零拷贝直通，也杜绝任何按 id 的转换缓存撞键错绑
                self._glCache = (
                    np.ascontiguousarray(mesh.verts, dtype=np.float32),
                    np.ascontiguousarray(vn, dtype=np.float32),
                    np.ascontiguousarray(base, dtype=np.float32),
                    idx,
                )
                self._glCacheKey = styleKey
                self._glMeshRef = mesh
                self._glMeshKey = self._reliefMeshKey
                self._roiBoxSinceGl = None
                self._geoDirtySinceGl = False
                self._glBaseDirty = False
            elif self._glMeshRef is mesh \
                    and self._glMeshKey != self._reliefMeshKey:
                # 同一网格、仅高度变化：只同步变化的 z / 法线 ROI / 底色 ROI
                self._syncGlMeshIncremental(mesh, model)
            elif self._glBaseDirty:
                # 笔画结束后的整份底色刷新（精确 min/max 归一化），法线不动
                verts, vn, base, idx = self._glCache
                self._glCache = (verts, vn,
                                 np.ascontiguousarray(
                                     self._baseColorsVert(mesh, model),
                                     dtype=np.float32), idx)
                self._glBaseDirty = False
        except Exception:
            import traceback
            traceback.print_exc()
            dbg("drawReliefSurfaceGL: rebuild failed -> keep last cache\n"
                + traceback.format_exc())
            # 只有缓存确实属于**当前**网格才允许沿用（数据略旧，下帧追上）。
            # 否则（从未渲染成功 / 缓存还是上一个模型的）本帧必须交外层软渲染
            # 兜底：软路径按当前模型数据绘制，画面至少正确，不会把上一模型的
            # 几何顶在新模型的位置上或画成空白。
            if (self._glCache is None or self._glCacheKey is None
                    or self._reliefMesh is None
                    or self._glMeshRef is not self._reliefMesh):
                raise
            mesh = self._reliefMesh
        verts, vn, base, idx = self._glCache
        eye = cam[0]
        light = mesh3d.lightDirection(model.lightAzimuth(), model.lightAltitude())
        params = self._lightParams(model.display3DMode(), model.material())
        mvp = self._buildMVP(cam, w, h, W, H)
        # 笔画期只有 ROI 块真正变化：交给 GL 层按行块局部 write（KB 级），
        # 其余顶点数据原地复用；幕帘硬边复制顶点分散在尾部，整份重传更稳
        roi = self._glRoiPending
        self._glRoiPending = None
        if roi is not None and getattr(mesh, "wall_dup", None) is not None:
            roi = None
        self._gl.draw(self._glCacheKey, verts, vn, base, idx, mvp, eye, light,
                      params, W, H, self._bgFloat,
                      getattr(self, "_curFbo", 0),
                      opacity=self._reliefOpacity(), roi=roi)
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
        # 记录投影坐标，供边框叠加使用（相机/网格不变时整帧复用；
        # 注意 _reliefProj 的消费方按 (mesh, sx, sy, valid) 四元解包，勿带 depth）
        sx, sy, _depth, valid = self._borderProjection(mesh.verts, cam, w, h)
        self._reliefProj = (mesh, sx, sy, valid)
        self._reliefDrawn = True   # 本帧 GPU 浮雕绘制成功（叠加层兜底检查用）

    def _borderProjection(self, verts, cam, w, h):
        """全场顶点投影的帧间缓存（仅供边框叠加/拾取辅助使用）。

        拖动笔画时鼠标移动帧的网格与相机都不变，而全场 6.5 万顶点投影
        要 2-3ms——按（网格实例 + 相机 + 视口尺寸）缓存后直接复用，
        笔刷圈跟随显著更跟手。旋转/缩放/数据变化时自动重算。

        注意：键里**不含**高度版本——笔画期 z 每帧微调，整份重投影（3000²
        ~30ms）会把拖动拖到 20fps 以下；边框叠加容许短暂滞后，笔画停下
        300ms 后由 :meth:`_onReliefCacheRefresh` 置空键重算到精确值。
        """
        key = (id(verts),
               round(self._yaw, 4), round(self._pitch, 4),
               round(self._dist, 3),
               round(self._pan[0], 3), round(self._pan[1], 3),
               self._mode, int(w), int(h))
        if getattr(self, "_projKey", None) == key \
                and self._projCache is not None:
            return self._projCache
        sx, sy, depth, valid = self._projectVerts(verts, cam, w, h)
        self._projKey = key
        self._projCache = (sx, sy, depth, valid)
        return self._projCache

    def _triIndexFor(self, mesh):
        """四边形面 → 三角形索引（uint32），按网格实例缓存复用。

        雕刻/磨光笔画只改高度（拓扑不变），每次版本刷新重建顶点/法线即可，
        索引直接复用上一份，省掉每帧 ~1.5MB 的重算与分配。

        键必须含网格实例（id(mesh)）：硬边复制（幕帘/悬崖壁）让顶点数相同
        的两个网格拓扑不同（复制顶点位置不同），只按 (gnx,gny,V) 会撞键——
        新模型配旧模型的索引缓冲 → 三角形连错顶点，渲染出扇形花瓣花屏
        （实测：悬崖圆盘 → 斜坡圆盘切换）。id(mesh) 在 _reliefMesh 持有的
        网格存活期内稳定且每次全量重建必然变化，拓扑不变的重用场景不受影响。
        """
        key = (id(mesh), mesh.gnx, mesh.gny, mesh.verts.shape[0])
        if (self._triIdxCacheKey == key and self._triIdxCache is not None
                and self._triIdxMeshRef is mesh):
            return self._triIdxCache
        faces = mesh.faces
        v00, v01, v11, v10 = faces[:, 0], faces[:, 1], faces[:, 2], faces[:, 3]
        F = faces.shape[0]
        idx = np.empty(F * 6, dtype=np.uint32)
        idx[0::6] = v00; idx[1::6] = v01; idx[2::6] = v11
        idx[3::6] = v00; idx[4::6] = v11; idx[5::6] = v10
        self._triIdxCache = idx
        self._triIdxCacheKey = key
        self._triIdxMeshRef = mesh   # 防同地址新网格假命中
        return idx

    def _baseColorsVert(self, mesh, model) -> np.ndarray:
        """逐顶点底色（未打光，[0,1]）：map→地形 LUT、color→用户色、material→程序化底。

        幕帘壁的着色约定（壁竖直光滑 + 台面纯色无杂点）：
        - map/color 模式颜色是**顶点自身属性**（顶点高度 / 顶点色号蒙版）：
          壁条底边顶点取低/外色、顶边取高/内色，Gouraud 插值出**沿高度从
          低到高的竖直渐变**（水平方向同高同色，无栅格交叉花色）；台面顶点
          同高同色 → 0 面/最高面纯色到边，不做面中心平均（面中心高度受
          跨壁锯齿面污染会在台面边缘产生杂点）；
        - material 模式底色是面属性（木纹/石材程序化纹），按顶点角色硬边
          分离散射（壁顶点只平均壁面、台面顶点只平均水平面）。
        """
        mode = model.display3DMode()
        material = model.material()
        base_color = model.color()
        V = mesh.verts.shape[0]
        if mode == "map":
            lut = shade.make_lut("terrain")                 # (256,3) uint8
            z = mesh.verts[:, 2]
            lo, hi = float(z.min()), float(z.max())
            # 记录本次归一化区间：笔画期 ROI 底色刷新沿用（避免每帧全场 gather）
            self._mapColorNorm = (lo, hi)
            vh = np.full(z.shape, 0.5) if hi - lo < 1e-9 else (z - lo) / (hi - lo)
            vi = np.clip((vh * 255.0).astype(np.int32), 0, 255)
            return lut[vi].astype(np.float64) / 255.0
        if mode == "color":
            nodeRgb = self._colorMaskNodeRgb(mesh, model)
            if nodeRgb is not None:
                return nodeRgb.astype(np.float64) / 255.0   # 顶点蒙版直出
            col = np.array(base_color if base_color else (21, 182, 201),
                           dtype=np.float64) / 255.0
            return np.tile(col, (V, 1))
        fb = mesh3d._materialBase(mesh, material, base_color).astype(np.float64) / 255.0
        return self._scatterWallAware(mesh, fb)

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
        if cm is None or cm.size > V:
            return None
        pal = paint_palette.PALETTE
        n = len(pal)
        base_rgb = model.color() or (21, 182, 201)
        lut = np.empty((n + 1, 3), dtype=np.float64)
        lut[0] = base_rgb                       # EMPTY(0) → 模型底色
        for i, c in enumerate(pal):
            lut[i + 1] = c                      # 值 v → PALETTE[v-1]
        vals = np.clip(cm.ravel().astype(np.int32), 0, n)
        rgb = lut[vals]                          # (V0,3) 0..255
        # 幕帘硬边复制顶点（XYZ 与原顶点相同）沿用同一栅格蒙版色
        wd = getattr(mesh, "wall_dup", None)
        if wd is not None and rgb.shape[0] < V:
            src, _dup = wd
            rgb = np.vstack([rgb, rgb[src]])
        if rgb.shape[0] != V:
            return None
        return rgb

    def _scatterFaceToVert(self, mesh, faceArr: np.ndarray) -> np.ndarray:
        """把逐面数组按顶点索引平均到共享顶点（Gouraud 顶点色/法线用）。

        用 ``np.bincount`` 按通道累加（C 速度）——``np.add.at`` 是非缓冲的
        裸索引循环，26 万面索引要 ~9ms 且每次笔画刷新都会跑，是雕刻/磨光
        期间长帧（模型"闪一下"）的主要成分之一；bincount 同结果 ~5ms。
        """
        V = mesh.verts.shape[0]
        C = faceArr.shape[1]
        fidx = mesh.faces.ravel()
        out = np.empty((V, C), dtype=np.float64)
        for c in range(C):
            out[:, c] = np.bincount(fidx, weights=faceArr[:, c].repeat(4),
                                    minlength=V)
        cnt = np.bincount(fidx, minlength=V).astype(np.float64)
        return out / np.maximum(cnt, 1.0)[:, None]

    def _scatterWallAware(self, mesh, faceArr: np.ndarray) -> np.ndarray:
        """面→顶点平均（Gouraud 顶点色/法线用）。

        幕帘硬边已在网格构建时通过**复制环顶点**物理分离：壁面引用副本、
        台面扇面引用原顶点（见 mesh3d.buildMesh 的 wall_dup），所以普通
        邻接平均即可——副本只被壁面引用（得水平法线），原环顶点只被台面
        面引用（得朝天法线），无需特殊角色逻辑。
        """
        return self._scatterFaceToVert(mesh, faceArr)

    def _noteRoiBox(self, box) -> None:
        """累计"自上次 GL 同步以来"受影响的顶点块（取并集，含端）。"""
        self._geoDirtySinceGl = True
        cur = self._roiBoxSinceGl
        if cur is None:
            self._roiBoxSinceGl = box
        else:
            self._roiBoxSinceGl = (min(cur[0], box[0]), max(cur[1], box[1]),
                                   min(cur[2], box[2]), max(cur[3], box[3]))

    def _syncGlMeshIncremental(self, mesh, model) -> None:
        """笔画期只把**变化区域**同步到 GPU 缓存（位置 z / 顶点法线 / map 底色）。

        前置：同一网格实例、拓扑不变（styleKey 命中），且自上次同步以来的
        高度改动都用 ``_roiBoxSinceGl`` 累计记录。全场 bincount 散射与地形
        LUT 全场 gather 都是 O(V)（3000² 栅格 ~120ms/帧），拖动因此卡死；
        这里改成只刷 ROI 顶点块（数量级 ~1e4），帧成本降到毫秒级。

        ``_roiBoxSinceGl`` 缺失（撤销、整场操作、状态不明）时回退整份重算，
        保证显示绝不落后于数据。
        """
        verts, vn, base, idx = self._glCache
        box = self._roiBoxSinceGl
        if box is not None:
            pr0, pr1, pc0, pc1 = box      # 受影响的顶点块（含端）
            # XY 不受高度编辑影响；z 只刷 ROI 块（整列 3D 步长拷贝 1M 行 ~8ms）
            gny, gnx = mesh.gny, mesh.gnx
            ids = (np.arange(pr0, pr1 + 1)[:, None] * gnx
                   + np.arange(pc0, pc1 + 1)[None, :]).ravel()
            verts[ids, 2] = np.ascontiguousarray(mesh.verts[ids, 2],
                                                 dtype=np.float32)
            wd = getattr(mesh, "wall_dup", None)
            if wd is not None:
                dsrc, ddup = wd
                verts[ddup, 2] = verts[dsrc, 2]
            self._updateVertexNormalsRoi(mesh, vn, pr0, pr1, pc0, pc1)
            if model.display3DMode() == "map":
                self._updateMapColorsRoi(mesh, base, pr0, pr1, pc0, pc1)
            self._glRoiPending = (pr0, pr1, pc0, pc1, gnx)
            self._scheduleReliefCacheRefresh()
        elif self._geoDirtySinceGl:
            # 兜底：有高度改动却没记录 ROI（状态不明）→ 整份重算，宁慢勿错
            verts[:, 2] = mesh.verts[:, 2]
            vn_full = self._scatterWallAware(mesh, mesh.normals)
            vn_full /= np.maximum(
                np.linalg.norm(vn_full, axis=1, keepdims=True), 1e-12)
            vn[:] = vn_full
            if model.display3DMode() == "map":
                base[:] = self._baseColorsVert(mesh, model)
            self._glBaseDirty = False
        else:
            verts[:, 2] = mesh.verts[:, 2]
        self._glMeshKey = self._reliefMeshKey
        self._roiBoxSinceGl = None
        self._geoDirtySinceGl = False

    def _updateVertexNormalsRoi(self, mesh, vn, pr0, pr1, pc0, pc1) -> None:
        """顶点块 ``[pr0..pr1] × [pc0..pc1]``（含端）的顶点法线增量重算。

        与 :meth:`_scatterFaceToVert` 整份散射击中逐位等价（含幕帘/悬崖硬边
        复制顶点的角色分离）：用网格里**实际的面→顶点引用**（remap 之后）
        在局部面块上做 bincount 平均，O(ROI) 而非 O(V)。
        """
        gny, gnx = mesh.gny, mesh.gnx
        wd = getattr(mesh, "wall_dup", None)
        V0 = mesh.verts.shape[0] - (0 if wd is None else wd[1].size)
        h, w = pr1 - pr0 + 1, pc1 - pc0 + 1
        if h <= 0 or w <= 0:
            return
        blk = vn[:V0].reshape(gny, gnx, 3)[pr0:pr1 + 1, pc0:pc1 + 1]
        if wd is None:
            # 无幕帘：顶点(i,j) 的邻接面恒为 (i-1,j-1),(i-1,j),(i,j-1),(i,j)，
            # 用 4 次 2D 区块加即可（比按面散射的整除/取模+bincount 快 5 倍）
            FN = mesh.normals.reshape(gny - 1, gnx - 1, 3)
            acc = np.zeros((h, w, 3), dtype=np.float64)
            cnt = np.zeros((h, w, 1), dtype=np.float64)
            for di in (-1, 0):
                fr0 = max(0, pr0 + di)
                fr1 = min(gny - 1, pr1 + di + 1)
                if fr1 <= fr0:
                    continue
                for dj in (-1, 0):
                    fc0 = max(0, pc0 + dj)
                    fc1 = min(gnx - 1, pc1 + dj + 1)
                    if fc1 <= fc0:
                        continue
                    acc[fr0 - di - pr0:fr1 - di - pr0,
                        fc0 - dj - pc0:fc1 - dj - pc0] += \
                        FN[fr0:fr1, fc0:fc1]
                    cnt[fr0 - di - pr0:fr1 - di - pr0,
                        fc0 - dj - pc0:fc1 - dj - pc0] += 1.0
            blk[:] = (acc / np.maximum(
                np.linalg.norm(acc, axis=2, keepdims=True), 1e-12)
            ).astype(np.float32)
            return
        fa0, fa1 = max(0, pr0 - 1), min(gny - 2, pr1)
        fb0, fb1 = max(0, pc0 - 1), min(gnx - 2, pc1)
        fb = mesh.faces.reshape(gny - 1, gnx - 1, 4)[fa0:fa1 + 1, fb0:fb1 + 1]
        fn = mesh.normals.reshape(gny - 1, gnx - 1, 3)[fa0:fa1 + 1, fb0:fb1 + 1]
        ids = fb.reshape(-1, 4).ravel()
        nrep = np.repeat(fn.reshape(-1, 3), 4, axis=0)
        r = ids // gnx
        c = ids % gnx
        ok = (r >= pr0) & (r <= pr1) & (c >= pc0) & (c <= pc1) & (ids < V0)
        loc = (r[ok] - pr0) * w + (c[ok] - pc0)
        acc = np.empty((h * w, 3), dtype=np.float64)
        for ch in range(3):
            acc[:, ch] = np.bincount(loc, weights=nrep[ok, ch], minlength=h * w)
        cnt = np.bincount(loc, minlength=h * w).astype(np.float64)
        acc /= np.maximum(np.linalg.norm(acc, axis=1, keepdims=True), 1e-12)
        sel = (cnt > 0).reshape(h, w)
        blk[sel] = acc.astype(np.float32).reshape(h, w, 3)[sel]
        if wd is not None:
            # 硬边复制顶点只被壁面引用：仅壁面参与平均（数量级为周长，很便宜）
            wfsel = np.nonzero(mesh.wall_faces)[0]
            fw = mesh.faces[wfsel].ravel()
            nw = mesh.normals[wfsel].repeat(4, axis=0)
            keep = fw >= V0
            local = fw[keep] - V0
            m = wd[1].size
            a2 = np.empty((m, 3), dtype=np.float64)
            for ch in range(3):
                a2[:, ch] = np.bincount(local, weights=nw[keep, ch],
                                        minlength=m)
            a2 /= np.maximum(np.linalg.norm(a2, axis=1, keepdims=True), 1e-12)
            vn[V0:V0 + m] = a2.astype(np.float32)

    def _updateMapColorsRoi(self, mesh, base, pr0, pr1, pc0, pc1) -> None:
        """map 模式底色 ROI 刷新：沿用上一次整份归一化区间 (lo, hi)。

        笔画期不重算全场 min/max：峰值可能暂时顶到 LUT 高端色（视觉无感），
        笔画停下 300ms 后由 :meth:`_onReliefCacheRefresh` 整份重刷到精确值。
        """
        norm = self._mapColorNorm
        if norm is None:
            return
        lut = self._mapLutF32
        if lut is None:
            lut = shade.make_lut("terrain").astype(np.float32) / 255.0
            self._mapLutF32 = lut
        lo, hi = norm
        gny, gnx = mesh.gny, mesh.gnx
        wd = getattr(mesh, "wall_dup", None)
        V0 = mesh.verts.shape[0] - (0 if wd is None else wd[1].size)
        z = mesh.verts[:V0, 2].reshape(gny, gnx)[pr0:pr1 + 1, pc0:pc1 + 1]
        if hi - lo < 1e-9:
            vi = np.full(z.shape, 128, dtype=np.int32)
        else:
            vi = np.clip(((z - lo) / (hi - lo) * 255.0).astype(np.int32),
                         0, 255)
        blk = base[:V0].reshape(gny, gnx, 3)[pr0:pr1 + 1, pc0:pc1 + 1]
        blk[:] = lut[vi]
        if wd is not None:
            base[V0:] = base[wd[0]]

    def _scheduleReliefCacheRefresh(self, delay: int = 300) -> None:
        """笔画增量同步后重启去抖定时器：停下后整份重刷底色/投影。

        拖动期间定时器不断被重启 → 不做整份重算；松手 300ms 后触发一次，
        把 map 底色的归一化区间与边框投影刷新到精确值。
        """
        t = self._reliefRefreshTimer
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(self._onReliefCacheRefresh)
            self._reliefRefreshTimer = t
        t.start(delay)

    def _onReliefCacheRefresh(self) -> None:
        self._glBaseDirty = True
        # 笔画期推迟的高度归一化在这里补算（每场 min/max 一遍）
        m = self._reliefMesh
        if m is not None and self._hnormDirty:
            hz = m.centers[:, 2]
            lo, hi = float(hz.min()), float(hz.max())
            if hi - lo < 1e-9:
                m.hnorm[:] = 0.5
            else:
                m.hnorm[:] = (hz - lo) / (hi - lo)
            self._hnormDirty = False
        self._projKey = None      # 边框投影下一帧整份重算（笔画期用的是旧值）
        self.update()

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
        # 远平面随相机距离外推：拉远超过旧固定 4000 时场景不会被裁空
        far = self._dist + 5000.0
        if ortho:
            scale = self._orthoScale(w, h)
            # 正交眼固定在 _dist+1000 高空（见 _camera）：near/far 相对
            # 眼平面外扩，覆盖目标平面上下 ±1.1k/6k 的场景几何。
            proj = ortho_proj(scale, w, h, -1100.0, self._dist + 6000.0)
        else:
            fovy = math.radians(FOV_DEG)
            aspect = (W / H) if H > 0 else 1.0
            proj = perspective(fovy, aspect, NEAR, far)
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
        # 网格线不抗锯齿：76 条线逐条 AA 是纯开销，视觉差异可忽略
        # （网格本身是浅灰细参考线；AA 只留给轴与选中/命令高亮）
        painter.setRenderHint(QPainter.Antialiasing, False)
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

        painter.setRenderHint(QPainter.Antialiasing, True)

    def _drawAxes(self, painter, cam, w: float, h: float,
                  depthBuf=None) -> None:
        length = GRID_RANGE
        eye, _r, _u, fwd, _o = cam

        def _wd(p):
            v = (p[0] - eye[0], p[1] - eye[1], p[2] - eye[2])
            return v[0] * fwd[0] + v[1] * fwd[1] + v[2] * fwd[2]

        for name, (color, direction) in _AXIS_STYLE.items():
            if name == "Z" and self._canvasMode != "cnc":
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
            if depthBuf is not None:
                # 轴线被实体遮挡的部分不画（与曲线同一套隐藏线消除）：
                # 整轴一段会同时穿过模型前后，按屏幕 ~6px 细分，
                # 每小段带世界坐标深度独立测深
                scr = math.hypot(seg[1][0] - seg[0][0],
                                 seg[1][1] - seg[0][1])
                n = max(16, min(256, int(scr / 6.0)))
                lines = []
                for i in range(n):
                    ta = i / n
                    tb = (i + 1) / n
                    pa = (direction[0] * length * ta,
                          direction[1] * length * ta,
                          direction[2] * length * ta)
                    pb = (direction[0] * length * tb,
                          direction[1] * length * tb,
                          direction[2] * length * tb)
                    s2 = self._projectSegment(pa, pb, cam, w, h)
                    if s2 is None:
                        continue
                    lines.append((s2[0][0], s2[0][1], s2[1][0], s2[1][1],
                                  _wd(pa), _wd(pb)))
                if not lines:
                    continue
                visible, _hid = self._filterHiddenLines(lines, depthBuf)
                if not visible:
                    continue
                painter.drawLines([QLineF(QPointF(a, b), QPointF(c, d))
                                   for a, b, c, d in visible])
            else:
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

    def _onDocRemovedSlot(self, oid: str = None, *_) -> None:
        """文档对象被移除：若本视口仍持该浮雕模型陈旧引用，主动清空，
        避免后续 paintGL 访问已释放的 heightField 闪退
        （2D 模式下同时删除模型+曲线场景）。"""
        m = self._reliefModel
        if m is not None and oid is not None and m.id() == oid:
            self._reliefModel = None
            self._reliefMeshKey = None
        self.update()

    def _cncDbg(self, msg: str) -> None:
        """CNC 渲染诊断日志（排查期每帧必写），落 render_debug.log。"""
        try:
            from ui.view_3d._renderdbg import dbg
            dbg("cnc " + msg)
        except Exception:
            pass

    def _firstCncModel(self):
        """CNC 模式要渲染的模型：CNC 独立空间导入的浮雕模型（首个可见）。"""
        models = self._reliefModelsForCnc()
        return models[0] if models else None

    def _reliefTarget(self):
        """当前应渲染的浮雕模型：浮雕模式锁定者优先，否则活动（选中）模型。

        3D 模式下不渲染任何浮雕模型——3D 是独立空间，看不到 2D/浮雕的模型。
        CNC 模式渲染 CNC 独立空间中导入的模型（与浮雕模式同一渲染路径）。
        模型所在图层被隐藏时返回 None（灯泡关掉后 3D 视口也不显示该模型）。
        """
        if self._canvasMode == "cnc":
            return self._firstCncModel()
        if self._canvasMode == "3d":
            return None
        model = self._reliefModel
        if model is not None and self._doc is not None:
            # 防御：模型可能已被 removeObject 删除但本视口仍持陈旧引用
            if self._doc.object(model.id()) is not model:
                self._reliefModel = None
                self._reliefMeshKey = None
                model = None
        if model is None and self._doc is not None:
            model = self._doc.activeReliefModel()
        if model is None:
            return None
        if self._doc is not None:
            layer = self._doc.layer(model.layerId())
            if layer is not None and not layer.visible:
                return None
        return model

    def _reliefScreenRect(self, model, cam, w, h):
        """浮雕模型在当前视角下的屏幕外接矩形：用于把参考图片压到模型下方。

        取模型包围盒 8 个角点的投影极值；包围盒无效/投影失败时返回 None。
        """
        try:
            bb = model.getBoundingBox()
            if bb is None or not bb.isValid():
                return None
            xs, ys = [], []
            for x in (bb.min.x, bb.max.x):
                for y in (bb.min.y, bb.max.y):
                    for z in (bb.min.z, bb.max.z):
                        s = self._project((x, y, z), cam, w, h)
                        if s is None:
                            continue
                        xs.append(s[0])
                        ys.append(s[1])
            if not xs:
                return None
            x0, x1 = min(xs), max(xs)
            y0, y1 = min(ys), max(ys)
            return QRectF(x0, y0, max(x1 - x0, 1.0), max(y1 - y0, 1.0))
        except Exception:
            return None

    def _reliefMeshFor(self, model, max_dim: int = None):
        """按 (版本, 放置, 垂直夸张, 固定网格密度) 缓存 3D 面网格。

        显示网格 LOD 与相机/缩放无关（专业做法）：缩放只改 MVP 矩阵，几何不重建
        →不重传 GPU 缓冲（避免每次缩放重建显存导致的卡顿/段错误）。密度只随模型
        真实分辨率与 ``max_dim``（设置页：GPU/软件各一档）变化。

        雕刻/磨光笔画期（同模型、同放置/夸张/密度，仅高度数据变）走
        :meth:`_tryRoiMeshUpdate` 脏区域增量更新——只重算笔刷扫过区域的
        z/法线/中心，其余顶点原样保留，帧成本从整场 ~20ms 降到 ~3ms，
        消除 60Hz 下踩不准垂直同步造成的规律性闪跳。
        """
        if max_dim is None:
            max_dim = _RELIEF_MESH_MAX
        dim = max(_RELIEF_MESH_MIN,
                  min(max_dim,
                      max(model.heightField().nx, model.heightField().ny)))
        dim = min(dim, max_dim)
        self._reliefMeshLodDim = dim     # 记录本次网格 LOD，供色号蒙版同分辨率对齐
        plc = model.placement().tobytes()
        zexag = round(float(model.zExaggeration()), 5)
        cstamp = int(model.curtainStamp())
        key = (id(model), model.version(), plc, zexag, dim, cstamp)
        if self._reliefMeshKey == key and self._reliefMesh is not None:
            return self._reliefMesh
        if self._tryRoiMeshUpdate(model, dim, key, plc, zexag):
            return self._reliefMesh
        hf = model.heightField()
        small = hf.downsample(dim)
        curtains = [(np.asarray(c["poly"], dtype=np.float64), float(c["d"]))
                    for c in model.curtains()]
        mesh = mesh3d.buildMesh(small, model.placement(), model.zExaggeration(),
                                dim, curtains=curtains or None)
        self._reliefMesh = mesh
        self._reliefMeshKey = key
        # 记录本次网格对应的降采样源场（快照副本），供下一笔做脏区域差分
        self._meshSrcGrid = small.data.copy()
        self._meshSrcMeta = (id(model), plc, zexag, dim, int(model.curtainStamp()))
        self._hnormDirty = False     # buildMesh 已算好 hnorm
        return mesh

    def _tryRoiMeshUpdate(self, model, dim, key, plc, zexag) -> bool:
        """脏区域增量更新：与上一笔相比只重算变化区域的 z/法线/中心。

        前提：同模型、同放置/夸张/密度（网格拓扑与 XY 不变，仅高度变）。
        与全量 ``buildMesh`` 逐位等价（同一降采样映射、同一法线/中心公式），
        但只触碰变化区域 ±1 环，成本 O(ROI) 而非 O(整场)。
        大范围改动（如撤销、整场磨光）超过 6 成格子变化时回退全量。
        返回 True 表示已就地更新 ``self._reliefMesh`` 并推进其 key。
        """
        m = self._reliefMesh
        src = self._meshSrcGrid
        meta = self._meshSrcMeta
        if m is None or src is None or meta is None:
            return False
        cstamp = int(model.curtainStamp())
        if meta != (id(model), plc, zexag, dim, cstamp):
            return False
        hf = model.heightField()
        # 必须与 buildMesh 的取样**完全一致**（downsample 用 linspace 均匀取样且
        # 必含末行末列，不是 [::step]）。旧实现用 [::step] 对比，一旦模型分辨率
        # 超过显存档（dim）采样就系统性错位、几乎每格都"变化"→ 增量判定永远
        # 失败、每帧回退全量重建（3000² 栅格 ≈1.3s/帧），塑形拖动因此卡死。
        g = hf.downsample(dim).data
        if g.shape != src.shape:
            return False
        changed = g != src
        nchg = int(np.count_nonzero(changed))
        if nchg == 0:
            self._reliefMeshKey = key    # 纯显示键变化：几何原样
            return True
        if nchg > 0.6 * changed.size:
            return False                 # 大范围改动：全量重建更划算
        # 包围盒用行/列 any + argmax 推导：比 nonzero（生成两个等长索引数组）
        # 省一半以上时间；笔画期每帧都要走这里，1M 格上的开销不可忽略
        rows_any = changed.any(axis=1)
        cols_any = changed.any(axis=0)
        r0 = int(np.argmax(rows_any))
        r1 = int(len(rows_any) - np.argmax(rows_any[::-1]))
        c0 = int(np.argmax(cols_any))
        c1 = int(len(cols_any) - np.argmax(cols_any[::-1]))

        gny, gnx = src.shape
        # 顶点 z 只重写变化块；XY 不变（整列 1M 行 float64 转换+步长写 ~12ms/帧）
        rids = np.arange(r0, r1)[:, None] * gnx + np.arange(c0, c1)[None, :]
        zblk = (g[r0:r1, c0:c1].astype(np.float64)
                * float(model.zExaggeration()))
        m.verts[rids.ravel(), 2] = zblk.ravel()
        # 幕帘硬边复制顶点（wall_dup）与原顶点 XYZ 相同，Z 同步
        wd = getattr(m, "wall_dup", None)
        if wd is not None:
            dsrc, ddup = wd
            m.verts[ddup, 2] = m.verts[dsrc, 2]

        # 变化区域 ±1 环的面：重算法线与中心（公式与 buildMesh 严格一致）
        gnyF, gnxF = src.shape[0] - 1, src.shape[1] - 1
        fr0, fr1 = max(0, r0 - 1), min(gnyF, r1)
        fc0, fc1 = max(0, c0 - 1), min(gnxF, c1)
        fgrid = m.faces.reshape(gnyF, gnxF, 4)
        sub = fgrid[fr0:fr1, fc0:fc1]
        p00 = m.verts[sub[..., 0]]
        p01 = m.verts[sub[..., 1]]
        p11 = m.verts[sub[..., 2]]
        p10 = m.verts[sub[..., 3]]
        nsub = np.cross(p11 - p00, p10 - p01).reshape(-1, 3)
        ln = np.linalg.norm(nsub, axis=1)
        ln[ln < 1e-12] = 1.0
        nsub /= ln[:, None]
        nsub[nsub[:, 2] < 0.0] *= -1.0
        # 无幕帘悬崖面：与 buildMesh 同一规则，按"高处→低处"定向
        # （朝向歧义会让雕刻过的悬崖列明暗交替成竖条纹）
        if getattr(m, "wall_n", None) is None:
            cl = np.abs(nsub[:, 2]) < 0.3
            if cl.any():
                P4 = m.verts[sub.reshape(-1, 4)[cl]]
                zz = P4[:, :, 2]
                hi_m = zz >= zz.max(axis=1, keepdims=True) - 1e-6
                lo_m = zz <= zz.min(axis=1, keepdims=True) + 1e-6
                hc = (P4 * hi_m[:, :, None]).sum(axis=1) / np.maximum(
                    hi_m.sum(axis=1), 1)[:, None]
                lc = (P4 * lo_m[:, :, None]).sum(axis=1) / np.maximum(
                    lo_m.sum(axis=1), 1)[:, None]
                dd = lc[:, :2] - hc[:, :2]
                dn = np.hypot(dd[:, 0], dd[:, 1])
                hn2 = nsub[cl][:, :2]
                hln = np.hypot(hn2[:, 0], hn2[:, 1])
                okc = (dn > 1e-9) & (hln > 1e-9)
                dt = np.zeros(len(hn2))
                dt[okc] = (hn2[okc] * dd[okc]).sum(axis=1) / (hln[okc] * dn[okc])
                nsub[np.nonzero(cl)[0][dt < 0]] *= -1.0
                # 与 buildMesh 同一模糊梯度朝向（楼梯锯齿抑制，壁面平滑）。
                # 模糊是局部的（2 遍 5 格盒模糊 ≈ 邻域 ±8 格）：只把变化区域的
                # 局部窗口喂进去，坐标改成窗口内偏移——整场 cumsum（1024²
                # ≈18ms/帧）曾把笔画帧拖到 40ms+。窗口边界模式同为 edge，
                # 且读点距窗口边 ≥8 格 → 与整场结果逐格一致（除非窗口被模型
                # 边界截断，此时边界恰好也是整场边界，仍一致）。
                gi = np.nonzero(cl)[0]
                r_g = fr0 + gi // (fc1 - fc0)
                c_g = fc0 + gi % (fc1 - fc0)
                pad = 10
                wr0 = max(0, int(r_g.min()) - pad)
                wr1 = min(g.shape[0] - 1, int(r_g.max()) + pad)
                wc0 = max(0, int(c_g.min()) - pad)
                wc1 = min(g.shape[1] - 1, int(c_g.max()) + pad)
                gx_c, gy_c = mesh3d._cliffGradientNormals(
                    g[wr0:wr1 + 1, wc0:wc1 + 1], r_g - wr0, c_g - wc0)
                gln = np.hypot(gx_c, gy_c)
                okg = gln > 1e-9
                sel = gi[okg]
                nsub[sel, 0] = -(gx_c[okg] / gln[okg])
                nsub[sel, 1] = -(gy_c[okg] / gln[okg])
                nsub[sel, 2] = 0.0
        # 幕帘竖直壁 quad：与 buildMesh 同一规则，用顶点水平外法向定向
        #（ROI 只改 Z、XY 投影位置不变，wall_n 沿用上一次全量构建的结果）
        wn = getattr(m, "wall_n", None)
        if wn is not None:
            idx4 = sub.reshape(-1, 4)
            fn = (wn[idx4[:, 0]] + wn[idx4[:, 1]] + wn[idx4[:, 2]]
                  + wn[idx4[:, 3]]) * 0.25
            fl = np.hypot(fn[:, 0], fn[:, 1])
            vm = (fl > 0.3) & (np.abs(nsub[:, 2]) < 0.7)
            if vm.any():
                nsub[vm, 0] = fn[vm, 0] / fl[vm]
                nsub[vm, 1] = fn[vm, 1] / fl[vm]
                nsub[vm, 2] = 0.0
        m.normals.reshape(gnyF, gnxF, 3)[fr0:fr1, fc0:fc1] = \
            nsub.reshape(fr1 - fr0, fc1 - fc0, 3)
        m.centers.reshape(gnyF, gnxF, 3)[fr0:fr1, fc0:fc1] = \
            (p00 + p01 + p11 + p10) * 0.25

        # 高度归一化（整场 min/max）推迟到笔画停下后：全场重算 ~8ms/帧，
        # 笔画期 hnorm 只影响软件路径的面色 LUT（GPU 路径不读），略微饱和无感
        self._hnormDirty = True

        self._meshSrcGrid = g.copy()
        self._reliefMeshKey = key
        # 记录本次受影响的**顶点块**（含端），供 GL 缓存做局部同步
        self._noteRoiBox((max(0, fr0), min(m.gny - 1, fr1),
                          max(0, fc0), min(m.gnx - 1, fc1)))
        return True

    def _gpuMeshDim(self) -> int:
        """GPU 路径显示网格密度上限（设置页可调，默认 256）。

    警告：不可为提速在笔画期切换密度——128↔256 切换时平缓区域/模型边缘
    的法线与轮廓会轻微跳动（用户看到"0 平面动一下"）。笔画期的帧成本靠
    bincount 散射 + 索引拓扑缓存 + GC 守卫压低，密度全程保持恒定。
    """
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
        self._glMeshRef = None
        self._glMeshKey = None
        self._roiBoxSinceGl = None
        self._geoDirtySinceGl = False
        self._glBaseDirty = False
        self._mapColorNorm = None
        self._projKey = None
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

    def _cncReliefModelForDraw(self):
        """独立扫描 CNC 空间浮雕模型（不走 _reliefTarget/_reliefModelsForCnc，
        异常原样抛出由调用方诊断），返回 (model, diag)。"""
        if self._doc is None:
            return None, ["doc none"]
        diag = []
        model = None
        try:
            from core.vector.geometry_types import ObjectType
            cnt = 0
            for o in self._doc.objects():
                cnt += 1
                if o.type() == ObjectType.SURFACE:
                    lay = self._doc.layer(o.layerId())
                    if lay is not None and not lay.visible:
                        continue
                    model = o
                    break
            diag.append("objects=%d surf=%s" % (
                cnt, "yes" if model is not None else "no"))
        except Exception as e:                      # noqa: BLE001
            diag.append("scan err:%r" % (e,))
        return model, diag

    def _drawCncModelLayer(self, painter, cam, w: float, h: float) -> None:
        """CNC 模式模型参考层（_paint3D 叠加层兜底）：B-Rep 管线已渲染实体
        （_reliefDrawn=True）时跳过；否则软渲染实体 → 面填充网格 → 红字诊断。
        """
        if self._reliefDrawn:
            return   # B-Rep 管线已画出实体模型
        if not self._reliefDrawn:
            try:
                self._drawReliefSurfaceSoftware(painter, cam, w, h)
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._cncDiagNote = "soft err:%r" % (e,)
        try:
            self._drawCncModelWire(painter, cam, w, h)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._cncDiagNote = "wire err:%r" % (e,)

    def _drawCncModelWire(self, painter, cam, w: float, h: float) -> None:
        """CNC 模型网格层：独立找模型、投影画四边形网格面（半透明填充+边线）。

        与 GL/浮雕管线零耦合——即使 GPU 与软渲染实体都不可见，模型形状也
        一定显示（浅蓝半透明曲面网格）。面过多时按步长稀疏采样控开销。
        """
        model, _diag = self._cncReliefModelForDraw()
        if model is None:
            self._cncDbg("wire scan: no model")
            return
        mesh = self._reliefMeshFor(model, max_dim=self._cpuMeshDim())
        if mesh is None or mesh.faceCount == 0:
            self._cncDbg("wire mesh: empty")
            return
        verts = mesh.verts
        faces = mesh.faces
        n = len(faces)
        stride = 1 if n <= 2000 else (n // 2000 + 1)
        painter.save()
        painter.setPen(QPen(QColor(120, 200, 255, 130), 1.0))
        painter.setBrush(QBrush(QColor(130, 205, 255, 46)))
        for t in faces[::stride]:
            pts = []
            ok = True
            for k in range(4):
                p = self._project(tuple(verts[t[k]]), cam, w, h)
                if p is None:
                    ok = False
                    break
                pts.append(QPointF(p[0], p[1]))
            if not ok:
                continue
            painter.drawPolygon(QPolygonF(pts))
        painter.restore()

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
        self._reliefDrawn = True   # 本帧浮雕已渲染（软渲染成功，叠加层不再补线框）

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
        mode = model.display3DMode()
        if mode in ("map", "color"):
            # 颜色是顶点自身属性（高度 LUT / 色号蒙版）：壁条沿 Z 竖直渐变、
            # 台面纯色；逐顶点打光（法线按角色硬边分离）
            base_v = self._baseColorsVert(mesh, model) * 255.0
            vn = self._scatterWallAware(mesh, mesh.normals)
            vn /= np.maximum(np.linalg.norm(vn, axis=1, keepdims=True), 1e-12)
            L = mesh3d._normVec(light)
            Vdir = mesh3d._normalizeRows(
                np.asarray(cam[0], dtype=np.float64)[None, :] - mesh.verts)
            diff = np.clip(vn @ L, 0.0, 1.0)
            head = np.clip(np.einsum("ij,ij->i", vn, Vdir), 0.0, 1.0)
            H = mesh3d._normalizeRows(L[None, :] + Vdir)
            spec = np.clip(np.einsum("ij,ij->i", vn, H), 0.0, 1.0)
            d = np.maximum(diff, head * mesh3d._HEAD)
            soft = mesh3d._FLOOR + (1.0 - mesh3d._FLOOR) * d
            amb = mesh3d._AMBIENT_MAP if mode == "map" else mesh3d._AMBIENT
            lit = amb + (1.0 - amb) * soft
            gain, spow = (40.0, 24.0) if mode == "map" else (50.0, 32.0)
            vcol = np.clip(base_v * lit[:, None]
                           + gain * (spec ** spow)[:, None], 0, 255)
        else:
            # 材质模式：面色（程序化底色+打光）按顶点角色硬边分离散射
            fc = mesh3d.colorFaces(mesh, mode, model.material(),
                                   model.color(), light, cam[0], self._terrainLut())
            vcol = self._scatterWallAware(mesh, fc.astype(np.float64))

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

    # ---------------- CNC 刀路轨迹（cnc 模式，QPainter 叠加） ----------------
    _CNC_TOOLPATH_COLORS = (
        "#ff5252", "#4caf50", "#2196f3", "#9c27b0", "#ff9800",
        "#00bcd4", "#e91e63", "#8bc34a", "#ffc107", "#3f51b5",
    )

    def _drawCncToolpaths(self, painter, cam, w: float, h: float,
                          gl=None, mvp=None, W: int = 0, H: int = 0) -> None:
        """cnc 模式：把 CncState 中可见路径的刀头运动轨迹画到 3D 视口。

        与 2D 叠加层共用同一份轨迹几何（cnc_toolpath_geom），保证两个视图
        轨迹一致；坐标 (x, y, z) 直接作为世界坐标投影——深度沿 3D Z 轴。
        颜色/线型与 2D 完全一致：进给实线、快速虚线、箭头+孔位圆标记固定
        屏幕尺寸。

        GPU 可用时（gl/mvp 传入）：路径是**固定几何**——折线一次性上传为
        顶点缓冲，此后每帧只更新 MVP uniform + 一次 draw call，旋转/缩放
        视图零逐点计算（顶点变换由 GPU 并行完成）。仅模拟播放中的当前
        路径仍走 QPainter 分段着色（已走/未走），箭头/孔位标记始终 QPainter。
        """
        if self._canvasMode != "cnc":
            return
        try:
            from core.cnc.state import CncState
            from ui.view_2d.cnc_toolpath_geom import build_toolpath_geometry
            state = CncState.instance()
            entries = state.pathEntries
        except Exception:
            return
        # 实体模拟播放中：已走过的路径用高亮色（琥珀黄），未走的保持原色
        sim_pos = None
        if state.simLine >= 0 and state.simPosition is not None:
            sim_pos = state.simInterpPos()
        done_col = QColor("#ffd54f")
        gpu_ok = gl is not None and mvp is not None and W > 0 and H > 0
        gpu_items = []       # (key, lines) —— 固定几何，GPU 画
        sim_gpu_items = []   # (key, g, col, raw_col) —— 模拟当前路径，GPU 画
        qpaint_items = []    # (g, col, is_sim) —— QPainter 画
        arrow_items = []     # (g, col) —— 箭头/孔位标记
        for idx, entry in enumerate(entries):
            if not entry.visible:
                continue
            col = QColor(entry.color or
                         self._CNC_TOOLPATH_COLORS[idx % len(self._CNC_TOOLPATH_COLORS)])
            try:
                g = build_toolpath_geometry(entry)
            except Exception:
                continue
            if sim_pos is not None:
                # 刀尖不在该路径附近（距离阈值外）→ 整条保持原色，
                # 避免远处刀尖投影到未走路径中段造成高亮分界乱动、闪烁
                near = self._cncNearestDistNP(
                    self._cncCatNP(g, "feed_h", "rapid_h"), sim_pos,
                    g.get("bbox"))
                if near <= 6.0:
                    if gpu_ok:
                        # 当前路径：GPU 整条基色 + 已走前缀高亮（顶点前缀
                        # 渲染，每帧 Python 只做一次二分找分割点——密集
                        # 精加工路径几万点也零压力）
                        key = ("cncpath", id(entry), id(entry.toolpaths))
                        sim_gpu_items.append((key, g, col, entry.color))
                        arrow_items.append((g, col))
                        continue
                    qpaint_items.append((g, col, True))
                    arrow_items.append((g, col))
                    continue
            if gpu_ok:
                key = ("cncpath", id(entry), id(entry.toolpaths))
                lines = g.get("gpu_lines")
                if lines is None or g.get("gpu_col") != entry.color:
                    lines = self._cncGpuLines(g, col)
                    g["gpu_lines"] = lines
                    g["gpu_col"] = entry.color
                gpu_items.append((key, lines))
            else:
                qpaint_items.append((g, col, False))
            arrow_items.append((g, col))
        # ---- GPU：一次上传（缓存命中零开销）+ 一次 draw call/路径 ----
        if gpu_items:
            try:
                painter.beginNativePainting()
                for key, lines in gpu_items:
                    gl.upload_paths(key, lines)
                    gl.draw_paths(key, mvp, W, H)
            except Exception:
                pass
            finally:
                try:
                    painter.endNativePainting()
                except Exception:
                    pass
        # ---- GPU：模拟播放中的当前路径（整条基色 + 已走前缀高亮） ----
        if sim_gpu_items:
            try:
                painter.beginNativePainting()
                for key, g, col, raw_col in sim_gpu_items:
                    self._cncDrawSimPathGPU(gl, mvp, W, H, key, g, col,
                                            raw_col, done_col, sim_pos)
            except Exception:
                pass
            finally:
                try:
                    painter.endNativePainting()
                except Exception:
                    pass
        # ---- QPainter：无 GPU 的路径 + 模拟播放中的当前路径（分段着色） ----
        for g, col, is_sim in qpaint_items:
            if is_sim:
                # 刀尖在该路径上：已走/未走分段着色
                self._cncDrawPolylineProgress(
                    painter, cam, w, h, g["feed_h"], col, done_col, sim_pos,
                    2.0, False, g.get("feed_cum"))
                self._cncDrawSegmentsProgress(
                    painter, cam, w, h, g["feed_v"], col, done_col, sim_pos, 2.0, False)
                if g["rapid_h"]:
                    rc = QColor(col)
                    rc.setAlpha(110)
                    drc = QColor(done_col)
                    drc.setAlpha(110)
                    self._cncDrawPolylineProgress(
                        painter, cam, w, h, g["rapid_h"], rc, drc, sim_pos, 1.2, True)
                if g["rapid_v"]:
                    rc = QColor(col)
                    rc.setAlpha(140)
                    drc = QColor(done_col)
                    drc.setAlpha(140)
                    self._cncDrawSegmentsProgress(
                        painter, cam, w, h, g["rapid_v"], rc, drc, sim_pos, 1.2, True)
            else:
                self._cncDrawPolyline(painter, cam, w, h, g["feed_h"], col, 2.0, False)
                self._cncDrawSegments(painter, cam, w, h, g["feed_v"], col, 2.0, False)
                if g["rapid_h"]:
                    rc = QColor(col)
                    rc.setAlpha(110)
                    self._cncDrawPolyline(painter, cam, w, h, g["rapid_h"], rc, 1.2, True)
                if g["rapid_v"]:
                    rc = QColor(col)
                    rc.setAlpha(140)
                    self._cncDrawSegments(painter, cam, w, h, g["rapid_v"], rc, 1.2, True)
        # ---- 箭头（起始下刀/结束抬刀）与孔位标记 ----
        for g, col in arrow_items:
            for ax, ay, az, _dx, _dy, dz in g["arrows"]:
                if g["arrows"][0] == (ax, ay, az, _dx, _dy, dz):
                    arrow_col = QColor("#ff5252")   # 起始下刀箭头
                else:
                    arrow_col = QColor("#4caf50")   # 结束抬刀箭头
                self._cncDrawArrow(painter, cam, w, h,
                                   (ax, ay, az), (ax, ay, az - dz * 3.0), arrow_col)
            if not g["has_horizontal"] and g["marks"]:
                for mx, my in g["marks"]:
                    self._cncDrawDrillMark(painter, cam, w, h, (mx, my, 0.0), col)

    def _cncGpuLines(self, g, col):
        """把一条路径的几何转成 GPU 顶点组：(折线, 颜色0-1, 虚线或None)。"""
        cf = (col.redF(), col.greenF(), col.blueF(), 1.0)
        rc = (col.redF(), col.greenF(), col.blueF(), 110.0 / 255.0)
        rv = (col.redF(), col.greenF(), col.blueF(), 140.0 / 255.0)
        lines = []
        if g["feed_h"]:
            lines.append((g["feed_h"], cf, None))
        for seg in g["feed_v"]:
            lines.append((seg, cf, None))
        if g["rapid_h"]:
            lines.append((g["rapid_h"], rc, (3.0, 2.0)))
        for seg in g["rapid_v"]:
            lines.append((seg, rv, (3.0, 2.0)))
        return lines


    def _cncNP(self, g, key: str):
        """几何折线的 numpy 缓存（g[key] → (n,3) float64），重建时按长度失效。"""
        src = g.get(key) or []
        arr = g.get(key + "_np")
        if arr is None or len(arr) != len(src):
            arr = np.asarray(src, dtype=np.float64).reshape(-1, 3) if src \
                else np.zeros((0, 3))
            g[key + "_np"] = arr
        return arr

    def _cncCatNP(self, g, *keys):
        """多条折线拼接的 numpy 缓存（粗筛/投影用）。"""
        if len(keys) == 1:
            return self._cncNP(g, keys[0])
        ck = "cat_np_" + "+".join(keys)
        arr = g.get(ck)
        lens = tuple(len(g.get(k) or ()) for k in keys)
        if arr is None or g.get(ck + "_lens") != lens:
            parts = [self._cncNP(g, k) for k in keys]
            arr = np.concatenate(parts, 0) if parts else np.zeros((0, 3))
            g[ck] = arr
            g[ck + "_lens"] = lens
        return arr

    def _cncNearestDistNP(self, arr, sim_pos, bbox=None) -> float:
        """折线到刀尖的最短 3D 距离（numpy 向量化点到线段）。"""
        if arr is None or len(arr) == 0:
            return 1e18
        if bbox is not None:
            bx0, by0, bz0, bx1, by1, bz1 = bbox
            sx, sy, sz = sim_pos[0], sim_pos[1], sim_pos[2]
            dx = max(bx0 - sx, 0.0, sx - bx1)
            dy = max(by0 - sy, 0.0, sy - by1)
            dz = max(bz0 - sz, 0.0, sz - bz1)
            if math.sqrt(dx * dx + dy * dy + dz * dz) > 6.0:
                return 1e18
        if len(arr) == 1:
            return float(np.sqrt(((arr[0] - np.asarray(sim_pos[:3])) ** 2).sum()))
        a = arr[:-1]
        ab = arr[1:] - a
        s = np.asarray(sim_pos[:3], dtype=np.float64)
        L2 = (ab * ab).sum(1)
        ok = L2 > 0.0
        t = np.zeros(len(a))
        t[ok] = ((s - a[ok]) * ab[ok]).sum(1) / L2[ok]
        np.clip(t, 0.0, 1.0, out=t)
        d = a + ab * t[:, None] - s
        return float(np.sqrt((d * d).sum(1).min()))

    def _cncPolylineParamNP(self, g, key: str, sim_pos) -> float:
        """sim_pos 在折线上的投影参数 t∈[0,1]（numpy 向量化，按累计长度）。"""
        arr = self._cncNP(g, key)
        if len(arr) < 2:
            return 0.0
        cum = g.get(key + "_cum")
        if cum is None and key == "feed_h":
            cum = g.get("feed_cum")
        if cum is None or len(cum) < len(arr):
            seglen = np.sqrt(((arr[1:] - arr[:-1]) ** 2).sum(1))
            cum = np.concatenate([[0.0], np.cumsum(seglen)])
            g[key + "_cum"] = cum
        total = float(cum[-1])
        if total <= 0.0:
            return 0.0
        a = arr[:-1]
        ab = arr[1:] - a
        s = np.asarray(sim_pos[:3], dtype=np.float64)
        L2 = np.maximum((ab * ab).sum(1), 1e-30)
        t = np.clip(((s - a) * ab).sum(1) / L2, 0.0, 1.0)
        p = a + ab * t[:, None]
        i = int(np.argmin(((p - s) ** 2).sum(1)))
        return float((cum[i] + math.sqrt(L2[i]) * t[i]) / total)

    def _cncProgressIndex(self, g, key: str, sim_pos) -> int:
        """已走折线前缀的点索引（二分，O(log n)，单调只增）。

        收刀回程/结束时刀尖折返，投影参数会回退，但"已走过的路径"不能
        变回原色——用单调最大值锁定；simLine 回退（拖进度条后退/重新
        播放）时清空重置。
        """
        pts = g.get(key) or []
        if len(pts) < 2:
            return 0
        t = self._cncPolylineParamNP(g, key, sim_pos)
        cum = g.get(key + "_cum")
        if cum is None and key == "feed_h":
            cum = g.get("feed_cum")
        if cum is None:
            return len(pts) - 1 if t >= 1.0 else 0
        import bisect
        cut = max(0.0, min(1.0, t)) * float(cum[-1])
        i = bisect.bisect_left(cum, cut)
        i = max(0, min(len(pts) - 1, i))
        # 单调推进锁存（seek 后退 / 重新播放时按 simLine 回退重置）
        try:
            from core.cnc.state import CncState
            cst = CncState.instance()
            line = cst.simLine
            cutting = bool(cst.simCutting)
        except Exception:
            cst = None
            line = None
            cutting = True
        if line is not None:
            if getattr(self, "_cncMonoLine", None) is None or \
                    line < self._cncMonoLine:
                self._cncMonoMax = {}
            self._cncMonoLine = line
        mid = (id(g), key)
        mono = getattr(self, "_cncMonoMax", None)
        if mono is None:
            mono = self._cncMonoMax = {}
        # 只有切削段（G1/G2/G3）才推进"已走"进度：快移接近下刀点时刀尖
        # 在路径上方平移，投影会落到路径中段，造成未切削就提前变黄。
        if cutting:
            if i < mono.get(mid, 0):
                i = mono[mid]
            else:
                mono[mid] = i
        else:
            i = mono.get(mid, 0)
        return i

    def _cncDrawSimPathGPU(self, gl, mvp, W, H, key, g, col, raw_col,
                           done_col, sim_pos) -> None:
        """模拟播放中当前路径的 GPU 渲染：整条基色 + 已走前缀高亮。

        基色整条与"非播放态"共用同一份顶点缓存（每帧 1 个 draw call、
        零重建）；已走高亮是只含主折线的第二份缓存，每帧按顶点前缀
        渲染（draw_paths count=已走段数×2）——几万点的密集路径每帧
        Python 成本也只有一次 O(log n) 二分。
        """
        lines = g.get("gpu_lines")
        if lines is None or g.get("gpu_col") != raw_col:
            lines = self._cncGpuLines(g, col)
            g["gpu_lines"] = lines
            g["gpu_col"] = raw_col
        gl.upload_paths(key, lines)
        gl.draw_paths(key, mvp, W, H)
        pts = g.get("feed_h") or []
        if len(pts) < 2:
            return
        dl = g.get("gpu_done_lines")
        if dl is None:
            dl = [(pts, (done_col.redF(), done_col.greenF(),
                         done_col.blueF(), 1.0), None)]
            g["gpu_done_lines"] = dl
        dkey = (key[0] + "_done", key[1], key[2])
        gl.upload_paths(dkey, dl)
        i = self._cncProgressIndex(g, "feed_h", sim_pos)
        gl.draw_paths(dkey, mvp, W, H, count=max(0, i) * 2)

    def _cncDist3(self, a, b) -> float:
        return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + \
               ((a[2] - b[2]) ** 2 if len(a) > 2 and len(b) > 2 else 0.0)

    def _cncPolylineParam(self, pts, sim_pos, cum=None) -> float:
        """sim_pos 在折线上的投影参数 t∈[0,1]（按 3D 累计长度，连续不跳变）。

        用 3D 距离：螺旋下刀每圈 XY 回到起点但 Z 加深，XY 投影会跳回 0；
        3D 投影沿累计长度单调推进，稳定不闪。
        """
        import math
        if cum is not None:
            total = cum[-1]
            segs = [cum[i + 1] - cum[i] for i in range(len(pts) - 1)]
        else:
            total = 0.0
            segs = []
            for i in range(len(pts) - 1):
                d = math.sqrt(self._cncDist3(pts[i], pts[i + 1]))
                segs.append(d)
                total += d
        if total <= 0.0:
            return 0.0
        best_t = 0.0
        best_d = 1e18
        acc = 0.0
        sx, sy, sz = sim_pos[0], sim_pos[1], sim_pos[2] if len(sim_pos) > 2 else 0.0
        for i in range(len(pts) - 1):
            seg_len = segs[i]
            if seg_len <= 0.0:
                continue
            a, b = pts[i], pts[i + 1]
            dx = b[0] - a[0]
            dy = b[1] - a[1]
            dz = (b[2] - a[2]) if len(a) > 2 and len(b) > 2 else 0.0
            t = ((sx - a[0]) * dx + (sy - a[1]) * dy +
                 (sz - a[2]) * dz if len(a) > 2 else
                 (sx - a[0]) * dx + (sy - a[1]) * dy) / (seg_len * seg_len)
            t = max(0.0, min(1.0, t))
            px = a[0] + dx * t
            py = a[1] + dy * t
            pz = (a[2] + dz * t) if len(a) > 2 else 0.0
            d = (px - sx) ** 2 + (py - sy) ** 2 + (pz - sz) ** 2
            if d < best_d:
                best_d = d
                best_t = (acc + seg_len * t) / total
            acc += seg_len
        return best_t

    def _cncSplitByParam(self, pts, t: float, cum=None):
        """按累计长度参数 t 把折线分为 [已走][未走]（3D 长度，t 连续→分界稳定）。

        cum：几何缓存的累计长度数组（可选），命中时二分 O(log n)。
        """
        if not pts:
            return [], []
        if cum is not None:
            total = cum[-1]
            if total <= 0.0:
                return pts, []
            cut = max(0.0, min(1.0, t)) * total
            import bisect
            i = bisect.bisect_left(cum, cut)
            i = max(0, min(len(pts) - 1, i))
            return pts[:i + 1], pts[i:]
        import math
        total = 0.0
        for i in range(len(pts) - 1):
            total += math.sqrt(self._cncDist3(pts[i], pts[i + 1]))
        if total <= 0.0:
            return pts, []
        cut = max(0.0, min(1.0, t)) * total
        acc = 0.0
        for i in range(len(pts) - 1):
            seg = math.sqrt(self._cncDist3(pts[i], pts[i + 1]))
            if acc + seg > cut + 1e-9:
                return pts[:i + 1], pts[i:]
            acc += seg
        return pts, []

    def _cncNearestDist(self, pts, sim_pos, bbox=None) -> float:
        """折线到刀尖的最短 3D 距离（点到线段，而非顶点）。

        bbox：几何缓存的 (minx,miny,minz,maxx,maxy,maxz)，先做 O(1) 粗筛，
        刀尖离包围盒超过 6mm 直接返回大数，跳过逐段距离计算。
        """
        import math
        if not pts:
            return 1e18
        if bbox is not None:
            bx0, by0, bz0, bx1, by1, bz1 = bbox
            sx, sy = sim_pos[0], sim_pos[1]
            sz = sim_pos[2] if len(sim_pos) > 2 else 0.0
            dx = max(bx0 - sx, 0.0, sx - bx1)
            dy = max(by0 - sy, 0.0, sy - by1)
            dz = max(bz0 - sz, 0.0, sz - bz1)
            if math.sqrt(dx * dx + dy * dy + dz * dz) > 6.0:
                return 1e18
        if len(pts) == 1:
            return math.sqrt(self._cncDist3(pts[0], sim_pos))
        bd = 1e18
        sx, sy = sim_pos[0], sim_pos[1]
        sz = sim_pos[2] if len(sim_pos) > 2 else 0.0
        for i in range(len(pts) - 1):
            a, b = pts[i], pts[i + 1]
            dx = b[0] - a[0]
            dy = b[1] - a[1]
            dz = (b[2] - a[2]) if len(a) > 2 and len(b) > 2 else 0.0
            az = a[2] if len(a) > 2 else 0.0
            L2 = dx * dx + dy * dy + dz * dz
            if L2 <= 0.0:
                continue
            t = ((sx - a[0]) * dx + (sy - a[1]) * dy + (sz - az) * dz) / L2
            t = max(0.0, min(1.0, t))
            px = a[0] + dx * t
            py = a[1] + dy * t
            pz = az + dz * t
            d = math.sqrt((px - sx) ** 2 + (py - sy) ** 2 + (pz - sz) ** 2)
            if d < bd:
                bd = d
        return bd

    def _cncDrawPolylineProgress(self, painter, cam, w, h, pts, base_col, done_col,
                                 sim_pos, width, dash, cum=None) -> None:
        """折线按"沿路径已走长度"连续分界着色，避免最近顶点跳变导致闪烁。"""
        if not pts:
            return
        t = self._cncPolylineParam(pts, sim_pos, cum)
        done, todo = self._cncSplitByParam(pts, t, cum)
        if len(done) >= 2:
            self._cncDrawPolyline(painter, cam, w, h, done, done_col, width, dash)
        if len(todo) >= 2:
            self._cncDrawPolyline(painter, cam, w, h, todo, base_col, width, dash)

    def _cncDrawSegmentsProgress(self, painter, cam, w, h, segs, base_col, done_col,
                                 sim_pos, width, dash) -> None:
        """独立段按"终点是否已被刀具越过"分组着色（终点比起点更近=已走）。"""
        d_segs = []
        t_segs = []
        for a, b in segs:
            if self._cncDist3(b, sim_pos) < self._cncDist3(a, sim_pos):
                d_segs.append((a, b))
            else:
                t_segs.append((a, b))
        if d_segs:
            self._cncDrawSegments(painter, cam, w, h, d_segs, done_col, width, dash)
        if t_segs:
            self._cncDrawSegments(painter, cam, w, h, t_segs, base_col, width, dash)

    def _cncDrawPolyline(self, painter, cam, w, h, pts, color, width, dash):
        if not pts:
            return
        pen = QPen(color, width)
        if dash:
            pen.setStyle(Qt.DashLine)
        pen.setCosmetic(True)
        painter.setPen(pen)
        # 每点只投影一次，整体构建 QPainterPath 后一次 drawPath 提交，
        # 避免每帧为几千个线段分别构造 QLineF 的 Python 开销。
        path = QPainterPath()
        started = False
        for p in pts:
            r = self._project(p, cam, w, h)
            if r is None:
                started = False
                continue
            if started:
                path.lineTo(r[0], r[1])
            else:
                path.moveTo(r[0], r[1])
                started = True
        if not path.isEmpty():
            painter.drawPath(path)

    def _cncDrawSegments(self, painter, cam, w, h, segs, color, width, dash):
        if not segs:
            return
        pen = QPen(color, width)
        if dash:
            pen.setStyle(Qt.DashLine)
        pen.setCosmetic(True)
        painter.setPen(pen)
        lines = []
        for (a, b) in segs:
            r = self._projectSegment(a, b, cam, w, h)
            if r is not None:
                lines.append(QLineF(QPointF(r[0][0], r[0][1]), QPointF(r[1][0], r[1][1])))
        if lines:
            painter.drawLines(lines)

    def _cncDrawArrow(self, painter, cam, w, h, tip_pt, tail_pt, color):
        """段尾方向箭头：尖端沿（段首→段尾）屏幕方向延伸，固定屏幕尺寸。

        加大尺寸 + 白色描边，与任意路径颜色都明确区分。
        """
        pa = self._project(tip_pt, cam, w, h)
        pb = self._project(tail_pt, cam, w, h)
        if pa is None or pb is None:
            return
        dx, dy = pa[0] - pb[0], pa[1] - pb[1]
        n = math.hypot(dx, dy)
        if n < 1e-6:
            return
        ux, uy = dx / n, dy / n
        px, py = pa[0], pa[1]
        size = 16.0      # 箭头长度
        half = 6.5       # 箭头半宽
        tip = QPointF(px + ux * size, py + uy * size)
        bx = QPointF(px - uy * half, py + ux * half)
        by = QPointF(px + uy * half, py - ux * half)
        # 白色描边 + 原色填充：箭头醒目且不与路径颜色混淆
        pen = QPen(QColor("#ffffff"), 2.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(color)
        painter.drawPolygon(QPolygonF([tip, bx, by]))

    def _cncDrawDrillMark(self, painter, cam, w, h, pt, color):
        """钻孔孔位标记：外圈 + 实心中心，固定屏幕尺寸。"""
        p = self._project(pt, cam, w, h)
        if p is None:
            return
        pen = QPen(color, 1.2)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(QPointF(p[0], p[1]), 6.0, 6.0)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(QPointF(p[0], p[1]), 2.0, 2.0)

    # ---------------- CNC 刀具显示（cnc 模式，实体模拟预览） ----------------
    def _drawCncTools(self, painter, cam, w: float, h: float) -> None:
        """cnc 模式且"显示刀具"勾选时：按刀具类型把当前选用刀具画到画布。

        实体填充（与刀具表图标同逻辑）：上段刀柄=固定直径灰色矩形，
        下段刀刃=按刀型填充——平底=矩形、球头=矩形+半球尖、锥度=梯形
        （从柄宽收窄到刃径，刀柄+锥度分两段）。刀具沿 X 排开，立在毛坯
        顶面，刀尖接触工件、刀体向上。
        """
        if self._canvasMode != "cnc":
            return
        try:
            from core.cnc.state import CncState
            state = CncState.instance()
        except Exception:
            return
        if not state.showTools or not state.toolControllers:
            return
        tip_z = 0.0
        try:
            tip_z = float(state.stock.thickness)
        except Exception:
            pass
        # 模拟播放中：刀具实体跟随当前 G 代码坐标真实移动（画当前刀具）；
        # 未播放：沿 X 排开显示全部刀具。
        sim_pos = None
        if state.simLine >= 0 and state.simPosition is not None:
            sim_pos = state.simInterpPos()
        if sim_pos is not None:
            try:
                tc0 = state.toolControllers[0]
                col = QColor(self._CNC_TOOLPATH_COLORS[0 % len(self._CNC_TOOLPATH_COLORS)])
                # 刀尖 G 代码 Z 直接作为世界坐标（Z0 = 世界 0 = 模型顶面，
                # 与路径显示 build_toolpath_geometry 完全一致，不加毛坯
                # 居中偏移——否则刀具被抬高 t/2，一直在毛坯上方水平走）。
                self._drawToolSolid(painter, cam, w, h, tc0.tool,
                                    sim_pos[0], sim_pos[1], sim_pos[2], col)
            except Exception:
                pass
            return
        for i, tc in enumerate(state.toolControllers):
            tool = tc.tool
            col = QColor(self._CNC_TOOLPATH_COLORS[i % len(self._CNC_TOOLPATH_COLORS)])
            try:
                self._drawToolSolid(painter, cam, w, h, tool, i * 32.0, 0.0, tip_z, col)
            except Exception:
                continue

    def _drawToolSolid(self, painter, cam, w, h, tool, cx, cy, tip_z, color) -> None:
        """画一把刀具的 3D 实体（真圆柱/锥台网格）。

        与刀具表图标同逻辑但立体：刀柄=固定直径灰色圆柱，刀刃=彩色圆柱
        （平底/球头/牛鼻）或锥台（锥度刀从柄宽收窄）。每个圆柱体用
        圆周 16 点投影：底部椭圆 + 侧面四边形条带（背面剔除）+ 顶部椭圆，
        任意旋转视角都呈现真实立体圆柱；球头刀在刀尖加半球网格。
        刀尖位于世界 (cx, cy, tip_z)，刀体向上。
        """
        from pymillcam.core.tools import ToolShape
        g = tool.geometry
        dia = float(g.get("diameter", 3.0))
        shank = float(g.get("shank_diameter", dia))
        total = float(g.get("total_length", 50.0))
        flute = float(g.get("flute_length", 15.0))
        taper = float(g.get("taper_angle", 0.0))
        shape = tool.shape
        N = 16

        def ring3(r, z):
            """世界坐标水平圆（16 点）。"""
            return [(cx + r * math.cos(2.0 * math.pi * k / N),
                     cy + r * math.sin(2.0 * math.pi * k / N),
                     tip_z + z) for k in range(N)]

        def projPts(pts):
            out = []
            for p in pts:
                q = self._project(p, cam, w, h)
                if q is None:
                    return None
                out.append(QPointF(q[0], q[1]))
            return out

        def fillPoly(pts, fill, border=None):
            if pts is None or len(pts) < 3:
                return
            pen = QPen(border if border is not None else fill.darker(140), 1.0)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(fill)
            painter.drawPolygon(QPolygonF(pts))

        def quadVisible(q0, q1, q2):
            """屏幕绕序判定（逆时针=正面，剔除背面）。"""
            e1x = q1.x() - q0.x()
            e1y = q1.y() - q0.y()
            e2x = q2.x() - q0.x()
            e2y = q2.y() - q0.y()
            return (e1x * e2y - e1y * e2x) < 0

        def drawCylinder(r_bot, z_bot, r_top, z_top, fill, border):
            """画一个圆柱/锥台：底椭圆 → 侧面条带（剔除背面）→ 顶椭圆。"""
            B = projPts(ring3(r_bot, z_bot))
            T = projPts(ring3(r_top, z_top))
            if B is None or T is None:
                return
            fillPoly(B, fill.darker(155), border)   # 底面（暗）
            for k in range(N):
                q0, q1 = B[k], B[(k + 1) % N]
                q3, q2 = T[k], T[(k + 1) % N]
                if not quadVisible(q0, q1, q2):
                    continue
                grad = QLinearGradient(q0, q1)
                grad.setColorAt(0.0, fill.darker(160))
                grad.setColorAt(0.5, fill)
                grad.setColorAt(1.0, fill.lighter(150))
                pen = QPen(border, 1.0)
                pen.setCosmetic(True)
                painter.setPen(pen)
                painter.setBrush(QBrush(grad))
                painter.drawPolygon(QPolygonF([q0, q1, q2, q3]))
            fillPoly(T, fill.lighter(118), border)   # 顶面（亮）

        # ---- 尺寸 ----
        # 刀柄：固定直径(shank/2) 直圆柱。
        # 无锥度刀：刀刃=刀径直圆柱，刀刃与刀柄之间加一段过渡斜坡
        #   （刃径→柄径，刀径≠柄径时的中间连接）。
        # 锥度刀：刀体本身就是锥形（顶部=柄径 → 刀尖=刀径），柄径之后
        #   直接就是刀的锥度，不再需要过渡斜坡。
        shank_r = shank / 2.0
        r_flute = dia / 2.0
        r_bot = r_flute                    # 刀尖半径（球头/牛鼻用）
        is_taper = taper > 0
        neck = min(3.0, max(0.0, total - flute))
        if neck < 0.5:
            neck = 0.0
        neck_eff = 0.0 if is_taper else neck   # 过渡斜坡仅用于无锥度刀

        # 1) 刀体（0..flute，彩色）
        if is_taper:
            # 锥度刀：顶部=柄径（与刀柄无缝衔接）→ 刀尖=刀径
            top_r = max(shank_r, r_flute)
            drawCylinder(r_bot, 0.0, top_r, flute,
                         color, QColor(color).darker(130))
        else:
            # 无锥度刀：刀径直圆柱
            drawCylinder(r_bot, 0.0, r_flute, flute,
                         color, QColor(color).darker(130))
        # 2) 过渡斜坡（仅无锥度刀：刃径 → 柄径 小段锥台）
        if neck_eff > 0:
            drawCylinder(r_flute, flute, shank_r, flute + neck_eff,
                         color.darker(125), QColor(color).darker(145))
        # 3) 刀柄直圆柱（flute+neck_eff..total，灰，半径恒定）
        drawCylinder(shank_r, flute + neck_eff, shank_r, total,
                     QColor("#9a9a9a"), QColor("#6e6e6e"))
        # 3) 刀尖形状
        if shape == ToolShape.BALLNOSE:
            # 半球网格（刀尖在 tip_z - r_bot）：两层弧 + 尖端三角形
            def hemi(th):
                c, s = math.cos(th), math.sin(th)
                return [(cx + r_bot * c * math.cos(2.0 * math.pi * k / N),
                         cy + r_bot * c * math.sin(2.0 * math.pi * k / N),
                         tip_z - r_bot * s) for k in range(N)]
            R1 = projPts(hemi(math.radians(40.0)))
            R2 = projPts(hemi(math.radians(80.0)))
            S = self._project((cx, cy, tip_z - r_bot), cam, w, h)
            if R1 is not None and R2 is not None and S is not None:
                S = QPointF(S[0], S[1])
                B0 = projPts(ring3(r_bot, 0.0))
                border_c = QColor(color).darker(130)
                for k in range(N):
                    q0, q1 = B0[k], B0[(k + 1) % N]
                    q3, q2 = R1[k], R1[(k + 1) % N]
                    if quadVisible(q0, q1, q2):
                        grad = QLinearGradient(q0, q1)
                        grad.setColorAt(0.0, color.darker(170))
                        grad.setColorAt(1.0, color.lighter(130))
                        pen = QPen(border_c, 1.0)
                        pen.setCosmetic(True)
                        painter.setPen(pen)
                        painter.setBrush(QBrush(grad))
                        painter.drawPolygon(QPolygonF([q0, q1, q2, q3]))
                    q0, q1 = R1[k], R1[(k + 1) % N]
                    q3, q2 = R2[k], R2[(k + 1) % N]
                    if quadVisible(q0, q1, q2):
                        grad = QLinearGradient(q0, q1)
                        grad.setColorAt(0.0, color.darker(180))
                        grad.setColorAt(1.0, color.lighter(120))
                        pen = QPen(border_c, 1.0)
                        pen.setCosmetic(True)
                        painter.setPen(pen)
                        painter.setBrush(QBrush(grad))
                        painter.drawPolygon(QPolygonF([q0, q1, q2, q3]))
                for k in range(N):
                    q0, q1 = R2[k], R2[(k + 1) % N]
                    if quadVisible(q0, q1, S):
                        painter.setPen(Qt.NoPen)
                        painter.setBrush(color.darker(150))
                        painter.drawPolygon(QPolygonF([q0, q1, S]))
        elif shape == ToolShape.BULLNOSE:
            # 牛鼻：底部圆角描边（圆角半径示意）
            pen = QPen(QColor(color).darker(130), 1.2)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            tip = self._project((cx, cy, tip_z), cam, w, h)
            if tip is not None:
                tipf = QPointF(tip[0], tip[1])
                rr = min(r_bot * 0.45, 1.8)
                for sgn in (1.0, -1.0):
                    c2 = QPointF(tipf.x(), tipf.y())
                    painter.drawArc(QRectF(c2.x() - rr, c2.y() - rr, 2.0 * rr, 2.0 * rr),
                                    90 * 16 if sgn > 0 else 0, 90 * 16)

    def _drawCncSimCursor(self, painter, cam, w: float, h: float) -> None:
        """模拟播放时：在画布当前位置画刀头游标（红圈 + 十字 + 小三角）。"""
        if self._canvasMode != "cnc":
            return
        try:
            from core.cnc.state import CncState
            state = CncState.instance()
        except Exception:
            return
        if state.simLine < 0 or not state.simPosition:
            return
        # 勾选"显示刀具"时由刀具实体指示位置，不再叠加游标
        if state.showTools:
            return
        p = self._project(state.simInterpPos(), cam, w, h)
        if p is None:
            return
        red = QColor("#ff1744")
        pen = QPen(red, 2.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(QPointF(p[0], p[1]), 7.0, 7.0)
        painter.drawLine(QPointF(p[0] - 11, p[1]), QPointF(p[0] + 11, p[1]))
        painter.drawLine(QPointF(p[0], p[1] - 11), QPointF(p[0], p[1] + 11))
        # 下方小三角（当前刀尖）
        tri = QPolygonF([
            QPointF(p[0] - 4, p[1] + 12),
            QPointF(p[0] + 4, p[1] + 12),
            QPointF(p[0], p[1] + 18),
        ])
        painter.setPen(Qt.NoPen)
        painter.setBrush(red)
        painter.drawPolygon(tri)

    @staticmethod
    def _affineFrom3(src, dst):
        """求把源三角形映射到目标三角形的仿射 QTransform（四角贴图用）。
        退化（共线/透视不可解）返回 None。"""
        try:
            A = [[s[0], s[1], 1.0] for s in src]
            bx = [d[0] for d in dst]
            by = [d[1] for d in dst]
            solx = np.linalg.solve(A, bx)
            soly = np.linalg.solve(A, by)
        except Exception:
            return None
        # QTransform(m11,m12,m13, m21,m22,m23, dx,dy,m33)：
        # x' = m11*x + m21*y + dx；y' = m12*x + m22*y + dy
        return QTransform(solx[0], soly[0], 0.0,
                          solx[1], soly[1], 0.0,
                          solx[2], soly[2], 1.0)

    def _drawImage3D(self, painter, obj, cam, w: float, h: float) -> None:
        """参考图片：世界四角投影到屏幕，按两个三角形仿射贴图。
        顶视时是精确的轴对齐贴图；透视下为分片仿射近似（参考底图足够）。
        选中时叠加主题色描边框。"""
        pm = obj.pixmap()
        if pm is None or pm.isNull():
            return
        sp = [self._project((p.x, p.y, p.z), cam, w, h)
              for p in obj.points()]
        if any(s is None for s in sp):
            return
        iw = max(float(pm.width()), 1.0)
        ih = max(float(pm.height()), 1.0)
        # 世界角序（Y 向上）：0=左下 1=右下 2=右上 3=左上
        bl, br, tr, tl = sp
        tris = [
            ((tl, tr, bl), ((0.0, 0.0), (iw, 0.0), (0.0, ih))),
            ((tr, br, bl), ((iw, 0.0), (iw, ih), (0.0, ih))),
        ]
        base = painter.transform()
        for dst, src in tris:
            t = self._affineFrom3(src, dst)
            if t is None:
                continue
            poly = QPolygonF([QPointF(p[0], p[1]) for p in dst])
            clip = QPainterPath()
            clip.addPolygon(poly)
            painter.save()
            painter.setClipPath(clip)
            painter.setTransform(base * t)
            # 注意：pm 是 QPixmap，必须用 drawPixmap（drawImage 只收 QImage）
            painter.drawPixmap(QPointF(0.0, 0.0), pm)
            painter.restore()
        if obj.isSelected():
            pen = QPen(QColor(theme.selectionColorFor(obj)), 2.0)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPolygon(QPolygonF([QPointF(p[0], p[1]) for p in sp]))

    def _drawObjects(self, painter, cam, w: float, h: float,
                     depthBuf=None, glBrepEdges: bool = False) -> None:
        """把文档对象投影绘制到 3D 视口。

        线宽/颜色与 2D 画布（base_item.BaseGraphicsItem）完全一致：普通态＝对象本色
        + lineWidth()；选中态＝高亮色 + max(宽+1.5, 3.0)。浮雕模式下也不额外加粗、
        不加描边——曲线可见性由“深度清除后置顶绘制”保证，无需特殊强调。

        depthBuf=(zbuf,W,H) 时曲线/点也做隐藏线消除（被实体遮挡不画）。

        depthBuf=(zbuf, W, H) 时传给 B-Rep/SOLID 边线绘制做隐藏线消除。
        """

        def stroke_width(obj, selected) -> float:
            w = obj.lineWidth()
            if obj.type() == ObjectType.POINT:
                w = max(w, 1.0) + 0.5      # 同 2D：点描边稍加粗保证可见
            if selected:
                w = max(w + 1.5, 3.0)      # 同 2D：选中加粗到至少 3px
            return w

        eye, _right, _up, fwd, _o = cam

        def _wd(p):
            # 深度 = 沿视线轴的正向距离（与 _project/_projectSegment、
            # GPU/软件深度图同一单位同符号：v = p - eye）——此前用
            # eye - p 符号相反，d<=zbuf+1 恒成立，曲线遮挡从未生效
            v = (p[0] - eye[0], p[1] - eye[1], p[2] - eye[2])
            return v[0] * fwd[0] + v[1] * fwd[1] + v[2] * fwd[2]

        def draw_lines(lines, pen):
            painter.setPen(pen)
            # 浮雕模式：曲线始终画在模型之上（不做深度剔除）——
            # 2D 曲线 z=0 会被浮雕模型表面完全遮挡，导致看不见
            if depthBuf is not None and self._canvasMode != "relief" and lines:
                # 模型遮挡曲线：被实体挡住的段**不画**（用户要求正常
                # 遮挡；透视开时 depthBuf=None 全部可见）
                visible, _hidden = self._filterHiddenLines(lines, depthBuf)
                if visible:
                    painter.drawLines([QLineF(QPointF(a, b), QPointF(c, d))
                                       for a, b, c, d in visible])
                return
            for ln in lines:
                painter.drawLine(QPointF(ln[0], ln[1]),
                                 QPointF(ln[2], ln[3]))

        # 参考图片先画（曲线/模型之下，同 2D 画布 z=-600 的垫底语义）。
        # 浮雕模式例外：曲面是 GL 原生绘制、先于本 QPainter 叠加层，图片若
        # 直接画会盖在模型上面。这里把模型屏幕范围挖空后再画图片，图片才
        # 真正位于模型下方（模型外的部分仍正常可见、可选中、可移动）。
        under_rect = None
        if self._canvasMode == "relief":
            tgt = self._reliefTarget()
            if tgt is not None and hasattr(tgt, "heightField"):
                under_rect = self._reliefScreenRect(tgt, cam, w, h)
        for obj in self._doc.visibleObjects():
            if obj.type() == ObjectType.IMAGE:
                painter.save()
                try:
                    if under_rect is not None:
                        reg = QRegion(0, 0, int(w), int(h))
                        reg -= QRegion(under_rect.toAlignedRect())
                        painter.setClipRegion(reg)
                    self._drawImage3D(painter, obj, cam, w, h)
                except Exception:
                    import traceback
                    traceback.print_exc()
                finally:
                    painter.restore()

        for obj in self._doc.visibleObjects():
            selected = obj.isSelected()
            hl = theme.selectionColorFor(obj) if selected else None
            col = QColor(hl) if hl else QColor(*obj.color())
            pen = QPen(col, stroke_width(obj, selected))
            pen.setCosmetic(True)
            if obj.type() == ObjectType.SOLID:
                # 挤出实体：半透明面（画家算法排序）+ 线框。
                # 仅 3D/CNC 画布模式绘制（浮雕/2D 模式不显示 3D 实体，同 BREP）。
                if self._canvasMode not in ("3d", "cnc"):
                    continue
                self._drawSolid(painter, obj, cam, w, h, depthBuf=depthBuf)
                continue
            if obj.type() == ObjectType.BREP:
                # B-Rep 实体：表面已光栅化（GPU 或软件），这里只画特征边线；
                # 仅 3D 画布模式绘制（浮雕/2D 模式不显示 3D 实体）。
                # 渲染模式（rendered）不画结构线——实体以纯表面着色呈现；
                # 选中时保留高亮边以便定位/编辑。
                # glBrepEdges=True：特征边已由 GPU 直绘（深度测试），跳过。
                if self._canvasMode not in ("3d", "cnc") or glBrepEdges:
                    continue
                if self._solidDisplayMode == "rendered" and not selected:
                    continue
                self._drawBrepEdges(painter, obj, cam, w, h,
                                    depthBuf=depthBuf)
                continue
            if obj.type() == ObjectType.POINT:
                pos = obj.position()
                p = self._project((pos.x, pos.y, pos.z), cam, w, h)
                if p is None:
                    continue
                if depthBuf is not None:
                    zbuf, zW, zH = depthBuf
                    sc = getattr(self, "_zbScale", 1.0)
                    ix, iy = int(p[0] * sc), int(p[1] * sc)
                    if (self._canvasMode != "relief"
                            and 0 <= iy < zH and 0 <= ix < zW \
                            and p[2] > zbuf[iy][ix] + 1.0):
                        continue    # 点被实体遮挡（浮雕模式置顶不剔除）
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
                            lines.append((a[0][0], a[0][1], a[1][0], a[1][1],
                                          _wd(spts[i]), _wd(spts[i + 1])))
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
                    lines.append((seg[0][0], seg[0][1], seg[1][0], seg[1][1],
                                  _wd(pts[i]), _wd(pts[(i + 1) % n])))
            draw_lines(lines, pen)

    # ---------------- B-Rep 表面：GPU 渲染（主路径） ----------------

    def _isStockObject(self, obj) -> bool:
        """对象是否为 CNC 毛坯：半透明显示（避免实心遮挡导入的模型/曲线）。"""
        try:
            from core.cnc.state import CncState
            st = CncState.instance()
            for o in st.stockObjects:
                if o.id() == obj.id():
                    return True
        except Exception:
            pass
        return False

    # ---------------- CNC 实体模拟（体素坯料） ----------------
    def _solidSimActive(self) -> bool:
        """实体模拟是否开启且有体素数据。"""
        try:
            from core.cnc.state import CncState
            st = CncState.instance()
            return bool(st.showSolidSim and st.simVoxel is not None)
        except Exception:
            return False

    def _drawSolidStockFrame(self, painter, cam, w: float, h: float) -> None:
        """实体模拟开启时画毛坯轮廓线框（深色虚线），坯料位置始终可见。"""
        if not self._solidSimActive():
            return
        try:
            from core.cnc.state import CncState
            st = CncState.instance()
            w2 = float(st.stock.width) * 0.5
            h2 = float(st.stock.height) * 0.5
            t2 = float(st.stock.thickness) * 0.5
            cs = [(-w2, -h2, -t2), (w2, -h2, -t2), (w2, h2, -t2),
                  (-w2, h2, -t2), (-w2, -h2, t2), (w2, -h2, t2),
                  (w2, h2, t2), (-w2, h2, t2)]
            edges = [(0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6),
                     (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7)]
            pen = QPen(QColor(84, 96, 112, 220), 1.4)
            pen.setCosmetic(True)
            pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            for a, b in edges:
                pa = self._project(cs[a], cam, w, h)
                pb = self._project(cs[b], cam, w, h)
                if pa is not None and pb is not None:
                    painter.drawLine(QPointF(pa[0], pa[1]),
                                     QPointF(pb[0], pb[1]))
        except Exception:
            pass

    def _drawCncStockGL(self, mvp, eye, light, W, H) -> None:
        """实体模拟坯料：高度场纹理位移渲染（GPU 主路径专用）。

        高度场 (vs.h) 作为 R32F 纹理上传（128KB 级），网格静态复用；
        法线/双色切削/深度阴影/金属高光在片元着色器逐像素计算——
        曲面由纹理双线性插值连续平滑，无格子台阶，且每 tick 不再
        重建/上传几十万顶点网格。
        """
        from core.cnc.state import CncState
        st = CncState.instance()
        vs = st.simVoxel
        if vs is None or getattr(vs, "h", None) is None:
            return
        w = float(st.stock.width)
        h_ = float(st.stock.height)
        t = float(st.stock.thickness)
        self._gl.draw_stock(
            ("cncstock", id(vs)), vs.h,
            x0=-w * 0.5, y0=-h_ * 0.5, wx=w, wy=h_,
            z_off=t * 0.5, z_top=t * 0.5, thick=t,
            resx=vs.res_x, resy=vs.res_y, resz=vs.res_z,
            mvp=mvp, eye=eye, light=light, W=W, H=H)

    def _cncVoxelEnt(self):
        """体素坯料 → B-Rep 渲染 ent (pos,nrm,base,alpha)（毛坯系→世界系居中）。

        性能：坯料走 **2.5D 高度场网格**（top_mesh：顶面+侧墙，顶点约格数
        *6 ≈ 1 万）——由模拟逻辑 tick 预提取缓存（vs.surf_cache，50ms 一次），
        渲染帧只读缓存做偏移，每帧 GPU 上传仅亚兆级。相比全 3D 表面网格
        （6.9 万顶点）上传量降 7 倍——实体模拟卡顿的根治方案。
        暂停/拖进度条等未预提取场景按需兜底提取。
        """
        try:
            from core.cnc.state import CncState
            st = CncState.instance()
            vs = st.simVoxel
            if vs is None:
                return None
            cache = getattr(vs, "surf_cache", None)
            if cache is not None:
                surf = cache[1]
            else:
                surf = vs.top_mesh()
                if surf is not None:
                    vs.surf_cache = (int(getattr(vs, "revision", 0)), surf)
            if surf is None:
                return None
            # ent 缓存：surf 对象引用未变（逻辑 tick 未重提取）→ 直接复用
            ec = getattr(self, "_voxEntCache", None)
            if ec is not None and ec[0] is surf:
                return ec[1]
            pos, nrm = surf
            pos = pos.copy()
            pos[:, 0] -= float(st.stock.width) * 0.5
            pos[:, 1] -= float(st.stock.height) * 0.5
            pos[:, 2] -= float(st.stock.thickness) * 0.5
            # 三色坯料（逐顶点）：未切坯料 = 石板蓝（比画布深、比切削面
            # 暗，轮廓一眼可辨）；切削底面 = 银白（刀痕最亮）；腔壁 = 暖
            # 灰中间调（色相与两者都不同，深度感来自明暗三级差）。
            t = float(st.stock.thickness)
            # 切深超过半层才算"已切"着色：覆盖率抗锯齿在刃缘留有浅于
            # 半层的过渡格，保持坯料色，避免雕刻区周围出现灰色细环
            vs = st.simVoxel
            eps = max(0.05, 0.5 * getattr(vs, "res_z", 0.3))
            cut = pos[:, 2] < t * 0.5 - eps   # 居中后未切顶面 z=+t/2
            base_arr = np.tile(np.asarray((96, 110, 132), dtype=np.float32),
                               (pos.shape[0], 1))
            floor = cut & (nrm[:, 2] > 0.7)   # 切削底面：最亮
            wall = cut & (nrm[:, 2] <= 0.7)   # 腔壁/陡坎：中间调
            base_arr[floor] = (222, 226, 232)
            base_arr[wall] = (162, 158, 148)
            # 深度阴影（近似环境光遮蔽）：切得越深越暗——凹槽底部/深腔
            # 自然变暗，浅痕接近原亮度，立体感和"自然阴影"由此而来。
            depth = np.clip((t * 0.5 - pos[:, 2]) / max(t, 1e-6), 0.0, 1.0)
            shade = (1.0 - 0.45 * depth).astype(np.float32)[:, None]
            base_arr[cut] = base_arr[cut] * shade[cut]
            ent = (pos, nrm, base_arr, 255.0)
            self._voxEntCache = (surf, ent)
            self._voxDbgTick = getattr(self, "_voxDbgTick", 0) + 1
            if self._voxDbgTick % 30 == 1:
                dbg(f"_cncVoxelEnt: verts={pos.shape[0]} "
                    f"grid={vs.nx}x{vs.ny}x{vs.nz} rev={vs.revision}")
            return ent
        except Exception:
            import traceback
            traceback.print_exc()
            dbg("_cncVoxelEnt FAIL\n" + traceback.format_exc())
            return None

    def _renderBrepGLData(self, cam, w: float, h: float,
                          W: int, H: int, painter=None):
        """B-Rep 表面 GPU 单次渲染（Qt 当前 GL 上下文内离屏：颜色+深度）+ 回读。

        调用方在 QPainter 激活期执行原生 GL，必须经 beginNativePainting 括号
        隔离（painter 由调用方传入）——否则 ModernGL 对 GL 状态/FBO 的改动会
        污染 QOpenGLWidget 画笔引擎。渲染发生在进程唯一的 Qt GL 上下文内，
        绝不创建第二个 OpenGL 上下文（独立 standalone 上下文与 Qt 上下文互踩
        current/FBO 是粉色 #FF00FF 花帧的根源）。

        返回 (QImage 设备像素, 视距离图 (H,W) float32, 缺失原因)。
        缺失原因用于调用方分级处理：None=成功；"empty"=场景无 B-Rep/预览
        实体（正常，静默跳过即可，绝不误报为 GPU 故障）；"no_gpu"/"sticky"/
        "error"/"bad_empty"=确有实体但本帧拿不到 GPU 图像，才需走 CPU 兜底。"""
        if self._doc is None:
            return None, None, "empty"
        if self._canvasMode not in ("3d", "cnc"):
            # 浮雕/2D 模式不渲染 3D 实体（设计如此：实体只属于 3D/CNC 空间）。
            # 否则浮雕曲面 ModernGL 管线与本离屏管线同帧混用，是切浮雕花屏的
            # 直接根源；历史文档里残留在 2D 空间的实体也在此被隔离。
            return None, None, "empty"
        solid_sim = self._solidSimActive()
        ents = []
        for obj in self._doc.visibleObjects():
            if not isinstance(obj, BRepObject) or obj.shape() is None:
                continue   # 锁定只挡拾取/编辑，不影响渲染可见性
            if solid_sim and self._isStockObject(obj):
                continue   # 实体模拟：毛坯由体素网格呈现，跳过半透明盒
            mesh = obj.mesh(0.05)
            if mesh is None or len(mesh["tris"]) == 0:
                continue
            pos = mesh["verts"][mesh["tris"]].reshape(-1, 3)
            nrm = mesh["normals"][mesh["tris"]].reshape(-1, 3)
            if self._meshHeightColor and self._isMeshReliefSolid(obj):
                base = self._meshTerrainBase(mesh)
            elif obj.isSelected():
                col = QColor(theme.selectionColorFor(obj))
                base = (col.red(), col.green(), col.blue())
            else:
                col = QColor(*obj.color())
                base = (col.red(), col.green(), col.blue())
            if self._isStockObject(obj):
                # 毛坯半透明：加工参考，不遮挡导入的模型/曲线
                alpha = 70
            elif self._solidPerspective:
                alpha = 140
            else:
                alpha = 255
            ents.append((pos, nrm, base, alpha))
        # 实体模拟：体素坯料表面网格并入渲染（毛坯盒已被跳过）
        if solid_sim:
            vent = self._cncVoxelEnt()
            if vent is not None:
                ents.append(vent)
        # 命令预览实体（挤出/推拉预览等，青色半透明）
        if self._cm is not None and self._cm.hasActive():
            inst = self._cm.activeInstance()
            mk = getattr(inst, "previewSolid", None)
            pobj = mk() if callable(mk) else None
            if isinstance(pobj, BRepObject):
                pm = pobj.mesh(0.05)
                if pm is not None and len(pm["tris"]) > 0:
                    pos = pm["verts"][pm["tris"]].reshape(-1, 3)
                    nrm = pm["normals"][pm["tris"]].reshape(-1, 3)
                    ents.append((pos, nrm, (21, 182, 201), 130))
        if not ents:
            return None, None, "empty"
        mvp = self._buildMVP(cam, w, h, W, H)
        eye, right, up, fwd, _ortho = cam
        # 实体光照：同 _paint3D 直绘路径（见 _solidLight 注释）
        light = self._solidLight(cam)
        if self._gl is None:
            # GPU 被设置关闭 → 整帧软渲染；否则兜底重建对象继续走 GPU
            # （_syncGLContext 正常路径已惰性建好；此处防非 paintGL 入口）。
            if not app_settings.gpu_accelerated:
                return None, None, "no_gpu"
            self._gl = GLSurface()
        if self._brepGpuBad >= 8:
            # GPU 表面已被连续判坏（空帧）：直接改走 CPU z-buffer，不再每帧白试
            if self._brepGpuBad == 8:
                dbg(f"_renderBrepGLData: B-Rep GPU 连续空帧 {self._brepGpuBad} 次"
                    f" -> 粘滞 CPU 光栅化（如需恢复 GPU，可切换显示模式/重启）")
            self._zbFrame = None
            return None, None, "sticky"
        try:
            # QPainter 激活期执行原生 GL：必须 beginNativePainting 隔离，否则
            # 画笔引擎与 ModernGL 互相污染 GL 状态（QOpenGLWidget 混用原生 GL
            # 的硬性要求）。render_brep 在 Qt 上下文内渲染到 GLSurface 自有 FBO
            # 并读回，返回前已恢复默认帧缓冲，QPainter 后续叠加不受影响。
            if painter is not None:
                painter.beginNativePainting()
            try:
                self._gl.ensure()
                rgba, eye_depth = self._gl.render_brep(
                    ents, mvp, eye, light, W, H,
                    (self._bgFloat[0], self._bgFloat[1], self._bgFloat[2]),
                    _ortho, near=NEAR, far=self._dist + 5000.0)
            finally:
                if painter is not None:
                    painter.endNativePainting()
            # render_brep 离场同样把绑定/跟踪留在 FBO 0（ctx.screen.use()）：
            # 对齐回 Qt 内部 FBO，后续 QPainter 叠加层才画在可见目标上
            self._syncQtFboTracking()
        except Exception:
            import traceback
            traceback.print_exc()
            dbg("_renderBrepGLData: B-Rep GPU 渲染异常 -> 本帧无表面\n"
                + traceback.format_exc())
            return None, None, "error"
        arr = np.frombuffer(rgba, np.uint8).reshape(H, W, 4)
        arr = np.flipud(arr)   # GL 底左原点 → Qt 左上原点
        rgba = np.ascontiguousarray(arr)
        eye_depth = np.flipud(eye_depth).copy()
        # 转成 QImage 供视口 QPainter 叠加：背景像素 alpha=0（透明），
        # 保证先画的背景/网格不被覆盖。同时把本帧视距离图缓存为隐藏线
        # 消除深度缓冲。_zbScale = W/w：GPU 深度图按设备像素分辨率存储，
        # 消费方（_drawObjects/_drawBrepEdges）用逻辑像素查询，需对齐。
        qimg = QImage(rgba.data, W, H, W * 4,
                      QImage.Format.Format_RGBA8888).copy()
        self._zbScale = (W / w) if w >= 2 else 1.0
        self._zbFrame = (eye_depth, W, H)
        # 帧健康自检 + 遥测：GPU 帧若"实体明明在视口内却全透明"，判定为
        # 静默空转（个别驱动 / FBO 错位下表现为实体消失但网格正常），
        # 本帧返回 None 由 _paint3D 自动改走 CPU z-buffer；连续发生则粘滞。
        try:
            cov_arr = arr[:, :, 3] > 0
            total = int(cov_arr.sum())
            fd = np.isfinite(eye_depth)
            nf = int(fd.sum())
            self._frameN = getattr(self, "_frameN", 0) + 1
            if self._frameN % 30 == 1:
                rng = ""
                if nf:
                    _d = eye_depth[fd]
                    rng = (f"min={float(_d.min()):.3f} "
                           f"max={float(_d.max()):.3f}")
                dbg(f"_renderBrepGLData: ents={len(ents)} "
                    f"qimg_cover={total}/{H * W} finite_depth={nf} {rng}")
            if total == 0:
                in_view = False
                for pos, _n, _b, _a in ents:
                    c = pos.mean(axis=0)
                    pr = self._project((float(c[0]), float(c[1]),
                                        float(c[2])), cam, w, h)
                    if pr is not None and -0.5 <= pr[0] <= w + 0.5 \
                            and -0.5 <= pr[1] <= h + 0.5:
                        in_view = True
                        break
                if in_view:
                    self._brepGpuBad += 1
                    dbg(f"_renderBrepGLData: GPU 空帧但实体在视口内 "
                        f"-> 坏帧第 {self._brepGpuBad} 次，本帧走 CPU z-buffer")
                    self._zbFrame = None
                    return None, None, "bad_empty"
                self._brepGpuBad = 0
            else:
                self._brepGpuBad = 0
        except Exception:
            pass
        return qimg, eye_depth, None

    # ---------------- B-Rep 表面：软 Z-buffer 光栅化（软件兜底路径） ----------------
    # 画家算法对穿插/掠射三角形必然产生排序错误（杂色楔块）；这里把全部
    # B-Rep 实体（含命令预览）一次性光栅化到带深度缓冲的离屏图像：
    # 逐像素深度测试 + 顶点解析法向插值着色，遮挡关系数学正确。
    _ZB_MAX_DIM = 1600   # 光栅化分辨率上限（性能保护）

    def _angularFor(self, obj, cam, w: float, h: float) -> float:
        """屏幕自适应角步长：按模型投影半径反解，使圆轮廓弦差 < 0.35px
        （数学上与真圆不可分辨）。近看变细、远看变粗，帧耗恒定。"""
        try:
            bb = obj.getBoundingBox()
            if not bb.isValid():
                return 0.05
            cs = [(bb.min.x, bb.min.y, bb.min.z), (bb.max.x, bb.min.y, bb.min.z),
                  (bb.max.x, bb.max.y, bb.min.z), (bb.min.x, bb.max.y, bb.min.z),
                  (bb.min.x, bb.min.y, bb.max.z), (bb.max.x, bb.min.y, bb.max.z),
                  (bb.max.x, bb.max.y, bb.max.z), (bb.min.x, bb.max.y, bb.max.z)]
            pts = [self._project(c, cam, w, h) for c in cs]
            pts = [q for q in pts if q is not None]
            if len(pts) < 2:
                return 0.05
            cx = sum(q[0] for q in pts) / len(pts)
            cy = sum(q[1] for q in pts) / len(pts)
            r_px = max(math.hypot(q[0] - cx, q[1] - cy) for q in pts)
            r_px = max(r_px, 1.0)
            ratio = max(-1.0, min(1.0, 1.0 - 0.35 / r_px))
            return max(0.006, min(0.3, 2.0 * math.acos(ratio)))
        except Exception:
            return 0.05

    def _brepZBufferEntries(self, cam=None, w=0.0, h=0.0, for_depth=False):
        """收集本帧要光栅化的 B-Rep 表面 [(mesh, (r,g,b), alpha)]。

        for_depth=True 时跳过线框模式提前返回（构建深度缓冲用）。
        """
        ents = []
        if self._canvasMode not in ("3d", "cnc") or self._doc is None:
            return ents
        if self._solidDisplayMode == "wireframe" and not for_depth:
            return ents
        for obj in self._doc.visibleObjects():
            if not isinstance(obj, BRepObject) or obj.shape() is None:
                continue   # 锁定只挡拾取/编辑，不影响渲染可见性
            mesh = obj.mesh(self._angularFor(obj, cam, w, h)
                            if cam is not None else 0.05)
            if mesh is None or len(mesh["tris"]) == 0:
                continue
            if self._isStockObject(obj) and self._solidSimActive():
                # 实体模拟：毛坯由体素网格呈现，软渲染路径同样跳过毛坯盒
                continue
            if obj.isSelected():
                col = QColor(theme.selectionColorFor(obj))
            else:
                col = QColor(*obj.color())
            if self._isStockObject(obj):
                # 毛坯半透明：加工参考，不遮挡导入的模型/曲线
                alpha = 70
            else:
                alpha = 255
            # 透视开关只影响背面线可见性，不影响面亮度
            ents.append((mesh, (col.red(), col.green(), col.blue()), alpha))
        # 实体模拟：体素坯料并入软渲染（毛坯盒已被跳过）
        if self._canvasMode == "cnc" and self._solidSimActive():
            vent = self._cncVoxelEnt()
            if vent is not None:
                pos, nrm, base, alpha = vent
                idx = np.arange(len(pos)) // 6
                mesh = {"verts": pos, "normals": nrm, "tris": np.stack(
                    [idx * 6 + 0, idx * 6 + 1, idx * 6 + 2,
                     idx * 6 + 0, idx * 6 + 2, idx * 6 + 3], -1).reshape(-1, 3),
                    "faceRanges": []}
                ents.append((mesh, base, 255))
        # 命令预览实体（挤出预览 / 推拉预览等，BRepObject）
        if self._cm is not None and self._cm.hasActive():
            inst = self._cm.activeInstance()
            mk = getattr(inst, "previewSolid", None)
            pobj = mk() if callable(mk) else None
            if isinstance(pobj, BRepObject):
                mesh = pobj.mesh()
                if mesh is not None and len(mesh["tris"]) > 0:
                    ents.append((mesh, (21, 182, 201), 130))
        # CNC 浮雕模型并入 B-Rep 软渲染（与毛坯同管线）
        if self._canvasMode == "cnc":
            model = self._cncReliefModelForDraw()[0]
            if model is not None:
                m = self._reliefMeshFor(model, max_dim=self._cpuMeshDim())
                if m is not None and m.faceCount > 0:
                    tris, vn = self._cncReliefMeshData(m)
                    md = {"verts": np.ascontiguousarray(m.verts, dtype=np.float32),
                          "tris": np.ascontiguousarray(tris, dtype=np.int64),
                          "normals": np.ascontiguousarray(vn, dtype=np.float32),
                          "faceRanges": [(0, len(tris), False)]}
                    col = QColor(*(tuple(model.color())
                                   if model.color() else (150, 170, 190)))
                    ents.append((md, (col.red(), col.green(), col.blue()), 255))
                    self._reliefDrawn = True
        return ents

    def _zbCacheKeyFor(self, cam, w: float, h: float):
        """表面光栅化帧间缓存键；命令交互/几何变化/视角变化时失效。

        返回 None 表示不可缓存（命令激活期每帧预览不同）。"""
        if self._canvasMode not in ("3d", "cnc") or self._doc is None:
            return None
        if self._cm is not None and self._cm.hasActive():
            return None
        objs = self._doc.visibleObjects()
        oids = tuple(o.id() for o in objs
                     if isinstance(o, BRepObject) and not o.isLocked()
                     and o.shape() is not None)
        sids = tuple(o.id() for o in objs if o.isSelected())
        seq = getattr(self._doc, "_historySeq", 0)
        # 实体模拟：体素随播放每帧变化，版本号并入缓存键强制失效旧帧
        vrev = 0
        try:
            from core.cnc.state import CncState
            vx = CncState.instance().simVoxel
            vrev = int(getattr(vx, "revision", 0))
        except Exception:
            pass
        return (self._mode,
                round(self._pan[0], 2), round(self._pan[1], 2),
                round(self._pitch, 5), round(self._yaw, 5),
                round(self._dist, 2), int(w), int(h),
                self._solidDisplayMode, bool(self._solidPerspective),
                seq, oids, sids, vrev)

    def _paintBrepZBuffer(self, painter, cam, w: float, h: float) -> None:
        """全部 B-Rep 表面 → 深度缓冲光栅化 → 一次性叠加到画布。

        着色=平面着色（Plasticity 风格）：一个拓扑面一个颜色，亮度由
        面质心相对实体中心的近似外法向与灯光夹角决定；无逐像素法向
        插值、无三角片痕迹。遮挡由逐像素深度测试保证（数学正确）。

        性能：顶点投影/几何法向/背面剔除/着色全部 numpy 批量计算；
        静态帧（相机/视口/几何/选中均未变）直接复用上一帧光栅化结果。"""
        import time as _time
        _now = _time.monotonic()
        cache_key = self._zbCacheKeyFor(cam, w, h)
        key_changed = cache_key is not None and cache_key != self._zbCacheKey
        if key_changed:
            # 视角/状态连续变化（两次 key 变化间隔 < 0.15s）视为交互中；
            # 首次渲染（无缓存）也低分辨率起步，避免一次性全分辨率卡顿
            moving = ((_now - self._zbLastKeyT) < 0.15
                      or self._zbCacheKey is None)
            self._zbLastKeyT = _now
        else:
            moving = False
        if (cache_key is not None and cache_key == self._zbCacheKey
                and not (self._zbLowRes and not moving)):
            # 命中缓存帧：交互（低分辨率）停止后强制升级全分辨率重绘
            qimg = self._zbCacheQImg
            if qimg is not None:
                painter.drawImage(0, 0, qimg)
                self._zbFrame = self._zbCacheFrame
                return
        ents = self._brepZBufferEntries(cam, w, h)
        if not ents:
            self._zbFrame = None
            self._zbCacheKey = cache_key
            self._zbCacheQImg = None
            self._zbCacheFrame = None
            self._zbLowRes = False
            return
        # 交互期降采样：视角连续变化时半分辨率光栅化（drawImage 平滑
        # 拉伸到全窗）；停止后自动升级全分辨率。静止帧始终全分辨率
        low = key_changed and moving
        self._zbLowRes = low
        _div = 2 if low else 1
        W = max(2, min(int(w), self._ZB_MAX_DIM) // _div)
        H = max(2, min(int(h), self._ZB_MAX_DIM) // _div)
        img = np.zeros((H, W, 4), dtype=np.uint8)
        zbuf = np.full((H, W), np.inf, dtype=np.float64)
        eye = cam[0]
        ex, ey, ez = float(eye[0]), float(eye[1]), float(eye[2])
        right, up, fwd = cam[1], cam[2], cam[3]
        ortho = cam[4]
        fx0, fy0, fz0 = float(fwd[0]), float(fwd[1]), float(fwd[2])
        rx0, ry0, rz0 = float(right[0]), float(right[1]), float(right[2])
        ux0, uy0, uz0 = float(up[0]), float(up[1]), float(up[2])
        # 灯光永远固定在屏幕后上方（相机 up/fwd 决定，不随模型/视角水平
        # 旋转改变阴影位置）：主光 = normalize(up - fwd)，恒有屏幕上方与
        # 后方分量；补光 = 屏幕前上方的弱 fill（只防死黑，Y 分量恒正，
        # 绝不自下而上照——避免把阴影区照亮）。
        _lx, _ly, _lz = (ux0 - fx0, uy0 - fy0, uz0 - fz0)
        _llen = math.sqrt(_lx * _lx + _ly * _ly + _lz * _lz) or 1e-12
        lx, ly, lz = _lx / _llen, _ly / _llen, _lz / _llen
        f2x, f2y, f2z = (ux0 + fx0 * 0.5, uy0 + fy0 * 0.5, uz0 + fz0 * 0.5)
        _l2 = math.sqrt(f2x * f2x + f2y * f2y + f2z * f2z) or 1e-12
        f2x, f2y, f2z = f2x / _l2, f2y / _l2, f2z / _l2
        for mesh, base_rgb, alpha in ents:
            verts = np.asarray(mesh["verts"], dtype=np.float64)
            nor = np.asarray(mesh["normals"], dtype=np.float64)
            tris = np.asarray(mesh["tris"], dtype=np.int64)
            ranges = mesh["faceRanges"]
            n = verts.shape[0]
            # 批量顶点投影（与 _project 语义一致：透视 NEAR 剔除 + 坐标钳位）
            vx = verts[:, 0] - ex
            vy = verts[:, 1] - ey
            vz = verts[:, 2] - ez
            depth = vx * fx0 + vy * fy0 + vz * fz0
            xx = vx * rx0 + vy * ry0 + vz * rz0
            yy = vx * ux0 + vy * uy0 + vz * uz0
            cxx, cyy = W / 2.0, H / 2.0
            if ortho:
                sc = self._orthoScale(W, H)
                sx = np.clip(cxx + xx * sc, -_COORD_CLAMP, _COORD_CLAMP)
                sy = np.clip(cyy - yy * sc, -_COORD_CLAMP, _COORD_CLAMP)
                ok = np.ones(n, dtype=bool)
            else:
                focal = self._focal(H)
                ok = depth >= NEAR
                sx = np.zeros(n)
                sy = np.zeros(n)
                dd = depth[ok]
                if dd.size:
                    sx[ok] = np.clip(cxx + xx[ok] / dd * focal,
                                     -_COORD_CLAMP, _COORD_CLAMP)
                    sy[ok] = np.clip(cyy - yy[ok] / dd * focal,
                                     -_COORD_CLAMP, _COORD_CLAMP)
            # 三角形批量：几何法向 / REVERSED 反转 / 背面剔除 / 归一化
            ia = tris[:, 0]
            ib = tris[:, 1]
            ic = tris[:, 2]
            av = verts[ia]
            bv = verts[ib]
            cv = verts[ic]
            gx = ((bv[:, 1] - av[:, 1]) * (cv[:, 2] - av[:, 2])
                  - (bv[:, 2] - av[:, 2]) * (cv[:, 1] - av[:, 1]))
            gy = ((bv[:, 2] - av[:, 2]) * (cv[:, 0] - av[:, 0])
                  - (bv[:, 0] - av[:, 0]) * (cv[:, 2] - av[:, 2]))
            gz = ((bv[:, 0] - av[:, 0]) * (cv[:, 1] - av[:, 1])
                  - (bv[:, 1] - av[:, 1]) * (cv[:, 0] - av[:, 0]))
            rev = np.zeros(len(tris), dtype=bool)
            for frng in ranges:
                fs, fc, fv = frng[0], frng[1], frng[2]
                if fc > 0 and fv:
                    rev[fs:fs + fc] = True
            if rev.any():
                gx[rev] = -gx[rev]
                gy[rev] = -gy[rev]
                gz[rev] = -gz[rev]
            fcx = (av[:, 0] + bv[:, 0] + cv[:, 0]) / 3.0
            fcy = (av[:, 1] + bv[:, 1] + cv[:, 1]) / 3.0
            fcz = (av[:, 2] + bv[:, 2] + cv[:, 2]) / 3.0
            # 外向法向修正：几何法向若指向实体内部则翻转（如底面绕序朝内，
            # 从底部视角看会被背面剔除成镂空；以"面心-质心"为外侧参考）。
            # 仅对"法向以 Z 为主"的面（底/顶面）生效：侧壁（外壁/孔壁）
            # 法向水平，内外方向由几何生成时已保证；质心判据对带孔实体
            # 会把孔壁误翻成朝实体内部，导致孔壁背面剔除成"镂空"。
            centx = verts[:, 0].mean()
            centy = verts[:, 1].mean()
            centz = verts[:, 2].mean()
            zdom = np.abs(gz) > (np.abs(gx) + np.abs(gy))
            flip = (gx * (fcx - centx) + gy * (fcy - centy)
                    + gz * (fcz - centz)) < 0.0
            flip &= zdom
            if flip.any():
                gx[flip] = -gx[flip]
                gy[flip] = -gy[flip]
                gz[flip] = -gz[flip]
            back = (gx * (ex - fcx) + gy * (ey - fcy)
                    + gz * (ez - fcz)) <= 0
            glen = np.sqrt(gx * gx + gy * gy + gz * gz)
            gsafe = glen.copy()
            gsafe[gsafe < 1e-12] = 1.0
            gx = gx / gsafe
            gy = gy / gsafe
            gz = gz / gsafe
            vis_tri = ok[ia] & ok[ib] & ok[ic] & ~back & (glen >= 1e-12)
            # 平面三角形（三顶点解析法向一致）→ 恒定色快速路径
            q0 = nor[ia]
            q1 = nor[ib]
            q2 = nor[ic]
            flat = (((q0[:, 0] * q1[:, 0] + q0[:, 1] * q1[:, 1]
                      + q0[:, 2] * q1[:, 2]) > 0.9999)
                    & ((q0[:, 0] * q2[:, 0] + q0[:, 1] * q2[:, 1]
                        + q0[:, 2] * q2[:, 2]) > 0.9999))
            b1 = gx * lx + gy * ly + gz * lz
            b2 = gx * f2x + gy * f2y + gz * f2z
            b1 = np.where(b1 < 0.0, 0.0, b1)
            b2 = np.where(b2 < 0.0, 0.0, b2)
            # 实体着色（明亮清晰）：环境下限抬高 + 主光 + 填充光——
            # 与 GPU 路径（gl_surface u_mode=7 分支）公式一致。
            bright = np.minimum(1.0, 0.36 + 0.68 * b1 + 0.22 * b2)
            # 光栅化前预取每三角形屏幕坐标/视距（循环内避免反复 numpy 标量索引）
            sx3 = sx[tris]
            sy3 = sy[tris]
            d3 = depth[tris]
            # 屏幕包围盒：三角形按最大边分桶批量 numpy 光栅化——
            # 固定 P 网格必须 ≥ 三角形包围盒任一边（按 max(bw,bh)
            # 分桶，P 取 8/21/48 三档全覆盖），否则窄长条三角形
            # （如 8×41，面积虽小）会漏画形成破洞；超大才逐个
            bw = (np.floor(np.max(sx3, axis=1))
                  - np.ceil(np.min(sx3, axis=1)) + 1).astype(np.int64)
            bh = (np.floor(np.max(sy3, axis=1))
                  - np.ceil(np.min(sy3, axis=1)) + 1).astype(np.int64)
            mbs = np.maximum(bw, bh)
            small = vis_tri & flat & (mbs <= 8)
            mid = vis_tri & flat & (mbs > 8) & (mbs <= 21)
            # mbs>21（如 8×41 窄长条）逐个光栅化：批量 P=48 网格
            # 每三角形 2304 像素行太重（帧耗翻倍）；逐个仅 ~330 像素
            big = vis_tri & ~(small | mid)
            for t_i in np.nonzero(big)[0]:
                if flat[t_i]:
                    rgb = (int(base_rgb[0] * bright[t_i]),
                           int(base_rgb[1] * bright[t_i]),
                           int(base_rgb[2] * bright[t_i]))
                    self._rasterTriZB(img, zbuf, W, H,
                                      sx3[t_i, 0], sy3[t_i, 0],
                                      sx3[t_i, 1], sy3[t_i, 1],
                                      sx3[t_i, 2], sy3[t_i, 2],
                                      d3[t_i, 0], d3[t_i, 1],
                                      d3[t_i, 2], rgb, alpha)
                else:
                    self._rasterTriZB(img, zbuf, W, H,
                                      sx3[t_i, 0], sy3[t_i, 0],
                                      sx3[t_i, 1], sy3[t_i, 1],
                                      sx3[t_i, 2], sy3[t_i, 2],
                                      d3[t_i, 0], d3[t_i, 1],
                                      d3[t_i, 2], base_rgb, alpha,
                                      nrm=(q0[t_i], q1[t_i], q2[t_i]),
                                      light=(lx, ly, lz, f2x, f2y, f2z))
            if small.any():
                self._rasterBatchZB(img, zbuf, W, H,
                                    sx3[small], sy3[small], d3[small],
                                    bright[small], base_rgb, alpha, 8)
            if mid.any():
                self._rasterBatchZB(img, zbuf, W, H,
                                    sx3[mid], sy3[mid], d3[mid],
                                    bright[mid], base_rgb, alpha, 21)
        if not img[:, :, 3].any():
            self._zbFrame = None
            self._zbCacheKey = cache_key
            self._zbCacheQImg = None
            self._zbCacheFrame = None
            return
        from PySide6.QtGui import QImage
        qimg = QImage(img.data, W, H, W * 4, QImage.Format.Format_RGBA8888)
        qimg = qimg.copy()   # 深拷贝：缓存需跨帧持有像素
        painter.drawImage(0, 0, qimg)
        self._zbFrame = (zbuf, W, H)
        self._zbCacheKey = cache_key
        self._zbCacheQImg = qimg
        self._zbCacheFrame = self._zbFrame

    def _rasterBatchZB(self, img, zbuf, W, H,
                        sx3, sy3, d3, bright,
                        base_rgb, alpha, P: int) -> None:
        """小/中三角形批量光栅化（平面恒定色快速路径）。

        一次 numpy 展开全部像素候选（每三角形 P×P 网格），重心坐标
        + 深度测试向量化；zbuf/img 更新用最小 Python 循环。仅支持
        不透明恒定色（alpha==255）；半透明走原逐三角形路径。
        """
        N = len(sx3)
        if N == 0 or alpha < 255:
            return
        x0 = np.clip(np.ceil(np.min(sx3, axis=1)).astype(np.int64), 0, W - 1)
        x1 = np.clip(np.floor(np.max(sx3, axis=1)).astype(np.int64), 0, W - 1)
        y0 = np.clip(np.ceil(np.min(sy3, axis=1)).astype(np.int64), 0, H - 1)
        y1 = np.clip(np.floor(np.max(sy3, axis=1)).astype(np.int64), 0, H - 1)
        gx, gy = np.meshgrid(np.arange(P, dtype=np.int64),
                             np.arange(P, dtype=np.int64))
        gx = gx.ravel()
        gy = gy.ravel()
        px = x0[:, None] + gx[None, :]                 # (N, 64)
        py = y0[:, None] + gy[None, :]
        valid = (px <= x1[:, None]) & (py <= y1[:, None])
        pxv = px.astype(np.float64)
        pyv = py.astype(np.float64)
        x0v = sx3[:, 0, None]
        y0v = sy3[:, 0, None]
        x1v = sx3[:, 1, None]
        y1v = sy3[:, 1, None]
        x2v = sx3[:, 2, None]
        y2v = sy3[:, 2, None]
        d0 = d3[:, 0, None]
        d1 = d3[:, 1, None]
        d2 = d3[:, 2, None]
        den = (y1v - y2v) * (x0v - x2v) + (x2v - x1v) * (y0v - y2v)
        ok_den = np.abs(den) > 1e-12
        with np.errstate(divide="ignore", invalid="ignore"):
            w0 = ((y1v - y2v) * (pxv - x2v)
                  + (x2v - x1v) * (pyv - y2v)) / den
            w1 = ((y2v - y0v) * (pxv - x2v)
                  + (x0v - x2v) * (pyv - y2v)) / den
            w2 = 1.0 - w0 - w1
            d = w0 * d0 + w1 * d1 + w2 * d2
        inside = valid & ok_den & (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
        idx = (py * W + px)
        zf = zbuf.ravel()
        imf = img.reshape(-1, 4)
        for i in range(N):
            sel = np.nonzero(inside[i])[0]
            if sel.size == 0:
                continue
            iidx = idx[i, sel]
            dd = d[i, sel]
            br = float(bright[i])
            cr = int(base_rgb[0] * br)
            cg = int(base_rgb[1] * br)
            cb = int(base_rgb[2] * br)
            better = dd < zf[iidx]
            if better.any():
                wsel = iidx[better]
                zf[wsel] = dd[better]
                imf[wsel, 0] = cr
                imf[wsel, 1] = cg
                imf[wsel, 2] = cb
                imf[wsel, 3] = 255

    def _rasterTriZB(self, img, zbuf, W, H,
                     x0, y0, x1, y1, x2, y2,
                     d0, d1, d2, rgb, alpha,
                     nrm=None, light=None) -> None:
        """单三角形光栅化：包围盒 + 重心坐标 + 深度测试。

        nrm=None：恒定色（平面三角形快速路径）；
        nrm=(n0,n1,n2)+light：逐像素法向插值 + 双灯光照（曲面平滑着色）。"""
        minX = max(0, int(min(x0, x1, x2)))
        maxX = min(W - 1, int(max(x0, x1, x2)) + 1)
        minY = max(0, int(min(y0, y1, y2)))
        maxY = min(H - 1, int(max(y0, y1, y2)) + 1)
        if minX > maxX or minY > maxY:
            return
        den = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(den) < 1e-12:
            return
        # 小包围盒走纯 Python 快速路径（细网格三角形平均只盖几个像素，
        # numpy 调用开销反而占大头）
        if (maxX - minX + 1) * (maxY - minY + 1) <= 64:
            if alpha >= 255:
                fr, fg, fb = rgb
                for py in range(minY, maxY + 1):
                    row_z = zbuf[py]
                    row_i = img[py]
                    for px in range(minX, maxX + 1):
                        w0 = ((y1 - y2) * (px - x2) + (x2 - x1) * (py - y2)) / den
                        if w0 < 0:
                            continue
                        w1 = ((y2 - y0) * (px - x2) + (x0 - x2) * (py - y2)) / den
                        if w1 < 0:
                            continue
                        w2 = 1.0 - w0 - w1
                        if w2 < 0:
                            continue
                        d = w0 * d0 + w1 * d1 + w2 * d2
                        if d < row_z[px]:
                            row_z[px] = d
                            row_i[px, 0] = fr
                            row_i[px, 1] = fg
                            row_i[px, 2] = fb
                            row_i[px, 3] = 255
            else:
                a = alpha / 255.0
                ia = 1.0 - a
                for py in range(minY, maxY + 1):
                    row_z = zbuf[py]
                    row_i = img[py]
                    for px in range(minX, maxX + 1):
                        w0 = ((y1 - y2) * (px - x2) + (x2 - x1) * (py - y2)) / den
                        if w0 < 0:
                            continue
                        w1 = ((y2 - y0) * (px - x2) + (x0 - x2) * (py - y2)) / den
                        if w1 < 0:
                            continue
                        w2 = 1.0 - w0 - w1
                        if w2 < 0:
                            continue
                        d = w0 * d0 + w1 * d1 + w2 * d2
                        if d < row_z[px]:
                            row_z[px] = d
                            o = row_i[px]
                            o[0] = int(rgb[0] * a + o[0] * ia)
                            o[1] = int(rgb[1] * a + o[1] * ia)
                            o[2] = int(rgb[2] * a + o[2] * ia)
                            o[3] = 255
            return
        X, Y = np.meshgrid(np.arange(minX, maxX + 1, dtype=np.float64),
                           np.arange(minY, maxY + 1, dtype=np.float64))
        w0 = ((y1 - y2) * (X - x2) + (x2 - x1) * (Y - y2)) / den
        w1 = ((y2 - y0) * (X - x2) + (x0 - x2) * (Y - y2)) / den
        w2 = 1.0 - w0 - w1
        mask = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
        if not mask.any():
            return
        depth = w0 * d0 + w1 * d1 + w2 * d2
        zr = zbuf[minY:maxY + 1, minX:maxX + 1]
        upd = mask & (depth < zr)
        if not upd.any():
            return
        region = img[minY:maxY + 1, minX:maxX + 1]
        if nrm is not None and light is not None:
            # 逐像素法向插值 + 双灯光照（曲面平滑着色）
            q0, q1, q2 = nrm
            nx = w0 * q0[0] + w1 * q1[0] + w2 * q2[0]
            ny = w0 * q0[1] + w1 * q1[1] + w2 * q2[1]
            nz = w0 * q0[2] + w1 * q1[2] + w2 * q2[2]
            nl = np.sqrt(nx * nx + ny * ny + nz * nz)
            nl[nl < 1e-9] = 1.0
            nx, ny, nz = nx / nl, ny / nl, nz / nl
            lx, ly, lz, f2x, f2y, f2z = light
            b1 = np.maximum(0.0, nx * lx + ny * ly + nz * lz)
            b2 = np.maximum(0.0, nx * f2x + ny * f2y + nz * f2z)
            # 实体着色（明亮清晰）：环境下限抬高 + 主光 + 填充光——
            # 与 GPU 路径（gl_surface u_mode=7 分支）公式一致。
            bright = np.minimum(1.0, 0.36 + 0.68 * b1 + 0.22 * b2)
            rr = np.minimum(255.0, rgb[0] * bright)
            gg = np.minimum(255.0, rgb[1] * bright)
            bb2 = np.minimum(255.0, rgb[2] * bright)
            if alpha >= 255:
                region[..., 0][upd] = rr[upd]
                region[..., 1][upd] = gg[upd]
                region[..., 2][upd] = bb2[upd]
                region[..., 3][upd] = 255
            else:
                a = alpha / 255.0
                dst = region[..., :3][upd].astype(np.float64)
                src = np.stack([rr[upd], gg[upd], bb2[upd]], axis=-1)
                region[..., :3][upd] = (
                    src * a + dst * (1.0 - a)).astype(np.uint8)
                region[..., 3][upd] = 255
            zr[upd] = depth[upd]
            return
        if alpha >= 255:
            region[..., 0][upd] = rgb[0]
            region[..., 1][upd] = rgb[1]
            region[..., 2][upd] = rgb[2]
            region[..., 3][upd] = 255
        else:
            a = alpha / 255.0
            dst = region[..., :3][upd].astype(np.float64)
            region[..., :3][upd] = (
                dst * (1.0 - a) + np.array(rgb, dtype=np.float64) * a
            ).astype(np.uint8)
            region[..., 3][upd] = 255
        zr[upd] = depth[upd]

    def _rasterTriDepth(self, zbuf, W, H,
                        x0, y0, x1, y1, x2, y2,
                        d0, d1, d2) -> None:
        """单三角形光栅化只更新深度缓冲（隐藏线消除用，不写颜色）。"""
        minX = max(0, int(min(x0, x1, x2)))
        maxX = min(W - 1, int(max(x0, x1, x2)) + 1)
        minY = max(0, int(min(y0, y1, y2)))
        maxY = min(H - 1, int(max(y0, y1, y2)) + 1)
        if minX > maxX or minY > maxY:
            return
        den = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(den) < 1e-12:
            return
        if (maxX - minX + 1) * (maxY - minY + 1) <= 64:
            for py in range(minY, maxY + 1):
                row_z = zbuf[py]
                for px in range(minX, maxX + 1):
                    w0 = ((y1 - y2) * (px - x2) + (x2 - x1) * (py - y2)) / den
                    if w0 < 0:
                        continue
                    w1 = ((y2 - y0) * (px - x2) + (x0 - x2) * (py - y2)) / den
                    if w1 < 0:
                        continue
                    w2 = 1.0 - w0 - w1
                    if w2 < 0:
                        continue
                    d = w0 * d0 + w1 * d1 + w2 * d2
                    if d < row_z[px]:
                        row_z[px] = d
            return
        X, Y = np.meshgrid(np.arange(minX, maxX + 1, dtype=np.float64),
                           np.arange(minY, maxY + 1, dtype=np.float64))
        w0 = ((y1 - y2) * (X - x2) + (x2 - x1) * (Y - y2)) / den
        w1 = ((y2 - y0) * (X - x2) + (x0 - x2) * (Y - y2)) / den
        w2 = 1.0 - w0 - w1
        mask = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
        if not mask.any():
            return
        depth = w0 * d0 + w1 * d1 + w2 * d2
        zr = zbuf[minY:maxY + 1, minX:maxX + 1]
        upd = mask & (depth < zr)
        if upd.any():
            zr[upd] = depth[upd]

    def _buildDepthBuffer(self, cam, w: float, h: float):
        """构建深度缓冲 (zbuf, W, H) 用于线框模式隐藏线消除。

        收集所有 B-Rep 实体表面光栅化到 zbuf（只填深度不着色）。
        无实体或无需消除时返回 None。
        """
        if self._canvasMode not in ("3d", "cnc") or self._doc is None:
            return None
        ents = self._brepZBufferEntries(cam, w, h, for_depth=True)
        if not ents:
            return None
        SS = 1   # 深度缓冲无需超采样（隐藏线判定用保守 1x 精度即可）
        W = max(2, min(int(w), self._ZB_MAX_DIM)) * SS
        H = max(2, min(int(h), self._ZB_MAX_DIM)) * SS
        zbuf = np.full((H, W), np.inf, dtype=np.float64)
        for mesh, _rgb, _alpha in ents:
            verts = mesh["verts"]
            tris = mesh["tris"]
            ranges = mesh["faceRanges"]
            n = len(verts)
            if n == 0:
                continue
            # 向量化投影（逐点 _project 是全场景软件路径的卡顿源）；
            # 无效点（近平面后）坐标/深度置 0，消费者均先查 vis 再取值
            sx, sy, depv, vis = self._projectVerts(
                np.asarray(verts, dtype=np.float64), cam, w * SS, h * SS)
            dep = np.where(vis, depv, 0.0)
            sx = np.where(vis, sx, 0.0)
            sy = np.where(vis, sy, 0.0)
            ctr = verts.mean(axis=0)
            for frng in ranges:
                fstart, fcnt, frev = frng[0], frng[1], frng[2]
                if fcnt <= 0:
                    continue
                fv = verts[tris[fstart:fstart + fcnt].reshape(-1)]
                fcx = float(fv[:, 0].mean())
                fcy = float(fv[:, 1].mean())
                fcz = float(fv[:, 2].mean())
                for t_i in range(fstart, fstart + fcnt):
                    ia, ib, ic = tris[t_i]
                    if not (vis[ia] and vis[ib] and vis[ic]):
                        continue
                    a, b, c = verts[ia], verts[ib], verts[ic]
                    gx = (b[1]-a[1])*(c[2]-a[2]) - (b[2]-a[2])*(c[1]-a[1])
                    gy = (b[2]-a[2])*(c[0]-a[0]) - (b[0]-a[0])*(c[2]-a[2])
                    gz = (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])
                    if frev:
                        gx, gy, gz = -gx, -gy, -gz
                    # 不做背面剔除：所有面（前+背）都光栅化到 zbuf，
                    # 这样从任何视角看，被遮挡的边线都能正确剔除
                    self._rasterTriDepth(zbuf, W, H,
                                         sx[ia], sy[ia], sx[ib], sy[ib],
                                         sx[ic], sy[ic],
                                         dep[ia], dep[ib], dep[ic])
        return (zbuf, W, H)

    def _filterHiddenLines(self, lines, depthBuf):
        """批量隐藏线消除（向量化）：lines 为 (x0,y0,x1,y1,d0,d1) 数组/列表，
        返回可见线段列表 [(x0,y0,x1,y1), ...]。

        每段采样 3 点（t=0.25/0.5/0.75）查深度图，全部可见才保留——
        一次 numpy fancy indexing 完成全部查询，替代逐像素 Python 循环。"""
        zbuf, zW, zH = depthBuf
        sc = getattr(self, "_zbScale", 1.0)
        L = np.asarray(lines, dtype=np.float64)
        x0, y0, x1, y1 = L[:, 0], L[:, 1], L[:, 2], L[:, 3]
        d0, d1 = L[:, 4], L[:, 5]
        vis = np.ones(len(L), dtype=bool)
        for t in (0.25, 0.5, 0.75):
            ix = np.int64((x0 + (x1 - x0) * t) * sc)
            iy = np.int64((y0 + (y1 - y0) * t) * sc)
            d = d0 + (d1 - d0) * t
            inb = (ix >= 0) & (ix < zW) & (iy >= 0) & (iy < zH)
            ok = np.ones(len(L), dtype=bool)
            ok[inb] = d[inb] <= zbuf[iy[inb], ix[inb]] + 1.0
            vis &= ok
        out = L[vis]
        hid = L[~vis]
        return ([(r[0], r[1], r[2], r[3]) for r in out],
                [(r[0], r[1], r[2], r[3]) for r in hid])

    def _drawLineTested(self, painter, zbuf, W, H,
                        x0, y0, d0, x1, y1, d1, eps=1.0) -> None:
        """画线段，被实体遮挡的部分不画（隐藏线消除）。

        对线段按屏幕像素步长采样，逐点查 zbuf 深度，
        只画深度 <= zbuf + eps 的可见段。
        """
        dx = x1 - x0
        dy = y1 - y0
        seg_len = math.sqrt(dx * dx + dy * dy)
        if seg_len < 1.0:
            sc = getattr(self, "_zbScale", 1.0)
            mx, my = int((x0 + x1) * 0.5 * sc), int((y0 + y1) * 0.5 * sc)
            if 0 <= my < H and 0 <= mx < W:
                if (d0 + d1) * 0.5 <= zbuf[my][mx] + eps:
                    painter.drawLine(QPointF(x0, y0), QPointF(x1, y1))
            else:
                painter.drawLine(QPointF(x0, y0), QPointF(x1, y1))
            return
        n = max(2, int(seg_len))
        sc = getattr(self, "_zbScale", 1.0)   # GPU 深度图为设备像素分辨率
        vis = []
        for i in range(n + 1):
            t = i / n
            x = x0 + dx * t
            y = y0 + dy * t
            d = d0 + (d1 - d0) * t
            ix, iy = int(x * sc), int(y * sc)
            if 0 <= iy < H and 0 <= ix < W:
                vis.append(d <= zbuf[iy][ix] + eps)
            else:
                vis.append(True)
        i = 0
        while i < n:
            if not vis[i]:
                i += 1
                continue
            j = i
            while j < n and vis[j + 1]:
                j += 1
            t0 = i / n
            t1 = j / n
            painter.drawLine(QPointF(x0 + dx * t0, y0 + dy * t0),
                             QPointF(x0 + dx * t1, y0 + dy * t1))
            i = j + 1

    def _drawBrepEdges(self, painter, obj, cam, w: float, h: float,
                       color: QColor | None = None,
                       depthBuf=None) -> None:
        """B-Rep 实体特征边线（表面填充由 z-buffer 通道负责）。

        depthBuf=(zbuf, W, H) 时做隐藏线消除：被实体遮挡的线段不画。

        性能：全部折线点一次性 numpy 投影；无隐藏线需求时合并成
        QPainterPath 一次 drawPath（万级线段单次绘制），避免逐段
        drawLine 的 Python/Qt 调用开销。
        """
        if obj.shape() is None:
            return
        mesh = obj.mesh()
        if mesh is None or len(mesh["tris"]) == 0:
            return
        selected = obj.isSelected()
        if color is None:
            hl = theme.selectionColorFor(obj) if selected else None
            col = QColor(hl) if hl else QColor(*obj.color())
        else:
            col = QColor(color)
        pen = QPen(col, max(obj.lineWidth() + (1.5 if selected else 0), 1.0))
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        top = obj.topology(self._angularFor(obj, cam, w, h),
                           edges_only=True)
        polys = top["edgePolylines"]
        if not polys:
            return
        zbuf = depthBuf[0] if depthBuf is not None else None
        zW = depthBuf[1] if depthBuf is not None else 0
        zH = depthBuf[2] if depthBuf is not None else 0
        # ---- 批量投影 ----
        pts = [p for poly in polys for p in poly]
        P = np.asarray(pts, dtype=np.float64)
        eye, right, up, fwd, ortho = cam
        v = P - np.asarray(eye, dtype=np.float64)
        depth = v[:, 0] * fwd[0] + v[:, 1] * fwd[1] + v[:, 2] * fwd[2]
        xx = v[:, 0] * right[0] + v[:, 1] * right[1] + v[:, 2] * right[2]
        yy = v[:, 0] * up[0] + v[:, 1] * up[1] + v[:, 2] * up[2]
        cx, cy = w / 2.0, h / 2.0
        if ortho:
            sc = self._orthoScale(w, h)
            sx = np.clip(cx + xx * sc, -_COORD_CLAMP, _COORD_CLAMP)
            sy = np.clip(cy - yy * sc, -_COORD_CLAMP, _COORD_CLAMP)
            ok = np.ones(len(P), dtype=bool)
        else:
            focal = self._focal(h)
            ok = depth >= NEAR
            sx = np.full(len(P), 0.0)
            sy = np.full(len(P), 0.0)
            dd = depth[ok]
            if dd.size:
                sx[ok] = np.clip(cx + xx[ok] / dd * focal,
                                 -_COORD_CLAMP, _COORD_CLAMP)
                sy[ok] = np.clip(cy - yy[ok] / dd * focal,
                                 -_COORD_CLAMP, _COORD_CLAMP)
        if zbuf is None:
            path = QPainterPath()
            idx = 0
            for poly in polys:
                n = len(poly)
                prev = False
                for k in range(n):
                    if ok[idx + k]:
                        if not prev:
                            path.moveTo(sx[idx + k], sy[idx + k])
                            prev = True
                        else:
                            path.lineTo(sx[idx + k], sy[idx + k])
                    else:
                        prev = False
                idx += n
            painter.drawPath(path)
            return
        # 隐藏线消除：逐段深度测试。交互期 zbuf 是半分辨率（W×H），
        # 边线投影坐标须缩放到 zbuf 坐标系再查深度，否则索引越界/错位
        if zW != w or zH != h:
            _kx = zW / w
            _ky = zH / h
            sxz = sx * _kx
            syz = sy * _ky
        else:
            sxz = sx
            syz = sy
        idx = 0
        for poly in polys:
            n = len(poly)
            for k in range(n - 1):
                if ok[idx + k] and ok[idx + k + 1]:
                    self._drawLineTested(painter, zbuf, zW, zH,
                                         sxz[idx + k], syz[idx + k],
                                         depth[idx + k],
                                         sxz[idx + k + 1], syz[idx + k + 1],
                                         depth[idx + k + 1])
            idx += n
    def _drawSolid(self, painter, obj, cam, w: float, h: float,
                   color: QColor | None = None,
                   depthBuf=None) -> None:
        """挤出实体（SOLID）叠加绘制。

        显示模式（self._solidDisplayMode）：
        - "wireframe"：仅基本结构线（底/顶轮廓 + 竖棱），不画面填充
        - "rendered"：不透明实体面（背面剔除 + 简单着色 + 画家算法排序）
          + 可见面边线

        color 传 QColor 时按预览风格绘制（命令预览：青色半透明）；
        缺省按文档对象规则着色（选中高亮同曲线：selectionColorFor + 加粗）。
        """
        faces = obj.faces()
        if not faces:
            return
        selected = obj.isSelected()
        if color is None:
            hl = theme.selectionColorFor(obj) if selected else None
            col = QColor(hl) if hl else QColor(*obj.color())
            width = max(obj.lineWidth() + 1.5, 3.0) if selected else obj.lineWidth()
        else:
            col = QColor(color)
            width = 1.5
        wireframeOnly = (self._solidDisplayMode == "wireframe" and color is None)
        opaqueRender = (self._solidDisplayMode == "rendered" and color is None)
        # ---- 面填充 ----
        if not wireframeOnly:
            eye = cam[0]
            # 灯光方向：始终在屏幕后上方（跟随相机，不随视图旋转变化）
            # L = normalize(up - fwd)：从表面指向光源（屏幕上方且后方）
            _up, _fwd = cam[2], cam[3]
            lx, ly, lz = _up[0] - _fwd[0], _up[1] - _fwd[1], _up[2] - _fwd[2]
            _llen = math.sqrt(lx * lx + ly * ly + lz * lz) or 1e-12
            lx, ly, lz = lx / _llen, ly / _llen, lz / _llen
            # 实体质心（用于判断面法向外侧）
            base, top = obj.basePoints(), obj.topPolyline()
            allp = base + top
            scx = sum(p.x for p in allp) / len(allp)
            scy = sum(p.y for p in allp) / len(allp)
            scz = sum(p.z for p in allp) / len(allp)
            # 预计算侧壁平滑法向：用底面边中点径向方向代替面片法向，
            # 使圆柱/曲面侧壁相邻面片法向连续，消除 flat shading 棱角。
            # faces 结构：[0]=底面, [1]=顶面, [2:]=侧壁（每段对应一条底边）
            smoothN = {}   # face index → (nx, ny, nz)
            nBase = len(base)
            if nBase >= 3 and len(faces) >= 2 + nBase:
                for i in range(nBase):
                    j = (i + 1) % nBase
                    # 底边方向
                    dx, dy = base[j].x - base[i].x, base[j].y - base[i].y
                    # 边法向（XY 平面内垂直于边）
                    nx, ny = dy, -dx
                    nlen2 = math.sqrt(nx * nx + ny * ny)
                    if nlen2 < 1e-12:
                        continue
                    nx, ny = nx / nlen2, ny / nlen2
                    # 确保朝外（边中点远离质心方向）
                    mx = (base[i].x + base[j].x) * 0.5 - scx
                    my = (base[i].y + base[j].y) * 0.5 - scy
                    if nx * mx + ny * my < 0:
                        nx, ny = -nx, -ny
                    smoothN[2 + i] = (nx, ny, 0.0)
            items = []
            for fi, poly in enumerate(faces):
                sp = [self._project((p.x, p.y, p.z), cam, w, h) for p in poly]
                if not sp or any(s is None for s in sp):
                    continue
                # 面法向：侧壁用平滑法向，底/顶用面片法向
                if fi in smoothN:
                    nx, ny, nz = smoothN[fi]
                    nlen = 1.0
                else:
                    v0, v1, v2 = poly[0], poly[1], poly[2]
                    ax, ay, az = v1.x - v0.x, v1.y - v0.y, v1.z - v0.z
                    bx, by, bz = v2.x - v0.x, v2.y - v0.y, v2.z - v0.z
                    nx = ay * bz - az * by
                    ny = az * bx - ax * bz
                    nz = ax * by - ay * bx
                    nlen = math.sqrt(nx * nx + ny * ny + nz * nz)
                    if nlen < 1e-12:
                        continue
                    # 确保法向朝外
                    fcx = sum(p.x for p in poly) / len(poly)
                    fcy = sum(p.y for p in poly) / len(poly)
                    fcz = sum(p.z for p in poly) / len(poly)
                    if nx * (fcx - scx) + ny * (fcy - scy) + nz * (fcz - scz) < 0:
                        nx, ny, nz = -nx, -ny, -nz
                # 面质心
                fcx = sum(p.x for p in poly) / len(poly)
                fcy = sum(p.y for p in poly) / len(poly)
                fcz = sum(p.z for p in poly) / len(poly)
                # 视线方向（面→相机）
                vx, vy, vz = eye[0] - fcx, eye[1] - fcy, eye[2] - fcz
                dot = nx * vx + ny * vy + nz * vz
                if opaqueRender and dot <= 0:
                    continue        # 背面剔除
                pts = [QPointF(s[0], s[1]) for s in sp]
                depth = sum(s[2] for s in sp) / len(sp)
                if opaqueRender:
                    # 灯光跟随相机屏幕后上方：bright = max(ambient, dot(N, L))
                    bright = max(0.30, (nx * lx + ny * ly + nz * lz) / nlen)
                    items.append((depth, pts, bright))
                else:
                    items.append((depth, pts, 1.0))
            if items:
                items.sort(key=lambda it: -it[0])   # 远 → 近
                painter.setPen(Qt.PenStyle.NoPen)
                for _depth, pts, bright in items:
                    fc = QColor(col)
                    if opaqueRender:
                        fc.setRed(min(255, int(fc.red() * bright)))
                        fc.setGreen(min(255, int(fc.green() * bright)))
                        fc.setBlue(min(255, int(fc.blue() * bright)))
                    else:
                        fc.setAlpha(110 if selected else 80)
                    painter.setBrush(fc)
                    painter.drawPolygon(QPolygonF(pts))
                if opaqueRender:
                    return
        # 线框：底/顶闭合轮廓 + 竖棱
        pen = QPen(col, width)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        base, top = obj.basePoints(), obj.topPolyline()
        n = len(base)
        zbuf = depthBuf[0] if depthBuf is not None else None
        zW = depthBuf[1] if depthBuf is not None else 0
        zH = depthBuf[2] if depthBuf is not None else 0
        if n >= 3 and len(top) == n:

            def _pl(p):
                pr = self._project((p.x, p.y, p.z), cam, w, h)
                return None if pr is None else pr

            def _drawSeg(p0, p1):
                if p0 is None or p1 is None:
                    return
                if zbuf is not None:
                    self._drawLineTested(painter, zbuf, zW, zH,
                                         p0[0], p0[1], p0[2],
                                         p1[0], p1[1], p1[2])
                else:
                    painter.drawLine(QPointF(p0[0], p0[1]),
                                     QPointF(p1[0], p1[1]))

            for src in (base, top):
                for i in range(n):
                    _drawSeg(_pl(src[i]), _pl(src[(i + 1) % n]))
            for i in range(n):
                _drawSeg(_pl(base[i]), _pl(top[i]))

    def _solidPickDist(self, obj, mx: float, my: float, cam, w: float,
                       h: float) -> float:
        """实体拾取距离（屏幕像素）：投影底/顶面内部命中为 0，
        否则取底/顶轮廓边与竖棱的最近线段距离；不可见返回 PICK_RADIUS。"""
        best = PICK_RADIUS
        for ring in (obj.basePoints(), obj.topPolyline()):
            pts = [(p.x, p.y, p.z) for p in ring]
            if len(pts) < 3:
                continue
            sp = [self._project(pt, cam, w, h) for pt in pts]
            if all(s is not None for s in sp) and \
                    self._pointInPoly(mx, my, [(s[0], s[1]) for s in sp]):
                return 0.0
            for i in range(len(sp)):
                s1, s2 = sp[i], sp[(i + 1) % len(sp)]
                if s1 is None or s2 is None:
                    continue
                d = self._screenSegDist(mx, my, s1, s2)
                if d < best:
                    best = d
        base, top = obj.basePoints(), obj.topPolyline()
        if len(base) >= 3 and len(top) == len(base):
            for i in range(len(base)):
                s1 = self._project((base[i].x, base[i].y, base[i].z), cam, w, h)
                s2 = self._project((top[i].x, top[i].y, top[i].z), cam, w, h)
                if s1 is None or s2 is None:
                    continue
                d = self._screenSegDist(mx, my, s1, s2)
                if d < best:
                    best = d
        return best

    def _drawDimension3D(self, painter, obj, col, pen, cam, w: float, h: float) -> None:
        """尺寸标注在 3D 视口的投影绘制：尺寸线/延伸线/箭头与读数文字。

        标注可位于 XY 平面（z=0，XY/linear kind）或真 3D 位置（z kind）。
        - lines: 4 元组 (x1,y1,x2,y2) 视为 z=0；6 元组 (x1,y1,z1,x2,y2,z2) 为 3D
        - fills: 2 元组点 (x,y) 视为 z=0；3 元组点 (x,y,z) 为 3D
        - texts: 带 "cz" 键用 3D 投影，否则 z=0
        读数文字在屏幕空间绘制：字号按中心点处的世界→像素比例换算，方向取
        文本右向向量投影后的屏幕角度，斜视角下文字仍横平竖直可读。
        """
        lay = obj.layout()

        # ---- 尺寸线 / 延伸线 ----
        segs = []
        for seg in lay["lines"]:
            if len(seg) == 6:
                x1, y1, z1, x2, y2, z2 = seg
                a = self._projectSegment((x1, y1, z1), (x2, y2, z2), cam, w, h)
            else:
                x1, y1, x2, y2 = seg
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
                for p in tri:
                    if len(p) == 3:
                        pr = self._project((p[0], p[1], p[2]), cam, w, h)
                    else:
                        pr = self._project((p[0], p[1], 0.0), cam, w, h)
                    if pr is None:
                        ok = False
                        break
                    pts.append(QPointF(pr[0], pr[1]))
                if ok and len(pts) == 3:
                    painter.drawPolygon(QPolygonF(pts))

        # ---- 读数文字（屏幕空间矢量字形，随投影缩放） ----
        # Z 标注（带 cz 的文字）采用 billboard 方式：文字位置用 3D 投影，
        # 但方向固定为屏幕竖直（世界角度直接当屏幕角度），不随相机倾斜旋转，
        # 字号按 Z 方向的世界→像素比例缩放——斜视角下文字始终可读。
        # XY/linear 标注（不带 cz）按 3D 投影方向旋转文字。
        texts = lay["texts"]
        fs = obj.fontHeight()
        if not texts or fs <= 1e-9:
            return
        _K = 64.0                       # 字形放大倍数（与 2D 图元一致保证小字清晰）
        font = QFont(app_settings.dim_font or "Microsoft YaHei")
        for t in texts:
            cz = t.get("cz", 0.0)
            is3D = "cz" in t            # Z 标注文字：billboard 渲染
            pc = self._project((t["cx"], t["cy"], cz), cam, w, h)
            if pc is None:
                continue
            a = math.radians(t["angle"])
            if is3D:
                # billboard：字号按 Z 方向投影比例，方向固定为屏幕角度
                pr = self._project((t["cx"], t["cy"], cz + 1.0), cam, w, h)
                if pr is None:
                    continue
                unit = math.hypot(pr[0] - pc[0], pr[1] - pc[1])
                scrAng = t["angle"]     # 世界角度直接当屏幕角度
            else:
                pr = self._project((t["cx"] + math.cos(a),
                                    t["cy"] + math.sin(a), cz), cam, w, h)
                if pr is None:
                    continue
                unit = math.hypot(pr[0] - pc[0], pr[1] - pc[1])
                scrAng = math.degrees(math.atan2(pr[1] - pc[1], pr[0] - pc[0]))
            fsPx = fs * unit
            if fsPx < 2.0:
                continue            # 太小（侧视角）省略，避免糊成一团
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
        if t == ObjectType.SOLID:
            # 挤出实体：取底面轮廓（isClosed=True 闭合），供框选/命中判定
            return [(p.x, p.y, p.z) for p in obj.basePoints()]
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
        if self._canvasMode in ("3d", "cnc"):
            self._drawCommandPreview3D(painter, inst, cam, w, h)
            # 命令自定义叠加层（推拉手柄等）：3D 预览分支内部有多个
            # return，故在手柄 overlay 统一在分支后绘制，保证手柄置顶。
            ov = getattr(inst, "drawOverlay", None)
            if callable(ov):
                try:
                    ov(painter, self)
                except Exception:
                    pass
            return
        path = inst.previewPath()
        ppen = inst.previewPen()
        if path is not None and ppen is not None:
            # 与 2D 完全同一套预览画笔：直接复用命令预览图元的 pen，
            # 线型/颜色/线宽全部由命令决定，这里不做改写，也不回退青色虚线。
            pen = QPen(ppen)
            pen.setCosmetic(True)
            painter.setPen(pen)
            # 拍平为折线，支持预览中的圆弧等曲线元素（arcTo）。
            # 恒等变换的拍平精度极粗（圆只有 9 段，看着是多边形），改用放大
            # 变换参与拍平——细分段数随变换尺度提高（100x ≈ 129 段）——再缩回。
            _Z = 100.0
            for poly in path.toSubpathPolygons(QTransform.fromScale(_Z, _Z)):
                if len(poly) < 2:
                    continue
                prev = (poly[0].x() / _Z, poly[0].y() / _Z, 0.0)
                for i in range(1, len(poly)):
                    q = poly[i]
                    cur = (q.x() / _Z, q.y() / _Z, 0.0)
                    seg = self._projectSegment(prev, cur, cam, w, h)
                    if seg is not None:
                        painter.drawLine(QPointF(*seg[0]), QPointF(*seg[1]))
                    prev = cur
        # 光标预览点：仅当命令声明需要时才画（推拉等不需要光标点）
        mk = inst.previewMarker() if inst.needsCursorMarker() else None
        if mk is not None:
            pr = self._project((mk[0], mk[1], 0.0), cam, w, h)
            if pr is not None:
                from ui.view_2d.item_factory import POINT_MARKER_PX
                painter.setPen(QPen(QColor("#15b6c9"), 1.5))
                painter.setBrush(QColor(Qt.GlobalColor.white))
                painter.drawEllipse(QPointF(pr[0], pr[1]),
                                    POINT_MARKER_PX, POINT_MARKER_PX)

    def _drawCommandPreview3D(self, painter, inst, cam, w: float, h: float) -> None:
        """3D 模式命令预览：真实三维绘制。

        * 逐点型命令（直线/曲线，有 lastInputPoint）：画"已定点 → 当前取点"
          的真实 3D 尾段（透视/顶视/侧视下都精确，含捕捉带出的 Z）；
        * 拖拽型命令（圆/弧/椭圆等）：顶视/透视下 2D 预览路径按构造平面
          高度 drawZ 投影；侧视下路径 (x,y) 即视图平面 (u,v)，映射到
          XZ/YZ 平面投影（侧视画形状已支持）。
        """
        last = inst.lastInputPoint()
        cur = self._lastResolved
        # 挤出等命令的实体预览（青色半透明面 + 线框，风格同预览虚线）
        mkSolid = getattr(inst, "previewSolid", None)
        if callable(mkSolid):
            pobj = mkSolid()
            if pobj is not None:
                if isinstance(pobj, BRepObject):
                    pass    # B-Rep 预览实体已并入 z-buffer 通道绘制
                else:
                    self._drawSolid(painter, pobj, cam, w, h,
                                    color=QColor("#15b6c9"))
        # 挤出等命令：已拾取的源曲线置顶高亮（金色粗线，覆盖在
        # 预览实体之上，用户"识别到"曲线的瞬间不会被实体盖没）
        pc = getattr(inst, "_previewCurve", None)
        if pc is not None:
            ppts = self._objectPolyline(pc)
            if len(ppts) >= 2:
                ppen = QPen(QColor("#ffd54f"), 2.0)
                ppen.setCosmetic(True)
                painter.setPen(ppen)
                isc = getattr(pc, "isClosed", None)
                closed = bool(isc()) if callable(isc) else False
                n = len(ppts)
                segN = n if closed else n - 1
                for i in range(segN):
                    s0 = ppts[i]
                    s1 = ppts[(i + 1) % n]
                    seg = self._projectSegment(s0, s1, cam, w, h)
                    if seg is not None:
                        painter.drawLine(QPointF(*seg[0]), QPointF(*seg[1]))
        # 光标预览点：仅当命令声明需要时跟随最近一次取点（挤出/推拉等实体命令不要点）
        from ui.view_2d.item_factory import POINT_MARKER_PX
        if cur is not None and inst.needsCursorMarker():
            pr = self._project((cur.x, cur.y, cur.z), cam, w, h)
            if pr is not None:
                painter.setPen(QPen(QColor("#15b6c9"), 1.5))
                painter.setBrush(QColor(Qt.GlobalColor.white))
                painter.drawEllipse(QPointF(pr[0], pr[1]),
                                    POINT_MARKER_PX, POINT_MARKER_PX)
        if last is not None and cur is not None \
                and not getattr(inst, "previewPlaneBased", False):
            # 真实 3D 尾段（实线，与 2D 逐点命令预览同风格）。点拾取可能被
            # 吸附带出构造平面（Z ≠ drawZ），顶视/透视下路径平面化会失真，
            # 故逐点命令只画尾段；侧视取点恒在视图平面内，尾段后继续画
            # 完整预览路径（曲线已画段/橡皮带等）。
            seg = self._projectSegment((last.x, last.y, last.z),
                                       (cur.x, cur.y, cur.z), cam, w, h)
            if seg is not None:
                # 尾段与 2D 预览用同一支画笔（逐点命令 = 实线 + 对象色）
                ppen = inst.previewPen()
                if ppen is not None:
                    pen = QPen(ppen)
                    pen.setCosmetic(True)
                    painter.setPen(pen)
                    painter.drawLine(QPointF(*seg[0]), QPointF(*seg[1]))
            if self._mode not in (2, 3):
                return
        if self._mode == 2 or self._mode == 3:
            # 侧视拖拽型命令：路径 (x,y) 即视图平面 (u,v) →
            # 世界 (X,Z)（前视，y=0 平面）/ (Y,Z)（右视，x=0 平面）
            path = inst.previewPath()
            ppen = inst.previewPen()
            if path is None or ppen is None:
                return
            pen = QPen(ppen)
            pen.setCosmetic(True)
            painter.setPen(pen)
            _Z = 100.0

            def _wpt(q):
                px, py = q.x() / _Z, q.y() / _Z
                return (px, 0.0, py) if self._mode == 2 else (0.0, px, py)

            for poly in path.toSubpathPolygons(QTransform.fromScale(_Z, _Z)):
                if len(poly) < 2:
                    continue
                prev = _wpt(poly[0])
                for i in range(1, len(poly)):
                    p3 = _wpt(poly[i])
                    seg = self._projectSegment(prev, p3, cam, w, h)
                    if seg is not None:
                        painter.drawLine(QPointF(*seg[0]), QPointF(*seg[1]))
                    prev = p3
            return
        if self._mode == 0 or self._mode == 1:
            # 拖拽型命令：预览路径落在构造平面上（z = drawZ）
            path = inst.previewPath()
            ppen = inst.previewPen()
            if path is None or ppen is None:
                return
            pen = QPen(ppen)
            pen.setCosmetic(True)
            painter.setPen(pen)
            _Z = 100.0
            for poly in path.toSubpathPolygons(QTransform.fromScale(_Z, _Z)):
                if len(poly) < 2:
                    continue
                prev = (poly[0].x() / _Z, poly[0].y() / _Z, self._drawZ)
                for i in range(1, len(poly)):
                    q = poly[i]
                    p3 = (q.x() / _Z, q.y() / _Z, self._drawZ)
                    seg = self._projectSegment(prev, p3, cam, w, h)
                    if seg is not None:
                        painter.drawLine(QPointF(*seg[0]), QPointF(*seg[1]))
                    prev = p3

    def _paintSnapMarker(self, painter, cam, w: float, h: float) -> None:
        """捕捉标记：世界点投影到屏幕后调用共用的 paintSnapMarker（同 2D）。"""
        snap = self._lastSnap
        if snap is None or not snap.isValid:
            return
        mp = snap.effectiveMarker
        p = self._project((mp.x, mp.y, mp.z), cam, w, h)
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

    # ---------------- 文件拖入视口 ----------------

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
        wp = self._screenToXYPlane(event.position().x(), event.position().y())
        self.fileDropped.emit(paths, wp)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        # 浮雕模式全局 Shift+左键拾色（与空格填色同定位，不限命令内）：
        # 点到浮雕模型的色号格 → 设为调色板当前颜色；未命中则走原有逻辑
        if event.button() == Qt.LeftButton \
                and (event.modifiers() & Qt.KeyboardModifier.ShiftModifier) \
                and self._pickColorAt(event.position()):
            event.accept()
            return
        # 浮雕模式全局 Ctrl+左键涂抹（堆料/去料，不限命令内——磨光进行中
        # 也可快速堆料/去料）：按下先"待定"，移动超阈值才成笔画，
        # 原地点击释放时仍走点选（Ctrl=追加/反选）
        # 例外：活动命令声明使用 Ctrl+点击（如区域塑形 Ctrl=内岛参与）时，
        # 由命令接管，不进入涂抹待定。
        ctrl_smear = event.button() == Qt.LeftButton \
            and (event.modifiers() & Qt.KeyboardModifier.ControlModifier) \
            and self._reliefModel is not None
        if ctrl_smear and self._cm is not None and self._cm.hasActive():
            inst = self._cm.activeInstance()
            claim = getattr(inst, "usesCtrlClick", None)
            if callable(claim) and claim():
                ctrl_smear = False
        if ctrl_smear:
            self._smearPending = QPointF(event.position())
            self._smearMods = event.modifiers()
            event.accept()
            return
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
        # XY 标注子标注拖动：已选中标注的读数文字按下即开始（可在 Y/X 向单独拉动）
        if event.button() == Qt.LeftButton and self._doc is not None:
            hit = self._dimHandleAt(event.position())
            if hit is not None:
                obj, axis = hit
                self._dimDragObj = obj
                self._dimDragAxis = axis
                self._dimStartWorld = self._screenToXYPlane(
                    event.position().x(), event.position().y())
                self._dimStartOx, self._dimStartOy = obj.xyOffsets()
                self._doc.beginEdit()
                self.setCursor(Qt.SizeVerCursor if axis == "x"
                               else Qt.SizeHorCursor)
                event.accept()
                return
        # 左键：由框选状态机接管（点击延迟到释放判定；控制点/操作轴命中
        # 已在上方分支提前返回）。不在按下时清选——曲线编辑模式下清选会
        # 退出编辑、控制点消失，导致无法框选控制点。清选在释放时按情况
        # 处理（点空白 → 清选，框选 → 选控制点/对象），与 2D 画布一致。
        if event.button() == Qt.LeftButton and self._doc is not None:
            self._boxStart = event.position()
            self._boxMods = event.modifiers()
            self._boxActive = False
            event.accept()
            return
        self._drag = (event.button(), event.position())
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        # 底部状态栏坐标实时跟随（浮雕模式：Z = 鼠标处模型高度；
        # 3D 模式：Z = 当前构造平面高度）
        wpRaw = self._screenToXYPlane(event.position().x(), event.position().y())
        if wpRaw is not None:
            z = self._drawZ if self._canvasMode in ("3d", "cnc") else self._modelZAt(wpRaw)
            self.mouseMoved.emit(Point3D(wpRaw.x, wpRaw.y, z))
        # Ctrl+涂抹：待定状态下移动超阈值 → 成笔画；中途松 Ctrl 取消待定
        if self._smearPending is not None:
            if not (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                self._smearPending = None
            else:
                d = event.position() - self._smearPending
                if math.hypot(d.x(), d.y()) > 3.0:
                    self._beginSmearStroke(event.position())
        if self._smearActive:
            self._smearMoveTo(event.position())
            event.accept()
            return
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
        if self._dimDragObj is not None:
            # XY 子标注拖动：X 宽标注只取 Y 分量、Y 高标注只取 X 分量
            wpt = self._screenToXYPlane(event.position().x(),
                                        event.position().y())
            if wpt is not None and self._dimStartWorld is not None:
                if self._dimDragAxis == "x":
                    oy = self._dimStartOy - (wpt.y - self._dimStartWorld.y)
                    self._dimDragObj.setXYOffset(self._dimStartOx, oy)
                else:
                    ox = self._dimStartOx - (wpt.x - self._dimStartWorld.x)
                    self._dimDragObj.setXYOffset(ox, self._dimStartOy)
                self._doc.notifyObjectChanged(self._dimDragObj.id())
                self.update()
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
            self._updateDimCursor(event.position())
            # 子元素悬停高亮（点/线/面过滤开启时，3D 模式下）
            subKinds = {k for k in self._selFilters if k != "body"}
            if self._canvasMode in ("3d", "cnc") and subKinds \
                    and self._cm is not None and not self._cm.hasActive():
                hit = self._pickSubElement(event.position().x(),
                                           event.position().y(), subKinds)
                if hit != self._subHover:
                    self._subHover = hit
                    self.update()
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

    @staticmethod
    def _screenSegDist(mx: float, my: float, s1, s2) -> float:
        """点到屏幕线段的距离（拾取共用）。"""
        abx, aby = s2[0] - s1[0], s2[1] - s1[1]
        ab2 = abx * abx + aby * aby
        if ab2 < 1e-9:
            return math.hypot(mx - s1[0], my - s1[1])
        t = max(0.0, min(1.0, ((mx - s1[0]) * abx + (my - s1[1]) * aby) / ab2))
        return math.hypot(mx - (s1[0] + t * abx), my - (s1[1] + t * aby))

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
        # 浮雕模式：模型覆盖的屏幕区域按"上层"处理，图片在那里不可拾取
        #（图片画在模型下方、视觉上也看不见）；模型外的图片区域照常可选。
        relief_rect = None
        if self._canvasMode == "relief":
            tgt = self._reliefTarget()
            if tgt is not None and hasattr(tgt, "heightField"):
                relief_rect = self._reliefScreenRect(tgt, cam, w, h)
        for obj in self._doc.visibleObjects():
            if obj.isLocked():
                continue
            if obj.type() == ObjectType.POINT:
                pos = obj.position()
                p = self._project((pos.x, pos.y, pos.z), cam, w, h)
                if p is not None and math.hypot(mx - p[0], my - p[1]) < bestD:
                    best, bestD = obj, math.hypot(mx - p[0], my - p[1])
                continue
            if obj.type() == ObjectType.SOLID:
                # 挤出实体：底/顶投影多边形内部命中 + 边线距离
                d = self._solidPickDist(obj, mx, my, cam, w, h)
                if d < bestD:
                    best, bestD = obj, d
                continue
            if obj.type() == ObjectType.BREP:
                # B-Rep 实体：投影三角形命中（深度最近优先）+ 边线距离
                tris = obj.triangles()
                if len(tris):
                    bestDepth = -1e18
                    for t in tris:
                        sp = [self._project((t[k][0], t[k][1], t[k][2]),
                                            cam, w, h) for k in range(3)]
                        if any(s is None for s in sp):
                            continue
                        if self._pointInPoly(mx, my,
                                             [(s[0], s[1]) for s in sp]):
                            depth = sum(s[2] for s in sp) / 3.0
                            if depth > bestDepth:
                                best, bestD, bestDepth = obj, 0.0, depth
                        else:
                            for k in range(3):
                                d = self._screenSegDist(mx, my, sp[k],
                                                        sp[(k + 1) % 3])
                                if d < bestD:
                                    best, bestD = obj, d
                if best is obj:
                    continue
                for poly in obj.topology()["edgePolylines"]:
                    for i in range(len(poly) - 1):
                        s1 = self._project(poly[i], cam, w, h)
                        s2 = self._project(poly[i + 1], cam, w, h)
                        if s1 is None or s2 is None:
                            continue
                        d = self._screenSegDist(mx, my, s1, s2)
                        if d < bestD:
                            best, bestD = obj, d
                continue
            if obj.type() == ObjectType.DIMENSION:
                # 标注：逐尺寸线段投影求屏幕距离（含箭头三角边）
                lay = obj.layout()
                for seg in lay.get("lines", []):
                    if len(seg) == 6:
                        x1, y1, z1, x2, y2, z2 = seg
                        s1 = self._project((x1, y1, z1), cam, w, h)
                        s2 = self._project((x2, y2, z2), cam, w, h)
                    else:
                        x1, y1, x2, y2 = seg
                        s1 = self._project((x1, y1, 0.0), cam, w, h)
                        s2 = self._project((x2, y2, 0.0), cam, w, h)
                    if s1 is None or s2 is None:
                        continue
                    d = self._screenSegDist(mx, my, s1, s2)
                    if d < bestD:
                        best, bestD = obj, d
                continue
            if obj.type() == ObjectType.IMAGE:
                # 参考图片：投影四边形内部命中（含边线小距离）。
                # 浮雕模式下被模型盖住的部分按上层处理，不拾取图片。
                if relief_rect is not None and \
                        relief_rect.contains(QPointF(mx, my)):
                    continue
                sp = [self._project((p.x, p.y, p.z), cam, w, h)
                      for p in obj.points()]
                if all(s is not None for s in sp) and \
                        self._pointInPoly(mx, my, [(s[0], s[1]) for s in sp]):
                    best, bestD = obj, 0.0
                continue
            isSurface = obj.type() == ObjectType.SURFACE
            if not isSurface and obj.type() == ObjectType.POLYCURVE:
                # 多段曲线（侧视烘焙的圆/弧/椭圆等）：逐子段投影求屏幕距离。
                # 子段间不连线（各子段独立折线，与渲染分支同结构）。
                for seg in obj.segments():
                    spts = seg.get("points", [])
                    for i in range(len(spts) - 1):
                        s1 = self._project(tuple(spts[i]), cam, w, h)
                        s2 = self._project(tuple(spts[i + 1]), cam, w, h)
                        if s1 is None or s2 is None:
                            continue
                        d = self._screenSegDist(mx, my, s1, s2)
                        if d < bestD:
                            best, bestD = obj, d
                continue
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

    def _dimHandleAt(self, pos) -> tuple | None:
        """光标下的可拖动 XY 子标注 (obj, axis)。axis='x' 表示 X 宽标注
        （上下拖动改 yOff），'y' 表示 Y 高标注（左右拖动改 xOff）。
        仅当该标注已被选中且未锁定；读数文字中心即拖动柄（命中半径 12px）。
        3D 视口下标注位于 z=0 平面，文字中心投影到屏幕后做屏幕距离判定。"""
        if self._doc is None:
            return None
        w, h = float(self.width()), float(self.height())
        if w < 2 or h < 2:
            return None
        cam = self._camera()
        mx, my = pos.x(), pos.y()
        best = None
        bestDist = 12.0
        for obj in self._doc.visibleObjects():
            if obj.type() != ObjectType.DIMENSION:
                continue
            if obj.isLocked() or not obj.isSelected():
                continue
            if obj.kind() != "xy":
                continue
            lay = obj.layout()
            for t in lay["texts"]:
                cz = t.get("cz", 0.0)
                p = self._project((t["cx"], t["cy"], cz), cam, w, h)
                if p is None:
                    continue
                d = math.hypot(p[0] - mx, p[1] - my)
                if d <= bestDist:
                    bestDist = d
                    a = abs(t["angle"] % 180.0)
                    axis = "y" if abs(a - 90.0) < 1.0 else "x"
                    best = (obj, axis)
        return best

    def _updateDimCursor(self, pos) -> None:
        """悬停 XY 标注读数文字时给出上下/左右可拖光标提示。"""
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

    def _applyPickSelection(self, obj, mods) -> None:
        """拾取结果写入文档选择：Ctrl=追加/反选，Shift=加选，否则单选；点空白=清空。
        非 Ctrl 点选命中群组成员时，同组其它未锁定成员一并选中（整组联动，与 2D 一致）。"""
        if mods & Qt.ControlModifier:
            if obj is not None:
                obj.setSelected(not obj.isSelected())
                self._doc.selectionChanged.emit()
            return
        if mods & Qt.ShiftModifier:
            if obj is not None:
                obj.setSelected(True)
                gid = obj.group()
                if gid:
                    for o in self._doc.objects():
                        if o.id() != obj.id() and o.group() == gid \
                                and not o.isLocked() and not o.isSelected():
                            o.setSelected(True)
                self._doc.selectionChanged.emit()
            return
        sel = self._doc.selectedObjects()
        if obj is not None and len(sel) == 1 and sel[0] is obj:
            return
        for o in sel:
            o.setSelected(False)
        if obj is not None:
            obj.setSelected(True)
            gid = obj.group()
            if gid:
                for o in self._doc.objects():
                    if o.id() != obj.id() and o.group() == gid \
                            and not o.isLocked():
                        o.setSelected(True)
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
        Shift 加选 / Ctrl 减选（同 2D 选择约定）。
        非 Ctrl 框选命中群组任一成员则整组选中（与 2D 画布一致）。"""
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

    def _objectInScreenRect(self, obj, sr: QRectF, leftToRight: bool) -> bool:
        """对象投影到屏幕后的包含/相交判定（与 2D 同规则）。"""
        w, h = float(self.width()), float(self.height())
        cam = self._camera()
        if obj.type() == ObjectType.POINT:
            pos = obj.position()
            p = self._project((pos.x, pos.y, pos.z), cam, w, h)
            return p is not None and sr.contains(QPointF(p[0], p[1]))
        if obj.type() == ObjectType.POLYCURVE:
            # 多段曲线（侧视烘焙的圆/弧/椭圆等）：全部子段投影点参与
            # 包含/相交判定（子段间不连线，与渲染分支同结构）
            spSegs = []
            for seg in obj.segments():
                spts = [tuple(p) for p in seg.get("points", [])]
                if len(spts) >= 2:
                    spSegs.append([self._project(p, cam, w, h) for p in spts])
            if not spSegs:
                return False
            if leftToRight:
                # 包含选择：全部子段的全部投影点都在框内
                return all(q is not None and sr.contains(QPointF(q[0], q[1]))
                           for sp in spSegs for q in sp)
            # 相交选择：任一投影点在框内，或任一子段线段与框相交
            if any(q is not None and sr.contains(QPointF(q[0], q[1]))
                   for sp in spSegs for q in sp):
                return True
            for sp in spSegs:
                for i in range(len(sp) - 1):
                    a, b = sp[i], sp[i + 1]
                    if a is None or b is None:
                        continue
                    if _segIntersectsRect(QPointF(a[0], a[1]),
                                          QPointF(b[0], b[1]), sr):
                        return True
            return False
        if obj.type() == ObjectType.BREP:
            # B-Rep 实体：网格顶点投影参与包含/相交判定（与点选同数据源）
            try:
                m = obj.mesh()
                verts = m.get("verts", []) if m else []
                tris = m.get("tris", []) if m else []
            except Exception:
                verts, tris = [], []
            spAll = [self._project(tuple(v), cam, w, h) for v in verts]
            ok = [q for q in spAll if q is not None]
            if not ok:
                return False
            if leftToRight:
                # 包含选择：全部投影顶点在框内（投影失败视为不可见，不参与）
                return all(sr.contains(QPointF(q[0], q[1])) for q in ok)
            # 相交选择：任一顶点在框内，或任一三角形投影边与框相交
            if any(sr.contains(QPointF(q[0], q[1])) for q in ok):
                return True
            for ti in tris:
                i0, i1, i2 = int(ti[0]), int(ti[1]), int(ti[2])
                if i0 >= len(spAll) or i1 >= len(spAll) or i2 >= len(spAll):
                    continue
                ss = (spAll[i0], spAll[i1], spAll[i2])
                if any(s is None for s in ss):
                    continue
                for k in range(3):
                    a, b = ss[k], ss[(k + 1) % 3]
                    if _segIntersectsRect(QPointF(a[0], a[1]),
                                          QPointF(b[0], b[1]), sr):
                        return True
            return False
        if obj.type() == ObjectType.IMAGE:
            # 参考图片：投影四角参与包含/相交判定（与 2D 同规则）
            sp = [self._project((p.x, p.y, p.z), cam, w, h)
                  for p in obj.points()]
            poly = [(s[0], s[1]) for s in sp if s is not None]
            if not poly:
                return False
            if leftToRight:
                return all(sr.contains(QPointF(x, y)) for x, y in poly)
            if any(sr.contains(QPointF(x, y)) for x, y in poly):
                return True
            for i in range(len(poly)):
                a = QPointF(poly[i][0], poly[i][1])
                b = QPointF(poly[(i + 1) % len(poly)][0],
                            poly[(i + 1) % len(poly)][1])
                if _segIntersectsRect(a, b, sr):
                    return True
            return False
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
        # 3D 模式选择过滤（点/线/面/实体）：
        # 主键盘 1 点 / 2 线 / 3 面 / 4 实体（不响应小键盘）
        # 普通=切换(单选)，Shift=加选/减选
        if self._canvasMode in ("3d", "cnc") and \
                (self._cm is None or not self._cm.hasActive()):
            keyMap = {Qt.Key_1: "vertex", Qt.Key_2: "edge",
                      Qt.Key_3: "face", Qt.Key_4: "body"}
            if event.key() in keyMap and \
                    not (event.modifiers() & Qt.KeypadModifier):
                if event.modifiers() & Qt.ShiftModifier:
                    self.toggleSelectionMode(keyMap[event.key()])
                else:
                    self.setSelectionMode(keyMap[event.key()])
                event.accept()
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

    def _modelZAt(self, wp) -> float:
        """浮雕模式鼠标处模型高度：锁定模型高度场的最近邻格点值；无模型/范围外为 0。"""
        model = self._reliefModel
        if model is None:
            return 0.0
        hf = model.heightField()
        ox, oy = hf.origin
        col = int(round((wp.x - ox) / hf.spacing))
        row = int(round((wp.y - oy) / hf.spacing))
        if 0 <= row < hf.ny and 0 <= col < hf.nx:
            return float(hf.data[row, col])
        return 0.0

    def _pickColorAt(self, pos: QPointF) -> bool:
        """浮雕模式全局 Shift+左键拾色：把点击处格子的色号设为当前颜色。

        与空格填色同一定位——“先填色再启动命令”工作流的全局操作，不限命令内。
        点击命中浮雕模型内的格子即拾取并返回 True（未填色格提示无法拾取）；
        无浮雕模型或点在模型外返回 False，鼠标事件回退到原有处理（命令取点/选择）。
        """
        if self._reliefModel is None or self._doc is None:
            return False
        model = self._reliefModel
        # 模型若有未烘焙的放置变换，先烘焙对齐（与空格填色同一约定）
        if model.hasPendingTransform():
            self._doc.beginEdit()
            try:
                model.bake()
                self._doc.notifyObjectChanged(model.id())
            finally:
                self._doc.endEdit()
        wp = self._screenToXYPlane(pos.x(), pos.y())
        if wp is None:
            return False
        hf = model.heightField()
        ox, oy = hf.origin
        col = int(round((wp.x - ox) / hf.spacing))
        row = int(round((wp.y - oy) / hf.spacing))
        if not (0 <= row < hf.ny and 0 <= col < hf.nx):
            return False
        cm = model.colorMask()
        value = int(cm[row, col]) \
            if cm is not None and cm.shape == (hf.ny, hf.nx) else 0
        if value <= 0:
            self._notifyGlobal("该处未填色，无法拾取颜色")
            return True
        PaintState.instance().setCurrentIndex(value - 1)   # 蒙版值 → 调色板索引
        self._notifyGlobal(
            f"已拾取 {PaintState.instance().currentHex()} 为当前颜色")
        return True

    def _notifyGlobal(self, text: str) -> None:
        """浮雕模式全局操作的反馈：左下角信息区为主，无命令管理器时退回历史。"""
        if self._cm is not None:
            self._cm.notifyResult(text)
        else:
            self.statusMessage.emit(text)

    def _fillSelectedColor(self) -> None:
        """浮雕模式空格填色：
        - 闭合曲线：内部填当前色（区域划分蒙版，原逻辑）。
        - 开放曲线：以曲线为分界线，两侧交替填当前色与下一色，整个模型分 2 色。

        每次填色自成一次可撤销操作（与区域浮雕命令解耦）。
        """
        model = self._reliefModel
        if model is None or self._doc is None:
            return
        sel = [o for o in self._doc.selectedObjects()
               if o.type() != ObjectType.SURFACE and not o.isLocked()]
        closed_curves = [o for o in sel if self._isClosedCurve(o)]
        open_curves = [o for o in sel if not self._isClosedCurve(o)]
        if not closed_curves and not open_curves:
            self.statusMessage.emit("请先用左键选中曲线，再按空格填色（闭合填区域内，开放分两色）")
            return
        baked = model.hasPendingTransform()
        if baked:
            model.bake()
        hf = model.heightField()
        grid = (hf.origin[0], hf.origin[1], hf.spacing, hf.nx, hf.ny)
        ps = PaintState.instance()
        value = ps.currentValue()
        from core.relief import paint_palette
        other = (value % paint_palette.count()) + 1
        self._doc.beginEdit()
        total = 0
        try:
            # 开放曲线先整场分两色（fillColorMaskValues 整场覆盖）
            if open_curves:
                total += self._fillOpenCurvesTwoColors(open_curves, model,
                                                        value, other)
            # 闭合曲线局部填当前色（叠加在两色之上）
            for o in closed_curves:
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
            self.statusMessage.emit(f"已填色 {total} 格（{ps.currentHex()}）")
        else:
            self.statusMessage.emit("所选曲线未命中模型范围或未把模型分开")

    def _fillOpenCurvesTwoColors(self, open_curves, model,
                                 value: int, other: int) -> int:
        """开放曲线作为分界线，整个模型按连通域交替填 value/other 两色。

        每条开放曲线两端沿切向延伸到模型边界形成横穿模型的分界线，栅格化为
        障碍墙，4 连通连通域按识别顺序交替填两色。返回填色格数。
        """
        from scipy.ndimage import label
        from core.relief.auto_separate import _rasterize_barriers
        from core.relief.region_relief import resample_polyline
        hf = model.heightField()
        sampled = []
        for o in open_curves:
            pts = sample_curve_points(o, 256)
            if pts.shape[0] < 2:
                continue
            ext = self._extendCurveToBounds(pts, hf)
            # 按弧长加密到约 0.5 格步距，保证栅格化形成连续障碍墙
            length = float(np.linalg.norm(np.diff(ext, axis=0), axis=1).sum())
            n = max(2, int(np.ceil(length / max(hf.spacing * 0.5, 1e-9))))
            sampled.append(resample_polyline(ext, n))
        if not sampled:
            return 0
        barrier = _rasterize_barriers(hf, sampled)
        free_labels, n = label(~barrier)
        if n == 0:
            return 0
        values = np.zeros((hf.ny, hf.nx), dtype=np.uint8)
        for lab in range(1, n + 1):
            mask = free_labels == lab
            values[mask] = value if (lab - 1) % 2 == 0 else other
        values[barrier] = value
        model.fillColorMaskValues(values)
        return int((~barrier).sum())

    def _extendCurveToBounds(self, pts: np.ndarray, hf) -> np.ndarray:
        """把开放曲线两端沿切向延伸到模型边界，形成横穿模型的分界线。"""
        ox, oy = hf.origin
        sp = hf.spacing
        xmin, ymin = ox, oy
        xmax = ox + (hf.nx - 1) * sp
        ymax = oy + (hf.ny - 1) * sp
        parts = []
        if pts.shape[0] >= 2:
            d0 = pts[0] - pts[1]
            if float(np.linalg.norm(d0)) > 1e-9:
                exit0 = self._rayBoxExit(pts[0], d0, xmin, ymin, xmax, ymax)
                if exit0 is not None:
                    parts.append(np.array([exit0], dtype=np.float64))
            d1 = pts[-1] - pts[-2]
            if float(np.linalg.norm(d1)) > 1e-9:
                exit1 = self._rayBoxExit(pts[-1], d1, xmin, ymin, xmax, ymax)
                if exit1 is not None:
                    parts.append(np.array([exit1], dtype=np.float64))
        parts.insert(1 if parts else 0, pts)
        return np.vstack(parts)

    def _rayBoxExit(self, p, d, xmin, ymin, xmax, ymax):
        """从 p 沿方向 d 射线穿出矩形 [xmin,ymin,xmax,ymax] 的交点。"""
        t_vals = []
        if abs(d[0]) > 1e-12:
            for x in (xmax, xmin):
                t = (x - p[0]) / d[0]
                if t > 0:
                    t_vals.append(t)
        if abs(d[1]) > 1e-12:
            for y in (ymax, ymin):
                t = (y - p[1]) / d[1]
                if t > 0:
                    t_vals.append(t)
        if not t_vals:
            return None
        t = min(t_vals)
        return (p[0] + t * d[0], p[1] + t * d[1])

    # ---------------- 浮雕模式全局 Ctrl+涂抹（堆料/去料） ----------------

    def _smearBrushSize(self):
        """Ctrl+涂抹笔刷尺寸 (直径, 半衰直径)：活动命令若声明了笔刷尺寸
        （磨光命令——与磨光笔刷同大，A/S、D/F 实时跟随）则借用，否则用
        SmearState 的固定预设。"""
        if self._cm is not None:
            inst = self._cm.activeInstance()
            get = getattr(inst, "ctrlSmearBrushParams", None)
            if callable(get):
                bp = get()
                if bp:
                    return float(bp[0]), float(bp[1])
        return SMEAR_DIAMETER, SMEAR_HALF_LIFE

    def _smearParams(self) -> SweepParams:
        """Ctrl+涂抹笔刷参数：高斯笔刷，方向由 SmearState 模式决定
        （堆料=正高度累加 / 去料=负高度雕刻）。"""
        sign = -1.0 if SmearState.instance().isRemove() else 1.0
        diameter, half_life = self._smearBrushSize()
        return SweepParams(brush_shape="gauss",
                           diameter=diameter,
                           half_life_diameter=half_life,
                           height=sign * SMEAR_HEIGHT,
                           brush_type="uniform",
                           merge="accumulate",
                           color_scope="ignore")

    def _beginSmearStroke(self, pos: QPointF) -> None:
        """把待定按下升级为涂抹笔画：开独立撤销会话并立即盖第一下。"""
        wp = self._screenToXYPlane(pos.x(), pos.y())
        model = self._reliefModel
        self._smearPending = None
        if wp is None or model is None or self._doc is None:
            return
        # 模型若有未烘焙的放置变换，先烘焙对齐（并入本笔撤销会话）
        if model.hasPendingTransform():
            model.bake()
        # 笔画前清一次循环 GC，随后暂停自动 GC：重建产生大量临时数组，
        # 笔画中触发 gen2 全量回收会造成偶发上百毫秒停顿（模型"闪一下"）
        gc.collect()
        self._gcWasEnabled = gc.isenabled()
        gc.disable()
        self._doc.beginEdit()
        self._smearActive = True
        self._smearStamps = 0
        self._smearLast = (wp.x, wp.y)
        self._smearStampAt(model, wp)
        self._smearTouch(model, force=True)

    def _smearStampAt(self, model, wp: Point3D) -> None:
        """在 wp 处盖一次涂抹笔刷（按 SmearState 当前模式堆料/去料）。"""
        moved = apply_brush_stamp(model.heightField(), wp.x, wp.y,
                                  self._smearParams())
        if moved > 0:
            self._smearStamps += 1

    def _smearTouch(self, model, force: bool = False) -> None:
        """笔画期间节流推进版本+通知视口重绘（≈30fps），松开时强制刷新。

        与堆料/磨光笔刷同因：盖章与 3D 显示网格重建解耦，涂抹才顺滑。"""
        now = time.monotonic()
        # 33ms（≈30fps）：重建已通过 bincount 散射 + 索引拓扑缓存 + GC 守卫
        # 做到轻量，30fps 可稳定 hold 住，视觉更新更跟手
        if force or (now - self._smearLastNotify) >= 0.033:
            model.bumpVersion()
            self._doc.notifyObjectChanged(model.id())
            self._smearLastNotify = now

    def _smearMoveTo(self, pos: QPointF) -> None:
        """笔画中沿轨迹按弧长间距连续盖章（间距 = 直径 1/4，不小于一栅格）。"""
        wp = self._screenToXYPlane(pos.x(), pos.y())
        model = self._reliefModel
        if wp is None or model is None or self._smearLast is None:
            return
        diameter, _hl = self._smearBrushSize()
        lx, ly = self._smearLast
        dx, dy = wp.x - lx, wp.y - ly
        dist = math.hypot(dx, dy)
        sp = max(diameter * 0.25, model.heightField().spacing, 1e-6)
        if dist < sp:
            return
        n = int(dist // sp)
        ux, uy = dx / dist, dy / dist
        for i in range(1, n + 1):
            self._smearStampAt(model, Point3D(lx + ux * sp * i,
                                              ly + uy * sp * i, 0.0))
        self._smearLast = (lx + ux * sp * n, ly + uy * sp * n)
        self._smearTouch(model)
        self.update()

    def _endSmearStroke(self) -> None:
        """结束一笔（强制刷新视口后撤销会话出栈，反馈盖章次数与方向）。"""
        if not self._smearActive:
            return
        self._smearActive = False
        self._smearLast = None
        model = self._reliefModel
        if self._doc is not None:
            if model is not None:
                self._smearTouch(model, force=True)
            self._doc.endEdit()
        if self._gcWasEnabled and not gc.isenabled():
            gc.enable()
        label = _SMEAR_LABELS[_SMEAR_MODES.index(SmearState.instance().mode())] \
            if SmearState.instance().mode() in _SMEAR_MODES else "堆料"
        self._notifyGlobal(f"Ctrl 涂抹完成：{label} × {self._smearStamps} 章")

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        # Ctrl+涂抹笔画进行中：松开左键结束一笔（一次拖动 = 一次可撤销操作）
        if event.button() == Qt.LeftButton and self._smearActive:
            self._endSmearStroke()
            event.accept()
            return
        # Ctrl+左键原地释放（未拖动成笔画）：回退为普通点选（Ctrl=追加/反选）
        if event.button() == Qt.LeftButton and self._smearPending is not None:
            pos, mods = self._smearPending, self._smearMods
            self._smearPending = None
            if self._doc is not None:
                picked = self._pickObject(pos.x(), pos.y())
                self._applyPickSelection(picked, mods)
            event.accept()
            return
        # 命令激活时：释放事件转发（涂抹类命令据其结束笔画/撤销会话）
        if self._cm is not None and self._cm.hasActive():
            if event.button() == Qt.LeftButton:
                self._cm.onMouseRelease(event.modifiers())
                self.update()
                event.accept()
                return
        if event.button() == Qt.LeftButton and self._dimDragObj is not None:
            # XY 子标注拖动结束（无位移单击则不产生撤销记录）
            self._dimDragObj = None
            self._dimDragAxis = ""
            self._dimCursorSet = False
            self.setCursor(Qt.ArrowCursor)
            self._doc.endEdit()
            self.update()
            event.accept()
            return
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
                picked = event.position()
                pickedMods = mods
                # 选择过滤（点/线/面/实体 任意组合）：开启子元素过滤时优先
                # 拾取子元素。命中挤出实体（SOLID）时先原地转换为 B-Rep
                # （一次撤销），再重试子元素拾取。
                subKinds = {k for k in self._selFilters if k != "body"}
                if subKinds and self._canvasMode in ("3d", "cnc"):
                    hit = self._pickSubElement(picked.x(), picked.y(),
                                               subKinds)
                    if hit is None and "body" in self._selFilters:
                        obj = self._pickObject(picked.x(), picked.y())
                        if obj is not None and obj.type() == ObjectType.SOLID \
                                and not obj.isLocked():
                            conv = self._convertSolidInDoc(obj)
                            if conv is not None:
                                hit = self._pickSubElement(picked.x(),
                                                           picked.y(),
                                                           subKinds)
                    if hit is not None:
                        if self._autoPushPullReady() \
                                and hit[1] in ("face", "edge"):
                            # 推拉常驻：点面/线直接出推拉手柄（免按钮）
                            self._autoStartPushPull(
                                picked.x(), picked.y(), pickedMods)
                        else:
                            self._applySubPick(hit, pickedMods)
                        event.accept()
                        return
                    self._subSel = []
                    self._subHover = None
                    if "body" not in self._selFilters:
                        # 点空白（子元素过滤下）：同样取消对象选择，
                        # 保证选中曲线/实体后点空白能取消选中
                        sel = self._doc.selectedObjects()
                        if sel:
                            for o in sel:
                                o.setSelected(False)
                            self._doc.selectionChanged.emit()
                        event.accept()
                        return
                obj = self._pickObject(picked.x(), picked.y())
                if obj is not None:
                    self._subSel = []
                    self._subHover = None
                self._applyPickSelection(obj, pickedMods)
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
        if app_settings.invert_wheel_zoom:
            angle = -angle   # 设置中开启“反向滚轮缩放”
        factor = 1.2 ** -angle
        w, h = float(self.width()), float(self.height())
        if w < 2 or h < 2:
            return
        cam = self._camera()
        mx = event.position().x()
        my = event.position().y()
        world = self._screenToGround(mx, my, cam, w, h)
        lo, hi = self._distLimits()
        self._dist = max(lo, min(hi, self._dist * factor))
        if world is not None and self._mode != 0:
            # 正交/顶视：pixelToWorld 的缩放比例本身就是精确的
            p = self._project(world, self._camera(), w, h)
            if p is not None:
                dx = mx - p[0]
                dy = my - p[1]
                wx, wy = self._pixelToWorld(dx, dy, self._camera(), w, h)
                self._pan = (self._pan[0] - wx, self._pan[1] - wy)
        # 透视模式：缩放围绕视口中心（相机注视点 pan），pan 不动，
        # 模型在屏幕上不漂移。围绕鼠标缩放时，鼠标点（尤其空白处）
        # 偏离模型越远，模型随透视被推得越远，看起来"跑很远"
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
        self._targetZ = 0.0
        if self._mode == 0:
            self._yaw = math.radians(135.0)
            self._pitch = math.radians(35.0)
            self._perspYaw, self._perspPitch = self._yaw, self._pitch
        self.update()

    def fitToBounds(self, bb) -> None:
        """相机适配到世界包围盒（保持当前视角方向/模式，仅调距离与目标点）。

        透视：以包围盒球半径适配较窄的视锥半角；正交：按 _orthoScale 反解
        距离。留 15% 边距；距离按场景尺寸自适应上下限（_distLimits）。
        """
        if bb is None:
            return
        c = bb.center()
        r_xy = 0.5 * math.hypot(bb.max.x - bb.min.x,
                                bb.max.y - bb.min.y)
        r = math.hypot(r_xy, 0.5 * (bb.max.z - bb.min.z))
        r = max(float(r), 1e-6)
        self._pan = (float(c.x), float(c.y))
        self._targetZ = 0.5 * (float(bb.min.z) + float(bb.max.z))
        w = max(float(self.width()), 1.0)
        h = max(float(self.height()), 1.0)
        if self._mode == 0:
            # 水平/垂直半角取较小者，保证两方向都在屏内
            hfov = 2.0 * math.atan(
                math.tan(math.radians(FOV_DEG) / 2.0) * (w / h))
            half = min(math.radians(FOV_DEG), hfov) * 0.5
            self._dist = r / math.sin(half) * 1.15
        else:
            # scale = min(w,h)*0.45/GRID_RANGE * DEFAULT_DIST/_dist
            self._dist = DEFAULT_DIST * r * 1.15 / GRID_RANGE
        lo, hi = self._distLimits()
        self._dist = min(max(self._dist, lo), hi)
        self.update()

    def fitReliefModel(self) -> None:
        """浮雕模式“全部观察”：相机适配到当前编辑模型（含 Z 夸张）。

        无编辑/选中模型时回退为 resetView。
        """
        model = self._reliefModel
        if model is None and self._doc is not None:
            model = self._doc.activeReliefModel()
        if model is None:
            self.resetView()
            return
        bb = model.getBoundingBox()
        # 显示网格 Z 乘以垂直夸张，适配按显示几何计
        hf = model.heightField()
        zex = float(model.zExaggeration())
        if hf.data.size:
            bb.max.z = max(bb.max.z, float(hf.data.max()) * zex)
        self.fitToBounds(bb)


"""捕捉管理器：按启用的捕捉类型在候选对象上求解吸附点。

支持 10 种对象捕捉（参考 AutoCAD OSNAP）：
端点 / 中点 / 圆心 / 节点 / 象限点 / 交点 / 垂足 / 切点 / 最近点 / 顶点。

捕捉平面轴：`snap(..., axes=(u, v))` 指定参与 2D 距离计算的两个世界轴。
默认 (0, 1)=XY（2D 画布/顶视）；3D 视口前视传 (0, 2)=XZ、右视传 (1, 2)=YZ，
使侧视下取点按屏幕平面度量距离——否则竖直方向的吸附会错误地吸到
XY 投影重合的点上（表现为前视画线第二个点被吸回第一个点）。
"""
from __future__ import annotations
import math
from collections import OrderedDict
from typing import List, Set, Optional, Tuple

import numpy as np

from .snap_types import SnapType, SnapResult
from ..vector.geometry_types import Point3D, ObjectType


_AXES_XY: Tuple[int, int] = (0, 1)

# 采样折线缓存上限（LRU 淘汰）：大场景对象数远超此值，原实现超限整体
# 清空会让缓存反复失效（清空后首帧重建上百个圆 → 数百 ms 卡顿）
_SAMPLE_CACHE_MAX = 2048


def _comp(p: Point3D, i: int) -> float:
    """点 p 的第 i 个世界分量（0=x, 1=y, 2=z）。"""
    return p.x if i == 0 else (p.y if i == 1 else p.z)


def _wAxis(axes: Tuple[int, int]) -> int:
    """捕捉平面外的第 3 轴索引。"""
    return 3 - axes[0] - axes[1]


def _nearestOnSegment(p: Point3D, a: Point3D, b: Point3D,
                      axes: Tuple[int, int] = _AXES_XY) -> Point3D:
    """点 p 到线段 ab 的最近点（按捕捉平面 (u,v) 度量，钳制到段内）。
    返回世界点：u,v 为投影最近点，平面外分量取 a 的值。"""
    u0, v0 = axes
    dx = _comp(b, u0) - _comp(a, u0)
    dy = _comp(b, v0) - _comp(a, v0)
    L2 = dx * dx + dy * dy
    if L2 < 1e-12:
        return a
    t = ((_comp(p, u0) - _comp(a, u0)) * dx + (_comp(p, v0) - _comp(a, v0)) * dy) / L2
    t = max(0.0, min(1.0, t))
    c = [0.0, 0.0, 0.0]
    c[u0] = _comp(a, u0) + t * dx
    c[v0] = _comp(a, v0) + t * dy
    c[_wAxis(axes)] = _comp(a, _wAxis(axes))
    return Point3D(c[0], c[1], c[2])


def _segSegIntersect(a1: Point3D, a2: Point3D, b1: Point3D, b2: Point3D,
                     axes: Tuple[int, int] = _AXES_XY):
    """两线段交点（按捕捉平面 (u,v) 求解；无交点返回 None）。
    返回世界点：u,v 为交点，平面外分量取 a1 的值。"""
    u0, v0 = axes
    d1x = _comp(a2, u0) - _comp(a1, u0)
    d1y = _comp(a2, v0) - _comp(a1, v0)
    d2x = _comp(b2, u0) - _comp(b1, u0)
    d2y = _comp(b2, v0) - _comp(b1, v0)
    denom = d1x * d2y - d1y * d2x
    if abs(denom) < 1e-12:
        return None
    t = ((_comp(b1, u0) - _comp(a1, u0)) * d2y -
         (_comp(b1, v0) - _comp(a1, v0)) * d2x) / denom
    s = ((_comp(b1, u0) - _comp(a1, u0)) * d1y -
         (_comp(b1, v0) - _comp(a1, v0)) * d1x) / denom
    if -1e-6 <= t <= 1 + 1e-6 and -1e-6 <= s <= 1 + 1e-6:
        c = [0.0, 0.0, 0.0]
        c[u0] = _comp(a1, u0) + t * d1x
        c[v0] = _comp(a1, v0) + t * d1y
        c[_wAxis(axes)] = _comp(a1, _wAxis(axes))
        return Point3D(c[0], c[1], c[2])
    return None


def _segCircleIntersect(a: Point3D, b: Point3D, cx: float, cy: float, r: float):
    """线段与圆交点列表（仅 XY 捕捉平面使用）。"""
    dx, dy = b.x - a.x, b.y - a.y
    fx, fy = a.x - cx, a.y - cy
    A = dx * dx + dy * dy
    B = 2 * (fx * dx + fy * dy)
    C = fx * fx + fy * fy - r * r
    disc = B * B - 4 * A * C
    if disc < 0 or A < 1e-12:
        return []
    sq = math.sqrt(disc)
    out = []
    for t in ((-B - sq) / (2 * A), (-B + sq) / (2 * A)):
        if -1e-6 <= t <= 1 + 1e-6:
            out.append(Point3D(a.x + t * dx, a.y + t * dy, a.z))
    return out


def _inArcRange(angle: float, sa: float, ea: float) -> bool:
    """角度是否在弧的扫掠范围内（支持顺/逆时针）。"""
    span = ea - sa
    if span >= 0:
        while angle < sa - 1e-6:
            angle += 2 * math.pi
        return sa - 1e-6 <= angle <= ea + 1e-6
    else:
        while angle > sa + 1e-6:
            angle -= 2 * math.pi
        return ea - 1e-6 <= angle <= sa + 1e-6


# 捕捉类型优先级（数值越小优先级越高）；同距离时优先选高优先级
_SNAP_PRIORITY = {
    SnapType.ENDPOINT: 0, SnapType.INTERSECTION: 1, SnapType.CENTER: 2,
    SnapType.QUARTER: 3, SnapType.MIDPOINT: 4, SnapType.TANGENT: 5,
    SnapType.PERPENDICULAR: 6, SnapType.VERTEX: 7, SnapType.POINT: 8,
    SnapType.NEAREST: 9,
}


class SnapManager:
    _instance = None

    def __init__(self) -> None:
        self._enabledTypes: Set[SnapType] = {
            SnapType.ENDPOINT, SnapType.NEAREST, SnapType.POINT, SnapType.MIDPOINT,
            SnapType.CENTER, SnapType.INTERSECTION, SnapType.PERPENDICULAR,
            SnapType.TANGENT, SnapType.QUARTER, SnapType.VERTEX,
        }
        # 捕捉半径（屏幕像素）：12px 太大——画线时刚点下的点/短线段的中点、
        # 垂足都会在光标还很远时就把光标拽走，手感"发黏"；6px 与主流 CAD 接近
        self._snapRadius: float = 6.0
        # 解析对象（圆/弧/椭圆/直线）→ 采样折线缓存：键为几何签名（内容变化即换键），
        # 超上限按 LRU 淘汰最早项；非解析对象不走此缓存（显示点列由对象自身缓存）
        self._sampleCache: "OrderedDict" = OrderedDict()
        # 当前捕捉平面轴（snap() 入口设置；主线程串行调用，无重入）
        self._ax: Tuple[int, int] = _AXES_XY

    @classmethod
    def instance(cls) -> "SnapManager":
        if cls._instance is None:
            cls._instance = SnapManager()
        return cls._instance

    def enabledTypes(self) -> Set[SnapType]:
        return self._enabledTypes

    def setEnabledTypes(self, types: Set[SnapType]) -> None:
        self._enabledTypes = set(types)

    def toggleType(self, t: SnapType) -> None:
        if t in self._enabledTypes:
            self._enabledTypes.discard(t)
        else:
            self._enabledTypes.add(t)

    def setTypeEnabled(self, t: SnapType, enabled: bool) -> None:
        if enabled:
            self._enabledTypes.add(t)
        else:
            self._enabledTypes.discard(t)

    def snapRadius(self) -> float:
        return self._snapRadius

    def setSnapRadius(self, r: float) -> None:
        self._snapRadius = r

    # ---------- 捕捉平面 (u,v) 访问 ----------
    def _u(self, p: Point3D) -> float:
        return _comp(p, self._ax[0])

    def _v(self, p: Point3D) -> float:
        return _comp(p, self._ax[1])

    def _mk(self, u: float, v: float, w: float) -> Point3D:
        """由 (u,v) + 平面外分量组装世界点。"""
        c = [0.0, 0.0, 0.0]
        c[self._ax[0]], c[self._ax[1]] = u, v
        c[_wAxis(self._ax)] = w
        return Point3D(c[0], c[1], c[2])

    def _dist2d(self, p: Point3D, q: Point3D) -> float:
        """两点在捕捉平面 (u,v) 上的距离。"""
        return math.hypot(self._u(p) - self._u(q), self._v(p) - self._v(q))

    def snap(self, worldPos: Point3D, objects: List, viewRadius: float,
             refPoint: Optional[Point3D] = None,
             axes: Tuple[int, int] = _AXES_XY,
             eye: Optional[tuple] = None) -> SnapResult:
        """求吸附。eye 为相机位置（3D 视口传入）：多个候选在捕捉平面上
        距离/优先级完全并列时（如顶视下圆柱上下两圆投影重合），取离
        相机更近（看得见的）那个；2D 画布不传，行为不变。

        refPoint 是命令**刚落下的输入点**：与该点重合的候选会被剔除——
        画直线/曲线时刚点下去的点立刻变成自己的端点/顶点候选，光标在
        捕捉半径内稍微一动就被拽回该点，无法正常拖动；剔除的只是
        "刚落的那一个点"，曲线的起点（闭合用）等其它点仍可正常吸附。"""
        self._ax = axes if axes in ((0, 1), (0, 2), (1, 2)) else _AXES_XY
        candidates: List[SnapResult] = []
        uw, vw = _comp(worldPos, self._ax[0]), _comp(worldPos, self._ax[1])
        # 鼠标粗筛：包围盒离鼠标超过捕捉半径的对象不可能产生可视捕捉点，
        # 整体跳过（不跑任何捕捉算法、不构造采样折线）。唯一例外是
        # 垂足·情况2——它把鼠标投到"过参考点且垂直于目标线"的无限直线上，
        # 吸附点可远离该对象，故仅对有参考点的直线豁免粗筛。
        prescreen = viewRadius > 0
        perp_lines = refPoint is not None and \
            SnapType.PERPENDICULAR in self._enabledTypes
        # 单对象捕捉
        for obj in objects:
            if not obj.isVisible() or obj.isLocked():
                continue
            featGetter = getattr(obj, "snapFeatures", None)
            feats = None
            if callable(featGetter):
                feats = featGetter()
            bb = self._objectBBox(obj, feats)
            if prescreen and bb is not None and not (
                    perp_lines and obj.type() == ObjectType.LINE) and (
                    uw < bb[0] - viewRadius or uw > bb[2] + viewRadius
                    or vw < bb[1] - viewRadius or vw > bb[3] + viewRadius):
                continue
            if callable(featGetter):
                # B-Rep 实体：按解析边特征吸附（圆心/端点/中点/象限/
                # 最近点/顶点），不走常规类型分发
                if feats:
                    for r in self._snapSolid(obj, feats, worldPos,
                                             viewRadius):
                        if r.isValid and r.distance <= viewRadius:
                            candidates.append(r)
                continue
            supported = set(obj.getSupportedSnapTypes())
            for st in (self._enabledTypes & supported):
                for r in self._snapObject(obj, st, worldPos, viewRadius, refPoint):
                    if r.isValid and r.distance <= viewRadius:
                        candidates.append(r)
        # 交点捕捉：需两两对象求交
        if SnapType.INTERSECTION in self._enabledTypes:
            candidates.extend(self._snapIntersections(worldPos, objects, viewRadius))
        if refPoint is not None and candidates:
            # 剔除与"刚落下的输入点"重合的候选（自己的端点/顶点/最近点）：
            # 否则画线/画曲线时刚点的点会一直把光标吸住，微动一下就被拽回。
            # 容差取捕捉半径的万分之一（≈0.001px 级别），只杀真正重合的点，
            # 不影响线段上的最近点吸附。
            eps = max(1e-9, abs(float(viewRadius)) * 1e-4)
            candidates = [r for r in candidates
                          if self._dist2d(r.position, refPoint) > eps]
        if not candidates:
            return SnapResult.invalid()
        # 优先级：最近点级别最低，只要存在非最近点命中就只用非最近点
        precise = [r for r in candidates if r.type != SnapType.NEAREST]
        pool = precise if precise else candidates
        best = min(pool, key=lambda r: (r.distance,
                                        _SNAP_PRIORITY.get(r.type, 99)))
        if eye is not None:
            # 距离+优先级完全并列（纯投影歧义）：取离相机更近的候选
            tied = [r for r in pool
                    if abs(r.distance - best.distance) < 1e-9
                    and _SNAP_PRIORITY.get(r.type, 99)
                    == _SNAP_PRIORITY.get(best.type, 99)]
            if len(tied) > 1:
                best = min(tied, key=lambda r: (
                    (eye[0] - r.position.x) ** 2
                    + (eye[1] - r.position.y) ** 2
                    + (eye[2] - r.position.z) ** 2))
        return best

    def _makeResult(self, obj, st: SnapType, p: Point3D, worldPos: Point3D) -> SnapResult:
        d = self._dist2d(p, worldPos)
        return SnapResult(position=p, type=st, objectId=obj.id(),
                          distance=d, isValid=True)

    def _snapObject(self, obj, st: SnapType, worldPos: Point3D,
                    viewRadius: float, refPoint: Optional[Point3D] = None) -> List[SnapResult]:
        res: List[SnapResult] = []
        if st == SnapType.ENDPOINT:
            res.extend(self._snapEndpoint(obj, worldPos))
        elif st == SnapType.MIDPOINT:
            res.extend(self._snapMidpoint(obj, worldPos))
        elif st == SnapType.CENTER:
            res.extend(self._snapCenter(obj, worldPos, viewRadius))
        elif st == SnapType.POINT:
            res.extend(self._snapNode(obj, worldPos))
        elif st == SnapType.QUARTER:
            res.extend(self._snapQuadrant(obj, worldPos))
        elif st == SnapType.PERPENDICULAR:
            res.extend(self._snapPerpendicular(obj, worldPos, refPoint))
        elif st == SnapType.TANGENT:
            res.extend(self._snapTangent(obj, worldPos))
        elif st == SnapType.NEAREST:
            res.extend(self._snapNearest(obj, worldPos))
        elif st == SnapType.VERTEX:
            res.extend(self._snapVertex(obj, worldPos))
        return res

    # ---------- 各类型实现 ----------
    def _snapEndpoint(self, obj, wp: Point3D) -> List[SnapResult]:
        out = []
        t = obj.type()
        if t == ObjectType.ARC:
            c = obj.center(); r = obj.radius()
            for a in (obj.startAngle(), obj.endAngle()):
                p = Point3D(c.x + r * math.cos(a), c.y + r * math.sin(a), c.z)
                out.append(self._makeResult(obj, SnapType.ENDPOINT, p, wp))
            return out
        n = obj.controlPointCount()
        if n >= 1:
            out.append(self._makeResult(obj, SnapType.ENDPOINT,
                                        obj.getControlPoint(0), wp))
        if n >= 2:
            out.append(self._makeResult(obj, SnapType.ENDPOINT,
                                        obj.getControlPoint(n - 1), wp))
        return out

    def _snapMidpoint(self, obj, wp: Point3D) -> List[SnapResult]:
        out = []
        t = obj.type()
        if t == ObjectType.ARC:
            c = obj.center(); r = obj.radius()
            ma = (obj.startAngle() + obj.endAngle()) * 0.5
            p = Point3D(c.x + r * math.cos(ma), c.y + r * math.sin(ma), c.z)
            out.append(self._makeResult(obj, SnapType.MIDPOINT, p, wp))
            return out
        if t == ObjectType.LINE:
            # 每段直线各自的中点
            pts = obj.points()
            for i in range(len(pts) - 1):
                a, b = pts[i], pts[i + 1]
                p = self._mk((self._u(a) + self._u(b)) * 0.5,
                             (self._v(a) + self._v(b)) * 0.5,
                             _comp(a, _wAxis(self._ax)) * 0.5
                             + _comp(b, _wAxis(self._ax)) * 0.5)
                out.append(self._makeResult(obj, SnapType.MIDPOINT, p, wp))
        elif t == ObjectType.POLYCURVE:
            # 每段曲线沿其采样长度的中点
            for seg in obj.segments():
                pts = [Point3D(p[0], p[1], p[2]) for p in seg.get("points", [])]
                p = self._pathMidpoint(pts)
                if p is not None:
                    out.append(self._makeResult(obj, SnapType.MIDPOINT, p, wp))
        elif t == ObjectType.CURVE:
            pts = obj.getSampledPoints() if hasattr(obj, "getSampledPoints") else []
            p = self._pathMidpoint(pts)
            if p is not None:
                out.append(self._makeResult(obj, SnapType.MIDPOINT, p, wp))
        return out

    def _pathMidpoint(self, pts) -> Optional[Point3D]:
        """沿折线累加长度（按捕捉平面 (u,v) 度量），返回总长度一半处的点。"""
        if len(pts) < 2:
            return None
        segs = []
        total = 0.0
        for i in range(len(pts) - 1):
            d = self._dist2d(pts[i], pts[i + 1])
            segs.append(d)
            total += d
        if total < 1e-9:
            return None
        half = total * 0.5
        acc = 0.0
        w = _wAxis(self._ax)
        for i, d in enumerate(segs):
            if acc + d >= half:
                t = (half - acc) / d if d > 1e-9 else 0.0
                a, b = pts[i], pts[i + 1]
                return self._mk(self._u(a) + (self._u(b) - self._u(a)) * t,
                                self._v(a) + (self._v(b) - self._v(a)) * t,
                                _comp(a, w))
            acc += d
        return pts[-1]

    # ---------- 椭圆解析（含自转角，与 _samplePointsOf 同一变换） ----------
    def _ellipseFrame(self, obj):
        """椭圆局部系 (center, a, b, cos, sin)：世界点 = c + R·(a·cosθ, b·sinθ)。"""
        c = obj.center()
        a, b = obj.majorAxis(), obj.minorAxis()
        rot = float(obj.rotation())
        return c, a, b, math.cos(rot), math.sin(rot)

    def _ellipsePointAt(self, obj, ang) -> Point3D:
        c, a, b, cr, sr = self._ellipseFrame(obj)
        lx, ly = a * math.cos(ang), b * math.sin(ang)
        return Point3D(c.x + lx * cr - ly * sr, c.y + lx * sr + ly * cr, c.z)

    def _ellipseLocal(self, obj, wp: Point3D):
        """世界点 → 椭圆局部坐标 (lx, ly)（逆自转角）。"""
        c, a, b, cr, sr = self._ellipseFrame(obj)
        dx, dy = wp.x - c.x, wp.y - c.y
        return (dx * cr + dy * sr, -dx * sr + dy * cr), c, a, b

    def _snapCenter(self, obj, wp: Point3D, viewRadius: float) -> List[SnapResult]:
        out = []
        t = obj.type()
        if t in (ObjectType.CIRCLE, ObjectType.ARC, ObjectType.ELLIPSE):
            c = obj.center()
            if self._ax != _AXES_XY:
                # 侧视：解析圆不在捕捉平面内，退化为"靠近圆心投影"即捕捉
                d = self._dist2d(c, wp)
                if d <= viewRadius:
                    out.append(SnapResult(position=c, type=SnapType.CENTER,
                                          objectId=obj.id(), distance=d,
                                          isValid=True))
                return out
            d = math.hypot(wp.x - c.x, wp.y - c.y)
            if t == ObjectType.ELLIPSE:
                # 椭圆：鼠标在椭圆边界附近且偏内 → 圆心（边界/内外判定
                # 都在椭圆局部系做，正确处理自转角）
                (lx, ly), c, a, b = self._ellipseLocal(obj, wp)
                if a < 1e-9 or b < 1e-9:
                    return out
                ang = math.atan2(ly * a, lx * b)
                bp = self._ellipsePointAt(obj, ang)
                edge_d = math.hypot(wp.x - bp.x, wp.y - bp.y)
                # 偏内：局部归一化坐标在椭圆内
                inside = (lx / a) ** 2 + (ly / b) ** 2 < 1.0
                if edge_d <= viewRadius and inside:
                    out.append(SnapResult(position=c, type=SnapType.CENTER,
                                          objectId=obj.id(), distance=edge_d, isValid=True))
            else:
                # 圆/弧：鼠标在圆周附近且偏内 → 圆心
                r = obj.radius()
                edge_d = abs(d - r)
                if edge_d <= viewRadius and d < r:
                    out.append(SnapResult(position=c, type=SnapType.CENTER,
                                          objectId=obj.id(), distance=edge_d, isValid=True))
        return out

    def _snapNode(self, obj, wp: Point3D) -> List[SnapResult]:
        out = []
        if obj.type() == ObjectType.POINT:
            out.append(self._makeResult(obj, SnapType.POINT, obj.position(), wp))
        return out

    def _snapQuadrant(self, obj, wp: Point3D) -> List[SnapResult]:
        out = []
        t = obj.type()
        if t == ObjectType.CIRCLE:
            c = obj.center(); r = obj.radius()
            for ang in (0, math.pi / 2, math.pi, 3 * math.pi / 2):
                p = Point3D(c.x + r * math.cos(ang), c.y + r * math.sin(ang), c.z)
                out.append(self._makeResult(obj, SnapType.QUARTER, p, wp))
        elif t == ObjectType.ARC:
            c = obj.center(); r = obj.radius()
            for ang in (0, math.pi / 2, math.pi, 3 * math.pi / 2):
                if _inArcRange(ang, obj.startAngle(), obj.endAngle()):
                    p = Point3D(c.x + r * math.cos(ang), c.y + r * math.sin(ang), c.z)
                    out.append(self._makeResult(obj, SnapType.QUARTER, p, wp))
        elif t == ObjectType.ELLIPSE:
            for ang in (0, math.pi / 2, math.pi, 3 * math.pi / 2):
                out.append(self._makeResult(obj, SnapType.QUARTER,
                                             self._ellipsePointAt(obj, ang), wp))
        return out

    def _snapPerpendicular(self, obj, wp: Point3D,
                            refPoint: Optional[Point3D]) -> List[SnapResult]:
        """垂足：从参考点（当前绘制起点）向目标线作垂线。
        情况1：参考点在线外，垂足在目标线上，鼠标靠近垂足时捕捉。
        情况2：参考点在目标线上，鼠标移动使预览线与目标线垂直时，
        将鼠标吸附到精确垂线方向，标记画在参考点（垂足）。
        全部按捕捉平面 (u,v) 度量。"""
        out = []
        if refPoint is None or obj.type() != ObjectType.LINE:
            return out
        w = _wAxis(self._ax)
        pts = obj.points()
        for i in range(len(pts) - 1):
            a, b = pts[i], pts[i + 1]
            foot = _nearestOnSegment(refPoint, a, b, self._ax)
            dx = self._u(b) - self._u(a)
            dy = self._v(b) - self._v(a)
            L2 = dx * dx + dy * dy
            if L2 < 1e-12:
                continue
            onLine = (self._dist2d(foot, refPoint) < 1e-6)
            if onLine:
                # 情况2：参考点在线上，预览线与目标线垂直时吸附
                rx = self._u(wp) - self._u(refPoint)
                ry = self._v(wp) - self._v(refPoint)
                previewLen = math.hypot(rx, ry)
                if previewLen < 1e-9:
                    continue
                # cos(angle) between preview and line direction
                cosA = (rx * dx + ry * dy) / (previewLen * math.sqrt(L2))
                # 角度容差约 5°（sin5°≈0.087）
                if abs(cosA) < 0.087:
                    # 垂直方向单位向量
                    px, py = -dy, dx
                    pL = math.sqrt(L2)
                    px, py = px / pL, py / pL
                    # 将鼠标投影到过 refPoint 的精确垂线上
                    proj = rx * px + ry * py
                    snapped = self._mk(self._u(refPoint) + px * proj,
                                       self._v(refPoint) + py * proj,
                                       _comp(refPoint, w))
                    d = self._dist2d(snapped, wp)
                    out.append(SnapResult(position=snapped,
                                          type=SnapType.PERPENDICULAR,
                                          objectId=obj.id(), distance=d,
                                          isValid=True, markerPos=refPoint))
            else:
                # 情况1：垂足在目标线上，鼠标靠近垂足时捕捉
                out.append(self._makeResult(obj, SnapType.PERPENDICULAR, foot, wp))
        return out

    def _snapTangent(self, obj, wp: Point3D) -> List[SnapResult]:
        out = []
        if self._ax != _AXES_XY:
            # 切点构造依赖"圆平面 = 捕捉平面"，侧视无意义
            return out
        t = obj.type()
        if t in (ObjectType.CIRCLE, ObjectType.ARC):
            c = obj.center(); r = obj.radius()
            d = math.hypot(wp.x - c.x, wp.y - c.y)
            if d > r:
                ang = math.atan2(wp.y - c.y, wp.x - c.x)
                off = math.acos(r / d)
                for a in (ang + off, ang - off):
                    if t == ObjectType.ARC and not _inArcRange(a, obj.startAngle(), obj.endAngle()):
                        continue
                    p = Point3D(c.x + r * math.cos(a), c.y + r * math.sin(a), c.z)
                    out.append(self._makeResult(obj, SnapType.TANGENT, p, wp))
        return out

    def _snapNearest(self, obj, wp: Point3D) -> List[SnapResult]:
        out = []
        t = obj.type()
        if t == ObjectType.LINE:
            pts = obj.points()
            best = None
            for i in range(len(pts) - 1):
                p = _nearestOnSegment(wp, pts[i], pts[i + 1], self._ax)
                d = self._dist2d(p, wp)
                if best is None or d < best[1]:
                    best = (p, d)
            if best:
                out.append(self._makeResult(obj, SnapType.NEAREST, best[0], wp))
        elif t in (ObjectType.CURVE, ObjectType.POLYCURVE):
            pts = obj.getSampledPoints() if hasattr(obj, "getSampledPoints") else []
            best = None
            for i in range(len(pts) - 1):
                p = _nearestOnSegment(wp, pts[i], pts[i + 1], self._ax)
                d = self._dist2d(p, wp)
                if best is None or d < best[1]:
                    best = (p, d)
            if best:
                out.append(self._makeResult(obj, SnapType.NEAREST, best[0], wp))
        elif t in (ObjectType.CIRCLE, ObjectType.ARC, ObjectType.ELLIPSE):
            if self._ax != _AXES_XY:
                # 侧视：解析公式依赖"圆平面 = 捕捉平面"，改走采样折线最近点
                pts = self._cachedSamples(obj)
                best = None
                for i in range(len(pts) - 1):
                    p = _nearestOnSegment(wp, pts[i], pts[i + 1], self._ax)
                    d = self._dist2d(p, wp)
                    if best is None or d < best[1]:
                        best = (p, d)
                if best:
                    out.append(self._makeResult(obj, SnapType.NEAREST, best[0], wp))
                return out
            if t == ObjectType.ELLIPSE:
                # 最近点：局部系求参数角（逆自转角后再 atan2），再映射回世界
                (lx, ly), c, a, b = self._ellipseLocal(obj, wp)
                ang = math.atan2(ly * a, lx * b)
                p = self._ellipsePointAt(obj, ang)
            else:
                c = obj.center(); r = obj.radius()
                ang = math.atan2(wp.y - c.y, wp.x - c.x)
                if t == ObjectType.ARC and not _inArcRange(ang, obj.startAngle(), obj.endAngle()):
                    ang = obj.startAngle()
                p = Point3D(c.x + r * math.cos(ang), c.y + r * math.sin(ang), c.z)
            out.append(self._makeResult(obj, SnapType.NEAREST, p, wp))
        return out

    def _snapVertex(self, obj, wp: Point3D) -> List[SnapResult]:
        out = []
        if obj.type() == ObjectType.LINE:
            for i in range(obj.controlPointCount()):
                out.append(self._makeResult(obj, SnapType.VERTEX,
                                            obj.getControlPoint(i), wp))
        return out

    # ---------- B-Rep 实体（解析边特征） ----------

    def _snapSolid(self, obj, feats: List, wp: Point3D,
                   viewRadius: float) -> List[SnapResult]:
        """实体子元素吸附：圆边 → 圆心（近圆周且偏内，与 2D 圆一致）/
        象限/端点/中点/最近点；直线边 → 端点/中点/最近点；顶点。
        位置带真实 3D 坐标（画在圆柱顶面可吸到顶圆真实圆心），
        距离按捕捉平面 (u,v) 度量。"""
        out: List[SnapResult] = []
        en = self._enabledTypes
        w = _wAxis(self._ax)
        for f in feats:
            k = f["kind"]
            if k == "circle":
                cx, cy, cz = f["center"]
                c = Point3D(cx, cy, cz)
                plane_par = abs(f["axis"][w]) > 0.999
                d = self._dist2d(c, wp)
                if SnapType.CENTER in en:
                    if plane_par:
                        # 圆平面 ∥ 捕捉平面：近圆周且偏内 → 圆心（同 2D 圆）
                        edge_d = abs(d - f["radius"])
                        if edge_d <= viewRadius and d < f["radius"]:
                            out.append(SnapResult(
                                position=c, type=SnapType.CENTER,
                                objectId=obj.id(), distance=edge_d,
                                isValid=True))
                    elif d <= viewRadius:
                        # 侧视/斜置圆：解析公式失效，靠近投影圆心即捕捉
                        out.append(SnapResult(
                            position=c, type=SnapType.CENTER,
                            objectId=obj.id(), distance=d, isValid=True))
                if SnapType.QUARTER in en:
                    for q in f["quarters"]:
                        out.append(self._makeResult(obj, SnapType.QUARTER,
                                                    q, wp))
                if not f["full"]:
                    if SnapType.ENDPOINT in en:
                        for p in f["endPts"]:
                            out.append(self._makeResult(obj, SnapType.ENDPOINT,
                                                        p, wp))
                    if SnapType.MIDPOINT in en and f["midPt"] is not None:
                        out.append(self._makeResult(obj, SnapType.MIDPOINT,
                                                    f["midPt"], wp))
                if SnapType.NEAREST in en and plane_par:
                    p = self._circleNearest(f, wp)
                    if p is not None:
                        out.append(self._makeResult(obj, SnapType.NEAREST,
                                                    p, wp))
            elif k == "line":
                a, b = f["a"], f["b"]
                if SnapType.ENDPOINT in en:
                    out.append(self._makeResult(obj, SnapType.ENDPOINT, a, wp))
                    out.append(self._makeResult(obj, SnapType.ENDPOINT, b, wp))
                if SnapType.MIDPOINT in en:
                    out.append(self._makeResult(obj, SnapType.MIDPOINT,
                                                Point3D((a.x + b.x) * 0.5,
                                                        (a.y + b.y) * 0.5,
                                                        (a.z + b.z) * 0.5),
                                                wp))
                if SnapType.NEAREST in en:
                    out.append(self._makeResult(
                        obj, SnapType.NEAREST,
                        _nearestOnSegment(wp, a, b, self._ax), wp))
            elif k == "vertex":
                if SnapType.VERTEX in en:
                    out.append(self._makeResult(obj, SnapType.VERTEX,
                                                f["p"], wp))
        return out

    def _circleNearest(self, f: dict, wp: Point3D) -> Optional[Point3D]:
        """鼠标在圆平面上的最近点（圆弧钳制到参数范围内）。"""
        ex, ey = f["ex"], f["ey"]
        cx, cy, cz = f["center"]
        vx, vy, vz = wp.x - cx, wp.y - cy, wp.z - cz
        la = vx * ex[0] + vy * ex[1] + vz * ex[2]
        lb = vx * ey[0] + vy * ey[1] + vz * ey[2]
        ang = math.atan2(lb, la)
        if not f["full"]:
            def _ad(x, y):
                dd = abs(x - y) % (2 * math.pi)
                return min(dd, 2 * math.pi - dd)
            ang = f["a0"] if _ad(ang, f["a0"]) <= _ad(ang, f["a1"]) \
                else f["a1"]
        r = f["radius"]
        ca, sa = math.cos(ang), math.sin(ang)
        return Point3D(cx + r * (ca * ex[0] + sa * ey[0]),
                       cy + r * (ca * ex[1] + sa * ey[1]),
                       cz + r * (ca * ex[2] + sa * ey[2]))

    # ---------- 交点（两两对象） ----------
    def _snapIntersections(self, wp: Point3D, objects: List,
                           viewRadius: float) -> List[SnapResult]:
        out: List[SnapResult] = []
        # 先用各对象的 (u,v) 包围盒粗筛再配对：
        # 1) 鼠标预筛——交点必然落在两个对象上，包围盒离鼠标超出捕捉半径
        #    的对象不可能贡献可视交点，直接剔除（大场景下把 O(N²) 配对
        #    压到只配视口附近的少数对象）；
        # 2) 包围盒用 _objectBBox（解析对象 O(1)）而非构造 1025 点采样后
        #    全表扫描——后者在圆较多时是鼠标移动的主要卡顿源，
        #    采样折线改为只在配对真正发生时（_intersectPair 内）按需构造。
        uw, vw = self._u(wp), self._v(wp)
        infos = []
        for o in objects:
            if not o.isVisible() or o.isLocked():
                continue
            featGetter = getattr(o, "snapFeatures", None)
            if callable(featGetter):
                continue   # B-Rep 实体走解析特征吸附，不参与折线求交
            bb = self._objectBBox(o)
            if bb is None:
                continue
            if viewRadius > 0 and (
                    bb[0] - uw > viewRadius or uw - bb[2] > viewRadius
                    or bb[1] - vw > viewRadius or vw - bb[3] > viewRadius):
                continue
            # 几何级邻域剪枝（比包围盒紧得多）：长直线的包围盒往往虚大，
            # 对象本身若没有任何几何落在半径内，其交点必然也不在半径内
            if viewRadius > 0 and not self._nearMouse(o, wp, viewRadius):
                continue
            infos.append((o, bb))
        n = len(infos)
        for i in range(n):
            oi, ba = infos[i]
            for j in range(i + 1, n):
                oj, bb2 = infos[j]
                if not _bboxOverlap(ba, bb2):
                    continue
                for p in self._intersectPair(oi, oj, wp, viewRadius):
                    d = self._dist2d(p, wp)
                    if d <= viewRadius:
                        out.append(SnapResult(position=p, type=SnapType.INTERSECTION,
                                              objectId=oi.id(), distance=d, isValid=True))
        return out

    def _nearMouse(self, obj, wp: Point3D, viewRadius: float) -> bool:
        """对象几何是否落在鼠标捕捉半径内（交点级剪枝）。

        交点必落在对象几何上：若整个对象离鼠标都超过捕捉半径，则它不可能
        贡献可视交点，可整对象剔除。直线/曲线按弦精确判定；圆/弧/椭圆在
        捕捉平面为 XY 时用解析距离（弧按其完整圆、椭圆按其中心距区间取
        保守界限）；其余平面无法解析，保守返回 True（宁可多算不漏）。"""
        t = obj.type()
        if t == ObjectType.POINT:
            return False                     # 点对象不参与求交
        if t in (ObjectType.LINE, ObjectType.CURVE, ObjectType.POLYCURVE):
            return bool(self._chordsNear(self._cachedSamples(obj),
                                         wp, viewRadius))
        if self._ax == _AXES_XY:
            try:
                c = obj.center()
                d = math.hypot(wp.x - c.x, wp.y - c.y)
                if t == ObjectType.CIRCLE or t == ObjectType.ARC:
                    return abs(d - float(obj.radius())) <= viewRadius
                if t == ObjectType.ELLIPSE:
                    a = float(obj.majorAxis())
                    b = float(obj.minorAxis())
                    return b - viewRadius <= d <= a + viewRadius
            except Exception:
                return True
        return True

    def _intersectPair(self, a, b, wp: Optional[Point3D] = None,
                       viewRadius: float = 0.0) -> List[Point3D]:
        # 侧视：解析圆类不在捕捉平面内，全部按采样折线求交
        if self._ax != _AXES_XY:
            sa = self._cachedSamples(a)
            sb = self._cachedSamples(b)
            return self._polylinePairIntersect(sa, sb, wp, viewRadius)
        ta, tb = a.type(), b.type()
        pts: List[Point3D] = []
        if ta == ObjectType.LINE and tb == ObjectType.LINE:
            # 闭合折线（矩形/多边形）点列补齐收尾副本后再求交
            pa = self._cachedSamples(a)
            pb = self._cachedSamples(b)
            pts = self._polylinePairIntersect(pa, pb, wp, viewRadius)
        elif ta == ObjectType.LINE and tb in (ObjectType.CIRCLE, ObjectType.ARC):
            pts.extend(self._lineCircle(a, b))
        elif tb == ObjectType.LINE and ta in (ObjectType.CIRCLE, ObjectType.ARC):
            pts.extend(self._lineCircle(b, a))
        elif ta in (ObjectType.CIRCLE, ObjectType.ARC) and tb in (ObjectType.CIRCLE, ObjectType.ARC):
            pts.extend(self._circleCircle(a, b))
        else:
            # 其余组合（圆×样条、样条×样条、椭圆×任意等）：
            # 按双方显示采样折线求交。交点捕捉只需给出显示交点位置，
            # 解析密采（圆 1024 点）与显示曲线一致，精度远小于捕捉半径。
            sa = self._cachedSamples(a)
            sb = self._cachedSamples(b)
            pts = self._polylinePairIntersect(sa, sb, wp, viewRadius)
        return pts

    def _polylinePairIntersect(self, pa: List[Point3D], pb: List[Point3D],
                               wp: Optional[Point3D], viewRadius: float) -> List[Point3D]:
        """两组采样折线按捕捉平面求交（包围盒快排 + 鼠标附近弦过滤）。"""
        pts: List[Point3D] = []
        if len(pa) >= 2 and len(pb) >= 2:
            # 包围盒快速排除（无交则整对跳过，避免 O(n·m) 全量弦求交）
            if not _bboxOverlap(_bboxOf(pa, self._ax), _bboxOf(pb, self._ax)):
                return []
            if wp is not None and viewRadius > 0 and \
                    (len(pa) > 32 or len(pb) > 32):
                # 多段折线：只保留经过鼠标附近（捕捉半径内）的弦，控制组合数
                # （对象级的粗筛已由 _nearMouse 完成，此处仅压缩弦对数量）
                nearA = self._chordsNear(pa, wp, viewRadius)
                nearB = self._chordsNear(pb, wp, viewRadius)
            else:
                nearA = list(zip(pa, pa[1:]))
                nearB = list(zip(pb, pb[1:]))
            for a1, a2 in nearA:
                for b1, b2 in nearB:
                    p = _segSegIntersect(a1, a2, b1, b2, self._ax)
                    if p:
                        pts.append(p)
        return pts

    def _geomKey(self, obj):
        """解析对象的几何签名：内容变化 → 键变化 → 采样缓存自然失效。
        非解析对象（曲线/多段线等）返回 None，不走采样缓存。"""
        t = obj.type()
        try:
            if t == ObjectType.CIRCLE:
                c, r = obj.center(), obj.radius()
                return (t, round(c.x, 9), round(c.y, 9), round(c.z, 9), round(r, 9))
            if t == ObjectType.ARC:
                c, r = obj.center(), obj.radius()
                return (t, round(c.x, 9), round(c.y, 9), round(c.z, 9), round(r, 9),
                        round(obj.startAngle(), 9), round(obj.endAngle(), 9))
            if t == ObjectType.ELLIPSE:
                c = obj.center()
                return (t, round(c.x, 9), round(c.y, 9), round(c.z, 9),
                        round(obj.majorAxis(), 9), round(obj.minorAxis(), 9),
                        round(obj.rotation(), 9))
            if t == ObjectType.LINE:
                pts = obj.points()
                return (t, tuple((round(p.x, 9), round(p.y, 9), round(p.z, 9))
                                 for p in pts),
                        bool(getattr(obj, "isClosed", lambda: False)()))
        except Exception:
            return None
        return None

    def _cachedSamples(self, obj) -> List[Point3D]:
        """对象显示采样折线：解析对象按几何签名缓存（移动/改参数即换键），
        其余类型直接取（Curve/Nurbs 的 getSampledPoints 有对象级缓存）。"""
        key = self._geomKey(obj)
        if key is None:
            return self._samplePointsOf(obj)
        hit = self._sampleCache.get(key)
        if hit is not None:
            self._sampleCache.move_to_end(key)
            return hit
        hit = self._samplePointsOf(obj)
        self._sampleCache[key] = hit
        while len(self._sampleCache) > _SAMPLE_CACHE_MAX:
            self._sampleCache.popitem(last=False)
        return hit

    def _objectBBox(self, obj, feats: Optional[List] = None):
        """对象在捕捉平面 (u,v) 上的包围盒 (u0,v0,u1,v1)，无法判定时返回 None。

        解析对象（圆/弧/椭圆）用 O(1) 保守盒（圆心±半径，宁大勿漏），
        直线取端点，B-Rep 实体取解析特征坐标；其余类型回退到采样折线扫描。
        这是鼠标粗筛的基础：捕捉点必落在对象几何上，故包围盒离鼠标超过
        捕捉半径的对象不可能产生可视捕捉点，可整体跳过（不必构造其采样
        折线、不必跑各类捕捉算法）——大场景鼠标移动卡顿的主因即在此。"""
        t = obj.type()
        ui, vi = self._ax
        try:
            if t in (ObjectType.CIRCLE, ObjectType.ARC):
                c = obj.center()
                r = float(obj.radius())
                u, v = _comp(c, ui), _comp(c, vi)
                return (u - r, v - r, u + r, v + r)
            if t == ObjectType.ELLIPSE:
                c = obj.center()
                r = max(float(obj.majorAxis()), float(obj.minorAxis()))
                u, v = _comp(c, ui), _comp(c, vi)
                return (u - r, v - r, u + r, v + r)
            if t == ObjectType.LINE:
                pts = obj.points()
                if not pts:
                    return None
                us = [_comp(p, ui) for p in pts]
                vs = [_comp(p, vi) for p in pts]
                return (min(us), min(vs), max(us), max(vs))
            if t == ObjectType.POINT:
                p = obj.position()
                u, v = _comp(p, ui), _comp(p, vi)
                return (u, v, u, v)
        except Exception:
            return None
        if feats:
            us: List[float] = []
            vs: List[float] = []
            for f in feats:
                k = f["kind"]
                if k == "circle":
                    c = Point3D(*f["center"])
                    r = float(f["radius"])
                    cu, cv = _comp(c, ui), _comp(c, vi)
                    us.extend((cu - r, cu + r))
                    vs.extend((cv - r, cv + r))
                elif k == "line":
                    for p in (f["a"], f["b"]):
                        us.append(_comp(p, ui))
                        vs.append(_comp(p, vi))
                elif k == "vertex":
                    p = f["p"]
                    us.append(_comp(p, ui))
                    vs.append(_comp(p, vi))
            if not us:
                return None
            return (min(us), min(vs), max(us), max(vs))
        return _bboxOf(self._cachedSamples(obj), self._ax)

    def _samplePointsOf(self, obj) -> List[Point3D]:
        """对象的显示采样折线（交点/最近点等通用求交的数据源）。

        圆/弧/椭圆按解析公式密采（显示即解析曲线，1024 点矢高可忽略），
        其余类型取显示点列本身——保证交点捕捉位置与显示曲线一致。"""
        t = obj.type()
        n = 1024
        if t == ObjectType.CIRCLE:
            c, r = obj.center(), obj.radius()
            # 三角函数整体向量化（.tolist() 回退为 Python float，语义与原实现一致）
            th = np.linspace(0.0, 2.0 * math.pi, n + 1)
            xs = (c.x + r * np.cos(th)).tolist()
            ys = (c.y + r * np.sin(th)).tolist()
            z = c.z
            return [Point3D(xs[i], ys[i], z) for i in range(n + 1)]
        if t == ObjectType.ARC:
            c, r = obj.center(), obj.radius()
            sa, ea = obj.startAngle(), obj.endAngle()
            span = ea - sa
            if abs(span) < 1e-9:
                span = 2 * math.pi
            th = np.linspace(sa, sa + span, n + 1)
            xs = (c.x + r * np.cos(th)).tolist()
            ys = (c.y + r * np.sin(th)).tolist()
            z = c.z
            return [Point3D(xs[i], ys[i], z) for i in range(n + 1)]
        if t == ObjectType.ELLIPSE:
            c = obj.center()
            a, b = obj.majorAxis(), obj.minorAxis()
            rot = float(obj.rotation())
            cr, sr = math.cos(rot), math.sin(rot)
            th = np.linspace(0.0, 2.0 * math.pi, n + 1)
            lx = (a * np.cos(th)).tolist()
            ly = (b * np.sin(th)).tolist()
            z = c.z
            return [Point3D(c.x + lx[i] * cr - ly[i] * sr,
                            c.y + lx[i] * sr + ly[i] * cr, z)
                    for i in range(n + 1)]
        if t == ObjectType.LINE:
            pts = list(obj.points())
        elif t == ObjectType.CURVE:
            pts = obj.getSampledPoints() if hasattr(obj, "getSampledPoints") else []
        elif t == ObjectType.POLYCURVE:
            pts = []
            for seg in obj.segments():
                pts.extend([Point3D(p[0], p[1], p[2]) for p in seg.get("points", [])])
        else:
            return []
        # 闭合轮廓（矩形/多边形等首尾不重复存储）补齐首点副本，
        # 让段遍历覆盖收尾边——否则那条边的交点捕捉永远无提示
        if len(pts) >= 3 and pts[0].distanceTo(pts[-1]) >= 1e-6 \
                and getattr(obj, "isClosed", lambda: False)():
            pts.append(Point3D(pts[0].x, pts[0].y, pts[0].z))
        return pts

    def _chordsNear(self, pts: List[Point3D], wp: Point3D,
                    viewRadius: float) -> list:
        """过滤出距 wp ≤ 捕捉半径的弦（按捕捉平面度量）。"""
        out = []
        for a1, a2 in zip(pts, pts[1:]):
            if self._dist2d(_nearestOnSegment(wp, a1, a2, self._ax), wp) <= viewRadius:
                out.append((a1, a2))
        return out

    def _lineCircle(self, lineObj, circObj) -> List[Point3D]:
        c = circObj.center(); r = circObj.radius()
        # 闭合折线（矩形/多边形）补齐收尾副本，保证收尾边也参与求交
        pts = self._cachedSamples(lineObj)
        out = []
        for i in range(len(pts) - 1):
            a, b = pts[i], pts[i + 1]
            for p in _segCircleIntersect(a, b, c.x, c.y, r):
                if circObj.type() == ObjectType.ARC:
                    ang = math.atan2(p.y - c.y, p.x - c.x)
                    if not _inArcRange(ang, circObj.startAngle(), circObj.endAngle()):
                        continue
                out.append(p)
        return out

    def _circleCircle(self, a, b) -> List[Point3D]:
        ca, ra = a.center(), a.radius()
        cb, rb = b.center(), b.radius()
        d = math.hypot(cb.x - ca.x, cb.y - ca.y)
        if d < 1e-9 or d > ra + rb + 1e-6 or d < abs(ra - rb) - 1e-6:
            return []
        a_ang = math.atan2(cb.y - ca.y, cb.x - ca.x)
        cos_phi = (ra * ra + d * d - rb * rb) / (2 * ra * d)
        cos_phi = max(-1.0, min(1.0, cos_phi))
        phi = math.acos(cos_phi)
        out = []
        for off in (phi, -phi):
            ang = a_ang + off
            p = Point3D(ca.x + ra * math.cos(ang), ca.y + ra * math.sin(ang), ca.z)
            if a.type() == ObjectType.ARC and not _inArcRange(ang, a.startAngle(), a.endAngle()):
                continue
            ang2 = math.atan2(p.y - cb.y, p.x - cb.x)
            if b.type() == ObjectType.ARC and not _inArcRange(ang2, b.startAngle(), b.endAngle()):
                continue
            out.append(p)
        return out


def _bboxOf(pts: List[Point3D], axes: Tuple[int, int] = _AXES_XY):
    if not pts:
        return None
    us = [_comp(p, axes[0]) for p in pts]
    vs = [_comp(p, axes[1]) for p in pts]
    return (min(us), min(vs), max(us), max(vs))


def _bboxOverlap(a, b) -> bool:
    return not (a[2] < b[0] - 1e-9 or b[2] < a[0] - 1e-9
                or a[3] < b[1] - 1e-9 or b[3] < a[1] - 1e-9)

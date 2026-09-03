"""捕捉管理器：按启用的捕捉类型在候选对象上求解吸附点。

支持 10 种对象捕捉（参考 AutoCAD OSNAP）：
端点 / 中点 / 圆心 / 节点 / 象限点 / 交点 / 垂足 / 切点 / 最近点 / 顶点。
"""
from __future__ import annotations
import math
from typing import List, Set, Optional
from .snap_types import SnapType, SnapResult
from ..vector.geometry_types import Point3D, ObjectType


def _nearestOnSegment(p: Point3D, a: Point3D, b: Point3D) -> Point3D:
    """点 p 到线段 ab 的最近点（钳制到段内）。"""
    dx, dy = b.x - a.x, b.y - a.y
    L2 = dx * dx + dy * dy
    if L2 < 1e-12:
        return a
    t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / L2
    t = max(0.0, min(1.0, t))
    return Point3D(a.x + t * dx, a.y + t * dy, a.z)


def _segSegIntersect(a1: Point3D, a2: Point3D, b1: Point3D, b2: Point3D):
    """两线段交点（无交点返回 None）。"""
    d1x, d1y = a2.x - a1.x, a2.y - a1.y
    d2x, d2y = b2.x - b1.x, b2.y - b1.y
    denom = d1x * d2y - d1y * d2x
    if abs(denom) < 1e-12:
        return None
    t = ((b1.x - a1.x) * d2y - (b1.y - a1.y) * d2x) / denom
    s = ((b1.x - a1.x) * d1y - (b1.y - a1.y) * d1x) / denom
    if -1e-6 <= t <= 1 + 1e-6 and -1e-6 <= s <= 1 + 1e-6:
        return Point3D(a1.x + t * d1x, a1.y + t * d1y, a1.z)
    return None


def _segCircleIntersect(a: Point3D, b: Point3D, cx: float, cy: float, r: float):
    """线段与圆交点列表。"""
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
        self._snapRadius: float = 12.0
        # 解析对象（圆/弧/椭圆/直线）→ 采样折线缓存：键为几何签名（内容变化即换键），
        # 超上限整体清空；非解析对象不走此缓存（显示点列由对象自身缓存）
        self._sampleCache = {}

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

    def snap(self, worldPos: Point3D, objects: List, viewRadius: float,
             refPoint: Optional[Point3D] = None) -> SnapResult:
        candidates: List[SnapResult] = []
        # 单对象捕捉
        for obj in objects:
            if not obj.isVisible() or obj.isLocked():
                continue
            supported = set(obj.getSupportedSnapTypes())
            for st in (self._enabledTypes & supported):
                for r in self._snapObject(obj, st, worldPos, viewRadius, refPoint):
                    if r.isValid and r.distance <= viewRadius:
                        candidates.append(r)
        # 交点捕捉：需两两对象求交
        if SnapType.INTERSECTION in self._enabledTypes:
            candidates.extend(self._snapIntersections(worldPos, objects, viewRadius))
        if not candidates:
            return SnapResult.invalid()
        # 优先级：最近点级别最低，只要存在非最近点命中就只用非最近点
        precise = [r for r in candidates if r.type != SnapType.NEAREST]
        pool = precise if precise else candidates
        return min(pool, key=lambda r: (r.distance, _SNAP_PRIORITY.get(r.type, 99)))

    def _makeResult(self, obj, st: SnapType, p: Point3D, worldPos: Point3D) -> SnapResult:
        d = math.hypot(p.x - worldPos.x, p.y - worldPos.y)
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
                p = Point3D((a.x + b.x) * 0.5, (a.y + b.y) * 0.5, (a.z + b.z) * 0.5)
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

    @staticmethod
    def _pathMidpoint(pts) -> Optional[Point3D]:
        """沿折线累加长度，返回总长度一半处的点（真正的路径中点）。"""
        if len(pts) < 2:
            return None
        segs = []
        total = 0.0
        for i in range(len(pts) - 1):
            d = math.hypot(pts[i + 1].x - pts[i].x, pts[i + 1].y - pts[i].y)
            segs.append(d)
            total += d
        if total < 1e-9:
            return None
        half = total * 0.5
        acc = 0.0
        for i, d in enumerate(segs):
            if acc + d >= half:
                t = (half - acc) / d if d > 1e-9 else 0.0
                a, b = pts[i], pts[i + 1]
                return Point3D(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t, a.z)
            acc += d
        return pts[-1]

    def _snapCenter(self, obj, wp: Point3D, viewRadius: float) -> List[SnapResult]:
        out = []
        t = obj.type()
        if t in (ObjectType.CIRCLE, ObjectType.ARC, ObjectType.ELLIPSE):
            c = obj.center()
            d = math.hypot(wp.x - c.x, wp.y - c.y)
            if t == ObjectType.ELLIPSE:
                # 椭圆：鼠标在椭圆边界附近且偏内 → 圆心
                a, b = obj.majorAxis(), obj.minorAxis()
                if a < 1e-9 or b < 1e-9:
                    return out
                ang = math.atan2((wp.y - c.y) * a, (wp.x - c.x) * b)
                bx = c.x + a * math.cos(ang); by = c.y + b * math.sin(ang)
                edge_d = math.hypot(wp.x - bx, wp.y - by)
                # 偏内：归一化坐标在椭圆内
                inside = ((wp.x - c.x) / a) ** 2 + ((wp.y - c.y) / b) ** 2 < 1.0
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
            c = obj.center(); a = obj.majorAxis(); b = obj.minorAxis()
            for ang in (0, math.pi / 2, math.pi, 3 * math.pi / 2):
                p = Point3D(c.x + a * math.cos(ang), c.y + b * math.sin(ang), c.z)
                out.append(self._makeResult(obj, SnapType.QUARTER, p, wp))
        return out

    def _snapPerpendicular(self, obj, wp: Point3D,
                            refPoint: Optional[Point3D]) -> List[SnapResult]:
        """垂足：从参考点（当前绘制起点）向目标线作垂线。
        情况1：参考点在线外，垂足在目标线上，鼠标靠近垂足时捕捉。
        情况2：参考点在目标线上，鼠标移动使预览线与目标线垂直时，
        将鼠标吸附到精确垂线方向，标记画在参考点（垂足）。"""
        out = []
        if refPoint is None or obj.type() != ObjectType.LINE:
            return out
        pts = obj.points()
        for i in range(len(pts) - 1):
            a, b = pts[i], pts[i + 1]
            foot = _nearestOnSegment(refPoint, a, b)
            dx, dy = b.x - a.x, b.y - a.y
            L2 = dx * dx + dy * dy
            if L2 < 1e-12:
                continue
            onLine = (math.hypot(foot.x - refPoint.x, foot.y - refPoint.y) < 1e-6)
            if onLine:
                # 情况2：参考点在线上，预览线与目标线垂直时吸附
                rx, ry = wp.x - refPoint.x, wp.y - refPoint.y
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
                    snapped = Point3D(refPoint.x + px * proj,
                                      refPoint.y + py * proj, refPoint.z)
                    d = math.hypot(snapped.x - wp.x, snapped.y - wp.y)
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
                p = _nearestOnSegment(wp, pts[i], pts[i + 1])
                d = math.hypot(p.x - wp.x, p.y - wp.y)
                if best is None or d < best[1]:
                    best = (p, d)
            if best:
                out.append(self._makeResult(obj, SnapType.NEAREST, best[0], wp))
        elif t in (ObjectType.CURVE, ObjectType.POLYCURVE):
            pts = obj.getSampledPoints() if hasattr(obj, "getSampledPoints") else []
            best = None
            for i in range(len(pts) - 1):
                p = _nearestOnSegment(wp, pts[i], pts[i + 1])
                d = math.hypot(p.x - wp.x, p.y - wp.y)
                if best is None or d < best[1]:
                    best = (p, d)
            if best:
                out.append(self._makeResult(obj, SnapType.NEAREST, best[0], wp))
        elif t in (ObjectType.CIRCLE, ObjectType.ARC):
            c = obj.center(); r = obj.radius()
            a = math.atan2(wp.y - c.y, wp.x - c.x)
            if t == ObjectType.ARC and not _inArcRange(a, obj.startAngle(), obj.endAngle()):
                a = obj.startAngle()
            p = Point3D(c.x + r * math.cos(a), c.y + r * math.sin(a), c.z)
            out.append(self._makeResult(obj, SnapType.NEAREST, p, wp))
        elif t == ObjectType.ELLIPSE:
            c = obj.center(); a = obj.majorAxis(); b = obj.minorAxis()
            ang = math.atan2((wp.y - c.y) * a, (wp.x - c.x) * b)
            p = Point3D(c.x + a * math.cos(ang), c.y + b * math.sin(ang), c.z)
            out.append(self._makeResult(obj, SnapType.NEAREST, p, wp))
        return out

    def _snapVertex(self, obj, wp: Point3D) -> List[SnapResult]:
        out = []
        if obj.type() == ObjectType.LINE:
            for i in range(obj.controlPointCount()):
                out.append(self._makeResult(obj, SnapType.VERTEX,
                                            obj.getControlPoint(i), wp))
        return out

    # ---------- 交点（两两对象） ----------
    def _snapIntersections(self, wp: Point3D, objects: List,
                           viewRadius: float) -> List[SnapResult]:
        out: List[SnapResult] = []
        n = len(objects)
        for i in range(n):
            oi = objects[i]
            if not oi.isVisible() or oi.isLocked():
                continue
            for j in range(i + 1, n):
                oj = objects[j]
                if not oj.isVisible() or oj.isLocked():
                    continue
                for p in self._intersectPair(oi, oj, wp, viewRadius):
                    d = math.hypot(p.x - wp.x, p.y - wp.y)
                    if d <= viewRadius:
                        out.append(SnapResult(position=p, type=SnapType.INTERSECTION,
                                              objectId=oi.id(), distance=d, isValid=True))
        return out

    def _intersectPair(self, a, b, wp: Optional[Point3D] = None,
                       viewRadius: float = 0.0) -> List[Point3D]:
        ta, tb = a.type(), b.type()
        pts: List[Point3D] = []
        if ta == ObjectType.LINE and tb == ObjectType.LINE:
            # 闭合折线（矩形/多边形）点列补齐收尾副本后再求交
            pa = self._cachedSamples(a)
            pb = self._cachedSamples(b)
            if len(pa) >= 2 and len(pb) >= 2:
                # 包围盒快速排除（无交则整对跳过，避免 O(n·m) 全量弦求交）
                if not _bboxOverlap(_bboxOf(pa), _bboxOf(pb)):
                    return []
                if wp is not None and viewRadius > 0 and \
                        (len(pa) > 32 or len(pb) > 32):
                    # 多段折线：只保留经过鼠标附近（捕捉半径内）的弦，控制组合数
                    nearA = self._chordsNear(pa, wp, viewRadius)
                    nearB = self._chordsNear(pb, wp, viewRadius)
                else:
                    nearA = list(zip(pa, pa[1:]))
                    nearB = list(zip(pb, pb[1:]))
                for a1, a2 in nearA:
                    for b1, b2 in nearB:
                        p = _segSegIntersect(a1, a2, b1, b2)
                        if p:
                            pts.append(p)
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
            if len(sa) >= 2 and len(sb) >= 2:
                # 包围盒快速排除
                if (_bboxOf(sa) is None or _bboxOf(sb) is None):
                    return []
                if not _bboxOverlap(_bboxOf(sa), _bboxOf(sb)):
                    return []
                if wp is not None and viewRadius > 0:
                    # 只保留经过鼠标附近（捕捉半径内）的弦，控制组合数
                    nearA = self._chordsNear(sa, wp, viewRadius)
                    nearB = self._chordsNear(sb, wp, viewRadius)
                else:
                    nearA = list(zip(sa, sa[1:]))
                    nearB = list(zip(sb, sb[1:]))
                for a1, a2 in nearA:
                    for b1, b2 in nearB:
                        p = _segSegIntersect(a1, a2, b1, b2)
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
        if hit is None:
            hit = self._samplePointsOf(obj)
            if len(self._sampleCache) > 256:
                self._sampleCache.clear()
            self._sampleCache[key] = hit
        return hit

    def _samplePointsOf(self, obj) -> List[Point3D]:
        """对象的显示采样折线（交点/最近点等通用求交的数据源）。

        圆/弧/椭圆按解析公式密采（显示即解析曲线，1024 点矢高可忽略），
        其余类型取显示点列本身——保证交点捕捉位置与显示曲线一致。"""
        t = obj.type()
        n = 1024
        if t == ObjectType.CIRCLE:
            c, r = obj.center(), obj.radius()
            return [Point3D(c.x + r * math.cos(2 * math.pi * i / n),
                            c.y + r * math.sin(2 * math.pi * i / n), c.z)
                    for i in range(n + 1)]
        if t == ObjectType.ARC:
            c, r = obj.center(), obj.radius()
            sa, ea = obj.startAngle(), obj.endAngle()
            span = ea - sa
            if abs(span) < 1e-9:
                span = 2 * math.pi
            return [Point3D(c.x + r * math.cos(sa + span * i / n),
                            c.y + r * math.sin(sa + span * i / n), c.z)
                    for i in range(n + 1)]
        if t == ObjectType.ELLIPSE:
            c = obj.center()
            a, b = obj.majorAxis(), obj.minorAxis()
            rot = obj.rotation()
            cr, sr = math.cos(rot), math.sin(rot)
            out: List[Point3D] = []
            for i in range(n + 1):
                th = 2 * math.pi * i / n
                lx = a * math.cos(th)
                ly = b * math.sin(th)
                out.append(Point3D(c.x + lx * cr - ly * sr,
                                   c.y + lx * sr + ly * cr, c.z))
            return out
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

    @staticmethod
    def _chordsNear(pts: List[Point3D], wp: Point3D,
                    viewRadius: float) -> list:
        """过滤出距 wp ≤ 捕捉半径的弦（交点若在 wp 附近，必在这样的弦上）。"""
        out = []
        for a1, a2 in zip(pts, pts[1:]):
            if _nearestOnSegment(wp, a1, a2).distanceTo(wp) <= viewRadius:
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


def _bboxOf(pts: List[Point3D]):
    if not pts:
        return None
    xs = [p.x for p in pts]
    ys = [p.y for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _bboxOverlap(a, b) -> bool:
    return not (a[2] < b[0] - 1e-9 or b[2] < a[0] - 1e-9
                or a[3] < b[1] - 1e-9 or b[3] < a[1] - 1e-9)

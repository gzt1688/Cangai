"""延伸（Extend）几何：把对象的指定端点沿当前切线方向延伸指定距离。

- extendObject(obj, atHead, distance)：就地或替换为延伸后的对象，返回
  (new_obj, replaced)。replaced=True 表示原对象需由调用方从文档移除，
  改为添加 new_obj；replaced=False 表示已就地修改原对象，调用方只需
  通知 UI 刷新数据。

支持类型：
- LINE：直接修改首/尾端点（保持 LineObject，replaced=False）。
- ARC：通过增加扫掠角实现延伸（保持 ArcObject，replaced=False）。
- CURVE / POLYCURVE / ELLIPSE：转 PolyCurveObject（原采样 + 延伸段），
  replaced=True。原对象被新 PolyCurve 替换。

延伸方向：取自最接近端点的两采样点连线的反向（即端段切线方向），
distance 为正表示向外延伸，负值会反向缩短（调用方按需校验）。
"""
from __future__ import annotations
import math
from typing import List, Tuple, Optional

from .geometry_types import Point3D


def _sampleOf(obj) -> List[Point3D]:
    """获取对象采样点（直线取顶点，曲线/弧/椭圆取采样折线）。"""
    t = obj.type().name
    if t == "LINE":
        return list(obj.points())
    if t == "CURVE":
        return list(obj.getSampledPoints())
    if t in ("ARC", "ELLIPSE", "CIRCLE"):
        poly = obj.toPolycurve()
        out: List[Point3D] = []
        for seg in poly.segments():
            out.extend([Point3D(p[0], p[1], p[2]) for p in seg.get("points", [])])
        return out
    if t == "POLYCURVE":
        out = []
        for seg in obj.segments():
            out.extend([Point3D(p[0], p[1], p[2]) for p in seg.get("points", [])])
        return out
    return []


def _endTangent(pts: List[Point3D], atHead: bool) -> Optional[Tuple[float, float]]:
    """端段切线单位向量（向外方向）。
    首端：取 pts[0]→pts[1] 反向；尾端：取 pts[-1]→pts[-2] 反向。
    退化（点过少或重合）返回 None。
    """
    if atHead:
        if len(pts) < 2:
            return None
        p0, p1 = pts[0], pts[1]
    else:
        if len(pts) < 2:
            return None
        p0, p1 = pts[-1], pts[-2]
    d = math.hypot(p0.x - p1.x, p0.y - p1.y)
    if d < 1e-9:
        return None
    return ((p0.x - p1.x) / d, (p0.y - p1.y) / d)


def extendObject(obj, atHead: bool, distance: float):
    """延伸对象指定端点 distance 距离。

    返回 (new_obj, replaced)：
    - new_obj: 延伸结果对象（replaced=False 时为原对象就地修改；
      replaced=True 时为新对象，需由调用方用其替换原对象）
    - replaced: 是否需要移除原对象
    - 失败返回 (None, False)
    """
    if distance <= 0:
        return None, False
    t = obj.type().name

    # LINE：直接在首/尾端插入新点
    if t == "LINE":
        pts = obj.points()
        if len(pts) < 2:
            return None, False
        uv = _endTangent(pts, atHead)
        if uv is None:
            return None, False
        ux, uy = uv
        if atHead:
            p0 = pts[0]
            new_pt = Point3D(p0.x + ux * distance, p0.y + uy * distance, p0.z)
            pts.insert(0, new_pt)
        else:
            p0 = pts[-1]
            new_pt = Point3D(p0.x + ux * distance, p0.y + uy * distance, p0.z)
            pts.append(new_pt)
        return obj, False

    # ARC：按 distance/radius 增加扫掠角
    if t == "ARC":
        r = obj.radius()
        if r < 1e-9:
            return None, False
        delta = distance / r
        if atHead:
            obj.setAngles(obj.startAngle() - delta, obj.endAngle())
        else:
            obj.setAngles(obj.startAngle(), obj.endAngle() + delta)
        return obj, False

    # CURVE/POLYCURVE/ELLIPSE：转 PolyCurveObject（原采样 + 延伸段）
    sampled = _sampleOf(obj)
    if len(sampled) < 2:
        return None, False
    uv = _endTangent(sampled, atHead)
    if uv is None:
        return None, False
    ux, uy = uv

    from .poly_curve_object import PolyCurveObject
    poly = PolyCurveObject()
    # 原对象所有采样点作为一段加入
    poly.addSegment({
        "points": [[p.x, p.y, p.z] for p in sampled]
    })
    if atHead:
        # 在原段前插入延伸段（new_pt → 原 first）
        p0 = sampled[0]
        new_pt = Point3D(p0.x + ux * distance, p0.y + uy * distance, p0.z)
        # 重新组织：把延伸段作为第一段，原段作为第二段
        poly2 = PolyCurveObject()
        poly2.addSegment({
            "points": [[new_pt.x, new_pt.y, new_pt.z],
                       [p0.x, p0.y, p0.z]]
        })
        for seg in poly.segments():
            poly2.addSegment(seg)
        poly = poly2
    else:
        # 在原段后追加延伸段（原 last → new_pt）
        p0 = sampled[-1]
        new_pt = Point3D(p0.x + ux * distance, p0.y + uy * distance, p0.z)
        poly.addSegment({
            "points": [[p0.x, p0.y, p0.z],
                       [new_pt.x, new_pt.y, new_pt.z]]
        })

    poly._color = obj._color
    poly._lineWidth = obj._lineWidth
    poly._layerId = obj.layerId()
    return poly, True


def _raySegmentDistance(p0: Point3D, uv, a: Point3D, b: Point3D):
    """射线 p0→uv（单位向量）与线段 a-b 求交。

    返回沿射线方向距离 t（>0 表示在端点前方）且交点落在线段内（s∈[0,1]）时为 t，
    否则返回 None。基于 fillet_processor.lineLineIntersection（不钳制 t/s 的无限线求交）。
    """
    from .fillet_processor import lineLineIntersection
    p1 = Point3D(p0.x + uv[0], p0.y + uv[1], p0.z)
    ip, t, s = lineLineIntersection(p0, p1, a, b)
    if ip is None or t <= 1e-6 or s < -1e-6 or s > 1.0 + 1e-6:
        return None
    return t  # uv 为单位向量 → t 即距离


def _segmentsOf(obj):
    """返回对象线段列表 [(a, b), ...]，闭合对象补上首尾闭合段。

    闭合对象（如矩形 LineObject 只存 4 个顶点不重复末点）的闭合回边
    不在相邻采样点对里，必须补上，否则延伸射线会穿过该边。
    """
    pts = _sampleOf(obj)
    if len(pts) < 2:
        return []
    segs = [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    closed = False
    if hasattr(obj, "isClosed"):
        try:
            closed = bool(obj.isClosed())
        except Exception:
            closed = False
    # 闭合但首末点不重合 → 补闭合段（首末点重合时相邻对已含闭合边）
    if closed and pts[0].distanceTo(pts[-1]) > 1e-6:
        segs.append((pts[-1], pts[0]))
    return segs


def nearestExtendDistance(obj, atHead: bool, boundaryObjects):
    """沿 obj 指定端切线射线，求与 boundaryObjects 采样段的最近交点距离。

    boundaryObjects 为候选边界曲线列表（已排除 obj 自身或仅含目标曲线），
    闭合边界对象（矩形/圆等）包含闭合回边。无交点返回 None。
    """
    pts = _sampleOf(obj)
    if len(pts) < 2:
        return None
    uv = _endTangent(pts, atHead)
    if uv is None:
        return None
    p0 = pts[0] if atHead else pts[-1]
    best = None
    for b in boundaryObjects:
        for a, c in _segmentsOf(b):
            t = _raySegmentDistance(p0, uv, a, c)
            if t is not None and (best is None or t < best):
                best = t
    return best

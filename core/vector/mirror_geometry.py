"""镜像几何：点与对象关于直线（镜像轴）的对称变换。

- mirrorPoint：点关于过 p1、p2 直线的对称点（2D 投影反射，Z 不变）；
- mirrorObject：对象沿镜像轴生成镜像副本，尽量保留原类型
  （LINE/CIRCLE/ARC/ELLIPSE/CURVE/POLYCURVE/POINT），外观属性同步复制。

用于镜像命令（用户点击 2 点定义镜像轴）与变换面板 XYZ 镜像的几何基础。
"""
from __future__ import annotations
import math
from typing import List, Optional

from .geometry_types import Point3D, ObjectType
from .line_object import LineObject
from .circle_object import CircleObject
from .arc_object import ArcObject
from .ellipse_object import EllipseObject
from .curve_object import CurveObject
from .poly_curve_object import PolyCurveObject
from .point_object import PointObject


def mirrorPoint(p: Point3D, p1: Point3D, p2: Point3D) -> Point3D:
    """点 p 关于过 p1、p2 直线的对称点（投影反射，Z 保持不变）。"""
    dx = p2.x - p1.x
    dy = p2.y - p1.y
    L = math.hypot(dx, dy)
    if L < 1e-12:
        return Point3D(p.x, p.y, p.z)
    ux, uy = dx / L, dy / L
    t = (p.x - p1.x) * ux + (p.y - p1.y) * uy
    projx = p1.x + ux * t
    projy = p1.y + uy * t
    return Point3D(2.0 * projx - p.x, 2.0 * projy - p.y, p.z)


def _mirrorPts(pts: List[Point3D], p1: Point3D, p2: Point3D) -> List[Point3D]:
    return [mirrorPoint(q, p1, p2) for q in pts]


def _arcSamplePoints(obj: ArcObject) -> List[Point3D]:
    """弧上 3 点（起点/中点/终点），用于镜像后三点重建保持弧方向。"""
    sa = obj.startAngle()
    ea = obj.endAngle()
    span = ea - sa
    if abs(span) < 1e-9:
        span = 2.0 * math.pi
    return [obj._arcPoint(sa), obj._arcPoint(sa + span * 0.5),
            obj._arcPoint(sa + span)]


def mirrorObject(obj, p1: Point3D, p2: Point3D):
    """生成 obj 关于镜像轴 p1-p2 的镜像副本（不入文档，保留外观属性）。

    支持 LINE/CIRCLE/ARC/ELLIPSE/CURVE/POLYCURVE/POINT；
    不支持的类型的返回 None。
    """
    t = obj.type().name
    o = None
    if t == "LINE":
        o = LineObject(_mirrorPts(obj.points(), p1, p2))
        o.setClosed(obj.isClosed())
    elif t == "CIRCLE":
        c = mirrorPoint(obj.center(), p1, p2)
        o = CircleObject(c.x, c.y, c.z, obj.radius())
    elif t == "ARC":
        p_s, p_m, p_e = _arcSamplePoints(obj)
        o = ArcObject()
        if not o.setThreePoints(mirrorPoint(p_s, p1, p2),
                                mirrorPoint(p_m, p1, p2),
                                mirrorPoint(p_e, p1, p2)):
            # 三点退化（共线）兜底：采样折线镜像 → PolyCurve
            poly = obj.toPolycurve()
            o = PolyCurveObject()
            for seg in poly.segments():
                ns = {"points": [[q.x, q.y, q.z] for q in
                                 _mirrorPts([Point3D(*p) for p in
                                             seg.get("points", [])], p1, p2)]}
                if seg.get("inputPoints"):
                    ns["inputPoints"] = [
                        [q.x, q.y, q.z] for q in
                        _mirrorPts([Point3D(*p) for p in seg["inputPoints"]],
                                   p1, p2)]
                o.addSegment(ns)
    elif t == "ELLIPSE":
        c = mirrorPoint(obj.center(), p1, p2)
        o = EllipseObject(c.x, c.y, c.z, obj.majorAxis(), obj.minorAxis())
        # 长轴方向角关于镜像轴方向反射：θ' = 2*axisAngle - θ
        axisAngle = math.atan2(p2.y - p1.y, p2.x - p1.x)
        o.setRotation(2.0 * axisAngle - obj.rotation())
    elif t == "CURVE":
        o = obj.clone()
        pts = _mirrorPts(list(obj.inputPoints()), p1, p2)
        o.buildFromPoints(pts, obj.curveDegree(), obj.curveMode())
        o.setClosed(obj.isClosed())
    elif t == "POLYCURVE":
        o = PolyCurveObject()
        for seg in obj.segments():
            ns = {"points": [[q.x, q.y, q.z] for q in
                             _mirrorPts([Point3D(*p) for p in
                                         seg.get("points", [])], p1, p2)]}
            if seg.get("inputPoints"):
                ns["inputPoints"] = [
                    [q.x, q.y, q.z] for q in
                    _mirrorPts([Point3D(*p) for p in seg["inputPoints"]],
                               p1, p2)]
            o.addSegment(ns)
    elif t == "POINT":
        p = mirrorPoint(obj.position(), p1, p2)
        o = PointObject(p.x, p.y, p.z)
    else:
        return None

    o._color = obj._color
    o._lineWidth = obj._lineWidth
    o._layerId = obj.layerId()
    return o

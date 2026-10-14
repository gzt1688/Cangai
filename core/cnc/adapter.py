"""几何适配器：cangai_relief 对象 → pymillcam GeometryEntity。

pymillcam 的 Segment 模型：
- LineSegment(start=(x,y), end=(x,y))
- ArcSegment(center=(x,y), radius, start_angle_deg, sweep_deg)
  sweep_deg：正=CCW，负=CW，abs=360 为整圆；start/end 由极坐标导出

cangai_relief 对象：
- LineObject：多点 polyline（List[Point3D]），可能闭合
- ArcObject：圆心 + 半径 + 起角/终角（弧度）
- CircleObject：圆心 + 半径
- CurveObject：样条曲线，采样为折线
- PointObject：单点（钻孔用）
"""
from __future__ import annotations

import math
from typing import List

from pymillcam.core.geometry import GeometryEntity
from pymillcam.core.segments import ArcSegment, LineSegment, Segment

from core.vector.arc_object import ArcObject
from core.vector.circle_object import CircleObject
from core.vector.curve_object import CurveObject
from core.vector.line_object import LineObject
from core.vector.point_object import PointObject
from core.vector.geometry_types import ObjectType, Point3D


def _pt2(p: Point3D) -> tuple[float, float]:
    return (float(p.x), float(p.y))


def line_to_segments(obj: LineObject) -> List[Segment]:
    """多点 polyline → 一串 LineSegment。闭合时首尾相接自动补一段。"""
    pts = obj.points()
    segs: List[Segment] = []
    if len(pts) < 2:
        return segs
    for a, b in zip(pts, pts[1:]):
        segs.append(LineSegment(start=_pt2(a), end=_pt2(b)))
    if obj.isClosed() and len(pts) >= 3:
        # 闭合：补尾→首
        if _pt2(pts[-1]) != _pt2(pts[0]):
            segs.append(LineSegment(start=_pt2(pts[-1]), end=_pt2(pts[0])))
    return segs


def arc_to_segments(obj: ArcObject) -> List[Segment]:
    """ArcObject（弧度）→ ArcSegment（度）。

    cangai 的 startAngle/endAngle 为弧度，endAngle>startAngle 视为 CCW。
    pymillcam 的 sweep_deg：正=CCW，负=CW。
    """
    cx, cy, _cz = obj.center().x, obj.center().y, obj.center().z
    r = obj.radius()
    s_rad = obj.startAngle()
    e_rad = obj.endAngle()
    s_deg = math.degrees(s_rad)
    sweep_deg = math.degrees(e_rad - s_rad)
    # 规范化到 (-360, 360]，保留方向
    if sweep_deg == 0.0:
        sweep_deg = 360.0  # 退化为整圆
    if abs(sweep_deg) > 360.0:
        sweep_deg = math.copysign(360.0, sweep_deg)
    return [ArcSegment(center=(float(cx), float(cy)),
                       radius=float(r),
                       start_angle_deg=s_deg,
                       sweep_deg=sweep_deg)]


def circle_to_segments(obj: CircleObject) -> List[Segment]:
    """CircleObject → 整圆 ArcSegment（sweep=360，CCW）。"""
    c = obj.center()
    r = obj.radius()
    return [ArcSegment(center=(float(c.x), float(c.y)),
                       radius=float(r),
                       start_angle_deg=0.0,
                       sweep_deg=360.0)]


def curve_to_segments(obj: CurveObject, samples_per_seg: int = 64) -> List[Segment]:
    """CurveObject → 采样为折线 LineSegment 列表。

    pymillcam 不支持 NURBS/样条，统一采样为折线（chord tolerance 由
    ProjectSettings 控制，这里仅做几何桥接，精度由采样密度保证）。
    """
    pts = obj.getSampledPoints(samplesPerSeg=samples_per_seg)
    segs: List[Segment] = []
    if len(pts) < 2:
        return segs
    for a, b in zip(pts, pts[1:]):
        segs.append(LineSegment(start=_pt2(a), end=_pt2(b)))
    if obj.isClosed() and len(pts) >= 3:
        if _pt2(pts[-1]) != _pt2(pts[0]):
            segs.append(LineSegment(start=_pt2(pts[-1]), end=_pt2(pts[0])))
    return segs


def ellipse_to_segments(obj, samples: int = 128) -> List[Segment]:
    """EllipseObject → 闭合折线 LineSegment 列表（参数方程采样）。

    majorAxis/minorAxis 为半轴（中心到顶点距离），rotation 为长轴旋转角（弧度）。
    """
    c = obj.center()
    a = obj.majorAxis()
    b = obj.minorAxis()
    rot = obj.rotation()
    cr, sr = math.cos(rot), math.sin(rot)
    pts = []
    for i in range(samples):
        t = 2.0 * math.pi * i / samples
        ct, st = math.cos(t), math.sin(t)
        pts.append((float(c.x + a * ct * cr - b * st * sr),
                    float(c.y + a * ct * sr + b * st * cr)))
    segs: List[Segment] = []
    for i in range(samples):
        segs.append(LineSegment(start=pts[i], end=pts[(i + 1) % samples]))
    return segs


def object_to_entity(obj) -> GeometryEntity | None:
    """任意 cangai 对象 → pymillcam GeometryEntity；不支持的类型返回 None。"""
    t = obj.type()
    segs: List[Segment] | None = None
    closed = False
    point: tuple[float, float] | None = None

    if t == ObjectType.LINE:
        segs = line_to_segments(obj)
        closed = obj.isClosed()
    elif t == ObjectType.ARC:
        segs = arc_to_segments(obj)
        # 单段弧默认不闭合（pymillcam 用 closed 表示闭合轮廓）
        closed = False
    elif t == ObjectType.CIRCLE:
        segs = circle_to_segments(obj)
        closed = True
    elif t == ObjectType.CURVE:
        segs = curve_to_segments(obj)
        closed = obj.isClosed()
    elif t == ObjectType.ELLIPSE:
        # 椭圆不是圆弧（变半径），按参数方程采样为闭合折线
        segs = ellipse_to_segments(obj)
        closed = True
    elif t == ObjectType.NURBS_CURVE:
        # NURBS 采样走 CurveObject 同样路径
        segs = curve_to_segments(obj)
        closed = obj.isClosed()
    elif t == ObjectType.POLYCURVE:
        # PolyCurve 可能聚合多段；尝试 toPolycurve 或采样
        try:
            segs = curve_to_segments(obj)
        except Exception:
            return None
        closed = getattr(obj, "isClosed", lambda: False)()
    elif t == ObjectType.POINT:
        p = obj.position() if hasattr(obj, "position") else None
        if p is None:
            # PointObject 可能用 getPoint / 其他接口
            return None
        point = (float(p.x), float(p.y))
    else:
        return None

    if point is not None:
        return GeometryEntity(point=point)
    if not segs:
        return None
    return GeometryEntity(segments=segs, closed=closed)


def objects_to_entities(objs) -> list[GeometryEntity]:
    """批量转换，跳过 None（不支持的对象）。"""
    out: list[GeometryEntity] = []
    for o in objs:
        e = object_to_entity(o)
        if e is not None:
            out.append(e)
    return out
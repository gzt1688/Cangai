"""闭合（ClosePath）几何：把开放曲线的首末点用连接段封闭。

- 复用 join_geometry 的对象→段列表转换（LINE/CURVE/POLYCURVE/ARC/NURBS_CURVE）
- 三种连接方式：
  - line   直线连接：末点→首点直线段
  - arc    圆弧连接：过首末点的半圆弧，凸向由首末切线平均方向决定
  - smooth 平滑曲线：Catmull-Rom 平滑段，端部切向与原曲线一致（G1 连续）
- closingSegmentFor：对象级入口，返回连接段 dict（PolyCurve 段格式）；
  对象不支持、已闭合或退化时返回 None。
"""
from __future__ import annotations
import math
from typing import Optional

from .geometry_types import Point3D
from .join_geometry import _segmentsOf as _objToSegments
from .fillet_processor import FilletResult, sampleFilletArc
from core.spline_utils import catmullRom

_CLOSABLE = ("LINE", "CURVE", "POLYCURVE", "ARC", "NURBS_CURVE")


def _norm(vx: float, vy: float) -> Optional[tuple]:
    L = math.hypot(vx, vy)
    if L < 1e-9:
        return None
    return (vx / L, vy / L)


def _segTangentAtEnd(segs) -> Optional[tuple]:
    """段列表末端的切线方向（向前找最后一段的末两点）。"""
    for s in reversed(segs):
        pts = s.get("points", [])
        if len(pts) >= 2:
            return _norm(pts[-1][0] - pts[-2][0], pts[-1][1] - pts[-2][1])
    return None


def _segTangentAtHead(segs) -> Optional[tuple]:
    """段列表首端的切线方向（沿曲线前进方向，首段前两点）。"""
    for s in segs:
        pts = s.get("points", [])
        if len(pts) >= 2:
            return _norm(pts[1][0] - pts[0][0], pts[1][1] - pts[0][1])
    return None


def closingSegmentFor(obj, method: str) -> Optional[dict]:
    """为开放曲线对象构造 tail→head 的闭合连接段。

    返回 PolyCurve 段 dict；对象不支持、已闭合或退化时返回 None。
    method: "line" | "arc" | "smooth"
    """
    if obj.type().name not in _CLOSABLE:
        return None
    segs = _objToSegments(obj)
    if not segs:
        return None
    head_p = segs[0].get("points", [])
    tail_p = segs[-1].get("points", [])
    if not head_p or not tail_p:
        return None
    head = Point3D(*head_p[0])
    tail = Point3D(*tail_p[-1])
    if head.distanceTo(tail) < 1e-6:
        return None  # 已闭合，无可接端点
    T_tail = _segTangentAtEnd(segs)
    T_head = _segTangentAtHead(segs)

    if method == "line":
        return {"points": [[tail.x, tail.y, tail.z],
                           [head.x, head.y, head.z]]}

    v = head - tail
    L = v.length()
    if L < 1e-9:
        return None

    if method == "arc":
        cx, cy = (tail.x + head.x) * 0.5, (tail.y + head.y) * 0.5
        r = L * 0.5
        nx, ny = -v.y / L, v.x / L          # 弦左侧法向
        tx = (T_tail[0] if T_tail else 0.0) + (T_head[0] if T_head else 0.0)
        ty = (T_tail[1] if T_tail else 0.0) + (T_head[1] if T_head else 0.0)
        t = _norm(tx, ty)
        if t is not None and (nx * t[0] + ny * t[1]) < 0:
            nx, ny = -nx, -ny               # 凸向切线平均方向一侧
        fr = FilletResult(valid=True,
                          center=Point3D(cx, cy, tail.z),
                          trimPt1=Point3D(tail.x, tail.y, tail.z),
                          trimPt2=Point3D(head.x, head.y, head.z),
                          arcMid=Point3D(cx + nx * r, cy + ny * r, tail.z))
        pts = [tail] + sampleFilletArc(fr, 20) + [head]
        return {"points": [[p.x, p.y, p.z] for p in pts]}

    if method == "smooth":
        k = L * 0.4
        tt = T_tail if T_tail is not None else _norm(v.x, v.y)
        th = T_head if T_head is not None else _norm(-v.x, -v.y)
        if tt is None:
            tt = (1.0, 0.0)
        if th is None:
            th = (-1.0, 0.0)
        cps = [tail,
               Point3D(tail.x + tt[0] * k, tail.y + tt[1] * k, tail.z),
               Point3D(head.x - th[0] * k, head.y - th[1] * k, head.z),
               head]
        sampled = catmullRom(cps, 20)
        return {"points": [[p.x, p.y, p.z] for p in sampled],
                "inputPoints": [[p.x, p.y, p.z] for p in cps]}
    return None

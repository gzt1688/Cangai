"""圆弧对象模型。"""
from __future__ import annotations
import math
from typing import Optional
from .base_object import BaseObject
from .geometry_types import Point3D, BoundingBox3D, ObjectType
from ..snap.snap_types import SnapType


def computeArcFromThreePoints(p1: Point3D, p2: Point3D, p3: Point3D):
    ax, ay = p1.x, p1.y
    bx, by = p2.x, p2.y
    cx, cy = p3.x, p3.y
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    center = Point3D(ux, uy, p1.z)
    radius = math.hypot(ax - ux, ay - uy)
    startAngle = math.atan2(ay - uy, ax - ux)
    endAngle = math.atan2(cy - uy, cx - ux)
    return center, radius, startAngle, endAngle


class ArcObject(BaseObject):
    def __init__(self, cx: float = 0, cy: float = 0, cz: float = 0,
                 radius: float = 1.0, startAngle: float = 0.0,
                 endAngle: float = math.pi) -> None:
        super().__init__(ObjectType.ARC)
        self._cx = float(cx); self._cy = float(cy); self._cz = float(cz)
        self._radius = float(radius)
        self._startAngle = float(startAngle)
        self._endAngle = float(endAngle)
        self._p1: Optional[Point3D] = None
        self._p2: Optional[Point3D] = None
        self._p3: Optional[Point3D] = None

    def center(self) -> Point3D:
        return Point3D(self._cx, self._cy, self._cz)

    def setCenter(self, p: Point3D) -> None:
        self._cx, self._cy, self._cz = p.x, p.y, p.z

    def radius(self) -> float:
        return self._radius

    def setRadius(self, r: float) -> None:
        self._radius = r

    def startAngle(self) -> float:
        return self._startAngle

    def endAngle(self) -> float:
        return self._endAngle

    def setAngles(self, s: float, e: float) -> None:
        self._startAngle = s
        self._endAngle = e

    def setThreePoints(self, p1: Point3D, p2: Point3D, p3: Point3D) -> bool:
        res = computeArcFromThreePoints(p1, p2, p3)
        if res is None:
            return False
        center, radius, sa, ea = res
        # 选择经过弧上点 p2 的扫掠方向：默认逆时针，
        # 若 p2 不在逆时针路径上则改为顺时针（endAngle < startAngle）
        span = ea - sa
        while span < 0:
            span += 2 * math.pi
        ma = math.atan2(p2.y - center.y, p2.x - center.x)
        delta = ma - sa
        while delta < 0:
            delta += 2 * math.pi
        if delta > span:
            span -= 2 * math.pi
        self._cx, self._cy, self._cz = center.x, center.y, center.z
        self._radius = radius
        self._startAngle = sa
        self._endAngle = sa + span
        self._p1, self._p2, self._p3 = p1, p2, p3
        return True

    def serialize(self) -> dict:
        d = super().serialize()
        d["center"] = [self._cx, self._cy, self._cz]
        d["radius"] = self._radius
        d["startAngle"] = self._startAngle
        d["endAngle"] = self._endAngle
        return d

    def deserialize(self, data: dict) -> None:
        super().deserialize(data)
        c = data.get("center", [0, 0, 0])
        self._cx, self._cy, self._cz = float(c[0]), float(c[1]), float(c[2])
        self._radius = float(data.get("radius", 1.0))
        self._startAngle = float(data.get("startAngle", 0.0))
        self._endAngle = float(data.get("endAngle", 0.0))

    def translate(self, dx: float, dy: float, dz: float) -> None:
        self._cx += dx; self._cy += dy; self._cz += dz

    def rotate(self, angle: float, ax: float, ay: float, az: float) -> None:
        """绕原点旋转（rotateAround 先平移到基准点再调此处）：转圆心位置，
        起止角同步旋转（绕 Z）；三点缓存失效置空。"""
        if (ax, ay, az) == (0, 0, 1):
            c, s = math.cos(angle), math.sin(angle)
            self._cx, self._cy = (self._cx * c - self._cy * s,
                                  self._cx * s + self._cy * c)
            self._startAngle += angle
            self._endAngle += angle
            self._p1 = self._p2 = self._p3 = None

    def scale(self, sx: float, sy: float, sz: float) -> None:
        self._cx *= sx; self._cy *= sy; self._cz *= sz
        self._radius *= (abs(sx) + abs(sy)) * 0.5

    def getBoundingBox(self) -> BoundingBox3D:
        bb = BoundingBox3D()
        n = 32
        for i in range(n + 1):
            t = self._startAngle + (self._endAngle - self._startAngle) * i / n
            bb.expand(Point3D(self._cx + self._radius * math.cos(t),
                              self._cy + self._radius * math.sin(t), self._cz))
        return bb

    def getPosition(self) -> Point3D:
        return self.center()

    def clone(self) -> "ArcObject":
        o = ArcObject(self._cx, self._cy, self._cz, self._radius,
                      self._startAngle, self._endAngle)
        o._color = self._color; o._lineWidth = self._lineWidth
        return o

    def controlPointCount(self) -> int:
        # 弧上 5 等分采样控制点（与 toPolycurve 采样一致）：选中即显示全部控制点；
        # 中心无控制点，点击圆心仍是"选中对象"而非编辑，不会误破坏圆弧
        return 5

    def _arcPoint(self, angle: float) -> Point3D:
        return Point3D(self._cx + self._radius * math.cos(angle),
                       self._cy + self._radius * math.sin(angle), self._cz)

    def getControlPoint(self, i: int) -> Point3D:
        a = self._startAngle + (self._endAngle - self._startAngle) * i / 4
        return self._arcPoint(a)

    def setControlPoint(self, i: int, p: Point3D) -> None:
        # 兜底（编辑前会先转自由曲线）：按到圆心距离更新半径，保持角度结构
        self._radius = max(0.0, math.hypot(p.x - self._cx, p.y - self._cy))

    def toPolycurve(self):
        """转为开放采样曲线：起点+3 内点+终点控制点，Catmull-Rom 平滑保持弧。"""
        from .poly_curve_object import PolyCurveObject
        from core.spline_utils import catmullRom
        from core.vector.geometry_types import Point3D
        poly = PolyCurveObject()
        cps = []
        n = 4
        for i in range(n + 1):
            a = self._startAngle + (self._endAngle - self._startAngle) * i / n
            cps.append(Point3D(self._cx + self._radius * math.cos(a),
                               self._cy + self._radius * math.sin(a), self._cz))
        pts = catmullRom(cps, 20)
        poly.addSegment({"points": [[p.x, p.y, p.z] for p in pts],
                         "inputPoints": [[p.x, p.y, p.z] for p in cps]})
        return poly

    def getSupportedSnapTypes(self):
        return [SnapType.CENTER, SnapType.ENDPOINT, SnapType.MIDPOINT,
                SnapType.QUARTER, SnapType.NEAREST, SnapType.TANGENT,
                SnapType.INTERSECTION]

    def pathLength(self) -> float:
        span = self._endAngle - self._startAngle
        if abs(span) < 1e-9:
            span = 2.0 * math.pi   # 起止角重合视为整圆
        return self._radius * abs(span)

    def enclosedArea(self) -> float | None:
        return None
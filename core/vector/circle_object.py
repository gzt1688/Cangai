"""圆对象模型。"""
from __future__ import annotations
import math
from .base_object import BaseObject
from .geometry_types import Point3D, BoundingBox3D, ObjectType
from ..snap.snap_types import SnapType


class CircleObject(BaseObject):
    def __init__(self, cx: float = 0, cy: float = 0, cz: float = 0,
                 radius: float = 1.0) -> None:
        super().__init__(ObjectType.CIRCLE)
        self._cx = float(cx); self._cy = float(cy); self._cz = float(cz)
        self._radius = float(radius)
        self._nx, self._ny, self._nz = 0.0, 0.0, 1.0

    def center(self) -> Point3D:
        return Point3D(self._cx, self._cy, self._cz)

    def setCenter(self, p: Point3D) -> None:
        self._cx, self._cy, self._cz = p.x, p.y, p.z

    def radius(self) -> float:
        return self._radius

    def setRadius(self, r: float) -> None:
        self._radius = r

    def serialize(self) -> dict:
        d = super().serialize()
        d["center"] = [self._cx, self._cy, self._cz]
        d["radius"] = self._radius
        return d

    def deserialize(self, data: dict) -> None:
        super().deserialize(data)
        c = data.get("center", [0, 0, 0])
        self._cx, self._cy, self._cz = float(c[0]), float(c[1]), float(c[2])
        self._radius = float(data.get("radius", 1.0))

    def translate(self, dx: float, dy: float, dz: float) -> None:
        self._cx += dx; self._cy += dy; self._cz += dz

    def rotate(self, angle: float, ax: float, ay: float, az: float) -> None:
        """绕原点旋转（rotateAround 先平移到基准点再调此处）：只转圆心位置，
        圆周形状旋转不变（与 ImageObject.rotate 同范式）。"""
        if (ax, ay, az) == (0, 0, 1):
            c, s = math.cos(angle), math.sin(angle)
            self._cx, self._cy = (self._cx * c - self._cy * s,
                                  self._cx * s + self._cy * c)

    def scale(self, sx: float, sy: float, sz: float) -> None:
        self._cx *= sx; self._cy *= sy; self._cz *= sz
        self._radius *= (abs(sx) + abs(sy)) * 0.5

    def getBoundingBox(self) -> BoundingBox3D:
        bb = BoundingBox3D()
        bb.expand(Point3D(self._cx - self._radius, self._cy - self._radius, self._cz))
        bb.expand(Point3D(self._cx + self._radius, self._cy + self._radius, self._cz))
        return bb

    def getPosition(self) -> Point3D:
        return self.center()

    def clone(self) -> "CircleObject":
        o = CircleObject(self._cx, self._cy, self._cz, self._radius)
        o._color = self._color; o._lineWidth = self._lineWidth
        return o

    def controlPointCount(self) -> int:
        # 圆周 8 等分控制点（与 toPolycurve 采样一致）：选中即显示全部控制点；
        # 圆心无控制点，点击圆心仍是"选中对象"而非编辑，不会误破坏圆
        return 8

    def getControlPoint(self, i: int) -> Point3D:
        a = 2.0 * math.pi * i / 8
        return Point3D(self._cx + self._radius * math.cos(a),
                       self._cy + self._radius * math.sin(a), self._cz)

    def setControlPoint(self, i: int, p: Point3D) -> None:
        # 拖动任一圆周点按到圆心距离更新半径（拖动前会先转为自由曲线，此为兜底）
        self._radius = max(0.0, math.hypot(p.x - self._cx, p.y - self._cy))

    def toPolycurve(self):
        """转为闭合采样曲线：圆周精确采样（首尾重合），控制点仍为 8 等分点。

        直接用圆公式按角度均匀采样，不经 catmullRom 逼近——catmullRom 是开放
        曲线端点处理（首段 p0、末段 p3 用重复端点），对首尾重合的闭合圆会在
        接缝附近切线方向错误、采样点向内凹陷（实测 30mm 圆接缝处偏差达 1.3mm），
        浮雕起形后首尾点变形。精确采样保证每点严格落在圆周上。
        """
        from .poly_curve_object import PolyCurveObject
        poly = PolyCurveObject()
        # 外观采样：圆周 n_sample 等分精确点（闭合，首尾重合）
        n_sample = 128
        pts = []
        for i in range(n_sample + 1):
            a = 2.0 * math.pi * i / n_sample
            pts.append([self._cx + self._radius * math.cos(a),
                        self._cy + self._radius * math.sin(a), self._cz])
        # 编辑控制点：8 圆周等分点（首尾重合，与 controlPointCount 一致）
        cps = []
        for i in range(8 + 1):
            a = 2.0 * math.pi * i / 8
            cps.append([self._cx + self._radius * math.cos(a),
                        self._cy + self._radius * math.sin(a), self._cz])
        poly.addSegment({"points": pts, "inputPoints": cps})
        return poly

    def getSupportedSnapTypes(self):
        return [SnapType.CENTER, SnapType.TANGENT, SnapType.QUARTER,
                SnapType.NEAREST, SnapType.INTERSECTION]

    def pathLength(self) -> float:
        return 2.0 * math.pi * self._radius

    def enclosedArea(self) -> float | None:
        return math.pi * self._radius * self._radius
"""椭圆对象模型。"""
from __future__ import annotations
import math
from .base_object import BaseObject
from .geometry_types import Point3D, BoundingBox3D, ObjectType
from ..snap.snap_types import SnapType


class EllipseObject(BaseObject):
    def __init__(self, cx: float = 0, cy: float = 0, cz: float = 0,
                 majorAxis: float = 2.0, minorAxis: float = 1.0) -> None:
        super().__init__(ObjectType.ELLIPSE)
        self._cx = float(cx); self._cy = float(cy); self._cz = float(cz)
        self._majorAxis = float(majorAxis)
        self._minorAxis = float(minorAxis)
        self._rotation = 0.0

    def center(self) -> Point3D:
        return Point3D(self._cx, self._cy, self._cz)

    def setCenter(self, p: Point3D) -> None:
        self._cx, self._cy, self._cz = p.x, p.y, p.z

    def majorAxis(self) -> float:
        return self._majorAxis

    def minorAxis(self) -> float:
        return self._minorAxis

    def setAxes(self, major: float, minor: float) -> None:
        self._majorAxis = major
        self._minorAxis = minor

    def rotation(self) -> float:
        return self._rotation

    def setRotation(self, r: float) -> None:
        self._rotation = r

    def serialize(self) -> dict:
        d = super().serialize()
        d["center"] = [self._cx, self._cy, self._cz]
        d["majorAxis"] = self._majorAxis
        d["minorAxis"] = self._minorAxis
        d["rotation"] = self._rotation
        return d

    def deserialize(self, data: dict) -> None:
        super().deserialize(data)
        c = data.get("center", [0, 0, 0])
        self._cx, self._cy, self._cz = float(c[0]), float(c[1]), float(c[2])
        self._majorAxis = float(data.get("majorAxis", 2.0))
        self._minorAxis = float(data.get("minorAxis", 1.0))
        self._rotation = float(data.get("rotation", 0.0))

    def translate(self, dx: float, dy: float, dz: float) -> None:
        self._cx += dx; self._cy += dy; self._cz += dz

    def rotate(self, angle: float, ax: float, ay: float, az: float) -> None:
        if (ax, ay, az) == (0, 0, 1):
            self._rotation += angle

    def scale(self, sx: float, sy: float, sz: float) -> None:
        self._cx *= sx; self._cy *= sy; self._cz *= sz
        self._majorAxis *= abs(sx)
        self._minorAxis *= abs(sy)

    def getBoundingBox(self) -> BoundingBox3D:
        bb = BoundingBox3D()
        bb.expand(Point3D(self._cx - self._majorAxis, self._cy - self._minorAxis, self._cz))
        bb.expand(Point3D(self._cx + self._majorAxis, self._cy + self._minorAxis, self._cz))
        return bb

    def getPosition(self) -> Point3D:
        return self.center()

    def clone(self) -> "EllipseObject":
        o = EllipseObject(self._cx, self._cy, self._cz, self._majorAxis, self._minorAxis)
        o._rotation = self._rotation
        o._color = self._color; o._lineWidth = self._lineWidth
        return o

    def controlPointCount(self) -> int:
        # 椭圆周 8 等分控制点（与 toPolycurve 采样一致）：选中即显示全部控制点；
        # 中心无控制点，点击中心仍是"选中对象"而非编辑，不会误破坏椭圆
        return 8

    def getControlPoint(self, i: int) -> Point3D:
        t = 2.0 * math.pi * i / 8
        c = math.cos(self._rotation)
        s = math.sin(self._rotation)
        ex = self._majorAxis * math.cos(t)
        ey = self._minorAxis * math.sin(t)
        return Point3D(self._cx + ex * c - ey * s,
                       self._cy + ex * s + ey * c, self._cz)

    def setControlPoint(self, i: int, p: Point3D) -> None:
        # 兜底（编辑前会先转自由曲线）：按拖动距离与当前控制点半径的比例缩放两轴，
        # 保持椭圆形状比例
        cur = self.getControlPoint(i)
        r_cur = math.hypot(cur.x - self._cx, cur.y - self._cy)
        r_new = math.hypot(p.x - self._cx, p.y - self._cy)
        if r_cur > 1e-12:
            f = max(0.0, r_new / r_cur)
            self._majorAxis *= f
            self._minorAxis *= f

    def toPolycurve(self):
        """转为闭合采样曲线：8 个椭圆周控制点（含旋转），Catmull-Rom 平滑。"""
        from .poly_curve_object import PolyCurveObject
        from core.spline_utils import catmullRom
        from core.vector.geometry_types import Point3D
        poly = PolyCurveObject()
        c = math.cos(self._rotation)
        s = math.sin(self._rotation)
        cps = []
        n = 8
        for i in range(n + 1):
            t = 2.0 * math.pi * i / n
            ex = self._majorAxis * math.cos(t)
            ey = self._minorAxis * math.sin(t)
            cps.append(Point3D(self._cx + ex * c - ey * s,
                               self._cy + ex * s + ey * c, self._cz))
        pts = catmullRom(cps, 20)
        poly.addSegment({"points": [[p.x, p.y, p.z] for p in pts],
                         "inputPoints": [[p.x, p.y, p.z] for p in cps]})
        return poly

    def getSupportedSnapTypes(self):
        return [SnapType.CENTER, SnapType.QUARTER, SnapType.NEAREST,
                SnapType.INTERSECTION]

    def pathLength(self) -> float:
        # Ramanujan 周长近似，误差约 0.04%
        a = max(abs(self._majorAxis), abs(self._minorAxis))
        b = min(abs(self._majorAxis), abs(self._minorAxis))
        if b <= 0.0:
            return 0.0
        h = ((a - b) / (a + b)) ** 2
        return math.pi * (a + b) * (1.0 + 3.0 * h / (10.0 + math.sqrt(4.0 - 3.0 * h)))

    def enclosedArea(self) -> float | None:
        return math.pi * abs(self._majorAxis) * abs(self._minorAxis)
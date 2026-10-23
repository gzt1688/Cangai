"""几何基础类型：Point3D / BoundingBox3D / Color3D 与坐标变换。"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
import math


class WorkPlane(Enum):
    XY = 0
    XZ = 1
    YZ = 2


class ObjectType(Enum):
    POINT = 0
    LINE = 1
    CURVE = 2
    FACE = 3
    SOLID = 4
    GROUP = 5
    CIRCLE = 6
    ARC = 7
    ELLIPSE = 8
    RECTANGLE = 9
    NURBS_CURVE = 10
    POLYCURVE = 11
    IMAGE = 12
    SURFACE = 13
    DIMENSION = 14
    BREP = 15      # OCC B-Rep 实体（BRepObject，持有 TopoDS_Shape）
    REVCLOUD = 16  # 修订云线（RevcloudObject：bulge 弧闭合多段线）


class CurveMode(Enum):
    FIT = 0
    CONTROL = 1


@dataclass
class Point3D:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    def __getitem__(self, i: int) -> float:
        return (self.x, self.y, self.z)[i]

    def __add__(self, o: "Point3D") -> "Point3D":
        return Point3D(self.x + o.x, self.y + o.y, self.z + o.z)

    def __sub__(self, o: "Point3D") -> "Point3D":
        return Point3D(self.x - o.x, self.y - o.y, self.z - o.z)

    def __mul__(self, s: float) -> "Point3D":
        return Point3D(self.x * s, self.y * s, self.z * s)

    __rmul__ = __mul__

    def dot(self, o: "Point3D") -> float:
        return self.x * o.x + self.y * o.y + self.z * o.z

    def cross(self, o: "Point3D") -> "Point3D":
        return Point3D(
            self.y * o.z - self.z * o.y,
            self.z * o.x - self.x * o.z,
            self.x * o.y - self.y * o.x,
        )

    def length(self) -> float:
        return math.sqrt(self.x * self.x + self.y * self.y + self.z * self.z)

    def normalized(self) -> "Point3D":
        L = self.length()
        if L < EPS_SMALL:
            return Point3D()
        return Point3D(self.x / L, self.y / L, self.z / L)

    def distanceTo(self, o: "Point3D") -> float:
        return (self - o).length()

    def toTuple(self) -> tuple:
        return (self.x, self.y, self.z)

    @staticmethod
    def fromTuple(t) -> "Point3D":
        return Point3D(float(t[0]), float(t[1]), float(t[2]) if len(t) > 2 else 0.0)


@dataclass
class BoundingBox3D:
    min: Point3D = field(default_factory=lambda: Point3D(math.inf, math.inf, math.inf))
    max: Point3D = field(default_factory=lambda: Point3D(-math.inf, -math.inf, -math.inf))

    def isValid(self) -> bool:
        return self.min.x <= self.max.x and self.min.y <= self.max.y and self.min.z <= self.max.z

    def width(self) -> float:
        return self.max.x - self.min.x

    def height(self) -> float:
        return self.max.y - self.min.y

    def depth(self) -> float:
        return self.max.z - self.min.z

    def center(self) -> Point3D:
        return Point3D(
            (self.min.x + self.max.x) * 0.5,
            (self.min.y + self.max.y) * 0.5,
            (self.min.z + self.max.z) * 0.5,
        )

    def expand(self, p: Point3D) -> None:
        self.min.x = min(self.min.x, p.x)
        self.min.y = min(self.min.y, p.y)
        self.min.z = min(self.min.z, p.z)
        self.max.x = max(self.max.x, p.x)
        self.max.y = max(self.max.y, p.y)
        self.max.z = max(self.max.z, p.z)


@dataclass
class Color3D:
    r: float = 1.0
    g: float = 1.0
    b: float = 1.0
    a: float = 1.0

    @staticmethod
    def orange() -> "Color3D":
        return Color3D(1.0, 100 / 255, 0.0, 1.0)

    @staticmethod
    def blue() -> "Color3D":
        return Color3D(0.0, 0.0, 1.0, 1.0)

    @staticmethod
    def white() -> "Color3D":
        return Color3D(1.0, 1.0, 1.0, 1.0)

    def toQColor(self):
        from PySide6.QtGui import QColor
        return QColor(int(self.r * 255), int(self.g * 255), int(self.b * 255), int(self.a * 255))


EPS_SMALL = 1e-10


def polylineLength(points) -> float:
    """折线实际长度（3D 欧氏距离逐段累加）。"""
    total = 0.0
    prev = None
    for p in points:
        if prev is not None:
            total += math.sqrt(
                (p.x - prev.x) ** 2 + (p.y - prev.y) ** 2 + (p.z - prev.z) ** 2)
        prev = p
    return total


def closedArea2D(points) -> float:
    """XY 平面鞋带公式闭合面积；点数 < 3 返回 0。"""
    n = len(points)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        a = points[i]
        b = points[(i + 1) % n]
        s += a.x * b.y - b.x * a.y
    return abs(s) * 0.5


def workPlaneToNormal(wp: WorkPlane) -> Point3D:
    if wp == WorkPlane.XY:
        return Point3D(0, 0, 1)
    if wp == WorkPlane.XZ:
        return Point3D(0, 1, 0)
    return Point3D(1, 0, 0)


def worldToPlane(p: Point3D, wp: WorkPlane) -> Point3D:
    if wp == WorkPlane.XY:
        return Point3D(p.x, p.y, 0)
    if wp == WorkPlane.XZ:
        return Point3D(p.x, p.z, 0)
    return Point3D(p.y, p.z, 0)


def planeToWorld(p: Point3D, wp: WorkPlane) -> Point3D:
    if wp == WorkPlane.XY:
        return Point3D(p.x, p.y, 0)
    if wp == WorkPlane.XZ:
        return Point3D(p.x, 0, p.y)
    return Point3D(0, p.x, p.y)
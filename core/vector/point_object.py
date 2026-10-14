"""点对象模型。"""
from __future__ import annotations
import math
from .base_object import BaseObject
from .geometry_types import Point3D, BoundingBox3D, ObjectType
from ..snap.snap_types import SnapType


class PointObject(BaseObject):
    def __init__(self, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> None:
        super().__init__(ObjectType.POINT)
        self._x = float(x)
        self._y = float(y)
        self._z = float(z)
        self._markerSize = 1.0

    def position(self) -> Point3D:
        return Point3D(self._x, self._y, self._z)

    def setPosition(self, p: Point3D) -> None:
        self._x, self._y, self._z = p.x, p.y, p.z

    def markerSize(self) -> float:
        return self._markerSize

    def serialize(self) -> dict:
        d = super().serialize()
        d["position"] = [self._x, self._y, self._z]
        d["markerSize"] = self._markerSize
        return d

    def deserialize(self, data: dict) -> None:
        super().deserialize(data)
        p = data.get("position", [0, 0, 0])
        self._x, self._y, self._z = float(p[0]), float(p[1]), float(p[2])
        self._markerSize = float(data.get("markerSize", 1.0))

    def translate(self, dx: float, dy: float, dz: float) -> None:
        self._x += dx; self._y += dy; self._z += dz

    def rotate(self, angle: float, ax: float, ay: float, az: float) -> None:
        """绕原点旋转（rotateAround 先平移到基准点再调此处）：只转位置。"""
        if (ax, ay, az) == (0, 0, 1):
            c, s = math.cos(angle), math.sin(angle)
            self._x, self._y = self._x * c - self._y * s, self._x * s + self._y * c

    def scale(self, sx: float, sy: float, sz: float) -> None:
        self._x *= sx; self._y *= sy; self._z *= sz

    def getBoundingBox(self) -> BoundingBox3D:
        bb = BoundingBox3D()
        bb.expand(Point3D(self._x, self._y, self._z))
        return bb

    def getPosition(self) -> Point3D:
        return self.position()

    def clone(self) -> "PointObject":
        o = PointObject(self._x, self._y, self._z)
        o._color = self._color; o._lineWidth = self._lineWidth
        return o

    def controlPointCount(self) -> int:
        return 1

    def getControlPoint(self, i: int) -> Point3D:
        return self.position()

    def setControlPoint(self, i: int, p: Point3D) -> None:
        self.setPosition(p)

    def getSupportedSnapTypes(self):
        return [SnapType.POINT, SnapType.NEAREST]
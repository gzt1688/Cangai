"""多段线对象模型。"""
from __future__ import annotations
import math
from typing import List
from .base_object import BaseObject
from .geometry_types import Point3D, BoundingBox3D, ObjectType, closedArea2D
from ..snap.snap_types import SnapType


class LineObject(BaseObject):
    def __init__(self, points: List[Point3D] | None = None) -> None:
        super().__init__(ObjectType.LINE)
        self._points: List[Point3D] = list(points) if points else []
        self._isClosed: bool = False

    def points(self) -> List[Point3D]:
        return self._points

    def addPoint(self, p: Point3D) -> None:
        self._points.append(p)

    def removeLastPoint(self) -> None:
        if self._points:
            self._points.pop()

    def isClosed(self) -> bool:
        return self._isClosed

    def setClosed(self, c: bool) -> None:
        self._isClosed = c

    def isEmpty(self) -> bool:
        return len(self._points) < 2

    def serialize(self) -> dict:
        d = super().serialize()
        d["points"] = [[p.x, p.y, p.z] for p in self._points]
        d["isClosed"] = self._isClosed
        return d

    def deserialize(self, data: dict) -> None:
        super().deserialize(data)
        self._points = [Point3D(*p) for p in data.get("points", [])]
        self._isClosed = bool(data.get("isClosed", False))

    def translate(self, dx: float, dy: float, dz: float) -> None:
        self._points = [Point3D(p.x + dx, p.y + dy, p.z + dz) for p in self._points]

    def rotate(self, angle: float, ax: float, ay: float, az: float) -> None:
        c, s = math.cos(angle), math.sin(angle)
        if (ax, ay, az) == (0, 0, 1):
            self._points = [Point3D(p.x * c - p.y * s, p.x * s + p.y * c, p.z) for p in self._points]

    def scale(self, sx: float, sy: float, sz: float) -> None:
        self._points = [Point3D(p.x * sx, p.y * sy, p.z * sz) for p in self._points]

    def getBoundingBox(self) -> BoundingBox3D:
        bb = BoundingBox3D()
        for p in self._points:
            bb.expand(p)
        return bb

    def getPosition(self) -> Point3D:
        if not self._points:
            return Point3D()
        return self._points[0]

    def clone(self) -> "LineObject":
        o = LineObject([Point3D(p.x, p.y, p.z) for p in self._points])
        o._isClosed = self._isClosed
        o._color = self._color; o._lineWidth = self._lineWidth
        return o

    def controlPointCount(self) -> int:
        return len(self._points)

    def getControlPoint(self, i: int) -> Point3D:
        return self._points[i]

    def setControlPoint(self, i: int, p: Point3D) -> None:
        if 0 <= i < len(self._points):
            self._points[i] = p

    def getSupportedSnapTypes(self):
        return [SnapType.ENDPOINT, SnapType.MIDPOINT, SnapType.NEAREST,
                SnapType.POINT, SnapType.VERTEX, SnapType.PERPENDICULAR]

    def pathLength(self) -> float:
        pts = self._points
        if len(pts) < 2:
            return 0.0
        total = 0.0
        for i in range(1, len(pts)):
            total += pts[i - 1].distanceTo(pts[i])
        if self._isClosed and len(pts) >= 3 and pts[0].distanceTo(pts[-1]) > 1e-12:
            total += pts[-1].distanceTo(pts[0])
        return total

    def enclosedArea(self) -> float | None:
        """返回闭合折线（XY 平面）鞋带面积。
        同时接受 isClosed flag=True 或「首末控制点几何重合（距离 < 1e-6）」两种闭合；
        少于 3 个唯一顶点返回 None；采样点最后若重复首点会先去重，避免鞋带公式重复归零段。"""
        pts = list(self._points)
        if len(pts) < 3:
            return None
        # 判定是否闭合：标志位 or 几何首尾重合
        closed = bool(self._isClosed)
        if not closed:
            if pts[0].distanceTo(pts[-1]) < 1e-6:
                closed = True
        if not closed:
            return None
        # 去重末点（如果首末重合/几乎重合，丢一个保证唯一顶点）
        if len(pts) >= 2 and pts[0].distanceTo(pts[-1]) < 1e-6:
            pts = pts[:-1]
        if len(pts) < 3:
            return None
        return closedArea2D(pts)
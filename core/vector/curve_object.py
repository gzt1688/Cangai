"""自由曲线对象模型（基于样条重采样）。"""
from __future__ import annotations
import math
from typing import List, Optional
from .base_object import BaseObject
from .geometry_types import (Point3D, BoundingBox3D, ObjectType, CurveMode,
                             polylineLength, closedArea2D)
from ..snap.snap_types import SnapType


class CurveObject(BaseObject):
    def __init__(self) -> None:
        super().__init__(ObjectType.CURVE)
        self._inputPoints: List[Point3D] = []
        self._sampledPoints: List[Point3D] = []
        self._curveMode: CurveMode = CurveMode.FIT
        self._curveDegree: int = 3
        self._isClosed: bool = False
        self._segmentBreaks: List[int] = []

    def inputPoints(self) -> List[Point3D]:
        return self._inputPoints

    def addPoint(self, p: Point3D) -> None:
        self._inputPoints.append(p)
        self._dirty = True

    def removeLastPoint(self) -> None:
        if self._inputPoints:
            self._inputPoints.pop()
            self._dirty = True

    def isEmpty(self) -> bool:
        return len(self._inputPoints) < 2

    def curveMode(self) -> CurveMode:
        return self._curveMode

    def setCurveMode(self, m: CurveMode) -> None:
        self._curveMode = m
        self._dirty = True

    def curveDegree(self) -> int:
        return self._curveDegree

    def setCurveDegree(self, d: int) -> None:
        self._curveDegree = d
        self._dirty = True

    def isClosed(self) -> bool:
        return self._isClosed

    def setClosed(self, c: bool) -> None:
        self._isClosed = c
        self._dirty = True

    def buildFromPoints(self, points: List[Point3D], degree: int = 3,
                        mode: CurveMode = CurveMode.FIT) -> bool:
        self._inputPoints = list(points)
        self._curveDegree = degree
        self._curveMode = mode
        return self._resample()

    def buildFromSampled(self, samples: List[Point3D],
                         inputPoints: Optional[List[Point3D]] = None) -> bool:
        """按已采样折线直接构建（不再二次样条重算），
        用于增量画曲线固化：结果与绘制预览完全一致。

        inputPoints 为编辑用的拟合点（曲线编辑显示的控制点），
        缺省退化为 samples（旧数据/两点直线场景）。"""
        self._inputPoints = list(inputPoints) if inputPoints else list(samples)
        self._sampledPoints = list(samples)
        self._dirty = False
        return len(samples) >= 2

    def _resample(self) -> bool:
        pts = self._inputPoints
        if self._isClosed and len(pts) >= 2 and pts[0] != pts[-1]:
            pts = pts + [pts[0]]
        if len(pts) < 2:
            self._sampledPoints = list(pts)
            return False
        try:
            if self._curveMode == CurveMode.CONTROL:
                # 控制多边形模式：B 样条不过控制点，按原模式重建
                from ..spline_utils import createControlPointBSpline
                self._sampledPoints = createControlPointBSpline(
                    pts, self._curveDegree, samples=100)
            else:
                # 与画曲线预览一致：cadcraft 式拟合点样条（NURBS 书第 9 章
                # 全局插值）——编辑控制点后曲线与描图预览同一形状风格
                from ..spline_utils import nurbs_fit_samples
                n = max(len(pts) - 1, 1)
                self._sampledPoints = nurbs_fit_samples(pts, max(20, 100 // n))
            return True
        except Exception:
            self._sampledPoints = list(pts)
            return False

    def getSampledPoints(self, samplesPerSeg: int = 100) -> List[Point3D]:
        if self._dirty or not self._sampledPoints:
            self._resample()
            self._dirty = False
        return self._sampledPoints

    def serialize(self) -> dict:
        d = super().serialize()
        d["inputPoints"] = [[p.x, p.y, p.z] for p in self._inputPoints]
        d["curveMode"] = self._curveMode.name if hasattr(self._curveMode, "name") else str(self._curveMode)
        d["curveDegree"] = self._curveDegree
        d["isClosed"] = self._isClosed
        return d

    def deserialize(self, data: dict) -> None:
        super().deserialize(data)
        self._inputPoints = [Point3D(*p) for p in data.get("inputPoints", [])]
        mode = data.get("curveMode", "FIT")
        try:
            self._curveMode = CurveMode[mode]
        except (KeyError, TypeError):
            self._curveMode = CurveMode.FIT
        self._curveDegree = int(data.get("curveDegree", 3))
        self._isClosed = bool(data.get("isClosed", False))
        self._resample()

    def translate(self, dx: float, dy: float, dz: float) -> None:
        self._inputPoints = [Point3D(p.x + dx, p.y + dy, p.z + dz) for p in self._inputPoints]
        self._dirty = True

    def rotate(self, angle: float, ax: float, ay: float, az: float) -> None:
        c, s = math.cos(angle), math.sin(angle)
        if (ax, ay, az) == (0, 0, 1):
            self._inputPoints = [Point3D(p.x * c - p.y * s, p.x * s + p.y * c, p.z)
                                 for p in self._inputPoints]
            self._dirty = True

    def scale(self, sx: float, sy: float, sz: float) -> None:
        self._inputPoints = [Point3D(p.x * sx, p.y * sy, p.z * sz) for p in self._inputPoints]
        self._dirty = True

    def getBoundingBox(self) -> BoundingBox3D:
        bb = BoundingBox3D()
        for p in self.getSampledPoints():
            bb.expand(p)
        return bb

    def getPosition(self) -> Point3D:
        return self._inputPoints[0] if self._inputPoints else Point3D()

    def clone(self) -> "CurveObject":
        o = CurveObject()
        o._inputPoints = [Point3D(p.x, p.y, p.z) for p in self._inputPoints]
        o._curveMode = self._curveMode
        o._curveDegree = self._curveDegree
        o._isClosed = self._isClosed
        o._color = self._color; o._lineWidth = self._lineWidth
        o._resample()
        return o

    def controlPointCount(self) -> int:
        return len(self._inputPoints)

    def getControlPoint(self, i: int) -> Point3D:
        return self._inputPoints[i]

    def setControlPoint(self, i: int, p: Point3D) -> None:
        if 0 <= i < len(self._inputPoints):
            self._inputPoints[i] = p
            self._dirty = True

    def getSupportedSnapTypes(self):
        types = [SnapType.ENDPOINT, SnapType.MIDPOINT, SnapType.NEAREST, SnapType.POINT]
        if self._isClosed:
            types.append(SnapType.CENTER)
        return types

    def pathLength(self) -> float:
        return polylineLength(self.getSampledPoints())

    def enclosedArea(self) -> float | None:
        """闭合贝塞尔面积；同时接受 isClosed flag=True 或「采样点几何首尾重合」两种闭合。"""
        pts = self.getSampledPoints()
        if len(pts) < 3:
            return None
        closed = bool(self._isClosed)
        if not closed:
            if pts[0].distanceTo(pts[-1]) < 1e-6:
                closed = True
        if not closed:
            return None
        if pts[0].distanceTo(pts[-1]) > 1e-12:
            pts = pts + [pts[0]]
        return closedArea2D(pts)
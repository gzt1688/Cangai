"""NURBS 曲线对象模型。"""
from __future__ import annotations
import math
from dataclasses import dataclass
from typing import List, Optional
from .base_object import BaseObject
from .geometry_types import (Point3D, BoundingBox3D, ObjectType,
                             polylineLength, closedArea2D)
from ..spline_utils import sampleNurbs
from ..snap.snap_types import SnapType


@dataclass
class ControlPoint:
    x: float
    y: float
    z: float
    weight: float = 1.0

    def toPoint3D(self) -> Point3D:
        return Point3D(self.x, self.y, self.z)


@dataclass
class NurbsSegment:
    controlPoints: List[ControlPoint]
    degree: int
    knotVector: List[float]


class NurbsCurveObject(BaseObject):
    def __init__(self) -> None:
        super().__init__(ObjectType.NURBS_CURVE)
        self._segments: List[NurbsSegment] = []
        self._closed: bool = False
        self._sampleCache: Optional[List[Point3D]] = None   # 采样缓存（内容变更即失效）
        self._sampleCachePerSeg: int = -1

    @staticmethod
    def create(degree: int, controlPoints: List[Point3D],
               weights: Optional[List[float]] = None, closed: bool = False) -> "NurbsCurveObject":
        obj = NurbsCurveObject()
        obj._closed = closed
        n = len(controlPoints)
        if n < 2:
            return obj
        k = min(degree, n - 1)
        knots: List[float] = [0.0] * (k + 1) + [float(i) for i in range(1, n - k)] + [float(n - k)] * (k + 1)
        cps = [ControlPoint(p.x, p.y, p.z, weights[i] if weights and i < len(weights) else 1.0)
               for i, p in enumerate(controlPoints)]
        obj._segments = [NurbsSegment(cps, k, knots)]
        return obj

    def segments(self) -> List[NurbsSegment]:
        return self._segments

    def isClosed(self) -> bool:
        return self._closed

    def setClosed(self, c: bool) -> None:
        self._closed = c

    def getSampledPoints(self, samplesPerSeg: int = 100) -> List[Point3D]:
        """采样折线（按内容缓存：包围盒/路径长度/图元重建每次调用不再全量重算）。"""
        if self._sampleCache is not None and self._sampleCachePerSeg == samplesPerSeg:
            return self._sampleCache
        out: List[Point3D] = []
        for seg in self._segments:
            cps = [cp.toPoint3D() for cp in seg.controlPoints]
            w = [cp.weight for cp in seg.controlPoints]
            out.extend(sampleNurbs(seg.degree, cps, w, seg.knotVector, samplesPerSeg))
        self._sampleCache = out
        self._sampleCachePerSeg = samplesPerSeg
        return out

    def _invalidateSampleCache(self) -> None:
        self._sampleCache = None
        self._sampleCachePerSeg = -1

    def translate(self, dx: float, dy: float, dz: float) -> None:
        for seg in self._segments:
            seg.controlPoints = [ControlPoint(cp.x + dx, cp.y + dy, cp.z + dz, cp.weight)
                                 for cp in seg.controlPoints]
        self._invalidateSampleCache()

    def rotate(self, angle: float, ax: float, ay: float, az: float) -> None:
        c, s = math.cos(angle), math.sin(angle)
        if (ax, ay, az) == (0, 0, 1):
            for seg in self._segments:
                seg.controlPoints = [ControlPoint(cp.x * c - cp.y * s, cp.x * s + cp.y * c, cp.z, cp.weight)
                                     for cp in seg.controlPoints]
            self._invalidateSampleCache()

    def scale(self, sx: float, sy: float, sz: float) -> None:
        for seg in self._segments:
            seg.controlPoints = [ControlPoint(cp.x * sx, cp.y * sy, cp.z * sz, cp.weight)
                                 for cp in seg.controlPoints]
        self._invalidateSampleCache()

    def getBoundingBox(self) -> BoundingBox3D:
        bb = BoundingBox3D()
        for p in self.getSampledPoints():
            bb.expand(p)
        return bb

    def getPosition(self) -> Point3D:
        if self._segments and self._segments[0].controlPoints:
            return self._segments[0].controlPoints[0].toPoint3D()
        return Point3D()

    def clone(self) -> "NurbsCurveObject":
        o = NurbsCurveObject()
        o._closed = self._closed
        o._segments = [NurbsSegment(
            [ControlPoint(cp.x, cp.y, cp.z, cp.weight) for cp in seg.controlPoints],
            seg.degree, list(seg.knotVector)) for seg in self._segments]
        o._color = self._color; o._lineWidth = self._lineWidth
        return o

    def getSupportedSnapTypes(self):
        types = [SnapType.ENDPOINT, SnapType.MIDPOINT, SnapType.NEAREST, SnapType.POINT]
        if self._closed:
            types.append(SnapType.CENTER)
        return types

    def pathLength(self) -> float:
        return polylineLength(self.getSampledPoints())

    def enclosedArea(self) -> float | None:
        """闭合 NURBS 面积；同时接受 closed flag=True 或「采样点几何首尾重合」两种闭合。"""
        pts = self.getSampledPoints()
        if len(pts) < 3:
            return None
        closed = bool(self._closed)
        if not closed:
            if pts[0].distanceTo(pts[-1]) < 1e-6:
                closed = True
        if not closed:
            return None
        if pts[0].distanceTo(pts[-1]) > 1e-12:
            pts = pts + [pts[0]]
        return closedArea2D(pts)
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
        # 贝塞尔手柄（点编辑控制柄）：每拟合点 [inx,iny,inz,outx,outy,outz]
        # 世界偏移，零向量=自动（切向取拟合样条节点导数）；None/全零=
        # 无手柄，走原全局 C2 拟合，旧数据零影响
        self._handles: Optional[List[List[float]]] = None
        # 自动手柄切向的样条节点导数缓存（键=闭合标志+全部拟合点坐标）
        self._derivCache = None
        # 度量缓存（键=采样列表身份：几何变化 → 采样列表整体重建 → 自动失效）。
        # 吸附粗筛/量测/选中框每个鼠标事件都会读取，全量扫描是卡顿主因。
        self._bbCache = None     # (samples, bbox)
        self._lenCache = None    # (samples, length)
        self._areaCache = None   # (samples, closed, area)

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
                # 手柄模式：逐区间三次 Hermite（贝塞尔手柄控制形状）
                if self.hasHandles() and len(pts) >= 2:
                    if self._isClosed and len(pts) >= 2 and pts[0] == pts[-1]:
                        pts = pts[:-1]
                    hs = self._ensureHandles()
                    h_in = [Point3D(h[0], h[1], h[2]) for h in hs[:len(pts)]]
                    h_out = [Point3D(h[3], h[4], h[5]) for h in hs[:len(pts)]]
                    from ..spline_utils import hermiteHandleSamples
                    n = max(len(pts) - 1, 1)
                    # 自动切向取样条节点导数 → 手柄模式与原拟合曲线同一条
                    self._sampledPoints = hermiteHandleSamples(
                        pts, h_in, h_out, self._isClosed and len(pts) >= 3,
                        max(20, 100 // n), self._sampledPoints,
                        self._autoDerivs())
                    return True
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
        if self.hasHandles():
            d["handles"] = [list(h) for h in self._handles]
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
        hs = data.get("handles")
        self._handles = [[float(v) for v in h] for h in hs] if hs else None
        self._resample()

    def translate(self, dx: float, dy: float, dz: float) -> None:
        self._inputPoints = [Point3D(p.x + dx, p.y + dy, p.z + dz) for p in self._inputPoints]
        self._dirty = True

    def rotate(self, angle: float, ax: float, ay: float, az: float) -> None:
        c, s = math.cos(angle), math.sin(angle)
        if (ax, ay, az) == (0, 0, 1):
            self._inputPoints = [Point3D(p.x * c - p.y * s, p.x * s + p.y * c, p.z)
                                 for p in self._inputPoints]
            self.transformHandles(lambda x, y, z: (x * c - y * s, x * s + y * c, z))
            self._dirty = True

    def scale(self, sx: float, sy: float, sz: float) -> None:
        self._inputPoints = [Point3D(p.x * sx, p.y * sy, p.z * sz) for p in self._inputPoints]
        self.transformHandles(lambda x, y, z: (x * sx, y * sy, z))
        self._dirty = True

    def getBoundingBox(self) -> BoundingBox3D:
        pts = self.getSampledPoints()
        c = self._bbCache
        if c is None or c[0] is not pts:
            bb = BoundingBox3D()
            for p in pts:
                bb.expand(p)
            self._bbCache = (pts, bb)
            return bb
        return c[1]

    def getPosition(self) -> Point3D:
        return self._inputPoints[0] if self._inputPoints else Point3D()

    def clone(self) -> "CurveObject":
        o = CurveObject()
        o._inputPoints = [Point3D(p.x, p.y, p.z) for p in self._inputPoints]
        o._curveMode = self._curveMode
        o._curveDegree = self._curveDegree
        o._isClosed = self._isClosed
        o._handles = [list(h) for h in self._handles] if self._handles else None
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

    # ---------------- 贝塞尔手柄（点编辑控制柄） ----------------

    def hasHandles(self) -> bool:
        return bool(self._handles) and any(any(h) for h in self._handles)

    def _autoDerivs(self) -> List[Point3D]:
        """拟合样条节点导数（自动手柄切向来源），按坐标键缓存
        （坐标一变即失效重解，O(n) 键构建 vs O(n³) 求解）。"""
        key = (self._isClosed,
               tuple((p.x, p.y, p.z) for p in self._inputPoints))
        c = self._derivCache
        if c is not None and c[0] == key:
            return c[1]
        from ..spline_utils import nurbs_fit_derivatives
        dr = nurbs_fit_derivatives(self._inputPoints, self._isClosed)
        self._derivCache = (key, dr)
        return dr

    def _ensureHandles(self) -> List[List[float]]:
        n = len(self._inputPoints)
        old = self._handles or []
        self._handles = [(list(old[i]) if i < len(old) and len(old[i]) == 6
                          else [0.0] * 6) for i in range(n)]
        return self._handles

    def getHandleOffset(self, i: int, side: str,
                        auto: bool = True) -> Optional[Point3D]:
        """第 i 控制点的手柄偏移（世界向量）；side 'in'/'out'。
        未拖过时返回自动估算（auto=False 则 None 表示未 materialize）；
        开放段首点无入侧、末点无出侧（闭合曲线两侧都有）。"""
        n = len(self._inputPoints)
        if n < 2 or not (0 <= i < n):
            return None
        closed = self._isClosed and n >= 3
        if not closed and ((side == "in" and i == 0)
                           or (side == "out" and i == n - 1)):
            return None
        hv = self._handles[i] if (self._handles and i < len(self._handles)) else None
        if hv is not None:
            k = 0 if side == "in" else 3
            if hv[k] or hv[k + 1] or hv[k + 2]:
                return Point3D(hv[k], hv[k + 1], hv[k + 2])
        if not auto:
            return None
        from ..spline_utils import autoHandleOffsets
        # 自动切向源 = 拟合样条节点导数（与画出的曲线同一条、同切向）
        pair = autoHandleOffsets(self._inputPoints, closed,
                                 self.getSampledPoints(),
                                 self._autoDerivs())[i]
        return pair[0] if side == "in" else pair[1]

    def setHandleOffset(self, i: int, side: str, off: Point3D) -> None:
        """materialize 一侧手柄；偏移回零侧回落自动，全零退回拟合表示。"""
        n = len(self._inputPoints)
        if n < 2 or not (0 <= i < n):
            return
        hs = self._ensureHandles()
        k = 0 if side == "in" else 3
        hs[i][k:k + 3] = [off.x, off.y, off.z]
        if not self.hasHandles():
            self._handles = None
        self._dirty = True

    def transformHandles(self, fn) -> None:
        """对已 materialize 的手柄偏移应用向量变换 fn(x,y,z)->(x,y,z)
        （gizmo 旋转/缩放选中控制点时手柄跟随）。"""
        if not self._handles:
            return
        for h in self._handles:
            for k in (0, 3):
                h[k], h[k + 1], h[k + 2] = fn(h[k], h[k + 1], h[k + 2])
        self._dirty = True

    def getSupportedSnapTypes(self):
        types = [SnapType.ENDPOINT, SnapType.MIDPOINT, SnapType.NEAREST, SnapType.POINT]
        if self._isClosed:
            types.append(SnapType.CENTER)
        return types

    def pathLength(self) -> float:
        pts = self.getSampledPoints()
        c = self._lenCache
        if c is None or c[0] is not pts:
            c = (pts, polylineLength(pts))
            self._lenCache = c
        return c[1]

    def enclosedArea(self) -> float | None:
        """闭合贝塞尔面积；同时接受 isClosed flag=True 或「采样点几何首尾重合」两种闭合。"""
        raw = self.getSampledPoints()
        if len(raw) < 3:
            return None
        closed = bool(self._isClosed)
        if not closed:
            if raw[0].distanceTo(raw[-1]) < 1e-6:
                closed = True
        if not closed:
            return None
        c = self._areaCache
        if c is None or c[0] is not raw:
            pts = raw if raw[0].distanceTo(raw[-1]) <= 1e-12 else raw + [raw[0]]
            c = (raw, closed, closedArea2D(pts))
            self._areaCache = c
        return c[2]
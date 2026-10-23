"""多段曲线对象模型（段数据驱动）。"""
from __future__ import annotations
from typing import List
from .base_object import BaseObject
from .geometry_types import (Point3D, BoundingBox3D, ObjectType,
                             polylineLength, closedArea2D)
from ..snap.snap_types import SnapType


class PolyCurveObject(BaseObject):
    def __init__(self) -> None:
        super().__init__(ObjectType.POLYCURVE)
        self._segments: List[dict] = []
        # 文字轮廓标记：文字命令创建的轮廓置 True（数据识别用）；
        # 文字轮廓仍是曲线对象，自动归入“曲线”固定层
        self.isText: bool = False
        # 采样缓存：捕捉在鼠标移动时对每对象每帧调 getSampledPoints 2-3 次
        # （MIDPOINT/NEAREST/交点），文字轮廓正是 PolyCurve，不缓存每帧全量重建
        self._sampledPts: List[Point3D] = []
        self._dirty = True

    def segments(self) -> List[dict]:
        return self._segments

    def addSegment(self, seg: dict) -> None:
        self._segments.append(seg)
        self._dirty = True

    def _transformPoints(self, key: str, fn) -> None:
        """对每段指定点集（points/inputPoints）应用变换 fn。"""
        for seg in self._segments:
            if key in seg:
                seg[key] = [fn(p) for p in seg[key]]
        self._dirty = True

    def translate(self, dx: float, dy: float, dz: float) -> None:
        fn = lambda p: [p[0] + dx, p[1] + dy, p[2] + dz]
        # 外观采样点与控制点（inputPoints）同步平移，避免控制点留在原地
        self._transformPoints("points", fn)
        self._transformPoints("inputPoints", fn)

    def rotate(self, angle: float, ax: float, ay: float, az: float) -> None:
        import math
        c, s = math.cos(angle), math.sin(angle)
        if (ax, ay, az) == (0, 0, 1):
            fn = lambda p: [p[0] * c - p[1] * s, p[0] * s + p[1] * c, p[2]]
            self._transformPoints("points", fn)
            self._transformPoints("inputPoints", fn)

    def scale(self, sx: float, sy: float, sz: float) -> None:
        fn = lambda p: [p[0] * sx, p[1] * sy, p[2] * sz]
        self._transformPoints("points", fn)
        self._transformPoints("inputPoints", fn)

    def getBoundingBox(self) -> BoundingBox3D:
        bb = BoundingBox3D()
        for seg in self._segments:
            for p in seg.get("points", []):
                bb.expand(Point3D(p[0], p[1], p[2]))
        return bb

    def getPosition(self) -> Point3D:
        if self._segments and self._segments[0].get("points"):
            p = self._segments[0]["points"][0]
            return Point3D(p[0], p[1], p[2])
        return Point3D()

    @staticmethod
    def _copySeg(s: dict) -> dict:
        c = {"points": [list(p) for p in s.get("points", [])]}
        if s.get("inputPoints"):
            c["inputPoints"] = [list(p) for p in s["inputPoints"]]
        return c

    def clone(self) -> "PolyCurveObject":
        o = PolyCurveObject()
        o._segments = [self._copySeg(s) for s in self._segments]
        o._color = self._color; o._lineWidth = self._lineWidth
        o.isText = self.isText
        return o

    def serialize(self) -> dict:
        d = super().serialize()
        d["segments"] = [self._copySeg(s) for s in self._segments]
        if self.isText:
            d["isText"] = True
        return d

    def deserialize(self, data: dict) -> None:
        super().deserialize(data)
        self.isText = bool(data.get("isText", False))
        self._segments = []
        for s in data.get("segments", []):
            self._segments.append(self._copySeg(s))
        self._dirty = True

    def _segmentControlPoints_(self, i: int) -> List[Point3D]:
        """第 i 段对外可见控制点：首段含首点，后续段跳过与前段共享的断开点。"""
        cps = self._segmentControlPoints(self._segments[i])
        if i > 0 and cps:
            return cps[1:]
        return cps

    def controlPointCount(self) -> int:
        n = 0
        for i in range(len(self._segments)):
            n += len(self._segmentControlPoints_(i))
        return n

    @staticmethod
    def _segmentControlPoints(seg: dict) -> List[Point3D]:
        """该段编辑控制点：优先点击点（inputPoints），
        旧数据缺省时退化为段首尾点（避免把采样折线当控制点）。"""
        inp = seg.get("inputPoints")
        if inp:
            return [Point3D(p[0], p[1], p[2]) for p in inp]
        pts = seg.get("points", [])
        if not pts:
            return []
        if len(pts) == 1:
            return [Point3D(pts[0][0], pts[0][1], pts[0][2])]
        return [Point3D(pts[0][0], pts[0][1], pts[0][2]),
                Point3D(pts[-1][0], pts[-1][1], pts[-1][2])]

    def getControlPoint(self, i: int) -> Point3D:
        for si in range(len(self._segments)):
            cps = self._segmentControlPoints_(si)
            if i < len(cps):
                return cps[i]
            i -= len(cps)
        return Point3D()

    def setControlPoint(self, i: int, p: Point3D) -> None:
        for si, seg in enumerate(self._segments):
            cps = self._segmentControlPoints_(si)
            n = len(cps)
            if i >= n:
                i -= n
                continue
            # 对外索引 → 段内实际索引（后段跳过共享首点）
            skip = 1 if (si > 0 and self._segmentControlPoints(seg)) else 0
            self._setSegmentPoint(si, i + skip, p)
            return

    def _setSegmentPoint(self, si: int, local: int, p: Point3D) -> None:
        """修改第 si 段第 local 个控制点（段内实际索引，含共享首点）；
        端点跨段共享时同步相邻段。"""
        seg = self._segments[si]
        n = len(self._segmentControlPoints(seg))
        inp = seg.get("inputPoints")
        if inp:
            inp[local] = [p.x, p.y, p.z]
            # 断开点共享：前段末点 / 后段首点一并更新
            if local == 0 and si > 0 and self._segments[si - 1].get("inputPoints"):
                self._segments[si - 1]["inputPoints"][-1] = [p.x, p.y, p.z]
            if local == n - 1 and si < len(self._segments) - 1 and \
                    self._segments[si + 1].get("inputPoints"):
                self._segments[si + 1]["inputPoints"][0] = [p.x, p.y, p.z]
        else:
            pts = seg["points"]
            if len(pts) == 1:
                pts[0] = [p.x, p.y, p.z]
            else:
                pts[0 if local == 0 else -1] = [p.x, p.y, p.z]
            if local == 0 and si > 0 and len(self._segments[si - 1]["points"]) > 1:
                self._segments[si - 1]["points"][-1] = [p.x, p.y, p.z]
            if local == n - 1 and si < len(self._segments) - 1 and \
                    len(self._segments[si + 1]["points"]) > 1:
                self._segments[si + 1]["points"][0] = [p.x, p.y, p.z]
        self._rebuildSegment(si)
        if local == 0 and si > 0:
            self._rebuildSegment(si - 1)
        if local == n - 1 and si < len(self._segments) - 1:
            self._rebuildSegment(si + 1)
        self._dirty = True

    def _rebuildSegment(self, si: int) -> None:
        """按该段控制点重采样外观（cadcraft 式拟合点样条，与画曲线预览
        同一套数学）。旧数据无 inputPoints 时以当前折线为锚重建。"""
        seg = self._segments[si]
        inp = seg.get("inputPoints")
        if inp:
            cps = [Point3D(p[0], p[1], p[2]) for p in inp]
            if len(cps) >= 3:
                from core.spline_utils import nurbs_fit_samples
                seg["points"] = [[p.x, p.y, p.z] for p in nurbs_fit_samples(cps, 20)]
            else:
                seg["points"] = [[p.x, p.y, p.z] for p in cps]
            return
        pts = [Point3D(p[0], p[1], p[2]) for p in seg.get("points", [])]
        if len(pts) >= 3:
            from core.spline_utils import nurbs_fit_samples
            seg["points"] = [[p.x, p.y, p.z] for p in nurbs_fit_samples(pts, 20)]

    def pathLength(self) -> float:
        total = 0.0
        for seg in self._segments:
            total += polylineLength([Point3D(*p) for p in seg.get("points", [])])
        return total

    def enclosedArea(self) -> float | None:
        all_pts: List[Point3D] = []
        for seg in self._segments:
            all_pts.extend([Point3D(*p) for p in seg.get("points", [])])
        if len(all_pts) < 3:
            return None
        if all_pts[0].distanceTo(all_pts[-1]) > 1e-12:
            return None
        return closedArea2D(all_pts)

    def getSupportedSnapTypes(self):
        return [SnapType.ENDPOINT, SnapType.MIDPOINT, SnapType.NEAREST,
                SnapType.POINT, SnapType.VERTEX]

    def getSampledPoints(self) -> List[Point3D]:
        """所有段采样点顺序拼接（供捕捉最近点用），带脏标记缓存。"""
        if self._dirty or not self._sampledPts:
            out: List[Point3D] = []
            for seg in self._segments:
                out.extend([Point3D(p[0], p[1], p[2]) for p in seg.get("points", [])])
            self._sampledPts = out
            self._dirty = False
        return self._sampledPts

    def points(self) -> List[Point3D]:
        """所有段采样点（供框选顶点判定用，与 LineObject.points() 对齐）。"""
        return self.getSampledPoints()
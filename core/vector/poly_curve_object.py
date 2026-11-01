"""多段曲线对象模型（段数据驱动）。"""
from __future__ import annotations
from typing import List, Optional
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
        # 度量缓存：键=各段 points 列表身份元组（变换/编辑都会整体重建段点
        # 列表，身份变化即失效）。吸附粗筛/量测每个鼠标事件都会读取。
        self._bbCache = None     # (segKey, bbox)
        self._lenCache = None    # (segKey, length)
        self._areaCache = None   # (segKey, area)

    def _segKey(self) -> tuple:
        """段点列表身份元组：段数或任一段点列表重建都会改变键值。"""
        return tuple(id(seg.get("points", ())) for seg in self._segments)

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
            self.transformHandles(lambda x, y, z: (x * c - y * s,
                                                   x * s + y * c, z))

    def scale(self, sx: float, sy: float, sz: float) -> None:
        fn = lambda p: [p[0] * sx, p[1] * sy, p[2] * sz]
        self._transformPoints("points", fn)
        self._transformPoints("inputPoints", fn)
        self.transformHandles(lambda x, y, z: (x * sx, y * sy, z))

    def getBoundingBox(self) -> BoundingBox3D:
        key = self._segKey()
        c = self._bbCache
        if c is None or c[0] != key:
            bb = BoundingBox3D()
            for seg in self._segments:
                for p in seg.get("points", []):
                    bb.expand(Point3D(p[0], p[1], p[2]))
            self._bbCache = (key, bb)
            return bb
        return c[1]

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
        if s.get("handles"):
            c["handles"] = [list(h) for h in s["handles"]]
        if s.get("type"):
            c["type"] = s["type"]
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

    # ---------------- 贝塞尔手柄（点编辑控制柄） ----------------

    def _locateControl(self, i: int) -> Optional[tuple]:
        """可见控制点索引 → (段号, 段内实际索引)；越界返回 None。"""
        for si, seg in enumerate(self._segments):
            cps = self._segmentControlPoints_(si)
            n = len(cps)
            if i < n:
                skip = 1 if (si > 0 and self._segmentControlPoints(seg)) else 0
                return si, i + skip
            i -= n
        return None

    def _locateHandle(self, i: int, side: str) -> Optional[tuple]:
        """可见控制点 + 侧（'in'/'out'）→ 拥有该手柄的 (段号, 段内索引)。
        断开共享锚点：末点的出侧归下段首点所有（两侧手柄独立，
        恰好构成锚点左右各一个柄）；开放曲线两端外侧无手柄。"""
        loc = self._locateControl(i)
        if loc is None:
            return None
        si, local = loc
        if side == "in" and local == 0:
            return None
        n = len(self._segmentControlPoints(self._segments[si]))
        if side == "out" and local >= n - 1:
            if si + 1 < len(self._segments):
                return si + 1, 0
            return None
        return si, local

    def _segmentDerivs(self, si: int) -> List[Point3D]:
        """该段拟合样条节点导数（自动手柄切向来源）。
        缓存存段字典 "_derivs"（键=全部拟合点坐标，坐标一变即失效重解）；
        _copySeg 白名单拷贝 → 不进序列化。"""
        seg = self._segments[si]
        inp = seg.get("inputPoints")
        if not inp:
            return []
        key = tuple((p[0], p[1], p[2]) for p in inp)
        c = seg.get("_derivs")
        if c is not None and c[0] == key:
            return c[1]
        from core.spline_utils import nurbs_fit_derivatives
        dr = nurbs_fit_derivatives([Point3D(p[0], p[1], p[2]) for p in inp],
                                   False)
        seg["_derivs"] = (key, dr)
        return dr

    def _segmentHandleOffset(self, si: int, local: int, side: str,
                             auto: bool) -> Optional[Point3D]:
        seg = self._segments[si]
        inp = seg.get("inputPoints")
        if not inp:
            return None
        hv = seg.get("handles")
        hv = hv[local] if (hv and local < len(hv) and len(hv[local]) == 6) else None
        if hv is not None:
            k = 0 if side == "in" else 3
            if hv[k] or hv[k + 1] or hv[k + 2]:
                return Point3D(hv[k], hv[k + 1], hv[k + 2])
        if not auto:
            return None
        from core.spline_utils import autoHandleOffsets
        pts = [Point3D(p[0], p[1], p[2]) for p in inp]
        # 自动切向源 = 该段样条节点导数（与画出的曲线同一条、同切向）
        pair = autoHandleOffsets(pts, False, seg.get("points") or None,
                                 self._segmentDerivs(si))[local]
        return pair[0] if side == "in" else pair[1]

    def getHandleOffset(self, i: int, side: str,
                        auto: bool = True) -> Optional[Point3D]:
        """第 i 可见控制点的手柄偏移（世界向量）；未拖过时返回自动估算
        （auto=False 则 None = 未 materialize）。"""
        loc = self._locateHandle(i, side)
        if loc is None:
            return None
        return self._segmentHandleOffset(loc[0], loc[1], side, auto)

    def setHandleOffset(self, i: int, side: str, off: Point3D) -> None:
        """materialize 一侧手柄；全零回落为无手柄（恢复拟合表示）。"""
        loc = self._locateHandle(i, side)
        if loc is None:
            return
        si, local = loc
        seg = self._segments[si]
        inp = seg.get("inputPoints")
        if not inp:
            return
        hs = seg.get("handles")
        if hs is None or len(hs) != len(inp):
            old = hs or []
            hs = [(list(old[k]) if k < len(old) and len(old[k]) == 6
                   else [0.0] * 6) for k in range(len(inp))]
        k = 0 if side == "in" else 3
        hs[local][k:k + 3] = [off.x, off.y, off.z]
        if any(any(h) for h in hs):
            seg["handles"] = hs
        else:
            seg.pop("handles", None)
        self._rebuildSegment(si)
        self._dirty = True

    def transformHandles(self, fn) -> None:
        """对已 materialize 的手柄偏移应用向量变换 fn(x,y,z)->(x,y,z)
        （gizmo 旋转/缩放选中控制点时手柄跟随）。"""
        for seg in self._segments:
            hs = seg.get("handles")
            if not hs:
                continue
            for h in hs:
                for k in (0, 3):
                    h[k], h[k + 1], h[k + 2] = fn(h[k], h[k + 1], h[k + 2])
            self._dirty = True

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
        同一套数学）。旧数据无 inputPoints 时以当前折线为锚重建。
        有贝塞尔手柄时改走逐区间三次 Hermite（手柄控制形状）。"""
        seg = self._segments[si]
        inp = seg.get("inputPoints")
        if inp:
            cps = [Point3D(p[0], p[1], p[2]) for p in inp]
            hs = seg.get("handles")
            if (hs and len(hs) == len(cps) and len(cps) >= 2
                    and any(any(h) for h in hs)):
                from core.spline_utils import hermiteHandleSamples
                h_in = [Point3D(h[0], h[1], h[2]) for h in hs]
                h_out = [Point3D(h[3], h[4], h[5]) for h in hs]
                # 自动切向取该段样条节点导数 → 手柄模式与原拟合曲线同一条
                seg["points"] = [[p.x, p.y, p.z]
                                 for p in hermiteHandleSamples(
                                     cps, h_in, h_out, False, 20,
                                     seg.get("points") or None,
                                     self._segmentDerivs(si))]
                return
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
        key = self._segKey()
        c = self._lenCache
        if c is None or c[0] != key:
            total = 0.0
            for seg in self._segments:
                total += polylineLength([Point3D(*p) for p in seg.get("points", [])])
            c = (key, total)
            self._lenCache = c
        return c[1]

    def enclosedArea(self) -> float | None:
        key = self._segKey()
        c = self._areaCache
        if c is not None and c[0] == key:
            return c[1]
        all_pts: List[Point3D] = []
        for seg in self._segments:
            all_pts.extend([Point3D(*p) for p in seg.get("points", [])])
        if len(all_pts) < 3:
            area = None
        elif all_pts[0].distanceTo(all_pts[-1]) > 1e-12:
            area = None
        else:
            area = closedArea2D(all_pts)
        self._areaCache = (key, area)
        return area

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
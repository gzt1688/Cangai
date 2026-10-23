"""修订云线对象（REVCLOUD 复刻）：带凸弧的闭合多段线。

几何模型与 cadcraft/AutoCAD 一致：沿边界多边形按目标弧长分点，相邻
分点之间以 bulge=0.5 的圆弧（约 53°）相连，弧凸向外；边界顶点只存
分点列，弧的圆心/半径由相邻分点的距离动态推出——平移/缩放后弧自然
自适应，无需额外参数。"""
from __future__ import annotations
import math
from typing import List
from PySide6.QtGui import QPainterPath

from .base_object import BaseObject
from .geometry_types import Point3D, BoundingBox3D, ObjectType


def _arc_for_chord(a: Point3D, b: Point3D, bulge: float,
                   samples: int = 12) -> List[Point3D]:
    """弦 a→b、凸度 bulge 的圆弧采样点（不含 a，含 b）。

    DXF 标准约定：bulge = tan(θ/4)（θ 为圆心角，有符号）。0.5 →
    θ ≈ 106.26°；圆心在弦中点沿有符号法线偏移 d/2/tan(θ/2) 处，
    从 a 绕圆心扫 θ 到 b。"""
    d = math.hypot(b.x - a.x, b.y - a.y)
    if d < 1e-12:
        return [b]
    theta = 4.0 * math.atan(bulge)
    r = d / (2.0 * math.sin(abs(theta) / 2.0))
    mx, my = (a.x + b.x) / 2.0, (a.y + b.y) / 2.0
    nx, ny = -(b.y - a.y) / d, (b.x - a.x) / d   # 法线
    off = (d / 2.0) / math.tan(theta / 2.0)       # DXF 有符号偏移（凸向外）
    ox, oy = mx + nx * off, my + ny * off
    a0 = math.atan2(a.y - oy, a.x - ox)
    pts: List[Point3D] = []
    for i in range(1, samples + 1):
        ang = a0 + theta * i / samples
        pts.append(Point3D(ox + r * math.cos(ang), oy + r * math.sin(ang), 0.0))
    return pts


class RevcloudObject(BaseObject):
    def __init__(self, vertices: List[Point3D] | None = None,
                 arc_len: float = 0.0, style: str = 'normal') -> None:
        super().__init__(ObjectType.REVCLOUD)
        self._vertices: List[Point3D] = list(vertices) if vertices else []
        self._arcLen = float(arc_len)
        self._style = style   # normal / calligraphy（书法笔触预留）
        self._bulge = 0.5     # 云弧凸度（DXF 约定：tan(θ/4)，0.5≈53°弧）

    def vertices(self) -> List[Point3D]:
        return self._vertices

    def setVertices(self, vs: List[Point3D]) -> None:
        self._vertices = list(vs)

    def arcLength(self) -> float:
        return self._arcLen

    def setArcLength(self, v: float) -> None:
        self._arcLen = float(v)

    def style(self) -> str:
        return self._style

    def isEmpty(self) -> bool:
        return len(self._vertices) < 3

    def path(self) -> QPainterPath:
        """云线路径：相邻分点间以 bulge=0.5 凸弧相连（采样折线）。"""
        path = QPainterPath()
        vs = self._vertices
        if len(vs) < 3:
            return path
        path.moveTo(vs[0].x, vs[0].y)
        for i in range(len(vs)):
            a = vs[i]
            b = vs[(i + 1) % len(vs)]
            for p in _arc_for_chord(a, b, self._bulge):
                path.lineTo(p.x, p.y)
        path.closeSubpath()
        return path

    def serialize(self) -> dict:
        d = super().serialize()
        d["vertices"] = [[p.x, p.y, p.z] for p in self._vertices]
        d["arcLength"] = self._arcLen
        d["style"] = self._style
        return d

    def deserialize(self, data: dict) -> None:
        super().deserialize(data)
        self._vertices = [Point3D(*p) for p in data.get("vertices", [])]
        self._arcLen = float(data.get("arcLength", 0.0))
        self._style = str(data.get("style", "normal"))

    def translate(self, dx: float, dy: float, dz: float = 0.0) -> None:
        self._vertices = [Point3D(p.x + dx, p.y + dy, p.z + dz)
                          for p in self._vertices]

    def rotate(self, angle: float, ax: float = 0.0, ay: float = 0.0,
               az: float = 1.0, cx: float = 0.0, cy: float = 0.0) -> None:
        c, s = math.cos(angle), math.sin(angle)
        self._vertices = [
            Point3D(cx + (p.x - cx) * c - (p.y - cy) * s,
                    cy + (p.x - cx) * s + (p.y - cy) * c, p.z)
            for p in self._vertices]

    def scale(self, sx: float, sy: float, sz: float = 1.0) -> None:
        self._vertices = [Point3D(p.x * sx, p.y * sy, p.z * sz)
                          for p in self._vertices]

    def getBoundingBox(self) -> BoundingBox3D:
        bb = BoundingBox3D()
        for p in self._arc_points():
            bb.expand(p)
        return bb

    def _arc_points(self) -> List[Point3D]:
        vs = self._vertices
        all_pts: List[Point3D] = []
        for i in range(len(vs)):
            a, b = vs[i], vs[(i + 1) % len(vs)]
            all_pts.append(a)
            all_pts.extend(_arc_for_chord(a, b, self._bulge))
        return all_pts

    def getPosition(self) -> Point3D:
        if not self._vertices:
            return Point3D()
        n = len(self._vertices)
        return Point3D(sum(p.x for p in self._vertices) / n,
                       sum(p.y for p in self._vertices) / n, 0.0)

    def clone(self) -> "RevcloudObject":
        o = RevcloudObject([Point3D(p.x, p.y, p.z) for p in self._vertices],
                           self._arcLen, self._style)
        o._color = self._color
        o._lineWidth = self._lineWidth
        return o

    def controlPointCount(self) -> int:
        return len(self._vertices)

    def getControlPoint(self, i: int) -> Point3D:
        return self._vertices[i % len(self._vertices)]

    def setControlPoint(self, i: int, p: Point3D) -> None:
        if self._vertices:
            self._vertices[i % len(self._vertices)] = p

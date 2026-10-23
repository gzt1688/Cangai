# -*- coding: utf-8 -*-
"""月饼/弹簧饼干模具几何助手（私有插件，移植自 Rhino 插件 Model）。

- offsetRingOutward：闭合轮廓向外偏移（与 OffsetCommand 同一套偏移内核，
  朝向自动校验=偏移后面积必须增大）
- mooncakeFrameRing：月饼/饼干通用的"镂空框"固定轮廓（8 段直线+圆弧，
  数据与 Rhino MooncakeMoldFunctions/SpringCookieFunctions 完全一致）
- buildBevelLipSolid：顶部斜口圈实心（对应 Rhino 饼干拉伸的闭合放样
  c1(middle@lo)→c3(middle@hi)→c2(outer@lo)，纯拓扑 Sewing 成实心零布尔）

输入输出均为世界坐标 Point3D 水平环（不依赖 ui/Qt，可独立测试）。
"""
from __future__ import annotations

import math
from typing import List, Optional

from core.vector.geometry_types import Point3D
from core.vector.offset_geometry import offsetPolyline, offsetSampledCurve

# 镂空框外接尺寸（用于从一组曲线中识别框；Rhino 固定数据 31×27）
FRAME_W, FRAME_H = 31.0, 27.0
_FRAME_TOL = 1.0


# ---------------------------------------------------------------- 环基础 ----

def ringSignedArea(ring: List[Point3D]) -> float:
    """XY 鞋带面积（带符号：CCW 为正）。"""
    s = 0.0
    n = len(ring)
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        s += a.x * b.y - b.x * a.y
    return s * 0.5


def stripClosing(ring: List[Point3D]) -> List[Point3D]:
    """去掉首尾重合闭合点（采样环常带闭合重复点）。"""
    if len(ring) >= 2 and ring[0].distanceTo(ring[-1]) < 1e-9:
        return ring[:-1]
    return list(ring)


def ensureCCW(ring: List[Point3D]) -> List[Point3D]:
    """环统一为逆时针（MakeFace 外环方向要求）。"""
    return ring if ringSignedArea(ring) >= 0 else list(reversed(ring))


def ringBBox(ring: List[Point3D]):
    xs = [p.x for p in ring]
    ys = [p.y for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


def capRing(ring: List[Point3D], max_n: int) -> List[Point3D]:
    """点数超限时等间隔抽稀（保持闭合环形状，不超限原样返回）。"""
    n = len(ring)
    if n <= max_n:
        return ring
    step = n / max_n
    return [ring[min(n - 1, int(i * step))] for i in range(max_n)]


def isFrameRing(ring: List[Point3D]) -> bool:
    """是否镂空框：外接尺寸 ≈ 31×27（固定模板轮廓的几何指纹）。"""
    x0, y0, x1, y1 = ringBBox(ring)
    return (abs((x1 - x0) - FRAME_W) <= _FRAME_TOL
            and abs((y1 - y0) - FRAME_H) <= _FRAME_TOL)


# ---------------------------------------------------------------- 偏移 ----

def offsetRingOutward(ring: List[Point3D], dist: float,
                      sharp: bool = False) -> Optional[List[Point3D]]:
    """闭合环向外偏移 dist（返回不含闭合重复点的环）。

    sharp=True 用折线偏移保留尖角（直线类），否则法线平滑偏移
    （与 OffsetCommand 的分支一致）。方向自动校验：偏移后面积
    必须增大，否则翻向重算（不依赖环的绕向约定）。
    """
    ring = stripClosing(ring)
    if len(ring) < 3 or abs(dist) < 1e-9:
        return None
    z = ring[0].z
    a0 = abs(ringSignedArea(ring))
    if a0 < 1e-9:
        return None

    def _one(d: float) -> List[Point3D]:
        if sharp:
            return offsetPolyline(ring, d, True)
        return offsetSampledCurve(ring, d, True)

    out = _one(dist)
    if len(out) < 3:
        return None
    if abs(ringSignedArea(out)) < a0:
        out = _one(-dist)
        if len(out) < 3 or abs(ringSignedArea(out)) < a0:
            return None
    # Z 与源环保持一致
    return [Point3D(p.x, p.y, z) for p in out]


# ------------------------------------------------------ 镂空框（8 段固定轮廓） ----

# 与 Rhino MooncakeMoldFunctions 逐段一致：直线 (起点,终点)、
# 圆弧 (起点, 过弧点, 终点)——三点定弧，整体为 31×27 圆角跑道形
_FRAME_SEGS = [
    ("L", (-13.2, 8.05), (-11.2, 8.05)),
    ("A", (-11.2, 8.05), (0.0, 13.5), (11.2, 8.05)),
    ("L", (11.2, 8.05), (13.2, 8.05)),
    ("A", (13.2, 8.05), (15.5, 0.0), (13.2, -8.05)),
    ("L", (13.2, -8.05), (11.2, -8.05)),
    ("A", (11.2, -8.05), (0.0, -13.5), (-11.2, -8.05)),
    ("L", (-11.2, -8.05), (-13.2, -8.05)),
    ("A", (-13.2, -8.05), (-15.5, 0.0), (-13.2, 8.05)),
]


def _arcRing(p1, pm, p2, z: float, cx: float, cy: float,
             n: int = 48) -> List[Point3D]:
    """过三点的圆弧采样环（含首末点；n 段）。"""
    ax, ay = p1
    bx, by = pm
    cx3, cy3 = p2
    d = 2.0 * (ax * (by - cy3) + bx * (cy3 - ay) + cx3 * (ay - by))
    if abs(d) < 1e-12:
        return [Point3D(ax + cx, ay + cy, z), Point3D(bx + cx, by + cy, z)]
    aa = ax * ax + ay * ay
    bb = bx * bx + by * by
    cc = cx3 * cx3 + cy3 * cy3
    ux = (aa * (by - cy3) + bb * (cy3 - ay) + cc * (ay - by)) / d
    uy = (aa * (cx3 - bx) + bb * (ax - cx3) + cc * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a1 = math.atan2(ay - uy, ax - ux)
    am = math.atan2(by - uy, bx - ux)
    a2 = math.atan2(cy3 - uy, cx3 - ux)
    d_ccw = (a2 - a1) % (2 * math.pi)
    pm_ccw = (am - a1) % (2 * math.pi)
    if pm_ccw <= d_ccw:
        s, total = 1.0, d_ccw
    else:
        s, total = -1.0, 2 * math.pi - d_ccw
    out = []
    for i in range(n + 1):
        ang = a1 + s * total * i / n
        out.append(Point3D(ux + r * math.cos(ang) + cx,
                           uy + r * math.sin(ang) + cy, z))
    return out


def mooncakeFrameRing(z: float, cx: float = 0.0, cy: float = 0.0,
                      arc_pts: int = 48) -> List[Point3D]:
    """镂空框采样环（平移到 (cx,cy)，不闭合重复点；隐式闭合）。"""
    ring: List[Point3D] = []
    for seg in _FRAME_SEGS:
        if seg[0] == "L":
            (x1, y1), _ = seg[1], seg[2]
            ring.append(Point3D(x1 + cx, y1 + cy, z))
        else:
            pts = _arcRing(seg[1], seg[2], seg[3], z, cx, cy, arc_pts)
            ring.extend(pts[:-1])   # 末点与下一段起点重合，丢弃
    return ring


# ---------------------------------------------------------------- 斜口放样 ----

def _ringWire(ring: List[Point3D], z: float):
    """环 → OCC 闭合线框（与 cutter_builder._ringWire 同源，直线边）。"""
    from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakeEdge,
                                    BRepBuilderAPI_MakeWire)
    from OCP.gp import gp_Pnt
    mw = BRepBuilderAPI_MakeWire()
    n = len(ring)
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        me = BRepBuilderAPI_MakeEdge(gp_Pnt(a.x, a.y, z),
                                     gp_Pnt(b.x, b.y, z))
        mw.Add(me.Edge())
    return mw.Wire()


def _stripFace(r1: List[Point3D], z1: float, r2: List[Point3D], z2: float):
    """两环间直纹面（ThruSections ruled）。"""
    from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections
    ts = BRepOffsetAPI_ThruSections(False, True, 1e-6)
    ts.AddWire(_ringWire(ensureCCW(r1), z1))
    ts.AddWire(_ringWire(ensureCCW(r2), z2))
    ts.Build()
    if not ts.IsDone():
        raise ValueError("斜口条带放样失败")
    return ts.Shape()


def buildBevelLipSolid(outer: List[Point3D], inner: List[Point3D],
                       z_lo: float, z_hi: float):
    """顶部斜口圈实心（Rhino 闭合放样 c1→c3→c2 的等价体）。

    outer/inner 为同一水平面（z_lo）上的外/内环；内环自 z_lo 竖直
    上到 z_hi 再锥形张开落到外环 z_lo——封闭壳：内柱面 + 斜锥面 +
    底面带孔环。对应 Rhino：c1=middle@(z+38)、c3=c1@(z+42)、
    c2=outer@(z+38)，LoftType.Straight 闭合放样。
    """
    from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakeFace,
                                    BRepBuilderAPI_Sewing)
    from OCP.ShapeFix import ShapeFix_Solid
    from OCP.TopoDS import TopoDS
    from OCP.TopAbs import TopAbs_ShapeEnum

    inner = stripClosing(inner)
    outer = stripClosing(outer)
    if len(inner) < 3 or len(outer) < 3:
        raise ValueError("斜口环点数不足")

    faces = [
        _stripFace(inner, z_lo, inner, z_hi),        # 内柱面
        _stripFace(inner, z_hi, outer, z_lo),        # 斜锥面
    ]
    of = ensureCCW(outer)
    inf_ = ensureCCW(inner)
    mf = BRepBuilderAPI_MakeFace(_ringWire(of, z_lo), True)
    # 内孔线框须与外环反向（外 CCW/内 CW）才被认作孔
    mf.Add(_ringWire(list(reversed(inf_)), z_lo))
    faces.append(mf.Face())                          # 底面带孔环

    sew = BRepBuilderAPI_Sewing(1e-4, True, True, True, False)
    for f in faces:
        sew.Add(f)
    sew.Perform()
    sewed = sew.SewedShape()

    st = sewed.ShapeType()
    if st == TopAbs_ShapeEnum.TopAbs_SOLID:
        solid = TopoDS.Solid_s(sewed)
    elif st == TopAbs_ShapeEnum.TopAbs_SHELL:
        solid = ShapeFix_Solid().SolidFromShell(TopoDS.Shell_s(sewed))
    else:
        from OCP.TopExp import TopExp_Explorer
        best, best_n = None, -1
        ex = TopExp_Explorer(sewed, TopAbs_ShapeEnum.TopAbs_SHELL)
        while ex.More():
            sh = TopoDS.Shell_s(ex.Current())
            nf = 0
            ex2 = TopExp_Explorer(sh, TopAbs_ShapeEnum.TopAbs_FACE)
            while ex2.More():
                nf += 1
                ex2.Next()
            if nf > best_n:
                best, best_n = sh, nf
            ex.Next()
        if best is None:
            raise ValueError("斜口缝合未产生壳")
        solid = ShapeFix_Solid().SolidFromShell(best)
    if solid is None or solid.IsNull():
        raise ValueError("斜口实体构建失败")
    from core.vector.brep_shape import _import_bd
    return _import_bd().Solid(solid)

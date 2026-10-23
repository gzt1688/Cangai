# -*- coding: utf-8 -*-
"""饼干外切模几何生成器（私有插件，不随软件分发）。

参考《Rhino 的外切模板参考.md》：闭合曲线（饼干外形）向外构建切模。

结构（z 向上，生成曲线=原曲线向外偏移 gap 缝隙）：
    腔体内壁 = 生成曲线（饼干与模腔的间隙 = gap）
    刀口唇   生成曲线 → +wall              高 h_blade（薄壁，切割刃）
    主体外圈 生成曲线 → +wall+band          高 h_body
    底部裙边 生成曲线 → +wall+band+flange   高 h_flange
    顶部斜面 a2 →(按角度内缩)→ 唇外圈，位于 h_body → h_blade

**纯拓扑构造，零布尔**：此 OCCT（7.9.3 novtk）对大多边形 BOPAlgo
又慢又不稳定（融合数秒、切割会静默产出错误实体、锥度挤出/放样楔
间歇崩溃），故实体由 7 张面直接构造并缝合：
    底面环(a3−cav)、a3 柱面、裙边顶环(a3−a2)、a2 柱面、
    斜面条带(a2→top)、顶面环(top−cav)、腔壁条带(cav)
条带 = ThruSections（每对对应边一张精确直纹四边形）；平面环 =
MakeFace+反向内孔线框（内孔必须与外环反向，否则孔被丢弃——实测
孔被丢时体积虚增 60%）；最后 Sewing+ShapeFix_Solid 定向成实心。
**严禁任何环节走 BOPAlgo**（含 ShapeFix_Face.FixOrientation——
数百边线框上 20s 级）。

任意闭合曲线稳健性（急弯/细尖/窄缝/自交都要能生成）：
- **输入归一**：Clipper SimplifyPolygon（非零填充=并集轮廓）→
  自交 8 字、重叠笔画归并为简单多边形（OCCT 不接受自交折线）；
- **有符号偏移全走 Clipper**：三级外偏取并集环（细尖/窄缝自然
  融合）；各环 RDP 保角抽稀（偏差≤容差，急弯自动加密）；
- **微边折叠**：手绘自相切处偏移会产生纳米级退化边，破坏建线
  闭合判定（实测 测试1.cai 腔体环 1nm 边建面失败）——_fitRing
  入口/出口折叠 ≤1µm 微边；
- **异环条带对应**：斜面/唇条带的两环按基准环弧长比例网格采样
  （点落在各自环上、密度继承自适应分布、一一对应不翻折）；
- **粗容差重试**：失败按容差阶梯整链重算。
- 原始 Clipper 输出（数千边）严禁直接进 ThruSections/缝合
  （实测单条带 14.5s）——所有环必须过 _fitRing。

与 Rhino 脚本的对应：safe_offset=Clipper 外偏移取最大环；
FilletCorners 圆角=JT_ROUND 圆角连接（凸角半径≈偏移量）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

from core.vector.geometry_types import Point3D
from core.vector.brep_object import BRepObject
from core.vector.brep_shape import BrepShape, _import_bd, _pts_to_face
from core.vector.outline_geometry import (
    _toClipper, _worldFromClipper, simplifyPolyline)

try:
    import pyclipper
    _HAS_PYCLIPPER = True
except Exception:
    _HAS_PYCLIPPER = False

# 每环点数上限（控制 OCCT 面数/布尔规模）。预览档即成品（确认直接
# 采用预览体），点数/容差按成品精度取：RDP 偏差 ≤0.01mm 级
_MAX_RING_PTS = 2048
_MAX_RING_PTS_FAST = 1024

# 腔体外扩 / 楔盘内缩量（mm）：只求打破共面，远小于打印精度
_EPS = 0.02


@dataclass
class CookieCutterParams:
    """切模参数（默认值 = Rhino 脚本值 + 缝隙 0.3，单位 mm）。"""
    gap: float = 0.3           # 缝隙：先把闭合曲线向外偏移此距离再生成
                               # （内壁=偏移后曲线，饼干与模腔留出间隙）
    wall: float = 0.6          # 刀壁厚：偏移曲线 → a1
    band: float = 1.0          # 加强带宽：a1 → a2
    flange: float = 3.2        # 裙边宽：a2 → a3
    height_blade: float = 15.0  # 总高（刀口唇）
    height_body: float = 12.0   # 主体外圈高
    height_flange: float = 3.6  # 底部裙边高
    bevel_auto: bool = True     # 斜面角度自动（随加强带/斜面高）
    bevel_angle_deg: float = 12.0  # 手动斜面角度（度，bevel_auto=False 时生效）
    bevel_curve: float = 0.0    # 手动截面弯曲：0=直线，+外凸，−内凹
                                # （比例系数，经中高控制环成型）

    def validate(self) -> None:
        if self.gap < 0:
            raise ValueError(f"缝隙不能为负（gap={self.gap}）")
        for name in ("wall", "band", "flange"):
            if getattr(self, name) <= 0:
                raise ValueError(f"宽度参数必须大于 0（{name}={getattr(self, name)}）")
        if not (0 < self.height_flange <= self.height_body < self.height_blade):
            raise ValueError(
                "高度需满足 0 < 裙边高 ≤ 体高 < 总高"
                f"（当前 {self.height_flange}/{self.height_body}"
                f"/{self.height_blade}）")

    def bevelHeight(self) -> float:
        """顶部斜面高 = 总高 − 体高。"""
        return self.height_blade - self.height_body


# ---------------------------------------------------------------- 环工具 ----

def _stripClosing(pts: List[Point3D]) -> List[Point3D]:
    """去掉首尾重合的闭合点（统一为"不重复首点"的环表示）。"""
    if len(pts) >= 2 and pts[0].distanceTo(pts[-1]) < 1e-9:
        return pts[:-1]
    return list(pts)


def _lift(pts: List[Point3D], z: float) -> List[Point3D]:
    """整环平移到高度 z（保持 x/y）。"""
    return [Point3D(p.x, p.y, z) for p in pts]


def _pathArea(path: list) -> float:
    try:
        return abs(pyclipper.Area(path))
    except Exception:
        return 0.0


def _normalizeRing(pts: List[Point3D]) -> Optional[List[Point3D]]:
    """自交/退化归一：非零填充并集轮廓，取面积最大环，去共线微段。

    OCCT 不接受自交折线；8 字/回勾/重叠笔画在此归并为简单多边形
    （对应 Rhino 脚本 CurveBoolean 取外轮廓）。
    """
    if not _HAS_PYCLIPPER or len(pts) < 3:
        return None
    z = pts[0].z
    path = _toClipper(pts)
    if len(path) < 3:
        return None
    try:
        rings = pyclipper.SimplifyPolygon(path, pyclipper.PFT_NONZERO)
    except Exception:
        rings = [path]
    if not rings:
        return None
    best = max(rings, key=_pathArea)
    if _pathArea(best) <= 0:
        return None
    try:
        best = pyclipper.CleanPolygon(best, 500)   # 500 单位 = 0.5µm
    except Exception:
        pass
    out = _worldFromClipper(best, z)
    out = _stripClosing(out)
    return out if len(out) >= 3 else None


def _offsetRing(pts: List[Point3D], dist: float) -> Optional[List[Point3D]]:
    """Clipper 有符号偏移（dist>0 外扩 / <0 内缩），取面积最大环。

    外扩取并集 → 细尖/窄缝自然融合永不自交；内缩用于楔截面 ε 收缩。
    失败（特征塌没了）返回 None，由调用方兜底。
    """
    if not _HAS_PYCLIPPER or len(pts) < 3 or dist == 0:
        return None
    z = pts[0].z
    path = _toClipper(pts)
    if len(path) < 3:
        return None
    if pyclipper.Area(path) < 0:
        path.reverse()
    # 弧逼近容差 0.002mm：拐角圆弧（JT_ROUND）的折线化低至 2µm，
    # 圆角过渡肉眼平滑（0.01 档在圆角处可见棱面）
    arc = max(int(round(min(0.002, abs(dist) / 10.0) * 1e6)), 2)
    co = pyclipper.PyclipperOffset()
    co.ArcTolerance = arc
    co.AddPath(path, pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
    try:
        result = co.Execute(int(round(dist * 1e6)))
    except Exception:
        return None
    if not result:
        return None
    best = max(result, key=_pathArea)
    out = _worldFromClipper(best, z)
    out = _stripClosing(out)
    return out if len(out) >= 3 else None


def _dedupeMicro(pts: List[Point3D], eps: float = 0.001) -> List[Point3D]:
    """折叠相邻间距 ≤eps（默认 1µm）的微边。

    手绘曲线的自相切处经 Clipper 偏移会产生纳米级退化边——零长边会
    破坏 bd.Polygon 的闭合判定（"Face can only be created with closed
    wires"，实测 测试1.cai 腔体环含 1nm 边建面失败）。"""
    if len(pts) < 2:
        return list(pts)
    out = [pts[0]]
    for p in pts[1:]:
        if out[-1].distanceTo(p) > eps:
            out.append(p)
    if len(out) > 2 and out[0].distanceTo(out[-1]) <= eps:
        out.pop()   # 末点≈首点：保持"不重复首点"的环约定
    return out


def _fitRing(pts: List[Point3D], tol: float,
             maxN: int = _MAX_RING_PTS) -> List[Point3D]:
    """微边折叠 + RDP 抽稀到 maxN 点以内（保角；容差从 tol 起、
    超限逐级 ×2.5 放粗）。

    等间隔截点会把尖角削平、Clipper 弧逼近微段会引发布尔 Null，
    RDP 按偏差抽稀两个问题一起解决。
    """
    pts = _dedupeMicro(pts)
    if len(pts) < 3:
        return list(pts)
    out = simplifyPolyline(pts, tol)
    t = tol
    while len(out) > maxN and t < 1.0:
        t *= 2.5
        out = simplifyPolyline(pts, t)
    out = _dedupeMicro(out)
    if len(out) < 3:
        out = [p for i, p in enumerate(pts)
               if i * maxN // len(pts) != (i + 1) * maxN // len(pts)]
        if len(out) < 3:
            out = list(pts)[:maxN]
    return out


def _alignStart(ref: "Point3D", pts: List[Point3D]) -> List[Point3D]:
    """旋转环起点到离参考点最近的顶点（放样两环接缝对齐，防扭转）。"""
    best = 0
    d2 = float("inf")
    for i, p in enumerate(pts):
        v = (p.x - ref.x) ** 2 + (p.y - ref.y) ** 2
        if v < d2:
            d2, best = v, i
    return pts[best:] + pts[:best]



def _cumFractions(pts: List[Point3D]):
    """环各顶点的累计弧长比例 [0,1)（去微边后）。"""
    ring = _dedupeMicro(pts)
    n = len(ring)
    cum = [0.0]
    for i in range(n):
        cum.append(cum[-1] + ring[i].distanceTo(ring[(i + 1) % n]))
    total = cum[n]
    if total < 1e-9:
        return [0.0] * n, ring
    return [cum[i] / total for i in range(n)], ring


def _sampleFrom(ring: List[Point3D], seg_idx: int, t_foot: float,
                fracs) -> List[Point3D]:
    """以角色起点（段 seg_idx 内 t_foot 处的投影足点）为弧长零点，
    按升序比例网格 fracs∈[0,1) 沿环单向取点。

    异环条带的顶点对应：两环同为源曲线的同阶偏移，按同一角色网格
    采样即一一对应，条带不可能翻折；密度继承各环 RDP 自适应分布。
    网格经角色平移后可能绕回（如 off≈1.0 时首值≈0.999）——从足点
    起单向累积弧长天然支持绕回，不依赖列表单调性。"""
    ring = _dedupeMicro(ring)
    n = len(ring)
    a0, b0 = ring[seg_idx], ring[(seg_idx + 1) % n]
    foot = Point3D(a0.x + (b0.x - a0.x) * t_foot,
                   a0.y + (b0.y - a0.y) * t_foot,
                   a0.z + (b.z - a.z) * t_foot if False else
                   a0.z + (b0.z - a0.z) * t_foot)
    # 行走点列：足点 → 环上 n 个顶点（回到段起点），弧长零点=足点。
    # t_foot=1.0 等边端情形下足点与顶点重合也不会插值到段的另一端
    walk = [foot]
    cum = [0.0]
    prev = foot
    for k in range(1, n + 1):
        cur = ring[(seg_idx + k) % n]
        walk.append(cur)
        cum.append(cum[-1] + prev.distanceTo(cur))
        prev = cur
    # 闭合段：从环末顶点走回足点（t_foot>0 时长度>0，漏掉会把总弧长
    # 算短、所有采样点按错误比例落点——实测 a1s 面积 169→54）
    walk.append(foot)
    cum.append(cum[-1] + prev.distanceTo(foot))
    total = cum[n + 1]
    out = []
    j = 0
    for f in fracs:
        target = f * total
        while j < n + 1 and cum[j + 1] < target:
            j += 1
        span = cum[j + 1] - cum[j]
        t = 0.0 if span < 1e-12 else (target - cum[j]) / span
        t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
        p0, p1 = walk[j], walk[j + 1]
        out.append(Point3D(p0.x + (p1.x - p0.x) * t,
                           p0.y + (p1.y - p0.y) * t,
                           p0.z + (p1.z - p0.z) * t))
    return out


def _projectFraction(ref_pt: Point3D, ring: List[Point3D]):
    """参考点在环折线上的投影 → (弧长比例 frac∈[0,1), 段号, 段内 t)。

    必须用点到线段投影而非最近顶点：RDP 环顶点稀疏区最近顶点可偏
    数 mm，对齐误差会让斜面条带整体扭转（实测用户曲线体积虚增 6 倍）。"""
    n = len(ring)
    cum = [0.0]
    for i in range(n):
        cum.append(cum[-1] + ring[i].distanceTo(ring[(i + 1) % n]))
    total = cum[n]
    best_d2 = float("inf")
    best = (0.0, 0, 0.0)
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        abx, aby = b.x - a.x, b.y - a.y
        l2 = abx * abx + aby * aby
        if l2 < 1e-18:
            continue
        t = ((ref_pt.x - a.x) * abx + (ref_pt.y - a.y) * aby) / l2
        t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
        px, py = a.x + t * abx, a.y + t * aby
        d2 = (ref_pt.x - px) ** 2 + (ref_pt.y - py) ** 2
        if d2 < best_d2 - 1e-18:
            best_d2 = d2
            best = ((cum[i] + t * (cum[i + 1] - cum[i])) / total, i, t)
    return best


def _projectOntoRing(src_pts: List[Point3D],
                     target: List[Point3D]) -> List[Point3D]:
    """源环各点 → 目标环折线上的最近点（保持源环顺序）。

    斜面条带两环的对应：内外两环同为源曲线的同阶偏移，源点沿法向
    的对应点=目标环上最近点——矩形角上内外圆角弧长不同，弧长比例
    配对在角上必然错位（切口歪），最近点投影按几何真实对应，角上
    不歪。目标环是源环的内缩偏移，投影映射保序（无翻折）。"""
    import numpy as np
    t = np.array([[q.x, q.y] for q in target])
    nxt = np.roll(t, -1, axis=0)
    ab = nxt - t
    l2 = (ab * ab).sum(axis=1)
    l2[l2 < 1e-18] = 1e-18
    out = []
    for p in src_pts:
        ap = np.array([p.x, p.y]) - t
        tt = np.clip((ab * ap).sum(axis=1) / l2, 0.0, 1.0)
        foot = t + ab * tt[:, None]
        d2 = ((foot - np.array([p.x, p.y])) ** 2).sum(axis=1)
        k = int(np.argmin(d2))
        out.append(Point3D(foot[k][0], foot[k][1], p.z))
    return out


def _scaledAboutCentroid(pts: List[Point3D], grow: float) -> List[Point3D]:
    """环关于质心缩放（兜底用：内缩偏移失败时的次选）。"""
    cx = sum(p.x for p in pts) / len(pts)
    cy = sum(p.y for p in pts) / len(pts)
    r = sum(math.hypot(p.x - cx, p.y - cy) for p in pts) / len(pts)
    f = 1.0 + grow / max(r, 1e-6)
    return [Point3D(cx + (p.x - cx) * f, cy + (p.y - cy) * f, p.z)
            for p in pts]




# ---------------------------------------------------------------- 主构建 ----


def _ringWire(ring: List[Point3D], z: float):
    """环 → OCC 闭合线框（n 段直线边；点距已过微边折叠）。

    用原生 OCP 构造：build123d 的 Vector/Edge 包装每个对象 ~80µs，
    千点环 × 8 线框实测拖慢 5s+，且首次使用会连带触发 brep_from_stl
    →sklearn/scipy 的 4s 冷导入。"""
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


def _sewSolid(pieces):
    bd = _import_bd()
    """纯拓扑构造实心（零布尔，绕开此内核大多边形 BOPAlgo 的慢/错/崩）。

    pieces: ("annulus", outer, inner, z) = 带孔平面（底/顶/裙边顶）；
            ("strip", ring1, ring2, z1, z2) = 两环间的放样条带
            （柱面/斜面/腔壁；同环两高度=柱面，异环=斜面）。
    条带用 ThruSections（ruled=False → 单张面），平面环用
    MakeFace+内孔线框，最后 Sewing+ShapeFix_Solid 定向成实心。
    """
    from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakeFace,
                                    BRepBuilderAPI_Sewing)
    from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections
    from OCP.ShapeFix import ShapeFix_Solid
    from OCP.TopoDS import TopoDS

    faces = []
    for piece in pieces:
        if piece[0] == "annulus":
            _, outer, inner, z = piece
            mf = BRepBuilderAPI_MakeFace(_ringWire(outer, z), True)
            # 内孔线框必须与外环反向（外环 CCW、内环 CW）MakeFace 才
            # 认作孔，否则孔被当第二外环丢弃（实测全部变实心盘，体积
            # 虚增 60%）。偏移环统一 CCW，内环点序反转即 CW。
            # 不用 ShapeFix_Face.FixOrientation：数百边线框上耗时 20s 级
            mf.Add(_ringWire(list(reversed(inner)), z))
            faces.append(mf.Face())
        else:
            _, r1, r2, z1, z2 = piece
            # ruled=True：每对对应边一张精确直纹四边形（几何精确、
            # 尖角保真）；跨四边形的渲染棱面由 triangulate 的特征边
            # 感知法向焊接消除（core/vector/brep_shape.py）
            ts = BRepOffsetAPI_ThruSections(False, True, 1e-6)
            ts.AddWire(_ringWire(r1, z1))
            ts.AddWire(_ringWire(r2, z2))
            ts.Build()
            if not ts.IsDone():
                raise ValueError("条带放样失败")
            faces.append(ts.Shape())

    sew = BRepBuilderAPI_Sewing(1e-4, True, True, True, False)
    for f in faces:
        sew.Add(f)
    sew.Perform()
    sewed = sew.SewedShape()

    from OCP.TopAbs import TopAbs_ShapeEnum
    st = sewed.ShapeType()
    if st == TopAbs_ShapeEnum.TopAbs_SOLID:
        solid = TopoDS.Solid_s(sewed)
    elif st == TopAbs_ShapeEnum.TopAbs_SHELL:
        solid = ShapeFix_Solid().SolidFromShell(TopoDS.Shell_s(sewed))
    else:
        # 复合体（缝合未合并成单壳）：取面数最多的壳
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
            raise ValueError("缝合未产生壳")
        solid = ShapeFix_Solid().SolidFromShell(best)
    if solid is None or solid.IsNull():
        raise ValueError("切模实体缝合失败")
    # 注意用 bd.Solid 包装：bd.Part(TopoDS_Solid) 会得到空壳（volume=0）
    return bd.Solid(solid)


def _currentShrink(params: "CookieCutterParams", h_bevel: float) -> float:
    """当前斜面档的顶部收缩量（自动=带宽；手动=角度决定，钳制）。"""
    if params.bevel_auto:
        return params.band
    return min(h_bevel * math.tan(
        math.radians(max(0.5, min(params.bevel_angle_deg, 85.0)))),
        params.band)


def _dedupeMicroPair(a_pts: List[Point3D], b_pts: List[Point3D]):
    """配对环去微边：目标环连续重复点（投影多点落在同一处，如角部
    圆弧/内缩融合区）连同源环对应点一并剔除，保持两环点数一致。
    闭合微边任一侧 ≤1µm 就删末对——b 环闭合微边会让 MakeEdge 直接
    失败（StdFail_NotDone），a 环删一点仅轻微简化。"""
    a_out, b_out = [a_pts[0]], [b_pts[0]]
    for i in range(1, len(b_pts)):
        if b_out[-1].distanceTo(b_pts[i]) > 0.001:
            a_out.append(a_pts[i])
            b_out.append(b_pts[i])
    if len(a_out) > 2 and (
            a_out[0].distanceTo(a_out[-1]) <= 0.001
            or b_out[0].distanceTo(b_out[-1]) <= 0.001):
        a_out.pop()
        b_out.pop()
    return a_out, b_out


def _buildOnce(src: List[Point3D], kind: str,
               params: CookieCutterParams, tol: float, maxPts: int,
               checkValid: bool) -> "object":
    """单次完整构建（tol=各环 RDP 容差，maxPts=环点数上限），失败抛 ValueError。"""
    # ---- 缝隙：先把闭合曲线向外偏移 gap，后续全部基于偏移曲线生成
    #      （内壁=偏移曲线，刀壁厚相对偏移曲线不变，整体外扩 gap）----
    if params.gap > 1e-9:
        src_eff = _offsetRing(src, params.gap)
        if src_eff is None:
            raise ValueError(
                f"缝隙 {params.gap} 偏移失败：曲线过小，请减小缝隙")
    else:
        src_eff = src

    # ---- 三级外偏移（Clipper 并集环：细尖/窄缝自然融合）----
    names = ("刀壁厚", "加强带宽", "裙边宽")
    rings: List[Optional[List[Point3D]]] = [src_eff, None, None, None]
    for i in range(1, 4):
        dist = (params.wall, params.band, params.flange)[i - 1]
        out = _offsetRing(rings[i - 1], dist)
        if out is None:
            raise ValueError(
                f"{names[i - 1]} {dist} 偏移失败：曲线过小或细节过密，"
                "请增大曲线尺寸或减小该宽度")
        rings[i] = _fitRing(out, tol, maxPts)
    _, a1, a2, a3 = rings

    # ---- 外壳：纯拓扑构造，零布尔 ----
    # 此 OCCT（7.9.3 novtk）对大多边形 BOPAlgo 又慢又不稳定（~400 点
    # 起融合数秒、切割会静默产出错误实体、锥度挤出间歇崩溃），故整个
    # 实体不用任何布尔：8 张面直接构造 → 缝合成实心。
    #   1 底面环(a3−cav)   2 a3 柱面   3 裙边顶环(a3−a2)   4 a2 柱面
    #   5 斜面(a2→top)     6 唇微带(→a1)  7 顶面环(a1−cav)  8 腔壁(cav)
    # 各环弧长均匀重采样到同点数 + 起点链式对齐 + 同向 → 条带放样
    # 顶点一一对应，数学上不可能翻折。
    z0 = float(src[0].z)

    # 斜面顶环：自动档 = 恰好收到唇外圈 a1（经典形状，刀缘宽=壁厚）；
    # 手动角更缓→收得少→顶缘更厚。零布尔下无需任何 ε/相切规避，
    # 也无需唇微带（0.05 级窄条的高宽比悬殊四边形是无效面来源）
    h_bevel = params.height_blade - params.height_body
    shrink = _currentShrink(params, h_bevel)
    if shrink >= params.band - 1e-9:
        top_ring = a1
    else:
        top_ring = _offsetRing(a2, -shrink) \
            or _scaledAboutCentroid(a2, -shrink)

    # 环保持 RDP 自适应密度（同环条带直接用，壁面偏差=RDP 容差）。
    # 斜面条带两环（a2→top）按**源曲线投影**配对：a2 各点在 top 环上
    # 取最近点（=沿法向的几何真实对应）——矩形角上内外圆角弧长不同，
    # 弧长比例配对在角上必然错位（切口歪，Rhino 自动挡同病），投影
    # 配对按几何对应，角上不歪；目标环为内缩偏移，投影映射保序不翻折
    a2s = _fitRing(a2, tol, maxPts)
    a1s = _projectOntoRing(a2s, top_ring)
    a2s, topr = _dedupeMicroPair(a2s, a1s)
    # 腔壁环=生成曲线（零布尔无相切顾虑，内壁精确=缝隙位），
    # 但必须同样过 RDP——原始 Clipper 输出数千边，条带/缝合 20s 级
    cav = _fitRing(src_eff, tol, maxPts)

    zf = z0 + params.height_flange
    body_z = z0 + params.height_body
    blade_z = z0 + params.height_blade
    if abs(params.bevel_curve) > 1e-6:
        # 手动截面弯曲：中高控制环收缩量按曲线系数缩放（+外凸/−内凹，
        # 0=直线）→ 斜面拆成两段条带过控制环（Rhino 手动调截面的
        # 参数化等价）
        z_mid = (body_z + blade_z) * 0.5
        shrink_mid = min(
            (z_mid - body_z) / max(h_bevel, 1e-9)
            * _currentShrink(params, h_bevel)
            * (1.0 + params.bevel_curve * 0.8), params.band)
        mid_ring = _offsetRing(a2, -shrink_mid)             or _scaledAboutCentroid(a2, -shrink_mid)
        a2s, mid_s = _dedupeMicroPair(a2s, _projectOntoRing(a2s, mid_ring))
        part = _sewSolid([
            ("annulus", a3, cav, z0),
            ("strip", a3, a3, z0, zf),
            ("annulus", a3, a2s, zf),
            ("strip", a2s, a2s, zf, body_z),
            ("strip", a2s, mid_s, body_z, z_mid),
            ("strip", mid_s, topr, z_mid, blade_z),
            ("annulus", topr, cav, blade_z),
            ("strip", cav, cav, z0, blade_z),
        ])
    else:
        part = _sewSolid([
            ("annulus", a3, cav, z0),
            ("strip", a3, a3, z0, zf),
            ("annulus", a3, a2s, zf),
            ("strip", a2s, a2s, zf, body_z),
            ("strip", a2s, topr, body_z, blade_z),
            ("annulus", topr, cav, blade_z),
            ("strip", cav, cav, z0, blade_z),
        ])

    if checkValid and not part.is_valid:
        raise ValueError("切模实体无效")
    return part


def build_cookie_cutter_part(pts: List[Point3D], kind: str,
                             params: CookieCutterParams,
                             fast: bool = False):
    """闭合曲线点列 → 切模 build123d Part（单一水密实体）。

    fast=True 预览档：粗抽稀 + 少点数 + 跳过有效性校验（约快 3 倍）；
    fast=False 确认档：完整质量。失败抛 ValueError（中文信息）。
    """
    params.validate()
    src = _stripClosing(pts)
    if len(src) < 3:
        raise ValueError("轮廓点数不足")

    src = _normalizeRing(src)
    if src is None:
        raise ValueError("轮廓无法构成闭合区域（自交或退化）")

    if fast:
        # 预览档=成品（确认直接采用预览体）：偏差 ≤0.02mm 肉眼光滑。
        # 更紧容差会让条带四边形面数/缝合超线性爆炸（6k 边 10s+，
        # 0.005 档实测 60s）——0.02 是此内核的速度/光滑平衡点
        ladders = ((0.01, 0.04),)
        maxPts, checkValid = _MAX_RING_PTS_FAST, False
    else:
        ladders = ((0.005, 0.02, 0.1),)
        maxPts, checkValid = _MAX_RING_PTS, True

    last: ValueError | None = None
    for ladder in ladders:
        for tol in ladder:
            try:
                return _buildOnce(src, kind, params, tol, maxPts, checkValid)
            except ValueError as e:
                last = e
    raise ValueError(f"{last}（已尝试多级抽稀容差仍失败，"
                     "请检查曲线是否过于极端）")


def build_cookie_cutter(pts: List[Point3D], kind: str,
                        params: CookieCutterParams,
                        fast: bool = False) -> BRepObject:
    """闭合曲线点列 → 饼干外切模 BRepObject（单一水密实体）。

    任意闭合曲线（急弯/细尖/窄缝/自交）均可生成：输入先归一为简单
    多边形，几何异常时按抽稀容差阶梯重试。
    pts：世界坐标水平闭合轮廓（带不带末尾闭合点均可）；
    kind：原曲线轮廓类型（决定腔体工具内壁精度：circle=解析圆）。
    """
    part = build_cookie_cutter_part(pts, kind, params, fast=fast)
    brep = BRepObject()
    brep.setShape(BrepShape.fromPart(part))
    return brep

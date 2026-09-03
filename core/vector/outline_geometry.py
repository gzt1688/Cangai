"""外轮廓几何：把一组选中曲线生成一条包裹全部的最大外轮廓（往外偏一段距离）。

完美复刻 Rhino 插件 ToolPage.ShowOuterOutline / SiliconeMoldFunctions.OffsetOutward 的算法：

  1) 闭合曲线 → 每条 Clipper 外偏 dist（EndType.Polygon，取面积最大环）；
  2) 开放曲线（凸出线）→ Clipper ET_OPENROUND 偏移成闭合"包裹带"；
  3) 全部包裹区域 Clipper 布尔并集（对应 Curve.CreateBooleanUnion）；
  4) 取所有外环（丢弃被完全包含的内孔）：
     - 1 条 → 直接作为包裹线；
     - 多条（选中曲线彼此隔开，如两个分开的圆）→ 全部曲线采样点凸包外偏 dist，
       得到一条把全部曲线都包进去的单一包裹线；
     - 0 条 → 取面积最大的并集环兜底。

所有结果统一为"世界坐标、闭合、首尾重合"的点列表，由调用方转成对象加入文档。
本模块为纯几何，不依赖 Qt / 文档对象。
"""
from __future__ import annotations
import math
from typing import List, Optional

from .geometry_types import Point3D

try:
    import pyclipper
    _HAS_PYCLIPPER = True
except ImportError:
    _HAS_PYCLIPPER = False


# 浮点坐标 → Clipper 整数坐标的缩放系数（与 offset_geometry 一致）
_SCALE = 1e6
# 开放/闭合曲线采样间距（mm）：对应 Rhino 插件 0.4 / 0.5 的弧长采样密度
_CLOSED_SPACING = 0.4
_OPEN_SPACING = 0.5


def clipperAvailable() -> bool:
    """pyclipper 是否可用（不可用时上层回退/提示）。"""
    return _HAS_PYCLIPPER


# ---------------------------------------------------------------- 采样工具 ----

def sampleClosed(pts: List[Point3D]) -> List[Point3D]:
    """闭合折线采样：把顶点序列补成 Clipper 需要的闭合、无重复末点序列。

    输入已是折线点列表（对象的多边形表示）；返回的序列首点 != 末点，
    且按 Rhino 的弧长均匀密度重采样（默认 0.4mm/点，上限 8000）。
    """
    return _resample(pts, _CLOSED_SPACING, closed=True)


def sampleOpen(pts: List[Point3D]) -> List[Point3D]:
    """开放折线采样：保持首尾，按 Rhino 的 0.5mm/点 密度重采样（上限 8000）。"""
    return _resample(pts, _OPEN_SPACING, closed=False)


def _resample(pts: List[Point3D], spacing: float,
              closed: bool = False) -> List[Point3D]:
    """按弧长均匀重采样到目标密度，返回不重复末点的点序列。"""
    if len(pts) < 2:
        return [Point3D(p.x, p.y, p.z) for p in pts]
    # 去掉可能存在的重复末点（闭合表示）
    work = list(pts)
    if len(work) >= 2 and _same2d(work[0], work[-1]):
        work = work[:-1]
    if len(work) < 2:
        return [Point3D(p.x, p.y, p.z) for p in work]

    segs = []
    total = 0.0
    for i in range(len(work) - 1):
        a, b = work[i], work[i + 1]
        L = math.hypot(b.x - a.x, b.y - a.y)
        segs.append((a, b, L))
        total += L
    if closed and len(work) >= 3:
        a, b = work[-1], work[0]
        L = math.hypot(b.x - a.x, b.y - a.y)
        if L > 1e-9:
            segs.append((a, b, L))
            total += L

    if total < 1e-9:
        return [Point3D(p.x, p.y, p.z) for p in work]

    n = int(math.ceil(total / max(spacing, 1e-4))) + 1
    n = max(n, 32 if closed else 16)
    n = min(n, 8000)

    out: List[Point3D] = []
    step = total / (n if not closed else n)  # 闭合：n 段回到首点
    # 沿弧长取 n(+1) 个点
    count = n if not closed else n
    # 逐段累积定位
    segIdx = 0
    segAccum = 0.0
    cur = segs[segIdx][0]
    out.append(Point3D(cur.x, cur.y, cur.z))
    target = 0.0
    for _ in range(count):
        target += step
        while segIdx < len(segs) and segAccum + segs[segIdx][2] < target - 1e-12:
            segAccum += segs[segIdx][2]
            segIdx += 1
        if segIdx >= len(segs):
            segIdx = len(segs) - 1
        a, b, L = segs[segIdx]
        t = (target - segAccum) / L if L > 1e-9 else 0.0
        t = max(0.0, min(1.0, t))
        out.append(Point3D(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t, a.z))
    if closed:
        out.append(Point3D(out[0].x, out[0].y, out[0].z))
    return out


def _same2d(a: Point3D, b: Point3D, tol: float = 1e-9) -> bool:
    return abs(a.x - b.x) < tol and abs(a.y - b.y) < tol


# ---------------------------------------------------------------- Clipper 工具 ----

def _toClipper(pts: List[Point3D]) -> list:
    path = []
    for p in pts:
        path.append((int(round(p.x * _SCALE)), int(round(p.y * _SCALE))))
    # 去除连续重复点（Clipper 不接受）
    out = []
    for q in path:
        if not out or q != out[-1]:
            out.append(q)
    return out


def _arcTolerance(dist: float) -> int:
    """圆弧离散精度：实际 0.01 单位，但不大于偏移量的 1/10（避免极小偏移退化）。"""
    return max(int(round(min(0.01, dist / 10.0) * _SCALE)), 2)


def _pathAreaClipper(path: list) -> float:
    try:
        return abs(pyclipper.Area(path))
    except Exception:
        return 0.0


def _worldFromClipper(path: list, z: float) -> List[Point3D]:
    """Clipper 闭合整数路径 → 世界坐标闭合点列表（去掉重复末点，末点=首点）。"""
    pts = [Point3D(q[0] / _SCALE, q[1] / _SCALE, z) for q in path]
    if len(pts) >= 2 and _same2d(pts[0], pts[-1], 1e-9):
        pts = pts[:-1]
    if len(pts) >= 3 and not _same2d(pts[0], pts[-1], 1e-9):
        pts.append(Point3D(pts[0].x, pts[0].y, z))
    return pts


# ---------------------------------------------------------------- 核心步骤 ----

def offsetClosedOutward(pts: List[Point3D], dist: float) -> Optional[List[Point3D]]:
    """闭合曲线沿外侧干净偏移 dist（Clipper 自动处理凹角与自交，取面积最大环）。

    对应 Rhino 插件 SiliconeMoldFunctions.OffsetOutward。
    """
    if not _HAS_PYCLIPPER or len(pts) < 3 or dist <= 0:
        return None
    z = pts[0].z
    path = _toClipper(pts)
    if len(path) < 3:
        return None
    # 保证逆时针（正面积），使正向 delta 一定为外扩
    if pyclipper.Area(path) < 0:
        path.reverse()
    co = pyclipper.PyclipperOffset()
    co.ArcTolerance = _arcTolerance(dist)
    co.AddPath(path, pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
    result = co.Execute(int(round(dist * _SCALE)))
    if not result:
        return None
    best = max(result, key=_pathAreaClipper)
    return _worldFromClipper(best, z)


def offsetOpenAsBand(pts: List[Point3D], dist: float) -> Optional[List[Point3D]]:
    """开放曲线用 Clipper 偏移成闭合"包裹带"（EndType.Round：开放路径得到闭合胶囊状轮廓）。

    对应 Rhino 插件 ToolPage.OffsetOpenAsBand。
    """
    if not _HAS_PYCLIPPER or len(pts) < 2 or dist <= 0:
        return None
    z = pts[0].z
    path = _toClipper(pts)
    if len(path) < 2:
        return None
    co = pyclipper.PyclipperOffset()
    co.ArcTolerance = _arcTolerance(dist)
    co.AddPath(path, pyclipper.JT_ROUND, pyclipper.ET_OPENROUND)
    result = co.Execute(int(round(dist * _SCALE)))
    if not result:
        return None
    best = max(result, key=_pathAreaClipper)
    return _worldFromClipper(best, z)


def booleanUnion(paths: List[List[Point3D]], z: float) -> List[List[Point3D]]:
    """多条闭合路径的布尔并集（对应 Rhino Curve.CreateBooleanUnion）。"""
    if not _HAS_PYCLIPPER or not paths:
        return []
    pc = pyclipper.Pyclipper()
    for pts in paths:
        path = _toClipper(pts)
        if len(path) >= 3:
            pc.AddPath(path, pyclipper.PT_SUBJECT, True)
    try:
        result = pc.Execute(pyclipper.CT_UNION,
                            pyclipper.PFT_NONZERO, pyclipper.PFT_NONZERO)
    except Exception:
        return []
    return [_worldFromClipper(p, z) for p in result if len(p) >= 3]


def _pathCentroid(pts: List[Point3D]) -> Point3D:
    x = sum(p.x for p in pts) / len(pts)
    y = sum(p.y for p in pts) / len(pts)
    return Point3D(x, y, pts[0].z if pts else 0.0)


def outerRings(paths: List[List[Point3D]]) -> List[List[Point3D]]:
    """从布尔并集结果中取出所有"外环"（丢弃被其它环完全包含的内孔）。

    在 XY 平面做点包含判定（Clipper.PointInPolygon）。对应 ToolPage.OuterRings。
    """
    if not _HAS_PYCLIPPER or not paths:
        return []
    outer: List[List[Point3D]] = []
    for i, pts in enumerate(paths):
        cen = _pathCentroid(pts)
        cpt = (int(round(cen.x * _SCALE)), int(round(cen.y * _SCALE)))
        insideOther = False
        for j, other in enumerate(paths):
            if j == i:
                continue
            opath = _toClipper(other)
            if len(opath) < 3:
                continue
            try:
                if pyclipper.PointInPolygon(cpt, opath) > 0:
                    insideOther = True
                    break
            except Exception:
                pass
        if not insideOther:
            outer.append(pts)
    return outer


def _largestAreaLoop(paths: List[List[Point3D]]) -> Optional[List[Point3D]]:
    if not paths:
        return None
    best = None
    bestA = -1.0
    for pts in paths:
        path = _toClipper(pts)
        a = _pathAreaClipper(path)
        if a > bestA:
            bestA = a
            best = pts
    return best


def convexHull2D(points: List[Point3D]) -> List[Point3D]:
    """2D 单调链凸包，返回逆时针（CCW）点序列（不含重复末点）。

    对应 ToolPage.ConvexHull2D。
    """
    pts = sorted(points, key=lambda p: (p.x, p.y))
    n = len(pts)
    if n < 3:
        return pts

    def cross(o, a, b):
        return (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x)

    lower: List[Point3D] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: List[Point3D] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    hull = lower[:-1] + upper[:-1]
    return hull


def convexHullWrapper(curves: List[List[Point3D]],
                      dist: float) -> Optional[List[Point3D]]:
    """全部曲线采样点的凸包（CCW）外偏 dist，得到把全部曲线包进去的单一包裹线。

    对应 ToolPage.ConvexHullWrapper。
    """
    allpts: List[Point3D] = []
    z = 0.0
    for pts in curves:
        if not pts:
            continue
        z = pts[0].z
        allpts.extend(pts)
    if len(allpts) < 3:
        return None
    hull = convexHull2D(allpts)
    if len(hull) < 3:
        return None
    closed = hull + [Point3D(hull[0].x, hull[0].y, hull[0].z)]
    return offsetClosedOutward(closed, dist)


# ---------------------------------------------------------------- 主入口 ----

def computeOuterOutline(closedCurves: List[List[Point3D]],
                        openCurves: List[List[Point3D]],
                        dist: float) -> List[List[Point3D]]:
    """外轮廓主入口：返回包裹全部曲线的单一外轮廓点列表（0 或 1 条）。

    完美复刻 Rhino 插件 ShowOuterOutline.ComputeOuter 的分支逻辑：
      - 闭合曲线每条外偏 → 开放曲线包裹带 → 布尔并集；
      - 外环 ≤1 条 → 直接用（0 条时取面积最大环）；
      - 外环多条（彼此隔开）→ 凸包外偏得到单一包裹线。

    参数:
        closedCurves: 每条闭合曲线的采样点列表
        openCurves:   每条开放曲线的采样点列表
        dist:         外偏距离（>0）
    返回:
        0 或 1 条外轮廓（世界坐标、闭合、首尾重合）。
    """
    if not _HAS_PYCLIPPER or dist <= 0:
        return []
    if not closedCurves and not openCurves:
        return []

    regions: List[List[Point3D]] = []
    for pts in closedCurves:
        off = offsetClosedOutward(pts, dist)
        if off is not None:
            regions.append(off)
    for pts in openCurves:
        band = offsetOpenAsBand(pts, dist)
        if band is not None:
            regions.append(band)
    if not regions:
        return []

    z = regions[0][0].z
    combined = booleanUnion(regions, z)
    if not combined:
        return []

    outer = outerRings(combined)
    if len(outer) <= 1:
        if len(outer) == 1:
            return [outer[0]]
        loop = _largestAreaLoop(combined)
        return [loop] if loop is not None else []

    # 多条外环 → 凸包外偏（覆盖"两个圆完全分开、没有连接线"的情况）
    hull = convexHullWrapper(list(closedCurves) + list(openCurves), dist)
    if hull is not None:
        return [hull]
    loop = _largestAreaLoop(combined)
    return [loop] if loop is not None else []


def simplifyPolyline(pts: List[Point3D], tolerance: float) -> List[Point3D]:
    """Douglas-Peucker 折线抽稀：公差内删除共线/近共线的密集顶点，保持形状。

    用于去掉 Clipper 偏移产生的大量离散点（对应"重建偏移线（去密集点）"，
    以公差控制形状偏差，结果仍是折线，不转 NURBS）。支持闭合多边形。
    """
    if tolerance <= 0 or len(pts) < 3:
        return pts
    work = list(pts)
    closed = len(work) >= 2 and _same2d(work[0], work[-1])
    if closed:
        work = work[:-1]
    n = len(work)
    if n < 3:
        return pts

    if closed:
        # 闭合多边形：取相距最远的两个顶点切成两段，分别 RDP（保证公共端点保留）。
        # 精确最远对是 O(n²)（n 上限 8000 时约 3200 万次距离计算会冻结 UI），
        # 改用两遍扫描近似：距首点最远点 A、距 A 最远点 B（直径的 ≥2/3），
        # 作为分段锚点足够——与图片转线稿的闭合简化策略一致。
        iA = 0
        best = -1.0
        for i in range(1, n):
            d = _dist2d(work[i], work[0])
            if d > best:
                best, iA = d, i
        iB = 0
        best = -1.0
        for i in range(n):
            d = _dist2d(work[i], work[iA])
            if d > best:
                best, iB = d, i
        if iB < iA:
            iA, iB = iB, iA
        seg1 = work[iA:iB + 1]
        seg2 = work[iB:] + work[:iA + 1]
        out = _rdp(seg1, tolerance)[:-1] + _rdp(seg2, tolerance)[:-1]
    else:
        out = _rdp(work, tolerance)

    if len(out) < 3:
        return pts
    if closed:
        out.append(Point3D(out[0].x, out[0].y, out[0].z))
    return out


def _dist2d(a: Point3D, b: Point3D) -> float:
    return math.hypot(b.x - a.x, b.y - a.y)


def _rdp(pts: List[Point3D], tol: float) -> List[Point3D]:
    """开放点列的 Douglas-Peucker 抽稀（显式栈迭代，首末点必保留；
    避免极端输入下递归深度失控）。"""
    n = len(pts)
    if n < 3:
        return list(pts)
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    while stack:
        first, last = stack.pop()
        if last - first < 2:
            continue
        a, b = pts[first], pts[last]
        dx, dy = b.x - a.x, b.y - a.y
        L = math.hypot(dx, dy)
        idx, dmax = -1, -1.0
        if L < 1e-12:
            # 首末重合：取离首点最远的点
            for i in range(first + 1, last):
                d = _dist2d(pts[i], a)
                if d > dmax:
                    dmax, idx = d, i
        else:
            for i in range(first + 1, last):
                p = pts[i]
                d = abs(dy * p.x - dx * p.y + b.x * a.y - b.y * a.x) / L
                if d > dmax:
                    dmax, idx = d, i
        if dmax <= tol or idx < 0:
            continue
        keep[idx] = True
        stack.append((first, idx))
        stack.append((idx, last))
    return [pts[i] for i in range(n) if keep[i]]

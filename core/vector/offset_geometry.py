"""偏移几何：将曲线沿法线方向偏移一定距离生成平行曲线。

- offsetPolyline：折线偏移，每段沿左法线平移，相邻偏移段求交点作新顶点（保留尖角）
- offsetSampledCurve：采样曲线偏移，每点沿相邻段法线平均方向平移（平滑曲线）
- clipperOffsetBothSides：基于 Clipper(pyclipper) 的双侧偏移，
  开放曲线生成一条闭合外轮廓（平头/圆头端盖），闭合曲线双向偏移出内/外轮廓
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


def _segIntersect(a1, a2, b1, b2) -> Optional[tuple]:
    """两直线交点（无限延长求交，不钳制到段内），平行返回 None。"""
    d1x, d1y = a2[0] - a1[0], a2[1] - a1[1]
    d2x, d2y = b2[0] - b1[0], b2[1] - b1[1]
    denom = d1x * d2y - d1y * d2x
    if abs(denom) < 1e-12:
        return None
    t = ((b1[0] - a1[0]) * d2y - (b1[1] - a1[1]) * d2x) / denom
    return (a1[0] + t * d1x, a1[1] + t * d1y)


def _leftNormal(a: Point3D, b: Point3D) -> tuple:
    """线段 ab 的左法线（单位向量）；退化返回 (0,0)。"""
    dx, dy = b.x - a.x, b.y - a.y
    L = math.hypot(dx, dy)
    if L < 1e-9:
        return (0.0, 0.0)
    return (-dy / L, dx / L)


def offsetPolyline(pts: List[Point3D], dist: float,
                    closed: bool = False) -> List[Point3D]:
    """折线偏移：每段沿左法线平移 dist，相邻偏移段求交点作新顶点。
    保留折线尖角；闭合折线首尾连接处同样求交。"""
    n = len(pts)
    if n < 2:
        return []
    if closed:
        segs = [(i, (i + 1) % n) for i in range(n)]
    else:
        segs = [(i, i + 1) for i in range(n - 1)]
    m = len(segs)
    # 每段偏移后的两端点
    off = []
    for (i, j) in segs:
        a, b = pts[i], pts[j]
        nx, ny = _leftNormal(a, b)
        if nx == 0.0 and ny == 0.0:
            off.append(None)
        else:
            off.append(((a.x + nx * dist, a.y + ny * dist),
                        (b.x + nx * dist, b.y + ny * dist)))
    newPts: List[Point3D] = []
    if closed:
        for k in range(n):
            prev = off[(k - 1) % m]
            cur = off[k % m]
            newPts.append(_vertexOf(prev, cur, pts[k]))
    else:
        # 首点 = 第一段偏移起点
        newPts.append(Point3D(off[0][0][0], off[0][0][1], pts[0].z)
                      if off[0] is not None else pts[0])
        # 中间点 = 相邻偏移段交点
        for k in range(1, n - 1):
            newPts.append(_vertexOf(off[k - 1], off[k], pts[k]))
        # 末点 = 最后段偏移终点
        newPts.append(Point3D(off[-1][1][0], off[-1][1][1], pts[-1].z)
                      if off[-1] is not None else pts[-1])
    return newPts


def _vertexOf(prev, cur, refPt: Point3D) -> Point3D:
    """两偏移段交点作为新顶点；平行/退化时取当前段起点。"""
    if prev is None or cur is None:
        return refPt
    inter = _segIntersect(prev[0], prev[1], cur[0], cur[1])
    if inter is not None:
        return Point3D(inter[0], inter[1], refPt.z)
    return Point3D(cur[0][0], cur[0][1], refPt.z)


def offsetSampledCurve(pts: List[Point3D], dist: float,
                       closed: bool = False) -> List[Point3D]:
    """采样曲线偏移：每点沿相邻段法线平均方向平移 dist。
    用于圆/椭圆/弧/样条等平滑曲线的采样点偏移。"""
    n = len(pts)
    if n < 2:
        return []
    segCount = n if closed else n - 1
    normals = []
    for i in range(segCount):
        normals.append(_leftNormal(pts[i], pts[(i + 1) % n]))
    newPts: List[Point3D] = []
    for k in range(n):
        if closed:
            n1 = normals[(k - 1) % segCount]
            n2 = normals[k % segCount]
        elif k == 0:
            n1 = n2 = normals[0]
        elif k == n - 1:
            n1 = n2 = normals[-1]
        else:
            n1 = normals[k - 1]
            n2 = normals[k]
        nx = (n1[0] + n2[0]) * 0.5
        ny = (n1[1] + n2[1]) * 0.5
        L = math.hypot(nx, ny)
        if L > 1e-9:
            nx /= L
            ny /= L
        newPts.append(Point3D(pts[k].x + nx * dist, pts[k].y + ny * dist,
                              pts[k].z))
    if closed and newPts:
        # 闭合曲线偏移后强制首尾重合，避免接缝处断开
        newPts[-1] = Point3D(newPts[0].x, newPts[0].y, newPts[0].z)
    return newPts


def _pointInPolygon(pts: List[Point3D], p: Point3D) -> bool:
    """射线法内点测试（even-odd）：对任意简单多边形（含非凸）结果唯一确定，
    不依赖“最近段”选择——这是闭合曲线偏移侧别稳定性的关键。"""
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i].x, pts[i].y
        xj, yj = pts[j].x, pts[j].y
        if (yi > p.y) != (yj > p.y):
            xint = (xj - xi) * (p.y - yi) / (yj - yi) + xi
            if p.x < xint:
                inside = not inside
        j = i
    return inside


def _polygonOrientation(pts: List[Point3D]) -> float:
    """鞋带公式有向面积：>0 逆时针（CCW），<0 顺时针（CW）。"""
    a = 0.0
    n = len(pts)
    for i in range(n):
        r = pts[i]
        q = pts[(i + 1) % n]
        a += r.x * q.y - q.x * r.y
    return a * 0.5


def _boundaryDistance(pts: List[Point3D], p: Point3D,
                      closed: bool) -> float:
    """点到折线边界的真实距离（投影参数钳制到段内，闭合环绕）。"""
    best = float('inf')
    n = len(pts)
    segCount = n if closed else n - 1
    for i in range(segCount):
        a, b = pts[i], pts[(i + 1) % n]
        dx, dy = b.x - a.x, b.y - a.y
        L2 = dx * dx + dy * dy
        if L2 < 1e-12:
            continue
        t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / L2
        t = max(0.0, min(1.0, t))
        d = math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy))
        if d < best:
            best = d
    return best


def sideOfPoint(pts: List[Point3D], p: Point3D,
                closed: bool = False) -> float:
    """判断点 p 在折线哪一侧，返回带符号的距离（幅值=到边界真实距离）。

    闭合曲线：inside/outside 用射线法唯一确定（非凸图形也稳定），
    符号按绕向归一到旧语义——CCW（左法线指向内）内侧为正，CW 内侧为负；
    因此“鼠标在图形内”得到的符号恒定，偏移预览不再随最近段切换而内外乱跳。
    开放曲线：最近段（投影钳制）法线与 (p-最近点) 的点积，
    >0 在行进方向左侧，<0 在右侧。"""
    if len(pts) < 2:
        return 0.0
    if closed:
        poly = pts
        if len(poly) >= 2 and poly[0].distanceTo(poly[-1]) < 1e-9:
            poly = poly[:-1]   # 去重复末点，避免零长段影响定向
        if len(poly) < 3:
            return 0.0
        dist = _boundaryDistance(poly, p, closed=True)
        inside = _pointInPolygon(poly, p)
        ccw = _polygonOrientation(poly) > 0
        if inside:
            return dist if ccw else -dist
        return -dist if ccw else dist
    bestDist = float('inf')
    bestIdx = 0
    bestT = 0.0
    for i in range(len(pts) - 1):
        a, b = pts[i], pts[i + 1]
        dx, dy = b.x - a.x, b.y - a.y
        L2 = dx * dx + dy * dy
        if L2 < 1e-12:
            continue
        t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / L2
        t = max(0.0, min(1.0, t))
        cx = a.x + t * dx
        cy = a.y + t * dy
        d = math.hypot(p.x - cx, p.y - cy)
        if d < bestDist:
            bestDist = d
            bestIdx = i
            bestT = t
    a, b = pts[bestIdx], pts[bestIdx + 1]
    nx, ny = _leftNormal(a, b)
    # 最近点
    cx = a.x + (b.x - a.x) * bestT
    cy = a.y + (b.y - a.y) * bestT
    return (p.x - cx) * nx + (p.y - cy) * ny


# ---------- Clipper 双侧偏移（参考 Rhino 插件 ClipperOffsetCurveBidirectional） ----------

_CLIPPER_SCALE = 1e6  # 浮点坐标 → Clipper 整数坐标的缩放系数


def _clipperPathsSimilar(a: list, b: list, tol: int = 2) -> bool:
    """两条 Clipper 整数路径是否近似相同（允许起点环形错位）。"""
    if len(a) != len(b) or not a:
        return False
    starts = [i for i, q in enumerate(b)
              if abs(q[0] - a[0][0]) <= tol and abs(q[1] - a[0][1]) <= tol]
    for s in starts[:4]:
        if all(abs(b[(s + i) % len(b)][0] - a[i][0]) <= tol
               and abs(b[(s + i) % len(b)][1] - a[i][1]) <= tol
               for i in range(len(a))):
            return True
    return False


def _dedupeClipperPaths(paths: list) -> list:
    """移除几何上几乎相同的重复轮廓。"""
    if len(paths) < 2:
        return paths
    kept: list = []
    for p in paths:
        if len(p) < 3:
            continue
        if any(_clipperPathsSimilar(p, q) for q in kept):
            continue
        kept.append(p)
    return kept


def _clipperBBoxSize(p: list) -> float:
    """路径包围盒最大边长（整数坐标），用于外轮廓判定。"""
    xs = [q[0] for q in p]
    ys = [q[1] for q in p]
    return max(max(xs) - min(xs), max(ys) - min(ys))


def clipperOffsetBothSides(pts: List[Point3D], totalWidth: float, closed: bool,
                           capStyle: str = "round",
                           keepInner: bool = False) -> Optional[List[List[Point3D]]]:
    """基于 Clipper(pyclipper) 的双侧偏移，返回闭合轮廓点列表（世界坐标）。

    - totalWidth 为总宽（与 Rhino 插件一致）：每侧偏移 totalWidth/2。
    - 开放曲线：单次偏移即得一条闭合外轮廓（双侧+端盖一体），
      capStyle='round' 圆头盖（端部半圆），'butt' 平头盖（端部垂直切平）。
    - 闭合曲线：分别 +d/-d 双向偏移得到外/内轮廓；
      keepInner=False 仅返回外轮廓（包围盒最大者），True 返回全部。
    - 自相交曲线 Clipper 自动处理（相交处断开/合并），无需走边法修剪。
    - 返回 None 表示 pyclipper 不可用（调用方回退）；[] 表示无结果
      （如内缩距离过大导致轮廓消失）。
    """
    if not _HAS_PYCLIPPER:
        return None
    if len(pts) < 2 or totalWidth <= 1e-9:
        return []
    z = pts[0].z
    scale = _CLIPPER_SCALE
    path = [(int(round(p.x * scale)), int(round(p.y * scale))) for p in pts]
    dedup = [path[0]]
    for q in path[1:]:
        if q != dedup[-1]:
            dedup.append(q)
    path = dedup
    if len(path) < 2:
        return []

    half = totalWidth / 2.0
    # 圆弧离散精度：实际 0.01 单位，但不大于偏移量的 1/10（避免极小偏移退化）
    arcTol = max(int(round(min(0.01, half / 10.0) * scale)), 2)

    def _run(delta: float, endType: int) -> list:
        co = pyclipper.PyclipperOffset()
        co.ArcTolerance = arcTol
        co.AddPath(path, pyclipper.JT_ROUND, endType)
        return co.Execute(int(round(delta * scale)))

    result: list = []
    if closed:
        # 闭合路径首尾需重合，保证偏移正确
        if path[0] != path[-1]:
            path = path + [path[0]]
        # 双向偏移：两个独立实例分别执行 +/-delta（避免状态互相影响导致内缩失效）
        result.extend(_run(half, pyclipper.ET_CLOSEDPOLYGON))
        result.extend(_run(-half, pyclipper.ET_CLOSEDPOLYGON))
    else:
        endType = (pyclipper.ET_OPENROUND if capStyle == "round"
                   else pyclipper.ET_OPENBUTT)
        result.extend(_run(half, endType))

    result = _dedupeClipperPaths(result)
    if not result:
        return []

    # 外轮廓判定：包围盒最大边长者（与 Rhino 插件一致，不计算面积）
    if not keepInner and len(result) > 1:
        outerIdx = max(range(len(result)),
                       key=lambda k: _clipperBBoxSize(result[k]))
        result = [result[outerIdx]]

    # 转回世界坐标，强制首尾重合（本项目闭合轮廓约定）
    out: List[List[Point3D]] = []
    for p in result:
        if len(p) < 3:
            continue
        world = [Point3D(q[0] / scale, q[1] / scale, z) for q in p]
        if (world[0].x - world[-1].x) ** 2 + \
                (world[0].y - world[-1].y) ** 2 > 1e-12:
            world.append(Point3D(world[0].x, world[0].y, z))
        out.append(world)
    return out
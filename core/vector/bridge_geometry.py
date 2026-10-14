"""桥接几何：按定位线段在闭合轮廓上开"料桥"缺口。

激光雕刻镂空时，挖件沿轮廓整圈切开会直接脱落；桥接在定位线（用户点
两个点画出）与闭合轮廓的交点处、按桥宽在切割路径上开缺口——缺口处
材料保留，把挖件连在板材上。

纯几何计算、不依赖 UI：输入采样折线（Point3D 列表，闭合折线首末点
重合，与 TrimCommand._getSamplePoints 输出一致），输出开桥后的保留段
与切除段（切除段供预览显示将要断开的位置）。
"""
from __future__ import annotations
from typing import List, Tuple
from core.vector.geometry_types import Point3D
from core.vector.fillet_processor import lineLineIntersection

_EPS = 1e-9


def polylineCumul(pts: List[Point3D]) -> List[float]:
    """折线累计弧长：cumul[i] = 顶点 pts[i] 处的弧长。"""
    cumul = [0.0]
    for i in range(len(pts) - 1):
        cumul.append(cumul[-1] + pts[i].distanceTo(pts[i + 1]))
    return cumul


def pointAtLength(pts: List[Point3D], cumul: List[float], s: float) -> Point3D:
    """弧长 s 处的精确插值点（s 夹取到 [0, 总长]）。"""
    total = cumul[-1]
    s = max(0.0, min(total, s))
    for i in range(len(pts) - 1):
        if s <= cumul[i + 1] or i == len(pts) - 2:
            seg = cumul[i + 1] - cumul[i]
            t = 0.0 if seg <= _EPS else (s - cumul[i]) / seg
            return pts[i] + (pts[i + 1] - pts[i]) * t
    return pts[-1]


def locatorCrossings(p1: Point3D, p2: Point3D, pts: List[Point3D]):
    """定位线段 p1p2 与折线的全部交点。

    返回 ([(轮廓弧长, 定位线参数, 交点), ...], cumul)：按轮廓弧长升序；
    定位线恰好穿过折线顶点时相邻两段各报一次交点，按弧长去重。"""
    cumul = polylineCumul(pts)
    found: List[Tuple[float, float, Point3D]] = []
    for i in range(len(pts) - 1):
        inter, t_loc, s = lineLineIntersection(p1, p2, pts[i], pts[i + 1])
        if inter is None:
            continue
        if -1e-9 <= t_loc <= 1.0 + 1e-9 and -1e-9 <= s <= 1.0 + 1e-9:
            seg = cumul[i + 1] - cumul[i]
            found.append((cumul[i] + seg * min(max(s, 0.0), 1.0), t_loc, inter))
    found.sort(key=lambda e: e[0])
    crossings: List[Tuple[float, float, Point3D]] = []
    for s, t_loc, p in found:
        if crossings and s - crossings[-1][0] <= 1e-6:
            continue
        crossings.append((s, t_loc, p))
    return crossings, cumul


def splitPolyline(pts: List[Point3D],
                  crossings: List[Tuple[float, float, Point3D]],
                  width: float, closed: bool):
    """按桥宽在交点处开缺口，返回 (保留段, 切除段) 两组点列。

    开放折线：缺口按弧长直接切除，保留段沿原顺序分段。
    闭合折线：接缝取相邻交点间最大弧（含跨缝弧）的中点——保证接缝离
    最近交点超过桥宽/2、任何缺口都不跨缝，旋转到接缝后按开放折线切段，
    最后把接缝两侧的保留段拼回一段。缺口布满整环（最大弧 ≤ 桥宽）时
    整体放弃，返回空。
    """
    cumul = polylineCumul(pts)
    total = cumul[-1]
    if total <= _EPS or width <= 0 or not crossings:
        return [], []
    if not closed:
        gaps = _clipMerge([(s - width * 0.5, s + width * 0.5) for s, _t, _p in crossings],
                          0.0, total)
        kept, removed = _splitOpen(pts, cumul, gaps)
        return ([seg for seg in kept if _polylineLen(seg) > 1e-6], removed)
    # 闭合：交点弧长归一化到 [0, total)——同一物理交点可能被报到 s 和
    # s+total 两处（交点恰在首末顶点），归一化后才能正确去重与规划接缝
    ss_raw = sorted(s % total for s, _t, _p in crossings)
    ss: List[float] = []
    for s in ss_raw:
        if not ss or s - ss[-1] > 1e-6:
            ss.append(s)
    if len(ss) >= 2 and ss[0] + total - ss[-1] <= 1e-6:
        ss.pop()   # 跨缝重复：首末同为一点
    arcs = [ss[i + 1] - ss[i] for i in range(len(ss) - 1)]
    arcs.append(ss[0] + total - ss[-1])
    k = max(range(len(arcs)), key=lambda i: arcs[i])
    if arcs[k] <= width:
        return [], []
    seam = (ss[k] + arcs[k] * 0.5) % total
    rot = _rotateRing(pts, cumul, seam)
    rot_cumul = polylineCumul(rot)
    gaps = _clipMerge([((s - seam) % total - width * 0.5,
                        (s - seam) % total + width * 0.5) for s in ss],
                      0.0, total)
    kept, removed = _splitOpen(rot, rot_cumul, gaps)
    if len(kept) >= 2:
        # 接缝不在任何缺口内：首段起点与末段终点同为接缝点，拼回一段
        kept = [kept[-1] + kept[0][1:]] + kept[1:-1]
    return [seg for seg in kept if _polylineLen(seg) > 1e-6], removed


# ==================== 内部辅助 ====================

def _polylineLen(pts: List[Point3D]) -> float:
    """折线总长（丢弃亚微米碎段用）。"""
    return sum(pts[i].distanceTo(pts[i + 1]) for i in range(len(pts) - 1))


def _dedupPts(pts: List[Point3D]) -> List[Point3D]:
    """去掉相邻重复点（缺口边界落在顶点上时会产生重合点）。"""
    out = [pts[0]]
    for p in pts[1:]:
        if p.distanceTo(out[-1]) > 1e-7:
            out.append(p)
    return out


def _verticesBetween(pts: List[Point3D], cumul: List[float],
                     lo: float, hi: float) -> List[Point3D]:
    """弧长开区间 (lo, hi) 内的原始顶点（区间端点由调用方精确插入）。"""
    return [pts[i] for i in range(1, len(pts) - 1) if lo < cumul[i] < hi]


def _clipMerge(raw: List[Tuple[float, float]],
               lo: float, hi: float) -> List[Tuple[float, float]]:
    """区间裁剪到 [lo, hi] 后合并重叠（按起点排序线性合并）。"""
    out: List[Tuple[float, float]] = []
    for gs, ge in sorted(raw):
        gs, ge = max(lo, gs), min(hi, ge)
        if ge - gs <= _EPS:
            continue
        if out and gs <= out[-1][1] + _EPS:
            if ge > out[-1][1]:
                out[-1] = (out[-1][0], ge)
        else:
            out.append((gs, ge))
    return out


def _splitOpen(pts: List[Point3D], cumul: List[float],
               gaps: List[Tuple[float, float]]):
    """开放折线按有序不重叠缺口切段，返回 (保留段, 切除段)。"""
    total = cumul[-1]
    kept: List[List[Point3D]] = []
    removed: List[List[Point3D]] = []
    cursor = 0.0
    cur = [pts[0]]
    for gs, ge in gaps:
        if gs - cursor > _EPS:
            cur.extend(_verticesBetween(pts, cumul, cursor, gs))
            cur.append(pointAtLength(pts, cumul, gs))
        elif cur:
            cur[-1] = pointAtLength(pts, cumul, gs)
        if len(cur) >= 2:
            kept.append(_dedupPts(cur))
        chunk = [pointAtLength(pts, cumul, gs)]
        chunk.extend(_verticesBetween(pts, cumul, gs, ge))
        chunk.append(pointAtLength(pts, cumul, ge))
        removed.append(_dedupPts(chunk))
        cur = [pointAtLength(pts, cumul, ge)]
        cursor = ge
    if total - cursor > _EPS:
        cur.extend(_verticesBetween(pts, cumul, cursor, total))
    cur.append(pts[-1])
    if len(cur) >= 2:
        kept.append(_dedupPts(cur))
    return kept, removed


def _rotateRing(pts: List[Point3D], cumul: List[float],
                seam: float) -> List[Point3D]:
    """闭合折线（首末重合）旋转到接缝：返回首末为接缝精确点的开环。"""
    k = 0
    for i in range(len(pts)):
        if cumul[i] <= seam + _EPS:
            k = i
        else:
            break
    a = pointAtLength(pts, cumul, seam)
    return ([a] + list(pts[k + 1:len(pts) - 1]) + [pts[0]]
            + list(pts[1:k + 1]) + [a])

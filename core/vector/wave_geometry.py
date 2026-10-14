"""波浪几何生成：波浪圆 / 波浪矩形（移植自原 C++ 项目）。

原版用 NURBS 有理弧段表达，本实现按项目纯 Python 路线简化为
采样折线（闭合 LineObject 可直接承载），几何语义与原版一致：
- 波浪圆：内圆上 N 个连接点，每段外凸弧经过外圆上的弧顶；
- 波浪矩形：长/短边各带独立波数的外凸波浪，角部带角波浪，
  波形支持 直线（折线）/ 平滑（正弦采样）/ 圆弧（过三点弧采样）。
"""
from __future__ import annotations
import math
from typing import List, Tuple

from .geometry_types import Point3D
from .arc_object import computeArcFromThreePoints

_WAVE_SAMPLES = 16   # 每个凸起（半波）的采样数


# ==================== 公共工具 ====================

def _sampleArcThrough(p1: Point3D, pMid: Point3D, p3: Point3D,
                      samples: int) -> List[Point3D]:
    """采样经过 pMid 一侧的过三点弧；退化时回退直线。含首尾点。"""
    res = computeArcFromThreePoints(p1, pMid, p3)
    if res is None:
        out = []
        for k in range(samples + 1):
            t = k / samples
            out.append(Point3D(p1.x + (p3.x - p1.x) * t,
                               p1.y + (p3.y - p1.y) * t, p1.z))
        return out
    center, radius, sa, ea = res
    span = ea - sa
    while span < 0:
        span += 2 * math.pi
    ma = math.atan2(pMid.y - center.y, pMid.x - center.x)
    delta = ma - sa
    while delta < 0:
        delta += 2 * math.pi
    if delta > span:
        span -= 2 * math.pi
    out = []
    for k in range(samples + 1):
        a = sa + span * k / samples
        out.append(Point3D(center.x + radius * math.cos(a),
                           center.y + radius * math.sin(a), p1.z))
    return out


def _bumpPoints(s: Point3D, e: Point3D, peak: Point3D, waveType: int,
                samples: int) -> List[Point3D]:
    """单个凸起（起点 s → 峰 peak → 终点 e）按波形采样，含首尾点。"""
    if waveType == 0:
        return [s, peak, e]
    if waveType == 2:
        return _sampleArcThrough(s, peak, e, samples)
    # 平滑（默认）：沿弦正弦偏移
    dx, dy = e.x - s.x, e.y - s.y
    L = math.hypot(dx, dy)
    if L < 1e-9:
        return [s, e]
    nx, ny = (peak.x - (s.x + e.x) * 0.5, peak.y - (s.y + e.y) * 0.5)
    out = []
    for k in range(samples + 1):
        t = k / samples
        off = math.sin(math.pi * t)
        out.append(Point3D(s.x + dx * t + nx * off,
                           s.y + dy * t + ny * off, s.z))
    return out


def _walkTo(pts: List[Point3D], startIdx: int, direction: int,
            dist: float) -> Tuple[Point3D, int]:
    """沿闭合折线从 startIdx 向 direction(+1/-1) 走 dist 长度，
    返回 (修剪点, 该点外侧保留的顶点索引)。"""
    n = len(pts)
    acc = 0.0
    i = startIdx
    for _ in range(n):
        j = (i + direction) % n
        seg = math.hypot(pts[j].x - pts[i].x, pts[j].y - pts[i].y)
        if acc + seg >= dist:
            t = (dist - acc) / seg if seg > 1e-12 else 0.0
            tp = Point3D(pts[i].x + (pts[j].x - pts[i].x) * t,
                         pts[i].y + (pts[j].y - pts[i].y) * t, pts[i].z)
            return tp, j
        acc += seg
        i = j
    return Point3D(pts[startIdx].x, pts[startIdx].y, pts[startIdx].z), startIdx


def _sampleFilletArc(a: Point3D, c: Point3D, b: Point3D, theta: float,
                     samples: int) -> List[Point3D]:
    """采样倒角圆弧（不含端点 a/b）：有理二次贝塞尔，控制点=角点 c，
    权重=cos(θ/2)，与原版 NURBS 倒角弧（addArcSegmentWorld）等价，
    结果恰为半径 r、两侧相切的圆弧。"""
    w = math.cos(theta * 0.5)
    out: List[Point3D] = []
    for k in range(1, samples):
        t = k / samples
        w0 = (1.0 - t) * (1.0 - t)
        w1 = 2.0 * w * t * (1.0 - t)
        w2 = t * t
        W = w0 + w1 + w2
        out.append(Point3D((w0 * a.x + w1 * c.x + w2 * b.x) / W,
                           (w0 * a.y + w1 * c.y + w2 * b.y) / W, a.z))
    return out


def filletClosedPolyline(pts: List[Point3D], junctions: List[int],
                         r: float, samples: int = 12,
                         rounded: bool = True) -> List[Point3D]:
    """在闭合折线的指定顶点处做倒角。

    与原版波浪圆倒角同算法：按转角 θ（=π−内角α）修剪两侧各
    t = r/tan(α/2) = r·tan(θ/2)，
    修剪量不超过相邻连接点间折线长的 45%；修剪区内原顶点全部剔除，
    不留多余线段。rounded=True 时修剪点间插入半径 r、两侧相切的
    圆弧采样点（圆角）；否则修剪点直连（倒直角）。
    """
    n = len(pts)
    if not junctions or r <= 1e-9 or n < 4:
        return list(pts)
    js = sorted(set(j for j in junctions if 0 <= j < n))
    if not js:
        return list(pts)

    def _segLen(a: int, b: int) -> float:
        acc = 0.0
        for i in range(a, b):
            p, q = pts[i % n], pts[(i + 1) % n]
            acc += math.hypot(q.x - p.x, q.y - p.y)
        return acc

    # 展开索引（每个连接点映射到 j 与 j+n 两个副本，修剪区间天然不跨界）。
    m = len(js)
    ext = list(js) + [j + n for j in js]

    def _info(idx: int):
        """连接点 idx 的 (修剪长, 转角)；平直/退化返回 None。"""
        j = ext[idx]
        ip, jn = (j - 1) % n, (j + 1) % n
        dx1, dy1 = pts[j % n].x - pts[ip].x, pts[j % n].y - pts[ip].y
        dx2, dy2 = pts[jn].x - pts[j % n].x, pts[jn].y - pts[j % n].y
        l1 = math.hypot(dx1, dy1)
        l2 = math.hypot(dx2, dy2)
        if l1 < 1e-12 or l2 < 1e-12:
            return None
        cosT = max(-1.0, min(1.0, (dx1 * dx2 + dy1 * dy2) / (l1 * l2)))
        theta = math.acos(cosT)   # 转角 = π−内角α
        if theta < 1e-6 or theta > math.pi - 1e-6:
            return None
        # 切距 = r/tan(α/2) = r·tan(θ/2)（旧式 r/tan(θ/2) 在非 90° 角处
        # 切距反向偏离；90° 时两者相等故矩形测试不暴露）
        t = r * math.tan(theta * 0.5)
        # 修剪量不超过相邻连接点间折线长的 45%（两侧独立钳制）；
        # idx=0 的前驱段跨闭合处：展开坐标为 js[m-1] → js[0]+n。
        pJ = js[idx - 1] if idx > 0 else js[-1]
        jEnd = j + n if idx == 0 else j
        t = min(t, _segLen(pJ, jEnd) * 0.45,
                _segLen(j, ext[idx + 1]) * 0.45)
        return (t, theta) if t > 1e-9 else None

    infos = [_info(i) for i in range(m)]
    start = next((i for i in range(m) if infos[i] is not None), None)
    if start is None:
        return list(pts)

    out: List[Point3D] = []

    def _emitCorner(idx: int) -> None:
        t, th = infos[idx]
        j = ext[idx] % n
        tb, _b = _walkTo(pts, j, -1, t)
        tf, _f = _walkTo(pts, j, +1, t)
        if rounded:
            out.extend([tb] + _sampleFilletArc(tb, pts[j], tf, th,
                                               samples) + [tf])
        else:
            out.extend([tb, tf])   # 倒直角：修剪点直连

    # 从首个有效修剪点出发绕行一周：先出 start 的倒角，再依次处理
    # 后续 m-1 个连接点及其间隙顶点，最后补上回到 start 的绕圈间隙。
    _emitCorner(start)
    for k in range(start + 1, start + m + 1):
        idx = k % m
        # 间隙顶点：上一连接点之后 → 当前连接点之前（展开坐标）；
        # 末段绕圈间隙的末端即 start 连接点：已发倒角则不包含，
        # 未发倒角（保留原顶点）时需包含以免丢点。
        lastGap = k == start + m
        a, b = ext[k - 1], ext[k]
        gEnd = b - 1 if (lastGap and infos[start] is not None) else b
        # 两侧修剪距离：落在修剪区内的原顶点必须剔除（否则残留多余线段）
        infA = infos[(k - 1) % m]
        dA = infA[0] if infA is not None else 0.0
        dB = infos[start][0] if lastGap else \
            (infos[idx][0] if infos[idx] is not None else 0.0)
        total = _segLen(a, b)
        dFromA = 0.0
        for g in range(a + 1, gEnd):
            p0, p1 = pts[(g - 1) % n], pts[g % n]
            dFromA += math.hypot(p1.x - p0.x, p1.y - p0.y)
            if dFromA >= dA - 1e-9 and total - dFromA >= dB - 1e-9:
                out.append(pts[g % n])
        if lastGap:
            break
        if infos[idx] is not None:
            _emitCorner(idx)
        else:
            out.append(pts[ext[k] % n])   # 不倒角：保留原顶点
    return out


# ==================== 波浪圆 ====================

def buildWaveCirclePoints(center: Point3D, innerR: float, bulgeR: float,
                          arcCount: int, chamferR: float = 0.0,
                          waveType: int = 2,
                          chamferRounded: bool = True,
                          samples: int = 16) -> List[Point3D]:
    """生成波浪圆闭合点列（首尾不重复）。

    内圆半径 innerR 上均布 arcCount 个连接点，每段凸起经过
    外圆半径 bulgeR 上的波峰；chamferR>0 时在连接点做倒角，
    chamferRounded 控制圆角（圆弧）/倒直角（直连）。
    waveType: 0=直线（折线三角波） 1=样条（平滑正弦） 2=圆弧（默认）。
    """
    if innerR < 1e-9 or bulgeR < 1e-9 or not (2 <= arcCount <= 36):
        return []
    step = 2 * math.pi / arcCount
    pts: List[Point3D] = []
    junctions: List[int] = []
    for i in range(arcCount):
        a0 = step * i
        am = a0 + step * 0.5
        a1 = a0 + step
        p1 = Point3D(center.x + innerR * math.cos(a0),
                     center.y + innerR * math.sin(a0), center.z)
        pm = Point3D(center.x + bulgeR * math.cos(am),
                     center.y + bulgeR * math.sin(am), center.z)
        p3 = Point3D(center.x + innerR * math.cos(a1),
                     center.y + innerR * math.sin(a1), center.z)
        seg = _bumpPoints(p1, p3, pm, waveType, samples)
        if i == 0:
            pts.extend(seg)
        else:
            pts.extend(seg[1:])   # 首点与上段尾点重合，跳过
        junctions.append((len(pts) - 1) % max(len(pts), 1))
    # 闭合处首尾重合修正：末点即首连接点
    if pts and math.hypot(pts[-1].x - pts[0].x, pts[-1].y - pts[0].y) < 1e-9:
        pts.pop()
    junctions = [j % max(len(pts), 1) for j in junctions[:-1]] + [0]
    if chamferR > 1e-9:
        pts = filletClosedPolyline(pts, junctions, chamferR,
                                   rounded=chamferRounded)
    return pts


# ==================== 波浪矩形 ====================

def buildWaveRectanglePoints(p1: Point3D, p2: Point3D, waveType: int,
                             longCount: int, shortCount: int,
                             sideH: float, cornerH: float,
                             cornerWidth: float,
                             cornerChamfer: float = 0.0,
                             samples: int = _WAVE_SAMPLES) -> List[Point3D]:
    """生成波浪矩形闭合点列。

    每条边裁掉 cornerWidth/2 后按波数生成外凸波浪（长边用
    longCount、短边用 shortCount），角部生成角波浪（高 cornerH）。
    cornerH=0 时为直角角点，cornerChamfer>0 时对角点做直线倒角。
    waveType: 0=直线折线 1=平滑正弦 2=圆弧。
    """
    x1, y1, x2, y2 = p1.x, p1.y, p2.x, p2.y
    w, h = abs(x2 - x1), abs(y2 - y1)
    if w < 1e-9 or h < 1e-9:
        return []
    corners = [Point3D(x1, y1, p1.z), Point3D(x2, y1, p1.z),
               Point3D(x2, y2, p1.z), Point3D(x1, y2, p1.z)]
    longCount = max(1, int(longCount))
    shortCount = max(1, int(shortCount))
    sideH = max(sideH, 0.0)
    cornerH = max(cornerH, 0.0)
    cornerWidth = max(cornerWidth, 0.0)
    cornerChamfer = max(cornerChamfer, 0.0) if cornerH < 1e-9 else 0.0

    out: List[Point3D] = []
    for e in range(4):
        A, B = corners[e], corners[(e + 1) % 4]
        dx, dy = B.x - A.x, B.y - A.y
        L = math.hypot(dx, dy)
        dirX, dirY = dx / L, dy / L
        # 内法线：波浪凸起朝向矩形内部
        nX, nY = -dirY, dirX
        # 角波宽仅在角波浪启用时裁边；倒角量另计（两者取大）
        base = cornerWidth * 0.5 if cornerH > 1e-9 else 0.0
        trim = min(max(base, cornerChamfer), L * 0.25)
        As = Point3D(A.x + dirX * trim, A.y + dirY * trim, A.z)
        Bs = Point3D(B.x - dirX * trim, B.y - dirY * trim, A.z)
        segLen = math.hypot(Bs.x - As.x, Bs.y - As.y)
        count = longCount if L >= max(w, h) - 1e-9 else shortCount
        if segLen < 1e-9 or sideH < 1e-9 or count <= 0:
            edgePts = [As, Bs]
        else:
            edgePts = []
            bl = segLen / count
            for k in range(count):
                s = Point3D(As.x + dirX * bl * k, As.y + dirY * bl * k, A.z)
                ee = Point3D(As.x + dirX * bl * (k + 1),
                             As.y + dirY * bl * (k + 1), A.z)
                peak = Point3D((s.x + ee.x) * 0.5 + nX * sideH,
                               (s.y + ee.y) * 0.5 + nY * sideH, A.z)
                bump = _bumpPoints(s, ee, peak, waveType, samples)
                edgePts.extend(bump if k == 0 else bump[1:])
        # 追加边点（跳过与上段重合的首点）
        out.extend(edgePts if e == 0 else edgePts[1:])
        # 角波浪：当前边尾 → 角顶外侧 → 下一条边首
        C = corners[(e + 1) % 4]
        nxt = corners[(e + 2) % 4]
        ndx, ndy = nxt.x - C.x, nxt.y - C.y
        nL = math.hypot(ndx, ndy)
        # 下一条边的内法线（左法线，与当前边一致）：此前误用右法线，
        # 导致角波峰朝矩形外侧，直线/平滑模式的角部方向不对。
        nnX, nnY = -ndy / nL, ndx / nL
        bx, by = nX + nnX, nY + nnY
        bL = math.hypot(bx, by)
        if bL < 1e-9:
            bx, by, bL = nX, nY, 1.0
        bx, by = bx / bL, by / bL
        nextTrim = min(max(cornerWidth * 0.5 if cornerH > 1e-9 else 0.0,
                           cornerChamfer), nL * 0.25)
        nxtStart = Point3D(C.x + (ndx / nL) * nextTrim,
                           C.y + (ndy / nL) * nextTrim, C.z)
        if cornerH < 1e-9:
            if cornerChamfer > 1e-9:
                # 角点倒角：角点两侧各退 cornerChamfer 直连（含收口角）
                cc = min(cornerChamfer, trim, nextTrim)
                out.append(Point3D(C.x - dirX * cc, C.y - dirY * cc, C.z))
                out.append(Point3D(C.x + (ndx / nL) * cc,
                                   C.y + (ndy / nL) * cc, C.z))
            elif e < 3:
                out.append(C)   # 直角角点（收口角由闭合线段自然经过）
        else:
            peak = Point3D(C.x + bx * cornerH, C.y + by * cornerH, C.z)
            bump = _bumpPoints(out[-1], nxtStart, peak, waveType, samples)
            out.extend(bump[1:])
            if e == 3 and out and math.hypot(out[-1].x - out[0].x,
                                             out[-1].y - out[0].y) < 1e-9:
                out.pop()   # 闭合处末点与首点重合，去重
    return out

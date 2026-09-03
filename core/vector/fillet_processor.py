"""倒角/圆角几何计算模块（纯算法，零 Qt 依赖）。

移植自原 C++ 项目 FilletProcessor：
- 直线-直线交点（参数化，含共面性检查）
- 圆角几何：交点、圆心、切点、圆弧中点
- 斜角几何：交点、修剪点
- 点到线段最近点（边拾取）
- 多段线批量角点圆角/斜角
- 闭合多边形顶点倒角（有理二次 Bezier 采样圆弧）
"""
from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Set

from .geometry_types import Point3D

# ==================== 常量（对应 FilletConstants.h） ====================

EPSILON_RADIUS = 1e-6
EPSILON_LENGTH = 1e-9
EPSILON_COPLANAR = 1e-3
PICK_PIXEL_RADIUS = 15.0
SHARP_ANGLE_MAX_RAD = math.pi * 165.0 / 180.0
ARC_SAMPLES = 12


# ==================== 结果结构 ====================

@dataclass
class FilletResult:
    """圆角计算结果。"""
    valid: bool = False
    intersection: Point3D = field(default_factory=Point3D)
    center: Point3D = field(default_factory=Point3D)
    trimPt1: Point3D = field(default_factory=Point3D)
    trimPt2: Point3D = field(default_factory=Point3D)
    arcMid: Point3D = field(default_factory=Point3D)
    arcWeight: float = 1.0


@dataclass
class BevelResult:
    """斜角计算结果。"""
    valid: bool = False
    intersection: Point3D = field(default_factory=Point3D)
    trimPt1: Point3D = field(default_factory=Point3D)
    trimPt2: Point3D = field(default_factory=Point3D)


@dataclass
class CornerResult:
    """单个角点的倒角结果（整线批量模式）。"""
    valid: bool = False
    cornerIndex: int = -1
    intersection: Point3D = field(default_factory=Point3D)
    trimPt1: Point3D = field(default_factory=Point3D)
    trimPt2: Point3D = field(default_factory=Point3D)
    center: Point3D = field(default_factory=Point3D)
    arcMid: Point3D = field(default_factory=Point3D)
    arcWeight: float = 1.0


# ==================== 内部辅助 ====================

def _computeEdgeDirections(seg1Start: Point3D, seg1End: Point3D,
                           seg2Start: Point3D, seg2End: Point3D,
                           intersection: Point3D):
    """计算两条边从交点出发指向各自远端端点的单位方向。"""
    toS1 = seg1Start - intersection
    toE1 = seg1End - intersection
    toS2 = seg2Start - intersection
    toE2 = seg2End - intersection
    if toS1.length() < EPSILON_LENGTH and toE1.length() < EPSILON_LENGTH:
        return None
    if toS2.length() < EPSILON_LENGTH and toE2.length() < EPSILON_LENGTH:
        return None
    d1 = toE1 if toE1.length() >= toS1.length() else toS1
    d2 = toE2 if toE2.length() >= toS2.length() else toS2
    len1 = d1.length()
    len2 = d2.length()
    if len1 < EPSILON_LENGTH or len2 < EPSILON_LENGTH:
        return None
    return d1 * (1.0 / len1), d2 * (1.0 / len2)


def isSharpCorner(prev: Point3D, corner: Point3D, nxt: Point3D) -> bool:
    """角点是否为可倒角的尖角（夹角小于 165° 且两边非退化）。"""
    v1 = corner - prev
    v2 = nxt - corner
    len1 = v1.length()
    len2 = v2.length()
    if len1 < EPSILON_LENGTH or len2 < EPSILON_LENGTH:
        return False
    cosAngle = max(-1.0, min(1.0, v1.dot(v2) / (len1 * len2)))
    return math.acos(cosAngle) < SHARP_ANGLE_MAX_RAD


# ==================== 几何计算 ====================

def lineLineIntersection(p1: Point3D, p2: Point3D, p3: Point3D, p4: Point3D):
    """两直线交点（参数化最小二乘）。

    返回 (交点, t, s)；平行或异面不相交时返回 (None, 0, 0)。
    """
    d1 = p2 - p1
    d2 = p4 - p3
    c = d1.cross(d2)
    if c.dot(c) < 1e-12:
        return None, 0.0, 0.0

    w = p3 - p1
    a = d1.dot(d1)
    b = d1.dot(d2)
    c2 = d2.dot(d2)
    d = d1.dot(w)
    e = d2.dot(w)

    denom = a * c2 - b * b
    if abs(denom) < 1e-12:
        return None, 0.0, 0.0

    t = (c2 * d - b * e) / denom
    s = (b * d - a * e) / denom

    i1 = p1 + d1 * t
    i2 = p3 + d2 * s
    if (i2 - i1).length() > EPSILON_COPLANAR:
        return None, 0.0, 0.0

    return Point3D((i1.x + i2.x) * 0.5, (i1.y + i2.y) * 0.5,
                   (i1.z + i2.z) * 0.5), t, s


def nearestPointOnSegment(segStart: Point3D, segEnd: Point3D,
                          testPoint: Point3D):
    """点到线段最近点，返回 (最近点, t)。"""
    d = segEnd - segStart
    len2 = d.dot(d)
    if len2 < 1e-12:
        return Point3D(segStart.x, segStart.y, segStart.z), 0.0
    t = (testPoint - segStart).dot(d) / len2
    t = max(0.0, min(1.0, t))
    return segStart + d * t, t


def computeFillet(seg1Start: Point3D, seg1End: Point3D,
                  seg2Start: Point3D, seg2End: Point3D,
                  radius: float) -> FilletResult:
    """给定两条直线与半径，计算圆角（交点/圆心/切点/弧中点）。"""
    result = FilletResult()
    if radius < EPSILON_RADIUS:
        return result

    inter, _, _ = lineLineIntersection(seg1Start, seg1End, seg2Start, seg2End)
    if inter is None:
        return result
    result.intersection = inter

    dirs = _computeEdgeDirections(seg1Start, seg1End, seg2Start, seg2End, inter)
    if dirs is None:
        return result
    dir1, dir2 = dirs

    cosA = max(-1.0, min(1.0, dir1.dot(dir2)))
    halfAngle = math.acos(cosA) * 0.5
    sinHalf = math.sin(halfAngle)
    if sinHalf < EPSILON_LENGTH:
        return result

    tangentDist = radius / math.tan(halfAngle)
    result.trimPt1 = inter + dir1 * tangentDist
    result.trimPt2 = inter + dir2 * tangentDist

    bisector = dir1 + dir2
    bisLen = bisector.length()
    if bisLen < EPSILON_LENGTH:
        return result
    bisector = bisector * (1.0 / bisLen)

    centerDist = radius / sinHalf
    result.center = inter + bisector * centerDist

    # 圆弧中点：沿两切点方向之和归一化
    v1 = (result.trimPt1 - result.center) * (1.0 / radius)
    v2 = (result.trimPt2 - result.center) * (1.0 / radius)
    d = v1.dot(v2)
    denom = 1.0 + d
    if denom > EPSILON_LENGTH:
        s = result.trimPt1 + result.trimPt2 - result.center * 2.0
        k = radius / denom
        result.arcMid = result.center + s * k
    else:
        bis = (result.trimPt1 + result.trimPt2) * 0.5 - result.center
        bisLen2 = bis.length()
        if bisLen2 > EPSILON_LENGTH:
            result.arcMid = result.center + bis * (radius / bisLen2)
        else:
            result.arcMid = Point3D(inter.x, inter.y, inter.z)

    result.arcWeight = math.cos(halfAngle)
    result.valid = True
    return result


def computeBevel(seg1Start: Point3D, seg1End: Point3D,
                 seg2Start: Point3D, seg2End: Point3D,
                 d1: float, d2: float) -> BevelResult:
    """给定两条直线与两个距离，计算斜角修剪点。"""
    result = BevelResult()
    if d1 < EPSILON_RADIUS or d2 < EPSILON_RADIUS:
        return result

    inter, _, _ = lineLineIntersection(seg1Start, seg1End, seg2Start, seg2End)
    if inter is None:
        return result
    result.intersection = inter

    dirs = _computeEdgeDirections(seg1Start, seg1End, seg2Start, seg2End, inter)
    if dirs is None:
        return result
    dir1, dir2 = dirs

    result.trimPt1 = inter + dir1 * d1
    result.trimPt2 = inter + dir2 * d2
    result.valid = True
    return result


def cornerFromProjection(seg1Start: Point3D, seg1End: Point3D,
                         seg2Start: Point3D, seg2End: Point3D,
                         mousePos: Point3D, bevel: bool = False) -> CornerResult:
    """按鼠标在第二条边上的投影位置计算倒角结果（悬停预览交互）。

    鼠标投影点即边2的修剪点，边1取相同修剪距离（对称倒角）：
    圆角模式下半径由修剪距离与夹角推导，斜角模式下即对称距离。
    边平行、退化或投影落在交点上时返回 valid=False。
    """
    result = CornerResult(valid=False)
    inter, _, _ = lineLineIntersection(seg1Start, seg1End, seg2Start, seg2End)
    if inter is None:
        return result
    result.intersection = inter

    proj, _ = nearestPointOnSegment(seg2Start, seg2End, mousePos)
    v2 = proj - inter
    dist2 = v2.length()
    if dist2 < EPSILON_LENGTH:
        return result
    dir2 = v2 * (1.0 / dist2)

    # 边1取离交点较远的端点一侧（与 computeFillet 的方向约定一致）
    toS1 = seg1Start - inter
    toE1 = seg1End - inter
    d1v = toE1 if toE1.length() >= toS1.length() else toS1
    len1 = d1v.length()
    if len1 < EPSILON_LENGTH:
        return result
    dir1 = d1v * (1.0 / len1)

    # 修剪距离不得超过边1可用长度，避免切过头
    trim = min(dist2, len1)
    result.trimPt1 = inter + dir1 * trim
    result.trimPt2 = inter + dir2 * trim

    if bevel:
        result.valid = True
        return result

    cosA = max(-1.0, min(1.0, dir1.dot(dir2)))
    halfAngle = math.acos(cosA) * 0.5
    sinHalf = math.sin(halfAngle)
    if sinHalf < EPSILON_LENGTH:
        return result
    radius = trim * math.tan(halfAngle)

    bisector = dir1 + dir2
    bisLen = bisector.length()
    if bisLen < EPSILON_LENGTH:
        return result
    bisector = bisector * (1.0 / bisLen)
    result.center = inter + bisector * (radius / sinHalf)

    # 圆弧中点：沿两切点方向之和归一化
    v1u = (result.trimPt1 - result.center) * (1.0 / radius)
    v2u = (result.trimPt2 - result.center) * (1.0 / radius)
    denom = 1.0 + v1u.dot(v2u)
    if denom > EPSILON_LENGTH:
        s = result.trimPt1 + result.trimPt2 - result.center * 2.0
        result.arcMid = result.center + s * (radius / denom)
    else:
        bis = (result.trimPt1 + result.trimPt2) * 0.5 - result.center
        bisLen2 = bis.length()
        if bisLen2 < EPSILON_LENGTH:
            return result
        result.arcMid = result.center + bis * (radius / bisLen2)
    result.arcWeight = math.cos(halfAngle)
    result.valid = True
    return result


def sampleFilletArc(fr: FilletResult, samples: int = ARC_SAMPLES) -> List[Point3D]:
    """按圆角结果采样圆弧点（不含两端切点），方向经过 arcMid 一侧。"""
    if not fr.valid:
        return []
    c = fr.center
    a1 = math.atan2(fr.trimPt1.y - c.y, fr.trimPt1.x - c.x)
    a2 = math.atan2(fr.trimPt2.y - c.y, fr.trimPt2.x - c.x)
    am = math.atan2(fr.arcMid.y - c.y, fr.arcMid.x - c.x)

    span = a2 - a1
    while span <= -math.pi:
        span += 2 * math.pi
    while span > math.pi:
        span -= 2 * math.pi
    # 若短弧未经过弧中点，则走另一侧
    midOfShort = a1 + span * 0.5
    dm = (am - midOfShort) % (2 * math.pi)
    if dm > math.pi:
        dm -= 2 * math.pi
    if abs(dm) > math.pi * 0.5:
        span = span - math.copysign(2 * math.pi, span) if span != 0 else 2 * math.pi

    pts: List[Point3D] = []
    r = fr.trimPt1.distanceTo(c)
    for i in range(1, samples + 1):
        t = i / (samples + 1)
        a = a1 + span * t
        pts.append(Point3D(c.x + r * math.cos(a),
                           c.y + r * math.sin(a), c.z))
    return pts


# ==================== 批量角点计算 ====================

def computeAllCornersFillet(points: Sequence[Point3D], radius: float,
                            sharpCorners: Optional[Set[int]] = None) -> List[CornerResult]:
    """批量计算折线所有内角点的圆角。"""
    results: List[CornerResult] = []
    if len(points) < 3 or radius < EPSILON_RADIUS:
        return results
    for i in range(1, len(points) - 1):
        cr = CornerResult(cornerIndex=i)
        if sharpCorners is not None and i not in sharpCorners:
            results.append(cr)
            continue
        prev, corner, nxt = points[i - 1], points[i], points[i + 1]
        if not isSharpCorner(prev, corner, nxt):
            results.append(cr)
            continue
        fr = computeFillet(prev, corner, corner, nxt, radius)
        if not fr.valid:
            results.append(cr)
            continue
        cr.valid = True
        cr.intersection = fr.intersection
        cr.trimPt1 = fr.trimPt1
        cr.trimPt2 = fr.trimPt2
        cr.center = fr.center
        cr.arcMid = fr.arcMid
        cr.arcWeight = fr.arcWeight
        results.append(cr)
    return results


def computeAllCornersBevel(points: Sequence[Point3D], d1: float, d2: float,
                           sharpCorners: Optional[Set[int]] = None) -> List[CornerResult]:
    """批量计算折线所有内角点的斜角。"""
    results: List[CornerResult] = []
    if len(points) < 3 or d1 < EPSILON_RADIUS or d2 < EPSILON_RADIUS:
        return results
    for i in range(1, len(points) - 1):
        cr = CornerResult(cornerIndex=i)
        if sharpCorners is not None and i not in sharpCorners:
            results.append(cr)
            continue
        prev, corner, nxt = points[i - 1], points[i], points[i + 1]
        if not isSharpCorner(prev, corner, nxt):
            results.append(cr)
            continue
        br = computeBevel(prev, corner, corner, nxt, d1, d2)
        if not br.valid:
            results.append(cr)
            continue
        cr.valid = True
        cr.intersection = br.intersection
        cr.trimPt1 = br.trimPt1
        cr.trimPt2 = br.trimPt2
        results.append(cr)
    return results


# ==================== 闭合多边形顶点倒角 ====================

def applyFilletToVertices(vertices: Sequence[Point3D], fr: float,
                         arcSamples: int = ARC_SAMPLES) -> List[Point3D]:
    """对闭合多边形每个顶点切出圆角，用有理二次 Bezier 采样圆弧。

    移植自原项目 PolygonCommandInstance::applyFilletToVertices。
    返回展开后的点列（仍为闭合轮廓，首尾不重复）。
    """
    result: List[Point3D] = []
    n = len(vertices)
    if n < 3 or fr < EPSILON_RADIUS:
        return list(vertices)

    for i in range(n):
        V = vertices[i]
        P = vertices[(i - 1 + n) % n]
        N = vertices[(i + 1) % n]

        e1 = P - V
        e2 = N - V
        len1 = e1.length()
        len2 = e2.length()
        if len1 < 1e-4 or len2 < 1e-4:
            continue
        e1 = e1 * (1.0 / len1)
        e2 = e2 * (1.0 / len2)

        cutLen = min(fr, len1 * 0.49, len2 * 0.49)
        T1 = Point3D(V.x + e1.x * cutLen, V.y + e1.y * cutLen, V.z)
        T2 = Point3D(V.x + e2.x * cutLen, V.y + e2.y * cutLen, V.z)
        result.append(T1)

        dot = e1.dot(e2)
        cross = e1.x * e2.y - e1.y * e2.x
        arcAngle = abs(math.atan2(cross, dot))
        w = math.cos(arcAngle / 2.0)
        if w < 0.05:
            w = 0.05

        for s in range(1, arcSamples + 1):
            t = s / (arcSamples + 1)
            b0 = (1 - t) * (1 - t)
            b1 = 2 * t * (1 - t) * w
            b2 = t * t
            denom = b0 + b1 + b2
            if denom < 1e-4:
                continue
            result.append(Point3D(
                (b0 * T1.x + b1 * V.x + b2 * T2.x) / denom,
                (b0 * T1.y + b1 * V.y + b2 * T2.y) / denom,
                (b0 * T1.z + b1 * V.z + b2 * T2.z) / denom,
            ))
        result.append(T2)
    return result

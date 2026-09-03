"""样条曲线数学工具：Catmull-Rom / B-Spline / NURBS 求值。"""
from __future__ import annotations
import math
from typing import List, Sequence
import numpy as np

from .vector.geometry_types import Point3D, CurveMode

def _to_array(points: Sequence[Point3D]) -> np.ndarray:
    return np.array([[p.x, p.y, p.z] for p in points], dtype=np.float64)


def _from_array(arr: np.ndarray) -> List[Point3D]:
    return [Point3D(float(r[0]), float(r[1]), float(r[2])) for r in arr]


def catmullRom(points: Sequence[Point3D], samplesPerSegment: int = 20) -> List[Point3D]:
    n = len(points)
    if n < 2:
        return list(points)
    if n == 2:
        return list(points)
    out: List[Point3D] = []
    for i in range(n - 1):
        p0 = points[i - 1] if i > 0 else points[i]
        p1 = points[i]
        p2 = points[i + 1]
        p3 = points[i + 2] if i + 2 < n else points[i + 1]
        for j in range(samplesPerSegment):
            t = j / samplesPerSegment
            t2 = t * t
            t3 = t2 * t
            x = 0.5 * ((2 * p1.x) + (-p0.x + p2.x) * t +
                       (2 * p0.x - 5 * p1.x + 4 * p2.x - p3.x) * t2 +
                       (-p0.x + 3 * p1.x - 3 * p2.x + p3.x) * t3)
            y = 0.5 * ((2 * p1.y) + (-p0.y + p2.y) * t +
                       (2 * p0.y - 5 * p1.y + 4 * p2.y - p3.y) * t2 +
                       (-p0.y + 3 * p1.y - 3 * p2.y + p3.y) * t3)
            z = 0.5 * ((2 * p1.z) + (-p0.z + p2.z) * t +
                       (2 * p0.z - 5 * p1.z + 4 * p2.z - p3.z) * t2 +
                       (-p0.z + 3 * p1.z - 3 * p2.z + p3.z) * t3)
            out.append(Point3D(x, y, z))
    out.append(points[-1])
    return out


def catmullRomSegment(p0: Point3D, p1: Point3D, p2: Point3D, p3: Point3D,
                      samples: int = 20) -> List[Point3D]:
    """单段 Catmull-Rom：p1 → p2（含两端），形由相邻四点决定。
    局部性：供增量画曲线时只重算近段、远段冻结采样。"""
    out: List[Point3D] = []
    for j in range(samples + 1):
        t = j / samples
        t2 = t * t
        t3 = t2 * t
        x = 0.5 * ((2 * p1.x) + (-p0.x + p2.x) * t +
                   (2 * p0.x - 5 * p1.x + 4 * p2.x - p3.x) * t2 +
                   (-p0.x + 3 * p1.x - 3 * p2.x + p3.x) * t3)
        y = 0.5 * ((2 * p1.y) + (-p0.y + p2.y) * t +
                   (2 * p0.y - 5 * p1.y + 4 * p2.y - p3.y) * t2 +
                   (-p0.y + 3 * p1.y - 3 * p2.y + p3.y) * t3)
        z = 0.5 * ((2 * p1.z) + (-p0.z + p2.z) * t +
                   (2 * p0.z - 5 * p1.z + 4 * p2.z - p3.z) * t2 +
                   (-p0.z + 3 * p1.z - 3 * p2.z + p3.z) * t3)
        out.append(Point3D(x, y, z))
    return out


def createInterpolatingBSpline(points: Sequence[Point3D], degree: int = 3,
                               samples: int = 100) -> List[Point3D]:
    n = len(points)
    if n < 2:
        return list(points)
    if n < degree + 1:
        return catmullRom(points, max(2, samples // max(1, n - 1)))
    try:
        from scipy.interpolate import splprep, splev
        arr = _to_array(points).T
        k = min(degree, n - 1)
        tck, u = splprep(arr, s=0, k=k)
        u_new = np.linspace(0.0, 1.0, samples)
        res = splev(u_new, tck)
        return [Point3D(float(res[0][i]), float(res[1][i]), float(res[2][i]))
                for i in range(samples)]
    except Exception:
        return catmullRom(points, max(2, samples // max(1, n - 1)))


def _clampedKnots(n: int, degree: int) -> np.ndarray:
    knots = np.concatenate([
        np.zeros(degree + 1),
        np.linspace(0, 1, n - degree)[1:-1],
        np.ones(degree + 1),
    ])
    return knots


def createControlPointBSpline(poles: Sequence[Point3D], degree: int = 3,
                              samples: int = 100) -> List[Point3D]:
    n = len(poles)
    if n < 2:
        return list(poles)
    if n < degree + 1:
        return catmullRom(poles, max(2, samples // max(1, n - 1)))
    try:
        from scipy.interpolate import BSpline
        k = min(degree, n - 1)
        t = _clampedKnots(n, k)
        c = _to_array(poles)
        spl = BSpline(t, c, k)
        u = np.linspace(0.0, 1.0, samples)
        res = spl(u)
        return _from_array(res)
    except Exception:
        return catmullRom(poles, max(2, samples // max(1, n - 1)))


def generateCurvePoints(points: Sequence[Point3D], mode: CurveMode = CurveMode.FIT,
                        degree: int = 3, samples: int = 100) -> List[Point3D]:
    if len(points) < 2:
        return list(points)
    if mode == CurveMode.CONTROL:
        return createControlPointBSpline(points, degree, samples)
    return createInterpolatingBSpline(points, degree, samples)


def generateCubicSplinePoints(points: Sequence[Point3D], samplesPerSegment: int = 20) -> List[Point3D]:
    return catmullRom(points, samplesPerSegment)


def bernsteinPoly(n: int, i: int, t: float) -> float:
    return math.comb(n, i) * (t ** i) * ((1 - t) ** (n - i))


def nurbsBasisVector(degree: int, knots: Sequence[float], t: float) -> List[float]:
    """t 处的完整 B 样条基函数向量（de Boor 递推），长度 n = len(knots)-degree-1。

    采样时每个 t 只需调用一次；旧实现递推范围截断为 range(n-d)，在
    控制点数 = 阶数+1（贝塞尔段）时基函数相对权重错误、曲线失真。
    """
    n = len(knots) - degree - 1
    N = [0.0] * n
    for j in range(n):
        if knots[j] <= t < knots[j + 1] or (j == n - 1 and t == knots[j + 1]):
            N[j] = 1.0
    for d in range(1, degree + 1):
        Nn = [0.0] * n
        for j in range(n):
            denom1 = knots[j + d] - knots[j]
            a = (t - knots[j]) / denom1 if denom1 != 0 else 0.0
            acc = a * N[j]
            if j + 1 < n:
                # N_{j+1,d-1} 越出存储范围（j+1 = n）时数学上恒为 0（t 在域内）
                denom2 = knots[j + d + 1] - knots[j + 1]
                if denom2 != 0:
                    acc += ((knots[j + d + 1] - t) / denom2) * N[j + 1]
            Nn[j] = acc
        N = Nn
    return N


def evaluateNurbsBasis(degree: int, knots: Sequence[float], i: int, t: float) -> float:
    """t 处第 i 个基函数值（保留旧接口；批量采样请用 nurbsBasisVector）。"""
    N = nurbsBasisVector(degree, knots, t)
    return N[i] if 0 <= i < len(N) else 0.0


def sampleNurbs(degree: int, controlPoints: Sequence[Point3D],
                weights: Sequence[float] | None, knots: Sequence[float],
                samples: int = 100) -> List[Point3D]:
    n = len(controlPoints)
    if weights is None:
        weights = [1.0] * n
    out: List[Point3D] = []
    tmin, tmax = knots[degree], knots[n]
    for s in range(samples):
        t = tmin + (tmax - tmin) * s / (samples - 1) if samples > 1 else tmin
        N = nurbsBasisVector(degree, knots, t)
        num = Point3D()
        den = 0.0
        for i in range(n):
            b = N[i] if 0 <= i < len(N) else 0.0
            w = weights[i] * b
            if w == 0.0:
                continue
            num = num + controlPoints[i] * w
            den += w
        if abs(den) > 1e-12:
            out.append(num * (1.0 / den))
        else:
            out.append(controlPoints[-1])
    return out
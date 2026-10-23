"""样条曲线数学工具：Catmull-Rom / B-Spline / NURBS 求值。

绘制样条采用向心参数化（centripetal, α=0.5）+ 局部 C2 五次 Hermite 插值：
- 均匀参数化在点击间距不均时必然过冲、出尖角甚至自交（线条不顺的根源）；
- 向心参数化（节点距 = 弦长^0.5）是唯一保证段内无尖角、无自交的 CR
  参数化（Yuksel, Schaefer & Keyser, "On the Parameterization of
  Catmull-Rom Curves", CAD 2011），Three.js / Unity 等引擎的标准选择；
- 五次 Hermite：每点估计一阶导 m 与二阶导 s，相邻段在共享节点使用
  同一组 (m, s) → 节点处**曲率连续（C2）**——三次 CR 只有 C1（切线连
  续、曲率跳变），观感即"点连接处不顺"。C2 的过渡即贝塞尔控制柄般
  平滑，这是用户可感知的"顺"的关键；
- 段形状仍只由相邻 4 个输入点决定 → 增量冻结（描图已画线永不动）
  架构完全不受影响；
- loosen 只缩放 m（相邻段共享同一缩放值，C2 不破坏）；
- 端点用 Bessel 条件：过前三点的抛物线取端点处一阶/二阶导数，
  端段与中间段同样贴合圆弧（反射虚点会沿弦线切角）。
"""
from __future__ import annotations
import math
from typing import List, Sequence, Tuple
import numpy as np

from .vector.geometry_types import Point3D, CurveMode

# 绘制样条默认参数（与 Rhino InterpCrv 实测形态对齐，2026-10-19 用户截图
# 逐点比对：α=0.25 轻向心参数化 + 标准切线幅值——谷底宽度/欠冲深度/
# 波峰形态三项均匹配；α 越大越抗压间距不均，越小越贴近 Rhino 均匀形态）
CR_LOOSEN = 1.0
CR_ALPHA = 0.25

def _to_array(points: Sequence[Point3D]) -> np.ndarray:
    return np.array([[p.x, p.y, p.z] for p in points], dtype=np.float64)


def _from_array(arr: np.ndarray) -> List[Point3D]:
    return [Point3D(float(r[0]), float(r[1]), float(r[2])) for r in arr]


def phantomPoint(a: Point3D, b: Point3D) -> Point3D:
    """b 关于 a 的镜像虚点（2a - b）：端点反射条件（仅光标预览尾段用）。"""
    return Point3D(2.0 * a.x - b.x, 2.0 * a.y - b.y, 2.0 * a.z - b.z)


def _dist3(a: Point3D, b: Point3D) -> float:
    return math.sqrt((b.x - a.x) ** 2 + (b.y - a.y) ** 2 + (b.z - a.z) ** 2)


def besselTangentStart(p0: Point3D, p1: Point3D, p2: Point3D,
                       alpha: float = 0.5, loosen: float = 1.0) -> Point3D:
    """曲线起点一阶导：过 p0/p1/p2 的抛物线在 p0 处的导数（Bessel 条件）。"""
    a = _dist3(p0, p1) ** alpha
    b = _dist3(p1, p2) ** alpha
    if a < 1e-12 or b < 1e-12 or (a + b) < 1e-12:
        return Point3D(0.0, 0.0, 0.0)
    c0 = -(2 * a + b) / (a * (a + b))
    c1 = (a + b) / (a * b)
    c2 = -a / ((a + b) * b)
    return Point3D(loosen * (c0 * p0.x + c1 * p1.x + c2 * p2.x),
                   loosen * (c0 * p0.y + c1 * p1.y + c2 * p2.y),
                   loosen * (c0 * p0.z + c1 * p1.z + c2 * p2.z))


def besselTangentEnd(p0: Point3D, p1: Point3D, p2: Point3D,
                     alpha: float = 0.5, loosen: float = 1.0) -> Point3D:
    """曲线终点一阶导：过 p0/p1/p2 的抛物线在 p2 处的导数（镜像 Bessel）。"""
    a = _dist3(p0, p1) ** alpha
    b = _dist3(p1, p2) ** alpha
    if a < 1e-12 or b < 1e-12 or (a + b) < 1e-12:
        return Point3D(0.0, 0.0, 0.0)
    c0 = b / (a * (a + b))
    c1 = -(a + b) / (a * b)
    c2 = (a + 2 * b) / ((a + b) * b)
    return Point3D(loosen * (c0 * p0.x + c1 * p1.x + c2 * p2.x),
                   loosen * (c0 * p0.y + c1 * p1.y + c2 * p2.y),
                   loosen * (c0 * p0.z + c1 * p1.z + c2 * p2.z))


def besselSecond(p0: Point3D, p1: Point3D, p2: Point3D,
                 alpha: float = 0.5) -> Point3D:
    """过 p0/p1/p2 的抛物线的二阶导数（二次拉格朗日的常值二阶导）。
    端点条件：首/末段缺第四点时用三点抛物线的 s 作为端点二阶导。"""
    a = _dist3(p0, p1) ** alpha
    b = _dist3(p1, p2) ** alpha
    if a < 1e-12 or b < 1e-12 or (a + b) < 1e-12:
        return Point3D(0.0, 0.0, 0.0)
    d0 = 1.0 / (a * (a + b))
    d1 = -1.0 / (a * b)
    d2 = 1.0 / ((a + b) * b)
    return Point3D(2.0 * (d0 * p0.x + d1 * p1.x + d2 * p2.x),
                   2.0 * (d0 * p0.y + d1 * p1.y + d2 * p2.y),
                   2.0 * (d0 * p0.z + d1 * p1.z + d2 * p2.z))


def _hermiteQuintic(p0: Point3D, p1: Point3D,
                    m0: Point3D, m1: Point3D,
                    s0: Point3D, s1: Point3D, T: float,
                    samples: int) -> List[Point3D]:
    """五次 Hermite 段采样（u∈[0,1]，T 为节点距）：给定端点位置/一阶/二阶
    导数，两端 m、s 与相邻段一致即 C2。"""
    T2 = T * T
    out: List[Point3D] = []
    for j in range(samples + 1):
        u = j / samples
        u2 = u * u
        u3 = u2 * u
        u4 = u3 * u
        u5 = u4 * u
        h00 = 1 - 10 * u3 + 15 * u4 - 6 * u5
        h10 = u - 6 * u3 + 8 * u4 - 3 * u5
        h20 = 0.5 * u2 - 1.5 * u3 + 1.5 * u4 - 0.5 * u5
        h01 = 10 * u3 - 15 * u4 + 6 * u5
        h11 = -4 * u3 + 7 * u4 - 3 * u5
        h21 = 0.5 * u3 - u4 + 0.5 * u5
        out.append(Point3D(
            h00 * p0.x + h01 * p1.x + h10 * T * m0.x + h11 * T * m1.x
            + h20 * T2 * s0.x + h21 * T2 * s1.x,
            h00 * p0.y + h01 * p1.y + h10 * T * m0.y + h11 * T * m1.y
            + h20 * T2 * s0.y + h21 * T2 * s1.y,
            h00 * p0.z + h01 * p1.z + h10 * T * m0.z + h11 * T * m1.z
            + h20 * T2 * s0.z + h21 * T2 * s1.z))
    return out


def _d1(a: Point3D, b: Point3D, den: float, loosen: float) -> Point3D:
    """一阶导差分 (b-a)/den × loosen（den 为节点距和）。"""
    if den < 1e-12:
        return Point3D(0.0, 0.0, 0.0)
    s = loosen / den
    return Point3D((b.x - a.x) * s, (b.y - a.y) * s, (b.z - a.z) * s)


def _d2(pa: Point3D, pb: Point3D, pc: Point3D,
        dta: float, dtb: float) -> Point3D:
    """节点 pb 处二阶导的中心差分：
    2·[(pc-pb)/dtb − (pb-pa)/dta] / (dta+dtb)。相邻段共享同一节点值 → C2。"""
    den = dta + dtb
    if dta < 1e-12 or dtb < 1e-12 or den < 1e-12:
        return Point3D(0.0, 0.0, 0.0)
    v1 = (pc.x - pb.x) / dtb - (pb.x - pa.x) / dta
    v2 = (pc.y - pb.y) / dtb - (pb.y - pa.y) / dta
    v3 = (pc.z - pb.z) / dtb - (pb.z - pa.z) / dta
    f = 2.0 / den
    return Point3D(v1 * f, v2 * f, v3 * f)


def catmullRom(points: Sequence[Point3D], samplesPerSegment: int = 20,
               loosen: float = CR_LOOSEN, alpha: float = None) -> List[Point3D]:
    """整条局部 C2 插值曲线（严格过点、节点曲率连续）。

    内部节点 m/s 用中心差分；首末点用 Bessel 一阶/二阶导。
    用于闭合环整体计算、编辑控制点后重采样（与绘制预览同一套数学）。
    alpha=0 均匀参数化，0.5 向心，1.0 弦长；默认 CR_ALPHA（对齐 Rhino）。"""
    if alpha is None:
        alpha = CR_ALPHA
    n = len(points)
    if n < 2:
        return list(points)
    if n == 2:
        return list(points)
    samples = max(1, int(samplesPerSegment))
    # 节点距 = 弦长^alpha（alpha→0 即均匀）
    t = [0.0]
    for i in range(1, n):
        d = _dist3(points[i - 1], points[i])
        if alpha > 1e-9:
            t.append(t[-1] + (d ** alpha if d > 1e-12 else 0.0))
        else:
            t.append(t[-1] + 1.0)

    ms: List[Point3D] = []
    ss: List[Point3D] = []
    for i in range(n):
        if i == 0 and n >= 3:
            ms.append(besselTangentStart(points[0], points[1], points[2],
                                         alpha, loosen))
            ss.append(besselSecond(points[0], points[1], points[2], alpha))
        elif i == n - 1 and n >= 3:
            ms.append(besselTangentEnd(points[n - 3], points[n - 2],
                                       points[n - 1], alpha, loosen))
            ss.append(besselSecond(points[n - 3], points[n - 2],
                                   points[n - 1], 0.5))
        else:
            ta, tb = t[max(i - 1, 0)], t[min(i + 1, n - 1)]
            pa = points[max(i - 1, 0)]
            pc = points[min(i + 1, n - 1)]
            ms.append(_d1(pa, pc, tb - ta, loosen))
            if i == 0 or i == n - 1:   # n==2 退化
                ss.append(Point3D(0.0, 0.0, 0.0))
            else:
                dtb = t[i + 1] - t[i]
                dta = t[i] - t[i - 1]
                ss.append(_d2(points[i - 1], points[i], points[i + 1],
                              dta, dtb))

    out: List[Point3D] = []
    for i in range(n - 1):
        T = t[i + 1] - t[i]
        if T < 1e-12:
            continue
        seg = _hermiteQuintic(points[i], points[i + 1], ms[i], ms[i + 1],
                              ss[i], ss[i + 1], T, samples)
        out.extend(seg[:-1])
    out.append(points[-1])
    return out


def catmullRomSegment(p0: Point3D, p1: Point3D, p2: Point3D, p3: Point3D,
                      samples: int = 20, alpha: float = None,
                      loosen: float = CR_LOOSEN,
                      tan1: "Point3D | None" = None,
                      tan2: "Point3D | None" = None,
                      sec1: "Point3D | None" = None,
                      sec2: "Point3D | None" = None) -> List[Point3D]:
    """单段局部 C2 五次 Hermite 插值：p1 → p2（含两端），形由相邻四点决定。

    一阶导（节点距 = 弦长^alpha）：
      m1 = loosen·(p2-p0)/(dt0+dt1)   m2 = loosen·(p3-p1)/(dt1+dt2)
    二阶导（节点中心差分）：
      s1 = 2·[(p2-p1)/dt1 − (p1-p0)/dt0]/(dt0+dt1)
      s2 = 2·[(p3-p2)/dt2 − (p2-p1)/dt1]/(dt1+dt2)
    相邻段在同一共享节点使用同一组 (m, s) → 节点处曲率连续（C2）。
    tan/sec 显式给定时用作端点导数（Bessel 端点条件，首末段用）。
    退化（重合点）时安全降级。供增量画曲线只重算近段、远段冻结。
    """
    if alpha is None:
        alpha = CR_ALPHA
    if alpha > 1e-9:
        dt0 = _dist3(p0, p1) ** alpha
        dt1 = _dist3(p1, p2) ** alpha
        dt2 = _dist3(p2, p3) ** alpha
    else:   # 均匀参数化
        dt0 = dt1 = dt2 = 1.0
    if dt1 < 1e-12:
        return [Point3D(p1.x, p1.y, p1.z) for _ in range(samples + 1)]

    m1 = tan1 if tan1 is not None else _d1(p0, p2, dt0 + dt1, loosen)
    m2 = tan2 if tan2 is not None else _d1(p1, p3, dt1 + dt2, loosen)
    s1 = sec1 if sec1 is not None else _d2(p0, p1, p2, dt0, dt1)
    s2 = sec2 if sec2 is not None else _d2(p1, p2, p3, dt1, dt2)
    return _hermiteQuintic(p1, p2, m1, m2, s1, s2, dt1, samples)


def _find_span(n: int, p: int, u: float, knots: Sequence[float]) -> int:
    """de Boor 节点跨度定位（NURBS 书 A2.1）。n = 末控制点下标。"""
    if u >= knots[n + 1]:
        return n
    lo, hi = p, n + 1
    mid = (lo + hi) // 2
    for _ in range(200):
        if u < knots[mid]:
            hi = mid
        elif u >= knots[mid + 1]:
            lo = mid
        else:
            break
        mid = (lo + hi) // 2
    return mid


def _basis(span: int, u: float, p: int, knots: Sequence[float]) -> List[float]:
    """Cox-de Boor 基函数三角递推（NURBS 书 A2.2）。返回 p+1 个非零基。"""
    nn = [0.0] * (p + 1)
    left = [0.0] * (p + 1)
    right = [0.0] * (p + 1)
    nn[0] = 1.0
    for j in range(1, p + 1):
        left[j] = u - knots[span + 1 - j]
        right[j] = knots[span + j] - u
        saved = 0.0
        for r in range(j):
            den = right[r + 1] + left[j - r]
            tmp = nn[r] / den if abs(den) > 1e-300 else 0.0
            nn[r] = saved + right[r + 1] * tmp
            saved = left[j - r] * tmp
        nn[j] = saved
    return nn


def _fit_params(pts: Sequence[Point3D], kind: str, closed: bool) -> List[float]:
    """拟合点归一化参数：chord=弦长、sqrt=向心、uniform=均匀
    （cadcraft KnotParam；闭包多最后一项——回到起点的那段弦）。"""
    n = len(pts)
    count = n + 1 if closed else n
    u = [0.0] * count
    total = 0.0
    for i in range(1, count):
        a = pts[(i - 1) % n]
        b = pts[i % n]
        d = a.distanceTo(b)
        if kind == 'sqrt':
            d = math.sqrt(max(d, 1e-12))
        elif kind == 'uniform':
            d = 1.0
        total += max(d, 1e-12) if kind != 'uniform' else d
        u[i] = total
    for i in range(count):
        u[i] /= max(total, 1e-300)
    return u


def _interp_knots(u: List[float]) -> Tuple[int, List[float]]:
    """插值 clamped 节点矢量：内节点 = 参数平均（NURBS 书式 9.8）。
    p = min(3, m-1)；返回 (次数, 节点表)。"""
    m = len(u)
    p = max(1, min(3, m - 1))
    knots = [0.0] * (p + 1)
    for j in range(1, m - p):
        knots.append(sum(u[j:j + p]) / p)
    knots.extend([1.0] * (p + 1))
    return p, knots


def _periodic_knots(t: List[float], n: int, p: int) -> List[float]:
    """周期（unclamped）节点矢量：knots[j] = t_{j-p}，两侧按整周期延伸。"""
    def tk(i: int) -> float:
        q, r = divmod(i, n)
        return t[r] + q
    return [tk(j - p) for j in range(n + 2 * p + 1)]


def nurbs_fit_samples(points: Sequence[Point3D],
                      samples_per_segment: int = 24,
                      closed: bool = False,
                      knots: str = 'chord') -> List[Point3D]:
    """拟合点样条（完整复刻开源 CAD cadcraft 的 SPLINE/Fit 命令）：

    Piegl & Tiller《NURBS 书》第 9 章全局插值——弦长参数化（默认，
    可选 sqrt/uniform）、节点平均的 clamped 三次 B 样条、自由端点、
    严格过每个拟合点；闭合为周期（unclamped）节点 + 循环求解，接缝
    处切线/曲率连续。预览 = 含光标伪点的整体重拟合，确认 = 仅真实点
    重拟合（与 cadcraft SplineM.preview/input 完全同构）。

    加点重拟合后，远离新点的控制点/段按 C2 端部衰减律 ~0.27^k 微调，
    每段固定局部参数采样 → 顶点不漂移，远处已描线视觉稳定。"""
    n = len(points)
    if n < 2:
        return list(points)
    pts = list(points)
    if closed and n > 3 and pts[0].distanceTo(pts[-1]) < 1e-12:
        pts.pop()
        n = len(pts)
    if n < 2:
        return list(points)
    spp = max(4, int(samples_per_segment))
    if not closed and n == 2:
        return list(pts)   # 两点 = 直线（cadcraft degree-1）
    if closed and n <= 3:
        f = pts + pts[:1]
        return catmullRom(f, spp)

    u = _fit_params(pts, knots, closed)
    try:
        if closed:
            return _nurbs_periodic_samples(pts, u, spp)
        p, kv = _interp_knots(u)
        m = len(kv) - p - 1   # 控制点数 = 拟合点数
        a = [[0.0] * m for _ in range(m)]
        bx = [0.0] * m
        by = [0.0] * m
        bz = [0.0] * m
        for row, (ui, q) in enumerate(zip(u, pts)):
            span = _find_span(m - 1, p, ui, kv)
            b = _basis(span, ui, p, kv)
            for kk, v in enumerate(b):
                j = span + kk - p
                if 0 <= j < m:
                    a[row][j] += v
            bx[row] = q.x
            by[row] = q.y
            bz[row] = q.z
        # 一次 LU 分解解 x/y/z 三个右端项（矩阵不被破坏）
        sol = np.linalg.solve(np.array(a), np.array([bx, by, bz], dtype=float).T)
        control = [Point3D(float(sol[i, 0]), float(sol[i, 1]), float(sol[i, 2]))
                   for i in range(m)]
        if any(not (math.isfinite(c.x) and math.isfinite(c.y) and math.isfinite(c.z))
               for c in control):
            raise ValueError('non-finite control points')
        from scipy.interpolate import BSpline
        spl = BSpline(kv, np.array([[c.x, c.y, c.z] for c in control]), p)
        # 每个节点跨度按固定局部参数采样（顶点不漂移），并把拟合参数
        # u_k 插入采样序列 → 曲线精确"穿过"每个拟合点（视觉可见）
        out: List[Point3D] = []
        lo, hi = kv[p], kv[m]
        spans = sorted({kv[i] for i in range(p, m + 1)
                        if kv[p] <= kv[i] <= hi})
        fit_u = sorted(ui for ui in u if lo < ui < hi)
        for s0, s1 in zip(spans[:-1], spans[1:]):
            if s1 - s0 < 1e-15:
                continue
            us = [s0 + (s1 - s0) * j / spp for j in range(spp)]
            us += [x for x in fit_u if s0 < x < s1]
            for uu in sorted(us):
                v = spl(uu)
                out.append(Point3D(float(v[0]), float(v[1]), float(v[2])))
        out.append(pts[-1])
        return out
    except Exception:
        return catmullRom(pts, spp)


def _nurbs_periodic_samples(pts: List[Point3D], u: List[float],
                            spp: int) -> List[Point3D]:
    """周期闭合拟合（cadcraft closed_fit 移植）：unclamped 节点 +
    循环系数矩阵（控制下标 mod n），首尾切线/曲率连续。"""
    n = len(pts)
    p = 3
    kv = _periodic_knots(u, n, p)
    a = [[0.0] * n for _ in range(n)]
    bx = [0.0] * n
    by = [0.0] * n
    bz = [0.0] * n
    m_ctrl = n + p   # 节点表长 = 控制数 + 次数 + 1
    for i in range(n):
        ui = u[i]
        span = _find_span(m_ctrl - 1, p, ui, kv)
        b = _basis(span, ui, p, kv)
        for kk, v in enumerate(b):
            j = (span + kk - p) % n
            a[i][j] += v
        bx[i] = pts[i].x
        by[i] = pts[i].y
        bz[i] = pts[i].z
    # 循环系数矩阵（np.linalg.solve，n×n 稠密；闭环点数通常远小于上限）
    sol = np.linalg.solve(np.array(a), np.array([bx, by, bz], dtype=float).T)
    control = [Point3D(float(sol[i, 0]), float(sol[i, 1]), float(sol[i, 2]))
               for i in range(n)]
    control.extend(control[:p])   # 首段控制点在尾部重复（clamped 化采样用）
    from scipy.interpolate import BSpline
    spl = BSpline(kv, np.array([[c.x, c.y, c.z] for c in control]), p)
    out: List[Point3D] = []
    # 采样区间必须一直走到周期终点 1.0（闭合弦 u[n]）：原实现只到
    # u[n-1]，闭合腿从未被采样 → 曲线几何上始终是开口的（闭合标志形同
    # 虚设，用户按"闭合"看不出任何变化）。
    spans = sorted(set(u[:n]) | {1.0})
    for s0, s1 in zip(spans[:-1], spans[1:]):
        for j in range(spp):
            uu = s0 + (s1 - s0) * j / spp
            v = spl(uu)
            out.append(Point3D(float(v[0]), float(v[1]), float(v[2])))
    v = spl(spans[-1])
    out.append(Point3D(float(v[0]), float(v[1]), float(v[2])))
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
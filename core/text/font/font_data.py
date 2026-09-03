"""字体数据结构与曲线采样工具。

GlyphData 直接存储采样后的折线点列（em 空间，y 轴向上），
布局层负责变换到世界坐标。TTF 的贝塞尔、SGL 的 B 样条在解析时
统一采样为折线，避免运行时再做曲线求值。
"""
from __future__ import annotations
from enum import Enum
from typing import Dict, List, Tuple, Optional, Callable
import math

Point = Tuple[float, float]


class FontType(Enum):
    SGL = 0
    SHX = 1
    TTF = 2


# ---------- 曲线采样 ----------

def _bezier2(p0: Point, p1: Point, p2: Point, n: int) -> List[Point]:
    pts = []
    for i in range(n + 1):
        t = i / n
        u = 1.0 - t
        x = u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0]
        y = u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1]
        pts.append((x, y))
    return pts


def _bezier3(p0: Point, p1: Point, p2: Point, p3: Point, n: int) -> List[Point]:
    pts = []
    for i in range(n + 1):
        t = i / n
        u = 1.0 - t
        a, b, c = u * u * u, 3 * u * u * t, 3 * u * t * t
        x = a * p0[0] + b * p1[0] + c * p2[0] + t * t * t * p3[0]
        y = a * p0[1] + b * p1[1] + c * p2[1] + t * t * t * p3[1]
        pts.append((x, y))
    return pts


def _clampedBSpline(ctrl: List[Point], degree: int, n: int) -> List[Point]:
    """钳位 B 样条：曲线过首末控制点，内部点仅控曲率。
    对应 C# NurbsCurve.Create(false, degree, ctrl)。"""
    try:
        from scipy.interpolate import BSpline
        import numpy as np
    except Exception:
        return _polyline(ctrl)
    try:
        m = len(ctrl) + degree + 1
        knots = [0.0] * (degree + 1)
        inner = m - 2 * (degree + 1)
        if inner > 0:
            for k in range(1, inner + 1):
                knots.append(k / (inner + 1))
        knots += [float(inner + 1)] * (degree + 1)
        t_max = knots[-1]
        c = np.array(ctrl, dtype=float)
        spl = BSpline(knots, c, degree)
        pts = []
        for i in range(n + 1):
            t = t_max * i / n
            v = spl(t)
            pts.append((float(v[0]), float(v[1])))
        return pts
    except Exception:
        return _polyline(ctrl)


def _interpCurve(ctrl: List[Point], n: int) -> List[Point]:
    """三次插值曲线：穿过每个控制点。对应 C# Curve.CreateInterpolatedCurve。"""
    try:
        from scipy.interpolate import make_interp_spline
        import numpy as np
    except Exception:
        return _polyline(ctrl)
    if len(ctrl) < 3:
        return _polyline(ctrl)
    try:
        xs = [p[0] for p in ctrl]
        if all(xs[i] < xs[i + 1] for i in range(len(xs) - 1)):
            bspl = make_interp_spline(xs, np.array(ctrl, dtype=float), k=3)
            x0, x1 = xs[0], xs[-1]
            pts = []
            for i in range(n + 1):
                x = x0 + (x1 - x0) * i / n
                v = bspl(x)
                pts.append((float(v[0]), float(v[1])))
            return pts
        idx = list(range(len(ctrl)))
        bspl = make_interp_spline(idx, np.array(ctrl, dtype=float), k=3)
        pts = []
        for i in range(n + 1):
            t = idx[-1] * i / n
            v = bspl(t)
            pts.append((float(v[0]), float(v[1])))
        return pts
    except Exception:
        return _polyline(ctrl)


def _polyline(pts: List[Point]) -> List[Point]:
    return list(pts)


# ---------- 字形/字体 ----------

class GlyphData:
    __slots__ = ("character", "width", "height", "paths",
                 "bbMin", "bbMax", "_inkCenter")

    def __init__(self) -> None:
        self.character: str = ""
        self.width: float = 0.0
        self.height: float = 1.0
        self.paths: List[List[Point]] = []
        self.bbMin: Optional[Point] = None
        self.bbMax: Optional[Point] = None
        self._inkCenter: Optional[Point] = None

    def inkCenter(self) -> Optional[Point]:
        if self._inkCenter is not None:
            return self._inkCenter
        xs, ys = [], []
        for path in self.paths:
            for x, y in path:
                xs.append(x); ys.append(y)
        if not xs:
            return None
        self._inkCenter = ((min(xs) + max(xs)) * 0.5,
                           (min(ys) + max(ys)) * 0.5)
        return self._inkCenter


class FontData:
    def __init__(self) -> None:
        self.name: str = ""
        self.filePath: str = ""
        self.type: FontType = FontType.TTF
        self.emSize: float = 1.0
        self.lineSpacing: float = 1.2
        self.defaultCharSpacing: float = 0.1
        self.glyphs: Dict[str, GlyphData] = {}
        self.glyphLoader: Optional[Callable[[str], Optional[GlyphData]]] = None
        self.autoCloseContours: bool = True
        self.isLoaded: bool = False
        self.error: str = ""

    def getGlyph(self, c: str) -> Optional[GlyphData]:
        g = self.glyphs.get(c)
        if g is not None:
            return g
        if self.glyphLoader is not None:
            g = self.glyphLoader(c)
            if g is not None:
                self.glyphs[c] = g
                return g
        return None

    def hasGlyph(self, c: str) -> bool:
        return c in self.glyphs

    def glyphCount(self) -> int:
        return len(self.glyphs)
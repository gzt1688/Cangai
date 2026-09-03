"""文字布局引擎（移植自 C# TextLayout + CurveTextLayout）。

输出统一为 List[List[List[(x,y)]]]：每个字的多条折线（已变换到世界坐标）。
直线布局：水平/垂直排列，字高/字间距/绕自身中心旋转。
曲线布局：沿折线分布，垂直于曲线/保持竖直，偏移/起始/反向/镜像/旋转。
"""
from __future__ import annotations

import math
from typing import List, Tuple, Optional
from .font.font_data import FontData, GlyphData, Point

Path = List[Point]
CharPaths = List[Path]


def _rotate(p: Point, cx: float, cy: float, cosA: float, sinA: float) -> Point:
    dx, dy = p[0] - cx, p[1] - cy
    return (cx + dx * cosA - dy * sinA, cy + dx * sinA + dy * cosA)


def _transformGlyph(g: GlyphData, scale: float, tx: float, ty: float,
                    rotDeg: float, origin: Optional[Point] = None) -> CharPaths:
    """缩放→平移→绕 origin 旋转（origin 为 None 时绕字墨迹中心）。"""
    out: CharPaths = []
    if rotDeg == 0.0:
        for path in g.paths:
            out.append([(p[0] * scale + tx, p[1] * scale + ty) for p in path])
        return out
    if origin is None:
        origin = g.inkCenter()
        if origin is None:
            origin = (0.0, 0.0)
    cx0 = origin[0] * scale + tx
    cy0 = origin[1] * scale + ty
    rad = math.radians(rotDeg)
    cosA, sinA = math.cos(rad), math.sin(rad)
    for path in g.paths:
        out.append([_rotate((p[0] * scale + tx, p[1] * scale + ty),
                            cx0, cy0, cosA, sinA) for p in path])
    return out


# ---------- 直线布局 ----------

def layoutLinear(text: str, font: FontData, charHeight: float,
                 charSpacing: float, vertical: bool = False,
                 rotation: float = 0.0) -> List[CharPaths]:
    """直线排列文字，返回每个字的折线列表（世界坐标，基线在 y=0）。"""
    results: List[CharPaths] = []
    if not text or font is None or not font.isLoaded:
        return results
    scale = charHeight / font.emSize
    curX = 0.0
    curY = 0.0
    for c in text:
        if c == "\n":
            if vertical:
                continue
            curY -= charHeight * font.lineSpacing
            curX = 0.0
            continue
        g = font.getGlyph(c)
        if g is None:
            if vertical:
                curY -= charHeight + charSpacing
            else:
                curX += charHeight * 0.5 + charSpacing
            results.append([])
            continue
        charW = g.width * scale
        paths = _transformGlyph(g, scale, curX, curY, rotation)
        results.append(paths)
        if vertical:
            curY -= charHeight + charSpacing
        else:
            curX += charW + charSpacing
    return results


# ---------- 曲线布局 ----------

class _PolylineCurve:
    """折线曲线：支持 PointAtLength / 切线 / 总长。"""

    def __init__(self, pts: List[Point]) -> None:
        self.pts = pts
        self.cum: List[float] = [0.0]
        for i in range(1, len(pts)):
            d = math.hypot(pts[i][0] - pts[i - 1][0],
                           pts[i][1] - pts[i - 1][1])
            self.cum.append(self.cum[-1] + d)
        self.length = self.cum[-1] if self.cum else 0.0
        self.closed = (len(pts) >= 3 and
                       math.hypot(pts[0][0] - pts[-1][0],
                                  pts[0][1] - pts[-1][1]) < 1e-6)

    def pointAt(self, length: float) -> Optional[Point]:
        if not self.pts or self.length <= 0:
            return None
        if self.closed:
            length = length % self.length
        else:
            length = max(0.0, min(self.length, length))
        for i in range(1, len(self.cum)):
            if self.cum[i] >= length:
                segLen = self.cum[i] - self.cum[i - 1]
                if segLen <= 1e-9:
                    return self.pts[i]
                t = (length - self.cum[i - 1]) / segLen
                p0, p1 = self.pts[i - 1], self.pts[i]
                return (p0[0] + (p1[0] - p0[0]) * t,
                        p0[1] + (p1[1] - p0[1]) * t)
        return self.pts[-1]

    def tangentAt(self, length: float) -> Point:
        if not self.pts or self.length <= 0:
            return (1.0, 0.0)
        if self.closed:
            length = length % self.length
        else:
            length = max(0.0, min(self.length, length))
        delta = min(0.1, self.length * 0.5)
        pb = self.pointAt(length - delta)
        pa = self.pointAt(length + delta)
        if pb is None or pa is None:
            return (1.0, 0.0)
        tx, ty = pa[0] - pb[0], pa[1] - pb[1]
        L = math.hypot(tx, ty)
        if L < 1e-9:
            return (1.0, 0.0)
        return (tx / L, ty / L)


def layoutOnCurve(text: str, font: FontData, curvePts: List[Point],
                  charHeight: float, charSpacing: float,
                  offset: float = 0.0, startPos: float = 0.0,
                  reverse: bool = False, mirror: bool = False,
                  rotation: float = 0.0,
                  perpendicular: bool = True) -> List[CharPaths]:
    """沿曲线排列文字。curvePts 为世界坐标折线点列。"""
    results: List[CharPaths] = []
    if (not text or font is None or not font.isLoaded or
            len(curvePts) < 2):
        return results
    curve = _PolylineCurve(curvePts)
    if curve.length <= 1e-9:
        return results
    scale = charHeight / font.emSize
    direction = -1.0 if reverse else 1.0
    curLen = startPos * curve.length
    prevNormal: Optional[Point] = None

    for c in text:
        g = font.getGlyph(c)
        if g is None:
            spaceW = charHeight * 0.5
            curLen += (spaceW + charSpacing) * direction
            results.append([])
            continue
        charW = g.width * scale
        if (not curve.closed and
                (curLen < 0 or curLen > curve.length)):
            curLen += (charW + charSpacing) * direction
            results.append([])
            continue
        centerL = curLen + charW / 2.0
        if curve.closed:
            centerL = centerL % curve.length
        pt = curve.pointAt(centerL)
        if pt is None:
            results.append([])
            curLen += (charW + charSpacing) * direction
            continue
        tan = curve.tangentAt(centerL)
        rawNormal = (-tan[1], tan[0])
        normal = rawNormal
        if prevNormal is not None:
            dot = normal[0] * prevNormal[0] + normal[1] * prevNormal[1]
            if dot < 0:
                normal = (-normal[0], -normal[1])
        prevNormal = normal

        if perpendicular:
            xDir = tan
            up = normal
        else:
            xDir = (1.0, 0.0)
            up = (0.0, 1.0)

        ink = g.inkCenter()
        if ink is None:
            ink = (font.emSize * 0.5, font.emSize * 0.5)
        cLocalX = ink[0] * scale
        cLocalY = ink[1] * scale

        circleCenter = (pt[0] + normal[0] * offset,
                        pt[1] + normal[1] * offset)
        finalPos = (circleCenter[0] - xDir[0] * cLocalX - up[0] * cLocalY,
                    circleCenter[1] - xDir[1] * cLocalX - up[1] * cLocalY)

        paths: CharPaths = []
        for path in g.paths:
            transformed = []
            for p in path:
                lx = p[0] * scale
                ly = p[1] * scale
                wx = finalPos[0] + xDir[0] * lx + up[0] * ly
                wy = finalPos[1] + xDir[1] * lx + up[1] * ly
                transformed.append((wx, wy))
            paths.append(transformed)

        if mirror:
            cosA, sinA = math.cos(math.pi), math.sin(math.pi)
            paths = [[_rotate(p, pt[0], pt[1], cosA, sinA) for p in path]
                     for path in paths]
        if rotation != 0.0:
            rad = math.radians(rotation)
            cosA, sinA = math.cos(rad), math.sin(rad)
            paths = [[_rotate(p, circleCenter[0], circleCenter[1], cosA, sinA)
                      for p in path] for path in paths]
        results.append(paths)
        curLen += (charW + charSpacing) * direction
    return results


def flatten(paths: List[CharPaths]) -> List[Path]:
    """把所有字的笔画扁平化为折线列表（供 PolyCurveObject.segments）。"""
    out: List[Path] = []
    for charPaths in paths:
        out.extend(charPaths)
    return out
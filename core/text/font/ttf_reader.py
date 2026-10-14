"""TTF/OTF 字体读取器（移植自 C# TTFFontReader）。

用 fontTools 提取字形轮廓（直线/二次/三次贝塞尔），采样为折线。
按需加载：仅探测单线属性，字形在 getGlyph 时才解析并缓存。
"""
from __future__ import annotations

import os
import math
from typing import Optional
from .font_data import (FontData, FontType, GlyphData, Point,
                        _bezier2, _bezier3)

_BEZIER_SEG = 16
_PROBE_CHARS = "1234567890ABCDEFGHIJKLabcdefghijklmnopqrstuvwxyz"


def _loadFontTools():
    from fontTools.ttLib import TTFont
    from fontTools.pens.recordingPen import DecomposingRecordingPen
    return TTFont, DecomposingRecordingPen


def _drawToPaths(pen_value, em, autoClose) -> tuple:
    """把 RecordingPen 的操作流转为折线 paths，返回 (paths, bbMin, bbMax)。"""
    paths = []
    cur = []
    subStart: Optional[Point] = None
    last: Optional[Point] = None
    minX = minY = float("inf")
    maxX = maxY = -float("inf")

    def _emit(p):
        nonlocal minX, minY, maxX, maxY
        cur.append(p)
        if p[0] < minX: minX = p[0]
        if p[0] > maxX: maxX = p[0]
        if p[1] < minY: minY = p[1]
        if p[1] > maxY: maxY = p[1]

    def _close():
        nonlocal subStart, last
        if len(cur) >= 2 and subStart is not None and last is not None:
            if autoClose and (abs(last[0] - subStart[0]) > 1e-6 or
                              abs(last[1] - subStart[1]) > 1e-6):
                cur.append(subStart)
                last = subStart
        if len(cur) >= 2:
            paths.append(list(cur))
        cur.clear()
        subStart = None
        last = None

    for op, args in pen_value:
        if op == "moveTo":
            if cur:
                _close()
            p = (args[0][0] / em, args[0][1] / em)
            cur.append(p)
            subStart = p
            last = p
            minX = min(minX, p[0]); maxX = max(maxX, p[0])
            minY = min(minY, p[1]); maxY = max(maxY, p[1])
        elif op == "lineTo":
            p = (args[0][0] / em, args[0][1] / em)
            _emit(p)
            last = p
        elif op == "curveTo":
            p1 = (args[0][0] / em, args[0][1] / em)
            p2 = (args[1][0] / em, args[1][1] / em)
            p3 = (args[2][0] / em, args[2][1] / em)
            for q in _bezier3(last, p1, p2, p3, _BEZIER_SEG)[1:]:
                _emit(q)
            last = p3
        elif op == "qCurveTo":
            pts = list(args)
            hasNone = pts[-1] is None
            if hasNone:
                pts = pts[:-1]
            if last is None:
                if len(pts) >= 2:
                    sx = (pts[0][0] + pts[-1][0]) * 0.5
                    sy = (pts[0][1] + pts[-1][1]) * 0.5
                else:
                    sx, sy = pts[0]
                p = (sx / em, sy / em)
                cur.append(p)
                subStart = p
                last = p
                minX = min(minX, p[0]); maxX = max(maxX, p[0])
                minY = min(minY, p[1]); maxY = max(maxY, p[1])
            if hasNone:
                p0 = last
                n = len(pts)
                for i in range(n):
                    p1 = (pts[i][0] / em, pts[i][1] / em)
                    if i < n - 1:
                        p2 = ((pts[i][0] + pts[i+1][0]) * 0.5 / em,
                              (pts[i][1] + pts[i+1][1]) * 0.5 / em)
                    else:
                        p2 = subStart
                    for q in _bezier2(p0, p1, p2, _BEZIER_SEG)[1:]:
                        _emit(q)
                    p0 = p2
                last = p0
            elif len(pts) == 1:
                p = (pts[0][0] / em, pts[0][1] / em)
                _emit(p); last = p
            else:
                p0 = last
                for i in range(len(pts) - 1):
                    p1 = (pts[i][0] / em, pts[i][1] / em)
                    p2 = (pts[i + 1][0] / em, pts[i + 1][1] / em)
                    for q in _bezier2(p0, p1, p2, _BEZIER_SEG)[1:]:
                        _emit(q)
                    p0 = p2
                last = p0
        elif op in ("closePath", "endPath"):
            _close()
    if cur:
        _close()
    if minX > maxX:
        return [], None, None
    return paths, (minX, minY), (maxX, maxY)


def _maxContourGap(pen_value, em) -> float:
    """子路径首尾端点距离最大值（归一化），用于单线字体探测。"""
    subStart = None
    last = None
    maxGap = 0.0
    for op, args in pen_value:
        if op == "moveTo":
            subStart = (args[0][0] / em, args[0][1] / em)
            last = subStart
        elif op == "lineTo":
            last = (args[0][0] / em, args[0][1] / em)
        elif op == "curveTo":
            last = (args[2][0] / em, args[2][1] / em)
        elif op == "qCurveTo":
            if args[-1] is not None:
                last = (args[-1][0] / em, args[-1][1] / em)
            elif len(args) >= 2:
                last = (args[-2][0] / em, args[-2][1] / em)
        elif op in ("closePath", "endPath"):
            if subStart is not None and last is not None:
                d = math.hypot(last[0] - subStart[0], last[1] - subStart[1])
                if d > maxGap:
                    maxGap = d
            subStart = None
    return maxGap



def _detectSingleLine(ttFont, glyphSet, cmap, em) -> bool:
    openCnt = 0
    total = 0
    RecordingPen = _loadFontTools()[1]
    for c in _PROBE_CHARS:
        gn = cmap.get(ord(c))
        if gn is None:
            continue
        pen = RecordingPen(glyphSet)
        try:
            glyphSet[gn].draw(pen)
        except Exception:
            continue
        if not pen.value:
            continue
        g = _maxContourGap(pen.value, em)
        total += 1
        if g > 0.35:
            openCnt += 1
    if total == 0:
        return False
    return openCnt / total > 0.4


def loadFromFile(filePath: str, fontNumber: int = 0) -> FontData:
    if not os.path.isfile(filePath):
        fd = FontData()
        fd.name = os.path.splitext(os.path.basename(filePath))[0]
        fd.type = FontType.TTF
        fd.error = f"字体文件不存在: {filePath}"
        return fd
    return _loadFromPath(filePath, fontNumber)


def _loadFromPath(filePath: str, fontNumber: int = 0) -> FontData:
    TTFont, RecordingPen = _loadFontTools()
    fd = FontData()
    fd.filePath = filePath
    fd.type = FontType.TTF
    fd.name = os.path.splitext(os.path.basename(filePath))[0]
    try:
        tt = TTFont(filePath, fontNumber=fontNumber, lazy=True)
        em = tt["head"].unitsPerEm
        try:
            cmap = tt.getBestCmap()
        except Exception:
            tt = TTFont(filePath, fontNumber=fontNumber, lazy=True)
            n = tt["maxp"].numGlyphs
            tt.setGlyphOrder(["glyph%d" % i for i in range(n)])
            cmap = tt.getBestCmap()
        glyphSet = tt.getGlyphSet()
        if not cmap:
            fd.error = "无 cmap 表"
            return fd
        fd.emSize = 1.0
        fd.lineSpacing = 1.2
        fd.defaultCharSpacing = 0.1
        fd.autoCloseContours = not _detectSingleLine(tt, glyphSet, cmap, em)
        fd.name = tt["name"].getDebugName(1) or fd.name

        def loader(c, _tt=tt, _gs=glyphSet, _cmap=cmap, _em=em,
                   _ac=fd.autoCloseContours, _rp=RecordingPen):
            gn = _cmap.get(ord(c))
            if gn is None:
                return None
            pen = _rp(_gs)
            try:
                _gs[gn].draw(pen)
            except Exception:
                return None
            if not pen.value:
                return None
            paths, bbMin, bbMax = _drawToPaths(pen.value, _em, _ac)
            if not paths:
                return None
            g = GlyphData()
            g.character = c
            g.paths = paths
            g.bbMin = bbMin
            g.bbMax = bbMax
            try:
                adv = _tt["hmtx"][gn][0]
                g.width = adv / _em
            except Exception:
                g.width = (bbMax[0] - bbMin[0]) if bbMin and bbMax else 0.5
            g.height = 1.0
            return g

        fd.glyphLoader = loader
        for c in "0123456789ABCDEFabcdef":
            g = loader(c)
            if g is not None:
                fd.glyphs[c] = g
        fd.isLoaded = True
    except Exception as ex:
        fd.error = str(ex)
        fd.isLoaded = False
    return fd
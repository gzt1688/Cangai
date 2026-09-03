"""SHP 字体读取器（AutoCAD 型源文本，移植自 C# SHPFontReader）。

解析 *CODE,NBYTES,NAME + 定义行：
  7,1,8,(x,y) 起点 | 1,9,(dx,dy),...,(0,0) 画线 |
  2,8,(dx,dy) 抬笔 | 7,2,0 结束
GB2312 双字节码→Unicode 字符。坐标平移到(0,0)后归一化。
"""
from __future__ import annotations

import os
import re
from typing import Optional
from .font_data import FontData, FontType, GlyphData, Point

_TOK_RE = re.compile(r"\((-?\d+),(-?\d+)\)|(-?\d+)")


def _decodeShape(defn):
    tokens = []
    for m in _TOK_RE.finditer(defn):
        if m.group(1) is not None:
            tokens.append((int(m.group(1)), int(m.group(2))))
        else:
            tokens.append(int(m.group(3)))
    result = []
    cx = cy = 0.0
    started = False
    cur = None
    k = 0
    n = len(tokens)
    while k < n:
        vi = tokens[k]
        if not isinstance(vi, int):
            k += 1
            continue
        if vi == 7 and k + 1 < n and isinstance(tokens[k + 1], int):
            sub = tokens[k + 1]
            if sub == 1 and k + 3 < n and tokens[k + 2] == 8 and isinstance(tokens[k + 3], tuple):
                cx, cy = float(tokens[k + 3][0]), float(tokens[k + 3][1])
                cur = [(cx, cy)]
                started = True
                k += 4
                continue
            if sub == 2:
                if cur:
                    result.append(cur)
                cur = None
                started = False
                k += 3
                continue
            k += 1
            continue
        if vi == 1 and k + 1 < n and tokens[k + 1] == 9:
            k += 2
            while k < n:
                if isinstance(tokens[k], tuple):
                    dx, dy = tokens[k]
                    if dx == 0 and dy == 0:
                        break
                    cx += dx; cy += dy
                    if started:
                        cur.append((cx, cy))
                    else:
                        cur = [(cx, cy)]
                        started = True
                    k += 1
                else:
                    break
            continue
        if vi == 2 and k + 1 < n and tokens[k + 1] == 8 and k + 2 < n and isinstance(tokens[k + 2], tuple):
            dx, dy = tokens[k + 2]
            cx += dx; cy += dy
            if cur:
                result.append(cur)
            cur = [(cx, cy)]
            started = True
            k += 3
            continue
        k += 1
    if cur:
        result.append(cur)
    return result


def loadFromFile(filePath: str) -> FontData:
    fd = FontData()
    fd.type = FontType.SHX
    fd.filePath = filePath
    fd.name = os.path.splitext(os.path.basename(filePath))[0]
    fd.autoCloseContours = False
    try:
        if not os.path.isfile(filePath):
            raise FileNotFoundError(f"字体文件不存在: {filePath}")
        try:
            with open(filePath, "r", encoding="gb2312") as f:
                lines = f.readlines()
        except Exception:
            with open(filePath, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        rawGlyphs = []
        curCode = None
        inGlyph = False
        sb = []
        for line in lines:
            s = line.strip()
            if not s:
                continue
            if s.startswith("*"):
                if curCode is not None and sb:
                    rawGlyphs.append((curCode, "".join(sb)))
                sb = []
                body = s[1:]
                if body.upper().startswith("BIGFONT"):
                    curCode = None; inGlyph = False
                    continue
                parts = body.split(",", 2)
                try:
                    curCode = int(parts[0].strip())
                    inGlyph = True
                except Exception:
                    curCode = None; inGlyph = False
                continue
            if inGlyph and curCode is not None:
                sb.append(s)
                sb.append(",")
        if curCode is not None and sb:
            rawGlyphs.append((curCode, "".join(sb)))
        if not rawGlyphs:
            raise ValueError("未找到任何字形定义")
        decoded = []
        gMinX = gMinY = float("inf")
        gMaxX = gMaxY = -float("inf")
        for code, defn in rawGlyphs:
            paths = _decodeShape(defn)
            if not paths:
                continue
            for path in paths:
                for x, y in path:
                    if x < gMinX: gMinX = x
                    if y < gMinY: gMinY = y
                    if x > gMaxX: gMaxX = x
                    if y > gMaxY: gMaxY = y
            decoded.append((code, paths))
        emSize = gMaxY - gMinY
        if emSize <= 0:
            emSize = 256.0
        mapped = 0
        for code, paths in decoded:
            ch: Optional[str] = None
            if 32 <= code <= 126:
                ch = chr(code)
            elif 0x100 <= code <= 0xFFFF:
                try:
                    s = bytes([(code >> 8) & 0xFF, code & 0xFF]).decode("gb2312")
                    if len(s) == 1:
                        ch = s
                except Exception:
                    pass
            if ch is None:
                continue
            g = GlyphData()
            g.character = ch
            lMinX = float("inf"); lMaxX = -float("inf")
            for path in paths:
                if len(path) < 2:
                    continue
                np = []
                for x, y in path:
                    nx = (x - gMinX) / emSize
                    ny = (y - gMinY) / emSize
                    np.append((nx, ny))
                    if nx < lMinX: lMinX = nx
                    if nx > lMaxX: lMaxX = nx
                g.paths.append(np)
            if not g.paths:
                continue
            g.bbMin = (lMinX, 0.0)
            g.bbMax = (lMaxX, 1.0)
            g.width = lMaxX - lMinX
            g.height = 1.0
            if ch not in fd.glyphs:
                fd.glyphs[ch] = g
                mapped += 1
        if mapped == 0:
            raise ValueError("未能将任何字形映射到字符")
        fd.emSize = 1.0
        fd.lineSpacing = 1.0
        fd.defaultCharSpacing = 0.1
        fd.isLoaded = True
    except Exception as ex:
        fd.isLoaded = False
        fd.error = str(ex)
    return fd
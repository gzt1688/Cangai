"""SHX 字体读取器（AutoCAD bigfont 二进制，移植自 C# SHXFontReader）。

头部 magic 0x0D0A1A08 + u16目录项数 + 8字节记录[code:u16][len:u16][offset:u32]
字形命令流：07 01 08 sx sy 起点 | 01 09 dx dy...00 00 画线 |
            02 08 dx dy 抬笔 | 07 02 00 结束
坐标平移到(0,0)后归一化（除以 emSize）。
"""
from __future__ import annotations

import os
import struct
from typing import Optional
from .font_data import FontData, FontType, GlyphData, Point


def _findDirStart(b):
    magic = (0x0D, 0x0A, 0x1A, 0x08)
    n = len(b)
    for i in range(n - 3):
        if (b[i], b[i + 1], b[i + 2], b[i + 3]) == magic:
            return i + 4
    return -1


def _findGlyphStart(b, off):
    n = len(b)
    limit = min(off + 16, n - 2)
    for i in range(off, limit + 1):
        if b[i] == 0x07 and b[i + 1] == 0x01 and b[i + 2] == 0x08:
            return i
    return -1


def _decodeGlyph(b, start):
    n = len(b)
    if start + 5 > n:
        return None
    if not (b[start] == 0x07 and b[start + 1] == 0x01 and b[start + 2] == 0x08):
        return None
    i = start + 3
    cx = cy = 0
    subs = []
    while i + 1 < n:
        b0, b1 = b[i], b[i + 1]
        if b0 == 0x07 and b1 == 0x01 and i + 4 < n and b[i + 2] == 0x08:
            cx = struct.unpack_from("b", b, i + 3)[0]
            cy = struct.unpack_from("b", b, i + 4)[0]
            subs.append([(float(cx), float(cy))])
            i += 5
        elif b0 == 0x01 and b1 == 0x09:
            i += 2
            while i + 1 < n:
                dx = struct.unpack_from("b", b, i)[0]
                dy = struct.unpack_from("b", b, i + 1)[0]
                i += 2
                if dx == 0 and dy == 0:
                    break
                cx += dx; cy += dy
                if subs:
                    subs[-1].append((float(cx), float(cy)))
        elif b0 == 0x02 and b1 == 0x08:
            if i + 3 < n:
                dx = struct.unpack_from("b", b, i + 2)[0]
                dy = struct.unpack_from("b", b, i + 3)[0]
                i += 4
                cx += dx; cy += dy
                subs.append([(float(cx), float(cy))])
            else:
                break
        elif b0 == 0x07 and b1 == 0x02 and i + 2 < n and b[i + 2] == 0x00:
            break
        else:
            i += 1
    subs = [s for s in subs if len(s) >= 2]
    return subs if subs else None


def _isDBCS(code):
    hi = (code >> 8) & 0xFF
    lo = code & 0xFF
    return 0xA1 <= hi <= 0xF7 and 0xA1 <= lo <= 0xFE


def _codeToChar(code):
    if 32 <= code <= 126:
        return chr(code)
    hi = (code >> 8) & 0xFF
    lo = code & 0xFF
    try:
        s = bytes([hi, lo]).decode("gb2312")
        if s:
            ch = s[0]
            if 0xFF10 <= ord(ch) <= 0xFF19:
                return chr(ord(ch) - 0xFF10 + ord("0"))
            if 0xFF21 <= ord(ch) <= 0xFF3A:
                return chr(ord(ch) - 0xFF21 + ord("A"))
            if 0xFF41 <= ord(ch) <= 0xFF5A:
                return chr(ord(ch) - 0xFF41 + ord("a"))
            return ch
    except Exception:
        pass
    return "\ufffd"


def loadFromFile(filePath: str) -> FontData:
    fd = FontData()
    fd.type = FontType.SHX
    fd.filePath = filePath
    fd.name = os.path.splitext(os.path.basename(filePath))[0]
    fd.autoCloseContours = False
    if not os.path.isfile(filePath):
        fd.error = f"字体文件不存在: {filePath}"
        return fd
    with open(filePath, "rb") as f:
        b = f.read()
    return _loadFromBytes(b, fd.name, filePath)


def _loadFromBytes(b, nameHint, filePath="") -> FontData:
    fd = FontData()
    fd.type = FontType.SHX
    fd.name = nameHint
    fd.filePath = filePath
    fd.autoCloseContours = False
    try:
        n = len(b)
        if n < 64:
            raise ValueError("文件过小，非有效 SHX")
        raw = []
        seen = set()
        dirStart = _findDirStart(b)
        usedReal = False
        if dirStart >= 0:
            count = b[dirStart] | (b[dirStart + 1] << 8)
            dirEnd = dirStart + 2 + count * 8
            if 0 < count <= 65535 and dirEnd <= n:
                p = dirStart + 2
                for _ in range(count):
                    code = b[p] | (b[p + 1] << 8)
                    off = (b[p + 4] | (b[p + 5] << 8) |
                           (b[p + 6] << 16) | (b[p + 7] << 24))
                    p += 8
                    if code == 0 or off < dirEnd or off + 8 > n:
                        continue
                    gs = _findGlyphStart(b, off)
                    if gs < 0:
                        continue
                    paths = _decodeGlyph(b, gs)
                    if paths and code not in seen:
                        seen.add(code)
                        raw.append((code, paths))
                if len(raw) > 50:
                    usedReal = True
        if not usedReal:
            raw.clear(); seen.clear()
            for p in range(n - 7):
                code = b[p] | (b[p + 1] << 8)
                isDbcs = 0x8140 <= code <= 0xFEFE and _isDBCS(code)
                isAscii = 32 <= code <= 126
                if not isDbcs and not isAscii:
                    continue
                off = (b[p + 4] | (b[p + 5] << 8) |
                       (b[p + 6] << 16) | (b[p + 7] << 24))
                if off < 64 or off + 8 > n:
                    continue
                gs = _findGlyphStart(b, off)
                if gs < 0:
                    continue
                paths = _decodeGlyph(b, gs)
                if paths and code not in seen:
                    seen.add(code)
                    raw.append((code, paths))
        if not raw:
            raise ValueError("未找到有效字形目录")
        gMinX = gMinY = float("inf")
        gMaxX = gMaxY = -float("inf")
        for _, paths in raw:
            for sub in paths:
                for x, y in sub:
                    if x < gMinX: gMinX = x
                    if y < gMinY: gMinY = y
                    if x > gMaxX: gMaxX = x
                    if y > gMaxY: gMaxY = y
        emSize = gMaxY - gMinY
        if emSize <= 0:
            emSize = 1.0
        shiftX, shiftY = -gMinX, -gMinY
        for code, paths in raw:
            ch = _codeToChar(code)
            if ch == "\ufffd":
                continue
            g = GlyphData()
            g.character = ch
            lMinX = float("inf"); lMaxX = -float("inf")
            for sub in paths:
                if len(sub) < 2:
                    continue
                path = []
                for x, y in sub:
                    nx = (x + shiftX) / emSize
                    ny = (y + shiftY) / emSize
                    path.append((nx, ny))
                    if nx < lMinX: lMinX = nx
                    if nx > lMaxX: lMaxX = nx
                g.paths.append(path)
            if not g.paths:
                continue
            g.bbMin = (lMinX, 0.0)
            g.bbMax = (lMaxX, 1.0)
            g.width = lMaxX - lMinX
            g.height = 1.0
            fd.glyphs[ch] = g
        if not fd.glyphs:
            raise ValueError("无字形映射成功")
        fd.emSize = 1.0
        fd.lineSpacing = 1.2
        fd.defaultCharSpacing = 0.1
        fd.isLoaded = True
    except Exception as ex:
        fd.isLoaded = False
        fd.error = str(ex)
    return fd
"""SGL 字体读取器（精雕 JDPaint 单线字体，移植自 C# SGLFontReader）。

二进制格式：
  头部 "JDPAINT FONT" + GBK名称(off20) + float em(off92)
       + u32 dataEnd(off104) + u32 idxStart(off108)
  索引表 94区×94位=8836条，每条8字节{u32 offset,u32 strokeCount}
  字形块：每笔画 u32头(高16=点数,低16=类型) + 点数×{float x,float y}
    类型低字节 1=直线 2=样条；坐标0~em，Y向上
  尾部扩展区（dataEnd 起）：256 项×12字节 ASCII 索引表
    {float, u32 offset, u32 类别<<16|块数}，表后(偏移 tail+0xC00)为
    94 个可打印 ASCII 字符的字形块，块区间=自身偏移→下一非零偏移；
    表内无宽度（float 恒 -1），半角固定按 0.5em 前进。
样条按尖角拆分后用钳位 B 样条/插值曲线采样为折线。
全重合的退化点块（如 ! . ; i 的圆点）渲染为小圆。
"""
from __future__ import annotations

import os
import math
import struct
from typing import Optional
from .font_data import (FontData, FontType, GlyphData, Point,
                        _clampedBSpline, _interpCurve, _polyline)

_MAGIC = b"JDPAINT FONT"
_BASELINE_GUARD = 0.0
_CORNER_DEG = 70.0
_SPLINE_SEG = 24


def _readInt32(b, off):
    if off < 0 or off + 4 > len(b):
        return 0
    return struct.unpack_from("<i", b, off)[0]


def _readUInt32(b, off):
    if off < 0 or off + 4 > len(b):
        return 0
    return struct.unpack_from("<I", b, off)[0]


def _readFloat(b, off):
    if off < 0 or off + 4 > len(b):
        return 0.0
    return struct.unpack_from("<f", b, off)[0]


def _readEmbeddedName(b, off, maxLen):
    try:
        ln = 0
        while ln < maxLen and off + ln < len(b) and b[off + ln] != 0:
            ln += 1
        if ln == 0:
            return None
        return b[off:off + ln].decode("gb2312", errors="ignore").strip()
    except Exception:
        return None


def _removeDuplicates(pts, eps=1e-3):
    out = []
    for p in pts:
        if not out or math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) > eps:
            out.append(p)
    if len(out) >= 3:
        closed = math.hypot(out[0][0] - out[-1][0],
                            out[0][1] - out[-1][1]) <= eps
        if (not closed and
                math.hypot(out[-1][0] - out[-2][0],
                           out[-1][1] - out[-2][1]) <= eps):
            out.pop()
    return out


def _splitSplineAtCorners(pts):
    n = len(pts)
    turn = [None] * n
    for i in range(1, n - 1):
        ax, ay = pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]
        bx, by = pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1]
        la = math.hypot(ax, ay); lb = math.hypot(bx, by)
        if la < 1e-9 or lb < 1e-9:
            continue
        d = (ax * bx + ay * by) / (la * lb)
        d = max(-1.0, min(1.0, d))
        turn[i] = math.acos(d) * 180.0 / math.pi

    elig = [False] * n
    for i in range(1, n - 1):
        if turn[i] is None or turn[i] < _CORNER_DEG:
            continue
        isHookTip = ((n - 1 - i) <= 2 and
                     pts[i][1] <= pts[i - 1][1] and
                     pts[i][1] <= pts[i + 1][1])
        elig[i] = (not isHookTip) and pts[i][1] >= _BASELINE_GUARD

    corner = [False] * n
    idx = 1
    while idx < n - 1:
        if elig[idx]:
            j = idx
            while j + 1 < n - 1 and elig[j + 1]:
                j += 1
            corner[idx] = True
            corner[j] = True
            idx = j + 1
        else:
            idx += 1

    segs = []
    start = 0
    for k in range(1, n - 1):
        if corner[k]:
            segs.append(pts[start:k + 1])
            start = k
    segs.append(pts[start:n])
    return segs


def _addCurveOrPolyline(curves, seg):
    if not seg or len(seg) < 2:
        return
    distinct = 1
    for q in range(1, len(seg)):
        if math.hypot(seg[q][0] - seg[q - 1][0],
                      seg[q][1] - seg[q - 1][1]) > 1e-6:
            distinct += 1
    if distinct < 2:
        return
    if len(seg) >= 4:
        pts = _clampedBSpline(seg, 3, _SPLINE_SEG)
    elif len(seg) == 3:
        if seg[1][1] < _BASELINE_GUARD:
            pts = _clampedBSpline(seg, 2, _SPLINE_SEG)
        else:
            pts = _interpCurve(seg, _SPLINE_SEG)
    else:
        pts = _polyline(seg)
    if pts and len(pts) >= 2:
        curves.append(pts)


def _dotPath(cx, cy, r, seg=12):
    """圆点折线（退化点块专用，闭合环）。"""
    return [(cx + r * math.cos(2.0 * math.pi * k / seg),
             cy + r * math.sin(2.0 * math.pi * k / seg))
            for k in range(seg + 1)]


def _parseGlyphBlock(b, start, end, em):
    paths = []
    minX = minY = float("inf")
    maxX = maxY = -float("inf")
    p = start
    guard = 0
    while p + 4 <= end and guard < 4096:
        guard += 1
        hdr = _readUInt32(b, p)
        npts = hdr >> 16
        typ = hdr & 0xFFFF
        p += 4
        if npts < 2 or npts > 2000 or p + npts * 8 > end:
            break
        pts = []
        for _ in range(npts):
            x = _readFloat(b, p); y = _readFloat(b, p + 4)
            p += 8
            pts.append((x, y))
        # 退化点块：所有点重合（! . ; i 的圆点）→ 渲染为小圆
        if all(math.hypot(q[0] - pts[0][0], q[1] - pts[0][1]) <= 1e-3
               for q in pts[1:]):
            cx, cy = pts[0]
            r = em * 0.02
            paths.append(_dotPath(cx, cy, r))
            if cx - r < minX: minX = cx - r
            if cx + r > maxX: maxX = cx + r
            if cy - r < minY: minY = cy - r
            if cy + r > maxY: maxY = cy + r
            continue
        for x, y in pts:
            if x < minX: minX = x
            if x > maxX: maxX = x
            if y < minY: minY = y
            if y > maxY: maxY = y
        lo = typ & 0xFF
        if lo == 2 and len(pts) >= 3:
            cleaned = _removeDuplicates(pts)
            if len(cleaned) >= 3:
                for seg in _splitSplineAtCorners(cleaned):
                    _addCurveOrPolyline(paths, seg)
            elif len(cleaned) >= 2:
                paths.append(_polyline(cleaned))
        else:
            if len(pts) >= 2:
                paths.append(_polyline(pts))
    if not paths:
        return None
    if minX > maxX:
        minX, maxX = 0.0, em
    if minY > maxY:
        minY, maxY = 0.0, em
    g = GlyphData()
    g.paths = [[(x / em, y / em) for x, y in path] for path in paths]
    g.bbMin = (minX / em, minY / em)
    g.bbMax = (maxX / em, maxY / em)
    g.width = 1.0
    g.height = 1.0
    return g


def _loadASCIIGlyphs(b, fd, tail, em):
    """尾部扩展区加载 94 个可打印 ASCII 字形（数字/字母/符号）。

    尾部结构：256 项×12 字节索引表 {float(恒-1), u32 offset, u32 类别/块数}，
    字形数据在表之后；每个字符的块区间 = 自身 offset → 下一非零 offset
    （最后一个延伸到文件尾）。区间解析已验证可 100% 精确走完每个字形。"""
    tableEnd = tail + 256 * 12
    if tableEnd > len(b):
        return 0
    offs = [_readUInt32(b, tail + c * 12 + 4) for c in range(256)]
    count = 0
    for code in range(0x21, 0x7F):
        off = offs[code]
        if off < tableEnd or off >= len(b):
            continue
        end = len(b)
        for c2 in range(code + 1, 256):
            if tableEnd <= offs[c2] < len(b):
                end = offs[c2]
                break
        g = _parseGlyphBlock(b, off, end, em)
        if g is None:
            continue
        ch = chr(code)
        g.character = ch
        g.width = 0.5  # 半角字符固定 0.5em 前进
        if ch not in fd.glyphs:
            fd.glyphs[ch] = g
            count += 1
    return count


def loadFromFile(filePath: str) -> FontData:
    fd = FontData()
    fd.type = FontType.SGL
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
    fd.type = FontType.SGL
    fd.name = nameHint
    fd.filePath = filePath
    fd.autoCloseContours = False
    try:
        if len(b) < 256:
            raise ValueError("文件过小，非有效 SGL")
        em = _readFloat(b, 92)
        if em <= 0 or em > 100000 or math.isnan(em):
            em = 1000.0
        dataEnd = _readInt32(b, 104)
        if dataEnd <= 0 or dataEnd > len(b):
            dataEnd = len(b)
        idxStart = _readInt32(b, 108)
        if idxStart <= 0 or idxStart >= len(b):
            idxStart = 212
        entryCount = 94 * 94
        entryOffsets = []
        for i in range(entryCount):
            off = idxStart + i * 8
            if off + 8 > len(b):
                break
            entryOffsets.append(_readInt32(b, off))
        sortedOffs = sorted({o for o in entryOffsets if 0 < o < dataEnd})
        if not sortedOffs:
            raise ValueError("索引表无有效字形偏移")
        loaded = 0
        for i, off in enumerate(entryOffsets):
            if off <= 0 or off >= dataEnd:
                continue
            pos = _bisect(sortedOffs, off)
            blockEnd = sortedOffs[pos + 1] if pos + 1 < len(sortedOffs) else dataEnd
            qu = i // 94 + 1
            wei = i % 94 + 1
            try:
                ch = bytes([0xA0 + qu, 0xA0 + wei]).decode("gb2312")
                if not ch or ch == "\ufffd":
                    continue
                ch = ch[0]
            except Exception:
                continue
            g = _parseGlyphBlock(b, off, blockEnd, em)
            if g is None:
                continue
            g.character = ch
            fd.glyphs[ch] = g
            loaded += 1
        # 尾部扩展区：补齐 ASCII 数字/字母/符号（GB 区不含半角 ASCII）。
        # 先加载再统一判空——只有尾部 ASCII 字形、无 GB 字形的 SGL 也是
        # 合法字体，提前 raise 会让它永远报"未解析到任何有效字形"
        asciiLoaded = _loadASCIIGlyphs(b, fd, dataEnd, em)
        if loaded == 0 and asciiLoaded == 0:
            raise ValueError("未解析到任何有效字形")
        fd.emSize = 1.0
        fd.lineSpacing = 1.0
        fd.defaultCharSpacing = 0.1
        fd.isLoaded = True
    except Exception as ex:
        fd.isLoaded = False
        fd.error = str(ex)
    # 细圆单线：数字与字母用规整单线字形替换（原 SGL 解析笔画歪斜不顺）
    if fd.isLoaded and nameHint == "细圆单线":
        from . import single_line_glyphs
        single_line_glyphs.override(fd)
    return fd


def _bisect(sortedList, value):
    import bisect
    return bisect.bisect_left(sortedList, value)


def isValidSGLFile(filePath: str) -> bool:
    try:
        with open(filePath, "rb") as f:
            head = f.read(len(_MAGIC))
        return head == _MAGIC
    except Exception:
        return False
"""SVG 导入：解析 SVG 图形元素，转换为本软件曲线对象并合并进当前文档。

元素映射（与 DXF 导入的对象粒度一致）：
  <circle>        → CircleObject（transform 为均匀缩放+旋转时）
  <ellipse>       → EllipseObject（同上；非均匀变换退化为采样折线）
  其余元素        → LineObject 采样折线：
    <path> 按子路径拆分（闭合子路径 setClosed；贝塞尔/圆弧已自适应采样）
    <line> 两点；<rect> 四角闭合（圆角按直角处理）；
    <polyline>/<polygon> 顶点折线

坐标系：SVG y 向下 → 世界 y 向上（绕水平轴翻转，尺寸不变，y=0 在图底边）。
单位：width/height 带物理单位（mm/cm/in/pt/pc）时按物理尺寸换算到 mm；
无物理单位（含 px）按 1 用户单位 = 1 mm 处理，便于雕刻/加工场景。
颜色：优先描边色，其次填充色，未识别用默认。g 嵌套 transform 已由
svg_io.parseSvg 应用。整批导入 beginEdit/endEdit 包裹为一条撤销记录。
"""
from __future__ import annotations
import math

from ..vector.geometry_types import Point3D
from ..vector.line_object import LineObject
from ..vector.circle_object import CircleObject
from ..vector.ellipse_object import EllipseObject
from ..vector.svg_io import parseSvg, _apply

# 折线抽稀容差（世界单位）：点距折线弦的最大保留偏差
_RESAMPLE_TOL = 0.2


def _rdpOpen(pts: list, tol: float) -> list:
    """开放折线 Douglas-Peucker（迭代实现）。"""
    n = len(pts)
    if n < 3:
        return list(pts)
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    stack = [(0, n - 1)]
    while stack:
        first, last = stack.pop()
        if last - first < 2:
            continue
        ax, ay = pts[first]
        bx, by = pts[last]
        maxD2 = -1.0
        idx = -1
        for i in range(first + 1, last):
            d2 = segDist2(pts[i][0], pts[i][1], ax, ay, bx, by)
            if d2 > maxD2:
                maxD2 = d2
                idx = i
        if maxD2 > tol * tol:
            keep[idx] = True
            stack.append((first, idx))
            stack.append((idx, last))
    return [pts[i] for i in range(n) if keep[i]]


def segDist2(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 < 1e-18:
        return (px - ax) ** 2 + (py - ay) ** 2
    t = ((px - ax) * dx + (py - ay) * dy) / l2
    t = min(1.0, max(0.0, t))
    qx, qy = px - (ax + t * dx), py - (ay + t * dy)
    return qx * qx + qy * qy


def _resample(pts: list, closed: bool, tol: float) -> list:
    """按弦高容差抽稀密集采样点（世界单位）。闭合环取距质心最远点 A、
    距 A 最远点 B 作锚点分两段简化，避免首尾重合导致 RDP 退化。"""
    n = len(pts)
    if n < 3 or tol <= 0:
        return list(pts)
    if not closed:
        return _rdpOpen(pts, tol)
    cx = sum(p[0] for p in pts) / n
    cy = sum(p[1] for p in pts) / n
    iA = max(range(n), key=lambda i: (pts[i][0] - cx) ** 2 + (pts[i][1] - cy) ** 2)
    iB = max(range(n), key=lambda i: (pts[i][0] - pts[iA][0]) ** 2 +
             (pts[i][1] - pts[iA][1]) ** 2)

    def takeWrap(i0, i1) -> list:
        out = [pts[i0]]
        i = i0
        while i != i1:
            i = (i + 1) % n
            out.append(pts[i])
        return out

    s1 = _rdpOpen(takeWrap(iA, iB), tol)
    s2 = _rdpOpen(takeWrap(iB, iA), tol)
    out = s1[:-1] + s2[:-1]
    cleaned = []
    for p in out:
        if not cleaned or abs(p[0] - cleaned[-1][0]) > 1e-9 or \
                abs(p[1] - cleaned[-1][1]) > 1e-9:
            cleaned.append(p)
    if len(cleaned) >= 3:
        cleaned.append(cleaned[0])
        return cleaned
    return list(pts)


def _uniformScaleRotation(m: list):
    """矩阵 (a,b,c,d,e,f) 可分解为 均匀缩放×旋转 时返回 (s, 角度rad)，否则 None。"""
    a, b, c, d = m[0], m[1], m[2], m[3]
    det = a * d - b * c
    s = math.sqrt(abs(det))
    if s < 1e-12:
        return None
    # (a,b,c,d)/s 应为正交矩阵（列向量模长 1 且互相垂直）
    c1 = (a * a + b * b) / (s * s)
    c2 = (c * c + d * d) / (s * s)
    dot = (a * c + b * d) / (s * s)
    if abs(c1 - 1.0) > 1e-6 or abs(c2 - 1.0) > 1e-6 or abs(dot) > 1e-6:
        return None
    return s, math.atan2(b, a)


def importSVG(doc, path: str, outputWidth: float | None = None) -> dict:
    """读取 SVG 文件并把曲线元素合并进 doc（当前文档）。

    outputWidth 不为 None 时整图等比缩放到该宽度（世界单位）；
    否则按物理尺寸换算，无物理尺寸时 1 用户单位 = 1 mm。
    返回统计：{"imported": 导入对象数, "skipped": 跳过元素数, "empty": 无内容}。
    """
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    info = parseSvg(text)
    stats = {"imported": 0, "skipped": info.skipped, "empty": True}
    if not info.paths:
        return stats

    # 缩放比例（用户单位 → 世界单位）
    if outputWidth and outputWidth > 0 and info.width > 1e-9:
        scale = outputWidth / info.width
    elif info.physicalMm and info.physicalMm[0] > 1e-9 and info.width > 1e-9:
        scale = info.physicalMm[0] / info.width
    else:
        scale = 1.0
    flipY = info.height * scale   # 世界 y = flipY - svg_y·scale

    def mapPt(x, y) -> Point3D:
        return Point3D(x * scale, flipY - y * scale, 0.0)

    def subpathObjs(entry) -> list:
        objs = []
        for sp in entry["subpaths"]:
            pts = [(p[0] * scale, flipY - p[1] * scale) for p in sp["points"]]
            pts = _resample(pts, sp["closed"], _RESAMPLE_TOL)
            if len(pts) < 2:
                continue
            lo = LineObject([Point3D(x, y, 0.0) for x, y in pts])
            if sp["closed"]:
                lo.setClosed(True)
            objs.append(lo)
        return objs

    doc.beginEdit()
    try:
        for entry in info.paths:
            color = entry["color"]
            objs = []
            if entry["kind"] == "ellipse":
                cx, cy, rx, ry = entry["geo"]
                dec = _uniformScaleRotation(entry["matrix"])
                if dec is not None:
                    s, rot = dec
                    c = mapPt(cx, cy)
                    if abs(rx - ry) < 1e-9 * max(rx, ry):
                        objs = [CircleObject(c.x, c.y, 0.0, rx * s)]
                    else:
                        eo = EllipseObject(c.x, c.y, 0.0, rx * s, ry * s)
                        eo.setRotation(rot)
                        objs = [eo]
            if not objs:
                objs = subpathObjs(entry)
            for obj in objs:
                doc.addObject(obj)
                if color is not None:
                    obj.setColor(color)
                stats["imported"] += 1
                stats["empty"] = False
    finally:
        doc.endEdit()
    return stats

"""SVG 导出：文档曲线对象采样为折线，写出带描边的 SVG <path>。

对象 → 路径映射（与 DXF 导出同一批曲线类型）：
  LINE/POLYCURVE 折线段 → polyline path（闭合标志保留）
  CIRCLE → 圆采样；ARC → 弧采样（扫掠方向保留）；ELLIPSE → 参数采样
  CURVE（Catmull-Rom）/NURBS_CURVE → getSampledPoints 采样
颜色：对象色 → 图层色 → 黑色。y 轴翻转（世界 y 向上 → SVG y 向下），
viewBox 以内容包围盒为准，单位 = mm（无物理单位声明）。
点对象（POINT）无轮廓意义，跳过不导出。
"""
from __future__ import annotations
import math

from ..vector.geometry_types import ObjectType, Point3D
from ..document import LAYER_CURVES_ID, isFixedLayerId
from ..vector.svg_io import formatPathData

_EXPORTABLE = {
    ObjectType.LINE, ObjectType.CIRCLE, ObjectType.ARC, ObjectType.ELLIPSE,
    ObjectType.CURVE, ObjectType.NURBS_CURVE, ObjectType.POLYCURVE,
}

_SEG_CIRCLE = 72          # 整圆采样段数
_SEG_PER_DEG = 1 / 3.0    # 弧采样密度（每度 1/3 段 ≈ 3°/段）


def _sampleLine(obj) -> tuple:
    pts = [(p.x, p.y) for p in obj.points()]
    if len(pts) >= 2 and math.dist(pts[0], pts[-1]) < 1e-9:
        pts = pts[:-1]   # 末点重复由 closed 标志表达
    return pts, bool(obj.isClosed())


def _sampleCircle(obj) -> tuple:
    c, r = obj.center(), obj.radius()
    if r <= 0:
        return [], False
    pts = [(c.x + r * math.cos(2 * math.pi * i / _SEG_CIRCLE),
            c.y + r * math.sin(2 * math.pi * i / _SEG_CIRCLE))
           for i in range(_SEG_CIRCLE)]
    return pts, True


def _sampleArc(obj) -> tuple:
    c, r = obj.center(), obj.radius()
    sa, ea = obj.startAngle(), obj.endAngle()
    span = ea - sa
    if r <= 0 or abs(span) < 1e-9:
        return _sampleCircle(obj)
    n = max(4, int(math.ceil(abs(math.degrees(span)) * _SEG_PER_DEG)))
    pts = [(c.x + r * math.cos(sa + span * i / n),
            c.y + r * math.sin(sa + span * i / n)) for i in range(n + 1)]
    return pts, False


def _sampleEllipse(obj) -> tuple:
    c = obj.center()
    a, b = abs(obj.majorAxis()), abs(obj.minorAxis())
    rot = obj.rotation()
    if a < 1e-12 or b < 1e-12:
        return [], False
    n = _SEG_CIRCLE
    ca, sa = math.cos(rot), math.sin(rot)
    pts = []
    for i in range(n + 1):
        t = 2 * math.pi * i / n
        ex, ey = a * math.cos(t), b * math.sin(t)
        pts.append((c.x + ex * ca - ey * sa, c.y + ex * sa + ey * ca))
    return pts, True


def _sampleCurve(obj) -> tuple:
    try:
        pts3 = obj.getSampledPoints(24)
    except Exception:
        return [], False
    return [(p.x, p.y) for p in pts3], bool(obj.isClosed())


def _sampleNurbs(obj) -> tuple:
    try:
        pts3 = obj.getSampledPoints(32)   # 全部段统一采样
    except Exception:
        return [], False
    return [(p.x, p.y) for p in pts3], bool(obj.isClosed())


def _samplePolycurve(obj) -> tuple:
    """逐段采样拼接：平滑段（inputPoints≥3）走 Catmull-Rom，其余用折线点。"""
    from ..spline_utils import catmullRom
    pts: list = []
    for seg in obj.segments():
        inp = seg.get("inputPoints")
        if inp and len(inp) >= 3:
            cps = [Point3D(float(p[0]), float(p[1]), float(p[2])) for p in inp]
            segPts = [(p.x, p.y) for p in catmullRom(cps, 12)]
        else:
            segPts = [(float(p[0]), float(p[1]))
                      for p in seg.get("points", [])]
        for p in segPts:
            if not pts or math.dist(pts[-1], p) > 1e-9:
                pts.append(p)
    closed = len(pts) >= 3 and math.dist(pts[0], pts[-1]) < 1e-6
    if closed:
        pts = pts[:-1]
    return pts, closed


_SAMPLERS = {
    ObjectType.LINE: _sampleLine,
    ObjectType.CIRCLE: _sampleCircle,
    ObjectType.ARC: _sampleArc,
    ObjectType.ELLIPSE: _sampleEllipse,
    ObjectType.CURVE: _sampleCurve,
    ObjectType.NURBS_CURVE: _sampleNurbs,
    ObjectType.POLYCURVE: _samplePolycurve,
}


def _objColor(doc, obj) -> tuple:
    c = obj.color()
    if c:
        return tuple(int(v) for v in c[:3])
    try:
        layer = doc.layer(obj.layerId())
        if layer is not None and layer.color:
            return tuple(int(v) for v in tuple(layer.color)[:3])
    except Exception:
        pass
    return (0, 0, 0)


def exportSVG(doc, path: str) -> int:
    """把文档中全部曲线对象导出为 SVG；返回写出路径数。"""
    entries = []   # (pts, closed, color)
    for obj in doc.allObjects():
        t = obj.type()
        if t not in _EXPORTABLE:
            continue
        sampler = _SAMPLERS.get(t)
        if sampler is None:
            continue
        try:
            pts, closed = sampler(obj)
        except Exception:
            continue   # 单个对象失败不阻断整体导出
        if len(pts) >= 2:
            entries.append((pts, closed, _objColor(doc, obj)))
    if not entries:
        return 0

    xs = [p[0] for pts, _, _ in entries for p in pts]
    ys = [p[1] for pts, _, _ in entries for p in pts]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    w = max(maxx - minx, 1e-6)
    h = max(maxy - miny, 1e-6)
    # 世界 y 向上 → SVG y 向下：y_svg = maxy - y_world，viewBox 从 0 开始
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{w:.3f}mm" height="{h:.3f}mm" '
        f'viewBox="0 0 {w:.3f} {h:.3f}">',
    ]
    colors = {}
    for pts, closed, color in entries:
        mapped = [(p[0] - minx, maxy - p[1]) for p in pts]
        d = formatPathData(mapped, closed)
        if not d:
            continue
        hexc = "#%02X%02X%02X" % color
        colors.setdefault(hexc, []).append(d)
    for hexc, ds in colors.items():
        lines.append(f'<g fill="none" stroke="{hexc}" stroke-width="0.2" '
                     'stroke-linecap="round" stroke-linejoin="round">')
        for d in ds:
            lines.append(f'<path d="{d}"/>')
        lines.append("</g>")
    lines.append("</svg>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return len(entries)

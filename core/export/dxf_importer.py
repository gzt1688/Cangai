"""DXF 导入：读取 DXF 模型空间实体，转换为本软件曲线对象并合并进当前文档。

DXF 实体 → 本软件对象映射：
  POINT        → PointObject
  LINE         → LineObject（两点）
  CIRCLE       → CircleObject
  ARC          → ArcObject（角度制→弧度；扫掠角正向化为逆时针）
  ELLIPSE      → 整椭圆 EllipseObject；椭圆弧采样为 LineObject
  LWPOLYLINE   → 无凸度 LineObject；有凸度时直线段合并为 LineObject、
                 凸度段(bulge)转换为 ArcObject
  旧版 POLYLINE → 3D 折线 → LineObject；2D 同 LWPOLYLINE 处理
  SPLINE       → 有拟合点 → CurveObject(Catmull-Rom 过点)；
                 仅控制点 → NurbsCurveObject（保留节点/权重/次数）
  INSERT       → virtual_entities() 展开块参照后按上表转换
  其他实体（TEXT/MTEXT/HATCH/DIMENSION/3DFACE 等）跳过并计数

图层：DXF 0 层对象落当前层；其余图层在“曲线”固定层下建/复用同名子层
（颜色按 ACI→RGB）；实体显式颜色（非 BYLAYER/BYBLOCK）覆盖对象颜色。
整批导入由 beginEdit/endEdit 包裹为一条撤销记录。
"""
from __future__ import annotations
import math
from typing import List, Tuple

import ezdxf
from ezdxf.math import bulge_to_arc
from ezdxf.colors import aci2rgb

from ..vector.geometry_types import Point3D, CurveMode
from ..vector.point_object import PointObject
from ..vector.line_object import LineObject
from ..vector.circle_object import CircleObject
from ..vector.arc_object import ArcObject
from ..vector.ellipse_object import EllipseObject
from ..vector.curve_object import CurveObject
from ..vector.nurbs_curve_object import NurbsCurveObject, NurbsSegment, ControlPoint
from ..document import LAYER_CURVES_ID


_BULGE_EPS = 1e-12


# ---------------- 实体转换 ----------------

def _pt3(p) -> Point3D:
    """ezdxf Vec3 / numpy 数组 / 元组 → Point3D。"""
    return Point3D(float(p[0]), float(p[1]),
                   float(p[2]) if len(p) > 2 else 0.0)


def _convertPoint(e) -> list:
    p = e.dxf.location
    return [PointObject(p.x, p.y, p.z)]


def _convertLine(e) -> list:
    s, en = e.dxf.start, e.dxf.end
    return [LineObject([Point3D(s.x, s.y, s.z), Point3D(en.x, en.y, en.z)])]


def _convertCircle(e) -> list:
    c = e.dxf.center
    return [CircleObject(c.x, c.y, c.z, float(e.dxf.radius))]


def _convertArc(e) -> list:
    c = e.dxf.center
    r = float(e.dxf.radius)
    if r <= 0:
        return []
    sa = math.radians(float(e.dxf.start_angle))
    # DXF ARC 恒逆时针；end < start 表示扫掠跨过 0°
    sweep = math.radians((float(e.dxf.end_angle) - float(e.dxf.start_angle))
                         % 360.0)
    if sweep < 1e-9:
        sweep = 2.0 * math.pi
    return [ArcObject(c.x, c.y, c.z, r, sa, sa + sweep)]


def _convertEllipse(e) -> list:
    c = e.dxf.center
    mx, my = float(e.dxf.major_axis[0]), float(e.dxf.major_axis[1])
    major = math.hypot(mx, my)
    ratio = float(e.dxf.ratio)
    if major <= 1e-12 or not (0.0 < ratio <= 1.0 + 1e-6):
        return []
    ratio = min(ratio, 1.0)
    rot = math.atan2(my, mx)
    sp = float(e.dxf.start_param)
    ep = float(e.dxf.end_param)
    full = (abs((ep - sp) - 2.0 * math.pi) < 1e-4 or
            (sp <= 1e-6 and ep >= 2.0 * math.pi - 1e-6))
    if full:
        minor = major * ratio
        eo = EllipseObject(c.x, c.y, c.z, major, minor)
        eo.setRotation(rot)
        return [eo]
    # 椭圆弧：参数采样为折线
    n = 64
    pts: List[Point3D] = []
    cr, sr = math.cos(rot), math.sin(rot)
    for i in range(n + 1):
        t = sp + (ep - sp) * i / n
        ex = major * math.cos(t)
        ey = major * ratio * math.sin(t)
        pts.append(Point3D(c.x + ex * cr - ey * sr,
                           c.y + ex * sr + ey * cr, c.z))
    return [LineObject(pts)]


def _polylineFromVertices(xyz: List[Tuple[float, float, float]],
                          bulges: List[float], closed: bool) -> list:
    """顶点列 + 每顶点凸度 → 对象列表。

    凸度 bulge = tan(包含角/4)，正=逆时针，作用于“该顶点→下一顶点”段；
    闭合时最后一个顶点的凸度作用于“末顶点→首顶点”闭合段。
    直线段连续累积合并为一个 LineObject；凸度段输出 ArcObject。
    """
    n = len(xyz)
    if n == 0:
        return []
    if n == 1:
        return [PointObject(xyz[0][0], xyz[0][1], xyz[0][2])]
    seg_count = n if closed else n - 1
    has_bulge = any(abs(bulges[i]) > _BULGE_EPS for i in range(seg_count)
                    if i < len(bulges))
    if not has_bulge:
        lo = LineObject([Point3D(*p) for p in xyz])
        if closed:
            lo.setClosed(True)
        return [lo]

    out = []
    buf = [xyz[0]]

    def flush(close_to_start: bool) -> None:
        if len(buf) >= 2:
            lo = LineObject([Point3D(*p) for p in buf])
            if close_to_start:
                lo.setClosed(True)
            out.append(lo)

    for i in range(seg_count):
        j = (i + 1) % n
        p1, p2 = xyz[i], xyz[j]
        b = bulges[i] if i < len(bulges) else 0.0
        if abs(b) <= _BULGE_EPS:
            buf.append(p2)
        else:
            # 凸度弧前的直线段先成对象（缓冲仅含起点时无直线段）
            if len(buf) >= 2:
                flush(False)
            center, sa, ea, radius = bulge_to_arc(
                (p1[0], p1[1]), (p2[0], p2[1]), float(b))
            # bulge_to_arc 返回的 sa/ea 隐含扫掠角 = (ea - sa) mod 2π：
            # 正凸度（逆时针）时通常 sa > ea（跨 0°）。若把 sa/ea 原样传入
            # ArcObject，endAngle < startAngle 会被渲染为“顺时针”，
            # 弧镜像到弦的另一侧（导入错乱）。统一归一化为正扫掠。
            sweep = (ea - sa) % (2.0 * math.pi)
            if sweep < 1e-9:
                sweep = 2.0 * math.pi
            out.append(ArcObject(center.x, center.y, p1[2],
                                 float(radius), float(sa), float(sa + sweep)))
            buf = [p2]
    # 收尾：闭合且缓冲回到自身起点 → 闭合折线；否则开放折线
    if closed and len(buf) >= 2 and \
            math.dist(buf[0][:2], buf[-1][:2]) < 1e-9:
        flush(True)
    else:
        flush(False)
    return out


def _convertLwpolyline(e) -> list:
    verts = list(e.get_points("xyseb"))   # (x, y, start_w, end_w, bulge)
    z = float(e.dxf.get("elevation", 0.0))
    xyz = [(float(v[0]), float(v[1]), z) for v in verts]
    bulges = [float(v[4]) for v in verts]
    return _polylineFromVertices(xyz, bulges, bool(e.closed))


def _convertLegacyPolyline(e) -> list:
    if e.is_3d_polyline:
        pts = [Point3D(p.x, p.y, p.z) for p in e.points()]
        lo = LineObject(pts)
        if e.is_closed:
            lo.setClosed(True)
        return [lo]
    verts = list(e.vertices)
    xyz = [(float(v.dxf.location.x), float(v.dxf.location.y),
            float(v.dxf.location.z)) for v in verts]
    bulges = [float(v.dxf.get("bulge", 0.0)) for v in verts]
    return _polylineFromVertices(xyz, bulges, bool(e.is_closed))


def _convertSpline(e) -> list:
    degree = int(e.dxf.get("degree", 3))
    closed = bool(e.closed)
    fit = list(e.fit_points)
    if len(fit) >= 2:
        pts = [_pt3(p) for p in fit]
        co = CurveObject()
        co.buildFromPoints(pts, degree=max(2, min(degree, 3)),
                           mode=CurveMode.FIT)
        if closed:
            co.setClosed(True)
        return [co]
    ctrl = list(e.control_points)
    if len(ctrl) >= 2:
        k = max(1, min(degree, len(ctrl) - 1))
        raw_w = list(e.weights)
        weights = [float(w) for w in raw_w] if len(raw_w) == len(ctrl) else None
        knots = [float(kv) for kv in e.knots]
        no = NurbsCurveObject()
        if len(knots) == len(ctrl) + k + 1:
            cps = [ControlPoint(float(p[0]), float(p[1]),
                                float(p[2]) if len(p) > 2 else 0.0,
                                weights[i] if weights else 1.0)
                   for i, p in enumerate(ctrl)]
            no._segments = [NurbsSegment(cps, k, knots)]
        else:
            # 节点缺失/不匹配：退化为夹持均匀节点
            pts = [_pt3(p) for p in ctrl]
            no = NurbsCurveObject.create(k, pts, weights, closed)
        no.setClosed(closed)
        return [no]
    return []


_CONVERTERS = {
    "POINT": _convertPoint,
    "LINE": _convertLine,
    "CIRCLE": _convertCircle,
    "ARC": _convertArc,
    "ELLIPSE": _convertEllipse,
    "LWPOLYLINE": _convertLwpolyline,
    "POLYLINE": _convertLegacyPolyline,
    "SPLINE": _convertSpline,
}


def _expandEntities(msp, stats: dict) -> list:
    """枚举模型空间实体；INSERT 块参照展开为虚拟实体（块变换已应用）。"""
    out = []
    for e in msp:
        if e.dxftype() == "INSERT":
            try:
                out.extend(e.virtual_entities())
            except Exception:
                stats["skipped"] += 1
                stats["skippedTypes"]["INSERT"] = \
                    stats["skippedTypes"].get("INSERT", 0) + 1
        else:
            out.append(e)
    return out


def _ensureSublayer(doc, dxfdoc, name: str) -> int:
    """在“曲线”固定层下复用/新建同名子层，并按 DXF 图层 ACI 色设色。"""
    for l in doc.layers():
        if l.name == name and l.parentId == LAYER_CURVES_ID:
            return l.id
    lid = doc.addLayer(name, parentId=LAYER_CURVES_ID)
    try:
        layer = dxfdoc.layers.get(name)
        aci = layer.color
        if 1 <= aci <= 255:
            rgb = aci2rgb(aci)
            doc.setLayerColor(lid, (rgb.r, rgb.g, rgb.b))
    except Exception:
        pass
    return lid


def _entityColor(e):
    """实体显式颜色（ACI 1..255）→ RGB 元组；BYLAYER(256)/BYBLOCK(0) → None。"""
    try:
        aci = int(e.dxf.get("color", 256))
    except Exception:
        return None
    if 1 <= aci <= 255:
        rgb = aci2rgb(aci)
        return (rgb.r, rgb.g, rgb.b)
    return None


def importDXF(doc, path: str) -> dict:
    """读取 DXF 文件并把其中曲线实体合并进 doc（当前文档）。

    返回统计：{"imported": 导入对象数, "skipped": 跳过实体数,
              "skippedTypes": {实体类型: 次数}}。
    整批导入为一条撤销记录；不支持的实体跳过计数，不致整体失败。
    """
    dxfdoc = ezdxf.readfile(path)
    stats = {"imported": 0, "skipped": 0, "skippedTypes": {}}
    entities = _expandEntities(dxfdoc.modelspace(), stats)

    # 按 DXF 图层分组处理（切换活动层使对象自动落位并继承图层颜色）
    groups = {}
    order = []
    for e in entities:
        name = e.dxf.get("layer", "0") or "0"
        if name not in groups:
            groups[name] = []
            order.append(name)
        groups[name].append(e)

    doc.beginEdit()
    prev_active = doc.activeLayerId()
    try:
        for name in order:
            if name == "0":
                lid = -1
            else:
                try:
                    lid = _ensureSublayer(doc, dxfdoc, name)
                except Exception:
                    lid = -1
            if lid >= 0:
                doc.setLayerActive(lid)
            for e in groups[name]:
                conv = _CONVERTERS.get(e.dxftype())
                objs = []
                if conv is not None:
                    try:
                        objs = conv(e)
                    except Exception:
                        objs = []
                if not objs:
                    stats["skipped"] += 1
                    t = e.dxftype()
                    stats["skippedTypes"][t] = \
                        stats["skippedTypes"].get(t, 0) + 1
                    continue
                color = _entityColor(e)
                for obj in objs:
                    doc.addObject(obj)
                    if color is not None:
                        obj.setColor(color)
                    stats["imported"] += 1
    finally:
        doc.setLayerActive(prev_active)
        doc.endEdit()
    return stats

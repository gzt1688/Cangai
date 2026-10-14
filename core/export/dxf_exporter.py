"""DXF 导出：把文档中的曲线类对象写为 DXF R2010（单位 mm）。

对象 → DXF 实体映射：
  PointObject      → POINT
  LineObject(2点)  → LINE
  LineObject(多点) → LWPOLYLINE（z 恒定，带 elevation）/ 3D POLYLINE（z 变化）
  CircleObject     → CIRCLE
  ArcObject        → ARC（角度制，恒逆时针；顺时针弧交换起止角表示）
  EllipseObject    → ELLIPSE（长轴端点矢量 + ratio）
  CurveObject      → SPLINE（FIT 模式写拟合点；CONTROL 模式写控制点）
  NurbsCurveObject → SPLINE（控制点 + 权重 + 节点）
  PolyCurveObject  → 逐段：平滑段(inputPoints) → SPLINE；直线段合并为 LWPOLYLINE/3D POLYLINE

图层：曲线族子层导出为同名 DXF 图层（颜色取最近 ACI）；固定“曲线”层 → DXF 0 层。
仅导出曲线类对象；浮雕模型/图片/标注不参与（模型走 STL）。
单个对象导出失败不阻断整体导出。
"""
from __future__ import annotations
import math
from typing import List, Optional, Tuple

import ezdxf
from ezdxf import units
from ezdxf.math import BSpline
from ezdxf.colors import DXF_DEFAULT_COLORS

from ..vector.geometry_types import ObjectType, Point3D


# 支持导出的曲线类对象
_EXPORTABLE_TYPES = {
    ObjectType.POINT, ObjectType.LINE, ObjectType.CIRCLE, ObjectType.ARC,
    ObjectType.ELLIPSE, ObjectType.CURVE, ObjectType.NURBS_CURVE,
    ObjectType.POLYCURVE,
}

_Z_EPS = 1e-9


def _nearestAci(rgb) -> int:
    """RGB 三元组 → DXF 调色板中最近的 ACI 颜色号（1..255）。"""
    r, g, b = int(rgb[0]), int(rgb[1]), int(rgb[2])
    best, best_d = 7, None
    for aci in range(1, 256):
        v = DXF_DEFAULT_COLORS[aci]
        cr = (v >> 16) & 255
        cg = (v >> 8) & 255
        cb = v & 255
        d = (cr - r) ** 2 + (cg - g) ** 2 + (cb - b) ** 2
        if best_d is None or d < best_d:
            best, best_d = aci, d
    return best


def _sanitizeLayerName(name: str) -> str:
    """DXF 图层名非法字符替换（< > / \\ \" : ; ? * | = ` 及空白控制符）。"""
    out = []
    for ch in str(name):
        if ch in '<>/\\":;?*|=`' or ord(ch) < 32:
            out.append("_")
        else:
            out.append(ch)
    return "".join(out).strip() or "0"


def _sameZ(points: List[Tuple[float, float, float]]) -> Optional[float]:
    """点列 z 是否全部相同；是返回该 z，否返回 None。"""
    if not points:
        return 0.0
    z0 = points[0][2]
    for p in points[1:]:
        if abs(p[2] - z0) > _Z_EPS:
            return None
    return z0


def _dist3(a, b) -> float:
    return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2)


class _DxfWriter:
    """单个文档导出过程的状态持有者（DXF 文档/模型空间/图层缓存）。"""

    def __init__(self) -> None:
        self.doc = ezdxf.new("R2010", setup=True)
        self.doc.units = units.MM
        self.msp = self.doc.modelspace()
        self._layerCache = {}

    def layerAttribs(self, doc, obj) -> dict:
        """对象 → dxfattribs：曲线族子层导出同名图层，固定层/其他 → 0 层。"""
        from ..document import LAYER_CURVES_ID, isFixedLayerId
        lid = obj.layerId()
        if lid == LAYER_CURVES_ID or isFixedLayerId(lid):
            return {"layer": "0"}
        layer = doc.layer(lid)
        if layer is None:
            return {"layer": "0"}
        name = _sanitizeLayerName(layer.name)
        if name == "0":
            return {"layer": "0"}
        if name not in self._layerCache:
            try:
                self.doc.layers.get(name)
            except Exception:
                # 图层不存在（ezdxf get 缺失时抛异常）→ 新建
                self.doc.layers.add(name, color=_nearestAci(layer.color))
            self._layerCache[name] = True
        return {"layer": name}

    # ---------------- 实体写出 ----------------

    def writePoint(self, obj, attr: dict) -> None:
        self.msp.add_point((obj.position().x, obj.position().y, obj.position().z),
                           dxfattribs=attr)

    def writeLine(self, obj, attr: dict) -> None:
        pts = [(p.x, p.y, p.z) for p in obj.points()]
        if len(pts) < 2:
            return
        closed = bool(obj.isClosed()) or _dist3(pts[0], pts[-1]) < 1e-6
        if len(pts) == 2 and not closed:
            self.msp.add_line(pts[0], pts[1], dxfattribs=attr)
            return
        self._writePolyline(pts, closed, attr)

    def _writePolyline(self, pts: List[Tuple[float, float, float]],
                       closed: bool, attr: dict) -> None:
        """多点折线：2 点开放 → LINE；z 恒定 → LWPOLYLINE(elevation)；
        z 变化 → 3D POLYLINE。"""
        if len(pts) == 2 and not closed:
            self.msp.add_line(pts[0], pts[1], dxfattribs=attr)
            return
        z = _sameZ(pts)
        if closed and len(pts) >= 2 and _dist3(pts[0], pts[-1]) < 1e-6:
            pts = pts[:-1]   # 闭合由标志表达，末点不重复
        if z is not None:
            lw = self.msp.add_lwpolyline(
                [(p[0], p[1]) for p in pts], close=closed,
                dxfattribs=dict(attr, elevation=z))
        else:
            poly = self.msp.add_polyline3d(pts, dxfattribs=attr)
            if closed:
                poly.close()

    def writeCircle(self, obj, attr: dict) -> None:
        c = obj.center()
        self.msp.add_circle((c.x, c.y, c.z), obj.radius(), dxfattribs=attr)

    def writeArc(self, obj, attr: dict) -> None:
        c = obj.center()
        r = obj.radius()
        sa, ea = obj.startAngle(), obj.endAngle()
        span = ea - sa
        if abs(span) < 1e-9:
            # 起止角重合视为整圆
            self.msp.add_circle((c.x, c.y, c.z), r, dxfattribs=attr)
            return
        if span > 0:
            start = math.degrees(sa) % 360.0
            end = start + math.degrees(span)
        else:
            # 顺时针弧：同一几何弧用“从终点逆时针扫回”表示（DXF ARC 恒 CCW）
            start = math.degrees(ea) % 360.0
            end = start + math.degrees(-span)
        self.msp.add_arc((c.x, c.y, c.z), r, start, end, dxfattribs=attr)

    def writeEllipse(self, obj, attr: dict) -> None:
        c = obj.center()
        a = abs(obj.majorAxis())
        b = abs(obj.minorAxis())
        rot = obj.rotation()
        if a < 1e-12:
            return
        if b > a:
            # DXF ELLIPSE 约定长轴矢量为长者：交换两轴，长轴方向转 90°
            a, b = b, a
            rot = rot + math.pi / 2
        ratio = max(0.0, min(1.0, b / a))
        majorVec = (a * math.cos(rot), a * math.sin(rot), 0.0)
        self.msp.add_ellipse((c.x, c.y, c.z), majorVec, ratio=ratio,
                             start_param=0.0, end_param=2.0 * math.pi,
                             dxfattribs=attr)

    def writeCurve(self, obj, attr: dict) -> None:
        from ..vector.geometry_types import CurveMode
        pts = [(p.x, p.y, p.z) for p in obj.inputPoints()]
        if len(pts) < 2:
            return
        degree = max(1, min(int(obj.curveDegree()), 3))
        closed = bool(obj.isClosed()) or (
            len(pts) >= 2 and _dist3(pts[0], pts[-1]) < 1e-6)
        if obj.curveMode() == CurveMode.CONTROL:
            # 控制多边形模式：B 样条不过控制点 → 写控制点 SPLINE
            ent = self.msp.add_spline(dxfattribs=attr)
            ent.apply_construction_tool(BSpline(pts, order=degree + 1))
            if closed:
                ent.closed = True
        else:
            # 拟合模式：Catmull-Rom 过点 → 写拟合点 SPLINE（过同一组点）
            ent = self.msp.add_spline(fit_points=pts, degree=degree,
                                      dxfattribs=attr)
            if closed:
                ent.closed = True

    def writeNurbs(self, obj, attr: dict) -> None:
        for seg in obj.segments():
            cps = [(cp.x, cp.y, cp.z) for cp in seg.controlPoints]
            if len(cps) < 2:
                continue
            weights = [cp.weight for cp in seg.controlPoints]
            degree = max(1, min(int(seg.degree), len(cps) - 1))
            knots = [float(k) for k in seg.knotVector]
            if len(knots) != len(cps) + degree + 1:
                knots = None   # 节点不合法时交给 BSpline 自动生成夹持均匀节点
            if weights and all(abs(w - 1.0) < 1e-9 for w in weights):
                weights = None
            ent = self.msp.add_spline(dxfattribs=attr)
            ent.apply_construction_tool(
                BSpline(cps, order=degree + 1, knots=knots, weights=weights))
            if obj.isClosed():
                ent.closed = True

    def writePolycurve(self, obj, attr: dict) -> None:
        """逐段：含 inputPoints(≥3) 的平滑段 → SPLINE；=2 → LINE；
        其余采样直线段连续合并为一条 LWPOLYLINE/3D POLYLINE（接点去重）。"""
        buf: List[Tuple[float, float, float]] = []

        def flush() -> None:
            if len(buf) >= 2:
                closed = _dist3(buf[0], buf[-1]) < 1e-6
                self._writePolyline(buf, closed, attr)
            buf.clear()

        for seg in obj.segments():
            inp = seg.get("inputPoints")
            if inp and len(inp) >= 3:
                flush()
                pts = [(float(p[0]), float(p[1]), float(p[2])) for p in inp]
                closed = _dist3(pts[0], pts[-1]) < 1e-6
                ent = self.msp.add_spline(
                    fit_points=pts, degree=3, dxfattribs=attr)
                if closed:
                    ent.closed = True
            elif inp and len(inp) == 2:
                flush()
                p1 = (float(inp[0][0]), float(inp[0][1]), float(inp[0][2]))
                p2 = (float(inp[1][0]), float(inp[1][1]), float(inp[1][2]))
                self.msp.add_line(p1, p2, dxfattribs=attr)
            else:
                segPts = [(float(p[0]), float(p[1]), float(p[2]))
                          for p in seg.get("points", [])]
                for p in segPts:
                    if not buf or _dist3(buf[-1], p) > 1e-9:
                        buf.append(p)
        flush()


_WRITERS = {
    ObjectType.POINT: "writePoint",
    ObjectType.LINE: "writeLine",
    ObjectType.CIRCLE: "writeCircle",
    ObjectType.ARC: "writeArc",
    ObjectType.ELLIPSE: "writeEllipse",
    ObjectType.CURVE: "writeCurve",
    ObjectType.NURBS_CURVE: "writeNurbs",
    ObjectType.POLYCURVE: "writePolycurve",
}


def exportDXF(doc, path: str) -> int:
    """把文档中全部曲线类对象导出为 DXF 文件（含 3D 空间曲线）；返回写出实体数。"""
    w = _DxfWriter()
    count = 0
    for obj in doc.allObjects():
        t = obj.type()
        if t not in _EXPORTABLE_TYPES:
            continue
        method = getattr(w, _WRITERS[t], None)
        if method is None:
            continue
        try:
            attr = w.layerAttribs(doc, obj)
            method(obj, attr)
            count += 1
        except Exception:
            # 单个对象失败不阻断整体导出
            continue
    w.doc.saveas(path)
    return count

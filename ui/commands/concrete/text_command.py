"""文字命令（移植自 Rhino TextCommand + TextEditorDialog）。

激活后创建 PolyCurveObject 实时预览：参数面板调参或鼠标移动即重算布局。
直线模式：鼠标定位插入点；曲线模式：点击拾取一条曲线对象沿其排列。
确认后对象保留，取消则移除。
"""
from __future__ import annotations

import math
from typing import List, Optional
from PySide6.QtGui import QPainterPath

from core.vector.geometry_types import Point3D, ObjectType
from core.vector.poly_curve_object import PolyCurveObject
from core.text.font import FontManager
from core.text.text_layout import layoutLinear, layoutOnCurve, flatten
from ..command_instance import CommandInstance
from .text_param_panel import TextParamPanel

_PICK_TOL = 8.0


class TextCommand(CommandInstance):
    def name(self) -> str:
        return "Text"

    def commandTitle(self) -> str:
        return "文字参数"

    def prompt(self) -> str:
        if self._panel is not None and self._panel.useCurve():
            if self._curvePts is None:
                return "点击选择一条曲线作为文字路径"
            return "调整参数后确认（右键/回车完成，Esc 取消）"
        if not self._placed:
            return "点击放置文字，然后调整参数（右键/回车完成，Esc 取消）"
        return "调整参数后确认（右键/回车完成，Esc 取消）"

    def start(self) -> None:
        super().start()
        self._insert = Point3D(0.0, 0.0, 0.0)
        self._curvePts: Optional[List] = None
        self._panel: Optional[TextParamPanel] = None
        self._placed: bool = False
        self._obj = PolyCurveObject()
        # 文字轮廓标记（数据识别用）：文字本身是曲线轮廓，按 PolyCurve 归入“曲线”层
        self._obj.isText = True
        self._doc.addObject(self._obj)

    def paramPanel(self):
        if self._panel is None:
            self._panel = TextParamPanel()
            self._panel.paramChanged.connect(self._rebuild)
            self._panel.confirmRequested.connect(self._onConfirm)
            self._panel.cancelRequested.connect(self.cancel)
        return self._panel

    # ---------- 内部 ----------

    def _rebuild(self) -> None:
        if self._panel is None or self._obj is None:
            return
        path = self._panel.fontPath()
        if not path:
            return
        font = FontManager.instance().get(path)
        if not font.isLoaded:
            return
        text = self._panel.text()
        if not text:
            self._obj._segments = []
            self._doc.notifyObjectChanged(self._obj.id())
            return
        ch = self._panel.charHeight()
        sp = self._panel.charSpacing()
        rot = self._panel.rotation()
        useCurve = self._panel.useCurve() and self._curvePts is not None
        if useCurve:
            paths = layoutOnCurve(
                text, font, self._curvePts, ch, sp,
                self._panel.offset(), self._panel.startPos(),
                self._panel.reverse(), self._panel.mirror(),
                rot, self._panel.perpendicular())
            ix, iy = 0.0, 0.0
        else:
            paths = layoutLinear(text, font, ch, sp,
                                 self._panel.vertical(), rot)
            ix, iy = self._insert.x, self._insert.y
        flat = flatten(paths)
        segs = []
        for pth in flat:
            if len(pth) < 2:
                continue
            segs.append({"points": [[p[0] + ix, p[1] + iy, 0.0] for p in pth]})
        self._obj._segments = segs
        self._doc.notifyObjectChanged(self._obj.id())
        if self._view is not None:
            scene = self._view.scene()
            if scene is not None:
                scene.invalidate()
            self._view.viewport().update()
        if self._previewItem is not None:
            self._setPreviewPath(QPainterPath())

    def _onConfirm(self) -> None:
        if self._obj is not None and self._obj._segments:
            self.finish()
        else:
            self.cancel()

    def cancel(self) -> None:
        if self._obj is not None and not self._obj._segments:
            self._doc.removeObject(self._obj.id())
        elif self._obj is not None:
            self._doc.removeObject(self._obj.id())
        super().cancel()

    # ---------- 交互 ----------

    def onMousePress(self, wp: Point3D, mods) -> None:
        if self._panel is not None and self._panel.useCurve():
            pts = self._pickCurve(wp)
            if pts is not None:
                self._curvePts = pts
                self._placed = True
                self._rebuild()
                self._hideCursorMarker()
            return
        self._insert = Point3D(wp.x, wp.y, wp.z)
        self._placed = True
        self._rebuild()
        self._hideCursorMarker()

    def onMouseMove(self, wp: Point3D, mods) -> None:
        if self._panel is not None:
            self._panel.setXYZ(wp.x, wp.y, wp.z)
        if self._placed:
            return
        if self._panel is not None and self._panel.useCurve():
            self._setCursorMarker(wp)
            return
        self._insert = Point3D(wp.x, wp.y, wp.z)
        self._rebuild()
        self._setCursorMarker(wp)

    def onRightClick(self, wp: Point3D) -> bool:
        if self._obj is not None and self._obj._segments:
            self.finish()
            return True
        return False

    def onEnter(self) -> bool:
        return self.onRightClick(Point3D())

    # ---------- 曲线拾取 ----------

    def _pickCurve(self, wp: Point3D) -> Optional[List]:
        """找点击位置最近的曲线对象，返回其折线点列 [(x,y),...]。"""
        best = None
        bestDist = float("inf")
        for obj in self._doc.visibleObjects():
            if obj.type() == ObjectType.POINT:
                continue
            pts = self._curvePointsFromObject(obj)
            if not pts:
                continue
            for i in range(len(pts) - 1):
                d = _distToSegment((wp.x, wp.y), pts[i], pts[i + 1])
                if d < bestDist:
                    bestDist = d
                    best = pts
        if best is not None and bestDist < _PICK_TOL:
            return best
        return None

    @staticmethod
    def _curvePointsFromObject(obj) -> List:
        t = obj.type()
        if t == ObjectType.LINE:
            return [(p.x, p.y) for p in obj.points()]
        if t == ObjectType.POLYCURVE:
            out = []
            for seg in obj.segments():
                sp = seg.get("points", [])
                for p in sp:
                    out.append((p[0], p[1]))
                out.append(None)
            return [p for p in out if p is not None]
        if t == ObjectType.CIRCLE:
            c, r = obj.center(), obj.radius()
            return [(c.x + r * math.cos(a), c.y + r * math.sin(a))
                    for a in (i * 2 * math.pi / 48 for i in range(49))]
        if t == ObjectType.ARC:
            c, r = obj.center(), obj.radius()
            sa, ea = obj.startAngle(), obj.endAngle()
            span = ea - sa
            if abs(span) < 1e-9:
                span = 2 * math.pi
            return [(c.x + r * math.cos(sa + span * i / 48),
                     c.y + r * math.sin(sa + span * i / 48))
                    for i in range(49)]
        if t in (ObjectType.CURVE, ObjectType.NURBS_CURVE):
            return [(p.x, p.y) for p in obj.getSampledPoints()]
        if t == ObjectType.ELLIPSE:
            c, a, b = obj.center(), obj.majorAxis(), obj.minorAxis()
            cr, sr = math.cos(obj.rotation()), math.sin(obj.rotation())
            out = []
            for i in range(49):
                th = i * 2 * math.pi / 48
                ex, ey = a * math.cos(th), b * math.sin(th)
                out.append((c.x + ex * cr - ey * sr, c.y + ex * sr + ey * cr))
            return out
        return []


def _distToSegment(p, a, b) -> float:
    ax, ay = a[0] - p[0], a[1] - p[1]
    bx, by = b[0] - p[0], b[1] - p[1]
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    if L2 < 1e-12:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = -(ax * dx + ay * dy) / L2
    t = max(0.0, min(1.0, t))
    cx, cy = a[0] + t * dx, a[1] + t * dy
    return math.hypot(p[0] - cx, p[1] - cy)


def registerAll(manager) -> None:
    manager.register("Text", TextCommand)
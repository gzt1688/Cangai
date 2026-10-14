"""文字命令（移植自 Rhino TextCommand + TextEditorDialog）。

激活后创建 PolyCurveObject 实时预览：参数面板调参或鼠标移动即重算布局。
直线模式：鼠标定位插入点；曲线模式：点击拾取一条曲线对象沿其排列。
确认后对象保留，取消则移除。
"""
from __future__ import annotations

import math
import uuid
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
        miss = self._missingChars()
        tip = f"；字体缺字形已用方框占位：{miss}" if miss else ""
        if self._panel is not None and self._panel.useCurve():
            if self._curvePts is None:
                return "点击选择一条曲线作为文字路径"
            return "调整参数后确认（右键/回车完成，Esc 取消）" + tip
        if not self._placed:
            return "点击放置文字，然后调整参数（右键/回车完成，Esc 取消）"
        return "调整参数后确认（右键/回车完成，Esc 取消）" + tip

    def _missingChars(self) -> str:
        """当前字体缺字形的非空白字符（去重，按出现顺序）。"""
        if self._panel is None:
            return ""
        path = self._panel.fontPath()
        if not path:
            return ""
        font = FontManager.instance().getCached(path)
        if font is None:
            return ""
        seen: List[str] = []
        for c in self._panel.text():
            if c.isspace() or c in seen:
                continue
            if font.getGlyph(c) is None:
                seen.append(c)
        return "".join(seen)

    def start(self) -> None:
        super().start()
        self._insert = Point3D(0.0, 0.0, 0.0)
        self._curvePts: Optional[List] = None
        self._curveW = 0.0   # 被拾取曲线所在平面的平面外分量（侧视恒 0）
        self._panel: Optional[TextParamPanel] = None
        self._placed: bool = False
        self._objs: List[PolyCurveObject] = []
        self._groupId: str = ""
        self._fontLoader = None

    def _preloadFont(self) -> None:
        """后台线程预加载当前字体，避免首次操作卡顿。"""
        if self._panel is None:
            return
        path = self._panel.fontPath()
        if not path:
            return
        if FontManager.instance().getCached(path) is not None:
            return
        from PySide6.QtCore import QThread, Signal, QObject

        class _Loader(QThread):
            done = Signal()

            def __init__(self, p):
                super().__init__()
                self._p = p

            def run(self):
                try:
                    FontManager.instance().get(self._p)
                except Exception:
                    pass
                self.done.emit()

        self._fontLoader = _Loader(path)
        self._fontLoader.done.connect(self._onFontLoaded)
        self._fontLoader.start()

    def _onFontLoaded(self) -> None:
        """字体加载完成后更新提示与预览。"""
        if self._manager is not None:
            self._manager.promptChanged.emit(self.prompt())
        if self._placed:
            self._rebuild()

    def previewObjectIds(self) -> set:
        """文字预览轮廓（实时入文档的 PolyCurveObject）须从捕捉候选完全排除：
        否则放置/调整期间光标会吸附到文字自身的端点（弹出“端点”吸附气泡），
        插入点也被自身字形拽住导致抖动。"""
        return {o.id() for o in getattr(self, "_objs", [])}

    def paramPanel(self):
        if self._panel is None:
            self._panel = TextParamPanel()
            self._panel.paramChanged.connect(self._rebuild)
            self._panel.confirmRequested.connect(self._onConfirm)
            self._panel.cancelRequested.connect(self.cancel)
            self._preloadFont()
        return self._panel

    # ---------- 内部 ----------

    def _rebuild(self) -> None:
        if self._panel is None:
            return
        path = self._panel.fontPath()
        if not path:
            return
        font = FontManager.instance().getCached(path)
        if font is None:
            return
        text = self._panel.text()
        if not text:
            self._clearObjs()
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
            anchor = self._mkUVW(self._viewAxes(), 0.0, 0.0, self._curveW)
        else:
            paths = layoutLinear(text, font, ch, sp,
                                 self._panel.vertical(), rot)
            ip = self._planePts([self._insert])[0]
            ix, iy = ip.x, ip.y
            anchor = self._insert
        flat = flatten(paths)
        segs = []
        for pth in flat:
            if len(pth) < 2:
                continue
            wpts = self._planeToWorld(
                [Point3D(p[0] + ix, p[1] + iy, 0.0) for p in pth], anchor)
            segs.append({"points": [[p.x, p.y, p.z] for p in wpts]})
        if not self._groupId:
            self._groupId = uuid.uuid4().hex
        while len(self._objs) > len(segs):
            old = self._objs.pop()
            self._doc.removeObject(old.id())
        while len(self._objs) < len(segs):
            obj = PolyCurveObject()
            obj.isText = True
            obj.setGroup(self._groupId)
            self._doc.addObject(obj)
            self._objs.append(obj)
        for obj, seg in zip(self._objs, segs):
            obj._segments = [seg]
            self._doc.notifyObjectChanged(obj.id())
        if self._view is not None:
            scene = self._view.scene()
            if scene is not None:
                scene.invalidate()
            self._view.viewport().update()
        if self._previewItem is not None:
            self._setPreviewPath(QPainterPath())

    def _clearObjs(self) -> None:
        for o in self._objs:
            self._doc.removeObject(o.id())
        self._objs.clear()

    def _onConfirm(self) -> None:
        if any(o._segments for o in self._objs):
            self.finish()
        else:
            self.cancel()

    def cancel(self) -> None:
        self._clearObjs()
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
        if any(o._segments for o in self._objs):
            self.finish()
            return True
        return False

    def onEnter(self) -> bool:
        return self.onRightClick(Point3D())

    # ---------- 曲线拾取 ----------

    def _pickCurve(self, wp: Point3D) -> Optional[List]:
        """找点击位置最近的曲线对象，返回其视图平面 (u,v,w) 折线点列。
        距离按视图平面度量（侧视拾取前视/右视平面内的曲线）。"""
        pp = self._planePts([wp])[0]
        best = None
        bestDist = float("inf")
        ownIds = {o.id() for o in self._objs}   # 排除文字自身预览轮廓
        for obj in self._doc.visibleObjects():
            if obj.id() in ownIds:
                continue
            if obj.type() == ObjectType.POINT:
                continue
            pts = self._curvePointsFromObject(obj)
            if not pts:
                continue
            for i in range(len(pts) - 1):
                d = _distToSegment((pp.x, pp.y), pts[i], pts[i + 1])
                if d < bestDist:
                    bestDist = d
                    best = pts
        if best is not None and bestDist < _PICK_TOL:
            # 记录曲线所在平面（平面外分量），供文字轮廓映射回世界坐标
            first = best[0]
            self._curveW = first[2] if len(first) > 2 else 0.0
            return best
        return None

    def _curvePointsFromObject(self, obj) -> List:
        """对象轮廓 → 当前视图平面 (u,v,w) 折线点列（顶视/2D 即世界 XY）。"""
        t = obj.type()
        if t == ObjectType.LINE:
            return [(p.x, p.y, p.z)
                    for p in self._planePts(list(obj.points()))]
        if t == ObjectType.POLYCURVE:
            out = []
            for seg in obj.segments():
                sp = seg.get("points", [])
                for p in sp:
                    q = self._planePts([Point3D(
                        p[0], p[1], p[2] if len(p) > 2 else 0.0)])[0]
                    out.append((q.x, q.y, q.z))
                out.append(None)
            return [p for p in out if p is not None]
        if t == ObjectType.CIRCLE:
            c, r = obj.center(), obj.radius()
            raw = [Point3D(c.x + r * math.cos(a), c.y + r * math.sin(a), c.z)
                   for a in (i * 2 * math.pi / 48 for i in range(49))]
            return [(p.x, p.y, p.z) for p in self._planePts(raw)]
        if t == ObjectType.ARC:
            c, r = obj.center(), obj.radius()
            sa, ea = obj.startAngle(), obj.endAngle()
            span = ea - sa
            if abs(span) < 1e-9:
                span = 2 * math.pi
            raw = [Point3D(c.x + r * math.cos(sa + span * i / 48),
                           c.y + r * math.sin(sa + span * i / 48), c.z)
                   for i in range(49)]
            return [(p.x, p.y, p.z) for p in self._planePts(raw)]
        if t in (ObjectType.CURVE, ObjectType.NURBS_CURVE):
            return [(p.x, p.y, p.z)
                    for p in self._planePts(list(obj.getSampledPoints()))]
        if t == ObjectType.ELLIPSE:
            c, a, b = obj.center(), obj.majorAxis(), obj.minorAxis()
            cr, sr = math.cos(obj.rotation()), math.sin(obj.rotation())
            raw = []
            for i in range(49):
                th = i * 2 * math.pi / 48
                ex, ey = a * math.cos(th), b * math.sin(th)
                raw.append(Point3D(c.x + ex * cr - ey * sr,
                                   c.y + ex * sr + ey * cr, c.z))
            return [(p.x, p.y, p.z) for p in self._planePts(raw)]
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
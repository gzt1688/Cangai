"""绘图命令实现：Point / Line / Circle / Arc / Ellipse / Curve。

每个命令可懒创建参数面板（paramPanel），命令激活时由主窗口挂到
画布右侧的 CommandParamHost；面板信号连回命令槽实时更新预览。
"""
from __future__ import annotations
import math
from typing import List
from PySide6.QtCore import Qt
from PySide6.QtGui import QPainterPath

from core.vector.geometry_types import Point3D
from core.vector.point_object import PointObject
from core.vector.line_object import LineObject
from core.vector.circle_object import CircleObject
from core.vector.arc_object import ArcObject, computeArcFromThreePoints
from core.vector.ellipse_object import EllipseObject
from core.vector.curve_object import CurveObject
from core.vector.poly_curve_object import PolyCurveObject
from core.vector.wave_geometry import buildWaveCirclePoints
from core.vector.fillet_processor import (nearestPointOnSegment,
                                          PICK_PIXEL_RADIUS)
from core.spline_utils import catmullRom, catmullRomSegment
from core.document import LAYER_CURVES_ID
from ..command_instance import CommandInstance
from ..param_panels import (PointParamPanel, LineParamPanel, CircleParamPanel,
                            ArcParamPanel, EllipseParamPanel, CurveParamPanel)


def _linePath(points: List[Point3D]) -> QPainterPath:
    path = QPainterPath()
    if not points:
        return path
    path.moveTo(points[0].x, points[0].y)
    for p in points[1:]:
        path.lineTo(p.x, p.y)
    return path


def _arcPathThrough(p1: Point3D, pMid: Point3D, p3: Point3D) -> QPainterPath:
    """过三点的弧预览路径：扫掠方向选择经过 pMid 的一侧。
    退化（共线/点重合）时返回空路径，不画误导性的直线。"""
    path = QPainterPath()
    if math.hypot(pMid.x - p1.x, pMid.y - p1.y) < 1e-9 or \
            math.hypot(pMid.x - p3.x, pMid.y - p3.y) < 1e-9:
        return path
    res = computeArcFromThreePoints(p1, pMid, p3)
    if res is None:
        return path
    center, radius, sa, ea = res
    span = ea - sa
    while span < 0:
        span += 2 * math.pi
    ma = math.atan2(pMid.y - center.y, pMid.x - center.x)
    delta = ma - sa
    while delta < 0:
        delta += 2 * math.pi
    if delta > span:
        span -= 2 * math.pi
    # 采样折线：直接用数学约定（atan2/cos/sin）采样，保证弧精确经过三点。
    # 不能用 QPainterPath.arcTo：其角度约定与数学约定存在 Y 轴方向差异，
    # 会把弧起点/弧上点偏移，且从当前点补一段直线。
    n = 64
    path.moveTo(center.x + radius * math.cos(sa),
                center.y + radius * math.sin(sa))
    for i in range(1, n + 1):
        a = sa + span * i / n
        path.lineTo(center.x + radius * math.cos(a),
                    center.y + radius * math.sin(a))
    return path


class PointCommand(CommandInstance):
    def name(self) -> str:
        return "Point"

    def commandTitle(self) -> str:
        return "点参数"

    def prompt(self) -> str:
        return "点击画布连续布点（或面板输入坐标确认），右键完成"

    def start(self) -> None:
        super().start()
        self._panel = None

    def paramPanel(self):
        if self._panel is None:
            self._panel = PointParamPanel()
            self._panel.confirmed.connect(self._onPanelConfirmed)
        return self._panel

    def _onPanelConfirmed(self) -> None:
        obj = PointObject(self._panel.x(), self._panel.y(), self._panel.z())
        self._doc.addObject(obj)
        # 连续布点模式：不结束命令，继续等待下一个点（右键完成）

    def onMousePress(self, wp: Point3D, mods) -> None:
        obj = PointObject(wp.x, wp.y, wp.z)
        self._doc.addObject(obj)

    def onRightClick(self, wp: Point3D) -> bool:
        self.finish()
        return True

    def onMouseMove(self, wp: Point3D, mods) -> None:
        if self._panel is not None:
            self._panel.setXYZ(wp.x, wp.y, wp.z)
        self._setPreviewPath(QPainterPath())
        self._setCursorMarker(wp)


class LineCommand(CommandInstance):
    def name(self) -> str:
        return "Line"

    def commandTitle(self) -> str:
        return "直线参数"

    def lastInputPoint(self):
        pts = self._obj.points() if self._obj else []
        return pts[-1] if pts else None

    def previewObjectIds(self) -> set:
        # 直线绘制时不完全排除自身：保留端点/中点/最近点/垂足等吸附，
        # 使光标可吸附回起点以闭合，也可吸附已画线段上的各点
        return set()

    def prompt(self) -> str:
        if getattr(self, "_bisectorMode", False):
            if self._bisMsg:
                return self._bisMsg
            if self._bisSeg1 is None:
                return "角平分线：选择第一条直线"
            if self._bisVertex is None:
                return "角平分线：选择第二条直线"
            return "角平分线：点击确定终点（右键/回车完成，Esc 取消）"
        n = len(self._obj.points()) if self._obj else 0
        if n == 0:
            return "指定直线起点"
        return "指定下一点（右键/回车完成，C 闭合，Esc 取消）"

    def start(self) -> None:
        super().start()
        self._obj = LineObject()
        self._panel = None
        self._preview = None            # 当前光标预览点（按参数画点/预览刷新用）
        self._lastCursor = None         # 最近一次鼠标世界坐标（加点后刷新尾段用）
        # 角平分线模式状态（原命令无此功能，本命令新增）
        self._bisectorMode = False
        self._bisSeg1 = None            # 第一条选中线段 (p, q)
        self._bisVertex = None          # 两直线延长交点（平分线起点）
        self._bisDirs = None            # 两条候选平分方向 (b1, b2)
        self._bisMsg = ""
        self._doc.addObject(self._obj)

    def paramPanel(self):
        if self._panel is None:
            self._panel = LineParamPanel()
            self._panel.addPointRequested.connect(self._onPanelAddPoint)
            self._panel.undoClicked.connect(self._onPanelUndo)
            self._panel.closeToggled.connect(self._onPanelClose)
            self._panel.confirmRequested.connect(self._onPanelConfirm)
            self._panel.drawParamRequested.connect(self._onPanelDrawParam)
            self._panel.bisectorToggled.connect(self._onPanelBisector)
            self._updateCloseState()
        return self._panel

    # ---------------- 面板槽 ----------------
    def _onPanelAddPoint(self) -> None:
        self._addPointAt(Point3D(self._panel.x(), self._panel.y(),
                                 self._panel.z()))

    def _onPanelUndo(self) -> None:
        if self._obj is None:
            return
        self._obj.removeLastPoint()
        self._doc.notifyObjectChanged(self._obj.id())
        self._updateCloseState()
        if not self._obj.points():
            # 原版：撤回到空 → 取消命令（经管理器同步 UI）
            self.cancel()
            from ..command_manager import CommandManager
            CommandManager.instance()._checkDone()

    def _onPanelClose(self, closed: bool) -> None:
        if closed:
            self._closePolygon()
        elif self._obj is not None:
            self._obj.setClosed(False)
            self._doc.notifyObjectChanged(self._obj.id())

    def _onPanelConfirm(self) -> None:
        if self._obj is not None and len(self._obj.points()) >= 2:
            self.finish()

    def _onPanelDrawParam(self) -> None:
        """按参数画点：一键沿（上段方向 ± 指定角度）画一段指定长度。
        角度为 0 时沿当前光标方向；仅一次性生效，不改变后续绘制。"""
        if self._obj is None or self._panel is None:
            return
        pts = self._obj.points()
        length = self._panel.lengthValue()
        if not pts or length <= 0:
            return
        angleDeg = self._panel.angleValue()
        ccw = self._panel.angleCCW()
        last = pts[-1]
        if angleDeg > 0:
            if len(pts) >= 2:
                prev = pts[-2]
                base = math.atan2(last.y - prev.y, last.x - prev.x)
            else:
                base = 0.0   # 仅有起点：以 +X 轴为基准取绝对角度
            delta = math.radians(angleDeg) if ccw else -math.radians(angleDeg)
            dirA = base + delta
        else:
            if self._preview is None:
                return
            dx = self._preview.x - last.x
            dy = self._preview.y - last.y
            if math.hypot(dx, dy) < 1e-10:
                return
            dirA = math.atan2(dy, dx)
        self._addPointAt(Point3D(last.x + length * math.cos(dirA),
                                 last.y + length * math.sin(dirA), last.z))

    # ---------------- 内部 ----------------
    def _updateCloseState(self) -> None:
        """闭合至少需 3 点（原版 updateCloseButtonState）。"""
        if self._panel is None or self._obj is None:
            return
        ok = len(self._obj.points()) >= 3
        self._panel.setCloseEnabled(ok, "" if ok else "至少需要 3 个点才能闭合")
        self._panel.setClosed(self._obj.isClosed())

    def _addPointAt(self, p: Point3D) -> None:
        if self._obj is None:
            return
        self._obj.addPoint(Point3D(p.x, p.y, p.z))
        self._doc.notifyObjectChanged(self._obj.id())
        self._updateCloseState()
        # 点击/输入后鼠标未移动，主动刷新尾段预览（原版 addPoint 同逻辑）
        if self._lastCursor is not None:
            self._preview = self._lastCursor
            self._updatePreviewPath(self._lastCursor)
        if self._panel is not None:
            self._panel.focusOnLength()

    def _closePolygon(self) -> None:
        if self._obj is None or len(self._obj.points()) < 3:
            return
        self._obj.setClosed(True)
        self._doc.notifyObjectChanged(self._obj.id())
        self.finish()

    def _cleanupIfInvalid(self) -> None:
        """原版 commit：点数不足 2 时删除对象，避免幽灵空线。"""
        if self._obj is not None and self._obj.isEmpty():
            self._doc.removeObject(self._obj.id())
            self._obj = None

    def finish(self) -> None:
        self._cleanupIfInvalid()
        super().finish()

    def cancel(self) -> None:
        self._cleanupIfInvalid()
        super().cancel()

    # ---------------- 交互 ----------------
    def onMousePress(self, wp: Point3D, mods) -> None:
        if self._bisectorMode:
            self._onBisectorPress(wp)
            return
        pts = self._obj.points() if self._obj else []
        if pts:
            wp = self._orthoPoint(wp, pts[-1])
        self._lastCursor = wp
        self._addPointAt(wp)

    def onMouseMove(self, wp: Point3D, mods) -> None:
        if self._bisectorMode:
            self._onBisectorMove(wp)
            return
        pts = self._obj.points() if self._obj else []
        if pts:
            wp = self._orthoPoint(wp, pts[-1])
        cursor = wp
        self._preview = cursor
        self._lastCursor = wp
        pts = self._obj.points() if self._obj else []
        if self._panel is not None:
            self._panel.setXYZ(cursor.x, cursor.y, cursor.z)
            if pts:
                last = pts[-1]
                segLen = math.sqrt((cursor.x - last.x) ** 2 +
                                   (cursor.y - last.y) ** 2 +
                                   (cursor.z - last.z) ** 2)
                self._panel.setLength(segLen)
                if len(pts) >= 2:
                    prev = pts[-2]
                    v1x, v1y = last.x - prev.x, last.y - prev.y
                    v2x, v2y = cursor.x - last.x, cursor.y - last.y
                    len1 = math.hypot(v1x, v1y)
                    len2 = math.hypot(v2x, v2y)
                    if len1 > 1e-10 and len2 > 1e-10:
                        cosA = max(-1.0, min(1.0, (v1x * v2x + v1y * v2y) /
                                             (len1 * len2)))
                        # 偏转角 = 180° - 夹角（原版一致）
                        self._panel.setAngle(180.0 - math.degrees(math.acos(cosA)))
                else:
                    self._panel.setAngle(0.0)
        self._updatePreviewPath(cursor)

    def _updatePreviewPath(self, cursor: Point3D) -> None:
        pts = self._obj.points() if self._obj else []
        if not pts:
            self._setPreviewPath(QPainterPath())
            self._setCursorMarker(cursor)
            return
        path = _linePath([pts[-1], cursor])
        if self._panel is not None and self._panel.isAnglePreviewChecked() \
                and len(pts) >= 2:
            self._appendAngleArc(path, pts[-2], pts[-1], cursor)
        # 预览尾段与已确认段同色（对象色），避免点击落点时青→橙变色闪烁/双线感
        self._setPreviewPath(path, solid=True,
                             color=self._obj.color() if self._obj else None)
        # 光标处叠加与点对象同样式的光标点（白底实心圆）
        self._setCursorMarker(cursor)

    @staticmethod
    def _appendAngleArc(path: QPainterPath, prev: Point3D, last: Point3D,
                        cursor: Point3D) -> None:
        """前段与当前段间的角度弧（原版 updateAngleArcPreview：24 段，
        半径 = min(两段长, 30) × 0.3，取最短角跨度）。"""
        v1u, v1v = prev.x - last.x, prev.y - last.y
        v2u, v2v = cursor.x - last.x, cursor.y - last.y
        len1 = math.hypot(v1u, v1v)
        len2 = math.hypot(v2u, v2v)
        if len1 < 1e-10 or len2 < 1e-10:
            return
        a1 = math.atan2(v1v, v1u)
        delta = math.atan2(v2v, v2u) - a1
        while delta > math.pi:
            delta -= 2 * math.pi
        while delta < -math.pi:
            delta += 2 * math.pi
        r = min(len1, len2, 30.0) * 0.3
        segs = 24
        for i in range(segs + 1):
            a = a1 + delta * i / segs
            x = last.x + r * math.cos(a)
            y = last.y + r * math.sin(a)
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)

    def onRightClick(self, wp: Point3D) -> bool:
        if self._bisectorMode:
            self.finish()
            return True
        if self._obj is not None and len(self._obj.points()) >= 2:
            self.finish()
            return True
        return False

    def onEnter(self) -> bool:
        return self.onRightClick(Point3D())

    def onKeyPress(self, key, mods) -> bool:
        # 原版 InputProcessor：C 键闭合（无修饰键）
        if key == Qt.Key_C and mods == Qt.NoModifier:
            self._closePolygon()
            return True
        return False

    # ---------------- 角平分线模式 ----------------

    def _onPanelBisector(self, on: bool) -> None:
        self._bisectorMode = on
        self._bisReset()
        if self._manager is not None:
            self._manager.promptChanged.emit(self.prompt())

    def _bisReset(self) -> None:
        self._bisSeg1 = None
        self._bisVertex = None
        self._bisDirs = None
        self._bisMsg = ""
        self._setPreviewPath(QPainterPath())

    def _pickSegment(self, wp: Point3D):
        """拾取鼠标附近最近的 LineObject 线段（同 Chamfer 拾取）。"""
        if self._doc is None:
            return None
        scale = abs(self._view.transform().m11()) if self._view is not None else 1.0
        worldR = PICK_PIXEL_RADIUS / max(scale, 1e-6)
        best, bestD = None, worldR
        for obj in self._doc.visibleObjects():
            if obj.type().name != "LINE":
                continue
            pts = obj.points()
            for i in range(len(pts) - 1):
                near, _ = nearestPointOnSegment(pts[i], pts[i + 1], wp)
                d = near.distanceTo(wp)
                if d <= bestD:
                    bestD, best = d, (pts[i], pts[i + 1])
        return best

    @staticmethod
    def _computeBisector(seg1, seg2):
        """两线段所在无限直线的交点 + 四条角平分方向；平行返回 (None, None)。

        两条直线相交产生四条角平分线（内/外平分线各有正反两个方向）。
        平分方向不用线段的存储方向（受用户画线方向影响），而用顶点指向
        各线段中点的射线求和/差，保证内外侧划分与绘制方向无关。
        返回 (顶点, (内侧, 内侧反向, 外侧, 外侧反向))。
        """
        (a1, a2), (b1, b2) = seg1, seg2
        d1x, d1y = a2.x - a1.x, a2.y - a1.y
        d2x, d2y = b2.x - b1.x, b2.y - b1.y
        L1, L2 = math.hypot(d1x, d1y), math.hypot(d2x, d2y)
        if L1 < 1e-12 or L2 < 1e-12:
            return None, None
        d1x, d1y = d1x / L1, d1y / L1
        d2x, d2y = d2x / L2, d2y / L2
        den = d1x * d2y - d1y * d2x
        if abs(den) < 1e-9:
            return None, None          # 平行：无交点无平分线
        sx, sy = b1.x - a1.x, b1.y - a1.y
        t = (sx * d2y - sy * d2x) / den
        v = Point3D(a1.x + d1x * t, a1.y + d1y * t, a1.z)

        def _rayFromVertex(px: float, py: float, fx: float, fy: float):
            """顶点指向点 (px,py) 的单位射线；退化时回退直线方向 (fx,fy)。"""
            rx, ry = px - v.x, py - v.y
            L = math.hypot(rx, ry)
            if L < 1e-9:
                return fx, fy
            return rx / L, ry / L

        # 射线取顶点 → 线段中点（中点退化在顶点上时回退直线方向）
        r1 = _rayFromVertex((a1.x + a2.x) * 0.5, (a1.y + a2.y) * 0.5,
                            d1x, d1y)
        r2 = _rayFromVertex((b1.x + b2.x) * 0.5, (b1.y + b2.y) * 0.5,
                            d2x, d2y)

        def _norm(dx: float, dy: float):
            L = math.hypot(dx, dy)
            return (dx / L, dy / L) if L > 1e-9 else None

        bIn = _norm(r1[0] + r2[0], r1[1] + r2[1])     # 内侧（角内）平分线
        bOut = _norm(r1[0] - r2[0], r1[1] - r2[1])    # 外侧（垂直）平分线
        if bIn is None and bOut is None:
            return None, None
        if bIn is None:            # 两射线反向共线（180°）：内外对调兼容
            bIn, bOut = bOut, (-bOut[0], -bOut[1])
        elif bOut is None:
            bOut = (-bIn[1], bIn[0])   # 两射线同向：补垂直方向
        # 四条平分线：内/外各含正反两个射线方向（同原版四象限选择）
        return v, (bIn, (-bIn[0], -bIn[1]), bOut, (-bOut[0], -bOut[1]))

    def _bisPickDir(self, wp: Point3D):
        """四条平分方向中取光标所在象限的那条（点积最大）。"""
        mx, my = wp.x - self._bisVertex.x, wp.y - self._bisVertex.y
        best, bestS = self._bisDirs[0], -1e30
        for d in self._bisDirs:
            s = mx * d[0] + my * d[1]
            if s > bestS:
                bestS, best = s, d
        return best

    def _onBisectorPress(self, wp: Point3D) -> None:
        self._bisMsg = ""
        if self._bisVertex is not None:
            # 已选定两直线：点击定终点（沿平分方向投影，保证平分线严格在角平分线上）
            b = self._bisPickDir(wp)
            t = ((wp.x - self._bisVertex.x) * b[0] +
                 (wp.y - self._bisVertex.y) * b[1])
            if t < 1e-6:
                self._bisMsg = "角平分线：终点需在角点前方，请重新点击"
                return
            end = Point3D(self._bisVertex.x + b[0] * t,
                          self._bisVertex.y + b[1] * t, wp.z)
            bis = LineObject([Point3D(self._bisVertex.x, self._bisVertex.y,
                                      wp.z), end])
            self._doc.addObject(bis)
            self._doc.notifyObjectChanged(bis.id())
            self._bisReset()   # 连续绘制：回到选第一条直线
            return
        seg = self._pickSegment(wp)
        if seg is None:
            self._bisMsg = "角平分线：未拾取到直线，请点击直线附近"
            return
        if self._bisSeg1 is None:
            self._bisSeg1 = seg
            return
        v, dirs = self._computeBisector(self._bisSeg1, seg)
        if v is None:
            self._bisMsg = "角平分线：两直线平行，请重选"
            self._bisSeg1 = None
            return
        self._bisVertex, self._bisDirs = v, dirs

    def _onBisectorMove(self, wp: Point3D) -> None:
        self._lastCursor = wp
        if self._panel is not None:
            self._panel.setXYZ(wp.x, wp.y, wp.z)
        if self._bisVertex is None or self._bisDirs is None:
            self._setPreviewPath(QPainterPath())
            self._setCursorMarker(wp)
            return
        # 只预览光标所在象限的那条平分线（四条候选中按点积最大选中）
        mx, my = wp.x - self._bisVertex.x, wp.y - self._bisVertex.y
        b = self._bisPickDir(wp)
        t = mx * b[0] + my * b[1]
        path = QPainterPath()
        if t > 1e-9:
            path.moveTo(self._bisVertex.x, self._bisVertex.y)
            path.lineTo(self._bisVertex.x + b[0] * t,
                        self._bisVertex.y + b[1] * t)
        self._setPreviewPath(path, solid=True)
        self._setCursorMarker(wp)


class CircleCommand(CommandInstance):
    def name(self) -> str:
        return "Circle"

    def commandTitle(self) -> str:
        return "圆参数"

    def prompt(self) -> str:
        if getattr(self, "_waveMode", False):
            if not hasattr(self, "_center") or self._center is None:
                return "指定波浪圆圆心"
            if not getattr(self, "_waveLocked", False):
                return "拖动定内外半径，点击确认大小"
            return "面板调参实时生效，右键或确认按钮完成"
        if not hasattr(self, "_center") or self._center is None:
            return "指定圆心"
        kb = getattr(self, "_kbInput", "")
        if kb:
            return f"输入直径: {kb}（回车确认，退格修改）"
        return "指定半径点，或直接输入直径回车确认"

    def start(self) -> None:
        super().start()
        self._center = None
        self._panel = None
        self._lastCursor = None
        self._kbInput = ""   # 键盘直接输入直径的缓冲区
        self._waveMode = False
        self._waveArcCount = 8
        self._waveChamfer = 0.0
        self._waveType = 2            # 0=直线 1=样条 2=圆弧（默认）
        self._waveChamferRounded = True   # 倒角类型：圆角（默认）/倒直角
        self._waveLocked = False    # 第二点已确认大小 → 锁定，进入调参阶段
        self._waveInner = 0.0
        self._waveBulge = 0.0
        self._waveObj = None        # 锁定后创建的波浪折线对象
        self._obj = CircleObject(0, 0, 0, 0.0)
        self._doc.addObject(self._obj)

    def paramPanel(self):
        if self._panel is None:
            self._panel = CircleParamPanel()
            self._panel.diameterChanged.connect(self._onPanelDiameter)
            self._panel.confirmed.connect(self._onPanelConfirmed)
            self._panel.waveModeChanged.connect(self._onPanelWaveMode)
            self._panel.waveArcCountChanged.connect(self._onPanelWaveCount)
            self._panel.waveChamferChanged.connect(self._onPanelWaveChamfer)
            self._panel.waveTypeChanged.connect(self._onPanelWaveType)
            self._panel.waveChamferTypeChanged.connect(self._onPanelChamferType)
            self._panel.waveRadiusChanged.connect(self._onPanelWaveRadius)
        return self._panel

    def _onPanelWaveMode(self, wave: bool) -> None:
        self._waveMode = wave
        if not wave and self._panel is not None:
            self._panel.disableWaveRadii()

    def _onPanelWaveCount(self, n: int) -> None:
        self._waveArcCount = n
        self._refreshWaveObject()

    def _onPanelWaveChamfer(self, r: float) -> None:
        self._waveChamfer = max(r, 0.0)
        self._refreshWaveObject()

    def _onPanelWaveType(self, t: int) -> None:
        self._waveType = t
        self._refreshWaveObject()

    def _onPanelChamferType(self, t: int) -> None:
        self._waveChamferRounded = t == 1
        self._refreshWaveObject()

    def _onPanelWaveRadius(self) -> None:
        """锁定后内/外半径调节：读取面板值实时重建。"""
        if self._panel is None or not self._waveLocked:
            return
        inner = self._panel.innerRadius()
        bulge = self._panel.bulgeRadius()
        if inner < 1e-9 or bulge < 1e-9:
            return
        self._waveInner, self._waveBulge = inner, bulge
        self._refreshWaveObject()

    def _refreshWaveObject(self) -> None:
        """锁定大小后：面板参数变化实时重建波浪对象点列；
        未锁定时仅刷新跟随光标的预览。"""
        if not self._waveMode:
            return
        if self._waveLocked and self._waveObj is not None:
            pts = buildWaveCirclePoints(self._center, self._waveInner,
                                        self._waveBulge, self._waveArcCount,
                                        self._waveChamfer, self._waveType,
                                        self._waveChamferRounded)
            if len(pts) >= 3:
                lst = self._waveObj.points()
                lst.clear()
                lst.extend(pts)
                self._waveObj.setClosed(True)
                self._doc.notifyObjectChanged(self._waveObj.id())
        elif self._center is not None and self._lastCursor is not None:
            d = math.hypot(self._lastCursor.x - self._center.x,
                           self._lastCursor.y - self._center.y)
            self._showWavePreview(d)

    def _showWavePreview(self, d: float) -> None:
        """按拖动距离 d 生成波浪预览（内圆=0.6d，外凸=d）。"""
        if d < 1e-9:
            return
        pts = buildWaveCirclePoints(self._center, d * 0.6, d,
                                    self._waveArcCount, self._waveChamfer,
                                    self._waveType, self._waveChamferRounded)
        if len(pts) < 3:
            return
        path = _linePath(pts)
        path.closeSubpath()
        self._setPreviewPath(path, solid=True,
                             color=self._obj.color() if self._obj else None)

    def _lockWaveSize(self, d: float) -> None:
        """第二点确认大小：锁定内外半径并创建波浪对象，命令保持活动，
        后续面板调参实时生效，右键/确认按钮才完成。"""
        if d < 1e-9:
            return
        self._waveInner = d * 0.6
        self._waveBulge = d
        pts = buildWaveCirclePoints(self._center, self._waveInner,
                                    self._waveBulge, self._waveArcCount,
                                    self._waveChamfer, self._waveType,
                                    self._waveChamferRounded)
        if len(pts) < 3:
            return
        # 替换掉标准模式的占位圆对象，产出闭合波浪折线
        if self._obj is not None:
            self._doc.removeObject(self._obj.id())
            self._obj = None
        wave = LineObject(pts)
        wave.setClosed(True)
        self._doc.addObject(wave)
        self._waveObj = wave
        self._waveLocked = True
        if self._panel is not None:
            self._panel.setWaveRadii(self._waveInner, self._waveBulge)
        self._setPreviewPath(QPainterPath())   # 成型后不再画预览

    def _onPanelDiameter(self, d: float) -> None:
        if self._center is not None and d > 0:
            self._obj.setRadius(d * 0.5)
            self._doc.notifyObjectChanged(self._obj.id())

    def _onPanelConfirmed(self) -> None:
        if self._waveMode:
            if self._waveLocked:
                self.finish()
            return
        if self._center is not None and self._obj.radius() > 0:
            self.finish()

    def cancel(self) -> None:
        # 波浪圆未确认完成就取消：删掉已成型的波浪对象与占位圆，不留幽灵对象
        if self._waveObj is not None:
            self._doc.removeObject(self._waveObj.id())
            self._waveObj = None
        if self._obj is not None and self._obj.radius() < 1e-9:
            self._doc.removeObject(self._obj.id())
            self._obj = None
        super().cancel()

    def _syncPanel(self) -> None:
        if self._panel is not None:
            self._panel.setDiameter(self._obj.radius() * 2.0)

    # ---------- 键盘直接输入直径 ----------

    def _applyKbInput(self) -> None:
        """按缓冲区解析直径并实时更新半径/预览/面板。"""
        try:
            d = float(self._kbInput)
        except ValueError:
            d = 0.0
        if d > 0:
            self._obj.setRadius(d * 0.5)
        self._syncPanel()
        self._doc.notifyObjectChanged(self._obj.id())
        if self._center is not None:
            r = self._obj.radius()
            path = QPainterPath()
            path.addEllipse(self._center.x - r, self._center.y - r, r * 2, r * 2)
            self._setPreviewPath(path, solid=True,
                                 color=self._obj.color() if self._obj else None)
        if self._manager is not None:
            self._manager.promptChanged.emit(self.prompt())

    def onKeyPress(self, key, mods) -> bool:
        # 仅标准模式且圆心已定：数字/小数点/退格直接输入直径
        if self._waveMode or self._center is None:
            return False
        if Qt.Key_0 <= key <= Qt.Key_9:
            self._kbInput += chr(int(key))
            self._applyKbInput()
            return True
        if key in (Qt.Key_Period, Qt.Key_Comma):
            if "." not in self._kbInput:   # 已有点则忽略，避免非法数字
                self._kbInput += "."
                self._applyKbInput()
            return True
        if key == Qt.Key_Backspace:
            if self._kbInput:
                self._kbInput = self._kbInput[:-1]
                self._applyKbInput()
            return True
        return False

    def onEnter(self) -> bool:
        if self._waveMode:
            return self.onRightClick(Point3D())
        if self._center is not None and self._obj.radius() > 0:
            self.finish()
            return True
        return False

    def onMousePress(self, wp: Point3D, mods) -> None:
        if self._waveMode:
            if self._center is None:
                self._center = Point3D(wp.x, wp.y, wp.z)
                return
            if not self._waveLocked:
                self._lastCursor = wp
                self._lockWaveSize(math.hypot(wp.x - self._center.x,
                                              wp.y - self._center.y))
            return
        if self._center is None:
            self._center = Point3D(wp.x, wp.y, wp.z)
            self._obj.setCenter(self._center)
            self._doc.notifyObjectChanged(self._obj.id())
            return
        r = math.hypot(wp.x - self._center.x, wp.y - self._center.y)
        self._obj.setRadius(r)
        self._syncPanel()
        self._doc.notifyObjectChanged(self._obj.id())
        self.finish()

    def onMouseMove(self, wp: Point3D, mods) -> None:
        if self._panel is not None:
            self._panel.setXYZ(wp.x, wp.y, wp.z)
        if self._waveMode:
            self._lastCursor = wp
            if self._center is None:
                self._setPreviewPath(QPainterPath())
                self._setCursorMarker(wp)
            elif not self._waveLocked:
                d = math.hypot(wp.x - self._center.x, wp.y - self._center.y)
                self._showWavePreview(d)
            return
        if self._center is None:
            self._setPreviewPath(QPainterPath())
            self._setCursorMarker(wp)
            return
        r = math.hypot(wp.x - self._center.x, wp.y - self._center.y)
        self._obj.setRadius(r)
        self._syncPanel()
        self._doc.notifyObjectChanged(self._obj.id())
        path = QPainterPath()
        path.addEllipse(self._center.x - r, self._center.y - r, r * 2, r * 2)
        self._setPreviewPath(path, solid=True,
                             color=self._obj.color() if self._obj else None)

    def onRightClick(self, wp: Point3D) -> bool:
        if self._waveMode:
            if self._waveLocked:
                self.finish()
                return True
            return False
        if self._center is not None and self._obj.radius() > 0:
            self.finish()
            return True
        return False


class ArcCommand(CommandInstance):
    def name(self) -> str:
        return "Arc"

    def commandTitle(self) -> str:
        return "圆弧参数"

    def lastInputPoint(self):
        return self._pts[-1] if self._pts else None

    def prompt(self) -> str:
        n = len(self._pts)
        if n == 0:
            return "指定圆弧起点"
        if n == 1:
            return "指定圆弧终点" if self._mode == 0 else "指定弧上点"
        return "指定弧上点（完成）" if self._mode == 0 else "指定圆弧终点（完成）"

    def start(self) -> None:
        super().start()
        self._pts: List[Point3D] = []
        self._panel = None
        self._lastCursor = None
        self._mode = 0   # 0=起点-终点-弧上点（默认） 1=起点-弧上点-终点
        self._obj = ArcObject(0, 0, 0, 0.0, 0.0, 0.0)
        self._doc.addObject(self._obj)

    def paramPanel(self):
        if self._panel is None:
            self._panel = ArcParamPanel()
            self._panel.addPointRequested.connect(self._onPanelAddPoint)
            self._panel.confirmRequested.connect(self._onPanelConfirm)
            self._panel.modeChanged.connect(self._onPanelMode)
            self._panel.setMode(self._mode)
        return self._panel

    def _onPanelMode(self, mode: int) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        # 切换模式：已收集的点语义失效，重新开始（原版同步提示）
        self._pts.clear()
        if self._lastCursor is not None:
            self._updatePreview(self._lastCursor)

    def _onPanelAddPoint(self) -> None:
        self._addPoint(Point3D(self._panel.x(), self._panel.y(),
                               self._panel.z()))

    def _onPanelConfirm(self) -> None:
        if len(self._pts) >= 3:
            self.finish()

    def _addPoint(self, p: Point3D) -> None:
        self._pts.append(p)
        if len(self._pts) == 3:
            if self._mode == 0:
                # 收集顺序：起点、终点、弧上点 → setThreePoints(起,弧,终)
                ok = self._obj.setThreePoints(self._pts[0], self._pts[2],
                                              self._pts[1])
            else:
                ok = self._obj.setThreePoints(self._pts[0], self._pts[1],
                                              self._pts[2])
            if ok:
                self._doc.notifyObjectChanged(self._obj.id())
            self.finish()
            return
        self._updatePreview(p)

    def onMousePress(self, wp: Point3D, mods) -> None:
        self._addPoint(Point3D(wp.x, wp.y, wp.z))

    def onMouseMove(self, wp: Point3D, mods) -> None:
        if self._panel is not None:
            self._panel.setXYZ(wp.x, wp.y, wp.z)
        self._lastCursor = wp
        self._updatePreview(wp)

    def _updatePreview(self, wp: Point3D) -> None:
        n = len(self._pts)
        if n == 0:
            self._setPreviewPath(QPainterPath())
            self._setCursorMarker(wp)
            return
        if n == 1:
            # 橡皮带：起点 → 光标，光标处叠加光标点（原版同交互）
            path = QPainterPath()
            path.moveTo(self._pts[0].x, self._pts[0].y)
            path.lineTo(wp.x, wp.y)
            self._setPreviewPath(path, solid=True,
                                 color=self._obj.color() if self._obj else None)
            self._setCursorMarker(wp)
            return
        if self._mode == 0:
            # 起点、终点已定，光标为弧上点（退化时不画）
            path = _arcPathThrough(self._pts[0], wp, self._pts[1])
        else:
            path = _arcPathThrough(self._pts[0], self._pts[1], wp)
        self._setPreviewPath(path, solid=True,
                             color=self._obj.color() if self._obj else None)
        if not path.isEmpty():
            self._setCursorMarker(wp)
        else:
            self._hideCursorMarker()

    def onRightClick(self, wp: Point3D) -> bool:
        if len(self._pts) >= 3:
            self.finish()
            return True
        return False

    def cancel(self) -> None:
        # 未凑齐三点的占位弧（半径 0）不留在文档里，避免幽灵对象
        if self._obj is not None and self._obj.radius() < 1e-9:
            self._doc.removeObject(self._obj.id())
            self._obj = None
        super().cancel()


class EllipseCommand(CommandInstance):
    def name(self) -> str:
        return "Ellipse"

    def commandTitle(self) -> str:
        return "椭圆参数"

    def lastInputPoint(self):
        return self._pts[-1] if self._pts else None

    def prompt(self) -> str:
        n = len(self._pts)
        if n == 0:
            return "指定椭圆中心"
        if n == 1:
            return "指定长轴端点"
        return "指定短轴端点（完成）"

    def start(self) -> None:
        super().start()
        self._pts: List[Point3D] = []
        self._panel = None
        self._obj = EllipseObject(0, 0, 0, 0.0, 0.0)
        self._doc.addObject(self._obj)

    def paramPanel(self):
        if self._panel is None:
            self._panel = EllipseParamPanel()
            self._panel.axesChanged.connect(self._onPanelAxes)
            self._panel.confirmed.connect(self._onPanelConfirmed)
        return self._panel

    def _onPanelAxes(self, major: float, minor: float) -> None:
        if self._pts and major > 0 and minor > 0:
            self._obj.setCenter(self._pts[0])
            self._obj.setAxes(major, minor)
            self._doc.notifyObjectChanged(self._obj.id())

    def _onPanelConfirmed(self) -> None:
        if (self._pts and self._obj.majorAxis() > 0
                and self._obj.minorAxis() > 0):
            self.finish()

    def onMousePress(self, wp: Point3D, mods) -> None:
        self._pts.append(Point3D(wp.x, wp.y, wp.z))
        if len(self._pts) == 3:
            c = self._pts[0]
            major = math.hypot(self._pts[1].x - c.x, self._pts[1].y - c.y)
            minor = math.hypot(wp.x - c.x, wp.y - c.y)
            self._obj.setCenter(c)
            self._obj.setAxes(major, minor)
            if self._panel is not None:
                self._panel.setAxes(major, minor)
            self._doc.notifyObjectChanged(self._obj.id())
            self.finish()
            return
        if len(self._pts) == 1:
            self._obj.setCenter(self._pts[0])
            self._doc.notifyObjectChanged(self._obj.id())

    def onMouseMove(self, wp: Point3D, mods) -> None:
        if self._panel is not None:
            self._panel.setXYZ(wp.x, wp.y, wp.z)
        n = len(self._pts)
        if n == 0:
            self._setPreviewPath(QPainterPath())
            self._setCursorMarker(wp)
            return
        self._hideCursorMarker()
        path = QPainterPath()
        if n == 1:
            major = math.hypot(wp.x - self._pts[0].x, wp.y - self._pts[0].y)
            path.addEllipse(self._pts[0].x - major, self._pts[0].y - major,
                            major * 2, major * 2)
        elif n == 2:
            c = self._pts[0]
            major = math.hypot(self._pts[1].x - c.x, self._pts[1].y - c.y)
            minor = math.hypot(wp.x - c.x, wp.y - c.y)
            path.addEllipse(c.x - major, c.y - minor, major * 2, minor * 2)
        self._setPreviewPath(path, solid=True,
                             color=self._obj.color() if self._obj else None)

    def onRightClick(self, wp: Point3D) -> bool:
        if len(self._pts) >= 3:
            self.finish()
            return True
        return False


class CurveCommand(CommandInstance):
    """样条曲线（精雕式交互）：局部 Catmull-Rom 增量绘制，
    严格经过每个点击点。远处的点采样后冻结不再重算（整条线重算会抖），
    每次只重算最近几个点 + 光标；右键断开只是标记分段（同精雕多重曲线，
    各段独立不继承走势），结束时所有段合并为一个多段曲线对象入文档，
    其末点作为下一段起点续画，连续两次右键结束。"""

    _SEG_SAMPLES = 20   # 每段采样数（密度足够消除锯齿）

    def name(self) -> str:
        return "Curve"

    def commandTitle(self) -> str:
        return "曲线参数"

    def lastInputPoint(self):
        return self._pts[-1] if self._pts else None

    def extraSnapObjects(self) -> list:
        # 曲线绘制期间已画段未入文档，提供临时代理对象供吸附；
        # 不排除任何类型：起点/顶点/中点/最近点等均可吸附（含连回起点闭合）
        if self._snapProxy is not None and len(self._snapProxy.points()) >= 2:
            return [self._snapProxy]
        return []

    def prompt(self) -> str:
        if not self._pts:
            return "指定曲线起点"
        return "指定下一点（右键断开续画，再右键完成，Esc 取消）"

    def start(self) -> None:
        super().start()
        self._pts: List[Point3D] = []   # 当前段输入点（未固化，不入文档）
        self._closed = False
        self._panel = None
        self._frozenSamples: List[Point3D] = []   # 远段冻结采样（首点起，含冻结末点）
        self._frozenN = 0   # 冻结采样已覆盖的输入点数
        self._segments: List[List[Point3D]] = []   # 已断开段的采样（结束时合并入文档）
        self._segInputs: List[List[Point3D]] = []  # 已断开段的输入点（编辑控制点用）
        self._snapProxy = LineObject()   # 已画段吸附代理（不入文档，仅用于吸附）

    def _updateSnapProxy(self) -> None:
        """同步吸附代理点列 = 已断开段采样 + 当前段已确定采样。
        光标预览段未确定，不参与；已画段可吸附中点/最近点/垂足等，
        端点/顶点通过 partialSnapObjectIds 排除。"""
        if self._snapProxy is None:
            return
        pts: List[Point3D] = []
        for s in self._segments:
            pts.extend(s)
        if len(self._pts) >= 2:
            pts.extend(self._segmentSamples())
        elif self._pts:
            pts.extend(self._pts)
        lst = self._snapProxy.points()
        lst.clear()
        lst.extend(pts)

    def paramPanel(self):
        if self._panel is None:
            self._panel = CurveParamPanel()
            self._panel.addPointRequested.connect(self._onPanelAddPoint)
            self._panel.undoClicked.connect(self._onPanelUndo)
            self._panel.closeToggled.connect(self._onPanelClose)
            self._panel.confirmRequested.connect(self._onPanelConfirm)
        return self._panel

    def _previewColor(self):
        """预览色取曲线层颜色：曲线固定归“曲线”层，与固化后实体颜色一致。"""
        try:
            return self._doc.layer(LAYER_CURVES_ID).color
        except Exception:
            return None

    def _onPanelAddPoint(self) -> None:
        self._pts.append(Point3D(self._panel.x(), self._panel.y(),
                                 self._panel.z()))
        self._updateSnapProxy()

    def _onPanelUndo(self) -> None:
        if self._pts:
            self._pts.pop()
        self._updateSnapProxy()

    def _onPanelClose(self, closed: bool) -> None:
        self._closed = closed
        self._updateSnapProxy()

    def _onPanelConfirm(self) -> None:
        self._finalize()
        self.finish()

    def _ensureFrozen(self) -> None:
        """把已确定的远段采样冻结：段 i（pts[i]→pts[i+1]）形状由
        pts[i-1..i+2] 决定，故 i ≤ n-3 的段已定型，冻结覆盖前 n-1 个输入点。
        只增量追加冻结不重算远段；面板撤点时覆盖回缩并重建（低频）。"""
        pts, n = self._pts, len(self._pts)
        cover = max(n - 1, 0)            # 定型后应覆盖的输入点数
        if cover < 2:
            self._frozenSamples = pts[:cover]
            self._frozenN = cover
            return
        if cover < self._frozenN:        # 撤点回缩：旧冻结失效，重建（点少低频）
            self._frozenSamples = []
            self._frozenN = 0
        k = self._frozenN
        if k < 2:
            self._frozenSamples = pts[:k]   # 0/1 个原始点作起始锚
        for i in range(max(k - 1, 0), cover - 1):
            p0 = pts[i - 1] if i > 0 else pts[i]
            seg = catmullRomSegment(p0, pts[i], pts[i + 1], pts[i + 2],
                                    self._SEG_SAMPLES)
            base = self._frozenSamples[:-1] if self._frozenSamples else []
            self._frozenSamples = base + seg
        self._frozenN = cover

    def _incrementalPath(self, cursor: Point3D) -> QPainterPath:
        """冻结远段 + 只用最近几个点与光标高密度重算近段。"""
        self._ensureFrozen()
        pts, m = self._pts, self._frozenN
        if m >= 2:
            # 尾两段 pts[m-1]→pts[m]→cursor：补真实前导点 pts[m-2] 保证
            # 与冻结远段在接点处切线连续（前导点钳制会在接点出尖角）
            segA = catmullRomSegment(pts[m - 2], pts[m - 1], pts[m], cursor,
                                     self._SEG_SAMPLES)
            segB = catmullRomSegment(pts[m - 1], pts[m], cursor, cursor,
                                     self._SEG_SAMPLES)
            allPts = self._frozenSamples[:-1] + segA + segB[1:]
        else:
            ctrl = pts + [cursor]
            if len(ctrl) < 3:
                allPts = ctrl   # 点数不足：直线预览（续画段起点→光标同全新段）
            else:
                allPts = catmullRom(ctrl, self._SEG_SAMPLES)
        return _linePath(allPts)

    def _segmentSamples(self) -> List[Point3D]:
        """计算当前段的固化采样（与绘制预览完全一致，断开/结束时调用）。
        非闭合按冻结采样 + 尾部构建；闭合补起点成环整体计算。"""
        if self._closed:
            temp = self._pts + [self._pts[0]]
            return catmullRom(temp, self._SEG_SAMPLES) if len(temp) >= 3 else temp
        if len(self._pts) < 2:
            return list(self._pts)
        self._ensureFrozen()
        m = self._frozenN
        if m >= 2:                      # 冻结已覆盖到 pts[n-2]
            tail = catmullRomSegment(self._pts[m - 2], self._pts[m - 1],
                                     self._pts[m], self._pts[m],
                                     self._SEG_SAMPLES)   # 尾段端点钳制（无后续点）
            return self._frozenSamples[:-1] + tail
        return list(self._pts)          # 仅 2 点：两点间就是直线（用户明确要求）

    def _finalize(self) -> None:
        """结果入文档：仅一段保持曲线对象；一旦断开过，所有段合并为
        一个多段曲线对象（精雕多重曲线：一段一段合在一起，各段独立）。"""
        segs = list(self._segments)
        inputs = list(self._segInputs)
        if len(self._pts) >= 2:
            segs.append(self._segmentSamples())
            inputs.append(list(self._pts))
        if not segs:
            return
        if len(segs) == 1:
            obj = CurveObject()
            # 拟合点（用户点击点）作为编辑控制点，采样点仅作外观
            obj.buildFromSampled(segs[0], inputs[0] if inputs else self._pts)
            obj.setClosed(self._closed)
            self._doc.addObject(obj)
            return
        poly = PolyCurveObject()
        for s, inp in zip(segs, inputs):
            poly.addSegment({
                "type": "Curve",
                "points": [[p.x, p.y, p.z] for p in s],
                "inputPoints": [[p.x, p.y, p.z] for p in inp]})
        self._doc.addObject(poly)

    def _committedPreview(self) -> QPainterPath:
        """已断开段未入文档，绘制期间拼进预览保持可见（所见即所得）。"""
        path = QPainterPath()
        for s in self._segments:
            if len(s) < 2:
                continue
            sub = _linePath(s)
            path.addPath(sub)
        return path

    def onMousePress(self, wp: Point3D, mods) -> None:
        if self._pts:
            wp = self._orthoPoint(wp, self._pts[-1])
        self._pts.append(Point3D(wp.x, wp.y, wp.z))
        self._updateSnapProxy()

    def onMouseMove(self, wp: Point3D, mods) -> None:
        if self._pts:
            wp = self._orthoPoint(wp, self._pts[-1])
        if self._panel is not None:
            self._panel.setXYZ(wp.x, wp.y, wp.z)
        if not self._pts:
            self._setPreviewPath(self._committedPreview(),
                                 solid=True, color=self._previewColor())
            self._setCursorMarker(wp)
            return
        if self._closed and len(self._pts) >= 2:
            # 闭合预览：闭环无冻结，补回起点成环整体计算（低频操作可接受）
            temp = self._pts + [Point3D(wp.x, wp.y, wp.z), self._pts[0]]
            sampled = catmullRom(temp, self._SEG_SAMPLES) if len(temp) >= 3 else temp
            path = _linePath(sampled)
        else:
            path = self._incrementalPath(Point3D(wp.x, wp.y, wp.z))
        path.addPath(self._committedPreview())   # 拼上已断开段（结束前未入文档）
        self._setPreviewPath(path, solid=True, color=self._previewColor())
        # 光标处叠加与点对象同样式的光标点（白底实心圆）
        self._setCursorMarker(wp)

    def onRightClick(self, wp: Point3D) -> bool:
        if len(self._pts) >= 2:
            # 第一次右键：断开——当前段采样收入分段列表（同精雕多重曲线，
            # 结束时合并为一个多段曲线对象，不即时入文档）；末点作为下一段
            # 起点续画，新段等同全新段，不继承前段走势（用户明确要求：
            # 断开点与之后点的两点间是直线）
            self._segments.append(self._segmentSamples())
            self._segInputs.append(list(self._pts))
            b = self._pts[-1]
            self._pts = [Point3D(b.x, b.y, b.z)]
            self._closed = False
            self._frozenSamples = []   # 新段重新起算冻结
            self._frozenN = 0
            if self._panel is not None:
                self._panel.setClosed(False)
            self._updateSnapProxy()
            # 立即按右键位置重算预览：清掉盖在固化对象上的旧路径，
            # 断开后的新段形状马上可见，不用等鼠标移动才刷新
            self.onMouseMove(wp, None)
            return True
        # 当前段只有起点或为空 → 第二次右键：合并所有段入文档并结束（同精雕）
        self._finalize()
        self.finish()
        return True

    def onEnter(self) -> bool:
        return self.onRightClick(Point3D())


def registerAll(manager) -> None:
    manager.register("Point", PointCommand)
    manager.register("Line", LineCommand)
    manager.register("Circle", CircleCommand)
    manager.register("Arc", ArcCommand)
    manager.register("Ellipse", EllipseCommand)
    manager.register("Curve", CurveCommand)

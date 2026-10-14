"""形状命令实现：Rectangle / Polygon（移植自原 C++ 项目）。

产出统一为闭合 LineObject：圆角以圆弧采样点、斜角以切点表达，
保证序列化与后续倒角命令可直接处理。
"""
from __future__ import annotations
import math
from typing import List
from PySide6.QtGui import QPainterPath

from core.vector.geometry_types import Point3D
from core.vector.line_object import LineObject
from core.vector.fillet_processor import (computeFillet, sampleFilletArc,
                                          applyFilletToVertices)
from core.vector.wave_geometry import buildWaveRectanglePoints
from ..command_instance import CommandInstance
from ..param_panels import RectangleParamPanel, PolygonParamPanel

_ARC_SAMPLES = 12


# ==================== 几何生成 ====================

def buildRectanglePoints(p1: Point3D, p2: Point3D, chamferType: int,
                         radius) -> List[Point3D]:
    """由对角点生成矩形轮廓点列。

    chamferType: 0=无 1=圆角 2=斜角；radius 可为统一半径（float）
    或四角独立半径列表 [角1..角4]（同原版四角独立倒角）。
    角序与 corners 一致：角1=起点角，随后按周向。返回闭合点列（首尾不重复）。
    """
    x1, y1, x2, y2 = p1.x, p1.y, p2.x, p2.y
    if abs(x2 - x1) < 1e-9 or abs(y2 - y1) < 1e-9:
        return []
    corners = [Point3D(x1, y1, p1.z), Point3D(x2, y1, p1.z),
               Point3D(x2, y2, p1.z), Point3D(x1, y2, p1.z)]
    if chamferType == 0:
        return corners
    if isinstance(radius, (int, float)):
        radii = [float(radius)] * 4
    else:
        radii = ([float(r) for r in radius] + [0.0, 0.0, 0.0, 0.0])[:4]
        # 面板角3/角4与角点周向顺序镜像对调（按用户视觉对位）
        radii[2], radii[3] = radii[3], radii[2]
    if all(r <= 0 for r in radii):
        return corners
    # 单角半径不超过相邻两边（宽/高）的 45%（与波浪圆倒角钳制一致）
    sideCap = min(abs(x2 - x1), abs(y2 - y1)) * 0.45

    out: List[Point3D] = []
    n = len(corners)
    for i in range(n):
        r = min(radii[i], sideCap)
        prev, cur, nxt = corners[(i - 1) % n], corners[i], corners[(i + 1) % n]
        if r <= 1e-6:
            out.append(cur)   # 该角不倒角：保留直角（同原版逐角独立）
            continue
        if chamferType == 1:
            fr = computeFillet(cur, prev, cur, nxt, r)
            if fr.valid:
                out.append(fr.trimPt1)
                out.extend(sampleFilletArc(fr, _ARC_SAMPLES))
                out.append(fr.trimPt2)
            else:
                out.append(cur)
        else:
            d1 = (prev - cur)
            d2 = (nxt - cur)
            L1, L2 = d1.length(), d2.length()
            if L1 < 1e-9 or L2 < 1e-9:
                out.append(cur)
                continue
            rr = min(r, L1 * 0.45, L2 * 0.45)
            out.append(cur + d1 * (rr / L1))
            out.append(cur + d2 * (rr / L2))
    return out


def buildPolygonPoints(center: Point3D, sides: int, radius: float,
                       star: bool, innerRadius: float, filletRadius: float,
                       startAngle: float = math.pi / 2) -> List[Point3D]:
    """生成正多边形/星形轮廓点列（可选顶点倒角），闭合首尾不重复。"""
    if sides < 3 or radius <= 0:
        return []
    verts: List[Point3D] = []
    total = sides * 2 if star else sides
    step = 2 * math.pi / total
    for i in range(total):
        r = radius
        if star and i % 2 == 1:
            r = max(0.01, min(innerRadius, radius - 0.01))
        a = startAngle + i * step
        verts.append(Point3D(center.x + r * math.cos(a),
                             center.y + r * math.sin(a), center.z))
    if filletRadius > 1e-6:
        return applyFilletToVertices(verts, filletRadius)
    return verts


def _polyPath(pts: List[Point3D], closed: bool = True) -> QPainterPath:
    path = QPainterPath()
    if not pts:
        return path
    path.moveTo(pts[0].x, pts[0].y)
    for p in pts[1:]:
        path.lineTo(p.x, p.y)
    if closed and len(pts) > 2:
        path.lineTo(pts[0].x, pts[0].y)
    return path


def _applyPoints(obj: LineObject, pts: List[Point3D]) -> None:
    """整体替换 LineObject 的点列。"""
    lst = obj.points()
    lst.clear()
    lst.extend(pts)
    obj.setClosed(True)


# ==================== Rectangle ====================

class RectangleCommand(CommandInstance):
    # 预览路径即完整轮廓（视图平面坐标）：顶视/透视按构造平面投影，
    # 侧视按视图平面 (u,v) 映射，不画尾段
    previewPlaneBased = True

    def name(self) -> str:
        return "Rectangle"

    def commandTitle(self) -> str:
        return "矩形参数"

    def lastInputPoint(self):
        return getattr(self, "_first", None)

    def prompt(self) -> str:
        if self._first is None:
            return "指定矩形第一个角点"
        if not self._editMode:
            return "指定对角点（右键/回车完成，Esc 取消）"
        return "编辑模式：调整参数后确认（右键/回车确认，Esc 取消）"

    def start(self) -> None:
        super().start()
        self._first = None
        self._second = None
        self._editMode = False    # 点完两角点后锁定大小，进入调参待确认（同原版）
        self._chamferType = 0
        self._cornerRadii = [0.0, 0.0, 0.0, 0.0]   # 四角独立半径（同原版）
        self._waveEnabled = False
        self._panel = None
        self._obj = LineObject()
        self._doc.addObject(self._obj)

    def paramPanel(self):
        if self._panel is None:
            self._panel = RectangleParamPanel()
            self._panel.sizeChanged.connect(self._onPanelSize)
            self._panel.chamferTypeChanged.connect(self._onPanelChamferType)
            self._panel.chamferRadiiChanged.connect(self._onPanelChamferRadii)
            self._panel.waveEnabledToggled.connect(self._onPanelWaveEnabled)
            self._panel.waveParamChanged.connect(self._onPanelWaveParam)
            self._panel.confirmRequested.connect(self._onPanelConfirm)
            self._panel.cancelRequested.connect(self.cancel)
        return self._panel

    # ---------- 面板槽 ----------

    def _onPanelSize(self, length: float, width: float) -> None:
        if self._first is None or length <= 0 or width <= 0:
            return
        fp = self._planePts([self._first])[0]
        # 保持当前拖动象限（未拖动时默认右上）；象限按视图平面 (u,v) 度量
        du, dv = 1.0, 1.0
        if self._second is not None:
            sp = self._planePts([self._second])[0]
            du = 1.0 if sp.x >= fp.x else -1.0
            dv = 1.0 if sp.y >= fp.y else -1.0
        self._second = self._planeToWorld(
            [Point3D(fp.x + du * length, fp.y + dv * width, 0.0)],
            self._first)[0]
        self._rebuild()

    def _onPanelChamferType(self, t: int) -> None:
        self._chamferType = t
        self._rebuild()

    def _onPanelChamferRadii(self) -> None:
        if self._panel is not None:
            self._cornerRadii = self._panel.cornerRadii()
        self._rebuild()

    def _onPanelWaveEnabled(self, on: bool) -> None:
        self._waveEnabled = on
        self._rebuild()

    def _onPanelWaveParam(self) -> None:
        if self._waveEnabled:
            self._rebuild()

    def _onPanelConfirm(self) -> None:
        if self._first is None or self._second is None:
            return
        self._rebuild()
        if len(self._obj.points()) >= 3:
            self.finish()

    # ---------- 内部 ----------

    def _buildPoints(self, p1: Point3D, p2: Point3D) -> List[Point3D]:
        """按当前参数（波浪优先于倒角）生成轮廓点列——视图平面 (u,v) 坐标
        （顶视/2D 画布即 XY；侧视 XZ/YZ），世界坐标在 _rebuild 统一映射。"""
        pp1, pp2 = self._planePts([p1, p2])
        if self._waveEnabled and self._panel is not None:
            return buildWaveRectanglePoints(
                pp1, pp2, self._panel.waveType(),
                self._panel.longWaveCount(), self._panel.shortWaveCount(),
                self._panel.sideWaveHeight(), self._panel.cornerWaveHeight(),
                self._panel.cornerWaveWidth(), self._panel.cornerChamfer())
        return buildRectanglePoints(pp1, pp2, self._chamferType,
                                    list(self._cornerRadii))

    def _rebuild(self) -> None:
        if self._first is None or self._second is None:
            return
        pts = self._buildPoints(self._first, self._second)
        _applyPoints(self._obj, self._planeToWorld(pts, self._first))
        self._doc.notifyObjectChanged(self._obj.id())
        if self._panel is not None and pts:
            fp = self._planePts([self._first])[0]
            sp = self._planePts([self._second])[0]
            self._panel.setSize(abs(sp.x - fp.x), abs(sp.y - fp.y))

    def cancel(self) -> None:
        if self._obj is not None and len(self._obj.points()) < 3:
            self._doc.removeObject(self._obj.id())
        super().cancel()

    # ---------- 交互 ----------

    def onMousePress(self, wp: Point3D, mods) -> None:
        if self._first is None:
            self._first = Point3D(wp.x, wp.y, wp.z)
            return
        if self._editMode:
            # 编辑模式再点空白：对角点跳到新位置（同原版，可重新定大小）
            self._second = Point3D(wp.x, wp.y, wp.z)
            self._rebuild()
            return
        # 第二个角点 → 锁定大小进入编辑模式，待用户调参后确认（同原版）
        self._second = Point3D(wp.x, wp.y, wp.z)
        self._enterEditMode()

    def _enterEditMode(self) -> None:
        self._editMode = True
        self._rebuild()
        # 预览路径已固化为正式对象，隐藏拖拽预览只留光标点（同圆命令锁定态）
        self._setPreviewPath(QPainterPath())

    def onMouseMove(self, wp: Point3D, mods) -> None:
        if self._panel is not None:
            self._panel.setXYZ(wp.x, wp.y, wp.z)
        if self._first is None:
            self._setPreviewPath(QPainterPath())
            self._setCursorMarker(wp)
            return
        if self._editMode:
            # 大小已锁定：移动只更新光标点（同原版编辑模式）
            self._setCursorMarker(wp)
            return
        self._second = Point3D(wp.x, wp.y, wp.z)
        pts = self._buildPoints(self._first, self._second)
        self._setPreviewPath(_polyPath(pts), solid=True,
                             color=self._obj.color() if self._obj else None)
        if self._panel is not None and pts:
            self._panel.setSize(abs(wp.x - self._first.x),
                                abs(wp.y - self._first.y))

    def onRightClick(self, wp: Point3D) -> bool:
        if self._first is not None and self._second is not None:
            self._rebuild()
            if len(self._obj.points()) >= 3:
                self.finish()
                return True
        return False

    def onEnter(self) -> bool:
        return self.onRightClick(Point3D())


# ==================== Polygon ====================

class PolygonCommand(CommandInstance):
    # 预览路径即完整轮廓（视图平面坐标）：同矩形，不画尾段
    previewPlaneBased = True

    def name(self) -> str:
        return "Polygon"

    def commandTitle(self) -> str:
        return "多边形参数"

    def lastInputPoint(self):
        return getattr(self, "_center", None)

    def prompt(self) -> str:
        if self._center is None:
            return "指定多边形中心"
        if not self._editMode:
            return "拖动确定半径，点击锁定（右键/回车完成，Esc 取消）"
        return "编辑模式：调整参数后确认（右键/回车确认，Esc 取消）"

    def start(self) -> None:
        super().start()
        self._center = None
        self._radius = 25.0
        self._sides = 6
        self._star = False
        self._innerRadius = 12.5
        self._filletRadius = 0.0
        self._editMode = False    # 点完半径点后锁定大小，进入调参待确认（同矩形）
        self._panel = None
        self._obj = LineObject()
        self._doc.addObject(self._obj)

    def paramPanel(self):
        if self._panel is None:
            self._panel = PolygonParamPanel()
            self._panel.centerChanged.connect(self._onPanelCenter)
            self._panel.sidesChanged.connect(self._onPanelSides)
            self._panel.radiusChanged.connect(self._onPanelRadius)
            self._panel.starModeChanged.connect(self._onPanelStar)
            self._panel.innerRadiusChanged.connect(self._onPanelInner)
            self._panel.filletRadiusChanged.connect(self._onPanelFillet)
            self._panel.confirmed.connect(self._onPanelConfirmed)
            self._panel.cancelRequested.connect(self.cancel)
            self._panel.setSides(self._sides)
            self._panel.setRadius(self._radius)
            self._panel.setInnerRadius(self._innerRadius)
            self._panel.setFilletRadius(self._filletRadius)
            self._panel.setStarMode(self._star)
        return self._panel

    # ---------- 面板槽 ----------

    def _onPanelCenter(self, x: float, y: float) -> None:
        if self._center is not None:
            # 面板中心为视图平面 (u,v) 坐标（顶视/2D 即世界 XY），映射回世界
            self._center = self._planeToWorld(
                [Point3D(x, y, 0.0)], self._center)[0]
            self._rebuild()

    def _onPanelSides(self, n: int) -> None:
        self._sides = n
        self._rebuild()

    def _onPanelRadius(self, r: float) -> None:
        self._radius = r
        self._rebuild()

    def _onPanelStar(self, star: bool) -> None:
        self._star = star
        self._rebuild()

    def _onPanelInner(self, r: float) -> None:
        self._innerRadius = r
        self._rebuild()

    def _onPanelFillet(self, r: float) -> None:
        self._filletRadius = r
        self._rebuild()

    def _onPanelConfirmed(self) -> None:
        self._rebuild()
        if len(self._obj.points()) >= 3:
            self.finish()

    # ---------- 内部 ----------

    def _points(self) -> List[Point3D]:
        if self._center is None:
            return []
        # 视图平面 (u,v) 坐标构建（顶视/2D 即 XY；侧视 XZ/YZ），
        # 世界坐标在 _rebuild 统一映射
        cp = self._planePts([self._center])[0]
        return buildPolygonPoints(cp, self._sides, self._radius,
                                  self._star, self._innerRadius,
                                  self._filletRadius)

    def _rebuild(self) -> None:
        pts = self._points()
        if pts:
            _applyPoints(self._obj, self._planeToWorld(pts, self._center))
        else:
            _applyPoints(self._obj, [])
        self._doc.notifyObjectChanged(self._obj.id())
        if pts:
            self._setPreviewPath(QPainterPath())  # 已有实体，清掉预览
        if self._panel is not None and self._center is not None:
            self._panel.setRadius(self._radius)

    def _enterEditMode(self) -> None:
        self._editMode = True
        self._rebuild()
        # 预览路径已固化为正式对象，隐藏拖拽预览只留光标点（同矩形锁定态）
        self._setPreviewPath(QPainterPath())

    def cancel(self) -> None:
        if self._obj is not None and len(self._obj.points()) < 3:
            self._doc.removeObject(self._obj.id())
        super().cancel()

    # ---------- 交互 ----------

    def onMousePress(self, wp: Point3D, mods) -> None:
        if self._center is None:
            self._center = Point3D(wp.x, wp.y, wp.z)
            if self._panel is not None:
                u, v = self._uv(wp)
                self._panel.setCenter(u, v)
            return
        # 半径按视图平面 (u,v) 度量（侧视：前视 XZ / 右视 YZ）
        r = self._uvDist(wp, self._center)
        if self._editMode:
            # 编辑模式再点空白：半径点跳到新位置（同矩形，可重新定大小）
            if r > 1e-6:
                self._radius = r
            self._rebuild()
            return
        # 第二次点击：锁定半径进入编辑模式，待用户调参后确认（同矩形）
        if r > 1e-6:
            self._radius = r
        self._enterEditMode()

    def onMouseMove(self, wp: Point3D, mods) -> None:
        if self._panel is not None:
            self._panel.setXYZ(wp.x, wp.y, wp.z)
        if self._center is None:
            self._setPreviewPath(QPainterPath())
            self._setCursorMarker(wp)
            return
        if self._editMode:
            # 大小已锁定：移动只更新光标点（同矩形编辑模式）
            self._setCursorMarker(wp)
            return
        r = self._uvDist(wp, self._center)
        if r > 1e-6:
            self._radius = r
            if self._panel is not None:
                self._panel.setRadius(r)
        pts = self._points()
        self._setPreviewPath(_polyPath(pts), solid=True,
                             color=self._obj.color() if self._obj else None)
        self._setCursorMarker(wp)

    def onRightClick(self, wp: Point3D) -> bool:
        if self._editMode:
            self._rebuild()
            if len(self._obj.points()) >= 3:
                self.finish()
                return True
        return False

    def onEnter(self) -> bool:
        return self.onRightClick(Point3D())


def registerAll(manager) -> None:
    manager.register("Rectangle", RectangleCommand)
    manager.register("Polygon", PolygonCommand)

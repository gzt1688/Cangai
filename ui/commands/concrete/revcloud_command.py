"""修订云线命令（REVCLOUD 复刻，移植自开源 CAD cadcraft 的 RevcloudM）。

几何：沿边界多边形按目标弧长分点，相邻分点间以 bulge=0.5 的凸弧
（约 53°）相连，弧凸向外，闭合——与 AutoCAD 修订云线同构。
模式：矩形（两点）/ 多边形（多点右击闭合）/ 手绘（按住拖动采样）。
"""
from __future__ import annotations
import math
from typing import List, Optional
from PySide6.QtGui import QPainterPath

from core.vector.geometry_types import Point3D, ObjectType
from core.document import LAYER_CURVES_ID
from core.vector.revcloud_object import RevcloudObject
from ..command_instance import CommandInstance
from ..param_panels import RevcloudPanel

_MAX_GEN = 4096


def _shoelace(pts: List[Point3D]) -> float:
    s = 0.0
    n = len(pts)
    for i in range(n):
        a = pts[i]
        b = pts[(i + 1) % n]
        s += a.x * b.y - b.x * a.y
    return s / 2.0


def _lerp(a: Point3D, b: Point3D, t: float) -> Point3D:
    return Point3D(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t,
                   a.z + (b.z - a.z) * t)


def cloud_vertices(boundary: List[Point3D], arc_len: float = 0.0,
                   per_edge: bool = True, reverse: bool = False,
                   target_arcs: int = 16) -> Optional[List[Point3D]]:
    """边界多边形 → 云线分点列（cadcraft cloud_vertices 移植）。

    arc_len = 0 时自适应为目标弧数（target_arcs）对应的弧长。
    每个分点间将由 bulge=0.5 凸弧连接（见 RevcloudObject）。"""
    pts: List[Point3D] = []
    for p in boundary:
        if not pts or p.distanceTo(pts[-1]) > 1e-9:
            pts.append(p)
    while len(pts) > 1 and pts[0].distanceTo(pts[-1]) < 1e-9:
        pts.pop()
    n = len(pts)
    if n < 3:
        return None
    area = _shoelace(pts)
    if abs(area) < 1e-12:
        return None
    if area < 0.0:
        pts.reverse()          # 统一逆时针（屏幕 y 向下时凸向外）
    perim = sum(pts[i].distanceTo(pts[(i + 1) % n]) for i in range(n))
    if arc_len <= 0.0:
        arc_len = perim / max(target_arcs, 3)
    arc_len = max(arc_len, perim / _MAX_GEN)
    out: List[Point3D] = []
    if per_edge:
        for i in range(n):
            a, b = pts[i], pts[(i + 1) % n]
            k = max(1, min(_MAX_GEN, round(a.distanceTo(b) / arc_len)))
            for j in range(k):
                out.append(_lerp(a, b, j / k))
    else:
        # 手绘：按累计弧长均匀分点（自由轨迹）
        k = max(3, min(_MAX_GEN, round(perim / arc_len)))
        acc = [0.0]
        for i in range(n):
            acc.append(acc[-1] + pts[i].distanceTo(pts[(i + 1) % n]))
        for j in range(k):
            target = perim * j / k
            lo, hi = 0, n - 1
            while lo < hi and acc[lo + 1] < target:
                lo += 1
            seg = acc[lo + 1] - acc[lo]
            t = 0.0 if seg < 1e-12 else (target - acc[lo]) / seg
            out.append(_lerp(pts[lo], pts[(lo + 1) % n], t))
    return out


def _polyPath(pts: List[Point3D], closed: bool = False) -> QPainterPath:
    path = QPainterPath()
    if not pts:
        return path
    path.moveTo(pts[0].x, pts[0].y)
    for p in pts[1:]:
        path.lineTo(p.x, p.y)
    if closed and len(pts) > 2:
        path.closeSubpath()
    return path


class RevcloudCommand(CommandInstance):
    """修订云线：矩形（两点）/ 多边形（多点右击闭合）/ 手绘（按住拖动）/
    选对象（点击闭合曲线，以它为边界生成云线）。
    默认矩形；面板可切换模式（选中高亮）。与 cadcraft RevcloudM 同构，
    选对象模式对应其 Object 分支（object_outline）。"""

    previewPlaneBased = True

    def name(self) -> str:
        return "Revcloud"

    def commandTitle(self) -> str:
        return "修订云线"

    def lastInputPoint(self):
        return self._pts[-1] if self._pts else None

    def prompt(self) -> str:
        if self._editMode:
            return "调整单弧尺寸后确认（右键/回车确认生成，Esc 取消）"
        if self._mode == 'rect':
            if not self._pts:
                return "指定修订云线的第一个角点（面板可切换模式）"
            return "指定对角点（右键/回车完成，Esc 取消）"
        if self._mode == 'poly':
            if not self._pts:
                return "指定修订云线起点（面板可切换模式）"
            return "指定下一点（右击闭合完成，Esc 取消）"
        if self._mode == 'object':
            return "点击一条闭合曲线/圆/椭圆，以它为边界生成云线（Esc 取消）"
        return "按住鼠标沿云线路径拖动，松开完成（Esc 取消）"

    def start(self) -> None:
        super().start()
        self._mode = 'rect'
        self._pts: List[Point3D] = []
        self._drawing = False       # 手绘拖动中
        self._editMode = False      # 边界已锁定，调参待确认
        self._boundaryPts: List[Point3D] = []   # 编辑模式固化的边界（世界坐标）
        self._arcLen = 0.0          # 单弧尺寸（0 = 自适应：周长/16）
        self._panel = None
        self._obj = RevcloudObject()
        self._doc.addObject(self._obj)
        self._previewPts: List[Point3D] = []

    def paramPanel(self):
        if self._panel is None:
            self._panel = RevcloudPanel()
            self._panel.modeChanged.connect(self._onPanelMode)
            self._panel.sizeChanged.connect(self._onPanelSize)
            self._panel.arcLenChanged.connect(self._onPanelArcLen)
            self._panel.confirmRequested.connect(self._onPanelConfirm)
            self._panel.cancelRequested.connect(self.cancel)
        return self._panel

    def _onPanelMode(self, mode: str) -> None:
        if self._editMode:
            return   # 编辑模式不允许换模式
        self._mode = mode
        self._pts = []
        self._refreshPreview()

    def _onPanelSize(self, length: float, width: float) -> None:
        """面板长/宽编辑（矩形模式）：按当前象限重设对角点。"""
        if self._mode != 'rect' or length <= 0 or width <= 0:
            return
        if self._editMode:
            a = self._boundaryPts[0]
            du = 1.0
            dv = 1.0
            if len(self._boundaryPts) >= 2:
                du = 1.0 if self._boundaryPts[1].x >= a.x else -1.0
                dv = 1.0 if self._boundaryPts[1].y >= a.y else -1.0
            self._boundaryPts = self._rectBoundary(
                a, Point3D(a.x + du * length, a.y + dv * width, a.z))
            self._rebuildObj()
            return
        if not self._pts or length <= 0 or width <= 0:
            return
        a = self._pts[0]
        du = 1.0
        dv = 1.0
        if len(self._pts) >= 2:
            du = 1.0 if self._pts[1].x >= a.x else -1.0
            dv = 1.0 if self._pts[1].y >= a.y else -1.0
        second = Point3D(a.x + du * length, a.y + dv * width, a.z)
        if len(self._pts) >= 2:
            self._pts[1] = second
        else:
            self._pts.append(second)
        self._refreshPreview()

    def _onPanelArcLen(self, v: float) -> None:
        """单弧尺寸（每段弧的弦长；0 = 自适应周长/16）。编辑模式实时重生成。"""
        self._arcLen = max(0.0, float(v))
        if self._editMode:
            self._rebuildObj()
            self._panel.setArcLen(self._effectiveArcLen(self._boundaryPts))
        else:
            self._refreshPreview()

    def _effectiveArcLen(self, boundary: List[Point3D]) -> float:
        """实际生效的单弧尺寸：固定值，或自适应（周长/16）。"""
        if self._arcLen > 0.0 or len(boundary) < 3:
            return self._arcLen
        n = len(boundary)
        perim = sum(boundary[i].distanceTo(boundary[(i + 1) % n])
                    for i in range(n))
        return perim / 16.0

    def _onPanelConfirm(self) -> None:
        if self._editMode:
            self._commit()
        elif self._mode == 'poly' and len(self._pts) >= 3:
            self._enterEditMode(self._planeToWorld(
                self._boundary(), self._pts[0]))
        elif self._mode == 'rect' and len(self._pts) >= 2:
            self._enterEditMode(self._planeToWorld(
                self._boundary(), self._pts[0]))

    # ---------- 云线构建 ----------

    def _rectBoundary(self, a: Point3D, b: Point3D) -> List[Point3D]:
        return [Point3D(a.x, a.y, 0.0), Point3D(b.x, a.y, 0.0),
                Point3D(b.x, b.y, 0.0), Point3D(a.x, b.y, 0.0)]

    def _boundary(self, extra: Optional[Point3D] = None) -> List[Point3D]:
        pts = list(self._pts)
        if extra is not None:
            pts.append(extra)
        if self._mode == 'rect' and len(pts) >= 2:
            return self._rectBoundary(pts[0], pts[1])
        return pts

    def _objectOutline(self, obj) -> Optional[List[Point3D]]:
        """闭合对象的轮廓采样点（云线边界基础）。不闭合/不支持 → None。"""
        t = obj.type()
        if t == ObjectType.LINE:
            if obj.isClosed() and len(obj.points()) >= 3:
                return list(obj.points())
            return None
        if t == ObjectType.CIRCLE:
            c, r = obj.center(), obj.radius()
            return [Point3D(c.x + r * math.cos(math.tau * i / 64),
                            c.y + r * math.sin(math.tau * i / 64), 0.0)
                    for i in range(64)]
        if t == ObjectType.ELLIPSE:
            c = obj.center()
            a, b = obj.majorAxis(), obj.minorAxis()
            rot = math.radians(obj.rotation()) if hasattr(obj, 'rotation') else 0.0
            out = []
            for i in range(72):
                th = math.tau * i / 72
                x, y = a * math.cos(th), b * math.sin(th)
                out.append(Point3D(c.x + x * math.cos(rot) - y * math.sin(rot),
                                   c.y + x * math.sin(rot) + y * math.cos(rot),
                                   0.0))
            return out
        if t in (ObjectType.CURVE, ObjectType.NURBS_CURVE):
            if getattr(obj, 'isClosed', lambda: False)():
                return list(obj.getSampledPoints())
            return None
        if t == ObjectType.POLYCURVE:
            if getattr(obj, 'isClosed', lambda: False)():
                out = []
                for seg in obj.segments():
                    for p in seg.get("points", []):
                        pt = Point3D(p[0], p[1], p[2])
                        if not out or out[-1].distanceTo(pt) > 1e-9:
                            out.append(pt)
                if len(out) >= 3 and out[0].distanceTo(out[-1]) < 1e-9:
                    out.pop()
                if len(out) >= 3:
                    return out
            return None
        if t == ObjectType.REVCLOUD:
            return list(obj.vertices())
        return None

    def _pickClosedObject(self, wp: Point3D):
        """拾取鼠标附近的闭合对象（云线本体除外）。"""
        scale = abs(self._view.transform().m11()) if self._view is not None else 1.0
        worldR = 15.0 / max(scale, 1e-6)   # 拾取半径（像素换算世界距离）
        best = None
        bestDist = worldR
        for obj in self._doc.visibleObjects():
            if obj is self._obj:
                continue
            pts = self._objectOutline(obj)
            if pts is None or len(pts) < 3:
                continue
            for i in range(len(pts)):
                a, b = pts[i], pts[(i + 1) % len(pts)]
                vx, vy = b.x - a.x, b.y - a.y
                L2 = vx * vx + vy * vy
                if L2 < 1e-18:
                    continue
                t = max(0.0, min(1.0, ((wp.x - a.x) * vx
                                       + (wp.y - a.y) * vy) / L2))
                d = math.hypot(wp.x - (a.x + t * vx), wp.y - (a.y + t * vy))
                if d <= bestDist:
                    bestDist = d
                    best = obj
        return best

    def _enterEditMode(self, boundary_world: List[Point3D]) -> None:
        """边界确认 → 锁定进入编辑模式：调单弧尺寸实时预览，确认生成。"""
        self._boundaryPts = list(boundary_world)
        self._editMode = True
        self._rebuildObj()
        if self._panel is not None:
            self._panel.setArcLen(self._effectiveArcLen(self._boundaryPts))
        self._setPreviewPath(QPainterPath())   # 固化到正式对象，隐藏预览

    def _rebuildObj(self) -> None:
        """按固化边界 + 当前单弧尺寸重建云线对象（编辑模式实时预览）。"""
        vs = cloud_vertices(self._boundaryPts, self._arcLen)
        if vs is None:
            return
        self._obj.setVertices(vs)
        self._obj.setArcLength(self._arcLen)
        self._doc.notifyObjectChanged(self._obj.id())

    def _commit(self) -> None:
        if self._editMode:
            self._setPreviewPath(QPainterPath())
            self.finish()
            return
        b = self._planeToWorld(self._boundary(), self._pts[0]) if self._pts else []
        vs = cloud_vertices(b, self._arcLen)
        if vs is None:
            return
        self._obj.setVertices(vs)
        self._obj.setArcLength(self._arcLen)
        self._doc.notifyObjectChanged(self._obj.id())
        self._setPreviewPath(QPainterPath())
        self.finish()

    # ---------- 交互 ----------

    def cancel(self) -> None:
        if self._obj is not None:
            self._doc.removeObject(self._obj.id())   # 取消 = 移除临时对象
        super().cancel()

    def onMousePress(self, wp: Point3D, mods) -> None:
        p = self._planePts([wp])[0]
        if self._editMode:
            # 编辑模式点击空白：矩形可重设对角，其他模式忽略
            if self._mode == 'rect':
                a = self._boundaryPts[0]
                du = 1.0 if wp.x >= a.x else -1.0
                dv = 1.0 if wp.y >= a.y else -1.0
                self._boundaryPts = self._rectBoundary(
                    a, Point3D(a.x + du * abs(wp.x - a.x),
                               a.y + dv * abs(wp.y - a.y), a.z))
                self._rebuildObj()
            return
        if self._mode == 'free':
            self._drawing = True
            self._pts = [p]
            return
        if self._mode == 'object':
            target = self._pickClosedObject(wp)
            if target is None:
                if self._manager is not None:
                    self._manager.promptChanged.emit(
                        "*未点到闭合曲线*  点击一条闭合曲线/圆/椭圆")
                return
            outline = self._objectOutline(target)
            if outline is None:
                return
            self._enterEditMode(outline)   # 锁定边界 → 编辑模式（同构）
            return
        if self._mode == 'rect':
            if not self._pts:
                self._pts = [p]
            else:
                self._pts.append(p)
                self._enterEditMode(self._planeToWorld(
                    self._boundary(), self._pts[0]))
            return
        # poly
        self._pts.append(p)
        self._refreshPreview()

    def onMouseMove(self, wp: Point3D, mods) -> None:
        p = self._planePts([wp])[0]
        if self._panel is not None:
            self._panel.setXYZ(wp.x, wp.y, wp.z)
        if self._editMode:
            self._setCursorMarker(wp)   # 边界已锁定：只更新光标点
            return
        if self._mode == 'object':
            # 悬停预览：光标下有闭合对象时实时显示云线效果
            target = self._pickClosedObject(wp)
            if target is not None:
                outline = self._objectOutline(target)
                vs = cloud_vertices(outline, self._arcLen,
                                    per_edge=False) if outline else None
                if vs is not None:
                    tmp = RevcloudObject(vs, self._arcLen)
                    self._setPreviewPath(tmp.path(), solid=True,
                                         color=self._previewColor())
                    self._setCursorMarker(wp)
                    return
            self._setPreviewPath(QPainterPath())
            self._setCursorMarker(wp)
            return
        if self._mode == 'free' and self._drawing:
            if not self._pts or p.distanceTo(self._pts[-1]) > self._freeStep():
                self._pts.append(p)
            self._refreshPreview()
            self._syncSize()
            return
        self._refreshPreview(extra=p)
        self._syncSize(extra=p)

    def _syncSize(self, extra: Optional[Point3D] = None) -> None:
        """拖动/加点时回写面板长宽与单弧尺寸（自适应值随边界实时更新）。"""
        if self._panel is None:
            return
        b = self._boundary(extra)
        if not b:
            return
        xs = [q.x for q in b]
        ys = [q.y for q in b]
        self._panel.setSize(max(xs) - min(xs), max(ys) - min(ys))
        if self._arcLen <= 0.0 and len(b) >= 3:
            n = len(b)
            perim = sum(b[i].distanceTo(b[(i + 1) % n]) for i in range(n))
            self._panel.setArcLen(perim / 16.0)

    def _previewColor(self):
        """预览色取曲线层颜色：与固化后实体颜色一致。"""
        try:
            return self._doc.layer(LAYER_CURVES_ID).color
        except Exception:
            return None

    def onMouseRelease(self, mods) -> None:
        if self._mode == 'free' and self._drawing:
            self._drawing = False
            self._enterEditMode(self._planeToWorld(
                self._boundary(), self._pts[0]))

    def onRightClick(self, wp: Point3D) -> bool:
        if self._editMode:
            self._commit()   # 编辑模式右击 = 确认生成
            return True
        if self._mode == 'poly' and len(self._pts) >= 3:
            self._enterEditMode(self._planeToWorld(
                self._boundary(), self._pts[0]))
            return True
        if self._mode == 'rect' and len(self._pts) >= 2:
            self._enterEditMode(self._planeToWorld(
                self._boundary(), self._pts[0]))
            return True
        return False

    def onEnter(self) -> bool:
        if self._editMode:
            self._commit()
            return True
        return self.onRightClick(self._lastWpSafe())

    def _lastWpSafe(self) -> Point3D:
        return self._pts[-1] if self._pts else Point3D()

    def _freeStep(self) -> float:
        if len(self._pts) < 2:
            return 1e-6
        # 自适应：以已走路径估算，采样间距 = 周长/64 与最小步长的折衷
        per = sum(self._pts[i].distanceTo(self._pts[i + 1])
                  for i in range(len(self._pts) - 1))
        return max(per / 32.0, 1e-6)

    def _refreshPreview(self, extra: Optional[Point3D] = None) -> None:
        # 锚点：首放点；尚未落点时用光标自身（planeToWorld 的原点）
        anchor = self._pts[0] if self._pts else (extra or Point3D())
        b = self._planeToWorld(self._boundary(extra),
                               anchor) if (self._pts or extra) else []
        vs = cloud_vertices(b, self._arcLen)
        if vs is None:
            self._setPreviewPath(_polyPath(b, closed=True))
            return
        obj = RevcloudObject(vs, self._arcLen)
        self._setPreviewPath(obj.path(), solid=True,
                             color=self._obj.color() if self._obj else None)


def registerAll(manager) -> None:
    manager.register("Revcloud", RevcloudCommand)

"""编辑命令：Chamfer 倒角 / Trim 修剪（移植自原 C++ 项目）。

交互流程：
- Chamfer 倒角：
  - 单角模式：点选第一条边 → 点选第二条边 → 实时预览 → 回车/右键/面板确认
  - 整线模式：点选一条多段线 → 预览全部尖角 → 确认批量执行
- Trim 修剪：
  - 选择边界曲线 → 选择要修剪的曲线 → 点击要保留的一侧 → 确认执行
v1 仅支持 LineObject 边（矩形/多边形产物即 LineObject，闭环自洽）。
"""
from __future__ import annotations
import math
from typing import List, Optional, Tuple
from PySide6.QtCore import Qt
from PySide6.QtGui import QPainterPath

from core.vector.geometry_types import Point3D, BoundingBox3D, ObjectType
from core.vector.line_object import LineObject
from core.vector.poly_curve_object import PolyCurveObject
from core.vector.fillet_processor import (FilletResult, CornerResult,
                                          computeFillet, computeBevel,
                                          sampleFilletArc, nearestPointOnSegment,
                                          computeAllCornersFillet,
                                          computeAllCornersBevel,
                                          lineLineIntersection,
                                          cornerFromProjection,
                                          PICK_PIXEL_RADIUS, ARC_SAMPLES)
from ..command_instance import CommandInstance
from ..param_panels import (ChamferParamPanel, TrimParamPanel, ArrayParamPanel,
                            OffsetParamPanel, MirrorParamPanel,
                            ExtendParamPanel, CloseParamPanel,
                            OutlineParamPanel, AlignParamPanel)
from core.vector.array_generator import (rectangularArray, circularArray,
                                          curveArray, getObjectsCenter)
from core.vector.mirror_geometry import mirrorObject
from core.vector.join_geometry import (joinObjects, _segmentsOf as _objToSegments)
from core.vector.extend_geometry import extendObject
from core.vector.close_geometry import closingSegmentFor
from core.vector.outline_geometry import (computeOuterOutline,
                                          simplifyPolyline,
                                          sampleClosed, sampleOpen)

_PICK_FAIL_SAME_SEG = "请选择不同的边"
_PICK_FAIL_NOT_ADJ = "同一多段线上请选择相邻两边，或选择两条独立直线"


class _EdgePick:
    """边拾取结果：对象 + 线段索引（支持闭合回边）。"""

    def __init__(self, obj: LineObject, segIdx: int, closing: bool = False) -> None:
        self.obj = obj
        self.segIdx = segIdx
        pts = obj.points()
        if closing:
            self._seg = (pts[-1], pts[0])
        else:
            self._seg = (pts[segIdx], pts[segIdx + 1])

    def segment(self) -> Tuple[Point3D, Point3D]:
        return self._seg

    def sameObject(self, other: "_EdgePick") -> bool:
        return self.obj is other.obj


class ChamferCommand(CommandInstance):
    def name(self) -> str:
        return "Chamfer"

    def commandTitle(self) -> str:
        return "倒角参数"

    # ---------- 生命周期 ----------

    def start(self) -> None:
        super().start()
        self._mode = 0            # 0=圆角 1=斜角
        self._radius = 5.0
        self._d1 = 5.0
        self._d2 = 5.0
        self._selectMode = 0      # 0=单角 1=整线
        self._pick1: Optional[_EdgePick] = None
        self._pick2: Optional[_EdgePick] = None
        self._wholeObj: Optional[LineObject] = None
        self._panel = None
        self._pickState = "waitFirst"
        self._msg = ""

    def prompt(self) -> str:
        if self._msg:
            return self._msg
        if self._selectMode == 1:
            if self._pickState == "waitWhole":
                return "选择要倒角的多段线"
            return "预览中：回车/右键/面板确认执行，Esc 取消"
        if self._pickState == "waitFirst":
            return "选择第一条边"
        return "移动到另一条边上实时预览，点击确认倒角"

    def paramPanel(self):
        if self._panel is None:
            self._panel = ChamferParamPanel()
            self._panel.modeChanged.connect(self._onPanelMode)
            self._panel.radiusChanged.connect(self._onPanelRadius)
            self._panel.distanceChanged.connect(self._onPanelDistance)
            self._panel.selectModeChanged.connect(self._onPanelSelectMode)
            self._panel.confirmed.connect(self._onPanelConfirmed)
            self._panel.setRadius(self._radius)
            self._panel.setDistances(self._d1, self._d2)
        return self._panel

    # ---------- 面板槽 ----------

    def _onPanelMode(self, mode: int) -> None:
        self._mode = mode
        self._refreshPreview()

    def _onPanelRadius(self, r: float) -> None:
        self._radius = r
        self._refreshPreview()

    def _onPanelDistance(self, d1: float, d2: float) -> None:
        self._d1, self._d2 = d1, d2
        self._refreshPreview()

    def _onPanelSelectMode(self, mode: int) -> None:
        self._selectMode = mode
        self._resetSelection()

    def _onPanelConfirmed(self) -> None:
        if self._pickState == "adjustWhole":
            self._confirm()

    def _resetSelection(self) -> None:
        self._pick1 = self._pick2 = None
        self._wholeObj = None
        self._pickState = "waitWhole" if self._selectMode == 1 else "waitFirst"
        self._msg = ""
        self._setPreviewPath(QPainterPath())

    # ---------- 边拾取 ----------

    def _worldPickRadius(self) -> float:
        scale = abs(self._view.transform().m11()) if self._view is not None else 1.0
        return PICK_PIXEL_RADIUS / max(scale, 1e-6)

    def _pickEdge(self, wp: Point3D) -> Optional[_EdgePick]:
        """拾取鼠标附近最近的 LineObject 线段（含闭合回边）。"""
        if self._doc is None:
            return None
        worldR = self._worldPickRadius()
        best: Optional[_EdgePick] = None
        bestDist = worldR
        for obj in self._doc.visibleObjects():
            if obj.type().name != "LINE":
                continue
            pts = obj.points()
            for i in range(len(pts) - 1):
                near, _ = nearestPointOnSegment(pts[i], pts[i + 1], wp)
                d = near.distanceTo(wp)
                if d <= bestDist:
                    bestDist = d
                    best = _EdgePick(obj, i)
            # 闭合但首末点不重合 → 补拾取闭合回边（如矩形 4 顶点闭环）
            closed = bool(obj.isClosed()) if hasattr(obj, "isClosed") else False
            if closed and len(pts) >= 2 and pts[0].distanceTo(pts[-1]) > 1e-6:
                near, _ = nearestPointOnSegment(pts[-1], pts[0], wp)
                d = near.distanceTo(wp)
                if d <= bestDist:
                    bestDist = d
                    best = _EdgePick(obj, len(pts) - 1, closing=True)
        return best

    # ---------- 几何计算 ----------

    def _hoverCorner(self, pick: _EdgePick, wp: Point3D) -> Optional[CornerResult]:
        """按鼠标在第二条边上的投影计算倒角结果（与预览一致，所见即所得）。"""
        if self._pick1 is None or pick is None:
            return None
        s1, e1 = self._pick1.segment()
        s2, e2 = pick.segment()
        return cornerFromProjection(s1, e1, s2, e2, wp, bevel=(self._mode == 1))

    def _secondPickError(self, pick: _EdgePick) -> str:
        """第二条边是否可选；不可选返回错误提示。"""
        if self._pick1.sameObject(pick):
            if pick.segIdx == self._pick1.segIdx:
                return _PICK_FAIL_SAME_SEG
            if self._sharedCornerIdx(self._pick1, pick) is None:
                return _PICK_FAIL_NOT_ADJ
        else:
            p1, p2 = self._pick1.obj.points(), pick.obj.points()
            if len(p1) != 2 or len(p2) != 2:
                return _PICK_FAIL_NOT_ADJ
        return ""

    @staticmethod
    def _sharedCornerIdx(a: _EdgePick, b: _EdgePick) -> Optional[int]:
        """两条边共享的角点顶点索引；不相邻（无公共端点）返回 None。

        兼容闭合回边（矩形 4 顶点闭环）与首末点重合（三角形式）两种表示。
        """
        pts = a.obj.points()
        s1, e1 = a.segment()
        s2, e2 = b.segment()
        for k, p in enumerate(pts):
            if (p.distanceTo(s1) < 1e-6 or p.distanceTo(e1) < 1e-6) and \
               (p.distanceTo(s2) < 1e-6 or p.distanceTo(e2) < 1e-6):
                return k
        return None

    def _wholeResults(self) -> List[CornerResult]:
        if self._wholeObj is None:
            return []
        pts = self._wholeObj.points()
        if self._mode == 0:
            return computeAllCornersFillet(pts, self._radius)
        return computeAllCornersBevel(pts, self._d1, self._d2)

    @staticmethod
    def _cornerPath(cr: CornerResult, isFillet: bool) -> QPainterPath:
        path = QPainterPath()
        if not cr.valid:
            return path
        path.moveTo(cr.trimPt1.x, cr.trimPt1.y)
        if isFillet:
            fr = FilletResult(valid=True, intersection=cr.intersection,
                              center=cr.center, trimPt1=cr.trimPt1,
                              trimPt2=cr.trimPt2, arcMid=cr.arcMid)
            for p in sampleFilletArc(fr, ARC_SAMPLES):
                path.lineTo(p.x, p.y)
        path.lineTo(cr.trimPt2.x, cr.trimPt2.y)
        return path

    def _refreshPreview(self) -> None:
        path = QPainterPath()
        if self._pickState == "adjustWhole":
            for cr in self._wholeResults():
                if cr.valid:
                    path.addPath(self._cornerPath(cr, self._mode == 0))
        self._setPreviewPath(path)

    # ---------- 交互 ----------

    def onMousePress(self, wp: Point3D, mods) -> None:
        self._msg = ""
        if self._selectMode == 1:
            self._pickWhole(wp)
            return
        if self._pickState == "waitFirst":
            pick = self._pickEdge(wp)
            if pick is not None:
                self._pick1 = pick
                self._pickState = "waitSecond"
            return
        if self._pickState == "waitSecond":
            # 悬停预览后单击立即确认（所见即所得）
            pick = self._pickEdge(wp)
            if pick is None:
                return
            err = self._secondPickError(pick)
            if err:
                self._msg = err
                return
            if self._confirmHover(pick, wp):
                self.finish()

    def _pickWhole(self, wp: Point3D) -> None:
        if self._pickState != "waitWhole":
            return
        pick = self._pickEdge(wp)
        if pick is None:
            return
        if len(pick.obj.points()) < 3:
            self._msg = "整线模式需要至少 3 个点的多段线"
            return
        self._wholeObj = pick.obj
        self._pickState = "adjustWhole"
        self._refreshPreview()

    def onMouseMove(self, wp: Point3D, mods) -> None:
        if self._panel is not None:
            self._panel.setXYZ(wp.x, wp.y, wp.z)
        if self._selectMode == 0 and self._pickState == "waitSecond":
            self._updateHoverPreview(wp)

    def _updateHoverPreview(self, wp: Point3D) -> None:
        """悬停在第二条边上时实时预览倒角；离开边则清除预览。"""
        path = QPainterPath()
        pick = self._pickEdge(wp)
        if pick is not None and not self._secondPickError(pick):
            cr = self._hoverCorner(pick, wp)
            if cr is not None and cr.valid:
                path = self._cornerPath(cr, self._mode == 0)
        self._setPreviewPath(path)

    def onRightClick(self, wp: Point3D) -> bool:
        if self._pickState == "adjustWhole":
            self._confirm()
            return True
        return False

    def onEnter(self) -> bool:
        if self._pickState == "adjustWhole":
            self._confirm()
            return True
        return False

    # ---------- 执行 ----------

    @staticmethod
    def _setPoints(obj: LineObject, pts: List[Point3D]) -> None:
        lst = obj.points()
        lst.clear()
        lst.extend(pts)

    def _arcSamplesOf(self, cr: CornerResult) -> List[Point3D]:
        if self._mode != 0:
            return []
        fr = FilletResult(valid=True, intersection=cr.intersection,
                          center=cr.center, trimPt1=cr.trimPt1,
                          trimPt2=cr.trimPt2, arcMid=cr.arcMid)
        return sampleFilletArc(fr, ARC_SAMPLES)

    def _confirm(self) -> None:
        if self._pickState == "adjustWhole":
            if self._confirmWhole():
                self.finish()

    def _confirmHover(self, pick: _EdgePick, wp: Point3D) -> bool:
        """按悬停预览结果立即执行倒角（单角模式单击确认）。"""
        cr = self._hoverCorner(pick, wp)
        if cr is None or not cr.valid:
            self._msg = "当前鼠标位置无法倒角（两边平行或距离过近）"
            return False
        self._pick2 = pick
        if self._pick1.sameObject(pick):
            return self._applyCornerOnPolyline(self._pick1.obj, cr)
        return self._applyCornerOnTwoObjects(cr)

    def _applyCornerOnPolyline(self, obj: LineObject, cr: CornerResult) -> bool:
        """同一多段线相邻两边：角点顶点替换为 修剪点+弧采样+修剪点。"""
        pts = list(obj.points())
        corner = self._sharedCornerIdx(self._pick1, self._pick2)
        if corner is None:
            return False
        middle = self._arcSamplesOf(cr)
        # 修剪点按与角点前一顶点（闭合时为末点）的距离排序，保证接线顺序正确
        prevPt = pts[corner - 1] if corner > 0 else pts[-1]
        if cr.trimPt1.distanceTo(prevPt) <= cr.trimPt2.distanceTo(prevPt):
            seq = [cr.trimPt1] + middle + [cr.trimPt2]
        else:
            seq = [cr.trimPt2] + middle + [cr.trimPt1]
        pts[corner:corner + 1] = seq
        self._setPoints(obj, pts)
        self._doc.notifyObjectChanged(obj.id())
        return True

    def _applyCornerOnTwoObjects(self, cr: CornerResult) -> bool:
        """两条独立直线：各自修剪，新建连接线（圆弧或直线）。"""
        for pick, trim in ((self._pick1, cr.trimPt1), (self._pick2, cr.trimPt2)):
            pts = list(pick.obj.points())
            s, e = pts[pick.segIdx], pts[pick.segIdx + 1]
            if s.distanceTo(cr.intersection) <= e.distanceTo(cr.intersection):
                newPts = [trim] + pts[pick.segIdx + 1:]
            else:
                newPts = pts[:pick.segIdx + 1] + [trim]
            if len(newPts) < 2:
                return False
            self._setPoints(pick.obj, newPts)
            self._doc.notifyObjectChanged(pick.obj.id())
        conn = LineObject([cr.trimPt1] + self._arcSamplesOf(cr) + [cr.trimPt2])
        self._doc.addObject(conn)
        return True

    def _confirmWhole(self) -> bool:
        if self._wholeObj is None:
            return False
        results = [cr for cr in self._wholeResults() if cr.valid]
        if not results:
            self._msg = "该多段线没有可倒角的尖角"
            return False
        pts = list(self._wholeObj.points())
        # 从后往前替换，避免索引偏移
        for cr in sorted(results, key=lambda c: c.cornerIndex, reverse=True):
            i = cr.cornerIndex
            if i <= 0 or i >= len(pts):
                continue
            middle = self._arcSamplesOf(cr)
            pts[i:i + 1] = [cr.trimPt1] + middle + [cr.trimPt2]
        self._setPoints(self._wholeObj, pts)
        self._doc.notifyObjectChanged(self._wholeObj.id())
        return True



# ==================== 群组 / 取消群组 ====================

class GroupCommand(CommandInstance):
    """群组：把当前选中的对象编为一组（共享同一组 id）。
    工具栏“群组”按钮左键触发；一次性动作，编组后立即结束。"""

    def name(self) -> str:
        return "Group"

    def commandTitle(self) -> str:
        return "群组"

    def prompt(self) -> str:
        # 一次性命令：无逐步引导，保持“提示信息显示区”
        return "提示信息显示区"

    def showSnapHints(self) -> bool:
        return False

    def start(self) -> None:
        n = self._doc.groupSelected() if self._doc is not None else 0
        if n:
            self._manager.notifyResult(f"已编组 {n} 个对象")
        else:
            self._manager.notifyResult("群组：请先选中要编组的对象")
        self.finish()

    def onMousePress(self, wp, mods) -> None:
        pass

    def onMouseMove(self, wp, mods) -> None:
        pass


class UngroupCommand(CommandInstance):
    """取消群组：清空当前选中对象的组归属。
    工具栏“群组”按钮右键触发；一次性动作。"""

    def name(self) -> str:
        return "Ungroup"

    def commandTitle(self) -> str:
        return "取消群组"

    def prompt(self) -> str:
        # 一次性命令：无逐步引导，保持“提示信息显示区”
        return "提示信息显示区"

    def showSnapHints(self) -> bool:
        return False

    def start(self) -> None:
        n = self._doc.ungroupSelected() if self._doc is not None else 0
        if n:
            self._manager.notifyResult(f"已取消 {n} 个对象的编组")
        else:
            self._manager.notifyResult("取消群组：当前选中对象不在任何组中")
        self.finish()

    def onMousePress(self, wp, mods) -> None:
        pass

    def onMouseMove(self, wp, mods) -> None:
        pass


# ==================== Join 组合 ====================

class JoinCommand(CommandInstance):
    """组合：把当前选中对象里端点重合的开放曲线合并为 PolyCurve。
    工具栏“组合”按钮触发；一次性动作，合并后立即结束。
    若合并前选中对象数为 N，合并后减少为 M 条 PolyCurve（M < N）。
    """

    def name(self) -> str:
        return "Join"

    def commandTitle(self) -> str:
        return "组合"

    def prompt(self) -> str:
        return "提示信息显示区"

    def showSnapHints(self) -> bool:
        return False

    def start(self) -> None:
        if self._doc is None:
            self.finish()
            return
        sel = self._doc.selectedObjects()
        if not sel:
            self._manager.notifyResult("组合：请先选中要合并的对象")
            self.finish()
            return
        results = joinObjects(sel)
        if not results:
            self._manager.notifyResult("组合：未发现端点重合的可合并曲线")
            self.finish()
            return
        self._doc.beginEdit()
        try:
            nNew = 0
            nDel = 0
            for poly, srcs in results:
                self._doc.addObject(poly)
                nNew += 1
                for o in srcs:
                    self._doc.removeObject(o.id())
                    nDel += 1
            self._manager.notifyResult(
                f"已组合：生成 {nNew} 条多段线，移除 {nDel} 个原对象")
        finally:
            self._doc.endEdit()
        self.finish()

    def onMousePress(self, wp, mods) -> None:
        pass

    def onMouseMove(self, wp, mods) -> None:
        pass


# ==================== ClosePath 闭合 ====================

class CloseCommand(CommandInstance):
    """闭合：把选中的开放曲线首末点连接起来（工具栏“闭合”按钮）。

    面板选择连接方式：
    - 直线连接：末点→首点直线段；LINE 原地追加首点
    - 圆弧连接：过首末点的半圆弧，凸向由端部切线平均方向决定
    - 平滑曲线：Catmull-Rom 平滑段，端部切向与原曲线一致（G1 连续）
    LINE 的圆弧/平滑闭合与 ARC/CURVE/NURBS 闭合会升格为 PolyCurve
    （原折线段 + 连接段），替换原对象。
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._panel = None
        self._method = 0
        self._targets = []

    def name(self) -> str:
        return "ClosePath"

    def commandTitle(self) -> str:
        return "闭合"

    def prompt(self) -> str:
        return "选择连接方式：右键/面板按钮确认闭合，Esc 取消"

    def showSnapHints(self) -> bool:
        return False

    def start(self) -> None:
        super().start()   # 进入 WAITING_INPUT 并创建预览图元
        self._method = 0
        if self._doc is None:
            self.finish()
            return
        self._targets = [o for o in self._doc.selectedObjects()
                         if o.type().name in ("LINE", "POLYCURVE", "ARC",
                                              "CURVE", "NURBS_CURVE")]
        if not self._targets:
            self._manager.notifyResult("闭合：请先选中要闭合的曲线")
            self.finish()

    def paramPanel(self):
        if self._panel is None:
            self._panel = CloseParamPanel()
            self._panel.methodChanged.connect(self._onMethod)
            self._panel.confirmed.connect(self._onConfirmed)
            self._panel.cancelRequested.connect(self.cancel)
            self._refreshPreview()
        return self._panel

    # ---------- 面板槽 ----------

    def _onMethod(self, method: int) -> None:
        self._method = method
        self._refreshPreview()

    def _onConfirmed(self) -> None:
        self._apply()
        self.finish()

    def onMousePress(self, wp, mods) -> None:
        pass

    def onMouseMove(self, wp, mods) -> None:
        pass

    def onRightClick(self, wp: Point3D) -> bool:
        # 右键 = 确认闭合（取消走面板“取消”按钮或 Esc）
        self._apply()
        self.finish()
        return True

    def _refreshPreview(self) -> None:
        """按当前连接方式预览所有可闭合对象的连接段（实线）。"""
        path = QPainterPath()
        method = ("line", "arc", "smooth")[self._method]
        for obj in self._targets:
            seg = closingSegmentFor(obj, method)
            if seg is None:
                continue
            pts = seg.get("points", [])
            if not pts:
                continue
            path.moveTo(pts[0][0], pts[0][1])
            for p in pts[1:]:
                path.lineTo(p[0], p[1])
        self._setPreviewPath(path, solid=True)

    # ---------- 执行 ----------

    def _promoteToPoly(self, obj, closingSeg: dict) -> PolyCurveObject:
        """对象升格为 PolyCurve：原段 + 闭合连接段，继承外观。"""
        poly = PolyCurveObject()
        for s in _objToSegments(obj):
            poly.addSegment(s)
        poly.addSegment(closingSeg)
        poly._color = obj._color
        poly._lineWidth = obj._lineWidth
        poly._layerId = obj.layerId()
        return poly

    def _apply(self) -> None:
        method = ("line", "arc", "smooth")[self._method]
        nIn = nNew = nSkip = 0
        self._doc.beginEdit()
        try:
            for obj in self._targets:
                t = obj.type().name
                if t == "LINE":
                    pts = obj.points()
                    if len(pts) < 2 or pts[0].distanceTo(pts[-1]) < 1e-6:
                        nSkip += 1
                        continue
                    seg = closingSegmentFor(obj, method)
                    if seg is None:
                        nSkip += 1
                        continue
                    if method == "line":
                        # 原地追加首点（新实例，避免变换时重复作用同一引用）
                        pts.append(Point3D(pts[0].x, pts[0].y, pts[0].z))
                        self._doc.notifyObjectChanged(obj.id())
                        nIn += 1
                    else:
                        self._doc.addObject(self._promoteToPoly(obj, seg))
                        self._doc.removeObject(obj.id())
                        nNew += 1
                elif t == "POLYCURVE":
                    seg = closingSegmentFor(obj, method)
                    if seg is None:
                        nSkip += 1
                        continue
                    obj.addSegment(seg)
                    self._doc.notifyObjectChanged(obj.id())
                    nIn += 1
                else:  # ARC / CURVE / NURBS_CURVE → 转 PolyCurve 后闭合
                    seg = closingSegmentFor(obj, method)
                    if seg is None:
                        nSkip += 1
                        continue
                    self._doc.addObject(self._promoteToPoly(obj, seg))
                    self._doc.removeObject(obj.id())
                    nNew += 1
        finally:
            self._doc.endEdit()
        if nIn + nNew:
            parts = []
            if nIn:
                parts.append(f"原地闭合 {nIn} 条")
            if nNew:
                parts.append(f"转换闭合 {nNew} 条（升格为多段线）")
            if nSkip:
                parts.append(f"跳过 {nSkip} 条（已闭合或不支持）")
            self._manager.notifyResult("闭合完成：" + "，".join(parts))
        else:
            self._manager.notifyResult("闭合：所选曲线均已闭合或不支持闭合")


# ==================== Mirror 镜像 ====================

class MirrorCommand(CommandInstance):
    """镜像命令：点选两点定义镜像轴，对选中对象生成镜像副本。

    交互流程：
    1. 命令启动：取当前选中对象作为镜像对象集；若无选中则等待选对象
    2. 点选第一点（镜像轴起点）
    3. 点选第二点（镜像轴终点）；移动时实时预览镜像结果
    4. 回车/右键/面板“确认执行”提交；Esc 取消
    选项：删除原图（默认不勾选）
    """

    def name(self) -> str:
        return "Mirror"

    def commandTitle(self) -> str:
        return "镜像参数"

    def start(self) -> None:
        super().start()
        self._selectedObjects: List = []
        self._p1: Optional[Point3D] = None
        self._p2: Optional[Point3D] = None
        self._cursor: Optional[Point3D] = None
        self._msg: str = ""
        self._panel: Optional[MirrorParamPanel] = None
        if self._doc is not None:
            objs = self._doc.selectedObjects()
            if objs:
                self._selectedObjects = objs

    def prompt(self) -> str:
        if self._msg:
            return self._msg
        if not self._selectedObjects:
            return "选择要镜像的对象（或已有选中直接进入）"
        if self._p1 is None:
            return "点选镜像轴第一点"
        return "点选镜像轴第二点（移动预览），右键/回车确认"

    def paramPanel(self):
        if self._panel is None:
            self._panel = MirrorParamPanel()
            self._panel.paramsChanged.connect(self._updatePreview)
            self._panel.confirmed.connect(self._onConfirmed)
            self._panel.cancelRequested.connect(self.cancel)
        return self._panel

    # ---------- 面板槽 ----------

    def _onConfirmed(self) -> None:
        self._executeMirror()

    # ---------- 预览 ----------

    def _updatePreview(self) -> None:
        if not self._selectedObjects or self._p1 is None:
            self._setPreviewPath(QPainterPath())
            return
        p2 = self._cursor if self._cursor is not None else self._p1
        from ui.view_2d.item_factory import _buildPath
        path = QPainterPath()
        for obj in self._selectedObjects:
            mirrored = mirrorObject(obj, self._p1, p2)
            if mirrored is not None:
                path.addPath(_buildPath(mirrored))
        # 镜像轴虚线
        axis = QPainterPath()
        axis.moveTo(self._p1.x, self._p1.y)
        axis.lineTo(p2.x, p2.y)
        path.addPath(axis)
        self._setPreviewPath(path)

    # ---------- 交互 ----------

    def onMousePress(self, wp: Point3D, mods) -> None:
        self._msg = ""
        # 若无选中对象：先尝试点选拾取
        if not self._selectedObjects:
            pick = self._pickObject(wp)
            if pick is None:
                self._msg = "未拾取到对象，请点击要镜像的曲线"
                return
            self._selectedObjects = [pick]
            if self._manager is not None:
                self._manager.promptChanged.emit(self.prompt())
            return
        # 已有选中对象：点选镜像轴
        if self._p1 is None:
            self._p1 = wp
            self._cursor = wp
            self._setCursorMarker(wp)
            if self._manager is not None:
                self._manager.promptChanged.emit(self.prompt())
            return
        # 第二点 → 立即执行（受正交约束）
        self._p2 = self._orthoPoint(wp, self._p1)
        self._executeMirror()

    def onMouseMove(self, wp: Point3D, mods) -> None:
        if self._panel is not None:
            self._panel.setXYZ(wp.x, wp.y, wp.z)
        if self._p1 is not None:
            # 镜像轴第二点受正交约束（水平/垂直）
            wp = self._orthoPoint(wp, self._p1)
        self._cursor = wp
        if self._p1 is None:
            return
        self._setCursorMarker(wp)
        self._updatePreview()

    def onRightClick(self, wp: Point3D) -> bool:
        if self._p1 is not None and self._p2 is None:
            # 已点第一点、未确认：用当前光标位置作为第二点执行（受正交约束）
            self._p2 = self._orthoPoint(wp, self._p1)
            self._executeMirror()
            return True
        self.finish()
        return True

    def onEnter(self) -> bool:
        if self._p1 is not None and self._p2 is None and self._cursor is not None:
            self._p2 = self._cursor
            self._executeMirror()
            return True
        return False

    # ---------- 执行 ----------

    def _executeMirror(self) -> None:
        if not self._selectedObjects:
            self._msg = "请先选中要镜像的对象"
            return
        if self._p1 is None or self._p2 is None:
            self._msg = "请点选两点定义镜像轴"
            return
        if self._p1.distanceTo(self._p2) < 1e-9:
            self._msg = "镜像轴两点重合，请重新点选"
            self._p2 = None
            return
        deleteOrig = (self._panel is not None and self._panel.deleteOriginal())
        if self._doc is not None:
            self._doc.beginEdit()
            try:
                for obj in self._selectedObjects:
                    mirrored = mirrorObject(obj, self._p1, self._p2)
                    if mirrored is None:
                        continue
                    self._doc.addObject(mirrored)
                    if deleteOrig:
                        self._doc.removeObject(obj.id())
                self._manager.notifyResult(
                    f"已镜像 {len(self._selectedObjects)} 个对象"
                    + ("，原对象已删除" if deleteOrig else "，保留原对象"))
            finally:
                self._doc.endEdit()
        self._msg = "镜像完成"
        self.finish()

    # ---------- 辅助 ----------

    def _worldPickRadius(self) -> float:
        scale = abs(self._view.transform().m11()) if self._view is not None else 1.0
        return PICK_PIXEL_RADIUS / max(scale, 1e-6)

    def _pickObject(self, wp: Point3D):
        """拾取鼠标附近最近的对象。"""
        if self._doc is None:
            return None
        worldR = self._worldPickRadius()
        best = None
        bestDist = worldR
        for obj in self._doc.visibleObjects():
            pts = self._getSamplePoints(obj)
            if len(pts) < 2:
                continue
            for i in range(len(pts) - 1):
                near, _ = nearestPointOnSegment(pts[i], pts[i + 1], wp)
                d = near.distanceTo(wp)
                if d <= bestDist:
                    bestDist = d
                    best = obj
        return best

    @staticmethod
    def _getSamplePoints(obj) -> List[Point3D]:
        t = obj.type().name
        if t == "LINE":
            return obj.points()
        if t == "CURVE":
            return obj.getSampledPoints()
        if t in ("CIRCLE", "ELLIPSE", "ARC"):
            poly = obj.toPolycurve()
            pts: List[Point3D] = []
            for seg in poly.segments():
                pts.extend([Point3D(p[0], p[1], p[2]) for p in seg.get("points", [])])
            return pts
        if t == "POLYCURVE":
            out: List[Point3D] = []
            for seg in obj.segments():
                out.extend([Point3D(p[0], p[1], p[2]) for p in seg.get("points", [])])
            return out
        return []


# ==================== Extend 延伸 ====================

class ExtendCommand(CommandInstance):
    """延伸命令：两种边界驱动模式（单击即用）。

    - 快速延伸(0)：直接点曲线 → 靠近点击位置的端自动延伸到
      文档中沿切线射线最近的相交曲线；
    - 选中目标延伸(1)：点曲线 → 点目标边界曲线 → 延伸到与目标交点。
    不再按指定长度延伸；延伸端由点击位置靠近首/尾端自动判定。
    支持 LINE/ARC/CURVE/POLYCURVE/ELLIPSE。
    """

    def name(self) -> str:
        return "Extend"

    def commandTitle(self) -> str:
        return "延伸参数"

    def showSnapHints(self) -> bool:
        return False

    def start(self) -> None:
        super().start()
        self._cursor: Optional[Point3D] = None
        self._msg: str = ""
        self._panel: Optional[ExtendParamPanel] = None
        self._endPick = None              # 目标模式临时记录 (obj, atHead)

    def prompt(self) -> str:
        if self._msg:
            return self._msg
        mode = self._panel.mode() if self._panel else 0
        if mode == 1:
            if self._endPick is not None:
                return "点击目标边界曲线"
            return "点击要延伸的曲线，再点目标边界"
        return "点击要延伸的曲线，较近端自动延伸到最近边界"

    def paramPanel(self):
        if self._panel is None:
            self._panel = ExtendParamPanel()
            self._panel.modeChanged.connect(self._onModeChanged)
            self._panel.cancelRequested.connect(self.cancel)
        return self._panel

    # ---------- 面板槽 ----------

    def _onModeChanged(self, mode: int) -> None:
        self._endPick = None
        self._setPreviewPath(QPainterPath())
        if self._manager is not None:
            self._manager.promptChanged.emit(self.prompt())

    # ---------- 预览 ----------

    def _updatePreview(self) -> None:
        from ui.view_2d.item_factory import _buildPath
        from core.vector.extend_geometry import nearestExtendDistance, extendObject
        path = QPainterPath()
        if self._cursor is not None and self._doc is not None:
            mode = self._panel.mode() if self._panel else 0
            if mode == 0:
                # 快速模式：光标所在曲线，预览较近端延伸到最近边界
                obj = self._pickObject(self._cursor)
                if obj is not None:
                    atHead = self._nearerHead(obj, self._cursor)
                    bounds = [o for o in self._doc.visibleObjects()
                              if o is not obj]
                    dist = nearestExtendDistance(obj, atHead, bounds)
                    if dist is not None and dist > 1e-9:
                        cloned = obj.clone() if hasattr(obj, "clone") else None
                        if cloned is not None:
                            new_obj, _ = extendObject(cloned, atHead, dist)
                            if new_obj is not None:
                                path.addPath(_buildPath(new_obj))
            elif self._endPick is not None:
                # 目标模式：已选延伸曲线，光标在潜在目标上时预览延伸到该目标
                obj, atHead = self._endPick
                target = self._pickTarget(self._cursor, obj)
                if target is not None:
                    dist = nearestExtendDistance(obj, atHead, [target])
                    if dist is not None and dist > 1e-9:
                        cloned = obj.clone() if hasattr(obj, "clone") else None
                        if cloned is not None:
                            new_obj, _ = extendObject(cloned, atHead, dist)
                            if new_obj is not None:
                                path.addPath(_buildPath(new_obj))
        self._setPreviewPath(path)

    # ---------- 交互 ----------

    def onMousePress(self, wp: Point3D, mods) -> None:
        self._msg = ""
        if self._panel is not None:
            self._panel.setXYZ(wp.x, wp.y, wp.z)
        self._cursor = wp
        mode = self._panel.mode() if self._panel else 0
        if mode == 0:
            self._doQuickExtend(wp)
        else:
            self._doTargetStep(wp)

    def onMouseMove(self, wp: Point3D, mods) -> None:
        if self._panel is not None:
            self._panel.setXYZ(wp.x, wp.y, wp.z)
        self._cursor = wp
        self._updatePreview()

    def onRightClick(self, wp: Point3D) -> bool:
        self.finish()
        return True

    def onEnter(self) -> bool:
        self.finish()
        return True

    # ---------- 快速延伸 ----------

    def _doQuickExtend(self, wp: Point3D) -> None:
        obj = self._pickObject(wp)
        if obj is None:
            self._msg = "请点击要延伸的曲线"
            self._emitPrompt()
            return
        atHead = self._nearerHead(obj, wp)
        bounds = [o for o in self._doc.visibleObjects()
                  if o is not obj] if self._doc is not None else []
        from core.vector.extend_geometry import nearestExtendDistance
        dist = nearestExtendDistance(obj, atHead, bounds)
        if dist is None or dist < 1e-9:
            self._msg = "未找到延伸边界"
        else:
            self._doExtend(obj, atHead, dist)
            self._msg = "延伸完成"
        self._emitPrompt()

    # ---------- 目标延伸 ----------

    def _doTargetStep(self, wp: Point3D) -> None:
        if self._endPick is None:
            obj = self._pickObject(wp)
            if obj is None:
                self._msg = "请点击要延伸的曲线"
            else:
                self._endPick = (obj, self._nearerHead(obj, wp))
                self._msg = ""
        else:
            obj, atHead = self._endPick
            target = self._pickTarget(wp, obj)
            if target is None:
                self._msg = "请点击目标边界曲线"
            else:
                from core.vector.extend_geometry import nearestExtendDistance
                dist = nearestExtendDistance(obj, atHead, [target])
                if dist is None or dist < 1e-9:
                    self._msg = "延伸方向与目标无交点"
                else:
                    self._doExtend(obj, atHead, dist)
                    self._msg = "延伸完成"
                # 回到点曲线状态，支持连续操作
                self._endPick = None
        self._emitPrompt()

    # ---------- 执行 ----------

    def _doExtend(self, obj, atHead: bool, dist: float) -> None:
        from core.vector.extend_geometry import extendObject
        if self._doc is None:
            return
        self._doc.beginEdit()
        try:
            new_obj, replaced = extendObject(obj, atHead, dist)
            if new_obj is None:
                return
            if replaced:
                self._doc.removeObject(obj.id())
                self._doc.addObject(new_obj)
            else:
                self._doc.notifyObjectChanged(obj.id())
            if self._manager is not None:
                self._manager.notifyResult("已延伸 1 个对象")
        finally:
            self._doc.endEdit()

    # ---------- 辅助 ----------

    def _worldPickRadius(self) -> float:
        scale = abs(self._view.transform().m11()) if self._view is not None else 1.0
        return PICK_PIXEL_RADIUS / max(scale, 1e-6)

    def _pickObject(self, wp: Point3D):
        """拾取鼠标附近最近的对象。"""
        if self._doc is None:
            return None
        worldR = self._worldPickRadius()
        best = None
        bestDist = worldR
        for obj in self._doc.visibleObjects():
            pts = self._getSamplePoints(obj)
            if len(pts) < 2:
                continue
            for i in range(len(pts) - 1):
                near, _ = nearestPointOnSegment(pts[i], pts[i + 1], wp)
                d = near.distanceTo(wp)
                if d <= bestDist:
                    bestDist = d
                    best = obj
        return best

    def _nearerHead(self, obj, wp: Point3D) -> bool:
        """点击位置靠近首端返回 True（延伸首端），否则延伸尾端。"""
        pts = self._getSamplePoints(obj)
        if len(pts) < 2:
            return False
        return wp.distanceTo(pts[0]) <= wp.distanceTo(pts[-1])

    def _emitPrompt(self) -> None:
        if self._manager is not None:
            self._manager.promptChanged.emit(self.prompt())

    def _pickTarget(self, wp: Point3D, exclude):
        """拾取鼠标附近最近的对象（排除 exclude），用于目标边界选择。"""
        if self._doc is None:
            return None
        worldR = self._worldPickRadius()
        best = None
        bestDist = worldR
        for obj in self._doc.visibleObjects():
            if obj is exclude:
                continue
            pts = self._getSamplePoints(obj)
            if len(pts) < 2:
                continue
            for i in range(len(pts) - 1):
                near, _ = nearestPointOnSegment(pts[i], pts[i + 1], wp)
                d = near.distanceTo(wp)
                if d <= bestDist:
                    bestDist = d
                    best = obj
        return best

    @staticmethod
    def _getSamplePoints(obj) -> List[Point3D]:
        t = obj.type().name
        if t == "LINE":
            return obj.points()
        if t == "CURVE":
            return obj.getSampledPoints()
        if t in ("CIRCLE", "ELLIPSE", "ARC"):
            poly = obj.toPolycurve()
            pts: List[Point3D] = []
            for seg in poly.segments():
                pts.extend([Point3D(p[0], p[1], p[2]) for p in seg.get("points", [])])
            return pts
        if t == "POLYCURVE":
            out: List[Point3D] = []
            for seg in obj.segments():
                out.extend([Point3D(p[0], p[1], p[2]) for p in seg.get("points", [])])
            return out
        return []


# ==================== Outline 外轮廓 ====================

class OutlineCommand(CommandInstance):
    """外轮廓命令（移植自 Rhino 插件 ToolPage.ShowOuterOutline）。

    选中曲线 → 弹参数面板（偏移距离/重建去密集点/最大点数）→ 实时预览
    包裹全部曲线的单一外轮廓 → 确定/右键生成闭合折线。
    算法：闭合曲线外偏 + 开放曲线包裹带 → Clipper 布尔并集 → 去内孔 →
    多区域（彼此隔开）时用全部采样点凸包外偏兜底。
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._panel = None
        self._targets = []

    def name(self) -> str:
        return "Outline"

    def commandTitle(self) -> str:
        return "外轮廓"

    def prompt(self) -> str:
        return "设置偏移距离，预览确认后右键/确定生成包裹线，Esc 取消"

    def showSnapHints(self) -> bool:
        return False

    def start(self) -> None:
        super().start()   # 进入 WAITING_INPUT 并创建预览图元
        if self._doc is None:
            self.finish()
            return
        ok = ("LINE", "POLYCURVE", "ARC", "CIRCLE", "ELLIPSE",
              "CURVE", "NURBS_CURVE")
        self._targets = [o for o in self._doc.selectedObjects()
                         if o.type().name in ok]
        if not self._targets:
            self._manager.notifyResult("外轮廓：请先选中曲线")
            self.finish()

    def paramPanel(self):
        if self._panel is None:
            self._panel = OutlineParamPanel()
            self._panel.paramsChanged.connect(self._refreshPreview)
            self._panel.confirmed.connect(self._onConfirmed)
            self._panel.cancelRequested.connect(self.cancel)
            self._refreshPreview()
        return self._panel

    # ---------- 面板槽 ----------

    def _onConfirmed(self) -> None:
        if self._apply():
            self.finish()

    def onMousePress(self, wp, mods) -> None:
        pass

    def onMouseMove(self, wp, mods) -> None:
        pass

    def onRightClick(self, wp: Point3D) -> bool:
        # 右键 = 确认生成（取消走面板"取消"按钮或 Esc）
        if self._apply():
            self.finish()
        return True

    # ---------- 计算与预览 ----------

    def _splitCurves(self):
        """选中对象 → (闭合采样点列, 开放采样点列)。"""
        closedPts: List[List[Point3D]] = []
        openPts: List[List[Point3D]] = []
        for obj in self._targets:
            pts = self._getSamplePoints(obj)
            if len(pts) < 2:
                continue
            try:
                flagC = bool(obj.isClosed())   # LINE/POLYCURVE 有该标志
            except Exception:
                flagC = False
            if flagC or pts[0].distanceTo(pts[-1]) < 1e-6:
                closedPts.append(sampleClosed(pts))
            else:
                openPts.append(sampleOpen(pts))
        return closedPts, openPts

    def _compute(self) -> List[List[Point3D]]:
        dist = self._panel.distance() if self._panel is not None else 2.0
        closedPts, openPts = self._splitCurves()
        return computeOuterOutline(closedPts, openPts, dist)

    def _refreshPreview(self) -> None:
        """实时预览包裹线（实线）。"""
        path = QPainterPath()
        for pts in self._compute():
            if len(pts) < 3:
                continue
            path.moveTo(pts[0][0], pts[0][1])
            for p in pts[1:]:
                path.lineTo(p[0], p[1])
            path.closeSubpath()
        self._setPreviewPath(path, solid=True)

    # ---------- 执行 ----------

    def _apply(self) -> bool:
        if self._doc is None:
            return False
        results = self._compute()
        if not results:
            self._manager.notifyResult("外轮廓：未能生成包裹线（请检查选中曲线与距离）")
            return False
        rebuild = self._panel.rebuild() if self._panel is not None else True
        tol = self._panel.tolerance() if self._panel is not None else 0.01
        self._doc.beginEdit()
        try:
            for pts in results:
                if rebuild:
                    pts = simplifyPolyline(pts, tol)
                line = LineObject(pts)
                line.setClosed(True)
                self._doc.addObject(line)
        finally:
            self._doc.endEdit()
        dist = self._panel.distance() if self._panel is not None else 0.0
        self._manager.notifyResult(
            "外轮廓：已生成 %d 条包裹线（距离 %.3f）" % (len(results), dist))
        return True

    @staticmethod
    def _getSamplePoints(obj) -> List[Point3D]:
        t = obj.type().name
        if t == "LINE":
            return obj.points()
        if t in ("CURVE", "NURBS_CURVE"):
            return obj.getSampledPoints()
        if t in ("CIRCLE", "ELLIPSE", "ARC"):
            poly = obj.toPolycurve()
            pts: List[Point3D] = []
            for seg in poly.segments():
                pts.extend([Point3D(p[0], p[1], p[2]) for p in seg.get("points", [])])
            return pts
        if t == "POLYCURVE":
            out: List[Point3D] = []
            for seg in obj.segments():
                out.extend([Point3D(p[0], p[1], p[2]) for p in seg.get("points", [])])
            return out
        return []


# ==================== Align 对齐 ====================

def _unionBBox(a: BoundingBox3D, b: BoundingBox3D) -> BoundingBox3D:
    """两个包围盒的并集（对齐单元 = 群组成员联合包围盒）。"""
    return BoundingBox3D(
        Point3D(min(a.min.x, b.min.x), min(a.min.y, b.min.y), min(a.min.z, b.min.z)),
        Point3D(max(a.max.x, b.max.x), max(a.max.y, b.max.y), max(a.max.z, b.max.z)))


class AlignCommand(CommandInstance):
    """对齐命令（工具栏"对齐"按钮）：上/下/左/右/居中五种对齐 + 均匀分布。

    - 选中 >=2 个对象：后选的（最后被 setSelected 的）为基准不动，其余对齐到基准；
    - 只选中 1 个：点对齐按钮后进入拾取模式，在画布上点击哪个对象就以它为基准；
    - 未选中：提示并结束。
    群组作为整体参与：编组成员合并为一个对齐单元（联合包围盒判定位置，
    移动时整组一起平移，组内相对位置不变）。
    对齐语义（包围盒）：上=顶边相接，下=底边相接，左=左边相接，右=右边相接，
    居中=包围盒中心重合。
    """

    _LABELS = {"top": "上", "bottom": "下", "left": "左",
               "right": "右", "center": "居中"}

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._panel = None
        self._targets = []
        self._pendingMode = None   # 拾取基准模式：待执行的对齐方式

    def name(self) -> str:
        return "Align"

    def commandTitle(self) -> str:
        return "对齐"

    def prompt(self) -> str:
        if self._pendingMode is not None:
            return "请在画布上点击作为基准的对象（%s对齐），右键取消" % (
                self._LABELS[self._pendingMode])
        return "选中2个以上对象点对齐按钮（后选的为基准）；或选中1个后点按钮再点基准对象"

    def showSnapHints(self) -> bool:
        return False

    def start(self) -> None:
        super().start()
        if self._doc is None:
            self.finish()
            return
        self._targets = list(self._doc.selectedObjects())
        if not self._targets:
            self._manager.notifyResult("对齐：请先选中对象")
            self.finish()

    def paramPanel(self):
        if self._panel is None:
            self._panel = AlignParamPanel()
            self._panel.alignRequested.connect(self._onAlign)
            self._panel.distributeRequested.connect(self._onDistribute)
            self._panel.cancelRequested.connect(self.cancel)
        return self._panel

    # ---------- 对齐单元（群组整体） ----------

    def _units(self):
        """把选中对象划分为对齐单元：同群组成员合并为整体。

        返回 [(seq, members, bbox)]（按 seq 升序）：
        seq = 单元内最大选择序号（后选判定）；members = 单元成员列表；
        bbox = 成员包围盒的并集。"""
        groups = {}
        singles = []
        for o in self._targets:
            gid = o.group()
            if gid:
                groups.setdefault(gid, []).append(o)
            else:
                singles.append([o])
        out = []
        for members in list(groups.values()) + singles:
            bbox = None
            seq = 0
            for m in members:
                b = m.getBoundingBox()
                if b is not None and b.isValid():
                    bbox = b if bbox is None else _unionBBox(bbox, b)
                seq = max(seq, m.selectionSeq())
            if bbox is not None:
                out.append((seq, members, bbox))
        out.sort(key=lambda t: t[0])
        return out

    # ---------- 执行 ----------

    def _onAlign(self, mode: str) -> None:
        units = self._units()
        if len(units) >= 2:
            base = units[-1]                 # 后选的单元为基准
            self._alignTo(mode, units[:-1], base)
            self.finish()
        elif len(units) == 1:
            # 只有 1 个：进入拾取基准模式（在画布上点击基准对象）
            self._pendingMode = mode
            self._emitPrompt()
        else:
            self._manager.notifyResult("对齐：请先选中对象")
            self.finish()

    def _onDistribute(self) -> None:
        """均匀分布：按面板勾选的 X/Y 轴把选中单元等间距排布（两端固定）。"""
        axes = []
        if self._panel is not None:
            if self._panel.xUniform():
                axes.append("x")
            if self._panel.yUniform():
                axes.append("y")
        if not axes:
            self._manager.notifyResult("分布：请先勾选 X 均匀 / Y 均匀")
            return
        units = self._units()
        if len(units) < 3:
            self._manager.notifyResult("分布：至少选中 3 个对象/群组才能均匀分布")
            return
        moved = 0
        self._doc.beginEdit()
        try:
            for axis in axes:
                moved += self._distributeAxis(axis, units)
        finally:
            self._doc.endEdit()
        # 通知选择变化 → 操作轴跟随新位置
        if self._doc is not None:
            self._doc.selectionChanged.emit()
        axesLabel = "+".join(a.upper() for a in axes)
        self._manager.notifyResult("分布：%s 均匀分布完成（移动 %d 个对象）" % (axesLabel, moved))
        self.finish()

    def onMousePress(self, wp, mods) -> None:
        if self._pendingMode is None:
            return
        base = self._pickBase(wp)
        if base is None:
            self._manager.notifyResult("对齐：未点到基准对象，请重新点击")
            return
        mode = self._pendingMode
        self._pendingMode = None
        self._alignTo(mode, self._units(), (0, base[0], base[1]))
        self.finish()

    def onMouseMove(self, wp, mods) -> None:
        pass

    def onRightClick(self, wp: Point3D) -> bool:
        if self._pendingMode is not None:
            # 拾取基准模式下右键 = 回到面板
            self._pendingMode = None
            self._emitPrompt()
            return True
        return False

    # ---------- 内部 ----------

    def _emitPrompt(self) -> None:
        if self._manager is not None:
            self._manager.promptChanged.emit(self.prompt())

    def _worldPickRadius(self) -> float:
        scale = abs(self._view.transform().m11()) if self._view is not None else 1.0
        return PICK_PIXEL_RADIUS / max(scale, 1e-6)

    def _pickBase(self, wp: Point3D):
        """拾取鼠标附近的文档对象作为基准（排除已选中的对象本身）。

        点到群组成员时基准为整组：返回 (members, bbox)，bbox 为成员联合包围盒。"""
        if self._doc is None:
            return None
        worldR = self._worldPickRadius()
        best = None
        bestDist = worldR
        for obj in self._doc.visibleObjects():
            if obj.isSelected():
                continue
            pts = OutlineCommand._getSamplePoints(obj)
            if len(pts) < 2:
                continue
            for i in range(len(pts) - 1):
                near, _ = nearestPointOnSegment(pts[i], pts[i + 1], wp)
                d = near.distanceTo(wp)
                if d <= bestDist:
                    bestDist = d
                    best = obj
        if best is None:
            return None
        gid = best.group()
        members = ([o for o in self._doc.objects() if o.group() == gid]
                   if gid else [best])
        bbox = None
        for m in members:
            b = m.getBoundingBox()
            if b is not None and b.isValid():
                bbox = b if bbox is None else _unionBBox(bbox, b)
        return (members, bbox)

    def _alignTo(self, mode: str, units, baseUnit) -> None:
        """把各对齐单元按 mode 对齐到 baseUnit（基准不动），一次撤销。

        units/baseUnit 均为 (seq, members, bbox)；群组单元整体平移。"""
        _seq, baseMembers, bb = baseUnit
        if bb is None or not bb.isValid():
            self._manager.notifyResult("对齐：基准对象无有效包围盒")
            return
        bc = bb.center()
        label = self._LABELS[mode]
        moved = 0
        self._doc.beginEdit()
        try:
            for _seq, members, mb in units:
                if mb is None or not mb.isValid():
                    continue
                mc = mb.center()
                dx = dy = 0.0
                if mode == "top":
                    dy = bb.max.y - mb.max.y
                elif mode == "bottom":
                    dy = bb.min.y - mb.min.y
                elif mode == "left":
                    dx = bb.min.x - mb.min.x
                elif mode == "right":
                    dx = bb.max.x - mb.max.x
                else:   # center
                    dx = bc.x - mc.x
                    dy = bc.y - mc.y
                if abs(dx) < 1e-9 and abs(dy) < 1e-9:
                    continue
                for o in members:   # 群组整体平移，组内相对位置不变
                    o.translate(dx, dy, 0.0)
                    self._doc.notifyObjectChanged(o.id())
                moved += 1
        finally:
            self._doc.endEdit()
        # 通知选择变化 → 操作轴跟随新位置
        if moved and self._doc is not None:
            self._doc.selectionChanged.emit()
        self._manager.notifyResult("对齐：%s对齐完成（移动 %d 个对象）" % (label, moved))

    def _distributeAxis(self, axis: str, units) -> int:
        """沿 axis（"x"/"y"）把对齐单元中心等间距排布：两端单元固定，中间等差。
        返回移动的对象数。"""
        items = []
        for u in units:
            c = u[2].center()
            items.append((c.x if axis == "x" else c.y, u))
        items.sort(key=lambda t: t[0])
        lo = items[0][0]
        step = (items[-1][0] - lo) / (len(items) - 1)
        moved = 0
        for i, (pos, (_seq, members, _mb)) in enumerate(items):
            if i == 0 or i == len(items) - 1:
                continue   # 两端固定
            d = lo + i * step - pos
            if abs(d) < 1e-9:
                continue
            for o in members:
                if axis == "x":
                    o.translate(d, 0.0, 0.0)
                else:
                    o.translate(0.0, d, 0.0)
                self._doc.notifyObjectChanged(o.id())
            moved += len(members)
        return moved


# ==================== PanelMove 面板移动（两点平移） ====================

class _PanelXformBase(CommandInstance):
    """面板变换命令共用基类：取选中对象、**实时改真实对象做预览**（与操作轴
    gizmo 同一机制），beginEdit/endEdit 事务包一条撤销记录，cancelEdit 回滚。

    预览机制（与 gizmo 对齐）：
    - 进入到「可以拖动预览」的状态时调 ``_startLiveEdit()`` 开启事务
    - 每次鼠标移动：先 ``_invertPrev()`` 撤销上一次预览对真实对象的几何改动，
      再 ``_applyNew(delta)`` 按新参数改真实几何；场景下的 2D 图元自动重绘，
      **图片、曲线、标注、群组**全部是「自己」的样子，不需要 clone 渲染管线。
    - 确认：``_commitLiveEdit()`` 提交事务，结束命令
    - 取消：``_cancelLiveEdit()`` 回滚事务，对象回到命令前的状态
    """

    def start(self) -> None:
        super().start()
        self._selectedObjects: List = []
        self._msg: str = ""
        if self._doc is not None:
            objs = self._doc.selectedObjects()
            if objs:
                self._selectedObjects = [o for o in objs if not o.isLocked()]
        # 实时预览状态
        self._editActive: bool = False          # doc.beginEdit() 是否已调用（endEdit 前保持 True）
        self._prevDelta: Optional[tuple] = None # 上一次预览参数（用于反向抵消）
        self._editKind: str = ""                # "move" / "rotate" / "scale"
        # 每个对象进入变换阶段时的几何快照（obj.id() → obj.serialize()），
        # 取消时用 deserialize 直接回到命令前的状态，避免 N 次反向 translate 的浮点误差。
        self._geoSnap: dict = {}

    # ---------- 选中 ----------

    def _unlockedSelected(self) -> List:
        return [o for o in self._selectedObjects if not o.isLocked()]

    def _ensureSelectionFromPick(self, wp: Point3D) -> bool:
        """若无选中对象：尝试在点击位置拾取；返回 True 表示已有对象。"""
        if self._selectedObjects:
            return True
        pick = self._pickObject(wp)
        if pick is None:
            self._msg = "未拾取到对象，请点击要操作的曲线"
            if self._manager is not None:
                self._manager.promptChanged.emit(self.prompt())
            return False
        self._selectedObjects = [pick]
        if self._doc is not None:
            self._doc.selectObject(pick.id())
        if self._manager is not None:
            self._manager.promptChanged.emit(self.prompt())
        return True

    # ---------- 预览：clone→路径（早期阶段仍可用于参考轮廓绘制） ----------

    def _buildOriginalPath(self) -> QPainterPath:
        from ui.view_2d.item_factory import _buildPath
        path = QPainterPath()
        for obj in self._unlockedSelected():
            path.addPath(_buildPath(obj))
        return path

    def _buildTransformedPath(self, xformFn):
        """早期（点 P1/P2 之前）可能仍需轮廓预览用；返回
        ``(合并后的克隆绘制路径: QPainterPath, 图片克隆对象列表: List[ImageObject])``。"""
        from ui.view_2d.item_factory import _buildPath
        from core.vector.image_object import ImageObject
        path = QPainterPath()
        imageClones: list = []
        for obj in self._unlockedSelected():
            try:
                clone = obj.clone()
            except Exception:
                continue
            try:
                xformFn(clone)
            except Exception:
                pass
            path.addPath(_buildPath(clone))
            if isinstance(clone, ImageObject):
                try:
                    pm = clone.pixmap()
                    if not pm.isNull():
                        imageClones.append(clone)
                except Exception:
                    pass
        return (path, imageClones)

    # ---------- 实时预览事务（与操作轴 gizmo 同模式） ----------
    # Document API 约定（见 core/document.py beginEdit/endEdit，均为零参数）：
    #   - beginEdit：记录两个域的 before 全量快照（曲线+浮雕），可嵌套不重复快照
    #   - endEdit：比较 before vs 当前，对有变化的域压入一条撤销记录；无变化忽略并清空 _editBefore
    #   - 不存在 cancelEdit：取消时需要「先手动恢复每个对象到进入阶段时的几何快照」→
    #     再调用 endEdit，此时 before == after → 不压栈且清空 _editBefore。

    def _snapGeometry(self) -> None:
        """记录当前未锁定选中对象的几何快照（必须在开 beginEdit 之前调用，
        或在 beginEdit 第一次内部快照之后调用，保证取消时 deserialize 回
        到 beginEdit 之前的对象状态）。"""
        self._geoSnap = {}
        for o in self._unlockedSelected():
            try:
                self._geoSnap[o.id()] = o.serialize()
            except Exception:
                pass

    def _restoreGeometryFromSnap(self) -> None:
        """取消用：按 _geoSnap 把每个对象的几何 + 颜色/群组/图层 完全还原。
        还原后 _prevDelta 清空（因为之后不需要再反向 translate 累积变换）。"""
        if not self._geoSnap:
            return
        for o in self._unlockedSelected():
            snap = self._geoSnap.get(o.id())
            if snap is None:
                continue
            try:
                o.deserialize(snap)
            except Exception:
                continue
        self._prevDelta = None

    def _startLiveEdit(self, kind: str) -> None:
        """开启变换预览：doc.beginEdit() + 快照 + 标记 editActive。

        调用时机：
        - 移动：点完 P1 之后
        - 旋转：点完 P2 之后
        - 缩放：点完 P2 之后
        """
        if self._editActive:
            return
        if self._doc is None or not self._unlockedSelected():
            return
        # 先快照（保证取消时能完整恢复），再调 beginEdit（beginEdit 内部 also 做快照，
        # 两者是不同层次：我们的 snap 是「取消恢复对象几何」的独立保险；
        # beginEdit 的快照是 Document 级别撤销栈的 before 参照）。
        self._snapGeometry()
        try:
            self._doc.beginEdit()
        except Exception:
            return
        self._editActive = True
        self._prevDelta = None
        self._editKind = kind

    def _invertPrev(self) -> None:
        """根据 _prevDelta 撤销上一次预览对真实对象的改动。"""
        if not self._editActive or self._prevDelta is None:
            return
        delta = self._prevDelta
        objs = self._unlockedSelected()
        if not objs:
            return
        if self._editKind == "move":
            dx, dy, dz = delta
            for o in objs:
                o.translate(-dx, -dy, -dz)
        elif self._editKind == "rotate":
            angle, cx, cy, cz = delta
            c = Point3D(cx, cy, cz)
            for o in objs:
                o.rotateAround(-angle, c)
        elif self._editKind == "scale":
            k, cx, cy, cz = delta
            if abs(k) > 1e-12:
                inv = 1.0 / k
                c = Point3D(cx, cy, cz)
                for o in objs:
                    o.scaleAround(inv, inv, inv, c)

    def _applyDelta(self, delta) -> None:
        """按新 delta 对真实对象施加变换（调用方先 _invertPrev）。

        每次改完对象几何立刻发 objectChanged 信号，确保 2D 场景的曲线图元
        重新计算 path 并重绘（图片项 paint 直接读 obj 属性，不需要信号也能
        刷新，但统一 notify 不浪费）。"""
        objs = self._unlockedSelected()
        if not objs:
            return
        if self._editKind == "move":
            dx, dy, dz = delta
            for o in objs:
                o.translate(dx, dy, dz)
        elif self._editKind == "rotate":
            angle, cx, cy, cz = delta
            c = Point3D(cx, cy, cz)
            for o in objs:
                o.rotateAround(angle, c)
        elif self._editKind == "scale":
            k, cx, cy, cz = delta
            c = Point3D(cx, cy, cz)
            for o in objs:
                o.scaleAround(k, k, k, c)
        # 通知 2D/3D 场景：这些对象几何变了，该重画/重算 path
        if self._doc is not None:
            for o in objs:
                try:
                    self._doc.notifyObjectChanged(o.id())
                except Exception:
                    pass

    def _updateLive(self, newDelta) -> None:
        """「反向旧 delta → 应用新 delta」的一帧预览刷新。
        刷新后：_prevDelta 存新 delta。"""
        if not self._editActive:
            return
        self._invertPrev()
        self._applyDelta(newDelta)
        self._prevDelta = newDelta

    def _commitLiveEdit(self) -> None:
        """确认结束：endEdit 提交（before≠after → 自动压一条撤销记录）。"""
        if not self._editActive:
            return
        self._prevDelta = None
        self._geoSnap.clear()
        try:
            if self._doc is not None:
                self._doc.endEdit()
        except Exception:
            pass
        self._editActive = False

    def _cancelLiveEdit(self) -> None:
        """取消：先还原对象几何到快照 → 再 endEdit 让 Document 比较前后一致
        → 不压撤销记录且清空内部的 _editBefore 标记。"""
        if not self._editActive:
            # 没开事务：之前可能还在 P1/P2 拾取阶段就 cancel，什么都不用做
            self._geoSnap.clear()
            if self._prevDelta is not None:
                self._invertPrev()
                self._prevDelta = None
            return
        self._restoreGeometryFromSnap()
        self._geoSnap.clear()
        try:
            if self._doc is not None:
                self._doc.endEdit()   # before == after → 不压栈，清空 _editBefore
        except Exception:
            pass
        self._editActive = False

    def cancel(self) -> None:
        self._cancelLiveEdit()
        super().cancel()

    def finish(self) -> None:
        # 防御：finish 时若事务仍挂着（意外路径），提交而不是回滚。
        if self._editActive:
            self._commitLiveEdit()
        super().finish()

    # ---------- gizmo 刷新 ----------

    def _notifyGizmoRefresh(self) -> None:
        """命令改完对象几何后：发 selectionChanged 通知 gizmo 重算中心与包围盒。"""
        if self._doc is not None:
            try:
                self._doc.selectionChanged.emit()
            except RuntimeError:
                pass

    # ---------- 辅助（借用 MirrorCommand 同文件实现）----------

    def _worldPickRadius(self) -> float:
        scale = abs(self._view.transform().m11()) if self._view is not None else 1.0
        return PICK_PIXEL_RADIUS / max(scale, 1e-6)

    def _pickObject(self, wp: Point3D):
        if self._doc is None:
            return None
        worldR = self._worldPickRadius()
        best = None
        bestDist = worldR
        for obj in self._doc.visibleObjects():
            pts = MirrorCommand._getSamplePoints(obj)
            if len(pts) < 2:
                # IMAGE / DIMENSION 等 _getSamplePoints 未覆盖的对象：
                # 尝试使用 points() 作为命中折线（四角矩形、标注起止点）
                if hasattr(obj, "points") and callable(getattr(obj, "points", None)):
                    try:
                        pts = list(obj.points())
                    except Exception:
                        pts = []
            if len(pts) < 2:
                continue
            # 闭合矩形（图片）：回到起点的边也要参与命中
            closedImg = (len(pts) == 4 and getattr(obj, "type", lambda: None)()
                         == ObjectType.IMAGE)
            segs = len(pts) - 1
            for i in range(segs + (1 if closedImg else 0)):
                a = pts[i]
                b = pts[(i + 1) % len(pts)]
                near, _ = nearestPointOnSegment(a, b, wp)
                d = near.distanceTo(wp)
                if d <= bestDist:
                    bestDist = d
                    best = obj
        return best

    def showSnapHints(self) -> bool:
        return True


class PanelMoveCommand(_PanelXformBase):
    """面板移动：选中对象 → 点基准点 → 点目标点（拖动预览；右键/回车确认）。

    与操作轴 gizmo 同预览机制：在 beginEdit 事务内实时修改真实对象的几何，
    场景下的图片、曲线、标注、群组 2D 图元自动重绘 = 「自己预览自己」。"""

    def name(self) -> str:
        return "PanelMove"

    def commandTitle(self) -> str:
        return "移动（两点）"

    def start(self) -> None:
        super().start()
        self._p1: Optional[Point3D] = None
        self._p2: Optional[Point3D] = None
        self._cursor: Optional[Point3D] = None

    def prompt(self) -> str:
        if self._msg:
            return self._msg
        if not self._selectedObjects:
            return "点击选中要移动的对象，或先选中再点基准点"
        if self._p1 is None:
            return "点选基准点（移动起点）"
        return "点选目标点（移动终点），拖动预览；右键/回车确认"

    def _updatePreview(self) -> None:
        """参考线预览（路径）：P1→目标 青色虚线；
        对象本身的预览：通过实时编辑真实几何触发 2D 场景自动重绘。"""
        if not self._selectedObjects or self._p1 is None:
            self._setPreviewPath(QPainterPath())
            return
        target = self._cursor if self._cursor is not None else self._p1
        # 实时改真实对象（事务中）
        dx = target.x - self._p1.x
        dy = target.y - self._p1.y
        dz = target.z - self._p1.z
        if self._editActive:
            self._updateLive((dx, dy, dz))
        # 参考线
        ref = QPainterPath()
        ref.moveTo(self._p1.x, self._p1.y)
        ref.lineTo(target.x, target.y)
        self._setPreviewPath(ref)

    def onMousePress(self, wp: Point3D, mods) -> None:
        self._msg = ""
        wp = self._orthoPoint(wp, self._p1) if self._p1 is not None else wp
        if not self._ensureSelectionFromPick(wp):
            return
        if self._p1 is None:
            self._p1 = wp
            self._cursor = wp
            self._setCursorMarker(wp)
            # 点完 P1，立刻开移动事务（delta=0 → 先不动对象）
            self._startLiveEdit("move")
            if self._manager is not None:
                self._manager.promptChanged.emit(self.prompt())
            return
        # 第二点 → 确认（当前 delta 就是最终值，commit 提交）
        self._p2 = wp
        self._applyMove()

    def onMouseMove(self, wp: Point3D, mods) -> None:
        wp = self._orthoPoint(wp, self._p1) if self._p1 is not None else wp
        self._cursor = wp
        if self._p1 is None:
            return
        self._setCursorMarker(wp)
        self._updatePreview()

    def onRightClick(self, wp: Point3D) -> bool:
        if self._p1 is not None and self._p2 is None:
            wp = self._orthoPoint(wp, self._p1)
            self._p2 = wp if self._cursor is None else self._cursor
            self._applyMove()
            return True
        self.finish()
        return True

    def onEnter(self) -> bool:
        if self._p1 is not None and self._p2 is None and self._cursor is not None:
            self._p2 = self._cursor
            self._applyMove()
            return True
        return False

    def _applyMove(self) -> None:
        objs = self._unlockedSelected()
        if not objs:
            self._msg = "没有可移动的对象"
            return
        if self._p1 is None or self._p2 is None:
            return
        dx = self._p2.x - self._p1.x
        dy = self._p2.y - self._p1.y
        dz = self._p2.z - self._p1.z
        # 如果事务还没开启（极小概率：P1 阶段开事务失败但仍能点 P2），兜底再开一次。
        if not self._editActive:
            self._startLiveEdit("move")
            self._applyDelta((dx, dy, dz))
        else:
            # 之前 onMouseMove 已经把对象移到了 _prevDelta；如果用户 P2 与光标一致，
            # 这里不再需要重新 apply；但如果用户在其他地方点的 P2，同步应用最终值。
            finalDelta = (dx, dy, dz)
            if self._prevDelta != finalDelta:
                self._updateLive(finalDelta)
        if abs(dx) < 1e-12 and abs(dy) < 1e-12 and abs(dz) < 1e-12:
            # 零位移：取消事务回滚，不落撤销记录
            self._cancelLiveEdit()
            self._manager.notifyResult(f"已移动 {len(objs)} 个对象（零位移，无撤销）")
            self.finish()
            return
        # commit：endEdit 提交一条撤销记录（所有中间步合并成一条）
        self._commitLiveEdit()
        if self._doc is not None:
            for o in objs:
                self._doc.notifyObjectChanged(o.id())
            self._manager.notifyResult(
                f"已移动 {len(objs)} 个对象（ΔX={dx:.2f}, ΔY={dy:.2f}）")
            self._notifyGizmoRefresh()
        self.finish()


# ==================== PanelRotate 面板旋转（三点定旋转） ====================

class PanelRotateCommand(_PanelXformBase):
    """面板旋转：选中对象 → 点中心点(P1) → 点参考起点(P2，定义基准方向)
    → 点目标位置(P3)，绕 P1 从 P1→P2 方向 旋转到 P1→P3 方向。
    右键/回车可用当前光标位置作为 P3 立即确认。

    预览与操作轴 gizmo 同款：beginEdit 事务中实时改真实对象几何，2D 图元自动重绘。"""

    def name(self) -> str:
        return "PanelRotate"

    def commandTitle(self) -> str:
        return "旋转（三点）"

    def start(self) -> None:
        super().start()
        self._p1: Optional[Point3D] = None  # 中心点
        self._p2: Optional[Point3D] = None  # 参考起点
        self._p3: Optional[Point3D] = None  # 目标点
        self._cursor: Optional[Point3D] = None

    def prompt(self) -> str:
        if self._msg:
            return self._msg
        if not self._selectedObjects:
            return "点击选中要旋转的对象，或先选中再点旋转中心"
        if self._p1 is None:
            return "点选旋转中心点"
        if self._p2 is None:
            return "点选参考起点（定义起始方向）"
        return "点选目标角度位置，拖动预览；右键/回车确认"

    def _currentAngleRad(self, target: Point3D) -> float:
        """按当前状态计算旋转角（弧度，绕 Z 轴）：目标方向 − 参考方向。"""
        if self._p1 is None or self._p2 is None:
            return 0.0
        v1x, v1y = self._p2.x - self._p1.x, self._p2.y - self._p1.y
        v2x, v2y = target.x - self._p1.x, target.y - self._p1.y
        a1 = math.atan2(v1y, v1x)
        a2 = math.atan2(v2y, v2x)
        return a2 - a1

    def _updatePreview(self) -> None:
        if not self._selectedObjects or self._p1 is None or self._p2 is None:
            # P1→P2 未完成：只画 P1→光标参考辅助线，不动真实对象
            if self._selectedObjects and self._p1 is not None:
                ref = QPainterPath()
                if self._cursor is not None:
                    ref.moveTo(self._p1.x, self._p1.y)
                    ref.lineTo(self._cursor.x, self._cursor.y)
                self._setPreviewPath(ref)
            else:
                self._setPreviewPath(QPainterPath())
            return
        target = self._cursor if self._cursor is not None else self._p2
        angle = self._currentAngleRad(target)
        # 实时改真实对象（事务中）
        if self._editActive:
            self._updateLive((angle, self._p1.x, self._p1.y, self._p1.z))
        # 两条参考线：中心→P2 基准方向、中心→目标当前方向
        ref = QPainterPath()
        ref.moveTo(self._p1.x, self._p1.y)
        ref.lineTo(self._p2.x, self._p2.y)
        ref.moveTo(self._p1.x, self._p1.y)
        ref.lineTo(target.x, target.y)
        self._setPreviewPath(ref)

    def onMousePress(self, wp: Point3D, mods) -> None:
        self._msg = ""
        if not self._ensureSelectionFromPick(wp):
            return
        if self._p1 is None:
            self._p1 = wp
            self._cursor = wp
            self._setCursorMarker(wp)
            if self._manager is not None:
                self._manager.promptChanged.emit(self.prompt())
            return
        if self._p2 is None:
            if self._p1.distanceTo(wp) < 1e-9:
                self._msg = "参考起点与中心点重合，请重新点选"
                if self._manager is not None:
                    self._manager.promptChanged.emit(self.prompt())
                return
            self._p2 = wp
            # 点完 P2，立刻开旋转事务（当前 delta=angle=0 → 先不动对象）
            self._startLiveEdit("rotate")
            self._setCursorMarker(wp)
            self._updatePreview()
            if self._manager is not None:
                self._manager.promptChanged.emit(self.prompt())
            return
        # P3 目标点 → 立即执行
        self._p3 = wp
        self._applyRotate()

    def onMouseMove(self, wp: Point3D, mods) -> None:
        self._cursor = wp
        if self._p1 is None:
            return
        self._setCursorMarker(wp)
        self._updatePreview()

    def onRightClick(self, wp: Point3D) -> bool:
        if self._p1 is not None and self._p2 is not None and self._p3 is None:
            self._p3 = wp if self._cursor is None else self._cursor
            self._applyRotate()
            return True
        self.finish()
        return True

    def onEnter(self) -> bool:
        if self._p1 is not None and self._p2 is not None and self._p3 is None and self._cursor is not None:
            self._p3 = self._cursor
            self._applyRotate()
            return True
        return False

    def _applyRotate(self) -> None:
        objs = self._unlockedSelected()
        if not objs:
            self._msg = "没有可旋转的对象"
            return
        if self._p1 is None or self._p2 is None or self._p3 is None:
            return
        angle = self._currentAngleRad(self._p3)
        if not self._editActive:
            self._startLiveEdit("rotate")
            self._applyDelta((angle, self._p1.x, self._p1.y, self._p1.z))
        else:
            finalDelta = (angle, self._p1.x, self._p1.y, self._p1.z)
            if self._prevDelta != finalDelta:
                self._updateLive(finalDelta)
        if abs(angle) < 1e-14:
            self._cancelLiveEdit()
            deg = math.degrees(angle)
            self._manager.notifyResult(f"已旋转 {len(objs)} 个对象（角度 {deg:.2f}°，零旋转回滚）")
            self.finish()
            return
        self._commitLiveEdit()
        if self._doc is not None:
            for o in objs:
                self._doc.notifyObjectChanged(o.id())
            deg = math.degrees(angle)
            self._manager.notifyResult(
                f"已旋转 {len(objs)} 个对象（角度 {deg:.2f}°）")
            self._notifyGizmoRefresh()
        self.finish()


# ==================== PanelScale 面板缩放（三点 + 键盘直接输入目标长度）====================

class PanelScaleCommand(_PanelXformBase):
    """面板缩放：选中对象 → 点基准点(P1) → 点参考起点(P2，定义参考长度 Lref 与方向)。
    之后有两种确认方式（并行，随时切换）：
      1. 鼠标：点 P3 或拖动预览 → 右键/回车确认
      2. 键盘：点完 P2 后直接按数字键输入「目标长度」（支持 0-9 . 退格）
         → 预览立刻按「方向=P1→P2，长度=输入值」重算 P3'
         → 回车/右键即可应用；Esc 先清空数字缓冲（清空后再 Esc 取消命令）

    比例 k = 目标长度 / Lref；绕 P1 对所有选中对象做等比缩放。
    第一个点(P1)始终不动，P2 被重新定位到 P3'（沿 P1→P2 单位向的新长度端点）。"""

    def name(self) -> str:
        return "PanelScale"

    def commandTitle(self) -> str:
        return "缩放（三点/数值）"

    def start(self) -> None:
        super().start()
        self._p1: Optional[Point3D] = None  # 基准点/缩放中心
        self._p2: Optional[Point3D] = None  # 参考起点（定义参考长度和方向）
        self._p3: Optional[Point3D] = None  # 目标点
        self._cursor: Optional[Point3D] = None
        self._kbInput: str = ""             # 键盘输入目标长度缓冲

    # ---------- 状态辅助 ----------

    def _refLen(self) -> float:
        if self._p1 is None or self._p2 is None:
            return 0.0
        return self._p1.distanceTo(self._p2)

    def _kbValue(self) -> Optional[float]:
        s = self._kbInput.strip()
        if not s or s == ".":
            return None
        try:
            v = float(s)
            return v if v > 0 else None
        except ValueError:
            return None

    def _directionUnit(self):
        """P1→P2 方向的单位向量（XY 平面），失败返回 None。"""
        if self._p1 is None or self._p2 is None:
            return None
        dx = self._p2.x - self._p1.x
        dy = self._p2.y - self._p1.y
        dz = self._p2.z - self._p1.z
        L = math.hypot(dx, math.hypot(dy, dz))
        if L < 1e-15:
            return None
        return (dx / L, dy / L, dz / L)

    def _kbTargetPoint(self) -> Optional[Point3D]:
        """把键盘输入值映射成「P1 + 单位(P1→P2) × 目标值」的 P3'。"""
        v = self._kbValue()
        u = self._directionUnit()
        if v is None or u is None or self._p1 is None:
            return None
        return Point3D(self._p1.x + u[0] * v,
                       self._p1.y + u[1] * v,
                       self._p1.z + u[2] * v)

    def _effectiveTarget(self) -> Optional[Point3D]:
        """预览/确认时使用的 P3：键盘优先级高于光标。"""
        p = self._kbTargetPoint()
        if p is not None:
            return p
        if self._p3 is not None:
            return self._p3
        return self._cursor

    # ---------- 提示 ----------

    def prompt(self) -> str:
        if self._msg:
            return self._msg
        if not self._selectedObjects:
            return "点击选中要缩放的对象，或先选中再点缩放基准点"
        if self._p1 is None:
            return "点选缩放基准点（缩放中心）"
        if self._p2 is None:
            return "点选参考起点（定义参考长度和方向）"

        refLen = self._refLen()
        # 键盘正在输入：优先显示键盘输入的目标值
        if self._kbInput:
            kb = self._kbValue()
            if kb is None:
                tail = f"[输入: {self._kbInput}  回车确认, Esc 清空]"
                return (f"参考长度 L={refLen:.3f}  "
                        f"{tail}")
            ratio = kb / refLen if refLen > 1e-15 else 1.0
            return (f"参考长度 L={refLen:.3f}  目标={kb:.3f}  "
                    f"比例 k={ratio:.4f}×  [回车确认, Esc 清空, 或继续点P3]")

        # 纯鼠标模式：显示光标位置对应的比例
        t = self._effectiveTarget()
        if t is not None and refLen > 1e-15:
            cur = self._p1.distanceTo(t)
            k = cur / refLen
            return (f"参考长度 L={refLen:.3f}  当前={cur:.3f}  比例 k={k:.4f}×  "
                    f"[点P3确认 或 直接输入数字设目标长度]")
        return (f"参考长度 L={refLen:.3f}  "
                f"[点P3确认 或 直接输入数字设目标长度（P1不动 沿P1→P2方向）]")

    # ---------- 比例/预览 ----------

    def _currentScale(self, target: Point3D) -> float:
        refLen = self._refLen()
        if refLen < 1e-12:
            return 1.0
        curLen = self._p1.distanceTo(target)
        return curLen / refLen

    def _updatePreview(self) -> None:
        if not self._selectedObjects or self._p1 is None or self._p2 is None:
            # P1→P2 未完成：只画 P1→光标辅助线
            if self._selectedObjects and self._p1 is not None:
                ref = QPainterPath()
                if self._cursor is not None:
                    ref.moveTo(self._p1.x, self._p1.y)
                    ref.lineTo(self._cursor.x, self._cursor.y)
                self._setPreviewPath(ref)
            else:
                self._setPreviewPath(QPainterPath())
            return
        target = self._effectiveTarget()
        if target is None:
            target = self._p2
        k = self._currentScale(target)
        # 实时改真实对象（事务中）
        if self._editActive:
            self._updateLive((k, self._p1.x, self._p1.y, self._p1.z))
        # 参考线：P1→P2（原参考）和 P1→target（当前目标）
        ref = QPainterPath()
        ref.moveTo(self._p1.x, self._p1.y)
        ref.lineTo(self._p2.x, self._p2.y)
        if target is not None:
            ref.moveTo(self._p1.x, self._p1.y)
            ref.lineTo(target.x, target.y)
        self._setPreviewPath(ref)

    def _emitPrompt(self) -> None:
        if self._manager is not None:
            self._manager.promptChanged.emit(self.prompt())

    # ---------- 键盘输入（P2 已点击后生效）----------

    def _applyKbChange(self) -> None:
        # 键盘输入变了：实时更新预览（真实对象）+ 左下角提示
        self._updatePreview()
        self._emitPrompt()

    def onKeyPress(self, key, mods) -> bool:
        # P2 未点：键盘输入不拦截
        if self._p2 is None:
            return False
        # 允许修饰键：无修饰 + 小键盘修饰（数字键盘的 0-9 会带 KeypadModifier）；
        # 遇到 Ctrl / Alt / Shift / Meta 直接不拦截，交给全局快捷键。
        blocked = (Qt.ControlModifier | Qt.AltModifier |
                   Qt.ShiftModifier | Qt.MetaModifier)
        if mods & blocked:
            return False
        if Qt.Key_0 <= key <= Qt.Key_9:
            self._kbInput += chr(int(key))
            self._applyKbChange()
            return True
        if key in (Qt.Key_Period, Qt.Key_Comma):
            if "." not in self._kbInput:
                self._kbInput += "."
                self._applyKbChange()
            return True
        if key == Qt.Key_Backspace:
            if self._kbInput:
                self._kbInput = self._kbInput[:-1]
                self._applyKbChange()
            return True
        if key == Qt.Key_Escape:
            if self._kbInput:
                # 第一次 Esc：只清数字缓冲
                self._kbInput = ""
                self._applyKbChange()
                return True
            return False   # 再 Esc：走默认命令取消
        return False

    # ---------- 鼠标 & 确认 ----------

    def onMousePress(self, wp: Point3D, mods) -> None:
        self._msg = ""
        if not self._ensureSelectionFromPick(wp):
            return
        if self._p1 is None:
            self._p1 = wp
            self._cursor = wp
            self._setCursorMarker(wp)
            self._emitPrompt()
            return
        if self._p2 is None:
            if self._p1.distanceTo(wp) < 1e-9:
                self._msg = "参考起点与基准点重合，请重新点选"
                self._emitPrompt()
                return
            self._p2 = wp
            # 点完 P2：立刻开缩放事务（当前 delta=k=1 → 先不动对象）
            self._startLiveEdit("scale")
            self._setCursorMarker(wp)
            self._updatePreview()
            self._emitPrompt()
            return
        # P3：鼠标点击，清键盘输入以避免歧义
        self._kbInput = ""
        self._p3 = wp
        self._applyScale()

    def onMouseMove(self, wp: Point3D, mods) -> None:
        self._cursor = wp
        if self._p1 is None:
            return
        self._setCursorMarker(wp)
        # 键盘输入阶段：仍允许光标移动，但 P3' 以键盘输入为准（预览已在 onKeyPress 更新过）
        if not self._kbInput:
            self._updatePreview()
            self._emitPrompt()
        else:
            # 键盘有输入，但仍需刷新提示（当前比例的数字提示仍是目标 k，不受光标影响）
            # 仍刷新预览保证 P3' 参考线绘制到最新键盘目标位置
            self._updatePreview()

    def onRightClick(self, wp: Point3D) -> bool:
        if self._p1 is not None and self._p2 is not None and self._p3 is None:
            kbP3 = self._kbTargetPoint()
            if kbP3 is not None:
                self._p3 = kbP3
            else:
                self._p3 = wp if self._cursor is None else self._cursor
            self._applyScale()
            return True
        self.finish()
        return True

    def onEnter(self) -> bool:
        if self._p1 is not None and self._p2 is not None and self._p3 is None:
            kbP3 = self._kbTargetPoint()
            if kbP3 is not None:
                self._p3 = kbP3
            elif self._cursor is not None:
                self._p3 = self._cursor
            else:
                return False
            self._applyScale()
            return True
        return False

    def _applyScale(self) -> None:
        objs = self._unlockedSelected()
        if not objs:
            self._msg = "没有可缩放的对象"
            return
        if self._p1 is None or self._p2 is None or self._p3 is None:
            return
        k = self._currentScale(self._p3)
        # 应用（或同步）最终几何：通过事务实时改真实对象
        if not self._editActive:
            self._startLiveEdit("scale")
            self._applyDelta((k, self._p1.x, self._p1.y, self._p1.z))
        else:
            finalDelta = (k, self._p1.x, self._p1.y, self._p1.z)
            if self._prevDelta != finalDelta:
                self._updateLive(finalDelta)
        if abs(k - 1.0) < 1e-12:
            # 无缩放：回滚事务，不落撤销
            self._cancelLiveEdit()
            refLen = self._refLen()
            newLen = self._p1.distanceTo(self._p3)
            self._manager.notifyResult(
                f"已缩放 {len(objs)} 个对象（目标长度 {newLen:.3f} / "
                f"参考 {refLen:.3f}，比例 k=1.0000，已回滚）")
            self.finish()
            return
        self._commitLiveEdit()
        if self._doc is not None:
            for o in objs:
                self._doc.notifyObjectChanged(o.id())
            refLen = self._refLen()
            newLen = self._p1.distanceTo(self._p3)
            if self._kbInput and self._kbValue() is not None:
                self._manager.notifyResult(
                    f"已缩放 {len(objs)} 个对象（目标长度 {newLen:.3f} / "
                    f"参考 {refLen:.3f}，比例 k={k:.4f}）")
            else:
                self._manager.notifyResult(
                    f"已缩放 {len(objs)} 个对象（比例 {k:.4f}）")
            self._notifyGizmoRefresh()
        self.finish()


# ==================== PanelMeasure 面板测量（两点距离，左下角提示）====================

class PanelMeasureCommand(CommandInstance):
    """变换面板测量命令：
    1) 点 P1 = 第一个点
    2) 点 P2 = 第二个点（鼠标移动时实时预览两点连线 + 当前距离；回车/右键也确认）
    结果：左下角 promptChanged 与 notifyResult 显示两点距离。
    """

    def name(self) -> str:
        return "PanelMeasure"

    def commandTitle(self) -> str:
        return "测量两点距离"

    def showSnapHints(self) -> bool:
        return True

    def start(self) -> None:
        super().start()
        self._p1: Optional[Point3D] = None
        self._p2: Optional[Point3D] = None
        self._cursor: Optional[Point3D] = None
        if self._manager is not None:
            self._manager.promptChanged.emit(self.prompt())

    def prompt(self) -> str:
        if self._p1 is None:
            return "测量：点选第一个点（P1），Esc 取消"
        if self._p2 is None:
            line1 = "测量：点选第二个点（P2），回车/右键确认，Esc 取消"
            if self._cursor is not None:
                d = self._p1.distanceTo(self._cursor)
                dx = self._cursor.x - self._p1.x
                dy = self._cursor.y - self._p1.y
                dz = self._cursor.z - self._p1.z
                line2 = (f"  当前距离 = {d:.3f}  （Δx={dx:+.3f}, Δy={dy:+.3f}"
                         f", Δz={dz:+.3f}）")
                return line1 + "\n" + line2
            return line1
        return ""

    # ---------- 输入 ----------

    def onMouseMove(self, wp: Point3D, mods) -> None:
        self._cursor = wp
        self._refreshPreview()
        if self._manager is not None:
            self._manager.promptChanged.emit(self.prompt())

    def onMousePress(self, wp: Point3D, mods) -> None:
        if self._p1 is None:
            self._p1 = wp
            self._cursor = wp
        elif self._p2 is None:
            self._p2 = wp
            self._applyMeasure()
            return
        if self._manager is not None:
            self._manager.promptChanged.emit(self.prompt())
        self._refreshPreview()

    def onEnter(self) -> None:
        if self._p1 is not None and self._p2 is None and self._cursor is not None:
            self._p2 = self._cursor
            self._applyMeasure()

    def onRightClick(self, wp: Point3D) -> bool:
        """测量：右键直接退出（不确认当前点）。返回 False 让 CommandManager
        执行默认 cancelActive，效果与 Esc 相同。"""
        return False

    def onKeyPress(self, key: int, mods: int) -> bool:
        return False

    # ---------- 预览：画出 P1→当前光标 的虚线，并在端点画圆标记 ----------

    def _refreshPreview(self) -> None:
        if self._previewItem is None or self._p1 is None:
            return
        from PySide6.QtGui import QPainterPath
        p = QPainterPath()
        p1 = self._p1
        p2 = self._cursor if self._p2 is None else self._p2
        if p2 is None:
            p2 = p1
        # 两点连线
        p.moveTo(p1.x, p1.y)
        p.lineTo(p2.x, p2.y)
        self._previewItem.setPath(p)
        # 两端点圆圈标记：复用 _markerItem (单个椭圆) 还是画两个？
        # 用两个独立 Ellipse 在 preview 里做：
        scene = self._previewItem.scene() if self._previewItem is not None else None
        if scene is None:
            return
        # 用 markerItem 画最近端点，另一个点在 preview path 里不画圈，
        # 直接用路径端点足矣。

    # ---------- 结果 ----------

    def _applyMeasure(self) -> None:
        if self._p1 is None or self._p2 is None:
            self.finish()
            return
        d = self._p1.distanceTo(self._p2)
        dx = self._p2.x - self._p1.x
        dy = self._p2.y - self._p1.y
        dz = self._p2.z - self._p1.z
        # 2D 投影长度
        dxy = math.hypot(dx, dy)
        msg = (f"测量距离 = {d:.3f}  （Δx={dx:+.3f}, Δy={dy:+.3f}, "
               f"Δz={dz:+.3f}, XY 投影={dxy:.3f}）")
        if self._manager is not None:
            self._manager.notifyResult(msg)
            self._manager.promptChanged.emit(msg)
        self.finish()


def registerAll(manager) -> None:
    manager.register("Chamfer", ChamferCommand)
    manager.register("Trim", TrimCommand)
    manager.register("Array", ArrayCommand)
    manager.register("Offset", OffsetCommand)
    manager.register("Group", GroupCommand)
    manager.register("Ungroup", UngroupCommand)
    manager.register("Join", JoinCommand)
    manager.register("Mirror", MirrorCommand)
    manager.register("Extend", ExtendCommand)
    manager.register("ClosePath", CloseCommand)
    manager.register("Outline", OutlineCommand)
    manager.register("Align", AlignCommand)
    manager.register("PanelMove", PanelMoveCommand)
    manager.register("PanelRotate", PanelRotateCommand)
    manager.register("PanelScale", PanelScaleCommand)
    manager.register("PanelMeasure", PanelMeasureCommand)


# ==================== Trim 修剪 ====================

def _denseOutlinePoints(obj, n: int = 1024) -> List[Point3D]:
    """对象真实轮廓的高密度采样（交点精化用）。

    圆/弧/椭圆显示为解析曲线，与粗折线采样存在矢高差——交点若直接取
    粗折线相交，切点会落在折线弦/顶点上而不是显示曲线的实际相交处。
    此处按解析公式密采真实轮廓；曲线/折线的显示点列即真实几何，原样返回。
    """
    t = obj.type().name
    if t == "CIRCLE":
        c, r = obj.center(), obj.radius()
        return [Point3D(c.x + r * math.cos(2 * math.pi * i / n),
                        c.y + r * math.sin(2 * math.pi * i / n), c.z)
                for i in range(n + 1)]
    if t == "ARC":
        c, r = obj.center(), obj.radius()
        sa, ea = obj.startAngle(), obj.endAngle()
        span = ea - sa
        if abs(span) < 1e-9:
            span = 2 * math.pi
        return [Point3D(c.x + r * math.cos(sa + span * i / n),
                        c.y + r * math.sin(sa + span * i / n), c.z)
                for i in range(n + 1)]
    if t == "ELLIPSE":
        c = obj.center()
        a, b = obj.majorAxis(), obj.minorAxis()
        rot = obj.rotation()
        cr, sr = math.cos(rot), math.sin(rot)
        out = []
        for i in range(n + 1):
            th = 2 * math.pi * i / n
            lx = a * math.cos(th)
            ly = b * math.sin(th)
            out.append(Point3D(c.x + lx * cr - ly * sr,
                               c.y + lx * sr + ly * cr, c.z))
        return out
    return TrimCommand._getSamplePoints(obj)


def _projectOnPolyline(pt: Point3D, pts: List[Point3D]):
    """点投影到折线：返回 (最近段索引, 段内参数 t, 最近点, 累计长度)。"""
    bestIdx, bestT, bestPt, bestCumul = 0, 0.0, pts[0], 0.0
    bestDist = float('inf')
    cumul = 0.0
    for i in range(len(pts) - 1):
        near, t = nearestPointOnSegment(pts[i], pts[i + 1], pt)
        d = near.distanceTo(pt)
        if d < bestDist:
            bestDist = d
            bestIdx, bestT, bestPt = i, t, near
            bestCumul = cumul + pts[i].distanceTo(pts[i + 1]) * t
        cumul += pts[i].distanceTo(pts[i + 1])
    return bestIdx, bestT, bestPt, bestCumul


class TrimCommand(CommandInstance):
    """修剪命令：支持两种模式。

    模式 0 - 快速修剪：直接点击要修剪的线段，自动找交点并修剪
    模式 1 - 边界修剪：先选边界线，再选要修剪的曲线

    交互流程：
    - 快速模式：点选要修剪的线段 → 自动找交点 → 点击保留侧 → 确认
    - 边界模式：选择边界曲线 → 选择要修剪的曲线 → 点击保留侧 → 确认
    """

    def name(self) -> str:
        return "Trim"

    def commandTitle(self) -> str:
        return "修剪参数"

    def showSnapHints(self) -> bool:
        """修剪时禁用吸附气泡，只在画线时显示。"""
        return False

    def start(self) -> None:
        super().start()
        self._mode: int = 0  # 0=快速修剪 1=边界修剪
        self._boundaryObj: Optional[LineObject] = None
        self._trimObj: Optional[LineObject] = None
        self._boundarySegIdx: int = -1
        self._trimSegIdx: int = -1
        self._intersection: Optional[Point3D] = None
        self._keepSide: int = 0  # 0=待选择 1=保留起点侧 2=保留终点侧
        self._pickState: str = "waitTrim"
        self._msg: str = ""
        self._panel: Optional[TrimParamPanel] = None
        self._trimIntersections = []
        self._trimLeftIdx = -1
        self._trimRightIdx = -1
        self._trimWrap = False

    def prompt(self) -> str:
        if self._msg:
            return self._msg
        if self._mode == 0:
            # 快速修剪模式
            if self._pickState == "waitTrim":
                return "点击要修剪的线段（自动找交点）"
            if self._pickState == "waitSide":
                return "点击要保留的一侧，或回车/右键确认"
        else:
            # 边界修剪模式
            if self._pickState == "waitBoundary":
                return "选择边界曲线"
            if self._pickState == "waitTrim":
                return "选择要修剪的曲线"
            if self._pickState == "waitSide":
                return "点击要保留的一侧，或回车/右键确认"
        return "回车/右键确认执行，Esc 取消"

    def paramPanel(self):
        if self._panel is None:
            self._panel = TrimParamPanel()
            self._panel.modeChanged.connect(self._onPanelMode)
            self._panel.confirmed.connect(self._onPanelConfirmed)
            self._panel.cancelRequested.connect(self.cancel)
        return self._panel

    # ---------- 面板槽 ----------

    def _onPanelMode(self, mode: int) -> None:
        self._mode = mode
        self._resetSelection()

    def _onPanelConfirmed(self) -> None:
        if self._pickState == "ready":
            self._confirm()

    def _resetSelection(self) -> None:
        self._boundaryObj = None
        self._trimObj = None
        self._boundarySegIdx = -1
        self._trimSegIdx = -1
        self._intersection = None
        self._keepSide = 0
        self._pickState = "waitTrim" if self._mode == 0 else "waitBoundary"
        self._msg = ""
        self._setPreviewPath(QPainterPath())

    # ---------- 边拾取 ----------

    def _worldPickRadius(self) -> float:
        scale = abs(self._view.transform().m11()) if self._view is not None else 1.0
        return PICK_PIXEL_RADIUS / max(scale, 1e-6)

    @staticmethod
    def _getSamplePoints(obj) -> List[Point3D]:
        """获取对象的采样点列表（支持直线/曲线/圆/椭圆/弧）。

        闭合轮廓（矩形/多边形等首尾不重复存储）统一补上首点副本：
        否则 range(len-1) 的段遍历漏掉收尾边，那条边的交点永远找不到
        （表现为"有一条边识别不到交点"）。"""
        t = obj.type().name
        if t == "LINE":
            pts = list(obj.points())
        elif t == "CURVE":
            pts = list(obj.getSampledPoints())
        elif t == "CIRCLE" or t == "ELLIPSE" or t == "ARC":
            poly = obj.toPolycurve()
            pts: List[Point3D] = []
            for seg in poly.segments():
                pts.extend([Point3D(p[0], p[1], p[2]) for p in seg.get("points", [])])
        elif t == "POLYCURVE":
            pts = []
            for seg in obj.segments():
                pts.extend([Point3D(p[0], p[1], p[2]) for p in seg.get("points", [])])
        else:
            return []
        if OffsetCommand._isClosed(obj) and pts and \
                pts[0].distanceTo(pts[-1]) >= 1e-6:
            pts.append(Point3D(pts[0].x, pts[0].y, pts[0].z))
        return pts

    def _pickEdge(self, wp: Point3D):
        """拾取鼠标附近最近的对象线段（支持直线/曲线/圆等）。"""
        if self._doc is None:
            return None
        worldR = self._worldPickRadius()
        best = None
        bestDist = worldR
        for obj in self._doc.visibleObjects():
            pts = self._getSamplePoints(obj)
            if len(pts) < 2:
                continue
            for i in range(len(pts) - 1):
                near, _ = nearestPointOnSegment(pts[i], pts[i + 1], wp)
                d = near.distanceTo(wp)
                if d <= bestDist:
                    bestDist = d
                    best = (obj, i)
        return best

    # ---------- 几何计算 ----------

    def _computeIntersection(self) -> bool:
        """计算边界与修剪曲线的交点。"""
        if self._boundaryObj is None or self._trimObj is None:
            return False
        if self._boundaryObj is self._trimObj:
            self._msg = "不能对同一条曲线自修剪"
            return False

        b_pts = self._getSamplePoints(self._boundaryObj)
        t_pts = self._getSamplePoints(self._trimObj)
        b_start, b_end = b_pts[self._boundarySegIdx], b_pts[self._boundarySegIdx + 1]

        # 遍历修剪曲线的所有线段，找最近的交点
        best_inter: Optional[Point3D] = None
        best_dist = float('inf')
        best_trim_idx = -1

        for i in range(len(t_pts) - 1):
            t_start, t_end = t_pts[i], t_pts[i + 1]
            inter, t_param, s_param = lineLineIntersection(b_start, b_end, t_start, t_end)
            if inter is not None:
                # 检查交点是否在线段有效范围内
                if 0 <= t_param <= 1 and 0 <= s_param <= 1:
                    d = inter.distanceTo(b_start)
                    if d < best_dist:
                        best_dist = d
                        best_inter = inter
                        best_trim_idx = i

        if best_inter is None:
            self._msg = "边界与修剪曲线无交点"
            return False

        self._intersection = best_inter
        self._trimSegIdx = best_trim_idx
        return True

    def _findIntersectionForQuickTrim(self, trimObj, trimSegIdx: int,
                                       clickPoint: Point3D) -> bool:
        """快速修剪：找所有交点，修剪点击位置所在的两交点之间的区段。"""
        if self._doc is None:
            return False

        t_pts = self._getSamplePoints(trimObj)
        if len(t_pts) < 2:
            return False

        # 收集所有交点（交点位置、线段索引、线段参数 t）
        # 参数范围带端点容差：修剪另一曲线后，其端点恰好落在闭合曲线上
        # （触碰式交点），浮点参数可能略微越界（如 s=-1e-4）而被漏判——
        # 漏判后闭合曲线只剩一个交点，环绕修剪会退化成整圆删除。
        # 按世界距离 0.05mm 容差接受，并把参数钳制回 [0,1]。
        endTol = 0.05
        intersections = []
        for j in range(len(t_pts) - 1):
            t_start, t_end = t_pts[j], t_pts[j + 1]
            t_seg = max(t_start.distanceTo(t_end), 1e-9)
            for obj in self._doc.visibleObjects():
                b_pts = self._getSamplePoints(obj)
                if len(b_pts) < 2:
                    continue
                isSelf = (obj is trimObj)
                for i in range(len(b_pts) - 1):
                    # 自身自相交：跳过同一段及相邻段（共享端点不算自相交）；
                    # 闭合折线收尾弦与首弦亦相邻（模运算距离，共享角点不算）
                    if isSelf:
                        n_seg = len(b_pts) - 1
                        adj = min(abs(i - j), n_seg - abs(i - j))
                        if adj <= 1:
                            continue
                    b_start, b_end = b_pts[i], b_pts[i + 1]
                    inter, t_param, s_param = lineLineIntersection(t_start, t_end, b_start, b_end)
                    if inter is None:
                        continue
                    if isSelf:
                        # 自身相交：严格判定——端点容差会把同一折线接缝/
                        # 角落处两条弦延长线的近似交点误判为真交点
                        if not (0 <= t_param <= 1 and 0 <= s_param <= 1):
                            continue
                    else:
                        tolT = endTol / t_seg
                        tolS = endTol / max(b_start.distanceTo(b_end), 1e-9)
                        if not (-tolT <= t_param <= 1 + tolT
                                and -tolS <= s_param <= 1 + tolS):
                            continue
                    # 交点被钳制到段端点（=折线顶点）时记录：交点可能只是
                    # 延长线近似，需经精化确认，否则会剪到顶点上
                    clamped = t_param < 0.0 or t_param > 1.0
                    t_param = min(max(t_param, 0.0), 1.0)
                    # 计算交点在曲线上的累积长度位置
                    seg_len = t_start.distanceTo(t_end)
                    cumul = sum(t_pts[k].distanceTo(t_pts[k + 1]) for k in range(j))
                    cumul_at_inter = cumul + seg_len * t_param
                    intersections.append(
                        (inter, j, t_param, cumul_at_inter, obj, clamped))

        if not intersections:
            self._msg = "未找到与其他曲线的交点"
            return False

        # 交点精化：候选点附近用真实轮廓的高密度采样重新求交，再投影回
        # 粗折线——消除交点落在折线顶点/弦上的近似误差（圆/弧/椭圆显示
        # 为解析曲线，粗折线交点与显示曲线的实际相交处存在矢高偏差）。
        denseCache = {}

        def denseOf(o):
            key = id(o)
            if key not in denseCache:
                denseCache[key] = _denseOutlinePoints(o)
            return denseCache[key]

        refined = []
        for cand in intersections:
            inter, j, t_param, cumul_at_inter, srcObj, clamped = cand
            tD = denseOf(trimObj)
            sD = tD if srcObj is trimObj else denseOf(srcObj)
            R = max(1.0, min(3.0 * t_pts[j].distanceTo(t_pts[j + 1]), 5.0))

            def windowPairs(dense):
                """候选点附近的高密度线段对：按线段到候选的距离过滤，
                而非按端点过滤——直线等对象的端点远在两侧，按端点过滤
                会把整个对象漏掉导致无法精化。"""
                pairs = []
                for p1, p2 in zip(dense, dense[1:]):
                    near, _ = nearestPointOnSegment(p1, p2, inter)
                    if near.distanceTo(inter) <= R:
                        pairs.append((p1, p2))
                return pairs

            tw = windowPairs(tD)
            sw = windowPairs(sD)
            best, bestD = inter, float('inf')
            for a1, a2 in tw:
                aSeg = max(a1.distanceTo(a2), 1e-9)
                for b1, b2 in sw:
                    bSeg = max(b1.distanceTo(b2), 1e-9)
                    ip, tt, ss = lineLineIntersection(a1, a2, b1, b2)
                    if ip is None:
                        continue
                    if srcObj is trimObj:
                        # 自身：严格判定（同收集阶段，防止角落延长线误判）
                        if not (0 <= tt <= 1 and 0 <= ss <= 1):
                            continue
                    else:
                        tolA = endTol / aSeg
                        tolB = endTol / bSeg
                        if not (-tolA <= tt <= 1 + tolA
                                and -tolB <= ss <= 1 + tolB):
                            continue
                    d = ip.distanceTo(inter)
                    if d < bestD:
                        bestD = d
                        best = ip
            # 被钳制到顶点的候选，若附近真实轮廓上无实际相交，
            # 说明只是延长线近似交点——丢弃，避免修剪切在顶点上
            if clamped and bestD == float('inf'):
                continue
            # 精化点投影回粗折线，保证切点严格落在保留折线上
            segI, tC, nearPt, cumulC = _projectOnPolyline(best, t_pts)
            refined.append((nearPt, segI, tC, cumulC, srcObj))

        # 按累积位置排序并去重：同一交点在相邻段/顶点附近会重复登记，
        # 重复项会让区间逻辑把切点选到顶点一侧
        refined.sort(key=lambda x: x[3])
        intersections = []
        for cand in refined:
            if intersections and \
                    cand[3] - intersections[-1][3] < 0.05:
                continue
            intersections.append(cand)

        # 计算点击位置在曲线上的投影位置
        click_proj = self._projectPointOnCurve(clickPoint, t_pts)

        # 找到点击位置所在的两交点区间
        # 交点位置列表
        positions = [it[3] for it in intersections]
        # 找到点击位置前后的交点
        left_idx = -1
        right_idx = -1
        for k, pos in enumerate(positions):
            if pos < click_proj:
                left_idx = k
            elif pos > click_proj and right_idx == -1:
                right_idx = k
                break

        if left_idx == -1 and right_idx == -1:
            self._msg = "点击位置不在交点区间内"
            return False

        # 存储修剪信息
        self._trimObj = trimObj
        self._trimIntersections = intersections
        self._trimLeftIdx = left_idx
        self._trimRightIdx = right_idx
        # 闭合曲线：点击落在首末交点之外的“跨接缝”环绕区间时，被剪的是
        # 跨接缝段（末交点→折线末端→起点→首交点），保留的是首末交点之间
        # 的整段——普通“左/右区间”分支按折线端点裁剪会把不该留的弧段留下
        self._trimWrap = (OffsetCommand._isClosed(trimObj)
                          and (left_idx == -1 or right_idx == -1))
        # 兼容旧字段
        self._boundaryObj = None
        self._boundarySegIdx = -1
        if left_idx >= 0:
            self._intersection = intersections[left_idx][0]
            self._trimSegIdx = intersections[left_idx][1]
        else:
            self._intersection = intersections[right_idx][0]
            self._trimSegIdx = intersections[right_idx][1]
        return True

    def _trimPath(self, keepSide: int) -> QPainterPath:
        """生成修剪后的预览路径。"""
        path = QPainterPath()
        if self._trimObj is None or self._intersection is None:
            return path

        pts = self._getSamplePoints(self._trimObj)
        if len(pts) < 2:
            return path

        # 找到交点在线段上的参数位置
        t_start, t_end = pts[self._trimSegIdx], pts[self._trimSegIdx + 1]
        _, t_param = nearestPointOnSegment(t_start, t_end, self._intersection)

        # 计算交点坐标
        inter_pt = Point3D(
            t_start.x + (t_end.x - t_start.x) * t_param,
            t_start.y + (t_end.y - t_start.y) * t_param,
            t_start.z + (t_end.z - t_start.z) * t_param
        )

        # 计算交点在整条曲线上的累积位置
        seg_lengths = []
        for i in range(len(pts) - 1):
            seg_lengths.append(pts[i].distanceTo(pts[i + 1]))

        # 累积长度到交点位置
        cumul_before = sum(seg_lengths[:self._trimSegIdx])
        cumul_at_inter = cumul_before + seg_lengths[self._trimSegIdx] * t_param
        total_length = sum(seg_lengths)

        if keepSide == 1:  # 保留起点侧
            # 从起点到交点
            if self._trimSegIdx > 0:
                path.moveTo(pts[0].x, pts[0].y)
                for i in range(self._trimSegIdx):
                    path.lineTo(pts[i + 1].x, pts[i + 1].y)
            else:
                path.moveTo(pts[0].x, pts[0].y)
            path.lineTo(inter_pt.x, inter_pt.y)
        elif keepSide == 2:  # 保留终点侧
            # 从交点到终点
            path.moveTo(inter_pt.x, inter_pt.y)
            for i in range(self._trimSegIdx + 1, len(pts)):
                path.lineTo(pts[i].x, pts[i].y)
        else:
            # 未确定保留侧：显示整条线段 + 交点标记
            path.moveTo(pts[0].x, pts[0].y)
            for i in range(len(pts) - 1):
                path.lineTo(pts[i + 1].x, pts[i + 1].y)
            # 在交点处画个小圆圈标记
            path.addEllipse(inter_pt.x - 3, inter_pt.y - 3, 6, 6)

        return path

    def _refreshPreview(self) -> None:
        if self._intersection is not None:
            path = self._trimPath(self._keepSide)
            self._setPreviewPath(path)
        else:
            self._setPreviewPath(QPainterPath())

    # ---------- 交互 ----------

    def onMousePress(self, wp: Point3D, mods) -> None:
        self._msg = ""
        if self._mode == 1 and self._pickState == "waitBoundary":
            # 边界修剪模式：选择边界曲线
            pick = self._pickEdge(wp)
            if pick is not None:
                self._boundaryObj, self._boundarySegIdx = pick
                self._pickState = "waitTrim"
            return
        if self._pickState == "waitTrim":
            pick = self._pickEdge(wp)
            if pick is None:
                return
            if self._mode == 0:
                # 快速修剪模式：自动找交点并执行修剪
                if self._findIntersectionForQuickTrim(pick[0], pick[1], wp):
                    # 根据点击位置自动判断保留侧
                    self._keepSide = self._determineKeepSide(wp)
                    # 直接执行修剪
                    self._confirm()
                else:
                    self._msg = "未找到与其他曲线的交点"
            else:
                # 边界修剪模式：计算与边界曲线的交点
                self._trimObj, self._trimSegIdx = pick
                if self._computeIntersection():
                    self._pickState = "waitSide"
                    self._keepSide = 0
                    self._refreshPreview()
                else:
                    self._trimObj = None
                    self._trimSegIdx = -1
            return
        if self._pickState == "waitSide":
            # 边界修剪模式：点击确定保留侧
            if self._intersection is None:
                return
            self._keepSide = self._determineKeepSide(wp)
            self._refreshPreview()
            self._pickState = "ready"
            return

    def _determineKeepSide(self, clickPoint: Point3D) -> int:
        """根据点击位置判断保留哪一侧（1=起点侧 2=终点侧）。"""
        if self._trimObj is None or self._intersection is None:
            return 1
        pts = self._getSamplePoints(self._trimObj)
        # 计算点击位置在曲线上的投影位置
        click_proj = self._projectPointOnCurve(clickPoint, pts)
        # 计算交点在曲线上的位置
        t_start, t_end = pts[self._trimSegIdx], pts[self._trimSegIdx + 1]
        _, t_param = nearestPointOnSegment(t_start, t_end, self._intersection)
        seg_lengths = [pts[i].distanceTo(pts[i + 1]) for i in range(len(pts) - 1)]
        cumul_before = sum(seg_lengths[:self._trimSegIdx])
        cumul_at_inter = cumul_before + seg_lengths[self._trimSegIdx] * t_param
        # 点击位置在交点之前就修剪掉起点侧（保留终点侧），反之亦然
        return 2 if click_proj < cumul_at_inter else 1

    def _projectPointOnCurve(self, point: Point3D, pts: List[Point3D]) -> float:
        """计算点在曲线上的投影位置（累积长度）。"""
        if len(pts) < 2:
            return 0.0
        best_proj = 0.0
        best_dist = float('inf')
        cumul = 0.0
        for i in range(len(pts) - 1):
            near, t = nearestPointOnSegment(pts[i], pts[i + 1], point)
            d = near.distanceTo(point)
            if d < best_dist:
                best_dist = d
                best_proj = cumul + pts[i].distanceTo(pts[i + 1]) * t
            cumul += pts[i].distanceTo(pts[i + 1])
        return best_proj

    def onMouseMove(self, wp: Point3D, mods) -> None:
        if self._panel is not None:
            self._panel.setXYZ(wp.x, wp.y, wp.z)

    def onRightClick(self, wp: Point3D) -> bool:
        # 右键退出修剪命令
        self.finish()
        return True

    def onEnter(self) -> bool:
        # 回车退出修剪命令
        self.finish()
        return True

    # ---------- 执行 ----------

    def _refreshPreview(self) -> None:
        if self._intersection is not None:
            path = self._trimPath(self._keepSide)
            self._setPreviewPath(path)
        else:
            self._setPreviewPath(QPainterPath())

    def _confirm(self) -> bool:
        if self._trimObj is None or self._intersection is None:
            self._msg = "未完成修剪选择"
            return False

        pts = self._getSamplePoints(self._trimObj)
        if len(pts) < 2:
            return False

        # 快速修剪模式：支持修剪中间段（两交点之间），返回多段
        if self._mode == 0 and hasattr(self, '_trimIntersections') and self._trimIntersections:
            segments = self._buildTrimmedPoints(pts)
            # 过滤过短的段
            segments = [s for s in segments if len(s) >= 2]
            if not segments:
                self._msg = "修剪后线段过短"
                return False
            self._updateObjectMultiSegments(self._trimObj, segments)
        else:
            # 边界修剪模式：单交点修剪
            t_start, t_end = pts[self._trimSegIdx], pts[self._trimSegIdx + 1]
            _, t_param = nearestPointOnSegment(t_start, t_end, self._intersection)
            inter_pt = Point3D(
                t_start.x + (t_end.x - t_start.x) * t_param,
                t_start.y + (t_end.y - t_start.y) * t_param,
                t_start.z + (t_end.z - t_start.z) * t_param
            )
            new_pts: List[Point3D] = []
            if self._keepSide == 1:
                new_pts = pts[:self._trimSegIdx + 1]
                new_pts.append(inter_pt)
            elif self._keepSide == 2:
                new_pts = [inter_pt] + pts[self._trimSegIdx + 1:]
            else:
                self._msg = "未选择保留侧"
                return False
            if len(new_pts) < 2:
                self._msg = "修剪后线段过短"
                return False
            self._updateObjectPoints(self._trimObj, new_pts)

        self._doc.notifyObjectChanged(self._trimObj.id())

        # 不退出命令，重置状态继续修剪
        self._resetForNextTrim()
        return True

    def _updateObjectMultiSegments(self, obj, segments: List[List[Point3D]]) -> None:
        """修剪后产生多段时：第一段更新原对象，其余段新建对象。"""
        if not segments:
            return
        # 第一段更新原对象
        self._updateObjectPoints(obj, segments[0])
        # 其余段新建对象
        if self._doc is not None:
            from core.vector.poly_curve_object import PolyCurveObject
            for seg_pts in segments[1:]:
                poly = PolyCurveObject()
                poly.addSegment({"points": [[p.x, p.y, p.z] for p in seg_pts]})
                self._doc.addObject(poly)

    def _buildTrimmedPoints(self, pts: List[Point3D]) -> List[List[Point3D]]:
        """根据交点区间构建修剪后的点列段（保留两端，删除中间段）。

        返回多段点列：两边都有交点时返回两段，避免交点间被直线相连；
        闭合曲线点击在跨接缝环绕区间时（_trimWrap），删除跨缝段，
        保留首末交点之间的整段（一段）。
        """
        intersections = self._trimIntersections
        left_idx = self._trimLeftIdx
        right_idx = self._trimRightIdx

        # 计算交点坐标
        def interPoint(inter_info):
            inter, seg_idx, t_param = inter_info[0], inter_info[1], inter_info[2]
            t_start, t_end = pts[seg_idx], pts[seg_idx + 1]
            return Point3D(
                t_start.x + (t_end.x - t_start.x) * t_param,
                t_start.y + (t_end.y - t_start.y) * t_param,
                t_start.z + (t_end.z - t_start.z) * t_param
            )

        # 闭合环绕区间：剪掉跨接缝段，保留首交点→末交点
        if getattr(self, "_trimWrap", False):
            first_inter, first_seg = intersections[0][0], intersections[0][1]
            last_inter, last_seg = intersections[-1][0], intersections[-1][1]
            # 退化保护：只剩一个交点（如另一交点处的曲线端点触碰被漏判）
            # 时环绕修剪会删掉几乎整个闭合曲线——放弃修剪，保留原状
            if intersections[0] is intersections[-1] or (
                    first_seg == last_seg
                    and first_inter.distanceTo(last_inter) < 1e-6):
                return []
            piece = ([first_inter]
                     + pts[first_seg + 1: last_seg + 1] + [last_inter])
            return [piece] if len(piece) >= 2 else []

        if left_idx == -1:
            # 只有右侧交点：修剪起点到右侧交点
            right_inter = interPoint(intersections[right_idx])
            right_seg = intersections[right_idx][1]
            return [[right_inter] + pts[right_seg + 1:]]
        elif right_idx == -1:
            # 只有左侧交点：修剪左侧交点到终点
            left_inter = interPoint(intersections[left_idx])
            left_seg = intersections[left_idx][1]
            return [pts[:left_seg + 1] + [left_inter]]
        else:
            # 两边都有交点：分成两段，避免交点间直线相连
            left_inter = interPoint(intersections[left_idx])
            left_seg = intersections[left_idx][1]
            right_inter = interPoint(intersections[right_idx])
            right_seg = intersections[right_idx][1]
            seg1 = pts[:left_seg + 1] + [left_inter]
            seg2 = [right_inter] + pts[right_seg + 1:]
            return [seg1, seg2]

    def _resetForNextTrim(self) -> None:
        """修剪完成后重置状态，准备下一次修剪（右键退出）。"""
        self._boundaryObj = None
        self._trimObj = None
        self._boundarySegIdx = -1
        self._trimSegIdx = -1
        self._intersection = None
        self._keepSide = 0
        self._trimIntersections = []
        self._trimLeftIdx = -1
        self._trimRightIdx = -1
        self._trimWrap = False
        self._pickState = "waitTrim" if self._mode == 0 else "waitBoundary"
        self._msg = ""
        self._setPreviewPath(QPainterPath())

    def _updateObjectPoints(self, obj, newPts: List[Point3D]) -> None:
        """更新对象点列（支持直线/曲线/圆等）。

        修剪后的曲线/圆等转为 PolyCurveObject 保留修剪形状，
        避免样条重算导致变形。
        """
        t = obj.type().name
        if t == "LINE":
            lst = obj.points()
            lst.clear()
            lst.extend(newPts)
        else:
            # 曲线/圆/椭圆/弧等：转为 PolyCurveObject 保留修剪形状
            from core.vector.poly_curve_object import PolyCurveObject
            oldId = obj.id()
            oldColor = obj._color
            oldLineWidth = obj._lineWidth
            oldLayerId = obj.layerId()
            poly = PolyCurveObject()
            poly.addSegment({"points": [[p.x, p.y, p.z] for p in newPts]})
            poly._id = oldId  # 保留旧 ID
            poly._color = oldColor
            poly._lineWidth = oldLineWidth
            poly._layerId = oldLayerId
            if self._doc is not None:
                self._doc.removeObject(oldId)
                self._doc.addObject(poly)


# ==================== Array 阵列 ====================

class ArrayCommand(CommandInstance):
    """阵列命令：矩形/圆形/曲线三种模式。

    交互流程：
    1. 选中要阵列的对象
    2. 在参数面板选择模式、调节参数
    3. 曲线阵列时拾取路径曲线
    4. 点击"确认执行"生成阵列
    """

    def name(self) -> str:
        return "Array"

    def commandTitle(self) -> str:
        return "阵列参数"

    def showSnapHints(self) -> bool:
        return False

    def start(self) -> None:
        super().start()
        self._pickState: str = "waitObjects"  # waitObjects/waitCurve/waitCenter/done
        self._selectedObjects = []
        self._curveObj = None
        self._center: Optional[Point3D] = None
        self._msg: str = ""
        self._panel: Optional[ArrayParamPanel] = None
        # 监听文档选择变化：选取阶段用户点选/框选对象时实时同步并刷新预览
        if self._doc is not None:
            self._doc.selectionChanged.connect(self._onSelectionChanged)
            objs = self._doc.selectedObjects()
            if objs:
                self._selectedObjects = objs
                self._pickState = "done"
                # 延迟到面板创建后刷新预览
                from PySide6.QtCore import QTimer
                QTimer.singleShot(0, self._updatePreview)

    def _disconnectSelection(self) -> None:
        if self._doc is not None:
            try:
                self._doc.selectionChanged.disconnect(self._onSelectionChanged)
            except RuntimeError:
                pass

    def finish(self) -> None:
        self._disconnectSelection()
        super().finish()

    def cancel(self) -> None:
        self._disconnectSelection()
        super().cancel()

    def prompt(self) -> str:
        if self._msg:
            return self._msg
        if self._pickState == "waitObjects":
            return "选择要阵列的对象（已选中对象自动使用），或点击拾取"
        if self._pickState == "waitCurve":
            return "拾取路径曲线"
        return "调节参数后点击\"确认执行\"，右键退出"

    def paramPanel(self):
        if self._panel is None:
            self._panel = ArrayParamPanel()
            self._panel.confirmed.connect(self._onConfirmed)
            self._panel.cancelRequested.connect(self._onCancel)
            self._panel.btnPickCurve().clicked.connect(self._onPickCurve)
            self._panel.btnPickCenter().clicked.connect(self._onPickCenter)
            self._panel.paramsChanged.connect(self._updatePreview)
            self._panel.modeChanged.connect(lambda *_: self._updatePreview())
        return self._panel

    def wantsSelectionOnPress(self) -> bool:
        # 选取对象阶段：把左键让给画布做点选/框选，选择变化经
        # selectionChanged 回驱动预览
        return self._pickState == "waitObjects"

    def _onCancel(self) -> None:
        """面板取消：清除已选阵列对象与拾取状态，回到选取阶段，
        命令保持活动，用户可重新点选/框选对象。"""
        self._selectedObjects = []
        self._curveObj = None
        self._center = None
        self._pickState = "waitObjects"
        self._msg = "选择要阵列的对象，或点击拾取"
        if self._panel is not None:
            self._panel.setCurveLabel("请拾取路径曲线")
            self._panel.setCenterLabel("圆心：未拾取")
        self._setPreviewPath(QPainterPath())
        if self._manager is not None:
            self._manager.promptChanged.emit(self.prompt())

    def _onSelectionChanged(self) -> None:
        """文档选择变化（点选/框选）→ 同步阵列对象并刷新预览。"""
        if self._pickState != "waitObjects":
            return
        if self._doc is None:
            return
        objs = self._doc.selectedObjects()
        if objs:
            self._selectedObjects = objs
            self._pickState = "done"
            self._msg = ""
            self._updatePreview()
            if self._manager is not None:
                self._manager.promptChanged.emit(self.prompt())

    # ---------- 面板槽 ----------

    def _onPickCurve(self) -> None:
        self._pickState = "waitCurve"

    def _onPickCenter(self) -> None:
        self._pickState = "waitCenter"
        self._msg = "点击拾取圆心"

    def _onConfirmed(self) -> None:
        self._executeArray()

    # ---------- 预览 ----------

    def _updatePreview(self) -> None:
        """实时预览阵列效果（不实际添加对象）。"""
        if self._panel is None or self._doc is None:
            return
        objs = self._selectedObjects
        if not objs:
            objs = self._doc.selectedObjects()
        if not objs:
            self._setPreviewPath(QPainterPath())
            return

        from ui.view_2d.item_factory import _buildPath
        mode = self._panel.mode()
        previewPath = QPainterPath()

        if mode == 0:
            # 矩形阵列预览
            offsets = self._rectOffsets(objs)
        elif mode == 1:
            # 圆形阵列预览
            offsets = self._circleOffsets(objs)
        elif mode == 2:
            # 曲线阵列预览
            offsets = self._curveOffsets(objs)
        else:
            offsets = []

        for dx, dy, dz, angle in offsets:
            for obj in objs:
                clone = obj.clone()
                if abs(angle) > 1e-9:
                    c = clone.getBoundingBox().center()
                    clone.rotateAround(angle, c)
                clone.translate(dx, dy, dz)
                p = _buildPath(clone)
                previewPath.addPath(p)

        self._setPreviewPath(previewPath)

    def _rectOffsets(self, objs) -> list:
        """计算矩形阵列的偏移列表 [(dx,dy,dz,angle)]。"""
        from core.vector.array_generator import getObjectsBBox
        bb = getObjectsBBox(objs)
        if not bb.isValid():
            return []
        stepX = bb.width() + self._panel.rectGapX()
        stepY = bb.height() + self._panel.rectGapY()
        offsets = []
        for i in range(self._panel.rectX()):
            for j in range(self._panel.rectY()):
                if i == 0 and j == 0:
                    continue
                offsets.append((i * stepX, j * stepY, 0.0, 0.0))
        return offsets

    def _circleOffsets(self, objs) -> list:
        """计算圆形阵列的偏移列表（支持多圈环形阵列）。

        已知半径模式：首圈跳过 i=0（选中对象本身），后续圈从 i=0 开始。
        每圈半径叠加 ring * ringSpacing。
        """
        from core.vector.array_generator import getObjectsCenter
        import math
        objCenter = getObjectsCenter(objs)
        # 未拾取圆心时默认世界原点，先预览出圆形阵列
        center = self._center if self._center is not None else Point3D(0, 0, 0)
        knownR = self._panel.circleKnownR()
        radius = self._panel.circleR()
        ringCount = self._panel.ringCount()
        ringSpacing = self._panel.ringSpacing()

        if knownR:
            vec = Point3D(objCenter.x - center.x, objCenter.y - center.y,
                          objCenter.z - center.z)
            baseRadius = vec.length()
            baseAngle = math.atan2(vec.y, vec.x)
        else:
            if radius < 1e-6:
                return []
            baseRadius = radius
            baseAngle = math.atan2(objCenter.y - center.y, objCenter.x - center.x)

        startRad = math.radians(self._panel.circleStart())
        rangeRad = math.radians(self._panel.circleRange())
        count = self._panel.circleCnt()
        stepAngle = rangeRad / count if count > 1 else 0
        offsets = []
        for ring in range(ringCount):
            ringRadius = baseRadius + ring * ringSpacing
            if ringRadius < 1e-6:
                continue
            # 首圈已知半径模式跳过 i=0，其余圈从 i=0 开始
            isFirstRing = (ring == 0)
            startI = 1 if (knownR and isFirstRing) else 0
            for i in range(startI, count):
                angle = baseAngle + startRad + i * stepAngle
                tx = center.x + ringRadius * math.cos(angle)
                ty = center.y + ringRadius * math.sin(angle)
                dx = tx - objCenter.x
                dy = ty - objCenter.y
                rot = i * stepAngle if self._panel.circleRotate() else 0.0
                offsets.append((dx, dy, 0.0, rot))
        return offsets

    def _curveOffsets(self, objs) -> list:
        """计算曲线阵列的偏移列表。"""
        if self._curveObj is None:
            return []
        from core.vector.array_generator import (getObjectsCenter, _lengthToParam,
                                                  _tangentAt)
        import math
        curvePts = self._getSamplePoints(self._curveObj)
        if len(curvePts) < 2:
            return []
        baseCenter = getObjectsCenter(objs)
        totalLength = sum(curvePts[i].distanceTo(curvePts[i + 1])
                          for i in range(len(curvePts) - 1))
        if totalLength < 1e-6:
            return []
        startLen = totalLength * self._panel.curveStart() / 100.0
        count = self._panel.curveCnt()
        spacing = self._panel.curveSpacing()
        fill = self._panel.curveFill()

        targetLengths = []
        if fill:
            if count < 2:
                return []
            step = (totalLength - startLen) / (count - 1)
            for i in range(count):
                targetLengths.append(startLen + i * step)
        else:
            if spacing <= 0:
                return []
            for i in range(count):
                l = startLen + i * spacing
                if l <= totalLength + 1e-6:
                    targetLengths.append(l)

        points = []
        tangents = []
        for l in targetLengths:
            res = _lengthToParam(curvePts, l)
            if res is None:
                continue
            idx, t, p = res
            points.append(p)
            tangents.append(_tangentAt(curvePts, idx))
        if not points:
            return []

        startPt = points[0]
        offsetVec = Point3D(baseCenter.x - startPt.x, baseCenter.y - startPt.y,
                            baseCenter.z - startPt.z)
        startTan = tangents[0]
        onCurve = self._panel.curveOnCurve()
        rotateTangent = self._panel.curveRotate()
        offsets = []
        for i in range(len(points)):
            pt = points[i]
            tan = tangents[i]
            if onCurve:
                targetPt = pt
            else:
                targetPt = Point3D(pt.x + offsetVec.x, pt.y + offsetVec.y,
                                   pt.z + offsetVec.z)
            dx = targetPt.x - baseCenter.x
            dy = targetPt.y - baseCenter.y
            dz = targetPt.z - baseCenter.z
            rot = 0.0
            if rotateTangent and i > 0:
                a1 = math.atan2(startTan.y, startTan.x)
                a2 = math.atan2(tan.y, tan.x)
                rot = a2 - a1
            offsets.append((dx, dy, dz, rot))
        return offsets

    # ---------- 交互 ----------

    def onMousePress(self, wp: Point3D, mods) -> None:
        self._msg = ""
        if self._pickState == "waitCenter":
            # 拾取圆心
            self._center = wp
            self._pickState = "done"
            if self._panel is not None:
                self._panel.setCenterLabel("圆心：%.1f, %.1f" % (wp.x, wp.y))
            self._updatePreview()
            return
        if self._pickState == "waitObjects":
            # 使用当前选中的对象
            if self._doc is not None:
                objs = self._doc.selectedObjects()
                if objs:
                    self._selectedObjects = objs
                    self._pickState = "done"
                    self._updatePreview()
                else:
                    self._msg = "请先选中要阵列的对象"
        elif self._pickState == "waitCurve":
            # 拾取路径曲线
            pick = self._pickCurveAt(wp)
            if pick is not None:
                self._curveObj = pick
                self._pickState = "done"
                if self._panel is not None:
                    self._panel.setCurveLabel("已拾取路径曲线")
                self._updatePreview()

    def onMouseMove(self, wp: Point3D, mods) -> None:
        if self._panel is not None:
            self._panel.setXYZ(wp.x, wp.y, wp.z)

    def onRightClick(self, wp: Point3D) -> bool:
        self._executeArray()
        return True

    def onEnter(self) -> bool:
        self._executeArray()
        return True

    # ---------- 拾取 ----------

    def _worldPickRadius(self) -> float:
        scale = abs(self._view.transform().m11()) if self._view is not None else 1.0
        return PICK_PIXEL_RADIUS / max(scale, 1e-6)

    def _pickCurveAt(self, wp: Point3D):
        """拾取鼠标附近最近的曲线对象（按点到曲线最近距离判断）。"""
        if self._doc is None:
            return None
        worldR = self._worldPickRadius()
        best = None
        bestDist = worldR
        for obj in self._doc.visibleObjects():
            t = obj.type().name
            if t not in ("LINE", "CURVE", "CIRCLE", "ELLIPSE", "ARC", "POLYCURVE"):
                continue
            pts = self._getSamplePoints(obj)
            if len(pts) < 2:
                continue
            # 计算点到曲线所有线段的最近距离
            for i in range(len(pts) - 1):
                near, _ = nearestPointOnSegment(pts[i], pts[i + 1], wp)
                d = near.distanceTo(wp)
                if d <= bestDist:
                    bestDist = d
                    best = obj
        return best

    @staticmethod
    def _getSamplePoints(obj) -> List[Point3D]:
        """获取对象的采样点列表。"""
        t = obj.type().name
        if t == "LINE":
            return obj.points()
        if t == "CURVE":
            return obj.getSampledPoints()
        if t in ("CIRCLE", "ELLIPSE", "ARC"):
            poly = obj.toPolycurve()
            pts: List[Point3D] = []
            for seg in poly.segments():
                pts.extend([Point3D(p[0], p[1], p[2]) for p in seg.get("points", [])])
            return pts
        if t == "POLYCURVE":
            out: List[Point3D] = []
            for seg in obj.segments():
                out.extend([Point3D(p[0], p[1], p[2]) for p in seg.get("points", [])])
            return out
        return []

    # ---------- 执行 ----------

    def _executeArray(self) -> None:
        if not self._selectedObjects:
            if self._doc is not None:
                self._selectedObjects = self._doc.selectedObjects()
        if not self._selectedObjects:
            self._msg = "请先选中要阵列的对象"
            return
        if self._doc is None or self._panel is None:
            return

        mode = self._panel.mode()
        if mode == 0:
            self._execRect()
        elif mode == 1:
            self._execCircle()
        elif mode == 2:
            self._execCurve()

    def _execRect(self) -> None:
        rectangularArray(self._doc, self._selectedObjects,
                         self._panel.rectX(), self._panel.rectY(),
                         self._panel.rectGapX(), self._panel.rectGapY())
        self._msg = "矩形阵列完成"
        self.finish()

    def _execCircle(self) -> None:
        from core.vector.array_generator import getObjectsCenter
        objCenter = getObjectsCenter(self._selectedObjects)
        # 未拾取圆心时默认世界原点（与预览一致）
        center = self._center if self._center is not None else Point3D(0, 0, 0)
        knownR = self._panel.circleKnownR()
        radius = self._panel.circleR()
        if knownR:
            radius = objCenter.distanceTo(center)
        circularArray(self._doc, self._selectedObjects,
                      center, self._panel.circleCnt(),
                      radius, self._panel.circleStart(),
                      self._panel.circleRange(),
                      self._panel.circleRotate(),
                      knownR,
                      self._panel.ringCount(),
                      self._panel.ringSpacing())
        self._msg = "圆形阵列完成"
        self.finish()

    def _execCurve(self) -> None:
        if self._curveObj is None:
            self._msg = "请先拾取路径曲线"
            return
        curvePts = self._getSamplePoints(self._curveObj)
        if len(curvePts) < 2:
            self._msg = "路径曲线无效"
            return
        curveArray(self._doc, self._selectedObjects, curvePts,
                   self._panel.curveCnt(), self._panel.curveSpacing(),
                   self._panel.curveFill(), self._panel.curveStart(),
                    self._panel.curveRotate(), self._panel.curveOnCurve())
        self._msg = "曲线阵列完成"
        self.finish()


# ==================== Offset 偏移 ====================

class OffsetCommand(CommandInstance):
    """偏移命令：将选中曲线沿法线方向偏移一定距离生成平行曲线。

    交互流程：
    1. 选中要偏移的对象（或启动后点击拾取）
    2. 面板输入偏移距离，鼠标移动决定偏移方向，实时预览
    3. 点击确认生成偏移对象，右键退出
    """

    def name(self) -> str:
        return "Offset"

    def commandTitle(self) -> str:
        return "偏移参数"

    def showSnapHints(self) -> bool:
        return False

    def start(self) -> None:
        super().start()
        self._selectedObjects: List = []
        self._cursor: Optional[Point3D] = None
        self._lockedSide: int = 0          # 0=未锁定 +1/-1=锁定方向，避免圆周附近闪烁
        self._msg: str = ""
        self._panel: Optional[OffsetParamPanel] = None
        if self._doc is not None:
            objs = self._doc.selectedObjects()
            if objs:
                self._selectedObjects = objs
                # 已有选中对象：立即预览（默认正方向偏移）
                from PySide6.QtCore import QTimer
                QTimer.singleShot(0, self._updatePreview)

    def prompt(self) -> str:
        if self._msg:
            return self._msg
        if not self._selectedObjects:
            return "选择要偏移的对象，或点击拾取"
        return "移动鼠标定方向，点击确认偏移，右键退出"

    def paramPanel(self):
        if self._panel is None:
            self._panel = OffsetParamPanel()
            self._panel.distanceChanged.connect(lambda *_: self._updatePreview())
            self._panel.paramsChanged.connect(self._updatePreview)
            self._panel.confirmed.connect(self._onConfirmed)
            self._panel.cancelRequested.connect(self.cancel)
        return self._panel

    # ---------- 面板槽 ----------

    def _onConfirmed(self) -> None:
        self._executeOffset()

    # ---------- 预览 ----------

    def _updateLockedSide(self, wp: Point3D) -> None:
        """根据鼠标位置更新锁定方向，加跨越阈值避免圆周附近闪烁。"""
        if not self._selectedObjects:
            return
        side = self._sideOfObjects(wp)
        threshold = self._worldPickRadius() * 0.5
        if self._lockedSide == 0:
            self._lockedSide = 1 if side >= 0 else -1
        elif abs(side) > threshold:
            newSide = 1 if side >= 0 else -1
            if newSide != self._lockedSide:
                self._lockedSide = newSide

    def _signedDistance(self) -> float:
        """带方向的偏移距离：用锁定方向决定符号，未锁定时默认正方向。"""
        if self._panel is None:
            return 0.0
        dist = self._panel.distance()
        if dist < 1e-9 or not self._selectedObjects:
            return 0.0
        if self._lockedSide == 0:
            return dist
        return self._lockedSide * dist

    def _sideOfObjects(self, p: Point3D) -> float:
        """点 p 相对所选对象集合的侧别（>0 左，<0 右），取最近对象。
        sideOfPoint 返回值的幅值即点到该对象边界的真实距离，直接作权重。"""
        from core.vector.offset_geometry import sideOfPoint
        best = 0.0
        bestDist = float('inf')
        for obj in self._selectedObjects:
            pts = self._getSamplePoints(obj)
            if len(pts) < 2:
                continue
            closed = self._isClosed(obj)
            s = sideOfPoint(pts, p, closed)
            d = abs(s)
            if d < bestDist and d > 1e-12:
                bestDist = d
                best = s
        return best

    def _updatePreview(self) -> None:
        if self._panel is None or not self._selectedObjects:
            self._setPreviewPath(QPainterPath())
            return
        dist = self._signedDistance()
        if abs(dist) < 1e-9:
            self._setPreviewPath(QPainterPath())
            return
        from ui.view_2d.item_factory import _buildPath
        path = QPainterPath()
        for obj in self._selectedObjects:
            for off in self._buildOffsetResults(obj, dist):
                path.addPath(_buildPath(off))
        self._setPreviewPath(path)

    def _buildOffsetResults(self, obj, dist: float) -> List:
        """构建偏移结果对象列表（含双侧/加盖）。

        双侧模式（参考 Rhino 插件 ClipperOffsetCurveBidirectional）：
        - 开放曲线 + 加盖：Clipper 单次偏移生成一条闭合外轮廓
          （双侧+端盖一体，平头盖/圆头盖由面板选择），距离语义为总宽（每侧各半）；
        - 闭合曲线：Clipper 双向偏移（±半宽）得到外/内轮廓，
          默认只保留外轮廓（包围盒最大者），勾选"保留内轮廓"则全部保留；
        - 自相交由 Clipper 自动处理。
        pyclipper 不可用时回退到下方平行线 + 连线盖的旧逻辑。
        """
        results: List = []
        if self._panel is None:
            return results
        both = self._panel.bothSides()
        cap = self._panel.cap()
        closed = self._isClosed(obj)

        # 双侧 +（开放曲线加盖 或 闭合曲线双向偏移）→ Clipper 闭合轮廓
        if both and ((cap and not closed) or closed):
            from core.vector.offset_geometry import clipperOffsetBothSides
            paths = clipperOffsetBothSides(
                self._getSamplePoints(obj), abs(dist), closed,
                capStyle=self._panel.capStyle(),
                keepInner=self._panel.keepInner())
            if paths is None:
                self._msg = "pyclipper 未安装，已回退旧偏移（pip install pyclipper）"
            elif paths:
                from core.vector.poly_curve_object import PolyCurveObject
                for pts in paths:
                    poly = PolyCurveObject()
                    poly.addSegment(
                        {"points": [[p.x, p.y, p.z] for p in pts]})
                    poly._color = obj._color
                    poly._lineWidth = obj._lineWidth
                    results.append(poly)
                return results

        # 单侧 / 双侧不加盖：平行偏移线（原有逻辑）
        dists = [dist, -dist] if both else [dist]
        if cap and not closed:
            # 加盖开放曲线：偏移线 + 端盖首尾相接组合成一条闭合 PolyCurve
            closedPoly = self._buildCappedClosedCurve(obj, dists)
            if closedPoly is not None:
                results.append(closedPoly)
            return results
        for d in dists:
            off = self._buildOffsetObject(obj, d)
            if off is None:
                continue
            results.append(off)

        return results

    @staticmethod
    def _objectEnds(obj):
        """取对象首末采样点（用于加盖连接）。"""
        t = obj.type().name
        if t == "LINE":
            pts = obj.points()
        elif t == "CIRCLE":
            c = obj.center(); r = obj.radius()
            pts = [Point3D(c.x + r, c.y, c.z), Point3D(c.x + r, c.y, c.z)]
        elif t == "ARC":
            c = obj.center(); r = obj.radius()
            sa, ea = obj.startAngle(), obj.endAngle()
            pts = [Point3D(c.x + r * math.cos(sa), c.y + r * math.sin(sa), c.z),
                   Point3D(c.x + r * math.cos(ea), c.y + r * math.sin(ea), c.z)]
        else:
            pts = obj.getSampledPoints() if hasattr(obj, "getSampledPoints") else []
            if not pts and hasattr(obj, "segments"):
                for seg in obj.segments():
                    pts.extend([Point3D(p[0], p[1], p[2]) for p in seg.get("points", [])])
        if len(pts) < 2:
            return None
        return (pts[0], pts[-1])

    def _addCapLines(self, results, obj, offsetEnds) -> None:
        """加盖：原对象首末点与偏移对象首末点连接。
        平头盖=直线；圆头盖=以中点为圆心的半圆弧（凸向曲线外侧）。"""
        srcEnds = self._objectEnds(obj)
        if srcEnds is None:
            return
        srcFirst, srcLast = srcEnds
        roundCap = (self._panel is not None
                    and self._panel.capStyle() == "round")
        srcPts = self._getSamplePoints(obj)
        # 首端盖的"曲线内侧"参考点取第二个采样点，末端盖取倒数第二个
        refFirst = srcPts[1] if len(srcPts) > 1 else None
        refLast = srcPts[-2] if len(srcPts) > 1 else None
        for offFirst, offLast in offsetEnds:
            if offFirst is None or offLast is None:
                continue
            if srcFirst.distanceTo(offFirst) > 1e-6:
                self._addCap(results, obj, srcFirst, offFirst,
                             refFirst, roundCap)
            if srcLast.distanceTo(offLast) > 1e-6:
                self._addCap(results, obj, srcLast, offLast,
                             refLast, roundCap)

    @staticmethod
    def _addCap(results, obj, a: Point3D, b: Point3D,
                interiorRef, roundCap: bool) -> None:
        """生成 a→b 的端盖：平头直线或半圆弧（凸向远离 interiorRef 一侧）。"""
        if not roundCap:
            if a.distanceTo(b) > 1e-6:
                cap = LineObject([Point3D(a.x, a.y, a.z),
                                  Point3D(b.x, b.y, b.z)])
                cap._color = obj._color; cap._lineWidth = obj._lineWidth
                results.append(cap)
            return
        mx, my = (a.x + b.x) * 0.5, (a.y + b.y) * 0.5
        r = a.distanceTo(b) * 0.5
        if r < 1e-9:
            return
        a0 = math.atan2(a.y - my, a.x - mx)
        # 半圆凸向：取远离曲线内侧参考点的一侧
        sign = 1.0
        if interiorRef is not None:
            m1x = mx + r * math.cos(a0 + math.pi / 2)
            m1y = my + r * math.sin(a0 + math.pi / 2)
            d1 = math.hypot(m1x - interiorRef.x, m1y - interiorRef.y)
            d2 = math.hypot(2 * mx - m1x - interiorRef.x,
                            2 * my - m1y - interiorRef.y)
            sign = 1.0 if d1 >= d2 else -1.0
        n = 16  # 半圆离散段数
        pts = [Point3D(mx + r * math.cos(a0 + sign * math.pi * i / n),
                       my + r * math.sin(a0 + sign * math.pi * i / n), a.z)
               for i in range(n + 1)]
        from core.vector.poly_curve_object import PolyCurveObject
        poly = PolyCurveObject()
        poly.addSegment({"points": [[p.x, p.y, p.z] for p in pts]})
        poly._color = obj._color; poly._lineWidth = obj._lineWidth
        results.append(poly)

    @staticmethod
    def _capPoints(a: Point3D, b: Point3D,
                   interiorRef, roundCap: bool) -> List[Point3D]:
        """端盖采样点（a→b）：平头=[a,b]，圆头=半圆弧（凸向远离 interiorRef 一侧）。"""
        if not roundCap:
            return [Point3D(a.x, a.y, a.z), Point3D(b.x, b.y, b.z)]
        mx, my = (a.x + b.x) * 0.5, (a.y + b.y) * 0.5
        r = a.distanceTo(b) * 0.5
        if r < 1e-9:
            return [Point3D(a.x, a.y, a.z), Point3D(b.x, b.y, b.z)]
        a0 = math.atan2(a.y - my, a.x - mx)
        sign = 1.0
        if interiorRef is not None:
            m1x = mx + r * math.cos(a0 + math.pi / 2)
            m1y = my + r * math.sin(a0 + math.pi / 2)
            d1 = math.hypot(m1x - interiorRef.x, m1y - interiorRef.y)
            d2 = math.hypot(2 * mx - m1x - interiorRef.x,
                            2 * my - m1y - interiorRef.y)
            sign = 1.0 if d1 >= d2 else -1.0
        n = 16
        return [Point3D(mx + r * math.cos(a0 + sign * math.pi * i / n),
                        my + r * math.sin(a0 + sign * math.pi * i / n), a.z)
                for i in range(n + 1)]

    @staticmethod
    def _makeClosedPoly(loop: List[Point3D], obj):
        """用首尾相接的点列表构建一条闭合 PolyCurve（强制首尾重合）。"""
        from core.vector.poly_curve_object import PolyCurveObject
        while len(loop) > 2 and loop[0].distanceTo(loop[-1]) < 1e-9:
            loop.pop()
        if len(loop) < 3:
            return None
        loop.append(Point3D(loop[0].x, loop[0].y, loop[0].z))
        poly = PolyCurveObject()
        poly.addSegment({"points": [[p.x, p.y, p.z] for p in loop]})
        poly._color = obj._color
        poly._lineWidth = obj._lineWidth
        return poly

    def _buildCappedClosedCurve(self, obj, dists: List[float]):
        """加盖开放曲线偏移 → 一条闭合 PolyCurve（偏移线 + 端盖首尾相接）。

        单侧：原曲线 + 末端盖 + 偏移线(反向) + 首端盖(反向)
              （以原曲线为底边，偏移线 + 两端盖封顶，形成闭合带）
        双侧：偏移线1 + 末端盖(连两偏移线末) + 偏移线2(反向) + 首端盖(连两偏移线首)
              （不含原曲线，两偏移线 + 两端盖形成闭合外轮廓，与 Clipper 语义一致）
        """
        roundCap = (self._panel is not None
                    and self._panel.capStyle() == "round")
        srcPts = self._getSamplePoints(obj)
        if len(srcPts) < 2:
            return None
        srcFirst, srcLast = srcPts[0], srcPts[-1]
        refFirst = srcPts[1] if len(srcPts) > 2 else None
        refLast = srcPts[-2] if len(srcPts) > 2 else None

        offCurves: List[List[Point3D]] = []
        for d in dists:
            off = self._buildOffsetObject(obj, d)
            if off is None:
                return None
            offPts = self._getSamplePoints(off)
            if len(offPts) < 2:
                return None
            offCurves.append(offPts)

        if len(offCurves) == 1:
            offPts = offCurves[0]
            capEnd = self._capPoints(srcLast, offPts[-1], refLast, roundCap)
            capStart = self._capPoints(srcFirst, offPts[0], refFirst, roundCap)
            loop = list(srcPts)
            loop += capEnd[1:]
            loop += list(reversed(offPts))[1:]
            loop += list(reversed(capStart))[1:]
        else:
            off1, off2 = offCurves[0], offCurves[1]
            capEnd = self._capPoints(off1[-1], off2[-1], refLast, roundCap)
            capStart = self._capPoints(off1[0], off2[0], refFirst, roundCap)
            loop = list(off1)
            loop += capEnd[1:]
            loop += list(reversed(off2))[1:]
            loop += list(reversed(capStart))[1:]
        return self._makeClosedPoly(loop, obj)

    def _buildOffsetObject(self, obj, dist: float):
        """构建单个对象的偏移结果对象（不入文档）。"""
        from core.vector.offset_geometry import (offsetPolyline,
                                                  offsetSampledCurve)
        from core.vector.circle_object import CircleObject
        from core.vector.arc_object import ArcObject
        from core.vector.poly_curve_object import PolyCurveObject
        t = obj.type().name
        if t == "CIRCLE":
            # 左法线对逆时针圆指向圆心，dist>0 向内 → 半径变小
            r = obj.radius() - dist
            if r < 1e-6:
                return None
            c = obj.center()
            o = CircleObject(c.x, c.y, c.z, r)
            o._color = obj._color; o._lineWidth = obj._lineWidth
            return o
        if t == "ARC":
            # 圆弧偏移方向需考虑绕向：span 原始符号即绕向（+逆时针/-顺时针，
            # setThreePoints 保证 span∈(-2π,2π)，不可归一化否则大弧绕向翻转）；
            # 逆时针左法线指向圆心，顺时针指向外，需翻转 dist 使方向与鼠标侧一致
            span = obj.endAngle() - obj.startAngle()
            d = -dist if span < 0 else dist
            r = obj.radius() - d
            if r < 1e-6:
                return None
            c = obj.center()
            o = ArcObject(c.x, c.y, c.z, r,
                          obj.startAngle(), obj.endAngle())
            o._color = obj._color; o._lineWidth = obj._lineWidth
            return o
        if t == "LINE":
            pts = obj.points()
            closed = self._isClosed(obj)
            # 几何闭合（首尾相接）时去掉重复末点，闭合环绕偏移才能正确求交
            if closed and len(pts) >= 3 and \
                    pts[0].distanceTo(pts[-1]) < 1e-6:
                pts = pts[:-1]
            newPts = offsetPolyline(pts, dist, closed)
            if len(newPts) < 2:
                return None
            o = LineObject(newPts)
            o.setClosed(closed)
            o._color = obj._color; o._lineWidth = obj._lineWidth
            return o
        # 曲线/椭圆/弧(采样)/多段曲线：采样后沿法线偏移，生成 PolyCurve
        pts = self._getSamplePoints(obj)
        if len(pts) < 2:
            return None
        closed = self._isClosed(obj)
        if closed and pts[0].distanceTo(pts[-1]) < 1e-6:
            pts = pts[:-1]  # 去掉重复末点，避免环绕段退化
        newPts = offsetSampledCurve(pts, dist, closed)
        if len(newPts) < 2:
            return None
        poly = PolyCurveObject()
        poly.addSegment({"points": [[p.x, p.y, p.z] for p in newPts]})
        poly._color = obj._color; poly._lineWidth = obj._lineWidth
        return poly

    # ---------- 交互 ----------

    def onMousePress(self, wp: Point3D, mods) -> None:
        self._msg = ""
        if not self._selectedObjects:
            # 先点偏移再点曲线：拾取点击位置附近的曲线
            pick = self._pickEdge(wp)
            if pick is not None:
                self._selectedObjects = [pick[0]]
                self._cursor = wp
                self._updateLockedSide(wp)
                self._updatePreview()
                if self._manager is not None:
                    self._manager.promptChanged.emit(self.prompt())
            else:
                self._msg = "未拾取到曲线，请点击要偏移的曲线"
            return
        # 已有对象：点击确认执行
        self._cursor = wp
        self._updateLockedSide(wp)
        self._executeOffset()

    def onMouseMove(self, wp: Point3D, mods) -> None:
        if self._panel is not None:
            self._panel.setXYZ(wp.x, wp.y, wp.z)
        if not self._selectedObjects:
            return
        self._cursor = wp
        self._updateLockedSide(wp)
        self._updatePreview()

    def onRightClick(self, wp: Point3D) -> bool:
        self.finish()
        return True

    def onEnter(self) -> bool:
        self._executeOffset()
        return True

    # ---------- 执行 ----------

    def _executeOffset(self) -> None:
        if not self._selectedObjects:
            if self._doc is not None:
                self._selectedObjects = self._doc.selectedObjects()
        if not self._selectedObjects:
            self._msg = "请先选中要偏移的对象"
            return
        dist = self._signedDistance()
        if abs(dist) < 1e-9:
            self._msg = "偏移距离过小"
            return
        for obj in self._selectedObjects:
            for off in self._buildOffsetResults(obj, dist):
                if self._doc is not None:
                    self._doc.addObject(off)
        self._msg = "偏移完成"
        self.finish()

    # ---------- 辅助 ----------

    def _worldPickRadius(self) -> float:
        scale = abs(self._view.transform().m11()) if self._view is not None else 1.0
        return PICK_PIXEL_RADIUS / max(scale, 1e-6)

    def _pickEdge(self, wp: Point3D):
        """拾取鼠标附近最近的对象（支持直线/曲线/圆等）。"""
        if self._doc is None:
            return None
        worldR = self._worldPickRadius()
        best = None
        bestDist = worldR
        for obj in self._doc.visibleObjects():
            pts = self._getSamplePoints(obj)
            if len(pts) < 2:
                continue
            for i in range(len(pts) - 1):
                near, _ = nearestPointOnSegment(pts[i], pts[i + 1], wp)
                d = near.distanceTo(wp)
                if d <= bestDist:
                    bestDist = d
                    best = (obj, i)
        return best

    @staticmethod
    def _isClosed(obj) -> bool:
        t = obj.type().name
        if t == "CIRCLE":
            return True
        if t == "ELLIPSE":
            return True
        if t == "LINE":
            if obj.isClosed():
                return True
        elif t in ("CURVE", "POLYCURVE"):
            if hasattr(obj, "isClosed") and obj.isClosed():
                return True
        else:
            return False
        # 几何闭合：首末采样点重合（如直线画的三角形首尾相接）也视为闭合，
        # 避免端盖/偏移按开放曲线处理时连回原始曲线
        pts = OffsetCommand._getSamplePoints(obj)
        if len(pts) >= 3 and pts[0].distanceTo(pts[-1]) < 1e-6:
            return True
        return False

    @staticmethod
    def _getSamplePoints(obj) -> List[Point3D]:
        """获取对象的采样点列表（支持直线/曲线/圆/椭圆/弧）。"""
        t = obj.type().name
        if t == "LINE":
            return obj.points()
        if t == "CURVE":
            return obj.getSampledPoints()
        if t in ("CIRCLE", "ELLIPSE", "ARC"):
            poly = obj.toPolycurve()
            pts: List[Point3D] = []
            for seg in poly.segments():
                pts.extend([Point3D(p[0], p[1], p[2]) for p in seg.get("points", [])])
            return pts
        if t == "POLYCURVE":
            out: List[Point3D] = []
            for seg in obj.segments():
                out.extend([Point3D(p[0], p[1], p[2]) for p in seg.get("points", [])])
            return out
        return []

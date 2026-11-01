"""曲线编辑控制器：底部“编辑曲线”开关激活后，
选中曲线即显示控制点，支持点选/框选控制点、直接拖动移动；
配合操作轴可对选中的控制点做移动/旋转/缩放变换。

2D 画布与 3D/浮雕视口共用（与操作轴同款双适配器模式）：
适配器提供 project(世界→屏幕)、screenToPlane(屏幕→世界)、redraw。
"""
from __future__ import annotations
import math
from typing import Dict, Optional, Set
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPen, QBrush

from core.vector.geometry_types import Point3D, BoundingBox3D

_PICK_PX = 7.0        # 控制点拾取半径（屏幕像素）
_RADIUS = 4.5         # 控制点圆半径（屏幕像素），比点对象(3.0)稍大
_HANDLE_PX = 7.0      # 手柄圆点拾取半径（屏幕像素）
_KNOB_R = 3.2         # 手柄圆点半径（屏幕像素）
_CP_COLOR = QColor(21, 182, 201)    # 未选中描边/选中填充（同命令预览青色）
_CP_LINE = QColor(21, 182, 201, 150)  # 手柄连线（半透明青）
_WHITE = QColor(255, 255, 255)
_BLACK = QColor(0, 0, 0)


class CurveEditController(QObject):
    """曲线控制点编辑状态机。"""

    changed = Signal()   # 目标/控制点选择变化（驱动操作轴刷新）

    def __init__(self, doc, parent=None) -> None:
        super().__init__(parent)
        self._doc = doc
        self._enabled = False
        self._obj = None                     # 当前编辑目标对象
        self._sel: Set[int] = set()          # 选中控制点索引
        self._adapters = []                  # 已挂接的视图适配器
        # 拖动（_dragKind: 'point'=控制点 / 'handle'=贝塞尔手柄）
        self._dragging = False
        self._dragKind = "point"
        self._dragIdx = -1
        self._dragSide = "in"
        self._dragSmooth = False          # 手柄平滑拖动（共线跟随），Alt 断开
        self._startWorld: Optional[Point3D] = None
        self._origPoints: Dict[int, Point3D] = {}   # 拖动开始时选中控制点原坐标
        self._origHandles: Dict[str, Optional[Point3D]] = {}  # 手柄拖动起始偏移
        # 悬停
        self._hover = -1

        doc.selectionChanged.connect(self._syncTarget)
        doc.objectRemoved.connect(self._onObjectRemoved)
        doc.objectChanged.connect(self._onObjectChanged)

    def setDocument(self, doc) -> None:
        """换绑文档（多文件切换）：断开旧文档信号并复位编辑目标。"""
        if doc is self._doc:
            return
        try:
            self._doc.selectionChanged.disconnect(self._syncTarget)
            self._doc.objectRemoved.disconnect(self._onObjectRemoved)
            self._doc.objectChanged.disconnect(self._onObjectChanged)
        except (RuntimeError, TypeError):
            pass
        self._doc = doc
        self._obj = None
        self._sel.clear()
        self._hover = -1
        self._dragging = False
        self._dragKind = "point"
        self._dragIdx = -1
        self._origHandles = {}
        doc.selectionChanged.connect(self._syncTarget)
        doc.objectRemoved.connect(self._onObjectRemoved)
        doc.objectChanged.connect(self._onObjectChanged)
        if self._enabled:
            self._syncTarget()
        self.changed.emit()
        self._redrawAll()

    # ---------------- 开关 / 生命周期 ----------------

    def setEnabled(self, on: bool) -> None:
        on = bool(on)
        if on == self._enabled:
            return
        self._enabled = on
        if not on:
            self._obj = None
            self._sel.clear()
            self._dragging = False
            self._dragKind = "point"
            self._origHandles = {}
            self._hover = -1
        else:
            self._syncTarget()
        self.changed.emit()
        self._redrawAll()

    def isEnabled(self) -> bool:
        return self._enabled

    def isActive(self) -> bool:
        """开关打开且已选中可编辑曲线。"""
        return self._enabled and self._obj is not None

    def isDragging(self) -> bool:
        return self._dragging

    def attach(self, adapter) -> None:
        self._adapters.append(adapter)

    @staticmethod
    def _editable(obj) -> bool:
        return (obj is not None and obj.controlPointCount() > 0
                and hasattr(obj, "setControlPoint"))

    @staticmethod
    def _isParametric(obj) -> bool:
        """圆/椭圆/圆弧等参数化对象：编辑时需先转成自由曲线。"""
        if obj is None:
            return False
        try:
            return obj.toPolycurve() is not None
        except AttributeError:
            return False

    def _convertTarget(self, anchors) -> bool:
        """破坏性编辑：把圆/椭圆/圆弧替换为 PolyCurveObject（保持外观与样式），
        并把 anchors（旧控制点世界坐标）映射为新对象上最近的控制点索引。"""
        obj = self._obj
        poly = obj.toPolycurve()
        if poly is None:
            return False
        poly.setLayerId(obj.layerId())
        poly.setColor(obj.color())
        poly.setLineWidth(obj.lineWidth())
        poly.setSelected(True)
        self._doc.removeObject(obj.id())
        self._doc.addObject(poly)
        # addObject 套用活动层默认样式，需恢复原样式
        poly.setLayerId(obj.layerId())
        poly.setColor(obj.color())
        poly.setLineWidth(obj.lineWidth())
        self._obj = poly
        self._sel = set()
        for op in anchors:
            best, bd = -1, float('inf')
            for i in range(poly.controlPointCount()):
                p = poly.getControlPoint(i)
                d = op.distanceTo(p)
                if d < bd:
                    bd, best = d, i
            if best >= 0:
                self._sel.add(best)
        self.changed.emit()
        self._redrawAll()
        return True

    # ---------------- 目标与控制点 ----------------

    def target(self):
        return self._obj

    def _syncTarget(self) -> None:
        """开关打开时：选中可编辑曲线即同步为目标并显示控制点；
        开关关闭时不追踪目标（控制点不显示，点击也走对象选择）。"""
        obj = None
        if self._enabled:
            for o in self._doc.selectedObjects():
                if self._editable(o):
                    obj = o
                    break
        if obj is not self._obj:
            self._obj = obj
            self._sel.clear()
        else:
            n = obj.controlPointCount() if obj else 0
            self._sel = {i for i in self._sel if i < n}
        self.changed.emit()
        self._redrawAll()

    def tryPressControlPoint(self, adapter, pos, mods) -> bool:
        """开关打开且命中控制点才进入控制点拖动；
        开关关闭时控制点不可见（_obj 为空），直接返回 False 交给外层对象选择。"""
        return self.handleMousePress(adapter, pos, mods)

    def _onObjectRemoved(self, oid: str) -> None:
        if self._obj is not None and oid == self._obj.id():
            self._obj = None
            self._sel.clear()
            self._dragging = False
            self._dragKind = "point"
            self.changed.emit()
            self._redrawAll()

    def _onObjectChanged(self, oid: str) -> None:
        if self._obj is not None and oid == self._obj.id():
            self._redrawAll()

    def controlCount(self) -> int:
        return self._obj.controlPointCount() if self._obj else 0

    def _controlPoints(self):
        if self._obj is None:
            return []
        return [self._obj.getControlPoint(i)
                for i in range(self._obj.controlPointCount())]

    def selectedIndices(self) -> Set[int]:
        return set(self._sel)

    def hasSelectedControls(self) -> bool:
        return self.isActive() and bool(self._sel)

    # ---------------- 拾取 / 选择 / 拖动 ----------------

    @staticmethod
    def _dragPoint(adapter, pos):
        """拖动取点：优先 z=0 平面（与操作轴拖动一致），
        透视下焦点平面映射非线性会导致控制点不跟手，故不用 screenToPlane。"""
        if hasattr(adapter, "dragPlanePoint"):
            return adapter.dragPlanePoint(pos.x(), pos.y())
        return adapter.screenToPlane(pos.x(), pos.y(), (0.0, 0.0, 0.0))

    def _handleHit(self, adapter, pos):
        """选中控制点的贝塞尔手柄命中检测（优先于控制点拾取）。
        返回 (点索引, 'in'/'out') 或 None。"""
        if not self.isActive() or not self._sel:
            return None
        obj = self._obj
        if not hasattr(obj, "getHandleOffset"):
            return None
        best, bestD = None, _HANDLE_PX
        for i in self._sel:
            a = obj.getControlPoint(i)
            for side in ("out", "in"):
                off = obj.getHandleOffset(i, side)
                if off is None:
                    continue
                s = adapter.project((a.x + off.x, a.y + off.y, a.z + off.z))
                if s is None:
                    continue
                d = math.hypot(pos.x() - s[0], pos.y() - s[1])
                if d <= bestD:
                    bestD, best = d, (i, side)
        return best

    def handleDoubleClick(self, adapter, pos, mods) -> bool:
        """双击控制点：恢复两侧手柄共线（平滑）。命中返回 True 消费事件。"""
        if not self.isActive():
            return False
        idx = self._pick(adapter, pos)
        if idx < 0:
            return False
        self._doc.beginEdit()
        self._restoreSmooth(idx)
        self._doc.endEdit()
        self._notify()   # notifyObjectChanged 使曲线图形项失效重建 + 全适配器重绘
        self.changed.emit()
        return True

    def _restoreSmooth(self, idx: int) -> None:
        """双击控制点：恢复两侧手柄共线（平滑）。方向取两侧行进方向的
        角平分线，各自保留原长度；已共线（含全自动）则不动。"""
        obj = self._obj
        if obj is None or not hasattr(obj, "getHandleOffset"):
            return
        hin = obj.getHandleOffset(idx, "in")
        hout = obj.getHandleOffset(idx, "out")
        if hin is None or hout is None:
            return
        li = math.hypot(hin.x, hin.y)
        lo = math.hypot(hout.x, hout.y)
        if li < 1e-12 or lo < 1e-12:
            return
        if abs(hin.x * hout.y - hin.y * hout.x) / (li * lo) < 1e-6:
            return   # 已共线
        # 行进方向：出柄向前、入柄向后 → 差向量 = 两侧方向的角平分线
        dx = hout.x / lo - hin.x / li
        dy = hout.y / lo - hin.y / li
        ln = math.hypot(dx, dy)
        if ln < 1e-9:   # 两柄同向的退化角点：取出行方向
            dx, dy, ln = hout.x / lo, hout.y / lo, 1.0
        obj.setHandleOffset(idx, "out",
                            Point3D(dx / ln * lo, dy / ln * lo, hout.z))
        obj.setHandleOffset(idx, "in",
                            Point3D(-dx / ln * li, -dy / ln * li, hin.z))

    def handleMousePress(self, adapter, pos, mods) -> bool:
        """命中手柄则拖手柄；命中控制点则选中并拖点；未命中返回 False。"""
        if not self.isActive():
            return False
        hit = self._handleHit(adapter, pos)
        if hit is not None:
            idx, side = hit
            self._doc.beginEdit()   # 一次手柄拖动合并为一次撤销
            self._dragging = True
            self._dragKind = "handle"
            self._dragIdx = idx
            self._dragSide = side
            sw = self._dragPoint(adapter, pos)
            self._startWorld = Point3D(sw[0], sw[1], sw[2]) if sw else Point3D()
            hin = self._obj.getHandleOffset(idx, "in")
            hout = self._obj.getHandleOffset(idx, "out")
            self._origHandles = {"in": hin, "out": hout}
            # 默认平滑拖动：两侧共线（自动柄/对称柄）时对侧跟随方向、
            # 保持自身长度；Alt 拖只动单柄（对侧不动），已是角点（不共线）
            # 也独立
            collinear = False
            if hin is not None and hout is not None:
                ln = math.hypot(hin.x, hin.y) * math.hypot(hout.x, hout.y)
                if ln > 1e-12:
                    cross = hin.x * hout.y - hin.y * hout.x
                    collinear = abs(cross) / ln < 1e-6
            self._dragSmooth = collinear and not (mods & Qt.AltModifier)
            self.changed.emit()
            adapter.redraw()
            return True
        idx = self._pick(adapter, pos)
        if idx < 0:
            self._hover = -1
            return False
        self._doc.beginEdit()   # 一次控制点拖动（含破坏性转曲线）合并为一次撤销
        if self._isParametric(self._obj):
            # 破坏性编辑：按下控制点即把圆/椭圆/圆弧转为自由曲线
            anchor = self._obj.getControlPoint(idx)
            if self._convertTarget([Point3D(anchor.x, anchor.y, anchor.z)]):
                idx = next(iter(self._sel)) if self._sel else -1
                if idx < 0:
                    # 转换后无有效控制点：结束会话再退出，避免撤销事务悬挂
                    self._doc.endEdit()
                    return False
        if mods & Qt.ControlModifier:
            if idx in self._sel:
                self._sel.discard(idx)
            else:
                self._sel.add(idx)
        elif mods & Qt.ShiftModifier:
            self._sel.add(idx)
        else:
            if not (len(self._sel) == 1 and idx in self._sel):
                self._sel = {idx}
        self._dragging = True
        self._dragKind = "point"
        self._dragIdx = idx
        sw = self._dragPoint(adapter, pos)
        self._startWorld = Point3D(sw[0], sw[1], sw[2]) if sw else Point3D()
        self._origPoints = {
            i: Point3D(p.x, p.y, p.z)
            for i, p in enumerate(self._controlPoints()) if i in self._sel}
        self.changed.emit()
        adapter.redraw()
        return True

    def handleMouseMove(self, adapter, pos, mods) -> bool:
        """拖动中实时移动选中控制点/手柄；否则更新悬停高亮。"""
        if not self.isActive():
            return False
        if self._dragging and self._dragKind == "handle":
            sw = self._dragPoint(adapter, pos)
            if sw is None:
                return True
            dx = sw[0] - self._startWorld.x
            dy = sw[1] - self._startWorld.y
            orig = self._origHandles.get(self._dragSide)
            if orig is not None:
                new = Point3D(orig.x + dx, orig.y + dy, orig.z)
                self._obj.setHandleOffset(self._dragIdx, self._dragSide, new)
                if self._dragSmooth:
                    other = "out" if self._dragSide == "in" else "in"
                    o = self._origHandles.get(other)
                    ln = math.hypot(new.x, new.y)
                    if o is not None and ln > 1e-12:
                        k = math.hypot(o.x, o.y) / ln
                        self._obj.setHandleOffset(
                            self._dragIdx, other,
                            Point3D(-new.x * k, -new.y * k, -new.z * k))
            self._doc.notifyObjectChanged(self._obj.id())
            adapter.redraw()
            return True
        if self._dragging:
            sw = self._dragPoint(adapter, pos)
            if sw is None:
                return True
            dx = sw[0] - self._startWorld.x
            dy = sw[1] - self._startWorld.y
            for i, orig in self._origPoints.items():
                self._obj.setControlPoint(
                    i, Point3D(orig.x + dx, orig.y + dy, orig.z))
            self._doc.notifyObjectChanged(self._obj.id())
            adapter.redraw()
            return True
        h = self._pick(adapter, pos)
        if h != self._hover:
            self._hover = h
            adapter.redraw()
        return False

    def handleMouseRelease(self, adapter, pos) -> bool:
        if not self._dragging:
            return False
        self._dragging = False
        self._dragKind = "point"
        self._dragIdx = -1
        self._origPoints = {}
        self._origHandles = {}
        self._doc.endEdit()
        self.changed.emit()
        adapter.redraw()
        return True

    def boxSelect(self, adapter, screenRect: QRectF, mods) -> None:
        """屏幕矩形框选控制点（Shift 加选 / Ctrl 减选）。"""
        if not self.isActive():
            return
        hits = set()
        for i, p in enumerate(self._controlPoints()):
            s = adapter.project((p.x, p.y, p.z))
            if s is not None and screenRect.contains(QPointF(s[0], s[1])):
                hits.add(i)
        if mods & Qt.ControlModifier:
            self._sel -= hits
        elif mods & Qt.ShiftModifier:
            self._sel |= hits
        else:
            if not hits:
                return   # 同对象框选约定：空框保留现有选择
            self._sel = hits
        self.changed.emit()
        adapter.redraw()

    def _pick(self, adapter, pos) -> int:
        best, bestD = -1, _PICK_PX
        for i, p in enumerate(self._controlPoints()):
            s = adapter.project((p.x, p.y, p.z))
            if s is None:
                continue
            d = math.hypot(pos.x() - s[0], pos.y() - s[1])
            if d <= bestD:
                bestD, best = d, i
        return best

    # ---------------- 绘制 ----------------

    def paint(self, painter, adapter) -> None:
        """在视口叠加绘制控制点（屏幕坐标圆，与点对象同款白圆青描边），
        仅"编辑曲线"开关打开且选中可编辑曲线时显示，选中/悬停高亮。
        选中控制点额外绘制贝塞尔手柄（连线 + 圆点，先画手柄再画点，
        锚点圆盖住连线接头）。"""
        if not self.isActive():
            return
        painter.save()
        painter.resetTransform()
        self._paintHandles(painter, adapter)
        for i, p in enumerate(self._controlPoints()):
            s = adapter.project((p.x, p.y, p.z))
            if s is None:
                continue
            x, y = s[0], s[1]
            selected = i in self._sel
            hover = i == self._hover
            if selected:
                painter.setPen(QPen(_WHITE, 1.0))
                painter.setBrush(QBrush(_CP_COLOR))
            else:
                painter.setPen(QPen(_CP_COLOR, 1.2))
                painter.setBrush(QBrush(_WHITE))
            painter.drawEllipse(QPointF(x, y), _RADIUS, _RADIUS)
            if hover:
                painter.setPen(QPen(_BLACK, 1.2))
                painter.drawEllipse(QPointF(x, y), _RADIUS, _RADIUS)
        painter.restore()

    def _paintHandles(self, painter, adapter) -> None:
        """选中控制点的贝塞尔手柄：锚点→柄点连线 + 实心青圆点。"""
        obj = self._obj
        if not self._sel or not hasattr(obj, "getHandleOffset"):
            return
        painter.setPen(QPen(_CP_LINE, 1.0))
        painter.setBrush(QBrush(_CP_COLOR))
        knobs = []
        for i in self._sel:
            a = obj.getControlPoint(i)
            sa = adapter.project((a.x, a.y, a.z))
            if sa is None:
                continue
            for side in ("in", "out"):
                off = obj.getHandleOffset(i, side)
                if off is None:
                    continue
                sk = adapter.project((a.x + off.x, a.y + off.y, a.z + off.z))
                if sk is None:
                    continue
                painter.drawLine(QPointF(sa[0], sa[1]), QPointF(sk[0], sk[1]))
                knobs.append((sk[0], sk[1]))
        painter.setPen(QPen(_WHITE, 0.8))
        painter.setBrush(QBrush(_CP_COLOR))
        for x, y in knobs:
            painter.drawEllipse(QPointF(x, y), _KNOB_R, _KNOB_R)

    # ---------------- 供操作轴变换选中控制点 ----------------

    def selectedBBox(self) -> BoundingBox3D:
        bb = BoundingBox3D()
        if self._obj is not None:
            for i in self._sel:
                bb.expand(self._obj.getControlPoint(i))
        return bb

    def targetBBox(self) -> BoundingBox3D:
        if self._obj is None:
            return BoundingBox3D()
        return self._obj.getBoundingBox()

    def _convertIfParametric(self) -> None:
        """gizmo 变换前若目标是圆/椭圆/圆弧则先转曲线（保持选中控制点映射）。"""
        if self._obj is None or not self._sel or not self._isParametric(self._obj):
            return
        anchors = [Point3D(p.x, p.y, p.z) for i in self._sel
                   for p in [self._obj.getControlPoint(i)]]
        self._convertTarget(anchors)

    def translateSelected(self, dx: float, dy: float, dz: float) -> None:
        if self._obj is None:
            return
        self._convertIfParametric()
        for i in self._sel:
            p = self._obj.getControlPoint(i)
            self._obj.setControlPoint(i, Point3D(p.x + dx, p.y + dy, p.z + dz))
        self._notify()

    def _transformSelectedHandles(self, fn) -> None:
        """对选中控制点已 materialize 的手柄偏移应用向量变换
        （gizmo 旋转/缩放时手柄跟随；未拖过的自动手柄不固化）。"""
        obj = self._obj
        if obj is None or not hasattr(obj, "getHandleOffset"):
            return
        for i in self._sel:
            for side in ("in", "out"):
                off = obj.getHandleOffset(i, side, auto=False)
                if off is None:
                    continue
                nx, ny, nz = fn(off.x, off.y, off.z)
                obj.setHandleOffset(i, side, Point3D(nx, ny, nz))

    def rotateSelected(self, rad: float, cx: float, cy: float) -> None:
        if self._obj is None:
            return
        self._convertIfParametric()
        c, s = math.cos(rad), math.sin(rad)
        for i in self._sel:
            p = self._obj.getControlPoint(i)
            nx = cx + (p.x - cx) * c - (p.y - cy) * s
            ny = cy + (p.x - cx) * s + (p.y - cy) * c
            self._obj.setControlPoint(i, Point3D(nx, ny, p.z))
        self._transformSelectedHandles(
            lambda x, y, z: (x * c - y * s, x * s + y * c, z))
        self._notify()

    def scaleSelected(self, sx: float, sy: float, cx: float, cy: float) -> None:
        if self._obj is None:
            return
        self._convertIfParametric()
        for i in self._sel:
            p = self._obj.getControlPoint(i)
            nx = cx + (p.x - cx) * sx
            ny = cy + (p.y - cy) * sy
            self._obj.setControlPoint(i, Point3D(nx, ny, p.z))
        self._transformSelectedHandles(
            lambda x, y, z: (x * sx, y * sy, z))
        self._notify()

    def _notify(self) -> None:
        self._doc.notifyObjectChanged(self._obj.id())
        self._redrawAll()

    def _redrawAll(self) -> None:
        for ad in self._adapters:
            ad.redraw()

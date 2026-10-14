# -*- coding: utf-8 -*-
"""3D 实体边倒角 / 圆角命令（build123d 内核）。

选中 BRep 实体 → 鼠标在 3D 视口点击特征边（投影拾取，Shift/Ctrl 多选，
悬停白高亮、选中黄高亮）→ 面板调圆角半径/倒角距离 → 确认/右键执行。
几何构造全部走 build123d（chamfer/fillet），原地替换实体 shape（一次撤销）。
"""
from __future__ import annotations
from typing import List, Optional

from PySide6.QtCore import Qt

from core.vector.geometry_types import ObjectType, Color3D
from core.vector.brep_object import BRepObject
from core.vector.brep_shape import BrepShape
from ..command_instance import CommandInstance
from ..param_panels import ChamferParamPanel

_PICK_PX = 14.0          # 边拾取屏幕容差（像素）


def _import_bd():
    """延迟导入 build123d（命令用到时才加载）。"""
    import os
    import sys
    src = os.path.normpath(os.path.join(
        os.path.dirname(__file__), "..", "..", "..",
        "deps", "build123d-0.11.1", "src"))
    if src not in sys.path:
        sys.path.insert(0, src)
    import build123d as bd
    return bd


def _segPointDist(px: float, py: float,
                  x0: float, y0: float, x1: float, y1: float) -> float:
    """点到线段（屏幕坐标）距离。"""
    ax, ay = x1 - x0, y1 - y0
    len2 = ax * ax + ay * ay
    t = 0.0 if len2 < 1e-18 else max(0.0, min(1.0,
        ((px - x0) * ax + (py - y0) * ay) / len2))
    dx, dy = px - (x0 + t * ax), py - (y0 + t * ay)
    return (dx * dx + dy * dy) ** 0.5


class _BrepChamferBase(CommandInstance):
    """3D 实体边倒角/圆角基类：目标实体 + 边拾取 + 预览 + 应用。"""

    _FILLET_MODE = False    # 子类覆写：True=圆角

    def __init__(self) -> None:
        super().__init__()
        self._mode = 0
        self._radius = 5.0
        self._d1 = 5.0
        self._d2 = 5.0
        self._panel = None
        self._target: Optional[BRepObject] = None
        self._picked: List[int] = []      # 已选边索引（与 edges() 同序）
        self._hover: Optional[tuple] = None   # (obj, idx) 悬停边
        self._previewObj: Optional[BRepObject] = None

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        super().start()
        self._mode = 1 if self._FILLET_MODE else 0
        self._target = None
        self._picked = []
        self._hover = None
        self._previewObj = None
        if self._doc is not None:
            sel = [o for o in self._doc.selectedObjects()
                   if o.type() == ObjectType.BREP and not o.isLocked()]
            if sel:
                self._target = sel[-1]
                self._notify("已锁定目标实体：点击其特征边倒角（Shift 多选）")
        if self._target is None:
            self._notify("请先选中一个 3D 实体（挤出生成的对象）")

    def paramPanel(self):
        if self._panel is None:
            self._panel = ChamferParamPanel()
            self._panel.modeChanged.connect(self._onPanelMode)
            self._panel.radiusChanged.connect(self._onPanelRadius)
            self._panel.distanceChanged.connect(self._onPanelDistance)
            self._panel.confirmed.connect(self._apply)
            self._panel.setRadius(self._radius)
            self._panel.setDistances(self._d1, self._d2)
        return self._panel

    def _onPanelMode(self, mode: int) -> None:
        self._mode = mode
        self._refreshPreview()

    def _onPanelRadius(self, r: float) -> None:
        self._radius = r
        self._refreshPreview()

    def _onPanelDistance(self, d1: float, d2: float) -> None:
        self._d1, self._d2 = d1, d2
        self._refreshPreview()

    def onMouseMove(self, wp, mods) -> None:
        hit = self._pickEdgeScreen(wp)
        if hit != self._hover:
            self._hover = hit
            self._updateHighlight()
        if hit is not None:
            self._view.update()

    def onMousePress(self, wp, mods) -> None:
        if self._target is None:
            return
        hit = self._pickEdgeScreen(wp)
        if hit is None:
            return
        obj, idx = hit
        # 悬停到其他实体 → 切换目标（清空已选边）
        if obj is not self._target:
            self._target = obj
            self._picked = []
        if mods & (Qt.KeyboardModifier.ShiftModifier
                   | Qt.KeyboardModifier.ControlModifier):
            if idx in self._picked:
                self._picked.remove(idx)
            else:
                self._picked.append(idx)
        else:
            self._picked = [idx]
        self._refreshPreview()

    def onRightClick(self, wp) -> bool:
        if self._target is not None and self._picked:
            self._apply()
            return True
        self.finish()
        return True

    def onEnter(self) -> bool:
        return self.onRightClick(None)

    def prompt(self) -> str:
        if self._target is None:
            return "请先在画布选中 3D 实体，再启动本命令"
        if not self._picked:
            return "点击实体特征边选择（悬停高亮），调参数后确认执行"
        return f"已选 {len(self._picked)} 条边，调节参数后确认/右键执行，Esc 结束"

    def previewSolid(self):
        return self._previewObj

    # ---------------- 边拾取（屏幕投影） ----------------

    def _pickEdgeScreen(self, wp) -> Optional[tuple]:
        """世界点（鼠标在 z=0 平面）→ 屏幕坐标 → 最近特征边。"""
        if self._view is None or self._target is None:
            return None
        cam = self._view._camera()
        w, h = self._view.width(), self._view.height()
        sp = self._view._project((wp.x, wp.y, wp.z), cam, w, h)
        if sp is None:
            return None
        mx, my = sp
        best, best_d = None, _PICK_PX
        for obj in (self._target,):
            shape = obj.shape()
            if not isinstance(shape, BrepShape):
                continue
            for i, poly in enumerate(shape.featureEdgePolylines()):
                for j in range(len(poly) - 1):
                    s = self._view._project(poly[j], cam, w, h)
                    e = self._view._project(poly[j + 1], cam, w, h)
                    if s is None or e is None:
                        continue
                    d = _segPointDist(mx, my, s[0], s[1], e[0], e[1])
                    if d < best_d:
                        best_d = d
                        best = (obj, i)
        return best

    # ---------------- 预览与高亮 ----------------

    def _updateHighlight(self) -> None:
        """悬停/选中边写入视口子元素高亮（白/黄加粗边）。"""
        if self._view is None:
            return
        entries = []
        if self._target is not None:
            for idx in self._picked:
                entries.append((self._target, "edge", idx))
        if self._hover is not None and (
                self._hover[0] is self._target
                and self._hover[1] not in self._picked):
            entries.append((self._hover[0], "edge", self._hover[1]))
        self._view._subSel = entries
        self._view.update()

    def _refreshPreview(self) -> None:
        """重建倒角/圆角预览实体（应用当前选中边 + 参数）。"""
        self._previewObj = None
        self._updateHighlight()
        if self._target is None or not self._picked:
            return
        shape = self._target.shape()
        if not isinstance(shape, BrepShape):
            return
        try:
            new_part = self._buildModified(shape)
        except Exception as e:
            self._notify(f"预览失败：{e}")
            return
        prev = BRepObject()
        prev.setShape(BrepShape.fromPart(new_part))
        prev.setColor(Color3D(0x15 / 255.0, 0xB6 / 255.0, 0xC9 / 255.0, 0.55))
        self._previewObj = prev
        if self._manager is not None:
            self._manager.invalidatePreview()

    def _buildModified(self, shape: BrepShape):
        bd = _import_bd()
        edges = [shape.edges()[i] for i in self._picked
                 if i < len(shape.edges())]
        if not edges:
            raise ValueError("选中的边已失效")
        if self._mode == 0:   # 圆角
            return bd.fillet(edges, self._radius)
        # 斜角：距离 1/距离 2
        if abs(self._d2 - self._d1) < 1e-9:
            return bd.chamfer(edges, self._d1)
        return bd.chamfer(edges, self._d1, self._d2)

    # ---------------- 执行 ----------------

    def _apply(self) -> None:
        if self._target is None or not self._picked:
            return
        shape = self._target.shape()
        if not isinstance(shape, BrepShape):
            return
        try:
            new_part = self._buildModified(shape)
        except Exception as e:
            self._notify(f"倒角失败：{e}")
            return
        doc = self._doc
        doc.beginEdit()
        try:
            self._target.setShape(BrepShape.fromPart(new_part))
            # 首次渲染预热：立即重建三角剖分/特征边缓存
            try:
                self._target.mesh()
                self._target.topology()
            except Exception:
                pass
        except Exception as e:
            self._notify(f"倒角失败：{e}")
            return
        finally:
            try:
                doc.endEdit()
            except Exception:
                pass
        if self._mode == 0:
            self._notify(f"已对 {len(self._picked)} 条边圆角（半径 {self._radius:g}）")
        else:
            self._notify(f"已对 {len(self._picked)} 条边倒角（距离 {self._d1:g}）")
        # 应用后回到拾取态：可继续选边（同一实体）倒角，Esc 结束
        self._picked = []
        self._previewObj = None
        self._updateHighlight()
        if self._manager is not None:
            self._manager.invalidatePreview()

    def _notify(self, text: str) -> None:
        if self._manager is not None:
            try:
                self._manager.notifyResult(str(text))
            except Exception:
                pass


class Chamfer3DCommand(_BrepChamferBase):
    _FILLET_MODE = False

    def name(self) -> str:
        return "Chamfer3D"

    def commandTitle(self) -> str:
        return "实体倒角"


class Fillet3DCommand(_BrepChamferBase):
    _FILLET_MODE = True

    def name(self) -> str:
        return "Fillet3D"

    def commandTitle(self) -> str:
        return "实体圆角"


def registerAll(manager) -> None:
    manager.register("Chamfer3D", Chamfer3DCommand)
    manager.register("Fillet3D", Fillet3DCommand)

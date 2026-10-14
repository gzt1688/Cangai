"""CNC 刀路画布叠加层：把 CncState 中可见的路径条目画到 2D 画布。

按"刀头运动轨迹"渲染，忠实还原每条指令：

- 水平移动（x/y 变化）：进给=实线，快速=同色虚线，圆弧按标准数学采样成折线；
- 垂直移动（x/y 不变、z 变化，如钻孔下刀/抬刀）：在当前位置画竖直线段，
  z 增大=视觉向上（抬刀），z 减小=视觉向下（下刀）；段尾带固定屏幕尺寸的方向箭头；
- 轨迹从世界中心 (0,0) 出发，体现"刀头从世界中心移动到点"的起始路径；
- 钻孔类路径（进给全为垂直下刀）额外在孔位画固定屏幕尺寸的圆标记；
- 图元不可拾取、不参与点选/框选（与网格、坐标轴一致），不影响画布交互。
"""
from __future__ import annotations

import os
import tempfile
import time
import traceback

from PySide6.QtCore import Qt, QObject, QPointF, QRectF
from PySide6.QtGui import QColor, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (QGraphicsItem, QGraphicsItemGroup,
                               QGraphicsPathItem)

from core.cnc.state import CncState
from ui.view_2d.cnc_toolpath_geom import build_toolpath_geometry

# 每条路径按添加顺序取色，循环使用
_ENTRY_COLORS = (
    "#ff5252", "#4caf50", "#2196f3", "#9c27b0", "#ff9800",
    "#00bcd4", "#e91e63", "#8bc34a", "#ffc107", "#3f51b5",
)


class _DrillMarkItem(QGraphicsItem):
    """钻孔孔位标记：外圈 + 实心中心点，固定屏幕尺寸（任意缩放可见）。"""

    def __init__(self, x: float, y: float, color: QColor) -> None:
        super().__init__()
        self._px, self._py = x, y
        self._color = color
        self._noInteract()

    def _noInteract(self) -> None:
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, False)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setEnabled(False)

    def boundingRect(self) -> QRectF:
        return QRectF(self._px - 10, self._py - 10, 20, 20)

    def paint(self, painter, option, widget=None) -> None:
        s = painter.transform().m11()
        if s <= 1e-9:
            s = 1.0
        outer = 6.0 / s
        inner = 2.0 / s
        pen = QPen(self._color, 1.2)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(self._px, self._py), outer, outer)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._color)
        painter.drawEllipse(QPointF(self._px, self._py), inner, inner)


class _ArrowItem(QGraphicsItem):
    """垂直移动段尾的方向箭头：固定屏幕尺寸。

    down=True 尖端朝下（下刀），False 尖端朝上（抬刀）。
    场景 y 增大=视觉向上（视图 scale(1,-1)），故向下=尖端 y 更小。
    """

    def __init__(self, x: float, y: float, down: bool, color: QColor) -> None:
        super().__init__()
        self._px, self._py = x, y
        self._down = down
        self._color = color
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, False)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setEnabled(False)

    def boundingRect(self) -> QRectF:
        return QRectF(self._px - 10, self._py - 10, 20, 20)

    def paint(self, painter, option, widget=None) -> None:
        s = painter.transform().m11()
        if s <= 1e-9:
            s = 1.0
        w, h = 8.0 / s, 5.0 / s
        tip_y = self._py + h if self._down else self._py - h
        pts = QPolygonF([
            QPointF(self._px, tip_y),
            QPointF(self._px - w / 2, self._py),
            QPointF(self._px + w / 2, self._py),
        ])
        pen = QPen(self._color, 1.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(self._color)
        painter.drawPolygon(pts)


class CncToolpathOverlay(QObject):
    """2D 画布上的 CNC 刀路叠加层（仅 CNC 模式启用，2D/浮雕/3D 模式不显示）。"""

    def __init__(self, scene, parent=None) -> None:
        super().__init__(parent)
        self._scene = scene
        self._state = CncState.instance()
        self._group = None
        self._enabled = False          # 默认禁用：2D 模式画布不显示 CNC 轨迹
        self._state.stateChanged.connect(self._rebuild)
        self._rebuild()

    def setEnabled(self, on: bool) -> None:
        """按画布模式开关叠加层：仅 CNC 模式显示轨迹。"""
        if self._enabled == on:
            return
        self._enabled = on
        self._rebuild()

    # ---------------------------------------------------------------- 重建
    def _rebuild(self) -> None:
        self._log("rebuild called, enabled=%s entries=%d" % (
            self._enabled, len(self._state.pathEntries)))
        try:
            if self._group is not None and self._group.scene() is not None:
                self._scene.removeItem(self._group)
            self._group = QGraphicsItemGroup()
            self._group.setZValue(100)
            self._group.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
            self._group.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self._scene.addItem(self._group)
            if not self._enabled:
                # 非 CNC 模式：不绘制任何 CNC 轨迹（数据独立）
                self._scene.update()
                return
            for idx, entry in enumerate(self._state.pathEntries):
                if not entry.visible:
                    continue
                color = QColor(entry.color or _ENTRY_COLORS[idx % len(_ENTRY_COLORS)])
                self._addEntry(entry, color)
            # 兜底刷新视图（图元加入后确保可见）
            self._scene.update()
            for v in self._scene.views():
                v.viewport().update()
            self._log("rebuild ok, items=%d, group_in_scene=%s" % (
                len(self._group.childItems()),
                self._group.scene() is self._scene))
        except Exception:
            self._log("rebuild ERROR:\n" + traceback.format_exc())

    @staticmethod
    def _log(msg: str) -> None:
        """临时诊断：写 %TEMP%/cangai_cnc_overlay.log（排查画布不显示路径用）。"""
        try:
            with open(os.path.join(tempfile.gettempdir(), "cangai_cnc_overlay.log"),
                      "a", encoding="utf-8") as f:
                f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
        except Exception:
            pass

    # ---------------------------------------------------------------- 轨迹
    def _addEntry(self, entry, color: QColor) -> None:
        g = build_toolpath_geometry(entry)

        def _polyline(pts):
            """折线 [(x,y,z)] → QPainterPath（2D 中 y_screen = y + z）。"""
            if not pts:
                return None
            p = QPainterPath()
            p.moveTo(pts[0][0], pts[0][1] + pts[0][2])
            for px, py, pz in pts[1:]:
                p.lineTo(px, py + pz)
            return p

        def _segments(segs):
            """独立线段 → 单条 QPainterPath（段间跳转用 moveTo）。"""
            p = QPainterPath()
            for (ax, ay, az), (bx, by, bz) in segs:
                p.moveTo(ax, ay + az)
                p.lineTo(bx, by + bz)
            return p

        feed_h = _polyline(g["feed_h"])
        feed_v = _segments(g["feed_v"])
        rapid_h = _polyline(g["rapid_h"])
        rapid_v = _segments(g["rapid_v"])
        if feed_h is not None and not feed_h.isEmpty():
            self._addPath(feed_h, color, dash=False)
        if feed_v is not None and not feed_v.isEmpty():
            self._addPath(feed_v, color, dash=False)
        if rapid_h is not None and not rapid_h.isEmpty():
            rc = QColor(color)
            rc.setAlpha(110)
            self._addPath(rapid_h, rc, dash=True)
        if rapid_v is not None and not rapid_v.isEmpty():
            rc = QColor(color)
            rc.setAlpha(140)
            self._addPath(rapid_v, rc, dash=True)
        for ax, ay, az, _dx, _dy, dz in g["arrows"]:
            down = dz < 0
            # 只画进刀(↓)与退刀(↑)两个箭头：下刀红、抬刀绿
            arrow_col = QColor("#e53935") if down else QColor("#43a047")
            self._group.addToGroup(_ArrowItem(ax, ay + az, down, arrow_col))
        # 纯钻孔类路径（无水平进给）：孔位画固定尺寸圆标记
        if not g["has_horizontal"] and g["marks"]:
            for mx, my in g["marks"]:
                self._group.addToGroup(_DrillMarkItem(mx, my, color))

    # ---------------------------------------------------------------- 图元
    def _addPath(self, qpath: QPainterPath, color: QColor, dash: bool) -> None:
        if qpath.isEmpty():
            return
        item = QGraphicsPathItem(qpath)
        pen = QPen(color, 1.4 if not dash else 1.0)
        pen.setCosmetic(True)
        if dash:
            pen.setStyle(Qt.PenStyle.DashLine)
        item.setPen(pen)
        item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, False)
        item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        item.setEnabled(False)
        self._group.addToGroup(item)

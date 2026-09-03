"""2D 画布场景：参考网格与坐标轴绘制。"""
from __future__ import annotations
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QPen
from PySide6.QtWidgets import QGraphicsScene, QGraphicsLineItem, QGraphicsItem

from core.grid_manager import GridManager
from ui.styles import theme

_AXIS_COLORS = {"X": "#e53935", "Y": "#43a047", "Z": "#1e88e5"}


def _makeReferenceItem(item: QGraphicsLineItem) -> QGraphicsLineItem:
    """参考线（网格/坐标轴）不参与点选与框选。"""
    item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
    item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsFocusable, False)
    item.setEnabled(False)
    return item



class GraphicsScene(QGraphicsScene):
    def __init__(self) -> None:
        super().__init__()
        self._grid = GridManager.instance()
        self._gridVisible = True
        self._axisVisible = True
        self.setSceneRect(-2000, -2000, 4000, 4000)
        self._gridItems: list = []
        self._buildReferenceGrid()
        self._axisItems: list = []
        self.setViewMode(1)

    def _buildReferenceGrid(self) -> None:
        spacing = 10.0
        count = 10
        half = spacing * count / 2.0
        self._gridHalf = half
        pen = QPen(QColor(200, 200, 200), 0)
        pen.setCosmetic(True)
        for i in range(count + 1):
            x = -half + i * spacing
            item = _makeReferenceItem(QGraphicsLineItem(x, -half, x, half))
            item.setPen(pen)
            item.setZValue(-1000)
            self.addItem(item)
            self._gridItems.append(item)
        for i in range(count + 1):
            y = -half + i * spacing
            item = _makeReferenceItem(QGraphicsLineItem(-half, y, half, y))
            item.setPen(pen)
            item.setZValue(-1000)
            self.addItem(item)
            self._gridItems.append(item)
        for it in self._gridItems:
            it.setVisible(self._gridVisible)

    def setViewMode(self, mode: int) -> None:
        for it in self._axisItems:
            self.removeItem(it)
        self._axisItems.clear()
        if mode == 0:
            axes = [("X", 1, 0), ("Y", 0, 1), ("Z", -0.6, -0.6)]
        elif mode == 1:
            axes = [("X", 1, 0), ("Y", 0, 1)]
        elif mode == 2:
            axes = [("X", 1, 0), ("Z", 0, 1)]
        else:
            axes = [("Y", 1, 0), ("Z", 0, 1)]
        for name, dx, dy in axes:
            color = _AXIS_COLORS[name]
            pen = QPen(QColor(color), 2)
            pen.setCosmetic(True)
            ex, ey = dx * self._gridHalf, dy * self._gridHalf
            line = _makeReferenceItem(QGraphicsLineItem(0, 0, ex, ey))
            line.setPen(pen)
            line.setZValue(-900)
            self.addItem(line)
            line.setVisible(self._axisVisible)
            self._axisItems.append(line)

    # ---------------- 显示开关（设置对话框联动） ----------------
    def setGridVisible(self, visible: bool) -> None:
        self._gridVisible = visible
        for it in self._gridItems:
            it.setVisible(visible)

    def setAxisVisible(self, visible: bool) -> None:
        self._axisVisible = visible
        for it in self._axisItems:
            it.setVisible(visible)

    def drawBackground(self, painter, rect: QRectF) -> None:
        painter.fillRect(rect, QColor(theme.BG_VIEWPORT))

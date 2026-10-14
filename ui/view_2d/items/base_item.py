"""2D 图元基类：绑定几何对象并处理样式。"""
from __future__ import annotations
from PySide6.QtCore import Qt
from PySide6.QtGui import QPen, QColor, QPainterPathStroker
from PySide6.QtWidgets import QGraphicsPathItem, QStyle, QStyleOptionGraphicsItem

from core.vector.geometry_types import ObjectType


class BaseGraphicsItem(QGraphicsPathItem):
    def __init__(self, obj) -> None:
        super().__init__()
        self._obj = obj
        self.setFlag(QGraphicsPathItem.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)
        self._syncColor()

    def object(self):
        return self._obj

    def objectId(self) -> str:
        return self._obj.id()

    def _syncColor(self) -> None:
        c = self._obj.color()
        pen = QPen(QColor(*c), self._obj.lineWidth())
        pen.setCosmetic(True)
        if self._obj.type() == ObjectType.POINT:
            # 点：白色实心圆 + 对象色描边（描边稍加粗保证可见性）
            pen.setWidthF(max(self._obj.lineWidth(), 1.0) + 0.5)
            self.setPen(pen)
            self.setBrush(QColor(Qt.GlobalColor.white))
        else:
            self.setPen(pen)
            self.setBrush(Qt.BrushStyle.NoBrush)

    def shape(self):
        """命中区域：仅沿路径的描边带可命中（点选/悬停均走 shape）。
        QGraphicsPathItem 默认 shape 是路径填充区域，圆/椭圆/闭合折线内部会被误选中；
        点对象保持实心标记盘命中。"""
        if self._obj.type() == ObjectType.POINT:
            return self.path()
        views = self.scene().views() if self.scene() is not None else []
        scale = views[0].zoomScale() if views else 1.0
        stroker = QPainterPathStroker()
        # cosmetic 笔按屏幕像素计宽：换算为世界宽度，保底屏幕 6px 点击容差
        stroker.setWidth(max(self.pen().widthF(), 6.0) / max(scale, 1e-9))
        return stroker.createStroke(self.path())

    def syncFromObject(self) -> None:
        self._syncColor()
        if self._obj.isSelected():
            self.setSelected(True)

    def boundingRect(self):
        """自绘包围盒虚线/加粗高亮可能略超出 path 范围，外扩安全余量防残影。"""
        return super().boundingRect().adjusted(-1.0, -1.0, 1.0, 1.0)

    def paint(self, painter, option, widget=None) -> None:
        # 抑制 Qt 默认选中虚线框（基类按 option.state 绘制，黑色且比包围盒大），
        # 选中视觉（加粗描边 + 精确包围盒虚线框）全部由图元自绘。
        opt = QStyleOptionGraphicsItem(option)
        opt.state &= ~QStyle.StateFlag.State_Selected
        if self._obj.isSelected():
            from ui.styles import theme
            savedPen = self.pen()
            hl = theme.selectionColorFor(self._obj)
            pen = QPen(QColor(hl), max(savedPen.widthF() + 1.5, 3.0))
            pen.setCosmetic(True)
            self.setPen(pen)
            super().paint(painter, opt, widget)
            self.setPen(savedPen)
            if not self._commandActive():
                self._paintSelectionBox(painter, hl)
        else:
            super().paint(painter, opt, widget)

    def _commandActive(self) -> bool:
        if self.scene() is None:
            return False
        for v in self.scene().views():
            cm = getattr(v, "_commandManager", None)
            if cm is not None and getattr(cm, "hasActive", lambda: False)():
                return True
        return False

    def _paintSelectionBox(self, painter, color: str) -> None:
        """选中虚线框：精确贴合对象世界包围盒（不含像素余量）。"""
        from core import app_settings
        if not app_settings.show_selection_box:
            return
        if self._obj.type() == ObjectType.POINT:
            return
        try:
            bb = self._obj.getBoundingBox()
        except Exception:
            return
        if bb is None or not getattr(bb, "isValid", lambda: True)():
            return
        from PySide6.QtCore import QPointF, QRectF
        rect = QRectF(QPointF(bb.min.x, bb.min.y), QPointF(bb.max.x, bb.max.y))
        if rect.isEmpty():
            return
        pen = QPen(QColor(color), 1.0, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect)

    def itemChange(self, change, value):
        if change == QGraphicsPathItem.ItemSelectedHasChanged:
            self._obj.setSelected(bool(value))
        return super().itemChange(change, value)
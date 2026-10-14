"""尺寸标注 2D 图元（DIMENSION）：自绘尺寸线/延伸线/箭头与矢量读数文字。

- 几何全部来自 DimensionObject.layout()（纯世界坐标），字号按对象世界字高
  随视图缩放（与模型空间一致，CAD 常规做法）；
- 命中区域 = 尺寸线/延伸线的加宽描边 + 读数文字包围盒（文字可点选，
  XY 标注读数字样即“单独拖动子标注”的手柄）；
- 选中视觉：加粗描边 + 世界包围盒虚线框（与曲线选中风格一致）。
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, QPointF, Qt
from PySide6.QtGui import (QPen, QColor, QPainterPath, QPolygonF,
                           QPainterPathStroker, QFont, QFontMetricsF,
                           QTransform)
from PySide6.QtWidgets import QGraphicsPathItem, QStyle, QStyleOptionGraphicsItem

from ui.styles import theme
from core import app_settings


class DimensionGraphicsItem(QGraphicsPathItem):
    def __init__(self, obj) -> None:
        super().__init__()
        self._obj = obj
        self.setFlag(QGraphicsPathItem.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)

    # ---------------- 对象访问 ----------------

    def object(self):
        return self._obj

    def objectId(self) -> str:
        return self._obj.id()

    # ---------------- 几何 ----------------

    def _pad(self) -> float:
        return self._obj.fontHeight() * 0.6 + 1.0

    def boundingRect(self) -> QRectF:
        bb = self._obj.getBoundingBox()
        if not bb.isValid():
            return QRectF()
        p = self._pad()
        return QRectF(QPointF(bb.min.x, bb.min.y),
                      QPointF(bb.max.x, bb.max.y)).adjusted(-p, -p, p, p)

    def _linesPath(self) -> QPainterPath:
        """尺寸线/延伸线合并路径（命中与描边用）。"""
        path = QPainterPath()
        for x1, y1, x2, y2 in self._obj.layout()["lines"]:
            path.moveTo(x1, y1)
            path.lineTo(x2, y2)
        return path

    def shape(self) -> QPainterPath:
        obj = self._obj
        path = self._linesPath()
        views = self.scene().views() if self.scene() is not None else []
        scale = views[0].zoomScale() if views else 1.0
        stroker = QPainterPathStroker()
        stroker.setWidth(max(obj.lineWidth(), 6.0) / max(scale, 1e-9))
        if not path.isEmpty():
            path = stroker.createStroke(path)
        # 读数文字包围盒参与命中（文字可点选/拖动）
        for x0, y0, x1, y1 in obj.textBounds():
            path.addRect(QRectF(QPointF(x0, y0), QPointF(x1, y1)))
        return path

    # ---------------- 绘制 ----------------

    def paint(self, painter, option, widget=None) -> None:
        lay = self._obj.layout()
        if not lay["lines"] and not lay["texts"]:
            return
        obj = self._obj
        hl = theme.selectionColorFor(obj) if obj.isSelected() else None
        col = QColor(hl) if hl else QColor(*obj.color())

        painter.save()
        pen = QPen(col, obj.lineWidth() + (1.5 if obj.isSelected() else 0.0))
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        # 尺寸线 / 延伸线
        for x1, y1, x2, y2 in lay["lines"]:
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
        # 箭头三角（实心）
        if lay["fills"]:
            painter.setBrush(col)
            painter.setPen(Qt.PenStyle.NoPen)
            for tri in lay["fills"]:
                painter.drawPolygon(QPolygonF(
                    [QPointF(px, py) for px, py in tri]))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(pen)

        # 读数文字：模型空间字高 = obj.fontHeight()（世界单位，随视图缩放，
        # CAD 常规）；字形先按放大整数字号生成轮廓路径，再缩回世界字高，
        # 保证小字高下依然清晰，且不受视口缩放影响字号精度。
        _K = 64.0
        fs = obj.fontHeight()
        if fs > 1e-9:
            fontPx = max(1, int(round(fs * _K)))
            font = QFont(app_settings.dim_font or "Microsoft YaHei")
            font.setPixelSize(fontPx)
            painter.setFont(font)
            fm = QFontMetricsF(font)
            shrink = QTransform.fromScale(1.0 / _K, 1.0 / _K)
            for t in lay["texts"]:
                painter.save()
                painter.translate(t["cx"], t["cy"])
                painter.scale(1.0, -1.0)          # 屏幕式 Y 向下后再绘文本
                painter.rotate(-t["angle"])       # 世界角 → 镜像系反向旋转
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(col)
                path = QPainterPath()
                w = fm.horizontalAdvance(t["text"])
                h = fm.height()
                asc = fm.ascent()
                # baseline 使字形包围盒中心落在 (0,0)
                path.addText(-w * 0.5, asc - h * 0.5, font, t["text"])
                painter.drawPath(shrink.map(path))
                painter.restore()

        # XY 标注选中：读数字样画虚线圆环，提示可单独拖动子标注
        if obj.isSelected() and obj.kind() == "xy":
            painter.save()
            hpen = QPen(QColor(hl), 1.0, Qt.PenStyle.DashLine)
            hpen.setCosmetic(True)
            painter.setPen(hpen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            rr = fs * 0.55
            for t in lay["texts"]:
                painter.drawEllipse(QPointF(t["cx"], t["cy"]), rr, rr)
            painter.restore()

        painter.restore()
        if obj.isSelected():
            self._paintSelectionBox(painter, hl)

    def _paintSelectionBox(self, painter, color: str) -> None:
        from core import app_settings
        if not app_settings.show_selection_box:
            return
        try:
            bb = self._obj.getBoundingBox()
        except Exception:
            return
        if bb is None or not bb.isValid():
            return
        rect = QRectF(QPointF(bb.min.x, bb.min.y), QPointF(bb.max.x, bb.max.y))
        if rect.isEmpty():
            return
        painter.save()
        pen = QPen(QColor(color), 1.0, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect)
        painter.restore()

    # ---------------- 状态同步 ----------------

    def syncFromObject(self) -> None:
        if self._obj.isSelected():
            self.setSelected(True)

    def itemChange(self, change, value):
        if change == QGraphicsPathItem.ItemSelectedHasChanged:
            self._obj.setSelected(bool(value))
        return super().itemChange(change, value)

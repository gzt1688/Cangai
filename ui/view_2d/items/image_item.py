"""图片 2D 图元：按对象中心/宽高/旋转绘制参考图（描图底图）。

- 图片层 z=-600：垫在浮雕模型(-500)与全部曲线(0)之下；
- 命中区 = 旋转后的四角多边形（内部可点选，框选走对象 points()）；
- 选中视觉 = 贴合四角的虚线框（与曲线选中样式一致），图片本体不加修饰。
"""
from __future__ import annotations
import math

from PySide6.QtCore import QRectF, QPointF, Qt
from PySide6.QtGui import QPen, QColor, QPainterPath, QPolygonF
from PySide6.QtWidgets import QGraphicsPathItem

from .base_item import BaseGraphicsItem


class ImageGraphicsItem(BaseGraphicsItem):
    """IMAGE 对象的 2D 图元：位图 + 选中虚线框。"""

    def __init__(self, obj) -> None:
        super().__init__(obj)

    def _cornerPath(self) -> QPainterPath:
        path = QPainterPath()
        pts = self._obj.points()
        if not pts:
            return path
        path.moveTo(pts[0].x, pts[0].y)
        for p in pts[1:]:
            path.lineTo(p.x, p.y)
        path.closeSubpath()
        return path

    def boundingRect(self) -> QRectF:
        bb = self._obj.getBoundingBox()
        if not bb.isValid():
            return QRectF()
        return QRectF(QPointF(bb.min.x, bb.min.y),
                      QPointF(bb.max.x, bb.max.y)).adjusted(-2.0, -2.0, 2.0, 2.0)

    def shape(self) -> QPainterPath:
        return self._cornerPath()

    def paint(self, painter, option, widget=None) -> None:
        o = self._obj
        pm = o.pixmap()
        painter.save()
        if not pm.isNull():
            # 绘制顺序：平移到中心 → 翻Y轴（屏幕Y-down → 图片正立Y-up）
            # → rotate(-角度)：Qt rotate(正度)=CW，但因Y已翻转，正旋转会反向。
            #   对象层 rotate() 是数学 CCW（_rotation += angle，points() 用 lx*cos-ly*sin），
            #   因此在 painter 里取 rotate(−degrees(rotation)) 才能让图片
            #   实际旋转方向与 points() 的四角虚线框、操作轴 gizmo 完全一致。
            painter.translate(o.center().x, o.center().y)
            painter.scale(1.0, -1.0)
            painter.rotate(-math.degrees(o.rotation()))
            painter.drawPixmap(
                QRectF(-o.width() * 0.5, -o.height() * 0.5,
                       o.width(), o.height()),
                pm, QRectF(0.0, 0.0, pm.width(), pm.height()))
        painter.restore()
        if o.isSelected():
            from ui.styles import theme
            from core import app_settings
            hl = theme.selectionColorFor(o)
            pts = o.points()
            poly = QPolygonF([QPointF(p.x, p.y) for p in pts])
            painter.save()
            if app_settings.show_selection_box:
                pen = QPen(QColor(hl), 1.0, Qt.PenStyle.DashLine)
            else:
                # 关闭虚线框时用加粗实线四角框作选中高亮（与曲线加粗描边一致）
                pen = QPen(QColor(hl), 2.0)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPolygon(poly)
            painter.restore()

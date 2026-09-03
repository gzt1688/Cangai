"""捕捉类型图标生成：用 QPainter 绘制双色 QIcon（unchecked 深色 / checked 白色），
适配捕捉按钮 checked/unchecked 两种状态。"""
from __future__ import annotations
from PySide6.QtCore import Qt, QPoint, QPointF
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QPen, QPolygonF
from core.snap.snap_types import SnapType


def _drawMarker(p: QPainter, t: SnapType, c: QPoint, r: int) -> None:
    """在屏幕坐标 c 处绘制捕捉类型标记（无填充，纯线条）。"""
    x, y = c.x(), c.y()
    if t == SnapType.ENDPOINT:
        p.drawRect(x - r, y - r, 2 * r, 2 * r)
    elif t == SnapType.MIDPOINT:
        p.drawPolygon(QPolygonF([QPointF(x, y - r), QPointF(x + r, y), QPointF(x, y + r)]))
    elif t == SnapType.CENTER:
        p.drawEllipse(c, r - 1, r - 1)
        p.drawLine(x - r, y, x + r, y)
        p.drawLine(x, y - r, x, y + r)
    elif t == SnapType.POINT:
        p.drawLine(x - r, y - r, x + r, y + r)
        p.drawLine(x - r, y + r, x + r, y - r)
    elif t == SnapType.QUARTER:
        p.drawPolygon(QPolygonF([QPointF(x, y - r), QPointF(x + r, y),
                                 QPointF(x, y + r), QPointF(x - r, y)]))
    elif t == SnapType.INTERSECTION:
        p.drawLine(x - r, y - r, x + r, y + r)
        p.drawLine(x - r, y + r, x + r, y - r)
    elif t == SnapType.PERPENDICULAR:
        p.drawLine(x, y, x + r, y)
        p.drawLine(x + r, y, x + r, y - r)
        p.drawLine(x + r, y - r, x, y - r)
    elif t == SnapType.TANGENT:
        p.drawEllipse(c, r - 2, r - 2)
        p.drawLine(x + r, y - r, x - r, y + r)
    elif t == SnapType.NEAREST:
        p.drawLine(x - r, y, x + r, y)
    elif t == SnapType.VERTEX:
        p.drawRect(x - r + 1, y - r + 1, 2 * r - 2, 2 * r - 2)


def makeSnapIcon(t: SnapType, size: int = 32) -> QIcon:
    """生成捕捉类型图标：unchecked 深色 + checked 白色。"""
    icon = QIcon()
    for state, color in ((QIcon.Off, "#333333"), (QIcon.On, "#ffffff")):
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(QColor(color), 1.6)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        _drawMarker(p, t, QPoint(size // 2, size // 2), 9)
        p.end()
        icon.addPixmap(pm, QIcon.Normal, state)
    return icon

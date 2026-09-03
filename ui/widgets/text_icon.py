"""用 QPainter 绘制中文文字图标，避免 Qt SVG 渲染器对 text 字体支持弱的问题。"""
from __future__ import annotations
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QFont, QPen


def makeTextIcon(text: str = "字", size: int = 64,
                 family: str = "YouYuan") -> QIcon:
    """生成白色文字透明背景图标。size 为像素分辨率（越大越清晰，按比例缩放显示）。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)
    font = QFont(family)
    font.setPixelSize(int(size * 0.98))
    font.setWeight(QFont.Black)
    font.setBold(True)
    p.setFont(font)
    p.setPen(QPen(QColor("#ffffff")))
    fm = p.fontMetrics()
    x = (size - fm.horizontalAdvance(text)) / 2
    y = (size + fm.ascent() - fm.descent()) / 2
    p.drawText(int(x), int(y), text)
    p.end()
    return QIcon(pm)
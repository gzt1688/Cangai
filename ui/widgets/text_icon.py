"""用 QPainter 绘制中文文字图标，避免 Qt SVG 渲染器对 text 字体支持弱的问题。"""
from __future__ import annotations
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QFont, QPen


def makeTextIcon(text: str = "字", size: int = 64,
                 family: str = "YouYuan") -> QIcon:
    """生成白色文字透明背景图标。size 为像素分辨率（越大越清晰，按比例缩放显示）。

    支持多字：超过 2 字自动拆成两行（每行 2 字，如"纹理填充"→
    "纹理"/"填充"），也可用 "\n" 显式分行；字号按行数自动缩小。
    """
    if "\n" in text:
        lines = text.split("\n")
    elif len(text) > 2:
        lines = [text[:2], text[2:]]
    else:
        lines = [text]
    n = max(len(s) for s in lines)

    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)
    font = QFont(family)
    if len(lines) == 1:
        font.setPixelSize(int(size * 0.98))
    else:
        # 两行：每行最多 n 字，留边距 + 行距
        font.setPixelSize(int(size * 0.86 / max(n, len(lines))))
    font.setWeight(QFont.Black)
    font.setBold(True)
    p.setFont(font)
    p.setPen(QPen(QColor("#ffffff")))
    fm = p.fontMetrics()
    line_h = fm.height()
    total_h = line_h * len(lines)
    y0 = (size - total_h) / 2
    for i, s in enumerate(lines):
        x = (size - fm.horizontalAdvance(s)) / 2
        y = y0 + i * line_h + fm.ascent()
        p.drawText(int(x), int(y), s)
    p.end()
    return QIcon(pm)
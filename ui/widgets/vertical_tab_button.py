"""竖排标签按钮（左侧面板折叠态）。"""
from __future__ import annotations
from PySide6.QtCore import QSize
from PySide6.QtWidgets import QPushButton, QSizePolicy

from ui.styles.theme import PRIMARY, PRIMARY_DARK, TEXT, TEXT_ON_PRIMARY


class VerticalTabButton(QPushButton):
    """竖排文字的切换标签按钮（文字逐字竖排，用于左侧面板）。"""

    TAB_WIDTH = 34
    PADDING = 10

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(parent)
        self._text = text
        self.setCheckable(True)
        self.setFixedWidth(self.TAB_WIDTH)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setText("\n".join(text))
        self.setStyleSheet(self._buildStyleSheet(PRIMARY))

    def labelText(self) -> str:
        return self._text

    def setVerticalText(self, text: str) -> None:
        """更新竖排文本：同步 _text（供 sizeHint 用）并逐字竖排显示。"""
        self._text = text
        self.setText("\n".join(text))

    def sizeHint(self) -> QSize:
        fm = self.fontMetrics()
        chars = max(len(self._text), 1)
        height = fm.lineSpacing() * chars + self.PADDING * 2
        return QSize(self.TAB_WIDTH, height)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def updateHighlightColor(self, color: str) -> None:
        self.setStyleSheet(self._buildStyleSheet(color))

    def _buildStyleSheet(self, color: str) -> str:
        return f"""
        QPushButton {{
            border: none;
            background: transparent;
            color: {TEXT};
            padding: 4px 0px;
        }}
        QPushButton:checked {{
            background: {color};
            color: {TEXT_ON_PRIMARY};
            font-weight: bold;
        }}
        QPushButton:hover {{ background: #e2e2e2; }}
        QPushButton:checked:hover {{ background: {PRIMARY_DARK}; }}
        """

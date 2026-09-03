"""流式布局管理器。"""
from __future__ import annotations
from PySide6.QtCore import Qt, QPoint, QRect, QSize
from PySide6.QtWidgets import QLayout, QLayoutItem


class FlowLayout(QLayout):
    """横向排布、宽度不足自动换行的布局（用于工具按钮栏分类页）。"""

    def __init__(self, parent=None, margin: int = 2, spacing: int = 3) -> None:
        super().__init__(parent)
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)
        self._items: list[QLayoutItem] = []

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientations:
        return Qt.Orientations()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._doLayout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._doLayout(rect, False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        size += QSize(m.left() + m.right(), m.top() + m.bottom())
        return size

    def _doLayout(self, rect: QRect, testOnly: bool) -> int:
        m = self.contentsMargins()
        effective = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x = effective.x()
        y = effective.y()
        lineHeight = 0
        for item in self._items:
            hint = item.sizeHint()
            spaceX = self.spacing()
            spaceY = self.spacing()
            nextX = x + hint.width() + spaceX
            if nextX - spaceX > effective.right() and lineHeight > 0:
                x = effective.x()
                y += lineHeight + spaceY
                nextX = x + hint.width() + spaceX
                lineHeight = 0
            if not testOnly:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = nextX
            lineHeight = max(lineHeight, hint.height())
        return y + lineHeight - rect.y() + m.bottom()

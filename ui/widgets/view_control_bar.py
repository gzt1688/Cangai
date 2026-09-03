"""视图控制条。"""
from __future__ import annotations
from PySide6.QtWidgets import QLabel, QHBoxLayout, QWidget, QFrame
from PySide6.QtCore import Qt, Signal


class ViewControlBar(QWidget):
    environmentChanged = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(28)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(6)
        self._btnStandard = QLabel("标准")
        self._btnRelief = QLabel("浮雕")
        self._btnDual = QLabel("双视窗")
        for lbl in (self._btnStandard, self._btnRelief, self._btnDual):
            lbl.setFixedHeight(22)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setMinimumWidth(56)
        self._setCurrent("标准")
        layout.addWidget(self._btnStandard)
        layout.addWidget(self._makeSep())
        layout.addWidget(self._btnRelief)
        layout.addSpacing(8)
        layout.addWidget(self._btnDual)
        layout.addStretch()

        for lbl, key in [(self._btnStandard, "标准"), (self._btnRelief, "浮雕"),
                         (self._btnDual, "双视窗")]:
            lbl.mousePressEvent = lambda e, k=key: self._setCurrent(k)

    def _makeSep(self) -> QFrame:
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setFixedWidth(1)
        return sep

    def _setCurrent(self, key: str) -> None:
        for lbl, k in [(self._btnStandard, "标准"), (self._btnRelief, "浮雕"),
                       (self._btnDual, "双视窗")]:
            if k == key:
                lbl.setStyleSheet("background:#15b6c9; color:#fff; border-radius:3px; padding:1px 6px;")
            else:
                lbl.setStyleSheet("color:#333; padding:1px 6px;")
        self.environmentChanged.emit(key)

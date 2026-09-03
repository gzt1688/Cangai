"""过滤工具栏。"""
from __future__ import annotations
from PySide6.QtWidgets import QToolBar, QPushButton
from PySide6.QtCore import Signal


class FilterToolbar(QToolBar):
    filterToggled = Signal(str, bool)

    def __init__(self) -> None:
        super().__init__("过滤")
        self.setMovable(False)
        for key, label in [("point", "点"), ("curve", "曲线"), ("surface", "曲面"),
                           ("mesh", "网格"), ("dimension", "标注"), ("other", "其它"),
                           ("disable", "禁用"), ("curveVisible", "曲线可见")]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedHeight(24)
            btn.setMinimumWidth(58)
            if key in ("point", "curve"):
                btn.setChecked(True)
            btn.toggled.connect(lambda checked, k=key: self.filterToggled.emit(k, checked))
            self.addWidget(btn)
            self.addSeparator()

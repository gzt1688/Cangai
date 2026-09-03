"""画布模式切换条：2D / 3D / 浮雕。"""
from __future__ import annotations
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget, QHBoxLayout, QPushButton, QButtonGroup

import ui.styles.theme as theme


# 画布工作模式：2D 绘图 / 浮雕 / 3D 预览（三选一，互斥）
CANVAS_MODES = [("2d", "2D"), ("relief", "浮雕"), ("3d", "3D")]

_BTN_W = 62
_BTN_H = 26


class CanvasModeBar(QWidget):
    """画布上方居中的模式切换条：2D / 浮雕 / 3D，三个按钮互斥。"""

    modeChanged = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("canvasModeBar")
        self.setAttribute(Qt.WA_StyledBackground, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QPushButton] = {}
        for key, text in CANVAS_MODES:
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.setFixedSize(_BTN_W, _BTN_H)
            btn.setCursor(Qt.PointingHandCursor)
            self._group.addButton(btn)
            layout.addWidget(btn)
            self._buttons[key] = btn

        self._current = "2d"
        self._group.buttonClicked.connect(self._onButtonClicked)
        self.refreshStyle()

        self.setFixedSize(_BTN_W * len(CANVAS_MODES) + 2, _BTN_H + 2)

    def _onButtonClicked(self, btn: QPushButton) -> None:
        for key, b in self._buttons.items():
            if b is btn:
                if key != self._current:
                    self._current = key
                    self.refreshStyle()
                    self.modeChanged.emit(key)
                else:
                    btn.setChecked(True)
                break

    def setCurrent(self, key: str, notify: bool = False) -> None:
        if key not in self._buttons:
            return
        if key == self._current:
            self.refreshStyle()
            return
        self._current = key
        self.refreshStyle()
        if notify:
            self.modeChanged.emit(key)

    def current(self) -> str:
        return self._current

    def refreshStyle(self) -> None:
        last = CANVAS_MODES[-1][0]
        for key, btn in self._buttons.items():
            radius = ""
            if key == CANVAS_MODES[0][0]:
                radius = "border-top-left-radius:3px; border-bottom-left-radius:3px;"
            elif key == last:
                radius = "border-top-right-radius:3px; border-bottom-right-radius:3px;"
            divider = "" if key == last else "border-right:1px solid #a0a0a0;"
            checked = "background:%s; color:%s;" % (theme.PRIMARY, theme.TEXT_ON_PRIMARY)
            normal = "background:#e0e0e0; color:#333333;"
            btn.setStyleSheet(
                "QPushButton { %s %s %s border:none; font-weight:bold; padding:0px; }"
                "QPushButton:hover { background:#d0d0d0; color:#333333; }"
                "QPushButton:checked { %s }"
                "QPushButton:checked:hover { background:%s; color:%s; }"
                % (radius, divider,
                   checked if key == self._current else normal,
                   checked if key == self._current else normal,
                   theme.PRIMARY_DARK if key == self._current else "#d0d0d0",
                   theme.TEXT_ON_PRIMARY if key == self._current else "#333333")
            )
            btn.setChecked(key == self._current)
        self.setStyleSheet(
            "QWidget#canvasModeBar { background:#e0e0e0; border:1px solid #a0a0a0;"
            " border-radius:4px; }"
        )

"""命令输入条控件。"""
from __future__ import annotations
from PySide6.QtWidgets import (QFrame, QVBoxLayout, QHBoxLayout, QTextEdit,
                                QLineEdit, QLabel)
from PySide6.QtCore import Signal


class CommandBar(QFrame):
    commandEntered = Signal(str)
    promptChanged = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setFrameShape(QFrame.NoFrame)
        self.setMinimumHeight(74)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(2)

        self._history = QTextEdit()
        self._history.setReadOnly(True)
        self._history.setMinimumHeight(44)
        layout.addWidget(self._history)

        bottom = QHBoxLayout()
        bottom.setSpacing(4)
        self._prompt = QLabel("就绪")
        self._prompt.setFixedWidth(72)
        self._prompt.setMinimumHeight(24)
        self._input = QLineEdit()
        self._input.setPlaceholderText("输入命令或坐标，回车确认")
        self._input.setMinimumHeight(24)
        bottom.addWidget(self._prompt)
        bottom.addWidget(self._input)
        layout.addLayout(bottom)

        self._input.returnPressed.connect(self._onReturn)

    def _onReturn(self) -> None:
        text = self._input.text().strip()
        if not text:
            return
        self._history.append(f"<b>&gt; {text}</b>")
        self._input.clear()
        self.commandEntered.emit(text)

    def setPrompt(self, text: str) -> None:
        self._prompt.setText(text)

    def appendHistory(self, text: str) -> None:
        self._history.append(text)

    def keyPressEvent(self, event):
        if event.key() == 16777216:
            self.commandEntered.emit("ESC")
            return
        super().keyPressEvent(event)

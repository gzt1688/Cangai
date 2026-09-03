"""属性面板：展示选中对象的通用属性。"""
from __future__ import annotations
from PySide6.QtWidgets import QWidget, QVBoxLayout, QTextEdit, QLabel


class PropertyPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        self._info = QLabel("未选中对象")
        self._text = QTextEdit()
        self._text.setReadOnly(True)
        self._text.setMinimumHeight(80)
        layout.addWidget(self._info)
        layout.addWidget(self._text)

    def showObject(self, obj) -> None:
        if obj is None:
            self._info.setText("未选中对象")
            self._text.clear()
            return
        self._info.setText(f"{obj.type().name}  id={obj.id()[:8]}")
        # 浮雕模型含大网格 blob，优先用轻量摘要避免刷屏
        d = obj.summaryDict() if hasattr(obj, "summaryDict") else obj.serialize()
        lines = [f"{k}: {v}" for k, v in d.items()]
        self._text.setPlainText("\n".join(lines))

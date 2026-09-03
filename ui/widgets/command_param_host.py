"""命令参数面板宿主：画布右侧浮动容器。

仿原项目 RightParameterPanel：命令激活时主窗口把该命令的参数面板
塞进本容器并显示；命令结束时清空并隐藏。定位由主窗口负责
（贴画布右缘、ViewSwitchBar 参数按钮左侧）。
"""
from __future__ import annotations
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton

import ui.styles.theme as theme


class CommandParamHost(QWidget):
    closed = Signal()
    resized = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedWidth(220)
        self.setStyleSheet(
            "QWidget#ParamHost { background:%s; border:1px solid %s;"
            " border-radius:4px; }" % (theme.BG_PANEL, theme.BORDER))
        self.setObjectName("ParamHost")

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 4, 6, 6)
        root.setSpacing(2)

        titleBar = QHBoxLayout()
        titleBar.setSpacing(4)
        self._titleLabel = QLabel("参数设置")
        self._titleLabel.setStyleSheet(
            "font-weight:bold; color:%s;" % theme.PRIMARY_DARK)
        titleBar.addWidget(self._titleLabel, 1)
        btnClose = QPushButton("×")
        btnClose.setFixedSize(20, 20)
        btnClose.setCursor(Qt.PointingHandCursor)
        btnClose.setStyleSheet(
            "QPushButton { background:transparent; border:none; font-weight:bold; }"
            "QPushButton:hover { color:#c0392b; }")
        btnClose.clicked.connect(self.closed.emit)
        titleBar.addWidget(btnClose)
        root.addLayout(titleBar)

        self._contentLayout = QVBoxLayout()
        self._contentLayout.setContentsMargins(0, 0, 0, 0)
        self._contentLayout.setSpacing(0)
        root.addLayout(self._contentLayout)

        self._noCommandLabel = QLabel("无活动命令")
        self._noCommandLabel.setAlignment(Qt.AlignCenter)
        self._noCommandLabel.setStyleSheet("color:#999; padding:12px;")
        root.addWidget(self._noCommandLabel)

        self._content: QWidget | None = None

    def _fitHeight(self) -> None:
        """按内容自适应高度，且不超过画布可视高度。
        （本部件由主窗口 move() 定位，不 adjustSize 会被裁得只剩标题栏）"""
        self.adjustSize()
        parent = self.parentWidget()
        if parent is not None:
            cap = parent.height() - 40
            if cap > 60 and self.height() > cap:
                self.resize(self.width(), cap)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self.resized.emit()

    def setContentWidget(self, widget: QWidget, title: str = "参数设置") -> None:
        """挂载活动命令的参数面板。"""
        if self._content is widget:
            self._titleLabel.setText(title)
            return
        self.clearContent()
        self._noCommandLabel.hide()
        self._content = widget
        self._titleLabel.setText(title)
        self._contentLayout.addWidget(widget)
        widget.show()
        self._fitHeight()

    def clearContent(self) -> None:
        """移除当前面板（不销毁：归属命令实例管理）。"""
        if self._content is not None:
            self._contentLayout.removeWidget(self._content)
            self._content.setParent(None)
            self._content.hide()
            self._content = None
        self._titleLabel.setText("参数设置")
        self._noCommandLabel.show()
        self._fitHeight()

    def hasContent(self) -> bool:
        return self._content is not None

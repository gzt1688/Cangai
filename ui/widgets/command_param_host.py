"""命令参数面板宿主：画布右侧浮动容器。

仿原项目 RightParameterPanel：命令激活时主窗口把该命令的参数面板
塞进本容器并显示；命令结束时清空并隐藏。定位由主窗口负责
（贴画布右缘、ViewSwitchBar 参数按钮左侧）。
"""
from __future__ import annotations
from PySide6.QtCore import Qt, Signal, QTimer, QEvent
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QScrollArea)

import ui.styles.theme as theme


class CommandParamHost(QWidget):
    closed = Signal()
    resized = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedWidth(270)
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

        # 内容滚动区：面板高度超过画布可视高度时可滚动（不再裁掉参数）
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.NoFrame)
        # 纵向常需（内容高于画布时滚动）；横向仅兜底（正常不应出现）
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setStyleSheet(
            "QScrollArea { background:transparent; border:none; }"
            "QScrollArea > QWidget > QWidget { background:transparent; }"
            "QScrollBar:vertical { background:transparent; width:6px; }"
            "QScrollBar::handle:vertical { background:%s; border-radius:3px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical"
            " { height:0; }"
            "QScrollBar:horizontal { background:transparent; height:6px; }"
            "QScrollBar::handle:horizontal { background:%s; border-radius:3px; }"
            "QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal"
            " { width:0; }"
            % (theme.BORDER, theme.BORDER))
        self._contentLayout = QVBoxLayout()
        self._contentLayout.setContentsMargins(0, 0, 0, 0)
        self._contentLayout.setSpacing(0)
        holder = QWidget()
        holder.setLayout(self._contentLayout)
        self._scroll.setWidget(holder)
        root.addWidget(self._scroll)
        self._scroll.hide()

        self._noCommandLabel = QLabel("无活动命令")
        self._noCommandLabel.setAlignment(Qt.AlignCenter)
        self._noCommandLabel.setStyleSheet("color:#999; padding:12px;")
        root.addWidget(self._noCommandLabel)

        self._content: QWidget | None = None

    # ---------- 高度自适应 ----------

    def _chromeHeight(self) -> int:
        """宿主自身除内容外的固定高度：边距 + 标题栏 + 间距。"""
        lay = self.layout()
        m = lay.contentsMargins() if lay is not None else None
        chrome = (m.top() + m.bottom()) if m is not None else 10
        chrome += max(self._titleLabel.sizeHint().height(), 20)
        chrome += (lay.spacing() * 2) if lay is not None else 4
        return chrome

    def _fitHeight(self) -> None:
        """按内容自适应高度，且不超过画布可视高度；超出部分滚动。
        （本部件由主窗口 move() 定位，不 adjustSize 会被裁得只剩标题栏）"""
        content = self._content
        if content is None:
            lay = self.layout()
            if lay is not None:
                lay.invalidate()
                lay.activate()
            self.adjustSize()
            return
        # 面板完整高度 = 内容 sizeHint（隐藏行不占空间）
        lay = content.layout()
        if lay is not None:
            lay.invalidate()
            lay.activate()
        total = content.sizeHint().height() + self._chromeHeight()
        cap = 10000
        parent = self.parentWidget()
        if parent is not None:
            c = parent.height() - 40
            if c > 60:
                cap = c
        self.resize(self.width(), max(60, min(total, cap)))

    def eventFilter(self, obj, event) -> bool:
        # 内容布局变化（类型切换显隐参数行等）→ 延迟重算高度
        if obj is self._content and event.type() in (
                QEvent.LayoutRequest, QEvent.Resize):
            QTimer.singleShot(0, self._fitHeight)
        return super().eventFilter(obj, event)

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
        widget.installEventFilter(self)
        self._contentLayout.addWidget(widget)
        widget.show()
        self._scroll.show()
        self._fitHeight()

    def clearContent(self) -> None:
        """移除当前面板（不销毁：归属命令实例管理）。"""
        if self._content is not None:
            self._content.removeEventFilter(self)
            self._contentLayout.removeWidget(self._content)
            self._content.setParent(None)
            self._content.hide()
            self._content = None
        self._titleLabel.setText("参数设置")
        self._noCommandLabel.show()
        self._scroll.hide()
        self._fitHeight()

    def hasContent(self) -> bool:
        return self._content is not None

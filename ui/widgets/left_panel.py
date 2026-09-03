"""左侧面板：文件页、最近记录与设置入口。"""
from __future__ import annotations
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QPushButton,
                                QStackedWidget, QListWidget, QLabel, QFrame,
                                QLineEdit)
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QShortcut, QKeySequence, QIcon

from .vertical_tab_button import VerticalTabButton
import ui.styles.theme as theme
import core.app_settings as app_settings


class LeftPanelWidget(QWidget):
    """左侧面板：竖排切换标签 + 内容区，点击标签展开/收起。"""

    expandedChanged = Signal(bool)
    clearRecentRequested = Signal()
    settingsRequested = Signal()
    importRequested = Signal()
    openRequested = Signal()
    saveRequested = Signal()
    saveAsRequested = Signal()
    quickSaveRequested = Signal(int, str)
    exportRequested = Signal(str, str)

    TAB_BAR_WIDTH = VerticalTabButton.TAB_WIDTH
    STACK_MIN_WIDTH = 150

    def __init__(self) -> None:
        super().__init__()
        self._isExpanded = True
        self._activeTabIndex = 0
        self._expandedWidth = 220

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._tabBar = QWidget()
        self._tabBar.setFixedWidth(self.TAB_BAR_WIDTH)
        tabLayout = QVBoxLayout(self._tabBar)
        tabLayout.setContentsMargins(0, 4, 0, 4)
        tabLayout.setSpacing(3)
        tabLayout.setAlignment(Qt.AlignTop)

        self._tabButtons = []
        for index, title in enumerate(["文件保存", "最近保存"]):
            btn = VerticalTabButton(title)
            btn.setChecked(index == 0)
            btn.clicked.connect(lambda _, i=index: self._onTabClicked(i))
            tabLayout.addWidget(btn)
            if index == 0:
                tabLayout.addWidget(self._makeHLine())
            self._tabButtons.append(btn)
        tabLayout.addStretch()

        self._fileButtons = []
        self._saveBtn = None
        for index, (iconPath, tooltip, sig, isSave) in enumerate((
            ("icons/open.svg", "打开文件", self.openRequested, False),
            ("icons/import.svg", "导入文件", self.importRequested, False),
            ("icons/save.svg", "保存文件", self.saveRequested, True),
            ("icons/save_as.svg", "另存为", self.saveAsRequested, False),
        )):
            if index:
                tabLayout.addWidget(self._makeHSeparator())
            btn = QPushButton()
            btn.setIcon(QIcon(iconPath))
            btn.setToolTip(tooltip)
            btn.clicked.connect(sig.emit)
            if isSave:
                btn.setIconSize(QSize(26, 26))
                btn.setFixedSize(36, 52)
                self._saveBtn = btn
            else:
                btn.setIconSize(QSize(24, 24))
                btn.setFixedSize(36, 36)
                btn.setStyleSheet(
                    "QPushButton { border: none; border-radius: 4px; background: transparent; }"
                    "QPushButton:hover { background: #d0d0d0; }"
                )
                if iconPath == "icons/import.svg":
                    self._importBtn = btn
            tabLayout.addWidget(btn)
            self._fileButtons.append(btn)
        self._applySaveBtnStyle()

        tabLayout.addWidget(self._makeHSeparator())
        self._settingsBtn = QPushButton()
        self._settingsBtn.setIcon(QIcon("icons/settings.svg"))
        self._settingsBtn.setIconSize(QSize(24, 24))
        self._settingsBtn.setFixedSize(36, 36)
        self._settingsBtn.setToolTip("设置")
        self._settingsBtn.setStyleSheet(
            "QPushButton { border: none; border-radius: 4px; background: transparent; }"
            "QPushButton:hover { background: #d0d0d0; }"
        )
        self._settingsBtn.clicked.connect(self.settingsRequested.emit)
        tabLayout.addWidget(self._settingsBtn)

        self._separator = QFrame()
        self._separator.setFrameShape(QFrame.VLine)
        self._separator.setFixedWidth(1)
        self._separator.setStyleSheet("QFrame { background: #c8c8c8; border: none; }")

        self._stack = QStackedWidget()
        self._stack.setMinimumWidth(self.STACK_MIN_WIDTH)
        self._savePage = self._buildSavePage()
        self._recentPage = self._buildRecentPage()
        self._stack.addWidget(self._savePage)
        self._stack.addWidget(self._recentPage)

        outer.addWidget(self._tabBar)
        outer.addWidget(self._separator)
        outer.addWidget(self._stack, 1)

        shortcut = QShortcut(QKeySequence(Qt.Key_QuoteLeft), self)
        shortcut.activated.connect(self.toggleExpanded)

        self._applyWidths()

    def _makeHLine(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFixedHeight(1)
        line.setStyleSheet("QFrame { background: #c8c8c8; border: none; }")
        return line

    def _makeHSeparator(self) -> QFrame:
        """图标按钮之间的水平分割线。"""
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFixedWidth(28)
        sep.setFixedHeight(1)
        sep.setStyleSheet("QFrame { background: #c8c8c8; border: none; }")
        return sep

    def _buildSavePage(self) -> QWidget:
        page = QWidget()
        page.setLayout(QVBoxLayout())
        self._savePage = page
        self._refreshSavePage()
        return page

    def _refreshSavePage(self) -> None:
        layout = self._savePage.layout()
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
            elif item.layout():
                self._clearSubLayout(item.layout())
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        hasAny = False
        for i, slot in enumerate(app_settings.file_save_slots):
            if not slot["enabled"]:
                continue
            hasAny = True
            row = QHBoxLayout()
            row.setSpacing(0)
            lbl = QLabel(slot["name"])
            row.addWidget(lbl)
            edit = QLineEdit()
            edit.setPlaceholderText("文件名")
            edit.setText(slot.get("filename", ""))
            row.addWidget(edit, 1)
            row.addSpacing(4)
            btn = QPushButton("保存")
            btn.setFixedWidth(50)
            idx = i
            editRef = edit
            btn.clicked.connect(
                lambda _, ix=idx, e=editRef: self.quickSaveRequested.emit(ix, e.text()))
            row.addWidget(btn)
            layout.addLayout(row)
        if not hasAny:
            hint = QLabel("（请在设置中勾选文件保存项）")
            hint.setStyleSheet("color: #999;")
            layout.addWidget(hint)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet("QFrame { background: #c8c8c8; border: none; }")
        layout.addWidget(sep)

        for fmt in ("DXF", "STL"):
            row = QHBoxLayout()
            row.setSpacing(0)
            lbl = QLabel(fmt)
            row.addWidget(lbl)
            edit = QLineEdit()
            edit.setPlaceholderText("文件名")
            row.addWidget(edit, 1)
            row.addSpacing(4)
            btn = QPushButton("保存")
            btn.setFixedWidth(50)
            editRef = edit
            fmtRef = fmt
            btn.clicked.connect(
                lambda _, f=fmtRef, e=editRef: self.exportRequested.emit(f, e.text()))
            row.addWidget(btn)
            layout.addLayout(row)

        layout.addStretch()

    def _clearSubLayout(self, lay) -> None:
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
            elif item.layout():
                self._clearSubLayout(item.layout())

    def _buildRecentPage(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(5)
        layout.setAlignment(Qt.AlignTop)
        self._recentList = QListWidget()
        self._recentList.setMaximumHeight(200)
        self._recentList.setAlternatingRowColors(True)
        layout.addWidget(self._recentList)
        clearButton = QPushButton("清空列表")
        clearButton.setFixedHeight(25)
        clearButton.clicked.connect(self.clearRecentRequested.emit)
        layout.addWidget(clearButton)
        layout.addStretch()
        return page

    def _onTabClicked(self, index: int) -> None:
        if self._isExpanded and index == self._activeTabIndex:
            self.collapse()
            return
        self._activeTabIndex = index
        self._stack.setCurrentIndex(index)
        for i, btn in enumerate(self._tabButtons):
            btn.setChecked(i == index)
        if not self._isExpanded:
            self.expand()

    def _applyWidths(self) -> None:
        if self._isExpanded:
            self.setMinimumWidth(self.TAB_BAR_WIDTH + 1 + self.STACK_MIN_WIDTH)
        else:
            self.setMinimumWidth(self.TAB_BAR_WIDTH + 1)

    def expand(self) -> None:
        if self._isExpanded:
            return
        self._isExpanded = True
        self._separator.show()
        self._stack.show()
        self._applyWidths()
        self.expandedChanged.emit(True)

    def collapse(self) -> None:
        if not self._isExpanded:
            return
        self._expandedWidth = max(self.width(), self.TAB_BAR_WIDTH + 1 + self.STACK_MIN_WIDTH)
        self._isExpanded = False
        self._separator.hide()
        self._stack.hide()
        self._applyWidths()
        self.expandedChanged.emit(False)

    def toggleExpanded(self) -> None:
        if self._isExpanded:
            self.collapse()
        else:
            self.expand()

    def isExpanded(self) -> bool:
        return self._isExpanded

    def tabBarWidth(self) -> int:
        return self.TAB_BAR_WIDTH + 1

    def setExpandedWidth(self, width: int) -> None:
        self._expandedWidth = max(width, self.TAB_BAR_WIDTH + 1 + self.STACK_MIN_WIDTH)

    def expandedWidth(self) -> int:
        return self._expandedWidth

    def tabButtons(self):
        return self._tabButtons

    def saveButton(self) -> QPushButton:
        return self._saveBtn

    def refreshFileButtonsStyle(self) -> None:
        """主题色变化后刷新保存按钮样式。"""
        self._applySaveBtnStyle()

    def refreshSavePage(self) -> None:
        """设置变更后重建文件保存面板。"""
        self._refreshSavePage()

    def _applySaveBtnStyle(self) -> None:
        if self._saveBtn is None:
            return
        self._saveBtn.setStyleSheet(
            f"QPushButton {{ background: {theme.PRIMARY}; border: none; border-radius: 6px; }}"
            f"QPushButton:hover {{ background: {theme.PRIMARY_DARK}; }}"
            f"QPushButton:pressed {{ background: {theme.PRIMARY_DARK}; }}"
        )

    def recentList(self) -> QListWidget:
        return self._recentList

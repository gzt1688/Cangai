"""左侧面板：文件页、最近记录与设置入口。"""
from __future__ import annotations
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QPushButton,
                                QStackedWidget, QListWidget, QLabel, QFrame,
                                QLineEdit)
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QShortcut, QKeySequence, QIcon
from resource_path import resource_path

from .vertical_tab_button import VerticalTabButton
import ui.styles.theme as theme
import core.app_settings as app_settings
from i18n import tr, languageChanged


# 左侧面板竖排标签名（中文原文，retranslateUi 时 tr() 查表）
# 第二个 tab 原"最近保存"无功能，改为"CNC向导"（精雕 7 步流程引导）
_TAB_NAMES = ["文件保存", "CNC向导"]


class LeftPanelWidget(QWidget):
    """左侧面板：竖排切换标签 + 内容区，点击标签展开/收起。"""

    expandedChanged = Signal(bool)
    settingsRequested = Signal()
    importRequested = Signal()
    openRequested = Signal()
    saveRequested = Signal()
    saveAsRequested = Signal()
    quickSaveRequested = Signal(int, str)
    exportRequested = Signal(str, str)
    calcRequested = Signal()        # 材料成本计算器
    cncStepRequested = Signal(str)   # CNC 向导步骤点击
    cncEditPathRequested = Signal(object)  # 单击路径标题 → 编辑该条目

    TAB_BAR_WIDTH = VerticalTabButton.TAB_WIDTH
    STACK_MIN_WIDTH = 150

    def __init__(self) -> None:
        super().__init__()
        self._isExpanded = False   # 启动默认收起（点标签或 ` 键展开）
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
        # 竖排 tab 标题保持中文：英文无法竖排显示
        for index, title in enumerate(_TAB_NAMES):
            btn = VerticalTabButton(title)
            btn.setChecked(index == 0)
            btn.clicked.connect(lambda _, i=index: self._onTabClicked(i))
            tabLayout.addWidget(btn)
            if index == 0:
                tabLayout.addWidget(self._makeHLine())
            self._tabButtons.append(btn)
        tabLayout.addStretch()

        # 计算器按钮（打开文件上方）：材料成本计算
        tabLayout.addWidget(self._makeHSeparator())
        self._calcBtn = QPushButton()
        self._calcBtn.setIcon(QIcon(resource_path("icons/calculator.svg")))
        self._calcBtn.setIconSize(QSize(24, 24))
        self._calcBtn.setFixedSize(36, 36)
        self._calcBtn.setToolTip(tr("材料成本计算器"))
        self._calcBtn._tipText = "材料成本计算器"
        self._calcBtn.setStyleSheet(
            "QPushButton { border: none; border-radius: 4px; background: transparent; }"
            "QPushButton:hover { background: #d0d0d0; }"
        )
        self._calcBtn.clicked.connect(self.calcRequested.emit)
        tabLayout.addWidget(self._calcBtn)

        self._fileButtons = []
        self._saveBtn = None
        for index, (iconPath, tooltip, sig, isSave) in enumerate((
            (resource_path("icons/open.svg"), "打开文件", self.openRequested, False),
            (resource_path("icons/import.svg"), "导入文件", self.importRequested, False),
            (resource_path("icons/save.svg"), "保存文件", self.saveRequested, True),
            (resource_path("icons/save_as.svg"), "另存为", self.saveAsRequested, False),
        )):
            if index:
                tabLayout.addWidget(self._makeHSeparator())
            btn = QPushButton()
            btn.setIcon(QIcon(iconPath))
            btn.setToolTip(tr(tooltip))
            btn._tipText = tooltip
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
                if iconPath == resource_path("icons/import.svg"):
                    self._importBtn = btn
            tabLayout.addWidget(btn)
            self._fileButtons.append(btn)
        self._applySaveBtnStyle()

        tabLayout.addWidget(self._makeHSeparator())
        self._settingsBtn = QPushButton()
        self._settingsBtn.setIcon(QIcon(resource_path("icons/settings.svg")))
        self._settingsBtn.setIconSize(QSize(24, 24))
        self._settingsBtn.setFixedSize(36, 36)
        self._settingsBtn.setToolTip(tr("设置"))
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
        if not self._isExpanded:        # 启动即收起：只留竖排标签栏
            self._separator.hide()
            self._stack.hide()

        shortcut = QShortcut(QKeySequence(Qt.Key_QuoteLeft), self)
        shortcut.activated.connect(self.toggleExpanded)

        self._applyWidths()
        languageChanged().connect(self.retranslateUi)

    def retranslateUi(self) -> None:
        """语言切换：竖排 tab 标签保持中文，重设 tooltip 与保存页文本。"""
        for i, btn in enumerate(self._tabButtons):
            btn.setVerticalText(_TAB_NAMES[i])   # 英文无法竖排，保持中文
        for btn in self._fileButtons:
            tip = getattr(btn, "_tipText", None)
            if tip:
                btn.setToolTip(tr(tip))
        self._calcBtn.setToolTip(tr("材料成本计算器"))
        self._settingsBtn.setToolTip(tr("设置"))
        self._refreshSavePage()


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
            edit.setPlaceholderText(tr("文件名"))
            edit.setText(slot.get("filename", ""))
            row.addWidget(edit, 1)
            row.addSpacing(4)
            btn = QPushButton(tr("保存"))
            btn.setFixedWidth(50)
            idx = i
            editRef = edit
            btn.clicked.connect(
                lambda _, ix=idx, e=editRef: self.quickSaveRequested.emit(ix, e.text()))
            row.addWidget(btn)
            layout.addLayout(row)
        if not hasAny:
            hint = QLabel(tr("（请在设置中勾选文件保存项）"))
            hint.setStyleSheet("color: #999;")
            layout.addWidget(hint)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet("QFrame { background: #c8c8c8; border: none; }")
        layout.addWidget(sep)

        for fmt in ("DXF", "SVG", "STEP", "STL"):
            row = QHBoxLayout()
            row.setSpacing(0)
            lbl = QLabel(fmt)
            row.addWidget(lbl)
            edit = QLineEdit()
            edit.setPlaceholderText(tr("文件名"))
            row.addWidget(edit, 1)
            row.addSpacing(4)
            btn = QPushButton(tr("保存"))
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
        """第二个 tab：CNC 向导面板（精雕 7 步流程状态）。"""
        from .cnc_guide_panel import CncGuidePanel
        page = CncGuidePanel()
        page.stepRequested.connect(self.cncStepRequested.emit)
        page.editPathRequested.connect(self.cncEditPathRequested.emit)
        self._cncGuidePanel = page
        return page

    def setCncDocument(self, doc) -> None:
        """把文档实例传给 CNC 向导面板，用于毛坯显隐控制。"""
        panel = getattr(self, "_cncGuidePanel", None)
        if panel is not None:
            panel.setDocument(doc)

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

    def setActiveTab(self, index: int) -> None:
        """外部切换 tab（如 CNC 模式切到向导 tab，其他模式切回文件 tab）。
        只切换内容页，不改变左侧栏展开/收起状态。"""
        if not (0 <= index < len(self._tabButtons)):
            return
        self._activeTabIndex = index
        self._stack.setCurrentIndex(index)
        for i, btn in enumerate(self._tabButtons):
            btn.setChecked(i == index)

    def cncGuidePanel(self):
        """CNC 向导面板实例（供外部刷新/连接信号）。"""
        return getattr(self, "_cncGuidePanel", None)

"""左侧面板：文件页、最近记录与设置入口。"""
from __future__ import annotations
import os
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QGridLayout,
                                QPushButton, QStackedWidget, QListWidget,
                                QListWidgetItem, QLabel, QFrame, QLineEdit,
                                QAbstractItemView)
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
    recentOpenRequested = Signal(str)   # 最近打开列表点击 → 打开该文件
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
        # （计算器/设置按钮已移至自绘标题栏 TitleBar，此处仅保留文件按钮）

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

        # （设置按钮已移至自绘标题栏 TitleBar）
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

    def _fileEdit(self, text: str = "") -> QLineEdit:
        """保存页/导出行的文件名输入框：最小 60px，随面板宽度自动拉宽。

        行用网格排版：输入框列吃满剩余宽度，「保存」按钮固定在面板右侧；
        面板不拉宽时也不会把行最小宽度顶出面板（按钮始终可见）。
        """
        edit = QLineEdit()
        edit.setPlaceholderText(tr("文件名"))
        edit.setText(text)
        edit.setMinimumWidth(60)
        return edit

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

        # 先收集行：文件槽 + 导出格式；用网格排版，使 7 个「保存」按钮严格对齐
        slot_rows = [(slot["name"], slot.get("filename", ""), i)
                     for i, slot in enumerate(app_settings.file_save_slots)
                     if slot["enabled"]]

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(6)
        r = 0
        lblW = 0
        for name, text, idx in slot_rows:
            lbl = QLabel(name)
            lblW = max(lblW, lbl.sizeHint().width())
            grid.addWidget(lbl, r, 0)
            edit = self._fileEdit(text)
            grid.addWidget(edit, r, 1)
            btn = QPushButton(tr("保存"))
            btn.setFixedWidth(50)
            btn.clicked.connect(
                lambda _, ix=idx, e=edit: self.quickSaveRequested.emit(ix, e.text()))
            grid.addWidget(btn, r, 2)
            r += 1
        if not slot_rows:
            hint = QLabel(tr("（请在设置中勾选文件保存项）"))
            hint.setStyleSheet("color: #999;")
            grid.addWidget(hint, r, 0, 1, 3)
            r += 1

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet("QFrame { background: #c8c8c8; border: none; }")
        grid.addWidget(sep, r, 0, 1, 3)
        r += 1

        for fmt in ("DXF", "SVG", "STEP", "STL"):
            lbl = QLabel(fmt)
            lblW = max(lblW, lbl.sizeHint().width())
            grid.addWidget(lbl, r, 0)
            edit = self._fileEdit("")
            grid.addWidget(edit, r, 1)
            btn = QPushButton(tr("保存"))
            btn.setFixedWidth(50)
            btn.clicked.connect(
                lambda _, f=fmt, e=edit: self.exportRequested.emit(f, e.text()))
            grid.addWidget(btn, r, 2)
            r += 1

        grid.setColumnMinimumWidth(0, lblW)
        grid.setColumnStretch(1, 1)     # 输入框吃满剩余宽度（面板拉宽 → 输入框跟随）
        grid.setColumnStretch(2, 0)     # 按钮列固定宽度 → 始终贴面板右侧
        layout.addLayout(grid)

        # ---- 最近打开的文件（分割线下方，单击快捷打开）----
        layout.addWidget(self._makeHLine())
        title = QLabel(tr("最近打开"))
        title.setStyleSheet("color: #888;")
        layout.addWidget(title)

        recent = [p for p in getattr(app_settings, "recent_files", [])
                  if os.path.exists(p)]
        if recent:
            lst = QListWidget()
            lst.setFrameShape(QFrame.NoFrame)
            lst.setStyleSheet(
                "QListWidget { background: transparent; border: none; }"
                # 每项底部一条分割线，便于区分相邻文件
                "QListWidget::item { padding: 4px 4px; border-bottom: 1px solid #d9d9d9; }"
                "QListWidget::item:hover { background: #e8e8e8; }"
                "QListWidget::item:selected { background: #dcdcdc; color: #222; }")
            lst.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
            lst.setTextElideMode(Qt.ElideMiddle)
            lst.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            for path in recent[:app_settings.RECENT_MAX]:
                it = QListWidgetItem(os.path.basename(path))
                it.setToolTip(path)
                it.setData(Qt.UserRole, path)
                lst.addItem(it)
            lst.itemClicked.connect(self._onRecentItemClicked)
            layout.addWidget(lst, 1)
        else:
            hint = QLabel(tr("（暂无最近打开的文件）"))
            hint.setStyleSheet("color: #999;")
            layout.addWidget(hint)
            layout.addStretch()

    def _onRecentItemClicked(self, item) -> None:
        """单击最近打开列表项 → 请求主窗口打开该文件。"""
        path = item.data(Qt.UserRole)
        if path:
            self.recentOpenRequested.emit(str(path))

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

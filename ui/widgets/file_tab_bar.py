"""文件标签栏：画布右下角悬浮的多文件切换标签。

- 点击标签切换文档；“＋”新建文档；
- 右键标签弹出菜单（重命名 / 关闭文件）——不设 × 按钮，保持标签简洁；
- 样式沿用画布浮层（ViewSwitchBar）的浅色方案：未选灰底、选中主题色。
标签数量随文档增减整体重建（文档数很少，重建成本可忽略）。
"""
from __future__ import annotations
from PySide6.QtWidgets import QWidget, QHBoxLayout, QPushButton, QMenu
from PySide6.QtCore import Qt, Signal
import ui.styles.theme as theme
from i18n import tr, languageChanged


def _tabStyle(selected: bool) -> str:
    if selected:
        return (
            "QPushButton { background:%s; color:#fff; font-size:12px; font-weight:bold;"
            " border:1px solid %s; border-radius:3px; padding:1px 8px; }"
        ) % (theme.PRIMARY, theme.PRIMARY)
    return (
        "QPushButton { background:#e0e0e0; color:#333; font-size:12px; font-weight:bold;"
        " border:1px solid #a0a0a0; border-radius:3px; padding:1px 8px; }"
        "QPushButton:hover { background:#d0d0d0; }"
    )


def _plusStyle() -> str:
    return (
        "QPushButton { background:#e0e0e0; color:#333; font-size:13px; font-weight:bold;"
        " border:1px solid #a0a0a0; border-radius:3px; padding:0px; }"
        "QPushButton:hover { background:#d0d0d0; }"
    )


class FileTabBar(QWidget):
    currentChanged = Signal(int)    # 点击标签切换文档
    renameRequested = Signal(int)   # 标签右键菜单“重命名”
    closeRequested = Signal(int)    # 标签右键菜单“关闭文件”
    newRequested = Signal()         # 点击“＋”新建文档

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setStyleSheet("border:none; background:transparent;")
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(3)
        self._tabs: list = []      # [(容器, 名称按钮)]
        self._closable: list = []  # 每标签是否允许关闭（与 _tabs 同序）
        self._current = -1

        self._plusBtn = QPushButton("＋", self)
        self._plusBtn.setFixedSize(22, 20)
        self._plusBtn.setToolTip(tr("新建文件"))
        self._plusBtn.setCursor(Qt.PointingHandCursor)
        self._plusBtn.setStyleSheet(_plusStyle())
        self._plusBtn.clicked.connect(self.newRequested.emit)
        self._row.addWidget(self._plusBtn)
        self.adjustSize()
        languageChanged().connect(self.retranslateUi)

    def retranslateUi(self) -> None:
        """语言切换：重设 tooltip（标签文本由 setTabs 重建）。"""
        self._plusBtn.setToolTip(tr("新建文件"))

    def setTabs(self, titles: list, dirtyFlags: list, current: int,
                closableFlags: list = None) -> None:
        """重建标签：titles 为文档标题，dirtyFlags 为未保存标记，current 为当前索引，
        closableFlags 为每标签是否允许关闭（None 表示全部允许）。"""
        if closableFlags is None:
            closableFlags = [True] * len(titles)
        for container, _ in self._tabs:
            self._row.removeWidget(container)
            container.deleteLater()
        self._tabs = []
        self._closable = list(closableFlags)
        self._current = current
        for i, title in enumerate(titles):
            selected = (i == current)
            closable = bool(closableFlags[i]) if i < len(closableFlags) else True
            cont = QWidget(self)
            cont.setStyleSheet("border:none; background:transparent;")
            lay = QHBoxLayout(cont)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(0)
            # 未保存标记：标题前圆点（宽度变化即视觉提示）
            nameBtn = QPushButton(("● " if dirtyFlags[i] else "") + title, cont)
            nameBtn.setFixedHeight(20)
            nameBtn.setCursor(Qt.PointingHandCursor)
            nameBtn.setToolTip(
                tr(f"{title}（右键：重命名/关闭文件）") if closable
                else tr(f"{title}（右键：重命名；最后一个文档，不可关闭）"))
            nameBtn.setStyleSheet(_tabStyle(selected))
            nameBtn.clicked.connect(lambda _, k=i: self.currentChanged.emit(k))
            nameBtn.setContextMenuPolicy(Qt.CustomContextMenu)
            nameBtn.customContextMenuRequested.connect(
                lambda pos, k=i, b=nameBtn: self._showTabMenu(k, b.mapToGlobal(pos)))
            lay.addWidget(nameBtn)
            # 父组件已显示时新建的子控件默认隐藏，布局会忽略隐藏项
            cont.show()
            self._row.insertWidget(self._row.count() - 1, cont)
            self._tabs.append((cont, nameBtn))
        self.adjustSize()

    def _showTabMenu(self, index: int, globalPos) -> None:
        """标签右键菜单：重命名 / 关闭文件。"""
        menu = self._buildTabMenu(index)
        chosen = menu.exec(globalPos)
        menu.deleteLater()   # 用完即销毁：避免每次右键累积一个 QMenu
        if chosen is None:
            return
        # 父级标签栏样式表为透明背景，构建菜单时已设显式样式（否则黑底看不清）
        if chosen.text() == tr("重命名"):
            self.renameRequested.emit(index)
        elif chosen.text() == tr("关闭文件"):
            self.closeRequested.emit(index)

    def _buildTabMenu(self, index: int) -> QMenu:
        """构建标签右键菜单；最后一个空文档的“关闭文件”置灰。"""
        menu = QMenu(self)
        menu.setStyleSheet((
            "QMenu { background:#e8e8e8; color:#000000; border:1px solid #a0a0a0; }"
            "QMenu::item { background:transparent; color:#000000; padding:3px 18px; }"
            "QMenu::item:selected { background:%s; color:#ffffff; }"
        ) % theme.PRIMARY)
        menu.addAction(tr("重命名"))
        act = menu.addAction(tr("关闭文件"))
        if index < 0 or index >= len(self._closable) or not self._closable[index]:
            act.setEnabled(False)
        return menu

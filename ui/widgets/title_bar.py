"""Windows 11 风格自绘标题栏：应用标题 + 分类标签 + 窗口控制按钮。

配合主窗口的 Qt.FramelessWindowHint 使用（无边框窗口）：
- 左侧：应用图标 + 窗口标题；
- 中部：分类标签栏（曲线绘制/曲线编辑/浮雕…，由 TopDock.detachNavBar 摘出嵌入，
  切换分类即切换顶部工具按钮页）；
- 右侧：最小化 / 最大化-还原 / 关闭（Win11 扁平悬停，关闭键悬停红色）。
- 空白处按住拖动 = 移动窗口（startSystemMove，支持 Aero Snap）；
  双击空白 = 最大化/还原。

功能零变更：仅视觉与窗口外观，全部窗口行为走系统原语。
"""
from __future__ import annotations

from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QPushButton, QTabBar
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QIcon, QFont

import ui.styles.theme as theme
from ui.widgets.top_dock import darkIcon
from resource_path import resource_path
from i18n import tr, languageChanged


def _titleBarQss() -> str:
    return f"""
    QWidget#titleBar {{
        background: {theme.BG};
        border-bottom: 1px solid {theme.BORDER};
    }}
    QLabel#titleIcon, QLabel#titleLabel {{
        background: transparent;
        color: {theme.TEXT};
    }}
    QPushButton#winBtn {{
        background: transparent;
        color: {theme.TEXT};
        border: none;
        border-radius: 0px;
        padding: 0px;
        font-family: "Segoe UI Symbol";
        font-size: 10pt;
    }}
    QPushButton#winBtn:hover {{
        background: rgba(0, 0, 0, 0.055);
    }}
    QPushButton#winBtn:pressed {{
        background: rgba(0, 0, 0, 0.09);
    }}
    QPushButton#winBtn:disabled {{
        color: #a8a8a8;
        background: transparent;
    }}
    QPushButton#closeBtn {{
        background: transparent;
        color: {theme.TEXT};
        border: none;
        border-radius: 0px;
        padding: 0px;
        font-family: "Segoe UI Symbol";
        font-size: 10pt;
    }}
    QPushButton#closeBtn:hover {{
        background: #c42b1c;
        color: #ffffff;
    }}
    QPushButton#closeBtn:pressed {{
        background: #a92b1f;
        color: #ffffff;
    }}
    """


class TitleBar(QWidget):
    """自绘标题栏。window 为主窗口（用于拖动/最大化），navBar 为可空的分类标签栏。

    布局：应用图标 | 分类标签 | 文件标题 | 弹性 | 计算器 设置 | 最小化 最大化 关闭
    """

    HEIGHT = 34

    calcRequested = Signal()      # 材料成本计算器
    settingsRequested = Signal()  # 设置

    def __init__(self, window, navBar: QTabBar | None = None) -> None:
        super().__init__(window)
        self._win = window
        self._navBar = navBar
        self.setObjectName("titleBar")
        self.setStyleSheet(_titleBarQss())

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 0, 1)
        lay.setSpacing(8)

        # 应用图标（窗口图标的 20px 缩略）
        self._iconLabel = QLabel(self)
        self._iconLabel.setObjectName("titleIcon")
        self._iconLabel.setFixedSize(20, 20)
        self._iconLabel.setScaledContents(True)
        icon = window.windowIcon()
        if icon is not None and not icon.isNull():
            self._iconLabel.setPixmap(icon.pixmap(QSize(20, 20)))
        lay.addWidget(self._iconLabel)

        lay.addSpacing(4)

        # 分类标签栏（来自 TopDock，切换 = 切换工具按钮页）。
        # 关闭滚动箭头：空间不足时改为隐藏文件标题（见 _relayoutTitle），
        # 并动态抬高主窗口最小宽度，保证所有标签始终完整可见。
        # 标题栏高度贴着标签实际高度（不留死区），窗口按钮随之同步。
        if navBar is not None:
            navBar.setParent(self)
            navBar.setUsesScrollButtons(False)
            lay.addWidget(navBar)
            self._fitNavHeight()
        else:
            self.setFixedHeight(self.HEIGHT)

        lay.addSpacing(4)

        # 文件标题：位于所有标签右侧（随 setWindowTitle 自动刷新）
        self._titleLabel = QLabel(window.windowTitle(), self)
        self._titleLabel.setObjectName("titleLabel")
        self._titleLabel.setStyleSheet("font-size: 9pt; color: %s;" % theme.TEXT_DIM)
        # 主窗口 setWindowTitle 时标题自动跟随（含文档名拼接）
        window.windowTitleChanged.connect(self._titleLabel.setText)
        lay.addWidget(self._titleLabel)

        lay.addStretch(1)

        # 工具按钮：成本计算器 / 设置（Win11 扁平图标按钮）
        self._calcBtn = self._makeToolBtn("icons/calculator.svg", tr("材料成本计算器"))
        self._calcBtn.clicked.connect(self.calcRequested.emit)
        self._settingsBtn = self._makeToolBtn("icons/settings.svg", tr("设置"))
        self._settingsBtn.clicked.connect(self.settingsRequested.emit)

        # 窗口控制按钮（最小化 / 最大化-还原 / 关闭）
        btnSize = QSize(44, self.HEIGHT - 1)
        self._minBtn = QPushButton("\u2500", self)   # 一
        self._maxBtn = QPushButton("\u25a1", self)   # □
        self._closeBtn = QPushButton("\u2715", self)  # ✕
        for b in (self._minBtn, self._maxBtn, self._closeBtn):
            b.setFixedSize(btnSize)
            b.setCursor(Qt.ArrowCursor)
            b.setFocusPolicy(Qt.NoFocus)
        self._minBtn.setObjectName("winBtn")
        self._maxBtn.setObjectName("winBtn")
        self._closeBtn.setObjectName("closeBtn")
        self._minBtn.setToolTip(tr("最小化"))
        self._maxBtn.setToolTip(tr("最大化"))
        self._closeBtn.setToolTip(tr("关闭"))
        self._minBtn.clicked.connect(self._win.showMinimized)
        self._maxBtn.clicked.connect(self._toggleMaximize)
        self._closeBtn.clicked.connect(self._win.close)
        lay.addWidget(self._calcBtn)
        lay.addWidget(self._settingsBtn)
        lay.addWidget(self._minBtn)
        lay.addWidget(self._maxBtn)
        lay.addWidget(self._closeBtn)

        # 主窗口最小宽度 = 标签完整放下所需（插件加页时随之抬高）
        self._updateMinWidth()
        languageChanged().connect(self.retranslateUi)

    def _chromeWidth(self) -> int:
        """标题栏除标签/标题外的固定占用：图标 + 右侧按钮组 + 间距余量。"""
        return (20 + 4 + 4 + 8 * 3 +
                self._calcBtn.width() + self._settingsBtn.width() +
                self._minBtn.width() + self._maxBtn.width() +
                self._closeBtn.width() + 10)

    def _updateMinWidth(self) -> None:
        """主窗口最小宽度保证所有分类标签完整可见（不出滚动箭头）。"""
        if self._navBar is None:
            return
        need = self._chromeWidth() + self._navBar.minimumSizeHint().width()
        self._win.setMinimumWidth(max(960, need))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayoutTitle()

    def _relayoutTitle(self) -> None:
        """空间不足时先隐藏文件标题（标签优先，避免出现滚动箭头/截断）。"""
        if self._navBar is None or not hasattr(self, "_calcBtn"):
            return   # 构造尚未完成（setFixedHeight 触发的早期 resize）
        need = (self._chromeWidth() +
                self._navBar.minimumSizeHint().width() + 60)
        self._titleLabel.setVisible(self.width() >= need)

    def _makeToolBtn(self, iconPath: str, tip: str) -> QPushButton:
        """标题栏扁平图标工具按钮（计算器/设置），悬停浅灰。"""
        btn = QPushButton(self)
        btn.setObjectName("winBtn")
        btn.setIcon(darkIcon(resource_path(iconPath)))
        btn.setIconSize(QSize(18, 18))
        btn.setFixedSize(QSize(36, self.HEIGHT - 1))
        btn.setCursor(Qt.ArrowCursor)
        btn.setFocusPolicy(Qt.NoFocus)
        btn.setToolTip(tip)
        return btn
    def _fitNavHeight(self) -> None:
        """标题栏高度 = 分类标签实际高度 + 上下各 2px（消除标签下死区）。"""
        if self._navBar is None:
            return
        hint = max(24, self._navBar.sizeHint().height())
        self._navBar.setFixedHeight(hint)
        self.HEIGHT = hint + 4
        self.setFixedHeight(self.HEIGHT)

    def retranslateUi(self) -> None:
        self._minBtn.setToolTip(tr("最小化"))
        self._maxBtn.setToolTip(tr("还原" if self._isMaximized() else "最大化"))
        self._closeBtn.setToolTip(tr("关闭"))
        self._calcBtn.setToolTip(tr("材料成本计算器"))
        self._settingsBtn.setToolTip(tr("设置"))

    # ---- 窗口状态 ----

    def _isMaximized(self) -> bool:
        return bool(self._win.windowState() & Qt.WindowMaximized)

    def syncMaxButton(self, maximized: bool) -> None:
        """主窗口状态变化时同步最大化按钮图标与提示。"""
        self._maxBtn.setText("\u2750" if maximized else "\u25a1")   # ❐ 还原 / □ 最大化
        self._maxBtn.setToolTip(tr("还原" if maximized else "最大化"))

    def _toggleMaximize(self) -> None:
        if self._isMaximized():
            self._win.showNormal()
        else:
            # 记录最大化前几何（还原时用）；全屏状态不做最大化
            if not (self._win.windowState() & Qt.WindowFullScreen):
                self._win._preMaxGeo = self._win.geometry()
            self._win.showMaximized()

    # ---- 拖动 / 双击 ----

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            handle = self._win.windowHandle()
            if handle is not None:
                handle.startSystemMove()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        # 双击标题栏空白 = 最大化/还原（点在标签/按钮上时事件不会到达这里）
        if event.button() == Qt.LeftButton:
            self._toggleMaximize()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def refreshStyle(self) -> None:
        """字体大小等变化后：刷新样式并按新标签高度重排标题栏。"""
        self.setStyleSheet(_titleBarQss())
        self._fitNavHeight()
        if hasattr(self, "_minBtn"):
            h = max(22, self.HEIGHT - 1)
            for b in (self._calcBtn, self._settingsBtn,
                      self._minBtn, self._maxBtn, self._closeBtn):
                b.setFixedSize(b.width(), h)
        self._updateMinWidth()
        self._relayoutTitle()

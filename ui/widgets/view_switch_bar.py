"""视图方向切换条：顶 / 前 / 侧 / 透视。"""
from __future__ import annotations
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QPushButton
from PySide6.QtCore import Qt, Signal, QEvent
import ui.styles.theme as theme

_NORMAL_BG = "#e0e0e0"
_HOVER_BG = "#d0d0d0"


def _labelNormal() -> str:
    return (
        "QLabel { background:%s; color:#333; font-size:12px; font-weight:bold;"
        " border:1px solid #a0a0a0; border-radius:3px; }"
        "QLabel:hover { background:%s; }"
    ) % (_NORMAL_BG, _HOVER_BG)


def _labelSelected() -> str:
    return (
        "QLabel { background:%s; color:#fff; font-size:12px; font-weight:bold;"
        " border:1px solid #a0a0a0; border-radius:3px; }"
    ) % theme.PRIMARY


def _btnStyle() -> str:
    return (
        "QPushButton { background:%s; color:#333; font-size:12px; font-weight:bold;"
        " border:1px solid #a0a0a0; border-radius:3px; padding:0px; }"
        "QPushButton:hover { background:%s; }"
    ) % (_NORMAL_BG, _HOVER_BG)


def _labelLocked() -> str:
    return (
        "QLabel { background:#efefef; color:#b0b0b0; font-size:12px; font-weight:bold;"
        " border:1px solid #d0d0d0; border-radius:3px; }"
    )


class ViewSwitchBar(QWidget):
    viewChanged = Signal(int)
    fitAllClicked = Signal()
    paramPanelClicked = Signal()
    imageSelected = Signal(str)
    mcpPanelClicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedWidth(25)
        self.setStyleSheet("border:none;")

        layout = QVBoxLayout(self)
        layout.setSpacing(0)
        layout.setContentsMargins(0, 5, 0, 5)
        layout.setAlignment(Qt.AlignTop)

        self._labels: list[tuple[QLabel, int]] = []
        for idx, text in [(0, "透\n视"), (1, "顶\n视"), (2, "前\n视"), (3, "右\n视")]:
            lbl = QLabel(text, self)
            lbl.setFixedSize(25, 50)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setCursor(Qt.PointingHandCursor)
            lbl.installEventFilter(self)
            self._labels.append((lbl, idx))
            layout.addWidget(lbl)

        layout.addSpacing(2)
        self._fitAllBtn = QPushButton("全\n观\n察", self)
        self._fitAllBtn.setFixedSize(25, 60)
        self._fitAllBtn.setStyleSheet(_btnStyle())
        self._fitAllBtn.clicked.connect(self.fitAllClicked.emit)
        layout.addWidget(self._fitAllBtn)

        layout.addSpacing(2)
        self._paramBtn = QPushButton("参\n数", self)
        self._paramBtn.setFixedSize(25, 50)
        self._paramBtn.setStyleSheet(_btnStyle())
        self._paramBtn.clicked.connect(self.paramPanelClicked.emit)
        layout.addWidget(self._paramBtn)

        layout.addSpacing(2)
        self._imageBtn = QPushButton("图\n片", self)
        self._imageBtn.setFixedSize(25, 50)
        self._imageBtn.setStyleSheet(_btnStyle())
        self._imageBtn.clicked.connect(self._onImageClicked)
        layout.addWidget(self._imageBtn)

        layout.addSpacing(2)
        self._aiBtn = QPushButton("Ai", self)
        self._aiBtn.setFixedSize(25, 40)
        self._aiBtn.setStyleSheet(_btnStyle())
        self._aiBtn.clicked.connect(self.mcpPanelClicked.emit)
        layout.addWidget(self._aiBtn)

        self._current = 1
        self._locked = False
        self._updateSelection(1)

        totalH = layout.sizeHint().height()
        self.setFixedSize(25, totalH)

    def _updateSelection(self, idx: int) -> None:
        self._current = idx
        self._refreshLabelStyles()

    def _refreshLabelStyles(self) -> None:
        for lbl, i in self._labels:
            if self._locked and i != self._current:
                lbl.setStyleSheet(_labelLocked())
            elif i == self._current:
                lbl.setStyleSheet(_labelSelected())
            else:
                lbl.setStyleSheet(_labelNormal())

    def setCurrent(self, idx: int) -> None:
        """外部同步选中项（不触发 viewChanged 信号）。"""
        self._updateSelection(idx)

    def current(self) -> int:
        return self._current

    def setLocked(self, locked: bool) -> None:
        """锁定视图方向：锁定后四个方向标签不可点击，仅显示当前方向。"""
        self._locked = locked
        self._refreshLabelStyles()

    def isLocked(self) -> bool:
        return self._locked

    def refreshStyle(self) -> None:
        for btn in (self._fitAllBtn, self._paramBtn, self._imageBtn, self._aiBtn):
            btn.setStyleSheet(_btnStyle())
        self._refreshLabelStyles()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.MouseButtonPress:
            for lbl, i in self._labels:
                if obj is lbl and not self._locked:
                    self._updateSelection(i)
                    self.viewChanged.emit(i)
                    break
        return super().eventFilter(obj, event)

    def _onImageClicked(self) -> None:
        import os
        from PySide6.QtWidgets import QFileDialog
        # 默认打开到用户桌面；获取失败时回退到用户目录（HOME / USERPROFILE）
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        if not os.path.isdir(desktop):
            # Windows 可能还有其他语言本地化名的桌面；尝试 Shell 特殊路径
            try:
                import ctypes
                from ctypes import wintypes
                CSIDL_DESKTOPDIRECTORY = 0x0010
                SHGFP_TYPE_CURRENT = 0
                buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
                ctypes.windll.shell32.SHGetFolderPathW(
                    0, CSIDL_DESKTOPDIRECTORY, 0, SHGFP_TYPE_CURRENT, buf)
                if buf.value and os.path.isdir(buf.value):
                    desktop = buf.value
            except Exception:
                desktop = os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, "选择图片", desktop,
            "图片文件 (*.png *.jpg *.jpeg *.bmp *.gif *.tiff *.webp *.svg *.ico);;"
            "所有文件 (*)")
        if path:
            self.imageSelected.emit(path)

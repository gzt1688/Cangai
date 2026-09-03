"""捕捉工具栏：捕捉类型开关、禁用、右键独选与调色板。"""
from __future__ import annotations
from typing import Dict, Optional
from PySide6.QtWidgets import QToolBar, QPushButton, QLabel, QFrame, QSizePolicy
from PySide6.QtCore import Qt, Signal, QEvent
from PySide6.QtGui import QColor
import ui.styles.theme as theme
from ui.widgets.paint_state import PaintState


class CaptureToolbar(QToolBar):
    snapTypeToggled = Signal(str, bool)

    # 捕捉类型按钮（key, 中文标签）：10 个类型全部默认开启
    _BTN_DEFS = [("endpoint", "端点"), ("nearest", "最近点"), ("point", "节点"),
                 ("midpoint", "中点"), ("center", "圆心"), ("intersection", "交点"),
                 ("perpendicular", "垂足"), ("tangent", "切点"),
                 ("quarter", "象限点"), ("vertex", "顶点")]
    _HIGHLIGHT = "#15b6c9"

    def __init__(self) -> None:
        super().__init__("捕捉")
        self.setMovable(False)
        # 禁用右键弹出的工具栏上下文菜单（"颜色栏"区域右键不再弹菜单）
        self.setContextMenuPolicy(Qt.PreventContextMenu)
        self._buttons: Dict[str, QPushButton] = {}
        # 右键独选快照：非空时表示处于"独选"模式，再次右键恢复
        self._snapshot: Optional[Dict[str, bool]] = None
        # 禁用（停用）按钮按下前的状态快照
        self._disabledSnapshot: Optional[Dict[str, bool]] = None

        title = QLabel("捕捉:")
        title.setFixedHeight(24)
        title.setStyleSheet("font-weight: bold; font-size: 13px; padding: 0 6px;")
        self.addWidget(title)

        for key, label in self._BTN_DEFS:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(True)  # 原项目：全部默认开启
            btn.setFixedHeight(24)
            btn.setMinimumWidth(40)
            btn.setStyleSheet(self._btnStyle())
            btn.setContextMenuPolicy(Qt.CustomContextMenu)
            btn.toggled.connect(lambda checked, k=key: self.snapTypeToggled.emit(k, checked))
            btn.customContextMenuRequested.connect(lambda _, k=key: self._onButtonRightClicked(k))
            self.addWidget(btn)
            self._buttons[key] = btn

        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setFixedHeight(20)
        sep.setStyleSheet("color: #ccc;")
        self.addWidget(sep)

        # 禁用按钮：勾选时停用全部捕捉并记住原状态，再按一次恢复（参考原项目"停用"）
        self._noneButton = QPushButton("禁用")
        self._noneButton.setCheckable(True)
        self._noneButton.setFixedHeight(24)
        self._noneButton.setMinimumWidth(40)
        self._noneButton.setStyleSheet(self._btnStyle())
        self._noneButton.toggled.connect(self._onDisableToggled)
        self._noneButton.toggled.connect(lambda checked: self.snapTypeToggled.emit("none", checked))
        self.addWidget(self._noneButton)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.VLine)
        sep2.setFixedHeight(20)
        sep2.setStyleSheet("color: #ccc;")
        self.addWidget(sep2)

        colorTitle = QLabel("当前颜色:")
        colorTitle.setFixedHeight(24)
        colorTitle.setStyleSheet("font-weight: bold; font-size: 13px; padding: 0 6px;")
        self.addWidget(colorTitle)

        self._colorBox = QLabel()
        self._colorBox.setFixedWidth(28)
        self._colorBox.setSizePolicy(QLabel().sizePolicy().horizontalPolicy(), QSizePolicy.Expanding)
        self._colorBox.setToolTip("当前颜色（空格填色 / W 切换）")
        self.addWidget(self._colorBox)

        sep3 = QFrame()
        sep3.setFrameShape(QFrame.VLine)
        sep3.setFixedHeight(20)
        sep3.setStyleSheet("color: #ccc;")
        self.addWidget(sep3)

        self._paletteBoxes: list[QLabel] = []
        for i in range(PaintState.count()):
            box = QLabel()
            box.setFixedWidth(18)
            box.setSizePolicy(QLabel().sizePolicy().horizontalPolicy(), QSizePolicy.Expanding)
            box.setToolTip(f"{PaintState.hexAt(i)}（点击设为当前颜色）")
            box.setCursor(Qt.PointingHandCursor)
            box.installEventFilter(self)
            self.addWidget(box)
            self._paletteBoxes.append(box)

        # 当前颜色以 PaintState 为唯一真相源：切换时刷新当前色块 + 调色板高亮
        self._paint = PaintState.instance()
        self._paint.currentChanged.connect(self._applyCurrentColor)
        self._applyCurrentColor(self._paint.currentIndex())

    # ------------------------------------------------------------------
    # 调色板 / 当前颜色（PaintState 驱动）
    # ------------------------------------------------------------------
    def _applyCurrentColor(self, index: int) -> None:
        """当前颜色变化：刷新“当前颜色”块 + 高亮调色板对应色块。"""
        pal = PaintState.palette()
        if not pal:
            return
        idx = int(index) % len(pal)
        r, g, b = pal[idx]
        self._colorBox.setStyleSheet(
            f"background: #{r:02x}{g:02x}{b:02x};"
            " border: 1px solid #999; border-radius: 2px;"
        )
        for i, box in enumerate(self._paletteBoxes):
            rr, gg, bb = pal[i]
            bg = f"background: #{rr:02x}{gg:02x}{bb:02x};"
            if i == idx:
                box.setStyleSheet(
                    f"{bg} border: 2px solid {self._HIGHLIGHT}; border-radius: 2px;"
                )
            else:
                box.setStyleSheet(f"{bg} border: 1px solid #888; border-radius: 2px;")

    def eventFilter(self, obj, event) -> bool:
        """调色板色块点击 → 设为当前颜色。"""
        if event.type() == QEvent.MouseButtonPress and obj in self._paletteBoxes:
            PaintState.instance().setCurrentIndex(self._paletteBoxes.index(obj))
            return True
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------
    # 样式
    # ------------------------------------------------------------------
    @classmethod
    def _btnStyle(cls) -> str:
        hl = theme.PRIMARY
        hover = QColor(hl).lighter(175).name()
        return (
            "QPushButton { padding: 0 4px; border: 1px solid #ccc; border-radius: 2px;"
            " background: transparent; color: #333; }"
            f"QPushButton:hover {{ background: {hover}; }}"
            f"QPushButton:checked {{ background: {hl}; color: white; border: 1px solid {hl}; }}"
            "QPushButton:disabled { background: #f0f0f0; color: #999; }"
        )

    def refreshStyle(self) -> None:
        """主题色变化后刷新捕捉按钮高亮样式。"""
        style = self._btnStyle()
        for btn in self._buttons.values():
            btn.setStyleSheet(style)
        self._noneButton.setStyleSheet(style)

    # ------------------------------------------------------------------
    # 禁用（停用）按钮：保存/恢复全部捕捉状态（参考原项目 onDisableToggled）
    # ------------------------------------------------------------------
    def _onDisableToggled(self, checked: bool) -> None:
        if checked:
            self._disabledSnapshot = {k: btn.isChecked() for k, btn in self._buttons.items()}
            for btn in self._buttons.values():
                btn.setChecked(False)   # 触发 snapTypeToggled，逐个移除
                btn.setEnabled(False)
        else:
            snapshot = self._disabledSnapshot or {}
            for key, btn in self._buttons.items():
                btn.setEnabled(True)
                btn.setChecked(snapshot.get(key, False))  # 触发 snapTypeToggled，逐个恢复
            self._disabledSnapshot = None

    # ------------------------------------------------------------------
    # 右键独选：第一次右键 → 只保留该类型；再次右键 → 恢复原状态
    # （参考原项目 onCaptureButtonRightClicked）
    # ------------------------------------------------------------------
    def _onButtonRightClicked(self, key: str) -> None:
        if self._snapshot is None:
            self._snapshot = {k: btn.isChecked() for k, btn in self._buttons.items()}
            for k, btn in self._buttons.items():
                btn.setChecked(k == key)   # 触发 snapTypeToggled，同步开关
        else:
            for k, btn in self._buttons.items():
                btn.setChecked(self._snapshot.get(k, False))
            self._snapshot = None

    def currentTypes(self) -> list[str]:
        return [k for k, btn in self._buttons.items() if btn.isChecked()]

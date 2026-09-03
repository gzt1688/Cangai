"""快捷命令条：常用绘图命令按钮。"""
from __future__ import annotations
from PySide6.QtCore import Signal, QSize, QPoint, Qt
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QFrame, QLabel, QPushButton

import ui.styles.theme as theme
from .text_icon import makeTextIcon


# 常用按钮栏：按功能分组，只放高频命令
QUICK_GROUPS = [
    [("Point", "点"), ("Line", "直线"), ("Curve", "样条"), ("Circle", "圆"),
     ("Ellipse", "椭圆"), ("Arc", "圆弧"), ("Rectangle", "矩形"), ("Polygon", "多边形"), ("Text", "文字")],
    [("Offset", "偏移"), ("Trim", "修剪"), ("Extend", "延伸"), ("Mirror", "镜像"),
     ("Chamfer", "倒角"), ("Array", "阵列"), ("Join", "组合"), ("Group", "群组"),
     ("ClosePath", "闭合"), ("Outline", "外轮廓"), ("Align", "对齐")],
    # 浮雕组：撤销/重做自成一套（浮雕历史），与顶部“撤销/重做”（曲线历史）相互独立
    [("NewModel", "新建模型"), ("ReliefUndo", "浮雕撤销"), ("ReliefRedo", "浮雕重做"),
     ("Solidify", "固化"), ("AddMaterial", "堆料"),
     ("RegionRelief", "区域浮雕"), ("Stamp", "冲压"), ("SweepMaterial", "导动堆料"),
     ("Smooth", "磨光"), ("Move", "移动")],
    # 视图组（占位）
    [("ZoomFit", "重置视图"), ("Pan", "平移")],
]

# 撤销/重做（置于"常用"标题后最前的动作按钮，不属于命令分组）。
# 顶部这对 = 曲线历史的撤销/重做；浮雕组内的 浮雕撤销/浮雕重做 才是浮雕历史的。
ACTION_BUTTONS = [("Undo", "撤销"), ("Redo", "重做")]

# 顶部动作按钮提示文字
_ACTION_TOOLTIP = {
    "Undo": "撤销曲线操作 (Ctrl+Z)",
    "Redo": "重做曲线操作 (Ctrl+Shift+Z / Ctrl+Y)",
}

# 常用栏图标映射（与 top_dock 共用同一套 SVG）
ICON_MAP = {
    "Undo": "icons/undo.svg",
    "Redo": "icons/redo.svg",
    "ReliefUndo": "icons/undo.svg",
    "ReliefRedo": "icons/redo.svg",
    "Point": "icons/point.svg",
    "Line": "icons/line.svg",
    "Circle": "icons/circle.svg",
    "Ellipse": "icons/ellipse.svg",
    "Arc": "icons/arc.svg",
    "Curve": "icons/curve.svg",
    "Rectangle": "icons/rectangle.svg",
    "Polygon": "icons/polygon.svg",
    "Offset": "icons/offset.svg",
    "Trim": "icons/trim.svg",
    "Extend": "icons/extend.svg",
    "Mirror": "icons/mirror.svg",
    "Chamfer": "icons/chamfer.svg",
    "Array": "icons/array.svg",
    "Join": "icons/join.svg",
    "Group": "icons/group.svg",
    "ClosePath": "icons/closepath.svg",
    "Outline": "icons/outline.svg",
    "Align": "icons/align.svg",
}
# 用 QPainter 绘制中文的命令
TEXT_ICON_KEYS = {"Text": "字"}
# 动作类按钮（发射动作信号，而非命令信号）
ACTION_KEYS = {"Undo", "Redo", "ReliefUndo", "ReliefRedo"}

# 浮雕相关命令：仅在“浮雕”画布模式下可用，其余模式置灰禁用
RELIEF_COMMAND_KEYS = {
    "NewModel", "ReliefUndo", "ReliefRedo", "Solidify", "AddMaterial",
    "RegionRelief", "Stamp", "SweepMaterial", "Smooth", "Move",
}


class QuickAccessBar(QWidget):
    """常用按钮栏：位于工具按钮栏下方，集中高频命令。"""

    commandRequested = Signal(str)
    undoRequested = Signal()
    redoRequested = Signal()
    reliefUndoRequested = Signal()
    reliefRedoRequested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(44)
        # 浮雕撤销/重做的“模式可用性”与“各自历史可用性”分存，叠加后决定按钮状态
        self._reliefModeOn = False
        self._reliefUndoAvail = False
        self._reliefRedoAvail = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 1, 0, 0)
        outer.setSpacing(0)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFixedHeight(1)
        line.setStyleSheet("QFrame { background: #c8c8c8; border: none; }")
        outer.addWidget(line)

        layout = QHBoxLayout()
        layout.setContentsMargins(4, 3, 4, 3)
        layout.setSpacing(4)

        title = QLabel("常用")
        title.setFixedWidth(32)
        title.setStyleSheet("color: #555555; background: transparent;")
        layout.addWidget(title)

        self._buttons = {}
        self._actionButtons = []
        # 最前的撤销/重做动作按钮（中性配色，独立于命令分组）
        for key, text in ACTION_BUTTONS:
            btn = QPushButton(self._wrapText(text))
            btn.setFixedSize(36, 36)
            btn.setToolTip(_ACTION_TOOLTIP.get(key, text))
            iconPath = ICON_MAP.get(key)
            if iconPath is not None:
                btn.setIcon(QIcon(iconPath))
                btn.setIconSize(QSize(32, 32))
                btn.setText("")
            btn.setStyleSheet(self._actionStyle())
            self._connectButton(btn, key)
            layout.addWidget(btn)
            self._buttons[key] = btn
            self._actionButtons.append(btn)
        layout.addWidget(self._makeSeparator())

        self._buttonGroups = []
        for index, group in enumerate(QUICK_GROUPS):
            if index:
                layout.addWidget(self._makeSeparator())
            group_btns = []
            for key, text in group:
                btn = QPushButton(self._wrapText(text))
                # 正方形按钮，预留空间给后续替换为图标
                btn.setFixedSize(36, 36)
                btn.setToolTip(
                    text + "（右键取消群组）" if key == "Group" else text)
                iconPath = ICON_MAP.get(key)
                if iconPath is not None:
                    btn.setIcon(QIcon(iconPath))
                    btn.setIconSize(QSize(32, 32))
                    btn.setText("")
                elif key in TEXT_ICON_KEYS:
                    btn.setIcon(makeTextIcon(TEXT_ICON_KEYS[key], 64))
                    btn.setIconSize(QSize(32, 32))
                    btn.setText("")
                self._connectButton(btn, key)
                # 新建模型按钮：右键直接执行重构网格
                if key == "NewModel":
                    btn.setContextMenuPolicy(Qt.CustomContextMenu)
                    btn.customContextMenuRequested.connect(
                        lambda pos, k=key: self.commandRequested.emit("ReconstructRelief"))
                if key == "Group":
                    btn.setContextMenuPolicy(Qt.CustomContextMenu)
                    btn.customContextMenuRequested.connect(
                        lambda pos: self.commandRequested.emit("Ungroup"))
                layout.addWidget(btn)
                self._buttons[key] = btn
                group_btns.append(btn)
            self._buttonGroups.append(group_btns)
        layout.addStretch()
        outer.addLayout(layout)
        self._reliefButtons = [self._buttons[k]
                               for k in RELIEF_COMMAND_KEYS if k in self._buttons]
        self.refreshStyle()

    def _connectButton(self, btn, key: str) -> None:
        """动作按钮发动作信号，其余发命令信号。"""
        if key == "Undo":
            btn.clicked.connect(self.undoRequested.emit)
        elif key == "Redo":
            btn.clicked.connect(self.redoRequested.emit)
        elif key == "ReliefUndo":
            btn.clicked.connect(self.reliefUndoRequested.emit)
        elif key == "ReliefRedo":
            btn.clicked.connect(self.reliefRedoRequested.emit)
        else:
            btn.clicked.connect(lambda _, k=key: self.commandRequested.emit(k))


    def setUndoEnabled(self, on: bool) -> None:
        """顶部“撤销”（曲线历史）可用状态。"""
        btn = self._buttons.get("Undo")
        if btn is not None:
            btn.setEnabled(on)

    def setRedoEnabled(self, on: bool) -> None:
        """顶部“重做”（曲线历史）可用状态。"""
        btn = self._buttons.get("Redo")
        if btn is not None:
            btn.setEnabled(on)

    def setReliefUndoEnabled(self, on: bool) -> None:
        """浮雕撤销可用状态（还需浮雕模式开启才真正可点）。"""
        self._reliefUndoAvail = on
        self._applyReliefHistoryButtons()

    def setReliefRedoEnabled(self, on: bool) -> None:
        """浮雕重做可用状态（还需浮雕模式开启才真正可点）。"""
        self._reliefRedoAvail = on
        self._applyReliefHistoryButtons()

    def _applyReliefHistoryButtons(self) -> None:
        """浮雕撤销/重做 = 浮雕模式开启 且 该方向历史非空。"""
        for key, avail in (("ReliefUndo", self._reliefUndoAvail),
                           ("ReliefRedo", self._reliefRedoAvail)):
            btn = self._buttons.get(key)
            if btn is not None:
                btn.setEnabled(self._reliefModeOn and avail)

    def setReliefButtonsEnabled(self, on: bool) -> None:
        """浮雕命令按钮仅在浮雕模式下可用；撤销/重做再叠加各自历史状态。"""
        self._reliefModeOn = on
        for btn in self._reliefButtons:
            btn.setEnabled(on)
        self._applyReliefHistoryButtons()

    @staticmethod
    def _wrapText(text: str) -> str:
        """4 字以上按钮文字拆成两行，避免固定尺寸下显示不全。"""
        if len(text) >= 4:
            half = (len(text) + 1) // 2
            return text[:half] + "\n" + text[half:]
        return text

    def refreshStyle(self) -> None:
        """按主题中的分组颜色刷新各按钮背景（设置-外观改色后调用）。"""
        style = self._actionStyle()
        for btn in self._actionButtons:
            btn.setStyleSheet(style)
        for index, btns in enumerate(self._buttonGroups):
            color = theme.QUICK_GROUP_COLORS[index % len(theme.QUICK_GROUP_COLORS)]
            hover = QColor(color).darker(112).name()
            pressed = QColor(color).darker(120).name()
            qss = (
                f"QPushButton {{ background: {color}; color: white;"
                " border: none; border-radius: 2px; padding: 0px; }"
                f"QPushButton:hover {{ background: {hover}; }}"
                f"QPushButton:pressed {{ background: {pressed}; }}"
                "QPushButton:disabled { background: #c8c8c8; color: #888888; }"
            )
            for btn in btns:
                btn.setStyleSheet(qss)

    @staticmethod
    def _actionStyle() -> str:
        """撤销/重做动作按钮样式：主题色背景 + 禁用态置灰。"""
        color = theme.PRIMARY
        return (
            f"QPushButton {{ background: {color}; color: white;"
            " border: none; border-radius: 2px; padding: 0px; }"
            f"QPushButton:hover {{ background: {theme.PRIMARY_DARK}; }}"
            f"QPushButton:pressed {{ background: {QColor(color).darker(120).name()}; }}"
            "QPushButton:disabled { background: #c8c8c8; }"
        )

    def _makeSeparator(self) -> QFrame:
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setFixedWidth(1)
        sep.setStyleSheet("QFrame { background: #c8c8c8; border: none; }")
        return sep

    def button(self, key: str):
        return self._buttons.get(key)

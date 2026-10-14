"""快捷命令条：常用绘图命令按钮。"""
from __future__ import annotations
from PySide6.QtCore import Signal, QSize, QPoint, Qt
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QFrame, QLabel, QPushButton

import ui.styles.theme as theme
from .text_icon import makeTextIcon
from resource_path import resource_path
from i18n import tr, languageChanged


# 常用按钮栏：按功能分组，只放高频命令
QUICK_GROUPS = [
    [("Point", "点"), ("Line", "直线"), ("Curve", "样条"), ("Circle", "圆"),
     ("Ellipse", "椭圆"), ("Arc", "圆弧"), ("Rectangle", "矩形"), ("Polygon", "多边形"), ("Text", "文字")],
    [("Offset", "偏移"), ("Trim", "修剪"), ("Extend", "延伸"), ("Mirror", "镜像"),
     ("Chamfer", "倒角"), ("Array", "阵列"), ("Join", "组合"), ("Group", "群组"),
     ("ClosePath", "闭合"), ("Outline", "轮廓"), ("Align", "对齐"),
     ("Bridge", "桥接")],
    # 浮雕组：撤销/重做自成一套（浮雕历史），与顶部“撤销/重做”（曲线历史）相互独立
    [("NewModel", "新建模型"), ("ReliefUndo", "浮雕撤销"), ("ReliefRedo", "浮雕重做"),
     ("Solidify", "固化"), ("AddMaterial", "塑形"),
     ("RegionRelief", "区域塑形"), ("Stamp", "冲压"), ("SweepMaterial", "路径塑形"),
     ("Smooth", "磨光"), ("DeJitter", "消除锯齿"), ("Move", "移动")],

]

# 撤销/重做（置于"常用"标题后最前的动作按钮，不属于命令分组）。
# 顶部这对 = 曲线历史的撤销/重做；浮雕组内的 浮雕撤销/浮雕重做 才是浮雕历史的。
ACTION_BUTTONS = [("Undo", "撤销"), ("Redo", "重做")]

# 顶部动作按钮提示（极简规则）：
#   - 普通按钮（撤销/重做/新建模型等）→ 空，鼠标悬停不显示提示。
#   - 有左右键二合一功能的按钮 → 仅在代码处直接写 左：…\n右：…（两行中文）。
#   因此 ACTION_TOOLTIP 默认全留空（不提示）。
_ACTION_TOOLTIP: dict[str, str] = {}

# 常用栏图标映射（与 top_dock 共用同一套 SVG）
ICON_MAP = {
    "Undo": resource_path("icons/undo.svg"),
    "Redo": resource_path("icons/redo.svg"),
    "ReliefUndo": resource_path("icons/undo.svg"),
    "ReliefRedo": resource_path("icons/redo.svg"),
    "Point": resource_path("icons/point.svg"),
    "Line": resource_path("icons/line.svg"),
    "Circle": resource_path("icons/circle.svg"),
    "Ellipse": resource_path("icons/ellipse.svg"),
    "Arc": resource_path("icons/arc.svg"),
    "Curve": resource_path("icons/curve.svg"),
    "Rectangle": resource_path("icons/rectangle.svg"),
    "Polygon": resource_path("icons/polygon.svg"),
    "Offset": resource_path("icons/offset.svg"),
    "Trim": resource_path("icons/trim.svg"),
    "Extend": resource_path("icons/extend.svg"),
    "Mirror": resource_path("icons/mirror.svg"),
    "Chamfer": resource_path("icons/chamfer.svg"),
    "Array": resource_path("icons/array.svg"),
    "Join": resource_path("icons/join.svg"),
    "Group": resource_path("icons/group.svg"),
    "ClosePath": resource_path("icons/closepath.svg"),
    "Outline": resource_path("icons/outline.svg"),
    "Align": resource_path("icons/align.svg"),
    "Bridge": resource_path("icons/bridge.svg"),
}
# 用 QPainter 绘制中文的命令
TEXT_ICON_KEYS = {"Text": "字"}
# 动作类按钮（发射动作信号，而非命令信号）
ACTION_KEYS = {"Undo", "Redo", "ReliefUndo", "ReliefRedo"}

# 浮雕相关命令：仅在“浮雕”画布模式下可用，其余模式置灰禁用
RELIEF_COMMAND_KEYS = {
    "NewModel", "ReliefUndo", "ReliefRedo", "Solidify", "AddMaterial",
    "RegionRelief", "Stamp", "SweepMaterial", "Smooth", "Move", "SweepSmooth",
    "DeJitter", "RegionTransform", "RegionDeform", "LevelCalibrate", "RegionSlope",
    "RevolveSlope", "ModelScale",
    "TextureFill", "SlopeZone", "RestBoundary",
}


class QuickAccessBar(QWidget):
    """常用按钮栏：位于工具按钮栏下方，集中高频命令。"""

    commandRequested = Signal(str)
    undoRequested = Signal()
    redoRequested = Signal()
    reliefUndoRequested = Signal()
    reliefRedoRequested = Signal()
    cureRequested = Signal()    # 固化：当前高度快照为基准（Solidify 左键）
    flowRequested = Signal()    # 流化：高度恢复至固化基准（Solidify 右键）

    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(44)
        # 浮雕撤销/重做的“模式可用性”与“各自历史可用性”分存，叠加后决定按钮状态
        self._reliefModeOn = False
        self._reliefUndoAvail = False
        self._reliefRedoAvail = False
        # 按钮 → 中文原文（retranslateUi 用）
        self._buttonTexts: dict[QPushButton, str] = {}
        # 按钮 → 中文 tooltip 原文（retranslateUi 用）
        self._buttonTooltips: dict[QPushButton, str] = {}
        # 按钮 → 是否有图标（retranslateUi 跳过有图标按钮不写字）
        self._buttonHasIcon: dict[QPushButton, bool] = {}

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

        self._titleLabel = QLabel(tr("常用"))
        self._titleLabel.setFixedWidth(32)
        self._titleLabel.setStyleSheet("color: #555555; background: transparent;")
        layout.addWidget(self._titleLabel)

        self._buttons = {}
        self._actionButtons = []
        # 最前的撤销/重做动作按钮（中性配色，独立于命令分组）
        for key, text in ACTION_BUTTONS:
            btn = QPushButton(self._wrapText(tr(text)))
            btn.setFixedSize(36, 36)
            # 撤销/重做按钮名已一眼看懂 → 不设 tooltip，悬停无信息噪声
            override = _ACTION_TOOLTIP.get(key, "")
            if override:
                btn.setToolTip(tr(override))
                self._buttonTooltips[btn] = override
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
            self._buttonTexts[btn] = text
            self._buttonHasIcon[btn] = True   # Undo/Redo 有图标
        layout.addWidget(self._makeSeparator())

        self._buttonGroups = []
        for index, group in enumerate(QUICK_GROUPS):
            if index:
                layout.addWidget(self._makeSeparator())
            group_btns = []
            for key, text in group:
                btn = QPushButton(self._wrapText(tr(text)))
                # 正方形按钮，预留空间给后续替换为图标
                btn.setFixedSize(36, 36)
                # 极简悬停规则：
                #   普通按钮（无左右键功能）→ 不设 tooltip，悬停不显示。
                #   左右键二合一按钮：在下方分支里按 左：…\n右：… 格式写。
                iconPath = ICON_MAP.get(key)
                hasIcon = False
                if iconPath is not None:
                    btn.setIcon(QIcon(iconPath))
                    btn.setIconSize(QSize(32, 32))
                    btn.setText("")
                    hasIcon = True
                elif key in TEXT_ICON_KEYS:
                    btn.setIcon(makeTextIcon(TEXT_ICON_KEYS[key], 64))
                    btn.setIconSize(QSize(32, 32))
                    btn.setText("")
                    hasIcon = True
                if not hasIcon:
                    self._applyQuickTextStyle(btn, text)
                if key == "Solidify":
                    # 固化/流化二合一：左键固化（快照基准）、右键流化（恢复基准）
                    btn.setToolTip(tr("左：固化\n右：流化"))
                    self._buttonTooltips[btn] = "左：固化\n右：流化"
                    btn.clicked.connect(self.cureRequested.emit)
                    btn.setContextMenuPolicy(Qt.CustomContextMenu)
                    btn.customContextMenuRequested.connect(
                        lambda pos: self.flowRequested.emit())
                else:
                    self._connectButton(btn, key)
                # 新建模型：左键新建，右键=重构网格
                if key == "NewModel":
                    btn.setToolTip(tr("左：新建模型\n右：重构网格"))
                    self._buttonTooltips[btn] = "左：新建模型\n右：重构网格"
                    btn.setContextMenuPolicy(Qt.CustomContextMenu)
                    btn.customContextMenuRequested.connect(
                        lambda pos, k=key: self.commandRequested.emit("ReconstructRelief"))
                if key == "Group":
                    btn.setToolTip(tr("左：新建群组\n右：取消群组"))
                    self._buttonTooltips[btn] = "左：新建群组\n右：取消群组"
                    btn.setContextMenuPolicy(Qt.CustomContextMenu)
                    btn.customContextMenuRequested.connect(
                        lambda pos: self.commandRequested.emit("Ungroup"))
                # 路径塑形：左键=路径塑形（导动堆料）
                if key == "SweepMaterial":
                    btn.setToolTip(tr("路径塑形"))
                    self._buttonTooltips[btn] = "路径塑形"
                # 磨光：左键=磨光（按住拖动），右键=路径磨光（沿曲线磨光）
                if key == "Smooth":
                    btn.setToolTip(tr("左：磨光\n右：路径磨光"))
                    self._buttonTooltips[btn] = "左：磨光\n右：路径磨光"
                    btn.setContextMenuPolicy(Qt.CustomContextMenu)
                    btn.customContextMenuRequested.connect(
                        lambda pos: self.commandRequested.emit("SweepSmooth"))
                layout.addWidget(btn)
                self._buttons[key] = btn
                self._buttonTexts[btn] = text
                self._buttonHasIcon[btn] = hasIcon
                group_btns.append(btn)
            self._buttonGroups.append(group_btns)
        layout.addStretch()
        outer.addLayout(layout)
        self._reliefButtons = [self._buttons[k]
                               for k in RELIEF_COMMAND_KEYS if k in self._buttons]
        self.refreshStyle()
        languageChanged().connect(self.retranslateUi)

    def _applyQuickTextStyle(self, btn, text: str) -> None:
        """无图标按钮字号：英文缩小显示全（36×36 按钮）。"""
        from i18n import getLanguage
        if getLanguage() == "English":
            en = tr(text)
            wrapped = self._wrapText(en)
            if "\n" in wrapped:
                lines = wrapped.split("\n")
                mx = max(len(l) for l in lines)
                fs = 9 if mx <= 4 else 8
            else:
                n = len(wrapped)
                fs = 10 if n <= 4 else 8
            btn.setStyleSheet(f"QPushButton {{ font-size:{fs}px; padding:0px; }}")

    def retranslateUi(self) -> None:
        """语言切换：重设标题、按钮文本与 tooltip。有图标按钮不写字。"""
        self._titleLabel.setText(tr("常用"))
        for btn, text in self._buttonTexts.items():
            if self._buttonHasIcon.get(btn, False):
                continue   # 有图标不写字
            btn.setText(self._wrapText(tr(text)))
            self._applyQuickTextStyle(btn, text)
        for btn, tip in self._buttonTooltips.items():
            btn.setToolTip(tr(tip))

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

    def _applyQuickTextStyle(self, btn, text: str) -> None:
        """无图标的文本按钮：套用统一动作按钮样式。"""
        btn.setStyleSheet(self._actionStyle())

    def _makeSeparator(self) -> QFrame:
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setFixedWidth(1)
        sep.setStyleSheet("QFrame { background: #c8c8c8; border: none; }")
        return sep

    def button(self, key: str):
        return self._buttons.get(key)

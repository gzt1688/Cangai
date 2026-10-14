"""顶部停靠区：命令分类标签与对应工具按钮。"""
from __future__ import annotations
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QTabBar, QStackedWidget,
                                QPushButton)
from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QIcon

import ui.styles.theme as theme
from .flow_layout import FlowLayout
from .quick_access_bar import QuickAccessBar
from .text_icon import makeTextIcon
from resource_path import resource_path
from i18n import tr, languageChanged


# 绘图命令图标映射（key -> icons/<name>.svg）；无映射的命令仍显示文字
ICON_MAP = {
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
    "ImageTrace": resource_path("icons/imagetrace.svg"),
    "ImageToModel": resource_path("icons/imagetomodel.svg"),
    "ReliefUndo": resource_path("icons/undo.svg"),
    "ReliefRedo": resource_path("icons/redo.svg"),
}
# 用 QPainter 绘制中文的命令（Qt SVG 渲染器对中文字体支持弱）
TEXT_ICON_KEYS = {"Text": "字"}

# 按钮悬停提示（统一极简规则）：
#   1) 没有左右键功能的普通按钮 → 不在此表写，直接不显示悬停提示。
#   2) 有左/右键两种操作的按钮 → 严格两行：
#         左：操作名
#         右：操作名
#      仅写中文功能名，不含命令名/快捷键/参数说明/版本兼容说明。
TOOLTIP_OVERRIDES = {
    # 曲线编辑
    "Group":  "左：新建群组\n右：取消群组",
    "Join":   "左：组合多条曲线\n右：断开组合",
    # 浮雕
    "Brush":  "左：固化（抬高）\n右：流化（压低）",
    "Smooth": "左：磨光\n右：路径磨光",
    "Stamp":  "左：冲压（抬高）\n右：凹压（压低）",
    "Blend":  "左：柔化边界\n右：硬化边界",
    "Flatten":"左：抹平\n右：取消抹平",
    "ModelScale": "模型扩缩：扩展或收缩模型边界",
    "RegionRelief": "左：区域塑形（叠加）\n右：区域塑形（挖除）",
    # 视图
    "ZoomWindow": "左：窗口放大\n右：窗口缩小",
    "ZoomFit":    "左：整体观察\n右：选中观察",
    "Pan":        "左：平移\n右：旋转视图",
    # 3D B-Rep（Plasticity 风格）
    "Boolean3D":  "3D 布尔：选中两个实体，并/差/交（差集=第一个减第二个）",
    "Thread":     "螺纹：面模式选中圆柱面生成内/外螺纹；空白点击生成独立螺柱",
}


# 工具按钮栏的全部分类：导航栏每个标签对应一页按钮
CATEGORIES = [
    ("曲线绘制", [
        ("Point", "点"), ("Line", "直线"), ("Curve", "样条"), ("Circle", "圆"),
        ("Ellipse", "椭圆"), ("Arc", "圆弧"), ("Rectangle", "矩形"),
        ("Polygon", "多边形"), ("Text", "文字"), ("ImageTrace", "图转线"),
    ]),
    ("曲线编辑", [
        ("Offset", "偏移"), ("Trim", "修剪"), ("Extend", "延伸"), ("Mirror", "镜像"),
        ("Chamfer", "倒角"), ("Array", "阵列"), ("Join", "组合"), ("Group", "群组"),
        ("ClosePath", "闭合"), ("Outline", "轮廓"), ("Align", "对齐"),
        ("Bridge", "桥接"),

    ]),
    ("浮雕", [
        ("NewModel", "新建\n模型"), ("ImageToModel", "图转\n模型"), ("ModelScale", "模型\n扩缩"),
        ("AddMaterial", "塑形"), ("RegionRelief", "区域\n塑形"), ("SweepMaterial", "路径\n塑形"),
        ("Stamp", "冲压"), ("Smooth", "磨光"), ("DeJitter", "消除\n锯齿"),
        ("Move", "移动"), ("Solidify", "固化"),
        ("ReliefUndo", "浮雕\n撤销"), ("ReliefRedo", "浮雕\n重做"),
    ]),
    ("浮雕几何", [
        ("RegionTransform", "区域\n变换"),
        ("RegionArray", "区域\n阵列"),
        ("RegionSymmetry", "区域\n对称"),
        ("RegionDeform", "区域\n变形"),
        ("LevelCalibrate", "水平\n校准"),
        ("RegionSlope", "斜面"),
        ("RevolveSlope", "旋转\n面"),
        ("TextureFill", "纹理\n填充"),
        ("SlopeZone", "构造\n曲线"),
    ]),
    ("3D", [
        ("Extrude", "挤出"),
        ("Boolean3D", "布尔"), ("Thread", "螺纹"),
    ]),
    ("CNC", [
        ("ImportToCnc", "导入\nCNC"),
        ("CncMachine", "机床\n设置"),
        ("CncStock", "安装\n几何体"),
        ("CncTools", "刀具表"),
        ("CncProfile", "生成\n刀路"),
        ("CncCollision", "碰撞\n检查"),
        ("CncSimulate", "实体\n模拟"),
        ("CncOutput", "路径\n输出"),
    ]),
]


class TopDock(QWidget):
    commandRequested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(0)

        self._navBar = QTabBar()
        self._navBar.setExpanding(False)
        self._navBar.setDrawBase(False)
        self.refreshStyle()
        bl.addWidget(self._navBar)

        self._toolStack = QStackedWidget()
        self._toolStack.setFixedHeight(50)
        # 组名 → 该页的 FlowLayout：内置分类用 CATEGORIES 构建，插件组运行时追加
        self._groupFlows: dict = {}
        # 插件按钮登记：key → (按钮, pluginId)（插件停用时移除）
        self._pluginButtons: dict = {}
        # 按钮 → 中文文本（retranslateUi 用）
        self._buttonTexts: dict[QPushButton, str] = {}
        self._buttonCategory: dict[QPushButton, str] = {}
        self._buttonHasIcon: dict[QPushButton, bool] = {}
        # tab 索引 → 中文分类名（retranslateUi 用）
        self._tabNames: list[str] = []
        for name, items in CATEGORIES:
            self._navBar.addTab(tr(name))
            self._tabNames.append(name)
            self._toolStack.addWidget(self._buildPage(name, items, self._groupFlows))
        bl.addWidget(self._toolStack)

        self._quickBar = QuickAccessBar()
        bl.addWidget(self._quickBar)
        outer.addWidget(bottom)

        self._navBar.currentChanged.connect(self._toolStack.setCurrentIndex)
        self._quickBar.commandRequested.connect(self.commandRequested.emit)

        self.setFixedHeight(bottom.sizeHint().height())
        languageChanged().connect(self.retranslateUi)

    def retranslateUi(self) -> None:
        """语言切换：重设导航标签与按钮文本/提示。有图标按钮不写字。"""
        for i, name in enumerate(self._tabNames):
            self._navBar.setTabText(i, tr(name))
        for btn, text in self._buttonTexts.items():
            if self._buttonHasIcon.get(btn, False):
                continue   # 有图标不写字
            btn.setText(tr(text))
            self._applyButtonTextStyle(btn, text, self._buttonCategory.get(btn, ""))
        for key, tip in TOOLTIP_OVERRIDES.items():
            btn = self._findButtonByKey(key)
            if btn is not None:
                btn.setToolTip(tr(tip))
        if hasattr(self._quickBar, "retranslateUi"):
            self._quickBar.retranslateUi()

    def _findButtonByKey(self, key: str) -> QPushButton | None:
        """按 key 反查按钮（retranslateUi 刷 tooltip / 模式门禁测试用）。
        覆盖内置按钮与插件按钮（两者都带 _cmdKey）。"""
        for btn, _text in self._buttonTexts.items():
            if getattr(btn, "_cmdKey", None) == key:
                return btn
        entry = self._pluginButtons.get(key)
        if entry is not None:
            return entry[0]
        return None

    def _buildPage(self, category: str, items, flows: dict | None = None) -> QWidget:
        page = QWidget()
        flow = FlowLayout(page, margin=1, spacing=3)
        for key, text in items:
            flow.addWidget(self._makeButton(key, text, category))
        if flows is not None:
            flows[self._navBar.tabText(self._navBar.count() - 1)] = flow
        return page

    def addToolButton(self, group: str, key: str, label: str, tooltip: str = "",
                      icon: str = "", action=None, pluginId: str = "") -> None:
        """运行时追加按钮（插件 API 入口）：group 分类页不存在则自动新建标签页。
        action 为 None 时点击发 commandRequested(key)（执行同名命令/动作），
        否则直接调用 action 回调。pluginId 记录归属（插件停用时按它移除）。"""
        group = group or "插件"
        flow = self._groupFlows.get(group)
        if flow is None:
            page = QWidget()
            flow = FlowLayout(page, margin=1, spacing=3)
            self._groupFlows[group] = flow
            self._navBar.addTab(group)
            self._toolStack.addWidget(page)
        btn = QPushButton(label)
        btn.setFixedSize(48, 48)
        btn.setToolTip(tooltip or label)
        if icon:
            from PySide6.QtGui import QIcon
            btn.setIcon(QIcon(icon))
            btn.setIconSize(QSize(44, 44))
            btn.setText("")
        elif len(label) == 4 and "\n" not in label:
            # 4 字标签分两行显示（同内置按钮"XX\nXX"样式），否则 48px 放不下
            btn.setText(label[:2] + "\n" + label[2:])
            btn.setStyleSheet("QPushButton { font-size:11px; padding:1px; }")
        if action is not None:
            btn.clicked.connect(lambda _=False, cb=action: cb())
        else:
            btn._cmdKey = key            # 命令类按钮：按模式门禁置灰（setCanvasMode）
            btn.clicked.connect(lambda _=False, k=key: self.commandRequested.emit(k))
        flow.addWidget(btn)
        self._pluginButtons[key] = (btn, pluginId)
        # 插件启用/重载时新按钮立即套用当前模式门禁（否则要等下次切模式）
        cur = getattr(self, "_canvasMode", None)
        if cur and action is None:
            from ui.commands.mode_policy import modeAllows
            if not modeAllows(key, cur):
                btn.setEnabled(False)

    def removeToolButtons(self, keys) -> int:
        """移除指定 key 的插件按钮（插件停用时调用），返回移除数。"""
        removed = 0
        for key in keys:
            entry = self._pluginButtons.pop(key, None)
            if entry is None:
                continue
            btn, _pid = entry
            btn.parentWidget().layout().removeWidget(btn)
            btn.hide()
            btn.deleteLater()
            removed += 1
        return removed

    def refreshStyle(self) -> None:
        """按全局字体大小等比放大导航标签字体与宽度（设置改字体时调用）。"""
        size = theme.FONT_SIZE + 3
        hpad = 28 + (theme.FONT_SIZE - 10) * 2
        self._navBar.setStyleSheet(
            f"""
            QTabBar::tab {{
                background: #e0e0e0;
                padding: 0px {hpad}px;
                border: 1px solid {theme.BORDER};
                margin: 0;
                font-size: {size}pt;
            }}
            QTabBar::tab:selected {{
                background: {theme.PRIMARY};
                color: {theme.TEXT_ON_PRIMARY};
                border: none;
                font-weight: bold;
            }}
            QTabBar::tab:hover {{
                background: #d8d8d8;
            }}
            QTabBar::tab:selected:hover {{
                background: {theme.PRIMARY_DARK};
            }}
            """
        )
        if getattr(self, "_quickBar", None) is not None:
            self._quickBar.refreshStyle()

    def _makeButton(self, key: str, text: str, category: str = "") -> QPushButton:
        btn = QPushButton(tr(text))
        btn._cmdKey = key
        # 正方形大按钮，预留空间给后续替换为正方形图标
        btn.setFixedSize(48, 48)
        # 极简悬停规则：
        #   - TOOLTIP_OVERRIDES 里显式定义的（通常是左右键两行提示）才显示；
        #   - 普通按钮一律不写 tooltip → 鼠标悬停无提示，避免信息噪声。
        tip = TOOLTIP_OVERRIDES.get(key, "")
        if tip:
            btn.setToolTip(tr(tip))
        hasIcon = False
        iconPath = ICON_MAP.get(key)
        if iconPath is not None:
            btn.setIcon(QIcon(iconPath))
            btn.setIconSize(QSize(44, 44))
            btn.setText("")
            hasIcon = True
        elif key in TEXT_ICON_KEYS:
            btn.setIcon(makeTextIcon(TEXT_ICON_KEYS[key], 64))
            btn.setIconSize(QSize(44, 44))
            btn.setText("")
            hasIcon = True
        # 无图标按钮：按语言和文本长度设字号（英文缩小显示全）
        if not hasIcon:
            self._applyButtonTextStyle(btn, text, category)
        btn.clicked.connect(lambda _, k=key: self.commandRequested.emit(k))
        if key == "Group":
            # 群组按钮：右键直接取消群组
            btn.setContextMenuPolicy(Qt.CustomContextMenu)
            btn.customContextMenuRequested.connect(
                lambda _pos: self.commandRequested.emit("Ungroup"))
        if key == "Smooth":
            # 磨光按钮：右键=路径磨光（沿曲线磨光，复用磨光笔刷参数）
            btn.setContextMenuPolicy(Qt.CustomContextMenu)
            btn.customContextMenuRequested.connect(
                lambda _pos: self.commandRequested.emit("SweepSmooth"))
        # 记录中文文本供 retranslateUi（图标按钮 text 为空也记，重译时跳过）
        self._buttonTexts[btn] = text
        self._buttonCategory[btn] = category
        self._buttonHasIcon[btn] = hasIcon
        return btn

    def _applyButtonTextStyle(self, btn, text: str, category: str) -> None:
        """无图标按钮字号：中文按原逻辑，英文按长度缩小显示全。"""
        from i18n import getLanguage
        is_en = getLanguage() == "English"
        large = category in ("浮雕", "浮雕几何")
        if is_en:
            en = tr(text)
            if "\n" in en:
                lines = en.split("\n")
                mx = max(len(l) for l in lines)
                fs = 11 if mx <= 5 else 9
            else:
                n = len(en)
                fs = 13 if n <= 5 else (10 if n <= 7 else 8)
            btn.setStyleSheet(f"QPushButton {{ font-size:{fs}px; padding:1px; }}")
        else:
            if "\n" in text:
                fs = 16 if large else 11
                btn.setStyleSheet(f"QPushButton {{ font-size:{fs}px; padding:1px; }}")
            elif large:
                btn.setStyleSheet("QPushButton { font-size:20px; padding:2px; }")

    def setCurrentCategory(self, name: str) -> None:
        """按分类名切换顶部类目标签页（外部联动入口）。

        name 须与 CATEGORIES 里的中文分类名一致，如 "曲线绘制"/"浮雕"/"3D"/"CNC"。
        找不到则忽略，不抛异常。
        """
        for i, n in enumerate(self._tabNames):
            if n == name:
                if self._navBar.currentIndex() != i:
                    self._navBar.setCurrentIndex(i)
                return

    def setCanvasMode(self, mode: str) -> None:
        """按画布模式门禁置灰命令按钮（模式策略见 ui/commands/mode_policy.py）：
        2D 模式禁 3D 实体/浮雕雕刻/CNC 刀路命令，浮雕模式禁 3D/CNC，依矩阵类推。
        未列入矩阵的命令（绘制/编辑）保持可用；带自定义 action 的插件按钮
        （纯回调，非命令）不参与门禁。"""
        from ui.commands.mode_policy import modeAllows
        self._canvasMode = mode   # 插件按钮运行时追加（addToolButton）要用
        for btn in self._buttonTexts:
            key = getattr(btn, "_cmdKey", None)
            if not key:
                continue
            allowed = modeAllows(key, mode)
            if btn.isEnabled() != allowed:
                btn.setEnabled(allowed)
        for btn, _pid in self._pluginButtons.values():
            key = getattr(btn, "_cmdKey", None)
            if not key:
                continue   # 自定义 action 的插件按钮
            allowed = modeAllows(key, mode)
            if btn.isEnabled() != allowed:
                btn.setEnabled(allowed)

    def quickBar(self) -> QuickAccessBar:
        return self._quickBar

    def setPrompt(self, text: str) -> None:
        """命令行已移除，保留空实现以兼容旧调用。"""
        pass

    def appendHistory(self, text: str) -> None:
        """命令行已移除：消息转发给 setHistoryHandler 注册的可见通道
        （主窗口接 StepHint），此前 41 处调用全部被静默丢弃。"""
        h = getattr(self, "_historyHandler", None)
        if h is not None:
            try:
                h(str(text))
            except Exception:
                pass

    def setHistoryHandler(self, handler) -> None:
        """注册 appendHistory 的转发目标（None 恢复静默）。"""
        self._historyHandler = handler

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


# 绘图命令图标映射（key -> icons/<name>.svg）；无映射的命令仍显示文字
ICON_MAP = {
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
    "ImageTrace": "icons/imagetrace.svg",
}
# 用 QPainter 绘制中文的命令（Qt SVG 渲染器对中文字体支持弱）
TEXT_ICON_KEYS = {"Text": "字"}

# 按钮提示文字覆盖（按钮文字过短时用完整名称）
TOOLTIP_OVERRIDES = {
    "ImageTrace": "图片转线稿：识别图片中的线稿生成曲线（单线 / 轮廓）",
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
        ("ClosePath", "闭合"), ("Outline", "外轮廓"), ("Align", "对齐"),
    ]),
    ("浮雕", [
        ("BitmapRelief", "位图浮雕"), ("RegionRelief", "区域浮雕"), ("Brush", "笔刷"),
        ("Smooth", "磨光"), ("Stamp", "冲压"), ("Blend", "混合"),
        ("Flatten", "抹平"), ("ExportHeight", "导出灰度"),
    ]),
    ("视图", [
        ("ZoomFit", "重置视图"), ("ZoomIn", "放大"), ("ZoomOut", "缩小"),
        ("ZoomWindow", "窗口缩放"), ("Pan", "平移"), ("Redraw", "重画"),
        ("StandardView", "标准视角"), ("DualView", "双视窗"),
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
        for name, items in CATEGORIES:
            self._navBar.addTab(name)
            self._toolStack.addWidget(self._buildPage(items, self._groupFlows))
        bl.addWidget(self._toolStack)

        self._quickBar = QuickAccessBar()
        bl.addWidget(self._quickBar)
        outer.addWidget(bottom)

        self._navBar.currentChanged.connect(self._toolStack.setCurrentIndex)
        self._quickBar.commandRequested.connect(self.commandRequested.emit)

        self.setFixedHeight(bottom.sizeHint().height())

    def _buildPage(self, items, flows: dict | None = None) -> QWidget:
        page = QWidget()
        flow = FlowLayout(page, margin=1, spacing=3)
        for key, text in items:
            flow.addWidget(self._makeButton(key, text))
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
        if action is not None:
            btn.clicked.connect(lambda _=False, cb=action: cb())
        else:
            btn.clicked.connect(lambda _=False, k=key: self.commandRequested.emit(k))
        flow.addWidget(btn)
        self._pluginButtons[key] = (btn, pluginId)

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

    def _makeButton(self, key: str, text: str) -> QPushButton:
        btn = QPushButton(text)
        # 正方形大按钮，预留空间给后续替换为正方形图标
        btn.setFixedSize(48, 48)
        btn.setToolTip(TOOLTIP_OVERRIDES.get(
            key, text + "（右键取消群组）" if key == "Group" else text))
        iconPath = ICON_MAP.get(key)
        if iconPath is not None:
            btn.setIcon(QIcon(iconPath))
            btn.setIconSize(QSize(44, 44))
            btn.setText("")
        elif key in TEXT_ICON_KEYS:
            btn.setIcon(makeTextIcon(TEXT_ICON_KEYS[key], 64))
            btn.setIconSize(QSize(44, 44))
            btn.setText("")
        btn.clicked.connect(lambda _, k=key: self.commandRequested.emit(k))
        if key == "Group":
            # 群组按钮：右键直接取消群组
            btn.setContextMenuPolicy(Qt.CustomContextMenu)
            btn.customContextMenuRequested.connect(
                lambda _pos: self.commandRequested.emit("Ungroup"))
        return btn

    def quickBar(self) -> QuickAccessBar:
        return self._quickBar

    def setPrompt(self, text: str) -> None:
        """命令行已移除，保留空实现以兼容旧调用。"""
        pass

    def appendHistory(self, text: str) -> None:
        """命令行已移除，保留空实现以兼容旧调用。"""
        pass

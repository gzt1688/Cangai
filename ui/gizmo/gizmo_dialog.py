"""操作轴精确输入小组件：无标题栏的小型浮层（单击操作轴的轴/圆弧/手柄时弹出）。

样式与交互：
- 无边框 / 无标题栏的紧凑 Tool 小窗：移动/旋转主行布局为
  「标签 | (±按钮+数值输入框，间距 0 贴合为一个整体，较高) | (复制按钮+数量输入框，
  垂直两格，两格总高与左侧输入组一致)」；缩放模式再多两行“数值含义/作用范围”选项
  （选项只有两三个，做成等宽等长的分段按钮组，不用下拉、弹层不会被截断，且两行左右齐平）；
  没有确定/取消按钮；
- 弹出位置在鼠标右下方（靠近屏幕边缘时自动翻转到鼠标另一侧，避免超出屏幕）；
- 确认/取消：输入框内按 Enter = 确认；Esc = 取消；单击弹框外空白 = 取消；
  在场景中单击右键 = 确认。外部点击会被本弹框消费，不落到视口。
- 复制数量：勾选“复制”后输入数量 N，确认时按输入值连续克隆 N 份
  （每份相对上一份偏移/旋转一次该输入值），实现“距离 1、复制 5”这类连续复制。
- 连续复制方式（仅移动模式，左缘标题“距离/间距”本身就是切换钮，点击标题切换）：
  距离 = 每份相对前一份平移一个输入值（默认）；
  间距 = 相邻两份的净间距为输入值，即步距 = 输入值 + 物体在该轴上的尺寸。
- 数值记忆：每次确认后记住该弹框的输入值（含正负号），下次打开自动带回；
  复制数量不记忆，始终默认 1。
"""
from __future__ import annotations
from math import lcm

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QFont, QGuiApplication, QMouseEvent
from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QButtonGroup,
                               QDialog, QDoubleSpinBox, QHBoxLayout, QLabel,
                               QPushButton, QSpinBox, QVBoxLayout)

# 左侧输入组（±按钮 + 数值框）的整体高度；右侧复制/数量两格之和与之相等
_GROUP_H = 46
_SIGN_W = 22
_VALUE_W = 72
_COPY_W = 44
_SUB_H = 22          # 复制按钮 / 数量框各自高度（两格 + 2px 间距 = _GROUP_H）

_QSS = f"""
GizmoInputDialog#gizmoInput {{
    background: #ffffff;
    border: 1px solid #c3cad6;
    border-radius: 8px;
}}
GizmoInputDialog#gizmoInput QLabel {{
    font-size: 14px;
    color: #55606f;
    background: transparent;
}}
GizmoInputDialog#gizmoInput QDoubleSpinBox,
GizmoInputDialog#gizmoInput QSpinBox {{
    background: #ffffff;
    border: 1px solid #c3cad6;
    border-radius: 5px;
    font-size: 15px;
    padding: 0 4px;
}}
GizmoInputDialog#gizmoInput QDoubleSpinBox:focus,
GizmoInputDialog#gizmoInput QSpinBox:focus {{
    border: 1px solid #15b6c9;
}}
GizmoInputDialog#gizmoInput QDoubleSpinBox:disabled,
GizmoInputDialog#gizmoInput QSpinBox:disabled {{
    color: #9aa5b1;
    background: #f8fafc;
    border-color: #e2e8f0;
}}
/* 主数值框：左缘与 ± 按钮贴合（去掉左圆角与左边框），字更大 */
GizmoInputDialog#gizmoInput QDoubleSpinBox#valueSpin {{
    border-left: none;
    border-radius: 0 5px 5px 0;
    padding-left: 6px;
    font-size: 19px;
    font-weight: 600;
}}
GizmoInputDialog#gizmoInput QDoubleSpinBox#valueSpin:focus {{
    border-left: none;
}}
GizmoInputDialog#gizmoInput QPushButton#copyBtn {{
    background: #f1f5f9;
    color: #334155;
    border: 1px solid #c3cad6;
    border-radius: 5px;
    font-size: 14px;
    padding: 0;
}}
GizmoInputDialog#gizmoInput QPushButton#copyBtn:hover {{
    background: #e2e8f0;
}}
GizmoInputDialog#gizmoInput QPushButton#copyBtn:checked {{
    background: #15b6c9;
    color: #ffffff;
    border-color: #15b6c9;
}}
/* ± 按钮：只保留左缘圆角，右边与输入框无缝贴合 */
GizmoInputDialog#gizmoInput QPushButton#signBtn {{
    background: #f1f5f9;
    color: #1e293b;
    border: 1px solid #c3cad6;
    border-right: none;
    border-radius: 6px 0 0 6px;
    font-size: 21px;
    font-weight: 700;
    padding: 0;
}}
GizmoInputDialog#gizmoInput QPushButton#signBtn:hover {{
    background: #e2e8f0;
    border-color: #15b6c9;
    color: #15b6c9;
}}
GizmoInputDialog#gizmoInput QPushButton#signBtn:checked {{
    background: #15b6c9;
    color: #ffffff;
    border-color: #15b6c9;
}}
/* 左缘标题钮（移动模式“距离/间距”切换）：外观同普通标题文字，可点按切换 */
GizmoInputDialog#gizmoInput QPushButton#titleBtn {{
    background: transparent;
    color: #55606f;
    border: none;
    border-radius: 5px;
    font-size: 14px;
    padding: 0;
}}
GizmoInputDialog#gizmoInput QPushButton#titleBtn:hover {{
    background: #e8f6f8;
    color: #15b6c9;
}}
GizmoInputDialog#gizmoInput QPushButton#titleBtn:checked {{
    color: #15b6c9;
    font-weight: 700;
}}
/* 缩放选项：分段按钮组（等宽等长、左右边缘齐平，不用下拉） */
GizmoInputDialog#gizmoInput QPushButton#optBtn {{
    background: #f1f5f9;
    color: #334155;
    border: 1px solid #c3cad6;
    border-left: none;
    border-radius: 0;
    padding: 0;
}}
GizmoInputDialog#gizmoInput QPushButton#optBtn[seg="first"] {{
    border-left: 1px solid #c3cad6;
    border-top-left-radius: 6px;
    border-bottom-left-radius: 6px;
}}
GizmoInputDialog#gizmoInput QPushButton#optBtn[seg="last"] {{
    border-top-right-radius: 6px;
    border-bottom-right-radius: 6px;
}}
GizmoInputDialog#gizmoInput QPushButton#optBtn:hover {{
    background: #e2e8f0;
}}
GizmoInputDialog#gizmoInput QPushButton#optBtn:checked {{
    background: #15b6c9;
    color: #ffffff;
    border-color: #15b6c9;
}}
"""


def _optionRows(parent, itemsList: list[list[str]]):
    """把少量选项平铺成互斥分段按钮组，替代下拉菜单（选项完整可见）。

    各行按钮数量可不同（3 个 / 2 个），但整行总宽相同、单元格等宽，
    因此两行左右边缘严格对齐；行高固定 24，间距 0 呈连续分段条。
    """
    font = QFont(parent.font())
    font.setPixelSize(13)          # 按钮统一字号
    pad = 12                       # 每个单元格文字左右余量合计

    rows = []
    for items in itemsList:
        rowLayout = QHBoxLayout()
        rowLayout.setContentsMargins(0, 0, 0, 0)
        rowLayout.setSpacing(0)
        group = QButtonGroup(parent)
        group.setExclusive(True)
        buttons = []
        for idx, text in enumerate(items):
            btn = QPushButton(text)
            btn.setObjectName("optBtn")
            btn.setCheckable(True)
            btn.setFont(font)
            btn.setCursor(Qt.PointingHandCursor)
            # 用按钮自己的字体度量（与实际渲染一致）算文字宽度
            need = btn.fontMetrics().horizontalAdvance(text) + pad
            btn.setProperty("seg", "first" if idx == 0 else
                            ("last" if idx == len(items) - 1 else "mid"))
            group.addButton(btn, idx)
            rowLayout.addWidget(btn)
            buttons.append((btn, need))
        group.button(0).setChecked(True)
        rows.append((rowLayout, group, buttons))

    # 两行总宽取同一值：每行按“最宽文字 × 格数”算需求，再取各格数的最小公倍数
    # 向上取整，保证两行都能整除、按钮等宽且左右边缘齐平
    total = max(len(row[2]) * max(n for _b, n in row[2]) for row in rows)
    step = lcm(*(len(row[2]) for row in rows))
    total = max(step, ((total + step - 1) // step) * step)
    for rowLayout, _group, buttons in rows:
        cell = total // len(buttons)
        for btn, _n in buttons:
            btn.setFixedSize(cell, 24)
    return [(layout, group) for layout, group, _b in rows], total


class GizmoInputDialog(QDialog):
    """单击操作轴时弹出的紧凑数值输入小浮层。

    无确定/取消按钮。确认 = 回车 / 场景中右键单击；取消 = Esc / 单击弹框外空白。
    打开期间监听应用级鼠标按下：点在弹框外则消费该事件并据此结束对话框，
    保证弹框消失前那次点击不会被视口当作框选/旋转等误操作。
    """

    # 记住各弹框最近一次确认的输入值（含正负号），进程内跨多次打开有效
    _remembered: dict[str, float] = {}

    def __init__(self, title: str, label: str, initial: float,
                 minV: float, maxV: float, suffix: str = "",
                 decimals: int = 3, scaleModes: bool = False,
                 spacingTitle: bool = False, persistKey: str = "",
                 parent=None) -> None:
        """spacingTitle=True（移动模式）时，左缘标题“距离”可点按切换为“间距”。

        persistKey 非空时，确认后记住输入值并在下次打开时带回（复制数量不做记忆）。
        """
        super().__init__(parent, Qt.FramelessWindowHint
                         | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setObjectName("gizmoInput")
        self.setStyleSheet(_QSS)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self._modeGroup = None
        self._axisGroup = None
        self._titleBtn = None
        self._persistKey = persistKey
        # 上次确认的数值（含正负）；没有记忆时用默认 initial
        last = (GizmoInputDialog._remembered.get(persistKey)
                if persistKey else None)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        # 缩放选项行先建好：它决定弹框内容宽度，主行数值框据此加宽、贴紧复制列
        optRows = None
        optTotal = 0
        if scaleModes:
            optRows, optTotal = _optionRows(self, [["绝对尺寸", "增量", "缩放比例"],
                                                   ["单轴", "所有轴"]])

        # ---- 主行：标签 | 输入组(±+数值) | 复制/数量 ----
        row = QHBoxLayout()
        row.setSpacing(6)

        # 左缘标题文字竖排（“距离”等两字排成两行），贴合高挑输入组。
        # 移动模式：标题本身就是“距离/间距”切换钮，点击在两种连续复制步距含义间切换
        labelW = 0
        if spacingTitle:
            self._titleBtn = QPushButton("\n".join(label))
            self._titleBtn.setObjectName("titleBtn")
            self._titleBtn.setCheckable(True)
            self._titleBtn.setFixedWidth(_SIGN_W)
            self._titleBtn.setCursor(Qt.PointingHandCursor)
            self._titleBtn.setToolTip("点按标题切换连续复制方式（勾选“复制”时生效）：\n"
                                      "距离：相邻两份间隔一个输入值；\n"
                                      "间距：相邻两份净间距 = 输入值\n"
                                      "      （步距 = 输入值 + 物体在该轴尺寸）")
            self._titleBtn.toggled.connect(self._updateTitleText)
            row.addWidget(self._titleBtn, 0, Qt.AlignVCenter)
            labelW = _SIGN_W
        else:
            lab = QLabel("\n".join(label))
            lab.setAlignment(Qt.AlignCenter)
            labelW = lab.sizeHint().width()
            if scaleModes:
                lab.setFixedWidth(labelW)   # 宽度固定，剩余宽度全部给数值框
            row.addWidget(lab, 0, Qt.AlignVCenter)

        # 左侧输入组：± 按钮与数值输入框间距 0、同高，视觉上贴合为一个整体
        left = QHBoxLayout()
        left.setSpacing(0)
        self._signBtn = QPushButton("+")
        self._signBtn.setObjectName("signBtn")
        self._signBtn.setCheckable(True)
        self._signBtn.setFixedSize(_SIGN_W, _GROUP_H)
        self._signBtn.setCursor(Qt.PointingHandCursor)
        self._signBtn.setToolTip("点击切换正 / 负方向")
        self._signBtn.toggled.connect(self._updateSignText)
        if last is not None and last < 0:
            self._signBtn.setChecked(True)   # 记住的是负值则恢复“-”状态
        left.addWidget(self._signBtn)

        self._spin = QDoubleSpinBox()
        self._spin.setObjectName("valueSpin")
        self._spin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self._spin.setRange(minV, maxV)
        self._spin.setDecimals(decimals)
        self._spin.setValue(abs(last) if last is not None else initial)
        if suffix:
            self._spin.setSuffix(suffix)
        if scaleModes:
            # 内容宽度由下方选项行决定：数值框吃掉标签/±/复制列之外的全部宽度，
            # 与复制按钮之间只留行内 6px 间距
            spinW = optTotal - labelW - _SIGN_W - _COPY_W - 12
            self._spin.setFixedSize(max(_VALUE_W, spinW), _GROUP_H)
        else:
            self._spin.setFixedSize(_VALUE_W, _GROUP_H)
        left.addWidget(self._spin)
        row.addLayout(left)

        # 右侧列：复制按钮（上）+ 复制数量（下），两格高度之和 = 左侧输入组高度
        right = QVBoxLayout()
        right.setSpacing(_GROUP_H - 2 * _SUB_H)   # = 2px
        self._copyBtn = QPushButton("复制")
        self._copyBtn.setObjectName("copyBtn")
        self._copyBtn.setCheckable(True)
        self._copyBtn.setFixedSize(_COPY_W, _SUB_H)
        self._copyBtn.setCursor(Qt.PointingHandCursor)
        self._copyBtn.setToolTip("勾选后，确认时先复制一份再应用变换；"
                                 "取消勾选则只变换原对象")
        self._countSpin = QSpinBox()
        self._countSpin.setButtonSymbols(QAbstractSpinBox.NoButtons)
        self._countSpin.setRange(1, 999)
        self._countSpin.setValue(1)
        self._countSpin.setAlignment(Qt.AlignCenter)
        self._countSpin.setFixedSize(_COPY_W, _SUB_H)
        self._countSpin.setToolTip("连续复制份数：\n"
                                   "勾选“复制”后，按当前值（含正负方向）\n"
                                   "连续复制 N 份，每份相对前一份偏移/旋转一次")
        right.addWidget(self._copyBtn)
        right.addWidget(self._countSpin)
        # 复制列贴右缘：与下方选项行右边缘对齐（窄弹框下与原先视觉一致）
        row.addStretch()
        row.addLayout(right)

        layout.addLayout(row)

        # ---- 缩放附加行：数值含义 / 作用范围（分段按钮，等宽等长、边缘齐平）----
        if optRows:
            self._modeGroup = optRows[0][1]
            self._axisGroup = optRows[1][1]
            for rowLayout, _group in optRows:
                layout.addLayout(rowLayout)

        # 输入框内回车即确认
        self._spin.lineEdit().returnPressed.connect(self.accept)

    # ---------------- 交互 ----------------

    def accept(self) -> None:
        """确认时记住本次输入值（含正负），供下次同 key 弹框带回。"""
        if self._persistKey:
            GizmoInputDialog._remembered[self._persistKey] = self.value()
        super().accept()

    def _updateSignText(self, neg: bool) -> None:
        self._signBtn.setText("-" if neg else "+")

    def _updateTitleText(self, on: bool) -> None:
        """True=间距 / False=距离，标题文字在“距离/间距”间切换。"""
        self._titleBtn.setText("间\n距" if on else "距\n离")

    def showEvent(self, event) -> None:
        super().showEvent(event)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self._spin.setFocus(Qt.ActiveWindowFocusReason)
        self._spin.selectAll()

    def hideEvent(self, event) -> None:
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        super().hideEvent(event)

    def eventFilter(self, obj, event) -> bool:
        """点在弹框外的鼠标按下：左键=取消，右键=确认，并消费事件。"""
        if (event.type() == QEvent.MouseButtonPress
                and isinstance(event, QMouseEvent)
                and not self._hitSelf(event)):
            if event.button() == Qt.LeftButton:
                self.reject()
            elif event.button() == Qt.RightButton:
                self.accept()
            else:
                return super().eventFilter(obj, event)
            return True
        return super().eventFilter(obj, event)

    def _hitSelf(self, event) -> bool:
        """判断该鼠标事件是否落在本浮层（或其子控件）内。"""
        w = QApplication.widgetAt(event.globalPosition().toPoint())
        while w is not None:
            if w is self:
                return True
            w = w.parentWidget()
        return False

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.accept()
            return
        if event.key() == Qt.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)

    def showAt(self, globalPos: QPoint) -> None:
        """弹出到鼠标右下方；靠近屏幕右/下边缘时翻转到鼠标另一侧。"""
        self.adjustSize()
        # 收紧宽度：主行各控件都是固定宽度，但样式表会放大 QSpinBox 等控件的
        # sizeHint，Qt 计算出的浮层宽度因此比内容实际所需宽出一截，右侧留白。
        # 这里直接压到布局真实需要的宽度（尾随 stretch 不占宽）。
        self.layout().activate()
        need = self.layout().sizeHint().width()
        if need > 0 and need < self.width():
            self.setFixedWidth(need)
        w = self.width()
        h = self.height()
        x = globalPos.x() + 14
        y = globalPos.y() + 10
        screen = QGuiApplication.screenAt(QPoint(int(globalPos.x()),
                                                 int(globalPos.y())))
        if screen is not None:
            av = screen.availableGeometry()
            if x + w > av.right():
                x = globalPos.x() - w - 14
            if y + h > av.bottom():
                y = globalPos.y() - h - 10
            x = max(av.left(), min(x, av.right() - w))
            y = max(av.top(), min(y, av.bottom() - h))
        self.move(int(x), int(y))

    # ---------------- 结果读取 ----------------

    def value(self) -> float:
        """带正负号的实际输入值（点中“-”按钮时为负值）。"""
        v = self._spin.value()
        return -v if self._signBtn.isChecked() else v

    def copyRequested(self) -> bool:
        return self._copyBtn.isChecked()

    def copyCount(self) -> int:
        """连续复制份数（复制未勾选时恒为 1，不产生额外副本）。"""
        return self._countSpin.value()

    def spacingMode(self) -> bool:
        """True=间距（净间距=输入值）；False=距离（步距=输入值，默认）。"""
        return bool(self._titleBtn and self._titleBtn.isChecked())

    def scaleMode(self) -> int:
        """0=绝对尺寸 1=增量 2=缩放比例（非缩放对话框返回 0）。"""
        return self._modeGroup.checkedId() if self._modeGroup else 0

    def uniformAxis(self) -> bool:
        """True=所有轴（等比）。"""
        return bool(self._axisGroup and self._axisGroup.checkedId() == 1)

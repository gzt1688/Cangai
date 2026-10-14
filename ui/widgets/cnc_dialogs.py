"""CNC 前期工作对话框：机床设置 / 安装几何体 / 刀具表。

三个 QDialog 都从 CncState 读数据，确认后写回 CncState 并 notifyChanged。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QRectF, QPointF
from PySide6.QtGui import QPainter, QBrush, QColor, QPolygonF, QPainterPath
from PySide6.QtWidgets import (QDialog, QApplication, QVBoxLayout, QHBoxLayout, QFormLayout,
                                QDoubleSpinBox, QSpinBox, QLineEdit, QComboBox,
                                QPushButton, QLabel, QGroupBox, QListWidget,
                                QListWidgetItem, QMessageBox, QTabWidget,
                                QWidget, QInputDialog, QRadioButton,
                                QButtonGroup, QStackedWidget, QCheckBox,
                                QGridLayout, QFrame, QTableWidget,
                                QTableWidgetItem, QHeaderView,
                                QPlainTextEdit, QProgressBar)

import ui.styles.theme as theme
from core.cnc.state import (CncState, ClampPlate, SideClamp, FixtureSetup,
                            ToolpathEntry,
                            STEP_MACHINE, STEP_STOCK, STEP_TOOLS, MATERIALS)


# ============================================================ 机床设置

class _ToolProgressWidget(QWidget):
    """刀具走位指示：一把铣刀沿路径线随进度移动（美化进度显示）。

    - 底部一条水平"路径线"：已走过部分用主题青色高亮
    - 铣刀（夹头+刀柄+刀刃+尖端）悬在路径线上方，x 随进度移动
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(46)
        self.setMinimumWidth(240)
        self._frac = 0.0

    def setFraction(self, frac: float) -> None:
        self._frac = max(0.0, min(1.0, float(frac)))
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        line_y = int(self.height() * 0.68)
        # 未走路径线（浅灰）
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#e2e2e2"))
        p.drawRoundedRect(4, line_y - 2, w - 8, 4, 2, 2)
        # 已走路径（主题青）
        done = int((w - 8) * self._frac)
        if done > 3:
            p.setBrush(QColor(theme.PRIMARY))
            p.drawRoundedRect(4, line_y - 2, done, 4, 2, 2)
        # 起点标记
        p.setBrush(QColor(theme.BORDER))
        p.drawEllipse(4, line_y - 3, 6, 6)
        # 刀具 x（clamp 在路径内）
        tx = 4 + done
        if self._frac <= 0.0:
            tx = 4
        if self._frac >= 1.0:
            tx = w - 4
        self._drawTool(p, tx, line_y)
        p.end()

    @staticmethod
    def _drawTool(p, x: int, y: int) -> None:
        """铣刀侧视：夹头 → 刀柄 → 颈环 → 刀刃（锥形带刃纹）→ 尖端。"""
        # 底部投影（半透明椭圆）
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 26))
        p.drawEllipse(x - 9, y - 4, 18, 6)
        # 夹头（深灰圆角块 + 内圈线 + 浅色描边）
        p.setPen(QColor("#c8c8c8"))
        p.setBrush(QColor("#4a4a4a"))
        p.drawRoundedRect(x - 4, y - 40, 8, 9, 2, 2)
        p.setPen(QColor("#7a7a7a"))
        p.drawLine(x - 4, y - 36, x + 4, y - 36)
        # 刀柄（亮金属灰窄矩形 + 描边）
        p.setPen(QColor("#c8c8c8"))
        p.setBrush(QColor("#8f8f8f"))
        p.drawRect(x - 2.5, y - 31, 5, 18)
        # 颈环（刀柄与刀刃之间的深色环）
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#4a4a4a"))
        p.drawRect(x - 3.5, y - 14, 7, 3)
        # 刀刃（主题青锥形，加浅色描边提轮廓）
        blade = QPainterPath()
        blade.moveTo(x - 6.5, y - 11)
        blade.lineTo(x + 6.5, y - 11)
        blade.lineTo(x + 2.5, y - 4)
        blade.lineTo(x - 2.5, y - 4)
        blade.closeSubpath()
        p.setBrush(QColor(theme.PRIMARY))
        p.drawPath(blade)
        p.setPen(QColor("#9fdce6"))
        p.drawPolyline([QPointF(x - 6.5, y - 11), QPointF(x + 6.5, y - 11)])
        # 刀刃纹（两道深青细线，模拟螺旋刃）
        p.setPen(QColor(theme.PRIMARY_DARK))
        p.drawLine(x - 5, y - 9, x + 5, y - 9)
        p.drawLine(x - 4, y - 7, x + 4, y - 7)
        p.setPen(Qt.NoPen)
        # 尖端（深青）
        tip = QPainterPath()
        tip.moveTo(x - 2.5, y - 4)
        tip.lineTo(x + 2.5, y - 4)
        tip.lineTo(x, y - 1)
        tip.closeSubpath()
        p.setBrush(QColor(theme.PRIMARY_DARK))
        p.drawPath(tip)


class _CalcProgressDialog(QDialog):
    """刀路计算进度对话框（定制样式）。

    - 尺寸加大（460 宽），标题 + 副标题 + 圆角进度条（内置百分比）
    - 取消按钮加大（110×38）并带悬停反馈
    - 与 QProgressDialog 兼容的最小接口：canceled 信号 / setValue / close
    """

    canceled = Signal()

    def __init__(self, label: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("正在计算刀路")
        self.setWindowModality(Qt.WindowModal)
        self.setFixedSize(460, 214)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 14)
        lay.setSpacing(10)

        # 唯一标题：工序说明（如"分层区域粗加工：逐层填充中…"）
        self._info = QLabel(label)
        self._info.setWordWrap(True)
        self._info.setStyleSheet(
            "font-size:12pt; font-weight:bold; color:%s;" % theme.TEXT)
        lay.addWidget(self._info)

        # 刀具走位指示（随进度移动）
        self._toolInd = _ToolProgressWidget()
        lay.addWidget(self._toolInd)

        self._bar = QProgressBar()
        self._bar.setRange(0, 1000)
        self._bar.setValue(0)
        self._bar.setFixedHeight(22)
        self._bar.setTextVisible(True)
        self._bar.setFormat("%p%")
        self._bar.setStyleSheet(
            "QProgressBar { background:#e8e8e8; border:1px solid %s;"
            " border-radius:7px; text-align:center; font-size:9pt;"
            " color:%s; }"
            "QProgressBar::chunk { background:%s; border-radius:6px; }"
            % (theme.BORDER, theme.TEXT, theme.PRIMARY))
        lay.addWidget(self._bar)

        btnCancel = QPushButton("取消")
        btnCancel.setMinimumSize(110, 38)
        btnCancel.setCursor(Qt.PointingHandCursor)
        btnCancel.setStyleSheet(
            "QPushButton { background:#8a8a8a; color:#ffffff;"
            " border:none; border-radius:6px; font-size:11pt;"
            " font-weight:bold; }"
            "QPushButton:hover { background:#e55a00; }"
            "QPushButton:pressed { background:#c44f00; }")
        btnCancel.clicked.connect(self._onCancel)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(btnCancel)
        lay.addLayout(row)

    def _onCancel(self) -> None:
        self.canceled.emit()
        self.close()

    def setValue(self, v: int) -> None:
        self._bar.setValue(int(v))
        self._toolInd.setFraction(int(v) / 1000.0)


class _AutoHeightStack(QStackedWidget):
    """高度跟随当前页面的 QStackedWidget（用于对话框高度自适应）。"""

    def sizeHint(self):
        w = self.currentWidget()
        if w is not None:
            h = w.sizeHint()
            if h.isValid():
                return h
        return super().sizeHint()

    def minimumSizeHint(self):
        w = self.currentWidget()
        if w is not None:
            h = w.minimumSizeHint()
            if h.isValid():
                return h
        return super().minimumSizeHint()


class CncMachineDialog(QDialog):
    """机床设置：3轴/4轴/5轴单选 + 对应参数页。

    3轴：名称/控制器/行程XYZ/主轴/安全高度/间隙平面
    4轴：3轴参数 + 旋转轴（A/B/C + 行程 + 转速）
    5轴：占位（开发中）
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("机床设置")
        self.setMinimumWidth(380)
        self._state = CncState.instance()

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        # ---- 顶部：轴数单选 ----
        axisRow = QHBoxLayout()
        axisRow.addWidget(self._label("机床类型"))
        self._rb3 = QRadioButton("3轴")
        self._rb4 = QRadioButton("4轴")
        self._rb5 = QRadioButton("5轴")
        self._axisGroup = QButtonGroup(self)
        self._axisGroup.addButton(self._rb3, 0)
        self._axisGroup.addButton(self._rb4, 1)
        self._axisGroup.addButton(self._rb5, 2)
        axisRow.addWidget(self._rb3)
        axisRow.addWidget(self._rb4)
        axisRow.addWidget(self._rb5)
        axisRow.addStretch(1)
        lay.addLayout(axisRow)

        # ---- 参数页堆栈 ----
        self._common3: dict = {}   # 3轴页公共参数控件（首次构建）
        self._common4: dict = {}   # 4轴页公共参数控件（二次构建，控件各自独立）
        self._stack = _AutoHeightStack()
        self._stack.addWidget(self._buildCommonPage())   # 0: 3轴
        self._stack.addWidget(self._build4AxisPage())     # 1: 4轴
        self._stack.addWidget(self._build5AxisPage())     # 2: 5轴
        lay.addWidget(self._stack)

        # ---- 按钮行 ----
        row = QHBoxLayout()
        btnOk = self._primaryBtn("确定")
        btnCancel = self._secondaryBtn("取消")
        btnOk.clicked.connect(self._onOk)
        btnCancel.clicked.connect(self.reject)
        row.addStretch(1)
        row.addWidget(btnCancel)
        row.addWidget(btnOk)
        lay.addLayout(row)

        # ---- 初始化选中 ----
        mt = self._state.machineType
        if mt == "4axis":
            self._rb4.setChecked(True)
            self._stack.setCurrentIndex(1)
        elif mt == "5axis":
            self._rb5.setChecked(True)
            self._stack.setCurrentIndex(2)
        else:
            self._rb3.setChecked(True)
            self._stack.setCurrentIndex(0)
        self._axisGroup.idClicked.connect(self._onAxisChanged)
        self._fitHeight()

    def _onAxisChanged(self, idx: int) -> None:
        self._stack.setCurrentIndex(idx)
        self._fitHeight()

    def _fitHeight(self) -> None:
        """根据当前参数页高度收缩对话框。"""
        self.layout().invalidate()
        self.adjustSize()
        page = self._stack.currentWidget()
        if page is not None:
            pageH = page.sizeHint().height()
            stackH = self._stack.sizeHint().height()
            if stackH > 0 and pageH != stackH:
                delta = stackH - pageH
                self.resize(self.width(), self.height() - delta)

    # ----- 参数页 -----
    def _buildCommonPage(self) -> QWidget:
        """3轴参数页（也是4轴的公共部分）。返回的控件按构建次序登记：
        第一次=3轴页（_common3），第二次=4轴页（_common4），确定时按当前页读取，
        避免"4轴页控件覆盖 3轴页属性导致 3轴页修改丢失"。"""
        w = QWidget()
        form = QFormLayout(w)
        form.setSpacing(8)
        m = self._state.machine

        self._name = QLineEdit(m.name)
        form.addRow("机床名称", self._name)

        from core.cnc.posts import list_posts
        self._controller = QComboBox()
        for pi in list_posts():
            self._controller.addItem(f"{pi.name}（{pi.description}）",
                                     pi.controller_id)
        i = self._controller.findData(m.controller)
        if i >= 0:
            self._controller.setCurrentIndex(i)
        # 连接放在 setCurrentIndex 之后：打开时不误触发宏刷新，
        # 保留 state 中已保存的自定义宏；之后切换控制器才刷新为默认值。
        self._controller.currentIndexChanged.connect(
            self._onControllerChanged, Qt.UniqueConnection)
        form.addRow("控制器", self._controller)

        self._travelX = self._dspin(m.travel.x, 1, 99999)
        self._travelY = self._dspin(m.travel.y, 1, 99999)
        self._travelZ = self._dspin(m.travel.z, 1, 99999)
        form.addRow("行程 X", self._travelX)
        form.addRow("行程 Y", self._travelY)
        form.addRow("行程 Z", self._travelZ)

        self._rpmMin = self._ispin(m.spindle.min_rpm, 100, 99999)
        self._rpmMax = self._ispin(m.spindle.max_rpm, 100, 99999)
        form.addRow("主轴最低转速", self._rpmMin)
        form.addRow("主轴最高转速", self._rpmMax)

        self._safeH = self._dspin(m.defaults.safe_height, 0.1, 999)
        self._clearance = self._dspin(m.defaults.clearance_plane, 0.0, 999)
        form.addRow("安全高度", self._safeH)
        form.addRow("间隙平面", self._clearance)

        # ---- 程序宏（覆盖后置文件默认，留空=回退默认）----
        macrosBox = QGroupBox("程序宏")
        mform = QFormLayout(macrosBox)
        mform.setSpacing(4)
        mform.setContentsMargins(8, 6, 8, 6)
        cur = m.macros or {}
        self._macroStart = QPlainTextEdit()
        self._macroEnd = QPlainTextEdit()
        self._macroChange = QPlainTextEdit()
        for e in (self._macroStart, self._macroEnd, self._macroChange):
            e.setFixedHeight(48)
            e.setStyleSheet(
                "QPlainTextEdit { font-family:Consolas,monospace; font-size:12px; }")
        self._macroStart.setPlainText(cur.get("program_start", ""))
        self._macroEnd.setPlainText(cur.get("program_end", ""))
        self._macroChange.setPlainText(cur.get("tool_change", ""))
        mform.addRow("程序头", self._macroStart)
        mform.addRow("程序尾", self._macroEnd)
        mform.addRow("换刀", self._macroChange)
        self._macroReset = QPushButton("恢复当前后处理默认")
        self._macroReset.setStyleSheet(
            "QPushButton { background:#fff; color:%s; border:1px solid %s;"
            " padding:3px 10px; border-radius:3px; }" % (theme.TEXT, theme.BORDER))
        self._macroReset.clicked.connect(self._onMacroReset, Qt.UniqueConnection)
        mform.addRow("", self._macroReset)
        form.addRow(macrosBox)

        group = {
            "name": self._name, "controller": self._controller,
            "travelX": self._travelX, "travelY": self._travelY,
            "travelZ": self._travelZ, "rpmMin": self._rpmMin,
            "rpmMax": self._rpmMax, "safeH": self._safeH,
            "clearance": self._clearance,
            "macroStart": self._macroStart,
            "macroEnd": self._macroEnd,
            "macroChange": self._macroChange,
        }
        if not self._common3:
            self._common3 = group
        else:
            self._common4 = group
        return w

    def _build4AxisPage(self) -> QWidget:
        """4轴参数页：3轴参数 + 旋转轴参数。"""
        w = QWidget()
        outer = QVBoxLayout(w)
        outer.setSpacing(8)

        outer.addWidget(self._buildCommonPage())

        rotBox = QGroupBox("旋转轴")
        rform = QFormLayout(rotBox)
        rform.setSpacing(8)

        self._rotaryAxis = QComboBox()
        self._rotaryAxis.addItem("A（绕 X 轴）", "A")
        self._rotaryAxis.addItem("B（绕 Y 轴）", "B")
        self._rotaryAxis.addItem("C（绕 Z 轴）", "C")
        i = self._rotaryAxis.findData(self._state.rotaryAxis)
        if i >= 0:
            self._rotaryAxis.setCurrentIndex(i)
        rform.addRow("旋转轴", self._rotaryAxis)

        self._rotaryMin = self._dspin(self._state.rotaryTravelMin, -99999, 99999)
        self._rotaryMax = self._dspin(self._state.rotaryTravelMax, -99999, 99999)
        self._rotaryRpm = self._ispin(self._state.rotaryMaxRpm, 1, 99999)
        rform.addRow("旋转行程最小", self._rotaryMin)
        rform.addRow("旋转行程最大", self._rotaryMax)
        rform.addRow("旋转最大转速", self._rotaryRpm)
        outer.addWidget(rotBox)
        outer.addStretch(1)
        return w

    def _build5AxisPage(self) -> QWidget:
        """5轴参数页：占位。"""
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setAlignment(Qt.AlignCenter)
        ph = self._label("5轴太复杂，开发中…")
        ph.setStyleSheet("color:#999; font-size:14px;")
        lay.addWidget(ph)
        return w

    # ----- 确定 -----
    def _onControllerChanged(self) -> None:
        """切换控制器 → 程序宏刷新为该后处理默认值。"""
        from core.cnc.posts import get_post
        cid = self._controller.currentData()
        post = get_post(cid)
        for edit, attr in ((self._macroStart, "program_start"),
                           (self._macroEnd, "program_end"),
                           (self._macroChange, "tool_change")):
            edit.setPlainText(getattr(post, attr))

    def _onMacroReset(self) -> None:
        """恢复当前后处理的程序宏默认值。"""
        self._onControllerChanged()

    def _onOk(self) -> None:
        idx = self._axisGroup.checkedId()
        # 读取**当前显示页**的公共参数控件（3轴页=common3，4轴页=common4），
        # 修复此前属性被 4轴页二次构建覆盖、3轴页修改丢失的问题。
        common = self._common3 if idx == 0 else self._common4
        m = self._state.machine
        m.name = common["name"].text()
        m.controller = common["controller"].currentData()
        m.travel.x = common["travelX"].value()
        m.travel.y = common["travelY"].value()
        m.travel.z = common["travelZ"].value()
        m.spindle.min_rpm = common["rpmMin"].value()
        m.spindle.max_rpm = common["rpmMax"].value()
        m.defaults.safe_height = common["safeH"].value()
        m.defaults.clearance_plane = common["clearance"].value()
        # 程序宏：留空项由后处理回退类默认
        m.macros = {
            "program_start": common["macroStart"].toPlainText().strip("\n"),
            "program_end": common["macroEnd"].toPlainText().strip("\n"),
            "tool_change": common["macroChange"].toPlainText().strip("\n"),
        }

        if idx == 1:
            self._state.machineType = "4axis"
            self._state.rotaryAxis = self._rotaryAxis.currentData()
            self._state.rotaryTravelMin = self._rotaryMin.value()
            self._state.rotaryTravelMax = self._rotaryMax.value()
            self._state.rotaryMaxRpm = self._rotaryRpm.value()
        elif idx == 2:
            self._state.machineType = "5axis"
        else:
            self._state.machineType = "3axis"

        self._state.notifyChanged()
        self.accept()

    # ----- 控件工厂 -----
    def _label(self, text):
        l = QLabel(text)
        l.setStyleSheet("font-weight:bold;")
        return l

    def _dspin(self, v, lo, hi):
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(2)
        s.setValue(float(v))
        return s

    def _ispin(self, v, lo, hi):
        s = QSpinBox()
        s.setRange(lo, hi)
        s.setValue(int(v))
        return s

    def _primaryBtn(self, text):
        b = QPushButton(text)
        b.setStyleSheet(
            "QPushButton { background:%s; color:%s; border:none;"
            " padding:6px 16px; border-radius:3px; font-weight:bold; }"
            % (theme.PRIMARY, theme.TEXT_ON_PRIMARY))
        return b

    def _secondaryBtn(self, text):
        b = QPushButton(text)
        b.setStyleSheet(
            "QPushButton { background:#fff; color:%s; border:1px solid %s;"
            " padding:6px 16px; border-radius:3px; }" % (theme.TEXT, theme.BORDER))
        return b


# ============================================================ 安装几何体

class CncStockDialog(QDialog):
    """安装几何体：材料毛坯 + 夹具（压板/矩形两边加）。"""

    def __init__(self, parent=None, doc=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("安装几何体")
        self.setMinimumWidth(420)
        self._state = CncState.instance()
        self._doc = doc

        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.addTab(self._buildStockTab(), "材料毛坯")
        tabs.addTab(self._buildFixtureTab(), "夹具")
        lay.addWidget(tabs)

        row = QHBoxLayout()
        btnOk = self._primaryBtn("确定")
        btnCancel = self._secondaryBtn("取消")
        btnOk.clicked.connect(self._onOk)
        btnCancel.clicked.connect(self.reject)
        row.addStretch(1)
        row.addWidget(btnCancel)
        row.addWidget(btnOk)
        lay.addLayout(row)

    def _buildStockTab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        s = self._state.stock
        self._stockW = self._dspin(s.width, 1, 99999)
        self._stockH = self._dspin(s.height, 1, 99999)
        self._stockT = self._dspin(s.thickness, 0.1, 999)
        form.addRow("毛坯宽 X", self._stockW)
        form.addRow("毛坯高 Y", self._stockH)
        form.addRow("毛坯厚 Z", self._stockT)

        # 按导入模型尺寸自动设置毛坯尺寸
        fitBtn = QPushButton("按模型尺寸设置毛坯")
        fitBtn.setStyleSheet(
            "QPushButton { background:%s; color:%s; border:none;"
            " padding:5px 12px; border-radius:3px; }"
            % (theme.PRIMARY, theme.TEXT_ON_PRIMARY))
        fitBtn.setToolTip("按 CNC 空间导入的模型（浮雕模型/实体）包围盒自动填入毛坯尺寸")
        fitBtn.clicked.connect(self._onFitStockToModel)
        form.addRow("", fitBtn)

        # 材料单选框（3列网格）
        matWidget = QWidget()
        matGrid = QGridLayout(matWidget)
        matGrid.setSpacing(4)
        matGrid.setContentsMargins(0, 0, 0, 0)
        self._matGroup = QButtonGroup(matWidget)
        self._matButtons = {}
        for idx, name in enumerate(MATERIALS):
            rb = QRadioButton(name)
            self._matGroup.addButton(rb, idx)
            matGrid.addWidget(rb, idx // 3, idx % 3)
            self._matButtons[name] = rb
        cur = self._state.materialType
        if cur in self._matButtons:
            self._matButtons[cur].setChecked(True)
        else:
            first = next(iter(self._matButtons), None)
            if first:
                self._matButtons[first].setChecked(True)
        form.addRow("材料", matWidget)

        # 硬度
        self._hardness = self._dspin(self._state.materialHardness, 0.1, 999)
        self._hardness.setSuffix(" HB")
        form.addRow("硬度", self._hardness)
        self._matGroup.idClicked.connect(self._onMaterialChanged)

        return w

    def _onFitStockToModel(self) -> None:
        """按 CNC 空间导入模型的包围盒自动设置毛坯宽/高/厚。"""
        from core.cnc.service import model_bounds
        b = model_bounds(self._doc)
        if b is None:
            QMessageBox.information(
                self, "提示",
                "CNC 空间还没有导入模型\n请先在 2D 环境选中模型，点顶部“导入CNC”，"
                "再回到这里按模型尺寸设置毛坯")
            return
        w, h, t = b
        self._stockW.setValue(w)
        self._stockH.setValue(h)
        self._stockT.setValue(t)

    def _onMaterialChanged(self) -> None:
        """材料单选切换：自动更新硬度值。"""
        btn = self._matGroup.checkedButton()
        if btn is None:
            return
        info = MATERIALS.get(btn.text())
        if info:
            self._hardness.setValue(info["hardness"])

    def _buildFixtureTab(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)
        outer.setSpacing(6)

        # 压板列表
        outer.addWidget(QLabel("压板（矩形压板）"))
        self._clampList = QListWidget()
        self._clampList.setMaximumHeight(120)
        outer.addWidget(self._clampList)
        row1 = QHBoxLayout()
        btnAddClamp = self._secondaryBtn("添加压板")
        btnDelClamp = self._secondaryBtn("删除")
        btnAddClamp.clicked.connect(self._addClamp)
        btnDelClamp.clicked.connect(self._delClamp)
        row1.addWidget(btnAddClamp)
        row1.addWidget(btnDelClamp)
        row1.addStretch(1)
        outer.addLayout(row1)

        # 矩形两边加
        outer.addWidget(QLabel("矩形两边加夹块"))
        self._sideList = QListWidget()
        self._sideList.setMaximumHeight(120)
        outer.addWidget(self._sideList)
        row2 = QHBoxLayout()
        btnAddSide = self._secondaryBtn("添加左夹块")
        btnAddSideR = self._secondaryBtn("添加右夹块")
        btnDelSide = self._secondaryBtn("删除")
        btnAddSide.clicked.connect(lambda: self._addSide("left"))
        btnAddSideR.clicked.connect(lambda: self._addSide("right"))
        btnDelSide.clicked.connect(self._delSide)
        row2.addWidget(btnAddSide)
        row2.addWidget(btnAddSideR)
        row2.addWidget(btnDelSide)
        row2.addStretch(1)
        outer.addLayout(row2)

        self._refreshFixtureLists()
        return w

    def _refreshFixtureLists(self) -> None:
        self._clampList.clear()
        for i, c in enumerate(self._state.fixture.clamps):
            self._clampList.addItem(f"压板{i+1}: ({c.x:g},{c.y:g}) {c.width:g}×{c.height:g} Z[{c.z_bottom:g},{c.z_top:g}]")
        self._sideList.clear()
        for i, c in enumerate(self._state.fixture.side_clamps):
            self._sideList.addItem(f"{c.side}夹块{i+1}: ({c.x:g},{c.y:g}) {c.width:g}×{c.height:g} Z[{c.z_bottom:g},{c.z_top:g}]")

    def _addClamp(self) -> None:
        c = ClampPlate()
        self._state.fixture.clamps.append(c)
        self._refreshFixtureLists()

    def _delClamp(self) -> None:
        i = self._clampList.currentRow()
        if 0 <= i < len(self._state.fixture.clamps):
            self._state.fixture.clamps.pop(i)
            self._refreshFixtureLists()

    def _addSide(self, side: str) -> None:
        c = SideClamp(side=side)
        self._state.fixture.side_clamps.append(c)
        self._refreshFixtureLists()

    def _delSide(self) -> None:
        i = self._sideList.currentRow()
        if 0 <= i < len(self._state.fixture.side_clamps):
            self._state.fixture.side_clamps.pop(i)
            self._refreshFixtureLists()

    def _onOk(self) -> None:
        s = self._state.stock
        s.width = self._stockW.value()
        s.height = self._stockH.value()
        s.thickness = self._stockT.value()
        btn = self._matGroup.checkedButton()
        if btn is not None:
            self._state.materialType = btn.text()
            s.material = btn.text()
        self._state.materialHardness = self._hardness.value()

        self._state.notifyChanged()
        self.accept()

    # ----- 控件工厂 -----
    def _dspin(self, v, lo, hi):
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(2)
        s.setValue(float(v))
        return s

    def _primaryBtn(self, text):
        b = QPushButton(text)
        b.setStyleSheet(
            "QPushButton { background:%s; color:%s; border:none;"
            " padding:6px 16px; border-radius:3px; font-weight:bold; }"
            % (theme.PRIMARY, theme.TEXT_ON_PRIMARY))
        return b

    def _secondaryBtn(self, text):
        b = QPushButton(text)
        b.setStyleSheet(
            "QPushButton { background:#fff; color:%s; border:1px solid %s;"
            " padding:6px 16px; border-radius:3px; }" % (theme.TEXT, theme.BORDER))
        return b


# ============================================================ 刀具表

# 6 种刀型：(key, 标题, 横截面颜色)
TOOL_TYPES = [
    ("endmill",       "平底刀",   "#3498db"),
    ("ballnose",      "球头刀",   "#27ae60"),
    ("bullnose",      "牛鼻刀",   "#e67e22"),
    ("taper_endmill", "锥度平底", "#e74c3c"),
    ("taper_ball",    "锥度球头", "#8e44ad"),
    ("taper_bull",    "锥度牛鼻", "#16a085"),
]

# 刀型 → pymillcam ToolShape 映射（锥度三类复用基础形状，用 geometry["taper_angle"] 区分）
_SHAPE_MAP = {
    "endmill":       "endmill",
    "ballnose":      "ballnose",
    "bullnose":      "bullnose",
    "taper_endmill": "endmill",
    "taper_ball":    "ballnose",
    "taper_bull":    "bullnose",
}

# 刀型 key → 中文标题
_TYPE_LABEL = {k: l for k, l, _ in TOOL_TYPES}


def _toolTypeOf(tool) -> str:
    """从 Tool 反推 6 种刀型 key（按 shape + taper_angle）。"""
    from pymillcam.core.tools import ToolShape
    taper = float(tool.geometry.get("taper_angle", 0.0)) > 0
    s = tool.shape
    if s == ToolShape.ENDMILL:
        return "taper_endmill" if taper else "endmill"
    if s == ToolShape.BALLNOSE:
        return "taper_ball" if taper else "ballnose"
    if s == ToolShape.BULLNOSE:
        return "taper_bull" if taper else "bullnose"
    return "endmill"


class ToolIconWidget(QWidget):
    """绘制刀具图标：上段刀柄（灰），下段刀具横截面（按刀型+颜色）。"""

    def __init__(self, toolType: str, color: str, parent=None) -> None:
        super().__init__(parent)
        self._toolType = toolType
        self._color = color
        self.setFixedSize(56, 80)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        h = self.height()
        cx = w / 2.0

        shankColor = QColor("#9a9a9a")
        toolColor = QColor(self._color)
        shankTop = 4.0
        shankBottom = 30.0
        shankWidth = 22.0
        neckBottom = 36.0          # 过渡斜坡底部（柄 → 刃 的短锥段）
        toolW = 12.0               # 刀刃宽度（刀径，窄于柄径）
        # 锥度刀：刀体本身就是锥形，柄（柄宽）之后直接就是锥度，
        # 顶部直接接柄宽、无过渡斜坡；非锥度刀刀刃从刃宽开始。
        toolTop = (shankBottom
                   if self._toolType.startswith("taper_") else neckBottom)
        toolBottom = h - 4.0
        taper = self._toolType.startswith("taper_")
        botW = 8.0 if taper else toolW   # 锥度刀刃底更窄

        p.setPen(Qt.NoPen)

        # 刀柄：固定宽度的直矩形（无斜坡）
        p.setBrush(QBrush(shankColor))
        p.drawRect(QRectF(cx - shankWidth / 2, shankTop, shankWidth, shankBottom - shankTop))

        # 过渡斜坡：仅无锥度刀（柄宽 → 刃宽 小梯形）；锥度刀柄后直接锥度
        if not taper:
            p.setBrush(QBrush(QColor(toolColor).darker(125)))
            p.drawPolygon(QPolygonF([
                QPointF(cx - shankWidth / 2, shankBottom),
                QPointF(cx + shankWidth / 2, shankBottom),
                QPointF(cx + toolW / 2, neckBottom),
                QPointF(cx - toolW / 2, neckBottom),
            ]))

        # 刀刃（锥度刀顶部=柄宽无缝接柄，非锥度刀从刃宽开始）
        p.setBrush(QBrush(toolColor))
        topW = shankWidth if taper else toolW
        isEndmill = self._toolType in ("endmill", "taper_endmill")
        isBall = self._toolType in ("ballnose", "taper_ball")
        isBull = self._toolType in ("bullnose", "taper_bull")
        if isEndmill:
            if taper:
                poly = QPolygonF([
                    QPointF(cx - topW / 2, toolTop),
                    QPointF(cx + topW / 2, toolTop),
                    QPointF(cx + botW / 2, toolBottom),
                    QPointF(cx - botW / 2, toolBottom),
                ])
                p.drawPolygon(poly)
            else:
                p.drawRect(QRectF(cx - topW / 2, toolTop, topW, toolBottom - toolTop))
        elif isBall:
            r = botW / 2
            stemBottom = toolBottom - r
            if taper:
                poly = QPolygonF([
                    QPointF(cx - topW / 2, toolTop),
                    QPointF(cx + topW / 2, toolTop),
                    QPointF(cx + botW / 2, stemBottom),
                    QPointF(cx - botW / 2, stemBottom),
                ])
                p.drawPolygon(poly)
            else:
                p.drawRect(QRectF(cx - topW / 2, toolTop, topW, stemBottom - toolTop))
            p.drawEllipse(QPointF(cx, stemBottom), r, r)
        elif isBull:
            # 牛鼻刀 = 平底刀底部两角倒圆角；整体一个 path 无接缝
            r = 4.0
            stemBottom = toolBottom - r
            tlX = cx - topW / 2
            trX = cx + topW / 2
            blX = cx - botW / 2
            brX = cx + botW / 2
            path = QPainterPath()
            path.moveTo(tlX, toolTop)
            path.lineTo(trX, toolTop)
            path.lineTo(brX, stemBottom)
            path.arcTo(QRectF(brX - 2 * r, stemBottom, 2 * r, 2 * r), 0, -90)
            path.lineTo(blX + r, toolBottom)
            path.arcTo(QRectF(blX, stemBottom, 2 * r, 2 * r), 270, -90)
            path.closeSubpath()
            p.drawPath(path)


class ToolTypeButton(QFrame):
    """刀型按钮：图标 + 标题，可选中，点击发 clicked(key)。"""
    clicked = Signal(str)

    def __init__(self, toolType: str, label: str, color: str, parent=None) -> None:
        super().__init__(parent)
        self._toolType = toolType
        self._label = label
        self._selected = False
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setSpacing(2)
        lay.setContentsMargins(4, 4, 4, 4)
        icon = ToolIconWidget(toolType, color)
        lab = QLabel(label)
        lab.setAlignment(Qt.AlignCenter)
        lab.setStyleSheet("font-weight:bold; font-size:11pt;")
        lay.addWidget(icon, 0, Qt.AlignCenter)
        lay.addWidget(lab)
        self._updateStyle()

    def setSelected(self, s: bool) -> None:
        self._selected = s
        self._updateStyle()

    def _updateStyle(self) -> None:
        if self._selected:
            self.setStyleSheet(
                "ToolTypeButton { border:2px solid %s; border-radius:6px;"
                " background:%s; }" % (theme.PRIMARY, "#e6f7fa"))
        else:
            self.setStyleSheet(
                "ToolTypeButton { border:1px solid %s; border-radius:6px;"
                " background:#fff; }" % theme.BORDER)

    def enterEvent(self, e) -> None:
        if not self._selected:
            self.setStyleSheet(
                "ToolTypeButton { border:1px solid %s; border-radius:6px;"
                " background:#eaeaea; }" % theme.BORDER)
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        self._updateStyle()
        super().leaveEvent(e)

    def mousePressEvent(self, e) -> None:
        self.clicked.emit(self._toolType)


class _ArrowButton(QPushButton):
    """自绘粗箭头按钮（→/←），避免文字箭头显示不清。"""

    def __init__(self, direction: str = "right", parent=None) -> None:
        super().__init__(parent)
        self._direction = direction
        self.setFixedSize(40, 40)
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect()
        # 背景
        if self.isDown():
            bg = QColor(theme.PRIMARY_DARK)
            fg = QColor(theme.TEXT_ON_PRIMARY)
        elif self.underMouse():
            bg = QColor("#d0d0d0")
            fg = QColor(theme.TEXT)
        else:
            bg = QColor("#ffffff")
            fg = QColor(theme.TEXT)
        p.fillRect(r, bg)
        p.setBrush(Qt.NoBrush)
        p.setPen(QColor(theme.BORDER))
        p.drawRoundedRect(r.adjusted(0, 0, -1, -1), 4, 4)
        # 粗箭头
        p.setPen(Qt.NoPen)
        p.setBrush(fg)
        cx, cy = r.width() / 2, r.height() / 2
        sz = 11
        if self._direction == "right":
            poly = QPolygonF([
                QPointF(cx - sz, cy - sz * 0.4),
                QPointF(cx + sz * 0.2, cy - sz * 0.4),
                QPointF(cx + sz * 0.2, cy - sz),
                QPointF(cx + sz, cy),
                QPointF(cx + sz * 0.2, cy + sz),
                QPointF(cx + sz * 0.2, cy + sz * 0.4),
                QPointF(cx - sz, cy + sz * 0.4),
            ])
        else:
            poly = QPolygonF([
                QPointF(cx + sz, cy - sz * 0.4),
                QPointF(cx - sz * 0.2, cy - sz * 0.4),
                QPointF(cx - sz * 0.2, cy - sz),
                QPointF(cx - sz, cy),
                QPointF(cx - sz * 0.2, cy + sz),
                QPointF(cx - sz * 0.2, cy + sz * 0.4),
                QPointF(cx + sz, cy + sz * 0.4),
            ])
        p.drawPolygon(poly)


class CncToolLibraryDialog(QDialog):
    """刀具表：上排 6 种刀型选择 + 下排左右分栏（左刀具列表右参数）。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("刀具表")
        self.setMinimumSize(760, 520)
        self._state = CncState.instance()
        self._currentType = "endmill"
        self._currentTool = None

        lay = QVBoxLayout(self)
        lay.setSpacing(8)
        lay.setContentsMargins(8, 8, 8, 8)

        # ---- 上部：刀型选择行 ----
        typeRow = QHBoxLayout()
        typeRow.setSpacing(6)
        self._typeButtons = {}
        for key, label, color in TOOL_TYPES:
            btn = ToolTypeButton(key, label, color)
            btn.clicked.connect(self._onTypeChanged)
            typeRow.addWidget(btn)
            self._typeButtons[key] = btn
        typeRow.addStretch(1)
        lay.addLayout(typeRow)

        # ---- 下部：左右分栏 ----
        bottom = QHBoxLayout()
        bottom.setSpacing(8)

        # 左：刀具列表（库存 + 当前使用 双列）
        leftBox = QGroupBox("刀具列表")
        llay = QVBoxLayout(leftBox)
        llay.setSpacing(4)
        listRow = QHBoxLayout()
        listRow.setSpacing(4)

        # 库存列
        stockCol = QVBoxLayout()
        stockCol.setSpacing(2)
        stockLab = QLabel("库存刀具")
        stockLab.setStyleSheet("font-weight:bold;")
        stockCol.addWidget(stockLab)
        self._list = QTableWidget(0, 3)
        self._list.setHorizontalHeaderLabels(["名称", "直径", "刃长"])
        self._list.setSelectionBehavior(QTableWidget.SelectRows)
        self._list.setSelectionMode(QTableWidget.SingleSelection)
        self._list.setEditTriggers(QTableWidget.NoEditTriggers)
        self._list.verticalHeader().setVisible(False)
        self._list.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._list.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._list.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self._list.setStyleSheet(
            "QTableWidget { font-size: 10pt; }"
            " QTableWidget::item:selected { background:%s; color:%s; }"
            % (theme.PRIMARY, theme.TEXT_ON_PRIMARY))
        stockCol.addWidget(self._list, 1)
        listRow.addLayout(stockCol, 1)

        # 中间按钮列（自绘粗箭头）
        midCol = QVBoxLayout()
        midCol.addStretch(1)
        btnToUse = _ArrowButton("right")
        btnFromUse = _ArrowButton("left")
        btnToUse.clicked.connect(self._toUse)
        btnFromUse.clicked.connect(self._fromUse)
        midCol.addWidget(btnToUse)
        midCol.addWidget(btnFromUse)
        midCol.addStretch(1)
        listRow.addLayout(midCol)

        # 当前使用列
        useCol = QVBoxLayout()
        useCol.setSpacing(2)
        useLab = QLabel("当前使用")
        useLab.setStyleSheet("font-weight:bold;")
        useCol.addWidget(useLab)
        self._useList = QTableWidget(0, 3)
        self._useList.setHorizontalHeaderLabels(["T", "名称", "直径"])
        self._useList.setSelectionBehavior(QTableWidget.SelectRows)
        self._useList.setSelectionMode(QTableWidget.SingleSelection)
        self._useList.setEditTriggers(QTableWidget.NoEditTriggers)
        self._useList.verticalHeader().setVisible(False)
        self._useList.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self._useList.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._useList.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self._useList.setStyleSheet(
            "QTableWidget { font-size: 10pt; }"
            " QTableWidget::item:selected { background:%s; color:%s; }"
            % (theme.PRIMARY, theme.TEXT_ON_PRIMARY))
        useCol.addWidget(self._useList, 1)
        listRow.addLayout(useCol, 1)

        llay.addLayout(listRow)
        bottom.addWidget(leftBox, 2)

        # 右：参数面板（标签切换：刀具参数 / 加工参数）
        rightBox = QGroupBox("刀具参数")
        rlay = QVBoxLayout(rightBox)
        rlay.setSpacing(4)
        self._paramTabs = QTabWidget()

        # ---- 标签1：刀具参数 ----
        geoTab = QWidget()
        geoForm = QFormLayout(geoTab)
        geoForm.setSpacing(6)
        geoForm.setLabelAlignment(Qt.AlignRight)
        self._nameEdit = QLineEdit()
        geoForm.addRow("名称", self._nameEdit)
        self._diaSpin = self._dspin(3.0, 0.1, 100)
        geoForm.addRow("直径 mm", self._diaSpin)
        self._fluteLen = self._dspin(15.0, 0.1, 999)
        geoForm.addRow("刃长 mm", self._fluteLen)
        self._totalLen = self._dspin(50.0, 0.1, 999)
        geoForm.addRow("总长 mm", self._totalLen)
        self._shankDia = self._dspin(3.0, 0.1, 100)
        geoForm.addRow("柄径 mm", self._shankDia)
        self._fluteCount = self._ispin(2, 1, 20)
        geoForm.addRow("刃数", self._fluteCount)
        self._taperAngle = self._dspin(0.0, 0, 60)
        geoForm.addRow("锥角 °", self._taperAngle)
        self._effLen = self._dspin(20.0, 0.1, 999)
        geoForm.addRow("有效长度 mm", self._effLen)
        self._paramTabs.addTab(geoTab, "刀具参数")

        # ---- 标签2：加工参数（按当前材料）----
        machTab = QWidget()
        machForm = QFormLayout(machTab)
        machForm.setSpacing(6)
        machForm.setLabelAlignment(Qt.AlignRight)
        self._matLabel = QLabel()
        self._matLabel.setStyleSheet("font-weight:bold;")
        machForm.addRow("加工材料", self._matLabel)
        self._rpm = self._ispin(18000, 100, 99999)
        machForm.addRow("主轴转速 rpm", self._rpm)
        self._feedXY = self._dspin(1200, 1, 99999)
        machForm.addRow("进给速度", self._feedXY)
        self._slotFeed = self._dspin(600, 1, 99999)
        machForm.addRow("开槽速度", self._slotFeed)
        self._plungeFeed = self._dspin(300, 1, 99999)
        machForm.addRow("下刀速度", self._plungeFeed)
        self._approachFeed = self._dspin(800, 1, 99999)
        machForm.addRow("进刀速度", self._approachFeed)
        self._paramTabs.addTab(machTab, "加工参数")

        rlay.addWidget(self._paramTabs)
        bottom.addWidget(rightBox, 1)

        lay.addLayout(bottom, 1)

        # ---- 底部按钮 ----
        btnRow = QHBoxLayout()
        btnOk = self._primaryBtn("确定")
        btnCancel = self._secondaryBtn("取消")
        btnOk.clicked.connect(self._onOk)
        btnCancel.clicked.connect(self.reject)
        btnRow.addStretch(1)
        btnRow.addWidget(btnCancel)
        btnRow.addWidget(btnOk)
        lay.addLayout(btnRow)

        # ---- 初始化 ----
        self._syncing = False
        self._typeButtons[self._currentType].setSelected(True)
        self._list.itemSelectionChanged.connect(self._onStockSelected)
        self._useList.itemSelectionChanged.connect(self._onUseSelected)
        for w in (self._nameEdit, self._diaSpin, self._fluteLen, self._totalLen,
                  self._shankDia, self._fluteCount, self._taperAngle, self._effLen,
                  self._rpm, self._feedXY, self._slotFeed,
                  self._plungeFeed, self._approachFeed):
            if isinstance(w, QLineEdit):
                w.textChanged.connect(self._onParamChanged)
            else:
                w.valueChanged.connect(self._onParamChanged)
        self._refresh()

    def _onTypeChanged(self, key: str) -> None:
        if key == self._currentType:
            return
        self._typeButtons[self._currentType].setSelected(False)
        self._currentType = key
        self._typeButtons[key].setSelected(True)
        self._currentTool = None
        self._refresh()

    def _refresh(self) -> None:
        self._refreshStock()
        self._refreshUseList()
        if self._list.rowCount() > 0:
            self._list.selectRow(0)
        else:
            self._currentTool = None
            self._clearParams()

    def _refreshStock(self) -> None:
        self._syncing = True
        self._list.setRowCount(0)
        for t in self._state.toolLibrary.tools:
            if _toolTypeOf(t) != self._currentType:
                continue
            dia = t.geometry.get("diameter", 0)
            flute = t.geometry.get("flute_length", 0)
            r = self._list.rowCount()
            self._list.insertRow(r)
            self._list.setItem(r, 0, QTableWidgetItem(t.name))
            self._list.setItem(r, 1, QTableWidgetItem(f"{dia:.3f}"))
            self._list.setItem(r, 2, QTableWidgetItem(f"{flute:.1f}"))
            self._list.item(r, 0).setData(Qt.UserRole, t)
        self._syncing = False

    def _refreshUseList(self) -> None:
        self._syncing = True
        self._useList.setRowCount(0)
        for i, tc in enumerate(self._state.toolControllers, 1):
            t = tc.tool
            if _toolTypeOf(t) != self._currentType:
                continue
            dia = t.geometry.get("diameter", 0)
            r = self._useList.rowCount()
            self._useList.insertRow(r)
            self._useList.setItem(r, 0, QTableWidgetItem(f"T{i}"))
            self._useList.setItem(r, 1, QTableWidgetItem(t.name))
            self._useList.setItem(r, 2, QTableWidgetItem(f"{dia:.3f}"))
            self._useList.item(r, 0).setData(Qt.UserRole, t)
        self._syncing = False

    def _onStockSelected(self) -> None:
        if self._syncing:
            return
        row = self._list.currentRow()
        if row < 0:
            return
        self._syncing = True
        self._useList.clearSelection()
        self._syncing = False
        item = self._list.item(row, 0)
        if item is not None:
            self._loadToolParams(item.data(Qt.UserRole))

    def _onUseSelected(self) -> None:
        if self._syncing:
            return
        row = self._useList.currentRow()
        if row < 0:
            return
        self._syncing = True
        self._list.clearSelection()
        self._syncing = False
        item = self._useList.item(row, 0)
        if item is not None:
            self._loadToolParams(item.data(Qt.UserRole))

    def _allParamWidgets(self):
        return (self._nameEdit, self._diaSpin, self._fluteLen, self._totalLen,
                self._shankDia, self._fluteCount, self._taperAngle, self._effLen,
                self._rpm, self._feedXY, self._slotFeed,
                self._plungeFeed, self._approachFeed)

    def _machKey(self, suffix: str) -> str:
        return f"{suffix}_{self._state.materialType}"

    def _defaultMachParams(self) -> dict:
        """按当前材料硬度推导加工速度默认值。"""
        info = MATERIALS.get(self._state.materialType,
                             {"hardness": 3.0, "feedRate": 2000})
        feed = float(info["feedRate"])
        hardness = float(info["hardness"])
        rpm = max(2000, int(18000 - hardness * 30))
        return {
            "rpm": rpm,
            "feed": feed,
            "slot_feed": feed * 0.5,
            "plunge_feed": feed * 0.3,
            "approach_feed": feed * 0.6,
        }

    def _clearParams(self) -> None:
        for w in self._allParamWidgets():
            w.blockSignals(True)
        self._nameEdit.setText("")
        self._diaSpin.setValue(3.0)
        self._fluteLen.setValue(15.0)
        self._totalLen.setValue(50.0)
        self._shankDia.setValue(3.0)
        self._fluteCount.setValue(2)
        self._taperAngle.setValue(0.0 if not self._currentType.startswith("taper_") else 10.0)
        self._effLen.setValue(20.0)
        self._matLabel.setText(self._state.materialType)
        d = self._defaultMachParams()
        self._rpm.setValue(d["rpm"])
        self._feedXY.setValue(d["feed"])
        self._slotFeed.setValue(d["slot_feed"])
        self._plungeFeed.setValue(d["plunge_feed"])
        self._approachFeed.setValue(d["approach_feed"])
        for w in self._allParamWidgets():
            w.blockSignals(False)

    def _loadToolParams(self, tool) -> None:
        if tool is None:
            return
        self._currentTool = tool
        for w in self._allParamWidgets():
            w.blockSignals(True)
        g = tool.geometry
        self._nameEdit.setText(tool.name)
        self._diaSpin.setValue(float(g.get("diameter", 3.0)))
        self._fluteLen.setValue(float(g.get("flute_length", 15.0)))
        self._totalLen.setValue(float(g.get("total_length", 50.0)))
        self._shankDia.setValue(float(g.get("shank_diameter", 3.0)))
        self._fluteCount.setValue(int(g.get("flute_count", 2)))
        self._taperAngle.setValue(float(g.get("taper_angle", 0.0)))
        self._effLen.setValue(float(g.get("effective_length", 20.0)))
        self._matLabel.setText(self._state.materialType)
        d = self._defaultMachParams()
        self._rpm.setValue(int(g.get(self._machKey("rpm"), d["rpm"])))
        self._feedXY.setValue(float(g.get(self._machKey("feed"), d["feed"])))
        self._slotFeed.setValue(float(g.get(self._machKey("slot_feed"), d["slot_feed"])))
        self._plungeFeed.setValue(float(g.get(self._machKey("plunge_feed"), d["plunge_feed"])))
        self._approachFeed.setValue(float(g.get(self._machKey("approach_feed"), d["approach_feed"])))
        for w in self._allParamWidgets():
            w.blockSignals(False)

    def _onParamChanged(self) -> None:
        if self._currentTool is None:
            return
        g = self._currentTool.geometry
        self._currentTool.name = self._nameEdit.text()
        g["diameter"] = self._diaSpin.value()
        g["flute_length"] = self._fluteLen.value()
        g["total_length"] = self._totalLen.value()
        g["shank_diameter"] = self._shankDia.value()
        g["flute_count"] = self._fluteCount.value()
        g["taper_angle"] = self._taperAngle.value()
        g["effective_length"] = self._effLen.value()
        g[self._machKey("rpm")] = self._rpm.value()
        g[self._machKey("feed")] = self._feedXY.value()
        g[self._machKey("slot_feed")] = self._slotFeed.value()
        g[self._machKey("plunge_feed")] = self._plungeFeed.value()
        g[self._machKey("approach_feed")] = self._approachFeed.value()
        # 同步表格显示
        dia = self._diaSpin.value()
        flute = self._fluteLen.value()
        row = self._list.currentRow()
        if row >= 0:
            self._list.item(row, 0).setText(self._currentTool.name)
            self._list.item(row, 1).setText(f"{dia:.3f}")
            self._list.item(row, 2).setText(f"{flute:.1f}")
        # 同步"当前使用"列表：遍历所有行按 tool 引用匹配（用户可能在库存列
        # 编辑，useList 的 currentRow 为 -1，只更新选中行会漏）
        for r in range(self._useList.rowCount()):
            it = self._useList.item(r, 0)
            if it is not None and it.data(Qt.UserRole) is self._currentTool:
                self._useList.item(r, 1).setText(self._currentTool.name)
                self._useList.item(r, 2).setText(f"{dia:.3f}")
        # 通知主面板/画布刷新（当前使用刀具列表、3D 刀具显示）
        try:
            self._state.notifyChanged()
        except Exception:
            pass
        self._state.notifyChanged()

    def _addTool(self) -> None:
        from pymillcam.core.tools import Tool, ToolShape
        shape = ToolShape(_SHAPE_MAP[self._currentType])
        taper = 10.0 if self._currentType.startswith("taper_") else 0.0
        label = _TYPE_LABEL[self._currentType]
        dia = 3.0
        tool = Tool(name=f"Ø{dia:g} {label}",
                    shape=shape,
                    geometry={"diameter": dia, "flute_length": 15.0,
                              "total_length": 50.0,
                              # 柄径最小 4mm：直径 ≤4 的刀柄径都等于 4
                              "shank_diameter": max(dia, 4.0),
                              "flute_count": 2, "taper_angle": taper,
                              "effective_length": 20.0})
        self._state.toolLibrary.tools.append(tool)
        self._state.notifyChanged()
        self._refresh()
        for i in range(self._list.rowCount()):
            if self._list.item(i, 0).data(Qt.UserRole) is tool:
                self._list.selectRow(i)
                break

    def _delTool(self) -> None:
        row = self._list.currentRow()
        if row < 0:
            return
        item = self._list.item(row, 0)
        if item is None:
            return
        tool = item.data(Qt.UserRole)
        # 同时从当前使用中移除引用该 tool 的 controller
        self._state.toolControllers = [tc for tc in self._state.toolControllers
                                       if tc.tool is not tool]
        try:
            self._state.toolLibrary.tools.remove(tool)
        except ValueError:
            pass
        self._currentTool = None
        self._state.notifyChanged()
        self._refresh()

    def _toUse(self) -> None:
        """库存 → 当前使用：为选中刀具创建 ToolController。"""
        row = self._list.currentRow()
        if row < 0:
            return
        item = self._list.item(row, 0)
        if item is None:
            return
        tool = item.data(Qt.UserRole)
        for tc in self._state.toolControllers:
            if tc.tool is tool:
                return
        from pymillcam.core.tools import ToolController
        num = len(self._state.toolControllers) + 1
        tc = ToolController(tool_number=num, tool=tool)
        self._state.toolControllers.append(tc)
        self._state.notifyChanged()
        self._refreshUseList()

    def _fromUse(self) -> None:
        """当前使用 ← 移除：删除选中刀具对应的 ToolController。"""
        row = self._useList.currentRow()
        if row < 0:
            return
        item = self._useList.item(row, 0)
        if item is None:
            return
        tool = item.data(Qt.UserRole)
        for tc in self._state.toolControllers:
            if tc.tool is tool:
                self._state.toolControllers.remove(tc)
                break
        self._state.notifyChanged()
        self._refreshUseList()

    def _onOk(self) -> None:
        # 持久化刀具库到用户配置目录
        try:
            from pathlib import Path
            from pymillcam.core.tool_library import save_library
            cfgDir = Path.home() / ".config" / "cangai_relief"
            save_library(self._state.toolLibrary, cfgDir / "tool_library.json")
        except Exception:
            pass
        self._state.notifyChanged()
        self.accept()

    # ----- 控件工厂 -----
    def _dspin(self, v, lo, hi):
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(3)
        s.setValue(float(v))
        return s

    def _ispin(self, v, lo, hi):
        s = QSpinBox()
        s.setRange(lo, hi)
        s.setValue(int(v))
        return s

    def _primaryBtn(self, text):
        b = QPushButton(text)
        b.setStyleSheet(
            "QPushButton { background:%s; color:%s; border:none;"
            " padding:6px 16px; border-radius:3px; font-weight:bold; }"
            % (theme.PRIMARY, theme.TEXT_ON_PRIMARY))
        return b

    def _secondaryBtn(self, text):
        b = QPushButton(text)
        b.setStyleSheet(
            "QPushButton { background:#fff; color:%s; border:1px solid %s;"
            " padding:6px 16px; border-radius:3px; }" % (theme.TEXT, theme.BORDER))
        return b


# ============================================================ 生成刀路

class CncToolpathDialog(QDialog):
    """生成刀路详细参数对话框：加工方案/图形/刀具 + 走刀/进给/安全/高级/计算 多标签页。

    确定后用参数构造 ProfileParams → build_profile_project → generate_profile_gcode，
    存入 CncState.toolpaths / gcode。
    """

    def __init__(self, doc=None, parent=None, entry=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("编辑路径" if entry is not None else "路径生成")
        self.setMinimumSize(680, 560)
        self._state = CncState.instance()
        self._doc = doc
        self._entry = entry          # 编辑已有路径时传入，None=新建
        self._picking = False
        self._pickingType = ""

        lay = QVBoxLayout(self)
        lay.setSpacing(8)
        lay.setContentsMargins(8, 8, 8, 8)

        # ---- 顶部：轴类型切换标签 + 具体加工方式 ----
        self._axisTabs = QTabWidget()
        self._axisTabs.setMaximumHeight(70)
        self._methodGroup = QButtonGroup(self)
        self._methodGroup.setExclusive(True)

        # 2轴加工
        w2 = QWidget()
        l2 = QHBoxLayout(w2)
        l2.setContentsMargins(4, 2, 4, 2)
        axis2Methods = [
            (0, "钻孔"), (1, "扩孔"), (2, "铣螺纹"),
            (3, "单线切割"), (4, "单线摆槽"), (5, "轮廓切割"), (6, "区域加工"),
        ]
        for mid, label in axis2Methods:
            rb = QRadioButton(label)
            self._methodGroup.addButton(rb, mid)
            l2.addWidget(rb)
        l2.addStretch(1)
        self._axisTabs.addTab(w2, "2轴加工")

        # 3轴加工
        w3 = QWidget()
        l3 = QHBoxLayout(w3)
        l3.setContentsMargins(4, 2, 4, 2)
        axis3Methods = [
            (7, "分层区域粗加工"), (8, "曲面精加工"), (9, "路径加工"),
        ]
        for mid, label in axis3Methods:
            rb = QRadioButton(label)
            self._methodGroup.addButton(rb, mid)
            l3.addWidget(rb)
        l3.addStretch(1)
        self._axisTabs.addTab(w3, "3轴加工")

        # 4轴加工（占位）
        w4 = QWidget()
        l4 = QHBoxLayout(w4)
        l4.setContentsMargins(4, 2, 4, 2)
        rb4 = QRadioButton("4轴加工（开发中）")
        rb4.setEnabled(False)
        self._methodGroup.addButton(rb4, 10)
        l4.addWidget(rb4)
        l4.addStretch(1)
        self._axisTabs.addTab(w4, "4轴加工")

        # 默认选"轮廓切割"
        btn = self._methodGroup.button(5)
        if btn is not None:
            btn.setChecked(True)
        lay.addWidget(self._axisTabs)

        # ---- 左右分栏：左=基本信息(1坯料 2刀具 3加工区域 4加工深度)  右=走刀方式等标签 ----
        mainSplit = QHBoxLayout()
        mainSplit.setSpacing(8)

        # 左侧面板
        leftPanel = QWidget()
        leftLay = QVBoxLayout(leftPanel)
        leftLay.setSpacing(6)
        leftLay.setContentsMargins(0, 0, 0, 0)
        leftPanel.setMinimumWidth(200)
        leftPanel.setMaximumWidth(220)

        # 1. 坯料
        s1 = QHBoxLayout()
        s1.addWidget(self._boldLabel("1 坯料"))
        s1.addStretch(1)
        leftLay.addLayout(s1)
        self._stockLabel = QLabel(self._stockText())
        self._stockLabel.setStyleSheet("color:%s; padding-left:12px;" % theme.TEXT_DIM)
        leftLay.addWidget(self._stockLabel)

        # 2. 刀具
        s2 = QHBoxLayout()
        s2.addWidget(self._boldLabel("2 刀具"))
        s2.addStretch(1)
        leftLay.addLayout(s2)
        self._toolCombo = QComboBox()
        self._toolCombo.setMinimumWidth(200)
        toolWrap = QHBoxLayout()
        toolWrap.setContentsMargins(12, 0, 0, 0)
        toolWrap.addWidget(self._toolCombo)
        leftLay.addLayout(toolWrap)

        # 3. 加工区域（点/线/面/几何体各自独立选取）
        s3 = QHBoxLayout()
        s3.addWidget(self._boldLabel("3 加工区域"))
        s3.addStretch(1)
        leftLay.addLayout(s3)
        self._pickedObjects = {}
        self._pickLabels = {}
        pickTypes = [("点", "点"), ("线", "线"), ("面", "面"), ("几何体", "几何体")]
        for tkey, tlabel in pickTypes:
            self._pickedObjects[tkey] = []
            row = QHBoxLayout()
            row.setContentsMargins(12, 0, 0, 0)
            row.addWidget(QLabel(tlabel))
            lbl = QLabel("未选择")
            lbl.setStyleSheet("color:%s;" % theme.TEXT_DIM)
            self._pickLabels[tkey] = lbl
            row.addWidget(lbl, 1)
            btn = QPushButton("选取")
            btn.setStyleSheet(
                "QPushButton { background:%s; color:%s; border:none;"
                " padding:3px 10px; border-radius:3px; }"
                % (theme.PRIMARY, theme.TEXT_ON_PRIMARY))
            btn.clicked.connect(lambda checked, k=tkey: self._onPickCurves(k))
            row.addWidget(btn)
            clr = QPushButton("清除")
            clr.setStyleSheet(
                "QPushButton { background:#e04f4f; color:%s; border:none;"
                " padding:3px 10px; border-radius:3px; }"
                % theme.TEXT_ON_PRIMARY)
            clr.clicked.connect(lambda checked, k=tkey: self._onClearPick(k))
            row.addWidget(clr)
            leftLay.addLayout(row)

        # 4. 加工深度
        s4 = QHBoxLayout()
        s4.addWidget(self._boldLabel("4 加工深度"))
        s4.addStretch(1)
        leftLay.addLayout(s4)
        depthRow = QHBoxLayout()
        depthRow.setContentsMargins(12, 0, 0, 0)
        self._depthStartSpin = self._dspin(0.0, -999, 0)
        self._depthEndSpin = self._dspin(-3.0, -999, 0)
        depthRow.addWidget(QLabel("起始"))
        depthRow.addWidget(self._depthStartSpin)
        depthRow.addWidget(QLabel("终止"))
        depthRow.addWidget(self._depthEndSpin)
        leftLay.addLayout(depthRow)

        leftLay.addStretch(1)
        mainSplit.addWidget(leftPanel)

        # ---- 右侧：参数标签页（按加工方式动态构建）----
        self._tabs = QTabWidget()
        self._rebuildTabs(self._methodGroup.checkedId())
        self._methodGroup.buttonToggled.connect(
            lambda btn, checked: self._onMethodChanged(btn, checked))

        mainSplit.addWidget(self._tabs, 1)
        lay.addLayout(mainSplit, 1)

        # ---- 底部按钮 ----
        btnRow = QHBoxLayout()
        btnOk = self._primaryBtn("确定")
        btnCancel = self._secondaryBtn("取消")
        btnOk.clicked.connect(self._onOk)
        btnCancel.clicked.connect(self.reject)
        btnRow.addStretch(1)
        btnRow.addWidget(btnCancel)
        btnRow.addWidget(btnOk)
        lay.addLayout(btnRow)

        # ---- 初始化 ----
        self._refreshTools()
        self._refreshGeometry()
        self._toolCombo.currentIndexChanged.connect(self._onToolChanged)
        if entry is not None:
            self._applyEntry(entry)

    def _onMethodChanged(self, btn, checked: bool) -> None:
        """加工方式切换 → 重建右侧标签页。"""
        if checked:
            self._rebuildTabs(self._methodGroup.id(btn))

    # ----- 参数快照（编辑已有路径时回填）-----
    METHOD_NAMES = {0: "钻孔", 1: "扩孔", 2: "铣螺纹", 3: "单线切割",
                    4: "单线摆槽", 5: "轮廓切割", 6: "区域加工",
                    7: "分层区域粗加工", 8: "曲面精加工", 9: "路径加工"}
    _SPIN_NAMES = ("_cutDepthSpin", "_depthStartSpin", "_depthEndSpin",
                   "_rpmSpin", "_feedXySpin", "_slotFeedSpin", "_feedZSpin",
                   "_approachFeedSpin", "_leadLenSpin", "_rampAngleSpin",
                   "_tabCountSpin", "_tabWidthSpin", "_tabHeightSpin",
                   "_dwellSpin", "_pitchSpin", "_threadDiaSpin",
                   "_boreDiaSpin", "_finishAllowanceSpin",
                   "_passCountSpin", "_grooveWidthSpin", "_groovePassSpin",
                   "_grooveStepSpin", "_threadPassSpin",
                   "_regionStepSpin", "_regionAllowSpin",
                   "_roughPassSpin", "_finishPassSpin", "_scanAngleSpin",
                   "_minStepSpin", "_maxStepSpin", "_stepoverSpin",
                   "_scallopSpin", "_finishAllowSpin", "_chordTolSpin",
                   "_safeHeightSpin", "_clearanceSpin", "_rampPeckSpin",
                   "_regionStepSpin", "_regionXyAllowSpin", "_regionZAllowSpin",
                   "_pathStepoverSpin", "_pathFinishSpin")
    _COMBO_NAMES = ("_leadStyleCombo", "_rampStrategyCombo", "_drillModeCombo",
                    "_expandModeCombo", "_threadTypeCombo", "_scanModeCombo",
                    "_finishRingCombo", "_cornerCombo", "_orderCombo", "_trimCombo", "_projCombo",
                    "_pathTypeCombo", "_contourCombo")
    _GROUP_NAMES = ("_offsetGroup", "_dirGroup", "_layerModeGroup",
                    "_stepModeGroup")
    _CHK_NAMES = ("_tabsEnabledChk", "_smoothChk")

    def _collectParams(self) -> dict:
        """把当前所有控件值打包成 flat dict（存入 ToolpathEntry.params）。"""
        d = {"_method": self._methodGroup.checkedId(),
             "_axisTab": self._axisTabs.currentIndex(),
             "_toolIndex": self._toolCombo.currentIndex()}
        for name in self._SPIN_NAMES:
            w = getattr(self, name, None)
            if w is not None:
                d[name] = w.value()
        for name in self._COMBO_NAMES:
            w = getattr(self, name, None)
            if w is not None:
                d[name] = w.currentData()
        for name in self._GROUP_NAMES:
            w = getattr(self, name, None)
            if w is not None and w.checkedId() is not None:
                d[name] = w.checkedId()
        for name in self._CHK_NAMES:
            w = getattr(self, name, None)
            if w is not None:
                d[name] = w.isChecked()
        return d

    def _applyParams(self, d: dict) -> None:
        """回填 flat dict 到控件（不同加工方式控件集不同，存在才填）。"""
        for name, val in d.items():
            if name in ("_method", "_axisTab", "_toolIndex"):
                continue
            if name in self._SPIN_NAMES:
                w = getattr(self, name, None)
                if w is not None:
                    w.setValue(float(val))
            elif name in self._COMBO_NAMES:
                w = getattr(self, name, None)
                if w is not None:
                    idx = w.findData(val)
                    if idx >= 0:
                        w.setCurrentIndex(idx)
            elif name in self._GROUP_NAMES:
                w = getattr(self, name, None)
                if w is not None:
                    btn = w.button(int(val))
                    if btn is not None:
                        btn.setChecked(True)
            elif name in self._CHK_NAMES:
                w = getattr(self, name, None)
                if w is not None:
                    w.setChecked(bool(val))

    def _applyEntry(self, entry) -> None:
        """编辑模式：把已有条目的加工方式/参数/加工区域回填到对话框。"""
        p = entry.params
        mid = int(p.get("_method", 0))
        btn = self._methodGroup.button(mid)
        if btn is not None:
            self._methodGroup.blockSignals(True)
            btn.setChecked(True)
            self._methodGroup.blockSignals(False)
        self._rebuildTabs(mid)
        self._axisTabs.setCurrentIndex(int(p.get("_axisTab", 0)))
        ti = p.get("_toolIndex")
        if ti is not None and 0 <= int(ti) < self._toolCombo.count():
            self._toolCombo.blockSignals(True)
            self._toolCombo.setCurrentIndex(int(ti))
            self._toolCombo.blockSignals(False)
        self._applyParams(p)
        picked = {k: list(v) for k, v in entry.picked.items()}
        for k in self._pickedObjects:
            picked.setdefault(k, [])
        self._pickedObjects = picked
        self._refreshGeometry()

    def _saveEntry(self, toolpaths, gcode) -> None:
        """生成成功后：新建或更新 ToolpathEntry 并通知 CncState。"""
        method = self._methodGroup.checkedId()
        method_name = self.METHOD_NAMES.get(method, "路径")
        tc = self._toolCombo.currentData()
        dia = tc.tool.geometry.get("diameter", 0) if tc else 0
        depth = abs(self._depthEndSpin.value()) if hasattr(self, "_depthEndSpin") else 0.0
        title = CncState.makeEntryTitle(method_name, dia, depth)
        if self._entry is not None:
            e = self._entry
            e.method_id = method
            e.method_name = method_name
            e.title = title
            e.params = self._collectParams()
            e.picked = {k: list(v) for k, v in self._pickedObjects.items()}
            e.toolpaths = list(toolpaths)
            e.gcode = gcode
            self._state.updatePathEntry(e)
        else:
            e = ToolpathEntry(
                method_id=method, method_name=method_name, title=title,
                params=self._collectParams(),
                picked={k: list(v) for k, v in self._pickedObjects.items()},
                toolpaths=list(toolpaths), gcode=gcode)
            # 新建时按添加顺序分配默认轨迹色（与画布渲染一致）
            try:
                from ui.view_2d.cnc_path_overlay import _ENTRY_COLORS
                e.color = _ENTRY_COLORS[len(self._state.pathEntries) % len(_ENTRY_COLORS)]
            except Exception:
                e.color = ""
            self._entry = e
            self._state.addPathEntry(e)
        self._state.collisionResult = None

    def _rebuildTabs(self, methodId: int) -> None:
        """按加工方式重建右侧参数标签页。"""
        self._tabs.clear()
        m = methodId
        if m == 0:    self._buildDrill()
        elif m == 1:  self._buildExpand()
        elif m == 2:  self._buildThread()
        elif m == 3:  self._buildSingleCut()
        elif m == 4:  self._buildSingleGroove()
        elif m == 5:  self._buildContour()
        elif m == 6:  self._buildRegion()
        elif m == 7:  self._buildRoughRegion()
        elif m == 8:  self._buildSurfaceFinish()
        elif m == 9:  self._buildPathMachine()
        else:          self._buildPlaceholder()
        # 3D 加工（分层区域粗加工 / 曲面精加工）：加工深度由模型本身
        # 决定，左侧"加工深度"（起始/终止）参数置灰不可改。
        auto_depth = m in (7, 8)
        if hasattr(self, "_depthStartSpin"):
            self._depthStartSpin.setEnabled(not auto_depth)
        if hasattr(self, "_depthEndSpin"):
            self._depthEndSpin.setEnabled(not auto_depth)

    # ----- 标签页构建辅助 -----
    def _formTab(self):
        w = QWidget()
        f = QFormLayout(w)
        f.setSpacing(6)
        f.setLabelAlignment(Qt.AlignRight)
        return w, f

    def _radioRow(self, parent, items):
        g = QButtonGroup(parent)
        g.setExclusive(True)
        w = QWidget()
        l = QHBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        for i, (rid, label) in enumerate(items):
            rb = QRadioButton(label)
            g.addButton(rb, rid)
            l.addWidget(rb)
            if i == 0:
                rb.setChecked(True)
        l.addStretch(1)
        return w, g

    def _comboRow(self, items):
        c = QComboBox()
        for label, data in items:
            c.addItem(label, data)
        return c

    def _addFeedTab(self, extraRows=None):
        """进给设置：分层方式 + 吃刀深度/层数 + 速度参数(从刀具读) + 额外行。

        分层方式三档：
          - 限定深度：参数 = 吃刀深度 mm（每层下刀量）
          - 限定层数：参数 = 层数（整数），每层下刀量 = 总深 / 层数
          - 关闭：不分层，参数禁用
        """
        t, f = self._formTab()
        w, self._layerModeGroup = self._radioRow(t, [(0, "限定深度"), (1, "限定层数"), (2, "关闭")])
        f.addRow("分层方式", w)
        self._cutDepthSpin = self._dspin(1.0, 0.01, 999)
        f.addRow("吃刀深度 mm", self._cutDepthSpin)
        self._cutDepthLabel = f.labelForField(self._cutDepthSpin)
        if extraRows:
            for label, widget in extraRows:
                f.addRow(label, widget)
        # 速度参数（从刀具加工参数读默认值，用户可修改）
        f.addRow(self._boldLabel("── 速度参数 ──"), QLabel(""))
        self._rpmSpin = self._ispin(18000, 100, 99999)
        f.addRow("主轴转速 rpm", self._rpmSpin)
        self._feedXySpin = self._dspin(1200, 1, 99999)
        f.addRow("进给速度 XY", self._feedXySpin)
        self._slotFeedSpin = self._dspin(600, 1, 99999)
        f.addRow("开槽进给", self._slotFeedSpin)
        self._feedZSpin = self._dspin(300, 1, 99999)
        f.addRow("下刀速度 Z", self._feedZSpin)
        self._approachFeedSpin = self._dspin(500, 1, 99999)
        f.addRow("进刀速度", self._approachFeedSpin)
        self._layerModeGroup.idClicked.connect(self._onLayerModeChanged)
        self._onLayerModeChanged(self._layerModeGroup.checkedId())
        self._tabs.addTab(t, "进给设置")

    def _onLayerModeChanged(self, mode: int) -> None:
        """分层方式切换：限定深度=吃刀深度mm，限定层数=层数(整数)，关闭=禁用。"""
        if not hasattr(self, "_cutDepthSpin"):
            return
        if mode == 1:   # 限定层数
            self._cutDepthLabel.setText("层数")
            self._cutDepthSpin.setDecimals(0)
            self._cutDepthSpin.setRange(1, 999)
            self._cutDepthSpin.setValue(max(1, int(round(self._cutDepthSpin.value()))))
            self._cutDepthSpin.setEnabled(True)
        elif mode == 0:  # 限定深度
            self._cutDepthLabel.setText("吃刀深度 mm")
            self._cutDepthSpin.setDecimals(2)
            self._cutDepthSpin.setRange(0.01, 999)
            self._cutDepthSpin.setEnabled(True)
        else:           # 关闭：不分层
            self._cutDepthSpin.setEnabled(False)

    def _layerStepdown(self, total_depth: float) -> float:
        """按分层方式返回每层下刀量（正数）。

        total_depth 为目标深度（负=向下）。限定层数时 = |总深|/层数；
        关闭时一次到底 = |总深|。
        """
        mode = self._layerModeGroup.checkedId() if hasattr(self, "_layerModeGroup") else 0
        if mode == 1:
            layers = max(1, int(self._cutDepthSpin.value()))
            return abs(total_depth) / layers
        if mode == 0:
            return self._cutDepthSpin.value() if hasattr(self, "_cutDepthSpin") else 1.0
        return abs(total_depth)

    def _addSafeTab(self, withLead=True, withRamp=True, rampOptions=None):
        """安全策略标签页。rampOptions 覆盖下刀策略选项（如单线切割专用）。"""
        t, f = self._formTab()
        self._safeHeightSpin = self._dspin(self._state.machine.defaults.safe_height, 0.1, 999)
        f.addRow("安全高度 mm", self._safeHeightSpin)
        self._clearanceSpin = self._dspin(self._state.machine.defaults.clearance_plane, 0.0, 999)
        f.addRow("间隙平面 mm", self._clearanceSpin)
        if withLead:
            self._leadStyleCombo = self._comboRow([("圆弧引入","arc"),("切向引入","tangent"),("直接下刀","direct")])
            f.addRow("引入样式", self._leadStyleCombo)
            self._leadLenSpin = self._dspin(2.0, 0.0, 999)
            f.addRow("引入长度 mm", self._leadLenSpin)
        if withRamp:
            opts = rampOptions or [("线性斜坡","linear"),("螺旋下刀","helical"),("直接下刀","plunge")]
            self._rampStrategyCombo = self._comboRow(opts)
            f.addRow("下刀策略", self._rampStrategyCombo)
            self._rampAngleSpin = self._dspin(3.0, 0.1, 90)
            f.addRow("斜坡角度 °", self._rampAngleSpin)
            if any(o[1] == "peck" for o in opts):
                self._rampPeckSpin = self._dspin(1.0, 0.1, 99)
                f.addRow("啄钻深度 mm", self._rampPeckSpin)
        self._tabs.addTab(t, "安全策略")

    def _addCalcTab(self, extraRows=None):
        """计算设置标签页。"""
        t, f = self._formTab()
        self._chordTolSpin = self._dspin(0.02, 0.001, 1.0)
        f.addRow("加工精度(弦差) mm", self._chordTolSpin)
        self._orderCombo = self._comboRow([("按选择顺序","select"),("最近邻优化","nearest"),("X 方向排序","x_sort"),("Y 方向排序","y_sort")])
        f.addRow("加工次序", self._orderCombo)
        self._cornerCombo = self._comboRow([("尖角保留","sharp"),("尖角倒圆","round"),("尖角倒角","chamfer")])
        f.addRow("尖角形式", self._cornerCombo)
        if extraRows:
            for label, widget in extraRows:
                f.addRow(label, widget)
        self._tabs.addTab(t, "计算设置")

    def _addTabsTab(self):
        """高级设置：连接片。"""
        t, f = self._formTab()
        self._tabsEnabledChk = QCheckBox("启用连接片")
        f.addRow("连接片", self._tabsEnabledChk)
        self._tabCountSpin = self._ispin(4, 1, 50)
        f.addRow("连接片数量", self._tabCountSpin)
        self._tabWidthSpin = self._dspin(5.0, 0.1, 999)
        f.addRow("连接片宽度 mm", self._tabWidthSpin)
        self._tabHeightSpin = self._dspin(1.5, 0.1, 999)
        f.addRow("连接片高度 mm", self._tabHeightSpin)
        self._tabs.addTab(t, "高级设置")

    # ----- 各加工方式标签页 -----
    def _buildDrill(self):
        """钻孔：走刀(钻孔模式+暂停) + 进给 + 安全(无引入) + 计算。"""
        t, f = self._formTab()
        self._drillModeCombo = self._comboRow([("深孔啄钻","peck"),("简单钻孔","simple"),("断屑钻","chip")])
        f.addRow("钻孔模式", self._drillModeCombo)
        self._dwellSpin = self._dspin(0.0, 0.0, 10)
        f.addRow("暂停时间 s", self._dwellSpin)
        self._tabs.addTab(t, "走刀方式")
        self._addFeedTab()
        self._addSafeTab(withLead=False, withRamp=True)
        self._addCalcTab()

    def _buildExpand(self):
        """扩孔：走刀(方向+模式) + 进给(含余量) + 安全 + 计算。"""
        t, f = self._formTab()
        w, self._dirGroup = self._radioRow(t, [(0,"顺时针"),(1,"逆时针")])
        f.addRow("扩孔方向", w)
        self._expandModeCombo = self._comboRow([("螺旋插补","helical"),("圆周分层","circular")])
        f.addRow("扩孔模式", self._expandModeCombo)
        self._boreDiaSpin = self._dspin(6.0, 0.1, 999)
        f.addRow("扩孔直径 mm", self._boreDiaSpin)
        self._tabs.addTab(t, "走刀方式")
        self._finishAllowanceSpin = self._dspin(0.2, 0.0, 10)
        self._addFeedTab([("扩孔余量 mm", self._finishAllowanceSpin)])
        self._addSafeTab()
        self._addCalcTab()

    def _buildThread(self):
        """铣螺纹：走刀(旋向+螺距+直径) + 进给 + 安全(无引入) + 计算(螺纹类型)。"""
        t, f = self._formTab()
        w, self._dirGroup = self._radioRow(t, [(0,"右旋"),(1,"左旋")])
        f.addRow("螺纹旋向", w)
        self._pitchSpin = self._dspin(1.5, 0.1, 20)
        f.addRow("螺距 mm", self._pitchSpin)
        self._threadDiaSpin = self._dspin(6.0, 0.1, 999)
        f.addRow("螺纹直径 mm", self._threadDiaSpin)
        self._tabs.addTab(t, "走刀方式")
        self._addFeedTab()
        self._addSafeTab(withLead=False, withRamp=True)
        t2, f2 = self._formTab()
        self._threadTypeCombo = self._comboRow([("内螺纹","internal"),("外螺纹","external")])
        f2.addRow("螺纹类型", self._threadTypeCombo)
        self._threadPassSpin = self._ispin(2, 1, 20)
        f2.addRow("精加工次数", self._threadPassSpin)
        self._tabs.addTab(t2, "计算设置")

    def _buildSingleCut(self):
        """单线切割：走刀(偏移+方向+次数) + 进给 + 安全 + 连接片 + 计算。"""
        t, f = self._formTab()
        w, self._offsetGroup = self._radioRow(t, [(0,"外"),(1,"内"),(2,"在线")])
        f.addRow("偏移侧", w)
        w2, self._dirGroup = self._radioRow(t, [(0,"顺铣"),(1,"逆铣")])
        f.addRow("走刀方向", w2)
        self._passCountSpin = self._ispin(1, 1, 50)
        f.addRow("切割次数", self._passCountSpin)
        self._tabs.addTab(t, "走刀方式")
        self._addFeedTab()
        self._addSafeTab(rampOptions=[("直接下刀","plunge"),("啄钻下刀","peck"),("沿轮廓下刀","ramp")])
        self._addTabsTab()
        self._addCalcTab()

    def _buildSingleGroove(self):
        """单线摆槽：走刀(偏移+方向+槽宽+次数) + 进给(含侧向步距) + 安全 + 计算。"""
        t, f = self._formTab()
        w, self._offsetGroup = self._radioRow(t, [(0,"外"),(1,"内"),(2,"在线")])
        f.addRow("偏移侧", w)
        w2, self._dirGroup = self._radioRow(t, [(0,"顺铣"),(1,"逆铣")])
        f.addRow("走刀方向", w2)
        self._grooveWidthSpin = self._dspin(5.0, 0.1, 999)
        f.addRow("摆槽宽度 mm", self._grooveWidthSpin)
        self._groovePassSpin = self._ispin(1, 1, 50)
        f.addRow("摆槽次数", self._groovePassSpin)
        self._tabs.addTab(t, "走刀方式")
        self._grooveStepSpin = self._dspin(2.0, 0.01, 999)
        self._addFeedTab([("侧向步距 mm", self._grooveStepSpin)])
        self._addSafeTab()
        self._addCalcTab()

    def _buildContour(self):
        """轮廓切割：走刀(偏移+方向+粗精次数) + 进给(含精加工余量) + 安全 + 连接片 + 计算(含轮廓设置)。"""
        t, f = self._formTab()
        w, self._offsetGroup = self._radioRow(t, [(0,"外"),(1,"内"),(2,"在线")])
        f.addRow("偏移侧", w)
        w2, self._dirGroup = self._radioRow(t, [(0,"顺铣"),(1,"逆铣")])
        f.addRow("走刀方向", w2)
        self._roughPassSpin = self._ispin(1, 1, 50)
        f.addRow("粗加工次数", self._roughPassSpin)
        self._finishPassSpin = self._ispin(1, 0, 50)
        f.addRow("精加工次数", self._finishPassSpin)
        self._tabs.addTab(t, "走刀方式")
        self._addFeedTab([("精加工余量 mm", self._dspin(0.0, 0.0, 10))])
        self._addSafeTab()
        self._addTabsTab()
        self._addCalcTab([("轮廓设置", self._comboRow([("单次走刀","single"),("多次走刀","multi"),("精修走刀","finish")]))])

    def _buildRegion(self):
        """区域加工：走刀(模式+方向+角度) + 进给(含步距+余量) + 安全 + 计算(含边界保留)。"""
        t, f = self._formTab()
        self._scanModeCombo = self._comboRow([("平行扫描","parallel"),("环切","offset"),("螺旋","helical")])
        f.addRow("走刀模式", self._scanModeCombo)
        w, self._dirGroup = self._radioRow(t, [(0,"顺铣"),(1,"逆铣")])
        f.addRow("走刀方向", w)
        self._scanAngleSpin = self._dspin(0.0, 0.0, 360)
        f.addRow("扫描角度 °", self._scanAngleSpin)
        self._scanAngleSpin.setToolTip("仅平行扫描有效：扫描线方向相对 X 轴的夹角（逆时针为正）")
        self._finishRingCombo = self._comboRow([("每层扫轮廓","each"),("最后一层扫轮廓","last")])
        f.addRow("轮廓精修", self._finishRingCombo)
        self._finishRingCombo.setToolTip("平行扫描时每层扫描线扫完后是否走一圈边界轮廓：\n每层扫轮廓=逐层精修边界（层间抬刀衔接）\n最后一层扫轮廓=层间连续蛇形、仅末层走轮廓")
        self._scanModeCombo.currentIndexChanged.connect(self._onScanModeChanged)
        self._tabs.addTab(t, "走刀方式")
        self._regionStepSpin = self._dspin(2.0, 0.01, 999)
        self._regionAllowSpin = self._dspin(0.0, 0.0, 10)
        self._addFeedTab([("侧向步距 mm", self._regionStepSpin),
                          ("精加工余量 mm", self._regionAllowSpin)])
        self._addSafeTab()
        self._addCalcTab([("边界保留", self._comboRow([("不保留","none"),("保留边界","keep"),("精修边界","finish")]))])
        self._onScanModeChanged(self._scanModeCombo.currentIndex())

    def _onScanModeChanged(self, index: int) -> None:
        """扫描角度/轮廓精修仅对平行扫描有效：环切/螺旋时置灰禁用。"""
        spin = getattr(self, "_scanAngleSpin", None)
        combo = getattr(self, "_scanModeCombo", None)
        mode = combo.currentData() if combo else None
        if spin is not None:
            spin.setEnabled(mode == "parallel")
        ring = getattr(self, "_finishRingCombo", None)
        if ring is not None:
            ring.setEnabled(mode == "parallel")

    def _buildRoughRegion(self):
        """分层区域粗加工：走刀(模式+方向+角度) + 进给(含步距+XY余量+Z余量) + 安全 + 高级 + 计算。"""
        t, f = self._formTab()
        self._scanModeCombo = self._comboRow([("平行扫描","parallel"),("环切","spiral"),("螺旋","helical")])
        f.addRow("走刀模式", self._scanModeCombo)
        w, self._dirGroup = self._radioRow(t, [(0,"顺铣"),(1,"逆铣")])
        f.addRow("走刀方向", w)
        self._scanAngleSpin = self._dspin(0.0, 0.0, 360)
        f.addRow("切削角度 °", self._scanAngleSpin)
        self._tabs.addTab(t, "走刀方式")
        self._regionStepSpin = self._stepoverCtl(self._defaultStepover())
        self._regionXyAllowSpin = self._dspin(0.3, 0.0, 10)
        self._regionZAllowSpin = self._dspin(0.2, 0.0, 10)
        self._addFeedTab([("侧向步距 mm", self._regionStepSpin),
                          ("XY 余量 mm", self._regionXyAllowSpin),
                          ("Z 余量 mm", self._regionZAllowSpin)])
        self._addSafeTab()
        t2, f2 = self._formTab()
        self._minStepSpin = self._dspin(1.0, 0.01, 999)
        f2.addRow("最小步距 mm", self._minStepSpin)
        self._maxStepSpin = self._dspin(5.0, 0.01, 999)
        f2.addRow("最大步距 mm", self._maxStepSpin)
        self._smoothChk = QCheckBox("平滑路径")
        f2.addRow("路径优化", self._smoothChk)
        self._tabs.addTab(t2, "高级设置")
        self._addCalcTab()

    def _buildSurfaceFinish(self):
        """曲面精加工：走刀(模式+方向+角度+行距方式) + 进给(行距+残留高度) + 安全(无斜坡) + 高级 + 计算。"""
        t, f = self._formTab()
        self._scanModeCombo = self._comboRow([("平行扫描","parallel"),("环切","spiral"),("螺旋","helical"),("3D等距","iso3d")])
        f.addRow("走刀模式", self._scanModeCombo)
        w, self._dirGroup = self._radioRow(t, [(0,"顺铣"),(1,"逆铣")])
        f.addRow("走刀方向", w)
        self._scanAngleSpin = self._dspin(0.0, 0.0, 360)
        f.addRow("切削角度 °", self._scanAngleSpin)
        w2, self._stepModeGroup = self._radioRow(t, [(0,"指定行距"),(1,"指定残留高度")])
        f.addRow("行距方式", w2)
        self._tabs.addTab(t, "走刀方式")
        t2, f2 = self._formTab()
        self._stepoverSpin = self._stepoverCtl(self._defaultStepover())
        f2.addRow("行距 mm", self._stepoverSpin)
        self._scallopSpin = self._dspin(0.01, 0.001, 1.0)
        f2.addRow("残留高度 mm", self._scallopSpin)
        self._finishAllowSpin = self._dspin(0.0, 0.0, 10)
        f2.addRow("精加工余量 mm", self._finishAllowSpin)
        f2.addRow(self._boldLabel("── 速度参数 ──"), QLabel(""))
        self._rpmSpin = self._ispin(18000, 100, 99999)
        f2.addRow("主轴转速 rpm", self._rpmSpin)
        self._feedXySpin = self._dspin(1200, 1, 99999)
        f2.addRow("进给速度 XY", self._feedXySpin)
        self._slotFeedSpin = self._dspin(600, 1, 99999)
        f2.addRow("开槽进给", self._slotFeedSpin)
        self._feedZSpin = self._dspin(300, 1, 99999)
        f2.addRow("下刀速度 Z", self._feedZSpin)
        self._approachFeedSpin = self._dspin(500, 1, 99999)
        f2.addRow("进刀速度", self._approachFeedSpin)
        self._tabs.addTab(t2, "进给设置")
        self._addSafeTab(withRamp=False)
        t3, f3 = self._formTab()
        self._trimCombo = self._comboRow([("不修剪","none"),("按边界修剪","boundary"),("按曲线修剪","curve")])
        f3.addRow("边界修剪", self._trimCombo)
        self._projCombo = self._comboRow([("Z 轴投影","z"),("法向投影","normal")])
        f3.addRow("投影方向", self._projCombo)
        self._smoothChk = QCheckBox("平滑路径")
        f3.addRow("路径优化", self._smoothChk)
        self._tabs.addTab(t3, "高级设置")
        self._addCalcTab()

    def _buildPathMachine(self):
        """路径加工：走刀(路径类型+方向+偏移) + 进给(行距+余量) + 安全(无斜坡) + 计算。"""
        t, f = self._formTab()
        self._pathTypeCombo = self._comboRow([("沿曲线","along"),("投影曲线","project")])
        f.addRow("路径类型", self._pathTypeCombo)
        w, self._dirGroup = self._radioRow(t, [(0,"顺铣"),(1,"逆铣")])
        f.addRow("走刀方向", w)
        w2, self._offsetGroup = self._radioRow(t, [(0,"外"),(1,"内"),(2,"在线")])
        f.addRow("偏移侧", w2)
        self._tabs.addTab(t, "走刀方式")
        self._pathStepoverSpin = self._stepoverCtl(self._defaultStepover())
        self._pathFinishSpin = self._dspin(0.0, 0.0, 10)
        self._addFeedTab([("行距 mm", self._pathStepoverSpin),
                          ("精加工余量 mm", self._pathFinishSpin)])
        self._addSafeTab(withRamp=False)
        self._addCalcTab()

    def _buildPlaceholder(self):
        """占位。"""
        t, f = self._formTab()
        f.addRow(QLabel("该加工方式开发中"), QLabel(""))
        self._tabs.addTab(t, "提示")

    def _boldLabel(self, text) -> QLabel:
        l = QLabel(text)
        l.setStyleSheet("font-weight:bold;")
        return l

    def _stockText(self) -> str:
        """坯料信息文本（材料+尺寸，从 CncState 读）。"""
        s = self._state.stock
        if s is None:
            return "未设置"
        return f"{self._state.materialType}  {s.width:g}×{s.height:g}×{s.thickness:g} mm"

    def _defaultStepover(self) -> float:
        """进给设置行距默认 = 当前刀具直径的一半。"""
        tc = self._toolCombo.currentData()
        if tc is not None:
            dia = tc.tool.geometry.get("diameter", 0.0)
            if dia > 1e-6:
                return dia / 2.0
        return 1.5

    def _stepoverCtl(self, dflt: float):
        """行距控件：初始值=当前刀径/2，记录用户是否手动改过。"""
        w = self._dspin(dflt, 0.01, 999)
        w._userEdited = False
        w.valueChanged.connect(lambda v, ww=w: setattr(ww, "_userEdited", True))
        return w

    def _refreshTools(self) -> None:
        """填充加工刀具下拉（当前使用刀具）。"""
        self._toolCombo.blockSignals(True)
        self._toolCombo.clear()
        for i, tc in enumerate(self._state.toolControllers, 1):
            t = tc.tool
            dia = t.geometry.get("diameter", 0)
            self._toolCombo.addItem(f"T{i} {t.name} Ø{dia:g}", tc)
        if self._toolCombo.count() > 0:
            self._toolCombo.setCurrentIndex(0)
            self._onToolChanged(0)
        self._toolCombo.blockSignals(False)

    def _refreshGeometry(self) -> None:
        """刷新所有加工数据类型的已选信息。"""
        for tkey, lbl in self._pickLabels.items():
            objs = self._pickedObjects.get(tkey, [])
            if objs:
                lbl.setText(f"已选 {len(objs)} 个")
                lbl.setStyleSheet("color:%s;" % theme.TEXT)
            else:
                lbl.setText("未选择")
                lbl.setStyleSheet("color:%s;" % theme.TEXT_DIM)

    def _onClearPick(self, tkey: str) -> None:
        """清除某类型已选对象：取消画布选中 + 清空记录 + 标签复位。"""
        if self._doc is None:
            return
        for o in self._pickedObjects.get(tkey, []):
            try:
                o.setSelected(False)
            except Exception:
                pass
        self._pickedObjects[tkey] = []
        self._refreshGeometry()

    def _onPickCurves(self, tkey: str) -> None:
        """点击某类型"选取"按钮：隐藏对话框，让用户在画布选对象，选完自动回来。"""
        if self._doc is None:
            return
        self._picking = True
        self._pickingType = tkey
        self._doc.selectionChanged.connect(self._onSelectionChanged)
        self.hide()
        self._pickLabels[tkey].setText(f"请在画布上选取{tkey}…")
        self._pickLabels[tkey].setStyleSheet("color:%s;" % theme.ORANGE)

    def _onSelectionChanged(self) -> None:
        """画布选择变化时，若有选中则存到对应类型并回到对话框。"""
        if not self._picking or self._doc is None:
            return
        sel = self._doc.selectedObjects()
        if sel:
            self._picking = False
            self._doc.selectionChanged.disconnect(self._onSelectionChanged)
            self._pickedObjects[self._pickingType] = list(sel)
            self._refreshGeometry()
            self.show()
            self.raise_()
            self.activateWindow()

    def _onToolChanged(self, idx: int) -> None:
        """切换刀具时从该刀加工参数(按材料存)填充进给设置的速度参数。"""
        if idx < 0:
            return
        tc = self._toolCombo.itemData(idx)
        if tc is None:
            return
        g = tc.tool.geometry
        mat = self._state.materialType
        info = MATERIALS.get(mat, {"hardness": 3.0, "feedRate": 2000})
        feed = float(info["feedRate"])
        hardness = float(info["hardness"])
        defRpm = max(2000, int(18000 - hardness * 30))
        defFeed = feed
        defSlot = feed * 0.5
        defPlunge = feed * 0.3
        defApproach = feed * 0.6
        if hasattr(self, "_rpmSpin"):
            self._rpmSpin.setValue(int(g.get(f"rpm_{mat}", defRpm)))
        if hasattr(self, "_feedXySpin"):
            self._feedXySpin.setValue(float(g.get(f"feed_{mat}", defFeed)))
        if hasattr(self, "_slotFeedSpin"):
            self._slotFeedSpin.setValue(float(g.get(f"slot_feed_{mat}", defSlot)))
        if hasattr(self, "_feedZSpin"):
            self._feedZSpin.setValue(float(g.get(f"plunge_feed_{mat}", defPlunge)))
        if hasattr(self, "_approachFeedSpin"):
            self._approachFeedSpin.setValue(float(g.get(f"approach_feed_{mat}", defApproach)))
        # 行距默认 = 直径一半；用户手动改过的不覆盖
        dia = float(g.get("diameter", 0.0))
        if dia > 1e-6:
            half = dia / 2.0
            for attr in ("_regionStepSpin", "_stepoverSpin", "_pathStepoverSpin"):
                w = getattr(self, attr, None)
                if w is not None and not getattr(w, "_userEdited", False):
                    w.blockSignals(True)
                    w.setValue(half)
                    w.blockSignals(False)

    def _onOk(self) -> None:
        method = self._methodGroup.checkedId()
        if self._doc is None:
            QMessageBox.warning(self, "提示", "无文档")
            return
        if method == 0:
            self._onOkDrill()
        elif method == 1:
            self._onOkBore()
        elif method == 2:
            self._onOkThread()
        elif method == 3:
            self._onOkWireCut()
        elif method == 4:
            self._onOkGroove()
        elif method == 5:
            self._onOkContour()
        elif method == 6:
            self._onOkRegion()
        elif method == 7:
            self._onOkRough3D()
        elif method == 8:
            self._onOkSurfaceFinish()
        elif method == 9:
            self._onOkPathMachine()
        else:
            QMessageBox.information(self, "提示", "该加工方式开发中")
            return

    def _onOkDrill(self) -> None:
        """钻孔：点选取 → DrillParams → build_drill_project → G 代码。"""
        sel = self._pickedObjects.get("点", [])
        if not sel:
            QMessageBox.warning(self, "提示", "钻孔需先在加工区域选\"点\"并选取钻孔点位")
            return

        from core.cnc.adapter import objects_to_entities
        from core.cnc.service import (build_drill_project, generate_profile_gcode,
                                       CncError, DrillParams)
        entities = objects_to_entities(sel)
        entities = [e for e in entities if e.point is not None]
        if not entities:
            QMessageBox.warning(self, "提示", "未取到有效钻孔点（需点对象或整圆）")
            return

        tc = self._toolCombo.currentData()
        tool_num = 1
        if tc is not None:
            for i, c in enumerate(self._state.toolControllers, 1):
                if c is tc:
                    tool_num = i
                    break
        g = tc.tool.geometry if tc else {}

        def _g(name):
            return getattr(self, name, None)
        cycle = _g("_drillModeCombo").currentData() if _g("_drillModeCombo") else "simple"
        dwell = _g("_dwellSpin").value() if _g("_dwellSpin") else 0.0
        rpm = self._rpmSpin.value() if hasattr(self, "_rpmSpin") else 18000
        feed_xy = self._feedXySpin.value() if hasattr(self, "_feedXySpin") else 1200
        feed_z = self._feedZSpin.value() if hasattr(self, "_feedZSpin") else 300
        safe_h = _g("_safeHeightSpin").value() if _g("_safeHeightSpin") else self._state.machine.defaults.safe_height
        clearance = _g("_clearanceSpin").value() if _g("_clearanceSpin") else self._state.machine.defaults.clearance_plane

        p = DrillParams(
            tool_diameter=g.get("diameter", 3.0) if tc else 3.0,
            flute_count=int(g.get("flute_count", 2)) if tc else 2,
            spindle_rpm=rpm,
            feed_xy=feed_xy,
            feed_z=feed_z,
            tool_number=tool_num,
            cut_depth=self._depthEndSpin.value(),
            peck_depth=self._layerStepdown(self._depthEndSpin.value()),
            cycle=cycle,
            dwell_s=dwell,
            controller=self._state.machine.controller,
            safe_height=safe_h,
            clearance_plane=clearance,
        )

        try:
            project = build_drill_project(entities, p)
            gcode, toolpaths = generate_profile_gcode(project)
        except CncError as e:
            QMessageBox.critical(self, "生成失败", str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "生成异常", str(e))
            return

        self._saveEntry(toolpaths, gcode)
        self.accept()

    def _onOkContour(self) -> None:
        """轮廓切割：线选取 → ProfileParams → build_profile_project → G 代码。"""
        sel = self._pickedObjects.get("线", [])
        if not sel:
            QMessageBox.warning(self, "提示", "请先选取加工数据（线）")
            return

        from core.cnc.adapter import objects_to_entities
        from core.cnc.service import (build_profile_project, generate_profile_gcode,
                                       CncError, ProfileParams)
        entities = objects_to_entities(sel)
        if not entities:
            QMessageBox.warning(self, "提示", "所选对象不支持 CNC（需线/弧/圆/曲线）")
            return

        tc = self._toolCombo.currentData()
        tool_num = 1
        if tc is not None:
            for i, c in enumerate(self._state.toolControllers, 1):
                if c is tc:
                    tool_num = i
                    break

        # 从进给设置控件读速度参数（已从刀具默认值填充，用户可修改）
        g = tc.tool.geometry if tc else {}
        rpm = self._rpmSpin.value() if hasattr(self, "_rpmSpin") else 18000
        feed_xy = self._feedXySpin.value() if hasattr(self, "_feedXySpin") else 1200
        feed_z = self._feedZSpin.value() if hasattr(self, "_feedZSpin") else 300

        # 安全读取各控件值（不同加工方式控件集不同）
        def _g(name):
            return getattr(self, name, None)
        og = _g("_offsetGroup")
        offset = ["outside", "inside", "on_line"][og.checkedId()] if og and og.checkedId() is not None else "outside"
        dg = _g("_dirGroup")
        direction = ["climb", "conventional"][dg.checkedId()] if dg and dg.checkedId() is not None else "climb"

        lmg = _g("_layerModeGroup")
        layer_mode = lmg.checkedId() if lmg and lmg.checkedId() is not None else 2
        multi_depth = layer_mode != 2
        # 限定层数时按总深/层数计算每层下刀量；关闭时一次到底
        if layer_mode == 1:
            stepdown = self._layerStepdown(self._depthEndSpin.value())
        elif layer_mode == 0:
            stepdown = _g("_cutDepthSpin").value() if _g("_cutDepthSpin") else 1.0
        else:
            stepdown = abs(self._depthEndSpin.value())

        lead_combo = _g("_leadStyleCombo")
        lead_style = lead_combo.currentData() if lead_combo else "direct"
        lead_len = _g("_leadLenSpin").value() if _g("_leadLenSpin") else 0.0
        ramp_combo = _g("_rampStrategyCombo")
        ramp_strategy = ramp_combo.currentData() if ramp_combo else "plunge"
        ramp_angle = _g("_rampAngleSpin").value() if _g("_rampAngleSpin") else 3.0

        tabs_chk = _g("_tabsEnabledChk")
        tabs_enabled = tabs_chk.isChecked() if tabs_chk else False
        tab_count = _g("_tabCountSpin").value() if _g("_tabCountSpin") else 4
        tab_width = _g("_tabWidthSpin").value() if _g("_tabWidthSpin") else 5.0
        tab_height = _g("_tabHeightSpin").value() if _g("_tabHeightSpin") else 1.5

        safe_h = _g("_safeHeightSpin").value() if _g("_safeHeightSpin") else self._state.machine.defaults.safe_height
        clearance = _g("_clearanceSpin").value() if _g("_clearanceSpin") else self._state.machine.defaults.clearance_plane
        chord_tol = _g("_chordTolSpin").value() if _g("_chordTolSpin") else 0.02

        p = ProfileParams(
            tool_diameter=g.get("diameter", 3.0) if tc else 3.0,
            flute_count=int(g.get("flute_count", 2)) if tc else 2,
            spindle_rpm=rpm,
            feed_xy=feed_xy,
            feed_z=feed_z,
            tool_number=tool_num,
            offset_side=offset,
            direction=direction,
            cut_depth=self._depthEndSpin.value(),
            stepdown=stepdown,
            multi_depth=multi_depth,
            lead_style=lead_style,
            lead_length=lead_len,
            ramp_strategy=ramp_strategy,
            ramp_angle_deg=ramp_angle,
            tabs_enabled=tabs_enabled,
            tab_count=tab_count,
            tab_width=tab_width,
            tab_height=tab_height,
            controller=self._state.machine.controller,
            safe_height=safe_h,
            clearance_plane=clearance,
            chord_tolerance=chord_tol,
        )

        try:
            project = build_profile_project(entities, p)
            gcode, toolpaths = generate_profile_gcode(project)
        except CncError as e:
            QMessageBox.critical(self, "生成失败", str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "生成异常", str(e))
            return

        self._saveEntry(toolpaths, gcode)
        self.accept()

    # ----- 3 轴：分层区域粗加工 / 曲面精加工 / 路径加工 -----
    def _g(self, name):
        """安全读取控件值（不同加工方式控件集不同）。"""
        return getattr(self, name, None)

    def _pickedBoundary(self):
        """同时选了闭合曲线（线）时，转为加工边界多边形（首尾闭合）。

        从"线"桶收集；若用户把边界曲线选进了"几何体"桶（与模型一起多选），
        也把其中的曲线类对象一并收集。adapter 转换失败时按对象类型直接
        采样兜底，保证 圆/椭圆/弧/闭合折线/样条 都能作为边界。
        返回 (boundary, 提示) 或 (None, None)。
        """
        from core.vector.geometry_types import ObjectType
        lines = list(self._pickedObjects.get("线", []))
        for o in self._pickedObjects.get("几何体", []):
            try:
                t = o.type()
            except Exception:
                continue
            if t in (ObjectType.CIRCLE, ObjectType.ELLIPSE, ObjectType.ARC,
                     ObjectType.LINE, ObjectType.CURVE,
                     ObjectType.NURBS_CURVE, ObjectType.POLYCURVE):
                lines.append(o)
        if not lines:
            return None, None
        # 1) adapter 转换（逐对象容错）
        from core.cnc.adapter import object_to_entity
        from core.cnc.service import _polyline_points
        ents = []
        for o in lines:
            try:
                e = object_to_entity(o)
                if e is not None:
                    ents.append(e)
            except Exception:
                continue
        pts = None
        if ents:
            try:
                pts = _polyline_points(ents)
            except Exception:
                pts = None
        # 2) 兜底：直接按对象几何采样
        if not pts or len(pts) < 3:
            pts = self._boundaryDirect(lines)
        if not pts or len(pts) < 3:
            return None, "所选边界曲线无法转为闭合多边形（请用 圆/椭圆/弧/闭合折线）"
        boundary = [(float(x), float(y)) for x, y in pts]
        dx = boundary[0][0] - boundary[-1][0]
        dy = boundary[0][1] - boundary[-1][1]
        if dx * dx + dy * dy > 1e-6:
            boundary.append(boundary[0])
        return boundary, None

    def _boundaryDirect(self, objs):
        """adapter 失败时的兜底：按对象类型直接采样闭合环。"""
        from core.vector.geometry_types import ObjectType
        import math as _m
        out: list = []
        for o in objs:
            try:
                t = o.type()
                if t == ObjectType.CIRCLE:
                    c = o.center()
                    r = o.radius()
                    for i in range(65):
                        a = 2.0 * _m.pi * i / 64.0
                        out.append((float(c.x) + r * _m.cos(a),
                                    float(c.y) + r * _m.sin(a)))
                elif t == ObjectType.ELLIPSE:
                    c = o.center()
                    a = o.majorAxis()
                    b = o.minorAxis()
                    rot = o.rotation()
                    cr, sr = _m.cos(rot), _m.sin(rot)
                    for i in range(129):
                        tt = 2.0 * _m.pi * i / 128.0
                        ct, st = _m.cos(tt), _m.sin(tt)
                        out.append((float(c.x) + a * ct * cr - b * st * sr,
                                    float(c.y) + a * ct * sr + b * st * cr))
                elif t == ObjectType.ARC:
                    c = o.center()
                    r = o.radius()
                    sa = o.startAngle()
                    ea = o.endAngle()
                    n = max(8, int(abs(ea - sa) * r / 0.4))
                    for i in range(n + 1):
                        aa = sa + (ea - sa) * i / max(1, n)
                        out.append((float(c.x) + r * _m.cos(aa),
                                    float(c.y) + r * _m.sin(aa)))
                elif t == ObjectType.LINE:
                    pps = o.points()
                    for p in pps:
                        out.append((float(p.x), float(p.y)))
                    if o.isClosed() and pps:
                        out.append((float(pps[0].x), float(pps[0].y)))
                elif t in (ObjectType.CURVE, ObjectType.NURBS_CURVE,
                           ObjectType.POLYCURVE):
                    try:
                        sp = o.getSampledPoints(64)
                        for p in sp:
                            out.append((float(p[0]), float(p[1])))
                        if o.isClosed() and sp:
                            out.append((float(sp[0][0]), float(sp[0][1])))
                    except Exception:
                        continue
            except Exception:
                continue
            if len(out) >= 3:
                break
        return out or None

    def _toolNum(self) -> int:
        tc = self._toolCombo.currentData()
        if tc is None:
            return 1
        for i, c in enumerate(self._state.toolControllers, 1):
            if c is tc:
                return i
        return 1

    def _makeProgressDlg(self, label: str = "正在计算刀路…"):
        """生成刀路计算进度对话框（定制样式：加大尺寸 / 加大取消按钮）。

        立即显示并刷新一次（等价原 QProgressDialog 的 minimumDuration(0)；
        普通 QDialog 不会自动显示，必须显式 show）。
        """
        d = _CalcProgressDialog(label, self)
        d.show()
        QApplication.processEvents()
        return d

    def _onOkRough3D(self) -> None:
        """分层区域粗加工：选几何体(3D 模型) → 水线分层 + 每层填充。"""
        objs = self._pickedObjects.get("几何体", [])
        if not objs:
            QMessageBox.warning(self, "提示",
                                "分层区域粗加工需先选取\"几何体\"（CNC 空间导入的 3D 模型）")
            return
        from core.cnc.service3d import (collect_model_mesh, Rough3DParams,
                                        build_rough3d_toolpath, generate_3d_gcode)
        mesh = collect_model_mesh(objs)
        if mesh is None:
            QMessageBox.warning(self, "提示", "所选对象无法生成网格（需 BREP 实体 / 浮雕模型）")
            return
        tc = self._toolCombo.currentData()
        g = tc.tool.geometry if tc else {}
        stock_top = None
        if self._state.stock is not None:
            stock_top = self._state.stock.thickness / 2.0
        # 3D 粗加工深度由模型本身决定：总深 = 模型表面到模型底 + Z余量
        z_allow = (self._regionZAllowSpin.value()
                   if hasattr(self, "_regionZAllowSpin") else 0.2)
        if stock_top is not None:
            z_top = min(float(stock_top), mesh["zmax"] + z_allow)
        else:
            z_top = mesh["zmax"] + z_allow
        auto_depth = z_top - (mesh["zmin"] + z_allow)
        lmg = self._g("_layerModeGroup")
        layer_mode = lmg.checkedId() if lmg and lmg.checkedId() is not None else 0
        if layer_mode == 2:
            # 分层区域粗加工禁止"关闭分层"：一刀切到总深会断刀
            # （深模型逐层下才安全），强制"限定深度"逐层下。
            layer_mode = 0
        multi_depth = layer_mode != 2
        if layer_mode == 2:
            stepdown = (self._g("_cutDepthSpin").value()
                        if self._g("_cutDepthSpin") else 1.0)
        else:
            stepdown = self._layerStepdown(auto_depth)
        sc = self._g("_scanModeCombo")
        scan_mode = sc.currentData() if sc else "offset"
        dg = self._g("_dirGroup")
        direction = ["climb", "conventional"][dg.checkedId()] \
            if dg and dg.checkedId() is not None else "climb"
        p = Rough3DParams(
            tool_diameter=g.get("diameter", 3.0) if tc else 3.0,
            flute_count=int(g.get("flute_count", 2)) if tc else 2,
            spindle_rpm=self._rpmSpin.value() if hasattr(self, "_rpmSpin") else 18000,
            feed_xy=self._feedXySpin.value() if hasattr(self, "_feedXySpin") else 1200,
            feed_z=self._feedZSpin.value() if hasattr(self, "_feedZSpin") else 300,
            tool_number=self._toolNum(),
            scan_mode=scan_mode,
            direction=direction,
            scan_angle=self._scanAngleSpin.value() if hasattr(self, "_scanAngleSpin") else 0.0,
            stepover=self._regionStepSpin.value() if hasattr(self, "_regionStepSpin") else 3.0,
            xy_allowance=self._regionXyAllowSpin.value() if hasattr(self, "_regionXyAllowSpin") else 0.3,
            z_allowance=z_allow,
            cut_depth=-auto_depth,
            stepdown=stepdown,
            multi_depth=multi_depth,
            min_step=self._minStepSpin.value() if hasattr(self, "_minStepSpin") else 1.0,
            max_step=self._maxStepSpin.value() if hasattr(self, "_maxStepSpin") else 5.0,
            smooth=bool(self._smoothChk.isChecked()) if hasattr(self, "_smoothChk") else False,
            controller=self._state.machine.controller,
            safe_height=self._safeHeightSpin.value() if self._g("_safeHeightSpin") else self._state.machine.defaults.safe_height,
            clearance_plane=self._clearanceSpin.value() if self._g("_clearanceSpin") else self._state.machine.defaults.clearance_plane,
            chord_tolerance=self._chordTolSpin.value() if self._g("_chordTolSpin") else 0.02,
        )
        boundary, bmsg = self._pickedBoundary()
        if bmsg:
            QMessageBox.warning(self, "提示", bmsg)
            return
        prog = self._makeProgressDlg()
        cancel = {"v": False}
        prog.canceled.connect(lambda: cancel.__setitem__("v", True))

        def _cb(frac):
            prog.setValue(int(frac * 1000))
            QApplication.processEvents()
            return not cancel["v"]

        try:
            sw = sh = None
            if self._state.stock is not None:
                sw = float(self._state.stock.width)
                sh = float(self._state.stock.height)
            tps = build_rough3d_toolpath(mesh, p, stock_top=stock_top,
                                         boundary=boundary, progress=_cb,
                                         stock_w=sw, stock_h=sh)
            if tps is None:
                prog.close()
                return  # 已取消
            if not tps or not tps[0].instructions:
                prog.close()
                QMessageBox.warning(self, "提示", "未生成有效刀路（毛坯顶到目标深度之间无模型材料？）")
                return
            gcode, toolpaths = generate_3d_gcode(tps, p)
            prog.close()
        except Exception as e:
            QMessageBox.critical(self, "生成异常", str(e))
            return
        self._saveEntry(toolpaths, gcode)
        self.accept()

    def _onOkSurfaceFinish(self) -> None:
        """曲面精加工：选几何体 → 平行扫描 / 水线精加工。"""
        objs = self._pickedObjects.get("几何体", [])
        if not objs:
            QMessageBox.warning(self, "提示",
                                "曲面精加工需先选取\"几何体\"（CNC 空间导入的 3D 模型）")
            return
        from core.cnc.service3d import (collect_model_mesh, SurfaceFinishParams,
                                        build_surface_finish_toolpath, generate_3d_gcode)
        mesh = collect_model_mesh(objs)
        if mesh is None:
            QMessageBox.warning(self, "提示", "所选对象无法生成网格（需 BREP 实体 / 浮雕模型）")
            return
        tc = self._toolCombo.currentData()
        g = tc.tool.geometry if tc else {}
        sc = self._g("_scanModeCombo")
        scan_mode = sc.currentData() if sc else "parallel"
        dg = self._g("_dirGroup")
        direction = ["climb", "conventional"][dg.checkedId()] \
            if dg and dg.checkedId() is not None else "climb"
        smg = self._g("_stepModeGroup")
        step_mode = ["stepover", "scallop"][smg.checkedId()] \
            if smg and smg.checkedId() is not None else "stepover"
        p = SurfaceFinishParams(
            tool_diameter=g.get("diameter", 3.0) if tc else 3.0,
            flute_count=int(g.get("flute_count", 2)) if tc else 2,
            spindle_rpm=self._rpmSpin.value() if hasattr(self, "_rpmSpin") else 18000,
            feed_xy=self._feedXySpin.value() if hasattr(self, "_feedXySpin") else 1200,
            feed_z=self._feedZSpin.value() if hasattr(self, "_feedZSpin") else 300,
            tool_number=self._toolNum(),
            scan_mode=scan_mode,
            direction=direction,
            scan_angle=self._scanAngleSpin.value() if hasattr(self, "_scanAngleSpin") else 0.0,
            step_mode=step_mode,
            stepover=self._stepoverSpin.value() if hasattr(self, "_stepoverSpin") else 0.5,
            scallop_height=self._scallopSpin.value() if hasattr(self, "_scallopSpin") else 0.01,
            finish_allowance=self._finishAllowSpin.value() if hasattr(self, "_finishAllowSpin") else 0.0,
            proj=(self._projCombo.currentData() if self._g("_projCombo") else "z"),
            trim=(self._trimCombo.currentData() if self._g("_trimCombo") else "none"),
            smooth=bool(self._smoothChk.isChecked()) if hasattr(self, "_smoothChk") else False,
            controller=self._state.machine.controller,
            safe_height=self._safeHeightSpin.value() if self._g("_safeHeightSpin") else self._state.machine.defaults.safe_height,
            clearance_plane=self._clearanceSpin.value() if self._g("_clearanceSpin") else self._state.machine.defaults.clearance_plane,
            chord_tolerance=self._chordTolSpin.value() if self._g("_chordTolSpin") else 0.02,
        )
        boundary, bmsg = self._pickedBoundary()
        if bmsg:
            QMessageBox.warning(self, "提示", bmsg)
            return
        prog = self._makeProgressDlg()
        cancel = {"v": False}
        prog.canceled.connect(lambda: cancel.__setitem__("v", True))

        def _cb(frac):
            prog.setValue(int(frac * 1000))
            QApplication.processEvents()
            return not cancel["v"]

        try:
            tps = build_surface_finish_toolpath(mesh, p, boundary=boundary,
                                                progress=_cb)
            if tps is None:
                return  # 已取消
            if not tps or not tps[0].instructions:
                QMessageBox.warning(self, "提示", "未生成有效刀路")
                return
            gcode, toolpaths = generate_3d_gcode(tps, p)
            prog.close()
        except Exception as e:
            QMessageBox.critical(self, "生成异常", str(e))
            return
        self._saveEntry(toolpaths, gcode)
        self.accept()

    def _onOkPathMachine(self) -> None:
        """路径加工：沿曲线 / 投影曲线（曲面需选几何体）。"""
        curves = self._pickedObjects.get("线", [])
        if not curves:
            QMessageBox.warning(self, "提示", "路径加工需先选取\"线\"（加工曲线）")
            return
        from core.cnc.adapter import objects_to_entities
        from core.cnc.service import _polylines_from_entities
        entities = objects_to_entities(curves)
        # 断点分段：不连续曲线各自走刀，不再强行首尾相接连刀切伤工件
        chains = _polylines_from_entities(entities) if entities else []
        if not chains:
            QMessageBox.warning(self, "提示", "所选曲线无法转为连续折线")
            return
        # 可选曲面（投影模式需要）
        mesh = None
        objs = self._pickedObjects.get("几何体", [])
        if objs:
            from core.cnc.service3d import collect_model_mesh
            mesh = collect_model_mesh(objs)
        ptc = self._g("_pathTypeCombo")
        path_type = ptc.currentData() if ptc else "along"
        if path_type == "project" and mesh is None:
            QMessageBox.warning(self, "提示", "投影曲线需同时选取\"几何体\"作为投影曲面")
            return
        tc = self._toolCombo.currentData()
        g = tc.tool.geometry if tc else {}
        dg = self._g("_dirGroup")
        direction = ["climb", "conventional"][dg.checkedId()] \
            if dg and dg.checkedId() is not None else "climb"
        og = self._g("_offsetGroup")
        offset_side = ["outside", "inside", "on_line"][og.checkedId()] \
            if og and og.checkedId() is not None else "on_line"
        lmg = self._g("_layerModeGroup")
        layer_mode = lmg.checkedId() if lmg and lmg.checkedId() is not None else 0
        multi_depth = layer_mode != 2
        stepdown = self._layerStepdown(self._depthEndSpin.value())
        p = PathMachineParams(
            tool_diameter=g.get("diameter", 3.0) if tc else 3.0,
            flute_count=int(g.get("flute_count", 2)) if tc else 2,
            spindle_rpm=self._rpmSpin.value() if hasattr(self, "_rpmSpin") else 18000,
            feed_xy=self._feedXySpin.value() if hasattr(self, "_feedXySpin") else 1200,
            feed_z=self._feedZSpin.value() if hasattr(self, "_feedZSpin") else 300,
            tool_number=self._toolNum(),
            path_type=path_type,
            direction=direction,
            offset_side=offset_side,
            stepover=self._pathStepoverSpin.value() if hasattr(self, "_pathStepoverSpin") else 0.5,
            finish_allowance=self._pathFinishSpin.value() if hasattr(self, "_pathFinishSpin") else 0.0,
            cut_depth=self._depthEndSpin.value(),
            stepdown=stepdown,
            multi_depth=multi_depth,
            controller=self._state.machine.controller,
            safe_height=self._safeHeightSpin.value() if self._g("_safeHeightSpin") else self._state.machine.defaults.safe_height,
            clearance_plane=self._clearanceSpin.value() if self._g("_clearanceSpin") else self._state.machine.defaults.clearance_plane,
            chord_tolerance=self._chordTolSpin.value() if self._g("_chordTolSpin") else 0.02,
        )
        try:
            from core.cnc.service3d import (build_path_machine_toolpath,
                                            generate_3d_gcode)
            prog = self._makeProgressDlg()
            cancel = {"v": False}
            prog.canceled.connect(lambda: cancel.__setitem__("v", True))

            def _cb(frac):
                prog.setValue(int(frac * 1000))
                QApplication.processEvents()
                return not cancel["v"]

            tps = []
            for pts in chains:
                t = build_path_machine_toolpath(mesh, pts, p, progress=_cb)
                if t is None:
                    return  # 已取消
                tps.extend(t if isinstance(t, list) else [t])
            if not tps or not tps[0].instructions:
                prog.close()
                QMessageBox.warning(self, "提示", "未生成有效刀路")
                return
            gcode, toolpaths = generate_3d_gcode(tps, p)
            prog.close()
        except Exception as e:
            QMessageBox.critical(self, "生成异常", str(e))
            return
        self._saveEntry(toolpaths, gcode)
        self.accept()

    def closeEvent(self, event) -> None:
        """关闭时断开 selectionChanged 信号，避免泄漏。"""
        if self._picking and self._doc is not None:
            try:
                self._doc.selectionChanged.disconnect(self._onSelectionChanged)
            except (TypeError, RuntimeError):
                pass
            self._picking = False
        super().closeEvent(event)

    # ----- 2 轴：扩孔 / 铣螺纹 / 单线切割 / 单线摆槽 / 区域加工 保存 -----
    def _toolNum(self) -> int:
        tc = self._toolCombo.currentData()
        if tc is None:
            return 1
        for i, c in enumerate(self._state.toolControllers, 1):
            if c is tc:
                return i
        return 1

    def _baseFeed(self):
        g = self._toolCombo.currentData().tool.geometry if self._toolCombo.currentData() else {}
        return dict(
            rpm=self._rpmSpin.value() if hasattr(self, "_rpmSpin") else 18000,
            feed_xy=self._feedXySpin.value() if hasattr(self, "_feedXySpin") else 1200,
            feed_z=self._feedZSpin.value() if hasattr(self, "_feedZSpin") else 300,
            tool_diameter=g.get("diameter", 3.0) if g else 3.0,
            flute_count=int(g.get("flute_count", 2)) if g else 2,
        )

    def _safeParams(self):
        def _g(n):
            return getattr(self, n, None)
        return dict(
            safe_height=_g("_safeHeightSpin").value() if _g("_safeHeightSpin") else self._state.machine.defaults.safe_height,
            clearance_plane=_g("_clearanceSpin").value() if _g("_clearanceSpin") else self._state.machine.defaults.clearance_plane,
            chord_tolerance=_g("_chordTolSpin").value() if _g("_chordTolSpin") else 0.02,
        )

    def _onOkBore(self) -> None:
        """扩孔：点/圆取孔心 → BoreParams → 螺旋/圆周分层 IR → G 代码。"""
        sel = self._pickedObjects.get("点", []) or self._pickedObjects.get("线", [])
        if not sel:
            QMessageBox.warning(self, "提示", "扩孔需先在加工区域选取点或圆")
            return
        from core.cnc.adapter import objects_to_entities
        from core.cnc.service import (BoreParams, build_bore_toolpath,
                                       generate_ir_gcode, CncError,
                                       _point_center)
        entities = [e for e in objects_to_entities(sel) if _point_center(e) is not None]
        if not entities:
            QMessageBox.warning(self, "提示", "未取到有效孔心（需点对象或整圆）")
            return
        center = _point_center(entities[0])
        bf = self._baseFeed()
        sp = self._safeParams()
        dg = getattr(self, "_dirGroup", None)
        mode = getattr(self, "_expandModeCombo", None)
        p = BoreParams(
            tool_diameter=bf["tool_diameter"], flute_count=bf["flute_count"],
            spindle_rpm=bf["rpm"], feed_xy=bf["feed_xy"], feed_z=bf["feed_z"],
            tool_number=self._toolNum(),
            bore_diameter=self._boreDiaSpin.value() if hasattr(self, "_boreDiaSpin") else 6.0,
            direction="climb" if (dg and dg.checkedId() == 0) else "conventional",
            mode=mode.currentData() if mode else "helical",
            cut_depth=self._depthEndSpin.value(),
            stepdown=self._layerStepdown(self._depthEndSpin.value()),
            multi_depth=True,
            finish_allowance=self._finishAllowanceSpin.value() if hasattr(self, "_finishAllowanceSpin") else 0.2,
            ramp_strategy=(getattr(self, "_rampStrategyCombo", None)
                           .currentData() if getattr(self, "_rampStrategyCombo", None) else "plunge"),
            ramp_angle_deg=(getattr(self, "_rampAngleSpin", None)
                            .value() if getattr(self, "_rampAngleSpin", None) else 3.0),
            controller=self._state.machine.controller, **sp,
        )
        try:
            tp = build_bore_toolpath(center, p)
            gcode, toolpaths = generate_ir_gcode([tp], p)
        except CncError as e:
            QMessageBox.critical(self, "生成失败", str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "生成异常", str(e))
            return
        self._saveEntry(toolpaths, gcode)
        self.accept()

    def _onOkThread(self) -> None:
        """铣螺纹：点/圆取孔心 → ThreadParams → 螺旋插补 IR → G 代码。"""
        sel = self._pickedObjects.get("点", []) or self._pickedObjects.get("线", [])
        if not sel:
            QMessageBox.warning(self, "提示", "铣螺纹需先在加工区域选取点或圆")
            return
        from core.cnc.adapter import objects_to_entities
        from core.cnc.service import (ThreadParams, build_thread_toolpath,
                                       generate_ir_gcode, CncError, _point_center)
        entities = [e for e in objects_to_entities(sel) if _point_center(e) is not None]
        if not entities:
            QMessageBox.warning(self, "提示", "未取到有效孔心（需点对象或整圆）")
            return
        center = _point_center(entities[0])
        bf = self._baseFeed()
        sp = self._safeParams()
        dg = getattr(self, "_dirGroup", None)
        p = ThreadParams(
            tool_diameter=bf["tool_diameter"], flute_count=bf["flute_count"],
            spindle_rpm=bf["rpm"], feed_xy=bf["feed_xy"], feed_z=bf["feed_z"],
            tool_number=self._toolNum(),
            direction="right" if (dg and dg.checkedId() == 0) else "left",
            pitch=self._pitchSpin.value(),
            thread_diameter=self._threadDiaSpin.value(),
            thread_type=self._threadTypeCombo.currentData() if hasattr(self, "_threadTypeCombo") else "internal",
            passes=self._threadPassSpin.value() if hasattr(self, "_threadPassSpin") else 2,
            cut_depth=self._depthEndSpin.value(),
            controller=self._state.machine.controller, **sp,
        )
        try:
            tp = build_thread_toolpath(center, p)
            gcode, toolpaths = generate_ir_gcode([tp], p)
        except CncError as e:
            QMessageBox.critical(self, "生成失败", str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "生成异常", str(e))
            return
        self._saveEntry(toolpaths, gcode)
        self.accept()

    def _onOkWireCut(self) -> None:
        """单线切割：线实体 → WireParams → 偏移折线分层 IR → G 代码。"""
        sel = self._pickedObjects.get("线", [])
        if not sel:
            QMessageBox.warning(self, "提示", "单线切割需先在加工区域选取线")
            return
        from core.cnc.adapter import objects_to_entities
        from core.cnc.service import (WireParams, build_wire_toolpath,
                                       generate_ir_gcode, CncError,
                                       _polylines_from_entities)
        entities = objects_to_entities(sel)
        # 断点分段：不连续线各自走刀，不强行连刀
        chains = _polylines_from_entities(entities)
        if not chains:
            QMessageBox.warning(self, "提示", "未取到有效线段")
            return
        bf = self._baseFeed()
        sp = self._safeParams()
        og = getattr(self, "_offsetGroup", None)
        dg = getattr(self, "_dirGroup", None)
        p = WireParams(
            tool_diameter=bf["tool_diameter"], flute_count=bf["flute_count"],
            spindle_rpm=bf["rpm"], feed_xy=bf["feed_xy"], feed_z=bf["feed_z"],
            tool_number=self._toolNum(),
            offset_side=["outside", "inside", "on_line"][og.checkedId()] if og and og.checkedId() is not None else "on_line",
            direction="climb" if (dg and dg.checkedId() == 0) else "conventional",
            passes=self._passCountSpin.value() if hasattr(self, "_passCountSpin") else 1,
            cut_depth=self._depthEndSpin.value(),
            stepdown=self._layerStepdown(self._depthEndSpin.value()),
            multi_depth=True,
            ramp_strategy=(getattr(self, "_rampStrategyCombo", None)
                           .currentData() if getattr(self, "_rampStrategyCombo", None) else "plunge"),
            ramp_peck_depth=(getattr(self, "_rampPeckSpin", None)
                             .value() if getattr(self, "_rampPeckSpin", None) else 1.0),
            controller=self._state.machine.controller, **sp,
        )
        try:
            tps = []
            for pts in chains:
                tp = build_wire_toolpath(pts, p)
                if tp:
                    tps.extend(tp if isinstance(tp, list) else [tp])
            if not tps:
                QMessageBox.warning(self, "提示", "未生成有效刀路")
                return
            gcode, toolpaths = generate_ir_gcode(tps, p)
        except CncError as e:
            QMessageBox.critical(self, "生成失败", str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "生成异常", str(e))
            return
        self._saveEntry(toolpaths, gcode)
        self.accept()

    def _onOkGroove(self) -> None:
        """单线摆槽：线实体 → GrooveParams → 横向蛇形多道 IR → G 代码。"""
        sel = self._pickedObjects.get("线", [])
        if not sel:
            QMessageBox.warning(self, "提示", "单线摆槽需先在加工区域选取线")
            return
        from core.cnc.adapter import objects_to_entities
        from core.cnc.service import (GrooveParams, build_groove_toolpath,
                                       generate_ir_gcode, CncError,
                                       _polylines_from_entities)
        entities = objects_to_entities(sel)
        # 断点分段：不连续线各自走刀，不强行连刀
        chains = _polylines_from_entities(entities)
        if not chains:
            QMessageBox.warning(self, "提示", "未取到有效线段")
            return
        bf = self._baseFeed()
        sp = self._safeParams()
        og = getattr(self, "_offsetGroup", None)
        dg = getattr(self, "_dirGroup", None)
        p = GrooveParams(
            tool_diameter=bf["tool_diameter"], flute_count=bf["flute_count"],
            spindle_rpm=bf["rpm"], feed_xy=bf["feed_xy"], feed_z=bf["feed_z"],
            tool_number=self._toolNum(),
            offset_side=["outside", "inside", "on_line"][og.checkedId()] if og and og.checkedId() is not None else "on_line",
            direction="climb" if (dg and dg.checkedId() == 0) else "conventional",
            groove_width=self._grooveWidthSpin.value(),
            passes=self._groovePassSpin.value(),
            stepover=self._grooveStepSpin.value() if hasattr(self, "_grooveStepSpin") else 2.0,
            cut_depth=self._depthEndSpin.value(),
            stepdown=self._layerStepdown(self._depthEndSpin.value()),
            multi_depth=True,
            controller=self._state.machine.controller, **sp,
        )
        try:
            tps = []
            for pts in chains:
                tp = build_groove_toolpath(pts, p)
                if tp:
                    tps.extend(tp if isinstance(tp, list) else [tp])
            if not tps:
                QMessageBox.warning(self, "提示", "未生成有效刀路")
                return
            gcode, toolpaths = generate_ir_gcode(tps, p)
        except CncError as e:
            QMessageBox.critical(self, "生成失败", str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "生成异常", str(e))
            return
        self._saveEntry(toolpaths, gcode)
        self.accept()

    def _onOkRegion(self) -> None:
        """区域加工：面/闭合线 → PocketOp（平行/环切/螺旋）→ G 代码。"""
        sel = self._pickedObjects.get("面", []) or self._pickedObjects.get("线", [])
        if not sel:
            QMessageBox.warning(self, "提示", "区域加工需先在加工区域选取封闭区域（面或闭合线）")
            return
        from core.cnc.adapter import objects_to_entities
        from core.cnc.service import (RegionParams, build_pocket_project,
                                       generate_profile_gcode, CncError)
        entities = objects_to_entities(sel)
        bf = self._baseFeed()
        sp = self._safeParams()
        dg = getattr(self, "_dirGroup", None)
        p = RegionParams(
            tool_diameter=bf["tool_diameter"], flute_count=bf["flute_count"],
            spindle_rpm=bf["rpm"], feed_xy=bf["feed_xy"], feed_z=bf["feed_z"],
            tool_number=self._toolNum(),
            scan_mode=self._scanModeCombo.currentData() if hasattr(self, "_scanModeCombo") else "offset",
            direction="climb" if (dg and dg.checkedId() == 0) else "conventional",
            scan_angle=self._scanAngleSpin.value() if hasattr(self, "_scanAngleSpin") else 0.0,
            stepover=self._regionStepSpin.value() if hasattr(self, "_regionStepSpin") else 2.0,
            finish_rings=self._finishRingCombo.currentData() if hasattr(self, "_finishRingCombo") else "each",
            cut_depth=self._depthEndSpin.value(),
            stepdown=self._layerStepdown(self._depthEndSpin.value()),
            multi_depth=True,
            controller=self._state.machine.controller, **sp,
        )
        try:
            project = build_pocket_project(entities, p)
            gcode, toolpaths = generate_profile_gcode(project)
        except CncError as e:
            QMessageBox.critical(self, "生成失败", str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "生成异常", str(e))
            return
        self._saveEntry(toolpaths, gcode)
        self.accept()

    # ----- 控件工厂 -----
    def _dspin(self, v, lo, hi):
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setDecimals(3)
        s.setValue(float(v))
        return s

    def _ispin(self, v, lo, hi):
        s = QSpinBox()
        s.setRange(lo, hi)
        s.setValue(int(v))
        return s

    def _primaryBtn(self, text):
        b = QPushButton(text)
        b.setStyleSheet(
            "QPushButton { background:%s; color:%s; border:none;"
            " padding:6px 16px; border-radius:3px; font-weight:bold; }"
            % (theme.PRIMARY, theme.TEXT_ON_PRIMARY))
        return b

    def _secondaryBtn(self, text):
        b = QPushButton(text)
        b.setStyleSheet(
            "QPushButton { background:#fff; color:%s; border:1px solid %s;"
            " padding:6px 16px; border-radius:3px; }" % (theme.TEXT, theme.BORDER))
        return b
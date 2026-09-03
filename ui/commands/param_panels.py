"""命令参数面板：基类 + 9 个绘图/编辑命令的面板。

面板由命令实例懒创建（CommandInstance.paramPanel()），命令激活时
被 CommandParamHost 挂到画布右侧；面板信号连回命令槽，数值修改
实时更新命令状态与画布预览。
"""
from __future__ import annotations
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
                               QGridLayout, QDoubleSpinBox, QSpinBox,
                               QPushButton, QCheckBox, QRadioButton,
                               QButtonGroup, QLabel, QGroupBox, QFrame,
                               QComboBox)

import ui.styles.theme as theme
from core.relief import HeightField
from core.relief.region_relief import (RegionReliefParams, COLOR_SCOPES,
                                       COLOR_SCOPE_LABELS)

_SPIN_RANGE = (-999999.0, 999999.0)
_LABEL_W = 52      # 行标签统一最小宽：容纳"指定角度"4 字不截断


class _ClickLabel(QLabel):
    """可点击的行标签（原版矩形面板长/宽标题点击联动）。"""
    clicked = Signal()

    def __init__(self, text: str = "", tip: str = "") -> None:
        super().__init__(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if tip:
            self.setToolTip(tip)
        self.setStyleSheet("color:%s; text-decoration:underline;" % theme.PRIMARY_DARK)

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(ev)


class ParamPanelBase(QWidget):
    """所有命令参数面板的公共基类：样式与控件工厂。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # QWidget 需开启 WA_StyledBackground 才会绘制样式表背景，否则面板无底色无边框
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "QWidget#ParamPanel { background:#ffffff; border:1px solid %s;"
            " border-radius:4px; }" % theme.BORDER)
        self.setObjectName("ParamPanel")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(6, 2, 6, 6)
        self._layout.setSpacing(5)
        self._spinX = self._spinY = self._spinZ = None

    # ---------- 控件工厂 ----------

    def _spin(self, value: float = 0.0, decimals: int = 2,
              step: float = 1.0) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(*_SPIN_RANGE)
        s.setDecimals(decimals)
        s.setSingleStep(step)
        s.setValue(value)
        return s

    def _ispin(self, value: int = 0, lo: int = 0, hi: int = 100) -> QSpinBox:
        s = QSpinBox()
        s.setRange(lo, hi)
        s.setValue(value)
        return s

    def _button(self, text: str, primary: bool = False) -> QPushButton:
        b = QPushButton(text)
        if primary:
            b.setStyleSheet(
                "QPushButton { background:%s; color:%s; padding:4px 10px; }"
                "QPushButton:hover { background:%s; }"
                % (theme.PRIMARY, theme.TEXT_ON_PRIMARY, theme.PRIMARY_DARK))
        return b

    def _button2(self, text: str) -> QPushButton:
        """次级按钮：白底描边，弱化视觉层级，与主按钮区分。"""
        b = QPushButton(text)
        b.setStyleSheet(
            "QPushButton { background:%s; color:%s; border:1px solid %s;"
            " border-radius:2px; padding:3px 8px; }"
            "QPushButton:hover { border-color:%s; color:%s; }"
            % (theme.BG_PANEL, theme.TEXT, theme.BORDER,
               theme.PRIMARY, theme.PRIMARY_DARK))
        return b

    def _label(self, text: str) -> QLabel:
        return QLabel(text)

    def _addRow(self, label: str, widget: QWidget) -> None:
        row = QHBoxLayout()
        row.setSpacing(4)
        lab = QLabel(label)
        lab.setMinimumWidth(_LABEL_W)
        row.addWidget(lab)
        row.addWidget(widget, 1)
        self._layout.addLayout(row)

    def _addCoordRows(self, withZ: bool = False) -> None:
        """X/Y/(Z) 坐标输入行，供画布取点类命令精确输入坐标。"""
        self._spinX = self._spin()
        self._spinY = self._spin()
        self._addRow("X", self._spinX)
        self._addRow("Y", self._spinY)
        if withZ:
            self._spinZ = self._spin()
            self._addRow("Z", self._spinZ)

    def _addPlaceholder(self, text: str) -> None:
        """扩展功能占位：置灰不可用。"""
        cb = QCheckBox(text)
        cb.setEnabled(False)
        cb.setToolTip("该功能尚未移植，后续版本提供")
        self._layout.addWidget(cb)

    def _notifyResize(self) -> None:
        """面板区域展开/收起后，逐级向上通知容器重新适配高度。
        只到面板宿主（CommandParamHost）为止：继续向上 adjustSize
        会波及主窗口，切换模式时整个界面会闪烁缩放。"""
        from ui.widgets.command_param_host import CommandParamHost
        self.adjustSize()
        p = self.parentWidget()
        while p is not None:
            if isinstance(p, CommandParamHost):
                p._fitHeight()
                break
            p.adjustSize()
            p = p.parentWidget()

    # ---------- 坐标读写 ----------

    def x(self) -> float:
        return self._spinX.value() if self._spinX else 0.0

    def y(self) -> float:
        return self._spinY.value() if self._spinY else 0.0

    def z(self) -> float:
        return self._spinZ.value() if self._spinZ else 0.0

    def setXYZ(self, x: float, y: float, z: float = 0.0) -> None:
        """画布取点时回写坐标显示（阻断信号防回环）。"""
        for s, v in ((self._spinX, x), (self._spinY, y), (self._spinZ, z)):
            if s is None:
                continue
            s.blockSignals(True)
            s.setValue(v)
            s.blockSignals(False)


# ==================== Point ====================

class PointParamPanel(ParamPanelBase):
    confirmed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._addCoordRows(withZ=True)
        btn = self._button("确认创建点", primary=True)
        btn.clicked.connect(self.confirmed.emit)
        self._layout.addWidget(btn)
        self._layout.addStretch(1)


# ==================== Line ====================

class LineParamPanel(ParamPanelBase):
    addPointRequested = Signal()
    undoClicked = Signal()
    closeToggled = Signal(bool)
    confirmRequested = Signal()
    drawParamRequested = Signal()   # 按参数画点：指定长度 + 指定角度一键落点
    bisectorToggled = Signal(bool)  # 角平分线模式开关

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._addCoordRows(withZ=True)
        btnAdd = self._button2("添加点")
        btnAdd.clicked.connect(self.addPointRequested.emit)
        self._layout.addWidget(btnAdd)

        # 长度：实时显示当前段长 + 输入目标长度（按参数画点时生效）
        self._labelLength = self._label("--")
        self._addRow("长度", self._labelLength)
        self._spinLength = self._spin(0.0, decimals=3)
        self._addRow("指定长度", self._spinLength)

        # 角度：实时偏转角 + 角度弧预览勾选（同行）；顺/逆 + 指定角度（相对上段）
        self._labelAngle = self._label("--")
        angRow = QHBoxLayout()
        angRow.setSpacing(4)
        labA = QLabel("角度")
        labA.setMinimumWidth(_LABEL_W)
        self._chkAnglePreview = QCheckBox("预览")
        angRow.addWidget(labA)
        angRow.addWidget(self._labelAngle)
        angRow.addWidget(self._chkAnglePreview)
        angRow.addStretch(1)
        self._layout.addLayout(angRow)

        self._angleCCW = True
        arow = QHBoxLayout()
        arow.setSpacing(4)
        lab = QLabel("指定角度")
        lab.setMinimumWidth(_LABEL_W)
        self._btnAngleSign = self._button2("逆")
        self._btnAngleSign.setToolTip("切换：逆时针/顺时针")
        self._btnAngleSign.clicked.connect(self._onSignToggled)
        self._spinAngle = self._spin(0.0, decimals=2)
        self._spinAngle.setFixedWidth(90)   # 收窄，给左侧标签腾出完整显示空间
        arow.addWidget(lab)
        arow.addWidget(self._btnAngleSign)
        arow.addWidget(self._spinAngle)
        arow.addStretch(1)
        self._layout.addLayout(arow)

        btnDraw = self._button("按参数画点")
        btnDraw.setToolTip("按指定长度和角度画一段：角度为 0 时沿光标方向")
        btnDraw.clicked.connect(self.drawParamRequested.emit)
        self._layout.addWidget(btnDraw)

        # 角平分线模式：选两条直线 → 预览平分线 → 点击定终点（本命令新增）
        self._chkBisector = QCheckBox("角平分线")
        self._chkBisector.setToolTip(
            "依次选两条直线，预览角平分线，再点击确定终点")
        self._chkBisector.toggled.connect(self.bisectorToggled.emit)
        self._layout.addWidget(self._chkBisector)

        row = QHBoxLayout()
        self._btnUndo = self._button2("撤回上一点")
        self._btnUndo.clicked.connect(self.undoClicked.emit)
        self._chkClose = QCheckBox("闭合")
        self._chkClose.toggled.connect(self.closeToggled.emit)
        row.addWidget(self._btnUndo)
        row.addWidget(self._chkClose)
        self._layout.addLayout(row)
        self._closeHint = self._label("")
        self._layout.addWidget(self._closeHint)

        btnOk = self._button("确认完成", primary=True)
        btnOk.clicked.connect(self.confirmRequested.emit)
        self._layout.addWidget(btnOk)
        self._layout.addStretch(1)

    def _onSignToggled(self) -> None:
        self._angleCCW = not self._angleCCW
        self._btnAngleSign.setText("逆" if self._angleCCW else "顺")

    def lengthValue(self) -> float:
        return self._spinLength.value()

    def angleValue(self) -> float:
        return self._spinAngle.value()

    def angleCCW(self) -> bool:
        return self._angleCCW

    def setLength(self, length: float) -> None:
        self._labelLength.setText(f"{length:.3f}")

    def setAngle(self, degrees: float) -> None:
        self._labelAngle.setText(f"{degrees:.2f}°")

    def isAnglePreviewChecked(self) -> bool:
        return self._chkAnglePreview.isChecked()

    def setCloseEnabled(self, enabled: bool, hint: str = "") -> None:
        self._chkClose.setEnabled(enabled)
        self._closeHint.setText(hint)

    def focusOnLength(self) -> None:
        self._spinLength.setFocus()
        self._spinLength.selectAll()

    def setClosed(self, closed: bool) -> None:
        self._chkClose.blockSignals(True)
        self._chkClose.setChecked(closed)
        self._chkClose.blockSignals(False)

    def setBisectorChecked(self, on: bool) -> None:
        self._chkBisector.blockSignals(True)
        self._chkBisector.setChecked(on)
        self._chkBisector.blockSignals(False)


# ==================== Curve ====================

class CurveParamPanel(ParamPanelBase):
    """样条曲线参数面板：坐标加点/撤销/闭合/确认（固定插值样条，无模式切换）。"""
    addPointRequested = Signal()
    undoClicked = Signal()
    closeToggled = Signal(bool)
    confirmRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._addCoordRows(withZ=True)
        btnAdd = self._button2("添加点")
        btnAdd.clicked.connect(self.addPointRequested.emit)
        self._layout.addWidget(btnAdd)

        row = QHBoxLayout()
        btnUndo = self._button2("撤回上一点")
        btnUndo.clicked.connect(self.undoClicked.emit)
        self._chkClose = QCheckBox("闭合")
        self._chkClose.toggled.connect(self.closeToggled.emit)
        row.addWidget(btnUndo)
        row.addWidget(self._chkClose)
        self._layout.addLayout(row)

        btnOk = self._button("确认完成", primary=True)
        btnOk.clicked.connect(self.confirmRequested.emit)
        self._layout.addWidget(btnOk)
        self._layout.addStretch(1)

    def setClosed(self, closed: bool) -> None:
        self._chkClose.blockSignals(True)
        self._chkClose.setChecked(closed)
        self._chkClose.blockSignals(False)


# ==================== Arc ====================

class ArcParamPanel(ParamPanelBase):
    addPointRequested = Signal()
    confirmRequested = Signal()
    modeChanged = Signal(int)   # 0=起点-终点-弧上点 1=起点-弧上点-终点

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # 模式文案较长，面板宽度有限 → 垂直排列避免挤压截断
        self._layout.addWidget(QLabel("绘制模式"))
        self._modeGroup = QButtonGroup(self)
        self._radioSE = QRadioButton("起点-终点-弧上点")
        self._radioSM = QRadioButton("起点-弧上点-终点")
        self._radioSE.setChecked(True)
        self._modeGroup.addButton(self._radioSE, 0)
        self._modeGroup.addButton(self._radioSM, 1)
        self._modeGroup.idClicked.connect(self.modeChanged.emit)
        self._modeGroup.idClicked.connect(self._syncHint)
        self._layout.addWidget(self._radioSE)
        self._layout.addWidget(self._radioSM)

        self._hintLabel = self._label("三点定弧：起点 → 终点 → 弧上点")
        self._hintLabel.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        self._layout.addWidget(self._hintLabel)
        self._addCoordRows(withZ=True)
        btnAdd = self._button2("添加点")
        btnAdd.clicked.connect(self.addPointRequested.emit)
        self._layout.addWidget(btnAdd)
        btnOk = self._button("确认完成", primary=True)
        btnOk.clicked.connect(self.confirmRequested.emit)
        self._layout.addWidget(btnOk)
        self._layout.addStretch(1)

    def setMode(self, mode: int) -> None:
        btn = self._radioSE if mode == 0 else self._radioSM
        self._modeGroup.blockSignals(True)
        btn.setChecked(True)
        self._modeGroup.blockSignals(False)
        self._syncHint(mode)

    def _syncHint(self, mode: int) -> None:
        if mode == 0:
            self._hintLabel.setText("三点定弧：起点 → 终点 → 弧上点")
        else:
            self._hintLabel.setText("三点定弧：起点 → 弧上点 → 终点")


# ==================== Circle ====================

class CircleParamPanel(ParamPanelBase):
    diameterChanged = Signal(float)
    confirmed = Signal()
    waveModeChanged = Signal(bool)
    waveArcCountChanged = Signal(int)
    waveChamferChanged = Signal(float)
    waveTypeChanged = Signal(int)
    waveChamferTypeChanged = Signal(int)   # 0=倒直角 1=圆角（圆弧）
    waveRadiusChanged = Signal()   # 内/外半径任一变化（仅启用态触发）

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        modeRow = QHBoxLayout()
        modeRow.setSpacing(4)
        self._modeGroup = QButtonGroup(self)
        self._radioStd = QRadioButton("标准")
        self._radioWave = QRadioButton("波浪")
        self._radioStd.setChecked(True)
        self._modeGroup.addButton(self._radioStd, 0)
        self._modeGroup.addButton(self._radioWave, 1)
        self._modeGroup.idClicked.connect(self._onMode)
        modeRow.addWidget(QLabel("模式"))
        modeRow.addWidget(self._radioStd)
        modeRow.addWidget(self._radioWave)
        modeRow.addStretch(1)
        self._layout.addLayout(modeRow)

        self._addCoordRows()
        self._spinDia = self._spin(0.0, step=0.5)
        self._spinDia.setMinimum(0.0)
        self._spinDia.valueChanged.connect(self.diameterChanged.emit)
        self._addRow("直径", self._spinDia)

        # 波浪参数区（选波浪模式后展开）
        self._waveWidget = QWidget()
        wlay = QVBoxLayout(self._waveWidget)
        wlay.setContentsMargins(0, 0, 0, 0)
        wlay.setSpacing(5)
        # 波形：直线 / 样条 / 圆弧（默认圆弧，与原版波浪圆外观一致）
        self._waveTypeGroup = QButtonGroup(self)
        self._radioWaveLine = QRadioButton("直线")
        self._radioWaveSpline = QRadioButton("样条")
        self._radioWaveArc = QRadioButton("圆弧")
        self._radioWaveArc.setChecked(True)
        self._waveTypeGroup.addButton(self._radioWaveLine, 0)
        self._waveTypeGroup.addButton(self._radioWaveSpline, 1)
        self._waveTypeGroup.addButton(self._radioWaveArc, 2)
        self._waveTypeGroup.idClicked.connect(self.waveTypeChanged.emit)
        trow = QHBoxLayout()
        trow.setSpacing(4)
        labT = QLabel("波形")
        labT.setMinimumWidth(_LABEL_W)
        trow.addWidget(labT)
        trow.addWidget(self._radioWaveLine)
        trow.addWidget(self._radioWaveSpline)
        trow.addWidget(self._radioWaveArc)
        trow.addStretch(1)
        wlay.addLayout(trow)
        self._spinArcCount = self._ispin(8, 2, 36)
        crow = QHBoxLayout()
        crow.setSpacing(4)
        labC = QLabel("弧数")
        labC.setMinimumWidth(_LABEL_W)
        crow.addWidget(labC)
        crow.addWidget(self._spinArcCount, 1)
        wlay.addLayout(crow)
        # 内/外半径：锁定大小前回写禁用，锁定后可实时调节
        self._spinInner = self._spin(0.0, decimals=3, step=0.5)
        self._spinInner.setMinimum(0.001)
        self._spinBulge = self._spin(0.0, decimals=3, step=0.5)
        self._spinBulge.setMinimum(0.001)
        self._spinInner.setEnabled(False)
        self._spinBulge.setEnabled(False)
        irow = QHBoxLayout()
        irow.setSpacing(4)
        labI = QLabel("内半径")
        labI.setMinimumWidth(_LABEL_W)
        irow.addWidget(labI)
        irow.addWidget(self._spinInner, 1)
        wlay.addLayout(irow)
        orow = QHBoxLayout()
        orow.setSpacing(4)
        labO = QLabel("外半径")
        labO.setMinimumWidth(_LABEL_W)
        orow.addWidget(labO)
        orow.addWidget(self._spinBulge, 1)
        wlay.addLayout(orow)
        # 倒角：类型（倒直角/圆角）+ 半径，同原版圆弧连接处圆角语义
        self._chamferTypeGroup = QButtonGroup(self)
        self._radioChamferStraight = QRadioButton("倒直角")
        self._radioChamferRound = QRadioButton("圆角")
        self._radioChamferRound.setChecked(True)
        self._chamferTypeGroup.addButton(self._radioChamferStraight, 0)
        self._chamferTypeGroup.addButton(self._radioChamferRound, 1)
        self._chamferTypeGroup.idClicked.connect(self.waveChamferTypeChanged.emit)
        tyrow = QHBoxLayout()
        tyrow.setSpacing(4)
        labTy = QLabel("倒角")
        labTy.setMinimumWidth(_LABEL_W)
        tyrow.addWidget(labTy)
        tyrow.addWidget(self._radioChamferStraight)
        tyrow.addWidget(self._radioChamferRound)
        tyrow.addStretch(1)
        wlay.addLayout(tyrow)
        self._spinWaveChamfer = self._spin(0.0, step=0.5)
        self._spinWaveChamfer.setMinimum(0.0)
        frow = QHBoxLayout()
        frow.setSpacing(4)
        labF = QLabel("倒角半径")
        labF.setMinimumWidth(_LABEL_W)
        frow.addWidget(labF)
        frow.addWidget(self._spinWaveChamfer, 1)
        wlay.addLayout(frow)
        hint = QLabel("拖动定大小（内=距离×0.6）；锁定后内外半径可继续调节")
        hint.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        hint.setWordWrap(True)
        wlay.addWidget(hint)
        self._waveWidget.hide()
        self._layout.addWidget(self._waveWidget)
        self._spinArcCount.valueChanged.connect(self.waveArcCountChanged.emit)
        self._spinWaveChamfer.valueChanged.connect(self.waveChamferChanged.emit)
        self._spinInner.valueChanged.connect(self._emitWaveRadius)
        self._spinBulge.valueChanged.connect(self._emitWaveRadius)

        btnOk = self._button("确认创建圆", primary=True)
        btnOk.clicked.connect(self.confirmed.emit)
        self._layout.addWidget(btnOk)
        self._layout.addStretch(1)

    def _onMode(self, mid: int) -> None:
        wave = mid == 1
        self._waveWidget.setVisible(wave)
        self._spinDia.setEnabled(not wave)
        self.waveModeChanged.emit(wave)
        self._notifyResize()

    def setDiameter(self, d: float) -> None:
        self._spinDia.blockSignals(True)
        self._spinDia.setValue(d)
        self._spinDia.blockSignals(False)

    def diameter(self) -> float:
        return self._spinDia.value()

    def _emitWaveRadius(self) -> None:
        # 程序回写（禁用态）不触发；仅用户调节时通知命令
        if self._spinInner.isEnabled() or self._spinBulge.isEnabled():
            self.waveRadiusChanged.emit()

    def waveType(self) -> int:
        return self._waveTypeGroup.checkedId()

    def setWaveRadii(self, inner: float, bulge: float) -> None:
        """回写内/外半径并启用调节（锁定后调用）。"""
        for s, v in ((self._spinInner, inner), (self._spinBulge, bulge)):
            s.blockSignals(True)
            s.setValue(v)
            s.blockSignals(False)
            s.setEnabled(True)

    def disableWaveRadii(self) -> None:
        self._spinInner.setEnabled(False)
        self._spinBulge.setEnabled(False)

    def innerRadius(self) -> float:
        return self._spinInner.value()

    def bulgeRadius(self) -> float:
        return self._spinBulge.value()


# ==================== Ellipse ====================

class EllipseParamPanel(ParamPanelBase):
    axesChanged = Signal(float, float)
    confirmed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._addCoordRows()
        self._spinMajor = self._spin(0.0, step=0.5)
        self._spinMajor.setMinimum(0.0)
        self._spinMinor = self._spin(0.0, step=0.5)
        self._spinMinor.setMinimum(0.0)
        self._spinMajor.valueChanged.connect(self._emitAxes)
        self._spinMinor.valueChanged.connect(self._emitAxes)
        self._addRow("长轴", self._spinMajor)
        self._addRow("短轴", self._spinMinor)
        btnOk = self._button("确认创建椭圆", primary=True)
        btnOk.clicked.connect(self.confirmed.emit)
        self._layout.addWidget(btnOk)
        self._addPlaceholder("波浪椭圆（待移植）")
        self._layout.addStretch(1)

    def _emitAxes(self) -> None:
        self.axesChanged.emit(self._spinMajor.value(), self._spinMinor.value())

    def setAxes(self, major: float, minor: float) -> None:
        for s, v in ((self._spinMajor, major), (self._spinMinor, minor)):
            s.blockSignals(True)
            s.setValue(v)
            s.blockSignals(False)


# ==================== Rectangle ====================

class RectangleParamPanel(ParamPanelBase):
    sizeChanged = Signal(float, float)      # 长/宽实时变化
    chamferTypeChanged = Signal(int)        # 0=无 1=圆角 2=斜角
    chamferRadiiChanged = Signal()          # 四角半径任一变化（同原版四角独立）
    confirmRequested = Signal()
    cancelRequested = Signal()
    waveEnabledToggled = Signal(bool)
    waveParamChanged = Signal()             # 波形/波数/高度任一变化

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._addCoordRows()
        self._spinLen = self._spin(0.0, step=0.5)
        self._spinLen.setMinimum(0.0)
        self._spinWid = self._spin(0.0, step=0.5)
        self._spinWid.setMinimum(0.0)
        self._spinLen.valueChanged.connect(self._emitSize)
        self._spinWid.valueChanged.connect(self._emitSize)
        # 长/宽标题可点击联动（同原版）：点长度 → 宽度=长度，方便画正方形；反之亦然
        self._labLen = _ClickLabel("长度", "点击：宽度 = 长度（快速画正方形）")
        self._labLen.setMinimumWidth(_LABEL_W)
        self._labLen.clicked.connect(self._onLenLabelClicked)
        rowLen = QHBoxLayout()
        rowLen.setSpacing(4)
        rowLen.addWidget(self._labLen)
        rowLen.addWidget(self._spinLen, 1)
        self._layout.addLayout(rowLen)
        self._labWid = _ClickLabel("宽度", "点击：长度 = 宽度（快速画正方形）")
        self._labWid.setMinimumWidth(_LABEL_W)
        self._labWid.clicked.connect(self._onWidLabelClicked)
        rowWid = QHBoxLayout()
        rowWid.setSpacing(4)
        rowWid.addWidget(self._labWid)
        rowWid.addWidget(self._spinWid, 1)
        self._layout.addLayout(rowWid)

        row = QHBoxLayout()
        self._chamferGroup = QButtonGroup(self)
        self._radioNone = QRadioButton("无倒角")
        self._radioFillet = QRadioButton("圆角")
        self._radioBevel = QRadioButton("斜角")
        self._radioNone.setChecked(True)
        self._chamferGroup.addButton(self._radioNone, 0)
        self._chamferGroup.addButton(self._radioFillet, 1)
        self._chamferGroup.addButton(self._radioBevel, 2)
        self._chamferGroup.idClicked.connect(self._onChamferType)
        row.addWidget(self._radioNone)
        row.addWidget(self._radioFillet)
        row.addWidget(self._radioBevel)
        self._layout.addLayout(row)

        # 倒角半径：四角独立（同原版）+ 同半径联动开关；
        # 未启用倒角时整区禁用。
        urow = QHBoxLayout()
        urow.setSpacing(4)
        self._labRadius = QLabel("半径")
        self._labRadius.setMinimumWidth(_LABEL_W)
        self._chkUniform = QCheckBox("同半径")
        self._chkUniform.setChecked(True)
        self._chkUniform.toggled.connect(self._onUniformToggled)
        urow.addWidget(self._labRadius)
        urow.addStretch(1)
        urow.addWidget(self._chkUniform)
        self._layout.addLayout(urow)
        self._cornerSpins = []
        for i in range(4):
            s = self._spin(0.0, step=0.5)
            s.setMinimum(0.0)
            s.setEnabled(False)
            s.valueChanged.connect(self._onCornerRadiusChanged)
            self._cornerSpins.append(s)
        self._layout.addLayout(self._wavePair("角1", self._cornerSpins[0],
                                              "角2", self._cornerSpins[1]))
        self._layout.addLayout(self._wavePair("角3", self._cornerSpins[2],
                                              "角4", self._cornerSpins[3]))

        # ---------- 波浪参数区（勾选展开，移植自原版） ----------
        self._chkWave = QCheckBox("启用波浪")
        self._chkWave.toggled.connect(self._onWaveToggled)
        self._layout.addWidget(self._chkWave)

        self._waveWidget = QWidget()
        wlay = QVBoxLayout(self._waveWidget)
        wlay.setContentsMargins(0, 0, 0, 0)
        wlay.setSpacing(5)
        trow = QHBoxLayout()
        trow.setSpacing(4)
        self._waveTypeGroup = QButtonGroup(self)
        self._radioWaveLine = QRadioButton("直线")
        self._radioWaveSmooth = QRadioButton("平滑")
        self._radioWaveArc = QRadioButton("圆弧")
        self._radioWaveSmooth.setChecked(True)
        self._waveTypeGroup.addButton(self._radioWaveLine, 0)
        self._waveTypeGroup.addButton(self._radioWaveSmooth, 1)
        self._waveTypeGroup.addButton(self._radioWaveArc, 2)
        self._waveTypeGroup.idClicked.connect(
            lambda _i: self.waveParamChanged.emit())
        trow.addWidget(QLabel("波形"))
        trow.addWidget(self._radioWaveLine)
        trow.addWidget(self._radioWaveSmooth)
        trow.addWidget(self._radioWaveArc)
        trow.addStretch(1)
        wlay.addLayout(trow)
        self._spinLongCount = self._ispin(5, 1, 50)
        self._spinShortCount = self._ispin(3, 1, 50)
        wlay.addLayout(self._wavePair("长边", self._spinLongCount,
                                      "短边", self._spinShortCount))
        self._spinSideH = self._spin(3.0, step=0.5)
        self._spinSideH.setRange(0.0, 100.0)
        self._spinCornerH = self._spin(6.5, step=0.5)
        self._spinCornerH.setRange(0.0, 100.0)
        wlay.addLayout(self._wavePair("边半径", self._spinSideH,
                                      "角半径", self._spinCornerH))
        self._spinCornerW = self._spin(6.0, step=0.5)
        self._spinCornerW.setRange(0.0, 50.0)
        self._spinCornerChamfer = self._spin(0.0, step=0.5)
        self._spinCornerChamfer.setRange(0.0, 50.0)
        wlay.addLayout(self._wavePair("角波宽", self._spinCornerW,
                                      "倒角", self._spinCornerChamfer))
        for s in (self._spinLongCount, self._spinShortCount, self._spinSideH,
                  self._spinCornerH, self._spinCornerW):
            s.valueChanged.connect(lambda _v: self.waveParamChanged.emit())
        self._waveWidget.hide()
        self._layout.addWidget(self._waveWidget)

        brow = QHBoxLayout()
        btnOk = self._button("确认创建", primary=True)
        btnOk.clicked.connect(self.confirmRequested.emit)
        btnCancel = self._button2("取消")
        btnCancel.clicked.connect(self.cancelRequested.emit)
        brow.addWidget(btnOk)
        brow.addWidget(btnCancel)
        self._layout.addLayout(brow)
        self._layout.addStretch(1)

    def _onWaveToggled(self, on: bool) -> None:
        self._waveWidget.setVisible(on)
        self.waveEnabledToggled.emit(on)
        self._notifyResize()

    def _wavePair(self, lab1: str, w1: QWidget, lab2: str,
                  w2: QWidget) -> QHBoxLayout:
        """波浪参数双列紧凑行（原版 waveGrid 两列布局的简化）。"""
        row = QHBoxLayout()
        row.setSpacing(4)
        l1 = QLabel(lab1)
        l1.setMinimumWidth(36)
        l2 = QLabel(lab2)
        l2.setMinimumWidth(36)
        row.addWidget(l1)
        row.addWidget(w1, 1)
        row.addWidget(l2)
        row.addWidget(w2, 1)
        return row

    def waveEnabled(self) -> bool:
        return self._chkWave.isChecked()

    def waveType(self) -> int:
        return self._waveTypeGroup.checkedId()

    def longWaveCount(self) -> int:
        return self._spinLongCount.value()

    def shortWaveCount(self) -> int:
        return self._spinShortCount.value()

    def sideWaveHeight(self) -> float:
        return self._spinSideH.value()

    def cornerWaveHeight(self) -> float:
        return self._spinCornerH.value()

    def cornerWaveWidth(self) -> float:
        return self._spinCornerW.value()

    def cornerChamfer(self) -> float:
        return self._spinCornerChamfer.value()

    def _emitSize(self) -> None:
        self.sizeChanged.emit(self._spinLen.value(), self._spinWid.value())

    def _onLenLabelClicked(self) -> None:
        # 点长度标题 → 宽度 = 长度（同原版 onLengthLabelClicked）
        self._spinWid.setValue(self._spinLen.value())
        self._emitSize()

    def _onWidLabelClicked(self) -> None:
        # 点宽度标题 → 长度 = 宽度（同原版 onWidthLabelClicked）
        self._spinLen.setValue(self._spinWid.value())
        self._emitSize()

    def _onChamferType(self, cid: int) -> None:
        self._setRadiiEnabled(cid > 0)
        self._labRadius.setText("距离" if cid == 2 else "半径")
        self.chamferTypeChanged.emit(cid)

    def _setRadiiEnabled(self, on: bool) -> None:
        for s in self._cornerSpins:
            s.setEnabled(on)
        self._chkUniform.setEnabled(on)

    def _onUniformToggled(self, on: bool) -> None:
        if on:
            # 勾上同半径：以角1为准同步其余三角（同原版）
            v = self._cornerSpins[0].value()
            for s in self._cornerSpins[1:]:
                s.blockSignals(True)
                s.setValue(v)
                s.blockSignals(False)
            self.chamferRadiiChanged.emit()

    def _onCornerRadiusChanged(self, _v: float) -> None:
        if self._chkUniform.isChecked():
            sender = self.sender()
            v = sender.value() if sender is not None else 0.0
            for s in self._cornerSpins:
                if s is not sender:
                    s.blockSignals(True)
                    s.setValue(v)
                    s.blockSignals(False)
        self.chamferRadiiChanged.emit()

    def setSize(self, length: float, width: float) -> None:
        for s, v in ((self._spinLen, length), (self._spinWid, width)):
            s.blockSignals(True)
            s.setValue(v)
            s.blockSignals(False)

    def length(self) -> float:
        return self._spinLen.value()

    def width(self) -> float:
        return self._spinWid.value()

    def setCornerRadius(self, index: int, r: float) -> None:
        """程序回写单角半径（不触发联动信号）。"""
        if 0 <= index < 4:
            s = self._cornerSpins[index]
            s.blockSignals(True)
            s.setValue(r)
            s.blockSignals(False)

    def setChamferRadii(self, radii) -> None:
        for i, r in enumerate(list(radii)[:4]):
            self.setCornerRadius(i, r)

    def cornerRadii(self) -> list:
        return [s.value() for s in self._cornerSpins]

    def setChamferRadius(self, r: float) -> None:
        self.setChamferRadii([r] * 4)

    def chamferRadius(self) -> float:
        return self._cornerSpins[0].value()

    def setChamferType(self, cid: int) -> None:
        btn = self._chamferGroup.button(cid)
        if btn is not None and not btn.isChecked():
            btn.setChecked(True)
            self._onChamferType(cid)

    def chamferType(self) -> int:
        return self._chamferGroup.checkedId()


# ==================== Polygon ====================

class PolygonParamPanel(ParamPanelBase):
    centerChanged = Signal(float, float)
    sidesChanged = Signal(int)
    radiusChanged = Signal(float)
    starModeChanged = Signal(bool)
    innerRadiusChanged = Signal(float)
    filletRadiusChanged = Signal(float)
    confirmed = Signal()
    cancelRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._addCoordRows()
        self._spinX.valueChanged.connect(self._emitCenter)
        self._spinY.valueChanged.connect(self._emitCenter)

        self._spinSides = self._ispin(6, 3, 64)
        self._spinSides.valueChanged.connect(self.sidesChanged.emit)
        self._addRow("边数", self._spinSides)

        self._spinRadius = self._spin(25.0, step=0.5)
        self._spinRadius.setMinimum(0.01)
        self._spinRadius.valueChanged.connect(self.radiusChanged.emit)
        self._addRow("外径", self._spinRadius)

        self._chkStar = QCheckBox("星型")
        self._chkStar.toggled.connect(self._onStarToggled)
        self._layout.addWidget(self._chkStar)

        self._spinInner = self._spin(12.5, step=0.5)
        self._spinInner.setMinimum(0.01)
        self._spinInner.setEnabled(False)
        self._spinInner.valueChanged.connect(self.innerRadiusChanged.emit)
        self._addRow("内径", self._spinInner)

        self._spinFillet = self._spin(0.0, step=0.5)
        self._spinFillet.setMinimum(0.0)
        self._spinFillet.valueChanged.connect(self.filletRadiusChanged.emit)
        self._addRow("倒角半径", self._spinFillet)

        brow = QHBoxLayout()
        btnOk = self._button("确认创建", primary=True)
        btnOk.clicked.connect(self.confirmed.emit)
        btnCancel = self._button2("取消")
        btnCancel.clicked.connect(self.cancelRequested.emit)
        brow.addWidget(btnOk)
        brow.addWidget(btnCancel)
        self._layout.addLayout(brow)
        self._layout.addStretch(1)

    def _emitCenter(self) -> None:
        self.centerChanged.emit(self.x(), self.y())

    def _onStarToggled(self, on: bool) -> None:
        self._spinInner.setEnabled(on)
        self.starModeChanged.emit(on)

    def setCenter(self, x: float, y: float) -> None:
        for s, v in ((self._spinX, x), (self._spinY, y)):
            s.blockSignals(True)
            s.setValue(v)
            s.blockSignals(False)

    def setSides(self, n: int) -> None:
        self._spinSides.blockSignals(True)
        self._spinSides.setValue(n)
        self._spinSides.blockSignals(False)

    def setRadius(self, r: float) -> None:
        self._spinRadius.blockSignals(True)
        self._spinRadius.setValue(r)
        self._spinRadius.blockSignals(False)

    def setInnerRadius(self, r: float) -> None:
        self._spinInner.blockSignals(True)
        self._spinInner.setValue(r)
        self._spinInner.blockSignals(False)

    def setFilletRadius(self, r: float) -> None:
        self._spinFillet.blockSignals(True)
        self._spinFillet.setValue(r)
        self._spinFillet.blockSignals(False)

    def setStarMode(self, star: bool) -> None:
        self._chkStar.blockSignals(True)
        self._chkStar.setChecked(star)
        self._spinInner.setEnabled(star)
        self._chkStar.blockSignals(False)


# ==================== Chamfer ====================

class ChamferParamPanel(ParamPanelBase):
    modeChanged = Signal(int)            # 0=圆角 1=斜角
    radiusChanged = Signal(float)
    distanceChanged = Signal(float, float)
    selectModeChanged = Signal(int)      # 0=单角 1=整线
    confirmed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        row = QHBoxLayout()
        self._modeGroup = QButtonGroup(self)
        self._radioFillet = QRadioButton("圆角")
        self._radioBevel = QRadioButton("斜角")
        self._radioFillet.setChecked(True)
        self._modeGroup.addButton(self._radioFillet, 0)
        self._modeGroup.addButton(self._radioBevel, 1)
        self._modeGroup.idClicked.connect(self._onMode)
        row.addWidget(self._radioFillet)
        row.addWidget(self._radioBevel)
        self._layout.addLayout(row)

        self._spinRadius = self._spin(5.0, step=0.5)
        self._spinRadius.setMinimum(0.0)
        self._spinRadius.valueChanged.connect(self.radiusChanged.emit)
        self._addRow("半径", self._spinRadius)

        self._spinD1 = self._spin(5.0, step=0.5)
        self._spinD1.setMinimum(0.0)
        self._spinD2 = self._spin(5.0, step=0.5)
        self._spinD2.setMinimum(0.0)
        self._spinD1.valueChanged.connect(self._emitDist)
        self._spinD2.valueChanged.connect(self._emitDist)
        self._addRow("距离 1", self._spinD1)
        self._addRow("距离 2", self._spinD2)
        self._spinD1.setEnabled(False)
        self._spinD2.setEnabled(False)

        srow = QHBoxLayout()
        self._selGroup = QButtonGroup(self)
        self._radioSingle = QRadioButton("单角")
        self._radioWhole = QRadioButton("整线")
        self._radioSingle.setChecked(True)
        self._selGroup.addButton(self._radioSingle, 0)
        self._selGroup.addButton(self._radioWhole, 1)
        self._selGroup.idClicked.connect(self.selectModeChanged.emit)
        srow.addWidget(QLabel("选择"))
        srow.addWidget(self._radioSingle)
        srow.addWidget(self._radioWhole)
        self._layout.addLayout(srow)

        btnOk = self._button("确认执行", primary=True)
        btnOk.clicked.connect(self.confirmed.emit)
        self._layout.addWidget(btnOk)
        self._layout.addStretch(1)

    def _emitDist(self) -> None:
        self.distanceChanged.emit(self._spinD1.value(), self._spinD2.value())

    def _onMode(self, mid: int) -> None:
        bevel = mid == 1
        self._spinRadius.setEnabled(not bevel)
        self._spinD1.setEnabled(bevel)
        self._spinD2.setEnabled(bevel)
        self.modeChanged.emit(mid)

    def setRadius(self, r: float) -> None:
        self._spinRadius.blockSignals(True)
        self._spinRadius.setValue(r)
        self._spinRadius.blockSignals(False)

    def setDistances(self, d1: float, d2: float) -> None:
        for s, v in ((self._spinD1, d1), (self._spinD2, d2)):
            s.blockSignals(True)
            s.setValue(v)
            s.blockSignals(False)


# ==================== ClosePath 闭合 ====================

class CloseParamPanel(ParamPanelBase):
    """闭合面板：为选中的开放曲线选择首尾连接方式。

    信号：
    - methodChanged(int): 连接方式切换（0=直线连接 1=圆弧连接 2=平滑曲线）
    - confirmed(): 点击"执行闭合"
    - cancelRequested(): 点击"取消"
    """
    methodChanged = Signal(int)
    confirmed = Signal()
    cancelRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        row = QHBoxLayout()
        self._group = QButtonGroup(self)
        for i, label in enumerate(("直线连接", "圆弧连接", "平滑曲线")):
            rb = QRadioButton(label)
            rb.setChecked(i == 0)
            self._group.addButton(rb, i)
            row.addWidget(rb)
        self._group.idClicked.connect(self._onMethod)
        self._layout.addLayout(row)

        btnRow = QHBoxLayout()
        btnOk = self._button("执行闭合", primary=True)
        btnOk.clicked.connect(self.confirmed.emit)
        btnCancel = self._button2("取消")
        btnCancel.clicked.connect(self.cancelRequested.emit)
        btnRow.addWidget(btnOk)
        btnRow.addWidget(btnCancel)
        self._layout.addLayout(btnRow)
        self._layout.addStretch(1)

    def _onMethod(self, mid: int) -> None:
        self.methodChanged.emit(mid)


# ==================== NewModel（新建浮雕模型） ====================

class NewModelParamPanel(ParamPanelBase):
    """新建浮雕模型面板：按选中曲线包围盒 + 余量 + 精度建高度场。

    只读显示包围盒尺寸；余量/精度/基面高度可调，实时预估栅格与内存，
    超过内存钳制上限时红色告警（与 HeightField.from_bbox 同一套逻辑）。
    """
    confirmed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._bboxW = 0.0
        self._bboxH = 0.0

        self._labBBox = self._label("-- × -- mm")
        self._addRow("包围盒", self._labBBox)

        self._spinMargin = self._spin(5.0, decimals=2, step=0.5)
        self._spinMargin.setRange(0.0, 100.0)
        self._spinMargin.valueChanged.connect(lambda _v: self._updateEstimate())
        self._addRow("余量", self._spinMargin)

        self._spinSpacing = self._spin(0.1, decimals=3, step=0.01)
        self._spinSpacing.setRange(0.001, 100.0)
        self._spinSpacing.valueChanged.connect(lambda _v: self._updateEstimate())
        self._addRow("精度", self._spinSpacing)

        self._spinBase = self._spin(0.0, decimals=2, step=0.5)
        self._addRow("基面高", self._spinBase)

        hint = QLabel("精度 = mm/像素（越小越细）；余量为四周额外材料")
        hint.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        hint.setWordWrap(True)
        self._layout.addWidget(hint)

        self._labEstimate = self._label("")
        self._labEstimate.setWordWrap(True)
        self._layout.addWidget(self._labEstimate)

        btnOk = self._button("确认创建", primary=True)
        btnOk.clicked.connect(self.confirmed.emit)
        self._layout.addWidget(btnOk)
        self._layout.addStretch(1)

    def setBBoxSize(self, w: float, h: float) -> None:
        """回填选中曲线包围盒尺寸（mm）并刷新预估。"""
        self._bboxW = float(w)
        self._bboxH = float(h)
        self._labBBox.setText(f"{w:.2f} × {h:.2f} mm")
        self._updateEstimate()

    def _updateEstimate(self) -> None:
        width = self._bboxW + 2.0 * self._spinMargin.value()
        height = self._bboxH + 2.0 * self._spinMargin.value()
        nx, ny, sp, clamped = HeightField.grid_size(
            width, height, self._spinSpacing.value())
        mb = nx * ny * 4 / (1024.0 * 1024.0)
        text = f"栅格 {nx} × {ny}    内存 {mb:.1f} MB"
        if clamped:
            text += f"\n⚠ 超内存上限，精度已自动加粗至 {sp:.4f} mm/px"
            self._labEstimate.setStyleSheet(
                "color:#e53935; font-weight:bold;")
        else:
            self._labEstimate.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        self._labEstimate.setText(text)

    def margin(self) -> float:
        return self._spinMargin.value()

    def spacing(self) -> float:
        return self._spinSpacing.value()

    def baseHeight(self) -> float:
        return self._spinBase.value()


# ==================== ReconstructRelief（重构浮雕网格） ====================

class ReconstructParamPanel(ParamPanelBase):
    """重构浮雕网格面板：把放置变换烘焙进网格 + 可选新尺寸/新精度。

    只读显示当前世界尺寸与栅格；精度可调，“贴合当前世界尺寸”勾选时用当前
    （变换后）尺寸，取消勾选可输入新尺寸（网格按新尺寸拉伸重采样）；
    实时预估栅格与内存，超内存钳制上限时红色告警（与 HeightField.grid_size 同逻辑）。
    """
    confirmed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._curW = 0.0
        self._curH = 0.0

        self._labSize = self._label("-- × -- mm")
        self._addRow("当前尺寸", self._labSize)
        self._labGrid = self._label("-- × --")
        self._addRow("当前栅格", self._labGrid)

        self._spinSpacing = self._spin(0.1, decimals=3, step=0.01)
        self._spinSpacing.setRange(0.001, 100.0)
        self._spinSpacing.valueChanged.connect(lambda _v: self._updateEstimate())
        self._addRow("精度", self._spinSpacing)

        self._chkFit = QCheckBox("贴合当前世界尺寸")
        self._chkFit.setChecked(True)
        self._chkFit.toggled.connect(self._onFitToggled)
        self._layout.addWidget(self._chkFit)

        self._spinW = self._spin(0.0, decimals=2, step=0.5)
        self._spinW.setRange(0.001, 100000.0)
        self._spinW.setEnabled(False)
        self._spinW.valueChanged.connect(lambda _v: self._updateEstimate())
        self._addRow("新宽度", self._spinW)
        self._spinH = self._spin(0.0, decimals=2, step=0.5)
        self._spinH.setRange(0.001, 100000.0)
        self._spinH.setEnabled(False)
        self._spinH.valueChanged.connect(lambda _v: self._updateEstimate())
        self._addRow("新高度", self._spinH)

        hint = QLabel("重构 = 把移动/缩放/旋转烘焙进网格并复位（唯一有损重采样）；"
                      "精度 = mm/像素（越小越细）。")
        hint.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        hint.setWordWrap(True)
        self._layout.addWidget(hint)

        self._labEstimate = self._label("")
        self._labEstimate.setWordWrap(True)
        self._layout.addWidget(self._labEstimate)

        btnOk = self._button("确认重构", primary=True)
        btnOk.clicked.connect(self.confirmed.emit)
        self._layout.addWidget(btnOk)
        self._layout.addStretch(1)

    def setModelInfo(self, curW: float, curH: float, nx: int, ny: int,
                     spacing: float) -> None:
        """回填当前模型的世界尺寸/栅格/精度并刷新预估。"""
        self._curW = float(curW)
        self._curH = float(curH)
        self._labSize.setText(f"{curW:.2f} × {curH:.2f} mm")
        self._labGrid.setText(f"{nx} × {ny}  @ {spacing:.4f} mm/px")
        self._spinSpacing.blockSignals(True)
        self._spinSpacing.setValue(float(spacing))
        self._spinSpacing.blockSignals(False)
        for s, v in ((self._spinW, curW), (self._spinH, curH)):
            s.blockSignals(True)
            s.setValue(float(v))
            s.blockSignals(False)
        self._updateEstimate()

    def _onFitToggled(self, on: bool) -> None:
        self._spinW.setEnabled(not on)
        self._spinH.setEnabled(not on)
        self._updateEstimate()
        self._notifyResize()

    def _updateEstimate(self) -> None:
        if self._chkFit.isChecked():
            w, h = self._curW, self._curH
        else:
            w, h = self._spinW.value(), self._spinH.value()
        if w <= 0 or h <= 0:
            self._labEstimate.setText("尺寸无效")
            return
        nx, ny, sp, clamped = HeightField.grid_size(w, h, self._spinSpacing.value())
        mb = nx * ny * 4 / (1024.0 * 1024.0)
        text = f"栅格 {nx} × {ny}    内存 {mb:.1f} MB"
        if clamped:
            text += f"\n⚠ 超内存上限，精度已自动加粗至 {sp:.4f} mm/px"
            self._labEstimate.setStyleSheet("color:#e53935; font-weight:bold;")
        else:
            self._labEstimate.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        self._labEstimate.setText(text)

    def spacing(self) -> float:
        return self._spinSpacing.value()

    def fitCurrent(self) -> bool:
        return self._chkFit.isChecked()

    def targetSize(self):
        return (self._spinW.value(), self._spinH.value())


# ==================== RegionRelief（区域浮雕） ====================

class RegionReliefParamPanel(ParamPanelBase):
    """区域浮雕参数面板（复刻 Rhino 插件"曲面区域"页）。

    分组与插件一致：截面类型 / 浮雕参数 / 表面特点 / 拼合方式。

    参数可用性随截面类型联动（对齐插件 RefreshParamAvailability）：
      - 矩形：只用基高 + 基角，边界/强度/高度置灰
      - 倒角：隐藏"自由高度 / 局部等高"、显示"外凸角 / 内凹角"，表面特点置灰
      - 其余（椭圆/三角/圆形/样条）：边界/强度可用；高度仅在"局部等高"下可用
    """

    finished = Signal()         # 点击"完成"（结束命令）
    paramsChanged = Signal()    # 任一参数变更（命令据此刷新提示）

    # 截面按钮 id 与 SHAPES 顺序一致：椭圆/矩形/三角/圆形/样条/倒角
    _SHAPE_LABELS = ("椭圆", "矩形", "三角", "圆形", "样条", "倒角")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        # ---- 截面类型（2 排 × 3 个）----
        self._shapeGroup = QButtonGroup(self)
        gShape, lShape = self._group("截面类型")
        lShape.addWidget(self._radioRow("", self._SHAPE_LABELS[0:3],
                                        self._shapeGroup, 0))
        lShape.addWidget(self._radioRow("", self._SHAPE_LABELS[3:6],
                                        self._shapeGroup, 3))
        self._shapeGroup.button(0).setChecked(True)
        self._shapeGroup.idClicked.connect(self._onShapeChanged)
        self._layout.addWidget(gShape)

        # ---- 浮雕参数 ----
        gRelief, lRelief = self._group("浮雕参数")

        self._spinBaseH = self._spin(3.0, decimals=1, step=0.5)
        self._spinBaseH.setRange(0.0, 50.0)
        lRelief.addWidget(self._row("基高", self._spinBaseH))

        self._spinBaseA = self._spin(80.0, decimals=0, step=1.0)
        self._spinBaseA.setRange(10.0, 90.0)
        lRelief.addWidget(self._row("基角", self._spinBaseA))

        lRelief.addWidget(self._divider())

        # 高度模式（倒角时隐藏）
        self._modeGroup = QButtonGroup(self)
        self._rowMode = self._radioRow("", ("自由高度", "局部等高"),
                                       self._modeGroup, 0)
        self._modeGroup.button(0).setChecked(True)
        self._modeGroup.idClicked.connect(self._onModeChanged)
        lRelief.addWidget(self._rowMode)

        # 倒角模式（仅倒角显示）
        self._chamferGroup = QButtonGroup(self)
        self._rowChamfer = self._radioRow("", ("外凸角", "内凹角"),
                                          self._chamferGroup, 0)
        self._chamferGroup.button(0).setChecked(True)
        self._chamferGroup.idClicked.connect(lambda *_: self.paramsChanged.emit())
        lRelief.addWidget(self._rowChamfer)

        lRelief.addWidget(self._divider())

        self._spinFreeH = self._spin(3.0, decimals=1, step=0.5)
        self._spinFreeH.setRange(0.1, 50.0)
        self._rowFreeH = self._row("高度", self._spinFreeH)
        lRelief.addWidget(self._rowFreeH)

        self._spinBorder = self._spin(45.0, decimals=0, step=1.0)
        self._spinBorder.setRange(1.0, 89.0)
        self._rowBorder = self._row("边界", self._spinBorder)
        lRelief.addWidget(self._rowBorder)

        self._spinStrength = self._spin(1.0, decimals=2, step=0.1)
        self._spinStrength.setRange(0.1, 2.0)
        self._rowStrength = self._row("强度", self._spinStrength)
        lRelief.addWidget(self._rowStrength)
        self._layout.addWidget(gRelief)

        # ---- 表面特点 ----
        self._finishGroup = QButtonGroup(self)
        gFinish, lFinish = self._group("表面特点")
        lFinish.addWidget(self._radioRow("", ("整体光滑", "棱角清晰"),
                                         self._finishGroup, 0))
        self._finishGroup.button(0).setChecked(True)
        self._finishGroup.idClicked.connect(lambda *_: self.paramsChanged.emit())
        self._layout.addWidget(gFinish)

        # ---- 拼合方式 ----
        self._blendGroup = QButtonGroup(self)
        gBlend, lBlend = self._group("拼合方式")
        lBlend.addWidget(self._radioRow("", ("叠加", "切除", "取代"),
                                        self._blendGroup, 0))
        self._blendGroup.button(0).setChecked(True)
        self._blendGroup.idClicked.connect(lambda *_: self.paramsChanged.emit())
        self._layout.addWidget(gBlend)

        # ---- 颜色（作用区域：按当前颜色限定起浮雕范围）----
        self._scopeGroup = QButtonGroup(self)
        gColor, lColor = self._group("颜色")
        lColor.addWidget(self._radioRow("", COLOR_SCOPE_LABELS,
                                        self._scopeGroup, 0))
        self._scopeGroup.button(0).setChecked(True)   # 默认“颜色无效”
        self._scopeGroup.idClicked.connect(lambda *_: self.paramsChanged.emit())
        self._layout.addWidget(gColor)

        # ---- 状态 + 应用 ----
        self._labStatus = QLabel("")
        self._labStatus.setWordWrap(True)
        self._labStatus.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        self._layout.addWidget(self._labStatus)

        btn = self._button("完成", primary=True)
        btn.clicked.connect(self.finished.emit)
        self._layout.addWidget(btn)
        self._layout.addStretch(1)

        for s in (self._spinBaseH, self._spinBaseA, self._spinFreeH,
                  self._spinBorder, self._spinStrength):
            s.valueChanged.connect(lambda *_: self.paramsChanged.emit())

        self._refreshAvailability()

    # ---------- 布局小工具 ----------

    def _group(self, title: str):
        g = QGroupBox(title)
        lay = QVBoxLayout(g)
        lay.setSpacing(3)
        lay.setContentsMargins(6, 4, 6, 4)
        return g, lay

    def _row(self, label: str, widget) -> QWidget:
        """标签 + 控件行（整体 QWidget，便于整行启用/禁用）。"""
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lab = QLabel(label)
        lab.setMinimumWidth(_LABEL_W)
        lay.addWidget(lab)
        lay.addWidget(widget, 1)
        return row

    def _radioRow(self, label, texts, group: QButtonGroup,
                  start_id: int = 0) -> QWidget:
        """一组单选按钮行（整体 QWidget，便于整行显示/隐藏）。"""
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setSpacing(4)
        lay.setContentsMargins(0, 0, 0, 0)
        if label:
            lab = QLabel(label)
            lab.setMinimumWidth(_LABEL_W)
            lay.addWidget(lab)
        for i, t in enumerate(texts):
            rb = QRadioButton(t)
            group.addButton(rb, start_id + i)
            lay.addWidget(rb)
        lay.addStretch(1)
        return row

    def _divider(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        line.setStyleSheet("color:%s;" % theme.BORDER)
        return line

    # ---------- 联动 ----------

    def _onShapeChanged(self, _id: int) -> None:
        self._refreshAvailability()
        self.paramsChanged.emit()

    def _onModeChanged(self, _id: int) -> None:
        self._refreshAvailability()
        self.paramsChanged.emit()

    def _refreshAvailability(self) -> None:
        """按截面类型刷新各参数可用性（对齐插件 RefreshParamAvailability）。"""
        is_rect = self.shape() == "rectangle"
        is_chamfer = self.shape() == "chamfer"
        local = self.heightMode() == "local"

        # 倒角：隐藏高度模式、显示倒角模式
        self._rowMode.setVisible(not is_chamfer)
        self._rowChamfer.setVisible(is_chamfer)

        border_strength = not is_rect and not is_chamfer
        self._rowBorder.setEnabled(border_strength)
        self._rowStrength.setEnabled(border_strength)
        self._rowFreeH.setEnabled(is_chamfer or (not is_rect and local))
        # 表面特点（整体光滑/棱角清晰）对倒角无意义
        for b in self._finishGroup.buttons():
            b.setEnabled(not is_chamfer)
        self._notifyResize()

    # ---------- 取值 ----------

    def shape(self) -> str:
        i = self._shapeGroup.checkedId()
        from core.relief.region_relief import SHAPES
        return SHAPES[i] if 0 <= i < len(SHAPES) else "ellipse"

    def heightMode(self) -> str:
        return "local" if self._modeGroup.checkedId() == 1 else "free"

    def chamferMode(self) -> str:
        return "concave" if self._chamferGroup.checkedId() == 1 else "convex"

    def finish(self) -> str:
        return "sharp" if self._finishGroup.checkedId() == 1 else "smooth"

    def blend(self) -> str:
        i = self._blendGroup.checkedId()
        return ("add", "subtract", "replace")[i if 0 <= i < 3 else 0]

    def colorScope(self) -> str:
        """作用区域：ignore 颜色无效 / inside 颜色内 / outside 颜色外。"""
        i = self._scopeGroup.checkedId()
        return COLOR_SCOPES[i] if 0 <= i < len(COLOR_SCOPES) else "ignore"

    def params(self) -> RegionReliefParams:
        """把面板当前值打包为算法参数。"""
        return RegionReliefParams(
            shape=self.shape(),
            base_height=self._spinBaseH.value(),
            base_angle=self._spinBaseA.value(),
            height_mode=self.heightMode(),
            free_height=self._spinFreeH.value(),
            border_angle=self._spinBorder.value(),
            bulge_strength=self._spinStrength.value(),
            finish=self.finish(),
            blend=self.blend(),
            chamfer_mode=self.chamferMode(),
            color_scope=self.colorScope(),
        )

    def setStatus(self, text: str, ok: bool = True) -> None:
        """回写执行结果/提示（红色=异常）。"""
        self._labStatus.setText(text)
        self._labStatus.setStyleSheet(
            "color:%s;" % (theme.TEXT_DIM if ok else "#e53935"))


# ==================== Trim 修剪 ====================

class TrimParamPanel(ParamPanelBase):
    modeChanged = Signal(int)          # 0=快速修剪 1=边界修剪
    confirmed = Signal()
    cancelRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # 模式选择：快速修剪 / 边界修剪
        modeRow = QHBoxLayout()
        self._modeGroup = QButtonGroup(self)
        self._radioQuick = QRadioButton("快速修剪")
        self._radioBoundary = QRadioButton("边界修剪")
        self._radioQuick.setChecked(True)
        self._modeGroup.addButton(self._radioQuick, 0)
        self._modeGroup.addButton(self._radioBoundary, 1)
        self._modeGroup.idClicked.connect(self.modeChanged.emit)
        modeRow.addWidget(QLabel("模式"))
        modeRow.addWidget(self._radioQuick)
        modeRow.addWidget(self._radioBoundary)
        modeRow.addStretch(1)
        self._layout.addLayout(modeRow)

        self._hintLabel = QLabel("点击要修剪的线段，自动找交点")
        self._hintLabel.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        self._layout.addWidget(self._hintLabel)

        btnOk = self._button("确认执行", primary=True)
        btnOk.clicked.connect(self.confirmed.emit)
        self._layout.addWidget(btnOk)

        btnCancel = self._button2("取消")
        btnCancel.clicked.connect(self.cancelRequested.emit)
        self._layout.addWidget(btnCancel)
        self._layout.addStretch(1)

        # 连接模式切换信号更新提示文字
        self._modeGroup.idClicked.connect(self._updateHint)

    def _updateHint(self, mode: int) -> None:
        if mode == 0:
            self._hintLabel.setText("点击要修剪的线段，自动找交点")
        else:
            self._hintLabel.setText("先选边界线，再选要修剪的曲线")


# ==================== Array 阵列 ====================

class ArrayParamPanel(ParamPanelBase):
    """阵列参数面板：矩形/圆形/曲线三种模式。

    信号:
        modeChanged(int): 0=矩形 1=圆形 2=曲线
        paramsChanged(): 任一参数变更
        confirmed(): 点击"确认执行"
        cancelRequested(): 点击"取消"
    """
    modeChanged = Signal(int)
    paramsChanged = Signal()
    confirmed = Signal()
    cancelRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # 模式选择
        modeRow = QHBoxLayout()
        self._modeGroup = QButtonGroup(self)
        self._radioRect = QRadioButton("矩形")
        self._radioCircle = QRadioButton("圆形")
        self._radioCurve = QRadioButton("曲线")
        self._radioRect.setChecked(True)
        self._modeGroup.addButton(self._radioRect, 0)
        self._modeGroup.addButton(self._radioCircle, 1)
        self._modeGroup.addButton(self._radioCurve, 2)
        self._modeGroup.idClicked.connect(self._onModeChanged)
        modeRow.addWidget(QLabel("模式"))
        modeRow.addWidget(self._radioRect)
        modeRow.addWidget(self._radioCircle)
        modeRow.addWidget(self._radioCurve)
        modeRow.addStretch(1)
        self._layout.addLayout(modeRow)

        # ---- 矩形阵列参数 ----
        self._rectGroup = QGroupBox("矩形阵列")
        rectLay = QVBoxLayout(self._rectGroup)
        rectLay.setSpacing(3)
        rectLay.setContentsMargins(6, 4, 6, 4)
        self._spinRectX = self._ispin(3, 1, 100)
        self._spinRectY = self._ispin(3, 1, 100)
        self._spinRectGapX = self._spin(5.0, decimals=2, step=0.5)
        self._spinRectGapY = self._spin(5.0, decimals=2, step=0.5)
        self._chkXYEqual = QCheckBox("XY 等距")
        self._chkXYEqual.toggled.connect(self._onXYEqual)
        rectLay.addWidget(self._row("X 数量", self._spinRectX))
        rectLay.addWidget(self._row("Y 数量", self._spinRectY))
        rectLay.addWidget(self._row("X 间距", self._spinRectGapX))
        rectLay.addWidget(self._row("Y 间距", self._spinRectGapY))
        rectLay.addWidget(self._chkXYEqual)
        self._layout.addWidget(self._rectGroup)

        # ---- 圆形阵列参数 ----
        self._circleGroup = QGroupBox("圆形阵列")
        circleLay = QVBoxLayout(self._circleGroup)
        circleLay.setSpacing(3)
        circleLay.setContentsMargins(6, 4, 6, 4)
        self._spinCircleCnt = self._ispin(8, 2, 100)
        self._spinCircleR = self._spin(50.0, decimals=2, step=1.0)
        self._spinCircleR.setRange(0.0, 9999.0)
        self._spinCircleStart = self._spin(0.0, decimals=1, step=5.0)
        self._spinCircleRange = self._spin(360.0, decimals=1, step=5.0)
        self._chkCircleRotate = QCheckBox("旋转物体")
        self._chkCircleRotate.setChecked(True)
        self._chkCircleKnownR = QCheckBox("已知半径（选中对象为阵列之一）")
        self._chkCircleKnownR.toggled.connect(self._onKnownR)
        self._btnPickCenter = self._button2("拾取圆心")
        self._labCenter = QLabel("圆心：未拾取")
        self._labCenter.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        self._spinRingCount = self._ispin(1, 1, 20)
        self._spinRingSpacing = self._spin(10.0, decimals=2, step=1.0)
        circleLay.addWidget(self._row("数量", self._spinCircleCnt))
        circleLay.addWidget(self._row("半径", self._spinCircleR))
        circleLay.addWidget(self._row("起始角", self._spinCircleStart))
        circleLay.addWidget(self._row("角度范围", self._spinCircleRange))
        circleLay.addWidget(self._row("圈数", self._spinRingCount))
        circleLay.addWidget(self._row("圈间距", self._spinRingSpacing))
        circleLay.addWidget(self._chkCircleRotate)
        circleLay.addWidget(self._chkCircleKnownR)
        circleLay.addWidget(self._btnPickCenter)
        circleLay.addWidget(self._labCenter)
        self._layout.addWidget(self._circleGroup)

        # ---- 曲线阵列参数 ----
        self._curveGroup = QGroupBox("曲线阵列")
        curveLay = QVBoxLayout(self._curveGroup)
        curveLay.setSpacing(3)
        curveLay.setContentsMargins(6, 4, 6, 4)
        self._spinCurveCnt = self._ispin(10, 1, 100)
        self._spinCurveSpacing = self._spin(10.0, decimals=2, step=0.5)
        self._chkCurveFill = QCheckBox("铺满曲线")
        self._chkCurveRotate = QCheckBox("切向旋转")
        self._chkCurveOnCurve = QCheckBox("贴在曲线上")
        self._chkCurveOnCurve.setChecked(True)
        self._spinCurveStart = self._spin(0.0, decimals=1, step=5.0)
        self._spinCurveStart.setRange(0.0, 100.0)
        self._labCurveHint = QLabel("请拾取路径曲线")
        self._labCurveHint.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        self._btnPickCurve = self._button2("拾取曲线")
        curveLay.addWidget(self._row("数量", self._spinCurveCnt))
        curveLay.addWidget(self._row("间距", self._spinCurveSpacing))
        curveLay.addWidget(self._row("起点%", self._spinCurveStart))
        curveLay.addWidget(self._chkCurveFill)
        curveLay.addWidget(self._chkCurveRotate)
        curveLay.addWidget(self._chkCurveOnCurve)
        curveLay.addWidget(self._labCurveHint)
        curveLay.addWidget(self._btnPickCurve)
        self._layout.addWidget(self._curveGroup)

        # ---- 确认/取消按钮 ----
        btnOk = self._button("确认执行", primary=True)
        btnOk.clicked.connect(self.confirmed.emit)
        self._layout.addWidget(btnOk)
        btnCancel = self._button2("取消")
        btnCancel.clicked.connect(self.cancelRequested.emit)
        self._layout.addWidget(btnCancel)
        self._layout.addStretch(1)

        # 参数变更信号
        for w in (self._spinRectX, self._spinRectY, self._spinRectGapX,
                  self._spinRectGapY, self._spinCircleCnt, self._spinCircleR,
                  self._spinCircleStart, self._spinCircleRange,
                  self._spinRingCount, self._spinRingSpacing,
                  self._spinCurveCnt, self._spinCurveSpacing, self._spinCurveStart):
            w.valueChanged.connect(lambda *_: self.paramsChanged.emit())
        for chk in (self._chkCircleRotate, self._chkCircleKnownR,
                    self._chkCurveFill, self._chkCurveRotate, self._chkCurveOnCurve):
            chk.toggled.connect(lambda *_: self.paramsChanged.emit())

        self._onModeChanged(0)

    def _row(self, label: str, widget) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lab = QLabel(label)
        lab.setMinimumWidth(_LABEL_W)
        lay.addWidget(lab)
        lay.addWidget(widget, 1)
        return row

    def _onModeChanged(self, mode: int) -> None:
        self._rectGroup.setVisible(mode == 0)
        self._circleGroup.setVisible(mode == 1)
        self._curveGroup.setVisible(mode == 2)
        self.modeChanged.emit(mode)
        self._notifyResize()

    def _onXYEqual(self, on: bool) -> None:
        self._spinRectGapY.setEnabled(not on)
        if on:
            self._spinRectGapX.valueChanged.connect(
                self._spinRectGapY.setValue)
        else:
            try:
                self._spinRectGapX.valueChanged.disconnect(
                    self._spinRectGapY.setValue)
            except RuntimeError:
                pass

    def _onKnownR(self, on: bool) -> None:
        self._spinCircleR.setEnabled(not on)
        self._spinCircleStart.setEnabled(not on)

    # ---------- 取值 ----------
    def mode(self) -> int:
        return self._modeGroup.checkedId()

    # 矩形
    def rectX(self) -> int: return self._spinRectX.value()
    def rectY(self) -> int: return self._spinRectY.value()
    def rectGapX(self) -> float: return self._spinRectGapX.value()
    def rectGapY(self) -> float:
        return self._spinRectGapX.value() if self._chkXYEqual.isChecked() else self._spinRectGapY.value()

    # 圆形
    def circleCnt(self) -> int: return self._spinCircleCnt.value()
    def circleR(self) -> float: return self._spinCircleR.value()
    def circleStart(self) -> float: return self._spinCircleStart.value()
    def circleRange(self) -> float: return self._spinCircleRange.value()
    def circleRotate(self) -> bool: return self._chkCircleRotate.isChecked()
    def circleKnownR(self) -> bool: return self._chkCircleKnownR.isChecked()
    def ringCount(self) -> int: return self._spinRingCount.value()
    def ringSpacing(self) -> float: return self._spinRingSpacing.value()

    # 曲线
    def curveCnt(self) -> int: return self._spinCurveCnt.value()
    def curveSpacing(self) -> float: return self._spinCurveSpacing.value()
    def curveFill(self) -> bool: return self._chkCurveFill.isChecked()
    def curveStart(self) -> float: return self._spinCurveStart.value()
    def curveRotate(self) -> bool: return self._chkCurveRotate.isChecked()
    def curveOnCurve(self) -> bool: return self._chkCurveOnCurve.isChecked()

    def btnPickCurve(self):
        return self._btnPickCurve

    def setCurveLabel(self, text: str) -> None:
        self._labCurveHint.setText(text)

    def btnPickCenter(self):
        return self._btnPickCenter

    def setCenterLabel(self, text: str) -> None:
        self._labCenter.setText(text)


# ==================== Offset 偏移 ====================

class OffsetParamPanel(ParamPanelBase):
    """偏移参数面板。

    信号:
        distanceChanged(float): 偏移距离变更
        confirmed(): 点击"确认执行"
        cancelRequested(): 点击"取消"
    """
    distanceChanged = Signal(float)
    paramsChanged = Signal()
    confirmed = Signal()
    cancelRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._spinDist = self._spin(5.0, decimals=2, step=0.5)
        self._spinDist.setRange(0.0, 9999.0)
        self._addRow("距离", self._spinDist)

        self._chkBoth = QCheckBox("双侧偏移")
        self._chkInner = QCheckBox("保留内轮廓")
        rowBoth = QHBoxLayout()
        rowBoth.setSpacing(8)
        rowBoth.addWidget(self._chkBoth)
        rowBoth.addWidget(self._chkInner)
        rowBoth.addStretch(1)
        self._layout.addLayout(rowBoth)

        self._chkCap = QCheckBox("加盖")
        self._capGroup = QButtonGroup(self)
        self._radioButt = QRadioButton("平头盖")
        self._radioRound = QRadioButton("圆头盖")
        self._radioButt.setChecked(True)
        self._capGroup.addButton(self._radioButt, 0)
        self._capGroup.addButton(self._radioRound, 1)
        rowCap = QHBoxLayout()
        rowCap.setSpacing(8)
        rowCap.addWidget(self._chkCap)
        rowCap.addWidget(self._radioButt)
        rowCap.addWidget(self._radioRound)
        rowCap.addStretch(1)
        self._layout.addLayout(rowCap)

        btnOk = self._button("确认执行", primary=True)
        btnOk.clicked.connect(self.confirmed.emit)
        self._layout.addWidget(btnOk)

        btnCancel = self._button2("取消")
        btnCancel.clicked.connect(self.cancelRequested.emit)
        self._layout.addWidget(btnCancel)
        self._layout.addStretch(1)

        self._spinDist.valueChanged.connect(self.distanceChanged.emit)
        for chk in (self._chkBoth, self._chkCap, self._chkInner):
            chk.toggled.connect(lambda *_: self.paramsChanged.emit())
        self._capGroup.idClicked.connect(
            lambda *_: self.paramsChanged.emit())

    def distance(self) -> float:
        return self._spinDist.value()

    def bothSides(self) -> bool:
        return self._chkBoth.isChecked()

    def cap(self) -> bool:
        return self._chkCap.isChecked()

    def keepInner(self) -> bool:
        return self._chkInner.isChecked()

    def capStyle(self) -> str:
        """端盖样式：'butt' 平头盖 / 'round' 圆头盖。"""
        return "round" if self._capGroup.checkedId() == 1 else "butt"

    def setHint(self, text: str) -> None:
        pass


# ==================== Mirror 镜像 ====================

class MirrorParamPanel(ParamPanelBase):
    """镜像参数面板。

    交互：点选两点定义镜像轴 → 实时预览镜像副本 → 确认执行。
    选项：
        - 删除原图（默认不勾选：保留原图，复制一份镜像副本）
        - 副本数：默认 1（每次镜像生成 1 份副本）
    """
    paramsChanged = Signal()
    confirmed = Signal()
    cancelRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._chkDelete = QCheckBox("删除原图")
        self._layout.addWidget(self._chkDelete)

        self._hintLabel = QLabel("点选两点作为镜像轴，预览后确认")
        self._hintLabel.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        self._layout.addWidget(self._hintLabel)

        btnOk = self._button("确认执行", primary=True)
        btnOk.clicked.connect(self.confirmed.emit)
        self._layout.addWidget(btnOk)

        btnCancel = self._button2("取消")
        btnCancel.clicked.connect(self.cancelRequested.emit)
        self._layout.addWidget(btnCancel)
        self._layout.addStretch(1)

        self._chkDelete.toggled.connect(lambda *_: self.paramsChanged.emit())

    def deleteOriginal(self) -> bool:
        return self._chkDelete.isChecked()

    def setHint(self, text: str) -> None:
        self._hintLabel.setText(text)


# ==================== Extend 延伸 ====================

class ExtendParamPanel(ParamPanelBase):
    """延伸参数面板：快速延伸 / 选中目标延伸 两种模式。

    - 快速延伸(0)：选对象后点击靠近端点 → 自动延伸到文档中最近的相交曲线
    - 选中目标延伸(1)：选对象 → 点端定方向 → 点目标边界曲线 → 延伸到与目标交点
    """
    modeChanged = Signal(int)
    cancelRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._layout.addWidget(QLabel("延伸模式"))
        self._modeGroup = QButtonGroup(self)
        self._radioQuick = QRadioButton("快速延伸")
        self._radioTarget = QRadioButton("选中目标延伸")
        self._radioQuick.setChecked(True)
        self._modeGroup.addButton(self._radioQuick, 0)
        self._modeGroup.addButton(self._radioTarget, 1)
        self._modeGroup.idClicked.connect(self.modeChanged.emit)
        self._layout.addWidget(self._radioQuick)
        self._layout.addWidget(self._radioTarget)

        self._hintLabel = QLabel("点击曲线即延伸（较近端）；目标模式：点曲线→点目标")
        self._hintLabel.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        self._layout.addWidget(self._hintLabel)

        btnCancel = self._button2("取消")
        btnCancel.clicked.connect(self.cancelRequested.emit)
        self._layout.addWidget(btnCancel)
        self._layout.addStretch(1)

    def mode(self) -> int:
        """0=快速延伸 1=选中目标延伸。"""
        return self._modeGroup.checkedId()

    def setHint(self, text: str) -> None:
        self._hintLabel.setText(text)


# ==================== Outline 外轮廓 ====================

class OutlineParamPanel(ParamPanelBase):
    """外轮廓参数面板（对应 Rhino 插件 ToolPage.ShowOuterOutline 窗口）。

    信号：
    - paramsChanged(): 距离/重建/最大点数任一变化 → 命令端重算预览
    - confirmed(): 点击"确定"生成包裹线
    - cancelRequested(): 点击"取消"
    """
    paramsChanged = Signal()
    confirmed = Signal()
    cancelRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._spinDist = self._spin(2.0, decimals=3, step=0.5)
        self._chkRebuild = QCheckBox("抽稀去密集点（保形状）")
        self._chkRebuild.setChecked(True)
        self._spinTol = self._spin(0.01, decimals=3, step=0.005)
        form = QFormLayout()
        form.addRow("偏移距离", self._spinDist)
        form.addRow(self._chkRebuild)
        form.addRow("抽稀公差", self._spinTol)
        self._layout.addLayout(form)
        self._spinDist.valueChanged.connect(self._emitChanged)
        self._chkRebuild.stateChanged.connect(self._emitChanged)
        self._spinTol.valueChanged.connect(self._emitChanged)

        btnRow = QHBoxLayout()
        btnOk = self._button("确定", primary=True)
        btnOk.clicked.connect(self.confirmed.emit)
        btnCancel = self._button2("取消")
        btnCancel.clicked.connect(self.cancelRequested.emit)
        btnRow.addWidget(btnOk)
        btnRow.addWidget(btnCancel)
        self._layout.addLayout(btnRow)
        self._layout.addStretch(1)

    def _emitChanged(self, *_) -> None:
        self.paramsChanged.emit()

    def distance(self) -> float:
        return self._spinDist.value()

    def rebuild(self) -> bool:
        return self._chkRebuild.isChecked()

    def tolerance(self) -> float:
        return self._spinTol.value()

    def setHint(self, text: str) -> None:
        pass


# ==================== Align 对齐 ====================

class AlignParamPanel(ParamPanelBase):
    """对齐面板：上/下/左/右/居中五个按钮 + 均匀分布（X/Y 均匀勾选）。

    信号：
    - alignRequested(str): 对齐方式（"top"/"bottom"/"left"/"right"/"center"）
    - distributeRequested(): 点击"均匀分布"（按勾选的 X/Y 均匀执行）
    - cancelRequested(): 点击"取消"
    """
    alignRequested = Signal(str)
    distributeRequested = Signal()
    cancelRequested = Signal()

    # 网格布局位置（方向示意：上在中上，左/中/右一行，下在中下）
    _BUTTONS = (
        ("上对齐", "top", 0, 1),
        ("左对齐", "left", 1, 0),
        ("居中对齐", "center", 1, 1),
        ("右对齐", "right", 1, 2),
        ("下对齐", "bottom", 2, 1),
    )

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        grid = QGridLayout()
        grid.setSpacing(4)
        for text, mode, row, col in self._BUTTONS:
            b = self._button(text)
            b.setMinimumHeight(36)
            b.clicked.connect(lambda _, m=mode: self.alignRequested.emit(m))
            grid.addWidget(b, row, col)
        self._layout.addLayout(grid)

        # 均匀分布：X 均匀 / Y 均匀 勾选 + 执行按钮
        distRow = QHBoxLayout()
        self._chkX = QCheckBox("X 均匀")
        self._chkX.setChecked(True)
        self._chkY = QCheckBox("Y 均匀")
        btnDist = self._button("均匀分布", primary=True)
        btnDist.setMinimumHeight(36)
        btnDist.clicked.connect(self.distributeRequested.emit)
        distRow.addWidget(self._chkX)
        distRow.addWidget(self._chkY)
        distRow.addWidget(btnDist)
        self._layout.addLayout(distRow)

        btnCancel = self._button2("取消")
        btnCancel.clicked.connect(self.cancelRequested.emit)
        self._layout.addWidget(btnCancel)
        self._layout.addStretch(1)

    def xUniform(self) -> bool:
        return self._chkX.isChecked()

    def yUniform(self) -> bool:
        return self._chkY.isChecked()

    def setHint(self, text: str) -> None:
        pass
# ==================== PanelScale 面板缩放（三点+数值目标长度）====================

class PanelScaleParamPanel(ParamPanelBase):
    """缩放参数面板：点完基准点(P1)与参考起点(P2)后，
    显示当前参考长度和实时比例，并允许输入目标长度直接完成缩放。

    规则：第一个点(P1)不动，方向沿 P1→P2；输入目标长度 L' 后，
    新参考点 P3 = P1 + 单位(P1→P2) × L'，比例 k = L' / |P1P2|，
    直接对所有选中对象绕 P1 做等比缩放并结束命令。"""

    paramsChanged = Signal()              # 选项变化：通知命令重绘预览
    confirmed = Signal()                  # 用户按「应用目标长度」或在数值框回车
    cancelRequested = Signal()            # 取消命令

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self._labRef = QLabel("参考长度：—")
        self._labRef.setStyleSheet("color:%s;" % theme.TEXT)
        self._layout.addWidget(self._labRef)

        self._labRatio = QLabel("当前比例：—")
        self._labRatio.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        self._layout.addWidget(self._labRatio)

        # 目标长度输入行
        row = QHBoxLayout()
        row.setSpacing(4)
        lab = QLabel("目标长度")
        lab.setMinimumWidth(_LABEL_W)
        row.addWidget(lab)
        self._spinTarget = self._spin(value=100.0, decimals=3, step=1.0)
        self._spinTarget.setRange(1e-6, 9999999.0)
        self._spinTarget.setToolTip(
            "点完基准点和参考起点后填入目标长度。\n"
            "第一个点不动，沿P1→P2方向把P2拉到新长度，立即执行缩放。")
        row.addWidget(self._spinTarget, 1)
        self._layout.addLayout(row)

        btnApply = self._button("应用目标长度", primary=True)
        btnApply.setMinimumHeight(32)
        btnApply.clicked.connect(self.confirmed.emit)
        self._layout.addWidget(btnApply)

        btnCancel = self._button2("取消")
        btnCancel.clicked.connect(self.cancelRequested.emit)
        self._layout.addWidget(btnCancel)

        self._hint = QLabel(
            "① 点基准点 → ② 点参考起点 → ③ 拖动预览或填目标长度")
        self._hint.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        self._hint.setWordWrap(True)
        self._layout.addWidget(self._hint)
        self._layout.addStretch(1)

        # 回车在输入框：等效点击"应用"
        self._spinTarget.lineEdit().returnPressed.connect(self.confirmed.emit)

    # ---------- 命令调用 ----------

    def setReferenceLength(self, length: float | None) -> None:
        """命令在点击 P2 后调用：显示参考长度。"""
        if length is None or length <= 0:
            self._labRef.setText("参考长度：—")
        else:
            self._labRef.setText(f"参考长度：{length:.3f}")

    def setCurrentRatio(self, ratio: float | None) -> None:
        """命令在 P2 之后的鼠标移动时调用：显示当前光标对应的比例。"""
        if ratio is None:
            self._labRatio.setText("当前比例：—")
        else:
            self._labRatio.setText(f"当前比例：{ratio:.4f}×")

    def targetLength(self) -> float:
        return float(self._spinTarget.value())

    def setTargetLength(self, value: float) -> None:
        self._spinTarget.setValue(max(1e-6, float(value)))

    def setHint(self, text: str) -> None:
        self._hint.setText(text)

"""状态栏：坐标、长度面积、正交与线宽开关。"""
from __future__ import annotations
from PySide6.QtWidgets import (QStatusBar, QLabel, QPushButton, QDoubleSpinBox)
from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
import ui.styles.theme as theme
from i18n import tr, languageChanged


class StatusBarWidget(QStatusBar):
    orthoToggled = Signal(bool)
    editCurveToggled = Signal(bool)

    lineWidthChanged = Signal(float)
    measureToggled = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setSizeGripEnabled(False)
        self.setFixedHeight(28)

        # 坐标前缀：原“工作平面”开关无任何功能，改为只读标签
        self._lblCoord = QLabel(tr("坐标"))
        self._lblCoord.setFixedWidth(48)
        self._lblCoord.setStyleSheet("color: #666;")

        self._lblX = QLabel("X: 0.000")
        self._lblY = QLabel("Y: 0.000")
        self._lblZ = QLabel("Z: 0.000")
        for lbl in (self._lblX, self._lblY, self._lblZ):
            lbl.setFixedWidth(88)

        self._btnOrtho = QPushButton(tr("正交"))
        self._btnEditCurve = QPushButton(tr("编辑曲线"))
        self._btnGizmo = QPushButton(tr("操作轴"))
        _toggleStyle = self._btnStyle()
        for b, w in [(self._btnOrtho, 50), (self._btnEditCurve, 72),
                     (self._btnGizmo, 62)]:
            b.setCheckable(True)
            b.setFixedHeight(22)
            b.setMinimumWidth(w)
            b.setStyleSheet(_toggleStyle)
            b.setToolTip(tr(b.text()))
        self._btnOrtho.setChecked(False)
        self._btnGizmo.setChecked(True)
        # 编辑曲线开关：打开后选中曲线才显示控制点，可点选/框选/拖动控制点
        self._btnEditCurve.setToolTip(
            tr("编辑曲线：打开后选中曲线显示控制点，点选/框选控制点即编辑，"
               "可用操作轴批量变换控制点"))

        self._lblLineW = QLabel(tr("线宽:"))
        self._spinLineW = QDoubleSpinBox()
        self._spinLineW.setRange(0.5, 20.0)
        self._spinLineW.setSingleStep(0.5)
        self._spinLineW.setValue(1.0)
        self._spinLineW.setFixedWidth(64)
        self._spinLineW.setFixedHeight(22)

        # 长度/面积：标题为开关按钮，关闭时不计算直接显示 "--"
        self._btnLen = QPushButton(tr("长度"))
        self._btnArea = QPushButton(tr("面积"))
        for b, w in [(self._btnLen, 48), (self._btnArea, 48)]:
            b.setCheckable(True)
            b.setChecked(False)
            b.setFixedHeight(22)
            b.setMinimumWidth(w)
            b.setStyleSheet(self._btnStyle())
            b.setToolTip(tr("开关打开后显示选中对象的测量值"))
        self._lblLen = QLabel("--")
        self._lblArea = QLabel("--")
        self._lblLen.setFixedWidth(104)
        self._lblArea.setFixedWidth(104)
        self._lastLen: float | None = None
        self._lastArea: float | None = None

        self.addWidget(self._lblCoord)
        self.addWidget(self._lblX)
        self.addWidget(self._lblY)
        self.addWidget(self._lblZ)

        self.addWidget(self._btnOrtho)
        self.addWidget(self._btnEditCurve)

        self.addWidget(self._btnGizmo)
        self.addWidget(self._lblLineW)
        self.addWidget(self._spinLineW)
        self.addWidget(self._btnLen)
        self.addWidget(self._lblLen)
        self.addWidget(self._btnArea)
        self.addWidget(self._lblArea)

        self._btnOrtho.toggled.connect(self.orthoToggled.emit)
        self._btnEditCurve.toggled.connect(self.editCurveToggled.emit)
        self._spinLineW.valueChanged.connect(self.lineWidthChanged.emit)
        self._btnLen.toggled.connect(self._onMeasureToggled)
        self._btnArea.toggled.connect(self._onMeasureToggled)

        # 最右下角：当前软件版本号与版本日期（常驻，不随左侧内容挤压）
        from version import APP_VERSION, APP_VERSION_DATE
        self._lblVersion = QLabel(f"{APP_VERSION} · {APP_VERSION_DATE}")
        self._lblVersion.setStyleSheet("color: #666; padding: 0 8px;")
        self._lblVersion.setToolTip(tr(f"版本 {APP_VERSION}（更新日期 {APP_VERSION_DATE}）"))
        self.addPermanentWidget(self._lblVersion)

        languageChanged().connect(self.retranslateUi)

    def retranslateUi(self) -> None:
        self._lblCoord.setText(tr("坐标"))
        self._btnOrtho.setText(tr("正交"))
        self._btnEditCurve.setText(tr("编辑曲线"))
        self._btnGizmo.setText(tr("操作轴"))
        self._lblLineW.setText(tr("线宽:"))
        self._btnLen.setText(tr("长度"))
        self._btnArea.setText(tr("面积"))
        self._btnOrtho.setToolTip(tr("正交"))
        self._btnGizmo.setToolTip(tr("操作轴"))
        self._btnLen.setToolTip(tr("开关打开后显示选中对象的测量值"))
        self._btnArea.setToolTip(tr("开关打开后显示选中对象的测量值"))
        self._btnEditCurve.setToolTip(
            tr("编辑曲线：打开后选中曲线显示控制点，点选/框选控制点即编辑，"
               "可用操作轴批量变换控制点"))

    def isOrthoChecked(self) -> bool:
        return self._btnOrtho.isChecked()

    def setOrthoChecked(self, on: bool) -> None:
        self._btnOrtho.setChecked(on)

    def isEditCurveChecked(self) -> bool:
        return self._btnEditCurve.isChecked()

    @classmethod
    def _btnStyle(cls) -> str:
        hl = theme.PRIMARY
        hover = QColor(hl).lighter(175).name()
        return (
            "QPushButton{background:#d0d4d8;color:#333;border:none;"
            "padding:0 4px;border-radius:2px;}"
            f"QPushButton:checked{{background:{hl};color:white;}}"
            f"QPushButton:hover{{background:{hover};}}"
            f"QPushButton:checked:hover{{background:{QColor(hl).darker(110).name()};}}"
        )

    def refreshStyle(self) -> None:
        """主题色变化后刷新正交/操作轴/长度/面积按钮高亮样式。"""
        style = self._btnStyle()
        for btn in (self._btnOrtho,
                    self._btnEditCurve, self._btnGizmo, self._btnLen,
                    self._btnArea):
            btn.setStyleSheet(style)

    def setCoordinates(self, x: float, y: float, z: float = 0.0) -> None:
        self._lblX.setText(f"X: {x:.3f}")
        self._lblY.setText(f"Y: {y:.3f}")
        self._lblZ.setText(f"Z: {z:.3f}")

    def setLineWidth(self, w: float) -> None:
        """回显选中对象的线宽（阻断信号，避免反向再刷一遍对象）。"""
        self._spinLineW.blockSignals(True)
        self._spinLineW.setValue(w)
        self._spinLineW.blockSignals(False)

    def setLengthArea(self, length: float | None, area: float | None) -> None:
        self._lastLen = length
        self._lastArea = area
        self._refreshMeasureLabels()

    def measureEnabled(self) -> bool:
        """长度或面积任一开关打开即为需要计算。"""
        return self._btnLen.isChecked() or self._btnArea.isChecked()

    def _onMeasureToggled(self, _checked: bool) -> None:
        self._refreshMeasureLabels()
        self.measureToggled.emit()

    def _refreshMeasureLabels(self) -> None:
        if self._btnLen.isChecked() and self._lastLen is not None:
            self._lblLen.setText(f"{self._lastLen:.3f}")
        else:
            self._lblLen.setText("--")
        if self._btnArea.isChecked() and self._lastArea is not None:
            self._lblArea.setText(f"{self._lastArea:.3f}")
        else:
            self._lblArea.setText("--")

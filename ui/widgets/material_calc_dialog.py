"""材料成本计算器：按形状输入尺寸自动算体积 → 成本 = 体积 × 密度 × 单价。

- 尺寸输入均为整数毫米（mm）；圆柱为 直径、长度。
- 体积按形状自动计算，也可直接输入体积（mm³）。
- 单价支持按重量（元/kg、元/g）与按体积（元/cm³、元/mm³）计价。
- 损耗支持：无 / 百分比（按成本加）/ 固定金额（元/件）。
"""
from __future__ import annotations
import math
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
                               QLabel, QComboBox, QDoubleSpinBox, QSpinBox,
                               QRadioButton, QButtonGroup, QStackedWidget,
                               QGroupBox, QWidget, QFrame, QPushButton)
from PySide6.QtCore import Qt
import ui.styles.theme as theme


# 常见材料密度（g/cm³），选材料自动填入密度，也可自行修改
MATERIALS = [
    ("四氟板（PTFE）", 2.20),
    ("尼龙 PA6", 1.14),
    ("尼龙 PA66", 1.15),
    ("聚甲醛 POM（赛钢）", 1.41),
    ("亚克力 PMMA（有机玻璃）", 1.19),
    ("聚碳酸酯 PC", 1.20),
    ("电木（酚醛树脂）", 1.45),
    ("紫铜 T2", 8.96),
    ("黄铜 H62", 8.50),
    ("铝 6061", 2.73),
    ("铝合金 7075", 2.81),
    ("不锈钢 304", 7.93),
    ("45# 钢", 7.85),
    ("铸铁", 7.20),
]

# 计价方式：显示名 → 计算单位（kg/g 按重量；cm3/mm3 按体积）
PRICE_UNITS = [
    ("元/kg（按重量）", "kg"),
    ("元/g（按重量）", "g"),
    ("元/cm³（按体积）", "cm3"),
    ("元/mm³（按体积）", "mm3"),
]

_DIALOG_QSS = """
QDialog {
    background: #ffffff;
}
QLabel, QRadioButton, QCheckBox {
    background: transparent;
}
QRadioButton {
    font-size: 12px;
    spacing: 4px;
    background: transparent;
}
QRadioButton::indicator {
    width: 13px;
    height: 13px;
    border: 1px solid #999;
    border-radius: 6px;
    background: white;
}
QRadioButton::indicator:checked {
    background: %s;
    border-color: %s;
}
QGroupBox {
    border: 1px solid #d5d8dd;
    border-radius: 6px;
    margin-top: 10px;
    background: transparent;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
    color: #444;
    font-weight: bold;
    background: transparent;
}
QStackedWidget, QWidget#transparent {
    background: transparent;
}
QFrame#hr {
    background: #d8d8d8;
    border: none;
}
""" % (theme.PRIMARY, theme.PRIMARY)


def _makeDimSpin(value: int) -> QSpinBox:
    """整数毫米尺寸输入框。"""
    spin = QSpinBox()
    spin.setRange(0, 1000000)
    spin.setSuffix(" mm")
    spin.setValue(value)
    return spin


def _makeHLine() -> QFrame:
    line = QFrame()
    line.setObjectName("hr")
    line.setFrameShape(QFrame.HLine)
    line.setFixedHeight(1)
    return line


class MaterialCalcDialog(QDialog):
    """材料成本计算器。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("材料成本计算器")
        self.setModal(True)
        self.setFixedWidth(440)
        self.setStyleSheet(_DIALOG_QSS)
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 14)
        root.setSpacing(10)

        # ── 组1：材料与尺寸 ──────────────────────────────
        g1 = QGroupBox("材料与尺寸")
        g1f = QFormLayout(g1)
        g1f.setContentsMargins(12, 14, 12, 12)
        g1f.setSpacing(9)
        g1f.setLabelAlignment(Qt.AlignRight)

        # 材料选择（内置常见材料密度）
        self._matCombo = QComboBox()
        for name, den in MATERIALS:
            self._matCombo.addItem(name, den)
        self._matCombo.currentIndexChanged.connect(self._onMaterialChanged)
        g1f.addRow("材料", self._matCombo)

        # 密度（g/cm³，可自定义）
        self._densitySpin = QDoubleSpinBox()
        self._densitySpin.setRange(0.01, 100.0)
        self._densitySpin.setDecimals(3)
        self._densitySpin.setSuffix(" g/cm³")
        self._densitySpin.setValue(MATERIALS[0][1])
        g1f.addRow("密度", self._densitySpin)

        # 形状：单选框一排（长方体 / 圆柱 / 直接输入体积）
        shapeRow = QHBoxLayout()
        shapeRow.setSpacing(10)
        self._shapeGroup = QButtonGroup(self)
        self._shapeBox = QRadioButton("长方体 / 板料")
        self._shapeCyl = QRadioButton("圆柱 / 棒料")
        self._shapeManual = QRadioButton("直接输入体积")
        for rb in (self._shapeBox, self._shapeCyl, self._shapeManual):
            self._shapeGroup.addButton(rb)
            rb.toggled.connect(self._onShapeChanged)
            shapeRow.addWidget(rb)
        shapeRow.addStretch(1)
        g1f.addRow("形状", shapeRow)

        # 尺寸输入（整数 mm；按形状切换）
        self._dimStack = QStackedWidget()

        boxW = QWidget()
        boxW.setObjectName("transparent")
        boxLay = QHBoxLayout(boxW)
        boxLay.setContentsMargins(0, 0, 0, 0)
        boxLay.setSpacing(4)
        self._boxLen = _makeDimSpin(100)   # 长
        self._boxWid = _makeDimSpin(100)   # 宽
        self._boxHei = _makeDimSpin(10)    # 高
        for lbl, spin in (("长", self._boxLen), ("宽", self._boxWid),
                          ("高", self._boxHei)):
            boxLay.addWidget(QLabel(lbl))
            boxLay.addWidget(spin, 1)
        self._dimStack.addWidget(boxW)

        cylW = QWidget()
        cylW.setObjectName("transparent")
        cylLay = QHBoxLayout(cylW)
        cylLay.setContentsMargins(0, 0, 0, 0)
        cylLay.setSpacing(4)
        self._cylDia = _makeDimSpin(10)    # 直径
        self._cylLen = _makeDimSpin(20)    # 长度
        for lbl, spin in (("直径", self._cylDia), ("长度", self._cylLen)):
            cylLay.addWidget(QLabel(lbl))
            cylLay.addWidget(spin, 1)
        self._dimStack.addWidget(cylW)

        emptyDimW = QWidget()
        emptyDimW.setObjectName("transparent")
        self._dimStack.addWidget(emptyDimW)   # 直接输入体积：无尺寸行
        g1f.addRow("尺寸", self._dimStack)

        # 体积（形状自动算 或 直接输入；标题只出现一次）
        self._volStack = QStackedWidget()
        self._volAutoLbl = QLabel("—")
        self._volAutoLbl.setStyleSheet("color: #333;")
        self._volStack.addWidget(self._volAutoLbl)          # index0 自动计算
        manW = QWidget()
        manW.setObjectName("transparent")
        manLay = QHBoxLayout(manW)
        manLay.setContentsMargins(0, 0, 0, 0)
        manLay.setSpacing(6)
        self._volManual = QSpinBox()
        self._volManual.setRange(0, 1000000000)
        self._volManual.setSuffix(" mm³")
        self._volManual.setValue(0)
        manLay.addWidget(self._volManual, 1)
        self._volCm3Lbl = QLabel("= 0.00 cm³")
        manLay.addWidget(self._volCm3Lbl)
        self._volStack.addWidget(manW)                      # index1 手动输入
        g1f.addRow("体积", self._volStack)

        root.addWidget(g1)

        # ── 组2：价格与损耗 ──────────────────────────────
        g2 = QGroupBox("价格与损耗")
        g2f = QFormLayout(g2)
        g2f.setContentsMargins(12, 14, 12, 12)
        g2f.setSpacing(9)
        g2f.setLabelAlignment(Qt.AlignRight)

        # 单价 + 计价方式（按重量 / 按体积）
        priceRow = QHBoxLayout()
        priceRow.setSpacing(6)
        self._priceSpin = QDoubleSpinBox()
        self._priceSpin.setRange(0.0, 1.0e9)
        self._priceSpin.setDecimals(2)
        self._priceSpin.setValue(0.0)
        priceRow.addWidget(self._priceSpin, 1)
        self._priceUnit = QComboBox()
        for name, _u in PRICE_UNITS:
            self._priceUnit.addItem(name)
        priceRow.addWidget(self._priceUnit)
        g2f.addRow("单价", priceRow)

        # 数量
        self._qtySpin = QSpinBox()
        self._qtySpin.setRange(1, 99999)
        self._qtySpin.setValue(1)
        self._qtySpin.setSuffix(" 件")
        g2f.addRow("数量", self._qtySpin)

        # 损耗：单选框（无 / 百分比 / 固定金额）
        lossRow = QHBoxLayout()
        lossRow.setSpacing(10)
        self._lossGroup = QButtonGroup(self)
        self._lossNone = QRadioButton("无")
        self._lossPct = QRadioButton("百分比")
        self._lossPctSpin = QDoubleSpinBox()
        self._lossPctSpin.setRange(0.0, 1000.0)
        self._lossPctSpin.setDecimals(2)
        self._lossPctSpin.setSuffix(" %")
        self._lossPctSpin.setValue(15.0)
        self._lossFix = QRadioButton("固定金额")
        self._lossFixSpin = QDoubleSpinBox()
        self._lossFixSpin.setRange(0.0, 1.0e9)
        self._lossFixSpin.setDecimals(2)
        self._lossFixSpin.setSuffix(" 元/件")
        self._lossFixSpin.setValue(0.0)
        for rb in (self._lossNone, self._lossPct, self._lossFix):
            self._lossGroup.addButton(rb)
            rb.toggled.connect(self._onLossChanged)
            lossRow.addWidget(rb)
        lossRow.addWidget(self._lossPctSpin)
        lossRow.addWidget(self._lossFixSpin)
        lossRow.addStretch(1)
        g2f.addRow("损耗", lossRow)

        root.addWidget(g2)

        # ── 结果卡片 ────────────────────────────────────
        res = QFrame()
        res.setStyleSheet(
            "QFrame { background: #ffffff; border: 1px solid #e1e3e7;"
            " border-radius: 6px; }")
        rl = QVBoxLayout(res)
        rl.setContentsMargins(14, 10, 14, 10)
        rl.setSpacing(6)
        self._massLbl = QLabel("材料质量：0.000 g（0.0000 kg）")
        self._massLbl.setStyleSheet("font-size: 12px; color: #555;")
        self._costLbl = QLabel("总成本：0.00 元")
        self._costLbl.setStyleSheet(
            "font-size: 18px; font-weight: bold; color: %s;" % theme.PRIMARY)
        rl.addWidget(self._massLbl)
        rl.addWidget(self._costLbl)
        root.addWidget(res)

        # ── 说明与按钮 ──────────────────────────────────
        hint = QLabel("成本 = 体积 × 密度 × 单价\n"
                      "体积由形状尺寸自动算出，也可直接输入；计价支持按重量"
                      "（元/kg、元/g）或按体积（元/cm³、元/mm³）；损耗按百分比"
                      "或固定金额（元/件）计入")
        hint.setStyleSheet("color: #888; font-size: 11px;")
        hint.setWordWrap(True)
        root.addWidget(hint)

        root.addWidget(_makeHLine())

        btnRow = QHBoxLayout()
        btnRow.addStretch(1)
        closeBtn = QPushButton("关闭")
        closeBtn.setFixedWidth(90)
        closeBtn.clicked.connect(self.accept)
        btnRow.addWidget(closeBtn)
        root.addLayout(btnRow)

        # ── 实时计算信号 ────────────────────────────────
        for spin in (self._boxLen, self._boxWid, self._boxHei,
                     self._cylDia, self._cylLen):
            spin.valueChanged.connect(self._recalc)
        self._volManual.valueChanged.connect(self._recalc)
        self._densitySpin.valueChanged.connect(self._recalc)
        self._priceSpin.valueChanged.connect(self._recalc)
        self._priceUnit.currentIndexChanged.connect(self._recalc)
        self._qtySpin.valueChanged.connect(self._recalc)
        self._lossPctSpin.valueChanged.connect(self._recalc)
        self._lossFixSpin.valueChanged.connect(self._recalc)
        # 初始选中（此时所有控件已建好，避免构造期回调访问未建控件）
        self._shapeBox.setChecked(True)
        self._lossPct.setChecked(True)   # 默认损耗 15%（百分比）
        self._onLossChanged()
        self._recalc()

    def _onMaterialChanged(self) -> None:
        den = self._matCombo.currentData()
        if den:
            self._densitySpin.setValue(den)

    def _onShapeChanged(self) -> None:
        if self._shapeManual.isChecked():
            self._dimStack.setCurrentIndex(2)
            self._volStack.setCurrentIndex(1)
        else:
            self._dimStack.setCurrentIndex(0 if self._shapeBox.isChecked() else 1)
            self._volStack.setCurrentIndex(0)
        self._recalc()

    def _onLossChanged(self) -> None:
        """损耗参数框只显示当前选中的方式。"""
        self._lossPctSpin.setVisible(self._lossPct.isChecked())
        self._lossFixSpin.setVisible(self._lossFix.isChecked())
        self._recalc()

    def _volumeMm3(self) -> float:
        """按当前形状与尺寸计算体积（mm³）。"""
        if self._shapeManual.isChecked():
            return float(self._volManual.value())
        if self._shapeCyl.isChecked():
            d = float(self._cylDia.value())
            h = float(self._cylLen.value())
            return math.pi * (d / 2.0) ** 2 * h
        return float(self._boxLen.value() * self._boxWid.value()
                     * self._boxHei.value())

    def _recalc(self) -> None:
        volMm3 = self._volumeMm3()
        volCm3 = volMm3 / 1000.0
        if self._shapeManual.isChecked():
            self._volCm3Lbl.setText("= %.2f cm³" % volCm3)
        else:
            self._volAutoLbl.setText("%.0f mm³（%.2f cm³）" % (volMm3, volCm3))
        massG = volCm3 * self._densitySpin.value()
        massKg = massG / 1000.0

        unit = PRICE_UNITS[self._priceUnit.currentIndex()][1]
        price = self._priceSpin.value()
        qty = self._qtySpin.value()
        if unit == "kg":
            cost = massKg * price
        elif unit == "g":
            cost = massG * price
        elif unit == "cm3":
            cost = volCm3 * price
        else:
            cost = volMm3 * price
        cost *= qty

        # 损耗
        if self._lossPct.isChecked():
            cost *= 1.0 + self._lossPctSpin.value() / 100.0
        elif self._lossFix.isChecked():
            cost += self._lossFixSpin.value() * qty

        self._massLbl.setText("材料质量：%.3f g（%.4f kg）" % (massG, massKg))
        self._costLbl.setText("总成本：%.2f 元" % cost)

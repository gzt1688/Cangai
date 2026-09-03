"""操作轴精确输入对话框：复刻原项目 GizmoInputDialog。

单击操作轴的轴/手柄时弹出：数值输入 + “复制对象”开关；
缩放模式额外提供 数值含义（绝对尺寸/增量/比例）与 作用轴（单轴/所有轴）选择。
"""
from __future__ import annotations
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QDoubleSpinBox, QComboBox, QCheckBox,
                               QDialogButtonBox)


class GizmoInputDialog(QDialog):
    def __init__(self, title: str, label: str, initial: float,
                 minV: float, maxV: float, suffix: str = "",
                 decimals: int = 3, scaleModes: bool = False,
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(280)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        row = QHBoxLayout()
        row.addWidget(QLabel(label))
        self._spin = QDoubleSpinBox()
        self._spin.setRange(minV, maxV)
        self._spin.setDecimals(decimals)
        self._spin.setValue(initial)
        self._spin.setSuffix(suffix)
        self._spin.setFixedWidth(140)
        self._spin.selectAll()
        row.addWidget(self._spin)
        row.addStretch()
        layout.addLayout(row)

        self._modeCombo = None
        self._axisCombo = None
        if scaleModes:
            mrow = QHBoxLayout()
            mrow.addWidget(QLabel("数值含义:"))
            self._modeCombo = QComboBox()
            self._modeCombo.addItems(["绝对尺寸", "增量", "缩放比例"])
            mrow.addWidget(self._modeCombo)
            mrow.addStretch()
            layout.addLayout(mrow)

            arow = QHBoxLayout()
            arow.addWidget(QLabel("作用范围:"))
            self._axisCombo = QComboBox()
            self._axisCombo.addItems(["单轴", "所有轴"])
            arow.addWidget(self._axisCombo)
            arow.addStretch()
            layout.addLayout(arow)

        self._copyCheck = QCheckBox("复制对象（变换应用于副本）")
        layout.addWidget(self._copyCheck)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def value(self) -> float:
        return self._spin.value()

    def copyRequested(self) -> bool:
        return self._copyCheck.isChecked()

    def scaleMode(self) -> int:
        """0=绝对尺寸 1=增量 2=缩放比例（非缩放对话框返回 0）。"""
        return self._modeCombo.currentIndex() if self._modeCombo else 0

    def uniformAxis(self) -> bool:
        """True=所有轴（等比）。"""
        return bool(self._axisCombo and self._axisCombo.currentIndex() == 1)

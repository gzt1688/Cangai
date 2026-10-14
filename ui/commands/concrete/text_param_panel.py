"""文字命令参数面板（移植自 Rhino TextEditorDialog，融入现有参数面板机制）。

参数变化发 paramChanged 信号，命令实时重算布局并更新预览对象。
"""
from __future__ import annotations
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QVBoxLayout, QHBoxLayout, QTextEdit,
                                QCheckBox, QRadioButton, QButtonGroup,
                                QComboBox, QLabel)

from ..param_panels import ParamPanelBase
import ui.styles.theme as theme


class TextParamPanel(ParamPanelBase):
    paramChanged = Signal()
    confirmRequested = Signal()
    cancelRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._updating = False
        self._build()
        self._loadFonts()

    def _build(self) -> None:
        self._txt = QTextEdit()
        self._txt.setPlainText("文字")
        self._txt.setFixedHeight(40)
        self._txt.textChanged.connect(self._onChanged)
        self._layout.addWidget(self._txt)

        row = QHBoxLayout(); row.setSpacing(4)
        row.addWidget(QLabel("排布"))
        self._grpDir = QButtonGroup(self)
        self._rbH = QRadioButton("水平")
        self._rbV = QRadioButton("垂直")
        self._rbH.setChecked(True)
        self._grpDir.addButton(self._rbH, 0)
        self._grpDir.addButton(self._rbV, 1)
        self._grpDir.idClicked.connect(lambda _i: self._onChanged())
        row.addWidget(self._rbH); row.addWidget(self._rbV)
        row.addStretch(1)
        self._layout.addLayout(row)

        self._cbBuiltin = QComboBox()
        self._cbBuiltin.currentIndexChanged.connect(self._onBuiltinChanged)
        self._addRow("内置字体", self._cbBuiltin)
        self._cbSystem = QComboBox()
        self._cbSystem.currentIndexChanged.connect(self._onSystemChanged)
        self._addRow("系统字体", self._cbSystem)

        self._spinHeight = self._spin(10.0, decimals=1, step=0.5)
        self._spinHeight.setRange(0.1, 1000.0)
        self._spinHeight.valueChanged.connect(self._onChanged)
        self._addRow("字高", self._spinHeight)
        self._spinSpacing = self._spin(1.0, decimals=1, step=0.1)
        self._spinSpacing.setRange(-100.0, 100.0)
        self._spinSpacing.valueChanged.connect(self._onChanged)
        self._addRow("字间距", self._spinSpacing)
        self._spinRot = self._spin(0.0, decimals=0, step=1.0)
        self._spinRot.setRange(0.0, 360.0)
        self._spinRot.valueChanged.connect(self._onChanged)
        self._addRow("旋转", self._spinRot)

        row = QHBoxLayout(); row.setSpacing(4)
        self._chkCurve = QCheckBox("沿曲线排列")
        self._chkCurve.toggled.connect(self._onCurveToggled)
        row.addWidget(self._chkCurve)
        self._chkReverse = QCheckBox("反向")
        self._chkReverse.toggled.connect(self._onChanged)
        row.addWidget(self._chkReverse)
        self._chkMirror = QCheckBox("镜像")
        self._chkMirror.toggled.connect(self._onChanged)
        row.addWidget(self._chkMirror)
        self._layout.addLayout(row)

        row = QHBoxLayout(); row.setSpacing(4)
        self._grpOrient = QButtonGroup(self)
        self._rbPerp = QRadioButton("垂直于曲线")
        self._rbVert = QRadioButton("保持竖直")
        self._rbPerp.setChecked(True)
        self._grpOrient.addButton(self._rbPerp, 0)
        self._grpOrient.addButton(self._rbVert, 1)
        self._grpOrient.idClicked.connect(lambda _i: self._onChanged())
        row.addWidget(self._rbPerp); row.addWidget(self._rbVert)
        self._layout.addLayout(row)

        self._spinOffset = self._spin(0.0, decimals=1, step=0.5)
        self._spinOffset.setRange(-100.0, 100.0)
        self._spinOffset.valueChanged.connect(self._onChanged)
        self._addRow("偏移", self._spinOffset)
        self._spinStart = self._spin(0.0, decimals=2, step=0.01)
        self._spinStart.setRange(0.0, 1.0)
        self._spinStart.valueChanged.connect(self._onChanged)
        self._addRow("起始", self._spinStart)

        btn = self._button("确认完成", primary=True)
        btn.clicked.connect(self.confirmRequested.emit)
        self._layout.addWidget(btn)
        self._layout.addStretch(1)
        self._setCurveEnabled(False)

    def _loadFonts(self) -> None:
        from core.text.font import FontManager
        fm = FontManager.instance()
        self._cbBuiltin.blockSignals(True)
        self._cbBuiltin.clear()
        self._cbBuiltin.addItem("-- 选择内置字体 --", "")
        for name, path in fm.listBuiltinFonts():
            self._cbBuiltin.addItem(name, path)
        self._cbBuiltin.blockSignals(False)
        if self._cbBuiltin.count() > 1:
            idx = self._cbBuiltin.findText("细圆单线")
            if idx < 0:
                idx = 1
            self._cbBuiltin.setCurrentIndex(idx)
        # 系统字体延迟加载：扫描系统字体目录+fontTools 读族名较慢，
        # 先占位让面板立即打开，事件循环空闲时再填充
        self._cbSystem.addItem("-- 加载中 --", "")
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._loadSystemFonts)

    def _loadSystemFonts(self) -> None:
        from core.text.font import FontManager
        fm = FontManager.instance()
        self._cbSystem.blockSignals(True)
        self._cbSystem.clear()
        self._cbSystem.addItem("-- 选择系统字体 --", "")
        for name, path in fm.listSystemFonts():
            self._cbSystem.addItem(name, path)
        self._cbSystem.blockSignals(False)

    def _onBuiltinChanged(self) -> None:
        if self._cbBuiltin.currentIndex() > 0:
            self._cbSystem.blockSignals(True)
            self._cbSystem.setCurrentIndex(0)
            self._cbSystem.blockSignals(False)
        self._onChanged()

    def _onSystemChanged(self) -> None:
        if self._cbSystem.currentIndex() > 0:
            self._cbBuiltin.blockSignals(True)
            self._cbBuiltin.setCurrentIndex(0)
            self._cbBuiltin.blockSignals(False)
        self._onChanged()

    def _onChanged(self) -> None:
        if self._updating:
            return
        self.paramChanged.emit()

    def _onCurveToggled(self, on: bool) -> None:
        self._setCurveEnabled(on)
        self._onChanged()

    def _setCurveEnabled(self, on: bool) -> None:
        for w in (self._chkReverse, self._chkMirror, self._rbPerp,
                  self._rbVert, self._spinOffset, self._spinStart):
            w.setEnabled(on)

    # ---------- 取值 ----------

    def text(self) -> str:
        return self._txt.toPlainText()

    def vertical(self) -> bool:
        return self._rbV.isChecked()

    def fontPath(self) -> str:
        if self._cbBuiltin.currentIndex() > 0:
            return self._cbBuiltin.currentData()
        if self._cbSystem.currentIndex() > 0:
            return self._cbSystem.currentData()
        return ""

    def charHeight(self) -> float:
        return self._spinHeight.value()

    def charSpacing(self) -> float:
        return self._spinSpacing.value()

    def rotation(self) -> float:
        return self._spinRot.value()

    def useCurve(self) -> bool:
        return self._chkCurve.isChecked()

    def reverse(self) -> bool:
        return self._chkReverse.isChecked()

    def mirror(self) -> bool:
        return self._chkMirror.isChecked()

    def perpendicular(self) -> bool:
        return self._rbPerp.isChecked()

    def offset(self) -> float:
        return self._spinOffset.value()

    def startPos(self) -> float:
        return self._spinStart.value()

    def setPrompt(self, text: str) -> None:
        pass
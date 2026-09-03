"""图片转线稿参数面板（移植自 Rhino 插件 ImageTracePanel，融入参数面板机制）。

来源支持画布上的图片物件（曲线自动叠加到图片平面）或从磁盘选择图片文件
（按输出宽度缩放放置）。参数变化发 paramChanged，由命令防抖后重跑识别；
面板内嵌取色器对话框实现"颜色拾取"（对应 Rhino 的视图吸管）。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog,
                               QFileDialog, QFrame, QHBoxLayout, QLabel,
                               QPushButton, QRadioButton, QButtonGroup,
                               QScrollArea, QSlider,
                               QVBoxLayout, QWidget)

from ..param_panels import ParamPanelBase
from core.vector.image_vectorizer import computeOtsuThreshold, detectInvert

# 会话级参数记忆（重开面板时恢复上次调参；阈值/反色按图片自动判断不记忆）
_PARAM_MEMO: dict = {}


class _SwatchRow(QWidget):
    """颜色样本行：小色块，点击移除。"""

    changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._colors: list = []
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(3)
        self._row.addStretch(1)

    def colors(self) -> list:
        return list(self._colors)

    def setColors(self, colors: list) -> None:
        self._colors = list(colors)
        self._rebuild()

    def addColor(self, c: QColor) -> None:
        self._colors.append(c)
        self._rebuild()
        self.changed.emit()

    def _rebuild(self) -> None:
        while self._row.count():
            item = self._row.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for i, c in enumerate(self._colors):
            chip = QPushButton()
            chip.setFixedSize(24, 18)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setToolTip("点击移除该颜色")
            chip.setStyleSheet(
                "QPushButton { background: #%02X%02X%02X; border: 1px solid #888;"
                " border-radius: 2px; }" % (c.red(), c.green(), c.blue()))
            chip.clicked.connect(lambda _, ix=i: self._remove(ix))
            self._row.insertWidget(self._row.count() - 1, chip)
        if not self._colors:
            hint = QLabel("（未拾取）")
            hint.setStyleSheet("color:#999;")
            self._row.insertWidget(self._row.count() - 1, hint)

    def _remove(self, idx: int) -> None:
        if 0 <= idx < len(self._colors):
            self._colors.pop(idx)
            self._rebuild()
            self.changed.emit()


class ColorPickDialog(QDialog):
    """取色器：显示来源图片，点击处取 3×3 均值色（对应 Rhino 视图吸管）。"""

    def __init__(self, image: QImage, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("拾取颜色：在图片上点击")
        self._image = image
        self._color: QColor | None = None
        self._scale = 1.0
        lab = QLabel()
        lab.setMouseTracking(True)
        sw = image.width()
        sh = image.height()
        fit = 420.0
        self._scale = min(1.0, fit / max(sw, sh, 1))
        pm = QPixmap.fromImage(image).scaled(
            int(sw * self._scale), int(sh * self._scale),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        lab.setPixmap(pm)
        lab.mousePressEvent = self._pick
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addWidget(lab)
        hint = QLabel("可连续取色不受限；此处点击一次即取一个颜色")
        hint.setStyleSheet("color:#777;")
        lay.addWidget(hint)

    def pickedColor(self):
        return self._color

    def _pick(self, ev) -> None:
        pos = ev.position()
        px = int(pos.x() / self._scale)
        py = int(pos.y() / self._scale)
        w, h = self._image.width(), self._image.height()
        px = min(max(px, 0), w - 1)
        py = min(max(py, 0), h - 1)
        r = g = b = n = 0
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                x, y = px + dx, py + dy
                if 0 <= x < w and 0 <= y < h:
                    c = self._image.pixelColor(x, y)
                    r += c.red()
                    g += c.green()
                    b += c.blue()
                    n += 1
        if n:
            self._color = QColor(r // n, g // n, b // n)
        self.accept()


class ImageTracePanel(ParamPanelBase):
    """图片转线稿面板。"""

    paramChanged = Signal()
    sourceChanged = Signal()
    commitRequested = Signal()
    cancelRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._image: QImage | None = None
        self._source = None            # ('doc', objId) | ('file', path) | None
        self._docImages: list = []     # [(objId, label)]
        self._updating = False
        self._build()
        self._applyMemo()

    # ---------------- UI 构建 ----------------

    def _build(self) -> None:
        body = QWidget()
        form = QVBoxLayout(body)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(4)
        self._form = form

        # ---- 来源 ----
        self._combo = QComboBox()
        self._combo.currentIndexChanged.connect(self._onComboChanged)
        btnBrowse = self._button2("浏览…")
        btnBrowse.setToolTip("从磁盘选择图片文件")
        btnBrowse.clicked.connect(self._onBrowse)
        row = QHBoxLayout()
        row.setSpacing(4)
        row.addWidget(self._combo, 1)
        row.addWidget(btnBrowse)
        form.addLayout(row)

        # ---- 黑白预览 ----
        self._preview = QLabel("未载入图片")
        self._preview.setFixedHeight(96)
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setStyleSheet(
            "background:#ffffff; border:1px solid #d0d0d0;")
        form.addWidget(self._preview)

        # ---- 模式 ----
        form.addWidget(self._sectionLabel("模式"))
        self._rbSingle = QRadioButton("单线")
        self._rbOutline = QRadioButton("轮廓")
        self._rbSingle.setChecked(True)
        self._grpCurve = QButtonGroup(self)
        self._grpCurve.addButton(self._rbSingle, 0)
        self._grpCurve.addButton(self._rbOutline, 1)
        self._grpCurve.idClicked.connect(lambda _i: self._onParam())
        row = QHBoxLayout()
        row.setSpacing(6)
        lab = QLabel("曲线")
        lab.setMinimumWidth(30)
        row.addWidget(lab)
        row.addWidget(self._rbSingle)
        row.addWidget(self._rbOutline)
        row.addStretch(1)
        form.addLayout(row)

        self._rbLuma = QRadioButton("亮度阈值")
        self._rbColor = QRadioButton("颜色拾取")
        self._rbLuma.setChecked(True)
        self._grpMethod = QButtonGroup(self)
        self._grpMethod.addButton(self._rbLuma, 0)
        self._grpMethod.addButton(self._rbColor, 1)
        self._grpMethod.idClicked.connect(lambda _i: self._onParam())
        row = QHBoxLayout()
        row.setSpacing(6)
        lab = QLabel("识别")
        lab.setMinimumWidth(30)
        row.addWidget(lab)
        row.addWidget(self._rbLuma)
        row.addWidget(self._rbColor)
        row.addStretch(1)
        form.addLayout(row)

        # ---- 亮度阈值组 ----
        self._gbLuma = QWidget()
        fl = QVBoxLayout(self._gbLuma)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(3)
        self._sldThreshold = QSlider(Qt.Orientation.Horizontal)
        self._sldThreshold.setRange(0, 255)
        self._sldThreshold.setValue(128)
        self._sldThreshold.valueChanged.connect(self._onSlider)
        self._spinThreshold = self._ispin(128, 0, 255)
        self._spinThreshold.setFixedWidth(54)
        self._spinThreshold.valueChanged.connect(self._onThresholdSpin)
        row = QHBoxLayout()
        row.setSpacing(4)
        lab = QLabel("阈值")
        lab.setMinimumWidth(30)
        row.addWidget(lab)
        row.addWidget(self._sldThreshold, 1)
        row.addWidget(self._spinThreshold)
        fl.addLayout(row)
        self._chkInvert = QCheckBox("反色（浅色线条 / 深色背景）")
        self._chkInvert.toggled.connect(lambda _on: self._onParam())
        fl.addWidget(self._chkInvert)
        form.addWidget(self._gbLuma)

        # ---- 颜色拾取组 ----
        self._gbColor = QWidget()
        fl = QVBoxLayout(self._gbColor)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(3)
        self._btnPickA = self._button2("拾取颜色A")
        self._btnPickA.clicked.connect(lambda: self._pickColor(0))
        self._spinTolA = self._spin(25.0, decimals=0, step=1.0)
        self._spinTolA.setRange(1.0, 100.0)
        self._spinTolA.setFixedWidth(54)
        self._spinTolA.valueChanged.connect(lambda _v: self._onParam())
        row = QHBoxLayout()
        row.setSpacing(4)
        row.addWidget(self._btnPickA)
        lab = QLabel("容差A")
        row.addWidget(lab)
        row.addWidget(self._spinTolA)
        row.addStretch(1)
        fl.addLayout(row)
        self._swA = _SwatchRow()
        self._swA.changed.connect(self._onParam)
        fl.addWidget(self._swA)
        self._btnPickB = self._button2("拾取颜色B")
        self._btnPickB.clicked.connect(lambda: self._pickColor(1))
        self._spinTolB = self._spin(25.0, decimals=0, step=1.0)
        self._spinTolB.setRange(1.0, 100.0)
        self._spinTolB.setFixedWidth(54)
        self._spinTolB.valueChanged.connect(lambda _v: self._onParam())
        row = QHBoxLayout()
        row.setSpacing(4)
        row.addWidget(self._btnPickB)
        lab = QLabel("容差B")
        row.addWidget(lab)
        row.addWidget(self._spinTolB)
        row.addStretch(1)
        fl.addLayout(row)
        self._swB = _SwatchRow()
        self._swB.changed.connect(self._onParam)
        fl.addWidget(self._swB)
        form.addWidget(self._gbColor)

        # ---- 预处理 ----
        form.addWidget(self._sectionLabel("预处理"))
        self._chkEnhance = QCheckBox("自动增强（模糊 / 低对比图）")
        self._chkEnhance.setChecked(True)
        self._chkEnhance.toggled.connect(lambda _on: self._onParam())
        form.addWidget(self._chkEnhance)
        self._spinBlur = self._ispin(1, 0, 5)
        self._spinMinArea = self._ispin(0, 0, 100000)
        self._spinBlur.valueChanged.connect(self._onParam2)
        self._spinMinArea.valueChanged.connect(self._onParam2)
        form.addLayout(self._twoCol("平滑", self._spinBlur, "小区域", self._spinMinArea))
        self._spinPrune = self._ispin(8, 0, 50)
        self._spinBlob = self._ispin(16, 0, 200)
        self._spinPrune.valueChanged.connect(self._onParam2)
        self._spinBlob.valueChanged.connect(self._onParam2)
        form.addLayout(self._twoCol("毛刺px", self._spinPrune, "粗描边", self._spinBlob))
        self._chkDebugLog = QCheckBox("诊断日志（结果区输出）")
        self._chkDebugLog.toggled.connect(lambda _on: self._onParam())
        form.addWidget(self._chkDebugLog)

        # ---- 输出 ----
        form.addWidget(self._sectionLabel("输出"))
        self._spinSimplify = self._spin(0.8, decimals=1, step=0.1)
        self._spinSimplify.setRange(0.1, 20.0)
        self._spinSimplify.setFixedWidth(54)
        self._spinSimplify.valueChanged.connect(self._onParam2)
        self._spinFit = self._spin(1.0, decimals=1, step=0.1)
        self._spinFit.setRange(0.1, 20.0)
        self._spinFit.setFixedWidth(54)
        self._spinFit.valueChanged.connect(self._onParam2)
        form.addLayout(self._twoCol("简化", self._spinSimplify, "拟合", self._spinFit))
        self._spinCorner = self._ispin(65, 5, 179)
        self._spinCorner.valueChanged.connect(self._onParam2)
        self._spinWidth = self._spin(100.0, decimals=0, step=10.0)
        self._spinWidth.setRange(1.0, 10000.0)
        self._spinWidth.setFixedWidth(54)
        self._spinWidth.valueChanged.connect(self._onParam2)
        form.addLayout(self._twoCol("尖角°", self._spinCorner, "输出宽", self._spinWidth))

        # ---- 滚动区（面板参数多，宿主高度不足时可滚动）----
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)
        self._layout.addWidget(scroll, 1)

        # ---- 固定底部：状态 + 按钮 ----
        self._status = QLabel("就绪")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color:#555;")
        self._layout.addWidget(self._status)

        row = QHBoxLayout()
        row.setSpacing(4)
        btnOk = self._button("生成曲线", primary=True)
        btnOk.clicked.connect(self.commitRequested.emit)
        btnCancel = self._button2("取消")
        btnCancel.clicked.connect(self.cancelRequested.emit)
        row.addWidget(btnOk, 1)
        row.addWidget(btnCancel)
        self._layout.addLayout(row)

        self._updateMethodGroups()

    def _sectionLabel(self, text: str) -> QLabel:
        s = QLabel(text)
        s.setStyleSheet("color:#666666; font-weight:bold;")
        return s

    def _twoCol(self, l1, w1, l2, w2) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(4)
        for text, w in ((l1, w1), (l2, w2)):
            lab = QLabel(text)
            lab.setMinimumWidth(34)
            row.addWidget(lab)
            row.addWidget(w, 1)
        return row

    # ---------------- 参数记忆（会话级） ----------------

    def _applyMemo(self) -> None:
        if not _PARAM_MEMO:
            return
        self._updating = True
        try:
            (self._rbSingle if _PARAM_MEMO.get("centerline", True)
             else self._rbOutline).setChecked(True)
            (self._rbLuma if _PARAM_MEMO.get("colorMode", False)
             else self._rbColor).setChecked(True)
            if "blur" in _PARAM_MEMO:
                self._spinBlur.setValue(_PARAM_MEMO["blur"])
            if "minArea" in _PARAM_MEMO:
                self._spinMinArea.setValue(_PARAM_MEMO["minArea"])
            if "prune" in _PARAM_MEMO:
                self._spinPrune.setValue(_PARAM_MEMO["prune"])
            if "blob" in _PARAM_MEMO:
                self._spinBlob.setValue(_PARAM_MEMO["blob"])
            if "simplify" in _PARAM_MEMO:
                self._spinSimplify.setValue(_PARAM_MEMO["simplify"])
            if "fit" in _PARAM_MEMO:
                self._spinFit.setValue(_PARAM_MEMO["fit"])
            if "corner" in _PARAM_MEMO:
                self._spinCorner.setValue(_PARAM_MEMO["corner"])
            if "width" in _PARAM_MEMO:
                self._spinWidth.setValue(_PARAM_MEMO["width"])
            if "tolA" in _PARAM_MEMO:
                self._spinTolA.setValue(_PARAM_MEMO["tolA"])
            if "tolB" in _PARAM_MEMO:
                self._spinTolB.setValue(_PARAM_MEMO["tolB"])
            self._chkEnhance.setChecked(_PARAM_MEMO.get("enhance", True))
            self._chkDebugLog.setChecked(_PARAM_MEMO.get("debug", False))
        finally:
            self._updating = False
        self._updateMethodGroups()

    def _saveMemo(self) -> None:
        _PARAM_MEMO.update({
            "centerline": self._rbSingle.isChecked(),
            "colorMode": self._rbColor.isChecked(),
            "blur": self._spinBlur.value(),
            "minArea": self._spinMinArea.value(),
            "prune": self._spinPrune.value(),
            "blob": self._spinBlob.value(),
            "simplify": self._spinSimplify.value(),
            "fit": self._spinFit.value(),
            "corner": self._spinCorner.value(),
            "width": self._spinWidth.value(),
            "tolA": self._spinTolA.value(),
            "tolB": self._spinTolB.value(),
            "enhance": self._chkEnhance.isChecked(),
            "debug": self._chkDebugLog.isChecked(),
        })

    # ---------------- 来源 ----------------

    def setDocImages(self, entries: list) -> None:
        """传入画布图片列表 [(objId, label)]；保留当前有效选择。"""
        self._docImages = list(entries)
        self._updating = True
        self._combo.blockSignals(True)
        self._combo.clear()
        for oid, label in self._docImages:
            self._combo.addItem("画布图片：" + label, ("doc", oid))
        self._combo.addItem("从文件选择…", None)
        cur = self._source
        idx = -1
        for i, (oid, _label) in enumerate(self._docImages):
            if cur is not None and cur[0] == "doc" and cur[1] == oid:
                idx = i
                break
        if idx >= 0:
            self._combo.setCurrentIndex(idx)
        elif self._source is not None and self._source[0] == "file":
            self._combo.setCurrentIndex(self._combo.count() - 1)
        elif self._docImages:
            self._source = self._combo.itemData(0)
            self._combo.setCurrentIndex(0)
        else:
            self._source = None
            self._combo.setCurrentIndex(self._combo.count() - 1)
        self._combo.blockSignals(False)
        self._updating = False

    def docImageCount(self) -> int:
        return len(self._docImages)

    def currentSource(self):
        return self._source

    def setSourceFile(self, path: str) -> None:
        self._source = ("file", path)
        self._updating = True
        self._combo.blockSignals(True)
        self._combo.setCurrentIndex(self._combo.count() - 1)
        self._combo.blockSignals(False)
        self._updating = False
        self.sourceChanged.emit()

    def _onComboChanged(self, _idx: int) -> None:
        if self._updating:
            return
        data = self._combo.currentData()
        if data is None:
            self._onBrowse()
            return
        self._source = data
        self.sourceChanged.emit()

    def _onBrowse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择图片", "", "图片 (*.png *.jpg *.jpeg *.bmp *.gif *.webp);;所有文件 (*)")
        if not path:
            # 还原下拉框显示（未选择文件）
            self._updating = True
            self._combo.blockSignals(True)
            if self._source is not None and self._source[0] == "doc":
                for i in range(self._combo.count()):
                    if self._combo.itemData(i) == self._source:
                        self._combo.setCurrentIndex(i)
                        break
            self._combo.blockSignals(False)
            self._updating = False
            return
        self.setSourceFile(path)

    # ---------------- 图片与预览 ----------------

    def setSourceImage(self, image: QImage | None, autoParams: bool = True) -> None:
        """载入来源图片；autoParams 时自动估算阈值与反色（Otsu + 边框亮度）。"""
        self._image = image
        if image is None or image.isNull():
            self._preview.setPixmap(QPixmap())
            self._preview.setText("未载入图片")
            return
        if autoParams:
            self._updating = True
            t = computeOtsuThreshold(image, self._chkEnhance.isChecked())
            self._spinThreshold.setValue(t)
            self._sldThreshold.setValue(t)
            self._chkInvert.setChecked(detectInvert(image, self._chkEnhance.isChecked()))
            self._updating = False

    def setBinaryPreview(self, image: QImage | None) -> None:
        if image is None or image.isNull():
            return
        pm = QPixmap.fromImage(image).scaledToHeight(
            self._preview.height() - 4,
            Qt.TransformationMode.SmoothTransformation)
        self._preview.setPixmap(pm)

    def setStatus(self, text: str) -> None:
        self._status.setText(text)

    # ---------------- 取值 ----------------

    def isColorMode(self) -> bool:
        return self._rbColor.isChecked() and \
            (len(self._swA.colors()) + len(self._swB.colors())) > 0

    def outputWidth(self) -> float:
        return self._spinWidth.value()

    def hasImage(self) -> bool:
        return self._image is not None and not self._image.isNull()

    def sourceImage(self):
        return self._image

    def buildOptions(self):
        """组装识别参数（不含放置：mapCorners/输出宽由命令按来源补齐）。"""
        from core.vector.image_vectorizer import ImageTraceOptions
        return ImageTraceOptions(
            threshold=self._spinThreshold.value(),
            invert=self._chkInvert.isChecked(),
            blur=self._spinBlur.value(),
            minAreaPx=float(self._spinMinArea.value()),
            centerlineMode=self._rbSingle.isChecked(),
            pruneLenPx=float(self._spinPrune.value()),
            blobWidthPx=float(self._spinBlob.value()),
            ballDiaPx=0.0,
            useColorKeys=self._rbColor.isChecked() and
            (len(self._swA.colors()) + len(self._swB.colors())) > 0,
            colorAKeysLab=[_colorToLab(c) for c in self._swA.colors()],
            colorBKeysLab=[_colorToLab(c) for c in self._swB.colors()],
            colorATolerance=self._spinTolA.value(),
            colorBTolerance=self._spinTolB.value(),
            autoEnhance=self._chkEnhance.isChecked(),
            simplifyTolPx=self._spinSimplify.value(),
            fitTolPx=self._spinFit.value(),
            cornerAngleDeg=float(self._spinCorner.value()),
            outputWidth=self._spinWidth.value(),
            centerAtOrigin=True,
            debugLog=(_sinkDebug if self._chkDebugLog.isChecked() else None),
        )

    # ---------------- 交互 ----------------

    def _onParam(self) -> None:
        self._updateMethodGroups()
        if not self._updating:
            self.paramChanged.emit()

    def _onParam2(self, _v=None) -> None:
        self._onParam()

    def _onSlider(self, v: int) -> None:
        if self._updating:
            return
        self._updating = True
        self._spinThreshold.setValue(v)
        self._updating = False
        self.paramChanged.emit()

    def _onThresholdSpin(self, v: int) -> None:
        if self._updating:
            return
        self._updating = True
        self._sldThreshold.setValue(v)
        self._updating = False
        self.paramChanged.emit()

    def _updateMethodGroups(self) -> None:
        color = self._rbColor.isChecked()
        self._gbLuma.setVisible(not color)
        self._gbColor.setVisible(color)
        # 组互斥切换改变面板高度：通知宿主容器重新适配
        self._notifyResize()

    def _pickColor(self, target: int) -> None:
        if not self.hasImage():
            self.setStatus("请先选择来源图片，再拾取颜色")
            return
        dlg = ColorPickDialog(self._image, self)
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.pickedColor() is None:
            return
        sw = self._swA if target == 0 else self._swB
        other = self._swB if target == 0 else self._swA
        if len(sw.colors()) >= 8:
            self.setStatus("颜色%s 最多 8 个，点击色块可移除后再拾取" %
                           ("A" if target == 0 else "B"))
            return
        sw.addColor(dlg.pickedColor())
        if not self._rbColor.isChecked():
            self._updating = True
            self._rbColor.setChecked(True)
            self._updating = False
            self._updateMethodGroups()
        c = dlg.pickedColor()
        self.setStatus("已拾取颜色%s #%02X%02X%02X（全图生效），渐变可多拾几个"
                       % ("A" if target == 0 else "B", c.red(), c.green(), c.blue()))
        self.paramChanged.emit()


def _colorToLab(c: QColor) -> tuple:
    from core.vector.image_vectorizer import rgbToLab
    return rgbToLab(c.red(), c.green(), c.blue())


def _sinkDebug(s: str) -> None:
    """诊断日志汇（命令读取 TraceResult.debug 转发到结果提示，无需实时输出）。"""
    pass

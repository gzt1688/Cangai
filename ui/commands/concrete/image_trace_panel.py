"""图片转线稿参数面板（VTracer 引擎，参数照官方桌面版面板）。

来源固定为画布上第一张图片（曲线自动叠加到图片平面）；参数变化发
paramChanged，由命令防抖后重跑识别。聚合：黑白/彩色/分水岭，
合成：堆叠/镂空，曲线拟合：像素/多边形/曲线，另有杂质过滤/细节/
简化/路径精度，与 VTracer 引擎参数一一对应。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QAbstractSpinBox, QCheckBox, QFrame,
                               QHBoxLayout, QLabel, QPushButton, QButtonGroup,
                               QScrollArea, QSlider, QVBoxLayout, QWidget)

from ..param_panels import ParamPanelBase, _LABEL_W, _SPIN_RANGE
import ui.styles.theme as theme
from core.vector.image_vectorizer import computeOtsuThreshold, detectInvert


from ..param_panels import ParamPanelBase, _LABEL_W, _SPIN_RANGE
import ui.styles.theme as theme
from core.vector.image_vectorizer import computeOtsuThreshold, detectInvert

# 会话级参数记忆（重开面板时恢复上次调参；阈值/反色按图片自动判断不记忆）
_PARAM_MEMO: dict = {}
_SPIN_W = 38        # 数值框统一宽度（无调节箭头）
_SEG_FONT = 11      # 分段按钮字号


class ImageTracePanel(ParamPanelBase):
    """图片转线稿面板。"""

    paramChanged = Signal()
    commitRequested = Signal()
    cancelRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._image: QImage | None = None
        self._updating = False
        self._build()
        self._applyMemo()

    # ---------------- UI 构建 ----------------

    def _spin(self, value: float = 0.0, decimals: int = 2,
              step: float = 1.0):
        s = super()._spin(value, decimals, step)
        s.setFixedWidth(_SPIN_W)
        s.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        s.setAlignment(Qt.AlignmentFlag.AlignRight)
        return s

    def _ispin(self, value: int = 0, lo: int = 0, hi: int = 100):
        s = super()._ispin(value, lo, hi)
        s.setFixedWidth(_SPIN_W)
        s.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        s.setAlignment(Qt.AlignmentFlag.AlignRight)
        return s

    def _rowLabel(self, text: str) -> QLabel:
        """统一行标签宽度，全面板左边缘对齐。"""
        lab = QLabel(text)
        lab.setMinimumWidth(_LABEL_W)
        return lab

    def _segmented(self, names: tuple) -> tuple:
        """官方风格分段选择器：一行等宽可选中按钮，选中填充主题色。

        返回 (QButtonGroup, 容器 QWidget)；按钮 id = 序号。"""
        grp = QButtonGroup(self)
        grp.setExclusive(True)
        row = QHBoxLayout()
        row.setSpacing(1)
        row.setContentsMargins(0, 0, 0, 0)
        for i, name in enumerate(names):
            b = QPushButton(name)
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(
                "QPushButton { background:%s; color:%s; border:1px solid %s;"
                " border-radius:3px; padding:2px 1px; font-size:%dpx; }"
                "QPushButton:hover { border-color:%s; color:%s; }"
                "QPushButton:checked { background:%s; color:%s;"
                " border-color:%s; font-weight:bold; }"
                % (theme.BG_PANEL, theme.TEXT, theme.BORDER,
                   _SEG_FONT, theme.PRIMARY, theme.PRIMARY_DARK,
                   theme.PRIMARY, theme.TEXT_ON_PRIMARY, theme.PRIMARY))
            grp.addButton(b, i)
            row.addWidget(b, 1)
        w = QWidget()
        w.setLayout(row)
        return grp, w

    def _card(self) -> tuple:
        """浅底圆角卡片容器：分组内容与面板其余部分视觉隔离。返回 (QFrame, 布局)。"""
        card = QFrame()
        card.setObjectName("traceCard")
        card.setStyleSheet(
            "QFrame#traceCard { background:#fafbfc; border:1px solid #e3e6ea;"
            " border-radius:4px; }")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(6, 5, 6, 5)
        lay.setSpacing(5)
        return card, lay

    def _build(self) -> None:
        body = QWidget()
        form = QVBoxLayout(body)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(5)
        self._form = form

        # ---- 黑白预览 ----
        self._preview = QLabel("未载入图片")
        self._preview.setFixedHeight(84)
        self._preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview.setStyleSheet(
            "background:#ffffff; border:1px solid #d0d0d0;")
        form.addWidget(self._preview)

        # ---- VTracer 参数卡片（照官方桌面版面板分组）----
        self._gbVt, fl = self._card()
        form.addWidget(self._gbVt)

        # 阈值 / 反色（仅黑白聚合）
        self._gbThresh = QWidget()
        tl = QVBoxLayout(self._gbThresh)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(3)
        self._sldThreshold = QSlider(Qt.Orientation.Horizontal)
        self._sldThreshold.setRange(0, 255)
        self._sldThreshold.setValue(128)
        self._sldThreshold.valueChanged.connect(self._onSlider)
        self._spinThreshold = self._ispin(128, 0, 255)
        self._spinThreshold.valueChanged.connect(self._onThresholdSpin)
        row = QHBoxLayout()
        row.setSpacing(4)
        row.addWidget(self._rowLabel("阈值"))
        row.addWidget(self._sldThreshold, 1)
        row.addWidget(self._spinThreshold)
        tl.addLayout(row)
        self._chkInvert = QCheckBox("反色（浅线/深底）")
        self._chkInvert.toggled.connect(lambda _on: self._onParam())
        tl.addWidget(self._chkInvert)

        # 聚合：黑白 / 彩色 / 分水岭
        fl.addWidget(self._sectionLabel("聚合"))
        self._grpVtCluster, _seg = self._segmented(("黑白", "彩色", "分水岭"))
        self._vtClusterVals = ["bw", "color-cluster", "watershed"]
        self._grpVtCluster.button(2).setChecked(True)   # 默认分水岭（照官方）
        self._grpVtCluster.idClicked.connect(lambda _i: self._onParam())
        fl.addWidget(_seg)

        # 阈值 / 反色（仅黑白聚合）
        fl.addWidget(self._gbThresh)

        # 浅色过滤（彩色/分水岭；丢弃背景等浅色色块的轮廓）
        self._rowVtLight = QWidget()
        r = QHBoxLayout(self._rowVtLight)
        r.setContentsMargins(0, 0, 0, 0)
        r.setSpacing(4)
        r.addWidget(self._rowLabel("浅色过滤"))
        self._spinVtLight = self._ispin(200, 0, 255)
        self._spinVtLight.setToolTip(
            "填充亮度 > 该值的色块不输出曲线；255 = 不过滤。\n"
            "去掉背景色块在字形之间穿过的轮廓线")
        self._spinVtLight.valueChanged.connect(self._onParam2)
        r.addWidget(self._spinVtLight)
        r.addStretch(1)
        fl.addWidget(self._rowVtLight)

        # 细节（仅分水岭）
        self._rowVtDetail = QWidget()
        r = QHBoxLayout(self._rowVtDetail)
        r.setContentsMargins(0, 0, 0, 0)
        r.setSpacing(4)
        r.addWidget(self._rowLabel("细节"))
        self._sldVtDetail = QSlider(Qt.Orientation.Horizontal)
        self._sldVtDetail.setRange(0, 255)
        self._sldVtDetail.setValue(122)
        self._sldVtDetail.valueChanged.connect(self._onSliderVtDetail)
        self._spinVtDetail = self._ispin(122, 0, 255)
        self._spinVtDetail.valueChanged.connect(self._onSpinVtDetail)
        r.addWidget(self._sldVtDetail, 1)
        r.addWidget(self._spinVtDetail)
        fl.addWidget(self._rowVtDetail)

        # 合成：堆叠 / 镂空 + 杂质过滤
        fl.addWidget(self._sectionLabel("合成"))
        self._grpVtHier, _seg = self._segmented(("堆叠", "镂空"))
        self._vtHierVals = ["stacked", "cutout"]
        self._grpVtHier.button(1).setChecked(True)   # 默认镂空（照官方截图）
        self._grpVtHier.idClicked.connect(lambda _i: self._onParam())
        fl.addWidget(_seg)
        self._spinVtSpeckle = self._ispin(0, 0, 128)
        self._spinVtSpeckle.valueChanged.connect(self._onParam2)
        row = QHBoxLayout()
        row.setSpacing(4)
        row.addWidget(self._rowLabel("杂质过滤"))
        row.addWidget(self._spinVtSpeckle)
        row.addStretch(1)
        fl.addLayout(row)

        # 曲线拟合：像素 / 多边形 / 曲线 + 简化 / 精度
        fl.addWidget(self._sectionLabel("曲线拟合"))
        self._grpVtFit, _seg = self._segmented(("像素", "多边形", "曲线"))
        self._vtFitVals = ["pixel", "polygon", "spline"]
        self._grpVtFit.button(2).setChecked(True)   # 默认曲线（照官方截图）
        self._grpVtFit.idClicked.connect(lambda _i: self._onParam())
        fl.addWidget(_seg)
        self._spinVtSimplify = self._spin(1.2, decimals=1, step=0.1)
        self._spinVtSimplify.setRange(0.0, 10.0)
        self._spinVtSimplify.setToolTip("曲线简化容差（像素），0 = 关闭")
        self._spinVtSimplify.valueChanged.connect(self._onParam2)
        self._spinVtPathPrec = self._ispin(2, 0, 8)
        self._spinVtPathPrec.valueChanged.connect(self._onParam2)
        fl.addLayout(self._twoCol("简化", self._spinVtSimplify,
                                  "精度", self._spinVtPathPrec))

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
        row.setSpacing(3)
        for text, w in ((l1, w1), (l2, w2)):
            lab = QLabel(text)
            lab.setMinimumWidth(44)
            row.addWidget(lab)
            row.addWidget(w, 1)
        return row

    # ---------------- 参数记忆（会话级） ----------------

    def _applyMemo(self) -> None:
        if not _PARAM_MEMO:
            self._updateMethodGroups()
            return
        self._updating = True
        try:
            if _PARAM_MEMO.get("vtCluster") in self._vtClusterVals:
                idx = self._vtClusterVals.index(_PARAM_MEMO["vtCluster"])
                self._grpVtCluster.button(idx).setChecked(True)
            if _PARAM_MEMO.get("vtHier") in self._vtHierVals:
                idx = self._vtHierVals.index(_PARAM_MEMO["vtHier"])
                self._grpVtHier.button(idx).setChecked(True)
            if _PARAM_MEMO.get("vtFit") in self._vtFitVals:
                idx = self._vtFitVals.index(_PARAM_MEMO["vtFit"])
                self._grpVtFit.button(idx).setChecked(True)
            if "vtDetail" in _PARAM_MEMO:
                self._sldVtDetail.setValue(_PARAM_MEMO["vtDetail"])
                self._spinVtDetail.setValue(_PARAM_MEMO["vtDetail"])
            if "vtSpeckle" in _PARAM_MEMO:
                self._spinVtSpeckle.setValue(_PARAM_MEMO["vtSpeckle"])
            if "vtSimplify" in _PARAM_MEMO:
                self._spinVtSimplify.setValue(_PARAM_MEMO["vtSimplify"])
            if "vtPathPrec" in _PARAM_MEMO:
                self._spinVtPathPrec.setValue(_PARAM_MEMO["vtPathPrec"])
            if "vtLight" in _PARAM_MEMO:
                self._spinVtLight.setValue(_PARAM_MEMO["vtLight"])
        finally:
            self._updating = False
        self._updateMethodGroups()

    def _saveMemo(self) -> None:
        _PARAM_MEMO.update({
            "vtCluster": self._vtClusterVals[self._grpVtCluster.checkedId()],
            "vtHier": self._vtHierVals[self._grpVtHier.checkedId()],
            "vtFit": self._vtFitVals[self._grpVtFit.checkedId()],
            "vtDetail": self._spinVtDetail.value(),
            "vtSpeckle": self._spinVtSpeckle.value(),
            "vtSimplify": self._spinVtSimplify.value(),
            "vtPathPrec": self._spinVtPathPrec.value(),
            "vtLight": self._spinVtLight.value(),
        })

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
            t = computeOtsuThreshold(image, False)
            self._spinThreshold.setValue(t)
            self._sldThreshold.setValue(t)
            self._chkInvert.setChecked(detectInvert(image, False))
            self._updating = False

    def setBinaryPreview(self, image: QImage | None) -> None:
        if image is None or image.isNull():
            self._preview.setPixmap(QPixmap())
            self._preview.setText("（无黑白预览）")
            return
        pm = QPixmap.fromImage(image).scaledToHeight(
            self._preview.height() - 4,
            Qt.TransformationMode.SmoothTransformation)
        self._preview.setPixmap(pm)

    def setStatus(self, text: str) -> None:
        self._status.setText(text)

    # ---------------- 取值 ----------------

    def hasImage(self) -> bool:
        return self._image is not None and not self._image.isNull()

    def sourceImage(self):
        return self._image

    def buildOptions(self):
        """组装识别参数（放置：mapCorners 由命令按画布图片补齐）。"""
        from core.vector.image_vectorizer import ImageTraceOptions
        return ImageTraceOptions(
            vtCluster=self._vtClusterVals[self._grpVtCluster.checkedId()],
            vtDetail=self._spinVtDetail.value(),
            vtHierarchical=self._vtHierVals[self._grpVtHier.checkedId()],
            vtSpeckle=self._spinVtSpeckle.value(),
            vtFit=self._vtFitVals[self._grpVtFit.checkedId()],
            vtSimplify=self._spinVtSimplify.value(),
            vtPathPrec=self._spinVtPathPrec.value(),
            vtLightLuma=self._spinVtLight.value(),
            threshold=self._spinThreshold.value(),
            invert=self._chkInvert.isChecked(),
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

    def _onSliderVtDetail(self, v: int) -> None:
        if self._updating:
            return
        self._updating = True
        self._spinVtDetail.setValue(v)
        self._updating = False
        self.paramChanged.emit()

    def _onSpinVtDetail(self, v: int) -> None:
        if self._updating:
            return
        self._updating = True
        self._sldVtDetail.setValue(v)
        self._updating = False
        self.paramChanged.emit()

    def _updateMethodGroups(self) -> None:
        cluster = self._vtClusterVals[self._grpVtCluster.checkedId()]
        # 阈值/反色仅黑白聚合有意义；细节仅分水岭；浅色过滤仅彩色/分水岭
        self._gbThresh.setVisible(cluster == "bw")
        self._rowVtDetail.setVisible(cluster == "watershed")
        self._rowVtLight.setVisible(cluster != "bw")
        # 组互斥切换改变面板高度：通知宿主容器重新适配
        self._notifyResize()


def _sinkDebug(s: str) -> None:
    """诊断日志汇（命令读取 TraceResult.debug 转发到结果提示）。"""
    pass

def _sinkDebug(s: str) -> None:
    """诊断日志汇（命令读取 TraceResult.debug 转发到结果提示，无需实时输出）。"""
    pass

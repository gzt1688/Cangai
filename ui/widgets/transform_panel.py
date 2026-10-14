"""变换面板：平移 / 旋转 / 缩放参数编辑与镜像快捷操作。"""
from __future__ import annotations
from typing import Optional
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                                QDoubleSpinBox, QCheckBox, QSlider,
                                QRadioButton, QButtonGroup,
                                QScrollArea, QFrame, QSizePolicy, QPushButton,
                                QDialog, QFormLayout, QDialogButtonBox)
from PySide6.QtCore import Qt, Signal, QEvent
from PySide6.QtGui import QMouseEvent
import ui.styles.theme as theme
from resource_path import resource_path
from core.vector.geometry_types import ObjectType, Point3D
from i18n import tr, languageChanged

# 浮雕 2D 预览显示模式：(对象存储值, 界面文案中文原文)；默认“等高线”置首
_RELIEF_MODES = [
    ("contour", "等高线"),
    ("gray", "灰度图"),
]

# 浮雕(3D)模式显示：与 2D 预览独立（对象存储值, 界面文案）；默认“地图”置首
_RELIEF_3D_MODES = [
    ("map", "地图"),
    ("color", "颜色"),
    ("material", "渲染"),
]



def _radioStyle() -> str:
    return (
        "QRadioButton { font-size: 12px; spacing: 4px; }"
        "QRadioButton::indicator { width: 13px; height: 13px;"
        " border: 1px solid #999; border-radius: 6px; background: white; }"
        f"QRadioButton::indicator:checked {{ background: {theme.PRIMARY};"
        f" border-color: {theme.PRIMARY}; }}"
    )


def _makeLabel(text: str, width: int = -1, style: str = "") -> QLabel:
    lbl = QLabel(text)
    if width > 0:
        lbl.setFixedWidth(width)
    if style:
        lbl.setStyleSheet(style)
    return lbl


# 工具行按钮统一尺寸：镜像三键与标注三键同宽同高、行首对齐
_BTN_W = 58
_BTN_H = 26


def _makeToolButton(text: str, tip: str = "") -> QPushButton:
    """标准工具按钮（固定尺寸，带悬浮提示；提示在禁用态也显示）。"""
    btn = QPushButton(text)
    btn.setFixedHeight(_BTN_H)
    btn.setFixedWidth(_BTN_W)
    if tip:
        btn.setToolTip(tip)
        btn.setAttribute(Qt.WA_AlwaysShowToolTips, True)
    return btn


class LeftRightPushButton(QPushButton):
    """按钮区分左/右键点击：左键 → leftClicked；右键 → rightClicked。
    不触发默认的 QPushButton.clicked，避免左右键被混淆。"""

    leftClicked = Signal()
    rightClicked = Signal()

    def __init__(self, text: str, tip: str = "", parent=None):
        super().__init__(text, parent)
        self.setFixedHeight(_BTN_H)
        self.setFixedWidth(_BTN_W)
        if tip:
            self.setToolTip(tip)
            self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.setDown(True)
            self.leftClicked.emit()
            event.accept()
            return
        if event.button() == Qt.MouseButton.RightButton:
            self.setDown(True)
            self.rightClicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self.isDown():
            self.setDown(False)
            event.accept()
            return
        super().mouseReleaseEvent(event)


def _makeSpinBox(width: int = 80, rng: float = 99999, decimals: int = 2) -> QDoubleSpinBox:
    sp = QDoubleSpinBox()
    sp.setRange(-rng, rng)
    sp.setValue(0)
    sp.setDecimals(decimals)
    sp.setFixedWidth(width)
    sp.setFixedHeight(28)
    sp.setStyleSheet(
        "QDoubleSpinBox { font-size: 14px; border: 1px solid #ccc;"
        " border-radius: 3px; background: white; padding: 2px; }"
        "QDoubleSpinBox::up-button { width: 16px; border: none;"
        f" background: #f5f5f5; image: url({resource_path('icons/plus.svg')}); }}"
        "QDoubleSpinBox::down-button { width: 16px; border: none;"
        f" background: #f5f5f5; image: url({resource_path('icons/minus.svg')}); }}"
        "QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {"
        " background: #e0e0e0; }"
        "QDoubleSpinBox::up-button:pressed, QDoubleSpinBox::down-button:pressed {"
        " background: #d0d0d0; }"
    )
    sp.setButtonSymbols(QDoubleSpinBox.PlusMinus)
    return sp


class TransformPanel(QWidget):
    mirrorRequested = Signal(str)      # "X" / "Y" / "Z"
    zHeightCommitRequested = Signal(float)  # “模型Z”确认：目标高度值（缩放高度场）
    zClipRequested = Signal(float, float)   # “限高保底”：clip 高度场到 [z_min, z_max]
    dimensionRequested = Signal(str)   # 标注："xy" / "align" / "z"
    dimensionTextBiggerRequested = Signal()    # 放大选中标注的读数文字
    dimensionTextSmallerRequested = Signal()   # 缩小选中标注的读数文字
    dimensionBindToggled = Signal()    # 关闭选中 XY 标注与物体的绑定（永久解除）
    moveRequested = Signal()           # 面板移动命令：两点平移选中对象
    rotateRequested = Signal()         # 面板旋转命令：两点定轴+拖动旋转
    scaleRequested = Signal()          # 面板缩放命令：两点定参考+拖动缩放
    measureRequested = Signal()        # 测量命令：点两个点，左下角显示两点距离
    solidDisplayModeRequested = Signal(str)   # 3D 实体显示模式："wireframe" / "rendered"
    solidPerspectiveRequested = Signal(bool)  # 3D 线框透视开关：True=显示背面线
    meshHeightColorRequested = Signal(bool)  # 网格 STL 实体高度色开关
    solidLightRequested = Signal(int, int)  # 3D 实体光照（方位角, 仰角）


    MIRROR_AXES = [("X", "X镜像"), ("Y", "Y镜像"), ("Z", "Z镜像")]

    def __init__(self) -> None:
        super().__init__()
        self._selectedObjectId: Optional[str] = None
        self._selectedObject = None      # 当前选中对象引用（标注绑定开关状态用）
        self._doc = None                 # 写回浮雕显示参数用（notifyObjectChanged）
        self._reliefObj = None           # 当前选中的浮雕模型（非 SURFACE 为 None）
        self._pinnedRelief = None        # 浮雕模式锁定的编辑目标（与选中解耦）
        self._updatingRelief = False     # 回填期间抑制写回

        main = QVBoxLayout(self)
        main.setContentsMargins(2, 2, 2, 2)
        main.setSpacing(0)
        main.setAlignment(Qt.AlignTop)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet("QScrollArea { border: none; }")
        scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        cl.setAlignment(Qt.AlignTop)
        scroll.setWidget(content)
        main.addWidget(scroll)

        self._content = cl
        self._buildSizeControls()
        self._buildOriginControls()
        # 移动/旋转/缩放快捷行（放在镜像三键上方）
        self._buildXformControls()
        self._content.addSpacing(2)
        self._buildMirrorControls()
        self._buildDimensionControls()
        self._buildCncCenterControls()
        self._buildReliefControls()

    def _buildXformControls(self) -> None:
        """移动 / 旋转 / 缩放 三键（放在镜像三键上方，尺寸与镜像按钮一致）。"""
        row = QWidget()
        row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(4)
        self._xformBtns = []
        for key, text, tip in (
                ("move", "移动", "选中对象后：点基准点 → 点目标点完成平移"),
                ("rotate", "旋转", "选中对象后：点中心点 → 点参考起点 → 拖动或点目标角度确认"),
                ("scale", "缩放", "选中对象后：点基准点 → 点参考起点 → 拖动或点目标距离确认")):
            btn = _makeToolButton(tr(text), tr(tip))
            if key == "move":
                btn.clicked.connect(self.moveRequested.emit)
            elif key == "rotate":
                btn.clicked.connect(self.rotateRequested.emit)
            elif key == "scale":
                btn.clicked.connect(self.scaleRequested.emit)
            rl.addWidget(btn)
            self._xformBtns.append((btn, text, tip))
        rl.addStretch()
        self._content.addWidget(row)

    def _buildMirrorControls(self) -> None:
        row = QWidget()
        row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(4)
        self._mirrorBtns = []
        for axis, text in self.MIRROR_AXES:
            btn = _makeToolButton(tr(text))
            btn.clicked.connect(lambda _, a=axis: self.mirrorRequested.emit(a))
            rl.addWidget(btn)
            self._mirrorBtns.append((btn, text))
        rl.addStretch()
        self._content.addWidget(row)

    def _buildDimensionControls(self) -> None:
        """尺寸标注区：
        - 与上方镜像三键行间隔 2px；
        - 第一行：XY标注（标选中对象包围盒宽高）/ 对齐标注（两点）/ Z标注（占位）
          ——与上方镜像三键同尺寸、行首对齐；
        - 第二行：增大尺寸 / 缩小尺寸（与上面两排按钮同宽）——整体缩放选中
          标注的读数文字；紧随其后的“绑定”开关显示当前 XY 标注的绑定状态
          （已绑定=可点击解除，已解绑=永久脱离）。

        整个标注区用容器 widget 包裹（self._dimBox），CNC 模式下整体隐藏。
        """
        self._dimBox = QWidget()
        self._dimBox.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        dl = QVBoxLayout(self._dimBox)
        dl.setContentsMargins(0, 2, 0, 0)
        dl.setSpacing(2)
        # ---- 第一行：创建标注（与镜像按钮同尺寸）----
        row = QWidget()
        row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(4)
        self._dimBtns = []
        for key, text, tip in (
                ("xy", "XY标注", "标注选中对象的 X 宽与 Y 高尺寸"),
                ("align", "对齐标注", "点击两个点标注其间距（任意方向）"),
                ("z", "Z标注", "Z 标注占位：后续版本用于为模型标注高度")):
            btn = _makeToolButton(tr(text), tr(tip))
            btn.clicked.connect(
                lambda _, k=key: self.dimensionRequested.emit(k))
            rl.addWidget(btn)
            self._dimBtns.append((btn, text, tip))
        rl.addStretch()
        dl.addWidget(row)

        # ---- 第二行：调整已选标注的读数文字大小（大/小 合并为一个左右键按钮）
        #           + 测量（两点距离）+ 绑定开关 ----
        row2 = QWidget()
        row2.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        rl2 = QHBoxLayout(row2)
        rl2.setContentsMargins(0, 0, 0, 0)
        rl2.setSpacing(4)
        # 大/小 双用按钮：左键=放大选中标注读数文字，右键=缩小
        sizeBtn = LeftRightPushButton(
            tr("大 / 小"),
            tr("调整选中标注的读数文字大小：左键=放大(×1.25)，右键=缩小(÷1.25)"))
        sizeBtn.leftClicked.connect(self.dimensionTextBiggerRequested.emit)
        sizeBtn.rightClicked.connect(self.dimensionTextSmallerRequested.emit)
        rl2.addWidget(sizeBtn)
        self._sizeBtn = sizeBtn
        # 测量按钮：点击进入测量命令，取两个点显示距离
        measureBtn = _makeToolButton(tr("测量"),
                                     tr("测量两点间距离：点击两个点后在左下角显示距离"))
        measureBtn.clicked.connect(self.measureRequested.emit)
        rl2.addWidget(measureBtn)
        self._measureBtn = measureBtn
        # 绑定开关紧随测量按钮，与上面两排按钮同宽同高、逐列对齐；
        # 文案随选中状态变化：绑定 / 已绑定 / 已解绑
        bindBtn = QPushButton(tr("绑定"))
        bindBtn.setFixedHeight(_BTN_H)
        bindBtn.setFixedWidth(_BTN_W)
        bindBtn.setCheckable(True)
        bindBtn.setAttribute(Qt.WA_AlwaysShowToolTips, True)
        bindBtn.clicked.connect(self.dimensionBindToggled.emit)
        rl2.addWidget(bindBtn)
        rl2.addStretch()
        self._bindBtn = bindBtn
        dl.addWidget(row2)
        self._content.addWidget(self._dimBox)
        self._updateBindButtonState(None)   # 初始无选中：禁用

    def _buildCncCenterControls(self) -> None:
        """CNC 模式专用“居中”按钮：把选中物体整体 XY 居中到原点、
        最高点移到 Z=0（毛坯上表面对齐原点，方便雕刻）。

        仅 CNC 模式显示（与标注区互斥），与镜像按钮同尺寸、行首对齐。
        """
        self._cncCenterBox = QWidget()
        self._cncCenterBox.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        cl = QVBoxLayout(self._cncCenterBox)
        cl.setContentsMargins(0, 2, 0, 0)
        cl.setSpacing(2)
        row = QWidget()
        row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(4)
        self._cncCenterBtn = _makeToolButton(
            tr("居中"),
            tr("把选中物体整体 XY 居中到原点，最高点移到 Z=0，方便雕刻"))
        self._cncCenterBtn.clicked.connect(self._onCncCenter)
        rl.addWidget(self._cncCenterBtn)
        rl.addStretch()
        cl.addWidget(row)
        self._content.addWidget(self._cncCenterBox)
        self._cncCenterBox.setVisible(False)   # 默认隐藏，仅 CNC 模式显示

    def _onCncCenter(self) -> None:
        """居中：选中物体整体 XY 居中到原点，最高点移到 Z=0。

        - 合并所有选中物体的世界包围盒；
        - XY 平移使合并包围盒中心落到 (0, 0)；
        - Z 平移使合并包围盒最高点落到 Z=0（物体位于 Z<=0 半空间）。
        """
        if self._doc is None:
            return
        sel = self._doc.selectedObjects()
        if not sel:
            return
        bb = None
        for obj in sel:
            objBB = obj.getBoundingBox()
            if not objBB.isValid():
                continue
            if bb is None:
                bb = objBB
            else:
                bb.expand(objBB.min)
                bb.expand(objBB.max)
        if bb is None:
            return
        cx = (bb.min.x + bb.max.x) * 0.5
        cy = (bb.min.y + bb.max.y) * 0.5
        zMax = bb.max.z
        dx, dy, dz = -cx, -cy, -zMax
        self._doc.beginEdit()
        try:
            for obj in sel:
                obj.translate(dx, dy, dz)
                self._doc.notifyObjectChanged(obj.id())
        finally:
            self._doc.endEdit()

    def _buildReliefControls(self) -> None:
        """浮雕显示区：仅选中 SURFACE 模型时可见。

        分「2D 预览」（平面着色模式）与「浮雕模式」（3D 曲面显示：地图/颜色块/
        渲染材质）两组，按当前画布模式二选一显示；光照组 + 模型Z（设定模型高度）
        在 3D 相关模式下共用。
        """
        self._reliefBox = QWidget()
        self._reliefBox.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        rl = QVBoxLayout(self._reliefBox)
        rl.setContentsMargins(0, 6, 0, 0)
        rl.setSpacing(4)

        title = _makeLabel(tr("模型显示模式"), -1, "font-weight: bold; font-size: 12px;")
        title.setFixedHeight(16)
        rl.addWidget(title)
        self._modelDisplayLabel = title

        # ---- 2D 预览组（平面着色模式：等高线 / 灰度图）----
        self._mode2DBox = QWidget()
        m2 = QVBoxLayout(self._mode2DBox)
        m2.setContentsMargins(0, 0, 0, 0)
        m2.setSpacing(4)
        modeRow = QWidget()
        modeRow.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        mr = QHBoxLayout(modeRow)
        mr.setContentsMargins(0, 0, 0, 0)
        mr.setSpacing(2)
        self._mode2DGroup = QButtonGroup(self)
        self._mode2DButtons = []
        for key, text in _RELIEF_MODES:
            btn = QRadioButton(tr(text))
            btn.setStyleSheet(_radioStyle())
            btn.setFixedHeight(20)
            self._mode2DGroup.addButton(btn, len(self._mode2DButtons))
            self._mode2DButtons.append(btn)
            mr.addWidget(btn)
        mr.addStretch()
        m2.addWidget(modeRow)
        rl.addWidget(self._mode2DBox)
        self._mode2DGroup.buttonClicked.connect(self._onReliefModeChanged)

        # ---- 浮雕(3D)模式组（地图/颜色块/渲染材质）----
        self._mode3DBox = QWidget()
        m3 = QVBoxLayout(self._mode3DBox)
        m3.setContentsMargins(0, 0, 0, 0)
        m3.setSpacing(4)
        # 单选框水平排列（紧凑）
        mode3Row = QWidget()
        mode3Row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        m3r = QHBoxLayout(mode3Row)
        m3r.setContentsMargins(0, 0, 0, 0)
        m3r.setSpacing(2)
        self._mode3DGroup = QButtonGroup(self)
        self._mode3DButtons = []
        for key, text in _RELIEF_3D_MODES:
            btn = QRadioButton(tr(text))
            btn.setStyleSheet(_radioStyle())
            btn.setFixedHeight(20)
            self._mode3DGroup.addButton(btn, len(self._mode3DButtons))
            self._mode3DButtons.append(btn)
            m3r.addWidget(btn)
        m3r.addStretch()
        m3.addWidget(mode3Row)
        self._mode3DGroup.buttonClicked.connect(self._onRelief3DModeChanged)
        rl.addWidget(self._mode3DBox)

        # ---- 3D 模式实体显示组（线框 / 渲染）----
        self._modeSolidBox = QWidget()
        ms = QVBoxLayout(self._modeSolidBox)
        ms.setContentsMargins(0, 0, 0, 0)
        ms.setSpacing(4)
        solidRow = QWidget()
        solidRow.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        msr = QHBoxLayout(solidRow)
        msr.setContentsMargins(0, 0, 0, 0)
        msr.setSpacing(2)
        self._modeSolidGroup = QButtonGroup(self)
        self._modeSolidButtons = []
        for key, text in (("wireframe", "线框"), ("rendered", "渲染")):
            btn = QRadioButton(tr(text))
            btn.setStyleSheet(_radioStyle())
            btn.setFixedHeight(20)
            self._modeSolidGroup.addButton(btn, len(self._modeSolidButtons))
            self._modeSolidButtons.append(btn)
            msr.addWidget(btn)
        # 透视开关（渲染模式）：紧挨"渲染"单选并排——开=X-Ray 透视
        # （表面半透明、背面线可见），关=实体不透明 + 隐藏线消除
        # （只画可见特征边）；线框模式不受影响
        self._solidPerspectiveBtn = QPushButton(tr("透视"))
        self._solidPerspectiveBtn.setCheckable(True)
        self._solidPerspectiveBtn.setChecked(False)
        self._solidPerspectiveBtn.setFixedHeight(20)
        self._solidPerspectiveBtn.setFixedWidth(46)
        self._updateSolidPerspectiveStyle()
        self._solidPerspectiveBtn.clicked.connect(self._onSolidPerspectiveToggled)
        msr.addWidget(self._solidPerspectiveBtn)
        # 高度色开关（网格 STL 实体专用）：开=按高度地形色带逐顶点上色
        # （台阶/浅纹顶视可辨），关=单色实体（默认）。其他实体不受影响。
        self._meshHeightColorBtn = QPushButton(tr("高度色"))
        self._meshHeightColorBtn.setCheckable(True)
        self._meshHeightColorBtn.setChecked(False)
        self._meshHeightColorBtn.setFixedHeight(20)
        self._meshHeightColorBtn.setFixedWidth(46)
        self._updateMeshHeightColorStyle()
        self._meshHeightColorBtn.clicked.connect(
            self._onMeshHeightColorToggled)
        msr.addWidget(self._meshHeightColorBtn)
        msr.addStretch()
        ms.addWidget(solidRow)
        # 光影滑块（3D 实体光照）：方位角/仰角——低仰角斜射产生阴影，
        # 高仰角接顶光。对所有视图生效（顶视图阴影从画面上方来）。
        self._solidAzSlider, self._solidAzValue, _ = self._reliefSliderRow(
            ms, "方位", 0, 360, self._onSolidLightChanged)
        self._solidAzSlider.setValue(0)
        self._solidAltSlider, self._solidAltValue, _ = self._reliefSliderRow(
            ms, "仰角", 5, 85, self._onSolidLightChanged)
        self._solidAltSlider.setValue(60)
        self._updateSolidLightLabels()
        self._modeSolidGroup.buttonClicked.connect(self._onSolidModeChanged)
        rl.addWidget(self._modeSolidBox)

        # ---- 光照（浮雕3D模式下显示，供地图着色/立体渲染）----
        self._lightBox = QWidget()
        ll = QVBoxLayout(self._lightBox)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(2)
        self._azSlider, self._azValue, self._azLabel = self._reliefSliderRow(
            ll, "方位角", 0, 360, self._onReliefAzimuth)
        self._altSlider, self._altValue, self._altLabel = self._reliefSliderRow(
            ll, "仰角", 0, 90, self._onReliefAltitude)
        rl.addWidget(self._lightBox)

        # ---- 模型Z：直接设定模型高度（按输入值缩放高度场，替代原“垂直夸张”）----
        self._zHeightBox = QWidget()
        zhb = QHBoxLayout(self._zHeightBox)
        zhb.setContentsMargins(0, 0, 0, 0)
        zhb.setSpacing(4)
        zLabel = QPushButton(tr("模型Z"))
        zLabel.setFixedWidth(48)
        zLabel.setStyleSheet(
            "font-size: 12px; text-align: left; border: none; "
            "background: transparent; padding: 0; color: #333;")
        zLabel.setToolTip(tr("单击设置限高保底"))
        zLabel.clicked.connect(self._onModelZLabelClicked)
        zhb.addWidget(zLabel)
        self._zLabel = zLabel
        self._zHeightSpin = _makeSpinBox(90, rng=999999, decimals=3)
        self._zHeightSpin.setRange(0.0, 999999.0)
        zhb.addWidget(self._zHeightSpin)
        self._zCommitBtn = QPushButton(tr("确认"))
        # 信号连接曾丢失致“确认”变死键（2026-09-20 修复）：
        # 按钮 clicked → _onReliefZCommit → zHeightCommitRequested
        self._zCommitBtn.clicked.connect(self._onReliefZCommit)
        zhb.addWidget(self._zCommitBtn)
        zhb.addStretch()
        rl.addWidget(self._zHeightBox)


        self._canvasMode = "2d"
        self._content.addWidget(self._reliefBox)
        self._reliefBox.setVisible(True)   # 始终显示，不依赖选中
        self._updateReliefEnabled()        # 初始化时设置可见性
        languageChanged().connect(self.retranslateUi)

    def _reliefSliderRow(self, parentLayout, name: str, lo: int, hi: int, slot):
        row = QWidget()
        rowl = QHBoxLayout(row)
        rowl.setContentsMargins(0, 0, 0, 0)
        rowl.setSpacing(4)
        label = _makeLabel(tr(name), 48, "font-size: 12px;")
        rowl.addWidget(label)
        slider = QSlider(Qt.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(lo)
        slider.valueChanged.connect(slot)
        rowl.addWidget(slider, 1)
        value = QLabel()
        value.setFixedWidth(44)
        value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        value.setStyleSheet("font-size: 12px; color: #333;")
        rowl.addWidget(value)
        parentLayout.addWidget(row)
        return slider, value, label

    def _buildSizeControls(self) -> None:
        self._sizeSpins: list[QDoubleSpinBox] = []
        self._uniformChecks: list[QCheckBox] = []
        self._lockChecks: list[QCheckBox] = []
        for label in ("X:", "Y:", "Z:"):
            row = QWidget()
            row.setFixedHeight(30)
            row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(2)

            rl.addWidget(_makeLabel(label, 14, "font-size: 16px;"))
            sp = _makeSpinBox(80)
            sp.setKeyboardTracking(False)   # 仅在 Enter / 失焦时提交值
            sp.editingFinished.connect(self._onSizeSpinCommit)
            self._sizeSpins.append(sp)
            rl.addWidget(sp)
            uc = QCheckBox(tr("等比"))
            uc.setFixedHeight(26)
            # 勾选等比 = 该轴被动跟随其他轴，输入框变灰不可输入（参考 Rhino）
            uc.stateChanged.connect(self._updateRatioLockState)
            self._uniformChecks.append(uc)
            rl.addWidget(uc)
            lc = QCheckBox(tr("锁定"))
            lc.setFixedHeight(26)
            self._lockChecks.append(lc)
            rl.addWidget(lc)
            rl.addStretch()
            self._content.addWidget(row)
        self._updatingSize = False

    def _buildOriginControls(self) -> None:
        title = _makeLabel(tr("变换基点"), -1, "font-weight: bold; font-size: 12px;")
        title.setFixedHeight(16)
        self._content.addWidget(title)
        self._originLabel = title

        self._originGroups: list[QButtonGroup] = []
        self._radioButtons: list[QRadioButton] = []
        defaults = (1, 1, 0)
        for i, label in enumerate(("X:", "Y:", "Z:")):
            row = QWidget()
            row.setFixedHeight(18)
            row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(2)

            rl.addWidget(_makeLabel(label, 16, "font-size: 14px;"))
            group = QButtonGroup(row)
            for j, text in enumerate(("最小", "中间", "最大")):
                rb = QRadioButton(tr(text))
                rb.setFixedHeight(18)
                rb.setStyleSheet(_radioStyle())
                group.addButton(rb, j)
                if j == defaults[i]:
                    rb.setChecked(True)
                rb.installEventFilter(self)
                rb.setProperty("axis", i)
                rb.setProperty("opt", j)
                rl.addWidget(rb)
                self._radioButtons.append(rb)
            self._originGroups.append(group)
            rl.addStretch()
            self._content.addWidget(row)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.MouseButtonDblClick and isinstance(obj, QRadioButton):
            axis = obj.property("axis")
            opt = obj.property("opt")
            if axis is not None and opt is not None:
                self._moveToOrigin(int(axis), int(opt))
                return True
        return super().eventFilter(obj, event)

    def _moveToOrigin(self, axis: int, opt: int) -> None:
        """双击基点按钮：把选中物体的对应面移到原点。

        axis: 0=X, 1=Y, 2=Z
        opt: 0=最小, 1=中间, 2=最大
        例：双击 Z 最小 → 物体 Z 最小值移到 Z=0；双击 X 最大 → X 最大值移到 X=0。
        """
        if self._doc is None:
            return
        sel = self._doc.selectedObjects()
        if not sel:
            return
        bb = None
        for obj in sel:
            objBB = obj.getBoundingBox()
            if bb is None:
                bb = objBB
            else:
                bb.expand(objBB.min)
                bb.expand(objBB.max)
        if bb is None:
            return
        if axis == 0:
            val = bb.min.x if opt == 0 else (bb.max.x if opt == 2 else (bb.min.x + bb.max.x) * 0.5)
            dx, dy, dz = -val, 0.0, 0.0
        elif axis == 1:
            val = bb.min.y if opt == 0 else (bb.max.y if opt == 2 else (bb.min.y + bb.max.y) * 0.5)
            dx, dy, dz = 0.0, -val, 0.0
        else:
            val = bb.min.z if opt == 0 else (bb.max.z if opt == 2 else (bb.min.z + bb.max.z) * 0.5)
            dx, dy, dz = 0.0, 0.0, -val
        self._doc.beginEdit()
        try:
            for obj in sel:
                obj.translate(dx, dy, dz)
                self._doc.notifyObjectChanged(obj.id())
        finally:
            self._doc.endEdit()

    def refreshStyle(self) -> None:
        for rb in self._radioButtons:
            rb.setStyleSheet(_radioStyle())

    def retranslateUi(self) -> None:
        """语言切换：重设变换面板所有可见文本。"""
        # 移动/旋转/缩放三键
        for btn, text, tip in getattr(self, "_xformBtns", []):
            btn.setText(tr(text))
            btn.setToolTip(tr(tip))
        # 镜像三键
        for btn, text in getattr(self, "_mirrorBtns", []):
            btn.setText(tr(text))
        # 标注三键
        for btn, text, tip in getattr(self, "_dimBtns", []):
            btn.setText(tr(text))
            btn.setToolTip(tr(tip))
        # 大/小 按钮
        if hasattr(self, "_sizeBtn"):
            self._sizeBtn.setText(tr("大 / 小"))
            self._sizeBtn.setToolTip(tr("调整选中标注的读数文字大小：左键=放大(×1.25)，右键=缩小(÷1.25)"))
        # 测量按钮
        if hasattr(self, "_measureBtn"):
            self._measureBtn.setText(tr("测量"))
            self._measureBtn.setToolTip(tr("测量两点间距离：点击两个点后在左下角显示距离"))
        # 绑定按钮（文案由 _updateBindButtonState 按选中态设置）
        if hasattr(self, "_bindBtn"):
            self._updateBindButtonState(self._selectedObject)
        # CNC 居中按钮
        if hasattr(self, "_cncCenterBtn"):
            self._cncCenterBtn.setText(tr("居中"))
            self._cncCenterBtn.setToolTip(
                tr("把选中物体整体 XY 居中到原点，最高点移到 Z=0，方便雕刻"))
        # 模型显示模式标题
        if hasattr(self, "_modelDisplayLabel"):
            self._modelDisplayLabel.setText(tr("模型显示模式"))
        # 2D 预览模式单选
        for i, btn in enumerate(getattr(self, "_mode2DButtons", [])):
            if i < len(_RELIEF_MODES):
                btn.setText(tr(_RELIEF_MODES[i][1]))
        # 3D 模式单选
        for i, btn in enumerate(getattr(self, "_mode3DButtons", [])):
            if i < len(_RELIEF_3D_MODES):
                btn.setText(tr(_RELIEF_3D_MODES[i][1]))
        # 实体显示模式单选（线框/渲染）
        _solidTexts = ("线框", "渲染")
        for i, btn in enumerate(getattr(self, "_modeSolidButtons", [])):
            if i < len(_solidTexts):
                btn.setText(tr(_solidTexts[i]))
        # 透视开关
        if hasattr(self, "_solidPerspectiveBtn"):
            self._solidPerspectiveBtn.setText(tr("透视"))
        # 光照标签
        if hasattr(self, "_azLabel"):
            self._azLabel.setText(tr("方位角"))
        if hasattr(self, "_altLabel"):
            self._altLabel.setText(tr("仰角"))
        # 模型Z
        if hasattr(self, "_zLabel"):
            self._zLabel.setText(tr("模型Z"))
            self._zLabel.setToolTip(tr("单击设置限高保底"))
        if hasattr(self, "_zCommitBtn"):
            self._zCommitBtn.setText(tr("确认"))
        # 等比/锁定
        for uc in getattr(self, "_uniformChecks", []):
            uc.setText(tr("等比"))
        for lc in getattr(self, "_lockChecks", []):
            lc.setText(tr("锁定"))
        # 变换基点标题
        if hasattr(self, "_originLabel"):
            self._originLabel.setText(tr("变换基点"))
        # 最小/中间/最大 单选（每 3 个一组）
        _originTexts = ("最小", "中间", "最大")
        for i, rb in enumerate(getattr(self, "_radioButtons", [])):
            rb.setText(tr(_originTexts[i % 3]))

    def setDocument(self, doc) -> None:
        """注入文档：连接对象变化信号以实时刷新“模型Z”最高点显示。"""
        if doc is self._doc:
            return
        if self._doc is not None:
            try:
                self._doc.objectChanged.disconnect(self._onDocObjectChanged)
            except RuntimeError:
                pass
        self._doc = doc
        if doc is not None:
            doc.objectChanged.connect(self._onDocObjectChanged)

    def _onDocObjectChanged(self, oid: str) -> None:
        """浮雕目标模型高度场变化（雕刻/重建/撤销等）→ 实时刷新“模型Z”。

        输入框正在编辑时不打断；目标锁定模型（浮雕模式）优先，否则当前
        选中的 SURFACE 模型。
        """
        target = self._pinnedRelief
        if target is None:
            sel = self._doc.selectedObjects() if self._doc is not None else []
            target = self._reliefBoxTarget(sel[0] if sel else None)
        if target is None or target.id() != oid:
            return
        if self._zHeightSpin.hasFocus():
            return
        self._refreshZHeightSpin(target)

    def setReliefModel(self, model) -> None:
        """浮雕模式锁定的编辑目标（与选中解耦）：面板持续显示其显示参数。

        传入 None（离开浮雕模式）时回到按当前选中对象决定显示区。
        """
        self._pinnedRelief = model
        if model is not None:
            self._showReliefBox(model)
        else:
            sel = self._doc.selectedObjects() if self._doc is not None else []
            self._showReliefBox(self._reliefBoxTarget(sel[0] if sel else None))

    def _reliefBoxTarget(self, obj):
        """浮雕显示区目标：浮雕模式锁定的编辑目标优先，否则选中对象（若为 SURFACE）。"""
        if self._pinnedRelief is not None:
            return self._pinnedRelief
        if obj is not None and obj.type() == ObjectType.SURFACE:
            return obj
        return None

    def showObject(self, obj) -> None:
        self._selectedObject = obj
        self._updateBindButtonState(obj)
        if obj is None:
            self._selectedObjectId = None
            for sp in self._sizeSpins:
                sp.setValue(0)
            self._updateRatioLockState()
            self._showReliefBox(self._reliefBoxTarget(None))
            return
        self._selectedObjectId = obj.id()
        bb = obj.getBoundingBox()
        if bb.isValid():
            self._sizeSpins[0].setValue(bb.width())
            self._sizeSpins[1].setValue(bb.height())
            self._sizeSpins[2].setValue(bb.depth())
        self._updateRatioLockState()
        self._showReliefBox(self._reliefBoxTarget(obj))

    def _updateRatioLockState(self) -> None:
        """等比 / 锁定联动：参考 Rhino 插件 TransformUI.cs。

        - 任意轴勾选等比后：仅勾选等比的轴输入框可输入（主动轴），
          其他两个轴输入框变灰不可输入（被动跟随）；
        - 未勾选任何等比时：所有轴输入框可输入（互不影响）；
        - 浮雕模型 Z 不可经 2D 变换改（恒禁用，覆盖等比状态）；
        - 等比 checkbox 本身始终可用（方便切换）；
        - 锁定 checkbox 不影响输入框启用状态，仅在提交时控制该轴 sx=1。
        """
        anyRatio = any(uc.isChecked() for uc in self._uniformChecks)
        isReliefZ = (self._selectedObject is not None
                     and self._selectedObject.type() == ObjectType.SURFACE)
        for j, sp in enumerate(self._sizeSpins):
            if j == 2 and isReliefZ:
                sp.setEnabled(False)
                continue
            if anyRatio:
                # 仅勾选等比的轴可输入（主动轴），其余变灰（被动跟随）
                sp.setEnabled(self._uniformChecks[j].isChecked())
            else:
                sp.setEnabled(True)

    # ---------------- XYZ 尺寸输入提交：按变换基点缩放选中对象 ----------------

    def _computeOriginCenter(self, bb) -> Point3D:
        """根据变换基点单选按钮（最小 / 中间 / 最大）算出中心点。"""
        def pick(opt, lo, hi):
            if opt == 0:
                return lo
            if opt == 2:
                return hi
            return (lo + hi) * 0.5
        xOpt = self._originGroups[0].checkedId() if self._originGroups else 1
        yOpt = self._originGroups[1].checkedId() if len(self._originGroups) > 1 else 1
        zOpt = self._originGroups[2].checkedId() if len(self._originGroups) > 2 else 1
        cx = pick(xOpt if xOpt >= 0 else 1, bb.min.x, bb.max.x)
        cy = pick(yOpt if yOpt >= 0 else 1, bb.min.y, bb.max.y)
        cz = pick(zOpt if zOpt >= 0 else 1, bb.min.z, bb.max.z)
        return Point3D(cx, cy, cz)

    def _onSizeSpinCommit(self) -> None:
        """XYZ 尺寸输入框 Enter / 失焦：按变换基点缩放选中对象。

        规则（参考 Rhino 插件 TransformUI.cs）：
        - 勾选了任意轴等比：勾选等比的轴为主动轴（唯一可输入），其他轴变灰被动跟随；
          以主动轴的新值算主比例，所有未锁定轴按主比例缩放；
        - 未勾选任何等比：每轴独立，按各自新值算比例；
        - 锁定勾选的轴：保持不变（sx=1）；
        - 浮雕模型 Z 不可经 2D 变换改，sz=1。
        所有缩放均绕面板下方“变换基点”单选按钮选中的中心点进行。
        """
        if self._updatingSize or self._doc is None:
            return
        obj = self._selectedObject
        if obj is None:
            return
        oldBB = obj.getBoundingBox()
        if not oldBB.isValid():
            return
        oldDims = [oldBB.width(), oldBB.height(), oldBB.depth()]
        newDims = [self._sizeSpins[0].value(),
                   self._sizeSpins[1].value(),
                   self._sizeSpins[2].value()]
        diffs = [abs(newDims[i] - oldDims[i]) for i in range(3)]
        anyRatio = any(uc.isChecked() for uc in self._uniformChecks)
        # 计算每轴缩放因子
        sFactors = [1.0, 1.0, 1.0]
        if anyRatio:
            # 勾选了等比：以勾选等比的轴中变化最大的为主动轴
            ratioAxes = [i for i in range(3)
                         if self._uniformChecks[i].isChecked()]
            axisIdx = max(ratioAxes, key=lambda i: diffs[i])
            if diffs[axisIdx] < 1e-9:
                return   # 主动轴没变：无操作
            mainRatio = (newDims[axisIdx] / oldDims[axisIdx]
                         if oldDims[axisIdx] > 1e-9 else 1.0)
            for j in range(3):
                if self._lockChecks[j].isChecked():
                    sFactors[j] = 1.0   # 锁定：不变
                else:
                    sFactors[j] = mainRatio   # 未锁定：按主比例联动
        else:
            # 无等比：每轴独立，按各自新值算比例
            axisIdx = diffs.index(max(diffs))
            if diffs[axisIdx] < 1e-9:
                return   # 三轴都没变：无操作
            for j in range(3):
                if self._lockChecks[j].isChecked():
                    sFactors[j] = 1.0
                else:
                    sFactors[j] = (newDims[j] / oldDims[j]
                                   if oldDims[j] > 1e-9 else 1.0)
        # 浮雕模型 Z 不可经 2D 变换改
        if obj.type() == ObjectType.SURFACE:
            sFactors[2] = 1.0
        # 防止零或负值
        sFactors = [max(s, 1e-6) if s > 0 else 1.0 for s in sFactors]
        sx, sy, sz = sFactors
        # 绕变换基点缩放
        center = self._computeOriginCenter(oldBB)
        self._doc.beginEdit()
        try:
            obj.scaleAround(sx, sy, sz, center)
            self._doc.notifyObjectChanged(obj.id())
        finally:
            self._doc.endEdit()
        # 回填 sizeSpins 反映缩放后真实尺寸（防止用户输入的值与等比/锁定结果不一致）
        self._updatingSize = True
        try:
            newBB = obj.getBoundingBox()
            if newBB.isValid():
                self._sizeSpins[0].setValue(newBB.width())
                self._sizeSpins[1].setValue(newBB.height())
                self._sizeSpins[2].setValue(newBB.depth())
        finally:
            self._updatingSize = False

    def _updateBindButtonState(self, obj) -> None:
        """同步“绑定”开关：状态一目了然，不出现“无缘由的灰色”。

        - 选中「已绑定」的 XY/Z 标注：可用 + 按下高亮，文案“已绑定”，点击解除；
        - 选中「已解绑」的 XY/Z 标注：禁用，文案“已解绑”（脱离关系后不可重绑）；
        - 其余情况（无选中 / 普通对象 / 其他标注）：禁用，文案“绑定”。
        """
        btn = getattr(self, "_bindBtn", None)
        if btn is None:
            return
        if obj is None or obj.type() != ObjectType.DIMENSION or obj.kind() not in ("xy", "z"):
            btn.setEnabled(False)
            btn.setChecked(False)
            btn.setText(tr("绑定"))
            btn.setToolTip(tr("绑定：让标注跟随被标注的物体，物体改变尺寸时"
                              "标注实时更新（选中 XY/Z 标注后可查看或解除绑定）"))
            return
        if obj.boundTargetIds():
            btn.setEnabled(True)
            btn.setChecked(True)
            btn.setText(tr("已绑定"))
            btn.setToolTip(tr("该标注已与物体绑定：物体改变尺寸时标注实时更新。"
                              "点击可解除绑定（解除后脱离关系，无法重新绑定）"))
        else:
            btn.setEnabled(False)
            btn.setChecked(False)
            btn.setText(tr("已解绑"))
            btn.setToolTip(tr("该标注已解除绑定：不再跟随物体，且不可重新绑定"))

    # ---------------- 浮雕显示参数（仅 SURFACE） ----------------

    def _showReliefBox(self, model) -> None:
        """显示/隐藏浮雕显示区；model 为 ReliefModelObject 时回填其显示参数。"""
        self._reliefObj = model
        self._reliefBox.setVisible(True)   # 始终显示，不依赖选中
        if model is None:
            self._updatingRelief = True
            try:
                self._mode2DButtons[0].setChecked(True)  # 默认等高线
                # 默认“地图”(高度色带 terrain)：与用户"浮雕模式默认地图"主题一致。
                # （模型对象自身的 display3DMode 不变，仅面板按钮在未绑定时显示"地图"。
                #  绑定到模型时会按模型实际值回填；进入浮雕模式时会按模型值重新同步。）
                for i, (k, _t) in enumerate(_RELIEF_3D_MODES):
                    if k == "map" and i < len(self._mode3DButtons):
                        self._mode3DButtons[i].setChecked(True)
                        break
                self._zHeightSpin.setValue(0.0)
                self._setZHeightEnabled(False)
            finally:
                self._updatingRelief = False
            self._updateReliefEnabled()
            return
        self._updatingRelief = True
        try:
            # 2D 模式：等高线 / 灰度图
            mode2d = model.displayMode()
            idx2 = 0
            for i, (key, _text) in enumerate(_RELIEF_MODES):
                if key == mode2d:
                    idx2 = i
                    break
            if 0 <= idx2 < len(self._mode2DButtons):
                self._mode2DButtons[idx2].setChecked(True)
            # 找 3D 模式对应的索引
            mode3d = model.display3DMode()
            idx3 = 0
            for i, (key, _text) in enumerate(_RELIEF_3D_MODES):
                if key == mode3d:
                    idx3 = i
                    break
            if 0 <= idx3 < len(self._mode3DButtons):
                self._mode3DButtons[idx3].setChecked(True)
            az = int(round(model.lightAzimuth()))
            self._azSlider.setValue(az)
            self._azValue.setText(f"{az}°")
            alt = int(round(model.lightAltitude()))
            self._altSlider.setValue(alt)
            self._altValue.setText(f"{alt}°")
            self._refreshZHeightSpin(model)
        finally:
            self._updatingRelief = False
        self._updateReliefEnabled()

    def _refreshZHeightSpin(self, model) -> None:
        """回填“模型Z”为当前最高高度（最低点在 Z=0 时即模型整体高度）。

        输入框始终可编辑（平面模型显示 0，用户可直接键入目标高度）；
        确认按钮仅当模型当前确有高度（可缩放）时启用。
        """
        try:
            cur = float(model.heightField().data.max())
        except Exception:
            cur = 0.0
        if cur < 0.0:
            cur = 0.0
        self._zHeightSpin.setValue(cur)
        self._zHeightSpin.setEnabled(True)
        self._zCommitBtn.setEnabled(cur > 0.0)
        if cur <= 0.0:
            self._zCommitBtn.setToolTip(tr("当前模型为平面（无高度），无法缩放"))
        else:
            self._zCommitBtn.setToolTip(tr("按输入数值缩放高度场"))

    def _setZHeightEnabled(self, enabled: bool) -> None:
        """无目标模型时的整体禁用（浮雕模式下恒有目标，通常只出现在退出后）。"""
        self._zHeightSpin.setEnabled(enabled)
        self._zCommitBtn.setEnabled(enabled)

    def _updateReliefEnabled(self) -> None:
        """按画布模式切换显示组。

        2D 模式：等高线/灰度图；3D 模式：线框/渲染；浮雕模式：地图/颜色/渲染 + 光照 + 模型Z。
        显示模式是查看操作，与选中无关，与 Tab 快捷键行为一致。
        """
        mode = getattr(self, "_canvasMode", "2d")
        is2D = mode == "2d"
        is3D = mode == "3d"
        isRelief = mode == "relief"
        self._mode2DBox.setVisible(is2D)
        self._mode3DBox.setVisible(isRelief)
        self._modeSolidBox.setVisible(is3D)
        for btn in getattr(self, "_mode2DButtons", []):
            btn.setEnabled(is2D)
        # 方位角/仰角/模型Z仅浮雕模式需要
        self._lightBox.setVisible(isRelief)
        self._lightBox.setEnabled(isRelief)
        self._zHeightBox.setVisible(isRelief)
        self._zHeightBox.setEnabled(isRelief)

    def _applyRelief(self, fn) -> None:
        """把显示参数写回浮雕模型并通知文档刷新 2D 预览（回填期间不写回）。"""
        if self._updatingRelief or self._reliefObj is None or self._doc is None:
            return
        fn(self._reliefObj)
        self._doc.notifyObjectChanged(self._reliefObj.id())

    def _onReliefModeChanged(self) -> None:
        """2D 模式单选按钮点击：全局切换所有 SURFACE 模型的显示模式。

        显示模式是查看操作，与选中无关——文档中所有浮雕模型同时切到
        被点选的模式，与 Tab 快捷键行为一致。
        """
        btn = self._mode2DGroup.checkedButton()
        if btn is None or self._doc is None or self._updatingRelief:
            return
        idx = self._mode2DGroup.id(btn)
        if not (0 <= idx < len(_RELIEF_MODES)):
            return
        mode = _RELIEF_MODES[idx][0]
        surfaces = self._doc.reliefModels()
        if not surfaces:
            return
        for o in surfaces:
            o.setDisplayMode(mode)
            self._doc.notifyObjectChanged(o.id())

    def _onReliefAzimuth(self, v: int) -> None:
        self._azValue.setText(f"{v}°")
        self._applyRelief(lambda o: o.setLightAzimuth(float(v)))

    def _onReliefAltitude(self, v: int) -> None:
        self._altValue.setText(f"{v}°")
        self._applyRelief(lambda o: o.setLightAltitude(float(v)))

    def _onReliefZCommit(self) -> None:
        """“模型Z”确认：把目标高度发给主窗口缩放高度场（发前只校验非零）。"""
        if self._reliefObj is None or self._updatingRelief:
            return
        target = float(self._zHeightSpin.value())
        if target <= 0.0:
            return
        self.zHeightCommitRequested.emit(target)

    def _onModelZLabelClicked(self) -> None:
        """单击“模型Z”标题：弹出限高保底对话框，clip 高度场到 [保底, 限高]。"""
        if self._reliefObj is None or self._updatingRelief:
            return
        hf = self._reliefObj.heightField()
        data = hf.data
        if data.size == 0:
            return
        cur_min = float(data.min())
        cur_max = float(data.max())
        dlg = QDialog(self)
        dlg.setWindowTitle(tr("限高保底"))
        form = QFormLayout(dlg)
        minSpin = QDoubleSpinBox()
        minSpin.setRange(-999999.0, 999999.0)
        minSpin.setDecimals(3)
        minSpin.setValue(cur_min)
        maxSpin = QDoubleSpinBox()
        maxSpin.setRange(-999999.0, 999999.0)
        maxSpin.setDecimals(3)
        maxSpin.setValue(cur_max)
        form.addRow(tr("保底（最小）"), minSpin)
        form.addRow(tr("限高（最大）"), maxSpin)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        form.addRow(btns)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        if dlg.exec() != QDialog.Accepted:
            return
        z_min = float(minSpin.value())
        z_max = float(maxSpin.value())
        if z_min > z_max:
            z_min, z_max = z_max, z_min
        if abs(z_min - cur_min) < 1e-9 and abs(z_max - cur_max) < 1e-9:
            return
        self.zClipRequested.emit(z_min, z_max)

    # ---------------- 浮雕(3D)模式显示参数 ----------------

    def _onRelief3DModeChanged(self) -> None:
        btn = self._mode3DGroup.checkedButton()
        if btn is None:
            return
        idx = self._mode3DGroup.id(btn)
        if 0 <= idx < len(_RELIEF_3D_MODES):
            mode = _RELIEF_3D_MODES[idx][0]
            self._applyRelief(lambda o: o.setDisplay3DMode(mode))

    def _onSolidModeChanged(self) -> None:
        """3D 模式实体显示单选按钮点击：发信号通知主窗口切换视口显示模式。"""
        btn = self._modeSolidGroup.checkedButton()
        if btn is None:
            return
        idx = self._modeSolidGroup.id(btn)
        modes = ("wireframe", "rendered")
        if 0 <= idx < len(modes):
            self.solidDisplayModeRequested.emit(modes[idx])

    def setSolidDisplayMode(self, mode: str) -> None:
        """从外部（如视口 Tab 切换/模式切换回填）同步单选按钮选中态。"""
        modes = ("wireframe", "rendered")
        if mode not in modes:
            return
        idx = modes.index(mode)
        if 0 <= idx < len(self._modeSolidButtons):
            self._modeSolidButtons[idx].setChecked(True)

    def _onSolidPerspectiveToggled(self, on: bool) -> None:
        """3D 渲染模式透视开关点击：切换样式并发信号。"""
        self._updateSolidPerspectiveStyle()
        self.solidPerspectiveRequested.emit(on)

    def _onMeshHeightColorToggled(self, on: bool) -> None:
        """网格 STL 实体"高度色"开关点击：切换样式并发信号。"""
        self._updateMeshHeightColorStyle()
        self.meshHeightColorRequested.emit(on)

    def _onSolidLightChanged(self, _v: int) -> None:
        """3D 实体光影滑块拖动：刷新数值标签并发信号。"""
        self._updateSolidLightLabels()
        self.solidLightRequested.emit(int(self._solidAzSlider.value()),
                                      int(self._solidAltSlider.value()))

    def _updateSolidLightLabels(self) -> None:
        self._solidAzValue.setText("%d°" % self._solidAzSlider.value())
        self._solidAltValue.setText("%d°" % self._solidAltSlider.value())

    def setSolidLight(self, az: int, alt: int) -> None:
        """从外部回填光影滑块（不触发信号）。"""
        self._solidAzSlider.blockSignals(True)
        self._solidAltSlider.blockSignals(True)
        self._solidAzSlider.setValue(int(az))
        self._solidAltSlider.setValue(int(alt))
        self._solidAzSlider.blockSignals(False)
        self._solidAltSlider.blockSignals(False)
        self._updateSolidLightLabels()

    def _updateMeshHeightColorStyle(self) -> None:
        """高度色按钮样式：开=主色亮背景白字，关=暗背景灰字。"""
        if self._meshHeightColorBtn.isChecked():
            self._meshHeightColorBtn.setStyleSheet(
                "QPushButton { background: #15b6c9; color: white;"
                " border: none; border-radius: 3px; font-size: 12px; }")
        else:
            self._meshHeightColorBtn.setStyleSheet(
                "QPushButton { background: #888; color: #ddd;"
                " border: 1px solid #999; border-radius: 3px; font-size: 12px; }")

    def setMeshHeightColor(self, on: bool) -> None:
        """从外部回填高度色开关状态。"""
        self._meshHeightColorBtn.setChecked(on)
        self._updateMeshHeightColorStyle()

    def _updateSolidPerspectiveStyle(self) -> None:
        """透视按钮样式：开=主色亮背景白字，关=暗背景灰字。"""
        if self._solidPerspectiveBtn.isChecked():
            self._solidPerspectiveBtn.setStyleSheet(
                "QPushButton { background: #15b6c9; color: white;"
                " border: none; border-radius: 3px; font-size: 12px; }")
        else:
            self._solidPerspectiveBtn.setStyleSheet(
                "QPushButton { background: #888; color: #ddd;"
                " border: 1px solid #999; border-radius: 3px; font-size: 12px; }")

    def setSolidPerspective(self, on: bool) -> None:
        """从外部回填透视开关状态。"""
        self._solidPerspectiveBtn.setChecked(on)
        self._updateSolidPerspectiveStyle()

    # ---------------- 显示模式循环切换（Tab 快捷键调用） ----------------

    def cycleDisplayMode(self) -> None:
        """2D 模式下全局循环切换显示模式：等高线 ↔ 灰度图。

        这是查看操作，与选中无关——文档中所有 SURFACE 模型同时切换为下一模式。
        同步面板单选按钮（仅当面板当前显示的 _reliefObj 也在被切集合内时回填）。
        """
        if self._doc is None:
            return
        surfaces = self._doc.reliefModels()
        if not surfaces:
            return
        # 以首个对象的当前模式为基准，循环到下一个
        cur = surfaces[0].displayMode()
        keys = [k for k, _t in _RELIEF_MODES]
        nxt = keys[(keys.index(cur) + 1) % len(keys)] if cur in keys else keys[0]
        # 全局切换：所有 SURFACE 模型设为新模式并通知刷新
        for o in surfaces:
            o.setDisplayMode(nxt)
            self._doc.notifyObjectChanged(o.id())
        # 同步面板单选按钮（抑制信号避免触发 _onReliefModeChanged 二次写回）
        self._updatingRelief = True
        try:
            for i, (key, _t) in enumerate(_RELIEF_MODES):
                if key == nxt and i < len(self._mode2DButtons):
                    self._mode2DButtons[i].setChecked(True)
                    break
        finally:
            self._updatingRelief = False

    def cycleDisplay3DMode(self) -> None:
        """浮雕模式下全局循环切换 3D 显示模式：**只在 地图(高度色带) ↔ 颜色** 两者间循环。

        这是查看操作，与选中无关——文档中所有 SURFACE 模型同时切换为下一模式。
        "渲染"(material) 保留在右侧变换面板 3D 组的第三个按钮供手动点选，但
        Tab 不再绕到它——用户要求："TAB 键切换只在地图和颜色 2 个循环切换"。
        """
        if self._doc is None:
            return
        surfaces = self._doc.reliefModels()
        if not surfaces:
            return
        cur = surfaces[0].display3DMode()
        pair = ["map", "color"]
        # 当前如果是 material（渲染）或其它未知值：按 Tab 直接进入循环起点 → map，
        # 保证接下来 Tab 都是 map↔color 二选一。
        if cur in pair:
            nxt = pair[(pair.index(cur) + 1) % len(pair)]
        else:
            nxt = pair[0]   # 当前不在循环集合里 → 默认先落到"地图"
        for o in surfaces:
            o.setDisplay3DMode(nxt)
            self._doc.notifyObjectChanged(o.id())
        # 同步面板单选按钮（三按钮全组仍存在，只是 Tab 不绕 material）
        self._updatingRelief = True
        try:
            for i, (key, _t) in enumerate(_RELIEF_3D_MODES):
                if key == nxt and i < len(self._mode3DButtons):
                    self._mode3DButtons[i].setChecked(True)
                    break
        finally:
            self._updatingRelief = False

    def setCanvasMode(self, mode: str) -> None:
        """画布模式变化：浮雕显示区在「2D 预览」组与「浮雕(3D)」组间切换。

        CNC 模式下隐藏标注区（XY/对齐/Z 标注、大/小、测量、绑定），
        显示“居中”按钮；其他模式反之。
        """
        self._canvasMode = mode
        isCnc = mode == "cnc"
        if hasattr(self, "_dimBox"):
            self._dimBox.setVisible(not isCnc)
        if hasattr(self, "_cncCenterBox"):
            self._cncCenterBox.setVisible(isCnc)
        self._updateReliefEnabled()

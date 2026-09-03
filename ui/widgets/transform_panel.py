"""变换面板：平移 / 旋转 / 缩放参数编辑与镜像快捷操作。"""
from __future__ import annotations
from typing import Optional
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                                QDoubleSpinBox, QCheckBox, QComboBox, QSlider,
                                QRadioButton, QButtonGroup,
                                QScrollArea, QFrame, QSizePolicy, QPushButton)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent
import ui.styles.theme as theme
from core.vector.geometry_types import ObjectType

# 浮雕 2D 预览显示模式：(对象存储值, 界面文案)；默认“等高线”置首
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
        " background: #f5f5f5; image: url(icons/plus.svg); }"
        "QDoubleSpinBox::down-button { width: 16px; border: none;"
        " background: #f5f5f5; image: url(icons/minus.svg); }"
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
    dimensionRequested = Signal(str)   # 标注："xy" / "align" / "z"
    dimensionTextBiggerRequested = Signal()    # 放大选中标注的读数文字
    dimensionTextSmallerRequested = Signal()   # 缩小选中标注的读数文字
    dimensionBindToggled = Signal()    # 关闭选中 XY 标注与物体的绑定（永久解除）
    moveRequested = Signal()           # 面板移动命令：两点平移选中对象
    rotateRequested = Signal()         # 面板旋转命令：两点定轴+拖动旋转
    scaleRequested = Signal()          # 面板缩放命令：两点定参考+拖动缩放
    measureRequested = Signal()        # 测量命令：点两个点，左下角显示两点距离


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
        self._buildReliefControls()

    def _buildXformControls(self) -> None:
        """移动 / 旋转 / 缩放 三键（放在镜像三键上方，尺寸与镜像按钮一致）。"""
        row = QWidget()
        row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(4)
        for key, text, tip in (
                ("move", "移动", "选中对象后：点基准点 → 点目标点完成平移"),
                ("rotate", "旋转", "选中对象后：点中心点 → 点参考起点 → 拖动或点目标角度确认"),
                ("scale", "缩放", "选中对象后：点基准点 → 点参考起点 → 拖动或点目标距离确认")):
            btn = _makeToolButton(text, tip)
            if key == "move":
                btn.clicked.connect(self.moveRequested.emit)
            elif key == "rotate":
                btn.clicked.connect(self.rotateRequested.emit)
            elif key == "scale":
                btn.clicked.connect(self.scaleRequested.emit)
            rl.addWidget(btn)
        rl.addStretch()
        self._content.addWidget(row)

    def _buildMirrorControls(self) -> None:
        row = QWidget()
        row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(4)
        for axis, text in self.MIRROR_AXES:
            btn = _makeToolButton(text)
            btn.clicked.connect(lambda _, a=axis: self.mirrorRequested.emit(a))
            rl.addWidget(btn)
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
        """
        # 镜像行与标注第一行之间的 2px 间距
        self._content.addSpacing(2)
        # ---- 第一行：创建标注（与镜像按钮同尺寸）----
        row = QWidget()
        row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(4)
        for key, text, tip in (
                ("xy", "XY标注", "标注选中对象的 X 宽与 Y 高尺寸"),
                ("align", "对齐标注", "点击两个点标注其间距（任意方向）"),
                ("z", "Z标注", "Z 标注占位：后续版本用于为模型标注高度")):
            btn = _makeToolButton(text, tip)
            btn.clicked.connect(
                lambda _, k=key: self.dimensionRequested.emit(k))
            rl.addWidget(btn)
        rl.addStretch()
        self._content.addWidget(row)

        # ---- 第二行：调整已选标注的读数文字大小（大/小 合并为一个左右键按钮）
        #           + 测量（两点距离）+ 绑定开关 ----
        row2 = QWidget()
        row2.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        rl2 = QHBoxLayout(row2)
        rl2.setContentsMargins(0, 2, 0, 0)
        rl2.setSpacing(4)
        # 大/小 双用按钮：左键=放大选中标注读数文字，右键=缩小
        sizeBtn = LeftRightPushButton(
            "大 / 小",
            "调整选中标注的读数文字大小：左键=放大(×1.25)，右键=缩小(÷1.25)")
        sizeBtn.leftClicked.connect(self.dimensionTextBiggerRequested.emit)
        sizeBtn.rightClicked.connect(self.dimensionTextSmallerRequested.emit)
        rl2.addWidget(sizeBtn)
        # 测量按钮：点击进入测量命令，取两个点显示距离
        measureBtn = _makeToolButton("测量",
                                     "测量两点间距离：点击两个点后在左下角显示距离")
        measureBtn.clicked.connect(self.measureRequested.emit)
        rl2.addWidget(measureBtn)
        # 绑定开关紧随测量按钮，与上面两排按钮同宽同高、逐列对齐；
        # 文案随选中状态变化：绑定 / 已绑定 / 已解绑
        bindBtn = QPushButton("绑定")
        bindBtn.setFixedHeight(_BTN_H)
        bindBtn.setFixedWidth(_BTN_W)
        bindBtn.setCheckable(True)
        bindBtn.setAttribute(Qt.WA_AlwaysShowToolTips, True)
        bindBtn.clicked.connect(self.dimensionBindToggled.emit)
        rl2.addWidget(bindBtn)
        rl2.addStretch()
        self._bindBtn = bindBtn
        self._content.addWidget(row2)
        self._updateBindButtonState(None)   # 初始无选中：禁用

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

        title = _makeLabel("模型显示模式", -1, "font-weight: bold; font-size: 12px;")
        title.setFixedHeight(16)
        rl.addWidget(title)

        # ---- 2D 预览组（平面着色模式：等高线 / 灰度图）----
        self._mode2DBox = QWidget()
        m2 = QVBoxLayout(self._mode2DBox)
        m2.setContentsMargins(0, 0, 0, 0)
        m2.setSpacing(4)
        modeRow = QWidget()
        modeRow.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        mr = QHBoxLayout(modeRow)
        mr.setContentsMargins(0, 0, 0, 0)
        mr.setSpacing(6)
        self._modeCombo = QComboBox()
        self._modeCombo.setMaximumWidth(130)
        for key, text in _RELIEF_MODES:
            self._modeCombo.addItem(text, key)

        self._modeCombo.currentIndexChanged.connect(self._onReliefModeChanged)
        mr.addWidget(self._modeCombo)
        mr.addStretch()
        m2.addWidget(modeRow)
        rl.addWidget(self._mode2DBox)

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
            btn = QRadioButton(text)
            btn.setStyleSheet(_radioStyle())
            btn.setFixedHeight(20)
            self._mode3DGroup.addButton(btn, len(self._mode3DButtons))
            self._mode3DButtons.append(btn)
            m3r.addWidget(btn)
        m3r.addStretch()
        m3.addWidget(mode3Row)
        self._mode3DGroup.buttonClicked.connect(self._onRelief3DModeChanged)
        rl.addWidget(self._mode3DBox)

        # ---- 光照（浮雕3D模式下显示，供地图着色/立体渲染）----
        self._lightBox = QWidget()
        ll = QVBoxLayout(self._lightBox)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(2)
        self._azSlider, self._azValue = self._reliefSliderRow(
            ll, "方位角", 0, 360, self._onReliefAzimuth)
        self._altSlider, self._altValue = self._reliefSliderRow(
            ll, "仰角", 0, 90, self._onReliefAltitude)
        rl.addWidget(self._lightBox)

        # ---- 模型Z：直接设定模型高度（按输入值缩放高度场，替代原“垂直夸张”）----
        self._zHeightBox = QWidget()
        zhb = QHBoxLayout(self._zHeightBox)
        zhb.setContentsMargins(0, 0, 0, 0)
        zhb.setSpacing(4)
        zhb.addWidget(_makeLabel("模型Z", 48, "font-size: 12px;"))
        self._zHeightSpin = _makeSpinBox(90, rng=999999, decimals=3)
        self._zHeightSpin.setRange(0.0, 999999.0)
        zhb.addWidget(self._zHeightSpin)
        self._zCommitBtn = QPushButton("确认")
        self._zCommitBtn.setFixedHeight(28)
        self._zCommitBtn.setFixedWidth(52)
        self._zCommitBtn.clicked.connect(self._onReliefZCommit)
        zhb.addWidget(self._zCommitBtn)
        zhb.addStretch()
        rl.addWidget(self._zHeightBox)


        self._canvasMode = "2d"
        self._reliefBox.setVisible(True)   # 始终显示，不依赖选中
        self._content.addWidget(self._reliefBox)
        self._updateReliefEnabled()        # 初始化时设置可见性

    def _reliefSliderRow(self, parentLayout, name: str, lo: int, hi: int, slot):
        row = QWidget()
        rowl = QHBoxLayout(row)
        rowl.setContentsMargins(0, 0, 0, 0)
        rowl.setSpacing(4)
        rowl.addWidget(_makeLabel(name, 48, "font-size: 12px;"))
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
        return slider, value

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
            self._sizeSpins.append(sp)
            rl.addWidget(sp)
            uc = QCheckBox("等比")
            uc.setFixedHeight(26)
            self._uniformChecks.append(uc)
            rl.addWidget(uc)
            lc = QCheckBox("锁定")
            lc.setFixedHeight(26)
            self._lockChecks.append(lc)
            rl.addWidget(lc)
            rl.addStretch()
            self._content.addWidget(row)

    def _buildOriginControls(self) -> None:
        title = _makeLabel("变换基点", -1, "font-weight: bold; font-size: 12px;")
        title.setFixedHeight(16)
        self._content.addWidget(title)

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
                rb = QRadioButton(text)
                rb.setFixedHeight(18)
                rb.setStyleSheet(_radioStyle())
                group.addButton(rb, j)
                if j == defaults[i]:
                    rb.setChecked(True)
                rl.addWidget(rb)
                self._radioButtons.append(rb)
            self._originGroups.append(group)
            rl.addStretch()
            self._content.addWidget(row)

    def refreshStyle(self) -> None:
        for rb in self._radioButtons:
            rb.setStyleSheet(_radioStyle())

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
            self._sizeSpins[2].setEnabled(True)
            self._showReliefBox(self._reliefBoxTarget(None))
            return
        self._selectedObjectId = obj.id()
        # 浮雕模型 Z（高度）锁定：不可经 2D 变换改变，只能在浮雕编辑态改
        isRelief = obj.type() == ObjectType.SURFACE
        self._sizeSpins[2].setEnabled(not isRelief)
        bb = obj.getBoundingBox()
        if bb.isValid():
            self._sizeSpins[0].setValue(bb.width())
            self._sizeSpins[1].setValue(bb.height())
            self._sizeSpins[2].setValue(bb.depth())
        self._showReliefBox(self._reliefBoxTarget(obj))

    def _updateBindButtonState(self, obj) -> None:
        """同步“绑定”开关：状态一目了然，不出现“无缘由的灰色”。

        - 选中「已绑定」的 XY 标注：可用 + 按下高亮，文案“已绑定”，点击解除；
        - 选中「已解绑」的 XY 标注：禁用，文案“已解绑”（脱离关系后不可重绑）；
        - 其余情况（无选中 / 普通对象 / 其他标注）：禁用，文案“绑定”。
        """
        btn = getattr(self, "_bindBtn", None)
        if btn is None:
            return
        if obj is None or obj.type() != ObjectType.DIMENSION or obj.kind() != "xy":
            btn.setEnabled(False)
            btn.setChecked(False)
            btn.setText("绑定")
            btn.setToolTip("绑定：让 XY 标注跟随被标注的物体，物体改变尺寸时"
                           "标注实时更新（选中 XY 标注后可查看或解除绑定）")
            return
        if obj.boundTargetIds():
            btn.setEnabled(True)
            btn.setChecked(True)
            btn.setText("已绑定")
            btn.setToolTip("该 XY 标注已与物体绑定：物体改变尺寸时标注实时更新。"
                           "点击可解除绑定（解除后脱离关系，无法重新绑定）")
        else:
            btn.setEnabled(False)
            btn.setChecked(False)
            btn.setText("已解绑")
            btn.setToolTip("该 XY 标注已解除绑定：不再跟随物体，且不可重新绑定")

    # ---------------- 浮雕显示参数（仅 SURFACE） ----------------

    def _showReliefBox(self, model) -> None:
        """显示/隐藏浮雕显示区；model 为 ReliefModelObject 时回填其显示参数。"""
        self._reliefObj = model
        self._reliefBox.setVisible(True)   # 始终显示，不依赖选中
        if model is None:
            self._updatingRelief = True
            try:
                self._modeCombo.setCurrentIndex(0)  # 默认等高线
                self._mode3DButtons[0].setChecked(True)  # 默认地图
                self._zHeightSpin.setValue(0.0)
                self._setZHeightEnabled(False)
            finally:
                self._updatingRelief = False
            self._updateReliefEnabled()
            return
        self._updatingRelief = True
        try:
            idx = self._modeCombo.findData(model.displayMode())
            self._modeCombo.setCurrentIndex(0 if idx < 0 else idx)
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
            self._zCommitBtn.setToolTip("当前模型为平面（无高度），无法缩放")
        else:
            self._zCommitBtn.setToolTip("按输入数值缩放高度场")

    def _setZHeightEnabled(self, enabled: bool) -> None:
        """无目标模型时的整体禁用（浮雕模式下恒有目标，通常只出现在退出后）。"""
        self._zHeightSpin.setEnabled(enabled)
        self._zCommitBtn.setEnabled(enabled)

    def _updateReliefEnabled(self) -> None:
        """按画布模式切换 2D/3D 显示组，并按是否为目标模型启停 2D 显示控件。"""
        in3D = getattr(self, "_canvasMode", "2d") in ("relief", "3d")
        self._mode2DBox.setVisible(not in3D)
        self._mode3DBox.setVisible(in3D)
        # 2D 预览无目标模型（未选中 SURFACE）时禁用显示模式，避免切了不生效
        self._modeCombo.setEnabled(not in3D and self._reliefObj is not None)
        # 方位角/仰角/模型Z仅浮雕(3D)模式需要，2D 模式下不显示
        self._lightBox.setVisible(in3D)
        self._lightBox.setEnabled(in3D)
        self._zHeightBox.setVisible(in3D)
        self._zHeightBox.setEnabled(in3D)

    def _applyRelief(self, fn) -> None:
        """把显示参数写回浮雕模型并通知文档刷新 2D 预览（回填期间不写回）。"""
        if self._updatingRelief or self._reliefObj is None or self._doc is None:
            return
        fn(self._reliefObj)
        self._doc.notifyObjectChanged(self._reliefObj.id())

    def _onReliefModeChanged(self, _idx: int) -> None:
        mode = self._modeCombo.currentData()
        self._updateReliefEnabled()
        self._applyRelief(lambda o: o.setDisplayMode(mode))

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

    # ---------------- 浮雕(3D)模式显示参数 ----------------

    def _onRelief3DModeChanged(self) -> None:
        btn = self._mode3DGroup.checkedButton()
        if btn is None:
            return
        idx = self._mode3DGroup.id(btn)
        if 0 <= idx < len(_RELIEF_3D_MODES):
            mode = _RELIEF_3D_MODES[idx][0]
            self._applyRelief(lambda o: o.setDisplay3DMode(mode))

    def setCanvasMode(self, mode: str) -> None:
        """画布模式变化：浮雕显示区在「2D 预览」组与「浮雕(3D)」组间切换。"""
        self._canvasMode = mode
        self._updateReliefEnabled()

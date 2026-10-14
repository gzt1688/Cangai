"""设置对话框：外观（主题色）等偏好设置。"""
from __future__ import annotations
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                                QPushButton, QGroupBox, QFormLayout,
                                QSpinBox, QDoubleSpinBox, QColorDialog,
                                QCheckBox, QWidget, QListWidget, QStackedWidget,
                                QButtonGroup, QRadioButton, QComboBox,
                                QLineEdit, QFileDialog, QFontComboBox)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence
import ui.styles.theme as theme
import core.app_settings as app_settings
from core.api import API_VERSION
from i18n import tr, languageChanged


# 预设主题色：青 / 橙 / 绿 / 蓝
_PRESET_COLORS = ["#15b6c9", "#ff6400", "#27ae60", "#1e88e5"]

# 导航页名（key 顺序对应 _stack 各页）
_NAV_NAMES = ["外观样式", "渲染性能", "通用", "文件", "键盘", "插件", "说明"]

# 软件基本信息（设置-说明页展示；发新版时更新版本号）
APP_NAME = "Cangai 浮雕设计"
APP_VERSION = "V1.0"


class SettingsDialog(QDialog):
    appearanceChanged = Signal()
    gridVisibleChanged = Signal(bool)
    axisVisibleChanged = Signal(bool)
    selectionBoxChanged = Signal(bool)
    fileSlotsChanged = Signal()
    renderSettingsChanged = Signal()
    dimensionSettingsChanged = Signal()   # 标注样式（字体/字号/单位）变更

    def __init__(self, parent=None, pluginManager=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("设置"))
        self.setMinimumSize(560, 480)
        self.setStyleSheet(f"background: {theme.BG_PANEL};")
        self._pluginManager = pluginManager

        self._renderDirty = False   # 渲染性能页有改动（应用/确认时才广播）
        self._dimDirty = False      # 标注样式页有改动（应用/确认时才广播）
        self._snapshot = self._takeSnapshot()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        outer = QHBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._nav = QListWidget()
        self._nav.setFixedWidth(140)
        self._applyNavStyle()
        for name in _NAV_NAMES:
            self._nav.addItem(tr(name))
        self._nav.setCurrentRow(0)
        self._nav.currentRowChanged.connect(self._onNavChanged)
        outer.addWidget(self._nav)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._buildAppearancePage())
        self._stack.addWidget(self._buildRenderPage())
        self._stack.addWidget(self._buildGeneralPage())
        self._stack.addWidget(self._buildFilePage())
        self._stack.addWidget(self._buildKeyboardPage())
        self._stack.addWidget(self._buildPluginsPage())
        self._stack.addWidget(self._buildOtherPage())
        outer.addWidget(self._stack, 1)
        root.addLayout(outer, 1)

        btnBar = QHBoxLayout()
        btnBar.setContentsMargins(12, 8, 12, 8)
        btnBar.setSpacing(10)
        btnBar.addStretch()
        _btnStyle = (
            "QPushButton { background: #e0e0e0; color: #333; border: 1px solid #bbb;"
            " border-radius: 3px; padding: 4px 0; }"
            "QPushButton:hover { background: #d0d0d0; }"
            "QPushButton:pressed { background: #c0c0c0; }")
        self._btnApply = QPushButton(tr("应用"))
        self._btnApply.setFixedWidth(64)
        self._btnApply.setStyleSheet(_btnStyle)
        self._btnApply.clicked.connect(self._onApply)
        btnBar.addWidget(self._btnApply)
        self._btnOk = QPushButton(tr("确认"))
        self._btnOk.setFixedWidth(64)
        self._btnOk.setStyleSheet(
            f"QPushButton {{ background: {theme.PRIMARY}; color: white; border: none; border-radius: 3px; padding: 4px 0; }}"
            f"QPushButton:hover {{ background: {theme.PRIMARY_DARK}; }}")
        self._btnOk.clicked.connect(self._onOk)
        btnBar.addWidget(self._btnOk)
        self._btnCancel = QPushButton(tr("取消"))
        self._btnCancel.setFixedWidth(64)
        self._btnCancel.setStyleSheet(_btnStyle)
        self._btnCancel.clicked.connect(self._onCancel)
        btnBar.addWidget(self._btnCancel)
        root.addLayout(btnBar)

        self._pendingColor = theme.PRIMARY
        self.appearanceChanged.connect(self._applyNavStyle)
        # 语言变更：重绘对话框自身可见文本
        languageChanged().connect(self.retranslateUi)

    def _takeSnapshot(self) -> dict:
        import copy
        return {
            "language": app_settings.language,
            "unit": app_settings.unit,
            "tolerance": app_settings.tolerance,
            "angle_precision": app_settings.angle_precision,
            "file_save_slots": copy.deepcopy(app_settings.file_save_slots),
            "PRIMARY": theme.PRIMARY,
            "FONT_SIZE": theme.FONT_SIZE,
            "QUICK_GROUP_COLORS": list(theme.QUICK_GROUP_COLORS),
            "SELECTION_CURVE": theme.SELECTION_CURVE,
            "SELECTION_MESH": theme.SELECTION_MESH,
            "perf": app_settings.perf_snapshot(),
            "dim_font": app_settings.dim_font,
            "dim_text_px": app_settings.dim_text_px,
            "dim_unit": app_settings.dim_unit,
        }

    def _restoreSnapshot(self) -> None:
        s = self._snapshot
        app_settings.language = s["language"]
        app_settings.unit = s["unit"]
        app_settings.tolerance = s["tolerance"]
        app_settings.angle_precision = s["angle_precision"]
        app_settings.file_save_slots = s["file_save_slots"]
        theme.PRIMARY = s["PRIMARY"]
        theme.FONT_SIZE = s["FONT_SIZE"]
        # 原地写回：其他模块可能持有列表引用
        theme.QUICK_GROUP_COLORS[:] = s["QUICK_GROUP_COLORS"]
        theme.SELECTION_CURVE = s["SELECTION_CURVE"]
        theme.SELECTION_MESH = s["SELECTION_MESH"]
        app_settings.perf_restore(s.get("perf", {}))
        app_settings.dim_font = s.get("dim_font", app_settings.dim_font)
        app_settings.dim_text_px = s.get("dim_text_px", app_settings.dim_text_px)
        app_settings.dim_unit = s.get("dim_unit", app_settings.dim_unit)

    # ---- 类别脏判定：应用/确认只对实际改动的类别发信号，避免全量广播 ----

    def _appearanceDirty(self) -> bool:
        s = self._snapshot
        return (theme.PRIMARY != s["PRIMARY"]
                or theme.FONT_SIZE != s["FONT_SIZE"]
                or list(theme.QUICK_GROUP_COLORS) != list(s["QUICK_GROUP_COLORS"])
                or theme.SELECTION_CURVE != s["SELECTION_CURVE"]
                or theme.SELECTION_MESH != s["SELECTION_MESH"])

    def _fileSlotsDirty(self) -> bool:
        return app_settings.file_save_slots != self._snapshot["file_save_slots"]

    def _onApply(self) -> None:
        self._saveShortcuts()
        app_settings.save_perf()
        if self._appearanceDirty():
            self.appearanceChanged.emit()
        if self._fileSlotsDirty():
            self.fileSlotsChanged.emit()
        if self._renderDirty:
            self.renderSettingsChanged.emit()
        if self._dimDirty:
            self.dimensionSettingsChanged.emit()

    def _onOk(self) -> None:
        self._onApply()
        self.accept()

    def _onCancel(self) -> None:
        self._restoreSnapshot()
        # 回滚是低频路径：全量广播保证各处按快照状态刷新
        self.appearanceChanged.emit()
        self.fileSlotsChanged.emit()
        self.renderSettingsChanged.emit()
        self.dimensionSettingsChanged.emit()
        self.reject()

    def _applyNavStyle(self) -> None:
        self._nav.setStyleSheet(
            f"QListWidget {{ background: #f0f0f0; border: none; border-right: 1px solid {theme.BORDER};"
            " outline: none; }"
            "QListWidget::item { padding: 10px 12px; border: none; }"
            f"QListWidget::item:selected {{ background: {theme.PRIMARY}; color: white; }}"
        )

    def _onNavChanged(self, row: int) -> None:
        self._stack.setCurrentIndex(row)

    def _buildAppearancePage(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)


        box = QGroupBox("")
        box.setStyleSheet("QGroupBox { border: none; padding: 0; margin: 0; }")
        form = QFormLayout(box)
        form.setSpacing(6)

        self._fontSpin = QSpinBox()
        self._fontSpin.setRange(8, 20)
        self._fontSpin.setFixedWidth(60)
        self._fontSpin.setValue(theme.FONT_SIZE)
        self._fontSpin.valueChanged.connect(self._onFontChanged)
        form.addRow("字体大小(pt):", self._fontSpin)

        colorRow = QHBoxLayout()
        colorRow.setSpacing(8)
        self._colorGroup = QButtonGroup(self)
        self._colorBtns = []
        for i, hexc in enumerate(_PRESET_COLORS):
            rb = QRadioButton()
            rb.setFixedWidth(40)
            rb.setFixedHeight(24)
            rb.setStyleSheet(
                f"QRadioButton::indicator {{ width: 18px; height: 18px;"
                f" border-radius: 9px; border: 2px solid #999; background: {hexc}; }}"
                f"QRadioButton::indicator:checked {{ border: 2px solid #333; }}"
            )
            self._colorGroup.addButton(rb, i)
            self._colorBtns.append(rb)
            colorRow.addWidget(rb)
        self._customColorRb = QRadioButton("自定义")
        self._customColorRb.setStyleSheet(
            "QRadioButton::indicator { width: 18px; height: 18px;"
            " border-radius: 9px; border: 2px solid #999; background: #888; }"
            "QRadioButton::indicator:checked { border: 2px solid #333; }"
        )
        self._colorGroup.addButton(self._customColorRb, len(_PRESET_COLORS))
        self._colorBtns.append(self._customColorRb)
        colorRow.addWidget(self._customColorRb)
        self._customColorBtn = QPushButton()
        self._customColorBtn.setFixedWidth(50)
        self._customColorBtn.setFixedHeight(24)
        self._customColorBtn.setStyleSheet(
            f"background: {theme.PRIMARY}; border: 1px solid #999; border-radius: 2px;"
        )
        self._customColorBtn.clicked.connect(self._pickColor)
        colorRow.addWidget(self._customColorBtn)
        colorRow.addStretch()
        try:
            idx = _PRESET_COLORS.index(theme.PRIMARY.lower())
            self._colorGroup.button(idx).setChecked(True)
        except ValueError:
            self._customColorRb.setChecked(True)
        self._colorGroup.buttonClicked.connect(self._onColorPreset)
        form.addRow("主题色:", colorRow)

        self._selCurveBtn = self._makeColorSwatch(theme.SELECTION_CURVE)
        self._selCurveBtn.clicked.connect(lambda: self._pickSelectionColor("curve"))
        form.addRow("选中曲线高亮:", self._selCurveBtn)
        self._selMeshBtn = self._makeColorSwatch(theme.SELECTION_MESH)
        self._selMeshBtn.clicked.connect(lambda: self._pickSelectionColor("mesh"))
        form.addRow("选中网格高亮:", self._selMeshBtn)

        quickRow = QHBoxLayout()
        quickRow.setSpacing(12)
        self._quickGroupBtns = []
        for i in range(len(theme.QUICK_GROUP_COLORS)):
            cell = QVBoxLayout()
            cell.setSpacing(2)
            lbl = QLabel(f"第{i + 1}组")
            lbl.setAlignment(Qt.AlignHCenter)
            lbl.setStyleSheet("color: #666666; font-size: 9px; background: transparent;")
            sw = self._makeColorSwatch(theme.QUICK_GROUP_COLORS[i])
            sw.clicked.connect(lambda _, idx=i: self._pickQuickGroupColor(idx))
            self._quickGroupBtns.append(sw)
            cell.addWidget(lbl)
            cell.addWidget(sw, 0, Qt.AlignHCenter)
            quickRow.addLayout(cell)
        quickRow.addStretch()
        form.addRow("常用工具栏颜色:", quickRow)

        layout.addWidget(box)

        box2 = QGroupBox("")
        box2.setStyleSheet("QGroupBox { border: none; padding: 0; margin: 0; }")
        form2 = QFormLayout(box2)
        form2.setSpacing(10)
        self._gridCheck = QCheckBox("显示参考网格")
        self._gridCheck.setChecked(True)
        self._gridCheck.toggled.connect(self.gridVisibleChanged.emit)
        form2.addRow(self._gridCheck)
        self._axisCheck = QCheckBox("显示坐标轴")
        self._axisCheck.setChecked(True)
        self._axisCheck.toggled.connect(self.axisVisibleChanged.emit)
        form2.addRow(self._axisCheck)
        self._selBoxCheck = QCheckBox("选中物体虚线框")
        self._selBoxCheck.setChecked(app_settings.show_selection_box)
        self._selBoxCheck.toggled.connect(self.selectionBoxChanged.emit)
        form2.addRow(self._selBoxCheck)
        layout.addWidget(box2)

        layout.addStretch()
        return page

    def _buildRenderPage(self) -> QWidget:
        """渲染性能页：显卡/内存等可调参数。默认值为合理最小（入门配置），
        高配用户可上调。所有控件直接写 app_settings（实时生效，MSAA 需重启）。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        hint = QLabel("默认值适配入门级配置（核显 / 8GB 内存），保证流畅不闪退；"
                      "配置更好的机器可上调以下数值获得更精细的曲面与更深的撤销历史。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666; font-size: 11px;")
        layout.addWidget(hint)

        # ---- 系统信息（只读） ----
        info = app_settings.system_info()
        boxInfo = QGroupBox("系统信息")
        fi = QFormLayout(boxInfo)
        fi.setSpacing(4)
        ram = info["total_ram_mb"]
        ramText = f"{ram / 1024:.1f} GB" if ram >= 1024 else (f"{ram} MB" if ram else "未知")
        lbCpu = QLabel(f"{info['cpu_cores']} 核")
        lbRam = QLabel(ramText)
        fi.addRow("CPU:", lbCpu)
        fi.addRow("物理内存:", lbRam)
        layout.addWidget(boxInfo)

        # ---- 显卡 / 渲染 ----
        boxGpu = QGroupBox("显卡 / 渲染")
        fg = QFormLayout(boxGpu)
        fg.setSpacing(6)

        self._gpuCheck = QCheckBox("启用 GPU 加速渲染 (ModernGL)")
        self._gpuCheck.setChecked(app_settings.gpu_accelerated)
        self._gpuCheck.toggled.connect(self._onGpuToggled)
        fg.addRow(self._gpuCheck)

        self._asyncCheck = QCheckBox("重计算放后台线程（区域塑形/重构）")
        self._asyncCheck.setChecked(app_settings.async_compute)
        self._asyncCheck.toggled.connect(self._onAsyncToggled)
        fg.addRow(self._asyncCheck)

        self._msaaCombo = QComboBox()
        self._msaaCombo.addItem("关闭", 0)
        self._msaaCombo.addItem("2x", 2)
        self._msaaCombo.addItem("4x", 4)
        self._msaaCombo.addItem("8x", 8)
        _msaa = int(app_settings.msaa_samples)
        _mi = self._msaaCombo.findData(_msaa)
        self._msaaCombo.setCurrentIndex(_mi if _mi >= 0 else 2)
        self._msaaCombo.currentIndexChanged.connect(self._onMsaaChanged)
        fg.addRow("抗锯齿 MSAA (重启生效):", self._msaaCombo)

        lo, hi = app_settings.MESH_MAX_DIM_GPU_RANGE
        self._meshGpuSpin = QSpinBox()
        self._meshGpuSpin.setRange(lo, hi)
        self._meshGpuSpin.setValue(int(app_settings.mesh_max_dim_gpu))
        self._meshGpuSpin.valueChanged.connect(self._onMeshGpuChanged)
        fg.addRow("3D 曲面网格密度 (GPU):", self._meshGpuSpin)

        lo, hi = app_settings.MESH_MAX_DIM_CPU_RANGE
        self._meshCpuSpin = QSpinBox()
        self._meshCpuSpin.setRange(lo, hi)
        self._meshCpuSpin.setValue(int(app_settings.mesh_max_dim_cpu))
        self._meshCpuSpin.valueChanged.connect(self._onMeshCpuChanged)
        fg.addRow("3D 曲面网格密度 (软件兜底):", self._meshCpuSpin)

        lo, hi = app_settings.PREVIEW_MAX_DIM_RANGE
        self._previewSpin = QSpinBox()
        self._previewSpin.setRange(lo, hi)
        self._previewSpin.setValue(int(app_settings.preview_max_dim))
        self._previewSpin.valueChanged.connect(self._onPreviewDimChanged)
        fg.addRow("2D 预览分辨率上限:", self._previewSpin)
        layout.addWidget(boxGpu)

        # ---- 内存 ----
        boxMem = QGroupBox("内存")
        fm = QFormLayout(boxMem)
        fm.setSpacing(6)

        lo, hi = app_settings.MAX_FIELD_PIXELS_SIDE_RANGE
        self._fieldSideSpin = QSpinBox()
        self._fieldSideSpin.setRange(lo, hi)
        self._fieldSideSpin.setSingleStep(256)
        self._fieldSideSpin.setValue(int(app_settings.max_field_pixels_side))
        self._fieldSideSpin.valueChanged.connect(self._onFieldSideChanged)
        fm.addRow("母场每边像素上限:", self._fieldSideSpin)
        self._fieldSideHint = QLabel("")
        self._fieldSideHint.setStyleSheet("color: #666; font-size: 11px;")
        fm.addRow("", self._fieldSideHint)
        self._updateFieldSideHint(int(app_settings.max_field_pixels_side))

        lo, hi = app_settings.UNDO_MEMORY_BUDGET_RANGE
        self._undoBudgetSpin = QSpinBox()
        self._undoBudgetSpin.setRange(lo, hi)
        self._undoBudgetSpin.setSingleStep(64)
        self._undoBudgetSpin.setSuffix(" MB")
        self._undoBudgetSpin.setValue(int(app_settings.undo_memory_budget_mb))
        self._undoBudgetSpin.valueChanged.connect(self._onUndoBudgetChanged)
        fm.addRow("撤销内存预算:", self._undoBudgetSpin)
        layout.addWidget(boxMem)

        layout.addStretch()
        return page

    # ---- 渲染性能页回调 ----
    def _onGpuToggled(self, on: bool) -> None:
        app_settings.gpu_accelerated = bool(on)
        self._renderDirty = True
        self.renderSettingsChanged.emit()

    def _onAsyncToggled(self, on: bool) -> None:
        app_settings.async_compute = bool(on)

    def _onMsaaChanged(self, _idx: int) -> None:
        app_settings.msaa_samples = int(self._msaaCombo.currentData())
        self._renderDirty = True

    def _onMeshGpuChanged(self, v: int) -> None:
        app_settings.mesh_max_dim_gpu = int(v)
        self._renderDirty = True
        self.renderSettingsChanged.emit()

    def _onMeshCpuChanged(self, v: int) -> None:
        app_settings.mesh_max_dim_cpu = int(v)
        self._renderDirty = True
        self.renderSettingsChanged.emit()

    def _onPreviewDimChanged(self, v: int) -> None:
        app_settings.preview_max_dim = int(v)
        self._renderDirty = True
        self.renderSettingsChanged.emit()

    def _onFieldSideChanged(self, v: int) -> None:
        app_settings.max_field_pixels_side = int(v)
        self._renderDirty = True
        self._updateFieldSideHint(int(v))
        self.renderSettingsChanged.emit()

    def _onUndoBudgetChanged(self, v: int) -> None:
        app_settings.undo_memory_budget_mb = int(v)

    def _updateFieldSideHint(self, side: int) -> None:
        """将每边像素上限换算为直观提示：单场内存与 0.1mm 精度下可支持的尺寸。"""
        mb = side * side * 4 / (1024.0 * 1024.0)
        mm = side * 0.1
        self._fieldSideHint.setText(
            f"≈ 单场 {mb:.0f} MB；0.1mm 精度下最大约 {mm:.0f}×{mm:.0f} mm")

    def _buildGeneralPage(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)


        box = QGroupBox("")
        box.setStyleSheet("QGroupBox { border: none; padding: 0; margin: 0; }")
        form = QFormLayout(box)
        form.setSpacing(6)

        self._langCombo = QComboBox()
        self._langCombo.addItems(app_settings.LANGUAGES)
        self._langCombo.setCurrentText(app_settings.language)
        self._langCombo.currentTextChanged.connect(self._onLanguageChanged)
        form.addRow("语言:", self._langCombo)

        self._unitCombo = QComboBox()
        self._unitCombo.addItems(app_settings.UNITS)
        self._unitCombo.setCurrentText(app_settings.unit)
        self._unitCombo.currentTextChanged.connect(self._onUnitChanged)
        form.addRow("单位:", self._unitCombo)

        self._tolCombo = QComboBox()
        self._tolCombo.addItems(app_settings.TOLERANCES)
        self._tolCombo.setCurrentText(app_settings.tolerance)
        self._tolCombo.currentTextChanged.connect(self._onToleranceChanged)
        form.addRow("公差:", self._tolCombo)

        self._angleCombo = QComboBox()
        self._angleCombo.addItems(app_settings.ANGLE_PRECISIONS)
        self._angleCombo.setCurrentText(app_settings.angle_precision)
        self._angleCombo.currentTextChanged.connect(self._onAnglePrecisionChanged)
        form.addRow("角度精度:", self._angleCombo)

        layout.addWidget(box)

        # ---- 标注样式：字体 / 默认文字大小 / 数值单位 ----
        boxDim = QGroupBox("标注")
        boxDim.setStyleSheet(
            "QGroupBox { border: 1px solid #ddd; border-radius: 4px;"
            " margin-top: 8px; padding-top: 10px; font-weight: bold; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px;"
            " padding: 0 4px; }")
        fd = QFormLayout(boxDim)
        fd.setSpacing(6)

        self._dimFontCombo = QFontComboBox()
        self._dimFontCombo.setFixedWidth(180)
        famIdx = self._dimFontCombo.findText(app_settings.dim_font,
                                             Qt.MatchFlag.MatchContains)
        # 回填期间抑制回调，避免因找不到原字体而意外改写设置值
        self._dimFontCombo.blockSignals(True)
        if famIdx >= 0:
            self._dimFontCombo.setCurrentIndex(famIdx)
        self._dimFontCombo.blockSignals(False)
        self._dimFontCombo.currentFontChanged.connect(self._onDimFontChanged)
        fd.addRow("字体:", self._dimFontCombo)

        self._dimTextPxSpin = QDoubleSpinBox()
        self._dimTextPxSpin.setRange(4.0, 40.0)
        self._dimTextPxSpin.setDecimals(1)
        self._dimTextPxSpin.setSingleStep(1.0)
        self._dimTextPxSpin.setValue(float(app_settings.dim_text_px))
        self._dimTextPxSpin.setSuffix(" px")
        self._dimTextPxSpin.valueChanged.connect(self._onDimTextPxChanged)
        fd.addRow("默认文字大小:", self._dimTextPxSpin)

        self._dimUnitEdit = QLineEdit(app_settings.dim_unit)
        self._dimUnitEdit.setFixedWidth(180)
        self._dimUnitEdit.setPlaceholderText("mm")
        self._dimUnitEdit.setToolTip(
            "读数数字后附加的单位（如 mm/cm/in；留空则不显示）")
        self._dimUnitEdit.textEdited.connect(self._onDimUnitChanged)
        self._dimUnitEdit.editingFinished.connect(self.dimensionSettingsChanged.emit)
        fd.addRow("数值单位:", self._dimUnitEdit)

        dimHint = QLabel("字体与单位即时作用于全部标注；文字大小仅影响新建标注，\n"
                         "已建标注可在变换面板用“增大尺寸/缩小尺寸”调整。")
        dimHint.setWordWrap(True)
        dimHint.setStyleSheet("color: #666; font-size: 11px;")
        fd.addRow("", dimHint)
        layout.addWidget(boxDim)

        layout.addStretch()
        return page

    def _onLanguageChanged(self, text: str) -> None:
        app_settings.language = text
        # 触发翻译器切换 → 广播 languageChanged（各组件 retranslateUi 实时刷新）
        from i18n import setLanguage
        setLanguage(text)
        # 主题/外观信号也广播一次，确保主窗口样式同步
        self.appearanceChanged.emit()

    def retranslateUi(self) -> None:
        """语言切换时重设对话框可见文本（关闭重开亦同）。"""
        self.setWindowTitle(tr("设置"))
        for i, name in enumerate(_NAV_NAMES):
            item = self._nav.item(i)
            if item is not None:
                item.setText(tr(name))
        self._btnApply.setText(tr("应用"))
        self._btnOk.setText(tr("确认"))
        self._btnCancel.setText(tr("取消"))

    def _onUnitChanged(self, text: str) -> None:
        app_settings.unit = text

    def _onToleranceChanged(self, text: str) -> None:
        app_settings.tolerance = text

    def _onAnglePrecisionChanged(self, text: str) -> None:
        app_settings.angle_precision = text

    def _onDimFontChanged(self, font) -> None:
        app_settings.dim_font = font.family()
        self._dimDirty = True
        self.dimensionSettingsChanged.emit()

    def _onDimTextPxChanged(self, v: float) -> None:
        app_settings.dim_text_px = float(v)
        self._dimDirty = True

    def _onDimUnitChanged(self, text: str) -> None:
        # 打字过程中只写值不广播（每字符全场景重绘代价高），编辑结束才发信号
        app_settings.dim_unit = text.strip()
        self._dimDirty = True

    def _buildKeyboardPage(self) -> QWidget:
        from PySide6.QtWidgets import QTableWidget, QTableWidgetItem, QHeaderView

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._keyConflictLabel = QLabel("")
        self._keyConflictLabel.setStyleSheet("color: #e74c3c; font-size: 12px;")
        layout.addWidget(self._keyConflictLabel)

        self._keyTable = QTableWidget(0, 3)
        self._keyTable.setHorizontalHeaderLabels(["功能", "快捷键", "说明"])
        self._keyTable.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self._keyTable.horizontalHeader().resizeSection(0, 100)
        self._keyTable.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self._keyTable.horizontalHeader().resizeSection(1, 120)
        self._keyTable.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._keyTable.verticalHeader().setVisible(False)

        self._keyCaptures = []
        for func, key, desc in app_settings.shortcuts:
            self._addShortcutRow(func, key, desc)

        layout.addWidget(self._keyTable, 1)

        btnRow = QHBoxLayout()
        btnRow.setSpacing(8)
        btnStyle = (
            "QPushButton { background: #e0e0e0; color: #333; border: 1px solid #bbb;"
            " border-radius: 3px; padding: 4px 0; }"
            "QPushButton:hover { background: #d0d0d0; }"
            "QPushButton:pressed { background: #c0c0c0; }"
        )
        addBtn = QPushButton("添加快捷键")
        addBtn.setFixedWidth(88)
        addBtn.setStyleSheet(btnStyle)
        addBtn.clicked.connect(self._onAddShortcut)
        btnRow.addWidget(addBtn)
        delBtn = QPushButton("删除选中")
        delBtn.setFixedWidth(88)
        delBtn.setStyleSheet(btnStyle)
        delBtn.clicked.connect(self._onDeleteShortcut)
        btnRow.addWidget(delBtn)
        btnRow.addStretch()
        layout.addLayout(btnRow)
        return page

    def _addShortcutRow(self, func: str = "", key: str = "", desc: str = "") -> None:
        """在快捷键表格末尾追加一行（功能名/快捷键捕获框/说明）。"""
        from PySide6.QtWidgets import QTableWidgetItem
        self._keyCaptures = getattr(self, "_keyCaptures", [])
        row = self._keyTable.rowCount()
        self._keyTable.insertRow(row)
        self._keyTable.setItem(row, 0, QTableWidgetItem(func))
        capture = KeyCaptureWidget(key)
        capture.changed.connect(self._checkKeyConflicts)
        self._keyTable.setCellWidget(row, 1, capture)
        self._keyCaptures.append(capture)
        self._keyTable.setItem(row, 2, QTableWidgetItem(desc))
        self._keyTable.setRowHeight(row, 28)

    def _onAddShortcut(self) -> None:
        self._addShortcutRow("", "", "")
        row = self._keyTable.rowCount() - 1
        item = self._keyTable.item(row, 0)
        if item is not None:
            self._keyTable.setCurrentCell(row, 0)
            self._keyTable.editItem(item)

    def _onDeleteShortcut(self) -> None:
        row = self._keyTable.currentRow()
        if row < 0:
            return
        self._keyTable.removeRow(row)
        if row < len(self._keyCaptures):
            del self._keyCaptures[row]
        self._checkKeyConflicts()

    def _saveShortcuts(self) -> None:
        """把表格内容写回 app_settings（应用/确认时调用）。"""
        rows = []
        for i in range(self._keyTable.rowCount()):
            item = self._keyTable.item(i, 0)
            func = item.text().strip() if item is not None else ""
            cap = self._keyTable.cellWidget(i, 1)
            key = cap.text().strip() if cap is not None else ""
            dscItem = self._keyTable.item(i, 2)
            desc = dscItem.text().strip() if dscItem is not None else ""
            rows.append([func, key, desc])
        app_settings.shortcuts = rows

    def _checkKeyConflicts(self) -> None:
        """检查快捷键冲突：重复的标红背景，顶部显示冲突提示。"""
        seen: dict[str, list] = {}
        for cap in self._keyCaptures:
            k = cap.text().strip()
            if not k:
                cap.setStyleSheet("")
                continue
            seen.setdefault(k, []).append(cap)
        conflicts = {k for k, v in seen.items() if len(v) > 1}
        conflictFuncs = []
        for i, cap in enumerate(self._keyCaptures):
            k = cap.text().strip()
            if k and k in conflicts:
                cap.setStyleSheet("background: #fde; border: 1px solid #e74c3c;")
                item = self._keyTable.item(i, 0)
                conflictFuncs.append(item.text() if item else f"第{i + 1}项")
            else:
                cap.setStyleSheet("")
        if conflictFuncs:
            self._keyConflictLabel.setText(
                f"⚠ 快捷键冲突: {', '.join(conflictFuncs)}")
        else:
            self._keyConflictLabel.setText("")

    def _buildFilePage(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        box = QGroupBox("")
        box.setStyleSheet("QGroupBox { border: none; padding: 0; margin: 0; }")
        form = QFormLayout(box)
        form.setSpacing(6)
        self._autosaveCheck = QCheckBox("启用自动保存")
        self._autosaveCheck.setChecked(bool(app_settings.autosave_enabled))
        self._autosaveCheck.setToolTip(
            "开启后每隔设定时间，把“有文件路径且未保存”的文档自动另存为\n"
            "同目录下 <文件名>.cai.autosave 恢复副本（不改变未保存标记）")
        self._autosaveCheck.toggled.connect(self._onAutosaveToggled)
        form.addRow(self._autosaveCheck)
        self._intervalSpin = QSpinBox()
        self._intervalSpin.setRange(1, 60)
        self._intervalSpin.setValue(int(app_settings.autosave_interval_min))
        self._intervalSpin.setSuffix(" 分钟")
        self._intervalSpin.valueChanged.connect(self._onAutosaveIntervalChanged)
        form.addRow("保存间隔:", self._intervalSpin)
        self._backupCheck = QCheckBox("保存时创建备份")
        self._backupCheck.setChecked(bool(app_settings.backup_on_save))
        self._backupCheck.setToolTip(
            "保存覆盖已有文件前，先把原文件复制为 <文件名>.cai.bak")
        self._backupCheck.toggled.connect(self._onBackupToggled)
        form.addRow(self._backupCheck)
        layout.addWidget(box)

        box2 = QGroupBox("")
        box2.setStyleSheet("QGroupBox { border: none; padding: 0; margin: 0; }")
        sl = QVBoxLayout(box2)
        sl.setSpacing(8)
        self._slotChecks = []
        self._slotNameEdits = []
        self._slotPathEdits = []
        for i, slot in enumerate(app_settings.file_save_slots):
            row = QHBoxLayout()
            row.setSpacing(6)
            chk = QCheckBox()
            chk.setChecked(slot["enabled"])
            chk.toggled.connect(lambda on, idx=i: self._onSlotEnabled(idx, on))
            self._slotChecks.append(chk)
            row.addWidget(chk)
            nameEdit = QLineEdit(slot["name"])
            nameEdit.setFixedWidth(80)
            nameEdit.setPlaceholderText("名称")
            nameEdit.textEdited.connect(lambda t, idx=i: self._onSlotNameChanged(idx, t))
            nameEdit.editingFinished.connect(self.fileSlotsChanged.emit)
            self._slotNameEdits.append(nameEdit)
            row.addWidget(nameEdit)
            edit = QLineEdit(slot["path"])
            edit.setPlaceholderText("选择保存路径")
            edit.textChanged.connect(lambda t, idx=i: self._onSlotPathChanged(idx, t))
            self._slotPathEdits.append(edit)
            row.addWidget(edit, 1)
            browse = QPushButton("浏览")
            browse.setFixedWidth(50)
            browse.clicked.connect(lambda _, idx=i: self._browseSlotPath(idx))
            row.addWidget(browse)
            sl.addLayout(row)
        layout.addWidget(box2)
        layout.addStretch()
        return page

    # ---- 文件页回调（自动保存 / 备份 / 保存槽） ----
    def _onAutosaveToggled(self, on: bool) -> None:
        app_settings.autosave_enabled = bool(on)

    def _onAutosaveIntervalChanged(self, v: int) -> None:
        app_settings.autosave_interval_min = int(v)

    def _onBackupToggled(self, on: bool) -> None:
        app_settings.backup_on_save = bool(on)

    def _onSlotEnabled(self, idx: int, on: bool) -> None:
        app_settings.file_save_slots[idx]["enabled"] = on
        self.fileSlotsChanged.emit()

    def _onSlotNameChanged(self, idx: int, text: str) -> None:
        # 打字过程中只写值不广播（每字符全量重建保存面板代价高），编辑结束才发信号
        app_settings.file_save_slots[idx]["name"] = text

    def _onSlotPathChanged(self, idx: int, text: str) -> None:
        app_settings.file_save_slots[idx]["path"] = text

    def _browseSlotPath(self, idx: int) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择保存路径")
        if d:
            self._slotPathEdits[idx].setText(d)

    def _buildOtherPage(self) -> QWidget:
        """说明页：软件基本信息（名称/版本/开发语言等，只读展示）。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        box = QGroupBox("软件信息")
        form = QFormLayout(box)
        form.setSpacing(6)
        _rows = [
            ("软件名称:", APP_NAME),
            ("版本:", APP_VERSION),
            ("开发语言:", "Python 3"),
            ("界面框架:", "PySide6（Qt 6）"),
            ("3D 渲染:", "OpenGL（ModernGL）"),
            ("几何内核:", "build123d（OCCT）"),
            ("插件接口:", f"API v{API_VERSION}"),
            ("分发方式:", "PyInstaller 便携版（免安装单目录）"),
        ]
        for label, value in _rows:
            lb = QLabel(value)
            lb.setStyleSheet("color: #333;")
            lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
            form.addRow(label, lb)

        layout.addWidget(box)

        hint = QLabel("插件可放入软件目录 plugins\\ 或 %APPDATA%\\Cangai\\plugins\\，"
                      "详见“插件”页。")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666; font-size: 11px;")
        layout.addWidget(hint)
        layout.addStretch()
        return page

    def _buildPluginsPage(self) -> QWidget:
        """插件页：列出已发现插件，勾选启用/禁用（立即生效），可重新扫描。"""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        hint = QLabel("插件目录：软件目录 plugins\\ 与 %APPDATA%\\Cangai\\plugins\\"
                      "（把含 plugin.json 的文件夹或 .py 放入即可）")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666; font-size: 11px;")
        layout.addWidget(hint)

        if self._pluginManager is None:
            tip = QLabel("（当前环境未接入插件管理器）")
            tip.setStyleSheet("color: #999;")
            layout.addWidget(tip)
            layout.addStretch()
            return page

        self._pluginRows = QWidget()
        self._pluginRowsLayout = QVBoxLayout(self._pluginRows)
        self._pluginRowsLayout.setContentsMargins(0, 0, 0, 0)
        self._pluginRowsLayout.setSpacing(4)
        layout.addWidget(self._pluginRows)

        btnRow = QHBoxLayout()
        btnRow.setSpacing(8)
        rescan = QPushButton("重新扫描并装载")
        rescan.clicked.connect(self._onRescanPlugins)
        btnRow.addWidget(rescan)
        btnRow.addStretch()
        layout.addLayout(btnRow)
        layout.addStretch()

        self._refreshPluginRows()
        return page

    def _refreshPluginRows(self) -> None:
        pm = self._pluginManager
        if pm is None:
            return
        while self._pluginRowsLayout.count():
            item = self._pluginRowsLayout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for info in pm.infos():
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(8)
            chk = QCheckBox(f"{info.name}  v{info.version}")
            chk.setChecked(info.enabled)
            chk.toggled.connect(lambda on, i=info.pluginId: self._onPluginEnabled(i, on))
            rl.addWidget(chk)
            statusColor = {"已装载": "#27ae60", "出错": "#e74c3c"}.get(info.status, "#999")
            st = QLabel(f"[{info.status}]" + (f" {info.error}" if info.error else ""))
            st.setStyleSheet(f"color: {statusColor}; font-size: 11px;")
            rl.addWidget(st, 1)
            if info.description:
                desc = QLabel(info.description)
                desc.setStyleSheet("color: #888; font-size: 11px;")
                rl.addWidget(desc, 2)
            self._pluginRowsLayout.addWidget(row)

    def _onPluginEnabled(self, pluginId: str, on: bool) -> None:
        if self._pluginManager is not None:
            self._pluginManager.setEnabled(pluginId, bool(on))
            self._refreshPluginRows()

    def _onRescanPlugins(self) -> None:
        if self._pluginManager is not None:
            self._pluginManager.discover()
            self._pluginManager.loadAll()
            self._refreshPluginRows()

    def _onFontChanged(self, val: int) -> None:
        theme.FONT_SIZE = val
        self.appearanceChanged.emit()

    def _makeColorSwatch(self, color: str) -> QPushButton:
        btn = QPushButton()
        btn.setFixedWidth(60)
        btn.setFixedHeight(24)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip("点击选择颜色")
        btn.setStyleSheet(
            f"background: {color}; border: 1px solid #999; border-radius: 2px;"
        )
        return btn

    def _openColorDialog(self, current: str) -> str | None:
        """弹出取色对话框，返回所选颜色 hex，取消返回 None。"""
        from PySide6.QtGui import QColor
        dlg = QColorDialog(QColor(current), self)
        dlg.setStyleSheet(
            "QDialog { background: white; }"
            "QPushButton { background: #e6e6e6; color: #333; border: 1px solid #ccc;"
            " padding: 4px 14px; min-height: 22px; border-radius: 2px; }"
            "QPushButton:hover { background: #d0d0d0; }"
            "QPushButton:pressed { background: #b0b0b0; }"
        )
        if dlg.exec() == QDialog.Accepted:
            c = dlg.currentColor()
            if c.isValid():
                return c.name()
        return None

    def _pickQuickGroupColor(self, index: int) -> None:
        c = self._openColorDialog(theme.QUICK_GROUP_COLORS[index])
        if c is not None:
            theme.QUICK_GROUP_COLORS[index] = c
            self._quickGroupBtns[index].setStyleSheet(
                f"background: {c}; border: 1px solid #999; border-radius: 2px;"
            )
            self.appearanceChanged.emit()


    def _pickSelectionColor(self, key: str) -> None:
        if key == "curve":
            current = theme.SELECTION_CURVE
            btn = self._selCurveBtn
        else:
            current = theme.SELECTION_MESH
            btn = self._selMeshBtn
        c = self._openColorDialog(current)
        if c is not None:
            if key == "curve":
                theme.SELECTION_CURVE = c
            else:
                theme.SELECTION_MESH = c
            btn.setStyleSheet(
                f"background: {c}; border: 1px solid #999; border-radius: 2px;"
            )
            self.appearanceChanged.emit()

    def _onColorPreset(self, btn) -> None:
        idx = self._colorGroup.id(btn)
        if idx is not None and 0 <= idx < len(_PRESET_COLORS):
            theme.PRIMARY = _PRESET_COLORS[idx]
            self.appearanceChanged.emit()

    def _pickColor(self) -> None:
        c = self._openColorDialog(theme.PRIMARY)
        if c is not None:
            theme.PRIMARY = c
            self._customColorBtn.setStyleSheet(
                f"background: {c}; border: 1px solid #999; border-radius: 2px;"
            )
            self._customColorRb.setStyleSheet(
                "QRadioButton::indicator { width: 18px; height: 18px;"
                " border-radius: 9px; border: 2px solid #999;"
                f" background: {c}; }}"
                "QRadioButton::indicator:checked { border: 2px solid #333; }"
            )
            self.appearanceChanged.emit()


_DEFAULT_SHORTCUTS = [
    ("撤销",       "Ctrl+Z", "撤销上一步"),
    ("重做",       "Ctrl+Y", "重做"),
    ("复制",       "Ctrl+C", "复制选中对象"),
    ("粘贴",       "Ctrl+V", "粘贴剪贴板对象"),
    ("正交",       "F8",     "切换正交模式"),
    ("切换显示模式", "Tab",   "2D：等高线↔灰度图；浮雕：地图↔颜色"),
]


class KeyCaptureWidget(QLineEdit):
    """快捷键捕获输入框：聚焦后按任意键组合即显示对应快捷键序列。"""

    changed = Signal()

    _MOD_NAMES = {
        Qt.ControlModifier: "Ctrl",
        Qt.ShiftModifier: "Shift",
        Qt.AltModifier: "Alt",
    }

    def __init__(self, keyText: str = "") -> None:
        super().__init__(keyText)
        self.setPlaceholderText("按键设置")
        self.setAlignment(Qt.AlignCenter)
        self.setFixedHeight(24)
        self._capturing = False
        self._justFocused = False

    def focusInEvent(self, event) -> None:
        self._capturing = True
        self._justFocused = True
        self.selectAll()
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:
        self._capturing = False
        self.deselect()
        super().focusOutEvent(event)


    def keyPressEvent(self, event) -> None:
        if not self._capturing:
            super().keyPressEvent(event)
            return
        if self._justFocused:
            self._justFocused = False
            return
        key = event.key()
        mods = event.modifiers()
        ctrl = mods & Qt.ControlModifier
        shift = mods & Qt.ShiftModifier
        if (ctrl and key == Qt.Key_A) or \
           (ctrl and key in (Qt.Key_C, Qt.Key_V, Qt.Key_X, Qt.Key_Z, Qt.Key_Y,
                             Qt.Key_Insert)) or \
           (shift and key in (Qt.Key_Insert, Qt.Key_Delete)):
            super().keyPressEvent(event)
            return
        if key in (Qt.Key_Backspace, Qt.Key_Delete):
            self.clear()
            self.changed.emit()
            return
        if key == Qt.Key_Escape:
            self.clearFocus()
            return
        # Tab / Backtab：QLineEdit 默认会用其切换焦点，这里显式拦截捕获为快捷键
        if key in (Qt.Key_Tab, Qt.Key_Backtab):
            parts = []
            for modFlag, modName in self._MOD_NAMES.items():
                if mods & modFlag:
                    parts.append(modName)
            parts.append("Tab" if key == Qt.Key_Tab else "Shift+Tab")
            self.setText("+".join(parts))
            self.changed.emit()
            event.accept()
            return
        if key in (Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt,
                   Qt.Key_Meta, Qt.Key_CapsLock):
            return
        parts = []
        for modFlag, modName in self._MOD_NAMES.items():
            if mods & modFlag:
                parts.append(modName)
        keyName = QKeySequence(key).toString()
        if keyName:
            parts.append(keyName)
        self.setText("+".join(parts))
        self.changed.emit()

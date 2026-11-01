"""设置对话框：外观（主题色）等偏好设置。"""
from __future__ import annotations
import os
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                                QPushButton, QGroupBox, QFormLayout, QGridLayout,
                                QSpinBox, QDoubleSpinBox, QColorDialog,
                                QCheckBox, QWidget, QListWidget, QStackedWidget,
                                QButtonGroup, QRadioButton, QComboBox,
                                QLineEdit, QFileDialog, QFontComboBox,
                                QScrollArea, QFrame, QMessageBox)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence
import ui.styles.theme as theme
import core.app_settings as app_settings
from core.api import API_VERSION
from version import APP_NAME, APP_VERSION, APP_VERSION_DATE
from i18n import tr, languageChanged


# 预设主题色：青 / 橙 / 绿 / 蓝
_PRESET_COLORS = ["#15b6c9", "#ff6400", "#27ae60", "#1e88e5"]

# 说明页「开源组件与致谢」：(组件名, 许可证, 在本软件中的用途)
# 只列实际在运行的第三方项目；改动依赖时同步更新此表（与 requirements.txt 对应）
_OPEN_SOURCE = [
    ("Python 3", "PSF License", "运行时"),
    ("Qt 6 / PySide6", "LGPL-3.0", "界面框架与控件"),
    ("Qt-Advanced-Docking-System", "LGPL-2.1", "可停靠面板布局"),
    ("NumPy", "BSD-3-Clause", "数值计算与高度场运算"),
    ("ModernGL", "MIT", "OpenGL 渲染封装"),
    ("glcontext", "MIT", "OpenGL 上下文创建"),
    ("OpenCV", "Apache-2.0", "图像处理、贴图与环境贴图"),
    ("SciPy", "BSD-3-Clause", "样条 / NURBS 求值（可选）"),
    ("fontTools", "MIT", "字体文件解析与字形轮廓提取"),
    ("ezdxf", "MIT", "DXF 曲线导入 / 导出"),
    ("VTracer", "MIT", "图片转线稿的矢量化引擎"),
    ("Shapely", "BSD-3-Clause", "2D 几何布尔 / 包容运算"),
    ("pyclipper（Clipper）", "MIT", "轮廓等距偏移"),
    ("pydantic", "MIT", "数据模型与参数校验"),
    ("build123d", "Apache-2.0", "参数化实体建模"),
    ("OpenCascade（OCCT）", "LGPL-2.1（含例外）", "几何内核"),
    ("pymillcam", "GPL-3.0", "CNC 刀路引擎与后处理"),
    ("PyInstaller", "GPL-2.0（引导器例外）", "打包为免安装便携版"),
]

# 导航页名（key 顺序对应 _stack 各页）；文件页已并入“通用”页
_NAV_NAMES = ["外观样式", "渲染性能", "通用", "键盘", "AI", "插件", "说明"]

# AI 页预置大模型服务商（默认 DeepSeek；“自定义”= 手填任意 OpenAI 兼容服务）
_AI_PRESETS = {
    "DeepSeek": {"base_url": "https://api.deepseek.com",
                 "models": ["deepseek-flash", "deepseek-v4-pro",
                            "deepseek-v4-flash"],
                 "hint": "DeepSeek 开放平台 platform.deepseek.com 创建 API Key；"
                         "deepseek-flash = 最新 V4.1 Flash（多模态/1M 上下文），"
                         "旧名 deepseek-chat/reasoner 已于 2026-07 弃用"},
    "豆包": {"base_url": "https://ark.cn-beijing.volces.com/api/v3",
             "models": ["doubao-seed-1-6-250615"],
             "hint": "火山方舟 volcengine.com 创建 API Key；模型名填推理接入点 ID"},
    "通义千问": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                 "models": ["qwen-plus", "qwen-vl-max"],
                 "hint": "阿里云百炼控制台创建 API Key"},
    "智谱GLM": {"base_url": "https://open.bigmodel.cn/api/paas/v4",
                "models": ["glm-4-plus"],
                "hint": "开放平台 bigmodel.cn 创建 API Key"},
    "Kimi": {"base_url": "https://api.moonshot.cn/v1",
             "models": ["kimi-k2-0905-preview"],
             "hint": "platform.moonshot.cn 创建 API Key"},
    "OpenAI": {"base_url": "https://api.openai.com/v1",
               "models": ["gpt-4o", "gpt-4.1"],
               "hint": "platform.openai.com 创建 API Key"},
    "自定义": {"base_url": "", "models": [], "hint": "任意 OpenAI 兼容服务："
              "填服务地址（以 /v1 结尾）与模型名即可"},
}

# MCP 桥默认监听端口（与 core/ai/bridge_server.DEFAULT_PORT 一致）
_BRIDGE_DEFAULT_PORT = 31789


class SettingsDialog(QDialog):
    appearanceChanged = Signal()
    gridVisibleChanged = Signal(bool)
    axisVisibleChanged = Signal(bool)
    selectionBoxChanged = Signal(bool)
    fileSlotsChanged = Signal()
    renderSettingsChanged = Signal()
    dimensionSettingsChanged = Signal()   # 标注样式（字体/字号/单位）变更
    aiSettingsChanged = Signal()          # AI 页（模型配置/MCP 桥）变更

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
        self._stack.addWidget(self._buildKeyboardPage())
        self._stack.addWidget(self._buildAiPage())
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
        # 文件关联：“设为 .cai 默认打开程序”放在最下面、“应用”按钮左边（全页可见）
        self._assocBtn = QPushButton("设为 .cai 默认打开程序")
        self._assocBtn.setFixedWidth(170)
        self._assocBtn.setStyleSheet(_btnStyle)
        self._assocBtn.setToolTip(
            "写入当前用户注册表，使双击 .cai 工程文件即用本程序打开（无需管理员权限）。")
        self._assocBtn.clicked.connect(self._onRegisterAssoc)
        btnBar.addWidget(self._assocBtn)
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
        from core.ai import llm_client
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
            "ai": llm_client.loadConfig(),
        }

    def _restoreSnapshot(self) -> None:
        s = self._snapshot
        from core.ai import llm_client
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
        if s.get("ai"):
            llm_client.saveConfig(s["ai"])

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
        aiChanged = self._aiDirty()
        self._saveAiSettings()
        if aiChanged:
            self._restartAiBridge()
            self.aiSettingsChanged.emit()
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
        self._invertWheelCheck = QCheckBox("反向滚轮缩放")
        self._invertWheelCheck.setChecked(app_settings.invert_wheel_zoom)
        self._invertWheelCheck.toggled.connect(self._onInvertWheelToggled)
        form2.addRow(self._invertWheelCheck)
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
        self._meshGpuSpin.setToolTip(
            "浮雕曲面显示网格每边最大格数。越大越细腻（1024≈千像素级图片接近 1:1，"
            "2048≈1:1 全像素）；越大越吃显存，2048 建议 8GB 以上显卡；"
            "只影响 3D 显示，不影响模型数据与导出 STL。")
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
        layout.setSpacing(8)

        _gstyle = ("QGroupBox { border: 1px solid #ddd; border-radius: 4px;"
                   " margin-top: 8px; padding-top: 10px; font-weight: bold; }"
                   "QGroupBox::title { subcontrol-origin: margin; left: 8px;"
                   " padding: 0 4px; }")

        # ---- 基本：语言 / 单位 / 公差 / 角度精度（两列一行，输入框自适应宽）----
        box = QGroupBox("基本")
        box.setStyleSheet(_gstyle)
        grid = QGridLayout(box)
        grid.setContentsMargins(12, 10, 12, 10)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        self._langCombo = QComboBox()
        self._langCombo.addItems(app_settings.LANGUAGES)
        self._langCombo.setCurrentText(app_settings.language)
        self._langCombo.currentTextChanged.connect(self._onLanguageChanged)
        self._unitCombo = QComboBox()
        self._unitCombo.addItems(app_settings.UNITS)
        self._unitCombo.setCurrentText(app_settings.unit)
        self._unitCombo.currentTextChanged.connect(self._onUnitChanged)
        self._tolCombo = QComboBox()
        self._tolCombo.addItems(app_settings.TOLERANCES)
        self._tolCombo.setCurrentText(app_settings.tolerance)
        self._tolCombo.currentTextChanged.connect(self._onToleranceChanged)
        self._angleCombo = QComboBox()
        self._angleCombo.addItems(app_settings.ANGLE_PRECISIONS)
        self._angleCombo.setCurrentText(app_settings.angle_precision)
        self._angleCombo.currentTextChanged.connect(self._onAnglePrecisionChanged)
        grid.addWidget(QLabel("语言:"), 0, 0)
        grid.addWidget(self._langCombo, 0, 1)
        grid.addWidget(QLabel("单位:"), 0, 2)
        grid.addWidget(self._unitCombo, 0, 3)
        grid.addWidget(QLabel("公差:"), 1, 0)
        grid.addWidget(self._tolCombo, 1, 1)
        grid.addWidget(QLabel("角度精度:"), 1, 2)
        grid.addWidget(self._angleCombo, 1, 3)
        layout.addWidget(box)

        # ---- 标注样式：字体 / 默认文字大小 / 数值单位 ----
        boxDim = QGroupBox("标注")
        boxDim.setStyleSheet(_gstyle)
        dgrid = QGridLayout(boxDim)
        dgrid.setContentsMargins(12, 10, 12, 10)
        dgrid.setHorizontalSpacing(8)
        dgrid.setVerticalSpacing(6)
        dgrid.setColumnStretch(1, 1)
        dgrid.setColumnStretch(3, 1)

        self._dimFontCombo = QFontComboBox()
        famIdx = self._dimFontCombo.findText(app_settings.dim_font,
                                             Qt.MatchFlag.MatchContains)
        # 回填期间抑制回调，避免因找不到原字体而意外改写设置值
        self._dimFontCombo.blockSignals(True)
        if famIdx >= 0:
            self._dimFontCombo.setCurrentIndex(famIdx)
        self._dimFontCombo.blockSignals(False)
        self._dimFontCombo.currentFontChanged.connect(self._onDimFontChanged)

        self._dimTextPxSpin = QDoubleSpinBox()
        self._dimTextPxSpin.setRange(4.0, 40.0)
        self._dimTextPxSpin.setDecimals(1)
        self._dimTextPxSpin.setSingleStep(1.0)
        self._dimTextPxSpin.setValue(float(app_settings.dim_text_px))
        self._dimTextPxSpin.setSuffix(" px")
        self._dimTextPxSpin.valueChanged.connect(self._onDimTextPxChanged)

        self._dimUnitEdit = QLineEdit(app_settings.dim_unit)
        self._dimUnitEdit.setPlaceholderText("mm")
        self._dimUnitEdit.setToolTip(
            "读数数字后附加的单位（如 mm/cm/in；留空则不显示）")
        self._dimUnitEdit.textEdited.connect(self._onDimUnitChanged)
        self._dimUnitEdit.editingFinished.connect(self.dimensionSettingsChanged.emit)

        dgrid.addWidget(QLabel("字体:"), 0, 0)
        dgrid.addWidget(self._dimFontCombo, 0, 1)
        dgrid.addWidget(QLabel("默认文字大小:"), 0, 2)
        dgrid.addWidget(self._dimTextPxSpin, 0, 3)
        dgrid.addWidget(QLabel("数值单位:"), 1, 0)
        dgrid.addWidget(self._dimUnitEdit, 1, 1)
        dimHint = QLabel("字体与单位即时作用于全部标注；文字大小仅影响新建标注，"
                         "已建标注可在变换面板用“增大尺寸/缩小尺寸”调整。")
        dimHint.setWordWrap(True)
        dimHint.setStyleSheet("color: #666; font-size: 11px;")
        dgrid.addWidget(dimHint, 2, 0, 1, 4)
        layout.addWidget(boxDim)

        # ---- 文件：自动保存 / 备份 / 保存槽 / 关联（原“文件”页并入本页）----
        boxFile = QGroupBox("文件")
        boxFile.setStyleSheet(_gstyle)
        fl = QVBoxLayout(boxFile)
        fl.setContentsMargins(12, 10, 12, 10)
        fl.setSpacing(6)

        saveRow = QHBoxLayout()
        saveRow.setSpacing(8)
        self._autosaveCheck = QCheckBox(tr("启用自动保存"))
        self._autosaveCheck.setChecked(bool(app_settings.autosave_enabled))
        self._autosaveCheck.setToolTip(tr(
            "开启后每隔设定时间，在软件目录 caibak 文件夹新建一份独立副本（不覆盖原文件、不改变未保存标记）\n"
            "文件名为 caibak/<主名>.autosave.<时间戳>.cai，每个文档只保留最近 20 份供回溯恢复"))
        self._autosaveCheck.toggled.connect(self._onAutosaveToggled)
        saveRow.addWidget(self._autosaveCheck)
        self._backupCheck = QCheckBox("保存时创建备份")
        self._backupCheck.setChecked(bool(app_settings.backup_on_save))
        self._backupCheck.setToolTip(
            "保存覆盖已有文件前，先把原文件复制为 <文件名>.cai.bak")
        self._backupCheck.toggled.connect(self._onBackupToggled)
        saveRow.addWidget(self._backupCheck)
        saveRow.addStretch()
        fl.addLayout(saveRow)

        intervalRow = QHBoxLayout()
        intervalRow.setSpacing(8)
        intervalRow.addWidget(QLabel("自动保存间隔:"))
        self._intervalSpin = QSpinBox()
        self._intervalSpin.setRange(1, 60)
        self._intervalSpin.setValue(int(app_settings.autosave_interval_min))
        self._intervalSpin.setSuffix(" 分钟")
        self._intervalSpin.valueChanged.connect(self._onAutosaveIntervalChanged)
        intervalRow.addWidget(self._intervalSpin)
        intervalRow.addStretch()
        fl.addLayout(intervalRow)

        stlRow = QHBoxLayout()
        stlRow.setSpacing(8)
        stlRow.addWidget(QLabel("STL 顶面简化容差:"))
        self._stlTolCombo = QComboBox()
        for _v, _txt in app_settings.STL_MERGE_TOL_CHOICES:
            self._stlTolCombo.addItem(_txt, _v)
        _ti = self._stlTolCombo.findData(float(app_settings.stl_merge_tol))
        self._stlTolCombo.setCurrentIndex(_ti if _ti >= 0 else 0)
        self._stlTolCombo.setToolTip(
            "闭合实体 STL 的顶面自适应简化（只影响导出，不改模型数据）。\n"
            "把高度场切成矩形块，块内曲面与拟合面的偏差小于此值就整块合并。\n"
            "实测 1024x1024 满幅浮雕：关闭 105MB，0.005mm 62MB，0.02mm 27MB；\n"
            "几何偏差约为该值的 2 倍（0.005mm → ≤0.01mm，远小于常见 0.1mm 层高）。\n"
            "选“关闭”则只合并严格共面的区域（零损失，仍约为旧实现的 1/2）。")
        self._stlTolCombo.currentIndexChanged.connect(self._onStlTolChanged)
        stlRow.addWidget(self._stlTolCombo)
        stlRow.addStretch()
        fl.addLayout(stlRow)

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
            fl.addLayout(row)

        assocNote = QLabel(
            "提示：底部“设为 .cai 默认打开程序”可注册文件关联，便携版双击工程文件"
            "即用本程序打开并显示 Cangai 图标（写入当前用户注册表，无需管理员权限）。")
        assocNote.setWordWrap(True)
        assocNote.setStyleSheet("color: #888; font-size: 11px;")
        fl.addWidget(assocNote)
        layout.addWidget(boxFile)

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
        if hasattr(self, "_invertWheelCheck"):
            self._invertWheelCheck.setText(tr("反向滚轮缩放"))

    def _onInvertWheelToggled(self, on: bool) -> None:
        """反向滚轮缩放：写入设置，2D/3D 视图滚轮事件即时按新方向生效。"""
        app_settings.invert_wheel_zoom = bool(on)

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

        self._keyTable = QTableWidget(0, 4)
        self._keyTable.setHorizontalHeaderLabels(["分组", "功能", "快捷键", "说明"])
        self._keyTable.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self._keyTable.horizontalHeader().resizeSection(0, 70)
        self._keyTable.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self._keyTable.horizontalHeader().resizeSection(1, 105)
        self._keyTable.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self._keyTable.horizontalHeader().resizeSection(2, 115)
        self._keyTable.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._keyTable.verticalHeader().setVisible(False)

        self._keyCaptures = []
        for row in app_settings.shortcuts:
            # 兼容旧格式 [功能, 快捷键, 说明]
            if len(row) >= 4:
                self._addShortcutRow(row[0], row[1], row[2], row[3])
            else:
                self._addShortcutRow("通用", row[0], row[1], row[2])

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

    def _addShortcutRow(self, group: str = "通用", func: str = "",
                        key: str = "", desc: str = "") -> None:
        """在快捷键表格末尾追加一行（分组/功能名/快捷键捕获框/说明）。"""
        from PySide6.QtWidgets import QTableWidgetItem
        from PySide6.QtGui import QBrush, QColor
        self._keyCaptures = getattr(self, "_keyCaptures", [])
        row = self._keyTable.rowCount()
        self._keyTable.insertRow(row)
        gItem = QTableWidgetItem(group)
        gItem.setTextAlignment(Qt.AlignCenter)
        gItem.setForeground(QBrush(QColor("#15b6c9")))
        self._keyTable.setItem(row, 0, gItem)
        self._keyTable.setItem(row, 1, QTableWidgetItem(func))
        capture = KeyCaptureWidget(key)
        capture.changed.connect(self._checkKeyConflicts)
        self._keyTable.setCellWidget(row, 2, capture)
        self._keyCaptures.append(capture)
        self._keyTable.setItem(row, 3, QTableWidgetItem(desc))
        self._keyTable.setRowHeight(row, 28)

    def _onAddShortcut(self) -> None:
        row = self._keyTable.currentRow()
        group = "通用"
        if row >= 0:
            item = self._keyTable.item(row, 0)
            if item is not None and item.text().strip():
                group = item.text().strip()
        self._addShortcutRow(group, "", "", "")
        row = self._keyTable.rowCount() - 1
        item = self._keyTable.item(row, 1)
        if item is not None:
            self._keyTable.setCurrentCell(row, 1)
            self._keyTable.editItem(item)

    def _onDeleteShortcut(self) -> None:
        row = self._keyTable.currentRow()
        if row < 0:
            return
        self._keyTable.removeRow(row)
        if len(self._keyCaptures) > row:
            del self._keyCaptures[row]
        self._checkKeyConflicts()

    def _saveShortcuts(self) -> None:
        """把表格内容写回 app_settings（应用/确认时调用）。"""
        rows = []
        for i in range(self._keyTable.rowCount()):
            gItem = self._keyTable.item(i, 0)
            group = gItem.text().strip() if gItem is not None else "通用"
            item = self._keyTable.item(i, 1)
            func = item.text().strip() if item is not None else ""
            cap = self._keyTable.cellWidget(i, 2)
            key = cap.text().strip() if cap is not None else ""
            dscItem = self._keyTable.item(i, 3)
            desc = dscItem.text().strip() if dscItem is not None else ""
            rows.append([group or "通用", func, key, desc])
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
                item = self._keyTable.item(i, 1)
                conflictFuncs.append(item.text() if item else f"第{i + 1}项")
            else:
                cap.setStyleSheet("")
        if conflictFuncs:
            self._keyConflictLabel.setText(
                f"⚠ 快捷键冲突: {', '.join(conflictFuncs)}")
        else:
            self._keyConflictLabel.setText("")

    def _onRegisterAssoc(self) -> None:
        try:
            from core.file_association import (register, user_choice_progid,
                                               APP_PROGID)
            register()
        except Exception as e:
            QMessageBox.warning(self, "文件关联",
                                f"写入注册表失败：{e}\n\n"
                                "若系统策略禁止写入，请改用安装版。")
            return
        extra = ""
        old = user_choice_progid()
        if old and old != APP_PROGID:
            extra = (f"\n\n系统原先记录的默认程序是 {old}。\n"
                     "请在 Windows 设置 → 应用 → 默认应用 → 按文件类型指定"
                     "默认应用 中，把 .cai 改为 Cangai 即可覆盖它。")
        QMessageBox.information(
            self, "文件关联",
            "已注册完成。\n\n"
            "资源管理器里的 .cai 文件应显示为 Cangai 工程文件图标，"
            "双击即用本程序打开。\n"
            "若图标仍是旧的，注销/重启资源管理器或重启电脑后刷新。"
            + extra)

    # ---- 文件页回调（自动保存 / 备份 / 保存槽） ----
    def _onAutosaveToggled(self, on: bool) -> None:
        app_settings.autosave_enabled = bool(on)

    def _onAutosaveIntervalChanged(self, v: int) -> None:
        app_settings.autosave_interval_min = int(v)

    def _onBackupToggled(self, on: bool) -> None:
        app_settings.backup_on_save = bool(on)

    def _onStlTolChanged(self, _idx: int) -> None:
        app_settings.stl_merge_tol = float(self._stlTolCombo.currentData())

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

    # ---- AI 页：大模型配置 + MCP 桥 ----

    def _buildAiPage(self) -> QWidget:
        """AI 页：选服务商（默认 DeepSeek，可选自定义 OpenAI 兼容服务）、
        API Key、模型、温度；MCP 桥开关。写入 llm_client 的 QSettings 键。"""
        from core.ai import llm_client
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)
        _gstyle = ("QGroupBox { border: 1px solid #ddd; border-radius: 4px;"
                   " margin-top: 8px; padding-top: 10px; font-weight: bold; }"
                   "QGroupBox::title { subcontrol-origin: margin; left: 8px;"
                   " padding: 0 4px; }")

        box = QGroupBox("大模型对话（视口右侧 Ai 按钮面板）")
        box.setStyleSheet(_gstyle)
        grid = QGridLayout(box)
        grid.setContentsMargins(12, 10, 12, 10)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)

        cfg = llm_client.loadConfig()
        from PySide6.QtCore import QSettings
        st = QSettings(llm_client.SETTINGS_ORG, llm_client.SETTINGS_APP)
        preset = str(st.value("AI/preset", "DeepSeek") or "DeepSeek")
        if preset not in _AI_PRESETS:
            preset = "自定义"

        self._aiPreset = QComboBox()
        self._aiPreset.addItems(list(_AI_PRESETS.keys()))
        self._aiPreset.setCurrentText(preset)
        self._aiPreset.currentTextChanged.connect(self._onAiPresetChanged)
        grid.addWidget(QLabel("服务商:"), 0, 0)
        grid.addWidget(self._aiPreset, 0, 1)

        self._aiBase = QLineEdit(cfg["base_url"])
        self._aiBase.setPlaceholderText("https://…/v1")
        grid.addWidget(QLabel("服务地址:"), 1, 0)
        grid.addWidget(self._aiBase, 1, 1)

        self._aiModel = QComboBox()
        self._aiModel.setEditable(True)
        grid.addWidget(QLabel("模型:"), 2, 0)
        grid.addWidget(self._aiModel, 2, 1)

        self._aiKey = QLineEdit(cfg["api_key"])
        self._aiKey.setEchoMode(QLineEdit.Password)
        self._aiKey.setPlaceholderText("sk-…（本页面仅保存在本机配置，不上传）")
        grid.addWidget(QLabel("API Key:"), 3, 0)
        grid.addWidget(self._aiKey, 3, 1)

        self._aiTemp = QDoubleSpinBox()
        self._aiTemp.setRange(0.0, 2.0)
        self._aiTemp.setSingleStep(0.1)
        self._aiTemp.setValue(float(cfg["temperature"]))
        grid.addWidget(QLabel("温度:"), 4, 0)
        grid.addWidget(self._aiTemp, 4, 1)

        self._aiHint = QLabel()
        self._aiHint.setWordWrap(True)
        self._aiHint.setStyleSheet("color: #888; font-size: 11px;")
        grid.addWidget(self._aiHint, 5, 0, 1, 2)
        layout.addWidget(box)

        # 测试连接（用当前表单值，不需先保存）
        testRow = QHBoxLayout()
        self._aiTestBtn = QPushButton("测试连接")
        self._aiTestBtn.clicked.connect(self._onAiTest)
        testRow.addWidget(self._aiTestBtn)
        testRow.addStretch()
        layout.addLayout(testRow)

        boxMcp = QGroupBox("MCP 桥（外部 AI 客户端驱动本软件）")
        boxMcp.setStyleSheet(_gstyle)
        mgrid = QGridLayout(boxMcp)
        mgrid.setContentsMargins(12, 10, 12, 10)
        mgrid.setHorizontalSpacing(8)
        mgrid.setVerticalSpacing(6)
        self._aiBridgeChk = QCheckBox("启用 MCP 桥")
        self._aiBridgeChk.setChecked(
            str(st.value("AI/bridge_enabled", True)).lower()
            not in ("false", "0", "no"))
        mgrid.addWidget(self._aiBridgeChk, 0, 0, 1, 3)
        mgrid.addWidget(QLabel("监听端口:"), 1, 0)
        self._aiBridgePort = QSpinBox()
        self._aiBridgePort.setRange(1024, 65535)
        try:
            self._aiBridgePort.setValue(int(st.value(
                "AI/bridge_port", _BRIDGE_DEFAULT_PORT)))
        except (TypeError, ValueError):
            self._aiBridgePort.setValue(_BRIDGE_DEFAULT_PORT)
        mgrid.addWidget(self._aiBridgePort, 1, 1)
        mgrid.setColumnStretch(2, 1)
        mcpHint = QLabel("软件运行期间在本机 127.0.0.1 监听；AI 客户端配置："
                         "python mcp/cangai_mcp_server.py（工具表与对话面板共用）")
        mcpHint.setStyleSheet("color: #888; font-size: 11px;")
        mgrid.addWidget(mcpHint, 2, 0, 1, 3)
        layout.addWidget(boxMcp)

        layout.addStretch()
        # 初始回填：按当前服务商填地址与模型表（不覆盖用户已存的自定义地址）
        self._applyAiPreset(preset, keepBaseIfCustom=(
            preset != "自定义" and cfg["base_url"] and
            cfg["base_url"] != _AI_PRESETS.get("DeepSeek",
                                              {}).get("base_url")
            and cfg["base_url"] not in
            (p["base_url"] for p in _AI_PRESETS.values() if p["base_url"])))
        if cfg["model"] and self._aiModel.findText(cfg["model"]) < 0:
            self._aiModel.setCurrentText(cfg["model"])
        return page

    def _applyAiPreset(self, name: str, keepBaseIfCustom: bool = False) -> None:
        """切服务商：更新地址/模型下拉/提示；自定义时地址与模型均可手输。"""
        p = _AI_PRESETS.get(name) or _AI_PRESETS["自定义"]
        self._aiModel.blockSignals(True)
        self._aiModel.clear()
        self._aiModel.addItems(p["models"])
        self._aiModel.setEditable(True)   # 下拉给建议，也允许手输任意模型名
        self._aiModel.blockSignals(False)
        if not keepBaseIfCustom and p["base_url"]:
            self._aiBase.setText(p["base_url"])
        self._aiHint.setText("提示：" + p["hint"])

    def _onAiPresetChanged(self, name: str) -> None:
        self._applyAiPreset(name)

    def showAiTab(self) -> None:
        """外部直达 AI 配置页（对话面板 ⚙ 按钮）。"""
        self._nav.setCurrentRow(_NAV_NAMES.index("AI"))

    def _aiDirty(self) -> bool:
        """AI 页/桥配置与快照是否有差异（页未构建时保守视为未改）。"""
        if not hasattr(self, "_aiPreset"):
            return False
        from core.ai import llm_client
        from PySide6.QtCore import QSettings
        if self._collectAiConfig() != self._snapshot.get("ai"):
            return True
        st = QSettings(llm_client.SETTINGS_ORG, llm_client.SETTINGS_APP)
        if str(st.value("AI/preset", "DeepSeek")) != self._aiPreset.currentText():
            return True
        if self._aiBridgeChk.isChecked() != str(st.value(
                "AI/bridge_enabled", True)).lower() not in ("false", "0", "no"):
            return True
        try:
            cur = int(st.value("AI/bridge_port", 31789))
        except (TypeError, ValueError):
            cur = 31789
        return cur != self._aiBridgePort.value()

    def _restartAiBridge(self) -> None:
        """按新开关/端口重启应用内 MCP 桥。"""
        try:
            from core.ai.bridge_server import stopBridge, startBridge
            stopBridge()
            if self._aiBridgeChk.isChecked():
                startBridge(self._aiBridgePort.value())
        except Exception:
            pass

    def _collectAiConfig(self) -> dict:
        return {
            "base_url": self._aiBase.text().strip(),
            "api_key": self._aiKey.text().strip(),
            "model": self._aiModel.currentText().strip(),
            "temperature": self._aiTemp.value(),
        }

    def _saveAiSettings(self) -> None:
        from core.ai import llm_client
        from PySide6.QtCore import QSettings
        cfg = self._collectAiConfig()
        if cfg["base_url"]:
            llm_client.saveConfig(cfg)
        st = QSettings(llm_client.SETTINGS_ORG, llm_client.SETTINGS_APP)
        st.setValue("AI/preset", self._aiPreset.currentText())
        st.setValue("AI/bridge_enabled", self._aiBridgeChk.isChecked())
        st.setValue("AI/bridge_port", self._aiBridgePort.value())

    def _onAiTest(self) -> None:
        from core.ai import llm_client
        cfg = self._collectAiConfig()
        if not cfg["api_key"]:
            QMessageBox.warning(self, "AI", "请先填写 API Key")
            return
        if not cfg["base_url"] or not cfg["model"]:
            QMessageBox.warning(self, "AI", "请填写服务地址与模型名")
            return
        self._aiTestBtn.setEnabled(False)
        self._aiTestBtn.setText("测试中…")
        ok = False
        err = ""
        try:
            msg = llm_client.chatOnce(cfg, [{"role": "user",
                                             "content": "ping"}],
                                      None, timeout=20.0)
            ok = bool(msg.get("content") or msg.get("tool_calls"))
        except llm_client.LLMError as e:
            err = str(e)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        finally:
            self._aiTestBtn.setEnabled(True)
            self._aiTestBtn.setText("测试连接")
        if ok:
            QMessageBox.information(self, "AI", "连接成功：" + cfg["model"] +
                                    "\n点“应用/确认”保存后生效。")
        else:
            QMessageBox.critical(self, "AI", "连接失败：\n" + err)

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
            ("更新日期:", APP_VERSION_DATE),
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

        # ---- 开源组件与致谢 ----
        boxOss = QGroupBox("开源组件与致谢")
        boxOss.setStyleSheet(
            "QGroupBox { border: 1px solid #ddd; border-radius: 4px;"
            " margin-top: 8px; padding-top: 10px; font-weight: bold; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px;"
            " padding: 0 4px; }")
        ov = QVBoxLayout(boxOss)
        ov.setSpacing(6)
        lbThanks = QLabel(
            f"{APP_NAME} 的实现离不开下面这些开源项目。谨向它们的作者与社区"
            "致以诚挚的感谢——正是这些慷慨分享的工作，让本软件得以站在"
            "巨人的肩膀上完成。")
        lbThanks.setWordWrap(True)
        lbThanks.setStyleSheet("color: #333; font-weight: normal;")
        ov.addWidget(lbThanks)

        ossInner = QWidget()
        ossGrid = QGridLayout(ossInner)
        ossGrid.setContentsMargins(0, 0, 0, 0)
        ossGrid.setHorizontalSpacing(12)
        ossGrid.setVerticalSpacing(3)
        for row, (name, lic, use) in enumerate(_OPEN_SOURCE):
            lbName = QLabel(name)
            lbName.setStyleSheet("color: #333; font-weight: normal;")
            lbName.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lbLic = QLabel(lic)
            lbLic.setStyleSheet("color: #888; font-size: 11px;"
                                " font-weight: normal;")
            lbLic.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lbUse = QLabel(use)
            lbUse.setStyleSheet("color: #555; font-weight: normal;")
            lbUse.setWordWrap(True)
            ossGrid.addWidget(lbName, row, 0)
            ossGrid.addWidget(lbLic, row, 1)
            ossGrid.addWidget(lbUse, row, 2)
        ossGrid.setColumnStretch(2, 1)
        ov.addWidget(ossInner)

        lbLicNote = QLabel("各组件的完整许可证与版权声明可在其官方仓库或"
                           "安装包附带的 LICENSE 文件中查阅；本软件不修改、"
                           "不重新授权这些第三方组件。")
        lbLicNote.setWordWrap(True)
        lbLicNote.setStyleSheet("color: #666; font-size: 11px;"
                                " font-weight: normal;")
        ov.addWidget(lbLicNote)
        layout.addWidget(boxOss)

        # ---- 开源免费声明 + 打赏收款码 ----
        boxDonate = QGroupBox("开源与支持")
        boxDonate.setStyleSheet(
            "QGroupBox { border: 1px solid #ddd; border-radius: 4px;"
            " margin-top: 8px; padding-top: 10px; font-weight: bold; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px;"
            " padding: 0 4px; }")
        dv = QVBoxLayout(boxDonate)
        dv.setSpacing(8)
        lbOpen = QLabel(f"{APP_NAME} 是一款开源免费软件，你可以自由地使用、"
                        "复制和分发，无需支付任何费用，所有功能不受任何限制。")
        lbOpen.setWordWrap(True)
        lbOpen.setStyleSheet("color: #333; font-weight: normal;")
        dv.addWidget(lbOpen)
        lbDonateTip = QLabel("软件的开发与维护纯属用爱发电。如果它对你的工作有帮助，"
                             "欢迎扫描二维码请作者喝杯咖啡～ （完全自愿，不影响任何功能）")
        lbDonateTip.setWordWrap(True)
        lbDonateTip.setStyleSheet("color: #666; font-size: 11px; font-weight: normal;")
        dv.addWidget(lbDonateTip)

        from PySide6.QtGui import QPixmap
        from resource_path import resource_path
        qrPath = resource_path("resources/donate_qr.png")
        qrLbl = QLabel()
        qrLbl.setAlignment(Qt.AlignHCenter)
        if os.path.exists(qrPath):
            pm = QPixmap(qrPath)
            if not pm.isNull():
                qrLbl.setPixmap(pm.scaled(
                    260, 260, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                qrLbl.setToolTip("扫码打赏支持作者（自愿）")
            else:
                qrLbl.setText("（打赏二维码加载失败）")
                qrLbl.setStyleSheet("color: #999; font-weight: normal;")
        else:
            qrLbl.setText("（未找到打赏二维码图片）")
            qrLbl.setStyleSheet("color: #999; font-weight: normal;")
        dv.addWidget(qrLbl, 0, Qt.AlignHCenter)
        layout.addWidget(boxDonate)

        layout.addStretch()

        # 说明页内容较长（软件信息 + 开源组件 + 致谢）：整页套滚动区，窗口不裁切
        wrap = QScrollArea()
        wrap.setWidgetResizable(True)
        wrap.setFrameShape(QFrame.NoFrame)
        wrap.setStyleSheet("QScrollArea { background: transparent; }")
        wrap.setWidget(page)
        return wrap

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

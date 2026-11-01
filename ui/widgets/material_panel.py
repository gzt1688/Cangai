"""材质面板：浮雕渲染材质的独立面板（Blender「原理化 BSDF」风格参数编辑）。

面板常驻显示（不依赖画布模式），内置常用材质预设（泥土/木头/塑料/石头/金属），
每个材质展开一整套专业参数：表面（基础色/金属度/粗糙度/折射率/透明度）、次表面
散射、高光（级别/着色/各向异性）、透射、自发光、法向。参数写回当前锁定的浮雕
模型 `ReliefModelObject`，实时驱动 3D 视口的 PBR 着色。

编辑材质或参数时自动把该模型的显示模式切到「渲染(material)」，保证所见即所得。
无绑定模型（非浮雕/无模型）时控件禁用。
"""
from __future__ import annotations

import ui.styles.theme as theme
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QButtonGroup, QPushButton, QSizePolicy,
                               QScrollArea, QDoubleSpinBox, QColorDialog, QCheckBox,
                               QFileDialog)
from PySide6.QtCore import Qt
from i18n import tr, languageChanged
from core import app_settings

from core.relief import material_params as matlib


def _swatchStyle(rgb, selected: bool) -> str:
    """预设按钮 QSS：色块底 + 文字，选中态主色描边。

    用普通模板 + % 填值，避免隐式拼接中 f/非 f 段的 {{ 转义不一致
    （非 f 串的 {{ }} 原样输出 → Qt 报 "Could not parse stylesheet"）。
    """
    border = "border: 2px solid %s;" % theme.PRIMARY if selected else "border: 1px solid #bbb;"
    weight = " font-weight: bold;" if selected else ""
    qss = ("QPushButton { padding: 2px 2px; border-radius: 6px;"
           " font-size: 13px;%s color: #202124;"
           " background: rgb(%d,%d,%d); %s }"
           " QPushButton:hover { border-color: #888; }")
    return qss % (weight, rgb[0], rgb[1], rgb[2], border)


def _colorBtnStyle(rgb01) -> str:
    r, g, b = [int(round(c * 255)) for c in rgb01]
    return ("QPushButton { border: 1px solid #999; border-radius: 3px;"
            " background: rgb(%d,%d,%d); }" % (r, g, b))


def _floatDecimalsStep(key: str):
    """按参数语义给小数位与步长，读数更专业顺手。"""
    if key == "tex_scale":
        return 1, 0.5
    if "radius" in key:
        return 1, 0.5
    if "strength" in key:
        return 2, 0.1
    return 3, 0.01


def _toggleStyle(selected: bool) -> str:
    """质量切换按钮 QSS：选中=主色白字，未选=浅灰底。"""
    if selected:
        return ("QPushButton { padding: 1px 8px; border-radius: 4px; font-size: 11px;"
                " color: white; background: %s; border: 1px solid %s; }"
                % (theme.PRIMARY, theme.PRIMARY))
    return ("QPushButton { padding: 1px 8px; border-radius: 4px; font-size: 11px;"
            " color: #444; background: #f0f0f0; border: 1px solid #bbb; }"
            " QPushButton:hover { border-color: #888; }")


class MaterialPanel(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._doc = None
        self._reliefObj = None
        self._viewport = None             # 3D 视口引用（加载 HDRI 后触发重绘）
        self._canvasMode = "2d"
        self._loading = False             # 回填期间抑制写回
        self._presetButtons = []          # (QPushButton, 存储值, 中文, rgb)
        self._editors = {}                # key -> ("spin", w) | ("color", btn)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(6)

        # ---- 材质预设按钮行（大按钮，平铺整行）----
        self._presetGroup = QButtonGroup(self)
        self._presetGroup.setExclusive(True)
        prow = QWidget()
        pr = QHBoxLayout(prow)
        pr.setContentsMargins(0, 0, 0, 0)
        pr.setSpacing(5)
        for key, text, rgb in matlib.MATERIAL_PRESETS:
            btn = QPushButton(tr(text))
            btn.setCheckable(True)
            btn.setFixedHeight(42)
            btn.setStyleSheet(_swatchStyle(rgb, False))
            btn.setToolTip(tr(text))
            self._presetGroup.addButton(btn)
            self._presetButtons.append((btn, key, text, rgb))
            pr.addWidget(btn, 1)
        outer.addWidget(prow)
        self._presetGroup.buttonClicked.connect(self._onPresetClicked)

        # ---- 渲染质量切换（粗糙 / 精致）----
        qrow = QWidget()
        qr = QHBoxLayout(qrow)
        qr.setContentsMargins(0, 0, 0, 0)
        qr.setSpacing(4)
        qlab = QLabel(tr("质量"))
        qlab.setStyleSheet("font-size: 11px; color: #666;")
        qr.addWidget(qlab)
        self._qualityLabel = qlab
        self._qualityGroup = QButtonGroup(self)
        self._qualityGroup.setExclusive(True)
        self._qualityButtons = []        # (QPushButton, 存储值, 中文)
        for key, text in (("basic", "粗糙"), ("fine", "精致")):
            btn = QPushButton(tr(text))
            btn.setCheckable(True)
            btn.setFixedHeight(22)
            btn.setStyleSheet(_toggleStyle(False))
            btn.setToolTip(tr("实时预览（快）" if key == "basic"
                              else "照片级 IBL+GGX+ACES"))
            self._qualityGroup.addButton(btn)
            self._qualityButtons.append((btn, key, text))
            qr.addWidget(btn)
        qr.addStretch()
        outer.addWidget(qrow)
        self._qualityGroup.buttonClicked.connect(self._onQualityClicked)

        # ---- HDRI 环境光照（全局，非逐模型）----
        erow = QWidget()
        er = QHBoxLayout(erow)
        er.setContentsMargins(0, 0, 0, 0)
        er.setSpacing(4)
        elab = QLabel(tr("环境"))
        elab.setStyleSheet("font-size: 11px; color: #666;")
        er.addWidget(elab)
        self._envLabel = elab
        self._envBtn = QPushButton(tr("加载"))
        self._envBtn.setFixedHeight(22)
        self._envBtn.setStyleSheet(_toggleStyle(False))
        self._envBtn.clicked.connect(self._onLoadEnv)
        er.addWidget(self._envBtn)
        self._envClearBtn = QPushButton(tr("清除"))
        self._envClearBtn.setFixedHeight(22)
        self._envClearBtn.setStyleSheet(_toggleStyle(False))
        self._envClearBtn.clicked.connect(self._onClearEnv)
        er.addWidget(self._envClearBtn)
        self._envInt = QDoubleSpinBox()
        self._envInt.setRange(0.0, 4.0)
        self._envInt.setDecimals(2)
        self._envInt.setSingleStep(0.1)
        self._envInt.setToolTip(tr("环境强度"))
        self._envInt.valueChanged.connect(self._onEnvIntensity)
        er.addWidget(self._envInt, 1)
        outer.addWidget(erow)
        self._syncEnvFromSettings()

        # ---- 参数滚动区 ----
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QScrollArea.NoFrame)
        inner = QWidget()
        form = QVBoxLayout(inner)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(3)
        self._buildParamForm(form)
        form.addStretch()
        self._scroll.setWidget(inner)
        outer.addWidget(self._scroll, 1)

        languageChanged().connect(self.retranslateUi)
        self._refreshEnabled()

    # ---------------- UI 构建 ----------------

    def _buildParamForm(self, form: QVBoxLayout) -> None:
        for _skey, label, rows in matlib.PARAM_SECTIONS:
            head = QLabel(label)
            head.setStyleSheet("font-weight: bold; font-size: 11px;"
                               " color: %s; padding-top: 4px;" % theme.PRIMARY)
            form.addWidget(head)
            for (key, rl, kind, mn, mx, _st) in rows:
                row = QWidget()
                h = QHBoxLayout(row)
                h.setContentsMargins(0, 0, 0, 0)
                h.setSpacing(6)
                lab = QLabel(tr(rl))
                lab.setFixedWidth(84)
                lab.setStyleSheet("font-size: 11px;")
                h.addWidget(lab)
                if kind == matlib.COLOR:
                    btn = QPushButton()
                    btn.setFixedSize(60, 20)
                    btn.setProperty("_rgb", [1.0, 1.0, 1.0])
                    btn.clicked.connect(lambda _c, k=key: self._pickColor(k))
                    self._editors[key] = ("color", btn)
                    h.addWidget(btn, 1, Qt.AlignLeft)
                elif kind == matlib.BOOL:
                    cb = QCheckBox()
                    cb.stateChanged.connect(lambda st, k=key: self._onBool(k, st))
                    self._editors[key] = ("bool", cb)
                    h.addWidget(cb, 1, Qt.AlignLeft)
                else:
                    sp = QDoubleSpinBox()
                    sp.setRange(mn, mx)
                    dec, step = _floatDecimalsStep(key)
                    sp.setDecimals(dec)
                    sp.setSingleStep(step)
                    sp.valueChanged.connect(lambda v, k=key: self._onFloat(k, v))
                    self._editors[key] = ("spin", sp)
                    h.addWidget(sp, 1)
                form.addWidget(row)

    # ---------------- 外部注入 ----------------

    def setDocument(self, doc) -> None:
        self._doc = doc

    def setCanvasMode(self, mode: str) -> None:
        self._canvasMode = mode if mode in ("2d", "relief", "3d", "cnc") else "2d"
        if mode != "relief":
            self._reliefObj = None
        self._refreshEnabled()

    def setReliefModel(self, model) -> None:
        """绑定浮雕编辑目标（与变换面板同源）；传 None 解绑。"""
        self._reliefObj = model
        self._loadFromModel()

    def setViewport(self, vp) -> None:
        """注入 3D 视口引用：环境图变更后触发重绘。"""
        self._viewport = vp

    # ---------------- HDRI 环境光照（全局） ----------------

    def _syncEnvFromSettings(self) -> None:
        """从 app_settings 回填环境控件（强度）。

        标签只写"环境"两个字：270 宽面板里再拼"程序化/文件名"会挤到
        显示不全；当前加载了哪个 HDRI 放在悬停提示里。"""
        self._envInt.blockSignals(True)
        self._envInt.setValue(float(getattr(app_settings, "env_intensity", 1.0) or 1.0))
        self._envInt.blockSignals(False)
        path = getattr(app_settings, "env_map_path", "") or ""
        self._envLabel.setText(tr("环境"))
        self._envLabel.setToolTip(path or tr("内置程序化摄影棚环境"))

    def _repaintViewport(self) -> None:
        vp = self._viewport
        if vp is not None:
            try:
                vp.update()
            except Exception:
                pass

    def _onLoadEnv(self) -> None:
        start = getattr(app_settings, "env_map_path", "") or ""
        path, _ = QFileDialog.getOpenFileName(
            self, tr("选择 HDRI 环境图"), start,
            "HDR / EXR / Image (*.hdr *.exr *.hdr.png *.png *.jpg *.jpeg);;All (*.*)")
        if not path:
            return
        app_settings.env_map_path = path
        app_settings.save_perf()
        self._syncEnvFromSettings()
        self._repaintViewport()

    def _onClearEnv(self) -> None:
        if not (getattr(app_settings, "env_map_path", "") or ""):
            return
        app_settings.env_map_path = ""
        app_settings.save_perf()
        self._syncEnvFromSettings()
        self._repaintViewport()

    def _onEnvIntensity(self, value: float) -> None:
        app_settings.env_intensity = float(value)
        app_settings.save_perf()
        self._repaintViewport()

    # ---------------- 读模型 → 控件 ----------------

    def _loadFromModel(self) -> None:
        m = self._reliefObj
        self._loading = True
        try:
            if m is not None:
                mat = m.material()
                for btn, key, _text, rgb in self._presetButtons:
                    sel = (key == mat)
                    btn.blockSignals(True)
                    btn.setChecked(sel)
                    btn.setStyleSheet(_swatchStyle(rgb, sel))
                    btn.blockSignals(False)
                q = m.materialQuality()
                for btn, key, _text in self._qualityButtons:
                    sel = (key == q)
                    btn.blockSignals(True)
                    btn.setChecked(sel)
                    btn.setStyleSheet(_toggleStyle(sel))
                    btn.blockSignals(False)
                params = m.materialParams()
                for key, (kind, w) in self._editors.items():
                    v = params.get(key)
                    if kind == "spin":
                        w.blockSignals(True)
                        if v is not None:
                            w.setValue(float(v))
                        w.blockSignals(False)
                    elif kind == "bool":
                        w.blockSignals(True)
                        w.setChecked(float(v or 0.0) > 0.5)
                        w.blockSignals(False)
                    else:
                        rgb = list(v) if v else [1.0, 1.0, 1.0]
                        w.setProperty("_rgb", rgb)
                        w.setStyleSheet(_colorBtnStyle(rgb))
            else:
                # 无模型：用当前选中的（或 clay 默认）预设参数展示，只读
                cur = self._currentPresetId()
                params = matlib.default_params(cur)
                for key, (kind, w) in self._editors.items():
                    v = params.get(key)
                    if kind == "spin":
                        w.blockSignals(True); w.setValue(float(v)); w.blockSignals(False)
                    elif kind == "bool":
                        w.blockSignals(True); w.setChecked(float(v or 0.0) > 0.5); w.blockSignals(False)
                    else:
                        rgb = list(v) if v else [1.0, 1.0, 1.0]
                        w.setProperty("_rgb", rgb); w.setStyleSheet(_colorBtnStyle(rgb))
        finally:
            self._loading = False
        self._refreshEnabled()

    def _currentPresetId(self) -> str:
        b = self._presetGroup.checkedButton()
        for btn, key, _t, _r in self._presetButtons:
            if btn is b:
                return key
        return "clay"

    # ---------------- 控件 → 模型 ----------------

    def _onPresetClicked(self, btn) -> None:
        if self._loading:
            return
        key = next((k for b, k, _t, _r in self._presetButtons if b is btn), None)
        if key is None:
            return
        # 同步选中描边
        for b, _k, _t, rgb in self._presetButtons:
            b.setStyleSheet(_swatchStyle(rgb, b is btn))
        m = self._reliefObj
        if m is None or self._doc is None:
            self._loadFromModel()          # 无模型：仅预览该预设参数
            return
        m.setDisplay3DMode("material")     # 关联渲染显示模式
        m.setMaterial(key)                 # 重置为该材质物理预设
        self._doc.notifyObjectChanged(m.id())
        self._loadFromModel()

    def _onQualityClicked(self, btn) -> None:
        """粗糙/精致切换：写回模型渲染质量档并切到渲染显示。"""
        if self._loading:
            return
        key = next((k for b, k, _t in self._qualityButtons if b is btn), None)
        if key is None:
            return
        for b, _k, _t in self._qualityButtons:
            b.setStyleSheet(_toggleStyle(b is btn))
        m = self._reliefObj
        if m is None or self._doc is None:
            return
        m.setDisplay3DMode("material")
        if m.setMaterialQuality(key):
            self._doc.notifyObjectChanged(m.id())

    def _onFloat(self, key: str, value: float) -> None:
        if self._loading:
            return
        self._applyParam(key, float(value))

    def _onBool(self, key: str, state: int) -> None:
        if self._loading:
            return
        self._applyParam(key, 1.0 if state else 0.0)

    def _pickColor(self, key: str) -> None:
        if self._loading:
            return
        kind, w = self._editors[key]
        rgb = w.property("_rgb") or [1.0, 1.0, 1.0]
        c = QColorDialog.getColor(
            _toQColor(rgb), self, tr("选择颜色"))
        if not c.isValid():
            return
        new = [c.redF(), c.greenF(), c.blueF()]
        w.setProperty("_rgb", new)
        w.setStyleSheet(_colorBtnStyle(new))
        self._applyParam(key, new)

    def _applyParam(self, key: str, value) -> None:
        m = self._reliefObj
        if m is None or self._doc is None:
            return
        m.setDisplay3DMode("material")     # 编辑参数即切到渲染，所见即所得
        if m.setMaterialParam(key, value):
            self._doc.notifyObjectChanged(m.id())

    def _refreshEnabled(self) -> None:
        ok = self._reliefObj is not None
        for btn, _k, _t, rgb in self._presetButtons:
            btn.setEnabled(ok)
            b = self._presetGroup.checkedButton()
            btn.setStyleSheet(_swatchStyle(rgb, btn is b))
        for btn, _k, _t in self._qualityButtons:
            btn.setEnabled(ok)
            qb = self._qualityGroup.checkedButton()
            btn.setStyleSheet(_toggleStyle(btn is qb))
        for _key, (_kind, w) in self._editors.items():
            w.setEnabled(ok)

    # ---------------- i18n ----------------

    def retranslateUi(self) -> None:
        for btn, _key, text, _rgb in self._presetButtons:
            btn.setText(tr(text))
            btn.setToolTip(tr(text))
        self._qualityLabel.setText(tr("质量"))
        for btn, key, text in self._qualityButtons:
            btn.setText(tr(text))
            btn.setToolTip(tr("实时预览（快）" if key == "basic"
                              else "照片级 IBL+GGX+ACES"))
        self._envBtn.setText(tr("加载"))
        self._envClearBtn.setText(tr("清除"))
        self._envInt.setToolTip(tr("环境强度"))
        self._syncEnvFromSettings()


def _toQColor(rgb01):
    from PySide6.QtGui import QColor
    return QColor(int(round(rgb01[0] * 255)), int(round(rgb01[1] * 255)),
                  int(round(rgb01[2] * 255)))

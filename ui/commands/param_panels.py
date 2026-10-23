"""命令参数面板：基类 + 9 个绘图/编辑命令的面板。

面板由命令实例懒创建（CommandInstance.paramPanel()），命令激活时
被 CommandParamHost 挂到画布右侧；面板信号连回命令槽，数值修改
实时更新命令状态与画布预览。
"""
from __future__ import annotations
import math
import os
from typing import List
from PySide6.QtCore import Qt, Signal, QEvent
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
                               QGridLayout, QDoubleSpinBox, QSpinBox,
                               QPushButton, QCheckBox, QRadioButton,
                               QButtonGroup, QLabel, QGroupBox, QFrame,
                               QComboBox, QStackedWidget, QSizePolicy,
                               QAbstractSpinBox)

import ui.styles.theme as theme
from core.relief import HeightField
from ui.widgets.smear_state import SmearState, MODE_LABELS as _SMEAR_LABELS
from core.relief.region_relief import (RegionReliefParams, COLOR_SCOPES,
                                       COLOR_SCOPE_LABELS)
from core.relief.textures import (FILL_PATTERNS, FILL_PATTERN_LABELS,
                                  RAND_SUBS, RAND_SUB_LABELS)
from core.relief.wall_curtain import DeJitterParams, DEFAULT_DEJITTER_DISTANCE
from core.relief.stamp import (StampParams, DEFAULT_ABSOLUTE, DEFAULT_RELATIVE,
                               DEFAULT_HIGH_LEVEL, DEFAULT_LOW_LEVEL,
                               DEFAULT_MID_PERCENT, DEFAULT_DIAMETER,
                               DIAMETER_MIN, DIAMETER_MAX,
                               SCOPES, SCOPE_LABELS, LEVEL_REGIONS,
                               LEVEL_REGION_LABELS)
from core.relief.sweep import (SweepParams, DEFAULT_SWEEP_DIAMETER,
                               DEFAULT_SWEEP_HALF_LIFE, DEFAULT_SWEEP_HEIGHT,
                               DEFAULT_SWEEP_THIN_PERCENT, BRUSH_SHAPE_LABELS,
                               BRUSH_TYPE_LABELS, MERGE_LABELS, MERGES,
                               APPLY_MODES, APPLY_LABELS,
                               PARAM_SCALE, HEIGHT_STEP, PARAM_MIN)
from core.relief.sweep import SweepParams as _BrushSweepParams
from core.relief.smooth import (SmoothParams, MODE_LABELS, STRENGTH_PRESETS,
                                STRENGTH_MIN, STRENGTH_MAX,
                                DEFAULT_SMOOTH_STRENGTH,
                                DEFAULT_SMOOTH_DIAMETER,
                                DEFAULT_SMOOTH_HALF_LIFE)

_SPIN_RANGE = (-999999.0, 999999.0)
_LABEL_W = 52      # 行标签统一最小宽：容纳"指定角度"4 字不截断


class _PanelStateStore:
    """命令面板参数持久化（%APPDATA%/Cangai/command_panel_state.json）。

    按面板类名分组存取；同一类名下按控件遍历序定位（面板构建顺序固定）。
    """

    @staticmethod
    def path() -> str:
        import os
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "Cangai")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            d = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(d, "command_panel_state.json")

    @classmethod
    def load(cls, key: str) -> dict:
        import json
        try:
            with open(cls.path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            v = data.get(key)
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}

    @classmethod
    def save(cls, key: str, values: dict) -> None:
        import json
        data = {}
        try:
            with open(cls.path(), "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        data[key] = values
        try:
            with open(cls.path(), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        except Exception:
            pass


class _ClickLabel(QLabel):
    """可点击的行标签（原版矩形面板长/宽标题点击联动）。"""
    clicked = Signal()

    def __init__(self, text: str = "", tip: str = "") -> None:
        super().__init__(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if tip:
            self.setToolTip(tip)
        self.setStyleSheet("color:%s; text-decoration:underline;" % theme.PRIMARY_DARK)

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(ev)


class ParamPanelBase(QWidget):
    """所有命令参数面板的公共基类：样式与控件工厂。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # QWidget 需开启 WA_StyledBackground 才会绘制样式表背景，否则面板无底色无边框
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "QWidget#ParamPanel { background:#ffffff; border:1px solid %s;"
            " border-radius:4px; }" % theme.BORDER)
        self.setObjectName("ParamPanel")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(6, 2, 6, 6)
        self._layout.setSpacing(5)
        self._spinX = self._spinY = self._spinZ = None

    # ---------- 控件工厂 ----------

    def _spin(self, value: float = 0.0, decimals: int = 2,
              step: float = 1.0) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(*_SPIN_RANGE)
        s.setDecimals(decimals)
        s.setSingleStep(step)
        s.setValue(value)
        return s

    def _ispin(self, value: int = 0, lo: int = 0, hi: int = 100) -> QSpinBox:
        s = QSpinBox()
        s.setRange(lo, hi)
        s.setValue(value)
        return s

    def _button(self, text: str, primary: bool = False) -> QPushButton:
        b = QPushButton(text)
        if primary:
            b.setStyleSheet(
                "QPushButton { background:%s; color:%s; padding:4px 10px; }"
                "QPushButton:hover { background:%s; }"
                % (theme.PRIMARY, theme.TEXT_ON_PRIMARY, theme.PRIMARY_DARK))
        return b

    def _button2(self, text: str) -> QPushButton:
        """次级按钮：白底描边，弱化视觉层级，与主按钮区分。"""
        b = QPushButton(text)
        b.setStyleSheet(
            "QPushButton { background:%s; color:%s; border:1px solid %s;"
            " border-radius:2px; padding:3px 8px; }"
            "QPushButton:hover { border-color:%s; color:%s; }"
            % (theme.BG_PANEL, theme.TEXT, theme.BORDER,
               theme.PRIMARY, theme.PRIMARY_DARK))
        return b

    def _label(self, text: str) -> QLabel:
        return QLabel(text)

    def _addRow(self, label: str, widget: QWidget) -> None:
        row = QHBoxLayout()
        row.setSpacing(4)
        lab = QLabel(label)
        lab.setMinimumWidth(_LABEL_W)
        row.addWidget(lab)
        row.addWidget(widget, 1)
        self._layout.addLayout(row)

    def _addRowLayout(self, label: str, widgets: list) -> None:
        """标签 + 多个 widget 横排（用于单选/多选按钮组）。"""
        row = QHBoxLayout()
        row.setSpacing(8)
        lab = QLabel(label)
        lab.setMinimumWidth(_LABEL_W)
        row.addWidget(lab)
        for w in widgets:
            row.addWidget(w)
        row.addStretch(1)
        self._layout.addLayout(row)

    def _addCoordRows(self, withZ: bool = False) -> None:
        """X/Y/(Z) 坐标输入行，供画布取点类命令精确输入坐标。"""
        self._spinX = self._spin()

    # ---------- 分组行工厂（基类提供；历史面板自有同名定义优先） ----------

    def _group(self, title: str):
        """分组框（返回 (QGroupBox, QVBoxLayout)）。"""
        g = QGroupBox(title)
        lay = QVBoxLayout(g)
        lay.setSpacing(3)
        lay.setContentsMargins(6, 4, 6, 4)
        return g, lay

    def _row(self, label: str, widget) -> QWidget:
        """标签 + 控件行（整体 QWidget，便于整行启用/禁用）。"""
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lab = QLabel(label)
        lab.setMinimumWidth(_LABEL_W)
        lay.addWidget(lab)
        lay.addWidget(widget, 1)
        return row

    def _radioRow(self, label, texts, group: QButtonGroup,
                  start_id: int = 0) -> QWidget:
        """一组单选按钮行（整体 QWidget，便于整行显示/隐藏）。"""
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setSpacing(4)
        lay.setContentsMargins(0, 0, 0, 0)
        if label:
            lab = QLabel(label)
            lab.setMinimumWidth(_LABEL_W)
            lay.addWidget(lab)
        for i, t in enumerate(texts):
            rb = QRadioButton(t)
            group.addButton(rb, start_id + i)
            lay.addWidget(rb)
        lay.addStretch(1)
        return row

    def _divider(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        line.setStyleSheet("color:%s;" % theme.BORDER)
        return line

    def _addCoordRows(self, withZ: bool = False) -> None:
        """X/Y/(Z) 坐标输入行，供画布取点类命令精确输入坐标。"""
        self._spinX = self._spin()
        self._spinY = self._spin()
        self._addRow("X", self._spinX)
        self._addRow("Y", self._spinY)
        if withZ:
            self._spinZ = self._spin()
            self._addRow("Z", self._spinZ)

    def _addPlaceholder(self, text: str) -> None:
        """扩展功能占位：置灰不可用。"""
        cb = QCheckBox(text)
        cb.setEnabled(False)
        cb.setToolTip("该功能尚未移植，后续版本提供")
        self._layout.addWidget(cb)

    # ---------- 直径/半衰直径联动（堆料/磨光/导动面板共用） ----------

    def _bindHalfLifeFollow(self, spinD: QDoubleSpinBox,
                            spinHL: QDoubleSpinBox) -> None:
        """直径变化 → 半衰直径**等比跟随**（保持比值，不用调两个参数）；
        半衰直径仍可独立设置（钳制 ≤ 直径）。有 paramsChanged 信号的面板
        会随之发信号（笔刷圈/视口即时刷新）。"""
        self._hlSpinD = spinD
        self._hlSpinHL = spinHL
        self._hlPrevD = spinD.value()
        spinD.valueChanged.connect(self._hlOnDiameterChanged)
        spinHL.valueChanged.connect(self._hlOnHalfLifeChanged)

    def _hlOnDiameterChanged(self, v: float) -> None:
        prev = self._hlPrevD
        self._hlPrevD = v
        if prev < 1e-9:
            return
        hl = self._hlSpinHL.value() * (v / prev)
        self._hlSpinHL.setValue(max(0.0, min(hl, v)))
        self._emitParamsChanged()

    def _hlOnHalfLifeChanged(self, v: float) -> None:
        d = self._hlSpinD.value()
        if v > d:                       # 独立设置时钳制 ≤ 直径
            self._hlSpinHL.setValue(d)
        self._emitParamsChanged()

    def _emitParamsChanged(self) -> None:
        sig = getattr(self, "paramsChanged", None)
        if sig is not None:
            sig.emit()

    def _notifyResize(self) -> None:
        """面板区域展开/收起后，逐级向上通知容器重新适配高度。
        只到面板宿主（CommandParamHost）为止：继续向上 adjustSize
        会波及主窗口，切换模式时整个界面会闪烁缩放。"""
        from ui.widgets.command_param_host import CommandParamHost
        lay = self.layout()
        if lay is not None:
            lay.invalidate()
            lay.activate()
        self.adjustSize()
        p = self.parentWidget()
        while p is not None:
            if isinstance(p, CommandParamHost):
                p._fitHeight()
                break
            from PySide6.QtWidgets import QScrollArea
            if isinstance(p, QScrollArea):
                # 滚动区 adjustSize 会缩到自身 sizeHint，压缩可见高度——跳过
                p = p.parentWidget()
                continue
            pl = p.layout()
            if pl is not None:
                pl.invalidate()
                pl.activate()
            p.adjustSize()
            p = p.parentWidget()

    # ---------- 坐标读写 ----------

    def x(self) -> float:
        return self._spinX.value() if self._spinX else 0.0

    def y(self) -> float:
        return self._spinY.value() if self._spinY else 0.0

    def z(self) -> float:
        return self._spinZ.value() if self._spinZ else 0.0

    def setXYZ(self, x: float, y: float, z: float = 0.0) -> None:
        """画布取点时回写坐标显示（阻断信号防回环）。"""
        for s, v in ((self._spinX, x), (self._spinY, y), (self._spinZ, z)):
            if s is None:
                continue
            s.blockSignals(True)
            s.setValue(v)
            s.blockSignals(False)

    # ---------- 参数记忆（重开命令保留上次填的参数） ----------

    def _persistKey(self) -> str:
        return self.__class__.__name__

    def _persistWidgets(self) -> list:
        """收集可持久化的输入控件（按创建序定位；坐标 X/Y/Z 除外；
        属于 QButtonGroup 的单选钮由按钮组统一收集，避免双重定位）。"""
        excl = {id(w) for w in (self._spinX, self._spinY, self._spinZ)
                if w is not None}
        out = []
        for cls in (QDoubleSpinBox, QSpinBox, QComboBox, QCheckBox,
                    QRadioButton):
            for w in self.findChildren(cls):
                if id(w) in excl:
                    continue
                if isinstance(w, QRadioButton) and w.group() is not None:
                    continue   # 按钮组统一负责
                out.append(w)
        return out

    def _persistGroups(self) -> list:
        return self.findChildren(QButtonGroup)

    def _collectState(self) -> dict:
        state = {}
        counts: dict = {}
        for w in self._persistWidgets():
            cls = type(w).__name__
            counts[cls] = counts.get(cls, 0) + 1
            key = "%s#%d" % (cls, counts[cls] - 1)
            if isinstance(w, (QDoubleSpinBox, QSpinBox)):
                state[key] = w.value()
            elif isinstance(w, QComboBox):
                state[key] = w.currentIndex()
            elif isinstance(w, (QCheckBox, QRadioButton)):
                state[key] = w.isChecked()
        for i, g in enumerate(self._persistGroups()):
            state["QButtonGroup#%d" % i] = g.checkedId()
        return state

    def _applyState(self, state: dict) -> None:
        counts: dict = {}
        changed_combos: list = []
        changed_checks: list = []
        changed_groups: list = []
        for w in self._persistWidgets():
            cls = type(w).__name__
            idx = counts.get(cls, 0)
            counts[cls] = idx + 1
            key = "%s#%d" % (cls, idx)
            if key not in state:
                continue
            v = state[key]
            w.blockSignals(True)
            try:
                if isinstance(w, (QDoubleSpinBox, QSpinBox)):
                    w.setValue(float(v))
                elif isinstance(w, QComboBox):
                    if isinstance(v, int) and 0 <= v < w.count():
                        old = w.currentIndex()
                        w.setCurrentIndex(v)
                        if w.currentIndex() != old:
                            changed_combos.append(w)
                elif isinstance(w, (QCheckBox, QRadioButton)):
                    old = w.isChecked()
                    w.setChecked(bool(v))
                    if w.isChecked() != old:
                        changed_checks.append(w)
            except Exception:
                pass
            w.blockSignals(False)
        # 按钮组（单选组）：恢复 checkedId 并重放 idClicked（kind 等联动
        # 接的是 idClicked，toggled 重放不会触发它们）
        for i, g in enumerate(self._persistGroups()):
            key = "QButtonGroup#%d" % i
            if key not in state:
                continue
            v = state[key]
            if isinstance(v, int) and v != g.checkedId() \
                    and g.button(v) is not None:
                g.button(v).setChecked(True)
                changed_groups.append((g, v))
        # 恢复期间 blockSignals 抑制了面板联动（如下方内容区随选择切换）
        # —— 对值有变化的控件补发联动信号（幂等，只影响真正变化的项）
        for w in changed_combos:
            w.currentIndexChanged.emit(w.currentIndex())
        for w in changed_checks:
            w.toggled.emit(w.isChecked())
        for g, v in changed_groups:
            g.idClicked.emit(v)

    def savePanelState(self) -> None:
        try:
            _PanelStateStore.save(self._persistKey(), self._collectState())
        except Exception:
            pass

    def restorePanelState(self) -> None:
        try:
            state = _PanelStateStore.load(self._persistKey())
            if state:
                self._applyState(state)
        except Exception:
            pass

    def showEvent(self, ev) -> None:
        if not getattr(self, "_stateRestored", False):
            self._stateRestored = True
            self.restorePanelState()
        super().showEvent(ev)

    def hideEvent(self, ev) -> None:
        self.savePanelState()
        super().hideEvent(ev)


# ==================== Point ====================

class PointParamPanel(ParamPanelBase):
    confirmed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._addCoordRows(withZ=True)
        btn = self._button("确认创建点", primary=True)
        btn.clicked.connect(self.confirmed.emit)
        self._layout.addWidget(btn)
        self._layout.addStretch(1)


# ==================== Line ====================

class LineParamPanel(ParamPanelBase):
    addPointRequested = Signal()
    undoClicked = Signal()
    closeToggled = Signal(bool)
    confirmRequested = Signal()
    drawParamRequested = Signal()   # 兼容旧引用（新面板改用开关式指定长度/角度）
    bisectorToggled = Signal(bool)  # 角平分线模式开关

    def _modeButton(self, text: str, tip: str) -> QPushButton:
        """可按下高亮的模式按钮（标题即开关：按下生效、再按关闭）。"""
        b = self._button2(text)
        b.setCheckable(True)
        b.setToolTip(tip)
        b.setStyleSheet(
            "QPushButton { background:%s; color:%s; border:1px solid %s;"
            " border-radius:2px; padding:3px 8px; }"
            "QPushButton:hover { border-color:%s; color:%s; }"
            "QPushButton:checked { background:%s; color:%s;"
            " border-color:%s; font-weight:bold; }"
            % (theme.BG_PANEL, theme.TEXT, theme.BORDER, theme.PRIMARY,
               theme.PRIMARY_DARK, theme.PRIMARY, theme.TEXT_ON_PRIMARY,
               theme.PRIMARY))
        return b

    def isLengthMode(self) -> bool:
        return self._btnLenMode.isChecked()

    def isAngleMode(self) -> bool:
        return self._btnAngMode.isChecked()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # 坐标用网格而非固定像素宽：X/Z 同处第 1 列（拉伸），Y/添加点同处第 3 列，
        # 列内宽度由布局统一分配 → 永远等宽且左端对齐，且任何面板宽度下都能压缩，
        # 不会撑出横向滚动条（默认内容宽 ≈258，含竖滚动条时 ≈252）。
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(5)
        self._spinX = self._spin()
        self._spinY = self._spin()
        self._spinZ = self._spin()
        for sp in (self._spinX, self._spinY, self._spinZ):
            sp.setMinimumWidth(44)          # 允许压缩，防溢出
            sp.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        labX, labY, labZ = QLabel("X"), QLabel("Y"), QLabel("Z")
        for lb in (labX, labY, labZ):
            lb.setFixedWidth(14)
        btnAdd = self._button2("添加点")
        btnAdd.setMinimumWidth(44)
        btnAdd.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        btnAdd.clicked.connect(self.addPointRequested.emit)
        grid.addWidget(labX, 0, 0)
        grid.addWidget(self._spinX, 0, 1)
        grid.addWidget(labY, 0, 2)
        grid.addWidget(self._spinY, 0, 3)
        grid.addWidget(labZ, 1, 0)
        grid.addWidget(self._spinZ, 1, 1)
        grid.addWidget(btnAdd, 1, 3)
        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 0)
        grid.setColumnStretch(3, 1)
        self._layout.addLayout(grid)

        # 实时读数同行、各占一半：长度 | 角度（同一网格列宽保证对半分）
        self._labelLength = self._label("--")
        self._labelAngle = self._label("--")
        for lb in (self._labelLength, self._labelAngle):
            # 水平 Ignored：读数再长也不会撑宽面板（超宽时裁切而非出滚动条）
            lb.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            lb.setMinimumWidth(0)
            lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
        info = QGridLayout()
        info.setContentsMargins(0, 0, 0, 0)
        info.setHorizontalSpacing(4)
        labLen, labAng = QLabel("长度："), QLabel("角度：")
        for lb in (labLen, labAng):
            lb.setFixedWidth(34)
        info.addWidget(labLen, 0, 0)
        info.addWidget(self._labelLength, 0, 1)
        info.addWidget(labAng, 0, 2)
        info.addWidget(self._labelAngle, 0, 3)
        info.setColumnStretch(0, 0)
        info.setColumnStretch(1, 1)
        info.setColumnStretch(2, 0)
        info.setColumnStretch(3, 1)
        self._layout.addLayout(info)

        # 指定长度：标题按钮即开关（按下高亮生效，再按关闭）
        self._btnLenMode = self._modeButton(
            "指定长度", "按下后按此长度落点，再按关闭")
        self._spinLength = self._spin(0.0, decimals=3)
        self._spinLength.setMinimumWidth(44)
        self._spinLength.setToolTip("指定长度（配合左侧开关使用）")
        rowLen = QHBoxLayout()
        rowLen.setSpacing(4)
        rowLen.addWidget(self._btnLenMode)
        rowLen.addWidget(self._spinLength, 1)
        self._layout.addLayout(rowLen)

        # 指定角度：标题按钮即开关 + 顺/逆 + 角度
        self._angleCCW = True
        self._btnAngMode = self._modeButton(
            "指定角度", "按下后按此角度落点（相对上段方向），再按关闭")
        self._btnAngleSign = self._button2("逆")
        self._btnAngleSign.setToolTip("切换：逆时针/顺时针")
        self._btnAngleSign.clicked.connect(self._onSignToggled)
        self._spinAngle = self._spin(0.0, decimals=2)
        self._spinAngle.setMinimumWidth(44)
        rowAng = QHBoxLayout()
        rowAng.setSpacing(4)
        rowAng.addWidget(self._btnAngMode)
        rowAng.addWidget(self._btnAngleSign)
        rowAng.addWidget(self._spinAngle, 1)
        self._layout.addLayout(rowAng)

        # 角平分线 / 闭合：同一行勾选项（角度弧预览恒开，不再提供勾选项）
        self._chkBisector = QCheckBox("角平分线")
        self._chkBisector.setToolTip(
            "依次选两条直线，预览角平分线，再点击确定终点")
        self._chkBisector.toggled.connect(self.bisectorToggled.emit)
        self._chkClose = QCheckBox("闭合")
        self._chkClose.toggled.connect(self.closeToggled.emit)
        rowOpt = QHBoxLayout()
        rowOpt.setSpacing(6)
        rowOpt.addWidget(self._chkBisector)
        rowOpt.addWidget(self._chkClose)
        self._layout.addLayout(rowOpt)

        # 撤回 / 确认完成：网格两等分列，宽度恒等
        self._btnUndo = self._button2("撤回（Z）")
        self._btnUndo.setMinimumWidth(44)
        self._btnUndo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._btnUndo.clicked.connect(self.undoClicked.emit)
        btnOk = self._button("确认完成", primary=True)
        btnOk.setMinimumWidth(44)
        btnOk.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        btnOk.clicked.connect(self.confirmRequested.emit)
        act = QGridLayout()
        act.setContentsMargins(0, 0, 0, 0)
        act.setHorizontalSpacing(6)
        act.addWidget(self._btnUndo, 0, 0)
        act.addWidget(btnOk, 0, 1)
        act.setColumnStretch(0, 1)
        act.setColumnStretch(1, 1)
        self._layout.addLayout(act)

        # 闭合不可用提示：无提示时不占高度
        self._closeHint = self._label("")
        self._closeHint.setWordWrap(True)
        self._closeHint.setVisible(False)
        self._layout.addWidget(self._closeHint)
        self._layout.addStretch(1)

        # 数值框会因"落点后自动聚焦长度"而抢走键盘焦点，导致 B/Z 等
        # 快捷键打在输入框里失效；这里把字母键/Esc 转发给命令处理。
        self._keyHandler = None
        for sp in (self._spinX, self._spinY, self._spinZ,
                   self._spinLength, self._spinAngle):
            sp.installEventFilter(self)

    def setKeyHandler(self, handler) -> None:
        """handler(key, mods) -> bool（True=命令已消费，吞掉该键）。"""
        self._keyHandler = handler

    def eventFilter(self, obj, ev) -> bool:
        if ev.type() == QEvent.Type.KeyPress and isinstance(obj, QAbstractSpinBox):
            key = ev.key()
            if key == Qt.Key_Escape or Qt.Key_A <= key <= Qt.Key_Z:
                if self._keyHandler is not None \
                        and self._keyHandler(key, ev.modifiers()):
                    return True
        return super().eventFilter(obj, ev)

    def _onSignToggled(self) -> None:
        self._angleCCW = not self._angleCCW
        self._btnAngleSign.setText("逆" if self._angleCCW else "顺")

    def lengthValue(self) -> float:
        return self._spinLength.value()

    def angleValue(self) -> float:
        return self._spinAngle.value()

    def angleCCW(self) -> bool:
        return self._angleCCW

    def setLength(self, length: float) -> None:
        self._labelLength.setText(f"{length:.3f}")

    def setAngle(self, degrees: float) -> None:
        self._labelAngle.setText(f"{degrees:.2f}°")

    def isAnglePreviewChecked(self) -> bool:
        return True     # 角度弧预览恒开（用户要求去掉勾选项，默认始终预览）

    def setCloseEnabled(self, enabled: bool, hint: str = "") -> None:
        self._chkClose.setEnabled(enabled)
        self._closeHint.setText(hint)
        # 有提示才占高度（面板宽度仅 270px，长提示会换行）
        self._closeHint.setVisible(bool(hint))

    def focusOnLength(self) -> None:
        self._spinLength.setFocus()
        self._spinLength.selectAll()

    def setClosed(self, closed: bool) -> None:
        self._chkClose.blockSignals(True)
        self._chkClose.setChecked(closed)
        self._chkClose.blockSignals(False)

    def setBisectorChecked(self, on: bool) -> None:
        self._chkBisector.blockSignals(True)
        self._chkBisector.setChecked(on)
        self._chkBisector.blockSignals(False)


# ==================== Curve ====================

class CurveParamPanel(ParamPanelBase):
    """样条曲线参数面板：坐标加点/撤销/闭合/确认（C2 插值样条）。"""
    addPointRequested = Signal()
    undoClicked = Signal()
    closeToggled = Signal(bool)     # 保留：其它调用方/自动闭合的状态同步
    closeRequested = Signal()       # 点"闭合（B）"按钮：请求闭合结束
    confirmRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._addCoordRows(withZ=True)
        btnAdd = self._button2("添加点")
        btnAdd.clicked.connect(self.addPointRequested.emit)
        self._layout.addWidget(btnAdd)

        row = QHBoxLayout()
        row.setSpacing(6)
        btnUndo = self._button2("撤回（Z）")
        btnUndo.clicked.connect(self.undoClicked.emit)
        # 闭合是"动作"按钮（不是开关）：按下即闭合结束，与快捷键 B 同一逻辑
        self._btnClose = self._button2("闭合（B）")
        self._btnClose.setToolTip("按 B 或点此按钮：闭合曲线并结束（至少 3 点）")
        self._btnClose.clicked.connect(self.closeRequested.emit)
        row.addWidget(btnUndo, 1)
        row.addWidget(self._btnClose, 1)
        self._layout.addLayout(row)

        btnOk = self._button("确认完成", primary=True)
        btnOk.clicked.connect(self.confirmRequested.emit)
        self._layout.addWidget(btnOk)
        self._layout.addStretch(1)

    def setClosed(self, closed: bool) -> None:
        """同步闭合状态（自动闭合时按钮变主题色，仅视觉反馈）。"""
        self._btnClose.setStyleSheet(
            "QPushButton { background:%s; color:%s; border:1px solid %s; }"
            % ((theme.PRIMARY, theme.TEXT_ON_PRIMARY, theme.PRIMARY)
               if closed else (theme.BG_PANEL, theme.TEXT, theme.BORDER)))

    def isClosed(self) -> bool:
        return False


# ==================== Arc ====================

class ArcParamPanel(ParamPanelBase):
    addPointRequested = Signal()
    confirmRequested = Signal()
    modeChanged = Signal(int)   # 0=起点-终点-弧上点 1=起点-弧上点-终点

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # 模式文案较长，面板宽度有限 → 垂直排列避免挤压截断
        self._layout.addWidget(QLabel("绘制模式"))
        self._modeGroup = QButtonGroup(self)
        self._radioSE = QRadioButton("起点-终点-弧上点")
        self._radioSM = QRadioButton("起点-弧上点-终点")
        self._radioSE.setChecked(True)
        self._modeGroup.addButton(self._radioSE, 0)
        self._modeGroup.addButton(self._radioSM, 1)
        self._modeGroup.idClicked.connect(self.modeChanged.emit)
        self._modeGroup.idClicked.connect(self._syncHint)
        self._layout.addWidget(self._radioSE)
        self._layout.addWidget(self._radioSM)

        self._hintLabel = self._label("三点定弧：起点 → 终点 → 弧上点")
        self._hintLabel.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        self._layout.addWidget(self._hintLabel)
        self._addCoordRows(withZ=True)
        btnAdd = self._button2("添加点")
        btnAdd.clicked.connect(self.addPointRequested.emit)
        self._layout.addWidget(btnAdd)
        btnOk = self._button("确认完成", primary=True)
        btnOk.clicked.connect(self.confirmRequested.emit)
        self._layout.addWidget(btnOk)
        self._layout.addStretch(1)

    def setMode(self, mode: int) -> None:
        btn = self._radioSE if mode == 0 else self._radioSM
        self._modeGroup.blockSignals(True)
        btn.setChecked(True)
        self._modeGroup.blockSignals(False)
        self._syncHint(mode)

    def _syncHint(self, mode: int) -> None:
        if mode == 0:
            self._hintLabel.setText("三点定弧：起点 → 终点 → 弧上点")
        else:
            self._hintLabel.setText("三点定弧：起点 → 弧上点 → 终点")


# ==================== Circle ====================

class CircleParamPanel(ParamPanelBase):
    diameterChanged = Signal(float)
    confirmed = Signal()
    waveModeChanged = Signal(bool)
    waveArcCountChanged = Signal(int)
    waveChamferChanged = Signal(float)
    waveTypeChanged = Signal(int)
    waveChamferTypeChanged = Signal(int)   # 0=倒直角 1=圆角（圆弧）
    waveRadiusChanged = Signal()   # 内/外半径任一变化（仅启用态触发）

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        modeRow = QHBoxLayout()
        modeRow.setSpacing(4)
        self._modeGroup = QButtonGroup(self)
        self._radioStd = QRadioButton("标准")
        self._radioWave = QRadioButton("波浪")
        self._radioStd.setChecked(True)
        self._modeGroup.addButton(self._radioStd, 0)
        self._modeGroup.addButton(self._radioWave, 1)
        self._modeGroup.idClicked.connect(self._onMode)
        modeRow.addWidget(QLabel("模式"))
        modeRow.addWidget(self._radioStd)
        modeRow.addWidget(self._radioWave)
        modeRow.addStretch(1)
        self._layout.addLayout(modeRow)

        self._addCoordRows()
        self._spinDia = self._spin(0.0, step=0.5)
        self._spinDia.setMinimum(0.0)
        self._spinDia.valueChanged.connect(self.diameterChanged.emit)
        self._addRow("直径", self._spinDia)

        # 波浪参数区（选波浪模式后展开）
        self._waveWidget = QWidget()
        wlay = QVBoxLayout(self._waveWidget)
        wlay.setContentsMargins(0, 0, 0, 0)
        wlay.setSpacing(5)
        # 波形：直线 / 样条 / 圆弧（默认圆弧，与原版波浪圆外观一致）
        self._waveTypeGroup = QButtonGroup(self)
        self._radioWaveLine = QRadioButton("直线")
        self._radioWaveSpline = QRadioButton("样条")
        self._radioWaveArc = QRadioButton("圆弧")
        self._radioWaveArc.setChecked(True)
        self._waveTypeGroup.addButton(self._radioWaveLine, 0)
        self._waveTypeGroup.addButton(self._radioWaveSpline, 1)
        self._waveTypeGroup.addButton(self._radioWaveArc, 2)
        self._waveTypeGroup.idClicked.connect(self.waveTypeChanged.emit)
        trow = QHBoxLayout()
        trow.setSpacing(4)
        labT = QLabel("波形")
        labT.setMinimumWidth(_LABEL_W)
        trow.addWidget(labT)
        trow.addWidget(self._radioWaveLine)
        trow.addWidget(self._radioWaveSpline)
        trow.addWidget(self._radioWaveArc)
        trow.addStretch(1)
        wlay.addLayout(trow)
        self._spinArcCount = self._ispin(8, 2, 36)
        crow = QHBoxLayout()
        crow.setSpacing(4)
        labC = QLabel("弧数")
        labC.setMinimumWidth(_LABEL_W)
        crow.addWidget(labC)
        crow.addWidget(self._spinArcCount, 1)
        wlay.addLayout(crow)
        # 内/外半径：锁定大小前回写禁用，锁定后可实时调节
        self._spinInner = self._spin(0.0, decimals=3, step=0.5)
        self._spinInner.setMinimum(0.001)
        self._spinBulge = self._spin(0.0, decimals=3, step=0.5)
        self._spinBulge.setMinimum(0.001)
        self._spinInner.setEnabled(False)
        self._spinBulge.setEnabled(False)
        irow = QHBoxLayout()
        irow.setSpacing(4)
        labI = QLabel("内半径")
        labI.setMinimumWidth(_LABEL_W)
        irow.addWidget(labI)
        irow.addWidget(self._spinInner, 1)
        wlay.addLayout(irow)
        orow = QHBoxLayout()
        orow.setSpacing(4)
        labO = QLabel("外半径")
        labO.setMinimumWidth(_LABEL_W)
        orow.addWidget(labO)
        orow.addWidget(self._spinBulge, 1)
        wlay.addLayout(orow)
        # 倒角：类型（倒直角/圆角）+ 半径，同原版圆弧连接处圆角语义
        self._chamferTypeGroup = QButtonGroup(self)
        self._radioChamferStraight = QRadioButton("倒直角")
        self._radioChamferRound = QRadioButton("圆角")
        self._radioChamferRound.setChecked(True)
        self._chamferTypeGroup.addButton(self._radioChamferStraight, 0)
        self._chamferTypeGroup.addButton(self._radioChamferRound, 1)
        self._chamferTypeGroup.idClicked.connect(self.waveChamferTypeChanged.emit)
        tyrow = QHBoxLayout()
        tyrow.setSpacing(4)
        labTy = QLabel("倒角")
        labTy.setMinimumWidth(_LABEL_W)
        tyrow.addWidget(labTy)
        tyrow.addWidget(self._radioChamferStraight)
        tyrow.addWidget(self._radioChamferRound)
        tyrow.addStretch(1)
        wlay.addLayout(tyrow)
        self._spinWaveChamfer = self._spin(0.0, step=0.5)
        self._spinWaveChamfer.setMinimum(0.0)
        frow = QHBoxLayout()
        frow.setSpacing(4)
        labF = QLabel("倒角半径")
        labF.setMinimumWidth(_LABEL_W)
        frow.addWidget(labF)
        frow.addWidget(self._spinWaveChamfer, 1)
        wlay.addLayout(frow)
        hint = QLabel("拖动定大小（内=距离×0.6）；锁定后内外半径可继续调节")
        hint.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        hint.setWordWrap(True)
        wlay.addWidget(hint)
        self._waveWidget.hide()
        self._layout.addWidget(self._waveWidget)
        self._spinArcCount.valueChanged.connect(self.waveArcCountChanged.emit)
        self._spinWaveChamfer.valueChanged.connect(self.waveChamferChanged.emit)
        self._spinInner.valueChanged.connect(self._emitWaveRadius)
        self._spinBulge.valueChanged.connect(self._emitWaveRadius)

        btnOk = self._button("确认创建圆", primary=True)
        btnOk.clicked.connect(self.confirmed.emit)
        self._layout.addWidget(btnOk)
        self._layout.addStretch(1)

    def _onMode(self, mid: int) -> None:
        wave = mid == 1
        self._waveWidget.setVisible(wave)
        self._spinDia.setEnabled(not wave)
        self.waveModeChanged.emit(wave)
        self._notifyResize()

    def setDiameter(self, d: float) -> None:
        self._spinDia.blockSignals(True)
        self._spinDia.setValue(d)
        self._spinDia.blockSignals(False)

    def diameter(self) -> float:
        return self._spinDia.value()

    def _emitWaveRadius(self) -> None:
        # 程序回写（禁用态）不触发；仅用户调节时通知命令
        if self._spinInner.isEnabled() or self._spinBulge.isEnabled():
            self.waveRadiusChanged.emit()

    def waveType(self) -> int:
        return self._waveTypeGroup.checkedId()

    def setWaveRadii(self, inner: float, bulge: float) -> None:
        """回写内/外半径并启用调节（锁定后调用）。"""
        for s, v in ((self._spinInner, inner), (self._spinBulge, bulge)):
            s.blockSignals(True)
            s.setValue(v)
            s.blockSignals(False)
            s.setEnabled(True)

    def disableWaveRadii(self) -> None:
        self._spinInner.setEnabled(False)
        self._spinBulge.setEnabled(False)

    def innerRadius(self) -> float:
        return self._spinInner.value()

    def bulgeRadius(self) -> float:
        return self._spinBulge.value()


# ==================== Ellipse ====================

class EllipseParamPanel(ParamPanelBase):
    axesChanged = Signal(float, float)
    confirmed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._addCoordRows()
        self._spinMajor = self._spin(0.0, step=0.5)
        self._spinMajor.setMinimum(0.0)
        self._spinMinor = self._spin(0.0, step=0.5)
        self._spinMinor.setMinimum(0.0)
        self._spinMajor.valueChanged.connect(self._emitAxes)
        self._spinMinor.valueChanged.connect(self._emitAxes)
        self._addRow("长轴", self._spinMajor)
        self._addRow("短轴", self._spinMinor)
        btnOk = self._button("确认创建椭圆", primary=True)
        btnOk.clicked.connect(self.confirmed.emit)
        self._layout.addWidget(btnOk)
        self._addPlaceholder("波浪椭圆（待移植）")
        self._layout.addStretch(1)

    def _emitAxes(self) -> None:
        self.axesChanged.emit(self._spinMajor.value(), self._spinMinor.value())

    def setAxes(self, major: float, minor: float) -> None:
        for s, v in ((self._spinMajor, major), (self._spinMinor, minor)):
            s.blockSignals(True)
            s.setValue(v)
            s.blockSignals(False)


# ==================== Rectangle ====================

class RectangleParamPanel(ParamPanelBase):
    sizeChanged = Signal(float, float)      # 长/宽实时变化
    chamferTypeChanged = Signal(int)        # 0=无 1=圆角 2=斜角
    chamferRadiiChanged = Signal()          # 四角半径任一变化（同原版四角独立）
    confirmRequested = Signal()
    cancelRequested = Signal()
    waveEnabledToggled = Signal(bool)
    waveParamChanged = Signal()             # 波形/波数/高度任一变化

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._addCoordRows()
        for sp in (self._spinX, self._spinY):
            sp.setMinimumWidth(44)
            sp.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._spinLen = self._spin(0.0, step=0.5)
        self._spinLen.setMinimum(0.0)
        self._spinLen.setMinimumWidth(44)
        self._spinLen.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._spinWid = self._spin(0.0, step=0.5)
        self._spinWid.setMinimum(0.0)
        self._spinWid.setMinimumWidth(44)
        self._spinWid.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._spinLen.valueChanged.connect(self._emitSize)
        self._spinWid.valueChanged.connect(self._emitSize)
        # 长/宽标题可点击联动（同原版）：点长度 → 宽度=长度，方便画正方形；反之亦然
        self._labLen = _ClickLabel("长度", "点击：宽度 = 长度（快速画正方形）")
        self._labLen.setMinimumWidth(_LABEL_W)
        self._labLen.clicked.connect(self._onLenLabelClicked)
        rowLen = QHBoxLayout()
        rowLen.setSpacing(4)
        rowLen.addWidget(self._labLen)
        rowLen.addWidget(self._spinLen, 1)
        self._layout.addLayout(rowLen)
        self._labWid = _ClickLabel("宽度", "点击：长度 = 宽度（快速画正方形）")
        self._labWid.setMinimumWidth(_LABEL_W)
        self._labWid.clicked.connect(self._onWidLabelClicked)
        rowWid = QHBoxLayout()
        rowWid.setSpacing(4)
        rowWid.addWidget(self._labWid)
        rowWid.addWidget(self._spinWid, 1)
        self._layout.addLayout(rowWid)

        row = QHBoxLayout()
        row.setSpacing(4)
        self._chamferGroup = QButtonGroup(self)
        self._radioNone = QRadioButton("无倒角")
        self._radioFillet = QRadioButton("圆角")
        self._radioBevel = QRadioButton("斜角")
        self._radioNone.setChecked(True)
        self._chamferGroup.addButton(self._radioNone, 0)
        self._chamferGroup.addButton(self._radioFillet, 1)
        self._chamferGroup.addButton(self._radioBevel, 2)
        self._chamferGroup.idClicked.connect(self._onChamferType)
        row.addWidget(self._radioNone)
        row.addWidget(self._radioFillet)
        row.addWidget(self._radioBevel)
        row.addStretch(1)
        self._layout.addLayout(row)

        # 倒角半径：四角独立（同原版）+ 同半径联动开关；
        # 未启用倒角时整区禁用。
        urow = QHBoxLayout()
        urow.setSpacing(4)
        self._labRadius = QLabel("半径")
        self._labRadius.setFixedWidth(34)
        self._chkUniform = QCheckBox("同半径")
        self._chkUniform.setChecked(True)
        self._chkUniform.toggled.connect(self._onUniformToggled)
        urow.addWidget(self._labRadius)
        urow.addStretch(1)
        urow.addWidget(self._chkUniform)
        self._layout.addLayout(urow)
        self._cornerSpins = []
        for i in range(4):
            s = self._spin(0.0, step=0.5)
            s.setMinimum(0.0)
            s.setEnabled(False)
            s.valueChanged.connect(self._onCornerRadiusChanged)
            self._cornerSpins.append(s)
        self._layout.addLayout(self._wavePair("角1", self._cornerSpins[0],
                                              "角2", self._cornerSpins[1]))
        self._layout.addLayout(self._wavePair("角3", self._cornerSpins[2],
                                              "角4", self._cornerSpins[3]))

        # ---------- 波浪参数区（勾选展开，移植自原版） ----------
        self._chkWave = QCheckBox("启用波浪")
        self._chkWave.toggled.connect(self._onWaveToggled)
        self._layout.addWidget(self._chkWave)

        self._waveWidget = QWidget()
        wlay = QVBoxLayout(self._waveWidget)
        wlay.setContentsMargins(0, 0, 0, 0)
        wlay.setSpacing(5)
        trow = QHBoxLayout()
        trow.setSpacing(4)
        self._waveTypeGroup = QButtonGroup(self)
        self._radioWaveLine = QRadioButton("直线")
        self._radioWaveSmooth = QRadioButton("平滑")
        self._radioWaveArc = QRadioButton("圆弧")
        self._radioWaveSmooth.setChecked(True)
        self._waveTypeGroup.addButton(self._radioWaveLine, 0)
        self._waveTypeGroup.addButton(self._radioWaveSmooth, 1)
        self._waveTypeGroup.addButton(self._radioWaveArc, 2)
        self._waveTypeGroup.idClicked.connect(
            lambda _i: self.waveParamChanged.emit())
        labWave = QLabel("波形")
        labWave.setFixedWidth(34)
        trow.addWidget(labWave)
        trow.addWidget(self._radioWaveLine)
        trow.addWidget(self._radioWaveSmooth)
        trow.addWidget(self._radioWaveArc)
        trow.addStretch(1)
        wlay.addLayout(trow)
        self._spinLongCount = self._ispin(5, 1, 50)
        self._spinShortCount = self._ispin(3, 1, 50)
        wlay.addLayout(self._wavePair("长边", self._spinLongCount,
                                      "短边", self._spinShortCount))
        self._spinSideH = self._spin(3.0, step=0.5)
        self._spinSideH.setRange(0.0, 100.0)
        self._spinCornerH = self._spin(6.5, step=0.5)
        self._spinCornerH.setRange(0.0, 100.0)
        wlay.addLayout(self._wavePair("边半径", self._spinSideH,
                                      "角半径", self._spinCornerH))
        self._spinCornerW = self._spin(6.0, step=0.5)
        self._spinCornerW.setRange(0.0, 50.0)
        self._spinCornerChamfer = self._spin(0.0, step=0.5)
        self._spinCornerChamfer.setRange(0.0, 50.0)
        wlay.addLayout(self._wavePair("角波宽", self._spinCornerW,
                                      "倒角", self._spinCornerChamfer))
        for s in (self._spinLongCount, self._spinShortCount, self._spinSideH,
                  self._spinCornerH, self._spinCornerW):
            s.valueChanged.connect(lambda _v: self.waveParamChanged.emit())
        self._waveWidget.hide()
        self._layout.addWidget(self._waveWidget)

        brow = QGridLayout()
        brow.setContentsMargins(0, 0, 0, 0)
        brow.setHorizontalSpacing(6)
        btnOk = self._button("确认创建", primary=True)
        btnOk.setMinimumWidth(44)
        btnOk.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        btnOk.clicked.connect(self.confirmRequested.emit)
        btnCancel = self._button2("取消")
        btnCancel.setMinimumWidth(44)
        btnCancel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        btnCancel.clicked.connect(self.cancelRequested.emit)
        brow.addWidget(btnOk, 0, 0)
        brow.addWidget(btnCancel, 0, 1)
        brow.setColumnStretch(0, 1)
        brow.setColumnStretch(1, 1)
        self._layout.addLayout(brow)
        self._layout.addStretch(1)

    def _onWaveToggled(self, on: bool) -> None:
        self._waveWidget.setVisible(on)
        self.waveEnabledToggled.emit(on)
        self._notifyResize()

    def _wavePair(self, lab1: str, w1: QWidget, lab2: str,
                  w2: QWidget) -> QGridLayout:
        """双列紧凑行：标签固定窄宽、两个输入框各占一列拉伸。

        以前用 QHBoxLayout + QDoubleSpinBox 默认 sizeHint（≈172px），
        两个并排最小宽 ≈428px，远超面板 258px 可视宽 → 撑出横向滚动条。
        改为网格 + 显式压缩下限后，宽度恒在面板内，且左右两列等宽。"""
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(4)
        l1, l2 = QLabel(lab1), QLabel(lab2)
        # 定宽放得下 3 个汉字（原 34 会把"边半径"截成"边半…"）；多出的宽度
        # 从两侧输入框各匀一点（输入框仍 ≥44，够显示 "0.00" + 箭头）
        for lb in (l1, l2):
            lb.setFixedWidth(46)
        for w in (w1, w2):
            w.setMinimumWidth(44)
            w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        grid.addWidget(l1, 0, 0)
        grid.addWidget(w1, 0, 1)
        grid.addWidget(l2, 0, 2)
        grid.addWidget(w2, 0, 3)
        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 0)
        grid.setColumnStretch(3, 1)
        return grid

    def waveEnabled(self) -> bool:
        return self._chkWave.isChecked()

    def waveType(self) -> int:
        return self._waveTypeGroup.checkedId()

    def longWaveCount(self) -> int:
        return self._spinLongCount.value()

    def shortWaveCount(self) -> int:
        return self._spinShortCount.value()

    def sideWaveHeight(self) -> float:
        return self._spinSideH.value()

    def cornerWaveHeight(self) -> float:
        return self._spinCornerH.value()

    def cornerWaveWidth(self) -> float:
        return self._spinCornerW.value()

    def cornerChamfer(self) -> float:
        return self._spinCornerChamfer.value()

    def _emitSize(self) -> None:
        self.sizeChanged.emit(self._spinLen.value(), self._spinWid.value())

    def _onLenLabelClicked(self) -> None:
        # 点长度标题 → 宽度 = 长度（同原版 onLengthLabelClicked）
        self._spinWid.setValue(self._spinLen.value())
        self._emitSize()

    def _onWidLabelClicked(self) -> None:
        # 点宽度标题 → 长度 = 宽度（同原版 onWidthLabelClicked）
        self._spinLen.setValue(self._spinWid.value())
        self._emitSize()

    def _onChamferType(self, cid: int) -> None:
        self._setRadiiEnabled(cid > 0)
        self._labRadius.setText("距离" if cid == 2 else "半径")
        self.chamferTypeChanged.emit(cid)

    def _setRadiiEnabled(self, on: bool) -> None:
        for s in self._cornerSpins:
            s.setEnabled(on)
        self._chkUniform.setEnabled(on)

    def _onUniformToggled(self, on: bool) -> None:
        if on:
            # 勾上同半径：以角1为准同步其余三角（同原版）
            v = self._cornerSpins[0].value()
            for s in self._cornerSpins[1:]:
                s.blockSignals(True)
                s.setValue(v)
                s.blockSignals(False)
            self.chamferRadiiChanged.emit()

    def _onCornerRadiusChanged(self, _v: float) -> None:
        if self._chkUniform.isChecked():
            sender = self.sender()
            v = sender.value() if sender is not None else 0.0
            for s in self._cornerSpins:
                if s is not sender:
                    s.blockSignals(True)
                    s.setValue(v)
                    s.blockSignals(False)
        self.chamferRadiiChanged.emit()

    def setSize(self, length: float, width: float) -> None:
        for s, v in ((self._spinLen, length), (self._spinWid, width)):
            s.blockSignals(True)
            s.setValue(v)
            s.blockSignals(False)

    def length(self) -> float:
        return self._spinLen.value()

    def width(self) -> float:
        return self._spinWid.value()

    def setCornerRadius(self, index: int, r: float) -> None:
        """程序回写单角半径（不触发联动信号）。"""
        if 0 <= index < 4:
            s = self._cornerSpins[index]
            s.blockSignals(True)
            s.setValue(r)
            s.blockSignals(False)

    def setChamferRadii(self, radii) -> None:
        for i, r in enumerate(list(radii)[:4]):
            self.setCornerRadius(i, r)

    def cornerRadii(self) -> list:
        return [s.value() for s in self._cornerSpins]

    def setChamferRadius(self, r: float) -> None:
        self.setChamferRadii([r] * 4)

    def chamferRadius(self) -> float:
        return self._cornerSpins[0].value()

    def setChamferType(self, cid: int) -> None:
        btn = self._chamferGroup.button(cid)
        if btn is not None and not btn.isChecked():
            btn.setChecked(True)
            self._onChamferType(cid)

    def chamferType(self) -> int:
        return self._chamferGroup.checkedId()


# ==================== Polygon ====================

class PolygonParamPanel(ParamPanelBase):
    centerChanged = Signal(float, float)
    sidesChanged = Signal(int)
    radiusChanged = Signal(float)
    starModeChanged = Signal(bool)
    innerRadiusChanged = Signal(float)
    filletRadiusChanged = Signal(float)
    confirmed = Signal()
    cancelRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._addCoordRows()
        self._spinX.valueChanged.connect(self._emitCenter)
        self._spinY.valueChanged.connect(self._emitCenter)

        self._spinSides = self._ispin(6, 3, 64)
        self._spinSides.valueChanged.connect(self.sidesChanged.emit)
        self._addRow("边数", self._spinSides)

        self._spinRadius = self._spin(25.0, step=0.5)
        self._spinRadius.setMinimum(0.01)
        self._spinRadius.valueChanged.connect(self.radiusChanged.emit)
        self._addRow("外径", self._spinRadius)

        self._chkStar = QCheckBox("星型")
        self._chkStar.toggled.connect(self._onStarToggled)
        self._layout.addWidget(self._chkStar)

        self._spinInner = self._spin(12.5, step=0.5)
        self._spinInner.setMinimum(0.01)
        self._spinInner.setEnabled(False)
        self._spinInner.valueChanged.connect(self.innerRadiusChanged.emit)
        self._addRow("内径", self._spinInner)

        self._spinFillet = self._spin(0.0, step=0.5)
        self._spinFillet.setMinimum(0.0)
        self._spinFillet.valueChanged.connect(self.filletRadiusChanged.emit)
        self._addRow("倒角半径", self._spinFillet)

        brow = QHBoxLayout()
        btnOk = self._button("确认创建", primary=True)
        btnOk.clicked.connect(self.confirmed.emit)
        btnCancel = self._button2("取消")
        btnCancel.clicked.connect(self.cancelRequested.emit)
        brow.addWidget(btnOk)
        brow.addWidget(btnCancel)
        self._layout.addLayout(brow)
        self._layout.addStretch(1)

    def _emitCenter(self) -> None:
        self.centerChanged.emit(self.x(), self.y())

    def _onStarToggled(self, on: bool) -> None:
        self._spinInner.setEnabled(on)
        self.starModeChanged.emit(on)

    def setCenter(self, x: float, y: float) -> None:
        for s, v in ((self._spinX, x), (self._spinY, y)):
            s.blockSignals(True)
            s.setValue(v)
            s.blockSignals(False)

    def setSides(self, n: int) -> None:
        self._spinSides.blockSignals(True)
        self._spinSides.setValue(n)
        self._spinSides.blockSignals(False)

    def setRadius(self, r: float) -> None:
        self._spinRadius.blockSignals(True)
        self._spinRadius.setValue(r)
        self._spinRadius.blockSignals(False)

    def setInnerRadius(self, r: float) -> None:
        self._spinInner.blockSignals(True)
        self._spinInner.setValue(r)
        self._spinInner.blockSignals(False)

    def setFilletRadius(self, r: float) -> None:
        self._spinFillet.blockSignals(True)
        self._spinFillet.setValue(r)
        self._spinFillet.blockSignals(False)

    def setStarMode(self, star: bool) -> None:
        self._chkStar.blockSignals(True)
        self._chkStar.setChecked(star)
        self._spinInner.setEnabled(star)
        self._chkStar.blockSignals(False)


# ==================== Chamfer ====================

class ChamferParamPanel(ParamPanelBase):
    modeChanged = Signal(int)            # 0=圆角 1=斜角
    radiusChanged = Signal(float)
    distanceChanged = Signal(float, float)
    selectModeChanged = Signal(int)      # 0=单角 1=整线
    confirmed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        row = QHBoxLayout()
        self._modeGroup = QButtonGroup(self)
        self._radioFillet = QRadioButton("圆角")
        self._radioBevel = QRadioButton("斜角")
        self._radioFillet.setChecked(True)
        self._modeGroup.addButton(self._radioFillet, 0)
        self._modeGroup.addButton(self._radioBevel, 1)
        self._modeGroup.idClicked.connect(self._onMode)
        row.addWidget(self._radioFillet)
        row.addWidget(self._radioBevel)
        self._layout.addLayout(row)

        self._spinRadius = self._spin(5.0, step=0.5)
        self._spinRadius.setMinimum(0.0)
        self._spinRadius.valueChanged.connect(self.radiusChanged.emit)
        self._addRow("半径", self._spinRadius)

        self._spinD1 = self._spin(5.0, step=0.5)
        self._spinD1.setMinimum(0.0)
        self._spinD2 = self._spin(5.0, step=0.5)
        self._spinD2.setMinimum(0.0)
        self._spinD1.valueChanged.connect(self._emitDist)
        self._spinD2.valueChanged.connect(self._emitDist)
        self._addRow("距离 1", self._spinD1)
        self._addRow("距离 2", self._spinD2)
        self._spinD1.setEnabled(False)
        self._spinD2.setEnabled(False)

        srow = QHBoxLayout()
        self._selGroup = QButtonGroup(self)
        self._radioSingle = QRadioButton("单角")
        self._radioWhole = QRadioButton("整线")
        self._radioSingle.setChecked(True)
        self._selGroup.addButton(self._radioSingle, 0)
        self._selGroup.addButton(self._radioWhole, 1)
        self._selGroup.idClicked.connect(self.selectModeChanged.emit)
        srow.addWidget(QLabel("选择"))
        srow.addWidget(self._radioSingle)
        srow.addWidget(self._radioWhole)
        self._layout.addLayout(srow)

        btnOk = self._button("确认执行", primary=True)
        btnOk.clicked.connect(self.confirmed.emit)
        self._layout.addWidget(btnOk)
        self._layout.addStretch(1)

    def _emitDist(self) -> None:
        self.distanceChanged.emit(self._spinD1.value(), self._spinD2.value())

    def _onMode(self, mid: int) -> None:
        bevel = mid == 1
        self._spinRadius.setEnabled(not bevel)
        self._spinD1.setEnabled(bevel)
        self._spinD2.setEnabled(bevel)
        self.modeChanged.emit(mid)

    def setMode(self, mode: int) -> None:
        """程序回写模式（0=圆角 1=斜角），不触发信号。"""
        for rb, mid in ((self._radioFillet, 0), (self._radioBevel, 1)):
            rb.blockSignals(True)
            rb.setChecked(mid == mode)
            rb.blockSignals(False)
        bevel = mode == 1
        self._spinRadius.setEnabled(not bevel)
        self._spinD1.setEnabled(bevel)
        self._spinD2.setEnabled(bevel)

    def setRadius(self, r: float) -> None:
        self._spinRadius.blockSignals(True)
        self._spinRadius.setValue(r)
        self._spinRadius.blockSignals(False)

    def setDistances(self, d1: float, d2: float) -> None:
        for s, v in ((self._spinD1, d1), (self._spinD2, d2)):
            s.blockSignals(True)
            s.setValue(v)
            s.blockSignals(False)


# ==================== Bridge 桥接 ====================

class BridgeParamPanel(ParamPanelBase):
    """桥接参数：桥宽（沿轮廓保留料桥的长度）。"""

    widthChanged = Signal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._spinWidth = self._spin(2.0, step=0.5)
        self._spinWidth.setMinimum(0.05)
        self._spinWidth.valueChanged.connect(self.widthChanged.emit)
        self._addRow("桥宽", self._spinWidth)
        tip = QLabel("点两点画定位线跨越内外轮廓，在交点处\n按桥宽截断并相连（桥宽=缺口宽度）")
        tip.setStyleSheet("color:#888888;")
        tip.setWordWrap(True)
        self._layout.addWidget(tip)
        self._layout.addStretch(1)

    def setBridgeWidth(self, w: float) -> None:
        """程序回写桥宽，不触发信号。"""
        self._spinWidth.blockSignals(True)
        self._spinWidth.setValue(w)
        self._spinWidth.blockSignals(False)


# ==================== PushPull 推拉（随选择类型切换） ====================

class PushPullParamPanel(ParamPanelBase):
    """推拉命令参数面板：选边 = 倒角页（圆角/斜角），选面 = 面编辑页
    （推拉 = 挤出/挖孔，缩放 = 锥度扩收面/倒角面调整/改半径）。

    命令在选到边/面时调 setContext() 切页并给操作提示，两页共用
    底部"确认执行"按钮；数值修改实时发信号驱动预览。
    """
    filletModeChanged = Signal(int)         # 0=圆角 1=斜角
    radiusChanged = Signal(float)
    faceModeChanged = Signal(int)           # 0=推拉 1=缩放
    faceDirChanged = Signal(int)            # 0=添加(挤出) 1=切除(挖孔)
    faceValueChanged = Signal(float)        # 推拉距离/缩放增量（带符号）
    confirmed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._ctx = None
        self._pages = QStackedWidget()
        self._layout.addWidget(self._pages)

        # ---- 页 0：边 = 倒角 ----
        edge = QWidget()
        el = QVBoxLayout(edge)
        el.setContentsMargins(0, 0, 0, 0)
        el.setSpacing(5)
        row = QHBoxLayout()
        self._modeGroup = QButtonGroup(self)
        self._radioFillet = QRadioButton("圆角")
        self._radioBevel = QRadioButton("斜角")
        self._radioFillet.setChecked(True)
        self._modeGroup.addButton(self._radioFillet, 0)
        self._modeGroup.addButton(self._radioBevel, 1)
        self._modeGroup.idClicked.connect(self._onEdgeMode)
        row.addWidget(self._radioFillet)
        row.addWidget(self._radioBevel)
        row.addStretch(1)
        el.addLayout(row)
        self._spinRadius = self._spin(5.0, step=0.5)
        self._spinRadius.setMinimum(0.0)
        self._spinRadius.valueChanged.connect(self.radiusChanged.emit)
        vr = QHBoxLayout()
        vr.setSpacing(4)
        lab = QLabel("半径")
        lab.setMinimumWidth(_LABEL_W)
        vr.addWidget(lab)
        vr.addWidget(self._spinRadius, 1)
        el.addLayout(vr)
        hint = QLabel("拖出手柄 = 圆角，拖入 = 斜角")
        hint.setWordWrap(True)
        el.addWidget(hint)
        self._pages.addWidget(edge)

        # ---- 页 1：面 = 推拉 / 缩放 ----
        face = QWidget()
        fl = QVBoxLayout(face)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(5)

        fm = QHBoxLayout()
        self._faceModeGroup = QButtonGroup(self)
        self._radioPush = QRadioButton("推拉")
        self._radioScale = QRadioButton("缩放")
        self._radioPush.setChecked(True)
        self._faceModeGroup.addButton(self._radioPush, 0)
        self._faceModeGroup.addButton(self._radioScale, 1)
        self._faceModeGroup.idClicked.connect(self._onFaceMode)
        labm = QLabel("模式")
        labm.setMinimumWidth(_LABEL_W)
        fm.addWidget(labm)
        fm.addWidget(self._radioPush)
        fm.addWidget(self._radioScale)
        fm.addStretch(1)
        fl.addLayout(fm)

        self._dirRow = QHBoxLayout()
        self._dirGroup = QButtonGroup(self)
        self._radioAdd = QRadioButton("添加(挤出)")
        self._radioCut = QRadioButton("切除(挖孔)")
        self._radioAdd.setChecked(True)
        self._dirGroup.addButton(self._radioAdd, 0)
        self._dirGroup.addButton(self._radioCut, 1)
        self._dirGroup.idClicked.connect(self._onDirClicked)
        labd = QLabel("方向")
        labd.setMinimumWidth(_LABEL_W)
        self._dirRow.addWidget(labd)
        self._dirRow.addWidget(self._radioAdd)
        self._dirRow.addWidget(self._radioCut)
        self._dirRow.addStretch(1)
        fl.addLayout(self._dirRow)

        vr2 = QHBoxLayout()
        vr2.setSpacing(4)
        self._labVal = QLabel("距离")
        self._labVal.setMinimumWidth(_LABEL_W)
        self._spinVal = self._spin(5.0, step=0.5)
        self._spinVal.setMinimum(0.0)
        self._spinVal.valueChanged.connect(self._emitFaceValue)
        vr2.addWidget(self._labVal)
        vr2.addWidget(self._spinVal, 1)
        fl.addLayout(vr2)

        self._hintFace = QLabel("点击实体面开始：推拉=挤出/挖孔，缩放=扩收面/"
                                "调整倒角")
        self._hintFace.setWordWrap(True)
        fl.addWidget(self._hintFace)
        self._pages.addWidget(face)

        # 默认显示面页（推拉为主操作、命令默认拾取面）；选到边时自动切倒角页
        self._pages.setCurrentIndex(1)
        self._ctx = "face"

        btnOk = self._button("确认执行", primary=True)
        btnOk.clicked.connect(self.confirmed.emit)
        self._layout.addWidget(btnOk)
        self._layout.addStretch(1)

    # ---------- 边页 ----------

    def _onEdgeMode(self, mid: int) -> None:
        self._spinRadius.setEnabled(mid == 0)
        self.filletModeChanged.emit(mid)

    # ---------- 面页 ----------

    def _onFaceMode(self, mid: int) -> None:
        self._updateFaceRows()
        self.faceModeChanged.emit(mid)
        self._emitFaceValue()

    def _onDirClicked(self, did: int) -> None:
        self.faceDirChanged.emit(did)
        self._emitFaceValue()

    def _emitFaceValue(self) -> None:
        v = self._spinVal.value()
        if (self._faceModeGroup.checkedId() == 0
                and self._dirGroup.checkedId() == 1):
            v = -v
        self.faceValueChanged.emit(v)

    def _updateFaceRows(self) -> None:
        """推拉 = 方向行可见 + 距离≥0；缩放 = 隐藏方向行 + 带符号增量。"""
        push = self._faceModeGroup.checkedId() == 0
        for i in range(self._dirRow.count()):
            w = self._dirRow.itemAt(i).widget()
            if w is not None:
                w.setVisible(push)
        self._labVal.setText("距离" if push else "增量")
        v = self._spinVal.value()
        self._spinVal.blockSignals(True)
        self._spinVal.setMinimum(0.0 if push else -_SPIN_RANGE[1])
        self._spinVal.setValue(v if push else abs(v))
        self._spinVal.blockSignals(False)

    # ---------- 程序回写（阻断信号防回环） ----------

    def setContext(self, kind: str, hint: str = "") -> None:
        """切换面板上下文：edge=倒角页，face=面编辑页（hint 为操作提示）。"""
        if self._ctx != kind:
            self._ctx = kind
            self._pages.setCurrentIndex(0 if kind == "edge" else 1)
            self._notifyResize()
        if kind == "face" and hint and hint != self._hintFace.text():
            self._hintFace.setText(hint)

    def setEdgeMode(self, mode: int) -> None:
        for rb, mid in ((self._radioFillet, 0), (self._radioBevel, 1)):
            rb.blockSignals(True)
            rb.setChecked(mid == mode)
            rb.blockSignals(False)
        self._spinRadius.setEnabled(mode == 0)

    def setRadius(self, r: float) -> None:
        self._spinRadius.blockSignals(True)
        self._spinRadius.setValue(r)
        self._spinRadius.blockSignals(False)

    def setFaceMode(self, mode: int) -> None:
        for rb, mid in ((self._radioPush, 0), (self._radioScale, 1)):
            rb.blockSignals(True)
            rb.setChecked(mid == mode)
            rb.blockSignals(False)
        self._updateFaceRows()

    def setFaceDir(self, d: int) -> None:
        for rb, did in ((self._radioAdd, 0), (self._radioCut, 1)):
            rb.blockSignals(True)
            rb.setChecked(did == d)
            rb.blockSignals(False)

    def setFaceValue(self, v: float) -> None:
        """推拉=符号写方向钮+幅值；缩放=带符号直接写。"""
        if self._faceModeGroup.checkedId() == 0:
            self.setFaceDir(0 if v >= 0.0 else 1)
            v = abs(v)
        self._spinVal.blockSignals(True)
        self._spinVal.setValue(v)
        self._spinVal.blockSignals(False)


# ==================== Boolean3D 布尔 ====================

class BooleanParamPanel(ParamPanelBase):
    """3D 布尔面板：并/差/交切换 + 对调 A/B + 保留工具体 + 确认。"""
    opChanged = Signal(int)        # 0=并 1=差 2=交
    swapRequested = Signal()       # 对调基体/工具体（差集方向调转）
    keepToolChanged = Signal(bool) # True=应用后保留工具体（B）
    confirmed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        row = QHBoxLayout()
        self._opGroup = QButtonGroup(self)
        self._radioUnion = QRadioButton("并集")
        self._radioCut = QRadioButton("差集")
        self._radioCommon = QRadioButton("交集")
        self._radioUnion.setChecked(True)
        for rb, oid in ((self._radioUnion, 0), (self._radioCut, 1),
                        (self._radioCommon, 2)):
            self._opGroup.addButton(rb, oid)
        self._opGroup.idClicked.connect(self.opChanged.emit)
        row.addWidget(self._radioUnion)
        row.addWidget(self._radioCut)
        row.addWidget(self._radioCommon)
        self._layout.addLayout(row)

        btnSwap = self._button2("对调 A/B")
        btnSwap.setToolTip("交换基体与工具体（差集方向调转；已拖动的位置保留）")
        btnSwap.clicked.connect(self.swapRequested.emit)
        self._layout.addWidget(btnSwap)

        self._chkKeep = QCheckBox("保留工具体")
        self._chkKeep.setToolTip("应用后不删除第二个实体（默认删除）")
        self._chkKeep.toggled.connect(self.keepToolChanged.emit)
        self._layout.addWidget(self._chkKeep)

        btnOk = self._button("确认执行", primary=True)
        btnOk.clicked.connect(self.confirmed.emit)
        self._layout.addWidget(btnOk)
        self._layout.addStretch(1)

    def setOp(self, op: int) -> None:
        """程序回写操作（0/1/2，键盘 1/2/3 切换时），不触发信号。"""
        for rb, oid in ((self._radioUnion, 0), (self._radioCut, 1),
                        (self._radioCommon, 2)):
            rb.blockSignals(True)
            rb.setChecked(oid == op)
            rb.blockSignals(False)


# ==================== Wall 墙线 ====================

class WallParamPanel(ParamPanelBase):
    """墙线面板：厚度 + 高度，实时预览；右键/回车确认生成（无生成按钮）。"""

    thicknessChanged = Signal(float)
    heightChanged = Signal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        gP, lP = self._group("墙体参数")
        self._spinT = self._spin(0.8, decimals=2, step=0.1)
        self._spinT.setRange(0.01, 99999.0)
        lP.addWidget(self._row("厚度", self._spinT))
        self._spinH = self._spin(5.0, decimals=2, step=1.0)
        self._spinH.setRange(0.01, 99999.0)
        lP.addWidget(self._row("高度", self._spinH))
        tip = QLabel("选中任意曲线（直线/圆弧/样条/圆），\n"
                     "两侧偏移成墙厚并挤出高度；\n"
                     "右键/回车确认生成")
        tip.setStyleSheet("color:%s; font-size:9pt;" % theme.TEXT_DIM)
        lP.addWidget(tip)
        self._layout.addWidget(gP)
        self._layout.addStretch(1)

        self._spinT.valueChanged.connect(self.thicknessChanged.emit)
        self._spinH.valueChanged.connect(self.heightChanged.emit)

    def _group(self, title: str):
        g = QGroupBox(title)
        lay = QVBoxLayout(g)
        lay.setSpacing(3)
        lay.setContentsMargins(6, 4, 6, 4)
        return g, lay

    def _row(self, label: str, widget) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lab = QLabel(label)
        lab.setMinimumWidth(_LABEL_W)
        lay.addWidget(lab)
        lay.addWidget(widget, 1)
        return row

    def setThickness(self, v: float) -> None:
        self._spinT.blockSignals(True)
        self._spinT.setValue(v)
        self._spinT.blockSignals(False)

    def setHeight(self, v: float) -> None:
        self._spinH.blockSignals(True)
        self._spinH.setValue(v)
        self._spinH.blockSignals(False)


# ==================== ClosePath 闭合 ====================

class CloseParamPanel(ParamPanelBase):
    """闭合面板：为选中的开放曲线选择首尾连接方式。

    信号：
    - methodChanged(int): 连接方式切换（0=直线连接 1=圆弧连接 2=平滑曲线）
    - confirmed(): 点击"执行闭合"
    - cancelRequested(): 点击"取消"
    """
    methodChanged = Signal(int)
    confirmed = Signal()
    cancelRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        row = QHBoxLayout()
        self._group = QButtonGroup(self)
        for i, label in enumerate(("直线连接", "圆弧连接", "平滑曲线")):
            rb = QRadioButton(label)
            rb.setChecked(i == 0)
            self._group.addButton(rb, i)
            row.addWidget(rb)
        self._group.idClicked.connect(self._onMethod)
        self._layout.addLayout(row)

        btnRow = QHBoxLayout()
        btnOk = self._button("执行闭合", primary=True)
        btnOk.clicked.connect(self.confirmed.emit)
        btnCancel = self._button2("取消")
        btnCancel.clicked.connect(self.cancelRequested.emit)
        btnRow.addWidget(btnOk)
        btnRow.addWidget(btnCancel)
        self._layout.addLayout(btnRow)
        self._layout.addStretch(1)

    def _onMethod(self, mid: int) -> None:
        self.methodChanged.emit(mid)


# ==================== NewModel（新建浮雕模型） ====================

class NewModelParamPanel(ParamPanelBase):
    """新建浮雕模型面板：按选中曲线包围盒 + 余量 + 精度建高度场。

    只读显示包围盒尺寸；余量/精度/基面高度可调，实时预估栅格与内存，
    超过内存钳制上限时红色告警（与 HeightField.from_bbox 同一套逻辑）。
    """
    confirmed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._bboxW = 0.0
        self._bboxH = 0.0

        self._labBBox = self._label("-- × -- mm")
        self._addRow("包围盒", self._labBBox)

        self._spinMargin = self._spin(5.0, decimals=2, step=0.5)
        self._spinMargin.setRange(0.0, 100.0)
        self._spinMargin.valueChanged.connect(lambda _v: self._updateEstimate())
        self._addRow("余量", self._spinMargin)

        self._spinSpacing = self._spin(0.1, decimals=3, step=0.01)
        self._spinSpacing.setRange(0.001, 100.0)
        self._spinSpacing.valueChanged.connect(lambda _v: self._updateEstimate())
        self._addRow("精度", self._spinSpacing)

        self._spinBase = self._spin(0.0, decimals=2, step=0.5)
        self._addRow("基面高", self._spinBase)

        hint = QLabel("精度 = mm/像素（越小越细）；余量为四周额外材料")
        hint.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        hint.setWordWrap(True)
        self._layout.addWidget(hint)

        self._labEstimate = self._label("")
        self._labEstimate.setWordWrap(True)
        self._layout.addWidget(self._labEstimate)

        btnOk = self._button("确认创建", primary=True)
        btnOk.clicked.connect(self.confirmed.emit)
        self._layout.addWidget(btnOk)
        self._layout.addStretch(1)

    def setBBoxSize(self, w: float, h: float) -> None:
        """回填选中曲线包围盒尺寸（mm）并刷新预估。"""
        self._bboxW = float(w)
        self._bboxH = float(h)
        self._labBBox.setText(f"{w:.2f} × {h:.2f} mm")
        self._updateEstimate()

    def _updateEstimate(self) -> None:
        width = self._bboxW + 2.0 * self._spinMargin.value()
        height = self._bboxH + 2.0 * self._spinMargin.value()
        nx, ny, sp, clamped = HeightField.grid_size(
            width, height, self._spinSpacing.value())
        mb = nx * ny * 4 / (1024.0 * 1024.0)
        text = f"栅格 {nx} × {ny}    内存 {mb:.1f} MB"
        if clamped:
            text += f"\n⚠ 超内存上限，精度已自动加粗至 {sp:.4f} mm/px"
            self._labEstimate.setStyleSheet(
                "color:#e53935; font-weight:bold;")
        else:
            self._labEstimate.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        self._labEstimate.setText(text)

    def margin(self) -> float:
        return self._spinMargin.value()

    def spacing(self) -> float:
        return self._spinSpacing.value()

    def baseHeight(self) -> float:
        return self._spinBase.value()


# ==================== ReconstructRelief（重构浮雕网格） ====================

class ReconstructParamPanel(ParamPanelBase):
    """重构浮雕网格面板：把放置变换烘焙进网格 + 四周边界余量。

    只读显示当前世界尺寸与栅格；精度可调，“贴合当前世界尺寸”勾选时用当前
    （变换后）尺寸，取消勾选可输入新尺寸（网格按新尺寸拉伸重采样）；
    实时预估栅格与内存，超内存钳制上限时红色告警（与 HeightField.grid_size 同逻辑）。
    """
    confirmed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._curW = 0.0
        self._curH = 0.0

        self._labSize = self._label("-- × -- mm")
        self._addRow("当前尺寸", self._labSize)
        self._labGrid = self._label("-- × --")
        self._addRow("当前栅格", self._labGrid)

        self._spinSpacing = self._spin(0.1, decimals=3, step=0.01)
        self._spinSpacing.setRange(0.001, 100.0)
        self._spinSpacing.valueChanged.connect(lambda _v: self._updateEstimate())
        self._addRow("精度", self._spinSpacing)

        self._spinMargin = self._spin(5.0, decimals=2, step=0.5)
        self._spinMargin.setRange(0.0, 100.0)
        self._spinMargin.valueChanged.connect(lambda _v: self._updateEstimate())
        self._addRow("边界余量", self._spinMargin)

        hint = QLabel("重构 = 把移动/缩放/旋转烘焙进网格并复位（唯一有损重采样）；"
                      "精度 = mm/像素（越小越细）；边界余量为四周额外材料。")
        hint.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        hint.setWordWrap(True)
        self._layout.addWidget(hint)

        self._labEstimate = self._label("")
        self._labEstimate.setWordWrap(True)
        self._layout.addWidget(self._labEstimate)

        btnOk = self._button("确认重构", primary=True)
        btnOk.clicked.connect(self.confirmed.emit)
        self._layout.addWidget(btnOk)
        self._layout.addStretch(1)

    def setModelInfo(self, curW: float, curH: float, nx: int, ny: int,
                     spacing: float) -> None:
        """回填当前模型的世界尺寸/栅格/精度并刷新预估。"""
        self._curW = float(curW)
        self._curH = float(curH)
        self._labSize.setText(f"{curW:.2f} × {curH:.2f} mm")
        self._labGrid.setText(f"{nx} × {ny}  @ {spacing:.4f} mm/px")
        self._spinSpacing.blockSignals(True)
        self._spinSpacing.setValue(float(spacing))
        self._spinSpacing.blockSignals(False)
        self._updateEstimate()

    def _updateEstimate(self) -> None:
        m = self._spinMargin.value()
        w, h = self._curW + 2.0 * m, self._curH + 2.0 * m
        if w <= 0 or h <= 0:
            self._labEstimate.setText("尺寸无效")
            return
        nx, ny, sp, clamped = HeightField.grid_size(w, h, self._spinSpacing.value())
        mb = nx * ny * 4 / (1024.0 * 1024.0)
        text = f"栅格 {nx} × {ny}    内存 {mb:.1f} MB"
        if clamped:
            text += f"\n⚠ 超内存上限，精度已自动加粗至 {sp:.4f} mm/px"
            self._labEstimate.setStyleSheet("color:#e53935; font-weight:bold;")
        else:
            self._labEstimate.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        self._labEstimate.setText(text)

    def spacing(self) -> float:
        return self._spinSpacing.value()

    def margin(self) -> float:
        return self._spinMargin.value()


# ==================== RegionRelief（区域浮雕） ====================

class RegionReliefParamPanel(ParamPanelBase):
    """区域浮雕参数面板（复刻 Rhino 插件"曲面区域"页）。

    分组与插件一致：截面类型 / 浮雕参数 / 表面特点 / 拼合方式。

    参数可用性随截面类型联动（对齐插件 RefreshParamAvailability）：
      - 矩形：只用基高 + 基角，边界/强度/高度置灰
      - 倒角：隐藏"自由高度 / 局部等高"、显示"外凸角 / 内凹角"，表面特点置灰
      - 其余（椭圆/三角/圆形/样条）：边界/强度可用；高度仅在"局部等高"下可用
    """

    finished = Signal()         # 点击"完成"（结束命令）
    paramsChanged = Signal()    # 任一参数变更（命令据此刷新提示）

    # 截面按钮 id 与 SHAPES 顺序一致：椭圆/矩形/三角/圆形/样条/倒角
    _SHAPE_LABELS = ("椭圆", "矩形", "三角", "圆形", "样条", "倒角")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        # ---- 截面类型（2 排 × 3 个）----
        self._shapeGroup = QButtonGroup(self)
        gShape, lShape = self._group("截面类型")
        lShape.addWidget(self._radioRow("", self._SHAPE_LABELS[0:3],
                                        self._shapeGroup, 0))
        lShape.addWidget(self._radioRow("", self._SHAPE_LABELS[3:6],
                                        self._shapeGroup, 3))
        self._shapeGroup.button(0).setChecked(True)
        self._shapeGroup.idClicked.connect(self._onShapeChanged)
        self._layout.addWidget(gShape)

        # ---- 浮雕参数 ----
        gRelief, lRelief = self._group("浮雕参数")

        self._spinBaseH = self._spin(3.0, decimals=1, step=0.5)
        self._spinBaseH.setRange(0.0, 50.0)
        lRelief.addWidget(self._row("基高", self._spinBaseH))

        self._spinBaseA = self._spin(80.0, decimals=0, step=1.0)
        self._spinBaseA.setRange(10.0, 90.0)
        lRelief.addWidget(self._row("基角", self._spinBaseA))

        lRelief.addWidget(self._divider())

        # 高度模式（倒角时隐藏）
        self._modeGroup = QButtonGroup(self)
        self._rowMode = self._radioRow("", ("自由高度", "局部等高"),
                                       self._modeGroup, 0)
        self._modeGroup.button(0).setChecked(True)
        self._modeGroup.idClicked.connect(self._onModeChanged)
        lRelief.addWidget(self._rowMode)

        # 倒角模式（仅倒角显示）
        self._chamferGroup = QButtonGroup(self)
        self._rowChamfer = self._radioRow("", ("外凸角", "内凹角"),
                                          self._chamferGroup, 0)
        self._chamferGroup.button(0).setChecked(True)
        self._chamferGroup.idClicked.connect(lambda *_: self.paramsChanged.emit())
        lRelief.addWidget(self._rowChamfer)

        lRelief.addWidget(self._divider())

        self._spinFreeH = self._spin(3.0, decimals=1, step=0.5)
        self._spinFreeH.setRange(0.1, 50.0)
        self._rowFreeH = self._row("高度", self._spinFreeH)
        lRelief.addWidget(self._rowFreeH)

        self._spinBorder = self._spin(45.0, decimals=0, step=1.0)
        self._spinBorder.setRange(1.0, 89.0)
        self._rowBorder = self._row("边界", self._spinBorder)
        lRelief.addWidget(self._rowBorder)

        self._spinStrength = self._spin(1.0, decimals=2, step=0.1)
        self._spinStrength.setRange(0.1, 2.0)
        self._rowStrength = self._row("强度", self._spinStrength)
        lRelief.addWidget(self._rowStrength)
        self._layout.addWidget(gRelief)

        # ---- 表面特点 ----
        self._finishGroup = QButtonGroup(self)
        gFinish, lFinish = self._group("表面特点")
        lFinish.addWidget(self._radioRow("", ("整体光滑", "棱角清晰"),
                                         self._finishGroup, 0))
        self._finishGroup.button(0).setChecked(True)
        self._finishGroup.idClicked.connect(lambda *_: self.paramsChanged.emit())
        self._layout.addWidget(gFinish)

        # ---- 拼合方式 ----
        self._blendGroup = QButtonGroup(self)
        gBlend, lBlend = self._group("拼合方式")
        lBlend.addWidget(self._radioRow("", ("叠加", "切除", "取代"),
                                        self._blendGroup, 0))
        self._blendGroup.button(0).setChecked(True)
        self._blendGroup.idClicked.connect(lambda *_: self.paramsChanged.emit())
        self._layout.addWidget(gBlend)

        # ---- 颜色（作用区域：按当前颜色限定起浮雕范围）----
        self._scopeGroup = QButtonGroup(self)
        gColor, lColor = self._group("颜色")
        lColor.addWidget(self._radioRow("", COLOR_SCOPE_LABELS,
                                        self._scopeGroup, 0))
        self._scopeGroup.button(0).setChecked(True)   # 默认“颜色无效”
        self._scopeGroup.idClicked.connect(lambda *_: self.paramsChanged.emit())
        self._layout.addWidget(gColor)

        # ---- 完成（信息提示不进面板，统一走画布左下角信息区）----
        btn = self._button("完成", primary=True)
        btn.clicked.connect(self.finished.emit)
        self._layout.addWidget(btn)
        self._layout.addStretch(1)

        for s in (self._spinBaseH, self._spinBaseA, self._spinFreeH,
                  self._spinBorder, self._spinStrength):
            s.valueChanged.connect(lambda *_: self.paramsChanged.emit())

        self._refreshAvailability()

    # ---------- 布局小工具 ----------

    def _group(self, title: str):
        g = QGroupBox(title)
        lay = QVBoxLayout(g)
        lay.setSpacing(3)
        lay.setContentsMargins(6, 4, 6, 4)
        return g, lay

    def _row(self, label: str, widget) -> QWidget:
        """标签 + 控件行（整体 QWidget，便于整行启用/禁用）。"""
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lab = QLabel(label)
        lab.setMinimumWidth(_LABEL_W)
        lay.addWidget(lab)
        lay.addWidget(widget, 1)
        return row

    def _radioRow(self, label, texts, group: QButtonGroup,
                  start_id: int = 0) -> QWidget:
        """一组单选按钮行（整体 QWidget，便于整行显示/隐藏）。"""
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setSpacing(4)
        lay.setContentsMargins(0, 0, 0, 0)
        if label:
            lab = QLabel(label)
            lab.setMinimumWidth(_LABEL_W)
            lay.addWidget(lab)
        for i, t in enumerate(texts):
            rb = QRadioButton(t)
            group.addButton(rb, start_id + i)
            lay.addWidget(rb)
        lay.addStretch(1)
        return row

    def _divider(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        line.setStyleSheet("color:%s;" % theme.BORDER)
        return line

    # ---------- 联动 ----------

    def _onShapeChanged(self, _id: int) -> None:
        self._refreshAvailability()
        self.paramsChanged.emit()

    def _onModeChanged(self, _id: int) -> None:
        self._refreshAvailability()
        self.paramsChanged.emit()

    def _refreshAvailability(self) -> None:
        """按截面类型刷新各参数可用性（对齐插件 RefreshParamAvailability）。"""
        is_rect = self.shape() == "rectangle"
        is_chamfer = self.shape() == "chamfer"
        local = self.heightMode() == "local"

        # 倒角：隐藏高度模式、显示倒角模式
        self._rowMode.setVisible(not is_chamfer)
        self._rowChamfer.setVisible(is_chamfer)
        # 矩形只用基高+基角：自由高度/局部等高这一组本身也无意义，整行置灰
        self._rowMode.setEnabled(not is_chamfer and not is_rect)

        border_strength = not is_rect and not is_chamfer
        self._rowBorder.setEnabled(border_strength)
        self._rowStrength.setEnabled(border_strength)
        self._rowFreeH.setEnabled(is_chamfer or (not is_rect and local))
        # 表面特点（整体光滑/棱角清晰）对倒角无意义
        for b in self._finishGroup.buttons():
            b.setEnabled(not is_chamfer)
        self._notifyResize()

    # ---------- 取值 ----------

    def shape(self) -> str:
        i = self._shapeGroup.checkedId()
        from core.relief.region_relief import SHAPES
        return SHAPES[i] if 0 <= i < len(SHAPES) else "ellipse"

    def heightMode(self) -> str:
        return "local" if self._modeGroup.checkedId() == 1 else "free"

    def chamferMode(self) -> str:
        return "concave" if self._chamferGroup.checkedId() == 1 else "convex"

    def finish(self) -> str:
        return "sharp" if self._finishGroup.checkedId() == 1 else "smooth"

    def blend(self) -> str:
        i = self._blendGroup.checkedId()
        return ("add", "subtract", "replace")[i if 0 <= i < 3 else 0]

    def colorScope(self) -> str:
        """作用区域：ignore 颜色无效 / inside 颜色内 / outside 颜色外。"""
        i = self._scopeGroup.checkedId()
        return COLOR_SCOPES[i] if 0 <= i < len(COLOR_SCOPES) else "ignore"

    def params(self) -> RegionReliefParams:
        """把面板当前值打包为算法参数。"""
        return RegionReliefParams(
            shape=self.shape(),
            base_height=self._spinBaseH.value(),
            base_angle=self._spinBaseA.value(),
            height_mode=self.heightMode(),
            free_height=self._spinFreeH.value(),
            border_angle=self._spinBorder.value(),
            bulge_strength=self._spinStrength.value(),
            finish=self.finish(),
            blend=self.blend(),
            chamfer_mode=self.chamferMode(),
            color_scope=self.colorScope(),
        )


# ==================== DeJitter（消除锯齿） ====================

class DeJitterParamPanel(ParamPanelBase):
    """消除锯齿面板：锯齿半径一项。

    复刻 Rhino 插件 ReliefDialogTab4"消除锯齿"（距离默认 0.25mm）：
    点选闭合曲线后，曲线两侧该半径内的网格顶点吸附到曲线形成**竖直光壁**
    （侧壁完全竖直、落在曲线上、沿曲线光滑无竖棱）。反馈走画布左下角
    信息区，面板只留"完成"。
    """

    finished = Signal()         # 点击"完成"（结束命令）
    paramsChanged = Signal()    # 距离变化

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        gParam, lParam = self._group("锯齿半径")
        self._spinDist = self._spin(DEFAULT_DEJITTER_DISTANCE,
                                    decimals=2, step=0.05)
        self._spinDist.setRange(0.0, 100.0)
        self._spinDist.setToolTip("锯齿半径 mm：曲线两侧该半径内的壁面吸附到曲线")
        lParam.addWidget(self._row("距离", self._spinDist))
        self._layout.addWidget(gParam)


        btn = self._button("完成", primary=True)
        btn.clicked.connect(self.finished.emit)
        self._layout.addWidget(btn)
        self._layout.addStretch(1)

        self._spinDist.valueChanged.connect(lambda *_: self.paramsChanged.emit())

    # ---------- 布局小工具 ----------

    def _group(self, title: str):
        g = QGroupBox(title)
        lay = QVBoxLayout(g)
        lay.setSpacing(3)
        lay.setContentsMargins(6, 4, 6, 4)
        return g, lay

    def _row(self, label: str, widget) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lab = QLabel(label)
        lab.setMinimumWidth(_LABEL_W)
        lay.addWidget(lab)
        lay.addWidget(widget, 1)
        return row

    # ---------- 取值 ----------

    def distance(self) -> float:
        return self._spinDist.value()

    def params(self) -> DeJitterParams:
        """把面板当前值打包为算法参数。"""
        return DeJitterParams(distance=self.distance())


# ==================== SweepMaterial（导动堆料） ====================

class SweepParamPanel(ParamPanelBase):
    """导动堆料面板（复刻 Rhino 插件【曲面导动】页并升级）。

    笔刷参数三项：直径 / 半衰直径 / 高度——命令内快捷键 A/S 调直径、
    D/F 调半衰直径、Q/W 调高度（面板实时同步）；鼠标跟随笔刷圈
    （外圈直径 + 内圈半衰轮廓）。叠加方式（堆料/取料）× 效果（累加/
    流动叠加）；颜色作用范围（无效/内部/外部，相对底部调色板当前
    颜色）。面板无状态栏：反馈统一走画布左下角信息区。
    """

    finished = Signal()
    paramsChanged = Signal()    # 任一参数变化（快捷键调参/单选切换）

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        # ---- 笔刷形状（三个一行）----
        gShape, lShape = self._group("笔刷形状")
        self._shapeGroup = QButtonGroup(self)
        lShape.addWidget(self._radioRow("", BRUSH_SHAPE_LABELS,
                                        self._shapeGroup, 0))
        self._shapeGroup.button(0).setChecked(True)
        self._shapeGroup.idClicked.connect(lambda *_: self.paramsChanged.emit())
        self._layout.addWidget(gShape)

        # ---- 笔刷参数（直径/半衰直径/高度，快捷键实时调节） ----
        gParam, lParam = self._group("笔刷参数")
        self._spinDiameter = self._spin(DEFAULT_SWEEP_DIAMETER,
                                        decimals=1, step=0.5)
        self._spinDiameter.setRange(PARAM_MIN, 1000.0)
        lParam.addWidget(self._ctrlRow("直径", self._spinDiameter))

        self._spinHalfLife = self._spin(DEFAULT_SWEEP_HALF_LIFE,
                                        decimals=1, step=0.5)
        self._spinHalfLife.setRange(0.0, 1000.0)
        lParam.addWidget(self._ctrlRow("半衰直径", self._spinHalfLife))
        # 直径变化 → 半衰等比跟随；半衰可独立调节（≤直径）
        self._bindHalfLifeFollow(self._spinDiameter, self._spinHalfLife)

        self._spinHeight = self._spin(DEFAULT_SWEEP_HEIGHT, decimals=1, step=0.5)
        self._spinHeight.setRange(0.0, 1000.0)
        lParam.addWidget(self._ctrlRow("高度", self._spinHeight))
        self._layout.addWidget(gParam)

        # ---- 笔刷类型（首行三个 + 第二行 尾粗+细端%）----
        gType, lType = self._group("笔刷类型")
        self._typeGroup = QButtonGroup(self)
        lType.addWidget(self._radioRow("", BRUSH_TYPE_LABELS[:3],
                                       self._typeGroup, 0))
        row2 = QWidget()
        lay2 = QHBoxLayout(row2)
        lay2.setSpacing(4)
        lay2.setContentsMargins(0, 0, 0, 0)
        rbTail = QRadioButton(BRUSH_TYPE_LABELS[3])
        self._typeGroup.addButton(rbTail, 3)
        lay2.addWidget(rbTail)
        self._spinThin = self._spin(DEFAULT_SWEEP_THIN_PERCENT,
                                    decimals=0, step=5.0)
        self._spinThin.setRange(1, 100)
        lay2.addWidget(QLabel("细端%"))
        lay2.addWidget(self._spinThin, 1)
        lType.addWidget(row2)
        self._typeGroup.button(0).setChecked(True)
        self._typeGroup.idClicked.connect(lambda *_: self.paramsChanged.emit())
        self._layout.addWidget(gType)

        # ---- 叠加方式（堆料/取料）----
        gApply, lApply = self._group("叠加方式")
        self._applyGroup = QButtonGroup(self)
        lApply.addWidget(self._radioRow("", APPLY_LABELS,
                                        self._applyGroup, 0))
        self._applyGroup.button(0).setChecked(True)
        self._applyGroup.idClicked.connect(lambda *_: self.paramsChanged.emit())
        self._layout.addWidget(gApply)

        # ---- 效果（累加/流动叠加）----
        gMerge, lMerge = self._group("效果")
        self._mergeGroup = QButtonGroup(self)
        lMerge.addWidget(self._radioRow("", MERGE_LABELS,
                                        self._mergeGroup, 0))
        self._mergeGroup.button(0).setChecked(True)
        self._mergeGroup.idClicked.connect(lambda *_: self.paramsChanged.emit())
        self._layout.addWidget(gMerge)

        # ---- 颜色（作用范围；短标签防溢出）----
        gColor, lColor = self._group("颜色")
        self._scopeGroup = QButtonGroup(self)
        lColor.addWidget(self._radioRow("", ("无效", "内部", "外部"),
                                        self._scopeGroup, 0))
        self._scopeGroup.button(0).setChecked(True)
        self._scopeGroup.idClicked.connect(lambda *_: self.paramsChanged.emit())
        self._layout.addWidget(gColor)

        # ---- 完成 ----
        btn = self._button("完成", primary=True)
        btn.clicked.connect(self.finished.emit)
        self._layout.addWidget(btn)
        self._layout.addStretch(1)

    # ---------- 布局小工具 ----------

    def _group(self, title: str):
        g = QGroupBox(title)
        lay = QVBoxLayout(g)
        lay.setSpacing(3)
        lay.setContentsMargins(6, 4, 6, 4)
        return g, lay

    def _ctrlRow(self, label: str, widget, extra=None) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setSpacing(4)
        lay.setContentsMargins(0, 0, 0, 0)
        lab = QLabel(label)
        lab.setMinimumWidth(_LABEL_W)
        lay.addWidget(lab)
        lay.addWidget(widget, 1)
        if extra is not None:
            lay.addWidget(extra)
        return row

    def _radioRow(self, label: str, texts, group: QButtonGroup,
                  start_id: int = 0) -> QWidget:
        """一组单选按钮行（整体 QWidget）。"""
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setSpacing(6)
        lay.setContentsMargins(0, 0, 0, 0)
        if label:
            lab = QLabel(label)
            lab.setMinimumWidth(_LABEL_W)
            lay.addWidget(lab)
        for i, t in enumerate(texts):
            rb = QRadioButton(t)
            group.addButton(rb, start_id + i)
            lay.addWidget(rb)
        lay.addStretch(1)
        return row

    # ---------- 取值 ----------

    def brushShape(self) -> str:
        i = self._shapeGroup.checkedId()
        return ("gauss", "cone", "hemisphere")[i if 0 <= i < 3 else 0]

    def brushType(self) -> str:
        i = self._typeGroup.checkedId()
        return ("uniform", "middle", "head", "tail")[i if 0 <= i < 4 else 0]

    def applyMode(self) -> str:
        """叠加方式：add=堆料 / remove=取料。"""
        i = self._applyGroup.checkedId()
        return APPLY_MODES[i] if 0 <= i < len(APPLY_MODES) else "add"

    def merge(self) -> str:
        i = self._mergeGroup.checkedId()
        return MERGES[i] if 0 <= i < len(MERGES) else "accumulate"

    def colorScope(self) -> str:
        i = self._scopeGroup.checkedId()
        return COLOR_SCOPES[i] if 0 <= i < len(COLOR_SCOPES) else "ignore"

    def diameter(self) -> float:
        return self._spinDiameter.value()

    def setDiameter(self, value: float) -> None:
        """快捷键调直径后回写（半衰直径随之钳制不超过直径）。"""
        v = max(PARAM_MIN, min(1000.0, float(value)))
        self._spinDiameter.setValue(v)
        if self._spinHalfLife.value() > v:
            self._spinHalfLife.setValue(v)

    def halfLife(self) -> float:
        return self._spinHalfLife.value()

    def setHalfLife(self, value: float) -> None:
        """快捷键调半衰直径后回写（钳制不超过直径）。"""
        v = max(0.0, min(self._spinDiameter.value(), float(value)))
        self._spinHalfLife.setValue(v)

    def height(self) -> float:
        return self._spinHeight.value()

    def setHeight(self, value: float) -> None:
        self._spinHeight.setValue(max(0.0, min(1000.0, float(value))))

    def params(self) -> SweepParams:
        return SweepParams(
            brush_shape=self.brushShape(),
            diameter=self._spinDiameter.value(),
            half_life_diameter=self._spinHalfLife.value(),
            height=self._spinHeight.value(),
            brush_type=self.brushType(),
            thin_percent=self._spinThin.value(),
            merge=self.merge(),
            apply=self.applyMode(),
            color_scope=self.colorScope(),
        )


# ==================== AddMaterial（堆料笔刷） ====================

class AddMaterialParamPanel(ParamPanelBase):
    """堆料笔刷面板：鼠标在模型上涂抹/雕刻，笔刷圈（含半衰圈）跟随光标。

    笔刷参数三项：直径 / 半衰直径 / 高度——命令内快捷键 A/S 调直径、
    D/F 调半衰直径、Q/W 调高度（面板实时同步）。笔刷形状（高斯/锥体/
    半球）；效果（堆料/去料）；颜色作用范围（颜色无效/颜色内/颜色外，
    相对底部调色板当前颜色）。面板无状态栏：反馈走画布左下角信息区。
    """

    finished = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # 命令指定的初始效果（堆料/去料）；None = 未指定，沿用面板记忆
        self._forcedApplyMode = None
        gShape, lShape = self._group("笔刷形状")
        self._shapeGroup = QButtonGroup(self)
        for i, t in enumerate(BRUSH_SHAPE_LABELS):
            rb = QRadioButton(t)
            self._shapeGroup.addButton(rb, i)
            lShape.addWidget(rb)
        self._shapeGroup.button(0).setChecked(True)
        self._layout.addWidget(gShape)

        # ---- 笔刷参数（直径/半衰直径/高度，快捷键实时调节） ----
        gParam, lParam = self._group("笔刷参数")
        self._spinDiameter = self._spin(DEFAULT_SWEEP_DIAMETER,
                                        decimals=1, step=0.5)
        self._spinDiameter.setRange(PARAM_MIN, 1000.0)
        lParam.addWidget(self._ctrlRow("直径", self._spinDiameter))

        self._spinHalfLife = self._spin(DEFAULT_SWEEP_HALF_LIFE,
                                        decimals=1, step=0.5)
        self._spinHalfLife.setRange(0.0, 1000.0)
        lParam.addWidget(self._ctrlRow("半衰直径", self._spinHalfLife))
        # 直径变化 → 半衰等比跟随；半衰可独立调节（≤直径）
        self._bindHalfLifeFollow(self._spinDiameter, self._spinHalfLife)

        self._spinHeight = self._spin(DEFAULT_SWEEP_HEIGHT, decimals=1, step=0.5)
        self._spinHeight.setRange(0.0, 1000.0)
        self._spinHeight.setToolTip("笔刷截面高度（强度）")
        lParam.addWidget(self._ctrlRow("高度", self._spinHeight))
        self._layout.addWidget(gParam)

        # ---- 效果（堆料/去料）----
        gApply, lApply = self._group("效果")
        self._applyGroup = QButtonGroup(self)
        rowApply = QWidget()
        layApply = QHBoxLayout(rowApply)
        layApply.setSpacing(12)
        layApply.setContentsMargins(0, 0, 0, 0)
        for i, t in enumerate(("堆料", "去料")):
            rb = QRadioButton(t)
            self._applyGroup.addButton(rb, i)
            layApply.addWidget(rb)
        layApply.addStretch(1)
        self._applyGroup.button(0).setChecked(True)
        lApply.addWidget(rowApply)
        self._layout.addWidget(gApply)

        # ---- 颜色（作用范围，相对底部调色板当前颜色）----
        gColor, lColor = self._group("颜色")
        self._scopeGroup = QButtonGroup(self)
        lColor.addWidget(self._radioRow("", COLOR_SCOPE_LABELS,
                                        self._scopeGroup, 0))
        self._scopeGroup.button(0).setChecked(True)
        self._layout.addWidget(gColor)

        # ---- 完成 ----
        btn = self._button("完成", primary=True)
        btn.clicked.connect(self.finished.emit)
        self._layout.addWidget(btn)
        self._layout.addStretch(1)

    # ---------- 布局小工具 ----------

    def _group(self, title: str):
        g = QGroupBox(title)
        lay = QVBoxLayout(g)
        lay.setSpacing(3)
        lay.setContentsMargins(6, 4, 6, 4)
        return g, lay

    def _ctrlRow(self, label: str, widget, extra=None) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setSpacing(4)
        lay.setContentsMargins(0, 0, 0, 0)
        lab = QLabel(label)
        lab.setMinimumWidth(_LABEL_W)
        lay.addWidget(lab)
        lay.addWidget(widget, 1)
        if extra is not None:
            lay.addWidget(extra)
        return row

    def _radioRow(self, label: str, texts, group: QButtonGroup,
                  start_id: int = 0) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setSpacing(6)
        lay.setContentsMargins(0, 0, 0, 0)
        if label:
            lab = QLabel(label)
            lab.setMinimumWidth(_LABEL_W)
            lay.addWidget(lab)
        for i, t in enumerate(texts):
            rb = QRadioButton(t)
            group.addButton(rb, start_id + i)
            lay.addWidget(rb)
        lay.addStretch(1)
        return row

    # ---------- 取值 ----------

    def brushShape(self) -> str:
        i = self._shapeGroup.checkedId()
        return ("gauss", "cone", "hemisphere")[i if 0 <= i < 3 else 0]

    def colorScope(self) -> str:
        i = self._scopeGroup.checkedId()
        return COLOR_SCOPES[i] if 0 <= i < len(COLOR_SCOPES) else "ignore"

    def diameter(self) -> float:
        return self._spinDiameter.value()

    def setDiameter(self, value: float) -> None:
        v = max(PARAM_MIN, min(1000.0, float(value)))
        self._spinDiameter.setValue(v)
        if self._spinHalfLife.value() > v:
            self._spinHalfLife.setValue(v)

    def halfLife(self) -> float:
        return self._spinHalfLife.value()

    def setHalfLife(self, value: float) -> None:
        v = max(0.0, min(self._spinDiameter.value(), float(value)))
        self._spinHalfLife.setValue(v)

    def height(self) -> float:
        return self._spinHeight.value()

    def setHeight(self, value: float) -> None:
        self._spinHeight.setValue(max(0.0, min(1000.0, float(value))))

    def applyMode(self) -> str:
        """效果：add=堆料（升高）/ remove=去料（雕刻去除）。"""
        return "remove" if self._applyGroup.checkedId() == 1 else "add"

    def setApplyMode(self, mode: str) -> None:
        """设置效果（快捷键直接以堆料/去料启动命令时用）。

        同时记为“强制效果”：面板首次显示时会恢复上次记忆的面板状态
        （ParamPanelBase.showEvent），若不记住，恢复动作会把这里选的
        堆料/去料覆盖掉——之前 Shift+Q 也变去料就是这个原因。
        """
        self._forcedApplyMode = "remove" if mode == "remove" else "add"
        btn = self._applyGroup.button(1 if self._forcedApplyMode == "remove" else 0)
        if btn is not None and not btn.isChecked():
            btn.setChecked(True)

    def restorePanelState(self) -> None:
        super().restorePanelState()
        if self._forcedApplyMode is not None:
            self.setApplyMode(self._forcedApplyMode)

    def params(self) -> _BrushSweepParams:
        # 高度以符号编码效果：去料 = 负高度（笔刷写回时雕刻去除）
        mag = abs(self._spinHeight.value())
        sign = -1.0 if self.applyMode() == "remove" else 1.0
        return _BrushSweepParams(
            brush_shape=self.brushShape(),
            diameter=self._spinDiameter.value(),
            half_life_diameter=self._spinHalfLife.value(),
            height=sign * mag,
            brush_type="uniform",
            thin_percent=DEFAULT_SWEEP_THIN_PERCENT,
            merge="accumulate",
            color_scope=self.colorScope(),
        )


# ==================== MoveHeight（笔刷移动高度场内容） ====================

class MoveHeightParamPanel(ParamPanelBase):
    """笔刷移动面板：在灰度图模式下按住拖动，笔刷范围内的高度场内容跟随移动。

    笔刷参数两项：直径 / 半衰直径（快捷键 A/S、D/F 实时调节）+ 笔刷形状
    （高斯/锥体/半球，决定羽化权重分布）。无高度/效果/颜色——移动只改变
    内容位置不改变高度。面板无状态栏：反馈走画布左下角信息区。
    """

    finished = Signal()
    paramsChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        gShape, lShape = self._group("笔刷形状")
        self._shapeGroup = QButtonGroup(self)
        for i, t in enumerate(BRUSH_SHAPE_LABELS):
            rb = QRadioButton(t)
            self._shapeGroup.addButton(rb, i)
            lShape.addWidget(rb)
        self._shapeGroup.button(0).setChecked(True)
        self._shapeGroup.idClicked.connect(lambda *_: self.paramsChanged.emit())
        self._layout.addWidget(gShape)

        gParam, lParam = self._group("笔刷参数")
        self._spinDiameter = self._spin(DEFAULT_SWEEP_DIAMETER,
                                        decimals=1, step=0.5)
        self._spinDiameter.setRange(PARAM_MIN, 1000.0)
        lParam.addWidget(self._ctrlRow("直径", self._spinDiameter))

        self._spinHalfLife = self._spin(DEFAULT_SWEEP_HALF_LIFE,
                                        decimals=1, step=0.5)
        self._spinHalfLife.setRange(0.0, 1000.0)
        lParam.addWidget(self._ctrlRow("半衰直径", self._spinHalfLife))
        self._bindHalfLifeFollow(self._spinDiameter, self._spinHalfLife)
        self._layout.addWidget(gParam)

        btn = self._button("完成", primary=True)
        btn.clicked.connect(self.finished.emit)
        self._layout.addWidget(btn)
        self._layout.addStretch(1)

    # ---------- 布局小工具 ----------

    def _group(self, title: str):
        g = QGroupBox(title)
        lay = QVBoxLayout(g)
        lay.setSpacing(3)
        lay.setContentsMargins(6, 4, 6, 4)
        return g, lay

    def _ctrlRow(self, label: str, widget) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setSpacing(4)
        lay.setContentsMargins(0, 0, 0, 0)
        lab = QLabel(label)
        lab.setMinimumWidth(_LABEL_W)
        lay.addWidget(lab)
        lay.addWidget(widget, 1)
        return row

    # ---------- 取值 ----------

    def brushShape(self) -> str:
        i = self._shapeGroup.checkedId()
        return ("gauss", "cone", "hemisphere")[i if 0 <= i < 3 else 0]

    def diameter(self) -> float:
        return self._spinDiameter.value()

    def setDiameter(self, value: float) -> None:
        v = max(PARAM_MIN, min(1000.0, float(value)))
        self._spinDiameter.setValue(v)
        if self._spinHalfLife.value() > v:
            self._spinHalfLife.setValue(v)

    def halfLife(self) -> float:
        return self._spinHalfLife.value()

    def setHalfLife(self, value: float) -> None:
        v = max(0.0, min(self._spinDiameter.value(), float(value)))
        self._spinHalfLife.setValue(v)

    def params(self) -> _BrushSweepParams:
        return _BrushSweepParams(
            brush_shape=self.brushShape(),
            diameter=self._spinDiameter.value(),
            half_life_diameter=self._spinHalfLife.value(),
            height=1.0,
            brush_type="uniform",
            thin_percent=DEFAULT_SWEEP_THIN_PERCENT,
            merge="accumulate",
            color_scope="ignore",
        )


# ==================== RegionTransform（区域变换） ====================

class RegionTransformParamPanel(ParamPanelBase):
    """区域变换面板：选定区域后按 R 旋转 / S 缩放 / G 移动，拖动变换。

    Z 参数：区域内高度均匀缩放（乘以 Z 倍，可为负值=翻转到 Z 负方向），实时预览。
    """

    finished = Signal()
    zChanged = Signal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._status = QLabel("点击闭合曲线或色块选定区域")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color:%s; padding:2px;" % theme.PRIMARY_DARK)
        self._status.setMinimumHeight(40)
        self._layout.addWidget(self._status)
        zRow = QHBoxLayout()
        zRow.setSpacing(4)
        labZ = QLabel("Z缩放:")
        labZ.setFixedWidth(40)
        zRow.addWidget(labZ)
        self._spinZ = self._spin(1.0, decimals=2, step=0.1)
        self._spinZ.setRange(-9999.0, 9999.0)
        # 默认 sizeHint 的数值框会把整行撑到 260px+ → 出现横向滚动条
        self._spinZ.setMinimumWidth(44)
        self._spinZ.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._spinZ.setToolTip("区域内高度均匀缩放（负值=翻转）")
        self._spinZ.valueChanged.connect(self.zChanged.emit)
        zRow.addWidget(self._spinZ, 1)
        zWidget = QWidget()
        zWidget.setLayout(zRow)
        zWidget.setMinimumHeight(32)
        self._layout.addWidget(zWidget)
        btn = self._button("完成", primary=True)
        btn.clicked.connect(self.finished.emit)
        self._layout.addWidget(btn)
        self._layout.addStretch(1)

    def setStatus(self, text: str) -> None:
        self._status.setText(text)

    def zScale(self) -> float:
        return float(self._spinZ.value())

    def setZScale(self, v: float) -> None:
        self._spinZ.blockSignals(True)
        self._spinZ.setValue(v)
        self._spinZ.blockSignals(False)


# ==================== RegionDeform（区域变形） ====================

class RegionDeformParamPanel(ParamPanelBase):
    """区域变形面板：选定源区域 → 点选目标轮廓 → 把源均匀变形到目标轮廓区域。

    Z 参数：变形后高度均匀缩放（乘以 Z 倍），实时预览。
    """

    finished = Signal()
    zChanged = Signal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._status = QLabel("点击闭合曲线或色块选定源区域")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color:%s; padding:2px;" % theme.PRIMARY_DARK)
        self._status.setMinimumHeight(40)
        self._layout.addWidget(self._status)
        zRow = QHBoxLayout()
        zRow.setSpacing(4)
        labZ = QLabel("Z缩放:")
        labZ.setFixedWidth(40)
        zRow.addWidget(labZ)
        self._spinZ = self._spin(1.0, decimals=2, step=0.1)
        self._spinZ.setRange(0.0, 9999.0)
        self._spinZ.setMinimumWidth(44)
        self._spinZ.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._spinZ.setToolTip("区域内高度均匀缩放")
        self._spinZ.valueChanged.connect(self.zChanged.emit)
        zRow.addWidget(self._spinZ)
        zWidget = QWidget()
        zWidget.setLayout(zRow)
        zWidget.setMinimumHeight(32)
        self._layout.addWidget(zWidget)
        btn = self._button("完成", primary=True)
        btn.clicked.connect(self.finished.emit)
        self._layout.addWidget(btn)
        self._layout.addStretch(1)

    def setStatus(self, text: str) -> None:
        self._status.setText(text)

    def zScale(self) -> float:
        return float(self._spinZ.value())

    def setZScale(self, v: float) -> None:
        self._spinZ.blockSignals(True)
        self._spinZ.setValue(v)
        self._spinZ.blockSignals(False)


# ==================== LevelCalibrate（水平校准） ====================

class LevelCalibrateParamPanel(ParamPanelBase):
    """水平校准面板：区域/整个模型 × 点 2 点为准去除沿连线方向线性倾斜，
    使两点等高，作用范围内"斜回来弄平"。
    """

    finished = Signal()
    scopeChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._scopeGroup = QButtonGroup(self)
        self._layout.addWidget(self._radioRow("模式",
                                               ("区域", "整个模型"),
                                               self._scopeGroup, 0))
        self._scopeGroup.button(0).setChecked(True)
        self._scopeGroup.idClicked.connect(lambda *_: self.scopeChanged.emit())
        self._status = QLabel("点击闭合曲线或色块选定区域")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color:%s; padding:2px;" % theme.PRIMARY_DARK)
        self._status.setMinimumHeight(40)
        self._layout.addWidget(self._status)
        btn = self._button("完成", primary=True)
        btn.clicked.connect(self.finished.emit)
        self._layout.addWidget(btn)
        self._layout.addStretch(1)

    def _radioRow(self, label: str, texts, group: QButtonGroup,
                  start_id: int = 0) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setSpacing(6)
        lay.setContentsMargins(0, 0, 0, 0)
        if label:
            lab = QLabel(label)
            lab.setMinimumWidth(_LABEL_W)
            lay.addWidget(lab)
        for i, t in enumerate(texts):
            rb = QRadioButton(t)
            group.addButton(rb, start_id + i)
            lay.addWidget(rb)
        lay.addStretch(1)
        return row

    def scope(self) -> str:
        return "region" if self._scopeGroup.checkedId() == 0 else "model"

    def setStatus(self, text: str) -> None:
        self._status.setText(text)


# ==================== RegionSlope（斜面） ====================

class RegionSlopeParamPanel(ParamPanelBase):
    """斜面面板：选区域 → 点选曲线（刀刃/最高线）→ 选侧 → 曲线为刀刃斜切，
    减去高度。参数：总斜高度、斜面类型（直线/样条/波浪/凸面/凹面）+
    类型细化参数（波纹数/波纹幅度、弧度）。
    """

    finished = Signal()
    paramsChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._spinH = self._spin(2.0, decimals=2, step=0.1)
        self._spinH.setRange(0.0, 9999.0)
        self._layout.addWidget(self._paramRow("总斜高度", self._spinH))
        # ---- 平台距离：距曲线该距离内不减（平台），斜面从该处开始 ----
        self._spinPlateau = self._spin(0.0, decimals=2, step=0.1)
        self._spinPlateau.setRange(0.0, 9999.0)
        self._platRow = self._paramRow("平台距离", self._spinPlateau)
        self._layout.addWidget(self._platRow)
        # ---- 叠加方式：切除（默认）/ 叠加 ----
        self._modeGroup = QButtonGroup(self)
        self._layout.addWidget(self._radioRow("方式",
                                               ("切除", "叠加"),
                                               self._modeGroup, 0))
        self._modeGroup.button(0).setChecked(True)
        self._modeGroup.idClicked.connect(lambda *_: self.paramsChanged.emit())
        self._typeGroup = QButtonGroup(self)
        self._layout.addWidget(self._radioRow("类型",
                                               ("直线", "样条"),
                                               self._typeGroup, 0))
        self._layout.addWidget(self._radioRow("", ("波浪", "凸面", "凹面"),
                                               self._typeGroup, 2))
        self._typeGroup.button(0).setChecked(True)
        self._typeGroup.idClicked.connect(self._onTypeChanged)

        # ---- 波浪细化参数（仅波浪类型显示） ----
        self._spinWaveCount = self._ispin(2, 1, 60)
        self._spinWaveAmp = self._ispin(30, 0, 100)
        self._spinWaveAmp.setSuffix("%")
        self._waveRow1 = self._paramRow("波纹数", self._spinWaveCount)
        self._waveRow2 = self._paramRow("波纹幅度", self._spinWaveAmp)
        self._layout.addWidget(self._waveRow1)
        self._layout.addWidget(self._waveRow2)
        # ---- 凸/凹细化参数（仅凸面/凹面显示） ----
        self._spinArc = self._ispin(25, 0, 100)
        self._spinArc.setSuffix("%")
        self._arcRow = self._paramRow("弧度", self._spinArc)
        self._layout.addWidget(self._arcRow)
        self._waveRow1.setVisible(False)
        self._waveRow2.setVisible(False)
        self._arcRow.setVisible(False)

        btn = self._button("完成", primary=True)
        btn.clicked.connect(self.finished.emit)
        self._layout.addWidget(btn)
        self._layout.addStretch(1)

    def _paramRow(self, label: str, spin: QWidget) -> QWidget:
        """标签 + 输入框一行（可整体显示/隐藏的细化参数行）。

        直接连接传入 spin 的 valueChanged（QDoubleSpinBox 不是 QSpinBox
        子类，findChildren(QSpinBox) 找不到它导致平台距离等不触发预览）。
        """
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setSpacing(6)
        lay.setContentsMargins(0, 0, 0, 0)
        lab = QLabel(label)
        lab.setMinimumWidth(_LABEL_W)
        lay.addWidget(lab)
        lay.addWidget(spin)
        lay.addStretch(1)
        if hasattr(spin, "valueChanged"):
            spin.valueChanged.connect(lambda *_: self.paramsChanged.emit())
        return w

    def _onTypeChanged(self, tid: int) -> None:
        self._waveRow1.setVisible(tid == 2)
        self._waveRow2.setVisible(tid == 2)
        self._arcRow.setVisible(tid in (3, 4))
        self.paramsChanged.emit()
        self._notifyResize()

    def _radioRow(self, label: str, texts, group: QButtonGroup,
                  start_id: int = 0) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setSpacing(6)
        lay.setContentsMargins(0, 0, 0, 0)
        if label:
            lab = QLabel(label)
            lab.setMinimumWidth(_LABEL_W)
            lay.addWidget(lab)
        for i, t in enumerate(texts):
            rb = QRadioButton(t)
            group.addButton(rb, start_id + i)
            lay.addWidget(rb)
        lay.addStretch(1)
        return row

    def height(self) -> float:
        return float(self._spinH.value())

    def profileType(self) -> str:
        types = ("linear", "spline", "wave", "convex", "concave")
        i = self._typeGroup.checkedId()
        return types[i] if 0 <= i < len(types) else "linear"

    def profileParams(self) -> dict:
        """收集参数：叠加方式 + 平台距离 + 当前类型细化参数。"""
        out = {"plateau": float(self._spinPlateau.value()),
               "mode": "add" if self._modeGroup.checkedId() == 1 else "subtract"}
        t = self.profileType()
        if t == "wave":
            out["wave_count"] = int(self._spinWaveCount.value())
            out["wave_amp"] = self._spinWaveAmp.value() / 100.0
        elif t in ("convex", "concave"):
            out["arc"] = self._spinArc.value() / 100.0
        return out

    def setStatus(self, text: str) -> None:
        pass


# ==================== RegionArray（区域阵列） ====================

class RegionArrayParamPanel(ParamPanelBase):
    """区域阵列面板：选定区域 → 选模式 → 阵列分布副本。

    三种模式（单选切换）：矩形阵列 / 圆形阵列 / 曲线阵列。
    沿程渐变（等粗/中粗/首粗/尾粗 + 细端%）借鉴路径塑形 _width_scale，
    副本沿阵列顺序缩放，实现首大尾小/中间粗两端细。
    """

    finished = Signal()
    paramsChanged = Signal()
    pickCurveRequested = Signal()
    pickCenterRequested = Signal()
    modeChanged = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        # ---- 模式单选 ----
        self._modeGroup = QButtonGroup(self)
        self._layout.addWidget(self._radioRow("模式",
                                               ("矩形", "圆形", "曲线"),
                                               self._modeGroup, 0))
        self._modeGroup.button(0).setChecked(True)
        self._modeGroup.idClicked.connect(self._onModeChanged)

        # ---- 矩形阵列参数 ----
        self._rectGroup, lRect = self._groupPlain()
        self._spinRectX = self._ispin(3, 1, 100)
        self._spinRectY = self._ispin(3, 1, 100)
        self._spinRectGapX = self._spin(5.0, decimals=2, step=0.5)
        self._spinRectGapY = self._spin(5.0, decimals=2, step=0.5)
        lRect.addWidget(self._ctrlRow("X 数量", self._spinRectX))
        lRect.addWidget(self._ctrlRow("Y 数量", self._spinRectY))
        lRect.addWidget(self._ctrlRow("X 间距", self._spinRectGapX))
        lRect.addWidget(self._ctrlRow("Y 间距", self._spinRectGapY))
        self._layout.addWidget(self._rectGroup)

        # ---- 圆形阵列参数 ----
        self._circleGroup, lCircle = self._groupPlain()
        self._spinCircleCnt = self._ispin(8, 2, 100)
        self._spinCircleRange = self._spin(360.0, decimals=1, step=5.0)
        self._chkCircleRotate = QCheckBox("旋转物体")
        self._labCenter = QLabel("请拾取圆心")
        self._labCenter.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        self._btnPickCenter = self._button2("拾取圆心")
        self._btnPickCenter.clicked.connect(self.pickCenterRequested.emit)
        lCircle.addWidget(self._ctrlRow("数量", self._spinCircleCnt))
        lCircle.addWidget(self._ctrlRow("角度范围", self._spinCircleRange))
        lCircle.addWidget(self._chkCircleRotate)
        lCircle.addWidget(self._labCenter)
        lCircle.addWidget(self._btnPickCenter)
        self._layout.addWidget(self._circleGroup)

        # ---- 曲线阵列参数 ----
        self._curveGroup, lCurve = self._groupPlain()
        self._spinCurveCnt = self._ispin(5, 1, 100)
        self._spinCurveSpacing = self._spin(10.0, decimals=2, step=0.5)
        self._spinCurveStart = self._spin(0.0, decimals=1, step=5.0)
        self._spinCurveStart.setRange(0.0, 100.0)
        self._chkCurveFill = QCheckBox("铺满曲线")
        self._chkCurveRotate = QCheckBox("切向旋转")
        self._chkCurveOnCurve = QCheckBox("贴在曲线上")
        self._chkCurveOnCurve.setChecked(True)
        self._labCurve = QLabel("请拾取路径曲线")
        self._labCurve.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        self._btnPickCurve = self._button2("拾取曲线")
        self._btnPickCurve.clicked.connect(self.pickCurveRequested.emit)
        lCurve.addWidget(self._ctrlRow("数量", self._spinCurveCnt))
        lCurve.addWidget(self._ctrlRow("间距", self._spinCurveSpacing))
        lCurve.addWidget(self._ctrlRow("起点%", self._spinCurveStart))
        lCurve.addWidget(self._chkCurveFill)
        lCurve.addWidget(self._chkCurveRotate)
        lCurve.addWidget(self._chkCurveOnCurve)
        lCurve.addWidget(self._labCurve)
        lCurve.addWidget(self._btnPickCurve)
        self._layout.addWidget(self._curveGroup)

        # ---- 沿程渐变（借鉴路径塑形 _width_scale） ----
        gGrad, lGrad = self._group("沿程渐变")
        self._typeGroup = QButtonGroup(self)
        lGrad.addWidget(self._radioRow("", ("等粗", "中粗", "首粗", "尾粗"),
                                        self._typeGroup, 0))
        self._typeGroup.button(0).setChecked(True)
        self._spinThin = self._spin(20.0, decimals=0, step=5.0)
        self._spinThin.setRange(1, 100)
        lGrad.addWidget(self._ctrlRow("细端%", self._spinThin))
        self._layout.addWidget(gGrad)

        # ---- Z 缩放 ----
        gZ, lZ = self._group("Z 缩放")
        self._spinZ = self._spin(1.0, decimals=2, step=0.1)
        self._spinZ.setRange(0.0, 9999.0)
        lZ.addWidget(self._ctrlRow("倍率", self._spinZ))
        self._layout.addWidget(gZ)

        # ---- 完成 ----
        btn = self._button("完成", primary=True)
        btn.clicked.connect(self.finished.emit)
        self._layout.addWidget(btn)
        self._layout.addStretch(1)

        # 参数变更信号
        for w in (self._spinRectX, self._spinRectY, self._spinRectGapX,
                  self._spinRectGapY, self._spinCircleCnt,
                  self._spinCircleRange,
                  self._spinCurveCnt, self._spinCurveSpacing,
                  self._spinCurveStart, self._spinThin, self._spinZ):
            w.valueChanged.connect(lambda *_: self.paramsChanged.emit())
        for chk in (self._chkCircleRotate, self._chkCurveFill,
                    self._chkCurveRotate, self._chkCurveOnCurve):
            chk.toggled.connect(lambda *_: self.paramsChanged.emit())
        self._typeGroup.idClicked.connect(lambda *_: self.paramsChanged.emit())

        self._onModeChanged(0)

    def _group(self, title: str):
        g = QGroupBox(title)
        lay = QVBoxLayout(g)
        lay.setSpacing(3)
        lay.setContentsMargins(6, 4, 6, 4)
        return g, lay

    def _groupPlain(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setSpacing(3)
        lay.setContentsMargins(0, 0, 0, 0)
        return w, lay

    def _ctrlRow(self, label: str, widget, extra=None) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setSpacing(4)
        lay.setContentsMargins(0, 0, 0, 0)
        lab = QLabel(label)
        lab.setMinimumWidth(_LABEL_W)
        lay.addWidget(lab)
        lay.addWidget(widget, 1)
        if extra is not None:
            lay.addWidget(extra)
        return row

    def _radioRow(self, label: str, texts, group: QButtonGroup,
                  start_id: int = 0) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setSpacing(6)
        lay.setContentsMargins(0, 0, 0, 0)
        if label:
            lab = QLabel(label)
            lab.setMinimumWidth(_LABEL_W)
            lay.addWidget(lab)
        for i, t in enumerate(texts):
            rb = QRadioButton(t)
            group.addButton(rb, start_id + i)
            lay.addWidget(rb)
        lay.addStretch(1)
        return row

    def _onModeChanged(self, mode: int) -> None:
        self._rectGroup.setVisible(mode == 0)
        self._circleGroup.setVisible(mode == 1)
        self._curveGroup.setVisible(mode == 2)
        self.modeChanged.emit(mode)
        lay = self.layout()
        if lay is not None:
            lay.invalidate()
            lay.activate()
        self._notifyResize()

    # ---------- 状态 ----------
    def setStatus(self, text: str) -> None:
        pass

    def setCurveLabel(self, text: str) -> None:
        self._labCurve.setText(text)

    def setCenterLabel(self, text: str) -> None:
        self._labCenter.setText(text)

    # ---------- 取值 ----------
    def mode(self) -> int:
        return self._modeGroup.checkedId()

    # 矩形
    def rectX(self) -> int:
        return self._spinRectX.value()

    def rectY(self) -> int:
        return self._spinRectY.value()

    def rectGapX(self) -> float:
        return float(self._spinRectGapX.value())

    def rectGapY(self) -> float:
        return float(self._spinRectGapY.value())

    # 圆形
    def circleCnt(self) -> int:
        return self._spinCircleCnt.value()


    def circleRange(self) -> float:
        return float(self._spinCircleRange.value())

    def circleRotate(self) -> bool:
        return self._chkCircleRotate.isChecked()

    # 曲线
    def curveCnt(self) -> int:
        return self._spinCurveCnt.value()

    def curveSpacing(self) -> float:
        return float(self._spinCurveSpacing.value())

    def curveFill(self) -> bool:
        return self._chkCurveFill.isChecked()

    def curveStart(self) -> float:
        return float(self._spinCurveStart.value())

    def curveRotate(self) -> bool:
        return self._chkCurveRotate.isChecked()

    def curveOnCurve(self) -> bool:
        return self._chkCurveOnCurve.isChecked()

    # 公共
    def brushType(self) -> str:
        i = self._typeGroup.checkedId()
        return ("uniform", "middle", "head", "tail")[i if 0 <= i < 4 else 0]

    def thinFrac(self) -> float:
        return float(self._spinThin.value()) / 100.0

    def zScale(self) -> float:
        return float(self._spinZ.value())


# ==================== RegionSymmetry（区域对称） ====================

class RegionSymmetryParamPanel(ParamPanelBase):
    """区域对称面板：左右/上下/自由对称 × 区域/整个模型。

    对称方向：左右对称 / 上下对称 / 自由对称（任意角度轴，仅区域模式）。
    作用模式：区域（仅选定区域内）/ 整个模型（全高度场）。
    选"整个模型"时自动禁用"自由对称"。
    """

    finished = Signal()
    paramsChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self._dirGroup = QButtonGroup(self)
        # 标题与选项都压缩成 2 字：完整语义放 tooltip。原来"左右对称/
        # 上下对称/自由对称" + 52px 标签宽使整行最小宽 298px → 横向滚动条。
        self._layout.addWidget(self._radioRow("对称",
                                               ("左右", "上下", "自由"),
                                               self._dirGroup, 0))
        self._dirGroup.button(0).setChecked(True)
        for i, tip in enumerate(("左右对称（关于竖轴镜像）",
                                 "上下对称（关于横轴镜像）",
                                 "自由对称（任意角度轴，仅区域模式）")):
            self._dirGroup.button(i).setToolTip(tip)
        self._dirGroup.idClicked.connect(self._onDirChanged)

        self._scopeGroup = QButtonGroup(self)
        self._layout.addWidget(self._radioRow("模式",
                                               ("区域", "整个模型"),
                                               self._scopeGroup, 0))
        self._scopeGroup.button(0).setChecked(True)
        self._scopeGroup.idClicked.connect(self._onScopeChanged)

        btn = self._button("完成", primary=True)
        btn.clicked.connect(self.finished.emit)
        self._layout.addWidget(btn)
        self._layout.addStretch(1)

    def _onDirChanged(self, *_):
        self.paramsChanged.emit()

    def _onScopeChanged(self, *_):
        if self._scopeGroup.checkedId() == 1:
            rb = self._dirGroup.button(2)
            if rb.isChecked():
                rb.setChecked(False)
                self._dirGroup.button(0).setChecked(True)
            rb.setEnabled(False)
        else:
            self._dirGroup.button(2).setEnabled(True)
        self.paramsChanged.emit()


    def _radioRow(self, label: str, texts, group: QButtonGroup,
                  start_id: int = 0) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setSpacing(6)
        lay.setContentsMargins(0, 0, 0, 0)
        if label:
            lab = QLabel(label)
            lab.setFixedWidth(40)   # 2 字标签：不用 52px 统一宽，避免整行溢出
            lay.addWidget(lab)
        for i, t in enumerate(texts):
            rb = QRadioButton(t)
            group.addButton(rb, start_id + i)
            lay.addWidget(rb)
        lay.addStretch(1)
        return row

    def direction(self) -> str:
        d = self._dirGroup.checkedId()
        if d == 0:
            return "left_right"
        if d == 1:
            return "top_bottom"
        return "free"

    def scope(self) -> str:
        return "region" if self._scopeGroup.checkedId() == 0 else "model"

    def setStatus(self, text: str) -> None:
        pass


# ==================== Smooth（磨光） ====================

class SmoothParamPanel(ParamPanelBase):
    """磨光面板：磨光方式 + 力度（预设档位单选 + 输入框实时联动）+ 涂抹
    （Ctrl+左键堆料/去料，与底部工具栏同源）+ 笔刷 + 颜色。

    ``show_smear=False`` 时不含"涂抹"组——路径磨光（沿曲线磨光）复用本
    面板（方式/力度/笔刷/颜色完全一致），但不提供 Ctrl+涂抹。

    力度交互（与需求一一对应）：
    - 输入框：用户任意输入（0.1–100%），始终实时显示当前力度；
    - 单选框＝预设档位（极轻微→极强，很小到很大全覆盖），选中即把
      输入框设为该值；输入框手动改值后档位自动取消/对上高亮。
    笔刷参数两项：直径 / 半衰直径（命令内快捷键 A/S、D/F 实时调节）。
    颜色作用范围（无效/内部/外部，相对底部调色板当前颜色）。
    面板无状态栏：反馈走画布左下角信息区。
    """

    finished = Signal()
    paramsChanged = Signal()    # 任一参数变化（快捷键调参/单选切换）

    def __init__(self, parent=None, show_smear: bool = True) -> None:
        self._showSmear = bool(show_smear)
        super().__init__(parent)

        # ---- 磨光方式（标签从简：去高补低/补低/取高） ----
        gMode, lMode = self._group("磨光方式")
        self._modeGroup = QButtonGroup(self)
        lMode.addWidget(self._radioRow("", MODE_LABELS, self._modeGroup, 0))
        self._modeGroup.button(0).setChecked(True)
        self._modeGroup.idClicked.connect(lambda *_: self.paramsChanged.emit())
        self._layout.addWidget(gMode)

        # ---- 磨光力度（预设档位单选 + 力度输入框） ----
        gStrength, lStrength = self._group("磨光力度")
        # 预设档位 3 排 × 3 档、不带行首标签（分组标题已表意），
        # 220px 窄面板内保证档位名完整可见
        self._presetGroup = QButtonGroup(self)
        presetNames = [name for name, _ in STRENGTH_PRESETS]
        for r in range(0, len(presetNames), 3):
            rowP = QWidget()
            layP = QHBoxLayout(rowP)
            layP.setSpacing(6)
            layP.setContentsMargins(0, 0, 0, 0)
            for j, name in enumerate(presetNames[r:r + 3]):
                rid = r + j
                rb = QRadioButton(name)
                rb.setToolTip(f"{name}：力度 {STRENGTH_PRESETS[rid][1]:g}%")
                self._presetGroup.addButton(rb, rid)
                layP.addWidget(rb)
            layP.addStretch(1)
            lStrength.addWidget(rowP)
        # 初始点亮档位 = 默认力度所在档
        _presetVals = [v for _, v in STRENGTH_PRESETS]
        _initGear = (_presetVals.index(DEFAULT_SMOOTH_STRENGTH)
                     if DEFAULT_SMOOTH_STRENGTH in _presetVals else 0)
        self._presetGroup.button(_initGear).setChecked(True)
        self._presetGroup.idClicked.connect(self._onPresetClicked)

        self._spinStrength = self._spin(DEFAULT_SMOOTH_STRENGTH,
                                        decimals=1, step=1.0)
        self._spinStrength.setRange(STRENGTH_MIN, STRENGTH_MAX)
        self._spinStrength.setToolTip("磨光力度%（0.1–100），可直接输入；"
                                      "命令内 Q/W 微调")
        lStrength.addWidget(self._ctrlRow("力度", self._spinStrength,
                                          QLabel("%")))
        self._spinStrength.valueChanged.connect(self._onStrengthEdited)
        self._layout.addWidget(gStrength)

        # ---- 涂抹（Ctrl+左键拖动的堆料/去料；与底部工具栏"涂抹"同源联动）----
        # 路径磨光（show_smear=False）不含此组
        if self._showSmear:
            gSmear, lSmear = self._group("涂抹 (Ctrl)")
            self._smearGroup = QButtonGroup(self)
            lSmear.addWidget(self._radioRow("", _SMEAR_LABELS, self._smearGroup, 0))
            _smear = SmearState.instance()
            self._smearGroup.button(0).setChecked(_smear.mode() == "add")
            self._smearGroup.button(1).setChecked(_smear.mode() == "remove")
            self._smearGroup.idClicked.connect(
                lambda rid: _smear.setMode("remove" if rid == 1 else "add"))
            _smear.modeChanged.connect(self._applySmearMode)
            for i, name in enumerate(_SMEAR_LABELS):
                self._smearGroup.button(i).setToolTip(
                    f"Ctrl+左键在模型上拖动涂抹时{name}"
                    + ("（升高）" if i == 0 else "（雕刻去除）")
                    + "；磨光进行中也可用")
            self._layout.addWidget(gSmear)

        # ---- 笔刷参数（直径/半衰直径，快捷键实时调节） ----
        gParam, lParam = self._group("笔刷参数")
        self._spinDiameter = self._spin(DEFAULT_SMOOTH_DIAMETER,
                                        decimals=1, step=0.5)
        self._spinDiameter.setRange(PARAM_MIN, 1000.0)
        lParam.addWidget(self._ctrlRow("直径", self._spinDiameter))

        self._spinHalfLife = self._spin(DEFAULT_SMOOTH_HALF_LIFE,
                                        decimals=1, step=0.5)
        self._spinHalfLife.setRange(PARAM_MIN, 1000.0)
        lParam.addWidget(self._ctrlRow("半衰直径", self._spinHalfLife))
        # 直径变化 → 半衰等比跟随；半衰可独立调节（≤直径）。
        # 任一变化发 paramsChanged → 命令刷新笔刷圈 → 视口即时重绘
        self._bindHalfLifeFollow(self._spinDiameter, self._spinHalfLife)
        self._layout.addWidget(gParam)

        # ---- 颜色（作用范围，相对底部调色板当前颜色）----
        gColor, lColor = self._group("颜色")
        self._scopeGroup = QButtonGroup(self)
        lColor.addWidget(self._radioRow("", ("无效", "内部", "外部"),
                                        self._scopeGroup, 0))
        self._scopeGroup.button(0).setChecked(True)
        self._scopeGroup.idClicked.connect(lambda *_: self.paramsChanged.emit())
        self._layout.addWidget(gColor)

        # ---- 完成 ----
        btn = self._button("完成", primary=True)
        btn.clicked.connect(self.finished.emit)
        self._layout.addWidget(btn)
        self._layout.addStretch(1)

    # ---------- 布局小工具 ----------

    def _group(self, title: str):
        g = QGroupBox(title)
        lay = QVBoxLayout(g)
        lay.setSpacing(3)
        lay.setContentsMargins(6, 4, 6, 4)
        return g, lay

    def _ctrlRow(self, label: str, widget, extra=None) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setSpacing(4)
        lay.setContentsMargins(0, 0, 0, 0)
        lab = QLabel(label)
        lab.setMinimumWidth(_LABEL_W)
        lay.addWidget(lab)
        lay.addWidget(widget, 1)
        if extra is not None:
            lay.addWidget(extra)
        return row

    def _radioRow(self, label: str, texts, group: QButtonGroup,
                  start_id: int = 0) -> QWidget:
        """一组单选按钮行（整体 QWidget）。"""
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setSpacing(6)
        lay.setContentsMargins(0, 0, 0, 0)
        if label:
            lab = QLabel(label)
            lab.setMinimumWidth(_LABEL_W)
            lay.addWidget(lab)
        for i, t in enumerate(texts):
            rb = QRadioButton(t)
            group.addButton(rb, start_id + i)
            lay.addWidget(rb)
        lay.addStretch(1)
        return row

    # ---------- 力度联动（档位 ⇄ 输入框） ----------

    def _applySmearMode(self, mode: str) -> None:
        """涂抹模式变化（含底部工具栏/程序设置）：同步本面板单选态。"""
        self._smearGroup.button(0).setChecked(mode == "add")
        self._smearGroup.button(1).setChecked(mode == "remove")

    def _onPresetClicked(self, rid: int) -> None:
        """选中预设档位：输入框直接等于该档位值（valueChanged 回流时
        _onStrengthEdited 会把该档位重新点亮，状态一致）。"""
        if 0 <= rid < len(STRENGTH_PRESETS):
            self._spinStrength.setValue(STRENGTH_PRESETS[rid][1])

    def _onStrengthEdited(self, value: float) -> None:
        """输入框变化（手动输入/快捷键）：实时同步档位高亮并通知命令。"""
        self._syncPresetRadios(value)
        self.paramsChanged.emit()

    def _syncPresetRadios(self, value: float) -> None:
        """值恰为某档位 → 点亮该档位；否则全部取消（互斥组需临时解除互斥）。"""
        hit = -1
        for i, (_, v) in enumerate(STRENGTH_PRESETS):
            if abs(v - float(value)) < 1e-6:
                hit = i
                break
        self._presetGroup.setExclusive(False)
        for b in self._presetGroup.buttons():
            b.setChecked(self._presetGroup.id(b) == hit)
        self._presetGroup.setExclusive(True)

    # ---------- 取值 ----------

    def mode(self) -> str:
        i = self._modeGroup.checkedId()
        from core.relief.smooth import MODES
        return MODES[i] if 0 <= i < len(MODES) else "both"

    def strength(self) -> float:
        return self._spinStrength.value()

    def setStrength(self, value: float) -> None:
        """快捷键调力度后回写输入框（档位高亮随 valueChanged 自动同步）。"""
        self._spinStrength.setValue(max(STRENGTH_MIN,
                                        min(STRENGTH_MAX, float(value))))

    def colorScope(self) -> str:
        i = self._scopeGroup.checkedId()
        return COLOR_SCOPES[i] if 0 <= i < len(COLOR_SCOPES) else "ignore"

    def diameter(self) -> float:
        return self._spinDiameter.value()

    def setDiameter(self, value: float) -> None:
        v = max(PARAM_MIN, min(1000.0, float(value)))
        self._spinDiameter.setValue(v)
        if self._spinHalfLife.value() > v:
            self._spinHalfLife.setValue(v)

    def halfLife(self) -> float:
        return self._spinHalfLife.value()

    def setHalfLife(self, value: float) -> None:
        v = max(PARAM_MIN, min(self._spinDiameter.value(), float(value)))
        self._spinHalfLife.setValue(v)

    def params(self) -> SmoothParams:
        return SmoothParams(
            mode=self.mode(),
            strength=self.strength(),
            diameter=self.diameter(),
            half_life_diameter=self.halfLife(),
            color_scope=self.colorScope(),
        )


# ==================== Stamp（冲压） ====================

class StampParamPanel(ParamPanelBase):
    """冲压参数面板（复刻 Rhino 插件"冲压"页，蒙版由曲线内外换为颜色模块）。

    分组与插件一致：冲压（绝对/相对 + 正负方向 + 作用范围 + 直径）+
    高低层次调整（作用区域 + 高/中/低三层 + 三个调整按钮）。

    作用范围二选一——**颜色无效**：按鼠标圆圈区域冲压（光标跟随圆圈，
    A 缩小 / S 放大，直径输入框联动）；**颜色内**：按单击处色号的整个
    色块区域冲压（不受圆圈限制）。高低层次调整的作用区域以底部调色板
    当前颜色为参照。面板只放参数，不放按钮/提示：冲压由画布单击触发，
    所有反馈显示在画布左下角信息区（CommandManager.notifyResult）。
    """

    finished = Signal()
    adjustHighRequested = Signal()      # 仅高层调整
    adjustLowRequested = Signal()       # 仅底层调整
    adjustOverallRequested = Signal()   # 高层 + 底层整体调整

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        # ---- 冲压 ----
        gStamp, lStamp = self._group("冲压")

        # 绝对 / 相对：各带自己的输入框（与插件两行排布一致）
        self._modeGroup = QButtonGroup(self)
        self._spinAbs = self._spin(DEFAULT_ABSOLUTE, decimals=1, step=1.0)
        self._spinAbs.setRange(-1000.0, 1000.0)
        self._spinRel = self._spin(DEFAULT_RELATIVE, decimals=1, step=1.0)
        self._spinRel.setRange(0.1, 1000.0)
        lStamp.addWidget(self._modeRow("绝对", self._modeGroup, 0, self._spinAbs))
        lStamp.addWidget(self._modeRow("相对", self._modeGroup, 1, self._spinRel))
        self._modeGroup.button(0).setChecked(True)
        self._modeGroup.idClicked.connect(self._onModeChanged)

        # 正方向 / 负方向（仅相对模式可用，默认正方向）
        self._dirGroup = QButtonGroup(self)
        rowDir = QWidget()
        layDir = QHBoxLayout(rowDir)
        layDir.setSpacing(12)
        layDir.setContentsMargins(0, 0, 0, 0)
        for i, t in enumerate(("正方向", "负方向")):
            rb = QRadioButton(t)
            self._dirGroup.addButton(rb, i)
            layDir.addWidget(rb)
        layDir.addStretch(1)
        self._dirGroup.button(0).setChecked(True)
        self._rowDir = rowDir
        lStamp.addWidget(rowDir)

        lStamp.addWidget(self._divider())

        # 直径：鼠标圆圈大小（画布内 A/S ±10% 调节，此处联动显示/可输入）
        self._spinDiameter = self._spin(DEFAULT_DIAMETER, decimals=1, step=1.0)
        self._spinDiameter.setRange(DIAMETER_MIN, DIAMETER_MAX)
        lStamp.addWidget(self._ctrlRow("直径", self._spinDiameter))

        # 作用范围：颜色无效 / 颜色内 / 颜色外（叠加颜色模块，同区域浮雕三元）
        self._scopeGroup = QButtonGroup(self)
        rowScope = QWidget()
        layScope = QHBoxLayout(rowScope)
        layScope.setSpacing(8)
        layScope.setContentsMargins(0, 0, 0, 0)
        for i, t in enumerate(SCOPE_LABELS):
            rb = QRadioButton(t)
            self._scopeGroup.addButton(rb, i)
            layScope.addWidget(rb)
        layScope.addStretch(1)
        self._scopeGroup.button(0).setChecked(True)
        lStamp.addWidget(rowScope)
        self._layout.addWidget(gStamp)

        # ---- 高低层次调整 ----
        gLevel, lLevel = self._group("高低层次调整")

        self._regionGroup = QButtonGroup(self)
        rowRegion = QWidget()
        layRegion = QHBoxLayout(rowRegion)
        layRegion.setSpacing(8)
        layRegion.setContentsMargins(0, 0, 0, 0)
        for i, t in enumerate(LEVEL_REGION_LABELS):
            rb = QRadioButton(t)
            self._regionGroup.addButton(rb, i)
            layRegion.addWidget(rb)
        layRegion.addStretch(1)
        self._regionGroup.button(0).setChecked(True)
        lLevel.addWidget(rowRegion)

        self._spinHigh = self._spin(DEFAULT_HIGH_LEVEL, decimals=1, step=0.5)
        self._spinHigh.setRange(-100000.0, 100000.0)
        btnHigh = self._button2("调整")
        btnHigh.clicked.connect(self.adjustHighRequested.emit)
        lLevel.addWidget(self._ctrlRow("高层调整", self._spinHigh, btnHigh))

        self._spinMid = self._spin(DEFAULT_MID_PERCENT, decimals=0, step=5.0)
        self._spinMid.setRange(10.0, 90.0)
        labPct = QLabel("%")
        lLevel.addWidget(self._ctrlRow("中层位置", self._spinMid, labPct))

        self._spinLow = self._spin(DEFAULT_LOW_LEVEL, decimals=1, step=0.5)
        self._spinLow.setRange(-100000.0, 100000.0)
        btnLow = self._button2("调整")
        btnLow.clicked.connect(self.adjustLowRequested.emit)
        lLevel.addWidget(self._ctrlRow("底层调整", self._spinLow, btnLow))

        btnOverall = self._button("整体层次调整")
        btnOverall.clicked.connect(self.adjustOverallRequested.emit)
        lLevel.addWidget(btnOverall)
        self._layout.addWidget(gLevel)

        # ---- 完成 ----
        btnDone = self._button("完成", primary=True)
        btnDone.clicked.connect(self.finished.emit)
        self._layout.addWidget(btnDone)
        self._layout.addStretch(1)

        self._onModeChanged(self._modeGroup.checkedId())

    # ---------- 布局小工具 ----------

    def _group(self, title: str):
        g = QGroupBox(title)
        lay = QVBoxLayout(g)
        lay.setSpacing(3)
        lay.setContentsMargins(6, 4, 6, 4)
        return g, lay

    def _modeRow(self, text: str, group: QButtonGroup, rid: int,
                 spin: QDoubleSpinBox) -> QWidget:
        """单选框 + 输入框一行（绝对 5.0 / 相对 1.0）。"""
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setSpacing(6)
        lay.setContentsMargins(0, 0, 0, 0)
        rb = QRadioButton(text)
        group.addButton(rb, rid)
        lay.addWidget(rb)
        lay.addWidget(spin, 1)
        return row

    def _ctrlRow(self, label: str, widget, extra=None) -> QWidget:
        """标签 + 控件 +（可选）后缀控件一行。"""
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setSpacing(4)
        lay.setContentsMargins(0, 0, 0, 0)
        lab = QLabel(label)
        lab.setMinimumWidth(_LABEL_W)
        lay.addWidget(lab)
        lay.addWidget(widget, 1)
        if extra is not None:
            lay.addWidget(extra)
        return row

    def _divider(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        line.setStyleSheet("color:%s;" % theme.BORDER)
        return line

    # ---------- 联动 ----------

    def _onModeChanged(self, rid: int) -> None:
        """绝对/相对联动：正负方向仅相对模式可用（与插件一致）。"""
        relative = (rid == 1)
        self._rowDir.setEnabled(relative)
        for b in self._dirGroup.buttons():
            b.setEnabled(relative)

    # ---------- 取值 ----------

    def mode(self) -> str:
        return "relative" if self._modeGroup.checkedId() == 1 else "absolute"

    def direction(self) -> str:
        return "negative" if self._dirGroup.checkedId() == 1 else "positive"

    def scope(self) -> str:
        """作用范围：ignore 颜色无效 / inside 颜色内 / outside 颜色外。"""
        i = self._scopeGroup.checkedId()
        return SCOPES[i] if 0 <= i < len(SCOPES) else "ignore"

    def diameter(self) -> float:
        """鼠标圆圈直径（mm）。"""
        return self._spinDiameter.value()

    def setDiameter(self, value: float) -> None:
        """A/S 快捷键调径后回写输入框（钳制在允许范围）。"""
        self._spinDiameter.setValue(max(DIAMETER_MIN,
                                        min(DIAMETER_MAX, float(value))))

    def value(self) -> float:
        return self._spinRel.value() if self.mode() == "relative" \
            else self._spinAbs.value()

    def stampParams(self) -> StampParams:
        return StampParams(mode=self.mode(), direction=self.direction(),
                           scope=self.scope(), value=self.value())

    def levelRegion(self) -> str:
        """层次调整作用区域：all 全部 / inside 颜色内 / outside 颜色外。"""
        i = self._regionGroup.checkedId()
        return LEVEL_REGIONS[i] if 0 <= i < len(LEVEL_REGIONS) else "all"

    def highLevel(self) -> float:
        return self._spinHigh.value()

    def lowLevel(self) -> float:
        return self._spinLow.value()

    def midPercent(self) -> float:
        return self._spinMid.value()


# ==================== Trim 修剪 ====================

class TrimParamPanel(ParamPanelBase):
    modeChanged = Signal(int)          # 0=快速修剪 1=边界修剪
    confirmed = Signal()
    cancelRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # 模式选择：快速修剪 / 边界修剪
        modeRow = QHBoxLayout()
        self._modeGroup = QButtonGroup(self)
        self._radioQuick = QRadioButton("快速修剪")
        self._radioBoundary = QRadioButton("边界修剪")
        self._radioQuick.setChecked(True)
        self._modeGroup.addButton(self._radioQuick, 0)
        self._modeGroup.addButton(self._radioBoundary, 1)
        self._modeGroup.idClicked.connect(self.modeChanged.emit)
        modeRow.addWidget(QLabel("模式"))
        modeRow.addWidget(self._radioQuick)
        modeRow.addWidget(self._radioBoundary)
        modeRow.addStretch(1)
        self._layout.addLayout(modeRow)

        self._hintLabel = QLabel("点击要修剪的线段，自动找交点")
        self._hintLabel.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        self._layout.addWidget(self._hintLabel)

        btnOk = self._button("确认执行", primary=True)
        btnOk.clicked.connect(self.confirmed.emit)
        self._layout.addWidget(btnOk)

        btnCancel = self._button2("取消")
        btnCancel.clicked.connect(self.cancelRequested.emit)
        self._layout.addWidget(btnCancel)
        self._layout.addStretch(1)

        # 连接模式切换信号更新提示文字
        self._modeGroup.idClicked.connect(self._updateHint)

    def _updateHint(self, mode: int) -> None:
        if mode == 0:
            self._hintLabel.setText("点击要修剪的线段，自动找交点")
        else:
            self._hintLabel.setText("先选边界线，再选要修剪的曲线")


# ==================== Array 阵列 ====================

class ArrayParamPanel(ParamPanelBase):
    """阵列参数面板：矩形/圆形/曲线三种模式。

    信号:
        modeChanged(int): 0=矩形 1=圆形 2=曲线
        paramsChanged(): 任一参数变更
        confirmed(): 点击"确认执行"
        cancelRequested(): 点击"取消"
    """
    modeChanged = Signal(int)
    paramsChanged = Signal()
    confirmed = Signal()
    cancelRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # 模式选择
        modeRow = QHBoxLayout()
        self._modeGroup = QButtonGroup(self)
        self._radioRect = QRadioButton("矩形")
        self._radioCircle = QRadioButton("圆形")
        self._radioCurve = QRadioButton("曲线")
        self._radioRect.setChecked(True)
        self._modeGroup.addButton(self._radioRect, 0)
        self._modeGroup.addButton(self._radioCircle, 1)
        self._modeGroup.addButton(self._radioCurve, 2)
        self._modeGroup.idClicked.connect(self._onModeChanged)
        modeRow.addWidget(QLabel("模式"))
        modeRow.addWidget(self._radioRect)
        modeRow.addWidget(self._radioCircle)
        modeRow.addWidget(self._radioCurve)
        modeRow.addStretch(1)
        self._layout.addLayout(modeRow)

        # ---- 矩形阵列参数 ----
        self._rectGroup = QGroupBox("矩形阵列")
        rectLay = QVBoxLayout(self._rectGroup)
        rectLay.setSpacing(3)
        rectLay.setContentsMargins(6, 4, 6, 4)
        self._spinRectX = self._ispin(3, 1, 100)
        self._spinRectY = self._ispin(3, 1, 100)
        self._spinRectGapX = self._spin(5.0, decimals=2, step=0.5)
        self._spinRectGapY = self._spin(5.0, decimals=2, step=0.5)
        self._chkXYEqual = QCheckBox("XY 等距")
        self._chkXYEqual.toggled.connect(self._onXYEqual)
        rectLay.addWidget(self._row("X 数量", self._spinRectX))
        rectLay.addWidget(self._row("Y 数量", self._spinRectY))
        rectLay.addWidget(self._row("X 间距", self._spinRectGapX))
        rectLay.addWidget(self._row("Y 间距", self._spinRectGapY))
        rectLay.addWidget(self._chkXYEqual)
        self._layout.addWidget(self._rectGroup)

        # ---- 圆形阵列参数 ----
        self._circleGroup = QGroupBox("圆形阵列")
        circleLay = QVBoxLayout(self._circleGroup)
        circleLay.setSpacing(3)
        circleLay.setContentsMargins(6, 4, 6, 4)
        self._spinCircleCnt = self._ispin(8, 2, 100)
        self._spinCircleR = self._spin(50.0, decimals=2, step=1.0)
        self._spinCircleR.setRange(0.0, 9999.0)
        self._spinCircleStart = self._spin(0.0, decimals=1, step=5.0)
        self._spinCircleRange = self._spin(360.0, decimals=1, step=5.0)
        self._chkCircleRotate = QCheckBox("旋转物体")
        self._chkCircleRotate.setChecked(True)
        self._chkCircleKnownR = QCheckBox("已知半径（选中对象为阵列之一）")
        self._chkCircleKnownR.toggled.connect(self._onKnownR)
        self._btnPickCenter = self._button2("拾取圆心")
        self._labCenter = QLabel("圆心：未拾取")
        self._labCenter.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        self._spinRingCount = self._ispin(1, 1, 20)
        self._spinRingSpacing = self._spin(10.0, decimals=2, step=1.0)
        circleLay.addWidget(self._row("数量", self._spinCircleCnt))
        circleLay.addWidget(self._row("半径", self._spinCircleR))
        circleLay.addWidget(self._row("起始角", self._spinCircleStart))
        circleLay.addWidget(self._row("角度范围", self._spinCircleRange))
        circleLay.addWidget(self._row("圈数", self._spinRingCount))
        circleLay.addWidget(self._row("圈间距", self._spinRingSpacing))
        circleLay.addWidget(self._chkCircleRotate)
        circleLay.addWidget(self._chkCircleKnownR)
        circleLay.addWidget(self._btnPickCenter)
        circleLay.addWidget(self._labCenter)
        self._layout.addWidget(self._circleGroup)

        # ---- 曲线阵列参数 ----
        self._curveGroup = QGroupBox("曲线阵列")
        curveLay = QVBoxLayout(self._curveGroup)
        curveLay.setSpacing(3)
        curveLay.setContentsMargins(6, 4, 6, 4)
        self._spinCurveCnt = self._ispin(10, 1, 100)
        self._spinCurveSpacing = self._spin(10.0, decimals=2, step=0.5)
        self._chkCurveFill = QCheckBox("铺满曲线")
        self._chkCurveRotate = QCheckBox("切向旋转")
        self._chkCurveOnCurve = QCheckBox("贴在曲线上")
        self._chkCurveOnCurve.setChecked(True)
        self._spinCurveStart = self._spin(0.0, decimals=1, step=5.0)
        self._spinCurveStart.setRange(0.0, 100.0)
        self._labCurveHint = QLabel("请拾取路径曲线")
        self._labCurveHint.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        self._btnPickCurve = self._button2("拾取曲线")
        curveLay.addWidget(self._row("数量", self._spinCurveCnt))
        curveLay.addWidget(self._row("间距", self._spinCurveSpacing))
        curveLay.addWidget(self._row("起点%", self._spinCurveStart))
        curveLay.addWidget(self._chkCurveFill)
        curveLay.addWidget(self._chkCurveRotate)
        curveLay.addWidget(self._chkCurveOnCurve)
        curveLay.addWidget(self._labCurveHint)
        curveLay.addWidget(self._btnPickCurve)
        self._layout.addWidget(self._curveGroup)

        # ---- 确认/取消按钮 ----
        btnOk = self._button("确认执行", primary=True)
        btnOk.clicked.connect(self.confirmed.emit)
        self._layout.addWidget(btnOk)
        btnCancel = self._button2("取消")
        btnCancel.clicked.connect(self.cancelRequested.emit)
        self._layout.addWidget(btnCancel)
        self._layout.addStretch(1)

        # 参数变更信号
        for w in (self._spinRectX, self._spinRectY, self._spinRectGapX,
                  self._spinRectGapY, self._spinCircleCnt, self._spinCircleR,
                  self._spinCircleStart, self._spinCircleRange,
                  self._spinRingCount, self._spinRingSpacing,
                  self._spinCurveCnt, self._spinCurveSpacing, self._spinCurveStart):
            w.valueChanged.connect(lambda *_: self.paramsChanged.emit())
        for chk in (self._chkCircleRotate, self._chkCircleKnownR,
                    self._chkCurveFill, self._chkCurveRotate, self._chkCurveOnCurve):
            chk.toggled.connect(lambda *_: self.paramsChanged.emit())

        self._onModeChanged(0)

    def _row(self, label: str, widget) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lab = QLabel(label)
        lab.setMinimumWidth(_LABEL_W)
        lay.addWidget(lab)
        lay.addWidget(widget, 1)
        return row

    def _onModeChanged(self, mode: int) -> None:
        self._rectGroup.setVisible(mode == 0)
        self._circleGroup.setVisible(mode == 1)
        self._curveGroup.setVisible(mode == 2)
        self.modeChanged.emit(mode)
        self._notifyResize()

    def _onXYEqual(self, on: bool) -> None:
        self._spinRectGapY.setEnabled(not on)
        if on:
            self._spinRectGapX.valueChanged.connect(
                self._spinRectGapY.setValue)
        else:
            try:
                self._spinRectGapX.valueChanged.disconnect(
                    self._spinRectGapY.setValue)
            except RuntimeError:
                pass

    def _onKnownR(self, on: bool) -> None:
        self._spinCircleR.setEnabled(not on)
        self._spinCircleStart.setEnabled(not on)

    # ---------- 取值 ----------
    def mode(self) -> int:
        return self._modeGroup.checkedId()

    # 矩形
    def rectX(self) -> int: return self._spinRectX.value()
    def rectY(self) -> int: return self._spinRectY.value()
    def rectGapX(self) -> float: return self._spinRectGapX.value()
    def rectGapY(self) -> float:
        return self._spinRectGapX.value() if self._chkXYEqual.isChecked() else self._spinRectGapY.value()

    # 圆形
    def circleCnt(self) -> int: return self._spinCircleCnt.value()
    def circleR(self) -> float: return self._spinCircleR.value()
    def circleStart(self) -> float: return self._spinCircleStart.value()
    def circleRange(self) -> float: return self._spinCircleRange.value()
    def circleRotate(self) -> bool: return self._chkCircleRotate.isChecked()
    def circleKnownR(self) -> bool: return self._chkCircleKnownR.isChecked()
    def ringCount(self) -> int: return self._spinRingCount.value()
    def ringSpacing(self) -> float: return self._spinRingSpacing.value()

    # 曲线
    def curveCnt(self) -> int: return self._spinCurveCnt.value()
    def curveSpacing(self) -> float: return self._spinCurveSpacing.value()
    def curveFill(self) -> bool: return self._chkCurveFill.isChecked()
    def curveStart(self) -> float: return self._spinCurveStart.value()
    def curveRotate(self) -> bool: return self._chkCurveRotate.isChecked()
    def curveOnCurve(self) -> bool: return self._chkCurveOnCurve.isChecked()

    def btnPickCurve(self):
        return self._btnPickCurve

    def setCurveLabel(self, text: str) -> None:
        self._labCurveHint.setText(text)

    def btnPickCenter(self):
        return self._btnPickCenter

    def setCenterLabel(self, text: str) -> None:
        self._labCenter.setText(text)


# ==================== Offset 偏移 ====================

class OffsetParamPanel(ParamPanelBase):
    """偏移参数面板。

    信号:
        distanceChanged(float): 偏移距离变更
        confirmed(): 点击"确认执行"
        cancelRequested(): 点击"取消"
    """
    distanceChanged = Signal(float)
    paramsChanged = Signal()
    confirmed = Signal()
    cancelRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._spinDist = self._spin(5.0, decimals=2, step=0.5)
        self._spinDist.setRange(0.0, 9999.0)
        self._addRow("距离", self._spinDist)

        self._chkBoth = QCheckBox("双侧偏移")
        self._chkInner = QCheckBox("保留内轮廓")
        rowBoth = QHBoxLayout()
        rowBoth.setSpacing(8)
        rowBoth.addWidget(self._chkBoth)
        rowBoth.addWidget(self._chkInner)
        rowBoth.addStretch(1)
        self._layout.addLayout(rowBoth)

        self._chkCap = QCheckBox("加盖")
        self._capGroup = QButtonGroup(self)
        self._radioButt = QRadioButton("平头盖")
        self._radioRound = QRadioButton("圆头盖")
        self._radioButt.setChecked(True)
        self._capGroup.addButton(self._radioButt, 0)
        self._capGroup.addButton(self._radioRound, 1)
        rowCap = QHBoxLayout()
        rowCap.setSpacing(8)
        rowCap.addWidget(self._chkCap)
        rowCap.addWidget(self._radioButt)
        rowCap.addWidget(self._radioRound)
        rowCap.addStretch(1)
        self._layout.addLayout(rowCap)

        btnOk = self._button("确认执行", primary=True)
        btnOk.clicked.connect(self.confirmed.emit)
        self._layout.addWidget(btnOk)

        btnCancel = self._button2("取消")
        btnCancel.clicked.connect(self.cancelRequested.emit)
        self._layout.addWidget(btnCancel)
        self._layout.addStretch(1)

        self._spinDist.valueChanged.connect(self.distanceChanged.emit)
        for chk in (self._chkBoth, self._chkCap, self._chkInner):
            chk.toggled.connect(lambda *_: self.paramsChanged.emit())
        self._capGroup.idClicked.connect(
            lambda *_: self.paramsChanged.emit())

    def distance(self) -> float:
        return self._spinDist.value()

    def bothSides(self) -> bool:
        return self._chkBoth.isChecked()

    def cap(self) -> bool:
        return self._chkCap.isChecked()

    def keepInner(self) -> bool:
        return self._chkInner.isChecked()

    def capStyle(self) -> str:
        """端盖样式：'butt' 平头盖 / 'round' 圆头盖。"""
        return "round" if self._capGroup.checkedId() == 1 else "butt"

    def setHint(self, text: str) -> None:
        pass


# ==================== Mirror 镜像 ====================

class MirrorParamPanel(ParamPanelBase):
    """镜像参数面板。

    交互：点选两点定义镜像轴 → 实时预览镜像副本 → 确认执行。
    选项：
        - 删除原图（默认不勾选：保留原图，复制一份镜像副本）
        - 副本数：默认 1（每次镜像生成 1 份副本）
    """
    paramsChanged = Signal()
    confirmed = Signal()
    cancelRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._chkDelete = QCheckBox("删除原图")
        self._layout.addWidget(self._chkDelete)

        self._hintLabel = QLabel("点选两点作为镜像轴，预览后确认")
        self._hintLabel.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        self._layout.addWidget(self._hintLabel)

        btnOk = self._button("确认执行", primary=True)
        btnOk.clicked.connect(self.confirmed.emit)
        self._layout.addWidget(btnOk)

        btnCancel = self._button2("取消")
        btnCancel.clicked.connect(self.cancelRequested.emit)
        self._layout.addWidget(btnCancel)
        self._layout.addStretch(1)

        self._chkDelete.toggled.connect(lambda *_: self.paramsChanged.emit())

    def deleteOriginal(self) -> bool:
        return self._chkDelete.isChecked()

    def setHint(self, text: str) -> None:
        self._hintLabel.setText(text)


# ==================== Extend 延伸 ====================

class ExtendParamPanel(ParamPanelBase):
    """延伸参数面板：快速延伸 / 选中目标延伸 两种模式。

    - 快速延伸(0)：选对象后点击靠近端点 → 自动延伸到文档中最近的相交曲线
    - 选中目标延伸(1)：选对象 → 点端定方向 → 点目标边界曲线 → 延伸到与目标交点
    """
    modeChanged = Signal(int)
    cancelRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._layout.addWidget(QLabel("延伸模式"))
        self._modeGroup = QButtonGroup(self)
        self._radioQuick = QRadioButton("快速延伸")
        self._radioTarget = QRadioButton("选中目标延伸")
        self._radioQuick.setChecked(True)
        self._modeGroup.addButton(self._radioQuick, 0)
        self._modeGroup.addButton(self._radioTarget, 1)
        self._modeGroup.idClicked.connect(self.modeChanged.emit)
        self._layout.addWidget(self._radioQuick)
        self._layout.addWidget(self._radioTarget)

        self._hintLabel = QLabel("点击曲线即延伸（较近端）；目标模式：点曲线→点目标")
        self._hintLabel.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        self._layout.addWidget(self._hintLabel)

        btnCancel = self._button2("取消")
        btnCancel.clicked.connect(self.cancelRequested.emit)
        self._layout.addWidget(btnCancel)
        self._layout.addStretch(1)

    def mode(self) -> int:
        """0=快速延伸 1=选中目标延伸。"""
        return self._modeGroup.checkedId()

    def setHint(self, text: str) -> None:
        self._hintLabel.setText(text)


# ==================== Outline 轮廓（外轮廓 / 内轮廓） ====================

class OutlineParamPanel(ParamPanelBase):
    """轮廓参数面板（对应 Rhino 插件 ToolPage.ShowOuterOutline 窗口）。

    轮廓模式（单选）：
    - 外轮廓：选中曲线后实时预览包裹全部曲线的单一外轮廓，确定/右键生成；
    - 内轮廓：无需预选曲线、无偏移距离，鼠标悬停到闭合区域内部即预览
      区域边界，点击画布直接提取该区域轮廓；面板隐藏"偏移距离"与"确定"
      按钮（所见即所得），右键/Esc 结束。

    信号：
    - paramsChanged(): 距离/重建/公差任一变化 → 命令端重算预览
    - modeChanged(str): 模式切换（"outer"/"inner"）→ 命令端切换交互
    - confirmed(): 点击"确定"（外轮廓模式）生成包裹线
    - cancelRequested(): 点击"取消"
    """
    paramsChanged = Signal()
    modeChanged = Signal(str)
    confirmed = Signal()
    cancelRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        # 轮廓模式：外轮廓（原有功能）/ 内轮廓（点击拾取）
        modeRow = QHBoxLayout()
        modeRow.setSpacing(4)
        lab = QLabel("轮廓模式")
        lab.setMinimumWidth(_LABEL_W)
        self._rbOuter = QRadioButton("外轮廓")
        self._rbInner = QRadioButton("内轮廓")
        self._rbOuter.setChecked(True)
        self._modeGroup = QButtonGroup(self)
        self._modeGroup.addButton(self._rbOuter, 0)
        self._modeGroup.addButton(self._rbInner, 1)
        modeRow.addWidget(lab)
        modeRow.addWidget(self._rbOuter)
        modeRow.addWidget(self._rbInner)
        self._layout.addLayout(modeRow)

        self._spinDist = self._spin(2.0, decimals=3, step=0.5)
        self._chkRebuild = QCheckBox("抽稀去密集点（保形状）")
        self._chkRebuild.setChecked(True)
        self._spinTol = self._spin(0.01, decimals=3, step=0.005)
        form = QFormLayout()
        # 偏移距离整行装入容器，内轮廓模式整体隐藏（内轮廓无偏移距离）
        self._distBox = QWidget()
        _dl = QHBoxLayout(self._distBox)
        _dl.setContentsMargins(0, 0, 0, 0)
        _dl.setSpacing(4)
        _labDist = QLabel("偏移距离")
        _labDist.setMinimumWidth(_LABEL_W)
        _dl.addWidget(_labDist)
        _dl.addWidget(self._spinDist, 1)
        form.addRow(self._distBox)
        form.addRow(self._chkRebuild)
        form.addRow("抽稀公差", self._spinTol)
        self._layout.addLayout(form)

        # 模式操作提示（随切换更新）
        self._hint = QLabel("外轮廓：选中曲线后预览包裹线，确定/右键生成")
        self._hint.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        self._hint.setWordWrap(True)
        self._layout.addWidget(self._hint)

        btnRow = QHBoxLayout()
        self._btnOk = self._button("确定", primary=True)
        self._btnOk.clicked.connect(self.confirmed.emit)
        btnCancel = self._button2("取消")
        btnCancel.clicked.connect(self.cancelRequested.emit)
        btnRow.addWidget(self._btnOk)
        btnRow.addWidget(btnCancel)
        self._layout.addLayout(btnRow)
        self._layout.addStretch(1)

        self._spinDist.valueChanged.connect(self._emitChanged)
        self._chkRebuild.stateChanged.connect(self._emitChanged)
        self._spinTol.valueChanged.connect(self._emitChanged)
        self._modeGroup.idClicked.connect(self._onModeClicked)

    def _onModeClicked(self, mid: int) -> None:
        """模式切换：同步单选状态、更新提示与确定按钮显隐，通知命令切换交互。"""
        if mid == 1:
            self._rbInner.setChecked(True)
            self._distBox.hide()   # 内轮廓无偏移距离
            self._hint.setText("内轮廓：点击闭合区域内部，提取该区域轮廓，右键/Esc 结束")
            self._btnOk.hide()
        else:
            self._rbOuter.setChecked(True)
            self._distBox.show()
            self._hint.setText("外轮廓：选中曲线后预览包裹线，确定/右键生成")
            self._btnOk.show()
        self.modeChanged.emit("inner" if mid == 1 else "outer")

    def _emitChanged(self, *_) -> None:
        self.paramsChanged.emit()

    def mode(self) -> str:
        return "inner" if self._rbInner.isChecked() else "outer"

    def setMode(self, mode: str) -> None:
        """命令端同步模式（如命令重启时保持上次选择），同时通知命令切换交互。"""
        target = 1 if mode == "inner" else 0
        if self._modeGroup.checkedId() == target:
            return
        self._onModeClicked(target)

    def distance(self) -> float:
        return self._spinDist.value()

    def rebuild(self) -> bool:
        return self._chkRebuild.isChecked()

    def tolerance(self) -> float:
        return self._spinTol.value()

    def setHint(self, text: str) -> None:
        pass


# ==================== Align 对齐 ====================

class AlignParamPanel(ParamPanelBase):
    """对齐面板：上/下/左/右/居中五个按钮 + 均匀分布（X/Y 均匀勾选）。

    信号：
    - alignRequested(str): 对齐方式（"top"/"bottom"/"left"/"right"/"center"）
    - distributeRequested(): 点击"均匀分布"（按勾选的 X/Y 均匀执行）
    - cancelRequested(): 点击"取消"
    """
    alignRequested = Signal(str)
    distributeRequested = Signal()
    cancelRequested = Signal()

    # 网格布局位置（方向示意：上在中上，左/中/右一行，下在中下）
    _BUTTONS = (
        ("上对齐", "top", 0, 1),
        ("左对齐", "left", 1, 0),
        ("居中对齐", "center", 1, 1),
        ("右对齐", "right", 1, 2),
        ("下对齐", "bottom", 2, 1),
    )

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        grid = QGridLayout()
        grid.setSpacing(4)
        for text, mode, row, col in self._BUTTONS:
            b = self._button(text)
            b.setMinimumHeight(36)
            b.clicked.connect(lambda _, m=mode: self.alignRequested.emit(m))
            grid.addWidget(b, row, col)
        self._layout.addLayout(grid)

        # 均匀分布：X 均匀 / Y 均匀 勾选 + 执行按钮
        distRow = QHBoxLayout()
        self._chkX = QCheckBox("X 均匀")
        self._chkX.setChecked(True)
        self._chkY = QCheckBox("Y 均匀")
        btnDist = self._button("均匀分布", primary=True)
        btnDist.setMinimumHeight(36)
        btnDist.clicked.connect(self.distributeRequested.emit)
        distRow.addWidget(self._chkX)
        distRow.addWidget(self._chkY)
        distRow.addWidget(btnDist)
        self._layout.addLayout(distRow)

        btnCancel = self._button2("取消")
        btnCancel.clicked.connect(self.cancelRequested.emit)
        self._layout.addWidget(btnCancel)
        self._layout.addStretch(1)

    def xUniform(self) -> bool:
        return self._chkX.isChecked()

    def yUniform(self) -> bool:
        return self._chkY.isChecked()

    def setHint(self, text: str) -> None:
        pass
# ==================== PanelScale 面板缩放（三点+数值目标长度）====================

class PanelScaleParamPanel(ParamPanelBase):
    """缩放参数面板：点完基准点(P1)与参考起点(P2)后，
    显示当前参考长度和实时比例，并允许输入目标长度直接完成缩放。

    规则：第一个点(P1)不动，方向沿 P1→P2；输入目标长度 L' 后，
    新参考点 P3 = P1 + 单位(P1→P2) × L'，比例 k = L' / |P1P2|，
    直接对所有选中对象绕 P1 做等比缩放并结束命令。"""

    paramsChanged = Signal()              # 选项变化：通知命令重绘预览
    confirmed = Signal()                  # 用户按「应用目标长度」或在数值框回车
    cancelRequested = Signal()            # 取消命令
    modeChanged = Signal(str)             # "point" / "object"
    objectConfirmed = Signal()            # 对象模式「确认」

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._objs: list = []
        self._loading: bool = False

        # ---- 模式切换：点模式（默认）/ 对象模式 ----
        rowMode = QHBoxLayout()
        rowMode.setSpacing(4)
        self._btnPoint = self._button("点模式", primary=True)
        self._btnObject = self._button2("对象模式")
        for b in (self._btnPoint, self._btnObject):
            b.setCheckable(True)
            b.setMinimumHeight(24)
        self._btnPoint.setChecked(True)
        self._btnPoint.setToolTip("三点缩放：基准点 → 参考起点 → 目标点/目标长度")
        self._btnObject.setToolTip(
            "对象模式：选基准对象，按其包围盒尺寸输入目标值，"
            "其余选中对象按同一比例跟随缩放")
        # 不做 QButtonGroup：checkable QPushButton 不进面板状态持久化，
        # 互斥置位完全由 _setMode 负责（避免恢复状态与堆栈页不一致）
        # 选中态高亮（按钮即开关）
        for b in (self._btnPoint, self._btnObject):
            b.setStyleSheet(
                "QPushButton { background:%s; color:%s; border:1px solid %s;"
                " border-radius:2px; padding:3px 8px; }"
                "QPushButton:checked { background:%s; color:%s;"
                " border-color:%s; }"
                % (theme.BG_PANEL, theme.TEXT, theme.BORDER,
                   theme.PRIMARY, theme.TEXT_ON_PRIMARY, theme.PRIMARY))
        self._btnPoint.clicked.connect(lambda: self._setMode("point"))
        self._btnObject.clicked.connect(lambda: self._setMode("object"))
        rowMode.addWidget(self._btnPoint, 1)
        rowMode.addWidget(self._btnObject, 1)
        self._layout.addLayout(rowMode)

        self._stack = QStackedWidget()

        # ---------- 点模式页 ----------
        pagePoint = QWidget()
        layPoint = QVBoxLayout(pagePoint)
        layPoint.setContentsMargins(0, 0, 0, 0)
        layPoint.setSpacing(4)
        self._labRef = QLabel("参考长度：—")
        self._labRef.setStyleSheet("color:%s;" % theme.TEXT)
        layPoint.addWidget(self._labRef)
        self._labRatio = QLabel("当前比例：—")
        self._labRatio.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        layPoint.addWidget(self._labRatio)
        row = QHBoxLayout()
        row.setSpacing(4)
        lab = QLabel("目标长度")
        lab.setMinimumWidth(_LABEL_W)
        row.addWidget(lab)
        self._spinTarget = self._spin(value=100.0, decimals=3, step=1.0)
        self._spinTarget.setRange(1e-6, 9999999.0)
        self._spinTarget.setToolTip(
            "点完基准点和参考起点后填入目标长度。\n"
            "第一个点不动，沿P1→P2方向把P2拉到新长度，立即执行缩放。")
        row.addWidget(self._spinTarget, 1)
        layPoint.addLayout(row)
        btnApply = self._button("应用目标长度", primary=True)
        btnApply.setMinimumHeight(32)
        btnApply.clicked.connect(self.confirmed.emit)
        layPoint.addWidget(btnApply)
        self._hint = QLabel(
            "① 点基准点 → ② 点参考起点 → ③ 拖动预览或填目标长度")
        self._hint.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        self._hint.setWordWrap(True)
        layPoint.addWidget(self._hint)
        layPoint.addStretch(1)
        self._spinTarget.lineEdit().returnPressed.connect(self.confirmed.emit)
        self._stack.addWidget(pagePoint)

        # ---------- 对象模式页 ----------
        pageObj = QWidget()
        layObj = QVBoxLayout(pageObj)
        layObj.setContentsMargins(0, 0, 0, 0)
        layObj.setSpacing(4)
        # 基准对象：画布点选（不用下拉）
        self._labRefObj = QLabel("基准：未选择（在画布上点选对象）")
        self._labRefObj.setStyleSheet("color:%s;" % theme.TEXT)
        self._labRefObj.setWordWrap(True)
        self._labRefObj.setToolTip(
            "在画布上点选对象作为准确尺寸基准；其尺寸按输入值精确变化，"
            "其余选中对象按同一比例跟随缩放（绕基准对象包围盒中心）")
        layObj.addWidget(self._labRefObj)
        self._refObj = None

        self._dimSpins: list[QDoubleSpinBox] = []
        self._ratioChecks: list[QCheckBox] = []
        for ax, tip in (
                ("X", "基准对象的 X 尺寸（包围盒宽度）"),
                ("Y", "基准对象的 Y 尺寸（包围盒高度）"),
                ("Z", "基准对象的 Z 尺寸（包围盒厚度）")):
            row = QWidget()
            row.setFixedHeight(28)
            row.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(3)
            lab = QLabel(ax)
            lab.setFixedWidth(14)
            lab.setToolTip(tip)
            rl.addWidget(lab)
            sp = self._spin(value=0.0, decimals=3, step=1.0)
            sp.setRange(1e-6, 9999999.0)
            sp.setToolTip(tip)
            sp.lineEdit().returnPressed.connect(self.objectConfirmed.emit)
            rl.addWidget(sp, 1)
            self._dimSpins.append(sp)
            uc = QCheckBox("等比")
            uc.setFixedHeight(24)
            # 三轴互斥：勾选某轴等比 = 该轴为主动轴，其余轴按同一比例跟随
            uc.stateChanged.connect(
                lambda _s, i=len(self._ratioChecks): self._onRatioToggled(i))
            rl.addWidget(uc)
            self._ratioChecks.append(uc)
            layObj.addWidget(row)

        self._labObjHint = QLabel(
            "改尺寸后按确认：基准对象精确到目标尺寸，其余对象同比例跟随")
        self._labObjHint.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        self._labObjHint.setWordWrap(True)
        layObj.addWidget(self._labObjHint)
        btnObjOk = self._button("确认", primary=True)
        btnObjOk.setMinimumHeight(32)
        btnObjOk.clicked.connect(self.objectConfirmed.emit)
        layObj.addWidget(btnObjOk)
        layObj.addStretch(1)
        self._stack.addWidget(pageObj)

        self._layout.addWidget(self._stack)

        btnCancel = self._button2("取消")
        btnCancel.clicked.connect(self.cancelRequested.emit)
        self._layout.addWidget(btnCancel)
        self._layout.addStretch(1)

    # ---------- 模式 ----------

    def mode(self) -> str:
        return "object" if self._btnObject.isChecked() else "point"

    def _setMode(self, mode: str) -> None:
        point = (mode == "point")
        if self._btnPoint.isChecked() == point and \
                self._btnObject.isChecked() == (not point):
            return
        self._btnPoint.setChecked(point)
        self._btnObject.setChecked(not point)
        self._stack.setCurrentIndex(0 if point else 1)
        self.modeChanged.emit(mode)

    # ---------- 对象模式 ----------

    def setRefObject(self, obj) -> None:
        """命令在画布上点选对象后调用：设为基准并回填其包围盒尺寸。"""
        self._refObj = obj
        if obj is None:
            self._labRefObj.setText("基准：未选择（在画布上点选对象）")
        else:
            try:
                t = obj.type()
                name = getattr(t, "name", "") or str(t)
            except Exception:
                name = ""
            self._labRefObj.setText(f"基准：{name}" if name else "基准：已选择")
        self._refreshDims()

    def refObject(self):
        return self._refObj

    def objectDims(self) -> tuple:
        return tuple(float(sp.value()) for sp in self._dimSpins)

    def ratioAxis(self) -> int:
        """当前勾选“等比”的轴（0/1/2）；未勾选返回 -1。"""
        for i, uc in enumerate(self._ratioChecks):
            if uc.isChecked():
                return i
        return -1

    def _onRatioToggled(self, i: int) -> None:
        """三轴等比互斥；勾选的轴为唯一可输入轴，其余变灰跟随。"""
        if self._loading:
            return
        uc = self._ratioChecks[i]
        self._loading = True
        try:
            if uc.isChecked():
                for j, other in enumerate(self._ratioChecks):
                    if j != i and other.isChecked():
                        other.setChecked(False)
        finally:
            self._loading = False
        anyRatio = self._ratioChecks[i].isChecked()
        for j, sp in enumerate(self._dimSpins):
            sp.setEnabled(not anyRatio or self._ratioChecks[j].isChecked())

    def _refreshDims(self) -> None:
        obj = self._refObj
        bb = None
        if obj is not None:
            try:
                bb = obj.getBoundingBox()
            except Exception:
                bb = None
        self._loading = True
        try:
            self._dimSpins[0].setEnabled(True)
            self._dimSpins[1].setEnabled(True)
            self._dimSpins[2].setEnabled(True)
            if bb is None or not bb.isValid():
                for sp in self._dimSpins:
                    sp.setValue(0.0)
            else:
                self._dimSpins[0].setValue(bb.width())
                self._dimSpins[1].setValue(bb.height())
                self._dimSpins[2].setValue(bb.depth())
            # 浮雕模型 Z 尺寸由高度场决定，放置缩放的 sz 被忽略
            isRelief = False
            if obj is not None:
                try:
                    from core.vector.object_type import ObjectType
                    isRelief = (obj.type() == ObjectType.SURFACE)
                except Exception:
                    isRelief = False
            self._dimSpins[2].setToolTip(
                "浮雕模型的 Z 尺寸请在变换面板“模型Z”修改（高度场缩放）"
                if isRelief else "基准对象的 Z 尺寸（包围盒厚度）")
            r = self.ratioAxis()
            for j, sp in enumerate(self._dimSpins):
                if j == 2 and isRelief:
                    sp.setEnabled(False)
                elif r >= 0:
                    # 勾选等比：只有主动轴可输入，其余灰显跟随
                    sp.setEnabled(self._ratioChecks[j].isChecked())
                else:
                    sp.setEnabled(True)
        finally:
            self._loading = False

    # ---------- 命令调用 ----------

    def setReferenceLength(self, length: float | None) -> None:
        """命令在点击 P2 后调用：显示参考长度。"""
        if length is None or length <= 0:
            self._labRef.setText("参考长度：—")
        else:
            self._labRef.setText(f"参考长度：{length:.3f}")

    def setCurrentRatio(self, ratio: float | None) -> None:
        """命令在 P2 之后的鼠标移动时调用：显示当前光标对应的比例。"""
        if ratio is None:
            self._labRatio.setText("当前比例：—")
        else:
            self._labRatio.setText(f"当前比例：{ratio:.4f}×")

    def targetLength(self) -> float:
        return float(self._spinTarget.value())

    def setTargetLength(self, value: float) -> None:
        self._spinTarget.setValue(max(1e-6, float(value)))

    def setHint(self, text: str) -> None:
        self._hint.setText(text)


# ==================== ImageToModel 图片转模型 ====================

class ImageToModelParamPanel(ParamPanelBase):
    """图片转浮雕模型面板（简化版：两阶段、7 参数、2 按钮）。

    阶段一 (图像处理 → 覆盖原图)：
        超分倍率 + 磨光强度 + 边缘阈值 + 细节增强
        → 输出处理后灰度 uint8，替换掉当前选中 ImageObject 的图像数据（可撤销）。
    阶段二 (生成模型)：
        浮雕深度 + 网格采样 + 像素间距
        → 从当前（处理好的）ImageObject 直接生成 HeightField 并加入文档。

    约定：图片已经通过"放置图片"命令导入软件 (ImageObject)，用户选中后命令自动绑定。
    """
    # 外部触发：请求把文档当前选中对象回填到面板
    pickSelectedRequested = Signal()
    # 阶段一：处理图片（覆盖原图）
    processImageRequested = Signal()
    # 阶段二：从已处理图生成模型
    generateModelRequested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QProgressBar

        # --- 0. 当前选中图片信息（只读） ---
        gbxImg = QGroupBox("当前选中图片")
        gbxImg.setStyleSheet(self._groupBoxQss())
        layImg = QFormLayout(gbxImg)
        layImg.setContentsMargins(6, 10, 6, 6)
        layImg.setVerticalSpacing(4)
        self._labImgName = QLabel("（未选中图片对象）")
        self._labImgName.setWordWrap(True)
        self._labImgName.setStyleSheet("color:%s;" % theme.TEXT_DIM)
        layImg.addRow("对象", self._labImgName)
        self._labImgPixels = QLabel("--")
        self._labImgPixels.setWordWrap(True)
        self._labImgPixels.setStyleSheet("color:%s;" % theme.PRIMARY_DARK)
        layImg.addRow("像素", self._labImgPixels)
        self._labImgWorld = QLabel("--")
        self._labImgWorld.setWordWrap(True)
        self._labImgWorld.setStyleSheet("color:%s;" % theme.PRIMARY_DARK)
        layImg.addRow("放置大小", self._labImgWorld)
        rowBtn = QHBoxLayout()
        rowBtn.setSpacing(4)
        self._btnPickSelected = self._button2("取当前选中图片")
        self._btnPickSelected.setToolTip("从画布当前选中对象中取图片（须为 ImageObject 类型）")
        self._btnPickSelected.clicked.connect(self.pickSelectedRequested.emit)
        rowBtn.addWidget(self._btnPickSelected, 1)
        layImg.addRow(rowBtn)
        self._layout.addWidget(gbxImg)

        # --- 1. 图像处理（阶段一） ---
        gbxP = QGroupBox("图像处理（参数）")
        gbxP.setStyleSheet(self._groupBoxQss())
        layP = QFormLayout(gbxP)
        layP.setContentsMargins(6, 10, 6, 6)
        layP.setVerticalSpacing(4)
        self._spinScale = self._ispin(1, 1, 4)
        self._spinScale.setToolTip("超分放大倍率 (1~4)：用 LANCZOS4 高质量插值提高细节分辨率")
        layP.addRow("超分倍率", self._spinScale)
        self._spinSmooth = self._ispin(30, 0, 100)
        self._spinSmooth.setToolTip("边缘保护磨光强度 (0=关)：平缓区磨平台阶、边缘线条保持锐利")
        layP.addRow("磨光强度", self._spinSmooth)
        self._spinEdgeThr = self._ispin(8, 1, 50)
        self._spinEdgeThr.setToolTip("边缘保护阈值 (1~50)：越大越倾向于保留锐利边缘")
        layP.addRow("边缘阈值", self._spinEdgeThr)
        self._spinDetail = self._ispin(0, 0, 100)
        self._spinDetail.setToolTip("细节增强 (0=关)：CLAHE+Unsharp，加深浮雕层次感")
        layP.addRow("细节增强", self._spinDetail)
        self._layout.addWidget(gbxP)

        # --- 2. 3D 参数（阶段二） ---
        gbx3 = QGroupBox("3D 参数（生成模型）")
        gbx3.setStyleSheet(self._groupBoxQss())
        lay3 = QFormLayout(gbx3)
        lay3.setContentsMargins(6, 10, 6, 6)
        lay3.setVerticalSpacing(4)
        self._spinDepth = self._spin(2.0, decimals=2, step=0.1)
        self._spinDepth.setRange(0.05, 50.0)
        self._spinDepth.setToolTip("浮雕总深度 (mm)：最大凸起高度，越白越高")
        lay3.addRow("浮雕深度", self._spinDepth)
        self._spinGridSampling = self._ispin(1, 1, 8)
        self._spinGridSampling.setToolTip("网格采样 (1~8)：1=每像素一个栅格，2=每2像素一个栅格（降采样，减少模型三角数）")
        lay3.addRow("网格采样", self._spinGridSampling)
        self._spinPixel = self._spin(0.1, decimals=4, step=0.005)
        self._spinPixel.setRange(0.0005, 100.0)
        self._spinPixel.setToolTip("像素间距 (mm/px)：生成 HeightField 时的栅格间距；默认按图片放置大小自动换算。勾选“尺寸匹配原图”后，生成的模型会按画布图片放置尺寸严格对齐。")
        lay3.addRow("像素间距", self._spinPixel)
        self._chkFitSize = QCheckBox("尺寸匹配原图")
        self._chkFitSize.setChecked(True)
        self._chkFitSize.setToolTip("默认勾选：生成的浮雕模型物理尺寸（宽×高 mm）严格等于图片在画布上的放置尺寸，放大倍率×4/网格降采样都只改变栅格密度而不改变整体大小。")
        lay3.addRow("", self._chkFitSize)
        self._labModelSize = QLabel("-- × -- mm")
        self._labModelSize.setWordWrap(True)
        self._labModelSize.setStyleSheet("color:%s;" % theme.PRIMARY_DARK)
        lay3.addRow("预估模型尺寸", self._labModelSize)
        self._layout.addWidget(gbx3)

        # 尺寸变化联动
        for w in (self._spinScale, self._spinGridSampling, self._spinPixel):
            w.valueChanged.connect(lambda _v: self._refreshEstimate())
        self._chkFitSize.stateChanged.connect(lambda _s: self._refreshEstimate())

        # --- 3. 进度条 + 2 个主按钮 ---
        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        self._progress.setFixedHeight(18)
        self._layout.addWidget(self._progress)

        btnRow = QHBoxLayout()
        btnRow.setSpacing(4)
        self._btnProcess = self._button2("处理图片")
        self._btnProcess.setMinimumHeight(32)
        self._btnProcess.setToolTip("按当前图像处理参数（超分/磨光/阈值/细节）跑一遍，结果覆盖掉当前选中的原图（可撤销）")
        self._btnProcess.clicked.connect(self.processImageRequested.emit)
        btnRow.addWidget(self._btnProcess, 1)

        self._btnGenerate = self._button("生成模型", primary=True)
        self._btnGenerate.setMinimumHeight(32)
        self._btnGenerate.setToolTip("从当前（处理好的）图片按 3D 参数生成 HeightField 浮雕模型（一次撤销）")
        self._btnGenerate.clicked.connect(self.generateModelRequested.emit)
        btnRow.addWidget(self._btnGenerate, 1)
        self._layout.addLayout(btnRow)

        self._layout.addStretch(1)

        # 状态
        self._imgShape: tuple = (0, 0)  # (H, W) 当前 ImageObject 的像素
        self._currentImageObj = None
        self._refreshEstimate()

    # ---------- 对外：参数 / 绑定 / 进度 ----------
    def imageProcessParams(self):
        """阶段一参数（覆盖原图用）。"""
        from core.relief.image_to_model import ImageProcessParams
        return ImageProcessParams(
            scale=int(self._spinScale.value()),
            blackThreshold=8,          # 固定默认：保持图像-高度映射一致
            clipPercent=0.5,           # 固定默认：拉伸对比度
            smoothStrength=int(self._spinSmooth.value()),
            edgeThreshold=int(self._spinEdgeThr.value()),
            detailEnhance=int(self._spinDetail.value()),
            invert=False,
            output01Height=True,       # 重要：输出即"白=高"的 0~255 映射，阶段二直接消费
        )

    def heightFieldParams(self):
        """阶段二参数（生成 HeightField 用）。"""
        from core.relief.image_to_model import HeightFieldParams
        return HeightFieldParams(
            depthMm=float(self._spinDepth.value()),
            pixelSizeMm=float(self._spinPixel.value()),
            baseHeight=0.0,
            blackThreshold=0,         # 阶段一已正确输出背景为纯黑 0，这里不要再二次截断
            autoCenter=False,         # 由命令层按图片几何平移对齐
            gridSampling=int(self._spinGridSampling.value()),
        )

    def currentImageObject(self):
        return getattr(self, "_currentImageObj", None)

    def attachImageObject(self, obj) -> bool:
        from core.vector.image_object import ImageObject
        if not isinstance(obj, ImageObject):
            self._currentImageObj = None
            self._labImgName.setText("（未选中图片对象）")
            self._labImgName.setStyleSheet("color:%s;" % theme.TEXT_DIM)
            self._labImgPixels.setText("--")
            self._labImgWorld.setText("--")
            self._imgShape = (0, 0)
            self._refreshEstimate()
            return False
        self._currentImageObj = obj
        pw, ph = obj.pixelSize()
        self._imgShape = (int(ph), int(pw))
        oid = str(obj.id()) if (hasattr(obj, 'id') and callable(getattr(obj, 'id', None))) else str(id(obj))
        name = oid
        if hasattr(obj, 'name') and callable(getattr(obj, 'name', None)):
            try:
                nm = obj.name()
                if nm:
                    name = nm
            except Exception:
                pass
        # 对象名可能是含路径的长文件名（无空格无法换行），直接显示会把
        # 表单最小宽度顶开、导致整行参数溢出面板——这里按像素宽度省略中段，
        # 完整名称放 tooltip。先套样式（粗体更宽），再按像素截断。
        self._labImgName.setStyleSheet("color:%s; font-weight:bold;" % theme.TEXT)
        fm = self._labImgName.fontMetrics()
        shown = fm.elidedText(str(name).replace("\\", "/").split("/")[-1],
                              Qt.ElideMiddle, 140)
        self._labImgName.setText(shown)
        self._labImgName.setToolTip(f"{name}\n对象ID: {oid}")
        self._labImgPixels.setText(f"{pw} × {ph}  px")
        self._labImgWorld.setText(f"{obj.width():.2f} × {obj.height():.2f} mm")
        # 自动换算像素间距：按放置大小 ÷ (像素-1) 平均
        sp_w = obj.width() / max(pw - 1, 1)
        sp_h = obj.height() / max(ph - 1, 1)
        sp = (sp_w + sp_h) * 0.5
        sp = max(1e-4, min(100.0, float(sp)))
        self._spinPixel.blockSignals(True)
        self._spinPixel.setValue(round(sp, 4))
        self._spinPixel.blockSignals(False)
        self._refreshEstimate()
        return True

    def setProgress(self, pct: int, msg: str = "") -> None:
        self._progress.setValue(max(0, min(100, int(pct))))
        self._progress.setFormat(f"{pct}% - {msg}" if msg else "%p%")

    def setEnabledButtons(self, enabled: bool) -> None:
        for b in (self._btnPickSelected, self._btnProcess, self._btnGenerate):
            b.setEnabled(enabled)

    # ---------- 内部 ----------
    def _groupBoxQss(self) -> str:
        return (
            "QGroupBox { border:1px solid %s; border-radius:3px; margin-top:6px;"
            " padding-top:2px; font-weight:bold; color:%s; }"
            " QGroupBox::title { subcontrol-origin: margin; left:6px; padding:0 3px; color:%s; }"
            % (theme.BORDER, theme.PRIMARY_DARK, theme.PRIMARY_DARK)
        )

    def fitSizeChecked(self) -> bool:
        return bool(getattr(self, "_chkFitSize", None) and self._chkFitSize.isChecked())

    def _refreshEstimate(self) -> None:
        """预估生成模型的物理尺寸 / 栅格 / 内存。

        考虑了 超分(scale) 和 网格降采样(gridSampling)：
            最终栅格尺寸 = ceil(N*scale / gs)
            若勾选“尺寸匹配原图”，模型尺寸 = 图片放置尺寸（超分/降采样只改变栅格密度）。
        """
        if not self._imgShape or self._imgShape[0] < 2 or self._imgShape[1] < 2:
            self._labModelSize.setText("-- × -- mm")
            return
        ny, nx = self._imgShape
        scale = int(max(1, self._spinScale.value()))
        gs = int(max(1, self._spinGridSampling.value()))
        sp_original = float(self._spinPixel.value())
        nx2, ny2 = nx * scale, ny * scale
        gx = (nx2 + gs - 1) // gs
        gy = (ny2 + gs - 1) // gs
        sp_final = sp_original * scale / gs
        Wmm = (gx - 1) * sp_final
        Hmm = (gy - 1) * sp_final
        mb = gx * gy * 4 / (1024.0 * 1024.0)
        if self.fitSizeChecked() and self._currentImageObj is not None:
            try:
                obj_w = float(self._currentImageObj.width())
                obj_h = float(self._currentImageObj.height())
                # 按宽方向严格 = obj_w 重新推导 spacing：ns = obj_w / (gx-1)
                # 高方向自动 = (gy-1)*ns ≈ obj_h（像素/放置比例一致时 ≈ 目标）
                ns = obj_w / max(gx - 1, 1)
                if ns <= 0:
                    ns = obj_h / max(gy - 1, 1)
                sp_final = ns
                Wmm = (gx - 1) * sp_final
                Hmm = (gy - 1) * sp_final
            except Exception:
                pass
        self._labModelSize.setText(
            f"{Wmm:.1f} × {Hmm:.1f} mm   ({gx}×{gy} @ {sp_final:.4f} mm, {mb:.1f} MB)")


# ==================== Extrude（挤出） ====================

class ExtrudeParamPanel(ParamPanelBase):
    """挤出参数面板：闭合轮廓沿 +Z 拉伸为实体，带拔模角度。

    两步式：点击曲线选中 → 调参数实时预览 → 点"确认"/右键挤出 → 继续下一条；
    点"结束"/Esc 退出命令。参数改动发 paramsChanged 刷新预览。
    """

    confirmed = Signal()        # 点击"确认"/右键：挤出当前预览曲线
    finished = Signal()         # 点击"结束"/Esc：结束命令
    paramsChanged = Signal()    # 任一参数变更（实时预览）

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        gEx, lEx = self._group("挤出参数")

        self._spinH = self._spin(10.0, decimals=2, step=1.0)
        self._spinH.setRange(0.01, 99999.0)
        lEx.addWidget(self._row("高度", self._spinH))

        self._spinDraft = self._spin(0.0, decimals=1, step=1.0)
        self._spinDraft.setRange(-89.0, 89.0)
        lEx.addWidget(self._row("拔模角度", self._spinDraft))

        tip = QLabel("角度＞0 顶面向内收，＜0 向外扩")
        tip.setStyleSheet("color:%s; font-size:9pt;" % theme.TEXT_DIM)
        lEx.addWidget(tip)
        self._layout.addWidget(gEx)

        # ---- 确认（挤出当前预览）/ 结束（退出命令）----
        btnConfirm = self._button("确认", primary=True)
        btnConfirm.clicked.connect(self.confirmed.emit)
        self._layout.addWidget(btnConfirm)
        btnEnd = self._button("结束")
        btnEnd.clicked.connect(self.finished.emit)
        self._layout.addWidget(btnEnd)
        self._layout.addStretch(1)

        self._spinH.valueChanged.connect(lambda *_: self.paramsChanged.emit())
        self._spinDraft.valueChanged.connect(lambda *_: self.paramsChanged.emit())

    # ---------- 布局小工具（同区域塑形等面板惯例） ----------

    def _group(self, title: str):
        g = QGroupBox(title)
        lay = QVBoxLayout(g)
        lay.setSpacing(3)
        lay.setContentsMargins(6, 4, 6, 4)
        return g, lay

    def _row(self, label: str, widget) -> QWidget:
        """标签 + 控件行（整体 QWidget，便于整行启用/禁用）。"""
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lab = QLabel(label)
        lab.setMinimumWidth(_LABEL_W)
        lay.addWidget(lab)
        lay.addWidget(widget, 1)
        return row

    def height(self) -> float:
        return self._spinH.value()

    def draftAngle(self) -> float:
        return self._spinDraft.value()


# ==================== Primitive 标准体 ====================

class PrimitiveParamPanel(ParamPanelBase):
    """标准体参数面板：立方体/圆柱体/圆环/圆锥体（圆台）。

    选形状 → 调尺寸/拔模/边处理 → 画布单击落点生成（命令保持激活可
    连放）。参数改动发 paramsChanged 刷新实时预览；确认/结束按钮与
    挤出面板同风格。
    """

    confirmed = Signal()        # 点击"创建"：在当前落点生成
    finished = Signal()         # 点击"结束"：退出命令
    paramsChanged = Signal()    # 任一参数变更（实时预览）

    SHAPE_KEYS = ("box", "cylinder", "torus", "cone")
    SHAPE_LABELS = ("立方体", "圆柱体", "圆环", "圆锥体")
    SHAPE_NAMES = {"box": "立方体", "cylinder": "圆柱体",
                   "torus": "圆环", "cone": "圆锥体"}

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        # ---- 形状单选（2+2 两行，避免一行 4 项溢出面板宽） ----
        self._shapeGroup = QButtonGroup(self)
        self._layout.addWidget(
            self._radioRow(None, self.SHAPE_LABELS[:2], self._shapeGroup, 0))
        self._layout.addWidget(
            self._radioRow(None, self.SHAPE_LABELS[2:], self._shapeGroup, 2))
        self._shapeGroup.button(0).setChecked(True)

        # ---- 尺寸组（行按形状显隐） ----
        g, lay = self._group("尺寸")

        def spin(v, lo=0.01, hi=99999.0, dec=2, step=1.0):
            s = self._spin(v, decimals=dec, step=step)
            s.setRange(lo, hi)
            s.setMinimumWidth(62)
            return s

        self._spinLen = spin(50.0)      # box 长 X
        self._spinWid = spin(50.0)      # box 宽 Y
        self._spinH = spin(20.0)        # box/cyl/cone 高
        self._spinDia = spin(40.0)      # cylinder 直径
        self._spinD1 = spin(40.0)       # cone 底直径
        self._spinD2 = spin(0.0, 0.0)   # cone 顶直径（0=尖锥）
        self._spinMaj = spin(60.0)      # torus 环中心直径
        self._spinMin = spin(20.0)      # torus 管直径
        self._rowLen = self._row("长 X", self._spinLen)
        self._rowWid = self._row("宽 Y", self._spinWid)
        self._rowH = self._row("高度", self._spinH)
        self._rowDia = self._row("直径", self._spinDia)
        self._rowD1 = self._row("底直径", self._spinD1)
        self._rowD2 = self._row("顶直径", self._spinD2)
        self._rowMaj = self._row("环径", self._spinMaj)
        self._rowMin = self._row("管径", self._spinMin)
        for row in (self._rowLen, self._rowWid, self._rowH, self._rowDia,
                    self._rowD1, self._rowD2, self._rowMaj, self._rowMin):
            lay.addWidget(row)

        self._spinDraft = self._spin(0.0, decimals=1)
        self._spinDraft.setRange(-89.0, 89.0)
        self._spinDraft.setMinimumWidth(62)
        self._rowDraft = self._row("拔模角度", self._spinDraft)
        lay.addWidget(self._rowDraft)
        tip = QLabel("＞0 顶收，＜0 顶扩")
        tip.setStyleSheet("color:%s; font-size:9pt;" % theme.TEXT_DIM)
        tip.setToolTip("角度＞0 顶面向内收，＜0 向外扩；\n"
                       "过大时自动收到当前尺寸的限制值")
        lay.addWidget(tip)

        # 圆环轴向
        self._axisGroup = QButtonGroup(self)
        self._rowAxis = self._radioRow("轴向", ("Z 水平", "X", "Y"),
                                       self._axisGroup, 0)
        self._axisGroup.button(0).setChecked(True)
        lay.addWidget(self._rowAxis)
        self._layout.addWidget(g)

        # ---- 边处理组（圆环隐藏） ----
        gE, layE = self._group("边处理")
        self._edgeGroup = QButtonGroup(self)
        layE.addWidget(self._radioRow(None, ("无", "倒角", "圆角"),
                                      self._edgeGroup, 0))
        self._edgeGroup.button(0).setChecked(True)
        self._spinEdge = spin(0.5, 0.0)
        layE.addWidget(self._row("数值", self._spinEdge))
        self._scopeGroup = QButtonGroup(self)
        layE.addWidget(self._radioRow(None, ("仅顶边", "顶+底边", "全部边"),
                                      self._scopeGroup, 0))
        self._scopeGroup.button(0).setChecked(True)
        self._groupEdge = gE
        self._layout.addWidget(gE)

        # ---- 放置组 ----
        gP, layP = self._group("放置")
        self._spinZ = spin(0.0, -99999.0)
        layP.addWidget(self._row("Z 基准", self._spinZ))
        self._spinRot = self._spin(0.0, decimals=1, step=5.0)
        self._spinRot.setRange(-360.0, 360.0)
        self._spinRot.setMinimumWidth(62)
        layP.addWidget(self._row("旋转", self._spinRot))
        self._alignGroup = QButtonGroup(self)
        layP.addWidget(self._radioRow(None, ("底面", "中心"),
                                      self._alignGroup, 0))
        self._alignGroup.button(0).setChecked(True)
        gP.setToolTip("Z 基准 = 底面高度；选「中心」时为体心高度")
        self._layout.addWidget(gP)

        # ---- 创建 / 结束 ----
        btnConfirm = self._button("创建", primary=True)
        btnConfirm.clicked.connect(self.confirmed.emit)
        self._layout.addWidget(btnConfirm)
        btnEnd = self._button("结束")
        btnEnd.clicked.connect(self.finished.emit)
        self._layout.addWidget(btnEnd)
        self._layout.addStretch(1)

        # ---- 信号 ----
        self._shapeGroup.idClicked.connect(lambda *_: self._onShapeChanged())
        self._axisGroup.idClicked.connect(lambda *_: self.paramsChanged.emit())
        self._edgeGroup.idClicked.connect(lambda *_: self.paramsChanged.emit())
        self._scopeGroup.idClicked.connect(lambda *_: self.paramsChanged.emit())
        self._alignGroup.idClicked.connect(lambda *_: self.paramsChanged.emit())
        for s in (self._spinLen, self._spinWid, self._spinH, self._spinDia,
                  self._spinD1, self._spinD2, self._spinMaj, self._spinMin,
                  self._spinDraft, self._spinEdge, self._spinZ,
                  self._spinRot):
            s.valueChanged.connect(lambda *_: self.paramsChanged.emit())
        self._onShapeChanged()

    # ---------- 形状切换显隐 ----------

    def _onShapeChanged(self) -> None:
        k = self.shape()
        is_torus = (k == "torus")
        self._rowLen.setVisible(k == "box")
        self._rowWid.setVisible(k == "box")
        self._rowDia.setVisible(k == "cylinder")
        self._rowD1.setVisible(k == "cone")
        self._rowD2.setVisible(k == "cone")
        self._rowMaj.setVisible(is_torus)
        self._rowMin.setVisible(is_torus)
        self._rowAxis.setVisible(is_torus)
        self._rowH.setVisible(k in ("box", "cylinder", "cone"))
        self._rowDraft.setVisible(k in ("box", "cylinder"))
        self._groupEdge.setVisible(not is_torus)
        self._notifyResize()
        self.paramsChanged.emit()

    # ---------- 取值 ----------

    def shape(self) -> str:
        idx = self._shapeGroup.checkedId()
        return self.SHAPE_KEYS[idx] if 0 <= idx < len(self.SHAPE_KEYS) \
            else "box"

    def params(self) -> dict:
        """构建参数字典（含钳制：拔模受尺寸限制、圆环管径 ≤ 环径 80%、
        倒角/圆角值 ≤ 最小 feature 的 1/3）。"""
        k = self.shape()
        p = {"kind": k,
             "cz": self._spinZ.value(),
             "rot": self._spinRot.value(),
             "align": "center" if self._alignGroup.checkedId() == 1
                      else "bottom",
             "edge_mode": ("none", "chamfer", "fillet")[
                 max(0, self._edgeGroup.checkedId())],
             "edge_val": self._spinEdge.value(),
             "edge_scope": self._scopeGroup.checkedId() + 1}
        import math
        if k == "box":
            l, w, h = self._spinLen.value(), self._spinWid.value(), \
                self._spinH.value()
            draft = self._spinDraft.value()
            lim = math.degrees(math.atan(min(l, w) / max(h, 1e-6)))
            p.update(l=l, w=w, h=h, draft=max(-lim, min(lim, draft)))
        elif k == "cylinder":
            d, h = self._spinDia.value(), self._spinH.value()
            draft = self._spinDraft.value()
            lim = math.degrees(math.atan((d * 0.5) / max(h, 1e-6)))
            p.update(d=d, h=h, draft=max(-lim, min(lim, draft)))
        elif k == "cone":
            p.update(d1=self._spinD1.value(), d2=self._spinD2.value(),
                     h=self._spinH.value())
        else:
            dmaj = self._spinMaj.value()
            dmin = min(self._spinMin.value(), dmaj * 0.8)
            p.update(dmaj=dmaj, dmin=dmin,
                     axis=("z", "x", "y")[max(0, self._axisGroup.checkedId())])
        if k != "torus":
            # 倒角/圆角上限：最小 feature（高/半径）的 1/3
            feats = [p.get("h", 0.0)]
            if "l" in p:
                feats += [p["l"], p["w"]]
            if "d" in p:
                feats.append(p["d"] * 0.5)
            if "d1" in p:
                feats.append(p["d1"] * 0.5)
            if p.get("d2", 0.0) > 0:
                feats.append(p["d2"] * 0.5)
            p["edge_val"] = min(p["edge_val"],
                                max(0.01, min(f for f in feats if f > 0) / 3))
        return p


# ==================== RevolveSlope 旋转面 ====================

class RevolveSlopeParamPanel(ParamPanelBase):
    """旋转面面板：高度缩放 + 叠加方式。

    交互：点击轮廓线 → 点击旋转轴两端点 → 预览（调参实时）→ 右键/回车确认。
    """

    finished = Signal()
    paramsChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._spinScale = self._spin(1.0, decimals=2, step=0.1)
        self._spinScale.setRange(0.0, 9999.0)
        self._layout.addWidget(self._paramRow("高度缩放", self._spinScale))

        self._modeGroup = QButtonGroup(self)
        self._layout.addWidget(self._radioRow("方式", ("取代", "叠加", "切除"),
                                               self._modeGroup, 0))
        self._modeGroup.button(0).setChecked(True)
        self._modeGroup.idClicked.connect(lambda *_: self.paramsChanged.emit())

        btn = self._button("完成", primary=True)
        btn.clicked.connect(self.finished.emit)
        self._layout.addWidget(btn)
        self._layout.addStretch(1)

    def _paramRow(self, label: str, spin: QWidget) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setSpacing(6)
        lay.setContentsMargins(0, 0, 0, 0)
        lab = QLabel(label)
        lab.setMinimumWidth(_LABEL_W)
        lay.addWidget(lab)
        lay.addWidget(spin)
        lay.addStretch(1)
        if hasattr(spin, "valueChanged"):
            spin.valueChanged.connect(lambda *_: self.paramsChanged.emit())
        return w

    def _radioRow(self, label: str, texts, group: QButtonGroup,
                  start_id: int = 0) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setSpacing(6)
        lay.setContentsMargins(0, 0, 0, 0)
        if label:
            lab = QLabel(label)
            lab.setMinimumWidth(_LABEL_W)
            lay.addWidget(lab)
        for i, t in enumerate(texts):
            rb = QRadioButton(t)
            group.addButton(rb, start_id + i)
            lay.addWidget(rb)
        lay.addStretch(1)
        return row

    def heightScale(self) -> float:
        return float(self._spinScale.value())

    def mode(self) -> str:
        i = self._modeGroup.checkedId()
        return ("replace", "add", "cut")[i if 0 <= i < 3 else 0]

    def setStatus(self, text: str) -> None:
        pass


# ==================== ModelScale ====================

class ModelScaleParamPanel(ParamPanelBase):
    """模型扩缩参数面板：上下左右四个方向扩展/收缩模型边界。

    每个方向 = 开关按钮 + 数值输入框：按钮按下去（高亮）该方向才参与扩缩，
    关闭的方向按 0 处理（数值框同步禁用）。"""

    finished = Signal()
    paramsChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._spins: list[QDoubleSpinBox] = []
        self._toggles: list[QPushButton] = []
        for label in ("上", "下", "左", "右"):
            sp = self._spin(5.0, decimals=2, step=1.0)
            sp.setRange(-99999.0, 99999.0)
            tg = QPushButton(label)
            tg.setCheckable(True)
            tg.setChecked(True)
            tg.setMinimumWidth(30)
            tg.setStyleSheet(self._toggleStyle())
            row = QHBoxLayout()
            row.setSpacing(4)
            row.addWidget(tg)
            row.addWidget(sp, 1)
            self._layout.addLayout(row)
            self._toggles.append(tg)
            self._spins.append(sp)
            tg.toggled.connect(lambda on, s=sp: s.setEnabled(on))
            tg.toggled.connect(lambda *_: self.paramsChanged.emit())
            sp.valueChanged.connect(lambda *_: self.paramsChanged.emit())

        btn = self._button("应用", primary=True)
        btn.clicked.connect(self.finished.emit)
        self._layout.addWidget(btn)
        self._layout.addStretch(1)

    def _toggleStyle(self) -> str:
        """方向开关样式：未选中灰字描边，选中主题色高亮（与主按钮同色系）。"""
        return (
            "QPushButton { background:%s; color:%s; border:1px solid %s;"
            " border-radius:2px; padding:2px 4px; }"
            "QPushButton:hover { border-color:%s; color:%s; }"
            "QPushButton:checked { background:%s; color:%s; border-color:%s; }"
            "QPushButton:checked:hover { background:%s; }"
            % (theme.BG_PANEL, theme.TEXT, theme.BORDER,
               theme.PRIMARY, theme.PRIMARY_DARK,
               theme.PRIMARY, theme.TEXT_ON_PRIMARY, theme.PRIMARY,
               theme.PRIMARY_DARK))

    def scaleValues(self) -> tuple[float, float, float, float]:
        """返回 (上, 下, 左, 右) 的扩缩值（mm）；开关关闭的方向按 0 处理。"""
        return tuple(sp.value() if tg.isChecked() else 0.0
                     for tg, sp in zip(self._toggles, self._spins))

    def setStatus(self, text: str) -> None:
        pass


# ==================== CNC 轮廓铣 ====================

class CncProfileParamPanel(ParamPanelBase):
    """CNC 轮廓铣参数面板。

    交互：在 2D 画布选中闭合曲线 → 调参 → 点"生成 G 代码" → 弹窗显示 G 代码。
    首期仅支持轮廓铣（Profile）；口袋/钻孔占位待后续补。
    """
    generateRequested = Signal()
    cancelRequested = Signal()
    paramsChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        # ---- 刀具 ----
        self._spinDia = self._spin(3.0, decimals=2, step=0.5)
        self._spinDia.setRange(0.1, 100.0)
        self._addRow("刀径", self._spinDia)

        self._spinFlutes = self._ispin(2, 1, 12)
        self._addRow("刃数", self._spinFlutes)

        self._spinRpm = self._ispin(18000, 100, 60000)
        self._addRow("转速", self._spinRpm)

        self._spinFeed = self._spin(1200.0, decimals=0, step=100.0)
        self._spinFeed.setRange(1.0, 99999.0)
        self._addRow("进给", self._spinFeed)

        # ---- 切削 ----
        self._spinDepth = self._spin(-3.0, decimals=2, step=0.5)
        self._spinDepth.setRange(-999.0, 0.0)
        self._addRow("切深", self._spinDepth)

        self._spinStep = self._spin(1.0, decimals=2, step=0.1)
        self._spinStep.setRange(0.01, 999.0)
        self._addRow("层降", self._spinStep)

        # 偏移侧：外/内/在线（单选）
        self._grpSide = QButtonGroup(self)
        self._rbOutside = QRadioButton("外")
        self._rbInside = QRadioButton("内")
        self._rbOnLine = QRadioButton("在线")
        self._grpSide.addButton(self._rbOutside, 0)
        self._grpSide.addButton(self._rbInside, 1)
        self._grpSide.addButton(self._rbOnLine, 2)
        self._rbOutside.setChecked(True)
        self._addRowLayout("偏移", [self._rbOutside, self._rbInside, self._rbOnLine])

        # 铣削方向：顺铣/逆铣
        self._grpDir = QButtonGroup(self)
        self._rbClimb = QRadioButton("顺铣")
        self._rbConv = QRadioButton("逆铣")
        self._grpDir.addButton(self._rbClimb, 0)
        self._grpDir.addButton(self._rbConv, 1)
        self._rbClimb.setChecked(True)
        self._addRowLayout("方向", [self._rbClimb, self._rbConv])

        # ---- 引入/斜坡 ----
        self._spinLead = self._spin(2.0, decimals=2, step=0.5)
        self._spinLead.setRange(0.0, 999.0)
        self._addRow("引入", self._spinLead)

        self._spinRamp = self._spin(3.0, decimals=1, step=0.5)
        self._spinRamp.setRange(0.1, 90.0)
        self._addRow("斜角", self._spinRamp)

        # ---- 机床 ----
        self._cmbController = QComboBox()
        self._cmbController.addItem("GRBL", "grbl")
        self._cmbController.addItem("UCCNC", "uccnc")
        self._addRow("后置", self._cmbController)

        # ---- 按钮 ----
        self._btnGen = self._button("生成 G 代码", primary=True)
        self._btnGen.clicked.connect(self.generateRequested.emit)
        self._layout.addWidget(self._btnGen)

        self._btnCancel = self._button2("取消")
        self._btnCancel.clicked.connect(self.cancelRequested.emit)
        self._layout.addWidget(self._btnCancel)
        self._layout.addStretch(1)

        # 参数变化通知
        for sp in (self._spinDia, self._spinFlutes, self._spinRpm,
                   self._spinFeed, self._spinDepth, self._spinStep,
                   self._spinLead, self._spinRamp):
            sp.valueChanged.connect(lambda *_: self.paramsChanged.emit())
        self._grpSide.idClicked.connect(lambda *_: self.paramsChanged.emit())
        self._grpDir.idClicked.connect(lambda *_: self.paramsChanged.emit())
        self._cmbController.currentIndexChanged.connect(
            lambda *_: self.paramsChanged.emit())

    # --------- 参数读取 ---------
    def params(self):
        from core.cnc.service import ProfileParams
        side = "outside" if self._rbOutside.isChecked() else (
            "inside" if self._rbInside.isChecked() else "on_line")
        direction = "climb" if self._rbClimb.isChecked() else "conventional"
        return ProfileParams(
            tool_diameter=self._spinDia.value(),
            flute_count=self._spinFlutes.value(),
            spindle_rpm=self._spinRpm.value(),
            feed_xy=self._spinFeed.value(),
            cut_depth=self._spinDepth.value(),
            stepdown=self._spinStep.value(),
            offset_side=side,
            direction=direction,
            lead_length=self._spinLead.value(),
            ramp_angle_deg=self._spinRamp.value(),
            controller=self._cmbController.currentData(),
        )

    def setHint(self, text: str) -> None:
        pass


# ==================== CNC G 代码输出 ====================

class CncGcodeDialog(ParamPanelBase):
    """路径输出面板：内置后处理器单选 + 导入的后置文件 + G 代码预览 + 导出 NC。

    - 内置 Mach3/GRBL/UCCNC 用单选框（默认 Mach3）；导入的后置文件独立分组
    - 导入分组含下拉（列出用户自定义）+ 只读路径框（显示当前后置文件路径）
    - 切换后处理器 → 用同一刀路重新生成预览，并同步 state.machine.controller
    - "导入后置文件…" → 支持 .py 与厂家 .epst（自动转换），导入后自动选中
    - "导出 NC" → ASCII + CRLF（Mach3 兼容）
    """
    closed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import (
            QPlainTextEdit, QFrame, QRadioButton, QGroupBox,
            QLineEdit, QButtonGroup)
        self._toolpaths: list = []
        self._macros: dict = {}
        self._builtin_paths: dict = {}

        # ---- 左右分布：左 G 代码 / 右 控制面板 ----
        body = QHBoxLayout()
        body.setSpacing(8)

        # 左：G 代码预览
        self._edit = QPlainTextEdit()
        self._edit.setReadOnly(True)
        self._edit.setMinimumWidth(360)
        self._edit.setMinimumHeight(300)
        self._edit.setStyleSheet(
            "QPlainTextEdit { background:#1e1e1e; color:#d4d4d4;"
            " font-family: Consolas, 'Courier New', monospace;"
            " font-size: 11px; border:1px solid %s; }" % theme.BORDER)
        body.addWidget(self._edit, 1)

        # 右：后处理器（内置单选） / 导入的后置文件 / 操作按钮
        side = QVBoxLayout()
        side.setSpacing(8)
        side.setContentsMargins(0, 0, 0, 0)

        # ── 组 1：内置后处理器（单选框）──
        gRadio = QGroupBox("内置后处理器")
        gRadio.setStyleSheet("QGroupBox { font-weight:bold; }")
        lvRadio = QVBoxLayout(gRadio)
        lvRadio.setSpacing(4)
        lvRadio.setContentsMargins(8, 12, 8, 8)
        self._radioGroup = QButtonGroup(self)
        self._radioButtons: dict[str, QRadioButton] = {}
        from core.cnc.posts import list_posts as _list_posts
        builtin = {p.controller_id: p
                   for p in _list_posts() if p.source == "builtin"}
        for cid in ("mach3", "grbl", "uccnc"):
            pi = builtin.get(cid)
            label = pi.name if pi else cid
            rb = QRadioButton(label)
            rb.setStyleSheet("font-weight:normal;")
            self._radioGroup.addButton(rb)
            self._radioButtons[cid] = rb
            rb.toggled.connect(
                lambda checked, c=cid: self._onRadio(cid, checked))
            lvRadio.addWidget(rb)
        # mach3 默认选中移到 __init__ 末尾（此时 _userCombo 等控件已就绪）

        # ── 组 2：导入的后置文件 ──
        gUser = QGroupBox("导入的后置文件")
        gUser.setStyleSheet("QGroupBox { font-weight:bold; }")
        lvUser = QVBoxLayout(gUser)
        lvUser.setSpacing(4)
        lvUser.setContentsMargins(8, 12, 8, 8)
        self._userCombo = QComboBox()
        self._userCombo.setMinimumWidth(200)
        self._pathEdit = QLineEdit()
        self._pathEdit.setReadOnly(True)
        self._pathEdit.setPlaceholderText("后置文件路径")
        self._pathEdit.setToolTip("当前后置文件所在路径")
        btnImport = self._button2("导入后置文件…")
        btnImport.clicked.connect(self._onImport)
        lvUser.addWidget(self._userCombo)
        lvUser.addWidget(self._pathEdit)
        lvUser.addWidget(btnImport)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color:%s;" % theme.BORDER)

        btnCopy = self._button2("复制")
        btnSave = self._button("导出 NC", primary=True)
        btnClose = self._button2("关闭")
        btnCopy.clicked.connect(self._onCopy)
        btnSave.clicked.connect(self._onSave)
        btnClose.clicked.connect(self.closed.emit)

        side.addWidget(gRadio)
        side.addWidget(gUser)
        side.addSpacing(4)
        side.addWidget(sep)
        side.addSpacing(4)
        side.addWidget(btnCopy)
        side.addWidget(btnSave)
        side.addWidget(btnClose)
        side.addStretch(1)

        body.addLayout(side)
        self._layout.addLayout(body)

        self._userCombo.currentIndexChanged.connect(self._onUserChanged)
        self._reloadPosts()
        self._radioButtons["mach3"].setChecked(True)

    # ---------- 上下文 ----------
    def setContext(self, toolpaths, macros=None, controller=None) -> None:
        """带刀路上下文的打开方式：可切换后处理器实时重新生成。"""
        self._toolpaths = list(toolpaths) if toolpaths else []
        self._macros = dict(macros) if macros else {}
        self._reloadPosts()
        if controller:
            if controller in self._radioButtons:
                self._radioButtons[controller].setChecked(True)
            else:
                i = self._userCombo.findData(controller)
                if i > 0:
                    self._userCombo.setCurrentIndex(i)
        self._refresh()

    def setGcode(self, text: str) -> None:
        """兼容入口：无刀路上下文，直接显示已有文本。"""
        self._toolpaths = []
        self._edit.setPlainText(text)

    # ---------- 后处理器 ----------
    def _reloadPosts(self) -> None:
        """刷新内置路径映射 + 导入下拉（保留当前选择）。"""
        from core.cnc.posts import list_posts
        infos = list_posts()
        self._builtin_paths = {p.controller_id: p.path
                               for p in infos if p.source == "builtin"}
        user = [p for p in infos if p.source == "user"]
        cur = self._userCombo.currentData()
        self._userCombo.blockSignals(True)
        self._userCombo.clear()
        self._userCombo.addItem("（使用内置后处理器）", "")
        for p in user:
            self._userCombo.addItem(p.name, p.controller_id)
        self._userCombo.setEnabled(bool(user))
        if cur:
            i = self._userCombo.findData(cur)
            if i > 0:
                self._userCombo.setCurrentIndex(i)
            else:
                self._userCombo.setCurrentIndex(0)
        else:
            self._userCombo.setCurrentIndex(0)
        self._userCombo.blockSignals(False)
        self._updatePath()

    def _currentController(self):
        """当前生效的后处理器 id：内置单选优先，否则导入下拉。"""
        for cid, rb in self._radioButtons.items():
            if rb.isChecked():
                return cid
        cid = self._userCombo.currentData()
        return cid or "mach3"

    def _onRadio(self, controller: str, checked: bool) -> None:
        """选中内置单选框：取消导入下拉选择，切回内置。"""
        if not checked:
            return
        if not hasattr(self, "_userCombo"):   # 构造期信号先于控件创建
            return
        self._userCombo.blockSignals(True)
        self._userCombo.setCurrentIndex(0)
        self._userCombo.blockSignals(False)
        self._updatePath()
        self._refresh()

    def _onUserChanged(self) -> None:
        """选中导入的后置文件：取消内置单选，改用导入项。"""
        cid = self._userCombo.currentData()
        if not cid:
            self._radioButtons["mach3"].setChecked(True)
            return
        # exclusive 组内 setChecked(False) 不生效，临时解除互斥再逐个取消
        self._radioGroup.setExclusive(False)
        for rb in self._radioButtons.values():
            rb.setChecked(False)
        self._radioGroup.setExclusive(True)
        self._updatePath()
        self._refresh()

    def _updatePath(self) -> None:
        """路径框显示当前生效后置文件的路径。"""
        cid = self._currentController()
        path = self._builtin_paths.get(cid, "")
        if not path:
            from core.cnc.posts import list_posts
            for p in list_posts():
                if p.controller_id == cid:
                    path = p.path
                    break
        self._pathEdit.setText(path or "")

    def _refresh(self) -> None:
        if not self._toolpaths:
            return
        try:
            from core.cnc.service import generate_gcode
            text = generate_gcode(self._toolpaths,
                                  self._currentController(), self._macros)
            self._edit.setPlainText(text)
        except Exception as e:
            self._edit.setPlainText(f"生成失败: {e}")
        # 同步机床控制器：下次从别处重新生成也走新后处理
        try:
            from core.cnc.state import CncState
            st = CncState.instance()
            cid = self._currentController()
            if cid and st.machine.controller != cid:
                st.machine.controller = cid
                st.notifyChanged()
        except Exception:
            pass

    def _onImport(self) -> None:
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        from core.cnc.posts import _user_posts_dir, scan_posts
        import os
        import shutil
        path, _ = QFileDialog.getOpenFileName(
            self, "导入后置文件", "",
            "后置文件 (*.py);;ArtCAM 后处理定义 (*.epst);;所有文件 (*)")
        if not path:
            return
        dest_dir = _user_posts_dir()
        lower = path.lower()
        want = None
        stem = None
        try:
            if lower.endswith(".epst"):
                # 厂家 ArtCAM 后处理定义 → 自动转换为 Cangai 后置文件
                from core.cnc.posts.epst_import import import_epst
                _, want = import_epst(path, dest_dir)
            elif lower.endswith(".py"):
                dest = str(dest_dir / os.path.basename(path))
                shutil.copy2(path, dest)
                stem = os.path.splitext(os.path.basename(path))[0]
            else:
                QMessageBox.warning(
                    self, "无法识别",
                    "请选择 .py（Cangai 后置文件）或 .epst（ArtCAM 后处理定义）")
                return
        except Exception as e:
            QMessageBox.warning(self, "导入失败", str(e))
            return
        scan_posts()
        self._reloadPosts()
        # 选中刚导入的后置文件（epst 精确 id；.py 按文件名匹配）
        if want is not None:
            i = self._userCombo.findData(want)
            if i > 0:
                self._userCombo.setCurrentIndex(i)
        elif stem:
            from core.cnc.posts import list_posts
            for pi in list_posts():
                if pi.source == "user" and (
                        pi.controller_id == f"user_{stem}".lower()
                        or stem in pi.name):
                    i = self._userCombo.findData(pi.controller_id)
                    if i > 0:
                        self._userCombo.setCurrentIndex(i)
                    break
        QMessageBox.information(
            self, "导入成功",
            f"已导入：{path}\n可在\"导入的后置文件\"下拉中选择使用。")

    # ---------- 按钮 ----------
    def _onCopy(self) -> None:
        from PySide6.QtWidgets import QApplication
        cb = QApplication.clipboard()
        cb.setText(self._edit.toPlainText())

    def _onSave(self) -> None:
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        from core.cnc.service import export_nc
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 NC 文件", "cangai_output.nc",
            "NC 文件 (*.nc);;G 代码 (*.gcode *.ngc);;所有文件 (*)")
        if not path:
            return
        try:
            export_nc(self._edit.toPlainText(), path,
                      encoding="ascii", crlf=True)
        except OSError as e:
            QMessageBox.warning(self, "导出失败", str(e))
            return
        QMessageBox.information(
            self, "导出成功",
            f"已导出：{path}\n（ASCII 编码 + CRLF 行尾，Mach3 兼容）")


# ==================== Thread 螺纹 ====================

class ThreadParamPanel(ParamPanelBase):
    """螺纹参数面板：公称直径/螺距/长度/内外螺纹/旋向。

    交互：选中圆柱面 → 面板调参实时预览 → 确认/右键生成螺纹；
    空白点击 → 生成独立外螺纹螺柱。参数改动发 paramsChanged 刷新预览。
    """

    confirmed = Signal()        # 点击"确认"/右键：生成当前预览螺纹
    finished = Signal()         # 点击"结束"/Esc：结束命令
    paramsChanged = Signal()    # 任一参数变更（实时预览）

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        from core.vector.thread_generator import (
            STANDARD_SIZES, METRIC_COARSE_PITCH, METRIC_FINE_PITCH,
            pitch_options)

        gEx, lEx = self._group("螺纹参数")

        # 公称直径下拉（标准尺寸）
        self._comboD = QComboBox()
        for d in STANDARD_SIZES:
            self._comboD.addItem(f"M{d:g}", d)
        self._comboD.setCurrentIndex(STANDARD_SIZES.index(6.0))
        lEx.addWidget(self._row("公称直径", self._comboD))

        # 螺距下拉（根据直径联动）
        self._comboP = QComboBox()
        lEx.addWidget(self._row("螺距", self._comboP))
        self._refreshPitches()

        # 螺纹长度
        self._spinL = self._spin(12.0, decimals=2, step=1.0)
        self._spinL.setRange(0.5, 99999.0)
        lEx.addWidget(self._row("长度", self._spinL))

        # 配合间隙（3D 打印：内外螺纹更松、可拧入）
        self._spinC = self._spin(0.0, decimals=2, step=0.05)
        self._spinC.setRange(0.0, 1.0)
        self._spinC.setToolTip(
            "3D 打印配合间隙（直径方向）：外螺纹先车小 δ 再套牙（整体"
            "变瘦）；内螺纹用加大 δ 的丝锥攻丝（牙槽更深、牙更矮）。"
            "0=标准配合；打印件建议 0.2~0.4。")
        lEx.addWidget(self._row("配合间隙", self._spinC))

        # 内/外螺纹单选
        self._rbExt = QRadioButton("外螺纹")
        self._rbInt = QRadioButton("内螺纹")
        self._rbExt.setChecked(True)
        bgType = QButtonGroup(self)
        bgType.addButton(self._rbExt, 0)
        bgType.addButton(self._rbInt, 1)
        lEx.addWidget(self._row2("类型", [self._rbExt, self._rbInt]))

        # 左/右旋单选
        self._rbRH = QRadioButton("右旋")
        self._rbLH = QRadioButton("左旋")
        self._rbRH.setChecked(True)
        bgHand = QButtonGroup(self)
        bgHand.addButton(self._rbRH, 0)
        bgHand.addButton(self._rbLH, 1)
        lEx.addWidget(self._row2("旋向", [self._rbRH, self._rbLH]))

        self._layout.addWidget(gEx)

        # ---- 确认 / 结束 ----
        btnConfirm = self._button("确认", primary=True)
        btnConfirm.clicked.connect(self.confirmed.emit)
        self._layout.addWidget(btnConfirm)
        btnEnd = self._button("结束")
        btnEnd.clicked.connect(self.finished.emit)
        self._layout.addWidget(btnEnd)
        self._layout.addStretch(1)

        # ---- 信号联动 ----
        self._comboD.currentIndexChanged.connect(self._onSizeChanged)
        self._comboP.currentIndexChanged.connect(
            lambda *_: self.paramsChanged.emit())
        self._spinL.valueChanged.connect(lambda *_: self.paramsChanged.emit())
        self._spinC.valueChanged.connect(lambda *_: self.paramsChanged.emit())
        self._rbExt.toggled.connect(lambda *_: self.paramsChanged.emit())
        self._rbRH.toggled.connect(lambda *_: self.paramsChanged.emit())

    # ---------- 布局小工具 ----------

    def _group(self, title: str):
        g = QGroupBox(title)
        lay = QVBoxLayout(g)
        lay.setSpacing(3)
        lay.setContentsMargins(6, 4, 6, 4)
        return g, lay

    def _row(self, label: str, widget) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lab = QLabel(label)
        lab.setMinimumWidth(_LABEL_W)
        lay.addWidget(lab)
        lay.addWidget(widget, 1)
        return row

    def _row2(self, label: str, widgets) -> QWidget:
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lab = QLabel(label)
        lab.setMinimumWidth(_LABEL_W)
        lay.addWidget(lab)
        for w in widgets:
            lay.addWidget(w)
        lay.addStretch(1)
        return row

    # ---------- 参数联动 ----------

    def _onSizeChanged(self, *_):
        self._refreshPitches()
        self.paramsChanged.emit()

    def _refreshPitches(self):
        """根据当前公称直径刷新螺距下拉（粗牙 + 细牙）。"""
        from core.vector.thread_generator import pitch_options
        d = self._comboD.currentData()
        self._comboP.blockSignals(True)
        self._comboP.clear()
        for p in pitch_options(float(d)):
            self._comboP.addItem(f"{p:g} mm", p)
        self._layout.activate()
        self._comboP.blockSignals(False)

    # ---------- 取值 ----------

    def majorDiameter(self) -> float:
        return float(self._comboD.currentData())

    def pitch(self) -> float:
        v = self._comboP.currentData()
        return float(v) if v is not None else 1.0

    def length(self) -> float:
        return self._spinL.value()

    def clearance(self) -> float:
        """配合间隙 δ（直径方向，mm）。"""
        return self._spinC.value()

    def isInternal(self) -> bool:
        return self._rbInt.isChecked()

    def isLeftHand(self) -> bool:
        return self._rbLH.isChecked()

    def setInternal(self, internal: bool, locked: bool = False) -> None:
        """命令层按拾取面自动设定内/外螺纹；locked=锁定不可改（面模式）。"""
        self._rbExt.blockSignals(True)
        self._rbInt.blockSignals(True)
        self._rbExt.setChecked(not internal)
        self._rbInt.setChecked(internal)
        self._rbExt.blockSignals(False)
        self._rbInt.blockSignals(False)
        self._rbExt.setEnabled(not locked)
        self._rbInt.setEnabled(not locked)
        self.paramsChanged.emit()




# ==================== TextureFill（几何纹理填充） ====================

class TextureFillParamPanel(ParamPanelBase):
    """纹理填充参数面板（复刻精雕 SurfMill"几何纹理"族 + 表面扰动）。

    几何纹理类型：随机分形 / 腐蚀 / 编织带 / 缠绕结 / 填充图案 /
    矩形网格 / 表面扰动。"填充图案"启用子图案选择（六边/砖形/菱形/
    圆环/点阵）。拼合方式与区域塑形一致（叠加/取代/切除）。
    """

    finished = Signal()
    applyWhole = Signal()
    paramsChanged = Signal()
    confirmed = Signal()      # 实时预览 → 确认生成
    cancelled = Signal()      # 预览 → 取消（回滚）
    refreshPreview = Signal()  # 手动"更新预览"（立即重算一次）

    _KINDS = ("fractal", "weave", "wrap", "fill")
    _KIND_LABELS = ("随机", "编织", "缠绕", "填充")
    _BLEND_LABELS = ("叠加", "取代", "切除")
    _SCOPE_LABELS = COLOR_SCOPE_LABELS
    _WEAVE_SECS = ("ellipse", "trapezoid", "spline")

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        # ---- 几何纹理类型（一行单选：随机/编织/缠绕/填充）----
        self._kindGroup = QButtonGroup(self)
        gKind, lKind = self._section("纹理类型")
        lKind.addWidget(self._radioRow("", self._KIND_LABELS,
                                       self._kindGroup, 0))
        self._kindGroup.button(0).setChecked(True)
        self._kindGroup.idClicked.connect(self._onKindChanged)
        self._layout.addWidget(gKind)

        # ---- 随机子模式（仅"随机"类型显示：分形/波浪/糙面）----
        self._subGroup = QButtonGroup(self)
        self._rowRandSub = self._radioRow("", RAND_SUB_LABELS,
                                          self._subGroup, 0)
        # 无分组框的裸行：左缩进 = 上方分组框的框线+内边距，
        # 使单选钮与"几何纹理类型"组内的单选钮垂直对齐
        _sub_indent = gKind.contentsRect().left() + 6
        if _sub_indent <= 6:
            _sub_indent = 16
        self._rowRandSub.layout().setContentsMargins(_sub_indent, 0, 0, 0)
        self._subGroup.button(0).setChecked(True)
        self._subGroup.idClicked.connect(self._onRandSubChanged)
        self._layout.addWidget(self._rowRandSub)

        def _pairRow(label, w1, w2, sep="×"):
            """一行放两个输入框（高度/强度、条数、上宽下宽），压缩纵向空间。"""
            w = QWidget()
            lay = QHBoxLayout(w)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(4)
            lab = QLabel(label)
            lab.setMinimumWidth(_LABEL_W)
            lay.addWidget(lab)
            lay.addWidget(w1, 1)
            if sep:
                lay.addWidget(QLabel(sep))
            lay.addWidget(w2, 1)
            return w

        # ---- 填充子图案（仅"填充图案"类型显示）----
        self._comboPattern = QComboBox()
        for lab in FILL_PATTERN_LABELS:
            self._comboPattern.addItem(lab)
        self._rowPattern = self._row("填充图案:", self._comboPattern)
        self._comboPattern.currentIndexChanged.connect(
            self._onFillPatternChanged)
        self._layout.addWidget(self._rowPattern)

        # ---- 填充几何参数（毫米制；仅填充类型显示）----
        self._spinFillSize = self._spin(6.0, decimals=1, step=0.5)
        self._spinFillSize.setRange(0.5, 200.0)
        self._rowFillSize = self._row("图案尺寸(mm):", self._spinFillSize)
        self._layout.addWidget(self._rowFillSize)
        # 砖形长/宽独立（默认标准砖 240×115 → 24×11.5，灰缝间距 1）
        self._spinFillBrickL = self._spin(24.0, decimals=1, step=0.5)
        self._spinFillBrickL.setRange(0.5, 500.0)
        self._spinFillBrickW = self._spin(11.5, decimals=1, step=0.5)
        self._spinFillBrickW.setRange(0.5, 500.0)
        self._rowFillBrick = _pairRow("砖 长/宽(mm):",
                                      self._spinFillBrickL,
                                      self._spinFillBrickW)
        self._layout.addWidget(self._rowFillBrick)
        self._spinFillGapX = self._spin(1.0, decimals=1, step=0.5)
        self._spinFillGapX.setRange(-100.0, 200.0)
        self._spinFillGapX.setToolTip("允许负值：负间距图案互相嵌入\n"
                                      "六边形 0 / -尺寸×0.135 ≈ 蜂窝\n"
                                      "砖形灰缝标准间距 = 1（10cm 砖比例）")
        self._spinFillGapY = self._spin(1.0, decimals=1, step=0.5)
        self._spinFillGapY.setRange(-100.0, 200.0)
        self._spinFillGapY.setToolTip("允许负值：负间距图案互相嵌入\n"
                                      "六边形 0 / -尺寸×0.135 ≈ 蜂窝\n"
                                      "砖形灰缝标准间距 = 1（10cm 砖比例）")
        self._rowFillGap = _pairRow("横/纵间距(mm):",
                                    self._spinFillGapX, self._spinFillGapY)
        self._layout.addWidget(self._rowFillGap)

        # ---- 纹理参数 ----
        # 高度/强度：所有类型共用（合成一行）
        self._spinH = self._spin(1.0, decimals=2, step=0.1)
        self._spinH.setRange(0.01, 50.0)
        self._spinStrength = self._spin(1.0, decimals=2, step=0.1)
        self._spinStrength.setRange(0.0, 3.0)
        self._layout.addWidget(
            _pairRow("高度(mm)/强度:", self._spinH, self._spinStrength, "/"))

        # ---- 波浪参数（仅随机·波浪子模式显示）----
        self._spinWaveN = self._ispin(8, 1, 200)
        self._spinWaveAng = self._spin(0.0, decimals=1, step=5.0)
        self._spinWaveAng.setRange(0.0, 360.0)
        self._rowWaveNA = _pairRow("数量/角度:", self._spinWaveN,
                                   self._spinWaveAng, "/")
        self._spinWaveCurv = self._spin(0.3, decimals=2, step=0.05)
        self._spinWaveCurv.setRange(0.0, 1.0)
        self._rowWaveCurv = self._row("弧度(0-1):", self._spinWaveCurv)
        self._layout.addWidget(self._rowWaveNA)
        self._layout.addWidget(self._rowWaveCurv)

        # 细节尺度：除编织/缠绕/缠绕结外共用
        self._spinDetail = self._spin(5.0, decimals=1, step=0.5)
        self._spinDetail.setRange(0.5, 100.0)
        self._rowDetail = self._row("细节尺度(mm):", self._spinDetail)
        self._layout.addWidget(self._rowDetail)

        # 随机种子：仅随机分形/腐蚀/矩形网格/表面扰动
        self._spinSeed = self._ispin(0, 0, 99999)
        self._rowSeed = self._row("随机种子:", self._spinSeed)
        self._layout.addWidget(self._rowSeed)

        # ---- 编织/缠绕公共参数（整面编织显示条数；缠绕显示提示并点曲线）----
        self._weaveBox, _wl = self._section("编织")
        _wl.setSpacing(4)
        self._labWeaveHint = QLabel("缠绕：单击曲线开始预览\n（单条自相交"
                                    "如“8”字，或多条编组均可）")
        self._labWeaveHint.setWordWrap(True)
        _wl.addWidget(self._labWeaveHint)

        # 条数（横×竖，仅整面编织显示）
        self._ispinWeaveRows = self._ispin(8, 1, 200)
        self._ispinWeaveCols = self._ispin(8, 1, 200)
        self._rowWeaveCounts = _pairRow("条数(横×竖):",
                                        self._ispinWeaveRows,
                                        self._ispinWeaveCols)
        _wl.addWidget(self._rowWeaveCounts)

        # 交叉角度（两组带的夹角，90=经纬垂直；仅整面编织显示）
        self._spinWeaveAngle = self._spin(90.0, decimals=0, step=5.0)
        self._spinWeaveAngle.setRange(10.0, 170.0)
        self._rowWeaveAngle = self._row("交叉角度(°):",
                                        self._spinWeaveAngle)
        _wl.addWidget(self._rowWeaveAngle)

        # 子编织（每带细分的细条数 + 细条间缝宽，竹编效果）
        self._ispinWeaveSub = self._ispin(1, 1, 8)
        self._spinWeaveSubGap = self._spin(0.6, decimals=1, step=0.2)
        self._spinWeaveSubGap.setRange(0.0, 20.0)
        self._rowWeaveSub = _pairRow("子编织(条/缝):",
                                     self._ispinWeaveSub,
                                     self._spinWeaveSubGap, sep="/")
        _wl.addWidget(self._rowWeaveSub)

        # 截面类型
        self._comboWeaveSec = QComboBox()
        for _lab in ("椭圆", "梯形", "样条"):
            self._comboWeaveSec.addItem(_lab)
        self._rowWeaveSec = self._row("截面类型:", self._comboWeaveSec)
        _wl.addWidget(self._rowWeaveSec)

        # 截面上宽/下宽（梯形用；下宽即带宽）
        self._spinWeaveTop = self._spin(1.5, decimals=2, step=0.5)
        self._spinWeaveTop.setRange(0.0, 100.0)
        self._spinWeaveBot = self._spin(4.0, decimals=2, step=0.5)
        self._spinWeaveBot.setRange(0.5, 200.0)
        self._rowWeaveW = _pairRow("上/下宽(mm):",
                                   self._spinWeaveTop,
                                   self._spinWeaveBot, sep="/")
        _wl.addWidget(self._rowWeaveW)

        # 椭圆高度比（仅椭圆截面显示）
        self._spinWeaveER = self._spin(1.0, decimals=2, step=0.1)
        self._spinWeaveER.setRange(0.1, 1.0)
        self._rowWeaveER = self._row("椭圆高度比:", self._spinWeaveER)
        _wl.addWidget(self._rowWeaveER)
        self._comboWeaveSec.currentIndexChanged.connect(
            lambda *_: self._onWeaveSecChanged())
        # 全局样式使 QSpinBox 最小提示宽达 ~113px，两列一行必然溢出；
        # 显式收窄（数值仍可完整显示，超出可键入）
        for _w in (self._spinH, self._spinStrength, self._ispinWeaveRows,
                   self._ispinWeaveCols, self._spinWeaveTop,
                   self._spinWeaveBot, self._spinWeaveER,
                   self._spinWeaveAngle, self._ispinWeaveSub,
                   self._spinWeaveSubGap,
                   self._spinWaveN, self._spinWaveAng, self._spinWaveCurv,
                   self._spinFillSize, self._spinFillGapX,
                   self._spinFillGapY,
                   self._spinFillBrickL, self._spinFillBrickW):
            _w.setMinimumWidth(62)
            _w.valueChanged.connect(lambda *_: self.paramsChanged.emit())
        self._layout.addWidget(self._weaveBox)

        # ---- 拼合方式 ----
        self._blendGroup = QButtonGroup(self)
        gBlend, lBlend = self._section("拼合方式")
        lBlend.addWidget(self._radioRow("", self._BLEND_LABELS,
                                        self._blendGroup, 0))
        self._blendGroup.button(0).setChecked(True)
        self._layout.addWidget(gBlend)

        # ---- 颜色范围 ----
        self._scopeGroup = QButtonGroup(self)
        gScope, lScope = self._section("颜色范围")
        lScope.addWidget(self._radioRow("", self._SCOPE_LABELS,
                                        self._scopeGroup, 0))
        self._scopeGroup.button(0).setChecked(True)
        self._layout.addWidget(gScope)

        # ---- 操作（面板最底部）----
        # 4 种类型均实时预览：调参即预览（不进撤销栈）；"更新预览"手动
        # 立即重算；"确认生成"一次性提交，"取消"还原，"完成"结束命令。
        # 拼合方式、颜色范围始终在按钮上方。
        row = QHBoxLayout()
        row.setSpacing(6)
        # 4 个按钮的 4 字文案原使整行最小宽恰好 250px（等于可视宽）→
        # 一旦出现纵向滚动条就触发横向滚动条；文案缩为 2 字，语义放 tooltip
        self._btnWeaveConfirm = self._button("确认", primary=True)
        self._btnWeaveConfirm.setToolTip("确认生成（提交并退出预览）")
        self._btnWeaveConfirm.clicked.connect(self.confirmed.emit)
        row.addWidget(self._btnWeaveConfirm)
        self._btnRefresh = self._button2("更新")
        self._btnRefresh.setToolTip("立即重算一次预览")
        self._btnRefresh.clicked.connect(self.refreshPreview.emit)
        row.addWidget(self._btnRefresh)
        self._btnWeaveCancel = self._button2("取消")
        self._btnWeaveCancel.setToolTip("取消并回滚到预览前")
        self._btnWeaveCancel.clicked.connect(self.cancelled.emit)
        row.addWidget(self._btnWeaveCancel)
        btnDone = self._button2("完成")
        btnDone.clicked.connect(self.finished.emit)
        row.addWidget(btnDone)
        row.addStretch(1)
        self._layout.addLayout(row)

        # 初始按默认类型（随机）调度显隐
        self._onKindChanged()

    def _section(self, title: str):
        """紧凑分组：小号加粗标题 + 行容器（替代 QGroupBox 的框线与内边距，
        整块可省 ~24px 纵向空间，避免参数区出现滚动条）。"""
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setSpacing(2)
        lay.setContentsMargins(0, 0, 0, 2)
        lab = QLabel(title)
        lab.setStyleSheet("color:%s; font-weight:bold;" % theme.PRIMARY_DARK)
        lay.addWidget(lab)
        box._titleLabel = lab
        return box, lay

    def kind(self) -> str:
        return self._KINDS[max(0, self._kindGroup.checkedId())]

    def _onRandSubChanged(self, *_) -> None:
        """随机子模式切换：波浪参数仅"波浪"子模式显示。"""
        is_waves = (self.kind() == "fractal"
                    and RAND_SUBS[max(0, self._subGroup.checkedId())]
                    == "waves")
        self._rowWaveNA.setVisible(is_waves)
        self._rowWaveCurv.setVisible(is_waves)
        self.paramsChanged.emit()

    def _onKindChanged(self, *_) -> None:
        """按纹理类型显隐各自参数（随机/编织/缠绕/填充各有各的参数）。
        拼合方式、颜色范围所有类型共用；操作按钮在面板最底部按类型切换。"""
        k = self.kind()
        is_weave = k in ("weave", "wrap")
        self._rowRandSub.setVisible(k == "fractal")   # 随机子模式单选
        is_fill = k == "fill"
        self._rowPattern.setVisible(is_fill)
        self._rowFillSize.setVisible(is_fill)
        self._rowFillGap.setVisible(is_fill)
        self._rowDetail.setVisible(k == "fractal")
        self._rowSeed.setVisible(k == "fractal")
        self._onFillPatternChanged()                  # 含径向间距显隐
        if is_weave:
            self._weaveBox._titleLabel.setText(
                "编织参数" if k == "weave" else "缠绕参数")
        self._weaveBox.setVisible(is_weave)
        self._rowWeaveCounts.setVisible(k == "weave")  # 条数仅整面编织
        self._rowWeaveAngle.setVisible(k == "weave")   # 交叉角度仅整面编织
        self._rowWeaveSub.setVisible(is_weave)         # 子编织两种模式通用
        self._labWeaveHint.setVisible(k == "wrap")     # 缠绕点曲线
        # 底部操作行（确认生成/更新预览/取消/完成）所有类型恒显
        self._onRandSubChanged()      # 含 paramsChanged

    def _onFillPatternChanged(self, *_) -> None:
        """填充图案切换：砖形显示长/宽行，其他图案显示尺寸行。"""
        pat = FILL_PATTERNS[max(0, self._comboPattern.currentIndex())]
        is_fill = self.kind() == "fill"
        is_brick = pat == "brick"
        self._rowFillSize.setVisible(is_fill and not is_brick)
        self._rowFillBrick.setVisible(is_fill and is_brick)
        self.paramsChanged.emit()

    def _onWeaveSecChanged(self, *_) -> None:
        # 椭圆高度比仅椭圆截面需要，其他截面直接隐藏
        self._rowWeaveER.setVisible(
            self._WEAVE_SECS[max(0, self._comboWeaveSec.currentIndex())]
            == "ellipse")
        self.paramsChanged.emit()

    def params(self) -> "TextureFillParams":
        from core.relief.textures import TextureFillParams
        return TextureFillParams(
            kind=self.kind(),
            rand_sub=RAND_SUBS[max(0, self._subGroup.checkedId())],
            waves_count=int(self._spinWaveN.value()),
            waves_curv=self._spinWaveCurv.value(),
            waves_angle=self._spinWaveAng.value(),
            fill_pattern=FILL_PATTERNS[self._comboPattern.currentIndex()],
            # 砖形：fill_size 语义 = 砖长（取砖长输入框）；其他图案取尺寸框
            fill_size=(self._spinFillBrickL.value()
                       if FILL_PATTERNS[self._comboPattern.currentIndex()]
                       == "brick" else self._spinFillSize.value()),
            fill_brick_w=self._spinFillBrickW.value(),
            fill_gap_x=self._spinFillGapX.value(),
            fill_gap_y=self._spinFillGapY.value(),
            height_mm=self._spinH.value(),
            strength=self._spinStrength.value(),
            detail_mm=self._spinDetail.value(),
            seed=int(self._spinSeed.value()),
            blend=("add", "replace", "subtract")[
                max(0, self._blendGroup.checkedId())],
            color_scope=COLOR_SCOPES[max(0, self._scopeGroup.checkedId())],
            weave_mode="curve" if self.kind() == "wrap" else "grid",
            weave_rows=int(self._ispinWeaveRows.value()),
            weave_cols=int(self._ispinWeaveCols.value()),
            weave_sec=self._WEAVE_SECS[
                max(0, self._comboWeaveSec.currentIndex())],
            weave_top_w=self._spinWeaveTop.value(),
            weave_bot_w=self._spinWeaveBot.value(),
            weave_ellipse_ratio=self._spinWeaveER.value(),
            weave_angle=self._spinWeaveAngle.value(),
            weave_sub_n=int(self._ispinWeaveSub.value()),
            weave_sub_gap=self._spinWeaveSubGap.value(),
        )


# ==================== SlopeZone（构造曲线：角度/高度） ====================

class SlopeZoneParamPanel(ParamPanelBase):
    """构造曲线参数面板（原"角度分区线"，扩展双模式）。

    角度曲线：按坡度分界把模型分成平坦/缓坡/陡坡/陡立等区，生成各区
    分界闭合曲线（每区一条曲线对象，按区着色）。
    高度曲线：按指定高度取等高线，生成该高度的水平截面闭合曲线。
    """

    finished = Signal()
    generate = Signal()
    paramsChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # ---- 模式单选：角度曲线 / 高度曲线 ----
        self._modeGroup = QButtonGroup(self)
        self._layout.addWidget(self._radioRow(
            "模式:", ("角度曲线", "高度曲线"), self._modeGroup, 0))
        self._modeGroup.button(0).setChecked(True)
        self._modeGroup.idClicked.connect(self._onModeChanged)

        # ---- 角度曲线参数（仅角度模式显示）----
        self._grpAngle, l = self._group("分界角度（度）")
        self._spinB1 = self._spin(10.0, decimals=0, step=1.0)
        self._spinB1.setRange(1.0, 89.0)
        l.addWidget(self._row("平坦/缓坡:", self._spinB1))
        self._spinB2 = self._spin(30.0, decimals=0, step=1.0)
        self._spinB2.setRange(1.0, 89.0)
        l.addWidget(self._row("缓坡/陡坡:", self._spinB2))
        self._spinB3 = self._spin(60.0, decimals=0, step=1.0)
        self._spinB3.setRange(1.0, 89.0)
        l.addWidget(self._row("陡坡/陡立:", self._spinB3))
        self._layout.addWidget(self._grpAngle)

        # ---- 高度曲线参数（仅高度模式显示）----
        self._spinHeight = self._spin(0.0, decimals=2, step=0.5)
        self._spinHeight.setRange(-1000.0, 1000.0)
        self._rowHeight = self._row("高度(mm):", self._spinHeight)
        self._layout.addWidget(self._rowHeight)

        self._spinTol = self._spin(0.05, decimals=2, step=0.01)
        self._spinTol.setRange(0.0, 1.0)
        self._addRow("曲线简化(mm):", self._spinTol)

        row = QHBoxLayout()
        row.setSpacing(8)
        btnGen = self._button("生成曲线", primary=True)
        btnGen.clicked.connect(self.generate.emit)
        btnDone = self._button2("完成")
        btnDone.clicked.connect(self.finished.emit)
        row.addWidget(btnGen)
        row.addWidget(btnDone)
        row.addStretch(1)
        self._layout.addLayout(row)

        self._onModeChanged()

    def _onModeChanged(self, *_) -> None:
        is_angle = self.mode() == "angle"
        self._grpAngle.setVisible(is_angle)
        self._rowHeight.setVisible(not is_angle)
        self.paramsChanged.emit()

    def mode(self) -> str:
        return ("angle", "height")[max(0, self._modeGroup.checkedId())]

    def bounds(self) -> tuple:
        return (self._spinB1.value(), self._spinB2.value(),
                self._spinB3.value())

    def height(self) -> float:
        return self._spinHeight.value()

    def tolerance(self) -> float:
        return self._spinTol.value()


# ==================== Revcloud（修订云线） ====================

class RevcloudPanel(ParamPanelBase):
    """修订云线面板：模式切换（矩形/多边形/手绘/选对象）+ 长宽 + 单弧尺寸。

    模式按钮选中态高亮（主题青色底白字）。"""
    modeChanged = Signal(str)          # 'rect' / 'poly' / 'free' / 'object'
    sizeChanged = Signal(float, float) # 长/宽（矩形模式编辑对角）
    arcLenChanged = Signal(float)      # 单弧尺寸（0 = 自适应）
    confirmRequested = Signal()
    cancelRequested = Signal()

    _MODES = (('rect', '矩形'), ('poly', '多边形'),
              ('free', '手绘'), ('object', '选对象'))
    _HINTS = {'rect': '两点绘制矩形云线',
              'poly': '连续点击，右击闭合完成',
              'free': '按住鼠标沿路径拖动，松开完成',
              'object': '点击一条闭合曲线，以它为边界生成云线'}

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._addCoordRows()
        self._modeBtns = {}
        row = QHBoxLayout()
        for mode, text in self._MODES:
            btn = self._button2(text)
            btn.setCheckable(True)
            btn.setStyleSheet(
                'QPushButton:checked { background:#15b6c9; color:#ffffff; }')
            btn.clicked.connect(lambda _=False, m=mode: self._onMode(m))
            self._modeBtns[mode] = btn
            row.addWidget(btn)
        self._layout.addLayout(row)
        self._modeBtns['rect'].setChecked(True)
        self._hint = self._label(self._HINTS['rect'])
        self._layout.addWidget(self._hint)

        # 长/宽（边界外接框尺寸；矩形模式可编辑对角，选对象模式禁用）
        self._spinLen = self._spin(0.0, step=0.5)
        self._spinWid = self._spin(0.0, step=0.5)
        self._spinLen.setMinimum(0.0)
        self._spinWid.setMinimum(0.0)
        self._spinLen.valueChanged.connect(self._emitSize)
        self._spinWid.valueChanged.connect(self._emitSize)
        rowLen = QHBoxLayout()
        rowLen.setSpacing(4)
        labLen = self._label('长')
        labLen.setMinimumWidth(28)
        rowLen.addWidget(labLen)
        rowLen.addWidget(self._spinLen, 1)
        self._layout.addLayout(rowLen)
        rowWid = QHBoxLayout()
        rowWid.setSpacing(4)
        labWid = self._label('宽')
        labWid.setMinimumWidth(28)
        rowWid.addWidget(labWid)
        rowWid.addWidget(self._spinWid, 1)
        self._layout.addLayout(rowWid)

        # 单弧尺寸（每段弧的弦长；0 = 自适应）
        self._spinArc = self._spin(0.0, step=0.5)
        self._spinArc.setMinimum(0.0)
        self._spinArc.valueChanged.connect(self._emitArc)
        rowArc = QHBoxLayout()
        rowArc.setSpacing(4)
        labArc = self._label('单弧')
        labArc.setMinimumWidth(28)
        rowArc.addWidget(labArc)
        rowArc.addWidget(self._spinArc, 1)
        self._layout.addLayout(rowArc)

        btnOk = self._button('确认完成', primary=True)
        btnOk.clicked.connect(self.confirmRequested.emit)
        self._layout.addWidget(btnOk)
        self._layout.addStretch(1)

    def _emitSize(self) -> None:
        self.sizeChanged.emit(self._spinLen.value(), self._spinWid.value())

    def _emitArc(self) -> None:
        self.arcLenChanged.emit(self._spinArc.value())

    def setSize(self, length: float, width: float) -> None:
        for s, v in ((self._spinLen, length), (self._spinWid, width)):
            s.blockSignals(True)
            s.setValue(v)
            s.blockSignals(False)

    def setArcLen(self, v: float) -> None:
        """回写实际生效的单弧尺寸（blockSignals 防回环）。"""
        self._spinArc.blockSignals(True)
        self._spinArc.setValue(v)
        self._spinArc.blockSignals(False)

    def setSizesEnabled(self, on: bool) -> None:
        self._spinLen.setEnabled(on)
        self._spinWid.setEnabled(on)

    def _onMode(self, mode: str) -> None:
        for m, btn in self._modeBtns.items():
            btn.blockSignals(True)
            btn.setChecked(m == mode)
            btn.blockSignals(False)
        self._hint.setText(self._HINTS.get(mode, ''))
        self.setSizesEnabled(mode in ('rect', 'poly', 'free'))
        self.modeChanged.emit(mode)

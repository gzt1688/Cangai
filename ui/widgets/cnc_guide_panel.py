"""CNC 向导面板：画布左侧显示精雕 7 步流程，可展开/折叠。

每步是一个可展开菜单：标题行（三角+图标+名称，主题色背景）+ 内容区（摘要+设置按钮）。
点击标题行切换展开/折叠，"设置"按钮发 stepRequested 信号路由到命令。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal, QEvent
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                                QPushButton, QFrame, QScrollArea, QCheckBox,
                                QColorDialog)

import ui.styles.theme as theme
from core.cnc.state import (CncState, STEP_ORDER, STEP_LABELS, STEP_STOCK,
                            STEP_TOOLS, STEP_TOOLPATH, STEP_SIMULATE)


class _PathCheckBox(QCheckBox):
    """带框的勾选框：未选=白底灰框，选中=主题色底+白色对勾。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(18, 18)

    def paintEvent(self, event) -> None:
        from PySide6.QtCore import QRectF, QPointF
        from PySide6.QtGui import QPainter, QColor, QPen, QPolygonF
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(1.5, 1.5, self.width() - 3, self.height() - 3)
        if self.isChecked():
            p.setPen(QPen(QColor(theme.PRIMARY), 1))
            p.setBrush(QColor(theme.PRIMARY))
            p.drawRoundedRect(r, 3, 3)
            pen = QPen(QColor("#ffffff"), 2, Qt.PenStyle.SolidLine,
                       Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPolyline(QPolygonF([
                QPointF(r.left() + 3.0, r.center().y()),
                QPointF(r.center().x() - 0.5, r.bottom() - 3.0),
                QPointF(r.right() - 2.5, r.top() + 3.5)]))
        else:
            p.setPen(QPen(QColor("#9a9a9a"), 1))
            p.setBrush(QColor("#ffffff"))
            p.drawRoundedRect(r, 3, 3)
        p.end()


class _CollapsibleStep(QWidget):
    """可展开/折叠的单步：标题行 + 内容区。"""
    stepRequested = Signal(str)

    def __init__(self, step: str, index: int) -> None:
        super().__init__()
        self.setStyleSheet("background:%s;" % theme.BG_PANEL)
        self._step = step
        self._index = index
        self._expanded = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ---- 标题行（主题色背景）----
        self._header = QFrame()
        self._header.setObjectName("cncStepHeader")
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.installEventFilter(self)
        hLay = QHBoxLayout(self._header)
        hLay.setContentsMargins(8, 6, 8, 6)
        hLay.setSpacing(6)

        self._arrow = QLabel("▶")
        self._arrow.setFixedWidth(12)
        self._arrow.setStyleSheet(
            "color:%s; font-size:10px; font-weight:bold; background:transparent;" % theme.TEXT_ON_PRIMARY)
        hLay.addWidget(self._arrow)

        self._icon = QLabel("○")
        self._icon.setFixedWidth(22)
        self._icon.setStyleSheet(
            "color:%s; font-size:16px; font-weight:bold;"
            " background:transparent;" % theme.TEXT_ON_PRIMARY)
        hLay.addWidget(self._icon)

        self._name = QLabel(f"{index+1}. {STEP_LABELS[step]}")
        self._name.setStyleSheet(
            "font-weight:bold; font-size:12px; color:%s; background:transparent;" % theme.TEXT_ON_PRIMARY)
        hLay.addWidget(self._name)
        hLay.addStretch(1)

        self._header.setStyleSheet(
            "QFrame#cncStepHeader { background:%s; border:none; }"
            "QFrame#cncStepHeader:hover { background:%s; }"
            % (theme.PRIMARY, theme.PRIMARY_DARK))
        lay.addWidget(self._header)

        # ---- 内容区 ----
        self._content = QWidget()
        self._content.setStyleSheet("background:%s;" % theme.BG_PANEL)
        self._cLay = QVBoxLayout(self._content)
        self._cLay.setContentsMargins(0, 6, 10, 8)
        self._cLay.setSpacing(6)

        self._summary = QLabel("")
        self._summary.setStyleSheet(
            "color:%s; font-size:10px;" % theme.TEXT_DIM)
        self._summary.setWordWrap(True)
        self._cLay.addWidget(self._summary)

        self._btn = QPushButton("设置")
        if step == STEP_TOOLPATH:
            self._btn.setText("添加路径")
        elif step == STEP_TOOLS:
            self._btn.setText("添加刀具")
        elif step == STEP_SIMULATE:
            self._btn.setText("开始模拟")
        self._btn.setStyleSheet(
            "QPushButton { background:%s; color:%s; border:none;"
            " padding:4px 14px; border-radius:3px; font-size:11px; }"
            "QPushButton:hover { background:%s; }"
            % (theme.PRIMARY, theme.TEXT_ON_PRIMARY, theme.PRIMARY_DARK))
        self._btn.clicked.connect(lambda: self.stepRequested.emit(self._step))
        # 按钮行：按钮右对齐，可插入同行动控件（如"显示刀具"勾选框）
        self._btnRow = QHBoxLayout()
        self._btnRow.setContentsMargins(0, 0, 0, 0)
        self._btnRow.addStretch(1)
        self._btnRow.addWidget(self._btn)
        self._cLay.addLayout(self._btnRow)
        lay.addWidget(self._content)
        # 默认折叠
        self._content.setVisible(False)

    def addWidget(self, widget: QWidget) -> None:
        """在内容区摘要与按钮行之间插入自定义控件。"""
        idx = self._cLay.indexOf(self._btnRow)
        self._cLay.insertWidget(idx, widget)

    def addToButtonRow(self, widget: QWidget) -> None:
        """把控件加到按钮同一行（按钮左侧）。"""
        self._btnRow.insertWidget(0, widget)

    def eventFilter(self, obj, event):
        if obj is self._header and event.type() == QEvent.MouseButtonPress:
            if event.button() == Qt.MouseButton.LeftButton:
                self.toggleExpanded()
                return True
        return super().eventFilter(obj, event)

    def toggleExpanded(self) -> None:
        self._expanded = not self._expanded
        self._arrow.setText("▼" if self._expanded else "▶")
        self._content.setVisible(self._expanded)

    def setExpanded(self, expanded: bool) -> None:
        if self._expanded != expanded:
            self.toggleExpanded()

    def updateState(self, done: bool, summary: str, current: bool) -> None:
        if done:
            self._icon.setText("✓")
            self._icon.setStyleSheet(
                "color:%s; font-size:20px; font-weight:900;"
                " background:transparent;" % theme.TEXT_ON_PRIMARY)
        else:
            self._icon.setText("●" if current else "○")
            self._icon.setStyleSheet(
                "color:%s; font-size:16px; font-weight:bold;"
                " background:transparent;" % theme.TEXT_ON_PRIMARY)
        self._summary.setText(summary)
        # 摘要为空时隐藏标签（如刀具步骤信息已由列表展示，避免重复）
        self._summary.setVisible(bool(summary))


class CncGuidePanel(QWidget):
    """CNC 向导面板：7 步流程，可展开/折叠。"""
    stepRequested = Signal(str)
    editPathRequested = Signal(object)   # 单击路径标题 → 请求编辑该条目

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("cncGuidePanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "QWidget#cncGuidePanel { background:%s; }" % theme.BG_PANEL)
        self._doc = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # 步骤滚动区
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(
            "QScrollArea { background:%s; border:none; }" % theme.BG_PANEL)
        container = QWidget()
        container.setStyleSheet("background:%s;" % theme.BG_PANEL)
        cLay = QVBoxLayout(container)
        cLay.setContentsMargins(0, 0, 0, 0)
        cLay.setSpacing(0)
        self._steps: dict = {}
        for index, step in enumerate(STEP_ORDER):
            item = _CollapsibleStep(step, index)
            item.stepRequested.connect(self.stepRequested.emit)
            cLay.addWidget(item)
            self._steps[step] = item
        cLay.addStretch(1)
        scroll.setWidget(container)
        lay.addWidget(scroll, 1)

        # 底部提示条
        self._hint = QLabel("")
        self._hint.setStyleSheet(
            "color:%s; font-size:10px; padding:6px 10px; background:%s;"
            " border-top:1px solid %s;"
            % (theme.TEXT_DIM, theme.BG_PANEL, theme.BORDER))
        self._hint.setWordWrap(True)
        lay.addWidget(self._hint)

        # ---- 安装坯料步骤：显示毛坯勾选框（与"设置"按钮同一行）----
        self._showStockCheck = QCheckBox("显示毛坯")
        self._showStockCheck.setStyleSheet(
            "color:%s; font-size:11px; padding:2px 0;"
            % theme.TEXT_DIM)
        stockStep = self._steps[STEP_STOCK]
        stockStep.addToButtonRow(self._showStockCheck)

        # ---- 刀具步骤：当前使用刀具列表（每行可删除，无标题，直接列出）----
        toolsStep = self._steps[STEP_TOOLS]
        self._toolListWidget = QWidget()
        self._toolListWidget.setStyleSheet("background:transparent;")
        self._toolListLay = QVBoxLayout(self._toolListWidget)
        self._toolListLay.setContentsMargins(0, 0, 0, 0)
        self._toolListLay.setSpacing(2)
        toolsStep.addWidget(self._toolListWidget)
        self._toolListWidget.setVisible(False)

        # ---- 实体模拟步骤：模拟路径勾选列表 + 显示刀具勾选框 ----
        simStep = self._steps[STEP_SIMULATE]
        self._simPathWidget = QWidget()
        self._simPathWidget.setStyleSheet("background:transparent;")
        self._simPathLay = QVBoxLayout(self._simPathWidget)
        self._simPathLay.setContentsMargins(0, 0, 0, 0)
        self._simPathLay.setSpacing(2)
        simStep.addWidget(self._simPathWidget)
        self._simPathWidget.setVisible(False)
        self._showToolsCheck = QCheckBox("显示刀具")
        self._showToolsCheck.setStyleSheet(
            "color:%s; font-size:11px; padding:2px 0;"
            % theme.TEXT_DIM)
        simStep.addToButtonRow(self._showToolsCheck)
        self._showSolidCheck = QCheckBox("实体")
        self._showSolidCheck.setStyleSheet(
            "color:%s; font-size:11px; padding:2px 0;"
            % theme.TEXT_DIM)
        simStep.addToButtonRow(self._showSolidCheck)

        # ---- 路径生成步骤：路径条目列表（勾选+标题+颜色+删除）----
        toolpathStep = self._steps[STEP_TOOLPATH]
        self._pathListWidget = QWidget()
        self._pathListWidget.setStyleSheet("background:transparent;")
        self._pathListLay = QVBoxLayout(self._pathListWidget)
        self._pathListLay.setContentsMargins(0, 0, 0, 0)
        self._pathListLay.setSpacing(2)
        toolpathStep.addWidget(self._pathListWidget)
        self._pathListWidget.setVisible(False)

        # ---- 状态绑定 ----
        self._state = CncState.instance()
        self._showStockCheck.setChecked(self._state.showStock)
        self._showStockCheck.toggled.connect(self._onShowStockToggled)
        self._showToolsCheck.setChecked(self._state.showTools)
        self._showToolsCheck.toggled.connect(self._onShowToolsToggled)
        self._showSolidCheck.setChecked(self._state.showSolidSim)
        self._showSolidCheck.toggled.connect(self._onShowSolidToggled)
        self._state.stateChanged.connect(self.refresh)
        self.refresh()

    def setDocument(self, doc) -> None:
        """绑定文档实例，勾选毛坯时更新画布对象。"""
        self._doc = doc

    def _onShowStockToggled(self, checked: bool) -> None:
        self._state.showStock = checked
        from core.cnc.service import update_stock_box
        update_stock_box(self._doc, self._state)
        self._state.notifyChanged()

    def _onShowToolsToggled(self, checked: bool) -> None:
        self._state.showTools = checked
        self._state.notifyChanged()

    def _onShowSolidToggled(self, checked: bool) -> None:
        """实体模拟勾选：构建/清空体素坯料（2.5D 高度场切削内核）。"""
        self._state.showSolidSim = bool(checked)
        try:
            if checked:
                from core.cnc.voxel_sim import ensure_stock_voxel
                vs = ensure_stock_voxel(self._state)
                if vs is None:
                    # 无有效毛坯：无法构建，退回未勾选
                    self._state.showSolidSim = False
                    self._showSolidCheck.blockSignals(True)
                    self._showSolidCheck.setChecked(False)
                    self._showSolidCheck.blockSignals(False)
                else:
                    vs.reset()
            else:
                from core.cnc.voxel_sim import clear_stock_voxel
                clear_stock_voxel(self._state)
        except Exception:
            import traceback
            traceback.print_exc()
        self._state.notifyChanged()

    def refresh(self) -> None:
        """刷新所有步骤状态，自动展开当前未完成步骤。"""
        nextStep = self._state.nextUndoneStep()
        for step, item in self._steps.items():
            done = self._state.isStepDone(step)
            summary = self._state.stepSummary(step)
            current = (step == nextStep)
            item.updateState(done, summary, current)
            if current:
                item.setExpanded(True)
        # 勾选框与状态同步（避免外部改动后不一致）
        chk = self._showStockCheck
        if chk.isChecked() != self._state.showStock:
            chk.blockSignals(True)
            chk.setChecked(self._state.showStock)
            chk.blockSignals(False)
        chk2 = self._showToolsCheck
        if chk2.isChecked() != self._state.showTools:
            chk2.blockSignals(True)
            chk2.setChecked(self._state.showTools)
            chk2.blockSignals(False)
        if nextStep is not None:
            self._hint.setText(f"下一步：{STEP_LABELS[nextStep]}")
        else:
            self._hint.setText("全部完成，可输出 G 代码")
        self._rebuildToolList()
        self._rebuildPathList()
        self._rebuildSimPathList()

    def _delButton(self, tip: str) -> QPushButton:
        """行尾删除按钮（×）：小号灰字，悬停变红。"""
        b = QPushButton("×")
        b.setFixedSize(16, 16)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setToolTip(tip)
        b.setStyleSheet(
            "QPushButton { border:none; color:%s; font-size:13px;"
            " background:transparent; line-height:1; }"
            "QPushButton:hover { color:#e5484d; font-weight:bold; }"
            % theme.TEXT_DIM)
        return b

    def _rebuildToolList(self) -> None:
        """重建刀具步骤的当前刀具列表：每行 T编号 名称 Ø直径 + 删除。"""
        lay = self._toolListLay
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        tcs = self._state.toolControllers
        self._toolListWidget.setVisible(bool(tcs))
        for tc in list(tcs):
            t = tc.tool
            dia = t.geometry.get("diameter", 0.0) if hasattr(t, "geometry") else 0.0
            row = QWidget()
            row.setStyleSheet(
                "background:transparent; border-bottom:1px solid %s;" % theme.BORDER)
            rowLay = QHBoxLayout(row)
            rowLay.setContentsMargins(0, 0, 0, 0)
            rowLay.setSpacing(4)
            name = getattr(t, "name", "刀具")
            lbl = QLabel(f"T{tc.tool_number} {name} Ø{dia:g}")
            lbl.setStyleSheet(
                "QLabel { color:%s; font-size:11px; background:transparent; }"
                % theme.TEXT)
            lbl.setToolTip(f"{name} 直径 {dia:g}mm")
            rowLay.addWidget(lbl, 1)
            dbtn = self._delButton(f"删除刀具 {name}")
            dbtn.clicked.connect(lambda _=False, c=tc: self._removeTool(c))
            rowLay.addWidget(dbtn)
            lay.addWidget(row)

    def _removeTool(self, tc) -> None:
        """删除一条当前使用刀具（ToolController），路径引用它的会失效前提示。"""
        from PySide6.QtWidgets import QMessageBox
        t = tc.tool
        name = getattr(t, "name", "刀具")
        used = False
        for e in self._state.pathEntries:
            ti = e.params.get("_toolIndex")
            if ti is not None:
                try:
                    if 0 <= int(ti) < len(self._state.toolControllers) \
                            and self._state.toolControllers[int(ti)] is tc:
                        used = True
                        break
                except (TypeError, ValueError):
                    pass
        if used:
            ret = QMessageBox.question(
                self, "删除刀具",
                f"确定删除刀具 {name}？\n已有路径正在使用它，删除后该路径将无法生成/模拟。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
        else:
            ret = QMessageBox.question(
                self, "删除刀具", f"确定删除刀具 {name}？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        if tc in self._state.toolControllers:
            self._state.toolControllers.remove(tc)
            self._state.notifyChanged()

    def _rebuildSimPathList(self) -> None:
        """重建实体模拟步骤的路径勾选列表：勾选=参与模拟。"""
        lay = self._simPathLay
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        entries = self._state.pathEntries
        self._simPathWidget.setVisible(bool(entries))
        for entry in entries:
            row = QWidget()
            row.setStyleSheet(
                "background:transparent; border-bottom:1px solid %s;" % theme.BORDER)
            rowLay = QHBoxLayout(row)
            rowLay.setContentsMargins(0, 0, 0, 0)
            rowLay.setSpacing(4)
            chk = _PathCheckBox()
            chk.setChecked(entry.simEnabled)
            chk.setToolTip(f"模拟/跳过 {entry.title}")
            chk.toggled.connect(
                lambda checked, e=entry: self._onSimPathToggled(e, checked))
            rowLay.addWidget(chk)
            lbl = QLabel(entry.title)
            lbl.setStyleSheet(
                "QLabel { color:%s; font-size:11px; background:transparent; }"
                % theme.TEXT)
            rowLay.addWidget(lbl, 1)
            dbtn = self._delButton(f"删除路径 {entry.title}")
            dbtn.clicked.connect(lambda _=False, e=entry: self._removePath(e))
            rowLay.addWidget(dbtn)
            lay.addWidget(row)

    def _removePath(self, entry) -> None:
        """删除路径条目（同时从模拟列表移除）。"""
        from PySide6.QtWidgets import QMessageBox
        ret = QMessageBox.question(
            self, "删除路径", f"确定删除路径 {entry.title}？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._state.removePathEntry(entry)

    def _onSimPathToggled(self, entry, checked: bool) -> None:
        entry.simEnabled = checked
        self._state.notifyChanged()

    def _rebuildPathList(self) -> None:
        """重建路径生成步骤的条目列表：勾选框(显示开关) + 可点击标题(编辑)。"""
        lay = self._pathListLay
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        entries = self._state.pathEntries
        self._pathListWidget.setVisible(bool(entries))
        for entry in entries:
            row = QWidget()
            row.setStyleSheet(
                "background:transparent; border-bottom:1px solid %s;" % theme.BORDER)
            rowLay = QHBoxLayout(row)
            rowLay.setContentsMargins(0, 0, 0, 0)
            rowLay.setSpacing(4)

            chk = _PathCheckBox()
            chk.setChecked(entry.visible)
            chk.setToolTip(f"显示/隐藏 {entry.title}")
            chk.toggled.connect(
                lambda checked, e=entry: self._state.setEntryVisible(e, checked))
            rowLay.addWidget(chk)

            lbl = QLabel(entry.title)
            lbl.setCursor(Qt.CursorShape.PointingHandCursor)
            lbl.setStyleSheet(
                "QLabel { color:%s; font-size:11px; background:transparent;"
                " text-decoration:underline; }"
                "QLabel:hover { color:%s; }"
                % (theme.TEXT, theme.PRIMARY))
            lbl.mousePressEvent = lambda ev, e=entry: self.editPathRequested.emit(e)
            rowLay.addWidget(lbl, 1)

            # 行尾颜色块：点击弹取色器，设置该路径在画布中的显示颜色
            colorBtn = QPushButton()
            colorBtn.setFixedSize(16, 16)
            colorBtn.setCursor(Qt.CursorShape.PointingHandCursor)
            colorBtn.setToolTip("设置路径显示颜色")
            colorBtn.setStyleSheet(
                "QPushButton { background:%s; border:1px solid #c5c5c5;"
                " border-radius:3px; }"
                "QPushButton:hover { border:1px solid %s; }"
                % (entry.color or "#dddddd", theme.PRIMARY))
            colorBtn.clicked.connect(
                lambda _=False, e=entry, b=colorBtn: self._pickEntryColor(e, b))
            rowLay.addWidget(colorBtn)
            dbtn = self._delButton(f"删除路径 {entry.title}")
            dbtn.clicked.connect(lambda _=False, e=entry: self._removePath(e))
            rowLay.addWidget(dbtn)
            lay.addWidget(row)

    def _pickEntryColor(self, entry, btn: QPushButton) -> None:
        """弹取色器改路径颜色，颜色块与画布同步刷新。"""
        color = QColorDialog.getColor(QColor(entry.color or "#dddddd"), self,
                                      "设置路径颜色")
        if not color.isValid():
            return
        entry.color = color.name()
        btn.setStyleSheet(
            "QPushButton { background:%s; border:1px solid #c5c5c5;"
            " border-radius:3px; }"
            "QPushButton:hover { border:1px solid %s; }"
            % (color.name(), theme.PRIMARY))
        self._state.notifyChanged()

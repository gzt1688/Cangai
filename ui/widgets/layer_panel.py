"""图层面板：四个固定图层（曲线/模型/图片/标注）为根，其下可建子图层。

- 固定根层：曲线(0)/模型(1)/图片(2)/标注(3)，不可删除、改名、调序；
  对象按类型自动归入曲线/模型/图片，“标注”层为将来标注功能预留；
  显示/锁定开关会级联到其下全部子图层。
- 子图层：在某固定层下新建（“新建”按钮），用于同族细分归档，可改名、改色、
  删除、上下移动；同样具有“当前/显示/锁定/颜色”等基本图层属性。
- “当前”勾选层 = 新对象放置目标：新对象先按类型落在对应固定族，若当前层是
  该族的子图层，则落入该子图层，否则落在固定层本身。
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QHBoxLayout,
                               QHeaderView, QLabel, QPushButton, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from core.document import LAYER_CURVES_ID, isFixedLayerId
from i18n import tr, languageChanged

# 表头中文原文（retranslateUi 用）
_LAYER_HEADERS = ["名称", "数量", "显示", "锁定", "颜色"]


class LayerPanel(QWidget):
    layerDataChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._doc = None
        self._refreshing = False
        self._pendingRefresh = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(4)


        tool = QHBoxLayout()
        tool.setContentsMargins(0, 0, 0, 0)
        tool.setSpacing(4)
        self._btnNew = QPushButton(tr("新建"), self)
        self._btnDelete = QPushButton(tr("删除"), self)
        self._btnUp = QPushButton(tr("向上"), self)
        self._btnDown = QPushButton(tr("向下"), self)
        self._btnNew.setToolTip(tr("在所属固定层下新建子图层"))
        self._btnDelete.setToolTip(tr("删除当前子图层（固定层不可删除）"))
        self._btnUp.setToolTip(tr("同一固定层内上移当前子图层"))
        self._btnDown.setToolTip(tr("同一固定层内下移当前子图层"))
        for b in (self._btnNew, self._btnDelete, self._btnUp, self._btnDown):
            b.setFixedSize(48, 24)
            b.setFocusPolicy(Qt.NoFocus)
            tool.addWidget(b)
        tool.addStretch()
        layout.addLayout(tool)

        self._btnNew.clicked.connect(self._onAddLayer)
        self._btnDelete.clicked.connect(self._onRemoveLayer)
        self._btnUp.clicked.connect(lambda: self._onMoveLayer(-1))
        self._btnDown.clicked.connect(lambda: self._onMoveLayer(1))

        self._tree = QTreeWidget(self)
        self._tree.setHeaderLabels([tr(h) for h in _LAYER_HEADERS])
        self._tree.setColumnWidth(0, 100)
        self._tree.setColumnWidth(1, 40)
        self._tree.setColumnWidth(2, 40)
        self._tree.setColumnWidth(3, 40)
        self._tree.setAlternatingRowColors(True)
        self._tree.setRootIsDecorated(True)
        self._tree.setIndentation(12)
        self._tree.setEditTriggers(QTreeWidget.DoubleClicked |
                                   QTreeWidget.EditKeyPressed)
        self._tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        self._tree.setSelectionBehavior(QTreeWidget.SelectRows)
        self._tree.setUniformRowHeights(True)
        self._tree.setAllColumnsShowFocus(False)
        self._tree.setFocusPolicy(Qt.ClickFocus)
        self._tree.itemChanged.connect(self._onItemChanged)
        self._tree.itemClicked.connect(self._onRowClicked)
        self._tree.installEventFilter(self)
        header = self._tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        # 数量/显示/锁定列固定 44px（原 ResizeToContents 约 28px）：
        # 名称列是弹性列吃掉剩余宽度，中间三列各放宽 16px = 名称列缩窄约 48px，
        # 视觉上明显变窄；Interactive 允许用户手动拖动微调
        for i in range(1, 4):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
            self._tree.setColumnWidth(i, 44)
        # 颜色列固定窄宽（色块按钮 22px 足够），ResizeToContents 会被
        # 表头文字"颜色"撑宽，挤占名称列。宽度须在切到 Fixed 模式后设置，
        # 否则冻结的是 ResizeToContents 算出的旧宽度
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self._tree.setColumnWidth(4, 30)
        # 默认 stretchLastSection 会拖拽最后一列吃掉剩余宽度，须关闭，
        # 否则颜色列的固定宽度不生效（剩余宽度应给名称列）
        header.setStretchLastSection(False)
        layout.addWidget(self._tree)
        languageChanged().connect(self.retranslateUi)

    def retranslateUi(self) -> None:
        """语言切换：重设按钮、表头，并重建树以刷新行内 tooltip。"""
        self._btnNew.setText(tr("新建"))
        self._btnDelete.setText(tr("删除"))
        self._btnUp.setText(tr("向上"))
        self._btnDown.setText(tr("向下"))
        self._btnNew.setToolTip(tr("在所属固定层下新建子图层"))
        self._btnDelete.setToolTip(tr("删除当前子图层（固定层不可删除）"))
        self._btnUp.setToolTip(tr("同一固定层内上移当前子图层"))
        self._btnDown.setToolTip(tr("同一固定层内下移当前子图层"))
        self._tree.setHeaderLabels([tr(h) for h in _LAYER_HEADERS])
        self._refreshLayerUI()

    # ---------------- 绑定与刷新 ----------------

    def setDocument(self, doc) -> None:
        """绑定文档：图层数据以 Document 为准（当前层/显隐/锁定/颜色联动）。"""
        if self._doc is not None and self._doc is not doc:
            try:
                self._doc.objectAdded.disconnect(self.refreshLater)
                self._doc.objectRemoved.disconnect(self.refreshLater)
                self._doc.objectChanged.disconnect(self.refreshLater)
            except RuntimeError:
                pass
        self._doc = doc
        if doc is not None:
            doc.objectAdded.connect(self.refreshLater)
            doc.objectRemoved.connect(self.refreshLater)
            doc.objectChanged.connect(self.refreshLater)
        self._refreshLayerUI()

    def refreshLater(self) -> None:
        """延迟刷新：合并多次 layerChanged，避免在信号处理中重建控件。"""
        if self._doc is None or self._pendingRefresh:
            return
        self._pendingRefresh = True
        QTimer.singleShot(0, self._flushRefresh)

    def _flushRefresh(self) -> None:
        self._pendingRefresh = False
        self._refreshLayerUI()

    # ---------------- 事件处理 ----------------

    def eventFilter(self, obj, event):
        if event.type() == QEvent.MouseButtonPress and isinstance(obj, QLabel):
            lid = obj.property("layerId")
            act = obj.property("layerAction")
            if lid is not None and act is not None and self._doc is not None:
                layer = self._doc.layer(int(lid))
                if layer is not None:
                    if act == "visible":
                        self._doc.setLayerFlag(int(lid),
                                               visible=not layer.visible)
                    elif act == "lock":
                        self._doc.setLayerFlag(int(lid),
                                               locked=not layer.locked)
                return True   # 已处理：避免冒泡成“选中该行=切换当前层”
        return super().eventFilter(obj, event)

    def _onRowClicked(self, item: QTreeWidgetItem, column: int) -> None:
        """单击图层行 = 设为当前层（新建的同族对象将放入该层）。"""
        if self._doc is None:
            return
        lid = item.data(0, Qt.UserRole)
        if lid is None:
            return
        lid = int(lid)
        if self._doc.activeLayerId() != lid:
            self._doc.setLayerActive(lid)
            self._refreshLayerUI()

    # ---------------- 界面刷新 ----------------

    def _refreshLayerUI(self) -> None:
        if self._doc is None:
            return
        layers = self._doc.layers()
        if not layers:
            return
        cur = self._doc.activeLayerId()
        ids = {l.id for l in layers}
        if cur not in ids:
            cur = LAYER_CURVES_ID
        self._refreshing = True
        try:
            self._tree.clear()
            rootItems = {}
            # 固定层为根行
            for layer in layers:
                if not isFixedLayerId(layer.id):
                    continue
                rootItems[layer.id] = self._appendRow(None, layer, layer.id == cur)
            # 子图层挂在所属固定层下（顺序按 layers()）
            for layer in layers:
                if isFixedLayerId(layer.id):
                    continue
                parentItem = rootItems.get(layer.parentId) \
                    or rootItems.get(LAYER_CURVES_ID)
                self._appendRow(parentItem, layer, layer.id == cur)
            for it in rootItems.values():
                it.setExpanded(True)
        finally:
            self._refreshing = False
        self._updateButtonStates()

    def _appendRow(self, parentItem, layer, isCurrent: bool) -> QTreeWidgetItem:
        fixed = isFixedLayerId(layer.id)
        item = QTreeWidgetItem(parentItem if parentItem is not None
                               else self._tree)
        item.setText(0, layer.name)
        item.setData(0, Qt.UserRole, layer.id)
        if fixed:
            # 固定层名称加粗，且不可改名
            f = item.font(0)
            f.setBold(True)
            item.setFont(0, f)
        else:  # 子图层名称可编辑
            item.setFlags(item.flags() | Qt.ItemIsEditable)
        if isCurrent:
            # 当前层行高亮（取消勾选框后用背景表示）
            item.setBackground(0, QColor(0, 120, 215, 40))
            item.setForeground(0, QColor(0, 84, 160))

        # 数量列：该图层（含子图层）对象数；点击行 = 设为当前层
        cnt = QLabel(str(self._doc.layerObjectCount(layer.id)))
        cnt.setAlignment(Qt.AlignCenter)
        cnt.setToolTip(tr("该图层（含子图层）的对象数量"))
        cnt.setStyleSheet("background:transparent;")
        self._tree.setItemWidget(item, 1, cnt)

        bulb = QLabel("💡" if layer.visible else "●")
        bulb.setAlignment(Qt.AlignCenter)
        bulb.setCursor(Qt.PointingHandCursor)
        bulb.setToolTip(tr("显示 / 隐藏该图层（固定层会联动其子图层）"))
        bulb.setStyleSheet("font-size:20px; background:transparent;")
        bulb.setProperty("layerId", layer.id)
        bulb.setProperty("layerAction", "visible")
        bulb.installEventFilter(self)
        self._tree.setItemWidget(item, 2, bulb)

        lock = QLabel("🔒" if layer.locked else "🔓")
        lock.setAlignment(Qt.AlignCenter)
        lock.setCursor(Qt.PointingHandCursor)
        lock.setToolTip(tr("锁定 / 解锁该图层（固定层会联动其子图层）"))
        lock.setStyleSheet("font-size:17px; background:transparent;")
        lock.setProperty("layerId", layer.id)
        lock.setProperty("layerAction", "lock")
        lock.installEventFilter(self)
        self._tree.setItemWidget(item, 3, lock)

        colorBtn = QPushButton()
        colorBtn.setFixedSize(22, 20)
        colorBtn.setFocusPolicy(Qt.NoFocus)
        colorBtn.setToolTip(tr("修改该图层颜色（同层对象颜色随之更新）"))
        colorBtn.setStyleSheet(
            f"background-color:{'#{:02x}{:02x}{:02x}'.format(*layer.color)};"
            "border:1px solid #999;")
        colorBtn.setProperty("layerId", layer.id)
        colorBtn.clicked.connect(
            lambda _, lid=layer.id: self._onColorClicked(int(lid)))
        self._tree.setItemWidget(item, 4, colorBtn)
        return item

    def _updateButtonStates(self) -> None:
        if self._doc is None:
            self._btnDelete.setEnabled(False)
            self._btnUp.setEnabled(False)
            self._btnDown.setEnabled(False)
            return
        cur = self._doc.activeLayerId()
        layer = self._doc.layer(cur)
        if layer is None:
            self._btnDelete.setEnabled(False)
            self._btnUp.setEnabled(False)
            self._btnDown.setEnabled(False)
            return
        fixed = isFixedLayerId(cur)
        self._btnDelete.setEnabled(not fixed)
        if fixed:
            self._btnUp.setEnabled(False)
            self._btnDown.setEnabled(False)
            return
        bros = self._doc.childLayers(layer.parentId)
        ids = [b.id for b in bros]
        i = ids.index(cur) if cur in ids else -1
        self._btnUp.setEnabled(i > 0)
        self._btnDown.setEnabled(0 <= i < len(bros) - 1)

    # ---------------- 交互处理 ----------------

    def _onColorClicked(self, lid: int) -> None:
        if self._doc is None:
            return
        layer = self._doc.layer(lid)
        if layer is None:
            return
        c = QColor(*layer.color)
        nc = QColorDialog.getColor(c, self, tr(f"修改「{layer.name}」图层颜色"))
        if not nc.isValid():
            return
        # Document 同步更新该图层既有对象颜色并触发刷新
        self._doc.setLayerColor(lid, (nc.red(), nc.green(), nc.blue()))
        self.layerDataChanged.emit()
        self._refreshLayerUI()

    def _onItemChanged(self, item: QTreeWidgetItem, column: int) -> None:
        if self._refreshing or column != 0 or self._doc is None:
            return
        lid = item.data(0, Qt.UserRole)
        if lid is None:
            return
        lid = int(lid)
        layer = self._doc.layer(lid)
        if layer is None or layer.name == item.text(0):
            return
        self._doc.renameLayer(lid, item.text(0))
        self.layerDataChanged.emit()

    def _onAddLayer(self) -> None:
        """在当前层所属固定层下新建子图层并设为当前层。"""
        if self._doc is None:
            return
        lid = self._doc.addLayer()
        self._doc.setLayerActive(lid)
        self._refreshLayerUI()
        self.layerDataChanged.emit()

    def _onRemoveLayer(self) -> None:
        """删除当前子图层（固定层不可删除）。"""
        if self._doc is None:
            return
        cur = self._doc.activeLayerId()
        if isFixedLayerId(cur):
            return
        self._doc.removeLayer(cur)
        self._refreshLayerUI()
        self.layerDataChanged.emit()

    def _onMoveLayer(self, delta: int) -> None:
        """在同一固定层内移动当前子图层的位置。"""
        if self._doc is None:
            return
        cur = self._doc.activeLayerId()
        if isFixedLayerId(cur):
            return
        self._doc.reorderLayer(cur, delta)
        self._refreshLayerUI()

    # ---------------- 兼容辅助 ----------------

    def currentLayerId(self) -> int:
        return self._doc.activeLayerId() if self._doc is not None else -1

    def layerIdAt(self, row: int) -> int:
        """返回指定根行对应的图层 id（-1 表示无）。"""
        item = self._tree.topLevelItem(row)
        if item is not None:
            lid = item.data(0, Qt.UserRole)
            if lid is not None:
                return int(lid)
        return -1

    def tree(self) -> QTreeWidget:
        return self._tree

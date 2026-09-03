"""场景桥：同步 Document 对象变化到 2D 场景图元。"""
from __future__ import annotations
from typing import Dict, Optional
from PySide6.QtWidgets import QGraphicsScene

from core.document import Document
from core.vector.geometry_types import ObjectType
from .item_factory import createItem, updateItem
from .items.base_item import BaseGraphicsItem

class SceneBridge:
    def __init__(self, scene: QGraphicsScene, document: Document) -> None:
        self._scene = scene
        self._doc = document
        self._items: Dict[str, BaseGraphicsItem] = {}
        self._viewScale: float = 1.0
        self._syncing = False
        # 文档选择快照：图元 itemChange 会在场景发信号前先行改写对象选中态，
        # 直接比较对象状态会漏判（如清选），需用快照判定是否发信号
        self._lastDocSelIds: set = set()
        self._doc.objectAdded.connect(self._onAdded)
        self._doc.objectRemoved.connect(self._onRemoved)
        self._doc.objectChanged.connect(self._onChanged)
        # 图层显隐变化：同步既有图元的可见性（隐藏层对象不再绘制/不可命中）
        self._doc.layerChanged.connect(self._syncLayerVisibility)
        # 场景选择变化（点选/框选/取消）→ 同步到文档选择并发出信号，
        # 驱动操作轴与属性/变换面板联动（图元 itemChange 只写对象不发信号）
        self._scene.selectionChanged.connect(self._syncSceneSelection)
        # 文档选择变化（代码选中/清空）→ 反向同步场景图元选中态，
        # 避免对象级取消选择后场景图元残留选中被后续框选/点选带入
        self._doc.selectionChanged.connect(self._syncDocSelection)

    def setDocument(self, document: Document) -> None:
        """换绑文档（多文件切换）：断开旧文档信号 → 清场按新文档重建 → 反向同步选择。"""
        if document is self._doc:
            self.rebuild()
            return
        try:
            self._doc.objectAdded.disconnect(self._onAdded)
            self._doc.objectRemoved.disconnect(self._onRemoved)
            self._doc.objectChanged.disconnect(self._onChanged)
            self._doc.layerChanged.disconnect(self._syncLayerVisibility)
            self._doc.selectionChanged.disconnect(self._syncDocSelection)
        except (RuntimeError, TypeError):
            pass
        self._doc = document
        self._doc.objectAdded.connect(self._onAdded)
        self._doc.objectRemoved.connect(self._onRemoved)
        self._doc.objectChanged.connect(self._onChanged)
        self._doc.layerChanged.connect(self._syncLayerVisibility)
        self._doc.selectionChanged.connect(self._syncDocSelection)
        # 重建期间抑制场景选择信号（移除/新增图元会触发 selectionChanged），
        # 完成后按新文档选择集做一次反向同步
        self._syncing = True
        try:
            self.rebuild()
        finally:
            self._syncing = False
        self._lastDocSelIds = set()
        self._syncDocSelection()

    def _syncDocSelection(self) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            for oid, item in self._items.items():
                obj = self._doc.object(oid)
                if obj is not None and item.isSelected() != obj.isSelected():
                    item.setSelected(obj.isSelected())
            self._lastDocSelIds = {o.id() for o in self._doc.objects() if o.isSelected()}
        finally:
            self._syncing = False

    def _syncSceneSelection(self) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            # 窗口销毁时场景 C++ 对象可能先于信号断开被释放，需容错
            try:
                # 对象图元按鸭子类型识别（hasattr objectId）：曲线/模型/图片走
                # BaseGraphicsItem，尺寸标注 DimensionGraphicsItem 独立继承
                # QGraphicsPathItem（非 BaseGraphicsItem 子类）。旧用 isinstance
                # 过滤会把标注选中排除在同步外，导致选中标注时文档不发
                # selectionChanged、变换面板绑定按钮不刷新。
                selIds = {it.objectId() for it in self._scene.selectedItems()
                          if hasattr(it, "objectId")}
            except RuntimeError:
                return
            # 与快照比较：itemChange 可能已先行同步对象态，不能只靠对象差异判断
            changed = selIds != self._lastDocSelIds
            for o in self._doc.objects():
                want = o.id() in selIds
                if o.isSelected() != want:
                    o.setSelected(want)
            self._lastDocSelIds = selIds
            if changed:
                self._doc.selectionChanged.emit()
        finally:
            self._syncing = False

    def _onAdded(self, oid: str) -> None:
        obj = self._doc.object(oid)
        if obj is None:
            return
        item = createItem(obj, self._viewScale)
        if item is None:
            return
        if obj.type() == ObjectType.SURFACE:
            # 浮雕模型压在网格(-1000)/轴(-900)之上、曲线(0)之下
            item.setZValue(-500)
        elif obj.type() == ObjectType.IMAGE:
            # 参考图片垫在最底：曲线(0)与浮雕模型(-500)之下，方便描图
            item.setZValue(-600)
        elif obj.type() == ObjectType.DIMENSION:
            # 尺寸标注浮于曲线/模型之上，便于读数不被压线遮挡
            item.setZValue(50)
        item.setVisible(self._layerVisible(obj))
        self._items[oid] = item
        self._scene.addItem(item)

    def _layerVisible(self, obj) -> bool:
        """对象所在图层是否可见（图层不存在视为可见，兼容旧数据）。
        子图层对象还受其父固定层显隐约束。"""
        layer = self._doc.layer(obj.layerId())
        if layer is None:
            return True
        if not layer.visible:
            return False
        if layer.parentId >= 0:
            parent = self._doc.layer(layer.parentId)
            return parent is None or parent.visible
        return True

    def _syncLayerVisibility(self) -> None:
        """图层显隐切换：把对应对象图元隐藏/显示（2D 画布同步）。"""
        for oid, item in self._items.items():
            obj = self._doc.object(oid)
            if obj is None:
                continue
            item.setVisible(self._layerVisible(obj))

    def _onRemoved(self, oid: str) -> None:
        item = self._items.pop(oid, None)
        if item is not None:
            self._scene.removeItem(item)

    def _onChanged(self, oid: str) -> None:
        item = self._items.get(oid)
        obj = self._doc.object(oid)
        if item is not None and obj is not None:
            updateItem(item, obj, self._viewScale)

    def updatePixelScale(self, scale: float) -> None:
        """视图缩放变化：点标记是固定屏幕尺寸，按新缩放重算其世界半径。"""
        if scale <= 1e-9 or abs(scale - self._viewScale) < 1e-12:
            return
        self._viewScale = scale
        for oid, item in self._items.items():
            obj = self._doc.object(oid)
            if obj is None:
                continue
            if obj.type() == ObjectType.POINT:
                updateItem(item, obj, scale)
            elif obj.type() == ObjectType.SURFACE:
                # 浮雕模型 boundingRect 的像素余量随缩放变，刷新几何缓存
                item.onViewScaleChanged()

    def item(self, oid: str) -> Optional[BaseGraphicsItem]:
        return self._items.get(oid)

    def objectId(self, item) -> Optional[str]:
        for oid, it in self._items.items():
            if it is item:
                return oid
        return None

    def rebuild(self) -> None:
        for item in list(self._items.values()):
            self._scene.removeItem(item)
        self._items.clear()
        for obj in self._doc.visibleObjects():
            self._onAdded(obj.id())

    def clear(self) -> None:
        for item in list(self._items.values()):
            self._scene.removeItem(item)
        self._items.clear()
"""文档模型：Document 维护对象集合与选择状态，Layer 描述图层。"""
from __future__ import annotations
import json
import os
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional
from PySide6.QtCore import QObject, Signal

from .vector.base_object import BaseObject
from .vector.line_object import LineObject
from .vector.circle_object import CircleObject
from .vector.curve_object import CurveObject
from .vector.arc_object import ArcObject
from .vector.ellipse_object import EllipseObject
from .vector.point_object import PointObject
from .vector.poly_curve_object import PolyCurveObject
from .vector.nurbs_curve_object import NurbsCurveObject
from .vector.relief_model_object import ReliefModelObject
from .vector.image_object import ImageObject
from .vector.dimension_object import DimensionObject
from .vector.brep_object import BRepObject
from .vector.revcloud_object import RevcloudObject
from .vector.geometry_types import ObjectType


_TYPE_FACTORY = {
    "LINE": LineObject,
    "CIRCLE": CircleObject,
    "CURVE": CurveObject,
    "ARC": ArcObject,
    "ELLIPSE": EllipseObject,
    "POINT": PointObject,
    "POLYCURVE": PolyCurveObject,
    "NURBS_CURVE": NurbsCurveObject,
    "SURFACE": ReliefModelObject,
    "IMAGE": ImageObject,
    "DIMENSION": DimensionObject,
    # 3D 实体（挤出/墙线/布尔/螺纹/推拉等 BRepObject 产物）
    "BREP": BRepObject,
    "REVCLOUD": RevcloudObject,
}


def objectFromDict(data: dict) -> Optional[BaseObject]:
    """根据序列化字典重建对象；未知类型返回 None。"""
    factory = _TYPE_FACTORY.get(data.get("type"))
    if factory is None:
        return None
    obj = factory()
    obj.deserialize(data)
    return obj


# ---- 固定图层（按对象类型自动归类的“族”）----
# 图层 0“曲线”/1“浮雕模型”/2“图片”/3“标注”固定存在，不可删除、改名、调序：
# 绘制/曲线类对象（含文字轮廓）自动归入“曲线”层；浮雕模型自动归入“浮雕模型”层；
# 参考图片（描图底图）自动归入“图片”层；尺寸标注对象自动归入“标注”层。
# 用户可在任一固定层下新建“子图层”做同族细分归档，子图层同样具备显隐/锁定/
# 颜色/重命名等图层属性。若“当前层”是该族的某个子图层，新建的同族对象会优先
# 落入该子图层，否则落入类型对应的固定层——自动分层规则保持不变，
# 仅多了一层同族内的手动细分。
LAYER_CURVES_ID = 0
LAYER_MODEL_ID = 1
LAYER_IMAGE_ID = 2
LAYER_ANNOTATION_ID = 3
LAYER_3D_MODEL_ID = 4
LAYER_CNC_ID = 5        # 加工模具：固定层，专放 CNC 加工模型
LAYER_FIXED_COUNT = 6   # 固定层数量；id ≥ LAYER_FIXED_COUNT 的层全部为子图层

# (id, 默认名, 默认颜色)：曲线层沿用描边主色，模型层取芥黄 #c9c909，
# 图片层取中性紫（仅作图层标识色，图片本身按原始像素显示），标注层取红色，
# 3D模型层取青绿（3D 模式挤出实体默认归此层，与曲线/浮雕模型区分）
_FIXED_LAYERS = (
    (LAYER_CURVES_ID, "曲线", (255, 100, 0)),
    (LAYER_MODEL_ID, "浮雕模型", (201, 201, 9)),
    (LAYER_IMAGE_ID, "图片", (155, 89, 182)),
    (LAYER_ANNOTATION_ID, "标注", (231, 76, 60)),
    (LAYER_3D_MODEL_ID, "3D模型", (0, 200, 200)),
    (LAYER_CNC_ID, "加工模具", (216, 175, 68)),
)


# 固定层默认可见性：所有固定层默认可见
_FIXED_LAYER_VISIBLE = (LAYER_CURVES_ID, LAYER_MODEL_ID, LAYER_IMAGE_ID,
                        LAYER_ANNOTATION_ID, LAYER_3D_MODEL_ID,
                        LAYER_CNC_ID)


def _isFixedLayer(lid: int) -> bool:
    """固定层判定：0 曲线 / 1 浮雕模型 / 2 图片 / 3 标注 / 4 3D模型 / 5 加工模具。
    固定层不可删除、改名、调序，其下可挂子图层。"""
    return LAYER_CURVES_ID <= lid < LAYER_FIXED_COUNT


def isFixedLayerId(lid: int) -> bool:
    """公开固定层判定（供图层面板等外部使用）。"""
    return _isFixedLayer(lid)


def _layerIdForObject(obj) -> int:
    """对象自动归属族根：浮雕模型(SURFACE) → 模型层；图片(IMAGE) → 图片层；
    尺寸标注(DIMENSION) → 标注层；挤出实体(SOLID)/B-Rep 实体(BREP) →
    3D模型层；其余（含 3D 曲线、文字轮廓 PolyCurveObject.isText）→ 曲线层。"""
    if obj.type() == ObjectType.SURFACE:
        return LAYER_MODEL_ID
    if obj.type() == ObjectType.IMAGE:
        return LAYER_IMAGE_ID
    if obj.type() == ObjectType.DIMENSION:
        return LAYER_ANNOTATION_ID
    if obj.type() in (ObjectType.SOLID, ObjectType.BREP):
        return LAYER_3D_MODEL_ID
    return LAYER_CURVES_ID


# 撤销/重做按“对象域”拆成相互独立的多套历史：
#   curve   → 2D/浮雕空间中非浮雕模型对象（曲线/点/文字…）的编辑；
#   relief  → 浮雕模型(SURFACE)对象（新建/区域浮雕/填色/雕刻/移动/删除…）的编辑；
#   curve3d → 3D 空间独立曲线的编辑（3D 模式单独绘制的曲线，与 2D/浮雕互不可见）。
# 各套历史各自记录、各自撤销/重做，互不混入、互不清空；
# curve3d 归入“曲线”撤销按钮（undoCurve/redoCurve 取 curve/curve3d 中较新者）。
_HISTORY_DOMAINS = ("curve", "relief", "curve3d")
_DOMAIN_CURVE3D = "curve3d"

# 文档活动空间：2D/浮雕模式共用 "2d" 空间（曲线与浮雕模型互相可见）；
# 3D 模式使用独立的 "3d" 空间（只包含 3D 模式里单独绘制的曲线，可带任意 Z），
# 切到 3D 后看不到 2D/浮雕里的曲线和模型，反之亦然。
# CNC 模式使用独立的 "cnc" 空间（毛坯 + CNC 模式下绘制的加工区域几何），
# 与 2D/浮雕、3D 均互不可见，数据完全独立。
SPACE_2D = "2d"
SPACE_3D = "3d"
SPACE_CNC = "cnc"


_CURVE_GEOM_TYPES = {ObjectType.ARC, ObjectType.CIRCLE, ObjectType.ELLIPSE,
                     ObjectType.RECTANGLE}


def _num(o, name, default=0.0) -> float:
    v = getattr(o, name, None)
    if v is None:
        return default
    try:
        return float(v()) if callable(v) else float(v)
    except Exception:
        return default


def _px(p):
    """统一取点坐标（Point3D 或列表）。"""
    if hasattr(p, "x"):
        return (p.x, p.y, p.z)
    try:
        return (p[0], p[1], p[2] if len(p) > 2 else 0.0)
    except Exception:
        return (0.0, 0.0, 0.0)


def _distinct2(pts) -> bool:
    """是否存在两个不同点（有实际可见几何）。"""
    if len(pts) < 2:
        return False
    x0, y0, z0 = _px(pts[0])
    for p in pts[1:]:
        x, y, z = _px(p)
        if abs(x - x0) > 1e-9 or abs(y - y0) > 1e-9 or abs(z - z0) > 1e-9:
            return True
    return False


def _countableObject(o) -> bool:
    """对象是否计入图层数量：与画布可选中口径一致——空几何不计。
    线/曲线系按采样几何（有实际可见线段）判定，不依赖 inputPoints；
    圆/圆弧/椭圆按实际尺寸（半径等>0）判定。"""
    t = getattr(o, "type", lambda: None)()
    if t == ObjectType.CIRCLE:
        return _num(o, "radius") > 1e-9
    if t == ObjectType.ARC:
        return (_num(o, "radius") > 1e-9
                and abs(_num(o, "endAngle") - _num(o, "startAngle")) > 1e-9)
    if t == ObjectType.ELLIPSE:
        return _num(o, "majorAxis") > 1e-9 and _num(o, "minorAxis") > 1e-9
    if t in (ObjectType.LINE, ObjectType.CURVE, ObjectType.NURBS_CURVE):
        try:
            if t == ObjectType.LINE:
                pts = list(o.points())
            else:
                pts = list(o.getSampledPoints())
            return _distinct2(pts)
        except Exception:
            return False
    if t in (ObjectType.POLYCURVE, ObjectType.RECTANGLE):
        try:
            segs = list(o.segments()) if hasattr(o, "segments") else []
            if not segs:
                poly = o.toPolycurve()
                segs = list(poly.segments()) if poly is not None else []
            pts = [p for seg in segs for p in seg.get("points", [])]
            return _distinct2(pts)
        except Exception:
            return False
    return True


@dataclass
class Layer:
    id: int
    name: str
    visible: bool = True
    locked: bool = False
    color: tuple = (255, 100, 0)
    parentId: int = -1   # 父固定层 id；-1 表示该层自身是固定根层


class Document(QObject):
    objectAdded = Signal(str)
    objectRemoved = Signal(str)
    objectChanged = Signal(str)
    selectionChanged = Signal()
    layerChanged = Signal()
    canUndoChanged = Signal(bool)
    canRedoChanged = Signal(bool)
    # 曲线 / 浮雕两套独立历史的可用状态（0 = 顶部撤销重做；1 = 浮雕组撤销重做）
    curveUndoChanged = Signal(bool)
    curveRedoChanged = Signal(bool)
    reliefUndoChanged = Signal(bool)
    reliefRedoChanged = Signal(bool)
    # 多文件系统：未保存状态变化（标签栏圆点提示）
    dirtyChanged = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self._objects: Dict[str, BaseObject] = {}
        # 3D 空间独立对象表（3D 模式单独绘制的曲线，与 2D/浮雕空间互不可见）
        self._objects3d: Dict[str, BaseObject] = {}
        # CNC 空间独立对象表（毛坯 + CNC 模式下绘制的加工区域几何）
        self._objectsCnc: Dict[str, BaseObject] = {}
        self._space: str = SPACE_2D
        self._layers: Dict[int, Layer] = {
            lid: Layer(lid, name, color=color,
                       visible=(lid in _FIXED_LAYER_VISIBLE))
            for lid, name, color in _FIXED_LAYERS}
        self._layerOrder: List[int] = [lid for lid, _, _ in _FIXED_LAYERS]
        self._nextLayerId: int = LAYER_FIXED_COUNT
        self._activeLayerId: int = LAYER_CURVES_ID
        self._defaultLineWidth: float = 1.0  # 新建对象继承的线宽（随底部线宽控件）
        # 撤销/重做：曲线、浮雕两套相互独立的历史栈。
        # 每项 = (该域操作前快照, 该域操作后快照, 单调递增序号)；
        # 序号用于跨域的“最近一次操作”排序（通用 undo/redo）。
        self._undoHistory: Dict[str, list] = {k: [] for k in _HISTORY_DOMAINS}
        self._redoHistory: Dict[str, list] = {k: [] for k in _HISTORY_DOMAINS}
        self._editBefore: Optional[dict] = None
        self._undoDepth = 50
        self._historySeq = 0
        # 文件元信息（多文件系统）：路径、显示标题、未保存标记
        self._filePath: str = ""
        self._title: str = ""
        self._dirty: bool = False

    # ---------------- 文件元信息（多文件系统） ----------------

    def filePath(self) -> str:
        """当前关联的文件路径；未保存过为空串。"""
        return self._filePath

    def setFilePath(self, path: str) -> None:
        """绑定文件路径：标题取文件主名（不含扩展名）。"""
        import os
        self._filePath = path
        self._title = os.path.splitext(os.path.basename(path))[0]

    def title(self) -> str:
        """标签栏显示标题：有文件名取文件主名，否则取“未命名N”。"""
        return self._title or "未命名"

    def setTitle(self, title: str) -> None:
        self._title = title

    def isDirty(self) -> bool:
        """自上次保存/加载后是否有内容修改（用于标签未保存标记与关闭确认）。"""
        return self._dirty

    def _setDirty(self, dirty: bool) -> None:
        """置未保存标记并广播（值不变时不重复发信号）。"""
        if self._dirty == dirty:
            return
        self._dirty = dirty
        self.dirtyChanged.emit(dirty)

    def markSaved(self) -> None:
        self._setDirty(False)

    def setDefaultLineWidth(self, w: float) -> None:
        self._defaultLineWidth = w

    def defaultLineWidth(self) -> float:
        return self._defaultLineWidth

    # ---------------- 活动空间（2D/浮雕 共用 2d；3D 模式独立 3d；CNC 独立 cnc） ----------------

    def space(self) -> str:
        """当前活动空间："2d"（2D/浮雕共用）、"3d"（3D 模式独立曲线）或 "cnc"（CNC 独立）。"""
        return self._space

    def setSpace(self, space: str) -> None:
        """切换活动空间；各空间的选中集一并清空，避免跨空间残留选中。"""
        if space not in (SPACE_2D, SPACE_3D, SPACE_CNC):
            return
        if space == self._space:
            return
        self.clearSelection()
        for o in self._objects3d.values():
            if o.isSelected():
                o.setSelected(False)
        for o in self._objectsCnc.values():
            if o.isSelected():
                o.setSelected(False)
        self._space = space

    def _activeStore(self) -> Dict[str, BaseObject]:
        """当前活动空间的对象表。"""
        if self._space == SPACE_3D:
            return self._objects3d
        if self._space == SPACE_CNC:
            return self._objectsCnc
        return self._objects

    def is3DObject(self, oid: str) -> bool:
        """对象是否属于 3D 空间（2D 场景据此跳过、不生成图元）。"""
        return oid in self._objects3d

    def isCncObject(self, oid: str) -> bool:
        """对象是否属于 CNC 空间（2D 场景据此跳过、不生成图元）。"""
        return oid in self._objectsCnc

    def addObject(self, obj: BaseObject, space: Optional[str] = None) -> None:
        """把对象加入指定空间（space=None 时加入当前活动空间）。"""
        if space == SPACE_3D:
            self._objects3d[obj.id()] = obj
        elif space == SPACE_CNC:
            self._objectsCnc[obj.id()] = obj
        else:
            self._activeStore()[obj.id()] = obj
        # 自动分层：先按类型落在对应固定层（“族”）；若“当前层”是该族的
        # 一个子图层，则优先落入该子图层，实现同族内的细分归档。
        # 3D 空间的对象落入当前活动空间的独立对象表（同样归“曲线”族）。
        root = _layerIdForObject(obj)
        lid = root
        act = self._layers.get(self._activeLayerId)
        if act is not None and (act.id == root or act.parentId == root):
            lid = act.id
        elif act is not None and root == LAYER_3D_MODEL_ID \
                and act.id == LAYER_CNC_ID:
            lid = act.id   # 3D 实体可落入“加工模具”固定层
        obj.setLayerId(lid)
        layer = self._layers.get(lid)
        if layer is not None:
            obj.setColor(layer.color)
            obj.setLayerLocked(layer.locked)   # 放入锁定层的新对象同样不可编辑
        obj.setLineWidth(self._defaultLineWidth)
        self.objectAdded.emit(obj.id())

    def removeObject(self, oid: str) -> Optional[BaseObject]:
        obj = self._objects.pop(oid, None)
        if obj is None:
            obj = self._objects3d.pop(oid, None)
        if obj is None:
            obj = self._objectsCnc.pop(oid, None)
        if obj is not None:
            self.objectRemoved.emit(oid)
        return obj

    def object(self, oid: str) -> Optional[BaseObject]:
        obj = self._objects.get(oid)
        if obj is None:
            obj = self._objects3d.get(oid)
        if obj is None:
            obj = self._objectsCnc.get(oid)
        return obj

    def objects(self) -> List[BaseObject]:
        """当前活动空间的对象列表（3D 模式只含 3D 曲线，CNC 只含 CNC 对象）。"""
        return list(self._activeStore().values())

    def allObjects(self) -> List[BaseObject]:
        """全部对象（跨空间）：导出、全局属性刷新等不区分空间的场景使用。"""
        return (list(self._objects.values()) + list(self._objects3d.values())
                + list(self._objectsCnc.values()))

    def isEmpty(self) -> bool:
        """文档内是否没有任何对象（用于“最后一个空文档不可关闭”等判定）。"""
        return not self._objects and not self._objects3d and not self._objectsCnc

    def visibleObjects(self) -> List[BaseObject]:
        out = []
        for obj in self._activeStore().values():
            layer = self._layers.get(obj.layerId())
            if obj.isVisible() and (layer is None or layer.visible):
                out.append(obj)
        return out

    def notifyObjectChanged(self, oid: str) -> None:
        # 绑定跟随：对象几何变化时同步刷新绑定它的 XY 标注（先发标注更新，
        # 后发对象自身变化，避免监听方先刷对象后刷标注造成的中间态闪烁）
        self._syncBoundDimensions(oid)
        self.objectChanged.emit(oid)

    def _syncBoundDimensions(self, changedOid: Optional[str] = None) -> None:
        """绑定标注跟随：把绑定到受影响对象的 XY/Z 标注被测区域同步为其
        现存绑定源对象的联合包围盒（保留字号/净空/子标注外拉距离）。

        changedOid 为 None 时全量同步（撤销/重做恢复后使用）。仅对仍绑定
        源对象的标注生效；源对象被删除后该标注不再跟随（保持最后区域），
        用户解绑后（目标列表清空）也自然不再参与。
        """
        touched = []
        for obj in list(self.allObjects()):
            if obj.type() != ObjectType.DIMENSION:
                continue
            if obj.kind() not in ("xy", "z"):
                continue
            if changedOid is not None and changedOid not in obj.boundTargetIds():
                continue
            srcs = [self.object(t) for t in obj.boundTargetIds()
                    if self.object(t) is not None]
            if not srcs:
                continue
            if obj.followTargetBounds(srcs):
                touched.append(obj.id())
        for oid in touched:
            self.objectChanged.emit(oid)

    def reliefModels(self) -> List[BaseObject]:
        """文档内全部浮雕模型对象（ObjectType.SURFACE）。"""
        return [o for o in self._objects.values() if o.type() == ObjectType.SURFACE]

    def activeReliefModel(self):
        """当前活动浮雕模型：浮雕编辑态（编辑锁）的 SURFACE 优先，否则首个选中的 SURFACE。

        注意只用编辑锁 isEditLocked：图层锁定（layerLocked）不算进入浮雕编辑。"""
        selected = None
        for o in self._objects.values():
            if o.type() != ObjectType.SURFACE:
                continue
            if o.isEditLocked():
                return o
            if selected is None and o.isSelected():
                selected = o
        return selected

    def selectedObjects(self) -> List[BaseObject]:
        return [o for o in self._activeStore().values() if o.isSelected()]

    def clearSelection(self) -> None:
        changed = False
        for o in self._activeStore().values():
            if o.isSelected():
                o.setSelected(False)
                changed = True
        if changed:
            self.selectionChanged.emit()

    def selectObject(self, oid: str, append: bool = False) -> None:
        if not append:
            self.clearSelection()
        obj = self._activeStore().get(oid)
        if obj is not None:
            obj.setSelected(True)
            self.selectionChanged.emit()

    def groupSelected(self) -> int:
        """把当前选中对象编为一组（共享同一组 id）；返回编组对象数。
        跳过锁定对象；已在其他组的对象归入新组（重新编组）。"""
        sel = [o for o in self._activeStore().values()
               if o.isSelected() and not o.isLocked()]
        if not sel:
            return 0
        gid = uuid.uuid4().hex
        for o in sel:
            o.setGroup(gid)
            self.objectChanged.emit(o.id())
        return len(sel)

    def ungroupSelected(self) -> int:
        """解除当前选中对象的组归属（清空组 id）；返回受影响对象数。"""
        sel = [o for o in self._activeStore().values()
               if o.isSelected() and o.group()]
        for o in sel:
            o.setGroup("")
            self.objectChanged.emit(o.id())
        return len(sel)

    def layers(self) -> List[Layer]:
        return [self._layers[i] for i in self._layerOrder if i in self._layers]

    def layer(self, lid: int) -> Optional[Layer]:
        return self._layers.get(lid)

    def layerObjectCount(self, lid: int, includeChildren: bool = True) -> int:
        """图层对象数量：含/不含子图层（当前活动空间）。"""
        ids = {lid}
        if includeChildren:
            ids.update(l.id for l in self._layers.values()
                       if l.parentId == lid)
        n = 0
        for o in self._activeStore().values():
            if o.layerId() not in ids:
                continue
            if not _countableObject(o):
                continue   # 空几何对象（如命令激活时的空线）不计入
            n += 1
        return n

    def childLayers(self, parentId: int) -> List[Layer]:
        """某固定层下的全部子图层（按 _layerOrder 顺序）。"""
        pid = parentId if _isFixedLayer(parentId) else -1
        return [l for l in self.layers()
                if l.parentId == pid] if pid >= 0 else []

    def layerRootId(self, lid: int) -> int:
        """图层所属固定族根 id：固定层返回自身，子图层返回其父固定层。"""
        l = self._layers.get(lid)
        if l is None:
            return LAYER_CURVES_ID
        if _isFixedLayer(l.id):
            return l.id
        return l.parentId if _isFixedLayer(l.parentId) else LAYER_CURVES_ID

    def addLayer(self, name: Optional[str] = None, parentId: int = None) -> int:
        """在固定层下新建一个子图层并追加到列表末尾；name 为空自动命名“子层N”。

        parentId 缺省时取“当前层”所属固定族：
        - 当前是固定层 → 新建在该层下；
        - 当前是子图层 → 新建为其兄弟（同父固定层）。
        parentId 非固定层时回退到“曲线”层。
        """
        if parentId is None:
            parentId = self.layerRootId(self._activeLayerId)
        if parentId is None or not _isFixedLayer(parentId):
            parentId = LAYER_CURVES_ID
        lid = self._nextLayerId
        self._nextLayerId += 1
        parent = self._layers[parentId]
        # 子图层默认继承父层颜色与显隐/锁定状态（同族视觉与可见性保持一致）
        self._layers[lid] = Layer(lid, name or f"子层{lid}",
                                  color=parent.color,
                                  visible=parent.visible,
                                  locked=parent.locked,
                                  parentId=parentId)
        self._layerOrder.append(lid)
        self.layerChanged.emit()
        return lid

    def removeLayer(self, lid: int) -> None:
        """删除子图层；固定层（曲线/模型/图片/标注）不可删除。

        被删子图层上的对象回归其父固定层。
        """
        layer = self._layers.get(lid)
        if layer is None or _isFixedLayer(lid):
            return
        parent = self._layers.get(layer.parentId)
        back = layer.parentId if parent is not None else LAYER_CURVES_ID
        self._layerOrder.remove(lid)
        del self._layers[lid]
        for obj in self.allObjects():
            if obj.layerId() == lid:
                obj.setLayerId(back)
                self.objectChanged.emit(obj.id())
        if self._activeLayerId == lid:
            self._activeLayerId = back
        self.layerChanged.emit()

    def reorderLayer(self, lid: int, delta: int) -> None:
        """在同一固定层下把子图层前/后移动一位；固定层不可移动。"""
        layer = self._layers.get(lid)
        if layer is None or _isFixedLayer(lid) or not _isFixedLayer(layer.parentId):
            return
        bros = [x for x in self._layerOrder
                if x in self._layers and self._layers[x].parentId == layer.parentId]
        i = bros.index(lid)
        j = i + delta
        if not (0 <= j < len(bros)):
            return
        self._layerOrder.remove(lid)
        ti = self._layerOrder.index(bros[j])
        self._layerOrder.insert(ti + (1 if delta > 0 else 0), lid)
        self.layerChanged.emit()

    def renameLayer(self, lid: int, name: str) -> None:
        """重命名额外图层；固定层名（曲线/模型）不可改。"""
        layer = self._layers.get(lid)
        if layer is None or _isFixedLayer(lid) or not name.strip():
            return
        layer.name = name.strip()
        self.layerChanged.emit()

    def setLayerActive(self, lid: int) -> None:
        """设置当前层（不广播信号；面板内点击等已计划刷新的场景使用）。"""
        if lid in self._layers:
            self._activeLayerId = lid

    def activeLayerId(self) -> int:
        return self._activeLayerId

    def setActiveLayerId(self, lid: int) -> None:
        self._activeLayerId = lid
        self.layerChanged.emit()

    def setLayerColor(self, lid: int, color: tuple) -> None:
        """改图层颜色：该图层上的既有对象同步更新（原项目同款行为）。"""
        layer = self._layers.get(lid)
        if layer is None:
            return
        layer.color = tuple(color)
        for obj in self.allObjects():
            if obj.layerId() == lid:
                obj.setColor(layer.color)
                self.objectChanged.emit(obj.id())
        self.layerChanged.emit()

    def setLayerFlag(self, lid: int, visible: Optional[bool] = None,
                      locked: Optional[bool] = None) -> None:
        layer = self._layers.get(lid)
        if layer is None:
            return
        # 固定根层显隐/锁定级联到其全部子图层（族内保持一致）；
        # 子图层自身的开关只影响该子层。
        affected = [lid]
        if _isFixedLayer(lid):
            affected += [c.id for c in self.childLayers(lid)]
        if visible is not None:
            for a in affected:
                self._layers[a].visible = visible
        if locked is not None:
            for a in affected:
                self._layers[a].locked = locked
        # 显隐/锁定落实到对象：
        # - 隐藏/锁定层：取消该层对象选中（避免隐藏后残留选中/操作轴，
        #   或锁定后残留无法操作的选中态）；
        # - 锁定层：对象整体挂“图层锁”（拾取/框选/操作轴按 obj.isLocked() 排除），
        #   解锁层即恢复可编辑；此锁独立于浮雕编辑锁（_locked），互不干扰。
        selChanged = False
        for obj in self.allObjects():
            if obj.layerId() not in affected:
                continue
            if (visible is not None and not visible and obj.isSelected()) \
                    or (locked is not None and locked and obj.isSelected()):
                obj.setSelected(False)
                selChanged = True
            if locked is not None:
                obj.setLayerLocked(locked)
        if selChanged:
            self.selectionChanged.emit()
        self.layerChanged.emit()

    def _syncLayerLocks(self) -> None:
        """按对象所在图层当前锁定状态同步“图层锁”（读档/撤销重建后调用）。

        对象新建时 addObject 已处理；此处覆盖 deserialize/重建后丢失的标记。
        """
        for obj in self.allObjects():
            layer = self._layers.get(obj.layerId())
            obj.setLayerLocked(bool(layer is not None and layer.locked))

    def _migrateLegacyLayerIds(self) -> None:
        """旧文件兼容：旧版额外图层 id 从 LAYER_MODEL_ID+1(=2) 起编号，与后来
        新增的固定层（2 图片 / 3 标注）冲突——直接读会把旧用户的“图层2”误认成
        固定图片层并将其对象挪走。判据：该 id 上存在类型与固定层不符的对象时，
        视为旧用户图层，整体重编号到固定层之后让位（兼容为曲线子图层）。"""
        legacyCheck = {LAYER_IMAGE_ID: ObjectType.IMAGE,
                       LAYER_ANNOTATION_ID: ObjectType.DIMENSION}
        for fixedId, ftype in legacyCheck.items():
            layer = self._layers.get(fixedId)
            if layer is None:
                continue
            mismatch = any(o.layerId() == fixedId and o.type() != ftype
                           for o in self.allObjects())
            if not mismatch:
                continue
            newId = max([LAYER_FIXED_COUNT - 1]
                        + [l.id for l in self._layers.values()]) + 1
            del self._layers[fixedId]
            self._layers[newId] = Layer(newId, layer.name,
                                        visible=layer.visible,
                                        locked=layer.locked, color=layer.color,
                                        parentId=LAYER_CURVES_ID)
            self._layerOrder[self._layerOrder.index(fixedId)] = newId
            for o in self.allObjects():
                if o.layerId() == fixedId:
                    o.setLayerId(newId)
                    self.objectChanged.emit(o.id())
            if self._activeLayerId == fixedId:
                self._activeLayerId = newId

    def _ensureFixedLayers(self) -> None:
        """读档 / 快照恢复后调用：保证固定层(0 曲线 / 1 浮雕模型 / 2 图片 / 3 标注)存在且置顶。

        - 固定层缺失时按默认名/颜色补齐；已存在则保留显隐/锁定/颜色状态，
          但名称统一为“曲线”“浮雕模型”“图片”“标注”（兼容旧文件第一层叫“默认”）；
        - 旧版本平级“额外图层”（未记录 parentId）兼容为“曲线”根下的子图层；
        - 固定层或子图层上残留类型不符的对象，按类型归位到对应固定层；
        - 引用不存在图层的对象同样按类型归入固定层；
        - 用户子图层及其对象原样保留，顺序接在固定层之后。
        """
        prev = {l.id: l for l in self._layers.values()}
        for lid, name, color in _FIXED_LAYERS:
            old = prev.get(lid)
            if old is None:
                self._layers[lid] = Layer(lid, name, color=color, parentId=-1)
            else:
                old.name = name
                old.parentId = -1
                self._layers[lid] = old
        # 子图层必须归属合法固定层；父级缺失/非法的按“曲线”处理
        for l in list(self._layers.values()):
            if _isFixedLayer(l.id):
                continue
            if not _isFixedLayer(l.parentId):
                l.parentId = LAYER_CURVES_ID
        fixed_ids = [lid for lid, _, _ in _FIXED_LAYERS]
        extras = [i for i in self._layerOrder
                  if i not in fixed_ids and i in self._layers]
        self._layerOrder = fixed_ids + extras
        # 对象归位：层不存在 → 按类型归位；固定层/子图层上类型与层不符 → 归对应固定层
        for obj in self.allObjects():
            cur = obj.layerId()
            l = self._layers.get(cur)
            if l is None:
                obj.setLayerId(_layerIdForObject(obj))
                self.objectChanged.emit(obj.id())
            elif _isFixedLayer(cur):
                if cur != _layerIdForObject(obj):
                    obj.setLayerId(_layerIdForObject(obj))
                    self.objectChanged.emit(obj.id())
            elif l.parentId != _layerIdForObject(obj):
                obj.setLayerId(_layerIdForObject(obj))
                self.objectChanged.emit(obj.id())
        # 图层编号递增到不与现有层冲突（始终 ≥ 固定层数量）
        self._nextLayerId = max(
            self._nextLayerId, LAYER_FIXED_COUNT,
            *[l.id + 1 for l in self._layers.values()])
        if self._activeLayerId not in self._layers:
            self._activeLayerId = LAYER_CURVES_ID

    # ---------------- 撤销 / 重做（曲线、浮雕两套相互独立） ----------------

    @staticmethod
    def _domainOf(obj) -> str:
        """对象所属历史域：浮雕模型(SURFACE) → relief；其余（曲线/点/文字…）→ curve。"""
        return "relief" if obj.type() == ObjectType.SURFACE else "curve"

    def _snapshotDomain(self, kind: str) -> dict:
        """按域快照：只序列化该域的对象，并记录该域内选中集。

        曲线/浮雕两套历史各存各的快照：撤销某一套时只重建对应域的对象，
        另一域对象与其历史互不干扰，保证“不能混合、相互独立”。
        走对象级 snapshot() 协议（默认=serialize；浮雕网格/位图为免压缩的
        原始字节快照），每项同时记录对象级内存计费供预算淘汰。
        curve3d 域取 3D 空间对象表（全部为曲线类对象）。
        """
        src = (self._objects3d.values() if kind == _DOMAIN_CURVE3D
               else self._objects.values())
        objs = sorted((o for o in src
                       if kind == _DOMAIN_CURVE3D or self._domainOf(o) == kind),
                      key=lambda o: o.id())
        entries = []
        for o in objs:
            snap = o.snapshot()
            entries.append({"id": o.id(), "snap": snap,
                            "size": o.snapshotSize(snap)})
        return {
            "objects": entries,
            "selected": [o.id() for o in objs if o.isSelected()],
        }

    def _nextSeq(self) -> int:
        self._historySeq += 1
        return self._historySeq

    def beginEdit(self) -> None:
        """开始一次可撤销的用户操作（拖动/命令等）。可嵌套，只记录首次快照。"""
        if self._editBefore is None:
            self._editBefore = {k: self._snapshotDomain(k) for k in _HISTORY_DOMAINS}

    def endEdit(self) -> None:
        """结束操作：按"哪个域的对象发生变化"压入对应域的撤销栈。

        - 改动浮雕模型(SURFACE) → 浮雕(relief)历史；仅动曲线/点/文字等 → 曲线(curve)历史；
        - 一个会话若同时改了曲线与模型，两套历史各记一项；
        - 快照无变化则忽略（不产生空撤销项）。
        - 压栈只清空本域的"重做"，另一域的重做/撤销不受影响。
        - 压栈后按内存预算（设置-渲染性能-撤销历史内存预算）从最旧开始淘汰。
        """
        if self._editBefore is None:
            return
        before = self._editBefore
        changedKinds = []
        for kind in _HISTORY_DOMAINS:
            # O(1) 预检：先用对象级 matchesSnapshot 签名判断该域是否有变，
            # 无变即跳过 after 全量物化——浮雕场 snapshot 是 64MB 级
            # memcpy + 字典全等 memcmp，未涉及该域的编辑（画线/改曲线）
            # 不再为它白付一次
            if not self._domainChanged(kind, before[kind]):
                continue
            after = self._snapshotDomain(kind)
            if after != before[kind]:
                size = (self._historyEntrySize(before[kind]) +
                        self._historyEntrySize(after))
                self._undoHistory[kind].append(
                    (before[kind], after, self._nextSeq(), size))
                if len(self._undoHistory[kind]) > self._undoDepth:
                    self._undoHistory[kind].pop(0)
                self._redoHistory[kind].clear()
                changedKinds.append(kind)
        self._editBefore = None
        if changedKinds:
            for kind in changedKinds:
                self._enforceHistoryBudget(kind)
            self._setDirty(True)   # 多文件系统：内容有变即标记未保存
            self._emitHistoryState()

    def _domainChanged(self, kind: str, beforeSnap: dict) -> bool:
        """该域自 beforeSnap 之后是否有任何变化（对象集/内容/选中集）。

        走 matchesSnapshot 协议：浮雕模型是 stamp/version O(1) 签名比较，
        曲线类为 serialize 全等（与原来 endEdit 的二次快照+字典比较等价）。
        """
        src = (self._objects3d.values() if kind == _DOMAIN_CURVE3D
               else self._objects.values())
        objs = [o for o in src
                if kind == _DOMAIN_CURVE3D or self._domainOf(o) == kind]
        beforeById = {e["id"]: e for e in beforeSnap.get("objects", [])}
        if len(objs) != len(beforeById):
            return True
        for o in objs:
            entry = beforeById.get(o.id())
            if entry is None:
                return True
            try:
                same = o.matchesSnapshot(entry["snap"])
            except Exception:
                same = False
            if not same:
                return True
        cur_sel = {o.id() for o in objs if o.isSelected()}
        return cur_sel != set(beforeSnap.get("selected", []))

    @staticmethod
    def _historyEntrySize(snap: dict) -> int:
        """撤销项内存计费：累加快照内各对象的 snapshotSize（大对象按原始字节）。"""
        total = 512
        for e in snap.get("objects", []):
            total += int(e.get("size", 0))
        return total

    def _enforceHistoryBudget(self, kind: str) -> None:
        """按“撤销历史内存预算”从最旧开始淘汰浮雕/位图大快照（至少保留一项）。"""
        try:
            from . import app_settings
            budget = app_settings.undo_memory_budget_bytes()
        except Exception:
            return
        st = self._undoHistory[kind]
        total = sum(e[3] for e in st)
        while total > budget and len(st) > 1:
            _, _, _, sz = st.pop(0)
            total -= sz

    def _restoreDomain(self, kind: str, snap: dict) -> None:
        """把 kind 域的对象恢复为快照状态；另一域对象与图层等文档状态保持不变。"""
        store = self._objects3d if kind == _DOMAIN_CURVE3D else self._objects
        target = {e["id"]: e for e in snap["objects"]}
        snap_sel = set(snap.get("selected", []))
        # 1) 移除快照中已不存在的该域对象
        for oid in [oid for oid, o in store.items()
                    if kind == _DOMAIN_CURVE3D or self._domainOf(o) == kind]:
            if oid not in target:
                store.pop(oid)
                self.objectRemoved.emit(oid)
        # 2) 与快照有差异的对象按快照重建（同 id 先移除再添加，驱动场景/视口刷新）；
        #    matchesSnapshot 未变（浮雕按 stamp/version O(1) 签名、其余按快照全等）
        #    的对象保留原实例，避免无谓重建（尤其浮雕模型网格缓存）。
        for oid, entry in target.items():
            cur = store.get(oid)
            if cur is not None:
                try:
                    same = cur.matchesSnapshot(entry["snap"])
                except Exception:
                    same = False
                if same:
                    continue
                store.pop(oid)
                self.objectRemoved.emit(oid)
            obj = self._objectFromSnapshot(entry["snap"])
            if obj is None:
                continue
            store[oid] = obj
            self.objectAdded.emit(oid)
        # 3) 该域选择集与快照对齐；其余域选择保持不变
        changed = False
        for oid, obj in list(store.items()):
            if kind != _DOMAIN_CURVE3D and self._domainOf(obj) != kind:
                continue
            want = oid in snap_sel
            if obj.isSelected() != want:
                obj.setSelected(want)
                changed = True
        if changed:
            self.selectionChanged.emit()
        # 重建对象会丢失图层锁标记（未参与序列化），按所在层当前状态补回
        self._syncLayerLocks()
        # 撤销/重做重建后对齐绑定标注：源对象几何回到快照状态，标注跟随刷新
        self._syncBoundDimensions()

    def _objectFromSnapshot(self, snap) -> Optional[BaseObject]:
        """由撤销快照重建对象：快照可能是对象实例（BRepObject 内存克隆）
        或序列化 dict。未知类型返回 None。"""
        if isinstance(snap, BaseObject):
            # 内存克隆快照：恢复时再克隆一份——历史栈仍持有原快照实例，
            # 直接把同一实例放回 store 的话，后续任何就地修改会静默改写
            # 历史，且再次恢复时 matchesSnapshot 自比较恒真、恢复变 no-op。
            # BrepShape/BrepObject.clone 共享不可变 part 与派生缓存，代价极低。
            try:
                return snap.clone()
            except Exception:
                return snap
        factory = _TYPE_FACTORY.get(snap.get("type"))
        if factory is None:
            return None
        obj = factory()
        obj.restoreSnapshot(snap)
        return obj

    def _undoOne(self, kind: str) -> None:
        st = self._undoHistory[kind]
        if not st:
            return
        before, after, _, _sz = st.pop()
        self._redoHistory[kind].append((before, after, self._nextSeq(), _sz))
        self._restoreDomain(kind, before)
        self._emitHistoryState()

    def _redoOne(self, kind: str) -> None:
        st = self._redoHistory[kind]
        if not st:
            return
        before, after, _, sz = st.pop()
        self._undoHistory[kind].append((before, after, self._nextSeq(), sz))
        if len(self._undoHistory[kind]) > self._undoDepth:
            self._undoHistory[kind].pop(0)
        # 反复撤销→重做也会往 undo 栈压大浮雕条目，同样受预算淘汰
        # （原来只有 endEdit 压栈时淘汰，重做路径可超预算）
        self._enforceHistoryBudget(kind)
        self._restoreDomain(kind, after)
        self._emitHistoryState()

    def undoCurve(self) -> None:
        """撤销曲线历史：只恢复曲线/点/文字等对象（含 3D 空间曲线），
        浮雕模型与其历史不受影响。"""
        kind = self._newestKind({"curve": self._undoHistory["curve"],
                                 _DOMAIN_CURVE3D: self._undoHistory[_DOMAIN_CURVE3D]})
        if kind is not None:
            self._undoOne(kind)

    def redoCurve(self) -> None:
        kind = self._newestKind({"curve": self._redoHistory["curve"],
                                 _DOMAIN_CURVE3D: self._redoHistory[_DOMAIN_CURVE3D]})
        if kind is not None:
            self._redoOne(kind)

    def undoRelief(self) -> None:
        """撤销浮雕历史：只恢复浮雕模型(SURFACE)对象，曲线与其历史不受影响。"""
        self._undoOne("relief")

    def redoRelief(self) -> None:
        self._redoOne("relief")

    @staticmethod
    def _newestKind(history: Dict[str, list]) -> Optional[str]:
        """返回记录序号最新的域：跨两套历史定位“最近一次操作”。"""
        best_kind, best_seq = None, -1
        for kind, st in history.items():
            if st and st[-1][2] > best_seq:
                best_kind, best_seq = kind, st[-1][2]
        return best_kind

    def undo(self) -> None:
        """通用撤销：撤销最近一次操作（曲线/浮雕中较新的一件）。"""
        kind = self._newestKind(self._undoHistory)
        if kind is not None:
            self._undoOne(kind)

    def redo(self) -> None:
        """通用重做：重做最近一次被撤销的操作。"""
        kind = self._newestKind(self._redoHistory)
        if kind is not None:
            self._redoOne(kind)

    def canCurveUndo(self) -> bool:
        return bool(self._undoHistory["curve"]) or \
            bool(self._undoHistory[_DOMAIN_CURVE3D])

    def canCurveRedo(self) -> bool:
        return bool(self._redoHistory["curve"]) or \
            bool(self._redoHistory[_DOMAIN_CURVE3D])

    def canReliefUndo(self) -> bool:
        return bool(self._undoHistory["relief"])

    def canReliefRedo(self) -> bool:
        return bool(self._redoHistory["relief"])

    def canUndo(self) -> bool:
        return self.canCurveUndo() or self.canReliefUndo()

    def canRedo(self) -> bool:
        return self.canCurveRedo() or self.canReliefRedo()

    def _emitHistoryState(self) -> None:
        """广播曲线/浮雕两套历史状态（另保留旧的全量信号供兼容）。"""
        self.curveUndoChanged.emit(self.canCurveUndo())
        self.curveRedoChanged.emit(self.canCurveRedo())
        self.reliefUndoChanged.emit(self.canReliefUndo())
        self.reliefRedoChanged.emit(self.canReliefRedo())
        self.canUndoChanged.emit(self.canUndo())
        self.canRedoChanged.emit(self.canRedo())

    def clearEditHistory(self) -> None:
        self._undoHistory = {k: [] for k in _HISTORY_DOMAINS}
        self._redoHistory = {k: [] for k in _HISTORY_DOMAINS}
        self._editBefore = None
        self._emitHistoryState()

    def clear(self) -> None:
        ids = (list(self._objects.keys()) + list(self._objects3d.keys())
               + list(self._objectsCnc.keys()))
        self._objects.clear()
        self._objects3d.clear()
        self._objectsCnc.clear()
        for oid in ids:
            self.objectRemoved.emit(oid)

    def save(self, path: str) -> None:
        """将文档序列化为 JSON 文件（含图层与全部对象，2D/3D/CNC 三空间
        + CNC 模块完整状态：机床/毛坯/夹具/刀具表/路径条目）。

        2D 空间：曲线、图片（像素内嵌）、浮雕模型（高度场+蒙版+幕帘
        压缩内嵌）；3D 空间：B-Rep 实体/网格模型；CNC 空间：CNC 画布
        对象 + 全部路径条目（参数、picked 引用、G 代码、刀路 IR）。
        单个对象序列化失败（记录日志）不影响其余内容落盘。
        """
        def _safe(o):
            try:
                return o.serialize()
            except Exception:
                import traceback
                traceback.print_exc()
                return None
        data = {
            "activeLayerId": self._activeLayerId,
            "layers": [{"id": l.id, "name": l.name, "visible": l.visible,
                        "locked": l.locked, "color": list(l.color),
                        "parentId": l.parentId}
                       for l in self.layers()],
            "objects": [s for o in self._objects.values()
                        if (s := _safe(o)) is not None],
            "objects3d": [s for o in self._objects3d.values()
                          if (s := _safe(o)) is not None],
            # CNC 空间：跳过临时对象（毛坯等运行时生成物，不落盘）
            "objectsCnc": [s for o in self._objectsCnc.values()
                           if not getattr(o, "_cncTransient", False)
                           and (s := _safe(o)) is not None],
        }
        # CNC 模块完整状态（路径/刀具/机床/毛坯/夹具）；导入失败时降级为空
        try:
            from core.cnc.state import CncState
            data["cncState"] = CncState.instance().toSaveDict()
        except Exception:
            data["cncState"] = {}
        # 原子写：先写同目录临时文件再 os.replace——磁盘满/序列化中途
        # 失败时不至于留下截断损坏的主文件（旧版本完整可回退）。
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        self._setDirty(False)

    @staticmethod
    def _safeObjectFromDict(data: dict):
        """单对象反序列化：失败（记录日志）返回 None，不阻塞整体加载。"""
        try:
            return objectFromDict(data)
        except Exception:
            import traceback
            traceback.print_exc()
            return None

    def load(self, path: str) -> None:
        """从 JSON 文件加载文档，替换当前全部内容。"""
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.clear()
        self.clearEditHistory()
        self._layers.clear()
        self._layerOrder.clear()
        self._nextLayerId = 1
        for ld in data.get("layers", []):
            layer = Layer(int(ld.get("id", 0)), ld.get("name", "图层"))
            layer.visible = bool(ld.get("visible", True))
            layer.locked = bool(ld.get("locked", False))
            c = ld.get("color")
            if c:
                layer.color = (int(c[0]), int(c[1]), int(c[2]))
            # 旧文件无 parentId：固定层视为根层；其余平级层兼容为“曲线”子图层
            pid = ld.get("parentId")
            layer.parentId = int(pid) if pid is not None else (-1
                                                               if _isFixedLayer(layer.id)
                                                               else LAYER_CURVES_ID)
            self._layers[layer.id] = layer
            self._layerOrder.append(layer.id)
            self._nextLayerId = max(self._nextLayerId, layer.id + 1)
        # 旧文件可能缺新增的固定层（如 3D模型层）：补建缺失的固定层
        for lid, name, color in _FIXED_LAYERS:
            if lid not in self._layers:
                self._layers[lid] = Layer(lid, name, color=color)
                self._layerOrder.append(lid)
        if not self._layers:
            self._layers[0] = Layer(0, "默认")
            self._layerOrder = [0]
            self._nextLayerId = LAYER_FIXED_COUNT
        self._activeLayerId = int(data.get("activeLayerId", 0))
        for od in data.get("objects", []):
            obj = self._safeObjectFromDict(od)
            if obj is not None:
                self._objects[obj.id()] = obj
                self.objectAdded.emit(obj.id())
        # 3D 空间独立曲线（旧文件无此键 = 空）
        for od in data.get("objects3d", []):
            obj = self._safeObjectFromDict(od)
            if obj is not None:
                self._objects3d[obj.id()] = obj
                self.objectAdded.emit(obj.id())
        # CNC 空间独立对象（旧文件无此键 = 空；毛坯等临时对象不落盘）
        for od in data.get("objectsCnc", []):
            obj = self._safeObjectFromDict(od)
            if obj is not None:
                self._objectsCnc[obj.id()] = obj
                self.objectAdded.emit(obj.id())
        # CNC 模块完整状态（机床/毛坯/夹具/刀具表/路径条目）：对象已在上方
        # 全部恢复，这里做 picked 对象 id → 引用解析；旧文件无此键 = 跳过
        cd = data.get("cncState")
        if cd:
            try:
                from core.cnc.state import CncState
                CncState.instance().restoreFromDict(cd, self)
            except Exception:
                import traceback
                traceback.print_exc()
        # 旧文件额外图层 id 与新增固定层冲突时先重编号让位
        self._migrateLegacyLayerIds()
        # 读档保证四个固定层存在并置顶，子图层原样保留
        self._ensureFixedLayers()
        self._syncLayerLocks()
        self.layerChanged.emit()
        self._setDirty(False)
"""几何对象抽象基类：定义对象类型、变换、包围盒、捕捉支持等接口。"""
from __future__ import annotations
import uuid
from abc import ABC, abstractmethod
from typing import List

from .geometry_types import Point3D, BoundingBox3D, ObjectType, polylineLength


class BaseObject(ABC):
    # 选择顺序戳（模块级递增计数）：后选的对象序号更大，供"后选为基准"类命令使用
    _SELECTION_COUNTER = [0]

    def __init__(self, objType: ObjectType) -> None:
        self._id: str = uuid.uuid4().hex
        self._type: ObjectType = objType
        self._layerId: int = 0
        self._visible: bool = True
        self._locked: bool = False          # 编辑锁：浮雕编辑目标等（随操作置位/复位）
        self._layerLocked: bool = False    # 所在图层被锁定（由图层面板驱动，不随编辑态变化）
        self._selected: bool = False
        self._selectionSeq: int = 0         # 选中时的顺序戳（0=未选中）
        self._dirty: bool = True
        self._translationOffset: Point3D = Point3D()
        self._color = (255, 100, 0)
        self._lineWidth: float = 1.0
        self._group: str = ""   # 组 id：非空表示属于某组（编组成员共享同一 id）

    def id(self) -> str:
        return self._id

    def type(self) -> ObjectType:
        return self._type

    def layerId(self) -> int:
        return self._layerId

    def setLayerId(self, lid: int) -> None:
        self._layerId = lid

    def isVisible(self) -> bool:
        return self._visible

    def setVisible(self, v: bool) -> None:
        self._visible = v

    def isLocked(self) -> bool:
        """对象是否不可编辑：编辑锁（浮雕编辑目标等）或所在图层被锁定
        都会使对象被拾取、框选、操作轴、捕捉排除。"""
        return self._locked or self._layerLocked

    def isEditLocked(self) -> bool:
        """是否处于编辑锁（如浮雕编辑目标）；图层锁定不算编辑锁。"""
        return self._locked

    def setLocked(self, locked: bool) -> None:
        """设置编辑锁（浮雕编辑目标等）；不影响图层锁状态。"""
        self._locked = bool(locked)

    def setLayerLocked(self, locked: bool) -> None:
        """跟随所在图层锁定状态（由图层面板/文档同步）。"""
        self._layerLocked = bool(locked)

    def isSelected(self) -> bool:
        return self._selected

    def selectionSeq(self) -> int:
        """选中顺序戳（后选的更大；0=未选中）。"""
        return self._selectionSeq

    def setSelected(self, s: bool) -> None:
        self._selected = s
        if s:
            BaseObject._SELECTION_COUNTER[0] += 1
            self._selectionSeq = BaseObject._SELECTION_COUNTER[0]
        else:
            self._selectionSeq = 0

    def color(self):
        return self._color

    def setColor(self, c) -> None:
        self._color = c

    def lineWidth(self) -> float:
        return self._lineWidth

    def setLineWidth(self, w: float) -> None:
        self._lineWidth = w

    def group(self) -> str:
        """所属组 id；空串表示未编组。"""
        return self._group

    def setGroup(self, gid: str) -> None:
        self._group = gid or ""

    @abstractmethod
    def translate(self, dx: float, dy: float, dz: float) -> None: ...

    @abstractmethod
    def rotate(self, angle: float, ax: float, ay: float, az: float) -> None: ...

    @abstractmethod
    def scale(self, sx: float, sy: float, sz: float) -> None: ...

    @abstractmethod
    def getBoundingBox(self) -> BoundingBox3D: ...

    @abstractmethod
    def getPosition(self) -> Point3D: ...

    @abstractmethod
    def clone(self) -> "BaseObject": ...

    def scaleAround(self, sx: float, sy: float, sz: float, center: Point3D) -> None:
        self.translate(-center.x, -center.y, -center.z)
        self.scale(sx, sy, sz)
        self.translate(center.x, center.y, center.z)

    def rotateAround(self, angle: float, center: Point3D) -> None:
        """绕指定点绕 Z 轴旋转 angle 弧度（不改变该点位置基准）。"""
        self.translate(-center.x, -center.y, -center.z)
        self.rotate(angle, 0, 0, 1)
        self.translate(center.x, center.y, center.z)

    def pathLength(self) -> float:
        """曲线实际长度。默认按采样折线累加，子类可覆盖为精确值。"""
        pts = self.getSampledPoints() if hasattr(self, "getSampledPoints") else []
        return polylineLength(pts)

    def enclosedArea(self) -> float | None:
        """闭合曲线包围的面积（XY 平面）；不闭合返回 None。"""
        return None

    def getSupportedSnapTypes(self) -> List:
        return []

    def controlPointCount(self) -> int:
        return 0

    def getControlPoint(self, i: int) -> Point3D:
        return Point3D()

    def setControlPoint(self, i: int, p: Point3D) -> None:
        pass

    def toPolycurve(self):
        """破坏性编辑：把参数化对象转成可自由编辑的 PolyCurveObject。
        返回 None 表示该对象本身已是自由曲线、无需转换（或不可转换）。"""
        return None

    def serialize(self) -> dict:
        return {"id": self._id, "type": self._type.name, "layerId": self._layerId,
                "color": list(self._color), "lineWidth": self._lineWidth,
                "group": self._group}

    def deserialize(self, d: dict) -> None:
        self._id = d.get("id", self._id)
        self._layerId = d.get("layerId", 0)
        self._color = tuple(d.get("color", (255, 100, 0)))
        self._lineWidth = d.get("lineWidth", 1.0)
        self._group = d.get("group", "")

    # ---------------- 撤销快照协议 ----------------
    # 默认实现 = serialize/deserialize；持有大数据的对象（浮雕网格/位图）
    # 应成对覆写 snapshot/restoreSnapshot（免压缩/免编码）与 matchesSnapshot
    # （O(1) 签名比较）与 snapshotSize（按原始字节计内存）。

    def snapshot(self) -> dict:
        """撤销快照：捕获对象全部可撤销内容（默认=完整 serialize）。"""
        return self.serialize()

    def restoreSnapshot(self, snap: dict) -> None:
        """由 snapshot() 产生的快照恢复内容（须与 snapshot 成对覆写）。"""
        self.deserialize(snap)

    def matchesSnapshot(self, snap: dict) -> bool:
        """当前内容是否与快照一致（撤销恢复时跳过未变对象，避免无谓重建）。
        默认重新 serialize 做全等比较；大对象覆写为廉价签名比较。"""
        return self.serialize() == snap

    def snapshotSize(self, snap: dict) -> int:
        """快照近似内存占用（字节）：撤销历史按内存预算淘汰旧项时计费。
        普通曲线/点/文字对象按 0 计（由撤销深度上限兜底）；
        大对象（浮雕网格/位图）覆写为按原始字节数计。"""
        return 0
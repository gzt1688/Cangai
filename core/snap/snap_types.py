"""捕捉类型枚举与捕捉结果数据结构。"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SnapType(Enum):
    NONE = 0
    ENDPOINT = 1
    NEAREST = 2
    POINT = 3
    MIDPOINT = 4
    CENTER = 5
    INTERSECTION = 6
    PERPENDICULAR = 7
    TANGENT = 8
    QUARTER = 9
    VERTEX = 10
    GRID = 11
    ORTHO = 12


_SNAP_LABELS = {
    SnapType.ENDPOINT: "端点",
    SnapType.NEAREST: "最近点",
    SnapType.POINT: "节点",
    SnapType.MIDPOINT: "中点",
    SnapType.CENTER: "圆心",
    SnapType.INTERSECTION: "交点",
    SnapType.PERPENDICULAR: "垂足",
    SnapType.TANGENT: "切点",
    SnapType.QUARTER: "象限点",
    SnapType.VERTEX: "顶点",
    SnapType.GRID: "网格",
}


def snapLabel(t: SnapType) -> str:
    return _SNAP_LABELS.get(t, "")


@dataclass
class SnapResult:
    position: Optional[object] = None
    type: SnapType = SnapType.NONE
    objectId: str = ""
    distance: float = 1e18
    isValid: bool = False
    markerPos: Optional[object] = None

    @property
    def effectiveMarker(self) -> object:
        return self.markerPos if self.markerPos is not None else self.position

    @staticmethod
    def invalid() -> "SnapResult":
        return SnapResult()
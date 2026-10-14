"""网格与正交约束：GridManager 单例提供网格配置与正交开关。"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum


class GridType(Enum):
    RECTANGULAR = 0
    POLAR = 1
    ISOMETRIC = 2


@dataclass
class GridConfig:
    type: GridType = GridType.RECTANGULAR
    spacing: float = 10.0
    subdivisions: int = 5
    majorColor = (180, 180, 180)
    minorColor = (220, 220, 220)
    visible: bool = True
    snapEnabled: bool = False


class GridManager:
    _instance = None

    def __init__(self) -> None:
        self._config = GridConfig()
        self._orthoEnabled = False

    @classmethod
    def instance(cls) -> "GridManager":
        if cls._instance is None:
            cls._instance = GridManager()
        return cls._instance

    def config(self) -> GridConfig:
        return self._config

    def setConfig(self, c: GridConfig) -> None:
        self._config = c

    def spacing(self) -> float:
        return self._config.spacing

    def minorSpacing(self) -> float:
        return self._config.spacing / max(1, self._config.subdivisions)

    def snapToGrid(self, p) -> "tuple[float,float]":
        if not self._config.snapEnabled:
            return (float(p[0]), float(p[1]))   # 关闭吸附：原样返回
        s = self.minorSpacing()
        return (round(p[0] / s) * s, round(p[1] / s) * s)

    def isOnGrid(self, p) -> bool:
        s = self.minorSpacing()
        return abs(p[0] - round(p[0] / s) * s) < 1e-9 and abs(p[1] - round(p[1] / s) * s) < 1e-9

    def setOrthoEnabled(self, e: bool) -> None:
        self._orthoEnabled = e

    def isOrthoEnabled(self) -> bool:
        return self._orthoEnabled

    def applyOrthoConstraint(self, start, end):
        if not self._orthoEnabled:
            return end
        dx, dy = end[0] - start[0], end[1] - start[1]
        if abs(dx) > abs(dy):
            return (end[0], start[1])
        return (start[0], end[1])
"""当前填色颜色（共享状态单例）。

底部捕捉工具栏（显示/点选调色板）与区域浮雕命令（空格填色、颜色内/外作用区域）
共用同一份"当前颜色"真相源，避免各持一份本地状态导致不同步。

颜色值约定与 core/relief/paint_palette.py 一致：
- 调色板索引 0..count()-1；
- 蒙版值 ``currentValue() = index + 1``（0 恒为"未填色"EMPTY）。
"""
from __future__ import annotations

from typing import Optional, Tuple

from PySide6.QtCore import QObject, Signal

from core.relief import paint_palette


class PaintState(QObject):
    """当前颜色单例（QObject）：调色板 + 当前索引，切换时发 currentChanged。

    额外维护一个“基础色”（浮雕模型/图层颜色）：与调色板第 2 个色块直接
    关联，图层改色时经 setBaseColor 同步并发 baseColorChanged 供底部色栏重绘。
    """

    currentChanged = Signal(int)          # 载荷为新当前索引（0 基）
    baseColorChanged = Signal()           # 基础色（第 2 色）变化

    _instance: Optional["PaintState"] = None

    def __init__(self) -> None:
        super().__init__()
        self._index: int = 0

    @classmethod
    def instance(cls) -> "PaintState":
        if cls._instance is None:
            cls._instance = PaintState()
        return cls._instance

    # ------------------------------------------------------------------
    # 基础色（浮雕模型/图层颜色 ↔ 调色板第 2 色）
    # ------------------------------------------------------------------
    @staticmethod
    def baseColor() -> Tuple[int, int, int]:
        return paint_palette.base_color()

    def setBaseColor(self, rgb) -> None:
        """把浮雕模型颜色同步为调色板第 2 色；变化则发 baseColorChanged。

        若当前选中的正是基础色槽，一并重发 currentChanged 刷新“当前颜色”块。
        """
        if paint_palette.set_base_color(rgb):
            self.baseColorChanged.emit()
            if self._index == paint_palette.BASE_INDEX:
                self.currentChanged.emit(self._index)

    # ------------------------------------------------------------------
    # 调色板（转发自 core，保证 core/UI 用同一张表）
    # ------------------------------------------------------------------
    @staticmethod
    def palette() -> Tuple[Tuple[int, int, int], ...]:
        """调色板 (r, g, b) 元组序列。"""
        return paint_palette.PALETTE

    @staticmethod
    def count() -> int:
        return paint_palette.count()

    @staticmethod
    def hexAt(index: int) -> str:
        return paint_palette.hex_at(index)

    # ------------------------------------------------------------------
    # 当前颜色
    # ------------------------------------------------------------------
    def currentIndex(self) -> int:
        return self._index

    def setCurrentIndex(self, index: int) -> None:
        """设为第 index 个颜色（越界回绕）；变化则发 currentChanged。"""
        n = paint_palette.count()
        if n <= 0:
            return
        i = int(index) % n
        if i == self._index:
            return
        self._index = i
        self.currentChanged.emit(self._index)

    def cycle(self) -> None:
        """循环切换到下一个颜色（W 键）。"""
        n = paint_palette.count()
        if n <= 0:
            return
        self.setCurrentIndex((self._index + 1) % n)

    def currentRgb(self) -> Tuple[int, int, int]:
        return paint_palette.rgb_at(self._index)

    def currentHex(self) -> str:
        return paint_palette.hex_at(self._index)

    def currentValue(self) -> int:
        """当前颜色对应的蒙版值（索引 + 1）。"""
        return paint_palette.index_to_value(self._index)

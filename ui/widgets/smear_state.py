"""Ctrl+左键涂抹的堆料/去料模式（共享状态单例）。

底部捕捉工具栏的"涂抹"模块（堆料/去料二选一）与 3D 视口的浮雕模式全局
交互（Ctrl+左键在模型上拖动涂抹）共用同一份真相源，避免各自持有本地
状态导致不同步。与磨光命令无关：磨光进行中也可随时 Ctrl+涂抹快速
堆料/去料。

笔刷参数用固定预设（小直径高斯，快速修补用）；方向由模式决定：
- add    堆料：涂抹处升高；
- remove 去料：涂抹处雕刻去除。
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, Signal

# 模式取值与中文标签
MODES = ("add", "remove")
MODE_LABELS = ("堆料", "去料")

# Ctrl+涂抹笔刷固定预设（直径/半衰直径 mm；单章高度 mm，方向按模式取正负）
SMEAR_DIAMETER = 3.0
SMEAR_HALF_LIFE = 1.5
SMEAR_HEIGHT = 1.0


class SmearState(QObject):
    """涂抹模式单例（QObject）：add=堆料 / remove=去料，切换时发 modeChanged。"""

    modeChanged = Signal(str)     # 载荷为新模式（"add"/"remove"）

    _instance: Optional["SmearState"] = None

    def __init__(self) -> None:
        super().__init__()
        self._mode: str = "add"

    @classmethod
    def instance(cls) -> "SmearState":
        if cls._instance is None:
            cls._instance = SmearState()
        return cls._instance

    def mode(self) -> str:
        return self._mode

    def isRemove(self) -> bool:
        """当前是否去料（False = 堆料）。"""
        return self._mode == "remove"

    def label(self) -> str:
        """当前模式中文标签（堆料/去料）。"""
        return MODE_LABELS[MODES.index(self._mode)] \
            if self._mode in MODES else MODE_LABELS[0]

    def setMode(self, mode: str) -> None:
        """设为 add/remove（非法值忽略）；变化则发 modeChanged。"""
        if mode not in MODES or mode == self._mode:
            return
        self._mode = mode
        self.modeChanged.emit(self._mode)

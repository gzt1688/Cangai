"""尺寸标注命令：对齐标注 = 用户点两个点，生成两点间距的对齐尺寸标注。

交互约定与其它取点命令一致：
- 第一点后随鼠标实时预览（尺寸线 + 延伸线 + 箭头轮廓 + 光标点）；
- 左键第二点生成 DimensionObject(kind="linear") 并加入文档（入撤销历史）；
- 右键/回车取消整次命令。
"""
from __future__ import annotations
import math

from PySide6.QtGui import QPainterPath
from PySide6.QtCore import QPointF

from core.vector.geometry_types import Point3D
from core.vector.dimension_object import DimensionObject
from core import app_settings
from ..command_instance import CommandInstance

# 读数文字与对象轮廓净空系数（相对字高）
TEXT_TO_OBJ_GAP = 0.9


class AlignDimCommand(CommandInstance):
    def __init__(self) -> None:
        super().__init__()
        self._p1: Point3D | None = None

    def name(self) -> str:
        return "AlignDim"

    def commandTitle(self) -> str:
        return "对齐标注"

    def prompt(self) -> str:
        if self._p1 is None:
            return "对齐标注：指定第一个点"
        return "对齐标注：指定第二个点（右键取消）"

    # ---------------- 样式 ----------------

    def _style(self) -> tuple:
        scale = 1.0
        if self._view is not None:
            scale = max(self._view.zoomScale(), 1e-9)
        # 默认读数文字屏幕高度取“设置 → 通用 → 标注”的配置
        fs = app_settings.dim_text_px / scale
        return (fs, fs * TEXT_TO_OBJ_GAP)

    # ---------------- 预览 ----------------

    def _previewPath(self, p2: Point3D) -> QPainterPath:
        """第一点已定：按临时 linear 标注的布局画尺寸线/延伸线/箭头轮廓。"""
        p1 = self._p1
        if p1 is None:
            return QPainterPath()
        fs, gap = self._style()
        tmp = DimensionObject()
        tmp.setLinear(p1, p2, fs, gap)
        lay = tmp.layout()
        path = QPainterPath()
        for x1, y1, x2, y2 in lay["lines"]:
            path.moveTo(x1, y1)
            path.lineTo(x2, y2)
        for tri in lay["fills"]:
            first = True
            for px, py in tri:
                if first:
                    path.moveTo(px, py)
                    first = False
                else:
                    path.lineTo(px, py)
            path.closeSubpath()
        return path

    def _updatePreview(self, cursor: Point3D) -> None:
        self._setPreviewPath(self._previewPath(cursor), solid=True)
        self._setCursorMarker(cursor)

    # ---------------- 交互 ----------------

    def onMousePress(self, wp: Point3D, mods) -> None:
        if self._p1 is None:
            self._p1 = wp
            self._setCursorMarker(wp)
            return
        p2 = wp
        dx = p2.x - self._p1.x
        dy = p2.y - self._p1.y
        if math.hypot(dx, dy) < 1e-6:
            self._manager.notifyResult("两点重合，未生成标注")
            self.finish()
            return
        fs, gap = self._style()
        obj = DimensionObject()
        obj.setLinear(self._p1, p2, fs, gap)
        self._doc.addObject(obj)
        self._manager.notifyResult(
            f"对齐标注已创建（距离 {obj.measureValue():.2f}）")
        self.finish()

    def onMouseMove(self, wp: Point3D, mods) -> None:
        if self._p1 is None:
            self._setCursorMarker(wp)
            return
        self._updatePreview(wp)


def registerAll(cm) -> None:
    cm.register("AlignDim", AlignDimCommand)

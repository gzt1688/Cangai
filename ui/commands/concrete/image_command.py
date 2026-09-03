"""图片放置命令：选文件后用鼠标点两个角点把图片框出来。

- 第一点/第二点确定目标矩形，图片**等比缩放**适配（contain）并居中——
  描图底图不变形；
- 第一角点后随鼠标实时预览：半透明位图 + 区域虚线框；
- 回车/右键取消放置（与其它命令的取消约定一致）。
"""
from __future__ import annotations
import math

from PySide6.QtCore import QRectF, QPointF, Qt
from PySide6.QtGui import QTransform, QPainterPath
from PySide6.QtWidgets import QGraphicsPixmapItem

from core.vector.geometry_types import Point3D
from core.vector.image_object import ImageObject
from ..command_instance import CommandInstance


class ImagePlaceCommand(CommandInstance):
    def __init__(self, path: str) -> None:
        super().__init__()
        self._path = path
        self._p1: Point3D | None = None
        self._obj: ImageObject | None = None
        self._imgPreview: QGraphicsPixmapItem | None = None

    def name(self) -> str:
        return "Image"

    def commandTitle(self) -> str:
        return "放置图片"

    def prompt(self) -> str:
        if self._p1 is None:
            return "指定图片区域的第一个角点"
        return "指定对角点完成放置（右键取消）"

    def start(self) -> None:
        super().start()
        self._obj = ImageObject.fromFile(self._path)

    # ---------------- 预览 ----------------

    def _ensureImgPreview(self) -> None:
        if self._imgPreview is None and self._view is not None \
                and self._obj is not None:
            self._imgPreview = QGraphicsPixmapItem(self._obj.pixmap())
            self._imgPreview.setOpacity(0.6)
            self._imgPreview.setZValue(9998)
            self._imgPreview.hide()
            self._view.scene().addItem(self._imgPreview)

    def _updatePreview(self, cursor: Point3D) -> None:
        """第一角点已定：按目标矩形刷新半透明位图 + 区域虚线框。"""
        if self._p1 is None:
            return
        rect = self._targetRect(cursor)
        self._setPreviewPath(self._rectPath(rect))
        self._setCursorMarker(cursor)
        self._ensureImgPreview()
        if self._imgPreview is not None and self._obj is not None:
            w, h, cx, cy = self._fitSize(rect)
            pm = self._imgPreview.pixmap()
            t = QTransform()
            t.translate(cx, cy)
            t.scale(1.0, -1.0)   # 世界 Y 向上：翻回屏幕式再贴图保持正立
            t.scale(w / max(pm.width(), 1), h / max(pm.height(), 1))
            t.translate(-pm.width() * 0.5, -pm.height() * 0.5)
            self._imgPreview.setTransform(t)
            self._imgPreview.show()

    def _clearPreview(self) -> None:
        if self._imgPreview is not None and self._view is not None:
            self._view.scene().removeItem(self._imgPreview)
        self._imgPreview = None
        super()._clearPreview()

    # ---------------- 几何 ----------------

    @staticmethod
    def _rectPath(rect: QRectF) -> QPainterPath:
        path = QPainterPath()
        path.addRect(rect)
        return path

    def _targetRect(self, p2: Point3D) -> QRectF:
        p1 = self._p1
        return QRectF(QPointF(p1.x, p1.y), QPointF(p2.x, p2.y)).normalized()

    def _fitSize(self, rect: QRectF) -> tuple:
        """图片等比适配目标矩形（contain 居中）：返回 (宽, 高, 中心x, 中心y)。
        矩形过小（点击未拖开）时按 100 世界宽等比。"""
        iw, ih = self._obj.pixelSize()
        iw = max(float(iw), 1.0)
        ih = max(float(ih), 1.0)
        rw, rh = rect.width(), rect.height()
        if rw < 1e-6 or rh < 1e-6:
            w = 100.0
            h = w * ih / iw
        else:
            s = min(rw / iw, rh / ih)
            w, h = iw * s, ih * s
        c = rect.center()
        return (w, h, c.x(), c.y())

    # ---------------- 交互 ----------------

    def onMousePress(self, wp: Point3D, mods) -> None:
        if self._p1 is None:
            self._p1 = wp
            return
        rect = self._targetRect(wp)
        w, h, cx, cy = self._fitSize(rect)
        self._obj.setGeometry(Point3D(cx, cy, 0.0), w, h, 0.0)
        self._doc.addObject(self._obj)
        self._manager.notifyResult(
            f"图片已放置（{w:.1f}×{h:.1f}，图层：图片）")
        self.finish()

    def onMouseMove(self, wp: Point3D, mods) -> None:
        if self._p1 is None:
            self._setCursorMarker(wp)
            return
        self._updatePreview(wp)

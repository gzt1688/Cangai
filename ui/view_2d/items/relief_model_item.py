"""浮雕模型 2D 图元：着色预览 + 网格轮廓，随放置仿射 ``_place`` 几何变换。

设计（对齐方案 v2 阶段 C）：
- 图元坐标系 = 世界坐标；``paint`` 内用 ``_place`` 构 ``QTransform`` 把 local 网格映射到世界，
  故移动/缩放/旋转只几何变换缓存位图，**不重算光照**（快、无损、实时预览）。
- 预览位图由 :mod:`core.relief.shade` 纯 numpy 着色 → ``QImage`` → 缓存 ``QPixmap``；
  着色前 ``hf.downsample(preview_max_dim)``（设置页可调，默认 1024），缓存键为
  对象 ``version()``（网格/显示参数变更才重算），重算按 ``_REBUILD_MIN_INTERVAL`` 节流。
- ``shape()`` 返回变换后**填充**多边形（内部可命中，点选/框选可选中）；
  ``boundingRect()`` 返回变换后世界 AABB + 屏幕像素余量（随视图缩放刷新）。
- **行序**：hf 行0=minY，视图 ``scale(1,-1)`` 翻转 Y——两者抵消后 minY 恰在屏幕底部，
  故此处不做任何镜像（往返测试见 _test_relief_e2e）。
"""
from __future__ import annotations

import time

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QPen, QColor, QImage, QPixmap, QPainterPath, QTransform
from PySide6.QtWidgets import QStyleOptionGraphicsItem, QStyle

from core.relief import shade
from core import app_settings
from .base_item import BaseGraphicsItem

# 预览着色前的降采样上限兜底值（实际取 app_settings.preview_max_dim，设置页可调）。
# 旧实现写死 1024，用户在设置里调到更高分辨率也不生效——2D 预览始终糊。
_PREVIEW_MAX_DIM = 1024
# 预览重建最小间隔（秒）：2116² 单次 107ms，笔画期每帧重建会拖住整个界面，
# 约 7 次/秒足够跟上塑形，且把最坏开销压在 ~15% 的帧时间以内。
_REBUILD_MIN_INTERVAL = 0.14
# 边框/选中光晕的屏幕像素余量（换算回世界，随缩放变化）
_EDGE_PX = 6.0


class ReliefModelItem(BaseGraphicsItem):
    """SURFACE 对象的 2D 图元：着色预览面 + 轮廓。"""

    def __init__(self, obj) -> None:
        super().__init__(obj)
        self._pixmap: QPixmap | None = None
        self._cachedKey: int | None = None
        self._pixmapDirty = False
        self._lastRebuild = 0.0          # 上次重建时刻（节流用）
        self._deferredPending = False    # 是否已排队一次延迟刷新
        self._rebuildCache()

    # ---------------- 预览缓存 ----------------

    @staticmethod
    def _previewDim() -> int:
        """预览降采样上限：读设置项 preview_max_dim（设置页可调，128~4096）。"""
        try:
            return max(64, int(app_settings.preview_max_dim))
        except Exception:
            return _PREVIEW_MAX_DIM

    def _rebuildCache(self) -> None:
        """按当前显示模式（等高线/灰度图）重算预览位图。"""
        o = self._obj
        t0 = time.perf_counter()
        small = o.heightField().downsample(self._previewDim())
        img = shade.render_preview(
            small, o.displayMode(), o.lightAzimuth(), o.lightAltitude(),
            o.zExaggeration())
        self._pixmap = self._toPixmap(img)
        self._cachedKey = o.version()
        self._pixmapDirty = False
        self._lastRebuild = time.perf_counter()
        # 2D 预览重建在 paint 时惰性发生；分辨率随 preview_max_dim 变（2116² 比
        # 1024² 贵约 4 倍），超过阈值写一行，用户日志里能直接看到它是否在卡。
        try:
            from ui.perf_monitor import log_slow
            log_slow("preview2d", (time.perf_counter() - t0) * 1e3,
                     "dim=%d %dx%d" % (self._previewDim(), img.shape[0],
                                       img.shape[1]), thr=40.0)
        except Exception:
            pass

    @staticmethod
    def _toPixmap(img: np.ndarray) -> QPixmap:
        """uint8 ndarray → QPixmap：gray→Grayscale8、rgb→RGB888；立即由
        QPixmap.fromImage 深拷贝，numpy 缓冲随即可释放。"""
        img = np.ascontiguousarray(img)
        h, w = img.shape[0], img.shape[1]
        if w <= 0 or h <= 0:
            return QPixmap()
        if img.ndim == 2:
            qimg = QImage(img.data, w, h, w, QImage.Format_Grayscale8)
        else:
            qimg = QImage(img.data, w, h, w * 3, QImage.Format_RGB888)
        return QPixmap.fromImage(qimg)

    # ---------------- 几何（世界坐标） ----------------

    def _placeTransform(self) -> QTransform:
        """由放置仿射 ``_place``（local->world）构 QTransform。

        numpy M: world_x=M00*lx+M01*ly+M02, world_y=M10*lx+M11*ly+M12；
        Qt QTransform(m11,m12,m21,m22,dx,dy): x'=m11*x+m21*y+dx, y'=m12*x+m22*y+dy。
        """
        M = self._obj.placement()
        return QTransform(M[0, 0], M[1, 0], M[0, 1], M[1, 1], M[0, 2], M[1, 2])

    def _localRect(self) -> QRectF:
        """local 网格矩形（minX, minY, 宽, 高）——图元画布范围。"""
        hf = self._obj.heightField()
        x0, y0 = hf.origin
        sx, sy = hf.size_mm()
        return QRectF(x0, y0, sx, sy)

    def _worldCorners(self):
        M = self._obj.placement()
        r = self._localRect()
        x0, y0 = r.left(), r.top()
        x1, y1 = r.right(), r.bottom()
        out = []
        for cx, cy in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            out.append((M[0, 0] * cx + M[0, 1] * cy + M[0, 2],
                        M[1, 0] * cx + M[1, 1] * cy + M[1, 2]))
        return out

    def _edgeMargin(self) -> float:
        views = self.scene().views() if self.scene() is not None else []
        scale = views[0].zoomScale() if views else 1.0
        return _EDGE_PX / max(scale, 1e-9)

    def boundingRect(self) -> QRectF:
        pts = self._worldCorners()
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        minx, maxx = min(xs), max(xs)
        miny, maxy = min(ys), max(ys)
        m = self._edgeMargin()
        return QRectF(minx - m, miny - m, (maxx - minx) + 2 * m, (maxy - miny) + 2 * m)

    def shape(self) -> QPainterPath:
        """变换后填充多边形：模型内部可命中（区别于曲线的描边带命中）。"""
        pts = self._worldCorners()
        path = QPainterPath()
        path.moveTo(pts[0][0], pts[0][1])
        for p in pts[1:]:
            path.lineTo(p[0], p[1])
        path.closeSubpath()
        return path

    # ---------------- 同步 / 绘制 ----------------

    def syncFromObject(self) -> None:
        super().syncFromObject()
        # 预览重建只"标记脏"，推迟到 paint 时惰性执行：浮雕模式的笔画
        # （堆料/磨光/Ctrl 涂抹）每 33ms 推一次 version，若在此急切重建，
        # 会在**隐藏的** 2D 画布上白烧几十毫秒/次（downsample 1024 + 着色），
        # 直接造成 3D 视口长帧（模型闪跳）。隐藏视图不会 paint → 零成本；
        # 2D 可见时 paint 前重建一次，Qt 事件合并天然节流。
        self._pixmapDirty = True
        # _place 可能已变（移动/缩放/旋转）→ 世界 AABB/shape 变，需告知场景
        self.prepareGeometryChange()
        self.update()

    def onViewScaleChanged(self) -> None:
        """视图缩放变化：boundingRect 的像素余量随缩放变，刷新几何缓存。"""
        self.prepareGeometryChange()
        self.update()

    def _scheduleDeferredRebuild(self) -> None:
        """节流窗口内被跳过的重建：到期后再 update 一次，保证画面最终追平。"""
        if self._deferredPending:
            return
        self._deferredPending = True
        from PySide6.QtCore import QTimer
        QTimer.singleShot(int(_REBUILD_MIN_INTERVAL * 1000) + 20,
                          self._deferredRebuild)

    def _deferredRebuild(self) -> None:
        """节流窗口结束后的补帧（由 QTimer 回调，可能晚于图元销毁）。"""
        try:
            self._deferredPending = False
            if not self.isVisible():
                return      # 隐藏视图不重建（可见时会重新 paint）
            self.update()
        except RuntimeError:
            # 图元对应的 C++ 对象已随文档删除/场景清空而销毁：定时器回调
            # 仍会触发，访问 self 会抛 "Internal C++ object already deleted"，
            # 这里静默吞掉（没有画面需要刷新）。
            pass

    def paint(self, painter, option, widget=None) -> None:
        # 抑制 Qt 默认选中虚线框；浮雕模型选中效果由图元自绘（橙色粗边框）。
        opt = QStyleOptionGraphicsItem(option)
        opt.state &= ~QStyle.StateFlag.State_Selected

        o = self._obj
        if self._pixmapDirty or self._obj.version() != self._cachedKey:
            # 节流：preview_max_dim=2116 时单次重建 107ms（1024² 约 24ms），
            # 雕刻/磨光每 33ms 推一次 version，若每次 paint 都重建，2D 视图
            # 可见时（分屏）会稳定掉到 10fps 以下，还把 3D 视口一起拖住。
            # 这里限制为最多 ~7 次/秒，中间几帧沿用上一张位图（浮雕模式下
            # 2D 只是参考底图，短暂滞后无感），并安排一次延迟刷新补齐最终状态。
            now = time.perf_counter()
            if now - self._lastRebuild < _REBUILD_MIN_INTERVAL:
                self._scheduleDeferredRebuild()
            else:
                # 时刻由 _rebuildCache 末尾统一记录（含实际绘制耗时）
                self._rebuildCache()      # 惰性重建：仅在本图元真实绘制时执行

        localRect = self._localRect()
        tr = self._placeTransform()

        painter.save()
        painter.setTransform(tr, True)          # 现在 painter 坐标 = local 网格坐标

        if self._pixmap is not None and not self._pixmap.isNull():
            painter.drawPixmap(localRect, self._pixmap, QRectF(self._pixmap.rect()))

        # 边框：选中橙色加粗，否则对象色
        if o.isSelected():
            from ui.styles import theme
            pen = QPen(QColor(theme.selectionColorFor(o)), 3.0)
        else:
            pen = QPen(QColor(*o.color()), max(o.lineWidth(), 1.0))
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(localRect)
        painter.restore()

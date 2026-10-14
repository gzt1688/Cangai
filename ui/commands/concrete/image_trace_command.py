"""图片转线稿命令（移植自 Rhino 插件 ImageTraceCommand + ImageTraceForm）。

选画布图片或磁盘图片 → 调整识别参数（阈值/反色/平滑/去噪/拟合）→
画布实时预览（默认叠加在图片正上方）→ 生成曲线（单个多段曲线对象，
自动归入"曲线"层，与本次命令合并为一次撤销）。
"""
from __future__ import annotations

import math

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtGui import QImage, QPainterPath

from core.vector.geometry_types import Point3D
from core.vector.image_object import ImageObject
from core.vector.image_vectorizer import TraceResult, vectorize
from core.vector.poly_curve_object import PolyCurveObject
from ..command_instance import CommandInstance
from .image_trace_panel import ImageTracePanel


class _TraceThread(QThread):
    """后台识别线程：完成发 traced 信号（跨线程自动排队回 UI 线程）。"""

    traced = Signal(object)

    def __init__(self, image: QImage, options, parent=None) -> None:
        super().__init__(parent)
        self._image = image
        self._options = options

    def run(self) -> None:
        res = vectorize(self._image, self._options)
        self.traced.emit(res)


class ImageTraceCommand(CommandInstance):
    def __init__(self) -> None:
        super().__init__()
        self._panel: ImageTracePanel | None = None
        self._image: QImage | None = None        # 当前识别源图
        self._imgObjId: str | None = None        # 画布图片来源 id（叠加放置用）
        self._result: TraceResult | None = None
        self._mapped = False                     # 当前结果是否叠加在图片上
        self._gen = 0                            # 预览代数：参数变化/关闭后使在途结果作废
        self._busy = False
        self._pending = False
        self._threads: list = []
        self._debounce = QTimer()
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)          # 拖动参数 0.25s 后才重跑识别
        self._debounce.timeout.connect(self.runTraceNow)

    def name(self) -> str:
        return "ImageTrace"

    def commandTitle(self) -> str:
        return "图片转线稿"

    def prompt(self) -> str:
        return "调整参数后点「生成曲线」（右键/回车确认，Esc 取消）"

    def showSnapHints(self) -> bool:
        return False

    def paramPanel(self):
        if self._panel is None:
            self._panel = ImageTracePanel()
            self._panel.paramChanged.connect(self._schedule)
            self._panel.commitRequested.connect(self.commitTraces)
            self._panel.cancelRequested.connect(self._onPanelCancel)
            self._loadSource()
        return self._panel

    # ---------------- 来源 ----------------

    def refreshDocImages(self) -> None:
        """命令激活期间画布新放入图片时自动采用。"""
        if self._panel is None:
            return
        if self._image is None:
            self._loadSource()

    def _loadSource(self) -> None:
        """自动取画布上第一张图片作为识别来源（曲线叠加到图片平面）。"""
        self._image = None
        self._imgObjId = None
        for obj in self._doc.objects():
            if isinstance(obj, ImageObject) and obj.pixelSize()[0] > 0:
                try:
                    img = QImage.fromData(obj.imageData())
                except Exception:
                    img = None
                if img is not None and not img.isNull():
                    self._imgObjId = obj.id()
                    self._image = img
                break
        if self._panel is None:
            return
        if self._image is None:
            self._panel.setSourceImage(None)
            self._panel.setStatus("请先用「图片」按钮在画布放置图片，再转线稿")
            return
        self._panel.setSourceImage(self._image, autoParams=True)
        self._panel.setStatus("已载入画布图片，正在识别…")
        self._schedule()

    # ---------------- 识别调度 ----------------

    def _schedule(self) -> None:
        if self._image is None:
            return
        self._debounce.stop()
        self._debounce.start()

    def runTraceNow(self) -> None:
        """立即跑一次识别（面板防抖超时或测试直接调用）。"""
        if self._panel is None or self._image is None:
            return
        self._debounce.stop()
        if self._busy:
            self._pending = True
            return
        self._busy = True
        self._gen += 1
        gen = self._gen

        opt = self._panel.buildOptions()
        # 叠加模式：来源是画布图片物件时，曲线直接映射到图片平面上
        self._mapped = False
        if self._imgObjId is not None:
            obj = self._doc.object(self._imgObjId)
            if obj is not None and isinstance(obj, ImageObject):
                opt.mapCorners = self._imageCorners(obj)
                self._mapped = True

        th = _TraceThread(self._image.copy(), opt)
        th.traced.connect(lambda res, g=gen: self._onTraced(res, g))
        th.finished.connect(lambda t=th: self._onThreadDone(t))
        self._threads.append(th)
        th.start()
        self._panel.setStatus("识别中…")

    def _onThreadDone(self, th: _TraceThread) -> None:
        if th in self._threads:
            self._threads.remove(th)
        th.deleteLater()
        self._busy = False
        if self._pending:
            self._pending = False
            self._schedule()

    def _onTraced(self, res: TraceResult, gen: int) -> None:
        if gen != self._gen:
            return   # 已过期（参数又变了/已取消）
        self._result = res
        if self._panel is not None:
            self._panel.setBinaryPreview(res.binaryPreview)
            if res.error is not None:
                self._panel.setStatus(f"识别失败：{res.error}")
            elif res.contours == 0:
                self._panel.setStatus("未识别到线条：试试调低阈值、勾选反色，"
                                      "或把「小区域」调小")
            else:
                self._panel.setStatus(
                    f"{res.contours} 条曲线 · {res.controlPoints} 控制点 · "
                    f"{res.elapsedMs:.0f} ms（叠加在图片上）")
        self._updatePreviewPath()

    def _updatePreviewPath(self) -> None:
        path = QPainterPath()
        if self._result is not None:
            for s in self._result.strokes:
                pts = s.points
                if len(pts) < 2:
                    continue
                path.moveTo(pts[0][0], pts[0][1])
                for p in pts[1:]:
                    path.lineTo(p[0], p[1])
        self._setPreviewPath(path, solid=True)

    @staticmethod
    def _imageCorners(obj: ImageObject):
        """图片物件平面三个角点（世界坐标）：左下 A、右下 B、左上 C。"""
        c = obj.center()
        hw, hh = obj.width() * 0.5, obj.height() * 0.5
        r = obj.rotation()
        cos, sin = math.cos(r), math.sin(r)

        def loc(x, y):
            return (c.x + x * cos - y * sin, c.y + x * sin + y * cos)

        return (loc(-hw, -hh), loc(hw, -hh), loc(-hw, hh))

    # ---------------- 生成曲线 ----------------

    def lastStrokeCount(self) -> int:
        return 0 if self._result is None else len(self._result.strokes)

    def commitTraces(self) -> None:
        if self._result is None or not self._result.strokes:
            if self._panel is not None:
                self._panel.setStatus("没有可生成的曲线：请先选择图片或调整参数")
            return
        # 按识别颜色分组（VTracer 彩色模式每色一个对象并着色；其余全部无色一组）
        groups: dict = {}
        order: list = []
        for s in self._result.strokes:
            key = tuple(s.color) if s.color else None
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(s)
        for key in order:
            obj = PolyCurveObject()
            for s in groups[key]:
                obj.addSegment({
                    "points": [[x, y, 0.0] for x, y in s.points],
                    "inputPoints": [[x, y, 0.0] for x, y in s.fitPoints],
                })
            self._doc.addObject(obj)
            if key is not None:
                obj.setColor(key)
        if self._manager is not None:
            msg = (f"图片转线稿：已生成 {len(self._result.strokes)} 条曲线"
                   if len(order) == 1 else
                   f"图片转线稿：已生成 {len(self._result.strokes)} 条曲线"
                   f"（{len(order)} 个颜色组）")
            self._manager.notifyResult(msg)
        self.finish()

    # ---------------- 交互与收尾 ----------------

    def onMouseMove(self, wp: Point3D, mods) -> None:
        self._setCursorMarker(wp)

    def onMousePress(self, wp: Point3D, mods) -> None:
        pass   # 纯面板调参命令：画布点击不参与（保持基类接口）

    def onRightClick(self, wp: Point3D) -> bool:
        if self._result is not None and self._result.strokes:
            self.commitTraces()
            return True
        return False

    def onEnter(self) -> bool:
        return self.onRightClick(Point3D())

    def _onPanelCancel(self) -> None:
        """面板「取消」：清理后交由调度中心正式结束命令（含撤销事务收尾）。"""
        self.cancel()
        if self._manager is not None:
            self._manager.cancelActive()

    def cancel(self) -> None:
        self._teardown()
        super().cancel()

    def finish(self) -> None:
        self._teardown()
        super().finish()

    def _teardown(self) -> None:
        # 递增代数使在途识别结果作废（_onTraced 的 gen 守卫丢弃）；线程自然结束
        self._gen += 1
        self._pending = False
        self._debounce.stop()
        if self._panel is not None:
            self._panel._saveMemo()

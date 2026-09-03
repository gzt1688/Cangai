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
            self._panel.sourceChanged.connect(self._loadSource)
            self._panel.commitRequested.connect(self.commitTraces)
            self._panel.cancelRequested.connect(self._onPanelCancel)
            self._panel.setDocImages(self._scanDocImages())
            if self._panel.currentSource() is not None:
                self._loadSource()
            else:
                self._panel.setStatus("没有画布图片：点「浏览…」选择图片文件")
        return self._panel

    # ---------------- 来源 ----------------

    def _scanDocImages(self) -> list:
        entries = []
        for obj in self._doc.objects():
            if isinstance(obj, ImageObject) and obj.pixelSize()[0] > 0:
                w, h = obj.pixelSize()
                entries.append((obj.id(), f"{w}×{h}"))
        return entries

    def refreshDocImages(self) -> None:
        """重新扫描画布图片（命令激活期间新放入的图片立即可选）。"""
        if self._panel is None:
            return
        had = self._panel.currentSource()
        self._panel.setDocImages(self._scanDocImages())
        cur = self._panel.currentSource()
        if cur != had and cur is not None:
            self._loadSource()

    def _loadSource(self) -> None:
        self._image = None
        self._imgObjId = None
        if self._panel is None:
            return
        src = self._panel.currentSource()
        if src is None:
            self._panel.setStatus("请选择画布图片或点「浏览…」选择图片文件")
            return
        try:
            if src[0] == "doc":
                obj = self._doc.object(src[1])
                if obj is None or not isinstance(obj, ImageObject):
                    self._panel.setStatus("画布图片已不存在，请重新选择")
                    return
                self._imgObjId = obj.id()
                img = QImage.fromData(obj.imageData())
            else:
                img = QImage(src[1])
            if img is None or img.isNull():
                self._panel.setStatus("图片无法解码，请换一张图片")
                return
            self._image = img
        except Exception as e:
            self._panel.setStatus(f"载入图片失败：{e}")
            return
        self._panel.setSourceImage(self._image, autoParams=True)
        self._panel.setStatus("已载入图片，正在识别…")
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
                extra = ("（叠加在图片上）" if self._mapped
                         else f"（宽度 {self._panel.outputWidth():g}）")
                self._panel.setStatus(
                    f"{res.contours} 条曲线 · {res.controlPoints} 控制点 · "
                    f"{res.elapsedMs:.0f} ms {extra}")
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
        obj = PolyCurveObject()
        for s in self._result.strokes:
            obj.addSegment({
                "points": [[x, y, 0.0] for x, y in s.points],
                "inputPoints": [[x, y, 0.0] for x, y in s.fitPoints],
            })
        self._doc.addObject(obj)
        if self._manager is not None:
            self._manager.notifyResult(
                f"图片转线稿：已生成 {len(self._result.strokes)} 条曲线")
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

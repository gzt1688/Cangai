# -*- coding: utf-8 -*-
"""图片转线稿功能测试：核心算法（轮廓/单线/颜色/自动阈值/放置）+ 命令与按钮注册 +
端到端流程（画布图片 → 识别 → 生成曲线 → 曲线层/叠加位置）。"""
from __future__ import annotations
import math
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QImage, QColor, QPainter

from core.vector.geometry_types import Point3D, ObjectType
from core.vector import image_vectorizer as iv
from core.vector.image_vectorizer import (ImageTraceOptions,
                                          computeOtsuThreshold, detectInvert,
                                          vectorize)


def _newImage(w: int, h: int, fill: str = "white") -> QImage:
    img = QImage(w, h, QImage.Format.Format_RGB32)
    img.fill(QColor(fill))
    return img


def _drawRing(size=200, rOuter=60, rInner=30, fg="black", bg="white") -> QImage:
    img = _newImage(size, size, bg)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(fg))
    c = size / 2.0
    p.drawEllipse(QPointF(c, c), rOuter, rOuter)
    p.setBrush(QColor(bg))
    p.drawEllipse(QPointF(c, c), rInner, rInner)
    p.end()
    return img


def _drawBar(w=120, h=40, x0=8, x1=112, y0=18, y1=22) -> QImage:
    img = _newImage(w, h)
    p = QPainter(img)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("black"))
    p.drawRect(x0, y0, x1 - x0, y1 - y0)
    p.end()
    return img


def _strokeLen(s) -> float:
    return sum(math.dist(s.points[i - 1], s.points[i])
               for i in range(1, len(s.points)))


def _bbox(s):
    xs = [p[0] for p in s.points]
    ys = [p[1] for p in s.points]
    return min(xs), min(ys), max(xs), max(ys)


def run():
    app = QApplication.instance() or QApplication(sys.argv)
    results = []

    def check(name, ok):
        results.append((name, ok))
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")

    # ================= 一、核心算法 =================
    # 1) 圆环轮廓模式：外圈 + 内孔（输出宽=图案像素宽 → 1:1 对照）
    ring = _drawRing()
    res = vectorize(ring, ImageTraceOptions(centerlineMode=False, blur=1,
                                            threshold=128, outputWidth=120.0))
    check("轮廓模式无错误", res.error is None)
    check("轮廓模式识别 2 条闭合曲线",
          len(res.strokes) == 2 and all(s.closed for s in res.strokes))
    if len(res.strokes) == 2:
        widths = sorted(_bbox(s)[2] - _bbox(s)[0] for s in res.strokes)
        check("外圈直径≈120", abs(widths[1] - 120.0) < 8.0)
        check("内孔直径≈60", abs(widths[0] - 60.0) < 8.0)

    # 2) 细圆环（带宽 8px < 默认粗描边 16）单线模式：中心线为一条闭合圆
    thinRing = _drawRing(200, 40, 32)
    res2 = vectorize(thinRing, ImageTraceOptions(centerlineMode=True, blur=1,
                                                 threshold=128, outputWidth=80.0))
    check("单线模式无错误", res2.error is None)
    closedStrokes = [s for s in res2.strokes if s.closed]
    check("单线模式识别到闭合中心线", len(closedStrokes) >= 1)
    if closedStrokes:
        longest = max(closedStrokes, key=_strokeLen)
        L = _strokeLen(longest)
        # 参考算法固有：简化→1:2:1 平滑使稀疏锚点闭合环轻微收缩，再按宽缩放略放大
        check("中心线长度≈2π·36≈226", abs(L - 2 * math.pi * 36) < 40.0)
        pts = np.asarray(longest.points)
        r = np.hypot(pts[:, 0], pts[:, 1])
        check("中心线半径合理且接近正圆",
              abs(r.mean() - 36.0) < 6.0 and r.std() < 3.0)

    # 3) 细横条单线模式：一条水平开放中心线，输出居中在原点
    res3 = vectorize(_drawBar(), ImageTraceOptions(centerlineMode=True, blur=1,
                                                   threshold=128, outputWidth=104.0))
    check("细条单线无错误", res3.error is None)
    openStrokes = [s for s in res3.strokes if not s.closed]
    check("细条识别 1 条开放中心线", len(openStrokes) == 1)
    if openStrokes:
        x0, y0, x1, y1 = _bbox(openStrokes[0])
        check("中心线沿横条中轴且居中原点",
              (y1 - y0) < 4.0 and abs((y0 + y1) / 2) < 3.0
              and 100.0 < (x1 - x0) <= 104.5)

    # 4) 颜色拾取模式：红圆（白底）
    labA = iv.rgbToLab(0xD0, 0x02, 0x1B)
    labB = iv.rgbToLab(255, 255, 255)
    res4 = vectorize(_drawRing(200, 60, 0, fg="#d0021b"), ImageTraceOptions(
        centerlineMode=False, blur=1, useColorKeys=True,
        colorAKeysLab=[labA], colorBKeysLab=[labB], outputWidth=120.0))
    check("颜色模式无错误", res4.error is None)
    check("颜色模式识别 1 条闭合曲线",
          len(res4.strokes) == 1 and res4.strokes[0].closed)
    if res4.strokes:
        x0, _, x1, _ = _bbox(res4.strokes[0])
        check("红圆直径≈120", abs((x1 - x0) - 120.0) < 8.0)

    # 5) Otsu 自动阈值与反色检测
    t = computeOtsuThreshold(ring, enhance=True)
    check("Otsu 阈值有效范围", 0 <= t <= 255)
    check("白底黑线不反色", detectInvert(ring, enhance=True) is False)
    res5 = vectorize(_drawRing(fg="white", bg="black"),
                     ImageTraceOptions(centerlineMode=False, threshold=t,
                                       invert=True, outputWidth=120.0))
    check("反色后轮廓识别 2 条", len(res5.strokes) == 2)

    # 6) 叠加映射：像素空间 → 图片平面角点
    res6 = vectorize(ring, ImageTraceOptions(
        centerlineMode=False, blur=1, threshold=128,
        mapCorners=((0.0, 0.0), (100.0, 0.0), (0.0, 50.0))))
    check("叠加映射无错误", res6.error is None)
    if res6.strokes:
        xs = [p[0] for s in res6.strokes for p in s.points]
        ys = [p[1] for s in res6.strokes for p in s.points]
        check("叠加映射落在图片平面内",
              min(xs) >= -1e-6 and max(xs) <= 100.0 + 1e-6
              and min(ys) >= -1e-6 and max(ys) <= 50.0 + 1e-6)

    check("黑白预览图生成", res2.binaryPreview is not None
          and not res2.binaryPreview.isNull())
    check("识别结果含诊断日志", len(res2.debug) > 0)

    # ================= 二、按钮与命令注册 =================
    from ui.widgets.top_dock import CATEGORIES, ICON_MAP
    drawGroup = next((g for name, g in CATEGORIES if name == "曲线绘制"), [])
    check("曲线绘制组含图片转线按钮",
          any(k == "ImageTrace" for k, _ in drawGroup))
    check("按钮已配图标", ICON_MAP.get("ImageTrace") == "icons/imagetrace.svg"
          and os.path.exists("icons/imagetrace.svg"))

    from ui.windows.mainWindow import MainWindow
    w = MainWindow()
    check("ImageTrace 命令已注册且可启动",
          w._cm.executeCommand("ImageTrace") and w._cm.hasActive())
    inst = w._cm.activeInstance()
    check("命令激活且带参数面板",
          inst is not None and inst.paramPanel() is not None)

    # ================= 三、端到端：画布图片 → 识别 → 生成曲线 =================
    from core.vector.image_object import ImageObject
    png = os.path.join(tempfile.mkdtemp(), "ring.png")
    ring.save(png)
    imgObj = ImageObject.fromFile(png)
    imgObj.setGeometry(Point3D(0, 0, 0), 100.0, 100.0, 0.0)
    w._doc.addObject(imgObj)

    panel = inst.paramPanel()
    inst.refreshDocImages()
    check("面板扫描到画布图片", panel.docImageCount() == 1)
    inst.runTraceNow()   # 同步触发一次识别（绕开防抖）

    t0 = time.time()   # 等后台线程完成（真实时间，最多 30s）
    while inst.lastStrokeCount() == 0 and time.time() - t0 < 30 \
            and w._cm.hasActive():
        app.processEvents()
        time.sleep(0.02)
    app.processEvents()
    check("识别出预览曲线", inst.lastStrokeCount() > 0)
    check("曲线叠加在图片平面上（映射模式）", inst._mapped)

    inst.commitTraces()
    app.processEvents()
    polys = [o for o in w._doc.objects() if o.type() == ObjectType.POLYCURVE]
    check("生成曲线已入文档（单对象、曲线层）",
          len(polys) == 1 and polys[0].layerId() == 0)
    check("命令已结束", not w._cm.hasActive())
    if polys:
        bb = polys[0].getBoundingBox()
        # 图片中心 (0,0)、宽高 100 → 世界范围 [-50,50]；曲线叠加应落在其中
        check("曲线包围盒落在图片平面范围内",
              bb.min.x >= -50.0 - 1e-6 and bb.max.x <= 50.0 + 1e-6
              and bb.min.y >= -50.0 - 1e-6 and bb.max.y <= 50.0 + 1e-6)
        segs = polys[0].segments()
        check("曲线段含编辑锚点",
              len(segs) > 0 and all(len(s.get("inputPoints", [])) >= 2
                                    for s in segs))

    print("=== 图片转线稿测试 ===")
    allOk = all(ok for _, ok in results)
    print(f"=== {'全部通过' if allOk else '存在失败'} ===")
    return allOk


if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)

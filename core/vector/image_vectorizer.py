"""图片转线稿核心算法（VTracer 开源引擎，Rust 本地编译轮子）。

流程：QImage → 等比缩放（最长边 ≤1000）→ RGBA → vtracer 聚类/拟合 →
SVG → svg_io.parseSvg 解析回折线（像素空间、y 轴向上）。
聚合方式（照官方桌面版面板）：
  黑白 bw        —— binary_threshold 用面板阈值，反色在像素上做
  彩色 color-cluster —— 官方默认色彩精度 6 / 层差 16
  分水岭 watershed   —— detail 滑块（watershed_detail）
合成 stacked/cutout、拟合 pixel/polygon/spline、简化 simplify（0=关）、
路径精度 path_precision 均直传引擎。彩色模式色块携带填充色。
黑白模式附一张二值化预览图。

输出坐标系：处理分辨率像素空间（w0×h0），y 轴向上、原点在图片左下角；
由调用方按"叠加到图片平面"或"缩放到输出宽度"放置到世界坐标。
"""
from __future__ import annotations

import math
import time
import traceback
from dataclasses import dataclass, field

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

_MAX_PROCESS_SIDE = 1000   # 处理分辨率上限（最长边像素）：预览与最终输出同一分辨率


# ==================== 参数与结果 ====================

@dataclass
class ImageTraceOptions:
    """图片转线稿参数（面板 UI 与核心算法之间传递）。"""
    threshold: int = 128          # 黑白聚合阈值（0-255），载入时 Otsu 自动估算
    invert: bool = False          # 反色：浅色线条画在深色背景上（仅黑白聚合）
    minAreaPx: float = 0.0        # 忽略小于该面积（像素²）的轮廓，0 = 不过滤
    fitTolPx: float = 1.0         # 编辑锚点简化容差（像素）
    # ---- VTracer 参数（与官方桌面版面板一一对应）----
    vtCluster: str = "watershed"   # 聚合："bw" / "color-cluster" / "watershed"
    vtDetail: int = 122            # 细节（仅分水岭）
    vtHierarchical: str = "cutout"  # 合成："stacked" / "cutout"
    vtSpeckle: int = 0             # 杂质过滤
    vtFit: str = "spline"          # 曲线拟合："pixel" / "polygon" / "spline"
    vtSimplify: float = 1.2        # 简化（px，0 = 关闭）
    vtPathPrec: int = 2            # 路径精度（小数位）
    vtLightLuma: int = 200         # 浅色过滤：填充亮度 > 该值的色块不输出
                                   # 曲线（255 = 不过滤；用于去掉背景色块
                                   # 在字形之间穿过的轮廓线）
    outputWidth: float = 100.0     # 输出宽度（世界单位），mapCorners 为空时使用
    centerAtOrigin: bool = True    # 居中放置到原点；否则左下角对齐原点
    # 叠加模式：像素空间 (0..w0, 0..h0) 映射到图片平面（世界坐标三个角点），
    # 非空时忽略 outputWidth/centerAtOrigin
    mapCorners: tuple | None = None   # (A, B, C) = 图片左下/右下/左上角（世界坐标）
    debugLog: object = None           # 诊断日志回调 fn(str)，None 关闭


@dataclass
class TraceStroke:
    """单条识别结果折线：points 供显示/捕捉，fitPoints 供编辑控制点。"""
    points: list                  # [(x, y), ...]；闭合折线首尾点重复
    fitPoints: list               # [(x, y), ...] 编辑锚点（简化后的折线）
    closed: bool
    color: tuple | None = None    # 识别区域填充色 (r,g,b)（VTracer 彩色模式）


@dataclass
class TraceResult:
    """单次识别结果。"""
    strokes: list = field(default_factory=list)   # [TraceStroke]
    contours: int = 0
    controlPoints: int = 0
    width: int = 0                 # 处理分辨率（像素）
    height: int = 0
    elapsedMs: float = 0.0
    binaryPreview: QImage | None = None   # 黑白预览图（行 0 = 图片顶部）
    previewCircles: list = field(default_factory=list)  # 滚圆示意 [(cx, cy, r)]（仅预览）
    debug: list = field(default_factory=list)  # 诊断日志（各阶段计数）
    error: str | None = None


# ==================== 对外入口 ====================

def computeOtsuThreshold(image: QImage, enhance: bool) -> int:
    """Otsu 全局自动阈值：按灰度直方图最大化类间方差，返回 0-255。"""
    gray = _grayField(image, enhance)
    if gray.size == 0:
        return 128
    hist = np.bincount(np.clip(gray.astype(np.int32), 0, 255).ravel(),
                       minlength=256)
    total = int(gray.size)
    sumAll = float(np.dot(np.arange(256, dtype=np.float64), hist))
    sumB = 0.0
    wB = 0
    bestVar = -1.0
    bestT = 128
    for t in range(256):
        wB += int(hist[t])
        if wB == 0:
            continue
        wF = total - wB
        if wF == 0:
            break
        sumB += float(t) * float(hist[t])
        mB = sumB / wB
        mF = (sumAll - sumB) / wF
        varBetween = float(wB) * wF * (mB - mF) * (mB - mF)
        if varBetween > bestVar:
            bestVar = varBetween
            bestT = t
    return bestT


def detectInvert(image: QImage, enhance: bool) -> bool:
    """按图片边框一圈的平均亮度判断是否需要反色（深背景返回 True）。"""
    gray = _grayField(image, enhance)
    h, w = gray.shape
    if h == 0 or w == 0:
        return False
    ring = np.concatenate([gray[0, :], gray[-1, :], gray[1:-1, 0], gray[1:-1, -1]])
    return float(ring.mean()) < 128.0


def vectorize(image: QImage, o: ImageTraceOptions) -> TraceResult:
    """执行识别：图片 → 参数 → 折线结果（像素空间、y 轴向上）。VTracer 引擎。"""
    st = TraceResult()
    t0 = time.perf_counter()
    logs: list = []

    def Log(s: str) -> None:
        logs.append(s)
        if o.debugLog is not None:
            try:
                o.debugLog(s)
            except Exception:
                pass

    try:
        rgb = _loadRgb(image, o)          # (h0, w0, 3) float32 0..255，y 轴向上
        h0, w0 = rgb.shape[:2]
        st.width, st.height = w0, h0
        if w0 < 2 or h0 < 2:
            st.error = "图片尺寸过小"
            return st

        try:
            import vtracer  # noqa: F401
        except ImportError:
            raise RuntimeError(
                "VTracer 引擎未安装（本地构建方法见 requirements.txt 注释）")

        strokes = _vectorizeVtracer(rgb, o, w0, h0, Log)

        # 黑白聚合给一张二值化预览（与阈值/反色一致）
        fld = None
        iso = invert = 0.0
        if o.vtCluster == "bw":
            pad = 0.0 if o.invert else 255.0
            fld = np.full((h0 + 2, w0 + 2), pad, dtype=np.float32)
            fld[1:-1, 1:-1] = _lumaField(rgb)
            iso, invert = float(o.threshold), o.invert

        # 放置：叠加到图片平面，或缩放到输出宽度
        if o.mapCorners is not None and strokes:
            _mapToCorners(strokes, st.previewCircles, o.mapCorners, w0, h0)
        elif strokes:
            _scaleToWidth(strokes, st.previewCircles, o)

        st.strokes = strokes
        st.contours = len(strokes)
        st.controlPoints = sum(len(s.fitPoints) for s in strokes)
        if fld is not None:
            st.binaryPreview = _renderBinary(fld, w0, h0, iso, invert, None)
    except Exception as ex:
        st.error = type(ex).__name__ + ": " + str(ex)
        traceback.print_exc()
    st.elapsedMs = (time.perf_counter() - t0) * 1000.0
    st.debug = logs
    return st


# ==================== VTracer 引擎 ====================

def _rgbToRgbaBuffer(rgb: np.ndarray, invert: bool) -> bytes:
    """rgb 场 (h,w,3) 0..255 y 轴向上 → RGBA8888 bytes（图片顶部行在前）。

    透明像素已在 _loadRgb 合成白底；黑白模式反色时在此反转 RGB
    （VTracer 黑白聚类以深色为前景，浅底深线图需先反色）。"""
    h, w = rgb.shape[:2]
    buf = np.empty((h, w, 4), dtype=np.uint8)
    buf[:, :, :3] = np.clip(rgb, 0.0, 255.0).astype(np.uint8)
    buf[:, :, 3] = 255
    if invert:
        buf[:, :, :3] = 255 - buf[:, :, :3]
    return buf[::-1].tobytes()


def _vtracerConfig(o):
    """面板参数 → vtracer.Config（与官方桌面版面板参数一一对应）。"""
    import vtracer
    cfg = vtracer.Config(
        clustering=o.vtCluster,
        hierarchical=o.vtHierarchical,
        mode=o.vtFit,
    )
    if o.vtCluster == "bw":
        cfg.binary_threshold = int(max(0, min(255, o.threshold)))
    elif o.vtCluster == "watershed":
        cfg.watershed_detail = int(max(0, min(255, o.vtDetail)))
    # 彩色聚类用官方默认（色彩精度 6 / 层差 16）
    cfg.filter_speckle = int(max(0, min(128, o.vtSpeckle)))
    cfg.simplify = float(o.vtSimplify) if o.vtSimplify > 0 else None
    cfg.path_precision = int(max(0, min(8, o.vtPathPrec)))
    return cfg


def _vectorizeVtracer(rgb: np.ndarray, o, w0: int, h0: int, Log) -> list:
    """VTracer：RGB 场 → SVG → 解析路径 → TraceStroke 列表（像素空间、y 向上）。"""
    import time as _time
    from core.vector.svg_io import parseSvg

    rgba = _rgbToRgbaBuffer(rgb, o.invert and o.vtCluster == "bw")
    cfg = _vtracerConfig(o)
    t0 = _time.perf_counter()
    svg = cfg.convert_pixels(rgba, w0, h0)
    Log(f"VTracer: {w0}x{h0} 转换 {(_time.perf_counter() - t0) * 1000:.0f} ms"
        f"，SVG {max(1, len(svg) // 1024)} KB")
    info = parseSvg(svg)

    strokes: list = []
    fitTol = max(0.3, o.fitTolPx)
    colorOut = o.vtCluster != "bw"   # 黑白之外的模式输出色块填充色
    for p in info.paths:
        color = p["color"] if colorOut else None
        if o.vtLightLuma < 255 and color is not None:
            r, g, b = color
            luma = 0.299 * r + 0.587 * g + 0.114 * b
            if luma > o.vtLightLuma:
                continue   # 浅色过滤：背景/浅灰色块的轮廓不输出
        for sp in p["subpaths"]:
            pts = [(x, h0 - y) for (x, y) in sp["points"]]
            if sp["closed"]:
                if len(pts) < 4 or \
                        abs(_signedArea(pts)) < max(0.2, o.minAreaPx):
                    continue
                fit = _simplifyClosed(list(pts), fitTol)
                if len(fit) < 3:
                    fit = list(pts)
            else:
                if len(pts) < 2:
                    continue
                fit = (_douglasPeucker(list(pts), fitTol)
                       if len(pts) > 2 else list(pts))
            strokes.append(TraceStroke(points=pts, fitPoints=fit,
                                       closed=sp["closed"], color=color))
    Log(f"VTracer: {len(info.paths)} 填充路径 → {len(strokes)} 条曲线"
        f"（跳过 {info.skipped} 元素）")
    return strokes


# ==================== 位图预处理 ====================

def _loadRgb(image: QImage, o: ImageTraceOptions) -> np.ndarray:
    """等比缩放到最长边 ≤ 1000；透明通道按白底合成；返回 (h0, w0, 3) 0..255、y 向上。"""
    img = image.convertToFormat(QImage.Format.Format_ARGB32)
    scale = min(1.0, _MAX_PROCESS_SIDE /
                float(max(img.width(), img.height(), 1)))
    w0 = max(1, int(round(img.width() * scale)))
    h0 = max(1, int(round(img.height() * scale)))
    if w0 != img.width() or h0 != img.height():
        img = img.scaled(w0, h0, Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    img = img.convertToFormat(QImage.Format.Format_RGBA8888)
    h, w = img.height(), img.width()
    bpl = img.bytesPerLine()
    buf = np.frombuffer(img.constBits(), dtype=np.uint8, count=bpl * h)
    arr = buf.reshape(h, bpl)[:, :w * 4].reshape(h, w, 4).astype(np.float32)
    a = arr[:, :, 3:4] / 255.0
    rgb = arr[:, :, :3] * a + 255.0 * (1.0 - a)   # 透明像素按白底合成
    return rgb[::-1].copy()                        # 行序翻成 y 轴向上


def _grayField(image: QImage, enhance: bool) -> np.ndarray:
    """载入时（Otsu/反色检测）用：下采样 + 灰度（y 向上）+ 可选增强。"""
    img = image.convertToFormat(QImage.Format.Format_ARGB32)
    scale = min(1.0, _MAX_PROCESS_SIDE /
                float(max(img.width(), img.height(), 1)))
    w0 = max(1, int(round(img.width() * scale)))
    h0 = max(1, int(round(img.height() * scale)))
    if w0 != img.width() or h0 != img.height():
        img = img.scaled(w0, h0, Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    img = img.convertToFormat(QImage.Format.Format_RGBA8888)
    h, w = img.height(), img.width()
    bpl = img.bytesPerLine()
    buf = np.frombuffer(img.constBits(), dtype=np.uint8, count=bpl * h)
    arr = buf.reshape(h, bpl)[:, :w * 4].reshape(h, w, 4).astype(np.float32)
    a = arr[:, :, 3:4] / 255.0
    rgb = arr[:, :, :3] * a + 255.0 * (1.0 - a)
    gray = (rgb[:, :, 0] * 0.299 + rgb[:, :, 1] * 0.587 + rgb[:, :, 2] * 0.114)
    if enhance:
        gray = _enhanceLuma(gray[::-1].copy())
        return gray
    return gray[::-1].copy()


def _lumaField(rgb: np.ndarray) -> np.ndarray:
    return (rgb[:, :, 0] * 0.299 + rgb[:, :, 1] * 0.587 +
            rgb[:, :, 2] * 0.114).astype(np.float32)


def _boxBlur(f: np.ndarray) -> np.ndarray:
    """半径 1 的分离盒滤波（3×3），边界钳位，一次调用 = 一遍平滑。"""
    p = np.pad(f, ((0, 0), (1, 1)), mode="edge")
    t = (p[:, :-2] + p[:, 1:-1] + p[:, 2:]) * (1.0 / 3.0)
    p = np.pad(t, ((1, 1), (0, 0)), mode="edge")
    return (p[:-2, :] + p[1:-1, :] + p[2:, :]) * (1.0 / 3.0)


def _enhanceLuma(g: np.ndarray) -> np.ndarray:
    """亮度自动增强：百分位对比度拉伸（2%~98%）+ 反锐化遮罩（0.6）。"""
    hist = np.bincount(np.clip(g.astype(np.int32), 0, 255).ravel(), minlength=256)
    total = int(g.size)
    if total == 0:
        return g
    csum = np.cumsum(hist)
    loTarget = total * 2 // 100
    hiTarget = total * 98 // 100
    loV = min(int(np.searchsorted(csum, loTarget, side="left")), 255)
    hiV = min(int(np.searchsorted(csum, hiTarget, side="left")), 255)
    if hiV - loV > 10 and (loV > 0 or hiV < 255):
        s = 255.0 / (hiV - loV)
        g = np.clip((g - loV) * s, 0.0, 255.0)
    blur = _boxBlur(g)
    return np.clip(g + 0.6 * (g - blur), 0.0, 255.0)


def _renderBinary(fld: np.ndarray, w0: int, h0: int, iso: float, invert: bool,
                  mask) -> QImage | None:
    """二值化预览图（黑=将要被描成线的区域），与识别用同一份场。"""
    try:
        inside = (fld > iso) if invert else (fld < iso)
        if mask is not None:
            inside = mask
        step = max(1, int(math.ceil(max(w0, h0) / 500.0)))
        pw = max(1, w0 // step)
        ph = max(1, h0 // step)
        ys = np.minimum(h0 - 1, np.arange(ph) * step)
        xs = np.minimum(w0 - 1, np.arange(pw) * step)
        # 识别场是 y 轴向上（行 0 = 图片底部），预览行 0 = 顶部，需翻转读取
        sub = inside[h0 - ys, :][:, xs + 1]
        buf = np.where(sub, 0, 255).astype(np.uint8)
        img = QImage(buf.tobytes(), pw, ph, pw, QImage.Format.Format_Grayscale8)
        return img.copy()
    except Exception:
        return None


# ==================== 折线简化与转角检测 ====================

def _signedArea(p: list) -> float:
    a = 0.0
    n = len(p)
    for i in range(n):
        x1, y1 = p[i]
        x2, y2 = p[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return a / 2.0




def _distToSegment2(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 < 1e-18:
        ex, ey = px - ax, py - ay
        return ex * ex + ey * ey
    t = ((px - ax) * dx + (py - ay) * dy) / l2
    t = min(1.0, max(0.0, t))
    qx = px - (ax + t * dx)
    qy = py - (ay + t * dy)
    return qx * qx + qy * qy


def _douglasPeucker(pts: list, tol: float) -> list:
    """开放折线 Douglas-Peucker（迭代实现，避免深递归）。"""
    n = len(pts)
    if n < 3:
        return list(pts)
    keep = [False] * n
    keep[0] = keep[n - 1] = True
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    tol2 = tol * tol
    stack = [(0, n - 1)]
    while stack:
        first, last = stack.pop()
        if last - first < 2:
            continue
        ax, ay = xs[first], ys[first]
        bx, by = xs[last], ys[last]
        maxD2 = -1.0
        idx = -1
        for i in range(first + 1, last):
            d2 = _distToSegment2(xs[i], ys[i], ax, ay, bx, by)
            if d2 > maxD2:
                maxD2 = d2
                idx = i
        if maxD2 > tol2:
            keep[idx] = True
            stack.append((first, idx))
            stack.append((idx, last))
    return [pts[i] for i in range(n) if keep[i]]


def _takeWrap(pts: list, i0: int, i1: int) -> list:
    """取闭合折线上从 i0 到 i1（含端点，环绕方向）的连续段。"""
    n = len(pts)
    out = [pts[i0]]
    i = i0
    while i != i1:
        i = (i + 1) % n
        out.append(pts[i])
    return out


def _simplifyClosed(pts: list, tol: float) -> list:
    """闭合折线 Douglas-Peucker：取距质心最远点 A、距 A 最远点 B 作为锚点分两段简化。"""
    n = len(pts)
    if n <= 8:
        return list(pts)
    cx = sum(p[0] for p in pts) / n
    cy = sum(p[1] for p in pts) / n
    iA = max(range(n), key=lambda i: (pts[i][0] - cx) ** 2 + (pts[i][1] - cy) ** 2)
    iB = max(range(n), key=lambda i: (pts[i][0] - pts[iA][0]) ** 2 +
             (pts[i][1] - pts[iA][1]) ** 2)
    s1 = _douglasPeucker(_takeWrap(pts, iA, iB), tol)
    s2 = _douglasPeucker(_takeWrap(pts, iB, iA), tol)
    result = s1[:-1] + s2[:-1]
    cleaned: list = []
    for p in result:
        if not cleaned or abs(p[0] - cleaned[-1][0]) > 1e-9 or \
                abs(p[1] - cleaned[-1][1]) > 1e-9:
            cleaned.append(p)
    if len(cleaned) > 2 and abs(cleaned[0][0] - cleaned[-1][0]) < 1e-9 and \
            abs(cleaned[0][1] - cleaned[-1][1]) < 1e-9:
        cleaned.pop()
    return cleaned


# ==================== 放置 ====================

def _mapToCorners(strokes: list, circles: list, corners, w0: int, h0: int) -> None:
    """像素空间 → 图片平面仿射映射（A=左下 B=右下 C=左上，世界坐标）。"""
    (ax, ay), (bx, by), (cx, cy) = corners
    ux, uy = (bx - ax) / w0, (by - ay) / w0
    vx, vy = (cx - ax) / h0, (cy - ay) / h0

    def mapPt(p):
        u, v = p
        return (ax + u * ux + v * vx, ay + u * uy + v * vy)

    for s in strokes:
        s.points = [mapPt(p) for p in s.points]
        s.fitPoints = [mapPt(p) for p in s.fitPoints]
    mapped = []
    for (px, py, r) in circles:
        p0 = mapPt((px - r, py - r))
        p1 = mapPt((px + r, py + r))
        mapped.append(((p0[0] + p1[0]) * 0.5, (p0[1] + p1[1]) * 0.5,
                       math.dist(p0, p1) * 0.5))
    circles[:] = mapped


def _scaleToWidth(strokes: list, circles: list, o: ImageTraceOptions) -> None:
    """缩放到输出宽度并按设置放置（居中或左下角对齐原点）。"""
    allx = [p[0] for s in strokes for p in s.points]
    ally = [p[1] for s in strokes for p in s.points]
    if not allx:
        return
    minx, maxx = min(allx), max(allx)
    miny, maxy = min(ally), max(ally)
    if maxx - minx < 1e-9:
        return
    scale = o.outputWidth / (maxx - minx)
    if o.centerAtOrigin:
        ax = (minx + maxx) * 0.5
        ay = (miny + maxy) * 0.5
    else:
        ax, ay = minx, miny

    def mapPt(p):
        return ((p[0] - ax) * scale, (p[1] - ay) * scale)

    for s in strokes:
        s.points = [mapPt(p) for p in s.points]
        s.fitPoints = [mapPt(p) for p in s.fitPoints]
    circles[:] = [((cx2 - ax) * scale, (cy2 - ay) * scale, r * scale)
                  for (cx2, cy2, r) in circles]

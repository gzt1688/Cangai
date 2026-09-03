"""图片转线稿核心算法（移植自 Rhino 插件 ImageVectorizer.cs，numpy 向量化实现）。

思路对齐 vectorizer.ai 的专业位图转矢量流程：
灰度 → 盒滤波平滑 →（Otsu 自动阈值）→ 亚像素 marching squares 提取轮廓
（在平滑灰度场上线性插值取等值线，轮廓天然亚像素级平滑）→
面积过滤去噪 → Douglas-Peucker 简化 → 转角检测分割 → Schneider 三次贝塞尔拟合
（尖角处折线转折，平滑段拟合贝塞尔链）→ 拼接为输出折线（按段细分采样）。
单线（中心线）模式：掩码取"模糊场∪原始场"保发丝线 → 全图细化 + 交点归一化
→ 骨架路径按方向续接穿过交点 → 按中位线宽分粗细
（粗支路用内切圆盘重建区域走轮廓，细支路沿法向重定位到两侧等值线中点做亚像素居中）。

颜色拾取模式：A/B 双色 Lab 色差分类场，等值线落在 A、B 色差相等处，
曲线严格贴合各颜色区域的边界。

输出坐标系：处理分辨率像素空间（w0×h0），y 轴向上、原点在图片左下角；
由调用方按"叠加到图片平面"或"缩放到输出宽度"放置到世界坐标。
"""
from __future__ import annotations

import math
import time
import traceback
from dataclasses import dataclass, field, replace

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

_MAX_PROCESS_SIDE = 1000   # 处理分辨率上限（最长边像素）：预览与最终输出同一分辨率


# ==================== 参数与结果 ====================

@dataclass
class ImageTraceOptions:
    """图片转线稿参数（面板 UI 与核心算法之间传递）。"""
    threshold: int = 128          # 黑/白分界阈值（0-255），载入时 Otsu 自动估算
    invert: bool = False          # 反色：浅色线条画在深色背景上
    blur: int = 1                 # 轮廓平滑强度（盒滤波次数 0-5）
    minAreaPx: float = 0.0        # 忽略小于该面积（像素²）的轮廓，0 = 不过滤
    centerlineMode: bool = True   # True=中心线（单线）；False=区域轮廓（双线）
    pruneLenPx: float = 8.0       # 中心线：剪掉短于该长度（像素）的毛刺分支
    blobWidthPx: float = 16.0     # 中心线：中位线宽超过该值的粗笔画走轮廓（0=全部细化）
    ballDiaPx: float = 0.0        # 滚圆直径（像素）：中位内切直径不足的细笔画整条不出线
    useColorKeys: bool = False    # True=颜色拾取；False=亮度阈值
    colorAKeysLab: list = field(default_factory=list)   # 颜色A（线条）Lab 采样，可多个
    colorBKeysLab: list = field(default_factory=list)   # 颜色B（背景）Lab 采样，可多个
    colorATolerance: float = 25.0  # 颜色A 容差（ΔE）
    colorBTolerance: float = 25.0  # 颜色B 容差（ΔE）
    autoEnhance: bool = True       # 自动增强：对比度拉伸 + 锐化
    simplifyTolPx: float = 0.8     # 折线简化容差（像素）
    fitTolPx: float = 1.0          # 曲线拟合最大误差（像素）
    cornerAngleDeg: float = 65.0   # 尖角判定阈值（度）
    outputWidth: float = 100.0     # 输出宽度（世界单位），仅文件来源放置时使用
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


def rgbToLab(r: float, g: float, b: float) -> tuple:
    """sRGB → CIELAB（D65），返回 (L, A, B)。面板拾取颜色时也用它保证一致。"""
    lab = _rgbToLab(np.array([[[r, g, b]]], dtype=np.float64))
    return (float(lab[0, 0, 0]), float(lab[0, 0, 1]), float(lab[0, 0, 2]))


def vectorize(image: QImage, o: ImageTraceOptions) -> TraceResult:
    """执行识别：图片 → 参数 → 折线结果（像素空间、y 轴向上）。"""
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

        colorMode = (o.useColorKeys and
                     (len(o.colorAKeysLab) + len(o.colorBKeysLab)) > 0)
        if colorMode:
            # 颜色模式下收紧简化/拟合容差：曲线严格贴合颜色区域边界
            o = replace(o, simplifyTolPx=min(o.simplifyTolPx, 0.5),
                        fitTolPx=min(o.fitTolPx, 0.5))

        # 2. 生成"内部度"场
        if colorMode:
            vals = _colorClassifyField(rgb, o.colorAKeysLab, o.colorBKeysLab,
                                       o.colorATolerance, o.colorBTolerance)
            iso, invert = 120.0, True   # 场值高 = 内部；120 让边界略向外偏
        else:
            vals = _lumaField(rgb)
            if o.autoEnhance:
                vals = _enhanceLuma(vals)
            iso, invert = float(o.threshold), o.invert

        # 3. 外扩 1 像素背景（保证轮廓闭合）+ 盒滤波平滑
        pad = 0.0 if (invert or colorMode) else 255.0
        fld = np.full((h0 + 2, w0 + 2), pad, dtype=np.float32)
        fld[1:-1, 1:-1] = vals
        for _ in range(max(0, int(o.blur))):
            fld = _boxBlur(fld)

        strokes: list = []
        traceMask = None
        if o.centerlineMode:
            strokes, traceMask = _traceCenterline(fld, vals, w0, h0, iso,
                                                  invert, o, Log, st)
            # 最终兜底：任何原因导致零结果时，退回整图轮廓提取
            if not strokes:
                strokes = _extractOutlineStrokes(fld, iso, invert, o,
                                                 w0 - 1, h0 - 1, Log)
                if strokes:
                    Log("单线提取为空，已退回轮廓模式输出")
        else:
            strokes = _extractOutlineStrokes(fld, iso, invert, o,
                                             w0 - 1, h0 - 1, Log)

        # 5. 放置：叠加到图片平面，或缩放到输出宽度
        if o.mapCorners is not None and strokes:
            _mapToCorners(strokes, st.previewCircles, o.mapCorners, w0, h0)
        elif strokes:
            _scaleToWidth(strokes, st.previewCircles, o)

        st.strokes = strokes
        st.contours = len(strokes)
        st.controlPoints = sum(len(s.fitPoints) for s in strokes)
        st.binaryPreview = _renderBinary(fld, w0, h0, iso, invert, traceMask)
    except Exception as ex:
        st.error = type(ex).__name__ + ": " + str(ex)
        traceback.print_exc()
    st.elapsedMs = (time.perf_counter() - t0) * 1000.0
    st.debug = logs
    return st


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


def _rgbToLab(rgb: np.ndarray) -> np.ndarray:
    """sRGB → CIELAB（D65），rgb (...,3) 0..255 → (...,3)。"""
    c = np.asarray(rgb, dtype=np.float64) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = lin[..., 0], lin[..., 1], lin[..., 2]
    x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883
    ft = lambda t: np.where(t > 0.008856, np.cbrt(t), 7.787 * t + 16.0 / 116)
    fx, fy, fz = ft(x), ft(y), ft(z)
    L = 116.0 * fy - 16.0
    A = 500.0 * (fx - fy)
    B = 200.0 * (fy - fz)
    return np.stack([L, A, B], axis=-1)


def _sqDist(lab: np.ndarray, keys: list) -> np.ndarray:
    """与一组 Lab 采样（非空）的最近色差平方，lab (...,3) → (...)。"""
    best = None
    for k in keys:
        d = ((lab - np.asarray(k, dtype=np.float64)) ** 2).sum(axis=-1)
        best = d if best is None else np.minimum(best, d)
    return best


def _colorClassifyField(rgb: np.ndarray, aKeys, bKeys, tolA, tolB) -> np.ndarray:
    """A/B 双色分类场：每像素取与两组采样的最近 Lab 色差 dA、dB。
    边界由 A/B 色差的等分线决定，等值线(128)恰好落在 dA == dB 处。"""
    lab = _rgbToLab(rgb)
    hasA = bool(aKeys)
    hasB = bool(bKeys)
    tolA = max(0.5, tolA)
    tolB = max(0.5, tolB)
    band = max(1.0, tolA + tolB)

    if not hasA and not hasB:
        return np.zeros(lab.shape[:2], dtype=np.float32)
    if not hasB:
        v = 255.0 * (1.0 - np.sqrt(_sqDist(lab, aKeys)) / (2 * tolA))
    elif not hasA:
        v = 255.0 * np.sqrt(_sqDist(lab, bKeys)) / (2 * tolB)
    else:
        dA = np.sqrt(_sqDist(lab, aKeys))
        dB = np.sqrt(_sqDist(lab, bKeys))
        fg = (dA <= tolA) & (dA <= dB)
        bg = (dB <= tolB) & (dB < dA)
        other = (dA > band) & (dB > band)
        s = np.clip((dB - dA) / band, -1.0, 1.0)
        v = np.where(fg, 255.0, np.where(bg | other, 0.0, 128.0 + 127.0 * s))
    return np.clip(v, 0.0, 255.0).astype(np.float32)


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


# ==================== 轮廓模式 ====================

def _traceContours(f: np.ndarray, iso: float, invert: bool) -> list:
    """marching squares 提取阈值等值线（亚像素插值），段方向统一为"内部在左"。
    返回闭合环列表 [ [(x,y),...] ]。"""
    inside = (f > iso) if invert else (f < iso)
    h, w = f.shape
    vTL = f[1:, :-1]
    vTR = f[1:, 1:]
    vBR = f[:-1, 1:]
    vBL = f[:-1, :-1]
    TL = inside[1:, :-1]
    TR = inside[1:, 1:]
    BR = inside[:-1, 1:]
    BL = inside[:-1, :-1]
    code = (TL.astype(np.uint8) | (TR.astype(np.uint8) << 1) |
            (BR.astype(np.uint8) << 2) | (BL.astype(np.uint8) << 3))
    ch, cw = code.shape
    cx = np.arange(cw, dtype=np.float64)[None, :].repeat(ch, 0)
    cy = np.arange(ch, dtype=np.float64)[:, None].repeat(cw, 1)

    def _cross(a, b) -> np.ndarray:
        with np.errstate(divide="ignore", invalid="ignore"):
            t = (iso - a) / (b - a)
        t = np.where(np.isfinite(t), t, 0.5)
        return np.clip(t, 1e-4, 1.0 - 1e-4)

    tL = _cross(vBL, vTL)   # 左边 (x, y+t)
    tR = _cross(vBR, vTR)   # 右边 (x+1, y+t)
    tB = _cross(vBL, vBR)   # 下边 (x+t, y)
    tT = _cross(vTL, vTR)   # 上边 (x+t, y+1)
    XL, YL = cx, cy + tL
    XR, YR = cx + 1.0, cy + tR
    XB, YB = cx + tB, cy
    XT, YT = cx + tT, cy + 1.0

    segs = []   # (x0, y0, x1, y1) 数组列表

    def emit(cond, x0, y0, x1, y1) -> None:
        m = code == cond
        if m.any():
            segs.append((x0[m], y0[m], x1[m], y1[m]))

    cenInside = None
    for cond in range(1, 15):
        if cond in (5, 10):
            continue
        m = code == cond
        if not m.any():
            continue
        if cond == 1:
            emit(cond, XL, YL, XT, YT)
        elif cond == 2:
            emit(cond, XT, YT, XR, YR)
        elif cond == 3:
            emit(cond, XL, YL, XR, YR)
        elif cond == 4:
            emit(cond, XR, YR, XB, YB)
        elif cond == 6:
            emit(cond, XT, YT, XB, YB)
        elif cond == 7:
            emit(cond, XL, YL, XB, YB)
        elif cond == 8:
            emit(cond, XB, YB, XL, YL)
        elif cond == 9:
            emit(cond, XB, YB, XT, YT)
        elif cond == 11:
            emit(cond, XB, YB, XR, YR)
        elif cond == 12:
            emit(cond, XR, YR, XL, YL)
        elif cond == 13:
            emit(cond, XR, YR, XT, YT)
        elif cond == 14:
            emit(cond, XT, YT, XL, YL)

    # 对角鞍点：按中心均值决定连接方式
    if ((code == 5) | (code == 10)).any():
        cenInside = (vTL + vTR + vBR + vBL) * 0.25
        cenInside = (cenInside > iso) if invert else (cenInside < iso)
        m5 = (code == 5) & cenInside
        if m5.any():
            segs.append((XR[m5], YR[m5], XT[m5], YT[m5]))
            segs.append((XL[m5], YL[m5], XB[m5], YB[m5]))
        m5 = (code == 5) & ~cenInside
        if m5.any():
            segs.append((XL[m5], YL[m5], XT[m5], YT[m5]))
            segs.append((XR[m5], YR[m5], XB[m5], YB[m5]))
        m10 = (code == 10) & cenInside
        if m10.any():
            segs.append((XT[m10], YT[m10], XL[m10], YL[m10]))
            segs.append((XB[m10], YB[m10], XR[m10], YR[m10]))
        m10 = (code == 10) & ~cenInside
        if m10.any():
            segs.append((XT[m10], YT[m10], XR[m10], YR[m10]))
            segs.append((XB[m10], YB[m10], XL[m10], YL[m10]))

    if not segs:
        return []

    ax = np.concatenate([s[0] for s in segs])
    ay = np.concatenate([s[1] for s in segs])
    bx = np.concatenate([s[2] for s in segs])
    by = np.concatenate([s[3] for s in segs])

    return _chainSegments(ax, ay, bx, by)


_Q = 4096.0   # 量化精度（1/4096 像素）


def _ptKey(x: float, y: float) -> int:
    return int(round(x * _Q)) * 1000000007 + int(round(y * _Q))


def _chainSegments(ax, ay, bx, by) -> list:
    """把等值线段按端点无向链接成闭合环（有向链接在鞍点处会接错产生乱飞弦线）。"""
    n = len(ax)
    keysA = np.empty(n, dtype=np.int64)
    keysB = np.empty(n, dtype=np.int64)
    incident: dict = {}
    for i in range(n):
        ka = _ptKey(ax[i], ay[i])
        kb = _ptKey(bx[i], by[i])
        keysA[i] = ka
        keysB[i] = kb
        incident.setdefault(ka, []).append(i)
        incident.setdefault(kb, []).append(i)

    used = [False] * n
    loops = []
    for si in range(n):
        if used[si]:
            continue
        used[si] = True
        startK = keysA[si]
        loop = [(ax[si], ay[si]), (bx[si], by[si])]
        curK = keysB[si]
        curSeg = si
        guard = n + 2
        while guard > 0:
            guard -= 1
            if curK == startK:
                break
            inc = incident.get(curK)
            if not inc:
                break
            nxt = -1
            for i in inc:
                if i != curSeg and not used[i]:
                    nxt = i
                    break
            if nxt < 0:
                break
            used[nxt] = True
            if keysA[nxt] == curK:
                far = (bx[nxt], by[nxt])
                curK = keysB[nxt]
            else:
                far = (ax[nxt], ay[nxt])
                curK = keysA[nxt]
            loop.append(far)
            curSeg = nxt
        if len(loop) >= 3:
            loops.append(loop)
    return loops


def _signedArea(p: list) -> float:
    a = 0.0
    n = len(p)
    for i in range(n):
        x1, y1 = p[i]
        x2, y2 = p[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return a / 2.0


def _polylineLength(p: list) -> float:
    if len(p) < 2:
        return 0.0
    arr = np.asarray(p, dtype=np.float64)
    return float(np.hypot(np.diff(arr[:, 0]), np.diff(arr[:, 1])).sum())


def _smoothPolyline(p: list, iterations: int, closed: bool) -> list:
    """折线平滑（1:2:1 加权移动平均）：开路径固定端点，闭路径环绕。"""
    n = len(p)
    if n < 3:
        return list(p)
    cur = list(p)
    for _ in range(iterations):
        nxt = []
        for i in range(n):
            if not closed and (i == 0 or i == n - 1):
                nxt.append(cur[i])
                continue
            a = cur[(i - 1) % n]
            b = cur[i]
            c = cur[(i + 1) % n]
            nxt.append(((a[0] + 2 * b[0] + c[0]) / 4.0,
                        (a[1] + 2 * b[1] + c[1]) / 4.0))
        cur = nxt
    return cur


def _extractOutlineStrokes(f: np.ndarray, iso: float, invert: bool, o,
                           maxX: float, maxY: float, Log) -> list:
    """轮廓提取：marching squares 亚像素等值线 → 面积过滤 → 简化 → 拟合。"""
    loops = _traceContours(f, iso, invert)
    if Log is not None:
        tot = sum(len(lp) for lp in loops)
        Log(f"轮廓: 环={len(loops)} 总点={tot}")
    out: list = []
    for loop in loops:
        loop = [(x - 1.0, y - 1.0) for x, y in loop]   # 去掉 1px 外扩边框
        if abs(_signedArea(loop)) < max(0.2, o.minAreaPx):
            continue
        poly = _simplifyClosed(loop, o.simplifyTolPx)
        if len(poly) < 3:
            continue
        stroke = _fitContour(poly, o.fitTolPx, o.cornerAngleDeg, maxX, maxY)
        if stroke is not None:
            out.append(stroke)
    return out


# ==================== 折线简化与转角检测 ====================

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


def _findCorners(p: list, angleDeg: float, k: int, openPath: bool) -> list:
    """转角检测：沿折线隔 k 点看方向变化，超过阈值记为尖角；
    相距太近（<3 点）的候选只留转角最大者。"""
    n = len(p)
    cands = []
    rng = range(k, n - k) if openPath else range(n)
    for i in rng:
        a = p[i - k]
        b = p[i]
        c = p[(i + k) % n]
        v1 = (b[0] - a[0], b[1] - a[1])
        v2 = (c[0] - b[0], c[1] - b[1])
        l1 = math.hypot(*v1)
        l2 = math.hypot(*v2)
        if l1 < 1e-9 or l2 < 1e-9:
            continue
        cos = (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2)
        cos = min(1.0, max(-1.0, cos))
        ang = math.degrees(math.acos(cos))
        if ang >= angleDeg:
            cands.append((i, ang))
    cands.sort(key=lambda t: -t[1])
    selected: list = []
    minGap = 3
    for idx, _ in cands:
        close = False
        for s in selected:
            d = abs(idx - s)
            if not openPath:
                d = min(d, n - d)
            if d < minGap:
                close = True
                break
        if not close:
            selected.append(idx)
    selected.sort()
    return selected


# ==================== 贝塞尔拟合与输出 ====================

def _lerp(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def _norm(v):
    l = math.hypot(*v)
    if l < 1e-12:
        return (1.0, 0.0)
    return (v[0] / l, v[1] / l)


def _fitContour(poly: list, fitTol: float, cornerAngleDeg: float,
                maxX: float, maxY: float):
    """闭合折线拟合为单条输出：尖角处硬转折，平滑段用三次贝塞尔链。"""
    if len(poly) < 3:
        return None
    corners = _findCorners(poly, cornerAngleDeg, 2, openPath=False)
    segs: list = []
    if not corners:
        # 无尖角：整环平滑拟合，接缝两侧行进方向取平均，避免起点折痕
        pts = list(poly) + [poly[0]]
        dA = _norm((poly[1][0] - poly[0][0], poly[1][1] - poly[0][1]))
        dB = _norm((poly[0][0] - poly[-1][0], poly[0][1] - poly[-1][1]))
        tAvg = _norm((dA[0] - dB[0], dA[1] - dB[1]))
        _fitCubic(pts, tAvg, tAvg, fitTol, segs)
    elif len(corners) == 1:
        # 唯一尖角（水滴形等）：整环从尖角出发闭合拟合
        i0 = corners[0]
        chain = [poly[(i0 + k) % len(poly)] for k in range(len(poly))]
        chain.append(chain[0])
        tL = _norm((chain[1][0] - chain[0][0], chain[1][1] - chain[0][1]))
        tR = _norm((chain[-2][0] - chain[-1][0], chain[-2][1] - chain[-1][1]))
        _fitCubic(chain, tL, tR, fitTol, segs)
    else:
        for ci in range(len(corners)):
            i0 = corners[ci]
            i1 = corners[(ci + 1) % len(corners)]
            chain = _takeWrap(poly, i0, i1)
            if len(chain) < 2:
                continue
            if len(chain) == 2:
                segs.append((chain[0], _lerp(chain[0], chain[1], 1 / 3),
                             _lerp(chain[0], chain[1], 2 / 3), chain[1]))
            else:
                tL = _norm((chain[1][0] - chain[0][0], chain[1][1] - chain[0][1]))
                tR = _norm((chain[-2][0] - chain[-1][0], chain[-2][1] - chain[-1][1]))
                _fitCubic(chain, tL, tR, fitTol, segs)

    segs = [s for s in segs if not (
        math.dist(s[0], s[3]) < 1e-9 and math.dist(s[0], s[1]) < 1e-9 and
        math.dist(s[0], s[2]) < 1e-9)]
    if not segs or len(segs) > 4000:
        return _fallbackClosed(poly, maxX, maxY)
    segs = _clampSegs(segs, maxX, maxY)
    # 拟合失控检测：曲线偏离源折线过远（乱飞）时退回折线
    if _curveDeviates(segs, poly, fitTol * 2 + 1.0, True):
        return _fallbackClosed(poly, maxX, maxY)
    return _strokesFromSegs(segs, poly, True)


def _fitOpenPath(path: list, fitTol: float, cornerAngleDeg: float,
                 maxX: float, maxY: float):
    """开放路径（骨架中心线）拟合：尖角处硬转折，平滑段用贝塞尔链。"""
    if path is None or len(path) < 2:
        return None
    segs: list = []
    if len(path) == 2:
        segs.append((path[0], _lerp(path[0], path[1], 1 / 3),
                     _lerp(path[0], path[1], 2 / 3), path[1]))
    else:
        corners = _findCorners(path, cornerAngleDeg, 2, openPath=True)
        pieces: list = []
        if not corners:
            pieces.append(path)
        else:
            prevIdx = 0
            for c in corners:
                if c > prevIdx:
                    pieces.append(path[prevIdx:c + 1])
                prevIdx = c
            if prevIdx < len(path) - 1:
                pieces.append(path[prevIdx:])
        for piece in pieces:
            if len(piece) < 2:
                continue
            if len(piece) == 2:
                segs.append((piece[0], _lerp(piece[0], piece[1], 1 / 3),
                             _lerp(piece[0], piece[1], 2 / 3), piece[1]))
            else:
                tL = _norm((piece[1][0] - piece[0][0], piece[1][1] - piece[0][1]))
                tR = _norm((piece[-2][0] - piece[-1][0], piece[-2][1] - piece[-1][1]))
                _fitCubic(piece, tL, tR, fitTol, segs)

    segs = [s for s in segs if not (
        math.dist(s[0], s[3]) < 1e-9 and math.dist(s[0], s[1]) < 1e-9 and
        math.dist(s[0], s[2]) < 1e-9)]
    if not segs or len(segs) > 4000:
        return _fallbackOpen(path, maxX, maxY)
    segs = _clampSegs(segs, maxX, maxY)
    if _curveDeviates(segs, path, fitTol * 2 + 1.0, False):
        return _fallbackOpen(path, maxX, maxY)
    return _strokesFromSegs(segs, path, False)


def _clampSegs(segs: list, maxX: float, maxY: float) -> list:
    """把贝塞尔段控制点夹进像素范围（曲线必在控制点凸包内，消除边缘过冲）。"""
    out = []
    for p0, p1, p2, p3 in segs:
        out.append((_clampPt(p0, maxX, maxY), _clampPt(p1, maxX, maxY),
                    _clampPt(p2, maxX, maxY), _clampPt(p3, maxX, maxY)))
    return out


def _clampPt(p, mx, my):
    return (min(max(p[0], 0.0), mx), min(max(p[1], 0.0), my))


def _strokesFromSegs(segs: list, fit: list, closed: bool) -> TraceStroke:
    """贝塞尔链 → 输出折线（每段细分采样）+ 编辑锚点（简化折线）。"""
    SUBDIV = 12
    pts: list = [segs[0][0]]
    for p0, p1, p2, p3 in segs:
        for i in range(1, SUBDIV + 1):
            t = i / SUBDIV
            mt = 1.0 - t
            a = mt * mt * mt
            b = 3 * t * mt * mt
            c = 3 * t * t * mt
            e = t * t * t
            pts.append((a * p0[0] + b * p1[0] + c * p2[0] + e * p3[0],
                        a * p0[1] + b * p1[1] + c * p2[1] + e * p3[1]))
    fitPts = list(fit)
    if closed:
        if pts and math.dist(pts[0], pts[-1]) > 1e-9:
            pts.append(pts[0])
        if fitPts and math.dist(fitPts[0], fitPts[-1]) > 1e-9:
            fitPts.append(fitPts[0])
    return TraceStroke(points=pts, fitPoints=fitPts, closed=closed)


def _fallbackClosed(poly: list, maxX: float, maxY: float):
    if poly is None or len(poly) < 3:
        return None
    pts = [(min(max(x, 0.0), maxX), min(max(y, 0.0), maxY)) for x, y in poly]
    pts = pts + [pts[0]]
    fit = list(pts)
    return TraceStroke(points=pts, fitPoints=fit, closed=True)


def _fallbackOpen(path: list, maxX: float, maxY: float):
    if path is None or len(path) < 2:
        return None
    pts = [(min(max(x, 0.0), maxX), min(max(y, 0.0), maxY)) for x, y in path]
    return TraceStroke(points=list(pts), fitPoints=list(pts), closed=False)


def _curveDeviates(segs: list, poly: list, maxDev: float, closed: bool) -> bool:
    """拟合失控检测：采样曲线，任一点距源折线超过 maxDev 即判定"乱飞"。"""
    pts: list = [segs[0][0]]
    for p0, p1, p2, p3 in segs:
        for i in range(1, 9):
            t = i / 8.0
            mt = 1.0 - t
            pts.append((mt**3 * p0[0] + 3 * t * mt * mt * p1[0] +
                        3 * t * t * mt * p2[0] + t**3 * p3[0],
                        mt**3 * p0[1] + 3 * t * mt * mt * p1[1] +
                        3 * t * t * mt * p2[1] + t**3 * p3[1]))
    n = len(poly)
    tol2 = maxDev * maxDev
    last = n if closed else n - 1
    for q in pts:
        best = float("inf")
        for j in range(last):
            a = poly[j]
            b = poly[(j + 1) % n]
            d = _distToSegment2(q[0], q[1], a[0], a[1], b[0], b[1])
            if d < best:
                best = d
                if best <= tol2:
                    break
        if best > tol2:
            return True
    return False


def _fitCubic(p: list, tL, tR, error: float, segs: list) -> None:
    """递归拟合：先整体生成贝塞尔，误差过大时按最大误差点分割
    （Schneider, Graphics Gems；用显式栈实现保证段序）。"""
    stack = [(p, tL, tR)]
    while stack:
        p, tL, tR = stack.pop()
        n = len(p)
        if n < 2:
            continue
        if n == 2:
            dist = math.dist(p[0], p[1]) / 3.0
            segs.append((p[0], (p[0][0] + tL[0] * dist, p[0][1] + tL[1] * dist),
                         (p[1][0] + tR[0] * dist, p[1][1] + tR[1] * dist), p[1]))
            continue
        u = _chordLengthParameterize(p)
        bez = _generateBezier(p, u, tL, tR)
        maxErr, idx = _computeMaxError(p, bez, u)
        if maxErr < error:
            segs.append(bez)
            continue
        if maxErr < error * error:
            ok = False
            for _ in range(4):
                u = _reparameterize(p, bez, u)
                bez = _generateBezier(p, u, tL, tR)
                maxErr, idx = _computeMaxError(p, bez, u)
                if maxErr < error:
                    segs.append(bez)
                    ok = True
                    break
            if ok:
                continue
        split = idx
        if split < 1 or split > n - 2:
            split = n // 2
        tC = _norm((p[split - 1][0] - p[split + 1][0],
                    p[split - 1][1] - p[split + 1][1]))
        # 先 push 右段再 push 左段：栈处理时左段先完成，保持段序
        stack.append((p[split:], (-tC[0], -tC[1]), tR))
        stack.append((p[:split + 1], tL, tC))


def _chordLengthParameterize(p: list) -> list:
    n = len(p)
    u = [0.0] * n
    for i in range(1, n):
        u[i] = u[i - 1] + math.dist(p[i - 1], p[i])
    total = u[n - 1]
    if total < 1e-12:
        for i in range(n):
            u[i] = i / (n - 1)
    else:
        u = [v / total for v in u]
    return u


def _generateBezier(p: list, u: list, tL, tR):
    """最小二乘生成过两端点、端点切向给定的三次贝塞尔。"""
    n = len(p)
    p0, p3 = p[0], p[n - 1]
    c00 = c01 = c11 = x0 = x1 = 0.0
    for i in range(n):
        t = u[i]
        mt = 1.0 - t
        b0 = mt * mt * mt
        b1 = 3 * t * mt * mt
        b2 = 3 * t * t * mt
        b3 = t * t * t
        a0 = (tL[0] * b1, tL[1] * b1)
        a1 = (tR[0] * b2, tR[1] * b2)
        c00 += a0[0] * a0[0] + a0[1] * a0[1]
        c01 += a0[0] * a1[0] + a0[1] * a1[1]
        c11 += a1[0] * a1[0] + a1[1] * a1[1]
        tmpx = p[i][0] - (p0[0] * (b0 + b1) + p3[0] * (b2 + b3))
        tmpy = p[i][1] - (p0[1] * (b0 + b1) + p3[1] * (b2 + b3))
        x0 += a0[0] * tmpx + a0[1] * tmpy
        x1 += a1[0] * tmpx + a1[1] * tmpy
    det = c00 * c11 - c01 * c01
    segLen = math.dist(p0, p3)
    eps = 1e-6 * segLen + 1e-12
    alphaL = alphaR = 0.0
    if abs(det) > 1e-12:
        alphaL = (x0 * c11 - x1 * c01) / det
        alphaR = (c00 * x1 - c01 * x0) / det
    if alphaL < eps or alphaR < eps:
        alphaL = alphaR = segLen / 3.0
    # 控制点臂长上限：病态参数化下最小二乘可能算出超大 alpha，导致曲线"乱飞"
    cap = segLen * 4 + 1e-9
    alphaL = min(alphaL, cap)
    alphaR = min(alphaR, cap)
    return (p0, (p0[0] + tL[0] * alphaL, p0[1] + tL[1] * alphaL),
            (p3[0] + tR[0] * alphaR, p3[1] + tR[1] * alphaR), p3)


def _bezierAt(b, t):
    mt = 1.0 - t
    a = mt * mt * mt
    c = 3 * t * mt * mt
    d = 3 * t * t * mt
    e = t * t * t
    return (a * b[0][0] + c * b[1][0] + d * b[2][0] + e * b[3][0],
            a * b[0][1] + c * b[1][1] + d * b[2][1] + e * b[3][1])


def _computeMaxError(p: list, b, u: list):
    maxD2 = -1.0
    idx = len(p) // 2
    for i in range(len(p)):
        q = _bezierAt(b, u[i])
        d2 = (q[0] - p[i][0]) ** 2 + (q[1] - p[i][1]) ** 2
        if d2 > maxD2:
            maxD2 = d2
            idx = i
    return math.sqrt(maxD2), idx


def _reparameterize(p: list, b, u: list) -> list:
    return [_newtonRaphson(b, p[i], u[i]) for i in range(len(u))]


def _newtonRaphson(b, pt, t: float) -> float:
    q = _bezierAt(b, t)
    qx, qy = q[0] - pt[0], q[1] - pt[1]
    mt = 1.0 - t
    # 一阶导
    d1x = (3 * mt * mt * (b[1][0] - b[0][0]) + 6 * t * mt * (b[2][0] - b[1][0]) +
           3 * t * t * (b[3][0] - b[2][0]))
    d1y = (3 * mt * mt * (b[1][1] - b[0][1]) + 6 * t * mt * (b[2][1] - b[1][1]) +
           3 * t * t * (b[3][1] - b[2][1]))
    # 二阶导
    d2x = (6 * mt * (b[2][0] - 2 * b[1][0] + b[0][0]) +
           6 * t * (b[3][0] - 2 * b[2][0] + b[1][0]))
    d2y = (6 * mt * (b[2][1] - 2 * b[1][1] + b[0][1]) +
           6 * t * (b[3][1] - 2 * b[2][1] + b[1][1]))
    num = qx * d1x + qy * d1y
    den = d1x * d1x + d1y * d1y + qx * d2x + qy * d2y
    if abs(den) < 1e-12:
        return t
    nt = t - num / den
    return min(1.0, max(0.0, nt))


# ==================== 中心线（骨架）模式 ====================

_NBR_D = ((0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1), (-1, 0), (-1, 1))


def _traceCenterline(fld, vals, w0, h0, iso, invert, o, Log, st):
    """分层管线：掩码 → 连通域去噪 → 细化 → 骨架走线 → 粗细分流 → 亚像素居中。"""
    h, w = fld.shape
    # [1] 掩码 = 模糊场 ∪ 原始场（发丝线保护）
    inside = (fld > iso) if invert else (fld < iso)
    mask = inside.copy()
    rawIn = (vals > iso) if invert else (vals < iso)
    recover = (~mask[1:-1, 1:-1]) & rawIn
    mask[1:-1, 1:-1] |= recover
    Log(f"单线: 发丝线找回={int(recover.sum())}px")

    labels, compCount = _labelComponents(mask, max(1.0, o.minAreaPx))
    dt = _distanceTransform(mask)          # 黑区像素到最近白边的距离
    compCnt = np.bincount(labels.ravel(), minlength=compCount + 1)
    compSum = np.bincount(labels.ravel(), weights=dt.ravel(),
                          minlength=compCount + 1)
    nb = int(compCnt[1:].sum())
    meanWidth = 2.0 * float(compSum[1:].sum()) / nb if nb > 0 else 0.0
    Log(f"单线: 场内像素={nb} 连通域={compCount} 全图平均线宽={meanWidth:.1f}px "
        f"BlobWidthPx={o.blobWidthPx:.1f}")

    # [4] 性能守卫：平均线宽超阈值且面积巨大的实心块直接走轮廓
    skel = mask.copy()
    blob = np.zeros_like(mask)
    blobPixels = 0
    solidGuardArea = 40000
    guardComps = 0
    if o.blobWidthPx > 0.5:
        widths = np.zeros(compCount + 1, dtype=np.float64)
        nz = compCnt > 0
        widths[nz] = 2.0 * compSum[nz] / compCnt[nz]
        areas = np.bincount(labels.ravel(), minlength=compCount + 1)
        for c in range(1, compCount + 1):
            if compCnt[c] == 0 or widths[c] < o.blobWidthPx or \
                    areas[c] < solidGuardArea:
                continue
            guardComps += 1
            sel = labels == c
            blob |= sel
            skel &= ~sel
            blobPixels += int(sel.sum())
    Log(f"单线: 守卫整块={guardComps} 像素={blobPixels}")

    # [2] 全图细化 + 交点归一化 + 端点合并 + 剪毛刺
    _zhangSuenThin(skel)
    _normalizeJunctions(skel)
    _mergeStrokeEndsSameComponent(skel, labels)
    _pruneSpurs(skel, int(round(max(0.0, o.pruneLenPx))))

    paths = _skeletonPaths(skel, w, h)
    paths = _mergePathsThroughJunctions(paths, labels, w, h, 0.35)
    openN = sum(1 for p in paths if not p["closed"])
    closedN = len(paths) - openN
    totPts = sum(len(p["points"]) for p in paths)
    maxPts = max((len(p["points"]) for p in paths), default=0)
    Log(f"单线: 骨架路径 开放={openN} 闭合={closedN} 总点数={totPts} 最长={maxPts}")

    minLength = max(1.0, o.pruneLenPx * 0.25)

    # [3] 支路级粗细分类：粗笔画走轮廓重建，细支路出中心线
    thinPaths: list = []
    if o.blobWidthPx > 0.5:
        thickN = 0
        for p in paths:
            medDt = _medianInscribedRadius(p, dt)
            if medDt < 0:
                continue
            if 2.0 * medDt >= o.blobWidthPx:
                thickN += 1
                _stampInscribedDisks(p, dt, blob)
            else:
                thinPaths.append(p)
        blobPixels = int(blob.sum())
        Log(f"单线: 支路分类 粗={thickN} 细={len(thinPaths)} 粗块像素={blobPixels}")
    else:
        thinPaths = list(paths)

    # 滚圆直径过滤（手动>0 时）：球滚不进去的细笔画整条不出线
    if o.ballDiaPx > 0.5:
        minR = o.ballDiaPx / 2.0 - max(1.2, o.ballDiaPx * 0.1)
        beforeF = len(thinPaths)
        thinPaths = [p for p in thinPaths
                     if _medianInscribedRadius(p, dt) >= minR]
        Log(f"单线: 宽度过滤(D≥{o.ballDiaPx:.0f}) 保留 {len(thinPaths)}/{beforeF} 条笔画")

    # 亚像素重定位：骨架点沿切线法向移到模糊场两侧等值线交点的中点
    refineDist = max(16.0, o.blobWidthPx)
    for sp in thinPaths:
        sp["points"] = _refineCenterline(sp["points"], sp["closed"], fld,
                                         iso, invert, refineDist)

    strokes: list = []
    for sp in thinPaths:
        pts = sp["points"]
        if sp["closed"]:
            if len(pts) >= 6:
                poly = _simplifyClosed(pts, o.simplifyTolPx)
                if len(poly) >= 3 and abs(_signedArea(poly)) >= max(0.2, o.minAreaPx):
                    poly = _smoothPolyline(poly, 2, True)
                    if len(poly) >= 3:
                        crv = _fitContour(poly, o.fitTolPx, o.cornerAngleDeg,
                                          w0 - 1, h0 - 1)
                        if crv is not None:
                            strokes.append(crv)
        elif len(pts) >= 2:
            poly = (_douglasPeucker(pts, max(0.5, o.simplifyTolPx))
                    if len(pts) > 2 else list(pts))
            if _polylineLength(poly) >= minLength:
                poly = _smoothPolyline(poly, 2, False)
                if len(poly) >= 2:
                    crv = _fitOpenPath(poly, o.fitTolPx, o.cornerAngleDeg,
                                       w0 - 1, h0 - 1)
                    if crv is not None:
                        strokes.append(crv)
    Log(f"单线: 骨架出线={len(strokes)}")

    # 滚圆示意（仅预览）：两条最长单线上各取一点画该处最大内切圆
    try:
        longs = sorted(
            (s for s in strokes if _polylineLength(s.points) > 30),
            key=lambda s: -_polylineLength(s.points))[:2]
        for s in longs:
            target = _polylineLength(s.points) * 0.45
            acc = 0.0
            pt = s.points[0]
            for i in range(1, len(s.points)):
                a, b = s.points[i - 1], s.points[i]
                d = math.dist(a, b)
                if acc + d >= target:
                    t = (target - acc) / d if d > 1e-9 else 0.0
                    pt = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
                    break
                acc += d
            ix = int(round(pt[0])) + 1
            iy = int(round(pt[1])) + 1
            if ix < 1 or iy < 1 or ix >= w - 1 or iy >= h - 1:
                continue
            r = float(dt[iy, ix])
            if r < 1.0:
                continue
            st.previewCircles.append((pt[0], pt[1], r))
    except Exception:
        pass

    # 粗块/实心块 → 区域轮廓（只保留粗块区为"内部"，其余压成外部）
    if blobPixels > 0:
        outside = iso - 1.0 if invert else iso + 1.0
        blobField = fld.copy()
        insideHere = (fld > iso) if invert else (fld < iso)
        blobField[insideHere & ~blob] = outside
        blobStrokes = _extractOutlineStrokes(blobField, iso, invert, o,
                                             w0 - 1, h0 - 1, Log)
        strokes.extend(blobStrokes)
        Log(f"单线: 粗块轮廓已并入，合计曲线={len(strokes)}")

    return strokes, mask


def _labelComponents(m: np.ndarray, minArea: float):
    """8 连通连通域标记：返回 (labels, count)；删除小于 minArea 的构件（去噪点）。"""
    try:
        from scipy import ndimage
        struct = np.ones((3, 3), dtype=bool)
        labels, count = ndimage.label(m, structure=struct)
        if count and minArea > 1.0:
            areas = np.bincount(labels.ravel(), minlength=count + 1)
            small = np.nonzero((areas > 0) & (areas < minArea))[0]
            if small.size:
                remove = np.isin(labels, small)
                m[remove] = False
                labels[remove] = 0
        return labels.astype(np.int32), int(count)
    except ImportError:
        return _labelComponentsBFS(m, minArea)


def _labelComponentsBFS(m: np.ndarray, minArea: float):
    """无 scipy 时的洪泛标记兜底（较慢）。"""
    h, w = m.shape
    labels = np.zeros((h, w), dtype=np.int32)
    cur = 0
    ys, xs = np.nonzero(m)
    for sy, sx in zip(ys.tolist(), xs.tolist()):
        if labels[sy, sx] != 0:
            continue
        cur += 1
        members = []
        stack = [(sy, sx)]
        labels[sy, sx] = cur
        while stack:
            y, x = stack.pop()
            members.append((y, x))
            for dy in (-1, 0, 1):
                ny = y + dy
                if ny < 0 or ny >= h:
                    continue
                for dx in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    nx = x + dx
                    if nx < 0 or nx >= w:
                        continue
                    if m[ny, nx] and labels[ny, nx] == 0:
                        labels[ny, nx] = cur
                        stack.append((ny, nx))
        if len(members) < minArea:
            for y, x in members:
                m[y, x] = False
                labels[y, x] = 0
            cur -= 1
    return labels, cur


def _distanceTransform(m: np.ndarray) -> np.ndarray:
    """像素到最近背景的距离（像素）。优先 scipy 精确 EDT，否则迭代倒角传播。"""
    try:
        from scipy import ndimage
        return ndimage.distance_transform_edt(m).astype(np.float32)
    except ImportError:
        h, w = m.shape
        big = np.float32(w + h)
        dt = np.where(m, big, np.float32(0.0)).astype(np.float32)
        D1, D2 = np.float32(1.0), np.float32(1.4142)
        while True:
            prev = dt
            acc = dt.copy()
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    wgt = D2 if (dx != 0 and dy != 0) else D1
                    sh = np.full((h, w), big, dtype=np.float32)
                    ys0, ys1 = max(0, dy), h + min(0, dy)
                    xs0, xs1 = max(0, dx), w + min(0, dx)
                    sh[ys0:ys1, xs0:xs1] = prev[ys0 - dy:ys1 - dy,
                                                xs0 - dx:xs1 - dx] + wgt
                    np.minimum(acc, sh, out=acc)
            dt = acc
            if np.array_equal(dt, prev):
                break
        return dt


def _zhangSuenThin(m: np.ndarray) -> None:
    """Zhang-Suen 细化（numpy 向量化）：把线条区域腐蚀成 1 像素宽骨架。"""
    if not m.any():
        return
    while True:
        changed = False
        for phase in (0, 1):
            mm = m.astype(np.uint8)
            p2 = mm[2:, 1:-1]   # 北 (y+1, x)
            p3 = mm[2:, 2:]     # 东北
            p4 = mm[1:-1, 2:]   # 东
            p5 = mm[:-2, 2:]    # 东南
            p6 = mm[:-2, 1:-1]  # 南
            p7 = mm[:-2, :-2]   # 西南
            p8 = mm[1:-1, :-2]  # 西
            p9 = mm[2:, :-2]    # 西北
            b = (p2 + p3 + p4 + p5 + p6 + p7 + p8 + p9)
            seq = (p9, p2, p3, p4, p5, p6, p7, p8, p9)
            a = np.zeros_like(b)
            for k in range(8):
                a += ((seq[k] == 0) & (seq[k + 1] == 1)).astype(np.uint8)
            cond = (m[1:-1, 1:-1] & (b >= 2) & (b <= 6) & (a == 1))
            if phase == 0:
                cond &= ~((p2 == 1) & (p4 == 1) & (p6 == 1))
                cond &= ~((p4 == 1) & (p6 == 1) & (p8 == 1))
            else:
                cond &= ~((p2 == 1) & (p4 == 1) & (p8 == 1))
                cond &= ~((p2 == 1) & (p6 == 1) & (p8 == 1))
            if cond.any():
                changed = True
                m[1:-1, 1:-1][cond] = False
        if not changed:
            return


def _branchCountSparse(pix: set, w: int, h: int, y: int, x: int) -> int:
    """像素引出的分支数：8 邻域环上 0→1 跳变次数（稀疏版）。"""
    vals = []
    for dx, dy in _NBR_D:
        nx, ny = x + dx, y + dy
        vals.append(1 if (0 <= nx < w and 0 <= ny < h and (ny * w + nx) in pix) else 0)
    t = 0
    for k in range(8):
        if not vals[k] and vals[(k + 1) % 8]:
            t += 1
    return t


def _normalizeJunctions(m: np.ndarray) -> None:
    """交点归一化：把细化残留的 2×2 黑块收敛成单像素（迭代到稳定）。"""
    h, w = m.shape
    while True:
        blocks = m[1:, 1:] & m[1:, :-1] & m[:-1, 1:] & m[:-1, :-1]
        cand = np.zeros((h, w), dtype=bool)
        cand[:-1, :-1] |= blocks
        cand[:-1, 1:] |= blocks
        cand[1:, :-1] |= blocks
        cand[1:, 1:] |= blocks
        cand &= m
        cand[0, :] = cand[-1, :] = False
        cand[:, 0] = cand[:, -1] = False
        ys, xs = np.nonzero(cand)
        if ys.size == 0:
            return
        pix = set(np.flatnonzero(m).tolist())
        changed = False
        for y, x in zip(ys.tolist(), xs.tolist()):
            i = y * w + x
            if i not in pix:
                continue
            # 用数组判定 2×2 块归属（m 已随删除变化）
            if not ((m[y, x + 1] and m[y + 1, x] and m[y + 1, x + 1]) or
                    (m[y, x - 1] and m[y + 1, x] and m[y + 1, x - 1]) or
                    (m[y, x + 1] and m[y - 1, x] and m[y - 1, x + 1]) or
                    (m[y, x - 1] and m[y - 1, x] and m[y - 1, x - 1])):
                continue
            if _branchCountSparse(pix, w, h, y, x) != 1:
                continue
            m[y, x] = False
            pix.discard(i)
            changed = True
        if not changed:
            return


def _mergeStrokeEndsSameComponent(skel: np.ndarray, labels: np.ndarray) -> None:
    """端点合并：两笔画端点像素 4-邻域相邻且同连通分量时填桥接像素。"""
    h, w = skel.shape
    mm = skel.astype(np.uint8)
    nbr = np.zeros((h, w), dtype=np.uint8)
    for dx, dy in _NBR_D:
        sh = np.zeros((h, w), dtype=np.uint8)
        ys0, ys1 = max(0, dy), h + min(0, dy)
        xs0, xs1 = max(0, dx), w + min(0, dx)
        sh[ys0:ys1, xs0:xs1] = mm[ys0 - dy:ys1 - dy, xs0 - dx:xs1 - dx]
        nbr += sh
    ends = np.nonzero(skel & (nbr == 1))
    ey, ex = ends[0].tolist(), ends[1].tolist()
    n = len(ey)
    if n < 2:
        return
    for a in range(n):
        ya, xa = ey[a], ex[a]
        la = labels[ya, xa]
        for b in range(a + 1, n):
            yb, xb = ey[b], ex[b]
            if labels[yb, xb] != la:
                continue
            d = abs(xa - xb) + abs(ya - yb)
            if d != 1 and d != 2:
                continue
            mx, my = (xa + xb) // 2, (ya + yb) // 2
            skel[my, mx] = True
            skel[yb, xb] = True


def _pruneSpurs(m: np.ndarray, maxLen: int) -> None:
    """剪掉骨架上的毛刺：从端点出发、长度 ≤ maxLen 且连到"真交叉点"的分支删除
    （单次快照遍历，不做级联删除）。"""
    if maxLen <= 0 or not m.any():
        return
    h, w = m.shape
    pix = set(np.flatnonzero(m).tolist())

    def branch(i: int) -> int:
        return _branchCountSparse(pix, w, h, i // w, i % w)

    branchMap = {i: branch(i) for i in pix}
    toDelete: list = []
    ys, xs = np.nonzero(m[1:-1, 1:-1])
    for y, x in zip((ys + 1).tolist(), (xs + 1).tolist()):
        i = y * w + x
        if branchMap.get(i, 0) != 1:
            continue
        chain = []
        prev = -1
        cur = i
        kind = 0
        while cur >= 0:
            chain.append(cur)
            if len(chain) > maxLen + 1:
                kind = 0
                break
            b = branchMap.get(cur, 0)
            if len(chain) > 1 and b != 2:
                kind = 1 if b >= 3 else 2
                break
            nxt = -1
            cy, cx2 = cur // w, cur % w
            for dx, dy in _NBR_D:
                nx, ny = cx2 + dx, cy + dy
                if nx < 0 or ny < 0 or nx >= w or ny >= h:
                    continue
                q = ny * w + nx
                if q != prev and q in pix:
                    nxt = q
                    break
            prev = cur
            cur = nxt
        if kind == 1 and len(chain) > 1:
            for q in chain[:-1]:
                toDelete.append(q)
    for q in toDelete:
        m[q // w, q % w] = False


def _skeletonPaths(m: np.ndarray, w: int, h: int) -> list:
    """沿骨架走线：节点（分支数≠2）之间连成开放折线，无节点的环连成闭合折线。
    走线按方向连续性选点（直行 > 斜行 > 转折）。
    输出点为图像坐标（场坐标 -1，外扩边框）；m 为 (h, w) 场尺寸掩码。"""
    pix = set(np.flatnonzero(m).tolist())
    deg = {i: _branchCountSparse(pix, w, h, i // w, i % w) for i in pix}
    visited = set()
    paths: list = []

    def P(i):
        return (float(i % w - 1), float(i // w - 1))

    def stepForward(prev, cur):
        bdx = (cur % w - prev % w) if prev >= 0 else 0.0
        bdy = (cur // w - prev // w) if prev >= 0 else 0.0
        blen = math.hypot(bdx, bdy)
        if blen < 1e-9:
            bdx = bdy = 0.0
            blen = 1.0
        cy, cx2 = cur // w, cur % w
        best = -1e-9
        nxt = -1
        for dx, dy in _NBR_D:
            nx, ny = cx2 + dx, cy + dy
            if nx < 0 or ny < 0 or nx >= w or ny >= h:
                continue
            q = ny * w + nx
            if q == prev or q not in pix or q in visited:
                continue
            score = (dx * bdx + dy * bdy) / (blen * math.hypot(dx, dy))
            if score > best:
                best = score
                nxt = q
        return nxt

    # 1) 节点 → 节点/端点 的开放路径（相邻两节点间的短路径按节点对只记一次）
    seenPairs = set()
    paths: list = []
    for i in list(pix):
        if deg[i] == 2 or deg[i] == 0:
            continue
        cy, cx2 = i // w, i % w
        for dx, dy in _NBR_D:
            nx, ny = cx2 + dx, cy + dy
            if nx < 0 or ny < 0 or nx >= w or ny >= h:
                continue
            nb = ny * w + nx
            if nb not in pix or nb in visited:
                continue
            ek = (min(i, nb), max(i, nb))
            if ek in seenPairs:
                continue
            seenPairs.add(ek)
            pts = [P(i)]
            prev, cur = i, nb
            while True:
                pts.append(P(cur))
                if deg.get(cur, 0) != 2:
                    break
                visited.add(cur)
                nxt = stepForward(prev, cur)
                if nxt < 0:
                    break
                prev, cur = cur, nxt
            paths.append({"points": pts, "closed": False})

    # 2) 无节点的闭合环（如圆圈笔画）
    for i in list(pix):
        if i in visited or deg[i] != 2:
            continue
        pts = []
        prev = -1
        cur = i
        while cur >= 0:
            pts.append(P(cur))
            visited.add(cur)
            nxt = stepForward(prev, cur)
            if nxt < 0:
                break
            prev, cur = cur, nxt
            if cur == i:
                break
        if len(pts) >= 4:
            paths.append({"points": pts, "closed": True})
    return paths


class _StrokeEnd:
    __slots__ = ("stroke", "end", "pos", "dir")

    def __init__(self, stroke, end, pos, d):
        self.stroke = stroke
        self.end = end
        self.pos = pos
        self.dir = d


def _makeEnd(stroke, end, pts: list) -> _StrokeEnd:
    if end == 0:
        return _StrokeEnd(stroke, 0, pts[0], _norm((pts[0][0] - pts[1][0],
                                                    pts[0][1] - pts[1][1])))
    n = len(pts)
    return _StrokeEnd(stroke, 1, pts[n - 1],
                      _norm((pts[n - 1][0] - pts[n - 2][0],
                             pts[n - 1][1] - pts[n - 2][1])))


def _reverseList(p: list) -> list:
    return list(reversed(p))


def _mergePathsThroughJunctions(paths: list, labels: np.ndarray, w: int, h: int,
                                minDot: float) -> list:
    """交点处按方向连续性把骨架路径两两续接成一条（十字/丁字的对臂合成单线）。"""
    n = len(paths)
    pts = [p["points"] for p in paths]
    alive = [True] * n
    isClosed = [p["closed"] for p in paths]

    ends: list = []
    for i in range(n):
        if not isClosed[i] and len(pts[i]) >= 2:
            ends.append(_makeEnd(i, 0, pts[i]))
            ends.append(_makeEnd(i, 1, pts[i]))

    def labelAt(p) -> int:
        x = int(round(p[0])) + 1
        y = int(round(p[1])) + 1
        if x < 0 or y < 0 or x >= w or y >= h:
            return 0
        return int(labels[y, x])

    # 端点按距离聚类（并查集）
    parent = list(range(len(ends)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(ends)):
        for j in range(i + 1, len(ends)):
            if math.dist(ends[i].pos, ends[j].pos) <= 1.5:
                a, b = find(i), find(j)
                if a != b:
                    parent[a] = b

    clusters: dict = {}
    for i in range(len(ends)):
        clusters.setdefault(find(i), []).append(ends[i])

    for cluster in clusters.values():
        if len(cluster) < 2:
            continue
        local = list(cluster)
        merges = 0
        while len(local) >= 2 and merges < 4000:
            ba = bb = -1
            best = minDot
            for a in range(len(local)):
                ea = local[a]
                if not alive[ea.stroke]:
                    continue
                la = labelAt(ea.pos)
                for b in range(a + 1, len(local)):
                    eb = local[b]
                    if eb.stroke == ea.stroke or not alive[eb.stroke]:
                        continue
                    lb = labelAt(eb.pos)
                    if la != 0 and lb != 0 and la != lb:
                        continue
                    score = -(ea.dir[0] * eb.dir[0] + ea.dir[1] * eb.dir[1])
                    if score <= best:
                        continue
                    best = score
                    ba, bb = a, b
            if ba < 0:
                break
            ea, eb = local[ba], local[bb]
            aPart = pts[ea.stroke] if ea.end == 1 else _reverseList(pts[ea.stroke])
            bPart = pts[eb.stroke] if eb.end == 0 else _reverseList(pts[eb.stroke])
            if len(aPart) + len(bPart) > 150000:
                local.remove(ea)
                local.remove(eb)
                continue
            if math.dist(aPart[-1], bPart[0]) < 1e-9:
                aPart = aPart + bPart[1:]
            else:
                aPart = aPart + bPart
            pts[ea.stroke] = aPart
            alive[eb.stroke] = False
            local = [r for r in local
                     if r.stroke != ea.stroke and r.stroke != eb.stroke]
            local.append(_makeEnd(ea.stroke, 0, pts[ea.stroke]))
            local.append(_makeEnd(ea.stroke, 1, pts[ea.stroke]))
            merges += 1

    result: list = []
    for i in range(n):
        if not alive[i] or len(pts[i]) < 2:
            continue
        closedNow = isClosed[i] or (
            len(pts[i]) >= 4 and math.dist(pts[i][0], pts[i][-1]) < 1e-6)
        result.append({"points": pts[i], "closed": closedNow})
    return result


def _medianInscribedRadius(p: dict, dt: np.ndarray) -> float:
    """骨架支路的中位内切半径（各点到最近边界距离的中位数）；无有效采样返回 -1。"""
    pts = p["points"]
    if len(pts) < 2:
        return -1.0
    h, w = dt.shape
    rs = []
    for q in pts:
        qx = int(round(q[0])) + 1
        qy = int(round(q[1])) + 1
        if qx < 1 or qy < 1 or qx >= w - 1 or qy >= h - 1:
            continue
        rs.append(float(dt[qy, qx]))
    if not rs:
        return -1.0
    rs.sort()
    return rs[len(rs) // 2]


def _stampInscribedDisks(p: dict, dt: np.ndarray, blob: np.ndarray) -> None:
    """把骨架支路的内切圆盘并集盖进 blob 掩码（medial disk 重建粗笔画区域）。"""
    h, w = dt.shape
    yy, xx = np.ogrid[:h, :w]
    for q in p["points"]:
        cx = int(round(q[0])) + 1
        cy = int(round(q[1])) + 1
        if cx < 1 or cy < 1 or cx >= w - 1 or cy >= h - 1:
            continue
        r = float(dt[cy, cx]) + 0.5
        blob |= (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r


def _sampleField(f: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """双线性采样场值（坐标夹在场范围内）。"""
    h, w = f.shape
    x = np.clip(x, 0.0, w - 1.0)
    y = np.clip(y, 0.0, h - 1.0)
    x0 = x.astype(np.int32)
    y0 = y.astype(np.int32)
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    fx = x - x0
    fy = y - y0
    top = f[y0, x0] * (1 - fx) + f[y0, x1] * fx
    bot = f[y1, x0] * (1 - fx) + f[y1, x1] * fx
    return top * (1 - fy) + bot * fy


def _refineCenterline(pts: list, closed: bool, fld: np.ndarray, iso: float,
                      invert: bool, maxDist: float) -> list:
    """亚像素重定位：整像素骨架点沿局部切线的法向移动到模糊场两侧等值线交点中点。
    开放路径端点固定不动——交点处各臂端点保持精确重合。"""
    n = len(pts)
    if n < 3:
        return pts
    arr = np.asarray(pts, dtype=np.float64)
    idx = np.arange(n)
    if closed:
        a = arr[(idx - 1) % n]
        b = arr[(idx + 1) % n]
    else:
        a = arr[np.clip(idx - 1, 0, n - 1)]
        b = arr[np.clip(idx + 1, 0, n - 1)]
    t = b - a
    tl = np.hypot(t[:, 0], t[:, 1])
    ok = tl > 1e-9
    nvec = np.zeros_like(t)
    nvec[ok, 0] = -t[ok, 1] / tl[ok]
    nvec[ok, 1] = t[ok, 0] / tl[ok]

    fx = arr[:, 0] + 1.0
    fy = arr[:, 1] + 1.0
    insideHigh = invert
    v0 = _sampleField(fld, fx, fy)
    inside0 = (v0 > iso) if insideHigh else (v0 < iso)

    def findCrossing(dx: np.ndarray, dy: np.ndarray) -> np.ndarray:
        """从 (fx, fy) 沿单位向量步进，找第一次跨过等值线的距离（线性插值）。"""
        step = 0.5
        result = np.full(n, -1.0)
        found = np.zeros(n, dtype=bool)
        prevS = np.zeros(n)
        prevV = v0.copy()
        for k in range(1, int(maxDist / step) + 1):
            s = k * step
            v = _sampleField(fld, fx + dx * s, fy + dy * s)
            ins = (v > iso) if insideHigh else (v < iso)
            cross = (~found) & (ins != inside0)
            if cross.any():
                denom = v - prevV
                with np.errstate(divide="ignore", invalid="ignore"):
                    t0 = np.where(np.abs(denom) > 1e-9, (iso - prevV) / denom, 0.5)
                t0 = np.clip(np.where(np.isfinite(t0), t0, 0.5), 0.0, 1.0)
                dist = prevS + t0 * (s - prevS)
                result[cross] = dist[cross]
                found |= cross
            prevS = np.where(found, prevS, s)
            prevV = np.where(found, prevV, v)
        return result

    sPos = findCrossing(nvec[:, 0], nvec[:, 1])
    sNeg = findCrossing(-nvec[:, 0], -nvec[:, 1])
    good = ok & (sPos >= 0) & (sNeg >= 0)
    if not closed:
        good[0] = False
        good[-1] = False
    out = arr.copy()
    shift = ((sPos - sNeg) * 0.5)
    out[good, 0] += shift[good] * nvec[good, 0]
    out[good, 1] += shift[good] * nvec[good, 1]
    return [(float(p[0]), float(p[1])) for p in out]


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

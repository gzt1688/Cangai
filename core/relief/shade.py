"""高度场 2D 着色（纯 numpy，零 Qt / 零 GL 依赖，可离屏测试）。

把一块 :class:`~core.relief.height_field.HeightField`（或裸 ``(ny, nx)`` ndarray）
栅格化成 ``uint8`` 图像，供 2D 图元转 ``QImage``/``QPixmap`` 预览。所有函数只接收
高度场或裸数组、只输出 ``uint8``，不 import 任何 Qt——着色核心与渲染层彻底解耦。

坐标/行序约定与 HeightField 一致：``data[row, col]``，row=Y 方向、col=X 方向，
行0=minY、列0=minX。输出图像行序同样"行0=minY"，是否翻转到"顶向下"显示由
上层（QImage 边界）唯一决定，本模块不做任何翻转。

2D 预览显示模式（仅两种，与 UI 保持一致）：
- ``contour`` : 浅灰渐变底 + 等值线（等高线，默认）。
- ``gray``    : 高度归一化 → 0..255 灰度图（ArtCAM 2D 同款）。

hillshade / pseudocolor / make_lut 等保留为底层着色工具（可被 3D 材质等复用）。
"""
from __future__ import annotations

import math
from typing import Union

import numpy as np

# 高度场或裸 (ny, nx) ndarray 都可作为输入
HeightLike = Union["object", np.ndarray]

# LUT 锚点（t, R, G, B），用 np.interp 逐通道插值出 256x3 查找表。
_LUT_ANCHORS = {
    # 浮雕友好：暗棕 → 暖褐 → 米白（低处深、高处亮，立体感自然）
    "relief": (
        (0.00, 40, 28, 18),
        (0.20, 95, 62, 38),
        (0.40, 150, 105, 72),
        (0.60, 200, 165, 130),
        (0.80, 235, 215, 190),
        (1.00, 252, 248, 240),
    ),
    # turbo 风格：靛蓝 → 青 → 绿 → 黄 → 橙 → 暗红（高度分层最醒目）
    "turbo": (
        (0.00, 48, 18, 59),
        (0.13, 65, 85, 203),
        (0.25, 60, 166, 220),
        (0.38, 45, 220, 175),
        (0.50, 140, 235, 90),
        (0.63, 225, 220, 60),
        (0.75, 250, 155, 40),
        (0.88, 235, 80, 30),
        (1.00, 122, 4, 3),
    ),
    # 中性灰阶
    "gray": (
        (0.00, 0, 0, 0),
        (1.00, 255, 255, 255),
    ),
    # 地图/高度色显示：颜色由用户从精雕原图拾取（从低到高 15 个采样），hnorm 等距分布。
    #   Z=0 亮青→青灰→绿→黄绿→棕褐→橙棕→浅灰（峰顶），柔和真实、不刺眼。
    "terrain": (
        (0.0000,  85, 216, 238),   # 亮青（Z=0 最低）
        (0.0714,  68, 141, 154),   # 深青
        (0.1429,  98, 140, 148),   # 青灰
        (0.2143, 127, 145, 151),   # 浅灰蓝
        (0.2857, 106, 143, 130),   # 灰绿
        (0.3571,  62, 135,  94),   # 绿
        (0.4286,  96, 149, 115),   # 浅绿
        (0.5000, 100, 156, 110),   # 绿
        (0.5714, 119, 168, 115),   # 黄绿
        (0.6429, 152, 162, 136),   # 卡其
        (0.7143, 175, 151, 138),   # 棕褐
        (0.7857, 182, 136, 111),   # 浅棕
        (0.8571, 196, 131,  95),   # 橙棕
        (0.9286, 193, 162, 145),   # 浅褐灰
        (1.0000, 186, 185, 185),   # 浅灰（峰顶）
    ),
}


# ---------------- 内部工具 ----------------

def _as_array(hf: HeightLike) -> np.ndarray:
    """取高度场的 (ny, nx) float32 数组；裸 ndarray 直接用。"""
    a = hf.data if hasattr(hf, "data") else hf
    return np.asarray(a, dtype=np.float32)


def _spacing(hf: HeightLike) -> float:
    """取网格精度 mm/px（裸 ndarray 无 spacing → 1.0）。"""
    sp = getattr(hf, "spacing", None)
    if sp is None:
        return 1.0
    return float(sp)


def _normalize01(hf: HeightLike) -> np.ndarray:
    """高度线性归一化到 [0,1]（float64）；平坦场（min==max）→ 0.5。"""
    a = _as_array(hf).astype(np.float64)
    lo = float(a.min())
    hi = float(a.max())
    if hi - lo < 1e-12:
        return np.full(a.shape, 0.5, dtype=np.float64)
    return (a - lo) / (hi - lo)


def _u8(x: np.ndarray) -> np.ndarray:
    """float [0,255] → uint8，四舍五入（避免截断偏暗，如平坦场 127.5→128）。"""
    return np.clip(np.rint(np.asarray(x, dtype=np.float64)), 0, 255).astype(np.uint8)


def _illumination(hf: HeightLike, azimuth: float, altitude: float,
                  zexag: float) -> np.ndarray:
    """定向光照强度 [0,1]（float64）：法线·光向点积。

    azimuth: 光源方位角（度，正北=0、顺时针，315=西北）；altitude: 仰角（度）。
    zexag: 垂直夸张倍数（放大坡度让微弱起伏可见）。
    """
    z = _as_array(hf).astype(np.float64) * float(zexag)
    sp = _spacing(hf)
    # np.gradient 返回 [d/d(axis0=行=Y), d/d(axis1=列=X)]，spacing 各向一致
    gy, gx = np.gradient(z, sp, sp)

    az = math.radians(float(azimuth))
    alt = math.radians(float(altitude))
    # 指向光源的单位向量（X=东, Y=北, Z=上）
    lx = math.cos(alt) * math.sin(az)
    ly = math.cos(alt) * math.cos(az)
    lz = math.sin(alt)

    # 表面法线 N = (-dz/dx, -dz/dy, 1)，illum = N·L / |N|
    num = -gx * lx - gy * ly + lz
    den = np.sqrt(gx * gx + gy * gy + 1.0)
    return np.clip(num / den, 0.0, 1.0)


# ---------------- 公开着色函数 ----------------

def make_lut(name: str = "relief") -> np.ndarray:
    """构造 256x3 uint8 颜色查找表（内置 'relief' / 'turbo' / 'gray'）。"""
    anchors = _LUT_ANCHORS.get(name) or _LUT_ANCHORS["relief"]
    t = np.array([a[0] for a in anchors], dtype=np.float64)
    ts = np.linspace(0.0, 1.0, 256, dtype=np.float64)
    lut = np.empty((256, 3), dtype=np.uint8)
    for ch in range(3):
        vals = np.array([a[ch + 1] for a in anchors], dtype=np.float64)
        lut[:, ch] = _u8(np.interp(ts, t, vals))
    return lut


def height_to_gray(hf: HeightLike, zexag: float | None = None) -> np.ndarray:
    """高度归一化 → 灰度 uint8 ``(ny, nx)``（平坦场 → 128）。

    zexag 仅为与其它着色函数保持签名一致而接受；灰度已自动归一化，故不影响结果。
    """
    a = _normalize01(hf)
    return _u8(np.clip(a, 0.0, 1.0) * 255.0)


def hillshade(hf: HeightLike, azimuth: float = 315.0, altitude: float = 45.0,
              zexag: float = 1.0, fraction: float = 1.0) -> np.ndarray:
    """山体阴影 uint8 ``(ny, nx)``：定向光照强度（0=背光暗、255=迎光亮）。

    fraction∈[0,1] 控制阴影浓度：1.0=纯定向光照（默认），<1 向平均亮度靠拢
    （对比减弱、更柔和）。
    """
    illum = _illumination(hf, azimuth, altitude, zexag)
    f = float(np.clip(fraction, 0.0, 1.0))
    shade = f * illum + (1.0 - f) * illum.mean()
    return _u8(np.clip(shade, 0.0, 1.0) * 255.0)


def overlay(gray: np.ndarray, shade: np.ndarray) -> np.ndarray:
    """Photoshop overlay 混合：灰度底色 ``gray`` × 光照 ``shade`` → RGB uint8
    ``(ny, nx, 3)``。暗部相乘、亮部滤色，最像真实浮雕的立体着色。
    """
    g = np.asarray(gray, dtype=np.float64) / 255.0
    s = np.asarray(shade, dtype=np.float64) / 255.0
    out = np.where(g < 0.5, 2.0 * g * s, 1.0 - 2.0 * (1.0 - g) * (1.0 - s))
    v = _u8(np.clip(out, 0.0, 1.0) * 255.0)
    return np.repeat(v[..., np.newaxis], 3, axis=2)


def pseudocolor(hf: HeightLike, lut: np.ndarray) -> np.ndarray:
    """高度 → LUT 伪彩 RGB uint8 ``(ny, nx, 3)``。"""
    a = _normalize01(hf)
    idx = np.clip((a * 255.0).astype(np.int32), 0, 255)
    lut = np.asarray(lut, dtype=np.uint8)
    return np.ascontiguousarray(lut[idx])


def contours(hf: HeightLike, levels: int = 12) -> np.ndarray:
    """等高线 RGB uint8 ``(ny, nx, 3)``：浅灰渐变底 + 深色 1px 等值线。

    levels: 等值线条数（linspace(min,max,levels)）。相邻单元跨越级别处画线。
    """
    a = _as_array(hf).astype(np.float64)
    lo = float(a.min())
    hi = float(a.max())
    n = max(2, int(levels))
    lv = np.linspace(lo, hi, n)
    # 每个单元落在哪一级区间（searchsorted - 1），钳到 [0, n-2]
    band = np.clip(np.searchsorted(lv, a, side="right") - 1, 0, n - 2)

    mask = np.zeros(a.shape, dtype=bool)
    dc = band[:, :-1] != band[:, 1:]          # 列方向邻居跨级
    dr = band[:-1, :] != band[1:, :]          # 行方向邻居跨级
    mask[:, :-1] |= dc
    mask[:, 1:] |= dc
    mask[:-1, :] |= dr
    mask[1:, :] |= dr

    base = 200.0 + _normalize01(hf) * 45.0    # 200..245 浅底
    rgb = np.repeat(_u8(base)[..., np.newaxis], 3, axis=2)
    rgb[mask] = (30, 90, 110)                 # 深青等值线
    return np.ascontiguousarray(rgb.astype(np.uint8))


def render_preview(hf: HeightLike, mode: str = "contour",
                   azimuth: float = 315.0, altitude: float = 45.0,
                   zexag: float = 1.0, lut_name: str = "relief",
                   levels: int = 12) -> np.ndarray:
    """按显示模式渲染 RGB uint8 ``(ny, nx, 3)`` 2D 预览图。

    当前仅两种模式（与 UI 一致）：
    - ``contour`` : 浅灰渐变底 + 等值线（等高线，默认）；
    - ``gray`` / 其它未知值 : 高度归一化灰度图。
    """
    if mode == "contour":
        return contours(hf, levels)
    # gray（或未知模式回退）→ 灰度 RGB
    g = height_to_gray(hf)
    return np.repeat(g[..., np.newaxis], 3, axis=2)

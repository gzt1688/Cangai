"""磨光（磨光笔刷）—— 扩散式磨光：把笔刷范围内的细节向邻域均值缓缓扩散。

三种磨光方式（对齐精雕"磨光"页，标签从简）：
- 去高补低 both：delta 全保留——高处下削、低处上填，双向抹平；
- 补低 fill：只保留正 delta——只填低处，绝不下刀；
- 取高 carve：只保留负 delta——只削高处，绝不填料。

算法（曲率流/扩散，丝滑的关键）：每点向**小邻域均值**（半径 = 笔刷半径的
1/3）靠拢，delta = 力度 × 笔刷权重 × (局部均值 − 当前高度)。邻域取小核，
磨的是"曲率"而非"高度"——

- 高处被邻域拖着缓缓下降、低处缓缓抬升（渐近式，不跳跃）；
- 平缓坡面与光滑区域几乎不动（本就平滑，无需磨）；
- 不会把笔刷范围削成一个大平台（旧版用整刷半径做均值窗口，目标面是
  一块平的均值，磨出来生硬、有台阶感）。

力度 strength（0.1–100，%）：一次盖章向局部均值靠拢的比例——0.5% 极微
（细微凹凸慢慢找平）、92% 极强（单笔显著、仍连续）。预设档位见
``STRENGTH_PRESETS``（很小到很大全覆盖），面板输入框可任意输入，
选中档位即把输入框设为该值（输入框实时显示当前力度）。

笔刷形状固定高斯：权重 = 高斯核（半衰直径定 σ）× Hann 窗（直径处截止），
与导动/堆料笔刷同一套衰减语义，控制磨光的空间作用范围。

坐标约定与 HeightField 一致：``data[row, col]``，行 0 = minY、列 0 = minX。
纯算法、零 UI 依赖、纯 numpy（不依赖 scipy）。
"""
from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np

MODES = ("both", "fill", "carve")
MODE_LABELS = ("去高补低", "补低", "取高")

# 力度：输入框范围 0.1–100（%）；快捷键 Q/W 每次 ±STRENGTH_STEP
STRENGTH_MIN = 0.1
STRENGTH_MAX = 100.0
STRENGTH_STEP = 2.0
DEFAULT_SMOOTH_STRENGTH = 8.0

# 预设档位（面板单选框，选中即把力度输入框设为该值）：按近等比细分，
# 低端加密——很细微的凹凸用"极微/很微"慢慢磨；面板 3 排 × 3 档排布
STRENGTH_PRESETS = (
    ("极微", 0.5),
    ("很微", 1.5),
    ("轻微", 4.0),
    ("较轻", 8.0),
    ("中等", 16.0),
    ("偏强", 30.0),
    ("强力", 50.0),
    ("很强", 72.0),
    ("极强", 92.0),
)

DEFAULT_SMOOTH_DIAMETER = 6.0
DEFAULT_SMOOTH_HALF_LIFE = 3.0


@dataclass
class SmoothParams:
    """磨光参数（mode 对应精雕磨光三方式；strength 为百分比力度）。"""

    mode: str = "both"                 # both=去高补低 / fill=补低 / carve=取高
    strength: float = DEFAULT_SMOOTH_STRENGTH      # 1..100（%）
    diameter: float = DEFAULT_SMOOTH_DIAMETER      # 直径 mm
    half_life_diameter: float = DEFAULT_SMOOTH_HALF_LIFE   # 半衰直径 mm
    color_scope: str = "ignore"        # ignore/inside/outside（相对当前颜色）


def _win_mean_1d(a: np.ndarray, k: int, axis: int) -> np.ndarray:
    """沿 axis 的 (2k+1) 窗口均值（边缘复制填充），k<=0 原样返回。

    cumsum 滑窗求和：O(n) 与窗口大小无关；边缘用复制填充使边界处
    均值偏向边界值（磨到模型边缘不产生假凹陷）。
    """
    if k <= 0:
        return a.astype(np.float64, copy=True)
    n = int(a.shape[axis])
    pad = [(0, 0), (0, 0)]
    pad[axis] = (k, k)
    p = np.pad(a.astype(np.float64), pad, mode="edge")
    c = np.cumsum(p, axis=axis)
    hi = np.take(c, np.arange(2 * k, 2 * k + n), axis=axis)
    lo = np.take(c, np.arange(0, max(n - 1, 0)), axis=axis)
    zeros = np.zeros_like(np.take(c, [0], axis=axis))
    lo = np.concatenate([zeros, lo], axis=axis)
    return (hi - lo) / float(2 * k + 1)


def _local_mean(z: np.ndarray, k: int) -> np.ndarray:
    """两遍盒式均值（先行后列）≈ 高斯低通：磨光的目标面。"""
    return _win_mean_1d(_win_mean_1d(z, k, 0), k, 1)


def apply_sweep_smooth(hf, poly: np.ndarray, params: SmoothParams,
                       color_mask: np.ndarray | None = None,
                       color_value: int = 0,
                       spacing: float | None = None,
                       closed: bool | None = None) -> int:
    """沿导动折线按弧长间距连续磨光（扩散式盖章），返回累计受影响格数。

    路径磨光：以笔刷直径的 1/4 为盖章间距沿线行走，每处施加一次
    :func:`apply_smooth_stamp`（方式/力度/半衰/颜色裁剪均由 params 决定）。
    重叠盖章沿线自然融合，磨出一条丝滑的路径磨光带。
    ``closed`` 为导动线原始闭合标志（真则补走末点→首点的闭合段，
    否则闭合曲线接缝处留一段未磨光）。
    """
    if poly.shape[0] < 2:
        return 0
    if closed is None:
        closed = bool(np.isclose(poly[0, 0], poly[-1, 0])
                      and np.isclose(poly[0, 1], poly[-1, 1]))
    if closed:
        poly = np.vstack([poly, poly[:1]])
    sp = hf.spacing
    if spacing is None:
        spacing = max(float(params.diameter) * 0.25, sp, 1e-6)
    ax, ay = poly[:, 0], poly[:, 1]
    seg_dx, seg_dy = np.diff(ax), np.diff(ay)
    seg_len = np.hypot(seg_dx, seg_dy)

    total = 0
    total += apply_smooth_stamp(hf, float(ax[0]), float(ay[0]), params,
                                color_mask, color_value)
    next_at = spacing          # 下一次盖章距起点弧长
    acc = 0.0                  # 已走过弧长（段起点）
    for i, L in enumerate(seg_len):
        L = float(L)
        if L < 1e-12:
            continue
        ux, uy = seg_dx[i] / L, seg_dy[i] / L
        seg_end = acc + L
        while next_at <= seg_end:
            d = next_at - acc
            total += apply_smooth_stamp(hf, ax[i] + ux * d, ay[i] + uy * d,
                                        params, color_mask, color_value)
            next_at += spacing
        acc = seg_end
    total += apply_smooth_stamp(hf, float(ax[-1]), float(ay[-1]), params,
                                color_mask, color_value)
    return total


def apply_smooth_stamp(hf, cx: float, cy: float, params: SmoothParams,
                       color_mask: np.ndarray | None = None,
                       color_value: int = 0) -> int:
    """以 (cx, cy)（世界 mm）为中心磨一次光（就地写入高度场）。

    扩散式（曲率流）：delta = 力度 × 笔刷权重 × (小邻域均值 − 当前高度)。
    - both：全保留（高处变低、低处变高）；
    - fill：只保留 ≥0 部分（补低）；carve：只保留 ≤0 部分（取高）。
    作用范围按当前颜色裁剪（颜色无效/颜色内/颜色外）。返回受影响格数。
    """
    ny, nx = hf.ny, hf.nx
    data = hf.data
    D = max(float(params.diameter), 1e-6)
    dH = min(max(float(params.half_life_diameter), 1e-6), D)
    f = min(1.0, max(0.0, float(params.strength) / 100.0))
    if f < 1e-9:
        return 0

    r = D * 0.5
    ox, oy = hf.origin
    sp = hf.spacing
    col_lo = max(0, int(math.floor((cx - r - ox) / sp)))
    col_hi = min(nx - 1, int(math.ceil((cx + r - ox) / sp)))
    row_lo = max(0, int(math.floor((cy - r - oy) / sp)))
    row_hi = min(ny - 1, int(math.ceil((cy + r - oy) / sp)))
    if col_lo > col_hi or row_lo > row_hi:
        return 0

    # 扩散尺度：邻域窗口 = 笔刷半径的 1/3（小核磨曲率，不削平台）。
    # 逐格上限防极端大刷×高精度的无谓开销（窗口越大越接近整场均值）。
    k = max(1, min(32, int(round(r / (3.0 * sp)))))
    win_lo_r = max(0, row_lo - k)
    win_hi_r = min(ny - 1, row_hi + k)
    win_lo_c = max(0, col_lo - k)
    win_hi_c = min(nx - 1, col_hi + k)
    z_win = data[win_lo_r:win_hi_r + 1, win_lo_c:win_hi_c + 1] \
        .astype(np.float64)
    mean_win = _local_mean(z_win, k)

    # 笔刷权重（ROI 内）：高斯核（半衰定 σ）× Hann 窗（直径处截止）
    ys = oy + np.arange(row_lo, row_hi + 1, dtype=np.float64) * sp
    xs = ox + np.arange(col_lo, col_hi + 1, dtype=np.float64) * sp
    dx = xs[None, :] - float(cx)
    dy = ys[:, None] - float(cy)
    d = np.hypot(dx, dy)
    sigma = (dH * 0.5) / math.sqrt(2.0 * math.log(2.0))
    g = np.exp(-(d * d) / (2.0 * sigma * sigma))
    rr = np.clip(d / r, 0.0, 1.0)
    win = 0.5 * (1.0 + np.cos(np.pi * rr))
    w = np.where(d < r, g * win, 0.0)

    # 均值窗中切出 ROI 对应部分，delta 只在 ROI 内计算
    mean_roi = mean_win[row_lo - win_lo_r:row_hi - win_lo_r + 1,
                        col_lo - win_lo_c:col_hi - win_lo_c + 1]
    z_roi = data[row_lo:row_hi + 1, col_lo:col_hi + 1].astype(np.float64)
    delta = f * w * (mean_roi - z_roi)
    if params.mode == "fill":
        delta = np.maximum(delta, 0.0)
    elif params.mode == "carve":
        delta = np.minimum(delta, 0.0)

    # 作用范围按当前颜色裁剪（颜色无效/颜色内/颜色外）
    if params.color_scope in ("inside", "outside"):
        if color_mask is not None and color_mask.shape == (ny, nx):
            inside_roi = color_mask[row_lo:row_hi + 1,
                                    col_lo:col_hi + 1] == int(color_value)
        else:
            inside_roi = np.zeros(delta.shape, dtype=bool)
        if params.color_scope == "inside":
            delta[~inside_roi] = 0.0
        else:
            delta[inside_roi] = 0.0

    mask = delta != 0.0
    if not mask.any():
        return 0
    roi = data[row_lo:row_hi + 1, col_lo:col_hi + 1]   # 基本切片视图：写穿
    roi[mask] += delta[mask].astype(np.float32)
    return int(np.count_nonzero(mask))

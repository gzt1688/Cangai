"""区域斜面：曲线像刀刃斜着切——斜面等高线平行于曲线（曲线沿法向偏移形成的
直纹面去切模型）。曲线处不减（最高），偏移到区域最远处减 height（最低），
中间按 profile 沿法向过渡。

算法：
1. 曲线密集栅格化为障碍墙；
2. scipy.ndimage.label 在区域内用曲线分隔出各侧（连通域）；
3. d_curve = 每点到曲线的最近距离（对直线即垂直距离，等高线平行于曲线）；
4. d_max = 选定侧内 d_curve 的最大值（把曲线沿法向偏移到区域最远处的距离）；
5. 归一化 t = d_curve / d_max（曲线上 t=0、最远边界 t=1）；
6. 衰减权重 w = 1 - profile(t)（曲线 w=0 不减=最高、最远处 w=1 减 height=最低）；
7. 选定侧内减去 height·w，外部不变。

方向（direction）决定归一化坐标 t 从哪来：
- ``normal``     到曲线的最近距离（默认，等高线平行于曲线）；
- ``radial``     到区域质心（或指定中心）的距离——用于圆形/椭圆截面造型；
- ``horizontal`` 选区沿 X 轴的位置；
- ``vertical``   选区沿 Y 轴的位置。
横向/竖向/径向同样支持平台距离：自起点起 plateau 距离内保持最高。

截面曲线（ptype="curve"）用归一化控制点 [(x, y), ...] 描述，x∈[0,1] 是 t，
y∈[0,1] 是"保留高度比例"（1=完全不减/最高，0=减满/最低），用单调三次样条
（PCHIP）插值，保证不超调。预设曲线见 :func:`preset_curve_points`。
"""
from __future__ import annotations

import numpy as np
from scipy.ndimage import distance_transform_edt, label

from core.relief.auto_separate import _rasterize_barriers
from core.relief.region_relief import resample_polyline


PROFILE_TYPES = ("linear", "spline", "wave", "convex", "concave", "curve")
PROFILE_LABELS = ("直线", "样条", "波浪", "凸面", "凹面", "曲线")

DIRECTIONS = ("normal", "radial", "horizontal", "vertical")
DIRECTION_LABELS = ("法向", "径向", "横向", "竖向")

# 内置截面曲线预设（名称 → 控制点）。点列均为归一化坐标，y=保留高度比例。
CURVE_PRESET_NAMES = ("直线", "平滑", "缓入", "缓出", "圆顶",
                      "碗形", "波浪", "阶梯", "反向斜坡")


def preset_curve_points(name: str) -> list:
    """内置预设曲线控制点（未知名返回直线）。点少而准，便于用户继续拖点。"""
    if name in ("直线", "反向斜坡"):
        return ([(0.0, 1.0), (1.0, 0.0)] if name == "直线"
                else [(0.0, 0.0), (1.0, 1.0)])
    n = 21 if name == "波浪" else (5 if name == "阶梯" else 9)
    t = np.linspace(0.0, 1.0, n)
    if name == "平滑":
        y = 3.0 * (1.0 - t) ** 2 - 2.0 * (1.0 - t) ** 3
    elif name == "缓入":
        y = 1.0 - t ** 1.8
    elif name == "缓出":
        y = (1.0 - t) ** 1.8
    elif name == "圆顶":
        y = np.sqrt(np.clip(1.0 - t * t, 0.0, 1.0))
    elif name == "碗形":
        y = 1.0 - np.sqrt(np.clip(1.0 - (1.0 - t) ** 2, 0.0, 1.0))
    elif name == "波浪":
        y = (1.0 - t) * (0.72 + 0.28 * np.cos(2.0 * np.pi * 2.0 * t))
    elif name == "阶梯":
        y = np.where(t < 0.45, 1.0, np.where(t > 0.55, 0.0, 0.5))
    else:                       # 直线
        y = 1.0 - t
    return [(float(a), float(b)) for a, b in zip(t, y)]


def normalize_curve_points(points) -> np.ndarray:
    """控制点归一化：clamp 到 [0,1]、按 x 升序、合并重复 x、补首尾端点。

    端点 x 固定 0 / 1（与 PS 曲线一致），缺失时用最近点的 y 补齐。
    """
    fallback = np.array([[0.0, 1.0], [1.0, 0.0]], dtype=np.float64)
    if points is None:
        return fallback
    try:
        pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    except Exception:
        return fallback
    if pts.shape[0] < 2 or not np.isfinite(pts).all():
        return fallback
    pts[:, 0] = np.clip(pts[:, 0], 0.0, 1.0)
    pts[:, 1] = np.clip(pts[:, 1], 0.0, 1.0)
    pts = pts[np.argsort(pts[:, 0], kind="stable")]
    keep = np.ones(pts.shape[0], dtype=bool)
    keep[1:] = np.diff(pts[:, 0]) > 1e-6
    pts = pts[keep]
    if pts.shape[0] < 2:
        return fallback
    if pts[0, 0] > 1e-6:
        pts = np.vstack(([0.0, pts[0, 1]], pts))
    if pts[-1, 0] < 1.0 - 1e-6:
        pts = np.vstack((pts, [1.0, pts[-1, 1]]))
    return pts


_LUT_N = 257
_LUT_CACHE: dict = {}


def _curve_lut(points, n: int = _LUT_N) -> np.ndarray:
    """控制点 → 等距采样表（PCHIP 单调插值，无超调）；小型缓存避免逐帧重建。"""
    pts = normalize_curve_points(points)
    key = (n, tuple(np.round(pts.ravel(), 4).tolist()))
    hit = _LUT_CACHE.get(key)
    if hit is not None:
        return hit
    xs = np.linspace(0.0, 1.0, n)
    if pts.shape[0] == 2:
        ys = np.interp(xs, pts[:, 0], pts[:, 1])
    else:
        try:
            from scipy.interpolate import PchipInterpolator
            ys = PchipInterpolator(pts[:, 0], pts[:, 1])(xs)
        except Exception:
            ys = np.interp(xs, pts[:, 0], pts[:, 1])
    ys = np.clip(ys, 0.0, 1.0)
    if len(_LUT_CACHE) > 8:
        _LUT_CACHE.clear()
    _LUT_CACHE[key] = ys
    return ys


def curve_profile(t: np.ndarray, points) -> np.ndarray:
    """归一化 t → 保留高度比例（曲线控制点插值）。"""
    lut = _curve_lut(points)
    return np.interp(np.clip(t, 0.0, 1.0),
                     np.linspace(0.0, 1.0, lut.size), lut)


def _profile(t: np.ndarray, ptype: str,
             params: dict | None = None) -> np.ndarray:
    """归一化距离 t∈[0,1] → 衰减权重，profile(0)=1（曲线最高）、profile(1)=0（最低）。

    params 细化参数（缺省用默认值）：
      wave:     wave_count 波纹数(1~10, 默认2)、wave_amp 波纹幅度(0~1, 默认0.3)
      convex:   arc 弧度(0~1, 默认0.25) → p=1+4·arc，w=1-(1-t^p)^(1/p) 外凸弧
      concave:  arc 弧度(0~1, 默认0.25) → p=1+4·arc，w=(1-(1-t)^p)^(1/p) 内凹弧
      curve:    curve_points 控制点列表 → PCHIP 插值
    arc=0 时凸/凹都退化为直线；凸凹互为镜像 w_c(t)=1-w_v(1-t)。
    """
    t = np.clip(t, 0.0, 1.0)
    p = params or {}
    if ptype == "linear":
        return 1.0 - t
    if ptype == "spline":
        s = 1.0 - t
        return 3.0 * s * s - 2.0 * s * s * s
    if ptype == "wave":
        n = max(1, int(p.get("wave_count", 2)))
        a = min(1.0, max(0.0, float(p.get("wave_amp", 0.3))))
        return (1.0 - t) * (1.0 - a + a * np.cos(2.0 * np.pi * n * t))
    if ptype == "convex":
        k = 1.0 + 4.0 * min(1.0, max(0.0, float(p.get("arc", 0.25))))
        return (1.0 - t ** k) ** (1.0 / k)
    if ptype == "concave":
        k = 1.0 + 4.0 * min(1.0, max(0.0, float(p.get("arc", 0.25))))
        return 1.0 - (1.0 - (1.0 - t) ** k) ** (1.0 / k)
    if ptype == "curve":
        return curve_profile(t, p.get("curve_points"))
    return 1.0 - t


def _densify_curve(curve_pts: np.ndarray, sp: float) -> np.ndarray:
    length = float(np.linalg.norm(np.diff(curve_pts, axis=0), axis=1).sum())
    n = max(2, int(np.ceil(length / max(sp * 0.5, 1e-9))))
    return resample_polyline(curve_pts, n)


def split_sides(hf, mask: np.ndarray, curve_pts: np.ndarray) -> list:
    """用曲线把区域分成若干侧（连通域），返回侧 mask 列表。

    曲线栅格化为障碍墙后在区域内做 4 连通标记，每个连通域为一侧。
    若曲线没把区域分开则返回单个侧（=整个区域去曲线）。
    """
    if mask is None or not mask.any() or curve_pts.shape[0] < 2:
        return []
    sp = hf.spacing
    dense = _densify_curve(curve_pts, sp)
    barrier = _rasterize_barriers(hf, [dense])
    region_free = mask & ~barrier
    labels, n = label(region_free)
    sides = []
    for i in range(1, n + 1):
        s = labels == i
        if s.any():
            sides.append(s)
    return sides


def _region_centroid(mask: np.ndarray) -> tuple:
    """区域质心（栅格坐标，内存 O(nx+ny)）。"""
    ny, nx = mask.shape
    col = mask.sum(axis=0, dtype=np.float64)
    row = mask.sum(axis=1, dtype=np.float64)
    tot = float(col.sum())
    if tot <= 0:
        return (ny - 1) * 0.5, (nx - 1) * 0.5
    cy = float((row * np.arange(ny, dtype=np.float64)).sum() / tot)
    cx = float((col * np.arange(nx, dtype=np.float64)).sum() / tot)
    return cy, cx


def _direction_values(hf, work: np.ndarray, curve_pts: np.ndarray,
                      direction: str, params: dict) -> np.ndarray:
    """返回 work 顺序的一维坐标值（mm；normal 从曲线算 0 起）。"""
    sp = hf.spacing
    if direction == "normal":
        dense = _densify_curve(curve_pts, sp)
        barrier = _rasterize_barriers(hf, [dense])
        dist = distance_transform_edt(~barrier)
        return (dist[work] * sp).astype(np.float32)

    ny, nx = work.shape
    ox, oy = hf.origin
    if direction == "radial":
        cx = params.get("center_x")
        cy = params.get("center_y")
        if cx is None or cy is None:
            gy, gx = _region_centroid(work)
        else:
            gx = (float(cx) - ox) / max(sp, 1e-9)
            gy = (float(cy) - oy) / max(sp, 1e-9)
        xx = np.arange(nx, dtype=np.float32)[None, :]
        parts = []
        for r0 in range(0, ny, 512):
            r1 = min(ny, r0 + 512)
            rr = np.arange(r0, r1, dtype=np.float32)[:, None]
            blk = np.sqrt((rr - gy) ** 2 + (xx - gx) ** 2) * sp
            wb = work[r0:r1]
            if wb.any():
                parts.append(blk[wb])
        if not parts:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(parts)

    if direction == "horizontal":
        xs = np.arange(nx, dtype=np.float32) * sp
        v = np.broadcast_to(xs[None, :], (ny, nx))
    else:                                        # vertical
        ys = np.arange(ny, dtype=np.float32) * sp
        v = np.broadcast_to(ys[:, None], (ny, nx))
    return v[work]


def region_slope(hf, mask: np.ndarray, curve_pts: np.ndarray,
                 height: float, ptype: str = "linear",
                 side_mask: np.ndarray | None = None,
                 profile_params: dict | None = None) -> int:
    """选定侧内以曲线为刀刃斜切：等高线平行于曲线，曲线处不减（最高），
    偏移到区域最远处减 height（最低），中间按 profile 过渡。就地改 hf.data。

    side_mask 指定做斜面的一侧（mask 子集）；None 则整个 mask。
    profile_params 细化参数：
      mode     叠加方式 "subtract"(切除,默认)/"add"(叠加)；两者完全相反——
               切除: w=1-profile(t), 曲线处不减=最高、远处减 height=最低
               叠加: w=profile(t),   曲线处减 height=最低、远处不减=最高
      direction  归一化坐标来源 normal/radial/horizontal/vertical（默认 normal）
      center_x/center_y  径向模式中心（世界坐标，缺省=区域质心）
      plateau  平台距离（世界单位，默认0）——从起点算 plateau 内不减（平台），
               衰减从该距离处开始；plateau>=最远距离时整个区域为平台不减
      wave_count / wave_amp  波浪波纹数、波纹幅度
      arc      凸/凹弧度
      curve_points  曲线类型（ptype="curve"）的控制点 [[x,y],...]
    区域外不变。返回实际修改格数。
    """
    if mask is None or not mask.any():
        return 0
    if height == 0.0:
        return 0
    p = profile_params or {}
    direction = p.get("direction", "normal")
    if direction not in DIRECTIONS:
        direction = "normal"
    cv = (np.asarray(curve_pts, dtype=np.float64)
          if curve_pts is not None else np.zeros((0, 2), dtype=np.float64))
    # 法向模式必须有曲线；径向/横向/竖向只用区域范围，曲线可省略
    if direction == "normal" and cv.shape[0] < 2:
        return 0
    work = mask & (side_mask if side_mask is not None else True)
    if not work.any():
        return 0

    v = _direction_values(hf, work, cv, direction, p)
    if v.size == 0:
        return 0

    vmin = float(v.min())
    vmax = float(v.max())
    span = vmax - vmin
    if span <= 1e-9:
        return 0
    plateau = max(0.0, float(p.get("plateau", 0.0)))
    denom = span - plateau
    if denom <= 1e-9:                    # 平台已覆盖整个区域
        t = np.zeros_like(v, dtype=np.float32)
    else:
        t = (v - vmin - plateau) / denom
        np.clip(t, 0.0, 1.0, out=t)

    prof = _profile(t, ptype, p).astype(np.float32, copy=False)
    vals = hf.data[work]
    if p.get("mode", "subtract") == "add":
        # 叠加：按曲线保留比例抬高（曲线高=抬得多）
        vals += np.float32(height) * prof
    else:
        # 切除：按曲线剩余比例削减（曲线高=减得少，曲线 1=最高不减）
        vals -= np.float32(height) * (1.0 - prof)
    hf.data[work] = vals
    return int(work.sum())

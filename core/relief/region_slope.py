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
"""
from __future__ import annotations

import numpy as np
from scipy.ndimage import distance_transform_edt, label

from core.relief.auto_separate import _rasterize_barriers
from core.relief.region_relief import resample_polyline


PROFILE_TYPES = ("linear", "spline", "wave", "convex", "concave")
PROFILE_LABELS = ("直线", "样条", "波浪", "凸面", "凹面")


def _profile(t: np.ndarray, ptype: str,
             params: dict | None = None) -> np.ndarray:
    """归一化距离 t∈[0,1] → 衰减权重，profile(0)=1（曲线最高）、profile(1)=0（最低）。

    params 细化参数（缺省用默认值）：
      wave:     wave_count 波纹数(1~10, 默认2)、wave_amp 波纹幅度(0~1, 默认0.3)
      convex:   arc 弧度(0~1, 默认0.25) → p=1+4·arc，w=1-(1-t^p)^(1/p) 外凸弧
      concave:  arc 弧度(0~1, 默认0.25) → p=1+4·arc，w=(1-(1-t)^p)^(1/p) 内凹弧
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
      plateau  平台距离（世界单位，默认0）——距曲线 plateau 内不减（平台），
               斜面从该距离处开始衰减；plateau>=最远距离时整个区域为平台不减
      wave_count / wave_amp  波浪波纹数、波纹幅度
      arc      凸/凹弧度
    区域外不变。返回实际修改格数。
    """
    if mask is None or not mask.any() or curve_pts.shape[0] < 2:
        return 0
    if height == 0.0:
        return 0
    sp = hf.spacing
    dense = _densify_curve(curve_pts, sp)
    barrier = _rasterize_barriers(hf, [dense])

    work = mask & (side_mask if side_mask is not None else True)
    if not work.any():
        return 0

    dist_curve = distance_transform_edt(~barrier)
    d_max = float(dist_curve[work].max())
    if d_max <= 1e-9:
        return 0

    plateau = max(0.0, float((profile_params or {}).get("plateau", 0.0)))
    plat_g = min(plateau / max(sp, 1e-9), d_max)
    t = (dist_curve - plat_g) / max(d_max - plat_g, 1e-9)
    np.clip(t, 0.0, 1.0, out=t)

    mode = (profile_params or {}).get("mode", "subtract")
    prof = _profile(t, ptype, profile_params)
    w = prof if mode == "add" else (1.0 - prof)
    delta = height * w
    sub = hf.data.astype(np.float64)
    sub[work] = sub[work] - delta[work]
    hf.data[:] = sub.astype(np.float32)
    return int(work.sum())

"""旋转面：轮廓线绕轴旋转180°生成半圆穹顶高度场。

轮廓线定义沿轴各位置的旋转半径 r(s)；高度场某点到轴垂直距离 d，
则高度 z = sqrt(r(s)² - d²)（d≤r 处有值，d>r 处为 0）。
轴上点（d=0）高度最高 z=r，即轮廓线到轴的垂直距离。
内凹（同一高度上有多个半径）按外轮廓包络填满，不留空缺；超出轮廓
首尾 s 范围的点不生成，保证不超出轮廓线。
"""
from __future__ import annotations
import numpy as np

__all__ = ["revolve_slope"]

_MAX_DENSE = 500000


def _densify(pts: np.ndarray, step: float) -> np.ndarray:
    """按弧长把折线加密到相邻点间距 ≤ step（不改变形状）。"""
    n = pts.shape[0]
    if n < 2 or step <= 0.0:
        return pts
    seg = np.linalg.norm(np.diff(pts[:, :2], axis=0), axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    total = float(cum[-1])
    if total <= 0.0 or total / step < n + 1:
        return pts
    m = int(min(total / step, _MAX_DENSE)) + 1
    uu = np.linspace(0.0, total, max(m, n))
    return np.stack([np.interp(uu, cum, pts[:, 0]),
                     np.interp(uu, cum, pts[:, 1])], axis=1)


def revolve_slope(hf, curve_pts: np.ndarray,
                  axis_p1, axis_p2,
                  height_scale: float = 1.0,
                  mode: str = "replace") -> None:
    """轮廓线绕轴旋转180°生成半圆穹顶高度场，就地写入 hf.data。

    参数：
    - hf: HeightField
    - curve_pts: (N, 2) 轮廓线世界坐标采样点（半边轮廓，大致平行于轴）
    - axis_p1, axis_p2: 旋转轴两端点 (x, y)
    - height_scale: 高度缩放系数（1.0=原样）
    - mode: "replace" 取代高度场 / "add" 叠加到现有高度 / "cut" 切除（现有高度减去旋转体高度）
    """
    if curve_pts is None or curve_pts.shape[0] < 2:
        return
    p1 = np.asarray(axis_p1, dtype=np.float64)[:2]
    p2 = np.asarray(axis_p2, dtype=np.float64)[:2]
    d = p2 - p1
    norm = float(np.linalg.norm(d))
    if norm < 1e-12:
        return
    u = d / norm

    # 1. 轮廓线 → (s, r) 映射：s=沿轴投影，r=到轴垂直距离（旋转半径）
    #    先按弧长加密到 ≤ 1/4 网格：命令只按固定点数采样轮廓，竖直段很密、
    #    近水平的内凹/回勾段很稀，稀的那几段会整行漏采样，于是同一行在
    #    "外轮廓 70 / 内勾 30" 之间来回跳，画出来就是横条纹（#76）。
    pts = _densify(curve_pts, hf.spacing * 0.25)
    rel = pts - p1
    s_curve = rel @ u
    r_curve = np.abs(rel[:, 0] * u[1] - rel[:, 1] * u[0])

    order = np.argsort(s_curve)
    s_sorted = s_curve[order]
    r_sorted = r_curve[order]

    # 1b. 去重：按约半个网格行高分箱，同一箱取最大 r（旋转面外轮廓，内凹填满）。
    # 不能用 round(s, 6) 去重：轮廓线有一段接近水平（内凹/回勾）时，上百个
    # 点几乎同 s 但 r 差别很大，插值会在这些近奇异点上高频抖动，生成整片
    # 横条纹（用户看到"乱了"）(#75)。
    binw = max(hf.spacing * 0.5, 1e-9)
    b = np.round(s_sorted / binw).astype(np.int64)
    ub, inv = np.unique(b, return_inverse=True)
    r_max = np.zeros(ub.shape[0], dtype=np.float64)
    np.maximum.at(r_max, inv, r_sorted)
    s_unique = ub * binw

    # 2. 网格点 → 沿轴投影 s' 和到轴垂直距离 d'
    ny, nx = hf.ny, hf.nx
    ox, oy = hf.origin
    sp = hf.spacing
    cols = np.arange(nx, dtype=np.float64)
    rows = np.arange(ny, dtype=np.float64)
    gx = ox + cols * sp
    gy = oy + rows * sp
    XX, YY = np.meshgrid(gx, gy)

    RX = XX - p1[0]
    RY = YY - p1[1]
    s_field = RX * u[0] + RY * u[1]
    d_field = np.abs(RX * u[1] - RY * u[0])

    # 3. 插值 r(s')，z = sqrt(r² - d²)
    if s_unique.shape[0] >= 2:
        r_field = np.interp(s_field, s_unique, r_max)
        # 严格限定在轮廓的 s 范围内：越界不生成，避免超出轮廓线端点
        in_range = (s_field >= s_unique[0]) & (s_field <= s_unique[-1])
    else:
        r_val = float(r_max[0]) if r_max.shape[0] > 0 else 0.0
        r_field = np.full_like(s_field, r_val)
        in_range = np.ones_like(s_field, dtype=bool)

    r2 = r_field ** 2
    d2 = d_field ** 2
    inside = in_range & (d2 <= r2) & (r2 > 1e-12)
    z_field = np.zeros_like(s_field)
    z_field[inside] = np.sqrt(np.maximum(r2[inside] - d2[inside], 0.0))
    z_field *= height_scale

    if mode == "add":
        hf.data[:] += z_field.astype(np.float32)
    elif mode == "cut":
        hf.data[:] -= z_field.astype(np.float32)
    else:
        hf.data[:] = z_field.astype(np.float32)

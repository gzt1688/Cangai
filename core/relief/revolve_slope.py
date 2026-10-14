"""旋转面：轮廓线绕轴旋转180°生成半圆穹顶高度场。

轮廓线定义沿轴各位置的旋转半径 r(s)；高度场某点到轴垂直距离 d，
则高度 z = sqrt(r(s)² - d²)（d≤r 处有值，d>r 处为 0）。
轴上点（d=0）高度最高 z=r，即轮廓线到轴的垂直距离。
"""
from __future__ import annotations
import numpy as np

__all__ = ["revolve_slope"]


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
    rel = curve_pts - p1
    s_curve = rel @ u
    r_curve = np.abs(rel[:, 0] * u[1] - rel[:, 1] * u[0])

    order = np.argsort(s_curve)
    s_sorted = s_curve[order]
    r_sorted = r_curve[order]

    # 去重：相同 s 取最大 r（旋转面外轮廓）
    s_unique, inv = np.unique(np.round(s_sorted, 6), return_inverse=True)
    r_max = np.zeros_like(s_unique)
    np.maximum.at(r_max, inv, r_sorted)

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

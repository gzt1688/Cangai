"""区域变形（封套变形 / Envelope Deformation）：把源区域内容按轮廓对应
均匀变形到目标轮廓区域。

算法：Mean Value Coordinates（MVC）反向映射。
- 源轮廓 src_poly 与目标轮廓 dst_poly 按周长等弧长采样到相同 N 点，建立
  顶点对应（第 i 个源点 ↔ 第 i 个目标点），起点用"相对质心最右点"对齐。
- 对目标区域每个格点 T，算 T 相对 dst_poly 顶点的 MVC 权重 w，则源坐标
  S = Σ w_i · src_poly[i]——这正是把 src_poly 内部"均匀映射"到 dst_poly
  内部的封套映射的反向映射（T 在 dst_poly 内 → S 在 src_poly 内）。
- 双线性采样源场在 S 处的高度，写入 T。反向映射保证目标区域完整填充无空洞。

参考：Hormann & Floater, "Mean Value Coordinates", CAGD 2006。
"""
from __future__ import annotations

import math

import numpy as np

from core.relief.region_relief import resample_polyline
from core.relief.sweep import _bilinear_sample_field


# ---------------- 轮廓对应 ----------------

def _east_intersection(poly: np.ndarray, centroid: np.ndarray):
    """从质心向正东（+x）射线与闭合多边形边的交点。

    返回 (边索引 i, 参数 t, 交点 xy)；无交点返回 None。
    使源/目标轮廓采样起点都落在"正东方向轮廓点"上：圆的右边中点 ↔
    矩形的右边中点，避免圆边中点对应矩形角点产生扭曲。
    """
    cx, cy = float(centroid[0]), float(centroid[1])
    n = poly.shape[0]
    best = None
    best_x = -np.inf
    for i in range(n):
        x1, y1 = float(poly[i, 0]), float(poly[i, 1])
        x2, y2 = float(poly[(i + 1) % n, 0]), float(poly[(i + 1) % n, 1])
        if abs(y2 - y1) < 1e-12:
            continue
        t = (cy - y1) / (y2 - y1)
        if 0.0 <= t <= 1.0:
            x = x1 + t * (x2 - x1)
            if x > cx and x > best_x:
                best_x = x
                best = (i, t, np.array([x, cy], dtype=np.float64))
    return best


def _resample_from_east(poly: np.ndarray, n: int) -> np.ndarray:
    """从正东方向轮廓点起，等弧长重采样闭合多边形为 n 个点（不含重复尾点）。

    对齐源/目标起点到各自"正东方向轮廓点"，使圆边中点对应矩形边中点而非
    角点，变形更均匀。正东射线无交点时回退到最右顶点。
    """
    if poly.shape[0] < 2 or n < 3:
        return poly
    if float(np.linalg.norm(poly[0] - poly[-1])) > 1e-9:
        poly = np.vstack([poly, poly[0:1]])
    centroid = poly[:-1].mean(axis=0)
    info = _east_intersection(poly[:-1], centroid)
    if info is not None:
        i, t, pt = info
        # 从交点起沿多边形正向重排：pt → v_{i+1} → ... → v_n → v_1 → ... → v_i → pt
        reordered = np.vstack([pt[None, :], poly[i + 1:], poly[1:i + 1],
                               pt[None, :]])
    else:
        i0 = int(np.argmax(poly[:-1, 0] - centroid[0]))
        reordered = np.vstack([poly[i0:], poly[1:i0 + 1]])
    pts = resample_polyline(reordered, n)  # (n+1, 2)，首尾重合
    return pts[:-1]  # (n, 2)


# ---------------- Mean Value Coordinates 反向映射 ----------------

def _mvc_block(pts: np.ndarray, src_poly: np.ndarray,
               dst_poly: np.ndarray) -> np.ndarray:
    """对一批目标点 pts (m,2) 算 MVC 反向映射源坐标 (m,2)。

    pts 须在 dst_poly 内或边上。dst_poly/src_poly: (N,2) 对应顶点。
    """
    # v[i] = dst_poly[i] - pts  -> (N, m, 2)
    v = dst_poly[:, None, :] - pts[None, :, :]
    r = np.linalg.norm(v, axis=2)  # (N, m)
    r = np.maximum(r, 1e-12)
    v_next = np.roll(v, -1, axis=0)  # v_{i+1}
    r_next = np.roll(r, -1, axis=0)
    # 有向面积 2A = v.x * v_next.y - v.y * v_next.x
    a2 = v[..., 0] * v_next[..., 1] - v[..., 1] * v_next[..., 0]
    # 点积 D = v · v_next
    d = v[..., 0] * v_next[..., 0] + v[..., 1] * v_next[..., 1]
    # tan(α_i/2) = sin α / (1 + cos α) = (2A/(r·r')) / (1 + D/(r·r'))
    #            = 2A / (r·r' + D)  ；w 用 tan(α/2) 比例，2 倍可约去 → A2/(r·r'+D)
    denom = r * r_next + d
    denom = np.where(np.abs(denom) < 1e-12, 1e-12, denom)
    tan_half = a2 / denom  # 边 i 的 tan(α_i/2)，(N, m)
    # w_i = (tan_half_{i-1} + tan_half_i) / r_i
    tan_half_prev = np.roll(tan_half, 1, axis=0)
    w = (tan_half_prev + tan_half) / r  # (N, m)
    # 归一化 λ_i = w_i / Σ w_j
    w_sum = w.sum(axis=0)
    w_sum = np.where(np.abs(w_sum) < 1e-12, 1e-12, w_sum)
    w_norm = w / w_sum[None, :]  # (N, m)
    # 源坐标 S = Σ_i λ_i · src_poly[i]  -> (m, 2) = w_norm.T @ src_poly
    S = w_norm.T @ src_poly  # (m, 2)
    # 边界顶点特判：p 接近某 dst 顶点时 MVC 数值不稳，直接取对应 src 顶点
    r_min = r.min(axis=0)
    is_vertex = r_min < 1e-6
    if is_vertex.any():
        idx = r.argmin(axis=0)
        S[is_vertex] = src_poly[idx[is_vertex]]
    return S


def _mvc_inverse_map(target_pts: np.ndarray, src_poly: np.ndarray,
                     dst_poly: np.ndarray, chunk: int = 4096) -> np.ndarray:
    """分块对 target_pts (M,2) 算 MVC 反向映射源坐标 (M,2)。"""
    m = target_pts.shape[0]
    if m == 0:
        return np.zeros((0, 2), dtype=np.float64)
    out = np.empty((m, 2), dtype=np.float64)
    for s in range(0, m, chunk):
        e = min(s + chunk, m)
        out[s:e] = _mvc_block(target_pts[s:e], src_poly, dst_poly)
    return out


# ---------------- 区域变形主入口 ----------------

def deform_region(hf, source: np.ndarray, base: np.ndarray,
                  src_poly: np.ndarray, dst_poly: np.ndarray,
                  dst_mask: np.ndarray, z_scale: float = 1.0,
                  n_samples: int = 128) -> int:
    """把源高度场 source 按封套变形 src_poly→dst_poly 写入目标轮廓区域。

    区域内容作为独立印章：原位置已在 base 中清空（source mask 内置 0），
    变形后内容覆盖目标轮廓 dst_mask 区域（直接取代），可移到模型任意位置。
    就地修改 hf.data。

    source: 完整源高度场（float64，采样用，任意位置都有值）；
    base:   where(src_mask, 0, source) 原位置清空的底场；
    src_poly/dst_poly: 源/目标轮廓世界坐标采样点 (K,2)；
    dst_mask: 目标轮廓内部掩膜 (ny,nx) bool；
    z_scale: 印章高度均匀缩放倍率。
    """
    if src_poly.shape[0] < 3 or dst_poly.shape[0] < 3:
        return 0
    ny, nx = hf.ny, hf.nx
    ox, oy = hf.origin
    sp = hf.spacing

    # 1. 轮廓对应：正东方向起点对齐 + 等弧长采样到相同 N 点
    src_pts = _resample_from_east(src_poly, n_samples)
    dst_pts = _resample_from_east(dst_poly, n_samples)

    # 2. 目标区域外接矩形
    rows = np.any(dst_mask, axis=1)
    cols = np.any(dst_mask, axis=0)
    if not rows.any() or not cols.any():
        return 0
    row_lo = int(np.argmax(rows))
    row_hi = int(len(rows) - 1 - np.argmax(rows[::-1]))
    col_lo = int(np.argmax(cols))
    col_hi = int(len(cols) - 1 - np.argmax(cols[::-1]))
    sub_mask = dst_mask[row_lo:row_hi + 1, col_lo:col_hi + 1]
    inside = sub_mask
    if not inside.any():
        return 0

    # 3. 目标格点世界坐标
    ys = (oy + np.arange(row_lo, row_hi + 1, dtype=np.float64) * sp)[:, None]
    xs = (ox + np.arange(col_lo, col_hi + 1, dtype=np.float64) * sp)[None, :]
    tx_full = np.broadcast_to(xs, inside.shape)
    ty_full = np.broadcast_to(ys, inside.shape)
    pts_t = np.column_stack([tx_full[inside], ty_full[inside]])  # (M, 2)

    # 4. MVC 反向映射：目标格 → 源坐标
    src_coords = _mvc_inverse_map(pts_t, src_pts, dst_pts)  # (M, 2)

    # 5. 双线性采样源场
    full_sx = np.zeros(inside.shape, dtype=np.float64)
    full_sy = np.zeros(inside.shape, dtype=np.float64)
    full_sx[inside] = src_coords[:, 0]
    full_sy[inside] = src_coords[:, 1]
    sampled = _bilinear_sample_field(source, full_sx, full_sy, ox, oy, sp)

    # 6. 写入：目标 mask 内取采样值 * z_scale，mask 外保持 base
    base_sub = base[row_lo:row_hi + 1, col_lo:col_hi + 1]
    new_vals = np.where(inside, sampled * z_scale, base_sub)
    hf.data[row_lo:row_hi + 1, col_lo:col_hi + 1] = new_vals.astype(np.float32)
    return int(inside.sum())
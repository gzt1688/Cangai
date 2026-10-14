"""区域阵列：把选定区域（闭合曲线内或色块）的高度场内容作为印章，
按矩形/圆形/曲线三种模式阵列分布多个副本。

区域识别同"区域变换"（闭合曲线内或同色色块）；阵列分布参考"曲线阵列"
（矩形/圆形/曲线三种模式）；沿程渐变借鉴"路径塑形"的 _width_scale
（等粗/中粗/首粗/尾粗 + 细端百分比）——副本沿阵列顺序缩放，实现
首大尾小/中间粗两端细等渐变阵列。

复制语义：原位置保留（阵列是复制不是剪切），副本写入目标位置直接
取代该处内容；副本重叠时后者覆盖前者。预览时每帧从 base 全量恢复
整个场再写所有副本（无残留）。

坐标约定与 HeightField 一致：data[row, col]，行 0 = minY、列 0 = minX。
纯算法、零 UI 依赖、纯 numpy。
"""
from __future__ import annotations

import math
from typing import List, Optional, Tuple

import numpy as np

from core.relief.sweep import _bilinear_sample_field


# ==================== 沿程渐变缩放 ====================

def _width_scale_scalar(t: float, brush_type: str, thin_frac: float) -> float:
    """单值版沿程缩放 w(t)∈[m,1]（t=顺序比例 0..1，m=细端比例）。"""
    t = max(0.0, min(1.0, t))
    m = max(0.0, min(1.0, thin_frac))
    wide = 1.0 - m
    if brush_type == "middle":
        return m + wide * (1.0 - abs(2.0 * t - 1.0))
    if brush_type == "head":
        return 1.0 - wide * t
    if brush_type == "tail":
        return m + wide * t
    return 1.0


# ==================== 曲线弧长工具 ====================

def _curve_total_length(pts: np.ndarray) -> float:
    if pts.shape[0] < 2:
        return 0.0
    return float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum())


def _length_to_point(pts: np.ndarray, target_len: float
                     ) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    n = pts.shape[0]
    if n < 2:
        return None
    cumul = 0.0
    for i in range(n - 1):
        seg = pts[i + 1] - pts[i]
        seg_len = float(np.linalg.norm(seg))
        if cumul + seg_len >= target_len:
            if seg_len < 1e-9:
                return pts[i].copy(), np.array([1.0, 0.0])
            t = max(0.0, min(1.0, (target_len - cumul) / seg_len))
            p = pts[i] + seg * t
            tan = seg / seg_len
            return p, tan
        cumul += seg_len
    return None


def _target_lengths(total: float, count: int, spacing: float,
                    fill: bool, start_pct: float) -> List[float]:
    if total < 1e-6 or count < 1:
        return []
    start_len = total * max(0.0, min(100.0, start_pct)) / 100.0
    out: List[float] = []
    if fill:
        if count < 2:
            return [start_len] if 0 <= start_len <= total else []
        step = (total - start_len) / (count - 1)
        for i in range(count):
            out.append(start_len + i * step)
    else:
        if spacing <= 0:
            return []
        for i in range(count):
            l = start_len + i * spacing
            if l <= total + 1e-6:
                out.append(l)
    return out


# ==================== 印章写入 ====================

def _stamp_to_field(hf, stamp: np.ndarray, stamp_mask: np.ndarray,
                    M: np.ndarray, src_bbox: Tuple[int, int, int, int],
                    z_scale: float) -> int:
    """把印章按仿射 M 写入高度场目标位置（只写印章内，不影响印章外）。

    就地修改 hf.data。返回覆盖格数。
    """
    ny, nx = hf.ny, hf.nx
    ox, oy = hf.origin
    sp = hf.spacing
    col_lo, row_lo, col_hi, row_hi = src_bbox

    inv_M = np.linalg.inv(M)
    corners = []
    for r, c in ((row_lo, col_lo), (row_lo, col_hi),
                 (row_hi, col_lo), (row_hi, col_hi)):
        x = ox + c * sp
        y = oy + r * sp
        wx = M[0, 0] * x + M[0, 1] * y + M[0, 2]
        wy = M[1, 0] * x + M[1, 1] * y + M[1, 2]
        corners.append((wx, wy))
    wxs = [c[0] for c in corners]
    wys = [c[1] for c in corners]
    a_col_lo = max(0, int(math.floor((min(wxs) - sp - ox) / sp)))
    a_col_hi = min(nx - 1, int(math.ceil((max(wxs) + sp - ox) / sp)))
    a_row_lo = max(0, int(math.floor((min(wys) - sp - oy) / sp)))
    a_row_hi = min(ny - 1, int(math.ceil((max(wys) + sp - oy) / sp)))
    if a_col_lo > a_col_hi or a_row_lo > a_row_hi:
        return 0

    ys = (oy + np.arange(a_row_lo, a_row_hi + 1, dtype=np.float64) * sp)[:, None]
    xs = (ox + np.arange(a_col_lo, a_col_hi + 1, dtype=np.float64) * sp)[None, :]
    src_x = inv_M[0, 0] * xs + inv_M[0, 1] * ys + inv_M[0, 2]
    src_y = inv_M[1, 0] * xs + inv_M[1, 1] * ys + inv_M[1, 2]

    trans_stamp = _bilinear_sample_field(stamp, src_x, src_y, ox, oy, sp)
    trans_mask = _bilinear_sample_field(stamp_mask, src_x, src_y, ox, oy, sp)
    cov = trans_mask > 0.5
    if not cov.any():
        return 0
    region = hf.data[a_row_lo:a_row_hi + 1, a_col_lo:a_col_hi + 1]
    region[cov] = (trans_stamp * z_scale)[cov].astype(np.float32)
    return int(cov.sum())


def _make_M(cx: float, cy: float, tx: float, ty: float,
            rot: float, sc: float) -> np.ndarray:
    """仿射矩阵 M = T(target) · R(rot) · S(sc) · T(-center)。

    印章中心 (cx,cy) 经缩放 sc、旋转 rot 后平移到 (tx,ty)。
    """
    c, s = math.cos(rot), math.sin(rot)
    cs, ss = c * sc, s * sc
    return np.array([
        [cs, -ss, tx - cs * cx + ss * cy],
        [ss, cs, ty - ss * cx - cs * cy],
        [0.0, 0.0, 1.0],
    ], dtype=np.float64)


# ==================== 矩形阵列 ====================

def array_region_rectangular(hf, stamp, stamp_mask, src_bbox, center,
                             count_x: int, count_y: int,
                             gap_x: float, gap_y: float,
                             brush_type: str, thin_frac: float,
                             z_scale: float) -> int:
    """矩形阵列：以区域中心为基准，按 X/Y 图形边缘间距网格分布副本。

    gap_x/gap_y 为图形与图形之间的边缘距离（非中心间距），
    步距 = 印章尺寸 + 间距。跳过 (0,0) 原始位置。
    """
    if stamp is None or count_x < 1 or count_y < 1:
        return 0
    cx, cy = center
    sp = hf.spacing
    stamp_w = (src_bbox[2] - src_bbox[0]) * sp
    stamp_h = (src_bbox[3] - src_bbox[1]) * sp
    step_x = stamp_w + gap_x
    step_y = stamp_h + gap_y
    total = max(count_x * count_y - 1, 1)
    moved = 0
    idx = 0
    for i in range(count_x):
        for j in range(count_y):
            if i == 0 and j == 0:
                idx += 1
                continue
            t = idx / total
            w = _width_scale_scalar(t, brush_type, thin_frac)
            if w < 1e-3:
                idx += 1
                continue
            tx = cx + i * step_x
            ty = cy + j * step_y
            M = _make_M(cx, cy, tx, ty, 0.0, w)
            moved += _stamp_to_field(hf, stamp, stamp_mask, M, src_bbox,
                                     z_scale * w)
            idx += 1
    return moved


# ==================== 圆形阵列 ====================

def array_region_circular(hf, stamp, stamp_mask, src_bbox, center,
                          center_base: Tuple[float, float],
                          count: int,
                          angle_range: float,
                          rotate_objects: bool,
                          brush_type: str, thin_frac: float,
                          z_scale: float) -> int:
    """圆形阵列：区域本身作为第一个对象，其余副本沿圆周分布。

    半径 = 圆心到区域中心的距离（自动）；起始角 = 圆心到区域中心方向
    （使 i=0 恰在原位置）。i=0 跳过（原位置保留=区域本身），i=1..count-1
    沿圆周均匀分布。rotate_objects: 副本随角度旋转。
    """
    if stamp is None or count < 2:
        return 0
    cx, cy = center
    bx, by = center_base
    dx = cx - bx
    dy = cy - by
    radius = math.hypot(dx, dy)
    if radius < 1e-6:
        return 0
    start_rad = math.atan2(dy, dx)
    step = math.radians(angle_range) / count
    total = max(count - 1, 1)
    moved = 0
    for i in range(count):
        if i == 0:
            continue
        t = i / total
        w = _width_scale_scalar(t, brush_type, thin_frac)
        if w < 1e-3:
            continue
        ang = start_rad + i * step
        tx = bx + radius * math.cos(ang)
        ty = by + radius * math.sin(ang)
        rot = (ang - start_rad) if rotate_objects else 0.0
        M = _make_M(cx, cy, tx, ty, rot, w)
        moved += _stamp_to_field(hf, stamp, stamp_mask, M, src_bbox,
                                 z_scale * w)
    return moved


# ==================== 曲线阵列 ====================

def array_region_along_curve(hf, stamp, stamp_mask, src_bbox, center,
                             curve_pts: np.ndarray,
                             count: int, spacing: float,
                             fill: bool, start_pct: float,
                             rotate_tangent: bool, on_curve: bool,
                             brush_type: str, thin_frac: float,
                             z_scale: float) -> int:
    """沿曲线阵列区域印章。原位置保留（复制语义）。"""
    if stamp is None or curve_pts is None or curve_pts.shape[0] < 2 or count < 1:
        return 0
    total = _curve_total_length(curve_pts)
    if total < 1e-6:
        return 0
    target_lens = _target_lengths(total, count, spacing, fill, start_pct)
    if not target_lens:
        return 0

    samples: List[Tuple[np.ndarray, np.ndarray]] = []
    for l in target_lens:
        res = _length_to_point(curve_pts, l)
        if res is not None:
            samples.append(res)
    if not samples:
        return 0

    cx, cy = center
    start_pt = samples[0][0]
    offset_x = cx - start_pt[0]
    offset_y = cy - start_pt[1]
    start_angle = math.atan2(samples[0][1][1], samples[0][1][0])
    n_samples = len(samples)

    moved = 0
    for i, (pt, tan) in enumerate(samples):
        t = i / max(n_samples - 1, 1)
        w = _width_scale_scalar(t, brush_type, thin_frac)
        if w < 1e-3:
            continue
        if on_curve:
            tx, ty = float(pt[0]), float(pt[1])
        else:
            tx, ty = float(pt[0] + offset_x), float(pt[1] + offset_y)
        rot = 0.0
        if rotate_tangent and i > 0:
            rot = math.atan2(float(tan[1]), float(tan[0])) - start_angle
        M = _make_M(cx, cy, tx, ty, rot, w)
        moved += _stamp_to_field(hf, stamp, stamp_mask, M, src_bbox,
                                 z_scale * w)
    return moved


# ==================== 统一预览 ====================

def preview_array(hf, source: np.ndarray, stamp, stamp_mask, src_bbox,
                  center, mode: int, params: dict) -> int:
    """预览：从 source 全量恢复高度场，再按模式写入所有阵列副本（无残留）。

    mode: 0=矩形 / 1=圆形 / 2=曲线
    params: 对应模式的参数字典。
    """
    hf.data[:] = source
    bt = params.get("brush_type", "uniform")
    tf = params.get("thin_frac", 0.0)
    zs = params.get("z_scale", 1.0)
    if mode == 0:
        return array_region_rectangular(
            hf, stamp, stamp_mask, src_bbox, center,
            params["count_x"], params["count_y"],
            params["gap_x"], params["gap_y"], bt, tf, zs)
    if mode == 1:
        return array_region_circular(
            hf, stamp, stamp_mask, src_bbox, center,
            params["center_base"], params["count"],
            params["angle_range"],
            params["rotate_objects"], bt, tf, zs)
    return array_region_along_curve(
        hf, stamp, stamp_mask, src_bbox, center, params["curve_pts"],
        params["count"], params["spacing"], params["fill"],
        params["start_pct"], params["rotate_tangent"], params["on_curve"],
        bt, tf, zs)

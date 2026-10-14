"""自动分色：识别全部选中曲线围成的闭合区域（平面分割）。

不再只认"闭合曲线"——任意曲线（含开放直线）相交、分割形成的所有闭合
区域都要识别出来。例如一个圆被一条直线横穿 → 圆被分成 2 个闭合区域，
两块各填一种颜色。

算法（栅格法，与色号蒙版同栅格、天然对齐）：
1. 把每条曲线按弧长加密到约 0.5 格步距，逐点标记所在格子为"障碍"
   （1 格厚的曲线墙；采样链天然 8 连通，配合 4 连通洪泛不会对角泄漏）；
2. 对"非障碍"格子做 4 连通连通域标记；
3. 触到模型栅格边界的连通域 = 无界外部；其余连通域即为闭合区域；
4. 曲线压线格子归入 2 格内最近的区域（一并上色，避免线上残留杂色），
   够不着任何区域的线格清 0。

纯算法、零 UI 依赖（scipy 懒加载）。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

import numpy as np


def _rasterize_barriers(hf, curves_sampled: List[np.ndarray]) -> np.ndarray:
    """把各曲线的密集采样点逐点标记到栅格，生成 1 格厚障碍掩膜。"""
    ox, oy = hf.origin
    sp = hf.spacing
    barrier = np.zeros((hf.ny, hf.nx), dtype=bool)
    for pts in curves_sampled:
        if pts.shape[0] < 2:
            continue
        cols = np.round((pts[:, 0] - ox) / sp).astype(np.int64)
        rows = np.round((pts[:, 1] - oy) / sp).astype(np.int64)
        ok = (rows >= 0) & (rows < hf.ny) & (cols >= 0) & (cols < hf.nx)
        barrier[rows[ok], cols[ok]] = True
    return barrier


def _densify(obj, spacing: float) -> np.ndarray:
    """曲线采样点按弧长加密到约 0.5 格步距（保证标记链连续）。"""
    from core.relief.region_relief import resample_polyline, sample_curve_points

    pts = sample_curve_points(obj, 64)
    if pts.shape[0] < 2:
        return pts
    length = float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum())
    n = max(2, int(np.ceil(length / max(spacing * 0.5, 1e-9))))
    return resample_polyline(pts, n)


def compute_enclosed_regions(hf, curves: list, min_area: int = 4) -> List[np.ndarray]:
    """识别全部曲线围成的闭合区域，返回 bool 掩膜列表（与高度场同形状）。

    curves: 任意曲线对象列表（闭合/开放均可），世界坐标。
    min_area: 最小区域格数——栅格量化会把"两曲线极近处的通道"偶尔封成
    1~3 格的伪区域，低于该面积的碎屑丢弃（默认 4 格）。
    区域互不重叠、不含曲线压线的格子；无闭合区域返回空列表。
    """
    from scipy.ndimage import label

    if hf.ny < 3 or hf.nx < 3 or not curves:
        return []

    sampled = [_densify(obj, hf.spacing) for obj in curves]
    barrier = _rasterize_barriers(hf, sampled)

    free_labels, n = label(~barrier)          # 4 连通：对角障碍链不泄漏
    if n == 0:
        return []
    # 触边的连通域 = 无界外部
    border = np.concatenate([free_labels[0, :], free_labels[-1, :],
                             free_labels[:, 0], free_labels[:, -1]])
    outside = {int(v) for v in np.unique(border) if v != 0}

    masks: List[np.ndarray] = []
    for lab in range(1, n + 1):
        if lab in outside:
            continue
        mask = free_labels == lab
        if int(mask.sum()) >= max(1, min_area):
            masks.append(mask)
    return masks


def compute_region_colors(hf, curves: list, palette_count: int,
                          min_area: int = 4
                          ) -> Tuple[Optional[np.ndarray], int]:
    """自动分色的最终色号图：(value_map, region_count)。

    value_map: (ny, nx) uint8——区域内格子 = 该区域色号（按面积外无关联、
    按识别顺序循环取 1..palette_count 色）；**曲线压线格子取 2 格内最近
    区域的颜色**（避免线上残留旧颜色形成杂色），够不着任何区域的线格
    与无界外部 = 0（未填色）。未识别到闭合区域返回 (None, 0)。
    """
    masks = compute_enclosed_regions(hf, curves, min_area)
    if not masks:
        return None, 0

    region_id = np.zeros((hf.ny, hf.nx), dtype=np.int32)
    for i, mask in enumerate(masks):
        region_id[mask] = i + 1

    # 曲线压线格子：取 2 格内最近区域的颜色（EDT 最近区域格索引）
    from scipy.ndimage import distance_transform_edt

    barrier = _rasterize_barriers(
        hf, [_densify(obj, hf.spacing) for obj in curves])
    line_cells = barrier & (region_id == 0)
    if line_cells.any():
        _, (iy, ix) = distance_transform_edt(region_id == 0,
                                             return_indices=True)
        nearest = region_id[iy, ix]
        take = line_cells & (nearest > 0)
        # EDT 全场距离：压线格到最近区域格 ≤ 2 格才认领（防外部远处线段被染色）
        dist = np.hypot(iy - np.arange(hf.ny)[:, None],
                        ix - np.arange(hf.nx)[None, :])
        take &= dist <= 2.0
        region_id[take] = nearest[take]

    # 区域序号循环映射到调色板色号（1..palette_count）；0 保持未填色
    values = np.where(region_id > 0,
                      (region_id - 1) % max(1, palette_count) + 1,
                      0).astype(np.uint8)
    return values, len(masks)

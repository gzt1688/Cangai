"""区域对称：把高度场内容关于对称轴镜像到另一侧。

左右对称：以 X=axis_x 为竖直对称轴。
上下对称：以 Y=axis_y 为水平对称轴。
自由对称：以 (p1x,p1y)→(p2x,p2y) 两点连线为任意角度对称轴（仅区域模式）。

作用范围：
- model（整个模型）：对所有格子做对称，由 keep_side 决定保留侧。
- region（区域）：把 mask 内的内容镜像到对称轴另一侧（mask 内保持原值，
  只写入 mask 外的镜像目标格），不需要 keep_side。

预览时每帧从 source 全量恢复再应用对称（无残留）。
"""
from __future__ import annotations

import numpy as np


def symmetry_field(hf, source: np.ndarray,
                   mask: np.ndarray | None,
                   axis_x: float, axis_y: float,
                   direction: str, scope: str,
                   keep_side: str = "left",
                   p2x: float = 0.0, p2y: float = 0.0) -> int:
    hf.data[:] = source
    data = hf.data
    ox, oy = hf.origin
    sp = hf.spacing
    ny, nx = hf.ny, hf.nx

    if direction == "free":
        return _symmetry_free(hf, source, mask, axis_x, axis_y,
                              p2x, p2y, scope)

    if direction == "left_right":
        col0 = (axis_x - ox) / sp
        if scope == "model":
            cols = np.arange(nx)
            col_mirror = np.round(2.0 * col0 - cols).astype(np.int64)
            if keep_side == "left":
                is_target = cols > col0
            else:
                is_target = cols < col0
            src_cols = col_mirror
            valid = (src_cols >= 0) & (src_cols < nx) & is_target
            tgt_cols = np.nonzero(valid)[0]
            if tgt_cols.size > 0:
                data[:, tgt_cols] = data[:, src_cols[tgt_cols]]
        elif mask is not None:
            rows_in, cols_in = np.nonzero(mask)
            cols_m = np.round(2.0 * col0 - cols_in).astype(np.int64)
            valid = (cols_m >= 0) & (cols_m < nx)
            rv = rows_in[valid]
            cv = cols_in[valid]
            mv = cols_m[valid]
            outside = ~mask[rv, mv]
            data[rv[outside], mv[outside]] = source[rv[outside], cv[outside]]
    else:
        row0 = (axis_y - oy) / sp
        if scope == "model":
            rows = np.arange(ny)
            row_mirror = np.round(2.0 * row0 - rows).astype(np.int64)
            if keep_side == "top":
                is_target = rows > row0
            else:
                is_target = rows < row0
            src_rows = row_mirror
            valid = (src_rows >= 0) & (src_rows < ny) & is_target
            tgt_rows = np.nonzero(valid)[0]
            if tgt_rows.size > 0:
                data[tgt_rows, :] = data[src_rows[tgt_rows], :]
        elif mask is not None:
            rows_in, cols_in = np.nonzero(mask)
            rows_m = np.round(2.0 * row0 - rows_in).astype(np.int64)
            valid = (rows_m >= 0) & (rows_m < ny)
            rv = rows_in[valid]
            cv = cols_in[valid]
            mv = rows_m[valid]
            outside = ~mask[mv, cv]
            data[mv[outside], cv[outside]] = source[rv[outside], cv[outside]]

    return int(np.count_nonzero(data != source))


def _symmetry_free(hf, source: np.ndarray, mask: np.ndarray | None,
                   p1x: float, p1y: float, p2x: float, p2y: float,
                   scope: str) -> int:
    """自由对称（任意角度轴）。

    用反向映射避免空洞：遍历目标区域每个格子，反向镜像找源点，
    源点在 mask 内则写入。正向映射（源→目标）在任意角度下取整
    会导致稀疏覆盖（多源点映射到同一目标格、部分目标格无源点），
    产生密密麻麻的空洞。
    """
    data = hf.data
    ox, oy = hf.origin
    sp = hf.spacing
    ny, nx = hf.ny, hf.nx

    dx = p2x - p1x
    dy = p2y - p1y
    L = float(np.hypot(dx, dy))
    if L < 1e-9:
        return 0
    dx /= L
    dy /= L

    if mask is None:
        return 0

    rows_in, cols_in = np.nonzero(mask)
    Xin = ox + cols_in * sp
    Yin = oy + rows_in * sp
    PinX = Xin - p1x
    PinY = Yin - p1y
    dotin = PinX * dx + PinY * dy
    Xmin = 2.0 * p1x + 2.0 * dotin * dx - Xin
    Ymin = 2.0 * p1y + 2.0 * dotin * dy - Yin
    mc_fwd = (Xmin - ox) / sp
    mr_fwd = (Ymin - oy) / sp

    r_min = max(0, int(np.floor(mr_fwd.min())) - 1)
    r_max = min(ny, int(np.ceil(mr_fwd.max())) + 2)
    c_min = max(0, int(np.floor(mc_fwd.min())) - 1)
    c_max = min(nx, int(np.ceil(mc_fwd.max())) + 2)
    if r_min >= r_max or c_min >= c_max:
        return 0

    rr, cc = np.indices((r_max - r_min, c_max - c_min), dtype=np.int64)
    rr += r_min
    cc += c_min

    outside = ~mask[rr, cc]
    rr_out = rr[outside]
    cc_out = cc[outside]

    X = ox + cc_out * sp
    Y = oy + rr_out * sp
    PX = X - p1x
    PY = Y - p1y
    dot = PX * dx + PY * dy
    Xm = 2.0 * p1x + 2.0 * dot * dx - X
    Ym = 2.0 * p1y + 2.0 * dot * dy - Y
    src_c = np.round((Xm - ox) / sp).astype(np.int64)
    src_r = np.round((Ym - oy) / sp).astype(np.int64)

    valid = (src_c >= 0) & (src_c < nx) & (src_r >= 0) & (src_r < ny)
    rr_out = rr_out[valid]
    cc_out = cc_out[valid]
    src_r = src_r[valid]
    src_c = src_c[valid]

    in_mask = mask[src_r, src_c]
    data[rr_out[in_mask], cc_out[in_mask]] = source[src_r[in_mask], src_c[in_mask]]

    return int(np.count_nonzero(data != source))

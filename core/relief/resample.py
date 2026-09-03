"""高度场仿射重采样（供 ReliefModelObject 的"重构"烘焙使用）。

纯算法、零 UI 依赖。scipy 懒加载（与 spline_utils 一致：不装也能 import 本模块，
只有真正调用重采样时才需要 scipy）。

坐标约定与 HeightField 一致：
- 世界 mm；栅格 data[row, col]，row=Y 方向、col=X 方向，行0=minY、列0=minX。
- 放置仿射 ``M`` 把 local(=源网格自身世界坐标) 映射到 world：``world = M · [lx, ly, 1]``。
- 烘焙即在目标世界 AABB 上建轴对齐网格，对每个目标节点用 ``M^{-1}`` 反投回 local，
  再在源网格上双线性采样。
"""
from __future__ import annotations

from typing import Tuple

import numpy as np


def resample_affine(src_hf, inv_M: np.ndarray, dst_origin: Tuple[float, float],
                    dst_nx: int, dst_ny: int, dst_spacing: float) -> np.ndarray:
    """在目标网格上通过逆仿射 ``inv_M`` 反采样源高度场，返回 float32 (dst_ny, dst_nx)。

    src_hf: 源 HeightField（其 origin/spacing 即 local 坐标系）。
    inv_M: 3x3 逆仿射（world -> local）。
    dst_origin/dst_nx/dst_ny/dst_spacing: 目标轴对齐网格定义（世界 mm）。
    源网格范围外填 0（= 基面高度）。
    """
    from scipy.ndimage import map_coordinates

    inv_M = np.asarray(inv_M, dtype=np.float64)
    dst_nx = max(2, int(dst_nx))
    dst_ny = max(2, int(dst_ny))
    dst_spacing = float(dst_spacing)

    gx = float(dst_origin[0]) + np.arange(dst_nx, dtype=np.float64) * dst_spacing
    gy = float(dst_origin[1]) + np.arange(dst_ny, dtype=np.float64) * dst_spacing
    X, Y = np.meshgrid(gx, gy)                       # (ny, nx) 目标节点世界坐标

    # world -> local
    lx = inv_M[0, 0] * X + inv_M[0, 1] * Y + inv_M[0, 2]
    ly = inv_M[1, 0] * X + inv_M[1, 1] * Y + inv_M[1, 2]

    # local -> 源栅格分数索引 (col, row)
    sx0, sy0 = src_hf.origin
    ssp = float(src_hf.spacing)
    col_f = (lx - sx0) / ssp
    row_f = (ly - sy0) / ssp

    out = map_coordinates(src_hf.data, [row_f, col_f], order=1,
                          mode="constant", cval=0.0)
    return np.ascontiguousarray(out, dtype=np.float32)


def resample_affine_nearest(src_arr: np.ndarray, src_origin: Tuple[float, float],
                            src_spacing: float, inv_M: np.ndarray,
                            dst_origin: Tuple[float, float], dst_nx: int,
                            dst_ny: int, dst_spacing: float) -> np.ndarray:
    """在目标网格上通过逆仿射 ``inv_M`` 以**最近邻**反采样源整数数组，返回
    uint8 (dst_ny, dst_nx)。

    与 :func:`resample_affine` 同构，但 order=0（最近邻）：调色板索引是
    类别值，双线性插值会产生非法的中间索引，故烘焙色号蒙版必须最近邻。
    源网格范围外填 0（= 未填色）。

    src_arr: 源二维数组 (ny, nx)（如色号蒙版）；src_origin/src_spacing: 源栅格
    的 (minX, minY) 与步长（与源 HeightField 一致）。
    """
    from scipy.ndimage import map_coordinates

    inv_M = np.asarray(inv_M, dtype=np.float64)
    dst_nx = max(2, int(dst_nx))
    dst_ny = max(2, int(dst_ny))
    dst_spacing = float(dst_spacing)

    gx = float(dst_origin[0]) + np.arange(dst_nx, dtype=np.float64) * dst_spacing
    gy = float(dst_origin[1]) + np.arange(dst_ny, dtype=np.float64) * dst_spacing
    X, Y = np.meshgrid(gx, gy)                       # (ny, nx) 目标节点世界坐标

    # world -> local
    lx = inv_M[0, 0] * X + inv_M[0, 1] * Y + inv_M[0, 2]
    ly = inv_M[1, 0] * X + inv_M[1, 1] * Y + inv_M[1, 2]

    # local -> 源栅格分数索引 (col, row)
    sx0, sy0 = float(src_origin[0]), float(src_origin[1])
    ssp = float(src_spacing)
    col_f = (lx - sx0) / ssp
    row_f = (ly - sy0) / ssp

    out = map_coordinates(np.asarray(src_arr), [row_f, col_f], order=0,
                          mode="constant", cval=0.0)
    return np.ascontiguousarray(out.astype(np.uint8))

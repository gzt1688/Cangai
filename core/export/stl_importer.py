"""STL 导入：读取二进制/ASCII STL 三角网格，按最大 Z 投影光栅化为高度场，
生成浮雕模型（ReliefModelObject）合并进当前文档。

本软件是 2.5D 浮雕系统：任意三角网格 → 规则栅格高度场（Z 向上、单值曲面）。
光栅化取每个栅格节点处覆盖三角形的最高 Z（max-Z 深度缓冲），未覆盖区域为
基准面 z=0；竖直面/退化三角形（XY 投影面积≈0）跳过。

整批导入由 beginEdit/endEdit 包裹为一条撤销记录；模型自动归入“模型”固定层。
"""
from __future__ import annotations

import os
import re
import struct

import numpy as np

from ..relief.height_field import HeightField
from ..vector.relief_model_object import ReliefModelObject

# 光栅化栅格长边顶点数上限（与导出封顶同量级；36 万格 → 内存/交互安全）
IMPORT_MAX_DIM = 600
# 单批三角形向量化展开的最大“三角形×栅格节点”条目数；超过则逐三角形回退
# （防止病态网格：少量三角形却覆盖整个包围盒导致展开数组爆炸）
_CHUNK_TRIS = 4096
_MAX_ENTRIES = 20_000_000
_EPS = 1e-9

_VERTEX_RE = re.compile(
    rb"vertex\s+([-+\d.eE]+)\s+([-+\d.eE]+)\s+([-+\d.eE]+)")


def _readBinarySTL(data: bytes):
    """按二进制 STL 解析；尺寸/计数不自洽返回 None。"""
    if len(data) < 84:
        return None
    (count,) = struct.unpack("<I", data[80:84])
    if len(data) != 84 + 50 * count:
        return None
    arr = np.frombuffer(data, dtype=np.uint8, count=50 * count, offset=84)
    # 每面 50 字节：12 法线 + 36 三顶点 + 2 属性；只取三顶点（字节偏移 12..48）
    base = arr.reshape(count, 50)[:, 12:48].copy()
    floats = base.view(np.float32).reshape(count, 9)
    return floats.reshape(count, 3, 3).astype(np.float64)


def _readAsciiSTL(data: bytes):
    """按 ASCII STL 解析（正则提取 vertex 三元组）；无有效面返回 None。"""
    vals = _VERTEX_RE.findall(data)
    if len(vals) < 3 or len(vals) % 3 != 0:
        return None
    arr = np.array([[float(a), float(b), float(c)] for a, b, c in vals],
                   dtype=np.float64)
    return arr.reshape(-1, 3, 3)


def readSTL(path: str) -> np.ndarray:
    """读取 STL 文件 → (T,3,3) float64 三角形数组；自动识别二进制/ASCII。"""
    with open(path, "rb") as f:
        data = f.read()
    tris = _readBinarySTL(data)
    if tris is None:
        tris = _readAsciiSTL(data)
    if tris is None or len(tris) == 0:
        raise ValueError("STL 文件中没有有效的三角面")
    return tris


def _rasterizeChunk(chunk: np.ndarray, grid: np.ndarray,
                    x0: float, y0: float, sp: float,
                    nx: int, ny: int) -> int:
    """把一批三角形 max-Z 光栅化进 grid(ny,nx)；返回跳过的退化面数。

    向量化策略：按每三角形 XY 包围盒覆盖到的栅格节点展开，再用重心坐标判定
    节点是否在面内并插值 Z。展开条目超上限时逐三角形回退（防病态大面）。
    """
    P = chunk[:, :, :2]                       # (t,3,2)
    Z = chunk[:, :, 2]                        # (t,3)
    t = chunk.shape[0]

    minx = P[:, :, 0].min(axis=1)
    maxx = P[:, :, 0].max(axis=1)
    miny = P[:, :, 1].min(axis=1)
    maxy = P[:, :, 1].max(axis=1)

    c0 = np.floor((minx - x0) / sp - 0.5).astype(np.int64)
    c1 = np.ceil((maxx - x0) / sp + 0.5).astype(np.int64)
    r0 = np.floor((miny - y0) / sp - 0.5).astype(np.int64)
    r1 = np.ceil((maxy - y0) / sp + 0.5).astype(np.int64)
    np.clip(c0, 0, nx - 1, out=c0)
    np.clip(c1, 0, nx - 1, out=c1)
    np.clip(r0, 0, ny - 1, out=r0)
    np.clip(r1, 0, ny - 1, out=r1)

    A = P[:, 0]
    B = P[:, 1]
    C = P[:, 2]
    den = ((B[:, 1] - C[:, 1]) * (A[:, 0] - C[:, 0])
           + (C[:, 0] - B[:, 0]) * (A[:, 1] - C[:, 1]))
    valid = np.abs(den) > 1e-12
    skipped = int((~valid).sum())

    counts = np.where(valid & (c1 >= c0) & (r1 >= r0),
                      (r1 - r0 + 1) * (c1 - c0 + 1), 0).astype(np.int64)
    total = int(counts.sum())
    if total == 0:
        return skipped

    if total <= _MAX_ENTRIES:
        starts = np.concatenate([[0], counts.cumsum()[:-1]])
        tri_idx = np.repeat(np.arange(t), counts)
        local = np.arange(total) - np.repeat(starts, counts)
        widths = np.repeat((c1 - c0 + 1), counts)
        cc = np.repeat(c0, counts) + local % widths
        rr = np.repeat(r0, counts) + local // widths
        _updateGrid(grid, tri_idx, rr, cc, A, B, C, Z, den, x0, y0, sp)
    else:
        # 病态批次：逐三角形小数组运算
        for i in range(t):
            if not valid[i] or counts[i] == 0:
                continue
            cc_g, rr_g = np.meshgrid(
                np.arange(c0[i], c1[i] + 1), np.arange(r0[i], r1[i] + 1))
            cc_f = cc_g.ravel()
            rr_f = rr_g.ravel()
            _updateGrid(grid, np.full(len(cc_f), i), rr_f, cc_f,
                        A, B, C, Z, den, x0, y0, sp)
    return skipped


def _updateGrid(grid, tri_idx, rr, cc, A, B, C, Z, den,
                x0: float, y0: float, sp: float) -> None:
    """按重心坐标判定节点 (rr,cc) 是否在三角形内，max-Z 更新高度场。"""
    px = x0 + cc * sp
    py = y0 + rr * sp
    a = A[tri_idx]
    b = B[tri_idx]
    c = C[tri_idx]
    d = den[tri_idx]
    w0 = ((b[:, 1] - c[:, 1]) * (px - c[:, 0])
          + (c[:, 0] - b[:, 0]) * (py - c[:, 1])) / d
    w1 = ((c[:, 1] - a[:, 1]) * (px - c[:, 0])
          + (a[:, 0] - c[:, 0]) * (py - c[:, 1])) / d
    w2 = 1.0 - w0 - w1
    inside = (w0 >= -_EPS) & (w1 >= -_EPS) & (w2 >= -_EPS)
    if not bool(inside.any()):
        return
    zz = (w0 * Z[tri_idx, 0] + w1 * Z[tri_idx, 1]
          + w2 * Z[tri_idx, 2])
    np.maximum.at(grid, (rr[inside], cc[inside]),
                  zz[inside].astype(np.float32))


def rasterizeToHeightField(tris: np.ndarray,
                           max_dim: int = IMPORT_MAX_DIM):
    """三角网格 → (HeightField, 跳过退化面数)（max-Z 投影；未覆盖区域 z=0）。"""
    xy = tris[:, :, :2].reshape(-1, 2)
    minx, miny = float(xy[:, 0].min()), float(xy[:, 1].min())
    maxx, maxy = float(xy[:, 0].max()), float(xy[:, 1].max())
    sx, sy = maxx - minx, maxy - miny
    if sx <= 1e-9 or sy <= 1e-9:
        raise ValueError("STL 网格在 XY 平面上的投影面积为 0，无法转为浮雕")
    sp = max(sx, sy) / float(max_dim - 1)
    nx = max(2, int(round(sx / sp)) + 1)
    ny = max(2, int(round(sy / sp)) + 1)

    grid = np.zeros((ny, nx), dtype=np.float32)
    skipped = 0
    for s in range(0, len(tris), _CHUNK_TRIS):
        skipped += _rasterizeChunk(tris[s:s + _CHUNK_TRIS], grid,
                                   minx, miny, sp, nx, ny)
    return HeightField(grid, (minx, miny), sp), skipped


def importSTL(doc, path: str) -> dict:
    """读取 STL 文件并转为浮雕模型合并进 doc（当前文档）。

    返回统计：{"imported": 模型数(0/1), "triangles": 三角面数,
              "skipped": 跳过的退化面数, "dims": [nx, ny], "spacing": mm}。
    整批导入为一条撤销记录。
    """
    tris = readSTL(path)
    hf, skipped = rasterizeToHeightField(tris)

    obj = ReliefModelObject.fromHeightField(hf)
    doc.beginEdit()
    try:
        # 浮雕模型属于 2D/浮雕共用空间（reliefModels 只查该空间）：若当前
        # 活动空间是 "3d"（3D 预览拖入 STL），直接 addObject 会把模型落进
        # _objects3d，导致浮雕模式/2D 永远看不到。临时切回 2D 空间再落。
        prev = doc.space()
        if prev != "2d":
            doc.setSpace("2d")
        try:
            doc.addObject(obj)
        finally:
            if prev != "2d":
                doc.setSpace(prev)
    finally:
        doc.endEdit()
    return {"imported": 1, "triangles": int(len(tris)),
            "skipped": int(skipped), "dims": [hf.nx, hf.ny],
            "spacing": hf.spacing,
            "name": os.path.splitext(os.path.basename(path))[0]}

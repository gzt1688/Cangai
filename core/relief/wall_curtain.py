"""消除锯齿（竖直光壁/幕帘）—— 复刻自 Rhino 插件 ReliefAlgorithms.ExecuteDeJitter。

插件原版作用于真 3D 网格：把轮廓曲线两侧"锯齿半径"内的网格顶点 **XY 吸附到
曲线上、Z 保持不动**，顶点沿曲线叠成一圈竖直"幕帘"——侧壁**完全竖直**且
精确落在曲线上，平台面一直延伸到曲线，壁面法线沿曲线连续变化（无栅格竖棱）。

本应用是 2.5D 规则栅格（HeightField 顶点 XY 不可横移，否则雕刻/冲压/色号
蒙版全部失效），故分两层复刻：

1. **高度场层**（:func:`snap_band_to_platforms`）：曲线两侧 distance 内的格点
   高度钳为该侧平台高度（内格=内平台、外格=外平台，平台值沿曲线法向取样+
   弧长圆滑），带内成为干净的二值陡壁——2D 俯视与后续加工数据锐利无斜坡；
2. **网格层**（:func:`project_vertices_to_curtains`）：3D/STL 规则网格构建时，
   把幕帘带内（距曲线 ≤ max(distance, 1.5·栅距)）顶点的 **XY 投影到曲线最近
   点、Z 不动**——跨壁四边形退化为沿曲线的竖直条带（幕帘），平台四边形被
   拉成从等距线到曲线的水平扇面（无洞）。投影只发生在网格构建副本上，
   母场栅格与雕刻语义完全不受影响。

与旧"带内线性斜坡"方案的区别：斜坡面虽连续但倾斜（仰角 ~10-15°），光照下
是一圈倒角/磨光面；幕帘壁面仰角 ≈ 0（竖直）且法线沿曲线连续，才是"绝对
光滑的侧壁"。

坐标约定与 HeightField 一致：``data[row, col]``，行 0 = minY、列 0 = minX。
幕帘曲线一律存**模型 local 坐标**（跟随 placement/bake 变换）。
纯算法、零 UI 依赖；scipy 懒加载（经 region_relief 的 EDT，仅用于候选剪枝）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .region_relief import (build_inside_mask, compute_signed_distance,
                            resample_polyline)

# 距离默认 0.25mm（对齐 Rhino 插件 ReliefDialogTab4 的锯齿半径初值）
DEFAULT_DEJITTER_DISTANCE = 0.25
# 曲线采样点数（与插件 SampleCurve 一致，UI 层 sample_curve_points 用）
SAMPLE_COUNT = 512
# 平台取样半径 = distance + PLATFORM_MARGIN·spacing：取样双线性核（最远触及
# √2·spacing）完全落在钳制带（≤ distance）之外 → 重复执行严格幂等。
PLATFORM_MARGIN = 2.0
# 投影半径相对网格栅距的下限：保证 LOD 粗网格上每段曲线仍有 ≥~2 圈顶点
# 参与投影（否则竖直条带稀疏成洞）。
PROJ_MIN_CELLS = 1.5
# 曲线顶点密度上限（按弧长加密时的点数帽，防止超大曲线撑爆 O(V·E) 计算）
MAX_CURVE_POINTS = 4096
# 幂等去重容差（mm）：新曲线与已登记幕帘的双向最大最近点距离小于此值视为同一条
CURTAIN_DEDUP_TOL = 0.5


@dataclass
class DeJitterParams:
    """消除锯齿参数。"""

    distance: float = DEFAULT_DEJITTER_DISTANCE    # 锯齿半径（mm）
    sample_count: int = SAMPLE_COUNT               # 曲线采样点数（UI 取样用）


# ==================== 几何辅助 ====================

def _bilinear(arr: np.ndarray, ox: float, oy: float, sp: float,
              tx: np.ndarray, ty: np.ndarray) -> np.ndarray:
    """在规则栅格 arr（原点 ox,oy、间距 sp）上对世界点 (tx, ty) 双线性采样。

    越界点钳制到场边（取边缘值而非 0）：曲线贴近模型边缘时，平台点走出
    场界不应把侧壁挖到 0，而是沿用边缘高度。
    """
    ny, nx = arr.shape
    fc = np.clip((tx - ox) / sp, 0.0, nx - 1)
    fr = np.clip((ty - oy) / sp, 0.0, ny - 1)
    c0 = np.floor(fc).astype(np.int64)
    r0 = np.floor(fr).astype(np.int64)
    c1 = np.minimum(c0 + 1, nx - 1)
    r1 = np.minimum(r0 + 1, ny - 1)
    tc = fc - c0
    tr = fr - r0
    top = arr[r0, c0] * (1.0 - tc) + arr[r0, c1] * tc
    bot = arr[r1, c0] * (1.0 - tc) + arr[r1, c1] * tc
    return top * (1.0 - tr) + bot * tr


def _ensure_density(poly: np.ndarray, sp: float) -> np.ndarray:
    """保证曲线顶点间距 ≤ 1.5·spacing（最近点/法向取样精度需要），过疏时按
    弧长加密（点数帽 MAX_CURVE_POINTS）。已足够密则原样返回。"""
    if poly.shape[0] < 2:
        return poly
    seg = np.linalg.norm(np.diff(poly, axis=0), axis=1)
    perim = float(seg.sum())
    if perim <= 1e-12:
        return poly
    closed = float(np.linalg.norm(poly[0] - poly[-1])) <= 1e-9
    n_eff = poly.shape[0] - 1 if closed else poly.shape[0]
    if perim / max(n_eff, 1) <= 1.5 * sp:
        return poly
    count = int(min(math.ceil(perim / (1.5 * sp)), MAX_CURVE_POINTS))
    return resample_polyline(poly, max(count, 3))


def _seg_nearest(px: np.ndarray, py: np.ndarray,
                 ax: np.ndarray, ay: np.ndarray,
                 bx: np.ndarray, by: np.ndarray,
                 rows_chunk: int = 2048, seg_chunk: int = 256):
    """点集 (px, py) 到折线各段 [a→b] 的最近距离、段索引与段上参数 t。

    返回 (dist, seg_idx, seg_t)。分块计算控制内存；零长度段（重复点）按
    点距离处理（t=0）。
    """
    m = px.size
    n_seg = ax.size
    abx = bx - ax
    aby = by - ay
    l2 = abx * abx + aby * aby
    inv = np.where(l2 > 1e-18, 1.0 / np.maximum(l2, 1e-18), 0.0)
    best_d2 = np.full(m, np.inf, dtype=np.float64)
    best_j = np.zeros(m, dtype=np.int64)
    best_t = np.zeros(m, dtype=np.float64)
    for rs in range(0, m, rows_chunk):
        re = min(rs + rows_chunk, m)
        mx = px[rs:re, None]
        my = py[rs:re, None]
        mr = re - rs
        ld2 = np.full(mr, np.inf, dtype=np.float64)
        lj = np.zeros(mr, dtype=np.int64)
        lt = np.zeros(mr, dtype=np.float64)
        jj = np.arange(mr)
        for ss in range(0, n_seg, seg_chunk):
            se = min(ss + seg_chunk, n_seg)
            wx = abx[None, ss:se]
            wy = aby[None, ss:se]
            t = (mx - ax[None, ss:se]) * wx + (my - ay[None, ss:se]) * wy
            t *= inv[None, ss:se]
            np.clip(t, 0.0, 1.0, out=t)
            dx = mx - (ax[None, ss:se] + t * wx)
            dy = my - (ay[None, ss:se] + t * wy)
            d2 = dx * dx + dy * dy
            j = np.argmin(d2, axis=1)
            md2 = d2[jj, j]
            upd = md2 < ld2
            ld2[upd] = md2[upd]
            lj[upd] = ss + j[upd]
            lt[upd] = t[jj, j][upd]
        best_d2[rs:re] = ld2
        best_j[rs:re] = lj
        best_t[rs:re] = lt
    return np.sqrt(best_d2), best_j, best_t


def _nearest_points(px, py, ax, ay, bx, by):
    """点集到折线的最近点坐标（与 :func:`_seg_nearest` 同一套分块计算）。

    返回 (qx, qy, dist, seg_j)。
    """
    dist, j, t = _seg_nearest(px, py, ax, ay, bx, by)
    qx = ax[j] + t * (bx[j] - ax[j])
    qy = ay[j] + t * (by[j] - ay[j])
    return qx, qy, dist, j


def _points_inside(px: np.ndarray, py: np.ndarray, poly: np.ndarray,
                   chunk: int = 4096) -> np.ndarray:
    """对原始折线做射线法内外判定（真几何符号，栅格掩膜在曲线附近有
    ±1 格锯齿误差）。"""
    x1 = poly[:, 0]
    y1 = poly[:, 1]
    dx = np.roll(x1, -1) - x1
    dy = np.roll(y1, -1) - y1
    m = px.size
    out = np.zeros(m, dtype=bool)
    for rs in range(0, m, chunk):
        re = min(rs + chunk, m)
        wy = py[rs:re, None]
        cross = (y1[None, :] > wy) != (np.roll(y1, -1)[None, :] > wy)
        with np.errstate(divide="ignore", invalid="ignore"):
            xint = x1[None, :] + (wy - y1[None, :]) * dx[None, :] / dy[None, :]
        cnt = np.count_nonzero(cross & (xint > px[rs:re, None]), axis=1)
        out[rs:re] = (cnt & 1) == 1
    return out


def _inward_normals(pts: np.ndarray):
    """曲线顶点的内法向：各边方向先转 +90°（左法向）再平均，直顶点处即边
    法向平分，尖刺（两邻边反向）退回单边法向。

    朝向由有向面积（鞋带公式）定绕向：CCW 时行进方向左侧 = 内侧。
    返回 (nx, ny) 两个 (N,) 数组。
    """
    prev = np.roll(pts, 1, axis=0)
    nxt = np.roll(pts, -1, axis=0)
    e0 = pts - prev
    e1 = nxt - pts
    l0 = np.maximum(np.linalg.norm(e0, axis=1), 1e-12)
    l1 = np.maximum(np.linalg.norm(e1, axis=1), 1e-12)
    u0 = e0 / l0[:, None]
    u1 = e1 / l1[:, None]
    # 左法向平均：perp(v) = (−v_y, v_x)。不能直接平分边方向向量——近平直
    # 顶点处两边几乎同向，其"角平分"是切向而非法向。
    bnx = -(u0[:, 1] + u1[:, 1])
    bny = u0[:, 0] + u1[:, 0]
    lb = np.hypot(bnx, bny)
    ok = lb > 1e-9
    nx_ = np.where(ok, bnx / np.maximum(lb, 1e-12), -u1[:, 1])
    ny_ = np.where(ok, bny / np.maximum(lb, 1e-12), u1[:, 0])
    x, y = pts[:, 0], pts[:, 1]
    area = 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))
    s = 1.0 if area >= 0.0 else -1.0
    return np.ascontiguousarray(nx_ * s), np.ascontiguousarray(ny_ * s)


def _circular_smooth(vals: np.ndarray, w: int) -> np.ndarray:
    """沿曲线的环形滑动平均（窗口 w，按顶点序 = 弧长序），消除平台读数的
    栅格量化噪声；w<3 或点数不足时不平滑。"""
    n = vals.size
    if w < 3 or n < 3:
        return vals
    w = min(w, n if n % 2 == 1 else n - 1)
    if w < 3:
        return vals
    half = w // 2
    ext = np.concatenate((vals[-half:], vals, vals[:half]))
    cs = np.concatenate(([0.0], np.cumsum(ext)))
    return (cs[w:w + n] - cs[:n]) / float(w)


def _prep_curve(poly: np.ndarray, sp: float):
    """曲线预处理：密度保障 + 闭合去重（roll 闭段用），返回 (pts, ax, ay, bx, by)。"""
    pts = _ensure_density(np.asarray(poly, dtype=np.float64), sp)
    if pts.shape[0] >= 2 and float(np.linalg.norm(pts[0] - pts[-1])) <= 1e-9:
        pts = pts[:-1]
    ax, ay = pts[:, 0], pts[:, 1]
    return pts, ax, ay, np.roll(ax, -1), np.roll(ay, -1)


# ==================== 高度场层：带内钳为平台 ====================

def snap_band_to_platforms(hf, poly: np.ndarray, distance: float) -> int:
    """把曲线两侧 distance 内的格点高度钳为该侧平台高度（就地经 setData 提交）。

    poly 为 **local 坐标**（与 hf 同坐标系）。带内：内侧格点 = 内平台值、
    外侧格点 = 外平台值（平台值沿曲线法向走到 distance+2·spacing 处双线性
    取样，沿弧长圆滑；窄缝/急弯处自动收短取样半径）。带外一律不动。

    返回实际改变高度的格点数；distance ≤ 0 或曲线不与模型相交返回 0。
    重复执行严格幂等（平台取样点在钳制带外，读不到被改过的格子）。
    """
    d = max(0.0, float(distance))
    if d < 1e-9 or poly.shape[0] < 3:
        return 0

    ox, oy = hf.origin
    sp = hf.spacing
    nx, ny = hf.nx, hf.ny
    grid = (ox, oy, sp, nx, ny)

    inside_mask = build_inside_mask(poly, grid)
    if not inside_mask.any() or inside_mask.all():
        return 0    # 曲线不在模型上 / 完全罩住模型：没有边界带
    sd_edt = compute_signed_distance(inside_mask, sp)     # 仅用于候选剪枝

    pts, ax, ay, bx, by = _prep_curve(poly, sp)
    n_pts = pts.shape[0]
    if n_pts < 3:
        return 0
    r = d + PLATFORM_MARGIN * sp          # 平台取样半径（在钳制带外）

    # ---- 平台高度：顶点内/外法向走到 r 处双线性取样，沿曲线弧长圆滑 ----
    n_inx, n_iny = _inward_normals(pts)
    data = hf.data
    qix, qiy = ax + n_inx * r, ay + n_iny * r
    qox, qoy = ax - n_inx * r, ay - n_iny * r
    # 窄缝/急弯保护：取样点离曲线不足 r−0.5·spacing 时收短该点取样半径，
    # 避免平台值读到对面壁或带内。
    qd, _, _ = _seg_nearest(np.concatenate((qix, qox)),
                            np.concatenate((qiy, qoy)), ax, ay, bx, by)
    rad = np.full(2 * n_pts, r, dtype=np.float64)
    shrink = qd < (r - 0.5 * sp)
    rad[shrink] = np.maximum(0.9 * qd[shrink], 0.5 * d)
    z_in_v = _bilinear(data, ox, oy, sp, ax + n_inx * rad[:n_pts],
                       ay + n_iny * rad[:n_pts])
    z_out_v = _bilinear(data, ox, oy, sp, ax - n_inx * rad[n_pts:],
                        ay - n_iny * rad[n_pts:])
    perim = float(np.linalg.norm(np.column_stack((bx - ax, by - ay)),
                                 axis=1).sum())
    w = int(round(r / max(perim / n_pts, 1e-12)))
    z_in_v = _circular_smooth(z_in_v, w)
    z_out_v = _circular_smooth(z_out_v, w)

    # ---- 候选格点：EDT 剪枝 → 精确距离 → 带内二值钳制 ----
    cand = np.abs(sd_edt) <= d + 3.0 * sp
    cr, cc = np.nonzero(cand)
    if cr.size == 0:
        return 0
    px = ox + cc * sp
    py = oy + cr * sp
    dist, seg_j, seg_t = _seg_nearest(px, py, ax, ay, bx, by)
    band = dist <= d
    if not band.any():
        return 0

    inside_true = _points_inside(px[band], py[band], pts)
    # 点恰在曲线上（射线法过顶点退化，交点 x==px 不计）→ 用栅格掩膜兜底
    rr, ccols = cr[band], cc[band]
    on_curve = dist[band] < sp * 0.01
    if on_curve.any():
        inside_true[on_curve] = inside_mask[rr[on_curve], ccols[on_curve]]
    k0 = seg_j[band]
    k1 = (k0 + 1) % n_pts
    u = seg_t[band]
    z_in_c = z_in_v[k0] * (1.0 - u) + z_in_v[k1] * u
    z_out_c = z_out_v[k0] * (1.0 - u) + z_out_v[k1] * u
    vals = np.where(inside_true, z_in_c, z_out_c).astype(np.float32)

    new = data.copy()
    before = new[rr, ccols]
    new[rr, ccols] = vals
    changed = int(np.count_nonzero(before != vals))
    if changed:
        hf.setData(new)
    return changed


# ==================== 网格层：顶点投影幕帘 ====================

def project_vertices_to_curtains(xy: np.ndarray, curtains, spacing: float):
    """把规则网格顶点 XY 投影到幕帘曲线（最近点），Z 由调用方保持不动。

    xy: (V, 2) 顶点 local 坐标（会被复制，不就地修改）；
    curtains: [(poly (M,2) local, distance_mm), ...]；
    spacing: 该网格的栅距（LOD 网格用降采样后的栅距）。

    返回 (new_xy, moved_mask, wall_n)：
    - new_xy: 投影后的顶点坐标（无幕帘时为原数组）；
    - moved_mask: (V,) bool，被投影过的顶点；
    - wall_n: (V,2) 被投影顶点的**水平外法向**（壁面朝外，沿曲线最近段法向；
      未投影顶点为 0）——竖直幕帘条 quad 的叉积法线 n_z≈0，翻正逻辑无法定
      朝向，mesh 构建时用 quad 顶点此外法向平均给出朝外、沿曲线连续的壁面法线。
    投影半径 = max(distance, 1.5·spacing)——保证粗 LOD 网格上每段曲线仍有
    ≥~2 圈顶点参与；多条幕帘重叠时顶点归到最近的曲线。
    """
    xy = np.asarray(xy, dtype=np.float64)
    moved = np.zeros(xy.shape[0], dtype=bool)
    wall_n = np.zeros((xy.shape[0], 2), dtype=np.float64)
    if not curtains:
        return xy, moved, wall_n
    out = xy.copy()
    # 全局最近投影距离：多幕帘重叠处取最近曲线
    best_d = np.full(xy.shape[0], np.inf, dtype=np.float64)
    for poly, d0 in curtains:
        poly = np.asarray(poly, dtype=np.float64)
        if poly.shape[0] < 3:
            continue
        radius = max(float(d0), PROJ_MIN_CELLS * float(spacing))
        pts, ax, ay, bx, by = _prep_curve(poly, spacing)
        # 包围盒剪枝（±radius）
        bb = (float(ax.min()) - radius, float(ax.max()) + radius,
              float(ay.min()) - radius, float(ay.max()) + radius)
        cand = ((xy[:, 0] >= bb[0]) & (xy[:, 0] <= bb[1]) &
                (xy[:, 1] >= bb[2]) & (xy[:, 1] <= bb[3]))
        idx = np.nonzero(cand)[0]
        if idx.size == 0:
            continue
        qx, qy, dist, _j = _nearest_points(xy[idx, 0], xy[idx, 1],
                                           ax, ay, bx, by)
        hit = dist <= radius
        if not hit.any():
            continue
        gi = idx[hit]
        gd = dist[hit]
        gqx, gqy = qx[hit], qy[hit]
        closer = gd < best_d[gi]
        gi = gi[closer]
        gqx, gqy, gd = gqx[closer], gqy[closer], gd[closer]
        out[gi, 0] = gqx
        out[gi, 1] = gqy
        best_d[gi] = gd
        moved[gi] = True
        # 水平外法向：内顶点投影方向朝外（q−p）；外顶点反向（p−q）
        ins = _points_inside(xy[gi, 0], xy[gi, 1], pts)
        dx = gqx - xy[gi, 0]
        dy = gqy - xy[gi, 1]
        s = np.where(ins, 1.0, -1.0)
        ln = np.maximum(np.hypot(dx, dy), 1e-9)
        wall_n[gi, 0] = s * dx / ln
        wall_n[gi, 1] = s * dy / ln
    return out, moved, wall_n


def curtain_duplicate(poly: np.ndarray, existing, tol: float = CURTAIN_DEDUP_TOL) -> bool:
    """新曲线是否与已登记幕帘近重合（双向最大最近点距离 < tol），用于幂等去重。"""
    poly = np.asarray(poly, dtype=np.float64)
    if poly.shape[0] < 3:
        return True
    for ep, _d in existing:
        ep = np.asarray(ep, dtype=np.float64)
        if ep.shape[0] < 3:
            continue
        _, _, d1, _ = _nearest_points(poly[:, 0], poly[:, 1],
                                      ep[:, 0], ep[:, 1],
                                      np.roll(ep[:, 0], -1),
                                      np.roll(ep[:, 1], -1))
        _, _, d2, _ = _nearest_points(ep[:, 0], ep[:, 1],
                                      poly[:, 0], poly[:, 1],
                                      np.roll(poly[:, 0], -1),
                                      np.roll(poly[:, 1], -1))
        if float(d1.max()) < tol and float(d2.max()) < tol:
            return True
    return False

"""几何纹理库（复刻精雕 SurfMill"几何纹理"族）：程序化高度纹理生成。

纯 numpy 算法、零 Qt 依赖。所有生成函数返回 ``float32`` 数组、形状
``(ny, nx)``、值域归一到 ``[0, 1]``（0=谷/1=峰），由调用方按"高度 mm"
缩放后叠入高度场。行序约定与 :mod:`core.relief.height_field` 一致
（row=Y、col=X，行 0 = minY）——生成时按 (ny, nx) 直接产出即可。

纹理族（对齐精雕帮助"几何纹理"章节）：
- ``fractal``   随机分形纹理：可平铺 value-noise fBm，多八度叠加
- ``corrosion`` 腐蚀纹理：Worley 胞状凹坑（板面 + 腐蚀沟 + 粗糙）
- ``weave``     编织带纹理：经纬带交织（一上一下交错）
- ``knot``      缠绕结纹理：三叶结管状包围场
- ``fill``      填充纹理：六边/砖形/菱形/圆环/点阵图案
- ``rectgrid``  矩形网格纹理：矩形凸台阵列（可选随机高度）
- ``perturb``   表面扰动纹理：高频多正弦微扰动（小幅平整糙面）

性能约定：胞状/结形在大网格上按"工作分辨率 + 双线性放大"控制耗时，
特征尺度随网格尺寸自动缩放（用户视角下纹理物理尺寸一致）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Dict

import numpy as np

# 纹理类型 id → 中文名（命令面板/提示统一引用）
KIND_LABELS: Dict[str, str] = {
    "fractal": "随机分形",
    "corrosion": "腐蚀",
    "weave": "编织带",
    "knot": "缠绕结",
    "fill": "填充图案",
    "rectgrid": "矩形网格",
    "perturb": "表面扰动",
}

# 面板默认参数（命令只暴露高频项，其余走默认）
DEFAULTS: Dict[str, dict] = {
    "fractal": dict(seed=0, octaves=4, persistence=0.5, lacunarity=2.0,
                    base_cells=4),
    "corrosion": dict(seed=0, cells=14, pit=0.65, gap=0.22, roughness=0.35),
    "weave": dict(bands=8, width=0.72, low=0.16),
    "knot": dict(turns=3, radius=0.34, tube=0.075),
    "fill": dict(pattern="hex", cells=10, gap=0.18, bevel=0.12),
    "rectgrid": dict(cols=8, rows=8, gap=0.22, bevel=0.16, random_depth=0.0,
                     seed=0),
    "perturb": dict(freq=22, harmonics=3, amp=1.0, seed=0),
}

# 填充纹理的子图案
FILL_PATTERNS = ("hex", "brick", "diamond", "ring", "dot")
FILL_PATTERN_LABELS = ("六边形", "砖形", "菱形", "圆环", "点阵")


# ------------------------------------------------------------------ #
# 公共工具
# ------------------------------------------------------------------ #

def _norm01(a: np.ndarray) -> np.ndarray:
    """归一到 [0,1]（常数场返回全 0.5，避免除零）。"""
    lo, hi = float(a.min()), float(a.max())
    if hi - lo < 1e-12:
        return np.full(a.shape, 0.5, np.float32)
    return ((a - lo) / (hi - lo)).astype(np.float32)


def _smoothstep(e0: float, e1: float, x: np.ndarray) -> np.ndarray:
    """平滑阶跃（e0→e1），向量化。"""
    t = np.clip((x - e0) / (e1 - e0 + 1e-12), 0.0, 1.0)
    return (t * t * (3.0 - 2.0 * t)).astype(np.float32)


def _resize(a: np.ndarray, ny: int, nx: int) -> np.ndarray:
    """双线性放大到 (ny, nx)（结形等重算子先算小图再放大）。"""
    if a.shape == (ny, nx):
        return a
    sy, sx = a.shape[0], a.shape[1]
    ys = (np.arange(ny) + 0.5) * sy / ny - 0.5
    xs = (np.arange(nx) + 0.5) * sx / nx - 0.5
    y0 = np.clip(np.floor(ys).astype(int), 0, sy - 1)
    x0 = np.clip(np.floor(xs).astype(int), 0, sx - 1)
    y1 = np.clip(y0 + 1, 0, sy - 1)
    x1 = np.clip(x0 + 1, 0, sx - 1)
    fy = np.clip(ys - y0, 0.0, 1.0)[:, None]
    fx = np.clip(xs - x0, 0.0, 1.0)[None, :]
    top = a[np.ix_(y0, x0)] * (1 - fx) + a[np.ix_(y0, x1)] * fx
    bot = a[np.ix_(y1, x0)] * (1 - fx) + a[np.ix_(y1, x1)] * fx
    return (top * (1 - fy) + bot * fy).astype(np.float32)


def _value_noise(ny: int, nx: int, cy: int, cx: int,
                 rng: np.random.Generator) -> np.ndarray:
    """可平铺 value noise：随机格 + 双线性插值（边界环绕）。"""
    g = rng.random((max(2, cy), max(2, cx))).astype(np.float32)
    gy, gx = g.shape
    ys = np.arange(ny) / ny * gy
    xs = np.arange(nx) / nx * gx
    y0 = np.floor(ys).astype(int) % gy
    x0 = np.floor(xs).astype(int) % gx
    fy = (ys - np.floor(ys)).astype(np.float32)[:, None]
    fx = (xs - np.floor(xs)).astype(np.float32)[None, :]
    a = g[np.ix_(y0, x0)]
    b = g[np.ix_(y0, (x0 + 1) % gx)]
    c = g[np.ix_((y0 + 1) % gy, x0)]
    d = g[np.ix_((y0 + 1) % gy, (x0 + 1) % gx)]
    top = a + (b - a) * fx
    bot = c + (d - c) * fx
    return (top + (bot - top) * fy).astype(np.float32)


# ------------------------------------------------------------------ #
# 纹理族实现
# ------------------------------------------------------------------ #

def fractal(ny: int, nx: int, *, seed: int = 0, octaves: int = 4,
            persistence: float = 0.5, lacunarity: float = 2.0,
            base_cells: int = 4) -> np.ndarray:
    """随机分形纹理：fBm 多八度叠加，可平铺（边界无缝）。"""
    rng = np.random.default_rng(seed)
    total = np.zeros((ny, nx), np.float32)
    amp, cy, cx = 1.0, max(2, int(base_cells)), max(2, int(base_cells))
    norm = 0.0
    for _ in range(max(1, int(octaves))):
        total += amp * _value_noise(ny, nx, cy, cx, rng)
        norm += amp
        amp *= float(persistence)
        cy = int(cy * lacunarity) + 1
        cx = int(cx * lacunarity) + 1
    return _norm01(total / max(norm, 1e-9))


def corrosion(ny: int, nx: int, *, seed: int = 0, cells: int = 14,
              pit: float = 0.65, gap: float = 0.22,
              roughness: float = 0.35) -> np.ndarray:
    """腐蚀纹理：Worley 胞状——板状凸起 + 腐蚀沟 + 粗糙凹坑。

    cells 控制胞数量（特征尺度）；pit=胞内平台深度占比；gap=腐蚀沟宽；
    roughness=高频粗糙强度。9 邻域向量化，全网格一次成场。
    """
    rng = np.random.default_rng(seed)
    gy = gx = max(2, int(cells))
    jy = rng.random(gy).astype(np.float32)
    jx = rng.random(gx).astype(np.float32)
    # 每像素所在胞内偏移 + 胞索引
    by = (np.arange(ny) / ny * gy).astype(np.int32) % gy
    bx = (np.arange(nx) / nx * gx).astype(np.int32) % gx
    ry = (np.arange(ny) / ny * gy - by).astype(np.float32) / gy * ny
    rx = (np.arange(nx) / nx * gx - bx).astype(np.float32) / gx * nx
    bh, bw = ny / gy, nx / gx
    # F1：对 3x3 邻域胞各算一次全网格距离取最小（9 次全数组运算）
    d1 = np.full((ny, nx), np.inf, np.float32)
    for oy in (-1, 0, 1):
        fy = by * bh + (oy * bh + jy[(by + oy) % gy] * bh)
        dy = (ry - fy)[:, None]
        for ox in (-1, 0, 1):
            fx = bx * bw + (ox * bw + jx[(bx + ox) % gx] * bw)
            dx = (rx - fx)[None, :]
            d = np.sqrt(dy * dy + dx * dx)
            np.minimum(d1, d, out=d1)
    cell = min(bh, bw)
    # 胞内平台（近特征点高） + 沟（远特征点低）
    plateau = 1.0 - _smoothstep(cell * (1.0 - gap) * 0.5,
                                cell * (1.0 - gap) * 0.5 + cell * gap * 0.5,
                                d1)
    field = pit * plateau
    if roughness > 0:
        n = _value_noise(ny, nx, max(8, gy * 2), max(8, gx * 2), rng)
        field = field * (1.0 - roughness) + roughness * n * plateau * 1.2
    return _norm01(field.astype(np.float32))


def weave(ny: int, nx: int, *, bands: int = 8, width: float = 0.72,
          low: float = 0.16) -> np.ndarray:
    """编织带纹理：平纹编织（plain weave）——圆股经纬带一上一下交织。

    每根带 = 半圆截面沿长度方向正弦起伏：交织处"上带"满高、"下带"
    沉到 low 高度并从上带两侧露出，带间露出底面缝隙。
    width=带宽占节距比例（0.72 → 缝隙约 28%）；low=下带沉底高度。
    """
    p = max(2.0, ny / max(2, int(bands)))     # 横向带行距（Y 方向节距）
    q = max(2.0, nx / max(2, int(bands)))     # 纵向带列距（X 方向节距）
    width = float(np.clip(width, 0.3, 0.95))
    low = float(np.clip(low, 0.0, 0.6))
    mid = 0.5 * (1.0 + low)
    amp = 0.5 * (1.0 - low)

    yy = np.arange(ny, dtype=np.float32)
    xx = np.arange(nx, dtype=np.float32)

    # ---- 横向带（沿 X 走向，行距 p）----
    ih = np.floor(yy / p).astype(np.int32)
    t_h = (yy - (ih + 0.5) * p) / (0.5 * width * p)
    dome_h = np.sqrt(np.clip(1.0 - t_h * t_h, 0.0, 1.0))       # 半圆截面 (ny,)
    # 沿长起伏：交织点在纵带中心 x=(jv+0.5)q，(ih+jv) 偶 → 横带在上（满高）
    top_h = mid + amp * np.sin(np.pi * (xx / q)[None, :] - np.pi * ih[:, None])
    h_band = dome_h[:, None] * top_h

    # ---- 纵向带（沿 Y 走向，列距 q）----
    jv = np.floor(xx / q).astype(np.int32)
    t_v = (xx - (jv + 0.5) * q) / (0.5 * width * q)
    dome_v = np.sqrt(np.clip(1.0 - t_v * t_v, 0.0, 1.0))       # 半圆截面 (nx,)
    # (ih+jv) 奇 → 纵带在上（与横带互补）
    top_v = mid + amp * np.sin(np.pi * jv[None, :] - np.pi * yy[:, None] / p)
    v_band = dome_v[None, :] * top_v

    # 上带满高覆盖处自然盖住下带（max 合成）
    return np.maximum(h_band, v_band).astype(np.float32)


def _curve_intersections(dense_a: np.ndarray, cum_a: np.ndarray,
                         dense_b: np.ndarray, cum_b: np.ndarray):
    """两条密集折线的全部交叉点：返回 (s_a, s_b) 弧长列表。

    线段相交判定向量化（O(nA·nB) 一次广播）；nA/nB 为折线段数。
    """
    a1, a2 = dense_a[:-1], dense_a[1:]
    b1, b2 = dense_b[:-1], dense_b[1:]
    r = a2 - a1                                   # (nA,2)
    s = b2 - b1                                   # (nB,2)
    r = r[:, None, :]                             # (nA,1,2)
    s = s[None, :, :]                             # (1,nB,2)
    d = b1[None, :, :] - a1[:, None, :]           # (nA,nB,2)
    denom = r[..., 0] * s[..., 1] - r[..., 1] * s[..., 0]
    ok = np.abs(denom) > 1e-12
    denom = np.where(ok, denom, 1.0)
    t = (d[..., 0] * s[..., 1] - d[..., 1] * s[..., 0]) / denom
    u = (d[..., 0] * r[..., 1] - d[..., 1] * r[..., 0]) / denom
    hit = ok & (t >= 0.0) & (t <= 1.0) & (u >= 0.0) & (u <= 1.0)
    if not hit.any():
        return []
    ia, ib = np.nonzero(hit)
    seg_a = np.linalg.norm(dense_a[1:] - dense_a[:-1], axis=1)
    seg_b = np.linalg.norm(dense_b[1:] - dense_b[:-1], axis=1)
    s_a = cum_a[:-1][ia] + t[ia, ib] * seg_a[ia]
    s_b = cum_b[:-1][ib] + u[ia, ib] * seg_b[ib]
    return list(zip(s_a.tolist(), s_b.tolist()))


def _top_along(s: np.ndarray, s_arr: np.ndarray, v_arr: np.ndarray) -> np.ndarray:
    """沿带高度调制：交叉点处取 v_k，相邻交叉间 smoothstep 过渡。"""
    if len(s_arr) == 0:
        return np.ones_like(s)
    if len(s_arr) == 1:
        return np.full_like(s, v_arr[0])
    s_c = np.clip(s, s_arr[0], s_arr[-1])
    k = np.clip(np.searchsorted(s_arr, s_c, side="right") - 1,
                0, len(s_arr) - 2)
    s0, s1 = s_arr[k], s_arr[k + 1]
    t = np.clip((s_c - s0) / max(float(s1 - s0), 1e-9), 0.0, 1.0)
    tt = (t * t * (3.0 - 2.0 * t)).astype(np.float64)
    return v_arr[k] + (v_arr[k + 1] - v_arr[k]) * tt


def weave_along_curves(
    hf,
    polys: list,
    *,
    width_mm: float = 8.0,
    low: float = 0.16,
    samples_per_mm: float = 2.0,
) -> tuple[np.ndarray, np.ndarray]:
    """沿群组曲线编织：每条曲线 = 一根带的中心线，返回 (field, mask)。

    用户预先画好相互交叉的一组曲线并群组；带贴着每条曲线走，交叉处
    一带满高在上、另一带沉底（low）从两侧露出——与整面平纹编织同款。

    上下分配（plain weave 一致性）：沿每根带第 k 个交叉处，带上/下由
    (k + φ带) 偶/奇 决定；φ带 通过交叉约束 φA⊕φB = 1⊕kA⊕kB 做 BFS
    2-着色传播。网格状排布下有精确解（等效 (i+j)%2）；奇圈冲突时该
    交叉回退为小索引带在上。

    实现：每条曲线按 samples_per_mm 重采样后在自身 ROI 内用 scipy EDT
    一次算出每格垂距与最近弧长；各带场做 max 合成。
    """
    ny, nx = hf.ny, hf.nx
    field = np.zeros((ny, nx), np.float32)
    mask = np.zeros((ny, nx), bool)
    if not polys or len(polys) == 0:
        return field, mask

    sp = float(hf.spacing)
    width = max(float(width_mm), sp * 2.0)
    half_w = width * 0.5
    low = float(np.clip(low, 0.0, 0.6))

    # ---- 弧长重采样 ----
    curves = []                      # (dense, cum)
    for poly in polys:
        pts = np.asarray(poly, dtype=float)
        if pts.ndim != 2 or pts.shape[0] < 2:
            curves.append(None)
            continue
        seg = np.linalg.norm(np.diff(pts[:, :2], axis=0), axis=1)
        cum = np.concatenate([[0.0], np.cumsum(seg)])
        total = float(cum[-1])
        if total < 1e-6:
            curves.append(None)
            continue
        step = 1.0 / max(float(samples_per_mm), 0.05)
        ns = max(2, int(total / step) + 1)
        s_query = np.linspace(0.0, total, ns)
        dense = np.stack([np.interp(s_query, cum, pts[:, 0]),
                          np.interp(s_query, cum, pts[:, 1])], axis=1)
        curves.append((dense, cum))
    valid = [i for i, c in enumerate(curves) if c is not None]
    if not valid:
        return field, mask

    # ---- 交叉点检测（两两曲线）----
    # crossings[i] = [(s_i, other, s_other), ...]
    crossings: dict[int, list] = {i: [] for i in valid}
    for a in range(len(valid)):
        ia = valid[a]
        da, ca = curves[ia]
        for b in range(a + 1, len(valid)):
            ib = valid[b]
            db, cb = curves[ib]
            for s_a, s_b in _curve_intersections(da, ca, db, cb):
                crossings[ia].append((s_a, ib, s_b))
                crossings[ib].append((s_b, ia, s_a))

    # 交叉点按弧长排序 + 去重（近于半带宽的交叉合并），得每带局部索引 k
    min_sep = width * 0.5
    locals_k: dict[int, list] = {}      # i -> [(s, k)]
    for i in valid:
        lst = sorted(crossings[i], key=lambda c: c[0])
        kept: list[tuple[float, int]] = []
        for s, _j, _sj in lst:
            if kept and s - kept[-1][0] < min_sep:
                continue
            kept.append((s, len(kept)))
        locals_k[i] = kept

    # ---- 相位 2-着色（BFS）：φA⊕φB = 1⊕kA⊕KB（mod 2）----
    phi: dict[int, int] = {}
    for root in valid:
        if root in phi:
            continue
        phi[root] = 0
        queue = [root]
        while queue:
            i = queue.pop()
            for s_i, j, _sj in crossings[i]:
                # 该交叉在 j 带上的局部索引
                kj = _local_index(locals_k[j], _sj_of(crossings[j], j, i, s_i))
                w = (1 + _k_of(locals_k[i], s_i) + kj) & 1
                want = phi[i] ^ w
                if j not in phi:
                    phi[j] = want
                    queue.append(j)
                # 已着色但不一致：奇圈冲突，保持现状（后续按小索引兜底）

    # ---- 每带高度调制 top(s) + 场合成 ----
    from scipy.ndimage import distance_transform_edt, median_filter
    for i in valid:
        dense, cum = curves[i]
        kept = locals_k[i]
        if kept:
            s_arr = np.array([s for s, _k in kept], dtype=np.float64)
            k_arr = np.array([k for _s, k in kept], dtype=np.int64)
            ph = phi.get(i, 0)
            v_arr = np.where((k_arr + ph) % 2 == 0, 1.0, low)
        else:
            s_arr = np.zeros(0)
            v_arr = np.zeros(0)

        # ROI
        x0 = max(0.0, float(dense[:, 0].min()) - width)
        x1 = min(float(hf.origin[0] + (nx - 1) * sp),
                 float(dense[:, 0].max()) + width)
        y0 = max(0.0, float(hf.origin[1] - 0.0) + 0.0, 0.0)  # placeholder
        y0 = max(0.0, float(dense[:, 1].min()) - width)
        y1 = min(float(hf.origin[1] + (ny - 1) * sp),
                 float(dense[:, 1].max()) + width)
        c0 = int((x0 - hf.origin[0]) / sp)
        c1 = min(nx, int((x1 - hf.origin[0]) / sp) + 2)
        r0 = int((y0 - hf.origin[1]) / sp)
        r1 = min(ny, int((y1 - hf.origin[1]) / sp) + 2)
        if c1 - c0 < 2 or r1 - r0 < 2:
            continue
        lw, lh = c1 - c0, r1 - r0

        # 曲线打标 → EDT 距离 + 最近弧长
        feats = np.ones((lh, lw), dtype=bool)
        arc = np.zeros((lh, lw), dtype=np.float64)
        gcols = np.clip(((dense[:, 0] - hf.origin[0]) / sp - c0).astype(int),
                        0, lw - 1)
        grows = np.clip(((dense[:, 1] - hf.origin[1]) / sp - r0).astype(int),
                        0, lh - 1)
        feats[grows, gcols] = False
        step = 1.0 / max(float(samples_per_mm), 0.05)
        arc[grows, gcols] = np.linspace(0.0, float(cum[-1]), len(dense))
        dist, idx = distance_transform_edt(feats, return_indices=True)
        s_map = median_filter(arc[idx[0], idx[1]], size=3, mode="nearest")

        d_mm = dist * sp
        dome = np.sqrt(np.clip(1.0 - (d_mm / half_w) ** 2, 0.0, 1.0))
        top = _top_along(s_map, s_arr, v_arr) if kept else 1.0
        band = dome * top
        # 带边缘柔化收口
        band = band * _smoothstep(half_w, half_w * 0.82, d_mm)

        roi = field[r0:r1, c0:c1]
        np.maximum(roi, band.astype(np.float32), out=roi)
        mask[r0:r1, c0:c1] |= d_mm <= half_w

    return np.clip(field, 0.0, 1.0), mask
    """沿曲线编织（复刻精雕"编织带纹理"的路径版）：返回 (field, mask)。

    field ∈ [0,1] 仅在 mask 内有效（mask=曲线两侧 width_mm 带内）。

    参数空间：以曲线为"经线"方向——
      - d = 到曲线的垂距（沿带宽度方向堆叠"纬"）→ 带索引 floor(d/pitch_u)
      - s = 曲线弧长（沿曲线方向）              → 带索引 floor(s/pitch_v)
    交替规则与整面编织一致（(iu+iv)%2 决定上下），因此天然贴合曲线走向。

    实现：曲线按 samples_per_mm 重采样后在 ROI 栅格上打标，用
    scipy EDT 一次算出每格的垂距与最近弧长（O(格数)，不做逐格×逐段）。
    """
    sp = float(hf.spacing)
    pts = np.asarray(poly, dtype=float)
    if pts.ndim != 2 or pts.shape[0] < 2:
        return (np.zeros((hf.ny, hf.nx), np.float32),
                np.zeros((hf.ny, hf.nx), bool))
    # ---- 弧长重采样 ----
    seg = np.linalg.norm(np.diff(pts[:, :2], axis=0), axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    total = float(cum[-1])
    if total < 1e-6:
        return (np.zeros((hf.ny, hf.nx), np.float32),
                np.zeros((hf.ny, hf.nx), bool))
    step = 1.0 / max(float(samples_per_mm), 0.05)
    ns = max(2, int(total / step) + 1)
    s_query = np.linspace(0.0, total, ns)
    dense = np.stack([np.interp(s_query, cum, pts[:, 0]),
                      np.interp(s_query, cum, pts[:, 1])], axis=1)
    # ---- ROI（曲线包围盒外扩带宽）----
    pitch_u = float(pitch_u) if pitch_u else max(sp * 6.0, total / 60.0)
    pitch_v = float(pitch_v) if pitch_v else pitch_u
    width = float(width_mm) if width_mm else pitch_u * 3.0
    x0 = max(0.0, float(dense[:, 0].min()) - width)
    x1 = min(float(hf.origin[0] + (hf.nx - 1) * sp),
             float(dense[:, 0].max()) + width)
    y0 = max(0.0, float(dense[:, 1].min()) - width)
    y1 = min(float(hf.origin[1] + (hf.ny - 1) * sp),
             float(dense[:, 1].max()) + width)
    c0 = int((x0 - hf.origin[0]) / sp)
    c1 = min(hf.nx, int((x1 - hf.origin[0]) / sp) + 2)
    r0 = int((y0 - hf.origin[1]) / sp)
    r1 = min(hf.ny, int((y1 - hf.origin[1]) / sp) + 2)
    if c1 - c0 < 2 or r1 - r0 < 2:
        return (np.zeros((hf.ny, hf.nx), np.float32),
                np.zeros((hf.ny, hf.nx), bool))
    lw, lh = c1 - c0, r1 - r0

    # ---- 曲线打标：feature=0，其余=1 ----
    feats = np.ones((lh, lw), dtype=bool)
    arc = np.zeros((lh, lw), dtype=np.float64)
    gcols = np.clip(((dense[:, 0] - hf.origin[0]) / sp - c0).astype(int),
                    0, lw - 1)
    grows = np.clip(((dense[:, 1] - hf.origin[1]) / sp - r0).astype(int),
                    0, lh - 1)
    feats[grows, gcols] = False
    arc[grows, gcols] = s_query

    from scipy.ndimage import distance_transform_edt, median_filter
    dist, idx = distance_transform_edt(feats, return_indices=True)
    s_map = arc[idx[0], idx[1]]                # 最近点的弧长（arc 已是 mm，不再乘 sp）
    # EDT 最近邻映射在"中轴/两支等距"处有 1 格跳变；曲线近切向段还会出现
    # 栅格阶梯（弧长场垂直于路径的肋纹）。先用中值滤波抹接缝，再按股距
    # 尺度做一次均值平滑（平滑长度 ≈ pitch/sp 格），阶梯随股距一起消失。
    from scipy.ndimage import uniform_filter
    s_map = median_filter(s_map, size=3, mode="nearest")
    smooth_px = max(3, int(round(pitch_v / sp)))
    if smooth_px > 3:
        s_map = uniform_filter(s_map, size=smooth_px, mode="nearest")

    # ---- 编织场（曲线参数空间 (s,d)）----
    # 两族斜股交叉：w1 = d·cosθ + s·sinθ，w2 = d·sinθ − s·cosθ（θ 默认 45°）。
    # 相比"沿曲线 + 垂直曲线"两族，斜股不会在中心线退化成恒定 d 的
    # 平板——这才是编织带的经典形态（斜向股线来回穿插）。
    # 股截面 = 半圆（dome），沿股长正弦起伏：交织处上股满高、下股沉底
    # （与整面编织 weave() 同款形态）。
    d_mm = dist * sp
    s_mm = s_map
    th = math.radians(float(np.clip(angle_deg, 10.0, 80.0)))
    ct, stt = math.cos(th), math.sin(th)
    w1 = d_mm * ct + s_mm * stt
    # 第二族错半周期：否则 d=0（曲线中心线）上两族同相 → 中心线恒为满高脊
    w2 = d_mm * stt - s_mm * ct + pitch_v * 0.5

    u1 = w1 / pitch_u
    u2 = w2 / pitch_v
    width_frac = float(np.clip(width_frac, 0.3, 0.95))
    e1 = np.abs(u1 - np.round(u1)) * 2.0           # 0=股中心 → 1=半节距
    dome1 = np.sqrt(np.clip(1.0 - (e1 / width_frac) ** 2, 0.0, 1.0))
    e2 = np.abs(u2 - np.round(u2)) * 2.0
    dome2 = np.sqrt(np.clip(1.0 - (e2 / width_frac) ** 2, 0.0, 1.0))

    low = float(np.clip(low, 0.0, 0.6))
    mid = 0.5 * (1.0 + low)
    amp = 0.5 * (1.0 - low)
    k1 = np.floor(u1)
    k2 = np.floor(u2)
    # 交织起伏：族1 在 u2=k2 交织处 (k1+k2) 偶 → 上（满高），奇 → 沉底
    top1 = mid + amp * np.sin(np.pi * (u2 - k1))
    top2 = mid + amp * np.sin(np.pi * (u1 - k2))
    field = np.maximum(dome1 * top1, dome2 * top2)
    # 带边缘柔化收口
    mask = d_mm <= width * 0.5
    field = field * _smoothstep(width * 0.5, width * 0.5 * 0.82, d_mm)

    full_field = np.zeros((hf.ny, hf.nx), np.float32)
    full_mask = np.zeros((hf.ny, hf.nx), bool)
    full_field[r0:r1, c0:c1] = np.clip(field, 0.0, 1.0)
    full_mask[r0:r1, c0:c1] = mask
    return full_field, full_mask


def knot(ny: int, nx: int, *, turns: int = 3, radius: float = 0.34,
         tube: float = 0.075) -> np.ndarray:
    """缠绕结纹理：三叶结 (trefoil) 中心线的管状距离场。

    大网格下先按 512² 工作分辨率光栅化曲线 + 距离变换，再双线性放大，
    控制耗时；管径随网格尺寸按比例缩放。
    """
    w = min(ny, nx, 512)
    t = np.linspace(0, 2 * np.pi, 960, endpoint=False)
    k = max(1, int(turns))
    x = np.sin(t) + 2.0 * np.sin(2.0 * t) * (k / 3.0)
    y = np.cos(t) - 2.0 * np.cos(2.0 * t) * (k / 3.0)
    x = (x - x.min()) / (x.max() - x.min() + 1e-9) * (w - 1)
    y = (y - y.min()) / (y.max() - y.min() + 1e-9) * (w - 1)
    pts = np.stack([x, y], axis=1).astype(np.int32)
    try:
        import cv2
        img = np.zeros((w, w), np.uint8)
        cv2.polylines(img, [pts], False, 255, 1, cv2.LINE_AA)
        dist = cv2.distanceTransform((img == 0).astype(np.uint8),
                                     cv2.DIST_L2, 5)
        tube_px = max(1.0, tube * w)
        field = 1.0 - _smoothstep(tube_px * 0.55, tube_px, dist)
        return _resize(field, ny, nx)
    except Exception:
        # 无 cv2 兜底：分段距离（小图上直接算）
        gy, gx = np.mgrid[0:w, 0:w].astype(np.float32)
        d = np.full((w, w), np.inf, np.float32)
        for i in range(len(pts) - 1):
            ax, ay = pts[i]
            bx, by = pts[i + 1]
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy
            if L2 < 1e-9:
                seg = np.hypot(gx - ax, gy - ay)
            else:
                tt = np.clip(((gx - ax) * dx + (gy - ay) * dy) / L2,
                             0.0, 1.0)
                seg = np.hypot(gx - (ax + tt * dx), gy - (ay + tt * dy))
            np.minimum(d, seg, out=d)
        tube_px = max(1.0, tube * w)
        field = 1.0 - _smoothstep(tube_px * 0.55, tube_px, d)
        return _resize(field, ny, nx)


def fill(ny: int, nx: int, *, pattern: str = "hex", cells: int = 10,
         gap: float = 0.18, bevel: float = 0.12) -> np.ndarray:
    """填充纹理：六边/砖形/菱形/圆环/点阵图案凸起。"""
    n = max(2, int(cells))
    yy = (np.arange(ny, dtype=np.float32)[:, None] / ny * n)
    xx = (np.arange(nx, dtype=np.float32)[None, :] / nx * n)
    fx = xx - np.floor(xx)
    fy = yy - np.floor(yy)
    if pattern == "brick":
        # 砖形：行间错缝半格
        row = np.floor(yy).astype(np.int32)
        fx = (fx + 0.5 * (row % 2)) % 1.0
        edge = np.minimum(np.minimum(fx, 1 - fx), np.minimum(fy, 1 - fy))
    elif pattern == "diamond":
        edge = 0.5 * (np.abs(fx - 0.5) + np.abs(fy - 0.5))
    elif pattern == "ring":
        d = np.hypot(fx - 0.5, fy - 0.5)
        edge = np.abs(d - 0.32)
    elif pattern == "dot":
        d = np.hypot(fx - 0.5, fy - 0.5)
        edge = d
    else:  # hex：六边形（轴对齐近似 + 行错缝）
        row = np.floor(yy).astype(np.int32)
        fxh = (fx + 0.5 * (row % 2)) % 1.0
        gy = np.abs(fy - 0.5) * 1.15
        gx = np.abs(fxh - 0.5)
        edge = np.maximum(gx, gy * 0.866 + gx * 0.5)
    half = 0.5 - gap * 0.5
    band = 1.0 - _smoothstep(half - bevel * 0.5, half + bevel * 0.5, edge)
    return _norm01(band)


def rectgrid(ny: int, nx: int, *, cols: int = 8, rows: int = 8,
             gap: float = 0.22, bevel: float = 0.16,
             random_depth: float = 0.0, seed: int = 0) -> np.ndarray:
    """矩形网格纹理：矩形凸台阵列（倒角边 + 可选随机高度）。"""
    c, r = max(1, int(cols)), max(1, int(rows))
    xx = (np.arange(nx, dtype=np.float32)[None, :] / nx * c)
    yy = (np.arange(ny, dtype=np.float32)[:, None] / ny * r)
    fx = xx - np.floor(xx)
    fy = yy - np.floor(yy)
    edge = np.minimum(np.minimum(fx, 1 - fx), np.minimum(fy, 1 - fy))
    half = 0.5 - gap * 0.5
    band = 1.0 - _smoothstep(half - bevel * 0.5, half + bevel * 0.5, edge)
    if random_depth > 0:
        rng = np.random.default_rng(seed)
        depth = rng.random((r, c)).astype(np.float32)
        depth = _resize(depth, ny, nx)
        band = band * (1.0 - random_depth + random_depth * depth)
    return _norm01(band)


def perturb(ny: int, nx: int, *, freq: int = 22, harmonics: int = 3,
            amp: float = 1.0, seed: int = 0) -> np.ndarray:
    """表面扰动纹理：多频正弦微扰（平整表面糙化，值域居中 0.5）。"""
    rng = np.random.default_rng(seed)
    yy = np.arange(ny, dtype=np.float32)[:, None] / ny
    xx = np.arange(nx, dtype=np.float32)[None, :] / nx
    f = np.zeros((ny, nx), np.float32)
    norm = 0.0
    a = 1.0
    for h in range(1, max(1, int(harmonics)) + 1):
        ph1 = rng.random() * 2 * np.pi
        ph2 = rng.random() * 2 * np.pi
        ang = rng.random() * 2 * np.pi
        k = 2 * np.pi * freq * h
        f += a * np.sin(k * (xx * np.cos(ang) + yy * np.sin(ang)) + ph1) \
            * np.cos(k * (yy * np.cos(ang) - xx * np.sin(ang)) + ph2)
        norm += a
        a *= 0.55
    f = f / max(norm, 1e-9)
    return np.clip(0.5 + 0.5 * amp * f, 0.0, 1.0).astype(np.float32)


# ------------------------------------------------------------------ #
# 调度入口
# ------------------------------------------------------------------ #

_GENERATORS: Dict[str, Callable[..., np.ndarray]] = {
    "fractal": fractal,
    "corrosion": corrosion,
    "weave": weave,
    "knot": knot,
    "fill": fill,
    "rectgrid": rectgrid,
    "perturb": perturb,
}


def generate(kind: str, ny: int, nx: int, **params) -> np.ndarray:
    """按类型生成纹理场（float32 (ny,nx) ∈ [0,1]）。

    未识别的类型回退随机分形；参数与 :data:`DEFAULTS` 合并（调用方
    只传需要覆盖的键）。
    """
    fn = _GENERATORS.get(kind) or fractal
    kw = dict(DEFAULTS.get(kind, {}))
    kw.update({k: v for k, v in params.items() if v is not None})
    return fn(int(ny), int(nx), **kw)


# ------------------------------------------------------------------ #
# 应用到高度场（供"纹理填充"命令调用）
# ------------------------------------------------------------------ #

@dataclass
class TextureFillParams:
    """纹理填充参数（面板 → 算法的唯一数据结构）。"""
    kind: str = "fractal"
    fill_pattern: str = "hex"
    height_mm: float = 1.0        # 纹理高度振幅（mm）
    strength: float = 1.0         # 强度倍率（0=无效果）
    detail_mm: float = 5.0        # 细节物理尺度（mm）→ 换算特征格数
    seed: int = 0
    blend: str = "add"            # add 叠加 / replace 取代 / subtract 切除
    color_scope: str = "ignore"   # ignore / inside / outside（色号蒙版）
    # 编织带专用（kind == "weave"）
    weave_mode: str = "grid"      # grid 整面编织 / curve 沿曲线编织
    weave_width_mm: float = 8.0   # 沿曲线编织的带宽
    weave_pitch_mm: float = 3.0   # 股距（节距）
    weave_angle: float = 45.0     # 股线斜角（度）


def _cells_for(hf, detail_mm: float) -> int:
    """细节物理尺度 → 特征格数（沿模型较大边）。"""
    try:
        w, h = hf.size_mm()
    except Exception:
        return 4
    size = max(float(w), float(h))
    return max(2, int(round(size / max(float(detail_mm), 0.5))))


def apply_texture(hf, params: TextureFillParams,
                  color_mask: np.ndarray | None = None,
                  color_value: int = 0,
                  poly: np.ndarray | None = None) -> int:
    """生成几何纹理并叠入高度场，返回被修改的格子数。

    hf: HeightField（就地修改 data）。poly=None → 整个模型；否则只在
    该闭合曲线（世界坐标采样点）围成的区域内生效。color_scope 与
    :func:`core.relief.region_relief.apply_region_relief` 同语义。
    """
    grid = (hf.origin[0], hf.origin[1], hf.spacing, hf.nx, hf.ny)
    if poly is not None:
        from core.relief.region_relief import build_inside_mask
        mask = build_inside_mask(np.asarray(poly, dtype=float), grid)
    else:
        mask = np.ones((hf.ny, hf.nx), dtype=bool)
    scope = getattr(params, "color_scope", "ignore")
    if scope in ("inside", "outside"):
        if color_mask is not None and color_mask.shape == mask.shape:
            inside_cur = (color_mask == int(color_value))
        else:
            inside_cur = np.zeros(mask.shape, dtype=bool)
        mask = (mask & inside_cur) if scope == "inside" else (mask & ~inside_cur)
    if not mask.any():
        return 0

    cells = _cells_for(hf, params.detail_mm)
    kw: Dict[str, object] = {}
    kind = params.kind

    # 编织带·沿曲线模式：股线贴着选中的曲线交叉（需提供 poly）
    if (kind == "weave"
            and getattr(params, "weave_mode", "grid") == "curve"
            and poly is not None):
        field_c, mask_c = weave_along_curve(
            hf, poly,
            width_mm=getattr(params, "weave_width_mm", 8.0),
            pitch_u=getattr(params, "weave_pitch_mm", 3.0),
            pitch_v=getattr(params, "weave_pitch_mm", 3.0),
            angle_deg=getattr(params, "weave_angle", 45.0),
        )
        mask = mask & mask_c
        if not mask.any():
            return 0
        amp = float(params.height_mm) * float(params.strength)
        if amp <= 0.0:
            return 0
        data = hf.data
        blend = getattr(params, "blend", "add")
        if blend == "add":
            new = data + field_c * amp
        elif blend == "subtract":
            new = data - field_c * amp
        else:
            new = field_c * amp
        before = data.copy()
        data[mask] = new[mask]
        return int(np.count_nonzero(data != before))

    if kind in ("fractal", "corrosion", "rectgrid", "perturb"):
        kw["seed"] = int(params.seed)
    if kind == "fractal":
        kw["base_cells"] = cells
    elif kind == "corrosion":
        kw["cells"] = cells
    elif kind == "weave":
        kw["bands"] = cells
    elif kind == "fill":
        kw["cells"] = cells
        kw["pattern"] = params.fill_pattern
    elif kind == "rectgrid":
        kw["cols"] = kw["rows"] = cells
    elif kind == "perturb":
        kw["freq"] = max(4, cells * 2)
    tex = generate(kind, hf.ny, hf.nx, **kw)

    amp = float(params.height_mm) * float(params.strength)
    if amp <= 0.0:
        return 0
    data = hf.data
    field = tex * amp
    blend = getattr(params, "blend", "add")
    if blend == "add":
        new = data + field
    elif blend == "subtract":
        new = data - field
    else:
        new = field
    before = data.copy()
    data[mask] = new[mask]
    return int(np.count_nonzero(data != before))

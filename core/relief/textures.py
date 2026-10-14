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
- ``water``     水波纹理：方向行进波 + 随机溅落同心涟漪（可平铺）
- ``waves``     波浪纹理：水面波列（数量/弧度/角度三参数）
- ``rough``     糙面纹理：细粒噪声 + 低频起伏（自然砂面）

"随机"类型按 rand_sub 子模式路由：分形(fractal)/波浪(waves)/糙面(rough)。

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
    "weave": "编织",
    "wrap": "缠绕",
    "knot": "缠绕结",
    "fill": "填充图案",
    "rectgrid": "矩形网格",
    "perturb": "表面扰动",
    "waves": "波浪",
    "rough": "糙面",
}

# 面板默认参数（命令只暴露高频项，其余走默认）
DEFAULTS: Dict[str, dict] = {
    "fractal": dict(seed=0, octaves=4, persistence=0.5, lacunarity=2.0,
                    base_cells=4),
    "corrosion": dict(seed=0, cells=14, pit=0.65, gap=0.22, roughness=0.35),
    "weave": dict(bands=8, width=0.72, low=0.16),
    "knot": dict(turns=3, radius=0.34, tube=0.075),
    "fill": dict(pattern="hex", size_mm=6.0, gap_x=1.0, gap_y=1.0,
                 brick_w_mm=11.5, spacing=0.2, bevel=0.4),
    "rectgrid": dict(cols=8, rows=8, gap=0.22, bevel=0.16, random_depth=0.0,
                     seed=0),
    "perturb": dict(freq=22, harmonics=3, amp=1.0, seed=0),
    "water": dict(seed=0, cells=6, waves=3, ripple_gain=0.6, damping=2.0),
    "waves": dict(seed=0, count=8, curvature=0.3, angle=0.0, sharp=0.3,
                  waver=0.4, uneven=0.4, ripple=0.18),
    "rough": dict(seed=0, cells=30, octaves=3),
}

# 填充纹理的子图案
FILL_PATTERNS = ("hex", "brick", "pyramid", "anti_pyramid")
FILL_PATTERN_LABELS = ("六边形", "砖形", "三棱锥", "正反三棱锥")

# 随机纹理的子模式（"随机"类型下的单选）
RAND_SUBS = ("fractal", "waves", "rough")
RAND_SUB_LABELS = ("分形", "波浪", "糙面")
_RAND_SUB_MAP = {"fractal": "fractal", "waves": "waves", "rough": "rough"}


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
    # 每像素所在胞索引（by/bx）+ 绝对像素坐标（ry/rx）
    by = (np.arange(ny) / ny * gy).astype(np.int32) % gy
    bx = (np.arange(nx) / nx * gx).astype(np.int32) % gx
    ry = np.arange(ny, dtype=np.float32)
    rx = np.arange(nx, dtype=np.float32)
    bh, bw = ny / gy, nx / gx
    # F1：对 3x3 邻域胞各算一次全网格距离取最小（9 次全数组运算）。
    # 特征点绝对坐标 = (胞索引(含越界环绕) + 胞内抖动) × 胞尺寸，
    # 越界胞用 %gy 取抖动值 → 环面拓扑，边界无缝可平铺。
    d1 = np.full((ny, nx), np.inf, np.float32)
    for oy in (-1, 0, 1):
        fy = (by + oy + jy[(by + oy) % gy]) * bh
        dy = (ry - fy)[:, None]
        for ox in (-1, 0, 1):
            fx = (bx + ox + jx[(bx + ox) % gx]) * bw
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


def _curve_intersections(dense_a: np.ndarray, dense_b: np.ndarray):
    """两条密集折线（弧长均匀重采样）的全部交叉点：返回 [(s_a, s_b), ...]。

    线段相交判定向量化（O(nA·nB) 一次广播）；弧长由 dense 自身累积。
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
    cum_a = np.concatenate([[0.0], np.cumsum(seg_a)])   # dense 弧长（mm）
    cum_b = np.concatenate([[0.0], np.cumsum(seg_b)])
    s_a = cum_a[:-1][ia] + t[ia, ib] * seg_a[ia]
    s_b = cum_b[:-1][ib] + u[ia, ib] * seg_b[ib]
    return list(zip(s_a.tolist(), s_b.tolist()))


def _curve_self_intersections(dense: np.ndarray, closed: bool = False):
    """单条折线的自相交点（跳过相邻段共享端点）：返回 [(s_a, s_b), ...]。

    例："8"字形曲线中部自穿越 → 返回两个分支各自的弧长。
    线段两两判定按行分块（控制 (块,n) 中间数组规模）；相邻段
    （|段号差|≤1）共享端点不算交叉；closed=True 时首段与闭合段
    （末段）共享端点也不算交叉。
    """
    n = dense.shape[0]
    if n < 4:
        return []
    seg = np.linalg.norm(dense[1:] - dense[:-1], axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    out = []
    B = 256                                    # 行块大小
    for s0 in range(0, n - 1, B):
        e0 = min(s0 + B, n - 1)
        a1 = dense[s0:e0]                      # 块内段起点
        r = (dense[s0 + 1:e0 + 1] - a1)[:, None, :]      # (B,1,2)
        ss = (dense[1:] - dense[:-1])[None, :, :]        # (1,n-1,2)
        d = dense[None, :-1, :] - a1[:, None, :]         # (B,n-1,2)
        denom = r[..., 0] * ss[..., 1] - r[..., 1] * ss[..., 0]
        ok = np.abs(denom) > 1e-12
        denom = np.where(ok, denom, 1.0)
        t = (d[..., 0] * ss[..., 1] - d[..., 1] * ss[..., 0]) / denom
        u = (d[..., 0] * r[..., 1] - d[..., 1] * r[..., 0]) / denom
        hit = ok & (t >= 0.0) & (t <= 1.0) & (u >= 0.0) & (u <= 1.0)
        if not hit.any():
            continue
        ia_loc, ib = np.nonzero(hit)
        ia = ia_loc + s0
        # 相邻段共享端点不算交叉；ia<ib 去掉块间重复（同一对只算一次）
        keep = ((np.abs(ia.astype(np.int64) - ib.astype(np.int64)) > 1)
                & (ia < ib))
        if closed:
            # 闭合曲线：首段(0)与闭合段(n-2)共享端点不算交叉
            keep &= ~((ia == 0) & (ib == n - 2))
        if not keep.any():
            continue
        ia, ib = ia[keep], ib[keep]
        ia_loc = ia_loc[keep]
        tt = t[ia_loc, ib]
        uu = u[ia_loc, ib]
        s_a = cum[:-1][ia] + tt * seg[ia]
        s_b = cum[:-1][ib] + uu * seg[ib]
        out.extend(zip(s_a.tolist(), s_b.tolist()))
    return out


def _top_along(s: np.ndarray, s_arr: np.ndarray, v_arr: np.ndarray,
               p_arr: np.ndarray | None = None,
               ramp_max: float = 0.0) -> np.ndarray:
    """沿带高度调制：控制点（交叉/带端）之间余弦过渡。

    p_arr is None（默认）：控制点之间整段余弦拱（相邻交叉密集时即为
    编织起伏形态），端点斜率 0。带端虚拟点值为 0，端部弧度收口到底面。

    p_arr（每控制点的局部平台半长）：各控制点值向两侧保持平台，平台
    之间余弦过渡——交叉处的上/下只局部作用于交叉附近。分配规则：
      * 每侧平台 ≤ 0.35×间隔（过渡段至少占间隔的 30%，不产生台阶）；
      * 过渡段超过 ramp_max 时向两侧平台均摊延长——长带不会从带端
        一路斜坡到交叉点，所有过渡长短均匀。
    """
    n = len(s_arr)
    if n == 0:
        return np.ones_like(s)
    if n == 1:
        return np.full_like(s, v_arr[0])
    k = np.clip(np.searchsorted(s_arr, s, side="right") - 1, 0, n - 2)
    s0 = s_arr[k]
    s1 = s_arr[k + 1]
    gap = np.maximum(s1 - s0, 1e-9)
    frac = np.clip((s - s0) / gap, 0.0, 1.0)
    tt = 0.5 * (1.0 - np.cos(np.pi * frac))          # 余弦拱：端点斜率 0
    val = v_arr[k] + (v_arr[k + 1] - v_arr[k]) * tt
    if p_arr is None:
        return val
    # 两控制点平台总长 ≥ 间隔（交叉过密）→ 整段余弦（最平滑，同
    # 网格编织形态）；否则各侧平台 ≤ 0.35×间隔（过渡段 ≥ 30%）
    fit = (p_arr[k] + p_arr[k + 1]) < gap
    a = np.minimum(p_arr[k], 0.35 * gap)             # s0 侧平台长
    b = np.minimum(p_arr[k + 1], 0.35 * gap)         # s1 侧平台长
    if ramp_max > 0:
        excess = np.maximum(gap - a - b - ramp_max, 0.0) * 0.5
        a = np.minimum(a + excess, 0.5 * gap)
        b = np.minimum(b + excess, 0.5 * gap)
    mid1 = gap - b                                   # 过渡段起点
    has_mid = mid1 > a + 1e-9
    d = s - s0
    mf = np.clip((d - a) / np.maximum(mid1 - a, 1e-9), 0.0, 1.0)
    ramp = 0.5 * (1.0 - np.cos(np.pi * mf))
    val_mid = v_arr[k] + (v_arr[k + 1] - v_arr[k]) * ramp
    local = np.where(d <= a, v_arr[k],
                     np.where(d >= mid1, v_arr[k + 1], val_mid))
    return np.where(fit & has_mid, local, val)


def _section_profile(d: np.ndarray, half_w: float, sec: str,
                     top_w: float, ellipse_ratio: float) -> np.ndarray:
    """编织带截面形状：d = 到带中心线垂距(mm)，返回 0..1（椭圆峰值=ratio）。

    trapezoid 梯形：顶部平缓、斜边过渡；上宽=下宽 → 矩形，上宽=0 → 三角。
    spline 样条：全程光滑鼓形（无平顶、无直边段），顶部/底部切线平、
    中段鼓——0.5·(1+cos(π·t^k))，曲率连续，区别于梯形的平顶+斜边。
    ellipse 椭圆：半椭圆拱，ellipse_ratio 控制高度比（1=半圆）。
    """
    t = np.clip(d / max(float(half_w), 1e-9), 0.0, 1.0)   # 0 中心 → 1 边缘
    if sec == "trapezoid":
        r = float(np.clip(top_w / max(2.0 * half_w, 1e-9), 0.0, 1.0))
        return 1.0 - _smoothstep(r, 1.0, t)
    if sec == "spline":
        # 指数 k 控制鼓形：k 越大顶部越平、越接近梯形；1=普通余弦拱
        kk = 1.8
        return 0.5 * (1.0 + np.cos(np.pi * t ** kk))
    if sec == "ellipse":
        return float(np.clip(ellipse_ratio, 0.05, 1.0)) * np.sqrt(
            np.clip(1.0 - t * t, 0.0, 1.0))
    return 1.0 - t                                        # 兜底：线性三角


def _offset_polyline(sub: np.ndarray, off: float) -> np.ndarray:
    """折线沿法向平移 off mm（顶点切线取相邻段均值，端点用单侧）。"""
    if abs(float(off)) < 1e-9:
        return sub
    d = np.diff(sub[:, :2], axis=0)
    ln = np.hypot(d[:, 0], d[:, 1])
    keep = ln > 1e-9
    if not keep.any():
        return sub
    t = d[keep] / ln[keep][:, None]
    tan = np.zeros((len(sub), 2))
    tan[1:-1] = t[:-1] + t[1:]
    tan[0] = t[0]
    tan[-1] = t[-1]
    ln2 = np.hypot(tan[:, 0], tan[:, 1])
    ln2[ln2 < 1e-9] = 1.0
    nrm = np.stack([-tan[:, 1] / ln2, tan[:, 0] / ln2], axis=1)
    return sub + nrm * float(off)


def weave_along_curves(
    hf,
    polys: list,
    *,
    width_mm: float = 4.0,
    top_w_mm: float = 1.5,
    sec: str = "ellipse",
    ellipse_ratio: float = 1.0,
    low: float = 0.45,
    samples_per_mm: float = 2.0,
    sub_n: int = 1,
    sub_gap: float = 0.6,
) -> tuple[np.ndarray, np.ndarray]:
    """沿曲线编织：每条曲线 = 一根带的中心线，返回 (field, mask)。

    输入可以是：多条相互交叉的曲线（群组），或单条自相交曲线
    （如"8"字——自穿越处两支上下交替）。首尾近闭（间隙 ≤ 0.75 带宽）
    的曲线自动闭合：补一段连接线，带环连续无端部收口（完整的 8/环）。
    带贴着每条曲线走，交叉处
    一带满高在上、另一带下沉（low）从两侧露出。low 取 0.45 左右
    （而非贴底）：下带保持截面体积感，读起来是"从下面穿过去"。

    截面：sec = ellipse / trapezoid / spline（见 :func:`_section_profile`），
    带宽度 = width_mm（截面下宽），trapezoid 的顶部平宽 = top_w_mm
    （spline 全程光滑无平顶，忽略上宽）。

    子编织（竹编）：sub_n > 1 时每根带沿法向均分为 sub_n 条细带，
    细带间留 sub_gap(mm) 缝，细带宽 = (带宽−缝)/n；细带共享主线
    的交叉上/下调制（同一根带的细条同上同下）。

    沿带起伏：交叉之间余弦拱（无平台），带端弧度收口到 0。
    自相交曲线（如"8"字）在交叉点拆成多个经过段分别栅格化：单带
    栅格化时交叉邻域的最近弧长在两支间跳变，会把上带沿中线切开
    （交叉不连贯的根源）；拆段后各段独立成带 max 合成，上带连续。
    拆出的段用局部平台调制，交叉上/下只作用于交叉附近。

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
    # 首尾近闭（间隙 ≤ 0.75 带宽）的曲线按闭合处理：补一段首尾连接线，
    # 带环连续、无端部收口——用户一笔画的"8"字/圆环应首尾相连不断开。
    curves = []                      # (dense, cum, closed)
    for poly in polys:
        pts = np.asarray(poly, dtype=float)
        if pts.ndim != 2 or pts.shape[0] < 2:
            curves.append(None)
            continue
        gap_end = float(np.linalg.norm(pts[-1, :2] - pts[0, :2]))
        closed = gap_end <= max(2.0 * sp, 0.75 * width)
        if closed and gap_end > 1e-9:
            pts = np.vstack([pts, pts[:1, :]])   # 补闭合段
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
        curves.append((dense, cum, closed))
    valid = [i for i, c in enumerate(curves) if c is not None]
    if not valid:
        return field, mask

    # ---- 交叉点检测（两两曲线之间 + 单条曲线自相交）→ 全局交叉记录 ----
    recs = []                        # (i, s_i, j, s_j)；自相交 i==j（s_i<s_j）
    for a in range(len(valid)):
        ia = valid[a]
        da, _ca, cl_a = curves[ia]
        for s_a, s_b in _curve_self_intersections(da, closed=cl_a):
            lo, hi = (s_a, s_b) if s_a <= s_b else (s_b, s_a)
            if hi - lo > 1e-9:
                recs.append((ia, lo, ia, hi))
        for b in range(a + 1, len(valid)):
            ib = valid[b]
            db, _cb, _cl_b = curves[ib]
            for s_a, s_b in _curve_intersections(da, db):
                recs.append((ia, s_a, ib, s_b))

    # ---- 每带按弧长去重：只合并切线掠过产生的重复命中（ε≈重采样步长）。
    # 注意不能用带宽级阈值——不同曲线对的合法交叉可以挨得很近
    # （如圆弧上相邻两线的穿越点仅差几 mm），大阈值会把它们误删，
    # 导致沿带上下奇偶序列错乱、大部分带沉底不可见。----
    eps = max(2.0 / max(float(samples_per_mm), 0.05), sp * 2.0)
    per_curve: dict[int, list] = {i: [] for i in valid}
    for ridx, (i, s_i, j, s_j) in enumerate(recs):
        per_curve[i].append((s_i, ridx))
        per_curve[j].append((s_j, ridx))
    drop: set[int] = set()
    for i in valid:
        last = None
        for s, ridx in sorted(per_curve[i]):
            if last is not None and s - last < eps:
                drop.add(ridx)
                continue
            last = s
    live_by_curve: dict[int, list] = {i: [] for i in valid}
    for ridx, (i, s_i, j, s_j) in enumerate(recs):
        if ridx in drop:
            continue
        live_by_curve[i].append((s_i, ridx))
        live_by_curve[j].append((s_j, ridx))
    # 每条带第 k 个交叉（按弧长排序）；自相交记录在同一带上贡献两个 k
    k_entries: dict[tuple[int, int], list[int]] = {}
    for i in valid:
        for k, (_s, ridx) in enumerate(sorted(live_by_curve[i])):
            k_entries.setdefault((i, ridx), []).append(k)

    # ---- 相位 2-着色（BFS）：约束 φA⊕φB = 1⊕kA⊕kB（mod 2）----
    # 每带第 k 交叉处 over ⟺ (k+φ) 偶。网格状排布有精确解（等效 (i+j)%2）；
    # 自相交不跨带传播（单带内两支 k 不同 → 上下天然交替）；奇圈冲突时
    # 该交叉兜底（自相交=弧长小的支路在上，异带=小索引带在上）。
    phi: dict[int, int] = {}
    for root in valid:
        if root in phi:
            continue
        phi[root] = 0
        stack = [root]
        while stack:
            i = stack.pop()
            for _s, ridx in sorted(live_by_curve[i]):
                ci, _si, cj, _sj = recs[ridx]
                if ci == cj:
                    continue          # 自相交：不参与跨带传播
                other = cj if ci == i else ci
                ks_i = k_entries.get((i, ridx))
                ks_o = k_entries.get((other, ridx))
                if not ks_i or not ks_o:
                    continue
                w = (1 + ks_i[0] + ks_o[0]) & 1
                want = phi[i] ^ w
                if other not in phi:
                    phi[other] = want
                    stack.append(other)

    # ---- 每交叉的上/下判定（over_at[ridx] = (ci 支是否在上, cj 支是否在上)）----
    over_at: dict[int, tuple[bool, bool]] = {}
    for ridx, (ci, _si, cj, _sj) in enumerate(recs):
        if ridx in drop:
            continue
        ks_i = k_entries.get((ci, ridx)) or []
        ks_j = k_entries.get((cj, ridx)) or []
        if not ks_i or not ks_j:
            continue
        k_i, k_j = (ks_i[0], ks_j[-1]) if ci == cj else (ks_i[0], ks_j[0])
        oi = (k_i + phi.get(ci, 0)) % 2 == 0
        oj = (k_j + phi.get(cj, 0)) % 2 == 0
        if oi == oj:
            oi, oj = (True, False) if ci == cj else (ci < cj, cj < ci)
        over_at[ridx] = (oi, oj)

    # ---- 每带高度调制 top(s)：交叉点处取 1.0/low ----
    # 每交叉的局部平台半长按交叉角自适应：浅角交叉两支并行段长
    # （重叠区 ≈ 带宽/sinθ），平台需覆盖整个重叠区，否则下带在重叠
    # 中段就爬升、出现台阶；深角交叉用基准值。过渡段长度由 _top_along
    # 均摊有界，保证所有交叉的高低过渡均匀。
    def _tan_at(i: int, s: float) -> np.ndarray:
        dense_i, cum_i, _cl = curves[i]
        sq_i = np.linspace(0.0, float(cum_i[-1]), len(dense_i))
        j = int(np.clip(np.searchsorted(sq_i, s), 1, len(dense_i) - 1))
        d = dense_i[j] - dense_i[j - 1]
        ln = float(np.hypot(d[0], d[1]))
        return d / ln if ln > 1e-9 else np.array([1.0, 0.0])

    base_p = max(1.2 * width, 4.0 * sp)
    top_pts: dict[int, list] = {i: [] for i in valid}
    cut_val: dict[int, dict[float, tuple]] = {}   # 自相交切点 → (上下值, 平台)
    for ridx, (ci, s_i, cj, s_j) in enumerate(recs):
        if ridx in drop or ridx not in over_at:
            continue
        di = _tan_at(ci, s_i)
        dj = _tan_at(cj, s_j)
        sin_t = abs(float(di[0] * dj[1] - di[1] * dj[0]))
        p = float(np.clip(1.15 * width / max(sin_t, 0.2),
                          base_p, 4.5 * width))
        o_i, o_j = over_at[ridx]
        top_pts[ci].append((s_i, o_i, p))
        top_pts[cj].append((s_j, o_j, p))
        if ci == cj:
            cut_val.setdefault(ci, {})[s_i] = (1.0 if o_i else low, p)
            cut_val[ci][s_j] = (1.0 if o_j else low, p)

    # ---- 渲染条目：自相交曲线按交叉点拆成多个"经过段"分别栅格化。
    # 单带栅格化时，交叉邻域内像素的最近弧长会在两支之间跳变（两支
    # 弧长差半个曲线），把上带沿中线切开——8 字交叉不连贯的根源；
    # 拆段后各段独立成带、max 合成，上带连续盖过下带。无自相交的
    # 曲线整条一带（行为不变）。拆出的段用局部平台调制：交叉上/下
    # 只作用于交叉附近，段内其余保持满高。----
    render: list[tuple] = []   # (pts, total, s_arr, v_arr, p_arr, ramp_max)
    for i in valid:
        dense, cum, closed_i = curves[i]
        total_i = float(cum[-1])
        sq = np.linspace(0.0, total_i, len(dense))
        merged: list[float] = []
        for s in sorted(cut_val.get(i, {})):
            if merged and s - merged[-1] < eps:
                continue
            merged.append(s)
        use_plat = bool(merged)
        bounds = [0.0] + merged + [total_i]
        ctrl = sorted(top_pts[i])
        # 闭合曲线：接缝（弧长 0 / total，同一物理点）不是带端，
        # 取满高让带环连续（无交叉时整带本就满高）
        end_def = 1.0 if closed_i else 0.0
        for sa, sb in zip(bounds[:-1], bounds[1:]):
            sa, sb = float(sa), float(sb)
            if sb - sa < max(sp, 1e-6):
                continue
            e0 = np.array([[np.interp(sa, sq, dense[:, 0]),
                            np.interp(sa, sq, dense[:, 1])]])
            e1 = np.array([[np.interp(sb, sq, dense[:, 0]),
                            np.interp(sb, sq, dense[:, 1])]])
            inner = dense[(sq > sa + 1e-9) & (sq < sb - 1e-9)]
            sub = np.vstack([e0, inner, e1])
            if sub.shape[0] < 2:
                continue
            iv = [(s - sa, o, p) for s, o, p in ctrl
                  if sa + 1e-6 < s < sb - 1e-6]
            if not ctrl and i not in cut_val:
                s_arr = v_arr = p_arr = None    # 无任何交叉 → 满高平带
            else:
                cv0 = cut_val.get(i, {}).get(sa)
                cv1 = cut_val.get(i, {}).get(sb)
                v0, q0 = cv0 if cv0 is not None else (end_def, base_p)
                v1, q1 = cv1 if cv1 is not None else (end_def, base_p)
                s_arr = np.array([0.0] + [t for t, _o, _q in iv]
                                 + [sb - sa], dtype=np.float64)
                v_arr = np.array([v0] + [1.0 if o else low
                                         for _t, o, _q in iv] + [v1],
                                 dtype=np.float64)
                # 仅自相交拆段用平台调制；普通曲线保持整段余弦（透镜形态）
                p_arr = (np.array([q0] + [q for _t, _o, q in iv] + [q1],
                                  dtype=np.float64) if use_plat else None)
            render.append((sub, sb - sa, s_arr, v_arr, p_arr,
                           3.0 * width if use_plat else 0.0))

    # ---- 子编织（竹编）：每带沿法向均分为 n 条细带，细带间留缝 ----
    n_sub = max(1, int(sub_n))
    if n_sub > 1:
        gap = float(np.clip(float(sub_gap), 0.0, width / n_sub * 0.8))
        w_sub = max((width - (n_sub - 1) * gap) / n_sub, 2.0 * sp)
        offs = [-width * 0.5 + w_sub * 0.5 + i * (w_sub + gap)
                for i in range(n_sub)]
    else:
        w_sub = width
        offs = [0.0]

    # ---- 场合成 ----
    from scipy.ndimage import distance_transform_edt, median_filter
    for sub, sub_total, s_arr, v_arr, p_arr, rmax in render:
        # ROI（注意钳到模型 origin 而非 0——模型坐标系常为居中负值，
        # 钳到 0 会把 origin 左/下侧的曲线整体裁掉、栅格化错位）
        x0 = max(float(hf.origin[0]), float(sub[:, 0].min()) - width)
        x1 = min(float(hf.origin[0] + (nx - 1) * sp),
                 float(sub[:, 0].max()) + width)
        y0 = max(float(hf.origin[1]), float(sub[:, 1].min()) - width)
        y1 = min(float(hf.origin[1] + (ny - 1) * sp),
                 float(sub[:, 1].max()) + width)
        c0 = int((x0 - hf.origin[0]) / sp)
        c1 = min(nx, int((x1 - hf.origin[0]) / sp) + 2)
        r0 = int((y0 - hf.origin[1]) / sp)
        r1 = min(ny, int((y1 - hf.origin[1]) / sp) + 2)
        if c1 - c0 < 2 or r1 - r0 < 2:
            continue
        lw, lh = c1 - c0, r1 - r0

        if s_arr is not None:
            top_common = True
        else:
            top_common = False
        half_i = w_sub * 0.5
        top_w_i = top_w_mm * (w_sub / width)   # 上宽随细带等比缩

        for off in offs:                       # 每条细带（sub_n=1 即主线）
            sub_o = _offset_polyline(sub, off)
            # 曲线打标 → EDT 距离 + 最近弧长
            feats = np.ones((lh, lw), dtype=bool)
            arc = np.zeros((lh, lw), dtype=np.float64)
            gcols = np.clip(((sub_o[:, 0] - hf.origin[0]) / sp - c0
                             ).astype(int), 0, lw - 1)
            grows = np.clip(((sub_o[:, 1] - hf.origin[1]) / sp - r0
                             ).astype(int), 0, lh - 1)
            feats[grows, gcols] = False
            arc[grows, gcols] = np.linspace(0.0, float(sub_total),
                                            len(sub_o))
            dist, idx = distance_transform_edt(feats, return_indices=True)
            s_map = median_filter(arc[idx[0], idx[1]], size=3,
                                  mode="nearest")

            d_mm = dist * sp
            prof = _section_profile(d_mm, half_i, sec, top_w_i,
                                    ellipse_ratio)
            if top_common:
                top = _top_along(s_map, s_arr, v_arr, p_arr=p_arr,
                                 ramp_max=rmax)
            else:
                top = 1.0
            band = prof * top
            # 仅抗锯齿的极窄边缘过渡（截面自身已归零收边）
            band = band * _smoothstep(half_i, half_i * 0.97, d_mm)

            roi = field[r0:r1, c0:c1]
            np.maximum(roi, band.astype(np.float32), out=roi)
            mask[r0:r1, c0:c1] |= d_mm <= half_i

    return np.clip(field, 0.0, 1.0), mask


def weave_grid(hf, rows: int, cols: int, *, band_w: float,
               top_w: float, sec: str = "ellipse",
               ellipse_ratio: float = 1.0,
               low: float = 0.45,
               angle: float = 90.0,
               sub_n: int = 1,
               sub_gap: float = 0.6) -> tuple[np.ndarray, np.ndarray]:
    """整面编织（编织模式）：按横/竖条数铺满模型，走统一的沿曲线管线。

    rows = 横向条数（沿 X 走向的带），cols = 纵向条数（第二组带）。
    angle = 两组带的交叉角度（度，默认 90 = 经纬垂直；第二组带绕
    模型中心旋转，第一组保持水平）。带宽 = band_w（截面下宽），
    自动收缩到 0.9×最小法向间距以内避免重叠糊面。
    中心线向模型外延伸，使带端收口区落在模型外（边上不塌陷）。
    """
    ny, nx = hf.ny, hf.nx
    sp = float(hf.spacing)
    ox, oy = float(hf.origin[0]), float(hf.origin[1])
    W = (nx - 1) * sp
    H = (ny - 1) * sp
    rows = max(1, int(rows))
    cols = max(1, int(cols))
    ang = float(np.clip(float(angle), 10.0, 170.0))
    beta = np.radians(ang)
    dirv = np.array([np.cos(beta), np.sin(beta)])      # 第二组走向
    nrm = np.array([-dirv[1], dirv[0]])                # 其法向
    cctr = np.array([ox + W * 0.5, oy + H * 0.5])
    corners = (np.array([[ox, oy], [ox + W, oy],
                         [ox, oy + H], [ox + W, oy + H]]) - cctr)
    proj = corners @ nrm
    lo, hi = float(proj.min()), float(proj.max())
    pitch2 = (hi - lo) / cols                          # 第二组法向间距
    px = W / cols if abs(dirv[0]) < 1e-9 else pitch2
    py = H / rows
    bw = max(min(float(band_w), 0.9 * px, 0.9 * py), sp * 2.0)
    m = 3.0                                   # 端部收口区移到模型外
    tx = np.linspace(ox - m, ox + W + m, max(8, int((W + 2 * m) / 2.0) + 1))
    ty = np.linspace(oy - m, oy + H + m, max(8, int((H + 2 * m) / 2.0) + 1))
    polys = []
    for i in range(rows):
        y = oy + (i + 0.5) * py
        polys.append(np.stack([tx, np.full_like(tx, y)], axis=1))
    if abs(dirv[1]) > 0.999:                  # 垂直交叉：竖直带（现状路径）
        for j in range(cols):
            x = ox + (j + 0.5) * px
            polys.append(np.stack([np.full_like(ty, x), ty], axis=1))
    else:
        L = 0.5 * np.hypot(W, H) + 2.0 * m + 4.0
        ts = np.linspace(-L, L, max(8, int(2.0 * L / 2.0) + 1))
        base = cctr + nrm * (0.5 * (lo + hi))
        for j in range(cols):
            pos = (j - (cols - 1) * 0.5) * pitch2
            cline = base + nrm * pos
            pts = cline[None, :] + ts[:, None] * dirv[None, :]
            polys.append(pts)
    return weave_along_curves(hf, polys, width_mm=bw, top_w_mm=top_w,
                              sec=sec, ellipse_ratio=ellipse_ratio, low=low,
                              sub_n=sub_n, sub_gap=sub_gap)


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


def fill(ny: int, nx: int, *, pattern: str = "hex", size_mm: float = 6.0,
         gap_x: float = 1.0, gap_y: float = 1.0,
         brick_w_mm: float = 11.5, spacing: float = 0.2,
         bevel: float = 0.4) -> np.ndarray:
    """填充纹理：图案凸起阵列（毫米制几何参数）。

    size_mm = 单个图案尺寸：六边为横向特征宽，砖形为砖长（长边），
    三棱锥为三角形边长，正反三棱锥为金字塔底宽；brick_w_mm =
    砖形宽度（短边，标准砖 240×115 → 24×11.5）；gap_x/gap_y =
    图案之间横向/纵向间距（mm，砖形即灰缝，标准 10 → 1）。
    spacing = 栅格物理间距（由调用方传 hf.spacing）。
    gap_x/gap_y 允许负值：节距小于图案尺寸，图案互相嵌入——
    六边形 gap_x=0、gap_y≈-0.13×尺寸 即为蜂窝（行错缝已内建）。

    图案：
      hex/brick  —— SDF 凸台（世界坐标晶格 + 3×3 邻域取最小并集，
                    负间距时图案伸出所属单元也能完整绘制）。
      pyramid    —— 三棱锥：等边三角形密排阵列（正/倒三角相间、
                    行错缝），每三角形从三边斜坡升到中心小平台
                    （分片线性高度场），缝处成连续 V 谷。
      anti_pyramid —— 正反三棱锥：与三棱锥同一三角晶格，但正三角
                    为凸锥（边 0.5→心 1）、倒三角为凹锥（边 0.5→
                    心 0），三个凸锥围一个凹锥，立方体堆叠视感。
    """
    sp = max(float(spacing), 1e-6)
    size = max(float(size_mm), 3.0 * sp)

    # ---- 三棱锥 / 正反三棱锥：高度场型图案（单元函数 + 邻域 max）----
    if pattern in ("pyramid", "anti_pyramid"):
        x = np.arange(nx, dtype=np.float32)[None, :] * sp
        y = np.arange(ny, dtype=np.float32)[:, None] * sp
        # 三角晶格：等边三角形密排阵列（正/倒三角相间，行错缝）。
        # 晶格节距 = 尺寸+间距；先在等边"拉伸空间"里完美密排，
        # 再由节距把图案撑开（负间距即互相嵌入）。
        px = max(size + float(gap_x), 2.0 * sp)
        py = max(0.8660254 * size + float(gap_y), 2.0 * sp)
        S = size                                # 三角形边长（拉伸空间）
        H = 0.8660254 * S                       # 三角形高
        kx = S / px                             # 世界 → 拉伸空间
        ky = H / py
        if pattern == "pyramid":
            # 三棱锥：每个三角形从三边斜坡升到中心小平台，缝处 V 谷
            r_in = H / 3.0                          # 内切圆半径（中心到边）
            g = min(float(gap_x), float(gap_y))     # 缝宽
            w2 = max(0.5 * g * S / px, -0.9 * r_in)  # 缝半宽（拉伸空间）
            d_top = max(0.85 * (r_in - w2), 1e-6)   # 斜坡段（顶面留平台）

            def shade(dn, d_up, d_dn):
                d = np.where(dn, d_dn, d_up)
                return np.clip((d - w2) / d_top, 0.0, 1.0)
        else:
            # 正反三棱锥：正三角凸三棱锥（边 0.5 → 心 1）、倒三角凹
            # 三棱锥（边 0.5 → 心 0），棱处 0.5 连续——三个凸锥围一
            # 个凹锥，渲染成立方体堆叠视感（面全为平面、无平顶）。
            def shade(dn, d_up, d_dn):
                d = np.where(dn, -np.maximum(d_dn, 0.0),
                             np.maximum(d_up, 0.0))
                return np.clip(0.5 + 1.5 * d / H, 0.0, 1.0)

        def cell(dx, dy):
            # 单元局部坐标：uu∈[0,S] 横向，vv∈[0,H] 纵向
            uu = (dx + 0.5 * px) * kx
            vv = (dy + 0.5 * py) * ky
            bc = H * (1.0 - np.abs(2.0 * uu / S - 1.0))
            # 正三角（底朝下居中、尖朝上）：到三边距离
            d1 = np.minimum(vv, np.minimum(
                (1.7320508 * uu - vv) * 0.5,
                (1.7320508 * (S - uu) - vv) * 0.5))
            # 倒三角（尖朝下在单元左/右角、底朝上跨单元边界）
            dl = np.minimum(H - vv, np.minimum(
                (vv - 1.7320508 * uu) * 0.5,
                (1.7320508 * uu + vv) * 0.5))
            dr = np.minimum(H - vv, np.minimum(
                (1.7320508 * uu + vv - 2.0 * H) * 0.5,
                (2.0 * H - 1.7320508 * uu + vv) * 0.5))
            dn = vv > bc
            return shade(dn, d1, np.where(uu <= 0.5 * S, dl, dr))

        h = np.zeros((ny, nx), np.float32)
        j0 = np.floor(y / py).astype(np.int32)
        if pattern == "anti_pyramid":
            # 场处处连续（棱=0.5），每像素只归属唯一单元；max 并集
            # 会被邻胞溢出的 0.5 平场抹平凹锥，故不做邻域循环
            sx = 0.5 * px * (j0 % 2)           # 行错缝半格
            k0 = np.round((x - sx) / px - 0.5).astype(np.int32)
            cx = (k0 + 0.5) * px + sx
            return cell(x - cx, y - (j0 + 0.5) * py).astype(np.float32)
        for oi in (-1, 0, 1):                  # 3×3 邻域并集（负间距互嵌）
            jr = j0 + oi
            dyv = y - (jr + 0.5) * py
            sx = 0.5 * px * (jr % 2)           # 行错缝半格
            k0 = np.round((x - sx) / px - 0.5).astype(np.int32)
            for oj in (-1, 0, 1):
                cx = (k0 + oj + 0.5) * px + sx
                np.maximum(h, cell(x - cx, dyv), out=h)
        return h

    # ---- 六边 / 砖形：SDF 凸台 ----
    bw = max(float(brick_w_mm), 3.0 * sp)       # 砖形宽度（短边）
    fw = bw if pattern == "brick" else size     # Y 方向特征尺寸
    # 允许负间距（图案互嵌），仅钳正节距避免除零；过密时图案并成整体
    px = max(size + float(gap_x), 2.0 * sp)     # X 方向节距（中心距）
    py = max(fw + float(gap_y), 2.0 * sp)       # Y 方向节距
    x = np.arange(nx, dtype=np.float32)[None, :] * sp
    y = np.arange(ny, dtype=np.float32)[:, None] * sp
    a = 0.5 * size                              # 图案半宽（mm）
    b = 0.5 * fw                                # Y 方向半尺寸（砖形=宽/2）

    def sdf(dx, dy) -> np.ndarray:
        """单个图案的有符号距离（<0 图案内）。dx/dy 相对图案中心。"""
        adx, ady = np.abs(dx), np.abs(dy)
        if pattern == "hex":
            # 六边形：竖直边 + 60° 斜边（宽 2a，高 1.155a，尖角朝上下）
            return np.maximum(adx - a, 0.5 * adx + 0.866 * ady - a)
        return np.maximum(adx - a, ady - b)           # 矩形（砖形 长×宽）

    offset = pattern in ("brick", "hex")
    d = np.full((ny, nx), np.inf, np.float32)
    j0 = np.floor(y / py).astype(np.int32)
    for oi in (-1, 0, 1):                    # 3×3 邻域胞并集
        jr = j0 + oi
        dyv = y - (jr + 0.5) * py
        sx = 0.5 * px * (jr % 2) if offset else 0.0   # 行错缝半格
        k0 = np.round((x - sx) / px - 0.5).astype(np.int32)
        for oj in (-1, 0, 1):
            cx = (k0 + oj + 0.5) * px + sx
            np.minimum(d, sdf(x - cx, dyv), out=d)
    d = d / max(float(bevel), 0.05)             # bevel(mm) → 归一化过渡带
    band = 1.0 - _smoothstep(-0.5, 0.5, d)
    return _norm01(band.astype(np.float32))


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


def water(ny: int, nx: int, *, seed: int = 0, cells: int = 6,
          waves: int = 3, ripple_gain: float = 0.6,
          damping: float = 2.0) -> np.ndarray:
    """水波纹理：方向行进波 + 随机溅落同心涟漪（环面拓扑 → 可平铺）。

    cells=特征波距（越大波越稀疏）；waves=方向波条数（整数周期无缝）；
    ripple_gain=涟漪相对波幅占比；damping=涟漪衰减（越大可见圈数越少）。
    涟漪中心胞内随机散布，取最近中心距离 F1（3×3 邻域向量化）。
    """
    rng = np.random.default_rng(seed)
    yy = np.arange(ny, dtype=np.float32)[:, None] / ny
    xx = np.arange(nx, dtype=np.float32)[None, :] / nx
    f = np.zeros((ny, nx), np.float32)
    norm = 0.0
    cw = max(2, int(cells))
    # ---- 方向行进波：整数周期铺满画布 → 边界无缝 ----
    for i in range(max(1, int(waves))):
        m = cw + 2 * i                       # 各波频率错开
        ang = rng.random() * 2 * np.pi
        ph = rng.random() * 2 * np.pi
        f += np.sin(2 * np.pi * m * (xx * np.cos(ang) + yy * np.sin(ang))
                    + ph)
        norm += 1.0
    # ---- 溅落涟漪：胞内随机中心 + 最近距离衰减环形波 ----
    gy = gx = max(2, int(round(cw * 0.6)))
    jy = rng.random(gy).astype(np.float32)
    jx = rng.random(gx).astype(np.float32)
    by = (np.arange(ny) / ny * gy).astype(np.int32) % gy
    bx = (np.arange(nx) / nx * gx).astype(np.int32) % gx
    ry = np.arange(ny, dtype=np.float32)
    rx = np.arange(nx, dtype=np.float32)
    bh, bw = ny / gy, nx / gx
    d1 = np.full((ny, nx), np.inf, np.float32)
    for oy in (-1, 0, 1):
        fy = (by + oy + jy[(by + oy) % gy]) * bh
        dy = (ry - fy)[:, None]
        for ox in (-1, 0, 1):
            fx = (bx + ox + jx[(bx + ox) % gx]) * bw
            dx = (rx - fx)[None, :]
            d = np.sqrt(dy * dy + dx * dx)
            np.minimum(d1, d, out=d1)
    cell = min(bh, bw)
    lam = cell / 2.5                         # 每胞约 2~3 圈涟漪
    ripple = np.sin(2 * np.pi * d1 / lam) * np.exp(-d1 * damping / cell)
    g = float(np.clip(ripple_gain, 0.0, 2.0))
    f += g * ripple * max(norm, 1e-9)
    return _norm01((f / max(norm * (1.0 + g), 1e-9)).astype(np.float32))


def waves(ny: int, nx: int, *, seed: int = 0, count: int = 8,
          curvature: float = 0.3, angle: float = 0.0,
          sharp: float = 0.3, waver: float = 0.4,
          uneven: float = 0.4, ripple: float = 0.18) -> np.ndarray:
    """波浪纹理：水面波列——高低起伏的平行/弧形波纹 + 自然随机性。

    count=整面高低起伏的次数（波峰数）；curvature=弧度（0=直线的波浪，
    1=同心圆弧）；angle=波浪前进方向（度，0=沿 +X）；sharp=波形前倾。
    随机性（seed 驱动，叠加在规则波列上）：
      waver=波前抖动（相位被低频噪声推移，波纹不平行/不整齐）；
      uneven=波幅起伏（各波峰高低不一）；
      ripple=细碎小波叠加。
    实现：波前相位 = 到虚拟波源的距离 / 波长。波源放在前进方向反侧、
    距画布中心 D；弧度→D 减小（0→D 很大≈直线，1→D=0 同心圆）。
    """
    rng = np.random.default_rng(seed)
    size = float(max(ny, nx))
    c = float(np.clip(curvature, 0.0, 1.0))
    count = max(1, int(count))
    # 波源距离：弧度越大波源越近 → 波前越弯
    D = size * 0.5 * (1.0 - c) / max(c, 0.02)
    ang = math.radians(float(angle))
    yy = (np.arange(ny, dtype=np.float32)[:, None] - ny * 0.5)
    xx = (np.arange(nx, dtype=np.float32)[None, :] - nx * 0.5)
    s = np.sqrt((xx + math.cos(ang) * D) ** 2
                + (yy + math.sin(ang) * D) ** 2)
    lam = size / count                       # 波长：整面 count 个高低
    ph = rng.random() * 2 * np.pi            # 种子 → 波峰错位
    # ---- 随机性叠加（特征尺度随波长缩放）----
    wv = float(np.clip(waver, 0.0, 0.9))
    un = float(np.clip(uneven, 0.0, 0.9))
    if wv > 0:
        # 相位扰动：低频噪声把波前推前/推后 ≤ wv 个周期（波前弯折不齐）
        n1 = _value_noise(ny, nx, max(3, count), max(3, count), rng)
        ph = ph + wv * 2 * np.pi * (n1 - 0.5)
    p = 2 * np.pi * s / lam + ph
    k = float(np.clip(sharp, 0.0, 0.8))
    prof = (np.sin(p) + k * np.sin(2 * p)) / (1.0 + k)
    if un > 0:
        # 波幅起伏：低频噪声让各段波峰高低不一
        n2 = _value_noise(ny, nx, max(2, count // 2 + 1),
                          max(2, count // 2 + 1), rng)
        prof = prof * (1.0 + un * 2.0 * (n2 - 0.5))
    rp = float(np.clip(ripple, 0.0, 0.6))
    if rp > 0:
        # 细碎波纹：约 1/4 波长尺度的微起伏
        n3 = _value_noise(ny, nx, count * 4 + 1, count * 4 + 1, rng)
        prof = prof + rp * (n3 - 0.5) * 2.0
    return _norm01(prof.astype(np.float32))


def rough(ny: int, nx: int, *, seed: int = 0, cells: int = 30,
          octaves: int = 3) -> np.ndarray:
    """糙面纹理：细粒噪声 + 少量低频起伏（自然不规则，无正弦网纹）。

    cells=细节尺度格数（越大颗粒越细）；两级高频 value-noise 叠出
    砂粒感，再叠 octaves 层低频起伏打破均匀感。
    """
    rng = np.random.default_rng(seed)
    c0 = max(4, int(cells))
    f = _value_noise(ny, nx, c0 * 4, c0 * 4, rng)             # 细粒层
    f += 0.5 * _value_noise(ny, nx, c0 * 8 + 1, c0 * 8 + 1, rng)  # 更细颗粒
    amp, norm, cy = 1.0, 1.5, c0
    for _ in range(max(1, int(octaves))):                     # 低频起伏层
        f += amp * _value_noise(ny, nx, cy, cy, rng)
        norm += amp
        amp *= 0.5
        cy = cy * 2 + 1
    return _norm01((f / norm).astype(np.float32))


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
    "water": water,
    "waves": waves,
    "rough": rough,
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
    rand_sub: str = "fractal"     # 随机子模式：fractal/waves/rough
    waves_count: int = 8          # 波浪·整面高低起伏次数（波峰数）
    waves_curv: float = 0.3       # 波浪·弧度（0=直线波，1=同心圆弧）
    waves_angle: float = 0.0      # 波浪·前进方向（度）
    fill_pattern: str = "hex"
    fill_size: float = 6.0       # 填充·单个图案尺寸（mm，砖形=砖长）
    fill_brick_w: float = 11.5   # 填充·砖形宽度（mm，标准砖 24×11.5）
    fill_gap_x: float = 1.0      # 填充·横向间距（mm，砖形=灰缝）
    fill_gap_y: float = 1.0      # 填充·纵向间距（mm，砖形=灰缝）
    height_mm: float = 1.0        # 纹理高度振幅（mm）
    strength: float = 1.0         # 强度倍率（0=无效果）
    detail_mm: float = 5.0        # 细节物理尺度（mm）→ 换算特征格数
    seed: int = 0
    blend: str = "add"            # add 叠加 / replace 取代 / subtract 切除
    color_scope: str = "ignore"   # ignore / inside / outside（色号蒙版）
    # 编织带专用（kind == "weave"）
    weave_mode: str = "grid"      # grid 编织(整面固定) / curve 缠绕(沿曲线)
    weave_rows: int = 8           # 编织·横向条数
    weave_cols: int = 8           # 编织·纵向条数
    weave_angle: float = 90.0     # 编织·交叉角度（度，90=经纬垂直）
    weave_sec: str = "ellipse"    # 截面：ellipse / trapezoid / spline
    weave_top_w: float = 1.5      # 截面上宽(mm)，=下宽→矩形，→0 为三角
    weave_bot_w: float = 4.0      # 截面下宽(mm)（=带宽度）
    weave_ellipse_ratio: float = 1.0   # 椭圆高度比（1=半圆，越小越扁）
    weave_sub_n: int = 1          # 子编织·每带细条数（1=不细分）
    weave_sub_gap: float = 0.6    # 子编织·细条缝宽(mm)


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
                  poly: np.ndarray | None = None,
                  polys: list | None = None) -> int:
    """生成几何纹理并叠入高度场，返回被修改的格子数。

    hf: HeightField（就地修改 data）。poly=None → 整个模型；否则只在
    该闭合曲线（世界坐标采样点）围成的区域内生效。color_scope 与
    :func:`core.relief.region_relief.apply_region_relief` 同语义。
    polys：编织带·沿曲线模式的多条路径（群组曲线采样点列表）。
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

    # 随机子模式：分形/腐蚀/糙面(perturb)/水波(water) → 实际生成器
    if kind == "fractal":
        kind = _RAND_SUB_MAP.get(
            getattr(params, "rand_sub", "fractal") or "fractal", "fractal")

    # 编织/缠绕：weave=整面编织（按横/竖条数固定铺满）；
    # wrap=缠绕（群组内每条曲线 = 一根带的中心线，需提供 polys）
    if kind in ("weave", "wrap"):
        if polys and (kind == "wrap"
                      or getattr(params, "weave_mode", "grid") == "curve"):
            field_c, mask_c = weave_along_curves(
                hf, polys,
                width_mm=getattr(params, "weave_bot_w", 4.0),
                top_w_mm=getattr(params, "weave_top_w", 1.5),
                sec=getattr(params, "weave_sec", "ellipse"),
                ellipse_ratio=getattr(params, "weave_ellipse_ratio", 1.0),
                sub_n=int(getattr(params, "weave_sub_n", 1)),
                sub_gap=float(getattr(params, "weave_sub_gap", 0.6)),
            )
        elif kind == "wrap":
            return 0        # 缠绕未提供曲线：不生成
        else:
            field_c, mask_c = weave_grid(
                hf,
                getattr(params, "weave_rows", 8),
                getattr(params, "weave_cols", 8),
                band_w=getattr(params, "weave_bot_w", 4.0),
                top_w=getattr(params, "weave_top_w", 1.5),
                sec=getattr(params, "weave_sec", "ellipse"),
                ellipse_ratio=getattr(params, "weave_ellipse_ratio", 1.0),
                angle=float(getattr(params, "weave_angle", 90.0)),
                sub_n=int(getattr(params, "weave_sub_n", 1)),
                sub_gap=float(getattr(params, "weave_sub_gap", 0.6)),
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

    if kind in ("fractal", "corrosion", "rectgrid", "perturb", "water",
                "waves", "rough"):
        kw["seed"] = int(params.seed)
    if kind == "fractal":
        kw["base_cells"] = cells
    elif kind in ("corrosion", "water"):
        kw["cells"] = cells
    elif kind == "waves":
        kw["count"] = int(getattr(params, "waves_count", 8))
        kw["curvature"] = float(getattr(params, "waves_curv", 0.3))
        kw["angle"] = float(getattr(params, "waves_angle", 0.0))
    elif kind == "rough":
        kw["cells"] = cells
    elif kind == "weave":
        kw["bands"] = cells
    elif kind == "fill":
        kw["pattern"] = params.fill_pattern
        if params.fill_pattern == "brick":
            # 砖形：长/宽独立（默认标准砖 24×11.5，灰缝 1）
            kw["size_mm"] = float(getattr(params, "fill_size", 24.0))
            kw["brick_w_mm"] = float(getattr(params, "fill_brick_w", 11.5))
        else:
            kw["size_mm"] = float(getattr(params, "fill_size", 6.0))
        kw["gap_x"] = float(getattr(params, "fill_gap_x", 1.0))
        kw["gap_y"] = float(getattr(params, "fill_gap_y", 1.0))
        kw["spacing"] = float(hf.spacing)
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

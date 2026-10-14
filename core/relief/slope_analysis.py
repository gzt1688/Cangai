"""坡度/残料分析（复刻精雕 SurfMill"残料边界线和角度分区线"）。

从高度场直接派生两类 2D 曲线（供区域浮雕 / CNC 残料补加工取用）：

- **角度分区线**：按网格坡度把模型分成平坦/缓坡/陡坡/陡立等区，
  输出各区分界闭曲线（默认 10°/30°/60° 分界，可配）。
- **残料边界线**：给定刀具半径与刀形，计算刀具**够不到**的残留材料
  区（球刀按球面包络、平底刀按圆柱包络的形态学膨胀），输出其边界
  闭曲线；threshold 控制"残留深度小于该值的忽略"。

纯 numpy 实现（零 Qt 依赖）：坡度用中心差分；残料包络用**形态学
闭运算**（closing，球刀/平底刀的可达包络皆为其圆盘 closing——"沿
表面滚动的刀具最低点轨迹"，滚动不进去的凹坑即残料）；等值线用
marching squares（mask 外扩一圈 False 衬边，保证输出均为闭合环）。
世界坐标换算遵循 HeightField 约定（row=Y、col=X，origin 为 (0,0)
节点世界坐标）。大网格（>512²）自动块最大降采样分析、掩码放大回填，
控制单次分析耗时。
"""
from __future__ import annotations

import math
from typing import List, Sequence, Tuple

import numpy as np

# 默认坡度分界（度）：0-10 平坦 / 10-30 缓坡 / 30-60 陡坡 / 60-90 陡立
DEFAULT_ZONE_BOUNDS: Tuple[float, ...] = (10.0, 30.0, 60.0)

ZONE_NAMES: Tuple[str, ...] = ("平坦区", "缓坡区", "陡坡区", "陡立区")

# 分区曲线着色（与 2D 画布对象色一致的三元组 0-255）
ZONE_COLORS: Tuple[Tuple[int, int, int], ...] = (
    (46, 160, 67),    # 平坦区 绿
    (243, 156, 18),   # 缓坡区 橙
    (230, 80, 40),    # 陡坡区 红
    (150, 40, 180),   # 陡立区 紫
)


# ------------------------------------------------------------------ #
# 坡度
# ------------------------------------------------------------------ #

def slope_degrees(hf) -> np.ndarray:
    """每格坡度（度）：中心差分梯度模长 × 180/π。边界格用单侧差分。"""
    h = np.asarray(hf.data, dtype=np.float64)
    sp = float(hf.spacing)
    gy = np.gradient(h, axis=0) / sp
    gx = np.gradient(h, axis=1) / sp
    mag = np.hypot(gx, gy)
    return np.degrees(np.arctan(mag)).astype(np.float32)


def slope_zone_masks(hf, bounds: Sequence[float] = DEFAULT_ZONE_BOUNDS
                     ) -> List[np.ndarray]:
    """按分界角度返回各区布尔掩码列表（len = len(bounds)+1）。"""
    sd = slope_degrees(hf)
    masks: List[np.ndarray] = []
    lo = 0.0
    for b in list(bounds) + [90.0]:
        masks.append((sd >= lo) & (sd < b))
        lo = b
    return masks


# ------------------------------------------------------------------ #
# 残料（刀具够不到的区域）
# ------------------------------------------------------------------ #

def _disk_offsets(radius_px: float):
    """圆盘整数偏移列表 [(dy, dx)]，半径 radius_px（格）。"""
    r = max(1, int(math.floor(radius_px + 0.5)))
    out: List[Tuple[int, int]] = []
    r2 = float(radius_px) * float(radius_px)
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dy * dy + dx * dx <= r2 + 1e-9:
                out.append((dy, dx))
    return out


def _shift(base: np.ndarray, dy: int, dx: int, fill: float) -> np.ndarray:
    """平移 (dy,dx)（正=向右/下），越界填 fill。"""
    out = np.full_like(base, fill)
    ny, nx = base.shape
    ys0, ys1 = max(0, dy), min(ny, ny + dy)
    xs0, xs1 = max(0, dx), min(nx, nx + dx)
    if ys1 <= ys0 or xs1 <= xs0:
        return out
    out[ys0:ys1, xs0:xs1] = base[ys0 - dy:ys1 - dy, xs0 - dx:xs1 - dx]
    return out


def _ball_sweep_levels(h: np.ndarray, R_px: float, spacing: float
                       ) -> np.ndarray:
    """球刀（半径 R_px 格）扫掠后的可到达面高度（mm，与 h 同单位）。

    精确两遍扫描（rest machining 的标准包络模型）：
      1. U(c) = max_{|q-c|≤R} [h(q) + sqrt(R²-|q-c|²)]  —— 球搁在表面时
         球心最低高度（q 为接触列）；
      2. M(x) = min_{|c-x|≤R} [U(c) - sqrt(R²-|x-c|²)]   —— 各搁放位置
         球底在列 x 的最低高度（最深一次走刀）；
    残料 = 原表面低于 M 的材料（h < M - threshold）。
    平底刀（tool="flat"）包络不同，由调用方走 closing 近似（见下）。
    """
    R2 = R_px * R_px
    U = np.full(h.shape, -np.inf, np.float32)
    for dy, dx in _disk_offsets(R_px):
        d2 = dy * dy + dx * dx
        dep = math.sqrt(max(R2 - d2, 0.0)) * spacing   # mm
        np.maximum(U, _shift(h, dy, dx, -np.inf) + dep, out=U)
    M = np.full(h.shape, np.inf, np.float32)
    for dy, dx in _disk_offsets(R_px):
        d2 = dy * dy + dx * dx
        dep = math.sqrt(max(R2 - d2, 0.0)) * spacing
        np.minimum(M, _shift(U, dy, dx, np.inf) - dep, out=M)
    return M


def rest_material_mask(hf, tool_radius_mm: float, tool: str = "ball",
                       threshold_mm: float = 0.0) -> np.ndarray:
    """残留材料掩码：刀具够不到的格子为 True。

    ball：精确球刀包络（两遍扫描，见 :func:`_ball_sweep_levels`）——
    窄沟/尖角/小 R 内拐角等滚动不进去的区域均为残料。
    flat：平底刀包络近似 = 圆盘形态学 closing（footprint 内取最高面），
    对窄沟类残料同样有效（尖角精度略低于球刀模型）。
    threshold_mm：残留深度 ≤ 阈值的忽略（默认 0）。

    大网格自动降采样分析（块最大，保守不漏）后掩码放大回填；
    分析分辨率同时受"偏移数上限"约束，控制单次分析耗时（秒级）。
    """
    h = np.asarray(hf.data, dtype=np.float32)
    sp = float(hf.spacing)
    R_mm = float(tool_radius_mm)
    R_px = R_mm / sp
    if R_px < 1.0 or h.size == 0:
        return np.zeros(h.shape, dtype=bool)
    # 降采样：按"运算量预算"选 ds（偏移数 ≈ πr²/4 × 两遍扫描），
    # 同时限制分析半径 ≤ 32 格；保证秒级完成。
    ds = 1
    while ds < 16:
        r2 = R_px / ds
        px = (h.shape[0] // ds) * (h.shape[1] // ds)
        if r2 <= 32.0 and px * (math.pi * r2 * r2 * 0.25) * 2 <= 3.0e8:
            break
        ds *= 2
    if ds > 1:
        ny2, nx2 = h.shape[0] // ds, h.shape[1] // ds
        h2 = h[:ny2 * ds, :nx2 * ds].reshape(
            ny2, ds, nx2, ds).max(axis=(1, 3))
        R2 = R_px / ds
    else:
        h2, R2 = h, R_px
    if tool == "flat":
        # 平底刀：closing 近似（cv2 优先，scipy 兜底，纯 numpy 偏移兜底）
        try:
            import cv2
            r = max(1, int(round(R2)))
            yy, xx = np.ogrid[-r:r + 1, -r:r + 1]
            k = ((yy * yy + xx * xx) <= r * r).astype(np.uint8)
            env = cv2.morphologyEx(np.ascontiguousarray(h2),
                                   cv2.MORPH_CLOSE, k,
                                   borderType=cv2.BORDER_REPLICATE)
            mask2 = h2 < env - float(threshold_mm)
        except Exception:
            M = _ball_sweep_levels(h2, R2, sp * ds)
            mask2 = M > h2 + float(threshold_mm)
    else:
        M = _ball_sweep_levels(h2, R2, sp * ds)
        mask2 = M > h2 + float(threshold_mm)
    if ds > 1:
        mask = np.repeat(np.repeat(mask2, ds, axis=0), ds, axis=1)
        if mask.shape[0] < h.shape[0] or mask.shape[1] < h.shape[1]:
            full = np.zeros(h.shape, dtype=bool)
            full[:mask.shape[0], :mask.shape[1]] = mask
            mask = full
        else:
            mask = mask[:h.shape[0], :h.shape[1]]
    else:
        mask = mask2
    return mask


# ------------------------------------------------------------------ #
# Marching Squares → 世界坐标闭合曲线
# ------------------------------------------------------------------ #

def mask_contours(mask: np.ndarray, hf, simplify_mm: float = 0.02
                  ) -> List[np.ndarray]:
    """布尔掩码 → 世界坐标闭合折线列表（marching squares）。

    掩码外扩一圈 False 衬边：贴模型边界的残料区也会沿边界闭合成环。
    返回的每条折线为 (N,2) float64 世界坐标（首尾点重合）。
    """
    m = np.asarray(mask, dtype=bool)
    if m.ndim != 2 or not m.any():
        return []
    ny, nx = m.shape
    padded = np.zeros((ny + 2, nx + 2), dtype=bool)
    padded[1:ny + 1, 1:nx + 1] = m
    # 边交点收集：key=(y,x,边) → 点（栅格浮点坐标）
    segs: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
    for r in range(ny + 1):
        row_t = padded[r]
        row_b = padded[r + 1]
        for c in range(nx + 1):
            tl, tr = bool(row_t[c]), bool(row_t[c + 1])
            bl, br = bool(row_b[c]), bool(row_b[c + 1])
            code = (tl << 3) | (tr << 2) | (br << 1) | bl
            if code == 0 or code == 15:
                continue
            # 角点编码：tl=8 tr=4 br=2 bl=1；四条边：上/右/下/左
            # 等值线段表（对角线 5/10 取竖/横一种解析，简单形状均为闭环）
            top = (c + 0.5, float(r))
            right = (float(c + 1), r + 0.5)
            bottom = (c + 0.5, float(r + 1))
            left = (float(c), r + 0.5)
            pairs = {
                1: (left, bottom),    # 仅 bl
                2: (bottom, right),   # 仅 br
                3: (left, right),     # 下边两点
                4: (top, right),      # 仅 tr
                5: (top, bottom),     # 左边两点（竖切）
                6: (top, bottom),     # 右边两点（竖切）
                7: (top, left),       # 切 tl 角（仅缺 tl）
                8: (top, left),       # 仅 tl
                9: (top, bottom),     # 左边两点（竖切）
                10: (left, right),    # 对角线 → 横切
                11: (top, right),     # 切 tr 角（仅缺 tr）
                12: (left, right),    # 上边两点（横切）
                13: (bottom, right),  # 切 br 角（仅缺 br）
                14: (left, bottom),   # 切 bl 角（仅缺 bl）
            }[code]
            segs.append(pairs)
    if not segs:
        return []
    loops = _stitch_segments(segs)
    # 栅格浮点 → 世界坐标（衬边偏移 -1），可选简化
    ox, oy = hf.origin
    sp = float(hf.spacing)
    out: List[np.ndarray] = []
    for pts in loops:
        arr = np.array([(ox + (p[0] - 1.0) * sp, oy + (p[1] - 1.0) * sp)
                        for p in pts], dtype=np.float64)
        if simplify_mm > 0 and len(arr) > 4:
            arr = _decimate(arr, simplify_mm)
        if len(arr) >= 3:
            out.append(arr)
    return out


def _stitch_segments(
        segs: List[Tuple[Tuple[float, float], Tuple[float, float]]]
) -> List[List[Tuple[float, float]]]:
    """把等值线段按端点（量化 1e-6）缝合进闭合环。

    端点互不重合的段（如插值 marching squares 的鞍点分裂）各自成环，
    不会把相邻两个环粘连成自交多边形。
    """
    adj: Dict[Tuple[int, int], List[Tuple[int, int]]] = {}
    for a, b in segs:
        ka = (round(a[0] * 1e6), round(a[1] * 1e6))
        kb = (round(b[0] * 1e6), round(b[1] * 1e6))
        adj.setdefault(ka, []).append(kb)
        adj.setdefault(kb, []).append(ka)
    used = set()
    loops: List[List[Tuple[float, float]]] = []
    for start in list(adj.keys()):
        if start in used:
            continue
        chain = [start]
        used.add(start)
        cur = start
        while True:
            cands = adj.get(cur, ())
            nxt = None
            for cand in cands:
                if cand not in used:      # 优先走未访问节点
                    nxt = cand
                    break
            if nxt is None:
                # 无未访问邻居：起点若是邻居则闭环，否则是断链（丢弃）
                if start in cands:
                    chain.append(start)
                break
            used.add(nxt)
            chain.append(nxt)
            cur = nxt
        if len(chain) >= 8 and chain[0] == chain[-1]:
            pts = [(p[0] / 1e6, p[1] / 1e6) for p in chain[:-1]]
            loops.append(pts)
    return loops


def _decimate(poly: np.ndarray, tol: float) -> np.ndarray:
    """弦高容差抽稀：合并中间点，保证每个被删点到其所在弦的垂距 ≤ tol。

    与"逐点三点垂距"贪心不同，弦高约束给整条折线的几何偏差一个硬上界
    （三点垂距版在大半径圆弧上会合并出弦高远超 tol 的长弦，放大视图
    下呈现为多边形）。
    """
    n = len(poly)
    if n < 3:
        return poly
    closed = float(np.hypot(*(poly[0] - poly[-1]))) < 1e-9
    work = np.vstack([poly[:-1], poly[0]]) if closed else poly
    m = len(work)
    keep = [0]
    i = 0
    while i < m - 1:
        last = i + 1
        j = i + 2
        while j < m:
            a = work[i]
            ab = work[j] - a
            L = float(np.hypot(*ab))
            mid = work[i + 1:j]
            if L > 1e-12 and mid.size:
                d = np.abs(ab[0] * (mid[:, 1] - a[1])
                           - ab[1] * (mid[:, 0] - a[0])) / L
                if d.max() <= tol:
                    last = j
                    j += 1
                    continue
            break
        keep.append(last)
        i = last
        if i >= m - 1:
            break
    out = work[keep]
    if closed and len(out) > 1:
        out = out[:-1]                    # 去掉显式闭合尾点
    return out


def field_contours(field: np.ndarray, hf, level: float,
                   simplify_mm: float = 0.02) -> List[np.ndarray]:
    """标量场等值线（插值 marching squares）→ 世界坐标闭合折线列表。

    与"先阈值成布尔掩码再 :func:`mask_contours`"相比的优势：
      1. 线性插值取交点，亚格子精度——粗网格模型上的圆不会退化成
         少数顶点的多边形（布尔掩码 + 边中点在粗网格下正是如此）；
      2. 鞍点单元（对角双进双出）按四角均值分裂为两段独立小环，
         两个相邻等值环不再共顶点粘连成自交多边形（"8"字穿插）。

    field 与 hf.data 同形状（行=y、列=x），level 语义与
    ``field >= level`` 一致；返回每条折线 (N,2) float64 世界坐标。
    """
    f = np.asarray(field, dtype=np.float64)
    if f.ndim != 2 or f.size == 0:
        return []
    ny, nx = f.shape
    inside = f >= float(level)
    segs: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
    lvl = float(level)

    def _hpt(r: int, c: int) -> Tuple[float, float]:
        """行 r 上 c~c+1 横边交点（格坐标 x=c+t, y=r）。"""
        v0, v1 = f[r, c], f[r, c + 1]
        t = 0.5 if v1 == v0 else (lvl - v0) / (v1 - v0)
        return (c + float(np.clip(t, 0.0, 1.0)), float(r))

    def _vpt(r: int, c: int) -> Tuple[float, float]:
        """列 c 上 r~r+1 竖边交点（格坐标 x=c, y=r+t）。"""
        v0, v1 = f[r, c], f[r + 1, c]
        t = 0.5 if v1 == v0 else (lvl - v0) / (v1 - v0)
        return (float(c), r + float(np.clip(t, 0.0, 1.0)))

    for r in range(ny - 1):
        rt = inside[r]                # 上行（row r）
        rb = inside[r + 1]            # 下行（row r+1）
        ft = f[r]
        fb = f[r + 1]
        for c in range(nx - 1):
            tl = bool(rt[c]); tr = bool(rt[c + 1])
            bl = bool(rb[c]); br = bool(rb[c + 1])
            code = (tl << 3) | (tr << 2) | (br << 1) | bl
            if code == 0 or code == 15:
                continue
            T = _hpt(r, c)            # top 边（tl-tr）
            B = _hpt(r + 1, c)        # bottom 边（bl-br）
            L = _vpt(r, c)            # left 边（tl-bl）
            R = _vpt(r, c + 1)        # right 边（tr-br）
            if code == 5 or code == 10:
                # 鞍点：按四角均值决定连接方式，两段各自独立成环
                ctr = 0.25 * (ft[c] + ft[c + 1] + fb[c + 1] + fb[c])
                if (code == 5 and ctr >= lvl) or \
                        (code == 10 and ctr < lvl):
                    # tr-bl（5）/ tl-br（10）内部连通：切掉外侧两角
                    segs.append((T, L))
                    segs.append((B, R))
                else:
                    # 对角两 blob 分离：切掉内侧两角
                    segs.append((T, R))
                    segs.append((B, L))
                continue
            pairs = {
                1: (L, B),            # 仅 bl
                2: (B, R),            # 仅 br
                3: (L, R),            # 下边两点
                4: (T, R),            # 仅 tr
                6: (T, B),            # 右边两点（竖切）
                7: (T, L),            # 仅缺 tl
                8: (T, L),            # 仅 tl
                9: (T, B),            # 左边两点（竖切）
                11: (T, R),           # 仅缺 tr
                12: (L, R),           # 上边两点（横切）
                13: (B, R),           # 仅缺 br
                14: (L, B),           # 仅缺 bl
            }[code]
            segs.append(pairs)
    if not segs:
        return []
    loops = _stitch_segments(segs)
    ox, oy = hf.origin
    sp = float(hf.spacing)
    out: List[np.ndarray] = []
    for pts in loops:
        arr = np.array([(ox + p[0] * sp, oy + p[1] * sp) for p in pts],
                       dtype=np.float64)
        if simplify_mm > 0 and len(arr) > 4:
            arr = _decimate(arr, simplify_mm)
        # 抽稀在大半径圆弧上会把顶点间隔拉到数毫米（弦高偏差远超
        # tol），渲染直线连接后放大即是"多边形"。按弧长上限补点，
        # 保证光滑等值线的折线密度（弦高偏差 < 0.01mm 量级）。
        if len(arr) >= 3:
            perim = float(np.hypot(*(arr - np.roll(arr, -1, axis=0)).T).sum())
            max_gap = max(0.3, min(1.0, perim / 144.0))
            arr = _densify(arr, max_gap)
        if len(arr) >= 3:
            out.append(arr)
    return out


def _densify(poly: np.ndarray, max_gap: float) -> np.ndarray:
    """按弧长上限补点：相邻点距超过 max_gap 的段线性插值加密（闭环）。"""
    closed = np.hypot(*(poly[0] - poly[-1])) < 1e-9
    if closed and len(poly) > 1:
        pts = np.vstack([poly[:-1], poly[0]])     # 显式闭合段序列
    else:
        pts = poly
    seg = np.hypot(*(np.diff(pts, axis=0)).T)
    out = [pts[0]]
    for i, L in enumerate(seg):
        if L <= 1e-12:
            continue
        n = int(np.ceil(L / max_gap))
        if n > 1:
            tt = np.linspace(0.0, 1.0, n + 1, endpoint=False)[1:]
            out.append(pts[i] + tt[:, None] * (pts[i + 1] - pts[i]))
        out.append(pts[i + 1])
    arr = np.vstack(out)
    if closed:
        arr = arr[:-1]                            # 去掉显式闭合尾点
    return arr

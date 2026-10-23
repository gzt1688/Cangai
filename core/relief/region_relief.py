"""区域浮雕（曲面区域浮雕）核心算法 —— 复刻自 Rhino 插件 ReliefAlgorithms。

总体架构（与插件完全一致）：**总高度 = 基高 + 自由高度**

- **基高（base height）**：仅依赖 ``baseHeight`` + ``baseAngle``，等价于 Rhino 的
  ``ExtrudeCrvTapered`` 拔模拉伸。对曲线采样 → 塌缩为折角点 → 每个栅格顶点取到折线
  的最近欧氏距离 d → 高度 = min(d × tan(基角), 基高)。斜面是平整直纹面、屋脊锐利。
  这是**所有截面类型的公共基础**。
- **自由高度（free bulge）**：在基高之上叠加的"鼓起"，各截面剖面不同。

截面类型：
- ``rectangle``：只有基高，无自由鼓起。
- ``ellipse`` / ``spline`` / ``triangle`` / ``circle``：基高 + 穹顶鼓起。
  鼓起基于有符号距离场（SDF，精确欧氏距离变换），归一化 s = d/dmax 后按剖面抬升；
  穹顶的"脚"落在基高平台边缘（sPlatform）上，故平台区保持平面。
  椭圆 + 整体光滑额外走泊松方程（粗网格稀疏直接法求解 + 双线性插值），无屋脊线。
- ``chamfer``：基高 + 四分之一圆弧倒角过渡（内凹角/外凸角）+ 平台。

坐标约定（与 HeightField 一致）：``field[row, col]``，row 0 = minY，col 0 = minX，
世界坐标 = origin + index × spacing。与插件的 ``GridSpec``（index = r*W + c）一一对应。

纯算法、零 UI 依赖；scipy 懒加载（与 resample.py 一致）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

# ---- 参数默认值（对齐 Rhino 插件 ReliefDialog 的字段初值） ----
DEFAULT_BASE_HEIGHT = 3.0
DEFAULT_BASE_ANGLE = 80.0
DEFAULT_FREE_HEIGHT = 3.0
DEFAULT_BORDER_ANGLE = 45.0
DEFAULT_BULGE_STRENGTH = 1.0
DEFAULT_HEIGHT_CALIB = 0.5
# 自由鼓起峰值 = 内切半径 × tan(边界角) × 强度 × 本系数。
# 取 0.5 有几何依据：椭圆剖面 u = 2t - t²（t = d/dmax）在边界处斜率
# dh/dd = 2·H/dmax，令其等于 tan(边界角)·强度 即得 H = dmax·tan(边界角)·强度/2，
# 于是"边界角"就是穹顶边缘的真实坡度，与精雕的区域浮雕一致。
# （旧值 0.4 是拍脑袋校准，10mm 圆/边界20° 时比精雕低约 8%。）

# 截面类型 / 表面特点 / 拼合方式 / 高度模式 / 倒角模式
SHAPES = ("ellipse", "rectangle", "triangle", "circle", "spline", "chamfer")
SHAPE_LABELS = ("椭圆", "矩形", "三角", "圆形", "样条", "倒角")
FINISHES = ("smooth", "sharp")
BLENDS = ("add", "subtract", "replace")
BLEND_LABELS = ("叠加", "切除", "取代")
HEIGHT_MODES = ("free", "local")
CHAMFER_MODES = ("convex", "concave")
# 作用区域（按色号蒙版限定起浮雕范围）：颜色无效 / 颜色内 / 颜色外
COLOR_SCOPES = ("ignore", "inside", "outside")
COLOR_SCOPE_LABELS = ("颜色无效", "颜色内", "颜色外")

# 曲线采样点数（插件 SampleCurve 默认 512）
SAMPLE_COUNT = 512
# 塌缩共线阈值（插件硬编码，注释为"网格步长的 1%"）
COLLAPSE_TOL = 0.001
# 泊松求解（整体光滑椭圆）：粗网格最大边、CG 迭代上限（直接法失败时回退）。
# 256 足以表达“整体光滑”穹顶：解按峰值归一化后双线性插值回细网格，相比 512
# 的形状差 <1%（肉眼不可辨），而求解规模降到 1/4、耗时降一个数量级。
POISSON_COARSE_MAX = 256
POISSON_MAX_ITER = 2000

Grid = Tuple[float, float, float, int, int]   # (minX, minY, spacing, nx, ny)


@dataclass
class RegionReliefParams:
    """区域浮雕参数（字段与 Rhino 插件 Tab1 面板一一对应）。"""

    shape: str = "ellipse"
    base_height: float = DEFAULT_BASE_HEIGHT
    base_angle: float = DEFAULT_BASE_ANGLE
    height_mode: str = "free"          # free=自由高度 / local=局部等高
    free_height: float = DEFAULT_FREE_HEIGHT
    border_angle: float = DEFAULT_BORDER_ANGLE
    bulge_strength: float = DEFAULT_BULGE_STRENGTH
    height_calib: float = DEFAULT_HEIGHT_CALIB
    finish: str = "smooth"             # smooth=整体光滑 / sharp=棱角清晰
    blend: str = "add"                 # add=叠加 / subtract=切除 / replace=取代
    chamfer_mode: str = "convex"       # convex=外凸角 / concave=内凹角
    color_scope: str = "ignore"        # ignore=颜色无效 / inside=颜色内 / outside=颜色外
    sample_count: int = SAMPLE_COUNT


# ==================== 曲线采样 ====================

def sample_curve_points(obj, sample_count: int = SAMPLE_COUNT) -> np.ndarray:
    """取曲线对象的世界 XY 采样点，返回 (N, 2) float64。

    统一处理各类曲线：带 ``getSampledPoints`` 的直接取；圆/椭圆/弧等参数对象只有
    ``toPolycurve``，先转换再取；折线对象（``LineObject``，如用 Line 命令画的
    矩形/多边形）只有 ``points()``，直接取顶点。点数不足时按弧长加密到
    ``sample_count``，保证扫描线与距离场的精度（插件对每段至少采样 64 点）。
    闭合对象（``isClosed``）若采样首尾未重合，补首点使闭合边被正确采样。
    """
    pts = None
    getter = getattr(obj, "getSampledPoints", None)
    if callable(getter):
        try:
            pts = getter()
        except TypeError:
            pts = getter(sample_count)
    if not pts:
        conv = getattr(obj, "toPolycurve", None)
        poly = conv() if callable(conv) else None
        if poly is not None:
            g = getattr(poly, "getSampledPoints", None)
            pts = g() if callable(g) else None
    if not pts:
        # 折线对象（LineObject 矩形/多边形）：继承的 BaseObject.toPolycurve 返回
        # None、又无 getSampledPoints，直接取顶点 points()
        pg = getattr(obj, "points", None)
        if callable(pg):
            try:
                pts = pg()
            except TypeError:
                pts = None
    if not pts:
        return np.zeros((0, 2), dtype=np.float64)

    arr = np.asarray([[float(p.x), float(p.y)] for p in pts], dtype=np.float64)
    if arr.shape[0] < 2:
        return np.zeros((0, 2), dtype=np.float64)
    # 闭合折线（isClosed 但顶点首尾未重合，如 LineObject 矩形/多边形）：补首点，
    # 使 resample/距离场/扫描线正确包含最后一段闭合边（与 pathLength 补边一致）。
    closed = getattr(obj, "isClosed", None)
    if callable(closed):
        try:
            is_closed = bool(closed())
        except Exception:
            is_closed = False
        if is_closed and float(np.linalg.norm(arr[0] - arr[-1])) > 1e-9:
            arr = np.vstack([arr, arr[0:1]])
    if arr.shape[0] < sample_count:
        arr = resample_polyline(arr, sample_count)
    return arr


def resample_polyline(poly: np.ndarray, count: int) -> np.ndarray:
    """按弧长把折线重采样为 count+1 个点（闭合折线首尾重合）。"""
    if poly.shape[0] < 2 or count < 2:
        return poly
    seg = np.linalg.norm(np.diff(poly, axis=0), axis=1)
    total = float(seg.sum())
    if total <= 1e-12:
        return poly
    # 累计弧长参数 → 等距取点
    acc = np.concatenate(([0.0], np.cumsum(seg)))
    targets = np.linspace(0.0, total, count + 1)
    return np.column_stack((np.interp(targets, acc, poly[:, 0]),
                            np.interp(targets, acc, poly[:, 1])))


def collapse_collinear(poly: np.ndarray, tol: float = COLLAPSE_TOL) -> np.ndarray:
    """把密集折线塌缩为折角点（矩形 → 4 角点），保留真实折角、去除共线中间点。

    用"点到直线距离"判定共线（而非角度）：弧线相邻段角度变化很小，但点到前一段
    的距离会累积增大，能正确区分弧线与直线。
    """
    if poly.shape[0] < 3:
        return poly
    # 去重相邻重复点
    d = np.linalg.norm(np.diff(poly, axis=0), axis=1)
    keep_mask = np.concatenate(([True], d > 1e-9))
    pts = poly[keep_mask]
    # 去掉与首点重合的尾点（闭合折线）
    while pts.shape[0] >= 3 and abs(pts[0, 0] - pts[-1, 0]) < 1e-9 \
            and abs(pts[0, 1] - pts[-1, 1]) < 1e-9:
        pts = pts[:-1]
    m = pts.shape[0]
    if m < 3:
        return pts

    keep: List[Tuple[float, float]] = [(pts[0, 0], pts[0, 1]), (pts[1, 0], pts[1, 1])]
    for i in range(2, m):
        x0, y0 = keep[-2]
        x1, y1 = keep[-1]
        x2, y2 = float(pts[i, 0]), float(pts[i, 1])
        dx, dy = x1 - x0, y1 - y0
        len_sq = dx * dx + dy * dy
        if len_sq < 1e-18:
            keep.append((x2, y2))
            continue
        cross = abs(dx * (y2 - y0) - dy * (x2 - x0))
        if cross / math.sqrt(len_sq) > tol:
            keep.append((x2, y2))
    if len(keep) >= 3:
        return np.asarray(keep, dtype=np.float64)
    return pts


# ==================== 扫描线 / 内部掩膜 ====================

def compute_row_scan_fills(poly: np.ndarray, min_y: float, dy: float,
                           rows: int) -> List[np.ndarray]:
    """逐行求多边形与扫描线的交点 X（升序），用于奇偶规则填充。"""
    out: List[np.ndarray] = []
    if poly.shape[0] < 2:
        return [np.zeros(0) for _ in range(rows)]
    x1, y1 = poly[:, 0], poly[:, 1]
    x2 = np.roll(x1, -1)
    y2 = np.roll(y1, -1)
    denom = y2 - y1
    for r in range(rows):
        wy = min_y + r * dy
        cross = (y1 > wy) != (y2 > wy)
        if not cross.any():
            out.append(np.zeros(0))
            continue
        with np.errstate(divide="ignore", invalid="ignore"):
            xs = x1[cross] + (wy - y1[cross]) * (x2[cross] - x1[cross]) / denom[cross]
        out.append(np.sort(xs))
    return out


def build_inside_mask(poly: np.ndarray, grid: Grid) -> np.ndarray:
    """扫描线奇偶规则填充，返回 (ny, nx) bool 内部掩膜。"""
    min_x, min_y, spacing, nx, ny = grid
    inside = np.zeros((ny, nx), dtype=bool)
    fills = compute_row_scan_fills(poly, min_y, spacing, ny)
    for r, xs in enumerate(fills):
        cnt = xs.size - (xs.size % 2)     # 成对使用交叉点
        for p in range(0, cnt, 2):
            c0 = int(math.ceil((xs[p] - min_x) / spacing - 1e-9))
            c1 = int(math.floor((xs[p + 1] - min_x) / spacing + 1e-9))
            c0 = max(0, c0)
            c1 = min(nx - 1, c1)
            if c1 >= c0:
                inside[r, c0:c1 + 1] = True
    return inside


# ==================== 有符号距离场（精确欧氏距离变换） ====================

def _boundary_seed(inside: np.ndarray) -> np.ndarray:
    """内部且四邻有外部、或外部且四邻有内部的像素（距离场零种子）。"""
    b = np.zeros_like(inside)
    b[:, :-1] |= inside[:, :-1] != inside[:, 1:]
    b[:, 1:] |= inside[:, 1:] != inside[:, :-1]
    b[:-1, :] |= inside[:-1, :] != inside[1:, :]
    b[1:, :] |= inside[1:, :] != inside[:-1, :]
    return b


def compute_signed_distance(inside: np.ndarray, cell: float,
                            outside_zero: bool = False) -> np.ndarray:
    """到边界的有符号距离（世界单位）：内部为正、外部为负。

    outside_zero=True 时外部一律为 0（粗网格求解用，见 ComputeSignedDistanceForCoarseGrid）。
    """
    from scipy.ndimage import distance_transform_edt

    seed = _boundary_seed(inside)
    if not seed.any():
        return np.zeros(inside.shape, dtype=np.float64)
    # 精确欧氏距离变换（与插件的 Felzenszwalb 1D EDT 同为精确解，单位为格）
    dist = distance_transform_edt(~seed).astype(np.float64)
    if outside_zero:
        # 必须把外部真正清零：EDT 对外部像素同样给出正距离，若直接返回
        # dist*cell，调用方取 max() 得到的是"离区域边界最远的**外部**像素"
        # 距离（随模型图幅变大而增大），穹顶峰值会按模型尺寸放大——这正是
        # 小圆浮雕在 160mm 模型上算出 12.87mm（应为 1.7mm 量级）的根因。
        return np.where(inside, dist, 0.0) * float(cell)
    return np.where(inside, dist, -dist) * float(cell)


def compute_polygon_sd(poly: np.ndarray, grid: Grid,
                       inside: np.ndarray | None = None,
                       holes: Sequence[np.ndarray] | None = None) -> np.ndarray:
    """解析多边形有符号距离场（全域）：到折线边界的精确欧氏距离。

    与 :func:`compute_signed_distance`（像素掩码 + EDT）相比，本函数不做
    网格化——距离直接按像素中心到边的解析几何计算，符号用射线法判定，
    圆/椭圆/样条等光滑轮廓的距离等值线为亚像素精度，不会继承像素化
    阶梯（像素化掩码的 EDT 台阶被穹顶剖面放大后，高度曲线会提取出
    "奇怪的多边形"）。

    全域计算（内正外负、边界连续过零）：穹顶鼓起在真边界处平滑归零，
    贴根的小高度等值线不再贴着像素掩码走。inside 参数保留兼容但不再
    用于跳过外部像素（符号必须全域正确）。

    holes：区域内部的孔洞环（如字母 O 的内圈、回字形内圈）。距离取到
    **所有**环边界的最近值；符号用全部环合并后的奇偶规则（外环内、孔洞外
    为正），孔洞边界因此也是一条真实的起坡边界，不会形成垂直断崖。

    性能：边数 > 256 的密集采样轮廓先按弧长均匀重采样到 256——弦高
    误差随曲率自消（对剖面 s = sd/dmax 是系统性偏置），对圆/椭圆类
    光滑轮廓不引入可见误差；射线法用交点排序 + searchsorted。
    距离场光滑（Lipschitz 1），全域解析在 ~1/4 采样网格上计算后双线性
    上采样（凸轮廓内部插值误差 < 0.001mm），仅边界 2 像素带内用全
    分辨率精确值——复杂度 O((N/4)²×E + 周长像素×E)，大网格秒级。
    """
    min_x, min_y, spacing, nx, ny = grid
    if poly.shape[0] < 2:
        return np.zeros((ny, nx), dtype=np.float64)
    rings = [poly]
    for h in holes or ():
        if h is not None and h.shape[0] >= 2:
            rings.append(h)
    # 密集环先按弧长均匀降采样（弦高误差随曲率自消）
    rings = [resample_polyline(r, 256) if r.shape[0] > 256 else r for r in rings]

    # 逐环展开边（不能跨环 roll，否则会把一个环的末点连到下一个环的首点）
    ax = np.concatenate([r[:, 0] for r in rings])
    ay = np.concatenate([r[:, 1] for r in rings])
    bx = np.concatenate([np.roll(r[:, 0], -1) for r in rings])
    by = np.concatenate([np.roll(r[:, 1], -1) for r in rings])
    abx = bx - ax
    aby = by - ay
    len_sq = abx * abx + aby * aby
    inv_len_sq = np.where(len_sq > 1e-18, 1.0 / np.maximum(len_sq, 1e-18), 1e18)

    def _exact_dist(pxv: np.ndarray, pyv: np.ndarray) -> np.ndarray:
        """展平像素 (P,) 的精确到边距离（分块矩阵）。"""
        m = pxv.size
        best = np.empty(m, dtype=np.float64)
        CH = 16384
        for s0 in range(0, m, CH):
            e0 = min(s0 + CH, m)
            apx = pxv[s0:e0, None] - ax[None, :]
            apy = pyv[s0:e0, None] - ay[None, :]
            tt = (apx * abx[None, :] + apy * aby[None, :]) * inv_len_sq[None, :]
            np.clip(tt, 0.0, 1.0, out=tt)
            dx = apx - tt * abx[None, :]
            dy = apy - tt * aby[None, :]
            best[s0:e0] = np.sqrt(np.min(dx * dx + dy * dy, axis=1))
        return best

    def _sign_rows(sd_grid: np.ndarray, ys: np.ndarray,
                   xs: np.ndarray) -> np.ndarray:
        """射线法定符号（交点排序 + searchsorted）。ys/xs 为世界坐标轴。"""
        out = sd_grid.copy()
        for ri, wy in enumerate(ys):
            cross = (ay > wy) != (by > wy)
            if not cross.any():
                out[ri] = -out[ri]
                continue
            xint = ax[cross] + (wy - ay[cross]) * abx[cross] / aby[cross]
            xso = np.sort(xint)
            cnt = len(xso) - np.searchsorted(xso, xs, side="left")
            out[ri] = np.where((cnt % 2) == 1, out[ri], -out[ri])
        return out

    # ---- 粗网格全域解析 ----
    step = max(1, int(math.ceil(max(nx, ny) / 256.0)))
    sub_iy = np.arange(0, ny, step, dtype=np.int64)
    sub_ix = np.arange(0, nx, step, dtype=np.int64)
    gy = min_y + sub_iy * spacing
    gx = min_x + sub_ix * spacing
    PY, PX = np.meshgrid(gy, gx, indexing="ij")
    sd_sub = _exact_dist(PX.ravel(), PY.ravel()).reshape(len(gy), len(gx))
    sd_sub = _sign_rows(sd_sub, gy, gx)
    if step > 1:
        from scipy.ndimage import map_coordinates
        yy, xx = np.meshgrid(np.arange(ny) / step, np.arange(nx) / step,
                             indexing="ij")
        sd = map_coordinates(sd_sub, [yy, xx], order=1, mode="nearest")
    else:
        sd = sd_sub

    # ---- 边界 2 像素带：全分辨率精确覆盖 ----
    if step > 1:
        band_mask = inside if inside is not None else build_inside_mask(poly, grid)
        if band_mask.any():
            from scipy.ndimage import binary_dilation
            band = binary_dilation(band_mask, structure=np.ones((5, 5), dtype=bool))
            rr_, cc_ = np.nonzero(band)
            d_band = _exact_dist(min_x + cc_ * spacing, min_y + rr_ * spacing)
            # band 内像素的符号：逐行射线（只对出现的行）
            for ri in np.unique(rr_):
                wy = min_y + ri * spacing
                cross = (ay > wy) != (by > wy)
                sel = rr_ == ri
                if not cross.any():
                    d_band[sel] = -d_band[sel]
                    continue
                xint = ax[cross] + (wy - ay[cross]) * abx[cross] / aby[cross]
                xso = np.sort(xint)
                pxs = min_x + cc_[sel] * spacing
                cnt = len(xso) - np.searchsorted(xso, pxs, side="left")
                d_band[sel] = np.where((cnt % 2) == 1, d_band[sel], -d_band[sel])
            sd[band] = d_band
    return sd


# ==================== 基高（所有截面的公共基础） ====================

def _seg_dist_sq(px: np.ndarray, py: float, ax: np.ndarray, ay: np.ndarray,
                 abx: np.ndarray, aby: np.ndarray,
                 inv_len_sq: np.ndarray, chunk: int = 512) -> np.ndarray:
    """点集 (px, py) 到各边的最小平方距离，返回 (M,)。分块计算以控制内存。"""
    m = px.size
    best = np.full(m, np.inf, dtype=np.float64)
    for s in range(0, m, chunk):
        e = min(s + chunk, m)
        apx = (px[s:e, None] - ax[None, :])
        apy = py - ay[None, :]
        t = (apx * abx[None, :] + apy * aby[None, :]) * inv_len_sq[None, :]
        np.clip(t, 0.0, 1.0, out=t)
        cx = ax[None, :] + t * abx[None, :]
        cy = ay[None, :] + t * aby[None, :]
        dx = px[s:e, None] - cx
        dy = py - cy
        best[s:e] = np.min(dx * dx + dy * dy, axis=1)
    return best


def compute_base_height_field(poly: np.ndarray, grid: Grid,
                              base_height: float, base_angle_deg: float,
                              holes: Sequence[np.ndarray] | None = None) -> np.ndarray:
    """基高高度场：高度 = min(d × tan(基角), 基高)，d 为到折线边界的最近欧氏距离。

    只计算多边形内部的顶点（扫描线确定内部掩膜），外部保持 0。
    斜面平整、屋脊锐利，等价于 Rhino 的 ExtrudeCrvTapered。
    holes 为区域内部孔洞环：孔洞内部同样保持 0，且孔洞边界与外墙一样
    向外起坡（逐边作用带对全部环生效），不会在岛屿边缘出现垂直断崖。

    实现：内部顶点先置为基高，再逐边在其作用带（边 bbox ±max_dist）内取
    min(min(d×tan, 基高))；超出所有作用带（最近边 > max_dist）的格子保持基高，
    外部格子为 0（高度非负，取 min 后仍为 0）。与原“逐行×区间×边（bbox 粗筛）”
    实现逐位等价：被原粗筛排除的边都 ≥max_dist、贡献被截断为基高，故纳入与否
    无影响；每条边用与原实现相同的表达式，最终高度逐位一致。
    """
    min_x, min_y, spacing, nx, ny = grid
    field = np.zeros((ny, nx), dtype=np.float64)
    if poly.shape[0] < 2 or base_height <= 1e-12:
        return field

    tan_base = math.tan(math.radians(max(1e-6, base_angle_deg)))
    if tan_base < 1e-6:
        tan_base = 1e-6
    max_dist = base_height / tan_base
    max_dist_sq = max_dist * max_dist

    rings = [poly]
    for h in holes or ():
        if h is not None and h.shape[0] >= 2:
            rings.append(h)
    # 逐环展开边（不能跨环 roll，否则会把一个环的末点连到下一个环的首点）
    ax = np.concatenate([r[:, 0] for r in rings])
    ay = np.concatenate([r[:, 1] for r in rings])
    bx = np.concatenate([np.roll(r[:, 0], -1) for r in rings])
    by = np.concatenate([np.roll(r[:, 1], -1) for r in rings])
    abx = bx - ax
    aby = by - ay
    len_sq = abx * abx + aby * aby
    inv_len_sq = np.where(len_sq > 1e-18, 1.0 / np.maximum(len_sq, 1e-18), 1e18)

    inside = build_inside_mask(poly, grid)
    for h in (holes or ()):
        if h is not None and h.shape[0] >= 2:
            inside &= ~build_inside_mask(h, grid)
    if not inside.any():
        return field
    field[inside] = base_height

    xs_axis = min_x + np.arange(nx, dtype=np.float64) * spacing
    ys_axis = min_y + np.arange(ny, dtype=np.float64) * spacing
    axl = ax.tolist(); ayl = ay.tolist()
    bxl = bx.tolist(); byl = by.tolist()
    abxl = abx.tolist(); abyl = aby.tolist()
    invl = inv_len_sq.tolist()
    for i in range(len(axl)):
        sx = axl[i]; sy = ayl[i]
        ex = bxl[i]; ey = byl[i]
        c0 = max(0, int(math.floor((min(sx, ex) - max_dist - min_x) / spacing)))
        c1 = min(nx - 1, int(math.ceil((max(sx, ex) + max_dist - min_x) / spacing)))
        r0 = max(0, int(math.floor((min(sy, ey) - max_dist - min_y) / spacing)))
        r1 = min(ny - 1, int(math.ceil((max(sy, ey) + max_dist - min_y) / spacing)))
        if c0 > c1 or r0 > r1:
            continue
        px = xs_axis[c0:c1 + 1]
        abxi = abxl[i]; abyi = abyl[i]; invi = invl[i]
        # 按行分块计算，控制临时数组内存（长边的大窗口）
        for rs in range(r0, r1 + 1, 512):
            re = min(rs + 512 - 1, r1)
            wy = ys_axis[rs:re + 1]
            apx = px[None, :] - sx
            apy = wy[:, None] - sy
            t = np.clip((apx * abxi + apy * abyi) * invi, 0.0, 1.0)
            dx = px[None, :] - (sx + t * abxi)
            dy = wy[:, None] - (sy + t * abyi)
            d2 = dx * dx + dy * dy
            h = np.where(d2 >= max_dist_sq, base_height, np.sqrt(d2) * tan_base)
            win = field[rs:re + 1, c0:c1 + 1]
            np.minimum(win, h, out=win)
    return field


# ==================== 掩膜平滑（盒式模糊） ====================

def _box_blur_axis(arr: np.ndarray, radius: int, axis: int) -> np.ndarray:
    """沿指定轴的可分离盒式模糊（前缀和窗口），与插件 BoxBlur 等价。"""
    n = arr.shape[axis]
    zeros_shape = arr.shape[:axis] + (1,) + arr.shape[axis + 1:]
    cs = np.concatenate([np.zeros(zeros_shape), np.cumsum(arr, axis=axis)], axis=axis)
    idx = np.arange(n)
    lo = np.clip(idx - radius, 0, n - 1)
    hi = np.clip(idx + radius, 0, n - 1)
    s = np.take(cs, hi + 1, axis=axis) - np.take(cs, lo, axis=axis)
    cnt = (hi - lo + 1).astype(np.float64)
    shape = [1] * arr.ndim
    shape[axis] = n
    return s / cnt.reshape(shape)


def smooth_height_field(field: np.ndarray, written: np.ndarray,
                        radius_cells: int, passes: int) -> np.ndarray:
    """掩膜盒式模糊：仅已写入（written）的顶点参与，未写入的一律保持 0。

    因此绝不把高度扩散到轮廓之外；屋脊线两侧均已写入，被平滑后变圆润。
    """
    if radius_cells < 1 or passes < 1:
        return field
    z = field.astype(np.float64, copy=True)
    for _ in range(passes):
        tmp = _box_blur_axis(z, radius_cells, axis=1)     # 行方向
        z2 = _box_blur_axis(tmp, radius_cells, axis=0)    # 列方向
        z2[~written] = 0.0
        z = z2
    return z


# ==================== 自由鼓起：穹顶（椭圆/样条/三角/圆） ====================

def _dome_profile(profile: str, t: np.ndarray) -> np.ndarray:
    """穹顶剖面形状（脚 t=0、中轴 t=1）。"""
    if profile == "triangle":
        return t
    if profile == "circle":
        return np.sqrt(np.maximum(0.0, 2.0 * t - t * t))
    if profile == "spline":
        u = 2.0 * t - t * t
        return u * np.sqrt(u)
    u = 2.0 * t - t * t        # ellipse：抛物线穹顶
    return u


def _smooth_sd_for_triangle(sd: np.ndarray, inside: np.ndarray) -> np.ndarray:
    """三角形截面：EDT 的网格量化台阶被线性剖面放大成条纹，对距离场做 3×3 均值。"""
    from scipy.ndimage import convolve

    kern = np.ones((3, 3), dtype=np.float64)
    s = convolve(sd * inside, kern, mode="constant", cval=0.0)
    c = convolve(inside.astype(np.float64), kern, mode="constant", cval=0.0)
    out = sd.copy()
    np.divide(s, c, out=out, where=(c > 0) & inside)
    return out


def compute_dome_free_bulge(inside: np.ndarray, sd: np.ndarray,
                            params: RegionReliefParams,
                            base_height: float, base_angle_deg: float) -> np.ndarray:
    """穹顶自由鼓起（棱角清晰 / 样条 / 三角 / 圆）。

    基于 SDF：s = d/dmax（边界 0、中轴 1）；穹顶的脚落在基高平台边缘 sPlatform 上，
    平台区不叠加鼓起（保持平面）。
    """
    field = np.zeros(inside.shape, dtype=np.float64)
    # 解析 SDF 无网格量化台阶，三角形不再需要 EDT 平滑补救
    work_sd = sd

    dmax = float(work_sd.max()) if work_sd.size else 0.0
    if dmax < 1e-3:
        return field

    bulge_peak = (dmax * math.tan(math.radians(params.border_angle))
                  * params.bulge_strength * params.height_calib)
    if bulge_peak <= 1e-9:
        return field

    tan_base = max(1e-6, math.tan(math.radians(max(1e-6, base_angle_deg))))
    base_dist = base_height / tan_base                     # 基高斜面半宽
    s_platform = (base_dist / dmax) if base_height > 1e-9 else 0.0
    s_platform = max(0.0, min(s_platform, 0.9))
    span = max(1e-6, 1.0 - s_platform)

    s = np.clip(work_sd / dmax, 0.0, 1.0)
    t = np.clip((s - s_platform) / span, 0.0, 1.0)
    shape = _dome_profile(params.shape, t)
    free = bulge_peak * shape
    # 截断改用解析 sd 符号（像素掩码边界有 1 格悬崖，会把小高度
    # 等值线变成像素阶梯多边形）
    free[(s <= s_platform) | (work_sd <= 0.0)] = 0.0
    return free


def compute_smooth_dome_free_bulge(inside: np.ndarray, grid: Grid,
                                   params: RegionReliefParams,
                                   base_height: float,
                                   base_angle_deg: float) -> np.ndarray:
    """整体光滑穹顶（仅椭圆）：泊松方程"吹气"算法。

    求解 ∇²u = -C（常数源项）、边界 u=0，解天然光滑、无屋脊线，像吹气一样鼓起。
    在粗网格（最大边 256）上用稀疏直接法求解后双线性插值回细网格。
    """
    min_x, min_y, spacing, nx, ny = grid
    field = np.zeros((ny, nx), dtype=np.float64)

    c_w = min(nx, POISSON_COARSE_MAX)
    c_h = min(ny, POISSON_COARSE_MAX)
    if c_w < 3 or c_h < 3:
        return field
    cdx = (spacing * (nx - 1)) / (c_w - 1)
    cdy = (spacing * (ny - 1)) / (c_h - 1)

    # 细网格 inside 下采样到粗网格（最近邻，与插件一致）
    fr = np.clip((np.arange(c_h) * (ny - 1) / (c_h - 1) + 0.5).astype(int), 0, ny - 1)
    fc = np.clip((np.arange(c_w) * (nx - 1) / (c_w - 1) + 0.5).astype(int), 0, nx - 1)
    coarse_inside = inside[np.ix_(fr, fc)]
    if not coarse_inside.any():
        return field

    coarse_sd = compute_signed_distance(coarse_inside, max(cdx, cdy), outside_zero=True)
    coarse_dmax = float(coarse_sd.max())
    if coarse_dmax < 1e-3:
        return field
    # 峰值必须按**细网格真实内切半径**算：粗网格（最大边 256）对 10mm 小圆在
    # 大模型上只有几个格子，量化会把半径压小，高度随模型尺寸漂移。
    # 粗网格只负责解的形状（解随后按峰值归一化），不影响高度。
    fine_sd = compute_signed_distance(inside, spacing)
    fine_dmax = float(fine_sd.max())
    if fine_dmax < 1e-3:
        return field

    target_peak = (fine_dmax * math.tan(math.radians(params.border_angle))
                   * params.bulge_strength * params.height_calib)
    if target_peak <= 1e-9:
        return field

    # 泊松源项：使峰值解归一化后恰为 target_peak
    c_const = 8.0 * target_peak / (coarse_dmax * coarse_dmax)
    coeff_x = 1.0 / (cdx * cdx)
    coeff_y = 1.0 / (cdy * cdy)
    center_coeff = 2.0 * (coeff_x + coeff_y)

    u = _solve_poisson(coarse_inside, c_const, coeff_x, coeff_y, center_coeff)

    peak = float(u.max())
    if peak > 1e-9:
        u *= target_peak / peak

    # 双线性插值回细网格（细列 c → 粗列 c * spacing / cdx）
    gx = np.arange(nx) * spacing / cdx
    gy = np.arange(ny) * spacing / cdy
    x0 = np.floor(gx).astype(int)
    y0 = np.floor(gy).astype(int)
    x1 = np.minimum(x0 + 1, c_w - 1)
    y1 = np.minimum(y0 + 1, c_h - 1)
    fx = (gx - x0)[None, :]
    fy = (gy - y0)[:, None]
    v00 = u[np.ix_(y0, x0)]
    v10 = u[np.ix_(y0, x1)]
    v01 = u[np.ix_(y1, x0)]
    v11 = u[np.ix_(y1, x1)]
    interp = ((1 - fx) * (1 - fy) * v00 + fx * (1 - fy) * v10 +
              (1 - fx) * fy * v01 + fx * fy * v11)
    field[inside] = interp[inside]

    # 基高平台区不叠加鼓起（与棱角清晰路径保持同一约定）
    if base_height > 1e-9:
        tan_base = max(1e-6, math.tan(math.radians(max(1e-6, base_angle_deg))))
        s_platform = max(0.0, min((base_height / tan_base) / fine_dmax, 0.9))
        field[(fine_sd / fine_dmax) <= s_platform] = 0.0
    return field


def _solve_poisson(inside: np.ndarray, c_const: float, coeff_x: float,
                   coeff_y: float, center_coeff: float,
                   rtol: float = 1e-5) -> np.ndarray:
    """求解离散泊松方程 ∇²u = -C（内部点），Dirichlet 边界 u=0。

    离散化：centerCoeff·u_i - coeff_x·(u_左+u_右) - coeff_y·(u_下+u_上) = C，
    区域外邻居按 u=0（Dirichlet），等价于从矩阵中直接省略这些项。

    性能：一次性向量化装配五点模板稀疏矩阵（CSR），用直接法 spsolve 求解；
    若解含非有限值或求解异常则回退共轭梯度（CG）。相比旧实现（LinearOperator
    + 每轮布尔索引 gather/scatter 的 Python matvec + rtol=1e-9 迭代数百次），
    快一个数量级；解随后按峰值归一化，故 rtol=1e-5 的残差对形状无可见影响。
    """
    from scipy.sparse import coo_matrix
    from scipy.sparse.linalg import cg, spsolve

    h, w = inside.shape
    u_full = np.zeros((h, w), dtype=np.float64)
    if h < 3 or w < 3:
        return u_full

    # 与插件一致：网格最外一圈不参与求解，恒为 0（Dirichlet 边界）
    solve = inside.copy()
    solve[0, :] = False
    solve[-1, :] = False
    solve[:, 0] = False
    solve[:, -1] = False
    n = int(solve.sum())
    if n == 0:
        return u_full

    # 稀疏装配：solve 节点编号 0..n-1，区域外邻居（idx=-1）自动省略
    idx = np.full((h, w), -1, dtype=np.int64)
    idx[solve] = np.arange(n)
    sr, sc = np.nonzero(solve)
    si = idx[sr, sc]
    rows = [si]
    cols = [si]
    vals = [np.full(n, center_coeff, dtype=np.float64)]
    for dr, dc, coef in ((0, -1, coeff_x), (0, 1, coeff_x),
                         (-1, 0, coeff_y), (1, 0, coeff_y)):
        nr = sr + dr
        nc = sc + dc
        m = (nr >= 0) & (nr < h) & (nc >= 0) & (nc < w)
        if not m.any():
            continue
        nj = idx[nr[m], nc[m]]
        k = nj >= 0
        if not k.any():
            continue
        rows.append(si[m][k])
        cols.append(nj[k])
        vals.append(np.full(int(k.sum()), -coef, dtype=np.float64))
    A = coo_matrix((np.concatenate(vals),
                    (np.concatenate(rows), np.concatenate(cols))),
                   shape=(n, n)).tocsr()

    rhs = np.full(n, float(c_const), dtype=np.float64)
    try:
        x = spsolve(A.tocsc(), rhs)
        if not np.all(np.isfinite(x)):
            raise ValueError("泊松直接法解含非有限值，回退 CG")
    except Exception:
        x, _info = cg(A, rhs, rtol=rtol, maxiter=POISSON_MAX_ITER)
    u_full[solve] = x
    return u_full


# ==================== 自由鼓起：倒角 ====================

def compute_chamfer_height_field(inside: np.ndarray, sd: np.ndarray,
                                 local_level: float, convex: bool) -> np.ndarray:
    """倒角截面：R = 高度，d∈[0,R] 为四分之一圆弧过渡，d>R 为平台。

    convex（外凸角）  h = sqrt(R² - (R-d)²)
    concave（内凹角） h = R - sqrt(R² - d²)
    """
    field = np.zeros(inside.shape, dtype=np.float64)
    r = float(local_level)
    if r <= 1e-9:
        return field
    d = np.clip(sd, 0.0, r)
    if convex:
        diff = r - d
        arc = np.sqrt(np.maximum(0.0, r * r - diff * diff))
    else:
        arc = r - np.sqrt(np.maximum(0.0, r * r - d * d))
    field[inside] = np.where(sd[inside] <= r, arc[inside], r)
    return field


# ==================== 主入口 ====================

def compute_region_relief_field(poly: np.ndarray, grid: Grid,
                                params: RegionReliefParams,
                                holes: Sequence[np.ndarray] | None = None) -> np.ndarray:
    """按截面类型算出总高度场 (ny, nx)：total = 基高 + 自由鼓起。

    poly 为曲线密集采样点（世界坐标，闭合）；holes 为内部孔洞环。
    """
    min_x, min_y, spacing, nx, ny = grid
    field = np.zeros((ny, nx), dtype=np.float64)
    if poly.shape[0] < 3:
        return field

    hole_polys = [h for h in (holes or ())
                  if h is not None and h.shape[0] >= 3]
    inside = build_inside_mask(poly, grid)
    for h in hole_polys:
        inside &= ~build_inside_mask(h, grid)
    if not inside.any():
        return field

    collapsed = collapse_collinear(poly)
    collapsed_holes = [collapse_collinear(h) for h in hole_polys]
    base = compute_base_height_field(collapsed, grid,
                                     params.base_height, params.base_angle,
                                     holes=collapsed_holes)

    if params.shape == "rectangle":
        # 矩形 = 只有基高，无自由鼓起
        return base

    if params.shape == "chamfer":
        sd = compute_signed_distance(inside, spacing)
        free = compute_chamfer_height_field(inside, sd, params.free_height,
                                            params.chamfer_mode == "convex")
        # 圆弧含 sqrt，EDT 量化台阶会放大为条纹 → 对自由鼓起做轻量平滑
        free = smooth_height_field(free, free > 1e-9, 1, 1)
        return base + free

    # 穹顶类：椭圆 / 样条 / 三角 / 圆
    if params.finish == "smooth" and params.shape == "ellipse":
        free = compute_smooth_dome_free_bulge(inside, grid, params,
                                              params.base_height, params.base_angle)
    else:
        # 穹顶用解析多边形 SDF：像素化掩码的 EDT 台阶会让高度曲线
        # 提取出"奇怪的多边形"（尤其小高度等值线贴着掩码边界走）
        sd = compute_polygon_sd(collapsed, grid, inside, collapsed_holes)
        free = compute_dome_free_bulge(inside, sd, params,
                                       params.base_height, params.base_angle)

    # 局部等高：只截自由鼓起部分（基高独立，不计入封顶）
    if params.height_mode == "local" and params.free_height > 1e-9:
        free = np.where(inside & (free > params.free_height), params.free_height, free)

    total = base + free
    # 三角形：线性剖面放大 EDT 台阶为可见条纹 → 对总高度场做平滑
    if params.shape == "triangle":
        total = smooth_height_field(total, total > 1e-9, 1, 1)
    return total


def apply_region_relief(hf, poly: np.ndarray,
                        params: RegionReliefParams,
                        color_mask: np.ndarray | None = None,
                        color_value: int = 0,
                        holes: Sequence[np.ndarray] | None = None) -> int:
    """把区域浮雕应用到高度场，返回被修改的顶点数。

    hf: HeightField（就地修改 data）。poly: 曲线采样点（世界坐标）。
    holes: 区域内部孔洞环（可选，如一条闭合轮廓里包含的文字内圈）。
    拼合方式：add 叠加 / subtract 切除 / replace 取代。

    作用区域（params.color_scope，按色号蒙版裁剪，相对当前颜色 color_value）：
      - ignore  颜色无效：整块曲线区域都起浮雕（默认，向后兼容）；
      - inside  颜色内：只在 ``color_mask == color_value`` 的格子起浮雕；
      - outside 颜色外：只在 ``color_mask != color_value`` 的格子起浮雕。
    color_mask 为 None 视作全 0（未填色）：inside → 全不起、outside → 全起。
    color_mask 必须与 hf.data 同形状（同栅格）。
    """
    grid = (hf.origin[0], hf.origin[1], hf.spacing, hf.nx, hf.ny)
    total = compute_region_relief_field(poly, grid, params, holes=holes)
    mask = total > 1e-6
    # 作用区域裁剪：按当前颜色把起浮雕范围限定在色号蒙版的内/外
    scope = getattr(params, "color_scope", "ignore")
    if scope in ("inside", "outside"):
        if color_mask is not None and color_mask.shape == mask.shape:
            inside_cur = (color_mask == int(color_value))
        else:
            inside_cur = np.zeros(mask.shape, dtype=bool)   # 未填色 = 无当前颜色区
        mask = (mask & inside_cur) if scope == "inside" else (mask & ~inside_cur)
    n = int(mask.sum())
    if n == 0:
        return 0
    data = hf.data
    if params.blend == "add":
        data[mask] += total[mask]
    elif params.blend == "subtract":
        data[mask] -= total[mask]
    else:      # replace：区域内高度直接设为浮雕高度
        data[mask] = total[mask]
    return n

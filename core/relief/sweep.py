"""导动堆料（曲面导动）—— 复刻自 Rhino 插件 ReliefAlgorithms.ExecuteSurfaceSweep。

笔刷沿导动曲线扫掠，在高度场上堆料/雕刻。与插件的差异在**作用范围**：
Rhino 没有颜色模块，插件用独立的"区域范围曲线"（全部/曲线内/曲线外）限定
作用范围；本应用有色号蒙版（浮雕模式空格填色），改为按当前颜色裁剪
（颜色无效/颜色内/颜色外，与区域浮雕同一约定）。

笔刷截面（沿曲线的法向剖面）：
- 高斯分布（gauss）：高斯核 × Hann 平滑窗，无硬边台阶；
    sigma = (半衰直径×w / 2) / sqrt(2·ln2)，cutoff = 直径×w / 2，
    prof = 高度×w · exp(-d²/(2σ²)) · hann(d/cutoff)；
- 锥体（cone）：直径/半衰直径(小直径)/高度 定义的立式锥台
  （dH=D 圆柱、dH=0 圆锥）；
- 半球（hemisphere）：直径 D、高度 H 的半椭球印章，z = H·w·sqrt(1-(d/R)²)。

笔刷粗细沿程变化（WidthScale，t 为沿曲线弧长比例 0→1，m = 细端%）：
- 等粗 uniform：w=1；中粗 middle：两端 m、中间 1；
- 首粗 head：起点 1 → 终点 m；尾粗 tail：起点 m → 终点 1。
直径、半衰直径、高度整体随 w 缩放（中心始终在曲线上）。

效果（merge，本应用重新定义，二选一）：
- 累加 accumulate：z += 增量，重复点击不断叠加；
- 流动叠加 flow：z = max(z, 增量层)——重复点击同参数结果不变（层包络）。

快捷键（命令内实时调节，面板同步）：A/S 直径、D/F 半衰直径（±10%），
Q/W 高度（±0.5mm）；鼠标跟随笔刷圈（直径大小）。

坐标约定与 HeightField 一致：``data[row, col]``，行 0 = minY、列 0 = minX。
纯算法、零 UI 依赖、纯 numpy。
"""
from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np

# 参数默认值（对齐插件曲面导动页输入框初值）
DEFAULT_SWEEP_DIAMETER = 3.0
DEFAULT_SWEEP_HALF_LIFE = 1.5
DEFAULT_SWEEP_HEIGHT = 3.0
DEFAULT_SWEEP_THIN_PERCENT = 20.0

BRUSH_SHAPES = ("gauss", "cone", "hemisphere")
BRUSH_SHAPE_LABELS = ("高斯分布", "锥体", "半球")
BRUSH_TYPES = ("uniform", "middle", "head", "tail")
BRUSH_TYPE_LABELS = ("等粗", "中粗", "首粗", "尾粗")
# 效果（组合方式）：累加（每次点击叠加）/ 流动叠加（与当前面取包络，重复点击不变）
MERGES = ("accumulate", "flow")
MERGE_LABELS = ("累加", "流动叠加")
# 叠加方式（方向）：堆料（沿曲线升高）/ 取料（沿曲线雕刻去除）
APPLY_MODES = ("add", "remove")
APPLY_LABELS = ("堆料", "取料")
# 作用范围（相对底部调色板当前颜色，与区域浮雕一致）
COLOR_SCOPES = ("ignore", "inside", "outside")
COLOR_SCOPE_LABELS = ("颜色无效", "颜色内", "颜色外")

_SAMPLE_COUNT = 2048        # 导动线采样密度（决定 t 归一化与距离精度）

# 快捷键步长：直径/半衰直径 ±10% 缩放，高度 ±0.5mm
PARAM_SCALE = 1.1
HEIGHT_STEP = 0.5
PARAM_MIN = 0.1


@dataclass
class SweepParams:
    """导动堆料参数（字段与插件笔刷分组一一对应）。"""

    brush_shape: str = "gauss"        # gauss=高斯分布 / cone=锥体 / hemisphere=半球
    diameter: float = DEFAULT_SWEEP_DIAMETER          # 直径（粗端）
    half_life_diameter: float = DEFAULT_SWEEP_HALF_LIFE  # 半衰直径（锥体=小直径）
    height: float = DEFAULT_SWEEP_HEIGHT              # 高度
    brush_type: str = "uniform"       # uniform/middle/head/tail
    thin_percent: float = DEFAULT_SWEEP_THIN_PERCENT  # 细端百分比 1..100
    merge: str = "accumulate"         # accumulate=累加 / flow=流动叠加
    apply: str = "add"                # add=堆料 / remove=取料
    color_scope: str = "ignore"       # ignore/inside/outside（相对当前颜色）


def _width_scale(t: np.ndarray, brush_type: str, thin_frac: float) -> np.ndarray:
    """笔刷粗细沿程调制 w(t)∈[m,1]（t=沿曲线弧长比例，m=细端比例）。"""
    t = np.clip(t, 0.0, 1.0)
    m = max(0.0, min(1.0, thin_frac))
    wide = 1.0 - m
    if brush_type == "middle":
        return m + wide * (1.0 - np.abs(2.0 * t - 1.0))
    if brush_type == "head":
        return 1.0 - wide * t
    if brush_type == "tail":
        return m + wide * t
    return np.ones_like(t)     # uniform 等粗


def sample_sweep_path(obj, sample_count: int = _SAMPLE_COUNT) -> np.ndarray:
    """导动线密集采样（开放/闭合均可）；闭合线去掉重复尾点。"""
    from core.relief.region_relief import sample_curve_points

    pts = sample_curve_points(obj, sample_count)
    if pts.shape[0] >= 2 and float(np.linalg.norm(pts[0] - pts[-1])) < 1e-9:
        pts = pts[:-1]        # 首尾重合：去掉重复尾点
    return pts


def compute_sweep_delta(hf, poly: np.ndarray, params: SweepParams,
                        color_mask: np.ndarray | None = None,
                        color_value: int = 0,
                        closed: bool | None = None) -> np.ndarray:
    """计算沿导动线 polyyne 的一次导动增量场 (ny, nx)（未写入高度场）。

    d = 格点到导动线的最近距离；t = 最近点处沿线的弧长比例；
    截面 = 笔刷形状(d, D·w, dH·w, H·w)；作用范围按当前颜色裁剪。
    closed 显式传入"原曲线是否闭合"：sample_sweep_path 已去掉闭合线的
    重复尾点，事后用首尾重合判定恒为 False，闭合段会被当开放线丢弃
    （闭合曲线留约 1/采样数 周长的缺口）。
    """
    ny, nx = hf.ny, hf.nx
    delta = np.zeros((ny, nx), dtype=np.float64)
    if poly.shape[0] < 2:
        return delta

    D = max(float(params.diameter), 1e-6)
    dH = min(max(float(params.half_life_diameter), 1e-6), D)
    H = abs(float(params.height))
    if H < 1e-9:
        return delta
    thin = max(0.0, min(1.0, float(params.thin_percent) / 100.0))
    ln2 = math.log(2.0)

    ax, ay = poly[:, 0], poly[:, 1]
    bx, by = np.roll(ax, -1), np.roll(ay, -1)
    abx, aby = bx - ax, by - ay
    if closed is None:
        closed = bool(np.isclose(poly[0, 0], poly[-1, 0]) and
                      np.isclose(poly[0, 1], poly[-1, 1]))
    closed = bool(closed)
    seglen = np.hypot(abx, aby)
    if not closed:
        seglen = seglen[:-1]          # 开放线最后一段不闭环
        bx, by, abx, aby = bx[:-1], by[:-1], abx[:-1], aby[:-1]
        ax, ay = ax[:-1], ay[:-1]
    valid = seglen > 1e-12
    if not valid.any():
        return delta
    ax, ay, bx, by = ax[valid], ay[valid], bx[valid], by[valid]
    abx, aby, seglen = abx[valid], aby[valid], seglen[valid]
    inv_len2 = 1.0 / np.maximum(seglen * seglen, 1e-18)
    cum = np.concatenate(([0.0], np.cumsum(seglen)))[:-1]   # 各段起点弧长
    total = float(seglen.sum())
    if total < 1e-9:
        return delta

    ox, oy = hf.origin
    sp = hf.spacing
    xs = ox + np.arange(nx, dtype=np.float64) * sp
    ys = oy + np.arange(ny, dtype=np.float64) * sp

    # 覆盖范围：由全部段端点（起点 ax/ay 与终点 bx/by）± 直径余量得到，逐段
    # footprint 再裁剪到该范围内。含终点可避免开放导动线末段末端漏盖章——旧
    # 实现仅用起点 ax/ay，漏掉了只存在于终点里的最后一个端点，导致末段末端
    # 附近一小片区域 delta 恒 0（已修正）。
    margin = D
    seg_xmin = float(min(ax.min(), bx.min()))
    seg_xmax = float(max(ax.max(), bx.max()))
    seg_ymin = float(min(ay.min(), by.min()))
    seg_ymax = float(max(ay.max(), by.max()))
    row_lo = max(0, int(np.floor((seg_ymin - margin - oy) / sp)))
    row_hi = min(ny - 1, int(np.ceil((seg_ymax + margin - oy) / sp)))
    col_lo = max(0, int(np.floor((seg_xmin - margin - ox) / sp)))
    col_hi = min(nx - 1, int(np.ceil((seg_xmax + margin - ox) / sp)))

    # 逐段盖章：每段只在其作用可达范围（沿段两端各 ±cut）内计算最近距离，
    # 取代“逐行 × 全列 × 附近段”的暴力扫描（后者对横跨整幅网格的导动线要
    # 逐行扫全宽，是长导动线在大网格上的主瓶颈）。cut 取最大可能半径 D/2
    # （w≤1，故任意沿程粗细的 cutoff 都不超过它）；段范围外的格子 d≥cut，
    # 截面恒为 0，不影响结果。逐段按升序 + 严格 '<' 更新，与旧实现“块内
    # argmin、跨块严格比较”的并列取最小段号语义完全一致。
    cut = D * 0.5
    best_d2 = np.full((ny, nx), np.inf)
    best_t = np.zeros((ny, nx))
    axl = ax.tolist(); ayl = ay.tolist(); bxl = bx.tolist(); byl = by.tolist()
    abxl = abx.tolist(); abyl = aby.tolist()
    sinvl = inv_len2.tolist(); slenl = seglen.tolist(); cuml = cum.tolist()
    for i in range(len(axl)):
        sxi = axl[i]; syi = ayl[i]
        exi = bxl[i]; eyi = byl[i]
        c0 = max(col_lo, int(math.floor((min(sxi, exi) - cut - ox) / sp)))
        c1 = min(col_hi, int(math.ceil((max(sxi, exi) + cut - ox) / sp)))
        r0 = max(row_lo, int(math.floor((min(syi, eyi) - cut - oy) / sp)))
        r1 = min(row_hi, int(math.ceil((max(syi, eyi) + cut - oy) / sp)))
        if c0 > c1 or r0 > r1:
            continue
        px = xs[c0:c1 + 1]
        wy = ys[r0:r1 + 1]
        abxi = abxl[i]; abyi = abyl[i]
        apx = px[None, :] - sxi
        apy = wy[:, None] - syi
        t = np.clip((apx * abxi + apy * abyi) * sinvl[i], 0.0, 1.0)
        dx = px[None, :] - (sxi + t * abxi)
        dy = wy[:, None] - (syi + t * abyi)
        d2 = dx * dx + dy * dy
        tnorm = (cuml[i] + t * slenl[i]) / total
        blk_d2 = best_d2[r0:r1 + 1, c0:c1 + 1]
        blk_t = best_t[r0:r1 + 1, c0:c1 + 1]
        improved = d2 < blk_d2
        best_d2[r0:r1 + 1, c0:c1 + 1] = np.where(improved, d2, blk_d2)
        best_t[r0:r1 + 1, c0:c1 + 1] = np.where(improved, tnorm, blk_t)

    # 有最近距离的格子统一算截面
    near = np.isfinite(best_d2)
    if not near.any():
        return delta
    d = np.sqrt(best_d2)
    w = _width_scale(best_t, params.brush_type, thin)
    d_local = D * w
    dh_local = dH * w
    h_local = H * w

    if params.brush_shape == "cone":
        # 锥台：底半径 R0=dLocal/2 处 0，顶半径 R1=dhLocal/2 处 h；R1>=R0 圆柱
        r0 = d_local * 0.5
        r1 = dh_local * 0.5
        prof = np.where(d < r0,
                        np.where(r1 >= r0, h_local,
                                 np.where(d <= r1, h_local,
                                          h_local * (r0 - d) / np.maximum(r0 - r1, 1e-12))),
                        0.0)
    elif params.brush_shape == "hemisphere":
        r = d_local * 0.5
        k = np.clip(d / np.maximum(r, 1e-12), 0.0, 1.0)
        prof = np.where(d < r, h_local * np.sqrt(np.maximum(0.0, 1.0 - k * k)), 0.0)
    else:
        # 高斯核 × Hann 窗
        sigma = (dh_local * 0.5) / math.sqrt(2.0 * ln2)
        cutoff = d_local * 0.5
        with np.errstate(divide="ignore", invalid="ignore"):
            g = np.exp(-(d * d) / (2.0 * sigma * sigma))
        rr = np.clip(d / np.maximum(cutoff, 1e-12), 0.0, 1.0)
        win = 0.5 * (1.0 + np.cos(np.pi * rr))
        prof = np.where(d < cutoff, h_local * g * win, 0.0)

    delta[near] = prof[near]

    # 作用范围按当前颜色裁剪（颜色无效/颜色内/颜色外）
    if params.color_scope in ("inside", "outside"):
        if color_mask is not None and color_mask.shape == (ny, nx):
            inside_cur = color_mask == int(color_value)
        else:
            inside_cur = np.zeros((ny, nx), dtype=bool)
        if params.color_scope == "inside":
            delta[~inside_cur] = 0.0
        else:
            delta[inside_cur] = 0.0
    return delta


def _brush_stamp_roi(hf, cx: float, cy: float, params: SweepParams):
    """计算单次笔刷印章的**局部**增量（只覆盖笔刷外接矩形，不分配整场）。

    返回 ``(row0, row1, col0, col1, prof)``：prof 为 float64 的
    ``(row1-row0+1, col1-col0+1)`` 增量，直接对应
    ``hf.data[row0:row1+1, col0:col1+1]``。无影响（高度为 0 / 完全在模型外）
    返回 None。

    塑形 drag 每帧要盖多章：旧实现每章都新建 (ny, nx) 的 float64 全零场，
    3000² 场单章 ~54ms（颜色裁剪 ~106ms），拖动直接卡死；局部化后成本只随
    笔刷直径与栅格间距变化，与模型图幅无关。
    """
    D = max(float(params.diameter), 1e-6)
    dH = min(max(float(params.half_life_diameter), 0.0), D)
    H = abs(float(params.height))
    if H < 1e-9:
        return None
    ln2 = math.log(2.0)
    r = D * 0.5
    ox, oy = hf.origin
    sp = hf.spacing
    col_lo = max(0, int(math.floor((cx - r - ox) / sp)))
    col_hi = min(hf.nx - 1, int(math.ceil((cx + r - ox) / sp)))
    row_lo = max(0, int(math.floor((cy - r - oy) / sp)))
    row_hi = min(hf.ny - 1, int(math.ceil((cy + r - oy) / sp)))
    if col_lo > col_hi or row_lo > row_hi:
        return None

    ys = oy + np.arange(row_lo, row_hi + 1, dtype=np.float64) * sp
    xs = ox + np.arange(col_lo, col_hi + 1, dtype=np.float64) * sp
    dx = xs[None, :] - cx
    dy = ys[:, None] - cy
    d = np.hypot(dx, dy)

    if params.brush_shape == "cone":
        r0 = D * 0.5
        r1 = dH * 0.5
        prof = np.where(d < r0,
                        np.where(r1 >= r0, H,
                                 np.where(d <= r1, H,
                                          H * (r0 - d) / max(r0 - r1, 1e-12))),
                        0.0)
    elif params.brush_shape == "hemisphere":
        k = np.clip(d / max(r, 1e-12), 0.0, 1.0)
        prof = np.where(d < r, H * np.sqrt(np.maximum(0.0, 1.0 - k * k)), 0.0)
    else:
        # 高斯核 × Hann 窗
        sigma = (dH * 0.5) / math.sqrt(2.0 * ln2)
        cutoff = D * 0.5
        g = np.exp(-(d * d) / (2.0 * sigma * sigma))
        rr = np.clip(d / max(cutoff, 1e-12), 0.0, 1.0)
        win = 0.5 * (1.0 + np.cos(np.pi * rr))
        prof = np.where(d < cutoff, H * g * win, 0.0)

    return row_lo, row_hi, col_lo, col_hi, prof


def compute_brush_stamp(hf, cx: float, cy: float, params: SweepParams) -> np.ndarray:
    """以 (cx, cy) 为中心的单次笔刷增量场 (ny, nx)（涂抹/雕刻一笔的印章）。

    截面与导动相同（高斯/锥体/半球，w=1 无沿程调制）；负高度即雕刻
    （去除材料）。作用范围（颜色裁剪）由调用方处理。
    """
    delta = np.zeros((hf.ny, hf.nx), dtype=np.float64)
    hit = _brush_stamp_roi(hf, cx, cy, params)
    if hit is None:
        return delta
    row_lo, row_hi, col_lo, col_hi, prof = hit
    delta[row_lo:row_hi + 1, col_lo:col_hi + 1] = prof
    return delta


def apply_brush_stamp(hf, cx: float, cy: float, params: SweepParams,
                      color_mask: np.ndarray | None = None,
                      color_value: int = 0) -> int:
    """把单次笔刷印章写进高度场（正高度=堆料累加，负高度=雕刻去除）。

    作用范围按当前颜色裁剪（颜色无效/颜色内/颜色外）。返回受影响格数。
    只读写笔刷外接矩形内的格子（O(ROI)），与模型图幅无关。
    """
    hit = _brush_stamp_roi(hf, cx, cy, params)
    if hit is None:
        return 0
    row_lo, row_hi, col_lo, col_hi, prof = hit
    if params.color_scope in ("inside", "outside"):
        if color_mask is not None and color_mask.shape == (hf.ny, hf.nx):
            inside_cur = color_mask[row_lo:row_hi + 1,
                                    col_lo:col_hi + 1] == int(color_value)
        else:
            inside_cur = None
        if params.color_scope == "inside":
            if inside_cur is None:
                return 0
            prof = np.where(inside_cur, prof, 0.0)
        elif inside_cur is not None:
            prof = np.where(inside_cur, 0.0, prof)
    mask = prof != 0.0
    moved = int(np.count_nonzero(mask))
    if moved == 0:
        return 0
    sign = -1.0 if params.height < 0 else 1.0
    sub = hf.data[row_lo:row_hi + 1, col_lo:col_hi + 1]
    sub[mask] += sign * prof[mask]
    return moved


def apply_sweep(hf, poly: np.ndarray, params: SweepParams,
                color_mask: np.ndarray | None = None,
                color_value: int = 0,
                base: np.ndarray | None = None,
                closed: bool | None = None) -> int:
    """沿导动线把增量写入高度场，返回受影响格数。

    叠加方式（apply）：堆料沿曲线升高、取料沿曲线雕刻去除（方向）；
    效果（merge）：累加每次叠加、流动叠加取层包络（重复点击不变）：
    - 堆料+累加：z += 增量；取料+累加：z -= 增量；
    - 堆料+流动：z = max(z, 底座+增量)；取料+流动：z = min(z, 底座-增量)。
      ``base`` 为固化底座（高度场形状）；未固化时底座取 0。
    ``closed`` 为导动线原始闭合标志（见 compute_sweep_delta）。
    """
    delta = compute_sweep_delta(hf, poly, params, color_mask, color_value,
                                closed=closed)
    sign = -1.0 if params.apply == "remove" else 1.0
    moved = int(np.count_nonzero(np.abs(delta) > 1e-6))
    if moved == 0:
        return 0
    mask = np.abs(delta) > 1e-6
    signed = sign * delta
    data = hf.data
    if params.merge == "flow":
        if base is not None and base.shape == data.shape:
            layer = base.astype(np.float32) + signed
        else:
            layer = signed
        if sign > 0:
            data[mask] = np.maximum(data[mask], layer[mask])   # 堆料:取高包络
        else:
            data[mask] = np.minimum(data[mask], layer[mask])   # 取料:取低包络
    else:      # 累加
        data[mask] += signed[mask]
    return moved


# ==================== 笔刷移动（高度场内容平移） ====================

def _brush_weight(d: np.ndarray, D: float, dH: float,
                  shape: str) -> np.ndarray:
    """笔刷归一化权重场（中心 1、边缘 0），与盖章截面同形去掉高度。

    d=格点到笔刷中心的距离，D=直径，dH=半衰直径，shape=形状。
    """
    r = D * 0.5
    if shape == "cone":
        r0 = r
        r1 = dH * 0.5
        if r1 >= r0:
            return np.where(d < r0, 1.0, 0.0)
        return np.where(d < r0,
                        np.where(d <= r1, 1.0,
                                 (r0 - d) / max(r0 - r1, 1e-12)),
                        0.0)
    if shape == "hemisphere":
        k = np.clip(d / max(r, 1e-12), 0.0, 1.0)
        return np.where(d < r, np.sqrt(np.maximum(0.0, 1.0 - k * k)), 0.0)
    # 高斯核 × Hann 窗
    sigma = (dH * 0.5) / math.sqrt(2.0 * math.log(2.0))
    cutoff = r
    with np.errstate(divide="ignore", invalid="ignore"):
        g = np.exp(-(d * d) / (2.0 * sigma * sigma))
    rr = np.clip(d / max(cutoff, 1e-12), 0.0, 1.0)
    win = 0.5 * (1.0 + np.cos(np.pi * rr))
    return np.where(d < cutoff, g * win, 0.0)


def _bilinear_sample_field(arr: np.ndarray, xs: np.ndarray,
                           ys: np.ndarray, ox: float, oy: float,
                           sp: float) -> np.ndarray:
    """对 2D 世界坐标场 (xs, ys)（同形）在 arr 上逐点双线性采样。

    arr 形状 (ny, nx)，行 0 = minY、列 0 = minX（与 HeightField 约定一致）。
    xs/ys 为同形 2D 数组（每格的采样世界坐标）。越界处返回 0。
    """
    ny, nx = arr.shape
    cols = (xs - ox) / sp
    rows = (ys - oy) / sp
    c0 = np.floor(cols).astype(np.int64)
    r0 = np.floor(rows).astype(np.int64)
    tc = cols - c0
    tr = rows - r0
    # 恰好落在右/上格线（c0 == n-1 → c1 == n）是合法边界采样：c1/r1 钳回
    # n-1（与 HeightField.sample_bilinear 同语义）。旧判定把 c1==n 视为
    # 越界返回 0，整格平移印章贴边时丢一行/列
    c1 = np.minimum(c0 + 1, nx - 1)
    r1 = np.minimum(r0 + 1, ny - 1)
    valid = (c0 >= 0) & (c0 <= nx - 1) & (r0 >= 0) & (r0 <= ny - 1)
    c0c = np.clip(c0, 0, nx - 1)
    r0c = np.clip(r0, 0, ny - 1)
    top = arr[r0c, c0c] * (1 - tc) + arr[r0c, c1] * tc
    bot = arr[r1, c0c] * (1 - tc) + arr[r1, c1] * tc
    res = top * (1 - tr) + bot * tr
    return np.where(valid, res, 0.0)


def move_brush_region(hf, cx: float, cy: float,
                      ddx: float, ddy: float,
                      params: SweepParams) -> int:
    """推移：笔刷中心 (cx, cy) 跟随鼠标，本帧增量位移 (ddx, ddy)。

    笔刷范围内每格按权重 w 位移 w·(ddx, ddy)，从上一帧状态采样
    new(x,y) = buf(x - w·ddx, y - w·ddy)——中心完全跟着鼠标走、
    边缘不动，内容连续流动不消失（跟推泥土一样）。就地修改 hf.data。
    返回受影响格数。
    """
    ny, nx = hf.ny, hf.nx
    D = max(float(params.diameter), 1e-6)
    dH = min(max(float(params.half_life_diameter), 0.0), D)
    shape = params.brush_shape
    r = D * 0.5
    ox, oy = hf.origin
    sp = hf.spacing

    col_lo = max(0, int(math.floor((cx - r - ox) / sp)))
    col_hi = min(nx - 1, int(math.ceil((cx + r - ox) / sp)))
    row_lo = max(0, int(math.floor((cy - r - oy) / sp)))
    row_hi = min(ny - 1, int(math.ceil((cy + r - oy) / sp)))
    if col_lo > col_hi or row_lo > row_hi:
        return 0
    if abs(ddx) < 1e-9 and abs(ddy) < 1e-9:
        return 0

    # 采样点 = ROI 格心 − w·dd（w∈[0,1]），最大偏移 ≤ |dd|：源缓冲只需
    # ROI ± |dd| 的局部窗口。整场 copy 在 4096² 场是 64MB/帧，涂抹笔刷
    # 30fps 下即持续 GB 级分配抖动
    pad_c = int(math.ceil(abs(ddx) / sp)) + 1
    pad_r = int(math.ceil(abs(ddy) / sp)) + 1
    wc_lo = max(0, col_lo - pad_c)
    wc_hi = min(nx - 1, col_hi + pad_c)
    wr_lo = max(0, row_lo - pad_r)
    wr_hi = min(ny - 1, row_hi + pad_r)
    buf = hf.data[wr_lo:wr_hi + 1, wc_lo:wc_hi + 1].copy()

    ys = (oy + np.arange(row_lo, row_hi + 1, dtype=np.float64) * sp)[:, None]
    xs = (ox + np.arange(col_lo, col_hi + 1, dtype=np.float64) * sp)[None, :]
    dist = np.hypot(xs - cx, ys - cy)
    w = _brush_weight(dist, D, dH, shape)

    samp_x = xs - w * ddx
    samp_y = ys - w * ddy
    new_vals = _bilinear_sample_field(buf, samp_x, samp_y,
                                      ox + wc_lo * sp, oy + wr_lo * sp, sp)
    hf.data[row_lo:row_hi + 1, col_lo:col_hi + 1] = new_vals.astype(np.float32)
    return int(new_vals.size)


# ==================== 区域变换（高度场区域内容仿射变换） ====================

def _mask_bbox(mask: np.ndarray):
    """mask 外接矩形 (col_lo, row_lo, col_hi, row_hi)，空返回 None。"""
    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)
    if not rows.any() or not cols.any():
        return None
    row_lo = int(np.argmax(rows))
    row_hi = int(len(rows) - 1 - np.argmax(rows[::-1]))
    col_lo = int(np.argmax(cols))
    col_hi = int(len(cols) - 1 - np.argmax(cols[::-1]))
    return col_lo, row_lo, col_hi, row_hi


def transform_region(hf, stamp: np.ndarray, stamp_mask: np.ndarray,
                     base: np.ndarray, M: np.ndarray,
                     src_bbox, z_scale: float = 1.0) -> int:
    """把印章场 stamp（mask 内内容、外为 0）按仿射 M 整体变换写入高度场。

    区域内容作为独立印章：原位置清空（base 已把 mask 内置 0），变换后
    印章覆盖目标位置（直接取代），可移到模型任意位置，不受原 mask 范围
    限制。旋转/缩放绕区域自身中心（由 M 编码）。就地修改 hf.data。
    z_scale: 印章高度均匀缩放倍率（每个格子高度 *= z_scale）。

    stamp: where(mask, source, 0)；stamp_mask: mask.astype(float)；
    base: where(mask, 0, source)；M: 3x3 正变换仿射（local->world）；
    src_bbox: 印章外接矩形 (col_lo, row_lo, col_hi, row_hi)。
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
    src_x_lo = ox + col_lo * sp
    src_x_hi = ox + col_hi * sp
    src_y_lo = oy + row_lo * sp
    src_y_hi = oy + row_hi * sp
    a_col_lo = max(0, int(math.floor((min(min(wxs), src_x_lo) - sp - ox) / sp)))
    a_col_hi = min(nx - 1, int(math.ceil((max(max(wxs), src_x_hi) + sp - ox) / sp)))
    a_row_lo = max(0, int(math.floor((min(min(wys), src_y_lo) - sp - oy) / sp)))
    a_row_hi = min(ny - 1, int(math.ceil((max(max(wys), src_y_hi) + sp - oy) / sp)))
    if a_col_lo > a_col_hi or a_row_lo > a_row_hi:
        return 0

    ys = (oy + np.arange(a_row_lo, a_row_hi + 1, dtype=np.float64) * sp)[:, None]
    xs = (ox + np.arange(a_col_lo, a_col_hi + 1, dtype=np.float64) * sp)[None, :]
    src_x = inv_M[0, 0] * xs + inv_M[0, 1] * ys + inv_M[0, 2]
    src_y = inv_M[1, 0] * xs + inv_M[1, 1] * ys + inv_M[1, 2]

    trans_stamp = _bilinear_sample_field(stamp, src_x, src_y, ox, oy, sp)
    trans_mask = _bilinear_sample_field(stamp_mask, src_x, src_y, ox, oy, sp)
    base_sub = base[a_row_lo:a_row_hi + 1, a_col_lo:a_col_hi + 1]
    new_vals = np.where(trans_mask > 0.5, trans_stamp * z_scale, base_sub)
    hf.data[a_row_lo:a_row_hi + 1, a_col_lo:a_col_hi + 1] = new_vals.astype(np.float32)
    return int((trans_mask > 0.5).sum())


# ==================== 水平校准（去除沿两点连线的线性倾斜） ====================

def level_calibrate(hf, p1x: float, p1y: float,
                    p2x: float, p2y: float,
                    mask: np.ndarray | None = None) -> bool:
    """以 (p1, p2) 两点为准水平校准：在作用范围内去除沿 p1→p2 方向的线性
    倾斜分量，使两点高度都变为它们的高度均值。

    作用范围：mask=None 时整个模型；mask 给定时仅 mask 内（mask 外不变）。
    设沿 p1→p2 方向有符号投影距离 t，斜率 slope=(h2-h1)/L，减去
    slope·(t - L/2)：p1 处 t=0 → h1+(h2-h1)/2=均值；p2 处 t=L → 均值。
    垂直于 p1→p2 方向的倾斜不变（2 点只能定 1 个方向的倾斜）。就地改 hf.data。
    两点重合或作用范围越界返回 False。
    """
    h1 = hf.sample_bilinear(p1x, p1y)
    h2 = hf.sample_bilinear(p2x, p2y)
    dx = p2x - p1x
    dy = p2y - p1y
    L = math.hypot(dx, dy)
    if L < 1e-9:
        return False
    ny, nx = hf.ny, hf.nx
    ox, oy = hf.origin
    sp = hf.spacing
    # 作用范围 bbox：mask 给定取 mask 外接矩形，否则整个场
    if mask is not None:
        rows = np.any(mask, axis=1)
        cols = np.any(mask, axis=0)
        if not rows.any() or not cols.any():
            return False
        row_lo = int(np.argmax(rows))
        row_hi = int(len(rows) - 1 - np.argmax(rows[::-1]))
        col_lo = int(np.argmax(cols))
        col_hi = int(len(cols) - 1 - np.argmax(cols[::-1]))
    else:
        row_lo, row_hi, col_lo, col_hi = 0, ny - 1, 0, nx - 1
    if col_lo > col_hi or row_lo > row_hi:
        return False
    ys = (oy + np.arange(row_lo, row_hi + 1, dtype=np.float64) * sp)[:, None]
    xs = (ox + np.arange(col_lo, col_hi + 1, dtype=np.float64) * sp)[None, :]
    # 沿 p1→p2 方向有符号投影距离
    t = ((xs - p1x) * dx + (ys - p1y) * dy) / L
    # 减去线性倾斜分量：delta = (h2-h1)·(t/L - 0.5)
    delta = (h2 - h1) * (t / L - 0.5)
    sub = hf.data[row_lo:row_hi + 1, col_lo:col_hi + 1].astype(np.float64)
    if mask is not None:
        mask_sub = mask[row_lo:row_hi + 1, col_lo:col_hi + 1]
        new_sub = np.where(mask_sub, sub - delta, sub)
    else:
        new_sub = sub - delta
    hf.data[row_lo:row_hi + 1, col_lo:col_hi + 1] = new_sub.astype(np.float32)
    return True

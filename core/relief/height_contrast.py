"""高差调整（浮雕高度对比度增强）：以某个**基准面**为不动点，把"离基准的高度
偏差"整体放大——高于基准的更高、低于基准的更低，即 `z' = base + f(z - base)`。

用途：浮雕图"凹凸不明显"本质是全部地形挤在一个很窄的 Z 区间里（对比度不足）。
本算子按 mm 直接拉开这个区间，比手工重新图转模型调参快得多。

映射（逐格，全部 float32、分块就地计算，内存与场大小无关）：

    d   = z - base                 # 相对基准的偏差
    ad  = |d|
    e   = max(ad - db, 0)          # db = 噪声保护死区：微小起伏不参与拉伸
    inc = (g - 1) * e              # 线性：只放大死区外的超出量
    inc = sat * tanh((g - 1) * e / sat)   # 柔和：增量渐近饱和，远端不过冲
    z'  = z + sign(d) * inc        # 死区内 inc 恒为 0 → 逐格不变

* **连续无跳变、基准不被推移**：增量是 `e` 的连续函数且 `e=0` 处为 0，故
  基准面/背景格始终不动，区域边界不会出现“台阶”。
* **不会越过基准**：`|inc| ≤ (g-1)·e ≤ ad`（g<1 时 a' 最低降到 db），压缩
  不会把凸起直接推成凹陷。
* **目标高差模式**：用户心智是“现在只有 1.2 mm，我要 3 mm”，比倍率直观；
  按作用域有效高差 (p99 - p1，抗孤峰/凹坑离群) 反算 g。

与"图转模型"阶段的高低层次重映射 (`image_to_model.remap_height`) 互补：后者在
生成时用归一化分层调整、作用全场；本算子对**已有模型**按 mm 工作，可限定区域、
可实时预览、可撤销。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# 基准面取法（与面板下拉一一对应）
BASES = ("auto", "zero", "min", "max", "mean", "median")
BASE_LABELS = ("背景面(自动)", "0 平面", "最低点", "最高点", "平均值", "中位数")

# 曲线方式
CURVES = ("linear", "soft")
CURVE_LABELS = ("线性", "柔和")

# 增强方式
GAIN_MODES = ("gain", "target")
GAIN_MODE_LABELS = ("增益", "目标高差")

_F32 = np.float32
_P_LO, _P_HI = 1.0, 99.0   # 有效高差统计分位（抗离群）
_BG_QUANT = 5.0            # "背景面(自动)" 取作用域 5% 分位
_STAT_MAX = 1 << 18        # 分位统计最多采样格数（p1/p99 用 26 万样本已稳定）
_CHUNK_ELEMS = 1 << 20     # 单次处理的格数上限（约 4MB/临时，内存有界）
_GAIN_MIN, _GAIN_MAX = 0.05, 20.0

# 面板默认值与取值范围（UI 直接用，避免两边各写一套数）
DEFAULT_GAIN_PCT = 150.0
GAIN_PCT_MIN, GAIN_PCT_MAX = 20.0, 500.0
DEFAULT_TARGET_MM = 3.0
DEFAULT_DEADBAND = 0.05


@dataclass
class HeightContrastParams:
    """高差调整参数。"""

    base: str = "auto"          # 基准面：见 BASES
    mode: str = "gain"          # gain 增益倍率 / target 目标有效高差
    gain: float = 1.5           # 增益倍率（>1 拉大高差，<1 压缩）
    target_mm: float = 3.0      # 目标有效高差 mm（mode=target）
    curve: str = "linear"       # linear 线性 / soft 软饱和
    deadband: float = 0.05      # 噪声保护死区 mm（|d| 以内不动）
    clip_range: bool = False    # 限幅到作用域原 [min, max]

    def clamp(self) -> "HeightContrastParams":
        """把输入钳到合法区间（面板已限范围，这里兜底外部调用）。"""
        if self.base not in BASES:
            self.base = "auto"
        if self.mode not in GAIN_MODES:
            self.mode = "gain"
        if self.curve not in CURVES:
            self.curve = "linear"
        self.gain = float(min(max(float(self.gain), _GAIN_MIN), _GAIN_MAX))
        self.target_mm = max(0.0, float(self.target_mm))
        self.deadband = max(0.0, float(self.deadband))
        return self


def solve_gain(p: HeightContrastParams, span: float) -> float:
    """增强方式 → 实际倍率：目标高差模式按有效高差反算。

    反算计入死区偏差：拉伸后高差 = g·span - 2(g-1)·db（上下两端各扣一个死区），
    故 g = (target - 2db) / (span - 2db)，否则大倍率下会系统性小于目标值。
    """
    if p.mode == "target":
        e = span - 2.0 * p.deadband
        if e <= 1e-6:
            return 1.0
        return float(min(max((p.target_mm - 2.0 * p.deadband) / e,
                             _GAIN_MIN), _GAIN_MAX))
    return float(min(max(p.gain, _GAIN_MIN), _GAIN_MAX))


def _rows_of(nx: int) -> int:
    """单次处理的行数（保证块内格数 ≤ _CHUNK_ELEMS，临时缓冲内存有界）。"""
    return max(1, _CHUNK_ELEMS // max(int(nx), 1))


def _stats_scan(z: np.ndarray, mask: np.ndarray | None = None):
    """作用域统计扫描：返回 (精确 min, 精确 max, 分位样本)。

    min/max 取精确值（“最低点/最高点”基准与限幅依赖它）；分位只用有界抽样
    （≤ _STAT_MAX 格），蒙版情形按行分块 gather，避免一次性物化整个区域。
    """
    ny = int(z.shape[0])
    if mask is None:
        v = z.ravel()
        if v.size == 0:
            return 0.0, 0.0, v
        step = max(1, int(v.size) // _STAT_MAX)
        return float(v.min()), float(v.max()), v[::step]
    rows = _rows_of(int(z.shape[1]))
    quota = max(1024, _STAT_MAX // max(1, -(-ny // rows)))
    lo = hi = None
    parts: list = []
    for r0 in range(0, ny, rows):
        mc = mask[r0:r0 + rows]
        if not mc.any():
            continue
        sub = z[r0:r0 + rows][mc]
        smin, smax = float(sub.min()), float(sub.max())
        lo = smin if lo is None else min(lo, smin)
        hi = smax if hi is None else max(hi, smax)
        parts.append(sub[::max(1, int(sub.size) // quota)])
    if lo is None:
        return 0.0, 0.0, np.zeros(1, dtype=_F32)
    return lo, hi, np.concatenate(parts)


def base_height(vals: np.ndarray, mode: str, lo: float, hi: float) -> float:
    """按取法算基准面高度（作用域内；min/max 用精确扫描值）。"""
    if mode == "zero":
        return 0.0
    if mode == "min":
        return float(lo)
    if mode == "max":
        return float(hi)
    if mode == "mean":
        return float(vals.mean())
    if mode == "median":
        return float(np.median(vals))
    return float(np.percentile(vals, _BG_QUANT))


def measure(z: np.ndarray, p: HeightContrastParams,
            mask: np.ndarray | None = None) -> dict:
    """只统计不修改：返回基准/有效高差/实际倍率（供面板信息行）。"""
    p = p.clamp()
    lo, hi, vals = _stats_scan(np.asarray(z, dtype=_F32), mask)
    if vals.size == 0:
        return {"ok": False, "cells": 0, "base": 0.0, "lo": 0.0, "hi": 0.0,
                "span": 0.0, "gain": 1.0, "deadband": p.deadband}
    base = base_height(vals, p.base, lo, hi)
    p_lo, p_hi = np.percentile(vals, [_P_LO, _P_HI])
    span = float(p_hi) - float(p_lo)
    return {"ok": True, "cells": int(mask.sum()) if mask is not None
            else int(z.size), "base": base, "lo": lo, "hi": hi,
            "span": span, "gain": solve_gain(p, span),
            "deadband": p.deadband}


def apply_height_contrast(hf, p: HeightContrastParams,
                          mask: np.ndarray | None = None) -> dict:
    """就地执行高差调整，返回统计（含调整前/后有效高差）。

    hf   : HeightField（写 hf.data，float32 (ny, nx)，单位 mm）
    p    : HeightContrastParams
    mask : 作用域蒙版 (ny, nx) bool；None = 整个模型

    返回 {"before", "after", "gain", "base", "cells", "lo", "hi"}；
    cells = 实际发生变化的格数（0 表示没有生效，例如倍率 1 且死区外无高差）。
    """
    p = p.clamp()
    data = hf.data
    if data.ndim != 2 or data.size == 0:
        return {"before": 0.0, "after": 0.0, "gain": 1.0, "base": 0.0,
                "cells": 0, "lo": 0.0, "hi": 0.0}
    ny, nx = int(data.shape[0]), int(data.shape[1])
    if mask is not None and mask.shape != data.shape:
        mask = None

    lo, hi, vals = _stats_scan(data, mask)
    if vals.size == 0:
        return {"before": 0.0, "after": 0.0, "gain": 1.0, "base": 0.0,
                "cells": 0, "lo": 0.0, "hi": 0.0}
    base = _F32(base_height(vals, p.base, lo, hi))
    p_lo, p_hi = np.percentile(vals, [_P_LO, _P_HI])
    before = float(p_hi) - float(p_lo)
    g = solve_gain(p, before)
    db = _F32(p.deadband)
    # 柔和曲线的增量饱和幅度：取作用域最大偏差尺度，增量最多再叠一个原幅度
    sat = _F32(max(abs(float(p_hi) - float(base)),
                   abs(float(base) - float(p_lo)), before, 1e-3))
    stats = {"before": before, "after": before, "gain": g, "base": float(base),
             "cells": 0, "lo": lo, "hi": hi}
    if abs(g - 1.0) < 1e-6 and p.curve == "linear":
        return stats    # 恒等映射，不必扫场

    rows = _rows_of(nx)
    changed = 0
    for r0 in range(0, ny, rows):
        blk = data[r0:r0 + rows]
        mc = None if mask is None else mask[r0:r0 + rows]
        d = np.subtract(blk, base, dtype=_F32)      # 偏差
        ad = np.abs(d)                              # 偏差幅
        e = np.subtract(ad, db, dtype=_F32)         # 死区外超出量
        np.maximum(e, _F32(0.0), out=e)
        delta = np.multiply(e, _F32(g - 1.0), dtype=_F32)   # 增量幅 inc
        if p.curve == "soft":
            np.divide(delta, sat, out=delta)
            np.tanh(delta, out=delta)
            delta *= sat                            # 增量饱和于 sat
        np.sign(d, out=d)
        delta *= d
        del ad, e, d
        if mc is not None:
            delta[~mc] = _F32(0.0)                 # 区域外零增量（保持原值）
        np.add(blk, delta, out=blk)
        if p.clip_range:
            # 限幅只作用于作用域：区域外原值可能本就在 [lo,hi] 之外，不得改动
            if mc is None:
                np.clip(blk, _F32(lo), _F32(hi), out=blk)
            else:
                t = np.clip(blk, _F32(lo), _F32(hi))
                np.copyto(blk, t, where=mc)
                del t
        changed += int(np.count_nonzero(delta))
        del delta
    stats["cells"] = changed
    if changed:
        _, _, after_vals = _stats_scan(data, mask)
        if after_vals.size:
            a_lo, a_hi = np.percentile(after_vals, [_P_LO, _P_HI])
            stats["after"] = float(a_hi) - float(a_lo)
    return stats


def format_stats(stats: dict) -> str:
    """统计 → 一句话说明（命令结果栏用，单行）。"""
    b, a = stats.get("before", 0.0), stats.get("after", 0.0)
    ratio = (a / b) if b > 1e-6 else 1.0
    return ("高差 %.2f → %.2f mm（×%.2f，基准 %.2f mm，%d 格）"
            % (b, a, ratio, stats.get("base", 0.0), stats.get("cells", 0)))


def _fmt_cells(n: int) -> str:
    """格数压缩写法（≥1 万用“万格”，避免数字长到多占一行）。"""
    n = int(n)
    return "%.1f 万格" % (n / 10000.0) if n >= 10000 else "%d 格" % n


def format_stats_lines(stats: dict) -> str:
    """统计 → 两行短文本（面板信息行用）。

    面板可用宽只有 ~244px（宿主固定 270），单行长文本会折成 3-4 行，而宿主按
    sizeHint 定高→信息行被裁；故每行只放两个数，保证一行装得下。
    """
    b, a = stats.get("before", 0.0), stats.get("after", 0.0)
    ratio = (a / b) if b > 1e-6 else 1.0
    return ("高差 %.2f→%.2fmm ×%.1f\n基准 %.2f · %s"
            % (b, a, ratio, stats.get("base", 0.0),
               _fmt_cells(stats.get("cells", 0))))

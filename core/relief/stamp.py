"""冲压与高低层次调整 —— 复刻自 Rhino 插件 ReliefAlgorithms 的
ExecuteStamp / ExecuteLevelAdjust（冲压页），并按本应用的交互重新设计蒙版。

与插件的差异在**蒙版来源**：Rhino 没有颜色模块，插件用"曲线内/外"
（扫描线填充 BuildInsideMask）限定冲压范围；本应用有色号蒙版
（ReliefModelObject.colorMask，浮雕模式空格填色），冲压区域二选一：

- 颜色无效（ignore）：**鼠标圆圈**区域——光标跟随一个可调直径的圆
  （A 缩小 / S 放大），定冲压区域大小；
- 颜色内（inside）：**单击处色号的整个色块区域**（全部同色号格子，
  不受圆圈限制）。

- 冲压（ExecuteStamp）：
  - 绝对（absolute）：范围内所有格子高度直接 = 输入值（z = value），
    形成平整台面——即"按模面冲压"；
  - 相对（relative）：范围内格子高度 = 原高度 ± 输入值（正/负方向）。
- 高低层次调整（ExecuteLevelAdjust）：取**整个模型**的高度范围
  [zMin, zMax]，按"中层位置"百分比切分上、下两段；高层/底层调整量按
  smoothstep（0→0、1→1、两端零斜率）从中层到顶/底柔性加权生效——
  顶/底全量、中层不动。作用区域（全部/颜色内/颜色外，相对底部调色板
  当前颜色）只决定哪些格子参与调整，高度范围始终按整个模型统计。

坐标约定与 HeightField 一致：``data[row, col]``，行 0 = minY、列 0 = minX。
纯算法、零 UI 依赖、纯 numpy（不依赖 scipy）。
"""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np

# 参数默认值（对齐插件冲压页输入框初值）
DEFAULT_ABSOLUTE = 5.0
DEFAULT_RELATIVE = 1.0
DEFAULT_HIGH_LEVEL = 1.0
DEFAULT_LOW_LEVEL = 1.0
DEFAULT_MID_PERCENT = 50.0
# 冲压圆圈默认直径（mm）；A/S 快捷键 ±10% 缩放
DEFAULT_DIAMETER = 10.0
DIAMETER_SCALE = 1.1
DIAMETER_MIN = 0.2
DIAMETER_MAX = 2000.0

MODES = ("absolute", "relative")
MODE_LABELS = ("绝对", "相对")
DIRECTIONS = ("positive", "negative")
DIRECTION_LABELS = ("正方向", "负方向")
# 冲压作用范围：颜色无效（鼠标圆圈）/ 颜色内（单击处色号整个色块）
SCOPES = ("ignore", "inside")
SCOPE_LABELS = ("颜色无效", "颜色内")
# 层次调整的作用区域（相对底部调色板当前颜色）：全部 / 颜色内 / 颜色外
LEVEL_REGIONS = ("all", "inside", "outside")
LEVEL_REGION_LABELS = ("全部", "颜色内", "颜色外")


@dataclass
class StampParams:
    """冲压参数（mode/direction/scope 对应插件冲压分组；scope 为颜色二元）。"""

    mode: str = "absolute"          # absolute=绝对 / relative=相对
    direction: str = "positive"     # positive=正方向 / negative=负方向（仅相对）
    scope: str = "ignore"           # ignore=颜色无效 / inside=颜色内
    value: float = DEFAULT_ABSOLUTE


def _smoothstep(t: np.ndarray) -> np.ndarray:
    """smoothstep：0→0、1→1，两端斜率为 0，使高低两侧在中层平滑相接（无尖点）。"""
    t = np.clip(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def build_circle_mask(hf, center_xy, radius: float) -> np.ndarray:
    """圆心 + 半径（世界 mm）→ 格点圆掩膜 (ny, nx) bool。

    格点 (row, col) 的世界坐标 = origin + index × spacing，
    到圆心距离 ≤ radius 即在圈内。
    """
    ox, oy = hf.origin
    xs = ox + np.arange(hf.nx, dtype=np.float64) * hf.spacing
    ys = oy + np.arange(hf.ny, dtype=np.float64) * hf.spacing
    dx = xs[None, :] - float(center_xy[0])
    dy = ys[:, None] - float(center_xy[1])
    r = float(radius)
    return (dx * dx + dy * dy) <= (r * r)


def _color_equal_mask(hf, color_mask: np.ndarray | None,
                      color_value: int) -> np.ndarray:
    """色号 == color_value 的格子掩膜。

    蒙版 None/形状不符视作全 0（全场未填色）：color_value=0 → 全场命中，
    否则全不命中。
    """
    if color_mask is not None and getattr(color_mask, "shape", None) == (hf.ny, hf.nx):
        return color_mask == int(color_value)
    return np.full((hf.ny, hf.nx), int(color_value) == 0, dtype=bool)


def _stamp_mask(hf, circle_mask: np.ndarray | None, params: StampParams,
                color_mask: np.ndarray | None, color_value: int) -> np.ndarray:
    """冲压工作掩膜：颜色无效 = 鼠标圆圈；颜色内 = 单击处色号的整个色块。"""
    if params.scope == "inside":
        # 单击处色号的整个色块区域（全部同色号格子，不受圆圈限制）
        return _color_equal_mask(hf, color_mask, color_value)
    # ignore：颜色无效，鼠标圆圈区域
    if circle_mask is None or getattr(circle_mask, "shape", None) != (hf.ny, hf.nx):
        return np.zeros((hf.ny, hf.nx), dtype=bool)
    return circle_mask


def apply_stamp(hf, params: StampParams, circle_mask: np.ndarray | None,
                color_mask: np.ndarray | None = None,
                color_value: int = 0) -> int:
    """冲压：把范围内格子的高度改为绝对值/加减相对值。

    hf: HeightField（就地修改 data）。circle_mask: build_circle_mask 的产物
    （仅颜色无效用）。color_value 为**单击处格子的色号**（颜色内用）。

    - absolute 绝对：z = value（平整台面）；
    - relative 相对：z = z ± value（direction 控制正负方向）。

    返回实际改变取值的格子数（重复冲同一绝对值不再计入），0 表示未命中。
    """
    data = hf.data
    mask = _stamp_mask(hf, circle_mask, params, color_mask, color_value)
    if not mask.any():
        return 0

    value = float(params.value)
    if params.mode == "relative":
        sign = -1.0 if params.direction == "negative" else 1.0
        new_vals = data.astype(np.float64) + sign * value
    else:
        new_vals = np.full(data.shape, value, dtype=np.float64)

    before = data[mask]
    after = new_vals[mask].astype(np.float32).astype(np.float64)
    changed = int(np.count_nonzero(before.astype(np.float64) != after))
    if changed:
        data[mask] = after.astype(np.float32)
    return changed


def _scope_mask(hf, color_mask: np.ndarray | None, color_value: int,
                scope: str) -> np.ndarray | None:
    """层次调整的作用区域掩膜（相对底部调色板当前颜色 color_value）。

    - inside  颜色内：色号 == 当前颜色的格子；
    - outside 颜色外：色号 != 当前颜色的格子；
    - 其他（all）返回 None，表示整个模型。
    color_mask 为 None 或形状与高度场不符视作全 0（未填色）。
    """
    if scope not in ("inside", "outside"):
        return None
    inside_cur = _color_equal_mask(hf, color_mask, color_value)
    return inside_cur if scope == "inside" else ~inside_cur


def apply_level_adjust(hf, high: float, low: float, mid_percent: float,
                       region: str = "all",
                       color_mask: np.ndarray | None = None,
                       color_value: int = 0) -> int:
    """高低层次调整：高层/底层调整量按 smoothstep 从中层到顶/底柔性生效。

    hf: HeightField（就地修改 data）。
      - high  高层调整量：高于中层的格子，从中层(0)到最高点(1)加权上升；
      - low   底层调整量：低于中层的格子，从中层(0)到最低点(1)加权上升；
      - mid_percent 中层位置（10–90 百分比，钳制到 1%–99%）；
      - region 作用区域：all 全部 / inside 颜色内 / outside 颜色外
        （相对当前颜色 color_value；高度范围始终按整个模型统计）。

    返回实际改变取值的格子数；模型无高度差（平场）时无需调整返回 0。
    """
    data = hf.data
    zmin = float(data.min())
    zmax = float(data.max())
    if zmax - zmin < 1e-9:
        return 0

    frac = min(0.99, max(0.01, float(mid_percent) / 100.0))
    zmid = zmin + frac * (zmax - zmin)
    top_range = max(1e-9, zmax - zmid)
    bot_range = max(1e-9, zmid - zmin)

    mask = _scope_mask(hf, color_mask, color_value, region)
    if mask is None:
        mask = np.ones(data.shape, dtype=bool)

    z = data.astype(np.float64)
    delta = np.where(z >= zmid,
                     float(high) * _smoothstep((z - zmid) / top_range),
                     float(low) * _smoothstep((zmid - z) / bot_range))
    # 调整量 numerically 0 的格子不写（与插件 |delta| < 1e-12 continue 一致）
    apply = mask & (np.abs(delta) >= 1e-12)
    if not apply.any():
        return 0

    before = data[apply].astype(np.float64)
    after = (z[apply] + delta[apply]).astype(np.float32).astype(np.float64)
    changed = int(np.count_nonzero(before != after))
    if changed:
        data[apply] = after.astype(np.float32)
    return changed

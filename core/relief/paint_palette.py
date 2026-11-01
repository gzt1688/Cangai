"""区域划分调色板（共享、纯数据、零 Qt / 零 UI 依赖）。

底部捕捉工具栏（ui/widgets/capture_toolbar.py）的一排颜色、当前颜色状态
（ui/widgets/paint_state.py）、颜色块渲染（ui/view_3d/viewport_3d.py 与
core/relief/mesh3d.py）都引用同一张表，保证"填色 → 渲染 → 作用区域裁剪"
用的索引与 RGB 完全一致。

蒙版取值约定（与 ReliefModelObject 的色号层一致）：
- ``EMPTY = 0``    未填色 = **浮雕模型基础色**（模型/图层颜色）；
- ``idx + 1``      调色板第 idx 个颜色（1..count()）。
之所以 +1，是让 0 恒定表示"空"，无需额外的掩膜数组。

第 2 个色块（索引 1 / 蒙版值 2）与浮雕模型基础色**直接关联**：
图层/模型改色时经 :func:`set_base_color` 同步，未填色格（EMPTY）在
色号裁剪中与基础色同值（见 :func:`equals_value_mask`）。

放在 core 而非 ui/styles/theme：core(mesh3d) 需要它，而 core 不得 import ui。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

# 31 色（迁移自 capture_toolbar.py 原硬编码 hex 列表，顺序即调色板顺序）；
# 可变列表：索引 1（第 2 色）为浮雕模型基础色槽，随图层色动态更新（UI 层
# 通过 PaintState.setBaseColor 调 :func:`set_base_color` 并重绘）。
PALETTE: List[Tuple[int, int, int]] = [
    (255,   0,   0),   # #FF0000 红
    (232, 236,  19),   # 基础色槽默认 #e8ec13（浮雕模型/图层默认颜色，set_base_color 同步）
    (  0,   0, 255),   # #0000FF 蓝
    (  0, 204,   0),   # #00CC00 绿
    (255, 136,   0),   # #FF8800 橙
    (136,   0, 255),   # #8800FF 紫
    (  0, 170, 170),   # #00AAAA 青
    (255, 182, 193),   # #FFB6C1 粉
    (139,  69,  19),   # #8B4513 棕
    (255, 255, 255),   # #FFFFFF 白
    (139,   0,   0),   # #8B0000 暗红
    (184, 134,  11),   # #B8860B 暗黄
    (  0,   0, 139),   # #00008B 暗蓝
    (  0, 100,   0),   # #006400 暗绿
    (204,  68,   0),   # #CC4400 砖橙
    ( 75,   0, 130),   # #4B0082 靛
    (  0, 139, 139),   # #008B8B 暗青
    (128, 128, 128),   # #808080 灰
    (  0,   0,   0),   # #000000 黑
    (245, 222, 179),   # #F5DEB3 麦色
    (255,   0, 255),   # #FF00FF 洋红
    (135, 206, 235),   # #87CEEB 天蓝
    (124, 179,  66),   # #7CB342 浅绿
    (183,  28,  28),   # #B71C1C 深红
    ( 21, 101, 192),   # #1565C0 深蓝
    ( 85, 107,  47),   # #556B2F 橄榄
    ( 49,  27, 146),   # #311B92 深靛
    (255, 160, 122),   # #FFA07A 浅鲑
    (218, 165,  32),   # #DAA520 金菊
    (192, 192, 192),   # #C0C0C0 银
    (233,  30,  99),   # #E91E63 玫红
]

EMPTY = 0
# 基础色槽：调色板第 2 个（索引 1）/ 蒙版值 2，与浮雕模型颜色直接关联
BASE_INDEX = 1
BASE_VALUE = BASE_INDEX + 1


def base_color() -> Tuple[int, int, int]:
    """当前基础色（调色板第 2 色，= 浮雕模型/图层颜色）。"""
    return PALETTE[BASE_INDEX]


def set_base_color(rgb) -> bool:
    """把浮雕模型基础色同步到调色板第 2 色（就地修改 PALETTE）。

    纯数据、零 Qt：返回是否发生变化，由 UI 层（PaintState）据此重绘色块。
    未填色格（EMPTY）渲染时本就用模型底色，此处让色块本身成为“模型基础色”，
    与图层改色联动。
    """
    if rgb is None:
        return False
    target = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
    if target == PALETTE[BASE_INDEX]:
        return False
    PALETTE[BASE_INDEX] = target
    return True


def equals_value_mask(color_mask, color_value: int):
    """色号蒙版 == color_value 的 bool 掩膜（EMPTY 与基础色同值）。

    未填色（0）即浮雕模型的基础颜色：当前颜色是基础色槽
    （``BASE_VALUE``）时未填色格一并命中；反之“颜色外”排除基础色时
    未填色格也被排除。color_mask 为 None 返回 None（调用方自行回退）。
    """
    if color_mask is None:
        return None
    v = int(color_value)
    m = (color_mask == v)
    if v == BASE_VALUE:
        m = m | (color_mask == EMPTY)
    return m


def count() -> int:
    """调色板颜色数量。"""
    return len(PALETTE)


def rgb_at(index: int) -> Tuple[int, int, int]:
    """第 index 个颜色的 (r, g, b)；越界回绕到有效范围。"""
    n = len(PALETTE)
    return PALETTE[int(index) % n]


def hex_at(index: int) -> str:
    """第 index 个颜色的 #rrggbb 十六进制串。"""
    r, g, b = rgb_at(index)
    return f"#{r:02x}{g:02x}{b:02x}"


def value_to_rgb(value: int) -> Optional[Tuple[int, int, int]]:
    """蒙版值 → (r, g, b)；``EMPTY``(0) 或越界返回 None（表示未填色）。"""
    v = int(value)
    if v <= EMPTY or v > len(PALETTE):
        return None
    return PALETTE[v - 1]


def rgb_to_value(rgb) -> int:
    """(r, g, b) → 蒙版值（调色板索引 + 1）；不在调色板中返回 ``EMPTY``。"""
    if rgb is None:
        return EMPTY
    target = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
    for i, c in enumerate(PALETTE):
        if c == target:
            return i + 1
    return EMPTY


def index_to_value(index: int) -> int:
    """调色板索引（0 基）→ 蒙版值（索引 + 1）。"""
    return int(index) + 1


def value_to_index(value: int) -> int:
    """蒙版值 → 调色板索引（0 基）；``EMPTY`` 或越界返回 -1。"""
    v = int(value)
    if v <= EMPTY or v > len(PALETTE):
        return -1
    return v - 1

# -*- coding: utf-8 -*-
"""按参考图风格重做曲线编辑 9 个图标：浅灰底 + 橙/深橙/黄橙两色造型。"""
import os

DIR = r"F:\softai\Cangai\cangai_relief\icons"

BG = '<rect x="1" y="1" width="22" height="22" rx="4" fill="#f0f0f0" stroke="#cfcfcf" stroke-width="1"/>\n'
ORANGE = "#f05820"
AMBER = "#f09018"
YELLOW = "#f0c018"
LIGHT = "#ffffff"

def svg(body):
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">\n'
            + BG + body + "</svg>\n")

icons = {}

# 1. 组合 join：上方深橙块(带半圆凹口) + 下方橙块(带半圆凸起)，咬合接缝
icons["join.svg"] = svg(
    '  <path d="M4 4 H20 V11.2 H15.5 A3.5 3.5 0 0 0 8.5 11.2 H4 Z" fill="' + AMBER + '"/>\n'
    '  <path d="M4 12.8 H8.5 A3.5 3.5 0 0 1 15.5 12.8 H20 V20 H4 Z" fill="' + ORANGE + '"/>\n'
)

# 2. 偏移 offset：曲线 + 平行偏移曲线 + 箭头
icons["offset.svg"] = svg(
    '  <path d="M3.5 16.5 C8 14, 9 11, 13.5 8 C16 6.5, 18 6.5, 20.5 5" fill="none" '
    'stroke="' + ORANGE + '" stroke-width="2.4" stroke-linecap="round"/>\n'
    '  <path d="M3.5 20 C8 17.5, 9 14.5, 13.5 11.5 C16 10, 18 10, 20.5 8.5" fill="none" '
    'stroke="' + AMBER + '" stroke-width="2.4" stroke-linecap="round"/>\n'
    '  <line x1="8" y1="12.6" x2="8" y2="16.3" stroke="' + YELLOW + '" stroke-width="2.2" stroke-linecap="round"/>\n'
    '  <polyline points="5.6,14.6 8,16.3 10.4,14.6" fill="none" stroke="' + YELLOW + '" '
    'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>\n'
)

# 3. 延伸 extend：曲线 + 末端外延箭头
icons["extend.svg"] = svg(
    '  <path d="M3.5 12 H14.5" stroke="' + ORANGE + '" stroke-width="2.4" stroke-linecap="round"/>\n'
    '  <circle cx="3.5" cy="12" r="2.1" fill="' + ORANGE + '" stroke="none"/>\n'
    '  <polyline points="15,9 20.5,12 15,15" fill="none" stroke="' + AMBER + '" '
    'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>\n'
)

# 4. 镜像 mirror：中轴虚线 + 两侧镜像三角
icons["mirror.svg"] = svg(
    '  <line x1="12" y1="2.5" x2="12" y2="21.5" stroke="' + YELLOW + '" stroke-width="2" stroke-dasharray="3,2.5"/>\n'
    '  <polygon points="9,7.5 3,17 9,17" fill="' + ORANGE + '" stroke="none"/>\n'
    '  <polygon points="15,7.5 21,17 15,17" fill="' + AMBER + '" stroke="none"/>\n'
)

# 5. 倒角 chamfer：直角角点被切平，虚线示意原始角
icons["chamfer.svg"] = svg(
    '  <path d="M4 20 L4 9 L16 9" fill="none" stroke="' + YELLOW + '" stroke-width="1.4" stroke-dasharray="3,2"/>\n'
    '  <path d="M4 20 L4 13.5 L9 9 L16 9" fill="none" stroke="' + ORANGE + '" '
    'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>\n'
    '  <circle cx="9" cy="9" r="1.6" fill="' + AMBER + '" stroke="none"/>\n'
)

# 6. 阵列 array：3x2 方块矩阵（右列深橙）
def _sq(x, y, color):
    return ('  <rect x="' + str(x) + '" y="' + str(y) + '" width="5.6" height="6.2" rx="1.2" fill="' + color + '" stroke="none"/>\n'
            '  <rect x="' + str(x + 0.7) + '" y="' + str(y + 0.7) + '" width="1.5" height="4.8" rx="0.75" fill="' + LIGHT + '" opacity="0.45"/>\n')
icons["array.svg"] = svg(
    _sq(3.2, 4, ORANGE) + _sq(9.2, 4, ORANGE) + _sq(15.2, 4, AMBER)
    + _sq(3.2, 12.5, ORANGE) + _sq(9.2, 12.5, ORANGE) + _sq(15.2, 12.5, AMBER)
)

# 7. 群组 group：双圆重叠（橙 + 深橙）
icons["group.svg"] = svg(
    '  <circle cx="9.5" cy="12" r="6.3" fill="' + ORANGE + '" stroke="none"/>\n'
    '  <circle cx="14.5" cy="12" r="6.3" fill="' + AMBER + '" stroke="none"/>\n'
    '  <circle cx="9.5" cy="12" r="6.3" fill="none" stroke="' + LIGHT + '" stroke-width="0.8" opacity="0.5"/>\n'
)

# 8. 修剪 trim：剪刀（交叉刀片 + 双环柄）
icons["trim.svg"] = svg(
    '  <path d="M5 4.5 L12 12 L16.5 18.5" fill="none" stroke="' + ORANGE + '" '
    'stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"/>\n'
    '  <path d="M19 4.5 L12 12 L7.5 18.5" fill="none" stroke="' + ORANGE + '" '
    'stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"/>\n'
    '  <circle cx="12" cy="12" r="1.5" fill="' + YELLOW + '" stroke="none"/>\n'
    '  <ellipse cx="17.2" cy="20.2" rx="2.6" ry="3.1" fill="' + AMBER + '" stroke="none"/>\n'
    '  <ellipse cx="6.8" cy="20.2" rx="2.6" ry="3.1" fill="' + AMBER + '" stroke="none"/>\n'
)

# 9. 闭合 closepath：开口圆弧 + 虚线弦 + 闭合箭头
icons["closepath.svg"] = svg(
    '  <path d="M4 12 A8 8 0 0 1 20 12" fill="none" stroke="' + ORANGE + '" '
    'stroke-width="2.3" stroke-linecap="round"/>\n'
    '  <line x1="4" y1="12" x2="20" y2="12" stroke="' + YELLOW + '" stroke-width="2" stroke-dasharray="3,2.5"/>\n'
    '  <polyline points="15.5,9.5 20.5,12 15.5,14.5" fill="none" stroke="' + AMBER + '" '
    'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>\n'
)

for name, body in icons.items():
    with open(os.path.join(DIR, name), "w", encoding="utf-8", newline="\n") as f:
        f.write(body)
    print("WROTE", name)

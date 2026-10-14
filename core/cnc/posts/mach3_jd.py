"""Mach3 三轴后置文件（精雕 JD50 / ArtCAM 风格）—— Cangai 默认后处理。

规则提取自 模板1.NC（真实机床输出）与 Mach3三轴后处理.epst（ArtCAM 定义），
逐项核验清单见 docs/CNC路径输出后处理体系设计.md 第 5.1 节：

- 程序头尾：`%` ... `M09` `M05` `G0 X0 Y0` `M30` `%`
- 主轴 S 在前：`S16000 M03`；冷却两位：`M08`/`M09`
- 快移/走刀两位码：`G00`/`G01`
- 坐标 3 位删尾零（Y4.840→Y4.84）；进给 1 位保留 .0（F300.0）
- 圆弧：`G17` + G02/G03 + I/J/K 增量圆心 3 位
- 模态省略 G 码/XYZ/F；换刀后强制全量；CRLF 行尾
"""
from __future__ import annotations

from .base import EnhancedGcodePost


class Mach3Post(EnhancedGcodePost):
    name = "Mach3 三轴 (精雕 JD50)"
    description = "ArtCAM 风格 · 默认机床"
    controller = "mach3"

    # ---- 数字格式（ArtCAM 双格式器）----
    coord_decimals = 3
    feed_decimals = 1
    arc_center_decimals = 3
    coord_strip_zeros = True
    feed_strip_zeros = False      # F300.0 保留 .0

    # ---- 指令方言 ----
    rapid_code = "G00"
    feed_code = "G01"
    arc_cw_code = "G02"
    arc_ccw_code = "G03"
    arc_plane_code = "G17"
    spindle_on = "S{rpm} M03"          # S 在前（模板 S16000 M03）
    spindle_off = "M05"
    coolant_on = "M08"
    coolant_off = "M09"
    # 精雕机（ArtCAM 方言）只认 G04 X 暂停；标准 Mach3 的 G04 P 会停/报警。
    # 与 Mach3三轴后处理.epst 的 DwellS（prefix="G04 X"）完全一致。
    dwell_code = "G04 X{dwell}"

    # ---- 程序宏 ----
    # 程序尾不含 M09/M05：由 flush_unclosed 按 IR 状态补发（避免重复），
    # 模板尾部顺序 M09 → M05 → G0 X0 Y0 → M30 → % 由此严格保证。
    # 模板全文无注释：Mach3 输出不携带刀路名/调试注释。
    program_start = "%"
    program_end = "G0 X0 Y0\nM30\n%"
    tool_change = "T{tool_number} M6"
    header_comments = ()
    emit_comments = False

    # ---- 模态 ----
    modal_g = True
    modal_xyz = True
    modal_f = True
    suppress_final_home = True    # 末条 RAPID X0 Y0 交给 program_end 的 G0 X0 Y0
    flush_unclosed = True

    line_ending = "\r\n"

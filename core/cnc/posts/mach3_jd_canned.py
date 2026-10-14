"""Mach3 三轴后置（精雕 JD50 方言 + 钻孔固定循环）—— 复刻 JDNcPost 固定循环输出。

与 :class:`Mach3Post` 的方言完全一致（程序头尾 / 两位码 / IJK 增量圆弧 /
CRLF / 模态省略），唯一区别：``canned_cycles = True``——把钻孔序列
（RAPID XY → RAPID Z(R) → FEED Z(孔深) → RAPID Z(回退)，连续同模式多孔）
压缩为 Fanoc 风格固定循环：

- 简单钻孔 → ``G98 G81 X.. Y.. Z.. R.. F..``
- 啄钻（多级进给、每级全回退到 R）→ ``G98 G83 X.. Y.. Z.. R.. Q.. F..``
- 后续同 R/同孔深的孔只发 ``X.. Y..``（模态重复，控制器自动再钻）
- 循环后接非重复段 / 程序尾 → ``G80`` 取消

保守约束（不压缩，按原样逐段输出）：无 R 平面、分段回退（chip break，
G83 语义不符）、孔底 DWELL、R 平面或孔深变化。
"""
from __future__ import annotations

from .base import EnhancedGcodePost
from .mach3_jd import Mach3Post


class Mach3CannedPost(Mach3Post):
    name = "Mach3 三轴 (精雕 JD50·固定循环)"
    description = "ArtCAM 风格 + 钻孔 G81/G83 固定循环"
    controller = "mach3_canned"

    canned_cycles = True

"""Qt 路径工具：把密集采样点用非均匀(centripetal) Catmull-Rom→三次贝塞尔平滑连接。

样条曲线在文档/预览里原本是逐点 lineTo 的直弦折线，拐弯处（曲率大）
均匀采样的直弦会露出明显棱面。用 Catmull-Rom 精确过每个采样点、以
C1 连续的切向作贝塞尔控制柄，可消除折线接缝的棱角 → 显示圆润。

切向必须**按参数间距加权**（非均匀 Catmull-Rom），否则在采样点疏密
突变处（样条每跨固定采样数 → 跨度长的点稀、跨度短的点密，拐点尤甚）
均匀权重的切向会估偏、产生过冲"小波浪"。用 centripetal 参数化
（alpha=0.5，knot 间隔 = 点距^0.5）进一步抑制尖点/自交。
"""
from __future__ import annotations
import math
from typing import List, Sequence, Tuple
from PySide6.QtGui import QPainterPath


def appendSmooth(path: QPainterPath, pts: Sequence[Tuple[float, float]],
                 move: bool = True, alpha: float = 0.5) -> None:
    """把 (x, y) 点列以非均匀 Catmull-Rom 平滑追加到 path。

    move=True 时先 moveTo 首点；点数 0/1/2 退化为空/不动/直线，
    保证直线橡皮筋等不受影响。曲线精确经过每个采样点（插值）。"""
    n = len(pts)
    if n == 0:
        return
    if move:
        path.moveTo(pts[0][0], pts[0][1])
    if n == 1:
        return
    if n == 2:
        path.lineTo(pts[1][0], pts[1][1])
        return

    # centripetal 参数节点：间隔 = 相邻点距^alpha（alpha=0.5 抗过冲/尖点）
    t: List[float] = [0.0] * n
    for i in range(1, n):
        d = math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1])
        t[i] = t[i - 1] + (max(d, 1e-12) ** alpha)

    def secant(i: int, j: int) -> Tuple[float, float]:
        dt = t[j] - t[i]
        if dt < 1e-12:
            dt = 1e-12
        return ((pts[j][0] - pts[i][0]) / dt, (pts[j][1] - pts[i][1]) / dt)

    # 每个节点的切向：非均匀 Catmull-Rom = 两侧割线按参数间隔加权平均；
    # 端点取单侧割线（避免均匀方案里端点复制导致的切向减半/接缝折角）
    m: List[Tuple[float, float]] = [(0.0, 0.0)] * n
    m[0] = secant(0, 1)
    m[n - 1] = secant(n - 2, n - 1)
    for i in range(1, n - 1):
        sp = secant(i - 1, i)          # 前向割线
        sn = secant(i, i + 1)          # 后向割线
        dtp = t[i] - t[i - 1]
        dtn = t[i + 1] - t[i]
        w = dtp + dtn
        if w < 1e-12:
            m[i] = sn
        else:
            m[i] = ((sp[0] * dtn + sn[0] * dtp) / w,
                    (sp[1] * dtn + sn[1] * dtp) / w)

    # Hermite → 三次贝塞尔：控制柄 = 切向 × 间隔 / 3
    for i in range(n - 1):
        dt = t[i + 1] - t[i]
        p1, p2 = pts[i], pts[i + 1]
        b1x = p1[0] + m[i][0] * dt / 3.0
        b1y = p1[1] + m[i][1] * dt / 3.0
        b2x = p2[0] - m[i + 1][0] * dt / 3.0
        b2y = p2[1] - m[i + 1][1] * dt / 3.0
        path.cubicTo(b1x, b1y, b2x, b2y, p2[0], p2[1])


def smoothPath(pts: Sequence[Tuple[float, float]]) -> QPainterPath:
    """新建一条平滑路径。"""
    path = QPainterPath()
    appendSmooth(path, pts, move=True)
    return path

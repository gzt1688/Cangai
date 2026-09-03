# -*- coding: utf-8 -*-
"""偏移侧别稳定性测试：sideOfPoint 内外判定 + OffsetCommand 锁定方向不乱跳。

回归背景：旧实现对闭合曲线用“最近段法线点积”判侧别，非凸图形上最近段
在两条符号相反的段之间跳变（且闭合分支投影不钳制），导致预览偏移线
在图形内外乱跑。修复后闭合曲线用射线法内点测试，符号只由“在内/在外”
与绕向决定，幅值为到边界的真实距离（投影钳制）。
"""
from __future__ import annotations
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

from core.vector.geometry_types import Point3D
from core.vector.offset_geometry import sideOfPoint
from ui.windows.mainWindow import MainWindow


def _square(ccw: bool, x0=0.0, y0=0.0, s=100.0):
    pts = [Point3D(x0, y0, 0), Point3D(x0 + s, y0, 0),
           Point3D(x0 + s, y0 + s, 0), Point3D(x0, y0 + s, 0)]
    return pts if ccw else list(reversed(pts))


def _lShape():
    """L 形多边形（CCW，逆时针），右侧有一个凹口：凹口区域在图形外。"""
    return [Point3D(0, 0, 0), Point3D(100, 0, 0), Point3D(100, 40, 0),
            Point3D(40, 40, 0), Point3D(40, 100, 0), Point3D(0, 100, 0)]


def run():
    QApplication(sys.argv)
    results = []

    def check(name, ok):
        results.append(ok)
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")

    # 1) 方形（凸）：内部点全部同号，外部点全部反号；两种绕向都对
    for ccw in (True, False):
        pts = _square(ccw)
        insideSigns = [sideOfPoint(pts, Point3D(x, y, 0), True)
                       for x, y in ((50, 50), (20, 80), (80, 20), (50, 90))]
        outsideSigns = [sideOfPoint(pts, Point3D(x, y, 0), True)
                        for x, y in ((-10, 50), (110, 50), (50, -10), (50, 110))]
        innerPos = all(s > 0 for s in insideSigns)
        check(f"方形内点同号且为正（ccw={ccw}）",
              (innerPos if ccw else all(s < 0 for s in insideSigns)))
        check(f"方形外点全反号（ccw={ccw}）",
              (all(s < 0 for s in outsideSigns) if ccw
               else all(s > 0 for s in outsideSigns)))

    # 2) 幅值 = 到边界真实距离：方形内 (50,50) 距边 50
    d = sideOfPoint(_square(True), Point3D(50, 50, 0), True)
    check("幅值为边界距离", abs(abs(d) - 50.0) < 1e-6)

    # 3) 非凸 L 形：凹口区（图形外）任何位置都得“外”符号，不随最近段跳变
    lpts = _lShape()
    notchSigns = [sideOfPoint(lpts, Point3D(x, y, 0), True)
                  for x, y in ((70, 70), (55, 45), (90, 90), (70, 42),
                               (45, 70), (99, 41))]
    bodySigns = [sideOfPoint(lpts, Point3D(x, y, 0), True)
                 for x, y in ((20, 20), (20, 80), (60, 20), (30, 50))]
    check("L形凹口（外）符号与体外一致", all(s < 0 for s in notchSigns)
          and all(s > 0 for s in bodySigns))
    # 凹口内紧贴内角与紧贴外边的符号必须一致（旧实现此处跳变）
    near1 = sideOfPoint(lpts, Point3D(42, 68, 0), True)   # 贴竖直内边
    near2 = sideOfPoint(lpts, Point3D(72, 42, 0), True)   # 贴水平内边
    check("凹口内贴边两点符号一致", near1 < 0 and near2 < 0)

    # 4) 重复末点（首尾相接的采样）不影响判定
    closed = _square(True) + [Point3D(0, 0, 0)]
    check("重复末点兼容",
          sideOfPoint(closed, Point3D(50, 50, 0), True) > 0
          and sideOfPoint(closed, Point3D(-5, 50, 0), True) < 0)

    # 5) 命令级：OffsetCommand 锁定方向——穿越边界才翻转，
    #    边界附近小幅抖动（幅值<翻转阈值）不翻转（阈值随视图缩放变化，取相对值）
    app = QApplication.instance()
    w = MainWindow()
    cm = w._cm
    from core.vector.line_object import LineObject
    rect = LineObject([Point3D(0, 0, 0), Point3D(100, 0, 0),
                       Point3D(100, 100, 0), Point3D(0, 100, 0)])
    rect.setClosed(True)
    w._doc.addObject(rect)
    rect.setSelected(True)
    cm.executeCommand("Offset")
    check("Offset 命令启动", cm.hasActive())

    cmd = cm.activeInstance()
    thr = cmd._worldPickRadius() * 0.5   # 与 _updateLockedSide 相同的翻转阈值
    # 从图形中心（内）出发
    cmd.onMouseMove(Point3D(50, 50, 0), Qt.NoModifier)
    sideLocked0 = cmd._lockedSide
    check("初始锁定为内侧", sideLocked0 != 0)
    # 外侧小幅抖动：距边界 thr*0.5 ↔ thr*0.1 来回 20 次（均低于阈值，不翻转）
    flipped = False
    for i in range(20):
        d = thr * 0.5 if i % 2 == 0 else thr * 0.1
        cmd.onMouseMove(Point3D(50, 100 + d, 0), Qt.NoModifier)
        if cmd._lockedSide != sideLocked0:
            flipped = True
            break
    check("边界附近抖动不翻转", not flipped)
    # 明确移到另一侧深处（>阈值）→ 翻转一次
    cmd.onMouseMove(Point3D(50, 100 + thr * 3, 0), Qt.NoModifier)
    check("穿越边界后翻转", cmd._lockedSide != sideLocked0)
    # 抖动不再翻回
    stable = True
    for i in range(20):
        d = thr * 0.5 if i % 2 == 0 else thr * 0.1
        cmd.onMouseMove(Point3D(50, 100 + d, 0), Qt.NoModifier)
        if cmd._lockedSide == sideLocked0:
            stable = False
            break
    check("翻转后小幅抖动不回翻", stable)

    cm.cancelActive()

    # 6) 端到端语义：圆内点击 → 生成向内偏移（半径变小）；圆外点击 → 向外
    from core.vector.circle_object import CircleObject
    circ = CircleObject(0, 0, 0, 50)
    w._doc.addObject(circ)
    circ.setSelected(True)
    cm.executeCommand("Offset")
    cmd = cm.activeInstance()
    if cmd._panel is None:
        cmd.paramPanel()          # 确保面板存在（距离来源）
    cmd._panel._spinDist.setValue(8.0)   # 面板距离 8
    cmd.onMouseMove(Point3D(0, 0, 0), Qt.NoModifier)   # 圆心 = 内侧
    cmd.onMousePress(Point3D(0, 0, 0), Qt.NoModifier)  # 点击执行
    circles = [o for o in w._doc.objects() if o.type().name == "CIRCLE"
               and o is not circ]
    check("圆内点击生成向内偏移", len(circles) == 1
          and abs(circles[0].radius() - 42.0) < 1e-6)

    ok = all(results)
    print(f"=== {'全部通过' if ok else '存在失败'} ===")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)

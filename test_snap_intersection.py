# -*- coding: utf-8 -*-
"""交点捕捉回归测试：圆×样条等此前未支持组合的交点捕捉。

回归背景：_intersectPair 只实现了 直线×直线 / 直线×圆(弧) / 圆(弧)×圆(弧)
的解析求交，圆×样条、样条×样条等组合直接返回空——交点捕捉在圆与样条
相交处无任何提示，用户据此判断"系统没算圆与样条的交点"。修复后其余
组合回退到双方显示采样折线的求交（含鼠标附近弦预过滤控制耗时）。
"""
from __future__ import annotations
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication

from core.vector.geometry_types import Point3D
from core.vector.circle_object import CircleObject
from core.vector.curve_object import CurveObject
from core.vector.poly_curve_object import PolyCurveObject
from core.vector.ellipse_object import EllipseObject
from core.vector.line_object import LineObject
from core.snap.snap_manager import SnapManager, SnapType
from core.spline_utils import catmullRom


def _smoothCurveCrossings(curve, cx, cy, r):
    """平滑样条（输入点高密度采样）与圆的交点。"""
    smooth = catmullRom(curve.inputPoints(), 2000)
    inters = []
    prev, prev_d = smooth[0], math.hypot(smooth[0].x - cx,
                                         smooth[0].y - cy) - r
    for p in smooth[1:]:
        d = math.hypot(p.x - cx, p.y - cy) - r
        if (prev_d < 0) != (d < 0):
            t = abs(prev_d) / (abs(prev_d) + abs(d))
            inters.append(Point3D(prev.x + (p.x - prev.x) * t,
                                  prev.y + (p.y - prev.y) * t, 0))
        prev, prev_d = p, d
    return inters


def _ellNorm(p, ell):
    """点相对椭圆的无量纲半径比（=1 即在椭圆上）。"""
    c = ell.center()
    return math.hypot((p.x - c.x) / ell.majorAxis(),
                      (p.y - c.y) / ell.minorAxis())


def run():
    QApplication(sys.argv)
    results = []

    def check(name, ok):
        results.append(ok)
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")

    sm = SnapManager.instance()
    sm.setEnabledTypes({SnapType.INTERSECTION})

    def snapBest(objs, wp, radius=12.0):
        r = sm.snap(wp, objs, radius)
        return r if (r.isValid and r.type == SnapType.INTERSECTION) else None

    # 1) 圆 × 样条曲线
    circ = CircleObject(0, 0, 0, 50)
    curve = CurveObject()
    curve.buildFromPoints([Point3D(-120, -40, 0), Point3D(-60, 30, 0),
                           Point3D(20, -35, 0), Point3D(100, 25, 0)])
    trues = _smoothCurveCrossings(curve, 0, 0, 50)
    check("圆与样条存在两个交点", len(trues) == 2)
    r1 = snapBest([circ, curve], Point3D(trues[0].x + 2, trues[0].y - 2, 0))
    check("圆×样条交点捕捉命中", r1 is not None)
    if r1 is not None:
        d = min(r1.position.distanceTo(q) for q in trues)
        check("圆×样条捕捉位置准确（≤0.05mm）", d <= 0.05)

    # 2) 圆 × 多段曲线（PolyCurve 直线段穿过）
    poly = PolyCurveObject()
    poly.addSegment({"points": [[-80, 5, 0], [80, -5, 0]]})
    dx, dy = 160.0, -10.0
    A = dx * dx + dy * dy
    B = 2 * (-80 * dx + 5 * dy)
    C = 80 * 80 + 25 - 2500
    disc = math.sqrt(B * B - 4 * A * C)
    trues2 = [Point3D(-80 + t * dx, 5 + t * dy, 0)
              for t in ((-B - disc) / (2 * A), (-B + disc) / (2 * A))]
    r2 = snapBest([circ, poly], Point3D(trues2[0].x, trues2[0].y + 3, 0))
    check("圆×多段曲线交点捕捉命中", r2 is not None)
    if r2 is not None:
        d = min(r2.position.distanceTo(q) for q in trues2)
        check("圆×多段曲线捕捉位置准确（≤0.05mm）", d <= 0.05)

    # 3) 椭圆 × 样条（走采样回退）
    ell = EllipseObject(500, 0, 0, 60, 40)
    curve2 = CurveObject()
    curve2.buildFromPoints([Point3D(420, -30, 0), Point3D(500, 30, 0),
                            Point3D(580, -25, 0)])
    smooth2 = catmullRom(curve2.inputPoints(), 2000)
    trues3 = []
    prev, prev_d = smooth2[0], _ellNorm(smooth2[0], ell) - 1.0
    for p in smooth2[1:]:
        d = _ellNorm(p, ell) - 1.0
        if (prev_d < 0) != (d < 0):
            t = abs(prev_d) / (abs(prev_d) + abs(d))
            trues3.append(Point3D(prev.x + (p.x - prev.x) * t,
                                  prev.y + (p.y - prev.y) * t, 0))
        prev, prev_d = p, d
    check("椭圆与样条存在交点", len(trues3) >= 2)
    r3 = snapBest([ell, curve2],
                  Point3D(trues3[0].x + 1.5, trues3[0].y - 1.5, 0))
    check("椭圆×样条交点捕捉命中", r3 is not None)
    if r3 is not None:
        d = min(r3.position.distanceTo(q) for q in trues3)
        check("椭圆×样条捕捉位置准确（≤0.05mm）", d <= 0.05)

    # 4) 直线 × 圆（原有解析路径不受影响）
    ln = LineObject([Point3D(-60, 30, 0), Point3D(60, 30, 0)])
    r4 = snapBest([circ, ln], Point3D(40, 30, 0))
    check("直线×圆解析交点捕捉仍有效", r4 is not None
          and r4.position.distanceTo(Point3D(40, 30, 0)) <= 0.05)

    ok = all(results)
    print(f"=== {'全部通过' if ok else '存在失败'} ===")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)

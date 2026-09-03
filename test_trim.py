# -*- coding: utf-8 -*-
"""修剪命令回归测试：闭合曲线（圆）跨接缝环绕区间修剪。

回归背景：快速修剪按“线性累计长度”找点击所在交点区间，闭合曲线点击
落在首末交点之外的环绕区间（跨折线接缝）时退化为按折线端点裁剪，
导致修剪结果越过交点“长出”一段（用户实测：圆+曲线穿过，修剪后圆
不在交点位置停住）。修复后环绕区间剪掉跨缝段，保留首末交点之间的整段。
"""
from __future__ import annotations
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt

from core.vector.geometry_types import Point3D
from core.vector.curve_object import CurveObject
from core.spline_utils import catmullRom
from ui.windows.mainWindow import MainWindow


def _piecePoints(obj):
    """PolyCurve 对象的各段点列。"""
    return [[Point3D(*p) for p in seg.get("points", [])]
            for seg in obj.segments()]


def _minDist(pt, polys):
    return min(min(pt.distanceTo(q) for q in poly) for poly in polys)


def _trueCircleCurveIntersections(curve, r, cx=0.0, cy=0.0):
    """平滑曲线（输入点高密度采样）与圆的精确交点。"""
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


def _lineCircleIntersections(p1, p2, cx, cy, r):
    """直线与圆的精确交点（解析）。"""
    dx, dy = p2.x - p1.x, p2.y - p1.y
    fx, fy = p1.x - cx, p1.y - cy
    a = dx * dx + dy * dy
    bq = 2 * (fx * dx + fy * dy)
    cq = fx * fx + fy * fy - r * r
    disc = math.sqrt(bq * bq - 4 * a * cq)
    out = []
    for t in ((-bq + disc) / (2 * a), (-bq - disc) / (2 * a)):
        out.append(Point3D(p1.x + t * dx, p1.y + t * dy, 0))
    return out


def run():
    QApplication(sys.argv)
    results = []

    def check(name, ok):
        results.append(ok)
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")

    w = MainWindow()
    w.show()
    cm = w._cm
    doc = w._doc

    # ---------- 场景 A：用户流程（圆 + 曲线穿过，点击圆下方弧修剪） ----------
    cm.executeCommand("Circle")
    cm.onMousePress(Point3D(0, 0, 0), Qt.NoModifier)
    cm.onMousePress(Point3D(50, 0, 0), Qt.NoModifier)
    circ = next(o for o in doc.objects() if o.type().name == "CIRCLE")

    cm.executeCommand("Curve")
    cm.onMousePress(Point3D(-120, -40, 0), Qt.NoModifier)
    cm.onMousePress(Point3D(-60, 30, 0), Qt.NoModifier)
    cm.onMousePress(Point3D(20, -35, 0), Qt.NoModifier)
    cm.onMousePress(Point3D(100, 25, 0), Qt.NoModifier)
    cm.onRightClick(Point3D())
    cm.onRightClick(Point3D())
    curve = next(o for o in doc.objects() if o.type().name == "CURVE")

    trueA = _trueCircleCurveIntersections(curve, 50)
    check("场景A有两个交点", len(trueA) == 2)

    before = set(id(o) for o in doc.objects())
    circ.setSelected(True)
    cm.executeCommand("Trim")
    cm.activeInstance().onMousePress(Point3D(0, -50, 0), Qt.NoModifier)
    piecesA = []
    for o in doc.objects():
        if id(o) not in before:
            piecesA.extend(_piecePoints(o))
    check("场景A修剪产生一段保留弧", len(piecesA) == 1)
    if piecesA and len(trueA) == 2:
        poly = piecesA[0]
        d1 = min(poly[0].distanceTo(q) for q in trueA)
        d2 = min(poly[-1].distanceTo(q) for q in trueA)
        check("场景A两端落在交点上（≤0.05mm）", d1 <= 0.05 and d2 <= 0.05)
        check("场景A点击的弧已被剪掉",
              _minDist(Point3D(0, -50, 0), piecesA) > 5.0)
    cm.cancelActive()

    # ---------- 场景 B/C：直线穿过圆，分别点击上弧 / 下弧 ----------
    from core.vector.circle_object import CircleObject
    from core.vector.line_object import LineObject

    def setupCircleLine(cx, lineP1, lineP2):
        c = CircleObject(cx, 0, 0, 50)
        doc.addObject(c)
        ln = LineObject([lineP1, lineP2])
        doc.addObject(ln)
        return c, ln

    def trimAndCollect(target, click):
        before = set(id(o) for o in doc.objects())
        target.setSelected(True)
        cm.executeCommand("Trim")
        cm.activeInstance().onMousePress(click, Qt.NoModifier)
        pieces = []
        for o in doc.objects():
            if id(o) not in before and o.type().name == "POLYCURVE":
                pieces.extend(_piecePoints(o))
        cm.cancelActive()
        return pieces

    # B：点击上弧（90°）→ 环绕区间（跨接缝）被剪，保留唯一一段下弧
    cB, lnB = setupCircleLine(300, Point3D(200, 20, 0), Point3D(400, -20, 0))
    trueB = _lineCircleIntersections(Point3D(200, 20, 0),
                                     Point3D(400, -20, 0), 300, 0, 50)
    check("场景B直线与圆有两交点", len(trueB) == 2)
    piecesB = trimAndCollect(cB, Point3D(300, 50, 0))
    check("场景B保留一段弧", len(piecesB) == 1)
    if piecesB:
        check("场景B点击的上弧已被剪掉",
              _minDist(Point3D(300, 50, 0), piecesB) > 5.0)
        d1 = min(piecesB[0][0].distanceTo(q) for q in trueB)
        d2 = min(piecesB[0][-1].distanceTo(q) for q in trueB)
        check("场景B两端落在交点上（≤0.05mm）", d1 <= 0.05 and d2 <= 0.05)

    # C：点击下弧（270°，普通中段区间）→ 保留两段，点击弧被剪掉
    cC, lnC = setupCircleLine(600, Point3D(500, 20, 0), Point3D(700, -20, 0))
    trueC = _lineCircleIntersections(Point3D(500, 20, 0),
                                     Point3D(700, -20, 0), 600, 0, 50)
    piecesC = trimAndCollect(cC, Point3D(600, -50, 0))
    check("场景C保留两段弧", len(piecesC) == 2)
    if len(piecesC) == 2:
        check("场景C点击的下弧已被剪掉",
              _minDist(Point3D(600, -50, 0), piecesC) > 5.0)
        # 每段内侧端贴交点，外侧端是圆折线原始接缝端点 (650,0)
        seam = Point3D(650, 0, 0)
        devs = [min(piecesC[0][-1].distanceTo(q) for q in trueC),
                min(piecesC[1][0].distanceTo(q) for q in trueC),
                piecesC[0][0].distanceTo(seam),
                piecesC[1][-1].distanceTo(seam)]
        check("场景C交点端≤0.05mm、外端为原始端点",
              all(d <= 0.05 for d in devs))

    # ---------- 场景 D（用户两步流程）：先剪曲线左尾，再剪圆下半 ----------
    # 回归：曲线左尾被剪后其起点恰落在圆上（触碰式交点），浮点参数微越界
    # 被漏判 → 圆只剩一个交点 → 环绕修剪退化成整圆消失。
    from core.vector.circle_object import CircleObject
    circD = CircleObject(1300, 0, 0, 50)
    doc.addObject(circD)
    curveD = CurveObject()
    curveD.buildFromPoints([Point3D(1180, -40, 0), Point3D(1245, 25, 0),
                            Point3D(1355, 20, 0), Point3D(1440, -30, 0)])
    doc.addObject(curveD)
    trueD = _trueCircleCurveIntersections(curveD, 50, 1300, 0)
    check("场景D曲线与圆有两交点", len(trueD) == 2)

    # 第一步：点击曲线左尾（左交点之前）→ 左尾被剪
    before1 = set(id(o) for o in doc.objects())
    curveD.setSelected(True)
    cm.executeCommand("Trim")
    cm.activeInstance().onMousePress(Point3D(1205, -15, 0), Qt.NoModifier)
    newCurves = [o for o in doc.objects() if id(o) not in before1
                 and o.type().name == "POLYCURVE"]
    check("场景D曲线左尾已剪", len(newCurves) == 1)
    cm.cancelActive()

    # 第二步：点击圆下半（270°）→ 应只剪掉下弧，保留完整上弧
    before2 = set(id(o) for o in doc.objects())
    circD.setSelected(True)
    cm.executeCommand("Trim")
    cm.activeInstance().onMousePress(Point3D(1300, -50, 0), Qt.NoModifier)
    piecesD = []
    for o in doc.objects():
        if id(o) not in before2 and o.type().name == "POLYCURVE":
            piecesD.extend(_piecePoints(o))
    check("场景D圆修剪产生一段保留弧", len(piecesD) == 1)
    if piecesD:
        arcLen = sum(piecesD[0][i].distanceTo(piecesD[0][i + 1])
                     for i in range(len(piecesD[0]) - 1))
        check("场景D保留的是完整上弧（弧长>100，非残片）", arcLen > 100.0)
        check("场景D点击的下弧已被剪掉",
              _minDist(Point3D(1300, -50, 0), piecesD) > 5.0)
        d1 = min(piecesD[0][0].distanceTo(q) for q in trueD)
        d2 = min(piecesD[0][-1].distanceTo(q) for q in trueD)
        check("场景D两端落在交点上（≤0.05mm）", d1 <= 0.05 and d2 <= 0.05)
    cm.cancelActive()

    # ---------- 场景 E：交点紧贴圆的折线顶点 → 必须剪在真实交点上 ----------
    # 直线在圆最高折线顶点 (1700,50) 下方 0.02mm 处穿过，两真实交点在顶点
    # 两侧各约 1.41mm。若交点被钳制到顶点，切点会落在 (1700,50) 上。
    circE = CircleObject(1700, 0, 0, 50)
    doc.addObject(circE)
    lineE = LineObject([Point3D(1600, 49.98, 0), Point3D(1800, 49.98, 0)])
    doc.addObject(lineE)
    trueE = _lineCircleIntersections(Point3D(1600, 49.98, 0),
                                     Point3D(1800, 49.98, 0), 1700, 0, 50)
    check("场景E直线与圆有两交点", len(trueE) == 2)
    piecesE = trimAndCollect(circE, Point3D(1700, -50, 0))
    check("场景E保留顶部小弧", len(piecesE) == 1)
    if piecesE:
        poly = piecesE[0]
        d1 = min(poly[0].distanceTo(q) for q in trueE)
        d2 = min(poly[-1].distanceTo(q) for q in trueE)
        check("场景E两端落在真实交点上（≤0.02mm，非顶点）",
              d1 <= 0.02 and d2 <= 0.02)
        arcLen = sum(poly[i].distanceTo(poly[i + 1])
                     for i in range(len(poly) - 1))
        check("场景E保留弧长为真实小冠（2.6~3.2）", 2.6 <= arcLen <= 3.2)

    # ---------- 场景 F：矩形 + 直线穿两条边（收尾边交点识别） ----------
    # 回归：矩形点列首尾不重复存储（4 角点），段遍历 range(len-1) 漏掉
    # 收尾边（末点→首点），那条边的交点永远识别不到。
    from core.vector.circle_object import CircleObject
    cm.executeCommand("Rectangle")
    cm.onMousePress(Point3D(2000, 0, 0), Qt.NoModifier)
    cm.onMousePress(Point3D(2100, 100, 0), Qt.NoModifier)
    cm.cancelActive()   # 矩形已入文档，结束绘制命令
    rect = next(o for o in doc.objects() if o.type().name == "LINE"
                and o.isClosed() and len(o.points()) == 4)
    # 交点在左（收尾）边 (2000,40) 与右 (2100,60)
    lnF = LineObject([Point3D(1950, 30, 0), Point3D(2150, 70, 0)])
    doc.addObject(lnF)
    trueF = [Point3D(2000, 40, 0), Point3D(2100, 60, 0)]

    # F1: 交点捕捉在收尾边交点处有提示
    from core.snap.snap_manager import SnapManager, SnapType as ST
    sm = SnapManager.instance()
    sm.setEnabledTypes({ST.INTERSECTION})
    rs = sm.snap(Point3D(2000, 40, 0), [rect, lnF], 12.0)
    check("场景F收尾边交点捕捉命中",
          rs.isValid and rs.type == ST.INTERSECTION
          and rs.position.distanceTo(Point3D(2000, 40, 0)) <= 0.05)

    # F2: 点击收尾边（左边缘上段）→ 剪掉含点击的下弧，保留两段
    # （矩形是 LineObject：第一段原地更新点列，其余段新建 PolyCurve 对象）
    beforeF = set(id(o) for o in doc.objects())
    rect.setSelected(True)
    cm.executeCommand("Trim")
    cm.activeInstance().onMousePress(Point3D(2000, 66, 0), Qt.NoModifier)
    piecesF = [list(rect.points())]
    for o in doc.objects():
        if id(o) not in beforeF and o.type().name == "POLYCURVE":
            piecesF.extend(_piecePoints(o))
    check("场景F矩形修剪产生保留段", len(piecesF) == 2)
    if len(piecesF) == 2:
        # 内侧切点（紧邻被剪段的端点）必须落在交点上
        inner = [piecesF[0][-1], piecesF[1][0]]
        devs = [min(e.distanceTo(q) for q in trueF) for e in inner]
        check("场景F所有切点落在交点上（≤0.05mm）",
              all(d <= 0.05 for d in devs))
        # 点击位置 (2000,66) 位于被剪掉的左边缘段上
        ends = [e for poly in piecesF for e in (poly[0], poly[-1])]
        check("场景F点击侧已被剪掉",
              min(e.distanceTo(Point3D(2000, 66, 0)) for e in ends) > 5.0)
    cm.cancelActive()

    ok = all(results)
    print(f"=== {'全部通过' if ok else '存在失败'} ===")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)

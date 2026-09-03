"""绘制闭环冒烟测试：依次执行各绘图命令并校验几何结果，作为回归基线。"""
from __future__ import annotations
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from core.vector.geometry_types import Point3D
from ui.windows.mainWindow import MainWindow


def run():
    QApplication(sys.argv)
    w = MainWindow()
    w.show()
    cm = w._cm
    doc = w._doc

    results = []

    cm.executeCommand("Line")
    cm.onMousePress(Point3D(0, 0, 0), Qt.NoModifier)
    cm.onMousePress(Point3D(50, 30, 0), Qt.NoModifier)
    cm.onMousePress(Point3D(50, 0, 0), Qt.NoModifier)
    cm.onEnter()
    lineObj = None
    for o in doc.objects():
        if o.type().name == "LINE":
            lineObj = o
    results.append(("Line", lineObj is not None and len(lineObj.points()) == 3))

    cm.executeCommand("Circle")
    cm.onMousePress(Point3D(20, 20, 0), Qt.NoModifier)
    cm.onMousePress(Point3D(20, 35, 0), Qt.NoModifier)
    circleObj = None
    for o in doc.objects():
        if o.type().name == "CIRCLE":
            circleObj = o
    results.append(("Circle", circleObj is not None and abs(circleObj.radius() - 15) < 1e-6))

    cm.executeCommand("Curve")
    cm.onMousePress(Point3D(0, 0, 0), Qt.NoModifier)
    cm.onMousePress(Point3D(20, 40, 0), Qt.NoModifier)
    cm.onMousePress(Point3D(40, 10, 0), Qt.NoModifier)
    cm.onMousePress(Point3D(60, 50, 0), Qt.NoModifier)
    # 新语义（精雕多重曲线）：第一次回车=断开当前段，第二次回车=结束入文档
    cm.onEnter()
    cm.onEnter()
    curveObj = None
    for o in doc.objects():
        if o.type().name == "CURVE":
            curveObj = o
    sampled = curveObj.getSampledPoints() if curveObj else []
    results.append(("Curve", curveObj is not None and len(curveObj.inputPoints()) == 4 and len(sampled) > 4))

    cm.executeCommand("Arc")
    cm.onMousePress(Point3D(0, 0, 0), Qt.NoModifier)
    cm.onMousePress(Point3D(15, 15, 0), Qt.NoModifier)
    cm.onMousePress(Point3D(30, 0, 0), Qt.NoModifier)
    arcObj = None
    for o in doc.objects():
        if o.type().name == "ARC":
            arcObj = o
    results.append(("Arc", arcObj is not None and arcObj.radius() > 0))

    cm.executeCommand("Ellipse")
    cm.onMousePress(Point3D(0, 0, 0), Qt.NoModifier)
    cm.onMousePress(Point3D(25, 0, 0), Qt.NoModifier)
    cm.onMousePress(Point3D(0, 15, 0), Qt.NoModifier)
    ellObj = None
    for o in doc.objects():
        if o.type().name == "ELLIPSE":
            ellObj = o
    results.append(("Ellipse", ellObj is not None and abs(ellObj.majorAxis() - 25) < 1e-6))

    cm.executeCommand("Point")
    cm.onMousePress(Point3D(5, 5, 0), Qt.NoModifier)
    cm.onRightClick(Point3D())   # 连续布点：右键结束命令
    ptObj = None
    for o in doc.objects():
        if o.type().name == "POINT":
            ptObj = o
    results.append(("Point", ptObj is not None and abs(ptObj.position().x - 5) < 1e-6))

    print("=== 绘制闭环测试 ===")
    allOk = True
    for name, ok in results:
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")
        allOk = allOk and ok
    print(f"总对象数: {len(doc.objects())}")
    print(f"=== {'全部通过' if allOk else '存在失败'} ===")
    return allOk


if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)
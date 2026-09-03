# -*- coding: utf-8 -*-
"""撤销系统测试：曲线/浮雕双域快照、未变对象跳过、浮雕快速快照（免压缩）、
位图快照、内存预算淘汰、无变化不入栈、存档 roundtrip 兼容。

注意：undo/redo 对"有差异"的对象按快照重建（文档内是新实例，与场景桥
重建语义一致），断言前须用 doc.object(id) 重新取对象，不能用旧引用。
"""
from __future__ import annotations
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

from core.document import Document
from core import app_settings
from core.relief.height_field import HeightField
from core.vector.geometry_types import Point3D, ObjectType
from core.vector.curve_object import CurveObject
from core.vector.relief_model_object import ReliefModelObject
from core.vector.image_object import ImageObject


def _curve(pts) -> CurveObject:
    c = CurveObject()
    c.buildFromSampled([Point3D(x, y, 0.0) for x, y in pts])
    return c


def run():
    results = []

    def check(name, ok):
        results.append((name, ok))
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")

    doc = Document()

    # ---- 1) 曲线域：undo/redo 数值还原 + 未变对象跳过（保留实例） ----
    ca = _curve([(0, 0), (10, 0)])
    cb = _curve([(0, 5), (10, 5)])
    doc.addObject(ca)
    doc.addObject(cb)
    check("初态无可撤销", not doc.canCurveUndo())
    doc.beginEdit()
    ca.translate(5, 0, 0)
    doc.endEdit()
    check("编辑后可撤销", doc.canCurveUndo())
    check("ca 已移动", doc.object(ca.id()).getBoundingBox().min.x == 5.0)
    doc.undoCurve()
    ca2 = doc.object(ca.id())
    check("undo 还原位置", ca2 is not None and ca2.getBoundingBox().min.x == 0.0)
    check("未变对象 cb 保留原实例", doc.object(cb.id()) is cb)
    doc.redoCurve()
    check("redo 重做移动", doc.object(ca.id()).getBoundingBox().min.x == 5.0)
    doc.undoCurve()

    # ---- 2) 无变化编辑不产生撤销项 ----
    doc.beginEdit()
    doc.endEdit()
    check("无变化不入栈", not doc.canCurveUndo())

    # ---- 3) 浮雕域：快速快照 undo/redo（免压缩路径） ----
    hf = HeightField(np.zeros((8, 8), dtype=np.float32), (0.0, 0.0), 1.0)
    model = ReliefModelObject(hf)
    doc.addObject(model)
    doc.beginEdit()
    new = np.full((8, 8), 3.0, dtype=np.float32)
    model.heightField().setData(new)
    model.bumpVersion()
    doc.notifyObjectChanged(model.id())
    doc.endEdit()
    check("浮雕编辑后可撤销（浮雕历史）", doc.canReliefUndo())
    check("浮雕已改值",
          float(doc.object(model.id()).heightField().data[3, 3]) == 3.0)
    doc.undoRelief()
    m2 = doc.object(model.id())
    check("浮雕 undo 还原高度场", m2 is not None
          and float(m2.heightField().data[3, 3]) == 0.0)
    doc.redoRelief()
    check("浮雕 redo 重做",
          float(doc.object(model.id()).heightField().data[3, 3]) == 3.0)
    doc.undoRelief()
    model = doc.object(model.id())   # undo 重建实例：后续用新引用

    # ---- 4) 浮雕放置变换（_place）可撤销 ----
    doc.beginEdit()
    model.translate(10.0, 0.0, 0.0)
    doc.endEdit()
    check("放置已平移", doc.object(model.id()).placement()[0, 2] == 10.0)
    doc.undoRelief()
    check("放置变换可撤销",
          doc.object(model.id()).placement()[0, 2] == 0.0)

    # ---- 5) 色号蒙版快照 roundtrip ----
    model = doc.object(model.id())   # 上节 undo 重建实例：再取新引用
    mask = np.zeros((8, 8), dtype=np.uint8)
    mask[2:5, 2:5] = 2
    doc.beginEdit()
    n = model.fillColorMask(mask > 0, 2)
    doc.notifyObjectChanged(model.id())
    doc.endEdit()
    check("蒙版已填", n > 0
          and int(doc.object(model.id()).colorMask()[3, 3]) == 2)
    doc.undoRelief()
    cm = doc.object(model.id()).colorMask()
    check("蒙版 undo 清空", cm is None or int(cm.max()) == 0)

    # ---- 6) 位图快照 roundtrip ----
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QImage, QColor
    from PySide6.QtCore import QBuffer
    QApplication.instance() or QApplication(sys.argv)
    img = QImage(16, 16, QImage.Format.Format_RGB32)
    img.fill(QColor(200, 30, 30))
    buf = QBuffer()
    buf.open(QBuffer.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    io = ImageObject()
    io.setImageData(bytes(buf.data()))
    io.setGeometry(Point3D(1, 2, 0), 30.0, 20.0, 0.5)
    snap = io.snapshot()
    io2 = ImageObject()
    io2.restoreSnapshot(snap)
    check("位图快照数据还原", io2.imageData() == io.imageData()
          and io2.pixelSize() == io.pixelSize()
          and abs(io2.rotation() - 0.5) < 1e-12)
    check("位图 matchesSnapshot 命中", io.matchesSnapshot(snap))
    io.translate(1, 0, 0)
    check("位图移动后快照不命中", not io.matchesSnapshot(snap))

    # ---- 7) 内存预算淘汰（大快照从最旧淘汰，至少保留一项） ----
    oldBudget = app_settings.undo_memory_budget_mb
    try:
        app_settings.undo_memory_budget_mb = 1   # 1MB：256KB/快照，5 项必被淘汰
        doc2 = Document()
        m = ReliefModelObject(HeightField(
            np.zeros((256, 256), dtype=np.float32), (0.0, 0.0), 1.0))  # 256KB/快照
        doc2.addObject(m)
        for v in range(1, 6):
            doc2.beginEdit()
            arr = np.full((256, 256), float(v), dtype=np.float32)
            m.heightField().setData(arr)
            m.bumpVersion()
            doc2.endEdit()
        depth = len(doc2._undoHistory["relief"])
        check(f"预算淘汰生效（压入 5 项后剩 {depth} 项）", 1 <= depth < 5)
        # 每项 ≈ before+after 各 256KB，1MB 预算只剩最末一项（v4→v5）：
        # 连撤到栈底应回到 v=4（最旧存活项的 before），v1~v3 已被淘汰
        while doc2.canReliefUndo():
            doc2.undoRelief()
        cur = doc2.object(m.id())
        check("淘汰后栈底是存活项的初值 v=4",
              cur is not None and float(cur.heightField().data[0, 0]) == 4.0)
    finally:
        app_settings.undo_memory_budget_mb = oldBudget

    # ---- 8) 存档 roundtrip 兼容（serialize 路径未受影响） ----
    path = os.path.join(tempfile.mkdtemp(), "undo_test.cai")
    doc.save(path)
    doc3 = Document()
    doc3.load(path)
    types = sorted(o.type().name for o in doc3.objects())
    check("存档 roundtrip（曲线+模型）",
          types.count("CURVE") == 2 and types.count("SURFACE") == 1)
    m3 = next(o for o in doc3.objects() if o.type() == ObjectType.SURFACE)
    check("存档模型高度场还原", float(m3.heightField().data[3, 3]) == 0.0)

    print("=== 撤销系统测试 ===")
    allOk = all(ok for _, ok in results)
    print(f"=== {'全部通过' if allOk else '存在失败'} ===")
    return allOk


if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)

"""多文件系统冒烟测试：新建/切换/关闭标签，保存/重开，右键重复命令。"""
from __future__ import annotations
import sys
import os
import tempfile

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
    results = []

    def check(name, ok):
        results.append((name, ok))
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")

    # 1) 初始：单个未命名标签
    check("初始单标签", len(w._openDocs) == 1 and w._docIndex == 0)

    # 2) 新建两个文档：始终追加，未命名1 保留
    w._newDocument()
    w._newDocument()
    check("新建两个标签", len(w._openDocs) == 3 and w._docIndex == 2)

    # 3) 在标签 2（当前）画一条线 → dirty
    doc2 = w._doc
    cm.executeCommand("Line")
    cm.onMousePress(Point3D(0, 0, 0), Qt.NoModifier)
    cm.onMousePress(Point3D(50, 30, 0), Qt.NoModifier)
    cm.onEnter()
    check("标签2画线", any(o.type().name == "LINE" for o in doc2.objects()))
    check("画线后 dirty", doc2.isDirty())

    # 4) 切回标签 0：对象隔离、视图重绑
    w._switchToDocument(0)
    check("切回标签0对象隔离", len(w._doc.objects()) == 0)
    check("self._doc 已重绑", w._doc is w._openDocs[0])

    # 5) 标签 0 画圆 → 只在本文档
    cm.executeCommand("Circle")
    cm.onMousePress(Point3D(20, 20, 0), Qt.NoModifier)
    cm.onMousePress(Point3D(20, 35, 0), Qt.NoModifier)
    check("标签0画圆", any(o.type().name == "CIRCLE" for o in w._doc.objects()))
    check("标签2不受影响", any(o.type().name == "LINE" for o in doc2.objects())
          and len(doc2.objects()) == 1)

    # 6) 右键重复命令（无活动命令时）：重复 Circle
    check("右键重复上一命令", cm.repeatLastCommand() and cm.hasActive())
    cm.cancelActive()

    # 7) 保存标签 0 → dirty 清除、路径绑定；标签 2 仍 dirty
    tmpdir = tempfile.mkdtemp()
    path0 = os.path.join(tmpdir, "doc0.cag")
    w._saveTo(path0)   # 指定路径保存（_saveDoc 无路径时会弹对话框）
    check("保存标签0", w._doc.filePath() == path0 and not w._doc.isDirty())
    check("标签2仍 dirty", doc2.isDirty())

    # 8) 保存标签 2 后关闭标签 0（未命名1），索引自动修正
    path2 = os.path.join(tmpdir, "doc2.cag")
    w._switchToDocument(2)
    w._saveTo(path2)
    w._switchToDocument(0)          # 回到标签 0
    w._closeDocument(0)             # 关闭它（已保存，无确认弹窗）
    check("关闭后剩余标签", len(w._openDocs) == 2 and w._docIndex == 0
          and w._openDocs[1] is doc2)

    # 9) 打开已保存文件 → 新标签载入对象
    w._newDocument(path0)
    check("打开文件载入对象",
          any(o.type().name == "CIRCLE" for o in w._doc.objects()))
    check("打开后不 dirty", not w._doc.isDirty())

    # 10) 场景桥同步：当前文档对象有图元，非当前文档无图元
    bridge = w._bridge
    nItems = len([i for i in bridge._items.values()])
    check("场景图元同步", nItems == len(w._doc.objects()))

    print("=== 多文件系统测试 ===")
    allOk = all(ok for _, ok in results)
    print(f"=== {'全部通过' if allOk else '存在失败'} ===")
    return allOk


if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)

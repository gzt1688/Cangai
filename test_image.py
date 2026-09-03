# -*- coding: utf-8 -*-
"""图片功能冒烟测试：放置命令、图层归属、z 顺序、序列化 roundtrip、变换。"""
from __future__ import annotations
import sys
import os
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QBuffer, QPointF
from PySide6.QtGui import QImage, QColor
from core.vector.geometry_types import Point3D, ObjectType
from core.document import Document, LAYER_IMAGE_ID
from core.vector.image_object import ImageObject
from ui.windows.mainWindow import MainWindow


def _makePng(width=32, height=24) -> bytes:
    img = QImage(width, height, QImage.Format.Format_RGB32)
    img.fill(QColor(200, 30, 30))
    buf = QBuffer()
    buf.open(QBuffer.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def run():
    QApplication(sys.argv)
    w = MainWindow()
    w.show()
    cm = w._cm
    results = []

    def check(name, ok):
        results.append((name, ok))
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")

    tmpdir = tempfile.mkdtemp()
    pngPath = os.path.join(tmpdir, "ref.png")
    with open(pngPath, "wb") as f:
        f.write(_makePng())

    # 1) 两点框放命令
    ok = cm.executeCommand("Image", pngPath)
    check("启动图片命令", ok and cm.hasActive())
    cm.onMousePress(Point3D(0, 0, 0), Qt.NoModifier)      # 第一角点
    check("第一角点后仍激活", cm.hasActive())
    cm.onMouseMove(Point3D(200, 100, 0), Qt.NoModifier)   # 预览
    cm.onMousePress(Point3D(200, 100, 0), Qt.NoModifier)  # 对角点 → 放置
    imgs = [o for o in w._doc.objects() if o.type() == ObjectType.IMAGE]
    check("图片已入文档", len(imgs) == 1 and not cm.hasActive())
    obj = imgs[0] if imgs else None

    # 2) 等比适配：32x24 图 → 200x100 框 → contain 为 133.33x100 居中
    if obj is not None:
        check("等比适配尺寸", abs(obj.width() - 32 * 100 / 24) < 1e-6
              and abs(obj.height() - 100.0) < 1e-6)
        c = obj.center()
        check("居中放置", abs(c.x - 100) < 1e-6 and abs(c.y - 50) < 1e-6)

    # 3) 图层归属：图片固定层
    check("图片固定图层", obj is not None
          and obj.layerId() == LAYER_IMAGE_ID
          and [l.name for l in w._doc.layers()][:3] == ["曲线", "模型", "图片"])

    # 4) z 顺序：图片在曲线下方（-600）
    item = w._bridge.item(obj.id()) if obj else None
    check("图片垫在曲线下方", item is not None and item.zValue() == -600)

    # 5) 变换：移动 / 缩放 / 旋转
    if obj is not None:
        obj.translate(10, -5, 0)
        check("移动", abs(obj.center().x - 110) < 1e-6
              and abs(obj.center().y - 45) < 1e-6)
        obj.scaleAround(2.0, 2.0, 2.0, obj.center())
        check("缩放", abs(obj.width() - 2 * 32 * 100 / 24) < 1e-6)
        bb0 = obj.getBoundingBox()
        import math
        obj.rotateAround(math.pi / 2, obj.center())   # 绕中心转 90°
        bb1 = obj.getBoundingBox()
        check("旋转（包围盒宽高互换）",
              abs(bb1.max.x - bb1.min.x - (bb0.max.y - bb0.min.y)) < 1e-4)

    # 6) 序列化 roundtrip：曲线 + 图片 + 图层 全部数据一起保存
    cm.executeCommand("Line")
    cm.onMousePress(Point3D(0, 0, 0), Qt.NoModifier)
    cm.onMousePress(Point3D(50, 30, 0), Qt.NoModifier)
    cm.onEnter()
    caiPath = os.path.join(tmpdir, "with_image.cai")
    w._saveTo(caiPath)
    check(".cai 文件保存", os.path.exists(caiPath)
          and w._doc.filePath().endswith(".cai") and not w._doc.isDirty())

    doc2 = Document()
    doc2.load(caiPath)
    types = [o.type() for o in doc2.objects()]
    check("载入含曲线+图片", types.count(ObjectType.LINE) == 1
          and types.count(ObjectType.IMAGE) == 1)
    img2 = next((o for o in doc2.objects()
                 if o.type() == ObjectType.IMAGE), None)
    if img2 is not None:
        check("图片数据完整还原",
              img2.imageData() == obj.imageData()
              and abs(img2.rotation() - obj.rotation()) < 1e-6
              and abs(img2.center().x - obj.center().x) < 1e-6)
        check("载入后图片归图片层",
              img2.layerId() == LAYER_IMAGE_ID)

    # 7) 场景图元：重建后可命中（shape 含中心点）
    from ui.view_2d.item_factory import createItem
    item2 = createItem(img2, 1.0)
    check("图元命中区含中心", item2.shape().contains(
        QPointF(img2.center().x, img2.center().y)))

    print("=== 图片功能测试 ===")
    allOk = all(ok for _, ok in results)
    print(f"=== {'全部通过' if allOk else '存在失败'} ===")
    return allOk


if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)

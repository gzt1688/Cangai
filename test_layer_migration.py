# -*- coding: utf-8 -*-
"""旧文件图层 id 迁移测试：旧版用户图层 id 2/3 与新增固定层(图片/标注)冲突时让位。"""
from __future__ import annotations
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.document import Document, LAYER_IMAGE_ID, LAYER_ANNOTATION_ID
from core.vector.geometry_types import ObjectType


def run():
    results = []

    def check(name, ok):
        results.append(ok)
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")

    tmpdir = tempfile.mkdtemp()

    # 旧版文件：用户自建了两个额外图层 id=2“图层2”(含直线)、id=3“图层3”(含圆)
    old = {
        "activeLayerId": 2,
        "layers": [
            {"id": 0, "name": "曲线"},
            {"id": 1, "name": "模型"},
            {"id": 2, "name": "图层2"},
            {"id": 3, "name": "图层3"},
        ],
        "objects": [
            {"id": "aaa111", "type": "LINE", "layerId": 2,
             "color": [255, 100, 0], "lineWidth": 1.0,
             "points": [[0, 0, 0], [10, 10, 0]], "closed": False},
            {"id": "bbb222", "type": "CIRCLE", "layerId": 3,
             "color": [255, 100, 0], "lineWidth": 1.0,
             "center": [5, 5, 0], "radius": 8},
        ],
    }
    path = os.path.join(tmpdir, "legacy.cag")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(old, f, ensure_ascii=False)

    doc = Document()
    doc.load(path)
    layers = {l.id: l.name for l in doc.layers()}

    line = doc.object("aaa111")
    circ = doc.object("bbb222")
    check("旧图层2让位重编号（原对象随层迁移，非图片层）",
          line is not None and line.layerId() != LAYER_IMAGE_ID
          and layers.get(line.layerId()) == "图层2")
    check("旧图层3让位重编号（非标注层）",
          circ is not None and circ.layerId() != LAYER_ANNOTATION_ID
          and layers.get(circ.layerId()) == "图层3")
    check("固定图片/标注层已建立且为空",
          layers.get(LAYER_IMAGE_ID) == "图片"
          and layers.get(LAYER_ANNOTATION_ID) == "标注"
          and not any(o.layerId() == LAYER_IMAGE_ID for o in doc.objects()))
    check("活动层随迁移指向新 id",
          doc.activeLayerId() == line.layerId())

    # 新格式文件（固定图片层上有真图片）不被误迁移
    from core.vector.image_object import ImageObject
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QBuffer
    from PySide6.QtGui import QImage, QColor
    app = QApplication.instance() or QApplication([])
    img = QImage(8, 8, QImage.Format.Format_RGB32)
    img.fill(QColor(9, 9, 9))
    buf = QBuffer()
    buf.open(QBuffer.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    doc2 = Document()
    im = ImageObject()
    im.setImageData(bytes(buf.data()))
    doc2.addObject(im)   # 自动归图片层
    p2 = os.path.join(tmpdir, "new.cag")
    doc2.save(p2)
    doc3 = Document()
    doc3.load(p2)
    imgLoaded = next(o for o in doc3.objects()
                     if o.type() == ObjectType.IMAGE)
    check("新格式图片层不被迁移", imgLoaded.layerId() == LAYER_IMAGE_ID
          and layers.get(LAYER_IMAGE_ID) == "图片"
          and "图片" in [l.name for l in doc3.layers()])

    ok = all(results)
    print(f"=== {'全部通过' if ok else '存在失败'} ===")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)

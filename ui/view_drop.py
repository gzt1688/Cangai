# -*- coding: utf-8 -*-
"""画布拖放导入的共享逻辑：支持把图片/曲线/网格/3D 模型/工程文件直接拖进画布。

两个画布控件（GraphicsView / Viewport3D）共用这里的扩展名过滤与接受判定；
dropEvent 的落点由各画布自行换算成世界坐标后，连同路径列表发 fileDropped 信号，
主窗口据此导入（图片落点直接放置 / DXF、STL、STEP、BREP 走对应导入 /
CAI、CAG 工程文件打开为新文档）。
"""
from __future__ import annotations

# 图片 / DXF 曲线 / STL 网格 / STEP-BREP 3D 模型 / Cangai 工程文件
SUPPORTED_SUFFIXES = (
    ".png", ".jpg", ".jpeg",
    ".dxf",
    ".stl",
    ".step", ".stp", ".brep",
    ".cai", ".cag",
)


def droppedFilePaths(event) -> list:
    """从拖放事件提取本地文件路径，只保留支持的扩展名。"""
    import os
    urls = event.mimeData().urls()
    if not urls:
        return []
    out = []
    for url in urls:
        if not url.isLocalFile():
            continue
        p = url.toLocalFile()
        if os.path.isfile(p) and p.lower().endswith(SUPPORTED_SUFFIXES):
            out.append(p)
    return out


def acceptIfFiles(event) -> bool:
    """拖拽带支持的文件时接受动作并返回 True，否则忽略并返回 False。"""
    paths = droppedFilePaths(event)
    if paths:
        event.acceptProposedAction()
    else:
        event.ignore()
    return bool(paths)

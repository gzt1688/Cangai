"""STEP 导出：把 3D B-Rep 实体（BRepObject/BrepShape，build123d 内核）导出为 .step 文件。

适用对象：挤出（Extrude）、墙线（WallLine）、布尔、螺纹、推拉等由
build123d 构造的 B-Rep 实体——几何以精确曲面/实体表示，适合 CAD/CAM
交换。仅支持 BrepShape 内核（持有 build123d part）；PolySolid 轻量内核
（面点模型，无拓扑 part）无法导出 STEP，自动跳过并计数。

多个实体时合并为一个 Compound 写入同一文件。
"""
from __future__ import annotations

from typing import List, Optional

from ..vector.geometry_types import ObjectType


def _import_bd():
    """延迟导入 build123d（仅在导出时加载，避免无 OCP 环境启动报错）。"""
    from build123d import Compound
    from build123d.exporters3d import export_step
    return Compound, export_step


def exportSTEP(doc, path: str, solids=None) -> int:
    """导出 3D B-Rep 实体为 .step，返回成功导出的实体数。

    - ``solids`` 非空时导出指定的实体列表（3D 模式选中实体导出）；
    - 否则导出文档中全部实体（BRepObject）。
    仅 BrepShape 内核可导出；PolySolid 跳过。
    无可导出实体时抛 ValueError（提示需 B-Rep 实体）。
    """
    Compound, export_step = _import_bd()
    objs: List = []
    if solids:
        objs = list(solids)
    else:
        for o in doc.allObjects():
            if o.type() in (ObjectType.SOLID, ObjectType.BREP):
                objs.append(o)
    parts = []
    skipped = 0
    for o in objs:
        ps = getattr(o, "_ps", None)
        if ps is None:
            skipped += 1
            continue
        part_fn = getattr(ps, "part", None)
        if part_fn is None:
            skipped += 1
            continue
        try:
            p = part_fn()
        except Exception:
            skipped += 1
            continue
        if p is None:
            skipped += 1
            continue
        parts.append(p)
    if not parts:
        if skipped and not objs:
            raise ValueError("没有可导出的 3D 实体")
        raise ValueError(
            "没有可导出的 B-Rep 实体（STEP 仅支持挤出/布尔等 build123d 实体）")
    to_export = parts[0] if len(parts) == 1 else Compound(children=parts)
    try:
        export_step(to_export, path)
    except Exception as e:
        raise ValueError(f"STEP 导出失败: {e}") from e
    return len(parts)

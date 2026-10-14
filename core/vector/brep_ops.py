# -*- coding: utf-8 -*-
"""B-Rep 布尔运算：并 / 差 / 交（OCP BRepAlgoAPI，FuzzyValue 可调）。

供 3D 布尔命令（Boolean3DCommand）与后续体素类功能调用：
- booleanOp(part_a, part_b, op) → 新 build123d Shape（不修改入参）。
  op: "union" | "cut" | "common"（cut = A − B，顺序敏感）。
- FuzzyValue 默认 1e-7（OCCT 引擎默认同量级）；两实体存在共面/贴面时
  调大到 1e-5 量级可显著减少失败率。
- meshToSolid(mesh) → 三角网格缝合为实体（PolySolid 旧内核实体升级用）。

build123d 延迟导入：与 brep_shape 相同策略，不阻塞软件启动。
"""
from __future__ import annotations
from typing import Optional

# 布尔操作名 → 语义
OPS = ("union", "cut", "common")
OP_NAMES = {"union": "并集", "cut": "差集", "common": "交集"}


def _import_bd():
    from .brep_shape import _import_bd as _f
    return _f()


def booleanOp(part_a, part_b, op: str = "union",
              fuzzy: Optional[float] = 1e-7):
    """两实体布尔 → 新 build123d Shape。

    part_a/part_b: build123d Shape（Part/Solid/Compound，含实体）。
    op: "union"(A∪B) / "cut"(A−B) / "common"(A∩B)。
    fuzzy: 布尔容差；None 用 OCCT 默认。共面场景失败可调大。
    结果无实体（不相交的差/交）抛 ValueError，由调用方转为用户提示。
    """
    if op not in OPS:
        raise ValueError(f"未知布尔操作: {op}")
    from OCP.BRepAlgoAPI import (BRepAlgoAPI_Fuse, BRepAlgoAPI_Cut,
                                 BRepAlgoAPI_Common)
    from OCP.TopTools import TopTools_ListOfShape

    def _lst(shape):
        l = TopTools_ListOfShape()
        l.Append(shape.wrapped)
        return l

    algo = {"union": BRepAlgoAPI_Fuse,
            "cut": BRepAlgoAPI_Cut,
            "common": BRepAlgoAPI_Common}[op]()
    algo.SetArguments(_lst(part_a))
    algo.SetTools(_lst(part_b))
    if fuzzy is not None and fuzzy > 0.0:
        algo.SetFuzzyValue(float(fuzzy))
    algo.SetRunParallel(True)
    algo.Build()
    if not algo.IsDone():
        raise ValueError("布尔运算失败（内核未完成）")
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    res = algo.Shape()
    if res is None or res.IsNull():
        raise ValueError("布尔结果为空")
    # 结果必须含实体：不相交的 cut/common 常返回空壳/空复合体
    exp = TopExp_Explorer(res, TopAbs_ShapeEnum.TopAbs_SOLID)
    if not exp.More():
        raise ValueError(
            "布尔结果无实体（两实体可能不相交）")
    # 规范化包装：Compound 仅含 1 个实体 → 直接取该实体（后续倒角/推拉
    # 等拓扑索引更干净）；多实体（切分成数块）保留 Compound。
    # 注意：Shape.cast 是抽象方法（返回 None），分发实现在 Compound.cast
    bd = _import_bd()
    shape = bd.Compound.cast(res)
    try:
        if isinstance(shape, bd.Compound):
            solids = shape.solids()
            if len(solids) == 1:
                shape = solids[0]
    except Exception:
        pass
    # 负体积实体（面朝向错乱，渲染会出"镂空黑斑"）直接报错让用户重试
    try:
        if shape.volume < 0.0:
            raise ValueError("布尔结果体积为负（实体朝向异常），请重试")
    except ValueError:
        raise
    except Exception:
        pass
    return shape


def meshToSolid(mesh: dict):
    """三角网格 {verts, tris} → 缝合实体（build123d Shape）。

    PolySolid 旧内核实体参与布尔前的升级路径：逐三角形建面 →
    BRepBuilderAPI_Sewing 缝合 → MakeSolid 封闭 → 体积负则翻转朝向。
    网格不闭合时抛 ValueError。
    """
    import numpy as np
    from OCP.BRepBuilderAPI import (BRepBuilderAPI_Sewing,
                                    BRepBuilderAPI_MakeSolid,
                                    BRepBuilderAPI_MakePolygon,
                                    BRepBuilderAPI_MakeFace)
    from OCP.gp import gp_Pnt
    from OCP.TopoDS import TopoDS
    verts = np.asarray(mesh["verts"], dtype=np.float64)
    tris = np.asarray(mesh["tris"], dtype=np.int64)
    if len(verts) == 0 or len(tris) == 0:
        raise ValueError("网格为空")
    sew = BRepBuilderAPI_Sewing(1e-4, True, True, True, False)
    for (a, b, c) in tris:
        va, vb, vc = verts[a], verts[b], verts[c]
        poly = BRepBuilderAPI_MakePolygon(
            gp_Pnt(*va), gp_Pnt(*vb), gp_Pnt(*vc), True)
        if not poly.IsDone():
            continue
        mkf = BRepBuilderAPI_MakeFace(poly.Wire(), True)
        if mkf.IsDone():
            sew.Add(mkf.Face())
    sew.Perform()
    shell = sew.SewedShape()
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopExp import TopExp_Explorer
    exp = TopExp_Explorer(shell, TopAbs_ShapeEnum.TopAbs_SHELL)
    if not exp.More():
        raise ValueError("网格无法缝合（实体可能不闭合）")
    mk = BRepBuilderAPI_MakeSolid()
    from OCP.TopoDS import TopoDS_Shell
    while exp.More():
        sh = TopoDS.Shell_s(exp.Current())
        mk.Add(sh)
        exp.Next()
    if not mk.IsDone():
        raise ValueError("缝合面无法组成实体")
    solid = mk.Solid()
    # 封闭实体朝向矫正：负体积（缝合绕向相反）翻转为正——OCCT 标准做法
    try:
        from OCP.BRepLib import BRepLib
        BRepLib.OrientClosedSolid_s(solid)
    except Exception:
        pass
    bd = _import_bd()
    return bd.Compound.cast(solid)

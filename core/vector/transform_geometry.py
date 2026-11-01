"""缩放辅助：圆遇到非等比缩放时先转换成等效椭圆再缩放。

圆只用「圆心 + 半径」描述，单轴缩放后必然变成椭圆，无法仍用圆表示。
若继续按平均比例缩小半径（旧行为），X 方向拉长 2 倍时圆仍是圆，几何错误。
本模块把这类缩放统一为「圆 → 椭圆（同 id 就地替换）→ 等比/非等比缩放」，
DXF/SVG/CNC/挤出/捕捉等椭圆路径都能正确读取。
"""
from __future__ import annotations

from .geometry_types import ObjectType
from .ellipse_object import EllipseObject


def scaleObjectAround(obj, sx: float, sy: float, sz: float, center, doc=None):
    """把单个对象绕 center 缩放；圆的非等比缩放转成椭圆并返回新对象。

    doc 为 None（例如纯计算场景）时保持原对象行为（半径取两轴平均）。
    返回实际承载结果的对象（对象被替换时为新对象，id 不变）。
    """
    if (doc is not None and obj.type() == ObjectType.CIRCLE
            and abs(abs(sx) - abs(sy)) > 1e-9):
        c = obj.center()
        ell = EllipseObject(c.x, c.y, c.z, obj.radius(), obj.radius())
        ell.scaleAround(sx, sy, sz, center)
        doc.replaceObject(obj.id(), ell)
        return ell
    obj.scaleAround(sx, sy, sz, center)
    return obj

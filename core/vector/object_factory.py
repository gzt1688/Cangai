"""几何对象工厂：按 ObjectType 创建对应对象实例。"""
from __future__ import annotations
from typing import Optional
from .geometry_types import ObjectType
from .base_object import BaseObject
from .point_object import PointObject
from .line_object import LineObject
from .circle_object import CircleObject
from .arc_object import ArcObject
from .ellipse_object import EllipseObject
from .curve_object import CurveObject
from .nurbs_curve_object import NurbsCurveObject
from .poly_curve_object import PolyCurveObject
from .relief_model_object import ReliefModelObject
from .dimension_object import DimensionObject
from .brep_object import BRepObject

def createFromType(typeStr: str) -> Optional[BaseObject]:
    try:
        t = ObjectType[typeStr.upper()]
    except KeyError:
        return None
    if t == ObjectType.POINT:
        return PointObject()
    if t == ObjectType.LINE:
        return LineObject()
    if t == ObjectType.CIRCLE:
        return CircleObject()
    if t == ObjectType.ARC:
        return ArcObject()
    if t == ObjectType.ELLIPSE:
        return EllipseObject()
    if t == ObjectType.CURVE:
        return CurveObject()
    if t == ObjectType.NURBS_CURVE:
        return NurbsCurveObject()
    if t == ObjectType.POLYCURVE:
        return PolyCurveObject()
    if t == ObjectType.SURFACE:
        return ReliefModelObject()
    if t == ObjectType.BREP:
        return BRepObject()
    if t == ObjectType.DIMENSION:
        return DimensionObject()
    return None
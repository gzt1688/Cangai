"""浮雕核心：高度场数据结构与后续算子/渲染（纯算法，零 UI 依赖）。"""
from .height_field import HeightField, MAX_PIXELS

__all__ = ["HeightField", "MAX_PIXELS"]

"""浮雕核心：高度场数据结构与后续算子/渲染（纯算法，零 UI 依赖）。"""
from .height_field import HeightField, MAX_PIXELS
from .image_to_model import (
    ImageToModelParams, ImageToModelResult,
    ImageProcessParams, HeightFieldParams,
    run_pipeline, run_pipeline_from_gray, decode_image_bytes,
    process_image_only, heightfield_from_processed,
    load_image_gray, auto_stretch,
    edge_protected_smooth, enhance_detail, remap_height,
)

__all__ = [
    "HeightField", "MAX_PIXELS",
    "ImageToModelParams", "ImageToModelResult",
    "ImageProcessParams", "HeightFieldParams",
    "run_pipeline", "run_pipeline_from_gray", "decode_image_bytes",
    "process_image_only", "heightfield_from_processed",
    "load_image_gray", "auto_stretch",
    "edge_protected_smooth", "enhance_detail", "remap_height",
]

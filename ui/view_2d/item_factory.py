"""QGraphicsItem 工厂：将几何对象转为可绘制图元。"""
from __future__ import annotations
import math
from typing import Optional
from PySide6.QtGui import QPainterPath

from core.vector.geometry_types import ObjectType, Point3D
from .items.base_item import BaseGraphicsItem
from .items.relief_model_item import ReliefModelItem
from .items.image_item import ImageGraphicsItem
from .items.dimension_item import DimensionGraphicsItem

POINT_MARKER_PX = 3.0  # 点标记屏幕半径（像素）：固定大小，不随视图缩放变化


def _buildPath(obj, viewScale: float = 1.0) -> QPainterPath:
    path = QPainterPath()
    t = obj.type()
    if t == ObjectType.POINT:
        p = obj.position()
        # 像素半径按当前视图缩放换算回世界半径，保证屏幕尺寸恒定（选择命中区同尺寸）
        r = POINT_MARKER_PX / max(viewScale, 1e-9)
        path.addEllipse(p.x - r, p.y - r, r * 2, r * 2)
    elif t == ObjectType.LINE:
        pts = obj.points()
        if pts:
            path.moveTo(pts[0].x, pts[0].y)
            for p in pts[1:]:
                path.lineTo(p.x, p.y)
            if obj.isClosed() and len(pts) > 2:
                path.lineTo(pts[0].x, pts[0].y)
    elif t == ObjectType.CIRCLE:
        c = obj.center()
        r = obj.radius()
        path.addEllipse(c.x - r, c.y - r, r * 2, r * 2)
    elif t == ObjectType.ARC:
        c = obj.center()
        r = obj.radius()
        sa, ea = obj.startAngle(), obj.endAngle()
        span = ea - sa
        if abs(span) < 1e-9:
            span = 2 * math.pi   # 起止角重合视为整圆
        # 采样折线（数学约定）：保证弧精确经过起/终点，避免 arcTo 角度约定偏差。
        n = 64
        path.moveTo(c.x + r * math.cos(sa), c.y + r * math.sin(sa))
        for i in range(1, n + 1):
            a = sa + span * i / n
            path.lineTo(c.x + r * math.cos(a), c.y + r * math.sin(a))
    elif t == ObjectType.ELLIPSE:
        c = obj.center()
        a, b = obj.majorAxis(), obj.minorAxis()
        path.addEllipse(c.x - a, c.y - b, a * 2, b * 2)
        if abs(obj.rotation()) > 1e-9:
            from PySide6.QtGui import QTransform
            tr = QTransform()
            tr.translate(c.x, c.y)
            tr.rotate(math.degrees(obj.rotation()))
            tr.translate(-c.x, -c.y)
            path = tr.map(path)
    elif t == ObjectType.CURVE:
        pts = obj.getSampledPoints()
        if pts:
            path.moveTo(pts[0].x, pts[0].y)
            for p in pts[1:]:
                path.lineTo(p.x, p.y)
    elif t == ObjectType.NURBS_CURVE:
        pts = obj.getSampledPoints()
        if pts:
            path.moveTo(pts[0].x, pts[0].y)
            for p in pts[1:]:
                path.lineTo(p.x, p.y)
    elif t == ObjectType.POLYCURVE:
        for seg in obj.segments():
            pts = seg.get("points", [])
            if pts:
                path.moveTo(pts[0][0], pts[0][1])
                for p in pts[1:]:
                    path.lineTo(p[0], p[1])
    elif t == ObjectType.REVCLOUD:
        path = obj.path()
    elif t == ObjectType.IMAGE:
        # 图片：四角旋转后的矩形轮廓（预览/命中辅助用）
        pts = obj.points()
        if len(pts) == 4:
            path.moveTo(pts[0].x, pts[0].y)
            for p in pts[1:]:
                path.lineTo(p.x, p.y)
            path.closeSubpath()
    return path


def createItem(obj, viewScale: float = 1.0) -> Optional[BaseGraphicsItem]:
    # SURFACE 浮雕模型走专用图元（着色预览面 + 填充命中），不构建路径
    if obj.type() == ObjectType.SURFACE:
        return ReliefModelItem(obj)
    # IMAGE 参考图走专用图元（位图 + 四角命中），垫在曲线下方
    if obj.type() == ObjectType.IMAGE:
        return ImageGraphicsItem(obj)
    # DIMENSION 尺寸标注走专用图元（自绘尺寸线/箭头/读数文字）
    if obj.type() == ObjectType.DIMENSION:
        return DimensionGraphicsItem(obj)
    item = BaseGraphicsItem(obj)
    item.setPath(_buildPath(obj, viewScale))
    return item


def updateItem(item: BaseGraphicsItem, obj, viewScale: float = 1.0) -> None:
    # 浮雕模型：几何/预览由 syncFromObject 内部按 version 脏标记重算，不重建路径
    if obj.type() == ObjectType.SURFACE:
        item.syncFromObject()
        return
    # 图片：几何每次由 paint 现读对象，syncFromObject 同步选中态即可
    if obj.type() == ObjectType.IMAGE:
        item.syncFromObject()
        item.prepareGeometryChange()
        return
    # 标注：自绘图元每次现读对象几何，仅需同步选中态并刷新缓存矩形
    if obj.type() == ObjectType.DIMENSION:
        item.syncFromObject()
        item.prepareGeometryChange()
        return
    item.setPath(_buildPath(obj, viewScale))
    item.syncFromObject()
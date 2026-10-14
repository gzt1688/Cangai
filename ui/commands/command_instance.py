"""命令实例基类：定义命令生命周期（启动/输入/完成/取消）。"""
from __future__ import annotations
import math
from abc import ABC, abstractmethod
from enum import Enum
from typing import List, Optional
from PySide6.QtCore import Qt
from PySide6.QtGui import QPen, QColor, QPainterPath
from PySide6.QtWidgets import QGraphicsPathItem, QGraphicsEllipseItem

from core.vector.geometry_types import Point3D

class CommandState(Enum):
    WAITING_START = 0
    WAITING_INPUT = 1
    COMPLETE = 2
    CANCELED = 3


class CommandInstance(ABC):
    def __init__(self) -> None:
        self._state = CommandState.WAITING_START
        self._manager = None   # 由 CommandManager 注入：面板直接调 finish 时回通知调度中心
        self._doc = None
        self._view = None
        self._previewItem: Optional[QGraphicsPathItem] = None
        self._markerItem: Optional[QGraphicsEllipseItem] = None
        self._previewImages: List = []   # 图片预览项（ImageGraphicsItem），用于移动/旋转/缩放时图片预览出自己

    def setContext(self, doc, view) -> None:
        self._doc = doc
        self._view = view
        self._modeView = None   # 活动视口探测（3D 模式下由 CommandManager 注入）

    def setModeView(self, view) -> None:
        """注入"模式视口"：提供 canvasMode()/viewMode() 接口的活动画布。
        上下文 _view 始终是 2D 画布（预览图元宿主），视图方向判定需查询
        真正可见的视口（3D 模式下为 Viewport3D，前视/右视按侧视平面绘制）。"""
        self._modeView = view

    def state(self) -> CommandState:
        return self._state

    def isComplete(self) -> bool:
        return self._state == CommandState.COMPLETE

    def isCanceled(self) -> bool:
        return self._state == CommandState.CANCELED

    def isAlive(self) -> bool:
        return self._state in (CommandState.WAITING_START, CommandState.WAITING_INPUT)

    def start(self) -> None:
        self._state = CommandState.WAITING_INPUT
        self._createPreview()

    def _createPreview(self) -> None:
        self._previewItem = QGraphicsPathItem()
        pen = QPen(QColor("#15b6c9"), 1, Qt.DashLine)
        pen.setCosmetic(True)
        self._previewItem.setPen(pen)
        self._previewItem.setBrush(Qt.BrushStyle.NoBrush)   # 预览永不填充
        self._previewItem.setZValue(9999)
        # 光标点：独立图元（白底实心圆），避免给整个预览路径上白笔刷
        self._markerItem = QGraphicsEllipseItem()
        mpen = QPen(QColor("#15b6c9"), 1.5)
        mpen.setCosmetic(True)
        self._markerItem.setPen(mpen)
        self._markerItem.setBrush(QColor(Qt.GlobalColor.white))
        self._markerItem.setZValue(9999)
        self._markerItem.hide()
        if self._view is not None:
            self._view.scene().addItem(self._previewItem)
            self._view.scene().addItem(self._markerItem)

    def _setPreviewPath(self, path, solid: bool = False, color=None) -> None:
        """设置预览路径（不填充）；光标点由 _setCursorMarker 单独绘制。
        solid=True 用于画线/弧/曲线等逐点交互预览（实线，用户明确要求不用虚线）；
        形状拖拽预览保持默认虚线。color 传入对象色可使预览与成型体同色，
        避免点击落点时变色闪烁（不传保持默认青色）。"""
        if self._previewItem is None:
            return
        self._previewItem.setPath(path)
        pen = self._previewItem.pen()
        if color is not None:
            pen.setColor(QColor(*color))
        if solid and not path.isEmpty():
            pen.setStyle(Qt.PenStyle.SolidLine)
            pen.setWidthF(1.5)
        else:
            pen.setStyle(Qt.PenStyle.DashLine)
            pen.setWidthF(1.0)
        self._previewItem.setPen(pen)

    def _setPreviewImages(self, imageObjects: list) -> None:
        """设置图片预览项：传入一组经过变换后的 ImageObject（建议是 clone），
        用与 2D 画布完全相同的 ImageGraphicsItem 渲染管线把它们作为预览画出来，
        保证图片移动/旋转/缩放预览就是「图片自己」。

        - 每次调用会先清理旧预览图片项；
        - 传入空列表等价于「清空图片预览」；
        - 非 ImageObject 会被自动跳过。"""
        if self._view is None:
            return
        sc = self._view.scene()
        # 清旧
        for it in self._previewImages:
            try:
                sc.removeItem(it)
            except Exception:
                pass
        self._previewImages.clear()
        if not imageObjects:
            return
        from core.vector.image_object import ImageObject as _ImageObject
        from ui.view_2d.items.image_item import ImageGraphicsItem as _ImageGraphicsItem
        for obj in imageObjects:
            if not isinstance(obj, _ImageObject):
                continue
            try:
                pm = obj.pixmap()
                if pm.isNull():
                    continue
                item = _ImageGraphicsItem(obj)
                # 预览项：不可选、不响应 hover（避免出现选中虚线框干扰）
                item.setFlag(_ImageGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
                item.setAcceptHoverEvents(False)
                item.setZValue(9998)   # 略低于路径虚线，保证四角矩形轮廓仍可见
                sc.addItem(item)
                self._previewImages.append(item)
            except Exception:
                continue

    def _markerRadius(self) -> float:
        """光标点半径（世界坐标）：固定屏幕尺寸，不随视图缩放。"""
        from ui.view_2d.item_factory import POINT_MARKER_PX
        scale = 1.0
        if self._view is not None:
            scale = max(self._view.transform().m11(), 1e-9)
        return POINT_MARKER_PX / scale

    def _setCursorMarker(self, wp) -> None:
        """在光标处显示与点对象同样式的光标点（白色实心圆）。"""
        if self._markerItem is None:
            return
        r = self._markerRadius()
        self._markerItem.setRect(wp.x - r, wp.y - r, r * 2, r * 2)
        self._markerItem.show()

    def _hideCursorMarker(self) -> None:
        if self._markerItem is not None:
            self._markerItem.hide()

    def _clearPreview(self) -> None:
        if self._view is not None:
            sc = self._view.scene()
            if self._previewItem is not None:
                sc.removeItem(self._previewItem)
            if self._markerItem is not None:
                sc.removeItem(self._markerItem)
            for it in self._previewImages:
                try:
                    sc.removeItem(it)
                except Exception:
                    pass
        self._previewItem = None
        self._markerItem = None
        self._previewImages.clear()

    def previewPath(self):
        """当前预览路径（世界 XY 坐标），供 3D/浮雕视口叠加绘制；无预览返回 None。"""
        if self._previewItem is None:
            return None
        path = self._previewItem.path()
        return None if path.isEmpty() else path

    def previewPen(self):
        """当前预览画笔（颜色/线型/线宽），供 3D 视口叠加绘制复用 2D 预览样式。"""
        if self._previewItem is None:
            return None
        return self._previewItem.pen()

    def needsCursorMarker(self) -> bool:
        """命令是否需要 3D 视口在光标处绘制预览点（白底实心圆）。
        逐点交互类命令默认 True；推拉等不需要的覆盖为 False。"""
        return True

    def previewMarker(self):
        """光标预览点几何（圆心、半径）；未显示返回 None。供 3D 视口绘制。"""
        if self._markerItem is None or not self._markerItem.isVisible():
            return None
        r = self._markerItem.rect()
        return (r.center().x(), r.center().y(), r.width() * 0.5)

    @abstractmethod
    def onMousePress(self, wp: Point3D, mods) -> None: ...

    @abstractmethod
    def onMouseMove(self, wp: Point3D, mods) -> None: ...

    def _viewAxes(self):
        """当前视图绘制平面的世界轴 (u, v)。2D 画布/顶视/透视 = XY；
        3D 视口（canvasMode=="3d"）前视 = XZ、右视 = YZ。
        优先查询模式视口（真正可见的活动画布）；其余画布模式（浮雕等）
        固定 XY——取点/预览几何都落在 XY 平面。"""
        for v in (getattr(self, "_modeView", None), self._view):
            if v is None:
                continue
            cm = getattr(v, "canvasMode", None)
            if cm is not None:
                try:
                    if v.canvasMode() != "3d":
                        continue   # 该视口非 3D 画布：固定 XY，交给下一个候选
                except Exception:
                    continue
            vm = getattr(v, "viewMode", None)
            if vm is None:
                continue
            try:
                m = int(vm())
            except Exception:
                continue
            if m == 2:
                return (0, 2)
            if m == 3:
                return (1, 2)
            return (0, 1)
        return (0, 1)

    def _isSideView(self) -> bool:
        """当前是否在 3D 视口侧视图（前视/右视）绘图：
        形状基元（圆/弧/椭圆）是 XY 平面对象，需按视图平面烘焙成曲线。"""
        return self._viewAxes() != (0, 1)

    def _uv(self, p: Point3D):
        """世界点在当前视图绘制平面上的 (u, v) 分量。"""
        ax = self._viewAxes()
        return self._comp(p, ax[0]), self._comp(p, ax[1])

    def _uvDist(self, p: Point3D, q: Point3D) -> float:
        """两点在当前视图绘制平面 (u,v) 上的距离（侧视画圆/椭圆度量）。"""
        ax = self._viewAxes()
        return math.hypot(self._comp(p, ax[0]) - self._comp(q, ax[0]),
                          self._comp(p, ax[1]) - self._comp(q, ax[1]))

    def _planePts(self, pts) -> List[Point3D]:
        """世界点列 → 当前视图平面坐标点列（x=u, y=v, z=平面外分量）。
        供 XY 平面数学工具（三点解弧等）在侧视平面复用。"""
        ax = self._viewAxes()
        w = 3 - ax[0] - ax[1]
        return [Point3D(self._comp(p, ax[0]), self._comp(p, ax[1]),
                        self._comp(p, w)) for p in pts]

    def _planeToWorld(self, pts, anchor: Point3D) -> List[Point3D]:
        """视图平面 (u,v) 点列 → 世界坐标点列。
        平面外分量 w 取锚点的对应分量（顶视/透视=构造平面 Z，
        前视/右视=所在侧视平面常量 0）。"""
        ax = self._viewAxes()
        w = self._comp(anchor, 3 - ax[0] - ax[1])
        return [self._mkUVW(ax, p.x, p.y, w) for p in pts]

    def _uvPath(self, pts) -> QPainterPath:
        """世界点列 → 当前视图平面 (u,v) 坐标的预览路径
        （ax=(0,1) 时与 _linePath 等价）。"""
        path = QPainterPath()
        if not pts:
            return path
        u0, v0 = self._uv(pts[0])
        path.moveTo(u0, v0)
        for p in pts[1:]:
            u, v = self._uv(p)
            path.lineTo(u, v)
        return path

    @staticmethod
    def _mkUVW(axes, u: float, v: float, w: float) -> Point3D:
        """由 (u,v) + 平面外分量 w 组装世界点。"""
        c = [0.0, 0.0, 0.0]
        c[axes[0]], c[axes[1]] = u, v
        c[3 - axes[0] - axes[1]] = w
        return Point3D(c[0], c[1], c[2])

    def _comp(self, p: Point3D, i: int) -> float:
        return p.x if i == 0 else (p.y if i == 1 else p.z)

    def _orthoPoint(self, wp: Point3D, origin: Point3D) -> Point3D:
        """正交约束：若开启，把 wp 约束到 origin 的水平/垂直方向。
        按当前视图绘制平面 (u,v) 度量（前视水平=X、垂直=Z）。"""
        from core.grid_manager import GridManager
        if not GridManager.instance().isOrthoEnabled():
            return wp
        ax = self._viewAxes()
        dx = self._comp(wp, ax[0]) - self._comp(origin, ax[0])
        dy = self._comp(wp, ax[1]) - self._comp(origin, ax[1])
        w = 3 - ax[0] - ax[1]
        if abs(dx) > abs(dy):
            # 水平：v 锁 origin，u 随鼠标
            return self._mkUVW(ax, self._comp(wp, ax[0]),
                               self._comp(origin, ax[1]), self._comp(wp, w))
        # 垂直：u 锁 origin，v 随鼠标
        return self._mkUVW(ax, self._comp(origin, ax[0]),
                           self._comp(wp, ax[1]), self._comp(wp, w))

    def onMouseRelease(self, mods) -> None:
        pass

    def onRightClick(self, wp: Point3D) -> bool:
        return False

    def showSnapHints(self) -> bool:
        """激活命令时是否在视口显示吸附气泡标记。默认 True；

        点击即生效型（如区域浮雕）可在子类返回 ``False``，避免鼠标旁出现
        吸附气泡干扰查看模型。
        """
        return True

    def wantsSelectionOnPress(self) -> bool:
        """命令是否希望把左键按下让给画布做点选/框选（而非交给命令）。
        默认 False；需在活动期间复用画布选择逻辑的命令（如阵列选取对象）
        可按当前状态返回 True。"""
        return False

    def onEnter(self) -> bool:
        return False

    def onKeyPress(self, key, mods) -> bool:
        """命令激活期间的键盘事件（如画线 C 键闭合）；返回 True 表示已消费。"""
        return False

    def allowsSelectionDelete(self) -> bool:
        """Del 键删除选中对象是否被允许（命令激活期间默认禁止——命令
        可能正持有目标对象引用，删除会破坏其内部状态）。

        "点击即生效、保持激活可连续操作"的命令（挤出/布尔等）在空闲
        态（无任何被引用的目标/预览）应返回 True：它们的应用结果会被
        自动选中，若一律禁止 Del，用户会碰到"实体选中却删不掉"。"""
        return False

    def cancel(self) -> None:
        self._state = CommandState.CANCELED
        self._clearPreview()

    def finish(self) -> None:
        self._state = CommandState.COMPLETE
        self._clearPreview()
        # 面板确认按钮等路径直接调 finish，不经鼠标/键盘转发，
        # 需主动告知调度中心收尾（收起参数面板、发结束信号）
        if self._manager is not None:
            self._manager._checkDone()

    def paramPanel(self):
        """懒创建并返回命令的参数面板（QWidget）；无面板返回 None。

        面板生命周期由宿主容器接管：命令结束后宿主负责移除/隐藏，
        命令实例自身不销毁面板（同一命令重启时还可复用引用）。
        """
        return None

    def commandTitle(self) -> str:
        """参数面板标题，默认取命令名。"""
        return self.name()

    def _cursorMarker(self, wp: Point3D) -> QPainterPath:
        """光标位置预览圆路径（仅供需要合并进路径的场合；
        常规光标点请用 _setCursorMarker 独立绘制）。"""
        r = self._markerRadius()
        path = QPainterPath()
        path.addEllipse(wp.x - r, wp.y - r, r * 2, r * 2)
        return path

    @abstractmethod
    def prompt(self) -> str: ...

    @abstractmethod
    def name(self) -> str: ...

    def lastInputPoint(self):
        """当前命令已输入的最后一个点（用于垂足/切线等需参考点的捕捉）。
        默认返回 None，各命令按需覆盖。"""
        return None

    def previewObjectIds(self) -> set:
        """当前命令正在构建的预览对象 ID 集合（应从捕捉候选中完全排除）。
        默认收集 _obj / _waveObj 等常见属性，子类可覆盖。
        画圆等不应吸附自身的命令保持此默认；画直线/曲线等希望吸附自身
        非顶点位置的命令应返回空集并改用 partialSnapObjectIds。"""
        ids: set = set()
        for attr in ("_obj", "_waveObj"):
            o = getattr(self, attr, None)
            if o is not None and hasattr(o, "id"):
                ids.add(o.id())
        return ids

    def partialSnapObjectIds(self) -> set:
        """应“部分排除”的对象 ID 集合：仍作为捕捉候选参与计算，但
        excludedSnapTypesForPartial() 中的吸附类型对它们不生效。
        用于画直线/曲线时：自身参与中点/最近点/垂足吸附，但排除端点/顶点
        （避免新点与起点或已画线段的顶点重合）。默认空集。"""
        return set()

    def excludedSnapTypesForPartial(self) -> set:
        """对 partialSnapObjectIds() 对象要排除的吸附类型集合。
        默认排除端点/顶点/节点（即“起点和已画线段的点”）。"""
        from core.snap.snap_types import SnapType
        return {SnapType.ENDPOINT, SnapType.VERTEX, SnapType.POINT}

    def extraSnapObjects(self) -> list:
        """额外加入捕捉候选的对象（不在文档中、仅用于吸附）。
        如画曲线时已画段未入文档，此处提供临时代理对象供吸附。
        默认空列表。"""
        return []
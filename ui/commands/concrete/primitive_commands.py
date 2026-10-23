"""3D 标准体命令：立方体/圆柱体/圆环/圆锥体（圆台）两段式放置。

"先落点、再调参、后确认"交互：点「标准体」按钮进入放置态 → 画布单击
落点锚定（进入调整态）→ 预览固定在锚点，调面板参数实时刷新（可旋转
视图随意观察，不跟随鼠标）→ 右键/回车/「创建」生成 BRepObject（一次
撤销），回到放置态可继续放置下一个；调整态再单击 = 移动锚点位置；
「结束」/Esc 退出命令。

几何走 BrepShape.fromPrimitive（build123d Box/Cylinder/Cone/Torus +
taper 拔模 + 边筛选倒角/圆角），生成对象与挤出体同管线（渲染/拾取/
STL/STEP 导出零额外适配）。仅 3D 模式可用（mode_policy 门禁）。
"""
from __future__ import annotations
import math
from typing import Optional

from PySide6.QtCore import QTimer

from core.vector.geometry_types import Point3D
from core.vector.brep_object import BRepObject
from core.vector.brep_shape import BrepShape, bdKernelReady, bdKernelError
from ..command_instance import CommandInstance
from ..param_panels import PrimitiveParamPanel


class PrimitiveCommand(CommandInstance):
    """标准体：实时预览跟随光标 → 单击放置（可连放）→ 右键结束。"""

    def __init__(self) -> None:
        super().__init__()
        self._panel: PrimitiveParamPanel | None = None
        self._previewObj: Optional[BRepObject] = None
        self._anchor: Optional[Point3D] = None   # 已放置锚点（None=放置态）
        self._kernelWaitHinted = False
        self._kernelRetryPending = False

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        super().start()
        clr = getattr(self._view, "clearSnapMarker", None)
        if callable(clr):
            clr()
        self._anchor = None
        self._previewObj = None

    def paramPanel(self):
        if self._panel is None:
            self._panel = PrimitiveParamPanel()
            self._panel.confirmed.connect(self._confirm)   # 创建 = 确认生成
            self._panel.finished.connect(self.finish)      # 结束命令
            self._panel.paramsChanged.connect(self._updatePreview)
        return self._panel

    # ---------------- 参数 ----------------

    def _clampedParams(self) -> dict:
        return dict(self._panel.params())

    def _buildAt(self, cx: float, cy: float, p: dict) -> BRepObject:
        p2 = dict(p)
        p2["cx"] = float(cx)
        p2["cy"] = float(cy)
        shape = BrepShape.fromPrimitive(p2["kind"], p2)
        brep = BRepObject()
        brep.setShape(shape)
        return brep

    # ---------------- 预览 ----------------

    def _updatePreview(self) -> None:
        """重建锚点处预览（调整态）。放置态（未落点）无预览——
        预览固定在锚点，不跟随鼠标，方便边调参边旋转观察。"""
        self._previewObj = None
        if self._manager is not None:
            self._manager.invalidatePreview()
        if self._panel is None or self._anchor is None:
            return
        if not bdKernelReady():
            # 内核预热中跳过构建（绝不等待模块锁），就绪后定时补建
            if not self._kernelWaitHinted:
                self._kernelWaitHinted = True
                err = bdKernelError()
                if err is not None:
                    self._notify("3D 内核不可用：%s（详见 crash.log）" % err)
                else:
                    self._notify("几何内核预热中，预览稍候自动出现（仅首次启动）")
            if not self._kernelRetryPending:
                self._kernelRetryPending = True
                QTimer.singleShot(400, self._kernelRetryPreview)
            return
        p = self._clampedParams()
        try:
            obj = self._buildAt(self._anchor.x, self._anchor.y, p)
            # 预览用粗网格保流畅（生成对象重新构建，走默认精度）
            try:
                obj.shape().setMeshQuality(tol=0.3)
            except Exception:
                pass
            self._previewObj = obj
        except Exception as e:
            self._notify(f"标准体构建失败：{e}")

    def _kernelRetryPreview(self) -> None:
        self._kernelRetryPending = False
        if bdKernelReady() and self._anchor is not None:
            self._updatePreview()

    def previewSolid(self):
        return self._previewObj

    # ---------------- 输入 ----------------

    def onMouseMove(self, wp, mods) -> None:
        """预览固定在锚点：鼠标移动不跟随（放置态无预览，不需要处理）。"""
        pass

    def onMousePress(self, wp, mods) -> None:
        """单击 = 放置/移动锚点（进入调整态：调参实时预览，右键确认）。"""
        if self._panel is None:
            self.paramPanel()
        self._anchor = Point3D(float(wp.x), float(wp.y), 0.0)
        self._updatePreview()
        if self._anchor is not None:
            self._notify("已放置：调参数实时预览，右键/回车/「创建」生成"
                         "（再单击 = 移动位置）")

    def _confirm(self) -> None:
        """确认生成：锚点处按当前参数构建实体（一次撤销），
        回到放置态可继续放置下一个。"""
        if self._panel is None or self._anchor is None:
            self._notify("请先在画布上单击放置标准体位置")
            return
        if not bdKernelReady():
            err = bdKernelError()
            if err is not None:
                self._notify("3D 内核不可用：%s（详见 crash.log）" % err)
            else:
                self._notify("几何内核预热中，请稍候再创建（仅首次启动）")
            return
        p = self._clampedParams()
        try:
            obj = self._buildAt(self._anchor.x, self._anchor.y, p)
        except Exception as e:
            self._notify(f"创建失败：{e}")
            return
        self._addObject(obj, p)
        # 回到放置态：预览清掉，等待下一次落点
        self._anchor = None
        self._previewObj = None
        if self._manager is not None:
            self._manager.invalidatePreview()

    def _addObject(self, obj: BRepObject, p: dict) -> None:
        doc = self._doc
        if doc is None:
            return
        doc.beginEdit()
        try:
            doc.addObject(obj)
            doc.selectObject(obj.id())
            # 首次渲染预热：立即构建三角剖分/特征边缓存
            try:
                obj.mesh()
                obj.topology()
            except Exception:
                pass
            name = PrimitiveParamPanel.SHAPE_NAMES.get(p["kind"], "标准体")
            self._notify(f"已创建{name}（Z 基准 {p['cz']:.2f}）")
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._notify(f"创建失败：{e}")
        finally:
            try:
                doc.endEdit()
            except Exception:
                pass

    def onRightClick(self, wp) -> bool:
        # 调整态：右键 = 确认生成；放置态：右键 = 结束命令
        if self._anchor is not None:
            self._confirm()
        else:
            self.finish()
        return True

    def onEnter(self) -> bool:
        return self.onRightClick(None)
        return True

    # ---------------- 视口协作 ----------------

    def needsCursorMarker(self) -> bool:
        return False

    def showSnapHints(self) -> bool:
        return False

    def allowsSelectionDelete(self) -> bool:
        """命令保持激活且结果被自动选中——必须允许 Del 删除。"""
        return True

    # ---------------- 提示 ----------------

    def _notify(self, text: str) -> None:
        if self._manager is not None:
            try:
                self._manager.notifyResult(str(text))
            except Exception:
                pass

    def prompt(self) -> str:
        if self._anchor is not None:
            return ("已放置：调参数实时预览（预览固定在落点，旋转视图"
                    "随意观察）；单击=移动位置，右键/回车/「创建」生成，"
                    "结束/Esc 退出")
        return ("单击画布放置标准体位置，然后调参数实时预览，"
                "右键/回车/「创建」生成（右键/Esc 结束）")

    def name(self) -> str:
        return "Primitive"

    def commandTitle(self) -> str:
        return "标准体"


def registerAll(manager) -> None:
    manager.register("Primitive", PrimitiveCommand)

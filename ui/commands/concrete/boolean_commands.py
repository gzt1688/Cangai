"""3D 布尔命令：选两个实体 → 并/差/交实时预览 → 确认应用（build123d 内核）。

交互（顺序敏感：差集 = 第一个实体 − 第二个实体）：
1. 点击第一个实体（基体 A，橙色轮廓）
2. 点击第二个实体（工具体 B，蓝色轮廓）→ 立即按当前操作预览
3. 并/差/交随时切换（面板单选 / 键盘 1/2/3），后台线程重建实时预览；
   Tab / 面板「对调 A/B」交换两实体（差集方向调转）；
   预览态拖动工具体三向箭头（红X/绿Y/蓝Z）实时调整位置；
   预览态点击其他实体可换工具体 B
4. 右键/回车/面板「确认执行」= 应用（A 替换为结果，B 默认删除，
   可勾选保留；B 拖动后的位置保留；一次撤销）。应用后回到第 1 步
   可连续布尔。Esc 取消布尔（拖动偏移只在应用/对调时写入实体）。

几何：core/vector/brep_ops.booleanOp（OCP BRepAlgoAPI Fuse/Cut/Common）。
预览重建在后台线程（布尔数百毫秒级，主线程只收结果），与推拉同款
合并式调度：只算最新一次请求。工具体移动用「基准 part + 偏移」虚拟
平移（build123d 平移只挂 Location，零重算）；偏移仅在应用/对调时
烘焙进实体，取消零成本还原。
"""
from __future__ import annotations
import math
import threading
import time
from typing import Optional

from PySide6.QtCore import QObject, Signal as _Signal, Qt
from PySide6.QtGui import QPen, QColor, QFont, QPolygonF
from PySide6.QtCore import QPointF

from core.vector.geometry_types import Point3D, Color3D, ObjectType
from core.vector.brep_shape import BrepShape
from core.vector.brep_object import BRepObject
from core.vector.brep_ops import booleanOp, meshToSolid
from ui.commands.command_instance import CommandInstance

_OPS = ("union", "cut", "common")
_OP_LABELS = ("并集", "差集", "交集")
_COL_A = "#ff9800"     # 基体 A 轮廓（橙）
_COL_B = "#42a5f5"     # 工具体 B 轮廓（蓝）
_COL_HOVER = "#ffffff" # 悬停实体轮廓（白）
_MAX_OVERLAY_PTS = 4000   # 轮廓叠加层单对象投影点上限（防大网格卡帧）

# 工具体移动手柄（与推拉手柄同款屏幕交互规则）
_AXES = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
_AXIS_LABELS = ("X", "Y", "Z")
_AXIS_COLORS = ("#e53935", "#43a047", "#1e88e5")   # 红 X / 绿 Y / 蓝 Z
_AXIS_HOVER_COLORS = ("#ff8a80", "#a5d6a7", "#90caf9")
_GIZMO_LEN = 25.0     # 箭头长度（世界单位）
_GRAB_PX = 11.0       # 箭头尖端抓取圆命中半径（屏幕 px）
_HEAD_PX = 9.0        # 箭头头部屏幕长度


class _PreviewDoneSignal(QObject):
    done = _Signal(object)


class Boolean3DCommand(CommandInstance):
    """两实体布尔（并/差/交），实时预览，确认应用。"""

    def __init__(self) -> None:
        super().__init__()
        self._a: Optional[BRepObject] = None   # 基体（结果宿主）
        self._b: Optional[BRepObject] = None   # 工具体（默认应用后删除）
        self._stage = "A"                      # A=选基体 B=选工具 PREVIEW=预览
        self._op = 0                           # 0=并 1=差 2=交
        self._keepTool = False                 # 应用后保留工具体
        self._hover: Optional[BRepObject] = None
        self._previewObj: Optional[BRepObject] = None
        self._previewVol: Optional[float] = None
        self._panel = None
        # 工具体移动：基准 part + 虚拟偏移（应用/对调时才烘焙进实体）
        self._bPart0 = None
        self._bOffset = (0.0, 0.0, 0.0)
        self._bCenter0 = None       # 基准包围盒中心（手柄锚点）
        self._bPolys0 = None        # 基准特征边折线（叠加绘制用，画时加偏移）
        # 拖拽状态（-1 = 未拖拽）
        self._dragAxis = -1
        self._dragBase = 0.0
        self._dragP0 = 0.0
        self._dragS0 = (0.0, 0.0)
        self._dragU = (1.0, 0.0)
        self._dragScale = 1.0
        self._gzHover = -1
        # 后台布尔（合并式：只算最新请求）
        self._bgLock = threading.Lock()
        self._bgPending = None
        self._bgBusy = False
        self._bgKey = None                     # 已入队/已应用的请求键
        self._bgSig = _PreviewDoneSignal()
        self._bgSig.done.connect(self._onBgPreviewDone)

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        super().start()
        self._stage = "A"
        self._a = self._b = None
        self._hover = None
        self._previewObj = None
        # 参数面板必须在 start() 内建好：宿主只在命令激活瞬间挂载一次
        self.paramPanel()
        # 布尔作用于整体实体：强制体过滤（面/线过滤下点选仍按体处理）
        try:
            vp = self._viewport()
            vp.setSelectionMode("body")
        except Exception:
            pass
        # 便捷：启动时恰好选中 2 个 B-Rep 实体 → 直接进预览
        # （文档顺序：第一个=基体，第二个=工具体）
        if self._doc is not None:
            sel = [o for o in self._doc.selectedObjects()
                   if isinstance(o, BRepObject) and not o.isLocked()
                   and isinstance(o.shape(), BrepShape)]
            if len(sel) >= 2:
                self._a, self._b = sel[0], sel[1]
                self._enterPreview("已按选中顺序取前两个实体（差集=第一个−第二个）")
        self._hideCursorMarker()
        self._notify("点击第一个实体（基体）" if self._stage == "A" else "")

    def prompt(self) -> str:
        if self._stage == "A":
            return "布尔：点击第一个实体（基体），Esc 结束"
        if self._stage == "B":
            return "布尔：点击第二个实体（工具体；差集 = 第一个 − 第二个）"
        return ("布尔预览：1/2/3 切换并/差/交，拖箭头移动工具体（蓝），"
                "Tab/面板对调 A/B，点击其他实体换工具体，右键/确认应用")

    def name(self) -> str:
        return "Boolean3D"

    def commandTitle(self) -> str:
        return "布尔"

    def allowsSelectionDelete(self) -> bool:
        """空闲态（选基体阶段：未持有任何操作数/预览）允许 Del 删除选中
        对象——布尔应用后结果被自动选中且命令保持激活可连续操作，
        一律禁止会让"结果实体选中却删不掉"。"""
        return self._stage == "A"

    def needsCursorMarker(self) -> bool:
        return False

    def showSnapHints(self) -> bool:
        return False

    def _viewport(self):
        """活动 3D 视口（同 PushPull：3D 模式必须用 _modeView）。"""
        mv = getattr(self, "_modeView", None)
        if mv is not None and mv is not self._view:
            cm = getattr(mv, "canvasMode", None)
            if callable(cm):
                try:
                    if cm() == "3d":
                        return mv
                except Exception:
                    pass
        return self._view

    # ---------------- 鼠标 ----------------

    def onMouseMove(self, wp, mods) -> None:
        if self._view is None:
            return
        sp = self._screenPoint(wp)
        if sp is None:
            return
        mx, my = sp
        # 拖拽箭头移动工具体（最优先）
        if self._dragAxis >= 0:
            self._updateDrag(mx, my)
            return
        hit = self._pickBody(mx, my)
        if hit != self._hover:
            self._hover = hit
        # 手柄悬停高亮（预览态才有手柄）
        self._gzHover = (self._hitGizmoAxis(mx, my)
                         if self._stage == "PREVIEW" else -1)
        try:
            self._viewport().update()
        except Exception:
            pass

    def onMousePress(self, wp, mods) -> None:
        if self._view is None:
            return
        sp = self._screenPoint(wp)
        if sp is None:
            return
        mx, my = sp
        # 预览态：命中移动手柄 → 开始拖拽（优先于换工具体）
        if self._stage == "PREVIEW":
            axi = self._hitGizmoAxis(mx, my)
            if axi >= 0:
                self._beginDrag(axi, mx, my)
                self._notify(f"拖动{_AXIS_LABELS[axi]}箭头移动工具体，松开结束")
                return
        hit = self._pickBody(mx, my)
        if hit is None:
            if self._stage == "A":
                self._notify("未命中实体：请点击 3D 实体（挤出/螺纹等）")
            return
        if self._stage == "A":
            if not self._setOperandA(hit):
                return
            self._stage = "B"
            self._notify("基体已选（橙色轮廓），点击第二个实体")
        elif self._stage == "B":
            if hit is self._a:
                self._notify("已选为基体，请点击另一个实体作工具体")
                return
            if not self._setOperandB(hit):
                return
            self._enterPreview()
        else:  # PREVIEW：点击其他实体 = 换工具体 B
            if hit is self._a or hit is self._b:
                return
            if not self._setOperandB(hit):
                return
            self._schedulePreview(force=True)
            self._notify("已更换工具体（蓝色轮廓）")
        try:
            self._viewport().update()
        except Exception:
            pass

    def onMouseRelease(self, mods) -> None:
        """结束箭头拖拽：强制精确重建一次（去抖期间可能漏了末梢位置）。"""
        if self._dragAxis >= 0:
            self._dragAxis = -1
            self._schedulePreview(force=True)
            try:
                self._viewport().update()
            except Exception:
                pass

    def onRightClick(self, wp) -> bool:
        if self._stage == "PREVIEW":
            self._apply()
            return True
        if self._stage == "B":
            # 撤销基体选择，回到第一步
            self._a = None
            self._stage = "A"
            self._notify("已取消基体，点击第一个实体")
            return True
        self.finish()
        return True

    def onEnter(self) -> bool:
        return self.onRightClick(None)

    def onKeyPress(self, key, mods) -> bool:
        """预览态：1/2/3 切换操作，Tab 交换 A/B。"""
        if self._stage != "PREVIEW":
            return False
        if key == Qt.Key.Key_1:
            self._setOp(0)
            return True
        if key == Qt.Key.Key_2:
            self._setOp(1)
            return True
        if key == Qt.Key.Key_3:
            self._setOp(2)
            return True
        if key == Qt.Key.Key_Tab:
            self._swapOperands()
            return True
        return False

    def cancel(self) -> None:
        self._stage = "A"   # 作废在途布尔结果（_onBgPreviewDone 的守卫键）
        self._bgKey = None
        self._bgPending = None
        self._restoreVisible()
        super().cancel()

    def finish(self) -> None:
        self._stage = "A"
        self._bgKey = None
        self._bgPending = None
        self._restoreVisible()
        super().finish()

    # ---------------- 操作数 ----------------

    def _setOperandA(self, obj) -> bool:
        if not self._prepareOperand(obj):
            return False
        self._a = obj
        return True

    def _setOperandB(self, obj) -> bool:
        if not self._prepareOperand(obj):
            return False
        self._b = obj
        return True

    def _prepareOperand(self, obj) -> bool:
        """操作数就绪检查：BRepObject + BrepShape；旧 PolySolid 升级。"""
        if not isinstance(obj, BRepObject):
            self._notify("布尔只能作用于 3D 实体（挤出/螺纹等）")
            return False
        if obj.isLocked():
            self._notify("实体已锁定，无法参与布尔")
            return False
        shape = obj.shape()
        if shape is None:
            self._notify("该实体无几何，无法参与布尔")
            return False
        if isinstance(shape, BrepShape):
            return True
        # 旧轻量内核（PolySolid）：三角网格缝合升级为 B-Rep（一次性）
        try:
            mesh = shape.triangulate()
            if mesh is None or len(mesh["tris"]) == 0:
                raise ValueError("无网格")
            if len(mesh["tris"]) > 20000:
                self._notify("旧实体网格过密，无法快速转换；请用新版挤出重建该实体")
                return False
            part = meshToSolid(mesh)
            obj.setShape(BrepShape.fromPart(part))
            self._notify("已把旧实体升级为 B-Rep 内核（一次转换，可撤销内）")
            return True
        except Exception as e:
            self._notify(f"旧实体转换失败：{e}")
            return False

    def _enterPreview(self, tip: str = "") -> None:
        self._stage = "PREVIEW"
        self._captureToolGeom()
        self.paramPanel()   # 激活参数面板（命令宿主在 start 后拉取）
        self._schedulePreview(force=True)
        if tip:
            self._notify(tip)

    # ---------------- 工具体移动（虚拟偏移） ----------------

    def _captureToolGeom(self) -> None:
        """捕获当前工具体 B 的基准几何：part / 包围盒中心 / 特征边折线。

        之后移动只改 _bOffset（基准 part 不可变，平移=挂 Location 零重算），
        应用/对调时才烘焙进实体，取消零成本还原。
        """
        self._bOffset = (0.0, 0.0, 0.0)
        self._dragAxis = -1
        self._gzHover = -1
        if self._b is None:
            self._bPart0 = None
            self._bCenter0 = None
            self._bPolys0 = None
            return
        self._bPart0 = self._b.shape().part()
        try:
            bb = self._bPart0.bounding_box()
            self._bCenter0 = (
                (float(bb.min.X) + float(bb.max.X)) / 2.0,
                (float(bb.min.Y) + float(bb.max.Y)) / 2.0,
                (float(bb.min.Z) + float(bb.max.Z)) / 2.0)
        except Exception:
            self._bCenter0 = (0.0, 0.0, 0.0)
        try:
            self._bPolys0 = self._b.topology()["edgePolylines"] or []
        except Exception:
            self._bPolys0 = []

    def _movedBPart(self):
        """基准 part + 当前偏移 → 实际参与布尔的工具体 part。"""
        if self._bPart0 is None:
            return None
        o = self._bOffset
        if abs(o[0]) + abs(o[1]) + abs(o[2]) < 1e-9:
            return self._bPart0
        return self._bPart0.translate(o)

    def _swapOperands(self) -> None:
        """对调基体/工具体（面板按钮与 Tab 共用）。

        拖拽偏移先烘焙进原工具体实体（setShape，隐藏中一次性写入），
        保证交换后两个实体位置不变、新工具偏移归零。
        """
        if self._stage != "PREVIEW" or self._a is None or self._b is None:
            return
        if any(abs(v) > 1e-9 for v in self._bOffset):
            try:
                self._b.setShape(BrepShape.fromPart(self._movedBPart()))
            except Exception:
                pass
        self._a, self._b = self._b, self._a
        self._captureToolGeom()
        self._schedulePreview(force=True)
        self._notify("已对调：新基体=橙色，新工具体=蓝色")

    # ---------------- 移动手柄（三向箭头） ----------------

    def _gizmoAxisScreens(self, view=None):
        """三轴箭头屏幕几何 [(s0, tip_world, s1, unit, scale)]。

        与轴平行视线（屏幕投影退化）或投影失败返回 None 占位；
        s0 = 锚点屏幕（随偏移移动），s1 = 箭头尖端屏幕，scale = px/世界单位。
        """
        if view is None:
            view = self._viewport()
        if view is None or self._bCenter0 is None:
            return [None, None, None]
        try:
            cam = view._camera()
            w, h = view.width(), view.height()
        except Exception:
            return [None, None, None]
        c = (self._bCenter0[0] + self._bOffset[0],
             self._bCenter0[1] + self._bOffset[1],
             self._bCenter0[2] + self._bOffset[2])
        s0 = view._project(c, cam, w, h)
        if s0 is None:
            return [None, None, None]
        out = []
        for ax in _AXES:
            tip = (c[0] + ax[0] * _GIZMO_LEN,
                   c[1] + ax[1] * _GIZMO_LEN,
                   c[2] + ax[2] * _GIZMO_LEN)
            s1 = view._project(tip, cam, w, h)
            if s1 is None:
                out.append(None)
                continue
            vx, vy = s1[0] - s0[0], s1[1] - s0[1]
            vl = math.hypot(vx, vy)
            if vl < 8.0:   # 视线与轴近平行：拖不动，不显示
                out.append(None)
                continue
            out.append(((s0[0], s0[1]), tip, (s1[0], s1[1]),
                        (vx / vl, vy / vl), vl / _GIZMO_LEN))
        return out

    def _hitGizmoAxis(self, mx: float, my: float) -> int:
        """箭头尖端抓取圆命中（最近轴，半径 _GRAB_PX）；无命中 -1。"""
        best, best_d = -1, _GRAB_PX
        for i, info in enumerate(self._gizmoAxisScreens()):
            if not info:
                continue
            s1 = info[2]
            d = math.hypot(mx - s1[0], my - s1[1])
            if d < best_d:
                best, best_d = i, d
        return best

    def _beginDrag(self, axi: int, mx: float, my: float) -> None:
        info = self._gizmoAxisScreens()[axi]
        if not info:
            return
        (s0, _tip, s1, u, scale) = info
        self._dragAxis = axi
        self._dragS0 = s0
        self._dragU = u
        self._dragScale = scale if scale > 1e-9 else 1.0
        self._dragP0 = (mx - s0[0]) * u[0] + (my - s0[1]) * u[1]
        self._dragBase = self._bOffset[axi]
        self._gzHover = axi

    def _updateDrag(self, mx: float, my: float) -> None:
        """鼠标投影到轴方向 → 世界偏移（屏幕 px 按手柄深度比例换算）。"""
        p = ((mx - self._dragS0[0]) * self._dragU[0]
             + (my - self._dragS0[1]) * self._dragU[1])
        world = self._dragBase + (p - self._dragP0) / self._dragScale
        o = list(self._bOffset)
        o[self._dragAxis] = world
        self._bOffset = tuple(o)
        self._schedulePreview()
        if self._manager is not None:
            self._manager.invalidatePreview()
        try:
            self._viewport().update()
        except Exception:
            pass

    # ---------------- 拾取 ----------------

    def _screenPoint(self, wp):
        vp = self._viewport()
        cam = vp._camera()
        w, h = vp.width(), vp.height()
        pr = vp._project((wp.x, wp.y, wp.z), cam, w, h)
        return (pr[0], pr[1]) if pr is not None else None

    def _pickBody(self, mx: float, my: float):
        try:
            return self._viewport()._pickObject(mx, my)
        except Exception:
            return None

    # ---------------- 后台预览重建 ----------------

    def _schedulePreview(self, force: bool = False) -> None:
        """合并式后台布尔：只保留最新请求，主线程立即返回。

        工具体用「基准 part + 偏移」虚拟平移（build123d translate 只挂
        Location，无重算），偏移参与去重键：拖动时仅位置变也触发重建。
        """
        if self._a is None or self._b is None:
            return
        part_b = self._movedBPart()
        if part_b is None:
            return
        key = (id(self._a.shape().part()), id(self._bPart0), self._op,
               round(self._bOffset[0], 2), round(self._bOffset[1], 2),
               round(self._bOffset[2], 2))
        if not force and key == self._bgKey:
            return
        self._bgKey = key
        with self._bgLock:
            self._bgPending = (self._a.shape().part(), part_b,
                               _OPS[self._op], key)
        if not self._bgBusy:
            self._bgBusy = True
            threading.Thread(target=self._bgLoop, daemon=True).start()

    def _bgLoop(self) -> None:
        while True:
            with self._bgLock:
                job = self._bgPending
                self._bgPending = None
            if job is None:
                self._bgBusy = False
                return
            part_a, part_b, op, key = job
            try:
                res = booleanOp(part_a, part_b, op)
                bs = BrepShape.fromPart(res)
                try:
                    self._bgSig.done.emit((key, bs, None))
                except RuntimeError:
                    return   # 命令已销毁
            except Exception as e:
                try:
                    self._bgSig.done.emit((key, None, str(e)))
                except RuntimeError:
                    return

    def _onBgPreviewDone(self, payload) -> None:
        key, bs, err = payload
        if self._stage != "PREVIEW" or key != self._bgKey:
            return   # 过期结果（已切换操作/换体/退出预览）
        if bs is None:
            self._clearPreview()
            self._restoreVisible()
            self._notify(f"布尔失败：{err}")
            return
        prev = BRepObject()
        prev.setShape(bs)
        prev.setColor(Color3D(0x15 / 255.0, 0xB6 / 255.0, 0xC9 / 255.0, 0.55))
        self._previewObj = prev
        try:
            self._previewVol = float(bs.volume())
        except Exception:
            self._previewVol = None
        # 预览期间隐藏 A/B：预览体=两实体运算后的效果
        self._setVisible(self._a, False)
        self._setVisible(self._b, False)
        if self._manager is not None:
            self._manager.invalidatePreview()
        try:
            self._viewport().update()
        except Exception:
            pass

    def _clearPreview(self) -> None:
        self._previewObj = None
        self._previewVol = None

    def _setVisible(self, obj, v: bool) -> None:
        if obj is not None:
            try:
                obj.setVisible(v)
            except Exception:
                pass

    def _restoreVisible(self) -> None:
        """退出预览/命令：恢复 A/B 显示，清预览。"""
        self._setVisible(self._a, True)
        self._setVisible(self._b, True)
        self._clearPreview()

    def previewSolid(self):
        return self._previewObj

    # ---------------- 应用 ----------------

    def _apply(self) -> None:
        if self._a is None or self._b is None:
            return
        if self._previewObj is None:
            self._notify("预览未就绪，请稍候再确认")
            return
        shape = self._previewObj.shape()
        a, b = self._a, self._b
        va = 0.0
        try:
            va = float(a.shape().volume())
        except Exception:
            pass
        vr = self._previewVol if self._previewVol is not None else 0.0
        doc = self._doc
        doc.beginEdit()
        try:
            a.setShape(shape)
            try:
                a.mesh()
                a.topology()   # 首次渲染预热
            except Exception:
                pass
            if not self._keepTool:
                doc.removeObject(b.id())
            elif any(abs(v) > 1e-9 for v in self._bOffset):
                # 保留工具体：把拖动后的位置烘焙进实体（隐藏中一次性写入）
                try:
                    b.setShape(BrepShape.fromPart(self._movedBPart()))
                except Exception:
                    pass
            doc.selectObject(a.id())
        except Exception as e:
            self._notify(f"应用失败：{e}")
            return
        finally:
            try:
                doc.endEdit()
            except Exception:
                pass
        self._notify(f"{_OP_LABELS[self._op]}完成：体积 {va:.3g} → {vr:.3g}"
                     + ("（工具体已保留）" if self._keepTool else ""))
        # 回到选基体态，可连续布尔
        self._restoreVisible()
        self._a = self._b = None
        self._hover = None
        self._stage = "A"
        if self._manager is not None:
            self._manager.invalidatePreview()
        try:
            self._viewport().update()
        except Exception:
            pass

    # ---------------- 参数面板 ----------------

    def paramPanel(self):
        if self._panel is None:
            from ..param_panels import BooleanParamPanel
            self._panel = BooleanParamPanel()
            self._panel.opChanged.connect(self._onPanelOp)
            self._panel.swapRequested.connect(self._swapOperands)
            self._panel.keepToolChanged.connect(self._onPanelKeep)
            self._panel.confirmed.connect(self._apply)
        return self._panel

    def _onPanelOp(self, op: int) -> None:
        self._setOp(op)

    def _onPanelKeep(self, keep: bool) -> None:
        self._keepTool = keep

    def _setOp(self, op: int) -> None:
        if op == self._op:
            return
        self._op = op
        panel = getattr(self, "_panel", None)
        if panel is not None:
            panel.setOp(op)
        self._schedulePreview(force=True)
        self._notify(f"已切换为{_OP_LABELS[op]}")

    # ---------------- 叠加绘制（轮廓 + 手柄 + 状态标签） ----------------

    def drawOverlay(self, painter, view) -> None:
        """3D 视口叠加层：A 橙 / B 蓝 / 悬停白 轮廓 + 工具体移动手柄 +
        左上角操作/体积/偏移标签。"""
        if view is None:
            return
        cam = view._camera()
        w, h = view.width(), view.height()
        if self._stage == "A":
            if self._hover is not None:
                self._drawPolys(painter, view, cam, w, h,
                                self._hover.topology()["edgePolylines"],
                                (0.0, 0.0, 0.0), _COL_HOVER)
        elif self._stage == "B":
            self._drawPolys(painter, view, cam, w, h,
                            self._a.topology()["edgePolylines"],
                            (0.0, 0.0, 0.0), _COL_A)
            if self._hover is not None and self._hover is not self._a:
                self._drawPolys(painter, view, cam, w, h,
                                self._hover.topology()["edgePolylines"],
                                (0.0, 0.0, 0.0), _COL_HOVER)
        else:  # PREVIEW：A/B 已隐藏，画轮廓指明各自角色（B 用基准+偏移）
            self._drawPolys(painter, view, cam, w, h,
                            self._a.topology()["edgePolylines"],
                            (0.0, 0.0, 0.0), _COL_A)
            self._drawPolys(painter, view, cam, w, h,
                            self._bPolys0 or [], self._bOffset, _COL_B)
            self._drawGizmo(painter, view)
        # 左上角状态标签：操作 + 体积（预览就绪时）+ 拖动偏移
        if self._stage == "PREVIEW":
            vol = (f"体积 {self._previewVol:.4g}"
                   if self._previewVol is not None else "计算中…")
            extra = ""
            if any(abs(v) > 1e-9 for v in self._bOffset):
                extra = "  移动 ({:+.3g}, {:+.3g}, {:+.3g})".format(
                    *self._bOffset)
            painter.setFont(QFont("Arial", 10))
            painter.setPen(QPen(QColor("#15b6c9"), 1.0))
            painter.drawText(14, 22,
                             f"{_OP_LABELS[self._op]}预览  {vol}{extra}"
                             "（1/2/3 切换，拖箭头移动，右键应用）")

    def _drawPolys(self, painter, view, cam, w, h, polys, off, col) -> None:
        """特征边折线 + 偏移 → 屏幕线段（带点数预算，防大网格卡帧）。"""
        if not polys:
            return
        pen = QPen(QColor(col), 1.6)
        pen.setCosmetic(True)
        painter.setPen(pen)
        budget = _MAX_OVERLAY_PTS
        for poly in polys:
            if budget <= 0:
                break
            n = len(poly)
            if n < 2:
                continue
            step = max(1, math.ceil(n / min(n, budget)))
            pts = []
            k = 0
            while k < n - 1:
                p = view._project((poly[k][0] + off[0], poly[k][1] + off[1],
                                   poly[k][2] + off[2]), cam, w, h)
                if p is not None:
                    pts.append((p[0], p[1]))
                k += step
            p = view._project((poly[n - 1][0] + off[0],
                               poly[n - 1][1] + off[1],
                               poly[n - 1][2] + off[2]), cam, w, h)
            if p is not None:
                pts.append((p[0], p[1]))
            budget -= len(pts)
            if len(pts) >= 2:
                for i in range(len(pts) - 1):
                    painter.drawLine(int(pts[i][0]), int(pts[i][1]),
                                     int(pts[i + 1][0]), int(pts[i + 1][1]))

    def _drawGizmo(self, painter, view) -> None:
        """工具体三向移动箭头（红X/绿Y/蓝Z），尖端抓取圆，悬停/拖拽高亮。"""
        if self._bCenter0 is None:
            return
        for i, info in enumerate(self._gizmoAxisScreens(view)):
            if not info:
                continue
            (s0, _tip, s1, u, _scale) = info
            active = (i == self._gzHover or i == self._dragAxis)
            col = _AXIS_HOVER_COLORS[i] if active else _AXIS_COLORS[i]
            pen = QPen(QColor(col), 2.4 if active else 1.8)
            pen.setCosmetic(True)
            painter.setPen(pen)
            # 箭头线（留出头部）+ 头部三角
            hx, hy = s1[0] - u[0] * _HEAD_PX, s1[1] - u[1] * _HEAD_PX
            painter.drawLine(QPointF(s0[0], s0[1]), QPointF(hx, hy))
            painter.setBrush(QColor(col))
            painter.drawPolygon(QPolygonF([
                QPointF(s1[0], s1[1]),
                QPointF(hx - u[1] * _HEAD_PX * 0.5, hy + u[0] * _HEAD_PX * 0.5),
                QPointF(hx + u[1] * _HEAD_PX * 0.5, hy - u[0] * _HEAD_PX * 0.5),
            ]))
            # 尖端抓取圆（拖拽命中区）：空心环 + 中心点
            gr = _GRAB_PX * 0.7
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QPointF(s1[0], s1[1]), gr, gr)
            painter.setBrush(QColor(col))
            painter.drawEllipse(QPointF(s1[0], s1[1]), gr * 0.4, gr * 0.4)
            # 轴名标签（拖拽中显示该轴偏移量）
            painter.setFont(QFont("Arial", 9))
            painter.setPen(QPen(QColor(col), 1.0))
            label = _AXIS_LABELS[i]
            if i == self._dragAxis:
                label += f" {self._bOffset[i]:+.3g}"
            painter.drawText(QPointF(s1[0] + u[0] * 12, s1[1] + u[1] * 12),
                             label)

    # ---------------- 工具 ----------------

    def _notify(self, text: str) -> None:
        if text and self._manager is not None:
            try:
                self._manager.notifyResult(str(text))
            except Exception:
                pass


def registerAll(manager) -> None:
    manager.register("Boolean3D", Boolean3DCommand)

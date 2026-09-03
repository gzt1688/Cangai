"""浮雕命令：NewModel（新建浮雕模型）+ ReconstructRelief（重构网格）
+ RegionRelief（区域浮雕）。

NewModel：在 2D 里选中若干曲线 → 点“新建模型” → 面板按选中曲线包围盒 + 余量 +
精度建一个平坦高度场（HeightField）→ 包成 ReliefModelObject(SURFACE) 加入文档并选中，
留在 2D（用户摆放好后再选中并切到浮雕模式进入编辑态；曲线与模型不绑定）。

ReconstructRelief：对选中的浮雕模型把放置变换（移动/缩放/旋转）烘焙进网格（重采样）
并复位 _place，可选新尺寸/新精度（唯一有损步骤，可撤销）。
"""
from __future__ import annotations

import numpy as np

from core.vector.geometry_types import BoundingBox3D, ObjectType
from core.relief import HeightField
from core.relief.region_relief import apply_region_relief, sample_curve_points
from core.vector.relief_model_object import ReliefModelObject
from ui.widgets.paint_state import PaintState
from ..command_instance import CommandInstance
from ..param_panels import (NewModelParamPanel, ReconstructParamPanel,
                            RegionReliefParamPanel)


class NewModelCommand(CommandInstance):
    """按选中曲线包围盒 + 余量创建高度场的命令（“选择 + 参数”型，不做画布取点）。"""

    def __init__(self) -> None:
        super().__init__()
        self._panel: NewModelParamPanel | None = None
        self._bbox = None   # (minX, minY, maxX, maxY) 世界 mm；无选中曲线为 None

    def start(self) -> None:
        super().start()
        self._bbox = self._computeBBox()

    def _computeBBox(self):
        """合并选中对象包围盒（取 XY，忽略 Z）；无有效选择返回 None。"""
        if self._doc is None:
            return None
        sel = self._doc.selectedObjects()
        if not sel:
            return None
        bb = BoundingBox3D()
        for obj in sel:
            ob = obj.getBoundingBox()
            if ob is None or not ob.isValid():
                continue
            bb.expand(ob.min)
            bb.expand(ob.max)
        if not bb.isValid():
            return None
        return (bb.min.x, bb.min.y, bb.max.x, bb.max.y)

    def paramPanel(self):
        if self._panel is None:
            self._panel = NewModelParamPanel()
            self._panel.confirmed.connect(self._create)
            if self._bbox is not None:
                self._panel.setBBoxSize(self._bbox[2] - self._bbox[0],
                                        self._bbox[3] - self._bbox[1])
            else:
                self._panel.setBBoxSize(0.0, 0.0)
        return self._panel

    def _create(self) -> None:
        # 以当前选择为准重算包围盒（防止面板创建后选择变化）
        bbox = self._computeBBox()
        if bbox is None or self._panel is None:
            return   # 无选中曲线：不创建（prompt 已提示），命令保持活动
        hf = HeightField.from_bbox((bbox[0], bbox[1]), (bbox[2], bbox[3]),
                                   self._panel.margin(), self._panel.spacing())
        base = self._panel.baseHeight()
        if base:
            hf.data[:] = base
        if self._doc is not None:
            # 建为一等文档对象（SURFACE）。新建即进入该模型的浮雕编辑态：
            # 命令结束后由 mainWindow 调用 _enterReliefEdit 锁定/脱离选中/
            # 绑定视口，并在浮雕模式下于 3D 视口以等高显示（曲线与模型解耦）。
            obj = ReliefModelObject.fromHeightField(hf)
            self._doc.addObject(obj)
            self._doc.selectObject(obj.id())
        self.finish()

    # 本命令为“选择 + 参数”型，不消费画布取点
    def onMousePress(self, wp, mods) -> None:
        pass

    def onMouseMove(self, wp, mods) -> None:
        pass

    def prompt(self) -> str:
        if self._bbox is None:
            return "请先选择曲线，再新建模型"
        return "设置余量/精度后点“确认创建”"

    def name(self) -> str:
        return "NewModel"

    def commandTitle(self) -> str:
        return "新建模型"


class ReconstructRelief(CommandInstance):
    """重构浮雕模型网格：把放置变换烘焙进网格（重采样）并复位 _place。

    “选择 + 参数”型命令，不消费画布取点；面板实时预估新栅格/内存，确认后烘焙。
    文档修改由 CommandManager 在命令生命周期内统一 beginEdit/endEdit（一次撤销）。
    """

    def __init__(self) -> None:
        super().__init__()
        self._panel: ReconstructParamPanel | None = None

    def paramPanel(self):
        if self._panel is None:
            self._panel = ReconstructParamPanel()
            self._panel.confirmed.connect(self._reconstruct)
        self._populate()   # 每次激活都按当前选中模型刷新
        return self._panel

    def _populate(self) -> None:
        model = self._doc.activeReliefModel() if self._doc is not None else None
        if model is None or self._panel is None:
            return
        hf = model.heightField()
        bb = model.getBoundingBox()
        curW = max(bb.max.x - bb.min.x, 0.0)
        curH = max(bb.max.y - bb.min.y, 0.0)
        self._panel.setModelInfo(curW, curH, hf.nx, hf.ny, hf.spacing)

    def _reconstruct(self) -> None:
        if self._doc is None or self._panel is None:
            return
        model = self._doc.activeReliefModel()
        if model is None:
            return   # 无选中模型：不重构（prompt 已提示），命令保持活动
        spacing = self._panel.spacing()
        matrix = None
        if not self._panel.fitCurrent():
            # 自定义目标尺寸：在当前世界 AABB 基础上再缩放（关于中心）后烘焙
            bb = model.getBoundingBox()
            curW = max(bb.max.x - bb.min.x, 1e-6)
            curH = max(bb.max.y - bb.min.y, 1e-6)
            tw, th = self._panel.targetSize()
            sx = max(float(tw), 1e-6) / curW
            sy = max(float(th), 1e-6) / curH
            cx = (bb.max.x + bb.min.x) * 0.5
            cy = (bb.max.y + bb.min.y) * 0.5
            S = np.eye(3, dtype=np.float64)
            S[0, 0] = sx
            S[1, 1] = sy
            S[0, 2] = cx - sx * cx
            S[1, 2] = cy - sy * cy
            matrix = S @ model.placement()
        model.bake(spacing, matrix=matrix)
        self._doc.notifyObjectChanged(model.id())
        self.finish()

    # 本命令为“选择 + 参数”型，不消费画布取点
    def onMousePress(self, wp, mods) -> None:
        pass

    def onMouseMove(self, wp, mods) -> None:
        pass

    def prompt(self) -> str:
        model = self._doc.activeReliefModel() if self._doc is not None else None
        if model is None:
            return "请先在 2D 选中浮雕模型，再重构网格"
        return "设置精度/尺寸后点“确认重构”（把放置变换烘焙进网格）"

    def name(self) -> str:
        return "ReconstructRelief"

    def commandTitle(self) -> str:
        return "重构网格"


def _pointPolylineDistance(x: float, y: float, pts: np.ndarray) -> float:
    """世界点到折线的最小欧氏距离（拾取用）。"""
    ax, ay = pts[:-1, 0], pts[:-1, 1]
    abx = pts[1:, 0] - ax
    aby = pts[1:, 1] - ay
    apx, apy = x - ax, y - ay
    len2 = abx * abx + aby * aby
    t = np.where(len2 > 1e-18, (apx * abx + apy * aby) / np.maximum(len2, 1e-18), 0.0)
    np.clip(t, 0.0, 1.0, out=t)
    dx = apx - t * abx
    dy = apy - t * aby
    return float(np.sqrt(np.min(dx * dx + dy * dy)))


class RegionReliefCommand(CommandInstance):
    """区域浮雕：选中闭合曲线 → 按截面参数在活动浮雕模型上起浮雕（复刻插件"曲面区域"页）。

    "选择 + 参数"型：命令激活即进入区域浮雕功能态——鼠标移动识别最近闭合曲线
    并以橙色描出预览，点击曲线即按当前参数起浮雕（免去 Rhino 里受限的"应用"按钮）。
    同一曲线重复点击会按新参数重做；命令不自动结束（右键/完成按钮结束，可连续对
    多条曲线起浮雕）。一次应用 = 一次可撤销操作。

    曲线为世界坐标，浮雕在高度场栅格上计算：模型若有未烘焙的放置变换会先烘焙
    对齐（与"进入浮雕编辑态"同一约定）；文档修改由 CommandManager 统一 beginEdit/
    endEdit，故一次应用 = 一次可撤销操作。
    """

    _PICK_PX = 8.0      # 点选容差（屏幕像素）

    def __init__(self) -> None:
        super().__init__()
        self._panel: RegionReliefParamPanel | None = None
        self._lastObjId = None       # 最近一条已起浮雕的曲线 id（调参重做用）
        self._lastDelta = None       # (indices, old_values) 该曲线上次写入的贡献

    # ---------------- 面板 ----------------

    def start(self) -> None:
        super().start()
        # 清掉上一操作的吸附气泡残留，区域浮雕不显示任何点/吸附预览
        clr = getattr(self._view, "clearSnapMarker", None)
        if callable(clr):
            clr()

    def paramPanel(self):
        if self._panel is None:
            self._panel = RegionReliefParamPanel()
            self._panel.finished.connect(self.finish)
            self._panel.paramsChanged.connect(self._refreshStatus)
        self._refreshStatus()
        return self._panel

    # ---------------- 查询 ----------------

    def activeModel(self):
        return self._doc.activeReliefModel() if self._doc is not None else None

    def _curveObjects(self) -> list:
        """当前选中的曲线对象（排除浮雕模型与锁定对象）。"""
        if self._doc is None:
            return []
        out = []
        for o in self._doc.selectedObjects():
            if o.type() == ObjectType.SURFACE or o.isLocked():
                continue
            if hasattr(o, "getSampledPoints") or hasattr(o, "toPolycurve"):
                out.append(o)
        return out

    # ---------------- 执行 ----------------

    def _applyToCurve(self, obj) -> None:
        """对单条闭合曲线用当前面板参数起浮雕（点击即执行）。

        同一曲线重复点击会先回滚其上次的贡献再按新参数重做（便于调参），
        不同曲线各自独立；一次应用 = 一次可撤销操作。

        本命令常驻（右键才结束、可连点多条曲线），而 CommandManager 只在整个
        命令生命周期 begin/end 一次，故这里每次应用单独 beginEdit/endEdit 出栈，
        否则整条命令的所有应用会被合并成一个撤销项、只能撤一步。
        """
        model = self.activeModel()
        if model is None:
            self._setStatus("请先在 2D 选中浮雕模型（区域浮雕作用于模型）", False)
            return
        if self._panel is None:
            return
        doc = self._doc
        if doc is None:
            return

        doc.beginEdit()
        try:
            params = self._panel.params()
            # 曲线是世界坐标：模型若有未烘焙的放置变换，先烘焙对齐（唯一有损步骤）
            if model.hasPendingTransform():
                model.bake()

            hf = model.heightField()
            # 同一曲线：先回滚上次写入，使本次按新参数干净重做（避免叠加变高）
            if self._lastObjId == obj.id() and self._lastDelta is not None:
                idx, old = self._lastDelta
                hf.data.ravel()[idx] = old
                self._lastDelta = None

            poly = sample_curve_points(obj, params.sample_count)
            if poly.shape[0] < 3:
                self._setStatus("曲线点数不足，无法起浮雕", False)
                return

            snap = hf.data.copy()
            moved = apply_region_relief(hf, poly, params,
                                        color_mask=model.colorMask(),
                                        color_value=PaintState.instance().currentValue())
            if moved > 0:
                flat = hf.data.ravel()
                sflat = snap.ravel()
                changed = flat != sflat
                self._lastObjId = obj.id()
                self._lastDelta = (np.nonzero(changed)[0], sflat[changed])
                model.bumpVersion()
                self._doc.notifyObjectChanged(model.id())
                self._setStatus(f"已起浮雕：{moved} 个顶点", True)
            else:
                # 未命中：本次无任何写入（含已回滚），清空记录
                self._lastObjId = None
                self._lastDelta = None
                self._setStatus("未命中区域：请确认曲线落在模型范围内", False)
        finally:
            doc.endEdit()

    def _setStatus(self, text: str, ok: bool = True) -> None:
        if self._panel is not None:
            self._panel.setStatus(text, ok)

    def _refreshStatus(self) -> None:
        model = self.activeModel()
        if model is None:
            self._setStatus("请先在 2D 选中浮雕模型", False)
            return
        self._setStatus("移动鼠标识别闭合曲线，点一下即起浮雕（右键结束命令）", True)

    # ---------------- 交互：画布识别闭合曲线并即时浮雕 ----------------

    def onMouseMove(self, wp, mods) -> None:
        """鼠标移动：识别最近闭合曲线并更新提示（不绘制光标点/吸附预览）。"""
        obj = self._pickClosedCurve(wp)
        if obj is not None:
            self._setStatus("点击曲线即可起浮雕（右键结束命令）", True)
        else:
            self._setStatus("移动鼠标识别闭合曲线，点一下即起浮雕（右键结束命令）", True)

    def onMousePress(self, wp, mods) -> None:
        """点击最近的闭合曲线即按当前参数起浮雕（无需应用按钮）。"""
        obj = self._pickClosedCurve(wp)
        if obj is None:
            return
        self._applyToCurve(obj)

    def onRightClick(self, wp) -> bool:
        """右键结束命令（2D 画布与 3D 视口一致：右键完成/结束当前命令）。"""
        self.finish()
        return True

    def onEnter(self) -> bool:
        """回车也结束命令（右键为主要结束方式，回车为等价快捷键）。"""
        self.finish()
        return True

    def showSnapHints(self) -> bool:
        """区域浮雕点击即起浮雕，无需精确吸附，关闭视口吸附气泡。"""
        return False

    def _pickClosedCurve(self, wp):
        """拾取最近的**闭合**曲线（世界点到折线最近距离在容差内）。

        闭合判定优先用对象的 isClosed()；圆/椭圆/弧等参数曲线若未直接暴露
        isClosed，则以采样点首尾是否重合（距离远小于曲线总长）识别闭合轮廓。
        """
        if self._doc is None:
            return None
        tol = self._pixelTolWorld()
        best, best_d = None, tol
        for o in self._doc.visibleObjects():
            if o.type() == ObjectType.SURFACE or o.isLocked():
                continue
            pts = sample_curve_points(o, 128)
            if pts.shape[0] < 3:
                continue
            # 闭合识别：对象显式 isClosed() 或采样首尾重合
            closed = getattr(o, "isClosed", None)
            if callable(closed):
                if not closed():
                    continue
            else:
                d_end = float(np.linalg.norm(pts[0] - pts[-1]))
                perim = float(np.linalg.norm(
                    np.diff(pts, axis=0), axis=1).sum())
                if perim > 1e-9 and d_end > 1e-3 * perim:
                    continue
            d = _pointPolylineDistance(wp.x, wp.y, pts)
            if d < best_d:
                best, best_d = o, d
        return best

    def _pixelTolWorld(self) -> float:
        """拾取容差：屏幕像素 → 世界 mm。"""
        try:
            s = abs(self._view.transform().m11())
            if s > 1e-9:
                return self._PICK_PX / s
        except Exception:
            pass
        return 1.0

    def prompt(self) -> str:
        if self.activeModel() is None:
            return "请先在 2D 选中浮雕模型"
        return "移动鼠标识别闭合曲线，点一下即起浮雕（右键结束命令）"

    def name(self) -> str:
        return "RegionRelief"

    def commandTitle(self) -> str:
        return "区域浮雕"


def registerAll(manager) -> None:
    manager.register("NewModel", NewModelCommand)
    manager.register("ReconstructRelief", ReconstructRelief)
    manager.register("RegionRelief", RegionReliefCommand)

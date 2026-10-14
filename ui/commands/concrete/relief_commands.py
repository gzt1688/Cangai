"""浮雕命令：NewModel（新建浮雕模型）+ ReconstructRelief（重构网格）
+ RegionRelief（区域浮雕）+ Stamp（冲压）+ Smooth（磨光）+ DeJitter（消除锯齿）。

NewModel：在 2D 里选中若干曲线 → 点“新建模型” → 面板按选中曲线包围盒 + 余量 +
精度建一个平坦高度场（HeightField）→ 包成 ReliefModelObject(SURFACE) 加入文档并选中，
留在 2D（用户摆放好后再选中并切到浮雕模式进入编辑态；曲线与模型不绑定）。

ReconstructRelief：对选中的浮雕模型把放置变换（移动/缩放/旋转）烘焙进网格（重采样）
并复位 _place，可选新尺寸/新精度（唯一有损步骤，可撤销）。

Stamp：冲压 + 高低层次调整（复刻插件“冲压页”并重设蒙版）。Rhino 插件用选中
闭合曲线的“曲线内/外”限定冲压范围；本应用改为二选一——颜色无效按**鼠标
圆圈**区域（A 缩小 / S 放大直径），颜色内按**按下处色号的整个色块区域**。
支持按住拖动连续冲压、按住不动持续冲压（150ms 一拍），一次笔画 = 一次撤销。

SweepMaterial：导动堆料（复刻插件“曲面导动”页并升级）。笔刷沿单击的曲线
扫掠，鼠标跟随笔刷圈，快捷键 A/S 直径、D/F 半衰直径、Q/W 高度实时调节；
效果二选一：累加（不断叠加）/ 流动叠加（与当前面取高包络，重复点击不变）；
作用范围按颜色裁剪。

Smooth：磨光笔刷。在模型上按住拖动，笔刷范围内的局部高低向邻域均值抹平；
方式三选一（去高补低/补低/取高），力度 = 预设档位单选 + 输入框实时联动，
快捷键 A/S 直径、D/F 半衰直径、Q/W 力度；作用范围按颜色裁剪。
"""
from __future__ import annotations

import gc
import math
import time

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QPainterPath

from core.vector.geometry_types import BoundingBox3D, ObjectType, Point3D
from core.relief import HeightField
from core.relief.region_relief import (apply_region_relief, sample_curve_points,
                                       build_inside_mask)
from core.relief.wall_curtain import (snap_band_to_platforms, DeJitterParams)
from core.relief.sweep import (apply_sweep, sample_sweep_path,
                                apply_brush_stamp, move_brush_region,
                                transform_region, level_calibrate,
                                PARAM_SCALE, HEIGHT_STEP,
                                MERGES, MERGE_LABELS,
                                APPLY_MODES, APPLY_LABELS)
from core.relief.region_array import (array_region_along_curve,
                                       preview_array)
from core.relief.region_symmetry import symmetry_field
from core.relief.region_deform import deform_region
from core.relief.region_slope import region_slope, split_sides
from core.relief.revolve_slope import revolve_slope
from core.relief.stamp import (apply_stamp, apply_level_adjust,
                               build_circle_mask, DIAMETER_SCALE,
                               DIAMETER_MIN, DIAMETER_MAX,
                               LEVEL_REGIONS, LEVEL_REGION_LABELS)
from core.relief.smooth import (apply_smooth_stamp, apply_sweep_smooth,
                                MODES, MODE_LABELS, STRENGTH_STEP)
from core.relief.textures import KIND_LABELS, apply_texture
from core.relief.slope_analysis import (ZONE_COLORS, ZONE_NAMES,
                                        rest_material_mask, mask_contours,
                                        slope_zone_masks)
from core.vector.poly_curve_object import PolyCurveObject
from core.vector.relief_model_object import ReliefModelObject, _paste_aligned
from ui.widgets.paint_state import PaintState
from ..command_instance import CommandInstance
from ..param_panels import (NewModelParamPanel, ReconstructParamPanel,
                            RegionReliefParamPanel, StampParamPanel,
                            SweepParamPanel, AddMaterialParamPanel,
                            SmoothParamPanel, DeJitterParamPanel,
                            MoveHeightParamPanel, RegionTransformParamPanel,
                            RegionArrayParamPanel,
                            RegionSymmetryParamPanel,
                            RegionDeformParamPanel,
                            LevelCalibrateParamPanel,
                            RegionSlopeParamPanel,
                            RevolveSlopeParamPanel,
                            ModelScaleParamPanel,
                            TextureFillParamPanel, SlopeZoneParamPanel,
                            RestBoundaryParamPanel)


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
        margin = self._panel.margin()
        model.bake(spacing, margin=margin)
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
    """区域塑形（原区域浮雕）：选中闭合曲线 → 按截面参数在活动浮雕模型上起浮雕（复刻插件"曲面区域"页）。

    "选择 + 参数"型：命令激活即进入区域浮雕功能态——鼠标点击闭合曲线即按当前
    参数在活动浮雕模型上起浮雕。支持"无限累加"：
        同一条曲线重复点击 N 次 = 在现有高度上再叠 N 次浮雕（blend=add 时），
        无需退出命令、也不会"先回滚再重做"。若想撤销某一次点击对应的那一层，
        直接 Ctrl+Z（每次点击都是一次独立的 beginEdit/endEdit 撤销栈）。

    命令不自动结束，右键/回车结束。
    """

    _PICK_PX = 8.0      # 点选容差（屏幕像素）

    def __init__(self) -> None:
        super().__init__()
        self._panel: RegionReliefParamPanel | None = None

    # ---------------- 面板 ----------------

    def start(self) -> None:
        super().start()
        self._lastWp = None
        self._baseSnap = None   # 命令内基准快照（首次塑形时取，命令内多次点选共用）
        clr = getattr(self._view, "clearSnapMarker", None)
        if callable(clr):
            clr()

    def paramPanel(self):
        if self._panel is None:
            self._panel = RegionReliefParamPanel()
            self._panel.finished.connect(self.finish)
        return self._panel

    # ---------------- 查询 ----------------

    def activeModel(self):
        return self._doc.activeReliefModel() if self._doc is not None else None

    def _notify(self, text: str) -> None:
        """结果反馈统一走画布左下角信息区（结果行，自动淡出）。"""
        if self._manager is not None:
            try:
                self._manager.notifyResult(str(text))
            except Exception:
                pass

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
        """对单条闭合曲线用当前面板参数起浮雕（点击即执行，可无限累加）。

        设计原则：
          * 永远不做"同曲线先回滚再重做"——这样同一条曲线反复点击就是纯累加
            (当 blend=add / subtract)，满足用户"无限累加 / 再点也有效"需求。
          * 每一次应用 = 一次独立的 beginEdit / endEdit（因为本命令常驻，
            CommandManager 只在命令生命周期包一次 begin/end），保证每一层
            浮雕都能 Ctrl+Z 单独撤销。
        """
        model = self.activeModel()
        if model is None:
            self._notify("请先选中浮雕模型（区域塑形作用于模型）")
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

            poly = sample_curve_points(obj, params.sample_count)
            if poly.shape[0] < 3:
                self._notify("曲线点数不足，无法起浮雕")
                return

            before = hf.data.copy()
            moved = apply_region_relief(hf, poly, params,
                                        color_mask=model.colorMask(),
                                        color_value=PaintState.instance().currentValue())
            if moved > 0:
                # 精确比较：是否真的变了（blend=replace 且新值等于旧值 也能正确识别）
                changed = not np.array_equal(before, hf.data)
                if changed:
                    model.bumpVersion()
                    try:
                        self._doc.notifyObjectChanged(model.id())
                    except Exception:
                        pass
                self._notify(f"已起浮雕（累加模式）：{moved} 个顶点")
            else:
                self._notify("未命中区域：请确认曲线落在模型范围内")
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._notify(f"区域塑形失败：{e}")
        finally:
            try:
                doc.endEdit()
            except Exception:
                pass

    # ---------------- 交互：画布识别闭合曲线并即时浮雕 ----------------

    def onMouseMove(self, wp, mods) -> None:
        pass

    def onMousePress(self, wp, mods) -> None:
        """点击最近的闭合曲线即按当前参数起浮雕（无需应用按钮）。

        未命中任何闭合曲线时给出明确提示，避免"点了像失效"。
        """
        obj = self._pickClosedCurve(wp)
        if obj is None:
            self._notify("未命中闭合曲线：请把鼠标移动到曲线上再点击（8 像素容差）")
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
            return "请先在 2D 选中浮雕模型（区域塑形作用于活动模型）"
        return "点击闭合曲线即起浮雕（同一条曲线反复点击会无限累加；右键/回车结束命令，Ctrl+Z 单独撤销每一层）"

    def name(self) -> str:
        return "RegionRelief"

    def commandTitle(self) -> str:
        return "区域塑形"


class DeJitterCommand(CommandInstance):
    """消除锯齿：点选闭合曲线，曲线两侧"距离"内的格点吸附到曲线（复刻插件
    ExecuteDeJitter 并改写到高度场）。

    交互与区域塑形一致："选择 + 参数"型——命令激活后在画布上点闭合曲线即
    对活动浮雕模型执行一次吸附；可连点多条曲线，右键/回车结束。每一次点击
    = 一次独立 beginEdit/endEdit（Ctrl+Z 单独撤销一次）。

    效果：把栅格量化产生的阶梯锯齿壁收为**贴着曲线的竖直光壁**——高度场
    带内钳为两侧平台高度（二值陡壁），同时把曲线登记为模型的幕帘：3D/STL
    网格构建时带内顶点 XY 投影到曲线（Z 不动），跨壁四边形退化为沿曲线的
    竖直幕帘，壁面完全竖直、法线沿曲线连续无竖棱（复刻 Rhino 插件顶点吸附；
    算法详见 core/relief/wall_curtain.py，重复点击同一条曲线幂等）。
    典型流程：冲压出台阶后，点轮廓线消除锯齿。
    """

    _PICK_PX = 8.0      # 点选容差（屏幕像素）

    def __init__(self) -> None:
        super().__init__()
        self._panel: DeJitterParamPanel | None = None

    # ---------------- 面板 ----------------

    def start(self) -> None:
        super().start()
        # 清掉上一操作的吸附气泡残留（点曲线即执行，无点/吸附预览）
        clr = getattr(self._view, "clearSnapMarker", None)
        if callable(clr):
            clr()

    def paramPanel(self):
        if self._panel is None:
            self._panel = DeJitterParamPanel()
            self._panel.finished.connect(self.finish)
        return self._panel

    # ---------------- 查询 ----------------

    def activeModel(self):
        return self._doc.activeReliefModel() if self._doc is not None else None

    def _notify(self, text: str) -> None:
        """结果反馈统一走画布左下角信息区（结果行，自动淡出）。"""
        if self._manager is not None:
            try:
                self._manager.notifyResult(str(text))
            except Exception:
                pass

    # ---------------- 执行 ----------------

    def _applyToCurve(self, obj) -> None:
        """对单条闭合曲线执行消除锯齿（点击即执行，可连点多条）。

        两层复刻 Rhino 竖直幕帘（详见 core/relief/wall_curtain.py）：
        1. 高度场：曲线两侧半径内格点钳为该侧平台高度（带内成干净二值陡壁）；
        2. 幕帘登记：曲线存入模型（local 坐标），3D/STL 网格构建时把带内
           顶点 XY 投影到曲线——跨壁四边形退化为沿曲线的竖直光壁。
        """
        model = self.activeModel()
        if model is None:
            self._notify("请先选中浮雕模型（消除锯齿作用于模型）")
            return
        if self._panel is None or self._doc is None:
            return
        doc = self._doc

        doc.beginEdit()
        try:
            params = self._panel.params()
            # 曲线是世界坐标：模型若有未烘焙的放置变换，先烘焙对齐
            if model.hasPendingTransform():
                model.bake()

            hf = model.heightField()
            poly_w = sample_curve_points(obj, params.sample_count)
            if poly_w.shape[0] < 3:
                self._notify("曲线点数不足，无法消除锯齿")
                return
            # world → local（幕帘与钳制都在模型 local 坐标系）
            M = np.asarray(model.placement(), dtype=np.float64)
            inv = np.linalg.inv(M)
            hom = np.concatenate([poly_w, np.ones((poly_w.shape[0], 1))], axis=1)
            poly_l = (hom @ inv.T)[:, :2]

            snapped = snap_band_to_platforms(hf, poly_l, params.distance)
            added = model.addCurtain(poly_l, params.distance)
            if added or snapped > 0:
                model.bumpVersion()
                try:
                    self._doc.notifyObjectChanged(model.id())
                except Exception:
                    pass
                if added:
                    self._notify(f"消除锯齿完成：轮廓已转为竖直光壁"
                                 f"（半径 {params.distance:g}mm）")
                else:
                    self._notify("该轮廓已消除过锯齿（壁面已为竖直光壁）")
            else:
                self._notify("未找到需处理的格点：请确认曲线落在模型上，"
                             "或增大锯齿半径")
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._notify(f"消除锯齿失败：{e}")
        finally:
            try:
                doc.endEdit()
            except Exception:
                pass

    # ---------------- 交互：画布识别闭合曲线并即时吸附 ----------------

    def onMouseMove(self, wp, mods) -> None:
        pass

    def onMousePress(self, wp, mods) -> None:
        """点击最近的闭合曲线即消除锯齿（无需应用按钮）。"""
        obj = self._pickClosedCurve(wp)
        if obj is None:
            self._notify("未命中闭合曲线：请把鼠标移动到曲线上再点击"
                         "（8 像素容差）")
            return
        self._applyToCurve(obj)

    def onRightClick(self, wp) -> bool:
        self.finish()
        return True

    def onEnter(self) -> bool:
        self.finish()
        return True

    def showSnapHints(self) -> bool:
        return False   # 本命令点曲线即执行，与 RegionRelief 一致，不启用取点吸附

    def prompt(self) -> str:
        if self.activeModel() is None:
            return "请先在 2D 选中浮雕模型（消除锯齿作用于活动模型）"
        return "点击闭合曲线即消除锯齿（栅格锯齿壁收为贴着曲线的竖直光壁；右键/回车结束）"

    def name(self) -> str:
        return "DeJitter"

    def commandTitle(self) -> str:
        return "消除锯齿"


class StampCommand(CommandInstance):
    """冲压：鼠标圆圈定区域，颜色模块选色块（复刻插件"冲压页"并重设蒙版）。

    交互：光标跟随一个可调直径的圆圈（预览），**A 缩小 / S 放大**直径；
    在浮雕模型上**按下开始冲压笔画**——
      - 拖动：沿路径按间距连续冲压（圆圈区域 / 颜色内整块）；
      - 按住不动：150ms 一拍持续冲压（规律重复盖章）；
      - 松开结束——一次笔画 = 一次可撤销操作。
      - 颜色无效：整个圆圈区域；颜色内：**按下处色号的整个色块区域**。
    绝对/相对、正/负方向取面板参数（绝对模式重复冲压幂等）。高低层次
    调整仍由面板按钮触发（作用区域以底部调色板当前颜色为参照）。命令
    常驻（右键/完成按钮结束）。

    面板只放参数不放按钮/提示：所有反馈经 CommandManager.notifyResult
    显示在画布左下角信息区。
    """

    AIR_MS = 150       # 按住不动的持续冲压节拍（毫秒）

    def __init__(self) -> None:
        super().__init__()
        self._panel: StampParamPanel | None = None
        self._lastWp = None    # 最近鼠标世界位置（圆圈跟随用）
        self._stroking = False     # 冲压笔画进行中（按下→拖动/按住→松开）
        self._lastStamp = None     # 最近一次盖章的世界坐标（弧长插值起点）
        self._stampColor = 0       # 颜色内模式按下处色号（笔画期间固定）
        self._totalCells = 0       # 本笔累计冲压格数（松开时反馈）
        self._lastNotify = 0.0     # 上次推进版本+通知视口的时刻（节流用）
        self._airTimer = None      # 持续冲压节拍定时器
        self._gcWasEnabled = True  # 笔画期 GC 暂停标记

    def start(self) -> None:
        super().start()
        self._lastWp = None
        self._stroking = False
        self._lastStamp = None
        # 清掉上一操作的吸附气泡残留，冲压不做任何画布取点
        clr = getattr(self._view, "clearSnapMarker", None)
        if callable(clr):
            clr()

    # ---------------- 面板 ----------------

    def paramPanel(self):
        if self._panel is None:
            self._panel = StampParamPanel()
            self._panel.finished.connect(self.finish)
            self._panel.adjustHighRequested.connect(
                lambda: self._applyLevelAdjust(self._panel.highLevel(), 0.0))
            self._panel.adjustLowRequested.connect(
                lambda: self._applyLevelAdjust(0.0, self._panel.lowLevel()))
            self._panel.adjustOverallRequested.connect(
                lambda: self._applyLevelAdjust(self._panel.highLevel(),
                                               self._panel.lowLevel()))
        return self._panel

    # ---------------- 查询 ----------------

    def activeModel(self):
        return self._doc.activeReliefModel() if self._doc is not None else None

    def _notify(self, text: str) -> None:
        """所有反馈统一走画布左下角信息区（结果行，自动淡出）。"""
        if self._manager is not None:
            self._manager.notifyResult(text)

    # ---------------- 圆圈预览（跟随光标，定冲压区域大小） ----------------

    def _radius(self) -> float:
        return max(0.05, self._panel.diameter() * 0.5) if self._panel is not None \
            else 5.0

    def _refreshCircle(self) -> None:
        """按最近鼠标位置与当前直径刷新圆圈预览（世界 XY，经视口投影叠加绘制）。"""
        from PySide6.QtCore import QRectF
        if self._lastWp is None or self._previewItem is None:
            return
        r = self._radius()
        path = QPainterPath()
        path.addEllipse(QRectF(self._lastWp.x - r, self._lastWp.y - r,
                               r * 2.0, r * 2.0))
        self._setPreviewPath(path, solid=True)
        if self._manager is not None:
            self._manager.invalidatePreview()

    def _scaleDiameter(self, factor: float) -> None:
        """A/S 快捷键：直径按 factor 缩放（钳制范围），同步面板输入框与圆圈。"""
        if self._panel is None:
            return
        d = max(DIAMETER_MIN, min(DIAMETER_MAX, self._panel.diameter() * factor))
        self._panel.setDiameter(d)
        self._refreshCircle()

    # ---------------- 执行 ----------------

    def _beginApply(self):
        """校验活动模型并开始一次独立撤销会话；返回 (doc, model) 或 None。"""
        model = self.activeModel()
        if model is None:
            self._notify("请先在浮雕模式选中浮雕模型（冲压作用于模型）")
            return None
        if self._panel is None or self._doc is None:
            return None
        self._doc.beginEdit()
        # 模型若有未烘焙的放置变换，先烘焙对齐（并入本次撤销会话，与区域浮雕一致）
        if model.hasPendingTransform():
            model.bake()
        return self._doc, model

    def _applyLevelAdjust(self, high: float, low: float) -> None:
        """层次调整：高层/底层/整体三个按钮共用（与插件 ApplyLevelAdjust 一致）。"""
        begun = self._beginApply()
        if begun is None:
            return
        doc, model = begun
        try:
            region = self._panel.levelRegion()
            mid = self._panel.midPercent()
            changed = apply_level_adjust(model.heightField(), high, low, mid,
                                         region=region,
                                         color_mask=model.colorMask(),
                                         color_value=PaintState.instance().currentValue())
            if changed:
                model.bumpVersion()
                doc.notifyObjectChanged(model.id())
                desc = LEVEL_REGION_LABELS[LEVEL_REGIONS.index(region)] \
                    if region in LEVEL_REGIONS else "全部"
                self._notify("层次调整完成：%d 格（高 %g，低 %g，中 %g%%，%s）"
                             % (changed, high, low, mid, desc))
            else:
                self._notify("没有可调整的格子：模型无高度差或区域内无格子")
        finally:
            doc.endEdit()

    # ---------------- 交互：圆圈跟随 + 拖动/按住持续冲压 + A/S 调径 ----------------

    def onMousePress(self, wp, mods) -> None:
        """按下开始冲压笔画：拖动沿路径连续冲压，按住不动持续冲压
        （150ms 一拍），松开结束——一次笔画 = 一次可撤销操作。"""
        model = self.activeModel()
        if model is None:
            self._notify("请先在浮雕模式选中浮雕模型（冲压作用于模型）")
            return
        if self._panel is None or self._doc is None:
            return
        hf = model.heightField()
        r = self._radius()
        ox, oy = hf.origin
        color = self._cellColorAt(model, wp)
        if self._panel.scope() == "inside":
            # 颜色内：取按下处色号的整个色块（笔画期间目标色块固定）
            if color is None:
                self._notify("请点击模型上的颜色区域")
                return
        else:
            # 颜色无效：圆圈与模型包围盒相交才尝试（圆心可略出界，蹭边也能冲到）
            if not (ox - r <= wp.x <= ox + (hf.nx - 1) * hf.spacing + r
                    and oy - r <= wp.y <= oy + (hf.ny - 1) * hf.spacing + r):
                self._notify("请点击模型范围（圆圈需覆盖模型）")
                return
        self._beginStampStroke(model, wp, color if color is not None else 0)

    def _beginStampStroke(self, model, wp, color_value: int) -> None:
        """开始冲压笔画：开启撤销会话并立即冲第一下，同时启动持续冲压节拍。"""
        if model.hasPendingTransform():
            model.bake()
        # 与雕刻笔刷同因：笔画期暂停循环 GC，防偶发长帧（模型"闪一下"）
        gc.collect()
        self._gcWasEnabled = gc.isenabled()
        gc.disable()
        self._doc.beginEdit()
        self._stroking = True
        self._stampColor = color_value
        self._totalCells = 0
        self._lastWp = wp
        self._lastStamp = (wp.x, wp.y)
        self._stampOnce(model, wp)
        self._touchModel(model, force=True)
        self._ensureAirTimer().start()

    def _stampOnce(self, model, wp) -> None:
        """按当前面板参数冲压一次（拖动盖章与气刷节拍共用）。"""
        if self._panel is None:
            return
        if self._panel.scope() == "inside":
            circle = None                 # 颜色内：整个色块（目标色块笔画期间固定）
            color_value = self._stampColor
        else:
            hf = model.heightField()
            circle = build_circle_mask(hf, (wp.x, wp.y), self._radius())
            color_value = 0
        params = self._panel.stampParams()
        changed = apply_stamp(model.heightField(), params, circle,
                              color_mask=model.colorMask(),
                              color_value=color_value)
        self._totalCells += changed
        if changed:
            self._touchModel(model)

    def _touchModel(self, model, force: bool = False) -> None:
        """笔画期间节流推进版本+通知视口重绘（≈30fps），松开时强制刷新。"""
        now = time.monotonic()
        if force or (now - self._lastNotify) >= 0.033:
            model.bumpVersion()
            self._doc.notifyObjectChanged(model.id())
            self._lastNotify = now

    # ---------------- 气刷：按住不动持续冲压 ----------------

    def _ensureAirTimer(self):
        if self._airTimer is None:
            from PySide6.QtCore import QTimer
            self._airTimer = QTimer()
            self._airTimer.setInterval(self.AIR_MS)
            self._airTimer.timeout.connect(self._airbrushTick)
        return self._airTimer

    def _airbrushTick(self) -> None:
        """冲压节拍：按住不动也持续冲压（150ms 一次，规律重复盖章）。"""
        if not self._stroking or self._lastWp is None:
            return
        model = self.activeModel()
        if model is None or self._panel is None or self._doc is None:
            return
        self._stampOnce(model, self._lastWp)

    def _endStampStroke(self) -> None:
        """结束冲压笔画（强制刷新视口后撤销会话出栈，反馈结果）。"""
        if not self._stroking:
            return
        self._stroking = False
        self._lastStamp = None
        if self._airTimer is not None:
            self._airTimer.stop()
        if self._doc is not None:
            model = self.activeModel()
            if model is not None:
                self._touchModel(model, force=True)
            self._doc.endEdit()
        if getattr(self, "_gcWasEnabled", True) and not gc.isenabled():
            gc.enable()
        if self._totalCells:
            params = self._panel.stampParams() if self._panel is not None else None
            if params is not None and params.mode == "relative":
                desc = ("相对（%s）" %
                        ("正方向" if params.direction == "positive"
                         else "负方向"))
            else:
                desc = "绝对"
            value = params.value if params is not None else 0.0
            self._notify("冲压完成：%d 格（%s，值 %g）" % (self._totalCells, desc, value))
        else:
            self._notify("未命中格子：范围内取值无变化")
        self._totalCells = 0

    def _cellColorAt(self, model, wp):
        """世界点处格子的色号；模型范围外返回 None（格子在但未填色为 0）。"""
        hf = model.heightField()
        ox, oy = hf.origin
        col = int(round((wp.x - ox) / hf.spacing))
        row = int(round((wp.y - oy) / hf.spacing))
        if not (0 <= row < hf.ny and 0 <= col < hf.nx):
            return None
        cm = model.colorMask()
        if cm is not None and cm.shape == (hf.ny, hf.nx):
            return int(cm[row, col])
        return 0

    def onMouseMove(self, wp, mods) -> None:
        """圆圈跟随光标；笔画中拖动按间距连续冲压。"""
        self._lastWp = wp
        self._refreshCircle()
        if not self._stroking or self._lastStamp is None:
            return
        model = self.activeModel()
        if model is None or self._panel is None or self._doc is None:
            return
        lx, ly = self._lastStamp
        dx, dy = wp.x - lx, wp.y - ly
        dist = math.hypot(dx, dy)
        spacing = max(self._radius() * 0.5, model.heightField().spacing, 1e-6)
        if dist < spacing:
            return
        n = int(dist // spacing)
        ux, uy = dx / dist, dy / dist
        for i in range(1, n + 1):
            self._stampOnce(model, Point3D(lx + ux * spacing * i,
                                           ly + uy * spacing * i, 0.0))
        self._lastStamp = (lx + ux * spacing * n, ly + uy * spacing * n)

    def onMouseRelease(self, mods) -> None:
        """松开左键：结束冲压笔画（一次笔画 = 一次可撤销操作）。"""
        self._endStampStroke()

    def onKeyPress(self, key, mods) -> bool:
        """A 缩小直径 / S 放大直径（±10%）。"""
        if key == Qt.Key_A and mods == Qt.NoModifier:
            self._scaleDiameter(1.0 / DIAMETER_SCALE)
            return True
        if key == Qt.Key_S and mods == Qt.NoModifier:
            self._scaleDiameter(DIAMETER_SCALE)
            return True
        return False

    def onRightClick(self, wp) -> bool:
        """右键结束命令（与区域浮雕一致）。"""
        self._endStampStroke()
        self.finish()
        return True

    def onEnter(self) -> bool:
        self._endStampStroke()
        self.finish()
        return True

    def cancel(self) -> None:
        self._endStampStroke()
        super().cancel()

    def finish(self) -> None:
        self._endStampStroke()
        super().finish()

    def showSnapHints(self) -> bool:
        """圆圈即定位预览，无需精确吸附，关闭视口吸附气泡。"""
        return False

    def prompt(self) -> str:
        if self.activeModel() is None:
            return "请先在浮雕模式选中浮雕模型"
        return ("按住拖动连续冲压，按住不动持续冲压：颜色无效按圆圈、"
                "颜色内按点击色块（A/S 调径，右键结束）")

    def name(self) -> str:
        return "Stamp"

    def commandTitle(self) -> str:
        return "冲压"


class SweepMaterialCommand(CommandInstance):
    """路径塑形（原导动堆料）：笔刷沿单击的曲线扫掠堆料（复刻插件【曲面导动】页并升级）。

    交互：鼠标跟随笔刷圈（直径大小），A/S 调直径、D/F 调半衰直径、
    Q/W 调高度（实时同步面板）；单击一条曲线（开放/闭合均可）即沿其
    导动——效果二选一：
      - 累加：z += 增量，重复点击不断叠加；
      - 流动叠加：z = max(z, 增量层)，重复点击同参数结果不变。
    作用范围按颜色裁剪（颜色无效/颜色内/颜色外，相对底部调色板当前
    颜色）。命令常驻（右键/完成按钮结束），每次应用独立出栈 =
    一次可撤销操作。反馈统一走画布左下角信息区。
    """

    _PICK_PX = 8.0      # 点选容差（屏幕像素）

    def __init__(self) -> None:
        super().__init__()
        self._panel: SweepParamPanel | None = None
        self._lastWp = None    # 最近鼠标世界位置（笔刷圈跟随用）
        self._baseSnap = None

    def start(self) -> None:
        super().start()
        self._lastWp = None
        self._baseSnap = None
        clr = getattr(self._view, "clearSnapMarker", None)
        if callable(clr):
            clr()

    # ---------------- 面板 ----------------

    def paramPanel(self):
        if self._panel is None:
            self._panel = SweepParamPanel()
            self._panel.finished.connect(self.finish)
            # 形状/类型/效果/范围切换 → 笔刷圈轮廓实时重建（如半球无内圈）
            self._panel.paramsChanged.connect(self._refreshCircle)
        return self._panel

    # ---------------- 查询 ----------------

    def activeModel(self):
        return self._doc.activeReliefModel() if self._doc is not None else None

    def _notify(self, text: str) -> None:
        """结果反馈统一走画布左下角信息区（结果行，自动淡出）。"""
        if self._manager is not None:
            self._manager.notifyResult(text)

    # ---------------- 笔刷圈预览（跟随光标，直径大小） ----------------

    def _radius(self) -> float:
        return (max(0.05, self._panel.diameter() * 0.5)
                if self._panel is not None else 1.5)

    def _refreshCircle(self) -> None:
        """笔刷圈预览：外圈 = 直径，内圈 = 半衰直径（精雕式笔刷轮廓）。

        高斯分布的内圈即半衰圈（高度衰减到一半的位置）；锥体的内圈为
        平顶（小直径）圈；半球无小直径参数，只画外圈。
        随光标移动与 A/S、D/F 调参实时刷新。
        """
        from PySide6.QtCore import QRectF
        if self._lastWp is None or self._previewItem is None:
            return
        wx, wy = self._lastWp.x, self._lastWp.y
        r = self._radius()
        path = QPainterPath()
        path.addEllipse(QRectF(wx - r, wy - r, r * 2.0, r * 2.0))
        if self._panel is not None and self._panel.brushShape() != "hemisphere":
            rh = self._panel.halfLife() * 0.5
            if 0.05 <= rh < r:
                path.addEllipse(QRectF(wx - rh, wy - rh, rh * 2.0, rh * 2.0))
        self._setPreviewPath(path, solid=True)
        # 快捷键调参后立即重绘视图（2D 场景自动刷新，3D 需显式触发）
        # 预览已变 → 通知可见视口立即重绘（旧代码刷的是隐藏的 2D 视图，
        # 3D 视口要等下次鼠标移动才更新——这就是"改笔刷大小圆圈不更新"）
        if self._manager is not None:
            self._manager.invalidatePreview()

    def _adjust(self, kind: str, factor: float) -> None:
        """快捷键实时调参（写回面板，笔刷圈同步刷新）。"""
        if self._panel is None:
            return
        if kind == "diameter":
            self._panel.setDiameter(self._panel.diameter() * factor)
        elif kind == "half_life":
            self._panel.setHalfLife(self._panel.halfLife() * factor)
        else:      # height：固定步长（有符号）
            self._panel.setHeight(self._panel.height()
                                  + (HEIGHT_STEP if factor > 0 else -HEIGHT_STEP))
        self._refreshCircle()

    def onKeyPress(self, key, mods) -> bool:
        """A/S 调直径、D/F 调半衰直径、Q/W 调高度（面板实时同步）。"""
        if mods != Qt.NoModifier:
            return False
        mapping = {
            Qt.Key_A: ("diameter", 1.0 / PARAM_SCALE),
            Qt.Key_S: ("diameter", PARAM_SCALE),
            Qt.Key_D: ("half_life", 1.0 / PARAM_SCALE),
            Qt.Key_F: ("half_life", PARAM_SCALE),
            Qt.Key_Q: ("height", -1.0),
            Qt.Key_W: ("height", 1.0),
        }
        hit = mapping.get(key)
        if hit is None:
            return False
        self._adjust(hit[0], hit[1])
        return True

    # ---------------- 执行 ----------------

    def _applyToCurve(self, obj) -> None:
        """沿曲线导动：按当前面板参数与效果写入。
        若 obj 属于群组，则对同组所有曲线一起导动（从同一基准算，不叠加）。
        同一命令内多次点选共用命令开始时的高度基准，重叠取包络不累加。"""
        model = self.activeModel()
        if model is None:
            self._notify("请先选中浮雕模型（路径塑形作用于模型）")
            return
        if self._panel is None or self._doc is None:
            return
        doc = self._doc

        gid = obj.group()
        if gid:
            curves = [o for o in doc.visibleObjects()
                      if o.group() == gid and not o.isLocked()
                      and o.type() != ObjectType.SURFACE]
        else:
            curves = [obj]

        doc.beginEdit()
        try:
            if model.hasPendingTransform():
                model.bake()

            hf = model.heightField()
            params = self._panel.params()
            cv = PaintState.instance().currentValue()
            cmask = model.colorMask()
            cbase = model.curedBase()
            is_remove = params.apply == "remove"

            if self._baseSnap is None:
                self._baseSnap = hf.data.copy()
            base = self._baseSnap
            accumulated = hf.data.copy()

            group_result = base.copy()
            total = 0
            for curve in curves:
                poly = sample_sweep_path(curve)
                if poly.shape[0] < 2:
                    continue
                # 闭合标志从曲线对象取（采样已去重复尾点，事后判不出）
                _cfn = getattr(curve, "isClosed", None)
                _closed = bool(_cfn()) if callable(_cfn) else False
                hf.data[:] = base
                moved = apply_sweep(hf, poly, params,
                                    color_mask=cmask, color_value=cv,
                                    base=cbase, closed=_closed)
                total += moved
                if is_remove:
                    group_result = np.minimum(group_result, hf.data)
                else:
                    group_result = np.maximum(group_result, hf.data)

            if is_remove:
                accumulated = np.minimum(accumulated, group_result)
            else:
                accumulated = np.maximum(accumulated, group_result)
            hf.data[:] = accumulated

            if total > 0:
                model.bumpVersion()
                self._doc.notifyObjectChanged(model.id())
                apply_lab = APPLY_LABELS[APPLY_MODES.index(params.apply)]                     if params.apply in APPLY_MODES else "堆料"
                merge_lab = MERGE_LABELS[MERGES.index(params.merge)]                     if params.merge in MERGES else "累加"
                self._notify(f"导动完成（{apply_lab}·{merge_lab}）：影响 {total} 格")
            else:
                self._notify("未命中区域：请确认曲线靠近模型且未被颜色范围排除")
        finally:
            doc.endEdit()

    # ---------------- 交互：画布识别曲线并即时导动 ----------------

    def onMousePress(self, wp, mods) -> None:
        """点击最近的曲线即沿其导动（开放/闭合均可）。"""
        obj = self._pickCurve(wp)
        if obj is None:
            return
        self._applyToCurve(obj)

    def onMouseMove(self, wp, mods) -> None:
        """笔刷圈跟随光标。"""
        self._lastWp = wp
        self._refreshCircle()

    def onRightClick(self, wp) -> bool:
        self.finish()
        return True

    def onEnter(self) -> bool:
        self.finish()
        return True

    def showSnapHints(self) -> bool:
        return False

    def _pickCurve(self, wp):
        """拾取最近的曲线（开放/闭合均可；排除浮雕模型与锁定对象）。"""
        if self._doc is None:
            return None
        tol = self._pixelTolWorld()
        best, best_d = None, tol
        for o in self._doc.visibleObjects():
            if o.type() == ObjectType.SURFACE or o.isLocked():
                continue
            pts = sample_curve_points(o, 128)
            if pts.shape[0] < 2:
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
            return "请先在浮雕模式选中浮雕模型"
        return ("单击曲线路径塑形；A/S 直径、D/F 半衰直径、Q/W 高度"
                "（右键结束）")

    def name(self) -> str:
        return "SweepMaterial"

    def commandTitle(self) -> str:
        return "路径塑形"


class SweepSmoothCommand(CommandInstance):
    """路径磨光（磨光按钮的右键功能）：笔刷沿单击的曲线扫掠磨光。

    交互与路径塑形一致：鼠标跟随笔刷圈（外圈=直径、内圈=半衰直径轮廓），
    A/S 调直径、D/F 调半衰直径、Q/W 调力度（实时同步面板）；单击一条
    曲线（开放/闭合均可）即沿其导动——以直径 1/4 为间距沿线连续施加
    扩散磨光，磨出一条丝滑的磨光带。磨光方式三选一（去高补低/补低/
    取高）；作用范围按颜色裁剪。命令常驻（右键/完成按钮结束，可连续
    对多条曲线磨光），一次点击 = 一次可撤销操作。反馈统一走画布左下角
    信息区。
    """

    _PICK_PX = 8.0      # 点选容差（屏幕像素）

    def __init__(self) -> None:
        super().__init__()
        self._panel = None
        self._lastWp = None    # 最近鼠标世界位置（笔刷圈跟随用）

    def start(self) -> None:
        super().start()
        self._lastWp = None
        clr = getattr(self._view, "clearSnapMarker", None)
        if callable(clr):
            clr()

    # ---------------- 面板 ----------------

    def paramPanel(self):
        if self._panel is None:
            # 复用磨光面板（方式/力度/笔刷/颜色一致），不含 Ctrl+涂抹组
            self._panel = SmoothParamPanel(show_smear=False)
            self._panel.finished.connect(self.finish)
            self._panel.paramsChanged.connect(self._refreshCircle)
        return self._panel

    # ---------------- 查询 ----------------

    def activeModel(self):
        return self._doc.activeReliefModel() if self._doc is not None else None

    def _notify(self, text: str) -> None:
        """结果反馈统一走画布左下角信息区（结果行，自动淡出）。"""
        if self._manager is not None:
            self._manager.notifyResult(text)

    # ---------------- 笔刷圈预览（外圈=直径，内圈=半衰直径） ----------------

    def _radius(self) -> float:
        return (max(0.05, self._panel.diameter() * 0.5)
                if self._panel is not None else 3.0)

    def _refreshCircle(self) -> None:
        """笔刷圈预览：外圈 = 直径，内圈 = 半衰直径（精雕式笔刷轮廓）。"""
        from PySide6.QtCore import QRectF
        if self._lastWp is None or self._previewItem is None:
            return
        wx, wy = self._lastWp.x, self._lastWp.y
        r = self._radius()
        path = QPainterPath()
        path.addEllipse(QRectF(wx - r, wy - r, r * 2.0, r * 2.0))
        rh = self._panel.halfLife() * 0.5 if self._panel is not None else 0.0
        if 0.05 <= rh < r:
            path.addEllipse(QRectF(wx - rh, wy - rh, rh * 2.0, rh * 2.0))
        self._setPreviewPath(path, solid=True)
        if self._manager is not None:
            self._manager.invalidatePreview()

    def _adjust(self, kind: str, factor: float) -> None:
        """快捷键实时调参（写回面板，笔刷圈同步刷新）。"""
        if self._panel is None:
            return
        if kind == "diameter":
            self._panel.setDiameter(self._panel.diameter() * factor)
        elif kind == "half_life":
            self._panel.setHalfLife(self._panel.halfLife() * factor)
        else:      # strength：固定步长（Q 降 / W 升）
            self._panel.setStrength(self._panel.strength()
                                    + (STRENGTH_STEP if factor > 0
                                       else -STRENGTH_STEP))
        self._refreshCircle()

    def onKeyPress(self, key, mods) -> bool:
        """A/S 调直径、D/F 调半衰直径、Q/W 调力度（面板实时同步）。"""
        if mods != Qt.NoModifier:
            return False
        mapping = {
            Qt.Key_A: ("diameter", 1.0 / PARAM_SCALE),
            Qt.Key_S: ("diameter", PARAM_SCALE),
            Qt.Key_D: ("half_life", 1.0 / PARAM_SCALE),
            Qt.Key_F: ("half_life", PARAM_SCALE),
            Qt.Key_Q: ("strength", -1.0),
            Qt.Key_W: ("strength", 1.0),
        }
        hit = mapping.get(key)
        if hit is None:
            return False
        self._adjust(hit[0], hit[1])
        return True

    # ---------------- 执行：沿曲线路径磨光 ----------------

    def _applyToCurve(self, obj) -> None:
        """沿曲线路径磨光（按当前面板参数）。
        若 obj 属于群组，则对同组所有曲线一起磨光。"""
        model = self.activeModel()
        if model is None:
            self._notify("请先选中浮雕模型（路径磨光作用于模型）")
            return
        if self._panel is None or self._doc is None:
            return
        doc = self._doc

        gid = obj.group()
        if gid:
            curves = [o for o in doc.visibleObjects()
                      if o.group() == gid and not o.isLocked()
                      and o.type() != ObjectType.SURFACE]
        else:
            curves = [obj]

        doc.beginEdit()
        try:
            if model.hasPendingTransform():
                model.bake()
            hf = model.heightField()
            params = self._panel.params()
            total = 0
            for curve in curves:
                poly = sample_sweep_path(curve)
                if poly.shape[0] < 2:
                    continue
                _cfn = getattr(curve, "isClosed", None)
                _closed = bool(_cfn()) if callable(_cfn) else False
                changed = apply_sweep_smooth(hf, poly, params,
                                             color_mask=model.colorMask(),
                                             color_value=PaintState.instance().currentValue(),
                                             closed=_closed)
                total += changed
            if total > 0:
                model.bumpVersion()
                doc.notifyObjectChanged(model.id())
                mode = params.mode
                lab = MODE_LABELS[MODES.index(mode)] if mode in MODES else "去高补低"
                self._notify(f"路径磨光完成：{lab}，力度 {params.strength:g}%，"
                             f"影响 {total} 格")
            else:
                self._notify("未命中区域：请确认曲线在模型范围内且未被颜色排除")
        finally:
            doc.endEdit()

    # ---------------- 交互：画布识别曲线并即时路径磨光 ----------------

    def onMousePress(self, wp, mods) -> None:
        """点击最近的曲线即沿其路径磨光（开放/闭合均可）。"""
        obj = self._pickCurve(wp)
        if obj is None:
            return
        self._applyToCurve(obj)

    def onMouseMove(self, wp, mods) -> None:
        """笔刷圈跟随光标。"""
        self._lastWp = wp
        self._refreshCircle()

    def onRightClick(self, wp) -> bool:
        self.finish()
        return True

    def onEnter(self) -> bool:
        self.finish()
        return True

    def showSnapHints(self) -> bool:
        return False

    def _pickCurve(self, wp):
        """拾取最近的曲线（开放/闭合均可；排除浮雕模型与锁定对象）。"""
        if self._doc is None:
            return None
        tol = self._pixelTolWorld()
        best, best_d = None, tol
        for o in self._doc.visibleObjects():
            if o.type() == ObjectType.SURFACE or o.isLocked():
                continue
            pts = sample_curve_points(o, 128)
            if pts.shape[0] < 2:
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
            return "请先在浮雕模式选中浮雕模型"
        return ("单击曲线沿其路径磨光；A/S 直径、D/F 半衰直径、Q/W 力度"
                "（右键结束）")

    def name(self) -> str:
        return "SweepSmooth"

    def commandTitle(self) -> str:
        return "路径磨光"


class AddMaterialCommand(CommandInstance):
    """塑形笔刷（原堆料）：鼠标在模型上涂抹/雕刻（复刻精雕堆料笔刷习惯）。

    交互：鼠标跟随笔刷圈（外圈=直径、内圈=半衰直径轮廓），A/S 调直径、
    D/F 调半衰直径、Q/W 调高度（实时同步面板）；在模型上**按住拖动**
    即沿轨迹按弧长间距连续盖章堆料（负高度=雕刻去除材料），一次拖动
    （按下→拖动→松开）= 一次可撤销操作。作用范围按颜色裁剪（颜色
    无效/颜色内/颜色外，相对底部调色板当前颜色）。反馈统一走画布
    左下角信息区。
    """

    _PICK_PX = 8.0      # 未使用（笔刷直接作用于模型表面），占位保持一致

    # ---- 雕刻笔刷流量（对齐 ZBrush/Blender 的 flow 概念：单章只施加一小部分，
    #      重叠叠加自然收敛，才是"柔和"；按住不动由气刷定时器持续施加=推料）----
    MOVE_FLOW = 0.25    # 移动盖章：相对面板"高度"的流量系数（拖 1 个直径 ≈ 高度×~1）
    AIR_FLOW = 0.04     # 气刷每拍（33ms）：按住不动持续堆料（≈1.2×高度/秒）
    AIR_MS = 33

    def __init__(self) -> None:
        super().__init__()
        self._panel: AddMaterialParamPanel | None = None
        self._lastWp = None        # 最近鼠标世界位置（笔刷圈跟随用）
        self._stroking = False     # 笔画进行中（按下→拖动→松开）
        self._lastStamp = None     # 最近一次盖章的世界坐标（弧长插值起点）
        self._lastNotify = 0.0     # 上次推进版本+通知视口的时刻（节流用）
        self._airTimer = None      # 气刷定时器：按住不动持续堆料（推料）

    def start(self) -> None:
        super().start()
        self._lastWp = None
        self._stroking = False
        self._lastStamp = None
        clr = getattr(self._view, "clearSnapMarker", None)
        if callable(clr):
            clr()

    # ---------------- 面板 ----------------

    def paramPanel(self):
        if self._panel is None:
            self._panel = AddMaterialParamPanel()
            self._panel.finished.connect(self.finish)
        return self._panel

    # ---------------- 查询 ----------------

    def activeModel(self):
        return self._doc.activeReliefModel() if self._doc is not None else None

    def _notify(self, text: str) -> None:
        """结果反馈统一走画布左下角信息区（结果行，自动淡出）。"""
        if self._manager is not None:
            self._manager.notifyResult(text)

    # ---------------- 笔刷圈预览（外圈=直径，内圈=半衰直径） ----------------

    def _radius(self) -> float:
        return (max(0.05, self._panel.diameter() * 0.5)
                if self._panel is not None else 1.5)

    def _refreshCircle(self) -> None:
        """笔刷圈预览：外圈 = 直径，内圈 = 半衰直径（精雕式笔刷轮廓）。"""
        from PySide6.QtCore import QRectF
        if self._lastWp is None or self._previewItem is None:
            return
        wx, wy = self._lastWp.x, self._lastWp.y
        r = self._radius()
        path = QPainterPath()
        path.addEllipse(QRectF(wx - r, wy - r, r * 2.0, r * 2.0))
        if self._panel is not None and self._panel.brushShape() != "hemisphere":
            rh = self._panel.halfLife() * 0.5
            if 0.05 <= rh < r:
                path.addEllipse(QRectF(wx - rh, wy - rh, rh * 2.0, rh * 2.0))
        self._setPreviewPath(path, solid=True)
        # 预览已变 → 通知可见视口立即重绘（旧代码刷的是隐藏的 2D 视图，
        # 3D 视口要等下次鼠标移动才更新——这就是"改笔刷大小圆圈不更新"）
        if self._manager is not None:
            self._manager.invalidatePreview()

    def _adjust(self, kind: str, factor: float) -> None:
        """快捷键实时调参（写回面板，笔刷圈同步刷新）。"""
        if self._panel is None:
            return
        if kind == "diameter":
            self._panel.setDiameter(self._panel.diameter() * factor)
        elif kind == "half_life":
            self._panel.setHalfLife(self._panel.halfLife() * factor)
        else:      # height：固定步长（有符号，负=雕刻）
            self._panel.setHeight(self._panel.height()
                                  + (HEIGHT_STEP if factor > 0 else -HEIGHT_STEP))
        self._refreshCircle()

    def onKeyPress(self, key, mods) -> bool:
        """A/S 调直径、D/F 调半衰直径、Q/W 调高度（面板实时同步）。"""
        if mods != Qt.NoModifier:
            return False
        mapping = {
            Qt.Key_A: ("diameter", 1.0 / PARAM_SCALE),
            Qt.Key_S: ("diameter", PARAM_SCALE),
            Qt.Key_D: ("half_life", 1.0 / PARAM_SCALE),
            Qt.Key_F: ("half_life", PARAM_SCALE),
            Qt.Key_Q: ("height", -1.0),
            Qt.Key_W: ("height", 1.0),
        }
        hit = mapping.get(key)
        if hit is None:
            return False
        self._adjust(hit[0], hit[1])
        return True

    # ---------------- 执行：按住拖动连续盖章 ----------------

    def _stampSpacing(self, model) -> float:
        """盖章间距：直径的 1/4，且不小于一个栅格。"""
        sp = max(self._radius() * 0.5, model.heightField().spacing)
        return max(sp, 1e-6)

    def _stampAt(self, model, wp, scale: float = 1.0) -> bool:
        """在 wp 处盖一次笔刷（按当前颜色裁剪），返回是否有格子被写入。

        ``scale`` 为流量系数（相对面板"高度"），单章只施加一小部分，
        沿轨迹重叠叠加自然收敛——柔和的关键。
        """
        params = self._panel.params()
        params.height = params.height * scale
        moved = apply_brush_stamp(model.heightField(), wp.x, wp.y, params,
                                  color_mask=model.colorMask(),
                                  color_value=PaintState.instance().currentValue())
        return moved > 0

    # ---------------- 气刷（推料）：按住不动持续堆料 ----------------

    def _ensureAirTimer(self):
        if self._airTimer is None:
            from PySide6.QtCore import QTimer
            self._airTimer = QTimer()
            self._airTimer.setInterval(self.AIR_MS)
            self._airTimer.timeout.connect(self._airbrushTick)
        return self._airTimer

    def _airbrushTick(self) -> None:
        """气刷节拍：按住不动也持续按流量堆料/去料（精雕推料手感）。"""
        if not self._stroking or self._lastWp is None:
            return
        model = self.activeModel()
        if model is None or self._panel is None or self._doc is None:
            return
        if self._stampAt(model, self._lastWp, scale=self.AIR_FLOW):
            self._touchModel(model)

    def _touchModel(self, model, force: bool = False) -> None:
        """笔画期间节流推进版本+通知视口重绘（≈30fps），松开时强制刷新。

        每次盖章都 bumpVersion 会让 3D 视口整块重建显示网格（降采样+法线+
        GL 缓冲），鼠标事件频率远超刷新率 → 卡顿；节流后盖章（numpy 小
        bbox 累加）与重建解耦，涂抹才顺滑。"""
        now = time.monotonic()
        # 33ms（≈30fps）：重建已通过 bincount 散射 + 索引拓扑缓存 + GC 守卫
        # 做到轻量，30fps 可稳定 hold 住，视觉更新更跟手
        if force or (now - self._lastNotify) >= 0.033:
            model.bumpVersion()
            self._doc.notifyObjectChanged(model.id())
            self._lastNotify = now

    def _beginStroke(self, model, wp) -> None:
        """开始一笔：开启独立撤销会话并立即盖第一下，同时启动气刷（推料）。"""
        if model.hasPendingTransform():
            model.bake()
        # 笔画前清一次循环 GC，随后暂停自动 GC：笔画期大量临时数组会让
        # gen2 全量回收偶发上百毫秒停顿（模型"闪一下"），结束一笔时恢复
        gc.collect()
        self._gcWasEnabled = gc.isenabled()
        gc.disable()
        self._doc.beginEdit()
        self._stroking = True
        self._lastWp = wp
        self._lastStamp = (wp.x, wp.y)
        self._stampAt(model, wp, scale=self.MOVE_FLOW)
        self._touchModel(model, force=True)
        self._ensureAirTimer().start()

    def _endStroke(self) -> None:
        """结束一笔（强制刷新视口后撤销会话出栈）。"""
        if not self._stroking:
            return
        self._stroking = False
        self._lastStamp = None
        if self._airTimer is not None:
            self._airTimer.stop()
        if self._doc is not None:
            model = self.activeModel()
            if model is not None:
                self._touchModel(model, force=True)
            self._doc.endEdit()
        if getattr(self, "_gcWasEnabled", True) and not gc.isenabled():
            gc.enable()

    def onMousePress(self, wp, mods) -> None:
        """在模型上按下即开始涂抹（雕刻）。"""
        model = self.activeModel()
        if model is None:
            self._notify("请先在浮雕模式选中浮雕模型（塑形作用于模型）")
            return
        if self._panel is None or self._doc is None:
            return
        hf = model.heightField()
        ox, oy = hf.origin
        if not (ox <= wp.x <= ox + (hf.nx - 1) * hf.spacing
                and oy <= wp.y <= oy + (hf.ny - 1) * hf.spacing):
            self._notify("请在模型范围内涂抹")
            return
        self._beginStroke(model, wp)

    def onMouseMove(self, wp, mods) -> None:
        """笔刷圈跟随光标；笔画中沿轨迹按弧长间距连续盖章。"""
        self._lastWp = wp
        self._refreshCircle()
        if not self._stroking or self._lastStamp is None:
            return
        model = self.activeModel()
        if model is None or self._panel is None or self._doc is None:
            return
        lx, ly = self._lastStamp
        dx, dy = wp.x - lx, wp.y - ly
        dist = math.hypot(dx, dy)
        spacing = self._stampSpacing(model)
        if dist < spacing:
            return
        n = int(dist // spacing)
        ux, uy = dx / dist, dy / dist
        stamped = False
        for i in range(1, n + 1):
            if self._stampAt(model, Point3D(lx + ux * spacing * i,
                                            ly + uy * spacing * i, 0.0),
                             scale=self.MOVE_FLOW):
                stamped = True
        self._lastStamp = (lx + ux * spacing * n, ly + uy * spacing * n)
        if stamped:
            self._touchModel(model)

    def onMouseRelease(self, mods) -> None:
        """松开左键：结束一笔（一次拖动 = 一次可撤销操作）。"""
        self._endStroke()

    def onRightClick(self, wp) -> bool:
        self._endStroke()
        self.finish()
        return True

    def onEnter(self) -> bool:
        self._endStroke()
        self.finish()
        return True

    def cancel(self) -> None:
        self._endStroke()
        super().cancel()

    def finish(self) -> None:
        self._endStroke()
        super().finish()

    def showSnapHints(self) -> bool:
        return False

    def prompt(self) -> str:
        if self.activeModel() is None:
            return "请先在浮雕模式选中浮雕模型"
        return ("按住拖动涂抹/雕刻，按住不动持续堆起；A/S 直径、D/F 半衰直径、"
                "Q/W 高度（右键结束）")

    def name(self) -> str:
        return "AddMaterial"

    def commandTitle(self) -> str:
        return "塑形"


class MoveHeightCommand(CommandInstance):
    """笔刷推移：在模型上按住拖动，笔刷跟着鼠标走，范围内高度场内容被
    推着流动跟随鼠标——跟推泥土一样，内容不消失。

    交互：鼠标跟随笔刷圈（外圈=直径、内圈=半衰直径轮廓）；在模型上**按住
    并拖动**，笔刷中心跟随鼠标，每帧增量位移按笔刷权重推移内容——中心
    完全跟着鼠标走、边缘不动，内容连续流动不消失。一次拖动（按下→拖动→
    松开）= 一次可撤销操作。快捷键 A/S 调直径、D/F 调半衰直径（实时同步
    面板）。反馈统一走画布左下角信息区。
    """

    def __init__(self) -> None:
        super().__init__()
        self._panel: MoveHeightParamPanel | None = None
        self._lastWp = None        # 最近鼠标世界位置（笔刷圈跟随 + 增量位移起点）
        self._stroking = False     # 笔画进行中（按下→拖动→松开）
        self._lastNotify = 0.0     # 上次推进版本+通知视口的时刻（节流用）

    def start(self) -> None:
        super().start()
        self._lastWp = None
        self._stroking = False

        clr = getattr(self._view, "clearSnapMarker", None)
        if callable(clr):
            clr()

    # ---------------- 面板 ----------------

    def paramPanel(self):
        if self._panel is None:
            self._panel = MoveHeightParamPanel()
            self._panel.finished.connect(self.finish)
        return self._panel

    # ---------------- 查询 ----------------

    def activeModel(self):
        return self._doc.activeReliefModel() if self._doc is not None else None

    def _notify(self, text: str) -> None:
        if self._manager is not None:
            self._manager.notifyResult(text)

    # ---------------- 笔刷圈预览 ----------------

    def _radius(self) -> float:
        return (max(0.05, self._panel.diameter() * 0.5)
                if self._panel is not None else 1.5)

    def _refreshCircle(self) -> None:
        from PySide6.QtCore import QRectF
        if self._lastWp is None or self._previewItem is None:
            return
        wx, wy = self._lastWp.x, self._lastWp.y
        r = self._radius()
        path = QPainterPath()
        path.addEllipse(QRectF(wx - r, wy - r, r * 2.0, r * 2.0))
        if self._panel is not None and self._panel.brushShape() != "hemisphere":
            rh = self._panel.halfLife() * 0.5
            if 0.05 <= rh < r:
                path.addEllipse(QRectF(wx - rh, wy - rh, rh * 2.0, rh * 2.0))
        self._setPreviewPath(path, solid=True)
        if self._manager is not None:
            self._manager.invalidatePreview()

    def _adjust(self, kind: str, factor: float) -> None:
        if self._panel is None:
            return
        if kind == "diameter":
            self._panel.setDiameter(self._panel.diameter() * factor)
        else:      # half_life
            self._panel.setHalfLife(self._panel.halfLife() * factor)
        self._refreshCircle()

    def onKeyPress(self, key, mods) -> bool:
        """A/S 调直径、D/F 调半衰直径（面板实时同步）。"""
        if mods != Qt.NoModifier:
            return False
        mapping = {
            Qt.Key_A: ("diameter", 1.0 / PARAM_SCALE),
            Qt.Key_S: ("diameter", PARAM_SCALE),
            Qt.Key_D: ("half_life", 1.0 / PARAM_SCALE),
            Qt.Key_F: ("half_life", PARAM_SCALE),
        }
        hit = mapping.get(key)
        if hit is None:
            return False
        self._adjust(hit[0], hit[1])
        return True

    # ---------------- 执行：按住拖动平移内容 ----------------

    def _touchModel(self, model, force: bool = False) -> None:
        """笔画期间节流推进版本+通知视口重绘（≈30fps），松开时强制刷新。"""
        now = time.monotonic()
        if force or (now - self._lastNotify) >= 0.033:
            model.bumpVersion()
            self._doc.notifyObjectChanged(model.id())
            self._lastNotify = now

    def _beginStroke(self, model, wp) -> None:
        """开始一笔：开启撤销会话，记录增量位移起点。"""
        if model.hasPendingTransform():
            model.bake()
        gc.collect()
        self._gcWasEnabled = gc.isenabled()
        gc.disable()
        self._doc.beginEdit()
        self._stroking = True
        self._lastWp = wp
        self._touchModel(model, force=True)

    def _endStroke(self) -> None:
        """结束一笔（强制刷新视口后撤销会话出栈）。"""
        if not self._stroking:
            return
        self._stroking = False
        if self._doc is not None:
            model = self.activeModel()
            if model is not None:
                self._touchModel(model, force=True)
            self._doc.endEdit()
        if getattr(self, "_gcWasEnabled", True) and not gc.isenabled():
            gc.enable()

    def onMousePress(self, wp, mods) -> None:
        """在模型上按下即开始推移笔画。"""
        model = self.activeModel()
        if model is None:
            self._notify("请先在浮雕模式选中浮雕模型（推移作用于模型）")
            return
        if self._panel is None or self._doc is None:
            return
        hf = model.heightField()
        ox, oy = hf.origin
        if not (ox <= wp.x <= ox + (hf.nx - 1) * hf.spacing
                and oy <= wp.y <= oy + (hf.ny - 1) * hf.spacing):
            self._notify("请在模型范围内推移")
            return
        self._beginStroke(model, wp)

    def onMouseMove(self, wp, mods) -> None:
        """笔刷圈跟随光标；笔画中按增量位移推移笔刷范围内内容。"""
        if not self._stroking:
            self._lastWp = wp
            self._refreshCircle()
            return
        if self._lastWp is None:
            self._lastWp = wp
            return
        model = self.activeModel()
        if model is None or self._panel is None or self._doc is None:
            return
        ddx = wp.x - self._lastWp.x
        ddy = wp.y - self._lastWp.y
        params = self._panel.params()
        moved = move_brush_region(model.heightField(), wp.x, wp.y,
                                  ddx, ddy, params)
        self._lastWp = wp
        self._refreshCircle()
        if moved > 0:
            self._touchModel(model)

    def onMouseRelease(self, mods) -> None:
        """松开左键：结束一笔（一次拖动 = 一次可撤销操作）。"""
        self._endStroke()

    def onRightClick(self, wp) -> bool:
        self._endStroke()
        self.finish()
        return True

    def onEnter(self) -> bool:
        self._endStroke()
        self.finish()
        return True

    def cancel(self) -> None:
        self._endStroke()
        super().cancel()

    def finish(self) -> None:
        self._endStroke()
        super().finish()

    def showSnapHints(self) -> bool:
        return False

    def prompt(self) -> str:
        if self.activeModel() is None:
            return "请先在浮雕模式选中浮雕模型"
        return ("按住拖动推移笔刷范围内的高度场内容（跟推泥土一样）；"
                "A/S 直径、D/F 半衰直径（右键结束）")

    def name(self) -> str:
        return "Move"

    def commandTitle(self) -> str:
        return "移动"


class SmoothCommand(CommandInstance):
    """磨光笔刷：按住拖动，把笔刷范围内的局部高低向邻域均值抹平。

    交互与堆料笔刷一致：鼠标跟随笔刷圈（外圈=直径、内圈=半衰直径轮廓），
    A/S 调直径、D/F 调半衰直径、Q/W 调力度（实时同步面板）；在模型上
    **按住拖动**即沿轨迹按弧长间距连续盖章磨光，一次拖动（按下→拖动→
    松开）= 一次可撤销操作。磨光方式三选一（去高补低/补低/取高）；
    力度 = 预设档位单选 + 输入框实时联动（面板内）。作用范围按颜色裁剪
    （颜色无效/颜色内/颜色外，相对底部调色板当前颜色）。反馈统一走
    画布左下角信息区。
    """

    # ---- 磨光流量：单章只施加一部分力度，沿轨迹/气刷重叠叠加，丝滑不生硬 ----
    MOVE_FLOW = 0.6     # 移动盖章：相对面板力度的系数
    AIR_FLOW = 0.9      # 气刷每拍（33ms）：按住不动持续磨，粗糙区 2-3 拍即见平滑
    AIR_MS = 33

    def __init__(self) -> None:
        super().__init__()
        self._panel: SmoothParamPanel | None = None
        self._lastWp = None        # 最近鼠标世界位置（笔刷圈跟随用）
        self._stroking = False     # 笔画进行中（按下→拖动→松开）
        self._lastStamp = None     # 最近一次盖章的世界坐标（弧长插值起点）
        self._lastNotify = 0.0     # 上次推进版本+通知视口的时刻（节流用）
        self._totalChanged = 0     # 本笔累计受影响格数（松开时反馈）
        self._airTimer = None      # 气刷定时器：按住不动持续磨光

    def start(self) -> None:
        super().start()
        self._lastWp = None
        self._stroking = False
        self._lastStamp = None
        clr = getattr(self._view, "clearSnapMarker", None)
        if callable(clr):
            clr()

    # ---------------- 面板 ----------------

    def paramPanel(self):
        if self._panel is None:
            self._panel = SmoothParamPanel()
            self._panel.finished.connect(self.finish)
            # 方式/档位/颜色切换 → 笔刷圈轮廓实时重建
            self._panel.paramsChanged.connect(self._refreshCircle)
        return self._panel

    # ---------------- 查询 ----------------

    def ctrlSmearBrushParams(self):
        """Ctrl+涂抹（堆料/去料）借用磨光笔刷的尺寸。

        磨光命令激活期间，视口的 Ctrl+左键涂抹读取本方法拿直径/半衰直径
        （与磨光笔刷同大，A/S、D/F 调整实时跟随）；无面板返回 None（视口
        回退到 SmearState 的固定预设）。
        """
        if self._panel is None:
            return None
        return (self._panel.diameter(), self._panel.halfLife())

    def activeModel(self):
        return self._doc.activeReliefModel() if self._doc is not None else None

    def _notify(self, text: str) -> None:
        """结果反馈统一走画布左下角信息区（结果行，自动淡出）。"""
        if self._manager is not None:
            self._manager.notifyResult(text)

    # ---------------- 笔刷圈预览（外圈=直径，内圈=半衰直径） ----------------

    def _radius(self) -> float:
        return (max(0.05, self._panel.diameter() * 0.5)
                if self._panel is not None else 3.0)

    def _refreshCircle(self) -> None:
        """笔刷圈预览：外圈 = 直径，内圈 = 半衰直径（精雕式笔刷轮廓）。"""
        from PySide6.QtCore import QRectF
        if self._lastWp is None or self._previewItem is None:
            return
        wx, wy = self._lastWp.x, self._lastWp.y
        r = self._radius()
        path = QPainterPath()
        path.addEllipse(QRectF(wx - r, wy - r, r * 2.0, r * 2.0))
        rh = self._panel.halfLife() * 0.5 if self._panel is not None else 0.0
        if 0.05 <= rh < r:
            path.addEllipse(QRectF(wx - rh, wy - rh, rh * 2.0, rh * 2.0))
        self._setPreviewPath(path, solid=True)
        # 预览已变 → 通知可见视口立即重绘（旧代码刷的是隐藏的 2D 视图，
        # 3D 视口要等下次鼠标移动才更新——这就是"改笔刷大小圆圈不更新"）
        if self._manager is not None:
            self._manager.invalidatePreview()

    def _adjust(self, kind: str, factor: float) -> None:
        """快捷键实时调参（写回面板，笔刷圈同步刷新）。"""
        if self._panel is None:
            return
        if kind == "diameter":
            self._panel.setDiameter(self._panel.diameter() * factor)
        elif kind == "half_life":
            self._panel.setHalfLife(self._panel.halfLife() * factor)
        else:      # strength：固定步长（Q 降 / W 升）
            step = STRENGTH_STEP if factor > 0 else -STRENGTH_STEP
            self._panel.setStrength(self._panel.strength() + step)
        self._refreshCircle()

    def onKeyPress(self, key, mods) -> bool:
        """A/S 调直径、D/F 调半衰直径、Q/W 调力度（面板实时同步）。"""
        if mods != Qt.NoModifier:
            return False
        mapping = {
            Qt.Key_A: ("diameter", 1.0 / PARAM_SCALE),
            Qt.Key_S: ("diameter", PARAM_SCALE),
            Qt.Key_D: ("half_life", 1.0 / PARAM_SCALE),
            Qt.Key_F: ("half_life", PARAM_SCALE),
            Qt.Key_Q: ("strength", -1.0),
            Qt.Key_W: ("strength", 1.0),
        }
        hit = mapping.get(key)
        if hit is None:
            return False
        self._adjust(hit[0], hit[1])
        return True

    # ---------------- 执行：按住拖动连续盖章 ----------------

    def _stampSpacing(self, model) -> float:
        """盖章间距：直径的 1/4，且不小于一个栅格。"""
        sp = max(self._radius() * 0.5, model.heightField().spacing)
        return max(sp, 1e-6)

    def _stampAt(self, model, wp, scale: float = 1.0) -> bool:
        """在 wp 处磨一次（按当前颜色裁剪），返回是否有格子被改变。

        ``scale`` 为力度系数：单章只施加一部分力度，沿轨迹/气刷重叠叠加，
        扩散平滑收敛——丝滑不生硬。
        """
        if self._panel is None:
            return False
        params = self._panel.params()
        params.strength = min(100.0, params.strength * scale)
        changed = apply_smooth_stamp(model.heightField(), wp.x, wp.y, params,
                                     color_mask=model.colorMask(),
                                     color_value=PaintState.instance().currentValue())
        if changed:
            self._totalChanged += changed
        return changed > 0

    # ---------------- 气刷：按住不动持续磨光 ----------------

    def _ensureAirTimer(self):
        if self._airTimer is None:
            from PySide6.QtCore import QTimer
            self._airTimer = QTimer()
            self._airTimer.setInterval(self.AIR_MS)
            self._airTimer.timeout.connect(self._airbrushTick)
        return self._airTimer

    def _airbrushTick(self) -> None:
        """气刷节拍：按住不动也持续磨光（气刷/推料手感）。"""
        if not self._stroking or self._lastWp is None:
            return
        model = self.activeModel()
        if model is None or self._panel is None or self._doc is None:
            return
        self._stampAt(model, self._lastWp, scale=self.AIR_FLOW)
        self._touchModel(model)

    def _touchModel(self, model, force: bool = False) -> None:
        """笔画期间节流推进版本+通知视口重绘（≈30fps），松开时强制刷新。

        与堆料笔刷同因：每次盖章都重建 3D 显示网格会卡顿，节流后盖章
        （numpy 小 bbox 均值滤波）与重建解耦，涂抹才顺滑。"""
        now = time.monotonic()
        # 33ms（≈30fps）：重建已通过 bincount 散射 + 索引拓扑缓存 + GC 守卫
        # 做到轻量，30fps 可稳定 hold 住，视觉更新更跟手
        if force or (now - self._lastNotify) >= 0.033:
            model.bumpVersion()
            self._doc.notifyObjectChanged(model.id())
            self._lastNotify = now

    def _beginStroke(self, model, wp) -> None:
        """开始一笔：开启独立撤销会话并立即磨第一下，同时启动气刷。"""
        if model.hasPendingTransform():
            model.bake()
        # 笔画前清一次循环 GC，随后暂停自动 GC：笔画期大量临时数组会让
        # gen2 全量回收偶发上百毫秒停顿（模型"闪一下"），结束一笔时恢复
        gc.collect()
        self._gcWasEnabled = gc.isenabled()
        gc.disable()
        self._doc.beginEdit()
        self._stroking = True
        self._totalChanged = 0
        self._lastWp = wp
        self._lastStamp = (wp.x, wp.y)
        self._stampAt(model, wp, scale=self.MOVE_FLOW)
        self._touchModel(model, force=True)
        self._ensureAirTimer().start()

    def _endStroke(self) -> None:
        """结束一笔（强制刷新视口后撤销会话出栈）。"""
        if not self._stroking:
            return
        self._stroking = False
        self._lastStamp = None
        if self._airTimer is not None:
            self._airTimer.stop()
        if self._doc is not None:
            model = self.activeModel()
            if model is not None:
                self._touchModel(model, force=True)
            self._doc.endEdit()
        if getattr(self, "_gcWasEnabled", True) and not gc.isenabled():
            gc.enable()
        if self._totalChanged:
            mode = self._panel.mode() if self._panel is not None else "both"
            lab = MODE_LABELS[MODES.index(mode)] if mode in MODES else "去高补低"
            strength = self._panel.strength() if self._panel is not None else 0.0
            self._notify(f"磨光完成：{lab}，力度 {strength:g}%，"
                         f"影响 {self._totalChanged} 格")
        self._totalChanged = 0

    def onMousePress(self, wp, mods) -> None:
        """在模型上按下即开始磨光。"""
        model = self.activeModel()
        if model is None:
            self._notify("请先在浮雕模式选中浮雕模型（磨光作用于模型）")
            return
        if self._panel is None or self._doc is None:
            return
        hf = model.heightField()
        ox, oy = hf.origin
        if not (ox <= wp.x <= ox + (hf.nx - 1) * hf.spacing
                and oy <= wp.y <= oy + (hf.ny - 1) * hf.spacing):
            self._notify("请在模型范围内磨光")
            return
        self._beginStroke(model, wp)

    def onMouseMove(self, wp, mods) -> None:
        """笔刷圈跟随光标；笔画中沿轨迹按弧长间距连续盖章。"""
        self._lastWp = wp
        self._refreshCircle()
        if not self._stroking or self._lastStamp is None:
            return
        model = self.activeModel()
        if model is None or self._panel is None or self._doc is None:
            return
        lx, ly = self._lastStamp
        dx, dy = wp.x - lx, wp.y - ly
        dist = math.hypot(dx, dy)
        spacing = self._stampSpacing(model)
        if dist < spacing:
            return
        n = int(dist // spacing)
        ux, uy = dx / dist, dy / dist
        stamped = False
        for i in range(1, n + 1):
            if self._stampAt(model, Point3D(lx + ux * spacing * i,
                                            ly + uy * spacing * i, 0.0),
                             scale=self.MOVE_FLOW):
                stamped = True
        self._lastStamp = (lx + ux * spacing * n, ly + uy * spacing * n)
        if stamped:
            self._touchModel(model)

    def onMouseRelease(self, mods) -> None:
        """松开左键：结束一笔（一次拖动 = 一次可撤销操作）。"""
        self._endStroke()

    def onRightClick(self, wp) -> bool:
        self._endStroke()
        self.finish()
        return True

    def onEnter(self) -> bool:
        self._endStroke()
        self.finish()
        return True

    def cancel(self) -> None:
        self._endStroke()
        super().cancel()

    def finish(self) -> None:
        self._endStroke()
        super().finish()

    def showSnapHints(self) -> bool:
        return False

    def prompt(self) -> str:
        if self.activeModel() is None:
            return "请先在浮雕模式选中浮雕模型"
        return ("在模型上按住拖动磨光（去高补低/补低/取高）；"
                "A/S 直径、D/F 半衰直径、Q/W 力度（右键结束）")

    def name(self) -> str:
        return "Smooth"

    def commandTitle(self) -> str:
        return "磨光"


class RegionTransformCommand(CommandInstance):
    """区域变换：鼠标选定一个区域（闭合曲线内或色块），对其高度场内容做
    旋转/缩放/移动。不用操作轴，直接鼠标拖动。

    交互：激活后点击闭合曲线（曲线内区域）或色块（同色号区域）选定变换
    对象 → 进入变换态 → 按 R 旋转 / S 缩放 / G 移动切模式（仅本命令内
    有效）→ 按住拖动实时变换预览 → 松开累积本次变换 → 可继续按 R/S/G 做
    下一次 → 右键/回车确认提交，Esc 取消恢复。以区域质心为变换中心。

    高级约束（类 Blender）：
      R 再按 R → 自定义旋转中心（点击一点作为旋转/缩放中心）
      S 后按 X → 只缩放 X 方向；按 Y → 只缩放 Y 方向
      G 后按 X → 只移动 X 方向；按 Y → 只移动 Y 方向
    """

    _PICK_PX = 8.0

    def __init__(self) -> None:
        super().__init__()
        self._panel: RegionTransformParamPanel | None = None
        self._phase = "region"     # region/pickCenter/mode/drag
        self._mode = None          # rotate / scale / move
        self._axis = None          # None / "x" / "y"  轴约束
        self._mask = None          # (ny,nx) bool 区域蒙版
        self._center = None        # (cx, cy) 世界坐标（当前变换中心）
        self._stamp = None         # where(mask, source, 0) 印章场
        self._stampMask = None     # mask.astype(float) 印章蒙版场
        self._base = None          # where(mask, 0, source) 原位置清空的底场
        self._srcBBox = None       # 印章外接矩形 (col_lo,row_lo,col_hi,row_hi)
        self._Mtotal = None        # 3x3 累积仿射矩阵
        self._initialSource = None # 最初快照（取消恢复用）
        self._dragStart = None     # 拖动起点
        self._lastWp = None        # 最近鼠标位置（松开时累积矩阵用）
        self._startAngle = 0.0     # 旋转起点角度
        self._startDist = 1.0      # 缩放起点距离
        self._lastNotify = 0.0

    def start(self) -> None:
        super().start()
        self._phase = "region"
        self._mode = None
        self._axis = None
        self._mask = None
        self._center = None
        self._stamp = None
        self._stampMask = None
        self._base = None
        self._srcBBox = None
        self._Mtotal = None
        self._initialSource = None
        self._lastWp = None
        clr = getattr(self._view, "clearSnapMarker", None)
        if callable(clr):
            clr()
        self._setStatus()

    def paramPanel(self):
        if self._panel is None:
            self._panel = RegionTransformParamPanel()
            self._panel.finished.connect(self.finish)
            self._panel.zChanged.connect(self._onZChanged)
        return self._panel

    def activeModel(self):
        return self._doc.activeReliefModel() if self._doc is not None else None

    def _notify(self, text: str) -> None:
        if self._manager is not None:
            self._manager.notifyResult(text)

    def _setStatus(self) -> None:
        if self._panel is None:
            return
        if self._phase == "region":
            self._panel.setStatus("点击闭合曲线或色块选定区域")
        elif self._phase == "pickCenter":
            self._panel.setStatus("点击一点作为旋转/缩放中心")
        elif self._phase == "mode":
            m = {"rotate": "旋转", "scale": "缩放", "move": "移动"}.get(self._mode, "")
            if m:
                ax = {"x": "（仅X轴）", "y": "（仅Y轴）"}.get(self._axis, "")
                hint = ""
                if self._mode == "rotate":
                    hint = "  再按R自定义中心"
                elif self._mode in ("scale", "move"):
                    hint = "  按X/Y约束轴"
                self._panel.setStatus(f"{m}模式{ax}：拖动变换{hint}（右键确认）")
            else:
                self._panel.setStatus("R 旋转 / S 缩放 / G 移动（右键确认，Esc 取消）")
        else:
            self._panel.setStatus("拖动中…松开完成本次变换")

    # ---------------- 区域拾取 ----------------

    def _pixelTolWorld(self) -> float:
        try:
            s = abs(self._view.transform().m11())
            if s > 1e-9:
                return self._PICK_PX / s
        except Exception:
            pass
        return 1.0

    def _pickClosedCurve(self, wp):
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

    def _selectRegion(self, model, wp) -> bool:
        """拾取闭合曲线内或色块区域，成功则设置 _mask/_center 并返回 True。"""
        hf = model.heightField()
        curve = self._pickClosedCurve(wp)
        mask = None
        if curve is not None:
            pts = sample_curve_points(curve, 256)
            grid = (hf.origin[0], hf.origin[1], hf.spacing, hf.nx, hf.ny)
            mask = build_inside_mask(pts, grid)
            if not mask.any():
                mask = None
        if mask is None:
            cm = model.colorMask()
            if cm is not None and cm.shape == (hf.ny, hf.nx):
                col, row = hf.world_to_grid(wp.x, wp.y)
                ri, ci = int(round(row)), int(round(col))
                if 0 <= ri < hf.ny and 0 <= ci < hf.nx:
                    val = int(cm[ri, ci])
                    if val > 0:
                        mask = (cm == val)
        if mask is None or not mask.any():
            return False
        self._mask = mask
        rows, cols = np.nonzero(mask)
        cx, cy = hf.grid_to_world(float(cols.mean()), float(rows.mean()))
        self._center = (cx, cy)
        return True

    # ---------------- 刷新 ----------------

    def _touchModel(self, model, force: bool = False) -> None:
        now = time.monotonic()
        if force or (now - self._lastNotify) >= 0.033:
            model.bumpVersion()
            self._doc.notifyObjectChanged(model.id())
            self._lastNotify = now

    def _refreshRegionOutline(self) -> None:
        """画区域蒙版边界预览（虚线轮廓）。

        用 numpy 向量化找边界格子，只对边界格子画线段，避免 O(ny*nx)
        Python 循环冻结 UI。
        """
        if self._mask is None or self._previewItem is None:
            return
        path = QPainterPath()
        m = self._mask
        ny, nx = m.shape
        hf = self.activeModel().heightField()
        ox, oy = hf.origin
        sp = hf.spacing
        left_edge = m & ~np.pad(m[:, :-1], ((0, 0), (1, 0)), constant_values=False)
        right_edge = m & ~np.pad(m[:, 1:], ((0, 0), (0, 1)), constant_values=False)
        top_edge = m & ~np.pad(m[:-1, :], ((1, 0), (0, 0)), constant_values=False)
        bot_edge = m & ~np.pad(m[1:, :], ((0, 1), (0, 0)), constant_values=False)
        rl, cl = np.nonzero(left_edge)
        rr, cr = np.nonzero(right_edge)
        rt, ct = np.nonzero(top_edge)
        rb, cb = np.nonzero(bot_edge)
        for r, c in zip(rl, cl):
            x = ox + c * sp
            path.moveTo(x, oy + r * sp)
            path.lineTo(x, oy + (r + 1) * sp)
        for r, c in zip(rr, cr):
            x = ox + (c + 1) * sp
            path.moveTo(x, oy + r * sp)
            path.lineTo(x, oy + (r + 1) * sp)
        for r, c in zip(rt, ct):
            y = oy + r * sp
            path.moveTo(ox + c * sp, y)
            path.lineTo(ox + (c + 1) * sp, y)
        for r, c in zip(rb, cb):
            y = oy + (r + 1) * sp
            path.moveTo(ox + c * sp, y)
            path.lineTo(ox + (c + 1) * sp, y)
        self._setPreviewPath(path, solid=False)
        if self._manager is not None:
            self._manager.invalidatePreview()

    # ---------------- 鼠标 ----------------

    def onMousePress(self, wp, mods) -> None:
        model = self.activeModel()
        if model is None:
            self._notify("请先在浮雕模式选中浮雕模型")
            return
        if self._doc is None:
            return
        if self._phase == "region":
            # 曲线/点击是世界坐标：先烘焙放置变换再建区域蒙版——bake 会
            # 替换整个母场（栅格/原点可能变），先选区后 bake 会让 mask 与
            # src 全部指向旧栅格（区域错位甚至形状不符崩溃）。
            if model.hasPendingTransform():
                model.bake()
            if self._selectRegion(model, wp):
                hf = model.heightField()
                src = hf.data.astype(np.float64)
                self._initialSource = hf.data.copy()
                mask = self._mask
                self._stamp = np.where(mask, src, 0.0)
                self._stampMask = mask.astype(np.float64)
                self._base = np.where(mask, 0.0, src)
                rows = np.any(mask, axis=1)
                cols = np.any(mask, axis=0)
                self._srcBBox = (int(np.argmax(cols)), int(np.argmax(rows)),
                                 int(len(cols) - 1 - np.argmax(cols[::-1])),
                                 int(len(rows) - 1 - np.argmax(rows[::-1])))
                self._Mtotal = np.eye(3, dtype=np.float64)

                self._phase = "mode"
                self._refreshRegionOutline()
                self._setStatus()
            else:
                self._notify("未识别到闭合曲线或色块，请点击曲线或已填色区域")
            return
        if self._phase == "pickCenter":
            self._center = (wp.x, wp.y)
            self._phase = "mode"
            self._setStatus()
            return
        if self._phase == "mode" and self._mode is not None:
            self._dragStart = wp
            cx, cy = self._center
            if self._mode == "rotate":
                self._startAngle = math.atan2(wp.y - cy, wp.x - cx)
            elif self._mode == "scale":
                self._startDist = max(math.hypot(wp.x - cx, wp.y - cy), 1e-6)
            self._phase = "drag"
            self._setStatus()

    def _dragMatrix(self, wp, cx: float, cy: float) -> np.ndarray:
        """本次拖动的变换矩阵（以 (cx,cy) 为基准，支持轴约束）。"""
        ax = self._axis
        if self._mode == "move":
            tx = wp.x - self._dragStart.x
            ty = wp.y - self._dragStart.y
            if ax == "x":
                ty = 0.0
            elif ax == "y":
                tx = 0.0
            return np.array([[1, 0, tx], [0, 1, ty], [0, 0, 1]],
                            dtype=np.float64)
        if self._mode == "rotate":
            theta = math.atan2(wp.y - cy, wp.x - cx) - self._startAngle
            c, s = math.cos(theta), math.sin(theta)
            return np.array([[c, -s, cx * (1 - c) + cy * s],
                             [s, c, cy * (1 - c) - cx * s],
                             [0, 0, 1]], dtype=np.float64)
        if self._mode == "scale":
            if ax == "x":
                ref = max(abs(self._dragStart.x - cx), 1.0)
                sc = max(1.0 + (wp.x - self._dragStart.x) / ref, 1e-3)
                return np.array([[sc, 0, cx * (1 - sc)],
                                 [0, 1, 0],
                                 [0, 0, 1]], dtype=np.float64)
            if ax == "y":
                ref = max(abs(self._dragStart.y - cy), 1.0)
                sc = max(1.0 + (wp.y - self._dragStart.y) / ref, 1e-3)
                return np.array([[1, 0, 0],
                                 [0, sc, cy * (1 - sc)],
                                 [0, 0, 1]], dtype=np.float64)
            d = math.hypot(wp.x - cx, wp.y - cy)
            sc = max(d / self._startDist, 1e-3)
            return np.array([[sc, 0, cx * (1 - sc)],
                             [0, sc, cy * (1 - sc)],
                             [0, 0, 1]], dtype=np.float64)
        return np.eye(3, dtype=np.float64)

    def _zScale(self) -> float:
        return self._panel.zScale() if self._panel is not None else 1.0

    def _applyPreview(self) -> None:
        """从 base 恢复并写入当前变换后印章（含 Z 缩放）。"""
        model = self.activeModel()
        if model is None or self._stamp is None:
            return
        hf = model.heightField()
        hf.data[:] = self._base
        M = self._Mtotal
        if self._lastWp is not None and self._center is not None:
            cx, cy = self._center
            M = self._dragMatrix(self._lastWp, cx, cy) @ self._Mtotal
        transform_region(hf, self._stamp, self._stampMask,
                         self._base, M, self._srcBBox,
                         z_scale=self._zScale())
        # 拖动中走 33ms 节流：强制刷新会让 3D 网格重建以鼠标事件频率
        # （60~125Hz）触发，大模型必卡；松手/提交路径另有 force 兜底
        self._touchModel(model, force=(self._phase != "drag"))

    def _onZChanged(self, val: float) -> None:
        if self._phase in ("mode", "drag") and self._stamp is not None:
            self._applyPreview()

    def onMouseMove(self, wp, mods) -> None:
        if self._phase != "drag" or self._stamp is None:
            return
        model = self.activeModel()
        if model is None or self._doc is None:
            return
        self._lastWp = wp
        self._applyPreview()

    def onMouseRelease(self, mods) -> None:
        if self._phase != "drag":
            return
        if self._lastWp is not None and self._center is not None:
            cx, cy = self._center
            self._Mtotal = self._dragMatrix(self._lastWp, cx, cy) @ self._Mtotal
        model = self.activeModel()
        if model is not None:
            self._touchModel(model, force=True)
        self._phase = "mode"
        self._mode = None
        self._axis = None
        self._setStatus()

    # ---------------- 键盘：R/S/G + X/Y 轴约束（仅本命令内有效）----------------

    def onKeyPress(self, key, mods) -> bool:
        if self._phase == "mode":
            if self._mode is None:
                if key == Qt.Key_R:
                    self._mode = "rotate"
                elif key == Qt.Key_S:
                    self._mode = "scale"
                elif key == Qt.Key_G:
                    self._mode = "move"
                else:
                    return False
                self._axis = None
                self._setStatus()
                return True
            if key == Qt.Key_R and self._mode == "rotate":
                self._phase = "pickCenter"
                self._setStatus()
                return True
            if key == Qt.Key_X:
                self._axis = "x"
                self._setStatus()
                return True
            if key == Qt.Key_Y:
                self._axis = "y"
                self._setStatus()
                return True
            if key == Qt.Key_R:
                self._mode = "rotate"
                self._axis = None
                self._setStatus()
                return True
            if key == Qt.Key_S:
                self._mode = "scale"
                self._axis = None
                self._setStatus()
                return True
            if key == Qt.Key_G:
                self._mode = "move"
                self._axis = None
                self._setStatus()
                return True
        return False

    def onRightClick(self, wp) -> bool:
        self._commit()
        self.finish()
        return True

    def onEnter(self) -> bool:
        self._commit()
        self.finish()
        return True

    def _commit(self) -> None:
        if self._doc is not None:
            model = self.activeModel()
            if model is not None:
                self._touchModel(model, force=True)

    def cancel(self) -> None:
        if self._doc is not None and self._initialSource is not None:
            model = self.activeModel()
            if model is not None:
                model.heightField().setData(self._initialSource)
                model.bumpVersion()
                self._doc.notifyObjectChanged(model.id())
        super().cancel()

    def finish(self) -> None:
        self._commit()
        super().finish()

    def showSnapHints(self) -> bool:
        return False

    def prompt(self) -> str:
        if self.activeModel() is None:
            return "请先在浮雕模式选中浮雕模型"
        if self._phase == "region":
            return "点击闭合曲线或色块选定区域"
        if self._phase == "pickCenter":
            return "点击一点作为旋转/缩放中心"
        if self._phase == "mode":
            return "R 旋转 / S 缩放 / G 移动，X/Y 约束轴，R再R自定义中心（右键确认，Esc 取消）"
        return "拖动变换中…松开完成本次变换"

    def name(self) -> str:
        return "RegionTransform"

    def commandTitle(self) -> str:
        return "区域变换"


class RegionArrayCommand(CommandInstance):
    """区域阵列：选定区域（闭合曲线内或色块）作为印章，按矩形/圆形/曲线
    三种模式阵列分布多个副本。

    交互：激活后点击闭合曲线/色块选定区域 → 根据模式拾取（矩形无需拾取、
    圆形拾取圆心、曲线拾取路径曲线）→ 调参实时预览 → 右键/回车确认，Esc 取消。
    沿程渐变（等粗/中粗/首粗/尾粗）借鉴路径塑形 _width_scale。
    """

    _PICK_PX = 8.0

    def __init__(self) -> None:
        super().__init__()
        self._panel: RegionArrayParamPanel | None = None
        # region=选区域 / center=选圆心 / path=选路径 / param=调参
        self._phase = "region"
        self._mask = None
        self._center = None        # (cx, cy) 印章中心世界坐标
        self._stamp = None
        self._stampMask = None
        self._srcBBox = None
        self._source = None        # 原始高度场快照
        self._centerBase = None    # (cx, cy) 圆心世界坐标
        self._curveObj = None
        self._curvePts = None      # (N,2) 世界坐标
        self._lastNotify = 0.0

    def start(self) -> None:
        super().start()
        self._phase = "region"
        self._mask = None
        self._center = None
        self._stamp = None
        self._stampMask = None
        self._srcBBox = None
        self._source = None
        self._centerBase = None
        self._curveObj = None
        self._curvePts = None
        clr = getattr(self._view, "clearSnapMarker", None)
        if callable(clr):
            clr()
        self._setStatus()

    def paramPanel(self):
        if self._panel is None:
            self._panel = RegionArrayParamPanel()
            self._panel.finished.connect(self.finish)
            self._panel.paramsChanged.connect(self._onParamsChanged)
            self._panel.pickCurveRequested.connect(self._onPickCurveRequested)
            self._panel.pickCenterRequested.connect(self._onPickCenterRequested)
            self._panel.modeChanged.connect(self._onModeChanged)
        return self._panel

    def activeModel(self):
        return self._doc.activeReliefModel() if self._doc is not None else None

    def _notify(self, text: str) -> None:
        if self._manager is not None:
            self._manager.notifyResult(text)

    def _setStatus(self) -> None:
        if self._panel is None:
            return
        if self._phase == "region":
            self._panel.setStatus("点击闭合曲线或色块选定区域")
        elif self._phase == "center":
            self._panel.setStatus("点击圆心位置")
        elif self._phase == "path":
            self._panel.setStatus("点击路径曲线（阵列沿此曲线分布）")
        else:
            self._panel.setStatus("调参预览中（右键确认，Esc 取消）")

    def _phaseForMode(self, mode: int) -> str:
        """模式对应的拾取阶段（矩形无需拾取直接进 param）。"""
        if mode == 1:
            return "center"
        if mode == 2:
            return "path"
        return "param"

    # ---------------- 区域拾取（同区域变换） ----------------

    def _pixelTolWorld(self) -> float:
        try:
            s = abs(self._view.transform().m11())
            if s > 1e-9:
                return self._PICK_PX / s
        except Exception:
            pass
        return 1.0

    def _pickClosedCurve(self, wp):
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

    def _selectRegion(self, model, wp) -> bool:
        hf = model.heightField()
        curve = self._pickClosedCurve(wp)
        mask = None
        if curve is not None:
            pts = sample_curve_points(curve, 256)
            grid = (hf.origin[0], hf.origin[1], hf.spacing, hf.nx, hf.ny)
            mask = build_inside_mask(pts, grid)
            if not mask.any():
                mask = None
        if mask is None:
            cm = model.colorMask()
            if cm is not None and cm.shape == (hf.ny, hf.nx):
                col, row = hf.world_to_grid(wp.x, wp.y)
                ri, ci = int(round(row)), int(round(col))
                if 0 <= ri < hf.ny and 0 <= ci < hf.nx:
                    val = int(cm[ri, ci])
                    if val > 0:
                        mask = (cm == val)
        if mask is None or not mask.any():
            return False
        self._mask = mask
        rows, cols = np.nonzero(mask)
        cx, cy = hf.grid_to_world(float(cols.mean()), float(rows.mean()))
        self._center = (cx, cy)
        return True

    # ---------------- 路径曲线拾取 ----------------

    def _pickPathCurve(self, wp):
        if self._doc is None:
            return None
        tol = self._pixelTolWorld()
        best, best_d = None, tol
        for o in self._doc.visibleObjects():
            if o.type() == ObjectType.SURFACE or o.isLocked():
                continue
            t = o.type().name
            if t not in ("LINE", "CURVE", "CIRCLE", "ELLIPSE",
                         "ARC", "POLYCURVE"):
                continue
            pts = sample_curve_points(o, 128)
            if pts.shape[0] < 2:
                continue
            d = _pointPolylineDistance(wp.x, wp.y, pts)
            if d < best_d:
                best, best_d = o, d
        return best

    # ---------------- 刷新 ----------------

    def _touchModel(self, model, force: bool = False) -> None:
        now = time.monotonic()
        if force or (now - self._lastNotify) >= 0.033:
            model.bumpVersion()
            self._doc.notifyObjectChanged(model.id())
            self._lastNotify = now

    def _refreshRegionOutline(self) -> None:
        if self._mask is None or self._previewItem is None:
            return
        path = QPainterPath()
        m = self._mask
        ny, nx = m.shape
        hf = self.activeModel().heightField()
        ox, oy = hf.origin
        sp = hf.spacing
        left_edge = m & ~np.pad(m[:, :-1], ((0, 0), (1, 0)), constant_values=False)
        right_edge = m & ~np.pad(m[:, 1:], ((0, 0), (0, 1)), constant_values=False)
        top_edge = m & ~np.pad(m[:-1, :], ((1, 0), (0, 0)), constant_values=False)
        bot_edge = m & ~np.pad(m[1:, :], ((0, 1), (0, 0)), constant_values=False)
        rl, cl = np.nonzero(left_edge)
        rr, cr = np.nonzero(right_edge)
        rt, ct = np.nonzero(top_edge)
        rb, cb = np.nonzero(bot_edge)
        for r, c in zip(rl, cl):
            x = ox + c * sp
            path.moveTo(x, oy + r * sp)
            path.lineTo(x, oy + (r + 1) * sp)
        for r, c in zip(rr, cr):
            x = ox + (c + 1) * sp
            path.moveTo(x, oy + r * sp)
            path.lineTo(x, oy + (r + 1) * sp)
        for r, c in zip(rt, ct):
            y = oy + r * sp
            path.moveTo(ox + c * sp, y)
            path.lineTo(ox + (c + 1) * sp, y)
        for r, c in zip(rb, cb):
            y = oy + (r + 1) * sp
            path.moveTo(ox + c * sp, y)
            path.lineTo(ox + (c + 1) * sp, y)
        self._setPreviewPath(path, solid=False)
        if self._manager is not None:
            self._manager.invalidatePreview()

    def _refreshPathCurve(self) -> None:
        if self._curvePts is None or self._previewItem is None:
            return
        path = QPainterPath()
        pts = self._curvePts
        path.moveTo(float(pts[0, 0]), float(pts[0, 1]))
        for i in range(1, pts.shape[0]):
            path.lineTo(float(pts[i, 0]), float(pts[i, 1]))
        self._setPreviewPath(path, solid=False)
        if self._manager is not None:
            self._manager.invalidatePreview()

    # ---------------- 预览 ----------------

    def _buildParams(self) -> dict | None:
        if self._panel is None:
            return None
        mode = self._panel.mode()
        bt = self._panel.brushType()
        tf = self._panel.thinFrac()
        zs = self._panel.zScale()
        if mode == 0:
            return {
                "count_x": self._panel.rectX(),
                "count_y": self._panel.rectY(),
                "gap_x": self._panel.rectGapX(),
                "gap_y": self._panel.rectGapY(),
                "brush_type": bt, "thin_frac": tf, "z_scale": zs,
            }
        if mode == 1:
            if self._centerBase is None:
                return None
            return {
                "center_base": self._centerBase,
                "count": self._panel.circleCnt(),

                "angle_range": self._panel.circleRange(),
                "rotate_objects": self._panel.circleRotate(),
                "brush_type": bt, "thin_frac": tf, "z_scale": zs,
            }
        if self._curvePts is None:
            return None
        return {
            "curve_pts": self._curvePts,
            "count": self._panel.curveCnt(),
            "spacing": self._panel.curveSpacing(),
            "fill": self._panel.curveFill(),
            "start_pct": self._panel.curveStart(),
            "rotate_tangent": self._panel.curveRotate(),
            "on_curve": self._panel.curveOnCurve(),
            "brush_type": bt, "thin_frac": tf, "z_scale": zs,
        }

    def _applyPreview(self) -> None:
        model = self.activeModel()
        if model is None or self._stamp is None or self._panel is None:
            return
        params = self._buildParams()
        if params is None:
            return
        preview_array(model.heightField(), self._source,
                      self._stamp, self._stampMask, self._srcBBox,
                      self._center, self._panel.mode(), params)
        self._touchModel(model, force=True)

    def _onParamsChanged(self) -> None:
        if self._phase == "param" and self._stamp is not None:
            self._applyPreview()

    def _onPickCurveRequested(self) -> None:
        if self._phase == "param":
            self._phase = "path"
            self._setStatus()

    def _onPickCenterRequested(self) -> None:
        if self._phase == "param":
            self._phase = "center"
            self._setStatus()

    def _onModeChanged(self, mode: int) -> None:
        if self._phase == "param":
            new_phase = self._phaseForMode(mode)
            need_pick = new_phase in ("center", "path")
            has_data = ((new_phase == "center" and self._centerBase is not None)
                        or (new_phase == "path" and self._curvePts is not None))
            if need_pick and not has_data:
                self._phase = new_phase
            else:
                self._phase = "param"
                self._applyPreview()
            self._setStatus()
        elif self._phase in ("center", "path"):
            self._phase = self._phaseForMode(mode)
            self._setStatus()

    # ---------------- 鼠标 ----------------

    def onMousePress(self, wp, mods) -> None:
        model = self.activeModel()
        if model is None:
            self._notify("请先在浮雕模式选中浮雕模型")
            return
        if self._doc is None:
            return
        if self._phase == "region":
            # 先 bake 再选区：bake 替换整个母场，选区蒙版必须基于新栅格
            if model.hasPendingTransform():
                model.bake()
            if self._selectRegion(model, wp):
                hf = model.heightField()
                src = hf.data.astype(np.float64)
                self._source = src
                mask = self._mask
                self._stamp = np.where(mask, src, 0.0)
                self._stampMask = mask.astype(np.float64)
                rows = np.any(mask, axis=1)
                cols = np.any(mask, axis=0)
                self._srcBBox = (int(np.argmax(cols)), int(np.argmax(rows)),
                                 int(len(cols) - 1 - np.argmax(cols[::-1])),
                                 int(len(rows) - 1 - np.argmax(rows[::-1])))
                mode = self._panel.mode() if self._panel else 0
                self._phase = self._phaseForMode(mode)
                self._refreshRegionOutline()
                if self._phase == "param":
                    self._applyPreview()
                self._setStatus()
            else:
                self._notify("未识别到闭合曲线或色块，请点击曲线或已填色区域")
            return
        if self._phase == "center":
            self._centerBase = (wp.x, wp.y)
            if self._panel is not None:
                self._panel.setCenterLabel(
                    f"圆心：({wp.x:.1f}, {wp.y:.1f})")
            self._phase = "param"
            self._applyPreview()
            self._setStatus()
            return
        if self._phase == "path":
            curve = self._pickPathCurve(wp)
            if curve is not None:
                self._curveObj = curve
                pts = sample_curve_points(curve, 512)
                if pts.shape[0] >= 2:
                    self._curvePts = pts
                    self._phase = "param"
                    if self._panel is not None:
                        self._panel.setCurveLabel(
                            f"路径：{curve.type().name}")
                    self._refreshPathCurve()
                    self._applyPreview()
                    self._setStatus()
            else:
                self._notify("未识别到曲线，请点击路径曲线")
            return

    def onMouseMove(self, wp, mods) -> None:
        pass

    def onRightClick(self, wp) -> bool:
        self._commit()
        self.finish()
        return True

    def onEnter(self) -> bool:
        self._commit()
        self.finish()
        return True

    def _commit(self) -> None:
        if self._doc is not None:
            model = self.activeModel()
            if model is not None:
                self._touchModel(model, force=True)

    def cancel(self) -> None:
        if self._doc is not None and self._source is not None:
            model = self.activeModel()
            if model is not None:
                model.heightField().setData(self._source.astype(np.float32))
                model.bumpVersion()
                self._doc.notifyObjectChanged(model.id())
        super().cancel()

    def finish(self) -> None:
        self._commit()
        super().finish()

    def showSnapHints(self) -> bool:
        return self._phase == "center"

    def prompt(self) -> str:
        if self.activeModel() is None:
            return "请先在浮雕模式选中浮雕模型"
        if self._phase == "region":
            return "点击闭合曲线或色块选定区域"
        if self._phase == "center":
            return "点击圆心位置"
        if self._phase == "path":
            return "点击路径曲线（阵列沿此曲线分布）"
        return "调参预览中（右键确认，Esc 取消）"

    def name(self) -> str:
        return "RegionArray"

    def commandTitle(self) -> str:
        return "区域阵列"


class RegionSymmetryCommand(CommandInstance):
    """区域对称：把高度场内容关于对称轴镜像到另一侧。

    左右/上下对称：
      整个模型：点击一侧确定保留侧，对称轴=模型中线。
      区域：选区域 → 点2个点定轴位置 → 预览。
    自由对称（任意角度轴，仅区域模式）：
      区域：选区域 → 点第1个点 → 移动鼠标实时预览 → 点第2个点确认。
    右键/回车确认，Esc 取消。
    """

    _PICK_PX = 8.0

    def __init__(self) -> None:
        super().__init__()
        self._panel: RegionSymmetryParamPanel | None = None
        self._phase = "init"
        self._mask = None
        self._source = None
        self._axisX = 0.0
        self._axisY = 0.0
        self._pt1 = None
        self._pt2X = 0.0
        self._pt2Y = 0.0
        self._hasRegion = False
        self._hasAxis = False
        self._keepSide = "left"
        self._lastDir = "left_right"
        self._lastNotify = 0.0

    def start(self) -> None:
        super().start()
        self._mask = None
        self._source = None
        self._pt1 = None
        self._pt2X = 0.0
        self._pt2Y = 0.0
        self._hasRegion = False
        self._hasAxis = False
        self._keepSide = "left"
        self._lastDir = self._panel.direction() if self._panel else "left_right"
        clr = getattr(self._view, "clearSnapMarker", None)
        if callable(clr):
            clr()
        d = self._panel.direction() if self._panel else "left_right"
        if self._scope() == "model" and d != "free":
            model = self.activeModel()
            if model is not None:
                self._snapshot(model)
                self._modelCenterAxis(model.heightField())
        self._phase = self._initialPhase()
        if self._phase == "click" and self._hasAxis:
            self._keepSide = "left"
            self._refreshAxisLine()
            self._applyPreview()
        self._setStatus()

    def paramPanel(self):
        if self._panel is None:
            self._panel = RegionSymmetryParamPanel()
            self._panel.finished.connect(self.finish)
            self._panel.paramsChanged.connect(self._onParamsChanged)
        return self._panel

    def activeModel(self):
        return self._doc.activeReliefModel() if self._doc is not None else None

    def _notify(self, text: str) -> None:
        if self._manager is not None:
            self._manager.notifyResult(text)

    def _scope(self) -> str:
        return self._panel.scope() if self._panel else "region"

    def _initialPhase(self) -> str:
        scope = self._scope()
        d = self._panel.direction() if self._panel else "left_right"
        if scope == "region":
            return "region"
        if d == "free":
            return "click"
        return "click"

    def _setStatus(self) -> None:
        if self._panel is None:
            return
        if self._phase == "region":
            self._panel.setStatus("点击闭合曲线或色块选定区域")
        elif self._phase == "pt1":
            self._panel.setStatus("点击第1个点确定对称轴")
        elif self._phase == "pt2":
            self._panel.setStatus("点击第2个点确定对称轴")
        elif self._phase == "click":
            self._panel.setStatus("点击模型一侧：该侧保留，另一侧镜像")
        else:
            self._panel.setStatus("预览中（右键确认，Esc 取消）")

    # ---------------- 区域拾取 ----------------

    def _pixelTolWorld(self) -> float:
        try:
            s = abs(self._view.transform().m11())
            if s > 1e-9:
                return self._PICK_PX / s
        except Exception:
            pass
        return 1.0

    def _pickClosedCurve(self, wp):
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

    def _selectRegion(self, model, wp) -> bool:
        hf = model.heightField()
        curve = self._pickClosedCurve(wp)
        mask = None
        if curve is not None:
            pts = sample_curve_points(curve, 256)
            grid = (hf.origin[0], hf.origin[1], hf.spacing, hf.nx, hf.ny)
            mask = build_inside_mask(pts, grid)
            if not mask.any():
                mask = None
        if mask is None:
            cm = model.colorMask()
            if cm is not None and cm.shape == (hf.ny, hf.nx):
                col, row = hf.world_to_grid(wp.x, wp.y)
                ri, ci = int(round(row)), int(round(col))
                if 0 <= ri < hf.ny and 0 <= ci < hf.nx:
                    val = int(cm[ri, ci])
                    if val > 0:
                        mask = (cm == val)
        if mask is None or not mask.any():
            return False
        self._mask = mask
        return True

    def _snapshot(self, model) -> None:
        if model.hasPendingTransform():
            model.bake()
        self._source = model.heightField().data.astype(np.float64).copy()

    def _modelCenterAxis(self, hf) -> None:
        ox, oy = hf.origin
        sp = hf.spacing
        self._axisX = ox + hf.nx * sp * 0.5
        self._axisY = oy + hf.ny * sp * 0.5
        self._hasAxis = True

    # ---------------- 刷新 ----------------

    def _touchModel(self, model, force: bool = False) -> None:
        now = time.monotonic()
        if force or (now - self._lastNotify) >= 0.033:
            model.bumpVersion()
            self._doc.notifyObjectChanged(model.id())
            self._lastNotify = now

    def _refreshRegionOutline(self) -> None:
        if self._mask is None or self._previewItem is None:
            return
        path = QPainterPath()
        m = self._mask
        ny, nx = m.shape
        hf = self.activeModel().heightField()
        ox, oy = hf.origin
        sp = hf.spacing
        left_edge = m & ~np.pad(m[:, :-1], ((0, 0), (1, 0)), constant_values=False)
        right_edge = m & ~np.pad(m[:, 1:], ((0, 0), (0, 1)), constant_values=False)
        top_edge = m & ~np.pad(m[:-1, :], ((1, 0), (0, 0)), constant_values=False)
        bot_edge = m & ~np.pad(m[1:, :], ((0, 1), (0, 0)), constant_values=False)
        rl, cl = np.nonzero(left_edge)
        rr, cr = np.nonzero(right_edge)
        rt, ct = np.nonzero(top_edge)
        rb, cb = np.nonzero(bot_edge)
        for r, c in zip(rl, cl):
            x = ox + c * sp
            path.moveTo(x, oy + r * sp)
            path.lineTo(x, oy + (r + 1) * sp)
        for r, c in zip(rr, cr):
            x = ox + (c + 1) * sp
            path.moveTo(x, oy + r * sp)
            path.lineTo(x, oy + (r + 1) * sp)
        for r, c in zip(rt, ct):
            y = oy + r * sp
            path.moveTo(ox + c * sp, y)
            path.lineTo(ox + (c + 1) * sp, y)
        for r, c in zip(rb, cb):
            y = oy + (r + 1) * sp
            path.moveTo(ox + c * sp, y)
            path.lineTo(ox + (c + 1) * sp, y)
        self._setPreviewPath(path, solid=False)
        if self._manager is not None:
            self._manager.invalidatePreview()

    def _refreshAxisLine(self) -> None:
        if not self._hasAxis or self._previewItem is None or self._panel is None:
            return
        model = self.activeModel()
        if model is None:
            return
        hf = model.heightField()
        ox, oy = hf.origin
        sp = hf.spacing
        path = QPainterPath()
        d = self._panel.direction()
        if d == "left_right":
            x = self._axisX
            path.moveTo(x, oy)
            path.lineTo(x, oy + hf.ny * sp)
        elif d == "top_bottom":
            y = self._axisY
            path.moveTo(ox, y)
            path.lineTo(ox + hf.nx * sp, y)
        else:
            dx = self._pt2X - self._axisX
            dy = self._pt2Y - self._axisY
            L = float(np.hypot(dx, dy))
            if L > 1e-9:
                dx /= L
                dy /= L
                ex = max(hf.nx * sp, hf.ny * sp) * 2.0
                path.moveTo(self._axisX - dx * ex, self._axisY - dy * ex)
                path.lineTo(self._axisX + dx * ex, self._axisY + dy * ex)
        self._setPreviewPath(path, solid=False)
        if self._manager is not None:
            self._manager.invalidatePreview()

    # ---------------- 预览 ----------------

    def _applyPreview(self) -> None:
        model = self.activeModel()
        if model is None or self._source is None or self._panel is None:
            return
        if not self._hasAxis:
            return
        scope = self._panel.scope()
        if scope == "region" and not self._hasRegion:
            return
        mask = self._mask if scope == "region" else None
        symmetry_field(model.heightField(), self._source, mask,
                       self._axisX, self._axisY,
                       self._panel.direction(), scope,
                       self._keepSide,
                       self._pt2X, self._pt2Y)
        # 鼠标拖动轴/保留侧每帧调用：走 33ms 节流，提交（_commit）时强制刷新
        self._touchModel(model)

    def _onParamsChanged(self) -> None:
        scope = self._scope()
        curDir = self._panel.direction() if self._panel else "left_right"
        dirChanged = curDir != self._lastDir
        self._lastDir = curDir
        if dirChanged:
            self._hasAxis = False
            self._pt1 = None
            if scope == "model" and curDir != "free":
                model = self.activeModel()
                if model is not None:
                    if self._source is None:
                        self._snapshot(model)
                    else:
                        model.heightField().data[:] = self._source.astype(np.float32)
                    self._modelCenterAxis(model.heightField())
            self._phase = self._initialPhase()
            if self._phase == "click" and self._hasAxis:
                self._keepSide = "left"
                self._refreshAxisLine()
                self._applyPreview()
            self._setStatus()
            return
        if scope == "model":
            if self._phase == "param":
                self._refreshAxisLine()
                self._applyPreview()
            else:
                if curDir != "free" and not self._hasAxis:
                    model = self.activeModel()
                    if model is not None:
                        if self._source is None:
                            self._snapshot(model)
                        else:
                            model.heightField().data[:] = self._source.astype(np.float32)
                        self._modelCenterAxis(model.heightField())
                self._phase = self._initialPhase()
                if self._phase == "click" and self._hasAxis:
                    self._keepSide = "left"
                    self._refreshAxisLine()
                    self._applyPreview()
                self._setStatus()
        else:
            if self._phase == "param" and not self._hasRegion:
                self._phase = "region"
                self._setStatus()
            elif self._phase == "param" and self._hasRegion:
                self._refreshAxisLine()
                self._applyPreview()
            elif self._phase == "click":
                self._phase = "region" if not self._hasRegion else "pt1"
                self._setStatus()

    # ---------------- 鼠标 ----------------

    def onMousePress(self, wp, mods) -> None:
        model = self.activeModel()
        if model is None:
            self._notify("请先在浮雕模式选中浮雕模型")
            return
        if self._doc is None:
            return
        d = self._panel.direction() if self._panel else "left_right"
        if self._phase == "region":
            # 先 bake 再选区（_snapshot 内的 bake 检查随之变为 no-op）：
            # 对称镜像作用于选区蒙版，蒙版必须基于烘焙后的新栅格
            if model.hasPendingTransform():
                model.bake()
            if self._selectRegion(model, wp):
                self._snapshot(model)
                self._hasRegion = True
                self._phase = "pt1"
                self._refreshRegionOutline()
                self._setStatus()
            else:
                self._notify("未识别到闭合曲线或色块")
            return
        if self._phase == "pt1":
            self._pt1 = (wp.x, wp.y)
            self._phase = "pt2"
            self._setStatus()
            return
        if self._phase == "pt2":
            if self._pt1 is not None:
                if d == "free":
                    self._axisX = self._pt1[0]
                    self._axisY = self._pt1[1]
                    self._pt2X = wp.x
                    self._pt2Y = wp.y
                else:
                    self._axisX = (self._pt1[0] + wp.x) * 0.5
                    self._axisY = (self._pt1[1] + wp.y) * 0.5
                    self._pt2X = wp.x
                    self._pt2Y = wp.y
                self._hasAxis = True
                self._phase = "param"
                self._refreshAxisLine()
                self._applyPreview()
                self._setStatus()
            return
        if self._phase == "click":
            if self._source is None:
                self._snapshot(model)
            if not self._hasAxis:
                self._modelCenterAxis(model.heightField())
            if d == "left_right":
                self._keepSide = "left" if wp.x < self._axisX else "right"
            else:
                self._keepSide = "top" if wp.y < self._axisY else "bottom"
            self._phase = "param"
            self._refreshAxisLine()
            self._applyPreview()
            self._setStatus()
            return

    def onMouseMove(self, wp, mods) -> None:
        if self._panel is None or self._source is None:
            return
        d = self._panel.direction()
        if self._phase == "pt2" and self._pt1 is not None:
            if d == "free":
                self._axisX = self._pt1[0]
                self._axisY = self._pt1[1]
                self._pt2X = wp.x
                self._pt2Y = wp.y
            else:
                self._axisX = (self._pt1[0] + wp.x) * 0.5
                self._axisY = (self._pt1[1] + wp.y) * 0.5
                self._pt2X = wp.x
                self._pt2Y = wp.y
            self._hasAxis = True
            self._refreshAxisLine()
            self._applyPreview()
        elif self._phase == "click" and self._hasAxis:
            if d == "left_right":
                self._keepSide = "left" if wp.x < self._axisX else "right"
            else:
                self._keepSide = "top" if wp.y < self._axisY else "bottom"
            self._applyPreview()


    def onRightClick(self, wp) -> bool:
        self._commit()
        self.finish()
        return True

    def onEnter(self) -> bool:
        self._commit()
        self.finish()
        return True

    def _commit(self) -> None:
        if self._doc is not None:
            model = self.activeModel()
            if model is not None:
                self._touchModel(model, force=True)

    def cancel(self) -> None:
        if self._doc is not None and self._source is not None:
            model = self.activeModel()
            if model is not None:
                model.heightField().setData(self._source.astype(np.float32))
                model.bumpVersion()
                self._doc.notifyObjectChanged(model.id())
        super().cancel()

    def finish(self) -> None:
        self._commit()
        super().finish()

    def showSnapHints(self) -> bool:
        return self._phase in ("pt1", "pt2", "click")

    def prompt(self) -> str:
        if self.activeModel() is None:
            return "请先在浮雕模式选中浮雕模型"
        if self._phase == "region":
            return "点击闭合曲线或色块选定区域"
        if self._phase == "pt1":
            return "点击第1个点确定对称轴"
        if self._phase == "pt2":
            return "点击第2个点确定对称轴"
        if self._phase == "click":
            return "点击模型一侧：该侧保留，另一侧镜像"
        return "预览中（右键确认，Esc 取消）"

    def name(self) -> str:
        return "RegionSymmetry"

    def commandTitle(self) -> str:
        return "区域对称"


# ============================================================================
# RegionDeformCommand：区域变形（封套变形，圆→矩形等）
# ============================================================================

class RegionDeformCommand(CommandInstance):
    """区域变形（封套变形）：点选源区域 → 点选目标轮廓 → 把源区域高度场
    按轮廓对应均匀变形到目标轮廓区域（如圆变矩形）。

    用 Mean Value Coordinates 反向映射：目标区域每格反查源坐标采样，
    保证完整填充无空洞。源内容作独立印章，原位置清空，可变形到模型任意位置。
    Z 参数：变形后高度均匀缩放。
    """

    _PICK_PX = 8.0

    def __init__(self) -> None:
        super().__init__()
        self._panel: RegionDeformParamPanel | None = None
        self._phase = "region"     # region / target / param
        self._mask = None          # 源区域蒙版 (ny,nx) bool
        self._srcCurve = None      # 源闭合曲线对象（色块则为 None）
        self._srcPoly = None       # 源轮廓世界采样点 (K,2)
        self._dstPoly = None       # 目标轮廓世界采样点 (K,2)
        self._dstMask = None       # 目标轮廓内部蒙版 (ny,nx) bool
        self._source = None        # 完整源高度场 float64
        self._base = None          # where(src_mask, 0, source) 原位置清空底场
        self._initialSource = None # 最初快照（取消恢复用）
        self._lastNotify = 0.0

    def start(self) -> None:
        super().start()
        self._phase = "region"
        self._mask = None
        self._srcCurve = None
        self._srcPoly = None
        self._dstPoly = None
        self._dstMask = None
        self._source = None
        self._base = None
        self._initialSource = None
        clr = getattr(self._view, "clearSnapMarker", None)
        if callable(clr):
            clr()
        self._setStatus()

    def paramPanel(self):
        if self._panel is None:
            self._panel = RegionDeformParamPanel()
            self._panel.finished.connect(self.finish)
            self._panel.zChanged.connect(self._onZChanged)
        return self._panel

    def activeModel(self):
        return self._doc.activeReliefModel() if self._doc is not None else None

    def _notify(self, text: str) -> None:
        if self._manager is not None:
            self._manager.notifyResult(text)

    def _setStatus(self) -> None:
        if self._panel is None:
            return
        if self._phase == "region":
            self._panel.setStatus("点击闭合曲线或色块选定源区域")
        elif self._phase == "target":
            self._panel.setStatus("点击目标闭合曲线（源将变形到该轮廓内）")
        else:
            self._panel.setStatus("预览中：调 Z 缩放，右键/回车确认（Esc 取消）")

    # ---------------- 拾取 ----------------

    def _pixelTolWorld(self) -> float:
        try:
            s = abs(self._view.transform().m11())
            if s > 1e-9:
                return self._PICK_PX / s
        except Exception:
            pass
        return 1.0

    def _pickClosedCurve(self, wp):
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

    def _selectRegion(self, model, wp) -> bool:
        """拾取闭合曲线内或色块区域，成功则设置 _mask/_srcCurve 并返回 True。"""
        hf = model.heightField()
        curve = self._pickClosedCurve(wp)
        mask = None
        if curve is not None:
            pts = sample_curve_points(curve, 256)
            grid = (hf.origin[0], hf.origin[1], hf.spacing, hf.nx, hf.ny)
            mask = build_inside_mask(pts, grid)
            if not mask.any():
                mask = None
        if mask is None:
            cm = model.colorMask()
            if cm is not None and cm.shape == (hf.ny, hf.nx):
                col, row = hf.world_to_grid(wp.x, wp.y)
                ri, ci = int(round(row)), int(round(col))
                if 0 <= ri < hf.ny and 0 <= ci < hf.nx:
                    val = int(cm[ri, ci])
                    if val > 0:
                        mask = (cm == val)
                        curve = None
        if mask is None or not mask.any():
            return False
        self._mask = mask
        self._srcCurve = curve
        return True

    def _srcPolyFromRegion(self, model):
        """源轮廓世界坐标采样点。曲线源则采样；色块源则从 mask 边界提取。"""
        hf = model.heightField()
        if self._srcCurve is not None:
            pts = sample_curve_points(self._srcCurve, 256)
            if pts.shape[0] >= 3:
                return pts
        from scipy import ndimage
        contours = ndimage.find_contours(self._mask.astype(np.float64), 0.5)
        if not contours:
            return None
        c = max(contours, key=len)
        if c.shape[0] < 3:
            return None
        ox, oy = hf.origin
        sp = hf.spacing
        return np.column_stack([ox + c[:, 1] * sp, oy + c[:, 0] * sp])

    # ---------------- 刷新 ----------------

    def _touchModel(self, model, force: bool = False) -> None:
        now = time.monotonic()
        if force or (now - self._lastNotify) >= 0.033:
            model.bumpVersion()
            self._doc.notifyObjectChanged(model.id())
            self._lastNotify = now

    def _drawMaskOutline(self, mask: np.ndarray) -> None:
        """画 mask 边界虚线轮廓预览。"""
        if mask is None or self._previewItem is None:
            return
        path = QPainterPath()
        m = mask
        ny, nx = m.shape
        hf = self.activeModel().heightField()
        ox, oy = hf.origin
        sp = hf.spacing
        left_edge = m & ~np.pad(m[:, :-1], ((0, 0), (1, 0)), constant_values=False)
        right_edge = m & ~np.pad(m[:, 1:], ((0, 0), (0, 1)), constant_values=False)
        top_edge = m & ~np.pad(m[:-1, :], ((1, 0), (0, 0)), constant_values=False)
        bot_edge = m & ~np.pad(m[1:, :], ((0, 1), (0, 0)), constant_values=False)
        rl, cl = np.nonzero(left_edge)
        rr, cr = np.nonzero(right_edge)
        rt, ct = np.nonzero(top_edge)
        rb, cb = np.nonzero(bot_edge)
        for r, c in zip(rl, cl):
            x = ox + c * sp
            path.moveTo(x, oy + r * sp)
            path.lineTo(x, oy + (r + 1) * sp)
        for r, c in zip(rr, cr):
            x = ox + (c + 1) * sp
            path.moveTo(x, oy + r * sp)
            path.lineTo(x, oy + (r + 1) * sp)
        for r, c in zip(rt, ct):
            y = oy + r * sp
            path.moveTo(ox + c * sp, y)
            path.lineTo(ox + (c + 1) * sp, y)
        for r, c in zip(rb, cb):
            y = oy + (r + 1) * sp
            path.moveTo(ox + c * sp, y)
            path.lineTo(ox + (c + 1) * sp, y)
        self._setPreviewPath(path, solid=False)
        if self._manager is not None:
            self._manager.invalidatePreview()

    # ---------------- 鼠标 ----------------

    def onMousePress(self, wp, mods) -> None:
        model = self.activeModel()
        if model is None:
            self._notify("请先在浮雕模式选中浮雕模型")
            return
        if self._doc is None:
            return
        if self._phase == "region":
            # 先 bake 再选区/取轮廓：变形把源区域搬进目标曲线，src 与蒙版
            # 必须基于烘焙后的新栅格
            if model.hasPendingTransform():
                model.bake()
            if self._selectRegion(model, wp):
                hf = model.heightField()
                src_poly = self._srcPolyFromRegion(model)
                if src_poly is None or src_poly.shape[0] < 3:
                    self._notify("无法提取源区域轮廓")
                    return
                src = hf.data.astype(np.float64)
                self._initialSource = hf.data.copy()
                self._source = src
                self._base = np.where(self._mask, 0.0, src)
                self._srcPoly = src_poly
                self._phase = "target"
                self._drawMaskOutline(self._mask)
                self._setStatus()
            else:
                self._notify("未识别到闭合曲线或色块，请点击曲线或已填色区域")
            return
        if self._phase == "target":
            curve = self._pickClosedCurve(wp)
            if curve is None:
                self._notify("未识别到目标闭合曲线，请点击目标轮廓曲线")
                return
            hf = model.heightField()
            dst_poly = sample_curve_points(curve, 256)
            if dst_poly.shape[0] < 3:
                self._notify("目标曲线采样点不足")
                return
            grid = (hf.origin[0], hf.origin[1], hf.spacing, hf.nx, hf.ny)
            dst_mask = build_inside_mask(dst_poly, grid)
            if not dst_mask.any():
                self._notify("目标轮廓内区域为空")
                return
            self._dstPoly = dst_poly
            self._dstMask = dst_mask
            self._phase = "param"
            self._applyPreview()
            self._setStatus()
            return

    def _zScale(self) -> float:
        return self._panel.zScale() if self._panel is not None else 1.0

    def _applyPreview(self) -> None:
        """从 base 恢复并写入封套变形后印章（含 Z 缩放）。"""
        model = self.activeModel()
        if model is None or self._source is None or self._dstPoly is None:
            return
        hf = model.heightField()
        hf.data[:] = self._base
        deform_region(hf, self._source, self._base,
                      self._srcPoly, self._dstPoly, self._dstMask,
                      z_scale=self._zScale())
        self._touchModel(model, force=True)

    def _onZChanged(self, val: float) -> None:
        if self._phase == "param" and self._source is not None:
            self._applyPreview()

    def onMouseMove(self, wp, mods) -> None:
        return

    def onMouseRelease(self, mods) -> None:
        return

    def onRightClick(self, wp) -> bool:
        if self._phase == "param":
            self._commit()
            self.finish()
            return True
        return False

    def onEnter(self) -> bool:
        if self._phase == "param":
            self._commit()
            self.finish()
            return True
        return False

    def _commit(self) -> None:
        if self._doc is not None:
            model = self.activeModel()
            if model is not None:
                self._touchModel(model, force=True)

    def cancel(self) -> None:
        if self._doc is not None and self._initialSource is not None:
            model = self.activeModel()
            if model is not None:
                model.heightField().setData(self._initialSource)
                model.bumpVersion()
                self._doc.notifyObjectChanged(model.id())
        super().cancel()

    def finish(self) -> None:
        self._commit()
        super().finish()

    def showSnapHints(self) -> bool:
        return False

    def prompt(self) -> str:
        if self.activeModel() is None:
            return "请先在浮雕模式选中浮雕模型"
        if self._phase == "region":
            return "点击闭合曲线或色块选定源区域"
        if self._phase == "target":
            return "点击目标闭合曲线（源将变形到该轮廓内）"
        return "预览中：调 Z 缩放，右键/回车确认（Esc 取消）"

    def name(self) -> str:
        return "RegionDeform"

    def commandTitle(self) -> str:
        return "区域变形"


# ============================================================================
# LevelCalibrateCommand：水平校准（以两点为准去除线性倾斜，斜面弄平）
# ============================================================================

class LevelCalibrateCommand(CommandInstance):
    """水平校准：区域/整个模型 × 点 2 点为准去除沿连线方向线性倾斜，使两点
    等高，作用范围内"斜回来弄平"。

    交互：选模式（区域须先点选闭合曲线/色块）→ 点击第 1 点 → 点击第 2 点 →
    预览校准结果 → 右键/回车确认，Esc 取消恢复。
    """

    _PICK_PX = 8.0

    def __init__(self) -> None:
        super().__init__()
        self._panel: LevelCalibrateParamPanel | None = None
        self._phase = "pt1"   # region / pt1 / pt2 / done
        self._mask = None      # 区域蒙版（scope=region 时）
        self._p1 = None        # (x, y)
        self._p2 = None
        self._initialSource = None
        self._lastNotify = 0.0

    def start(self) -> None:
        super().start()
        self._mask = None
        self._p1 = None
        self._p2 = None
        self._initialSource = None
        clr = getattr(self._view, "clearSnapMarker", None)
        if callable(clr):
            clr()
        self._phase = "region" if self._scope() == "region" else "pt1"
        self._setStatus()

    def paramPanel(self):
        if self._panel is None:
            self._panel = LevelCalibrateParamPanel()
            self._panel.finished.connect(self.finish)
            self._panel.scopeChanged.connect(self._onScopeChanged)
        return self._panel

    def activeModel(self):
        return self._doc.activeReliefModel() if self._doc is not None else None

    def _notify(self, text: str) -> None:
        if self._manager is not None:
            self._manager.notifyResult(text)

    def _scope(self) -> str:
        return self._panel.scope() if self._panel else "region"

    def _onScopeChanged(self) -> None:
        self._mask = None
        self._p1 = None
        self._p2 = None
        self._initialSource = None
        if self._previewItem is not None:
            self._previewItem.setPath(QPainterPath())
        self._phase = "region" if self._scope() == "region" else "pt1"
        self._setStatus()

    def _setStatus(self) -> None:
        if self._panel is None:
            return
        if self._phase == "region":
            self._panel.setStatus("点击闭合曲线或色块选定区域")
        elif self._phase == "pt1":
            self._panel.setStatus("点击第 1 个点")
        elif self._phase == "pt2":
            self._panel.setStatus("点击第 2 个点（连线方向即校准方向）")
        else:
            self._panel.setStatus("已校准：右键/回车确认，Esc 取消")

    # ---------------- 区域拾取 ----------------

    def _pixelTolWorld(self) -> float:
        try:
            s = abs(self._view.transform().m11())
            if s > 1e-9:
                return self._PICK_PX / s
        except Exception:
            pass
        return 1.0

    def _pickClosedCurve(self, wp):
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

    def _selectRegion(self, model, wp) -> bool:
        hf = model.heightField()
        curve = self._pickClosedCurve(wp)
        mask = None
        if curve is not None:
            pts = sample_curve_points(curve, 256)
            grid = (hf.origin[0], hf.origin[1], hf.spacing, hf.nx, hf.ny)
            mask = build_inside_mask(pts, grid)
            if not mask.any():
                mask = None
        if mask is None:
            cm = model.colorMask()
            if cm is not None and cm.shape == (hf.ny, hf.nx):
                col, row = hf.world_to_grid(wp.x, wp.y)
                ri, ci = int(round(row)), int(round(col))
                if 0 <= ri < hf.ny and 0 <= ci < hf.nx:
                    val = int(cm[ri, ci])
                    if val > 0:
                        mask = (cm == val)
        if mask is None or not mask.any():
            return False
        self._mask = mask
        return True

    # ---------------- 刷新 ----------------

    def _touchModel(self, model, force: bool = False) -> None:
        now = time.monotonic()
        if force or (now - self._lastNotify) >= 0.033:
            model.bumpVersion()
            self._doc.notifyObjectChanged(model.id())
            self._lastNotify = now

    def _drawMaskOutline(self, mask: np.ndarray) -> None:
        if mask is None or self._previewItem is None:
            return
        path = QPainterPath()
        m = mask
        ny, nx = m.shape
        hf = self.activeModel().heightField()
        ox, oy = hf.origin
        sp = hf.spacing
        left_edge = m & ~np.pad(m[:, :-1], ((0, 0), (1, 0)), constant_values=False)
        right_edge = m & ~np.pad(m[:, 1:], ((0, 0), (0, 1)), constant_values=False)
        top_edge = m & ~np.pad(m[:-1, :], ((1, 0), (0, 0)), constant_values=False)
        bot_edge = m & ~np.pad(m[1:, :], ((0, 1), (0, 0)), constant_values=False)
        rl, cl = np.nonzero(left_edge)
        rr, cr = np.nonzero(right_edge)
        rt, ct = np.nonzero(top_edge)
        rb, cb = np.nonzero(bot_edge)
        for r, c in zip(rl, cl):
            x = ox + c * sp
            path.moveTo(x, oy + r * sp); path.lineTo(x, oy + (r + 1) * sp)
        for r, c in zip(rr, cr):
            x = ox + (c + 1) * sp
            path.moveTo(x, oy + r * sp); path.lineTo(x, oy + (r + 1) * sp)
        for r, c in zip(rt, ct):
            y = oy + r * sp
            path.moveTo(ox + c * sp, y); path.lineTo(ox + (c + 1) * sp, y)
        for r, c in zip(rb, cb):
            y = oy + (r + 1) * sp
            path.moveTo(ox + c * sp, y); path.lineTo(ox + (c + 1) * sp, y)
        self._setPreviewPath(path, solid=False)
        if self._manager is not None:
            self._manager.invalidatePreview()

    def _drawMarkers(self) -> None:
        """画已采集点十字标记 + 连线（虚线）。"""
        if self._previewItem is None:
            return
        path = QPainterPath()
        r = 2.0
        if self._p1 is not None:
            x, y = self._p1
            path.moveTo(x - r, y); path.lineTo(x + r, y)
            path.moveTo(x, y - r); path.lineTo(x, y + r)
        if self._p2 is not None:
            x, y = self._p2
            path.moveTo(x - r, y); path.lineTo(x + r, y)
            path.moveTo(x, y - r); path.lineTo(x, y + r)
        if self._p1 is not None and self._p2 is not None:
            path.moveTo(self._p1[0], self._p1[1])
            path.lineTo(self._p2[0], self._p2[1])
        self._setPreviewPath(path, solid=False)
        if self._manager is not None:
            self._manager.invalidatePreview()

    def _inBounds(self, hf, x, y) -> bool:
        col, row = hf.world_to_grid(x, y)
        return 0.0 <= col <= hf.nx - 1 and 0.0 <= row <= hf.ny - 1

    # ---------------- 鼠标 ----------------

    def onMousePress(self, wp, mods) -> None:
        model = self.activeModel()
        if model is None:
            self._notify("请先在浮雕模式选中浮雕模型")
            return
        if self._doc is None:
            return
        # 先 bake 再取场/选区：水平校准直接写母场，hf 引用必须是烘焙后的
        # 新场对象（bake 后再写旧引用=校准结果写进孤儿数组，静默丢失）
        if model.hasPendingTransform():
            model.bake()
        hf = model.heightField()
        if self._phase == "region":
            if self._selectRegion(model, wp):
                self._phase = "pt1"
                self._drawMaskOutline(self._mask)
                self._setStatus()
            else:
                self._notify("未识别到闭合曲线或色块，请点击曲线或已填色区域")
            return
        if not self._inBounds(hf, wp.x, wp.y):
            self._notify("点在模型范围外，请在模型内点击")
            return
        if self._phase == "pt1":
            self._p1 = (wp.x, wp.y)
            self._phase = "pt2"
            self._drawMarkers()
            self._setStatus()
            return
        if self._phase == "pt2":
            self._initialSource = hf.data.copy()
            self._p2 = (wp.x, wp.y)
            mask = self._mask if self._scope() == "region" else None
            if level_calibrate(hf, self._p1[0], self._p1[1],
                               self._p2[0], self._p2[1], mask=mask):
                self._phase = "done"
                self._drawMarkers()
                self._touchModel(model, force=True)
                self._setStatus()
            else:
                self._notify("两点重合，请点不同的第 2 个点")
                self._p2 = None
                self._initialSource = None

    def onMouseMove(self, wp, mods) -> None:
        return

    def onMouseRelease(self, mods) -> None:
        return

    def onRightClick(self, wp) -> bool:
        if self._phase == "done":
            self._commit()
            self.finish()
            return True
        return False

    def onEnter(self) -> bool:
        if self._phase == "done":
            self._commit()
            self.finish()
            return True
        return False

    def _commit(self) -> None:
        if self._doc is not None:
            model = self.activeModel()
            if model is not None:
                self._touchModel(model, force=True)

    def cancel(self) -> None:
        if self._doc is not None and self._initialSource is not None:
            model = self.activeModel()
            if model is not None:
                model.heightField().setData(self._initialSource)
                model.bumpVersion()
                self._doc.notifyObjectChanged(model.id())
        super().cancel()

    def finish(self) -> None:
        self._commit()
        super().finish()

    def showSnapHints(self) -> bool:
        return False

    def prompt(self) -> str:
        if self.activeModel() is None:
            return "请先在浮雕模式选中浮雕模型"
        if self._phase == "region":
            return "点击闭合曲线或色块选定区域"
        if self._phase == "pt1":
            return "点击第 1 个点"
        if self._phase == "pt2":
            return "点击第 2 个点（连线方向即校准方向）"
        return "已校准：右键/回车确认，Esc 取消"

    def name(self) -> str:
        return "LevelCalibrate"

    def commandTitle(self) -> str:
        return "水平校准"


# ============================================================================
# RegionSlopeCommand：斜面（区域内以曲线为最高线向边界衰减减去高度）
# ============================================================================

class RegionSlopeCommand(CommandInstance):
    """斜面：选区域 → 点选曲线（刀刃/最高线，可闭合/开放）→ 曲线把区域分两侧，
    点击选要做斜面的一侧 → 该侧内曲线处最高、区域边界最低，按 profile 衰减减去高度。

    交互：点击区域 → 点击曲线 → 点击选侧 → 预览（调高度/类型实时）→ 右键/回车确认，Esc 取消。
    """

    _PICK_PX = 8.0

    def __init__(self) -> None:
        super().__init__()
        self._panel: RegionSlopeParamPanel | None = None
        self._phase = "region"   # region / curve / side / param
        self._mask = None        # 区域蒙版
        self._curvePts = None    # 曲线世界采样点 (K,2)
        self._sides = []         # 曲线分出的各侧 mask 列表
        self._sideMask = None    # 选定做斜面的一侧
        self._initialSource = None
        self._lastNotify = 0.0

    def start(self) -> None:
        super().start()
        self._phase = "region"
        self._mask = None
        self._curvePts = None
        self._sides = []
        self._sideMask = None
        self._initialSource = None
        clr = getattr(self._view, "clearSnapMarker", None)
        if callable(clr):
            clr()
        self._setStatus()

    def paramPanel(self):
        if self._panel is None:
            self._panel = RegionSlopeParamPanel()
            self._panel.finished.connect(self.finish)
            self._panel.paramsChanged.connect(self._onParamsChanged)
        return self._panel

    def activeModel(self):
        return self._doc.activeReliefModel() if self._doc is not None else None

    def _notify(self, text: str) -> None:
        if self._manager is not None:
            self._manager.notifyResult(text)

    def _setStatus(self) -> None:
        if self._panel is None:
            return
        if self._phase == "region":
            self._panel.setStatus("点击闭合曲线或色块选定区域")
        elif self._phase == "curve":
            self._panel.setStatus("点击曲线（刀刃/最高线，可闭合/开放）")
        elif self._phase == "side":
            self._panel.setStatus("点击选择要做斜面的一侧")
        else:
            self._panel.setStatus("预览中：调高度/类型，右键/回车确认（Esc 取消）")

    # ---------------- 拾取 ----------------

    def _pixelTolWorld(self) -> float:
        try:
            s = abs(self._view.transform().m11())
            if s > 1e-9:
                return self._PICK_PX / s
        except Exception:
            pass
        return 1.0

    def _pickClosedCurve(self, wp):
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

    def _pickAnyCurve(self, wp):
        """点选任意曲线（不要求闭合），用于最高线拾取。"""
        if self._doc is None:
            return None
        tol = self._pixelTolWorld()
        best, best_d = None, tol
        for o in self._doc.visibleObjects():
            if o.type() == ObjectType.SURFACE or o.isLocked():
                continue
            pts = sample_curve_points(o, 128)
            if pts.shape[0] < 2:
                continue
            d = _pointPolylineDistance(wp.x, wp.y, pts)
            if d < best_d:
                best, best_d = o, d
        return best

    def _selectRegion(self, model, wp) -> bool:
        hf = model.heightField()
        curve = self._pickClosedCurve(wp)
        mask = None
        if curve is not None:
            pts = sample_curve_points(curve, 256)
            grid = (hf.origin[0], hf.origin[1], hf.spacing, hf.nx, hf.ny)
            mask = build_inside_mask(pts, grid)
            if not mask.any():
                mask = None
        if mask is None:
            cm = model.colorMask()
            if cm is not None and cm.shape == (hf.ny, hf.nx):
                col, row = hf.world_to_grid(wp.x, wp.y)
                ri, ci = int(round(row)), int(round(col))
                if 0 <= ri < hf.ny and 0 <= ci < hf.nx:
                    val = int(cm[ri, ci])
                    if val > 0:
                        mask = (cm == val)
        if mask is None or not mask.any():
            return False
        self._mask = mask
        return True

    # ---------------- 刷新 ----------------

    def _touchModel(self, model, force: bool = False) -> None:
        now = time.monotonic()
        if force or (now - self._lastNotify) >= 0.033:
            model.bumpVersion()
            self._doc.notifyObjectChanged(model.id())
            self._lastNotify = now

    def _drawMaskOutline(self, mask: np.ndarray) -> None:
        if mask is None or self._previewItem is None:
            return
        path = QPainterPath()
        m = mask
        ny, nx = m.shape
        hf = self.activeModel().heightField()
        ox, oy = hf.origin
        sp = hf.spacing
        left_edge = m & ~np.pad(m[:, :-1], ((0, 0), (1, 0)), constant_values=False)
        right_edge = m & ~np.pad(m[:, 1:], ((0, 0), (0, 1)), constant_values=False)
        top_edge = m & ~np.pad(m[:-1, :], ((1, 0), (0, 0)), constant_values=False)
        bot_edge = m & ~np.pad(m[1:, :], ((0, 1), (0, 0)), constant_values=False)
        rl, cl = np.nonzero(left_edge)
        rr, cr = np.nonzero(right_edge)
        rt, ct = np.nonzero(top_edge)
        rb, cb = np.nonzero(bot_edge)
        for r, c in zip(rl, cl):
            x = ox + c * sp
            path.moveTo(x, oy + r * sp); path.lineTo(x, oy + (r + 1) * sp)
        for r, c in zip(rr, cr):
            x = ox + (c + 1) * sp
            path.moveTo(x, oy + r * sp); path.lineTo(x, oy + (r + 1) * sp)
        for r, c in zip(rt, ct):
            y = oy + r * sp
            path.moveTo(ox + c * sp, y); path.lineTo(ox + (c + 1) * sp, y)
        for r, c in zip(rb, cb):
            y = oy + (r + 1) * sp
            path.moveTo(ox + c * sp, y); path.lineTo(ox + (c + 1) * sp, y)
        self._setPreviewPath(path, solid=False)
        if self._manager is not None:
            self._manager.invalidatePreview()

    # ---------------- 鼠标 ----------------

    def onMousePress(self, wp, mods) -> None:
        model = self.activeModel()
        if model is None:
            self._notify("请先在浮雕模式选中浮雕模型")
            return
        if self._doc is None:
            return
        if self._phase == "region":
            # 先 bake 再选区：坡度计算作用于选区蒙版，蒙版必须基于新栅格
            if model.hasPendingTransform():
                model.bake()
            if self._selectRegion(model, wp):
                self._initialSource = model.heightField().data.copy()
                self._phase = "curve"
                self._drawMaskOutline(self._mask)
                self._setStatus()
            else:
                self._notify("未识别到闭合曲线或色块，请点击曲线或已填色区域")
            return
        if self._phase == "curve":
            curve = self._pickAnyCurve(wp)
            if curve is None:
                self._notify("未识别到曲线，请点击曲线")
                return
            pts = sample_curve_points(curve, 256)
            if pts.shape[0] < 2:
                self._notify("曲线采样点不足")
                return
            self._curvePts = pts
            model = self.activeModel()
            if model is None:
                return
            hf = model.heightField()
            sides = split_sides(hf, self._mask, pts)
            if len(sides) <= 1:
                self._sideMask = None
                self._phase = "param"
                self._applyPreview()
                self._setStatus()
            else:
                self._sides = sides
                self._sideMask = None
                self._phase = "side"
                self._setStatus()
            return
        if self._phase == "side":
            model = self.activeModel()
            if model is None:
                return
            hf = model.heightField()
            col, row = hf.world_to_grid(wp.x, wp.y)
            ri, ci = int(round(row)), int(round(col))
            for s in self._sides:
                if 0 <= ri < hf.ny and 0 <= ci < hf.nx and s[ri, ci]:
                    self._sideMask = s
                    self._phase = "param"
                    self._applyPreview()
                    self._setStatus()
                    return
            self._notify("未落在任何一侧，请点击区域内曲线某一侧")
            return

    def _height(self) -> float:
        return self._panel.height() if self._panel is not None else 2.0

    def _profileType(self) -> str:
        return self._panel.profileType() if self._panel is not None else "linear"

    def _applyPreview(self) -> None:
        """从初始快照恢复并写入斜面（减去高度）。"""
        model = self.activeModel()
        if model is None or self._initialSource is None or self._curvePts is None:
            return
        hf = model.heightField()
        hf.data[:] = self._initialSource
        params = self._panel.profileParams() if self._panel is not None else {}
        region_slope(hf, self._mask, self._curvePts,
                     self._height(), self._profileType(),
                     side_mask=self._sideMask,
                     profile_params=params)
        self._touchModel(model, force=True)

    def _onParamsChanged(self) -> None:
        if self._phase == "param" and self._initialSource is not None:
            self._applyPreview()

    def onMouseMove(self, wp, mods) -> None:
        if self._phase == "side":
            model = self.activeModel()
            if model is None:
                return
            hf = model.heightField()
            col, row = hf.world_to_grid(wp.x, wp.y)
            ri, ci = int(round(row)), int(round(col))
            hover = None
            for s in self._sides:
                if 0 <= ri < hf.ny and 0 <= ci < hf.nx and s[ri, ci]:
                    hover = s
                    break
            if hover is not None:
                self._drawMaskOutline(hover)
        return

    def onMouseRelease(self, mods) -> None:
        return

    def onRightClick(self, wp) -> bool:
        if self._phase == "param":
            self._commit()
            self.finish()
            return True
        return False

    def onEnter(self) -> bool:
        if self._phase == "param":
            self._commit()
            self.finish()
            return True
        return False

    def _commit(self) -> None:
        if self._doc is not None:
            model = self.activeModel()
            if model is not None:
                self._touchModel(model, force=True)

    def cancel(self) -> None:
        if self._doc is not None and self._initialSource is not None:
            model = self.activeModel()
            if model is not None:
                model.heightField().setData(self._initialSource)
                model.bumpVersion()
                self._doc.notifyObjectChanged(model.id())
        super().cancel()

    def finish(self) -> None:
        self._commit()
        super().finish()

    def showSnapHints(self) -> bool:
        return False

    def prompt(self) -> str:
        if self.activeModel() is None:
            return "请先在浮雕模式选中浮雕模型"
        if self._phase == "region":
            return "点击闭合曲线或色块选定区域"
        if self._phase == "curve":
            return "点击曲线（刀刃/最高线，可闭合/开放）"
        if self._phase == "side":
            return "点击选择要做斜面的一侧"
        return "预览中：调高度/类型，右键/回车确认（Esc 取消）"

    def name(self) -> str:
        return "RegionSlope"

    def commandTitle(self) -> str:
        return "斜面"


class RevolveSlopeCommand(CommandInstance):
    """旋转面：选轮廓线 → 选旋转轴（两点）→ 轮廓线绕轴旋转出高度场。

    交互：点击曲线 → 点击轴端点1 → 点击轴端点2 → 预览（调参实时）→ 右键/回车确认。
    """

    _PICK_PX = 8.0

    def __init__(self) -> None:
        super().__init__()
        self._panel: RevolveSlopeParamPanel | None = None
        self._phase = "curve"
        self._curvePts = None
        self._axisP1 = None
        self._axisP2 = None
        self._initialSource = None
        self._lastNotify = 0.0

    def start(self) -> None:
        super().start()
        self._phase = "curve"
        self._curvePts = None
        self._axisP1 = None
        self._axisP2 = None
        self._initialSource = None
        clr = getattr(self._view, "clearSnapMarker", None)
        if callable(clr):
            clr()
        self._setStatus()

    def paramPanel(self):
        if self._panel is None:
            self._panel = RevolveSlopeParamPanel()
            self._panel.finished.connect(self.finish)
            self._panel.paramsChanged.connect(self._onParamsChanged)
        return self._panel

    def activeModel(self):
        return self._doc.activeReliefModel() if self._doc is not None else None

    def _notify(self, text: str) -> None:
        if self._manager is not None:
            self._manager.notifyResult(text)

    def _setStatus(self) -> None:
        if self._panel is None:
            return
        if self._phase == "curve":
            self._panel.setStatus("点击选取轮廓线（半边轮廓）")
        elif self._phase == "axis1":
            self._panel.setStatus("点击旋转轴第一个端点")
        elif self._phase == "axis2":
            self._panel.setStatus("点击旋转轴第二个端点")
        else:
            self._panel.setStatus("预览中：调参数，右键/回车确认（Esc 取消）")

    def _pixelTolWorld(self) -> float:
        try:
            s = abs(self._view.transform().m11())
            if s > 1e-9:
                return self._PICK_PX / s
        except Exception:
            pass
        return 1.0

    def _pickAnyCurve(self, wp):
        if self._doc is None:
            return None
        tol = self._pixelTolWorld()
        best, best_d = None, tol
        for o in self._doc.visibleObjects():
            if o.type() == ObjectType.SURFACE or o.isLocked():
                continue
            pts = sample_curve_points(o, 128)
            if pts.shape[0] < 2:
                continue
            d = _pointPolylineDistance(wp.x, wp.y, pts)
            if d < best_d:
                best, best_d = o, d
        return best

    def _touchModel(self, model, force: bool = False) -> None:
        now = time.monotonic()
        if force or (now - self._lastNotify) >= 0.033:
            model.bumpVersion()
            self._doc.notifyObjectChanged(model.id())
            self._lastNotify = now

    def onMousePress(self, wp, mods) -> None:
        model = self.activeModel()
        if model is None:
            self._notify("请先在浮雕模式选中浮雕模型")
            return
        if self._doc is None:
            return
        if self._phase == "curve":
            curve = self._pickAnyCurve(wp)
            if curve is None:
                self._notify("未识别到曲线，请点击曲线")
                return
            pts = sample_curve_points(curve, 256)
            if pts.shape[0] < 2:
                self._notify("曲线采样点不足")
                return
            self._curvePts = pts
            if model.hasPendingTransform():
                model.bake()
            self._initialSource = model.heightField().data.copy()
            self._phase = "axis1"
            self._setStatus()
            return
        if self._phase == "axis1":
            self._axisP1 = (float(wp.x), float(wp.y))
            self._phase = "axis2"
            self._setStatus()
            return
        if self._phase == "axis2":
            self._axisP2 = (float(wp.x), float(wp.y))
            dx = self._axisP2[0] - self._axisP1[0]
            dy = self._axisP2[1] - self._axisP1[1]
            if dx * dx + dy * dy < 1e-12:
                self._notify("轴两端点太近，请重新点击第二个端点")
                return
            self._phase = "param"
            self._applyPreview()
            self._setStatus()
            return

    def _applyPreview(self) -> None:
        model = self.activeModel()
        if model is None or self._initialSource is None or self._curvePts is None:
            return
        if self._axisP1 is None or self._axisP2 is None:
            return
        hf = model.heightField()
        hf.data[:] = self._initialSource
        scale = self._panel.heightScale() if self._panel is not None else 1.0
        mode = self._panel.mode() if self._panel is not None else "replace"
        revolve_slope(hf, self._curvePts, self._axisP1, self._axisP2,
                      height_scale=scale, mode=mode)
        self._touchModel(model, force=True)

    def _onParamsChanged(self) -> None:
        if self._phase == "param" and self._initialSource is not None:
            self._applyPreview()

    def onMouseMove(self, wp, mods) -> None:
        if self._phase == "axis2" and self._axisP1 is not None:
            path = QPainterPath()
            path.moveTo(self._axisP1[0], self._axisP1[1])
            path.lineTo(wp.x, wp.y)
            self._setPreviewPath(path, solid=False)
            if self._manager is not None:
                self._manager.invalidatePreview()
        return

    def onMouseRelease(self, mods) -> None:
        return

    def onRightClick(self, wp) -> bool:
        if self._phase == "param":
            self._commit()
            self.finish()
            return True
        return False

    def onEnter(self) -> bool:
        if self._phase == "param":
            self._commit()
            self.finish()
            return True
        return False

    def _commit(self) -> None:
        if self._doc is not None:
            model = self.activeModel()
            if model is not None:
                self._touchModel(model, force=True)

    def cancel(self) -> None:
        if self._doc is not None and self._initialSource is not None:
            model = self.activeModel()
            if model is not None:
                model.heightField().setData(self._initialSource)
                model.bumpVersion()
                self._doc.notifyObjectChanged(model.id())
        super().cancel()

    def finish(self) -> None:
        self._commit()
        super().finish()

    def showSnapHints(self) -> bool:
        return self._phase in ("axis1", "axis2")

    def prompt(self) -> str:
        if self.activeModel() is None:
            return "请先在浮雕模式选中浮雕模型"
        if self._phase == "curve":
            return "点击选取轮廓线（半边轮廓）"
        if self._phase == "axis1":
            return "点击旋转轴第一个端点"
        if self._phase == "axis2":
            return "点击旋转轴第二个端点"
        return "预览中：调参数，右键/回车确认（Esc 取消）"

    def name(self) -> str:
        return "RevolveSlope"

    def commandTitle(self) -> str:
        return "旋转面"


# ==================== ModelScale ====================

class ModelScaleCommand(CommandInstance):
    """模型扩缩：把模型0平面上下左右扩大或缩小。

    交互：选中浮雕模型 → 面板输入上下左右扩缩值（正=扩大，负=收缩）→ 应用。
    算法：扩缩时保持原高度场内容不变，边界外扩区域填充0高度，内缩区域裁剪。
    """

    def __init__(self) -> None:
        super().__init__()
        self._panel: ModelScaleParamPanel | None = None

    def onMousePress(self, wp, mods) -> None: pass
    def onMouseMove(self, wp, mods) -> None: pass

    def showSnapHints(self) -> bool:
        """面板输入型命令，无需精确吸附，关闭视口吸附气泡。"""
        return False

    def paramPanel(self):
        if self._panel is None:
            self._panel = ModelScaleParamPanel()
            self._panel.finished.connect(self._apply)
        return self._panel

    def start(self) -> None:
        super().start()
        if self._doc is None:
            return
        if self._getTarget() is None:
            self._notify("请先选中一个浮雕模型，或在浮雕模式下编辑")
            return
        self._refreshPanel()

    def _refreshPanel(self) -> None:
        if self._panel is None:
            return
        # 面板默认显示当前模型边界信息
        obj = self._getTarget()
        if obj is not None and obj.type() == ObjectType.SURFACE:
            hf = obj.heightField()
            if hf is not None:
                x0, y0, x1, y1 = hf.bbox()
                self._panel.setStatus(f"当前: {x0:.1f},{y0:.1f} → {x1:.1f},{y1:.1f}")

    def _getTarget(self):
        """获取目标浮雕模型：浮雕编辑态（当前编辑）的模型优先，否则首个选中的。"""
        if self._doc is None:
            return None
        return self._doc.activeReliefModel()

    def _apply(self) -> None:
        obj = self._getTarget()
        if obj is None:
            self._notify("请先选中一个浮雕模型，或在浮雕模式下编辑")
            return
        hf = obj.heightField()
        if hf is None:
            return
        top, bottom, left, right = self._panel.scaleValues()
        if all(abs(v) < 1e-9 for v in (top, bottom, left, right)):
            self.finish()
            return
        # 模型若带未烘焙放置变换，先烘焙对齐（与消除锯齿等命令一致）
        if obj.hasPendingTransform():
            obj.bake()
            hf = obj.heightField()
        # 计算新边界
        x0, y0, x1, y1 = hf.bbox()
        nx, ny = hf.nx, hf.ny
        sp = hf.spacing
        # 扩缩后的世界边界
        new_x0 = x0 - left
        new_y0 = y0 - bottom
        new_x1 = x1 + right
        new_y1 = y1 + top
        # 新栅格尺寸
        new_nx = max(2, int(math.ceil((new_x1 - new_x0) / sp)) + 1)
        new_ny = max(2, int(math.ceil((new_y1 - new_y0) / sp)) + 1)
        # 创建新数据数组，填充0
        new_data = np.zeros((new_ny, new_nx), dtype=np.float32)
        # 计算原数据在新数组中的偏移
        ox = int(round((x0 - new_x0) / sp))
        oy = int(round((y0 - new_y0) / sp))
        # 复制原数据到新位置（统一偏移粘贴：外扩补 0、内缩裁剪）
        _paste_aligned(new_data, hf.data, ox, oy)
        # 更新高度场
        new_hf = HeightField(new_data, (new_x0, new_y0), sp)
        obj.setHeightField(new_hf)
        if self._doc is not None:
            self._doc.notifyObjectChanged(obj.id())
        self.finish()

    def _notify(self, text: str) -> None:
        if self._manager is not None:
            self._manager.notifyResult(text)

    def prompt(self) -> str:
        return "设置上下左右扩缩值后点“应用”"

    def name(self) -> str:
        return "ModelScale"

    def commandTitle(self) -> str:
        return "模型扩缩"


def registerAll(manager) -> None:
    manager.register("NewModel", NewModelCommand)
    manager.register("ReconstructRelief", ReconstructRelief)
    manager.register("RegionRelief", RegionReliefCommand)
    manager.register("DeJitter", DeJitterCommand)
    manager.register("Stamp", StampCommand)
    manager.register("SweepMaterial", SweepMaterialCommand)
    manager.register("AddMaterial", AddMaterialCommand)
    manager.register("Move", MoveHeightCommand)
    manager.register("Smooth", SmoothCommand)
    manager.register("SweepSmooth", SweepSmoothCommand)
    manager.register("RegionTransform", RegionTransformCommand)
    manager.register("RegionArray", RegionArrayCommand)
    manager.register("RegionSymmetry", RegionSymmetryCommand)
    manager.register("RegionDeform", RegionDeformCommand)
    manager.register("LevelCalibrate", LevelCalibrateCommand)
    manager.register("RegionSlope", RegionSlopeCommand)
    manager.register("RevolveSlope", RevolveSlopeCommand)
    manager.register("ImageToModel", ImageToModelCommand)
    manager.register("ModelScale", ModelScaleCommand)
    manager.register("TextureFill", TextureFillCommand)
    manager.register("SlopeZone", SlopeZoneCommand)
    manager.register("RestBoundary", RestBoundaryCommand)


# ============================================================================
# ImageToModelCommand：图片（已导入文档） → 浮雕模型（两阶段）
# ============================================================================

class ImageToModelCommand(CommandInstance):
    """图片转浮雕模型（两阶段：处理图片 + 生成模型）。

    阶段一 "处理图片"：
        使用"图像处理参数"(超分倍率 / 磨光强度 / 边缘阈值 / 细节增强)
        在后台对当前选中 ImageObject 灰度图跑全套处理，并把输出的 uint8
        "高度映射灰度"写回原图对象（覆盖图像数据）。
        图片的世界尺寸不变 (width/height/center 不变)；像素元数据更新为
        新分辨率，保证后续生成模型时像素间距能再次自动换算。
        这一步是文档对象的可撤销修改。

    阶段二 "生成模型"：
        从当前（已处理好的）ImageObject 灰度图按 "3D 参数"(浮雕深度 /
        网格采样 / 像素间距) 生成 HeightField，然后加入文档。
        模型中心与图片中心自动重合，尺寸一致。
        这一步是新增文档对象的可撤销修改。
    """

    def __init__(self) -> None:
        super().__init__()
        self._panel = None
        self._worker = None
        # 取消/结束标志：_abortWorker 置 True，各 worker 槽入口据此丢弃
        # 已排队送达的过期结果（launch 时复位）
        self._aborted = False
        # 画布交互属性占位（本命令实际不使用画布拾取流程，但 onMouseMove
        # 会引用 _source 做早退判断，缺失会抛 AttributeError）
        self._source = None

    # ---------------- 面板 ----------------
    def paramPanel(self):
        if self._panel is None:
            from ui.commands.param_panels import ImageToModelParamPanel
            self._panel = ImageToModelParamPanel()
            self._panel.pickSelectedRequested.connect(self._autoBindFromSelected)
            self._panel.processImageRequested.connect(self._onProcessImage)
            self._panel.generateModelRequested.connect(self._onGenerateModel)
        return self._panel

    # ---------------- 生命周期 ----------------
    def start(self) -> None:
        super().start()
        self._autoBindFromSelected()

    def cancel(self) -> None:
        self._abortWorker()
        super().cancel()

    def finish(self) -> None:
        self._abortWorker()
        super().finish()

    def _abortWorker(self) -> None:
        w = getattr(self, "_worker", None)
        if w is None:
            return
        self._aborted = True
        # 断开信号：取消/结束后 worker 已排队的 success/failed 不再送达
        # 本命令的槽（否则过期结果会覆盖原图像对象/重挂面板）
        for sig in ("success", "failed", "progress"):
            try:
                getattr(w, sig).disconnect()
            except Exception:
                pass
        try:
            w.requestInterruption()
        except Exception:
            pass
        if w.isRunning():
            try:
                # 只优雅等待：不再 terminate 强杀——worker 持有 numpy/cv2
                # 内存，Windows 上 TerminateThread 可能在线程写内存/持堆锁
                # 中途被杀（堆损坏）；进度回调已支持协作取消，让它自然退出
                w.wait(1500)
            except Exception:
                pass
        self._worker = None

    # ---------------- 绑定选中的 ImageObject ----------------
    def _autoBindFromSelected(self) -> None:
        if self._panel is None or self._doc is None:
            return
        from core.vector.image_object import ImageObject
        first = None
        # 先从选中对象集合 (修正：Document.selectedObjects() 而非 selectedIds())
        for o in self._doc.selectedObjects():
            if isinstance(o, ImageObject):
                first = o
                break
        if first is None:
            for o in self._doc.objects():
                if isinstance(o, ImageObject):
                    first = o
                    break
        self._panel.attachImageObject(first if first is not None else None)

    # ---------------- 阶段一：处理图片（覆盖原图） ----------------
    def _onProcessImage(self) -> None:
        if self._panel is None or self._worker is not None:
            return
        img_obj = self._panel.currentImageObject()
        if img_obj is None:
            self._notify("当前没有可用的图片对象，请先在画布中选中一张图片")
            return
        data = img_obj.imageData()
        if not data:
            self._notify("图片对象数据为空")
            return
        img_params = self._panel.imageProcessParams()
        # 保留原始世界几何 (center/width/height/rotation) 不变，仅图像像素会变
        geo = (
            float(img_obj.center().x), float(img_obj.center().y), float(img_obj.center().z),
            float(img_obj.width()), float(img_obj.height()),
            float(img_obj.rotation()),
            img_obj.id(),
        )
        self._runStageA(data, img_params, geo)

    def _clearWorkerRef(self, which_thread) -> None:
        """finished 统一清理入口：当且仅当 self._worker == 这条线程时才清空引用，允许下一条任务立即启动。"""
        cur = getattr(self, "_worker", None)
        if cur is which_thread:
            self._worker = None

    def _onWorkerProgress(self, pct: int, msg: str) -> None:
        try:
            if not self._aborted and self._panel is not None:
                self._panel.setProgress(int(pct), str(msg))
        except Exception:
            import traceback
            traceback.print_exc()

    def _runStageA(self, data: bytes, img_params, geo):
        from PySide6.QtCore import QThread, Signal as QtSignal

        class _StageAThread(QThread):
            progress = QtSignal(int, str)
            success = QtSignal(object, tuple)   # (processed_gray_uint8, geo)
            failed = QtSignal(str)

            def __init__(self, img_bytes, prm, geo_, parent=None):
                super().__init__(parent)
                self._img_bytes = bytes(img_bytes)
                self._params = prm
                self._geo = geo_
                self._interrupted = False

            def requestInterruption(self) -> None:
                self._interrupted = True

            def run(self) -> None:
                try:
                    import numpy as np
                    from core.relief.image_to_model import (
                        decode_image_bytes, process_image_only,
                    )
                    def _cb(pct: int, msg: str) -> None:
                        if self._interrupted:
                            raise RuntimeError("已取消")
                        self.progress.emit(int(pct), str(msg))

                    self.progress.emit(5, "解码图片")
                    gray = decode_image_bytes(self._img_bytes)
                    self.progress.emit(10, "图像算法处理")
                    out = process_image_only(gray, self._params, progress_cb=_cb)
                    # numpy.copy 到主线程独立内存空间，避免子线程对象析构后跨线程访问非法内存
                    self.success.emit(np.ascontiguousarray(out, dtype=np.uint8).copy(), self._geo)
                except Exception as e:
                    import traceback
                    tb = traceback.format_exc()
                    print(tb, flush=True)
                    self.failed.emit(f"{e}")

        thread = _StageAThread(data, img_params, geo, parent=self._panel)
        thread.progress.connect(self._onWorkerProgress)
        thread.success.connect(self._onStageADone)
        thread.failed.connect(self._onWorkerFailed)
        thread.finished.connect(lambda: self._clearWorkerRef(thread))

        self._aborted = False
        self._worker = thread
        self._panel.setEnabledButtons(False)
        self._panel.setProgress(0, "图像处理启动中...")
        thread.start()

    def _onStageADone(self, new_gray, geo) -> None:
        if self._aborted or self._panel is None:
            return   # 命令已取消/结束：丢弃过期结果
        self._panel.setEnabledButtons(True)
        if self._doc is None:
            self._panel.setProgress(0, "失败：无文档")
            return
        cx, cy, cz, obj_w, obj_h, rot, oid = geo
        from core.vector.image_object import ImageObject
        import cv2
        import numpy as np

        try:
            # uint8 灰度 → PNG bytes (无损, 压缩 fast)
            g = np.asarray(new_gray, dtype=np.uint8)
            ok, buf = cv2.imencode(".png", g, [cv2.IMWRITE_PNG_COMPRESSION, 1])
            if not ok:
                raise RuntimeError("PNG 编码失败")
            new_bytes = buf.tobytes()
            obj = self._doc.object(oid) if oid else None
            if not isinstance(obj, ImageObject):
                raise RuntimeError(f"图片对象不存在 id={oid}")

            # 文档的 objectChanged 通常包装在 beginModify/endModify 里
            # 这里直接 setImageData → 手动 emit，保证 2D 视图刷新
            obj.setImageData(new_bytes)
            # 重新保持原放置几何 (setImageData 不改变 width/height/center/rotation)
            # 但我们希望图片的"世界尺寸"不因为像素变多而改变：ImageObject 的
            # width/height 本来就是独立的几何属性，setImageData 不修改它们 →
            # 已保持不变。再显式 setGeometry 确认一下：
            from core.vector.geometry_types import Point3D
            obj.setGeometry(Point3D(cx, cy, cz), obj_w, obj_h, rot)

            # 触发文档对象变更信号 → 2D 视图重绘图片
            try:
                self._doc.objectChanged.emit(oid)
            except Exception:
                pass

            # 把处理后的对象重新 attach 到面板 → 刷新像素信息与像素间距
            self._panel.attachImageObject(obj)

            h, w = g.shape[:2]
            self._notify(f"图像处理完成：{w}×{h} 像素，已写回原图对象（可撤销）")
            self._panel.setProgress(100, "处理完成并写回原图")
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._notify(f"写回图片对象失败：{e}")
            self._panel.setProgress(0, "失败")

    # ---------------- 阶段二：生成模型（从已处理好的图片） ----------------
    def _onGenerateModel(self) -> None:
        if self._panel is None or self._worker is not None:
            return
        img_obj = self._panel.currentImageObject()
        if img_obj is None:
            self._notify("当前没有可用的图片对象，请先在画布中选中一张图片（必要时点“处理图片”再生成）")
            return
        data = img_obj.imageData()
        if not data:
            self._notify("图片对象数据为空，无法生成模型")
            return

        img_p = self._panel.imageProcessParams()      # 只需要 scale (超分倍率)
        hf_p = self._panel.heightFieldParams()       # 深度/网格/像素间距/...
        fit_size = bool(self._panel.fitSizeChecked())
        # 重要：面板 pixelSizeMm 是"原图"的像素间距 (由图片对象自动换算)，
        # 阶段 B 的最终栅格间距 = 原 spacing × scale / gs
        gs = max(1, int(hf_p.gridSampling))
        s = max(1, int(img_p.scale))
        final_spacing = float(hf_p.pixelSizeMm) * s / gs
        # 覆盖到新的 HeightFieldParams 对象 (不修改 hf_p 本身以免影响后续)
        from core.relief.image_to_model import HeightFieldParams
        final_hf_p = HeightFieldParams(
            depthMm=float(hf_p.depthMm),
            pixelSizeMm=float(final_spacing),   # 最终栅格 spacing (若勾选"尺寸匹配原图"，done 后再按对象几何重算)
            baseHeight=float(hf_p.baseHeight),
            blackThreshold=int(hf_p.blackThreshold),
            autoCenter=False,                   # 命令层平移
            gridSampling=int(hf_p.gridSampling),
        )
        # 图片几何：用于后续平移对齐；fit_size=True 时 spacing 会在 done 回调中重算
        cx = float(img_obj.center().x)
        cy = float(img_obj.center().y)
        obj_w = float(img_obj.width())
        obj_h = float(img_obj.height())

        self._runStageB(data, final_hf_p, (cx, cy, obj_w, obj_h, s, gs, bool(fit_size)))

    def _runStageB(self, data: bytes, hf_p, geo):
        from PySide6.QtCore import QThread, Signal as QtSignal

        class _StageBThread(QThread):
            progress = QtSignal(int, str)
            success = QtSignal(object, tuple)   # (HeightField, geo)
            failed = QtSignal(str)

            def __init__(self, img_bytes, prm, geo_, parent=None):
                super().__init__(parent)
                self._img_bytes = bytes(img_bytes)
                self._hf_params = prm
                self._geo = geo_
                self._interrupted = False

            def requestInterruption(self) -> None:
                self._interrupted = True

            def run(self) -> None:
                try:
                    import numpy as np
                    from core.relief.image_to_model import (
                        decode_image_bytes, heightfield_from_processed)

                    def _cb(pct, msg):
                        if self._interrupted:
                            raise RuntimeError("已取消")
                        self.progress.emit(int(pct), str(msg))

                    self.progress.emit(5, "解码图片")
                    gray = decode_image_bytes(self._img_bytes)
                    # 关键：对齐 HeightField 行方向（row=minY=世界下方）与
                    # ImageObject 2D 显示（图片顶部像素 = 世界上方 = maxY）
                    # —— 翻转为：灰度首行 row=0 = 图片顶部 → 送入 HeightField 时
                    #           要对应 HeightField 的 maxY = 最后一行 row=H-1。
                    gray_aligned = np.ascontiguousarray(gray[::-1, :], dtype=np.uint8).copy()
                    self.progress.emit(10, "生成 HeightField")
                    hf = heightfield_from_processed(gray_aligned, self._hf_params, progress_cb=_cb)
                    self.success.emit(hf, self._geo)
                except Exception as e:
                    import traceback
                    print(traceback.format_exc(), flush=True)
                    self.failed.emit(f"{e}")

        thread = _StageBThread(data, hf_p, geo, parent=self._panel)
        thread.progress.connect(self._onWorkerProgress)
        thread.success.connect(self._onStageBDone)
        thread.failed.connect(self._onWorkerFailed)
        thread.finished.connect(lambda: self._clearWorkerRef(thread))

        self._aborted = False
        self._worker = thread
        self._panel.setEnabledButtons(False)
        self._panel.setProgress(0, "生成模型启动中...")
        thread.start()

    def _onStageBDone(self, hf, geo) -> None:
        if self._aborted or self._panel is None:
            return   # 命令已取消/结束：丢弃过期结果
        try:
            if self._panel is not None:
                self._panel.setEnabledButtons(True)
            if self._doc is None:
                if self._panel is not None:
                    self._panel.setProgress(0, "失败：无文档")
                return
            # geo = (cx, cy, obj_w, obj_h, s, gs, fit_size)
            cx, cy, obj_w, obj_h, s, gs, fit_size = (
                float(geo[0]), float(geo[1]), float(geo[2]), float(geo[3]),
                int(geo[4]), int(geo[5]), bool(geo[6]))
            nx, ny = hf.nx, hf.ny
            sp = float(hf.spacing)

            if fit_size:
                # 强制整体物理尺寸 = 图片放置尺寸 mm：
                # HeightField 只支持单 spacing，不能分别设置 X/Y 方向间距。
                # 这里以"宽度方向严格对齐"作为基准 (X 是主维度，最直观)：
                #   new_sp = obj_w / (nx - 1)
                # 因为 HeightField 栅格行/列比已经等于图片像素 W:H，而图片像素比例与
                # 画布放置尺寸 obj_w:obj_h 在 attachImageObject 时已被 pixelSize 近似匹配，
                # 所以高方向 (ny-1)*new_sp ≈ obj_h 误差通常 < 0.1 mm (1px 级)，视觉完全一致。
                ns = float(obj_w) / max(int(nx) - 1, 1)
                if ns <= 0:
                    ns = float(obj_h) / max(int(ny) - 1, 1)
                if ns > 0:
                    hf.setSpacing(float(ns))
                    sp = ns

            # 中心对齐图片 (cx, cy)：HF 世界宽 = (nx-1)*sp，高 = (ny-1)*sp
            wmm = (int(nx) - 1) * sp
            hmm = (int(ny) - 1) * sp
            ox = cx - wmm * 0.5
            oy = cy - hmm * 0.5
            hf.setOrigin((float(ox), float(oy)))

            from core.vector.relief_model_object import ReliefModelObject
            obj = ReliefModelObject.fromHeightField(hf)
            self._doc.addObject(obj)
            self._doc.selectObject(obj.id())
            hmax = float(hf.data.max())
            note = "（尺寸已与原图对齐）" if fit_size else "（未强制对齐尺寸）"
            self._notify(
                f"生成模型完成：{nx}×{ny} 栅格，尺寸 {wmm:.2f}×{hmm:.2f} mm，"
                f"最高 {hmax:.2f} mm {note}（可撤销）")
            if self._panel is not None:
                self._panel.setProgress(100, "已生成模型")
            # 生成模型后不自动结束命令（用户可能继续生成不同参数的多个版本）
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                if self._panel is not None:
                    self._panel.setEnabledButtons(True)
                    self._panel.setProgress(0, "失败")
            except Exception:
                pass
            self._notify(f"生成模型失败：{e}")

    def _onWorkerFailed(self, msg: str) -> None:
        if self._aborted or self._panel is None:
            return
        self._panel.setEnabledButtons(True)
        self._panel.setProgress(0, "失败")
        self._notify(f"图片转模型失败：{msg}")

    # ---------------- 画布事件（右键/回车退出） ----------------
    def onMousePress(self, wp, mods) -> None: pass
    def onMouseMove(self, wp, mods) -> None:
        if self._panel is None or self._source is None:
            return
        d = self._panel.direction()
        if self._phase == "pt2" and self._pt1 is not None:
            if d == "free":
                self._axisX = self._pt1[0]
                self._axisY = self._pt1[1]
                self._pt2X = wp.x
                self._pt2Y = wp.y
                self._hasAxis = True
                self._keepSide = "positive"
                self._refreshAxisLine()
                self._applyPreview()
            else:
                self._axisX = (self._pt1[0] + wp.x) * 0.5
                self._axisY = (self._pt1[1] + wp.y) * 0.5
                self._pt2X = wp.x
                self._pt2Y = wp.y
                self._hasAxis = True
                self._refreshAxisLine()
                self._applyPreview()
        elif self._phase == "click" and d == "free" and self._hasAxis:
            dx = self._pt2X - self._axisX
            dy = self._pt2Y - self._axisY
            L = float(np.hypot(dx, dy))
            if L > 1e-9:
                nx_n = -dy / L
                ny_n = dx / L
                side = (wp.x - self._axisX) * nx_n + (wp.y - self._axisY) * ny_n
                self._keepSide = "positive" if side >= 0 else "negative"
                self._applyPreview()
        elif self._phase == "click" and d != "free" and self._hasAxis:
            if d == "left_right":
                self._keepSide = "left" if wp.x < self._axisX else "right"
            else:
                self._keepSide = "top" if wp.y < self._axisY else "bottom"
            self._applyPreview()
    def onRightClick(self, wp) -> bool:
        self.finish()
        return True
    def onEnter(self) -> bool:
        self.finish()
        return True

    # ---------------- 杂项 ----------------
    def _notify(self, text: str) -> None:
        if self._manager is not None:
            self._manager.notifyResult(text)

    def prompt(self) -> str:
        if self._panel is None:
            return "启动图转模型命令"
        try:
            if self._panel.currentImageObject() is None:
                return "请先在画布中选中一张已导入的图片，或点面板“取当前选中图片”"
        except Exception:
            return "图转模型命令（初始化中）…"
        return "1) 调图像处理参数→点“处理图片”覆盖原图  2) 调3D参数→点“生成模型”（右键退出）"

    def name(self) -> str:
        return "ImageToModel"

    def commandTitle(self) -> str:
        return "图片转模型"



# ============================================================================
# TextureFillCommand：几何纹理 → 高度场（复刻精雕 SurfMill"几何纹理"族）
# ============================================================================

class TextureFillCommand(CommandInstance):
    """纹理填充：程序化几何纹理叠加到活动浮雕模型。

    复刻精雕 SurfMill 帮助"几何纹理"章节：随机分形 / 腐蚀 / 编织带 /
    缠绕结 / 填充图案（六边/砖形/菱形/圆环/点阵）/ 矩形网格 / 表面扰动。
    交互与区域塑形一致：点击闭合曲线 = 仅该曲线区域内填充；面板
    "应用到整个模型" = 整模型；每次应用独立 beginEdit/endEdit（可 Ctrl+Z）。
    """

    _PICK_PX = 8.0

    def __init__(self) -> None:
        super().__init__()
        self._panel: TextureFillParamPanel | None = None

    def name(self) -> str:
        return "TextureFill"

    def commandTitle(self) -> str:
        return "纹理填充"

    def start(self) -> None:
        super().start()
        clr = getattr(self._view, "clearSnapMarker", None)
        if callable(clr):
            clr()

    def paramPanel(self):
        if self._panel is None:
            self._panel = TextureFillParamPanel()
            self._panel.applyWhole.connect(self._applyWhole)
            self._panel.finished.connect(self.finish)
        return self._panel

    def _notify(self, text: str) -> None:
        if self._manager is not None:
            try:
                self._manager.notifyResult(str(text))
            except Exception:
                pass

    def _apply(self, poly=None) -> None:
        model = self._doc.activeReliefModel() if self._doc is not None else None
        if model is None:
            self._notify("请先选中浮雕模型（纹理填充作用于活动模型）")
            return
        if self._panel is None or self._doc is None:
            return
        params = self._panel.params()
        if (params.kind == "weave" and params.weave_mode == "curve"
                and poly is None):
            # 沿曲线编织且未点击指定路径：回退到当前选中的曲线
            sel = self._selectedCurve()
            if sel is None:
                self._notify("沿曲线编织需要一条曲线：点击画布上的曲线，"
                             "或先选中一条曲线再点“应用到整个模型”")
                return
            poly = sample_curve_points(sel, 512)
        doc = self._doc
        doc.beginEdit()
        try:
            if model.hasPendingTransform():
                model.bake()
            hf = model.heightField()
            n = apply_texture(hf, params,
                              color_mask=model.colorMask(),
                              color_value=PaintState.instance().currentValue(),
                              poly=poly)
            if n > 0:
                model.bumpVersion()
                try:
                    self._doc.notifyObjectChanged(model.id())
                except Exception:
                    pass
                self._notify(f"纹理已填充：{n} 个顶点"
                             f"（{KIND_LABELS.get(params.kind, params.kind)}）")
            else:
                self._notify("未命中区域：确认曲线落在模型范围内"
                             "且颜色范围设置允许")
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._notify(f"纹理填充失败：{e}")
        finally:
            try:
                doc.endEdit()
            except Exception:
                pass

    def _applyWhole(self) -> None:
        self._apply(None)

    def _selectedCurve(self):
        """当前选中的曲线（沿曲线编织的路径来源：优先面板点击，其次已选曲线）。"""
        if self._doc is None:
            return None
        for o in self._doc.selectedObjects():
            if o.type() == ObjectType.SURFACE or o.isLocked():
                continue
            if hasattr(o, "getSampledPoints") or hasattr(o, "toPolycurve"):
                return o
        return None

    def onMouseMove(self, wp, mods) -> None:
        pass

    def onMousePress(self, wp, mods) -> None:
        tol = self._pixelTolWorld()
        if self._panel is not None and self._panel.weaveMode() == "curve":
            # 沿曲线编织：开放曲线即可作为编织路径
            obj = pick_curve(self._doc, wp.x, wp.y, tol)
            if obj is None:
                self._notify("未命中曲线：把鼠标移到曲线上再点击（编织路径）")
                return
            self._apply(sample_curve_points(obj, 512))
            return
        obj = pick_closed_curve(self._doc, wp.x, wp.y, tol)
        if obj is None:
            self._notify("未命中闭合曲线：把鼠标移动到曲线上再点击"
                         "（8 像素容差），或用面板“应用到整个模型”")
            return
        self._apply(sample_curve_points(obj, 256))

    def onRightClick(self, wp) -> bool:
        self.finish()
        return True

    def onEnter(self, wp) -> bool:
        return self.onRightClick(wp)

    def showSnapHints(self) -> bool:
        return False

    def _pixelTolWorld(self) -> float:
        try:
            s = abs(self._view.transform().m11())
            if s > 1e-9:
                return self._PICK_PX / s
        except Exception:
            pass
        return 1.0

    def prompt(self) -> str:
        if (self._doc is None
                or self._doc.activeReliefModel() is None):
            return "请先在 2D 选中浮雕模型（纹理填充作用于活动模型）"
        return "点击闭合曲线即在该区域内填充纹理；面板可应用到整个模型（右键/回车结束）"


# ============================================================================
# SlopeZoneCommand：角度分区线（复刻精雕"角度分区线"）
# ============================================================================

class SlopeZoneCommand(CommandInstance):
    """角度分区线：按坡度把模型分区并生成分界闭合曲线。

    复刻精雕 SurfMill"残料边界线和角度分区线"之角度分区：默认
    10°/30°/60° 分界（平坦/缓坡/陡坡/陡立），每区一条曲线对象、
    按区着色（绿→橙→红→紫），供区域塑形 / CNC 分区加工取用。
    """

    def __init__(self) -> None:
        super().__init__()
        self._panel: SlopeZoneParamPanel | None = None

    def name(self) -> str:
        return "SlopeZone"

    def commandTitle(self) -> str:
        return "角度分区线"

    def paramPanel(self):
        if self._panel is None:
            self._panel = SlopeZoneParamPanel()
            self._panel.generate.connect(self._generate)
            self._panel.finished.connect(self.finish)
        return self._panel

    def _notify(self, text: str) -> None:
        if self._manager is not None:
            try:
                self._manager.notifyResult(str(text))
            except Exception:
                pass

    def _generate(self) -> None:
        model = self._doc.activeReliefModel() if self._doc is not None else None
        if model is None:
            self._notify("请先选中浮雕模型（角度分区作用于活动模型）")
            return
        if self._panel is None or self._doc is None:
            return
        doc = self._doc
        doc.beginEdit()
        try:
            if model.hasPendingTransform():
                model.bake()
            hf = model.heightField()
            bounds = self._panel.bounds()
            tol = self._panel.tolerance()
            zones = slope_zone_masks(hf, bounds)
            made = 0
            for mask, nm, col in zip(zones, ZONE_NAMES, ZONE_COLORS):
                if not mask.any():
                    continue
                loops = mask_contours(mask, hf, simplify_mm=tol)
                if not loops:
                    continue
                obj = PolyCurveObject()
                obj._segments = [
                    {"points": [[float(x), float(y), 0.0] for x, y in lp]}
                    for lp in loops]
                obj.setColor(col)
                doc.addObject(obj)
                made += 1
            if made:
                self._notify(f"已生成 {made} 条角度分区线"
                             f"（分界 {bounds[0]:.0f}/{bounds[1]:.0f}/"
                             f"{bounds[2]:.0f}°）")
            else:
                self._notify("未生成分区线：模型太平（无坡度变化）")
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._notify(f"角度分区失败：{e}")
        finally:
            try:
                doc.endEdit()
            except Exception:
                pass

    def onMouseMove(self, wp, mods) -> None:
        pass

    def onMousePress(self, wp, mods) -> None:
        pass

    def onRightClick(self, wp) -> bool:
        self.finish()
        return True

    def onEnter(self, wp) -> bool:
        return self.onRightClick(wp)

    def showSnapHints(self) -> bool:
        return False

    def prompt(self) -> str:
        if (self._doc is None
                or self._doc.activeReliefModel() is None):
            return "请先在 2D 选中浮雕模型（角度分区作用于活动模型）"
        return "在面板设置分界角度后点击“生成分区线”（右键/回车结束命令）"


# ============================================================================
# RestBoundaryCommand：残料边界线（复刻精雕"残料边界线"）
# ============================================================================

class RestBoundaryCommand(CommandInstance):
    """残料边界线：按刀具半径算"刀具够不到"的残留区并生成边界闭合曲线。

    复刻精雕 SurfMill"残料边界线和角度分区线"之残料边界：球刀走精确
    两遍扫描包络（静球搁放 + 球底最深走刀），平底刀走圆盘 closing
    近似；窄沟/尖角/小 R 内拐角均识别为残料。生成的曲线可直接用于
    区域塑形或 CNC 残料补加工选区。
    """

    def __init__(self) -> None:
        super().__init__()
        self._panel: RestBoundaryParamPanel | None = None

    def name(self) -> str:
        return "RestBoundary"

    def commandTitle(self) -> str:
        return "残料边界线"

    def onMouseMove(self, wp, mods) -> None:
        pass

    def onMousePress(self, wp, mods) -> None:
        pass

    def paramPanel(self):
        if self._panel is None:
            self._panel = RestBoundaryParamPanel()
            self._panel.generate.connect(self._generate)
            self._panel.finished.connect(self.finish)
        return self._panel

    def _notify(self, text: str) -> None:
        if self._manager is not None:
            try:
                self._manager.notifyResult(str(text))
            except Exception:
                pass

    def _generate(self) -> None:
        model = self._doc.activeReliefModel() if self._doc is not None else None
        if model is None:
            self._notify("请先选中浮雕模型（残料边界作用于活动模型）")
            return
        if self._panel is None or self._doc is None:
            return
        doc = self._doc
        doc.beginEdit()
        try:
            if model.hasPendingTransform():
                model.bake()
            hf = model.heightField()
            mask = rest_material_mask(hf, self._panel.toolRadius(),
                                      tool=self._panel.toolShape(),
                                      threshold_mm=self._panel.threshold())
            loops = mask_contours(mask, hf,
                                  simplify_mm=self._panel.tolerance())
            if loops:
                obj = PolyCurveObject()
                obj._segments = [
                    {"points": [[float(x), float(y), 0.0] for x, y in lp]}
                    for lp in loops]
                obj.setColor((230, 80, 40))
                doc.addObject(obj)
                self._notify(f"已生成残料边界：{len(loops)} 条闭合曲线"
                             f"（刀具半径 {self._panel.toolRadius():.2f}mm，"
                             f"{'球刀' if self._panel.toolShape() == 'ball' else '平底刀'}）")
            else:
                self._notify("无残料：该刀具半径下模型全部可加工")
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._notify(f"残料边界失败：{e}")
        finally:
            try:
                doc.endEdit()
            except Exception:
                pass

    def onRightClick(self, wp) -> bool:
        self.finish()
        return True

    def onEnter(self, wp) -> bool:
        return self.onRightClick(wp)

    def showSnapHints(self) -> bool:
        return False

    def prompt(self) -> str:
        if (self._doc is None
                or self._doc.activeReliefModel() is None):
            return "请先在 2D 选中浮雕模型（残料边界作用于活动模型）"
        return "在面板设置刀具半径后点击“生成残料边界”（右键/回车结束命令）"


# ---- 模块级工具：闭合曲线拾取（纹理填充与区域塑形共用） ----

def pick_curve(doc, x: float, y: float, tol_world: float):
    """拾取距世界点 (x,y) 最近的**任意**曲线（开放/闭合均可，编织路径用）。"""
    if doc is None:
        return None
    best, best_d = None, tol_world
    for o in doc.visibleObjects():
        if o.type() == ObjectType.SURFACE or o.isLocked():
            continue
        pts = sample_curve_points(o, 128)
        if pts.shape[0] < 2:
            continue
        d = _pointPolylineDistance(x, y, pts)
        if d < best_d:
            best, best_d = o, d
    return best


def pick_closed_curve(doc, x: float, y: float, tol_world: float):
    """拾取距世界点 (x,y) 最近的**闭合**曲线（容差 tol_world）。

    闭合判定：对象显式 isClosed()，或采样首尾距离远小于周长。
    """
    if doc is None:
        return None
    best, best_d = None, tol_world
    for o in doc.visibleObjects():
        if o.type() == ObjectType.SURFACE or o.isLocked():
            continue
        pts = sample_curve_points(o, 128)
        if pts.shape[0] < 3:
            continue
        closed = getattr(o, "isClosed", None)
        if callable(closed):
            if not closed():
                continue
        else:
            d_end = float(np.linalg.norm(pts[0] - pts[-1]))
            perim = float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum())
            if perim > 1e-9 and d_end > 1e-3 * perim:
                continue
        d = _pointPolylineDistance(x, y, pts)
        if d < best_d:
            best, best_d = o, d
    return best

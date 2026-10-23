# -*- coding: utf-8 -*-
"""饼干外切模命令（私有插件）：选闭合曲线 → 调参预览 → 确认生成切模实体。

继承挤出命令的拾取/分组机制；预览与确认构建全部在后台线程执行
（同布尔命令的合并式后台模式：只保留最新请求、忙时不开新线程、
结果经 Signal 回主线程），主线程零重计算、零卡顿：
- 预览：fast 档（少点数/粗抽稀/粗网格/跳过校验），网格在后台预热；
- 确认：完整质量构建 + 网格/拓扑预热都在后台，回主线程只做
  addObject/选中（无冻结）；
- 过期结果按 key 丢弃（拖参数时只生效最后一次）。
嵌套分组只取每组外轮廓（饼干孔不参与切模），独立轮廓各自生成一个。
"""
from __future__ import annotations

import math
import threading
from typing import List, Optional

from PySide6.QtCore import QObject, Signal as _Signal

from core.vector.brep_object import BRepObject
from core.vector.brep_shape import BrepShape
from ui.commands.concrete.solid_commands import (
    ExtrudeCommand, _profileIsHorizontal)

from cutter_builder import CookieCutterParams, build_cookie_cutter_part


class _BgDoneSignal(QObject):
    done = _Signal(object)


class CookieCutterCommand(ExtrudeCommand):
    """饼干切模：复用挤出命令的拾取/分组/撤销，构建走后台线程。"""

    # 采样上限：样条按 ~0.3mm 弦距密集重采样后可到数千点（上限保护）
    _MAX_PROFILE_PTS = 4096

    # 源折线平滑度：样条曲线密集重采样的目标弦距（mm）。默认采样每段
    # 固定 ~20 点，大曲线弦距可达数 mm，壁面跟随折线出现肉眼可见的
    # 棱面（用户实测反馈）；按弦距自适应加密后与 2D 显示一致光滑
    _TARGET_SPACING = 0.3

    @classmethod
    def _sampleOutline(cls, obj) -> List:
        """样条曲线密集重采样：控制点出发按 0.3mm 弦距重建采样
        （与画布同一条插值曲线，仅密度提高）；圆/椭圆解析加密；
        其余类型走基类。"""
        pts = super()._sampleOutline(obj)
        try:
            from core.vector.geometry_types import ObjectType
            # 圆/椭圆：解析加密到 512 点（基类固定 96 点，r=20 圆弦差
            # 10µm、棱角 3.75°，渲染有明显棱面）
            if obj.type() in (ObjectType.CIRCLE, ObjectType.ELLIPSE):
                dense = cls._denseConic(obj)
                if dense is not None and len(dense) >= 3:
                    return dense
                return pts
            if obj.type() not in (ObjectType.CURVE, ObjectType.NURBS_CURVE):
                return pts
            ctrl = obj.inputPoints() if callable(
                getattr(obj, "inputPoints", None)) else None
            if not ctrl or len(ctrl) < 3 or len(pts) < 3:
                return pts
            closed = bool(obj.isClosed()) if callable(
                getattr(obj, "isClosed", None)) else False
            if closed and ctrl[0].distanceTo(ctrl[-1]) > 1e-9:
                ctrl = list(ctrl) + [ctrl[0]]
            seg_max = max(ctrl[i].distanceTo(ctrl[i + 1])
                          for i in range(len(ctrl) - 1))
            k = max(20, min(500, int(math.ceil(seg_max / cls._TARGET_SPACING))))
            from core.spline_utils import catmullRom
            from core.vector.curve_object import CurveMode
            if getattr(obj, "curveMode", None) == CurveMode.CONTROL:
                from core.spline_utils import createControlPointBSpline
                dense = createControlPointBSpline(
                    ctrl, int(getattr(obj, "curveDegree", 3) or 3),
                    samples=k)
            else:
                dense = catmullRom(ctrl, k)
            if len(dense) >= 3:
                dense = [p for i, p in enumerate(dense)
                         if i == 0 or dense[i - 1].distanceTo(p) > 1e-9]
                if dense[0].distanceTo(dense[-1]) < 1e-9:
                    dense = dense[:-1]
                dense = cls._capPoints(dense, cls._MAX_PROFILE_PTS)
                if len(dense) >= 3:
                    return dense
        except Exception:
            pass
        return pts

    # 圆锥曲线采样目标弦差（mm）：自适应点数使弦差 ≤ 此值（肉眼看
    # 不出棱的量级），点数随半径/曲率增长，不固定
    _CONIC_SAG = 0.002

    @classmethod
    def _denseConic(cls, obj):
        """圆/椭圆 → 曲率自适应解析采样（弦差 ≤ _CONIC_SAG，含 Z）。

        N = π/acos(1 − s/R)：圆 R=半径；椭圆取最小曲率半径 b²/a
        （长轴端）。r=20 → 199 点，r=100 → 445 点，小圆更少。"""
        from core.vector.geometry_types import ObjectType, Point3D
        t = obj.type()
        c = obj.center()
        z = c.z
        s = cls._CONIC_SAG
        if t == ObjectType.CIRCLE:
            r_min = max(obj.radius(), 1e-6)
        elif t == ObjectType.ELLIPSE:
            a_ax, b_ax = obj.majorAxis(), obj.minorAxis()
            if a_ax < b_ax:
                a_ax, b_ax = b_ax, a_ax
            r_min = max(b_ax * b_ax / max(a_ax, 1e-9), 1e-6)
        else:
            return None
        n = int(math.ceil(math.pi / math.acos(max(-1.0,
              min(1.0, 1.0 - s / r_min)))))
        n = max(64, min(4096, n))
        out = []
        if t == ObjectType.CIRCLE:
            r = obj.radius()
            out = [Point3D(c.x + r * math.cos(a * 2 * math.pi / n),
                           c.y + r * math.sin(a * 2 * math.pi / n), z)
                   for a in range(n)]
        else:
            a_ax, b_ax = obj.majorAxis(), obj.minorAxis()
            cr, sr = math.cos(obj.rotation()), math.sin(obj.rotation())
            for i in range(n):
                th = i * 2 * math.pi / n
                ex, ey = a_ax * math.cos(th), b_ax * math.sin(th)
                out.append(Point3D(c.x + ex * cr - ey * sr,
                                   c.y + ex * sr + ey * cr, z))
        return out

    def __init__(self) -> None:
        super().__init__()
        self._bgSig = _BgDoneSignal()
        self._bgSig.done.connect(self._onBgDone)
        self._bgLock = threading.Lock()
        self._bgPending: Optional[tuple] = None   # (mode, key, jobs)
        self._bgBusy = False
        self._bgKey: Optional[tuple] = None       # 最新请求键（过期判定）
        # 预览体对应的请求键：确认时比对，一致则直接采用预览结果（零等待）
        self._previewObjKey: Optional[tuple] = None
        # 预览网格细分容差（BrepShape 轻量档）：0.12mm 档拐角/圆弧
        # 渲染平滑，三角量仍远低于默认 0.08 档的两倍以内
        self._previewMeshTol = 0.08

    # ---------------- 预览（点选后后台构建，悬停不预览） ----------------

    def onMouseMove(self, wp, mods) -> None:
        """拾取态悬停不预览（用户要求：点选后才预览）；确认态无操作。"""
        pass

    def _currentIdent(self) -> tuple:
        """当前曲线组 + 参数的稳定标识（曲线对象 id + 参数值）。"""
        groups = self._activeGroups()
        params = self._panel.params() if self._panel is not None \
            else CookieCutterParams()
        return (tuple(id(g[0]) for g in groups),
                tuple(sorted(vars(params).items())))

    def _collectJobs(self, groups) -> Optional[list]:
        """采样各组外轮廓 → 后台构建任务；无效返回 None。"""
        params = self._panel.params() if self._panel is not None \
            else CookieCutterParams()
        jobs = []
        for outer, _holes in groups:
            profile = self._sampleOutline(outer)
            if len(profile) < 3 or not _profileIsHorizontal(profile):
                continue
            jobs.append((profile, self._profileKind(outer), params))
        return jobs or None

    def _updatePreview(self) -> None:
        """调度后台快速预览（主线程立即返回；只保留最新请求）。"""
        groups = self._activeGroups()
        if not groups:
            return
        jobs = self._collectJobs(groups)
        if jobs is None:
            self._notify("切模失败：曲线轮廓需为水平闭合曲线且点数充足")
            return
        key = ("preview",) + self._currentIdent()
        self._bgKey = key
        with self._bgLock:
            self._bgPending = ("preview", key, jobs)
        if not self._bgBusy:
            self._bgBusy = True
            threading.Thread(target=self._bgLoop, daemon=True).start()

    # ---------------- 确认（预览即结果，零等待） ----------------

    def _confirmExtrude(self) -> None:
        """确认生成：预览体与当前曲线/参数一致 → 直接采用（网格已预热，
        零计算零卡顿）；预览未就绪或多组 → 后台完整质量构建后应用。"""
        groups = self._activeGroups()
        if not groups:
            self._notify("未选中闭合曲线")
            return
        ident = self._currentIdent()
        if (len(groups) == 1 and self._previewObj is not None
                and self._previewObjKey == ("preview",) + ident):
            self._extrudeBodies([self._previewObj])
            self._resetAfterConfirm()
            return
        jobs = self._collectJobs(groups)
        if jobs is None:
            self._notify("切模失败：曲线轮廓需为水平闭合曲线且点数充足")
            return
        key = ("confirm",) + ident + (id(groups),)
        self._bgKey = key
        with self._bgLock:
            self._bgPending = ("confirm", key, jobs)
        if not self._bgBusy:
            self._bgBusy = True
            threading.Thread(target=self._bgLoop, daemon=True).start()
        self._notify(f"正在生成 {len(jobs)} 个切模…")

    # ---------------- 后台构建循环（同布尔命令模式） ----------------

    def _bgLoop(self) -> None:
        while True:
            with self._bgLock:
                job = self._bgPending
                self._bgPending = None
            if job is None:
                self._bgBusy = False
                return
            mode, key, jobs = job
            try:
                if mode == "preview":
                    result = self._buildPreviewShape(jobs)
                else:
                    result = self._buildFinalObjects(jobs)
                try:
                    self._bgSig.done.emit((mode, key, result, None))
                except RuntimeError:
                    return   # 命令已销毁
            except Exception as e:
                try:
                    self._bgSig.done.emit((mode, key, None, str(e)))
                except RuntimeError:
                    return

    def _buildPreviewShape(self, jobs):
        """fast 档构建 + 粗网格预热（纯 core 计算，任意线程安全）。"""
        parts = [build_cookie_cutter_part(p, k, prm, fast=True)
                 for p, k, prm in jobs]
        if len(parts) == 1:
            part = parts[0]
        else:
            from core.vector.brep_shape import _import_bd
            part = _import_bd().Compound(parts)   # 多组预览：仅合成不布尔
        bs = BrepShape.fromPart(part)
        bs.setMeshQuality(tol=self._previewMeshTol)
        bs.triangulate()   # 后台预热网格缓存（预览渲染零等待）
        return bs

    def _buildFinalObjects(self, jobs) -> List[BRepObject]:
        """完整质量构建 + 精网格/拓扑预热（确认后渲染/拾取零首帧卡顿）。"""
        out = []
        for p, k, prm in jobs:
            part = build_cookie_cutter_part(p, k, prm, fast=False)
            brep = BRepObject()
            brep.setShape(BrepShape.fromPart(part))
            try:
                brep.mesh()
                brep.topology()
            except Exception:
                pass
            out.append(brep)
        return out

    def _onBgDone(self, payload) -> None:
        mode, key, result, err = payload
        if key != self._bgKey or not self.isAlive():
            return   # 过期结果（参数已变/已退出）
        if err is not None:
            if mode == "confirm":
                self._resetAfterConfirm()
            self._notify(f"切模失败：{err}")
            return
        if mode == "preview":
            if self._pickMode and self._previewCurve is None:
                return   # 已回到拾取态
            prev = BRepObject()
            prev.setShape(result)
            self._previewObj = prev
            self._previewObjKey = key
        else:
            self._extrudeBodies(result)
            self._resetAfterConfirm()
            return
        if self._manager is not None:
            self._manager.invalidatePreview()
        try:
            self._viewport().update()
        except Exception:
            pass

    def _resetAfterConfirm(self) -> None:
        """确认后回到拾取态（同挤出命令确认流程）。"""
        self._pickMode = True
        self._previewCurve = None
        self._previewGroups = []
        self._previewObj = None
        self._previewObjKey = None
        if self._manager is not None:
            self._manager.invalidatePreview()

    def _viewport(self):
        """活动 3D 视口（同布尔命令：3D 模式必须用 _modeView）。"""
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

    # ---------------- 应用结果（主线程，轻量） ----------------

    def _extrudeBodies(self, bodies: List[BRepObject]) -> None:
        """加入切模实体（一次 beginEdit/endEdit = 一次撤销；结果全选）。
        网格/拓扑已在后台预热，此处零重计算。"""
        if self._doc is None or not bodies:
            return
        doc = self._doc
        doc.beginEdit()
        try:
            for result in bodies:
                doc.addObject(result)
                doc.selectObject(result.id())
            p = self._panel.params() if self._panel is not None \
                else CookieCutterParams()
            self._notify(f"已生成 {len(bodies)} 个切模（壁厚 {p.wall:.2f}，"
                         f"总高 {p.height_blade:.1f}）")
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._notify(f"切模失败：{e}")
        finally:
            try:
                doc.endEdit()
            except Exception:
                pass

    # ---------------- 交互适配 ----------------

    def start(self) -> None:
        # 同挤出命令的预选便捷：多选 → 嵌套分组，每组外轮廓各生成一个切模
        super().start()
        if self._previewGroups and len(self._previewGroups) > 1:
            self._notify(f"多组切模：{len(self._previewGroups)} 条外轮廓"
                         f"各生成一个（嵌套内圈=饼干孔，不参与）")
        elif self._previewCurve is not None and self._previewHoles:
            self._notify(f"外轮廓 + {len(self._previewHoles)} 个内圈："
                         "内圈是饼干孔，切模仅按外轮廓生成")

    def paramPanel(self):
        if self._panel is None:
            self._panel = _makePanel()()
            self._panel.confirmed.connect(self._confirmExtrude)
            self._panel.finished.connect(self.finish)
            self._panel.paramsChanged.connect(self._updatePreview)
        return self._panel

    def prompt(self) -> str:
        if self._pickMode:
            return ("点击闭合曲线选中并预览（悬停不预览）；或多选曲线后启动"
                    " = 每条外轮廓各生成一个切模（内圈忽略），右键/确认生成，"
                    "Esc 结束")
        return ("调节参数后右键/确认即时生成（采用预览结果，无等待；"
                "Esc 放弃当前，再 Esc 结束命令）")

    def name(self) -> str:
        return "CookieCutter"

    def commandTitle(self) -> str:
        return "模具"


def _makePanel():
    from PySide6.QtWidgets import (QCheckBox, QGroupBox, QHBoxLayout, QLabel,
                                   QPushButton, QVBoxLayout, QWidget)
    from ui.commands.param_panels import ParamPanelBase, _LABEL_W
    from ui.styles import theme

    class CookieCutterParamPanelImpl(ParamPanelBase):
        """切模参数面板：尺寸/高度/斜面角度；改动触发后台快速预览。"""

        confirmed = _Signal()
        finished = _Signal()
        paramsChanged = _Signal()

        def __init__(self, parent=None) -> None:
            super().__init__(parent)

            gSz, lSz = self._group("尺寸（向外偏移）")
            # 缝隙标题即开关按钮：按下高亮"有缝"=启用缝隙；
            # 弹起灰色"无缝"=无缝隙(gap=0)
            self._btnGap = QPushButton("有缝")
            self._btnGap.setCheckable(True)
            self._btnGap.setChecked(True)
            self._btnGap.setToolTip(
                "按下=有缝（曲线先外扩此距离再生成）；灰色无缝=贴着曲线生成")
            self._btnGap.setMinimumWidth(_LABEL_W)
            self._styleGapBtn(True)
            self._spinGap = self._spin(0.3, decimals=2, step=0.1)
            self._spinGap.setRange(0.0, 50.0)
            rowGap = QHBoxLayout()
            rowGap.setContentsMargins(0, 0, 0, 0)
            rowGap.setSpacing(4)
            rowGap.addWidget(self._btnGap)
            rowGap.addWidget(self._spinGap, 1)
            rowWrap = QWidget()
            rowWrap.setLayout(rowGap)
            lSz.addWidget(rowWrap)
            self._spinWall = self._spin(0.6, decimals=2, step=0.1)
            self._spinWall.setRange(0.05, 100.0)
            lSz.addWidget(self._row("刀壁厚", self._spinWall))
            self._spinBand = self._spin(1.0, decimals=2, step=0.1)
            self._spinBand.setRange(0.05, 100.0)
            lSz.addWidget(self._row("加强带宽", self._spinBand))
            self._spinFlange = self._spin(3.2, decimals=2, step=0.1)
            self._spinFlange.setRange(0.05, 100.0)
            lSz.addWidget(self._row("裙边宽", self._spinFlange))
            self._layout.addWidget(gSz)

            gH, lH = self._group("高度")
            self._spinBlade = self._spin(15.0, decimals=1, step=0.5)
            self._spinBlade.setRange(0.5, 9999.0)
            lH.addWidget(self._row("总高（刀口）", self._spinBlade))
            self._spinBody = self._spin(12.0, decimals=1, step=0.5)
            self._spinBody.setRange(0.1, 9999.0)
            lH.addWidget(self._row("体高", self._spinBody))
            self._spinFlangeH = self._spin(3.6, decimals=1, step=0.2)
            self._spinFlangeH.setRange(0.05, 9999.0)
            lH.addWidget(self._row("裙边高", self._spinFlangeH))
            self._layout.addWidget(gH)

            gB, lB = self._group("顶部斜面")
            self._chkAuto = QCheckBox("自动角度（随加强带宽）")
            self._chkAuto.setChecked(True)
            lB.addWidget(self._chkAuto)
            self._spinBevel = self._spin(12.0, decimals=1, step=1.0)
            self._spinBevel.setRange(0.5, 85.0)
            self._spinBevel.setEnabled(False)
            lB.addWidget(self._row("斜面角度", self._spinBevel))
            self._spinCurve = self._spin(0.0, decimals=0, step=10.0)
            self._spinCurve.setRange(-80.0, 80.0)
            lB.addWidget(self._row("截面弯曲%", self._spinCurve))
            tip = QLabel("手动调截面：角度越小斜面越缓、顶缘越厚；"
                         "弯曲% 负=外凸、正=内凹（0=直线）")
            tip.setWordWrap(True)
            tip.setStyleSheet("color:%s; font-size:9pt;" % theme.TEXT_DIM)
            lB.addWidget(tip)
            self._layout.addWidget(gB)

            btnConfirm = self._button("确认", primary=True)
            btnConfirm.clicked.connect(self.confirmed.emit)
            self._layout.addWidget(btnConfirm)
            btnEnd = self._button("结束")
            btnEnd.clicked.connect(self.finished.emit)
            self._layout.addWidget(btnEnd)
            self._layout.addStretch(1)

            for s in (self._spinGap, self._spinWall, self._spinBand,
                      self._spinFlange, self._spinBlade, self._spinBody,
                      self._spinFlangeH, self._spinBevel,
                      self._spinCurve):
                s.valueChanged.connect(lambda *_: self._onChange())
            self._btnGap.toggled.connect(self._onGapToggled)
            self._chkAuto.toggled.connect(self._onAutoToggled)

        def _styleGapBtn(self, on: bool) -> None:
            """缝隙开关样式：按下=主题色高亮，弹起=灰色。"""
            self._btnGap.setStyleSheet(
                "QPushButton { background:%s; color:%s;"
                " border:1px solid %s; border-radius:2px; padding:3px 6px; }"
                "QPushButton:checked { background:%s; color:%s;"
                " border-color:%s; font-weight:bold; }"
                % (theme.BG_PANEL, theme.TEXT_DIM, theme.BORDER,
                   theme.PRIMARY, theme.TEXT_ON_PRIMARY, theme.PRIMARY))

        def _onGapToggled(self, on: bool) -> None:
            self._btnGap.setText("有缝" if on else "无缝")
            self._styleGapBtn(on)
            self._spinGap.setEnabled(on)
            self.paramsChanged.emit()

        def _group(self, title: str):
            g = QGroupBox(title)
            lay = QVBoxLayout(g)
            lay.setSpacing(3)
            lay.setContentsMargins(6, 4, 6, 4)
            return g, lay

        def _row(self, label: str, widget) -> QWidget:
            row = QWidget()
            lay = QHBoxLayout(row)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(4)
            lab = QLabel(label)
            lab.setMinimumWidth(_LABEL_W)
            lay.addWidget(lab)
            lay.addWidget(widget, 1)
            return row

        def _onAutoToggled(self, on: bool) -> None:
            self._spinBevel.setEnabled(not on)
            self.paramsChanged.emit()

        def _onChange(self) -> None:
            self.paramsChanged.emit()

        def params(self) -> CookieCutterParams:
            return CookieCutterParams(
                gap=self._spinGap.value() if self._btnGap.isChecked() else 0.0,
                wall=self._spinWall.value(),
                band=self._spinBand.value(),
                flange=self._spinFlange.value(),
                height_blade=self._spinBlade.value(),
                height_body=self._spinBody.value(),
                height_flange=self._spinFlangeH.value(),
                bevel_auto=self._chkAuto.isChecked(),
                bevel_angle_deg=self._spinBevel.value(),
                bevel_curve=self._spinCurve.value())

    return CookieCutterParamPanelImpl

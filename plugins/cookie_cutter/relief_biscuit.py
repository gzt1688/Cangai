# -*- coding: utf-8 -*-
"""浮雕饼干（私有插件，仅浮雕模式）。

选中/点选闭合曲线后一次性成形：
  曲线**外**：高度全部清零（冲压成 0，回到底面）；
  曲线**内**：高度场先按 Z 镜像（h → -h），再整块上抬，
  使抬升后的**最低点**落在参数指定高度（默认 +2）。

即"把凸起的饼干花纹翻成凹模、且整体悬在底面之上 2mm"的效果。
嵌套曲线按奇偶规则处理（内环=孔，不参与镜像）。
"""
from __future__ import annotations

import numpy as np
from PySide6.QtWidgets import QCheckBox, QLabel

from core.vector.geometry_types import ObjectType
from core.relief.region_relief import sample_curve_points, build_inside_mask
from ui.commands.command_instance import CommandInstance
from ui.commands.param_panels import ParamPanelBase


_CMD_NAME = "BiscuitRelief"

DEFAULT_BOTTOM = 2.0      # “最低点”默认高度（mm）


class BiscuitReliefCommand(CommandInstance):
    """浮雕饼干：曲线外清零 + 曲线内 Z 镜像后最低点抬到指定高度。"""

    _PICK_PX = 8.0

    def __init__(self) -> None:
        super().__init__()
        self._panel = None

    def name(self) -> str:
        return _CMD_NAME

    def commandTitle(self) -> str:
        return "浮雕饼干"

    def prompt(self) -> str:
        return ("点击闭合曲线执行（或先选中曲线后按回车）："
                "曲线外高度清零，曲线内 Z 镜像后最低点抬到「最低点」值；"
                "右键/Esc 结束")

    # ---------------- 面板 ----------------

    def paramPanel(self):
        if self._panel is None:
            self._panel = BiscuitReliefParamPanel()
        return self._panel

    def _bottom(self) -> float:
        try:
            return float(self._panel.bottom())
        except Exception:
            return DEFAULT_BOTTOM

    def _mirror(self) -> bool:
        try:
            return bool(self._panel.mirror())
        except Exception:
            return True

    # ---------------- 启动 ----------------

    def start(self) -> None:
        super().start()
        clr = getattr(self._view, "clearSnapMarker", None)
        if callable(clr):
            clr()

    # ---------------- 鼠标 ----------------

    def onMousePress(self, wp, mods) -> None:
        obj = self._pickClosedCurve(wp)
        if obj is None:
            self._notify("未命中闭合曲线：请点击曲线，或先选中曲线后按回车")
            return
        self._run([obj])

    def onRightClick(self, wp) -> bool:
        self.finish()
        return True

    def onEnter(self) -> bool:
        """有选中曲线时执行，否则结束命令。"""
        sel = [o for o in (self._doc.selectedObjects() if self._doc else [])
               if self._isClosedCurve(o)]
        if sel:
            self._run(sel)
        else:
            self.finish()
        return True

    def onMouseMove(self, wp, mods) -> None:
        self._hideCursorMarker()

    # ---------------- 拾取 ----------------

    @staticmethod
    def _isClosedCurve(obj) -> bool:
        """闭合曲线判定：圆/椭圆/矩形天然闭合；其余看 isClosed 标志
        或首末采样点是否重合（CircleObject 没有 isClosed 方法）。"""
        t = obj.type()
        if obj.isLocked():
            return False
        if t in (ObjectType.CIRCLE, ObjectType.ELLIPSE, ObjectType.RECTANGLE):
            return True
        if t not in (ObjectType.LINE, ObjectType.CURVE, ObjectType.POLYCURVE,
                     ObjectType.ARC, ObjectType.NURBS_CURVE):
            return False
        closed = getattr(obj, "isClosed", None)
        if callable(closed):
            try:
                if closed():
                    return True
            except Exception:
                pass
        pts = sample_curve_points(obj, 64)
        if pts.shape[0] >= 2:
            return float(np.hypot(*(pts[0] - pts[-1]))) < 1e-6
        return False

    def _tolWorld(self) -> float:
        try:
            s = abs(self._view.transform().m11())
            if s > 1e-9:
                return self._PICK_PX / s
        except Exception:
            pass
        return 1.0

    def _pickClosedCurve(self, wp):
        """按屏幕像素容差找最近的闭合曲线（采样点最近距离）。"""
        if self._doc is None or wp is None:
            return None
        tol = self._tolWorld()
        best, best_d = None, tol
        q = np.array([wp.x, wp.y], dtype=np.float64)
        for o in self._doc.visibleObjects():
            if not self._isClosedCurve(o):
                continue
            pts = sample_curve_points(o, 128)
            if pts.shape[0] < 3:
                continue
            d = float(np.min(np.linalg.norm(pts - q, axis=1)))
            if d < best_d:
                best, best_d = o, d
        return best

    # ---------------- 执行 ----------------

    def _notify(self, text: str) -> None:
        if self._manager is not None:
            self._manager.notifyResult(text)

    def _run(self, objs) -> None:
        model = self._doc.activeReliefModel() if self._doc is not None else None
        if model is None:
            self._notify("请先在浮雕模式下选中浮雕模型")
            return
        doc = self._doc
        doc.beginEdit()
        try:
            if model.hasPendingTransform():
                model.bake()
            hf = model.heightField()
            mask = self._buildMask(hf, objs)
            if not mask.any():
                doc.endEdit()
                self._notify("曲线内没有覆盖到任何高度场像素（曲线太小或不在模型上）")
                return
            bottom = self._bottom()
            mirror = self._mirror()
            hb = hf.data
            inside = hb[mask].astype(np.float64)
            if mirror:
                inside = -inside          # Z 镜像：凸起翻成凹下
            # 抬升使变换后的最低点 = 参数高度
            inside += bottom - float(inside.min())
            hb[mask] = inside.astype(hb.dtype)
            hb[~mask] = 0.0
            model.bumpVersion()
            doc.notifyObjectChanged(model.id())
            doc.endEdit()
        except Exception as e:
            doc.endEdit()
            self._notify(f"浮雕饼干失败：{e}")
            return
        h0, h1 = float(inside.min()), float(inside.max())
        shape = "Z 镜像后" if mirror else "直接"
        self._notify(f"浮雕饼干完成：外部清零，内部{shape}抬到 "
                     f"{h0:g}~{h1:g}（最低点 {h0:g}）")
        self.finish()

    def _buildMask(self, hf, objs) -> np.ndarray:
        """多条闭合曲线的内部蒙版：按奇偶规则叠加（嵌套内环=孔）。"""
        grid = (hf.origin[0], hf.origin[1], hf.spacing, hf.nx, hf.ny)
        mask = np.zeros((hf.ny, hf.nx), dtype=bool)
        for o in objs:
            pts = sample_curve_points(o, 512)
            if pts.shape[0] < 3:
                continue
            mask ^= build_inside_mask(pts, grid)
        return mask


class BiscuitReliefParamPanel(ParamPanelBase):
    """参数：最低点高度（镜像后整块上抬到的基准高度）。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        g, lay = self._group("浮雕饼干")
        self._spinBottom = self._spin(DEFAULT_BOTTOM, decimals=2, step=0.1)
        self._spinBottom.setRange(-1000.0, 1000.0)
        lay.addWidget(self._row("最低点", self._spinBottom))
        self._chkMirror = QCheckBox("Z 镜像")
        self._chkMirror.setChecked(True)
        self._chkMirror.setToolTip("勾选：曲线内高度先 Z 镜像（凸→凹）"
                                   "再整体抬高；不勾：保持原形状直接抬高")
        lay.addWidget(self._chkMirror)
        from ui.styles import theme
        tip = QLabel("曲线外高度清零；曲线内按最低点参数整体抬高"
                     "（可先 Z 镜像）")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:%s; font-size:9pt;" % theme.TEXT_DIM)
        lay.addWidget(tip)
        self._layout.addWidget(g)
        self._layout.addStretch(1)

    def bottom(self) -> float:
        return self._spinBottom.value()

    def mirror(self) -> bool:
        return self._chkMirror.isChecked()

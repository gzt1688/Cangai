# -*- coding: utf-8 -*-
"""硅胶外围（私有插件，仅浮雕模式）。

选中/点选闭合曲线后一步生成硅胶模外沿：
  0. 先把模型高度场**四边扩边**（扩边量 = 外偏距离 + 外沿宽度，默认
     5.8mm，整格向上取整、新边补 0 基准面）——新建模型余量通常只有 5mm，
     不扩边则外偏区域/围坝会落到模型外；
  1. 曲线往外偏「外偏距离」（默认 5mm）——区域用解析有符号距离场
     （compute_polygon_sd，内正外负）判定，凹形轮廓天然正确，
     无需真的生成偏移曲线；
  2. 外偏曲线**以内**（含模型）整体相对冲压抬升「相对抬升」
     （默认 1.2mm）——均匀垫底，细节不变；
  3. 再往外偏「外沿宽度」（默认 0.8mm）的环带，绝对冲压到
     当前模型最高点 + 「壁顶加高」（默认 5mm）——形成挡硅胶的围坝。

多条曲线按奇偶规则处理（嵌套内环=孔）；围坝高度取垫底**前**的
模型全局最高点。
"""
from __future__ import annotations

import numpy as np
from PySide6.QtWidgets import QLabel

from core.relief.region_relief import compute_polygon_sd, sample_curve_points
from ui.commands.param_panels import ParamPanelBase
from relief_biscuit import BiscuitReliefCommand


_CMD_NAME = "SiliconeRim"

DEFAULT_OFFSET = 5.0      # 外偏距离（mm）
DEFAULT_RISE = 1.2        # 相对抬升（mm）
DEFAULT_BAND = 0.8        # 外沿宽度（mm）
DEFAULT_WALL = 5.0        # 壁顶加高（围坝顶 = 模型最高点 + 该值）


class SiliconeRimCommand(BiscuitReliefCommand):
    """硅胶外围：外偏内垫底（相对抬升），外沿环带冲到模型最高点+加高。"""

    def name(self) -> str:
        return _CMD_NAME

    def commandTitle(self) -> str:
        return "硅胶外围"

    def prompt(self) -> str:
        return ("点击闭合曲线执行（或先选中曲线后按回车）："
                "外偏距离内整体相对抬升「相对抬升」，再往外的「外沿宽度」环带"
                "绝对冲压到模型最高点+「壁顶加高」；右键/Esc 结束")

    # ---------------- 面板 ----------------

    def paramPanel(self):
        if self._panel is None:
            self._panel = SiliconeRimParamPanel()
        return self._panel

    def _offset(self) -> float:
        try:
            return float(self._panel.offset())
        except Exception:
            return DEFAULT_OFFSET

    def _rise(self) -> float:
        try:
            return float(self._panel.rise())
        except Exception:
            return DEFAULT_RISE

    def _band(self) -> float:
        try:
            return float(self._panel.band())
        except Exception:
            return DEFAULT_BAND

    def _wall(self) -> float:
        try:
            return float(self._panel.wall())
        except Exception:
            return DEFAULT_WALL

    # ---------------- 执行 ----------------

    def _run(self, objs) -> None:
        model = self._doc.activeReliefModel() if self._doc is not None else None
        if model is None:
            self._notify("请先在浮雕模式下选中浮雕模型")
            return
        off1 = self._offset()
        rise = self._rise()
        band_w = self._band()
        wall_add = self._wall()
        rings = []
        for o in objs:
            pts = sample_curve_points(o, 512)
            if pts.shape[0] >= 3:
                rings.append(pts)
        if not rings:
            self._notify("没有有效的闭合曲线")
            return
        doc = self._doc
        doc.beginEdit()
        try:
            if model.hasPendingTransform():
                model.bake()
            hf = model.heightField()
            # 步骤0：四边扩边（新建模型余量常只有 5mm，外偏+围坝会出界）
            expand = off1 + max(0.0, band_w)
            pad_px = int(np.ceil(expand / hf.spacing - 1e-9))
            if pad_px > 0:
                hf = hf.padded(pad_px * hf.spacing)   # 新边补 0 基准面
                model.setHeightField(hf)   # 蒙版/幕帘/缓存随场同步
            grid = (hf.origin[0], hf.origin[1], hf.spacing, hf.nx, hf.ny)
            sd = compute_polygon_sd(rings[0], grid,
                                    holes=rings[1:] if len(rings) > 1 else None)
            data = hf.data
            top = float(data.max())          # 当前模型最高点（垫底前）
            floor = sd > -off1               # 外偏曲线以内（含模型）：垫底
            if band_w > 0:
                band = (sd <= -off1) & (sd >= -(off1 + band_w))
            else:
                band = np.zeros_like(sd, dtype=bool)
            if not floor.any() and not band.any():
                doc.endEdit()
                self._notify("曲线区域没有覆盖到任何高度场像素（曲线不在模型上）")
                return
            if floor.any():
                data[floor] = (data[floor].astype(np.float64) + rise
                               ).astype(data.dtype)
            if band.any():
                data[band] = np.float32(top + wall_add)
            model.bumpVersion()
            doc.notifyObjectChanged(model.id())
            doc.endEdit()
        except Exception as e:
            doc.endEdit()
            self._notify(f"硅胶外围失败：{e}")
            return
        parts = []
        if pad_px > 0:
            parts.append(f"四边扩边 {pad_px * hf.spacing:g}mm")
        parts.append(f"外偏 {off1:g} 内整体抬升 {rise:g}")
        if band_w > 0:
            if band.any():
                parts.append(f"外沿 {band_w:g} 环带冲压到 {top + wall_add:g}"
                             f"（模型最高 {top:g}+{wall_add:g}）")
            else:
                parts.append("外沿环带超出模型范围，未生成围坝")
        self._notify("硅胶外围完成：" + "，".join(parts))
        self.finish()


class SiliconeRimParamPanel(ParamPanelBase):
    """参数：外偏距离 / 相对抬升 / 外沿宽度 / 壁顶加高。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        g, lay = self._group("硅胶外围")
        self._spinOffset = self._spin(DEFAULT_OFFSET, decimals=2, step=0.5)
        self._spinOffset.setRange(0.0, 200.0)
        lay.addWidget(self._row("外偏距离", self._spinOffset))
        self._spinRise = self._spin(DEFAULT_RISE, decimals=2, step=0.1)
        self._spinRise.setRange(-100.0, 100.0)
        lay.addWidget(self._row("相对抬升", self._spinRise))
        self._spinBand = self._spin(DEFAULT_BAND, decimals=2, step=0.1)
        self._spinBand.setRange(0.0, 100.0)
        lay.addWidget(self._row("外沿宽度", self._spinBand))
        self._spinWall = self._spin(DEFAULT_WALL, decimals=2, step=0.5)
        self._spinWall.setRange(0.0, 200.0)
        lay.addWidget(self._row("壁顶加高", self._spinWall))
        from ui.styles import theme
        tip = QLabel("执行时先四边扩边（外偏距离+外沿宽度，余量不足的模型"
                     "也能放下围坝）；外偏距离内整体相对抬升（垫底，细节不变）；"
                     "再往外「外沿宽度」的环带绝对冲压到 模型最高点+壁顶加高")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:%s; font-size:9pt;" % theme.TEXT_DIM)
        lay.addWidget(tip)
        self._layout.addWidget(g)
        self._layout.addStretch(1)

    def offset(self) -> float:
        return self._spinOffset.value()

    def rise(self) -> float:
        return self._spinRise.value()

    def band(self) -> float:
        return self._spinBand.value()

    def wall(self) -> float:
        return self._spinWall.value()

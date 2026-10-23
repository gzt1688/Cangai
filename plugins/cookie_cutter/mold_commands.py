# -*- coding: utf-8 -*-
"""月饼/饼干模具命令（私有插件，移植自 Rhino 插件 Model 的 4 个功能）。

- 月饼曲线：选 1 条闭合轮廓 → 向外偏移内壁(缝隙)/外壁(缝隙+2.3) + 镂空框，
  共 3 条曲线（Rhino MoldMooncakeCurveCommand）
- 月饼拉伸：选 3 条（镂空框/内偏移/外偏移）→ 底板(高3, 镂空) + 模壁(高45)
  两个实体，输入曲线删除（Rhino MoldMooncakeExtrudeCommand）
- 饼干曲线：选 1 条闭合轮廓 → 3 条偏移(缝隙/+0.6/+1.6) + 镂空框，共 4 条
  （Rhino SpringCookieFunctions.RunSpringCurve）
- 饼干拉伸：选 4 条（镂空框/内/中/外）→ 底板(3) + 内衬套(43) + 主体壁(38)
  + 外壁层(38) + 顶部斜口放样(38→42)，输入曲线保留
  （Rhino SpringCookieFunctions.RunSpringExtrude）

与 Rhino 的对应关系：
- Rhino ExtrudeCrv 多条嵌套闭合曲线 = 外轮廓带内孔的单个实体，这里用
  BrepShape.fromExtrude(profile, h, holes=[...]) 等价实现；
- Rhino 闭合放样 c1(middle@+38)→c3(middle@+42)→c2(outer@+38) 的斜口，
  用 buildBevelLipSolid（纯拓扑缝合成实心）等价实现；
- 曲线命令工作流同 Rhino（生成 → 对齐 → 点选位置放置）：生成后整组
  曲线跟随鼠标预览（组合包围盒中心贴光标、相对布局不变，即 Rhino
  CenterCurvesAtOrigin 的中心语义，目标点改由用户点选），点击落位；
- 曲线角色识别：优先按镂空框 31×27 几何指纹，退化按面积升序
  （Rhino 月饼按面积、饼干按选择顺序）。
"""
from __future__ import annotations

import traceback
from typing import List, Optional

from core.vector.geometry_types import ObjectType, Point3D
from core.vector.brep_object import BRepObject
from core.vector.brep_shape import BrepShape
from ui.commands.command_instance import CommandInstance
from ui.commands.concrete.solid_commands import (
    ExtrudeCommand, _profileIsHorizontal)

from mold_builder import (offsetRingOutward, mooncakeFrameRing,
                          buildBevelLipSolid, stripClosing, capRing,
                          ringBBox, ringSignedArea, isFrameRing)

_MAX_RING_PTS = 2048   # 环采样上限（描图曲线可能数千点，保护构建耗时）


def _tb() -> None:
    traceback.print_exc()


class _MoldCommon(ExtrudeCommand):
    """月饼/饼干模具命令公共部分：拾取复用挤出命令，几何走模具助手。"""

    # 选取时排除的类型（同挤出命令）
    _SKIP = (ObjectType.SURFACE, ObjectType.SOLID, ObjectType.POINT,
             ObjectType.DIMENSION, ObjectType.IMAGE, ObjectType.GROUP)

    def __init__(self) -> None:
        super().__init__()
        self._picked: List = []
        self._made: List = []      # 本命令生成的对象（拾取时排除）

    # ---------------- 公共助手 ----------------

    def _emitPrompt(self) -> None:
        if self._manager is not None:
            try:
                self._manager.promptChanged.emit(self.prompt())
            except Exception:
                pass

    def _preselectedClosed(self) -> List:
        """启动时已选中的闭合曲线（同挤出命令的过滤）。"""
        if self._doc is None:
            return []
        return [o for o in self._doc.selectedObjects()
                if not o.isLocked() and o.type() not in self._SKIP
                and self._isClosedCurve(o)]

    def _ringsOf(self, objs: List) -> List:
        """曲线对象 → 水平采样环列表（去闭合点、限点数、统一到同一 Z）。"""
        rings = []
        for o in objs:
            r = stripClosing(self._sampleOutline(o))
            if len(r) < 3:
                raise ValueError("存在无法采样的曲线（需闭合轮廓）")
            if not _profileIsHorizontal(r):
                raise ValueError("所有曲线需为水平轮廓（同一水平面）")
            rings.append(capRing(r, _MAX_RING_PTS))
        z0 = rings[0][0].z
        for r in rings:
            if max(abs(p.z - z0) for p in r) > 0.01:
                raise ValueError("所有曲线需位于同一水平面（Z 一致）")
            for p in r:
                p.z = z0
        return rings

    @staticmethod
    def _polyCurveObj(ring, src=None):
        """闭合环 → PolyCurveObject（首尾补重合点=闭合判定/挤出可用）。"""
        from core.vector.poly_curve_object import PolyCurveObject
        pts = [[p.x, p.y, p.z] for p in ring]
        if pts and ring[0].distanceTo(ring[-1]) > 1e-9:
            pts.append([ring[0].x, ring[0].y, ring[0].z])
        o = PolyCurveObject()
        o.addSegment({"points": pts})
        if src is not None:
            o._color = getattr(src, "_color", o._color)
            o._lineWidth = getattr(src, "_lineWidth", o._lineWidth)
        return o

    def _addBodies(self, shapes: List, note: str,
                   remove: Optional[List] = None) -> str:
        """BrepShape 列表 → 实体入档（一次撤销、全选、网格预热）；
        remove 为同时删除的输入曲线（同一撤销记录）。"""
        bodies = []
        for shape in shapes:
            b = BRepObject()
            b.setShape(shape)
            try:
                b.mesh()
                b.topology()
            except Exception:
                pass
            bodies.append(b)
        doc = self._doc
        doc.beginEdit()
        try:
            for o in (remove or []):
                doc.removeObject(o.id())
            for b in bodies:
                doc.addObject(b)
                doc.selectObject(b.id(), True)
        except Exception:
            _tb()
            raise
        finally:
            doc.endEdit()
        self._made.extend(bodies)
        return note.format(n=len(bodies))

    def onMouseMove(self, wp, mods) -> None:
        pass   # 无实时预览（生成很快，点击即出结果）

    def _runExecute(self) -> None:
        """执行拉伸并收尾（成功→结束命令；失败→清空重选）。"""
        try:
            self._notify(self._execute(self._picked))
        except Exception as e:
            _tb()
            self._notify(f"{self.commandTitle()}失败：{e}")
            self._picked = []
            self._emitPrompt()
            return
        self.finish()


# ================================================================ 曲线命令 ----

class _CurveMoldCommand(_MoldCommon):
    """曲线生成基类（同 Rhino 工作流：生成 → 对齐 → 点选位置放置）。

    点 1 条闭合曲线后即按当前缝隙生成全部曲线（镂空框与偏移组已对齐
    为同心），但不入档：整组以待放置环的形式跟随鼠标预览（组合包围盒
    中心贴光标，相对布局不变，同 Rhino CenterCurvesAtOrigin 的中心
    语义），点击后在光标位置入档（一次撤销）；右键取消本次，可连续做。
    """

    DEFAULT_GAP = 0.2
    PANEL_TIP = ""
    PROMPT_DESC = ""

    def __init__(self) -> None:
        super().__init__()
        self._panel = None
        self._pending: Optional[List] = None      # 待放置环列表
        self._pendingCenter: Optional[tuple] = None

    # 子类接口
    def _offsetDeltas(self, gap: float) -> List[float]:
        raise NotImplementedError

    def _madeNote(self, gap: float) -> str:
        raise NotImplementedError

    def paramPanel(self):
        if self._panel is None:
            self._panel = _makeGapPanel(self.DEFAULT_GAP,
                                        self.PANEL_TIP)()
        return self._panel

    def _gap(self) -> float:
        try:
            return float(self._panel.gap())
        except Exception:
            return self.DEFAULT_GAP

    def start(self) -> None:
        CommandInstance.start(self)   # 不走挤出命令的预选预览流程
        clr = getattr(self._view, "clearSnapMarker", None)
        if callable(clr):
            clr()
        self._picked = []
        self._pending = None
        self._emitPrompt()
        # 便捷：启动时已选中一条闭合曲线 → 直接生成待放置
        sel = self._preselectedClosed()
        if len(sel) >= 1:
            try:
                self._preparePending(sel[0], None)
            except Exception as e:
                _tb()
                self._notify(f"{self.commandTitle()}失败：{e}")

    def onMouseMove(self, wp, mods) -> None:
        if self._pending is not None:
            self._updatePlacePreview(wp)

    def onMousePress(self, wp, mods) -> None:
        if self._pending is not None:
            self._commitPlace(wp)
            return
        obj = self._pickClosedCurve(wp)
        if obj is None:
            self._notify("未命中闭合曲线：把鼠标移到曲线上再点击")
            return
        if any(o is obj for o in self._made):
            self._notify("这是本命令刚生成的曲线，请点击原始轮廓")
            return
        try:
            self._preparePending(obj, wp)
        except Exception as e:
            _tb()
            self._notify(f"{self.commandTitle()}失败：{e}")

    def onRightClick(self, wp) -> bool:
        if self._pending is not None:
            self._pending = None       # 取消本次（未入档，无副作用）
            self._clearPreview()
            self._emitPrompt()
            return True
        self.finish()
        return True

    def onEnter(self) -> bool:
        if self._pending is not None:
            return True   # 放置态回车不作为（放置位置以鼠标点击为准）
        self.finish()
        return True

    # ---------------- 生成与放置 ----------------

    def _preparePending(self, obj, wp) -> None:
        """生成全部曲线环（镂空框与偏移同心对齐），进入放置态。"""
        ring = stripClosing(self._sampleOutline(obj))
        if len(ring) < 3:
            raise ValueError("曲线轮廓采样失败（需闭合轮廓）")
        if not _profileIsHorizontal(ring):
            raise ValueError("曲线需为水平轮廓")
        ring = capRing(ring, _MAX_RING_PTS)
        z = ring[0].z
        x0, y0, x1, y1 = ringBBox(ring)
        cx, cy = (x0 + x1) * 0.5, (y0 + y1) * 0.5
        gap = self._gap()
        sharp = obj.type() == ObjectType.LINE

        rings = [mooncakeFrameRing(z, cx, cy)]
        for d in self._offsetDeltas(gap):
            off = offsetRingOutward(ring, d, sharp=sharp)
            if off is None:
                raise ValueError(f"向外偏移 {d:.2f} 失败（距离过大？）")
            rings.append(off)
        self._pending = rings
        bs = [ringBBox(r) for r in rings]
        self._pendingCenter = (
            (min(b[0] for b in bs) + max(b[2] for b in bs)) * 0.5,
            (min(b[1] for b in bs) + max(b[3] for b in bs)) * 0.5)
        self._updatePlacePreview(wp)
        self._emitPrompt()

    def _updatePlacePreview(self, wp) -> None:
        """整组待放置曲线平移到光标（组合中心贴光标）的幽灵预览。"""
        from PySide6.QtGui import QPainterPath
        gcx, gcy = self._pendingCenter
        if wp is None:
            dx = dy = 0.0
        else:
            dx, dy = wp.x - gcx, wp.y - gcy
        path = QPainterPath()
        for r in self._pending:
            moved = [Point3D(p.x + dx, p.y + dy, p.z) for p in r]
            sub = self._uvPath(moved + [moved[0]])   # 补首点闭合
            path.addPath(sub)
        self._setPreviewPath(path)

    def _commitPlace(self, wp) -> None:
        """在光标位置入档（一次撤销），回到拾取态可连续做。"""
        gcx, gcy = self._pendingCenter
        dx, dy = wp.x - gcx, wp.y - gcy
        gap = self._gap()
        doc = self._doc
        doc.beginEdit()
        try:
            for r in self._pending:
                o = self._polyCurveObj(
                    [Point3D(p.x + dx, p.y + dy, p.z) for p in r])
                doc.addObject(o)
                doc.selectObject(o.id(), True)
                self._made.append(o)
        finally:
            doc.endEdit()
        self._pending = None
        self._clearPreview()
        self._notify(self._madeNote(gap))
        self._emitPrompt()

    def finish(self) -> None:
        self._pending = None   # 未入档的待放置直接丢弃，无副作用
        self._clearPreview()
        super().finish()

    def prompt(self) -> str:
        if self._pending is not None:
            return ("曲线组已生成并按中心对齐：移动鼠标选择放置位置，"
                    "点击落位；右键取消本次")
        return (f"点击一条闭合曲线生成{self.PROMPT_DESC}，再点选位置"
                f"放置（可连续做）；右键/Esc 结束")


class MooncakeCurveCommand(_CurveMoldCommand):
    """月饼曲线：轮廓 → 内偏移(缝隙)/外偏移(缝隙+2.3) + 镂空框。"""

    DEFAULT_GAP = 0.2
    PANEL_TIP = ("生成镂空框 + 内偏移(缝隙) + 外偏移(缝隙+2.3)，"
                 "随后用「月饼拉伸」生成模具")
    PROMPT_DESC = "镂空框 + 内/外偏移共 3 条曲线"

    def _offsetDeltas(self, gap: float) -> List[float]:
        return [gap, gap + 2.3]

    def _madeNote(self, gap: float) -> str:
        return (f"月饼曲线完成：镂空框 + 内偏移({gap:.2f}) + "
                f"外偏移({gap + 2.3:.2f})，共 3 条（已选中）")

    def name(self) -> str:
        return "MooncakeCurve"

    def commandTitle(self) -> str:
        return "月饼曲线"


class CookieCurveCommand(_CurveMoldCommand):
    """饼干曲线：轮廓 → 3 条偏移(缝隙/+0.6/+1.6) + 镂空框。"""

    DEFAULT_GAP = 0.26
    PANEL_TIP = ("生成镂空框 + 3 条偏移(缝隙/缝隙+0.6/缝隙+1.6)，"
                 "随后用「饼干拉伸」生成模具")
    PROMPT_DESC = "镂空框 + 3 条偏移共 4 条曲线"

    def _offsetDeltas(self, gap: float) -> List[float]:
        return [gap, gap + 0.6, gap + 1.6]

    def _madeNote(self, gap: float) -> str:
        return (f"饼干曲线完成：镂空框 + 3 条偏移({gap:.2f}/"
                f"{gap + 0.6:.2f}/{gap + 1.6:.2f})，共 4 条（已选中）")

    def name(self) -> str:
        return "CookieCurve"

    def commandTitle(self) -> str:
        return "饼干曲线"


# ================================================================ 拉伸命令 ----

class _ExtrudeMoldCommand(_MoldCommon):
    """拉伸基类：累积点选 N 条闭合曲线 → 一次生成全部实体后结束。"""

    NEED = 3

    def start(self) -> None:
        CommandInstance.start(self)
        clr = getattr(self._view, "clearSnapMarker", None)
        if callable(clr):
            clr()
        self._picked = []
        # 便捷：启动前已选中足够曲线 → 直接生成（同 Rhino 预选工作流）
        sel = self._preselectedClosed()
        if len(sel) >= self.NEED:
            self._picked = sel[:self.NEED]
            self._runExecute()
            return
        self._emitPrompt()

    def onMousePress(self, wp, mods) -> None:
        obj = self._pickClosedCurve(wp)
        if obj is None:
            self._notify("未命中闭合曲线：把鼠标移到曲线上再点击")
            return
        if any(o is obj for o in self._picked):
            self._notify("该曲线已选过")
            return
        self._picked.append(obj)
        if len(self._picked) >= self.NEED:
            self._runExecute()
        else:
            self._emitPrompt()

    def onRightClick(self, wp) -> bool:
        if self._picked:
            self._picked = []   # 清空重选
            self._emitPrompt()
            return True
        self.finish()
        return True

    def onEnter(self) -> bool:
        return self.onRightClick(None)

    # 子类实现
    def _execute(self, objs: List) -> str:
        raise NotImplementedError

    def prompt(self) -> str:
        k = len(self._picked)
        if k == 0:
            return (f"点击 {self.NEED} 条闭合曲线（可先多选后再点按钮）；"
                    "右键结束")
        return f"已选 {k}/{self.NEED} 条闭合曲线：继续点击；右键清空重选"


def _identify(rings: List, roles_asc: List[str]):
    """曲线角色识别。roles_asc 为面积升序角色名（长度=环数）：
    首角色=镂空框，优先按 31×27 几何指纹挑出，其余环按面积升序映射
    后续角色；找不到框时全部按面积升序映射（Rhino 原行为）。
    返回 {角色: 环}。"""
    order = sorted(range(len(rings)),
                   key=lambda i: abs(ringSignedArea(rings[i])))
    frame_idx = next((i for i in order if isFrameRing(rings[i])), None)
    out = {}
    if frame_idx is None:
        for role, i in zip(roles_asc, order):
            out[role] = rings[i]
    else:
        out[roles_asc[0]] = rings[frame_idx]
        rest = [i for i in order if i != frame_idx]
        for role, i in zip(roles_asc[1:], rest):
            out[role] = rings[i]
    return out


class MooncakeExtrudeCommand(_ExtrudeMoldCommand):
    """月饼拉伸：镂空框/内偏移/外偏移 → 底板(高3,镂空) + 模壁(高45)。

    同 Rhino：{镂空框,外偏移} 拉伸 3 = 带镂空花纹孔的底板；
    {内偏移,外偏移} 拉伸 45 = 模壁（外偏移为外廓、内偏移为腔孔）。
    """

    NEED = 3
    H_FLOOR = 3.0
    H_WALL = 45.0

    def _execute(self, objs: List) -> str:
        rings = self._ringsOf(objs)
        m = _identify(rings, ["frame", "inner", "outer"])
        shapes = [
            BrepShape.fromExtrude(m["outer"], self.H_FLOOR, 0.0,
                                  [m["frame"]], "polygon", ["polygon"]),
            BrepShape.fromExtrude(m["outer"], self.H_WALL, 0.0,
                                  [m["inner"]], "polygon", ["polygon"]),
        ]
        return self._addBodies(
            shapes,
            f"月饼拉伸完成：底板(高{self.H_FLOOR:.0f},镂空) + "
            f"模壁(高{self.H_WALL:.0f})，共 {{n}} 个实体",
            remove=objs)   # 同 Rhino：输入曲线已消耗

    def name(self) -> str:
        return "MooncakeExtrude"

    def commandTitle(self) -> str:
        return "月饼拉伸"


class CookieExtrudeCommand(_ExtrudeMoldCommand):
    """饼干拉伸：镂空框/内/中/外 → 底板+内衬套+主体壁+外壁层+顶部斜口。

    同 Rhino：{孔,外}@3 底板、{中,内}@43 内衬套、{内,外}@38 主体壁、
    {中,外}@38 外壁层；斜口 = 闭合放样 c1(中@+38)→c3(中@+42)→
    c2(外@+38)。输入曲线保留（同 Rhino）。
    """

    NEED = 4
    H_FLOOR = 3.0
    H_SLEEVE = 43.0
    H_BODY = 38.0
    BEVEL_LO = 38.0
    BEVEL_RISE = 4.0

    def _execute(self, objs: List) -> str:
        rings = self._ringsOf(objs)
        m = _identify(rings, ["frame", "inner", "middle", "outer"])
        z0 = m["outer"][0].z
        shapes = [
            BrepShape.fromExtrude(m["outer"], self.H_FLOOR, 0.0,
                                  [m["frame"]], "polygon", ["polygon"]),
            BrepShape.fromExtrude(m["middle"], self.H_SLEEVE, 0.0,
                                  [m["inner"]], "polygon", ["polygon"]),
            BrepShape.fromExtrude(m["outer"], self.H_BODY, 0.0,
                                  [m["inner"]], "polygon", ["polygon"]),
            BrepShape.fromExtrude(m["outer"], self.H_BODY, 0.0,
                                  [m["middle"]], "polygon", ["polygon"]),
        ]
        shapes.append(BrepShape.fromPart(buildBevelLipSolid(
            m["outer"], m["middle"],
            z0 + self.BEVEL_LO, z0 + self.BEVEL_LO + self.BEVEL_RISE)))
        return self._addBodies(
            shapes,
            f"饼干拉伸完成：底板({self.H_FLOOR:.0f}) + "
            f"内衬套({self.H_SLEEVE:.0f}) + 主体壁({self.H_BODY:.0f})×2"
            f" + 顶部斜口，共 {{n}} 个实体")

    def name(self) -> str:
        return "CookieExtrude"

    def commandTitle(self) -> str:
        return "饼干拉伸"


# ---------------------------------------------------------------- 参数面板 ----

def _makeGapPanel(default_gap: float, tip: str):
    from PySide6.QtWidgets import (QGroupBox, QHBoxLayout, QLabel,
                                   QVBoxLayout, QWidget)
    from ui.commands.param_panels import ParamPanelBase, _LABEL_W
    from ui.styles import theme

    class _GapPanel(ParamPanelBase):
        """缝隙参数面板：改动即生效（下次点击生成时采用）。"""

        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            g = QGroupBox("偏移")
            lay = QVBoxLayout(g)
            lay.setSpacing(3)
            lay.setContentsMargins(6, 4, 6, 4)
            self._spinGap = self._spin(default_gap, decimals=2, step=0.05)
            self._spinGap.setRange(0.0, 50.0)
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(4)
            lab = QLabel("缝隙")
            lab.setMinimumWidth(_LABEL_W)
            rl.addWidget(lab)
            rl.addWidget(self._spinGap, 1)
            lay.addWidget(row)
            if tip:
                tipLab = QLabel(tip)
                tipLab.setWordWrap(True)
                tipLab.setStyleSheet(
                    "color:%s; font-size:9pt;" % theme.TEXT_DIM)
                lay.addWidget(tipLab)
            self._layout.addWidget(g)
            self._layout.addStretch(1)

        def gap(self) -> float:
            return self._spinGap.value()

    return _GapPanel

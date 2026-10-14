# -*- coding: utf-8 -*-
"""3D 螺纹命令（build123d/OCCT 内核），对标 UG Thread 功能。

交互（Plasticity 风格）：
- 选中 BRep 实体 → 鼠标在 3D 视口点击圆柱面 → 自动识别内孔面/外侧面
  并锁定内/外螺纹 → 面板调参实时预览 → 确认/右键生成；
- 空白点击 → 生成独立外螺纹螺柱（沿 +Z，起点在点击点）。

布尔逻辑与实际加工一致：
- 外螺纹（外侧面）：实体 − 螺旋沟槽（车削，从圆柱表面切除材料）；
- 内螺纹（内孔面）：实体 − 螺旋牙条（攻丝，从孔壁切除材料）。

几何构造走 core.vector.thread_generator（GB/T 193 公制标准牙型，
螺旋线 frenet 扫掠），命令层只负责拾取圆柱面轴系与参数面板联动。
"""
from __future__ import annotations
import math
import threading
from typing import Optional, Tuple

from PySide6.QtCore import Qt, QObject, Signal

from core.vector.geometry_types import ObjectType, Color3D, Point3D
from core.vector.brep_object import BRepObject
from core.vector.brep_shape import BrepShape
from ..command_instance import CommandInstance
from ..param_panels import ThreadParamPanel

import time as _time


class _BgDone(QObject):
    """后台预览线程 → 主线程结果信号（同推拉命令模式）。"""
    done = Signal(object)


def _uiLog(text: str) -> None:
    """螺纹命令诊断日志（UI 实测排障用，写工作区 _thread_ui.log）。"""
    try:
        with open(r"F:\softai\Cangai\cangai_relief\_thread_ui.log", "a",
                  encoding="utf-8") as f:
            f.write("[%s] %s\n" % (_time.strftime("%H:%M:%S"), text))
    except Exception:
        pass


def _isCylinderFace(face) -> bool:
    """判断 build123d Face 是否为圆柱面（圆柱侧面）。"""
    try:
        from OCP.BRep import BRep_Tool
        from OCP.Geom import Geom_CylindricalSurface
        return isinstance(BRep_Tool.Surface_s(face.wrapped),
                          Geom_CylindricalSurface)
    except Exception:
        return False


def _cylinderAxis(face) -> Optional[Tuple[Tuple[float, float, float],
                                          Tuple[float, float, float], float]]:
    """圆柱面 → (轴点, 轴方向, 半径)；非圆柱面返回 None。

    本管线（build123d 布尔/Location）产出的曲面已是全局坐标，
    直接取 Position() 即可（叠加 Location 会双重变换）。
    """
    try:
        from OCP.BRep import BRep_Tool
        h = BRep_Tool.Surface_s(face.wrapped)
        if not _isCylinderFace(face):
            return None
        pos = h.Position()
        loc = pos.Location()
        d = pos.Direction()
        return ((loc.X(), loc.Y(), loc.Z()),
                (d.X(), d.Y(), d.Z()),
                float(h.Radius()))
    except Exception:
        return None


def _faceExtentAlongAxis(face, axis_dir) -> Tuple[float, float]:
    """面沿轴方向的参数范围 [t_min, t_max]（用于推断螺纹长度）。"""
    try:
        bb = face.bounding_box()
        dx, dy, dz = axis_dir
        corners = [
            (bb.min.X, bb.min.Y, bb.min.Z),
            (bb.min.X, bb.min.Y, bb.max.Z),
            (bb.min.X, bb.max.Y, bb.min.Z),
            (bb.min.X, bb.max.Y, bb.max.Z),
            (bb.max.X, bb.min.Y, bb.min.Z),
            (bb.max.X, bb.min.Y, bb.min.Z),
            (bb.max.X, bb.max.Y, bb.min.Z),
            (bb.max.X, bb.max.Y, bb.max.Z),
        ]
        ts = [c[0] * dx + c[1] * dy + c[2] * dz for c in corners]
        return min(ts), max(ts)
    except Exception:
        return 0.0, 0.0


def _classifyFaceSide(part, face, axis_pt, axis_dir) -> Optional[bool]:
    """判断圆柱面是外侧面还是内孔面。

    返回 True=内孔面（实体材料在半径外侧），False=外侧面（材料在内侧），
    None=无法判断。方法：在面上取一点，沿径向 ±ε 采样并用
    BRepClass3d_SolidClassifier 对宿主实体做内外分类（比法线更可靠，
    不受面朝向反转影响）。
    """
    try:
        from OCP.BRep import BRep_Tool
        from OCP.BRepClass3d import BRepClass3d_SolidClassifier
        from OCP.BRepTools import BRepTools
        from OCP.gp import gp_Pnt
        from OCP.TopAbs import TopAbs_IN, TopAbs_ON, TopAbs_REVERSED

        # 面参数域中点取面上点（build123d 0.11 无 position_at，直走 OCP）。
        # 注意：本管线布尔/Location 后曲面已烘焙为全局坐标，不再叠加变换。
        surf = BRep_Tool.Surface_s(face.wrapped)
        try:
            u0, u1, v0, v1 = BRepTools.UVBounds_s(face.wrapped)
        except Exception:
            u0, u1, v0, v1 = surf.Bounds()
        p = surf.Value((u0 + u1) / 2.0, (v0 + v1) / 2.0)
        d = axis_dir
        t = ((p.X() - axis_pt[0]) * d[0] + (p.Y() - axis_pt[1]) * d[1] +
             (p.Z() - axis_pt[2]) * d[2])
        # 径向方向（从轴指向采样点）
        rad = (p.X() - axis_pt[0] - t * d[0],
               p.Y() - axis_pt[1] - t * d[1],
               p.Z() - axis_pt[2] - t * d[2])
        rn = math.sqrt(rad[0] ** 2 + rad[1] ** 2 + rad[2] ** 2)
        if rn < 1e-9:
            return None
        rad = (rad[0] / rn, rad[1] / rn, rad[2] / rn)
        eps = min(0.1, 0.01 * rn)

        def inside(sgn: float) -> bool:
            q = gp_Pnt(p.X() + sgn * eps * rad[0],
                       p.Y() + sgn * eps * rad[1],
                       p.Z() + sgn * eps * rad[2])
            cl = BRepClass3d_SolidClassifier(part.wrapped, q, 1e-7)
            return cl.State() in (TopAbs_IN, TopAbs_ON)

        out_mat = inside(+1.0)   # 半径外侧有材料
        in_mat = inside(-1.0)    # 半径内侧有材料
        if in_mat and not out_mat:
            return False         # 外侧面：材料在轴侧
        if out_mat and not in_mat:
            return True          # 内孔面：材料在孔壁外侧
        # 两侧均有/均无材料（薄壁或分类失败）→ 拓扑朝向兜底：
        # 圆柱面几何外法向恒为径向；实体孔壁的面朝向必为 REVERSED
        # （法向指向轴），外侧面为 FORWARD。
        return face.wrapped.Orientation() == TopAbs_REVERSED
    except Exception:
        return None


class ThreadCommand(CommandInstance):
    """螺纹：点击圆柱面生成内/外螺纹；空白点击生成独立螺柱。"""

    def __init__(self) -> None:
        super().__init__()
        self._panel: Optional[ThreadParamPanel] = None
        self._target: Optional[BRepObject] = None     # 面模式：宿主实体
        self._faceIdx: Optional[int] = None           # 面模式：圆柱面索引
        self._cylAxis: Optional[tuple] = None         # (轴点, 方向, 半径)
        self._pickMode = True                         # True=拾取态；False=确认态
        self._syncing = False                         # 面板参数同步中（抑制预览）
        self._hover: Optional[tuple] = None           # (obj, kind, idx) 悬停面
        self._previewObj: Optional[BRepObject] = None
        self._blankPoint: Optional[Point3D] = None    # 空白点击独立螺柱起点
        self._faceInternal = False                    # 面模式：识别为内孔面？
        self._zStart = 0.0                            # 面模式：螺纹沿轴起点偏移
        # 后台预览（丝滑）：合并式任务队列 + 信号回主线程 + 过期丢弃
        self._bgLock = threading.Lock()
        self._bgPending = None
        self._bgBusy = False
        self._bgSig = _BgDone()
        self._bgSig.done.connect(self._onBgDone)
        self._jobSeq = 0            # 每次新任务/状态复位自增：丢弃过期结果
        self._lastKey = None        # 当前预览对应的参数键（确认时复用）
        # OCCT 非线程安全：后台预览与同步确认串行化（防两线程同时
        # 布尔导致一边卡死——实测 worker 被 concurrent 布尔拖到 100s+）
        self._occtLock = threading.Lock()

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        super().start()
        _uiLog("=== ThreadCommand v0916e start ===")
        self._invalidateJobs()
        clr = getattr(self._view, "clearSnapMarker", None)
        if callable(clr):
            clr()
        self._target = None
        self._faceIdx = None
        self._cylAxis = None
        self._hover = None
        self._previewObj = None
        self._blankPoint = None
        self._pickMode = True
        self._showTarget(True)
        self._faceInternal = False
        self._zStart = 0.0
        if self._panel is not None:
            self._panel.setInternal(False, locked=False)
        # 便捷：启动时已选中 BRep 实体 → 直接进入面拾取态
        if self._doc is not None:
            sel = [o for o in self._doc.selectedObjects()
                   if o.type() == ObjectType.BREP and not o.isLocked()]
            if sel:
                self._target = sel[-1]
                self._notify("已锁定目标实体：点击其圆柱面生成螺纹")

    def paramPanel(self):
        if self._panel is None:
            self._panel = ThreadParamPanel()
            self._panel.confirmed.connect(self._confirmThread)
            self._panel.finished.connect(self.finish)
            self._panel.paramsChanged.connect(self._updatePreview)
        return self._panel

    def _confirmThread(self) -> None:
        """确认生成当前预览螺纹，然后回到拾取态（可继续）。

        无论是否应用成功，都必须恢复原实体显示——预览期间它是
        隐藏的，漏掉恢复就是“确认后实体消失”。
        """
        target = self._target
        if self._previewObj is not None:
            self._applyThread()
        self._pickMode = True
        self._target = None
        self._faceIdx = None
        self._cylAxis = None
        self._blankPoint = None
        self._previewObj = None
        self._faceInternal = False
        self._zStart = 0.0
        self._invalidateJobs()      # 作废在途后台任务与预览键
        if target is not None:
            try:
                target.setVisible(True)
            except Exception:
                pass
        if self._panel is not None:
            # 解锁类型单选但保留用户选择（连续生成同类螺纹）
            self._panel.setInternal(self._panel.isInternal(), locked=False)
        if self._manager is not None:
            self._manager.invalidatePreview()

    def onMouseMove(self, wp, mods) -> None:
        if not self._pickMode:
            return
        hit = self._pickCylinderFace(wp)
        if hit != self._hover:
            self._hover = hit
            self._updateHighlight()
        if hit is not None and self._view is not None:
            self._view.update()

    def onMousePress(self, wp, mods) -> None:
        if not self._pickMode:
            return
        hit = self._pickCylinderFace(wp)
        if hit is None:
            # 空白点击 → 独立螺纹（面板类型：外=螺柱 / 内=螺母），起点在点击点
            _uiLog(f"PRESS MISS at ({wp.x:.2f},{wp.y:.2f},{wp.z:.2f}) "
                   f"viewport={'yes' if self._viewport() else 'NO'} "
                   f"→ blank 模式")
            self._target = None
            self._faceIdx = None
            self._cylAxis = None
            self._blankPoint = Point3D(wp.x, wp.y, wp.z)
            self._pickMode = False
            self._ensurePanel()
            if self._panel is not None:
                self._panel.setInternal(self._panel.isInternal(), locked=False)
            self._updatePreview()
            kindTxt = "内螺纹螺母（带外壁）" if self._panel.isInternal() \
                else "外螺纹螺柱"
            self._notify(f"空白位置：生成独立{kindTxt}（调节参数后确认）")
            return
        obj, _, idx = hit
        self._target = obj
        self._faceIdx = idx
        self._blankPoint = None
        # 提取圆柱面轴系
        shape = obj.shape()
        if not isinstance(shape, BrepShape):
            self._notify("目标实体非 BRep 内核，无法生成螺纹")
            return
        try:
            part = shape.part()
            face = part.faces()[idx]
            ax = _cylinderAxis(face)
            if ax is None:
                _uiLog(f"PICK face#{idx} NOT cylinder "
                       f"(geom={face.geom_type})")
                self._notify("所选面非圆柱面")
                return
            self._cylAxis = ax
            axis_pt, axis_dir, _r = ax
            # 面板要先就位再同步参数（同步中间态不触发预览重建，
            # 避免用未就绪参数算出「预览失败」噪音）
            self._ensurePanel()
            self._syncing = True
            # 识别内孔面 / 外侧面 → 自动定内/外螺纹
            side = _classifyFaceSide(part, face, axis_pt, axis_dir)
            self._faceInternal = bool(side)
            if side is None:
                self._notify("未能自动判断内/外螺纹（面板可手动切换类型）")
            # 螺纹起点取圆柱面沿轴的下端，螺纹长度默认覆盖整个面
            t0, t1 = _faceExtentAlongAxis(face, axis_dir)
            self._zStart = t0 - (axis_pt[0] * axis_dir[0] +
                                 axis_pt[1] * axis_dir[1] +
                                 axis_pt[2] * axis_dir[2])
            # 用圆柱直径推断最近标准尺寸，同步面板
            self._syncPanelFromCylinder(ax[2] * 2.0, face, axis_dir, side)
            _uiLog(f"PICK obj={obj.id()[:8]} face#{idx} "
                   f"axis_pt=({axis_pt[0]:.3f},{axis_pt[1]:.3f},"
                   f"{axis_pt[2]:.3f}) dir=({axis_dir[0]:.3f},"
                   f"{axis_dir[1]:.3f},{axis_dir[2]:.3f}) "
                   f"r={ax[2]:.3f} side={side} t=[{t0:.3f},{t1:.3f}] "
                   f"zStart={self._zStart:.3f} "
                   f"panel D={self._panel.majorDiameter():g} "
                   f"P={self._panel.pitch():g} "
                   f"L={self._panel.length():g} "
                   f"internal={self._panel.isInternal()}")
        except Exception as e:
            _uiLog(f"PICK FAILED: {type(e).__name__}: {e}")
            self._notify(f"圆柱面参数提取失败：{e}")
            return
        finally:
            self._syncing = False
        self._pickMode = False
        self._updatePreview()
        kindTxt = "内孔面 → 内螺纹（攻丝：切出螺旋牙）" if self._faceInternal \
            else "外侧面 → 外螺纹（车削：切出螺旋沟槽）"
        self._notify(f"已识别{kindTxt}；调节参数后确认/右键生成")

    def onRightClick(self, wp) -> bool:
        if not self._pickMode:
            self._confirmThread()
            return True
        self.finish()
        return True

    def onEnter(self) -> bool:
        return self.onRightClick(None)

    def cancel(self) -> None:
        """取消：与 finish 同一套收尾——Esc 不作废在途子进程任务的话，
        _onBgDone 的 job[0] != self._jobSeq 守卫失效，过期回调会把
        真实目标实体隐藏。"""
        self._invalidateJobs()
        self._showTarget(True)
        self._previewObj = None
        if self._view is not None:
            try:
                self._view._subSel = []
                self._view._subHover = None
            except Exception:
                pass
        super().cancel()

    def finish(self) -> None:
        """结束命令：作废后台任务、恢复原实体显示并清高亮。"""
        self._invalidateJobs()
        self._showTarget(True)
        if self._view is not None:
            try:
                self._view._subSel = []
                self._view._subHover = None
            except Exception:
                pass
        super().finish()

    def needsCursorMarker(self) -> bool:
        return False

    def showSnapHints(self) -> bool:
        return False

    def allowsSelectionDelete(self) -> bool:
        """拾取空闲态（未持有目标/落点/预览）允许 Del 删除选中对象——
        螺纹生成结果被自动选中且命令保持激活可连续生成，一律禁止会
        删不掉。"""
        return (self._pickMode and self._target is None
                and self._blankPoint is None)

    # ---------------- 拾取 ----------------

    def _viewport(self):
        for v in (getattr(self, "_modeView", None), self._view):
            if v is not None and hasattr(v, "_pickSubElement"):
                return v
        return None

    def _screenPoint(self, wp):
        vp = self._viewport()
        if vp is None:
            return None
        cam = vp._camera()
        w, h = vp.width(), vp.height()
        pr = vp._project((wp.x, wp.y, wp.z), cam, w, h)
        if pr is None:
            return None
        return (pr[0], pr[1])

    def _pickCylinderFace(self, wp):
        """拾取最近的圆柱面（返回 (obj, "face", 面索引) 或 None）。

        注意 _pickSubElement 的 face 命中返回的是三角形索引，必须经
        mesh["faceRanges"] 转成 BRep 面索引（与 part.faces() 同序）。
        """
        vp = self._viewport()
        if vp is None or self._doc is None:
            return None
        sp = self._screenPoint(wp)
        if sp is None:
            return None
        mx, my = sp
        try:
            hit = vp._pickSubElement(mx, my, kinds={"face"})
        except Exception:
            return None
        if hit is None:
            return None
        obj, kind, idx = hit
        if kind != "face":
            return None
        shape = obj.shape()
        if not isinstance(shape, BrepShape):
            return None
        try:
            mesh = obj.mesh()
            face_idx = None
            for fi, r in enumerate(mesh["faceRanges"]):
                s, c = int(r[0]), int(r[1])
                if s <= idx < s + c:
                    face_idx = fi
                    break
            if face_idx is None:
                return None
            part = shape.part()
            faces = part.faces()
            if face_idx >= len(faces):
                return None
            if not _isCylinderFace(faces[face_idx]):
                return None
            return (obj, "face", face_idx)
        except Exception:
            return None

    # ---------------- 面板联动 ----------------

    def _ensurePanel(self) -> None:
        """确保面板已创建（拾取命中后挂面板）。"""
        if self._panel is None and self._manager is not None:
            self.paramPanel()
            try:
                self._manager.activeInstance()
            except Exception:
                pass

    def _syncPanelFromCylinder(self, diameter, face, axis_dir, side) -> None:
        """圆柱面直径 → 最近标准尺寸；面长度 → 螺纹长度；
        面类型（内孔/外侧）→ 自动锁定内/外螺纹。"""
        if self._panel is None:
            return
        from core.vector.thread_generator import nearest_standard_size
        # 内孔面：底孔(≈小径)反推公称大径；外侧面直径即大径。
        # 匹配约束（保证牙条真的咬进孔壁材料）：
        #   小径 ≤ 孔径（牙根必须达到壁面，否则螺纹悬浮在孔内
        #   ——“看得到螺纹但没切削”就是这个错配）
        #   且 大径 > 孔径（牙顶探入材料）。
        try:
            if side is True:
                from core.vector.thread_generator import (
                    METRIC_COARSE_PITCH, minor_diameter)
                engage, fallback = None, None
                for s in sorted(METRIC_COARSE_PITCH):
                    p = METRIC_COARSE_PITCH[s]
                    if s <= diameter + 0.05:
                        continue          # 牙顶够不到孔壁
                    if fallback is None or s < fallback:
                        fallback = s
                    if minor_diameter(s, p) <= diameter + 0.05 \
                            and (engage is None or s > engage):
                        engage = s        # 小径≤孔径里取最大（咬合最深）
                diameter = engage if engage is not None \
                    else (fallback if fallback is not None else diameter)
        except Exception:
            pass
        std = nearest_standard_size(diameter)
        if std is not None:
            for i in range(self._panel._comboD.count()):
                if abs(self._panel._comboD.itemData(i) - std) < 1e-9:
                    self._panel._comboD.setCurrentIndex(i)
                    break
        # 类型锁定：内孔面 → 内螺纹；外侧面 → 外螺纹
        if side is not None:
            self._panel.setInternal(side is True, locked=True)
        # 长度默认取圆柱面沿轴范围
        try:
            t0, t1 = _faceExtentAlongAxis(face, axis_dir)
            L = t1 - t0
            if L > 0.5:
                self._panel._spinL.setValue(round(L, 1))
        except Exception:
            pass

    # ---------------- 预览 ----------------

    def _updateHighlight(self) -> None:
        if self._view is None:
            return
        entries = []
        if self._hover is not None:
            obj, _, idx = self._hover
            entries.append((obj, "face", idx))
        try:
            self._view._subSel = entries
            self._view.update()
        except Exception:
            pass

    def _showTarget(self, v: bool) -> None:
        """预览期间隐藏/恢复原实体：预览体=「在原模型上」切出螺纹的最终效果。"""
        if self._target is not None:
            try:
                self._target.setVisible(v)
            except Exception:
                pass

    def _invalidateJobs(self) -> None:
        """状态复位：作废在途后台任务与预览参数键。"""
        self._jobSeq += 1
        self._lastKey = None
        with self._bgLock:
            self._bgPending = None

    def _previewKey(self):
        """当前预览结果的参数指纹（确认时判断能否直接复用）。"""
        if self._panel is None:
            return None
        vals = (round(self._panel.majorDiameter(), 4),
                round(self._panel.pitch(), 4),
                round(self._panel.length(), 4),
                self._panel.isInternal(), self._panel.isLeftHand(),
                round(self._panel.clearance(), 4))
        if self._blankPoint is not None:
            return ("blank", vals,
                    (round(self._blankPoint.x, 4),
                     round(self._blankPoint.y, 4),
                     round(self._blankPoint.z, 4)))
        if self._target is None or self._cylAxis is None:
            return None
        ax = self._cylAxis
        return ("face", vals, self._target.id(),
                tuple(round(c, 4) for c in ax[0]),
                tuple(round(c, 4) for c in ax[1]), round(ax[2], 4),
                round(self._zStart, 4), self._faceIdx)

    def _updatePreview(self) -> None:
        """调度子进程后台预览（丝滑）：主线程只做轻量导出，布尔在
        全新子进程算（限内存 1.5G、崩溃不伤主程序），结果回主线程
        导入；过期任务丢弃；参数未变时不重算。"""
        import os
        import json
        if self._panel is None or self._syncing:
            return
        key = self._previewKey()
        if key is None or key == self._lastKey:
            return
        self._jobSeq += 1
        self._lastKey = None
        seq = self._jobSeq
        vals = (self._panel.majorDiameter(), self._panel.pitch(),
                self._panel.length(), self._panel.isInternal(),
                self._panel.isLeftHand(), self._panel.clearance())
        shape = None
        if self._blankPoint is None and self._target is not None:
            shape = self._target.shape()
        blank = None if self._blankPoint is None else \
            (self._blankPoint.x, self._blankPoint.y, self._blankPoint.z)
        BASE = r"F:\softai\Cangai\cangai_relief"
        tmp = os.path.join(BASE, "_bg_tmp")
        os.makedirs(tmp, exist_ok=True)
        job_path = os.path.join(tmp, f"job_{seq}.json")
        in_path = os.path.join(tmp, f"in_{seq}.brep")
        out_path = os.path.join(tmp, f"out_{seq}.brep")
        if shape is not None:
            try:
                from OCP.BRepTools import BRepTools
                BRepTools.Write_s(shape.part().wrapped, in_path)
            except Exception as e:
                self._notify(f"预览失败：实体导出失败 {e}")
                return
        elif blank is None:
            return
        jobdict = {"vals": list(vals), "blank": blank,
                   "axis": [list(self._cylAxis[0]),
                            list(self._cylAxis[1]),
                            self._cylAxis[2]]
                   if self._cylAxis else None,
                   "zStart": self._zStart, "faceIdx": self._faceIdx}
        try:
            with open(job_path, "w", encoding="utf-8") as f:
                json.dump(jobdict, f)
        except Exception as e:
            self._notify(f"预览失败：任务写入失败 {e}")
            return
        with self._bgLock:
            self._bgPending = (seq, key, job_path, in_path, out_path)
        if not self._bgBusy:
            self._bgBusy = True
            threading.Thread(target=self._bgLoop, daemon=True).start()

    def _bgLoop(self) -> None:
        """合并式后台循环：只算最新任务（快速拖参数时旧任务被覆盖）。"""
        while True:
            with self._bgLock:
                job = self._bgPending
                self._bgPending = None
            if job is None:
                self._bgBusy = False
                return
            t0 = _time.time()
            try:
                brep_path, err = self._computeBg(job)   # 子进程编排，零 OCCT
                _uiLog(f"BG job#{job[0]} kind={job[1][0]} "
                       f"elapsed={_time.time() - t0:.1f}s "
                       f"{'err=' + str(err) if err else 'ok'}")
                self._bgSig.done.emit((job, brep_path, err))
            except RuntimeError:
                return          # 命令已销毁（窗口关闭）
            except Exception as e:
                _uiLog(f"BG job#{job[0]} FAILED "
                       f"elapsed={_time.time() - t0:.1f}s {e}")
                try:
                    self._bgSig.done.emit((job, None, str(e)))
                except RuntimeError:
                    return

    def _computeBg(self, job):
        """后台线程：编排子进程计算（线程内零 OCCT——布尔在全新
        子进程里跑，这是本工作区唯一从未失手的运行环境；顺带被
        _run_capped 看门狗限内存 1.5G，原生崩溃不伤主程序）。"""
        import os
        import json
        import subprocess
        import sys
        seq, _key, job_path, in_path, out_path = job
        BASE = r"F:\softai\Cangai\cangai_relief"
        try:
            cmd = [sys.executable, os.path.join(BASE, "_run_capped.py"),
                   "1536", os.path.join(BASE, "_thread_bg.py"),
                   job_path, in_path, out_path]
            # OpenBLAS 按核数预分配大缓冲，会在内存帽下初始化失败
            env = dict(os.environ)
            env.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1",
                       MKL_NUM_THREADS="1")
            try:
                p = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, env=env,
                    creationflags=0x08000000)   # CREATE_NO_WINDOW
            except Exception:
                p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, env=env)
            try:
                out, _ = p.communicate(timeout=240)
            except subprocess.TimeoutExpired:
                p.kill()
                return None, "子进程超时（240s）"
            text = out.decode("utf-8", "replace").strip()
            line = ""
            for ln in reversed(text.splitlines()):
                if ln.startswith("{"):
                    line = ln          # 看门狗退出行在 JSON 之后，别取最后一行
                    break
            status = None
            if line:
                try:
                    status = json.loads(line)
                except Exception:
                    status = None
            if status is None:
                return None, "worker 无有效输出"
            if not status.get("ok"):
                return None, str(status.get("err", "子进程失败"))
            return out_path, None
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"

    def _onBgDone(self, payload) -> None:
        """主线程应用后台结果：导入结果 brep、显示、清理临时文件。"""
        import os
        job, out_path, err = payload
        try:
            if job[0] != self._jobSeq:
                return                  # 已过期（参数又变/状态已复位）
            brep = None
            if out_path is not None:
                try:
                    from OCP.BRepTools import BRepTools
                    from OCP.TopoDS import TopoDS_Shape
                    from OCP.BRep import BRep_Builder
                    from core.vector.thread_generator import (
                        _import_bd, _wrap_topods)
                    shape = TopoDS_Shape()
                    BRepTools.Read_s(shape, out_path, BRep_Builder())
                    part = _wrap_topods(shape, _import_bd())
                    brep = BRepObject()
                    brep.setShape(BrepShape.fromPart(part))
                except Exception as e:
                    err = f"结果导入失败：{e}"
            if brep is None:
                self._previewObj = None
                self._showTarget(True)
                if err:
                    self._notify(f"预览失败：{err}")
            else:
                brep.setColor(Color3D(0x15 / 255.0, 0xB6 / 255.0,
                                      0xC9 / 255.0, 0.55))
                self._previewObj = brep
                self._lastKey = job[1]
                if job[1][0] == "face":    # 面模式：预览替代原实体显示
                    self._showTarget(False)
            if self._manager is not None:
                self._manager.invalidatePreview()
        finally:
            for p in job[2:5]:
                try:
                    if p and os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass

    def previewSolid(self):
        return self._previewObj

    def _faceResult(self, params, tag: str):
        """同步面模式结果（确认兜底路径；预览走后台 _computeBg）。"""
        return self._faceResultCore(params, self._target.shape(),
                                    self._cylAxis, self._zStart,
                                    self._faceIdx, tag)

    def _faceResultCore(self, params, shape, cylAxis, zStart, faceIdx,
                        tag: str):
        """面模式核心布尔（线程安全，不触碰 Qt；预览/确认共用同一
        布尔，保证所见即所得）。实际切削链在
        thread_generator.compute_thread_part（子进程预览同样走它，
        两处永远同源）。"""
        if not isinstance(shape, BrepShape):
            raise ValueError("目标实体非 BRep 内核")
        from core.vector.thread_generator import compute_thread_part
        return compute_thread_part(
            shape.part(), params, cylAxis[0], cylAxis[1], cylAxis[2],
            zStart, faceIdx, log=lambda m: _uiLog(f"{tag} {m}"))

    # ---------------- 执行 ----------------

    def _applyThread(self) -> None:
        """确认生成螺纹：写入文档（面模式原地改实体，空白模式新增对象）。"""
        from core.vector.thread_generator import (
            ThreadParams, make_external_solid, make_groove_solid,
            make_internal_solid, make_ridge_solid, place_along_axis,
            robust_cut)
        if self._doc is None or self._panel is None:
            return
        D = self._panel.majorDiameter()
        P = self._panel.pitch()
        L = self._panel.length()
        internal = self._panel.isInternal()
        lefthand = self._panel.isLeftHand()
        params = ThreadParams(D, P, L, internal=internal,
                              lefthand=lefthand,
                              clearance=self._panel.clearance())
        doc = self._doc
        doc.beginEdit()
        try:
            with self._occtLock:    # 与后台预览串行（OCCT 非线程安全）
                if self._blankPoint is not None:
                    # 独立螺纹：外=螺柱 / 内=螺母（带外壁）
                    solid = make_internal_solid(params) if internal \
                        else make_external_solid(params)
                    placed = place_along_axis(
                        solid, (0.0, 0.0, 1.0),
                        (self._blankPoint.x, self._blankPoint.y,
                         self._blankPoint.z), 0.0)
                    brep = BRepObject()
                    brep.setShape(BrepShape.fromPart(placed))
                    doc.addObject(brep)
                    doc.selectObject(brep.id())
                    try:
                        brep.mesh()
                        brep.topology()
                    except Exception:
                        pass
                    kindTxt = "内螺纹螺母" if internal else "外螺纹螺柱"
                    clrTxt = f" 间隙{params.clearance:g}" \
                        if params.clearance > 0 else ""
                    self._notify(f"已生成独立{kindTxt} M{D:g}×{P:g}×{L:g}"
                                 f"{clrTxt}")
                elif self._target is not None and self._cylAxis is not None:
                    if (self._previewObj is not None
                            and self._lastKey is not None
                            and self._lastKey == self._previewKey()):
                        # 复用后台预览结果：确认零重算，与预览严格一致
                        self._target.setShape(self._previewObj.shape())
                        kindTxt = "内螺纹（攻丝）" if internal \
                            else "外螺纹（车削）"
                        clrTxt = f" 间隙{params.clearance:g}" \
                            if params.clearance > 0 else ""
                        self._notify(f"已生成{kindTxt} M{D:g}×{P:g}×{L:g}"
                                     f"{clrTxt}")
                        try:
                            self._target.mesh()
                            self._target.topology()
                        except Exception:
                            pass
                        try:
                            vp = self._viewport()
                            if vp is not None:
                                vp.update()
                        except Exception:
                            pass
                    else:
                        self._applyOnFace(params)
                else:
                    self._notify("无有效目标")
        except Exception as e:
            import traceback
            traceback.print_exc()
            _uiLog(f"APPLY FAILED: {type(e).__name__}: {e}")
            self._notify(f"螺纹生成失败：{e}")
        finally:
            try:
                doc.endEdit()
            except Exception:
                pass

    def _applyOnFace(self, params) -> None:
        """面模式应用（真实加工逻辑，同 UG Thread）：写入文档。
        布尔与防护统一在 _faceResult（与预览完全一致，所见即所得）。"""
        new_part = self._faceResult(params, "APPLY")
        self._target.setShape(BrepShape.fromPart(new_part))
        kindTxt = "内螺纹（攻丝）" if params.internal else "外螺纹（车削）"
        clrTxt = f" 间隙{params.clearance:g}" if params.clearance > 0 else ""
        self._notify(f"已生成{kindTxt} M{params.major_d:g}×"
                     f"{params.pitch:g}×{params.length:g}{clrTxt}")
        try:
            self._target.mesh()
            self._target.topology()
        except Exception:
            pass
        try:
            vp = self._viewport()
            if vp is not None:
                vp.update()
        except Exception:
            pass

    # ---------------- 提示与反馈 ----------------

    def _notify(self, text: str) -> None:
        if self._manager is not None:
            try:
                self._manager.notifyResult(str(text))
            except Exception:
                pass

    def prompt(self) -> str:
        if self._pickMode:
            return ("点击圆柱面（自动识别内孔/外侧面）生成螺纹；"
                    "空白点击生成独立螺柱/螺母（Esc 结束）")
        if self._blankPoint is not None:
            if self._panel is not None and self._panel.isInternal():
                return "独立内螺纹（螺母，带外壁）：调节参数后右键/确认生成（Esc 放弃）"
            return "独立外螺纹（螺柱）：调节参数后右键/确认生成（Esc 放弃）"
        return "圆柱面螺纹：调节参数后右键/确认生成（Esc 放弃，再 Esc 结束）"

    def name(self) -> str:
        return "Thread"

    def commandTitle(self) -> str:
        return "螺纹"


def registerAll(manager) -> None:
    manager.register("Thread", ThreadCommand)
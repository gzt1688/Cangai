"""墙线（WallLine）命令：选中任意曲线（单条/多条）→ 双侧偏移成墙厚 → 挤出高度。

交互：
- 先选中曲线再点按钮：全部加入（后台生成预览，丝滑不卡）
- 未选中点按钮：逐条点击选曲线（累积，可多条）
面板（厚度/高度）实时预览（后台批量重建）；**右键/回车确认生成**，
生成后命令结束（无确认按钮）。

偏移完全复用软件"偏移"命令（clipperOffsetBothSides：双侧偏移 + 圆头盖
+ 保留内轮廓）：偏移得外/内轮廓后全部一起挤出成一个墙体（外-内），
内轮廓不单独成实体；自相交由 Clipper 自动处理。
"""
from __future__ import annotations

import threading
import time
from typing import List, Optional

from PySide6.QtCore import QObject, Signal as _Signal

from core.vector.geometry_types import Point3D, Color3D, ObjectType
from core.vector.brep_shape import BrepShape
from core.vector.brep_object import BRepObject
from ui.commands.command_instance import CommandInstance

_MAX_PTS = 900         # 采样点数上限（clipper 输入保护）
_REBUILD_MS = 90       # 参数连续拖动时的重建节流
_PREVIEW_TOL = 0.5     # 预览网格容差（build123d tessellate 实测对平面
                       # 细分影响有限，保留默认精度兜底）
_PREVIEW_ANGULAR = 0.25


class _WallPreviewSignal(QObject):
    done = _Signal(object)


def _import_bd():
    from core.vector.brep_shape import _stagedImportBd
    _stagedImportBd()
    import build123d  # noqa: F401
    return build123d


def _reducePts(pts, tol=0.08):
    """保形减稀（Ramer-Douglas-Peucker）：降低 clipper 输入点数。

    容差极小（0.08 世界单位，< 1/10 墙厚），偏移轮廓与原始曲线形状
    无感差异；闭合环（首尾重合）保持闭合。避免圆/样条被 2D 显示精度
    采样成 256/101 点导致 clipper 偏移慢 7 倍。
    """
    n = len(pts)
    if n <= 5:
        return pts
    closed = pts[0].distanceTo(pts[-1]) < 1e-9
    seq = pts[:-1] if closed else list(pts)

    def dist(a, b, p):
        dx = b.x - a.x
        dy = b.y - a.y
        l2 = dx * dx + dy * dy
        if l2 < 1e-12:
            return ((p.x - a.x) ** 2 + (p.y - a.y) ** 2) ** 0.5
        t = max(0.0, min(1.0, ((p.x - a.x) * dx + (p.y - a.y) * dy) / l2))
        qx = a.x + t * dx
        qy = a.y + t * dy
        return ((p.x - qx) ** 2 + (p.y - qy) ** 2) ** 0.5

    def dp(seq, tol, out):
        if len(seq) < 3:
            out.extend(seq)
            return
        a, b = seq[0], seq[-1]
        dmax, idx = 0.0, -1
        for i in range(1, len(seq) - 1):
            d = dist(a, b, seq[i])
            if d > dmax:
                dmax, idx = d, i
        if dmax > tol and idx > 0:
            dp(seq[:idx + 1], tol, out)
            dp(seq[idx:], tol, out)
        else:
            out.append(a)
            out.append(b)

    out: List[Point3D] = []
    dp(seq, tol, out)
    if closed:
        out.append(out[0])
    return out


def _sampleCurve(obj) -> List[Point3D]:
    """任意曲线 → 世界点列（与 2D 画布显示同源：复用挤出命令采样）。

    注意：_sampleOutline 对 CURVE/NURBS 类型无条件补首点做闭合点
    （它按挤出闭合轮廓设计）；开放曲线必须去掉，否则 clipper 会把
    开放线当闭合处理（墙首尾自动相连）。返回前做保形减稀（容差
    0.05 世界单位），避免高点数让 clipper 偏移变慢。
    """
    from ui.commands.concrete.solid_commands import ExtrudeCommand
    try:
        pts = ExtrudeCommand._sampleOutline(obj)
    except Exception:
        return []
    if not _isClosedCurve(obj) and len(pts) >= 2:
        if pts[0].distanceTo(pts[-1]) < 1e-9:
            pts = pts[:-1]
    return _reducePts(pts)


def _isClosedCurve(obj) -> bool:
    """闭合判定：直接复用 2D 画布既有判定（圆/椭圆/矩形天然闭合）。"""
    from ui.commands.concrete.solid_commands import ExtrudeCommand
    try:
        return bool(ExtrudeCommand._isClosedCurve(obj))
    except Exception:
        return False


def _polyArea(ring) -> float:
    """shoelace 有符号面积（外环/内环判定与分裂排序用）。"""
    s = 0.0
    n = len(ring)
    if n < 3:
        return 0.0
    for i in range(n):
        p1 = ring[i]
        p2 = ring[(i + 1) % n]
        x1 = p1.x if hasattr(p1, "x") else p1[0]
        y1 = p1.y if hasattr(p1, "y") else p1[1]
        x2 = p2.x if hasattr(p2, "x") else p2[0]
        y2 = p2.y if hasattr(p2, "y") else p2[1]
        s += x1 * y2 - x2 * y1
    return s * 0.5


def _toPolygon(bd, ring):
    """闭合点环（含首尾重合时去重）→ build123d Polygon。

    强制逆时针（法线 +Z）：clipper 分裂轮廓点序不统一，
    顺时针 Polygon 的 extrude 会沿其法线向下（负方向）挤出。
    """
    pts = list(ring)
    if len(pts) >= 2 and abs(pts[0][0] - pts[-1][0]) < 1e-9 \
            and abs(pts[0][1] - pts[-1][1]) < 1e-9:
        pts = pts[:-1]
    if len(pts) < 3:
        return None
    if _polyArea(pts) < 0:
        pts = pts[::-1]
    xy = [(p.x, p.y) if hasattr(p, "x") else (p[0], p[1]) for p in pts]
    return bd.Polygon(*[bd.Vector(a, b) for a, b in xy])


def _buildWall(pts, thickness, height, z, closed=False):
    """复用偏移命令的双侧偏移（圆头盖+保留内轮廓）→ 内外轮廓一起挤出。

    面积最大=外轮廓，其余=内孔；先建外环 Face、内孔用 2D 布尔减掉，
    再一次性挤出——比"每环单独挤出再 3D 布尔减"快 2~3 倍
    （2D 布尔远快于 3D 布尔）。内轮廓不单独成实体，自相交由
    Clipper 自动处理。
    """
    bd = _import_bd()
    if len(pts) < 2 or thickness <= 0.0 or height <= 0.0:
        return None
    from core.vector.offset_geometry import clipperOffsetBothSides
    try:
        rings = clipperOffsetBothSides(pts, thickness, closed,
                                       capStyle="round", keepInner=True)
    except Exception:
        return None
    if not rings:
        return None
    rings.sort(key=lambda r: -abs(_polyArea(r)))
    outer = _toPolygon(bd, rings[0])
    if outer is None:
        return None
    # 外环 sketch − 内孔 sketch（2D 布尔）→ 一次挤出
    # （比"每环单独挤出再 3D 布尔减"快 2~3 倍）
    for r in rings[1:]:
        if abs(_polyArea(r)) < 1e-6:
            continue
        inner = _toPolygon(bd, r)
        if inner is None:
            continue
        try:
            outer = outer - inner
        except Exception:
            continue
    try:
        solid = bd.extrude(outer, height)
    except Exception:
        return None
    if abs(z) > 1e-9:
        solid = solid.translate((0.0, 0.0, z))
    return solid


class WallLineCommand(CommandInstance):
    """墙线：选中任意曲线（单条或多条）→ 双侧偏移成墙厚 → 挤出高度。"""

    def __init__(self) -> None:
        super().__init__()
        self._items: List[tuple] = []     # [(pts, closed), ...]
        self._pickedObjs: List = []       # 已加入的源曲线对象（去重）
        self._panel = None
        self._thickness = 0.8
        self._height = 5.0
        self._previewObj: Optional[BRepObject] = None
        self._bgLock = threading.Lock()
        self._bgPending = None
        self._bgBusy = False
        self._bgSig = _WallPreviewSignal()
        self._bgSig.done.connect(self._onBgDone)
        self._lastKey = None
        self._lastRebuild = 0.0

    # ---------------- 生命周期 ----------------
    def start(self) -> None:
        super().start()
        self._items = []
        self._pickedObjs = []
        self._previewObj = None
        # 已选中曲线 → 直接加入（先选中，再点按钮）
        curves = []
        if self._doc is not None:
            for o in self._doc.selectedObjects():
                if o.isLocked():
                    continue
                t = o.type()
                if t in (ObjectType.SURFACE, ObjectType.SOLID, ObjectType.POINT,
                         ObjectType.DIMENSION, ObjectType.IMAGE, ObjectType.GROUP):
                    continue
                pts = _sampleCurve(o)
                if len(pts) >= 2:
                    curves.append((pts, _isClosedCurve(o)))
                    self._pickedObjs.append(o)
        if curves:
            self._items = curves
            self._notify(f"墙线：已选 {len(curves)} 条曲线，"
                         "调厚度/高度后右键/回车确认生成")
            self._refreshPreview(force=True)
        else:
            self._notify("墙线：点击曲线加入（可多条），或先选中曲线再点"
                         "本按钮；右键/回车确认生成")

    def prompt(self) -> str:
        if self._items:
            n = len(self._items)
            return (f"墙线：{n} 条曲线，厚度 {self._thickness:g} / "
                    f"高度 {self._height:g}，右键/回车确认生成")
        return "墙线：点击选中任意曲线（直线/圆弧/样条/圆）；右键结束"

    def name(self) -> str:
        return "WallLine"

    def needsCursorMarker(self) -> bool:
        return False

    def showSnapHints(self) -> bool:
        return False

    def _notify(self, text: str) -> None:
        if self._manager is not None:
            self._manager.notifyResult(str(text))

    def previewSolid(self):
        return self._previewObj

    # ---------------- 参数面板 ----------------
    def paramPanel(self):
        if self._panel is None:
            from ..param_panels import WallParamPanel
            self._panel = WallParamPanel()
            self._panel.thicknessChanged.connect(self._onThickness)
            self._panel.heightChanged.connect(self._onHeight)
            self._panel.setThickness(self._thickness)
            self._panel.setHeight(self._height)
        return self._panel

    def _onThickness(self, v: float) -> None:
        self._thickness = max(0.01, v)
        self._refreshPreview(force=True)

    def _onHeight(self, v: float) -> None:
        self._height = max(0.01, v)
        self._refreshPreview(force=True)

    # ---------------- 拾取（single 模式） ----------------
    def _pickCurve(self, wp):
        """点选任意曲线（开放/闭合），复用挤出命令采样 + 距离判定。"""
        import numpy as np
        from ui.commands.concrete.solid_commands import _pointPolylineDist2D
        best, best_d = None, 1e18
        for o in self._doc.visibleObjects():
            if o.isLocked():
                continue
            t = o.type()
            if t in (ObjectType.SURFACE, ObjectType.SOLID, ObjectType.POINT,
                     ObjectType.DIMENSION, ObjectType.IMAGE, ObjectType.GROUP):
                continue
            pts = _sampleCurve(o)
            if len(pts) < 2:
                continue
            arr = np.asarray([[p.x, p.y] for p in pts], dtype=np.float64)
            d = _pointPolylineDist2D(wp.x, wp.y, arr)
            perim = float(np.linalg.norm(np.diff(arr, axis=0), axis=1).sum())
            if perim < 1e-9:
                continue
            tol = max(5.0, 0.05 * perim)
            if d < tol and d < best_d:
                best, best_d = o, d
        return best

    def onMouseMove(self, wp, mods) -> None:
        if self._items:
            return

    def onMousePress(self, wp, mods) -> None:
        if self._view is None:
            return
        o = self._pickCurve(wp)
        if o is None:
            self._notify("未选中曲线，请点击曲线附近")
            return
        if any(o is p for p in self._pickedObjs):
            self._notify("该曲线已加入")
            return
        pts = _sampleCurve(o)
        if len(pts) < 2:
            self._notify("曲线采样失败")
            return
        self._pickedObjs.append(o)
        self._items.append((pts, _isClosedCurve(o)))
        self._notify(f"已选 {len(self._items)} 条曲线"
                     f"（厚度 {self._thickness:g} / 高度 {self._height:g}，"
                     "右键/回车确认生成）")
        self._refreshPreview(force=True)

    def onRightClick(self, wp) -> bool:
        # 有曲线：右键 = 确认生成；无曲线：右键 = 结束命令
        if self._items:
            self._apply()
            return True
        self.finish()
        return True

    def onEnter(self) -> bool:
        return self.onRightClick(None)

    # ---------------- 预览（后台批量重建，丝滑） ----------------
    def _refreshPreview(self, force: bool = False) -> None:
        if not self._items:
            self._previewObj = None
            if self._manager is not None:
                self._manager.invalidatePreview()
            return
        key = (round(self._thickness, 2), round(self._height, 2))
        if not force:
            if key == self._lastKey:
                return
            now = time.monotonic() * 1000.0
            if now - self._lastRebuild < _REBUILD_MS:
                return
        self._lastKey = key
        self._lastRebuild = time.monotonic() * 1000.0
        self._scheduleBg("preview")

    def _scheduleBg(self, kind: str) -> None:
        with self._bgLock:
            self._bgPending = (kind, list(self._items), self._thickness,
                               self._height)
        if not self._bgBusy:
            self._bgBusy = True
            threading.Thread(target=self._bgLoop, daemon=True).start()

    def _buildAll(self, items, thick, hgt):
        """生成所有墙实体（后台串行）。

        OCP/build123d 扩展持有 GIL，多线程并行反而因线程切换灾难性慢
        （实测 4 线程并行 24 条 22s vs 串行 1.7s），故严格串行。
        """
        parts: List = []
        for pts, closed in items:
            try:
                z = pts[0].z if pts else 0.0
                s = _buildWall(pts, thick, hgt, z, closed)
                if s is not None:
                    parts.append(s)
            except Exception:
                continue
        return parts

    def _bgLoop(self) -> None:
        while True:
            with self._bgLock:
                job = self._bgPending
                self._bgPending = None
            if job is None:
                self._bgBusy = False
                return
            kind, items, thick, hgt = job
            err: Optional[str] = None
            try:
                parts = self._buildAll(items, thick, hgt)
            except Exception as e:  # noqa: BLE001
                parts, err = [], str(e)
            try:
                if kind == "apply":
                    # 后台预三角化（精细网格缓存）→ 主线程 addObject 显示零卡
                    objs = []
                    for p in parts:
                        bs = BrepShape.fromPart(p)
                        try:
                            bs.triangulate()
                        except Exception:
                            pass
                        objs.append(bs)
                    self._bgSig.done.emit(("apply", thick, hgt, objs, err))
                else:
                    # 预览：Compound 包装（无布尔 union，毫秒级）→ 后台
                    # 预三角化（网格缓存随对象带去主线程，显示零卡顿）
                    bs = None
                    if parts:
                        bd = _import_bd()
                        comp = bd.Compound(children=parts)
                        bs = BrepShape.fromPart(comp)
                        bs.setMeshQuality(tol=_PREVIEW_TOL,
                                          angular=_PREVIEW_ANGULAR)
                        try:
                            bs.triangulate()
                        except Exception:
                            pass
                    self._bgSig.done.emit(("preview", thick, hgt, bs, err))
            except RuntimeError:
                return

    def _onBgDone(self, payload) -> None:
        kind, thick, hgt, data, err = payload
        if not self._items:
            return
        if abs(self._thickness - thick) > 0.01 or abs(self._height - hgt) > 0.01:
            return  # 过期结果，丢弃
        if kind == "preview":
            if data is None:
                if err:
                    self._notify(f"墙线预览失败：{err}")
                return
            prev = BRepObject()
            prev.setShape(data)
            prev.setColor(Color3D(0.08, 0.71, 0.79, 0.55))
            self._previewObj = prev
            if self._manager is not None:
                self._manager.invalidatePreview()
            try:
                self._viewport().update()
            except Exception:
                pass
        else:  # apply
            if not data:
                self._notify(f"墙生成失败：{err or '轮廓无效'}"
                             "（可调参数后右键重试）")
                return
            doc = self._doc
            doc.beginEdit()
            try:
                for bs in data:
                    obj = BRepObject()
                    obj.setShape(bs)
                    obj.setColor(Color3D(0.2, 0.71, 0.9, 1.0))
                    doc.addObject(obj)
            finally:
                doc.endEdit()
            self._items = []
            self._pickedObjs = []
            self._previewObj = None
            if self._manager is not None:
                self._manager.invalidatePreview()
            try:
                self._viewport().update()
            except Exception:
                pass
            self._notify(f"已生成 {len(data)} 个墙体")
            self.finish()

    # ---------------- 确认 ----------------
    def _apply(self) -> None:
        if not self._items:
            self._notify("请先选中曲线")
            return
        self._notify(f"正在生成 {len(self._items)} 个墙体…")
        self._scheduleBg("apply")

    def cancel(self) -> None:
        self._previewObj = None
        self._items = []
        self._pickedObjs = []
        if self._manager is not None:
            self._manager.invalidatePreview()
        super().cancel()


def registerAll(manager) -> None:
    manager.register("WallLine", WallLineCommand)

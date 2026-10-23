"""3D 实体命令：Extrude（挤出）——闭合曲线沿 +Z 拉伸为实体（带拔模角度）。

"点击即生效"型命令（同区域塑形）：点"挤出"按钮进入功能态，鼠标移到闭合
曲线上实时显示挤出预览（源曲线金色高亮置顶 + 实体预览），点击即按当前面板
参数生成 BRepObject（PolySolid 轻量内核，恒 BREP 类型）；可连点多条曲线，
每次点击 = 一次独立撤销，右键/回车结束命令。多选曲线后启动 = 按嵌套
关系分组全部挤出：偶数层为外轮廓、其直接内层为孔（同心圆交替 → 多个
圆环），独立轮廓各自成组。
"""
from __future__ import annotations
import math
from typing import List, Optional

import numpy as np

from core.vector.geometry_types import ObjectType, Point3D
from core.vector.brep_object import BRepObject
from core.vector.brep_shape import BrepShape, bdKernelReady, bdKernelError
from core.vector.poly_solid import PolySolid
from ..command_instance import CommandInstance
from ..param_panels import ExtrudeParamPanel


def _pointPolylineDist2D(px: float, py: float, pts: np.ndarray) -> float:
    """世界点 (px,py) 到 XY 折线的最小距离（拾取用；忽略 Z 避免构造平面差导致拾取失败）。"""
    ax, ay = pts[:-1, 0], pts[:-1, 1]
    abx = pts[1:, 0] - ax
    aby = pts[1:, 1] - ay
    apx, apy = px - ax, py - ay
    len2 = abx * abx + aby * aby
    t = np.where(len2 > 1e-18, (apx * abx + apy * aby) / np.maximum(len2, 1e-18), 0.0)
    np.clip(t, 0.0, 1.0, out=t)
    dx = apx - t * abx
    dy = apy - t * aby
    return float(np.sqrt(np.min(dx * dx + dy * dy)))


def _profileIsHorizontal(pts: List[Point3D], tol: float = 1e-6) -> bool:
    """轮廓是否近似水平（各点 Z 一致）：挤出沿 +Z 仅支持水平闭合轮廓。"""
    if len(pts) < 3:
        return False
    zs = [p.z for p in pts]
    return (max(zs) - min(zs)) <= tol * max(
        1.0, max(math.hypot(p.x, p.y) for p in pts))


def _polyArea2(pts: List[Point3D]) -> float:
    """XY 鞋带面积（绝对值）：多轮廓排序用（面积最大者为外轮廓）。"""
    s = 0.0
    n = len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        s += a.x * b.y - b.x * a.y
    return abs(s) * 0.5


def _pointInPolyXY(px: float, py: float, pts) -> bool:
    """射线法点在多边形内（XY；pts 为 Point3D 列表，闭合轮廓）。"""
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i].x, pts[i].y
        xj, yj = pts[j].x, pts[j].y
        if (yi > py) != (yj > py):
            xint = (xj - xi) * (py - yi) / (yj - yi) + xi
            if px < xint:
                inside = not inside
        j = i
    return inside


class ExtrudeCommand(CommandInstance):
    """挤出：点击曲线选中 → 调参数预览 → 确认/右键挤出（可连续挤出多条）。"""

    _MAX_PROFILE_PTS = 256   # 轮廓采样点上限（控制实体面数）

    def __init__(self) -> None:
        super().__init__()
        self._panel: ExtrudeParamPanel | None = None
        self._previewCurve = None       # 当前预览所基于的曲线（外轮廓）
        self._previewHoles: List = []   # 多轮廓模式的孔轮廓对象列表（圆环等）
        self._previewGroups: List = []  # 多组模式 [(外轮廓, [孔…]), …] 全部挤出
        self._previewObj: Optional[BRepObject] = None
        self._pickMode = True           # True=等待点击曲线；False=已选中待确认

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        super().start()
        clr = getattr(self._view, "clearSnapMarker", None)
        if callable(clr):
            clr()
        self._pickMode = True
        self._previewCurve = None
        self._previewHoles = []
        self._previewGroups = []
        self._previewObj = None
        # 便捷：启动时已选中闭合曲线 → 直接进入预览态（调参数后确认挤出）
        # 多条选中 → 按嵌套分组：偶数层=外轮廓、其直接内层=孔；独立轮廓
        # 各自成组全部挤出（同心圆奇偶交替 = 多个圆环）
        if self._doc is not None:
            sel = [o for o in self._doc.selectedObjects()
                   if not o.isLocked() and o.type() not in (
                       ObjectType.SURFACE, ObjectType.SOLID, ObjectType.POINT,
                       ObjectType.DIMENSION, ObjectType.IMAGE, ObjectType.GROUP)
                   and self._isClosedCurve(o)]
            if sel:
                self.paramPanel()
                groups = self._groupCurves(sel)
                if groups:
                    self._previewGroups = groups if len(groups) > 1 else []
                    self._previewCurve = groups[0][0]
                    self._previewHoles = groups[0][1]
                    self._pickMode = False
                    self._updatePreview()
                    if len(groups) > 1:
                        self._notify(f"多组挤出：{len(groups)} 组轮廓全部挤出"
                                     f"（嵌套曲线按圆环处理）")
                    elif groups[0][1]:
                        self._notify(f"多轮廓挤出：外轮廓 + "
                                     f"{len(groups[0][1])} 个孔")

    def paramPanel(self):
        if self._panel is None:
            self._panel = ExtrudeParamPanel()
            self._panel.confirmed.connect(self._confirmExtrude)    # 确认：挤出当前预览
            self._panel.finished.connect(self.finish)              # 结束：退出命令
            self._panel.paramsChanged.connect(self._updatePreview)
        return self._panel

    def _confirmExtrude(self) -> None:
        """确认挤出当前预览（多组 = 全部挤出，一次撤销），回到拾取态。"""
        if not bdKernelReady():
            err = bdKernelError()
            if err is not None:
                self._notify("3D 内核不可用：%s（详见 crash.log）" % err)
                return
            # 预载线程仍在导入 build123d：此刻构建会在模块锁上冻结 UI
            # 数秒到数十秒（冷盘首次导入）——提示稍候，不阻塞
            self._notify("几何内核预热中，请稍候 2~3 秒再确认（仅首次启动）")
            return
        bodies = self._buildGroupBodies()
        if bodies:
            self._extrudeBodies(bodies)
        else:
            self._notify("挤出失败：曲线轮廓需为水平闭合曲线且点数充足")
        self._pickMode = True
        self._previewCurve = None
        self._previewGroups = []
        self._previewObj = None
        if self._manager is not None:
            self._manager.invalidatePreview()

    def onMouseMove(self, wp, mods) -> None:
        """拾取态：鼠标悬停曲线刷新预览；确认态：保持当前预览不变。"""
        if not self._pickMode:
            return
        cand = self._pickClosedCurve(wp)
        if cand is not self._previewCurve:
            self._previewCurve = cand
            self._previewGroups = []
            self._previewHoles = []
            self._updatePreview()

    def onMousePress(self, wp, mods) -> None:
        """拾取态：点击曲线选中并进入确认态；确认态：忽略（等确认/右键）。"""
        if not self._pickMode:
            return
        obj = self._pickClosedCurve(wp)
        if obj is None:
            self._notify("未命中闭合曲线：把鼠标移到曲线上再点击")
            return
        self._previewCurve = obj
        self._previewGroups = []
        self._previewHoles = []
        self._pickMode = False
        self._updatePreview()

    def onRightClick(self, wp) -> bool:
        # 确认态：右键 = 确认挤出；拾取态：右键 = 结束命令
        if not self._pickMode:
            self._confirmExtrude()
            return True
        self.finish()
        return True

    def onEnter(self) -> bool:
        return self.onRightClick(None)

    def needsCursorMarker(self) -> bool:
        """挤出不显示光标预览点。"""
        return False

    def showSnapHints(self) -> bool:
        return False

    def allowsSelectionDelete(self) -> bool:
        """拾取空闲态（未持有预览曲线）允许 Del 删除选中对象——挤出结果
        被自动选中且命令保持激活可连续挤出，一律禁止会删不掉。"""
        return self._pickMode and self._previewCurve is None

    # ---------------- 拾取 ----------------

    def _pickClosedCurve(self, wp):
        """拾取最近的闭合曲线（XY 距离在容差内；忽略 Z 避免构造平面差导致失败）。"""
        if self._doc is None:
            return None
        best, best_d = None, 1e9
        for o in self._doc.visibleObjects():
            if o.isLocked() or \
                    o.type() in (ObjectType.SURFACE, ObjectType.SOLID,
                                 ObjectType.POINT, ObjectType.DIMENSION,
                                 ObjectType.IMAGE, ObjectType.GROUP):
                continue
            if not self._isClosedCurve(o):
                continue
            pts = self._sampleOutline(o)
            if len(pts) < 3:
                continue
            arr = np.asarray([[p.x, p.y] for p in pts], dtype=np.float64)
            if not np.allclose(arr[0], arr[-1]):
                arr = np.vstack([arr, arr[0:1]])
            d = _pointPolylineDist2D(wp.x, wp.y, arr)
            perim = float(np.linalg.norm(np.diff(arr, axis=0), axis=1).sum())
            tol = max(5.0, 0.05 * perim)   # 容差：周长 5%，下限 5 世界单位
            if d < tol and d < best_d:
                best, best_d = o, d
        return best

    @staticmethod
    def _isClosedCurve(obj) -> bool:
        """闭合判定：直接复用 2D 画布选中摘要的同一套判定
        （MainWindow._isCurveClosed：isClosed 标志 + 首末采样点距离，
        圆/椭圆/矩形天然闭合），保证 3D 挤出与 2D 显示一致。"""
        from ui.windows.mainWindow import MainWindow
        return bool(MainWindow._isCurveClosed(obj))

    def _groupCurves(self, curves) -> List[tuple]:
        """闭合曲线按嵌套关系分组：每条曲线的深度 = 包含它的其他曲线
        数（包含判定=轮廓代表点在对方多边形内）；深度偶数者为外轮廓，
        其直接内层（parent 指向它且深度为奇）为孔。独立轮廓各自成组
        → 全部都挤出；同心圆奇偶交替 → 多个圆环。"""
        items = []
        for o in curves:
            pts = self._sampleOutline(o)
            if len(pts) >= 3:
                items.append({"obj": o, "pts": pts,
                              "area": _polyArea2(pts),
                              "depth": 0, "parent": -1})
        n = len(items)
        for i in range(n):
            # 代表点取轮廓首顶点（必在自身轮廓上；层叠族下不会落在
            # 其他轮廓的边上）
            px, py = items[i]["pts"][0].x, items[i]["pts"][0].y
            for j in range(n):
                if i == j:
                    continue
                if _pointInPolyXY(px, py, items[j]["pts"]):
                    items[i]["depth"] += 1
                    # parent = 最紧邻的包含者（面积最小）
                    if items[i]["parent"] < 0 or \
                            items[j]["area"] < \
                            items[items[i]["parent"]]["area"]:
                        items[i]["parent"] = j
        groups = []
        for i, it in enumerate(items):
            if it["depth"] % 2 != 0:
                continue
            holes = [o2["obj"] for k, o2 in enumerate(items)
                     if o2["parent"] == i and o2["depth"] % 2 == 1]
            groups.append((it["obj"], holes))
        return groups

    @classmethod
    def _sampleOutline(cls, obj) -> List[Point3D]:
        """闭合曲线 → 世界 3D 采样轮廓（带 Z；按弧长抽稀到上限点数）。

        覆盖各类曲线：圆/弧/椭圆按参数采样（无 getSampledPoints），
        自由曲线用 getSampledPoints，多段曲线拼接 segments，折线取 points()。
        """
        t = obj.type()
        # 自由曲线（样条/NURBS）：优先取画布显示同源的采样点
        # （Catmull-Rom 过点插值，getSampledPoints 与 2D 显示完全一致）——
        # 3D 侧面形状 = 用户看到的曲线形状；控制点仅作兜底。
        if t in (ObjectType.CURVE, ObjectType.NURBS_CURVE):
            getter = getattr(obj, "getSampledPoints", None)
            if callable(getter):
                try:
                    raw = getter()
                except TypeError:
                    try:
                        raw = getter(cls._MAX_PROFILE_PTS)
                    except Exception:
                        raw = None
                if raw and len(raw) >= 3:
                    out = [Point3D(float(p.x), float(p.y), float(p.z))
                           for p in raw]
                    if len(out) >= 2 and out[0].distanceTo(out[-1]) < 1e-9:
                        out = out[:-1]
                    if len(out) >= 3:
                        out.append(Point3D(out[0].x, out[0].y, out[0].z))
                        return cls._capPoints(out, cls._MAX_PROFILE_PTS)
            # 兜底：控制点直连
            ip_getter = getattr(obj, "inputPoints", None)
            if callable(ip_getter):
                try:
                    raw = ip_getter()
                except Exception:
                    raw = None
                if raw and len(raw) >= 3:
                    out = [Point3D(float(p.x), float(p.y), float(p.z))
                           for p in raw]
                    if len(out) >= 2 and out[0].distanceTo(out[-1]) < 1e-9:
                        out = out[:-1]
                    if len(out) >= 3:
                        return out
        if t == ObjectType.CIRCLE:
            c, r = obj.center(), obj.radius()
            return [Point3D(c.x + r * math.cos(a * 2 * math.pi / 96),
                            c.y + r * math.sin(a * 2 * math.pi / 96), c.z)
                    for a in range(96)]
        if t == ObjectType.ARC:
            c, r = obj.center(), obj.radius()
            sa, ea = obj.startAngle(), obj.endAngle()
            span = ea - sa
            if abs(span) < 1e-9:
                span = 2 * math.pi
            return [Point3D(c.x + r * math.cos(sa + span * i / 96),
                            c.y + r * math.sin(sa + span * i / 96), c.z)
                    for i in range(97)]
        if t == ObjectType.ELLIPSE:
            c, a, b = obj.center(), obj.majorAxis(), obj.minorAxis()
            cr, sr = math.cos(obj.rotation()), math.sin(obj.rotation())
            out = []
            for i in range(96):
                th = i * 2 * math.pi / 96
                ex, ey = a * math.cos(th), b * math.sin(th)
                out.append(Point3D(c.x + ex * cr - ey * sr,
                                   c.y + ex * sr + ey * cr, c.z))
            return out
        # 自由曲线 / 多段曲线 / 折线
        pts = None
        getter = getattr(obj, "getSampledPoints", None)
        if callable(getter):
            try:
                pts = getter()
            except TypeError:
                try:
                    pts = getter(cls._MAX_PROFILE_PTS)
                except Exception:
                    pts = None
        if not pts:
            # 多段曲线（POLYCURVE）：拼接各子段点列
            segs = getattr(obj, "segments", None)
            if callable(segs):
                out = []
                for seg in segs():
                    sp = seg.get("points", [])
                    for p in sp:
                        out.append(Point3D(float(p[0]), float(p[1]),
                                           float(p[2]) if len(p) > 2 else 0.0))
                if out:
                    pts = out
        if not pts:
            conv = getattr(obj, "toPolycurve", None)
            poly = conv() if callable(conv) else None
            g = getattr(poly, "getSampledPoints", None) if poly is not None else None
            pts = g() if callable(g) else None
        if not pts:
            pg = getattr(obj, "points", None)
            if callable(pg):
                try:
                    pts = pg()
                except TypeError:
                    pts = None
        if not pts:
            return []
        out = [Point3D(float(p.x), float(p.y), float(p.z)) for p in pts]
        closed = len(out) >= 2 and out[0].distanceTo(out[-1]) < 1e-9
        if closed:
            out = out[:-1]
        # 控制点/采样点直连返回，不做任何抽稀（用户方案：原原本本拉伸）
        if closed and len(out) >= 3:
            out.append(Point3D(out[0].x, out[0].y, out[0].z))
        return out

    @staticmethod
    def _capPoints(pts: List[Point3D], maxN: int) -> List[Point3D]:
        """仅上限保护：点数超限时等间隔保留（不改变轮廓形状），
        不超限原样返回。不做弧长抽稀/关键点匹配等转换——
        样条/自由曲线轮廓与画布显示（getSampledPoints 同源）一致。"""
        n = len(pts)
        if n <= maxN:
            return pts
        step = (n - 1) / maxN
        return [pts[min(n - 1, int(round(i * step)))] for i in range(maxN)]

    @staticmethod
    def _thinByArcLength(pts: List[Point3D], maxN: int) -> List[Point3D]:
        """点数超限时按弧长均匀抽稀（闭合轮廓，含末段闭合边；保留首点）。"""
        n = len(pts)
        if n <= maxN:
            return pts
        cum = [0.0]
        for i in range(n):
            cum.append(cum[-1] + pts[i].distanceTo(pts[(i + 1) % n]))
        total = cum[n]
        if total < 1e-12:
            return pts[:maxN]
        step = total / maxN
        out, j = [pts[0]], 0
        for k in range(1, maxN):
            target = k * step
            while j < n - 1 and cum[j + 1] < target:
                j += 1
            a, b = pts[j], pts[(j + 1) % n]
            span = cum[j + 1] - cum[j]
            t = 0.0 if span < 1e-12 else (target - cum[j]) / span
            t = max(0.0, min(1.0, t))
            out.append(Point3D(a.x + (b.x - a.x) * t,
                               a.y + (b.y - a.y) * t,
                               a.z + (b.z - a.z) * t))
        return out

    # ---------------- 预览与执行 ----------------

    def _profileKind(self, obj) -> str:
        """轮廓类型 → BrepShape 轮廓重建方式。

        圆用解析圆（精确）；样条/自由曲线用平滑样条过控制点（与 2D 显示
        Catmull-Rom 同风格，外墙与轮廓一致）；椭圆 96 点过点样条；其余
        （折线/多段/矩形）顶点直连。"""
        t = obj.type()
        if t == ObjectType.CIRCLE:
            return "circle"
        if t == ObjectType.ELLIPSE:
            return "spline"
        if t in (ObjectType.CURVE, ObjectType.NURBS_CURVE):
            return "spline"
        if t == ObjectType.RECTANGLE:
            return "polygon"
        ip = getattr(obj, "inputPoints", None)
        if callable(ip):
            try:
                raw = ip()
                if raw and len(raw) >= 3:
                    return "spline"
            except Exception:
                pass
        return "polygon"

    def _activeGroups(self) -> List[tuple]:
        """当前生效的挤出分组：多组模式用 _previewGroups，否则单曲线。"""
        if self._previewGroups:
            return self._previewGroups
        if self._previewCurve is not None:
            return [(self._previewCurve, list(self._previewHoles))]
        return []

    def _buildOne(self, outer, holes) -> Optional[BRepObject]:
        """单组挤出体：外轮廓 + 孔 → build123d（BrepShape.fromExtrude）。"""
        if self._panel is None:
            return None
        profile = self._sampleOutline(outer)
        if len(profile) < 3:
            return None
        if not _profileIsHorizontal(profile):
            return None
        # 去掉首尾闭合点（_sampleOutline 为闭合判定保留的重合点）
        if profile[0].distanceTo(profile[-1]) < 1e-9:
            profile = profile[:-1]
        if len(profile) < 3:
            return None
        holes_pts = []
        hole_kinds = []
        for hobj in holes:
            hp = self._sampleOutline(hobj)
            if len(hp) < 3 or not _profileIsHorizontal(hp):
                return None
            if hp[0].distanceTo(hp[-1]) < 1e-9:
                hp = hp[:-1]
            if len(hp) < 3:
                return None
            holes_pts.append(hp)
            hole_kinds.append(self._profileKind(hobj))
        try:
            shape = BrepShape.fromExtrude(
                profile, self._panel.height(), self._panel.draftAngle(),
                holes_pts or None, self._profileKind(outer),
                hole_kinds or None)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._notify(f"挤出失败：{e}")
            return None
        brep = BRepObject()
        brep.setShape(shape)
        if outer.type() == ObjectType.CIRCLE:
            c = outer.center()
            brep.setSourceCircle({"cx": c.x, "cy": c.y, "cz": c.z,
                                  "radius": float(outer.radius())})
        return brep

    def _buildGroupBodies(self) -> List[BRepObject]:
        """所有分组的挤出体列表（每组 = 外轮廓 + 直接内层孔）。"""
        out = []
        for outer, holes in self._activeGroups():
            brep = self._buildOne(outer, holes)
            if brep is not None:
                out.append(brep)
        return out

    def _updatePreview(self) -> None:
        """按当前预览分组 + 面板参数重建预览实体（多组融合为一个预览体）。"""
        self._previewObj = None
        if not bdKernelReady():
            # 内核预热中跳过构建（悬停热路径绝不等待模块锁），就绪后
            # 定时补建（鼠标不动也能恢复预览）；只提示一次避免刷屏
            if not getattr(self, "_kernelWaitHinted", False):
                self._kernelWaitHinted = True
                err = bdKernelError()
                if err is not None:
                    self._notify("3D 内核不可用：%s（详见 crash.log）" % err)
                else:
                    self._notify("几何内核预热中，预览稍候自动出现（仅首次启动）")
            if not getattr(self, "_kernelRetryPending", False):
                self._kernelRetryPending = True
                try:
                    from PySide6.QtCore import QTimer
                    QTimer.singleShot(400, self._kernelRetryPreview)
                except Exception:
                    pass
            return
        bodies = self._buildGroupBodies()
        if len(bodies) == 1:
            self._previewObj = bodies[0]
        elif bodies:
            try:
                part = bodies[0].shape().part()
                for b in bodies[1:]:
                    part = part + b.shape().part()
                merged = BRepObject()
                merged.setShape(BrepShape.fromPart(part))
                self._previewObj = merged
            except Exception:
                self._previewObj = bodies[0]
        if self._manager is not None:
            self._manager.invalidatePreview()

    def _kernelRetryPreview(self) -> None:
        """内核预热完成后补建预览（期间鼠标未动也能恢复显示）。"""
        self._kernelRetryPending = False
        if bdKernelReady() and self._previewCurve is not None:
            self._updatePreview()

    def previewSolid(self):
        return self._previewObj

    def _extrudeBodies(self, bodies: List[BRepObject]) -> None:
        """挤出全部实体（一次 beginEdit/endEdit = 一次撤销；结果全选）。"""
        if self._doc is None or self._panel is None or not bodies:
            return
        doc = self._doc
        doc.beginEdit()
        try:
            for result in bodies:
                doc.addObject(result)
                doc.selectObject(result.id())
                # 首次渲染预热：立即构建三角剖分/特征边/面点缓存，
                # 避免用户第一次旋转时一次性承担数百毫秒计算
                try:
                    result.mesh()
                    result.topology()
                except Exception:
                    pass
            n = len(bodies)
            self._notify(f"已挤出 {n} 个实体（高度 "
                         f"{self._panel.height():.2f}，拔模 "
                         f"{self._panel.draftAngle():.1f}°）")
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._notify(f"挤出失败：{e}")
        finally:
            try:
                doc.endEdit()
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
            return ("点击闭合曲线选中；或多选曲线后启动 = 全部挤出"
                    "（嵌套按圆环），右键/确认挤出，Esc 结束")
        if self._previewGroups:
            return (f"多组挤出（{len(self._previewGroups)} 组）：调节参数后"
                    "右键/确认挤出（Esc 放弃，再 Esc 结束）")
        if self._previewHoles:
            return "多轮廓（外轮廓+孔）：调节参数后右键/确认挤出（Esc 放弃，再 Esc 结束）"
        return "调节参数后右键/确认挤出（Esc 放弃当前，再 Esc 结束命令）"

    def name(self) -> str:
        return "Extrude"

    def commandTitle(self) -> str:
        return "挤出"


def registerAll(manager) -> None:
    manager.register("Extrude", ExtrudeCommand)

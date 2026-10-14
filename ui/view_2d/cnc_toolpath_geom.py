"""CNC 刀路轨迹几何：把 ToolpathEntry 的指令序列转成可渲染的线段集合。

2D 画布叠加层与 3D 视口（cnc 模式）共用这份纯计算，保证两个视图轨迹一致。

坐标系约定：
- 加工平面 = XY，刀具深度沿 Z（G 代码语义）；
- 2D 画布把 Z 直接映射到屏幕 Y（y_screen = py + z）；
- 3D 视口把 (x, y, z) 作为世界坐标投影（深度沿 3D Z 轴，天然垂直）。

返回结构：
{
  "feed_h":  [(x,y,z), ...],          # 水平进给/圆弧折线（连续）
  "rapid_h": [(x,y,z), ...],          # 水平快速移动折线（连续）
  "feed_v":  [[(x,y,z),(x,y,z)], ...],# 垂直进给下刀（每段独立）
  "rapid_v": [[(x,y,z),(x,y,z)], ...],# 垂直快速（抬刀/快速下刀，每段独立）
  "arrows":  [(x, y, z, dx, dy, dz), ...],  # 整条路径只保留 2 个：
              # 第一个下刀箭头（dz<0）+ 最后一个抬刀箭头（dz>0），用于标识进刀/退刀
  "marks":   [(x, y), ...],           # 纯钻孔孔位（无水平进给时）
  "has_horizontal": bool,
}
"""
from __future__ import annotations

import math

_RAPID = ("rapid",)
_FEED = ("feed",)
_ARC_CW = "arc_cw"
_ARC_CCW = "arc_ccw"


def _norm3(dx, dy, dz):
    n = math.hypot(dx, dy, dz)
    if n < 1e-12:
        return 0.0, 1.0, 0.0
    return dx / n, dy / n, dz / n


def build_toolpath_geometry(entry):
    """把一条路径的指令流解析为可渲染线段集合（轨迹从世界中心 (0,0,0) 出发）。

    路径条目是固定数据：解析结果缓存挂在 entry 上，2D 场景重建 / 3D 每帧
    重绘都直接复用，不重复遍历指令流（模拟高亮按 simPosition 分段着色，
    不需要重建几何）。
    """
    tp = getattr(entry, "toolpaths", None)
    if tp is not None:
        cached = getattr(entry, "_geom_cache", None)
        if cached is not None and getattr(entry, "_geom_tp", None) is tp:
            return cached
    geom = _build_geometry(entry)
    if tp is not None:
        try:
            entry._geom_cache = geom
            entry._geom_tp = tp
        except Exception:
            pass
    return geom


def _build_geometry(entry):
    """（内部）指令流 → 渲染线段集合，无缓存。"""
    feed_h: list = []
    rapid_h: list = []
    feed_v: list = []
    rapid_v: list = []
    first_down: tuple = None   # 第一个下刀箭头（进刀）
    last_up: tuple = None      # 最后一个抬刀箭头（退刀）
    drill_marks: set = set()
    has_horizontal = False
    x = y = z = 0.0

    def _h_move(path, tx, ty):
        # 连续水平折线：首点或断点处 push 起点，否则只追加终点
        if not path or path[-1] != (x, y, z):
            path.append((x, y, z))
        path.append((tx, ty, z))

    for tp in entry.toolpaths:
        for inst in tp.instructions:
            ix, iy, iz = inst.x, inst.y, inst.z
            if ix is None and iy is None and iz is None:
                continue
            # ---- 圆弧（整圆起点=终点也要折线化；i/j 为圆心相对量） ----
            if inst.type in (_ARC_CW, _ARC_CCW):
                has_horizontal = True
                self_cx, self_cy = x + (inst.i or 0.0), y + (inst.j or 0.0)
                r = math.hypot(inst.i or 0.0, inst.j or 0.0)
                tx = x if ix is None else ix
                ty = y if iy is None else iy
                tz = z if iz is None else iz     # 圆弧画在目标深度（每圈自己一层）
                if r < 1e-9:
                    _h_move(feed_h, tx, ty)
                else:
                    a0 = math.atan2(y - self_cy, x - self_cx)
                    a1 = math.atan2(ty - self_cy, tx - self_cx)
                    sweep = a1 - a0
                    if inst.type == _ARC_CCW:
                        while sweep <= 0:      # 起点=终点（整圆）也补满 2π
                            sweep += 2 * math.pi
                    else:
                        while sweep >= 0:
                            sweep -= 2 * math.pi
                    n = max(8, min(96, int(abs(sweep) * r / 0.4) + 1))
                    if not feed_h or feed_h[-1] != (x, y, z):
                        feed_h.append((x, y, z))
                    for k in range(1, n + 1):
                        a = a0 + sweep * k / n
                        # 圆弧带 Z（螺旋）时每段 z 线性过渡 → 连续螺旋线
                        zk = tz if tz == z else z + (tz - z) * k / n
                        feed_h.append((self_cx + r * math.cos(a),
                                       self_cy + r * math.sin(a), zk))
                x, y = tx, ty
                if tz != z:
                    z = tz           # 螺旋圆弧已含 z 过渡，跳过下方垂直分支
                    continue
            # ---- 水平移动（x/y 相对当前位置变化；z 同时变化时记 3D 斜线） ----
            elif ix is not None and iy is not None and (ix != x or iy != y):
                diag = iz is not None and abs(iz - z) > 1e-9
                if inst.type in _RAPID:
                    if diag:
                        rapid_h.append((x, y, z))
                        rapid_h.append((ix, iy, iz))
                        z = iz
                    else:
                        _h_move(rapid_h, ix, iy)
                elif inst.type in _FEED:
                    has_horizontal = True
                    if diag:
                        feed_h.append((x, y, z))
                        feed_h.append((ix, iy, iz))
                        z = iz
                    else:
                        _h_move(feed_h, ix, iy)
                else:
                    if not feed_h or feed_h[-1] != (x, y, z):
                        feed_h.append((x, y, z))
                    feed_h.append((ix, iy, z))
                x, y = ix, iy
            # ---- 垂直移动（z 变化） ----
            if iz is not None and abs(iz - z) > 1e-9:
                px = ix if ix is not None else x
                py = iy if iy is not None else y
                seg = ((px, py, z), (px, py, iz))
                dx, dy, dz = _norm3(0.0, 0.0, iz - z)
                if iz > z:      # 抬刀
                    rapid_v.append(seg)
                    last_up = (px, py, iz, dx, dy, dz)
                else:           # 下刀
                    if inst.type in _FEED:
                        feed_v.append(seg)
                    else:
                        rapid_v.append(seg)
                    if first_down is None:
                        first_down = (px, py, iz, dx, dy, dz)
                    if inst.type in _FEED:
                        drill_marks.add((px, py))
                z = iz
    arrows = []
    if first_down is not None:
        arrows.append(first_down)
    if last_up is not None:
        arrows.append(last_up)

    # 渲染抽稀：路径过密（曲面精加工等）时对连续折线均匀抽样，
    # 控制总绘制点数，避免 2D/3D 视图重绘卡顿（不影响 G 代码与模拟）。
    def _decimate(polyline, max_pts):
        if len(polyline) <= max_pts:
            return polyline
        step = (len(polyline) - 1) / float(max_pts - 1)
        return [polyline[min(len(polyline) - 1, int(round(i * step)))]
                for i in range(max_pts)]

    _budget = 10000
    for _key in ("feed_h", "rapid_h"):
        _polys = feed_h if _key == "feed_h" else rapid_h
        if not _polys:
            continue
        _total = sum(len(p) for p in _polys)
        if _total > _budget:
            _ratio = _budget / float(_total)
            _new = [_decimate(p, max(4, int(len(p) * _ratio)))
                    for p in _polys]
            if _key == "feed_h":
                feed_h = _new
            else:
                rapid_h = _new

    # 预计算：路径包围盒（3D 最近距离粗筛用）+ feed_h 每条折线的累积长度
    # （模拟高亮分段用，避免每帧重新累计）。
    _bbox = None
    _all = list(feed_h) + list(rapid_h)
    _all += [p for seg in feed_v for p in seg]
    _all += [p for seg in rapid_v for p in seg]
    if _all:
        xs0 = [p[0] for p in _all]
        ys0 = [p[1] for p in _all]
        zs0 = [p[2] if len(p) > 2 else 0.0 for p in _all]
        _bbox = (min(xs0), min(ys0), min(zs0),
                 max(xs0), max(ys0), max(zs0))
    _feed_cum = [0.0]
    _acc = 0.0
    for i in range(len(feed_h) - 1):
        _a, _b = feed_h[i], feed_h[i + 1]
        _acc += math.sqrt((_b[0] - _a[0]) ** 2 + (_b[1] - _a[1]) ** 2
                          + ((_b[2] - _a[2]) ** 2
                             if len(_a) > 2 and len(_b) > 2 else 0.0))
        _feed_cum.append(_acc)

    return {
        "feed_h": feed_h,
        "rapid_h": rapid_h,
        "feed_v": feed_v,
        "rapid_v": rapid_v,
        "arrows": arrows,
        "marks": sorted(drill_marks),
        "has_horizontal": has_horizontal,
        "bbox": _bbox,
        "feed_cum": _feed_cum,
    }

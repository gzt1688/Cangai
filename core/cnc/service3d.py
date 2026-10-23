"""3 轴刀路生成：分层区域粗加工 / 曲面精加工 / 路径加工。

几何输入：CNC 空间模型（BREP/SOLID 三角网格、SURFACE 高度场），
        以及 2D 曲线（路径加工用，可投影到曲面）。
输出：pymillcam Toolpath（IRInstruction 序列），经 generate_ir_gcode 出 G 代码，
     与 2D 刀路共用画布渲染 / 实体模拟链路。

坐标系约定（与 2D 一致）：
- 加工平面 = XY，深度沿 Z（负方向向下）；
- 模型/毛坯在世界坐标中放置，毛坯中心在原点、顶面 z = thickness/2；
- G 代码从世界中心 (0,0,0) 出发。
"""
from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass, field

import numpy as np


# --------------------------------------------------------------------------- #
# 参数（与 cnc_dialogs.py 控件命名一致）
# --------------------------------------------------------------------------- #

@dataclass
class Rough3DParams:
    """分层区域粗加工（Z-level / 水线粗加工）参数。"""
    # 刀具
    tool_diameter: float = 3.0
    flute_count: int = 2
    spindle_rpm: int = 18000
    feed_xy: float = 1200.0
    feed_z: float = 300.0
    tool_number: int = 1
    # 走刀
    scan_mode: str = "offset"        # parallel=平行扫描 / offset=环切 / helical=螺旋
    direction: str = "climb"         # climb=顺铣 / conventional=逆铣
    scan_angle: float = 0.0          # 平行扫描角度 °
    stepover: float = 3.0            # 侧向步距 mm
    xy_allowance: float = 0.3        # XY 径向余量 mm
    z_allowance: float = 0.2         # Z 轴向余量 mm
    # 深度
    cut_depth: float = -5.0          # 目标深度（负，相对毛坯顶面向下）
    stepdown: float = 1.0            # 每层下刀量 mm
    multi_depth: bool = True
    # 自适应
    min_step: float = 1.0            # 最小步距 mm（凹腔收敛步距）
    max_step: float = 5.0            # 最大步距 mm（平面区步距）
    smooth: bool = False
    # 机床
    controller: str = "grbl"
    safe_height: float = 5.0
    clearance_plane: float = 3.0
    chord_tolerance: float = 0.02


@dataclass
class SurfaceFinishParams:
    """曲面精加工（平行扫描 / 水线精加工）参数。"""
    # 刀具（球头刀）
    tool_diameter: float = 3.0
    flute_count: int = 2
    spindle_rpm: int = 18000
    feed_xy: float = 1200.0
    feed_z: float = 300.0
    tool_number: int = 1
    # 走刀
    scan_mode: str = "parallel"      # parallel=平行扫描 / iso3d=水线精加工
    direction: str = "climb"
    scan_angle: float = 0.0
    step_mode: str = "stepover"      # stepover=指定行距 / scallop=指定残留高度
    stepover: float = 0.5            # 行距 mm
    scallop_height: float = 0.01     # 残留高度 mm
    finish_allowance: float = 0.0    # 精加工余量 mm（负=过切）
    # 投影与修剪
    proj: str = "z"                  # z=Z 轴投影 / normal=法向投影（球头半径补偿）
    trim: str = "none"               # none=不修剪 / boundary=按模型边界修剪
    smooth: bool = False
    # 深度范围（可选；0=自动取模型全高）
    z_top: float = 0.0
    z_bottom: float = 0.0
    # 机床
    controller: str = "grbl"
    safe_height: float = 5.0
    clearance_plane: float = 3.0
    chord_tolerance: float = 0.02


@dataclass
class PathMachineParams:
    """路径加工（沿曲线 / 投影曲线）参数。"""
    # 刀具
    tool_diameter: float = 3.0
    flute_count: int = 2
    spindle_rpm: int = 18000
    feed_xy: float = 1200.0
    feed_z: float = 300.0
    tool_number: int = 1
    # 走刀
    path_type: str = "along"         # along=沿曲线 / project=投影到曲面
    direction: str = "climb"
    offset_side: str = "on_line"     # outside=外 / inside=内 / on_line=在线
    stepover: float = 0.5            # 相邻走刀行距 mm
    finish_allowance: float = 0.0    # 精加工余量 mm
    # 深度
    cut_depth: float = -3.0          # 目标深度（负）
    stepdown: float = 1.0
    multi_depth: bool = True
    # 机床
    controller: str = "grbl"
    safe_height: float = 5.0
    clearance_plane: float = 3.0
    chord_tolerance: float = 0.02


# --------------------------------------------------------------------------- #
# 网格收集：BREP/SOLID 三角网格 + SURFACE 高度场 → 统一三角网格
# --------------------------------------------------------------------------- #

def collect_model_mesh(objs) -> dict | None:
    """把选中的模型对象合并为一个三角网格。

    支持 BREP/SOLID（mesh() → verts/tris）与 SURFACE（高度场规则网格）。
    返回 {"verts": (N,3), "tris": (M,3), "zmin": float, "zmax": float,
          "bbox": (x0,y0,x1,y1)}；没有有效模型返回 None。
    """
    verts_list: list[np.ndarray] = []
    tris_list: list[np.ndarray] = []
    offset = 0
    for o in objs:
        try:
            t = o.type()
        except Exception:
            continue
        try:
            from core.vector.geometry_types import ObjectType
            if t == ObjectType.SURFACE:
                m = _surface_mesh(o)
            elif t in (ObjectType.BREP, ObjectType.SOLID):
                m = _brep_mesh(o)
            else:
                continue
        except Exception:
            continue
        if m is None or len(m["tris"]) == 0:
            continue
        verts_list.append(m["verts"])
        tris_list.append(m["tris"] + offset)
        offset += len(m["verts"])
    if not verts_list:
        return None
    verts = np.concatenate(verts_list, axis=0)
    tris = np.concatenate(tris_list, axis=0)
    zmin = float(verts[:, 2].min())
    zmax = float(verts[:, 2].max())
    return {
        "verts": verts.astype(np.float64),
        "tris": tris.astype(np.int64),
        "zmin": zmin,
        "zmax": zmax,
        "bbox": (float(verts[:, 0].min()), float(verts[:, 1].min()),
                 float(verts[:, 0].max()), float(verts[:, 1].max())),
    }


def _brep_mesh(o) -> dict | None:
    mesh = o.mesh(0.05) if hasattr(o, "mesh") else None
    if mesh is None or not len(mesh.get("tris", [])):
        return None
    return {"verts": np.asarray(mesh["verts"], dtype=np.float64),
            "tris": np.asarray(mesh["tris"], dtype=np.int64)}


def _surface_mesh(o) -> dict | None:
    """SURFACE（浮雕模型）高度场 → 规则三角网格（世界坐标）。

    浮雕对象带有 local→world 放置仿射（placement），画布上移动/旋转/缩放后
    必须应用该矩阵，否则刀路位置与显示不一致。网格按加工分辨率抽稀，
    长边上限 256 格（约 13 万三角形），避免高分辨率浮雕生成百万级网格卡死。
    """
    hf = o.heightField()
    if hf is None or hf.ny < 2 or hf.nx < 2:
        return None
    # CNC 计算一律用真实高度场：zExaggeration 仅为显示参数（与 STL 导出
    # 同语义），乘进去会导致实际切深随显示状态变化（过切）。
    data = np.asarray(hf.data, dtype=np.float64)
    ny, nx = data.shape
    ox, oy = hf.origin
    sp = float(hf.spacing)
    # 抽稀：长边 ≤ 256 格（X/Y 各自抽样，步距各自放大，网格不变形）
    max_n = 256
    sy = sx = 1
    if nx > max_n:
        sx = max(1, int(nx / max_n))
    if ny > max_n:
        sy = max(1, int(ny / max_n))
    if sx > 1 or sy > 1:
        data = data[::sy, ::sx]
        ny, nx = data.shape
    spx, spy = sp * sx, sp * sy
    xs = ox + np.arange(nx) * spx
    ys = oy + np.arange(ny) * spy
    xx, yy = np.meshgrid(xs, ys)          # (ny, nx)
    zz = data
    # 应用 local→world 放置仿射（3×3：xy 旋转/平移 + z 缩放）
    try:
        M = np.asarray(o.placement(), dtype=np.float64)
        if M.shape != (3, 3):
            raise ValueError
        wx = M[0, 0] * xx + M[0, 1] * yy + M[0, 2]
        wy = M[1, 0] * xx + M[1, 1] * yy + M[1, 2]
        wz = M[2, 0] * xx + M[2, 1] * yy + M[2, 2] * zz
    except Exception:
        wx, wy, wz = xx, yy, zz
    verts = np.stack([wx.ravel(), wy.ravel(), wz.ravel()], axis=1)
    # 每个格子 2 个三角形（统一绕序：逆时针朝 +Z）
    rows, cols = ny - 1, nx - 1
    i0 = (np.arange(rows)[:, None] * nx + np.arange(cols)[None, :])
    a = i0.ravel()
    b = a + 1
    c = a + nx
    d = a + nx + 1
    t1 = np.stack([a, c, b], axis=1)
    t2 = np.stack([b, c, d], axis=1)
    tris = np.concatenate([t1, t2], axis=0)
    return {"verts": verts, "tris": tris}


# --------------------------------------------------------------------------- #
# 水线切片：三角网格与水平面求交 → 闭合多边形（shapely）
# --------------------------------------------------------------------------- #

def _tri_z_extent(mesh: dict):
    """每个三角形的 (zmin, zmax)；按 verts 数组身份缓存于 mesh（切片逐层复用）。

    同一模型的多次水线切片共享网格，逐层重复扫描 13 万三角形只为筛出跨层
    的一小部分（约 7 千）代价很高；缓存跨度后每层只做两次数组比较。
    """
    verts = mesh["verts"]
    cached = mesh.get("_triZ")
    if cached is not None and cached[0] is verts:
        return cached[1], cached[2]
    vz = verts[mesh["tris"]][:, :, 2]
    lo = vz.min(axis=1)
    hi = vz.max(axis=1)
    mesh["_triZ"] = (verts, lo, hi)
    return lo, hi


def mesh_waterline(mesh: dict, z: float,
                   min_area: float = 1e-6) -> list[list[tuple[float, float]]]:
    """取模型在高度 z 的水平截面轮廓（外边界+岛），返回多边形点列。

    每个三角形与平面相交得 0 或 2 个交点 → 线段（向量化批量求交）；
    所有线段经 _assemble_rings 组装成闭合多边形，滤除噪声小环。
    """
    verts, tris = mesh["verts"], mesh["tris"]
    z = float(z)
    # 用预缓存的三角形 z 跨度筛选跨层三角形（避免每层重扫全部三角形顶点）
    tzlo, tzhi = _tri_z_extent(mesh)
    idx = np.nonzero((tzlo <= z) & (tzhi >= z))[0]
    if len(idx) == 0:
        return []
    # 跨平面的三角形：对 3 条边批量求与 z 平面的交点（端点在 3 个顶点上环绕）
    vv = verts[tris[idx]]                                # (K,3,3)
    vn = np.roll(vv, -1, axis=1)                          # 每条边的终点
    za = vv[:, :, 2]
    zb = vn[:, :, 2]
    dz = zb - za
    with np.errstate(divide="ignore", invalid="ignore"):
        # dz=0 的水平边会得到 inf/nan，但这些位置随后被 hit 掩掉，不参与结果
        t = (z - za) / dz
        hx = vv[:, :, 0] + (vn[:, :, 0] - vv[:, :, 0]) * t
        hy = vv[:, :, 1] + (vn[:, :, 1] - vv[:, :, 1]) * t
    # 与 z 严格相交（不含水平边），且交点落在边内 → 命中
    hit = (np.abs(dz) >= 1e-12) & ((za - z) * (zb - z) <= 0.0)
    cnt = hit.sum(axis=1)
    ok = cnt >= 2
    if not ok.any():
        return []
    hx, hy, hit = hx[ok], hy[ok], hit[ok]
    # 取每条边序（0/1/2）中最早命中的两条 → 与原逐边 append 顺序一致
    rows = np.arange(len(hx))
    e1 = np.argmax(hit, axis=1)
    hit2 = hit.copy()
    hit2[rows, e1] = False
    e2 = np.argmax(hit2, axis=1)
    segs = np.empty((len(hx), 2, 2), dtype=np.float64)
    segs[:, 0, 0] = hx[rows, e1]
    segs[:, 0, 1] = hy[rows, e1]
    segs[:, 1, 0] = hx[rows, e2]
    segs[:, 1, 1] = hy[rows, e2]
    rings = _assemble_rings(segs)
    out: list[list[tuple[float, float]]] = []
    for r in rings:
        if len(r) < 3:
            continue
        area = abs(_ring_area(r))
        if area < min_area:
            continue
        out.append([(float(x), float(y)) for x, y in r])
    return out


def _cluster_endpoints(flat: np.ndarray):
    """端点按 1e-6 网格聚类为整数节点，返回 (nodes (N,2) f8, inv (2M,) i8)。

    把 (x,y)*1e6 折叠成一维 int64 键后 np.unique，比 np.unique(axis=0) 快约
    6×；键序与 (x,y) 字典序一致，故节点编号与 axis=0 版本完全相同。坐标范围
    过大可能溢出 int64 时（跨度乘积 ≥ 2^62）回退 axis=0。
    """
    kn = np.round(flat, 6)
    qi = np.rint(kn * 1e6).astype(np.int64)
    qmin = qi.min(axis=0)
    rng = qi.max(axis=0) - qmin + 1
    if int(rng[0]) * int(rng[1]) >= (1 << 62):
        nodes, inv = np.unique(kn, axis=0, return_inverse=True)
        return nodes, inv.reshape(-1).astype(np.int64)
    key = (qi[:, 0] - qmin[0]) * rng[1] + (qi[:, 1] - qmin[1])
    _uk, idx, inv = np.unique(key, return_index=True, return_inverse=True)
    return kn[idx], inv.reshape(-1).astype(np.int64)


def _assemble_rings(segments) -> list:
    """线段自组装成闭合环。

    端点按 1e-6 网格聚类为整数节点（一维键 np.unique 排序 → 确定性），邻接表
    存 (邻节点, 边 id)，走环时用 bytearray 标记已用边。无向边去重与自环“压
    两条”语义、邻接插入顺序均与原逐段 round+dict+线性扫描实现逐环等价。
    每条边走一步即被消费，故走环必然终止（无需计数保护）。
    """
    if len(segments) == 0:
        return []
    seg = np.asarray(segments, dtype=np.float64)
    if seg.ndim != 3 or seg.shape[1:] != (2, 2):
        return []
    nodes, inv = _cluster_endpoints(seg.reshape(-1, 2))
    u = inv[0::2]
    v = inv[1::2]
    n_node = len(nodes)
    # 无向边去重：键 = min*n_node + max（与 (min,max) 排序一致）；保留“首次出现
    # 顺序”，使邻接插入顺序与原逐段实现一致（原实现同端点对的线段会被折叠成
    # 一条，含零长自环；否则平面过顶点时重复边会各插一次形成重复顶点）。
    key = np.minimum(u, v) * n_node + np.maximum(u, v)
    _, first = np.unique(key, return_index=True)
    fi = first[np.argsort(first, kind="stable")]
    ea = u[fi].tolist()          # 保留原始段方向（决定 adj 键插入顺序）
    eb = v[fi].tolist()
    n_edge = len(ea)
    adj: dict = {}
    setdef = adj.setdefault
    for e in range(n_edge):
        a = ea[e]
        b = eb[e]
        setdef(a, []).append((b, e))
        setdef(b, []).append((a, e))
        if a == b:
            setdef(a, []).append((a, e))      # 自环：原实现压入两条
    used = bytearray(n_edge)
    rings: list = []
    for start in adj.keys():
        ring = [start]
        cur = start
        prev = -1
        while True:
            cand = None
            cand_any = None
            for e in adj[cur]:
                if e[0] == prev:
                    continue
                if cand_any is None:
                    cand_any = e
                if not used[e[1]]:
                    cand = e
                    break
            if cand is None:
                cand = cand_any
            if cand is None or used[cand[1]]:
                break
            used[cand[1]] = 1
            ring.append(cand[0])
            prev, cur = cur, cand[0]
            if cand[0] == start:
                break
        if len(ring) >= 4:
            rings.append(ring)
    nodepy = nodes.tolist()
    out: list = []
    seen: set = set()
    for r in rings:
        if r[0] == r[-1]:
            r = r[:-1]
        sig = frozenset(r)
        if sig in seen:
            continue
        seen.add(sig)
        if len(r) >= 3:
            out.append([(nodepy[i][0], nodepy[i][1]) for i in r])
    return out


def _pt(x: float, y: float):
    """shapely Point 快捷构造。"""
    from shapely.geometry import Point
    return Point(float(x), float(y))


def _rings_to_regions(rings: list) -> list:
    """水线环列表 → 带孔多边形列表（shapely）。

    mesh_waterline 返回平铺环（模型外边界与岛混在一起，无包含关系）。
    这里按包含深度分层：偶数深度=区域外边界，奇数深度=该区域的孔（岛）。
    岛内的岛（深度 2）重新成为区域，递归语义由奇偶自然给出。
    """
    try:
        from shapely.geometry import Polygon
    except Exception:
        return []
    polys = []
    for r in rings:
        if len(r) < 3:
            continue
        try:
            p = Polygon([(float(x), float(y)) for x, y in r])
            if not p.is_valid:
                p = p.buffer(0)
            if not p.is_empty and p.area > 1e-9:
                polys.append(p)
        except Exception:
            continue
    if not polys:
        return []
    # 包围盒预筛：a.contains(b) 的必要条件是 a 的 bbox 含 b 的 bbox，
    # 先比 bbox（纯元组比较）再调 shapely contains，跳过绝大多数无谓调用
    bounds = [p.bounds for p in polys]

    def _bbox_holds(inner_b, outer_b):
        return (inner_b[0] >= outer_b[0] and inner_b[1] >= outer_b[1]
                and inner_b[2] <= outer_b[2] and inner_b[3] <= outer_b[3])

    # 包含深度：被几个更大的环包含（面积排序后 O(n²)，n=环数，很小）
    order = sorted(range(len(polys)), key=lambda i: polys[i].area, reverse=True)
    depth = [0] * len(polys)
    for a in range(len(order)):
        ia = order[a]
        for b in range(a + 1, len(order)):
            ib = order[b]
            if not _bbox_holds(bounds[ib], bounds[ia]):
                continue
            try:
                if polys[ia].contains(polys[ib]):
                    depth[ib] += 1
            except Exception:
                continue
    outers = [i for i in range(len(polys)) if depth[i] % 2 == 0]
    holes = [i for i in range(len(polys)) if depth[i] % 2 == 1]
    # 每个孔挂到包含它的最小外边界上
    holes_of: dict[int, list] = {i: [] for i in outers}
    for hi in holes:
        host = None
        for oi in outers:
            if not _bbox_holds(bounds[hi], bounds[oi]):
                continue
            try:
                if polys[oi].contains(polys[hi]) and \
                        (host is None or polys[oi].area < polys[host].area):
                    host = oi
            except Exception:
                continue
        if host is not None:
            g = polys[hi]
            if g.geom_type == "Polygon":
                holes_of[host].append(g.exterior.coords)
            else:
                # 无效/自交环经 buffer(0) 可能变成 MultiPolygon（无 .exterior）：
                # 退回逐部件的边界作为孔，避免此处 AttributeError 崩溃
                for part in getattr(g, "geoms", ()):
                    holes_of[host].append(part.exterior.coords)
    regions = []
    for oi in outers:
        try:
            regions.append(Polygon(polys[oi].exterior.coords, holes_of[oi]))
        except Exception:
            regions.append(polys[oi])
    return regions


def _clip_to_boundary(rings: list, boundary: list):
    """水线截面环列表裁剪到用户闭合边界内（shapely 求交）。

    返回裁剪后的环点列列表；边界外无残留返回 None。求交失败返回原环。
    """
    if not rings or not boundary or len(boundary) < 3:
        return rings
    try:
        from shapely.geometry import Polygon
        from shapely.ops import unary_union
        b = Polygon([(float(x), float(y)) for x, y in boundary])
        if b.is_empty or b.area < 1e-9:
            return rings
        regs = _rings_to_regions(rings)
        if not regs:
            return None
        inter = unary_union(regs).intersection(b)
        if inter.is_empty:
            return None
        out: list = []
        geoms = inter.geoms if hasattr(inter, "geoms") else [inter]
        for g in geoms:
            if g.is_empty or g.geom_type != "Polygon":
                continue
            ext = [(float(x), float(y)) for x, y in g.exterior.coords]
            if len(ext) >= 4:
                out.append(ext[:-1] if ext[0] == ext[-1] else ext)
            for hole in g.interiors:
                hc = [(float(x), float(y)) for x, y in hole.coords]
                if len(hc) >= 4:
                    out.append(hc[:-1] if hc[0] == hc[-1] else hc)
        return out if out else None
    except Exception:
        return rings


def _poly_to_lists(poly) -> list:
    """shapely 多边形 → 可 pickle 的 [(外边界点列, [孔点列...])]。"""
    out = []
    try:
        geoms = poly.geoms if hasattr(poly, "geoms") else [poly]
        for g in geoms:
            if g.is_empty:
                continue
            ext = [(float(x), float(y)) for x, y in g.exterior.coords]
            holes = [[(float(x), float(y)) for x, y in h.coords]
                     for h in g.interiors]
            out.append((ext, holes))
    except Exception:
        return []
    return out


def _lists_to_poly(lists):
    """_poly_to_lists 的逆操作（进程池 worker 侧）。"""
    try:
        from shapely.geometry import Polygon
        from shapely.ops import unary_union
    except Exception:
        return None
    polys = []
    for ext, holes in lists:
        try:
            p = Polygon(ext, [h for h in holes if len(h) >= 4])
            if not p.is_valid:
                p = p.buffer(0)
            if not p.is_empty and p.area > 1e-9:
                polys.append(p)
        except Exception:
            continue
    if not polys:
        return None
    return polys[0] if len(polys) == 1 else unary_union(polys)


# --------------------------------------------------------------------------- #
# 曲面采样：任意 (x,y) 求表面高度与法线（网格分桶加速）
# --------------------------------------------------------------------------- #

class SurfaceSampler:
    """三角网格曲面采样器：点 (x,y) → 表面 z 与法向（重心插值）。"""

    def __init__(self, mesh: dict, bins: int = 128) -> None:
        self.verts = mesh["verts"]
        self.tris = mesh["tris"]
        x0, y0, x1, y1 = mesh["bbox"]
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1
        self.bins = max(8, bins)
        self._cell = max((x1 - x0) / self.bins, 1e-9), max((y1 - y0) / self.bins, 1e-9)
        self._table: dict[int, list[int]] = {}
        self._tri_bbox()
        self._vertex_normals()

    def _bin_key(self, bx: int, by: int) -> int:
        return by * self.bins + bx

    def _tri_bbox(self) -> None:
        v = self.verts
        t = self.tris
        mn = v[t].min(axis=1)   # (M,3)
        mx = v[t].max(axis=1)
        cw, ch = self._cell
        for i in range(len(t)):
            bx0 = max(0, int((mn[i, 0] - self.x0) / cw))
            bx1 = min(self.bins - 1, int((mx[i, 0] - self.x0) / cw))
            by0 = max(0, int((mn[i, 1] - self.y0) / ch))
            by1 = min(self.bins - 1, int((mx[i, 1] - self.y0) / ch))
            for bx in range(bx0, bx1 + 1):
                for by in range(by0, by1 + 1):
                    self._table.setdefault(self._bin_key(bx, by), []).append(i)

    def _vertex_normals(self) -> None:
        v = self.verts
        t = self.tris
        tri_n = np.cross(v[t[:, 1]] - v[t[:, 0]], v[t[:, 2]] - v[t[:, 0]])
        ln = np.linalg.norm(tri_n, axis=1)
        tri_n[ln > 1e-12] /= ln[ln > 1e-12][:, None]
        vn = np.zeros_like(v)
        np.add.at(vn, t[:, 0], tri_n)
        np.add.at(vn, t[:, 1], tri_n)
        np.add.at(vn, t[:, 2], tri_n)
        ln = np.linalg.norm(vn, axis=1)
        vn[ln > 1e-12] /= ln[ln > 1e-12][:, None]
        self._vn = vn
        self._tri_n = tri_n

    def _ray_hit(self, x: float, y: float, t_idx: int):
        """Z 方向射线与三角形求交，返回 (z, bary, tri_n)。"""
        i0, i1, i2 = self.tris[t_idx]
        a = self.verts[i0]
        b = self.verts[i1]
        c = self.verts[i2]
        # 重心坐标（XY 投影）
        denom = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
        if abs(denom) < 1e-15:
            return None
        wa = ((b[1] - c[1]) * (x - c[0]) + (c[0] - b[0]) * (y - c[1])) / denom
        wb = ((c[1] - a[1]) * (x - c[0]) + (a[0] - c[0]) * (y - c[1])) / denom
        wc = 1.0 - wa - wb
        if wa < -1e-6 or wb < -1e-6 or wc < -1e-6:
            return None
        z = wa * a[2] + wb * b[2] + wc * c[2]
        n = (wa * self._vn[i0] + wb * self._vn[i1] + wc * self._vn[i2])
        ln = math.sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2])
        if ln > 1e-12:
            n = n / ln
        return z, (wa, wb, wc), n

    def sample(self, x: float, y: float, default_z: float = 0.0):
        """(x,y) → (表面z, 法线或None)。无命中返回 (default_z, None)。"""
        if not (self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1):
            return default_z, None
        bx = int((x - self.x0) / self._cell[0])
        by = int((y - self.y0) / self._cell[1])
        bx = max(0, min(self.bins - 1, bx))
        by = max(0, min(self.bins - 1, by))
        cand = self._table.get(self._bin_key(bx, by))
        if not cand:
            return default_z, None
        best = None
        for ti in cand:
            hit = self._ray_hit(x, y, ti)
            if hit is None:
                continue
            if best is None or hit[0] > best[0]:
                best = hit
        if best is None:
            return default_z, None
        return float(best[0]), best[2]

    def sample_many(self, xs, ys, default_z: float = 0.0):
        """批量采样：输入点数组，返回 (zs, normals)。

        zs: np.ndarray（未命中处为 default_z）
        normals: list，未命中处为 None，其余为 (nx,ny,nz) tuple
        同 sample() 的语义（取最高交点），向量化加速。
        """
        n = len(xs)
        zs = np.full(n, default_z, dtype=np.float64)
        normals = [None] * n
        inb = ((xs >= self.x0) & (xs <= self.x1)
               & (ys >= self.y0) & (ys <= self.y1))
        if not np.any(inb):
            return zs, normals
        xs_in = np.asarray(xs, dtype=np.float64)[inb]
        ys_in = np.asarray(ys, dtype=np.float64)[inb]
        idx_in = np.nonzero(inb)[0]
        bx = np.clip(((xs_in - self.x0) / self._cell[0]).astype(np.int64),
                     0, self.bins - 1)
        by = np.clip(((ys_in - self.y0) / self._cell[1]).astype(np.int64),
                     0, self.bins - 1)
        groups: dict = {}
        for k in range(len(idx_in)):
            key = int(by[k]) * self.bins + int(bx[k])
            groups.setdefault(key, []).append(k)
        v = self.verts
        tr = self.tris
        vn = self._vn
        for key, ks in groups.items():
            cand = self._table.get(key)
            if not cand:
                continue
            # 小组（≤8 点）走 Python 快路径，避免 numpy 小数组开销
            if len(ks) <= 8:
                for k in ks:
                    gi = int(idx_in[k])
                    bx0 = float(xs_in[k])
                    by0 = float(ys_in[k])
                    best = None
                    for ti in cand:
                        hit = self._ray_hit(bx0, by0, ti)
                        if hit is None:
                            continue
                        if best is None or hit[0] > best[0]:
                            best = hit
                    if best is not None:
                        zs[gi] = float(best[0])
                        normals[gi] = (float(best[2][0]), float(best[2][1]),
                                       float(best[2][2]))
                continue
            xg = xs_in[ks]
            yg = ys_in[ks]
            m = len(ks)
            best_z = np.full(m, -np.inf)
            best_n = np.zeros((m, 3))
            for ti in cand:
                i0, i1, i2 = tr[ti]
                a = v[i0]
                b = v[i1]
                c = v[i2]
                denom = ((b[1] - c[1]) * (a[0] - c[0])
                         + (c[0] - b[0]) * (a[1] - c[1]))
                if abs(denom) < 1e-15:
                    continue
                wa = ((b[1] - c[1]) * (xg - c[0])
                      + (c[0] - b[0]) * (yg - c[1])) / denom
                wb = ((c[1] - a[1]) * (xg - c[0])
                      + (a[0] - c[0]) * (yg - c[1])) / denom
                wc = 1.0 - wa - wb
                inside = (wa >= -1e-6) & (wb >= -1e-6) & (wc >= -1e-6)
                if not np.any(inside):
                    continue
                z = wa * a[2] + wb * b[2] + wc * c[2]
                better = inside & (z > best_z)
                if np.any(better):
                    best_z[better] = z[better]
                    nrm = (wa[better, None] * vn[i0]
                           + wb[better, None] * vn[i1]
                           + wc[better, None] * vn[i2])
                    ln = np.linalg.norm(nrm, axis=1, keepdims=True)
                    nrm[ln[:, 0] > 1e-12] /= ln[ln[:, 0] > 1e-12]
                    best_n[better] = nrm
            hit = np.isfinite(best_z) & (best_z > -np.inf)
            for j in range(m):
                if hit[j]:
                    gi = int(idx_in[ks[j]])
                    zs[gi] = float(best_z[j])
                    normals[gi] = (float(best_n[j, 0]), float(best_n[j, 1]),
                                   float(best_n[j, 2]))
        return zs, normals


# --------------------------------------------------------------------------- #
# 刀尖点计算
# --------------------------------------------------------------------------- #

def _tip_point(px: float, py: float, surf_z: float,
               normal, r: float, proj: str) -> tuple[float, float, float]:
    """接触点 (px,py,surf_z) → 刀尖位置。

    proj="z"：刀尖在接触点正上方/下方偏移半径（球头刀 Z 向补偿，
             水平面精确、斜面略有欠切）。
    proj="normal"：沿表面法向缩进半径（球头刀三维补偿，斜面精确）。
    """
    if proj == "normal" and normal is not None:
        nx, ny, nz = float(normal[0]), float(normal[1]), float(normal[2])
        if nz < 0.0:
            nx, ny, nz = -nx, -ny, -nz   # 3 轴只加工朝上表面
        if nz > 1e-9:
            # 球头刀：球心 = 接触点 + 法向 * r（材料外侧）；
            # 刀尖（G 代码 Z，球底最低点）= 球心竖直下移 r。
            # 水平面（nz≈1）→ 刀尖 z = surf_z，不欠切也不过切。
            return (px + nx * r, py + ny * r, surf_z + nz * r - r)
    # 回退 / Z 投影：水平面近似，刀尖（球底）接触表面
    return (px, py, surf_z)


# --------------------------------------------------------------------------- #
# 刀路 IR 生成辅助
# --------------------------------------------------------------------------- #

def _ir3(inst_type, x=None, y=None, z=None, f=None, s=None, tool_number=None,
         comment=None):
    from pymillcam.engine.ir import IRInstruction
    return IRInstruction(type=inst_type, x=x, y=y, z=z, f=f, s=s,
                         tool_number=tool_number, comment=comment)


def _retract_h(p, z_top: float | None = None) -> float:
    """抬刀/安全高度：safe_height 语义是相对料面的安全距离，但 3D 链路的
    G 代码是世界坐标（毛坯顶面 z=thickness/2，可高于 safe_height）。
    统一取 max(safe_height, 已知最高面 + 1mm)，杜绝厚毛坯 G0 在料内横移。"""
    if z_top is None:
        return float(p.safe_height)
    return max(float(p.safe_height), float(z_top) + 1.0)


def _prefix3(p, op_name: str, z_top: float | None = None) -> list:
    from pymillcam.engine.ir import MoveType
    return [
        _ir3(MoveType.COMMENT, comment=op_name),
        _ir3(MoveType.TOOL_CHANGE, tool_number=p.tool_number),
        _ir3(MoveType.SPINDLE_ON, s=p.spindle_rpm),
        _ir3(MoveType.RAPID, z=_retract_h(p, z_top)),
    ]


def _suffix3(p, z_top: float | None = None) -> list:
    from pymillcam.engine.ir import MoveType
    return [
        _ir3(MoveType.RAPID, z=_retract_h(p, z_top)),
        _ir3(MoveType.RAPID, x=0.0, y=0.0),
    ]


def generate_3d_gcode(toolpaths, p) -> tuple[str, list]:
    """3D 刀路 → G 代码（与 2D 共用 Cangai 后置体系，用户自定义后置生效）。"""
    from core.cnc.posts import get_post as _cangai_get_post
    from core.cnc.service import _post_default_macros
    post = _cangai_get_post(p.controller)
    return post.post_program(toolpaths,
                             macros=_post_default_macros(p.controller)), toolpaths


# --------------------------------------------------------------------------- #
# 区域填充：环切 / 平行扫描（shapely 多边形含孔，刀具中心可行域）
# --------------------------------------------------------------------------- #

def _ring_area(poly) -> float:
    """多边形有向面积（正=CCW）。"""
    a = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return a / 2.0


def _reverse_ring(poly):
    return list(reversed(poly))


def _region_fill_rings(region, step: float, limit: int = 400) -> list:
    """区域（含孔）环切填充：逐次内缩 step 取边界环。

    返回 [(环点列, is_hole)]，按内缩次序（外→内）排列。
    is_hole=True 表示该环是区域的孔边（材料在环内侧，如模型岛壁）。
    shapely buffer(-d) 对含孔多边形：外边界内缩、孔外扩，语义正确。
    """
    out: list = []
    if step <= 1e-6:
        return out
    cur = region
    for _n in range(limit):
        if cur is None or cur.is_empty:
            break
        geoms = cur.geoms if hasattr(cur, "geoms") else [cur]
        progressed = False
        for g in geoms:
            if g.is_empty or g.area < step * step * 0.2:
                continue
            ext = [(float(x), float(y)) for x, y in g.exterior.coords]
            if len(ext) >= 4 and ext[0] == ext[-1]:
                ext = ext[:-1]
            if len(ext) >= 3:
                out.append((ext, False))
            for hole in g.interiors:
                hc = [(float(x), float(y)) for x, y in hole.coords]
                if len(hc) >= 4 and hc[0] == hc[-1]:
                    hc = hc[:-1]
                if len(hc) >= 3:
                    out.append((hc, True))
            progressed = True
        if not progressed:
            break
        try:
            cur = cur.buffer(-step, join_style=2)
        except Exception:
            try:
                cur = cur.buffer(-step)
            except Exception:
                break
    return out


def _zigzag_region(region, angle_deg: float, step: float) -> list:
    """区域（含孔）平行扫描填充：旋转区域 → 水平扫描线求交 → 转回世界系。

    返回折线段列表（每条是 [(x,y), (x,y)]，方向已蛇形交替）。
    孔（模型岛）由 shapely 求交自动避开，扫描线被孔打断成多段。
    """
    try:
        from shapely.geometry import LineString
        from shapely.affinity import rotate as _rotate
    except Exception:
        return []
    rad = math.radians(angle_deg)
    cr, sr = math.cos(rad), math.sin(rad)
    try:
        rot = _rotate(region, angle_deg, origin=(0, 0))
    except Exception:
        return []
    if rot.is_empty:
        return []
    x0, y0, x1, y1 = rot.bounds
    if y1 - y0 < 1e-9:
        return []

    def _inv(pt):
        x, y = pt
        return (x * cr + y * sr, -x * sr + y * cr)

    lines: list = []
    v = math.floor(y0 / step) * step
    k = 0
    while v <= y1 + 1e-9:
        try:
            inter = rot.intersection(
                LineString([(x0 - 1.0, v), (x1 + 1.0, v)]))
        except Exception:
            inter = None
        if inter is not None and not inter.is_empty:
            segs = []
            gs = inter.geoms if hasattr(inter, "geoms") else [inter]
            for g in gs:
                if g.geom_type == "LineString" and g.length > 1e-9:
                    cs = list(g.coords)
                    segs.append((cs[0], cs[-1]))
            segs.sort(key=lambda s: s[0][0])
            for j, s in enumerate(segs):
                fwd = (k + j) % 2 == 0
                a, b = (s[0], s[1]) if fwd else (s[1], s[0])
                lines.append([_inv(a), _inv(b)])
        v += step
        k += 1
    return lines


# --------------------------------------------------------------------------- #
# 1) 分层区域粗加工（水线 + 每层填充）
# --------------------------------------------------------------------------- #

def build_rough3d_toolpath(mesh: dict, p: Rough3DParams,
                           stock_top: float | None = None,
                           boundary: list | None = None,
                           progress=None,
                           stock_w: float | None = None,
                           stock_h: float | None = None) -> list:
    """分层区域粗加工（Z-level / 水线粗加工，正向浮雕语义）。

    每层 z 的清除区域 = 加工范围 − 模型水线截面 S(z) 外扩(刀半径+XY余量)：
    刀具只走"模型外部"（表面低于该层的区域），模型本体保留为阶梯状。
    加工范围：boundary（用户闭合边界）> 毛坯矩形 > 模型包围盒外扩。

    stock_top：毛坯顶面 z（无毛坯传 None → 用模型 zmax）。
    返回 [] 表示该深度范围内无任何可加工材料。
    progress(frac)->bool：进度回调，返回 False 取消。
    """
    from pymillcam.engine.ir import MoveType
    from pymillcam.engine.ir import Toolpath
    from shapely.geometry import Polygon

    if stock_top is None:
        z_top = mesh["zmax"] + p.z_allowance
    else:
        z_top = min(float(stock_top), mesh["zmax"] + p.z_allowance)
    target = z_top - abs(p.cut_depth)
    z_end = max(target, mesh["zmin"] + p.z_allowance)
    if z_end > z_top - 1e-9:
        z_end = z_top
    tool_r = p.tool_diameter / 2.0

    # 分层：首层即入料（z_top 只擦顶面，不生成整面空走），
    # 末层缺口超半层补一层、不足半层直接贴到 z_end。
    step = max(p.stepdown, 1e-3)
    layers: list[float] = []
    z = z_top - step
    guard = 0
    while z >= z_end - 1e-9 and guard < 500:
        layers.append(z)
        z -= step
        guard += 1
    if not layers:
        layers = [z_end]
    elif layers[-1] > z_end + 1e-9:
        if layers[-1] - z_end > 0.5 * step:
            layers.append(z_end)
        else:
            layers[-1] = z_end
    if stock_top is not None:
        # 毛坯顶以上的层全是空走，直接剔除
        layers = [lz for lz in layers if lz < float(stock_top) - 1e-6]
        if not layers:
            return []

    # 加工范围（可加工域，刀具中心可行区将在每层再减去保留区）
    mach_poly = None
    if boundary is not None and len(boundary) >= 3:
        try:
            mach_poly = Polygon([(float(x), float(y)) for x, y in boundary])
            if not mach_poly.is_valid:
                mach_poly = mach_poly.buffer(0)
        except Exception:
            mach_poly = None
    if (mach_poly is None or mach_poly.is_empty) and stock_w and stock_h:
        w2, h2 = float(stock_w) / 2.0, float(stock_h) / 2.0
        mach_poly = Polygon([(-w2, -h2), (w2, -h2), (w2, h2), (-w2, h2)])
    if mach_poly is None or mach_poly.is_empty:
        bx0, by0, bx1, by1 = mesh["bbox"]
        m = tool_r + p.xy_allowance + 2.0 * max(step, p.stepover) + 1.0
        mach_poly = Polygon([(bx0 - m, by0 - m), (bx1 + m, by0 - m),
                             (bx1 + m, by1 + m), (bx0 - m, by1 + m)])
    if mach_poly.is_empty or mach_poly.area < 1e-9:
        return []
    machinable = _poly_to_lists(mach_poly)
    if not machinable:
        return []

    # 抬刀高度必须高于所有切削层（层 Z 可能在 safe_height 之上）
    retract_h = _retract_h(p, z_top)

    insts = _prefix3(p, "Rough3D", z_top)
    layer_specs = []
    for lz in layers:
        layer_specs.append((lz, {
            "scan_mode": p.scan_mode, "tool_r": tool_r,
            "stepover": p.stepover, "xy_allowance": p.xy_allowance,
            "direction": p.direction, "scan_angle": p.scan_angle,
            "retract_h": retract_h, "feed_z": p.feed_z,
            "feed_xy": p.feed_xy, "machinable": machinable,
        }))
    results = _parallel_map(_rough_layer_worker, layer_specs,
                            progress=progress, mesh=mesh)
    if results is None:
        return None  # 已取消
    any_cut = False
    for r in results:
        if r is None:
            continue
        if r[1]:
            any_cut = True
        insts.extend(r[1])
    if not any_cut:
        return []

    insts.extend(_suffix3(p, z_top))
    return [Toolpath(operation_name="Rough3D",
                     tool_number=p.tool_number, instructions=insts)]


def build_surface_finish_toolpath(mesh: dict, p: SurfaceFinishParams,
                                   boundary: list | None = None,
                                   progress=None) -> list:
    """曲面精加工。

    parallel：沿 scan_angle 方向生成平行行距线，逐点投影到曲面（Z 轴投影
              或法向投影补偿球头半径），蛇形走刀。
    iso3d：水线精加工——按行距在模型高度上取等距水线，沿截面轮廓单圈走刀。
    spiral / helical：暂以 iso3d 水线近似（同层螺旋需曲面参数域，后续增强）。
    boundary：可选闭合边界——只在该闭合曲线内部精加工。
    """
    from pymillcam.engine.ir import MoveType
    from pymillcam.engine.ir import Toolpath

    tool_r = p.tool_diameter / 2.0
    insts = _prefix3(p, "SurfaceFinish", mesh["zmax"])
    if p.scan_mode in ("spiral", "helical"):
        p.scan_mode = "iso3d"

    if p.scan_mode == "iso3d":
        ok = _emit_waterline_finish(insts, mesh, p, tool_r, boundary, progress)
    else:
        ok = _emit_parallel_finish(insts, mesh, p, tool_r, boundary, progress)
    if ok is False:
        return None  # 已取消

    insts.extend(_suffix3(p, mesh["zmax"]))
    return [Toolpath(operation_name="SurfaceFinish",
                     tool_number=p.tool_number, instructions=insts)]


# --------------------------------------------------------------------------- #
# 多核并行基础（进程池；打包/frozen 或异常时自动回退单线程顺序执行）
# --------------------------------------------------------------------------- #

_GLOBAL: dict = {}


def _init_worker(mesh):
    """进程池 worker 初始化：每个子进程持有一份网格与采样器。"""
    _GLOBAL["mesh"] = mesh
    _GLOBAL["sampler"] = None


def _parallel_map(fn, items, progress=None, mesh=None):
    """多进程并行执行 fn((idx, item)) -> result。

    items：spec 列表；返回与 items 顺序一致的结果列表。
    progress(frac) -> bool：每完成一项回调一次，返回 False 中断（返回 None）。
    frozen 打包环境 / 启动失败 / 任务过少：自动回退单线程顺序执行。
    环境变量 CANGAI_CNC_MP=0 可强制单线程（看门狗/低内存场景）；
    spawn 子进程会重新 import __main__，入口脚本必须有
    if __name__ == "__main__": 守卫（main.py 已有）。
    """
    try:
        if getattr(sys, "frozen", False):
            raise RuntimeError("frozen 环境不启用多进程")
        if os.environ.get("CANGAI_CNC_MP", "1") != "1":
            raise RuntimeError("CANGAI_CNC_MP=0 禁用多进程")
        import multiprocessing as _mp
        n = min(_mp.cpu_count(), 8)
        if n < 2 or len(items) < 16:
            # 任务太少时进程池 spawn 开销大于并行收益，顺序执行
            raise RuntimeError("任务过少")
        ctx = _mp.get_context("spawn")
        with ctx.Pool(n, initializer=_init_worker, initargs=(mesh,)) as pool:
            total = len(items)
            out = [None] * total
            for i, r in pool.imap_unordered(fn, enumerate(items), chunksize=1):
                out[i] = r
                if progress is not None and not progress((i + 1) / total):
                    pool.terminate()
                    return None
            return out
    except Exception:
        if mesh is not None:
            _init_worker(mesh)
        out = []
        total = len(items)
        for i, it in enumerate(items):
            out.append(fn((i, it)))
            if progress is not None and not progress((i + 1) / total):
                return None
        return out


def _rough_layer_worker(args):
    """粗加工单层生成（进程池 worker）。args = (idx, (lz, spec dict))。

    本层清除区 = 加工范围 − 模型水线截面（含岛拓扑）外扩(刀半径+XY余量)。
    环/线走刀：入口优先与上一段末点在清除区内直线连接（层内不抬刀），
    连不上才抬到 retract_h（必高于所有切削层）重新定位——杜绝 G00 下扎。
    """
    from pymillcam.engine.ir import MoveType
    from shapely.geometry import LineString
    idx, item = args
    mesh = _GLOBAL["mesh"]
    lz, d = item
    insts: list = []
    region = _lists_to_poly(d["machinable"])
    if region is None or region.is_empty:
        return idx, insts
    tool_r = d["tool_r"]
    xy_allow = d["xy_allowance"]
    # 保留区 = 模型水线截面（岛→孔）外扩刀具半径+余量
    rings = mesh_waterline(mesh, lz)
    if rings:
        regs = _rings_to_regions(rings)
        if regs:
            from shapely.ops import unary_union
            try:
                keep = unary_union(regs).buffer(tool_r + xy_allow,
                                                join_style=2)
            except Exception:
                keep = unary_union(regs).buffer(tool_r + xy_allow)
            if not keep.is_empty:
                try:
                    region = region.difference(keep)
                except Exception:
                    pass
    if region.is_empty or region.area < 1e-9:
        return idx, insts

    lz = float(lz)
    feed_z = d["feed_z"]
    feed_xy = d["feed_xy"]
    retract_h = d["retract_h"]
    base_cw = (d["direction"] == "climb")
    prev_end = None   # 上一走刀段末点 (x,y)，层内可直线连接

    def _can_link(a, b):
        if a is None:
            return False
        try:
            return region.buffer(1e-6).contains(LineString([a, b]))
        except Exception:
            return False

    def _enter(x, y):
        nonlocal prev_end
        if _can_link(prev_end, (x, y)):
            insts.append(_ir3(MoveType.FEED, x=x, y=y, f=feed_xy))
        else:
            insts.append(_ir3(MoveType.RAPID, z=retract_h))
            insts.append(_ir3(MoveType.RAPID, x=x, y=y))
            insts.append(_ir3(MoveType.FEED, x=x, y=y, z=lz, f=feed_z))
        prev_end = (x, y)

    if d["scan_mode"] in ("offset", "helical"):
        for ring, is_hole in _region_fill_rings(region, d["stepover"]):
            if len(ring) < 3:
                continue
            # 绕向（M3 顺时针主轴，climb=材料在行进右侧）：
            # 区域外边界环材料在外 → CW；孔边环（模型岛壁）材料在内 → CCW。
            want_cw = base_cw if not is_hole else (not base_cw)
            if (_ring_area(ring) > 0) == want_cw:
                ring = _reverse_ring(ring)
            _enter(ring[0][0], ring[0][1])
            for (px, py) in ring[1:]:
                insts.append(_ir3(MoveType.FEED, x=px, y=py, f=feed_xy))
            # 环闭合：回到起点（Clipper/shapely 环隐含闭合，必须显式走回）
            insts.append(_ir3(MoveType.FEED, x=ring[0][0], y=ring[0][1],
                              f=feed_xy))
            prev_end = ring[0]
    else:
        for zz in _zigzag_region(region, d["scan_angle"], d["stepover"]):
            if len(zz) < 2:
                continue
            _enter(zz[0][0], zz[0][1])
            for (px, py) in zz[1:]:
                insts.append(_ir3(MoveType.FEED, x=px, y=py, f=feed_xy))
            prev_end = zz[-1]
    return idx, insts


def _finish_row_worker(args):
    """平行扫描精加工单行采样（进程池 worker）。
    spec = (umin, umax, v, rad, ds, tool_r, proj, bpoly_pts, allow)
    → 该行连续命中段列表 runs=[[pt,...],...]（刀尖 Z 已加精加工余量）。
    边界外/未采样的点把行拆成多段——发射端段间抬刀，绝不连续 FEED
    横穿空隙（否则以切削深度切过用户明确排除的区域）。
    """
    idx, item = args
    umin, umax, v, rad, ds, tool_r, proj, bpoly_pts, allow = item
    sm = _GLOBAL.get("sampler")
    if sm is None:
        sm = SurfaceSampler(_GLOBAL["mesh"])
        _GLOBAL["sampler"] = sm
    cr, sr = math.cos(rad), math.sin(rad)
    bpoly = None
    if bpoly_pts:
        try:
            from shapely.geometry import Polygon
            bp = Polygon([(float(x), float(y)) for x, y in bpoly_pts])
            if not bp.is_empty and bp.area > 1e-9:
                bpoly = bp
        except Exception:
            bpoly = None
    us = np.arange(umin, umax + ds * 0.5, ds)
    if len(us) == 0:
        return idx, []
    wx = cr * us + sr * v
    wy = -sr * us + cr * v
    keep = None
    if bpoly is not None:
        keep = np.array([bpoly.contains(_pt(float(x), float(y)))
                         for x, y in zip(wx, wy)])
    if len(us) == 0:
        return idx, []
    zs, nrml = sm.sample_many(wx, wy)
    runs: list = []
    cur: list = []

    def _flush():
        if len(cur) >= 2:
            runs.append(list(cur))   # 追加副本：cur 随后 clear 复用
        cur.clear()

    for j in range(len(us)):
        if keep is not None and not keep[j]:
            _flush()
            continue
        nrm = nrml[j]
        if nrm is None:
            _flush()
            continue
        tx, ty, tz = _tip_point(float(wx[j]), float(wy[j]),
                                float(zs[j]), nrm, tool_r, proj)
        cur.append((tx, ty, tz + allow))
    _flush()
    return idx, runs


def _emit_parallel_finish(insts, mesh, p, tool_r: float,
                           boundary: list | None = None,
                           progress=None) -> None:
    """平行扫描曲面精加工（boundary：闭合边界，只加工其内部）。

    progress(frac)->bool：进度回调（多进程/单线程统一驱动），返回 False 取消。
    """
    from pymillcam.engine.ir import MoveType
    retract_z = _retract_h(p, mesh["zmax"])
    x0, y0, x1, y1 = mesh["bbox"]
    bpoly = None
    if boundary is not None and len(boundary) >= 3:
        try:
            from shapely.geometry import Polygon
            bpoly = Polygon([(float(x), float(y)) for x, y in boundary])
        except Exception:
            bpoly = None
        if bpoly is not None and (bpoly.is_empty or bpoly.area < 1e-9):
            bpoly = None
    if bpoly is not None:
        x0, y0, x1, y1 = bpoly.bounds
    step = max(0.05, p.stepover)
    if p.step_mode == "scallop" and p.scallop_height > 1e-6:
        # 残留高度 → 行距（球头近似）：h = r - sqrt(r^2 - (s/2)^2)
        r_eff = max(tool_r, 1e-6)
        h = p.scallop_height
        if h < r_eff:
            step = 2.0 * math.sqrt(max(r_eff * r_eff - (r_eff - h) ** 2, 0.0))
        step = max(0.05, min(step, max(0.5, r_eff * 2.0)))
    rad = math.radians(p.scan_angle)
    cr, sr = math.cos(rad), math.sin(rad)

    def _rot(pt, inv=False):
        px, py = pt
        if inv:
            return (px * cr + py * sr, -px * sr + py * cr)
        return (px * cr - py * sr, px * sr + py * cr)

    # 模型投影区域 → 旋转后 v 范围
    vs = []
    for (a, b) in [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]:
        _u, vv = _rot((a, b))
        vs.append(vv)
    vmin, vmax = min(vs), max(vs)
    v0 = math.floor(vmin / step) * step

    # 行内采样间距（弦差控制；默认更稀疏，兼顾平滑与渲染性能）
    ds = max(0.3, p.chord_tolerance * 16.0)
    bpoly_pts = None
    if bpoly is not None:
        bpoly_pts = [(float(x), float(y)) for x, y in boundary]
    rows_spec: list = []
    k = 0
    while True:
        v = v0 + k * step
        if v > vmax + 1e-9:
            break
        # 该行 u 范围：模型 bbox 旋转后 u 上下界
        umin, umax = math.inf, -math.inf
        for (a, b) in [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]:
            u, _vv = _rot((a, b))
            umin = min(umin, u)
            umax = max(umax, u)
        if umax - umin < 1e-9:
            k += 1
            continue
        rows_spec.append((umin, umax, v, rad, ds, tool_r, p.proj,
                          bpoly_pts, p.finish_allowance))
        k += 1

    # 行采样（多核并行 / 自动回退顺序）
    results = _parallel_map(_finish_row_worker, rows_spec,
                            progress=progress, mesh=mesh)
    if results is None:
        return False  # 已取消
    rows = []
    for r in results:
        if r is None:
            continue
        runs = [rn for rn in r[1] if len(rn) >= 2]
        if runs:
            rows.append(runs)

    # 蛇形连接：偶数行正向、奇数行反向；行间直接平移过渡（不抬刀），
    # 只在第一行下刀，之后在同一加工深度连续往返扫描。
    # 行内有空隙（边界排除/未采样命中）时分段走刀：段间抬刀重定位，
    # 不再连续 FEED 横穿空隙——否则以切削深度切过用户明确排除的区域。
    for ri, runs in enumerate(rows):
        if len(runs) == 1:
            row = runs[0]
            if ri % 2 == 1:
                row = list(reversed(row))
            x0p, y0p, z0p = row[0]
            if ri == 0:
                insts.append(_ir3(MoveType.RAPID, z=retract_z))
                insts.append(_ir3(MoveType.RAPID, x=x0p, y=y0p))
                insts.append(_ir3(MoveType.FEED, x=x0p, y=y0p, z=z0p,
                                  f=p.feed_z))
            else:
                # 行尾 → 下一行首：保持当前深度直接平移（z 线性过渡）
                insts.append(_ir3(MoveType.FEED, x=x0p, y=y0p, z=z0p,
                                  f=p.feed_xy))
            for (qx, qy, qz) in row[1:]:
                insts.append(_ir3(MoveType.FEED, x=qx, y=qy, z=qz, f=p.feed_xy))
        else:
            ordered = list(reversed(runs)) if ri % 2 == 1 else runs
            for row in ordered:
                x0p, y0p, z0p = row[0]
                insts.append(_ir3(MoveType.RAPID, z=retract_z))
                insts.append(_ir3(MoveType.RAPID, x=x0p, y=y0p))
                insts.append(_ir3(MoveType.FEED, x=x0p, y=y0p, z=z0p,
                                  f=p.feed_z))
                for (qx, qy, qz) in row[1:]:
                    insts.append(_ir3(MoveType.FEED, x=qx, y=qy, z=qz,
                                      f=p.feed_xy))
    return True


def _emit_waterline_finish(insts, mesh, p, tool_r: float,
                           boundary: list | None = None,
                           progress=None) -> None:
    """水线精加工：按行距取等距高度层，沿每层截面轮廓单圈走刀。
    boundary：闭合边界——先裁剪水线截面再走刀。
    progress(frac)->bool：进度回调，返回 False 取消。
    """
    from pymillcam.engine.ir import MoveType
    z_top = p.z_top if p.z_top != 0.0 else mesh["zmax"]
    z_bot = p.z_bottom if p.z_bottom != 0.0 else mesh["zmin"]
    if z_top <= z_bot:
        z_top, z_bot = mesh["zmax"], mesh["zmin"]
    step = max(0.05, p.stepover)
    n = max(1, int(round((z_top - z_bot) / step)) + 1)
    zs = [z_top - (z_top - z_bot) * i / max(1, n - 1) for i in range(n)]
    ds = max(0.3, p.chord_tolerance * 16.0)
    allow = p.finish_allowance
    sampler = SurfaceSampler(mesh)  # 采样器整个函数只建一次
    for zi, z in enumerate(zs):
        if progress is not None and not progress((zi + 1) / max(1, len(zs))):
            return False  # 已取消
        polys = mesh_waterline(mesh, z)
        if not polys:
            continue
        if boundary is not None:
            clipped = _clip_to_boundary(polys, boundary)
            if clipped is None:
                continue
            polys = clipped
        # 外环（面积最大）沿轮廓精加工；岛环同样处理
        polys.sort(key=lambda rp: -abs(_ring_area(rp)))
        for ring in polys:
            # 法向补偿：沿轮廓按边长采样（XY），投影到曲面求刀尖
            edges: list = []
            for i in range(len(ring)):
                ax, ay = ring[i]
                bx, by = ring[(i + 1) % len(ring)]
                edge = math.hypot(bx - ax, by - ay)
                nseg = max(1, int(round(edge / ds)) + 1)
                edges.append((ax, ay, bx, by, nseg))
            tot = sum(e[4] for e in edges)
            if tot < 2:
                continue
            wxs = np.empty(tot, dtype=np.float64)
            wys = np.empty(tot, dtype=np.float64)
            pos = 0
            for (ax, ay, bx, by, nseg) in edges:
                for kk in range(nseg):
                    t = kk / nseg
                    wxs[pos] = ax + (bx - ax) * t
                    wys[pos] = ay + (by - ay) * t
                    pos += 1
            szs, nrml = sampler.sample_many(wxs, wys, default_z=z)
            pts = []
            for j in range(tot):
                if nrml[j] is None:
                    continue
                tx, ty, tz = _tip_point(float(wxs[j]), float(wys[j]),
                                        float(szs[j]), nrml[j], tool_r, p.proj)
                pts.append((tx, ty, tz + allow))
            if len(pts) < 2:
                continue
            pts.append(pts[0])   # 闭合：回到环起点
            px, py, pz = pts[0]
            insts.append(_ir3(MoveType.RAPID, z=_retract_h(p, z_top)))
            insts.append(_ir3(MoveType.RAPID, x=px, y=py))
            insts.append(_ir3(MoveType.FEED, x=px, y=py, z=pz, f=p.feed_z))
            for (qx, qy, qz) in pts[1:]:
                insts.append(_ir3(MoveType.FEED, x=qx, y=qy, z=qz, f=p.feed_xy))
    return True


def build_path_machine_toolpath(mesh: dict | None,
                                curve_pts: list,
                                p: PathMachineParams,
                                progress=None) -> list:
    """路径加工。

    path_type="along"：沿曲线（CNC 空间 2D 曲线）逐层走刀，z 由分层深度决定。
    path_type="project"：曲线 XY 投影到曲面（需 mesh），刀尖沿曲面起伏。
    偏移侧决定中心线基准，行距×走刀次数生成多行。
    """
    from pymillcam.engine.ir import MoveType
    from pymillcam.engine.ir import Toolpath

    if len(curve_pts) < 2:
        return []
    z_ref = float(mesh["zmax"]) if mesh is not None else 0.0
    insts = _prefix3(p, "PathMachine", z_ref)
    tool_r = p.tool_diameter / 2.0

    # 基准偏移量（刀具半径 + 精加工余量；在线=0）
    side = p.offset_side
    d0 = 0.0
    if side == "outside":
        d0 = tool_r + p.finish_allowance
    elif side == "inside":
        d0 = -(tool_r + p.finish_allowance)

    # 走刀次数：行距>0 且需要覆盖时生成多行（在线向两侧展开，单侧向外延展）
    passes = 1
    if p.stepover > 1e-6:
        if side == "on_line":
            passes = 3   # 中心 + 两侧
        else:
            passes = 2   # 基准 + 下一行
    offsets: list[float] = []
    if side == "on_line":
        offsets = [0.0]
        if passes > 1:
            offsets = [0.0, p.stepover, -p.stepover]
    else:
        offsets = [d0]
        for _k in range(passes - 1):
            last = offsets[-1]
            nxt = last + p.stepover if last >= 0 else last - p.stepover
            offsets.append(nxt)

    # 分层
    z_top = 0.0
    z_end = -abs(p.cut_depth)
    zs = [z_top]
    z = z_top - p.stepdown
    guard = 0
    while z > z_end - 1e-9 and guard < 200:
        zs.append(z)
        z -= p.stepdown
        guard += 1
    if zs[-1] > z_end + 1e-9:
        zs.append(z_end)

    sampler = SurfaceSampler(mesh) if mesh is not None else None
    for li, lz in enumerate(zs):
        if progress is not None and not progress((li + 1) / max(1, len(zs))):
            return None  # 已取消
        for off in offsets:
            base = _offset_curve_2d(curve_pts, off)
            if base is None or len(base) < 2:
                continue
            # 3D 化：along → z=lz；project → z=曲面（未命中点分段丢弃，
            # 不落 z=0 当假刀位点——正高度浮雕区会被切入）
            runs: list = []
            cur: list = []
            for (wx, wy) in base:
                if p.path_type == "project" and sampler is not None:
                    sz, nrm = sampler.sample(wx, wy)
                    if nrm is None:
                        if len(cur) >= 2:
                            runs.append(cur)
                        cur = []
                        continue
                    cur.append((wx, wy, sz))
                else:
                    cur.append((wx, wy, lz))
            if len(cur) >= 2:
                runs.append(cur)
            for pts3 in runs:
                px, py, pz = pts3[0]
                insts.append(_ir3(MoveType.RAPID, z=_retract_h(p, z_ref)))
                insts.append(_ir3(MoveType.RAPID, x=px, y=py))
                insts.append(_ir3(MoveType.FEED, x=px, y=py, z=pz, f=p.feed_z))
                for (qx, qy, qz) in pts3[1:]:
                    insts.append(_ir3(MoveType.FEED, x=qx, y=qy, z=qz,
                                      f=p.feed_xy))

    insts.extend(_suffix3(p, z_ref))
    return [Toolpath(operation_name="PathMachine",
                     tool_number=p.tool_number, instructions=insts)]


def _offset_curve_2d(curve_pts: list, offset: float):
    """2D 折线按行进方向偏移（左法向 +offset），无自交（clipper）。"""
    if abs(offset) < 1e-9:
        return list(curve_pts)
    try:
        from core.cnc.service import _offset_polyline
        return _offset_polyline(curve_pts, offset)
    except Exception:
        return list(curve_pts)

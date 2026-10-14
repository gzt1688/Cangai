"""轻量多边形实体（PolySolid）：自研 3D 内核核心数据结构。

替代 OpenCASCADE：实体 = 一组世界坐标多边形面（闭合流形三角化网格语义），
用顶点去重 + 耳切三角化做渲染网格，边/顶点拓扑由面推导。支持：
  - 三角化（任意简单多边形，耳切法，含凹多边形）
  - 拓扑（唯一边 / 顶点 / 逐面三角区间）
  - 面法向（Newell）、包围盒、平移/旋转/缩放
  - 序列化（面点列 JSON，紧凑）

面绕向约定：从实体外侧看逆时针（外法向由 Newell 法给出）。
"""
from __future__ import annotations
import math
from typing import Dict, List, Optional, Tuple

from .geometry_types import Point3D, BoundingBox3D

_VKEY_EPS = 1e-6


def _vkey(x: float, y: float, z: float) -> Tuple[float, float, float]:
    """顶点去重键：坐标四舍五入到 1e-6。"""
    return (round(x / _VKEY_EPS), round(y / _VKEY_EPS),
            round(z / _VKEY_EPS))


def polygonNormal(poly: List[Point3D]) -> Tuple[float, float, float]:
    """Newell 法求多边形单位法向（任意平面多边形，含凹）。"""
    n = [0.0, 0.0, 0.0]
    m = len(poly)
    for i in range(m):
        a, b = poly[i], poly[(i + 1) % m]
        n[0] += (a.y - b.y) * (a.z + b.z)
        n[1] += (a.z - b.z) * (a.x + b.x)
        n[2] += (a.x - b.x) * (a.y + b.y)
    ln = math.sqrt(n[0] * n[0] + n[1] * n[1] + n[2] * n[2])
    if ln < 1e-12:
        return (0.0, 0.0, 1.0)
    return (n[0] / ln, n[1] / ln, n[2] / ln)


def polygonCentroid(poly: List[Point3D]) -> Point3D:
    m = len(poly)
    return Point3D(sum(p.x for p in poly) / m,
                   sum(p.y for p in poly) / m,
                   sum(p.z for p in poly) / m)


def _triangulatePolygon(pts: List[Point3D]) -> List[Tuple[int, int, int]]:
    """耳切三角化：输入多边形点列（首尾不重复，≥3 点），输出三角形索引。

    支持凹多边形；退化（共线/面积为零）返回空列表。
    """
    n = len(pts)
    if n < 3:
        return []
    # 投影平面：取法向分量最大的轴做 2D 投影（数值稳定）
    nx, ny, nz = polygonNormal(pts)
    if abs(nz) >= abs(nx) and abs(nz) >= abs(ny):
        u = lambda p: p.x
        v = lambda p: p.y
    elif abs(nx) >= abs(ny):
        u = lambda p: p.y
        v = lambda p: p.z
    else:
        u = lambda p: p.x
        v = lambda p: p.z
    coords = [(u(p), v(p)) for p in pts]
    area2 = 0.0
    for i in range(n):
        x1, y1 = coords[i]
        x2, y2 = coords[(i + 1) % n]
        area2 += x1 * y2 - x2 * y1
    if abs(area2) < 1e-12:
        return []
    # 保证 2D 投影逆时针
    if area2 < 0:
        coords = list(reversed(coords))
    idx = list(range(n))

    def is_convex(i: int) -> bool:
        p0 = coords[idx[i - 1]]
        p1 = coords[idx[i]]
        p2 = coords[idx[(i + 1) % len(idx)]]
        return (p1[0] - p0[0]) * (p2[1] - p1[1]) \
            - (p1[1] - p0[1]) * (p2[0] - p1[0]) > 1e-12

    def point_in_tri(px: float, py: float,
                     a, b, c) -> bool:
        d1 = (b[0] - a[0]) * (py - a[1]) - (b[1] - a[1]) * (px - a[0])
        d2 = (c[0] - b[0]) * (py - b[1]) - (c[1] - b[1]) * (px - b[0])
        d3 = (a[0] - c[0]) * (py - c[1]) - (a[1] - c[1]) * (px - c[0])
        has_neg = (d1 < -1e-12) or (d2 < -1e-12) or (d3 < -1e-12)
        has_pos = (d1 > 1e-12) or (d2 > 1e-12) or (d3 > 1e-12)
        return not (has_neg and has_pos)

    out: List[Tuple[int, int, int]] = []
    guard = 0
    while len(idx) > 3 and guard < n * n:
        guard += 1
        ear = None
        m = len(idx)
        for i in range(m):
            if not is_convex(i):
                continue
            a = coords[idx[(i - 1) % m]]
            b = coords[idx[i]]
            c = coords[idx[(i + 1) % m]]
            ok = True
            for j in range(m):
                if j in ((i - 1) % m, i, (i + 1) % m):
                    continue
                pj = coords[idx[j]]
                # 桥接重复点：与耳三角形顶点重合，不构成遮挡（带孔封盖用）
                if (abs(pj[0] - a[0]) < 1e-9 and abs(pj[1] - a[1]) < 1e-9) or \
                   (abs(pj[0] - b[0]) < 1e-9 and abs(pj[1] - b[1]) < 1e-9) or \
                   (abs(pj[0] - c[0]) < 1e-9 and abs(pj[1] - c[1]) < 1e-9):
                    continue
                if point_in_tri(pj[0], pj[1], a, b, c):
                    ok = False
                    break
            if ok:
                ear = i
                break
        if ear is None:
            return []   # 无法三角化（自交/退化）
        m = len(idx)
        a = idx[(ear - 1) % m]
        b = idx[ear]
        c = idx[(ear + 1) % m]
        out.append((a, b, c))
        del idx[ear]
    if len(idx) == 3:
        out.append((idx[0], idx[1], idx[2]))
    return out


def _triangulateWithHoles(outer: List[Point3D],
                            holes: List[List[Point3D]]):
    """带孔多边形三角化（孔桥接 + 耳切）：外环与孔环逐点直连封盖。

    不做弧长重采样/起点对齐：外环/孔环就是用户画的控制点。
    孔环桥接进外环形成单连通多边形后交给 _triangulatePolygon 耳切，
    返回三角形坐标三元组（底面 z=0；顶面用相同拓扑的 top 轮廓另算）。
    """
    poly = [Point3D(p.x, p.y, p.z) for p in outer]
    if len(poly) < 3:
        return []
    # 外环强制 CCW（封盖绕向基准；渲染层 zdom 门再统一底面朝向）
    if _signedArea2D(poly) < 0.0:
        poly = list(reversed(poly))
    for hole in holes:
        if len(hole) < 3:
            continue
        h = [Point3D(p.x, p.y, p.z) for p in hole]
        # 孔环转 CW（与外环相反），桥接后单连通多边形不自交
        if _signedArea2D(h) > 0.0:
            h = list(reversed(h))
        # 最近桥接对：孔环顶点 ↔ 当前多边形顶点（孔在外环内部，安全）
        best = None
        for i, hp in enumerate(h):
            for j, pp in enumerate(poly):
                d = (hp.x - pp.x) ** 2 + (hp.y - pp.y) ** 2
                if best is None or d < best[0]:
                    best = (d, i, j)
        if best is None:
            continue
        _, hi, pj = best
        ring = [h[(hi + k) % len(h)] for k in range(len(h))]
        # 桥接：外环 pj 处断开，插入整个孔环（进入/离开孔环各一次）
        poly = (poly[:pj + 1] + ring + [ring[0], poly[pj]]
                + poly[pj + 1:])
    idx = _triangulatePolygon(poly)
    return [(poly[a], poly[b], poly[c]) for (a, b, c) in idx]


def _signedArea2D(pts: List[Point3D]) -> float:
    """XY 投影鞋带面积（>0 逆时针）。"""
    s = 0.0
    n = len(pts)
    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        s += a.x * b.y - b.x * a.y
    return s * 0.5


def _resampleClosed(poly: List[Point3D], n: int) -> List[Point3D]:
    """闭合多边形按弧长均匀重采样到 n 点（保留首点；点数一致或输入非法时原样返回）。

    用于带孔挤出：孔轮廓重采样到与外轮廓同点数，保证环带逐点对应。
    """
    m = len(poly)
    if m == n or m < 3 or n < 3:
        return list(poly)
    cum = [0.0]
    for i in range(m):
        cum.append(cum[-1] + poly[i].distanceTo(poly[(i + 1) % m]))
    total = cum[m]
    if total < 1e-12:
        return list(poly[:n])
    out, j = [poly[0]], 0
    for k in range(1, n):
        target = k * total / n
        while j < m - 1 and cum[j + 1] < target:
            j += 1
        a, b = poly[j], poly[(j + 1) % m]
        span = cum[j + 1] - cum[j]
        t = 0.0 if span < 1e-12 else (target - cum[j]) / span
        t = max(0.0, min(1.0, t))
        out.append(Point3D(a.x + (b.x - a.x) * t,
                           a.y + (b.y - a.y) * t,
                           a.z + (b.z - a.z) * t))
    return out


def _alignRingStart(ring: List[Point3D],
                    ref_dir: Tuple[float, float]) -> List[Point3D]:
    """旋转闭合环，使首点相对质心的方向最接近 ref_dir（外环首点方向）。

    用于孔环起点对齐外环起点：任意形状（圆/矩形/多边形）的孔环与外环
    若由不同起始点采样，直接逐点连接会导致环带三角形交叉错乱；按极角
    对齐后内外环逐点对应，环带、孔壁、外壁全部正确。
    """
    n = len(ring)
    if n < 3:
        return ring
    cx = sum(p.x for p in ring) / n
    cy = sum(p.y for p in ring) / n
    rx, ry = ref_dir
    rl = math.hypot(rx, ry) or 1e-12
    rx, ry = rx / rl, ry / rl
    best, bestd = 0, -1.0
    for k in range(n):
        dx, dy = ring[k].x - cx, ring[k].y - cy
        dl = math.hypot(dx, dy) or 1e-12
        d = (dx * rx + dy * ry) / dl
        if d > bestd:
            best, bestd = k, d
    return ring[best:] + ring[:best] if best else ring


def offsetTopPolyline(base: List[Point3D], height: float,
                      draftAngle: float) -> List[Point3D]:
    """挤出顶面轮廓：底面按拔模角逐顶点 miter 偏移 + Z 抬升。

    偏移量 = height × tan(角度)，沿角平分线方向；钳制在近似内切半径内，
    避免极端参数下顶面自交翻转。>0 顶面向内收，<0 向外扩。
    """
    n = len(base)
    if n < 3:
        return []
    z0 = sum(p.z for p in base) / n
    zt = z0 + height
    off = height * math.tan(math.radians(draftAngle))
    if abs(off) < 1e-12:
        return [Point3D(p.x, p.y, zt) for p in base]
    area = abs(_signedArea2D(base))
    perim = 0.0
    for i in range(n):
        a, b = base[i], base[(i + 1) % n]
        perim += math.hypot(b.x - a.x, b.y - a.y)
    inr = (2.0 * area / perim) if perim > 1e-12 else 0.0
    off = max(-0.98 * inr, min(0.98 * inr, off))
    sign = 1.0 if _signedArea2D(base) > 0.0 else -1.0   # 内侧法线方向随绕向翻转
    out: List[Point3D] = []
    for i in range(n):
        pv, v, nx_ = base[(i - 1) % n], base[i], base[(i + 1) % n]
        t1 = (v.x - pv.x, v.y - pv.y)
        t2 = (nx_.x - v.x, nx_.y - v.y)
        l1 = math.hypot(*t1) or 1e-12
        l2 = math.hypot(*t2) or 1e-12
        n1 = (-t1[1] / l1 * sign, t1[0] / l1 * sign)
        n2 = (-t2[1] / l2 * sign, t2[0] / l2 * sign)
        dot = n1[0] * n2[0] + n1[1] * n2[1]
        den = 1.0 + dot
        if den < 1e-6:   # 折返角：退化为出边法线
            mx, my = n2
        else:
            mx, my = (n1[0] + n2[0]) / den, (n1[1] + n2[1]) / den
        out.append(Point3D(v.x + mx * off, v.y + my * off, zt))
    return out


class PolySolid:
    """轻量多边形实体：顶点数组 + 多边形面（顶点索引环）。

    顶点用坐标去重共享（_key → index），保证相邻面沿边无缝拼接。
    面以环存储（首尾不重复）；面绕向按外法向逆时针约定。
    """

    def __init__(self) -> None:
        self._verts: List[Tuple[float, float, float]] = []
        self._vmap: Dict[Tuple, int] = {}
        self._faces: List[List[int]] = []

    def clone(self) -> "PolySolid":
        """深拷贝：BRepObject.clone/_objectFromSnapshot 的撤销快照与阵列
        克隆路径会调用，缺失会 AttributeError 中断整个快照循环。"""
        o = PolySolid()
        o._verts = list(self._verts)
        o._vmap = dict(self._vmap)
        o._faces = [list(r) for r in self._faces]
        return o

    # ---------------- 构建 ----------------

    @classmethod
    def fromFacePoints(cls, faces: List[List[Point3D]]) -> "PolySolid":
        """从面点列构建（每个面=多边形点列，世界坐标）。"""
        ps = cls()
        for poly in faces:
            if len(poly) < 3:
                continue
            ring = [ps._addVertex(p.x, p.y, p.z) for p in poly]
            ps._faces.append(ring)
        return ps

    def _addVertex(self, x: float, y: float, z: float) -> int:
        k = _vkey(x, y, z)
        i = self._vmap.get(k)
        if i is None:
            i = len(self._verts)
            self._verts.append((float(x), float(y), float(z)))
            self._vmap[k] = i
        return i

    @classmethod
    def extrude(cls, profile: List[Point3D], height: float,
                draftAngle: float = 0.0,
                holes: Optional[List[List[Point3D]]] = None) -> "PolySolid":
        """闭合水平轮廓沿 +Z 挤出成实体（棱柱/台体，带拔模角，支持孔）。

        profile: 水平闭合轮廓（世界坐标，通常同一 Z 平面，≥3 点）
        height: 挤出高度（>0）
        draftAngle: 拔模角（度）；>0 顶面向内收，<0 向外扩
        holes: 孔轮廓列表（每个 ≥3 点，位于 profile 内部、同一 Z 平面）。
               传入时挤出带孔实体（圆环等）：底/顶面按外轮廓-孔环带三角化，
               孔壁生成内侧面；孔轮廓按弧长重采样到与外轮廓同点数保证
               环带逐点对应。
        """
        n = len(profile)
        if n < 3 or height <= 0.0:
            raise ValueError("轮廓至少 3 点且高度 > 0")
        if holes:
            return cls._extrudeHollow(profile, float(height),
                                      float(draftAngle), holes)
        top = offsetTopPolyline(profile, float(height), float(draftAngle))
        faces: List[List[Point3D]] = [
            [Point3D(p.x, p.y, p.z) for p in profile],
            [Point3D(p.x, p.y, p.z) for p in top],
        ]
        for i in range(n):
            j = (i + 1) % n
            faces.append([profile[i], profile[j], top[j], top[i]])
        return cls.fromFacePoints(faces)

    @classmethod
    def _extrudeHollow(cls, profile: List[Point3D], height: float,
                       draftAngle: float,
                       holes: List[List[Point3D]]) -> "PolySolid":
        """带孔挤出（用户方案：曲线几个点就几个点直直拉伸 + 上下封盖）。

        外壁/孔壁：外环/孔环控制点逐段直连的竖直面，不做采样、抽稀、
        重采样、起点对齐等任何计算；底/顶面用带孔多边形三角化封盖
        （孔桥接 + 耳切，这是封盖唯一必要的计算）。各面朝向由渲染层
        "外向法向修正 + zdom 门"统一处理。
        """
        n = len(profile)
        # 外环规范化：强制 CCW（封盖三角化绕向基准）
        if _signedArea2D(profile) < 0.0:
            profile = list(reversed(profile))
        top = offsetTopPolyline(profile, height, draftAngle)
        faces: List[List[Point3D]] = []
        # 外壁：每段一个竖直四边形（控制点直连）
        for i in range(n):
            j = (i + 1) % n
            faces.append([profile[i], profile[j], top[j], top[i]])
        valid_holes: List[List[Point3D]] = []
        hole_tops: List[List[Point3D]] = []
        for hole in holes:
            if len(hole) < 3:
                continue
            valid_holes.append(list(hole))
            ht = offsetTopPolyline(hole, height, draftAngle)
            hole_tops.append(ht)
            m = len(hole)
            for i in range(m):
                j = (i + 1) % m
                # 孔壁：每段一个竖直四边形（逆序，渲染外向修正后朝外显示）
                faces.append([hole[i], ht[i], ht[j], hole[j]])
        if not faces:
            return cls.extrude(profile, height, draftAngle)
        # 底/顶封盖：带孔三角化（底用 profile、顶用 top 轮廓，绕向一致）
        tris_b = _triangulateWithHoles(profile, valid_holes)
        for (a, b, c) in tris_b:
            faces.append([a, b, c])
        tris_t = _triangulateWithHoles(top, hole_tops)
        for (a, b, c) in tris_t:
            faces.append([a, b, c])
        return cls.fromFacePoints(faces)

    def facePoints(self) -> List[List[Point3D]]:
        """面点列（世界坐标），供序列化/外部使用。"""
        out = []
        for ring in self._faces:
            out.append([Point3D(*self._verts[i]) for i in ring])
        return out

    def setFacePoints(self, faces: List[List[Point3D]]) -> None:
        self._verts = []
        self._vmap = {}
        self._faces = []
        for poly in faces:
            if len(poly) < 3:
                continue
            ring = [self._addVertex(p.x, p.y, p.z) for p in poly]
            self._faces.append(ring)

    # ---------------- 访问 ----------------

    def verts(self) -> List[Tuple[float, float, float]]:
        return list(self._verts)

    def faceRings(self) -> List[List[int]]:
        return [list(r) for r in self._faces]

    def faceCount(self) -> int:
        return len(self._faces)

    def vertexCount(self) -> int:
        return len(self._verts)

    def facePoint(self, fi: int, vi: int) -> Point3D:
        return Point3D(*self._verts[self._faces[fi][vi]])

    def facePointsAt(self, fi: int) -> List[Point3D]:
        return [Point3D(*self._verts[i]) for i in self._faces[fi]]

    def faceNormalAt(self, fi: int) -> Tuple[float, float, float]:
        return polygonNormal(self.facePointsAt(fi))

    def faceCentroid(self, fi: int) -> Point3D:
        return polygonCentroid(self.facePointsAt(fi))

    def isClosed(self) -> bool:
        """闭合判定：每个顶点被至少 2 个面共用，且每条边恰好 2 个面。"""
        edge_count: Dict[Tuple[int, int], int] = {}
        for ring in self._faces:
            m = len(ring)
            for i in range(m):
                a, b = ring[i], ring[(i + 1) % m]
                key = (a, b) if a < b else (b, a)
                edge_count[key] = edge_count.get(key, 0) + 1
        for c in edge_count.values():
            if c != 2:
                return False
        return True

    # ---------------- 三角化（渲染） ----------------

    def triangulate(self) -> Dict[str, object]:
        """渲染网格：verts/normals/tris/faceRanges。

        faceRanges: [(tri_start, tri_count, flipped)]，与面同序，区间落在
        tris 全局索引上；flipped=该面绕向与 3D 三角绕向相反（背面剔除/法向
        修正用）。
        """
        verts: List[Tuple[float, float, float]] = []
        normals: List[Tuple[float, float, float]] = []
        tris: List[Tuple[int, int, int]] = []
        ranges: List[Tuple[int, int, bool]] = []
        for ring in self._faces:
            pts = [Point3D(*self._verts[i]) for i in ring]
            nrm = polygonNormal(pts)
            tri_idx = _triangulatePolygon(pts)
            base = len(verts)
            for p in pts:
                verts.append((p.x, p.y, p.z))
                normals.append(nrm)
            flipped = False
            tri_start = len(tris)
            for (a, b, c) in tri_idx:
                # 校验 3D 绕向与外法向一致；不一致则翻转
                pa, pb, pc = pts[a], pts[b], pts[c]
                ux, uy, uz = (pb.x - pa.x, pb.y - pa.y, pb.z - pa.z)
                vx, vy, vz = (pc.x - pa.x, pc.y - pa.y, pc.z - pa.z)
                cx = uy * vz - uz * vy
                cy = uz * vx - ux * vz
                cz = ux * vy - uy * vx
                if cx * nrm[0] + cy * nrm[1] + cz * nrm[2] < 0:
                    tris.append((base + a, base + c, base + b))
                    flipped = True
                else:
                    tris.append((base + a, base + b, base + c))
            ranges.append((tri_start, len(tri_idx), flipped))
        import numpy as np
        return {
            "verts": np.array(verts, dtype=np.float64),
            "normals": np.array(normals, dtype=np.float64),
            "tris": np.array(tris, dtype=np.int64),
            "faceRanges": ranges,
        }

    # ---------------- 拓扑（边 / 顶点） ----------------

    def uniqueEdges(self) -> List[Tuple[int, int]]:
        """唯一边（顶点索引对，小索引在前）；顺序按首次出现。"""
        seen: Dict[Tuple[int, int], int] = {}
        out: List[Tuple[int, int]] = []
        for ring in self._faces:
            m = len(ring)
            for i in range(m):
                a, b = ring[i], ring[(i + 1) % m]
                key = (a, b) if a < b else (b, a)
                if key not in seen:
                    seen[key] = len(out)
                    out.append(key)
        return out

    def edgePolylines(self) -> List[List[Tuple[float, float, float]]]:
        """每条唯一边 → 世界坐标折线（直线段=2 点）。"""
        edges = self.uniqueEdges()
        out = []
        for a, b in edges:
            out.append([self._verts[a], self._verts[b]])
        return out

    def featureEdgePolylines(self, minAngleDeg: float = 12.0
                             ) -> List[List[Tuple[float, float, float]]]:
        """特征边折线：边两侧面法向夹角 > 阈值的折痕/棱边。

        三角剖分产生的共面/近平面对角线（夹角≈0）不视为特征边，
        曲面细分边（如 96 段圆柱相邻面夹角≈3.75°）也被滤除，
        只保留真正能表达实体轮廓的棱边，结构线干净整齐。
        """
        verts = self._verts
        faces = self._faces
        norms: List[Optional[Tuple[float, float, float]]] = []
        for ring in faces:
            m = len(ring)
            if m < 3:
                norms.append(None)
                continue
            a, b, c = verts[ring[0]], verts[ring[1]], verts[ring[2]]
            nx = (b[1] - a[1]) * (c[2] - a[2]) - (b[2] - a[2]) * (c[1] - a[1])
            ny = (b[2] - a[2]) * (c[0] - a[0]) - (b[0] - a[0]) * (c[2] - a[2])
            nz = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
            ln = math.sqrt(nx * nx + ny * ny + nz * nz)
            norms.append(None if ln < 1e-12
                         else (nx / ln, ny / ln, nz / ln))
        edge_faces: Dict[Tuple[int, int], List[int]] = {}
        for fi, ring in enumerate(faces):
            m = len(ring)
            for i in range(m):
                a, b = ring[i], ring[(i + 1) % m]
                key = (a, b) if a < b else (b, a)
                edge_faces.setdefault(key, []).append(fi)
        cos_min = math.cos(math.radians(minAngleDeg))
        out: List[List[Tuple[float, float, float]]] = []
        for (a, b), adj in edge_faces.items():
            if len(adj) < 2:
                out.append([verts[a], verts[b]])
                continue
            n0, n1 = norms[adj[0]], norms[adj[1]]
            if n0 is None or n1 is None:
                out.append([verts[a], verts[b]])
                continue
            d = n0[0] * n1[0] + n0[1] * n1[1] + n0[2] * n1[2]
            if d < cos_min:
                out.append([verts[a], verts[b]])
        return out

    def edgeEndpoints(self, ei: int) -> Tuple[int, int]:
        return self.uniqueEdges()[ei]

    def faceEdgeAdjacency(self, edge: Tuple[int, int]) -> List[int]:
        """包含该边的面索引列表（一般 2 个；流形边）。"""
        a, b = edge
        out = []
        for fi, ring in enumerate(self._faces):
            m = len(ring)
            for i in range(m):
                x, y = ring[i], ring[(i + 1) % m]
                if (x == a and y == b) or (x == b and y == a):
                    out.append(fi)
                    break
        return out

    def edgeLength(self, ei: int) -> float:
        a, b = self.uniqueEdges()[ei]
        p, q = self._verts[a], self._verts[b]
        return math.sqrt((q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2
                         + (q[2] - p[2]) ** 2)

    # ---------------- 变换 ----------------

    def translate(self, dx: float, dy: float, dz: float) -> None:
        self._verts = [(x + dx, y + dy, z + dz)
                       for x, y, z in self._verts]
        self._vmap = {}
        for i, (x, y, z) in enumerate(self._verts):
            self._vmap[_vkey(x, y, z)] = i

    def rotate(self, angle: float, ax: float, ay: float, az: float) -> None:
        ln = math.sqrt(ax * ax + ay * ay + az * az)
        if ln < 1e-12:
            return
        ax, ay, az = ax / ln, ay / ln, az / ln
        c, s = math.cos(angle), math.sin(angle)
        C = 1.0 - c
        out = []
        for x, y, z in self._verts:
            nx = (c + C * ax * ax) * x + (C * ax * ay - s * az) * y \
                + (C * ax * az + s * ay) * z
            ny = (C * ay * ax + s * az) * x + (c + C * ay * ay) * y \
                + (C * ay * az - s * ax) * z
            nz = (C * az * ax - s * ay) * x + (C * az * ay + s * ax) * y \
                + (c + C * az * az) * z
            out.append((nx, ny, nz))
        self._verts = out
        self._vmap = {}

    def scale(self, sx: float, sy: float, sz: float) -> None:
        self._verts = [(x * sx, y * sy, z * sz) for x, y, z in self._verts]
        self._vmap = {}

    # ---------------- 包围盒 ----------------

    def boundingBox(self) -> BoundingBox3D:
        bb = BoundingBox3D()
        for x, y, z in self._verts:
            bb.expand(Point3D(x, y, z))
        return bb

    # ---------------- 序列化 ----------------

    def serialize(self) -> dict:
        return {"faces": [[[p.x, p.y, p.z] for p in self.facePointsAt(fi)]
                          for fi in range(self.faceCount())]}

    @classmethod
    def deserialize(cls, data: dict) -> "PolySolid":
        ps = cls()
        for poly in data.get("faces", []):
            if len(poly) < 3:
                continue
            ring = [ps._addVertex(float(p[0]), float(p[1]), float(p[2]))
                    for p in poly]
            ps._faces.append(ring)
        return ps

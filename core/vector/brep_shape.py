# -*- coding: utf-8 -*-
"""BrepShape：build123d Part 适配层（PolySolid 同款接口子集）。

Cangai 3D 模块改用 build123d 0.11.1（OCCT 内核）后，把 Part 包装成
渲染/拾取/序列化所需的形状接口，替换自研 PolySolid：

- triangulate() → {verts, normals, tris, faceRanges}：逐 BRep Face 细分，
  顶点平滑法向（面积加权），与渲染层 z-buffer 光栅化约定完全一致；
  faceRanges 每面 (tri_start, tri_count, flipped=False)（tessellate 绕向
  已是 BRep 外法向，渲染不再反转）。
- featureEdgePolylines() → 真实 BRep 边折线（每边 33 段采样）：结构线
  干净整齐，不再是三角面内部边。
- facePoints() → 三角片列表（面填充绘制用）。
- verts/boundingBox/isClosed/translate/rotate/scale/serialize/deserialize。

build123d 延迟导入：首次使用时才加载（import ~3s 不阻塞软件启动）。
"""
from __future__ import annotations
import math
import os
import tempfile
from typing import Dict, List, Optional, Tuple

import numpy as np

from .geometry_types import Point3D, BoundingBox3D

_EDGE_SAMPLE = 33   # 每条特征边的折线段数


def _meshShadedNormals(verts, tris):
    """纯网格（STL 导入）的专业渲染准备，返回 (verts, tris, vn)。

    管线：阶梯焊接（大→小容差，坍缩保护）→ 一致绕向（BFS 翻转奇偶 +
    分量级定向 + 小面清理）→ 角加权分簇顶点法向（45° 硬边）。
    - 阶梯焊接：CAD 网格（顶点精确共享）小容差即净、几何全保真；扫描
      抖动网格自动降到能焊的档（抖动全吸，细小三角形少量损失换平滑）。
    - 坍缩保护必须存在：大容差会把细小三角形焊塌（模型缺角，用户实测）。
    """
    M0 = len(tris)
    Nv0 = len(verts)
    span0 = max(float(np.ptp(verts, axis=0).max()), 1e-9)
    # ---- 阶梯焊接（大→小，取首个坍缩 ≤0.2% 的档；都不行用最小档）----
    best = None
    try:
        from scipy.spatial import cKDTree
        for tf in (4e-3, 2e-3, 1e-3, 5e-4, 2e-4):
            tol = max(span0 * tf, 1e-9)
            vw, tw = verts, tris.copy()
            n2, m2 = Nv0, M0
            collapsed = 0
            pairs = cKDTree(vw).query_pairs(tol, output_type="ndarray")
            if len(pairs):
                parent = np.arange(n2)

                def _find(a):
                    while parent[a] != a:
                        parent[a] = parent[parent[a]]
                        a = parent[a]
                    return a

                for a, b in pairs:
                    ra, rb = _find(int(a)), _find(int(b))
                    if ra != rb:
                        parent[max(ra, rb)] = min(ra, rb)
                roots = np.array([_find(i) for i in range(n2)],
                                 dtype=np.int64)
                uniq, remap = np.unique(roots, return_inverse=True)
                # remap = "原顶点 → 新顶点"（长度=n2）；角点经 remap[tw]
                cnt = np.bincount(remap, minlength=len(uniq))
                sv = np.zeros((len(uniq), 3))
                np.add.at(sv, remap, vw)
                vw = sv / cnt[:, None]
                tw = remap[tw]
                good = ((tw[:, 0] != tw[:, 1]) & (tw[:, 1] != tw[:, 2])
                        & (tw[:, 0] != tw[:, 2]))
                collapsed = int((~good).sum())
                tw = tw[good]
                m2 = len(tw)
                n2 = len(vw)
            if best is None or collapsed <= best[0]:
                best = (collapsed, vw, tw)
            if collapsed <= max(20, int(0.002 * M0)):
                break   # 该档坍缩可接受：更大容差不再尝试
        _, verts, tris = best
    except Exception:
        pass
    M = len(tris)
    Nv = len(verts)
    if M == 0 or Nv == 0:
        vn = np.zeros((Nv, 3))
        vn[:, 2] = 1.0
        return verts, tris, vn
    v0, v1, v2 = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    g = np.cross(v1 - v0, v2 - v0)
    ln = np.linalg.norm(g, axis=1)
    med_ln = float(np.median(ln[ln > 1e-12])) if (ln > 1e-12).any() else 0.0
    sliver = ln < max(med_ln * 1e-9, 1e-15)
    lns = np.where(sliver, 1.0, ln)
    gn = g / lns[:, None]
    gn[sliver] = 0.0
    # ---- 一致绕向（索引式：焊接后共享边 = 相同顶点索引对）----
    e = tris[:, [[0, 1], [1, 2], [2, 0]]].reshape(-1, 2)
    ek = np.sort(e, axis=1)
    ekey = ek[:, 0].astype(np.int64) * (Nv + 1) + ek[:, 1]
    order = np.argsort(ekey, kind="stable")
    sk = ekey[order]
    gs = np.concatenate(([0], np.flatnonzero(sk[1:] != sk[:-1]) + 1))
    ge = np.concatenate((gs[1:], [len(sk)]))
    sz = ge - gs
    size2 = sz == 2
    p0 = order[gs[size2]]
    p1 = order[gs[size2] + 1]
    ta, tb = p0 // 3, p1 // 3
    ca, cb = p0 % 3, p1 % 3
    d0 = verts[tris[ta, (ca + 1) % 3]] - verts[tris[ta, ca]]
    d1 = verts[tris[tb, (cb + 1) % 3]] - verts[tris[tb, cb]]
    conflict = (d0 * d1).sum(axis=1) > 0.0
    rel = np.full(M, -1, dtype=np.int8)
    nc = int(conflict.sum())
    if nc:
        from scipy.sparse import csr_matrix as _csr
        crow = np.concatenate((ta[conflict], tb[conflict]))
        ccol = np.concatenate((tb[conflict], ta[conflict]))
        cd = np.concatenate((np.ones(nc, np.int8), np.ones(nc, np.int8)))
        Wc = _csr((cd, (crow, ccol)), shape=(M, M))
        while True:
            unknown = np.flatnonzero(rel < 0)
            if not unknown.size:
                break
            rel[unknown[0]] = 0
            frontier = np.array([unknown[0]], dtype=np.int64)
            while frontier.size:
                coo = Wc[frontier].tocoo()
                tri = frontier[coo.row].astype(np.int64)
                nb = coo.col.astype(np.int64)
                want = ((rel[tri] + coo.data) & 1).astype(np.int8)
                m = rel[nb] == -1
                if not m.any():
                    frontier = np.array([], dtype=np.int64)
                    break
                nb, want = nb[m], want[m]
                o = np.argsort(nb, kind="stable")
                nbs, ws = nb[o], want[o]
                first = np.concatenate(([True], nbs[1:] != nbs[:-1]))
                uniq = nbs[first]
                rel[uniq] = ws[first]
                frontier = uniq
    rel[rel < 0] = 0
    if (rel == 1).any():
        tris = np.where((rel == 1)[:, None], tris[:, ::-1], tris)
        v0, v1, v2 = (verts[tris[:, 0]], verts[tris[:, 1]],
                      verts[tris[:, 2]])
        g = np.cross(v1 - v0, v2 - v0)
        g[sliver] = 0.0
    # ---- 分量级全局朝向（孤立翻转块 BFS 摸不到）----
    from scipy.sparse import csr_matrix as _csr2
    from scipy.sparse.csgraph import connected_components as _cc2
    live_e = size2.copy()          # 已知配对边 = 活边（分量闭合判定用）
    F = _csr2((np.ones(len(ta)), (ta, tb)), shape=(M, M))
    ncomp, lab = _cc2(F, directed=False)
    wgt = np.where(sliver, 0.0, ln)
    zsum = np.bincount(lab, weights=g[:, 2] * wgt, minlength=ncomp)
    vol = np.einsum('ij,ij->i', verts[tris[:, 0]],
                    np.cross(verts[tris[:, 1]], verts[tris[:, 2]]))
    vsum = np.bincount(lab, weights=vol, minlength=ncomp)
    face_bad = np.zeros(M, dtype=bool)
    face_bad[np.flatnonzero(live_e & ~size2) // 3] = True
    closed_c = ~(np.bincount(lab, weights=face_bad.astype(float),
                             minlength=ncomp) > 0)
    inv_comp = np.where(closed_c, vsum < 0.0, zsum < 0.0)
    flip_faces = inv_comp[lab]
    if flip_faces.any():
        tris = np.where(flip_faces[:, None], tris[:, ::-1], tris)
        v0, v1, v2 = (verts[tris[:, 0]], verts[tris[:, 1]],
                      verts[tris[:, 2]])
        g = np.cross(v1 - v0, v2 - v0)
        g[sliver] = 0.0
    # ---- 收尾 1：**开放网格**主朝向为上时，下朝向小面积面 = 翻转残留
    # → 翻正。闭合网格（如有符号体积的实体）不做——底面朝下是真实几何，
    # 会把整个底面翻黑（用户实测罐底 100% 被误翻）。open_mesh = 面图
    # 存在未配对边。
    open_mesh = bool((~size2).any())
    if open_mesh and (g[:, 2] * wgt).sum() > 0.0:
        med_ar = float(np.median(ln[~sliver])) if (~sliver).any() else 0.0
        small_down = ((~sliver) & (g[:, 2] < 0.0)
                      & (ln <= med_ar * 8.0))
        if small_down.any():
            tris = np.where(small_down[:, None], tris[:, ::-1], tris)
            g[small_down] = -g[small_down]
    # ---- 收尾 2：与局部平均强反向的小面翻转（墙面反转三角）----
    gn = g / np.where(sliver, 1.0, np.maximum(ln, 1e-18))[:, None]
    gn[sliver] = 0.0
    acc2 = np.zeros((Nv, 3))
    c2 = np.zeros(Nv)
    np.add.at(acc2, tris.ravel(), np.repeat(gn, 3, axis=0))
    np.add.at(c2, tris.ravel(), 3.0)
    loc2 = acc2 / np.maximum(c2, 1)[:, None]
    ll2 = np.linalg.norm(loc2, axis=1)
    ll2[ll2 < 1e-12] = 1.0
    v0, v1, v2 = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
    g = np.cross(v1 - v0, v2 - v0)
    g[sliver] = 0.0
    gn = g / np.where(sliver, 1.0, np.maximum(ln, 1e-18))[:, None]
    gn[sliver] = 0.0
    d2f = (gn * loc2[tris[:, 0]]).sum(axis=1)
    med2 = float(np.median(ln[~sliver])) if (~sliver).any() else 0.0
    flip2 = (d2f < -0.25) & (ln <= med2 * 8.0) & (~sliver)
    if flip2.any():
        tris = np.where(flip2[:, None], tris[:, ::-1], tris)
        v0, v1, v2 = (verts[tris[:, 0]], verts[tris[:, 1]],
                      verts[tris[:, 2]])
        g = np.cross(v1 - v0, v2 - v0)
        g[sliver] = 0.0
        gn = g / np.where(sliver, 1.0, np.maximum(ln, 1e-18))[:, None]
        gn[sliver] = 0.0
    # ---- 分簇顶点法向（角加权，45° 硬边）----
    M = len(tris)
    ang = np.empty((M, 3))

    def _ang(pa, pb, pc):
        u = pb - pa
        w = pc - pa
        lu = np.linalg.norm(u, axis=1)
        lw = np.linalg.norm(w, axis=1)
        dd = (u * w).sum(axis=1) / np.maximum(lu * lw, 1e-18)
        return np.arccos(np.clip(dd, -1.0, 1.0))

    ang[:, 0] = _ang(v0, v1, v2)
    ang[:, 1] = _ang(v1, v0, v2)
    ang[:, 2] = _ang(v2, v0, v1)
    gcorner = gn[np.repeat(np.arange(M), 3)] * ang.reshape(-1, 1)  # (3M,3)
    tri_sum = gcorner.reshape(M, 3, 3).sum(axis=1)
    tl = np.linalg.norm(tri_sum, axis=1)
    tl[tl < 1e-18] = 1.0
    tri_n = tri_sum / tl[:, None]
    e = tris[:, [[0, 1], [1, 2], [2, 0]]].reshape(-1, 2)
    ek = np.sort(e, axis=1)
    ek_key = ek[:, 0].astype(np.int64) * (Nv + 1) + ek[:, 1]
    order = np.argsort(ek_key, kind="stable")
    sk = ek_key[order]
    gs = np.concatenate(([0], np.flatnonzero(sk[1:] != sk[:-1]) + 1))
    ge = np.concatenate((gs[1:], [len(sk)]))
    parent = list(range(M))

    def _find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    cos45 = math.cos(math.radians(45.0))
    for g0, g1 in zip(gs, ge):
        if g1 - g0 != 2:
            continue   # 开边界/非流形：不归簇（保守保硬边）
        ta = order[g0] // 3
        tb = order[g0 + 1] // 3
        if ta == tb:
            continue
        if tri_n[ta] @ tri_n[tb] < cos45:
            continue   # 硬棱
        ra, rb = _find(ta), _find(tb)
        if ra != rb:
            if ra > rb:
                ra, rb = rb, ra
            parent[rb] = ra
    roots = np.array([_find(i) for i in range(M)], dtype=np.int64)
    # 顶点按 (位置, 簇) 全展开：棱=拆分硬边，曲面=平滑
    T1 = M + 1
    corner_node = tris.reshape(-1)
    pair_key = corner_node * T1 + roots[np.repeat(np.arange(M), 3)]
    up, inv = np.unique(pair_key, return_inverse=True)
    inv = inv.reshape(-1)
    grp_node = up // T1
    new_verts = verts[grp_node]
    new_tris = np.arange(len(up), dtype=np.int64)[inv].reshape(M, 3)
    gsum = np.zeros((len(up), 3))
    np.add.at(gsum, inv, gcorner.reshape(-1, 3))
    nl = np.linalg.norm(gsum, axis=1)
    nl[nl < 1e-18] = 1.0
    vn = gsum / nl[:, None]
    zero = np.linalg.norm(vn, axis=1) < 1e-12
    if zero.any():
        vn[zero] = np.array([0.0, 0.0, 1.0])
    return new_verts, new_tris, vn

def _import_bd():
    """延迟导入 build123d（模块级 import 会扫字体，慢）。

    注意：后台预载线程仍在导入时，本调用会在 Python 模块锁上阻塞到
    导入完成——冷盘首次导入可达 4~30s。鼠标热路径（挤出预览等）应先
    用 :func:`bdKernelReady` 探测，未就绪时跳过构建，绝不冻结 UI。"""
    import sys
    src = os.path.join(os.path.dirname(__file__), "..", "..",
                       "deps", "build123d-0.11.1", "src")
    src = os.path.normpath(src)
    if src not in sys.path:
        sys.path.insert(0, src)
    import build123d as bd  # noqa: F401
    return bd


def bdKernelReady() -> bool:
    """build123d 是否已完成导入（探测不触发导入）。

    sys.modules 只在模块执行完毕后登记：True = import 即时返回可安全
    调用；False = 未开始或后台线程正在导入，此刻调 _import_bd 会阻塞
    主线程直到导入完成（"启动后立刻挤出卡死"的根因）。"""
    import sys
    return "build123d" in sys.modules


def _stagedImportBd():
    """分步导入 build123d：把一次 3~4s 的导入拆成几大步，每步之间
    time.sleep(0) 让出 GIL，后台线程执行时主线程事件循环照常跑，
    避免"等待光标冻结"。

    完成后效果等同 _import_bd()：模块已全部缓存，_import_bd() 立即
    返回。OCP 首个 pyd 的初始化（约 0.3~0.4s）无法再拆细，属偶发
    微卡，其余时间主线程基本流畅。"""
    import time
    import sys
    src = os.path.join(os.path.dirname(__file__), "..", "..",
                       "deps", "build123d-0.11.1", "src")
    src = os.path.normpath(src)
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        import OCP  # noqa: F401  包本体
        time.sleep(0.005)
        # build_enums 是最大的 Python 依赖链（含 geometry/topology
        # 大量类定义，约 2~3s），单独一步 + 让出，主线程期间可响应
        import build123d.build_enums  # noqa: F401
        time.sleep(0.005)
        # 顶层 import：内部其余子模块此时依赖已缓存，几乎零耗时
        import build123d  # noqa: F401
    except Exception:
        import traceback
        traceback.print_exc()


class BrepShape:
    """build123d Part 适配：实现 PolySolid 接口子集（渲染/拾取/序列化）。"""

    _MESH_TOL = 0.08         # 网格细分容差（世界单位）：0.3 太粗，曲面
                            # 三角形大，渲染成"全是三角面/镂空感"。0.08 下
                            # 典型挤出体 1~3 万三角形，GPU 渲染无压力。
    _MESH_ANGULAR = 0.05    # 角度容差（弧度 ~3°）：控制曲率细分

    def __init__(self, part=None):
        self._part = part
        self._meshTol = BrepShape._MESH_TOL
        self._meshAngular = BrepShape._MESH_ANGULAR
        self._meshCache: Optional[dict] = None
        self._topoCache: Optional[dict] = None
        self._edgesCache: Optional[list] = None
        self._polyCache: Optional[list] = None
        self._faceCache: Optional[list] = None
        # 纯网格 shape（全面无解析曲面，STL 导入）：triangulate 时置 True
        self._meshOnly = False
        # 纯网格 shape 的累积仿射（4×4，None=identity）：绕开 OCCT 变换
        # （见 _meshXfUpdate 注释），triangulate/boundingBox 读取时施加
        self._meshXf = None
        # 纯网格 shape 的本地三角化缓存（不含仿射；变换读取时施加，
        # 移动/缩放拖动每帧只做矩阵乘，不重跑管线）
        self._meshTriCache = None
        self._vertsCache: Optional[list] = None

    def setMeshQuality(self, tol: Optional[float] = None,
                       angular: Optional[float] = None) -> None:
        """预览等轻量场景用粗网格（三角面少 → 三角化/渲染快）。

        只影响本实例的网格细分，不清 BRep 拓扑；已缓存网格无效化。
        """
        if tol is not None:
            self._meshTol = float(tol)
        if angular is not None:
            self._meshAngular = float(angular)
        self._clearCache()

    # ---------------- 构造 ----------------

    @staticmethod
    def fromExtrude(profile, height, draft_angle=0.0,
                    holes=None, kind="polygon",
                    hole_kinds=None) -> "BrepShape":
        """水平闭合轮廓挤出 → BrepShape（build123d 内核）。

        profile/holes: List[Point3D]（世界坐标，同一水平面，≥3 点）
        height: 挤出高度；draft_angle: 拔模角（度）
        kind: 轮廓类型 "circle"/"ellipse"/"spline"/"polygon"（决定轮廓重建方式）
        hole_kinds: 孔轮廓对应类型列表（缺省与 kind 相同）
        """
        bd = _import_bd()
        # 点序归一：拔模路径的挤出方向取自轮廓点序绕向（Plane.z_dir），
        # 顺时针点列会向 −Z 挤出（法向翻转救不了放样分支）——外轮廓
        # 统一逆时针、孔统一顺时针
        profile = list(profile)
        if _signed_area_xy(profile) < 0:
            profile.reverse()
        if holes:
            holes = [list(hp) for hp in holes]
            for i, hp in enumerate(holes):
                if _signed_area_xy(hp) > 0:
                    hp.reverse()
        face = _pts_to_face(bd, profile, kind)
        if face is None:
            raise ValueError("轮廓无法构建")
        # 挤出方向必须恒为 +Z：顺时针点列的面法向朝 -Z，翻转面朝向
        if float(face.normal_at().Z) < 0:
            face = bd.Face(face.wrapped.Reversed())
        if holes:
            hks = hole_kinds or [kind] * len(holes)
            for hp, hk in zip(holes, hks):
                hf = _pts_to_face(bd, hp, hk)
                if hf is not None:
                    if float(hf.normal_at().Z) < 0:
                        hf = bd.Face(hf.wrapped.Reversed())
                    face = face - hf
        # 注意：build123d 的 taper 单位是「度」（其内部自做 radians 转换），
        # 直接传角度值；若先 radians 会被当成 0.17° 级微角度=拔模无效
        if abs(float(draft_angle)) > 1e-9:
            try:
                part = bd.extrude(face, amount=float(height),
                                  taper=float(draft_angle))
            except Exception:
                # DPrism 快路径对多顶点多边形/复杂轮廓抛
                # Standard_TypeMismatch——回退自建 offset+loft 放样
                part = _taper_loft_extrude(bd, face, float(height),
                                           float(draft_angle))
        else:
            part = bd.extrude(face, amount=float(height))
        return BrepShape(part)

    @staticmethod
    def fromPart(part) -> "BrepShape":
        return BrepShape(part)

    def part(self):
        return self._part

    # ---------------- 基础状态 ----------------

    def isClosed(self) -> bool:
        return self._part is not None

    def volume(self) -> float:
        if self._part is None:
            return 0.0
        return float(self._part.volume)

    # ---------------- 网格化（渲染 / 拾取共用缓存） ----------------

    def triangulate(self) -> Optional[dict]:
        """{verts, normals, tris, faceRanges} —— 与 PolySolid.triangulate 同构。

        verts: (N,3)；normals: (N,3) 顶点平滑法向（面积加权，朝外）；
        tris: (M,3)；faceRanges: [(tri_start, tri_count, flipped=False)] 与
        BRep 面同序（flipped=False：tessellate 绕向已是外法向，渲染不反转）。

        纯网格 shape：本地三角化（OCCT 读取+焊接+分簇，贵）缓存于
        `_meshTriCache`，实例仿射 `_meshXf` 在读取时廉价施加——移动/
        缩放拖动每帧只有 numpy 矩阵乘，不重跑三角化管线（拖动延迟的
        根因是变换清缓存→每帧全量重算）。
        """
        if self._part is None:
            return None
        if self._isMeshOnlyShape():
            if self._meshTriCache is None:
                self._triangulateLocal()
            return self._applyMeshXf(self._meshTriCache)
        if self._meshCache is not None:
            return self._meshCache
        self._triangulateLocal()
        return self._meshCache

    def _triangulateLocal(self) -> None:
        """三角化管线（不含实例仿射），结果存 _meshTriCache（纯网格）或
        _meshCache（B-Rep 面）。"""
        bd = _import_bd()
        verts_all: List[Tuple[float, float, float]] = []
        tris_all: List[Tuple[int, int, int]] = []
        ranges: List[Tuple[int, int, bool]] = []
        tri_start = 0
        tri_no_ref: List[bool] = []   # 无解析法向参考的三角形（回退启发式）
        n_faces = 0       # 实际产出三角形的面数（无法三角化的面不计）
        n_no_surf = 0     # 其中无解析曲面的面数（STL 网格导入面）
        try:
            faces = list(self._part.faces())
        except Exception:
            faces = []
        # 整体网格化（关键）：OCCT 对共享边只离散一次，相邻面的边界
        # 三角带逐点一致。此前逐面 face.tessellate() 各自离散共享边，
        # 顶缘处顶面与锥面三角带互相交叠 → 深度冲突 → 渲染出锯齿
        # 条纹（拔模侧面杂色，用户实测）。逐面路径仅作兜底保留。
        whole_meshed = False
        if self._part is not None:
            try:
                self._part.mesh(self._meshTol, self._meshAngular)
                from OCP.BRep import BRep_Tool
                from OCP.TopLoc import TopLoc_Location
                from OCP.TopAbs import TopAbs_Orientation
                whole_meshed = True
            except Exception:
                whole_meshed = False
        for face in faces:
            if whole_meshed:
                try:
                    loc = TopLoc_Location()
                    poly = BRep_Tool.Triangulation_s(face.wrapped, loc)
                    if poly is None or poly.NbNodes() < 3:
                        continue
                    trsf = loc.Transformation()
                    reverse = (face.wrapped.Orientation()
                               == TopAbs_Orientation.TopAbs_REVERSED)
                    vs = [bd.Vector(v.X(), v.Y(), v.Z())
                          for v in (poly.Node(i).Transformed(trsf)
                                    for i in range(1, poly.NbNodes() + 1))]
                    ts = [((t.Value(1) - 1, t.Value(3) - 1, t.Value(2) - 1)
                           if reverse else
                           (t.Value(1) - 1, t.Value(2) - 1, t.Value(3) - 1))
                          for t in poly.Triangles()]
                except Exception:
                    continue
            else:
                try:
                    vs, ts = face.tessellate(self._meshTol, self._meshAngular)
                except Exception:
                    continue
            # 无解析曲面的面（STL 网格导入的 face 等）：绕向即权威外法向
            # （STL 规范顶点逆时针=外法向），不能调 normal_at——无曲面时
            # OCCT 直接段错误（进程崩溃，try/except 挡不住）；也不能进
            # 下方质心兜底（凹形网格的正确法向会被误翻）。
            try:
                from OCP.BRep import BRep_Tool as _BTool
                has_surf = _BTool.Surface_s(face.wrapped) is not None
            except Exception:
                has_surf = True
            ok_ref = not has_surf
            n_faces += 1
            if not has_surf:
                n_no_surf += 1
            # 面定向修正：采样若干三角形，与"该三角形质心处的解析面法向"
            # （BRepGProp_Face 按面定向）逐个比对、多数表决后整面翻。
            # 不能只看首三角形——球面极点/圆柱接缝处的首三角形常是
            # 叉积≈0 的退化窄条，符号随机会把正确的面整面翻反。旧的
            # "质心启发式"对深凹槽会误判（底面在实体质心下方时朝上的
            # 法向被翻成朝下，渲染变暗显出三角斑），仅作无参考面兜底
            if has_surf and ts:
                try:
                    votes_ok = votes_bad = 0
                    step = max(1, len(ts) // 6)
                    for k in range(0, len(ts), step):
                        a, b, c = ts[k]
                        p0, p1, p2 = vs[a], vs[b], vs[c]
                        ux, uy, uz = (p1.X - p0.X, p1.Y - p0.Y,
                                      p1.Z - p0.Z)
                        wx, wy, wz = (p2.X - p0.X, p2.Y - p0.Y,
                                      p2.Z - p0.Z)
                        gx = uy * wz - uz * wy
                        gy = uz * wx - ux * wz
                        gz = ux * wy - uy * wx
                        if (gx * gx + gy * gy + gz * gz) < 1e-18:
                            continue
                        fcx = (p0.X + p1.X + p2.X) / 3.0
                        fcy = (p0.Y + p1.Y + p2.Y) / 3.0
                        fcz = (p0.Z + p1.Z + p2.Z) / 3.0
                        nn = face.normal_at((fcx, fcy, fcz))
                        d = (gx * nn.X + gy * nn.Y + gz * nn.Z)
                        if d >= 0.0:
                            votes_ok += 1
                        else:
                            votes_bad += 1
                        if votes_ok + votes_bad >= 6:
                            break
                    if votes_ok or votes_bad:
                        if votes_bad > votes_ok:
                            ts = [(t0, t2, t1) for (t0, t1, t2) in ts]
                        ok_ref = True
                except Exception:
                    ok_ref = False
            base = len(verts_all)
            verts_all.extend((float(v.X), float(v.Y), float(v.Z)) for v in vs)
            for tri in ts:
                tris_all.append((base + tri[0], base + tri[1],
                                 base + tri[2]))
                tri_no_ref.append(not ok_ref)
            ranges.append((tri_start, len(ts), False))
            tri_start += len(ts)
        if not verts_all or not tris_all:
            self._meshTriCache = {
                "verts": np.zeros((0, 3)), "normals": np.zeros((0, 3)),
                "tris": np.zeros((0, 3), dtype=np.int64),
                "faceRanges": ranges,
            }
            return
        verts = np.array(verts_all, dtype=np.float64)
        tris = np.array(tris_all, dtype=np.int64)
        # 三角形面法向（面积权重，未归一化）——焊接与顶点法向共用
        v0, v1, v2 = verts[tris[:, 0]], verts[tris[:, 1]], verts[tris[:, 2]]
        g = np.cross(v1 - v0, v2 - v0)
        gln = np.linalg.norm(g, axis=1)
        gln[gln < 1e-18] = 1.0
        gn = g / gln[:, None]
        # ---- 纯网格 shape（STL 导入）：按二面角分簇的顶点法向 ----
        # STL 整体是一张无曲面网格面，顶点已被按位置合并共享；直接累加
        # 面法向会把 90° 棱（罐口/沿口）两侧也平均 → 棱边一圈忽明忽暗的
        # 锯齿三角斑（用户实测）。改为：共享边二面角 ≤ 阈值的三角形并查
        # 集归簇，簇内平均法向；棱两侧分属不同簇 → 顶点按簇拆分（渲染
        # 即硬边），曲面区仍全平滑。
        mesh_shaded = False
        self._meshOnly = bool(n_faces) and n_no_surf == n_faces
        if n_faces and n_no_surf == n_faces:
            try:
                verts, tris, vn = _meshShadedNormals(verts, tris)
                mesh_shaded = True
            except Exception:
                mesh_shaded = False
        # ---- 特征边感知的顶点焊接 ----
        # 每张 B-Rep 面独立细分：面边界处顶点位置重复、法向割裂——
        # 直纹条带的每对对应边是一张独立面，圆/曲面的墙面渲染出明显
        # 棱线（用户实测）。按"共享边两侧三角形的局部二面角 ≤ 阈值 →
        # 焊接顶点、法向跨面累加"消除；棱线（顶缘等 90° 突变）保持锐利。
        # 注意判据必须是共享边两侧三角形的【局部】法向——面平均法向对
        # 曲面会失真（整圆锥面的径向分量相互抵消，平均法向 ≈ +Z，与
        # 顶盖平行，误判"平滑"并焊接顶缘 → 侧面明暗交替牙齿，用户实测）。
        try:
            from collections import defaultdict
            # 纯网格 shape：顶点已按位置共享、无跨面边界边，焊接是无效功，
            # 且要对全部边建桶，大网格内存/耗时不可控 → 跳过
            weld_ranges = [] if mesh_shaded else ranges
            buckets = defaultdict(list)
            # 面内边界边（只被本面一个三角形使用的边）按端点位置哈希，
            # 同时记录该边所属的一个三角形（局部二面角用）
            for fi, (s0, cnt, _) in enumerate(weld_ranges):
                ec = defaultdict(list)
                for ti, t in enumerate(tris[s0:s0 + cnt]):
                    for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
                        ec[(a, b) if a < b else (b, a)].append(s0 + ti)
                for (a, b), tl in ec.items():
                    if len(tl) != 1:
                        continue
                    ka = (round(verts[a][0], 5), round(verts[a][1], 5),
                          round(verts[a][2], 5))
                    kb = (round(verts[b][0], 5), round(verts[b][1], 5),
                          round(verts[b][2], 5))
                    key = (ka, kb) if ka < kb else (kb, ka)
                    buckets[key].append((fi, a, b, tl[0]))
            # 平滑共享边 → 并查集焊接顶点
            parent = list(range(len(verts)))

            def _find(x):
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            smooth_cos = math.cos(math.radians(30.0))
            for lst in buckets.values():
                if len(lst) != 2:
                    continue   # 非 2 面（开放边界/非流形）不焊
                fa, aa, ab, ta = lst[0]
                fb, ba, bb, tb = lst[1]
                if fa == fb:
                    continue
                if gn[ta] @ gn[tb] < smooth_cos:
                    continue   # 局部二面角 > 30°：特征棱，保持锐利
                # 端点按位置对齐：两面的网格边可能反向遍历共享边（流形
                # 邻接的常态），按桶内顺序直配会把对角端点焊在一起
                # （相距可达数十 mm，并查集链式传染 → 三角形顶点全面
                # 错位 → 渲染出横跨模腔的扇形膜，实测圆切模 733 个
                # 20mm+ 大三角）
                if (verts[aa][0] - verts[ba][0]) ** 2                         + (verts[aa][1] - verts[ba][1]) ** 2                         + (verts[aa][2] - verts[ba][2]) ** 2                         <= (verts[aa][0] - verts[bb][0]) ** 2                         + (verts[aa][1] - verts[bb][1]) ** 2                         + (verts[aa][2] - verts[bb][2]) ** 2:
                    ra, rb = _find(aa), _find(ba)
                    if ra != rb:
                        parent[ra] = rb
                    ra, rb = _find(ab), _find(bb)
                    if ra != rb:
                        parent[ra] = rb
                else:
                    ra, rb = _find(aa), _find(bb)
                    if ra != rb:
                        parent[ra] = rb
                    ra, rb = _find(ab), _find(ba)
                    if ra != rb:
                        parent[ra] = rb
            root_new = {}
            new_verts_list = []
            old2new = np.empty(len(verts), dtype=np.int64)
            for i in range(len(verts)):
                r = _find(i)
                ni = root_new.get(r)
                if ni is None:
                    ni = len(new_verts_list)
                    root_new[r] = ni
                    new_verts_list.append(verts[i])
                old2new[i] = ni
            verts = np.array(new_verts_list, dtype=np.float64)
            tris = old2new[tris]
        except Exception:
            pass
        # 顶点平滑法向：相邻三角形面法向（面积加权）累加后归一化
        # （焊接后同一位置的多面顶点共享索引 → 法向跨面平滑）
        if not mesh_shaded:
            vn = np.zeros_like(verts)
            np.add.at(vn, tris[:, 0], g)
            np.add.at(vn, tris[:, 1], g)
            np.add.at(vn, tris[:, 2], g)
            gl = np.linalg.norm(vn, axis=1)
            gl[gl < 1e-12] = 1.0
            vn = vn / gl[:, None]
        # 兜底（仅限拿不到解析法向参考的三角形）："质心方向"启发式 +
        # Z 主面限定。不能对所有三角形启用——深凹槽底面在实体质心
        # 下方，朝上的真实外法向会被误翻成朝下
        # 网格 STL（STL 导入）不进此兜底：①绕向修复已保证方向一致；
        # ②质心启发式是为凸 B-Rep 面设计的，对凹形模具会把凹腔内壁的
        # 正确法向误翻 → 墙面黑斑（用户截图实测）。tri_no_ref 对网格
        # 全为 True，必须显式排除。
        if any(tri_no_ref) and not self._meshOnly:
            try:
                mask = np.array(tri_no_ref, dtype=bool)
                cent = verts.mean(axis=0)
                fc = (v0 + v1 + v2) / 3.0
                dot = (g[:, 0] * (fc[:, 0] - cent[0])
                       + g[:, 1] * (fc[:, 1] - cent[1])
                       + g[:, 2] * (fc[:, 2] - cent[2]))
                gln = np.linalg.norm(g, axis=1)
                gln[gln < 1e-12] = 1.0
                zdom = np.abs(g[:, 2]) / gln > 0.5
                inward = ((dot / gln) < -0.3) & zdom & mask
                if inward.any():
                    for _k in np.where(inward)[0]:
                        for _iv in tris[_k]:
                            vn[_iv] = -vn[_iv]
            except Exception:
                pass
        if self._meshOnly:
            # 纯网格：本地三角化缓存（实例仿射在读取时施加，见 _applyMeshXf）
            self._meshTriCache = {
                "verts": verts,
                "normals": vn,
                "tris": tris,
                "faceRanges": ranges,
            }
        else:
            self._meshCache = {
                "verts": verts,
                "normals": vn,
                "tris": tris,
                "faceRanges": ranges,
            }

    def _applyMeshXf(self, base: dict) -> dict:
        """把实例仿射 `_meshXf` 施加到本地三角化缓存（廉价 numpy 运算）。

        顶点：仿射变换；法向：逆转置变换后归一化（仿射变换下表面法向
        的正确变换，旋转/平移/缩放都成立）。返回新 dict（不修改缓存）。"""
        if self._meshXf is None:
            return base
        m = self._meshXf
        lin = m[:3, :3]
        verts = base["verts"] @ lin.T + m[:3, 3]
        try:
            nrm = base["normals"] @ np.linalg.inv(lin).T
            ln = np.linalg.norm(nrm, axis=1)
            ln[ln < 1e-18] = 1.0
            nrm = nrm / ln[:, None]
        except Exception:
            nrm = base["normals"]
        return {"verts": verts, "normals": nrm, "tris": base["tris"],
                "faceRanges": base["faceRanges"]}

    def _clearCache(self) -> None:
        self._meshCache = None
        self._meshTriCache = None
        self._topoCache = None
        self._edgesCache = None
        self._polyCache = None
        self._faceCache = None
        self._vertsCache = None
        self._faceCache = None
        self._vertsCache = None

    def facePoints(self) -> List[List[Point3D]]:
        """全部三角片（面填充绘制用；与 triangulate 的 tris 一致）。

        基于 triangulate 缓存，结果再缓存（渲染每帧 topology 重查）。"""
        if self._faceCache is not None:
            return self._faceCache
        m = self.triangulate()
        if m is None:
            self._faceCache = []
            return self._faceCache
        verts, tris = m["verts"], m["tris"]
        out = [[Point3D(float(verts[i][0]), float(verts[i][1]),
                        float(verts[i][2])) for i in t] for t in tris]
        self._faceCache = out
        return out

    def triangles(self) -> np.ndarray:
        """(T,3,3) 世界坐标三角形。"""
        m = self.triangulate()
        if m is None or len(m["tris"]) == 0:
            return np.zeros((0, 3, 3), dtype=np.float64)
        return m["verts"][m["tris"]]

    # ---------------- 拓扑（边 / 顶点） ----------------

    def edges(self) -> List:
        """build123d Edge 列表（与 featureEdgePolylines 一一对应，seam 已滤）。"""
        if self._part is None:
            return []
        if self._edgesCache is None:
            self._edgesCache = [
                e for e, seam in zip(self._part.edges(), self._seamFlags())
                if not seam
            ]
        return self._edgesCache

    def _seamFlags(self) -> List[bool]:
        """每条 BRep 边是否 seam 接缝。

        圆柱/圆锥/球/环面的轴向接缝线是曲面自身的 seam 边（该边在该
        面内闭合），显示结构线会出现多余竖线；真实棱边（矩形柱竖棱、
        倒角斜面与柱面交界）不是 seam。直接复用 build123d 的
        Face.seams（内部 ShapeAnalysis_Edge.IsSeam，标准 OCC 判据）。"""
        if self._part is None:
            return []
        if self._topoCache is not None:
            return self._topoCache
        flags = []
        try:
            seam_set = set()
            for face in self._part.faces():
                for seam in face.seams:
                    seam_set.add(seam)
            for ed in self._part.edges():
                is_seam = any(
                    ed.wrapped.IsSame(s.wrapped) for s in seam_set)
                flags.append(is_seam)
        except Exception:
            flags = [False] * len(list(self._part.edges()))
        self._topoCache = flags
        return flags

    def featureEdgePolylines(self, minAngleDeg: float = 12.0
                             ) -> List[List[Tuple[float, float, float]]]:
        """结构线折线。纯网格 shape（STL 导入面，无 B-Rep 边）：从三角化
        提取特征边——二面角 > 阈值的棱 + 开边界边（线框模式/选中高亮用，
        用户实测网格实体线框空白）。

        阈值：网格实体用 **30°**（MeshLab 等专业工具默认）——扫描/导出
        网格的二面角噪声本身可达 ±10°，12° 会把每条边都判成特征边，
        选中态几十万条线渲染成密集散斑（用户实测）；真实棱线（刻线壁/
        台阶沿）通常 >30°。另按锐利程度截断 15 万条兜底。"""
        if self._isMeshOnlyShape():
            if self._polyCache is not None:
                base = self._polyCache
            else:
                m = self.triangulate()
                if m is None or len(m["tris"]) == 0:
                    self._polyCache = []
                    return []
                vv = m["verts"]
                tt = np.asarray(m["tris"], dtype=np.int64)
                # 面法向用**平滑后**的顶点法向均值（原始叉积法向带逐面
                # 噪声——抖动网格每条边都成 >12° "特征边"，选中态几十万
                # 条线画成密集散斑，用户实测）。平滑后只有真实棱线超阈。
                fn = vn_s = m["normals"][tt].mean(axis=1)
                ln = np.linalg.norm(fn, axis=1)
                ln[ln < 1e-18] = 1.0
                fn = fn / ln[:, None]
                # 边中点量化分组（与顶点索引无关）：抖动网格顶点常不共享
                # （抖动 > 焊接容差），索引式边分组会把每条边都判成开边界
                # → 全保留成散斑。按量化中点配对同一几何边的多条三角形边。
                e = tt[:, [[0, 1], [1, 2], [2, 0]]].reshape(-1, 2)
                elo, ehi = e.min(axis=1), e.max(axis=1)
                mid = (vv[elo] + vv[ehi]) * 0.5
                tol_e = max(float(np.ptp(vv, axis=0).max()) * 2e-3, 1e-9)
                # KDTree 半径配对 + 连通分量：量化取整会在格边界劈开同组
                # 边（抖动 ≈ 格宽时大量配对跨格 → 全被误判开边界）。
                # connected_components 向量化（Python 并查集 45 万条目
                # 要 5s+，实测）。
                try:
                    from scipy.spatial import cKDTree
                    from scipy.sparse import coo_matrix
                    from scipy.sparse.csgraph import connected_components
                    # 精确重复分组（同绕向修复；互最近邻在精确重合点上
                    # 平局，配对必失败——实测 49.5%）
                    pr = cKDTree(mid).query_pairs(1e-9,
                                                  output_type="ndarray")
                    if len(pr):
                        g2 = coo_matrix(
                            (np.ones(len(pr)), (pr[:, 0], pr[:, 1])),
                            shape=(len(mid), len(mid)))
                        _, gi = connected_components(g2, directed=False)
                    else:
                        gi = np.arange(len(mid), dtype=np.int64)
                except Exception:
                    gi = np.arange(len(mid), dtype=np.int64)
                order2 = np.argsort(gi, kind="stable")
                sg = gi[order2]
                gs = np.concatenate(([0], np.flatnonzero(sg[1:] != sg[:-1]) + 1))
                ge = np.concatenate((gs[1:], [len(sg)]))
                cos_min = math.cos(math.radians(30.0))   # 网格实体固定 30°
                t0 = order2[gs] // 3
                size2 = (ge - gs) == 2
                t1 = t0.copy()
                t1[size2] = order2[gs[size2] + 1] // 3
                dot = (fn[t0] * fn[t1]).sum(axis=1)
                open_e = (ge - gs) == 1
                nonman = (ge - gs) > 2
                dih = np.where(open_e | nonman, math.pi,
                               np.arccos(np.clip(dot, -1.0, 1.0)))
                keep = (dih > math.radians(30.0)) | open_e | nonman
                idx = np.nonzero(keep)[0]
                if len(idx) > 150000:      # 极端噪声网格兜底截断
                    order3 = idx[np.argsort(-dih[idx], kind="stable")[:150000]]
                else:
                    order3 = idx
                out = []
                for gi2 in order3:
                    ea = order2[gs[gi2]]
                    out.append((tuple(vv[elo[ea]]), tuple(vv[ehi[ea]])))
                self._polyCache = out
                base = out
            # 本地折线 → 实例仿射（变换每次重建拓扑时施加）
            if self._meshXf is None:
                return [list(p) for p in base]
            lin = self._meshXf[:3, :3]
            off = self._meshXf[:3, 3]
            res = []
            for poly in base:
                arr = np.asarray(poly, dtype=np.float64) @ lin.T + off
                res.append([tuple(p) for p in arr])
            return res
        if self._polyCache is not None:
            return self._polyCache
        out = []
        for e in self.edges():
            pts = []
            try:
                p0 = e @ 0.0
                p1 = e @ 1.0
                # 直线检测：中点是否在端点连线上（直边 2 点即可，
                # 样条挤出侧棱 180 条从 33 段 → 2 段，边绘制大提速）
                pm = e @ 0.5
                vx = p1.X - p0.X
                vy = p1.Y - p0.Y
                vz = p1.Z - p0.Z
                straight = True
                L2 = vx * vx + vy * vy + vz * vz
                if L2 <= 1e-16:
                    # 闭合曲线（圆/椭圆/样条环：起点=终点）：端点重合导致
                    # L2=0，必须按曲线采样，否则圆边退化成 2 个重合点，
                    # 边拾取/结构线绘制全部失效
                    straight = False
                elif L2 > 1e-16:
                    wx = pm.X - p0.X
                    wy = pm.Y - p0.Y
                    wz = pm.Z - p0.Z
                    crx = wy * vz - wz * vy
                    cry = wz * vx - wx * vz
                    crz = wx * vy - wy * vx
                    if (crx * crx + cry * cry + crz * crz) / L2 > 1e-9:
                        straight = False
                if straight:
                    pts = [(float(p0.X), float(p0.Y), float(p0.Z)),
                           (float(p1.X), float(p1.Y), float(p1.Z))]
                else:
                    for k in range(_EDGE_SAMPLE):
                        p = e @ (k / (_EDGE_SAMPLE - 1))
                        pts.append((float(p.X), float(p.Y), float(p.Z)))
            except Exception:
                continue
            if pts:
                out.append(pts)
        self._polyCache = out
        return out

    def edgePolylines(self) -> List[List[Tuple[float, float, float]]]:
        return self.featureEdgePolylines()

    def verts(self) -> List[Tuple[float, float, float]]:
        if self._vertsCache is not None:
            return self._vertsCache
        m = self.triangulate()
        if m is None:
            self._vertsCache = []
            return self._vertsCache
        out = [tuple(v) for v in m["verts"]]
        self._vertsCache = out
        return out

    # ---------------- 包围盒 ----------------

    def boundingBox(self) -> BoundingBox3D:
        if self._part is None:
            return BoundingBox3D()
        bb = self._part.bounding_box()
        corners = [(bb.min.X, bb.min.Y, bb.min.Z),
                   (bb.max.X, bb.max.Y, bb.max.Z)]
        # 纯网格 shape 的实例仿射：基准包围盒 8 角施加累积矩阵
        if self._meshXf is not None:
            cs = [(x, y, z)
                  for x in (bb.min.X, bb.max.X)
                  for y in (bb.min.Y, bb.max.Y)
                  for z in (bb.min.Z, bb.max.Z)]
            arr = (self._meshXf @ np.array(
                [(*c, 1.0) for c in cs]).T).T[:, :3]
            corners = [(arr[:, 0].min(), arr[:, 1].min(), arr[:, 2].min()),
                       (arr[:, 0].max(), arr[:, 1].max(), arr[:, 2].max())]
        (x0, y0, z0), (x1, y1, z1) = corners
        return BoundingBox3D(
            Point3D(float(x0), float(y0), float(z0)),
            Point3D(float(x1), float(y1), float(z1)),
        )

    # ---------------- 变换（不可变 Part → 生成新 Part） ----------------

    def translate(self, dx: float, dy: float, dz: float) -> None:
        if self._part is None:
            return
        m = np.eye(4)
        m[:3, 3] = (float(dx), float(dy), float(dz))
        if self._meshXfUpdate(m):
            return
        # transform=True 烘焙进几何。默认 moved(location) 只挂 location，
        # 后续 scale 会绕"location 位置"缩放并把几何/位置错位组合——
        # 实测 T(-60) 后 S(.5) 把 z∈[-60,60] 映成 [-120,-60]（应在
        # [-30,30]），模型被打到网格平面以下"消失"（尺寸缩放后看不见，
        # 用户实测）。烘焙后 location 恒为 identity，各变换语义可组合。
        self._part = self._part.translate((dx, dy, dz), transform=True)
        self._clearCache()

    def rotate(self, angle: float, ax: float, ay: float, az: float) -> None:
        if self._part is None:
            return
        # Rodrigues 旋转矩阵（绕过原点、方向 (ax,ay,az)，度）
        ln = math.sqrt(ax * ax + ay * ay + az * az)
        if ln < 1e-12:
            return
        ux, uy, uz = ax / ln, ay / ln, az / ln
        t = math.radians(angle)
        c, s = math.cos(t), math.sin(t)
        C = 1.0 - c
        m = np.array([
            [c + ux * ux * C, ux * uy * C - uz * s, ux * uz * C + uy * s, 0.0],
            [uy * ux * C + uz * s, c + uy * uy * C, uy * uz * C - ux * s, 0.0],
            [uz * ux * C - uy * s, uz * uy * C + ux * s, c + uz * uz * C, 0.0],
            [0.0, 0.0, 0.0, 1.0]])
        if self._meshXfUpdate(m):
            return
        bd = _import_bd()
        axis = bd.Axis((0.0, 0.0, 0.0), (ax, ay, az))
        # 同 translate：烘焙几何（默认挂 location，与 scale 组合会错位）
        self._part = self._part.rotate(axis, float(angle), transform=True)
        self._clearCache()

    def scale(self, sx: float, sy: float, sz: float) -> None:
        if self._part is None:
            return
        m = np.diag([float(sx), float(sy), float(sz), 1.0])
        if self._meshXfUpdate(m):
            return
        # 显式绕世界原点缩放（BaseObject.scaleAround 用 T/S/T 包裹实现
        # 绕心缩放）。about=None 时绕"location 位置"——与挂 location 的
        # 平移/旋转组合会产生双重偏移（见 translate 注释）。
        self._part = self._part.scale((sx, sy, sz), about=(0.0, 0.0, 0.0))
        self._clearCache()

    # ---------------- 纯网格 shape 的 numpy 仿射 ----------------

    def _isMeshOnlyShape(self) -> bool:
        """全部面无解析曲面（STL 导入的纯网格面）。"""
        if self._part is None:
            return False
        try:
            from OCP.BRep import BRep_Tool
            faces = list(self._part.faces())
            return bool(faces) and all(
                BRep_Tool.Surface_s(f.wrapped) is None for f in faces)
        except Exception:
            return False

    def _meshXfUpdate(self, m: np.ndarray) -> bool:
        """纯网格 shape 的变换：4×4 仿射累积在 _meshXf（左乘新矩阵）。

        OCCT 对无曲面网格面的变换不可靠：默认 translate/rotate 只挂
        location，随后的 scale 绕"location 位置"缩放产生双重偏移（模型
        被打到网格平面以下，尺寸缩放后"消失"）；transform=True 烘焙对它
        又是 bbox 空操作（实测 T(-60) 后包围盒不动）。故纯网格 shape
        完全绕开 OCCT：矩阵累积在 _meshXf，triangulate/boundingBox
        读取时施加。**不清缓存**——本地三角化与仿射无关，拖动每帧只有
        numpy 矩阵乘（结果缓存由 BRepObject 侧失效重建，廉价）。
        返回 True 表示已处理（调用方直接返回）。"""
        if not self._isMeshOnlyShape():
            return False
        if self._meshXf is None:
            self._meshXf = np.eye(4)
        self._meshXf = m @ self._meshXf
        return True

    # ---------------- 序列化（STEP 保真） ----------------

    def serialize(self) -> Optional[dict]:
        """Part → STEP 文本（export_step 临时文件）→ dict。

        export_step 失败（从 STEP 导入恢复的 shape 再导出会失败——
        build123d/OCCT 限制，用户保存后重开即复现）时降级为三角网格
        快照（verts+tris），保证保存文件不丢 3D 实体。
        """
        if self._part is None:
            return None
        # 纯网格 shape（STL 导入的面等）：export_step 会"成功"写出不含
        # 几何的空 STEP，保存→重开直接丢模型。全部面都无解析曲面时直接
        # 走网格快照（deserialize 端 BRepObject 有对应 {"mesh": …} 分支）。
        try:
            from OCP.BRep import BRep_Tool
            faces = self._part.faces()
            if faces and all(BRep_Tool.Surface_s(f.wrapped) is None
                             for f in faces):
                return self._meshSnapshot()
        except Exception:
            pass
        bd = _import_bd()
        fd, path = tempfile.mkstemp(suffix=".step")
        os.close(fd)
        try:
            bd.export_step(self._part, path)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception:
            return self._meshSnapshot()
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        return {"step": text}

    def _meshSnapshot(self) -> Optional[dict]:
        """三角网格 → {"mesh": …} 降级快照（无三角形返回 None）。"""
        m = self.triangulate()
        if m is None or m.get("verts") is None or not len(m.get("tris", [])):
            return None
        return {"mesh": {
            "verts": [[float(v) for v in r] for r in m["verts"]],
            "tris": [[int(t) for t in r] for r in m["tris"]]}}

    @staticmethod
    def deserialize(data: dict) -> "BrepShape":
        bd = _import_bd()
        text = data.get("step", "")
        fd, path = tempfile.mkstemp(suffix=".step")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            part = bd.import_step(path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        return BrepShape(part)

    def clone(self) -> "BrepShape":
        """Part 不可变：共享引用安全（撤销快照/阵列不修改原 Part）。

        派生缓存（三角网格/特征边/拓扑…）同为 part 的只读派生数据，
        一并共享——否则每次快照克隆都在丢缓存，snapshotSize/渲染又
        触发整轮重新三角化（实测每实体 ~350ms，文档实体越多，点任何
        命令越卡）。_clearCache 为整体置 None 重绑，不会污染原实例。
        """
        o = BrepShape(self._part)
        o._meshTol = self._meshTol
        o._meshAngular = self._meshAngular
        o._meshCache = self._meshCache
        o._topoCache = self._topoCache
        o._edgesCache = self._edgesCache
        o._polyCache = self._polyCache
        o._faceCache = self._faceCache
        o._vertsCache = self._vertsCache
        o._meshOnly = self._meshOnly
        o._meshXf = (None if self._meshXf is None
                     else self._meshXf.copy())
        o._meshTriCache = self._meshTriCache
        return o


# ---------------- 轮廓 → build123d Face ----------------

def _signed_area_xy(pts) -> float:
    """闭合点列在 XY 平面的有向面积（>0 = 逆时针，面法向 +Z）。"""
    s = 0.0
    n = len(pts)
    for i in range(n):
        p0, p1 = pts[i], pts[(i + 1) % n]
        s += p0.x * p1.y - p1.x * p0.y
    return 0.5 * s


def _fit_circle_xy(pts, rel_tol: float = 0.005):
    """点列是否近似共圆（Kasa 最小二乘 + 最大径向偏差 ≤ rel_tol·r）。

    3D 模式画的圆会烘焙成密集多段曲线（kind=spline 走顶点直连），挤出
    侧面变成一圈窄平面条（拔模后尤为明显）。检测命中改用解析圆面，
    侧面为光顺圆柱/圆锥。整圈偏差 < 0.5%·r 的自由曲线在制造语义上
    即圆。返回 (cx, cy, r) 或 None。
    """
    n = len(pts)
    if n < 8:
        return None
    try:
        import numpy as np
        x = np.array([p.x for p in pts], dtype=np.float64)
        y = np.array([p.y for p in pts], dtype=np.float64)
        z = x * x + y * y
        A = np.array([[float(x @ x), float(x @ y), float(x.sum())],
                      [float(x @ y), float(y @ y), float(y.sum())],
                      [float(x.sum()), float(y.sum()), float(n)]])
        rhs = np.array([float(z @ x), float(z @ y), float(z.sum())])
        a, b, c = np.linalg.solve(A, rhs)
    except Exception:
        return None
    cx, cy = a / 2.0, b / 2.0
    r2 = c + cx * cx + cy * cy
    if r2 <= 1e-18:
        return None
    r = math.sqrt(r2)
    dev = float(np.max(np.abs(np.hypot(x - cx, y - cy) - r)))
    if dev > rel_tol * max(r, 1e-9):
        return None
    return cx, cy, r


def _fit_ellipse_xy(pts, rel_tol: float = 0.005):
    """点列是否近似共椭圆（代数最小二乘 conic 拟合 + 残差校验）。

    3D 画椭圆被烘焙成密集多段曲线，顶点直连挤出 → 侧面一圈窄平面条
    （拔模后顶缘锯齿状杂面）。命中（归一化残差 ≤ 0.5%）改用解析椭圆，
    侧面为光顺 BSPLINE/锥面。返回 (cx, cy, a, b, rot)——中心/半长轴/
    半短轴/长轴方位角，或 None。
    """
    n = len(pts)
    if n < 16:
        return None
    try:
        import numpy as np
        x = np.array([p.x for p in pts], dtype=np.float64)
        y = np.array([p.y for p in pts], dtype=np.float64)
        # conic: a·x² + b·xy + c·y² + d·x + e·y + f = 0，取 a+c=1 归一：
        # x² + c(y²−x²) + b·xy + d·x + e·y + f = 0
        cols = np.column_stack([y * y - x * x, x * y, x, y,
                                np.ones(n)])
        rhs = -(x * x)
        sol, *_ = np.linalg.lstsq(cols, rhs, rcond=None)
        c, b, d, e, f = sol
        a = 1.0 - c
        Q = np.array([[a, b * 0.5], [b * 0.5, c]])
        l2 = np.array([d, e])
        # 椭圆判定：Q 正定（两特征值同号为正）
        vals, vecs = np.linalg.eigh(Q)
        if vals[0] <= 1e-12 or vals[1] <= 1e-12:
            return None
        center = np.linalg.solve(2.0 * Q, -l2)
        k = float(center @ Q @ center + l2 @ center + f)   # = f(center)
        if k >= -1e-12:
            return None
        axes = np.sqrt(-k / vals)   # 半轴，与 vals 同序（λ 小 → 轴长）
        # 残差：变换到主轴系（第 i 列方向对应 axes[i] 半轴）
        R = vecs
        rel = np.column_stack([x - center[0], y - center[1]]) @ R
        rr = np.sqrt((rel[:, 0] / axes[0]) ** 2 + (rel[:, 1] / axes[1]) ** 2)
        if float(np.max(np.abs(rr - 1.0))) > rel_tol:
            return None
        # vals 升序 → axes[0] 为长轴半轴，vecs[:,0] 为长轴方向。
        # 椭圆长轴是条线（方向 mod π）：归一到 (−π/2, π/2]
        major_dir = vecs[:, 0]
        rot = math.atan2(major_dir[1], major_dir[0])
        if rot > math.pi / 2:
            rot -= math.pi
        elif rot <= -math.pi / 2:
            rot += math.pi
        semi_a, semi_b = float(axes[0]), float(axes[1])   # a ≥ b
        if semi_b < 1e-9:
            return None
        return (float(center[0]), float(center[1]),
                semi_a, semi_b, rot)
    except Exception:
        return None


def _is_smooth_dense_closed(pts, max_turn_deg: float = 12.0,
                            min_n: int = 32) -> bool:
    """密集闭合点列是否处处光滑（无尖角）。

    逐顶点转向角（相邻边方向夹角）全部 ≤ max_turn_deg 即光滑——手绘
    平滑曲线的密集采样每点转向 ~2°，而带尖角的密集多边形（星形/锯齿）
    在角点转向数十度，被排除保持直连。供"周期样条过点构建轮廓"守卫：
    光滑曲线拔模走光顺曲面，尖角保持平面片。"""
    n = len(pts)
    if n < min_n:
        return False
    lim = math.radians(max_turn_deg)
    for i in range(n):
        p0, p1, p2 = pts[i - 1], pts[i], pts[(i + 1) % n]
        ax, ay = p1.x - p0.x, p1.y - p0.y
        bx, by = p2.x - p1.x, p2.y - p1.y
        la = math.hypot(ax, ay)
        lb = math.hypot(bx, by)
        if la < 1e-12 or lb < 1e-12:
            continue
        cross = ax * by - ay * bx
        dot = ax * bx + ay * by
        if math.atan2(abs(cross), dot) > lim:
            return False
    return True


def _taper_loft_extrude(bd, face, height: float, taper_deg: float):
    """拔模挤出兜底：轮廓线与"平移 + 内缩"的上层线放样成体。

    bd.extrude(taper) 的 DPrism 快路径对多顶点多边形轮廓会抛
    Standard_TypeMismatch（TopoDS::Solid 转换失败）——这里按 build123d
    内部 loft 分支同构自建：offset_2d 内缩 + 平移到挤出高度 + 放样。
    """
    plane = bd.Plane(face)
    off = -height * math.tan(math.radians(taper_deg))
    wires = [face.outer_wire()] + list(face.inner_wires())
    secs = []
    for i, w in enumerate(wires):
        flip = -1 if i > 0 else 1   # 孔反向（脱模）
        local = plane.to_local_coords(w)
        try:
            low = local.offset_2d(flip * off, kind=bd.Kind.INTERSECTION)
        except Exception as e:
            # 内缩量超过凹陷处曲率半径 → 偏移线自交，几何上不可行
            raise ValueError("拔模角过大：轮廓内缩时自交，"
                             "请减小拔模角或挤出高度") from e
        tw = plane.from_local_coords(low)
        tw.move(bd.Location((0, 0, height)))
        secs.append((w, tw))
    solid = bd.Solid.make_loft([secs[0][0], secs[0][1]])
    for w, tw in secs[1:]:
        solid = solid.cut(bd.Solid.make_loft([w, tw]))
    return solid


def _pts_to_face(bd, pts, kind):
    """水平点列 → build123d Face（统一 Face(Wire([edge])) 构造，可互减）。

    圆用解析圆（精确），样条平滑过点（与 2D 显示 Catmull-Rom 同风格，
    外墙与轮廓一致），折线/多边形直连顶点。轮廓 Z 不一致时按首点 Z
    平移到水平面。"""
    if len(pts) < 3:
        return None
    cz = float(pts[0].z)
    xy = [(float(p.x), float(p.y)) for p in pts]
    ox = oy = 0.0
    if kind == "circle":
        cx = sum(p.x for p in pts) / len(pts)
        cy = sum(p.y for p in pts) / len(pts)
        r = math.hypot(pts[0].x - cx, pts[0].y - cy)
        if r < 1e-9:
            return None
        # make_circle 在原点构造，需再平移到曲线实际圆心（样条/多边形
        # 用原始点坐标构造，位置天然保留）
        edge = bd.Edge.make_circle(r)
        ox, oy = cx, cy
    else:
        # 共圆/共椭圆检测：3D 模式画的圆/椭圆被烘焙成密集多段曲线
        # （顶点直连会让侧面成一圈窄平面条、拔模后顶缘锯齿状杂面）——
        # 命中则用解析面（圆→圆柱/圆锥，椭圆→光顺 BSPLINE 侧面）
        fit = _fit_circle_xy(pts) if kind in ("spline", "polygon") else None
        if fit is not None:
            fx, fy, fr = fit
            if fr < 1e-9:
                fit = None
            else:
                edge = bd.Edge.make_circle(fr)
                ox, oy = fx, fy
        if fit is None and kind in ("spline", "polygon"):
            ef = _fit_ellipse_xy(pts)
            if ef is not None and ef[2] > 1e-9 and ef[3] > 1e-9:
                ecx, ecy, ea, eb, erot = ef
                # 解析椭圆按长轴方位角放置到实际中心/平面（含 Z）
                eplane = bd.Plane(origin=(ecx, ecy, cz),
                                  x_dir=(math.cos(erot), math.sin(erot), 0),
                                  z_dir=(0, 0, 1))
                edge = bd.Edge.make_ellipse(ea, eb).moved(
                    bd.Location(eplane))
                fit = (ecx, ecy)   # 标记：边已带全位置，不再平移
                ox = oy = 0.0
                cz = 0.0   # 位置已含 Z，末尾统一平移不再叠乘
        if fit is None and kind in ("spline", "polygon") and \
                _is_smooth_dense_closed(pts):
            # 密集光滑闭合曲线（手绘样条等，不恰为圆/椭圆）：周期样条
            # 过点构建——拔模/无拔模侧面都是光顺曲面。多边形直连的窄
            # 平面条在拔模斜面上呈锯齿杂面（用户实测）。插值过点保证
            # 形状与画布显示一致；尖角曲线已被守卫排除。
            try:
                spts = [(float(p.x), float(p.y), cz) for p in pts]
                edge = bd.Edge.make_spline(spts, periodic=True)
            except Exception:
                edge = None
            if edge is not None:
                fit = (0.0, 0.0)   # 边已用原始坐标，不再平移
                ox = oy = 0.0
        if fit is None:
            # 样条/椭圆/折线/多边形：全部直连顶点（轮廓点已由画布显示同源
            # 采样，足够密时直连即曲线形状；不再做 B 样条插值——少控制点
            # 时过冲/扭结导致外墙与轮廓不一致）
            try:
                edge = bd.Polygon(xy).edges()
            except Exception:
                return None
    try:
        if isinstance(edge, (list, tuple)):
            face = bd.Face(bd.Wire(edge))
        else:
            face = bd.Face(bd.Wire([edge]))
    except Exception:
        # 极少数退化轮廓：退回折线
        try:
            face = bd.Face(bd.Wire(bd.Polygon(xy).edges()))
        except Exception:
            return None
    if abs(ox) > 1e-9 or abs(oy) > 1e-9 or abs(cz) > 1e-9:
        face = face.moved(bd.Location((ox, oy, cz)))
    return face

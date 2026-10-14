"""STL 导出：把文档中的浮雕模型（ReliefModelObject）高度场三角化为
顶面开放网格，写出二进制 STL（单位 mm，Z 向上，与高度场约定一致）。

后处理裁剪与体积优化：
- 只输出高度场顶面三角形，不生成底板/侧壁/底面——浮雕 CAM 只加工顶面曲面；
- 裁剪：整面最大 Z ≤ CROP_Z(0.01mm) 的三角形直接删除——浮雕基面（Z=0 裙边）
  全部裁掉，文件体积大幅缩减；
- 平坦区域合并：四角等高（容差 _FLAT_TOL）的相邻四边形 greedy 合并成最大矩形，
  每矩形仅 2 个三角形，进一步压缩面数。

导出分辨率：经 ReliefModelObject.lod(EXPORT_MAX_DIM) 降采样封顶，避免超大母场
（最大 4096² ≈ 3300 万四边形）产生数 GB 的 STL。
非浮雕对象（曲线/点/图片/标注）与 STL 无关，跳过并计数。
"""
from __future__ import annotations

import os
import struct
import math

import numpy as np

from ..vector.geometry_types import ObjectType

# 导出网格每边顶点数上限（顶面三角形数 ≈ 2×(N-1)²，1024 → 约 209 万三角面）
EXPORT_MAX_DIM = 1024
# 裁剪阈值（mm）：整面最大 Z ≤ 此值的三角形删除（Z=0 基面裙边全部裁掉）
CROP_Z = 0.01
# 平坦四边形判定容差（mm）：四角 Z 差小于此值视为同一平面，可合并
_FLAT_TOL = 1e-6

_BINARY_HEADER = b"cangai_relief binary STL" + b"\0" * (80 - len(b"cangai_relief binary STL"))


def _topTriangleIndices(data: np.ndarray, lock: "np.ndarray | None" = None) -> np.ndarray:
    """高度场顶面四边形 → 三角形顶点索引 (T,3)（平坦区 greedy 矩形合并）。

    非平坦四边形：常规对角线 (v00-v11) 剖分为 2 三角形，绕序法线 +Z；
    平坦四边形（四角等高）：greedy 合并为最大矩形，每矩形 2 三角形。

    lock: (qr,qc) bool，True 的四边形**禁止合并**（即使等高），逐格剖分。
    幕帘投影把环上顶点 XY 移到曲线上：这些顶点接触的四边形若并入大矩形，
    矩形边只连角点 = 切弧直弦，弦与弧之间无三角形 → 顶面沿曲线出现条状
    镂空（切片软件自动补缝故打印正常，查看器里是洞）。锁死后顶面逐格铺
    到弧线上的投影顶点，无弦切缺口。
    """
    ny, nx = data.shape
    qr, qc = ny - 1, nx - 1
    z00 = data[:-1, :-1]
    z01 = data[:-1, 1:]
    z10 = data[1:, :-1]
    z11 = data[1:, 1:]
    zq = (z00 + z01 + z10 + z11) * 0.25
    zmax = np.maximum(np.maximum(z00, z01), np.maximum(z10, z11))
    zmin = np.minimum(np.minimum(z00, z01), np.minimum(z10, z11))
    flat = (zmax - zmin) < _FLAT_TOL                     # (qr, qc)
    if lock is not None:
        flat = flat & (~lock)

    parts = []
    # 平坦四边形 greedy 合并为最大矩形（行扫描 + 行内右扩 + 整行下扩）
    if flat.any():
        visited = np.zeros((qr, qc), dtype=bool)
        for r in range(qr):
            cand = np.flatnonzero(flat[r] & ~visited[r])
            for c in cand:
                if visited[r, c]:
                    continue
                z = zq[r, c]
                # 行内向右扩展（连续同高平坦 quad）
                ok = flat[r] & (np.abs(zq[r] - z) <= _FLAT_TOL)
                stop = np.flatnonzero(~ok[c + 1:])
                c2 = qc - 1 if stop.size == 0 else c + int(stop[0])
                # 向下扩展（整段矩形行同高平坦且未被占用）
                r2 = r
                while r2 + 1 < qr:
                    band = (flat[r2 + 1, c:c2 + 1]
                            & (np.abs(zq[r2 + 1, c:c2 + 1] - z) <= _FLAT_TOL))
                    if not bool(band.all()):
                        break
                    r2 += 1
                visited[r:r2 + 1, c:c2 + 1] = True
                v00 = r * nx + c
                v01 = r * nx + c2 + 1
                v10 = (r2 + 1) * nx + c
                v11 = (r2 + 1) * nx + c2 + 1
                parts.append(np.array([[v00, v01, v11], [v00, v11, v10]]))

    # 非平坦四边形：常规 2 三角剖分（对角线 v00-v11，绕序法线 +Z）
    nf_r, nf_c = np.where(~flat)
    if nf_r.size:
        v00 = nf_r.astype(np.int64) * nx + nf_c
        t1 = np.stack([v00, v00 + 1, v00 + nx + 1], axis=1)
        t2 = np.stack([v00, v00 + nx + 1, v00 + nx], axis=1)
        parts.append(np.concatenate([t1, t2], axis=0))

    if not parts:
        return np.zeros((0, 3), dtype=np.int64)
    return np.concatenate(parts, axis=0)


def _modelTriangles(obj) -> np.ndarray:
    """单个浮雕模型 → (T,3,3) 世界坐标顶面三角形（已裁基面裙边）。"""
    hf = obj.lod(EXPORT_MAX_DIM)
    data = np.asarray(hf.data, dtype=np.float64)
    ny, nx = data.shape
    if nx < 2 or ny < 2:
        return np.zeros((0, 3, 3), dtype=np.float64)

    x0, y0 = hf.origin
    sp = float(hf.spacing)
    lx = x0 + np.arange(nx, dtype=np.float64) * sp
    ly = y0 + np.arange(ny, dtype=np.float64) * sp
    LX, LY = np.meshgrid(lx, ly)                     # (ny, nx)

    # 消除锯齿幕帘：带内顶点 XY 投影到曲线（local 空间），导出竖直光壁
    curtains = [(np.asarray(c["poly"], dtype=np.float64), float(c["d"]))
                for c in getattr(obj, "curtains", lambda: [])()]
    moved = None
    if curtains:
        from core.relief.wall_curtain import project_vertices_to_curtains
        xy = np.stack([LX.ravel(), LY.ravel()], axis=1)
        xy2, moved, _n = project_vertices_to_curtains(xy, curtains, sp)
        LX = xy2[:, 0].reshape(ny, nx)
        LY = xy2[:, 1].reshape(ny, nx)

    # 放置仿射（local->world，仅 XY；Z 取真实高度，垂直夸张仅为显示参数）
    M = np.asarray(obj.placement(), dtype=np.float64)
    WX = M[0, 0] * LX + M[0, 1] * LY + M[0, 2]
    WY = M[1, 0] * LX + M[1, 1] * LY + M[1, 2]

    pts = np.stack([WX.ravel(), WY.ravel(), data.ravel()], axis=1)   # (V,3)
    # 投影移动过的顶点接触的四边形禁止平坦合并（否则大矩形边切弧成直弦，
    # 弦与曲线之间无三角形 → 顶面沿轮廓条状镂空）
    lockq = None
    if moved is not None and moved.any():
        mg = moved.reshape(ny, nx)
        lockq = (mg[:-1, :-1] | mg[:-1, 1:] | mg[1:, :-1] | mg[1:, 1:])
    idx = _topTriangleIndices(data, lockq)
    tris = pts[idx]                                   # (T,3,3)

    # 后处理裁剪：整面最大 Z ≤ 阈值（浮雕 Z=0 基面裙边）直接删除
    if len(tris):
        tris = tris[tris[:, :, 2].max(axis=1) > CROP_Z]

    # 剔除退化三角形（零面积）
    if len(tris):
        e1 = tris[:, 1] - tris[:, 0]
        e2 = tris[:, 2] - tris[:, 0]
        area2 = np.einsum("ij,ij->i", np.cross(e1, e2), np.cross(e1, e2))
        tris = tris[area2 > 1e-18]
    return tris


def _circleExtrudeTriangles(obj, src: dict, nseg: int = 360) -> np.ndarray:
    """圆底面挤出体 → (T,3,3) 解析圆三角形网格。

    底面用源圆 (cx,cy,cz,radius) 按 ``nseg`` 等分解析采样；顶面从
    ``obj.topPolyline()`` 拟合拔模后的圆心/半径/z 再解析采样。两圆同角度序
    一一对应，侧壁按 [b_i,b_j,t_j,t_i] 对角线剖分。绕序：底面反转（法线 -Z）、
    顶面正序（法线 +Z）、侧壁外法线朝外。nseg=360 → 每段 1°，棱角偏差 < 0.004%×R。
    """
    bcx, bcy, bcz, br = src["cx"], src["cy"], src["cz"], src["radius"]
    top = obj.topPolyline()
    if len(top) < 3:
        return np.zeros((0, 3, 3), dtype=np.float64)
    # 去掉首尾重合点
    if top[0].distanceTo(top[-1]) < 1e-9:
        top = top[:-1]
    tcx = sum(p.x for p in top) / len(top)
    tcy = sum(p.y for p in top) / len(top)
    tcz = sum(p.z for p in top) / len(top)
    tr = sum(math.hypot(p.x - tcx, p.y - tcy) for p in top) / len(top)

    base = [(bcx + br * math.cos(2 * math.pi * i / nseg),
             bcy + br * math.sin(2 * math.pi * i / nseg), bcz)
            for i in range(nseg)]
    topc = [(tcx + tr * math.cos(2 * math.pi * i / nseg),
             tcy + tr * math.sin(2 * math.pi * i / nseg), tcz)
            for i in range(nseg)]

    tris = []
    # 底面：反转序扇形剖分（法线 -Z）
    rb = list(reversed(base))
    for k in range(1, nseg - 1):
        tris.append((rb[0], rb[k], rb[k + 1]))
    # 顶面：正序扇形剖分（法线 +Z）
    for k in range(1, nseg - 1):
        tris.append((topc[0], topc[k], topc[k + 1]))
    # 侧壁：[b_i, b_j, t_j, t_i] → (b_i,b_j,t_j)+(b_i,t_j,t_i)
    for i in range(nseg):
        j = (i + 1) % nseg
        b0, b1 = base[i], base[j]
        t0, t1 = topc[i], topc[j]
        tris.append((b0, b1, t1))
        tris.append((b0, t1, t0))

    arr = np.array(tris, dtype=np.float64)
    e1 = arr[:, 1] - arr[:, 0]
    e2 = arr[:, 2] - arr[:, 0]
    area2 = np.einsum("ij,ij->i", np.cross(e1, e2), np.cross(e1, e2))
    arr = arr[area2 > 1e-18]
    return arr


def _solidTriangles(obj) -> np.ndarray:
    """实体对象（BREP 轻量实体 / 挤出实体）→ (T,3,3) 闭合三角形网格。

    BREP：直接取 PolySolid 耳切网格 obj.triangles()；
    挤出实体：源圆（sourceCircle 非空）按解析圆 360 段重采样消除棱角，
    其他底面/顶面 n 边形按首顶点扇形剖分，侧壁四边形按对角线剖分。
    绕序保证法线朝外；退化三角形（零面积）剔除。
    """
    if obj.type() == ObjectType.BREP:
        # 单圆轮廓挤出（sourceCircle + 顶面轮廓存在）：按源圆解析
        # 360 段重采样消除侧面棱角；带孔轮廓（圆环等，faces[1] 是
        # 环带三角面而非顶面轮廓，len<4）不走该路径，直接用 PolySolid
        # 耳切网格（含环带底/顶 + 外壁 + 孔壁，闭合且法线朝外）。
        src = getattr(obj, "sourceCircle", lambda: None)()
        if src and len(obj.topPolyline()) >= 4:
            return _circleExtrudeTriangles(obj, src)
        t = obj.triangles()
        if t is None or len(t) == 0:
            return np.zeros((0, 3, 3), dtype=np.float64)
        return np.asarray(t, dtype=np.float64)
    src = getattr(obj, "sourceCircle", lambda: None)()
    if src:
        return _circleExtrudeTriangles(obj, src)

    faces = obj.faces()
    if not faces:
        return np.zeros((0, 3, 3), dtype=np.float64)

    tris = []
    base, top = faces[0], faces[1]
    walls = faces[2:]

    if len(base) >= 3:
        rb = list(reversed(base))            # 反转 → 从 +Z 看顺时针，法线朝 -Z
        for k in range(1, len(rb) - 1):
            tris.append((rb[0], rb[k], rb[k + 1]))
    if len(top) >= 3:
        for k in range(1, len(top) - 1):     # 正序 → 从 +Z 看逆时针，法线朝 +Z
            tris.append((top[0], top[k], top[k + 1]))
    for w in walls:
        if len(w) < 4:
            continue
        b0, b1, t1, t0 = w[0], w[1], w[2], w[3]
        tris.append((b0, b1, t1))
        tris.append((b0, t1, t0))

    if not tris:
        return np.zeros((0, 3, 3), dtype=np.float64)
    arr = np.array([[[p.x, p.y, p.z] for p in t] for t in tris],
                   dtype=np.float64)
    e1 = arr[:, 1] - arr[:, 0]
    e2 = arr[:, 2] - arr[:, 0]
    area2 = np.einsum("ij,ij->i", np.cross(e1, e2), np.cross(e1, e2))
    arr = arr[area2 > 1e-18]
    return arr


def _writeBinarySTL(tris: np.ndarray, path: str) -> int:
    """把 (T,3,3) float 三角形写出二进制 STL，返回三角形数。"""
    tris = np.ascontiguousarray(tris, dtype=np.float32)
    t_count = int(tris.shape[0])
    e1 = tris[:, 1] - tris[:, 0]
    e2 = tris[:, 2] - tris[:, 0]
    norm = np.cross(e1, e2)
    length = np.linalg.norm(norm, axis=1, keepdims=True)
    length[length < 1e-12] = 1.0
    norm = norm / length

    out_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(out_dir, exist_ok=True)
    with open(path, "wb") as f:
        f.write(_BINARY_HEADER)
        f.write(struct.pack("<I", t_count))
        buf = np.zeros((t_count, 12), dtype=np.float32)
        buf[:, 0:3] = norm
        buf[:, 3:6] = tris[:, 0]
        buf[:, 6:9] = tris[:, 1]
        buf[:, 9:12] = tris[:, 2]
        rec = np.zeros((t_count, 50), dtype=np.uint8)
        rec[:, :48] = np.ascontiguousarray(buf).view(np.uint8).reshape(t_count, 48)
        f.write(rec.tobytes())
    return t_count


def exportSTL(doc, path: str, solids=None) -> int:
    """导出二进制 STL，返回三角形总数。

    - ``solids`` 非空时导出指定的挤出实体列表（3D 模式选中模型导出，
      闭合实体网格：底面+顶面+侧壁）；
    - 否则导出文档中全部浮雕模型（顶面开放网格）。
    无可导出几何时抛 ValueError；单个对象失败不阻断其余对象。
    """
    if solids:
        all_tris = []
        for obj in solids:
            try:
                tris = _solidTriangles(obj)
            except Exception:
                continue
            if len(tris):
                all_tris.append(tris)
        if not all_tris:
            raise ValueError("选中的模型无可导出的三角形")
        tris = np.concatenate(all_tris, axis=0)
        return _writeBinarySTL(tris, path)

    surfaces = [o for o in doc.allObjects() if o.type() == ObjectType.SURFACE]
    if not surfaces:
        raise ValueError("文档中没有可导出的浮雕模型（STL 仅导出 3D 浮雕）")

    all_tris = []
    for obj in surfaces:
        try:
            tris = _modelTriangles(obj)
        except Exception:
            continue
        if len(tris):
            all_tris.append(tris)
    if not all_tris:
        raise ValueError(
            "浮雕低于裁剪阈值（Z≤%.2f 的基面部分已被裁掉），无可导出的三角形"
            % CROP_Z)

    tris = np.concatenate(all_tris, axis=0)
    return _writeBinarySTL(tris, path)

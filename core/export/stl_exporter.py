"""STL 导出：把文档中的浮雕模型（ReliefModelObject）高度场三角化，
写出二进制 STL（单位 mm，Z 向上，与高度场约定一致）。

两种输出（``closed`` 开关）：
- ``closed=False``（历史行为，CAM 用）：只输出高度场**顶面**开放网格，
  不生成底板/侧壁；整面最大 Z ≤ CROP_Z(0.01mm) 的基面裙边三角形删除。
- ``closed=True``（默认，切片/3D 打印用）：输出**闭合实体**——顶面 + Z 底面
  + 沿网格边框的竖直侧壁，水密（每条有向边恰好两次且反向），可直接切片。
  默认还从底部削掉 0.01mm 再落床（``base_crop=BASE_CROP_Z``）：浮雕背景正好在
  z=0，闭合成实体后顶/底在 z=0 共面，就是切片器里"多余的片"；削掉后背景平板
  连同该平面一起消失，底面仍在 z=0。底面 = 裁剪后顶面的投影（几何与旧实现
  相同，构造不同见下）；``base_crop=0`` 可恢复旧几何。

共同优化：
- 顶面容差自适应三角化（:func:`_topTriangleIndices`）：四边形网格 kd 递归切成
  矩形块，块内顶点能被一张双线性面近似（偏差 ≤ merge_tol）就整块接受，按
  **周边折线扇形**剖分（不是 2 个大三角形）——三角形数从"面积级" 2wh 降到
  "周长级" 2(w+h)。周边保留全部网格顶点，相邻块不论大小都按网格单位配边，
  所以天然水密（闭合实体里 T 接缝就是非流形边，切片器报"需修复"）。
  ``merge_tol=0``（严格共面）时零几何误差，但平滑曲面仍是逐格 2 面/格；
  传正值（默认读设置 app_settings.stl_merge_tol = 0.005mm）才能把曲面整体
  压掉一半以上，代价是曲面有 ≤ 2*tol 的微小偏差。
- 底面整片合并（``compactBottom``）：关闭时底面 = 裁剪后顶面逐三角形镜像，
  面数与顶面同量级（闭合实体里占文件近一半）。底面其实是 z=zb 的**一张平面**，
  与高度无关：按四边形掩码再次 greedy 合并成最大矩形 + 扇形剖分，三角形数从
  "面积级"降到"周长级"，几何与水密性完全不变；只有跨越裁平面的窄边带
  （以及幕帘锁定格）仍逐三角形镜像。闭合实体 STL 因此约减半。
- 跳过裁掉的四边形：整块压在裁平面之下的四边形不再生成三角形（旧实现先生成
  再裁掉），省掉这部分三角化时间与峰值内存。

导出分辨率：经 ReliefModelObject.lod(EXPORT_MAX_DIM) 降采样封顶，避免超大母场
（4096² 会产生数 GB 的 STL）；未超上限的模型按**母场原始分辨率**导出，不做任何
重采样（导出几何 = 编辑时的数据）。
非浮雕对象（曲线/点/图片/标注）与 STL 无关，跳过并计数。

绕序/法向：顶面三角形一律朝 +Z（放置矩阵含镜像 det<0 时整体翻转；幕帘投影造成的
个别折叠三角形也按几何方向纠正）；闭合实体按有向体积判正负、整体朝外。STL 查看器
里不应再出现黑面/翻面。
"""
from __future__ import annotations

import os
import struct
import math

import numpy as np

from ..vector.geometry_types import ObjectType

# 导出网格每边顶点数上限（顶面逐格剖分是 2×(N-1)² 三角形，2048 → 最多约 838 万；
# 自适应合并与基面裁剪通常把它压到几十万，开容差简化后可低至几万）。低于此上限的模型
# 按原始分辨率导出，避免"模型做得细、导出变粗"。
EXPORT_MAX_DIM = 2048
# 裁剪阈值（mm）：整面最大 Z ≤ 此值的三角形删除（Z=0 基面裙边全部裁掉）
CROP_Z = 0.01
# 闭合实体默认从底部削掉的高度（mm）：浮雕背景通常正好在 z=0，闭合成实体后
# 顶面/底面在 z=0 共面，就是切片器里"多余的片"。导出时削掉底部 0.01 再落床
# （底面仍在原高度），可把该平面及背景区域整体去掉。
BASE_CROP_Z = 0.01
# 严格共面合并的判定容差（mm）：块内顶点与拟合面偏差小于此值视为同一平面。
# merge_tol 不会低于它（max(_FLAT_TOL, merge_tol)），所以传 0 仍能合并真平面。
_FLAT_TOL = 1e-6
# 写 STL 的分块大小（三角面数）：法向/记录缓冲按块分配，控制峰值内存
_WRITE_CHUNK = 262144

_BINARY_HEADER = b"cangai_relief binary STL" + b"\0" * (80 - len(b"cangai_relief binary STL"))


def _defaultMergeTol() -> float:
    """顶面简化容差的当前默认值（读设置；读不到回退 0 = 严格共面）。"""
    try:
        from core import app_settings
        return max(0.0, float(app_settings.stl_merge_tol))
    except Exception:
        return 0.0


def _fanAround(loop: np.ndarray, cidx: int) -> np.ndarray:
    """闭合环 + 内部中心点 → 三角形索引 (n,3)（每条环边一条三角形，无退化）。

    中心点必须在环**内部**：矩形边上全是共线网格顶点（相邻四边形要用的
    公共顶点），从角点扇形会得到一串零面积三角形，剔除后就是洞/T 型接缝。
    """
    nxt = np.roll(loop, -1)
    c = np.full(len(loop), cidx, dtype=np.int64)
    return np.stack([c, loop, nxt], axis=1)


def _topTriangleIndices(data: np.ndarray, lock: "np.ndarray | None" = None,
                        quad_mask: "np.ndarray | None" = None,
                        tol: float = _FLAT_TOL):
    """高度场顶面 → ``(idx, cents)``：三角形顶点索引 + 合并矩形中心点。

    三角化 = **kd 递归 + 矩形边界环扇形**：把四边形网格切成矩形块，块内
    所有顶点都能被一张双线性面近似（偏差 ≤ tol）就整块接受，用「周边每个
    网格顶点 + 一个中心点」扇形剖分（三角形数 = 2(w+h)，远小于逐格 2wh）；
    否则沿长边二分；小到没有合并收益时逐格对角线剖分。

    为什么边界上必须保留**全部**网格顶点：相邻两块（不论大小）共用一条
    网格边，两边都按网格单位细分 → 每条有向边恰好被两个三角形反向使用，
    天然水密，不需要额外的 T 型接缝缝合。于是合并只在块内部进行，边界
    顶点永远在，绕序 = 网格 index 空间 CCW（法线 +Z）。

    ``idx`` 中 ``>= nx*ny`` 的编号是块中心顶点：编号 ``nx*ny + k`` 对应
    ``cents[k] = (row, col, z)``（网格空间），由调用方映射到世界坐标后
    追加在顶点表尾部（见 :func:`_buildVertices`）。中心点 z 取块四角均值
    （= 双线性的中心值），与判据口径一致。

    lock: (qr,qc) bool，True 的四边形**禁止合并**（即使等高），逐格剖分。
    幕帘投影把环上顶点 XY 移到曲线上：这些顶点接触的四边形若并入大矩形，
    矩形边只连角点 = 切弧直弦，弦与弧之间无三角形 → 顶面沿曲线出现条状
    镂空（切片软件自动补缝故打印正常，查看器里是洞）。锁死后顶面逐格铺
    到弧线上的投影顶点，无弦切缺口。

    quad_mask: (qr,qc) bool，**只生成**这里的四边形（None = 全部）。闭合实体
    导出用它跳过整块压在裁平面下的四边形；合并块也不会跨出掩码。

    tol: 顶点相对块双线性面的最大允许偏差（mm）。默认 ``_FLAT_TOL`` = 严格
    共面（块内顶点精确落在同一平面上，几何零误差）；传正值 = 顶面自适应
    简化。扇形面相对双线性面还有一层鞍形误差（块四角 ``z00+z11-z01-z10``
    决定），故实际偏差约为 tol 的 1.5~2 倍；鞍形项超过 4*tol 的块直接拒。
    """
    ny, nx = data.shape
    qr, qc = ny - 1, nx - 1
    base = nx * ny                      # 块中心顶点起始编号

    gen = (np.ones((qr, qc), dtype=bool) if quad_mask is None
           else np.asarray(quad_mask, dtype=bool))     # 生成哪些四边形
    mrg = gen if lock is None else (gen & (~np.asarray(lock, dtype=bool)))

    parts: list = []
    cents: list = []

    def emit_quads(r0: int, r1: int, c0: int, c1: int) -> None:
        gm = gen[r0:r1, c0:c1]
        if not gm.any():
            return
        rr, cc = np.nonzero(gm)
        v00 = (rr + r0).astype(np.int64) * nx + (cc + c0)
        parts.append(np.stack([v00, v00 + 1, v00 + nx + 1], axis=1))
        parts.append(np.stack([v00, v00 + nx + 1, v00 + nx], axis=1))

    def emit_block(r0: int, r1: int, c0: int, c1: int) -> None:
        # 周边逆时针（网格 index 空间，行/列 = y/x）：
        # 上行左→右、右列下、下行右→左、左列上
        per = np.array(
            [r0 * nx + cc for cc in range(c0, c1 + 1)]
            + [rr * nx + c1 for rr in range(r0 + 1, r1 + 1)]
            + [r1 * nx + cc for cc in range(c1 - 1, c0 - 1, -1)]
            + [rr * nx + c0 for rr in range(r1 - 1, r0, -1)], dtype=np.int64)
        cid = base + len(cents)
        cents.append(((r0 + r1) * 0.5, (c0 + c1) * 0.5,
                      0.25 * float(data[r0, c0] + data[r0, c1]
                                   + data[r1, c0] + data[r1, c1])))
        parts.append(_fanAround(per, cid))

    def block_ok(r0: int, r1: int, c0: int, c1: int) -> bool:
        if not bool(mrg[r0:r1, c0:c1].all()):
            return False
        z = data[r0:r1 + 1, c0:c1 + 1]
        z00 = z[0, 0]
        z01 = z[0, -1]
        z10 = z[-1, 0]
        z11 = z[-1, -1]
        cross = z00 + z11 - z01 - z10
        if abs(cross) > 4.0 * tol:      # 鞍形项：限制锥面偏离双线性的幅度
            return False
        h, w = z.shape
        u = np.arange(h) / (h - 1)
        v = np.arange(w) / (w - 1)
        p = (z00 + (z01 - z00) * v + (z10 - z00) * u[:, None]
             + cross * (u[:, None] * v))
        return float(np.abs(z - p).max()) <= tol

    min_quads = 16          # h*w ≤ 16 的块：合并收益 ≤ 2 倍，不再细分

    def build(r0: int, r1: int, c0: int, c1: int) -> None:
        h, w = r1 - r0, c1 - c0
        if h <= 0 or w <= 0:
            return
        if not bool(gen[r0:r1, c0:c1].any()):
            return
        if h >= 2 and w >= 2:
            if h + w < h * w and block_ok(r0, r1, c0, c1):
                emit_block(r0, r1, c0, c1)
                return
            if h * w > min_quads:
                # 掩码不齐（斜边/锁定窄带）时先把不齐的行/列**整条剥掉**：
                # 否则中点二分会把本来能整块合并的主体也一并切碎
                # （斜坡 + 底面锁定带：一张平面被切成 500+ 个小块）
                if h >= 4 or w >= 4:
                    ok = mrg[r0:r1, c0:c1].all(axis=1)
                    if not ok.all():
                        idx = np.flatnonzero(ok)
                        a = int(idx[0]) if idx.size else 0
                        b = h - 1 - int(idx[-1]) if idx.size else 0
                        if a and (not b or a <= b):
                            build(r0, r0 + a, c0, c1)
                            build(r0 + a, r1, c0, c1)
                            return
                        if b:
                            build(r0, r1 - b, c0, c1)
                            build(r1 - b, r1, c0, c1)
                            return
                        if idx.size == 0:       # 没有整行可合并：再按列剥
                            ok = mrg[r0:r1, c0:c1].all(axis=0)
                            idx = np.flatnonzero(ok)
                            a = int(idx[0]) if idx.size else 0
                            b = w - 1 - int(idx[-1]) if idx.size else 0
                            if a and (not b or a <= b):
                                build(r0, r1, c0, c0 + a)
                                build(r0, r1, c0 + a, c1)
                                return
                            if b:
                                build(r0, r1, c0, c1 - b)
                                build(r0, r1, c1 - b, c1)
                                return
                if h >= w:
                    rm = r0 + (h >> 1)
                    build(r0, rm, c0, c1)
                    build(rm, r1, c0, c1)
                else:
                    cm = c0 + (w >> 1)
                    build(r0, r1, c0, cm)
                    build(r0, r1, cm, c1)
                return
        emit_quads(r0, r1, c0, c1)

    build(0, qr, 0, qc)

    cents = np.array(cents, dtype=np.float64).reshape(-1, 3)
    if not parts:
        return np.zeros((0, 3), dtype=np.int64), cents
    return np.concatenate(parts, axis=0), cents


def _fieldVertices(obj, quantize: bool = False):
    """浮雕模型 → ``(pts, data, det, lockq, ctx)``：世界坐标顶点表与顶面信息。

    ``pts``：(V,3) 行主序 ``(row=Y, col=X)`` 顶点（Z = 真实高度，不含垂直夸张）；
    ``data``：高度场 (ny,nx)；``det``：放置矩阵 XY 手性（<0 = 含镜像）；
    ``lockq``：禁止平坦合并的 (qr,qc) 掩码（幕帘投影移动过顶点的四边形）；
    ``ctx``：``(x0, y0, sp, M)``，供 :func:`_buildVertices` 映射合并矩形中心点。

    ``quantize``：``True`` 时顶点先量化成 float32 并**保持 float32**（STL 最终就是
    float32）。闭合实体靠这个让"底面高度 == 顶面最低点"的判定与写出的文件逐位
    一致，侧壁退化三角形才不会在 float64→float32 之间凭空出现/消失（那样就是
    非流形边）；同时顶点表/三角形表内存减半（2048² 闭合实体差几百 MB）。
    """
    hf = obj.lod(EXPORT_MAX_DIM)
    data = np.asarray(hf.data, dtype=np.float64)
    ny, nx = data.shape
    if nx < 2 or ny < 2:
        return None

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
    if quantize:
        pts = np.ascontiguousarray(pts, dtype=np.float32)
    # 投影移动过的顶点接触的四边形禁止平坦合并（否则大矩形边切弧成直弦，
    # 弦与曲线之间无三角形 → 顶面沿轮廓条状镂空）
    lockq = None
    if moved is not None and moved.any():
        mg = moved.reshape(ny, nx)
        lockq = (mg[:-1, :-1] | mg[:-1, 1:] | mg[1:, :-1] | mg[1:, 1:])
    det = M[0, 0] * M[1, 1] - M[0, 1] * M[1, 0]
    return pts, data, det, lockq, (x0, y0, sp, M)


def _faceUp(tris: np.ndarray, det: float) -> np.ndarray:
    """顶面绕序统一朝 +Z（高度场顶面是单值曲面：正确绕序的法向 z 必为正）。

    放置矩阵含镜像（det<0，如 scale(-1,1) 或负缩放烘焙）时 XY 手性翻转，
    整份三角形都会反向——表现就是 STL 里所有面法向朝下（黑面/需修复）。
    幕帘投影把环上顶点挪到曲线，个别四边形可能折叠 → 逐面再按几何方向纠正。
    """
    if not len(tris):
        return tris
    if det < 0:
        tris = tris[:, [0, 2, 1]]
    nzt = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])[:, 2]
    flip = nzt < 0
    if bool(flip.any()):
        tris[flip] = tris[flip][:, [0, 2, 1]]
    return tris


def _dropDegenerate(tris: np.ndarray) -> np.ndarray:
    """剔除退化三角形（零面积）。"""
    if not len(tris):
        return tris
    e1 = tris[:, 1] - tris[:, 0]
    e2 = tris[:, 2] - tris[:, 0]
    c = np.cross(e1, e2)                    # 只算一次（两次 cross 会多占 T×24B）
    area2 = np.einsum("ij,ij->i", c, c)
    del e1, e2, c
    keep = area2 > 1e-18
    if bool(keep.all()):                    # 常态：没有退化面就不复制整表
        return tris
    return tris[keep]


def _clipCrossing(tris: np.ndarray, zmin: float) -> np.ndarray:
    """跨越 z=zmin 的三角形 → 平面以上部分（1 或 2 个三角形）。

    按 z 升序排序（升序后：1 个顶点在上 → 最后一个；2 个顶点在上 → 后两个），
    只在**真正跨越平面**的边上求交点：1 个顶点在上用边 (0,2)/(1,2)，2 个
    在上用边 (0,1)/(0,2)。（不能一律用 (0,1)——水平边两端同高低于平面时
    分母为 0，插值会炸成 1e10 量级的错误顶点。）
    排序会打乱绕序，最后按法向 z>0 统一朝上（浮雕顶面是单值曲面）。
    """
    if not len(tris):
        return tris
    order = np.argsort(tris[:, :, 2], axis=1)
    ts = np.take_along_axis(tris, order[:, :, None], axis=1)
    zs = np.take_along_axis(tris[:, :, 2], order, axis=1)
    two = zs[:, 1] >= zmin                         # 两个顶点在平面上方
    parts = []

    def _hit(idx, a, b, za, zb):
        d = zb - za
        d = np.where(np.abs(d) < 1e-12, 1e-12, d)
        p = a + (b - a) * ((zmin - za) / d)[:, None]
        p[:, 2] = zmin
        return p

    if two.any():
        t0, t1, t2 = ts[two, 0], ts[two, 1], ts[two, 2]
        z0, z1, z2 = zs[two, 0], zs[two, 1], zs[two, 2]
        pa = _hit(two, t0, t1, z0, z1)
        pb = _hit(two, t0, t2, z0, z2)
        parts.append(np.stack([pa, t1, t2], axis=1))
        parts.append(np.stack([pa, t2, pb], axis=1))
    if (~two).any():
        t0, t1, t2 = ts[~two, 0], ts[~two, 1], ts[~two, 2]
        z0, z1, z2 = zs[~two, 0], zs[~two, 1], zs[~two, 2]
        pa = _hit(~two, t0, t2, z0, z2)
        pb = _hit(~two, t1, t2, z1, z2)
        parts.append(np.stack([pa, t2, pb], axis=1))
    out = parts[0] if len(parts) == 1 else np.concatenate(parts, axis=0)
    nz = np.cross(out[:, 1] - out[:, 0], out[:, 2] - out[:, 0])[:, 2]
    flip = nz < 0
    if bool(flip.any()):
        out[flip] = out[flip][:, [0, 2, 1]]
    return out


def _clipAboveZ(tris: np.ndarray, zmin: float) -> np.ndarray:
    """顶面三角形裁到 z ≥ zmin：整块保留 / 整块删除 / 跨平面精确剖切。"""
    if not len(tris):
        return tris
    nAbove = (tris[:, :, 2] >= zmin).sum(axis=1)
    keep = nAbove == 3
    cross = (nAbove > 0) & (nAbove < 3)
    parts = []
    if keep.any():
        parts.append(tris[keep])
    if cross.any():
        parts.append(_clipCrossing(tris[cross], zmin))
    if not parts:
        return np.zeros((0, 3, 3), dtype=tris.dtype)
    if len(parts) == 1:
        return parts[0]
    return np.concatenate(parts, axis=0)


def _buildVertices(pts: np.ndarray, cents: np.ndarray, ctx) -> np.ndarray:
    """顶点表 + 合并矩形中心点（网格空间）→ 世界坐标顶点表（中心点追加在尾部）。

    ``ctx = (x0, y0, sp, M)``：高度场原点/间距与 local→world 仿射。中心点
    ``(row, col, z)`` 按同一仿射映射，Z 直接取矩形平均高度（平坦区内，
    与周角偏差 < _FLAT_TOL，视觉上仍是平面）。
    """
    if cents is None or len(cents) == 0:
        return pts
    x0, y0, sp, M = ctx
    lx = x0 + cents[:, 1] * sp
    ly = y0 + cents[:, 0] * sp
    wx = M[0, 0] * lx + M[0, 1] * ly + M[0, 2]
    wy = M[1, 0] * lx + M[1, 1] * ly + M[1, 2]
    extra = np.stack([wx, wy, cents[:, 2]], axis=1)
    # 跟随顶点表 dtype（闭合实体是 float32，vstack 强转会隐性升成 float64）
    extra = extra.astype(pts.dtype, copy=False)
    return np.vstack([pts, extra])


def _modelTriangles(obj) -> np.ndarray:
    """单个浮雕模型 → (T,3,3) 世界坐标顶面三角形（已裁基面裙边）。"""
    grid = _fieldVertices(obj)
    if grid is None:
        return np.zeros((0, 3, 3), dtype=np.float64)
    pts, data, det, lockq, ctx = grid
    idx, cents = _topTriangleIndices(data, lockq)
    tris = _faceUp(_buildVertices(pts, cents, ctx)[idx], det)

    # 后处理裁剪：整面最大 Z ≤ 阈值（浮雕 Z=0 基面裙边）直接删除
    if len(tris):
        tris = tris[tris[:, :, 2].max(axis=1) > CROP_Z]
    return _dropDegenerate(tris)


def _boundaryLoop(nx: int, ny: int) -> np.ndarray:
    """网格外边界顶点索引环（逆时针，行/列 = y/x；行号增大 = +Y）。"""
    top = [cc for cc in range(nx - 1)]
    right = [rr * nx + nx - 1 for rr in range(ny - 1)]
    bottom = [(ny - 1) * nx + cc for cc in range(nx - 1, 0, -1)]
    left = [rr * nx for rr in range(ny - 1, 0, -1)]
    return np.array(top + right + bottom + left, dtype=np.int64)


def _quadIndices(nx: int, ny: int) -> np.ndarray:
    """全部四边形按对角线 v00-v11 剖分 (T,3)，绕序 +Z（逐格，不合并）。"""
    qr, qc = ny - 1, nx - 1
    if qr < 1 or qc < 1:
        return np.zeros((0, 3), dtype=np.int64)
    r, c = np.mgrid[0:qr, 0:qc]
    v00 = (r * nx + c).ravel().astype(np.int64)
    t1 = np.stack([v00, v00 + 1, v00 + nx + 1], axis=1)
    t2 = np.stack([v00, v00 + nx + 1, v00 + nx], axis=1)
    return np.concatenate([t1, t2], axis=0)


def _cropQuadClasses(pts: np.ndarray, data: np.ndarray, crop: float, zb: float):
    """按**裁剪后**高度把网格四边形分类 → ``(above, partial)``（(qr,qc) bool）。

    与 :func:`_clipAboveZ` 同一口径（用 ``pts`` 的 float32 顶点、先减 crop 再与
    zb 比，且相减表达式与顶面裁剪逐位一致），避免 float64 判定在 float32 写盘
    分辨率下"看似整块在上、实际被裁一刀"的边：

    - ``above``：四角都高于裁平面 → 顶面三角形裁切不会动它，底面可整片合并；
    - ``partial``：跨越裁平面（含四角恰好压在裁平面上的退化格）→ 逐格剖切，
      底面按裁剪后的三角形逐位镜像。
    其余（四角都不高于裁平面）调用方直接跳过：顶面底面都不需要它。
    """
    ny, nx = data.shape
    z = np.asarray(pts[:, 2], dtype=np.float32).reshape(ny, nx)
    zc = z - np.float32(crop)
    z00 = zc[:-1, :-1]
    z01 = zc[:-1, 1:]
    z10 = zc[1:, :-1]
    z11 = zc[1:, 1:]
    zmin = np.minimum(np.minimum(z00, z01), np.minimum(z10, z11))
    zmax = np.maximum(np.maximum(z00, z01), np.maximum(z10, z11))
    zlo = np.float32(zb)
    above = zmin > zlo                     # 整块高于裁平面（裁切不会动它）
    below = zmax <= zlo                    # 整块在裁平面上/下（裁完什么都不剩）
    return above, (~above) & (~below)


def _planeCapIndices(mask: np.ndarray, nx: int, ny: int,
                     maxRects: "int | None" = None):
    """四边形掩码 → 底面平面盖板三角形索引 ``(idx, cents)``（z 由调用方置为底面高）。

    底面是一整张平面，与高度**无关**：掩码内相邻四边形直接 greedy 合并成最大
    矩形（行内右扩 + 整行下扩），每个矩形用周边网格顶点 + 中心点扇形剖分——
    三角形数从"面积级"降到"周长级"：整张底面（满幅浮雕的常态）只需**几十~几
    百面**，圆岛/镂空这类区域的矩形周长也总小于逐格 2 面/格。孤立 1×1 四边形
    直接出 2 个三角形，不为单个格子加中心顶点。

    ``maxRects``：矩形数上限（防止铺得极碎的掩码把贪心拖成逐格循环、反而
    比逐格镜像更费）。超出时返回 ``(None, None)``，调用方退回逐格镜像。

    绕序在网格 index 空间按 **-Z**（底面朝下）；``cents`` 与
    :func:`_topTriangleIndices` 同格式（网格空间 row/col/z），编号从
    ``nx*ny`` 起，由 :func:`_buildVertices` 追加到顶点表尾部。
    矩形在网格空间恒凸，经放置矩阵线性映射后仍凸，扇形剖分不会自交。
    """
    qr, qc = ny - 1, nx - 1
    if mask is None or qr < 1 or qc < 1 or not bool(mask.any()):
        return (np.zeros((0, 3), dtype=np.int64),
                np.zeros((0, 3), dtype=np.float64))
    base = nx * ny
    visited = np.zeros((qr, qc), dtype=bool)
    parts = []
    cents = []
    for r in range(qr):
        cand = np.flatnonzero(mask[r] & (~visited[r]))
        for c in cand:
            if visited[r, c]:
                continue
            if maxRects is not None and len(parts) >= maxRects:
                return None, None
            ok = mask[r] & (~visited[r])
            stop = np.flatnonzero(~ok[c + 1:])
            c2 = qc - 1 if stop.size == 0 else c + int(stop[0])
            r2 = r
            while r2 + 1 < qr:
                band = mask[r2 + 1, c:c2 + 1] & (~visited[r2 + 1, c:c2 + 1])
                if not bool(band.all()):
                    break
                r2 += 1
            visited[r:r2 + 1, c:c2 + 1] = True
            v00 = r * nx + c
            if r2 == r and c == c2:
                parts.append(np.array([[v00, v00 + nx + 1, v00 + 1],
                                       [v00, v00 + nx, v00 + nx + 1]],
                                      dtype=np.int64))
                continue
            # 矩形周边逆时针后**反向**（法线 -Z）：上行列、右列下、下行列、左列上
            per = np.array(
                [r * nx + cc for cc in range(c, c2 + 2)]
                + [rr * nx + c2 + 1 for rr in range(r + 1, r2 + 2)]
                + [(r2 + 1) * nx + cc for cc in range(c2, c - 1, -1)]
                + [rr * nx + c for rr in range(r2, r, -1)],
                dtype=np.int64)[::-1]
            cid = base + len(cents)
            # 中心点 z 留 0：底面是平面，调用方拿到三角形后统一置为 zb
            cents.append(((r + r2 + 1) * 0.5, (c + c2 + 1) * 0.5, 0.0))
            parts.append(_fanAround(per, cid))
    cents = np.array(cents, dtype=np.float64).reshape(-1, 3)
    return np.concatenate(parts, axis=0), cents


def _bottomCap(pts: np.ndarray, data: np.ndarray, above: np.ndarray,
               keep: np.ndarray, lock_top: "np.ndarray | None", ctx,
               crop: float, zb: float, tol: float) -> np.ndarray:
    """裁剪后实体的底面（z=zb 平面）→ (T,3,3)，与旧"整片镜像"几何相同。

    - ``above`` 四边形（整块高于裁平面）：底面在该处就是平的，按大矩形整片
      合并 + 扇形剖分（见 :func:`_planeCapIndices`），面数降到周长级。
    - 其余 ``keep`` 四边形（跨裁平面 / 幕帘锁定）：取顶面三角形在 zb 上的
      投影（逐格剖切后镜像），保证与顶面的裁切线逐位配边。

    掩码过于破碎（贴底噪声把 above 打散成散点）时合并反而更费，此时整块
    退回逐格镜像（与旧实现逐位一致）。

    两侧共享的网格边都是"单段"：矩形周边包含边上的全部网格顶点，镜像侧的
    裁切不会断开两端均高于裁平面的边，故 1:1 配对，不会出现 T 接缝/非流形边。
    """
    ny, nx = data.shape
    if lock_top is None:
        capMask = above
        mirror = keep & (~above)
    else:
        capMask = above & (~lock_top)
        mirror = keep & (~capMask)

    parts = []
    budget = max(4096, int(capMask.sum()) // 8 + 64)
    cidx, ccents = _planeCapIndices(capMask, nx, ny, budget)
    if cidx is not None and len(cidx) >= 2 * int(capMask.sum()):
        cidx = None                     # 合并后并不比逐格少：不值得
    if cidx is None:
        mirror = keep                   # 整块退回镜像（几何与旧实现一致）
    elif len(cidx):
        cverts = _buildVertices(pts, ccents, ctx)
        cap = cverts[cidx]
        cap[:, :, 2] = zb                 # 周边顶点抬到裁后实体的最低点
        parts.append(cap)
        del cverts
    del cidx, ccents

    if bool(mirror.any()):
        midx, mcents = _topTriangleIndices(data, lock_top, quad_mask=mirror,
                                          tol=tol)
        if len(midx):
            mverts = _buildVertices(pts, mcents, ctx)
            mt = mverts[midx]
            del mverts
            mt[:, :, 2] = mt[:, :, 2] - np.float32(crop)
            mt = _dropDegenerate(_clipAboveZ(mt, zb))
            if len(mt):
                bot = mt[:, [0, 2, 1]]           # 反向即投影到 zb 平面（法线 -Z）
                bot[:, :, 2] = zb
                parts.append(bot)
            del midx, mcents, mt

    if not parts:
        return np.zeros((0, 3, 3), dtype=np.float32)
    return parts[0] if len(parts) == 1 else np.concatenate(parts, axis=0)


def _isConvexRing(xy: np.ndarray) -> bool:
    """二维环是否**凸**（不要求逆时针；共线的边允许）。

    网格边框上相邻顶点在直边段上共线，转向叉积恰好 0——若按 ``cross > 0``
    判凸会整条边框都判凹（旧实现的 bug：矩形永远走逐格兜底）。这里只看
    非共线处的转向是否同号；幕帘把顶点拉出真实凹角时转向会反号 → 返回 False。
    """
    if len(xy) < 3:
        return False
    d = np.roll(xy, -1, axis=0) - xy            # 各条边向量
    e = np.roll(d, -1, axis=0)                  # 下一条边
    cross = d[:, 0] * e[:, 1] - d[:, 1] * e[:, 0]
    scale = float(np.abs(d).max()) if len(d) else 0.0
    if scale <= 0.0:
        return False
    tol = 1e-6 * scale * scale                  # 共线（或 float32 量化噪声）阈值
    big = cross[np.abs(cross) > tol]
    if len(big) < 3:                            # 退化成一条线，不是面
        return False
    return bool((big > 0).all() or (big < 0).all())


def _cropWalls(pts: np.ndarray, loop: np.ndarray, crop: float,
               zb: float) -> list:
    """底部削掉 ``crop`` 后的侧壁：沿网格外边界，只保留裁平面以上的部分。

    与旧侧壁不同，边界顶点可能落到裁平面以下（浮雕缓坡穿过 z=0.02）：
    此时按线性插值在边界段上求出与裁平面的交点 P，用 P 代替低于平面的
    端点，保证侧壁与裁剪后的顶面（边界止于 P）/底面（投影止于 P）配边。

    侧壁上沿必须用**与顶面同一口径**（先减 crop）的高度：顶面整体下移了
    crop，若侧壁上沿仍用原始高度，沿整个边框就会留一圈高 crop 的裂缝
    （切片器报"需修复"、每条边框边只有一个相邻面）。
    """
    zc = np.float32(crop)
    t0 = pts[loop].copy()
    t0[:, 2] = t0[:, 2] - zc
    t1 = pts[np.roll(loop, -1)].copy()
    t1[:, 2] = t1[:, 2] - zc
    h0 = t0[:, 2] - zb
    h1 = t1[:, 2] - zb
    b0 = t0.copy()
    b0[:, 2] = zb
    b1 = t1.copy()
    b1[:, 2] = zb
    both = (h0 > 0.0) & (h1 > 0.0)
    up0 = (h0 > 0.0) & (h1 <= 0.0)
    up1 = (h0 <= 0.0) & (h1 > 0.0)

    walls = []
    if bool(both.any()):
        walls.append(np.stack([t0[both], b0[both], b1[both]], axis=1))
        walls.append(np.stack([t0[both], b1[both], t1[both]], axis=1))
    if bool(up0.any()):
        s = (h0[up0] / (h0[up0] - h1[up0]))[:, None]
        p = t0[up0] + (t1[up0] - t0[up0]) * s
        p[:, 2] = zb
        walls.append(np.stack([t0[up0], b0[up0], p], axis=1))
    if bool(up1.any()):
        s = (h0[up1] / (h0[up1] - h1[up1]))[:, None]
        p = t0[up1] + (t1[up1] - t0[up1]) * s
        p[:, 2] = zb
        walls.append(np.stack([p, b1[up1], t1[up1]], axis=1))
    return walls


def _modelSolidTriangles(obj, base_crop: float = BASE_CROP_Z,
                        compactBottom: bool = True,
                        merge_tol: "float | None" = None) -> np.ndarray:
    """单个浮雕模型 → (T,3,3) **闭合实体**（顶面 + 底面 + 四周侧壁，水密）。

    供切片/3D 打印：STL 必须是有厚度的体，只有顶面的开放网格在切片器里
    是"零厚度面片"。造法（与 CAM 用的开放顶面共用同一套顶面三角化）：

    - 顶面：完整高度场顶面（**不裁基面**，边框必须留到侧壁能接上），
      绕序 +Z；平坦矩形用中心点扇形剖分，与逐格剖分相邻处无 T 型接缝。
    - 底面：Z = ``min(0, 场最低点)`` 平面，边界环与顶面同顶点、绕序反转
      （法线 -Z）；凸环用中心点扇形（约 2(nx+ny) 个三角形，矩形/平行四边形
      恒凸），凹（幕帘把边框顶点拉弯）才退回逐格。
    - 侧壁：只沿网格外边界环，每段顶 z→底 z 竖直壁，法线朝外；顶/底同高
      （退化）的段不加三角形——该处顶面与底面的边本身互为反向，直接配对。

    ``compactBottom``（默认开）：裁掉底部后，底面不再逐三角形镜像整个顶面，
    而是把整块高于裁平面的区域当成**一张平面**整片合并（:func:`_bottomCap`），
    只有跨裁平面的窄边带仍镜像——几何逐位相同，面数从面积级降到周长级。

    ``merge_tol``：顶面容差简化（mm），None = 读设置 ``stl_merge_tol``（默认
    0.005）；0 = 只合并严格共面的块（几何零误差）。正值按块内顶点与双线性
    面的偏差判定，仅限裁平面之上的格，保证与底面色块配边；实测最大几何偏差
    约为该值的 1.5~2 倍。

    绕序：全部在**网格 index 空间**构造（顶 +Z、底 -Z、壁朝外，彼此配对），
    镜像放置 det<0 时最后**整体**翻转——只翻一部分（如旧实现只翻顶面）会让
    顶/壁绕序不一致，表现为每条边被 4 个三角形共用/方向冲突。
    每条有向边恰好出现两次且方向相反（见 ``_probe_stl_solid.py``）；
    最后按有向体积兜底整体朝外（幕帘折叠等异常情形）。
    """
    grid = _fieldVertices(obj, quantize=True)
    if grid is None:
        return np.zeros((0, 3, 3), dtype=np.float64)
    pts, data, det, lockq, ctx = grid
    nx, ny = int(data.shape[1]), int(data.shape[0])
    if merge_tol is None:
        merge_tol = _defaultMergeTol()
    tol = max(_FLAT_TOL, float(merge_tol))

    # 底面高度：浮雕基面通常正好是 0；有负高度（下沉雕刻）就落到最低点。
    # 与顶面取同一量化口径（pts 已 float32 化）——逐位对齐才能保证
    # "顶 z == zb" 判定与写出的文件一致（否则侧壁退化三角形在 float64→
    # float32 之间凭空出现/消失 = 非流形边）。
    zb = float(np.float32(min(0.0, float(data.min()))))

    loop = _boundaryLoop(nx, ny)     # index 空间逆时针（不随镜像反转）
    crop = max(0.0, float(base_crop))

    # 闭合实体：先按**裁剪后**高度把四边形分三类，整块压在裁平面下的那批
    # 直接不生成（旧实现先生成再裁掉，白算一半以上三角形，还多占几百 MB
    # 峰值内存）；跨裁平面的格强制不合并，保证顶面与底面裁切线逐位配边。
    above = partial = keep = None
    lock_top = lockq
    if crop > 0.0 and compactBottom:
        above, partial = _cropQuadClasses(pts, data, crop, zb)
        keep = above | partial
        lock_top = partial if lockq is None else (lockq | partial)

    idx, cents = _topTriangleIndices(data, lock_top, quad_mask=keep, tol=tol)
    if not len(idx):
        return np.zeros((0, 3, 3), dtype=np.float64)
    verts = _buildVertices(pts, cents, ctx)
    top = verts[idx]                 # index 空间已统一 CCW（法线 +Z）
    del idx                          # 千万级面时索引表也占百 MB 级，尽早释放
    del cents

    if crop > 0.0:
        # ---- 底部削掉 crop（去掉 z=0 共面的"多余片"）----
        # 顶面整体下移 crop 后裁到 z >= zb（落床后底面仍在 zb）；
        # 底面 = 裁剪后顶面在 zb 平面上的投影（反向）：顶面边界（裁切线）
        # 恰好与底面边界共用同一条 z=zb 的边，直接配对，无需额外补壁。
        # 下移用 float32 减法，与 _cropQuadClasses 的分类口径逐位一致。
        top[:, :, 2] = top[:, :, 2] - np.float32(crop)
        top = _dropDegenerate(_clipAboveZ(top, zb))
        if not len(top):
            return np.zeros((0, 3, 3), dtype=np.float64)
        del verts
        if compactBottom:
            bot = _bottomCap(pts, data, above, keep, lock_top, ctx,
                             crop, zb, tol)
            if len(bot) >= len(top):
                # 底面合并没省下面数（掩码碎/几乎是窄带）：直接镜整个顶面更省
                del bot
                bot = top[:, [0, 2, 1]].copy()
                bot[:, :, 2] = zb
        else:
            bot = top[:, [0, 2, 1]].copy()
            bot[:, :, 2] = zb
        walls = _cropWalls(pts, loop, crop, zb)
    else:
        # ---- 底面：边界环反向 → 法线 -Z ----
        rev = loop[::-1]
        if _isConvexRing(pts[loop][:, :2]):
            # 中心点扇形（矩形边上全是共线网格点，从角点扇形会得到零面积三角形）
            cx = float(pts[loop][:, 0].mean())
            cy = float(pts[loop][:, 1].mean())
            cidx = len(verts)        # 底面中心点追加在顶点表尾部
            verts = np.vstack([verts, np.array([[cx, cy, zb]],
                                               dtype=verts.dtype)])
            bi = _fanAround(rev, cidx)
        else:
            bi = _quadIndices(nx, ny)[:, ::-1]  # 幕帘拉凹边框：退回逐格（同拓扑）
        bot = verts[bi].copy()
        del verts
        bot[:, :, 2] = zb

        # ---- 侧壁：顶面边界环 → 底 z（每段四边形，退化则只留一个三角形）----
        t0 = pts[loop]
        t1 = pts[np.roll(loop, -1)]
        h0 = t0[:, 2] - zb
        h1 = t1[:, 2] - zb
        b0 = t0.copy()
        b0[:, 2] = zb
        b1 = t1.copy()
        b1[:, 2] = zb
        both = (h0 > 0.0) & (h1 > 0.0)
        only0 = (h0 > 0.0) & ~(h1 > 0.0)     # 下一顶点已在底面：留 (t0,b0,b1)
        only1 = ~(h0 > 0.0) & (h1 > 0.0)     # 本顶点已在底面：留 (t0,b1,t1)

        walls = [np.stack([t0[both], b0[both], b1[both]], axis=1),
                 np.stack([t0[both], b1[both], t1[both]], axis=1),
                 np.stack([t0[only0], b0[only0], b1[only0]], axis=1),
                 np.stack([t0[only1], b1[only1], t1[only1]], axis=1)]
        walls = [w for w in walls if len(w)]

    parts = [p for p in ([top, bot] + walls) if len(p)]
    # 逐部件剔除退化三角形（各自小得多），再拼接：避免对整表再做一次
    # 「掩码 + 复制」（2048² 时那一步就是几百 MB）
    parts = [_dropDegenerate(p) for p in parts]
    parts = [p for p in parts if len(p)]
    if not parts:
        return np.zeros((0, 3, 3), dtype=np.float64)
    tris = parts[0] if len(parts) == 1 else np.concatenate(parts, axis=0)
    parts.clear()
    if det < 0:
        # 镜像放置：XY 手性翻转，整体反向（原地换 1/2 顶点，不整表复制）
        tmp = tris[:, 1].copy()
        tris[:, 1] = tris[:, 2]
        tris[:, 2] = tmp
        del tmp
    return _orientOutward(tris)


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


def _orientOutward(tris: np.ndarray) -> np.ndarray:
    """闭合网格整体朝外：有向体积为负说明绕序整体反了（镜像变换/导入网格等）。

    只对**闭合**网格有意义（STL 实体/挤出体）：散度定理下体积符号即绕序方向。
    开放网格（浮雕顶面）不走这里，它按 +Z 逐面定向（见 :func:`_modelTriangles`）。

    体积用 float64 累加器算（float32 顶点也够判符号），不做整表 float64 复制——
    千万级面时那一份复制就是几百 MB。
    """
    if len(tris) == 0:
        return tris
    t = np.asarray(tris)
    c = np.cross(t[:, 1], t[:, 2])
    vol = float(np.einsum("ij,ij->i", t[:, 0], c, dtype=np.float64).sum())
    del c
    if vol < 0:
        return t[:, [0, 2, 1]]
    return t


def _writeBinarySTL(tris: np.ndarray, path: str) -> int:
    """把 (T,3,3) float 三角形写出二进制 STL，返回三角形数。

    分块写（每块 ``_WRITE_CHUNK`` 面）：法向/记录缓冲只占块大小，闭合实体的
    千万级面数不会再额外吃 1GB 级临时内存（本机内存吃紧，大网格会 OOM）。
    """
    tris = np.ascontiguousarray(tris, dtype=np.float32)
    t_count = int(tris.shape[0])

    out_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(out_dir, exist_ok=True)
    with open(path, "wb") as f:
        f.write(_BINARY_HEADER)
        f.write(struct.pack("<I", t_count))
        for i in range(0, t_count, _WRITE_CHUNK):
            part = tris[i:i + _WRITE_CHUNK]
            n = int(part.shape[0])
            e1 = part[:, 1] - part[:, 0]
            e2 = part[:, 2] - part[:, 0]
            norm = np.cross(e1, e2)
            length = np.linalg.norm(norm, axis=1, keepdims=True)
            length[length < 1e-12] = 1.0
            norm = norm / length
            buf = np.empty((n, 12), dtype=np.float32)
            buf[:, 0:3] = norm
            buf[:, 3:6] = part[:, 0]
            buf[:, 6:9] = part[:, 1]
            buf[:, 9:12] = part[:, 2]
            rec = np.zeros((n, 50), dtype=np.uint8)
            rec[:, :48] = buf.view(np.uint8).reshape(n, 48)
            f.write(rec.tobytes())
            del e1, e2, norm, length, buf, rec
    return t_count


def exportSTL(doc, path: str, solids=None, closed: bool = True,
              base_crop: float = BASE_CROP_Z, surfaces=None,
              merge_tol: "float | None" = None) -> int:
    """导出二进制 STL，返回三角形总数。

    - ``solids`` 非空时导出指定的挤出实体列表（3D 模式选中模型导出，
      闭合实体网格：底面+顶面+侧壁）；
    - ``surfaces`` 非空时只导出这些浮雕模型（2D/浮雕模式下选中网格导出）；
    - 两者都为空时导出文档中全部浮雕模型。``closed=True``（默认）时每个浮雕模型导出为
      **闭合实体**（顶面+底面+四周侧壁，水密，供切片/3D 打印）；并默认从底部
      削掉 ``base_crop``（0.01mm）再落床，去掉浮雕 z=0 基面上的共面"多余片"
      （``base_crop=0`` 关闭该裁剪）。``closed=False`` 只导出顶面开放网格
      （CAM 用，兼容旧行为）。
    - ``merge_tol``：顶面容差简化（mm），None = 读设置 ``stl_merge_tol``（默认
      0.005mm，实测最大偏差 ~0.01mm，远小于常见 0.1mm 层高）；传 0 则只合并
      严格共面的块（几何零损失，但文件约为开启时的 2~3 倍）。
    无可导出几何时抛 ValueError；单个对象失败不阻断其余对象（但 MemoryError
    直接抛出——内存不足不是"没有几何"，静默吞掉会让用户看到误导性提示）。
    """
    if solids:
        all_tris = []
        for obj in solids:
            try:
                tris = _solidTriangles(obj)
                tris = _orientOutward(tris)     # 闭合体：镜像/导入网格绕序兜底
            except MemoryError:
                raise
            except Exception:
                continue
            if len(tris):
                all_tris.append(tris)
        if not all_tris:
            raise ValueError("选中的模型无可导出的三角形")
        tris = all_tris[0] if len(all_tris) == 1 else np.concatenate(all_tris, axis=0)
        all_tris.clear()                    # 单个大网格不再多留一份引用
        return _writeBinarySTL(tris, path)

    if surfaces is None:
        surfaces = [o for o in doc.allObjects() if o.type() == ObjectType.SURFACE]
    if not surfaces:
        raise ValueError("文档中没有可导出的浮雕模型（STL 仅导出 3D 浮雕）")

    all_tris = []
    failed = 0
    for obj in surfaces:
        try:
            tris = (_modelSolidTriangles(obj, base_crop, merge_tol=merge_tol)
                    if closed else _modelTriangles(obj))
        except MemoryError:
            raise
        except Exception:
            failed += 1
            continue
        if len(tris):
            all_tris.append(tris)
    if not all_tris:
        if failed == len(surfaces):
            raise ValueError("浮雕模型三角化失败，无可导出的三角形")
        if closed:
            raise ValueError(
                "浮雕模型低于底部裁剪高度（%.2fmm），削掉后没有剩余几何；"
                "可把模型垫高或关闭闭合实体导出" % float(base_crop))
        raise ValueError(
            "浮雕低于裁剪阈值（Z≤%.2f 的基面部分已被裁掉），无可导出的三角形；"
            "导出闭合实体可保留基面" % CROP_Z)

    tris = all_tris[0] if len(all_tris) == 1 else np.concatenate(all_tris, axis=0)
    all_tris.clear()                        # 单个大网格不再多留一份引用
    return _writeBinarySTL(tris, path)

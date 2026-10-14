"""浮雕 3D 曲面网格构建 + 逐面着色（纯 numpy，零 Qt / 零 GL，可离屏测试）。

把一块高度场（含放置仿射 + 垂直夸张）栅格化成一张可投影的四边形面网格，并按
浮雕(3D)显示模式逐面算 ``uint8`` 颜色，供 3D 视口用 QPainter 画家算法投影绘制：

- ``map``      地图显示：按 Z 高度归一化查地形 LUT（低绿→高白）上色，叠加定向光。
- ``color``    颜色块：用户自选底色（对象色）+ 定向光漫反射 + 轻微高光。
- ``material`` 渲染材质：陶土/木头/塑料/石材/金属，程序化底色 + 漫反射 + 镜面高光。

几何（顶点/法线/高度）只随模型数据/放置/夸张变化，可缓存；颜色依赖视线方向
（镜面高光），逐帧按 eye 计算但为 numpy 向量运算，成本可忽略。

坐标/行序约定与 :class:`~core.relief.height_field.HeightField` 一致：世界 mm、
Z 向上、行0=minY、列0=minX。
"""
from __future__ import annotations

import numpy as np

from . import shade

# 3D 显示曲面每边最大网格数：降采样上限，面数≈N²，交互帧率优先于精度
DEFAULT_MAX_DIM = 64

# 光照模型（GPU 片元着色器 ui/view_3d/gl_surface.py 的 FRAG 与此严格一致）：
#   _AMBIENT 环境光基底（越高越亮越平）；_HEAD 头灯补光权重（光随视线，正面恒亮）；
#   _FLOOR  阴影下限（把漫反射 [0,1] 抬到 [_FLOOR,1]，消除死黑硬阴影）。
# 三者共同实现“明亮、均匀、无大阴影”的精雕式观感。
_AMBIENT = 0.55
_AMBIENT_MAP = 0.70      # 地图/高度色模式：光照更平，保色带鲜艳清晰（精雕式）
_HEAD = 0.90
_FLOOR = 0.50


def _cliffGradientNormals(data: np.ndarray, r_f, c_f):
    """模糊高度场的水平梯度（悬崖面平滑朝向用）。

    data: (ny,nx) 高度格网；r_f/c_f: 面左上格点坐标。返回面中心处的
    (gx, gy)——两次 5 格盒模糊 ≈ 高斯 σ≈2 格，栅格悬崖的楼梯锯齿在
    模糊场里被抹平，梯度方向即平滑等高线的径向。
    """
    b = data.astype(np.float64)
    for _ in range(2):
        c0 = np.cumsum(np.pad(b, ((2, 2), (0, 0)), mode="edge"), axis=0)
        b = (c0[4:, :] - c0[:-4, :]) / 5.0
        c1 = np.cumsum(np.pad(b, ((0, 0), (2, 2)), mode="edge"), axis=1)
        b = (c1[:, 4:] - c1[:, :-4]) / 5.0
    gy, gx = np.gradient(b)
    gx_c = 0.25 * (gx[r_f, c_f] + gx[r_f, c_f + 1]
                   + gx[r_f + 1, c_f] + gx[r_f + 1, c_f + 1])
    gy_c = 0.25 * (gy[r_f, c_f] + gy[r_f, c_f + 1]
                   + gy[r_f + 1, c_f] + gy[r_f + 1, c_f + 1])
    return gx_c, gy_c


class ReliefMesh:
    """一张已构建的 3D 四边形面网格（世界坐标）。

    verts   : (V,3) float64 世界顶点 (x, y, z)
    faces   : (F,4) int32   每个面的顶点索引（绕序使法线朝 +Z）
    normals : (F,3) float64 单位面法线（已翻正，z ≥ 0）
    centers : (F,3) float64 面中心（世界）
    hnorm   : (F,)  float64 面高度归一化 [0,1]（按整场 z 的 min/max）
    gnx,gny : int   降采样后网格每边顶点数（供取边界/角点）
    wall_n  : (V,2) float64 或 None——幕帘投影顶点的水平外法向（world，
              未投影顶点为 0），供竖直壁 quad 法线定向与 ROI 增量重算复用
    wall_faces : (F,) bool 或 None——幕帘竖直壁面标记（True=竖直光壁条/
              近竖直面）。壁与台面在环上共享的顶点已**复制分裂**（wall_dup）：
              壁面引用副本（水平法线）、台面扇面引用原顶点（朝天法线），
              硬边处法线不互相拉斜，台面边缘无明暗杂色带。
    wall_dup : (src_idx, dup_idx) 或 None——硬边复制顶点映射（副本与原顶点
              位置相同，ROI 改 Z 后需把副本 Z 同步回原顶点）。
    """
    __slots__ = ("verts", "faces", "normals", "centers", "hnorm", "gnx", "gny",
                 "wall_n", "wall_faces", "wall_dup")

    def __init__(self, verts, faces, normals, centers, hnorm,
                 gnx: int, gny: int, wall_n=None, wall_faces=None,
                 wall_dup=None) -> None:
        self.verts = verts
        self.faces = faces
        self.normals = normals
        self.centers = centers
        self.hnorm = hnorm
        self.gnx = int(gnx)
        self.gny = int(gny)
        self.wall_n = wall_n
        self.wall_faces = wall_faces
        self.wall_dup = wall_dup

    @property
    def faceCount(self) -> int:
        return int(self.faces.shape[0])


def _normVec(v) -> np.ndarray:
    v = np.asarray(v, dtype=np.float64)
    length = float(np.linalg.norm(v))
    if length < 1e-12:
        return np.array([0.0, 0.0, 1.0])
    return v / length


def _normalizeRows(a: np.ndarray) -> np.ndarray:
    length = np.linalg.norm(a, axis=1)
    length[length < 1e-12] = 1.0
    return a / length[:, None]


def lightDirection(azimuth_deg: float, altitude_deg: float) -> np.ndarray:
    """指向光源的单位向量（与 :mod:`core.relief.shade` 同约定：X=东,Y=北,Z=上）。

    azimuth: 方位角（度，正北=0、顺时针，315=西北）；altitude: 仰角（度）。
    """
    az = np.radians(float(azimuth_deg))
    alt = np.radians(float(altitude_deg))
    return np.array([np.cos(alt) * np.sin(az),
                     np.cos(alt) * np.cos(az),
                     np.sin(alt)], dtype=np.float64)


def cavityFactors(verts, cells: int = 256) -> np.ndarray:
    """网格浮雕实体的逐顶点着色因子（视图无关，多尺度带通）。

    目标观感：平的地方真正平（无杂色），沟槽/凸形/台阶**整体**可辨。
    - 多尺度带通：band_r = blur_r(z) − z。细尺度（r=3 格）抓刻线边缘，
      粗尺度（r=20 格 ≈ 跨度 8%）让整条沟槽/整个凸形相对周边显形——
      带通天然滤掉"板厚"直流分量，厚板浅纹（纹深仅占全高一小截）也
      有强对比（绝对高度编码做不到，用户实测"只有一点点痕迹"）；
    - 每尺度独立坡度门控：波浪/缓坡在各尺度都是缓的（斜率小），带通
      响应按该尺度斜率压掉；刻线壁/台阶沿斜率大，正常通过；
    - 每尺度按 |band| 的 90 分位自归一（不同深浅模型都饱和合适），
      空格邻域填充 + 因子 blur(1)：无杂色斑。
    返回 (V,) 因子，约 [0.45, 1.25]。"""
    v = np.asarray(verts, dtype=np.float64)
    x, y, z = v[:, 0], v[:, 1], v[:, 2]

    span = max(float(np.ptp(x)), float(np.ptp(y)), 1e-9)
    # 栅格分辨率自适应顶点密度：比网格还细的因子栅格会采样混叠出菱形
    # 纹（实测 256² 因子栅格 × 120² 网格）；上限 cells，下限 48。
    G = int(np.clip(np.sqrt(len(v)), 48.0, float(cells)))
    cs = span / G
    ix = np.clip(((x - x.min()) / cs), 0, G - 1).astype(np.int64)
    iy = np.clip(((y - y.min()) / cs), 0, G - 1).astype(np.int64)
    flat = iy * G + ix
    sz = np.bincount(flat, weights=z, minlength=G * G).astype(np.float64)
    cn = np.bincount(flat, minlength=G * G).astype(np.float64)
    zg = (sz / np.maximum(cn, 1e-9)).reshape(G, G)
    # 空格（无顶点的格，栅格可能比顶点密）均值是 0——直接模糊会把"假深
    # 坑"扩散到邻域成杂色斑（实测平台内 std 0.17）。用邻域均值迭代填充。
    empty = cn.reshape(G, G) <= 0
    if empty.any():
        import warnings as _w
        zg = np.where(empty, np.nan, zg)
        for _ in range(80):
            m = np.isnan(zg)
            if not m.any():
                break
            p = np.pad(zg, 1, mode="edge")
            win = np.stack([p[i:i + G, j:j + G]
                            for i in range(3) for j in range(3)])
            with _w.catch_warnings():
                _w.simplefilter("ignore", category=RuntimeWarning)
                mean = np.nanmean(win, axis=0)
            fill = m & ~np.isnan(mean)
            if not fill.any():
                break
            zg = np.where(fill, mean, zg)
        zg = np.nan_to_num(zg, nan=float(np.nanmean(zg)))

    def _box(a, r):
        """可分离盒模糊（边缘 pad，r 单元格）。"""
        k = 2 * r + 1
        p = np.pad(a, ((r, r), (0, 0)), mode="edge")
        c = np.cumsum(p, axis=0)
        o = (c[k - 1:] - np.vstack(
            [np.zeros((1, a.shape[1])), c[:-k]])) / k
        p = np.pad(o, ((0, 0), (r, r)), mode="edge")
        c = np.cumsum(p, axis=1)
        return (c[:, k - 1:] - np.hstack(
            [np.zeros((a.shape[0], 1)), c[:, :-k]])) / k

    zg = _box(zg, 1)                       # 去单格噪声
    gky, gkx = np.gradient(zg)
    slope0 = np.hypot(gkx, gky)

    fgm = np.ones((G, G))
    got = False
    # 三档尺度：细（刻线边缘）/ 中（沟槽整体）/ 粗（大平台、凸形整体）。
    # 门控权重先按本尺度半径扩散（_box(w, r)）：平台/沟槽内部是平的
    # （自身斜率 0），靠"墙边开的门"扩散进来才能整片显形（实测不扩散
    # 时台阶平台内部无信号，对比掉到 0.01 量级）。
    # 幅度与门控（用户实测"顶视仍看不出深度"后加强）：响应幅度加大
    # （沟槽最深 0.45、凸形 0.20），归一降到 P80（更多特征饱和显形），
    # 门控从平方改线性、阈值放宽（轻度抑制波浪即可——过度门控会把
    # 真实图案也压没）。
    for r, amp_d, amp_r in ((3, 0.40, 0.15), (12, 0.40, 0.18), (24, 0.35, 0.15)):
        blur = _box(zg, r)
        band = blur - zg                    # >0 = 低于周边（沟槽），<0 = 凸
        sc = float(np.percentile(np.abs(band), 80.0))
        if sc < max(span * 2e-4, 1e-9):
            continue                        # 该尺度无有效特征
        # 坡度门控（轻度）：大波浪缓坡的带通泄漏部分抑制
        gy2, gx2 = np.gradient(blur)
        slope = np.maximum(np.hypot(gx2, gy2), slope0 * 0.3)
        w = np.clip(slope / max(2.0 * sc / (r * cs), 1e-9), 0.0, 1.0) ** 1.5
        w = _box(w, max(1, r // 2))         # 门向平台/沟槽内部扩散
        resp = (-np.clip(band / (2.0 * sc), 0.0, amp_d)
                + np.clip(-band / (2.0 * sc), 0.0, amp_r))
        fgm = fgm + w * resp
        got = True
    if not got:
        return np.full(len(v), 1.0)
    # 温和绝对高度亮度（P2-P98 归一，0.94+0.12·hn）：台阶这类对称结构
    # 在带通里上下邻居相消，靠绝对高度区分层级；幅度小（波浪仅贡献
    # ~0.03 std）不回杂色。
    zlo2, zhi2 = np.percentile(zg, [2.0, 98.0])
    hn2 = np.clip((zg - zlo2) / max(zhi2 - zlo2, 1e-9), 0.0, 1.0)
    fgm = fgm * (0.94 + 0.12 * hn2)
    fgm = _box(fgm, 1)                      # 因子场平滑（去残余碎斑）
    return np.clip(fgm.ravel()[flat], 0.40, 1.30)


def buildMesh(hf, placement=None, zexag: float = 1.0,
              max_dim: int = DEFAULT_MAX_DIM, curtains=None) -> "ReliefMesh | None":
    """由高度场 + 放置仿射 + 垂直夸张构建 3D 面网格（降采样到 ≤ max_dim²）。

    placement: 3x3 local->world 仿射（None=单位阵）；zexag: Z 放大倍数。
    curtains: 消除锯齿幕帘列表 ``[(poly(M,2) local, distance_mm), ...]``——
    带内规则顶点的 XY 投影到曲线最近点（Z 不动），跨壁四边形退化为沿曲线
    的竖直幕帘（竖直光壁，见 core/relief/wall_curtain.py）。
    退化（覆盖尺寸为 0 或不足 2x2）返回 None。
    """
    small = hf.downsample(int(max_dim))
    data = np.asarray(small.data, dtype=np.float64)
    ny, nx = data.shape
    if nx < 2 or ny < 2:
        return None
    x0, y0 = small.origin
    sp = float(small.spacing)
    lx = x0 + np.arange(nx, dtype=np.float64) * sp
    ly = y0 + np.arange(ny, dtype=np.float64) * sp
    LX, LY = np.meshgrid(lx, ly)                     # (ny, nx)
    Z = data * float(zexag)

    # 消除锯齿幕帘：带内顶点 XY 投影到曲线（local 空间，M 变换之前）
    wall_n = None
    wall_w = None
    if curtains:
        from .wall_curtain import project_vertices_to_curtains
        xy = np.stack([LX.ravel(), LY.ravel()], axis=1)
        xy2, _moved, wall_n = project_vertices_to_curtains(xy, curtains, sp)
        LX = xy2[:, 0].reshape(ny, nx)
        LY = xy2[:, 1].reshape(ny, nx)

    M = np.eye(3, dtype=np.float64) if placement is None \
        else np.asarray(placement, dtype=np.float64)
    WX = M[0, 0] * LX + M[0, 1] * LY + M[0, 2]
    WY = M[1, 0] * LX + M[1, 1] * LY + M[1, 2]
    verts = np.stack([WX.ravel(), WY.ravel(), Z.ravel()], axis=1)   # (V,3)

    # 四边形面索引（行优先 idx = r*nx + c），绕序 v00->v01->v11->v10（法线 +Z）
    rr, cc = np.meshgrid(np.arange(ny - 1), np.arange(nx - 1), indexing="ij")
    v00 = (rr * nx + cc).ravel()
    v01 = v00 + 1
    v10 = v00 + nx
    v11 = v10 + 1
    faces = np.stack([v00, v01, v11, v10], axis=1).astype(np.int32)

    p00, p01, p11, p10 = verts[v00], verts[v01], verts[v11], verts[v10]
    centers = (p00 + p01 + p11 + p10) * 0.25
    # 对角线叉积 = 面积加权法线（鞍形面更稳）；翻正使 z ≥ 0（单值曲面法线朝上）
    n = np.cross(p11 - p00, p10 - p01)
    # 幕帘竖直条/扇面可能产生零面积退化叉积（顶点重合）→ 兜底 +Z，避免归一化 NaN
    ln = np.linalg.norm(n, axis=1)
    bad = ln < 1e-12
    if bad.any():
        n[bad] = np.array([0.0, 0.0, 1.0])
        ln[bad] = 1.0
    normals = n / ln[:, None]
    normals[normals[:, 2] < 0.0] *= -1.0
    # 无幕帘的陡壁 quad（|n_z|≈0 的悬崖面：STL 圆柱侧壁、台阶底座等）：
    # 对角叉积只剩水平分量，上面的 z≥0 翻正无法定水平朝向 → 相邻壁列
    # 法向忽内忽外，Gouraud 明暗交替成竖条纹（2D 导入 STL 切浮雕实测，
    # 与原生构造同现 → 与数据来源无关，是悬崖面朝向歧义）。按"高处→
    # 低处"定向：壁面恒指向背离材料一侧（材料在高处一侧之下）。
    cliff = np.abs(normals[:, 2]) < 0.3
    if cliff.any():
        idx_c = np.nonzero(cliff)[0]
        P4 = verts[faces[idx_c]]                      # (K,4,3)
        z = P4[:, :, 2]
        hi = z >= z.max(axis=1, keepdims=True) - 1e-6
        lo = z <= z.min(axis=1, keepdims=True) + 1e-6
        hc = (P4 * hi[:, :, None]).sum(axis=1) / np.maximum(
            hi.sum(axis=1), 1)[:, None]
        lc = (P4 * lo[:, :, None]).sum(axis=1) / np.maximum(
            lo.sum(axis=1), 1)[:, None]
        d = lc[:, :2] - hc[:, :2]
        dn = np.hypot(d[:, 0], d[:, 1])
        hn = normals[idx_c][:, :2]
        hln = np.hypot(hn[:, 0], hn[:, 1])
        ok = (dn > 1e-9) & (hln > 1e-9)
        dot = np.zeros(len(idx_c))
        dot[ok] = (hn[ok] * d[ok]).sum(axis=1) / (hln[ok] * dn[ok])
        normals[idx_c[dot < 0]] *= -1.0   # z≈0，整向量翻转只改水平朝向
        # 楼梯锯齿抑制：栅格化悬崖（STL 圆盘台阶等）的逐列朝向在 ±x/±y
        # 间跳变，Gouraud 明暗交替成竖条纹。用模糊高度场的水平梯度
        # （= 平滑等高线方向）给悬崖面定向：圆盘悬崖得到干净的径向
        # 朝向，锯齿被抵消；梯度退化（|g|≈0）保留上面的定向。
        # 配合下方 wall_faces=cliff 的硬边复制使用：环顶点副本只被悬崖
        # 面平均，否则朝向法向会被台面/底面的朝天法向稀释（实测两者
        # 缺一不可：只复制 → 楼梯跳变仍在；只模糊 → 被朝天法向淹没）。
        try:
            gx_c, gy_c = _cliffGradientNormals(data, idx_c // (nx - 1),
                                               idx_c % (nx - 1))
            gln = np.hypot(gx_c, gy_c)
            okg = gln > 1e-9
            normals[idx_c[okg], 0] = -(gx_c[okg] / gln[okg])
            normals[idx_c[okg], 1] = -(gy_c[okg] / gln[okg])
            normals[idx_c[okg], 2] = 0.0
        except Exception:
            pass
    # 幕帘竖直壁 quad：叉积法线 n_z≈0（翻正无法定朝向），用四顶点的水平外法向
    # 平均给出**朝外、沿曲线连续**的壁面法线（平台扇面 n_z≈1 不受影响）。
    # wall_n 为 local 向量，随 placement 线性部分变换到 world（旋转/平移严格正确）。
    wall_faces = np.zeros(faces.shape[0], dtype=bool)
    if wall_n is not None:
        wnx = M[0, 0] * wall_n[:, 0] + M[0, 1] * wall_n[:, 1]
        wny = M[1, 0] * wall_n[:, 0] + M[1, 1] * wall_n[:, 1]
        wall_w = np.stack([wnx, wny], axis=1)
        fn = (wall_w[v00] + wall_w[v01] + wall_w[v11] + wall_w[v10]) * 0.25
        fl = np.hypot(fn[:, 0], fn[:, 1])
        vmask = (fl > 0.3) & (np.abs(normals[:, 2]) < 0.7)
        if vmask.any():
            normals[vmask, 0] = fn[vmask, 0] / fl[vmask]
            normals[vmask, 1] = fn[vmask, 1] / fl[vmask]
            normals[vmask, 2] = 0.0
            wall_faces = vmask
    elif cliff.any():
        # 无幕帘的悬崖面（STL 圆柱侧壁、台阶底座等）同样按壁面处理：
        # 硬边复制把环顶点按壁/台面分离，散射后壁顶点只被悬崖面平均
        # （配合上面的模糊梯度朝向 → 干净的圆柱面着色，无竖条纹）。
        wall_faces = cliff

    hz = centers[:, 2]
    lo, hi = float(hz.min()), float(hz.max())
    hnorm = np.full(hz.shape, 0.5) if hi - lo < 1e-9 else (hz - lo) / (hi - lo)

    # 硬边复制：壁面与台面扇面共享的环上顶点复制一份（XYZ 相同，仅分裂
    # 法线/颜色）——壁面改引副本（散射后得水平法线），台面扇面保留原顶点
    # （散射后朝天法线）。否则环顶点法线 = 水平/朝天平均成斜向，Gouraud 插值
    # 会在台面靠曲线一侧拉出一条明暗杂色带（"最高/最低曲线两边的杂色"）。
    wall_dup = None
    if wall_faces.any():
        V0 = verts.shape[0]
        wv = np.bincount(faces[wall_faces].ravel(), minlength=V0) > 0
        fv = np.bincount(faces[~wall_faces].ravel(), minlength=V0) > 0
        bnd = wv & fv
        if bnd.any():
            src = np.nonzero(bnd)[0]
            dup = np.arange(V0, V0 + src.size, dtype=np.int64)
            remap = np.full(V0, -1, dtype=np.int64)
            remap[src] = dup
            faces = faces.copy()
            fw = faces[wall_faces]
            mapped = remap[fw]
            faces[wall_faces] = np.where(mapped >= 0, mapped, fw).astype(np.int32)
            verts = np.vstack([verts, verts[src]])
            wall_dup = (src, dup)
            if wall_w is not None:
                wall_w = np.vstack([wall_w, wall_w[src]])

    return ReliefMesh(verts, faces, normals, centers, hnorm, nx, ny,
                      wall_n=(wall_w if wall_w is not None else None),
                      wall_faces=(wall_faces if wall_faces.any() else None),
                      wall_dup=wall_dup)


def _materialBase(mesh: ReliefMesh, material: str, base_color) -> np.ndarray:
    """材质程序化底色 (F,3) float64（未打光）。木纹/石材按世界坐标稳定生成。"""
    c = mesh.centers
    if material == "wood":
        wx, wy = c[:, 0], c[:, 1]
        # 沿 X 的年轮条纹 + 随 Y 轻微扭曲（0..1）
        g = 0.5 + 0.5 * np.sin((wx * 0.9 + 2.0 * np.sin(wy * 0.12)) * 0.6)
        dark = np.array((94.0, 58.0, 30.0))
        light = np.array((178.0, 128.0, 79.0))
        return dark[None, :] * (1.0 - g[:, None]) + light[None, :] * g[:, None]
    if material == "stone":
        wx, wy = c[:, 0], c[:, 1]
        speck = 0.5 + 0.5 * np.sin(wx * 3.1) * np.sin(wy * 2.7)
        return np.array((150.0, 148.0, 142.0)) * (0.86 + 0.28 * speck)[:, None]
    if material == "metal":
        return np.tile(np.array((176.0, 182.0, 190.0)), (c.shape[0], 1))
    if material == "plastic":
        rgb = np.array(base_color if base_color else (230.0, 230.0, 235.0),
                       dtype=np.float64)
        return np.tile(rgb, (c.shape[0], 1))
    # clay（默认陶土/蜡）：使用模型底色（精雕黄面效果）；缺省暖灰哑光
    rgb = np.array(base_color if (base_color and len(base_color) == 3)
                   else (198.0, 187.0, 172.0), dtype=np.float64)
    return np.tile(rgb, (c.shape[0], 1))


def _materialShading(material: str, diff, spec_ang, normals):
    """材质的高光强度/指数与漫反射权重（返回 kd, specGain, specPow, addAmbient）。"""
    if material == "plastic":
        return 0.58, 150.0, 60.0, 0.42     # 强锐镜面高光、亮底
    if material == "metal":
        return 0.45, 210.0, 80.0, 0.25     # 低漫反射、极锐高光 + 菲涅尔
    if material == "wood":
        return None, 26.0, 16.0, None      # 半光泽清漆（用 lit）
    if material == "stone":
        return None, 18.0, 20.0, None
    return None, 70.0, 30.0, None          # clay：用 lit + 明显高光（反光实物感）


def colorFaces(mesh: ReliefMesh, mode: str = "material", material: str = "clay",
               base_color=(21, 182, 201), light=(0.5, -0.5, 0.7),
               eye=(0.0, 0.0, 300.0), lut: np.ndarray | None = None,
               face_base: np.ndarray | None = None) -> np.ndarray:
    """逐面着色 → ``(F,3) uint8``。mode ∈ {map, color, material}。

    light: 指向光源单位向量；eye: 相机世界位置（算逐面视线方向做镜面高光）；
    lut: map 模式的颜色查找表（默认 terrain）；
    face_base: color 模式的逐面底色 ``(F,3)`` float 0..255（颜色块模式按色号蒙版
      逐面上色：已填色取调色板色、未填色取模型底色）；None 则退回单一 base_color。
    """
    N = mesh.normals
    L = _normVec(light)
    diff = np.clip(N @ L, 0.0, 1.0)                     # (F,) 定向光漫反射
    Vn = _normalizeRows(np.asarray(eye, float)[None, :] - mesh.centers)
    head = np.clip(np.einsum("ij,ij->i", N, Vn), 0.0, 1.0)   # 头灯（光随视线）
    H = _normalizeRows(L[None, :] + Vn)                 # 半程向量
    spec_ang = np.clip(np.einsum("ij,ij->i", N, H), 0.0, 1.0)
    # 主光与头灯取亮者 → 正面恒亮；再抬高下限消除死黑（明亮均匀、无大阴影）
    d = np.maximum(diff, head * _HEAD)
    soft = _FLOOR + (1.0 - _FLOOR) * d                  # 软化漫反射 [_FLOOR,1]
    # 地图(高度色)模式：颜色即高度，光照更平以保色带鲜艳清晰；材质模式保留立体明暗
    amb = _AMBIENT_MAP if mode == "map" else _AMBIENT
    lit = amb + (1.0 - amb) * soft                      # 基础明暗

    if mode == "map":
        table = shade.make_lut("terrain") if lut is None else np.asarray(lut)
        idx = np.clip((mesh.hnorm * 255.0).astype(np.int32), 0, 255)
        base = table[idx].astype(np.float64)
        rgb = base * lit[:, None] + 40.0 * (spec_ang ** 24)[:, None]
    elif mode == "color":
        if face_base is not None:
            # 逐面底色（颜色块蒙版）：base 已是 (F,3)，直接乘逐面明暗
            base = np.asarray(face_base, dtype=np.float64)
            rgb = base * lit[:, None] + 50.0 * (spec_ang ** 32)[:, None]
        else:
            base = np.array(base_color if base_color else (21, 182, 201),
                            dtype=np.float64)
            rgb = base[None, :] * lit[:, None] + 50.0 * (spec_ang ** 32)[:, None]
    else:
        base = _materialBase(mesh, material, base_color)
        kd, specGain, specPow, addAmb = _materialShading(
            material, diff, spec_ang, N)
        if kd is None:                                   # wood/stone/clay：用 lit
            rgb = base * lit[:, None]
        else:                                            # plastic/metal：显式环境+漫反射
            rgb = base * (addAmb + kd * soft)[:, None]
        if material == "metal":                          # 边缘菲涅尔提亮
            fres = (1.0 - np.clip(N[:, 2], 0.0, 1.0)) ** 2
            rgb = rgb + base * (0.5 * fres)[:, None]
        rgb = rgb + specGain * (spec_ang ** specPow)[:, None]

    return np.clip(np.rint(rgb), 0, 255).astype(np.uint8)

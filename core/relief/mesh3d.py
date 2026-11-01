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
from . import material_params as matlib

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

# 材质(PBR)专用“摄影棚环境”（GPU gl_surface.FRAG mode==8 与此严格一致）。
# 真实感来自：按反射向量采样一个程序化 HDRI 风格环境（渐变天空/地面 +
# 几盏 HDR 柔光箱）作为金属/光泽的镜面反射 + GGX 直接高光 + ACES 色调映射。
_PI = 3.141592653589793
_ENV_ZENITH = (0.36, 0.40, 0.48)    # 天顶（淡冷）
_ENV_HORIZON = (0.60, 0.60, 0.62)   # 地平线（亮中性）
_ENV_GROUND = (0.10, 0.09, 0.08)    # 地面（暗暖）
# 柔光箱：(方向(未归一), HDR 颜色(可>1), sigma 角半径)。多盏不同方位/色温
# 的亮面，被光滑表面反射时产生标志性的亮带——“像渲染”而非“填色”的关键。
# 含一颚极小极亮的“太阳”高光点（sigma小）→金属上锐利镜面反光。
_ENV_BOXES = [
    ((0.45, 0.30, 0.84), (3.00, 2.95, 2.85), 0.28),   # 主灯：上前方大软箱
    ((0.50, -0.35, 0.72), (7.00, 6.80, 6.30), 0.07),  # 小太阳：锐利高光点
    ((-0.72, 0.20, 0.62), (0.45, 0.65, 1.05), 0.55),  # 冷侧光：左上方
    ((0.10, -0.85, 0.40), (1.30, 1.00, 0.68), 0.45),  # 暖轮廓光：后下方
]
_KEY_COLOR = (1.00, 0.98, 0.95)   # 直接主光色（中性略暖，由 az/alt 滑块定方向）
_KEY_INTENSITY = 1.70              # 直接主光强度（提高明暗对比）
_AMB_DIFF = 0.20                   # 漫反射环境光系数（降低→不洗白、更有方向感）
_IBL_GAIN = 1.35                   # 环境镜面反射增益（金属更镜）
# 环境光遮蔽(AO)：由高度场“腔体”(局部平均高-本点高)烘焙，凹陷处变暗→浮雕立体感。
_AO_MIN = 0.30                     # AO 下限（最深阴影保留亮度）
_AO_RADII = ((3, 0.55), (8, 0.30), (16, 0.18))   # (盒模糊半径, 权重) 多尺度腔体
# 反照率纹理：CPU 逐顶点采样易撞频(moiré)，故反照率用低频大尺度图案
# (`_matAlbedoV`)，高频细节交给 GPU 逐像素 bump(`_matHeightV`)。
_TEX_ALBEDO_GAIN = 0.5           # 反照率纹理对比缩放
_EXPOSURE = 1.05                   # 曝光（色调映射前）


def _cliffGradientNormals(data: np.ndarray, r_f, c_f):
    """模糊高度场的水平梯度（悬崖面平滑朝向用）。

    data: (ny,nx) 高度格网；r_f/c_f: 面左上格点坐标。返回面中心处的
    (gx, gy)——两次 5 格盒模糊 ≈ 高斯 σ≈2 格，栅格悬崖的楼梯锯齿在
    模糊场里被抹平，梯度方向即平滑等高线的径向。
    """
    b = np.asarray(data, dtype=np.float32)
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

    verts   : (V,3) float32 世界顶点 (x, y, z)
    faces   : (F,4) int32   每个面的顶点索引（绕序使法线朝 +Z）
    normals : (F,3) float32 单位面法线（已翻正，z ≥ 0）
    centers : (F,3) float32 面中心（世界）
    hnorm   : (F,)  float32 面高度归一化 [0,1]（按整场 z 的 min/max）
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
    # 顶点/法线/中心全程 float32：GL 上传本来就是 f4（原先 here 造 float64
    # 再整份 astype 成 f4 是白烧一倍内存），2048² 全量重建的峰值 RSS 从
    # ~1580MB 降到 ~400MB、耗时从 ~1.05s 降到 ~0.3s。世界坐标 mm 量级下
    # f4 量化误差 < 1e-4 mm，远小于浮雕显示与拾取精度。
    data = np.ascontiguousarray(small.data, dtype=np.float32)
    ny, nx = data.shape
    if nx < 2 or ny < 2:
        return None
    x0, y0 = small.origin
    sp = float(small.spacing)
    lx = x0 + np.arange(nx, dtype=np.float64) * sp
    ly = y0 + np.arange(ny, dtype=np.float64) * sp
    M = np.eye(3, dtype=np.float64) if placement is None \
        else np.asarray(placement, dtype=np.float64)
    # 无幕帘时世界 XY 用一维行/列广播直接得到 (ny,nx)，省掉两份 meshgrid 坐标表
    WX = (M[0, 0] * lx)[None, :] + (M[0, 1] * ly)[:, None] + M[0, 2]
    WY = (M[1, 0] * lx)[None, :] + (M[1, 1] * ly)[:, None] + M[1, 2]

    # 消除锯齿幕帘：带内顶点 XY 投影到曲线（local 空间，M 变换之前）
    wall_n = None
    wall_w = None
    if curtains:
        from .wall_curtain import project_vertices_to_curtains
        LX, LY = np.meshgrid(lx, ly)
        xy = np.stack([LX.ravel(), LY.ravel()], axis=1)
        xy2, _moved, wall_n = project_vertices_to_curtains(xy, curtains, sp)
        del xy
        # 投影后顶点 XY 不再可分，只能整表变换
        LX = xy2[:, 0].reshape(ny, nx)
        LY = xy2[:, 1].reshape(ny, nx)
        WX = M[0, 0] * LX + M[0, 1] * LY + M[0, 2]
        WY = M[1, 0] * LX + M[1, 1] * LY + M[1, 2]
        del LX, LY

    Z = data * np.float32(zexag)
    verts = np.empty((ny * nx, 3), dtype=np.float32)
    verts[:, 0] = WX.ravel()
    verts[:, 1] = WY.ravel()
    verts[:, 2] = Z.ravel()
    del WX, WY, Z

    # 四边形面索引（行优先 idx = r*nx + c），绕序 v00->v01->v11->v10（法线 +Z）
    nqy, nqx = ny - 1, nx - 1
    _v00 = (np.arange(nqy, dtype=np.int32)[:, None] * np.int32(nx)
            + np.arange(nqx, dtype=np.int32)[None, :]).ravel()
    faces = np.empty((_v00.size, 4), dtype=np.int32)
    faces[:, 0] = _v00
    faces[:, 1] = _v00
    faces[:, 1] += 1
    faces[:, 2] = _v00
    faces[:, 2] += nx + 1
    faces[:, 3] = _v00
    faces[:, 3] += nx
    del _v00

    # 面中心/法线直接由网格切片算：旧实现用 4 个 (F,3) 花式索引 gather
    # （2048² 即 4×100MB 复制），是重建时间的大头。
    A = verts.reshape(ny, nx, 3)
    # 对角线叉积 = 面积加权法线（鞍形面更稳）；翻正使 z ≥ 0（单值曲面法线朝上）
    d1 = A[1:, 1:] - A[:-1, :-1]                 # p11 - p00
    d2 = A[1:, :-1] - A[:-1, 1:]                 # p10 - p01
    n = np.cross(d1, d2)
    del d1, d2
    n = n.reshape(-1, 3)
    # 四个角点求和后 /4（原地累加，避免 4 份大临时数组）
    cc = A[:-1, :-1] + A[1:, 1:]
    cc += A[1:, :-1]
    cc += A[:-1, 1:]
    cc *= np.float32(0.25)
    centers = cc.reshape(-1, 3)
    del cc
    # 幕帘竖直条/扇面可能产生零面积退化叉积（顶点重合）→ 兜底 +Z，避免归一化 NaN
    ln = np.sqrt(np.einsum("ij,ij->i", n, n))
    bad = ln < 1e-12
    if bad.any():
        n[bad] = np.array([0.0, 0.0, 1.0], dtype=np.float32)
        ln[bad] = 1.0
    normals = n / ln[:, None]
    del n, ln
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
        fn = (wall_w[faces[:, 0]] + wall_w[faces[:, 1]]
              + wall_w[faces[:, 2]] + wall_w[faces[:, 3]]) * 0.25
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
    hnorm = (np.full(hz.shape, np.float32(0.5), dtype=np.float32)
             if hi - lo < 1e-9 else (hz - lo) / (hi - lo))

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


def _matParams(material: str, mat_params: dict | None) -> dict:
    """取材质参数：优先用户参数，缺省回落到该材质的物理预设（与 GPU 分支一致）。"""
    if mat_params:
        return matlib.normalize_params(mat_params)
    return matlib.default_params(material)


def _vhash2(ix, iy):
    """确定性格点哈希 → [0,1)（numpy 值噪声用）。"""
    n = ix * 127.1 + iy * 311.7
    return np.mod(np.sin(n) * 43758.5453123, 1.0)


def _noise2(x, y):
    """双线性平滑值噪声 → [0,1)。"""
    ix = np.floor(x); iy = np.floor(y)
    fx = x - ix; fy = y - iy
    ux = fx * fx * (3.0 - 2.0 * fx)
    uy = fy * fy * (3.0 - 2.0 * fy)
    a = _vhash2(ix, iy); b = _vhash2(ix + 1.0, iy)
    c = _vhash2(ix, iy + 1.0); d = _vhash2(ix + 1.0, iy + 1.0)
    return (a * (1 - ux) + b * ux) * (1 - uy) + (c * (1 - ux) + d * ux) * uy


def _fbm2(x, y, octaves: int = 4):
    """分形布朗运动（多倍频值噪声叠加）→ 大致 [0,1]，有机、不重复。"""
    x = np.asarray(x, dtype=np.float64); y = np.asarray(y, dtype=np.float64)
    tot = np.zeros_like(x); amp = 0.5; freq = 1.0; norm = 0.0
    for _ in range(octaves):
        tot = tot + amp * _noise2(x * freq, y * freq)
        norm += amp; freq *= 2.0; amp *= 0.5
    return tot / norm


def _matAlbedoV(tid: int, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """反照率专用**低频有机**噪声纹理 h≈[-1,1]（值噪声 fBm，非规则正弦）。

    仅用于 CPU 逐顶点反照率调制（低频→不撞频）；高频凹凸在 GPU 逐像素 bump。
    用噪声取代纯正弦→消除“太规则/机械重复”的观感。
    """
    if tid == 1:      # wood：不规则年轮（半径被噪声扰动）
        return np.clip(np.sin(u * 2.2 + 2.2 * _fbm2(u * 0.5, v * 0.5, 3)), -1.0, 1.0)
    if tid == 2:      # plastic：极淡橘皮
        return (_fbm2(u * 1.6, v * 1.6, 3) - 0.5) * 0.8
    if tid == 3:      # stone：多倍频岩石斑块
        return (_fbm2(u * 1.1, v * 1.1, 5) - 0.5) * 2.0
    if tid == 4:      # metal：拉丝（沿 u 拉长→各向异性）
        return (_fbm2(u * 0.5, v * 3.0, 4) - 0.5) * 1.2
    # clay(0)：柔和有机斑驳
    return (_fbm2(u * 1.2, v * 1.2, 4) - 0.5) * 1.6


def _boxblur(a: np.ndarray, r: int) -> np.ndarray:
    """可分离盒模糊（边缘延拓），用于多尺度腔体 AO。返回同形状。"""
    if r < 1:
        return a.copy()
    k = 2 * r + 1
    ap = np.pad(a, r, mode="edge")
    cs = np.cumsum(ap, axis=1, dtype=np.float64)
    cs = np.insert(cs, 0, 0.0, axis=1)                 # 前缀和补零→窗口数=原长
    row = (cs[:, k:] - cs[:, :-k]) / k
    cs2 = np.cumsum(row, axis=0, dtype=np.float64)
    cs2 = np.insert(cs2, 0, 0.0, axis=0)
    return (cs2[k:, :] - cs2[:-k, :]) / k


def _faceAO(mesh: ReliefMesh) -> np.ndarray:
    """逐面环境光遮蔽 (F,) float [~_AO_MIN..1]：高度场“腔体”多尺度烘焙。

    以局部平均高与本身高之差(凹陷为正)估计遮蔽，多个模糊半径加权；
    按全场高度范围归一→与尺寸/zexag 无关。非纯网格(面数≠(gny-1)*(gnx-1))时返回 1。
    """
    gny, gnx = mesh.gny, mesh.gnx
    F = mesh.centers.shape[0]
    if (gny - 1) * (gnx - 1) != F:
        return np.ones(F)
    H = mesh.centers[:, 2].reshape(gny - 1, gnx - 1)
    rng = (H.max() - H.min()) + 1e-6
    occ = np.zeros_like(H)
    for rad, wt in _AO_RADII:
        cav = np.clip(_boxblur(H, rad) - H, 0.0, None)
        occ += wt * np.clip(cav / rng * 2.2, 0.0, 1.0)
    return np.clip(1.0 - occ, _AO_MIN, 1.0).reshape(-1)


def _materialBase(mesh: ReliefMesh, material: str, base_color,
                  mat_params: dict | None = None) -> np.ndarray:
    """材质反照率 (F,3) float64 [0,255]（未打光）：基础色 × 低通纹理 × AO。

    反照率纹理用大尺度/低对比（避免逐顶点撞频 moiré），高频细节交给 GPU 逐像素 bump；
    同时乘上由高度场烘焙的环境光遮蔽 AO（凹陷变暗）→浮雕立体感。
    """
    P = _matParams(material, mat_params)
    bc = np.asarray(P["base_color"], dtype=np.float64) * 255.0
    c = mesh.centers
    mod = _faceAO(mesh)                        # (F,) AO
    scale = max(float(P.get("tex_scale", 6.0)), 0.5)
    amount = float(P.get("tex_amount", 0.0))
    if float(P.get("tex_on", 1.0)) <= 0.5:     # 纹理开关（勾选）：关→仅 AO、无纹理
        amount = 0.0
    if amount > 0.0:
        h = _matAlbedoV(matlib.TEXTURE_ID.get(material, 0), c[:, 0] / scale, c[:, 1] / scale)
        mod = mod * np.clip(1.0 + amount * _TEX_ALBEDO_GAIN * h, 0.0, 2.0)
    return bc[None, :] * mod[:, None]


def _envSample(d: np.ndarray) -> np.ndarray:
    """程序化摄影棚环境 HDR 辐亮度：渐变天空/地面 + 多柔光箱。

    d: (F,3) 单位方向。返回 (F,3) float HDR（可 >1）。与 GPU envSample 一致。
    """
    z = d[:, 2]
    zen = np.asarray(_ENV_ZENITH); hor = np.asarray(_ENV_HORIZON); grd = np.asarray(_ENV_GROUND)
    t = np.clip(z, 0.0, 1.0) ** 0.6          # 地平线→天顶
    gt = np.clip(-z, 0.0, 1.0) ** 0.5        # 地平线→地面
    base = hor[None, :] * (1.0 - t[:, None]) + zen * t[:, None]
    base = base * (1.0 - gt[:, None]) + grd * gt[:, None]
    # 柔光箱高斯瓣（以单位向量弦距近似角距：弦长²≈2(1-cosθ)）
    for bdir, bcol, sig in _ENV_BOXES:
        bn = np.asarray(bdir, dtype=np.float64)
        bn = bn / np.linalg.norm(bn)
        ang2 = np.clip(((d - bn) ** 2).sum(-1), 0.0, None)
        w = np.exp(-ang2 / (2.0 * sig * sig))
        base = base + np.asarray(bcol) * w[:, None]
    return base


def _acesFilmic(x: np.ndarray) -> np.ndarray:
    """ACES 近似色调映射（HDR→LDR）：高光滚降、中等对比。逐通道。"""
    a, b, c, d, e = 2.51, 0.03, 2.43, 0.59, 0.14
    return np.clip((x * (a * x + b)) / (x * (c * x + d) + e), 0.0, 1.0)


def _pbrShade(P: dict, albedo: np.ndarray, N: np.ndarray, L: np.ndarray,
              Vn: np.ndarray, spec_ang: np.ndarray, quality: str = "fine"):
    """逐面 PBR 着色 → rgb float [0,1]（环境IBL + GGX + ACES）。

    albedo (F,3) 反照率[0,1]；N/L/Vn 均为单位向量 (F,3)/(3,)；spec_ang=dot(N,H)。
    金属/光泽按反射向量 R 采样摄影棚环境（亮带随曲面扫过→金属感）；GGX 微表面
    直接高光使粗糙度改变高光形状而非明暗；ACES 色调映射避免高光削平。与 GPU 严格一致。
    """
    metallic = float(P["metallic"]); rough = min(max(float(P["roughness"]), 0.03), 1.0)
    ior = float(P["ior"]); spec_level = float(P["spec_level"])
    sss = float(P["sss_weight"]); trans = float(P["trans_weight"])
    emis = np.asarray(P["emis_color"], dtype=np.float64) * min(float(P["emis_strength"]), 1.0)
    key_c = np.asarray(_KEY_COLOR) * _KEY_INTENSITY

    f0d = min((((ior - 1.0) / (ior + 1.0)) ** 2) * (spec_level * 2.0), 1.0)
    F0 = f0d * (1.0 - metallic) + albedo * metallic               # (F,3)
    diffAlb = albedo * (1.0 - metallic) * (1.0 - trans)           # 漫反射反照率

    nv = np.clip(np.einsum("ij,ij->i", N, Vn), 1e-4, 1.0)         # N·V
    nl = np.clip(N @ L, 0.0, 1.0)                                 # N·L
    fr = F0 + (1.0 - F0) * ((1.0 - nv) ** 5)[:, None]             # Schlick 菲涅尔

    if quality != "fine":
        # 粗糙档：半球环境光 + Blinn 高光；无逐像素环境采样/无 GGX/无 ACES，更快。
        hemi = 0.5 + 0.5 * N[:, 2]
        zen = np.asarray(_ENV_ZENITH); grd = np.asarray(_ENV_GROUND)
        ambient = (grd[None, :] * (1.0 - hemi[:, None]) + zen * hemi[:, None]) * 0.55
        shin = min(max(2.0 / (rough * rough) - 2.0, 2.0), 1200.0)
        keySpec = fr * (spec_ang ** shin)[:, None] * key_c[None, :] \
            * (0.3 + 0.7 * (1.0 - rough)) * nl[:, None]
        rgb = diffAlb * (key_c[None, :] * nl[:, None] + ambient) + keySpec + fr * ambient * 0.6
        rgb = rgb + albedo * ambient * (sss * 0.4) + emis[None, :]
        return np.clip(rgb, 0.0, 1.0)

    # 环境：漫反射环境辐照度(取法线方向近似) + 镜面按反射向量采样(粗糙度模糊)
    envN = _envSample(N)
    R = 2.0 * nv[:, None] * N - Vn
    R = R / np.maximum(np.linalg.norm(R, axis=1, keepdims=True), 1e-9)
    envR = _envSample(R) * (1.0 - rough) + envN * rough           # 粗糙→向法线环境模糊

    # 菲涅尔（Schlick）：环镜 IB 与 GGX 共用（basic 已提前返回，这里复用 fr）

    # GGX 直接高光（主光）
    a2 = (rough * rough) ** 2
    NoH = np.clip(spec_ang, 0.0, 1.0)
    den = NoH * NoH * (a2 - 1.0) + 1.0
    D = a2 / (_PI * den * den + 1e-7)
    k = (rough * rough) * 0.5
    G = (nv / (nv * (1.0 - k) + k)) * (nl / (nl * (1.0 - k) + k))
    Hn = _normalizeRows(L[None, :] + Vn)
    VoH = np.clip(np.einsum("ij,ij->i", Vn, Hn), 0.0, 1.0)
    Fdir = F0 + (1.0 - F0) * ((1.0 - VoH) ** 5)[:, None]
    ggx = (D * G) / (4.0 * nv * nl + 1e-5)                        # (F,) 逐面标量
    spec_dir = Fdir * ggx[:, None] * key_c[None, :] * nl[:, None]

    # 漫反射：方向主光 Lambert + 环境（金属无漫反射）；环境系数低→不洗白、有方向感
    diff = diffAlb * ((key_c[None, :] * (nl * 0.5)[:, None]) + envN * _AMB_DIFF)
    # 环境镜面（IBL）：金属反射亮带主要来源；按 (1-rough) 强衰减→粗糙介质(泥/木/石)几乎不反射
    spec_ibl = fr * envR * (1.0 - rough) * _IBL_GAIN

    rgb = diff + spec_dir + spec_ibl
    rgb = rgb + albedo * envN * (sss * 0.4)                      # 次表面柔光
    rgb = rgb + emis[None, :]
    rgb = _acesFilmic(rgb * _EXPOSURE)                            # 色调映射
    return np.clip(rgb, 0.0, 1.0)


def colorFaces(mesh: ReliefMesh, mode: str = "material", material: str = "clay",
               base_color=(21, 182, 201), light=(0.5, -0.5, 0.7),
               eye=(0.0, 0.0, 300.0), lut: np.ndarray | None = None,
               face_base: np.ndarray | None = None,
               mat_params: dict | None = None, quality: str = "fine") -> np.ndarray:
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
        P = _matParams(material, mat_params)
        albedo = _materialBase(mesh, material, base_color, P) / 255.0   # (F,3) [0,1]
        rgb = _pbrShade(P, albedo, N, L, Vn, spec_ang, quality) * 255.0

    return np.clip(np.rint(rgb), 0, 255).astype(np.uint8)

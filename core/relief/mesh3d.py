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


class ReliefMesh:
    """一张已构建的 3D 四边形面网格（世界坐标）。

    verts   : (V,3) float64 世界顶点 (x, y, z)
    faces   : (F,4) int32   每个面的顶点索引（绕序使法线朝 +Z）
    normals : (F,3) float64 单位面法线（已翻正，z ≥ 0）
    centers : (F,3) float64 面中心（世界）
    hnorm   : (F,)  float64 面高度归一化 [0,1]（按整场 z 的 min/max）
    gnx,gny : int   降采样后网格每边顶点数（供取边界/角点）
    """
    __slots__ = ("verts", "faces", "normals", "centers", "hnorm", "gnx", "gny")

    def __init__(self, verts, faces, normals, centers, hnorm,
                 gnx: int, gny: int) -> None:
        self.verts = verts
        self.faces = faces
        self.normals = normals
        self.centers = centers
        self.hnorm = hnorm
        self.gnx = int(gnx)
        self.gny = int(gny)

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


def buildMesh(hf, placement=None, zexag: float = 1.0,
              max_dim: int = DEFAULT_MAX_DIM) -> "ReliefMesh | None":
    """由高度场 + 放置仿射 + 垂直夸张构建 3D 面网格（降采样到 ≤ max_dim²）。

    placement: 3x3 local->world 仿射（None=单位阵）；zexag: Z 放大倍数。
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
    normals = _normalizeRows(n)
    normals[normals[:, 2] < 0.0] *= -1.0

    hz = centers[:, 2]
    lo, hi = float(hz.min()), float(hz.max())
    hnorm = np.full(hz.shape, 0.5) if hi - lo < 1e-9 else (hz - lo) / (hi - lo)
    return ReliefMesh(verts, faces, normals, centers, hnorm, nx, ny)


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

"""浮雕模型对象：一等文档对象（ObjectType.SURFACE），持有高度场 + 放置仿射变换。

设计（对齐方案 v2）：
- 2D 显示为带着色预览的网格面（见 ui/view_2d/items/relief_model_item.py）。
- 变换只改 XY：translate / rotate(Z) / scale 累积到 ``_place``（无损、实时预览）；
  dz / sz 一律忽略（Z=高度不可经变换改变，只能在浮雕编辑态改）。
- "重构"(bake) 把 ``_place`` 重采样烘焙进网格并复位为单位阵（唯一有损步骤）。
- 曲线与模型彻底解耦：本对象不持有任何曲线引用。
- 序列化：metadata + 网格(zlib 压缩 base64)，可正确存盘/撤销。
"""
from __future__ import annotations

import base64
import math
import zlib

import numpy as np

from .base_object import BaseObject
from .geometry_types import Point3D, BoundingBox3D, ObjectType
from core.relief.height_field import HeightField

# 2D 预览显示模式（默认"等高线"）
DISPLAY_MODES = ("gray", "contour")
# 浮雕(3D)模式显示：地图(按高度上色) / 颜色块(用户填色) / 渲染材质
DISPLAY_3D_MODES = ("map", "color", "material")
# "渲染材质"下的可选材质
MATERIALS = ("clay", "wood", "plastic", "stone", "metal")


class ReliefModelObject(BaseObject):
    """浮雕模型：一块高度场 + 放置仿射（local->world）。"""

    def __init__(self, hf: HeightField | None = None) -> None:
        super().__init__(ObjectType.SURFACE)
        if hf is None:
            # 工厂无参构造用的最小占位网格；deserialize 会替换
            hf = HeightField(np.zeros((2, 2), dtype=np.float32), (0.0, 0.0), 1.0)
        self._hf = hf
        self._place = np.eye(3, dtype=np.float64)   # local->world 仿射；单位阵=已对齐
        self._version = 0                            # 网格/参数变更计数（预览缓存键）
        # 显示参数（按模型存储、可序列化；对齐 ArtCAM 的按模型"浮雕光线"）
        self._displayMode = "contour"
        self._lightAzimuth = 315.0
        self._lightAltitude = 45.0
        self._zExaggeration = 1.0
        self._gridDivisions = 10
        # 浮雕(3D)模式显示参数（与 2D 预览独立）：默认地图/陶土
        self._display3DMode = "map"
        self._material = "clay"
        # 与 theme 主色一致的默认轮廓色
        self._color = (21, 182, 201)
        # LOD 金字塔缓存：按 (母场 stamp, 长边上限) 记忆化降采样副本。
        # 显示/交互取此副本，母场保持加工精度——显示分辨率与加工分辨率分离。
        self._lodCache: dict = {}
        self._lodStamp: int = -1
        # 色号蒙版层（区域划分用）：与母场同形状 uint8，0=未填色、idx+1=调色板第 idx 色。
        # 懒分配（首次填色才建），None 表示全空；_maskStamp 供渲染缓存失效（独立于几何 version，
        # 因填色不改高度、不应触发网格几何重建）。
        self._colorMask: np.ndarray | None = None
        self._maskStamp: int = 0
        self._lodMaskCache: dict = {}

    # ---------------- 工厂 / 访问器 ----------------

    @classmethod
    def fromHeightField(cls, hf: HeightField) -> "ReliefModelObject":
        return cls(hf)

    def heightField(self) -> HeightField:
        return self._hf

    def lod(self, maxDim: int) -> HeightField:
        """按长边上限返回降采样副本（LOD 金字塔的一档），按 (stamp, maxDim) 记忆化。

        2D 预览与 3D 网格都从此取源，不再每次对全场 downsample；母场数据被替换
        （stamp 变）时旧档位自动失效并清理，避免陈旧网格堆积。
        """
        hf = self._hf
        key = (hf.stamp, int(maxDim))
        cached = self._lodCache.get(key)
        if cached is not None:
            return cached
        if self._lodStamp != hf.stamp:
            self._lodCache.clear()
            self._lodStamp = hf.stamp
        sub = hf.downsample(int(maxDim))
        self._lodCache[key] = sub
        return sub

    def placement(self) -> np.ndarray:
        return self._place

    def version(self) -> int:
        return self._version

    def bumpVersion(self) -> None:
        self._version += 1

    def hasPendingTransform(self) -> bool:
        """是否有未烘焙的放置变换（进入浮雕编辑态时据此决定是否自动重构）。"""
        return not np.allclose(self._place, np.eye(3), atol=1e-9)

    # 显示参数 get/set（写操作触发 bumpVersion 以便预览缓存失效）
    def displayMode(self) -> str:
        return self._displayMode

    def setDisplayMode(self, m: str) -> None:
        if m in DISPLAY_MODES and m != self._displayMode:
            self._displayMode = m
            self.bumpVersion()

    def lightAzimuth(self) -> float:
        return self._lightAzimuth

    def setLightAzimuth(self, a: float) -> None:
        self._lightAzimuth = float(a)
        self.bumpVersion()

    def lightAltitude(self) -> float:
        return self._lightAltitude

    def setLightAltitude(self, a: float) -> None:
        self._lightAltitude = float(a)
        self.bumpVersion()

    def zExaggeration(self) -> float:
        return self._zExaggeration

    def setZExaggeration(self, z: float) -> None:
        self._zExaggeration = max(0.0, float(z))
        self.bumpVersion()

    def gridDivisions(self) -> int:
        return self._gridDivisions

    def setGridDivisions(self, n: int) -> None:
        self._gridDivisions = max(1, int(n))
        self.bumpVersion()

    # 浮雕(3D)模式显示参数 get/set（写操作触发 bumpVersion 使曲面缓存失效）
    def display3DMode(self) -> str:
        return self._display3DMode

    def setDisplay3DMode(self, m: str) -> None:
        if m in DISPLAY_3D_MODES and m != self._display3DMode:
            self._display3DMode = m
            self.bumpVersion()

    def material(self) -> str:
        return self._material

    def setMaterial(self, m: str) -> None:
        if m in MATERIALS and m != self._material:
            self._material = m
            self.bumpVersion()

    # ---------------- 色号蒙版层（区域划分：填色 / 作用区域裁剪 / 颜色块渲染） ----------------

    def colorMask(self):
        """色号蒙版 (ny, nx) uint8（0=未填色、idx+1=调色板色）；未填过返回 None。

        与母场 ``heightField().data`` 同形状、同栅格，故可直接与区域浮雕的内部
        掩膜逐格对齐（见 :func:`core.relief.region_relief.apply_region_relief`）。
        """
        return self._colorMask

    def maskStamp(self) -> int:
        """色号蒙版版本号：每次填色/烘焙推进，供渲染底色缓存（surfKey）失效。

        独立于几何 version —— 填色不改高度，不应触发网格几何重建。
        """
        return self._maskStamp

    def ensureColorMask(self) -> np.ndarray:
        """按母场形状确保色号蒙版已分配（全 0），返回它（可能是既有实例）。"""
        hf = self._hf
        if self._colorMask is None or self._colorMask.shape != (hf.ny, hf.nx):
            self._colorMask = np.zeros((hf.ny, hf.nx), dtype=np.uint8)
        return self._colorMask

    def fillColorMask(self, inside, value: int) -> int:
        """把 ``inside``(ny, nx) bool 为 True 的格子填成 ``value``（copy-on-write）。

        返回本次实际改变取值的格子数（0=无变化）。推进 ``_maskStamp``（不动几何
        version），使渲染底色缓存失效但几何网格缓存保留。撤销由文档全量 serialize
        快照覆盖（色号层已入 serialize），无需额外处理。
        """
        m = self.ensureColorMask()
        inside = np.asarray(inside, dtype=bool)
        if inside.shape != m.shape:
            return 0
        v = int(value) & 0xFF
        if not bool((inside & (m != v)).any()):
            return 0
        new = m.copy()
        new[inside] = v
        self._colorMask = new
        self._maskStamp += 1
        self._lodMaskCache.clear()
        return int(inside.sum())

    def lodColorMask(self, maxDim: int):
        """按长边上限返回降采样色号蒙版（最近邻 ``[::step,::step]``），与
        :meth:`core.relief.height_field.HeightField.downsample` **同一 step 公式**，
        故形状与 ``buildMesh(hf, ..., max_dim=maxDim)`` 的网格 (gny, gnx) 完全对齐。

        按 ``(_maskStamp, hf.stamp, maxDim)`` 记忆化；未填色返回 None。
        """
        if self._colorMask is None:
            return None
        hf = self._hf
        maxDim = int(maxDim)
        key = (self._maskStamp, hf.stamp, maxDim)
        cached = self._lodMaskCache.get(key)
        if cached is not None:
            return cached
        ny, nx = hf.ny, hf.nx
        if max(ny, nx) <= maxDim:
            step = 1
        else:
            step = int(math.ceil(max(ny, nx) / float(maxDim)))
        sub = np.ascontiguousarray(self._colorMask[::step, ::step])
        self._lodMaskCache[key] = sub
        return sub

    # ---------------- 变换（累积到 _place，无损；仅 XY） ----------------

    def translate(self, dx: float, dy: float, dz: float) -> None:
        # 忽略 dz：Z（高度）不可经变换改变
        T = np.eye(3, dtype=np.float64)
        T[0, 2] += float(dx)
        T[1, 2] += float(dy)
        self._place = T @ self._place

    def rotate(self, angle: float, ax: float, ay: float, az: float) -> None:
        # 仅绕 Z 轴有效（2.5D 高度场不倾斜）；旋转关于世界原点，
        # gizmo 以 translate(-c)/rotate/translate(+c) 括号实现绕任意中心旋转。
        if abs(az) < 1e-9:
            return
        c, s = np.cos(float(angle)), np.sin(float(angle))
        R = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]], dtype=np.float64)
        self._place = R @ self._place

    def scale(self, sx: float, sy: float, sz: float) -> None:
        # 忽略 sz：Z 锁定；负值=镜像（烘焙时重采样翻转）
        S = np.eye(3, dtype=np.float64)
        S[0, 0] = float(sx)
        S[1, 1] = float(sy)
        self._place = S @ self._place

    def bake(self, dst_spacing: float | None = None, matrix=None) -> None:
        """重构：把放置变换烘焙进网格（重采样），复位 _place 为单位阵。

        matrix: 可选自定义 local->world 仿射（默认用当前 ``_place``）。重构命令需要
                改变世界尺寸时传入 ``S·_place``（先缩放到目标尺寸再烘焙）。
        """
        M = self._place if matrix is None else np.asarray(matrix, dtype=np.float64)
        old_hf = self._hf
        old_mask = self._colorMask
        new_hf = old_hf.bake_by_affine(M, dst_spacing)
        # 色号蒙版随网格一起烘焙：最近邻重采样到新网格（保住调色板索引不被插值污染）
        if old_mask is not None:
            from core.relief.resample import resample_affine_nearest
            inv_M = np.linalg.inv(np.asarray(M, dtype=np.float64))
            new_mask = resample_affine_nearest(
                old_mask, old_hf.origin, old_hf.spacing, inv_M,
                new_hf.origin, new_hf.nx, new_hf.ny, new_hf.spacing)
        else:
            new_mask = None
        self._hf = new_hf
        self._colorMask = new_mask
        self._place = np.eye(3, dtype=np.float64)
        self._maskStamp += 1
        self._lodMaskCache.clear()
        self.bumpVersion()

    # ---------------- 几何查询 ----------------

    def getBoundingBox(self) -> BoundingBox3D:
        x0, y0, x1, y1 = self._hf.bbox()
        M = self._place
        z_max = float(self._hf.data.max()) if self._hf.data.size else 0.0
        z_max = max(0.0, z_max)
        bb = BoundingBox3D()
        for cx, cy in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            wx = M[0, 0] * cx + M[0, 1] * cy + M[0, 2]
            wy = M[1, 0] * cx + M[1, 1] * cy + M[1, 2]
            bb.expand(Point3D(wx, wy, 0.0))
            bb.expand(Point3D(wx, wy, z_max))
        return bb

    def getPosition(self) -> Point3D:
        M = self._place
        ox, oy = self._hf.origin
        return Point3D(M[0, 0] * ox + M[0, 1] * oy + M[0, 2],
                       M[1, 0] * ox + M[1, 1] * oy + M[1, 2], 0.0)

    def pathLength(self) -> float:
        return 0.0

    def enclosedArea(self) -> float | None:
        return None

    def clone(self) -> "ReliefModelObject":
        o = ReliefModelObject(
            HeightField(self._hf.data.copy(), self._hf.origin, self._hf.spacing))
        o._place = self._place.copy()
        o._displayMode = self._displayMode
        o._lightAzimuth = self._lightAzimuth
        o._lightAltitude = self._lightAltitude
        o._zExaggeration = self._zExaggeration
        o._gridDivisions = self._gridDivisions
        o._display3DMode = self._display3DMode
        o._material = self._material
        o._color = self._color
        o._lineWidth = self._lineWidth
        o._colorMask = None if self._colorMask is None else self._colorMask.copy()
        o._maskStamp = self._maskStamp
        return o

    # ---------------- 序列化 / 撤销快照 ----------------

    def _commonDict(self) -> dict:
        """文件与撤销快照共用的元数据字段（基础属性 + 网格/显示参数）。"""
        d = super().serialize()
        hf = self._hf
        d["origin"] = [hf.origin[0], hf.origin[1]]
        d["spacing"] = hf.spacing
        d["dims"] = [hf.nx, hf.ny]
        d["place"] = self._place.tolist()
        d["display"] = {"mode": self._displayMode, "azimuth": self._lightAzimuth,
                        "altitude": self._lightAltitude, "zexag": self._zExaggeration,
                        "gridDivisions": self._gridDivisions,
                        "mode3d": self._display3DMode, "material": self._material}
        return d

    def serialize(self) -> dict:
        d = self._commonDict()
        hf = self._hf
        raw = np.ascontiguousarray(hf.data, dtype=np.float32).tobytes()
        d["grid"] = base64.b64encode(zlib.compress(raw, 6)).decode("ascii")
        # 色号蒙版（区域划分）：稀疏（多为 0）→ zlib 压缩极小；全空/形状不符则省略
        cm = self._colorMask
        if cm is not None and cm.shape == (hf.ny, hf.nx) and bool(cm.any()):
            d["colorMask"] = base64.b64encode(zlib.compress(
                np.ascontiguousarray(cm, dtype=np.uint8).tobytes(), 6)).decode("ascii")
        return d

    def snapshot(self) -> dict:
        """撤销快照：字段与 serialize 相同，但网格/蒙版存原始字节（免 zlib/base64，
        各一次 memcpy）。内容比较走 matchesSnapshot 的 stamp 签名（O(1)），
        本方法只在撤销项真正入栈时调用。"""
        d = self._commonDict()
        hf = self._hf
        # 签名字段放前面：endEdit 的字典全等比较在内容变化时先在这些键上快速失败
        d["hfStamp"] = hf.stamp
        d["version"] = self._version
        d["maskStamp"] = self._maskStamp
        d["grid_raw"] = np.ascontiguousarray(hf.data, dtype=np.float32).tobytes()
        cm = self._colorMask
        if cm is not None and cm.shape == (hf.ny, hf.nx) and bool(cm.any()):
            d["colorMask_raw"] = np.ascontiguousarray(cm, dtype=np.uint8).tobytes()
        return d

    def restoreSnapshot(self, snap: dict) -> None:
        """由 snapshot() 恢复（原始字节路径，免 zlib/base64 解码）。"""
        origin, sp, nx, ny = self._restoreMetadata(snap)
        raw = snap.get("grid_raw")
        if raw:
            arr = np.frombuffer(raw, dtype=np.float32).reshape((ny, nx)).copy()
        else:
            arr = np.zeros((ny, nx), dtype=np.float32)
        self._hf = HeightField(arr, origin, sp)
        cm = snap.get("colorMask_raw")
        if cm:
            self._colorMask = np.frombuffer(cm, dtype=np.uint8).reshape((ny, nx)).copy()
        else:
            self._colorMask = None
        self._finishRestore()

    def matchesSnapshot(self, snap: dict) -> bool:
        """O(1) 快照比较：母场 stamp + 内容 version + 蒙版 stamp + 放置阵。
        （撤销快照按引用持有旧数组，内容编辑走 setData/commitRoi 推进 stamp，
        显示/蒙版/放置各有计数或逐一比较，覆盖全部可撤销变更路径。）"""
        if "hfStamp" not in snap:
            return self.serialize() == snap
        return (self._hf.stamp == snap.get("hfStamp")
                and self._version == snap.get("version")
                and self._maskStamp == snap.get("maskStamp")
                and np.array_equal(
                    self._place,
                    np.asarray(snap.get("place"), dtype=np.float64)))

    def snapshotSize(self, snap: dict) -> int:
        return int(self._hf.data.nbytes) + \
            (int(self._colorMask.nbytes) if self._colorMask is not None else 0) + 4096

    def _restoreMetadata(self, data: dict) -> tuple:
        """应用基础属性与元数据（origin/spacing/dims/place/display）。
        返回 (origin, spacing, nx, ny) 供建场。"""
        super().deserialize(data)
        o = data.get("origin", [0.0, 0.0])
        sp = float(data.get("spacing", 1.0))
        dims = data.get("dims", [2, 2])
        nx, ny = int(dims[0]), int(dims[1])
        pl = data.get("place")
        self._place = np.array(pl, dtype=np.float64) if pl else np.eye(3, dtype=np.float64)
        disp = data.get("display", {}) or {}
        self._displayMode = disp.get("mode", self._displayMode)
        if self._displayMode not in DISPLAY_MODES:
            self._displayMode = "contour"
        self._lightAzimuth = float(disp.get("azimuth", self._lightAzimuth))
        self._lightAltitude = float(disp.get("altitude", self._lightAltitude))
        self._zExaggeration = float(disp.get("zexag", self._zExaggeration))
        self._gridDivisions = int(disp.get("gridDivisions", self._gridDivisions))
        self._display3DMode = disp.get("mode3d", self._display3DMode)
        if self._display3DMode not in DISPLAY_3D_MODES:
            self._display3DMode = "material"
        self._material = disp.get("material", self._material)
        if self._material not in MATERIALS:
            self._material = "clay"
        return (float(o[0]), float(o[1])), sp, nx, ny

    def _finishRestore(self) -> None:
        """重建蒙版/几何缓存计数（与 deserialize 收尾一致）。"""
        self._maskStamp += 1
        self._lodMaskCache.clear()
        self.bumpVersion()

    def summaryDict(self) -> dict:
        """属性面板用的轻量摘要（不含网格 blob，避免刷屏）。"""
        d = super().serialize()
        hf = self._hf
        d["origin"] = [round(hf.origin[0], 3), round(hf.origin[1], 3)]
        d["spacing"] = round(hf.spacing, 4)
        d["dims"] = [hf.nx, hf.ny]
        d["size_mm"] = [round(v, 2) for v in hf.size_mm()]
        d["memory_mb"] = round(hf.memory_mb(), 2)
        d["display"] = self._displayMode
        d["display3d"] = self._display3DMode
        d["material"] = self._material
        d["pendingTransform"] = bool(self.hasPendingTransform())
        return d

    def deserialize(self, data: dict) -> None:
        origin, sp, nx, ny = self._restoreMetadata(data)
        g = data.get("grid")
        if g:
            raw = zlib.decompress(base64.b64decode(g))
            arr = np.frombuffer(raw, dtype=np.float32).reshape((ny, nx)).copy()
        else:
            arr = np.zeros((ny, nx), dtype=np.float32)
        self._hf = HeightField(arr, origin, sp)
        cm = data.get("colorMask")
        if cm:
            raw = zlib.decompress(base64.b64decode(cm))
            self._colorMask = np.frombuffer(raw, dtype=np.uint8).reshape((ny, nx)).copy()
        else:
            self._colorMask = None
        self._finishRestore()

"""高度场（2.5D 浮雕母场）：规则栅格 + 单通道 float32 ndarray。

这是全项目浮雕数据的唯一真相源，纯算法、零 UI 依赖、不 import 任何 Qt。

约定（钉死，勿改）：
- 母场 ``data`` 形状 ``(ny, nx)``，``dtype=float32``，单位 mm，存曲面相对基面
  (z=0) 的高度；``data[row, col]`` 中 row=Y 方向、col=X 方向。
- 行序：**行 0 = minY**、列 0 = minX；即 ``origin`` 是栅格节点 (0,0) 的世界坐标
  （左下角）。此约定与 GL 纹理 t=0 在底部一致，故上传纹理无需翻转。
- 世界 mm ↔ 栅格索引的换算只发生在本类的方法内（单位纪律：算子禁止裸写像素）。
- ndarray → 纹理行序的换算只发生在 :meth:`HeightField.to_texture_array` 这一个
  边界函数里。
"""
from __future__ import annotations

import itertools
import math
from typing import Tuple

import numpy as np

# 内存钳制上限：4096² float32 ≈ 64MB，超过则自动加粗 spacing（见 from_bbox）。
# 4096² 已支持到 400mm@0.1mm；再大只会拖垮交互与撤销内存，故设为安全护栏。
MAX_PIXELS = 4096 * 4096

# 数据版本号发号器：每次母场数组被替换（setData/构造）推进一次，
# 供撤销快照按 stamp 廉价比较“内容是否变化”（不逐格比对、不压缩）。
_STAMP = itertools.count(1)


class HeightField:
    """单值高度场：一块覆盖自身包围盒的规则栅格。

    一个浮雕模型 = 一个 HeightField，只覆盖自身范围，禁止全局大画布。
    """

    def __init__(self, data: np.ndarray, origin: Tuple[float, float],
                 spacing: float) -> None:
        if spacing <= 0:
            raise ValueError("spacing 必须为正（mm/px）")
        arr = np.asarray(data)
        if arr.ndim != 2:
            raise ValueError("高度场 data 必须是二维 (ny, nx)")
        # 强制 float32 + C 连续：GPU 上传与内存估算都依赖这个布局
        self._data = np.ascontiguousarray(arr, dtype=np.float32)
        self._origin = (float(origin[0]), float(origin[1]))
        self._spacing = float(spacing)
        self._stamp = next(_STAMP)
        # from_bbox 触发内存钳制时置 True，requested_spacing 记录用户原始精度
        self.clamped: bool = False
        self.requested_spacing: float = self._spacing

    # ---------------- 只读属性 ----------------

    @property
    def data(self) -> np.ndarray:
        return self._data

    @property
    def origin(self) -> Tuple[float, float]:
        return self._origin

    def setOrigin(self, xy: Tuple[float, float]) -> None:
        """设置栅格 (row=0, col=0) 节点的世界坐标。不触发 stamp 更新（仅用于生成时定位）。"""
        self._origin = (float(xy[0]), float(xy[1]))

    def setSpacing(self, spacing: float) -> None:
        """重设栅格间距 (mm/px)；内部数据栅格不重采样，直接改变世界物理尺寸。"""
        s = float(spacing)
        if s <= 0:
            raise ValueError("spacing 必须为正（mm/px）")
        self._spacing = s
        if not getattr(self, "clamped", False):
            self.requested_spacing = s

    @property
    def spacing(self) -> float:
        return self._spacing

    @property
    def stamp(self) -> int:
        """数据版本号：母场数组每被替换一次即变化（撤销快照比较用）。"""
        return self._stamp

    @property
    def nx(self) -> int:
        return int(self._data.shape[1])

    @property
    def ny(self) -> int:
        return int(self._data.shape[0])

    def size_mm(self) -> Tuple[float, float]:
        """覆盖的世界尺寸 (宽, 高) mm。"""
        return ((self.nx - 1) * self._spacing, (self.ny - 1) * self._spacing)

    def bbox(self) -> Tuple[float, float, float, float]:
        """世界包围盒 (minX, minY, maxX, maxY) mm。"""
        w, h = self.size_mm()
        x0, y0 = self._origin
        return (x0, y0, x0 + w, y0 + h)

    def memory_mb(self) -> float:
        """母场占用内存（MB），供面板实时预估。"""
        return self._data.nbytes / (1024.0 * 1024.0)

    # ---------------- 工厂 ----------------

    @staticmethod
    def grid_size(width_mm: float, height_mm: float,
                  spacing: float) -> Tuple[int, int, float, bool]:
        """由世界尺寸 + 精度算栅格 (nx, ny, 实际spacing, 是否被钳制)。

        纯函数，供面板预估与 from_bbox 共用同一套钳制逻辑。内存上限从
        ``core.app_settings.max_field_pixels()`` 实时读取（设置页可调）。
        """
        if spacing <= 0:
            raise ValueError("spacing 必须为正（mm/px）")
        try:
            import core.app_settings as _s
            max_pixels = int(_s.max_field_pixels())
        except Exception:
            max_pixels = MAX_PIXELS
        w = max(0.0, float(width_mm))
        h = max(0.0, float(height_mm))
        sp = float(spacing)
        nx = max(2, int(math.ceil(w / sp)) + 1)
        ny = max(2, int(math.ceil(h / sp)) + 1)
        clamped = False
        if nx * ny > max_pixels:
            # 由世界尺寸直接求所需 spacing：使 (w/sp)*(h/sp) ≈ max_pixels。
            # 只加粗不细化；+1 取整可能仍略超，再用保险循环逐步加粗至落入上限。
            sp = max(sp, math.sqrt(max(w, 1e-9) * max(h, 1e-9) / float(max_pixels)))
            nx = max(2, int(math.ceil(w / sp)) + 1)
            ny = max(2, int(math.ceil(h / sp)) + 1)
            while nx * ny > max_pixels:
                sp *= 1.01
                nx = max(2, int(math.ceil(w / sp)) + 1)
                ny = max(2, int(math.ceil(h / sp)) + 1)
            clamped = True
        return nx, ny, sp, clamped

    @classmethod
    def from_bbox(cls, bbox_min: Tuple[float, float],
                  bbox_max: Tuple[float, float], margin: float,
                  spacing: float) -> "HeightField":
        """按 [包围盒 + 余量] 建平坦高度场（复刻精雕“新建模型”）。

        bbox_min/bbox_max: 选中曲线包围盒的 (x, y)（世界 mm）。
        margin: 四周余量 mm；spacing: 精度 mm/px。
        """
        margin = max(0.0, float(margin))
        x0 = float(bbox_min[0]) - margin
        y0 = float(bbox_min[1]) - margin
        x1 = float(bbox_max[0]) + margin
        y1 = float(bbox_max[1]) + margin
        nx, ny, sp, clamped = cls.grid_size(x1 - x0, y1 - y0, spacing)
        hf = cls(np.zeros((ny, nx), dtype=np.float32), (x0, y0), sp)
        hf.clamped = clamped
        hf.requested_spacing = float(spacing)
        return hf

    # ---------------- 重构：仿射烘焙（重采样） ----------------

    def bake_by_affine(self, M, dst_spacing: float | None = None) -> "HeightField":
        """把放置仿射 ``M``（local->world）烘焙进网格：在世界 AABB 上重采样出
        一个新的轴对齐高度场（唯一有损的重采样步骤，供 ReliefModelObject“重构”）。

        M: 3x3 仿射，world = M · [lx, ly, 1]，local 即本网格自身的 origin/spacing 坐标。
        dst_spacing: 目标精度 mm/px；默认沿用当前 spacing（保持物理分辨率），
                     超 MAX_PIXELS 时由 grid_size 自动加粗。
        """
        from .resample import resample_affine

        M = np.asarray(M, dtype=np.float64)
        x0, y0, x1, y1 = self.bbox()
        corners = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
        wxs = []
        wys = []
        for cx, cy in corners:
            wxs.append(M[0, 0] * cx + M[0, 1] * cy + M[0, 2])
            wys.append(M[1, 0] * cx + M[1, 1] * cy + M[1, 2])
        min_x, max_x = min(wxs), max(wxs)
        min_y, max_y = min(wys), max(wys)

        sp = float(dst_spacing) if dst_spacing else self._spacing
        nx, ny, sp, clamped = self.grid_size(max_x - min_x, max_y - min_y, sp)
        inv_M = np.linalg.inv(M)
        data = resample_affine(self, inv_M, (min_x, min_y), nx, ny, sp)
        hf = HeightField(data, (min_x, min_y), sp)
        hf.clamped = clamped
        hf.requested_spacing = float(dst_spacing) if dst_spacing else self.requested_spacing
        return hf

    # ---------------- 世界 ↔ 栅格映射 ----------------

    def world_to_grid(self, x: float, y: float) -> Tuple[float, float]:
        """世界 mm → 栅格 (col, row)（浮点，可为分数索引）。"""
        x0, y0 = self._origin
        return ((float(x) - x0) / self._spacing, (float(y) - y0) / self._spacing)

    def grid_to_world(self, col: float, row: float) -> Tuple[float, float]:
        """栅格 (col, row) → 世界 mm (x, y)。"""
        x0, y0 = self._origin
        return (x0 + float(col) * self._spacing, y0 + float(row) * self._spacing)

    def sample_bilinear(self, x: float, y: float) -> float:
        """世界坐标双线性采样高度（mm）；越界返回 0.0。"""
        col, row = self.world_to_grid(x, y)
        nx, ny = self.nx, self.ny
        if col < 0 or row < 0 or col > nx - 1 or row > ny - 1:
            return 0.0
        c0 = int(math.floor(col))
        r0 = int(math.floor(row))
        c1 = min(c0 + 1, nx - 1)
        r1 = min(r0 + 1, ny - 1)
        tc = col - c0
        tr = row - r0
        d = self._data
        top = d[r0, c0] * (1 - tc) + d[r0, c1] * tc
        bot = d[r1, c0] * (1 - tc) + d[r1, c1] * tc
        return float(top * (1 - tr) + bot * tr)

    # ---------------- 区域读写（算子脏 ROI 用） ----------------

    def _clamp_roi(self, roi: Tuple[int, int, int, int]):
        col0, row0, col1, row1 = (int(v) for v in roi)
        col0 = max(0, min(col0, self.nx))
        col1 = max(0, min(col1, self.nx))
        row0 = max(0, min(row0, self.ny))
        row1 = max(0, min(row1, self.ny))
        return col0, row0, col1, row1

    def get_region(self, roi: Tuple[int, int, int, int]) -> np.ndarray:
        """取 roi=(col0,row0,col1,row1)（末端开区间）内子场副本。"""
        col0, row0, col1, row1 = self._clamp_roi(roi)
        return self._data[row0:row1, col0:col1].copy()

    def set_region(self, roi: Tuple[int, int, int, int], values) -> None:
        """写入 roi 内子场（values 形状需匹配或可广播）。就地修改，
        仅用于尚未被任何撤销快照引用的新建场（如新建模型基面填充）。"""
        col0, row0, col1, row1 = self._clamp_roi(roi)
        if col1 <= col0 or row1 <= row0:
            return
        self._data[row0:row1, col0:col1] = values

    # ---------------- copy-on-write 提交（撤销安全） ----------------

    def setData(self, arr) -> None:
        """替换母场数据（copy-on-write 提交点）：换上新数组并推进 stamp。

        旧数组保持不变，供已存在的撤销快照/op 按引用安全持有（绝不就地改写
        可能被共享的数组）。形状必须与当前母场一致。强制深拷贝——
        ascontiguousarray 对已是 float32/C 连续的输入原样返回同一对象，
        调用方随后改该数组会绕过 stamp 静默改场。
        """
        a = np.array(arr, dtype=np.float32, copy=True, order="C")
        if a.shape != self._data.shape:
            raise ValueError("setData 形状必须与母场一致")
        self._data = a
        self._stamp = next(_STAMP)

    def commitRoi(self, roi: Tuple[int, int, int, int], values) -> None:
        """copy-on-write 写入 roi 子场：先整场复制再写 ROI 并推进 stamp。

        区域浮雕等“已提交场”的内容编辑一律走此路径，保证旧数组不被就地
        改写（撤销快照按引用持有旧数组仍成立）。ROI 小时复制成本仅一次 memcpy。
        """
        col0, row0, col1, row1 = self._clamp_roi(roi)
        if col1 <= col0 or row1 <= row0:
            return
        new = self._data.copy()
        new[row0:row1, col0:col1] = values
        self.setData(new)

    # ---------------- 预览 / 渲染边界 ----------------

    def downsample(self, max_dim: int = 1024) -> "HeightField":
        """等比降采样出预览副本（长边 ≤ max_dim）；已足够小则返回自身。

        行/列均匀取样且必含末行末列（linspace）：[::step] 会丢掉末尾
        (step−1) 行/列，场尺寸随降采样系统性缩小、丢边界内容。
        """
        if max(self.nx, self.ny) <= max_dim:
            return self
        step = int(math.ceil(max(self.nx, self.ny) / float(max_dim)))

        def _take(n: int) -> np.ndarray:
            return np.unique(np.linspace(
                0, n - 1, max(2, int(math.ceil(n / step))))
                .round().astype(np.intp))

        rows = _take(self.ny)
        cols = _take(self.nx)
        sub = self._data[np.ix_(rows, cols)]
        # 名义栅距按较长轴平均步距：末样本恰好落在原场边界上，
        # 总尺寸不再缩小（短轴误差 < 1 个粗格，预览级可忽略）
        (n_long, c_long) = max((self.ny, len(rows)), (self.nx, len(cols)))
        sp = float(self._spacing * (n_long - 1) / max(c_long - 1, 1))
        return HeightField(sub, self._origin, sp)

    def padded(self, margin: float) -> "HeightField":
        """四周外扩 ``margin`` mm 并补 0（基准面），返回新场。

        保持原 spacing（不参与 grid_size 钳制，避免扩边改变精度导致原数据
        粘贴错位）；四边各扩 ``round(margin/spacing)`` 格，栅格实际余量 ≥
        margin。margin <= 0 返回自身。供“重构网格”加边界余量使用。
        """
        margin = max(0.0, float(margin))
        if margin <= 0:
            return self
        pad = max(1, int(round(margin / self._spacing)))
        ny, nx = self.ny + 2 * pad, self.nx + 2 * pad
        new = np.zeros((ny, nx), dtype=np.float32)
        new[pad:pad + self.ny, pad:pad + self.nx] = self._data
        hf = HeightField(
            new,
            (self._origin[0] - pad * self._spacing,
             self._origin[1] - pad * self._spacing),
            self._spacing)
        hf.clamped = self.clamped
        hf.requested_spacing = self.requested_spacing
        return hf

    def to_texture_array(self) -> np.ndarray:
        """ndarray → 纹理行序的唯一边界函数（连续 float32）。

        在“行 0 = minY”约定下，GL 纹理 t=0 位于底部即 minY，故此处为恒等
        （不翻转）。任何渲染层若需要顶向下的显示行序，只能在此函数内调整，
        其余代码不得散写行序翻转。
        """
        return np.ascontiguousarray(self._data, dtype=np.float32)

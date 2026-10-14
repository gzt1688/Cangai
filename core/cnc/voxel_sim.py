"""实体模拟核心：体素化坯料 + 刀具扫掠切削。

原理：毛坯按分辨率 res 离散成 3D bool 数组（True=材料存在）。模拟播放时，
刀具沿 G1/G2/G3 切削段运动，把刀体扫过的体素置 False（被切掉）；G0 快移
（抬刀）不切削。剩余体素的暴露表面提取为三角网格，交给 GPU B-Rep 管线
渲染，形成"坯料被实体刀具真实雕刻"的效果。

坐标约定（与 G 代码一致）：
- G 代码 X/Y 以毛坯中心为原点（X=0,Y=0 在毛坯中心）
- G 代码 Z=0 为毛坯顶面，向下为负（切深为负值）
- 体素内部坐标：x∈[0,w], y∈[0,h], z∈[0,t]（z 从底面向上）
- 刀尖 G 代码 (xg,yg,zg) → 体素网格 (xg + w/2, yg + h/2, zg + t/2)
"""
from __future__ import annotations

import math
from typing import List, Optional, Tuple

import numpy as np


def ensure_stock_voxel(state) -> Optional[VoxelStock]:
    """确保 state.simVoxel 已按当前毛坯尺寸构建（勾选"实体"时直接调用）。

    毛坯尺寸变化时重建。返回 VoxelStock 或 None。
    """
    try:
        st = state.stock
        w, h, t = float(st.width), float(st.height), float(st.thickness)
    except Exception:
        return None
    vs = state.simVoxel
    if vs is not None and abs(vs.width - w) < 1e-9 \
            and abs(vs.height - h) < 1e-9 \
            and abs(vs.thickness - t) < 1e-9:
        return vs
    # 实体模拟分辨率：最大边 /200（100mm → 0.5mm 格）。高度场内核
    # 每列一个 float32、扫掠/提网格全向量化，/200 网格最坏（50x 速度
    # 密集路径）单 tick ~25ms，仍在 50ms 预算内；0.5mm 格距下 3mm 刀
    # 具的凹槽有 6~7 格宽，配合覆盖率渐变 + Gouraud 法线足够光滑。
    res = max(0.4, max(w, h, t) / 200.0)
    vs = HeightfieldStock(w, h, t, res)
    vs.reset()
    state.simVoxel = vs
    return vs


def clear_stock_voxel(state) -> None:
    """清空体素坯料（取消勾选"实体"时调用）。"""
    state.simVoxel = None


def _tool_radii(tool) -> List[Tuple[float, float, float, float]]:
    """刀具沿 Z 的半径分段 [(z0, z1, r_start, r_end)]（相对刀尖，向上）。

    tool: pymillcam Tool（tool.geometry dict + tool.shape）。
    分段：球头（BALLNOSE）z∈[0,r] 球面包络分段线性化 → 刃段剩余
    （锥度刀线性增大，封顶柄半径）→ 刀柄（固定直径）。
    平底/牛鼻/锥度刀：刃段从刃径线性变化到 r_end（牛鼻圆角近似为平底）。
    """
    g = tool.geometry
    dia = float(g.get("diameter", 3.0))
    shank = float(g.get("shank_diameter", dia))
    total = float(g.get("total_length", 50.0))
    flute = float(g.get("flute_length", min(15.0, total)))
    taper_angle = float(g.get("taper_angle", 0.0))
    r_tip = dia * 0.5
    r_shank = shank * 0.5
    is_ball = False
    try:
        from pymillcam.core.tools import ToolShape
        if getattr(tool, "shape", None) == ToolShape.BALLNOSE:
            is_ball = True
    except Exception:
        if "BALL" in str(getattr(tool, "shape", "") or "").upper():
            is_ball = True
    segs: List[Tuple[float, float, float, float]] = []
    z = 0.0
    if flute > 1e-9:
        r_end = r_tip
        if taper_angle > 1e-9:
            # 锥度刀：从刃径线性增大，封顶柄半径
            r_end = min(r_shank, r_tip + flute * math.tan(
                math.radians(taper_angle)))
        if is_ball and r_tip > 1e-9:
            # 球头：z∈[0,r_tip] 半径 = sqrt(r²-(r-z)²)（尖点为 0），分段线性化
            n = 12
            z_prev = 0.0
            r_prev = 0.0
            for k in range(1, n + 1):
                zk = r_tip * k / n
                rk = math.sqrt(max(r_tip * r_tip - (r_tip - zk) ** 2, 0.0))
                if zk > z_prev + 1e-9:
                    segs.append((z_prev, zk, r_prev, rk))
                z_prev, r_prev = zk, rk
            if flute > r_tip + 1e-9:
                # 球头以上剩余刃长（锥度刀继续线性增大）
                taper_len = flute - r_tip
                r_up = r_tip if taper_angle <= 1e-9 else min(
                    r_shank, r_tip + taper_len * math.tan(
                        math.radians(taper_angle)))
                segs.append((r_tip, flute, r_tip, r_up))
        else:
            segs.append((z, flute, r_tip, r_end))
        z = flute
    if total > z + 1e-9:
        segs.append((z, total, r_shank, r_shank))
    return segs


class HeightfieldStock:
    """2.5D 高度场坯料 + 刀具底刃包络切削（3 轴铣专用，丝滑模拟内核）。

    3 轴加工不存在欠切方向：任一 (x,y) 列的材料只从顶面向下被去除，
    所以每列只需存"剩余材料顶面高度" h[i,j]（自毛坯底面向上，0=切穿）。
    扫掠时把刀具底刃包络（水平距离 → 刀具底面高度 的 2D 模板）沿路径
    平移，对覆盖区域做 np.minimum——相比 3D bool 体素逐层布尔操作，
    数据量/计算量降 nz 倍且无 Python 逐层循环；与渲染端 2.5D 高度场
    (top_mesh) 天然一致。

    对外接口与 VoxelStock 相同：reset / sweep / replay / top_mesh /
    revision，可直接被模拟面板与 3D 视口使用。
    """

    def __init__(self, width: float, height: float, thickness: float,
                 res: Optional[float] = None) -> None:
        self.width = float(width)
        self.height = float(height)
        self.thickness = float(thickness)
        if res is None:
            res = max(0.8, max(self.width, self.height, self.thickness) / 120.0)
        self.res = float(res)
        self.nx = max(2, int(round(self.width / self.res)))
        self.ny = max(2, int(round(self.height / self.res)))
        self.nz = max(2, int(round(self.thickness / self.res)))
        self.res_x = self.width / self.nx
        self.res_y = self.height / self.ny
        self.res_z = self.thickness / self.nz
        self.h: np.ndarray | None = None   # (ny, nx) 材料顶面高度
        self.revision = 0
        self.surf_cache = None             # (revision, top_mesh 结果) 缓存
        self._profile_cache: dict = {}

    # ------------------------------------------------------------- 构建/重置
    def reset(self) -> None:
        """重新填充完整毛坯高度场。"""
        self.h = np.full((self.ny, self.nx), self.thickness, dtype=np.float32)
        self.revision += 1
        self.surf_cache = None

    # ------------------------------------------------------------- 刀具模板
    def _profile(self, tool):
        """刀具底刃包络模板（含边界覆盖率，用于轮廓抗锯齿）。

        返回 (prof, cov, (di0, di1, dj0, dj1))：
        prof[j,i] = 距刀轴水平距离 d 处刀具底面高度（相对刀尖，向上；
        刀轴外 = +inf）；cov[j,i] = 该格被刀具覆盖的比例 0~1——边界格
        按半径穿越格子的位置渐变（中心正好在刃缘=0.5），切削时按覆盖率
        混合切深，圆形轮廓平滑无台阶。按刀具几何缓存。
        """
        g = tool.geometry
        key = (float(g.get("diameter", 0.0)), float(g.get("shank_diameter", 0.0)),
               float(g.get("flute_length", 0.0)), float(g.get("total_length", 0.0)),
               float(g.get("taper_angle", 0.0)), str(getattr(tool, "shape", "")),
               self.res_x, self.res_y, self.res_z)
        hit = self._profile_cache.get(key)
        if hit is not None:
            return hit
        segs = _tool_radii(tool)
        rmax = max((r for _z0, _z1, _r0, r in segs), default=1.0)
        span_i = int(math.ceil(rmax / self.res_x)) + 1
        span_j = int(math.ceil(rmax / self.res_y)) + 1
        di0, di1 = -span_i, span_i + 1
        dj0, dj1 = -span_j, span_j + 1
        ii, jj = np.mgrid[di0:di1, dj0:dj1]
        d = np.sqrt((ii * self.res_x) ** 2 + (jj * self.res_y) ** 2)
        prof = np.full(d.shape, np.inf, dtype=np.float32)
        cov = np.zeros(d.shape, dtype=np.float32)
        zmax = segs[-1][1] if segs else 1.0
        dz = 0.5 * self.res_z
        zs = np.arange(0.0, zmax + dz * 0.5, dz)
        # 各采样高度上的刀具半径：分段线性（球头=球面分段、锥度=线性、
        # 柄段恒定），r_start/r_end 由分段自带
        rad = np.zeros(zs.shape, dtype=np.float64)
        for z0, z1, r0, r1 in segs:
            m = (zs >= z0 - 1e-9) & (zs <= z1 + 1e-9)
            t = np.clip((zs[m] - z0) / max(z1 - z0, 1e-9), 0.0, 1.0)
            rad[m] = r0 + (r1 - r0) * t
        # 底刃包络 + 覆盖率：以真实刃缘为准（不加半格余量——余量会把
        # 刀具半径外的格子也部分切削，雕刻区周围出现一圈被擦伤的灰环）。
        # 边界格覆盖率 = 格心相对刃缘的位置渐变：格心在刃缘上 = 0.5，
        # 向外半格 = 0（不切），向内半格 = 1（全切）。
        res_m = 0.5 * (self.res_x + self.res_y) * 1.25   # 过渡带宽 1.25 格
        for k in range(len(zs)):
            marg = rad[k] - d              # >0 = 格心在刃缘内
            hit_k = marg > 0.0
            newly = hit_k & np.isinf(prof)
            prof = np.where(hit_k,
                            np.minimum(prof, np.float32(zs[k])), prof)
            cov = np.where(newly,
                           np.clip(0.5 + marg / res_m, 0.0, 1.0).astype(
                               np.float32), cov)
        self._profile_cache[key] = (prof, cov, (di0, di1, dj0, dj1))
        return prof, cov, (di0, di1, dj0, dj1)

    # ------------------------------------------------------------- 扫掠切削
    def sweep(self, p0: Tuple[float, float, float],
              p1: Tuple[float, float, float], tool) -> None:
        """沿 G 代码线段 p0→p1 扫掠刀具（区域 np.minimum，密集采样）。"""
        if self.h is None or p0 == p1:
            return
        prof, cov, (di0, di1, dj0, dj1) = self._profile(tool)
        # 采样步距：刀具足印（直径 ≥ 2×格距）下 3/4 格距即无缝覆盖，
        # 再密只会增加采样数不改变切削结果
        step = max(self.res_x, self.res_y) * 0.75
        d = math.sqrt((p1[0] - p0[0]) ** 2 + (p1[1] - p0[1]) ** 2 +
                      (p1[2] - p0[2]) ** 2)
        n = max(1, int(math.ceil(d / step)))
        h = self.h
        changed = False
        for k in range(n + 1):
            t = k / n
            if self._stamp((p0[0] + (p1[0] - p0[0]) * t,
                            p0[1] + (p1[1] - p0[1]) * t,
                            p0[2] + (p1[2] - p0[2]) * t),
                           prof, cov, di0, di1, dj0, dj1, h):
                changed = True
        if changed:
            self.revision += 1
            self.surf_cache = None

    def sweep_path(self, pts: list, tool) -> None:
        """沿折线批量扫掠（密集精加工路径优化）。

        pts: 刀尖位置序列 [(x,y,z)]（G 代码系），连续切削段合并为一次
        调用：刀具模板只取一次、revision/缓存只失效一次，把"每段一次
        sweep() 调用"的 Python 开销从 段数 次降到 1 次。
        """
        if self.h is None or len(pts) < 2:
            return
        prof, cov, (di0, di1, dj0, dj1) = self._profile(tool)
        step = max(self.res_x, self.res_y) * 0.75
        h = self.h
        changed = False
        # 折线起点先落一戳（与逐段 sweep 的 t=0 采样一致）
        self._stamp(pts[0], prof, cov, di0, di1, dj0, dj1, h)
        changed = True
        prev = pts[0]
        for p1 in pts[1:]:
            p0 = prev
            prev = p1
            if p0 == p1:
                continue
            d = math.sqrt((p1[0] - p0[0]) ** 2 + (p1[1] - p0[1]) ** 2 +
                          (p1[2] - p0[2]) ** 2)
            n = max(1, int(math.ceil(d / step)))
            for k in range(1, n + 1):
                t = k / n
                if self._stamp((p0[0] + (p1[0] - p0[0]) * t,
                                p0[1] + (p1[1] - p0[1]) * t,
                                p0[2] + (p1[2] - p0[2]) * t),
                               prof, cov, di0, di1, dj0, dj1, h):
                    changed = True
        if changed:
            self.revision += 1
            self.surf_cache = None

    def _stamp(self, p: Tuple[float, float, float], prof, cov, di0, di1,
               dj0, dj1, h) -> bool:
        """刀尖落在 p（G 代码系）处盖一次底刃包络印。返回是否接触材料。

        边界格按覆盖率混合切深（抗锯齿）：cov=1 全切、cov∈(0,1) 的格子
        新高度 = 原高度×(1-cov) + 切深×cov——圆形轮廓呈平滑坡过渡而非
        格子台阶。
        """
        if p[2] >= 0.0:
            return False
        zc = self.thickness + p[2]
        vx = p[0] + self.width * 0.5
        vy = p[1] + self.height * 0.5
        ix = int(round(vx / self.res_x))
        iy = int(round(vy / self.res_y))
        i0 = max(0, ix + di0); i1 = min(self.nx, ix + di1)
        j0 = max(0, iy + dj0); j1 = min(self.ny, iy + dj1)
        if i0 >= i1 or j0 >= j1:
            return False
        sub = h[j0:j1, i0:i1]
        # inf 封顶到 1e30：covs=0 的格 cut_z 项恒为 0，混合结果=原高度，
        # 无需 np.where 分支（也不产生 inf×0 的 nan 警告）
        cut_z = zc + np.minimum(
            prof[j0 - (iy + dj0): j1 - (iy + dj0),
                 i0 - (ix + di0): i1 - (ix + di0)], np.float32(1e30))
        covs = cov[j0 - (iy + dj0): j1 - (iy + dj0),
                   i0 - (ix + di0): i1 - (ix + di0)]
        blended = sub * (1.0 - covs) + cut_z * covs
        np.minimum(sub, blended, out=sub)
        return True

    def replay(self, moves: list, tool) -> None:
        """从完整毛坯重放到指定移动段列表（seek 跳转用）。

        连续切削段合并成折线批量扫掠（sweep_path），密集路径重放比逐段
        sweep 快数倍。
        """
        self.reset()
        cur = [0.0, 0.0, 0.0]
        run: list = []
        for _li, tgt, cutting in moves:
            if cutting:
                if not run:
                    run.append(tuple(cur))
                run.append(tuple(tgt))
            elif run:
                self.sweep_path(run, tool)
                run = []
            cur[:] = tgt
        if run:
            self.sweep_path(run, tool)

    # ------------------------------------------------------------- 表面提取
    def _denoise(self) -> np.ndarray:
        """保边去噪，返回平滑副本（不改写母场）。

        刀具包络按高度采样离散化，切削轮廓边缘会留一圈 1~2 层的台阶
        噪声（视觉上的锯齿）；而腔壁/轮廓的大落差（≫容差）被排除在
        平均之外，竖壁锐度不受影响。每方向 1 格邻域，向量化一次完成。
        渲染/回放每帧都会取网格，就地改写母场会让切削细节被反复平滑
        逐渐"融化"。
        """
        h = self.h
        ny, nx = h.shape
        tol = self.res_z * 2.0
        acc = h.astype(np.float32)
        cnt = np.ones(h.shape, dtype=np.float32)
        hp = np.pad(h, 1, mode="edge")
        for (sy, sx) in ((0, 1), (2, 1), (1, 0), (1, 2)):
            nb = hp[sy:sy + ny, sx:sx + nx]
            ok = np.abs(nb - h) <= tol
            acc += np.where(ok, nb, 0.0)
            cnt += ok
        np.divide(acc, cnt, out=acc)
        return acc

    def top_mesh(self):
        """2.5D 高度场坯料网格（顶面 + 四周边墙），全 numpy 向量化。

        返回 (pos (N,3) float32, nrm (N,3) float32)（毛坯系，非索引三角）
        或 None。顶面每格 2 三角 6 顶点，平面着色（逐三角面法线）。
        """
        if self.h is None:
            return None
        h = self.h
        ny, nx = h.shape
        if not np.any(h > 0.0):
            return None
        hh = self._denoise()
        xs = (np.arange(nx, dtype=np.float32) * self.res_x)
        ys = (np.arange(ny, dtype=np.float32) * self.res_y)
        # 顶面：每格 6 顶点 [00,10,11, 00,11,01]，按格连续展开
        x0g = xs[None, :-1]; x1g = xs[None, 1:]
        y0g = ys[:-1, None]; y1g = ys[1:, None]
        z00 = hh[:-1, :-1]; z10 = hh[:-1, 1:]
        z11 = hh[1:, 1:];  z01 = hh[1:, :-1]
        pos_l = []
        nrm_l = []
        # 顶面三角流（向量化拼接 6 组 (R,C,3) 再交错）
        A = np.stack(np.broadcast_arrays(x0g, y0g, z00), -1)
        B = np.stack(np.broadcast_arrays(x1g, y0g, z10), -1)
        C = np.stack(np.broadcast_arrays(x1g, y1g, z11), -1)
        D = np.stack(np.broadcast_arrays(x0g, y1g, z01), -1)
        # 顶面顶点平滑法线（Gouraud）：顶点法线 = 周围 6 个三角形面法线
        # 之和。高度场已有覆盖率渐变打底（边界不再是二值台阶），平滑法
        # 线呈现真实雕刻面的连续曲率光泽；平面着色会显出格子面片的
        # "锯齿感"。
        def _fn(v0, v1, v2):
            n = np.cross(v1 - v0, v2 - v0)
            ln = np.sqrt((n * n).sum(-1, keepdims=True))
            ln = np.where(ln < 1e-12, 1.0, ln)
            return n / ln
        n1 = _fn(A, B, C)          # 三角 1 面法线
        n2 = _fn(A, C, D)          # 三角 2 面法线
        r0 = np.arange(ny - 1)[:, None]; r1 = r0 + 1
        c0 = np.arange(nx - 1)[None, :]; c1 = c0 + 1
        # 每个网格顶点累加相邻三角形法线（顶点索引 = r*nx + c）
        VN = np.zeros((ny * nx, 3), dtype=np.float32)
        ia = (r0 * nx + c0).ravel()          # A: (r, c)
        ib = (r0 * nx + c0 + 1).ravel()      # B: (r, c+1)
        ic = ((r0 + 1) * nx + c0 + 1).ravel()  # C: (r+1, c+1)
        id_ = ((r0 + 1) * nx + c0).ravel()   # D: (r+1, c)
        for ii_ in (ia, ib, ic):
            np.add.at(VN, ii_, n1.reshape(-1, 3))
        for ii_ in (ia, ic, id_):
            np.add.at(VN, ii_, n2.reshape(-1, 3))
        ln = np.sqrt((VN * VN).sum(1, keepdims=True))
        ln = np.where(ln < 1e-12, 1.0, ln)
        VN = (VN / ln).reshape(ny, nx, 3)
        pos_l.append(np.concatenate([A, B, C, A, C, D], axis=-1).reshape(-1, 3))
        nrm_l.append(np.concatenate(
            [VN[r0, c0], VN[r0, c0 + 1], VN[r1, c1],
             VN[r0, c0], VN[r1, c1], VN[r1, c0]], axis=-1).reshape(-1, 3))

        # 四周边墙：从顶面边沿竖直落到 z=0（毛坯块体侧面）

        def _wall(xa, ya, za, xb, yb, zb, nrm):
            """边沿线段数组 (M,) → 墙面三角流 [P0,P1,P0b, P0b,P1,P1b]。"""
            xa, ya, za, xb, yb, zb = np.broadcast_arrays(xa, ya, za, xb, yb, zb)
            m = xa.shape[0]
            P0 = np.stack([xa, ya, za], -1)[:, None, :]
            P1 = np.stack([xb, yb, zb], -1)[:, None, :]
            Z0 = np.stack([xa, ya, np.zeros_like(xa)], -1)[:, None, :]
            Z1 = np.stack([xb, yb, np.zeros_like(xb)], -1)[:, None, :]
            pos = np.concatenate([P0, P1, Z0, Z0, P1, Z1], axis=1)
            n = np.asarray(nrm, dtype=np.float32)
            nrm_out = np.broadcast_to(n, (m, 6, 3))
            return pos.reshape(-1, 3), nrm_out.reshape(-1, 3)

        # 南墙（j=0，法线 -Y）/ 北墙（j=ny-1，+Y）
        for (jrow, nrm) in ((0, (0.0, -1.0, 0.0)), (ny - 1, (0.0, 1.0, 0.0))):
            p, n = _wall(xs[:-1], ys[jrow], hh[jrow, :-1],
                         xs[1:], ys[jrow], hh[jrow, 1:], nrm)
            pos_l.append(p.astype(np.float32))
            nrm_l.append(n)
        # 西墙（i=0，-X）/ 东墙（i=nx-1，+X）
        for (icol, nrm) in ((0, (-1.0, 0.0, 0.0)), (nx - 1, (1.0, 0.0, 0.0))):
            p, n = _wall(xs[icol], ys[:-1], hh[:-1, icol],
                         xs[icol], ys[1:], hh[1:, icol], nrm)
            pos_l.append(p.astype(np.float32))
            nrm_l.append(n)

        # 内部陡坎侧壁：相邻列高度差超过一层 → 挖出竖直腔壁。没有这些
        # 墙，凹槽/型腔侧壁是"裂缝"，侧视图没有实体的立体感。材料列的
        # 竖直面在较低邻居一侧暴露：法线朝向低侧。
        def _emit_cliff(a_line, b0_arr, b1_arr, z_hi, z_lo, nrm, swap):
            """竖直面（数组掩码）：a=a_line（墙的固定坐标）、b∈[b0,b1]、
            z∈[z_lo,z_hi]。swap=False → 顶点 (a,b,z)（a=x），True → (b,a,z)
            （a=y）。"""
            m = z_hi - z_lo > max(self.res_z * 1.5, 1e-6)
            if not np.any(m):
                return
            am = np.broadcast_to(a_line, m.shape)[m]
            b0m = np.broadcast_to(b0_arr, m.shape)[m]
            b1m = np.broadcast_to(b1_arr, m.shape)[m]
            zh = z_hi[m]
            zl = z_lo[m]
            if swap:
                P0 = np.stack([b0m, am, zh], -1)[:, None, :]
                P1 = np.stack([b1m, am, zh], -1)[:, None, :]
                Z0 = np.stack([b0m, am, zl], -1)[:, None, :]
                Z1 = np.stack([b1m, am, zl], -1)[:, None, :]
            else:
                P0 = np.stack([am, b0m, zh], -1)[:, None, :]
                P1 = np.stack([am, b1m, zh], -1)[:, None, :]
                Z0 = np.stack([am, b0m, zl], -1)[:, None, :]
                Z1 = np.stack([am, b1m, zl], -1)[:, None, :]
            pos = np.concatenate([P0, P1, Z0, Z0, P1, Z1], axis=1).reshape(-1, 3)
            n = np.asarray(nrm, dtype=np.float32)
            nrm_out = np.broadcast_to(n, (len(am), 6, 3)).reshape(-1, 3)
            pos_l.append(pos.astype(np.float32))
            nrm_l.append(nrm_out)

        # X 向列间陡坎：界面 x=xs[j+1]，跨 y 单元（高度取单元中点行）
        xl = xs[None, 1:] + np.zeros((ny - 1, 1), dtype=np.float32)
        y00 = ys[:-1, None] + np.zeros((1, nx - 1), dtype=np.float32)
        y01 = ys[1:, None] + np.zeros((1, nx - 1), dtype=np.float32)
        hL = (hh[:-1, :-1] + hh[1:, :-1]) * 0.5
        hR = (hh[:-1, 1:] + hh[1:, 1:]) * 0.5
        _emit_cliff(xl, y00, y01, hL, hR, (1.0, 0.0, 0.0), False)
        _emit_cliff(xl, y00, y01, hR, hL, (-1.0, 0.0, 0.0), False)
        # Y 向列间陡坎：界面 y=ys[i+1]，跨 x 单元（高度取单元中点列）
        yl = ys[1:, None] + np.zeros((1, nx - 1), dtype=np.float32)
        x00 = xs[None, :-1] + np.zeros((ny - 1, 1), dtype=np.float32)
        x01 = xs[None, 1:] + np.zeros((ny - 1, 1), dtype=np.float32)
        hD = (hh[:-1, :-1] + hh[:-1, 1:]) * 0.5
        hU = (hh[1:, :-1] + hh[1:, 1:]) * 0.5
        _emit_cliff(yl, x00, x01, hD, hU, (0.0, 1.0, 0.0), True)
        _emit_cliff(yl, x00, x01, hU, hD, (0.0, -1.0, 0.0), True)
        pos = np.concatenate(pos_l, 0)
        nrm = np.concatenate(nrm_l, 0)
        return pos.astype(np.float32), nrm.astype(np.float32)


class VoxelStock:
    """体素坯料 + 刀具扫掠切削仿真。

    用法：
        vs = VoxelStock(width, height, thickness)
        vs.reset()
        vs.sweep((0,0,0), (10,0,-3), tool)   # 一段切削运动
        pos, nrm, base = vs.surface()        # 提取表面网格供渲染
    """

    def __init__(self, width: float, height: float, thickness: float,
                 res: Optional[float] = None) -> None:
        self.width = float(width)
        self.height = float(height)
        self.thickness = float(thickness)
        if res is None:
            res = max(0.8, max(self.width, self.height, self.thickness) / 120.0)
        self.res = float(res)
        self.nx = max(2, int(round(self.width / self.res)))
        self.ny = max(2, int(round(self.height / self.res)))
        self.nz = max(2, int(round(self.thickness / self.res)))
        # 实际网格间距（重算，保证网格铺满毛坯）
        self.res_x = self.width / self.nx
        self.res_y = self.height / self.ny
        self.res_z = self.thickness / self.nz
        self.vox: np.ndarray | None = None
        self.revision = 0          # 表面缓存失效版本号
        self._template_cache: dict = {}
        self._last_sweep: Optional[tuple] = None

    # ------------------------------------------------------------- 构建/重置
    def reset(self) -> None:
        """重新填充完整毛坯体素。"""
        self.vox = np.ones((self.nx, self.ny, self.nz), dtype=bool)
        self.revision += 1
        self._last_sweep = None

    def _g2v(self, g: Tuple[float, float, float]) -> Tuple[float, float, float]:
        """G 代码坐标 → 体素网格坐标（float，非整数）。

        G 代码 Z=0 为毛坯顶面（毛坯系 z=thickness），向下为负。
        """
        return (g[0] + self.width * 0.5,
                g[1] + self.height * 0.5,
                self.thickness + g[2])

    # ------------------------------------------------------------- 刀具模板
    def _tool_radii(self, tool) -> List[Tuple[float, float, float, float]]:
        """刀具沿 Z 的半径分段（与模块级 _tool_radii 一致，含球头包络）。"""
        return _tool_radii(tool)

    def _template(self, tool) -> Tuple[np.ndarray, Tuple[int, int, int, int, int, int]]:
        """构建刀具体素掩码（相对刀尖，刀体向上）。

        返回 (mask, (di0, di1, dj0, dj1, dk0, dk1))：mask 形状
        (di1-di0, dj1-dj0, dk1-dk0)，bool，True=该相对体素被刀具占据。
        按 (直径,柄径,刃长,总长,锥角,形状,分辨率) 缓存。
        """
        g = tool.geometry
        key = (float(g.get("diameter", 0.0)), float(g.get("shank_diameter", 0.0)),
               float(g.get("flute_length", 0.0)), float(g.get("total_length", 0.0)),
               float(g.get("taper_angle", 0.0)), str(getattr(tool, "shape", "")),
               self.res)
        hit = self._template_cache.get(key)
        if hit is not None:
            return hit
        segs = self._tool_radii(tool)
        rmax = max((r for _z0, _z1, _r0, r in segs), default=1.0)
        # 相对范围（体素）：XY 半径 rmax+半格，Z 从刀尖到总长
        k_hi = int(math.ceil(float(
            segs[-1][1] if segs else 1.0) / self.res_z)) + 1
        i_span = int(math.ceil((rmax + max(self.res_x, self.res_y)) /
                               min(self.res_x, self.res_y))) + 1
        j_span = i_span
        di0, di1 = -i_span, i_span + 1
        dj0, dj1 = -j_span, j_span + 1
        dk0, dk1 = 0, k_hi + 1
        ii, jj, kk = np.mgrid[di0:di1, dj0:dj1, dk0:dk1]
        dx = (ii.astype(np.float64) + 0.0) * self.res_x
        dy = (jj.astype(np.float64) + 0.0) * self.res_y
        dz = kk.astype(np.float64) * self.res_z   # 相对刀尖高度
        r2 = dx * dx + dy * dy
        mask = np.zeros_like(ii, dtype=bool)
        for z0, z1, r0, r1 in segs:
            seg_ok = (dz >= z0) & (dz <= z1 + 1e-9)
            # 分段内半径线性插值（球头=球面分段线性化）
            rr = r0 + (r1 - r0) * np.clip(
                (dz - z0) / max(z1 - z0, 1e-9), 0.0, 1.0)
            mask |= seg_ok & (r2 <= (rr + 0.5 * self.res_x) ** 2)
        mask &= (dz >= 0.0)
        self._template_cache[key] = (mask, (di0, di1, dj0, dj1, dk0, dk1))
        return mask, (di0, di1, dj0, dj1, dk0, dk1)

    # ------------------------------------------------------------- 扫掠切削
    def sweep(self, p0: Tuple[float, float, float],
              p1: Tuple[float, float, float], tool) -> None:
        """沿 G 代码线段 p0→p1 扫掠刀具（连续切削，密集采样）。"""
        if self.vox is None or p0 == p1:
            self._last_sweep = p1
            return
        mask, (di0, di1, dj0, dj1, dk0, dk1) = self._template(tool)
        step = max(self.res_x, self.res_y, self.res_z) * 0.5
        d = math.sqrt((p1[0] - p0[0]) ** 2 + (p1[1] - p0[1]) ** 2 +
                      (p1[2] - p0[2]) ** 2)
        n = max(1, int(math.ceil(d / step)))
        for k in range(n + 1):
            t = k / n
            gx = p0[0] + (p1[0] - p0[0]) * t
            gy = p0[1] + (p1[1] - p0[1]) * t
            gz = p0[2] + (p1[2] - p0[2]) * t
            if gz >= 0.0:
                continue   # 刀尖在毛坯顶面以上：未接触材料
            vx, vy, vz = self._g2v((gx, gy, gz))
            ix = int(round(vx / self.res_x))
            iy = int(round(vy / self.res_y))
            iz = int(round(vz / self.res_z))
            x0 = max(0, ix + di0)
            x1 = min(self.nx, ix + di1)
            y0 = max(0, iy + dj0)
            y1 = min(self.ny, iy + dj1)
            z0 = max(0, iz + dk0)
            z1 = min(self.nz, iz + dk1)
            if x0 >= x1 or y0 >= y1 or z0 >= z1:
                continue
            m = mask[x0 - (ix + di0): x1 - (ix + di0),
                     y0 - (iy + dj0): y1 - (iy + dj0),
                     z0 - (iz + dk0): z1 - (iz + dk0)]
            self.vox[x0:x1, y0:y1, z0:z1] &= ~m
        self.revision += 1
        self._last_sweep = p1

    def replay(self, moves: list, tool) -> None:
        """从完整毛坯重放到指定移动段列表（seek 跳转用）。

        moves: [(行号, (x,y,z), cutting)]，按顺序逐段处理；cutting=False 的
        段（G0 快移）只移动不切削。
        """
        self.reset()
        cur = [0.0, 0.0, 0.0]
        for _li, tgt, cutting in moves:
            if cutting:
                self.sweep(tuple(cur), tuple(tgt), tool)
            cur[:] = tgt

    # ------------------------------------------------------------- 表面提取
    def surface(self) -> Optional[Tuple[np.ndarray, np.ndarray, Tuple[int, int, int]]]:
        """提取剩余体素的暴露表面三角网格。

        返回 (pos (N,3) float32, nrm (N,3) float32, base (3,)) 或 None（空）。
        每个暴露面 2 三角形 6 顶点；法线按面方向固定。
        """
        if self.vox is None:
            return None
        v = self.vox
        nx, ny, nz = v.shape
        if not v.any():
            return None
        base = (124, 134, 148)   # 坯料深灰蓝：与浅灰画布背景形成清晰对比
        # 每方向暴露面：v[i] 存在且邻居不存在；并补最外层边界（完整坯料时
        # 边界面没有相邻体素可比，必须显式暴露，否则整体表面返回空）
        # +x / -x
        xp = np.concatenate([v[:-1] & ~v[1:], v[-1:]], axis=0)
        xn = np.concatenate([v[:1], v[1:] & ~v[:-1]], axis=0)
        # +y / -y
        yp = np.concatenate([v[:, :-1, :] & ~v[:, 1:, :], v[:, -1:, :]], axis=1)
        yn = np.concatenate([v[:, :1, :], v[:, 1:, :] & ~v[:, :-1, :]], axis=1)
        # +z / -z
        zp = np.concatenate([v[:, :, :-1] & ~v[:, :, 1:], v[:, :, -1:]], axis=2)
        zn = np.concatenate([v[:, :, :1], v[:, :, 1:] & ~v[:, :, :-1]], axis=2)
        xs = np.arange(nx)[:, None, None]
        ys = np.arange(ny)[None, :, None]
        zs = np.arange(nz)[None, None, :]
        # 体素顶点世界坐标（体素左下角 + 半格居中）
        ox = np.arange(nx) * self.res_x
        oy = np.arange(ny) * self.res_y
        oz = np.arange(nz) * self.res_z

        def _emit(mask, axis, side):
            idx = np.argwhere(mask)
            if idx.size == 0:
                return None
            i, j, k = idx[:, 0], idx[:, 1], idx[:, 2]
            x0 = ox[i]; y0 = oy[j]; z0 = oz[k]
            x1 = x0 + self.res_x; y1 = y0 + self.res_y; z1 = z0 + self.res_z
            n = len(i)
            if axis == 0:   # x 面
                if side > 0:
                    xs = np.full((n,), x1); nrm = (1.0, 0.0, 0.0)
                    # 逆时针（从 +X 看）→ 法线 +X
                    A = np.stack([xs, y0, z0], 1)
                    B = np.stack([xs, y1, z0], 1)
                    C = np.stack([xs, y1, z1], 1)
                    D = np.stack([xs, y0, z1], 1)
                else:
                    xs = np.full((n,), x0); nrm = (-1.0, 0.0, 0.0)
                    # 逆时针（从 -X 看）→ 法线 -X
                    A = np.stack([xs, y0, z0], 1)
                    B = np.stack([xs, y1, z1], 1)
                    C = np.stack([xs, y1, z0], 1)
                    D = np.stack([xs, y0, z1], 1)
            elif axis == 1:  # y 面
                if side > 0:
                    ys_ = np.full((n,), y1); nrm = (0.0, 1.0, 0.0)
                    # 逆时针（从 +Y 看）→ 法线 +Y
                    A = np.stack([x0, ys_, z0], 1)
                    B = np.stack([x1, ys_, z1], 1)
                    C = np.stack([x1, ys_, z0], 1)
                    D = np.stack([x0, ys_, z1], 1)
                else:
                    ys_ = np.full((n,), y0); nrm = (0.0, -1.0, 0.0)
                    A = np.stack([x0, ys_, z0], 1)
                    B = np.stack([x1, ys_, z0], 1)
                    C = np.stack([x1, ys_, z1], 1)
                    D = np.stack([x0, ys_, z1], 1)
            else:            # z 面
                if side > 0:
                    zs_ = np.full((n,), z1); nrm = (0.0, 0.0, 1.0)
                    # 逆时针（从 +Z 看）→ 法线 +Z
                    A = np.stack([x0, y0, zs_], 1)
                    B = np.stack([x1, y0, zs_], 1)
                    C = np.stack([x1, y1, zs_], 1)
                    D = np.stack([x0, y1, zs_], 1)
                else:
                    zs_ = np.full((n,), z0); nrm = (0.0, 0.0, -1.0)
                    # 逆时针（从 -Z 看）→ 法线 -Z
                    A = np.stack([x0, y0, zs_], 1)
                    B = np.stack([x1, y1, zs_], 1)
                    C = np.stack([x1, y0, zs_], 1)
                    D = np.stack([x0, y1, zs_], 1)
            # 每面 6 顶点 [A,B,C,A,C,D]：按面交替拼接（axis=1 先按面交错）
            pts = np.concatenate([A, B, C, A, C, D], 1).reshape(-1, 3)
            nrm_a = np.tile(np.asarray(nrm, dtype=np.float32), (6 * n, 1))
            return pts.astype(np.float32), nrm_a

        parts = []
        for mask, axis, side in (
                (xp, 0, 1), (xn, 0, -1),
                (yp, 1, 1), (yn, 1, -1),
                (zp, 2, 1), (zn, 2, -1)):
            r = _emit(mask, axis, side)
            if r is not None:
                parts.append(r)
        if not parts:
            return None
        pos = np.concatenate([p for p, _n in parts], 0)
        nrm = np.concatenate([n for _p, n in parts], 0)
        return pos, nrm, base


    def top_mesh(self):
        """2.5D 高度场坯料网格（顶面 + 四周边墙），铣削本质是 2.5D。

        每 XY 列取最高实体层作为顶面高度，构建"顶面 + 外周边墙"网格。
        顶点数约 = 格数*6 + 侧墙，比全 3D 表面（surface()）少一个量级，
        每帧 GPU 上传量从 ~10MB 降到亚兆级——实体模拟卡顿的根治方案。

        返回 (pos (N,3) float32, nrm (N,3) float32)（毛坯系，非索引三角）
        或 None。base 颜色由调用方统一给定。
        """
        if self.vox is None:
            return None
        v = self.vox
        ny, nx, nz = v.shape
        if not v.any():
            return None
        # 每列最高实体层（从顶向下第一个 True）→ 顶面高度
        top_iz = nz - 1 - np.argmax(v[:, :, ::-1], axis=2)
        h = (top_iz + 1).astype(np.float32) * self.res_z
        h = np.where(v.any(axis=2), h, 0.0)
        xs = np.arange(nx, dtype=np.float32) * self.res_x
        ys = np.arange(ny, dtype=np.float32) * self.res_y
        # 顶面法线：中央差分（凹槽边缘有起伏，光照可见）
        gy, gx = np.gradient(h, self.res_y, self.res_x)
        nl = np.sqrt(gx * gx + gy * gy + 1.0)
        nx_ = (-gx / nl).astype(np.float32)
        ny_ = (-gy / nl).astype(np.float32)
        nz_ = (np.ones_like(h, dtype=np.float32) / nl).astype(np.float32)
        pos_l = []
        nrm_l = []
        # 顶面：每格 2 三角 6 顶点（展开）
        for i in range(ny - 1):
            for j in range(nx - 1):
                x0, x1 = xs[j], xs[j + 1]
                y0, y1 = ys[i], ys[i + 1]
                z00, z10 = h[i, j], h[i, j + 1]
                z01, z11 = h[i + 1, j], h[i + 1, j + 1]
                pts = ((x0, y0, z00), (x1, y0, z10), (x1, y1, z11),
                       (x0, y0, z00), (x1, y1, z11), (x0, y1, z01))
                for k, (px, py, pz) in enumerate(pts):
                    pos_l.append((px, py, pz))
                    ii = i + (1 if k in (2, 3, 4, 5) else 0)
                    jj = j + (1 if k in (1, 2, 4) else 0)
                    nrm_l.append((float(nx_[ii, jj]), float(ny_[ii, jj]),
                                  float(nz_[ii, jj])))
        # 四周边墙：从顶面边沿竖直到 z=0（毛坯块体侧面）
        def _wall(a0, b0, z0, a1, b1, z1, nrm):
            for (aa0, bb0, zz0), (aa1, bb1, zz1) in zip(
                    ((a0, b0, z0),), ((a1, b1, z1),)):
                pos_l.extend(((aa0, bb0, zz0), (aa1, bb1, zz1),
                              (aa0, bb0, 0.0), (aa0, bb0, 0.0),
                              (aa1, bb1, zz1), (aa1, bb1, 0.0)))
                nrm_l.extend((nrm, nrm, nrm, nrm, nrm, nrm))
        # 南墙（i=0）/ 北墙（i=ny-1）
        for j in range(nx - 1):
            _wall(xs[j], ys[0], h[0, j], xs[j + 1], ys[0], h[0, j + 1],
                  (0.0, -1.0, 0.0))
            _wall(xs[j], ys[-1], h[-1, j], xs[j + 1], ys[-1], h[-1, j + 1],
                  (0.0, 1.0, 0.0))
        # 西墙（j=0）/ 东墙（j=nx-1）
        for i in range(ny - 1):
            _wall(xs[0], ys[i], h[i, 0], xs[0], ys[i + 1], h[i + 1, 0],
                  (-1.0, 0.0, 0.0))
            _wall(xs[-1], ys[i], h[i, -1], xs[-1], ys[i + 1], h[i + 1, -1],
                  (1.0, 0.0, 0.0))
        if not pos_l:
            return None
        return (np.asarray(pos_l, dtype=np.float32),
                np.asarray(nrm_l, dtype=np.float32))

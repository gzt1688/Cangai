# -*- coding: utf-8 -*-
"""标准螺纹几何生成（build123d/OCCT 内核）。

按 GB/T 193-2003 / ISO 68-1 公制螺纹标准：
- 牙型角 60°（牙侧与径向平面夹角 30°）；
- 大径 D、小径 d1 = D - 1.082532P、中径 d2 = D - 0.649519P；
- 牙顶平宽 w_c = 3P/16、牙底平宽 w_r = 3P/16、单侧牙侧轴向跨度
  d_v = (D-d1)/2 · tan30° = 5P/16。

生成方式（经典 OCCT 螺纹路线）：
- 牙型剖面（梯形）画在螺旋线起点、垂直于螺旋线切线的平面内；
- 沿多圈螺旋线做 frenet 扫掠（BRepOffsetAPI_MakePipeShell）生成螺旋牙条；
- 外螺纹 = 小径芯轴圆柱 ∪ 牙条（裁剪到长度）；
- 内螺纹 = 牙条作为挖切工具，从实体中减去形成螺旋沟槽。

返回的螺纹均沿 +Z 轴构建、起点在 z=0；由调用方按目标圆柱面轴系变换放置。
"""
from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# 标准公制螺纹参数表（GB/T 193-2003 粗牙 + 常用细牙）
# ---------------------------------------------------------------------------

#: 粗牙螺距表：公称直径 → 标准螺距（mm）
METRIC_COARSE_PITCH = {
    3.0: 0.5, 3.5: 0.6, 4.0: 0.7, 4.5: 0.75, 5.0: 0.8, 6.0: 1.0,
    7.0: 1.0, 8.0: 1.25, 9.0: 1.25, 10.0: 1.5, 11.0: 1.5, 12.0: 1.75,
    14.0: 2.0, 16.0: 2.0, 18.0: 2.5, 20.0: 2.5, 22.0: 2.5, 24.0: 3.0,
    27.0: 3.0, 30.0: 3.5, 33.0: 3.5, 36.0: 4.0, 39.0: 4.0, 42.0: 4.5,
}

#: 常用细牙螺距表：公称直径 → 可选细牙螺距列表（不含粗牙）
METRIC_FINE_PITCH = {
    8.0: [1.0, 0.75], 10.0: [1.25, 1.0, 0.75], 12.0: [1.5, 1.25, 1.0],
    14.0: [1.5, 1.25, 1.0], 16.0: [1.5, 1.25, 1.0], 18.0: [1.5, 1.0],
    20.0: [1.5, 1.0], 22.0: [1.5, 1.0], 24.0: [2.0, 1.5, 1.0],
    27.0: [2.0, 1.5, 1.0], 30.0: [2.0, 1.5, 1.0], 33.0: [2.0, 1.5],
    36.0: [3.0, 2.0, 1.5], 39.0: [3.0, 2.0, 1.5], 42.0: [3.0, 2.0, 1.5],
}

#: 面板规格下拉可选项（标准公称直径，mm）
STANDARD_SIZES = [3.0, 3.5, 4.0, 4.5, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0,
                  11.0, 12.0, 14.0, 16.0, 18.0, 20.0, 22.0, 24.0, 27.0,
                  30.0, 33.0, 36.0]

#: 牙型常量（ISO 68 基本牙型）
_H = math.sqrt(3.0) / 2.0          # 基本三角形高度系数 √3/2
_H1 = 5.0 / 8.0 * _H               # 工作高度系数 5H/8 = 0.5413
_ROOT_H = 1.082532                 # d1 = D - 1.082532P（=5H/4）


def minor_diameter(major_d: float, pitch: float) -> float:
    """小径 d1 = D - 5H/4·P = D - 1.082532P。"""
    return major_d - _ROOT_H * pitch


def pitch_diameter(major_d: float, pitch: float) -> float:
    """中径 d2 = D - 3H/4·P = D - 0.649519P。"""
    return major_d - 0.75 * _H * pitch


def pitch_options(major_d: float) -> list:
    """某公称直径可选的螺距列表：[标准粗牙] + [常用细牙]。"""
    opts = []
    if major_d in METRIC_COARSE_PITCH:
        opts.append(METRIC_COARSE_PITCH[major_d])
    opts.extend(METRIC_FINE_PITCH.get(major_d, []))
    return opts


def nearest_standard_size(d: float) -> Optional[float]:
    """取最接近的标准公称直径（容差 3%），无则 None。"""
    best, bd = None, None
    for s in STANDARD_SIZES:
        dd = abs(s - d)
        if bd is None or dd < bd:
            best, bd = s, dd
    if best is not None and bd <= max(0.05 * best, 0.02):
        return best
    return None


# ---------------------------------------------------------------------------
# 牙型几何
# ---------------------------------------------------------------------------

@dataclass
class ThreadParams:
    """螺纹参数（标准公制牙型）。

    clearance：3D 打印配合间隙（mm，直径量）。
    外螺纹=先车小到 D−clearance 再套牙（整体变瘦）；内螺纹=丝锥加大
    clearance 攻丝（牙槽加深、牙变矮）。0=标准配合。
    """

    major_d: float          # 大径 D（mm）
    pitch: float            # 螺距 P（mm）
    length: float           # 螺纹长度 L（mm）
    internal: bool = False  # True=内螺纹（孔内），False=外螺纹（圆柱外）
    lefthand: bool = False  # True=左旋
    clearance: float = 0.0  # 配合间隙 δ（mm，直径方向）

    # ---- 派生（计算属性）----
    def minor(self) -> float:
        return minor_diameter(self.major_d, self.pitch)

    def pitch_d(self) -> float:
        return pitch_diameter(self.major_d, self.pitch)

    @property
    def thread_height(self) -> float:
        """牙高（半径差）：(D - d1) / 2 = H1·P。"""
        return (self.major_d - self.minor()) / 2.0

    def effective_major(self) -> float:
        """外螺纹车小后的有效大径 D−δ（内螺纹返回原 D）。"""
        if self.internal or self.clearance <= 0.0:
            return self.major_d
        floor = self.minor() + 0.3 * (self.major_d - self.minor())
        return max(self.major_d - self.clearance, floor)

    def validate(self) -> None:
        if self.major_d <= 0 or self.pitch <= 0 or self.length <= 0:
            raise ValueError("螺纹参数需为正数")
        if self.pitch >= self.major_d:
            raise ValueError("螺距过大（应小于公称直径）")
        if self.minor() <= 0:
            raise ValueError("小径非法")
        if self.clearance < 0.0:
            raise ValueError("配合间隙不能为负")
        if self.clearance > 0.8 * (self.major_d - self.minor()):
            raise ValueError("配合间隙过大（超过牙高的 80%）")


def _import_bd():
    """确保 build123d 已就绪并返回模块。"""
    from core.vector.brep_shape import _stagedImportBd
    _stagedImportBd()
    import build123d
    return build123d


def _ridge_section_points(D: float, P: float, lefthand: bool,
                          overlap: float = 0.0, r_extend: float = 0.0):
    """梯形牙条剖面顶点（世界坐标），画在螺旋线起点 (r_mid, 0, 0)、
    垂直于螺旋线切线的平面内。

    剖面局部坐标：u=径向（朝外为正，范围 [-h, +h]），v=切向/近轴向。
    顶点顺序：牙顶左 → 牙顶右 → 牙底右 → 牙底左（闭合，对称梯形）。
    overlap：牙底向内多探出的径向余量（用于外螺纹与芯轴融合时
    避免相切面导致 OCCT 布尔失败；内螺纹挖切用 0 保持标准小径）。
    r_extend：牙顶径向外扩（内螺纹配合间隙=加大丝锥，牙槽更深）。
    """
    r2 = D / 2.0 + r_extend
    r1 = minor_diameter(D, P) / 2.0 - overlap  # 小径半径
    r_mid = (r1 + r2) / 2.0
    h = (r2 - r1) / 2.0
    w_c = 3.0 / 16.0 * P        # 牙顶平宽
    w_r = 3.0 / 16.0 * P        # 牙底平宽
    d_v = 2.0 * h * math.tan(math.radians(30.0))   # 单侧牙侧轴向跨度 5P/16

    # 螺旋线切线方向（起点 (r_mid,0,0)，+Z，右旋）：切向≈圆周方向
    T = (0.0, r_mid, P / (2.0 * math.pi))
    tn = math.sqrt(T[0] ** 2 + T[1] ** 2 + T[2] ** 2)
    T = (T[0] / tn, T[1] / tn, T[2] / tn)
    # 与 T 垂直的平面内、近轴向的方向（u=径向已与 T 垂直）
    w = (0.0, -T[2], T[1])
    wn = math.sqrt(w[0] ** 2 + w[1] ** 2 + w[2] ** 2)
    w = (w[0] / wn, w[1] / wn, w[2] / wn)
    if lefthand:
        w = (-w[0], -w[1], -w[2])

    local = [
        (h, -w_c / 2.0),             # 牙顶左
        (h, w_c / 2.0),              # 牙顶右
        (-h, w_c / 2.0 + d_v),       # 牙底右（牙侧外扩 d_v，对称梯形）
        (-h, -(w_c / 2.0 + d_v)),    # 牙底左
    ]
    pts = []
    for u, v in local:
        pts.append((r_mid + u, v * w[1], v * w[2]))
    return pts


def _make_ridge(D: float, P: float, length: float,
                lefthand: bool, overlap: float = 0.0,
                r_extend: float = 0.0):
    """螺旋牙条：梯形剖面沿螺旋线 frenet 扫掠（多圈，覆盖约 [-P, L+P]）。

    返回 build123d Solid，沿 +Z，径向 [d1/2-overlap, D/2+r_extend]。
    注意：剖面必须画在螺旋线起点（世界坐标），此处螺旋线从 z=0 起，
    牙条自然覆盖 [−w_c/2, L+P+w_c/2+d_v]，由调用方裁剪到长度。
    """
    bd = _import_bd()
    r_mid = (minor_diameter(D, P) / 2.0 + D / 2.0 + r_extend) / 2.0
    span = length + P
    helix = bd.Helix(pitch=P, height=span, radius=r_mid,
                     center=(0, 0, 0), direction=(0, 0, 1),
                     lefthand=lefthand)
    spine = bd.Wire([helix.edges()[0]]).wrapped

    pts = _ridge_section_points(D, P, lefthand, overlap, r_extend)
    es = [bd.Edge.make_line(p1, p2)
          for p1, p2 in zip(pts, pts[1:] + pts[:1])]
    sec = bd.Wire(es).wrapped

    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakePipeShell
    builder = BRepOffsetAPI_MakePipeShell(spine)
    builder.SetMode(True)          # frenet：剖面始终垂直于螺旋线
    builder.Add(sec, False, False)
    builder.Build()
    builder.MakeSolid()
    return bd.Solid(builder.Shape())


def _trim_ridge(ridge, D: float, length: float, pad: float = 0.0,
                inset: float = 0.0):
    """把牙条裁剪到 [inset−pad, length−inset+pad]，端面与轴线垂直。

    - pad（外伸）：用于攻丝牙条，端面伸出实体端面外，避免共面。
    - inset（内缩）：用于车削沟槽工具——实测工具上端面一旦越过实体
      上端面（哪怕 0.03mm），OCCT 差集即返回空；两端各内缩 0.02mm
      （视觉不可见）可完全避开端面交叉，螺纹长度=圆柱面全长时必用。
    """
    bd = _import_bd()
    z_lo = inset - pad
    z_hi = length - inset + pad
    box = bd.Box(2.0 * D + 4.0, 2.0 * D + 4.0, z_hi - z_lo + 1e-3,
                 align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.CENTER))
    box = box.translate((0, 0, (z_lo + z_hi) / 2.0))
    return ridge & box


def make_ridge_solid(params: ThreadParams):
    """生成裁剪后的螺旋牙条实体（沿 +Z，z∈[0, L]）。

    外螺纹：与芯轴并成螺柱；内螺纹：从目标实体中减去（攻丝）。
    内缘咬至小径以下 overlap（防与底孔壁恰好相切导致布尔不可清理）。
    内螺纹带配合间隙时改用「加大 δ 的标准丝锥」（D+δ 整体标准牙型
    ——完全复用标准扫掠路径；曾试过只外扩牙顶的剖面，大间隙时
    扫掠几何劣化、布尔 no-op，弃用）。
    """
    params.validate()
    D_tap = params.major_d + (params.clearance if params.internal else 0.0)
    key = ("rid", round(D_tap, 4), round(params.pitch, 4),
           round(params.length, 4), params.lefthand)
    t = _TOOL_CACHE.get(key)
    if t is None:
        t = _trim_ridge(
            _make_ridge(D_tap, params.pitch, params.length,
                        params.lefthand,
                        overlap=max(0.01, params.pitch * 0.01)),
            D_tap, params.length, pad=0.05)
        if len(_TOOL_CACHE) >= 8:
            _TOOL_CACHE.pop(next(iter(_TOOL_CACHE)))
        _TOOL_CACHE[key] = t
    return t


def make_external_solid(params: ThreadParams):
    """外螺纹螺柱：小径芯轴 ∪ 牙条。沿 +Z，z∈[0, L]，径向 [d1/2, D/2]。

    牙条向内多探出 overlap（≈1% 螺距）与芯轴产生真实体积重叠，
    避免两者恰好相切（内径都等于小径）时 OCCT 布尔并集失败。
    """
    bd = _import_bd()
    params.validate()
    D = params.effective_major()     # 配合间隙：车小到 D−δ 后套牙
    ridge = _make_ridge(D, params.pitch, params.length,
                        params.lefthand,
                        overlap=max(0.01, params.pitch * 0.01))
    ridge = _trim_ridge(ridge, D, params.length)
    core = bd.Cylinder(minor_diameter(D, params.pitch) / 2.0,
                       params.length,
                       align=(bd.Align.CENTER, bd.Align.CENTER,
                              bd.Align.MIN))
    return core + ridge


def make_internal_solid(params: ThreadParams,
                        wall: Optional[float] = None):
    """内螺纹螺母实体：外壁圆柱 − 底孔 − 螺旋牙条（攻丝）。

    空白处独立生成内螺纹时用：外壁即牙顶外那圈材料（螺母壁），
    默认壁厚 = D/2（外径 2D，如 M6 → Φ12 螺母）。沿 +Z，z∈[0, L]。
    """
    bd = _import_bd()
    params.validate()
    if wall is None:
        wall = params.major_d / 2.0
    body = bd.Cylinder(params.major_d / 2.0 + wall, params.length,
                       align=(bd.Align.CENTER, bd.Align.CENTER,
                              bd.Align.MIN))
    bore = bd.Cylinder(params.minor() / 2.0, params.length,
                       align=(bd.Align.CENTER, bd.Align.CENTER,
                              bd.Align.MIN))
    ridge = make_ridge_solid(params)   # 攻丝牙条（内缘已咬合防相切）
    tube = body - bore
    return cut_thread(tube, ridge, (0.0, 0.0, 1.0), (0.0, 0.0, 0.0), 0.0,
                      expect=ridge.volume * 0.8)


def _wrap_topods(topods, bd):
    """TopoDS 布尔结果 → build123d Solid/Compound（downcast+解包）。"""
    from build123d.topology.shape_core import (
        downcast, unwrap_topods_compound)
    from OCP.TopoDS import TopoDS_Compound, TopoDS_Solid
    r = downcast(topods)
    if isinstance(r, TopoDS_Compound):
        inner = unwrap_topods_compound(r, True)
        if not isinstance(inner, TopoDS_Compound):
            r = inner
    if isinstance(r, TopoDS_Compound):
        return bd.Compound.cast(r)
    if isinstance(r, TopoDS_Solid):
        return bd.Solid.cast(r)
    return bd.Compound.cast(topods)


def robust_cut(part, tool):
    """确定性布尔差集（螺纹专用）：关并行 + 多级模糊容差重试。

    OCCT 并行布尔对螺旋扫掠工具是非确定性的——同输入时而正常切除、
    时而判「不相交」原样返回、时而返回空（本工作区实测三态都出现过，
    与 20GB 内存爆炸同族）。SetRunParallel(False) 保证确定性；
    切不动时逐级加大模糊容差（≤5µm，远小于牙型细节）重试。
    不做 build123d 的 UnifySameDomain 清理（螺旋面上是碎块来源）。

    tool 可以是单个形状或形状列表（多工具一次差集——分步切
    「已布尔过的实体」会再次触发 OCCT no-op，实测两工具合一切
    可避开）。
    """
    bd = _import_bd()
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.TopTools import TopTools_ListOfShape
    tools = tool if isinstance(tool, (list, tuple)) else [tool]
    try:
        base_vol = part.volume
    except Exception:
        base_vol = None
    last = None
    for fuzzy in (0.0, 1e-5, 1e-4, 1e-3, 5e-3, 2e-2):
        try:
            op = BRepAlgoAPI_Cut()
            op.SetRunParallel(False)
            if fuzzy > 0.0:
                op.SetFuzzyValue(fuzzy)
            la, lb = TopTools_ListOfShape(), TopTools_ListOfShape()
            la.Append(part.wrapped)
            for t in tools:
                lb.Append(t.wrapped)
            op.SetArguments(la)
            op.SetTools(lb)
            op.Build()
            if not op.IsDone():
                continue
            res = _wrap_topods(op.Shape(), bd)
            last = res
            if res is None or len(res.faces()) == 0:
                continue
            if base_vol is not None:
                try:
                    # 切除量必须为正且超过灰尘阈值（±1e-2mm³ 数值噪声；
                    # 体积反增=布尔垃圾结果）——真实螺纹切削≥零点几 mm³
                    thr = max(0.05, 1e-4 * base_vol)
                    if base_vol - res.volume < thr:
                        continue     # no-op/垃圾：加大容差重试
                    return res
                except Exception:
                    return res
            return res
        except Exception:
            continue
    return last


def verify_skin(result, r_keep, r_stock, t_list,
                axis_pt=(0.0, 0.0, 0.0), axis_dir=(0.0, 0.0, 1.0)):
    """外螺纹车壳清净度验证：在 [r_keep, r_stock] 环带内撒点分类，
    任何采样点在材料内（=牙顶上方残留外皮）即 False。

    直接几何验证，不依赖切除量推断——车壳布尔部分失败（切除量
    达标但局部留疤）只有采样能发现。采样点沿 axis_dir、以 axis_pt
    为基准、t_list 给轴向位置（工具局部 z）。
    """
    import math as _m
    try:
        from OCP.BRepClass3d import BRepClass3d_SolidClassifier
        from OCP.gp import gp_Pnt
        from OCP.TopAbs import TopAbs_IN, TopAbs_ON
        ln = _m.sqrt(sum(c * c for c in axis_dir))
        if ln < 1e-12:
            return True
        d = tuple(c / ln for c in axis_dir)
        ref = (1.0, 0.0, 0.0) if abs(d[0]) < 0.9 else (0.0, 1.0, 0.0)
        xd = (d[1] * ref[2] - d[2] * ref[1],
              d[2] * ref[0] - d[0] * ref[2],
              d[0] * ref[1] - d[1] * ref[0])
        yd = (d[1] * xd[2] - d[2] * xd[1],
              d[2] * xd[0] - d[0] * xd[2],
              d[0] * xd[1] - d[1] * xd[0])
        for t in t_list:
            cx = axis_pt[0] + d[0] * t
            cy = axis_pt[1] + d[1] * t
            cz = axis_pt[2] + d[2] * t
            for adeg in range(0, 360, 15):
                a = _m.radians(adeg)
                ca, sa = _m.cos(a), _m.sin(a)
                for rr in ((r_keep + r_stock) / 2.0,
                           (2.0 * r_keep + r_stock) / 3.0,
                           r_stock - 0.02):
                    q = gp_Pnt(cx + rr * (ca * xd[0] + sa * yd[0]),
                               cy + rr * (ca * xd[1] + sa * yd[1]),
                               cz + rr * (ca * xd[2] + sa * yd[2]))
                    cl = BRepClass3d_SolidClassifier(result.wrapped, q,
                                                     1e-7)
                    if cl.State() in (TopAbs_IN, TopAbs_ON):
                        return False
        return True
    except Exception:
        return True    # 验证不可用时不拦（窗口仍兜底）


def _deep_copy(shape):
    """真深拷贝（BRepBuilderAPI_Copy 复制几何与 TFace）。

    build123d 的 .copy() 共享底层 TShape——布尔对参数面容差的
    原地污染会穿透拷贝，导致同参数第二次布尔结果不同（实测：
    预览成功后拾取刷新再预览即失败）。
    """
    bd = _import_bd()
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
    return _wrap_topods(BRepBuilderAPI_Copy(shape.wrapped).Shape(), bd)


def _cut_once(part, placed, fuzzy, bd):
    """单次差集布尔（关并行；不做任何清理），返回包装结果或 None。"""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.TopTools import TopTools_ListOfShape
    try:
        op = BRepAlgoAPI_Cut()
        op.SetRunParallel(False)
        if fuzzy > 0.0:
            op.SetFuzzyValue(fuzzy)
        la, lb = TopTools_ListOfShape(), TopTools_ListOfShape()
        la.Append(part.wrapped)
        lb.Append(placed.wrapped)
        op.SetArguments(la)
        op.SetTools(lb)
        op.Build()
        if not op.IsDone():
            return None
        return _wrap_topods(op.Shape(), bd)
    except Exception:
        return None


_PHASE_CACHE = {}    # 参数键 → 上次切中的 (相位, 容差)：下次首试即中
_TOOL_CACHE = {}     # 参数键 → 工具实体（cut_thread 内会深拷贝，缓存永不被污染）


def _cache_phase(key, ang, fuzzy):
    if key is None:
        return
    if len(_PHASE_CACHE) > 64:
        _PHASE_CACHE.clear()
    _PHASE_CACHE[key] = (ang, fuzzy)


def compute_thread_part(part, params, axis_pt, axis_dir, face_r,
                        z_start, face_idx=None, log=None):
    """完整螺纹切削链（同步确认 / 子进程预览共用，纯函数无 UI）。

    part 会被深拷贝（防布尔容差污染）。face_idx 用于确定车壳覆盖的
    圆柱面范围（清端口台阶）。失败抛 RuntimeError。log=日志函数。
    """
    def _log(msg):
        if log is not None:
            try:
                log(msg)
            except Exception:
                pass
    make_groove_solid, make_ridge_solid = globals()["make_groove_solid"], \
        globals()["make_ridge_solid"]
    try:
        part = _deep_copy(part)
    except Exception:
        pass
    # 注：勿做「规范化到原点」的烘焙变换——BRepBuilderAPI_Transform
    # 会重写曲面数值，实测直接导致布尔全相位失败（变体矩阵证实：
    # 原坐标直切在任意放置处均通过）。
    ck = (round(params.major_d, 3), round(params.pitch, 3),
          round(params.length, 3), round(params.clearance, 3),
          params.lefthand)
    try:
        pv = part.volume
    except Exception:
        pv = -1.0
    if params.internal:
        ridge = make_ridge_solid(params)
        r_out = (params.major_d + params.clearance) / 2.0
        r_in = minor_diameter(r_out * 2.0, params.pitch) / 2.0
        frac = max(0.05, min(1.0, (r_out - face_r) /
                             max(1e-9, r_out - r_in)))
        result = cut_thread(part, ridge, axis_dir, axis_pt,
                            z_start, expect=ridge.volume * frac,
                            cache_key=("rid",) + ck)
    else:
        import math as _m
        work = part
        R = max(face_r, params.major_d / 2.0)
        ts = (0.1, params.length / 2.0, params.length - 0.1)
        bx = tuple(axis_pt[k] + axis_dir[k] * z_start for k in range(3))
        if params.clearance > 0.0:
            # 车壳覆盖整个圆柱面并两端各越出 0.05：端口盘边（台阶）
            # 一并清掉，整杆统一到 D−δ
            turn_len = params.length
            turn_z = z_start
            try:
                if face_idx is not None:
                    face = part.faces()[face_idx]
                    bb = face.bounding_box()
                    _d = axis_dir
                    lo = min(c[k] * _d[k] for c in bb_corners(bb))
                    hi = max(c[k] * _d[k] for c in bb_corners(bb))
                    turn_len = max(params.length, hi - lo) + 0.1
                    turn_z = z_start - 0.05
            except Exception:
                pass
            turn = make_turndown_solid(params, face_r, length=turn_len)
            if turn is not None:
                r_in = params.effective_major() / 2.0 - 0.02
                est_turn = 0.95 * _m.pi * (R * R - r_in * r_in) * turn_len
                r_keep = params.effective_major() / 2.0 + 0.01
                ts_t = (0.1, turn_len / 2.0, turn_len - 0.1)
                bx_t = tuple(axis_pt[k] + axis_dir[k] * turn_z
                             for k in range(3))
                work = cut_thread(
                    part, turn, axis_dir, axis_pt, turn_z,
                    expect=est_turn, helical=False,
                    cache_key=("turn",) + ck,
                    verify=lambda res: verify_skin(
                        res, r_keep, R - 0.01, ts_t, bx_t, axis_dir))
                if work is None:
                    raise RuntimeError("车外圆（间隙）失败：请微调间隙值")
        groove = make_groove_solid(params)
        result = cut_thread(
            work, groove, axis_dir, axis_pt, z_start,
            expect=groove.volume * 0.9, cache_key=("grv",) + ck,
            # 注意括号：lambda 必须整体作为条件表达式的一支，
            # 否则条件会落进 lambda 体里（clr=0 时恒拒，经典优先级坑）
            verify=((lambda res: verify_skin(
                res, params.effective_major() / 2.0 + 0.01,
                R - 0.01, ts, bx, axis_dir))
                if params.clearance > 0.0 else None))
    try:
        rv = result.volume
    except Exception:
        rv = -1.0
    _log(f"CUT {'INT' if params.internal else 'EXT'} "
         f"D={params.major_d:g} P={params.pitch:g} "
         f"L={params.length:g} clr={params.clearance:g} "
         f"zStart={z_start:.3f} part_vol={pv:.3f} "
         f"result_faces={0 if result is None else len(result.faces())} "
         f"result_vol={rv:.3f}")
    if result is None or len(result.faces()) == 0:
        raise RuntimeError("布尔结果为空：螺纹工具与实体未正确相交")
    try:
        thr = max(0.05, 1e-4 * abs(pv))
        unchanged = pv - result.volume < thr
    except Exception:
        unchanged = False
    if unchanged:
        raise RuntimeError("螺纹工具未切到实体（布尔无效果）")
    return result


def bb_corners(bb):
    """包围盒 8 角（沿轴投影求范围用）。"""
    return [
        (bb.min.X, bb.min.Y, bb.min.Z),
        (bb.min.X, bb.min.Y, bb.max.Z),
        (bb.min.X, bb.max.Y, bb.min.Z),
        (bb.min.X, bb.max.Y, bb.max.Z),
        (bb.max.X, bb.min.Y, bb.min.Z),
        (bb.max.X, bb.min.Y, bb.max.Z),
        (bb.max.X, bb.max.Y, bb.min.Z),
        (bb.max.X, bb.max.Y, bb.max.Z),
    ]


def cut_thread(part, tool, axis_dir, axis_pt, z_start, expect=None,
               verify=None, helical=True, cache_key=None):
    """螺纹挖切：放置 + 相位×容差重试 + 双重验收（切除量窗口 +
    可选 verify 几何直接验证）。

    OCCT 扫掠/环形工具布尔失败模式：no-op / 数值灰尘 / 部分切除 /
    体积反增 / 过量切除 / 局部留疤（切除量达标但外皮残留——只有
    verify 采样能发现）。expect=预期切除量：窗口 [0.5, 1.6]×expect；
    verify(result) 需返回 True。任一验收不过→换相位/容差重试；
    全不过且给了 expect → 返回 None（宁报错不写垃圾）。

    helical=True（螺旋工具）：10 相位 × 2 容差（相位是主要修复
    手段；大容差×螺旋工具是原生崩溃高发区，砍掉）。
    helical=False（环形/圆柱工具）：旋转对称，相位无效；只用
    4 档容差。
    """
    bd = _import_bd()
    # 真深拷贝实体与工具：OCCT 布尔会原地抬高参数面的容差（隐性
    # 污染输入），且 build123d 的 .copy() 共享 TShape 挡不住——
    # 所有损伤只落在深拷贝副本上。
    try:
        part = _deep_copy(part)
        tool = _deep_copy(tool)
    except Exception:
        pass
    phases = (0.0, 53.0, 113.0, 197.0, 307.0,
              31.0, 151.0, 79.0, 251.0, 17.0) if helical else (0.0,)
    fuzzies = (0.0, 1e-3) if helical else (0.0, 1e-3, 5e-3, 2e-2)
    if cache_key is not None and cache_key in _PHASE_CACHE:
        ph, fz = _PHASE_CACHE[cache_key]
        if ph in phases and fz in fuzzies:
            # 上次的胜利组合排最前：常见路径 1 次布尔即中
            phases = (ph,) + tuple(p for p in phases if p != ph)
            fuzzies = (fz,) + tuple(f for f in fuzzies if f != fz)
    try:
        base_vol = part.volume
    except Exception:
        base_vol = None
    if base_vol is None:
        placed = place_along_axis(tool, axis_dir, axis_pt, z_start)
        return robust_cut(part, placed)
    thr = max(0.05, 1e-4 * base_vol)
    cap = 0.85 * base_vol    # 螺纹不可能切掉实体 85%+（垃圾结果上限）
    lo = 0.5 * expect if (expect is not None and expect > 0) else thr
    hi = 1.6 * expect if (expect is not None and expect > 0) else cap
    best, best_rm = None, -1e18
    for fuzzy in fuzzies:
        for ang in phases:
            t = tool
            if ang:
                t = tool.rotate(bd.Axis((0, 0, 0), (0, 0, 1)), ang)
            placed = place_along_axis(t, axis_dir, axis_pt, z_start)
            res = _cut_once(part, placed, fuzzy, bd)
            if res is None or len(res.faces()) == 0:
                continue
            try:
                rm = base_vol - res.volume
            except Exception:
                continue
            if res.volume < thr:
                continue               # 空结果（整只切没了）也是失败
            if lo <= rm <= min(hi, cap):
                if verify is None or verify(res):
                    _cache_phase(cache_key, ang, fuzzy)
                    return res         # 落在验收窗口且几何验证通过
                continue               # 窗口过了但留疤：换组合重试
            if thr <= rm <= cap and rm > best_rm:
                if verify is None or verify(res):
                    best, best_rm = res, rm
        if best is not None and best_rm >= thr and fuzzy > 0.0:
            break                   # 已有可用结果，不再加大容差
    if expect is not None and expect > 0:
        return None                 # 给了预期却无一落窗：宁报错不写垃圾
    return best if best_rm >= thr else None


def make_groove_solid(params: ThreadParams):
    """外螺纹车削挖切工具：负牙型（两牙之间的螺旋沟槽）直接扫掠。

    不再用「大径圆柱 − 螺柱」推导——两圆柱同轴同径共面，OCCT 布尔
    不可清理（结果含碎块、体积错误，且后续布尔内存行为不可预测）。
    沟槽剖面沿螺旋线直接扫掠：内缘切至略低于小径（防相切），
    外缘沿牙侧 30° 斜线延伸越过毛坯表面（外扩量按牙顶宽钳制，
    保证剖面不越出一个螺距周期、扫掠不自交）。
    沿 +Z，z∈[0, L]。目标圆柱 − 本工具 = 外螺纹。
    """
    params.validate()
    key = ("grv", round(params.effective_major(), 4),
           round(params.pitch, 4), round(params.length, 4),
           params.lefthand)
    t = _TOOL_CACHE.get(key)
    if t is None:
        t = _make_groove_tool(params.effective_major(), params.pitch,
                              params.length, params.lefthand)
        if len(_TOOL_CACHE) >= 8:
            _TOOL_CACHE.pop(next(iter(_TOOL_CACHE)))
        _TOOL_CACHE[key] = t
    return t


def make_turndown_solid(params: ThreadParams, stock_radius: float,
                        length: Optional[float] = None):
    """外螺纹带间隙时的毛坯车小工具：[D', R_out] 环形壳。

    真实加工逻辑：先把毛坯车到 D' = D−δ 再套牙，整体变瘦、配合更松。
    length 可覆盖车削长度（默认=螺纹长度）：命令层通常传整个圆柱面
    的长度并两端各越出一点，把端口盘边（台阶）一并清掉。
    间隙为 0 或内螺纹时返回 None（无需车小）。
    """
    bd = _import_bd()
    params.validate()
    if params.clearance <= 0.0 or params.internal:
        return None
    L = length if length is not None else params.length
    r_in = params.effective_major() / 2.0 - 0.02
    # 内缘咬进牙顶 0.02：与新牙顶面（D'/2）恰好相切会触发 OCCT no-op
    r_out = max(stock_radius, params.major_d / 2.0) + 0.5
    inset = min(0.02, L * 0.05)
    h = L - 2.0 * inset
    outer = bd.Cylinder(r_out, h,
                        align=(bd.Align.CENTER, bd.Align.CENTER,
                               bd.Align.MIN))
    inner = bd.Cylinder(r_in, h,
                        align=(bd.Align.CENTER, bd.Align.CENTER,
                               bd.Align.MIN))
    return (outer - inner).translate((0.0, 0.0, inset))


def _groove_section_points(D: float, P: float, lefthand: bool):
    """沟槽剖面顶点（世界坐标），剖面画法同 _ridge_section_points。

    剖面局部坐标：u=径向（朝外为正），v=近轴向，轴向占满一个螺距周期。
    槽壁即牙侧 30° 斜线的延伸：e(r) = w_c/2 + tan30°·(r2 − r)。
    """
    r2 = D / 2.0
    r1 = minor_diameter(D, P) / 2.0
    r_mid = (r1 + r2) / 2.0
    w_c = 3.0 / 16.0 * P
    k = math.tan(math.radians(30.0))
    # 外缘外扩 40% 牙顶半宽的斜线延伸量：越过毛坯表面消除共面，
    # 且 e(ro) ≥ 0.1·w_c/2 > 0，剖面不越出螺距周期
    ro = r2 + 0.4 * (w_c / 2.0) / k
    ri = r1 - max(0.01, P * 0.01)             # 内缘咬至小径以下防相切

    T = (0.0, r_mid, P / (2.0 * math.pi))
    tn = math.sqrt(T[0] ** 2 + T[1] ** 2 + T[2] ** 2)
    T = (T[0] / tn, T[1] / tn, T[2] / tn)
    w = (0.0, -T[2], T[1])
    wn = math.sqrt(w[0] ** 2 + w[1] ** 2 + w[2] ** 2)
    w = (w[0] / wn, w[1] / wn, w[2] / wn)
    if lefthand:
        w = (-w[0], -w[1], -w[2])

    e_ro = w_c / 2.0 - k * (ro - r2)
    e_ri = w_c / 2.0 + k * (r2 - ri)
    local = [
        (ro - r_mid, e_ro),            # 外左（牙侧延伸线）
        (ro - r_mid, P - e_ro),        # 外右
        (ri - r_mid, P - e_ri),        # 内右
        (ri - r_mid, e_ri),            # 内左
    ]
    pts = []
    for u, v in local:
        pts.append((r_mid + u, v * w[1], v * w[2]))
    return pts


def _make_groove_tool(D: float, P: float, length: float, lefthand: bool):
    """沟槽挖切工具：负牙型剖面沿螺旋线 frenet 扫掠后裁剪到 [inset, L−inset]。

    螺旋线从 z=−P 起扫（多扫一个整圈）：两端裁剪面永远落在完整牙圈
    中段——实测若裁在扫掠起始端盖过渡区（z≈0 附近一个牙圈内），
    会切出薄碎几何，OCCT 差集把工具判为「不相交」（毛坯原样返回）
    或干脆返回空。inset 再避开与实体端面的共面。
    """
    bd = _import_bd()
    r_mid = (minor_diameter(D, P) / 2.0 + D / 2.0) / 2.0
    span = length + 2.0 * P
    helix = bd.Helix(pitch=P, height=span, radius=r_mid,
                     center=(0, 0, -P), direction=(0, 0, 1),
                     lefthand=lefthand)
    spine = bd.Wire([helix.edges()[0]]).wrapped

    pts = [(x, y, z - P)
           for (x, y, z) in _groove_section_points(D, P, lefthand)]
    es = [bd.Edge.make_line(p1, p2)
          for p1, p2 in zip(pts, pts[1:] + pts[:1])]
    sec = bd.Wire(es).wrapped

    from OCP.BRepOffsetAPI import BRepOffsetAPI_MakePipeShell
    builder = BRepOffsetAPI_MakePipeShell(spine)
    builder.SetMode(True)
    builder.Add(sec, False, False)
    builder.Build()
    builder.MakeSolid()
    tool = bd.Solid(builder.Shape())
    if not tool.is_valid:
        from OCP.ShapeFix import ShapeFix_Shape
        fix = ShapeFix_Shape(tool.wrapped)
        fix.Perform()
        tool = bd.Solid(fix.Shape())
    # 两端内缩 0.02（视觉不可见）：避开与实体端面共面
    return _trim_ridge(tool, D, length,
                       inset=min(0.02, length * 0.05))


def place_along_axis(solid, axis_dir, axis_point, z_start):
    """把 +Z 向螺纹（z 起点 0）变换到目标轴系：轴方向、轴位置、起点偏移。"""
    bd = _import_bd()
    from OCP.gp import gp_Pnt, gp_Dir, gp_Ax3, gp_Trsf
    from OCP.TopLoc import TopLoc_Location
    from build123d import Location

    ln = math.sqrt(axis_dir[0] ** 2 + axis_dir[1] ** 2 + axis_dir[2] ** 2)
    if ln < 1e-12:
        raise ValueError("轴方向非法")
    d = (axis_dir[0] / ln, axis_dir[1] / ln, axis_dir[2] / ln)
    origin = gp_Pnt(axis_point[0] + d[0] * z_start,
                    axis_point[1] + d[1] * z_start,
                    axis_point[2] + d[2] * z_start)
    # 局部 X 方向：取与轴不平行的参考向量的叉积（右手系）
    ref = (1.0, 0.0, 0.0) if abs(d[0]) < 0.9 else (0.0, 1.0, 0.0)
    xd = (d[1] * ref[2] - d[2] * ref[1],
          d[2] * ref[0] - d[0] * ref[2],
          d[0] * ref[1] - d[1] * ref[0])
    xln = math.sqrt(xd[0] ** 2 + xd[1] ** 2 + xd[2] ** 2)
    xd = (xd[0] / xln, xd[1] / xln, xd[2] / xln)
    trsf = gp_Trsf()
    trsf.SetTransformation(gp_Ax3(origin, gp_Dir(*d), gp_Dir(*xd)),
                           gp_Ax3(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)))
    return Location(TopLoc_Location(trsf)) * solid

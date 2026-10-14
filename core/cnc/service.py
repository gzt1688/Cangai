"""CNC 服务层：封装 pymillcam 调用，对外暴露高层 API。

UI（命令/参数面板）只与本模块交互，不直接 import pymillcam，
这样 pymillcam 升级或替换后置时 UI 无需改动。

主要 API：
- build_profile_project(entities, params) → pymillcam Project（装好 op/tool/machine）
- generate_profile_gcode(project) → str（G 代码）
- generate_profile_preview_segments(project) → list[Segment]（预览刀路）
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from pymillcam.core.geometry import GeometryEntity, GeometryLayer
from pymillcam.core.machine import MachineDefinition
from pymillcam.core.operations import (
    DrillCycle,
    DrillOp,
    GeometryRef,
    LeadConfig,
    LeadStyle,
    MillingDirection,
    OffsetSide,
    PocketOp,
    PocketStrategy,
    ProfileOp,
    RampConfig,
    RampStrategy,
    TabConfig,
)
from pymillcam.core.project import Project, ProjectSettings, Stock
from pymillcam.core.segments import Segment
from pymillcam.core.tools import CuttingData, Tool, ToolController
from pymillcam.engine.ir import Toolpath
from pymillcam.engine.services import ToolpathService

from core.cnc.posts import (
    PostInfo,
    get_post as resolve_post,
    list_posts as list_posts_,
    scan_posts,
)


class CncError(Exception):
    """CNC 生成失败（几何不闭合、刀路计算异常等）。"""


# --------------------------------------------------------------------------- #
# 参数对象（UI 用 dataclass 传参，避免直接依赖 pymillcam 的 Pydantic 模型）
# --------------------------------------------------------------------------- #

@dataclass
class ProfileParams:
    """轮廓铣参数（UI 面板 ↔ service 的传输对象）。"""
    # 刀具
    tool_diameter: float = 3.0
    flute_count: int = 2
    spindle_rpm: int = 18000
    feed_xy: float = 1200.0
    feed_z: float = 300.0
    tool_number: int = 1
    # 切削
    offset_side: str = "outside"   # inside / outside / on_line
    direction: str = "climb"       # climb / conventional
    cut_depth: float = -3.0        # 目标深度（负=向下）
    stepdown: float = 1.0          # 每层下刀量（正）
    multi_depth: bool = True
    # 引入引出
    lead_style: str = "arc"        # arc / tangent / direct
    lead_length: float = 2.0
    # 斜坡下刀
    ramp_strategy: str = "linear"  # helical / linear / plunge
    ramp_angle_deg: float = 3.0
    # 连接片
    tabs_enabled: bool = False
    tab_count: int = 4
    tab_width: float = 5.0
    tab_height: float = 1.5
    # 机床
    controller: str = "mach3"       # mach3 / grbl / uccnc / 用户自定义
    safe_height: float = 5.0
    clearance_plane: float = 3.0
    # 安全
    chord_tolerance: float = 0.02


@dataclass
class BoreParams:
    """扩孔铣削参数：以孔心为圆心螺旋/圆周分层铣出目标孔径。"""
    # 刀具
    tool_diameter: float = 3.0
    flute_count: int = 2
    spindle_rpm: int = 18000
    feed_xy: float = 1200.0
    feed_z: float = 300.0
    tool_number: int = 1
    # 切削
    bore_diameter: float = 6.0       # 目标孔直径（正）
    direction: str = "climb"         # climb=顺时针 / conventional=逆时针
    mode: str = "helical"            # helical=螺旋插补 / circular=圆周分层
    cut_depth: float = -3.0          # 孔深（负）
    stepdown: float = 1.0            # 每层下刀量
    multi_depth: bool = True
    finish_allowance: float = 0.2    # 精加工余量（最后一刀留量，然后精修）
    ramp_strategy: str = "plunge"    # plunge=直接下刀 / linear=线性斜坡 / helical=螺旋下刀
    ramp_angle_deg: float = 3.0      # 斜坡角度 °
    # 机床
    controller: str = "mach3"
    safe_height: float = 5.0
    clearance_plane: float = 3.0
    chord_tolerance: float = 0.02


@dataclass
class ThreadParams:
    """铣螺纹参数：螺旋插补铣内/外螺纹。"""
    # 刀具
    tool_diameter: float = 3.0
    flute_count: int = 2
    spindle_rpm: int = 18000
    feed_xy: float = 1200.0
    feed_z: float = 300.0
    tool_number: int = 1
    # 切削
    direction: str = "right"         # right=右旋 / left=左旋
    pitch: float = 1.5               # 螺距 mm
    thread_diameter: float = 6.0     # 螺纹大径 mm
    thread_type: str = "internal"    # internal=内螺纹 / external=外螺纹
    passes: int = 2                  # 精加工次数（径向分刀）
    cut_depth: float = -6.0          # 螺纹长度（负）
    # 机床
    controller: str = "mach3"
    safe_height: float = 5.0
    clearance_plane: float = 3.0
    chord_tolerance: float = 0.02


@dataclass
class WireParams:
    """单线切割参数：沿开放线走刀（可偏移、可重复、分层）。"""
    # 刀具
    tool_diameter: float = 3.0
    flute_count: int = 2
    spindle_rpm: int = 18000
    feed_xy: float = 1200.0
    feed_z: float = 300.0
    tool_number: int = 1
    # 切削
    offset_side: str = "on_line"     # outside / inside / on_line（曲线原始方向左/右/在线）
    direction: str = "climb"         # climb=正向 / conventional=反向（不改变偏移侧）
    passes: int = 1                  # 切割次数（重复走刀）
    cut_depth: float = -3.0
    stepdown: float = 1.0
    multi_depth: bool = True
    # 下刀策略
    ramp_strategy: str = "plunge"    # plunge=直接下刀 / peck=啄钻下刀 / ramp=沿轮廓斜坡下刀
    ramp_peck_depth: float = 1.0     # 啄钻步距（peck 策略每啄深度）
    # 机床
    controller: str = "mach3"
    safe_height: float = 5.0
    clearance_plane: float = 3.0
    chord_tolerance: float = 0.02


@dataclass
class RegionParams:
    """区域加工参数（PocketOp 填充封闭区域）。"""
    # 刀具
    tool_diameter: float = 3.0
    flute_count: int = 2
    spindle_rpm: int = 18000
    feed_xy: float = 1200.0
    feed_z: float = 300.0
    tool_number: int = 1
    # 切削
    scan_mode: str = "offset"        # parallel=平行扫描 / offset=环切 / helical=螺旋
    direction: str = "climb"
    scan_angle: float = 0.0          # 平行扫描角度 °
    stepover: float = 2.0            # 侧向步距 mm
    finish_rings: str = "each"       # 平行扫描边界精修时机：each=每层扫轮廓 / last=最后一层扫轮廓
    cut_depth: float = -3.0
    stepdown: float = 1.0
    multi_depth: bool = True
    # 机床
    controller: str = "mach3"
    safe_height: float = 5.0
    clearance_plane: float = 3.0
    chord_tolerance: float = 0.02


@dataclass
class GrooveParams:
    """单线摆槽参数：沿开放线横向蛇形摆动铣出槽宽。"""
    # 刀具
    tool_diameter: float = 3.0
    flute_count: int = 2
    spindle_rpm: int = 18000
    feed_xy: float = 1200.0
    feed_z: float = 300.0
    tool_number: int = 1
    # 切削
    offset_side: str = "on_line"     # 槽中心基准（曲线原始方向左/右/在线）
    direction: str = "climb"
    groove_width: float = 5.0        # 槽宽 mm（道数×步距的限界）
    passes: int = 3                  # 摆槽道数（横向来回次数）
    stepover: float = 2.0            # 侧向步距 mm
    cut_depth: float = -3.0
    stepdown: float = 1.0
    multi_depth: bool = True
    # 机床
    controller: str = "mach3"
    safe_height: float = 5.0
    clearance_plane: float = 3.0
    chord_tolerance: float = 0.02


# --------------------------------------------------------------------------- #
# 构造 Project
# --------------------------------------------------------------------------- #

def _make_tool_controller(p: ProfileParams | "DrillParams") -> ToolController:
    tool = Tool(
        name=f"Ø{p.tool_diameter:g} endmill",
        geometry={
            "diameter": p.tool_diameter,
            "flute_length": 15.0,
            "total_length": 50.0,
            "shank_diameter": p.tool_diameter,
            "flute_count": p.flute_count,
        },
        cutting_data={"default": CuttingData(
            spindle_rpm=p.spindle_rpm,
            feed_xy=p.feed_xy,
            feed_z=p.feed_z,
            stepdown=getattr(p, "stepdown", None) or getattr(p, "peck_depth", 1.0),
        )},
    )
    return ToolController(
        tool_number=p.tool_number,
        tool=tool,
        spindle_rpm=p.spindle_rpm,
        feed_xy=p.feed_xy,
        feed_z=p.feed_z,
    )


def _post_default_macros(controller_id: str) -> dict:
    """取后置文件类默认宏（program_start/end/tool_change）。

    保证 Mach3 项目用 `%` 头 / `M09 M05 G0 X0 Y0 M30 %` 尾，
    不被 pymillcam MachineDefinition 的中性默认（G21 G90 G94 G17…）覆盖。
    """
    post = resolve_post(controller_id)
    return {
        "program_start": post.program_start,
        "program_end": post.program_end,
        "tool_change": post.tool_change,
    }


def _make_machine(p: ProfileParams | "DrillParams") -> MachineDefinition:
    m = MachineDefinition(controller=p.controller)
    m.defaults.safe_height = p.safe_height
    m.defaults.clearance_plane = p.clearance_plane
    m.macros = _post_default_macros(p.controller)
    return m


def _make_profile_op(p: ProfileParams, entity_ids: list[str]) -> ProfileOp:
    from pymillcam.core.operations import GeometryRef
    refs = [GeometryRef(layer_name="cnc", entity_id=eid) for eid in entity_ids]
    lead = LeadConfig(style=LeadStyle(p.lead_style), length=p.lead_length)
    ramp = RampConfig(strategy=RampStrategy(p.ramp_strategy),
                      angle_deg=p.ramp_angle_deg)
    tabs = TabConfig(enabled=p.tabs_enabled, count=p.tab_count,
                     width=p.tab_width, height=p.tab_height)
    return ProfileOp(
        name="Profile",
        geometry_refs=refs,
        offset_side=OffsetSide(p.offset_side),
        direction=MillingDirection(p.direction),
        cut_depth=p.cut_depth,
        stepdown=p.stepdown,
        multi_depth=p.multi_depth,
        lead_in=lead,
        lead_out=lead,
        tabs=tabs,
        ramp=ramp,
        tool_controller_id=p.tool_number,
    )


def build_profile_project(entities: list[GeometryEntity],
                           p: ProfileParams) -> Project:
    """把几何实体 + 参数装成 pymillcam Project（含 op/tool/machine）。"""
    layer = GeometryLayer(name="cnc", entities=entities)
    tc = _make_tool_controller(p)
    op = _make_profile_op(p, [e.id for e in entities])
    machine = _make_machine(p)
    return Project(
        name="cangai-cnc",
        machine=machine,
        stock=Stock(thickness=abs(p.cut_depth) if p.cut_depth < 0 else 10.0),
        settings=ProjectSettings(
            safe_height=p.safe_height,
            clearance_plane=p.clearance_plane,
            chord_tolerance=p.chord_tolerance,
        ),
        geometry_layers=[layer],
        tool_controllers=[tc],
        operations=[op],
    )


# --------------------------------------------------------------------------- #
# 生成
# --------------------------------------------------------------------------- #

_service = ToolpathService()


def generate_profile_preview_segments(project: Project) -> list[Segment]:
    """返回预览刀路（2D plan-view Segment 列表），供画布叠加显示。"""
    if not project.operations:
        return []
    op = project.operations[0]
    try:
        return _service.compute_preview(op, project)
    except Exception as e:
        raise CncError(f"预览生成失败: {e}") from e


def generate_profile_gcode(project: Project) -> tuple[str, list[Toolpath]]:
    """生成 G 代码 + IR 刀路。返回 (gcode_str, toolpaths)。"""
    try:
        post = resolve_post(project.machine.controller)
        return _service.generate_program(project, post)
    except Exception as e:
        raise CncError(f"G 代码生成失败: {e}") from e


# --------------------------------------------------------------------------- #
# 钻孔
# --------------------------------------------------------------------------- #

@dataclass
class DrillParams:
    """钻孔参数（UI 面板 ↔ service 的传输对象）。"""
    # 刀具
    tool_diameter: float = 3.0
    flute_count: int = 2
    spindle_rpm: int = 18000
    feed_xy: float = 1200.0
    feed_z: float = 300.0
    tool_number: int = 1
    # 切削
    cut_depth: float = -3.0        # 孔底目标深度（负=向下）
    peck_depth: float = 1.0        # 啄钻每层深度（正）
    cycle: str = "simple"          # simple / peck / chip_break
    dwell_s: float = 0.0           # 孔底暂停秒数
    # 机床
    controller: str = "mach3"
    safe_height: float = 5.0
    clearance_plane: float = 3.0
    # 安全
    chord_tolerance: float = 0.02


def build_drill_project(entities: list[GeometryEntity],
                        p: DrillParams) -> Project:
    """把钻孔点实体 + 参数装成 pymillcam Project（含 DrillOp/tool/machine）。

    entities 应为 POINT 实体（或整圆，引擎取圆心为钻孔点）。
    """
    from pymillcam.core.operations import GeometryRef as GR
    layer = GeometryLayer(name="cnc", entities=entities)
    tc = _make_tool_controller(p)
    refs = [GR(layer_name="cnc", entity_id=e.id) for e in entities]
    cycle = DrillCycle.SIMPLE
    peck = None
    if p.cycle == "peck":
        cycle = DrillCycle.PECK
        peck = p.peck_depth
    elif p.cycle == "chip_break":
        cycle = DrillCycle.CHIP_BREAK
        peck = p.peck_depth
    op = DrillOp(
        name="Drill",
        geometry_refs=refs,
        cycle=cycle,
        peck_depth=peck,
        dwell_at_bottom_s=p.dwell_s,
        cut_depth=p.cut_depth,
        tool_controller_id=p.tool_number,
    )
    machine = _make_machine(p)
    return Project(
        name="cangai-cnc",
        machine=machine,
        stock=Stock(thickness=abs(p.cut_depth) if p.cut_depth < 0 else 10.0),
        settings=ProjectSettings(
            safe_height=p.safe_height,
            clearance_plane=p.clearance_plane,
            chord_tolerance=p.chord_tolerance,
        ),
        geometry_layers=[layer],
        tool_controllers=[tc],
        operations=[op],
    )


# --------------------------------------------------------------------------- #
# 扩孔 / 铣螺纹 / 单线切割 / 单线摆槽（自写 IR 生成，pymillcam 无对应 op）
# --------------------------------------------------------------------------- #

def _ir_common_prefix(p, op_name: str = "op") -> list:
    """通用程序头：注释 + 换刀 + 主轴 + 抬到安全高度（与钻孔引擎一致）。"""
    from pymillcam.engine.ir import IRInstruction, MoveType
    return [
        IRInstruction(type=MoveType.COMMENT, comment=op_name),
        IRInstruction(type=MoveType.TOOL_CHANGE, tool_number=p.tool_number),
        IRInstruction(type=MoveType.SPINDLE_ON, s=p.spindle_rpm),
        IRInstruction(type=MoveType.RAPID, z=p.safe_height),
    ]


def _ir_common_suffix(p) -> list:
    """通用程序尾：抬到安全高度 → 水平回原点。"""
    from pymillcam.engine.ir import IRInstruction, MoveType
    return [
        IRInstruction(type=MoveType.RAPID, z=p.safe_height),
        IRInstruction(type=MoveType.RAPID, x=0.0, y=0.0),
    ]


def _point_center(entity) -> tuple[float, float] | None:
    """取点实体坐标或整圆/圆弧圆心，用于扩孔/铣螺纹的孔心。"""
    if entity.point is not None:
        return entity.point[0], entity.point[1]
    for seg in entity.segments:
        if seg.type == "arc":
            return seg.center[0], seg.center[1]
    return None


def _near2d(a, b, eps: float = 1e-9) -> bool:
    return abs(a[0] - b[0]) < eps and abs(a[1] - b[1]) < eps


def _append_chain(chains: list, piece: list) -> None:
    """把一段折线接到端点相接的现有链上（四方向匹配），接不上另起新链。"""
    if not piece:
        return
    for ch in chains:
        if _near2d(ch[-1], piece[0]):
            ch.extend(piece[1:])
            return
        if _near2d(ch[-1], piece[-1]):
            ch.extend(list(reversed(piece))[1:])
            return
        if _near2d(ch[0], piece[0]):
            ch[:0] = list(reversed(piece[1:]))
            return
        if _near2d(ch[0], piece[-1]):
            ch[:0] = piece[:-1]
            return
    chains.append(list(piece))


def _polylines_from_entities(entities) -> list[list[tuple[float, float]]]:
    """把线实体集合合并为若干条连续折线（线段端点连续化，弧离散化）。

    不连续处在断点分段——绝不把断点强行首尾相接：沿线加工会在两段
    之间以切削进给拉出一条连刀，切伤工件。返回按长度降序排列。
    """
    chains: list = []
    for ent in entities:
        for seg in ent.segments:
            if seg.type == "line":
                s, e = seg.start, seg.end
                piece = [(s[0], s[1]), (e[0], e[1])]
            elif seg.type == "arc":
                import math as _m
                c = seg.center
                n = max(4, min(64, int(abs(seg.sweep_deg) * seg.radius / 0.4) + 1))
                piece = []
                for k in range(n + 1):
                    a = _m.radians(seg.start_angle_deg + seg.sweep_deg * k / n)
                    piece.append((c[0] + seg.radius * _m.cos(a),
                                  c[1] + seg.radius * _m.sin(a)))
            else:
                continue
            _append_chain(chains, piece)
    chains = [c for c in chains if len(c) >= 2]
    chains.sort(key=len, reverse=True)
    return chains


def _polyline_points(entities) -> list[tuple[float, float]] | None:
    """兼容入口：最长的一条连续折线（多段不连续请用 _polylines_from_entities）。"""
    chains = _polylines_from_entities(entities)
    return chains[0] if chains else None


def _offset_polyline(pts, offset: float, miter_limit: float = 2.0) -> list[tuple[float, float]]:
    """折线沿行进方向左法向偏移 offset（0=在线），结果无自交。

    优先用 Clipper（pyclipper）开放偏移生成带，取目标侧最长连续边
    （Clipper 自动修剪凹角自交环）；pyclipper 不可用时回退手写
    miter+圆弧过渡实现。
    """
    if abs(offset) < 1e-9:
        return pts
    try:
        import pyclipper  # noqa: F401
        _HAS_CLIPPER = True
    except Exception:
        _HAS_CLIPPER = False
    if _HAS_CLIPPER:
        try:
            from core.vector.geometry_types import Point3D
            from core.vector.offset_geometry import (
                clipperOffsetBothSides, sideOfPoint)
            import math as _m
            pts3 = [Point3D(x, y, 0.0) for x, y in pts]
            band = clipperOffsetBothSides(
                pts3, totalWidth=2.0 * abs(offset), closed=False,
                capStyle="butt")
            if band:
                b = band[0]
                sides = [sideOfPoint(pts3, p, closed=False) for p in b]
                # 带环按 side 符号切段，取最长连续同号段（目标偏移边）
                runs: list = []
                cur = [0]
                for k in range(1, len(b)):
                    if (sides[k] > 0) != (sides[cur[0]] > 0):
                        runs.append(cur)
                        cur = [k]
                    else:
                        cur.append(k)
                runs.append(cur)
                want_left = offset > 0.0
                # 目标侧候选段：同号且至少 2 点
                cand = [run for run in runs if len(run) >= 2 and
                        (sum(sides[i] for i in run) / len(run) > 0) == want_left]
                if cand:
                    # 取最贴近目标偏移距的段（端盖弧残留 |side| 渐变，会偏离）
                    best = min(cand, key=lambda run: sum(
                        (abs(sides[i]) - abs(offset)) ** 2 for i in run))
                    side_pts = [(b[i].x, b[i].y) for i in best]
                    # 方向规整：段首应接近折线起点
                    if _m.hypot(side_pts[0][0] - pts[0][0],
                                side_pts[0][1] - pts[0][1]) > \
                       _m.hypot(side_pts[0][0] - pts[-1][0],
                                side_pts[0][1] - pts[-1][1]):
                        side_pts.reverse()
                    return side_pts
        except Exception:
            pass
    # 回退：手写 miter + 圆弧过渡
    return _offset_polyline_simple(pts, offset, miter_limit)


def _offset_polyline_simple(pts, offset: float,
                            miter_limit: float = 2.0) -> list[tuple[float, float]]:
    """手写折线偏移（回退实现）：凸角 miter、凹角/超限圆弧过渡。

    圆弧过渡（圆心=顶点、半径=offset、短弧 n1→n2）不会穿到对侧
    偏移边，避免凹角自相交。miter_limit 控制最大延长倍数。
    """
    import math as _m
    n = len(pts)
    if n < 2:
        return list(pts)
    # 去掉相邻重复点（零长段会导致法向退化/自交）
    dedup: list = [pts[0]]
    for q in pts[1:]:
        if abs(q[0] - dedup[-1][0]) > 1e-9 or abs(q[1] - dedup[-1][1]) > 1e-9:
            dedup.append(q)
    pts = dedup
    n = len(pts)
    if n < 2:
        return list(pts)
    # 每段单位左法向（(dx,dy)→(-dy,dx)/len）
    seg_n: list = []
    for i in range(n - 1):
        dx = pts[i + 1][0] - pts[i][0]
        dy = pts[i + 1][1] - pts[i][1]
        ln = _m.hypot(dx, dy)
        if ln < 1e-12:
            seg_n.append((0.0, 0.0))
        else:
            seg_n.append((-dy / ln, dx / ln))
    out: list = []
    for i in range(n):
        if i == 0:
            nx, ny = seg_n[0]
            out.append((pts[i][0] + nx * offset, pts[i][1] + ny * offset))
        elif i == n - 1:
            nx, ny = seg_n[-1]
            out.append((pts[i][0] + nx * offset, pts[i][1] + ny * offset))
        else:
            n1x, n1y = seg_n[i - 1]
            n2x, n2y = seg_n[i]
            bx, by = n1x + n2x, n1y + n2y
            bl = _m.hypot(bx, by)
            if bl < 1e-9:
                # 180° 折返：直接取前段法向端点
                out.append((pts[i][0] + n1x * offset, pts[i][1] + n1y * offset))
                continue
            bx, by = bx / bl, by / bl
            dot = n1x * bx + n1y * by
            m = 1.0 / dot if dot > 1e-9 else 1e6
            if dot <= 0.0 or m > miter_limit:
                # 凹角/超限：圆弧过渡（圆心=顶点、半径=offset、短弧 n1→n2）
                a1 = _m.atan2(n1y, n1x)
                a2 = _m.atan2(n2y, n2x)
                da = a2 - a1
                while da > _m.pi:
                    da -= 2 * _m.pi
                while da < -_m.pi:
                    da += 2 * _m.pi
                steps = max(3, int(abs(da) / (_m.pi / 8)) + 1)
                for k in range(1, steps + 1):
                    a = a1 + da * k / steps
                    out.append((pts[i][0] + _m.cos(a) * offset,
                                pts[i][1] + _m.sin(a) * offset))
                continue
            out.append((pts[i][0] + bx * offset * m, pts[i][1] + by * offset * m))
    return out


def _ir_arc_toolpath(name, tool_number, instructions, controller, safe_height):
    """把指令流封装成 Toolpath（补通用前后缀）。"""
    from pymillcam.engine.ir import Toolpath
    return Toolpath(operation_name=name, tool_number=tool_number,
                    instructions=instructions)


def build_bore_toolpath(center, p: BoreParams) -> Toolpath:
    """扩孔：以 (cx,cy) 为圆心，半径=(目标孔径-刀径)/2 铣圆。

    helical=每圈 Z 降 stepdown 的螺旋插补；circular=在已切环带上方
    原位逐层下刀（每层整圆，层间吃刀=stepdown，不再从孔心进给——
    孔径大于 2×刀径时孔心是未切实心岛，孔心下刀=扎刀）。
    先留精加工余量走粗切，最后在粗切环带上方下到孔底整周精修。
    """
    import math
    from pymillcam.engine.ir import IRInstruction, MoveType
    cx, cy = center
    if p.bore_diameter <= p.tool_diameter + 1e-6:
        raise CncError(
            f"孔径({p.bore_diameter:g}mm)必须大于刀具直径"
            f"({p.tool_diameter:g}mm)")
    r = (p.bore_diameter - p.tool_diameter) / 2.0
    r_cut = r - p.finish_allowance
    if r_cut < p.tool_diameter * 0.05:
        raise CncError(
            f"精加工余量({p.finish_allowance:g}mm)过大：粗切半径≤0，"
            f"需小于 (孔径-刀径)/2={r:g}mm")
    dir_cw = (p.direction == "climb")
    arc = MoveType.ARC_CW if dir_cw else MoveType.ARC_CCW
    depth = abs(p.cut_depth)
    step = p.stepdown if p.multi_depth else depth
    n_layer = max(1, int(math.ceil(depth / step))) if p.multi_depth else 1

    insts = _ir_common_prefix(p, "Bore")
    x0, y0 = cx + r, cy     # 精修（目标尺寸）起点
    x0c, y0c = cx + r_cut, cy   # 粗切（留精修余量）起点
    if p.mode == "helical":
        # 螺旋插补：快速到孔心上方 → 间隙 → 按下刀策略切入第一层 →
        # 每圈 Z 降 stepdown 的螺旋整圆（圆心恒为孔心）
        insts.append(IRInstruction(type=MoveType.RAPID, x=cx, y=cy))
        insts.append(IRInstruction(type=MoveType.RAPID, z=p.clearance_plane))
        z_targets = [-(k + 1) * step for k in range(n_layer)]
        z_targets[-1] = -depth
        first = z_targets[0]
        if p.ramp_strategy == "helical":
            # 螺旋切入：移到圆边顶面 → 第一圈螺旋到第一层深
            insts.append(IRInstruction(
                type=MoveType.FEED, x=x0c, y=y0c, z=0.0, f=p.feed_xy))
            insts.append(IRInstruction(
                type=arc, x=x0c, y=y0c, z=first, i=-r_cut, j=0.0,
                f=p.feed_xy))
        elif p.ramp_strategy == "linear":
            # 线性斜坡切入：孔心顶面斜坡到圆边第一层深（斜线经过孔心岛
            # 顶部属进给切削，非快移下扎，安全）
            insts.append(IRInstruction(
                type=MoveType.FEED, x=cx, y=cy, z=0.0, f=p.feed_xy))
            insts.append(IRInstruction(
                type=MoveType.FEED, x=x0c, y=y0c, z=first, f=p.feed_xy))
        else:
            # 直接下刀切入：孔心垂直下顶面 → 水平到圆边 → 垂直下第一层深
            insts.append(IRInstruction(
                type=MoveType.FEED, x=cx, y=cy, z=0.0, f=p.feed_z))
            insts.append(IRInstruction(
                type=MoveType.FEED, x=x0c, y=y0c, z=0.0, f=p.feed_xy))
            insts.append(IRInstruction(
                type=MoveType.FEED, x=x0c, y=y0c, z=first, f=p.feed_z))
        for zt in z_targets[1:]:
            if zt <= 0:
                insts.append(IRInstruction(
                    type=arc, x=x0c, y=y0c, z=zt, i=-r_cut, j=0.0,
                    f=p.feed_xy))
    else:
        # 圆周分层：定位到粗切环带起点上方 → 垂直下首层 → 整圆 →
        # 原位逐层向下（层间吃刀=stepdown，始终在已切环带内）
        z_targets = [-(k + 1) * step for k in range(n_layer)]
        z_targets[-1] = -depth
        insts.append(IRInstruction(type=MoveType.RAPID, x=x0c, y=y0c))
        insts.append(IRInstruction(type=MoveType.RAPID, z=p.clearance_plane))
        for zt in z_targets:
            insts.append(IRInstruction(
                type=MoveType.FEED, x=x0c, y=y0c, z=zt, f=p.feed_z))
            insts.append(IRInstruction(
                type=arc, x=x0c, y=y0c, z=zt, i=-r_cut, j=0.0,
                f=p.feed_xy))
    # 精修：抬刀 → 精修半径环带上方（该环带已被粗切到孔底，仅剩径向
    # 余量薄环）→ 垂直下到孔底 → 整周精修到目标尺寸
    insts.append(IRInstruction(type=MoveType.RAPID, z=p.safe_height))
    insts.append(IRInstruction(type=MoveType.RAPID, x=x0, y=y0))
    insts.append(IRInstruction(type=MoveType.RAPID, z=p.clearance_plane))
    insts.append(IRInstruction(
        type=MoveType.FEED, x=x0, y=y0, z=-depth, f=p.feed_z))
    insts.append(IRInstruction(
        type=arc, x=x0, y=y0, z=-depth, i=-r, j=0.0, f=p.feed_xy))
    insts += _ir_common_suffix(p)
    return _ir_arc_toolpath("Bore", p.tool_number, insts, p.controller, p.safe_height)


def build_thread_toolpath(center, p: ThreadParams) -> Toolpath:
    """铣螺纹：螺旋插补。内螺纹走刀半径=(大径-刀径)/2，外=(大径+刀径)/2。

    每刀：从顶部螺旋进刀到底部（每圈 Z 降一个螺距），径向分刀精修。
    右旋=顺时针下行(ARC_CW)，左旋=逆时针下行(ARC_CCW)。
    """
    import math
    from pymillcam.engine.ir import IRInstruction, MoveType
    cx, cy = center
    if p.thread_type == "internal":
        if p.thread_diameter <= p.tool_diameter + 1e-6:
            raise CncError(
                f"内螺纹大径({p.thread_diameter:g}mm)必须大于刀具直径"
                f"({p.tool_diameter:g}mm)")
        r = (p.thread_diameter - p.tool_diameter) / 2.0
    else:
        # 外螺纹：刀心半径 = (大径+刀径)/2（末刀刀刃恰好在杆面；
        # 用户输入小径时即为全牙深切削半径）
        r = (p.thread_diameter + p.tool_diameter) / 2.0
    right_hand = (p.direction == "right")
    arc = MoveType.ARC_CW if right_hand else MoveType.ARC_CCW
    pitch = max(p.pitch, 0.05)
    depth = abs(p.cut_depth)
    n_turns = max(1, int(math.ceil(depth / pitch)))
    passes = max(1, p.passes)

    insts = _ir_common_prefix(p, "Thread")
    # 径向分刀：内螺纹半径由小到大收敛到孔壁（先浅后深）；
    # 外螺纹半径由大到小收敛到最终半径（首刀浅/空走，末刀到尺寸），
    # 避免"首刀最深"的全牙深一刀切。
    radial_step = min(0.25 * r, 0.5) if r > 0 else 0.1   # 每刀径向切深上限
    r_steps = []
    if p.thread_type == "internal":
        cur = r - radial_step * (passes - 1)
        for k in range(passes):
            r_steps.append(max(cur, r * 0.5))
            cur += radial_step
    else:
        cur = r + radial_step * (passes - 1)
        for k in range(passes):
            r_steps.append(cur)
            cur -= radial_step
    r_steps[-1] = r

    for rk in r_steps:
        xk, yk = cx + rk, cy
        insts.append(IRInstruction(type=MoveType.RAPID, x=cx, y=cy))
        insts.append(IRInstruction(type=MoveType.RAPID, z=p.clearance_plane))
        # 切入：从间隙平面进给到 (xk,yk,0)——内螺纹在预钻孔内空走，
        # 外螺纹斜线跨过杆边缘时仍在杆顶上方，落点在杆面之外
        insts.append(IRInstruction(type=MoveType.FEED, x=xk, y=yk, z=0.0, f=p.feed_z))
        for t in range(n_turns):
            zt = min(-(t + 1) * pitch, -depth) if t == n_turns - 1 else -(t + 1) * pitch
            insts.append(IRInstruction(type=arc, x=xk, y=yk, z=zt, i=-rk, j=0.0, f=p.feed_xy))
        insts.append(IRInstruction(type=MoveType.RAPID, z=p.clearance_plane))
    insts += _ir_common_suffix(p)
    return _ir_arc_toolpath("Thread", p.tool_number, insts, p.controller, p.safe_height)


def _emit_wire_plunge(insts, layer_pts, zt: float, p) -> None:
    """单线切割每遍第一层下刀：按 p.ramp_strategy 切入。

    - plunge：起点垂直直接下到 zt
    - peck：啄钻——每啄 ramp_peck_depth 抬到间隙排屑，再快进回上一啄上方，
      上下交替慢慢下到 zt（与钻孔 G83 同理）
    - ramp：沿轮廓——起点下到顶面，沿第一段轮廓线性斜坡（z 从 0 慢慢到 zt）
    """
    from pymillcam.engine.ir import IRInstruction, MoveType
    x0, y0 = layer_pts[0]
    insts.append(IRInstruction(type=MoveType.RAPID, x=x0, y=y0))
    insts.append(IRInstruction(type=MoveType.RAPID, z=p.clearance_plane))
    if p.ramp_strategy == "peck":
        peck = max(p.ramp_peck_depth, 0.1)
        zcur = 0.0
        while zcur - peck > zt + 1e-9:
            znext = zcur - peck
            insts.append(IRInstruction(
                type=MoveType.FEED, x=x0, y=y0, z=znext, f=p.feed_z))
            insts.append(IRInstruction(type=MoveType.RAPID, z=p.clearance_plane))
            insts.append(IRInstruction(type=MoveType.RAPID, z=znext + 0.5))
            zcur = znext
        insts.append(IRInstruction(
            type=MoveType.FEED, x=x0, y=y0, z=zt, f=p.feed_z))
    elif p.ramp_strategy == "ramp" and len(layer_pts) >= 2:
        import math as _m
        insts.append(IRInstruction(
            type=MoveType.FEED, x=x0, y=y0, z=0.0, f=p.feed_z))
        x1, y1 = layer_pts[1]
        seg_len = _m.hypot(x1 - x0, y1 - y0)
        n = max(4, min(64, int(seg_len / 0.5) + 1))
        for i in range(1, n + 1):
            t = i / n
            insts.append(IRInstruction(
                type=MoveType.FEED,
                x=x0 + (x1 - x0) * t, y=y0 + (y1 - y0) * t,
                z=zt * t, f=p.feed_xy))
    else:
        insts.append(IRInstruction(
            type=MoveType.FEED, x=x0, y=y0, z=zt, f=p.feed_z))


def build_wire_toolpath(points, p: WireParams) -> Toolpath:
    """单线切割：沿（可偏移的）折线分层走刀，可重复 N 次。

    offset_side 基于曲线原始方向的左侧（outside）/右侧（inside）/在线；
    direction=conventional 只反向走刀，不改变偏移的物理侧。
    """
    from pymillcam.engine.ir import IRInstruction, MoveType
    side = {"outside": 1.0, "inside": -1.0, "on_line": 0.0}.get(p.offset_side, 0.0)
    off = side * p.tool_diameter / 2.0
    pts = _offset_polyline(list(points), off)
    if p.direction == "conventional":
        pts = list(reversed(pts))
    depth = abs(p.cut_depth)
    step = p.stepdown if p.multi_depth else depth
    n_layer = max(1, int(math.ceil(depth / step))) if p.multi_depth else 1

    insts = _ir_common_prefix(p)
    seq = list(pts)
    for _ in range(max(1, p.passes)):
        for k in range(n_layer):
            zt = -(k + 1) * step if k < n_layer - 1 else -depth
            # 蛇形连续：偶数层正向、奇数层反向（层间不抬刀回起点）
            layer_pts = seq if k % 2 == 0 else list(reversed(seq))
            x0, y0 = layer_pts[0]
            if k == 0:
                # 每遍第一层：按策略下刀（直接/啄钻/沿轮廓）
                _emit_wire_plunge(insts, layer_pts, zt, p)
                # 沿轮廓斜坡已走到层内第一点，主循环从第二点起
                body_start = 1 if p.ramp_strategy == "ramp" else 0
            else:
                # 层间连续：本层起点=上一层终点，原位垂直下到本层深
                insts.append(IRInstruction(type=MoveType.FEED, x=x0, y=y0, z=zt, f=p.feed_z))
                body_start = 0
            for (px, py) in layer_pts[body_start:]:
                insts.append(IRInstruction(type=MoveType.FEED, x=px, y=py, z=zt, f=p.feed_xy))
        # 每遍结束：抬刀到安全高度（下遍重新开始）
        insts.append(IRInstruction(type=MoveType.RAPID, z=p.safe_height))
    insts += _ir_common_suffix(p)
    return _ir_arc_toolpath("WireCut", p.tool_number, insts, p.controller, p.safe_height)


def build_groove_toolpath(points, p: GrooveParams) -> Toolpath:
    """单线摆槽：槽宽内按侧向步距分成多道，蛇形来回（层内连续、层间抬刀）。

    槽宽优先：道数内能铺满时收小步距精确到宽；铺不满时加道数
    （实际槽宽 = 刀径 + (道数-1)×步距 ≥ 要求槽宽）。
    """
    import math
    from pymillcam.engine.ir import IRInstruction, MoveType
    base = list(points)
    if p.direction == "conventional":
        base = list(reversed(base))
    side = {"outside": 1.0, "inside": -1.0, "on_line": 0.0}.get(p.offset_side, 0.0)
    lanes = max(1, p.passes)
    stepover = max(p.stepover, 0.05)
    if p.groove_width > p.tool_diameter + 1e-6:
        if lanes > 1:
            fit = (p.groove_width - p.tool_diameter) / (lanes - 1)
            if fit < stepover:
                # 用户道数能覆盖槽宽：内收步距，槽宽精确
                stepover = max(fit, 0.05)
            else:
                # 用户道数铺不满：加道数（步距不放大）
                lanes = 1 + int(math.ceil(
                    (p.groove_width - p.tool_diameter) / stepover))
        else:
            lanes = 1 + int(math.ceil(
                (p.groove_width - p.tool_diameter) / stepover))
    offsets = [side * p.tool_diameter / 2.0 + (k - (lanes - 1) / 2.0) * stepover
               for k in range(lanes)]
    lane_pts = [_offset_polyline(base, o) for o in offsets]
    depth = abs(p.cut_depth)
    step = p.stepdown if p.multi_depth else depth
    n_layer = max(1, int(math.ceil(depth / step))) if p.multi_depth else 1

    insts = _ir_common_prefix(p)
    for k in range(n_layer):
        zt = -(k + 1) * step if k < n_layer - 1 else -depth
        insts.append(IRInstruction(type=MoveType.RAPID, x=lane_pts[0][0][0], y=lane_pts[0][0][1]))
        insts.append(IRInstruction(type=MoveType.RAPID, z=p.clearance_plane))
        insts.append(IRInstruction(type=MoveType.FEED, x=lane_pts[0][0][0], y=lane_pts[0][0][1], z=zt, f=p.feed_z))
        for li, pts in enumerate(lane_pts):
            seq = pts if li % 2 == 0 else list(reversed(pts))
            start = seq[0]
            insts.append(IRInstruction(type=MoveType.FEED, x=start[0], y=start[1], z=zt, f=p.feed_xy))
            for (px, py) in seq[1:]:
                insts.append(IRInstruction(type=MoveType.FEED, x=px, y=py, z=zt, f=p.feed_xy))
        if k < n_layer - 1:
            insts.append(IRInstruction(type=MoveType.RAPID, z=p.safe_height))
    insts += _ir_common_suffix(p)
    return _ir_arc_toolpath("Groove", p.tool_number, insts, p.controller, p.safe_height)


def generate_ir_gcode(toolpaths: list, p) -> tuple[str, list]:
    """把自写 IR toolpath 列表交给后处理生成 G 代码（macros 用后置文件默认）。"""
    post = resolve_post(p.controller)
    return post.post_program(toolpaths), toolpaths


def generate_gcode(toolpaths: list, controller_id: str,
                   macros: dict | None = None) -> str:
    """按后置文件 id + 可选宏覆盖生成 G 代码（路径输出对话框用）。"""
    post = resolve_post(controller_id)
    return post.post_program(toolpaths, macros=macros)


def export_nc(text: str, path: str, *, encoding: str = "ascii",
              crlf: bool = True) -> None:
    """把 G 代码文本落盘为 NC 文件（Mach3 兼容：ASCII + CRLF）。

    Mach3 对编码/行尾挑剔：CRLF 是模板1.NC 的 100% 行尾，ASCII 保证
    无中文残留（中文注释会触发替换为 '?'，配合 UI 提示）。
    """
    body = text.replace("\r\n", "\n").replace("\n", "\r\n") if crlf else text
    with open(path, "w", encoding=encoding, errors="replace", newline="") as fh:
        fh.write(body)
    return None


def list_posts() -> list[PostInfo]:
    """返回全部可用后置文件（UI 下拉数据源）。"""
    return list_posts_()


def build_pocket_project(entities: list[GeometryEntity], p) -> Project:
    """区域加工：PocketOp（平行扫描/环切/螺旋）填充封闭区域。"""
    from pymillcam.core.operations import GeometryRef as GR
    layer = GeometryLayer(name="cnc", entities=entities)
    tc = _make_tool_controller(p)
    refs = [GR(layer_name="cnc", entity_id=e.id) for e in entities]
    strat = {"parallel": PocketStrategy.ZIGZAG,
             "offset": PocketStrategy.OFFSET,
             "helical": PocketStrategy.SPIRAL}.get(p.scan_mode, PocketStrategy.OFFSET)
    ramp = RampConfig(strategy=RampStrategy.PLUNGE, angle_deg=90.0)
    op = PocketOp(
        name="Region",
        geometry_refs=refs,
        strategy=strat,
        direction=MillingDirection(p.direction),
        stepover=p.stepover,
        angle_deg=p.scan_angle,
        cut_depth=p.cut_depth,
        stepdown=p.stepdown,
        multi_depth=p.multi_depth,
        ramp=ramp,
        rest_machining=False,
        finish_rings=getattr(p, "finish_rings", "each"),
        tool_controller_id=p.tool_number,
    )
    machine = _make_machine(p)
    return Project(
        name="cangai-cnc",
        machine=machine,
        stock=Stock(thickness=abs(p.cut_depth) if p.cut_depth < 0 else 10.0),
        settings=ProjectSettings(
            safe_height=p.safe_height,
            clearance_plane=p.clearance_plane,
            chord_tolerance=p.chord_tolerance,
        ),
        geometry_layers=[layer],
        tool_controllers=[tc],
        operations=[op],
    )


# --------------------------------------------------------------------------- #
# 毛坯实体管理
# --------------------------------------------------------------------------- #

def model_bounds(doc):
    """CNC 空间导入模型（浮雕模型/实体）的轴对齐包围盒尺寸。

    返回 (宽X, 高Y, 厚Z) 毫米；CNC 空间没有模型时返回 None。
    仅统计 CNC 独立空间中的 SURFACE/BREP/SOLID 对象，曲线不参与。
    """
    if doc is None:
        return None
    import numpy as np
    try:
        from core.vector.geometry_types import ObjectType
        lo = np.array([np.inf, np.inf, np.inf])
        hi = np.array([-np.inf, -np.inf, -np.inf])
        found = False
        for o in doc.objects():
            t = o.type()
            if t == ObjectType.SURFACE:
                hf = o.heightField()
                ox, oy = hf.origin
                sp = hf.spacing
                # 真实高度（不乘显示用 Z 夸张，与刀路/导出一致）
                zmin = float(np.min(hf.data))
                zmax = float(np.max(hf.data))
                # 应用 local→world 放置仿射（画布上移动/旋转过的浮雕
                # 毛坯尺寸才与刀路几何一致，自动毛坯不失真）
                try:
                    M = np.asarray(o.placement(), dtype=np.float64)
                    if M.shape != (3, 3):
                        raise ValueError
                    zs = [zmin, zmax]
                    for cx, cy in ((ox, oy),
                                   (ox + (hf.nx - 1) * sp, oy),
                                   (ox, oy + (hf.ny - 1) * sp),
                                   (ox + (hf.nx - 1) * sp,
                                    oy + (hf.ny - 1) * sp)):
                        wx = M[0, 0] * cx + M[0, 1] * cy + M[0, 2]
                        wy = M[1, 0] * cx + M[1, 1] * cy + M[1, 2]
                        wz = [M[2, 0] * cx + M[2, 1] * cy + M[2, 2] * z
                              for z in zs]
                        lo[0] = min(lo[0], wx); hi[0] = max(hi[0], wx)
                        lo[1] = min(lo[1], wy); hi[1] = max(hi[1], wy)
                        lo[2] = min(lo[2], min(wz)); hi[2] = max(hi[2], max(wz))
                except Exception:
                    lo[0] = min(lo[0], ox);              hi[0] = max(hi[0], ox + (hf.nx - 1) * sp)
                    lo[1] = min(lo[1], oy);              hi[1] = max(hi[1], oy + (hf.ny - 1) * sp)
                    lo[2] = min(lo[2], zmin);            hi[2] = max(hi[2], zmax)
                found = True
            elif t in (ObjectType.BREP, ObjectType.SOLID):
                mesh = o.mesh(0.05) if hasattr(o, "mesh") else None
                if mesh is None:
                    continue
                vs = mesh["verts"]
                lo[0] = min(lo[0], float(vs[:, 0].min())); hi[0] = max(hi[0], float(vs[:, 0].max()))
                lo[1] = min(lo[1], float(vs[:, 1].min())); hi[1] = max(hi[1], float(vs[:, 1].max()))
                lo[2] = min(lo[2], float(vs[:, 2].min())); hi[2] = max(hi[2], float(vs[:, 2].max()))
                found = True
        if not found or not np.isfinite(lo[0]):
            return None
        w = hi[0] - lo[0]
        h = hi[1] - lo[1]
        tz = hi[2] - lo[2]
        return (max(round(w, 2), 1.0), max(round(h, 2), 1.0), max(round(tz, 2), 1.0))
    except Exception:
        return None


def update_stock_box(doc, state) -> None:
    """根据 state.showStock 在世界中心生成/删除毛坯实体立方体（BRepObject）。

    doc  : Document 实例
    state: CncState 实例（读 showStock/stock，写 stockObjects）
    毛坯放入文档的 CNC 独立空间（SPACE_CNC），2D/浮雕/3D 模式不可见；
    标记 _cncTransient，不随文件保存。
    """
    if doc is None:
        return
    from core.document import SPACE_CNC
    from core.vector.brep_object import BRepObject
    from core.vector.geometry_types import Point3D

    doc.beginEdit()
    for obj in state.stockObjects:
        try:
            doc.removeObject(obj.id())
        except Exception:
            pass
    state.stockObjects = []

    if not state.showStock:
        doc.endEdit()
        return

    w = state.stock.width
    h = state.stock.height
    t = state.stock.thickness
    x0, x1 = -w / 2, w / 2
    y0, y1 = -h / 2, h / 2
    z0, z1 = -t / 2, t / 2
    p = lambda a, b, c: Point3D(a, b, c)
    faces = [
        [p(x0, y0, z0), p(x1, y0, z0), p(x1, y1, z0), p(x0, y1, z0)],
        [p(x0, y1, z1), p(x1, y1, z1), p(x1, y0, z1), p(x0, y0, z1)],
        [p(x0, y0, z0), p(x0, y1, z0), p(x0, y1, z1), p(x0, y0, z1)],
        [p(x1, y0, z0), p(x1, y0, z1), p(x1, y1, z1), p(x1, y1, z0)],
        [p(x0, y0, z0), p(x0, y0, z1), p(x1, y0, z1), p(x1, y0, z0)],
        [p(x0, y1, z0), p(x1, y1, z0), p(x1, y1, z1), p(x0, y1, z1)],
    ]
    box = BRepObject()
    box.setShape(faces)
    box._cncTransient = True      # 不落盘（下次进入 CNC 模式按需重建）
    doc.addObject(box, space=SPACE_CNC)
    doc.notifyObjectChanged(box.id())
    state.stockObjects = [box]
    doc.endEdit()
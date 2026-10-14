"""CNC 流程状态单例：跟踪精雕 7 步流程的设置状态。

7 步：
  1. 机床设置（MachineDefinition）
  2. 安装几何体（Stock 材料 + Fixture 夹具）
  3. 刀具表（ToolLibrary）
  4. 生成刀路（ProfileOp/PocketOp/DrillOp + IR）
  5. 碰撞检查（结果）
  6. 实体模拟（2D 轨迹 / 3D 切除占位）
  7. 路径输出（G 代码）

UI（向导面板/命令/对话框）读写此单例，不直接互传。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from PySide6.QtCore import QObject, Signal

from pymillcam.core.machine import MachineDefinition
from pymillcam.core.project import Stock
from pymillcam.core.tool_library import ToolLibrary
from pymillcam.core.tools import Tool, ToolShape, ToolController
from pymillcam.engine.ir import Toolpath, MoveType

from core.cnc.posts.mach3_jd import Mach3Post


# --------------------------------------------------------------------------- #
# 夹具模型
# --------------------------------------------------------------------------- #

@dataclass
class ClampPlate:
    """压板夹具：矩形压板，位置 + 尺寸 + Z 高度。

    压板是 CNC 最常见夹具：一块矩形板压在毛坯边缘，螺钉固定。
    碰撞检查时把压板当作矩形障碍物，刀具快速移动时不能撞到。
    """
    x: float = 0.0          # 压板中心 X
    y: float = 0.0          # 压板中心 Y
    width: float = 40.0     # 压板宽（X 方向）
    height: float = 10.0    # 压板高（Y 方向）
    z_top: float = 5.0      # 压板顶面 Z（毛坯表面以上部分）
    z_bottom: float = -2.0  # 压板底面 Z（压在毛坯上的部分）


@dataclass
class SideClamp:
    """矩形两边加夹具：毛坯左右两侧各一块夹块。

    适合矩形毛坯对夹：两块夹块从两侧把毛坯夹住。
    """
    side: str = "left"      # left / right
    x: float = 0.0          # 夹块中心 X
    y: float = 0.0          # 夹块中心 Y
    width: float = 20.0     # 夹块宽
    height: float = 30.0    # 夹块高（Y 方向，覆盖毛坯侧边）
    z_top: float = 10.0     # 夹块顶面 Z
    z_bottom: float = -5.0  # 夹块底面 Z


@dataclass
class FixtureSetup:
    """夹具设置：可混合多种夹具形式。"""
    clamps: list[ClampPlate] = field(default_factory=list)
    side_clamps: list[SideClamp] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 材料定义表
# --------------------------------------------------------------------------- #

# 常用 CNC 雕刻/铣削材料：硬度(HB 布氏硬度) + 默认给进速度(mm/min)
MATERIALS = {
    "木料":    {"hardness": 3.0,   "feedRate": 2000},
    "亚克力":  {"hardness": 9.0,   "feedRate": 1500},
    "PVC":     {"hardness": 8.0,   "feedRate": 1800},
    "电木":    {"hardness": 35.0,  "feedRate": 1000},
    "蜡":      {"hardness": 1.0,   "feedRate": 2500},
    "石膏":    {"hardness": 2.0,   "feedRate": 2000},
    "铝":      {"hardness": 20.0,  "feedRate": 1200},
    "铝合金":  {"hardness": 60.0,  "feedRate": 800},
    "铜":      {"hardness": 40.0,  "feedRate": 1000},
    "黄铜":    {"hardness": 80.0,  "feedRate": 600},
    "不锈钢":  {"hardness": 200.0, "feedRate": 300},
    "碳钢":    {"hardness": 150.0, "feedRate": 400},
}


# --------------------------------------------------------------------------- #
# 流程状态
# --------------------------------------------------------------------------- #

# 7 步标识
STEP_MACHINE = "machine"
STEP_STOCK = "stock"
STEP_TOOLS = "tools"
STEP_TOOLPATH = "toolpath"
STEP_COLLISION = "collision"
STEP_SIMULATE = "simulate"
STEP_OUTPUT = "output"

STEP_ORDER = [STEP_MACHINE, STEP_STOCK, STEP_TOOLS, STEP_TOOLPATH,
              STEP_COLLISION, STEP_SIMULATE, STEP_OUTPUT]

STEP_LABELS = {
    STEP_MACHINE: "机床设置",
    STEP_STOCK: "安装坯料",
    STEP_TOOLS: "刀具表",
    STEP_TOOLPATH: "路径生成",
    STEP_COLLISION: "碰撞检查",
    STEP_SIMULATE: "实体模拟",
    STEP_OUTPUT: "路径输出",
}


@dataclass
class CollisionResult:
    """碰撞检查结果。"""
    ok: bool = True
    messages: list[str] = field(default_factory=list)
    collision_points: list[tuple[float, float, float]] = field(default_factory=list)


@dataclass
class ToolpathEntry:
    """一条刀路条目（路径生成步骤列表中的一行）。

    title     : 显示标题，如"钻孔-3mm"（加工方式-刀具直径）
    params    : 对话框控件值快照（flat dict），重编辑回填用
    picked    : 加工区域选取结果 {类型: [对象]}
    toolpaths : 该条目生成的 IR 刀路
    gcode     : 该条目生成的 G 代码
    visible   : 是否显示该路径
    simEnabled: 是否参与实体模拟（实体模拟步骤勾选控制）
    """
    method_id: int = 0
    method_name: str = ""
    title: str = ""
    params: dict = field(default_factory=dict)
    picked: dict = field(default_factory=dict)
    toolpaths: list = field(default_factory=list)
    gcode: str = ""
    visible: bool = True
    simEnabled: bool = True
    color: str = ""              # 轨迹显示颜色（hex，空=按添加顺序取默认色）


class CncState(QObject):
    """CNC 流程状态单例。

    全局唯一实例，UI 各处通过 CncState.instance() 获取。
    任意步骤状态变化发 stateChanged 信号，向导面板刷新。
    """
    stateChanged = Signal()
    # 模拟播放的轻量推进信号：只驱动 3D 视口重绘（刀具移动/实体切削），
    # 不触发 2D 叠加层与向导面板的全量重建——路径几何并未变化。
    simChanged = Signal()

    # 模拟动画插值（渲染/模拟频率解耦）：
    # 逻辑 tick（50ms）推进切削并记录本 tick 起点/终点/时刻；渲染定时器
    # （16ms）每帧按时间在 A→B 间线性插值，动画 60fps 连续丝滑，不再
    # 随 tick 频率 20Hz 跳变。
    simAnimA = None          # 本 tick 起点位置 (x,y,z)
    simAnimB = None          # 本 tick 结束位置 (x,y,z)
    simAnimT = 0.0           # 本 tick 起始时刻（time.monotonic()）

    def simInterpPos(self, now=None):
        """当前渲染帧的插值刀尖位置（tick 之间线性插值）。"""
        if self.simAnimA is None or self.simAnimB is None:
            return self.simPosition
        import time
        if now is None:
            now = time.monotonic()
        frac = (now - self.simAnimT) / 0.05
        if frac <= 0.0:
            return tuple(self.simAnimA)
        if frac > 1.0:
            # 下一逻辑 tick 迟到（密集路径切削计算偏重）：按本 tick 速度
            # 继续外推（最多一个 tick），避免"到点停住 → 下一 tick 跳走"
            # 的周期性顿挫；外推上限保证方向突变时不飞出。
            frac = min(frac, 2.0)
        a, b = self.simAnimA, self.simAnimB
        return (a[0] + (b[0] - a[0]) * frac,
                a[1] + (b[1] - a[1]) * frac,
                a[2] + (b[2] - a[2]) * frac)

    _instance: Optional["CncState"] = None

    @classmethod
    def instance(cls) -> "CncState":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        super().__init__()
        # 1. 机床（默认 Mach3：Cangai 出厂即配精雕机）
        self.machine: MachineDefinition = MachineDefinition(controller="mach3")
        self.machine.macros = {
            "program_start": Mach3Post.program_start,
            "program_end": Mach3Post.program_end,
            "tool_change": Mach3Post.tool_change,
        }
        # 机床类型与旋转轴（4轴用）
        self.machineType: str = "3axis"   # 3axis / 4axis / 5axis
        self.rotaryAxis: str = "A"        # A(绕X) / B(绕Y) / C(绕Z)
        self.rotaryTravelMin: float = -360.0
        self.rotaryTravelMax: float = 360.0
        self.rotaryMaxRpm: int = 100
        # 2. 材料 + 夹具
        self.stock: Stock = Stock(width=200.0, height=200.0, thickness=10.0)
        self.materialType: str = "木料"
        self.materialHardness: float = MATERIALS.get("木料", {}).get("hardness", 3.0)
        self.fixture: FixtureSetup = FixtureSetup()
        self.showStock: bool = False          # 显示毛坯立方体（默认不显示）
        self.stockObjects: list = []          # 毛坯画布对象引用（LineObject 列表）
        self.showTools: bool = False          # 画布显示刀具（实体模拟预览）
        # 3. 刀具表 + 当前选用刀具
        self.toolLibrary: ToolLibrary = ToolLibrary()
        self.toolControllers: list[ToolController] = []
        # 4. 刀路条目列表（路径生成步骤可添加多条，每条可单独显示/编辑）
        self.pathEntries: list[ToolpathEntry] = []
        # 聚合缓存（所有条目合并，供碰撞/模拟/输出步骤使用）
        self.toolpaths: list[Toolpath] = []
        # 5. 碰撞检查结果
        self.collisionResult: Optional[CollisionResult] = None
        # 6. 模拟（无状态，运行时产生）
        # 7. 输出 G 代码（聚合）
        self.gcode: str = ""
        # 6/7 步完成标记：必须实际执行对应步骤才算完成（添加路径不自动打勾）
        self.simulationDone: bool = False
        self.outputDone: bool = False
        # 实体模拟播放状态（G 代码逐行播放）
        self.simLines: list[str] = []                 # G 代码文本行
        self.simLine: int = -1                        # 当前播放行（-1=未开始）
        self.simPosition: Optional[tuple] = None      # 当前刀尖世界坐标 (x,y,z)
        self.simPlaying: bool = False
        self.simCutting: bool = False                 # 当前段是否切削（G1/G2/G3）
        self.showSolidSim: bool = False       # 实体模拟：显示坯料被真实雕刻（体素）
        self.simVoxel: object = None          # VoxelStock 实例（面板维护，视口渲染）

        # 预置常用刀具（库存为空时）
        self._seedDefaultTools()

    # ----------------------------------------------------------- 预置刀具
    DEFAULT_TOOL_DIAMETERS = (1.0, 2.0, 4.0, 6.0, 8.0, 10.0, 16.0)
    DEFAULT_TAPER_ANGLES = (10.0, 20.0, 30.0)
    # 非锥度刀型：(shape, label)；锥度刀型：(shape, label, True)
    _TOOL_PRESETS = (
        (ToolShape.ENDMILL,  "平底刀",   False),
        (ToolShape.BALLNOSE, "球头刀",   False),
        (ToolShape.BULLNOSE, "牛鼻刀",   False),
        (ToolShape.ENDMILL,  "锥度平底", True),
        (ToolShape.BALLNOSE, "锥度球头", True),
        (ToolShape.BULLNOSE, "锥度牛鼻", True),
    )

    def _seedDefaultTools(self) -> None:
        """库存为空时预置刀具：非锥度刀型 7 直径，锥度刀型 7 直径 × 3 锥角(10/20/30°)。"""
        if self.toolLibrary.tools:
            return
        for shape, label, isTaper in self._TOOL_PRESETS:
            for dia in self.DEFAULT_TOOL_DIAMETERS:
                tapers = self.DEFAULT_TAPER_ANGLES if isTaper else (0.0,)
                for taper in tapers:
                    suffix = f"{taper:g}°" if isTaper else ""
                    tool = Tool(
                        name=f"Ø{dia:g} {label} {suffix}".strip(),
                        shape=shape,
                        geometry={
                            "diameter": dia,
                            "flute_length": max(15.0, dia * 5.0),
                            "total_length": max(50.0, dia * 10.0),
                            # 柄径最小 4mm：直径 ≤4 的刀柄径都等于 4
                            "shank_diameter": max(dia, 4.0),
                            "flute_count": 2 if dia <= 6.0 else 4,
                            "taper_angle": taper,
                            "effective_length": max(20.0, dia * 6.0),
                        },
                    )
                    self.toolLibrary.tools.append(tool)

    # ----------------------------------------------------------- 步骤完成态
    def isStepDone(self, step: str) -> bool:
        if step == STEP_MACHINE:
            return self.machine is not None
        if step == STEP_STOCK:
            return self.stock is not None and self.stock.thickness > 0
        if step == STEP_TOOLS:
            return len(self.toolControllers) > 0
        if step == STEP_TOOLPATH:
            return len(self.pathEntries) > 0
        if step == STEP_COLLISION:
            return self.collisionResult is not None
        if step == STEP_SIMULATE:
            # 必须实际执行实体模拟才算完成（添加路径后不打勾）
            return self.simulationDone
        if step == STEP_OUTPUT:
            # 必须实际执行路径输出才算完成（添加路径后不打勾）
            return self.outputDone
        return False

    def stepSummary(self, step: str) -> str:
        """向导面板显示的步骤摘要（一行文字）。"""
        if step == STEP_MACHINE:
            base = f"{self.machine.name} | {self.machine.controller} | 行程 {self.machine.travel.x:g}×{self.machine.travel.y:g}×{self.machine.travel.z:g} | 安全 {self.machine.defaults.safe_height:g} / 间隙 {self.machine.defaults.clearance_plane:g}"
            if self.machineType == "4axis":
                return (f"[4轴 {self.rotaryAxis}] {base}"
                        f" | 旋转 {self.rotaryTravelMin:g}~{self.rotaryTravelMax:g}°")
            if self.machineType == "5axis":
                return f"[5轴] {base}"
            return base
        if step == STEP_STOCK:
            n = len(self.fixture.clamps) + len(self.fixture.side_clamps)
            return f"{self.materialType} | 毛坯 {self.stock.width:g}×{self.stock.height:g}×{self.stock.thickness:g} mm | 夹具 {n} 处"
        if step == STEP_TOOLS:
            # 刀具信息由左侧栏"刀具"步骤的刀具列表直接展示，摘要留空避免重复
            return ""
        if step == STEP_TOOLPATH:
            if not self.pathEntries:
                return "未生成"
            return f"共 {len(self.pathEntries)} 条路径"
        if step == STEP_COLLISION:
            if self.collisionResult is None:
                return "未检查"
            return "通过" if self.collisionResult.ok else f"发现 {len(self.collisionResult.collision_points)} 处碰撞"
        if step == STEP_SIMULATE:
            return ""
        if step == STEP_OUTPUT:
            return f"{len(self.gcode)} 字符" if self.gcode else "未生成"
        return ""

    def nextUndoneStep(self) -> Optional[str]:
        """下一个未完成的步骤（向导引导用）。"""
        for s in STEP_ORDER:
            if not self.isStepDone(s):
                return s
        return None

    # ----------------------------------------------------------- 路径条目管理
    @staticmethod
    def makeEntryTitle(method_name: str, tool_diameter: float,
                       cut_depth: float = 0.0) -> str:
        """路径标题：加工方式-刀具直径-加工深度，如"钻孔-3mm-深3mm"。"""
        return f"{method_name}-{tool_diameter:g}mm-深{abs(cut_depth):g}mm"

    def rebuildAggregates(self) -> None:
        """把所有条目的刀路/G 代码聚合成 toolpaths/gcode（碰撞/模拟/输出用）。"""
        self.toolpaths = []
        gcodes = []
        for e in self.pathEntries:
            self.toolpaths.extend(e.toolpaths)
            if e.gcode:
                gcodes.append(e.gcode)
        self.gcode = "\n".join(gcodes)

    def addPathEntry(self, entry: ToolpathEntry) -> None:
        self.pathEntries.append(entry)
        self.rebuildAggregates()
        self.notifyChanged()

    def updatePathEntry(self, entry: ToolpathEntry) -> None:
        self.rebuildAggregates()
        self.notifyChanged()

    def removePathEntry(self, entry: ToolpathEntry) -> None:
        if entry in self.pathEntries:
            self.pathEntries.remove(entry)
        self.rebuildAggregates()
        self.notifyChanged()

    def setEntryVisible(self, entry: ToolpathEntry, visible: bool) -> None:
        entry.visible = visible
        self.notifyChanged()

    def setEntryColor(self, entry: ToolpathEntry, hex_color: str) -> None:
        """设置单条路径的画布显示颜色（空字符串=恢复默认按序取色）。"""
        entry.color = hex_color
        self.notifyChanged()

    # ----------------------------------------------------------- 修改通知
    def notifyChanged(self) -> None:
        """任意步骤数据变化后调用，发信号刷新向导面板。"""
        self.stateChanged.emit()


    # ------------------------------------------------------ 保存 / 恢复
    def toSaveDict(self) -> dict:
        """序列化 CNC 模块全部持久内容（机床/毛坯/夹具/刀具表/路径条目）。

        路径条目的 picked 画布对象引用转对象 id；刀路 IR 用
        dataclasses.asdict；Pydantic 模型用 model_dump()（含全部字段）。
        运行时状态（模拟播放、体素、聚合缓存 toolpaths/gcode 可重建）
        不落盘。返回的 dict 可直接 json.dump。
        """
        import dataclasses
        entries = []
        for e in self.pathEntries:
            tp = [{
                "operation_name": t.operation_name,
                "tool_number": int(t.tool_number),
                "instructions": [dataclasses.asdict(i) for i in t.instructions],
            } for t in e.toolpaths]
            entries.append({
                "method_id": e.method_id,
                "method_name": e.method_name,
                "title": e.title,
                "params": dict(e.params),
                "picked": {k: [o.id() for o in v]
                           for k, v in e.picked.items()},
                "toolpaths": tp,
                "gcode": e.gcode,
                "visible": e.visible,
                "simEnabled": e.simEnabled,
                "color": e.color,
            })
        try:
            machine = self.machine.model_dump()
        except Exception:
            machine = {}
        try:
            stock = self.stock.model_dump()
        except Exception:
            stock = {}
        return {
            "machine": machine,
            "machineType": self.machineType,
            "rotaryAxis": self.rotaryAxis,
            "rotaryTravelMin": self.rotaryTravelMin,
            "rotaryTravelMax": self.rotaryTravelMax,
            "rotaryMaxRpm": self.rotaryMaxRpm,
            "stock": stock,
            "materialType": self.materialType,
            "materialHardness": self.materialHardness,
            "fixture": {
                "clamps": [dataclasses.asdict(c) for c in self.fixture.clamps],
                "side_clamps": [dataclasses.asdict(c)
                                for c in self.fixture.side_clamps],
            },
            "showStock": self.showStock,
            "showTools": self.showTools,
            "tools": [t.model_dump() for t in self.toolLibrary.tools],
            "controllers": [c.model_dump() for c in self.toolControllers],
            "pathEntries": entries,
            "simulationDone": self.simulationDone,
            "outputDone": self.outputDone,
        }

    def restoreFromDict(self, data: dict, doc=None) -> None:
        """从 toSaveDict 恢复 CNC 模块状态。

        doc: Document（用于路径条目 picked 的对象 id → 引用解析，对象
        需已加载）。任何单项失败不阻塞整体恢复。
        """
        from pymillcam.core.machine import MachineDefinition
        from pymillcam.core.project import Stock
        from pymillcam.core.tools import Tool, ToolController
        from pymillcam.core.tool_library import ToolLibrary
        from pymillcam.engine.ir import Toolpath, IRInstruction
        try:
            md = data.get("machine")
            if md:
                self.machine = MachineDefinition.model_validate(md)
        except Exception:
            pass
        self.machineType = data.get("machineType", self.machineType)
        self.rotaryAxis = data.get("rotaryAxis", self.rotaryAxis)
        try:
            self.rotaryTravelMin = float(data.get(
                "rotaryTravelMin", self.rotaryTravelMin))
            self.rotaryTravelMax = float(data.get(
                "rotaryTravelMax", self.rotaryTravelMax))
            self.rotaryMaxRpm = int(data.get("rotaryMaxRpm", self.rotaryMaxRpm))
        except Exception:
            pass
        try:
            sd = data.get("stock")
            if sd:
                self.stock = Stock.model_validate(sd)
        except Exception:
            pass
        self.materialType = data.get("materialType", self.materialType)
        try:
            self.materialHardness = float(data.get(
                "materialHardness", self.materialHardness))
        except Exception:
            pass
        fx = data.get("fixture") or {}
        try:
            self.fixture = FixtureSetup(
                clamps=[ClampPlate(**c) for c in fx.get("clamps", [])],
                side_clamps=[SideClamp(**c)
                             for c in fx.get("side_clamps", [])])
        except Exception:
            pass
        self.showStock = bool(data.get("showStock", False))
        self.showTools = bool(data.get("showTools", False))
        lib = ToolLibrary()
        for td in data.get("tools", []):
            try:
                lib.tools.append(Tool.model_validate(td))
            except Exception:
                continue
        self.toolLibrary = lib
        self.toolControllers = []
        for ctl in data.get("controllers", []):
            try:
                self.toolControllers.append(
                    ToolController.model_validate(ctl))
            except Exception:
                continue
        entries = []
        for ed in data.get("pathEntries", []):
            try:
                picked = {}
                for k, ids in (ed.get("picked") or {}).items():
                    objs = []
                    if doc is not None:
                        for oid in ids:
                            o = doc.object(oid)
                            if o is not None:
                                objs.append(o)
                    picked[k] = objs
                tps = []
                for t in ed.get("toolpaths", []):
                    try:
                        tps.append(Toolpath(
                            operation_name=str(t.get("operation_name", "")),
                            tool_number=int(t.get("tool_number", 1)),
                            instructions=[
                                IRInstruction(
                                    **{**ins, "type": MoveType(ins["type"])}
                                    if isinstance(ins.get("type"), str)
                                    else ins)
                                for ins in t.get("instructions", [])]))
                    except Exception:
                        continue
                entries.append(ToolpathEntry(
                    method_id=int(ed.get("method_id", 0)),
                    method_name=str(ed.get("method_name", "")),
                    title=str(ed.get("title", "")),
                    params=dict(ed.get("params") or {}),
                    picked=picked,
                    toolpaths=tps,
                    gcode=str(ed.get("gcode", "")),
                    visible=bool(ed.get("visible", True)),
                    simEnabled=bool(ed.get("simEnabled", True)),
                    color=str(ed.get("color", ""))))
            except Exception:
                continue
        self.pathEntries = entries
        self.simulationDone = bool(data.get("simulationDone", False))
        self.outputDone = bool(data.get("outputDone", False))
        self.rebuildAggregates()
        try:
            self.stateChanged.emit()
        except Exception:
            pass

# CNC 路径输出 · 后处理体系设计文档

- 版本：v1.0（设计定稿）
- 状态：待实现
- 关联文件：`core/cnc/service.py`、`core/cnc/state.py`、`ui/widgets/cnc_dialogs.py`、`ui/commands/concrete/cnc_commands.py`、`ui/commands/param_panels.py`
- 黄金参照：`模板1.NC`（真实机床输出）、`Mach3三轴后处理.epst`（ArtCAM 后处理定义）

---

## 1. 背景与目标

### 1.1 现状

- 刀路生成已覆盖：轮廓铣 / 钻孔 / 扩孔 / 铣螺纹 / 单线切割 / 摆槽 / 区域加工 / 3D 粗加工 / 3D 精修。
- 数据流：**IR Toolpath → Post（`get_post(controller)` 硬编码 grbl/uccnc）→ G 代码字符串**。
- UI 第 7 步"路径输出"已有：生成 G 代码 + 只读显示 + 复制/保存（`CncGcodeDialog`）。
- 缺陷：后处理仅两项硬编码；无模态优化（坐标每行全量输出）；保存走 `ascii + errors=replace`（中文注释会丢失）；默认控制器为 grbl。

### 1.2 目标

1. **默认后处理 = Mach3 三轴（精雕 JD50）**，输出格式与 `模板1.NC` 逐规则一致。
2. 后置文件可扩展：内置 3 个（mach3 / grbl / uccnc）+ 用户自定义目录 + 导入 `.py`。
3. 模态优化达到 ArtCAM 水准：G 码 / X Y Z / F / 主轴 / 冷却只输出变化的量。
4. 机床设置对话框可编辑程序头 / 程序尾 / 换刀宏（macros），默认取所选后置文件。
5. 导出 NC 文件：默认 ASCII 编码 + CRLF 行尾（Mach3 最兼容）。

---

## 2. 架构总览

```
┌─ ① 后置文件库 ──────────────────────────────────────────────┐
│  内置 core/cnc/posts/*.py  +  用户 %APPDATA%\Cangai\posts\  +  导入 .py │
│  （每个 .py = 一个 EnhancedGcodePost 子类 = 一个后置文件）           │
└──────────────────────┬──────────────────────────────────────┘
                       │ 扫描 → 注册表（替换 POST_REGISTRY 硬编码两项）
┌──────────────────────▼──────────────────────────────────────┐
│ ② 后处理引擎 core/cnc/posts/                                  │
│  EnhancedGcodePost（模态状态机 + 方言配置类属性）                │
│    ├─ Mach3Post（默认 · JD50 规格）                            │
│    ├─ GrblPost（行为与 pymillcam 现状一致）                    │
│    └─ UccncPost（行为与现状一致）                               │
└──────────────────────┬──────────────────────────────────────┘
                       │ post_program(toolpaths, macros) → str
┌──────────────────────▼──────────────────────────────────────┐
│ ③ 服务层 core/cnc/service.py（UI 不接触后处理类）               │
│  list_posts() / generate_gcode() / export_nc()               │
└──────────────────────┬──────────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────────┐
│ ④ UI 层                                                      │
│  机床设置对话框（+ 程序宏三行编辑）                              │
│  路径输出对话框（后置下拉 + 预览 + 导出 NC）                     │
└─────────────────────────────────────────────────────────────┘
```

设计原则：
- **不修改 vendored pymillcam**（`deps/pymillcam-main/` 保持原样，升级可直接替换）。后处理引擎是 Cangai 侧独立实现，实现 `pymillcam.post.base.PostProcessor` 协议即可对接现有 `generate_program` / `post_program` 调用。
- **UI 只经 service 层**，后置文件体系升级不影响 UI。

---

## 3. 后置文件体系

### 3.1 目录结构

```
cangai_relief/
├── core/cnc/posts/          # Cangai 后处理引擎与内置后置文件
│   ├── __init__.py          # 注册表 + 目录扫描 + list_posts() + get_post()
│   ├── base.py              # EnhancedGcodePost（模态状态机 + 方言配置）
│   ├── mach3_jd.py          # Mach3 三轴（精雕 JD50）—— 默认
│   ├── grbl.py              # GRBL（行为与 pymillcam 现状一致）
│   └── uccnc.py             # UCCNC（行为与现状一致）
└── docs/CNC路径输出后处理体系设计.md
```

用户目录：`%APPDATA%\Cangai\posts\`（首次运行时创建）。用户放入的 `.py` 自动发现、随软件升级保留。

### 3.2 注册与发现

`core/cnc/posts/__init__.py`：

```python
@dataclass
class PostInfo:
    controller_id: str   # 唯一 id（= 类属性 controller，小写，如 "mach3"）
    name: str            # 显示名，如 "Mach3 三轴 (精雕 JD50)"
    description: str     # 描述行
    source: str          # "builtin" / "user"

POST_REGISTRY: dict[str, type[EnhancedGcodePost]] = {}

def scan_posts() -> None: ...   # 扫描内置包 + 用户目录，importlib 加载 *.py，
                                # 收集 EnhancedGcodePost 子类（排除基类），写入 POST_REGISTRY

def list_posts() -> list[PostInfo]: ...   # 按注册顺序返回
def get_post(controller_id: str) -> EnhancedGcodePost: ...
    # 未知 id 回退默认（mach3），与 pymillcam 现状的回退语义一致
```

### 3.3 导入自定义后置文件

- UI"导入后置文件…"→ `QFileDialog.getOpenFileName`（过滤 `*.py`）→ 复制到用户目录 → 重新 `scan_posts()` → 刷新下拉。
- 文件重名时提示覆盖。

---

## 4. EnhancedGcodePost 详细设计

### 4.1 定位

独立实现 `PostProcessor` 协议（`post_program(toolpaths, *, macros=None) -> str`），不继承 `BasicGcodePost`。方言差异全部收敛为**类属性**，用户自定义后置文件 = 改配置 + 可选覆盖少量钩子。

### 4.2 类属性（方言配置表）

| 属性 | 类型 | Mach3 值 | 说明 |
|---|---|---|---|
| `name` | str | `Mach3 三轴 (精雕 JD50)` | 显示名 |
| `description` | str | `ArtCAM 风格 · 默认机床` | UI 描述 |
| `controller` | str | `mach3` | 注册 id（小写） |
| `coord_decimals` | int | `3` | 坐标小数位 |
| `feed_decimals` | int | `1` | 进给小数位 |
| `arc_center_decimals` | int | `3` | I/J/K 小数位 |
| `feed_keep_trailing` | bool | `True` | 进给保留 `.0`（F300.0） |
| `coord_strip_zeros` | bool | `True` | 坐标删尾零（Y4.840→Y4.84） |
| `rapid_code` | str | `G00` | 快移码 |
| `feed_code` | str | `G01` | 直线插补码 |
| `arc_cw_code` / `arc_ccw_code` | str | `G02` / `G03` | 圆弧码 |
| `arc_plane_code` | str | `G17` | 圆弧平面声明（切换才输出） |
| `spindle_on` | str | `S{rpm} M03` | 主轴开模板（`{rpm}` 占位） |
| `spindle_off` | str | `M05` | 主轴停 |
| `spindle_ccw` | str | `S{rpm} M04` | 主轴反转（预留） |
| `coolant_on` / `coolant_off` | str | `M08` / `M09` | 冷却开/关 |
| `dwell` | str | `G04 P{dwell}` | 暂停模板 |
| `program_start` | str | `%` | 程序头宏 |
| `program_end` | str | `M09\nM05\nM30\n%` | 程序尾宏 |
| `tool_change` | str | `T{tool_number} M6` | 换刀宏（`{tool_number}` 占位） |
| `suppress_final_home` | bool | `True` | 吞掉 IR 尾部 `RAPID x=0,y=0`，由 program_end 输出回原点 |
| `modal_g` / `modal_xyz` / `modal_f` | bool | `True` | 模态开关（G 码 / 坐标 / 进给） |
| `emit_line_numbers` | bool | `False` | 行号开关（预留） |
| `comment_prefix` / `comment_suffix` | str | `(` / `)` | 注释包裹（Mach3 模板不输出注释，基类默认不产生） |

### 4.3 模态状态机

内部维护 `_ModalState`：

```python
@dataclass
class _ModalState:
    g: str | None = None           # 当前 G 模式（G00/G01/G02/G03）
    x: float | None = None         # 上一 X
    y: float | None = None
    z: float | None = None
    f: float | None = None         # 上一进给
    s: int | None = None           # 上一主轴转速
    spindle_dir: int | None = None # M03/M04/M05 状态
    coolant: bool | None = None    # 冷却开关状态
    plane: str | None = None       # G17/G18/G19 状态
    tool: int | None = None        # 当前刀号

    def reset(self) -> None: ...   # 全部置 None → 下次强制全量输出
```

输出规则：

| IR 指令 | 输出逻辑 | 模板证据 |
|---|---|---|
| `RAPID` | 若 `modal_g` 且 G 模式 ≠ G00 → 输出 `G00`；XYZ 按 `modal_xyz` 只输出变化的；**换刀后首条 RAPID 强制全量** | `G00 Z5.175` → `X-0.791 Y8.087`（无 G00、无 Z） |
| `FEED` | G 模式 ≠ G01 → 输出 `G01`；XYZ 变化才输出；F 变化才输出 | `G01 Z0.343 F300.0` → `Y0.555 Z0.27 F1200.0`（无 G01/X） |
| `ARC_CW/CCW` | 平面 G17 切换才输出；G02/G03；XYZ 变化才输出；I/J/K 恒输出（增量圆心） | epst `MoveCircularXY` |
| `SPINDLE_ON` | 输出 `spindle_on` 模板（`S16000 M03`）；s 或方向变化才输出 | 仅换刀后一次 |
| `SPINDLE_OFF` | 输出 `spindle_off` | 尾部 |
| `COOLANT_ON/OFF` | 状态切换才输出 `M08` / `M09` | 头尾各一次 |
| `TOOL_CHANGE` | 输出 `tool_change` 宏；**随后 `modal.reset()`** | `T1 M6` 后强制 `M08 S... G00 Z G00 XY` |
| `DWELL` | `G04 P{dwell}` | epst `DwellMS` |
| `COMMENT` | 按 `comment_prefix/suffix` 输出（Mach3 默认无） | 模板无注释 |

**关键边界（来自 epst `FirstMoveAfterToolChange` 语义）**：
- 换刀后第一段运动：`G00`、`Z`（安全高度）、`X`、`Y` 必须全量输出，不受模态省略影响。
- 程序起点（首个 toolpath 首条指令前）等效于一次 reset。

**尾部回原点处理**：IR 通用后缀含 `RAPID x=0, y=0`。Mach3 模板要求 `G0 X0 Y0`（硬编码文本）位于 `M09/M05` 之后。故：
- `suppress_final_home = True` 时，后处理识别"末段 toolpath 的最后一条 RAPID 且 x==0 且 y==0"，**吞掉不输出**，由 `program_end` 宏统一输出 `M09\nM05\nG0 X0 Y0\nM30\n%`。
- 顺序核对（模板尾部）：`G00 Z5.175`（IR 抬刀）→ `M09` → `M05` → `G0 X0 Y0` → `M30` → `%` ✓

### 4.4 双格式器

```python
def _fmt_coord(v: float, decimals: int) -> str:
    """坐标：固定 decimals 位 + 删尾零。 4.840→"4.84"  5.175→"5.175"  0.0→"0" """
    text = f"{v:.{decimals}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text

def _fmt_feed(v: float, decimals: int) -> str:
    """进给：固定 decimals 位 + 保留尾零。 300.0→"300.0"  1200.0→"1200.0" """
    return f"{v:.{decimals}f}"
```

- 模板核验：坐标 `Y4.840 → Y4.84`、`Z5.175 → Z5.175`；进给 `F300.0`、`F1200.0` 均带 `.0`。
- Mach3 配置：`coord_decimals=3`、`feed_decimals=1`、`coord_strip_zeros=True`、`feed_keep_trailing=True`。

### 4.5 post_program 流程

```
post_program(toolpaths, macros=None)
  1. effective = {类属性默认宏} ∪ macros（机床覆盖）
  2. 输出 program_start 宏（splitlines 逐行）
  3. state = _ModalState()；state.reset()
  4. for tp in toolpaths:
       for inst in tp.instructions:
           line = _emit(inst, state, effective)   # 模态状态机
           if line: lines.append(line)
  5. 输出 program_end 宏
  6. 行尾统一 CRLF（"\r\n"），结尾换行
```

---

## 5. 内置后置文件规格

### 5.1 Mach3（默认）· 与 `模板1.NC` 逐项核验

| # | 规则 | 期望输出 | 模板证据 | 核验 |
|---|---|---|---|---|
| 1 | 程序头 | `%` | 首行 `%` | ✅ |
| 2 | 换刀 | `T1 M6` | `T1 M6` | ✅ |
| 3 | 冷却开 | `M08` | `M08` | ✅ |
| 4 | 主轴 | `S16000 M03`（S 在前） | `S16000 M03` | ✅ |
| 5 | 安全抬刀 | `G00 Z5.175` | `G00 Z5.175` | ✅ |
| 6 | 定位 | `G00 X-0.681 Y0.416` | 同左 | ✅ |
| 7 | 模态 Z 快移 | `Z5.175`（无 G00） | `Z5.175` | ✅ |
| 8 | 下刀 | `G01 Z0.343 F300.0` | 同左 | ✅ |
| 9 | 走刀 | `Y0.555 Z0.27 F1200.0`（省略 G01/X） | 同左 | ✅ |
| 10 | 坐标格式 | 3 位删尾零 | `Y4.84` / `Z5.175` | ✅ |
| 11 | 进给格式 | 1 位保留 `.0` | `F300.0` | ✅ |
| 12 | 抬刀 | `G00 Z2.376` | `G00 Z2.376` | ✅ |
| 13 | 冷却关 / 主轴停 | `M09` / `M05` | 尾部 `M09` `M05` | ✅ |
| 14 | 回原点 | `G0 X0 Y0`（由宏输出） | 尾部 `G0 X0 Y0` | ✅ |
| 15 | 程序尾 | `M30` + `%` | `M30` `%` | ✅ |
| 16 | 行尾 | CRLF | 116263 行全 CRLF | ✅ |
| 17 | 无行号 / 无 G21 G90 / 无注释 | — | 全文件零出现 | ✅ |
| 18 | 圆弧（模板无实例） | `G17` + `G02/G03` + I/J/K 增量 3 位 | epst `MoveCircularXY` + `ArcCenterXYZ(mIncreMode=TRUE)` | ✅ |

**未覆盖说明**：
- 钻孔：pymillcam 引擎展开为 G0/G1（`engine/drill.py` 注释明确），本设计**第一版不实现 G81/G83 固定循环**，展开式输出对 Mach3 完全兼容；后续如需真循环，属 IR 扩展 + 后处理增强，另行设计。
- 刀补 G41/G42、长度补偿 G43：当前 IR 无补偿指令，预留（`spindle_on` 等模板已有占位思路），不阻塞本期。

### 5.2 GRBL / UCCNC

- 迁移进 `core/cnc/posts/`，**输出行为与 pymillcam 现状逐字一致**（防回归）：
  - `rapid_code="G0"`、`feed_code="G1"`、`spindle_on="M3 S{rpm}"`、`coolant_on="M8"`、`coolant_off="M9"`、`coord_decimals=4`、`program_start="G21 G90"`、`program_end="M5\nM30"`、`tool_change="T{tool_number} M6"`。
  - 模态：与现状一致（grbl/uccnc 暂不启用坐标省略，`modal_xyz=False`；G/F 模态保持）。
- 内置免责注释由 `comment_prefix` 控制：grbl/uccnc 保留原注释行（通过 `header_comments` 类属性），Mach3 关闭。

---

## 6. 服务层 API

`core/cnc/service.py` 新增（UI 只 import 这些）：

```python
@dataclass
class PostInfo:
    controller_id: str
    name: str
    description: str
    source: str          # "builtin" | "user"

def list_posts() -> list[PostInfo]: ...
    # 首次调用触发 scan_posts()

def get_post(controller_id: str | None = None) -> EnhancedGcodePost:
    # 默认用 CncState.instance().machine.controller

def generate_gcode(toolpaths: list,
                   controller_id: str | None = None,
                   macros: dict | None = None) -> str:
    # 封装 post.post_program；异常包装为 CncError

def export_nc(text: str, path: str, *,
              encoding: str = "ascii",
              crlf: bool = True) -> None:
    # 统一写盘：行尾替换为 \r\n；ascii 用 errors="replace"（并提示）
```

兼容性：现有 `generate_profile_gcode` / `generate_ir_gcode` 内部改为走 `generate_gcode`，签名不变，UI 现有调用不受影响。

---

## 7. UI 设计

### 7.1 机床设置对话框（`cnc_dialogs.py` `_buildCommonPage`）

```
┌─ 控制器（下拉，默认 mach3）─────────────────────┐
│  Mach3 三轴 (精雕 JD50)  ▼                     │
│  GRBL · UCCNC                                  │
└────────────────────────────────────────────────┘
┌─ 程序宏（默认取所选后置文件的值）──────────────────┐
│ 程序头: [ %                        ]            │
│ 程序尾: [ M09<CR>M05<CR>M30<CR>%  ]            │
│ 换刀:   [ T{tool_number} M6        ]            │
│ [恢复默认]（重新填充所选后置文件的默认宏）           │
└────────────────────────────────────────────────┘
```

- 读取时：`m.macros` 为空 → 显示后置文件默认宏；非空 → 显示用户值。
- 确定时：三行写入 `m.macros`（空行/空值 = 删除该项，回退后置默认）。
- `MachineDefinition.macros` 已随 `toSaveDict`/`restoreFromDict` 持久化（`model_dump`），无需额外字段。
- 控制器下拉数据源改为 `list_posts()`。

### 7.2 路径输出对话框（`CncOutputCommand` + `CncGcodeDialog` 增强）

```
┌─ 路径输出 ────────────────────────────────────────┐
│ 后处理器: [Mach3 三轴 (精雕 JD50)  ▼]  [导入后置文件…] │
│           描述: ArtCAM 风格 · 默认机床             │
│ G 代码预览（只读 QPlainTextEdit，滚动）            │
│ [导出 NC…]  [复制]  [关闭]                        │
└──────────────────────────────────────────────────┘
```

逻辑：
1. 打开时下拉默认选中 `CncState.machine.controller` 对应项；用户切换后 `state.machine.controller` 同步（后续重生成沿用）。
2. 若 `state.gcode` 为空或后置切换 → 用当前后置 + `state.machine.macros` 重新 `generate_gcode(state.toolpaths)` 填充预览。
3. "导出 NC…"→ `QFileDialog.getSaveFileName`（默认 `{文档名}.nc`，过滤 `NC 文件 (*.nc);;G 代码 (*.gcode *.ngc);;所有文件 (*)`）→ `export_nc`（ASCII + CRLF）→ 成功提示。
4. "导入后置文件…"→ 复制到用户目录 → `scan_posts()` → 刷新下拉。

---

## 8. 状态默认值变更

| 位置 | 现值 | 改为 |
|---|---|---|
| `core/cnc/state.py` `CncState.__init__` | `MachineDefinition(controller="grbl")` | `controller="mach3"` |
| `core/cnc/service.py` 各 Params | `controller: str = "grbl"` | `controller: str = "mach3"` |
| `ui/widgets/cnc_dialogs.py` 控制器下拉 | 硬编码 GRBL/UCCNC | `list_posts()` 填充，默认 mach3 |
| `ui/commands/concrete/cnc_commands.py` `CncOutputCommand` | 直接 `get_post(state.machine.controller)` | 走 `service.generate_gcode` + 后置下拉交互 |

---

## 9. 验证方案

### 9.1 格式对照（Mach3 vs 模板规则断言）

用固定小几何（20×20 矩形轮廓 + 一条斜线 + 一个圆）生成 G 代码，断言：

- 首行 `%`；次行 `T1 M6`
- 存在 `S\d+ M03`（S 在前）；无 `M3 S`
- 存在 `M08`；尾部 `M09` 在 `M05` 前、`M05` 在 `M30` 前
- 尾部含 `G0 X0 Y0`、`M30`、末行 `%`
- 全文无 `N\d+`、无 `G21`、无 `(` 注释
- 走刀段出现"省略 G01/X 的行"（如 `Y.. Z.. F..` 形式）
- 坐标 ≤3 位小数；F 形如 `F\d+\.\d`
- 行尾全部 `\r\n`

### 9.2 黄金样例（快照回归）

手工构造 5 条指令序列（换刀→主轴→抬刀→定位→直线→圆弧→抬刀→回原点），期望输出硬编码为黄金文件，跑通即锁定。

### 9.3 回归（防破坏）

- grbl/uccnc：同一 project 改造前后输出**逐字一致**。
- 现有 6 类加工（Profile/Drill/Bore/Thread/Wire/Groove）+ 3D 粗/精：全部走一遍生成不报错。

### 9.4 真实对照

- 用你的浮雕模型跑一次 Mach3 输出，抽样与 `模板1.NC` 的头/尾/模态行人工比对。

---

## 10. 实现清单（顺序）

| # | 任务 | 涉及文件 |
|---|---|---|
| 1 | `posts/base.py`：EnhancedGcodePost（模态状态机 + 双格式器 + 方言类属性） | 新增 |
| 2 | `posts/mach3_jd.py` / `grbl.py` / `uccnc.py` 三个内置后置文件 | 新增 |
| 3 | `posts/__init__.py`：扫描注册 + `list_posts` / `get_post` | 新增 |
| 4 | `service.py`：`generate_gcode` / `export_nc` / `list_posts` 接入；各 Params 默认 mach3 | 修改 |
| 5 | `state.py`：默认 controller=mach3 | 修改 |
| 6 | `cnc_dialogs.py`：控制器下拉数据源 + 程序宏编辑组 | 修改 |
| 7 | `cnc_commands.py` + `param_panels.py`：路径输出对话框（后置下拉 + 导入 + 导出 NC） | 修改 |
| 8 | 验证脚本：格式对照 + 黄金样例 + 回归 | 新增 tests/ |
| 9 | 真实模型对照 `模板1.NC` | 手动 |

## 11. 风险与注意事项

- **不改 vendored pymillcam**：所有增强在 Cangai 侧，pymillcam 升级零冲突。
- **模态重置边界**：换刀后首段必须全量输出（epst `FirstMoveAfterToolChange`），遗漏会导致坐标漂移——状态机 reset 后由"首条指令强制全量"保证。
- **尾部回原点顺序**：依赖 `suppress_final_home` 吞掉 IR 的 `RAPID x0y0`，若未来 IR 后缀变化需同步。
- **编码**：默认 ascii+replace（与模板一致、Mach3 最兼容）；中文注释会变 `?`，UI 提示用户。
- **`模板1.NC` 无圆弧/钻孔实例**：圆弧规则以 epst 为准，验证时用黄金样例覆盖，不依赖模板。

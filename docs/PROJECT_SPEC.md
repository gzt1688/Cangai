# Cangai-Relief 项目规范

> 版本：1.0
> 适用范围：`cangai_relief` 仓库全部代码
> 维护原则：**以本文档为唯一事实来源**；任何与本规范冲突的改动，以修改本文档为先。

---

## 1. 项目概述

Cangai-Relief 是一款面向浮雕设计的 CAD 桌面软件，核心能力为：

- **2D 矢量绘制**：点 / 直线 / 圆 / 圆弧 / 椭圆 / 自由曲线 / NURBS 曲线的交互式绘制；
- **对象捕捉**：端点、中点、圆心、交点、垂点、切点、四分点等，支持工具栏开关与右键独选；
- **图层面板 / 属性面板 / 变换面板**：对象管理、属性编辑、平移旋转缩放；
- **3D 浮雕预览**：当前为三维线框/网格占位视口，规划接入 GPU 渲染。

当前形态：桌面 GUI（PySide6），数据层（`core/`）与界面层（`ui/`）分离。

---

## 2. 技术路线规划

### 2.1 技术栈与版本基线

| 类别 | 技术 | 版本 | 说明 |
| --- | --- | --- | --- |
| 运行时 | Python | **3.12.x** | 见下方"版本锁定说明" |
| GUI | PySide6 | 6.11.x | Qt6 官方 Python 绑定 |
| 停靠布局 | PySide6-QtAds | 5.0.0.2 | 源码 vendored 于 `deps/` |
| 2D 画布 | Qt `QGraphicsView / QGraphicsScene` | — | 场景桥同步数据模型 |
| 3D 渲染 | QPainter 软渲染（现状）→ moderngl | 5.12.0 | 规划 GPU 渲染 |
| 数学计算 | numpy | 2.5.x | 样条/NURBS 求值 |
| 可选 | scipy | 1.18.x | `spline_utils` 延迟导入，非必需 |

**版本锁定说明**：整条依赖链（moderngl + glcontext、numba、PyInstaller、PySide6、PySide6-QtAds）
取各包 wheel 覆盖范围的**交集**，当前交集为 **Python 3.12**。
其中 moderngl / glcontext 无 Python 3.14 的 wheel（3.14 需源码编译），故基线不得随意升级。
新增依赖前，必须核对目标 Python 版本是否存在稳定 wheel（`pip index versions` 或 PyPI API）。

### 2.2 分层架构

```
┌─────────────────────────────┐
│  ui/（界面层）               │
│  windows · widgets · view_2d│
│  view_3d · commands · styles│
└─────────────┬───────────────┘
              │ 只允许 ui → core 依赖
┌─────────────▼───────────────┐
│  core/（领域层）             │
│  document · vector · snap   │
│  grid · spline_utils        │
└─────────────────────────────┘
```

**依赖方向铁律**：

- `ui` 层可以依赖 `core` 层；**`core` 层禁止依赖 `ui` 层**；
- `core` 层保持对 Qt 的**弱依赖**（仅 `document` 使用 `QtCore` 信号；
  几何类型层在函数体内延迟导入 `QtGui.QColor`，避免顶层强制依赖）；
- 包内使用相对导入（`from .xxx import`），跨包使用绝对导入（`from core.xxx import`）。

### 2.3 模块地图

| 模块 | 职责 |
| --- | --- |
| `core/document.py` | `Document`（对象集合、选择、信号）、`Layer` 图层模型 |
| `core/vector/` | 几何对象层：`Point3D` 等基础类型、`BaseObject` 抽象基类、各具体对象、`object_factory` 工厂 |
| `core/snap/` | 捕捉：`SnapType` 枚举、`SnapResult` 结构、`SnapManager` 求解 |
| `core/grid_manager.py` | 网格配置与正交约束（单例） |
| `core/spline_utils.py` | Catmull-Rom / B-Spline / NURBS 数学 |
| `ui/windows/mainWindow.py` | 主窗口：Dock 布局、全局信号接线（**唯一**的组装点） |
| `ui/widgets/` | 可复用控件：工具栏、面板、对话框 |
| `ui/view_2d/` | 2D 画布 MVC：View / Scene / ItemFactory / SceneBridge |
| `ui/view_3d/` | 3D 视口（软渲染占位） |
| `ui/commands/` | 命令系统：`CommandManager` 调度、`CommandInstance` 基类、`concrete/` 具体命令 |
| `ui/styles/theme.py` | QSS 主题与颜色常量（所有控件从这里取色，禁止内联魔法色值） |

### 2.4 关键设计决策

1. **命令模式**：所有用户操作收敛为命令（`executeCommand → onMousePress/onEnter → 完成`）。
   新增功能优先扩展命令，不直接写死在 View 事件里。
2. **数据模型与视图分离**：`core/vector` 的对象是纯数据模型；2D 图元由 `item_factory` +
   `scene_bridge` 负责同步；不把 `QGraphicsItem` 逻辑塞进 core。
3. **单例**：`SnapManager` / `GridManager` / `CommandManager` 采用模块级单例（`_instance` +
   `instance()`）。后续抽取统一 `Singleton` 基类时保持接口不变。
4. **信号接线集中在 mainWindow**：跨控件通信一律通过 Qt Signal，避免控件之间互相引用。

### 2.5 路线图（Roadmap）

| 阶段 | 目标 | 里程碑可交付 |
| --- | --- | --- |
| P1（现状） | 2D 绘制闭环、捕捉、图层、属性/变换面板 | `test_draw.py` 全绿 |
| P2 | 撤销/重做接线（`core/undo.py` 已删除，需按命令模式重建）；文件保存/加载（JSON）；打通两套图层模型 | 可保存/恢复工程文件 |
| P3 | 3D 视口接入 moderngl 真实几何渲染（替代 QPainter 占位） | 2D 对象可在 3D 视口正确显示 |
| P4 | 浮雕高度场、网格生成/导出、布尔运算 | 首个可导出浮雕模型 |

---

## 3. 代码规范

### 3.1 命名规范

| 对象 | 规则 | 示例 |
| --- | --- | --- |
| 文件名 | snake_case | `snap_manager.py` |
| 类 / 枚举 / 信号 | PascalCase | `class SnapManager`、`Signal(str, bool)` |
| 函数 / 方法 / 变量 | snake_case | `def get_snap_types()`、`snap_radius` |
| 私有成员 | 单下划线前缀 | `self._enabled_types` |
| 常量 | UPPER_SNAKE | `_AXIS_LENGTH = 80.0` |
| Qt 槽 / 事件处理器 | `on_` / `_on` 前缀 | `def _on_snap_toggled` |

### 3.2 格式

- 缩进 4 空格；行宽 ≤ 100；
- 一行一条语句，**禁止分号连用**（`a = 1; b = 2` 是禁止的）；
- 格式化工具：`ruff format`（兼容 black），并在 CI/本地提交前运行；
- 建议 `pyproject.toml` 中配置：

```toml
[tool.ruff]
line-length = 100
target-version = "py312"
```

### 3.3 import 规范

- 顺序：`__future__` → 标准库 → 第三方 → 本地模块；分组间空一行；
- 包内相对导入，包外绝对导入；
- 禁止函数体内 `import`，除非存在**真正的循环依赖或可选依赖**（`spline_utils` 的 scipy 属后者）；
- 每个模块的 docstring 位于最顶部（`from __future__` 之前）。

### 3.4 类型注解

- 所有**公开 API**（函数签名、方法参数与返回值）必须注解；内部实现也要尽量注解；
- 所有模块首行保持 `from __future__ import annotations`（惰性求值，避免循环类型引用）；
- 禁止裸 `List` / `Dict`，使用 `List[T]`、`dict[str, float]`；
- 常量、dataclass 字段应注解。

### 3.5 docstring

- 每个模块必须有**模块级 docstring**（一句话职责说明，中文）；
- 公开类与方法建议补一行摘要；不强制逐参说明；
- 使用三重双引号；注释用 `#`，全角标点前后不加多余空格。

### 3.6 错误处理

- 禁止 `except: pass` 吞异常；至少 `logging` 或向用户提示；
- UI 事件回调中，预期内的失败走信号/提示，不向用户抛裸 traceback；
- 数据校验放在 core 层入口，UI 层只做展示与交互。

### 3.7 质量门禁（提交前必须通过）

```bash
# 静态检查：未使用 import / 未定义名称
.venv\Scripts\python -m pyflakes core ui main.py test_draw.py
# 回归测试
run.bat test
# 应用启动冒烟
run.bat
```

> 当前状态：pyflakes **零告警**，`test_draw.py` 全绿。

---

## 4. 项目结构规范

```
cangai_relief/
├── main.py                # 唯一入口：QApplication + MainWindow
├── test_draw.py           # 回归冒烟测试（run.bat test）
├── requirements.txt       # 版本锁定依赖
├── run.bat                # 启动 / 测试入口
├── docs/PROJECT_SPEC.md   # 本规范
├── core/                  # 领域层（禁止依赖 ui/）
├── ui/                    # 界面层（可以依赖 core/）
├── deps/                  # vendored 第三方源码（PySide6-QtAds），禁止直接修改
├── icons/                 # SVG 图标
└── resources/             # 资源文件
```

新增代码的位置判断：

- 纯几何/算法/数据 → `core/`；
- 一个命令 → `ui/commands/concrete/`（实现 `CommandInstance` 并注册）；
- 一个可复用控件 → `ui/widgets/`；
- 画布内交互 → `ui/view_2d/`；
- 全局装配与接线 → `mainWindow.py`（**不要**在控件内部直接 new 其他大控件）。

---

## 5. 测试规范

- `test_draw.py` 是当前唯一的回归基线，覆盖六种绘图命令的闭环；
- 测试可访问 `MainWindow` 的私有成员（`w._cm`、`w._doc`），属已知耦合，
  引入 pytest 后应逐步改为公开访问器；
- 规划：`tests/` 目录 + pytest；core 层单测（几何/样条/捕捉）优先，
  UI 层只做冒烟。

---

## 6. Git 工作流规范

> 当前仓库**尚未初始化 git**，以下为落地约定。

- **分支模型**：`main`（可发布） + `feature/*`（功能分支）；禁止直接提交 `main`；
- **Commit Message**（Conventional Commits）：

```
feat(捕捉): 右键独选与停用快照
fix(3d): 修复视口切换后相机重置
refactor(core): 全量文件名改为 snake_case
docs: 新增项目规范
```

- 类型：`feat` / `fix` / `refactor` / `docs` / `test` / `chore` / `perf`；
- 提交前：`pyflakes` 零告警 + `run.bat test` 通过；
- 文件重命名类重构必须与引用同步提交，保证每次提交可运行。

---

## 7. 环境与构建

```bash
# 首次：创建虚拟环境并安装依赖（Python 3.12 必须）
D:\Python-3.12.14\python.exe -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

# 运行
run.bat

# 回归
run.bat test
```

- 打包预留：PyInstaller（Python 3.12 下有 wheel），hook 需覆盖 PySide6 插件与 QtAds；
- 发布时同步升级 `requirements.txt` 并更新本文档 2.1 版本基线。

---

## 8. 已知技术债与清理计划

| # | 债务 | 位置 | 计划 |
| --- | --- | --- | --- |
| 1 | 两套图层模型：`core.Layer` 与 `layer_panel._LayerData` 未打通 | `core/document.py`、`ui/widgets/layer_panel.py` | P2 统一为 core 模型 |
| 2 | 网格绘制硬编码（10.0/count=10），未读 `GridManager` 配置 | `ui/view_2d/graphics_scene.py` | P2 接入配置 |
| 3 | 命令名/中英映射五处重复（`top_dock`、`quick_access_bar`、`mainWindow`…） | 各处 | 抽统一命令注册表 |
| 4 | 单例样板重复（4 处手写 `_instance`） | `core/`、`ui/commands/` | 抽 `Singleton` 基类 |
| 5 | `SnapManager._snapObject` 仅实现 4 种捕捉，枚举声明了 10 种 | `core/snap/snap_manager.py` | P2 补齐策略 |
| 6 | 3D 视口为占位软渲染，与 2D 场景桥未打通 | `ui/view_3d/viewport_3d.py` | P3 moderngl |
| 7 | `PolyCurveObject` 用裸 `dict` 存段数据 | `core/vector/poly_curve_object.py` | 改 typed 数据结构 |
| 8 | 移除/清空 `core/relief/`（空包）与 `resources/Icons`（空目录） | — | 顺手清理 |

> 新增代码**不得**继续制造上述债务；已有债务按 P 优先级逐步消解。

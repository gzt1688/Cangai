# Cangai 开放平台架构：API / 插件 / AI 自动化

> 本文档面向插件开发者与 AI 集成者，说明软件的开放扩展架构、
> 稳定 API 面与接入路径。随 `API_VERSION` 演进，本文档同步更新。

## 1. 设计原则

```
┌─────────────────────────────────────────────────────┐
│  插件 (plugins/)          AI 自动化 (脚本/Agent)      │
│      │                          │                    │
│      └──────────┬───────────────┘                    │
│                 ▼                                    │
│        core.api.CangaiAPI   ← 唯一稳定门面（版本化）   │
│      ┌───────────┼────────────┐                       │
│      ▼           ▼            ▼                       │
│   事件总线     命令/动作注册   文档/几何/设置            │
│  (events)   (commandManifest) (Document/对象)          │
│      └───────────┴────────────┘                       │
│                 ▼                                    │
│      宿主层 MainWindow（界面钩子）                      │
│   工具栏按钮 / 历史记录 / 多文件 / 撤销系统              │
└─────────────────────────────────────────────────────┘
```

1. **单一门面**：第三方代码只 `import core.api`，永不直接依赖 MainWindow
   或内部模块——内部重构不破坏插件。
2. **宿主无关**：门面不依赖 GUI。MainWindow 只是"宿主"之一（提供按钮、
   历史、多文件等界面钩子）；无宿主时 API 独立驱动 core 文档
   （单测/无头批处理/AI 后台自动化正是这样跑的）。
3. **同一套 API 服务人与 AI**：`commandManifest()` 就是 AI 的工具表；
   `runScript()` 是执行入口；`events` 是感知通道。
4. **错误隔离**：插件装载失败、动作抛异常、事件订阅者出错，均只记录
   与提示，不影响主程序与其他插件。

## 2. 插件开发

### 2.1 插件形态与安装

| 形态 | 结构 | 位置 |
|------|------|------|
| 目录插件 | `plugin.json`（清单）+ 入口脚本（默认 `main.py`） | 内置 `plugins\` 或用户目录 |
| 单文件插件 | `xxx.py`（id 取文件名） | 同上 |

- 内置目录：软件目录下 `plugins\`
- 用户目录：`%APPDATA%\Cangai\plugins\`（推荐，升级软件不丢失）
- 同名 id：用户插件覆盖内置。

启用/禁用：设置 → 插件（立即生效；状态持久化于 `render_settings.json`）。

### 2.2 清单 plugin.json

```json
{
  "id": "example_tools",        // 唯一 id（设置存储/卸载的键）
  "name": "示例工具",            // 显示名
  "version": "1.0.0",
  "apiVersion": 1,              // 所兼容的 API 版本（> 当前版本的拒载）
  "description": "演示插件 API",
  "entry": "main.py"
}
```

### 2.3 入口脚本

```python
def register(api):
    # 1) 瞬时动作：纯函数，自动进入命令系统（撤销分组/历史/重复执行）
    def my_action(context):
        doc = api.activeDocument()
        doc.beginEdit()
        oid = api.addPolyline([(0, 0), (100, 0), (50, 80)])
        doc.endEdit()
        api.notify(f"已生成对象 {oid}")

    api.registerAction("myplugin.action1", my_action,
                       title="我的动作",
                       meta={"group": "我的插件",
                             "description": "示例：画一个三角形"})

    # 2) 工具按钮：出现在顶部工具栏"我的插件"分类页（点击执行同名动作）
    api.registerToolButton(key="myplugin.action1", label="三角形",
                           group="我的插件", pluginId="myplugin")

    # 3) 事件订阅：感知文档/命令变化（返回退订闭包）
    unsubscribe = api.events.subscribe(
        "document.added", lambda oid: print("新增对象", oid))

def unregister(api):
    # 可选：停用回调（动作/按钮由管理器自动清理，这里清理手动订阅等资源）
    ...
```

### 2.4 事件主题

| 主题 | 载荷 | 时机 |
|------|------|------|
| `document.added` / `document.removed` / `document.changed` | 对象 id | 文档对象变化 |
| `document.selectionChanged` | None | 选择集变化 |
| `document.layerChanged` | None | 图层变化 |
| `command.started` / `command.finished` / `command.ended` | 命令名 | 命令生命周期 |
| `app.resultMessage` | 文本 | 操作结果反馈 |
| `app.ready` | `{"plugins": [id...]}` | 启动完成（插件装载后） |

## 3. API 参考（`API_VERSION = 1`）

### 文档
| 方法 | 说明 |
|------|------|
| `activeDocument()` | 当前文档（无宿主时为内置无头文档） |
| `documents()` / `openDocument(path)` / `newDocument()` | 多文件系统（需宿主） |
| `beginEdit()` / `endEdit()` | 批量修改的撤销分组（可嵌套） |

### 对象
| 方法 | 说明 |
|------|------|
| `addObject(obj) -> id` / `removeObject(id)` / `getObject(id)` | 增删查 |
| `objects(typeName=?, layerId=?, selected=?)` | 条件查询（如 `typeName="CURVE"`） |
| `selectedObjects()` / `select(oids)` / `clearSelection()` | 选择集 |
| `addPolyline(points, closed=False)` / `addLine(x1,y1,x2,y2)` / `addCircle(cx,cy,r)` / `addPoint(x,y)` | 速建几何，返回 id |

### 扩展注册
| 方法 | 说明 |
|------|------|
| `registerAction(name, fn, title, meta)` | 瞬时动作；`fn(context)`；有宿主时进入命令系统 |
| `registerCommand(name, factory, meta)` | 交互命令（需画布取点的完整流程） |
| `registerToolButton(key, label, group, ...)` | 工具栏按钮（点击执行同名动作/命令） |

### 执行与清单
| 方法 | 说明 |
|------|------|
| `runAction(name, context=None)` | 执行动作（有宿主走命令系统，无宿主直接调用） |
| `executeCommand(name, *args)` / `cancelCommand()` | 交互命令 |
| `commandManifest()` | **AI 工具表**：全部动作/命令的 name/kind/title/description/group |

### UI 服务与脚本
| 方法 | 说明 |
|------|------|
| `notify(text)` / `setStatus(text)` / `appendHistory(text)` | 界面反馈 |
| `runScript(code)` | 执行 Python 脚本（注入 `api`，脚本用 `result = ...` 回传） |
| `runOnMainThread(fn)` | 工作线程改 UI 的唯一安全途径 |
| `pluginSettings(pluginId)` | 插件专属持久化键值存储 |
| `events.subscribe(topic, fn)` | 事件订阅 |

## 4. AI 自动化接入路径

软件的开放 API 天然是 AI 的操作面，推荐分层接入：

### 4.1 工具调用型（Agent + function calling）
1. 启动时（或经 `commandManifest()`）取全部可执行能力作为工具清单；
2. Agent 决定调用 `runAction` / `executeCommand` / 速建几何 API；
3. 通过 `events` 订阅执行反馈（`command.finished`、`app.resultMessage`），
   形成闭环。

### 4.2 脚本生成型
LLM 生成 Python 代码 → `api.runScript(code)` 执行（注入 `api`）→
读取 `result` 返回值。适合批量绘图、参数化生成等复合任务。

### 4.3 无头批处理
门面宿主无关——无 GUI 环境下同样可用（见 `test_api_plugins.py` 的
"无宿主"用例）：

```python
from core.api import CangaiAPI
api = CangaiAPI()
api.addPolyline([...])          # 直接驱动 core 文档
doc = api.activeDocument()
doc.save("out.cai")
```

### 4.4 线程纪律
Qt 界面只能在主线程改动。Agent/工作线程中：
- 纯 core 计算（几何/文档数据）→ 任意线程；
- UI 相关 → `api.runOnMainThread(fn)` 投递回主线程。

## 5. 版本与兼容

- `core.api.API_VERSION`：**1**。删除/改名公开方法、变更语义时递增；
- 插件清单 `apiVersion` 大于当前版本时拒载（提示升级软件）；
- 建议插件以 `hasattr(api, "方法名")` 做可选能力探测，向前兼容。

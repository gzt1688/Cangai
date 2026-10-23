"""Cangai 开放 API：插件、脚本与 AI 自动化的唯一稳定入口。

架构原则（开放平台方向，整体设计约定）：
- **单一门面**：插件/脚本/AI 只 import 本模块，不直接依赖 MainWindow 等内部
  实现——内部重构不破坏第三方插件。
- **宿主无关**：门面本身不依赖 GUI。`attachHost()` 由界面层（MainWindow）
  注入界面钩子；无宿主时（测试/无头自动化/AI 批处理）API 仍可独立驱动
  core 文档完成对象增删改查与动作执行。
- **版本化**：`API_VERSION` 随不兼容变更递增；插件清单声明所兼容版本。
- **同一套 API 服务人与 AI**：`commandManifest()` 输出的命令清单即 AI 的
  工具表；`runScript()` 是 AI 的执行入口；`events` 是感知通道。

线程约定：Qt 界面只能在主线程改动。脚本/AI 从工作线程调用时，请把 UI 相关
操作经 `runOnMainThread(fn)` 投递回主线程；纯 core 计算可在任意线程。
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.document import Document
from core.events import EventBus
from core.vector.circle_object import CircleObject
from core.vector.curve_object import CurveObject
from core.vector.line_object import LineObject
from core.vector.point_object import PointObject
from core.vector.geometry_types import Point3D

# 对外 API 版本：不兼容变更时递增（插件清单 apiVersion 与此比对）
API_VERSION = 1

# 内置插件目录（随软件分发）与用户插件目录（%APPDATA%/Cangai/plugins）
def builtinPluginDir() -> str:
    """插件目录：源码树为项目根 plugins\；打包版为 exe 同级 plugins\。

    打包脚本会在发布包里创建该空目录，用户把自己写的插件放进去即可
    （同名 id 时用户投放的版本覆盖内置）。
    """
    if getattr(sys, "frozen", False):
        return os.path.join(
            os.path.dirname(os.path.abspath(sys.executable)), "plugins")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                        "plugins")


def userPluginDir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Cangai", "plugins")


def privatePluginDir() -> str:
    """私有插件目录（源码树级，**不随软件分发**——打包脚本只带 plugins/）。

    开发期个人功能放这里：进版本库、开发运行可装载，但公共发布版
    天然不包含（目录不存在时发现阶段直接跳过）。
    """
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                        "plugins_private")


@dataclass
class ToolButtonSpec:
    """工具按钮注册描述：出现在顶部工具分类页上。"""
    group: str                      # 工具分类页名（不存在则自动新建标签页）
    key: str                        # 唯一键（动作/命令名，重复注册覆盖）
    label: str                      # 按钮文字
    tooltip: str = ""
    icon: str = ""                  # 可选 SVG 路径
    pluginId: str = ""              # 注册来源插件（管理/清理用）
    meta: dict = field(default_factory=dict)


@dataclass
class ActionEntry:
    fn: Callable                    # fn(context: dict) -> Any
    meta: dict
    pluginId: str = ""


class PluginSettings:
    """插件专属设置（按插件 id 隔离的 JSON 键值存储，随文档目录落盘）。"""

    def __init__(self, pluginId: str, path: str) -> None:
        self.pluginId = pluginId
        self._path = path
        self._data: dict = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (OSError, ValueError):
            self._data = {}

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        self._data[key] = value
        self.save()

    def update(self, values: dict) -> None:
        self._data.update(values)
        self.save()

    def asDict(self) -> dict:
        return dict(self._data)

    def save(self) -> bool:
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            return True
        except OSError:
            return False


class CangaiAPI:
    """应用门面。插件通过 `register(api)` 收到的 `api` 即本类实例。"""

    API_VERSION = API_VERSION

    def __init__(self) -> None:
        self.events = EventBus()
        self._host = None
        self._actions: Dict[str, ActionEntry] = {}       # 瞬时动作（纯函数）
        self._commands: Dict[str, dict] = {}             # 交互命令（工厂）
        self._buttons: Dict[str, ToolButtonSpec] = {}    # 工具按钮（key 去重）
        self._settingsCache: Dict[str, PluginSettings] = {}
        self._fallbackDoc: Document | None = None        # 无宿主时的独立文档
        self._mainThreadInvoker = None
        self._pluginScope: str = ""                      # 插件装载窗口期（自动归属注册项）
        self._relayDoc = None
        self._relaySlots = None

    # ---------------- 插件装载作用域（PluginManager 专用） ----------------

    def beginPluginScope(self, pluginId: str) -> None:
        """装载窗口期：期间的动作/命令/按钮注册自动归属该插件（启停清理用）。"""
        self._pluginScope = pluginId

    def endPluginScope(self) -> None:
        self._pluginScope = ""

    def snapshotRegistrations(self) -> tuple:
        """登记快照（动作/命令/按钮名集合），供装载器 diff 出插件注册项。"""
        return (set(self._actions), set(self._commands), set(self._buttons))

    def unregisterRegistration(self, name: str) -> bool:
        """按名注销动作或命令（宿主命令表中同步移除）。"""
        had = False
        if name in self._actions:
            self._actions.pop(name)
            had = True
        if name in self._commands:
            self._commands.pop(name)
            had = True
        if had and self._host is not None:
            self._host.unregisterCommand(name)
        return had

    def removeButtonsForPlugin(self, pluginId: str) -> int:
        """移除某插件注册的全部工具按钮，返回移除数。"""
        keys = [k for k, s in self._buttons.items() if s.pluginId == pluginId]
        for k in keys:
            self._buttons.pop(k)
        if keys and self._host is not None:
            self._host.removeToolButtons(keys)
        return len(keys)

    # ---------------- 宿主接线 ----------------

    def attachHost(self, host) -> None:
        """由界面宿主（MainWindow）调用：注入界面钩子并回放启动期缓冲的注册。
        宿主需实现 registerActionCommand / registerInteractiveCommand /
        addToolButton / wireApiEvents / runOnMainThread / notify / setStatus /
        appendHistory / executeCommand / activeDocument / documents /
        openDocument / newDocument（详见 docs/API与插件架构.md）。"""
        self._host = host
        for name, e in self._actions.items():
            host.registerActionCommand(name, e.fn, e.meta)
        for name, e in self._commands.items():
            host.registerInteractiveCommand(name, e["factory"], e["meta"])
        for spec in self._buttons.values():
            host.addToolButton(spec)
        host.wireApiEvents(self.events)

    def detachHost(self) -> None:
        self._host = None

    def hasHost(self) -> bool:
        return self._host is not None

    # ---------------- 文档访问 ----------------

    def activeDocument(self) -> Document:
        """当前活动文档；无宿主时返回门面自带的独立文档（无头模式）。"""
        if self._host is not None:
            return self._host.activeDocument()
        if self._fallbackDoc is None:
            self._fallbackDoc = Document()
            self._fallbackDoc.setTitle("无头文档")
            self.relayDocumentEvents(self._fallbackDoc)   # 无宿主也走事件总线
        return self._fallbackDoc

    def documents(self) -> List[Document]:
        if self._host is not None:
            return self._host.documents()
        return [self.activeDocument()]

    def openDocument(self, path: str) -> Document:
        if self._host is None:
            raise RuntimeError("openDocument 需要界面宿主")
        return self._host.openDocument(path)

    def newDocument(self) -> Document:
        if self._host is None:
            raise RuntimeError("newDocument 需要界面宿主")
        return self._host.newDocument()

    # ---------------- 对象操作 ----------------

    def addObject(self, obj) -> str:
        """加入对象（自动分层/撤销快照由文档负责），返回对象 id。"""
        doc = self.activeDocument()
        doc.addObject(obj)
        return obj.id()

    def removeObject(self, oid: str) -> bool:
        doc = self.activeDocument()
        return doc.removeObject(oid) is not None

    def getObject(self, oid: str):
        return self.activeDocument().object(oid)

    def objects(self, typeName: str | None = None, layerId: int | None = None,
                selected: bool | None = None) -> List:
        """按类型名/图层/选中状态查询对象（如 typeName="CURVE"）。
        跨空间查询：包含 2D/浮雕空间与 3D 空间的全部对象。"""
        out = []
        for o in self.activeDocument().allObjects():
            if typeName is not None and o.type().name != typeName:
                continue
            if layerId is not None and o.layerId() != layerId:
                continue
            if selected is not None and o.isSelected() != selected:
                continue
            out.append(o)
        return out

    def selectedObjects(self) -> List:
        return self.activeDocument().selectedObjects()

    def clearSelection(self) -> None:
        self.activeDocument().clearSelection()

    def select(self, oids: List[str], append: bool = False) -> None:
        doc = self.activeDocument()
        if not append:
            doc.clearSelection()
        for oid in oids:
            doc.selectObject(oid, append=True)

    def beginEdit(self) -> None:
        """开始一次可撤销的批量修改（与 endEdit 成对；可嵌套）。"""
        self.activeDocument().beginEdit()

    def endEdit(self) -> None:
        self.activeDocument().endEdit()

    # ---------------- 速建几何 ----------------

    def addPolyline(self, points, closed: bool = False) -> str:
        """由 [(x, y), ...] 世界坐标点列创建曲线对象，返回 id。"""
        curve = CurveObject()
        curve.buildFromSampled(
            [Point3D(float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else 0.0)
             for p in points])
        curve.setClosed(closed)
        return self.addObject(curve)

    def addLine(self, x1, y1, x2, y2) -> str:
        line = LineObject([Point3D(float(x1), float(y1), 0.0),
                           Point3D(float(x2), float(y2), 0.0)])
        return self.addObject(line)

    def addCircle(self, cx: float, cy: float, radius: float) -> str:
        c = CircleObject()
        c.setCenter(Point3D(float(cx), float(cy), 0.0))
        c.setRadius(float(radius))
        return self.addObject(c)

    def addPoint(self, x: float, y: float) -> str:
        p = PointObject()
        p.setPosition(Point3D(float(x), float(y), 0.0))
        return self.addObject(p)

    # ---------------- 扩展注册（插件主要入口） ----------------

    def registerAction(self, name: str, fn: Callable, title: str | None = None,
                       meta: dict | None = None, pluginId: str = "") -> None:
        """注册瞬时动作（无画布交互的纯函数）：fn(context) -> Any。
        有宿主时包装为命令（进入命令系统：撤销分组/历史/重复执行）；
        无宿主时 runAction 直接调用（无头自动化）。重复注册按名覆盖。"""
        m = dict(meta or {})
        m.setdefault("title", title or name)
        self._actions[name] = ActionEntry(
            fn=fn, meta=m, pluginId=pluginId or self._pluginScope)
        if self._host is not None:
            self._host.registerActionCommand(name, fn, m)

    def registerCommand(self, name: str, factory: Callable,
                        meta: dict | None = None) -> None:
        """注册交互命令（factory(*args) 返回 CommandInstance 子类实例，
        供需要画布取点的完整命令流程使用）。"""
        self._commands[name] = {"factory": factory, "meta": dict(meta or {})}
        if self._host is not None:
            self._host.registerInteractiveCommand(name, factory, dict(meta or {}))

    def registerToolButton(self, key: str, label: str, group: str = "插件",
                           tooltip: str = "", icon: str = "",
                           pluginId: str = "", meta: dict | None = None) -> None:
        """在顶部工具区注册按钮：点击执行同名动作/命令（key 即注册名）。
        group 对应工具分类页，不存在则自动新建。"""
        spec = ToolButtonSpec(group=group, key=key, label=label,
                              tooltip=tooltip or label, icon=icon,
                              pluginId=pluginId or self._pluginScope,
                              meta=dict(meta or {}))
        self._buttons[key] = spec
        if self._host is not None:
            self._host.addToolButton(spec)

    def registerToolDivider(self, group: str = "插件",
                            pluginId: str = "") -> None:
        """在顶部工具区的分组内插入一条竖向分割线（同组内区分子功能）。
        以特殊 ToolButtonSpec 登记，插件停用时随按钮一并移除。"""
        n = sum(1 for k in self._buttons if "::divider::" in k) + 1
        key = f"{group}::divider::{n}"
        spec = ToolButtonSpec(group=group, key=key, label="", tooltip="",
                              icon="", pluginId=pluginId or self._pluginScope,
                              meta={"divider": True})
        self._buttons[key] = spec
        if self._host is not None:
            self._host.addToolButton(spec)

    # ---------------- 执行 ----------------

    def runAction(self, name: str, context: dict | None = None) -> Any:
        """执行已注册动作：有宿主走命令系统（撤销/历史），无宿主直接调用。"""
        entry = self._actions.get(name)
        if entry is None:
            raise KeyError(f"动作未注册: {name}")
        if self._host is not None:
            self._host.executeCommand(name)
            return None
        return entry.fn(dict(context or {}))

    def executeCommand(self, name: str, *args) -> bool:
        """执行交互命令（画布取点类完整流程）。"""
        if self._host is not None:
            return self._host.executeCommand(name, *args)
        raise RuntimeError("交互命令需要界面宿主")

    def cancelCommand(self) -> None:
        if self._host is not None:
            self._host.cancelActiveCommand()

    # ---------------- 清单（AI 工具表） ----------------

    def commandManifest(self) -> List[dict]:
        """输出全部可执行动作/命令的元数据清单——AI 自动化的工具表。
        字段：name / kind / title / description / group。
        含门面注册的插件项与宿主命令系统的内置命令。"""
        out: List[dict] = []
        seen = set()
        for name, e in self._actions.items():
            seen.add(name)
            out.append({"name": name, "kind": "action",
                        "title": e.meta.get("title", name),
                        "description": e.meta.get("description", ""),
                        "group": e.meta.get("group", "")})
        for name, e in self._commands.items():
            seen.add(name)
            m = e["meta"]
            out.append({"name": name, "kind": "command",
                        "title": m.get("title", name),
                        "description": m.get("description", ""),
                        "group": m.get("group", "")})
        if self._host is not None:
            for m in self._host.commandMetaList():
                if m["name"] in seen:
                    continue
                seen.add(m["name"])
                out.append({"name": m["name"], "kind": "command",
                            "title": m.get("title", m["name"]),
                            "description": m.get("description", ""),
                            "group": m.get("group", "")})
        out.sort(key=lambda d: (d["group"], d["name"]))
        return out

    # ---------------- UI 服务（无宿主时安全降级为空操作） ----------------

    def notify(self, text: str) -> None:
        if self._host is not None:
            self._host.notify(text)

    def setStatus(self, text: str, timeoutMs: int = 4000) -> None:
        if self._host is not None:
            self._host.setStatus(text, timeoutMs)

    def appendHistory(self, text: str) -> None:
        if self._host is not None:
            self._host.appendHistory(text)

    # ---------------- 线程投递 ----------------

    def runOnMainThread(self, fn: Callable[[], Any]) -> None:
        """把调用投递回 Qt 主线程（工作线程/AI 线程改 UI 的唯一安全途径）；
        无宿主（无 Qt 事件循环）时在当前线程直接执行。"""
        if self._host is not None:
            self._host.runOnMainThread(fn)
            return
        fn()

    # ---------------- 脚本 / AI 执行入口 ----------------

    def relayDocumentEvents(self, doc: Document) -> None:
        """宿主在文档（重）绑定时调用：把文档信号接入事件总线。
        旧文档的连接按引用自动断开，避免多文件切换后事件混串。"""
        old = self._relayDoc
        slots = self._relaySlots
        if old is not None and slots is not None:
            try:
                old.objectAdded.disconnect(slots[0])
                old.objectRemoved.disconnect(slots[1])
                old.objectChanged.disconnect(slots[2])
                old.selectionChanged.disconnect(slots[3])
                old.layerChanged.disconnect(slots[4])
            except (RuntimeError, TypeError):
                pass
        self._relayDoc = doc
        if doc is None:
            self._relaySlots = None
            return
        bus = self.events
        slots = (
            lambda oid: bus.emit("document.added", oid),
            lambda oid: bus.emit("document.removed", oid),
            lambda oid: bus.emit("document.changed", oid),
            lambda: bus.emit("document.selectionChanged"),
            lambda: bus.emit("document.layerChanged"),
        )
        self._relaySlots = slots
        doc.objectAdded.connect(slots[0])
        doc.objectRemoved.connect(slots[1])
        doc.objectChanged.connect(slots[2])
        doc.selectionChanged.connect(slots[3])
        doc.layerChanged.connect(slots[4])

    def runScript(self, code: str, extraNamespace: dict | None = None) -> Any:
        """执行 Python 脚本，注入 `api`（本门面）。
        脚本可把返回值赋给变量 `result`。AI 自动化经此入口驱动全部功能。"""
        namespace: dict = {"api": self, "__name__": "__cangai_script__"}
        if extraNamespace:
            namespace.update(extraNamespace)
        exec(compile(code, "<cangai-script>", "exec"), namespace)
        return namespace.get("result")

    # ---------------- 插件设置 ----------------

    def pluginSettings(self, pluginId: str) -> PluginSettings:
        """按插件 id 取隔离的持久化设置（JSON 落盘，互不干扰）。"""
        st = self._settingsCache.get(pluginId)
        if st is None:
            base = os.environ.get("APPDATA") or os.path.expanduser("~")
            safe = "".join(ch for ch in pluginId if ch.isalnum() or ch in "-_")
            st = PluginSettings(pluginId, os.path.join(
                base, "Cangai", "plugin_settings", (safe or "plugin") + ".json"))
            self._settingsCache[pluginId] = st
        return st

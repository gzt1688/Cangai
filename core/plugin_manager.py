"""插件管理器：发现/装载/启停/卸载，错误隔离。

插件形态（二选一）：
- 目录：`<目录>/plugin.json`（清单）+ 入口脚本（默认 main.py）；
- 单文件：`<目录>/xxx.py`（id 取文件名，元数据走缺省）。

入口脚本需提供 `register(api)`；可选 `unregister(api)`（停用时回调清理）。
清单字段：id / name / version / apiVersion / description / entry。

发现目录：软件内置 `plugins/` + 用户 `%APPDATA%/Cangai/plugins/`
（同名 id 时用户插件覆盖内置）。启停状态持久化于 app_settings.plugin_state。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import traceback
from dataclasses import dataclass

import core.app_settings as app_settings
from core.api import API_VERSION, builtinPluginDir, userPluginDir


@dataclass
class PluginInfo:
    pluginId: str
    name: str
    version: str
    apiVersion: int
    description: str
    path: str            # 插件目录（单文件插件为其所在目录）
    entry: str           # 入口脚本相对路径
    enabled: bool = True
    loaded: bool = False
    status: str = "未装载"
    error: str = ""


class PluginManager:
    def __init__(self, api, extraDirs: list | None = None) -> None:
        self._api = api
        self._dirs: list = [builtinPluginDir(), userPluginDir()]
        if extraDirs:
            self._dirs.extend(extraDirs)
        self._infos: dict = {}          # id → PluginInfo
        self._modules: dict = {}        # id → 入口模块
        self._registered: dict = {}     # id → {动作/命令名集合}（卸载用）

    # ---------------- 发现 ----------------

    def discover(self) -> list:
        """扫描全部插件目录，合并出插件信息（用户插件覆盖内置同名）。"""
        for d in self._dirs:
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                path = os.path.join(d, name)
                if os.path.isdir(path):
                    manifestPath = os.path.join(path, "plugin.json")
                    if not os.path.isfile(manifestPath):
                        continue
                    try:
                        with open(manifestPath, "r", encoding="utf-8") as f:
                            m = json.load(f)
                    except Exception as e:
                        info = PluginInfo(name, name, "?", API_VERSION,
                                          f"清单解析失败: {e}", path, "main.py")
                        info.status = "出错"
                        info.error = f"plugin.json 解析失败: {e}"
                        self._infos[name] = info
                        continue
                    pid = str(m.get("id") or name)
                    info = PluginInfo(
                        pluginId=pid, name=str(m.get("name", name)),
                        version=str(m.get("version", "0.0.0")),
                        apiVersion=int(m.get("apiVersion", 1)),
                        description=str(m.get("description", "")),
                        path=path, entry=str(m.get("entry", "main.py")))
                elif name.endswith(".py") and not name.startswith("_"):
                    pid = name[:-3]
                    info = PluginInfo(pluginId=pid, name=pid, version="0.0.0",
                                      apiVersion=API_VERSION, description="",
                                      path=d, entry=name)
                else:
                    continue
                info.enabled = bool(app_settings.plugin_state.get(pid, True))
                self._infos[pid] = info   # 后扫描的目录覆盖（用户覆盖内置）
        return self.infos()

    def infos(self) -> list:
        return list(self._infos.values())

    def info(self, pluginId: str) -> PluginInfo | None:
        return self._infos.get(pluginId)

    # ---------------- 装载 ----------------

    def loadAll(self) -> None:
        """装载全部启用且兼容的插件（单个失败不影响其余）。"""
        for info in self.infos():
            if info.enabled and not info.loaded:
                self.load(info)

    def load(self, info: PluginInfo) -> bool:
        if info.loaded:
            return True
        if info.apiVersion > API_VERSION:
            info.status = "出错"
            info.error = (f"插件要求 API v{info.apiVersion}，"
                          f"当前应用为 v{API_VERSION}")
            return False
        entryPath = os.path.join(info.path, info.entry)
        if not os.path.isfile(entryPath):
            info.status = "出错"
            info.error = f"入口脚本不存在: {entryPath}"
            return False
        before = self._api.snapshotRegistrations()
        try:
            spec = importlib.util.spec_from_file_location(
                f"cangai_plugin_{info.pluginId}", entryPath)
            if spec is None or spec.loader is None:
                raise RuntimeError("无法创建导入规格")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            register = getattr(mod, "register", None)
            if not callable(register):
                raise RuntimeError("入口脚本缺少 register(api) 函数")
            self._api.beginPluginScope(info.pluginId)
            try:
                register(self._api)
            finally:
                self._api.endPluginScope()
            after = self._api.snapshotRegistrations()
            self._registered[info.pluginId] = (after[0] - before[0]) | \
                                              (after[1] - before[1])
            self._modules[info.pluginId] = mod
            info.loaded = True
            info.status = "已装载"
            info.error = ""
            return True
        except Exception as e:
            info.status = "出错"
            info.error = f"{type(e).__name__}: {e}"
            traceback.print_exc()
            return False

    def unload(self, info: PluginInfo) -> None:
        """停用装载：先回调插件 unregister(api)，再移除其注册的动作/命令/按钮。"""
        if not info.loaded:
            return
        mod = self._modules.pop(info.pluginId, None)
        if mod is not None:
            un = getattr(mod, "unregister", None)
            if callable(un):
                try:
                    un(self._api)
                except Exception:
                    traceback.print_exc()
        for name in sorted(self._registered.get(info.pluginId, ())):
            self._api.unregisterRegistration(name)
        self._registered.pop(info.pluginId, None)
        self._api.removeButtonsForPlugin(info.pluginId)
        sys.modules.pop(f"cangai_plugin_{info.pluginId}", None)
        info.loaded = False
        info.status = "已停用" if not info.enabled else "未装载"

    def reload(self, pluginId: str) -> bool:
        info = self._infos.get(pluginId)
        if info is None:
            return False
        if info.loaded:
            self.unload(info)
        return self.load(info)

    # ---------------- 启停（持久化） ----------------

    def setEnabled(self, pluginId: str, on: bool) -> None:
        """设置启用状态并持久化；已装载的插件立即卸载/装载。"""
        info = self._infos.get(pluginId)
        if info is None:
            return
        info.enabled = bool(on)
        app_settings.plugin_state[pluginId] = bool(on)
        if not on and info.loaded:
            self.unload(info)
        elif on and not info.loaded:
            self.load(info)
        app_settings.save_perf()

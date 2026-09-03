# -*- coding: utf-8 -*-
"""开放平台测试：API 门面 / 事件总线 / 插件装载与启停 / 脚本入口 / 命令清单。"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

from core.events import EventBus
from core.api import CangaiAPI
from ui.windows.mainWindow import MainWindow


def run():
    results = []

    def check(name, ok):
        results.append((name, ok))
        print(f"  {name}: {'PASS' if ok else 'FAIL'}")

    # ---- 1) 事件总线：发布/订阅/退订/异常隔离 ----
    bus = EventBus()
    got = []
    unsub = bus.subscribe("t", lambda p: got.append(p))
    bus.emit("t", 1)
    unsub()
    bus.emit("t", 2)
    check("事件订阅与退订", got == [1])

    def bad(_p):
        raise RuntimeError("boom")
    got2 = []
    bus.subscribe("t2", bad)
    bus.subscribe("t2", lambda p: got2.append(p))
    bus.emit("t2", "x")
    check("订阅者异常被隔离", got2 == ["x"])

    # 签名自适应：零参处理器（无载荷主题）与带载荷/默认参处理器混用
    got3 = []
    bus.subscribe("t3", lambda: got3.append("zero"))
    bus.subscribe("t3", lambda p: got3.append(("payload", p)))

    def handler_with_default(payload=None):
        got3.append(("default", payload))
    bus.subscribe("t3", handler_with_default)
    bus.emit("t3")
    check("零参/带载荷/默认参处理器自适应",
          got3 == ["zero", ("payload", None), ("default", None)])

    # 退订（按原处理器对象）
    bus.unsubscribe("t3", handler_with_default)
    bus.emit("t3")
    check("按对象退订后不再触发", len(got3) == 5)

    # ---- 2) API 门面（无宿主独立驱动 core 文档）----
    api = CangaiAPI()
    oid = api.addPolyline([(0, 0), (10, 0), (10, 10)], closed=True)
    check("无宿主速建折线", api.getObject(oid) is not None)
    check("对象类型查询", len(api.objects(typeName="CURVE")) == 1)
    api.beginEdit()
    cid = api.addCircle(0, 0, 5)
    api.endEdit()
    check("批量编辑入撤销", api.activeDocument().canCurveUndo())
    api.activeDocument().undo()
    check("批量编辑可撤销", api.getObject(cid) is None)

    fired = []
    api.events.subscribe("document.added", lambda p: fired.append(p))
    api.addPoint(1, 2)
    check("门面事件（无宿主直发）", len(fired) == 1)

    # ---- 3) MainWindow 宿主：插件装载 / 按钮 / 动作 / 清单 ----
    w = MainWindow()
    api, pm = w._api, w._plugins
    infos = {i.pluginId: i for i in pm.infos()}
    check("发现并装载示例插件", infos.get("example_tools") is not None
          and infos["example_tools"].loaded)
    names = [m["name"] for m in api.commandManifest()]
    check("清单含插件动作与内置命令",
          "ExampleSpiral" in names and "Line" in names and "ImageTrace" in names)
    check("插件按钮已登记", "ExampleSpiral" in w._topDock._pluginButtons)

    # ---- 4) 动作执行（经命令系统 → 可撤销） ----
    before = len(w._doc.objects())
    check("执行动作", api.runAction("ExampleSpiral") is None)
    check("动作新增几何", len(w._doc.objects()) == before + 1)
    w._doc.undo()
    check("动作可撤销", len(w._doc.objects()) == before)

    # ---- 5) 脚本入口（AI 自动化通道） ----
    result = api.runScript("result = api.addCircle(0, 0, 25)")
    check("脚本建几何并回传结果",
          result is not None and w._doc.object(result) is not None)
    count = api.runScript("result = len(api.objects())")
    check("脚本查询对象数", count == before + 1)

    # ---- 6) 停用/启用（卸载清理 + 恢复） ----
    pm.setEnabled("example_tools", False)
    names = [m["name"] for m in api.commandManifest()]
    check("停用后动作注销", "ExampleSpiral" not in names
          and "ExampleSpiral" not in w._topDock._pluginButtons)
    pm.setEnabled("example_tools", True)
    info = pm.info("example_tools")
    names = [m["name"] for m in api.commandManifest()]
    check("重新启用后恢复", info.loaded and "ExampleSpiral" in names
          and "ExampleSpiral" in w._topDock._pluginButtons)

    # ---- 7) 事件总线接入文档信号 ----
    fired = []
    api.events.subscribe("document.added", lambda p: fired.append(p))
    api.addPoint(3, 4)
    check("宿主文档事件接入总线", len(fired) == 1)

    # 还原插件启用状态，避免影响其他用例
    app_settings.plugin_state.pop("example_tools", None)

    print("=== 开放平台测试 ===")
    allOk = all(ok for _, ok in results)
    print(f"=== {'全部通过' if allOk else '存在失败'} ===")
    return allOk


if __name__ == "__main__":
    import core.app_settings as app_settings
    ok = run()
    sys.exit(0 if ok else 1)

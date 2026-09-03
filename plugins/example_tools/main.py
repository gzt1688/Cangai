"""示例插件：演示 Cangai 插件 API 的主要扩展点。

- register(api)   注册动作（自动出现在"插件示例"工具页）+ 订阅事件；
- unregister(api) 停用回调（本示例的动作/按钮由管理器自动清理）。

把本目录复制到 %APPDATA%/Cangai/plugins/ 即成为用户插件。
"""
import math

_UI_PAGE = "插件示例"


def register(api) -> None:
    # ---- 1) 瞬时动作：无画布交互的纯函数，自动进入命令系统（可撤销/重复） ----
    def draw_spiral(context) -> None:
        """画一条阿基米德螺旋线（演示速建几何 + 批量撤销）。"""
        turns = float(api.pluginSettings("example_tools").get("turns", 4.0))
        pts = []
        for i in range(int(turns * 72) + 1):
            t = i / 72.0
            r = 4.0 * t
            pts.append((r * math.cos(t * 2 * math.pi),
                        r * math.sin(t * 2 * math.pi)))
        doc = api.activeDocument()
        doc.beginEdit()
        oid = api.addPolyline(pts)
        doc.endEdit()
        api.notify(f"示例插件：已生成螺旋线（{len(pts)} 点，id {oid[:8]}…）")

    api.registerAction(
        "ExampleSpiral", draw_spiral, title="示例螺旋线",
        meta={"group": _UI_PAGE, "description": "画一条阿基米德螺旋线"})

    # ---- 2) 工具按钮：出现在顶部工具栏"插件示例"分类页 ----
    api.registerToolButton(
        key="ExampleSpiral",       # 与动作同名 → 点击执行该动作
        label="螺旋线",
        group=_UI_PAGE,
        tooltip="示例插件：画一条阿基米德螺旋线",
        pluginId="example_tools")

    # ---- 3) 事件订阅：感知选区/命令变化（保存退订闭包，停用时调用） ----
    def on_selection_changed() -> None:
        globals()["_selCount"] = len(api.selectedObjects())

    globals()["_unsubscribe_selection"] = \
        api.events.subscribe("document.selectionChanged", on_selection_changed)


def unregister(api) -> None:
    """停用回调：这里清理手动订阅的事件（动作/按钮由管理器自动移除）。"""
    un = globals().get("_unsubscribe_selection")
    if un is not None:
        un()

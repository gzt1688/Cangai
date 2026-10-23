# -*- coding: utf-8 -*-
"""饼干外切模插件（私有，不随公共版发布）。

选闭合曲线 → 调参预览 → 生成外切模实体（刀壁/加强带/裙边/顶部斜口），
用于饼干切模的 3D 打印模型。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

CMD = "cookie_cutter.create"
RELIEF_CMD = "cookie_cutter.biscuit_relief"
GROUP = "模具"

# 移植自 Rhino 插件 Model 的 4 个模具功能（月饼曲线/拉伸 + 饼干曲线/拉伸）
_MOLDS = [
    ("cookie_cutter.mooncake_curve", "MooncakeCurveCommand",
     "月饼曲线", "闭合轮廓 → 镂空框 + 内/外偏移共 3 条曲线"
                "（缝隙=内偏移距，外壁=缝隙+2.3）"),
    ("cookie_cutter.mooncake_extrude", "MooncakeExtrudeCommand",
     "月饼拉伸", "选镂空框/内偏移/外偏移 3 条曲线 → 底板(高3,镂空)"
                " + 模壁(高45)"),
    ("cookie_cutter.cookie_curve", "CookieCurveCommand",
     "饼干曲线", "闭合轮廓 → 镂空框 + 3 条偏移(缝隙/+0.6/+1.6)共 4 条曲线"),
    ("cookie_cutter.cookie_extrude", "CookieExtrudeCommand",
     "饼干拉伸", "选镂空框/内/中/外 4 条曲线 → 底板+内衬套+主体壁"
                "+顶部斜口放样"),
]


def register(api):
    from cutter_command import CookieCutterCommand
    try:
        from relief_biscuit import BiscuitReliefCommand
    except Exception as e:      # 新版文件缺失时只丢这一个功能
        print(f"[cookie_cutter] 浮雕饼干模块不可用: {e}")
        BiscuitReliefCommand = None

    api.registerCommand(
        CMD, CookieCutterCommand,
        {"title": "模具", "group": GROUP,
         "description": "选择闭合曲线生成饼干外切模实体"
                        "（刀壁/加强带/裙边/顶部斜口）"})

    # 第一个按钮改为文字"饼干外切"（与组内其他文字按钮一致，不再用图标）
    api.registerToolButton(
        key=CMD, label="饼干外切", group=GROUP,
        tooltip="闭合曲线 → 饼干外切模（调参预览后确认）",
        icon="")

    from mold_commands import (MooncakeCurveCommand, MooncakeExtrudeCommand,
                               CookieCurveCommand, CookieExtrudeCommand)
    classes = {"MooncakeCurveCommand": MooncakeCurveCommand,
               "MooncakeExtrudeCommand": MooncakeExtrudeCommand,
               "CookieCurveCommand": CookieCurveCommand,
               "CookieExtrudeCommand": CookieExtrudeCommand}
    for key, cls_name, label, tip in _MOLDS:
        api.registerCommand(key, classes[cls_name],
                            {"title": label, "group": GROUP,
                             "description": tip})
        api.registerToolButton(key=key, label=label, group=GROUP,
                               tooltip=tip, icon="")

    # 内置墙线命令挂到模具组（命令本体仍由主程序注册，
    # 插件停用只移除按钮，不影响命令本身）
    api.registerToolButton(
        key="WallLine", label="墙线", group=GROUP,
        tooltip="选中曲线 → 双侧偏移成墙厚并挤出（右键/回车确认生成）")

    # 分割线：以上为 3D 实体类模具功能，以下为浮雕模式功能。
    # 旧版宿主没有 registerToolDivider：跳过分割线即可，绝不能让整组按钮
    # 因新功能注册失败而被插件加载器整体回滚（表现为“模具按钮全消失”）。
    _addDivider = getattr(api, "registerToolDivider", None)
    if callable(_addDivider):
        _addDivider(GROUP)
    if BiscuitReliefCommand is not None:
        try:
            api.registerCommand(
                RELIEF_CMD, BiscuitReliefCommand,
                {"title": "浮雕饼干", "group": GROUP,
                 "description": "选中/点选闭合曲线 → 曲线外高度清零，曲线内 Z 镜像"
                                "后整块上抬到最低点指定高度（默认 2）"})
            api.registerToolButton(
                key=RELIEF_CMD, label="浮雕饼干", group=GROUP,
                tooltip="选闭合曲线 → 曲线外冲压成 0，曲线内 Z 镜像后最低点抬到 +2",
                icon="")
        except Exception as e:
            print(f"[cookie_cutter] 浮雕饼干注册失败（不影响其它按钮）: {e}")


def unregister(api):
    pass

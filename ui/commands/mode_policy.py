"""画布模式 × 命令 允许矩阵（单一事实源）。

四个画布模式：2D（平面绘图）/ 浮雕（曲面雕刻）/ 3D（实体建模）/ CNC（刀路）。
空间隔离由 Document.setSpace 保证（2D/浮雕共用 2d 空间，3D/CNC 各自独立）；
本模块负责**命令层**的门禁：哪些命令允许在哪个模式下启动。

设计原则：
- 3D 实体命令（挤出/布尔/螺纹/推拉等）只在 3D 模式可用——挤出结果只会
  进入 3D 空间，杜绝"2D 下挤出 → 实体落进 2D/浮雕共享空间 → 浮雕视口
  混用两条 GL 管线渲染实体+曲面而花屏"这条事故链。
- 浮雕雕刻命令只在浮雕模式可用（与快捷条 RELIEF_COMMAND_KEYS 门禁一致）。
- CNC 刀路命令只在 CNC 模式可用；「导入 CNC」是桥梁命令（把选中曲线
  克隆进 CNC 空间后自动切到 CNC 模式），任何模式都可发起。
- 未列入矩阵的命令（曲线绘制/编辑/标注/视图/插件命令）视为全模式可用。
"""
from __future__ import annotations

# 画布模式 → 显示名（提示文案用）
MODE_LABELS = {"2d": "2D", "relief": "浮雕", "3d": "3D", "cnc": "CNC"}

# 命令名 → 允许的画布模式元组
COMMAND_MODES = {
    # ---- 3D 实体命令：仅 3D 模式（build123d 内核，结果进 3D 空间） ----
    "Extrude":   ("3d",),   # 挤出
    "PushPull":  ("3d", "cnc"),   # 推拉（面挤出/挖孔/倒角；CNC 空间调整实体沿用原支持）
    "Boolean3D": ("3d",),   # 3D 布尔
    "Thread":    ("3d",),   # 螺纹
    "Chamfer3D": ("3d",),   # 3D 斜角
    "Fillet3D":  ("3d",),   # 3D 圆角
    "WallLine":  ("3d",),   # 墙线（双侧偏移挤出墙体实体）

    # ---- 模具插件（cookie_cutter）：生成 3D 打印模具实体，仅 3D 模式 ----
    "cookie_cutter.create":          ("3d",),   # 模具（外切模）
    "cookie_cutter.mooncake_curve":  ("3d",),   # 月饼曲线
    "cookie_cutter.mooncake_extrude": ("3d",),  # 月饼拉伸
    "cookie_cutter.cookie_curve":    ("3d",),   # 饼干曲线
    "cookie_cutter.cookie_extrude":  ("3d",),   # 饼干拉伸

    # ---- 浮雕命令：仅浮雕模式 ----
    "NewModel":        ("relief",),   # 新建模型
    "ImageToModel":    ("relief",),   # 图转模型
    "ReconstructRelief": ("relief",), # 重构浮雕
    "ModelScale":      ("relief",),   # 模型扩缩
    "AddMaterial":     ("relief",),   # 塑形
    "RegionRelief":    ("relief",),   # 区域塑形
    "SweepMaterial":   ("relief",),   # 路径塑形
    "Stamp":           ("relief",),   # 冲压
    "Smooth":          ("relief",),   # 磨光
    "SweepSmooth":     ("relief",),   # 路径磨光
    "DeJitter":        ("relief",),   # 消除锯齿
    "Move":            ("relief",),   # 移动（高度迁移）
    "RegionTransform": ("relief",),   # 区域变换
    "RegionArray":     ("relief",),   # 区域阵列
    "RegionSymmetry":  ("relief",),   # 区域对称
    "RegionDeform":    ("relief",),   # 区域变形
    "LevelCalibrate":  ("relief",),   # 水平校准
    "RegionSlope":     ("relief",),   # 斜面
    "RevolveSlope":    ("relief",),   # 旋转面
    "TextureFill":     ("relief",),   # 纹理填充（几何纹理族）
    "SlopeZone":       ("relief",),   # 角度分区线
    "RestBoundary":    ("relief",),   # 残料边界线
    # 浮雕动作（mainWindow 直调，不经过 executeCommand；
    # 列在此处供顶部按钮按模式置灰）
    "ReliefUndo":      ("relief",),   # 浮雕撤销
    "ReliefRedo":      ("relief",),   # 浮雕重做
    "Solidify":        ("relief",),   # 固化

    # ---- CNC 刀路命令：仅 CNC 模式（ImportToCnc 为桥梁命令不限制） ----
    "CncMachine":   ("cnc",),   # 机床设置
    "CncStock":     ("cnc",),   # 安装几何体
    "CncTools":     ("cnc",),   # 刀具表
    "CncProfile":   ("cnc",),   # 生成刀路
    "CncCollision": ("cnc",),   # 碰撞检查
    "CncSimulate":  ("cnc",),   # 实体模拟
    "CncOutput":    ("cnc",),   # 路径输出
}


def modeAllows(name: str, mode: str) -> bool:
    """命令 name 是否允许在画布模式 mode 下使用；未列入矩阵 = 全模式可用。"""
    allowed = COMMAND_MODES.get(name)
    if allowed is None:
        return True
    return mode in allowed


def refusalHint(name: str, mode: str) -> str:
    """命令被模式门禁拒绝时的提示文案。"""
    allowed = COMMAND_MODES.get(name) or ()
    where = "、".join(MODE_LABELS.get(m, m) for m in allowed) or "指定"
    cur = MODE_LABELS.get(mode, mode)
    return f"「{name}」仅在{where}模式下可用（当前：{cur}模式）"

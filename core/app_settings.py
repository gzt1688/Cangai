"""应用级通用设置（语言、单位、公差、角度精度）。"""
from __future__ import annotations

import json
import os

LANGUAGES = ["简体中文", "English"]
UNITS = ["毫米 (mm)", "厘米 (cm)", "英寸 (in)"]
PRECISIONS = ["0", "0.0", "0.00", "0.000"]
TOLERANCES = ["0.1", "0.01", "0.001", "0.0001"]
ANGLE_PRECISIONS = ["1", "0.1", "0.01", "0.001"]

language = LANGUAGES[0]
unit = UNITS[0]
tolerance = "0.01"
angle_precision = "0.1"

file_save_slots = [
    {"enabled": True, "path": "", "name": "文件1"},
    {"enabled": True, "path": "", "name": "文件2"},
    {"enabled": True, "path": "", "name": "文件3"},
    {"enabled": False, "path": "", "name": "文件4"},
    {"enabled": False, "path": "", "name": "文件5"},
]

# 键盘快捷键：(分组, 功能名, 快捷键序列, 说明)。功能名用于运行时把序列映射到动作。
# 浮雕/浮雕几何两组的默认值全部取自北京精雕（JDPaint 雕塑模块）的既有快捷键：
#   堆料 Shift+Q / 去料 Shift+W / 导动堆料 Shift+A / 导动去料 Shift+S
#   区域浮雕 Shift+D / 冲压 Shift+R / 磨光 Shift+E / 导动磨光 Shift+J
#   整体固化 Shift+G / 颜色浮雕 Shift+F / 擦除 Shift+T
# 本软件的可左右键按钮按“左键=精雕对应功能”登记；堆料/去料这类成对功能
# 拆成两行各占一键（Shift+Q / Shift+W），快捷键会以对应效果直接启动同一命令。
# 精雕本身没有默认键的功能留空，用户可自行设置。
shortcuts = [
    ["通用", "撤销", "Ctrl+Z", "撤销上一步"],
    ["通用", "重做", "Ctrl+Y", "重做"],
    ["通用", "复制", "Ctrl+C", "复制选中对象"],
    ["通用", "粘贴", "Ctrl+V", "粘贴剪贴板对象"],
    ["通用", "正交", "F8", "切换正交模式"],
    ["通用", "切换显示模式", "Tab", "2D：等高线↔灰度图；浮雕：地图↔颜色"],
    # ---- 浮雕（笔刷类，精雕 Shift 系快捷键）----
    ["浮雕", "堆料", "Shift+Q", "塑形笔刷，堆料（精雕 Shift+Q）"],
    ["浮雕", "去料", "Shift+W", "塑形笔刷，去料（精雕 Shift+W）"],
    ["浮雕", "区域塑形", "Shift+D", "区域浮雕（精雕 Shift+D）；右键挖除"],
    ["浮雕", "路径塑形", "Shift+A", "导动堆料（精雕 Shift+A）；右键导动去料（精雕 Shift+S）"],
    ["浮雕", "冲压", "Shift+R", "冲压抬高（精雕 Shift+R）；右键凹压"],
    ["浮雕", "磨光", "Shift+E", "磨光（精雕 Shift+E）；右键路径磨光（精雕 Shift+J）"],
    ["浮雕", "固化", "Shift+G", "整体固化（精雕 Shift+G）"],
    ["浮雕", "显示/隐藏曲线", "E", "浮雕模式下切换曲线图层显示"],
    ["浮雕", "新建模型", "", ""],
    ["浮雕", "图转模型", "", ""],
    ["浮雕", "模型扩缩", "", ""],
    ["浮雕", "消除锯齿", "", ""],
    ["浮雕", "移动", "", ""],
    ["浮雕", "浮雕撤销", "", "浮雕历史撤销（精雕雕塑内为 Ctrl+Z，与全局撤销冲突，未占用）"],
    ["浮雕", "浮雕重做", "", "浮雕历史重做"],
    # ---- 浮雕几何（精雕均为菜单功能，无默认快捷键）----
    ["浮雕几何", "区域变换", "", ""],
    ["浮雕几何", "区域阵列", "", ""],
    ["浮雕几何", "区域对称", "", ""],
    ["浮雕几何", "区域变形", "", ""],
    ["浮雕几何", "水平校准", "", ""],
    ["浮雕几何", "斜面", "", ""],
    ["浮雕几何", "旋转面", "", ""],
    ["浮雕几何", "纹理填充", "", ""],
    ["浮雕几何", "构造曲线", "", ""],
]

# 键盘页“功能名” → 命令名：只有登记在这里的功能，快捷键才真正触发命令。
# 未登记项（撤销/重做/复制/粘贴/正交等）由主窗口已有逻辑处理。
shortcut_commands = {
    "堆料": ("AddMaterial", "add"),
    "去料": ("AddMaterial", "remove"),
    "区域塑形": "RegionRelief",
    "路径塑形": "SweepMaterial",
    "冲压": "Stamp",
    "磨光": "Smooth",
    "固化": "Solidify",
    "显示/隐藏曲线": "ToggleCurveLayer",
    "新建模型": "NewModel",
    "图转模型": "ImageToModel",
    "模型扩缩": "ModelScale",
    "消除锯齿": "DeJitter",
    "移动": "Move",
    "浮雕撤销": "ReliefUndo",
    "浮雕重做": "ReliefRedo",
    "区域变换": "RegionTransform",
    "区域阵列": "RegionArray",
    "区域对称": "RegionSymmetry",
    "区域变形": "RegionDeform",
    "水平校准": "LevelCalibrate",
    "斜面": "RegionSlope",
    "旋转面": "RevolveSlope",
    "纹理填充": "TextureFill",
    "构造曲线": "SlopeZone",
}

# ==================== 标注样式设置（设置 → 通用 → 标注）====================
# 标注读数文字使用的字体；创建新标注时读数文字的屏幕高度（px，按创建时
# 视口缩放换算为模型字高）；读数数字后附加的单位字符串（默认 mm，可清空）。
dim_font = "Microsoft YaHei"
dim_text_px = 12.0
dim_unit = "mm"


# ==================== 外观：选中物体虚线框 ====================
# 选中物体时是否绘制贴合包围盒的虚线框（设置 → 外观样式 → 选中物体虚线框）。
# 默认关闭：选中高亮已由图元加粗描边体现，虚线框仅在需要明确包围盒时开启。
show_selection_box = False


# ==================== 鼠标滚轮反向缩放 ====================
# 开启后滚轮缩放方向反转（2D 画布与 3D/浮雕视口同时生效）：默认滚轮向前=放大，
# 反向=向前缩小，适配习惯反向滚轮的用户（设置 → 外观样式）。
invert_wheel_zoom = False


# ==================== 渲染 / 性能设置 ====================
# 默认值按“入门级独显或核显 + 8GB 内存”给出，保证流畅不闪退（合理最小值）；
# 配置更好的机器可在“设置 → 渲染性能”页上调，获得更精细的曲面、更深的撤销历史。
# 这些值被 core.relief / 3D 视口 / 2D 预览 / 文档撤销实时读取（改后下次绘制即生效，
# 标注“重启生效”的项除外）。

# ---- 内存 ----
# 单个浮雕母场允许的每边像素上限（超出自动加粗精度）。默认 4096 → 4096²≈64MB。
# 支持到约 400mm@0.1mm；高配可上调到 8192（≈256MB/场）。
max_field_pixels_side = 4096
MAX_FIELD_PIXELS_SIDE_RANGE = (512, 8192)

# 撤销历史占用的内存预算（MB），超出按最旧淘汰。默认 256。
undo_memory_budget_mb = 256
UNDO_MEMORY_BUDGET_RANGE = (32, 4096)

# ---- 显卡 / 渲染 ----
# 是否启用 GPU(ModernGL) 加速浮雕曲面渲染；关闭则走 CPU 软件光栅化兜底。
gpu_accelerated = True

# GPU 路径 3D 曲面每边最大网格数（面数≈N²，单次 draw call）。默认 1024：
# 显示网格按整数步长抽稀，1024 对 1000~2000px 的图正好是 1~2 倍抽稀，
# 大图不再糊成格子；旧默认 256 会抽稀 4~5 倍，浮雕明显"像素化"。
mesh_max_dim_gpu = 1024
# 上限 2048：面数≈4.2M、顶点缓冲约 150MB、索引约 100MB，配 8G 以上显卡可开；
# 再往上（4096）单次重建的 numpy 中转数组会到 GB 级，32G 内存机器容易吃紧。
MESH_MAX_DIM_GPU_RANGE = (32, 2048)

# 软件兜底路径 3D 曲面每边最大网格数（逐三角形 Python 循环，成本高）。默认 100。
mesh_max_dim_cpu = 100
MESH_MAX_DIM_CPU_RANGE = (16, 400)

# 2D 着色预览的降采样长边上限。默认 1024；高配可上调到 2048/4096。
preview_max_dim = 1024
PREVIEW_MAX_DIM_RANGE = (128, 4096)

# MSAA 抗锯齿采样数（0=关闭）。改后需重启生效（GL 上下文在启动时创建）。默认 4。
msaa_samples = 4
MSAA_CHOICES = (0, 2, 4, 8)

# 区域浮雕/重构等重计算是否放到后台线程（关闭则在主线程同步计算，会卡）。默认开。
async_compute = True


# ==================== 文件保存设置（自动保存 / 备份） ====================
# 自动保存：开启后按间隔把"有文件路径且未保存"的文档另存为 <路径>.autosave 恢复
# 副本（不改动未保存标记）；备份：保存覆盖已有文件前先复制 <路径>.bak。
autosave_enabled = False
autosave_interval_min = 5
backup_on_save = True

# ==================== 插件启停状态 ====================
# 插件 id → 是否启用（缺省视为启用，用户禁用后记 False，重启保持）。
plugin_state: dict = {}


def max_field_pixels() -> int:
    """当前母场像素上限（每边上限²），供 HeightField.grid_size 钳制用。"""
    side = max(64, int(max_field_pixels_side))
    return side * side


def undo_memory_budget_bytes() -> int:
    """撤销内存预算（字节）。"""
    return int(max(1, undo_memory_budget_mb)) * 1024 * 1024


def perf_snapshot() -> dict:
    """渲染/性能与文件设置快照（供设置对话框取消时回滚）。"""
    return {
        "settings_version": _SETTINGS_VERSION,
        "language": language,
        "max_field_pixels_side": max_field_pixels_side,
        "undo_memory_budget_mb": undo_memory_budget_mb,
        "gpu_accelerated": gpu_accelerated,
        "mesh_max_dim_gpu": mesh_max_dim_gpu,
        "mesh_max_dim_cpu": mesh_max_dim_cpu,
        "preview_max_dim": preview_max_dim,
        "msaa_samples": msaa_samples,
        "async_compute": async_compute,
        "autosave_enabled": autosave_enabled,
        "autosave_interval_min": autosave_interval_min,
        "backup_on_save": backup_on_save,
        "invert_wheel_zoom": invert_wheel_zoom,
        "plugin_state": dict(plugin_state),
    }


def perf_restore(snap: dict) -> None:
    """从快照回滚渲染/性能与文件设置。"""
    global max_field_pixels_side, undo_memory_budget_mb, gpu_accelerated
    global mesh_max_dim_gpu, mesh_max_dim_cpu, preview_max_dim
    global msaa_samples, async_compute
    global autosave_enabled, autosave_interval_min, backup_on_save
    global language, invert_wheel_zoom
    language = snap.get("language", language)
    max_field_pixels_side = snap.get("max_field_pixels_side", max_field_pixels_side)
    undo_memory_budget_mb = snap.get("undo_memory_budget_mb", undo_memory_budget_mb)
    gpu_accelerated = snap.get("gpu_accelerated", gpu_accelerated)
    mesh_max_dim_gpu = snap.get("mesh_max_dim_gpu", mesh_max_dim_gpu)
    mesh_max_dim_cpu = snap.get("mesh_max_dim_cpu", mesh_max_dim_cpu)
    preview_max_dim = snap.get("preview_max_dim", preview_max_dim)
    msaa_samples = snap.get("msaa_samples", msaa_samples)
    async_compute = snap.get("async_compute", async_compute)
    autosave_enabled = bool(snap.get("autosave_enabled", autosave_enabled))
    autosave_interval_min = int(snap.get("autosave_interval_min", autosave_interval_min))
    backup_on_save = bool(snap.get("backup_on_save", backup_on_save))
    invert_wheel_zoom = bool(snap.get("invert_wheel_zoom", invert_wheel_zoom))
    ps = snap.get("plugin_state")
    if isinstance(ps, dict):
        plugin_state.clear()
        plugin_state.update(ps)


def system_info() -> dict:
    """探测本机 CPU 核数与物理内存（纯标准库，只读展示用）。"""
    import os
    info = {"cpu_cores": os.cpu_count() or 0, "total_ram_mb": 0}
    try:
        if os.name == "nt":
            import ctypes

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = _MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                info["total_ram_mb"] = int(stat.ullTotalPhys // (1024 * 1024))
    except Exception:
        pass
    return info


# ==================== 渲染性能/文件设置持久化 ====================
# 持久化渲染性能与文件保存项（上调后/开启后重启不丢）；语言/主题等沿用现有内存态约定。
# 文件损坏/缺失时自愈为默认值，不影响启动。
_SETTINGS_VERSION = 2      # 设置文件版本（用于一次性默认值迁移，见 load_perf）

_PERF_KEYS = (
    "language",
    "max_field_pixels_side", "undo_memory_budget_mb", "gpu_accelerated",
    "mesh_max_dim_gpu", "mesh_max_dim_cpu", "preview_max_dim",
    "msaa_samples", "async_compute",
    "autosave_enabled", "autosave_interval_min", "backup_on_save",
    "invert_wheel_zoom",
    "plugin_state",
)


def perf_config_path() -> str:
    """设置 JSON 路径：%APPDATA%/Cangai/render_settings.json（非 Windows 用 ~）。"""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "Cangai")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(d, "render_settings.json")


def load_perf(path: str | None = None) -> None:
    """从磁盘加载设置（启动时调用）；缺失/损坏则保持默认。"""
    p = path or perf_config_path()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    snap = {k: data[k] for k in _PERF_KEYS if k in data}
    # v1 → v2 迁移：旧版默认网格上限 256，对大图抽稀 4~5 倍，浮雕明显像素化。
    # 只迁移"仍是旧默认值"的设置，用户自己改过的数值保持不变。
    try:
        ver = int(data.get("settings_version", 1) or 1)
    except Exception:
        ver = 1
    if ver < _SETTINGS_VERSION:
        try:
            if int(snap.get("mesh_max_dim_gpu", 0) or 0) == 256:
                snap["mesh_max_dim_gpu"] = mesh_max_dim_gpu
        except Exception:
            pass
    perf_restore(snap)


def save_perf(path: str | None = None) -> bool:
    """将当前渲染设置写盘（设置对话框应用/确认时调用）。"""
    p = path or perf_config_path()
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(perf_snapshot(), f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


# 启动时加载已保存的渲染设置（覆盖默认值）。
load_perf()

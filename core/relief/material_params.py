"""浮雕渲染材质参数：Blender「原理化 BSDF」风格的专业参数定义与常用材质预设。

单一数据源：`PARAM_SECTIONS` 描述分组与每个参数（供 UI 生成控件、供渲染器取用），
`PRESET_PARAMS` 给出 5 个内置材质的物理正确默认值。材质参数以扁平 dict 存储、
随浮雕模型序列化；键名即渲染器消费名。

颜色一律用 0..1 的 [r, g, b] 三元 list（JSON 友好）。
"""
from __future__ import annotations

# 参数种类
FLOAT = "float"
COLOR = "color"
BOOL = "bool"        # 勾选框（存为 0.0/1.0 float，JSON 友好）

# 分组：(组键, 组标题, [ (键, 标签, 种类, 最小, 最大, 步长/默认标记) , ... ])
#   浮点：min/max 用于滑块/SpinBox；颜色无 min/max（用 None）。
PARAM_SECTIONS: list[tuple[str, str, list[tuple]]] = [
    ("surface", "表面 Surface", [
        ("base_color",   "基础色",   COLOR, None, None, None),
        ("metallic",     "金属度",   FLOAT, 0.0, 1.0, None),
        ("roughness",    "粗糙度",   FLOAT, 0.02, 1.0, None),
        ("ior",          "折射率",   FLOAT, 1.0, 2.5, None),
        ("alpha",        "透明度",   FLOAT, 0.0, 1.0, None),
    ]),
    ("subsurface", "次表面散射 Subsurface", [
        ("sss_weight",   "权重",     FLOAT, 0.0, 1.0, None),
        ("sss_radius",   "半径 mm",  FLOAT, 0.0, 50.0, None),
    ]),
    ("specular", "高光 Specular", [
        ("spec_level",   "级别",     FLOAT, 0.0, 1.0, None),
        ("spec_tint",    "着色",     COLOR, None, None, None),
        ("anisotropic",  "各向异性", FLOAT, 0.0, 1.0, None),
    ]),
    ("transmission", "透射 Transmission", [
        ("trans_weight", "权重",     FLOAT, 0.0, 1.0, None),
        ("trans_rough",  "粗糙度",   FLOAT, 0.0, 1.0, None),
    ]),
    ("emission", "自发光 Emission", [
        ("emis_color",   "颜色",     COLOR, None, None, None),
        ("emis_strength", "强度",    FLOAT, 0.0, 5.0, None),
    ]),
    ("normal", "法向 Normal", [
        ("normal_strength", "强度",  FLOAT, 0.0, 1.0, None),
    ]),
    ("texture", "表面纹理 Texture", [
        ("tex_on",      "启用纹理",   BOOL, None, None, None),
        ("tex_scale",   "纹理尺度 mm", FLOAT, 0.5, 40.0, None),
        ("tex_amount",  "纹理强度",   FLOAT, 0.0, 1.0, None),
    ]),
]

# 每种材质的程序化表面纹理编号（与着色器 matHeight(id, uv) 分支一致）
TEXTURE_ID = {"clay": 0, "wood": 1, "plastic": 2, "stone": 3, "metal": 4}

# 展平的全部键（顺序稳定，供归一化/序列化）
ALL_KEYS = [k for _s, _l, rows in PARAM_SECTIONS for (k, *_r) in rows]


def _P(base, metallic, rough, ior, alpha, sssw, sssr, specl, aniso,
      transw, transr, emis, emisl, norm, tex_scale, tex_amount, tex_on=0.0):
    """构造一个材质参数字典（位置参数与上面顺序一致，便于写预设）。"""
    return {
        "base_color": list(base), "metallic": metallic, "roughness": rough,
        "ior": ior, "alpha": alpha,
        "sss_weight": sssw, "sss_radius": sssr,
        "spec_level": specl, "spec_tint": [1.0, 1.0, 1.0], "anisotropic": aniso,
        "trans_weight": transw, "trans_rough": transr,
        "emis_color": list(emis), "emis_strength": emisl,
        "normal_strength": norm,
        "tex_scale": tex_scale, "tex_amount": tex_amount, "tex_on": tex_on,
    }


# 5 个内置材质：(id, 中文标签, 按钮色块 0-255)。与 core.MATERIALS 同序。
MATERIAL_PRESETS: list[tuple[str, str, tuple]] = [
    ("clay",    "泥土", (198, 187, 172)),
    ("wood",    "木头", (150, 100, 58)),
    ("plastic", "塑料", (205, 205, 214)),
    ("stone",   "石头", (150, 148, 142)),
    ("metal",   "金属", (176, 182, 190)),
]

# 各材质的物理近似默认参数（参照 Blender Principled BSDF 常用配方）
PRESET_PARAMS: dict[str, dict] = {
    # 泥土/陶土：暖褐哑光、极低高光（介质不反光）、强次表面（软泥透光感）；斑驳表面纹理
    "clay": _P((0.45, 0.30, 0.20), 0.0, 0.92, 1.5, 1.0,
               0.18, 8.0, 0.18, 0.0, 0.0, 0.0, (0, 0, 0), 0.0, 0.0, 6.0, 0.55),
    # 木头：棕褐、年轮纹理 + 沟槽凹凸；哑光原木（高粗糙、极低高光→不反光）
    "wood": _P((0.34, 0.20, 0.10), 0.0, 0.85, 1.5, 1.0,
               0.03, 3.0, 0.10, 0.0, 0.0, 0.0, (0, 0, 0), 0.0, 0.0, 8.0, 0.70),
    # 塑料：亮泽、锐高光、橘皮微凹凸
    "plastic": _P((0.80, 0.80, 0.86), 0.0, 0.20, 1.45, 1.0,
                  0.0, 0.0, 0.55, 0.0, 0.05, 0.10, (0, 0, 0), 0.0, 0.0, 3.0, 0.18),
    # 石头：中性灰、粗糙颗粒纹理；高粗糙哑光（不反光）
    "stone": _P((0.44, 0.44, 0.43), 0.0, 0.95, 1.5, 1.0,
                0.02, 2.0, 0.15, 0.0, 0.0, 0.0, (0, 0, 0), 0.0, 0.0, 10.0, 0.75),
    # 金属：全金属反色、锐高光、拉丝纹理（各向异性亮条）
    "metal": _P((0.83, 0.86, 0.90), 1.0, 0.28, 2.0, 1.0,
                0.0, 0.0, 1.0, 0.0, 0.0, 0.0, (0, 0, 0), 0.0, 0.0, 2.5, 0.45),
}


def default_params(preset: str) -> dict:
    """返回某材质预设的参数副本（未知预设回落 clay）。"""
    src = PRESET_PARAMS.get(preset) or PRESET_PARAMS["clay"]
    return _normalize(src)


def normalize_params(d: dict | None) -> dict:
    """把任意（可能缺键/旧版）参数字典补全为完整结构（保留已有值）。"""
    return _normalize(d or {})


def _normalize(d: dict) -> dict:
    out: dict = {}
    base = PRESET_PARAMS["clay"]
    for k in ALL_KEYS:
        if k in d and d[k] is not None:
            out[k] = _copy_val(d[k])
        else:
            out[k] = _copy_val(base[k])
    return out


def _copy_val(v):
    return list(v) if isinstance(v, (list, tuple)) else v

"""SVG 解析与路径数据生成（无第三方依赖，xml.etree + 正则 tokenizer）。

供两处使用：
  1. SVG 文件导入（core/export/svg_importer.py）：解析 <path>/<line>/<rect>/
     <circle>/<ellipse>/<polyline>/<polygon>，应用 g 嵌套 transform，
     输出根用户坐标系（SVG 原始方向，y 向下）的采样折线与颜色；
  2. 图片转线稿 VTracer 引擎（image_vectorizer.py）：解析其输出的
     `<svg><path d="M..c..Z"/></svg>` 抽取轮廓。

所有曲线（三次/二次贝塞尔、椭圆弧、圆角矩形）统一自适应采样为折线，
闭合路径首尾点重复。长度/颜色解析只覆盖工程图常见形式（mm/cm/in/pt、
#hex、rgb()、常用命名色），未识别的返回 None 由调用方决定默认值。
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET

_TOK = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d*\.\d+|\d+\.?)"
                  r"(?:[eE][-+]?\d+)?")
_NUM = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
_CUBIC_SAMPLES = 16     # 每段三次/二次贝塞尔固定采样数
_ARC_DEG_STEP = 6.0     # 椭圆弧按角度采样步长

_NAMED_COLORS = {
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0),
    "green": (0, 128, 0), "lime": (0, 255, 0), "blue": (0, 0, 255),
    "yellow": (255, 255, 0), "cyan": (0, 255, 255), "magenta": (255, 0, 255),
    "gray": (128, 128, 128), "grey": (128, 128, 128), "orange": (255, 165, 0),
    "purple": (128, 0, 128), "brown": (165, 42, 42),
    "navy": (0, 0, 128), "silver": (192, 192, 192),
}


# ==================== 路径数据解析 ====================

def _arcSample(x0, y0, rx, ry, phiDeg, largeArc, sweep, x1, y1) -> list:
    """SVG 椭圆弧端点参数化（W3C 规范 F.6.5）→ 采样点列（不含起点）。"""
    if rx <= 0 or ry <= 0:
        return [(x1, y1)]
    rx, ry = abs(rx), abs(ry)
    phi = math.radians(phiDeg)
    cosp, sinp = math.cos(phi), math.sin(phi)
    dx2, dy2 = (x0 - x1) / 2.0, (y0 - y1) / 2.0
    x1p = cosp * dx2 + sinp * dy2
    y1p = -sinp * dx2 + cosp * dy2
    lam = x1p * x1p / (rx * rx) + y1p * y1p / (ry * ry)
    if lam > 1.0:
        s = math.sqrt(lam)
        rx *= s
        ry *= s
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    if den <= 1e-18:
        return [(x1, y1)]
    co = math.sqrt(max(0.0, num / den))
    if largeArc == sweep:
        co = -co
    cxp = co * rx * y1p / ry
    cyp = -co * ry * x1p / rx
    cx = cosp * cxp - sinp * cyp + (x0 + x1) / 2.0
    cy = sinp * cxp + cosp * cyp + (y0 + y1) / 2.0

    def ang(ux, uy, vx, vy) -> float:
        u = math.hypot(ux, uy)
        v = math.hypot(vx, vy)
        if u < 1e-18 or v < 1e-18:
            return 0.0
        c = max(-1.0, min(1.0, (ux * vx + uy * vy) / (u * v)))
        a = math.acos(c)
        if ux * vy - uy * vx < 0:
            a = -a
        return a

    th1 = ang(1.0, 0.0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    # W3C F.6.5：第二向量为 (-(x1p+cxp)/rx, -(y1p+cyp)/ry)——此前 y 分量
    # 误写成 -(y1p-cyp)，与 th1/采样点不一致导致弧段走错半圆
    dth = ang((x1p - cxp) / rx, (y1p - cyp) / ry,
              (-(x1p + cxp)) / rx, (-(y1p + cyp)) / ry)
    if not sweep and dth > 0:
        dth -= 2.0 * math.pi
    elif sweep and dth < 0:
        dth += 2.0 * math.pi
    n = max(2, int(math.ceil(abs(math.degrees(dth)) / _ARC_DEG_STEP)))
    pts = []
    for i in range(1, n + 1):
        th = th1 + dth * i / n
        # W3C 逆变换用 +rx·cos/+ry·sin（此前取负=对径映射 θ→θ+π）
        px, py = rx * math.cos(th), ry * math.sin(th)
        pts.append((cx + cosp * px - sinp * py,
                    cy + sinp * px + cosp * py))
    return pts


def _cubicAt(p0, p1, p2, p3, t) -> tuple:
    mt = 1.0 - t
    a = mt * mt * mt
    b = 3.0 * t * mt * mt
    c = 3.0 * t * t * mt
    d = t * t * t
    return (a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
            a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1])


def _quadAt(p0, p1, p2, t) -> tuple:
    mt = 1.0 - t
    return (mt * mt * p0[0] + 2.0 * t * mt * p1[0] + t * t * p2[0],
            mt * mt * p0[1] + 2.0 * t * mt * p1[1] + t * t * p2[1])


def parsePathData(d: str) -> list:
    """解析 path d 属性 → 子路径列表 [{"points": [(x,y),...], "closed": bool}]。

    支持 M/L/H/V/C/S/Q/T/A/Z 全命令（绝对/相对）；曲线按固定采样数折线化。
    解析失败的部分忽略，返回已成功解析的子路径。
    """
    tokens = _TOK.findall(d or "")
    if not tokens:
        return []
    subs: list = []
    pts: list = []
    closed = False
    cx = cy = 0.0            # 当前点
    sx = sy = 0.0            # 子路径起点
    lx = ly = 0.0            # 上段最后控制点（S/T 平滑用）
    lastCmd = ""
    i = 0
    n = len(tokens)

    def flush() -> None:
        nonlocal pts, closed
        if len(pts) >= 2:
            subs.append({"points": pts, "closed": closed})
        pts = []
        closed = False

    def num() -> float:
        nonlocal i
        v = float(tokens[i])
        i += 1
        return v

    while i < n:
        t = tokens[i]
        if _TOK.match(t) and t.isalpha():
            cmd = t
            i += 1
        else:
            cmd = lastCmd or "M"   # 后续数字沿用上一命令
        rel = cmd.islower()
        C = cmd.upper()
        if C == "M":
            flush()
            x, y = num(), num()
            if rel:
                x += cx
                y += cy
            cx, cy = x, y
            sx, sy = x, y
            pts.append((x, y))
            lastCmd = "l" if rel else "L"   # M 后续坐标对隐式变 L
            continue
        if C == "L":
            x, y = num(), num()
            if rel:
                x += cx
                y += cy
            cx, cy = x, y
            pts.append((x, y))
        elif C == "H":
            x = num()
            if rel:
                x += cx
            cx = x
            pts.append((cx, cy))
        elif C == "V":
            y = num()
            if rel:
                y += cy
            cy = y
            pts.append((cx, cy))
        elif C == "C":
            x1, y1, x2, y2, x, y = (num(), num(), num(), num(), num(), num())
            if rel:
                x1 += cx
                y1 += cy
                x2 += cx
                y2 += cy
                x += cx
                y += cy
            for k in range(1, _CUBIC_SAMPLES + 1):
                pts.append(_cubicAt((cx, cy), (x1, y1), (x2, y2), (x, y),
                                    k / _CUBIC_SAMPLES))
            lx, ly = x2, y2
            cx, cy = x, y
        elif C == "S":
            x2, y2, x, y = num(), num(), num(), num()
            if rel:
                x2 += cx
                y2 += cy
                x += cx
                y += cy
            if lastCmd.upper() in ("C", "S"):
                x1, y1 = 2.0 * cx - lx, 2.0 * cy - ly
            else:
                x1, y1 = cx, cy
            for k in range(1, _CUBIC_SAMPLES + 1):
                pts.append(_cubicAt((cx, cy), (x1, y1), (x2, y2), (x, y),
                                    k / _CUBIC_SAMPLES))
            lx, ly = x2, y2
            cx, cy = x, y
        elif C == "Q":
            x1, y1, x, y = num(), num(), num(), num()
            if rel:
                x1 += cx
                y1 += cy
                x += cx
                y += cy
            for k in range(1, _CUBIC_SAMPLES + 1):
                pts.append(_quadAt((cx, cy), (x1, y1), (x, y),
                                   k / _CUBIC_SAMPLES))
            lx, ly = x1, y1
            cx, cy = x, y
        elif C == "T":
            x, y = num(), num()
            if rel:
                x += cx
                y += cy
            if lastCmd.upper() in ("Q", "T"):
                x1, y1 = 2.0 * cx - lx, 2.0 * cy - ly
            else:
                x1, y1 = cx, cy
            for k in range(1, _CUBIC_SAMPLES + 1):
                pts.append(_quadAt((cx, cy), (x1, y1), (x, y),
                                   k / _CUBIC_SAMPLES))
            lx, ly = x1, y1
            cx, cy = x, y
        elif C == "A":
            rx, ry, rot, la, sw, x, y = (num(), num(), num(), num(),
                                         num(), num(), num())
            if rel:
                x += cx
                y += cy
            for q in _arcSample(cx, cy, rx, ry, rot, bool(la), bool(sw), x, y):
                pts.append(q)
            cx, cy = x, y
        elif C == "Z":
            if pts:
                cx, cy = sx, sy
                pts.append((sx, sy))
            closed = True
            flush()
            lastCmd = ""
            continue
        lastCmd = cmd
    flush()
    return subs


# ==================== 样式与变换 ====================

def _parseColor(v: str):
    """颜色字面量 → (r,g,b)；none/未识别 → None。"""
    if not v:
        return None
    v = v.strip().lower()
    if v in ("none", "transparent", "inherit", "currentcolor"):
        return None
    m = re.match(r"^#([0-9a-f]{6})$", v)
    if m:
        h = m.group(1)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    m = re.match(r"^#([0-9a-f]{3})$", v)
    if m:
        h = m.group(1)
        return (int(h[0] * 2, 16), int(h[1] * 2, 16), int(h[2] * 2, 16))
    m = re.match(r"^rgba?\(([^)]+)\)$", v)
    if m:
        parts = [p.strip() for p in m.group(1).split(",")]
        try:
            vals = [float(p[:-1]) * 2.55 if p.endswith("%") else float(p)
                    for p in parts[:3]]   # 逐分量判断 % 后缀（alpha 不能代表 RGB）
            return tuple(min(255, max(0, int(round(c)))) for c in vals)
        except (ValueError, IndexError):
            return None
    return _NAMED_COLORS.get(v)


def _styleDict(el) -> dict:
    """元素 style="" 内联样式 → dict（CSS 声明优先于同名属性由调用方合并）。"""
    out = {}
    s = el.get("style")
    if s:
        for decl in s.split(";"):
            if ":" in decl:
                k, v = decl.split(":", 1)
                out[k.strip().lower()] = v.strip()
    return out


def parseTransform(s: str):
    """transform 属性 → 矩阵 (a,b,c,d,e,f)；空/无法解析 → 单位矩阵。"""
    m = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    if not s:
        return m
    for name, argstr in re.findall(r"(\w+)\s*\(([^)]*)\)", s):
        args = [float(v) for v in _NUM.findall(argstr)]
        t = None
        if name == "matrix" and len(args) >= 6:
            t = args[:6]
        elif name == "translate" and args:
            t = [1.0, 0.0, 0.0, 1.0, args[0],
                 args[1] if len(args) > 1 else 0.0]
        elif name == "scale" and args:
            sy = args[1] if len(args) > 1 else args[0]
            t = [args[0], 0.0, 0.0, sy, 0.0, 0.0]
        elif name == "rotate" and args:
            a = math.radians(args[0])
            ca, sa = math.cos(a), math.sin(a)
            t = [ca, sa, -sa, ca, 0.0, 0.0]
            if len(args) >= 3:
                cx, cy = args[1], args[2]
                t = [t[0], t[1], t[2], t[3],
                     cx - ca * cx + sa * cy, cy - sa * cx - ca * cy]
        elif name == "skewx" and args:
            t = [1.0, 0.0, math.tan(math.radians(args[0])), 1.0, 0.0, 0.0]
        elif name == "skewy" and args:
            t = [1.0, math.tan(math.radians(args[0])), 0.0, 1.0, 0.0, 0.0]
        if t:
            m = [m[0] * t[0] + m[2] * t[1],
                 m[1] * t[0] + m[3] * t[1],
                 m[0] * t[2] + m[2] * t[3],
                 m[1] * t[2] + m[3] * t[3],
                 m[0] * t[4] + m[2] * t[5] + m[4],
                 m[1] * t[4] + m[3] * t[5] + m[5]]
    return m


def _apply(m, x, y) -> tuple:
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


# ==================== 文档解析 ====================

_LEN_MM = {"mm": 1.0, "cm": 10.0, "in": 25.4, "pt": 25.4 / 72.0,
           "pc": 25.4 / 6.0, "px": 1.0, "": 1.0}


def _parseLength(v: str):
    """长度字面量 → (数值, 是否物理单位)；百分比/无法解析 → None。"""
    if not v:
        return None
    v = v.strip().lower()
    if v.endswith("%"):
        return None
    m = re.match(r"^([-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?)\s*"
                 r"(mm|cm|in|pt|pc|px)?$", v)
    if not m:
        return None
    return float(m.group(1)), (m.group(2) or "") in ("mm", "cm", "in", "pt", "pc")


class SvgInfo:
    """解析结果：视口尺寸（用户单位）+ 根用户坐标系的路径列表。"""

    def __init__(self) -> None:
        self.width: float = 0.0        # 视口宽（用户单位）
        self.height: float = 0.0
        self.physical: bool = False    # width/height 是否带物理单位
        self.physicalMm: tuple | None = None   # (宽mm, 高mm) 仅物理单位时
        self.paths: list = []          # [{"subpaths": [...], "color": (r,g,b)|None}]
        self.skipped: int = 0          # 跳过的图形元素数（use/text/image…）


def parseSvg(text: str) -> SvgInfo:
    """解析 SVG 文本；根用户坐标系（全部 transform 已应用，y 向下）。"""
    info = SvgInfo()
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        # 尝试去掉 XML 声明/DOCTYPE 后重试
        cleaned = re.sub(r"<\?[^>]*\?>|<!DOCTYPE[^>]*>", "", text,
                         count=1, flags=re.S)
        try:
            root = ET.fromstring(cleaned)
        except ET.ParseError:
            return info

    def local(tag) -> str:
        return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""

    if local(root.tag) != "svg":
        return info

    wAttr = root.get("width", "")
    hAttr = root.get("height", "")
    vb = root.get("viewBox", "")
    vbVals = [float(v) for v in _NUM.findall(vb)] if vb else []
    wLen = _parseLength(wAttr)
    hLen = _parseLength(hAttr)
    if wLen and hLen:
        info.width, info.height = wLen[0], hLen[0]
        info.physical = wLen[1] or hLen[1]
    elif len(vbVals) == 4:
        info.width, info.height = vbVals[2], vbVals[3]
    elif wLen:
        info.width = wLen[0]
    elif hLen:
        info.height = hLen[0]
    # 物理尺寸单独保存：viewBox 存在时用户单位数以 viewBox 为准，
    # mm 比例 = 物理尺寸 / viewBox 对应边
    phys = [v if isPhys else None for v, isPhys in
            (wLen or (None, False), hLen or (None, False))]
    if phys[0] is not None or phys[1] is not None:
        if len(vbVals) == 4:
            uw, uh = vbVals[2], vbVals[3]
        else:
            uw, uh = info.width, info.height
        mmPerUser = None
        if phys[0] is not None and uw > 1e-9:
            mmPerUser = phys[0] / uw
        elif phys[1] is not None and uh > 1e-9:
            mmPerUser = phys[1] / uh
        if mmPerUser is not None:
            info.physical = True
            info.physicalMm = (uw * mmPerUser, uh * mmPerUser)
    # 仅有单边尺寸且无 viewBox 时另一边保持 0，由调用方决定默认值
    if len(vbVals) == 4 and info.width <= 0:
        info.width = vbVals[2]
    if len(vbVals) == 4 and info.height <= 0:
        info.height = vbVals[3]

    paths = []
    skipped = [0]

    def walk(el, m, inherited) -> None:
        t = local(el.tag)
        if t == "defs" or t == "style" or t == "metadata":
            return
        a, b, c, d, e, f = parseTransform(el.get("transform", ""))
        m = [m[0] * a + m[2] * b, m[1] * a + m[3] * b,
             m[0] * c + m[2] * d, m[1] * c + m[3] * d,
             m[0] * e + m[2] * f + m[4], m[1] * e + m[3] * f + m[5]]
        style = _styleDict(el)
        # CSS 优先级：继承 < 自身 presentation 属性 < style 内联声明。
        # style 已含内联声明（最高优先），后两者只填空缺键
        for k in ("stroke", "fill"):
            v = inherited.get(k)
            if v is not None and k not in style and k not in el.attrib:
                style[k] = v
            v = el.attrib.get(k)
            if v is not None and k not in style:
                style[k] = v
        stroke = _parseColor(style.get("stroke", ""))
        fill = _parseColor(style.get("fill", ""))
        # 线稿取色优先描边色（描边即轮廓线）；无描边用填充色
        color = stroke if stroke is not None else fill

        def emit(subpaths, kind: str = "polyline", geo=None) -> None:
            if not subpaths and geo is None:
                return
            mapped = []
            for sp in subpaths:
                mapped.append({
                    "points": [_apply(m, x, y) for x, y in sp["points"]],
                    "closed": sp["closed"],
                })
            paths.append({"subpaths": mapped, "color": color,
                          "kind": kind, "geo": geo, "matrix": list(m)})

        if t == "path":
            # 畸形 d（缺参数/A 参数不足）按 docstring 忽略该元素，不让
            # ValueError/IndexError 穿透到导入命令崩溃
            try:
                emit(parsePathData(el.get("d", "")))
            except (ValueError, IndexError):
                skipped[0] += 1
        elif t == "line":
            try:
                x1, y1 = float(el.get("x1", "0")), float(el.get("y1", "0"))
                x2, y2 = float(el.get("x2", "0")), float(el.get("y2", "0"))
            except ValueError:
                skipped[0] += 1
            else:
                emit([{"points": [(x1, y1), (x2, y2)], "closed": False}])
        elif t == "rect":
            try:
                x, y = float(el.get("x", "0")), float(el.get("y", "0"))
                w, h = float(el.get("width", "0")), float(el.get("height", "0"))
            except ValueError:
                skipped[0] += 1
            else:
                if w > 0 and h > 0:
                    emit([{"points": [(x, y), (x + w, y), (x + w, y + h),
                                      (x, y + h), (x, y)], "closed": True}])
        elif t in ("circle", "ellipse"):
            try:
                cx, cy = float(el.get("cx", "0")), float(el.get("cy", "0"))
                rx = float(el.get("r", "0")) if t == "circle" \
                    else float(el.get("rx", "0"))
                ry = float(el.get("r", "0")) if t == "circle" \
                    else float(el.get("ry", "0"))
            except ValueError:
                skipped[0] += 1
            else:
                if rx > 0 and ry > 0:
                    n = max(16, int(math.ceil(360.0 / _ARC_DEG_STEP)))
                    pts = [(cx + rx * math.cos(2 * math.pi * i / n),
                            cy + ry * math.sin(2 * math.pi * i / n))
                           for i in range(n + 1)]
                    emit([{"points": pts, "closed": True}],
                         kind="ellipse", geo=(cx, cy, rx, ry))
        elif t in ("polyline", "polygon"):
            vals = [float(v) for v in _NUM.findall(el.get("points", ""))]
            pts = [(vals[i], vals[i + 1]) for i in range(0, len(vals) - 1, 2)]
            if t == "polygon" and pts and \
                    (abs(pts[0][0] - pts[-1][0]) > 1e-9 or
                     abs(pts[0][1] - pts[-1][1]) > 1e-9):
                pts.append(pts[0])
            if len(pts) >= 2:
                emit([{"points": pts, "closed": t == "polygon"}])
        elif t == "g" or t == "svg" or t == "a":
            inherited = style
            for child in list(el):
                walk(child, m, inherited)
            return
        else:
            skipped[0] += 1
            return
        for child in list(el):
            walk(child, m, style)

    # <defs> 内的模板图形不算跳过数量：walk 会因 defs 分支直接返回
    for child in list(root):
        walk(child, [1.0, 0.0, 0.0, 1.0, 0.0, 0.0], {})
    info.paths = paths
    info.skipped = skipped[0]
    return info


# ==================== 生成 ====================

def _fmt(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return s if s else "0"


def formatPathData(points: list, closed: bool) -> str:
    """折线 → path d 字符串（M/L/Z）。"""
    if len(points) < 2:
        return ""
    out = [f"M{_fmt(points[0][0])},{_fmt(points[0][1])}"]
    for x, y in points[1:]:
        out.append(f"L{_fmt(x)},{_fmt(y)}")
    if closed:
        out.append("Z")
    return "".join(out)

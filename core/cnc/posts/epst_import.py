"""ArtCAM .epst 后处理定义 → Cangai 后置文件 转换器。

epst 是 ArtCAM 的后处理"配方"（XML）：描述每类刀路动作输出什么文本、
哪些参数在何时输出（ALWAYS/UPDATED/NEVER 模态）、数字格式与 M/G 码值映射。
它不是可执行代码，只有 ArtCAM 能读——Cangai 自研后处理引擎无法直接运行它。

本模块把 epst 的方言参数映射到 Cangai 的结构化类属性（EnhancedGcodePost），
生成一个可用的 .py 后置文件，供 scan_posts 注册。转换覆盖 Cangai 会生成的
指令族（快速/直线/圆弧/主轴/冷却/换刀/暂停/程序头尾）；钻孔固定循环
（G81/G83…）、补偿、RTCP 等 Cangai 暂不生成的指令被安全跳过。

映射要点（ArtCAM 4.1 语义）：
- 参数 state: ALWAYS=每次输出 / UPDATED=变化才输出（模态）/ NEVER=不输出
- 参数值映射表（如 MotionMode: QUICK=00 LINE=01 → G00/G01）
- block 内项以空格连接 → 一行；多 block → 多行
- FinishProgram 的强制赋值（M09/M05）→ Cangai 的 flush_unclosed 收尾冲刷
- 坐标 3 位删尾零 / 进给 1 位保留 .0 / 圆弧 I/J 增量恒输出（模板1.NC 验证）
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# 中间模型
# --------------------------------------------------------------------------- #


@dataclass
class ParamDef:
    """单个参数的格式与输出前缀。"""
    prefix: str = ""
    postfix: str = ""
    decimals: int = 3
    incremental: bool = False     # mIncreMode=TRUE（圆弧圆心增量）
    negative: bool = True


@dataclass
class CommandItem:
    """命令块内的一项输出（参数或固定文本）。"""
    kind: str                      # "param" / "text"
    param_id: str | None = None
    state: str = "ALWAYS"          # ALWAYS / UPDATED / NEVER
    text: str = ""
    assign: str | None = None      # assign="5"（强制输出）
    value: str | None = None       # 强制组值


@dataclass
class CommandDef:
    id: str
    active: bool
    blocks: list[list[CommandItem]] = field(default_factory=list)


@dataclass
class EpstModel:
    """解析后的 epst 中间模型。"""
    name: str = "Imported"
    formats: dict[str, ParamDef] = field(default_factory=dict)   # 参数 id → 格式
    groups: dict[str, dict[str, str]] = field(default_factory=dict)  # 参数 id → {组: 值}
    commands: dict[str, CommandDef] = field(default_factory=dict)
    program_symbol: str = "%"      # block_set.symbol


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #


def _child(el, ns, tag):
    return el.find(f"{ns}{tag}")


def parse_epst(path: str | Path) -> EpstModel:
    """解析 .epst 文件 → EpstModel。

    ArtCAM 的 XML 使用 ``esp:`` 前缀却未声明命名空间（非标准），
    解析前剥掉前缀再交给 ElementTree。
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"<(/?)(esp:)", r"<\1", text)
    root = ET.fromstring(text)
    ns = ""
    model = EpstModel(name=p.stem)

    # ---- config-parameters / program_format ----
    cfg = _child(root, ns, "config-parameters")
    if cfg is not None:
        pf = _child(cfg, ns, "program_format")
        if pf is not None:
            bset = _child(pf, ns, "block_set")
            if bset is not None:
                model.program_symbol = (
                    bset.get("symbol_value") or bset.get("symbol") or "%")

    # ---- parameters: format(参数格式) + 直接 param(带 group 的参数) ----
    params_el = _child(root, ns, "parameters")
    if params_el is not None:
        for fmt in params_el.findall(f"{ns}format"):
            fdec = int(fmt.get("mDecPlaces", "3"))
            finc = (fmt.get("mIncreMode", "FALSE").upper() == "TRUE")
            fneg = (fmt.get("mNegative", "EXIST").upper() != "NEVER")
            for prm in fmt.findall(f"{ns}param"):
                pid = prm.get("id")
                if pid:
                    model.formats[pid] = ParamDef(
                        prefix=prm.get("prefix", ""),
                        postfix=prm.get("postfix", ""),
                        decimals=fdec,
                        incremental=finc,
                        negative=fneg,
                    )
        # 直接 param（带 group 的值映射参数）
        for prm in params_el.findall(f"{ns}param"):
            pid = prm.get("id")
            if not pid:
                continue
            grp: dict[str, str] = {}
            for g in prm.findall(f"{ns}group"):
                gid = g.get("id")
                val = g.get("value")
                if gid is not None and val is not None:
                    grp[gid] = val
            if grp:
                model.groups[pid] = grp
            # 无 group 的裸参数（ToolNumber/SpindleSpeed/DwellS…）也登记格式
            if pid not in model.formats:
                model.formats[pid] = ParamDef(prefix=prm.get("prefix", ""))

    # ---- commands ----
    cmds_el = _child(root, ns, "commands")
    if cmds_el is not None:
        for cmd in cmds_el.findall(f"{ns}command"):
            cid = cmd.get("id")
            if not cid:
                continue
            active = (cmd.get("state", "ACTIVE").upper() == "ACTIVE")
            cd = CommandDef(id=cid, active=active)
            for blk in cmd.findall(f"{ns}block"):
                items: list[CommandItem] = []
                for it in blk:
                    tag = it.tag.split("}")[-1]
                    if tag == "cmd-item-text":
                        tx = _child(it, ns, "text")
                        items.append(CommandItem(
                            kind="text",
                            state=it.get("state", "ALWAYS"),
                            text=tx.text or "" if tx is not None else ""))
                    elif tag == "cmd-item-param":
                        items.append(CommandItem(
                            kind="param",
                            param_id=it.get("id"),
                            state=it.get("state", "ALWAYS"),
                            assign=it.get("assign"),
                            value=it.get("value")))
                if items:
                    cd.blocks.append(items)
            model.commands[cid] = cd
    return model


# --------------------------------------------------------------------------- #
# 方言提取
# --------------------------------------------------------------------------- #

# 参数 id → Cangai 占位符（引擎注入的值）
_PLACEHOLDER = {
    "SpindleSpeed": "S{rpm}",
    "ToolNumber": "T{tool_number}",
    "ToolPrepareNumber": "T{tool_number}",
}


def _group_value(model: EpstModel, param_id: str, group_id: str,
                 default: str = "") -> str:
    """取参数组的值（如 MotionMode/QUICK → "00"）。"""
    g = model.groups.get(param_id)
    if not g:
        return default
    v = g.get(group_id)
    return default if v is None else v


def _render_item(model: EpstModel, it: CommandItem) -> str | None:
    """渲染命令项为文本片段；无法映射（引擎无此概念）返回 None。"""
    if it.kind == "text":
        return it.text
    pid = it.param_id
    if pid is None or pid == "BlockNumber":
        return None
    if pid in _PLACEHOLDER:
        return _PLACEHOLDER[pid]
    # 组值参数 → prefix + 组值（取第一组作为默认；具体组由调用方指定）
    fmt = model.formats.get(pid)
    prefix = fmt.prefix if fmt else ""
    if it.assign is not None and it.value is not None:
        return f"{prefix}{it.value}"
    return None


def _render_block_line(model: EpstModel, items: list[CommandItem]) -> str:
    """块内项以空格连接成一行（ArtCAM 默认分隔）。"""
    parts: list[str] = []
    for it in items:
        s = _render_item(model, it)
        if s is not None and s != "":
            parts.append(s)
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# 生成 Cangai 后置文件
# --------------------------------------------------------------------------- #


def _extract_attrs(model: EpstModel) -> dict:
    """从 epst 模型提取 EnhancedGcodePost 类属性字典。"""
    a: dict = {}
    ns = model  # 缩写

    # ---- 数字格式（MachineXYZ / FeedRate / ArcCenterXYZ）----
    mxyz = ns.formats.get("MachineX") or ns.formats.get("MachineY")
    ffeed = ns.formats.get("FeedRate")
    farc = ns.formats.get("ArcCenterX") or ns.formats.get("ArcCenterY")
    a["coord_decimals"] = mxyz.decimals if mxyz else 3
    a["feed_decimals"] = ffeed.decimals if ffeed else 1
    a["arc_center_decimals"] = farc.decimals if farc else 3
    # ArtCAM 惯例（模板1.NC 验证）：坐标删尾零、进给保留 .0
    a["coord_strip_zeros"] = True
    a["feed_strip_zeros"] = False

    # ---- G/M 码 ----
    a["rapid_code"] = "G" + _group_value(ns, "MotionMode", "QUICK", "00")
    a["feed_code"] = "G" + _group_value(ns, "MotionMode", "LINE", "01")
    a["arc_cw_code"] = "G" + _group_value(ns, "MotionMode", "CWARC", "02")
    a["arc_ccw_code"] = "G" + _group_value(ns, "MotionMode", "CCWARC", "03")
    a["arc_plane_code"] = "G" + _group_value(ns, "PlaneMode", "PLANE_XY", "17") \
        if "PlaneMode" in ns.groups else None
    a["spindle_on"] = "S{rpm} M03"      # FirstMoveAfterToolChange: S.. M03
    a["spindle_off"] = "M" + _group_value(ns, "SpindleDirMode", "SDIR_OFF", "05")
    a["coolant_on"] = "M" + _group_value(ns, "CoolantMode", "COOL_ON", "08")
    a["coolant_off"] = "M" + _group_value(ns, "CoolantMode", "COOL_OFF", "09")
    # 暂停：优先 CmdTimeS 的 DwellS（G04 X），退而 CmdTimeMS 的 DwellMS（G04 P）
    dwell_prefix = ""
    for cid in ("CmdTimeS", "CmdTimeMS"):
        cd = ns.commands.get(cid)
        if cd and cd.active and cd.blocks:
            for blk in cd.blocks:
                for it in blk:
                    if (it.kind == "param" and it.param_id
                            and it.param_id.startswith("Dwell")
                            and it.param_id != "BlockNumber"):
                        fmt = ns.formats.get(it.param_id)
                        if fmt and fmt.prefix:
                            dwell_prefix = fmt.prefix
                            break
            if dwell_prefix:
                break
    a["dwell_code"] = f"{dwell_prefix}{{dwell}}" if dwell_prefix else "G04 P{dwell}"

    # ---- 程序宏 ----
    a["program_start"] = _command_text(ns, "StartProgram") or ns.program_symbol
    a["tool_change"] = _tool_change_macro(ns)

    # ---- 程序尾 + 收尾冲刷 ----
    end_lines: list[str] = []
    flush = False
    home = False
    finish = ns.commands.get("FinishProgram")
    if finish and finish.active:
        for blk in finish.blocks:
            forced = [it for it in blk
                      if it.kind == "param" and it.assign is not None]
            if forced:
                # 强制赋值参数（M09/M05 收尾）→ flush_unclosed
                flush = True
                continue
            line = _render_block_line(ns, blk)
            if line:
                end_lines.append(line)
                if re.search(r"\bG0+\s+X0\s+Y0\b", line):
                    home = True
    a["program_end"] = "\n".join(end_lines) if end_lines else "M30"
    a["flush_unclosed"] = bool(flush)
    a["suppress_final_home"] = bool(home)

    # ---- 模态（MoveLinear/MoveRapid 的参数 state）----
    modal_g = modal_xyz = modal_f = True
    for cid in ("MoveLinear", "MoveRapid"):
        cd = ns.commands.get(cid)
        if not cd or not cd.blocks:
            continue
        for blk in cd.blocks:
            for it in blk:
                if it.kind != "param" or not it.param_id:
                    continue
                pid = it.param_id
                if pid == "MotionMode" and it.state == "ALWAYS":
                    modal_g = False
                elif pid in ("MachineX", "MachineY", "MachineZ") \
                        and it.state == "ALWAYS":
                    modal_xyz = False
                elif pid == "FeedRate" and it.state == "ALWAYS":
                    modal_f = False
    a["modal_g"] = modal_g
    a["modal_xyz"] = modal_xyz
    a["modal_f"] = modal_f

    # ArtCAM 输出惯例：无行号、无注释
    a["emit_comments"] = False
    a["line_ending"] = "\r\n"
    return a


def _command_text(model: EpstModel, cid: str) -> str:
    """命令的纯文本行（如 StartProgram 的 %）。"""
    cd = model.commands.get(cid)
    if not cd or not cd.blocks:
        return ""
    lines = [blk[0].text for blk in cd.blocks
             if len(blk) == 1 and blk[0].kind == "text"]
    return "\n".join(lines)


def _tool_change_macro(model: EpstModel) -> str:
    """从 LoadFirstTool / ChangeTool 提取换刀宏（如 "T{tool_number} M6"）。"""
    for cid in ("LoadFirstTool", "ChangeTool"):
        cd = model.commands.get(cid)
        if cd and cd.blocks:
            line = _render_block_line(model, cd.blocks[0])
            if line:
                return line
    return "T{tool_number} M6"


# --------------------------------------------------------------------------- #
# 输出 .py 文件
# --------------------------------------------------------------------------- #


def _class_name(stem: str) -> str:
    name = re.sub(r"[^\w]", "_", stem)
    if not name or name[0].isdigit():
        name = "P" + name
    return f"UserPost_{name}"


def _controller_id(stem: str) -> str:
    """controller id：user_ + 文件名（保留中文，稳定可读，小写统一）。"""
    cid = re.sub(r"\s+", "_", stem.strip())
    return f"user_{cid}".lower()


def controller_id_from_stem(stem: str) -> str:
    """公开入口：从文件名（不含扩展名）推导 controller id（UI 选中用）。"""
    return _controller_id(stem)


def build_post_source(model: EpstModel, source_path: str) -> tuple[str, str, str]:
    """生成 Cangai 后置文件源码。

    返回 (class_name, controller_id, py_source)。
    """
    attrs = _extract_attrs(model)
    stem = model.name
    class_name = _class_name(stem)
    controller_id = _controller_id(stem)

    lines = [
        f'"""由 Cangai 从 ArtCAM 后处理定义自动生成：{stem}.epst',
        f"来源文件: {str(source_path).replace(chr(92), '/')}",
        "如需调整方言（程序头/尾/换刀/模态），直接编辑本文件即可。",
        '"""',
        "from core.cnc.posts.base import EnhancedGcodePost",
        "",
        "",
        f"class {class_name}(EnhancedGcodePost):",
        f"    name = {f'{stem}（导入）'!r}",
        f"    description = {f'ArtCAM 后处理定义导入 · {stem}'!r}",
        f"    controller = {controller_id!r}",
        "",
    ]
    # 按固定顺序输出属性，便于阅读/比对
    order = [
        ("coord_decimals", None), ("feed_decimals", None),
        ("arc_center_decimals", None), ("coord_strip_zeros", None),
        ("feed_strip_zeros", None),
        ("rapid_code", None), ("feed_code", None),
        ("arc_cw_code", None), ("arc_ccw_code", None),
        ("arc_plane_code", None),
        ("spindle_on", None), ("spindle_off", None),
        ("coolant_on", None), ("coolant_off", None), ("dwell_code", None),
        ("program_start", None), ("program_end", None),
        ("tool_change", None),
        ("modal_g", None), ("modal_xyz", None), ("modal_f", None),
        ("suppress_final_home", None), ("flush_unclosed", None),
    ]
    for key, _ in order:
        lines.append(f"    {key} = {attrs[key]!r}")
    lines.append('    line_ending = "\\r\\n"')
    lines.append("")
    return class_name, controller_id, "\n".join(lines)


def import_epst(path: str | Path, dest_dir: str | Path) -> tuple[str, str]:
    """导入 .epst → 生成 .py 后置文件到 dest_dir。

    返回 (生成的 .py 绝对路径, controller_id)。同名文件（同 epst 名）
    覆盖（重新导入=更新）。
    """
    src = Path(path)
    model = parse_epst(src)
    _, controller_id, source = build_post_source(model, str(src))
    out = Path(dest_dir) / f"{model.name}.py"
    out.write_text(source, encoding="utf-8")
    return str(out), controller_id


# 便于命令行自测
if __name__ == "__main__":
    import sys
    p = sys.argv[1]
    m = parse_epst(p)
    cn, cid, src = build_post_source(m, p)
    print("class:", cn)
    print("controller:", cid)
    print("---- 生成源码 ----")
    print(src)

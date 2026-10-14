"""EnhancedGcodePost —— Cangai 后处理引擎核心。

模态状态机 + 可配置方言 + 双格式器。方言差异全部收敛为类属性，
自定义后置文件 = 继承本类并改类属性即可，无需理解后处理细节。

设计要点（详见 docs/CNC路径输出后处理体系设计.md）：
- 模态：G 码 / X Y Z / F / 主轴 / 冷却只输出变化的量；换刀后强制全量。
- 双格式器：坐标删尾零（4.840→4.84）、进给可选保留 .0（F300.0）。
- 不修改 vendored pymillcam；实现其 PostProcessor 协议（post_program）
  即可对接现有 generate_program / post_program 调用。
"""
from __future__ import annotations

from dataclasses import dataclass

from pymillcam.engine.ir import IRInstruction, MoveType, Toolpath


@dataclass
class _ModalState:
    """模态状态：记录上一输出值，值变化才重新输出。None = 未定义（强制输出）。"""
    g: str | None = None            # 当前 G 模式（G00/G01/G02/G03）
    x: float | None = None
    y: float | None = None
    z: float | None = None
    f: float | None = None          # 上一进给
    s: int | None = None            # 上一主轴转速
    spindle_dir: int | None = None  # 3=M03 4=M04 0=停
    coolant: bool | None = None     # 冷却开关状态
    plane: str | None = None        # G17/G18/G19 平面状态
    tool: int | None = None         # 当前刀号
    canned: bool = False            # 固定循环（G81/G83）活动中

    def reset(self) -> None:
        """清空全部状态 → 下一条指令强制全量输出（换刀后语义）。"""
        self.g = self.x = self.y = self.z = self.f = None
        self.s = self.spindle_dir = None
        self.coolant = self.plane = None
        self.tool = None
        self.canned = False


@dataclass
class _Hole:
    """一次钻孔的解析结果（_matchHole 输出）。"""
    end: int                 # 消费到的指令下标（不含）
    x: float | None
    y: float | None
    depth: float             # 最终孔深（负值）
    clear_z: float | None    # R 平面（ clearance ）
    f: float | None
    pecks: list              # 啄钻中间深度（>1 个即 G83）


@dataclass
class _CannedDrill:
    """合成指令：一整轮固定循环的首孔（G98 G81/G83 X Y Z R Q F）。"""
    x: float | None
    y: float | None
    depth: float
    clear_z: float | None
    f: float | None
    pecks: list


@dataclass
class _CannedRepeat:
    """合成指令：固定循环模态重复（仅 X Y，控制器自动再钻一个孔）。"""
    x: float | None
    y: float | None


class EnhancedGcodePost:
    """IR → 控制器 G 代码。子类配置方言，本类负责模态输出与格式。"""

    name: str = "Basic"
    description: str = ""
    controller: str = "basic"

    # ---- 数字格式 ----
    coord_decimals: int = 4          # X/Y/Z 小数位
    feed_decimals: int = 1           # F 小数位
    arc_center_decimals: int = 4     # I/J/K 小数位
    coord_strip_zeros: bool = True   # 坐标删尾零：4.840 → 4.84
    feed_strip_zeros: bool = True    # 进给删尾零：300.0 → 300（Mach3 关 → F300.0）

    # ---- 指令方言 ----
    rapid_code: str = "G0"
    feed_code: str = "G1"
    arc_cw_code: str = "G2"
    arc_ccw_code: str = "G3"
    arc_plane_code: str | None = None   # 圆弧平面声明（G17），切换才输出；None=不输出
    spindle_on: str = "M3 S{rpm}"
    spindle_off: str = "M5"
    spindle_ccw: str = "M4 S{rpm}"
    coolant_on: str = "M8"
    coolant_off: str = "M9"
    dwell_code: str = "G4 P{dwell}"

    # ---- 程序宏（可被 MachineDefinition.macros 覆盖）----
    program_start: str = ""
    program_end: str = "M5\nM30"
    tool_change: str = "T{tool_number} M6"
    header_comments: tuple[str, ...] = ()   # 程序头注释（括号包裹）
    emit_comments: bool = True              # IR COMMENT 指令是否输出（Mach3 模板无注释）

    # ---- 模态开关 ----
    modal_g: bool = True     # G 码只输出切换
    modal_xyz: bool = True   # X/Y/Z 只输出变化
    modal_f: bool = True     # F 只输出变化
    suppress_final_home: bool = False  # 吞掉末条 RAPID X0Y0，由 program_end 输出回原点
    flush_unclosed: bool = False       # 程序尾前补发未关闭的冷却/主轴（M09/M05 收尾）

    # ---- 钻孔固定循环（复刻 JDNcPost 固定循环输出；默认关闭保持既有输出） ----
    canned_cycles: bool = False        # True 时把钻孔序列压缩为 G81/G83
    drill_code: str = "G81"            # 简单钻孔
    peck_code: str = "G83"             # 啄钻（全回退；分段回退不压缩，保守）
    canned_cancel: str = "G80"         # 固定循环取消
    canned_return: str = "G98"         # 回初始平面

    line_ending: str = "\n"

    # ------------------------------------------------------------------ #
    # 协议入口
    # ------------------------------------------------------------------ #
    def post_program(
        self,
        toolpaths: list[Toolpath],
        *,
        macros: dict | None = None,
    ) -> str:
        """把 IR toolpath 列表翻译为完整 G 代码程序。

        macros（MachineDefinition.macros）中非空项覆盖对应类默认宏；
        空项回退类默认。
        """
        effective = {
            "program_start": self.program_start,
            "program_end": self.program_end,
            "tool_change": self.tool_change,
        }
        if macros:
            for k in ("program_start", "program_end", "tool_change"):
                v = macros.get(k)
                if v:
                    effective[k] = v

        lines = [f"({c})" for c in self.header_comments]
        lines.extend(effective["program_start"].splitlines())

        st = _ModalState()
        st.reset()
        last_tp = len(toolpaths) - 1
        for ti, tp in enumerate(toolpaths):
            insts = tp.instructions
            if self.canned_cycles:
                insts = self._canned_pass(insts)
            n = len(insts)
            for ii, item in enumerate(insts):
                if isinstance(item, _CannedDrill):
                    if st.canned:
                        lines.append(self.canned_cancel)
                        st.canned = False
                    line = self._emit_canned(item, st)
                elif isinstance(item, _CannedRepeat):
                    line = self._emit_canned_repeat(item, st)
                else:
                    if st.canned and not self._isCannedRepeat(item):
                        # 固定循环后接非重复段：先 G80 取消再正常输出
                        lines.append(self.canned_cancel)
                        st.canned = False
                    line = self._emit(
                        item, st, effective,
                        is_last=(ti == last_tp and ii == n - 1),
                    )
                if line:
                    lines.append(line)
        if st.canned:
            lines.append(self.canned_cancel)
            st.canned = False

        # 收尾冲刷：未关闭的冷却/主轴先关（Mach3 尾部 M09→M05 顺序由此保证，
        # IR 已显式关闭时不重复输出；此逻辑只对 flush_unclosed 方言生效）
        if self.flush_unclosed:
            if st.coolant:
                lines.append(self.coolant_off)
                st.coolant = False
            if st.spindle_dir:
                lines.append(self.spindle_off)
                st.spindle_dir = 0

        lines.extend(effective["program_end"].splitlines())
        return self.line_ending.join(lines) + self.line_ending

    # ------------------------------------------------------------------ #
    # 钻孔固定循环（G81/G83）压缩
    # ------------------------------------------------------------------ #
    @staticmethod
    def _isRapidZ(inst) -> bool:
        return (inst.type == MoveType.RAPID and inst.z is not None
                and inst.x is None and inst.y is None)

    @staticmethod
    def _isFeedZ(inst) -> bool:
        return (inst.type == MoveType.FEED and inst.z is not None
                and inst.x is None and inst.y is None)

    def _isCannedRepeat(self, inst) -> bool:
        """固定循环模态重复段：只有 X/Y 的 RAPID（控制器自动再执行一轮）。"""
        return (inst.type == MoveType.RAPID and inst.z is None
                and (inst.x is not None or inst.y is not None))

    def _matchHole(self, ins: list, i: int) -> "_Hole | None":
        """从下标 i 尝试解析一轮完整钻孔；成功返回 _Hole，否则 None。

        模式：RAPID XY → [RAPID Z(R平面)] → FEED Z(孔深或首切深)
              → [RAPID Z(回退) → FEED Z(更深)]* → RAPID Z(回退)
        保守约束：无 R 平面、中间回退不到 R 平面（分段回退）、孔底
        DWELL——都不压缩，按原样输出（正确性优先于行数）。
        """
        n = len(ins)
        if i >= n:
            return None
        a = ins[i]
        if a.type != MoveType.RAPID or (a.x is None and a.y is None):
            return None
        j = i + 1
        clear_z = a.z
        if j < n and self._isRapidZ(ins[j]):
            clear_z = ins[j].z
            j += 1
        if clear_z is None or j >= n or not self._isFeedZ(ins[j]):
            return None
        feeds = [ins[j].z]
        f = ins[j].f
        k = j + 1
        while (k + 1 < n and self._isRapidZ(ins[k])
               and self._isFeedZ(ins[k + 1])
               and ins[k + 1].z < feeds[-1] - 1e-9):
            if ins[k].z < clear_z - 1e-6:
                return None        # 分段回退（chip break）：不压成 G83
            feeds.append(ins[k + 1].z)
            k += 2
        if k < n and ins[k].type == MoveType.DWELL:
            return None        # 孔底暂停：不压缩（保守）
        if k < n and self._isRapidZ(ins[k]):
            if ins[k].z < clear_z - 1e-6:
                return None
            k += 1
        return _Hole(end=k, x=a.x, y=a.y, depth=feeds[-1], clear_z=clear_z,
                     f=f, pecks=feeds[:-1] if len(feeds) > 1 else [])

    def _canned_pass(self, instructions: list) -> list:
        """把连续同模式钻孔序列压缩为 _CannedDrill/_CannedRepeat 合成指令。

        R 平面与最终孔深都相同的后续孔只发 X/Y（Fanuc 固定循环模态
        重复语义）；任一变化 → 重新发整轮。
        """
        out: list = []
        i, n = 0, len(instructions)
        while i < n:
            hole = self._matchHole(instructions, i)
            if hole is None:
                out.append(instructions[i])
                i += 1
                continue
            out.append(_CannedDrill(hole.x, hole.y, hole.depth,
                                    hole.clear_z, hole.f, hole.pecks))
            i = hole.end
            while True:
                nxt = self._matchHole(instructions, i)
                if (nxt is None
                        or abs((nxt.clear_z or 0.0) - (hole.clear_z or 0.0))
                        > 1e-6
                        or abs(nxt.depth - hole.depth) > 1e-6):
                    break
                out.append(_CannedRepeat(nxt.x, nxt.y))
                i = nxt.end
        return out

    def _emit_canned(self, item: "_CannedDrill", st: "_ModalState") -> str:
        """首孔：G98 G81/G83 X Y Z R [Q] F。"""
        code = self.peck_code if item.pecks else self.drill_code
        parts = [self.canned_return, code]
        if item.x is not None:
            parts.append("X" + self._fmt_coord(item.x))
        if item.y is not None:
            parts.append("Y" + self._fmt_coord(item.y))
        parts.append("Z" + self._fmt_coord(item.depth))
        if item.clear_z is not None:
            parts.append("R" + self._fmt_coord(item.clear_z))
        if item.pecks:
            # Q = 每啄进给量：取首啄增量（R 平面 → 第一啄深）
            q = abs(item.pecks[0] - (item.clear_z or 0.0))
            parts.append("Q" + self._fmt_coord(q))
        if item.f is not None:
            parts.append("F" + self._fmt_feed(item.f))
            st.f = item.f
        st.canned = True
        if item.x is not None:
            st.x = item.x
        if item.y is not None:
            st.y = item.y
        return " ".join(parts)

    def _emit_canned_repeat(self, item: "_CannedRepeat",
                            st: "_ModalState") -> str | None:
        """模态重复：仅输出变化的 X/Y（F/Z/R 沿用循环态）。"""
        parts: list[str] = []
        if item.x is not None and (not self.modal_xyz or item.x != st.x):
            parts.append("X" + self._fmt_coord(item.x))
        if item.y is not None and (not self.modal_xyz or item.y != st.y):
            parts.append("Y" + self._fmt_coord(item.y))
        if not parts:
            return None
        if item.x is not None:
            st.x = item.x
        if item.y is not None:
            st.y = item.y
        return " ".join(parts)

    # ------------------------------------------------------------------ #
    # 单指令翻译
    # ------------------------------------------------------------------ #
    def _emit(
        self,
        inst: IRInstruction,
        st: _ModalState,
        effective: dict,
        *,
        is_last: bool,
    ) -> str | None:
        t = inst.type
        if t == MoveType.COMMENT:
            if not (self.emit_comments and inst.comment):
                return None
            return f"({inst.comment})"
        if t == MoveType.RAPID:
            return self._emit_move(inst, st, self.rapid_code, is_last=is_last)
        if t == MoveType.FEED:
            return self._emit_move(inst, st, self.feed_code)
        if t == MoveType.ARC_CW:
            return self._emit_arc(inst, st, self.arc_cw_code)
        if t == MoveType.ARC_CCW:
            return self._emit_arc(inst, st, self.arc_ccw_code)
        if t == MoveType.SPINDLE_ON:
            if inst.s is None:
                return None
            if st.spindle_dir == 3 and st.s == inst.s:
                return None
            st.spindle_dir, st.s = 3, inst.s
            return self.spindle_on.format(rpm=inst.s)
        if t == MoveType.SPINDLE_OFF:
            if st.spindle_dir == 0:
                return None
            st.spindle_dir = 0
            return self.spindle_off
        if t == MoveType.COOLANT_ON:
            if st.coolant:
                return None
            st.coolant = True
            return self.coolant_on
        if t == MoveType.COOLANT_OFF:
            if st.coolant is False:
                return None
            st.coolant = False
            return self.coolant_off
        if t == MoveType.TOOL_CHANGE:
            if inst.tool_number is None:
                return None
            st.reset()
            st.tool = inst.tool_number
            return effective["tool_change"].replace(
                "{tool_number}", str(inst.tool_number)).rstrip("\n")
        if t == MoveType.DWELL:
            if inst.f is None:
                return None
            return self.dwell_code.format(dwell=self._fmt_coord(inst.f))
        # MACRO 及其他未知类型：忽略
        return None

    def _emit_move(
        self,
        inst: IRInstruction,
        st: _ModalState,
        code: str,
        *,
        is_last: bool = False,
    ) -> str | None:
        if (is_last and self.suppress_final_home
                and inst.z is None and inst.x == 0 and inst.y == 0):
            # 末条 RAPID X0 Y0 由 program_end 宏输出（Mach3: G0 X0 Y0）
            return None
        if (st.canned and code == self.rapid_code and inst.z is None
                and (inst.x is not None or inst.y is not None)):
            # 固定循环模态重复：只发 X/Y（不带 G00，控制器自动再钻一个孔）
            parts: list[str] = []
            if inst.x is not None and (not self.modal_xyz or inst.x != st.x):
                parts.append("X" + self._fmt_coord(inst.x))
            if inst.y is not None and (not self.modal_xyz or inst.y != st.y):
                parts.append("Y" + self._fmt_coord(inst.y))
            if not parts:
                return None
            if inst.x is not None:
                st.x = inst.x
            if inst.y is not None:
                st.y = inst.y
            return " ".join(parts)
        parts: list[str] = []
        if not (self.modal_g and st.g == code):
            parts.append(code)
        if inst.x is not None and (not self.modal_xyz or inst.x != st.x):
            parts.append("X" + self._fmt_coord(inst.x))
        if inst.y is not None and (not self.modal_xyz or inst.y != st.y):
            parts.append("Y" + self._fmt_coord(inst.y))
        if inst.z is not None and (not self.modal_xyz or inst.z != st.z):
            parts.append("Z" + self._fmt_coord(inst.z))
        if inst.f is not None and (not self.modal_f or inst.f != st.f):
            parts.append("F" + self._fmt_feed(inst.f))
        if not parts:
            return None
        st.g = code
        if inst.x is not None:
            st.x = inst.x
        if inst.y is not None:
            st.y = inst.y
        if inst.z is not None:
            st.z = inst.z
        if inst.f is not None:
            st.f = inst.f
        return " ".join(parts)

    def _emit_arc(
        self,
        inst: IRInstruction,
        st: _ModalState,
        code: str,
    ) -> str | None:
        parts: list[str] = []
        if self.arc_plane_code and st.plane != self.arc_plane_code:
            parts.append(self.arc_plane_code)
            st.plane = self.arc_plane_code
        if not (self.modal_g and st.g == code):
            parts.append(code)
        if inst.x is not None and (not self.modal_xyz or inst.x != st.x):
            parts.append("X" + self._fmt_coord(inst.x))
        if inst.y is not None and (not self.modal_xyz or inst.y != st.y):
            parts.append("Y" + self._fmt_coord(inst.y))
        if inst.z is not None and (not self.modal_xyz or inst.z != st.z):
            parts.append("Z" + self._fmt_coord(inst.z))
        # I/J/K 恒输出（增量圆心），不参与模态
        if inst.i is not None:
            parts.append("I" + self._fmt_arc(inst.i))
        if inst.j is not None:
            parts.append("J" + self._fmt_arc(inst.j))
        if inst.f is not None and (not self.modal_f or inst.f != st.f):
            parts.append("F" + self._fmt_feed(inst.f))
        if not parts:
            return None
        st.g = code
        if inst.x is not None:
            st.x = inst.x
        if inst.y is not None:
            st.y = inst.y
        if inst.z is not None:
            st.z = inst.z
        if inst.f is not None:
            st.f = inst.f
        return " ".join(parts)

    # ------------------------------------------------------------------ #
    # 格式器
    # ------------------------------------------------------------------ #
    def _fmt(self, value: float, decimals: int, strip: bool) -> str:
        text = f"{value:.{decimals}f}"
        if strip and "." in text:
            text = text.rstrip("0").rstrip(".")
        if text in ("-0", "-0."):
            text = "0"
        return text or "0"

    def _fmt_coord(self, value: float) -> str:
        return self._fmt(value, self.coord_decimals, self.coord_strip_zeros)

    def _fmt_feed(self, value: float) -> str:
        return self._fmt(value, self.feed_decimals, self.feed_strip_zeros)

    def _fmt_arc(self, value: float) -> str:
        return self._fmt(value, self.arc_center_decimals, True)

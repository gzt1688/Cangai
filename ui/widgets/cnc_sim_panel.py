# -*- coding: utf-8 -*-
"""实体模拟面板（单面板）：上方 G 代码显示框，下方播放条（横）。

点击"开始模拟"后由主窗口调用 showPanel() + play()：加载 G 代码文本、
QTimer 逐行推进并更新 CncState.simLine / simPosition（画布 3D 实体刀具/
游标据此移动），G 代码当前执行行黄色高亮。播放完毕后再点播放从头开始。
"""
import math
import re

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QTextCursor, QTextFormat
from PySide6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QLabel,
                               QPlainTextEdit, QPushButton, QSlider,
                               QTextEdit, QVBoxLayout, QWidget)

import ui.styles.theme as theme
import core.app_settings as app_settings
from core.cnc.state import CncState

# G 代码坐标轴解析
_RE_X = re.compile(r'X([-+]?\d*\.?\d+)')
_RE_Y = re.compile(r'Y([-+]?\d*\.?\d+)')
_RE_Z = re.compile(r'Z([-+]?\d*\.?\d+)')
_RE_G = re.compile(r'\bG([0-9]+(?:\.[0-9]+)?)')   # 行内全部 G 码（含两位码）
_RE_I = re.compile(r'I([-+]?\d*\.?\d+)')
_RE_J = re.compile(r'J([-+]?\d*\.?\d+)')

# 播放速度选项：(显示文本, 每次 tick 推进行数)
_SPEEDS = [("2x", 2.0), ("3x", 3.0), ("4x", 4.0), ("5x", 5.0),
           ("8x", 8.0), ("10x", 10.0), ("15x", 15.0),
           ("20x", 20.0), ("30x", 30.0), ("50x", 50.0)]

_TICK_MS = 50


class CncSimulationPanel(QFrame):
    """实体模拟面板：上方 G 代码显示框 + 下方播放条（横，贴画布左侧）。"""

    closeRequested = Signal()

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._state = CncState.instance()
        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._tick)
        # 渲染定时器：以 16ms（≈60fps）驱动 3D 视口重绘，刀头位置由
        # state.simInterpPos() 在两次逻辑 tick 之间线性插值——刀具沿路径
        # 动画连续丝滑。实体坯料已禁用，每帧仅路径+刀具绘制，60fps 无压力。
        self._renderTimer = QTimer(self)
        self._renderTimer.setInterval(16)
        self._renderTimer.timeout.connect(self._renderTick)
        self._acc = 0.0
        self.setFixedWidth(340)
        # 平滑播放状态：moves=有坐标的移动段 [(行号, 目标xyz, 是否切削)]，mi=当前段
        self._moves: list = []
        self._mi = -1
        self._cur: list = None     # 当前实际位置（连续插值）
        self._target: list = None  # 当前段目标
        self._cutting: bool = False  # 当前段是否切削（G1/G2/G3）
        self._finished = False
        # 实体模拟（体素切削）
        self._voxel: object = None   # VoxelStock
        self._voxTool = None
        self._voxPrev: tuple = None  # 已扫掠到的刀尖位置
        self._voxKey: tuple = None   # (w,h,t) 缓存，毛坯尺寸变化重建
        self._lastSolid = bool(self._state.showSolidSim)
        self._state.stateChanged.connect(self._onStateChanged)
        self._buildUi()

    # ------------------------------------------------------------------ UI
    def _buildUi(self) -> None:
        self.setStyleSheet(
            "CncSimulationPanel { background:#ffffff; border:1px solid %s;"
            " border-radius:6px; }" % theme.BORDER)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 8)
        lay.setSpacing(6)

        # 标题行
        head = QHBoxLayout()
        title = QLabel("实体模拟")
        title.setStyleSheet("font-weight:bold; font-size:12px; color:%s;"
                            % theme.TEXT)
        head.addWidget(title)
        head.addStretch(1)
        close = QPushButton("×")
        close.setFixedSize(22, 22)
        close.setCursor(Qt.PointingHandCursor)
        close.setStyleSheet(
            "QPushButton { border:none; color:%s; font-size:14px;"
            " background:transparent; } QPushButton:hover { color:%s; }"
            % (theme.TEXT_DIM, theme.PRIMARY))
        close.clicked.connect(self.closePanel)
        head.addWidget(close)
        lay.addLayout(head)

        # 上方：G 代码框（竖着）
        self._gcode = QPlainTextEdit()
        self._gcode.setReadOnly(True)
        self._gcode.setFont(QFont("Consolas", 9))
        self._gcode.setStyleSheet(
            "QPlainTextEdit { background:%s; color:%s; border:1px solid %s;"
            " border-radius:4px; font-family:Consolas; }"
            % ("#fbfbfb", "#333333", theme.BORDER))
        lay.addWidget(self._gcode, 1)

        # 下方：播放条（横）
        bar = QHBoxLayout()
        bar.setSpacing(8)

        self._playBtn = QPushButton("▶")
        self._playBtn.setFixedSize(28, 28)
        self._playBtn.setCursor(Qt.PointingHandCursor)
        self._playBtn.setStyleSheet(
            "QPushButton { background:%s; color:%s; border:none;"
            " border-radius:14px; font-size:13px; }"
            "QPushButton:hover { background:%s; }"
            % (theme.PRIMARY, theme.TEXT_ON_PRIMARY, theme.PRIMARY_DARK))
        self._playBtn.clicked.connect(self.toggle)
        bar.addWidget(self._playBtn)

        self._speedCombo = QComboBox()
        for text, _v in _SPEEDS:
            self._speedCombo.addItem(text)
        self._speedCombo.setCurrentIndex(3)   # 默认 1x
        self._speedCombo.setStyleSheet(
            "QComboBox { background:#fff; border:1px solid %s;"
            " border-radius:3px; padding:1px 4px; font-size:10px; }" % theme.BORDER)
        bar.addWidget(self._speedCombo)

        self._slider = QSlider(Qt.Horizontal)
        self._slider.setRange(0, 0)
        self._slider.setCursor(Qt.PointingHandCursor)
        self._slider.setStyleSheet(
            "QSlider::groove:horizontal { height:5px; background:%s;"
            " border-radius:2px; }"
            "QSlider::handle:horizontal { width:12px; margin:-4px 0;"
            " border-radius:6px; background:%s; }"
            % (theme.BORDER, theme.PRIMARY))
        self._slider.sliderMoved.connect(self._onSeek)
        bar.addWidget(self._slider, 1)

        self._lineLabel = QLabel("0 / 0")
        self._lineLabel.setStyleSheet("color:%s; font-size:10px;" % theme.TEXT_DIM)
        bar.addWidget(self._lineLabel)
        lay.addLayout(bar)

    # ------------------------------------------------------------- 对外
    def _buildMoves(self, lines) -> list:
        """提取有坐标的 G 代码行 → [(行号, (x,y,z), cutting)]。

        cutting=False 表示快移（G0，抬刀不切削），True 为切削（G1/G2/G3）。
        G 代码坐标是模态的：行内只写变化的轴，缺失轴保持当前值，
        所以按行累积维护当前坐标，只更新出现的轴（避免斜飞回原点）。
        运动模态组（G0/G1/G2/G3）同样按行跟踪：Mach3 方言两位码
        （G00/G01/G02）、模态省略 G 码（后续行只写 X/Y/Z）、以及行内
        非运动 G 码（G17 平面、G04 暂停等）都不能干扰运动模态判定。
        G2/G3 圆弧行插值折线（含整圆起点=终点），否则模拟不走圆。
        """
        moves = []
        cur = [0.0, 0.0, 0.0]
        g_mode = 0   # 运动模态：0/1/2/3（跨行保持）
        for i, line in enumerate(lines):
            for m in _RE_G.finditer(line):
                try:
                    code = int(float(m.group(1)))
                except ValueError:
                    continue
                if code in (0, 1, 2, 3):
                    g_mode = code
            x = _RE_X.search(line)
            y = _RE_Y.search(line)
            z = _RE_Z.search(line)
            cutting = g_mode in (1, 2, 3)
            # 圆弧插补：按 I/J 圆心折线化，让刀具真实走出圆弧轨迹
            # （整圆行终点=起点，无 X/Y 也要插值；G2 顺/G3 逆）
            if g_mode in (2, 3):
                ii = _RE_I.search(line)
                jj = _RE_J.search(line)
                if ii or jj:
                    # G 代码 I/J = 圆心相对当前点的偏移（加）
                    self_cx = cur[0] + (float(ii.group(1)) if ii else 0.0)
                    self_cy = cur[1] + (float(jj.group(1)) if jj else 0.0)
                    r = math.hypot(float(ii.group(1)) if ii else 0.0,
                                   float(jj.group(1)) if jj else 0.0)
                    if r > 1e-9:
                        tx = float(x.group(1)) if x else cur[0]
                        ty = float(y.group(1)) if y else cur[1]
                        tz = float(z.group(1)) if z else cur[2]
                        a0 = math.atan2(cur[1] - self_cy, cur[0] - self_cx)
                        a1 = math.atan2(ty - self_cy, tx - self_cx)
                        sweep = a1 - a0
                        if g_mode == 3:       # G3 逆时针
                            while sweep <= 0:
                                sweep += 2 * math.pi
                        else:                       # G2 顺时针
                            while sweep >= 0:
                                sweep -= 2 * math.pi
                        n = max(8, min(96, int(abs(sweep) * r / 0.4) + 1))
                        for k in range(1, n + 1):
                            a = a0 + sweep * k / n
                            # 圆弧带 Z（螺旋）时 z 线性过渡 → 连续螺旋线
                            zk = tz if tz == cur[2] else cur[2] + (tz - cur[2]) * k / n
                            moves.append((i, (self_cx + r * math.cos(a),
                                              self_cy + r * math.sin(a), zk),
                                          True))
                        cur[0], cur[1], cur[2] = tx, ty, tz
                        continue
            if x is None and y is None and z is None:
                continue
            if x is not None:
                cur[0] = float(x.group(1))
            if y is not None:
                cur[1] = float(y.group(1))
            if z is not None:
                cur[2] = float(z.group(1))
            moves.append((i, tuple(cur), cutting))
        return moves

    # --------------------------------------------------------- 实体模拟(体素)
    def _solidTool(self):
        """当前模拟刀具（优先当前选用刀具；取不到用 3mm 平底兜底）。"""
        try:
            tcs = self._state.toolControllers
            if tcs:
                return tcs[0].tool
        except Exception:
            pass
        if self._voxTool is None:
            from pymillcam.core.tools import Tool, ToolShape
            self._voxTool = Tool(name="3mm 平底", shape=ToolShape.ENDMILL,
                                 geometry={"diameter": 3.0, "shank_diameter": 4.0,
                                           "flute_length": 15.0,
                                           "total_length": 50.0,
                                           "taper_angle": 0.0})
        return self._voxTool

    def _ensureVoxel(self) -> None:
        """确保体素已按当前毛坯尺寸构建（引用 state.simVoxel）。"""
        from core.cnc.voxel_sim import ensure_stock_voxel
        self._voxel = ensure_stock_voxel(self._state)

    def _resetVoxel(self) -> None:
        """实体模拟重置：恢复完整毛坯。"""
        if self._voxel is not None:
            self._voxel.reset()
            # 清高度场缓存：下一渲染帧按需重建（完整坯料网格）
            try:
                self._voxel.surf_cache = None
            except Exception:
                pass
        self._voxPrev = None

    def _onStateChanged(self) -> None:
        """向导"实体"勾选变化：同步面板体素引用（构建已由勾选回调完成）。"""
        solid = bool(self._state.showSolidSim)
        if solid == self._lastSolid:
            return
        self._lastSolid = solid
        if solid:
            self._ensureVoxel()
            self._resetVoxel()
        else:
            self._voxel = None
            self._voxPrev = None
            self._state.simVoxel = None

    def showPanel(self, lines) -> None:
        """加载 G 代码并显示面板（不自动播放）。"""
        self._gcode.setPlainText("\n".join(lines))
        self._gcode.setExtraSelections([])
        self._gcode.moveCursor(QTextCursor.Start)
        self._moves = self._buildMoves(lines)
        total = len(self._moves)
        last_line = self._moves[-1][0] if self._moves else 0
        self._slider.setRange(0, max(last_line, 0))
        self._slider.setValue(0)
        self._lineLabel.setText("0 / %d" % len(lines))
        self._state.simLine = -1
        self._state.simPosition = None
        self._state.simCutting = False
        self._state.simPlaying = False
        self._mi = -1
        self._cur = None
        self._target = None
        self._cutting = False
        self._finished = False
        # 实体模拟：勾选时按毛坯尺寸构建/重置
        if self._state.showSolidSim:
            self._ensureVoxel()
            self._resetVoxel()
        self._updatePlayBtn()
        self.show()

    def play(self) -> None:
        if not self._moves:
            return
        # 播放完毕后再点播放：从头开始
        if self._finished:
            self._mi = -1
            self._cur = None
            self._target = None
            self._cutting = False
            self._slider.setValue(0)
            self._lineLabel.setText("0 / %d" % len(self._state.simLines))
            self._state.simLine = -1
            self._state.simPosition = None
            self._state.simCutting = False
            self._finished = False
            if self._state.showSolidSim:
                self._ensureVoxel()
                self._resetVoxel()
        self._state.simPlaying = True
        self._timer.start()
        self._renderTimer.start()
        self._updatePlayBtn()
        self._state.notifyChanged()

    def pause(self) -> None:
        self._state.simPlaying = False
        self._timer.stop()
        self._renderTimer.stop()
        self._state.simAnimA = None
        self._state.simAnimB = None
        self._updatePlayBtn()
        self._state.notifyChanged()

    def toggle(self) -> None:
        if self._state.simPlaying:
            self.pause()
        else:
            self.play()

    def closePanel(self) -> None:
        self.pause()
        self._state.simLine = -1
        self._state.simPosition = None
        self._state.simCutting = False
        self._state.simVoxel = None
        self._voxel = None
        self._voxPrev = None
        self._state.notifyChanged()
        self._mi = -1
        self._cur = None
        self._target = None
        self._finished = False
        self.hide()
        self.closeRequested.emit()

    # ------------------------------------------------------------- 播放
    def _renderTick(self) -> None:
        """渲染定时器：高频（≈60fps）驱动 3D 视口重绘，位置由插值提供。"""
        try:
            self._state.simChanged.emit()
        except Exception:
            pass

    def _onSeek(self, value: int) -> None:
        """拖动进度条：跳到不晚于 value 行的最近移动段（直接到位）。"""
        if not self._moves:
            return
        idx = 0
        for i, (li, tgt, _cut) in enumerate(self._moves):
            if li <= value:
                idx = i
            else:
                break
        li, tgt, _cut = self._moves[idx]
        self._mi = idx
        self._target = list(tgt)
        self._cur = list(tgt)
        self._cutting = _cut
        self._state.simPosition = tuple(tgt)
        self._finished = False
        # 跳转后立即显示目标位置，清空插值区间（避免被旧动画拉回）
        self._state.simAnimA = None
        self._state.simAnimB = None
        self._applyLineInfo(li)
        # 实体模拟：重放到当前位置（只切削段扫掠）
        if self._state.showSolidSim and self._voxel is not None:
            self._ensureVoxel()
            self._voxel.replay(self._moves[:idx + 1], self._solidTool())
            self._voxPrev = tuple(tgt)
            # 跳转后立即同步高度场缓存（渲染帧不再读旧坯料）
            try:
                self._voxel.surf_cache = (
                    int(getattr(self._voxel, "revision", 0)),
                    self._voxel.top_mesh())
            except Exception:
                pass
        self._state.notifyChanged()

    def _tick(self) -> None:
        if not self._moves:
            self.pause()
            return
        speed = _SPEEDS[self._speedCombo.currentIndex()][1]
        step = speed * 0.9   # 每 tick 移动距离(mm)：1x ≈ 0.9mm/50ms
        state = self._state
        solid = state.showSolidSim and self._voxel is not None
        # 本 tick 起点（插值 A 端）：进入 tick 时的当前位置
        self._tickStart = (tuple(self._cur) if self._cur is not None
                           else (0.0, 0.0, 0.0))
        moved = False
        last_li = None
        # 本 tick 的连续切削折线（批量扫掠）：密集精加工路径一个 tick 跨
        # 几十~上百段，逐段 sweep()/高亮会让 tick 超预算 → 跳帧。改为
        # 段内只推进位置，扫掠合并为 tick 末一次 sweep_path，行号/高亮/
        # 进度条也只在 tick 末按最终行刷新一次。
        cut_run: list = []

        def _flush_cut():
            if len(cut_run) >= 2:
                self._voxel.sweep_path(cut_run, self._solidTool())
            del cut_run[:]

        while step > 1e-9:
            if self._cur is None:
                self._cur = [0.0, 0.0, 0.0]   # 起点：世界中心
            if self._target is None:
                self._mi += 1
                if self._mi >= len(self._moves):
                    self._finished = True
                    if solid:
                        _flush_cut()
                    self._finish()
                    return
                li, tgt, cut = self._moves[self._mi]
                self._target = list(tgt)
                self._cutting = cut
                last_li = li
            dx = self._target[0] - self._cur[0]
            dy = self._target[1] - self._cur[1]
            dz = self._target[2] - self._cur[2]
            d = math.hypot(dx, dy, dz)
            if d <= step:
                self._cur[:] = self._target
                self._target = None
                step -= d
            else:
                k = step / d
                self._cur[0] += dx * k
                self._cur[1] += dy * k
                self._cur[2] += dz * k
                step = 0.0
            # 实体模拟：收集连续切削折线（G0 快移只追位置并截断折线）
            if solid:
                if self._voxPrev is None:
                    self._voxPrev = tuple(self._cur)
                elif self._cutting:
                    if not cut_run:
                        cut_run.append(self._voxPrev)
                    cut_run.append(tuple(self._cur))
                    self._voxPrev = tuple(self._cur)
                else:
                    _flush_cut()
                    self._voxPrev = tuple(self._cur)
            moved = True
            state.simPosition = tuple(self._cur)
            # 当前段切削状态（视口据此决定"已走路径"高亮是否推进：
            # 快移接近下刀点时不推进，避免投影到路径中段提前变黄）
            state.simCutting = bool(self._cutting)
        if solid:
            _flush_cut()
        if moved:
            if last_li is not None:
                self._applyLineInfo(last_li)
            # 记录本 tick 插值区间（A→B, 50ms），渲染定时器按时间插值
            state.simAnimA = self._tickStart
            state.simAnimB = tuple(self._cur)
            try:
                import time
                state.simAnimT = time.monotonic()
            except Exception:
                state.simAnimT = 0.0
            # 实体模拟网格提取：GPU 主路径走高度场纹理渲染（draw_stock），
            # 不需要三角网格——仅在 GPU 关闭（软渲染兜底要读网格）时才
            # 在 tick 内提取缓存；本 tick 无切削时 revision 不变直接复用。
            if solid and self._voxel is not None \
                    and not app_settings.gpu_accelerated:
                vs = self._voxel
                cache = getattr(vs, "surf_cache", None)
                rev = int(getattr(vs, "revision", 0))
                if cache is None or cache[0] != rev:
                    try:
                        vs.surf_cache = (rev, vs.top_mesh())
                    except Exception:
                        pass

    def _applyLineInfo(self, idx: int) -> None:
        """更新进度条/行号/G 代码高亮到指定行（不移动刀具位置）。"""
        self._slider.setValue(idx)
        self._lineLabel.setText("%d / %d" % (idx + 1, len(self._state.simLines)))
        self._state.simLine = idx
        self._highlightLine(idx)

    def _highlightLine(self, idx: int) -> None:
        cursor = self._gcode.textCursor()
        cursor.movePosition(QTextCursor.Start)
        cursor.movePosition(QTextCursor.Down, QTextCursor.MoveAnchor, idx)
        cursor.select(QTextCursor.LineUnderCursor)
        extra = QTextEdit.ExtraSelection()
        extra.format.setBackground(QColor("#e8820c"))
        extra.format.setForeground(QColor("#ffffff"))
        extra.format.setFontWeight(QFont.Weight.Bold)
        extra.format.setProperty(QTextFormat.FullWidthSelection, True)
        extra.cursor = cursor
        self._gcode.setExtraSelections([extra])
        self._gcode.setTextCursor(cursor)
        self._gcode.centerCursor()

    def _finish(self) -> None:
        self._state.simPlaying = False
        self._timer.stop()
        self._renderTimer.stop()
        self._state.simAnimA = None
        self._state.simAnimB = None
        self._updatePlayBtn()
        self._state.notifyChanged()

    def _updatePlayBtn(self) -> None:
        self._playBtn.setText("⏸" if self._state.simPlaying else "▶")

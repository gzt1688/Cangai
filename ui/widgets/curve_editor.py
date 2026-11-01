"""PS 风格截面曲线控件：可加/删/拖控制点，实时输出归一化曲线点。

曲线坐标：x∈[0,1] 是截面归一化位置（法向距离 / 半径 / 横向 / 竖向），
y∈[0,1] 是该处的"保留高度比例"（1=完全不减/最高，0=减满/最低）。
端点 x 固定为 0 / 1（只可上下拖动），中间点可增删并沿 x 拖动（不越过邻居）。

交互：
- 左键点空白处：在曲线上加一个点并直接拖动；
- 左键拖动控制点：实时移动（pointsChanged）；
- 右键 / 双击控制点：删除（端点不可删）；
- 选中点后 Delete 删除、↑↓ 微调高度、←→ 微调位置（Shift 步长 ×5）。
"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt, Signal, QSize, QPointF
from PySide6.QtGui import (QPainter, QPen, QColor, QPainterPath, QFont,
                           QPolygonF)
from PySide6.QtWidgets import QWidget

import ui.styles.theme as theme
from core.relief.region_slope import (CURVE_PRESET_NAMES, preset_curve_points,
                                      normalize_curve_points, curve_profile)

_PAD = 4           # 绘图区内边距（px）
_HIT = 8.0         # 控制点命中半径（px）
_R = 4.2           # 控制点绘制半径（px）
_MIN_GAP = 0.004   # 相邻控制点最小 x 间距（归一化）


class CurveEditor(QWidget):
    """截面曲线编辑器（PS 曲线风格）。"""

    pointsChanged = Signal(list)     # 拖动/加点/删点实时触发（list[(x,y)]）
    editingFinished = Signal()       # 一次拖动/操作结束

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pts = [(0.0, 1.0), (1.0, 0.0)]
        self._hover = -1
        self._drag = -1
        self._sel = -1
        self._showNormalRef = True
        self.setMinimumHeight(140)
        self.setMaximumHeight(200)
        self.setMinimumWidth(120)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setToolTip(
            "左键空白处加点并拖动；右键/双击点删除；\n"
            "选中点后 Delete 删除、↑↓ 调高度、←→ 调位置（Shift ×5）")
        self.setStyleSheet("background: %s;" % theme.BG_PANEL)

    # ---------------- 数据 ----------------

    def points(self) -> list:
        return [(float(x), float(y)) for x, y in self._pts]

    def setPoints(self, points, emit: bool = False) -> None:
        pts = normalize_curve_points(points)
        self._pts = [(float(x), float(y)) for x, y in pts]
        self._sel = min(self._sel, len(self._pts) - 1)
        if self._sel < 0:
            self._sel = -1
        self._hover = -1
        self._drag = -1
        self.update()
        if emit:
            self.pointsChanged.emit(self.points())
            self.editingFinished.emit()

    def setPreset(self, name: str, emit: bool = True) -> None:
        self.setPoints(preset_curve_points(name), emit=emit)

    def flip(self, emit: bool = True) -> None:
        """上下翻转（保留高度 1-y）。"""
        self.setPoints([(x, 1.0 - y) for x, y in self._pts], emit=emit)

    def reset(self, emit: bool = True) -> None:
        self.setPoints([(0.0, 1.0), (1.0, 0.0)], emit=emit)

    def isLinear(self) -> bool:
        pts = normalize_curve_points(self._pts)
        return (pts.shape[0] == 2
                and abs(pts[0, 1] - 1.0) < 1e-6
                and abs(pts[1, 1]) < 1e-6)

    # ---------------- 几何 ----------------

    def _plot_rect(self):
        return self.rect().adjusted(_PAD, _PAD, -_PAD, -_PAD)

    def _to_px(self, x: float, y: float) -> QPointF:
        r = self._plot_rect()
        return QPointF(r.left() + x * r.width(),
                       r.bottom() - y * r.height())

    def _to_data(self, pos) -> tuple:
        r = self._plot_rect()
        x = (pos.x() - r.left()) / max(1, r.width())
        y = (r.bottom() - pos.y()) / max(1, r.height())
        return float(np.clip(x, 0.0, 1.0)), float(np.clip(y, 0.0, 1.0))

    def _curve_y(self, x: float) -> float:
        return float(curve_profile(np.array([x]), self._pts)[0])

    def _hit(self, pos) -> int:
        """返回命中的控制点索引（无则 -1）。"""
        best, best_d = -1, _HIT
        for i, (x, y) in enumerate(self._pts):
            p = self._to_px(x, y)
            d = ((p.x() - pos.x()) ** 2 + (p.y() - pos.y()) ** 2) ** 0.5
            if d <= best_d:
                best, best_d = i, d
        return best

    # ---------------- 绘制 ----------------

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = self._plot_rect()

        # 背景与网格（4×4）
        p.fillRect(r, QColor("#fbfdfe"))
        p.setPen(QPen(QColor("#e8eff1"), 1))
        for i in range(1, 4):
            x = r.left() + r.width() * i / 4.0
            y = r.top() + r.height() * i / 4.0
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))

        # 线性参考（y = 1-x，左上 → 右下），细虚线
        if self._showNormalRef:
            p.setPen(QPen(QColor("#d3dde0"), 1, Qt.PenStyle.DashLine))
            p.drawLine(self._to_px(0, 1), self._to_px(1, 0))

        # 曲线与填充
        xs = np.linspace(0.0, 1.0, 129)
        ys = curve_profile(xs, self._pts)
        poly = QPolygonF([self._to_px(float(x), float(y))
                          for x, y in zip(xs, ys)])
        path = QPainterPath()
        path.moveTo(self._to_px(0, 0))
        for pt in poly:
            path.lineTo(pt)
        path.lineTo(self._to_px(1, 0))
        path.closeSubpath()
        p.setPen(Qt.PenStyle.NoPen)
        fill = QColor(theme.PRIMARY)
        fill.setAlpha(26)
        p.fillPath(path, fill)

        curve_path = QPainterPath()
        curve_path.moveTo(poly[0])
        for k in range(1, poly.count()):
            curve_path.lineTo(poly[k])
        p.setPen(QPen(QColor(theme.PRIMARY_DARK), 2))
        p.drawPath(curve_path)

        # 控制点
        for i, (x, y) in enumerate(self._pts):
            c = self._to_px(x, y)
            radius = _R
            if i == self._sel:
                radius += 1.6
                p.setPen(QPen(QColor(theme.PRIMARY_DARK), 1.6))
                p.setBrush(QColor(theme.PRIMARY))
            elif i == self._hover:
                radius += 1.0
                p.setPen(QPen(QColor(theme.PRIMARY), 1.6))
                p.setBrush(QColor(theme.BG_PANEL))
            else:
                p.setPen(QPen(QColor(theme.PRIMARY_DARK), 1.4))
                p.setBrush(QColor(theme.BG_PANEL))
            p.drawEllipse(c, radius, radius)

        # 拖动时右上角显示坐标
        if self._drag >= 0 and 0 <= self._drag < len(self._pts):
            x, y = self._pts[self._drag]
            p.setPen(QPen(QColor(theme.TEXT_DIM)))
            f = QFont(self.font())
            f.setPointSizeF(max(7.0, f.pointSizeF() - 1.0))
            p.setFont(f)
            txt = "%.2f  %d%%" % (x, round(y * 100))
            p.drawText(r.adjusted(2, 1, -3, -2),
                       int(Qt.AlignmentFlag.AlignRight
                           | Qt.AlignmentFlag.AlignTop), txt)

        p.setPen(QPen(QColor("#cbd5d8"), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(r)

    def sizeHint(self) -> QSize:
        return QSize(240, 160)

    # ---------------- 鼠标 ----------------

    def mousePressEvent(self, ev) -> None:
        pos = ev.position()
        if ev.button() == Qt.MouseButton.RightButton:
            i = self._hit(pos)
            if 0 < i < len(self._pts) - 1:
                del self._pts[i]
                self._sel = -1
                self._drag = -1
                self._hover = -1
                self.update()
                self.pointsChanged.emit(self.points())
                self.editingFinished.emit()
            return
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        i = self._hit(pos)
        if i < 0:
            # 在曲线上加点：x 取点击处，y 取当前曲线值（落点在线上）
            x0, _ = self._to_data(pos)
            x0 = float(np.clip(x0, _MIN_GAP, 1.0 - _MIN_GAP))
            y0 = float(np.clip(self._curve_y(x0), 0.0, 1.0))
            self._pts.append((x0, y0))
            self._pts.sort(key=lambda t: t[0])
            i = next(k for k, t in enumerate(self._pts) if t[0] == x0)
            self._sel = i
            self._drag = i
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self.update()
            self.pointsChanged.emit(self.points())
            return
        self._sel = i
        self._drag = i
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self.update()

    def mouseMoveEvent(self, ev) -> None:
        pos = ev.position()
        if self._drag >= 0:
            x, y = self._to_data(pos)
            n = len(self._pts)
            if self._drag == 0:
                x = 0.0
            elif self._drag == n - 1:
                x = 1.0
            else:
                lo = self._pts[self._drag - 1][0] + _MIN_GAP
                hi = self._pts[self._drag + 1][0] - _MIN_GAP
                x = float(np.clip(x, lo, hi))
            self._pts[self._drag] = (x, y)
            self.update()
            self.pointsChanged.emit(self.points())
            return
        hit = self._hit(pos)
        if hit != self._hover:
            self._hover = hit
            self.setCursor(Qt.CursorShape.SizeAllCursor if hit >= 0
                           else Qt.CursorShape.CrossCursor)
            self.update()

    def mouseReleaseEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton and self._drag >= 0:
            self._drag = -1
            self.update()
            self.editingFinished.emit()

    def mouseDoubleClickEvent(self, ev) -> None:
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        i = self._hit(ev.position())
        if 0 < i < len(self._pts) - 1:
            del self._pts[i]
            self._sel = -1
            self._drag = -1
            self._hover = -1
            self.update()
            self.pointsChanged.emit(self.points())
            self.editingFinished.emit()
        else:
            super().mouseDoubleClickEvent(ev)

    def leaveEvent(self, ev) -> None:
        if self._hover != -1:
            self._hover = -1
            self.update()
        super().leaveEvent(ev)

    # ---------------- 键盘 ----------------

    def keyPressEvent(self, ev) -> None:
        i = self._sel
        if i < 0 or i >= len(self._pts):
            super().keyPressEvent(ev)
            return
        key = ev.key()
        step = 0.1 if ev.modifiers() & Qt.KeyboardModifier.ShiftModifier else 0.02
        x, y = self._pts[i]
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if 0 < i < len(self._pts) - 1:
                del self._pts[i]
                self._sel = -1
                self._hover = -1
                self.update()
                self.pointsChanged.emit(self.points())
                self.editingFinished.emit()
            return
        if key == Qt.Key.Key_Up:
            y = min(1.0, y + step)
        elif key == Qt.Key.Key_Down:
            y = max(0.0, y - step)
        elif key == Qt.Key.Key_Left and 0 < i < len(self._pts) - 1:
            x = max(self._pts[i - 1][0] + _MIN_GAP, x - step)
        elif key == Qt.Key.Key_Right and 0 < i < len(self._pts) - 1:
            x = min(self._pts[i + 1][0] - _MIN_GAP, x + step)
        else:
            super().keyPressEvent(ev)
            return
        self._pts[i] = (x, float(np.clip(y, 0.0, 1.0)))
        self.update()
        self.pointsChanged.emit(self.points())
        self.editingFinished.emit()

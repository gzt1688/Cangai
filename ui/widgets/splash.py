# -*- coding: utf-8 -*-
"""启动加载页。

主窗口构造与全部重模块（字体 / 3D 内核 / OCP / 几何 / CNC）预热期间
显示此页面并汇报进度；全部就绪后才允许进入主界面，避免用户过早操作
尚未加载的模块导致卡死。
"""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, QTimer, QPointF, QRectF
from PySide6.QtGui import (QColor, QFont, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (QApplication, QLabel, QProgressBar,
                               QVBoxLayout, QWidget)

from i18n import tr
from resource_path import resource_path
from version import APP_VERSION, APP_VERSION_DATE


class SplashScreen(QWidget):
    """无边框加载页：Logo + 标题 + 进度条 + 当前加载项。"""

    _FLY_H = 80           # 蜻蜓贴图绘制高度(px)

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(Qt.FramelessWindowHint |
                            Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(560, 384)
        self._allowClose = False
        self._centerOnScreen()

        # 蜻蜓停在进度条已填充部分的右端，随进度推移；振翅相位驱动扇翅
        self._flyPhase = 0.0
        self._flyTimer = QTimer(self)
        self._flyTimer.setInterval(33)      # ≈30fps
        self._flyTimer.timeout.connect(self._tickFly)

        # 写实蜻蜓贴图（透明背景 PNG），停在进度头上方随进度前移
        self._flyPix = QPixmap(resource_path("resources/splash_dragonfly.png"))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(48, 34, 48, 26)
        layout.setSpacing(0)

        logo = QLabel(self)
        logo.setAlignment(Qt.AlignCenter)
        logo.setFixedHeight(60)        # 只固定高度（宽度铺满），靠 AlignCenter 居中且不影响布局
        icon = QPixmap(resource_path("icons/Cangai.ico"))
        if not icon.isNull():
            logo.setPixmap(icon.scaled(60, 60, Qt.KeepAspectRatio,
                                       Qt.SmoothTransformation))
        layout.addWidget(logo)
        layout.addSpacing(12)

        title = QLabel(tr("Cangai 浮雕设计"), self)
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QFont("Microsoft YaHei", 15, QFont.Bold))
        title.setStyleSheet("color:#1a1a1a;")
        title.setFixedHeight(34)   # 固定高度，锁定进度条上方总布局
        layout.addWidget(title)
        layout.addSpacing(4)

        self._status = QLabel(tr("正在初始化…"), self)
        self._status.setAlignment(Qt.AlignCenter)
        self._status.setStyleSheet("color:#5f5f5f;")
        # 锁定为一行固定高度：加载文本变化时不再改变布局，进度条位置保持不动
        self._status.setWordWrap(False)
        self._status.setFixedHeight(max(self._status.fontMetrics().height() + 6, 22))
        layout.addWidget(self._status)
        layout.addSpacing(82)      # 留出蜻蜓停在进度条上时的高度

        self._bar = QProgressBar(self)
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(6)
        self._bar.setStyleSheet(
            "QProgressBar{border:none;border-radius:3px;background:#e8e8e8;}"
            "QProgressBar::chunk{border-radius:3px;"
            "background:qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            "stop:0 #15b6c9,stop:1 #7fdce8);}")
        layout.addWidget(self._bar)
        layout.addSpacing(8)

        self._percent = QLabel("0%", self)
        self._percent.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._percent.setStyleSheet("color:#8a8a8a;font-size:9pt;")
        self._percent.setFixedHeight(20)
        layout.addWidget(self._percent)
        layout.addStretch(1)

        hint = QLabel(tr("正在加载全部功能模块，请稍候…"), self)
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet("color:#a0a0a0;font-size:9pt;")
        hint.setFixedHeight(20)
        layout.addWidget(hint)
        layout.addSpacing(6)

        ver = QLabel(
            f"{tr('版本')} {APP_VERSION}　·　{tr('更新日期')} {APP_VERSION_DATE}",
            self)
        ver.setAlignment(Qt.AlignCenter)
        ver.setStyleSheet("color:#b0b0b0;font-size:9pt;")
        layout.addWidget(ver)

    def _centerOnScreen(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.move(geo.center().x() - self.width() // 2,
                  geo.center().y() - self.height() // 2)

    def setLoadProgress(self, value: int, text: str = "") -> None:
        """更新进度与当前加载项（由主窗口后台预载线程转发）。"""
        value = max(0, min(100, int(value)))
        self._bar.setValue(value)
        self._percent.setText(f"{value}%")
        if text:
            self._status.setText(text)
        self.update()      # 蜻蜓随已填充部分右端立即前移

    def finish(self, window=None) -> None:
        """主窗口显示后关闭加载页。"""
        self._allowClose = True
        self.close()

    def closeEvent(self, event) -> None:
        # 加载未完成时禁止关闭，避免主窗口尚未显示而进程空转
        if self._allowClose:
            self._flyTimer.stop()
            super().closeEvent(event)
        else:
            event.ignore()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 蜻蜓静止显示，仅随进度移动，无需相位动画定时器

    def _tickFly(self) -> None:
        """推进相位；蜻蜓水平位置在 paintEvent 中由进度条当前值决定。"""
        self._flyPhase += 0.08
        self.update()

    def _drawDragonfly(self, painter: QPainter, cx: float, cy: float) -> None:
        """把蜻蜓贴图静止停在进度条右端，仅随进度前移（不摇晃）。"""
        if self._flyPix.isNull():
            return
        dh = self._FLY_H
        dw = int(dh * self._flyPix.width() / self._flyPix.height())
        painter.save()
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        target = QRectF(cx - dw / 2.0, cy - dh, dw, dh)   # 底边贴进度条上沿
        source = QRectF(0, 0, self._flyPix.width(), self._flyPix.height())
        painter.drawPixmap(target, self._flyPix, source)
        painter.restore()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect().adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(rect, 10, 10)
        painter.fillPath(path, QColor("#ffffff"))
        painter.setPen(QPen(QColor("#dcdcdc"), 1))
        painter.drawPath(path)

        # 顶部主题色横条（裁剪到圆角内）
        painter.save()
        painter.setClipPath(path)
        grad = QLinearGradient(0, 0, self.width(), 0)
        grad.setColorAt(0.0, QColor("#15b6c9"))
        grad.setColorAt(1.0, QColor("#7fdce8"))
        painter.fillRect(0, 0, self.width(), 4, grad)
        painter.restore()

        # 蜻蜓停在进度条已填充部分的右端，像停在进度条上随进度前移
        geo = self._bar.geometry()
        if geo.width() > 0:
            rng = max(1, self._bar.maximum() - self._bar.minimum())
            frac = (self._bar.value() - self._bar.minimum()) / rng
            cx = geo.left() + geo.width() * frac
            dw = self._FLY_H * self._flyPix.width() / max(1, self._flyPix.height())
            margin = dw * 0.5 + 6
            cx = min(max(cx, margin), self.width() - margin)   # 防止贴边被裁掉
            cy = geo.top() + 2.0      # 贴图底边轻贴进度条上沿
            painter.setClipPath(path)
            self._drawDragonfly(painter, cx, cy)

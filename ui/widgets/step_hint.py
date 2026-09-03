"""视图左下角悬浮步骤/结果提示条（无背景，透明鼠标穿透）。

- 步骤行：跟随命令的 promptChanged（操作步骤引导）；
- 结果行：跟随 CommandManager.notifyResult（操作结果反馈），
  最后一条新结果到达后 15 秒自动淡出；有新结果到达时立刻替换并重新计时。"""
from __future__ import annotations
import html

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QLabel

_STEP_COLOR = "#333333"
_RESULT_COLOR = "#0e8a99"
_RESULT_FADEOUT_MS = 15 * 1000   # 结果行：最后一条新结果到达后 15 秒淡出


class StepHintOverlay(QLabel):
    """无背景悬浮提示，用于操作步骤引导与结果反馈。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._step: str = ""
        self._result: str = ""
        self._resultTimer = QTimer(self)
        self._resultTimer.setSingleShot(True)
        self._resultTimer.timeout.connect(self._clearResult)
        self.setWordWrap(False)
        self.setAlignment(Qt.AlignLeft | Qt.AlignBottom)
        # 不拦截鼠标事件，悬浮在画布上
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setStyleSheet(
            "background: transparent; font-size: 13px;"
            "padding: 4px 8px;")
        self.setTextFormat(Qt.RichText)
        self._render()

    # ---------- 对外接口 ----------

    def setStep(self, text) -> None:
        """设置步骤引导文本（命令 promptChanged）。"""
        self._step = text or ""
        self._render()

    def setResult(self, text) -> None:
        """设置操作结果文本：
        - 新结果到达时立即替换显示；
        - 每次收到新结果重置倒计时（最后一条 15 秒后淡出）。"""
        self._result = text or ""
        self._render()
        self._resultTimer.start(_RESULT_FADEOUT_MS)

    def clearResult(self) -> None:
        self._result = ""
        self._resultTimer.stop()
        self._render()

    # ---------- 内部 ----------

    def _clearResult(self) -> None:
        self._result = ""
        self._render()

    def _render(self) -> None:
        parts = []
        if self._step:
            parts.append("<span style='color:%s;'>%s</span>"
                         % (_STEP_COLOR, html.escape(self._step)))
        if self._result:
            parts.append("<span style='color:%s;'>%s</span>"
                         % (_RESULT_COLOR, html.escape(self._result)))
        self.setText("<br/>".join(parts))

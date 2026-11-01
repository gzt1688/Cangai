"""AI 对话面板：视口右侧「Ai」按钮弹出。

结构（自上而下）：
- 标题栏：模型名称显示 + 清除会话 + 设置(⚙) + 关闭
- 对话记录区：用户/助手气泡 + 工具执行状态行
- 输入区：多行输入框（Enter 发送 / Shift+Enter 换行）
- 按钮行：上传图片 | 视口截图 | 发送

对话循环在工作线程跑 OpenAI 兼容 API（core.ai.llm_client），tool-calling
经主线程执行 core.ai.tools 注册表——与外部 MCP 客户端共用同一套工具。
"""
from __future__ import annotations

import json
import queue
import uuid
from typing import List, Optional

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QEvent, QSize
from PySide6.QtGui import QPixmap, QFontMetrics, QIcon
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QPushButton, QTextEdit, QScrollArea, QFrame,
                               QFileDialog, QMessageBox)

import ui.styles.theme as theme
from resource_path import resource_path
from i18n import tr, languageChanged

from core.ai import llm_client
from core.ai.tools import toolSchemas, invoke, SYSTEM_PROMPT

# 标题栏模型名 chip 最大宽度（面板固定 340：标题~50 + chip + 三按钮~70 + 边距）
_MODEL_CHIP_MAXW = 150


# ---------------------------------------------------------------------------
# 工作线程：对话 + 工具循环
# ---------------------------------------------------------------------------

class _ChatWorker(QThread):
    """执行 chat/completions 多轮工具循环；工具执行请求经主线程回投。"""

    toolRequest = Signal(str, str)         # (call_id, "name|argsJson")
    statusLine = Signal(str)               # 状态行（工具执行提示）
    finishedOk = Signal(str, list)         # (最终文本, 新增消息列表)
    finishedErr = Signal(str)              # 错误信息

    MAX_TOOL_ROUNDS = 12
    MAX_RESULT_CHARS = 6000                # 单个工具结果文本截断长度

    def __init__(self, cfg: dict, messages: list, host_images: list,
                 parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._messages = messages          # OpenAI 格式（不含 system）
        self._hostImages = host_images     # 本回合附到用户消息的 data URL
        self._results: queue.Queue = queue.Queue()
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def putResult(self, call_id: str, content: str) -> None:
        self._results.put((call_id, content))

    def run(self) -> None:
        try:
            text, newMsgs = self._loop()
            self.finishedOk.emit(text, newMsgs)
        except llm_client.LLMError as e:
            self.finishedErr.emit(str(e))
        except Exception as e:
            self.finishedErr.emit(f"{type(e).__name__}: {e}")

    def _loop(self):
        startLen = len(self._messages)
        msgs = list(self._messages)        # 拷贝：不回写面板历史
        # 本回合图片附件 → 多模态用户消息
        if self._hostImages and msgs and msgs[-1].get("role") == "user":
            userText = msgs[-1]["content"]
            content = [{"type": "text", "text": userText}]
            for url in self._hostImages:
                content.append(
                    {"type": "image_url", "image_url": {"url": url}})
            msgs[-1] = {"role": "user", "content": content}
        for _ in range(self.MAX_TOOL_ROUNDS):
            if self._cancelled:
                return tr("（已取消）"), []
            reply = llm_client.chatOnce(self._cfg,
                                        [{"role": "system",
                                          "content": SYSTEM_PROMPT}] + msgs,
                                        toolSchemas())
            calls = reply.get("tool_calls") or []
            if not calls:
                text = reply.get("content") or tr("（模型未返回内容）")
                msgs.append({"role": "assistant", "content": text})
                return text, msgs[startLen:]
            # 记录 assistant 的工具调用消息
            msgs.append({"role": "assistant", "content": reply.get("content"),
                         "tool_calls": calls})
            for call in calls:
                if self._cancelled:
                    return tr("（已取消）"), []
                fn = call.get("function", {})
                name = fn.get("name", "")
                argsStr = fn.get("arguments") or "{}"
                self.statusLine.emit(tr("执行工具") + f"：{name}")
                cid = call.get("id") or name
                self.toolRequest.emit(cid, f"{name}|{argsStr}")
                content = self._awaitResult(cid)
                msgs.append({"role": "tool", "tool_call_id": cid,
                             "content": content})
        return tr("（工具调用轮次达到上限，请简化请求或分步进行）"), []

    def _awaitResult(self, cid: str):
        """等主线程工具结果；图片类结果转多模态 content parts，
        普通结果截断为纯文本。"""
        while True:
            try:
                gotId, raw = self._results.get(timeout=120)
            except queue.Empty:
                return json.dumps({"error": "工具执行超时"},
                                  ensure_ascii=False)
            if gotId == cid:
                break
        try:
            data = json.loads(raw)
        except ValueError:
            return raw
        if isinstance(data, dict):
            b64 = None
            mime = "image/png"
            if data.get("image_png_b64"):
                b64 = data["image_png_b64"]
            elif data.get("image_b64"):
                b64 = data["image_b64"]
                mime = data.get("mime", "image/png")
            if b64:
                rest = {k: v for k, v in data.items()
                        if k not in ("image_png_b64", "image_b64", "mime")}
                text = json.dumps(rest, ensure_ascii=False, default=str)
                if len(text) > self.MAX_RESULT_CHARS:
                    text = text[:self.MAX_RESULT_CHARS] + "...(截断)"
                return [{"type": "text", "text": text},
                        {"type": "image_url",
                         "image_url": {"url": llm_client.b64ToDataUrl(
                             b64, mime)}}]
        text = raw if isinstance(raw, str) else json.dumps(
            data, ensure_ascii=False, default=str)
        if len(text) > self.MAX_RESULT_CHARS:
            text = text[:self.MAX_RESULT_CHARS] + "...(截断)"
        return text


# ---------------------------------------------------------------------------
# 面板
# ---------------------------------------------------------------------------

class AiChatPanel(QWidget):
    closed = Signal()

    def __init__(self, host=None, parent=None) -> None:
        super().__init__(parent)
        self._host = host                   # MainWindow（runOnMainThread 等）
        self._objectName = "AiChatPanel"
        self.setObjectName(self._objectName)
        # 普通 QWidget 不会自动按 QSS 画背景（只有 promoted QFrame 才会）：
        # 显式开启 StyledBackground，否则面板在视口上“镂空”透明
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedWidth(340)
        self.setStyleSheet(
            "QWidget#AiChatPanel { background:%s; border:1px solid %s;"
            " border-radius:4px; }" % (theme.BG_PANEL, theme.BORDER))

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 8)
        root.setSpacing(6)

        # ---------------- 标题栏：模型名称 + 按钮 ----------------
        bar = QHBoxLayout()
        bar.setSpacing(4)
        self._titleLabel = QLabel(tr("AI 助手"))
        self._titleLabel.setStyleSheet(
            "font-weight:bold; color:%s;" % theme.PRIMARY_DARK)
        bar.addWidget(self._titleLabel)
        # 模型名 chip：限宽 + 省略号，避免长名把右侧三个按钮挤出面板边界被遮拦
        self._modelLabel = QLabel("")
        self._modelLabel.setMaximumWidth(_MODEL_CHIP_MAXW)
        self._modelLabel.setStyleSheet(
            "color:#888; font-size:11px; padding:1px 6px;"
            " background:#f0f0f0; border-radius:3px;")
        bar.addWidget(self._modelLabel)
        bar.addStretch(1)
        self._btnClear = self._toolBtn("", tr("清除会话"))
        self._btnClear.setIcon(QIcon(resource_path("icons/broom.svg")))
        self._btnClear.setIconSize(QSize(16, 16))
        self._btnClear.clicked.connect(self._onClear)
        bar.addWidget(self._btnClear)
        self._btnCfg = self._toolBtn("⚙", tr("模型设置"))
        self._btnCfg.clicked.connect(self._onSettings)
        bar.addWidget(self._btnCfg)
        self._btnClose = self._toolBtn("×", tr("关闭"))
        self._btnClose.setStyleSheet(
            "QPushButton { background:transparent; border:none; font-weight:bold;"
            " padding:0px; font-size:14px; }"
            "QPushButton:hover { color:#c0392b; }")
        self._btnClose.clicked.connect(lambda: self.closed.emit())
        bar.addWidget(self._btnClose)
        root.addLayout(bar)

        # ---------------- 对话记录 ----------------
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setAttribute(Qt.WA_StyledBackground, True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(
            "QScrollArea { background:transparent; border:none; }"
            "QScrollArea > QWidget > QWidget { background:transparent; }")
        self._histHolder = QFrame()
        self._histHolder.setAttribute(Qt.WA_StyledBackground, True)
        self._histHolder.setStyleSheet(
            "QFrame { background:transparent; border:none; }")
        self._histLayout = QVBoxLayout(self._histHolder)
        self._histLayout.setContentsMargins(0, 0, 0, 0)
        self._histLayout.setSpacing(6)
        self._histLayout.addStretch(1)
        self._scroll.setWidget(self._histHolder)
        root.addWidget(self._scroll, 1)

        # ---------------- 附件缩略图行（有图时显示） ----------------
        self._attachRow = QHBoxLayout()
        self._attachRow.setSpacing(4)
        root.addLayout(self._attachRow)

        # ---------------- 输入框 ----------------
        self._input = QTextEdit()
        self._input.setFixedHeight(64)
        self._input.setPlaceholderText(tr("输入消息，Enter 发送，Shift+Enter 换行"))
        self._input.setStyleSheet(
            "QTextEdit { background:white; border:1px solid %s;"
            " border-radius:4px; padding:4px; }" % theme.BORDER)
        self._input.installEventFilter(self)
        root.addWidget(self._input)

        # ---------------- 按钮行：上传图片 / 截图 / 发送 ----------------
        btnRow = QHBoxLayout()
        btnRow.setSpacing(6)
        self._btnImage = QPushButton(tr("上传图片"))
        self._btnImage.clicked.connect(self._onPickImage)
        btnRow.addWidget(self._btnImage)
        self._btnShot = QPushButton(tr("视口截图"))
        self._btnShot.setToolTip(tr("截取当前视口画面一并发给 AI"))
        self._btnShot.clicked.connect(self._onPickShot)
        btnRow.addWidget(self._btnShot)
        btnRow.addStretch(1)
        self._btnCancel = QPushButton(tr("取消"))
        self._btnCancel.hide()
        self._btnCancel.clicked.connect(self._onCancel)
        btnRow.addWidget(self._btnCancel)
        self._btnSend = QPushButton(tr("发送"))
        self._btnSend.setDefault(True)
        self._btnSend.setStyleSheet(
            "QPushButton { background:%s; color:white; border:none;"
            " border-radius:4px; padding:5px 16px; font-weight:bold; }"
            "QPushButton:hover { background:%s; }"
            "QPushButton:disabled { background:#bbb; }"
            % (theme.PRIMARY, theme.PRIMARY_DARK))
        self._btnSend.clicked.connect(self._onSend)
        btnRow.addWidget(self._btnSend)
        root.addLayout(btnRow)

        # ---------------- 状态 ----------------
        self._messages: List[dict] = []     # OpenAI 历史（不含 system）
        self._pendingImages: List[dict] = []  # {"path","dataUrl","thumb"}
        self._worker: Optional[_ChatWorker] = None
        self._busy = False

        self.refreshModelLabel()
        self._appendWelcome()
        languageChanged().connect(self.retranslateUi)

    # ---------------- 小工具 ----------------

    @staticmethod
    def _toolBtn(text: str, tip: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setToolTip(tip)
        btn.setFixedSize(24, 22)
        btn.setCursor(Qt.PointingHandCursor)
        # 关键：显式 padding:0 覆盖全局 QSS 的 QPushButton padding:3px 10px，
        # 否则小按钮内容区被左右内边距挤没，图标/文字显示不全
        btn.setStyleSheet(
            "QPushButton { background:transparent; border:none; font-weight:bold;"
            " padding:0px; font-size:14px; }"
            "QPushButton:hover { color:%s; }" % theme.PRIMARY_DARK)
        return btn

    def refreshModelLabel(self) -> None:
        cfg = llm_client.loadConfig()
        self._cfg = cfg
        name = llm_client.modelDisplayName(cfg["model"])
        # 按 chip 可用宽（扣除左右 padding）省略号截断，tooltip 保留全名
        avail = max(30, _MODEL_CHIP_MAXW - 14)
        fm = self._modelLabel.fontMetrics()
        self._modelLabel.setText(
            fm.elidedText(name, Qt.ElideRight, avail))
        self._modelLabel.setToolTip(f"{name}\n{cfg['model']}\n{cfg['base_url']}")

    def retranslateUi(self) -> None:
        self._titleLabel.setText(tr("AI 助手"))
        self._btnClear.setToolTip(tr("清除会话"))
        self._btnCfg.setToolTip(tr("模型设置"))
        self._btnClose.setToolTip(tr("关闭"))
        self._btnImage.setText(tr("上传图片"))
        self._btnShot.setText(tr("视口截图"))
        self._btnSend.setText(tr("发送"))
        self._btnCancel.setText(tr("取消"))
        self._input.setPlaceholderText(
            tr("输入消息，Enter 发送，Shift+Enter 换行"))

    # ---------------- 气泡渲染 ----------------

    def _addBubble(self, who: str, text: str) -> None:
        isUser = who == "user"
        lbl = QLabel()
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lbl.setStyleSheet(
            "QLabel { background:%s; color:#222; border-radius:6px;"
            " padding:6px 8px; font-size:12px; }"
            % ("#d8ecff" if isUser else "#ffffff"))
        lbl.setText(text)
        idx = self._histLayout.count() - 1     # stretch 前插入
        self._histLayout.insertWidget(idx, lbl)
        QTimer.singleShot(0, self._scrollToEnd)

    def _addStatus(self, text: str) -> None:
        lbl = QLabel("◌ " + text)
        lbl.setStyleSheet("color:#999; font-size:11px; padding:0 4px;")
        self._histLayout.insertWidget(self._histLayout.count() - 1, lbl)
        QTimer.singleShot(0, self._scrollToEnd)

    def _scrollToEnd(self) -> None:
        self._scroll.verticalScrollBar().setValue(
            self._scroll.verticalScrollBar().maximum())

    def _appendWelcome(self) -> None:
        self._addBubble("ai", tr(
            "你好！我是 Cangai AI 助手，可以直接操作当前文档：查询/创建几何、"
            "执行软件命令、运行脚本、导入导出文件、查看视口画面。\n"
            "首次使用请先点 ⚙ 配置大模型服务地址与 API Key。"))

    # ---------------- 附件（图片） ----------------

    def _onPickImage(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, tr("上传图片"), "",
            tr("图片文件") + " (*.png *.jpg *.jpeg *.bmp *.webp *.gif)")
        if not path:
            return
        self._addAttachment(path)

    def _onPickShot(self) -> None:
        """抓当前活动视口 → 存临时 PNG → 作为附件。"""
        if self._host is None:
            return
        import os, tempfile
        view = getattr(self._host, "_viewport3D", None) \
            if getattr(self._host, "_active3D", False) \
            else getattr(self._host, "_view", None)
        if view is None:
            return
        pix = view.grab()
        path = os.path.join(tempfile.gettempdir(),
                            f"cangai_shot_{uuid.uuid4().hex[:8]}.png")
        if not pix.save(path):
            return
        self._addAttachment(path)

    def _addAttachment(self, path: str) -> None:
        try:
            dataUrl = llm_client.compressImageToDataUrl(path)
        except llm_client.LLMError as e:
            QMessageBox.warning(self, tr("AI 助手"), str(e))
            return
        pm = QPixmap(path)
        thumb = QLabel()
        thumb.setFixedSize(40, 40)
        thumb.setStyleSheet(
            "border:1px solid %s; border-radius:4px; background:white;"
            % theme.BORDER)
        thumb.setPixmap(pm.scaled(38, 38, Qt.KeepAspectRatio,
                                  Qt.SmoothTransformation))
        thumb.setToolTip(path)
        att = {"path": path, "dataUrl": dataUrl, "thumb": thumb}
        self._pendingImages.append(att)
        self._attachRow.addWidget(thumb)
        thumb.show()

    def _clearAttachments(self) -> None:
        for att in self._pendingImages:
            att["thumb"].setParent(None)
        self._pendingImages = []

    # ---------------- 发送 / 取消 / 清除 ----------------

    def eventFilter(self, obj, event) -> bool:
        # 输入框：Enter 发送，Shift+Enter 换行
        if obj is self._input and event.type() == QEvent.KeyPress and \
                event.key() in (Qt.Key_Return, Qt.Key_Enter) and \
                not (event.modifiers() & Qt.ShiftModifier):
            self._onSend()
            return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event) -> None:
        # Enter 发送（输入框自己有焦点时由 eventFilter 处理，这里兜底面板级）
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and \
                not (event.modifiers() & Qt.ShiftModifier):
            self._onSend()
            return
        super().keyPressEvent(event)

    def _onSend(self) -> None:
        if self._busy:
            return
        text = self._input.toPlainText().strip()
        if not text and not self._pendingImages:
            return
        if not text:
            text = tr("请分析这张图片")
        if not self._cfg.get("api_key"):
            if QMessageBox.question(
                    self, tr("AI 助手"),
                    tr("尚未配置 API Key，现在去设置吗？")) == \
                    QMessageBox.Yes:
                self._onSettings()
            return
        self._addBubble("user", text)
        self._messages.append({"role": "user", "content": text})
        self._input.clear()
        imgs = [att["dataUrl"] for att in self._pendingImages]
        self._clearAttachments()
        self._trimHistory()
        self._startWorker(imgs)

    def _trimHistory(self) -> None:
        # 只保留最近 40 条（system 每次请求现拼，不入历史）
        if len(self._messages) > 40:
            self._messages = self._messages[-40:]

    def _startWorker(self, imgs: list) -> None:
        self._busy = True
        self._btnSend.setEnabled(False)
        self._btnCancel.show()
        self._addStatus(tr("思考中…"))
        self._worker = _ChatWorker(dict(self._cfg), list(self._messages),
                                   imgs, self)
        self._worker.toolRequest.connect(self._onToolRequest)
        self._worker.statusLine.connect(self._addStatus)
        self._worker.finishedOk.connect(self._onWorkerDone)
        self._worker.finishedErr.connect(self._onWorkerError)
        self._worker.start()

    def _onCancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            # 排空积压的工具请求，防止工作线程等结果
            self._addStatus(tr("正在取消…"))

    def _onToolRequest(self, callId: str, payload: str) -> None:
        """跨线程信号：已在主线程。执行工具后把结果回投工作线程。"""
        name, _, argsJson = payload.partition("|")
        try:
            result = invoke(name, argsJson, timeout=25.0)
            content = json.dumps(result, ensure_ascii=False, default=str)
        except Exception as e:
            content = json.dumps({"error": str(e)}, ensure_ascii=False)
        if self._worker is not None:
            self._worker.putResult(callId, content)

    def _onWorkerDone(self, text: str, newMsgs: list) -> None:
        if newMsgs:
            self._messages.extend(newMsgs)
        if text:
            self._addBubble("ai", text)
        self._finishWorker()

    def _onWorkerError(self, err: str) -> None:
        self._addBubble("ai", tr("出错：") + err)
        self._finishWorker()

    def _finishWorker(self) -> None:
        self._busy = False
        self._btnSend.setEnabled(True)
        self._btnCancel.hide()
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None

    # ---------------- 设置 / 清除 ----------------

    def _onSettings(self) -> None:
        """⚙ 打开主设置对话框并直达“AI”页。"""
        host = self._host
        if host is not None and hasattr(host, "openAiSettings"):
            host.openAiSettings()
            self.refreshModelLabel()
            return
        QMessageBox.information(
            self, tr("AI 助手"),
            tr("请在 设置 → AI 页 配置大模型服务与 API Key"))

    def _onClear(self) -> None:
        self._messages = []
        while self._histLayout.count() > 1:
            item = self._histLayout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._appendWelcome()

    def closeEvent(self, event) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._worker.wait(2000)
        super().closeEvent(event)

"""LLM 对话客户端：OpenAI 兼容 chat/completions（纯标准库 urllib）。

- 配置（base_url / api_key / model / temperature）存 QSettings；
- models.json（与本文件同目录）可登记多个模型显示名，面板顶部展示当前模型；
- 请求体支持 tool-calling（function calling）与多模态 image_url（data URL）。
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from typing import Optional

SETTINGS_ORG = "Cangai"
SETTINGS_APP = "CangaiRelief"

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"     # 2026-10 最新 V4.1 Flash（旧名 deepseek-chat/reasoner 已弃用）

# 图片附件统一压缩上限（多模态模型足够识别线稿/照片，且控制请求体积）
IMAGE_MAX_SIDE = 1280
IMAGE_JPEG_Q = 85


class LLMError(RuntimeError):
    """带用户可读信息的调用错误。"""


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def loadConfig() -> dict:
    from PySide6.QtCore import QSettings
    st = QSettings(SETTINGS_ORG, SETTINGS_APP)
    return {
        "base_url": st.value("AI/base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL,
        "api_key": st.value("AI/api_key", "") or "",
        "model": st.value("AI/model", DEFAULT_MODEL) or DEFAULT_MODEL,
        "temperature": float(st.value("AI/temperature", 0.3) or 0.3),
    }


def saveConfig(cfg: dict) -> None:
    from PySide6.QtCore import QSettings
    st = QSettings(SETTINGS_ORG, SETTINGS_APP)
    st.setValue("AI/base_url", cfg.get("base_url", DEFAULT_BASE_URL))
    st.setValue("AI/api_key", cfg.get("api_key", ""))
    st.setValue("AI/model", cfg.get("model", DEFAULT_MODEL))
    st.setValue("AI/temperature", float(cfg.get("temperature", 0.3)))


def modelDisplayName(model: str) -> str:
    """models.json 里可把模型 id 映射为友好显示名，找不到就原样返回。
    （打包版由 Cangai.spec 把 core/ai/models.json 收进 _MEIPASS）"""
    try:
        from resource_path import resource_path
        path = resource_path("core/ai/models.json")
        with open(path, "r", encoding="utf-8") as f:
            table = json.load(f)
        return table.get(model, model)
    except (OSError, ValueError, ImportError):
        return model


# ---------------------------------------------------------------------------
# 图片处理（主线程调用：QtGui）
# ---------------------------------------------------------------------------

def compressImageToDataUrl(path: str) -> str:
    """读图片 → 等比压缩到 1280 边长 → JPEG data URL（主线程调用）。"""
    from PySide6.QtCore import Qt, QIODevice
    from PySide6.QtGui import QImage, QPixmap
    from PySide6.QtCore import QBuffer
    img = QImage(path)
    if img.isNull():
        raise LLMError(f"无法读取图片: {path}")
    if max(img.width(), img.height()) > IMAGE_MAX_SIDE:
        img = img.scaled(IMAGE_MAX_SIDE, IMAGE_MAX_SIDE,
                         Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    qb = QBuffer()
    qb.open(QBuffer.ReadWrite)
    qp = QPixmap.fromImage(img)
    qp.save(qb, "JPEG", IMAGE_JPEG_Q)
    raw = bytes(qb.data())
    qb.close()
    if not raw:
        raise LLMError("图片编码失败")
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")


def b64ToDataUrl(b64: str, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{b64}"


# ---------------------------------------------------------------------------
# 请求
# ---------------------------------------------------------------------------

def buildPayload(cfg: dict, messages: list, tools: Optional[list]) -> dict:
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": float(cfg.get("temperature", 0.3)),
        "stream": False,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return payload


def chatOnce(cfg: dict, messages: list, tools: Optional[list],
             timeout: float = 120.0) -> dict:
    """发一次 chat/completions 请求，返回 choices[0].message（dict）。

    可能抛 LLMError（网络/鉴权/格式）。工作线程调用。"""
    base = (cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    url = base + "/chat/completions"
    key = cfg.get("api_key") or ""
    if not key:
        raise LLMError("未配置 API Key：点面板右上 ⚙ 填入大模型服务地址与密钥")
    body = json.dumps(buildPayload(cfg, messages, tools)).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:400]
        except OSError:
            pass
        raise LLMError(f"HTTP {e.code}: {detail or e.reason}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"网络错误: {e.reason}") from e
    except (ValueError, KeyError) as e:
        raise LLMError(f"响应格式错误: {e}") from e
    try:
        return data["choices"][0]["message"]
    except (KeyError, IndexError):
        raise LLMError(f"响应缺少 choices: {json.dumps(data)[:300]}")

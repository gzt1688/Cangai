"""应用内 MCP 桥：本机 TCP 上的 JSON-RPC（行分隔），供 mcp/cangai_mcp_server.py 连接。

协议（每行一条 JSON，JSON-RPC 2.0）：
- {"method":"ping"}                          → 探活（软件是否在运行）
- {"method":"tools/list"}                    → 与面板对话同一套工具表
- {"method":"tools/call","params":{"name":..,"arguments":{..}}}

安全：只监听 127.0.0.1；端口默认 31789，QSettings("AI/bridge_port") 可改；
QSettings("AI/bridge_enabled")=false 时完全不监听。
工具执行经 core.ai.tools.invoke 投递回 Qt 主线程（界面忙时 30s 超时报错）。
"""
from __future__ import annotations

import json
from typing import Optional

from PySide6.QtNetwork import QTcpServer

DEFAULT_PORT = 31789
SETTINGS_ORG = "Cangai"
SETTINGS_APP = "CangaiRelief"

_server = None      # 单例 QTcpServer


def bridgeEnabled() -> bool:
    from PySide6.QtCore import QSettings
    st = QSettings(SETTINGS_ORG, SETTINGS_APP)
    v = st.value("AI/bridge_enabled", True)
    return str(v).lower() not in ("false", "0", "no")


def bridgePort() -> int:
    from PySide6.QtCore import QSettings
    st = QSettings(SETTINGS_ORG, SETTINGS_APP)
    try:
        return int(st.value("AI/bridge_port", DEFAULT_PORT))
    except (TypeError, ValueError):
        return DEFAULT_PORT


def startBridge(port: Optional[int] = None) -> Optional[int]:
    """启动桥（主线程调用）。返回实际监听端口；未启用/失败返回 None。"""
    global _server
    if _server is not None and _server.isListening():
        return _server.serverPort()
    if port is None:
        if not bridgeEnabled():
            return None
        port = bridgePort()
    from PySide6.QtNetwork import QHostAddress
    srv = BridgeServer()
    if not srv.listen(QHostAddress.LocalHost, port):
        # 端口被占：退回随机端口（MCP 端用 ping 扫描或配置同端口即可）
        if not srv.listen(QHostAddress.LocalHost, 0):
            return None
    _server = srv
    return srv.serverPort()


def stopBridge() -> None:
    global _server
    if _server is not None:
        _server.close()
        _server = None


class BridgeServer(QTcpServer):
    """行分隔 JSON-RPC 服务（只监听 127.0.0.1）。"""

    def __init__(self) -> None:
        super().__init__()
        self.newConnection.connect(self._onConn)

    def _onConn(self) -> None:
        sock = self.nextPendingConnection()
        if sock is None:
            return
        buf = {"data": b""}

        def _ready() -> None:
            buf["data"] += bytes(sock.readAll().data())
            while b"\n" in buf["data"]:
                line, buf["data"] = buf["data"].split(b"\n", 1)
                resp = _handle(line.decode("utf-8", "replace").strip())
                if resp is not None:
                    sock.write(resp.encode("utf-8") + b"\n")
                    sock.flush()

        sock.disconnected.connect(sock.deleteLater)
        sock.readyRead.connect(_ready)


def _handle(line: str) -> Optional[str]:
    """处理一行 JSON-RPC，返回应答行（通知返回 None）。"""
    if not line:
        return None
    try:
        msg = json.loads(line)
    except ValueError:
        return _err(None, -32700, "JSON 解析失败")
    rid = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params") or {}
    try:
        if method == "ping":
            from core.api import getApiInstance
            return _res(rid, {"app": "Cangai", "hasHost": getApiInstance().hasHost()})
        if method == "tools/list":
            from core.ai.tools import mcpToolDefs
            return _res(rid, {"tools": mcpToolDefs()})
        if method == "tools/call":
            from core.ai.tools import callToolForMcp
            return _res(rid, callToolForMcp(
                params.get("name", ""), params.get("arguments") or {}))
        if method == "resources/list":
            return _res(rid, {"resources": []})
        if method == "prompts/list":
            return _res(rid, {"prompts": []})
        return _err(rid, -32601, f"未知方法: {method}")
    except Exception as e:               # 工具失败按 MCP 语义回内容而非协议错误
        if method == "tools/call":
            text = json.dumps({"error": str(e)}, ensure_ascii=False)
            return _res(rid, {"content": [{"type": "text", "text": text}],
                              "isError": True})
        return _err(rid, -32000, str(e))


def _res(rid, result) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": rid, "result": result},
                      ensure_ascii=False)


def _err(rid, code: int, message: str) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": rid,
                       "error": {"code": code, "message": message}},
                      ensure_ascii=False)

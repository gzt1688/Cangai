#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cangai 浮雕 —— 标准 MCP (Model Context Protocol) 服务端。

轻量桥接器：自身不含 CAD 逻辑，通过本机 TCP（应用内 core/ai/bridge_server.py，
默认 127.0.0.1:31789）把 MCP 请求转发给正在运行的 Cangai 软件，让外部 AI 客户端
完整驱动软件（文档/几何/命令/脚本/导入导出/视口截图，工具表与软件内「Ai」面板同源，
见 core/ai/tools.py）。

支持两种传输：
  1) stdio（默认）—— 本地桌面 MCP 客户端（Claude Desktop / WorkBuddy / TRAE /
     Cursor / Qoder 等）以“命令 + 参数”方式拉起本进程：
         python mcp/cangai_mcp_server.py
         python mcp/cangai_mcp_server.py --bridge-port 31789
  2) Streamable HTTP —— 支持“远程/URL 型”MCP 的客户端（含部分豆包/在线客户端）：
         python mcp/cangai_mcp_server.py --transport http --host 127.0.0.1 --port 8765
     然后客户端填 MCP 服务地址 http://127.0.0.1:8765/mcp 即可。
     （仅本机回环安全；如需局域网访问自行 --host 0.0.0.0，注意暴露风险。）

实现的方法：initialize / notifications/initialized / ping /
tools/list / tools/call / resources/list / prompts/list / logging/setLevel /
notifications/cancelled。协议版本协商支持 2025-06-18 / 2025-03-26 / 2024-11-05。
纯标准库，无第三方依赖。前置条件：Cangai 软件正在运行且 MCP 桥已启用。

stdio 客户端配置示例：
    {"mcpServers": {"cangai": {"command": "python",
      "args": ["F:/softai/Cangai/cangai_relief/mcp/cangai_mcp_server.py"]}}}
HTTP 客户端配置示例：
    {"mcpServers": {"cangai": {"url": "http://127.0.0.1:8765/mcp"}}}
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SUPPORTED_PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]
SERVER_INFO = {
    "name": "cangai-relief",
    "title": "Cangai 浮雕 CAD",
    "version": "1.2",
}
SERVER_CAPABILITIES = {
    "tools": {"listChanged": False},
    "logging": {},
}


class BridgeError(RuntimeError):
    pass


class TcpBridge:
    """到应用内桥（core/ai/bridge_server）的行分隔 JSON-RPC 客户端（线程安全）。"""

    def __init__(self, port: int) -> None:
        self._port = port
        self._sock = None
        self._buf = b""
        self._next_id = 0
        self._lock = threading.Lock()

    def _ensure(self) -> None:
        if self._sock is not None:
            return
        try:
            s = socket.create_connection(("127.0.0.1", self._port), timeout=5)
        except OSError as e:
            raise BridgeError(
                f"无法连接 Cangai 软件 (127.0.0.1:{self._port}: {e})。"
                "请确认 Cangai 正在运行，且 设置→AI 中「启用 MCP 桥」已勾选。") from e
        s.settimeout(120)
        self._sock = s
        self._buf = b""

    def call(self, method: str, params: dict | None = None):
        with self._lock:                    # 复用单连接：串行化跨线程请求
            self._ensure()
            self._next_id += 1
            rid = self._next_id
            msg = {"jsonrpc": "2.0", "id": rid, "method": method,
                   "params": params or {}}
            try:
                self._sock.sendall(
                    (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
                while True:
                    while b"\n" in self._buf:
                        line, self._buf = self._buf.split(b"\n", 1)
                        if not line.strip():
                            continue
                        resp = json.loads(line.decode("utf-8"))
                        if resp.get("id") == rid:
                            if "error" in resp:
                                raise BridgeError(
                                    resp["error"].get("message", "桥接错误"))
                            return resp.get("result")
                    self._buf += self._sock.recv(65536)
            except BridgeError:
                raise
            except OSError as e:
                self.close()
                raise BridgeError(f"与 Cangai 的桥接连接中断: {e}") from e

    def alive(self) -> bool:
        try:
            self.call("ping")
            return True
        except BridgeError:
            return False

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


# ---------------------------------------------------------------------------
# 协议分发：返回“响应 dict”或 None（通知不回应）。stdio / http 共用。
# ---------------------------------------------------------------------------

def dispatch(req: dict, bridge: TcpBridge):
    rid = req.get("id")
    method = req.get("method", "")
    params = req.get("params") or {}

    def result(val):
        return {"jsonrpc": "2.0", "id": rid, "result": val}

    def error(code, message):
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": code, "message": message}}

    if method == "initialize":
        asked = params.get("protocolVersion")
        ver = asked if asked in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        return result({"protocolVersion": ver,
                       "capabilities": SERVER_CAPABILITIES,
                       "serverInfo": SERVER_INFO})

    if method.startswith("notifications/"):        # initialized / cancelled / 等
        return None
    if method == "logging/setLevel":
        return result({})

    if method == "ping":
        # server 存活即算 ping 通；桥状态由 tools/* 反映
        return result({})

    if method == "tools/list":
        try:
            return result(bridge.call("tools/list"))
        except BridgeError as e:
            return error(-32002, str(e))

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            return result(bridge.call("tools/call",
                                      {"name": name, "arguments": args}))
        except BridgeError as e:
            # 桥不可用：以工具错误内容返回，让模型看到中文指引而非裸协议错误
            return result({"content": [{"type": "text", "text": json.dumps(
                {"error": str(e)}, ensure_ascii=False)}], "isError": True})

    if method in ("resources/list", "resources/templates/list"):
        key = "resources" if method == "resources/list" else "resourceTemplates"
        return result({key: []})
    if method == "prompts/list":
        return result({"prompts": []})

    if rid is None:                                 # 未知通知：忽略
        return None
    return error(-32601, f"method not found: {method}")


# ---------------------------------------------------------------------------
# stdio 传输
# ---------------------------------------------------------------------------

def run_stdio(bridge: TcpBridge) -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            _emit({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "JSON parse error"}})
            continue
        reqs = req if isinstance(req, list) else [req]
        for one in reqs:
            resp = dispatch(one, bridge)
            if resp is not None:
                _emit(resp)
    bridge.close()
    return 0


def _emit(msg: dict) -> None:
    line = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
    buf = getattr(sys.stdout, "buffer", None)
    if buf is not None:                       # 直写字节：不受控制台 GBK 影响
        buf.write(line)
        buf.flush()
    else:
        sys.stdout.write(line.decode("utf-8"))
        sys.stdout.flush()


# ---------------------------------------------------------------------------
# Streamable HTTP 传输（stateless：一次 POST 一条 JSON-RPC，直接回 JSON）
# ---------------------------------------------------------------------------

def run_http(bridge: TcpBridge, host: str, port: int) -> int:
    httpd = make_http_server(bridge, host, port)
    print(f"[cangai-mcp] Streamable HTTP 监听 http://{host}:{port}/mcp"
          "  (Ctrl+C 停止)", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        bridge.close()
    return 0


class McpHttpHandler(BaseHTTPRequestHandler):
    """Streamable HTTP（stateless）：POST /mcp 收一条 JSON-RPC，直接回 JSON。"""

    def _bridge(self):
        return getattr(self.server, "bridge", None)

    def _allowed_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True                     # 非浏览器客户端
        from urllib.parse import urlparse
        h = urlparse(origin).hostname or ""
        host_hdr = urlparse("http://" + (self.headers.get("Host") or "")).hostname
        return h in ("127.0.0.1", "localhost", "::1") or h == host_hdr

    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path.rstrip("/") not in ("/mcp", ""):
            self._send(404, {"error": "not found; POST to /mcp"})
            return
        if not self._allowed_origin():
            self._send(403, {"error": "cross-origin forbidden"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            req = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700,
                                       "message": "JSON parse error"}})
            return
        reqs = req if isinstance(req, list) else [req]
        responses = [r for r in (dispatch(one, self._bridge()) for one in reqs)
                     if r is not None]
        if not responses:                     # 全是通知
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send(200, responses[0] if len(responses) == 1 else responses)

    def do_GET(self):
        # 无状态模式不提供 SSE 长连接；返回服务信息便于探活
        br = self._bridge()
        online = br.alive() if hasattr(br, "alive") else False
        self._send(200, {"server": SERVER_INFO, "transport": "streamable-http",
                         "bridge_online": online,
                         "usage": "POST JSON-RPC to /mcp"})

    def log_message(self, *a):                # 静音默认访问日志
        pass


def make_http_server(bridge, host: str, port: int):
    httpd = ThreadingHTTPServer((host, port), McpHttpHandler)
    httpd.bridge = bridge
    return httpd


def main() -> int:
    # Windows 控制台默认 GBK：强制 stdout/stderr 为 UTF-8 + 换行 \n，
    # 否则中文工具名/描述/错误会污染 stdio JSON-RPC 信道导致握手失败
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", newline="\n")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="Cangai MCP 服务端 (stdio / http)")
    ap.add_argument("--transport", choices=["stdio", "http"], default="stdio",
                    help="MCP 传输方式（默认 stdio）")
    ap.add_argument("--bridge-port", type=int, default=31789,
                    help="应用内 MCP 桥端口（默认 31789）")
    ap.add_argument("--host", default="127.0.0.1",
                    help="http 传输监听地址（默认仅本机 127.0.0.1）")
    ap.add_argument("--port", type=int, default=8765,
                    help="http 传输监听端口（默认 8765）")
    args = ap.parse_args()

    bridge = TcpBridge(args.bridge_port)
    if not bridge.alive():
        print("[cangai-mcp] 警告：当前连不上 Cangai 软件（桥未运行/未启用）；"
              "启动 Cangai 后工具即可用。", file=sys.stderr)

    if args.transport == "http":
        return run_http(bridge, args.host, args.port)
    return run_stdio(bridge)


if __name__ == "__main__":
    sys.exit(main())

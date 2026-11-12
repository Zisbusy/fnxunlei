# -*- coding: utf-8 -*-
"""httpd.py —— Unix Socket HTTP 服务 + 静态页面。

- ThreadingUnixHTTPServer：监听 ${TRIM_APPDEST}/fnxunlei.sock（由 fnOS 统一网关转发）
- ThreadingTCPServer：仅供本地开发调试（--http 127.0.0.1:8788）
- Handler：/api/* → api.py 的 action，其余路径提供 www 静态文件
- 安全：静态文件做真实路径边界检查，拒绝目录穿越；请求体有大小上限
"""

from __future__ import annotations

import json
import mimetypes
import os
import socketserver
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import api

MAX_BODY = 2 << 20          # 单请求体上限 2MB（粘贴链接足够）
MAX_HEADER_COUNT = 100


def normalize_base_path(path: str) -> str:
    if not path:
        return "/"
    normalized = path.strip()
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized.rstrip("/") or "/"


def strip_base_path(path: str, base_path: str) -> str:
    if base_path != "/" and path.startswith(base_path):
        return path[len(base_path):] or "/"
    return path or "/"


def parse_request_body(handler) -> dict:
    """解析 POST 请求体：支持 application/json 与表单两种格式。"""
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        length = 0
    if length < 0 or length > MAX_BODY:
        raise ValueError("请求体过大")
    raw = handler.rfile.read(length) if length else b""
    if not raw:
        return {}
    text = raw.decode("utf-8", "replace")
    content_type = handler.headers.get("Content-Type", "")
    if "application/json" in content_type:
        payload = json.loads(text or "{}")
        if not isinstance(payload, dict):
            return {}
        return {key: ["" if value is None else str(value)] for key, value in payload.items()}
    return parse_qs(text, keep_blank_values=True)


def merge_query_action(path: str, query: str) -> dict:
    parsed = parse_qs(query or "", keep_blank_values=True)
    if "action" not in parsed:
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2 and parts[0] == "api":
            parsed["action"] = [parts[1]]
    return parsed


def dispatch_api(handler, api_path: str, query: str) -> None:
    try:
        body = parse_request_body(handler)
        query_values = merge_query_action(api_path, query)
    except (ValueError, json.JSONDecodeError) as exc:
        api.http_write({"ok": False, "error": "请求格式不正确：%s" % exc})
        return

    api.REQUEST_CONTEXT.value = {
        "handler": handler,
        "body": body,
        "query": query_values,
    }
    try:
        action = api.first_value("action")
        if action.endswith(".cgi"):
            action = action[:-4]
        if not action:
            api.error_response("400 Bad Request", "缺少 action 参数")
        handler_fn = api.ACTIONS.get(action)
        if not handler_fn:
            api.error_response("404 Not Found", "未知操作：%s" % action)
        handler_fn()
    except api.ResponseDone:
        return
    except Exception as exc:  # noqa: BLE001 - 兜底成可读 JSON，不泄露堆栈
        import traceback

        import util

        util.log("API 异常 %s: %s" % (api_path, traceback.format_exc(limit=3)))
        try:
            api.error_response(
                "500 Internal Server Error",
                "服务内部错误：%s（详见应用日志 info.log）" % util.clip(exc, 200),
            )
        except api.ResponseDone:
            return
    finally:
        if hasattr(api.REQUEST_CONTEXT, "value"):
            del api.REQUEST_CONTEXT.value


if hasattr(socketserver, "UnixStreamServer"):

    class ThreadingUnixHTTPServer(  # type: ignore[misc]
        socketserver.ThreadingMixIn, socketserver.UnixStreamServer
    ):
        daemon_threads = True
        allow_reuse_address = True

        def __init__(self, socket_path, handler_cls, *, base_path, www_root):
            self.server_name = "fnxunlei"
            self.server_port = 0
            self.base_path = normalize_base_path(base_path)
            self.www_root = Path(www_root)
            super().__init__(socket_path, handler_cls)
            # 网关以 root 运行，socket 只需对 owner 可写；组/其他不可写
            try:
                os.chmod(socket_path, 0o660)
            except OSError:
                pass

else:  # pragma: no cover - 仅非 Unix 平台（如本地 Windows 调试）
    ThreadingUnixHTTPServer = None  # type: ignore[assignment]


class ThreadingTCPHTTPServer(ThreadingHTTPServer):
    """本地开发用：TCP 监听，同样的 base_path / www_root 语义。"""

    daemon_threads = True

    def __init__(self, address, handler_cls, *, base_path, www_root):
        self.base_path = normalize_base_path(base_path)
        self.www_root = Path(www_root)
        super().__init__(address, handler_cls)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fnxunlei"
    sys_version = ""

    def do_GET(self):
        self.route()

    def do_HEAD(self):
        self.route()

    def do_POST(self):
        self.route()

    def do_PUT(self):
        self.route()

    def do_DELETE(self):
        self.route()

    def log_message(self, fmt, *args):
        """默认不落访问日志：任务轮询很频繁，日志只保留关键事件（util.log）。"""

    def route(self):
        try:
            if len(self.headers.keys()) > MAX_HEADER_COUNT:
                self.send_error(HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE)
                return
            parsed = urlsplit(self.path or "/")
            if self.server.base_path != "/" and parsed.path == self.server.base_path:
                self.send_response(HTTPStatus.MOVED_PERMANENTLY)
                location = self.server.base_path + "/"
                if parsed.query:
                    location += "?" + parsed.query
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            path = strip_base_path(parsed.path, self.server.base_path)
            if path.startswith("/api"):
                dispatch_api(self, path, parsed.query)
                return
            self.serve_static(path)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def serve_static(self, path: str):
        rel_path = unquote(path or "/")
        if rel_path in ("", "/"):
            rel_path = "/index.html"
        root = self.server.www_root.resolve()
        try:
            target = (root / rel_path.lstrip("/")).resolve()
        except (OSError, RuntimeError):
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        if target != root and root not in target.parents:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in (
            "application/javascript", "application/json",
        ):
            content_type += "; charset=utf-8"
        try:
            data = target.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header(
            "Cache-Control",
            "no-store" if target.name == "index.html" else "public, max-age=60",
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

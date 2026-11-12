# -*- coding: utf-8 -*-
"""xlhttp.py —— 只用标准库的 HTTP 客户端（替代 httpx，安装零依赖）。

为什么不用 requests / httpx：
- fnOS 应用安装时不应联网装 Python 包（离线可用性 + 供应链安全）；
- 迅雷接口只需要 JSON / 查询参数 / multipart 表单 / Set-Cookie 读取，
  标准库 urllib 完全够用。

要点：
- 显式禁用环境代理（ProxyHandler({})）：NAS 上残留的 http_proxy 会让
  迅雷登录请求绕道境外出口，触发风控；
- HTTP 4xx/5xx 不抛异常，而是把状态码与响应体一起返回——迅雷接口大量
  使用「HTTP 200 + 错误体」或 4xx + JSON，需要统一按错误码判断；
- Set-Cookie 逐条解析，供风控验证链路（sendsms / checksms）使用。
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid

from errors import XunleiError

DEFAULT_TIMEOUT = 20.0

_SSL_CONTEXT = ssl.create_default_context()
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=_SSL_CONTEXT),
)


class HttpResult:
    """一次 HTTP 响应的最小封装。"""

    def __init__(self, status: int, headers: list, body: bytes):
        self.status = status
        self.headers = headers            # [(name, value), ...]
        self.body = body or b""

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def header(self, name: str) -> str:
        target = (name or "").lower()
        for key, value in self.headers:
            if key.lower() == target:
                return value
        return ""

    def set_cookies(self) -> list:
        """解析本响应写入的 cookie：[(name, value), ...]（value 为空表示删除）。"""
        out = []
        for key, value in self.headers:
            if key.lower() != "set-cookie":
                continue
            pair = (value or "").split(";", 1)[0].strip()
            if not pair or "=" not in pair:
                continue
            name, _, cookie_value = pair.partition("=")
            out.append((name.strip(), cookie_value.strip()))
        return out

    def json(self):
        try:
            return json.loads(self.text)
        except ValueError:
            return None


def _encode_params(params: dict) -> str:
    clean = {k: v for k, v in (params or {}).items() if v is not None}
    return urllib.parse.urlencode(clean, doseq=True)


def _build_multipart(fields: dict) -> tuple:
    """最小 multipart/form-data 编码（风控验证页的跨域 iframe 表单同形）。"""
    boundary = "----fnxunlei" + uuid.uuid4().hex
    chunks = []
    for name, value in (fields or {}).items():
        text = "" if value is None else str(value)
        chunks.append(
            ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
             % (boundary, name, text)).encode("utf-8")
        )
    chunks.append(("--%s--\r\n" % boundary).encode("utf-8"))
    return b"".join(chunks), "multipart/form-data; boundary=" + boundary


def request(method: str, url: str, *, params: dict = None, json_body: dict = None,
            form: dict = None, multipart: dict = None, headers: dict = None,
            timeout: float = DEFAULT_TIMEOUT) -> HttpResult:
    """发一次 HTTP 请求；网络层失败抛 XunleiError，HTTP 错误码原样返回。"""
    if params:
        query = _encode_params(params)
        if query:
            url = url + ("&" if "?" in url else "?") + query

    body = None
    final_headers = {"accept": "application/json;charset=UTF-8"}
    final_headers.update(headers or {})

    if json_body is not None:
        body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        final_headers["content-type"] = "application/json"
    elif multipart:
        body, content_type = _build_multipart(multipart)
        final_headers["content-type"] = content_type
    elif form is not None:
        body = _encode_params(form).encode("utf-8")
        final_headers["content-type"] = "application/x-www-form-urlencoded"

    req = urllib.request.Request(url, data=body, method=method.upper())
    for key, value in final_headers.items():
        req.add_header(key, value)

    try:
        with _OPENER.open(req, timeout=timeout) as response:
            return HttpResult(response.status, list(response.headers.items()), response.read())
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read()
        except Exception:  # noqa: BLE001 - 错误响应读失败也不能盖住状态码
            payload = b""
        return HttpResult(exc.code, list(exc.headers.items()) if exc.headers else [], payload)
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLError):
            raise XunleiError("TLS 握手失败（请检查 NAS 系统时间与证书链）：%s" % reason)
        raise XunleiError("网络错误（请检查 NAS 出网 / DNS / 代理设置）：%s" % reason)
    except (socket.timeout, TimeoutError):
        raise XunleiError("网络超时（%.0fs）：迅雷接口无响应，稍后重试" % timeout)
    except ssl.SSLError as exc:
        raise XunleiError("TLS 握手失败（请检查 NAS 系统时间与证书链）：%s" % exc)
    except (http.client.HTTPException, OSError) as exc:
        raise XunleiError("网络请求失败：%s" % exc)

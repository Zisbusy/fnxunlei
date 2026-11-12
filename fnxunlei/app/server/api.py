# -*- coding: utf-8 -*-
"""api.py —— HTTP 接口动作处理（只服务「推送下载」这一件事）。

统一响应：{"ok": true, ...} 或 {"ok": false, "error": "中文错误"}。
请求上下文（handler / body / query）由 httpd.py 在分发前注入 REQUEST_CONTEXT。

安全约定：
- 凭据只在后端内存与 var/config.json 中流转，接口只回掩码；
- 网关写操作只放行管理员（X-Trim-Isadmin: true），普通用户只读；
- 未经过网关（本地直连 / 调试）时不带 X-Trim-* 头，按本机可信处理。
"""

from __future__ import annotations

import json
import threading
import time

import links as linksmod
import store
import util
import xl_channel
from errors import XunleiError
from version import APP_NAME, DISPLAY_NAME, VERSION

REQUEST_CONTEXT = threading.local()

MAX_DISPATCH = 20          # 单次推送条数上限
DISPATCH_BUDGET = 60.0     # 单次请求的推送时间预算（秒）


class ResponseDone(Exception):
    """响应已写出，用于终止当前请求处理。"""


def current_request() -> dict:
    return getattr(REQUEST_CONTEXT, "value", {}) or {}


# ---------------------------------------------------------------------------
# 响应输出
# ---------------------------------------------------------------------------
def http_write(payload: dict, status: int = 200) -> None:
    request = current_request()
    handler = request.get("handler")
    if handler is None:
        raise RuntimeError("no active request handler")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)
    raise ResponseDone()


def ok_response(payload: dict = None) -> None:
    body = {"ok": True}
    if payload:
        body.update(payload)
    http_write(body)


def error_response(status, message: str) -> None:
    http_write(
        {
            "ok": False,
            "error": util.sanitize_text(util.redact(message or "")),
            "http_status": str(status),
        },
        status=403 if str(status).startswith("403") else 200,
    )


# ---------------------------------------------------------------------------
# 参数读取
# ---------------------------------------------------------------------------
def first_value(name: str, default: str = "") -> str:
    """先取 query，再取表单/JSON body（单值）。"""
    request = current_request()
    for source in (request.get("query") or {}, request.get("body") or {}):
        values = source.get(name)
        if values:
            return values[0]
    return default


def form_values() -> dict:
    request = current_request()
    body = request.get("body") or {}
    return {key: (values[0] if values else "") for key, values in body.items()}


def is_mutating() -> bool:
    handler = current_request().get("handler")
    return bool(handler and handler.command in ("POST", "PUT", "PATCH", "DELETE"))


def is_admin_request() -> bool:
    """只有经网关转发时才校验管理员；无 X-Trim-* 头视为本机直连。"""
    handler = current_request().get("handler")
    if handler is None:
        return True
    headers = handler.headers
    via_gateway = any(key.lower().startswith("x-trim-") for key in headers.keys())
    if not via_gateway:
        return True
    return (headers.get("X-Trim-Isadmin", "") or "").strip().lower() == "true"


def gateway_identity() -> dict:
    """网关注入的可信身份（仅用于审计日志，不用于业务判断）。"""
    handler = current_request().get("handler")
    if handler is None:
        return {"uid": "", "name": "", "admin": ""}
    headers = handler.headers
    return {
        "uid": (headers.get("X-Trim-Userid", "") or "").strip(),
        "name": (headers.get("X-Trim-Username", "") or "").strip(),
        "admin": (headers.get("X-Trim-Isadmin", "") or "").strip(),
    }


# ---------------------------------------------------------------------------
# 通道实例与鉴权
# ---------------------------------------------------------------------------
def channel() -> xl_channel.XunleiRemote:
    return xl_channel.XunleiRemote()


def _guard() -> None:
    """写操作鉴权 + 审计：网关身份只记录，不用来替代业务判断。"""
    if not is_mutating():
        return
    who = gateway_identity()
    if not is_admin_request():
        util.log("拒绝写操作 action=%s uid=%s（非管理员）"
                 % (first_value("action"), who["uid"] or "-"))
        error_response("403 Forbidden", "仅管理员可执行此操作")
    util.log("写操作 action=%s uid=%s admin=%s"
             % (first_value("action"), who["uid"] or "-", who["admin"] or "local"))


# ---------------------------------------------------------------------------
# 动作
# ---------------------------------------------------------------------------
def handle_status():
    ch = channel()
    ok_response({
        "app": {"name": APP_NAME, "display": DISPLAY_NAME, "version": VERSION},
        "status": ch.status(),
        "config": store.public_config(ch.conf),
    })


def handle_config_get():
    ch = channel()
    ok_response({
        "app": {"name": APP_NAME, "display": DISPLAY_NAME, "version": VERSION},
        "config": store.public_config(ch.conf),
        "review": ch.review_state(),
    })


def handle_config_set():
    _guard()
    payload = form_values()
    config = store.apply_form(store.load_config(), payload)
    ok_response({"config": store.public_config(config)})


def handle_login():
    _guard()
    ch = channel()
    payload = form_values()
    if payload.get("login_name") or payload.get("password"):
        # 登录时直接用表单里的账号密码（含"****** = 保持原值"语义）
        ch.conf = store.apply_form(ch.conf, payload)
        ch.auth.conf = ch.conf
    info = ch.login()
    ok_response({"status": info, "config": store.public_config(ch.conf)})


def handle_logout():
    _guard()
    ch = channel()
    ch.logout()
    ok_response({"detail": "已退出登录（本机账号与密码仍保留）"})


def handle_devices():
    ch = channel()
    ok_response({"devices": ch.devices(), "target": (ch.conf.get("target") or "")})


def handle_dirs():
    """浏览远程设备下载根下的目录；失败时返回 browse_ok=False 让界面回退手动输入。"""
    path = (first_value("path") or "").strip()
    target = (first_value("target") or "").strip()
    segments = [s for s in path.split("/") if s]
    if path.startswith("/") or "\\" in path or any(s in (".", "..") for s in segments):
        error_response("400 Bad Request", "目录参数不正确")

    try:
        data = channel().browse_dirs("/".join(segments), target=target)
    except XunleiError as exc:
        ok_response({
            "browse_ok": False,
            "error": util.sanitize_text(str(exc)),
            "root": "",
            "path": "/".join(segments),
            "parent": None,
            "dirs": [],
        })
    ok_response({"browse_ok": True, **data})


def handle_parse():
    payload = form_values()
    ok_response({"links": [info.to_dict()
                           for info in linksmod.parse_text(payload.get("text") or "")]})


def handle_add():
    _guard()
    ch = channel()
    payload = form_values()
    infos = linksmod.parse_text(payload.get("text") or "")
    if not infos:
        error_response("400 Bad Request", "没有可推送的链接（请粘贴 magnet / thunder / http 链接）")
    directory = (payload.get("directory") or "").strip().strip("/")

    started = time.monotonic()
    results = []
    for info in infos:
        if not info.dispatchable:
            results.append({
                "ok": False, "url": info.url, "name": info.display,
                "error": info.reason or "该链接类型暂不支持推送",
            })
            continue
        if len(results) >= MAX_DISPATCH:
            results.append({
                "ok": False, "url": info.url, "name": info.display,
                "error": "单次最多推送 %d 条，本条及之后未推送，请分批提交" % MAX_DISPATCH,
            })
            continue
        if results and time.monotonic() - started > DISPATCH_BUDGET:
            results.append({
                "ok": False, "url": info.url, "name": info.display,
                "error": "推送超时（>%d秒）：前 %d 条已处理，本条及之后未推送，请分批重试"
                         % (int(DISPATCH_BUDGET), len(results)),
            })
            continue

        prior = history_check(info.url)
        try:
            added = ch.add(info.url, directory=directory or None)
            item = {
                "ok": True,
                "url": info.url,
                "id": added["id"],
                "name": added["name"],
                "dir": added["dir"],
                "size": added["size"],
            }
            if prior.get("downloaded"):
                item["duplicate"] = prior.get("name") or ""
            results.append(item)
        except XunleiError as exc:
            results.append({
                "ok": False, "url": info.url, "name": info.display,
                "error": util.sanitize_text(str(exc)),
            })

    ok_response({
        "results": results,
        "total": len(results),
        "success": len([item for item in results if item.get("ok")]),
    })


def handle_review_send():
    _guard()
    ch = channel()
    detail = ch.review_send()
    ok_response({
        "detail": "验证码已发送至 %s" % (detail.get("mobile") or "绑定手机"),
        "review": ch.review_state(),
    })


def handle_review_check():
    _guard()
    code = (first_value("code") or "").strip()
    ch = channel()
    detail = ch.review_check(code)
    ok_response({"detail": detail.get("detail", "验证通过"),
                 "review": ch.review_state()})


def handle_diagnose():
    """只读自检（界面不展示，可 curl 调用）。"""
    ok_response({"diagnose": channel().diagnose()})


# ---------------------------------------------------------------------------
# 下载历史查重（同一资源重复推送时给提示）
# ---------------------------------------------------------------------------
def history_check(url: str) -> dict:
    identity = linksmod.identity(url)
    if not identity:
        return {"downloaded": False}
    for record in reversed(store.load_records()):
        record_identity = record.get("identity") or linksmod.identity(record.get("url", ""))
        if record_identity and record_identity == identity:
            return {
                "downloaded": True,
                "name": record.get("name") or record.get("url", "")[:80],
                "created_at": record.get("created_at"),
            }
    return {"downloaded": False}


# action 路由表
ACTIONS = {
    "status": handle_status,
    "config_get": handle_config_get,
    "config_set": handle_config_set,
    "login": handle_login,
    "logout": handle_logout,
    "devices": handle_devices,
    "dirs": handle_dirs,
    "parse": handle_parse,
    "add": handle_add,
    "review_send": handle_review_send,
    "review_check": handle_review_check,
    "diagnose": handle_diagnose,
}

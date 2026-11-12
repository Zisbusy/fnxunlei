# -*- coding: utf-8 -*-
"""util.py —— 通用小工具：日志、文本清洗、脱敏、体积格式化。"""

from __future__ import annotations

import os
import re
import time

import paths

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# 链接与错误文案里的敏感查询参数值，日志/界面统一掩码
_SECRET_QS_RE = re.compile(
    r"(?i)\b(token|access_token|refresh_token|captcha_token|plugin_app_token"
    r"|secret|client_secret|password|passwd|auth|key)=([^&\s\"'<>]+)"
)


def sanitize_text(text, limit: int = 4000) -> str:
    """去掉控制字符、统一换行并截断，避免把异常内容原样回给界面。"""
    value = "" if text is None else str(text)
    value = _CONTROL_RE.sub("", value)
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return value[:limit]


def redact(text) -> str:
    """掩码 URL / 提示文本中的凭据类查询参数。"""
    return _SECRET_QS_RE.sub(lambda m: "%s=******" % m.group(1), str(text or ""))


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(message: str) -> None:
    """追加一行运行日志（时间 + 内容，脱敏后最多 600 字符）；失败静默。"""
    try:
        line = "%s %s\n" % (now_str(), redact(sanitize_text(message, 600)))
        with open(paths.LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass


def fmt_size(size) -> str:
    """字节数 → 人类可读（1.2 GB / 860 MB …）。"""
    try:
        value = float(size or 0)
    except (TypeError, ValueError):
        return "-"
    if value <= 0:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024.0
        index += 1
    if index == 0:
        return "%d %s" % (int(value), units[index])
    return "%.1f %s" % (value, units[index])


def clip(text, limit: int = 200) -> str:
    value = sanitize_text(text, limit + 1)
    return value if len(value) <= limit else value[: limit - 1] + "…"


def file_mode_600(path: str) -> None:
    """配置文件收紧到 600（Windows 上 chmod 基本无效，忽略异常）。"""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

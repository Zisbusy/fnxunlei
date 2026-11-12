# -*- coding: utf-8 -*-
"""store.py —— 配置与本地任务记录的持久化。

- 配置：<var>/config.json（含迅雷登录态，权限收紧到 600，不出现在 fpk 里）
- 任务记录：<var>/tasks.json（任务 id / 原始链接 / 名称 / 目录 / 时间）

配置只在后端内存与这个文件之间流转：不下发前端、不写日志。
"""

from __future__ import annotations

import copy
import json
import os
import threading

import paths
import util

_LOCK = threading.RLock()

# 全部为扁平键：远程通道只需要这些
DEFAULT_CONFIG = {
    # 账号与目标
    "login_name": "",
    "password": "",
    "device_id": "",
    "target": "",
    "dir": "",
    # 登录态（自动维护）
    "access_token": "",
    "token_type": "Bearer",
    "refresh_token": "",
    "user_id": "",
    "user_name": "",
    # 风控（review_panel）短信验证会话
    "creditkey": "",
    "review_url": "",
    "review_token": "",
    "review_key": "",
    "review_cookies": {},
}

# 前端可写的键（其余键一律忽略，避免前端覆盖登录态）
_WRITABLE = {"login_name", "password", "target", "dir"}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config() -> dict:
    with _LOCK:
        raw = None
        try:
            with open(paths.CONFIG_FILE, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            raw = None
        if not isinstance(raw, dict):
            return copy.deepcopy(DEFAULT_CONFIG)
        return _merge(DEFAULT_CONFIG, raw)


def save_config(config: dict) -> dict:
    merged = _merge(DEFAULT_CONFIG, config or {})
    with _LOCK:
        paths.ensure_data_dir()
        tmp = paths.CONFIG_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(merged, handle, ensure_ascii=False, indent=2)
            util.file_mode_600(tmp)
            os.replace(tmp, paths.CONFIG_FILE)
        except OSError:
            pass
    return merged


def ensure_config() -> None:
    """首次安装时落一份默认配置（不含任何凭据）。"""
    if os.path.isfile(paths.CONFIG_FILE):
        return
    save_config(load_config())


def apply_form(config: dict, payload: dict) -> dict:
    """把前端表单写入配置；password 传 "******" 表示保持原值。"""
    out = _merge(DEFAULT_CONFIG, config or {})
    for key in _WRITABLE:
        if key not in payload:
            continue
        value = payload.get(key)
        value = "" if value is None else str(value)
        if key == "password" and value == "******":
            continue
        if key == "target":
            out[key] = value.strip()
        elif key == "dir":
            out[key] = value.strip().strip("/")
        elif key == "login_name":
            out[key] = value.strip()
        else:
            out[key] = value
    return save_config(out)


def public_config(config: dict) -> dict:
    """给界面的配置视图：密码只回掩码，登录态一律不下发。"""
    password = bool((config.get("password") or "").strip())
    return {
        "login_name": config.get("login_name", ""),
        "password": "******" if password else "",
        "has_password": password,
        "target": config.get("target", ""),
        "dir": config.get("dir", ""),
        "device_id": config.get("device_id", ""),
        "user_id": config.get("user_id", ""),
    }


# ---------------------------------------------------------------------------
# 本地推送记录
# ---------------------------------------------------------------------------
def load_records() -> list:
    with _LOCK:
        try:
            with open(paths.TASKS_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict) and item.get("id")]


def save_records(records: list) -> None:
    with _LOCK:
        paths.ensure_data_dir()
        tmp = paths.TASKS_FILE + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(records[-500:], handle, ensure_ascii=False, indent=1)
            os.replace(tmp, paths.TASKS_FILE)
        except OSError:
            pass


def add_record(record: dict) -> None:
    """新增一条推送记录（同 id 覆盖）；用于「同一资源是否推过」的提示。"""
    with _LOCK:
        records = [r for r in load_records() if str(r.get("id")) != str(record.get("id"))]
        records.append(record)
        save_records(records)

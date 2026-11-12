# -*- coding: utf-8 -*-
"""paths.py —— 运行数据目录与文件位置。

fnOS 里数据目录由 cmd/main 通过 --data-dir 传入（即 TRIM_PKGVAR），
开发/调试时可用环境变量 FNXL_DATA_DIR 覆盖。
"""

from __future__ import annotations

import os

# server/ 的上两级 = 应用根目录
APP_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DATA_DIR = os.environ.get("FNXL_DATA_DIR") or os.environ.get("TRIM_PKGVAR") or os.path.join(
    APP_ROOT, "var"
)

CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
LOG_FILE = os.path.join(DATA_DIR, "info.log")


def configure(data_dir: str) -> None:
    """按 --data-dir 重算所有文件路径（入口处调用一次）。"""
    global DATA_DIR, CONFIG_FILE, TASKS_FILE, LOG_FILE
    if not data_dir:
        return
    DATA_DIR = os.path.abspath(data_dir)
    CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
    TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
    LOG_FILE = os.path.join(DATA_DIR, "info.log")


def ensure_data_dir() -> None:
    try:
        os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)
    except OSError:
        pass

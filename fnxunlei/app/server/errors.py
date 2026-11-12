# -*- coding: utf-8 -*-
"""errors.py —— 迅雷通道的业务异常。

所有面向用户的失败都收敛到 XunleiError，由 api 层统一转成
{"ok": false, "error": "..."}；未预料的异常由 httpd 兜底。
"""


class XunleiError(Exception):
    """迅雷通道不可用或请求失败（包含可直接展示给用户的中文原因）。"""

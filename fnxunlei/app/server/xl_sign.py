# -*- coding: utf-8 -*-
"""xl_sign.py —— 迅雷客户端协议常量与签名。

这里的 CLIENT_ID / CLIENT_SECRET / APP_KEY 是**公共应用参数**（提取自官方
客户端，所有第三方实现共用，不是用户私有凭据），登录协议必需，无法移除；
可用环境变量 XL_CLIENT_ID / XL_CLIENT_SECRET / XL_APP_KEY 覆盖。

签名规则（与迅雷 Web 端 / alist thunder 驱动一致）：
- devicesign = "div101." + device_id + md5(sha1_hex(device_id + package + appid + appkey))
- captcha_sign = "1." + 依次对 (ClientID + ClientVersion + PackageName + DeviceID + timestamp
  再逐个拼接 ALGORITHMS) 取 md5
"""

from __future__ import annotations

import hashlib
import os
import time

CLIENT_ID = os.environ.get("XL_CLIENT_ID") or "Xp6vsxz_7IYVw2BB"
CLIENT_SECRET = os.environ.get("XL_CLIENT_SECRET") or "Xp6vsy4tN9toTVdMSpomVdXpRmES"
CLIENT_VERSION = "8.31.0.9726"
PACKAGE_NAME = "com.xunlei.downloadprovider"
APP_ID = "40"
APP_KEY = os.environ.get("XL_APP_KEY") or "34a062aaa22f906fca4fefe9fb3a3021"

SIGN_PROVIDER = "access_end_point_token"
REDIRECT_URI = "xlaccsdk01://xunlei.com/callback?state=harbor"

XLUSER_BASE = "https://xluser-ssl.xunlei.com"
XLUSER_API = XLUSER_BASE + "/v1"
DRIVE_API = "https://api-pan.xunlei.com/drive/v1"

ALGORITHMS = [
    "9uJNVj/wLmdwKrJaVj/omlQ",
    "Oz64Lp0GigmChHMf/6TNfxx7O9PyopcczMsnf",
    "Eb+L7Ce+Ej48u",
    "jKY0",
    "ASr0zCl6v8W4aidjPK5KHd1Lq3t+vBFf41dqv5+fnOd",
    "wQlozdg6r1qxh0eRmt3QgNXOvSZO6q/GXK",
    "gmirk+ciAvIgA/cxUUCema47jr/YToixTT+Q6O",
    "5IiCoM9B1/788ntB",
    "P07JH0h6qoM6TSUAK2aL9T5s2QBVeY9JWvalf",
    "+oK0AN",
]

USER_AGENT = (
    "ANDROID-com.xunlei.downloadprovider/8.31.0.9726 netWorkType/5G appid/40 "
    "deviceName/Xiaomi_M2004j7ac deviceModel/M2004J7AC OSVersion/12 "
    "protocolVersion/301 platformVersion/10 sdkVersion/512000 "
    "Oauth2Client/0.9 (Linux 4_14_186-perf-gddfs8vbb238b) (JAVA 0)"
)
CORE_LOGIN_UA = "android-ok-http-client/xl-acc-sdk/version-5.0.12.512000"
# 短信验证链路必须与验证页（vertifyPhone.html）同形：浏览器 UA、不带 SDK 头。
REVIEW_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def new_device_id() -> str:
    """生成 32 位十六进制设备 id（首次登录时生成并持久化）。"""
    return md5_hex(str(time.time_ns()) + CLIENT_ID)


def device_sign(device_id: str) -> str:
    base = "%s%s%s%s" % (device_id, PACKAGE_NAME, APP_ID, APP_KEY)
    inner = hashlib.sha1(base.encode("utf-8")).hexdigest()
    return "div101." + device_id + md5_hex(inner)


def captcha_sign(device_id: str) -> tuple:
    """返回 (timestamp_ms, captcha_sign)。"""
    timestamp = str(int(time.time() * 1000))
    seed = "%s%s%s%s%s" % (
        CLIENT_ID, CLIENT_VERSION, PACKAGE_NAME, device_id, timestamp
    )
    for algo in ALGORITHMS:
        seed = md5_hex(seed + algo)
    return timestamp, "1." + seed

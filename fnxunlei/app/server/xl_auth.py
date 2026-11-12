# -*- coding: utf-8 -*-
"""xl_auth.py —— 迅雷账号登录链、令牌续期与风控短信验证。

登录链（与迅雷 Web 端 / alist thunder 驱动一致）：
  1) POST https://xluser-ssl.xunlei.com/xluser.core.login/v3/login   （账号密码 → sessionID）
  2) POST https://xluser-ssl.xunlei.com/v1/shield/captcha/init        （人机令牌 captcha_token）
  3) POST https://xluser-ssl.xunlei.com/v1/auth/signin/token          （sessionID → access_token）
  4) POST https://xluser-ssl.xunlei.com/v1/auth/token                 （refresh_token → access_token）

风控（review_panel）时会要求短信验证：先 GET .../v3/sendsms 拿 token，
再 multipart POST .../v3/checksms 提交 6 位验证码，结果在 Set-Cookie 里，
最后 GET /credit/v1/report 续期 creditkey，然后重新走登录链。

凭据只落在本机 var/config.json（600），不下发前端、不写日志。
"""

from __future__ import annotations

import json
import re
import time
from urllib.parse import parse_qs, urlsplit

import xl_sign as sign
import xlhttp
from errors import XunleiError
from version import VERSION

EMAIL_RE = re.compile(r"\w+([-+.]\w+)*@\w+([-.]\w+)*\.\w+([-.]\w+)*")

# 需要刷新令牌后重试的错误码（与 alist 一致）
TOKEN_ERROR_CODES = {4122, 4121, 10, 16}
CAPTCHA_ERROR_CODE = 9
# 设备端调用超时（迅雷服务端 → 设备插件反向代理超时）
DEVICE_TIMEOUT_CODE = 2


# ---------------------------------------------------------------------------
# 响应判定
# ---------------------------------------------------------------------------
def err_code(data: dict) -> int:
    """登录接口用 errorCode（字符串），drive 接口用 error_code（整型）。"""
    if not isinstance(data, dict):
        return 0
    for key in ("error_code", "errorCode"):
        try:
            value = int(data.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value:
            return value
    return 0


def is_error(data: dict) -> bool:
    """error == "success" 视为成功；否则 error_code/error/error_description 任一非空即错误。"""
    if not isinstance(data, dict):
        return True
    if (data.get("error") or "").strip() == "success":
        return False
    return bool(
        err_code(data)
        or (data.get("error") or "").strip()
        or (data.get("error_description") or "").strip()
        or (data.get("errorDesc") or "").strip()
    )


def err_text(prefix: str, data: dict) -> str:
    """错误文案：优先中文 errorDesc，其次英文 error_description。"""
    detail = (data.get("errorDesc") or data.get("error_description")
              or data.get("error") or "未知错误")
    code = data.get("error_code") or data.get("errorCode") or 0
    return "%s：%s（error_code=%s · v%s）" % (prefix, detail, code, VERSION)


def is_device_timeout(data: dict) -> bool:
    if err_code(data) == DEVICE_TIMEOUT_CODE:
        return True
    if (data.get("error") or "").strip() == "external_unknown_err":
        return True
    return "远程调用失败" in (data.get("errorDesc") or data.get("error_description") or "")


def masked_hint(data: dict) -> str:
    """error_details[].detail 的脱敏证据摘要（保留链路形态）。"""
    for item in (data.get("error_details") or []):
        if isinstance(item, dict) and (item.get("detail") or "").strip():
            text = str(item["detail"])
            text = re.sub(r"[0-9a-fA-F]{16,}", "[hash]", text)
            text = re.sub(r"\d{6,}", "[id]", text)
            return text[:200]
    return ""


def device_timeout_text(data: dict) -> str:
    hint = masked_hint(data)
    return (
        "迅雷接口错误：设备端响应超时（error_code=2 · v%s）——"
        "迅雷服务端到目标设备的反向代理调用超时：设备上的迅雷远程插件响应慢或未运行"
        "（设备高负载下载 / 网络链路差 / 插件未启用都会触发）。该错误只影响目录浏览与"
        "路径解析，稍后重试常自动恢复；持续出现请检查目标设备的迅雷远程插件是否在运行，"
        "或在设置里换一台支持远程插件的设备%s"
        % (VERSION, ("。证据：%s" % hint) if hint else "")
    )


# ---------------------------------------------------------------------------
# 账号会话
# ---------------------------------------------------------------------------
class XunleiAuth:
    """负责账号登录态：请求头、登录链、令牌续期、风控短信验证。"""

    def __init__(self, config: dict, save):
        self.conf = config
        self._save_cb = save

    # ---------------------------------------------------------------- 基础
    def _save(self) -> None:
        try:
            self._save_cb(self.conf)
        except Exception:  # noqa: BLE001 - 持久化失败不应打断登录流程
            pass

    def device_id(self) -> str:
        device_id = (self.conf.get("device_id") or "").strip()
        if len(device_id) != 32:
            device_id = sign.new_device_id()
            self.conf["device_id"] = device_id
            self._save()
        return device_id

    def base_headers(self, user_agent: str = sign.USER_AGENT) -> dict:
        return {
            "user-agent": user_agent,
            "accept": "application/json;charset=UTF-8",
            "x-device-id": self.device_id(),
            "x-client-id": sign.CLIENT_ID,
            "x-client-version": sign.CLIENT_VERSION,
        }

    def auth_headers(self) -> dict:
        headers = self.base_headers()
        token = self.conf.get("access_token") or ""
        if token:
            headers["authorization"] = "%s %s" % (
                self.conf.get("token_type") or "Bearer", token
            )
        captcha = self.conf.get("captcha_token") or ""
        if captcha:
            headers["x-captcha-token"] = captcha
        return headers

    def json_request(self, method: str, url: str, **kwargs) -> dict:
        """登录链专用：发请求 + 解析 JSON（网络错误统一转 XunleiError）。"""
        result = xlhttp.request(method, url, **kwargs)
        data = result.json()
        if not isinstance(data, dict):
            raise XunleiError(
                "迅雷接口响应异常（HTTP %s）：%s" % (result.status, result.text[:120])
            )
        return data

    # ---------------------------------------------------------------- 登录链
    def core_login(self, username: str, password: str) -> str:
        body = {
            "protocolVersion": "301", "sequenceNo": "1000012",
            "platformVersion": "10", "isCompressed": "0",
            "appid": sign.APP_ID, "clientVersion": sign.CLIENT_VERSION,
            "peerID": "0" * 32, "appName": "ANDROID-com.xunlei.downloadprovider",
            "sdkVersion": "512000", "devicesign": sign.device_sign(self.device_id()),
            "netWorkType": "WIFI", "providerName": "NONE",
            "deviceModel": "M2004J7AC", "deviceName": "Xiaomi_M2004j7ac",
            "OSVersion": "12", "creditkey": self.conf.get("creditkey", ""),
            "hl": "zh-CN", "userName": username, "passWord": password,
            "verifyKey": "", "verifyCode": "", "isMd5Pwd": "0",
        }
        data = self.json_request(
            "POST", sign.XLUSER_BASE + "/xluser.core.login/v3/login",
            json_body=body, headers=self.base_headers(sign.CORE_LOGIN_UA), timeout=30,
        )
        if is_error(data):
            if (data.get("error") or "").strip() == "review_panel":
                raise XunleiError(self.review_message(data))
            raise XunleiError(err_text("迅雷登录失败", data))
        session_id = data.get("sessionID") or ""
        if not session_id:
            raise XunleiError("迅雷登录失败：未返回 sessionID")
        self.conf["creditkey"] = data.get("creditkey", "")
        # CoreLoginResp.userID：登录后刷人机令牌 meta 的首要来源
        uid = data.get("userID") or data.get("userid") or data.get("user_id")
        if uid:
            self.conf["user_id"] = uid
        return session_id

    def refresh_captcha_in_login(self, action: str, username: str) -> str:
        """登录时刷新人机令牌：meta 用账号形态（email / phone_number / username）。"""
        if EMAIL_RE.match(username):
            meta = {"email": username}
        elif 11 <= len(username) <= 18:
            meta = {"phone_number": username}
        else:
            meta = {"username": username}
        return self.captcha_init(action, meta)

    def refresh_captcha_at_login(self, action: str, user_id: str) -> str:
        """登录后刷新人机令牌：meta 固定五键签名形态。

        user_id 即使为空也必须带键——缺 user_id/captcha_sign 会被当成「登录时」
        场景签发，令牌对 drive/v1 无效（error_code=9 循环的根因）。
        """
        timestamp, signature = sign.captcha_sign(self.device_id())
        meta = {
            "client_version": sign.CLIENT_VERSION,
            "package_name": sign.PACKAGE_NAME,
            "user_id": user_id or "",
            "timestamp": timestamp,
            "captcha_sign": signature,
        }
        return self.captcha_init(action, meta)

    def captcha_init(self, action: str, meta: dict) -> str:
        body = {
            "action": action,
            "captcha_token": self.conf.get("captcha_token", ""),
            "client_id": sign.CLIENT_ID,
            "device_id": self.device_id(),
            "meta": meta,
            "redirect_uri": sign.REDIRECT_URI,
        }
        data = self.json_request(
            "POST", sign.XLUSER_API + "/shield/captcha/init",
            json_body=body, headers=self.base_headers(), timeout=30,
        )
        if data.get("url"):
            raise XunleiError("需要人工验证：请打开 %s 完成验证后重试" % data["url"])
        token = data.get("captcha_token") or ""
        if not token:
            raise XunleiError(err_text("获取人机令牌失败", data))
        self.conf["captcha_token"] = token
        self._save()
        return token

    # ---------------------------------------------------------------- 登录 / 续期
    def login(self) -> None:
        username = (self.conf.get("login_name") or "").strip()
        password = self.conf.get("password") or ""
        if not username or not password:
            raise XunleiError("未填写迅雷账号（手机号/邮箱）与密码，无法登录")
        session_id = self.core_login(username, password)
        self.refresh_captcha_in_login("POST:/v1/auth/signin/token", username)
        body = {
            "client_id": sign.CLIENT_ID,
            "client_secret": sign.CLIENT_SECRET,
            "provider": sign.SIGN_PROVIDER,
            "signin_token": session_id,
        }
        data = self.json_request(
            "POST", sign.XLUSER_API + "/auth/signin/token",
            json_body=body, headers=self.base_headers(), timeout=30,
        )
        if not data.get("access_token"):
            if is_error(data):
                raise XunleiError(err_text("换取令牌失败", data))
            raise XunleiError("换取令牌失败：响应无 access_token")
        self.apply_token(data)
        # 登录成功，风控信任密钥已消费（与 alist 一致：清空避免复用过期值）
        self.conf["creditkey"] = ""
        self.conf["review_url"] = ""
        self.conf["review_token"] = ""
        self.conf["review_key"] = ""
        self.conf["review_cookies"] = {}
        self._save()

    def apply_token(self, data: dict) -> None:
        self.conf["access_token"] = data.get("access_token", "")
        self.conf["token_type"] = data.get("token_type") or "Bearer"
        self.conf["refresh_token"] = (
            data.get("refresh_token") or self.conf.get("refresh_token", "")
        )
        self.conf["user_id"] = (
            data.get("user_id") or data.get("sub") or self.conf.get("user_id", "")
        )

    def refresh_access_token(self) -> None:
        refresh = self.conf.get("refresh_token") or ""
        if not refresh:
            raise XunleiError("尚未登录，请在「设置」里点「登录」")
        body = {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": sign.CLIENT_ID,
            "client_secret": sign.CLIENT_SECRET,
        }
        data = self.json_request(
            "POST", sign.XLUSER_API + "/auth/token",
            json_body=body, headers=self.base_headers(), timeout=30,
        )
        if not data.get("access_token"):
            # 不在后台用账号密码静默重登：那会让「退出登录」失效，也会反复触发风控
            raise XunleiError("登录态已失效，请在「设置」里点「登录」重新登录")
        self.apply_token(data)
        self._save()

    def ensure_session(self) -> None:
        """确保有可用登录态：有 access_token 直接放行，否则用 refresh_token 续期。

        不做「用账号密码静默重登」——需要人工登录时给出明确提示。
        """
        if self.conf.get("access_token"):
            return
        if self.conf.get("refresh_token"):
            self.refresh_access_token()
            return
        raise XunleiError("尚未登录，请在「设置」里点「登录」")

    def logout(self) -> None:
        for key in ("access_token", "refresh_token", "token_type", "user_id",
                    "user_name", "creditkey", "review_url", "review_token",
                    "review_key"):
            self.conf[key] = ""
        self.conf["review_cookies"] = {}
        self._save()

    def me(self) -> dict:
        """GET /v1/user/me：登录态校验 + 昵称。"""
        self.ensure_session()
        data = self.json_request(
            "GET", sign.XLUSER_API + "/user/me",
            headers=self.auth_headers(), timeout=15,
        )
        if err_code(data) in TOKEN_ERROR_CODES:
            self.refresh_access_token()
            data = self.json_request(
                "GET", sign.XLUSER_API + "/user/me",
                headers=self.auth_headers(), timeout=15,
            )
        if is_error(data):
            raise XunleiError(err_text("登录态校验失败", data))
        return data

    # ---------------------------------------------------------------- 风控短信验证
    def review_message(self, data: dict) -> str:
        """review_panel 风控：保存验证会话并给出可操作的指引。"""
        ds = sign.device_sign(self.device_id())
        url = (data.get("reviewurl") or "").strip()
        if url:
            url = url + "&deviceid=" + ds
        creditkey = (data.get("creditkey") or "").strip()
        if creditkey:
            self.conf["creditkey"] = creditkey
        self.conf["review_url"] = url
        # 新验证会话：运行态密钥重置为验证链接里的（旧短信令牌随旧会话失效）
        self.conf["review_key"] = (
            (parse_qs(urlsplit(url).query).get("creditkey") or [""])[0] or creditkey
        )
        self.conf["review_token"] = ""
        self._save()

        lines = ["本次登录触发迅雷风控验证（review_panel），需要短信验证一次："]
        if url:
            lines += [
                "1) 在本页「设置 → 短信验证」点「获取验证码」，输入短信 6 位码提交；",
                "2) 验证通过后点「重新登录」完成登录。",
            ]
        else:
            lines.append("但本次响应缺少 reviewurl，无法在面板内验证——请稍后重试。")
        if creditkey:
            lines.append("验证数据 creditkey=%s deviceid=%s（已自动填入）" % (creditkey, ds))
        if url:
            lines.append("备用验证链接（仅迅雷 App 内置浏览器可打开）：%s" % url)
        return "\n".join(lines)

    def review_state(self) -> dict:
        url = (self.conf.get("review_url") or "").strip()
        mobile = ""
        if url:
            mobile = (parse_qs(urlsplit(url).query).get("mobile") or [""])[0]
        return {
            "active": bool(url),
            "mobile": mobile,
            "creditkey": self.conf.get("creditkey", ""),
        }

    def review_payload(self, creditkey: str = None) -> dict:
        """sendsms/checksms 参数 = 验证链接查询串 ∪ 页面 baseParams 兜底值。"""
        query = {
            k: v[0] for k, v in
            parse_qs(urlsplit(self.conf.get("review_url") or "").query).items()
        }
        params = {
            "provideNname": "NONE", "netWorkType": "NONE", "providerName": "NONE",
            "protocolVersion": "301", "devicesign": sign.device_sign(self.device_id()),
            "platformVersion": "1", "fromPlatformVersion": "1",
            "timestamp": str(int(time.time() * 1000)), "sdkVersion": "1.0",
            "deviceModel": "M2004J7AC", "deviceName": "Xiaomi_M2004j7ac",
            "OSVersion": "12",
        }
        params.update(query)
        if params.get("deviceid"):
            # 页面把 URL 的 deviceid（device_sign 值）改名为 devicesign
            params["devicesign"] = params.pop("deviceid")
        params["smsEvent"] = "34"
        params["creditkey"] = (
            creditkey if creditkey is not None else self.review_creditkey()
        )
        return params

    def review_creditkey(self) -> str:
        """运行态信任密钥：续期值 > 验证链接里的 > 顶层 creditkey。"""
        return (
            (self.conf.get("review_key") or "").strip()
            or (parse_qs(urlsplit(self.conf.get("review_url") or "").query)
                .get("creditkey") or [""])[0]
            or (self.conf.get("creditkey") or "")
        )

    def set_review_key(self, key: str) -> None:
        self.conf["review_key"] = (key or "").strip()
        self._save()

    # 验证链路传输层（与浏览器页面同形）
    def review_base(self) -> str:
        query = {
            k: v[0] for k, v in
            parse_qs(urlsplit(self.conf.get("review_url") or "").query).items()
        }
        host = (query.get("mainHost") or "").strip().split(",")[0]
        host = host.replace("https://", "").replace("http://", "").strip("/")
        return ("https://" + host) if host else sign.XLUSER_BASE

    def review_cookies(self) -> dict:
        return dict(self.conf.get("review_cookies") or {})

    def review_http(self, method: str, url: str, **kwargs) -> xlhttp.HttpResult:
        """验证链路专用：浏览器 UA + cookie 罐，贯穿 sendsms/checksms/上报。"""
        headers = {"user-agent": sign.REVIEW_UA}
        jar = self.review_cookies()
        if jar:
            headers["cookie"] = "; ".join("%s=%s" % (k, v) for k, v in jar.items())
        result = xlhttp.request(method, url, headers=headers, timeout=30, **kwargs)
        changed = False
        for name, value in result.set_cookies():
            if value == "":
                if jar.pop(name, None) is not None:
                    changed = True      # 删除型 Set-Cookie
            elif jar.get(name) != value:
                jar[name] = value
                changed = True
        if changed:
            self.conf["review_cookies"] = jar
            self._save()
        return result

    @staticmethod
    def jsonp(text: str) -> dict:
        """JSONP 响应兜底解析：callback({...}) → dict。"""
        match = re.search(r"\((\{.*\})\)\s*;?\s*$", text or "", re.S)
        if not match:
            return {}
        try:
            data = json.loads(match.group(1))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def merge_review_result(self, result: xlhttp.HttpResult, *,
                            cookies_first: bool) -> dict:
        """结果 = JSON 体 ∪ Set-Cookie。

        sendsms/上报走 JSONP 读响应体；checksms 经跨域 iframe 表单提交，
        页面只能读 cookie（blogresult/securitytoken/token/creditkey/error 都在
        Set-Cookie 里）。
        """
        data = result.json()
        if not isinstance(data, dict):
            data = self.jsonp(result.text)
        for name, value in result.set_cookies():
            if cookies_first and value != "":
                data[name] = value          # 页面以 cookie 为地面真相
            else:
                data.setdefault(name, value)
        jar = self.conf.get("review_cookies") or {}
        dropped = False
        for stale in ("blogresult", "error"):
            if jar.pop(stale, None) is not None:
                dropped = True
        if dropped:
            self._save()
        return data

    def review_get(self, path: str, params: dict) -> dict:
        """GET + JSONP（页面 g.request）：附 csrf_token=md5(deviceid)。"""
        query = dict(params)
        seed = (self.review_cookies().get("deviceid") or self.device_id())[:32]
        query.setdefault("csrf_token", sign.md5_hex(seed))
        result = self.review_http("GET", self.review_base() + path, params=query)
        return self.merge_review_result(result, cookies_first=False)

    def review_post_form(self, path: str, params: dict) -> dict:
        """multipart 表单 POST（页面隐藏 iframe form，不带 csrf_token）。"""
        result = self.review_http("POST", self.review_base() + path, multipart=params)
        return self.merge_review_result(result, cookies_first=True)

    @staticmethod
    def err13(data: dict) -> bool:
        """会话失效：errorCode=13 或错误名 userinfo_expired。"""
        code = str(data.get("errorCode") or data.get("error_code") or "")
        return code == "13" or (data.get("error") or "").strip() == "userinfo_expired"

    def review_with_retry(self, kind: str, extra: dict = None, *,
                          retry_refresh: bool = True) -> dict:
        """一次点击内自动恢复会话失效（errorCode=13 / userinfo_expired）。"""
        renewed = refreshed = False
        for _ in range(3):
            if kind == "send":
                data = self.review_get(
                    "/xluser.core.login/v3/sendsms", self.review_payload()
                )
            else:
                data = self.review_post_form(
                    "/xluser.core.login/v3/checksms",
                    dict(self.review_payload(), **(extra or {})),
                )
            if not self.err13(data):
                return data
            new_key = (data.get("creditkey") or "").strip()
            if new_key and new_key != self.review_creditkey() and not renewed:
                renewed = True
                self.set_review_key(new_key)
                continue
            if not refreshed:
                refreshed = True
                self.refresh_review_session()
                if not retry_refresh:
                    raise XunleiError(
                        "验证码提交失败：验证会话已失效，已自动刷新——"
                        "原短信验证码随旧会话失效，请重新点「获取验证码」"
                    )
                continue
            tail = (
                "请点「重新登录」获取新的验证会话后重试，或用迅雷 App "
                "内置浏览器打开备用验证链接完成验证"
                if kind == "send" else "请重新点「获取验证码」获取新短信"
            )
            raise XunleiError("验证会话刷新后仍被迅雷拒绝（error_code=13）——" + tail)
        raise XunleiError("验证请求失败：自动重试次数超限")

    def refresh_review_session(self) -> None:
        """验证会话失效时重登一次，刷新 review_url / creditkey。"""
        username = (self.conf.get("login_name") or "").strip()
        password = self.conf.get("password") or ""
        if not username or not password:
            raise XunleiError("验证会话已失效，且未保存账号密码，无法刷新——请先填写账号密码")
        try:
            self.core_login(username, password)
        except XunleiError as exc:
            if "review_panel" in str(exc):
                return          # 已刷新为新验证会话
            raise

    def review_send_sms(self) -> dict:
        state = self.review_state()
        if not state["active"]:
            raise XunleiError("当前没有待完成的短信验证（请先点「登录」触发）")
        data = self.review_with_retry("send")
        code = str(data.get("errorCode") or data.get("error_code") or "0")
        if code in ("1007", "6"):
            raise XunleiError("该次验证需要图形验证，短信通道不可用——请用迅雷 App 打开备用验证链接")
        if code != "0":
            raise XunleiError(err_text("获取验证码失败", data))
        token = (data.get("token") or "").strip()
        if not token:
            raise XunleiError("获取验证码失败：响应缺少 token")
        if (data.get("creditkey") or "").strip():
            self.set_review_key(data["creditkey"].strip())
        self.conf["review_token"] = token
        self._save()
        return {"mobile": state["mobile"]}

    def review_report(self, data: dict) -> str:
        """checksms 成功后上报（页面 shiledReport，GET+JSONP）。"""
        query = {
            k: v[0] for k, v in
            parse_qs(urlsplit(self.conf.get("review_url") or "").query).items()
        }
        jar = self.review_cookies()
        params = {
            "type": "sms", "scene": "34", "userid": query.get("userID", ""),
            "data": data.get("securitytoken", ""),
            "result": data.get("blogresult", ""), "appid": query.get("appid", ""),
            "deviceid": jar.get("deviceid") or self.device_id(),
            "creditkey": jar.get("creditkey") or self.review_creditkey(),
        }
        try:
            result = self.review_get("/credit/v1/report", params)
        except XunleiError:
            return ""
        if str(result.get("code") or "") == "200":
            return (result.get("creditkey") or "").strip()
        return ""

    def review_check_sms(self, sms_code: str) -> dict:
        state = self.review_state()
        if not state["active"]:
            raise XunleiError("当前没有待完成的短信验证（请先点「登录」触发）")
        token = (self.conf.get("review_token") or "").strip()
        code = (sms_code or "").strip()
        if not token:
            raise XunleiError("请先点「获取验证码」")
        if not re.fullmatch(r"\d{6}", code):
            raise XunleiError("请输入 6 位数字验证码")
        data = self.review_with_retry(
            "check", {"token": token, "smsCode": code}, retry_refresh=False
        )
        if str(data.get("blogresult") or "") != "0":
            raise XunleiError(err_text("验证码提交失败", data))
        renewed = self.review_report(data)
        if renewed:
            self.conf["creditkey"] = renewed
        elif (data.get("creditkey") or "").strip():
            self.conf["creditkey"] = data["creditkey"].strip()
        else:
            self.conf["creditkey"] = self.review_creditkey()
        self.conf["review_url"] = ""
        self.conf["review_token"] = ""
        self.conf["review_key"] = ""
        self._save()
        return {
            "detail": "验证通过，请点「重新登录」完成登录",
            "creditkey": self.conf.get("creditkey", ""),
        }

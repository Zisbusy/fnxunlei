# -*- coding: utf-8 -*-
"""xl_drive.py —— 迅雷 drive/v1 客户端（远程设备专用）。

接口（与 xlapi.go / pyxunlei / alist thunder 驱动一致）：
  设备发现 GET  /drive/v1/tasks?type=user#runner        → tasks[].params.target
  链接解析 POST /drive/v1/resource/list                 {"page_size":2000,"urls":url}
  目录表   GET  <设备API>/drive/v1/files                filters kind=drive#folder
  建目录   POST <设备API>/drive/v1/files                kind=drive#folder
  建任务   POST /drive/v1/task                          type=user#download-url
  任务列表 GET  /drive/v1/tasks?space=&filters=...
  暂停恢复 PATCH /drive/v1/task                         set_params.spec={"phase":...}
  删除任务 DELETE /drive/v1/tasks?space=&task_ids=

两个关键结论（来自上游实机验证）：
1. 远程设备（user#runner 空间）的目录表**不能**用 api-pan 的 /drive/v1/files
   （只服务云盘，设备空间会被网关 502），必须先发现设备专属 API：
   GET /drive/v1/apps/INNER_API?space=<target> → link，形如
   https://<relay>/<path>/?plugin_app_id=X&plugin_app_token=Y，
   之后所有目录请求走 <ApiDomain>drive/v1/files 并带上 plugin 凭据。
2. 建任务必须带真实的 params.parent_folder_id / parent_folder_path，
   缺省时任务会被建出来但永远停在 PHASE_TYPE_PENDING（排队中 0%）。
"""

from __future__ import annotations

import json
import threading
import time
from urllib.parse import parse_qs, urlsplit, urlunsplit

import util
import xlhttp
import xl_sign as sign
from errors import XunleiError
from xl_auth import (CAPTCHA_ERROR_CODE, TOKEN_ERROR_CODES, XunleiAuth,
                     device_timeout_text, err_code, err_text, is_device_timeout,
                     is_error)

ACTIVE_PHASES = "PHASE_TYPE_PENDING,PHASE_TYPE_RUNNING"
TASK_TYPES = "user#download,user#download-url"
# 无 download_paths 提示时，按目录名兜底识别下载根
DOWNLOAD_ROOT_NAMES = {"download", "downloads", "下载", "下载目录"}

# 目录解析总预算（秒）：多形状阶梯 + 逐段建目录是同步链路，上游卡住时会拖垮
# 网关响应预算，超预算即止损。
RESOLVE_BUDGET = 18.0
# 解析结果复用窗口（秒）：批量派发同一目录只解析一次
RESOLVE_CACHE_TTL = 120.0
# 解析失败负缓存（秒）：设备插件持续超时时，避免每条链接都重烧整条失败链
NEG_TTL = 30.0

# 单次目录浏览最多返回的子目录数（超大目录不下发全部）
MAX_BROWSE_DIRS = 300

# 进程级缓存（key 已包含 space，天然按设备隔离）
_RELAY_CACHE = {}       # space -> {base, plugin_app_id, plugin_app_token, at}
_RESOLVE_CACHE = {}     # (space, dir) -> (at, (parent_id, parent_path))
_RESOLVE_NEG = {}       # (space, dir) -> (at, message)
_CACHE_LOCK = threading.Lock()


def scalar_int(value, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def agg_tries(tries: list) -> str:
    """聚合同因错误：相同 msg 的形状标签合并，避免错误文案被重复刷屏。"""
    groups = {}
    order = []
    plain = []
    for item in tries:
        label, sep, message = str(item).partition("：")
        if not sep:
            plain.append(str(item))
            continue
        if message not in groups:
            groups[message] = []
            order.append(message)
        groups[message].append(label)
    parts = list(plain)
    for message in order:
        labels = groups[message]
        head = "、".join(labels)
        if len(labels) > 1:
            head += "（%d 处同因）" % len(labels)
        parts.append("%s：%s" % (head, message))
    return "；".join(parts)


def norm_dir(path: str) -> str:
    """目录路径归一：clean + 尾斜杠（/movies/2025 → /movies/2025/）。"""
    value = (path or "").replace("\\", "/").strip()
    if not value:
        return ""
    segments = [s for s in value.split("/") if s]
    if not segments:
        return "/"
    return "/" + "/".join(segments) + "/"


def folder_path(folder: dict) -> str:
    """目录项的真实路径：params.RealPath 优先，AliasPath 兜底。"""
    params = folder.get("params") or {}
    for key in ("RealPath", "realPath", "real_path",
                "AliasPath", "aliasPath", "alias_path"):
        value = norm_dir(str(params.get(key) or ""))
        if value:
            return value
    return ""


def clip_timeout(deadline, per_call: float) -> float:
    """把单次请求超时裁到解析预算剩余时间内。"""
    if deadline is None:
        return per_call
    return max(1.0, min(per_call, deadline - time.monotonic()))


def _cache_set(cache: dict, key, value) -> None:
    with _CACHE_LOCK:
        if len(cache) > 256:
            cache.clear()
        cache[key] = value


def _cache_get(cache: dict, key):
    with _CACHE_LOCK:
        return cache.get(key)


def _cache_pop(cache: dict, key) -> None:
    with _CACHE_LOCK:
        cache.pop(key, None)


class DriveClient:
    """参数化的 drive/v1 客户端（本应用只服务远程设备通道）。"""

    def __init__(self, auth: XunleiAuth, base_url: str = sign.DRIVE_API):
        self.auth = auth
        self.base_url = (base_url or sign.DRIVE_API).rstrip("/")
        # 设备端超时 GET 重试次数（默认 2，共 3 次尝试）
        self.device_timeout_retries = 2

    # ------------------------------------------------------------ 请求
    def _request(self, method: str, path: str, *, params: dict = None,
                 json_body: dict = None, timeout: float = 30,
                 deadline: float = None) -> dict:
        """drive/v1 请求：按错误码分流（令牌续期 / 人机令牌 / 设备超时重试）。

        迅雷错误多以 HTTP 200 或 4xx + error_code 返回，必须按错误码判断，
        不能只看 HTTP 状态码——否则暂停/删除的失败会被当成成功。
        """
        url = path if path.startswith("http") else self.base_url + path
        action = method + ":" + urlsplit(url).path

        for attempt in range(3):
            result = xlhttp.request(
                method, url, params=params, json_body=json_body,
                headers=self.auth.auth_headers(), timeout=timeout,
            )
            data = result.json()
            if not isinstance(data, dict):
                raise XunleiError(
                    "迅雷接口响应异常（HTTP %s）：%s" % (result.status, result.text[:120])
                )
            if not is_error(data):
                return data

            code = err_code(data)
            if attempt < 2 and code in TOKEN_ERROR_CODES:
                self.auth.refresh_access_token()
                continue
            if attempt < 2 and code == CAPTCHA_ERROR_CODE:
                # 登录后刷新（alist RefreshCaptchaTokenAtLogin 语义）
                self.auth.refresh_captcha_at_login(
                    action, self.auth.conf.get("user_id", "")
                )
                continue
            if (attempt < self.device_timeout_retries and method == "GET"
                    and is_device_timeout(data)
                    and (deadline is None or time.monotonic() < deadline)):
                # 设备端超时多为瞬态，GET 幂等可重试；POST 不重试（建目录非幂等）
                util.log("drive GET 设备超时重试: %s %s" % (method, urlsplit(url).path))
                time.sleep(1)
                continue

            if code == CAPTCHA_ERROR_CODE:
                uid = self.auth.conf.get("user_id", "")
                raise XunleiError(
                    "迅雷接口错误：人机令牌校验失败（error_code=9）——"
                    "这不是短信验证码输错；请点「登录」重试，若仍失败请重新完成一次短信验证"
                    "（user_id=%s）" % ("已取到" if uid else "空")
                )
            if is_device_timeout(data):
                raise XunleiError(device_timeout_text(data))
            raise XunleiError(err_text("迅雷接口错误", data))

        raise XunleiError("迅雷接口错误：重试后仍失败")

    # ------------------------------------------------------------ 设备
    def device_targets(self) -> list:
        """runner 任务即设备空间：[{target, name, online}]。"""
        data = self._request("GET", "/tasks", params={"type": "user#runner"}, timeout=10)
        out = []
        for task in data.get("tasks", []) or []:
            params = task.get("params") or {}
            target = str(params.get("target") or "").strip()
            if not target:
                continue
            out.append({
                "target": target,
                "name": task.get("name") or target,     # 设备名
                "phase": task.get("phase") or "",
            })
        return out

    # ------------------------------------------------------------ 资源解析
    def parse_resource(self, url: str) -> dict:
        data = self._request(
            "POST", "/resource/list",
            json_body={"page_size": 2000, "urls": url}, timeout=25,
        )
        resources = ((data.get("list") or {}).get("resources")) or []
        if not resources:
            raise XunleiError("链接解析失败：迅雷未返回资源信息（链接可能已失效或不受支持）")
        item = resources[0]
        return {
            "name": item.get("name") or item.get("file_name") or "",
            "size": scalar_int(item.get("file_size")),
            "file_count": max(1, scalar_int(item.get("file_count"), 1)),
            "mime_type": item.get("mime_type") or "",
            "file_id": item.get("file_id") or "",
        }

    # ------------------------------------------------ 设备专属 API（INNER_API）
    def relay_api(self, space: str, refresh: bool = False, deadline: float = None) -> dict:
        """发现远程设备的专属 API 网关（XunLei.Cloud SDK GetClientApi 同构）。"""
        now = time.time()
        cached = _cache_get(_RELAY_CACHE, space)
        if cached and not refresh and now - cached.get("at", 0) < 300:
            return cached

        data = self._request(
            "GET", self.base_url + "/apps/INNER_API", params={"space": space},
            timeout=clip_timeout(deadline, 10), deadline=deadline,
        )
        url = (data.get("link") or "").strip()
        if not url:
            for item in (data.get("links") or []):
                if isinstance(item, dict) and (item.get("url") or "").strip():
                    url = item["url"].strip()
                    break
        if not url:
            raise XunleiError("设备专属 API 发现失败：INNER_API 响应缺少 link/links[].url")

        parts = urlsplit(url)
        base = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        if not base.endswith("/"):
            base += "/"      # SDK 是 ApiDomain + 相对路径的字符串拼接
        query = parse_qs(parts.query)
        entry = {
            "base": base,
            "plugin_app_id": (query.get("plugin_app_id") or [""])[0],
            "plugin_app_token": (query.get("plugin_app_token") or [""])[0],
            "at": now,
        }
        _cache_set(_RELAY_CACHE, space, entry)
        util.log(
            "设备API发现: space=%s relay=%s plugin_token=%s"
            % (space[:12], parts.netloc + parts.path,
               "有" if entry["plugin_app_token"] else "无")
        )
        return entry

    def relay_params(self, api: dict, space: str) -> dict:
        params = {"device_space": "", "space": space}
        if api.get("plugin_app_id"):
            params["plugin_app_id"] = api["plugin_app_id"]
        if api.get("plugin_app_token"):
            params["plugin_app_token"] = api["plugin_app_token"]
        return params

    def relay_folder_shapes(self, api: dict, space: str, parent_id: str) -> list:
        """设备 API 目录表参数形状（SDK GetClientPath 反编译实锤）。"""
        filters_full = json.dumps({
            "trashed": {"eq": False},
            "phase": {"eq": "PHASE_TYPE_COMPLETE"},
            "kind": {"eq": "drive#folder"},
        }, separators=(",", ":"))
        filters_kind = json.dumps({"kind": {"eq": "drive#folder"}},
                                  separators=(",", ":"))
        base = self.relay_params(api, space)
        full = dict(
            base, parent_id=parent_id or "", limit="200",
            with_audit="true", filters=filters_full, page_token="",
            order="TYPE_DESC",
            **{"with": ["withCategoryDiskMountPath",
                        "withCategoryHistoryDownloadPath"]}
        )
        kind_only = dict(base, parent_id=parent_id or "", limit="200",
                         filters=filters_kind)
        return [("设备API完整形状", full), ("设备API极简形状", kind_only)]

    def folder_shapes_fallback(self, space: str, parent_id: str) -> list:
        """设备 API 发现失败时的 api-pan 回退形状（兼容未来开放变化）。"""
        filters = json.dumps({"kind": {"eq": "drive#folder"}}, separators=(",", ":"))
        standard = {"space": space, "limit": "200",
                    "parent_id": parent_id or "", "filters": filters}
        minimal = {"space": space, "limit": "200", "parent_id": parent_id or ""}
        return [("标准形状", standard), ("极简形状", minimal)]

    def folders(self, space: str, parent_id: str = "", deadline: float = None) -> list:
        """目录表查询：设备专属 API 优先，仅发现失败时回退 api-pan 阶梯。"""
        tries = []
        discovered = False

        for refresh in (False, True):     # 第二轮强制重发现（凭据过期自愈）
            if deadline is not None and time.monotonic() >= deadline:
                tries.append("预算耗尽未及尝试")
                break
            try:
                api = self.relay_api(space, refresh=refresh, deadline=deadline)
            except XunleiError as exc:
                tries.append("设备API发现：%s" % util.clip(exc, 100))
                break
            discovered = True
            saw_ok = False
            for label, params in self.relay_folder_shapes(api, space, parent_id):
                if deadline is not None and time.monotonic() >= deadline:
                    tries.append("预算耗尽未及尝试")
                    break
                try:
                    data = self._request(
                        "GET", api["base"] + "drive/v1/files", params=params,
                        timeout=clip_timeout(deadline, 8), deadline=deadline,
                    )
                except XunleiError as exc:
                    tries.append("%s：%s" % (label, util.clip(exc, 100)))
                    continue
                saw_ok = True
                files = [
                    item for item in (data.get("files") or [])
                    if isinstance(item, dict)
                    and item.get("kind") in (None, "", "drive#folder")
                ]
                if files:
                    return files
                tries.append("%s：空表" % label)
            if saw_ok:                    # 设备 API 可达但目录为空：如实返回
                return []

        if discovered:                    # relay 全挂 ≠ api-pan 能救
            raise XunleiError(
                "云端目录表查询失败（设备API 各形状均失败，未回退 api-pan"
                "——设备空间的 api-pan /files 只会 502）：" + agg_tries(tries)
            )

        util.log("设备API发现失败，回退 api-pan: %s" % str(tries)[:120])
        for label, params in self.folder_shapes_fallback(space, parent_id):
            if deadline is not None and time.monotonic() >= deadline:
                tries.append("预算耗尽未及尝试")
                break
            try:
                data = self._request(
                    "GET", "/files", params=params,
                    timeout=clip_timeout(deadline, 8), deadline=deadline,
                )
            except XunleiError as exc:
                tries.append("api-pan %s：%s" % (label, util.clip(exc, 100)))
                continue
            files = [
                item for item in (data.get("files") or [])
                if isinstance(item, dict)
                and item.get("kind") in (None, "", "drive#folder")
            ]
            if files:
                return files
            tries.append("api-pan %s：空表" % label)
        raise XunleiError("云端目录表查询失败：" + agg_tries(tries))

    def make_folder(self, space: str, parent_id: str, name: str,
                    deadline: float = None) -> dict:
        """建子目录：设备 API 优先，发现失败才回退 api-pan。"""
        try:
            api = self.relay_api(space, deadline=deadline)
        except XunleiError:
            api = None

        if api:
            data = self._request(
                "POST", api["base"] + "drive/v1/files",
                params=self.relay_params(api, space),
                json_body={"parent_id": parent_id, "name": name,
                           "space": space, "kind": "drive#folder"},
                timeout=clip_timeout(deadline, 10), deadline=deadline,
            )
        else:
            data = self._request(
                "POST", "/files",
                json_body={"parent_id": parent_id, "name": name,
                           "space": space, "kind": "drive#folder"},
                timeout=clip_timeout(deadline, 10), deadline=deadline,
            )
        file_id = (data.get("file") or {}).get("id") or ""
        if not file_id:
            raise XunleiError("创建下载子目录 %s 失败：响应缺少 file.id" % name)
        return {"id": file_id, "name": name, "params": {"RealPath": ""}}

    def device_config(self, space: str, deadline: float = None) -> dict:
        """设备配置（download_paths 下载根提示）；失败静默返回 {}。"""
        if space:
            try:
                api = self.relay_api(space, deadline=deadline)
                return self._request(
                    "GET", api["base"] + "device/config",
                    params=self.relay_params(api, space),
                    timeout=clip_timeout(deadline, 10), deadline=deadline,
                )
            except XunleiError:
                pass
        root = (self.base_url[:-len("/drive/v1")]
                if self.base_url.endswith("/drive/v1") else self.base_url)
        try:
            return self._request("GET", root + "/device/config",
                                 timeout=clip_timeout(deadline, 8))
        except XunleiError:
            return {}

    # ------------------------------------------------------------ 下载根解析
    def resolve_dest_folder(self, space: str, directory: str = "") -> tuple:
        """解析下载目标目录 → (parent_folder_id, parent_folder_path)。

        directory 为下载根下的相对路径（如 movies/2025），中间层级缺失时自动创建。
        结果按 (space, directory) 缓存，失败进 30s 负缓存。
        """
        key = (space, (directory or "").strip())
        cached = _cache_get(_RESOLVE_CACHE, key)
        if cached and time.monotonic() - cached[0] < RESOLVE_CACHE_TTL:
            return cached[1]
        negative = _cache_get(_RESOLVE_NEG, key)
        if negative and time.monotonic() - negative[0] < NEG_TTL:
            raise XunleiError(
                "%s（%d 秒内已失败过，稍后自动重试）" % (negative[1], int(NEG_TTL))
            )

        try:
            result = self._resolve_uncached(space, directory,
                                            time.monotonic() + RESOLVE_BUDGET)
        except XunleiError as exc:
            _cache_set(_RESOLVE_NEG, key, (time.monotonic(), str(exc)))
            raise
        _cache_pop(_RESOLVE_NEG, key)
        _cache_set(_RESOLVE_CACHE, key, (time.monotonic(), result))
        return result

    def harvest_root(self, space: str, tasks: list = None):
        """目录表不可用时的兜底：从历史任务的 params 反查真实 parent_folder_id。"""
        if tasks is None:
            try:
                tasks = self.list_tasks(space)
            except XunleiError as exc:
                util.log("历史任务反查失败: %s" % util.clip(exc, 80))
                return None
        best = None
        for task in tasks or []:
            params = task.get("params") if isinstance(task.get("params"), dict) else {}
            parent_id = str(params.get("parent_folder_id") or "").strip()
            if not parent_id:
                continue
            parent_path = str(params.get("parent_folder_path") or "").strip()
            if parent_path:
                return parent_id, parent_path
            best = best or (parent_id, "")
        return best

    def _resolve_uncached(self, space: str, directory: str, deadline: float) -> tuple:
        folders = None
        try:
            folders = self.folders(space, "", deadline=deadline)
            if not folders:
                raise XunleiError(
                    "引擎目录表为空，无法解析下载根目录"
                    "（缺少 parent_folder_id 时任务不会开始下载）"
                )
        except XunleiError as exc:
            harvested = self.harvest_root(space)
            if harvested is None:
                raise XunleiError(
                    "%s（历史任务反查也不可用：该设备没有带 parent_folder_id 的历史任务。"
                    "可先用官方迅雷 App 对该设备建一个下载任务（选好下载目录），"
                    "任务参数会带上 parent_folder_id，反查即恢复；"
                    "或检查目标设备上的迅雷远程插件是否在运行）" % exc
                ) from exc
            util.log(
                "目录表不可用，历史任务反查兜底: space=%s → id=%s path=%s"
                % (space[:12], harvested[0][:16], harvested[1][:32])
            )
            return harvested[0], self._with_subdir(space, *harvested, directory,
                                                   deadline=deadline)

        hint = ""
        for path in (self.device_config(space, deadline=deadline).get("download_paths") or []):
            if str(path).strip():
                hint = norm_dir(str(path))
                break

        root_id, root_path = "", ""
        if hint:                              # 1) 配置提示精确匹配
            for folder in folders:
                if folder_path(folder) == hint:
                    root_id, root_path = str(folder.get("id")), hint
                    break
        if not root_id and len(folders) == 1:  # 2) 唯一顶层目录
            root_id = str(folders[0].get("id"))
            root_path = folder_path(folders[0]) or hint
        if not root_id:                       # 3) 常见下载根名
            for folder in folders:
                name = (folder.get("name") or "").strip().lower()
                if folder_path(folder) in ("/downloads/", "/") or name in DOWNLOAD_ROOT_NAMES:
                    root_id = str(folder.get("id"))
                    root_path = folder_path(folder) or hint
                    break
        if not root_id:                       # 4) pyxunlei 启发式
            parent_id = str(folders[0].get("parent_id") or "").strip()
            root_id = parent_id or str(folders[0].get("id"))
            root_path = hint
        if not root_id:
            raise XunleiError("无法识别下载根目录（parent_folder_id 解析失败）")
        return self._with_subdir(space, root_id, root_path, directory, deadline=deadline)

    def _with_subdir(self, space: str, root_id: str, root_path: str,
                     directory: str, deadline: float) -> tuple:
        """在下载根下逐段定位/创建目录，返回最深的 (id, path)。"""
        dest = (directory or "").strip()
        if not dest:
            return root_id, root_path

        normalized = norm_dir(dest)
        if root_path and normalized == root_path:
            return root_id, root_path
        if root_path and normalized.startswith(root_path):
            rel = normalized[len(root_path):]        # 根下绝对路径 → 相对段
        elif dest.startswith("/"):
            # 根外/根未知的绝对路径：保留根 id 让任务能开始下载，路径原样交给引擎
            return root_id, dest
        else:
            rel = normalized.lstrip("/")

        target = norm_dir((root_path or "") + rel)
        parent_id, parent_path = root_id, root_path
        try:
            for segment in [s for s in rel.split("/") if s]:
                child = None
                for folder in self.folders(space, parent_id, deadline=deadline):
                    if ((folder.get("name") or "").strip() == segment
                            or folder_path(folder) == parent_path + segment + "/"):
                        child = folder
                        break
                if child is None:
                    child = self.make_folder(space, parent_id, segment, deadline=deadline)
                parent_id = str(child.get("id"))
                parent_path = parent_path + segment + "/"
        except XunleiError:
            # 子目录解析失败（常见：引擎账号对该目录无读写权）→ 保留已解析到的目录，
            # 完整目标路径交给引擎尽力落盘
            util.log("子目录解析降级: space=%s 目标=%s → 用 %s" % (space, target, parent_id))
            return parent_id, target
        return parent_id, parent_path

    # ------------------------------------------------------------ 目录浏览（只读）
    def _child_named(self, space: str, parent_id: str, name: str,
                     deadline: float = None):
        for folder in self.folders(space, parent_id, deadline=deadline):
            if (folder.get("name") or "").strip() == name:
                return folder
        return None

    def browse_dirs(self, space: str, relative: str = "") -> dict:
        """只读浏览远程设备「下载根目录」下的子目录（不建目录、不派发任务）。

        返回 {root, path, parent, dirs:[{name,path}], truncated}，
        path 是相对下载根的路径（如 movies/2025），根目录为空串。
        目录表查不到（设备端插件超时等）会抛 XunleiError，由界面回退到手动输入。
        """
        root_id, root_path = self.resolve_dest_folder(space, "")
        parent_id, current = root_id, ""
        for segment in [s for s in (relative or "").split("/") if s]:
            child = self._child_named(space, parent_id, segment)
            if child is None:
                raise XunleiError("远程设备上没有目录：%s（根目录 %s）"
                                  % (segment, root_path or "/"))
            parent_id = str(child.get("id"))
            current = (current + "/" + segment).strip("/")

        dirs = []
        for folder in self.folders(space, parent_id):
            name = (folder.get("name") or "").strip()
            if not name:
                continue
            dirs.append({"name": name, "path": (current + "/" + name).strip("/")})
        dirs.sort(key=lambda item: item["name"].lower())

        return {
            "root": root_path,
            "path": current,
            "parent": current.rpartition("/")[0] if current else None,
            "dirs": dirs[:MAX_BROWSE_DIRS],
            "truncated": len(dirs) > MAX_BROWSE_DIRS,
        }

    # ------------------------------------------------------------ 建任务
    def create_task(self, url: str, resource: dict, space: str, *,
                    directory: str = "") -> str:
        """创建下载任务；parent_folder_id 为空时按 directory 解析真实下载目录。"""
        try:
            parent_id, parent_path = self.resolve_dest_folder(
                space, (directory or "").strip()
            )
        except XunleiError as exc:
            util.log("目录解析失败: dir=%r → %s"
                     % ((directory or "")[:40], util.clip(exc, 120)))
            raise

        name = resource.get("name") or url[:120]
        body = {
            "type": "user#download-url",
            "name": name,
            "file_name": name,
            "file_size": str(resource.get("size") or "0"),
            "space": space,
            "params": {
                "target": space,
                "url": url,
                "total_file_count": str(resource.get("file_count") or "1"),
                "parent_folder_path": parent_path,
                "parent_folder_id": parent_id,
                "sub_file_index": "--1,",
                "mime_type": resource.get("mime_type") or "",
                "file_id": resource.get("file_id") or "",
            },
        }
        data = self._request("POST", "/task", json_body=body, timeout=30)
        task = data.get("task") or data
        task_id = task.get("id") or ""
        if not task_id:
            raise XunleiError(
                "创建任务失败：%s" % json.dumps(data, ensure_ascii=False)[:200]
            )
        util.log("建任务成功: id=%s parent=%s" % (task_id, parent_id[:16]))
        return str(task_id)

    # ------------------------------------------------------------ 任务列表
    def list_tasks(self, space: str) -> list:
        """任务列表（按 page_token 翻页，封顶 6 页）。

        本应用不再展示任务面板，但「下载根目录」在目录表不可用时需要靠历史任务
        反查 parent_folder_id，因此保留这个只读方法。
        """
        filters = json.dumps({"type": {"in": TASK_TYPES}}, separators=(",", ":"))
        out = []
        page_token = ""
        for _ in range(6):
            params = {"space": space, "filters": filters, "limit": "200"}
            if page_token:
                params["page_token"] = page_token
            data = self._request("GET", "/tasks", params=params, timeout=12)
            out.extend(data.get("tasks", []) or [])
            page_token = str(
                data.get("next_page_token") or data.get("nextPageToken") or ""
            ).strip()
            if not page_token:
                break
        return out

# -*- coding: utf-8 -*-
"""xl_channel.py —— 「迅雷远程通道」对外的唯一门面。

本应用只做一件事：**把下载链接推送到迅雷远程设备**。
  账号登录（含风控短信验证）→ 发现账号绑定的远程设备 → 解析设备下载目录 →
  推送任务（自动建目录）→ 返回任务 id。

任务由远程设备执行，走迅雷 P2SP 加速；本机只发控制请求，不落地任何文件。
"""

from __future__ import annotations

import time

import links as linksmod
import store
import util
from errors import XunleiError
from xl_auth import XunleiAuth
from xl_drive import DriveClient

CHANNEL_KEY = "xunlei"

# 目标设备空间的内存缓存（按账号隔离，120s），避免每次请求都拉设备列表
_TARGET_CACHE = {"user": "", "target": "", "at": 0.0}
TARGET_TTL = 120.0


class XunleiRemote:
    """一次请求一个实例：配置从磁盘读，登录态变化立即落盘。"""

    def __init__(self):
        self.conf = store.load_config()
        self.auth = XunleiAuth(self.conf, self._persist)
        self.drive = DriveClient(self.auth)

    def _persist(self, conf: dict) -> None:
        """登录态落盘；保持 self.conf 与 auth.conf 指向同一个 dict。"""
        self.conf = conf
        self.auth.conf = conf
        store.save_config(conf)

    # ------------------------------------------------------------ 状态
    def configured(self) -> bool:
        has_password = bool((self.conf.get("login_name") or "").strip()
                            and (self.conf.get("password") or ""))
        return has_password or bool(self.conf.get("refresh_token")
                                    or self.conf.get("access_token"))

    def status(self, with_devices: bool = True) -> dict:
        """账号 / 登录态 / 远程设备概览。"""
        review = self.auth.review_state()
        if not self.configured():
            return {
                "configured": False,
                "reachable": False,
                "detail": "未配置迅雷账号：请在「设置」里填写手机号/邮箱与密码后点「登录」",
                "user": "",
                "devices": [],
                "target": "",
                "review": review,
            }

        try:
            self.auth.ensure_session()
            me = self.auth.me()
            name = (me.get("name") or me.get("nickname")
                    or me.get("userName") or "")
            if name and name != self.conf.get("user_name"):
                self.conf["user_name"] = name
                self._persist(self.conf)

            devices = []
            detail = "已登录"
            if with_devices:
                devices = self.drive.device_targets()
                detail = "已登录，远程设备 %d 台" % len(devices)
                target = (self.conf.get("target") or "").strip()
                online = [item["target"] for item in devices]
                if target and online and target not in online:
                    detail += "；⚠ 所选目标设备已不在线"
                if not target and not online:
                    detail += "；⚠ 账号下没有远程设备，请先在迅雷 App 里绑定远程下载设备"
            return {
                "configured": True,
                "reachable": True,
                "detail": detail,
                "user": self.conf.get("user_name", "") or self.conf.get("login_name", ""),
                "devices": devices,
                "target": (self.conf.get("target") or "").strip(),
                "review": review,
            }
        except XunleiError as exc:
            return {
                "configured": True,
                "reachable": False,
                "detail": util.sanitize_text(str(exc)),
                "user": self.conf.get("user_name", "") or self.conf.get("login_name", ""),
                "devices": [],
                "target": (self.conf.get("target") or "").strip(),
                "review": self.auth.review_state(),
            }

    # ------------------------------------------------------------ 登录
    def login(self) -> dict:
        if not (self.conf.get("login_name") or "").strip():
            raise XunleiError("请先填写迅雷账号（手机号或邮箱）")
        if not (self.conf.get("password") or ""):
            raise XunleiError("请先填写迅雷账号密码")
        self.auth.login()
        _TARGET_CACHE.update({"user": "", "target": "", "at": 0.0})
        util.log("迅雷账号登录成功: user_id=%s" % (self.conf.get("user_id") or "-"))
        return self.status()

    def logout(self) -> None:
        self.auth.logout()
        _TARGET_CACHE.update({"user": "", "target": "", "at": 0.0})

    # ------------------------------------------------------------ 设备
    def devices(self) -> list:
        self.auth.ensure_session()
        return self.drive.device_targets()

    def target_of(self) -> str:
        """目标设备空间：设置里选定的优先；未选时用账号下第一台在线设备。"""
        configured = (self.conf.get("target") or "").strip()
        if configured:
            return configured

        account = self.conf.get("user_id") or self.conf.get("login_name") or ""
        cached = _TARGET_CACHE
        if (cached["target"] and cached["user"] == account
                and time.time() - cached["at"] < TARGET_TTL):
            return cached["target"]

        online = [item["target"] for item in self.devices()]
        if not online:
            raise XunleiError(
                "当前迅雷账号下未发现远程下载设备——请先在迅雷 App 里绑定远程下载设备，"
                "或在「设置 → 下载位置」里手动指定"
            )
        cached.update({"user": account, "target": online[0], "at": time.time()})
        return online[0]

    # ------------------------------------------------------------ 推送
    def add(self, url: str, name: str = None, directory: str = None) -> dict:
        """解析链接 → 解析/创建目录 → 建任务；返回任务 id 等结果。"""
        self.auth.ensure_session()
        space = self.target_of()
        resource = self.drive.parse_resource(url)
        if name:
            resource["name"] = name
        target_dir = (directory or "").strip() or (self.conf.get("dir") or "")
        task_id = self.drive.create_task(url, resource, space, directory=target_dir)
        store.add_record({
            "backend": CHANNEL_KEY,
            "id": str(task_id),
            "url": url,
            "name": resource.get("name") or url[:120],
            "dir": target_dir,
            "created_at": int(time.time()),
            "identity": linksmod.identity(url),
        })
        return {
            "id": str(task_id),
            "name": resource.get("name") or url[:120],
            "dir": target_dir,
            "size": resource.get("size") or 0,
        }

    # ------------------------------------------------------------ 目录浏览
    def browse_dirs(self, path: str = "", target: str = "") -> dict:
        """浏览远程设备下载根下的子目录（只读，不建目录）。

        target 为界面上当前选中的设备（可能尚未保存），为空则用已保存/自动发现的目标。
        """
        self.auth.ensure_session()
        space = (target or "").strip() or self.target_of()
        return self.drive.browse_dirs(space, (path or "").strip())

    # ------------------------------------------------------------ 风控短信验证
    def review_state(self) -> dict:
        return self.auth.review_state()

    def review_send(self) -> dict:
        return self.auth.review_send_sms()

    def review_check(self, code: str) -> dict:
        return self.auth.review_check_sms(code)

    # ------------------------------------------------------------ 自诊断（API only）
    def diagnose(self) -> dict:
        """分层自检：配置 → 登录态 → 设备 → 设备API → 目录表。

        只读探测（不建任务、不建目录），用于在 NAS 上通过 curl 定位问题。
        """
        steps = []

        def add(name, ok, detail):
            steps.append({"name": name, "ok": bool(ok), "detail": util.sanitize_text(detail)})

        if not self.configured():
            add("账号配置", False, "未填写账号密码")
            return {"ok": False, "steps": steps}
        add("账号配置", True, "账号：%s" % ((self.conf.get("login_name") or "-")[:32]))

        try:
            me = self.auth.me()
            name = me.get("name") or me.get("nickname") or me.get("userName") or ""
            add("登录态", True, "已登录%s" % (("（%s）" % name) if name else ""))
        except XunleiError as exc:
            add("登录态", False, str(exc))
            return {"ok": False, "steps": steps}

        try:
            devices = self.drive.device_targets()
            names = "、".join(item.get("name") or item["target"][:8] for item in devices)
            add("远程设备", bool(devices),
                ("发现 %d 台：%s" % (len(devices), names)) if devices
                else "账号下没有远程设备，请先在迅雷 App 绑定")
            if not devices:
                return {"ok": False, "steps": steps}
        except XunleiError as exc:
            add("远程设备", False, str(exc))
            return {"ok": False, "steps": steps}

        try:
            space = self.target_of()
            add("目标设备", True, "target=%s…" % space[:12])
        except XunleiError as exc:
            add("目标设备", False, str(exc))
            return {"ok": False, "steps": steps}

        try:
            api = self.drive.relay_api(space, refresh=True)
            add("设备专属API", True, "relay=%s" % api["base"][:80])
        except XunleiError as exc:
            add("设备专属API", False, str(exc))

        try:
            folders = self.drive.folders(space, "")
            add("目录表", bool(folders), "顶层目录 %d 个" % len(folders))
        except XunleiError as exc:
            add("目录表", False, str(exc))

        return {"ok": all(step["ok"] for step in steps), "steps": steps}

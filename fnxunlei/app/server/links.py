# -*- coding: utf-8 -*-
"""links.py —— 下载链接解析（magnet / thunder / http(s) / ftp / ed2k）。

在本地先做结构与格式校验，再交给迅雷云端解析资源元数据：
- magnet  ：xt=urn:btih 信息哈希（40 位 hex 或 32 位 base32）、dn、tr
- thunder ：base64 套壳（AA…ZZ）解码后按内层类型再解析
- ed2k    ：解析出文件名/大小/哈希供展示，但迅雷远程通道不支持派发
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote, unquote, urlparse

# 可派发到迅雷远程通道的链接类型
DISPATCHABLE = {"magnet", "http", "https", "ftp", "torrent"}
# 只解析展示、不派发
UNSUPPORTED = {"ed2k"}

HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")
BASE32 = re.compile(r"^[A-Z2-7]{32}$")
BTIH_URN = re.compile(r"^urn:btih:([0-9a-zA-Z]{32,40})$", re.I)


@dataclass
class LinkInfo:
    kind: str                 # magnet|http|https|ftp|ed2k|torrent|unknown
    url: str                  # 归一化后的链接（thunder 解码后为内层链接）
    display: str = ""         # 展示名（磁力 dn / ed2k 文件名 / URL 尾段）
    ok: bool = True           # 结构是否可解析
    dispatchable: bool = True # 能否派发给迅雷远程通道
    reason: str = ""          # 不可用原因
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.ok:
            self.dispatchable = False

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "url": self.url,
            "display": self.display,
            "ok": self.ok,
            "dispatchable": self.dispatchable,
            "reason": self.reason,
        }


# ---------------------------------------------------------------- thunder
def decode_thunder(url: str):
    """thunder://<base64(AA<真实链接>ZZ)> → 真实链接；失败返回 None。"""
    payload = url[len("thunder://"):].strip().rstrip("/")
    payload += "=" * (-len(payload) % 4)   # 补 padding，兼容 urlsafe 变体
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            raw = decoder(payload).decode("utf-8", errors="replace")
        except (ValueError, UnicodeDecodeError):
            continue
        if raw.startswith("AA") and raw.endswith("ZZ"):
            return raw[2:-2]
        if raw.startswith(("http://", "https://", "ftp://", "magnet:", "ed2k:")):
            return raw
    return None


# ---------------------------------------------------------------- magnet
def parse_magnet(url: str) -> LinkInfo:
    info = LinkInfo(kind="magnet", url=url)
    query = url.split("?", 1)[1] if "?" in url else ""
    params = parse_qs(query, keep_blank_values=True)

    infohash = ""
    for xt in params.get("xt", []):
        match = BTIH_URN.match(xt.strip())
        if match:
            infohash = match.group(1)
            break
    if not infohash:
        info.ok = False
        info.reason = "磁力链接缺少有效的 xt=urn:btih 信息哈希"
        return info

    if HEX40.match(infohash):
        algo, digest = "hex", infohash.lower()
    elif BASE32.match(infohash):
        algo, digest = "base32", infohash.upper()
    else:
        info.ok = False
        info.reason = "信息哈希格式不正确（应为 40 位 hex 或 32 位 base32）"
        return info

    dn = unquote((params.get("dn") or [""])[0])
    trackers = [unquote(t) for t in params.get("tr", []) if t]
    info.display = dn or "magnet:%s…" % digest[:8]
    info.meta = {
        "infohash": digest,
        "infohash_algo": algo,
        "dn": dn,
        "trackers": trackers,
    }
    return info


# ---------------------------------------------------------------- ed2k
def parse_ed2k(url: str) -> LinkInfo:
    """ed2k://|file|<name>|<size>|<md4>|/ —— 只解析结构，不派发。"""
    info = LinkInfo(kind="ed2k", url=url, dispatchable=False)
    parts = url.split("|")
    if len(parts) < 5 or parts[1] != "file":
        info.ok = False
        info.reason = "ed2k 链接格式不正确"
        return info
    name = unquote(parts[2])
    try:
        size = int(parts[3])
    except ValueError:
        size = 0
    digest = parts[4].lower()
    info.display = name or url[:64]
    info.meta = {"name": name, "size": size, "hash": digest}
    info.reason = "迅雷远程通道不支持 ed2k 协议（仅解析展示）"
    return info


# ---------------------------------------------------------------- 直链
def parse_direct(url: str) -> LinkInfo:
    parsed = urlparse(url)
    kind = parsed.scheme.lower()
    name = unquote(parsed.path.rsplit("/", 1)[-1]) if parsed.path else ""
    info = LinkInfo(kind=kind, url=url, display=name or url[:64])
    if name.lower().endswith(".torrent"):
        info.kind = "torrent"
        info.meta = {"torrent_url": url, "name": name}
    return info


def magnet_from_infohash(infohash: str, dn: str = "", trackers=None) -> str:
    parts = ["magnet:?xt=urn:btih:%s" % infohash]
    if dn:
        parts.append("dn=%s" % quote(dn))
    for tracker in trackers or []:
        parts.append("tr=%s" % quote(tracker, safe=""))
    return "&".join(parts)


# ---------------------------------------------------------------- 身份标识
def identity(url: str) -> str:
    """链接身份（下载历史查重用）：magnet 按信息哈希归一。"""
    raw = (url or "").strip()
    if not raw:
        return ""
    info = parse_line(raw)
    meta = info.meta or {}
    if meta.get("infohash"):
        digest = str(meta["infohash"]).lower()
        if meta.get("infohash_algo") == "base32":
            try:
                digest = base64.b32decode(digest.upper()).hex()
            except ValueError:
                pass
        return "btih:" + digest
    if meta.get("hash"):
        return "ed2k:" + str(meta["hash"]).lower()
    return "url:" + (info.url or raw).lower()


# ---------------------------------------------------------------- 入口
def parse_line(line: str) -> LinkInfo:
    url = (line or "").strip()
    if not url:
        return LinkInfo(kind="unknown", url="", ok=False, reason="空链接")

    low = url.lower()
    if low.startswith("thunder://"):
        inner = decode_thunder(url)
        if not inner:
            return LinkInfo(kind="thunder", url=url, ok=False,
                            reason="thunder 链接解码失败（非标准 AA/ZZ 套壳）")
        inner_info = parse_line(inner)
        inner_info.meta["thunder_original"] = url
        return inner_info

    if low.startswith("magnet:"):
        return parse_magnet(url)
    if low.startswith("ed2k://"):
        return parse_ed2k(url)
    if low.startswith(("http://", "https://", "ftp://")):
        return parse_direct(url)
    return LinkInfo(
        kind="unknown",
        url=url,
        ok=False,
        reason="无法识别的链接协议（支持 magnet / thunder / http(s) / ftp / ed2k）",
    )


_SCHEME_RE = re.compile(r"(?:magnet:|thunder://|ed2k://|https?://|ftp://)", re.I)
MAX_LINKS = 50


def split_links(text: str) -> list:
    """批量文本切条：按协议前缀切，避免把合法 URL 里的逗号/顿号拆断。"""
    chunks = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        marks = list(_SCHEME_RE.finditer(line))
        if marks:
            for index, match in enumerate(marks):
                end = marks[index + 1].start() if index + 1 < len(marks) else len(line)
                seg = line[match.start():end].strip().rstrip(",;，；、").strip()
                if seg:
                    chunks.append(seg)
        else:
            for seg in re.split(r"[\s,;，；、]+", line):
                if seg.strip():
                    chunks.append(seg.strip())
    return chunks[:MAX_LINKS]


def parse_text(text: str) -> list:
    return [parse_line(chunk) for chunk in split_links(text)]

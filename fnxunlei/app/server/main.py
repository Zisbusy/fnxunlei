# -*- coding: utf-8 -*-
"""main.py —— fnxunlei 后端入口。

生产（fnOS，由 cmd/main 启动）：
    python3 server/main.py \
        --unix-socket  ${TRIM_APPDEST}/fnxunlei.sock \
        --base-path    /app/fnxunlei \
        --www-root     ${TRIM_APPDEST}/www \
        --data-dir     ${TRIM_PKGVAR} \
        --run-user     ${TRIM_USERNAME} --run-group ${TRIM_GROUPNAME}

权限模型：
- 生命周期脚本以 root 运行（需要把 Socket 绑到 root 拥有的应用目录，
  这也是 fnOS 统一网关要求的落点）；
- 本服务在**绑定 Socket 之后**立即降权到应用专用用户（run-as=root 只为 bind，
  长期服务不是 root），并用该用户读写 var 目录；
- 若目标用户无法解析或数据目录不可写，则退回 root 运行并写日志，
  保证应用不会因降权失败而完全不可用。

开发（任意平台）：
    python3 server/main.py --http 127.0.0.1:8788 \
        --base-path / --www-root app/www --data-dir ./var
"""

from __future__ import annotations

import argparse
import os
import signal
import sys

import httpd
import paths
import store
import util


def _parse_address(value: str):
    host, _, port = value.rpartition(":")
    return (host or "127.0.0.1", int(port or 8788))


def _resolve_ids(user: str, group: str):
    """按用户名/组名解析 uid/gid；非 root 或解析失败返回 None。"""
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or geteuid() != 0:
        return None
    try:
        import grp
        import pwd
    except ImportError:
        return None
    try:
        uid = pwd.getpwnam(user).pw_uid if user else 0
        gid = grp.getgrnam(group).gr_gid if group else uid
    except KeyError:
        return None
    if uid == 0:
        return None
    return uid, gid


def _drop_privileges(user: str, group: str) -> bool:
    """绑定 Socket 后降权到应用用户；数据目录归属先转给该用户。"""
    ids = _resolve_ids(user, group)
    if not ids:
        return False
    uid, gid = ids

    try:
        os.makedirs(paths.DATA_DIR, mode=0o700, exist_ok=True)
        for target in (paths.DATA_DIR, paths.CONFIG_FILE, paths.TASKS_FILE,
                       paths.LOG_FILE):
            if os.path.exists(target):
                os.chown(target, uid, gid)
        probe = os.path.join(paths.DATA_DIR, ".write-probe")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("1")
        os.unlink(probe)
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)
    except (OSError, AttributeError):
        return False

    util.log("服务已降权到 %s(%d):%s(%d)" % (user, uid, group, gid))
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="fnxunlei Unix socket server")
    parser.add_argument("--unix-socket", default="", help="Unix Socket 路径（生产模式）")
    parser.add_argument("--http", default="", help="开发模式：TCP 监听 host:port")
    parser.add_argument("--base-path", default="/app/fnxunlei", help="网关前缀")
    parser.add_argument("--www-root", required=True, help="静态页面目录")
    parser.add_argument("--data-dir", default="", help="运行数据目录（TRIM_PKGVAR）")
    parser.add_argument("--run-user", default="", help="绑定 Socket 后降权到的应用用户")
    parser.add_argument("--run-group", default="", help="应用用户所属组")
    args = parser.parse_args()

    paths.configure(args.data_dir)
    paths.ensure_data_dir()

    if args.http:
        server = httpd.ThreadingTCPHTTPServer(
            _parse_address(args.http), httpd.Handler,
            base_path=args.base_path, www_root=args.www_root,
        )
        address = "http://%s:%d%s/" % (
            server.server_address[0], server.server_address[1],
            "" if server.base_path == "/" else server.base_path,
        )
        print("[fnxunlei] dev server: %s" % address, flush=True)
    else:
        if not args.unix_socket:
            parser.error("生产模式需要 --unix-socket")
        if httpd.ThreadingUnixHTTPServer is None:
            parser.error("当前平台不支持 Unix Socket")
        if os.path.exists(args.unix_socket):
            try:
                os.unlink(args.unix_socket)
            except OSError:
                pass
        server = httpd.ThreadingUnixHTTPServer(
            args.unix_socket, httpd.Handler,
            base_path=args.base_path, www_root=args.www_root,
        )
        print("[fnxunlei] listening on %s" % args.unix_socket, flush=True)
        # Socket 已绑定，长期服务降权（失败则保持 root，但记录日志）
        if not _drop_privileges(args.run_user, args.run_group):
            util.log("未降权：run-user=%r 解析失败或数据目录不可写，继续以当前身份运行"
                     % (args.run_user or "-"))

    paths.ensure_data_dir()
    store.ensure_config()

    def shutdown(_signum, _frame):
        try:
            server.server_close()
        finally:
            _cleanup_socket(args.unix_socket)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        server.serve_forever()
    finally:
        server.server_close()
        _cleanup_socket(args.unix_socket)
    return 0


def _cleanup_socket(socket_path: str) -> None:
    if not socket_path:
        return
    if os.path.exists(socket_path):
        try:
            os.unlink(socket_path)
        except OSError:
            pass     # 降权后可能无权限删除，由 cmd/main 的 stop 兜底清理


if __name__ == "__main__":
    raise SystemExit(main())

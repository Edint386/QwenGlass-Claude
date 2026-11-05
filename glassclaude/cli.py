"""``glassclaude serve`` 在云端跑，``glassclaude bridge`` 在 Mac 上跑。配置都走环境变量，token 不进命令行。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys

import uvicorn

from .bridge import Bridge
from .hearing import AsrConfig
from .server import create_app
from .sessions import live_sessions


def _require(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        sys.exit(f"缺少环境变量 {name}")
    return value


def _serve(args: argparse.Namespace) -> None:
    devices = frozenset(d.strip() for d in os.environ.get("GLASSCLAUDE_ALLOWED_DEVICES", "").split(",") if d.strip())
    if not devices:
        logging.warning("没有设置 GLASSCLAUDE_ALLOWED_DEVICES，任何连到这个服务的眼镜都能操作你的 Claude 会话；发布插件之前必须设置")
    asr = AsrConfig.from_env()
    logging.info("自己的识别：%s", "已配置" if asr else "没有配置，音频流插件听不到用户说话")
    app = create_app(_require("GLASSCLAUDE_AGENT_TOKEN"), _require("GLASSCLAUDE_BRIDGE_TOKEN"), devices, asr)
    uvicorn.run(app, host=args.host, port=args.port, ws_max_size=2**20, log_level="info", ws_ping_interval=20, ws_ping_timeout=20)


def _bridge(args: argparse.Namespace) -> None:
    _supported()
    url = args.cloud or _require("GLASSCLAUDE_CLOUD")
    if args.keep_awake:  # 电脑休眠时整条链路就断了。盖子合上时这个也拦不住
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    asyncio.run(Bridge(url, _require("GLASSCLAUDE_BRIDGE_TOKEN")).run())


def _menubar(_: argparse.Namespace) -> None:
    from . import menubar  # 要 rumps，只有 Mac 上装了 mac 这组依赖才有
    menubar.run()


def _install_agent(_: argparse.Namespace) -> None:
    from . import menubar
    menubar.install_agent()


def _uninstall_agent(_: argparse.Namespace) -> None:
    from . import menubar
    menubar.uninstall_agent()


def _sessions(_: argparse.Namespace) -> None:
    _supported()
    for session in live_sessions():
        print(f"{session.state:8} {session.name}  [{session.project}]  {session.id}")


def _supported() -> None:
    """Mac 这一侧只在 macOS 上测过。Linux 大概能跑；Windows 上探活会结束 Claude 的进程，收件箱也不是 Unix socket。"""
    if sys.platform == "win32":
        sys.exit("Windows 还不支持：桥接程序只在 macOS 上测过")


def main() -> None:
    parser = argparse.ArgumentParser(prog="glassclaude")
    commands = parser.add_subparsers(required=True)
    serve = commands.add_parser("serve", help="云端服务")
    serve.add_argument("--host", default=os.environ.get("GLASSCLAUDE_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("GLASSCLAUDE_PORT", "8790")))
    serve.set_defaults(run=_serve)
    bridge = commands.add_parser("bridge", help="Mac 上的桥接程序")
    bridge.add_argument("--cloud", help="云端地址，比如 wss://example.com/claude/bridge；不写就读 GLASSCLAUDE_CLOUD")
    bridge.add_argument("--keep-awake", action="store_true", help="运行时不让电脑休眠")
    bridge.set_defaults(run=_bridge)
    commands.add_parser("menubar", help="桥接加菜单栏图标（配置读 ~/.config/glassclaude/env）").set_defaults(run=_menubar)
    commands.add_parser("install-agent", help="装成登录自启，崩了自动拉起").set_defaults(run=_install_agent)
    commands.add_parser("uninstall-agent", help="卸掉登录自启").set_defaults(run=_uninstall_agent)
    commands.add_parser("sessions", help="列出本机正在运行的会话").set_defaults(run=_sessions)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args.run(args)

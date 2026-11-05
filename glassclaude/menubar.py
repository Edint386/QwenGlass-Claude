"""菜单栏小图标：桥接在后台线程里跑，图标显示连没连上云端，点开看眼镜在看哪个会话、最近一条指令。

``glassclaude menubar`` 启动，``glassclaude install-agent`` 装成登录自启（launchd），崩了自动拉起。
配置从 ``~/.config/glassclaude/env`` 读（launchd 不经过 shell），日志写 ``~/Library/Logs/glassclaude-bridge.log``。

- 断开超过 :data:`OFFLINE_NOTICE_SECONDS` 弹一条系统通知，重新连上再说一声。
- 暂停：眼镜发来的指令一律不进会话，眼镜上提示“电脑上暂停了”。看回复照常。
- 重启：整个进程换一个新的（execv），不靠 launchd，手动启动的也能用。
- 退出：正常退出（返回 0），launchd 设的是只在异常退出时拉起，所以不会马上又起来，下次登录才会。
"""

from __future__ import annotations

import asyncio
import logging
import os
import plistlib
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .bridge import Bridge, Status

log = logging.getLogger(__name__)

ENV_FILE = Path.home() / ".config" / "glassclaude" / "env"
LOG_FILE = Path.home() / "Library" / "Logs" / "glassclaude-bridge.log"
#: launchd 接住的 stdout/stderr：日志已经直接写 LOG_FILE，这里只剩没接住的崩溃信息
CRASH_FILE = Path.home() / "Library" / "Logs" / "glassclaude-bridge.crash.log"
AGENT_LABEL = "local.glassclaude.bridge"
AGENT_FILE = Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"
#: 断开这么久才弹通知：云端重启、网络抖一下几秒就回来了，不用打扰
OFFLINE_NOTICE_SECONDS = 60


def load_env(path: Path = ENV_FILE) -> None:
    """``KEY=VALUE`` 一行一个，已经有的环境变量不覆盖。"""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _ago(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    if seconds < 60:
        return f"{seconds} 秒前"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    return f"{seconds // 3600} 小时前"


def describe(status: Status, now: float) -> tuple[str, list[str]]:
    """菜单栏的标题和下拉里的几行字。"""
    if status.paused:
        title, state = "👓⏸", "暂停中：眼镜的指令不进会话"
    elif status.connected:
        title, state = "👓🟢", f"已连上云端（{_ago(now - status.since)}）"
    else:
        title = "👓🔴" if now - status.since >= OFFLINE_NOTICE_SECONDS else "👓🟠"
        state = f"没连上云端（{_ago(now - status.since)}断开）" + (f"：{status.error[:60]}" if status.error else "")
    watching = f"眼镜在看：{status.watching}" if status.watching else "眼镜没在看会话"
    command = (f"最近一条指令：{_ago(now - status.command_at)}，发给 {status.command_to}，{status.command_chars} 字"
               if status.command_at else "还没有发过指令")
    return title, [state, watching, command]


def notify(title: str, message: str) -> None:
    """系统通知。不打包成 .app 的 Python 用不了新的通知接口，osascript 最省事。"""
    script = f'display notification "{message}" with title "{title}"'
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


def agent_plist(program: str) -> dict:
    """登录时启动；异常退出（不是 0）自动拉起，正常退出就停着，到下次登录。"""
    return {
        "Label": AGENT_LABEL,
        "ProgramArguments": [program, "menubar"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Interactive",
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": str(CRASH_FILE),
        "StandardErrorPath": str(CRASH_FILE),
    }


def install_agent() -> None:
    program = str(Path(sys.argv[0]).resolve())
    AGENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    AGENT_FILE.write_bytes(plistlib.dumps(agent_plist(program)))
    domain = f"gui/{os.getuid()}"
    _bootout(domain)
    subprocess.run(["launchctl", "bootstrap", domain, str(AGENT_FILE)], check=True)
    print(f"装好了：{AGENT_FILE}，登录时自动启动。日志在 {LOG_FILE}")


def _bootout(domain: str) -> None:
    """卸掉正在跑的那份。bootout 是异步的，旧进程没退干净之前 bootstrap 会报 5，所以等它真没了。"""
    target = f"{domain}/{AGENT_LABEL}"
    subprocess.run(["launchctl", "bootout", target], check=False, capture_output=True)
    for _ in range(50):
        if subprocess.run(["launchctl", "print", target], check=False, capture_output=True).returncode:
            return
        time.sleep(0.2)


def uninstall_agent() -> None:
    _bootout(f"gui/{os.getuid()}")
    AGENT_FILE.unlink(missing_ok=True)
    print("已卸载，不再自动启动")


def _log_to_file() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]  # 换掉命令行那份写 stderr 的，不然 launchd 下每行记两遍
    root.setLevel(logging.INFO)


def run() -> None:
    import rumps  # 只有 Mac 上装了 mac 这组依赖才有
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)  # 只要菜单栏图标，不占程序坞
    load_env()
    _log_to_file()
    url, token = os.environ.get("GLASSCLAUDE_CLOUD", ""), os.environ.get("GLASSCLAUDE_BRIDGE_TOKEN", "")
    if not url or not token:
        notify("Claude 眼镜桥接", f"缺少配置：{ENV_FILE}")
        sys.exit(f"缺少 GLASSCLAUDE_CLOUD 或 GLASSCLAUDE_BRIDGE_TOKEN，看看 {ENV_FILE}")
    subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])  # 电脑休眠时整条链路就断了。盖子合上时这个也拦不住
    bridge = Bridge(url, token)
    threading.Thread(target=lambda: asyncio.run(bridge.run()), name="bridge", daemon=True).start()
    log.info("菜单栏启动，桥接在后台线程里跑")

    class App(rumps.App):
        def __init__(self) -> None:
            self.lines = [rumps.MenuItem(""), rumps.MenuItem(""), rumps.MenuItem("")]
            self.pause = rumps.MenuItem("暂停接收眼镜指令", callback=self.toggle)
            menu = [*self.lines, None, self.pause, rumps.MenuItem("重启桥接", callback=self.restart),
                    rumps.MenuItem("打开日志", callback=self.open_log)]
            super().__init__("Claude 眼镜桥接", title="👓", menu=menu, quit_button="退出")
            self.warned = False  # 这次断开已经通知过了
            self.ticks = 0
            rumps.Timer(self.refresh, 1).start()

        def refresh(self, _: object) -> None:
            self.ticks += 1
            if self.ticks == 3:  # 刚启动时还没摆好位置。被 Bartender 之类收起来时 x 会是负数，查“看不到图标”用
                frame = self._nsapp.nsstatusitem.button().window().frame()
                log.info("菜单栏图标位置 x=%d 宽=%d", frame.origin.x, frame.size.width)
            status, now = bridge.status, time.time()
            title, lines = describe(status, now)
            self.title = title
            for item, text in zip(self.lines, lines, strict=True):
                item.title = text
            offline = not status.connected and now - status.since >= OFFLINE_NOTICE_SECONDS
            if offline and not self.warned:
                self.warned = True
                notify("Claude 眼镜桥接", "已经断开一分钟了，眼镜上会显示电脑离线")
            elif status.connected and self.warned:
                self.warned = False
                notify("Claude 眼镜桥接", "已经重新连上")

        def toggle(self, item: object) -> None:
            bridge.status.paused = not bridge.status.paused
            self.pause.state = bridge.status.paused
            log.info("暂停接收眼镜指令：%s", bridge.status.paused)

        def restart(self, _: object) -> None:
            log.info("菜单里点了重启")
            os.execv(sys.executable, [sys.executable, *sys.argv])

        def open_log(self, _: object) -> None:
            subprocess.run(["open", "-a", "Console", str(LOG_FILE)], check=False)

    App().run()

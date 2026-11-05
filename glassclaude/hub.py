"""云端的中枢：一头是 Mac 上的桥接程序，一头是眼镜会话。

Mac 连上来之后这里只留最新的状态，不存历史，容器重启什么都不丢。指令不排队：电脑不在线就直接告诉用户没发出去，
隔几个小时才执行一条语音指令比发不出去危险。只有用户正在看的那个会话的文字才会从 Mac 发上来。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from .panel import Panel

__all__ = ["Hub"]

log = logging.getLogger(__name__)

#: 记住多少个眼镜会话（断线重连要用）
MAX_PANELS = 16
#: 指令多久之内有效。电脑休眠时压在连接里的指令，醒来后不能再执行
SEND_SECONDS = 15

Outbox = Callable[[dict[str, Any]], bool]
Delivery = list[tuple[Panel, list[dict[str, Any]]]]


class Hub:
    def __init__(self, allowed_devices: frozenset[str] = frozenset()):
        self.allowed_devices = allowed_devices
        self.panels: OrderedDict[str, Panel] = OrderedDict()
        self.sessions: list[dict[str, Any]] = []
        self.turns: dict[str, dict[str, Any]] = {}
        self.last_selected: str | None = None
        self._outbox: Outbox | None = None
        self._watching: str | None = None

    # ------------------------------------------------------------------ 给眼镜用

    @property
    def online(self) -> bool:
        return self._outbox is not None

    def turn(self, session: str) -> dict[str, Any] | None:
        return self.turns.get(session)

    def watch(self, session: str | None) -> None:
        self._watching = session
        self._push({"type": "watch", "session": session})

    def send(self, session: str, text: str) -> str | None:
        command = uuid.uuid4().hex
        message = {"type": "send", "cmd": command, "session": session, "text": text, "expires": time.time() + SEND_SECONDS}
        return command if self._push(message) else None

    def _push(self, message: dict[str, Any]) -> bool:
        return self._outbox is not None and self._outbox(message)

    def attach(self, session_id: str, epoch: int) -> tuple[Panel | None, str]:
        """返回 (会话, CREATED / RESUMED / STALE)。"""
        panel = self.panels.get(session_id)
        if panel is None:
            # 不认识又不是第一次连：服务重启过（每次部署都会），内存里的会话没了。平台接受一次 CREATED，
            # 重新打开页面接着用；回 UNKNOWN 的话会话就结束，眼镜直接退出
            if epoch > 1:
                log.info("会话 %s 第 %d 次连接，服务里没有它（重启过），当作新会话重新打开页面", session_id, epoch)
            panel = Panel(session_id, self, self.allowed_devices)
            panel.epoch = epoch
            self.panels[session_id] = panel
            while len(self.panels) > MAX_PANELS:
                self.panels.popitem(last=False)
            return panel, "CREATED"
        if epoch <= panel.epoch:
            return panel, "STALE"
        panel.epoch = epoch
        self.panels.move_to_end(session_id)
        return panel, "RESUMED"

    def drop(self, session_id: str) -> None:
        self.panels.pop(session_id, None)
        self.released()

    def released(self) -> None:
        """没有眼镜连着时不再让 Mac 发文字上来。"""
        if not any(panel.socket is not None for panel in self.panels.values()):
            self.watch(None)

    def _live(self) -> list[Panel]:
        return [panel for panel in self.panels.values() if panel.socket is not None]

    # ------------------------------------------------------------------ 给桥接程序用

    def connected(self, outbox: Outbox) -> Delivery:
        self._outbox = outbox
        return [(panel, panel.on_link()) for panel in self._live()]

    def disconnected(self, outbox: Outbox) -> Delivery:
        if self._outbox is not outbox:  # 已经被新连接顶替了
            return []
        self._outbox = None
        return [(panel, panel.on_link()) for panel in self._live()]

    def receive(self, message: dict[str, Any]) -> Delivery:
        kind = message.get("type")
        if kind == "hello":
            log.info("桥接程序连上了")
            return []
        if kind == "sessions":
            items = message.get("items")
            self.sessions = [item for item in items if isinstance(item, dict) and "id" in item] if isinstance(items, list) else []
            ids = {item["id"] for item in self.sessions}
            self.turns = {key: value for key, value in self.turns.items() if key in ids}
            return [(panel, panel.on_sessions()) for panel in self._live()]
        if kind == "turn":
            session = message.get("session")
            if not isinstance(session, str):
                return []
            message["heard"] = time.monotonic()  # 这条是什么时候到的：换会话时拿出来的可能是旧的，用时要从这里往后算
            self.turns[session] = message
            return [(panel, panel.on_turn(message)) for panel in self._live()]
        if kind == "sent":
            return [(panel, panel.on_sent(message)) for panel in self.panels.values()]  # 眼镜闪断时回执也要记下
        log.info("忽略桥接消息 %r", kind)
        return []

"""Mac 上的桥接程序：主动连云端，把会话同步上去，把眼镜的指令送进会话。

会话列表来自 ``~/.claude/sessions``，回复来自记录文件，指令走收件箱 socket，都不用改 Claude Code 的设置，也不用它登录。
只有眼镜正在看的那个会话的文字才发上去，发之前过 :mod:`~glassclaude.cleanup`。

和云端的消息都是一行 JSON：

- 上行 ``hello``、``sessions``（列表，只有名字和状态）、``turn``（正在看的会话的最新一轮和进度）、``sent``（指令的回执）。
- 下行 ``watch``（换一个要看的会话，``null`` 是不看了）、``send``（把一句话送进会话）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import websockets

from . import inbox
from .cleanup import brief, clean
from .doing import doing, pick
from .sessions import IDLE, LocalSession, live_sessions, transcript_path
from .tasks import Tasks
from .transcript import AGENT_TOOLS, Call, Step, Tail, Turn
from .usage import read_usage

__all__ = ["Bridge"]

log = logging.getLogger(__name__)

POLL_SECONDS = 1.0
#: 断线后等多久重连，翻倍到上限
RETRY_MIN_SECONDS, RETRY_MAX_SECONDS = 1.0, 30.0
#: 发出去之后等这么久还没在记录文件里看到，就告诉眼镜没等到回执。Claude 忙时也会马上进队列
DELIVERY_SECONDS = 15.0
#: 列表里的名字最长这么多字
NAME_CHARS = 40
#: 眼镜上的名字显示这么多字，这之内相同的两个会话要加编号才分得开
NAME_SHOWN = 18
#: 工具和后台任务的说明最长这么多字
LABEL_CHARS = 48
#: 提问最长这么多字
PROMPT_CHARS = 200
#: 运行中的进度至少隔这么久才发一次。云端发给眼镜时还有自己的间隔
PROGRESS_SECONDS = 2.0
#: 有子代理在跑时隔这么久报一次 token，它们不写主线记录，看不出变化
TASK_SECONDS = 5.0
_MODEL = re.compile(r"^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?(?:\[.*\])?$")


@dataclass
class Status:
    """给菜单栏看的：连没连上、从什么时候起、眼镜在看哪个会话、最近一条指令、最近的错误。只在桥接自己的线程里写。"""

    connected: bool = False
    since: float = field(default_factory=time.time)  # 连上或断开的时刻
    error: str = ""
    watching: str = ""  # 会话名
    command_at: float = 0.0
    command_to: str = ""
    command_chars: int = 0
    paused: bool = False  # 暂停时眼镜发来的指令一律不进会话


@dataclass
class _Pending:
    command: str
    session: str
    content: str
    after: str  # 这个时刻之后进队列的才算
    deadline: float
    tail: Tail | None = None  # 目标不是正在看的会话时单独跟着，只看回执，不上传内容


def _model_name(model: str) -> str:
    """``claude-opus-5-5`` 写成 ``Opus 5.5``，认不出的格式去掉前缀。"""
    match = _MODEL.match(model)
    if match is None:
        return model.removeprefix("claude-")
    family, major, minor = match.groups()
    return f"{family.title()} {major}.{minor}" if minor else f"{family.title()} {major}"


def _step(step: Step, busy: bool) -> dict[str, Any]:
    """工具组的用时算到最后一个结果，所以没有工具在跑时就不算还在走，后面只是 Claude 在想。"""
    if step.kind == "tools":
        return {"k": "tools", "n": step.count, "s": step.start, "e": step.end, "o": step.open and busy}
    return {"k": step.kind, "t": clean(inbox.unwrap(step.text) if step.kind == "you" else step.text)}  # 眼镜插的话也带着那句说明


def _task(call: Call, tasks: Tasks | None) -> dict[str, Any]:
    kind = "workflow" if call.name == "Workflow" else "agent" if call.name in AGENT_TOOLS else "background"
    tokens = left = None
    if tasks is not None and kind == "agent":
        tokens = tasks.agent_tokens(call)
    elif tasks is not None and kind == "workflow":
        tokens, left = tasks.workflow(call)
    return {"name": brief(call.label or call.name, LABEL_CHARS), "kind": kind, "at": call.at, "tokens": tokens, "left": left}


def _listing(sessions: list[LocalSession]) -> list[dict[str, Any]]:
    """发给云端的列表，只有名字和状态。同一目录开两个会话时名字一样，加上 ID 开头才知道发给了哪个。"""
    names = [brief(session.name, NAME_CHARS) for session in sessions]
    shown = [name[:NAME_SHOWN] for name in names]
    return [
        {"id": s.id, "pid": s.pid, "name": f"{name[:NAME_SHOWN - 6]}·{s.id[:4]}" if shown.count(name[:NAME_SHOWN]) > 1 else name, "state": s.state, "at": s.updated_at}
        for s, name in zip(sessions, names, strict=True)
    ]


class Bridge:
    def __init__(self, url: str, token: str, home: Path | None = None, poll: float = POLL_SECONDS):
        self.url = url
        self.token = token
        self.home = home
        self.poll = poll
        self.sessions: dict[str, LocalSession] = {}
        self.watching: str | None = None
        self.tail: Tail | None = None
        self.tasks: Tasks | None = None
        self.pending: list[_Pending] = []
        self._lock = asyncio.Lock()  # 定时检查和换会话都动 tail，中途有 await，要排队
        self._listed: list[dict[str, Any]] | None = None
        self._shown: tuple[Any, ...] | None = None
        self._sent_at = float("-inf")
        self.status = Status()

    async def run(self) -> None:
        """一直连着，断了就重连。别的异常也不能让这个常驻进程退出。"""
        delay = RETRY_MIN_SECONDS
        while True:
            try:
                headers = {"Authorization": f"Bearer {self.token}"}
                async with websockets.connect(self.url, additional_headers=headers, ping_interval=20, ping_timeout=20, max_size=2**20) as ws:
                    log.info("连上云端 %s", self.url)
                    self._mark(True, "")
                    delay = RETRY_MIN_SECONDS
                    await self.serve(ws)
            except (OSError, websockets.WebSocketException, TimeoutError) as exc:
                log.warning("云端连接断了：%s，%.0f 秒后重连", exc, delay)
                self._mark(False, str(exc))
            except Exception as exc:
                log.exception("桥接出错，%.0f 秒后重连", delay)
                self._mark(False, f"出错：{exc}")
            await asyncio.sleep(delay)
            delay = min(delay * 2, RETRY_MAX_SECONDS)

    def _mark(self, connected: bool, error: str) -> None:
        if connected != self.status.connected:
            self.status.since = time.time()
        self.status.connected, self.status.error = connected, error

    async def serve(self, ws: Any) -> None:
        """一条连接的生命周期。断线时列表和正在看的内容要全量重发，所以这里清掉。"""
        self._listed = self._shown = None
        self.watching, self.tail = None, None
        self.pending.clear()
        await self._send(ws, {"type": "hello", "version": 1})
        tasks = [asyncio.create_task(self._receive(ws)), asyncio.create_task(self._tick(ws))]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send(self, ws: Any, message: dict[str, Any]) -> None:
        await ws.send(json.dumps(message, ensure_ascii=False, separators=(",", ":")))

    async def _receive(self, ws: Any) -> None:
        async for raw in ws:
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            kind = message.get("type") if isinstance(message, dict) else None
            if kind == "watch":
                async with self._lock:
                    await self._watch(ws, message.get("session"))
            elif kind == "send":
                await self._post(ws, message)

    async def _tick(self, ws: Any) -> None:
        while True:
            async with self._lock:
                await self._scan(ws)
                await self._follow(ws)
                await self._settle(ws)
            await asyncio.sleep(self.poll)

    async def _scan(self, ws: Any) -> None:
        found: dict[str, LocalSession] = {}
        for session in await asyncio.to_thread(live_sessions, self.home):
            found.setdefault(session.id, session)  # 同一个 ID 有两个进程时用最新的
        self.sessions = found
        listed = _listing(list(found.values()))
        # updated_at 一直在变，比较时不看它，只在名字和状态变了才发
        key = [{k: v for k, v in item.items() if k != "at"} for item in listed]
        if key != self._listed:
            self._listed = key
            await self._send(ws, {"type": "sessions", "items": listed})

    async def _watch(self, ws: Any, session: Any) -> None:
        self.watching = session if isinstance(session, str) else None
        local = self.sessions.get(self.watching) if self.watching else None
        self.status.watching = local.name if local else ""
        self.tail = None
        self._shown = None  # 云端可能换了容器，就算看的是同一个会话也重发一次
        await self._follow(ws)

    async def _follow(self, ws: Any) -> None:
        """看正在看的会话有没有新东西。一轮结束马上发，运行中的进度隔一段时间发一次。"""
        session = self.watching
        if session is None:
            return
        local = self.sessions.get(session)
        if self.tail is None:
            if local is None:  # ID 是云端发来的，列表里有才打开。刚连上时列表还没扫，下一次再看
                return
            path = await asyncio.to_thread(transcript_path, session, self.home)
            if path is None:
                return
            self.tail, self.tasks = Tail(path), Tasks(path)
        tail = self.tail
        if not await asyncio.to_thread(tail.path.exists):  # 记录文件被挪走了，下次重新找
            self.tail = None
            return
        await asyncio.to_thread(tail.poll)
        for call in [c for c in tail.background.values() if local is not None and c.at * 1000 < local.started_at]:
            del tail.background[call.id]  # 进程重开过：之前的后台任务不会再有通知
        turn = tail.turn or Turn()
        gone = local is None
        background = [] if gone else await asyncio.to_thread(self._alive, tail)
        # 主线说完了：被打断的一轮没有 end_turn，只能看状态。像 Codex 那样，后台任务还在跑、或者刚结束等 Claude 回话时，整个任务还没完
        settled = turn.done or (local is not None and local.state == IDLE)
        waiting = not gone and settled and (bool(background) or tail.awaiting(time.time()))
        done = gone or (settled and not waiting)  # 进程没了也不会再有回复
        # 被打断时最后一个工具已经把 text 清空了，屏幕上留着 Claude 说过的话，不能变成“没有文字回复”
        said = turn.text if turn.done else turn.narration
        usage = await asyncio.to_thread(read_usage, session, time.time(), self.home)
        plan = tuple((item["t"], item["s"]) for item in tail.plan) if tail.plan_at >= turn.began else ()
        shown = (turn.prompt, done, waiting, said, turn.tools, () if gone else tuple(tail.running), tuple(c.id for c in background), len(turn.steps), usage, tail.context, tail.model, tail.effort, plan)
        now = time.monotonic()
        counting = not gone and any(c.name in AGENT_TOOLS for c in [*tail.running.values(), *background])
        if shown == self._shown and not (counting and now - self._sent_at >= TASK_SECONDS):
            return
        turning = self._shown is None or shown[:2] != self._shown[:2]
        if not turning and now - self._sent_at < PROGRESS_SECONDS:
            return
        self._shown, self._sent_at = shown, now
        await self._send(ws, await asyncio.to_thread(self._turn_message, session, tail, turn, done, gone, usage, background, waiting, plan))

    def _alive(self, tail: Tail) -> list[Call]:
        now = time.time()
        return [call for call in tail.background.values() if self.tasks is None or not self.tasks.quiet(call, now)]

    def _turn_message(self, session: str, tail: Tail, turn: Turn, done: bool, gone: bool, usage: Any, background: list[Call],
                      waiting: bool = False, plan: tuple[tuple[str, str], ...] = ()) -> dict[str, Any]:
        running = [] if gone else list(tail.running.values())
        agents = [c for c in [*running, *background] if c.name in AGENT_TOOLS]
        others = [c for c in background if c.name not in AGENT_TOOLS]
        current = pick(running)
        window = usage.window
        return {
            "type": "turn",
            "session": session,
            "prompt": brief(inbox.unwrap(turn.prompt), PROMPT_CHARS),
            "source": turn.source,
            "text": clean(turn.text if turn.done else turn.narration) if done else "",  # 运行中看时间流，这段用不上
            "done": done,
            "at": turn.at,
            "tools": turn.tools,
            "now": time.time(),
            "began": turn.began,
            "last": turn.last,
            "steps": [_step(step, bool(running)) for step in turn.steps],
            "doing": {"text": brief(doing(current), LABEL_CHARS), "at": current.at} if current else None,
            "tasks": [_task(call, self.tasks) for call in [*agents, *others]],
            "waiting": waiting,  # 主线说完了，在等后台任务
            "plan": [{"t": brief(text, LABEL_CHARS), "s": state} for text, state in plan],  # 这一轮里改过的计划，没有就是空的
            "model": _model_name(tail.model),
            "effort": tail.effort,
            "context": tail.context * 100 // window if window and tail.context else None,
            "five": usage.five,
            "week": usage.week,
        }

    async def _post(self, ws: Any, message: dict[str, Any]) -> None:
        command, target, text, expires = message.get("cmd"), message.get("session"), message.get("text"), message.get("expires")
        session = self.sessions.get(target) if isinstance(target, str) else None
        if session is None or not isinstance(text, str) or not text.strip():
            await self._send(ws, {"type": "sent", "cmd": command, "state": "failed", "error": "会话不在了"})
            return
        if self.status.paused:  # 菜单栏里点了暂停
            log.info("暂停中，没有发进会话 %s", session.id)
            await self._send(ws, {"type": "sent", "cmd": command, "state": "failed", "error": "电脑上暂停了"})
            return
        if not isinstance(expires, int | float) or time.time() > expires:  # 电脑休眠时压在连接里的指令，醒来后不能再执行
            log.warning("指令过期了，没有发进会话 %s", session.id)
            await self._send(ws, {"type": "sent", "cmd": command, "state": "failed", "error": "过期了"})
            return
        after = (datetime.now(UTC) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        try:
            content = await asyncio.to_thread(inbox.post, session.socket, text)
        except OSError as exc:
            log.warning("发进会话失败：%s", exc)
            await self._send(ws, {"type": "sent", "cmd": command, "state": "failed", "error": "连不上这个会话"})
            return
        log.info("指令已发进会话 %s（%s）：%d 字", session.name, session.id, len(text))
        self.status.command_at, self.status.command_to, self.status.command_chars = time.time(), session.name, len(text)
        await self._send(ws, {"type": "sent", "cmd": command, "state": "posted"})
        self.pending.append(_Pending(str(command), session.id, content, after, time.monotonic() + DELIVERY_SECONDS))

    async def _settle(self, ws: Any) -> None:
        """看记录文件里有没有出现发出去的话。bypassPermissions 的会话不收，这里等不到。"""
        for item in list(self.pending):
            if item.session != self.watching and item.tail is None:
                path = await asyncio.to_thread(transcript_path, item.session, self.home)
                item.tail = Tail(path) if path else None
            if item.tail is not None:
                await asyncio.to_thread(item.tail.poll)
            tail = self.tail if item.session == self.watching else item.tail
            if tail is not None and tail.received(item.content, item.after):
                self.pending.remove(item)
                await self._send(ws, {"type": "sent", "cmd": item.command, "state": "delivered"})
            elif time.monotonic() > item.deadline:
                self.pending.remove(item)
                await self._send(ws, {"type": "sent", "cmd": item.command, "state": "unconfirmed"})

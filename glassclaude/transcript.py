"""从会话记录文件里读最新一轮。

``~/.claude/projects/<目录>/<session-id>.jsonl`` 一行一个 JSON，会话一直往后追加，长会话能到几十 MB。
这是内部格式，文档没写，所以只认用得到的几个字段，别的行类型一律跳过。认识的有：

- ``user``：真人的提示词（``origin.kind`` 是 ``human``，旧版本没有 ``origin``）、工具结果（有 ``toolUseResult``，跳过）、
  另一个进程发来的消息（``origin.kind`` 是 ``peer``，内容前后包着说明文字）、后台任务的通知（``task-notification``，
  Claude 会接着回话，但不算谁问的）。内容里还可能夹着系统提示和斜杠命令的标签。
- ``assistant``：一行一个内容块，同一次回复拆在几行。``stop_reason`` 是 ``end_turn`` 表示这一轮说完了。
- ``queue-operation``：消息进了会话的队列，``content`` 是收到的原文，用来确认投递成功。
- ``attachment``：Claude 忙时收到的消息，类型是 ``queued_command``，在两次工具调用之间读到，不开新的一轮。
  后台任务结束的通知也走这里，``commandMode`` 是 ``task-notification``，内容里有 ``<tool-use-id>`` 和 ``<status>``。

工具和后台任务的进度也从这里读，一个内容块写完才落盘，所以文字是一段一段出来的，不是逐字：

- ``tool_use`` 块有 ``id``，结果在 ``user`` 行的 ``tool_result`` 块里，``tool_use_id`` 对得上，这之间工具在运行。
- 转到后台的工具，结果行的 ``toolUseResult`` 里 ``status`` 是 ``async_launched``（Agent、Workflow），或者有 ``backgroundTaskId``（Bash）。
  它们要等到通知才算结束。
- ``isSidechain``：子代理的，不算主线。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from .cleanup import fold

__all__ = ["AGENT_TOOLS", "Call", "Step", "Tail", "Turn"]

#: 第一次打开时从文件尾往前读多少：够放下最后一轮就行
TAIL_BYTES = 8 * 1024 * 1024
_PEER_HEAD = "Another Claude session sent a message:\n"
_PEER_TAIL = "\n\nThis came from another Claude session"
_ENVELOPE = re.compile(r"^<cross-session-message[^>]*>\n(.*)\n</cross-session-message>$", re.S)
_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>\s*", re.S)
_COMMAND = re.compile(r"<command-name>(.*?)</command-name>", re.S)
_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
_SHELL = re.compile(r"^<bash-input>(.*?)</bash-input>", re.S)
#: 这些开头的不是谁问的话
_NOT_PROMPTS = ("<task-notification", "<local-command", "<bash-stdout", "<bash-stderr", "<agent-message", "[Request interrupted")
#: 粘贴的内容，开头和结尾的标签带同一个 id
_PASTED = re.compile(r'<pasted_content id="([^"]*)">.*?</pasted_content id="\1">', re.S)
_NOTIFIED = re.compile(r"<tool-use-id>(.*?)</tool-use-id>")
#: 改写检测看偏移前面多少字节
_MARK_BYTES = 64
#: 这一轮记多少字的旁白，也是时间流的长度
NARRATION_CHARS = 4000
#: 算子代理的工具，别的转到后台的都算后台任务
AGENT_TOOLS = ("Agent", "Task", "Workflow")
_STOP_TOOLS = ("TaskStop", "KillShell")
#: 桌面版的计划工具。整份写 ``items``；``advance`` 把进行中的那步做完、开始下一步；``done`` ``start`` 按从 1 数的序号改
PLAN_TOOL = "mcp__ccd_turn__update_plan"
#: 说完之后隔这么久又接着说，算新的一轮：后台任务跑了半天才回来，总用时不该一直算下去
RESUME_SECONDS = 30 * 60
#: 后台任务结束的通知到了，Claude 一般马上回话。等这么久还没回，就当这一轮真结束了
REPLY_WAIT_SECONDS = 30.0


@dataclass(frozen=True)
class Call:
    """一次工具调用。``label`` 是给人看的那一点内容，比如文件名、Bash 的说明。"""

    id: str
    name: str
    label: str = ""
    #: 开始的时刻，Unix 秒
    at: float = 0.0
    #: ``label`` 是 Claude 自己写的说明，不是文件名这类
    described: bool = False
    #: 子代理的 ID，工作流的 run ID。转到后台时才知道
    agent: str = ""
    run: str = ""


@dataclass
class Step:
    """时间流里的一项：Claude 说的一段话（``text``）、你中途插的话（``you``）、连着用的一组工具（``tools``）。"""

    kind: str
    text: str = ""
    count: int = 0
    start: float = 0.0
    end: float = 0.0
    #: 这组工具还在进行：后面还没有别的东西
    open: bool = False


@dataclass
class Turn:
    """一轮对话：谁说了什么，Claude 最后回了什么。"""

    prompt: str = ""
    #: ``user`` 是在电脑或手机上打字发的，``peer`` 是别的进程发来的，眼镜发的也算
    source: str = "user"
    #: 最后一个工具调用之后的文字：中间过程的旁白不算，和 Stop hook 的 last_assistant_message 一样
    text: str = ""
    #: 这一轮的时间流，代码块已经折叠。运行中显示这个
    steps: list[Step] = field(default_factory=list)
    tools: int = 0
    done: bool = False
    #: 被打断了：没有 end_turn，但这一轮已经停了。``done`` 不能用，桥接程序靠它决定显示最后一段还是全部旁白
    stopped: bool = False
    #: 提问是从文件尾往前读时碰到的附件，真正的开头可能在更前面
    guessed: bool = False
    at: str = ""
    #: 总用时的两头。中途插话不重新算，全部说完才定住
    began: float = 0.0
    last: float = 0.0

    @property
    def narration(self) -> str:
        return "\n\n".join(step.text for step in self.steps if step.kind == "text")

    def say(self, text: str) -> None:
        """记下一段话，太多时整段丢掉最旧的。先折叠再记：按字数切已拼好的文字，会切在代码块中间，围栏就配不上对了。"""
        self.close_tools()
        self.steps.append(Step("text", fold(text)[-NARRATION_CHARS:]))
        while sum(len(step.text) for step in self.steps) > NARRATION_CHARS and len(self.steps) > 1:
            del self.steps[0]

    def interject(self, text: str) -> None:
        self.close_tools()
        self.steps.append(Step("you", text))

    def use_tool(self, at: float) -> None:
        self.tools += 1
        step = self.steps[-1] if self.steps else None
        if step is not None and step.kind == "tools" and step.open:
            step.count += 1
        else:
            self.steps.append(Step("tools", count=1, start=at, end=at, open=True))

    def tool_finished(self, at: float) -> None:
        step = self.steps[-1] if self.steps else None
        if step is not None and step.kind == "tools" and step.open:
            step.end = at

    def close_tools(self) -> None:
        if self.steps and self.steps[-1].kind == "tools":
            self.steps[-1].open = False


def _describe(name: str, tool_input: Any) -> str:
    """工具调用给人看的说明。不用 Bash 的命令本身：命令里可能有密钥，说明是 Claude 自己写的一句话。"""
    values = tool_input if isinstance(tool_input, dict) else {}
    for key in ("description", "file_path", "notebook_path", "pattern", "query", "url", "skill", "action"):
        value = values.get(key)
        if isinstance(value, str) and value:
            return value.rsplit("/", 1)[-1] if key.endswith("path") else value
    return ""


def _seconds(timestamp: Any) -> float:
    """记录里的时间是 ``2026-11-02T12:00:00.000Z``。"""
    try:
        return datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _short_name(name: str) -> str:
    """``mcp__服务__工具`` 太长，眼镜上只留工具名。"""
    return name.rsplit("__", 1)[-1] if name.startswith("mcp__") else name


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _prompt_text(text: str) -> str:
    """提示词里给人看的部分，没有就是空的。"""
    text = _PASTED.sub("（粘贴的内容）", _REMINDER.sub("", text)).strip()
    if text.startswith(_NOT_PROMPTS):
        return ""
    if command := _COMMAND.search(text):
        arguments = _ARGS.search(text)
        return f"{command.group(1)} {arguments.group(1) if arguments else ''}".strip()
    if shell := _SHELL.match(text):
        return f"!{shell.group(1)}"
    return text


def _peer_body(text: str) -> str:
    """去掉 Claude Code 包在外面的说明，留下发来的那句话。"""
    if text.startswith(_PEER_HEAD):
        text = text[len(_PEER_HEAD):].split(_PEER_TAIL, 1)[0]
    match = _ENVELOPE.match(text)
    return match.group(1) if match else text


@dataclass
class Tail:
    """跟着一个记录文件读，只读新增的部分。"""

    path: Path
    turn: Turn | None = None
    #: 进过队列的消息 (原文, 时间)，确认投递用
    queued: list[tuple[str, str]] = field(default_factory=list)
    #: 还没有结果的工具，按 tool_use 的 id
    running: dict[str, Call] = field(default_factory=dict)
    #: 转到后台、还没有结束通知的工具
    background: dict[str, Call] = field(default_factory=dict)
    #: 最新一次回复用的模型和思考强度，比如 ``claude-opus-5-5``、``xhigh``
    model: str = ""
    effort: str = ""
    #: 最新一次回复的上下文 token，主线的
    context: int = 0
    #: 桌面版的计划，每项 ``t`` 是这一步、``s`` 是 pending / in_progress / completed。``plan_at`` 是最后一次改的时刻
    plan: list[dict[str, str]] = field(default_factory=list)
    plan_at: float = 0.0
    #: 最近一条后台任务结束通知的时刻，等 Claude 回它
    notified_at: float = 0.0
    _ended: str = ""  # 说完这一轮的那条回复的 ID
    _ended_at: float = 0.0  # 说完或被打断的时刻：接着说之前隔了多久从这里算，中间只有思考的行不算回话
    _tasks: dict[str, str] = field(default_factory=dict)  # 后台任务的 ID 对应哪个 tool_use：TaskStop 只说任务 ID
    _offset: int = 0
    _rest: bytes = b""
    _opened: bool = False
    _mark: bytes = b""  # 偏移前面的最后几个字节
    _command: tuple[str, str] | None = None  # 斜杠命令，等到 Claude 回话才算一轮：/model 这类本地命令不回话

    def received(self, content: str, after: str) -> bool:
        """这段原文在 ``after`` 之后进过队列。时间要比，同一句话可能说过两次。"""
        return any(text == content and at >= after for text, at in self.queued)

    def poll(self) -> bool:
        """读新增的行，这一轮有变化时返回 True。"""
        try:
            size = self.path.stat().st_size
        except OSError:
            return False
        before = self._snapshot()
        if self._rewritten(size):  # 压缩时文件被换掉，撤回时尾部被截断重写，大小不一定变小
            self._offset, self._rest, self._mark, self._opened = 0, b"", b"", False
            self._reset()
        if size == self._offset:
            return self._snapshot() != before
        if self._opened:
            self._read(self._offset, size)
        else:
            self._open(size)
        return self._snapshot() != before

    def _snapshot(self) -> tuple[Any, ...]:
        turn = self.turn or Turn()
        plan = tuple(item["s"] for item in self.plan)
        return turn.prompt, turn.text, len(turn.steps), turn.tools, turn.done, tuple(self.running), tuple(self.background), plan

    def _rewritten(self, size: int) -> bool:
        if size < self._offset:
            return True
        if not self._mark:
            return False
        with self.path.open("rb") as file:
            file.seek(self._offset - len(self._mark))
            return file.read(len(self._mark)) != self._mark

    def _reset(self) -> None:
        self.turn, self._command = None, None
        self.model, self.effort, self.context, self._ended, self._ended_at = "", "", 0, "", 0.0
        self.plan, self.plan_at, self.notified_at = [], 0.0, 0.0
        self.running.clear()
        self.background.clear()
        self._tasks.clear()
        self.queued.clear()

    def _open(self, size: int) -> None:
        """从文件尾往前找最后一轮。这段里没有提示词就加倍往前，长工具结果可能占掉很多。
        只找到 assistant 行不算：提问在更前面，工具数也不全。"""
        span = TAIL_BYTES
        while True:
            start = max(0, size - span)
            self._rest = b""
            self._reset()
            self._read(start, size, partial_head=start > 0)
            if (self.turn is not None and self.turn.prompt and not self.turn.guessed) or start == 0:
                self._opened = True
                return
            span *= 2

    def _read(self, start: int, end: int, *, partial_head: bool = False) -> None:
        with self.path.open("rb") as file:
            file.seek(start)
            chunk = self._rest + file.read(end - start)
        self._offset = end
        self._mark = chunk[-_MARK_BYTES:]
        lines = chunk.split(b"\n")
        self._rest = lines.pop()  # 最后一段可能写了一半
        if partial_head and lines:
            lines.pop(0)
        for line in lines:
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                self._feed(row)

    def _feed(self, row: dict[str, Any]) -> None:
        kind = row.get("type")
        if kind == "queue-operation":
            if row.get("operation") == "enqueue" and isinstance(row.get("content"), str):
                self.queued.append((row["content"], str(row.get("timestamp", ""))))
                del self.queued[:-32]
                self._notified(row["content"], row)  # 会话紧接着结束时，通知只留在这一行
            return
        if row.get("isSidechain"):
            return
        if kind == "attachment":
            self._queued_command(row)
        if kind not in ("user", "assistant"):
            return
        message = row.get("message")
        if not isinstance(message, dict):
            return
        if kind == "user":
            self._user(row, message)
        else:
            self._assistant(row, message)

    def _begin(self, prompt: str, source: str, row: dict[str, Any]) -> None:
        at = _seconds(row.get("timestamp"))
        self.turn = Turn(prompt=prompt, source=source, at=str(row.get("timestamp", "")), began=at, last=at)

    def _results(self, row: dict[str, Any], content: Any) -> bool:
        """工具结果。是的话返回 True。一行里可能有几个结果，``toolUseResult`` 只有一份，转后台的工具都是一个结果一行。"""
        blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"] if isinstance(content, list) else []
        detail = row.get("toolUseResult")
        if blocks and self.turn is not None:
            self.turn.tool_finished(_seconds(row.get("timestamp")))
            self.turn.last = max(self.turn.last, _seconds(row.get("timestamp")))
        for block in blocks:
            call = self.running.pop(str(block.get("tool_use_id")), None)
            launched = isinstance(detail, dict) and (detail.get("status") == "async_launched" or detail.get("backgroundTaskId"))
            if call is not None and launched:
                label = call.label or str(detail.get("workflowName") or detail.get("description") or "")
                self.background[call.id] = replace(call, label=label, agent=str(detail.get("agentId") or ""), run=str(detail.get("runId") or ""))
                self._tasks[str(detail.get("taskId") or detail.get("backgroundTaskId") or detail.get("agentId"))] = call.id
            elif call is not None and call.name in _STOP_TOOLS and isinstance(detail, dict) and not block.get("is_error"):
                # Bash 和工作流被停掉时没有结束通知
                self.background.pop(self._tasks.pop(str(detail.get("task_id")), ""), None)
        return bool(blocks) or detail is not None

    def _notified(self, text: str, row: dict[str, Any]) -> None:
        """后台任务结束的通知。子代理停下时可能再发一次，已经没有这一项也没关系。
        任务结束也算这一轮的进展：总用时要算到这里，Claude 接着回话前这一轮也还没完。"""
        ended = [call for call in (self.background.pop(call_id, None) for call_id in _NOTIFIED.findall(text)) if call is not None]
        at = _seconds(row.get("timestamp"))
        if ended and at:
            self.notified_at = max(self.notified_at, at)
            if self.turn is not None and any(call.at >= self.turn.began for call in ended):  # 这一轮派出去的才算这一轮的
                self.turn.last = max(self.turn.last, at)

    def awaiting(self, now: float) -> bool:
        """后台任务刚结束，Claude 还没回话：通知之后记录里还没有新的输出。"""
        last = self.turn.last if self.turn is not None else 0.0
        return 0 < now - self.notified_at < REPLY_WAIT_SECONDS and last <= self.notified_at

    def _user(self, row: dict[str, Any], message: dict[str, Any]) -> None:
        content = message.get("content")
        if self._results(row, content) or row.get("isCompactSummary"):  # 压缩摘要接着上一轮，不是谁问的
            return
        raw = _text_of(content)
        self._notified(raw, row)
        origin = row.get("origin")
        peer = isinstance(origin, dict) and origin.get("kind") == "peer"
        if row.get("isMeta") and not peer:  # hook 和系统塞进来的
            return
        if isinstance(origin, dict) and origin.get("kind") not in ("human", "peer"):
            return
        if raw.lstrip().startswith("<local-command"):  # 这条斜杠命令在本地跑完了，不算一轮
            self._command = None
        text = _peer_body(raw) if peer else _prompt_text(raw)
        if not text:
            if raw.lstrip().startswith("[Request interrupted") and self.turn is not None:
                self.turn.stopped = True
                self._ended_at = self.turn.last
            return
        self.running.clear()  # 新的一轮开始了，上一轮没写结果的工具是被打断的
        if not peer and _COMMAND.search(raw):
            self._command = (text, str(row.get("timestamp", "")))
            return
        self._command = None
        self._begin(text, "peer" if peer else "user", row)

    def _queued_command(self, row: dict[str, Any]) -> None:
        """忙时收到的消息：后面的回复是回它的，所以也算一轮的开头。"""
        attachment = row.get("attachment")
        if not isinstance(attachment, dict) or attachment.get("type") != "queued_command":
            return
        raw = _text_of(attachment.get("prompt"))
        self._notified(raw, row)
        origin = attachment.get("origin")
        kind = origin.get("kind") if isinstance(origin, dict) else None
        peer = _ENVELOPE.match(raw) is not None
        if kind == "task-notification" or (kind == "peer" and not peer):  # 子代理的汇报不是谁问的
            return
        text = _peer_body(raw) if peer else _prompt_text(raw)
        if not text:
            return
        if self.turn is not None and self.turn.prompt and not self.turn.done:  # 运行中插的话：总用时不重新算
            self.turn.interject(text)
        else:
            guessed = self.turn is None or not self.turn.prompt
            self._begin(text, "peer" if peer else "user", row)
            self.turn.guessed = guessed

    def _measure(self, row: dict[str, Any], message: dict[str, Any]) -> None:
        """这次回复的模型、思考强度和上下文大小。出错时本地生成的那条模型写着 ``<synthetic>``，不算。"""
        model = message.get("model")
        if isinstance(model, str) and model and not model.startswith("<"):
            self.model = model
            effort = row.get("effort")
            self.effort = effort if isinstance(effort, str) else ""
        usage = message.get("usage")
        if isinstance(usage, dict):
            tokens = [usage.get(key) for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
            total = sum(value for value in tokens if isinstance(value, int))
            self.context = total or self.context

    def _resume(self, turn: Turn, at: float, timestamp: str, message: str) -> Turn:
        """说完之后又接着说：后台任务结束的通知会让 Claude 回话。像 Codex 那样算同一个任务，时间流和总用时接着走，
        只是最后的回复换成新说的。隔得太久（:data:`RESUME_SECONDS`）才算新的一轮，用时从这里重新算。
        同一条回复拆成的几行都带着最后的 ``stop_reason``，还是这条回复的话不算接着说。"""
        if not (turn.done or turn.stopped) or (message and message == self._ended):
            return turn
        if at - max(self._ended_at or turn.last, self.notified_at) >= RESUME_SECONDS:
            self.turn = Turn(prompt=turn.prompt, source=turn.source, tools=0, at=timestamp, began=at, last=at)
            return self.turn
        turn.done = turn.stopped = False
        turn.text = ""
        return turn

    def _plan(self, update: Any, at: float) -> None:
        """跟着计划工具改计划。只有增量、没读到整份计划时（整份在读的范围之前）不改。"""
        if not isinstance(update, dict):
            return
        items = update.get("items")
        if isinstance(items, list):
            self.plan = [{"t": str(item.get("content", "")), "s": str(item.get("status", "pending"))} for item in items if isinstance(item, dict)]
        elif not self.plan:
            return
        if update.get("advance"):
            current = next((i for i, item in enumerate(self.plan) if item["s"] == "in_progress"), None)
            if current is not None:
                self.plan[current]["s"] = "completed"
            following = next((i for i, item in enumerate(self.plan) if item["s"] == "pending" and (current is None or i > current)), None)
            if following is not None:
                self.plan[following]["s"] = "in_progress"
        done = update.get("done")
        for number in done if isinstance(done, list) else ():
            if isinstance(number, int) and 0 < number <= len(self.plan):
                self.plan[number - 1]["s"] = "completed"
        start = update.get("start")
        if isinstance(start, int) and 0 < start <= len(self.plan):
            self.plan[start - 1]["s"] = "in_progress"
        self.plan_at = at

    def _assistant(self, row: dict[str, Any], message: dict[str, Any]) -> None:
        if str(message.get("model", "")).startswith("<") and not row.get("isApiErrorMessage"):  # 重开会话时本地补的那句，不是回复
            return
        timestamp = str(row.get("timestamp", ""))
        at = _seconds(timestamp)
        if self._command is not None:  # Claude 回话了，那条斜杠命令才算一轮
            prompt, began = self._command
            self._command = None
            self.turn = Turn(prompt=prompt, at=began, began=_seconds(began), last=at)
        if self.turn is None:
            self.turn = Turn(at=timestamp, began=at, last=at)
        self._measure(row, message)
        turn = self.turn
        turn.last = max(turn.last, at)
        content = message.get("content")
        reply = str(message.get("id") or "")
        for block in content if isinstance(content, list) else ():
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                turn = self._resume(turn, at, timestamp, reply)
                said = str(block.get("text", ""))
                turn.text = f"{turn.text}\n\n{said}" if turn.text else said
                turn.say(said)
            elif block.get("type") == "tool_use":
                turn = self._resume(turn, at, timestamp, reply)
                name = _short_name(str(block.get("name", "")))
                tool_input = block.get("input")
                if block.get("name") == PLAN_TOOL:
                    self._plan(tool_input, at)
                described = isinstance(tool_input, dict) and bool(tool_input.get("description"))
                call = Call(str(block.get("id", "")), name, _describe(name, tool_input), at, described)
                self.running[call.id] = call
                turn.use_tool(at)
                turn.text = ""
        # 思考和正文各占一行，都写着 end_turn：只有思考的那行还没说完
        thinking = isinstance(content, list) and bool(content) and all(isinstance(b, dict) and b.get("type") == "thinking" for b in content)
        if (message.get("stop_reason") == "end_turn" and not thinking) or row.get("isApiErrorMessage"):
            turn.done = True
            self._ended, self._ended_at = reply, turn.last
            turn.close_tools()

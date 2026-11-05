"""眼镜上的一次会话：看回复、换会话、语音发指令。

和 reader 的 ``AgentSession`` 一样，所有逻辑都在这里，收一条消息、返回要发回去的消息列表，不碰网络，方便单测。
Claude 那边的事都经过 ``link``（:class:`~glassclaude.hub.Hub`）。

- 页面四种状态 ``m``：0 看回复，1 选会话，2 确认草稿，3 运行中。页面按键时自己改，服务也能改（来了新草稿），都写在主数据里。
- 回复分页放进 6 个槽排成的环：翻页只是本地换显示哪个槽，服务再补上离得远的槽。做法和 reader 的文字版一样。
- 运行中左边是时间流（``layout.py``），底行是正在跑的工具，右边是总用时、模型、子代理和后台任务、三个圆环。
  时间流翻页要走服务，有两秒延迟。全部结束且没有任务时右栏收起，回复铺满全屏。
- 麦克风一直开着，旁人的话也会变成 ``UTTERANCE``，所以任何一句话都只变成草稿，单击才发。发之前核对三样：
  版本号（等单击送到的两秒里草稿可能又变了）、
  草稿放不放得下（放不下的部分用户看不到，所以根本不收）。列表开着时不弹草稿，不然光标下的单击就变成了发送。
- 发出去之后等回执：等不到时明确告诉用户“不确定发出去没有”，这条提示不会被新回复冲掉，发失败的话草稿放回来。
- 眼镜重启和屏幕被持续改写有关（reader 上实测过），所以进度合并着发，至少隔 :data:`LIVE_SECONDS`。
  用时一直在变，运行中就是这个频率。2 秒一次是 0.5 次每秒，在 reader 出过重启的范围里，要上真机看，重启的话调大。一轮开始和结束马上发。
- 会话列表在看回复时跟着更新，打开时不用等网络。列表开着时不更新，不然光标下的那一行会变。选中的会话排第一行，
  打开列表时光标在 0，这样服务不用写光标，也就不会和用户的滑动抢。
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from collections.abc import Iterator
from typing import Any, Protocol

from dataclasses import dataclass

from .layout import blank_rows, clock, live_pages, row_data, stream_rows
from .text import BODY_WIDTH, NARROW_WIDTH, paginate, wrapped

__all__ = ["DRAFT", "LIST_ROWS", "LIVE", "PAGE_KEYWORD", "PICK", "READ", "SLOTS", "SPEC_VERSION", "Link", "Panel"]

log = logging.getLogger(__name__)

SPEC_VERSION = "1.0"
#: 平台的 pages 列表里只有 templateId 和说明，按说明里的这个词认
PAGE_KEYWORD = "claude-panel"
#: 环上的槽数，和 pages/claude-panel/template.xml 一致；当前页前面留 AHEAD 页、后面留 BEHIND 页
SLOTS, AHEAD, BEHIND = 6, 3, 2
#: 页面的四种状态
READ, PICK, DRAFT, LIVE = 0, 1, 2, 3
#: 运行中的进度至少隔这么久才发给眼镜一次。原来是 5 秒，用户在真机上嫌慢，要一到两秒。用时也靠这个走
#: （2026-11-04 试过让眼镜用 <tick> 自己走秒：每秒的刷新会把列表竖线的滑动动画重播一遍，竖线来回闪，用户选了改回服务推）
LIVE_SECONDS = 2.0
#: 没有草稿时的提示留多久
HINT_SECONDS = 5.0
#: 右栏最多放几个任务
TASK_ROWS = 3
#: 限额到这么多圆环才变亮，平时不抢正文
BRIGHT_PERCENT = 80
#: 会话列表的行数、行高和第一行的 top
LIST_ROWS, ROW_HEIGHT, LIST_TOP = 5, 38, 48
#: 底行的样式，页面查 F 这张表。实心块只在要用户动手时出现
FOOT_NONE, FOOT_LIVE, FOOT_NEED, FOOT_QUIET = 0, 1, 2, 3
#: 上报走到服务要这么久。提示出来之前按的键不算看到了提示
REPORT_SECONDS = 3.0
#: 平台约 120 秒没有业务消息就结束会话
KEEPALIVE_SECONDS = 90
#: 这么久没操作就不再保活，会话结束，麦克风才会关
IDLE_LIMIT_SECONDS = 1800
#: 比这短的话不算指令：“嗯”“好”
MIN_DRAFT_CHARS = 2
#: 草稿框的尺寸，和页面一致。放不下的话不收：看不到的部分不能发
DRAFT_SIZE, DRAFT_LINES = 24, 5
#: 发出去的话在时间流末尾“排队中”最多挂这么久
QUEUED_SECONDS = 600
#: 发出去这么久没有回执，就告诉用户不确定。写进会话之后要多等一会儿：Mac 看记录文件等 15 秒
SEND_WAIT_SECONDS, RECEIPT_WAIT_SECONDS = 20.0, 25.0
#: 翻不动时页面报同一个槽，可能是更新丢了。全量重发至少隔这么久
RESEND_SECONDS = 5.0
_HEADER_CHARS = 18
_PROMPT_CHARS = 60
_FOOT_CHARS = 60
_LIST_CHARS = 24
_STATES = {"busy": "运行中", "idle": "已完成", "waiting": "等你确认"}
#: 语音只认这几句固定说法，别的话都是草稿
_LIST_WORDS = frozenset({"切换会话", "换个会话", "会话列表", "看看会话", "有哪些会话"})
_CANCEL_WORDS = frozenset({"取消", "不发了", "算了", "清空"})
#: 调眼镜真实亮度的设备工具（平台文档的名字，阅读器用的同一个）。能不能用以 SESSION_INIT.tools 为准
BRIGHTNESS_TOOL = "change_brightness"
_DIGITS = "零一二三四五六七八九十百两"
#: “亮度调到五十”“把亮度调成 80%”“亮度三”。要以“亮度”开头，旁人随口一句里带个“亮”字不算
_BRIGHTNESS = re.compile(rf"(?:帮我|请)?(?:把|将|调)?亮度(?:调到|调成|调为|改成|改到|设为|设成|到|调)?(?:百分之)?([0-9{_DIGITS}]+)[%％]?吧?")
#: 相对的说法：没法读到眼镜现在的亮度，从上次设的数加减，没设过从 50 算
_BRIGHTER = re.compile(r"(?:帮我|请|再)?(?:把|将)?(?:亮度)?(?:调亮|调高|亮)一?[点些]吧?")
_DIMMER = re.compile(r"(?:帮我|请|再)?(?:把|将)?(?:亮度)?(?:调暗|调低|暗)一?[点些]吧?")
BRIGHTNESS_STEP, BRIGHTNESS_START = 20, 50
_NOT_CONNECTED = "电脑没有连上。看看 Mac 上的桥接程序在不在运行，电脑是不是休眠了。"
_NO_SESSIONS = "电脑上没有正在运行的 Claude Code 会话。"
_UNSURE = "不确定发出去没有，先看电脑"
_MISSING = object()


@dataclass
class _Outgoing:
    """发出去、等回执的一条指令。"""

    command: str
    text: str
    target: str
    at: float
    posted: bool = False


class Link(Protocol):
    """Claude 那边，由 Hub 实现。"""

    online: bool
    sessions: list[dict[str, Any]]
    last_selected: str | None

    def turn(self, session: str) -> dict[str, Any] | None: ...
    def watch(self, session: str | None) -> None: ...
    def send(self, session: str, text: str) -> str | None: ...


def _ring(step: int) -> list[dict[str, int]]:
    return [{"n": (i + step) % SLOTS} for i in range(SLOTS)]


def _cycle(count: int, step: int) -> list[dict[str, int]]:
    """光标的下一个、上一个，首尾相接。长度固定：下标没用到的也要有，数组只能整个发。
    ``d`` 是光标到了这一行时竖线动画的起点：刚才那一行相对这一行的位置。页面先写光标再查这个值，所以按到达的行查。"""
    return [{"n": (i + step) % count, "d": ((i - step) % count - i) * ROW_HEIGHT} if i < count else {"n": 0, "d": 0} for i in range(LIST_ROWS)]


_WHITE, _DIM, _QUIET, _BLACK = "#FFFFFF", "#A0A0A0", "#A0A0A0", "#000000"
#: 页面查的表里不变的那些，打开时发一次。F 是底行的样式，下标是 FOOT_*；W 是提问的宽度、底行的宽度和右边字的位置，下标是 rp；C 是列表每行的颜色，下标是光标
PAGE_TABLES = {
    "nx": _ring(1),
    "pv": _ring(-1),
    "tt": [{"n": LIST_TOP + i * ROW_HEIGHT} for i in range(LIST_ROWS)],
    "F": [
        {"b": _BLACK, "c": _BLACK, "d": _BLACK},
        {"b": _BLACK, "c": _WHITE, "d": _WHITE},
        {"b": _WHITE, "c": _BLACK, "d": _WHITE},
        {"b": _BLACK, "c": _QUIET, "d": _BLACK},
    ],
    "W": [{"q": 594, "f": 608, "x": 504}, {"q": 386, "f": 408, "x": 296}],
}


def _squeeze(text: str) -> str:
    return re.sub(r"[，。！？、,.!?\s]", "", text)


def _number(text: str) -> int | None:
    """``"50"`` / ``"五"`` / ``"八十"`` / ``"一百"`` → 数字，太复杂的中文数字不认。"""
    if text.isdigit():
        return int(text)
    digits = {c: i for i, c in enumerate("零一二三四五六七八九")} | {"两": 2}
    if len(text) == 1 and text in digits:
        return digits[text]
    if text in ("百", "一百"):
        return 100
    if "十" in text and "百" not in text:
        tens, _, ones = text.partition("十")
        if (not tens or tens in digits) and (not ones or ones in digits):
            return digits.get(tens, 1) * 10 + digits.get(ones, 0)
    return None


def brightness_command(spoken: str, last: int | None) -> int | None:
    """这句话是调亮度的就返回要设的值（0～100），不是返回 None。只说一位数（“亮度五”）当成十档里的第几档。"""
    if m := _BRIGHTNESS.fullmatch(spoken):
        value = _number(m.group(1))
        if value is None or value > 100:
            return None
        return value * 10 if 0 < value <= 10 else value
    base = BRIGHTNESS_START if last is None else last
    if _BRIGHTER.fullmatch(spoken):
        return min(base + BRIGHTNESS_STEP, 100)
    if _DIMMER.fullmatch(spoken):
        return max(base - BRIGHTNESS_STEP, 0)
    return None


def _draft_text(draft: str) -> str:
    """草稿框只放得下 :data:`DRAFT_LINES` 行。再长也收（用户不要“分开说”），框里显示最后几行，开头写个省略号。"""
    lines = wrapped(draft, BODY_WIDTH, DRAFT_SIZE)
    if len(lines) > DRAFT_LINES:
        lines = ["…" + lines[-DRAFT_LINES].lstrip(), *lines[-DRAFT_LINES + 1:]]
    return "\n".join(lines)


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[:limit - 1]}…"


def _wan(tokens: int) -> str:
    return f"{tokens / 10000:.1f} 万" if tokens < 1000000 else f"{tokens // 10000} 万"


def _task_line(task: dict[str, Any], now: float) -> tuple[str, str]:
    """任务的两行：名字，以及是什么、跑了多久、用了多少 token。"""
    spent = clock(now - float(task.get("at") or now))
    tokens = task.get("tokens")
    kind = task.get("kind")
    if kind == "workflow":
        head = f"工作流 {task['left']}" if task.get("left") is not None else "工作流"
    else:
        head = "子代理" if kind == "agent" else "后台"
    parts = [head, spent, *([_wan(tokens)] if tokens else [])]
    return str(task.get("name", "")), " · ".join(parts)


#: 页面可见、焦点、亮灭屏的上报，只记日志
_LIFECYCLE = ("show", "hide", "focus", "blur", "screen_on", "screen_off")
_PLAN_STATES = {"completed": "已完成", "in_progress": "进行中"}


def _plan_lines(plan: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """计划占任务那几格：从正在做的一步往下排，快到底时往回补已完成的，格子尽量排满。"""
    states = [str(item.get("s", "")) for item in plan]
    current = next((i for i, state in enumerate(states) if state == "in_progress"), None)
    if current is None:
        current = next((i for i, state in enumerate(states) if state != "completed"), len(plan) - 1)
    first = max(0, min(current, len(plan) - TASK_ROWS))
    return [(str(item.get("t", "")), f"{i + 1}/{len(plan)} · {_PLAN_STATES.get(states[i], '待办')}") for i, item in enumerate(plan[first:first + TASK_ROWS], first)]


def _gauge(number: int, percent: int | None) -> dict[str, Any]:
    """一个圆环：两个半环各转多少度，环里的数，弧的颜色。读不到时只有底环。"""
    value = min(max(percent or 0, 0), 100)
    degrees = value * 18 // 5
    return {
        f"a{number}": min(degrees, 180),
        f"e{number}": max(degrees - 180, 0),
        f"m{number}": "--" if percent is None else str(percent),
        f"u{number}": _WHITE if value >= BRIGHT_PERCENT else _DIM,
    }


class Panel:
    """一次眼镜会话的状态。:meth:`handle` 收一条平台消息，返回要发回去的消息列表。"""

    def __init__(self, session_id: str, link: Link, allowed_devices: frozenset[str] = frozenset()):
        self.id = session_id
        self.link = link
        self.allowed_devices = allowed_devices
        self.epoch = 0
        self.template_id = ""
        self.page_uuid = ""
        self.selected: str | None = None
        self.pages = [""]
        self.page = self.slot = 0
        self.loaded: tuple[str, str, bool, bool] | None = None
        self.picking = False
        self.live = False  # 这一轮还在跑：看时间流，不看回复
        self.overview = False  # 回复翻到最后一页再往后滑：回到两栏界面看时间流和右栏，这一轮已经完了
        self.mode_sent_at = float("-inf")  # 上次发 m 的时刻
        self.queued: list[tuple[str, str, float]] = []  # 发出去了、会话记录里还没出现的话：(会话, 原文, 发出时刻)，时间流末尾先显示
        self.device_tools: set[str] = set()  # 这次会话平台开放的设备工具
        self.brightness: int | None = None  # 上次设的眼镜亮度，读不到真实值
        self.calls = 0
        self.input_mode = "ASR_TEXT"  # 插件的通信方式：ASR_TEXT 平台发识别好的话，AUDIO 平台推声音、自己识别（hearing.py）
        self.back = 0  # 时间流往回翻了几页，0 是跟着最新的
        self.anchor = 0  # 往回翻时只看这么多行，后来的行不能把用户正在看的内容挤走
        self.progress: dict[str, Any] = {}
        self.held: dict[str, Any] | None = None  # 到时间才显示的进度
        self.pushed_at = float("-inf")
        self.rows: list[dict[str, Any]] = []
        self.draft = ""
        self.draft_version = 0
        self.draft_at = 0.0  # 最后一次说话的时刻，追加用
        self.target: str | None = None  # 草稿发给谁：定稿时屏幕上写的那个会话，不跟着光标变
        self.notice = ""
        self.hint = ""  # 只和草稿有关的提示，草稿没了就清
        self.hint_at = 0.0  # 没有草稿时的提示没人清，到时间自己走
        self.alert = ""  # 要用户看到的提示，新回复冲不掉，下一次操作才清
        self.alert_at = float("-inf")
        self.offline_at = 0.0  # Mac 断开的时刻，用时停在这里
        self.outgoing: dict[str, _Outgoing] = {}
        self.selected_pid = 0
        self.sent: dict[str, Any] = {}
        self.closing = False
        self.socket: Any = None
        self.emit: Any = None
        self.resent_at = float("-inf")
        self.last_action = time.monotonic()

    # ------------------------------------------------------------------ 协议

    def envelope(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"specVersion": SPEC_VERSION, "sessionId": self.id, "type": kind, "payload": payload}

    def ready(self, mode: str) -> dict[str, Any]:
        return self.envelope("SESSION_READY", {"mode": mode, "connectionEpoch": self.epoch})

    def error(self, code: str, message: str, *, fatal: bool = False) -> dict[str, Any]:
        return self.envelope("ERROR", {"scope": "SESSION", "code": code, "message": message, "fatal": fatal})

    def speak(self, text: str) -> dict[str, Any]:
        action = {"speechId": f"speech_{uuid.uuid4().hex[:8]}", "index": 1, "hasNext": False, "text": text}
        return self.envelope("OUTPUT", {"actions": [{"type": "SPEAK", "payload": action}]})

    def _show(self, mode: str, data: dict[str, Any]) -> dict[str, Any]:
        props: dict[str, Any] = {"data": data}
        if mode == "OPEN":
            props["templateId"] = self.template_id
        view = {"type": "TEMPLATE", "props": props}
        action = {"pageUuid": self.page_uuid, "display": {"mode": mode}, "view": view}
        return self.envelope("OUTPUT", {"actions": [{"type": "SHOW_UI", "payload": action}]})

    def start(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """第一次连接。进入时说的那句话不当指令：多半只是打开技能的说法。"""
        identity = payload.get("identity") or {}
        device = str(identity.get("deviceOpenId") or "")
        log.info("眼镜会话 %s：设备 %r", self.id, device)
        if self.allowed_devices and device not in self.allowed_devices:
            log.warning("拒绝设备 %r", device)
            self.closing = True
            return [self.error("FORBIDDEN_DEVICE", "这副眼镜没有绑定这台电脑", fatal=True)]
        pages = payload.get("pages") or []
        wanted = [p for p in pages if PAGE_KEYWORD in str(p.get("description", ""))]
        # 服务重启后平台重连时可能不再带 pages（文档说沿用原来的），就用配置里记下的页面
        self.template_id = next((str(p.get("templateId", "")) for p in wanted or pages), "") or os.environ.get("GLASSCLAUDE_TEMPLATE_ID", "")
        if not self.template_id:
            log.warning("平台没有下发页面：%s", pages)
            return [self.speak("眼镜页面还没配置好，没办法显示。")]
        log.info("会话 %s 用页面 %s，通信方式 %s", self.id, self.template_id, payload.get("inputMode") or "ASR_TEXT")  # 这个 ID 可以填进 GLASSCLAUDE_TEMPLATE_ID
        self.input_mode = str(payload.get("inputMode") or "ASR_TEXT")
        tools = payload.get("tools") or []
        log.info("会话 %s 可用工具：%s", self.id, [t.get("name") for t in tools if isinstance(t, dict)])
        self.device_tools = {str(t.get("name")) for t in tools if isinstance(t, dict)}
        self.page_uuid = f"page_{uuid.uuid4().hex[:12]}"
        ids = [s["id"] for s in self.link.sessions]
        self._select(self.link.last_selected if self.link.last_selected in ids else next(iter(ids), None))
        self._reload_rows()
        data = self._data()
        data = {**PAGE_TABLES, **data}
        self.sent = dict(data)
        return [self._show("OPEN", data)]

    def resumed(self) -> list[dict[str, Any]]:
        """断线重连：断线时丢的更新补不回来，全量重发。页面自己写的字段不发，它们可能比这边新。"""
        if not self.page_uuid:
            return []
        # m 也要重发：一轮结束的更新可能丢在断开的连接上，页面还停在运行中，时间流已经是空的。列表开着时这样会关掉它，上报到了会再打开
        self.sent = {key: self.sent[key] for key in ("i", "c") if key in self.sent}
        self._follow_session()
        if not self.picking:
            self._reload_rows()
        self.link.watch(self.selected)
        return self._refresh()

    def handle(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        kind, payload = message.get("type"), message.get("payload") or {}
        if kind == "UTTERANCE":
            return self._utterance(str(payload.get("text", "")))
        if kind == "UI_ACTION":
            self.last_action = time.monotonic()
            return self._action(payload)
        if kind == "TOOL_RESULT":
            return self._tool_result(payload)
        if kind == "ERROR":
            log.warning("平台报错（会话 %s）：%s", self.id, payload)
        else:
            log.info("忽略消息 %s（会话 %s）", kind, self.id)
        return []

    def expired(self) -> bool:
        """太久没操作。旁人说话不算操作，不然麦克风永远关不掉。"""
        return time.monotonic() - self.last_action > IDLE_LIMIT_SECONDS

    def keepalive(self) -> list[dict[str, Any]]:
        """重发一个不变的字段，算一条业务消息。"""
        if not self.page_uuid:
            return []
        return [self._show("UPDATE", {"h": self.sent.get("h", "")})]

    def failed(self) -> list[dict[str, Any]]:
        return [self.error("INTERNAL", "处理这条消息时出错了")]

    # ------------------------------------------------------------------ 页面数据

    def _session(self) -> dict[str, Any] | None:
        return next((s for s in self.link.sessions if s["id"] == self.selected), None)

    @property
    def mode(self) -> int:
        return PICK if self.picking else DRAFT if self.draft else LIVE if self.live or self.overview else READ

    def _now(self) -> float:
        """用时按 Mac 的钟算，云端的钟不一定和它一样。Mac 断开时停在最后听到的时刻，不能一直走下去。"""
        mac = float(self.progress.get("now") or 0)
        if not mac:
            return time.time()
        heard = float(self.progress.get("heard") or time.monotonic())
        if not self.link.online:
            return mac + max(self.offline_at - heard, 0.0)
        return mac + time.monotonic() - heard

    def _right_shown(self) -> bool:
        """这一轮在跑，或者还有子代理、后台任务在跑。"""
        return self.live or bool(self.progress.get("tasks"))

    def _total(self, now: float) -> str:
        """总用时，从第一句话算起，说完就定住。"""
        began = float(self.progress.get("began") or 0)
        if not began:
            return ""
        return clock((now if self.live else float(self.progress.get("last") or began)) - began)

    def _waiting_lines(self) -> list[dict[str, Any]]:
        """发出去的话：Claude 正忙时要等它做完手上这一步才读到，记录里才有。在那之前先排在时间流末尾，标着排队中。
        记录里出现了（插话或者新一轮的提问）就去掉，十分钟还没出现也去掉。"""
        steps = self.progress.get("steps") or []
        seen = [_squeeze(str(step.get("t", ""))) for step in steps if step.get("k") == "you"] + [_squeeze(str(self.progress.get("prompt") or ""))]
        now = time.monotonic()
        self.queued = [(session, text, at) for session, text, at in self.queued
                       if now - at < QUEUED_SECONDS and not any(_squeeze(text)[:20] in line or line[:20] and line[:20] in _squeeze(text) for line in seen)]
        return [{"k": "you", "t": f"{text}（排队中）"} for session, text, _ in self.queued if session == self.selected]

    def _live_view(self, now: float) -> tuple[list[dict[str, Any]], str]:
        """时间流现在这一页，和往回翻时的页码。回复页上也发：翻过最后一页时页面自己切到两栏，不等服务。"""
        if not self.progress:
            return blank_rows(), ""
        rows = stream_rows([*(self.progress.get("steps") or []), *self._waiting_lines()], now)
        if not self.back:
            self.anchor = len(rows)
        pages = live_pages(rows[:self.anchor])
        self.back = min(self.back, len(pages) - 1)
        index = len(pages) - 1 - self.back
        return row_data(pages[index]), f"{index + 1}/{len(pages)}" if self.back else ""

    def _footer(self, now: float, total: str, scrolled: str) -> tuple[str, str, int]:
        """底行：左边的字、右边的字、样式。要用户动手的优先，其次是正在跑的工具。"""
        progress = self.progress
        session = self._session()
        doing = progress.get("doing") if self.live else None
        spent = clock(now - float(doing.get("at") or now)) if doing else ""
        right = f"{scrolled} {spent}".strip()
        if self.alert:
            return self.alert, "", FOOT_NEED
        if self.link.online and session is not None and session.get("state") == "waiting":
            return (f"等你确认：{doing['text']}" if doing else "等你确认"), right, FOOT_NEED
        if self.hint:
            return self.hint, "", FOOT_QUIET
        if not self.link.online:
            return "电脑离线", "", FOOT_QUIET
        if session is None:
            return ("没有会话" if self.selected is None else "会话已结束"), "", FOOT_QUIET
        if self.live:
            if doing:
                return str(doing.get("text", "")), right, FOOT_LIVE
            if self.notice and self.outgoing:  # 回执只在指令路上时写，到了就该看 Claude 在做什么
                return self.notice, scrolled, FOOT_LIVE
            since = f"{scrolled} {clock(now - float(progress.get('last') or now))}".strip()
            if progress.get("waiting"):  # 主线说完了，整个任务等后台任务跑完才算完
                count = len(progress.get("tasks") or [])
                return (f"等 {count} 个后台任务" if count else "等 Claude 接着说"), since, FOOT_LIVE
            return "Claude 正在想", since, FOOT_LIVE
        if self.notice:
            return self.notice, "", FOOT_LIVE
        if not progress:
            return "", "", FOOT_NONE
        if self._right_shown() or self.overview or not total:
            return "已完成", "", FOOT_QUIET
        return f"已完成 · 用时 {total}", "", FOOT_QUIET

    def _tasks(self, now: float) -> dict[str, str]:
        """右栏中间：子代理、工作流和后台任务；都没有时，这一轮有计划就放计划。"""
        tasks = list(self.progress.get("tasks") or [])
        plan = self.progress.get("plan") or []
        lines = [_task_line(task, now) for task in tasks[:TASK_ROWS]] if tasks or not plan else _plan_lines(plan)
        if len(tasks) > TASK_ROWS:  # 放不下时最后一格写还有几个
            lines[-1] = (f"还有 {len(tasks) - TASK_ROWS + 1} 个", "")
        lines += [("", "")] * (TASK_ROWS - len(lines))
        return {key: value for i, (name, meta) in enumerate(lines) for key, value in ((f"n{i}", name), (f"d{i}", meta))}

    def _data(self) -> dict[str, Any]:
        session = self._session()
        progress = self.progress
        now = self._now()
        total = self._total(now)
        rows, scrolled = self._live_view(now)
        text, right, style = self._footer(now, total, scrolled)
        last = len(self.pages) - 1
        forward, backward = min(AHEAD, last - self.page), min(BEHIND, self.page)
        ring: list[dict[str, str]] = [{"t": "", "s": ""} for _ in range(SLOTS)]
        for offset in range(-backward, forward + 1):
            number = self.page + offset + 1
            ring[(self.slot + offset) % SLOTS] = {"t": self.pages[number - 1], "s": f"{number}/{last + 1}" if last else ""}
        name = _short(session["name"], _HEADER_CHARS) if session else "Claude"
        data: dict[str, Any] = {
            "h": name,
            "q": _short(str(progress.get("prompt") or name), _PROMPT_CHARS),
            "rp": 1 if self._right_shown() or self.overview else 0,
            "tm": total,
            "mo": str(progress.get("model") or ""),
            "ef": str(progress.get("effort") or ""),
            "ft": _short(text, _FOOT_CHARS),
            "fm": right,
            "fk": style,
            "R": rows,
            "P": ring,
            "i": self.slot,
            "c": 0,  # 模板的上报里要用，页面只在列表开着时改它，不上报
            "hi": (self.slot + forward) % SLOTS,
            "lo": (self.slot - backward) % SLOTS,
            "m": self.mode,
            "rb": LIVE if self.live or self.overview else READ,  # 关掉列表和草稿时回到哪：页面先切，不等服务
            "dv": self.draft_version,
            "dr": _draft_text(self.draft),
            "dt": f"发给 {_short(self._target_name(), _HEADER_CHARS)}",
            # 列表是页面自己打开的，等服务改这行要两秒，所以平时就写单击打开。草稿页和这个字段总是同一条更新到
            "df": self.alert or self.hint or ("单击发送" if self.draft and not self.picking else "单击打开"),
            "L": [{"a": _short(row["name"], _LIST_CHARS), "e": _STATES.get(row["state"], "")} for row in self.rows] + [{"a": "", "e": ""}] * (LIST_ROWS - len(self.rows)),
            **self._tasks(now),
            **_gauge(1, progress.get("context")),
            **_gauge(2, progress.get("five")),
            **_gauge(3, progress.get("week")),
        }
        count = max(len(self.rows), 1)
        data["ln"], data["lp"] = _cycle(count, 1), _cycle(count, -1)
        return data

    def _target_name(self) -> str:
        return next((s["name"] for s in self.link.sessions if s["id"] == self.target), "没有选中的会话")

    def _refresh(self) -> list[dict[str, Any]]:
        if not self.page_uuid:
            return []
        changed = {key: value for key, value in self._data().items() if self.sent.get(key, _MISSING) != value}
        if not changed:
            return []
        self.sent.update(changed)
        if "m" in changed:
            self.mode_sent_at = time.monotonic()
        return [self._show("UPDATE", changed)]

    def _reload_rows(self) -> None:
        current = [s for s in self.link.sessions if s["id"] == self.selected]
        others = [s for s in self.link.sessions if s["id"] != self.selected]
        self.rows = (current + others)[:LIST_ROWS]

    def _select(self, session: str | None) -> None:
        if session != self.selected:
            self.selected = session
            self.loaded = None
        self.selected_pid = next((int(s.get("pid") or 0) for s in self.link.sessions if s["id"] == session), 0)
        self.link.last_selected = session
        self.link.watch(session)
        self._load(self.link.turn(session) if session else None)

    def _load(self, turn: dict[str, Any] | None) -> None:
        """换内容。同一轮的内容没变就不动，用户翻到哪就在哪。"""
        self.progress, self.held = turn or {}, None
        self.live = turn is not None and not turn.get("done")
        strip = self._right_shown()
        if turn is None:
            text = "" if self.link.online and self.selected else _NOT_CONNECTED if not self.link.online else _NO_SESSIONS
            key = ("", text, True, False)
        else:
            text = str(turn.get("text") or "（这一轮没有文字回复）")
            key = (str(turn.get("prompt", "")), str(turn.get("text", "")), bool(turn.get("done")), strip)
        if key == self.loaded:
            return
        same_turn = self.loaded is not None and key[0] == self.loaded[0] and key[2] == self.loaded[2]
        if not same_turn:
            self.back = 0
            self.overview = False
        self.loaded = key
        if self.live:  # 运行中看时间流，回复页用不着
            self.pages, self.page = [""], 0
        else:
            self.pages = paginate(text, NARROW_WIDTH if strip else BODY_WIDTH)
            self.page = min(self.page, len(self.pages) - 1) if same_turn else 0
        if key[2]:  # 这一轮完了，“已收到”这类提示就过时了
            self.notice = ""

    # ------------------------------------------------------------------ 眼镜上的操作

    def _action(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        action = payload.get("action") or {}
        name, params = action.get("name"), action.get("params") or {}
        if payload.get("pageUuid") != self.page_uuid:
            return []  # 旧页面发来的
        log.info("页面上报 %s %s（会话 %s）", name, params, self.id)
        if time.monotonic() - self.alert_at >= REPORT_SECONDS:
            self.alert = ""
        if isinstance(params.get("c"), int):  # 页面按键时自己写的
            self.sent["c"] = params["c"]
        slot = params.get("slot")
        if name == "turn":
            return self._turn(slot)
        # 翻页的上报可能丢了，页面已经翻走了。先跟上，不然 i 会把页面拉回旧的一页
        behind = self._turn(slot) if isinstance(slot, int) and slot != self.slot else []
        # 上报里的 m 可能比还在路上的那条旧，不能信，所以回应带上 m，页面和服务才会收敛。放在跟页之后：不然草稿算重新上屏了一次。
        # 但服务这几秒没发过 m 时，路上没有别的 m，上报的就是页面现在的样子：一样的话不再发。打开列表后马上滑动，
        # 这条多余的更新正好让竖线的滑动重播一遍
        reported = params.get("m")
        if isinstance(reported, int) and time.monotonic() - self.mode_sent_at > REPORT_SECONDS:
            self.sent["m"] = reported
        else:
            self.sent.pop("m", None)
        return [*behind, *self._act(name, params)]

    def _act(self, name: Any, params: dict[str, Any]) -> list[dict[str, Any]]:
        if name == "list":
            self.notice = ""
            self.picking = True
            return self._refresh()
        if name == "older":
            self.back += 1  # 到头了 _live_view 会夹回来
            return self._refresh()
        if name == "newer":
            if self.overview and not self.back:  # 两栏看到最新了再往后滑：回到回复的第一页，转一圈
                self.overview = False
                self.page = 0
            self.back = max(self.back - 1, 0)
            return self._refresh()
        if name == "more":  # 回复翻过了最后一页，页面已经自己切到两栏
            self.overview = bool(self.progress) and not self.live
            self.back = 0
            return self._refresh()
        if name == "pick":
            return self._pick(params.get("c"))
        if name == "send":
            return self._send(params.get("dv"))
        if name == "drop":
            return self._drop(params.get("dv"))
        if name == "exit":  # 双击两次：页面自己关了，会话要由服务结束，麦克风才会关
            self.closing = True
            return []
        if name not in _LIFECYCLE:
            log.info("不认识的页面动作 %r", name)
        return self._refresh()  # 上报里的 t 可能改了页面开始数秒的时刻，计时要跟着重发

    def _turn(self, slot: Any) -> list[dict[str, Any]]:
        if not isinstance(slot, int) or not 0 <= slot < SLOTS:
            return []
        delta = (slot - self.slot) % SLOTS
        if delta > AHEAD:
            delta -= SLOTS
        if delta == 0 and time.monotonic() - self.resent_at >= RESEND_SECONDS:
            # 只有走到环的尽头翻不动才会报同一个槽：这边早到了，更新丢了，或者还在路上
            self.resent_at = time.monotonic()
            for key in ("P", "hi", "lo"):
                self.sent.pop(key, None)
        self.slot = slot
        self.page = min(max(self.page + delta, 0), len(self.pages) - 1)
        self.sent["i"] = slot
        return self._refresh()

    def _pick(self, cursor: Any) -> list[dict[str, Any]]:
        self.picking = False
        self.hint = ""
        if isinstance(cursor, int) and 0 <= cursor < len(self.rows):
            row = self.rows[cursor]  # 列表开着时行不更新：这一行可能 /clear 过，ID 换了，进程还是那个
            sessions = self.link.sessions
            alive = next((s for s in sessions if s["id"] == row["id"]), None) or next((s for s in sessions if s.get("pid") and s.get("pid") == row.get("pid")), None)
            self._select((alive or row)["id"])
        self._reload_rows()
        return self._refresh()

    def _send(self, version: Any) -> list[dict[str, Any]]:
        if not self.draft or version != self.draft_version:
            return self._stale_draft()
        if self.target not in [s["id"] for s in self.link.sessions]:
            self.hint = "电脑离线，没发出去" if not self.link.online else "这个会话已经结束了"
            return self._refresh()
        command = self.link.send(self.target, self.draft) if self.target else None
        if command is None:
            self.hint = "电脑离线，没发出去"
            return self._refresh()
        self.outgoing[command] = _Outgoing(command, self.draft, str(self.target), time.monotonic())
        self.queued.append((str(self.target), self.draft, time.monotonic()))
        self._clear_draft()
        self.notice = "发送中"
        return self._refresh()

    def _stale_draft(self) -> list[dict[str, Any]]:
        """屏幕上那一版不是现在这一版，草稿还在，让用户再看一眼。这条更新可能丢了，补一次。"""
        self.hint = "内容变了，再确认一次" if self.draft else ""
        for key in ("dr", "dv", "dt"):
            self.sent.pop(key, None)
        return self._refresh()

    def _drop(self, version: Any) -> list[dict[str, Any]]:
        if version != self.draft_version:  # 取消的是屏幕上那一版，用户又说了话就留着新的
            return self._stale_draft()
        self._clear_draft()
        return self._refresh()

    def _clear_draft(self) -> None:
        self.draft = self.hint = ""
        self.draft_version += 1

    # ------------------------------------------------------------------ 语音

    def _utterance(self, text: str) -> list[dict[str, Any]]:
        spoken = _squeeze(text)
        log.info("语音（会话 %s）：%d 字", self.id, len(text))  # 旁人的话也在里面，原文不能进日志
        if spoken in _LIST_WORDS:
            self.last_action = time.monotonic()
            self.notice = self.alert = ""
            self.picking = True
            self.sent.pop("c", None)  # 上次选的光标还留在页面上
            return self._refresh()
        if spoken in _CANCEL_WORDS:
            if not self.draft:
                return []
            self.last_action = time.monotonic()
            self._clear_draft()
            return self._refresh()
        level = brightness_command(spoken, self.brightness)
        if level is not None:  # 调亮度的话这里就办了，不进草稿、不发给 Claude
            self.last_action = time.monotonic()
            return self._set_brightness(level)
        if len(spoken) < MIN_DRAFT_CHARS:
            return []
        if self.selected is None:  # 确认页要写清楚发给谁
            self.hint, self.hint_at = "还没有会话，这句没记下", time.monotonic()
            return self._refresh()
        now = time.monotonic()
        appending = bool(self.draft)  # 新说的话都接在后面，草稿只在发出去或清掉时才空（原来隔 20 秒就换掉，用户不要）
        merged = f"{self.draft}{text.strip()}" if appending else text.strip()
        self.draft = merged
        if not appending:
            self.target = self.selected  # 草稿发给现在选中的会话，说到一半换会话也不变
        self.draft_at = now
        self.draft_version += 1
        self.hint = "有新草稿，选完再看" if self.picking else ""
        self.sent.pop("m", None)  # 带 m 的更新丢过的话，草稿就藏在后台了
        return self._refresh()

    def _set_brightness(self, level: int) -> list[dict[str, Any]]:
        """用平台的设备工具调眼镜亮度，只写不读。底行提示一下设了多少。"""
        now = time.monotonic()
        if BRIGHTNESS_TOOL not in self.device_tools:
            self.hint, self.hint_at = "这次会话平台没有开放亮度调节", now
            return self._refresh()
        self.brightness = level
        self.calls += 1
        self.hint, self.hint_at = f"亮度调到 {level}", now
        call = {"callId": f"brightness_{self.calls}", "name": BRIGHTNESS_TOOL, "arguments": {"operation": "SET", "value": level}}
        return [self.envelope("TOOL_CALL", call), *self._refresh()]

    def _tool_result(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """工具的结果。文档说只回 DISPATCHED，整条记下来，看实际带不带别的。"""
        log.info("工具结果（会话 %s）：%s", self.id, payload)
        if not str(payload.get("callId")).startswith("brightness_") or payload.get("status") in ("DISPATCHED", "SUCCESS"):
            return []
        self.hint, self.hint_at = "亮度没调成", time.monotonic()
        return self._refresh()

    # ------------------------------------------------------------------ Claude 那边的变化，由 Hub 转来

    def _follow_session(self) -> None:
        """选中的会话没了要跟上：/clear 之后进程和收件箱不变，只换了 ID。真结束了就把进度收起来，不然一直写着正在调用。
        草稿的收件人不跟着改：新对话没有上下文，发不出去比悄悄改投安全。"""
        sessions = self.link.sessions
        if self.selected is None:
            if sessions:
                self._select(sessions[0]["id"])
            return
        current = self._session()
        if current is not None:  # 同一个 ID 可能换了进程：恢复会话之后再 /clear，要跟的是新进程
            self.selected_pid = int(current.get("pid") or 0)
        if not self.link.online or current is not None:
            return
        renewed = next((s["id"] for s in sessions if self.selected_pid and s.get("pid") == self.selected_pid), None)
        if renewed:
            self._select(renewed)
        elif self._right_shown():  # 运行中桥接程序不发整段文字，Claude 说过的话在时间流里
            said = "\n\n".join(str(step.get("t", "")) for step in self.progress.get("steps") or [] if step.get("k") == "text")
            self._load({**self.progress, "text": self.progress.get("text") or said, "done": True, "doing": None, "tasks": []})

    def on_sessions(self) -> list[dict[str, Any]]:
        self._follow_session()
        if not self.picking:
            self._reload_rows()
        return self._refresh()

    def on_turn(self, turn: dict[str, Any]) -> list[dict[str, Any]]:
        if turn.get("session") != self.selected:
            return []
        started_or_ended = (turn.get("prompt"), turn.get("done")) != (self.progress.get("prompt"), self.progress.get("done"))
        if not started_or_ended and time.monotonic() - self.pushed_at < LIVE_SECONDS:
            self.held = turn
            return []
        return self._push(turn)

    def _push(self, turn: dict[str, Any]) -> list[dict[str, Any]]:
        self.pushed_at = time.monotonic()
        self._load(turn)
        return self._refresh()

    def tick(self) -> list[dict[str, Any]]:
        """每秒看一次：到时间了，把攒着的进度发出去；发出去的指令等太久了要告诉用户。"""
        out = self._give_up(list(self._overdue()))
        if self.hint and not self.draft and not self.picking and time.monotonic() - self.hint_at >= HINT_SECONDS:
            self.hint = ""
            out = [*out, *self._refresh()]
        if time.monotonic() - self.pushed_at >= LIVE_SECONDS:
            if self.held is not None:
                out = [*out, *self._push(self.held)]
            elif self.link.online and self._right_shown() and self.mode in (READ, LIVE):  # 用时要走；列表开着时不推，不然竖线的滑动会重播
                self.pushed_at = time.monotonic()
                out = [*out, *self._refresh()]
        return out

    def _overdue(self) -> Iterator[str]:
        now = time.monotonic()
        for item in self.outgoing.values():
            if now - item.at > (RECEIPT_WAIT_SECONDS if item.posted else SEND_WAIT_SECONDS):
                yield item.command

    def _give_up(self, commands: list[str]) -> list[dict[str, Any]]:
        if not commands:
            return []
        for command in commands:
            del self.outgoing[command]
        self.alert, self.alert_at = _UNSURE, time.monotonic()
        self.notice = ""
        return self._refresh()

    def on_sent(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        outgoing = self.outgoing.get(str(event.get("cmd")))
        if outgoing is None:
            return []
        state = event.get("state")
        if state == "posted":
            outgoing.posted, outgoing.at = True, time.monotonic()
            self.notice = "已发出"
            return self._refresh()
        del self.outgoing[outgoing.command]
        self.notice = ""
        if state == "delivered":
            self.notice = "Claude 已收到"
        elif state == "unconfirmed":
            self.alert, self.alert_at = "没等到回执，先看电脑", time.monotonic()
        else:
            self.alert, self.alert_at = f"没发出去：{event.get('error') or '未知原因'}", time.monotonic()
            self.queued = [item for item in self.queued if item[1] != outgoing.text]
            if not self.draft:  # 话放回来，不用重说
                self.draft, self.target, self.draft_at = outgoing.text, outgoing.target, time.monotonic()
                self.draft_version += 1
        return self._refresh()

    def on_link(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if self.link.online:
            self.link.watch(self.selected)
            self._follow_session()
        else:  # 指令可能在断线前到了 Mac，也可能没有
            self.offline_at = time.monotonic()
            out = self._give_up([item.command for item in self.outgoing.values() if not item.posted])
        if self.selected is None or self.loaded is None or not self.loaded[0]:
            self._load(self.link.turn(self.selected) if self.selected else None)
        return [*out, *self._refresh()]

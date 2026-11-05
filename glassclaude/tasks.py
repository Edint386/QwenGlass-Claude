"""子代理和工作流用了多少 token。

文件都在记录文件旁边的 ``<会话 ID>/subagents/`` 下：

- 子代理：``agent-<ID>.jsonl``，``agent-<ID>.meta.json`` 里的 ``toolUseId`` 对得上主线的工具调用。
- 工作流：``workflows/<run ID>/agent-*.jsonl``，``journal.jsonl`` 一行一件事。一次尝试以 ``result`` 或 ``failed`` 结束，重试再加一个 ``started``，
  所以 ``started`` 减这两样就是还有几个在跑。

token 是最后一次请求的输入、缓存和输出相加，这是它当前的上下文大小，和 Claude Code 自己报的数一致，不是累计消耗。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .transcript import AGENT_TOOLS, Call

__all__ = ["Tasks"]

#: 从文件尾往前读多少。最后一次请求的那行通常就在这里面，不在的话加倍往前
TAIL_BYTES = 256 * 1024
#: 文件这么久没动就当它已经不跑了。被打断的工作流收不到结束通知，不然会一直挂在屏幕上
QUIET_SECONDS = 1800.0
_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def _rows(path: Path, span: int | None = None) -> tuple[list[dict[str, Any]], bool]:
    """最后 ``span`` 字节里的行，``None`` 是整个文件。第二项说读到文件头了没有。"""
    try:
        with path.open("rb") as file:
            size = file.seek(0, 2)
            start = 0 if span is None else max(0, size - span)
            file.seek(start)
            lines = file.read().split(b"\n")
    except OSError:
        return [], True
    if start:
        lines.pop(0)  # 第一行可能只读到一半
    rows = []
    for line in lines:
        try:
            row = json.loads(line) if line else None
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows, start == 0


def _last_tokens(path: Path) -> int:
    span = TAIL_BYTES
    while True:
        rows, whole = _rows(path, span)
        for row in reversed(rows):
            message = row.get("message")
            usage = message.get("usage") if isinstance(message, dict) and row.get("type") == "assistant" else None
            if isinstance(usage, dict):
                return sum(value for key in _FIELDS if isinstance(value := usage.get(key), int))
        if whole:
            return 0
        span *= 4  # 一个大工具结果就能把最后一次请求挤出去


class Tasks:
    """跟着一个会话的 subagents 目录。文件没变就用上次的数，工作流可能有几十个代理。"""

    def __init__(self, transcript: Path):
        self.root = transcript.with_suffix("") / "subagents"
        self._seen: dict[Path, tuple[tuple[int, int], Any]] = {}
        self._agents: dict[str, str] = {}  # tool_use 的 id 对应哪个子代理：前台的要从 meta 文件找

    def _cached(self, path: Path, read: Any) -> Any:
        try:
            stat = path.stat()
        except OSError:
            return None
        stamp = (stat.st_mtime_ns, stat.st_size)
        seen = self._seen.get(path)
        if seen is not None and seen[0] == stamp:
            return seen[1]
        value = read(path)
        self._seen[path] = (stamp, value)
        return value

    def _agent_id(self, call: Call) -> str:
        if call.agent:
            return call.agent
        if call.id not in self._agents:
            for path in self.root.glob("agent-*.meta.json"):
                meta = self._cached(path, lambda p: next(iter(_rows(p)[0]), {}))
                if meta and meta.get("toolUseId") == call.id:
                    self._agents[call.id] = path.name.removeprefix("agent-").removesuffix(".meta.json")
                    break
        return self._agents.get(call.id, "")

    def agent_tokens(self, call: Call) -> int | None:
        agent = self._agent_id(call)
        return self._cached(self.root / f"agent-{agent}.jsonl", _last_tokens) if agent else None

    def workflow(self, call: Call) -> tuple[int | None, int | None]:
        """(token, 还有几个代理在跑)。工作流把各代理加起来，比它自己报的差约 3%。"""
        if not call.run:
            return None, None
        folder = self.root / "workflows" / call.run
        tokens = sum(self._cached(path, _last_tokens) or 0 for path in folder.glob("agent-*.jsonl"))
        kinds = self._cached(folder / "journal.jsonl", lambda p: [row.get("type") for row in _rows(p)[0]]) or []
        return tokens, max(kinds.count("started") - kinds.count("result") - kinds.count("failed"), 0)

    def quiet(self, call: Call, now: float) -> bool:
        """子代理或工作流的文件很久没动了。还没有文件的不算，后台 Bash 没有文件。"""
        if call.run:
            folder = self.root / "workflows" / call.run
            paths = [*folder.glob("agent-*.jsonl"), folder / "journal.jsonl"]
        else:
            agent = self._agent_id(call) if call.name in AGENT_TOOLS else ""
            paths = [self.root / f"agent-{agent}.jsonl"] if agent else []
        times = []
        for path in paths:
            try:
                times.append(path.stat().st_mtime)
            except OSError:
                continue
        return bool(times) and now - max(times) > QUIET_SECONDS

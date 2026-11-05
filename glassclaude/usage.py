"""读 mod 写的上下文和限额（``mod/glassclaude-usage``）。

限额只有 Claude Code 自己知道，本机别的地方没有。mod 每轮把数字写到 ``~/.claude/glassclaude/usage/<会话 ID>.json``。
没装 mod，或者会话是装之前开的，这里就读不到：限额留空，上下文窗口用别的会话报的。写到一半读不出来时下次再读。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = ["Usage", "read_usage"]

_LIMITS = {"five_hour": "five", "seven_day": "week"}


@dataclass(frozen=True)
class Usage:
    window: int = 0
    five: int | None = None
    week: int | None = None


def _load(path: Path) -> dict[str, Any]:
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _by_age(directory: Path) -> list[Path]:
    """新的在前。"""
    try:
        entries = [entry for entry in os.scandir(directory) if entry.name.endswith(".json")]
    except OSError:
        return []
    found = []
    for entry in entries:
        try:
            found.append((entry.stat().st_mtime, entry.path))
        except OSError:
            continue
    return [Path(path) for _, path in sorted(found, reverse=True)]


def _limits(record: dict[str, Any], now: float) -> dict[str, int | None]:
    limits = {}
    for limit in record.get("rateLimits") or []:
        if isinstance(limit, dict) and limit.get("kind") in _LIMITS:
            limits[_LIMITS[limit["kind"]]] = _percent(limit, now)
    return limits


def _percent(limit: dict[str, Any], now: float) -> int | None:
    """过了重置时间的读数是旧的：这个窗口已经从零开始了。"""
    used = limit.get("percentUsed")
    if not isinstance(used, int | float):
        return None
    try:
        resets = datetime.fromisoformat(str(limit.get("resetsAt", "")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        resets = None
    return 0 if resets is not None and resets <= now else int(used)


def read_usage(session: str, now: float, home: Path | None = None) -> Usage:
    """限额是账号的，用最新的一份读数：新开的会话第一轮之前没有读数，要接着往旧的找。上下文窗口先看这个会话自己的。"""
    directory = (home or Path.home()) / ".claude" / "glassclaude" / "usage"
    paths = _by_age(directory)
    if not paths:
        return Usage()
    limits: dict[str, int | None] = {}
    context: Any = _load(directory / f"{session}.json").get("context")
    for path in paths:
        record = _load(path)
        limits = limits or _limits(record, now)
        context = context or record.get("context")
        if limits and context:
            break
    window = context.get("window") if isinstance(context, dict) else 0
    return Usage(window if isinstance(window, int) else 0, limits.get("five"), limits.get("week"))

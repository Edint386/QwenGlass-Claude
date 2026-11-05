"""列出这台电脑上正在运行的 Claude Code 会话。

每个活着的会话在 ``~/.claude/sessions/<pid>.json`` 留一条记录：名字、目录、状态、收件箱 socket。
桌面版 Code 标签和终端里的 ``claude`` 都写这里，不用登录，也不用改设置。这是内部格式，文档没写，
``claude agents --json`` 输出的是同一批数据，但 ``claude`` 不在 PATH 上，路径随桌面版更新变，所以直接读文件。
"""

from __future__ import annotations

import glob
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = ["BUSY", "IDLE", "WAITING", "LocalSession", "live_sessions", "transcript_path"]

#: 会话状态。等你确认工具权限时是 WAITING，``waitingFor`` 写着在等什么
BUSY, IDLE, WAITING = "busy", "idle", "waiting"


@dataclass(frozen=True)
class LocalSession:
    id: str
    pid: int
    name: str
    cwd: str
    state: str
    waiting: str
    socket: str
    updated_at: int
    started_at: int = 0  # 这个进程启动的时刻，毫秒

    @property
    def project(self) -> str:
        return Path(self.cwd).name


def _alive(pid: int) -> bool:
    if sys.platform == "win32":  # Windows 上 os.kill 不是探一下，是直接结束那个进程
        raise RuntimeError("Windows 还不支持：会话探活和收件箱 socket 只在 macOS 上测过")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # 进程在，只是不归我们
        return True
    return True


def _is_socket(path: str) -> bool:
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except OSError:
        return False


def _parse(path: Path) -> LocalSession | None:
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):  # 正在写，或者刚被删
        return None
    pid, session_id, socket = record.get("pid"), record.get("sessionId"), record.get("messagingSocketPath")
    if not isinstance(pid, int) or not session_id or not socket:
        return None  # 没有收件箱的会话发不了消息：bare 模式
    if not _alive(pid) or not _is_socket(socket):
        return None  # 进程崩了留下的记录
    return LocalSession(
        id=str(session_id),
        pid=pid,
        name=str(record.get("name") or Path(str(record.get("cwd", ""))).name),
        cwd=str(record.get("cwd", "")),
        state=str(record.get("status", IDLE)),
        waiting=str(record.get("waitingFor") or ""),
        socket=str(socket),
        updated_at=int(record.get("updatedAt") or record.get("startedAt") or 0),
        started_at=int(record.get("startedAt") or 0),
    )


def live_sessions(home: Path | None = None) -> list[LocalSession]:
    """活着的会话，最近有动静的在前。"""
    directory = (home or Path.home()) / ".claude" / "sessions"
    found = [session for path in directory.glob("*.json") if (session := _parse(path))]
    return sorted(found, key=lambda s: s.updated_at, reverse=True)


def transcript_path(session_id: str, home: Path | None = None) -> Path | None:
    """这个会话的记录文件。目录名是工作目录转义来的，路径太长时还会截断加哈希，所以按文件名找。"""
    projects = (home or Path.home()) / ".claude" / "projects"
    return next(iter(projects.glob(f"*/{glob.escape(session_id)}.jsonl")), None)

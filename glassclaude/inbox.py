"""把一句话发进正在运行的 Claude Code 会话。

每个会话有一个 Unix socket 收件箱（文档 cross-session-messaging），消息是一行 JSON。文档写了脚本可以往里发，
但没写格式，这里的字段是从 Claude Code 2.1.309 的代码里看来的，实测外部进程能送达桌面版会话：

- 会话空闲时立刻开始新一轮，忙时在两次工具调用之间读到。
- Claude 看到的是“另一个会话发来的请求”，不算用户本人打字：它会照办，但权限提示照常弹，这条消息批准不了任何东西。
- socket 不回话。送没送到要看记录文件里有没有出现这句话（:class:`~glassclaude.transcript.Tail` 的 ``queued``）。
- 权限模式是 bypassPermissions 的会话不收：消息挂起五分钟后丢掉，记录文件里也不会出现。
"""

from __future__ import annotations

import json
import re
import socket
import time
import uuid

__all__ = ["SENDER", "envelope", "post", "unwrap"]

#: Claude 和用户在会话里看到的发件人
SENDER = "glasses"
_CONNECT_TIMEOUT = 3.0
#: macOS 上写完马上关连接，对方可能读不到。Claude Code 自己等 150 毫秒
_LINGER_SECONDS = 0.2
#: 告诉 Claude 这句话的来历和回复的去处。眼镜一屏只有几行字
_PREFACE = "（用户在千问眼镜上用语音发来这句话，可能有错别字。你的回复会显示在眼镜上，请简短，不要贴大段代码。）"
_CLOSING_TAG = re.compile(r"<\s*/\s*cross-session-message", re.I)


def envelope(text: str) -> str:
    """包成 Claude Code 自己用的格式，这样会话里显示发件人。正文里的结束标签要转义，不然会提前结束。"""
    body = _CLOSING_TAG.sub(r"<\\cross-session-message", f"{_PREFACE}\n{text}")
    return f'<cross-session-message from-name="{SENDER}">\n{body}\n</cross-session-message>'


def unwrap(prompt: str) -> str:
    """记录文件里读回来的提示词（和运行中插的话）去掉开头那句说明。前后的空白和换行不一定原样留着。"""
    text = prompt.lstrip()
    return text[len(_PREFACE):].lstrip() if text.startswith(_PREFACE) else prompt


def post(socket_path: str, text: str) -> str:
    """发出去，返回记录文件里会出现的那段原文。连不上时抛 ``OSError``。"""
    content = envelope(text)
    message = {"msgV": 1, "msg_id": str(uuid.uuid4()), "type": "user", "message": {"role": "user", "content": content}, "priority": "next"}
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(_CONNECT_TIMEOUT)
        client.connect(socket_path)
        client.sendall((json.dumps(message, ensure_ascii=False) + "\n").encode())
        time.sleep(_LINGER_SECONDS)
    return content

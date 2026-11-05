"""运行中底行写什么：把正在跑的工具换成一句人话。

桌面版界面上的“已运行页面脚本”这类文案不在记录文件里，这里按工具名自己写一份。Bash 和子代理有 Claude 写的说明，直接用。
"""

from __future__ import annotations

from collections.abc import Iterable

from .transcript import Call

__all__ = ["doing", "pick"]

#: 没什么信息量的工具，底行不写
QUIET = frozenset({"update_plan", "wrap_up", "mark_chapter", "ToolSearch", "TodoWrite"})
#: 后面跟文件名或搜索词
_VERBS = {
    "Read": "读取中",
    "Edit": "编辑中",
    "Write": "写入中",
    "NotebookEdit": "编辑中",
    "Grep": "搜索中",
    "Glob": "查找中",
    "WebSearch": "搜索中",
}
_PHRASES = {
    "javascript_tool": "正在运行页面脚本",
    "navigate": "正在打开页面",
    "computer": "正在操作页面",
    "browser_batch": "正在操作页面",
    "read_page": "正在读取页面",
    "get_page_text": "正在读取页面",
    "find": "正在查找页面内容",
    "form_input": "正在填写表单",
    "preview_start": "正在启动预览",
    "preview_stop": "正在停止预览",
    "preview_logs": "正在看预览日志",
    "read_console_messages": "正在看控制台",
    "read_network_requests": "正在看网络请求",
    "resize_window": "正在调整窗口",
    "tabs_context": "正在查看标签页",
    "tabs_context_mcp": "正在查看标签页",
    "tabs_create": "正在打开标签页",
    "tabs_create_mcp": "正在打开标签页",
    "tabs_close": "正在关闭标签页",
    "tabs_close_mcp": "正在关闭标签页",
    "file_upload": "正在上传文件",
    "SendUserFile": "正在发送文件",
    "Artifact": "正在发布页面",
    "Workflow": "正在运行工作流",
    "WebFetch": "正在读取网页",
    "Skill": "正在加载技能",
    "TaskStop": "正在停止任务",
    "AskUserQuestion": "在问你问题",
}


def doing(call: Call) -> str:
    """这个工具在做什么，没什么可写的返回空。"""
    if call.name in QUIET:
        return ""
    if call.described:
        return call.label
    if call.name in _VERBS:
        return f"{_VERBS[call.name]} {call.label}".strip()
    return _PHRASES.get(call.name, call.name)


def pick(calls: Iterable[Call]) -> Call | None:
    """同时跑几个时写哪个：有说明的优先，其次最新开始的。"""
    shown = [call for call in calls if doing(call)]
    return max(shown, key=lambda call: (call.described, call.at), default=None)

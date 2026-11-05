"""把回复排成眼镜上的页。字宽用眼镜原生字体的（``native_font.py``，从 reader 拷来），自己断好行，每页行数才是确定的。"""

from __future__ import annotations

import re
from bisect import bisect_right

from . import native_font

__all__ = ["BODY_WIDTH", "LINES_PER_PAGE", "NARROW_WIDTH", "TEXT_SIZE", "paginate", "wrapped"]

#: 正文字号。原生字号只有固定档位，行距是模拟器里量的
TEXT_SIZE, LINE_PITCH = 18, 26
#: 正文区，和 pages/claude-panel/template.xml 一致。右栏在的时候只有左栏那么宽
BODY_WIDTH, NARROW_WIDTH, BODY_HEIGHT = 594, 386, 206
LINES_PER_PAGE = (BODY_HEIGHT - TEXT_SIZE) // LINE_PITCH + 1
#: 行尾标点最多超出正文区这么多，和 label 右边留的余量一致
HANG = 12
_NO_LINE_START = frozenset("，。、；：？！）》」』】〉…—～·,.;:?!)]}%”’")
_UNIT = re.compile(r"[A-Za-z0-9_./:@#%+=~\\-]+[,;!?)\]]*|.", re.S)
_STARTS = [start for start, _, _ in native_font.REGULAR]


def _width(ch: str, size: int) -> int:
    code = ord(ch)
    i = bisect_right(_STARTS, code) - 1
    start, end, advance = native_font.REGULAR[i] if i >= 0 else (0, 0, 0)
    return round((advance if start <= code <= end else native_font.REGULAR_BLANK) * size / 1000)


def _units(paragraph: str) -> list[str]:
    """断行的最小单位：一个汉字一格，连着的英文、数字、路径算一格，不然 panel.py 会断成 panel. 和 py。"""
    return _UNIT.findall(paragraph)


def _wrap(paragraph: str, width: int, size: int = TEXT_SIZE) -> list[str]:
    """一段断成几行。行首不放标点：放不下时把标点挂在行尾，超出的部分在 label 右边留着余量。比一行还长的英文才拆开。"""
    lines, line, used = [], "", 0
    for unit in _units(paragraph):
        advance = sum(_width(ch, size) for ch in unit)
        hanging = len(unit) == 1 and unit in _NO_LINE_START  # 只有单个的标点才挂在行尾，.env 这样的词要换行
        if hanging and used + advance > width + HANG and line:  # 挂不下：带着前一个字一起下一行，连着几个标点就都带上
            cut = len(line) - 1
            while cut > 0 and line[cut] in _NO_LINE_START:
                cut -= 1
            cut = cut or len(line) - 1
            carried = line[cut:]
            lines.append(line[:cut].rstrip(" "))
            line, used = carried, sum(_width(ch, size) for ch in carried)
        elif not hanging and used + advance > width and line:
            lines.append(line.rstrip(" "))
            line, used = "", 0
        if advance > width:
            for ch in unit:
                step = _width(ch, size)
                if used + step > width and line:
                    lines.append(line)
                    line, used = "", 0
                line += ch
                used += step
            continue
        line += unit
        used += advance
    lines.append(line)
    return lines


def paginate(text: str, width: int = BODY_WIDTH, per_page: int = LINES_PER_PAGE) -> list[str]:
    """至少返回一页。段落之间的空行留着，翻到页首时去掉。"""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        lines.extend(_wrap(paragraph, width) if paragraph.strip() else [""])
    pages: list[str] = []
    current: list[str] = []
    for line in lines:
        if not line and not current:
            continue
        current.append(line)
        if len(current) == per_page:
            pages.append("\n".join(current).rstrip("\n"))
            current = []
    if any(current):
        pages.append("\n".join(current).rstrip("\n"))
    return pages or [""]


def wrapped(text: str, width: int, size: int) -> list[str]:
    """一段话断成几行，给固定大小的一块字用。"""
    return _wrap(" ".join(text.split()), width, size)

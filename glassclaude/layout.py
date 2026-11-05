"""运行中的时间流排成眼镜上的行。

一行一个 ``label``，每行的位置、颜色、字号都写在数据里，段落之间才能留空。页面的绑定有上限，所以最多 :data:`ROWS` 行。
最后一页放最新的内容，放不下的旧行往前翻。翻页要等服务，这里一行比回复页贵，不值得提前发。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .text import NARROW_WIDTH, TEXT_SIZE, _wrap

__all__ = ["ROWS", "Row", "blank_rows", "clock", "live_pages", "row_data", "stream_rows"]

#: 时间流的上下边界和行数，和 pages/claude-panel/template.xml 一致
TOP, BOTTOM, ROWS = 42, 248, 7
#: 用不着的行放到屏幕外
OFFSCREEN = 300
WHITE, OLDER, QUOTE, DIM, LIVE, BLACK = "#FFFFFF", "#B4B4B4", "#A0A0A0", "#808080", "#D0D0D0", "#000000"
_HEIGHT = {"new": 26, "old": 26, "you": 26, "tool": 24, "live": 24}
_COLOR = {"new": WHITE, "old": OLDER, "you": QUOTE, "tool": DIM, "live": LIVE}
_PARAGRAPH, _BLOCK = 10, 8


@dataclass(frozen=True)
class Row:
    kind: str
    text: str
    time: str = ""
    paragraph: bool = False  # 同一段话里新起的一段，上面多留一点


def clock(seconds: float) -> str:
    """``4:33``。一小时以上写 ``1h02``：``1:02:33`` 太宽，会碰到旁边的字。"""
    total = max(int(seconds), 0)
    minutes, rest = divmod(total, 60)
    return f"{minutes // 60}h{minutes % 60:02d}" if minutes >= 60 else f"{minutes}:{rest:02d}"


def stream_rows(steps: list[dict[str, Any]], now: float, width: int = NARROW_WIDTH) -> list[Row]:
    """时间流排成行。最新一段最亮，旧的调暗，工具组更暗，还在跑的工具组亮一档。"""
    last_text = max((i for i, step in enumerate(steps) if step.get("k") == "text"), default=-1)
    rows: list[Row] = []
    for i, step in enumerate(steps):
        kind = step.get("k")
        if kind == "tools":
            running = bool(step.get("o"))
            spent = (now if running else float(step.get("e") or 0)) - float(step.get("s") or 0)
            rows.append(Row("live" if running else "tool", f"已运行 {step.get('n', 0)} 个命令", clock(spent)))
        elif kind == "you":
            rows.extend(Row("you", line) for line in _wrap(" ".join(str(step.get("t", "")).split()), width, TEXT_SIZE))
        else:
            style, spaced = ("new" if i == last_text else "old"), False
            for paragraph in str(step.get("t", "")).split("\n"):
                if not paragraph.strip():
                    spaced = True
                    continue
                for j, line in enumerate(_wrap(paragraph, width, TEXT_SIZE)):
                    rows.append(Row(style, line, paragraph=spaced and j == 0))
                spaced = False
    return rows


def _gap(above: Row, below: Row) -> int:
    if below.paragraph:
        return _PARAGRAPH
    flows = above.kind == below.kind or {above.kind, below.kind} == {"new", "old"} or below.kind in ("tool", "live")
    return 0 if flows else _BLOCK


def live_pages(rows: list[Row]) -> list[list[Row]]:
    """从最新一行往回排成页，返回时旧的页在前。至少一页。"""
    pages: list[list[Row]] = []
    page: list[Row] = []
    used = 0
    for row in reversed(rows):
        add = _HEIGHT[row.kind] + (_gap(row, page[0]) if page else 0)
        if page and (used + add > BOTTOM - TOP or len(page) == ROWS):
            pages.insert(0, page)
            page, used, add = [], 0, _HEIGHT[row.kind]
        page.insert(0, row)
        used += add
    pages.insert(0, page)
    return pages


def row_data(page: list[Row]) -> list[dict[str, Any]]:
    """一页的页面数据，正好 :data:`ROWS` 项。"""
    out = []
    y, above = TOP, None
    for row in page:
        if above is not None:
            y += _gap(above, row)
        small = row.kind in ("tool", "live")
        out.append({"y": y, "r": row.text, "t": row.time, "c": _COLOR[row.kind], "z": 16 if small else TEXT_SIZE, "b": DIM if row.kind == "you" else BLACK})
        y += _HEIGHT[row.kind]
        above = row
    return out + blank_rows(ROWS - len(page))


def blank_rows(count: int = ROWS) -> list[dict[str, Any]]:
    return [{"y": OFFSCREEN, "r": "", "t": "", "c": BLACK, "z": TEXT_SIZE, "b": BLACK} for _ in range(count)]

"""文字离开电脑之前过一遍：去掉密钥，把 Markdown 摊平成眼镜上能看的纯文字。

显示和朗读的内容都经过云端和千问平台，控制台的调试日志还会留下发给页面的数据，所以这一步在 Mac 上做，云端拿到的已经是过滤后的。
过滤靠规则，不保证一个不漏，代码块整段折叠掉是最主要的防线。两头都要顾：漏了密钥不行，把正常的说明、路径、测试名盖掉也不行。

- 有固定形状的（各家的 key、JWT、私钥、长的随机串）用正则。
- 标签加值的写法太多（``**密码：** x``、``password := "x"``、``密码是 x``），不写正则穷举，:func:`_value_span` 从关键词往后扫：
  跳过装饰符号，认一个分隔符，再取值。无引号的值只认 ASCII，中文句子不会被当成值；靠“是”连着的值要更严，``密码是 6 位数字`` 不算。
- 所有规则都不能回溯：Claude 复述的长串没有空白，平方级的规则就能把桥接卡到掉线。
"""

from __future__ import annotations

import re
import string
from pathlib import Path

__all__ = ["MAX_CHARS", "brief", "clean", "fold", "redact"]

#: 一条回复最多同步这么多字，眼镜上差不多 30 页。再长的到电脑上看
MAX_CHARS = 6000
_MASK = "〔已隐藏〕"
_LIST_MARK = r"(?:[-*+]|\d+[.)])[ \t]+"
_OPEN_FENCE = re.compile(rf"^([ \t>]*)({_LIST_MARK})?(`{{3,}}|~{{3,}})(.*)$")
_CLOSE_FENCE = re.compile(r"^[ \t>]*(`{3,}|~{3,})[ \t]*$")
_LIST_ITEM = re.compile(rf"^([ \t]*)({_LIST_MARK})")
_BOUND = r"(?<![A-Za-z0-9_])"
_TOKENS = (
    re.compile(rf"{_BOUND}sk-[A-Za-z0-9_-]{{16,}}"),  # Anthropic、OpenAI
    re.compile(rf"{_BOUND}(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{{16,}}"),
    re.compile(rf"{_BOUND}(?:AKIA|ASIA)[A-Z0-9]{{16}}(?![A-Z0-9])"),
    re.compile(rf"{_BOUND}xox[abprs]-[A-Za-z0-9-]{{10,}}"),
    re.compile(rf"{_BOUND}eyJ[A-Za-z0-9_-]{{10,}}\.[A-Za-z0-9_-]{{10,}}\.[A-Za-z0-9_-]{{5,}}"),  # JWT
    re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32,}(?![0-9a-fA-F])"),
)
_AUTH_LINE = re.compile(r"(?i)(authorization[\"'`]{0,3}[ \t]*[:=][ \t]*[\"'`]?)((?:[A-Za-z]{1,16}[ \t]+)?[!-~]{4,})")
_AUTH_WORD = re.compile(rf"(?i){_BOUND}(bearer|basic)[ \t]+([A-Za-z0-9._~+/-]{{8,}}={{0,2}})")
_PRIVATE_BEGIN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY[A-Z ]*-----")
_PRIVATE_END = re.compile(r"-----END [A-Z ]*PRIVATE KEY[A-Z ]*-----")
_BASE64_LINES = re.compile(r"(?:[ \t]*\n[ \t]*[A-Za-z0-9+/=]{16,})*")
#: 一长串看着随机的字符。要整串取出来再判断，写成带前瞻的正则是平方级
_LONG_RUN = re.compile(r"(?<![A-Za-z0-9+/_-])[A-Za-z0-9+/_-]{32,}={0,2}")
_PART = re.compile(r"[/_-]+")
#: 路径、链接、标识符由单词组成，camelCase 也算，数字粘在前一节上：平均每节至少这么长
_HUMP = re.compile(r"[A-Z]?[a-z]+[0-9]*|[A-Z]+[0-9]*(?![a-z])|[0-9]+")
_HUMP_CHARS = 3.5
_KEY_EN = re.compile(r"(?i)(?:passw(?:or)?d|passphrase|client[ _-]?secret|secret(?:[ _-]?key)?|token(?!s)|api[ _-]?key|access[ _-]?key|private[ _-]?key"
                     r"|encryption[_-]?key|signing[_-]?key|auth[_-]?key|credentials?)")
_KEY_SHORT = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*_(?:PASS|pass|PWD|pwd)(?![A-Za-z0-9])|PGPASS|(?<![A-Za-z0-9_/.-])pwd(?![A-Za-z0-9])")
_KEY_ZH = re.compile(r"密码|口令|密钥|秘钥|令牌")
_KEY_ENV = re.compile(rf"{_BOUND}[A-Z][A-Z0-9_]{{0,48}}_KEY[ \t]*=[ \t]*\S+")
_SUFFIX = re.compile(r"[A-Za-z0-9_.-]{0,24}")
#: 键的后缀是这些的话，值不是密钥本身
_SKIP_SUFFIX = re.compile(r"(?i)count|limit|type|len|size|name|path|file|ttl|expir|url|uri|hash|min|max|policy|field|header")
_FILE_NAME = re.compile(r"\.(?:py|js|ts|tsx|jsx|md|json|ya?ml|toml|txt|sh|go|rs|java|html|css)$", re.I)
_SEPARATOR = re.compile(r":=|=>|===|==|=|:|：|＝|也是|改成|改为|设为|设成|是|为|(?i:is|are)\b")
_WORD_SEPARATORS = ("也是", "改成", "改为", "设为", "设成", "是", "为")
_QUOTES = {'"': '"', "'": "'", "`": "`", "“": "”", "‘": "’", "「": "」", "『": "』"}
_ASCII_RUN = re.compile(r"[!-~]{1,200}")
#: 这些“值”是类型注解和字面量
_NOT_VALUES = frozenset(("str", "bytes", "int", "bool", "string", "number", "boolean", "none", "null", "true", "false", "undefined", "optional[str]", "any", "object"))
_TYPE_NAME = re.compile(r"^(?:[A-Z][a-z]+)+$|^&|<")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_CURL_USER = re.compile(rf"{_BOUND}-u[ \t]+[^\s:]{{1,64}}:(\S{{1,200}})")
_DB_PASSWORD = re.compile(r"(?<![A-Za-z0-9_-])(?:mysql|mysqldump|mysqladmin|sshpass)(?![A-Za-z0-9_-])[^\n|;]{0,120}?[ \t]-p[ \t]?(\S{1,200})")
_COOKIE = re.compile(r"(?i)(?<![A-Za-z0-9_])((?:set-)?cookie[\"'`]{0,3}[ \t]*[:=][ \t]*[\"'`]?)([^\s\"'`=]{1,64}=[^\s\"'`]{0,300}(?:[ \t]+[^\s\"'`=]{1,64}=[^\s\"'`]{0,300})*)")
_URL_PASSWORD = re.compile(r"(?i)(?<![a-z0-9+.-])([a-z][a-z0-9+.-]{0,20}://[^\s:/@]{0,64}):([!-~]{1,200})@")
_PORT = re.compile(r"\d{1,5}(?:/|$)")
_CELL_KEY = re.compile(r"(?i)(?:passw(?:or)?d|passphrase|secret|token|api[ _-]?key|access[ _-]?key|credentials?|密码|口令|密钥|秘钥|令牌)$")
_INLINE = re.compile(r"(`[^`\n]+`)")
_LINK = re.compile(r"!?\[([^\]\n]{0,200})\]\([^)\s]{0,500}(?:\s+\"[^\"\n]{0,200}\")?\)")
_HEADING = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.M)
_BULLET = re.compile(r"^([ \t]*)[-*+][ \t]+", re.M)
#: 词中间的 ** 和 __ 不是强调：2**10、__init__.py。边界只看 ASCII，汉字紧贴着也算强调
_EMPHASIS = re.compile(r"(?<![A-Za-z0-9_*])(\*\*|__)(?=\S)([^\n]{1,200}?)(?<=\S)\1(?![A-Za-z0-9_*])")
_BLANKS = re.compile(r"\n{3,}")
_RULE_CHARS = frozenset("-*_ \t")
_TABLE_RULE_CHARS = frozenset("|:- \t")
_ASCII_CHARS = string.ascii_letters + string.digits + string.punctuation


def fold(text: str) -> str:
    """代码块折叠成一行。围栏要成对：结束行的字符相同、不比开头短，这样四个反引号里能套三个，没结束的吃到文件尾。
    缩进四格的代码块也折叠。列表里的续行不算：要比列表项的内容再多缩进四格才是代码。"""
    out: list[str] = []
    fence, prefix, count, indented = "", "", 0, 0
    previous_blank, list_indent = True, -1

    def flush() -> None:
        nonlocal indented
        if indented:
            out.append(f"〔代码 {indented} 行〕")
            indented = 0

    for line in text.split("\n"):
        if fence:
            close = _CLOSE_FENCE.match(line)
            if close and close.group(1)[0] == fence[0] and len(close.group(1)) >= len(fence):
                out.append(f"{prefix}〔代码 {count} 行〕")
                fence = ""
            else:
                count += 1
            continue
        opening = _OPEN_FENCE.match(line)
        if opening and "`" not in opening.group(4):  # 同一行里又有反引号的是行内代码
            flush()
            fence, count = opening.group(3), 0
            prefix = f"{opening.group(1)}{opening.group(2) or ''}"
            continue
        blank = not line.strip()
        width = len(line) - len(line.lstrip(" \t"))
        item = _LIST_ITEM.match(line)
        code_from = 4 if list_indent < 0 else list_indent + 4
        if not blank and width >= code_from and (indented or previous_blank):
            indented += 1
            continue
        flush()
        out.append(line)
        previous_blank = blank
        if item:
            list_indent = len(item.group(0))
        elif not blank and width == 0:
            list_indent = -1
    flush()
    if fence:
        out.append(f"{prefix}〔代码 {count} 行〕")
    return "\n".join(out)


def _private_keys(text: str) -> str:
    """有结尾的整段盖掉，带 Proc-Type 头的加密私钥也包括。只提到开头一行时，只盖紧跟着的 base64，后面的说明不能丢。"""
    out, position = [], 0
    while begin := _PRIVATE_BEGIN.search(text, position):
        end = _PRIVATE_END.search(text, begin.end())
        stop = end.end() if end else _BASE64_LINES.match(text, begin.end()).end()  # type: ignore[union-attr]
        out += [text[position:begin.start()], _MASK]
        position = stop
    return "".join([*out, text[position:]])


def _looks_random(run: str) -> bool:
    """大小写和数字都有，而且读起来不像单词。"""
    if not (any(c.isdigit() for c in run) and any(c.isupper() for c in run) and any(c.islower() for c in run)):
        return False
    humps = [hump for part in _PART.split(run.rstrip("=")) if part for hump in _HUMP.findall(part)]
    return not humps or sum(map(len, humps)) / len(humps) < _HUMP_CHARS


def _auth_word(match: re.Match[str]) -> str:
    """``Bearer abc``。普通英文里也有这两个词，所以值要像凭据：带数字、等号，或者大小写混着。"""
    value = match.group(2)
    if any(c.isdigit() for c in value) or "=" in value or (any(c.isupper() for c in value[1:]) and any(c.islower() for c in value)):
        return f"{match.group(1)} {_MASK}"
    return match.group(0)


def _skip(text: str, position: int, chars: str, limit: int = 12) -> int:
    end = min(len(text), position + limit)
    while position < end and text[position] in chars:
        position += 1
    return position


def _not_a_value(value: str) -> bool:
    return value.lower() in _NOT_VALUES or _TYPE_NAME.search(value) is not None or value[0] in "{[(<|"


def _value_span(text: str, match: re.Match[str], *, chinese: bool) -> tuple[int, int] | None:
    """这个关键词后面的值在哪。没有值就返回 None。"""
    end, start = match.end(), match.start()
    long_option = text[max(0, start - 2):start] == "--"
    if not chinese:
        suffix = _SUFFIX.match(text, end).group(0)  # type: ignore[union-attr]
        if suffix[:1].isalpha() or _SKIP_SUFFIX.search(suffix) or _FILE_NAME.search(suffix) or text[start - 1:start] == "/":
            return None  # tokenizer、token_count、token_store.py、POST /oauth/token
        end += len(suffix)
    position = _skip(text, end, "\"'`*]) \t")
    separator = _SEPARATOR.match(text, position)
    word = separator.group(0) if separator else ""
    if word:
        position = separator.end()  # type: ignore[union-attr]
    elif not chinese and not long_option:
        return None
    return _take_value(text, position, word)


def _take_value(text: str, position: int, word: str) -> tuple[int, int] | None:
    """从分隔符后面取值。``word`` 是分隔符。"""
    position = _skip(text, position, " \t*")
    if word in _WORD_SEPARATORS and text[position:position + 1] in (":", "："):  # 密码是：x
        position = _skip(text, position + 1, " \t*")
    # 要求严的三种：靠“是”连着，靠英文 is 连着，或者根本没有分隔符
    strict = word in _WORD_SEPARATORS or word.lower() in ("is", "are") or not word
    if position >= len(text):
        return None
    closer = _QUOTES.get(text[position])
    if closer:
        close = text.find(closer, position + 1, position + 202)
        if close <= position + 1 or "\n" in text[position:close] or _not_a_value(text[position + 1:close]):
            return None
        return position + 1, close
    run = _ASCII_RUN.match(text, position)
    value = run.group(0).rstrip(")]}>,;.'\"`*") if run else ""
    if not value:
        return None
    stop = position + len(value)
    if _not_a_value(value):
        after = _skip(text, stop, ",) \t")  # password: str = "x"
        return _take_value(text, after + 1, "=") if text[after:after + 1] == "=" else None
    if strict and not _strict_value(value, text, stop, loose=word in _WORD_SEPARATORS):
        return None
    return position, stop


def _strict_value(value: str, text: str, stop: int, *, loose: bool) -> bool:
    """``密码是 hunter2`` 算，``令牌是 JWT 格式``、``密码是 6 位数字`` 不算，英文 ``token is valid`` 也不算。"""
    following = text[_skip(text, stop, " \t"):stop + 8]
    if _CJK.match(following):
        return False
    mixed = any(c.isdigit() for c in value) and any(c.isalpha() for c in value)
    return mixed or (loose and len(value) >= 4 and not value.isdigit())


def _keyed(text: str) -> str:
    spans: list[tuple[int, int]] = []
    for pattern, chinese in ((_KEY_EN, False), (_KEY_SHORT, False), (_KEY_ZH, True)):
        for match in pattern.finditer(text):
            if span := _value_span(text, match, chinese=chinese):
                spans.append(span)
    out, position = [], 0
    for start, stop in sorted(spans):
        if start >= position:
            out += [text[position:start], _MASK]
            position = stop
    return "".join([*out, text[position:]])


def _url_password(match: re.Match[str]) -> str:
    return match.group(0) if _PORT.match(match.group(2)) else f"{match.group(1)}:{_MASK}@"


def _cells(line: str) -> list[str]:
    """按竖线切格子，行内代码和 ``\\|`` 里的竖线不算。"""
    cells, current, code, previous = [], [], False, ""
    for ch in line:
        if ch == "`":
            code = not code
        if ch == "|" and not code and previous != "\\":
            cells.append("".join(current))
            current = []
        else:
            current.append(ch)
        previous = ch
    cells.append("".join(current))
    return cells


def _is_rule(line: str) -> bool:
    stripped = line.strip()
    return "|" in stripped and "-" in stripped and set(stripped) <= _TABLE_RULE_CHARS


def _cell_key(cell: str) -> bool:
    return _CELL_KEY.search(cell.strip().strip("*`：: ")) is not None


def _maskable(cell: str) -> bool:
    value = cell.strip().strip("`*\"'")
    return bool(value) and value.isascii() and not _not_a_value(value) and not value.isdigit()


def _tables(text: str) -> str:
    """表格里键和值分在两格，或者键在列头，扫描器看不到。说明列是中文，不盖。"""
    if "|" not in text:
        return text
    lines = text.split("\n")
    headers: set[int] = {i - 1 for i, line in enumerate(lines) if i and _is_rule(line)}
    columns: list[int] = []
    for i, line in enumerate(lines):
        if "|" not in line:
            columns = []
            continue
        if _is_rule(line):
            continue
        cells = _cells(line)
        if i in headers:
            columns = [k for k, cell in enumerate(cells) if _cell_key(cell)]
            continue
        changed = False
        for k in range(len(cells)):
            by_column = k in columns
            by_label = k > 0 and _cell_key(cells[k - 1])
            if (by_column or by_label) and _maskable(cells[k]):
                cells[k] = f" {_MASK} "
                changed = True
        if changed:
            lines[i] = "|".join(cells)
    return "\n".join(lines)


def _home(text: str) -> str:
    """只换这台电脑自己的家目录。``/home/dashboard`` 是网页路由，别的机器的目录也不是这里的隐私。"""
    home = str(Path.home())
    if home not in text:
        return text
    return re.sub(rf"(?:(?<![A-Za-z0-9._:/-])|(?<=file://)){re.escape(home)}(?![A-Za-z0-9._-])", "~", text)


def redact(text: str) -> str:
    """把看着像密钥的东西盖掉。"""
    text = _private_keys(text)
    for pattern in _TOKENS:
        text = pattern.sub(_MASK, text)
    text = _LONG_RUN.sub(lambda m: _MASK if _looks_random(m.group(0)) else m.group(0), text)
    text = _URL_PASSWORD.sub(_url_password, text)
    text = _AUTH_LINE.sub(rf"\1{_MASK}", text)
    text = _AUTH_WORD.sub(_auth_word, text)
    text = _COOKIE.sub(rf"\1{_MASK}", text)
    text = _KEY_ENV.sub(lambda m: f"{m.group(0).split('=', 1)[0]}={_MASK}", text)
    for pattern in (_CURL_USER, _DB_PASSWORD):
        text = pattern.sub(lambda m: m.group(0).replace(m.group(1), _MASK), text)
    text = _keyed(text)
    return _home(_tables(text))


def _rule_line(line: str) -> bool:
    stripped = line.strip()
    return len(stripped) >= 3 and set(stripped) <= _RULE_CHARS and len(set(stripped) - {" ", "\t"}) == 1


def _table_rows(lines: list[str]) -> list[str]:
    """分隔行前后的才算表格，``|x| + |y|`` 不是。"""
    table: set[int] = set()
    for i, line in enumerate(lines):
        if _is_rule(line):
            table.add(i - 1)
            j = i + 1
            while j < len(lines) and "|" in lines[j]:
                table.add(j)
                j += 1
    out = []
    for i, line in enumerate(lines):
        if _is_rule(line):
            continue
        if i in table:
            cells = [cell.strip().replace("\\|", "|") for cell in _cells(line.strip())]
            line = "  ".join(cells[1:-1] if len(cells) > 2 and not cells[0] and not cells[-1] else cells)
        out.append(line)
    return out


def _emphasis(match: re.Match[str]) -> str:
    inner = match.group(2)
    if match.group(1) == "__" and inner.isascii() and inner.replace("_", "").isalnum():  # __init__
        return match.group(0)
    return inner


def _plain(text: str) -> str:
    """行内代码以外的部分去掉链接和强调。代码里的 ``2**10``、``__init__`` 不能动。"""
    parts = _INLINE.split(text)
    for i, part in enumerate(parts):
        parts[i] = part[1:-1] if i % 2 else _EMPHASIS.sub(_emphasis, _LINK.sub(r"\1", part))
    return "".join(parts)


def _flatten(text: str) -> str:
    lines = _table_rows([line.rstrip(" \t") for line in text.split("\n") if not _rule_line(line)])
    return _plain(_BULLET.sub(r"\1· ", _HEADING.sub("", "\n".join(lines))))


def clean(text: str, limit: int = MAX_CHARS) -> str:
    """代码块先折叠，别的规则就碰不到里面的东西了。过滤之前先截一刀，太长的输入不用全算，切点退到完整的词上，
    半截密钥规则认不出来；过滤之后才截到真正的长度。截过就一定写明，不然用户以为这就是全文。"""
    folded = fold(text.replace("\r\n", "\n"))
    kept = folded[: limit * 2]
    if len(kept) < len(folded):
        kept = kept.rstrip(_ASCII_CHARS)
    flat = _BLANKS.sub("\n\n", _flatten(redact(kept))).strip()
    rest = len(folded) - len(kept) + max(len(flat) - limit, 0)
    return f"{flat[:limit]}\n……后面还有 {rest} 字，到电脑上看" if rest > 0 else flat


def brief(text: str, limit: int) -> str:
    """名字、工具说明这类短字段：文件名和搜索模式不是 Markdown，不摊平，一行放不下就切掉，不加“到电脑上看”。"""
    text = " ".join(redact(text[: limit * 4]).split())
    return text if len(text) <= limit else f"{text[:limit - 1]}…"

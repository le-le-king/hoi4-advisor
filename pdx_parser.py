"""Paradox 文本存档解析器（HOI4 / EU4 / CK3 / Stellaris 通用）。

Clausewitz 存档的文本格式很简单:

    gamestate={
        date=1940.8.19
        countries={
            GER={
                political_power=1250.000
                num_of_civilian_factories=132
            }
        }
        units={
            division={
                { name="12. Panzerdivision" location=12406 }
                { name="4. Infanterie" location=12410 }
            }
        }
    }

只有三样东西: 裸词、引号字符串、大括号块。键值对是 `键 = 值`。
同一个键可以出现多次（存档里到处都是），所以用 Multi 标记这种重复。

设计取舍:
- 不做流式解析，整份读进内存。HOI4 存档几十 MB，现代机器扛得住。
- 数字/日期由 _scalar 猜类型，日期 `1940.8.19` 有两个点，float() 会失败，
  自然落回字符串，不需要特判。
- 解析结果是嵌套 dict，重复键的值是 Multi（list 子类），下游用 as_list() 统一。
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any, Iterator

__all__ = ["parse", "load_save", "as_list", "Multi", "PdxParseError"]


class PdxParseError(Exception):
    """存档格式不对时抛出。"""


class Multi(list):
    """同一个键在一个块里出现了多次。

    继承 list 是为了直接当序列用；用独立类型是为了和"值本身就是列表"
    的情况区分开（比如 __anon 里的匿名块）。
    """


# ---------------------------------------------------------------------------
# 词法
# ---------------------------------------------------------------------------

# 顺序很重要: 注释要排在裸词前面，否则 '#' 会被当成普通字符吃掉整行。
_TOKEN_RE = re.compile(
    r'#[^\n]*'                  # 注释到行尾
    r'|"(?:[^"\\]|\\.)*"'       # 引号字符串（支持 \" 转义）
    r'|<=|>=|==|!='             # 双字符运算符
    r'|[=<>]'                   # 单字符运算符
    r'|[{}]'                    # 大括号
    r'|[^\s{}=<>!#"]+'          # 裸词（数字、日期、tag、yes/no）
)

_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    '"': '"',
    "\\": "\\",
}


def _unescape(raw: str) -> str:
    """处理引号串里的反斜杠转义。"""
    if "\\" not in raw:
        return raw
    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "\\" and i + 1 < n:
            nxt = raw[i + 1]
            out.append(_ESCAPES.get(nxt, nxt))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _tokenize(text: str) -> Iterator[tuple[str, str]]:
    """产出 (kind, value)。kind 是 str / op / { / } / eof。"""
    for match in _TOKEN_RE.finditer(text):
        tok = match.group()
        head = tok[0]
        if head == "#":
            continue
        if head == '"':
            yield ("str", _unescape(tok[1:-1]))
        elif head in "{}":
            yield (head, head)
        elif len(tok) <= 2 and tok in ("=", "<", ">", "<=", ">=", "==", "!="):
            yield ("op", tok)
        else:
            yield ("str", tok)
    yield ("eof", "")


# ---------------------------------------------------------------------------
# 语法
# ---------------------------------------------------------------------------

def _scalar(raw: str) -> Any:
    """把裸词转成合适的 Python 类型。"""
    if raw == "yes":
        return True
    if raw == "no":
        return False
    if raw and (raw[0].isdigit() or raw[0] in "+-"):
        try:
            return int(raw)
        except ValueError:
            pass
        try:
            return float(raw)
        except ValueError:
            # 日期 1940.8.19 走这里，保留原字符串
            pass
    return raw


def _put(block: dict, key: str, value: Any) -> None:
    """往块里塞一个键值，重复键合并成 Multi。"""
    if key not in block:
        block[key] = value
        return
    existing = block[key]
    if isinstance(existing, Multi):
        existing.append(value)
    else:
        block[key] = Multi([existing, value])


class _Stream:
    """带一个字符预读的 token 流。"""

    __slots__ = ("_it", "_buf")

    def __init__(self, tokens: Iterator[tuple[str, str]]) -> None:
        self._it = tokens
        self._buf: tuple[str, str] | None = None

    def peek(self) -> tuple[str, str]:
        if self._buf is None:
            self._buf = next(self._it)
        return self._buf

    def take(self) -> tuple[str, str]:
        tok = self.peek()
        self._buf = None
        return tok


def _flatten(block: dict) -> Any:
    """把只有匿名块 / 只有裸词的块压平成列表。

    存档里两种"列表"写法:

        division={ { name="A" } { name="B" } }   # 匿名块列表
        allies={ ITA HUN ROM }                    # 裸词列表

    不压平的话下游要写 obj['division']['__anon'] 这种别扭路径，
    所以这里直接还原成列表。
    """
    keys = list(block.keys())
    if keys == ["__anon"]:
        return block["__anon"]
    if keys == ["__bare"]:
        return block["__bare"]
    if keys and all(k in ("__anon", "__bare") for k in keys):
        # 实测 1.14.8 的档里有 18 个块同时混着匿名块和裸词
        # （旧版这里直接 return block，下游当 dict 用，__anon 的内容就悄悄丢了）。
        # token 的先后顺序在这里已经没保存，能合回一个列表总比丢掉强。
        out: list = []
        for key in ("__anon", "__bare"):
            value = block[key]
            out.extend(value if isinstance(value, list) else [value])
        return Multi(out)
    return block


def _parse_block(stream: _Stream, depth: int = 0) -> Any:
    """解析一个块，调用时 '{' 已被消费，直到匹配的 '}' 返回。"""
    if depth > 64:
        raise PdxParseError("嵌套超过 64 层，文件可能损坏")

    block: dict[str, Any] = {}
    while True:
        kind, value = stream.peek()

        if kind == "}":
            stream.take()
            return _flatten(block)
        if kind == "eof":
            # 存档被截断（游戏写盘时被杀），能读出多少算多少
            return _flatten(block)

        if kind == "{":
            # 匿名块，比如 units 下面一堆 { ... }
            stream.take()
            _put(block, "__anon", _parse_block(stream, depth + 1))
            continue

        stream.take()
        if kind != "str":
            continue

        key = value
        nkind, _ = stream.peek()

        if nkind == "op":
            stream.take()
            vkind, vval = stream.take()
            if vkind == "{":
                _put(block, key, _parse_block(stream, depth + 1))
            elif vkind in ("eof", "op"):
                _put(block, key, None)
            else:
                _put(block, key, _scalar(vval))
        elif nkind == "{":
            stream.take()
            _put(block, key, _parse_block(stream, depth + 1))
        else:
            # 光秃秃的词，是裸词列表的一项（allies={ ITA HUN } 那种）
            _put(block, "__bare", _scalar(key))


def parse(text: str) -> dict:
    """解析整份存档文本，返回顶层 dict。"""
    text = text.lstrip("\ufeff")
    if text.startswith("HOI4txt"):
        text = text[7:]
    elif text.startswith("HOI4bin"):
        raise PdxParseError(
            "这是二进制存档，不能用文本解析器。请走 load_save()（会自动转 "
            "pdx_binary.parse_binary）。"
        )
    stream = _Stream(_tokenize(text))
    result: dict[str, Any] = {}

    while True:
        kind, value = stream.peek()
        if kind == "eof":
            break
        if kind == "{":
            stream.take()
            block = _parse_block(stream)
            _put(result, "__anon", block)
            continue
        stream.take()
        if kind != "str":
            continue
        nkind, _ = stream.peek()
        if nkind == "op":
            stream.take()
            vkind, vval = stream.take()
            if vkind == "{":
                _put(result, value, _parse_block(stream))
            elif vkind in ("eof", "op"):
                _put(result, value, None)
            else:
                _put(result, value, _scalar(vval))
        else:
            _put(result, value, True)

    return result


# ---------------------------------------------------------------------------
# 读文件
# ---------------------------------------------------------------------------

def _decode(data: bytes) -> str:
    """存档编码兜底: UTF-8 优先，失败退 GBK，最后 latin-1 保命。"""
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def load_save(path: str | Path) -> dict:
    """读存档文件并解析。

    支持三种形态:
    - zip 包裹的文本存档（PK 头）
    - 裸文本存档（HOI4txt 或直接 gamestate）
    - 二进制存档（HOI4bin，转给 pdx_binary，输出树同构）
    """
    path = Path(path)
    with open(path, "rb") as fh:
        head = fh.read(4)
        fh.seek(0)
        raw = fh.read()

    if head[:2] == b"PK":
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            target = next((n for n in names if "gamestate" in n.lower()), None)
            if target is None:
                target = next((n for n in names if n.lower().endswith(".txt")), None)
            if target is None:
                raise PdxParseError(f"zip 里没找到 gamestate，只有: {names}")
            raw = archive.read(target)

    if raw[:8] == b"HOI4bin5" or raw[:7] == b"HOI4bin":
        # 0.9.12: 二进制存档直接解析（HOI4bin 磁带格式，见 pdx_binary）。
        # b"HOI4bin5" 只是魔数后紧跟 player token(0x2a35) 低字节的巧合，
        # 两个判断命中的是同一批文件，保留纯为可读性。
        from .pdx_binary import parse_binary
        return parse_binary(raw)

    return parse(_decode(raw))


# ---------------------------------------------------------------------------
# 取值助手
# ---------------------------------------------------------------------------

def as_list(value: Any) -> list:
    """把值统一成列表。

    Paradox 里"某键出现 N 次"在读的时候永远当列表处理最省事，
    所以 Multi、普通 list 都直接展开，标量包成单元素列表。
    """
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def get_int(block: dict, key: str, default: int = 0) -> int:
    """取整数字段，容忍 float 和字符串。"""
    raw = block.get(key, default)
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, (int, float)):
        return int(raw)
    try:
        return int(float(str(raw)))
    except (TypeError, ValueError):
        return default


def get_float(block: dict, key: str, default: float = 0.0) -> float:
    """取浮点字段。"""
    raw = block.get(key, default)
    if isinstance(raw, bool):
        return float(raw)
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return float(str(raw))
    except (TypeError, ValueError):
        return default


def get_str(block: dict, key: str, default: str = "") -> str:
    """取字符串字段。"""
    raw = block.get(key, default)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return "yes" if raw else "no"
    return str(raw)

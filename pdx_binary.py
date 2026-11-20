# -*- coding: utf-8 -*-
"""HOI4 二进制存档解析器（HOI4bin）。

格式（对 1.16.9 "Countenance" 的 51MB 实档全量验证：371 万条记录、
150 万容器、最深 30 层，精确走到文件末字节，零漂移）：

    "HOI4bin" | 磁带

容器 entry（对象/数组通用，直到 0x0004 闭合）：
    KEY | 0x0001('=') | TYPE | VALUE     命名键值对
    VALUE（后面不跟 '='）                裸数组元素
    0x0003 ... 0x0004                    匿名嵌套容器（__anon）

KEY 可以是：u16 token（字段名编译成的数字）、字符串（国家 tag 等）、
标量（states={ 1={...} } 这种整数键）。

TYPE 表（与 jomini 0.37 lexer.rs LexemeId 常量一致）：
    0x0001 '='   0x0003 对象开   0x0004 对象闭
    0x000c i32(4B)      0x0014 u32(4B)      0x029c u64(8B)
    0x000d f32(4B)      0x0167 f64(8B)      0x000e bool(1B)
    0x0317 定点i32/1000(4B)  0x0318 定点u16/1000(2B)
    0x000f 引号字符串    0x0017 裸词字符串    （u16 长度 + 字节，可能带 UTF-8 BOM）
    其余 u16 = 以 token 存储的枚举值（bareword，0 字节载荷）

Paradox 还会把 3 位小数定点数的原始整数直接塞进 f32/f64 标签
（stability=1000 表示 1.000）：正小值的位模式是 denormal float、
负值是 NaN 形态，据此识别并按 i32/i64 ÷1000 解码。

文件尾可以省略全部外层闭合符：读到 EOF 即隐式闭合（实测如此）。

date 字段是 i32「总小时数」（无闰年历，2 月恒 28 天，纪元年 -5000）：
    hour = raw % 24 + 1;  year, doy0 = divmod(raw // 24, 365); year -= 5000

输出树与 pdx_parser.parse 完全同构（同名键、Multi、__anon/__bare、
yes/no→bool、日期→"Y.M.D.H" 字符串），save_state 无需区分来源。

tokens=None 时输出「原始模式」：token 键保持 int，枚举值包成 BareWord，
供对齐工具（_p104_align）生成 token 表用。

注意：token→名字映射是**按游戏版本**的。随包 token 表来自 1.16.9
文本/二进制存档对齐，其他版本核心字段大多兼容，但新字段会缺失。
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

try:  # 包内正常路径
    from .pdx_parser import Multi, PdxParseError, _flatten, _put
except ImportError:  # 探针直接按文件加载时
    from pdx_parser import Multi, PdxParseError, _flatten, _put

__all__ = [
    "parse_binary", "load_binary", "decode_date", "encode_date",
    "is_binary_save", "head_fields", "BareWord", "MAGIC",
]

MAGIC = b"HOI4bin"

_EQUAL = 0x0001
_OPEN = 0x0003
_END = 0x0004
_I32 = 0x000C
_F32 = 0x000D
_BOOL = 0x000E
_QUOTED = 0x000F
_U32 = 0x0014
_UNQUOTED = 0x0017
_F64 = 0x0167
_U64 = 0x029C
# jomini 0.37 LexemeId：3 位小数定点数（HOI4 用它存 manpower 等字段）
_FIXED3_I32 = 0x0317
_FIXED3_U16 = 0x0318

_SCALARS = frozenset({_I32, _F32, _BOOL, _U32, _F64, _U64,
                      _FIXED3_I32, _FIXED3_U16})
_STRS = frozenset({_QUOTED, _UNQUOTED})

_unpack = struct.unpack_from
_MAX_DEPTH = 100

# gamestate 顶层 date 字段；完整日期 token 集合见 hoi4_tokens.DATE_TOKENS
_DEFAULT_DATE_TOKENS = frozenset({0x284A})

_CUM_DAYS = (31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334, 365)


class BareWord(int):
    """原始模式下标记「以 token 存储的枚举值」，与普通整数区分。"""

    __slots__ = ()


def is_binary_save(raw: bytes) -> bool:
    """头 7 字节是不是 HOI4bin。"""
    return raw[:7] == MAGIC


def decode_date(raw: int) -> str:
    """i32 总小时数 -> 'Y.M.D.H'（与文本存档的 date 字符串同格式）。"""
    day, hour0 = divmod(raw, 24)
    year, doy0 = divmod(day, 365)
    year -= 5000
    if not (1 <= year <= 2200) or hour0 >= 24:
        raise ValueError(f"date 原始值超出合理范围: {raw}")
    prev = 0
    month = 12
    day_of_month = doy0 - 334 + 1
    for i, cum in enumerate(_CUM_DAYS):
        if doy0 < cum:
            month = i + 1
            day_of_month = doy0 - prev + 1
            break
        prev = cum
    return f"{year}.{month}.{day_of_month}.{hour0 + 1}"


def encode_date(text: str) -> int:
    """'Y.M.D[.H]' -> i32 总小时数（decode_date 的逆，校验用）。"""
    parts = [int(x) for x in str(text).split(".")]
    if len(parts) == 3:
        parts.append(1)  # 无小时按 1 点（游戏内部 hour 从 1 起）
    year, month, day, hour = parts[0], parts[1], parts[2], parts[3]
    doy0 = (_CUM_DAYS[month - 2] if month > 1 else 0) + day - 1
    return ((year + 5000) * 365 + doy0) * 24 + (hour - 1)


def _token_names():
    """懒加载随包 token 表；没有就退回原始模式可用的最小集。"""
    try:
        try:
            from . import hoi4_tokens
        except ImportError:
            import hoi4_tokens
        return hoi4_tokens.TOKENS, frozenset(hoi4_tokens.DATE_TOKENS)
    except ImportError:
        return None, _DEFAULT_DATE_TOKENS


def parse_binary(raw: bytes, tokens: dict | None = "...",
                 date_tokens: frozenset | None = None) -> dict:
    """解析 HOI4bin 字节流。

    tokens="..."（默认）用随包 token 表；tokens=None 输出原始模式
    （int 键 + BareWord 值），给对齐工具用。
    """
    if not is_binary_save(raw):
        raise PdxParseError("不是 HOI4bin 二进制存档")
    if tokens == "...":
        tokens, default_dates = _token_names()
        if date_tokens is None:
            date_tokens = default_dates
    if date_tokens is None:
        date_tokens = _DEFAULT_DATE_TOKENS

    data = raw
    n = len(data)
    named = tokens is not None
    toks = tokens or {}

    def read_scalar(typ: int, p: int):
        if typ == _I32:
            return _unpack("<i", data, p)[0], p + 4
        if typ == _U32:
            return _unpack("<I", data, p)[0], p + 4
        if typ == _BOOL:
            return bool(data[p]), p + 1
        if typ == _F32:
            return _f32_or_fixed(p), p + 4
        if typ == _U64:
            return _unpack("<Q", data, p)[0], p + 8
        if typ == _FIXED3_I32:
            # jomini 语义是 raw/1000，但 HOI4 文本写手对定点字段输出原始
            # 整数（manpower.ratio 文本=331800、二进制 raw=331800），为与
            # 文本档同构这里返回原始值，不除 1000。
            return _unpack("<i", data, p)[0], p + 4
        if typ == _FIXED3_U16:
            return _unpack("<H", data, p)[0], p + 2
        # _F64
        return _f64_or_fixed(p), p + 8

    def _f32_or_fixed(p: int) -> float:
        """f32 槽解码；Paradox 会把定点数原始整数塞进浮点标签。

        正常游戏浮点都落在 [1e-30, 1e30]；槽里出现非规格化浮点/NaN/
        天文数字时，其实是 3 位小数定点的原始 i32（正小数→denormal，
        负数→NaN 形态）。已在 1.14.8/1.16.9 实档 + 文本档同字段核对。
        """
        v = _unpack("<f", data, p)[0]
        if v == 0.0 or (1e-30 <= abs(v) <= 1e30 and v == v):
            return round(v, 6)
        return round(_unpack("<i", data, p)[0] / 1000.0, 6)

    def _f64_or_fixed(p: int) -> float:
        v = _unpack("<d", data, p)[0]
        if v == 0.0 or (1e-30 <= abs(v) <= 1e30 and v == v):
            return v
        return round(_unpack("<q", data, p)[0] / 1000.0, 6)

    def read_str(p: int):
        ln = _unpack("<H", data, p)[0]
        s = data[p + 2:p + 2 + ln].decode("utf-8", "replace")
        if s[:1] == "\ufeff":
            s = s[1:]
        return s, p + 2 + ln

    def bare_value(tok: int):
        if named:
            name = toks.get(tok)
            return name if name is not None else BareWord(tok)
        return BareWord(tok)

    def parse_value(p: int, depth: int):
        """p 指向 TYPE u16。返回 (value, newpos)。"""
        typ = _unpack("<H", data, p)[0]
        if typ == _OPEN:
            return parse_container(p + 2, depth + 1)
        if typ in _STRS:
            return read_str(p + 2)
        if typ in _SCALARS:
            return read_scalar(typ, p + 2)
        return bare_value(typ), p + 2

    def parse_container(p: int, depth: int):
        if depth > _MAX_DEPTH:
            raise PdxParseError(f"二进制存档嵌套超过 {_MAX_DEPTH} 层，文件可能损坏")
        block: dict[str | int, Any] = {}
        while True:
            if p + 2 > n:
                # EOF = 隐式闭合（HOI4bin 尾部省略外层 0x0004，实测如此；
                # 也和文本解析器「截断档能读多少算多少」的行为一致）
                return _flatten(block), p
            t = _unpack("<H", data, p)[0]
            if t == _END:
                return _flatten(block), p + 2
            p += 2
            if t == _OPEN:
                child, p = parse_container(p, depth + 1)
                _put(block, "__anon", child)
                continue
            if t == _EQUAL:
                # 混合容器里的孤立 '='（jomini MixedContainer），跳过
                continue
            if t in _STRS:
                s, p = read_str(p)
                if p + 2 <= n and _unpack("<H", data, p)[0] == _EQUAL:
                    v, p = parse_value(p + 2, depth)
                    _put(block, s, v)
                else:
                    _put(block, "__bare", s)
                continue
            if t in _SCALARS:
                v, p = read_scalar(t, p)
                if p + 2 <= n and _unpack("<H", data, p)[0] == _EQUAL:
                    # 整数/浮点键：states={ 1={...} } 这类
                    vv, p = parse_value(p + 2, depth)
                    _put(block, str(int(v)) if isinstance(v, int) else repr(v), vv)
                else:
                    _put(block, "__bare", v)
                continue
            # u16 token 键
            if p + 2 <= n and _unpack("<H", data, p)[0] == _EQUAL:
                v, p = parse_value(p + 2, depth)
                if named and t in date_tokens and type(v) is int:
                    try:
                        v = decode_date(v)
                    except ValueError:
                        pass
                if named:
                    key: Any = toks.get(t)
                    _put(block, key if key is not None else t, v)
                else:
                    _put(block, t, v)
            else:
                # 裸 token（枚举列表元素）
                _put(block, "__bare", bare_value(t))

    result, _ = parse_container(7, 1)
    if not isinstance(result, dict):
        raise PdxParseError("二进制存档顶层不是对象")
    return result


def load_binary(path: str | Path, raw_mode: bool = False) -> dict:
    """读文件并解析。raw_mode=True 给对齐工具用。"""
    raw = Path(path).read_bytes()
    if raw_mode:
        return parse_binary(raw, tokens=None)
    return parse_binary(raw)


# ── 头部指纹（只读前几 KB，不整档解析）──────────────────

# 顶层头部字段的 token（跨版本稳定，已在 1.14.8 / 1.16.9 实档验证）
HEAD_TOKENS = {
    0x2A35: "player",
    0x2E3E: "ideology",
    0x284A: "date",
    0x299F: "difficulty",
    0x00EE: "version",
    0x2DF8: "tutorial",
    0x349D: "save_version",
    0x3B80: "game_unique_id",
    0x3682: "session",
}


def _skip_container(raw: bytes, p: int, n: int) -> int:
    """线性跳过嵌套容器；p 指向容器内第一个 entry。

    返回配对 END 之后的位置；缓冲耗尽返回 n（调用方按「读不到」处理）。
    磁带自描述：每个 u16 都能定位自己的载荷长度，无需区分键/值位置。
    """
    depth = 1
    while p + 2 <= n:
        t = _unpack("<H", raw, p)[0]
        p += 2
        if t == _OPEN:
            depth += 1
        elif t == _END:
            depth -= 1
            if depth == 0:
                return p
        elif t in _STRS:
            if p + 2 > n:
                return n
            p += 2 + _unpack("<H", raw, p)[0]
        elif t == _BOOL:
            p += 1
        elif t == _FIXED3_U16:
            p += 2
        elif t in (_I32, _U32, _F32, _FIXED3_I32):
            p += 4
        elif t in (_U64, _F64):
            p += 8
        # 其余 u16（token 键、'='、bareword 枚举）：0 字节载荷
    return n


def head_fields(raw: bytes, max_entries: int = 300) -> dict:
    """从二进制存档头部提取指纹字段，供 save_meta.read_head 用。

    只走顶层 entry，嵌套容器整体跳过；头部字段（player/date/version/…）
    都排在超大容器之前、文件头 1KB 内，8KB 读取足够。缓冲耗尽时返回
    已收集到的字段，不报错（read_head 自己判断缺不缺关键项）。

    返回值与文本头同构：date 是解码后的 "Y.M.D.H"，tutorial 是
    "yes"/"no"，session/save_version 是 str，difficulty 是枚举名。
    """
    out: dict[str, Any] = {}
    if not is_binary_save(raw):
        return out
    toks, _ = _token_names()
    toks = toks or {}
    n = len(raw)
    p = 7
    entries = 0
    while p + 2 <= n and entries < max_entries:
        t = _unpack("<H", raw, p)[0]
        p += 2
        if t == _END:
            break
        if t == _OPEN:  # 顶层匿名容器（理论上没有），保守跳过
            p = _skip_container(raw, p, n)
            entries += 1
            continue
        entries += 1
        if p + 2 > n or _unpack("<H", raw, p)[0] != _EQUAL:
            break  # 裸 token 开头，后续尺寸不可靠，停
        p += 2
        if p + 2 > n:
            break
        typ = _unpack("<H", raw, p)[0]
        p += 2
        val: Any
        if typ == _OPEN:
            p = _skip_container(raw, p, n)
            if p >= n and t in HEAD_TOKENS:
                break  # 容器被截断，后面也读不到了
            continue
        if typ in _STRS:
            if p + 2 > n:
                break
            ln = _unpack("<H", raw, p)[0]
            if p + 2 + ln > n:
                break
            val = raw[p + 2:p + 2 + ln].decode("utf-8", "replace")
            if val[:1] == "\ufeff":
                val = val[1:]
            p += 2 + ln
        elif typ in _SCALARS:
            size = (1 if typ == _BOOL else 2 if typ == _FIXED3_U16
                    else 8 if typ in (_U64, _F64) else 4)
            if p + size > n:
                break
            if typ == _BOOL:
                val = bool(raw[p])
            elif typ == _I32:
                val = _unpack("<i", raw, p)[0]
            elif typ == _U32:
                val = _unpack("<I", raw, p)[0]
            elif typ == _U64:
                val = _unpack("<Q", raw, p)[0]
            elif typ == _F32:
                val = round(_unpack("<f", raw, p)[0], 6)
            elif typ == _FIXED3_I32:
                val = _unpack("<i", raw, p)[0]  # 原始整数（同 parse_binary）
            elif typ == _FIXED3_U16:
                val = _unpack("<H", raw, p)[0]
            else:
                val = _unpack("<d", raw, p)[0]
            p += size
        else:
            val = toks.get(typ, f"@{typ:04x}")  # bareword 枚举
        name = HEAD_TOKENS.get(t)
        if name is None:
            continue
        if name == "date" and type(val) is int:
            try:
                val = decode_date(val)
            except ValueError:
                pass
        if name == "tutorial" and isinstance(val, bool):
            val = "yes" if val else "no"
        if name in ("session", "save_version") and not isinstance(val, str):
            val = str(val)
        out[name] = val
    return out

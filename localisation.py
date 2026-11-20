"""汉化表：把游戏内部 key 和外文师名翻成能直接念给用户的中文。

资料出处（不是我猜的）：
- HOI4 Wiki「Localisation」：文件在 `localisation/<语言>/*_l_<lang>.yml`，
  UTF-8-BOM，行格式 `key:0 "值"`，值里有 § 颜色码 / $变量$ / £图标。
- HOI4 Wiki「Namelist modding」「Division modding」：师名不是本地化 key，
  而是 `division_names_group` 指向 `common/units/names_divisions/*.txt`，
  里面 `ordered = { 2 = { "%d. Infanterie-Division" } }` 按 `name_order` 拼出来，
  `%d` 是阿拉伯数字、`%s` 是罗马数字，`fallback_name` 是序号用尽后的兜底。

所以这里做两件事：
1. 把简中 yml 洗成一张 key→中文 的表（带缓存，省得每次启动扫 10 MB）。
2. 把拼出来的外文番号按中文口径翻成「第2步兵师」这种。

另外 `digest()` 生成一份精简中文名词对照，随开局情报一起交给模型——
用户要的"把汉化文件给她"，给的是她用得上的那份，不是 10 MB 原文。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

__all__ = ["Localisation", "load_localisation", "zh_division_name"]

# key 后面可以跟 :0 之类的版本号；值必须双引号、单行
_KEY_RE = re.compile(
    r'^\s*([A-Za-z0-9_.\-]+)(?::\d+)?\s*:\s*"((?:[^"\\]|\\.)*)"\s*(?:#.*)?$'
)
_FMT_RE = re.compile(
    r'§[A-Za-z0-9!+\-=*%]'      # 颜色/数字格式码
    r'|£[A-Za-z0-9_]+'          # 图标
    r'|\$[A-Za-z0-9_.\-]+\$'    # 嵌套变量
)

# 番号里的外文兵种词 -> 中文。长词优先，避免 "Panzergrenadier" 被先替成 "装甲"
_WORD_ZH: tuple[tuple[str, str], ...] = (
    ("Motorisierte Infanterie-Division", "摩托化步兵师"),
    ("Motorised Infantry Division", "摩托化步兵师"),
    ("Motorized Infantry Division", "摩托化步兵师"),
    ("Panzergrenadier-Division", "装甲掷弹兵师"),
    ("Fallschirmjäger-Division", "伞兵师"),
    ("Parachute Division", "伞兵师"),
    ("Gebirgsjäger-Division", "山地师"),
    ("Gebirgs-Division", "山地师"),
    ("Mountain Division", "山地师"),
    ("Infanterie-Division", "步兵师"),
    ("Infantry Division", "步兵师"),
    ("Panzer-Division", "装甲师"),
    ("Panzer Division", "装甲师"),
    ("Armored Division", "装甲师"),
    ("Armoured Division", "装甲师"),
    ("Kavallerie-Division", "骑兵师"),
    ("Cavalry Division", "骑兵师"),
    ("Grenadier-Division", "掷弹兵师"),
    ("Marine-Division", "海军陆战师"),
    ("Marine Division", "海军陆战师"),
    ("Artillerie-Division", "炮兵师"),
    ("Artillery Division", "炮兵师"),
    ("Motorised Division", "摩托化师"),
    ("Motorized Division", "摩托化师"),
    ("Mechanised Division", "机械化师"),
    ("Mechanized Division", "机械化师"),
    ("Light Division", "轻型师"),
    ("Division", "师"),
    ("Infanterie", "步兵"),
    ("Infantry", "步兵"),
    ("Panzergrenadier", "装甲掷弹兵"),
    ("Grenadier", "掷弹兵"),
    ("Fallschirmjäger", "伞兵"),
    ("Gebirgsjäger", "山地兵"),
    ("Kavallerie", "骑兵"),
    ("Cavalry", "骑兵"),
    ("Panzer", "装甲"),
    ("Artillerie", "炮兵"),
    ("Artillery", "炮兵"),
    ("Marine", "海军陆战"),
    ("Brigade", "旅"),
    ("Regiment", "团"),
)


def _clean(value: str) -> str:
    value = (value.replace('\\n', ' ').replace('\\t', ' ')
             .replace('\\"', '"').replace('\\\\', '\\'))
    value = _FMT_RE.sub("", value)
    return re.sub(r"\s+", " ", value).strip()


def _roman(n: int) -> str:
    out = ""
    for val, sym in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
                     (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
                     (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while n >= val:
            out += sym
            n -= val
    return out or "I"


def zh_division_name(raw: str, order: int, loc: "Localisation | None" = None) -> str:
    """`"%d. Infanterie-Division"` + name_order=2 -> `第2步兵师`。

    raw 也可能是本地化 key，或玩家自己起的中文名——中文原样保留，不硬翻。
    """
    raw = (raw or "").strip()
    if not raw:
        return f"第{order}师"
    if loc is not None:
        hit = loc.get(raw)
        if hit:
            raw = hit
    name = raw.replace("%d", str(order)).replace("%s", _roman(order))
    # 已经是中文（玩家自定义名 / 中文番号），直接返回
    if re.search(r"[\u4e00-\u9fff]", name) and not re.search(r"[A-Za-z]{4,}", name):
        return name
    m = re.match(r"^\s*(\d+)\s*[.、]\s*(.+?)\s*$", name)
    if m:
        return f"第{m.group(1)}{_translate(m.group(2))}"
    return _translate(name)


def _translate(text: str) -> str:
    out = text
    for en, zh in _WORD_ZH:
        out = out.replace(en, zh)
    # 剩下的长外文词不硬造翻译，留原样比编一个假名字强
    return re.sub(r"\s+", " ", out).strip(" .-")


class Localisation:
    """key -> 中文。查不到就返回空串，由调用方决定怎么兜底。"""

    def __init__(self, table: dict[str, str] | None = None,
                 source: str = "", scanned: int = 0) -> None:
        self.table = table or {}
        self.source = source
        self.scanned = scanned

    def get(self, key: str, default: str = "") -> str:
        return self.table.get(key, default)

    def country(self, tag: str) -> str:
        tag = (tag or "").strip()
        return (self.table.get(tag) or self.table.get(tag + "_DEF")
                or self.table.get(tag + "_ADJ") or "")

    def state(self, sid: int) -> str:
        return self.table.get(f"STATE_{sid}", "")

    def digest(self, tags=(), state_ids=(), max_rows: int = 60) -> str:
        """给模型的中文名词对照（汉化文件里她用得上的那部分）。"""
        lines = [f"【中文名词对照｜取自游戏简体中文汉化文件，共收录 {len(self.table)} 条】"]
        countries = [f"{t}={nm}" for t in (tags or ())
                     for nm in [self.country(t)] if nm and nm != t]
        if countries:
            lines.append("国家：" + "、".join(countries[:30]))
        states = [f"{sid}={nm}" for sid in (state_ids or ())
                  for nm in [self.state(sid)] if nm]
        if states:
            lines.append("地区：" + "、".join(states[:max_rows]))
        lines.append("编制层级：集团军群（元帅，下辖若干集团军）→ 集团军（下辖若干师）→ 师。")
        lines.append("兵种：infantry→步兵、armor→装甲/坦克、motorized→摩托化、"
                     "mountaineers→山地、paratrooper→伞兵、artillery→炮兵。")
        lines.append("师名是存档里的「番号组 + 序号」拼出来的，上面已按中文口径写成番号；"
                     "集团军 / 集团军群的名字就是存档里玩家或 AI 起的中文名。")
        return "\n".join(lines)


def load_localisation(game_dir: str | Path | None,
                      cache_dir: str | Path | None = None) -> Localisation:
    """优先用当前世界资料库里的国名/州名（跟着 mod 走，TFR 这类 mod
    新增的地名也能读到）；没有资料库再扫游戏目录的简中 yml。"""
    try:
        from . import panel
        names = panel.current_names()
    except Exception:
        names = None
    if names and (names.get("countries") or names.get("states")):
        db: dict[str, str] = {}
        for tag, v in (names.get("countries") or {}).items():
            if v:
                db[str(tag)] = v
        for sid, v in (names.get("states") or {}).items():
            if v:
                db[f"STATE_{sid}"] = v
        return Localisation(db, f"资料库 {names.get('world') or ''}", len(db))
    if not game_dir:
        return Localisation(source="没找到游戏目录")
    root = Path(game_dir)
    folders = [root / "localisation" / "simp_chinese"]
    replace = root / "localisation" / "replace"
    if replace.is_dir():
        folders.append(replace)      # replace 后加载，覆盖同 key
    folders = [f for f in folders if f.is_dir()]
    if not folders:
        return Localisation(source="没找到简中汉化目录")

    files: list[Path] = []
    for f in folders:
        files.extend(sorted(f.glob("*.yml")))
    if not files:
        return Localisation(source="简中汉化目录里没有 yml")

    stamp = (str(root), len(files),
             int(max(f.stat().st_mtime for f in files)),
             max(f.stat().st_size for f in files))
    cache = Path(cache_dir) / "loc_cache.json" if cache_dir else None
    if cache and cache.is_file():
        try:
            old = json.loads(cache.read_text(encoding="utf-8"))
            if old.get("stamp") == list(stamp):
                return Localisation(old.get("table") or {}, str(root),
                                    len(old.get("table") or {}))
        except (OSError, ValueError):
            pass

    table: dict[str, str] = {}
    for f in files:
        try:
            text = f.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            # 长文本是事件/国策描述，名词表用不上，别占内存
            if len(line) > 200:
                continue
            m = _KEY_RE.match(line)
            if not m:
                continue
            value = _clean(m.group(2))
            if value:
                table[m.group(1)] = value

    if cache:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"stamp": list(stamp), "table": table},
                                        ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return Localisation(table, str(root), len(table))

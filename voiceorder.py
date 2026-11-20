"""听出来的那句话里，如果是一道"调令"，就地翻译成插件自己的动作。

## 为什么要单独做一层

语音认出来的文字直接丢给对话模型也能调兵，但那条路要等模型、要等工具回调，
一句"把华沙的师调到柏林"来回三四秒，而玩家在游戏里是按着键说话的，节奏不能等。

所以这里做一件事：**只认最短的几种游戏指令**，认出来就直接调插件自己的
`hoi4_move_units` / `hoi4_battle_plan` / `hoi4_units_near`，不等模型；
认不出来就原样交回对话，让她按平时的样子理解。

## 只认三种，认不准就放手

| 说法 | 翻译成 |
|---|---|
| 把华沙的师调到柏林 / 从华沙派两个师去柏林 | `move_units(from=华沙, to=柏林, count=n)` |
| 在柏林划一条防线 / 守住华沙 | `battle_plan(defence, army_at=柏林)` |
| 柏林附近有什么部队 / 看看华沙周围 | `units_near(where=柏林)` |

宁可漏认（变成普通聊天），也不要错认 —— 说"政治点调到500"被当成调兵，
那就是在替玩家乱动他的存档。
"""

from __future__ import annotations

import re

__all__ = ["parse", "OrderError", "parse_country_reply"]

# 动词 + 介词，必须成对出现才算调兵。"调"后面跟着"整""查""出来"都不算。
_MOVE_VERBS = "调|派|移|开|拉|送|推|扔|挪|抽|调拨|派遣"
_MOVE_PREPS = "到|去|往|至|向"

_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
        "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "几": 3}

# 这些词后面跟着"调到 X"不是调兵，是别的东西（资源/点数/工厂）
_NOT_ARMY_TOKENS = (
    "政治点", "指挥点", "政治力量", "工厂", "民用工厂", "军用工厂", "军工厂",
    "科研", "研究", "人力", "兵役", "装备", "资源", "补给", "稳定度",
    "战争支持度", "基础设施", "建筑", "国策", "焦点", "预算", "产能",
    "生产线", "效率", "速度", "税率", "音量", "画质", "分辨率",
)

# 泛指"我的部队"，不是具体出发地
_VAGUE_SOURCES = (
    "部队", "师", "兵", "军团", "军", "全军", "所有部队", "全部部队",
    "所有师", "所有兵", "我的部队", "我的师", "我的兵", "全部", "所有",
    "这些部队", "那些部队", "主力", "兵力",
)

_TAIL_NOISE = ("吧", "呗", "啊", "呀", "哦", "了", "一下", "那边", "那里",
               "去", "好", "谢谢", "快点", "赶紧", "喵", "呢", "嘛", "的")
_HEAD_NOISE = ("帮我", "给我", "麻烦", "请", "让我", "把", "将", "从", "然后",
               "现在", "马上", "立刻", "快", "能不能", "可不可以",
               "看看", "查查", "看一下", "看一眼", "瞅瞅", "瞧瞧",
               "在", "你", "喵")

# 动词后面到介词之间允许夹着宾语（"派两个师去"的"两个师"）
_PREPS = "过去|到|去|往|至|向"


class OrderError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _clean(text: str) -> str:
    s = str(text or "").strip()
    s = re.sub(r"[\s，。！？、,.!?；;：:“”\"'（）()]", "", s)
    # 用户习惯先喊名字再下令：「YUI你看一下欧洲西部海岸线」
    s = re.sub(r"^(?:yui|尤伊|由依|结衣)+", "", s, flags=re.I)
    return s


def _strip_head(s: str) -> str:
    changed = True
    while changed and s:
        changed = False
        for w in _HEAD_NOISE:
            if s.startswith(w):
                s = s[len(w):]
                changed = True
    return s


def _strip_tail(s: str) -> str:
    changed = True
    while changed and s:
        changed = False
        for w in _TAIL_NOISE:
            if s.endswith(w) and len(s) > len(w):
                s = s[: -len(w)]
                changed = True
    return s


_COUNT_RE = re.compile(
    r"(?:(\d+)|([一两二三四五六七八九十几]))(?:个|座|支|只)[\u4e00-\u9fa5]{0,3}?(?:师|部队|军|兵团)"
)


def _num_of(s: str) -> int:
    s = str(s or "").strip()
    if not s:
        return 0
    if s.isdigit():
        try:
            return int(s)
        except ValueError:
            return 0
    return _NUM.get(s, 0)


def _find_count(text: str) -> tuple[int, str]:
    """从整句里挖出"N 个师"。

    量词（个/座/支/只）是必须的：没有它，"第三集团军"会被读成 3 个师。
    """
    m = _COUNT_RE.search(text)
    if not m:
        return 0, text
    n = _num_of(m.group(1) or m.group(2))
    return n, (text[: m.start()] + text[m.end():])


def _cut_preamble(s: str) -> str:
    """把"我做以下部署/你记一下"这类开场白切掉，从句子的祈使结构开始。

    取最后一个 把/将/从 之前的全部丢掉（"把 A 从 B 调到 C" 里"从"更靠后，
    那句话的出发地本来就是 B）。句子从这三个字之一开头时原样返回。
    """
    idx = -1
    for ch in ("把", "将", "从"):
        i = s.rfind(ch)
        if i > idx:
            idx = i
    return s[idx:] if idx > 0 else s


def _looks_like_unit(tail: str) -> bool:
    return len(tail) <= 10 and any(w in tail for w in ("师", "部队", "兵", "军", "团"))


def _clean_place(s: str) -> str:
    """把"把柏林所在省份勃兰登堡的一个散兵师"修成"勃兰登堡"。

    语音识别出来的地名周围全是噪音：祈使词、所在省、量词、部队名。
    这层只管削词，不判断真假——认不认得出来交给 places.resolve。
    """
    s = (s or "").strip()
    changed = True
    while changed and s:
        changed = False
        for w in ("把", "将", "从", "在", "对", "给", "向", "往", "到", "去", "至"):
            if s.startswith(w) and len(s) > 1:
                s = s[len(w):]
                changed = True
    # "柏林所在省份勃兰登堡" → "勃兰登堡"
    if "所在" in s:
        s = s.split("所在", 1)[1]
        s = s.lstrip("省州城份市地区")
    # "华沙的师" / "勃兰登堡的一个散兵师" → 去掉"的"后面的部队名词
    if "的" in s:
        head, tail = s.rsplit("的", 1)
        if head and _looks_like_unit(tail):
            s = head
    for suf in ("的部队", "的师", "的兵", "部队", "师", "兵", "兵团", "军团"):
        if s.endswith(suf) and len(s) > len(suf):
            s = s[: -len(suf)]
    return _strip_tail(s)


# 兵种关键词（用户说"那个伞兵师"时用来挑师）。长词在前，免得"陆战"抢在"海军陆战"前。
_UNIT_TYPES = (
    ("海军陆战", "海军陆战"), ("陆战队", "海军陆战"),
    ("伞兵", "伞兵"), ("空降", "伞兵"), ("散兵", "伞兵"), ("伞降", "伞兵"),
    ("坦克", "装甲"), ("装甲", "装甲"),
    ("摩托化", "摩托化"), ("机械化", "机械化"),
    ("骑兵", "骑兵"), ("山地", "山地"),
    ("步兵", "步兵"), ("民兵", "民兵"), ("卫戍", "卫戍"),
)


def _find_unit_type(text: str) -> str:
    for word, canon in _UNIT_TYPES:
        if word in text:
            return canon
    return ""


def _parse_move(text: str) -> dict | None:
    if any(tok in text for tok in _NOT_ARMY_TOKENS):
        return None
    s = _cut_preamble(text)
    # 动词和介词之间可能夹着宾语：'派两个师去柏林' / '调过去' / '调用到'
    m = re.search(rf"(?:{_MOVE_VERBS})[^\s]{{0,6}}?(?:{_PREPS})", s)
    if not m:
        return None
    head = s[: m.start()]
    tail = s[m.end():]
    if not tail:
        return None
    count, _rest = _find_count(s)
    src = _clean_place(head)
    dst = _clean_place(tail)
    unit_type = _find_unit_type(s)
    if src in _VAGUE_SOURCES:
        src = ""
    # 目的地说不出个地名（纯数字/纯符号），不认
    if not dst or dst.isdigit():
        return None
    return {"kind": "move",
            "args": {"from_place": src, "to_place": dst, "count": count,
                     "unit_type": unit_type},
            "echo": f"调兵：{src or '（没说从哪）'} → {dst}"
                    + (f"，{count} 个师" if count else "")
                    + (f"（{unit_type}）" if unit_type else "")}


def _parse_defence(text: str) -> dict | None:
    if "防线" in text or "防御线" in text:
        s = text
        for w in ("建立", "划出", "拉一条", "划一条", "布一条", "一条", "划", "拉", "布",
                  "建立一条", "新的", "新", "防线", "防御线", "的"):
            s = s.replace(w, "")
        place = _strip_tail(_strip_head(s))
        if place:
            return {"kind": "defence", "args": {"army_at": place},
                    "echo": f"划线：守住 {place}"}
        return None
    for head in ("守住", "防守", "防御", "守"):
        if text.startswith(head):
            place = _strip_tail(_strip_head(text[len(head):]))
            if place:
                return {"kind": "defence", "args": {"army_at": place},
                        "echo": f"划线：守住 {place}"}
    return None


def _parse_near(text: str) -> dict | None:
    for kw in ("附近", "周围", "周边"):
        if kw in text:
            idx = text.index(kw)
            head = _strip_head(text[:idx])
            if head and head not in ("这", "那", "我"):
                return {"kind": "near", "args": {"where": head},
                        "echo": f"查部队：{head} {kw}"}
    m = re.match(r"^(?:看看|查查|看看|查一下|看一下)?(.+?)(?:有|都有|都有哪)(?:什么|哪些)?(?:部队|兵|师)",
                 text)
    if m:
        head = _strip_head(m.group(1))
        if head and head not in ("我", "我的", "这", "那"):
            return {"kind": "near", "args": {"where": head},
                    "echo": f"查部队：{head}"}
    return None


_CONFIRM_WORDS = ("是", "是的", "对", "对的", "没错", "是我", "就是我", "嗯", "嗯对",
                  "正确", "确定", "没错就是", "对的没错", "就是")

_COUNTRY_PREFIXES = (
    "我这一局玩的是", "我这一局是", "这一局玩的是", "这一局是",
    "我这局玩的是", "我这局是", "这局玩的是", "这局是", "本局是",
    "我玩的是", "我玩儿的", "我玩的", "我玩", "我是",
    "我选择的是", "我选的", "我选的是", "选的是", "玩的是", "当前是", "是",
)

# 回答里出现这些词就不是国名（是命令/地名/疑问）
_COUNTRY_BAD = ("把", "调", "派", "师", "兵", "部队", "军", "省", "州", "市", "城",
                "吗", "么", "什么", "哪", "怎么", "为什么", "不", "别", "让",
                "去", "到", "给我", "帮我")


def parse_country_reply(text: str) -> tuple[str, str] | None:
    """识别"你玩的是哪国"的回答。

    只在**有一条被国家卡住的调令挂着**时才会用，所以可以宽松点：
    - "是/对/是我/没错" → ("confirm", "")：确认刚才猜的那个国家
    - "德国" / "我玩的是德国" / "是德国" → ("name", "德国")
    认不出返回 None（这句就照常交给对话模型）。
    """
    s = _clean(text)
    if not s or len(s) > 16:
        return None
    if s in _CONFIRM_WORDS:
        return ("confirm", "")
    for p in sorted(_COUNTRY_PREFIXES, key=len, reverse=True):
        if s.startswith(p) and len(s) > len(p):
            s = s[len(p):]
            break
    s = _strip_tail(s)
    s = s.lstrip("在")
    if not s:
        return None
    if s in _CONFIRM_WORDS:
        return ("confirm", "")
    if len(s) > 8:
        return None
    if any(w in s for w in _COUNTRY_BAD):
        return None
    return ("name", s)


_CN_NUM = {"零": "0", "一": "1", "二": "2", "两": "2", "三": "3", "四": "4",
           "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}


def _army_token(s: str) -> str:
    """把「九集团军」「第9集团军」「9军」统一成「9集团军」这种。"""
    s = s.replace("个", "")
    for cn, ar in _CN_NUM.items():
        s = s.replace(cn, ar)
    s = s.replace("十", "10")
    s = s.replace("第", "")
    if "集团军" in s:
        head = s.split("集团军")[0]
        return f"{head}集团军" if head else "集团军"
    if s.endswith("军"):
        return f"{s[:-1]}集团军" if s[:-1] else s
    return s


def _parse_take_command(text: str) -> dict | None:
    """「我把第9集团军指挥权给你」「你来指挥第9集团军」「第9集团军交给你」"""
    if "集团军" not in text and "军" not in text:
        return None
    if not any(w in text for w in ("指挥权", "指挥", "交给你", "给你",
                                   "你来", "接管", "归你")):
        return None
    s = text
    for w in ("我现在把", "我把", "请你", "你替我", "帮我", "现在", "把",
              "交给你了", "交给你", "给你了", "给你", "指挥权", "指挥",
              "你来", "接管", "归你", "了", "吧"):
        s = s.replace(w, "")
    s = _strip_tail(_strip_head(s))
    if not s or "军" not in s:
        return None
    army = _army_token(s)
    if not army or army == "集团军":
        return None
    return {"kind": "take", "args": {"army": army},
            "echo": f"接管：{army}"}


def _parse_auto_attack(text: str) -> dict | None:
    """「开始进攻」「持续推进」「停手」「别打了」"""
    if any(w in text for w in ("开始进攻", "自动进攻", "持续进攻", "往前推",
                               "持续推进", "自己打", "你来打")):
        return {"kind": "auto", "args": {}, "echo": "接管部队开始自动推进"}
    if any(w in text for w in ("停手", "暂停进攻", "别打了", "停止进攻",
                               "先别动", "停下来", "收手")):
        return {"kind": "pause", "args": {}, "echo": "接管部队停手"}
    return None


def _parse_look(text: str) -> dict | None:
    """「你看一下欧洲西部海岸线」「法国海岸怎么样」「北海那边有兵吗」"""
    for kw in ("海岸线", "海岸", "沿岸"):
        if kw not in text:
            continue
        s = text
        for w in ("你看一下", "你去看一下", "你去看", "你看", "看看", "看一下",
                  "查一下", "瞅瞅", "那边", "这边", "一带", "怎么样", "如何",
                  "有兵吗", "部队", "的", "了"):
            s = s.replace(w, "")
        s = _strip_tail(_strip_head(s))
        if not s:
            continue
        if not s.endswith(kw):
            s += kw
        return {"kind": "look", "args": {"area": s}, "echo": f"看区域：{s}"}
    return None


def parse(text: str) -> dict | None:
    """认出来就返回 {"kind","args","echo"}，认不出返回 None。"""
    raw = _clean(text)
    if not raw or len(raw) < 2:
        return None
    for fn in (_parse_take_command, _parse_auto_attack, _parse_look,
               _parse_move, _parse_defence, _parse_near):
        try:
            got = fn(raw)
        except Exception:  # noqa: BLE001 解析绝不能把语音线程搞死
            got = None
        if got:
            got["text"] = raw
            return got
    return None


if __name__ == "__main__":
    samples = [
        "把华沙的师调到柏林",
        "从华沙派两个师去柏林吧",
        "帮我把柏林的部队调到华沙",
        "把部队调到柏林",
        "政治点调到500",
        "在柏林划一条防线",
        "守住华沙",
        "柏林附近有什么部队",
        "看看华沙周围",
        "我今天想吃拉面",
        "把第三师调过去",
        "调兵到柏林",
    ]
    for s in samples:
        got = parse(s)
        print(f"{s!r:34} -> {got['kind'] if got else '不认'}"
              + (f"  {got['args']}" if got else ""))

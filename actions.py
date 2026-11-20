"""能发给游戏的控制台命令 —— 白名单，不是黑名单。

为什么是白名单而不是"用户说什么就发什么"：不是为了安全，是**她可能拼错**。
`moveunit 12 3401` 里的 unit_id 是字符串，错一个字符就是调错部队，
而玩家很可能正在手动指挥同一支部队。所以只放"纯加法、加了也不会有坏处"的这一档。

这一档的边界（来自开发思路第三节的结论）：
- 加数值：政治点 / 人力 / 陆军经验 / 指挥点 / 燃料 / 稳定度 / 战争支持
- 加速类：研究加速 / 瞬间建造 / 瞬间训练 / 国策自动完成
- 加民族精神（有明确 id，可校验字符集）
- **不做**：moveunit / tp / run / 宣战 / 吞并 / 切国家 / 读档存档

另外一条隐性规矩：命令必须是纯 ASCII。控制台走 WM_CHAR 投递，
中文会变成一堆乱码按键，宁可在这一层拦掉。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "ActError",
    "Action",
    "ACTIONS",
    "DENIED",
    "resolve",
    "action_list",
    "verifiable_fields",
]


class ActError(ValueError):
    """拒绝执行。kind 用来让上层给不同的提示，不要一律说"不行"。"""

    def __init__(self, kind: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.hint = hint


@dataclass(frozen=True)
class Action:
    key: str
    cmd: str                   # 模板，{n} 是数量、{extra} 是附加参数
    label: str                 # 中文名，提示里用它
    field: str = ""            # snapshot["player"] 里的字段名，用于"下次存档核对"
    needs_amount: bool = True
    minimum: int = 1
    maximum: int = 10_000
    note: str = ""


ACTIONS: dict[str, Action] = {
    a.key: a
    for a in (
        Action("add_political_power", "pp {n}", "政治点", "政治点", maximum=5_000),
        Action("add_manpower", "manpower {n}", "人力", "人力", minimum=1_000, maximum=2_000_000),
        Action("add_army_xp", "xp {n}", "陆军经验", "陆军经验", maximum=5_000),
        Action("add_command_power", "command_power {n}", "指挥点", "指挥点", maximum=200),
        Action("add_fuel", "fuel {n}", "燃料", "燃料", maximum=100_000),
        # 稳定度和战争支持是 0~100 的百分比，一次加太多没意义
        Action("add_stability", "add_stability {n}", "稳定度", "稳定度", maximum=50),
        Action("add_war_support", "add_war_support {n}", "战争支持", "战争支持", maximum=50),
        Action("research_fast", "research_fast", "研究加速", needs_amount=False,
               note="一次生效，对已经排队的科技也管用"),
        Action("instant_construction", "instantconstruction", "瞬间建造", needs_amount=False,
               note="只对已经排进队列的建筑生效，凭空加建筑做不到"),
        Action("instant_training", "instanttraining", "瞬间训练", needs_amount=False),
        Action("focus_autocomplete", "Focus.AutoComplete", "国策自动完成", needs_amount=False,
               note="是个开关，再发一次就关掉"),
        Action("add_ideas", "add_ideas {extra}", "民族精神", needs_amount=False,
               note="要真实的 idea id，拼错了游戏只飘一行红字，看不出来"),
    )
}

# 模型可能直接说游戏原生命令，给几个常见别名，省一轮来回
ALIASES = {
    "pp": "add_political_power",
    "political_power": "add_political_power",
    "manpower": "add_manpower",
    "xp": "add_army_xp",
    "army_xp": "add_army_xp",
    "command_power": "add_command_power",
    "cp": "add_command_power",
    "fuel": "add_fuel",
    "stability": "add_stability",
    "war_support": "add_war_support",
    "research": "research_fast",
    "instantconstruction": "instant_construction",
    "instanttraining": "instant_training",
    "ideas": "add_ideas",
}

# 明确拒绝的，理由要能说出口（用户会觉得"为什么不帮我调兵"）
DENIED: dict[str, str] = {
    "move_unit": "调部队",
    "moveunit": "调部队",
    "teleport": "瞬移部队",
    "tp": "瞬移部队",
    "run": "执行脚本文件",
    "run_script": "执行脚本文件",
    "execute": "执行脚本",
    "declare_war": "宣战",
    "war": "宣战",
    "annex": "吞并",
    "puppet": "扶植傀儡",
    "add_core": "加核心领土",
    "set_owner": "改州归属",
    "remove_core": "去核心",
    "tag": "切换国家",
    "switch_tag": "切换国家",
    "event": "触发事件",
    "fire_event": "触发事件",
    "whitepeace": "白和平",
    "yesman": "无限外交（AI 全答应）",
    "load": "读档",
    "save": "存档",
    "reload": "重载存档",
}

_IDEA_RE = re.compile(r"^[a-z][a-z0-9_]{1,48}$")


def action_list() -> str:
    return "、".join(f"{a.key}（{a.label}）" for a in ACTIONS.values())


def verifiable_fields() -> set[str]:
    return {a.field for a in ACTIONS.values() if a.field}


def resolve(action: str, amount: int = 0, extra: str = "") -> dict:
    """把 (动作, 数量, 附加参数) 变成一条能投递的命令。

    通过白名单返回 {"cmd","label","field","amount","note"}；
    否则抛 ActError，kind 说明为什么不行。**绝不返回一条自己没校验过的命令。**
    """
    raw = (action or "").strip()
    key = raw.lower().replace("-", "_").replace(" ", "_")

    if key in DENIED or raw.lower() in DENIED:
        label = DENIED.get(key) or DENIED.get(raw.lower()) or "这个操作"
        raise ActError(
            "denied",
            f"{label}这类命令我不发。它能直接改局面，发错一次就要读档，"
            f"而你说不定正在自己指挥这支部队。",
            "给她一个替代建议（比如「这块你自己点更快，我只报数」），别硬发命令。",
        )

    key = ALIASES.get(key, key)
    spec = ACTIONS.get(key)
    if spec is None:
        raise ActError(
            "unknown",
            f"这个动作不在名单里：{raw}。我能做的只有这些：{action_list()}。",
            "原样转发，不要自己编一个动作名重试。",
        )

    try:
        n = int(amount or 0)
    except (TypeError, ValueError):
        raise ActError("bad_amount", f"数量得是整数，收到的是 {amount!r}。") from None

    extra = (extra or "").strip()

    if spec.needs_amount:
        if n <= 0:
            raise ActError(
                "bad_amount",
                f"{spec.label}要指定加多少（1 到 {spec.maximum} 之间）。",
                "问用户想加多少，别自己猜一个数发出去。",
            )
        if n > spec.maximum:
            raise ActError(
                "bad_amount",
                f"一次最多加 {spec.maximum} 点{spec.label}，你说的是 {n}。"
                f"要更多就分几次，或者直接把上限告诉我。",
            )
        if n < spec.minimum:
            raise ActError("bad_amount", f"{spec.label}一次至少 {spec.minimum}。")
        cmd = spec.cmd.format(n=n)
        out_amount = n
    else:
        if "{extra}" in spec.cmd:
            if not _IDEA_RE.match(extra):
                raise ActError(
                    "bad_extra",
                    "这个操作要一个 id（小写字母开头，只能有字母数字和下划线），"
                    "不然游戏会静默失败。",
                    "问用户要具体的 id，别自己编。",
                )
            cmd = spec.cmd.format(extra=extra)
        else:
            cmd = spec.cmd
        out_amount = 0

    if not cmd.isascii():
        # 理论上到不了这里（模板都是 ASCII），但当最后一道闸留着
        raise ActError("bad_cmd", "命令里有非 ASCII 字符，游戏控制台收不了。")

    return {
        "key": key,
        "cmd": cmd,
        "label": spec.label,
        "field": spec.field,
        "amount": out_amount,
        "note": spec.note,
        "verifiable": bool(spec.field) and out_amount > 0,
    }

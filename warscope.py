"""参战范围：只让「本国 + 盟友 + 敌对国」的战况进到模型和用户眼前。

之前的问题：`frontline()` 返回的是**全服所有陆战**，哨兵的全服控制图 diff
又把**所有国家**的省份易主都报了一遍。玩家在欧洲打德国，中日在中国打得再凶
也会一条条播出来——那不是"她知道不了的内容"，是根本跟这局没关系的内容。

判定不读外交关系（内存里没有可靠偏移），只看**玩家自己参与的那些陆战**：
- 跟玩家分处攻守两侧 → 敌对国
- 跟玩家同侧并肩 → 盟友
- 其余国家（哪怕是本阵营的 AI 在别的战区另打一场）→ 不算这局的参战国

也就是说，日本是不是盟友，不看阵营列表，看它有没有跟玩家在同一场仗里并肩
打过。这样"我在欧洲、你在东亚自己打"自然被排除。

前线暂时安静时（玩家没有任何交火）不能把范围清零，否则下一轮会把整条战线
全吞掉；这时候沿用上一轮的范围， TTL 到了才收口。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

__all__ = ["Scope", "derive", "filter_rows", "row_parties",
           "advance_relevant", "public"]

DEFAULT_TTL = 900.0     # 15 分钟；够一场战役从接敌到推进，又不至于换局还带着


@dataclass
class Scope:
    player: str = ""
    allies: list[str] = field(default_factory=list)
    enemies: list[str] = field(default_factory=list)
    ts: float = 0.0
    source: str = "live"     # live | cached | save | none
    total_combats: int = 0
    filtered_combats: int = 0

    @property
    def relevant(self) -> set[str]:
        return {self.player} | set(self.allies) | set(self.enemies)

    @property
    def is_known(self) -> bool:
        """有没有认出至少一个对手或盟友（只有玩家自己 = 没认出来）。"""
        return bool(self.allies or self.enemies)

    def fresh(self, ttl: float = DEFAULT_TTL) -> bool:
        return self.ts > 0 and (time.monotonic() - self.ts) < ttl

    def touch(self) -> "Scope":
        """沿用上一轮：只刷新时间戳，别把 ts 一直往后推成永不过期。"""
        return Scope(self.player, list(self.allies), list(self.enemies),
                     time.monotonic(), "cached", self.total_combats,
                     self.filtered_combats)

    def as_dict(self) -> dict:
        return {
            "player": self.player,
            "allies": list(self.allies),
            "enemies": list(self.enemies),
            "relevant": sorted(self.relevant),
            "source": self.source,
            "total_combats": self.total_combats,
            "filtered_combats": self.filtered_combats,
        }


_INTERNAL_COUNTS = ("total_combats", "filtered_combats")


def public(scope: "Scope | dict | None") -> dict:
    """准备交给模型 / 用户的那一版范围字典。

    `total_combats` / `filtered_combats` 是排查用的诊断计数——「全服一共
    多少处交火、其中多少处被过滤掉」。0.9.9 定的边界是「别处的仗连汇总
    都不给」，这两个数字要是留在 `context` / `event` / 工具 `meta` 里，
    就等于变相告诉她「世界上还有 N 场跟你无关的仗」。内部日志照用，
    出插件之前一律摘掉。
    """
    d = scope.as_dict() if isinstance(scope, Scope) else dict(scope or {})
    for k in _INTERNAL_COUNTS:
        d.pop(k, None)
    return d


def row_parties(row: dict) -> tuple[set[str], set[str], set[str]]:
    """一场陆战里出现的国家 -> (全部, 攻方, 守方)。

    `attacker` / `defender` 是真正参战师反查出来的双方，优先用它；
    读不到才退回 `sides`（省内恰好有哪些国家的师），这时分不出攻守。
    """
    atk = set((row.get("attacker") or {}).keys())
    dfs = set((row.get("defender") or {}).keys())
    if atk or dfs:
        return atk | dfs, atk, dfs
    sides = {s.get("country") for s in (row.get("sides") or [])
             if s.get("country")}
    return sides, set(), set()


def derive(player: str, rows: list[dict], prev: "Scope | None" = None,
           ttl: float = DEFAULT_TTL) -> Scope:
    """从全服陆战里认出"玩家这局"的参战国。"""
    player = (player or "").strip()
    enemies: set[str] = set()
    allies: set[str] = set()
    for row in rows or []:
        parties, atk, dfs = row_parties(row)
        if player not in parties or not (atk or dfs):
            continue
        if player in atk:
            enemies |= dfs - {player}
            allies |= atk - {player}
        else:
            enemies |= atk - {player}
            allies |= dfs - {player}
    # 敌对国和盟友不该重叠（同一国家不会既并肩又对打；真出现了按敌人算）
    allies -= enemies
    if enemies or allies:
        return Scope(player, sorted(allies), sorted(enemies),
                     time.monotonic(), "live")
    # 玩家这一刻没直接交火：沿用上一轮，别把战线全吞了
    if prev is not None and prev.player == player and prev.is_known and prev.fresh(ttl):
        return prev.touch()
    return Scope(player, [], [], time.monotonic(), "none")


def filter_rows(rows: list[dict], scope: Scope) -> list[dict]:
    """只保留参战国之间的战斗。

    范围没认出来时（source=none）返回空列表，而不是"全服照报"——
    宁可说"这会儿没有跟你有关的交火"，也不能把别处的仗报到她面前。
    """
    if not scope.is_known:
        return []
    rel = scope.relevant
    keep = [r for r in (rows or []) if row_parties(r)[0] & rel]
    scope.total_combats = len(rows or [])
    scope.filtered_combats = len(rows or []) - len(keep)
    return keep


def advance_relevant(new_country: str, old_country: str,
                     scope: "Scope | set[str]") -> bool:
    """省份易主要不要报？两边都跟这局无关就整个丢掉，连汇总都不给。"""
    rel = scope.relevant if isinstance(scope, Scope) else set(scope)
    return bool({new_country, old_country} & rel)

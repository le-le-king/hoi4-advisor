"""存档 → 玩家视角状态（含异常检测与预排版）。

和 `hoi4_bridge/game_state.py` 的区别，三条：

1. **版本检查**。存档自带 `version`，本地游戏数据目录是另一个版本时，
   州名/省名映射会整体错位（实测遇到过：1.13.1 的存档配 1.14.8 的数据，
   中国的师被标到"九州"）。这种情况不能装作没事，得明确告诉她。
2. **只产出可落盘的裁剪结构**。解析出的 34 MB 里 99% 要扔掉，
   落地就一百来 KB，之后所有查询毫秒级。
3. **异常检测**。游戏界面不会告诉你"盟友有 3 个师赖在你国土上"，
   而这句话才是参谋的价值所在。

所有落盘走插件 `data/`，不碰游戏目录（游戏目录只读，写过一次缓存就被污染了）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .pdx_parser import Multi, as_list, get_float, get_int, get_str, load_save

__all__ = ["SaveState", "load_state", "format_date", "scan_cores"]


# 建筑 key -> 我们的字段名。mod 加新建筑时这里统计不到，
# 会在 anomalies 里挂一条"有未识别建筑"的提示，不至于悄悄漏账。
BUILDING_KEYS = {
    "industrial_complex": "civ",
    "arms_factory": "mil",
    "dockyard": "dock",
    "infrastructure": "infra",
    "anti_air_building": "aa",
    "air_base": "air_base",
    "radar_station": "radar",
    "synthetic_refinery": "refinery",
    "fuel_silo": "fuel_silo",
    "naval_base": "naval_base",
}

RESOURCE_NAMES = {
    "oil": "石油",
    "aluminium": "铝",
    "rubber": "橡胶",
    "tungsten": "钨",
    "steel": "钢",
    "chromium": "铬",
}

# 只把这些国家的盘面存进缓存。玩家 + 大国 + 邻国 + 交战方，
# 实测 334 个全存是 24 MB，存二十来个是几十 KB。
MAJOR_KEEP = ("GER", "SOV", "ENG", "FRA", "USA", "ITA", "JAP", "CHI", "RAJ", "CAN", "AST")


def format_date(raw: str) -> str:
    """`1937.3.1.2` → `1937年3月1日`。HOI4 的日期带小时，丢掉。"""
    parts = str(raw).split(".")
    if len(parts) >= 3:
        try:
            return f"{int(parts[0])}年{int(parts[1])}月{int(parts[2])}日"
        except ValueError:
            return str(raw)
    return str(raw)


def _count_building(node: Any) -> int:
    """建筑数量。

    存档里 `level` 两种形态：
        level = 100            -> 1 个
        level = [100,100,...]  -> 数组长度就是个数
    值是耐久百分比，不是等级，别被名字骗了。
    """
    if not isinstance(node, dict):
        return 0
    level = node.get("level")
    if isinstance(level, list):
        return len(level)
    return 0 if level is None else 1


def _leaf_number(node: Any) -> float | None:
    """从 `army_manpower_value={ value={ tag="LUX" value=1000 } }` 里抠出 1000。"""
    for _ in range(6):
        if isinstance(node, bool):
            return None
        if isinstance(node, (int, float)):
            return float(node)
        if not isinstance(node, dict) or "value" not in node:
            return None
        node = node["value"]
    return None


def _manpower_ratio(div: dict) -> float | None:
    """师的满编比 = 现有兵力 / 编制需求，0～1；读不到返回 None。

    0.9.8 之前把 `strength` 当百分比，用 `strength < 60` 判「缺编严重」。
    实测存档里 `strength` 是**绝对值**（德国步兵师 228.000，卢森堡民兵 22.000，
    同一个国家不同师能差 8 倍），拿 60 一刀切，16 个师里 12 个被误报缺编。
    真正的满编信息在 `army_manpower` 里，两个 value 一除就是比例。
    """
    man = div.get("army_manpower")
    if not isinstance(man, dict):
        return None
    got = _leaf_number(man.get("army_manpower_value"))
    need = _leaf_number(man.get("army_manpower_need"))
    if got is None or need is None or need <= 0:
        return None
    return max(0.0, min(1.0, got / need))


def scan_cores(game_dir) -> dict[str, set[int]]:
    """扫 history/states，返回 tag → 核心省集合。

    **纯静态游戏数据**，跟当前是哪一局无关（1936 剧本的国土），所以谁都能调：
    SaveState.country_cores() 和 zone.Zone 共用这一份解析。0.4 秒左右。
    owner / add_core_of 在 `state.history={...}` 子块里（1.16.9 实测：
    state={ id name provinces history={ owner=FRA add_core_of=FRA } }），
    顶层没有这两个字段——0.9.13 第一版就在这里读成了 0 个国家。
    """
    from pathlib import Path as _Path

    from .pdx_parser import parse

    cores: dict[str, set[int]] = {}
    states_dir = _Path(game_dir) / "history" / "states"
    if not states_dir.is_dir():
        return cores
    for path in states_dir.glob("*.txt"):
        try:
            node = parse(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 单个州文件坏了不能拖垮整体
            continue
        block = node.get("state")
        if not isinstance(block, dict):
            continue
        provs: list[int] = []
        for prov in as_list(block.get("provinces")):
            try:
                provs.append(int(prov))
            except (TypeError, ValueError):
                continue
        if not provs:
            continue
        hist = block.get("history")
        src = hist if isinstance(hist, dict) else block
        tags = {str(t) for t in as_list(src.get("add_core_of"))
                if isinstance(t, str) and t}
        owner = src.get("owner")
        if isinstance(owner, str) and owner:
            tags.add(owner)
        for t in tags:
            cores.setdefault(t, set()).update(provs)
    return cores


class SaveState:
    """一份存档，按"玩家能看见什么"组织。"""

    def __init__(self, save_path: str | Path, game_dir: str | Path | None,
                 cache_dir: str | Path | None = None) -> None:
        self.save_path = Path(save_path)
        self.game_dir = Path(game_dir) if game_dir else None
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.tree = load_save(self.save_path)
        self.root = self.tree.get("gamestate", self.tree)
        if not isinstance(self.root, dict):
            raise ValueError("存档顶层结构异常")
        self._prov_map: dict[int, int] | None = None
        self._state_names: dict[int, str] | None = None
        self._country_names: dict[str, str] | None = None
        self._cores: dict[str, list[int]] | None = None
        self._game_ver: str | None = None

    # ── 基础 ──────────────────────────────────────────

    @property
    def player_tag(self) -> str:
        tag = self.root.get("player")
        if isinstance(tag, str) and tag:
            return tag
        pc = self.root.get("player_countries")
        if isinstance(pc, dict) and pc:
            return str(next(iter(pc.keys())))
        return ""

    @property
    def date(self) -> str:
        return get_str(self.root, "date")

    @property
    def version(self) -> str:
        return get_str(self.root, "version")

    def country(self, tag: str) -> dict:
        countries = self.root.get("countries", {})
        node = countries.get(tag) if isinstance(countries, dict) else None
        return node if isinstance(node, dict) else {}

    # ── 版本一致性 ────────────────────────────────────

    def switch_game_dir(self, game_dir: str | Path) -> None:
        """换数据目录，并把名称映射全部作废重读。

        为什么要这个：存档版本只有解析完才知道（.hoi4 是 zip，头部读不到），
        所以流程是"先随便挑一个目录解析 → 读到 version → 换到匹配的目录
        → 名称映射重跑一遍"。存档树本身不用重解析。
        """
        new_dir = Path(game_dir)
        if self.game_dir and new_dir == self.game_dir:
            return
        self.game_dir = new_dir
        self._prov_map = None
        self._state_names = None
        self._country_names = None
        self._cores = None
        self._game_ver = None
        for name in ("prov_to_state.json", "state_names.json", "country_names.json",
                     "country_cores.json"):
            if self.cache_dir:
                try:
                    (self.cache_dir / name).unlink()
                except OSError:
                    pass

    def game_version(self) -> str:
        """读游戏目录里的 changelog 头，拿到本地数据版本。"""
        if self._game_ver is not None:
            return self._game_ver
        if not self.game_dir:
            self._game_ver = ""
            return self._game_ver
        changelog = self.game_dir / "changelog.txt"
        if not changelog.is_file():
            return ""
        try:
            head = changelog.read_text(encoding="utf-8", errors="replace")[:2000]
        except OSError:
            return ""
        m = re.search(r"(\d+\.\d+\.\d+)", head)
        self._game_ver = m.group(1) if m else ""
        return self._game_ver

    def version_check(self) -> dict:
        """存档版本 vs 本地数据版本。

        不一致的后果要说清楚：province/state 的编号语义会变，
        州名和部队位置会整体错位，而资源/工厂这类按 tag 统计的数字不受影响。
        """
        save_ver = self.version
        m = re.search(r"(\d+\.\d+\.\d+)", save_ver)
        save_num = m.group(1) if m else ""
        game_num = self.game_version()
        if not save_num or not game_num:
            return {"known": False, "save": save_num, "game": game_num, "warn": ""}
        if save_num == game_num:
            return {"known": True, "save": save_num, "game": game_num, "warn": ""}
        return {
            "known": True,
            "save": save_num,
            "game": game_num,
            "warn": (
                f"存档是 {save_num}，本机游戏数据是 {game_num}，"
                "两边的省/州编号不一样，州名和部队位置可能对不上"
                "（国家层面的数字，比如工厂、政治点，不受影响）"
            ),
        }

    # ── 名称映射（带缓存） ────────────────────────────

    def _cache_file(self, name: str) -> Path | None:
        if not self.cache_dir:
            return None
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return self.cache_dir / name

    def province_to_state(self) -> dict[int, int]:
        if self._prov_map is not None:
            return self._prov_map

        cache = self._cache_file("prov_to_state.json")
        if cache and cache.is_file():
            try:
                raw = json.loads(cache.read_text(encoding="utf-8"))
                # 换了数据目录/版本，旧映射就作废了，必须校验
                if raw.get("game_version", "") == self.game_version():
                    self._prov_map = {int(k): int(v) for k, v in raw["map"].items()}
                    return self._prov_map
            except (OSError, ValueError, KeyError):
                pass

        mapping: dict[int, int] = {}
        if self.game_dir:
            states_dir = self.game_dir / "history" / "states"
            if states_dir.is_dir():
                from .pdx_parser import parse

                for path in states_dir.glob("*.txt"):
                    try:
                        node = parse(path.read_text(encoding="utf-8", errors="replace"))
                    except Exception:  # noqa: BLE001 单个州文件坏了不能拖垮整体
                        continue
                    block = node.get("state")
                    if not isinstance(block, dict):
                        continue
                    sid = get_int(block, "id")
                    if not sid:
                        continue
                    for prov in as_list(block.get("provinces")):
                        try:
                            mapping[int(prov)] = sid
                        except (TypeError, ValueError):
                            continue

        self._prov_map = mapping
        if cache:
            try:
                payload = {"game_version": self.game_version(), "map": {str(k): v for k, v in mapping.items()}}
                cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        return mapping

    def state_names(self) -> dict[int, str]:
        if self._state_names is not None:
            return self._state_names

        cache = self._cache_file("state_names.json")
        if cache and cache.is_file():
            try:
                raw = json.loads(cache.read_text(encoding="utf-8"))
                if raw.get("game_version", "") == self.game_version():
                    self._state_names = {int(k): v for k, v in raw["map"].items()}
                    return self._state_names
            except (OSError, ValueError, KeyError):
                pass

        names: dict[int, str] = {}
        if self.game_dir:
            pattern = re.compile(r'^\s*STATE_(\d+)\s*:\s*\d*\s*"?([^"\n]*)"?', re.M)
            for loc_dir in (
                self.game_dir / "localisation" / "simp_chinese",
                self.game_dir / "localisation" / "english",
            ):
                if not loc_dir.is_dir():
                    continue
                for yml in loc_dir.glob("*.yml"):
                    if "state" not in yml.name.lower():
                        continue
                    try:
                        text = yml.read_text(encoding="utf-8-sig", errors="replace")
                    except OSError:
                        continue
                    for match in pattern.finditer(text):
                        names.setdefault(int(match.group(1)), match.group(2).strip())
                if names:
                    break

        self._state_names = names
        if cache:
            try:
                payload = {"game_version": self.game_version(), "map": {str(k): v for k, v in names.items()}}
                cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        return names

    def country_names(self) -> dict[str, str]:
        """tag -> 中文国名。读 localisation 的 TAG_xxx。"""
        if self._country_names is not None:
            return self._country_names

        cache = self._cache_file("country_names.json")
        if cache and cache.is_file():
            try:
                raw = json.loads(cache.read_text(encoding="utf-8"))
                if raw.get("game_version", "") == self.game_version():
                    self._country_names = dict(raw["map"])
                    return self._country_names
            except (OSError, ValueError, KeyError):
                pass

        names: dict[str, str] = {}
        if self.game_dir:
            pattern = re.compile(r'^\s*([A-Z]{3})\s*:\s*\d*\s*"([^"\n]*)"', re.M)
            for loc_dir in (
                self.game_dir / "localisation" / "simp_chinese",
                self.game_dir / "localisation" / "english",
            ):
                if not loc_dir.is_dir():
                    continue
                for yml in loc_dir.glob("*.yml"):
                    if "countr" not in yml.name.lower():
                        continue
                    try:
                        text = yml.read_text(encoding="utf-8-sig", errors="replace")
                    except OSError:
                        continue
                    for match in pattern.finditer(text):
                        names.setdefault(match.group(1), match.group(2).strip())
                if names:
                    break

        self._country_names = names
        if cache:
            try:
                cache.write_text(
                    json.dumps(
                        {"game_version": self.game_version(), "map": names}, ensure_ascii=False
                    ),
                    encoding="utf-8",
                )
            except OSError:
                pass
        return names

    def country_label(self, tag: str) -> str:
        name = self.country_names().get(tag, "")
        return f"{name}（{tag}）" if name else tag

    def country_cores(self) -> dict[str, list[int]]:
        """tag -> 本土（核心）省号列表。0.9.13：国名查部队要按国土圈范围。

        为什么要静态国土而不是"该国部队所在省"：挪威这种被占领的国家，
        残余部队可能只剩一个师躲在孤岛上，拿它当种子就看不到德国布置在
        挪威海岸线的十几个驻军——用户问"挪威附近"想看的是挪威这块地。
        数据取自 history/states 的 add_core_of + owner（1936 剧本的国土），
        按游戏版本缓存成 country_cores.json，与 prov_to_state 同一套失效规则。
        """
        if self._cores is not None:
            return self._cores

        cache = self._cache_file("country_cores.json")
        if cache and cache.is_file():
            try:
                raw = json.loads(cache.read_text(encoding="utf-8"))
                if raw.get("game_version", "") == self.game_version():
                    self._cores = {str(k): [int(p) for p in v]
                                   for k, v in raw["map"].items()}
                    return self._cores
            except (OSError, ValueError, KeyError, TypeError):
                pass

        cores: dict[str, set[int]] = {}
        if self.game_dir:
            cores = scan_cores(self.game_dir)

        self._cores = {t: sorted(s) for t, s in cores.items()}
        if cache:
            try:
                cache.write_text(
                    json.dumps({"game_version": self.game_version(),
                                "map": self._cores}, ensure_ascii=False),
                    encoding="utf-8",
                )
            except OSError:
                pass
        return self._cores

    def province_label(self, pid: int) -> str:
        sid = self.province_to_state().get(pid)
        if not sid:
            return f"省{pid}"
        return self.state_names().get(sid) or f"州{sid}"

    # ── 国家盘面 ──────────────────────────────────────

    def building_totals(self, tag: str) -> dict[str, int]:
        states = self.root.get("states", {})
        if not isinstance(states, dict):
            return {}
        totals: dict[str, int] = {}
        for state in states.values():
            if not isinstance(state, dict) or state.get("owner") != tag:
                continue
            buildings = state.get("buildings")
            if not isinstance(buildings, dict):
                continue
            for key, node in buildings.items():
                field = BUILDING_KEYS.get(key)
                if field:
                    totals[field] = totals.get(field, 0) + _count_building(node)
        return totals

    def nation_stats(self, tag: str) -> dict:
        node = self.country(tag)
        if not node:
            return {}
        politics = node.get("politics") if isinstance(node.get("politics"), dict) else {}
        fuel = node.get("fuel_status") if isinstance(node.get("fuel_status"), dict) else {}
        man = node.get("manpower") if isinstance(node.get("manpower"), dict) else {}
        res = node.get("resources") if isinstance(node.get("resources"), dict) else {}
        produced = res.get("produced") if isinstance(res.get("produced"), dict) else {}
        counts = self.building_totals(tag)

        return {
            "tag": tag,
            "政治点": round(get_float(politics, "political_power"), 1),
            "稳定度": round(get_float(node, "stability") * 100, 1),
            "战争支持": round(get_float(node, "war_support") * 100, 1),
            "指挥点": round(get_float(node, "command_power"), 1),
            "人力": get_int(man, "current") or get_int(man, "ratio"),
            "燃料": get_int(fuel, "fuel"),
            "燃料上限": get_int(fuel, "max_fuel"),
            "民工": counts.get("civ", 0),
            "军工厂": counts.get("mil", 0),
            "船坞": counts.get("dock", 0),
            "研究槽": get_int(node, "research_slot"),
            "陆军经验": round(get_float(node, "army_experience"), 0),
            "海军经验": round(get_float(node, "navy_experience"), 0),
            "空军经验": round(get_float(node, "air_experience"), 0),
            "资源": {
                RESOURCE_NAMES.get(k, k): int(v)
                for k, v in produced.items()
                if isinstance(v, (int, float)) and v
            },
        }

    def focus(self) -> dict:
        """当前国策 + 进度。"""
        node = self.country(self.player_tag)
        block = node.get("focus") if isinstance(node.get("focus"), dict) else {}
        if not block:
            return {}
        out = {
            "当前": get_str(block, "focus") or get_str(block, "id") or "",
            "进度": round(get_float(block, "progress"), 1),
            "完成日": get_str(block, "completion_date"),
        }
        return {k: v for k, v in out.items() if v}

    # ── 部队 ──────────────────────────────────────────

    def divisions(self, tag: str, limit: int = 400) -> list[dict]:
        node = self.country(tag)
        units = node.get("units") if isinstance(node.get("units"), dict) else {}
        out: list[dict] = []
        for div in as_list(units.get("division"))[:limit]:
            if not isinstance(div, dict):
                continue
            loc = get_int(div, "location")
            out.append(
                {
                    "省": loc,
                    "位置": self.province_label(loc) if loc else "未知",
                    "组织度": round(get_float(div, "organisation"), 1),
                    # strength 是存档里的绝对兵力值，不是百分比，别拿它判满编
                    "兵力": round(get_float(div, "strength"), 1),
                    "满编比": _manpower_ratio(div),
                    "在移动": bool(div.get("path") or div.get("move_priority")),
                    # 这个字段其实是补给率（0～100+），旧名"补员率"容易误读，
                    # 保留旧键兼容下游，另给一个准确名字
                    "补员率": round(get_float(div, "army_current_supply_ratio"), 0),
                    "补给率": round(get_float(div, "army_current_supply_ratio"), 0),
                }
            )
        return out

    def army_summary(self, tag: str) -> dict:
        divs = self.divisions(tag)
        by_state: dict[str, int] = {}
        by_prov: dict[int, int] = {}
        moving = 0
        weak = 0
        unknown_ratio = 0
        for d in divs:
            by_state[d["位置"]] = by_state.get(d["位置"], 0) + 1
            pid = d.get("省") or 0
            if pid:
                by_prov[pid] = by_prov.get(pid, 0) + 1
            if d["在移动"]:
                moving += 1
            ratio = d.get("满编比")
            if ratio is None:
                unknown_ratio += 1
            elif ratio < 0.8:
                weak += 1
        top = sorted(by_state.items(), key=lambda kv: -kv[1])[:20]
        # 按省分组是给"点地图调兵"用的：地图上点的是省，不是州。
        # 只留前 60 个，多的要么是散兵要么国家太大，报出来也读不完。
        positions = [
            {"prov": pid, "state": self.province_label(pid), "n": n}
            for pid, n in sorted(by_prov.items(), key=lambda kv: -kv[1])[:60]
        ]
        return {
            "师总数": len(divs),
            "行军中": moving,
            # 语义变了：现在是"满编比 < 80% 的师数"，不是旧版"strength < 60"
            "缺编严重": weak,
            "满编未知": unknown_ratio,
            "分布": dict(top),
            "按省": positions,
        }

    def foreign_divisions_on_soil(self, tag: str) -> dict[str, int]:
        """外国部队站在你国土上，分国家统计。UI 上要翻好几层才看得到。"""
        my_states = {
            str(sid)
            for sid, st in (self.root.get("states") or {}).items()
            if isinstance(st, dict) and st.get("owner") == tag
        }
        prov_map = self.province_to_state()
        result: dict[str, int] = {}
        for other, node in (self.root.get("countries") or {}).items():
            if other == tag or not isinstance(node, dict):
                continue
            units = node.get("units")
            if not isinstance(units, dict):
                continue
            for div in as_list(units.get("division")):
                if not isinstance(div, dict):
                    continue
                sid = prov_map.get(get_int(div, "location"))
                if sid and str(sid) in my_states:
                    result[other] = result.get(other, 0) + 1
        return dict(sorted(result.items(), key=lambda kv: -kv[1]))

    # ── 外交 ──────────────────────────────────────────

    def relations(self, tag: str) -> dict:
        node = self.country(tag)
        dip = node.get("diplomacy") if isinstance(node.get("diplomacy"), dict) else {}
        guaranteed = [str(x) for x in as_list(dip.get("cached_allies_and_gurantees")) if isinstance(x, str)]
        active = dip.get("active_relations") if isinstance(dip.get("active_relations"), dict) else {}
        hostile: dict[str, float] = {}
        for other, rel in active.items():
            if not isinstance(rel, dict):
                continue
            friction = get_float(rel, "border_friction_claim")
            if friction > 0:
                hostile[str(other)] = round(friction, 0)
        hostile = dict(sorted(hostile.items(), key=lambda kv: -kv[1])[:8])
        return {
            "有关系的国家": self.country_label_list(guaranteed[:12]),
            "边境摩擦": {self.country_label(k): v for k, v in hostile.items()},
        }

    def country_label_list(self, tags: list[str]) -> list[str]:
        return [self.country_label(t) for t in tags]

    # ── 局势摘要（给缓存 + 给模型） ───────────────────

    def snapshot(self, include_army: bool = True) -> dict:
        tag = self.player_tag
        stats = self.nation_stats(tag)
        snap: dict[str, Any] = {
            "meta": {
                "save_path": str(self.save_path),
                "save_date": self.date,
                "date_text": format_date(self.date),
                "save_version": self.version,
                "game_dir": str(self.game_dir) if self.game_dir else "",
                "game_version": self.game_version(),
                "player": tag,
                "player_name": self.country_names().get(tag, tag),
                "state_count": len(self.root.get("states") or {}),
                "country_count": len(self.root.get("countries") or {}),
            },
            "version": self.version_check(),
            "player": stats,
            "focus": self.focus(),
            "relations": self.relations(tag),
            "foreign_on_soil": {
                self.country_label(k): v for k, v in self.foreign_divisions_on_soil(tag).items()
            },
        }
        if include_army:
            snap["army"] = self.army_summary(tag)

        # 0.9.13：顺带把「国家→本土省」缓存建好（国名查部队要按国土圈范围）。
        # 首次要解析一遍 history/states（几秒），之后走 country_cores.json，零成本。
        self.country_cores()

        # 大国盘面：只留几个标量，体积可控
        keepers: dict[str, dict] = {}
        countries = self.root.get("countries") or {}
        targets = set(MAJOR_KEEP)
        targets.update(t for t in countries if t == tag)
        for t in list(targets):
            node = countries.get(t)
            if not isinstance(node, dict):
                continue
            counts = self.building_totals(t)
            divs = len(as_list((node.get("units") or {}).get("division")))
            keepers[t] = {
                "名": self.country_names().get(t, t),
                "民工": counts.get("civ", 0),
                "军工厂": counts.get("mil", 0),
                "师": divs,
                "稳定度": round(get_float(node, "stability") * 100, 0),
            }
        snap["countries"] = keepers
        snap["data_quality"] = self.quality_check(snap)
        snap["anomalies"] = self.anomalies(snap)
        return snap

    # ── 数据可信度自检（0.9.8） ───────────────────────

    def quality_check(self, snap: dict) -> dict:
        """解析完先自问一句：这份数真的读全了吗？

        以前只要解析没抛异常就当全对，于是"版本错位导致州名全变 id"、
        "存档被截断只读到 3 个国家"这类问题会一路静默流到回答里。
        这里只做不依赖外部知识的硬检查，检查不出来就明确写 notes。
        """
        countries = len(self.root.get("countries") or {})
        states = len(self.root.get("states") or {})
        positions = (snap.get("army") or {}).get("按省") or []
        named = 0
        for p in positions:
            label = str(p.get("state") or "")
            if label and not label.startswith(("省", "州")):
                named += 1
        coverage = round(named / len(positions), 2) if positions else None
        issues: list[str] = []
        if countries < 50:
            issues.append(f"只读到 {countries} 个国家（正常 300+），存档可能没读全")
        if states < 100:
            issues.append(f"只读到 {states} 个州（正常 900+），存档可能没读全")
        if not re.match(r"^\d{4}\.\d+\.\d+", self.date or ""):
            issues.append(f"存档日期读成了 {self.date!r}，不像正常日期")
        if coverage is not None and coverage < 0.5:
            issues.append(
                f"部队所在省只有 {coverage:.0%} 能对上中文州名，"
                "多半是游戏数据目录和存档版本没对上"
            )
        if not self.game_dir:
            issues.append("没找到游戏数据目录，位置只能报省号")
        return {
            "ok": not issues,
            "countries": countries,
            "states": states,
            "state_name_coverage": coverage,
            "issues": issues,
            "notes": [
                "人力取存档 manpower 的原值、燃料取 fuel_status 的原值，单位按游戏内显示理解",
                "师 strength 是绝对兵力值，不是百分比；满编要用 army_manpower 的两个 value 相除",
                "指挥点上限会随学说/部长/民族精神变化，不要按 100 死算",
            ],
        }

    # ── 异常检测 ──────────────────────────────────────

    def anomalies(self, snap: dict) -> list[dict]:
        """只报"用户在界面上看不到"或"看到也不会当回事"的东西。

        每条都带 level：warn（该管）、info（提一句）、doubt（数据可疑）。
        """
        out: list[dict] = []
        player = snap.get("player") or {}
        tag = self.player_tag
        label = self.country_names().get(tag, tag)

        foreign = snap.get("foreign_on_soil") or {}
        for who, n in foreign.items():
            out.append(
                {
                    "level": "warn",
                    "kind": "foreign_troops",
                    "text": f"{who} 有 {n} 个师待在你的国土上",
                }
            )

        pp = player.get("政治点", 0)
        if pp >= 1500:
            out.append(
                {
                    "level": "warn",
                    "kind": "idle_pp",
                    "text": f"政治点堆到 {pp:.0f}，这个数已经溢出了，等于白攒",
                }
            )
        elif pp >= 800:
            out.append(
                {
                    "level": "info",
                    "kind": "idle_pp",
                    "text": f"政治点攒了 {pp:.0f}，够点两个政策了",
                }
            )

        cp = player.get("指挥点", 0)
        if cp >= 90:
            # 0.9.8：别再说"满值是 100"。实测这份 1.14.8 的档里是 140，
            # 上限会被学说、部长、民族精神抬高，硬编码 100 是把正常值报成异常。
            out.append(
                {
                    "level": "info",
                    "kind": "idle_cp",
                    "text": f"指挥点攒到 {cp:.0f} 了，够放几次司令部技能，别一直放着不花",
                }
            )

        xp = player.get("陆军经验", 0)
        if xp >= 500:
            out.append(
                {
                    "level": "info",
                    "kind": "idle_army_xp",
                    "text": f"陆军经验 {xp:.0f}，够改一次编制或补学说了",
                }
            )

        stab = player.get("稳定度", 100)
        if stab < 40:
            out.append(
                {"level": "warn", "kind": "low_stability", "text": f"稳定度只有 {stab:.0f}，很危险"}
            )
        ws = player.get("战争支持", 100)
        if ws < 30:
            out.append(
                {"level": "warn", "kind": "low_war_support", "text": f"战争支持只有 {ws:.0f}，动员拉不动"}
            )

        manpower = player.get("人力", 0)
        if 0 < manpower < 20000:
            out.append(
                {
                    "level": "warn",
                    "kind": "low_manpower",
                    "text": f"可用人力只剩 {manpower}，再打大仗就补不上兵了",
                }
            )

        army = snap.get("army") or {}
        for state, n in (army.get("分布") or {}).items():
            if n >= 8:
                out.append(
                    {
                        "level": "info",
                        "kind": "stacked_divisions",
                        "text": f"{state} 堆了 {n} 个师，留意补给跟不跟得上",
                    }
                )

        # 数据可信度：版本错位时主动降级结论
        ver = snap.get("version") or {}
        if ver.get("warn"):
            out.append({"level": "doubt", "kind": "version_mismatch", "text": ver["warn"]})

        # 0.9.8：解析自检发现的问题也报出来，别让"读到一半的档"装成全对
        quality = snap.get("data_quality") or {}
        for text in quality.get("issues") or []:
            out.append({"level": "doubt", "kind": "data_quality", "text": f"数据可疑：{text}"})
        if army.get("满编未知"):
            out.append(
                {
                    "level": "doubt",
                    "kind": "unknown_full_strength",
                    "text": f"有 {army['满编未知']} 个师没读到满编数据，缺编数只算读得到的那些",
                }
            )

        # 工厂数离谱时提示可能是 mod / 控制台 / 版本错位
        dock = player.get("船坞", 0)
        mil = player.get("军工厂", 0)
        if mil and dock > mil * 2 and dock > 30:
            out.append(
                {
                    "level": "doubt",
                    "kind": "odd_dockyards",
                    "text": f"船坞 {dock} 个、军工厂才 {mil} 个，这个比例不正常，可能是 mod 或版本错位",
                }
            )

        # 该管的排前面，数据可疑的排中间，提一句的放最后
        rank = {"warn": 0, "doubt": 1, "info": 2}
        out.sort(key=lambda a: rank.get(str(a.get("level")), 3))
        return out


def load_state(save_path: str | Path, game_dir: str | Path | None,
               cache_dir: str | Path | None = None) -> SaveState:
    return SaveState(save_path, game_dir, cache_dir)

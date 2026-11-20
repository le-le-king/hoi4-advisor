"""接管一支部队并指挥它（0.9.15）。

## 两层数据怎么接起来的

- **编组（谁属于哪个集团军）只在存档里**：`countries.<TAG>.theatres.theatre[]`
  → `orders_group[]`（集团军）→ `member[].unit.id`（师 id）。内存里没逆出
  `CArmy → ArmyGroup` 的指针链（0.9.9 试过，读出来是空的）。
- **位置和实时状态只在内存里**：`CArmy + 0x24` 就是同一个师 id
  （0.9.15 真机确认：228/228 命中、无重复、位置一致率 100%）。

所以认领一个集团军 = 拿存档里的师 id 列表，去内存按 id 换地址。编组变化很慢、
师 id 稳定，存档旧几分钟不影响。**这是唯一被验证过的可靠接法，不猜。**

## 安全边界（写死在代码里）

1. 只指挥**玩家自己国家**的师（地址来自玩家国家的师列表，绝不碰别国）。
2. 指挥权必须**显式接管**，随时可交回；插件重启/换局自动失效。
3. 每次动兵前记下原位置，`release(restore=True)` 可以把师调回去。
4. 玩家手动动过的师**立刻让出**（冲突检测），不再覆盖他的命令。
"""

from __future__ import annotations

import json
import struct
import time
from pathlib import Path

__all__ = ["CommandError", "Commander", "ARMY_ALIASES"]

# 集团军名字的宽松匹配：用户说「第9集团军」「9军」「第九集团军」都要认
ARMY_ALIASES = ("集团军", "军")

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_CN_ORD = str.maketrans({"〇": "0", "一": "1", "二": "2", "三": "3", "四": "4",
                         "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"})


# 集团军作战计划对象（COrdersGroup）——0.9.16 真机逆向确认
#   CArmy + 0xA0 -> COrdersGroup*（同集团军全部师共享、不同集团军互不相同：
#                   20/20 一致、20 个指针、34 个未编组师为 0 已验）
#   COrdersGroup + 0x040 motorization_level（20/20 与存档全等）
#   COrdersGroup + 0x048 CTheatre*（战区对象，45 条前线的 owner 都是它）
#   COrdersGroup + 0x060/+0x064 成员数组 cap/size（size=师数，20/20 全等）
#   COrdersGroup + 0x140 名字（内联 UTF-8，如「第9集团军」）
#   COrdersGroup + 0x170 execution_type（20/20 与存档全等，0=待命）
#   COrdersGroup + 0x174 cohesion_type（20/20 全等）
# 全部只读使用；**不写**（写未验证的结构可能坏档）。
OFF_GROUP_PTR = 0xA0
OFF_GROUP_MOTOR = 0x40
OFF_GROUP_THEATRE = 0x48
OFF_GROUP_CAP = 0x60
OFF_GROUP_SIZE = 0x64
OFF_GROUP_NAME = 0x140
OFF_GROUP_EXEC = 0x170
OFF_GROUP_COHESION = 0x174

# execution_type 的已知取值（存档里实际出现过）
EXEC_NAMES = {0: "待命", 1: "执行中", 2: "执行中", 4: "执行中"}


class CommandError(RuntimeError):
    def __init__(self, code: str, msg: str, hint: str = ""):
        super().__init__(msg)
        self.code = code
        self.msg = msg
        self.hint = hint


def _norm(text: str) -> str:
    """「第九集团军」→「9集团军」；去掉空格和标点，留下数字+集团军/军。"""
    s = (text or "").strip()
    for sep in (" ", "　", "，", ",", "。", "的"):
        s = s.replace(sep, "")
    out = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "十":
            # 十 / 十三 / 二十 / 二十三
            if i + 1 < len(s) and s[i + 1] in _CN_DIGITS and s[i + 1] != "十":
                out.append(str(10 + _CN_DIGITS[s[i + 1]]))
                i += 2
                continue
            out.append("10")
        elif ch in _CN_DIGITS:
            out.append(str(_CN_DIGITS[ch]))
        else:
            out.append(ch)
        i += 1
    return "".join(out).translate(_CN_ORD)


def _army_key(name: str) -> str:
    """从「第9集团军」里抽「9」；抽不出就返回归一化全名。"""
    s = _norm(name)
    for suffix in ("集团军群", "集团军", "军"):
        if s.endswith(suffix):
            core = s[: -len(suffix)]
            if core.startswith("第"):
                core = core[1:]
            return core or s
    return s


class Commander:
    """指挥权状态 + 集团军名册 + 目标队列。状态落盘，重启不丢「谁在指挥谁」。"""

    def __init__(self, live, data_dir: Path, log=None) -> None:
        self._live = live
        self._dir = Path(data_dir)
        self._state_path = self._dir / "command_state.json"
        self._log = log or (lambda msg: None)
        self._armies_memo: tuple[float, dict] | None = None

    # ── 名册（来自存档编组缓存） ──────────────────

    def armies(self, max_age: float = 900.0) -> dict[str, dict]:
        """集团军名 → {army_id, ids:[师id], count}。

        数据源是 watcher 解析存档时写的快照缓存 `state_cache.json` 里的
        `oob` 段（0.9.9 起就有，零额外解析成本）。没有缓存就返回 {}，
        调用方必须如实说「还没解析过存档，认不出集团军」，**绝不猜一个**。
        """
        import time as _t
        if self._armies_memo and _t.time() - self._armies_memo[0] < max_age:
            return self._armies_memo[1]
        raw: dict = {}
        cache_file = self._dir / "cache" / "state_cache.json"
        if not cache_file.is_file():
            cache_file = self._dir / "state_cache.json"
        try:
            whole = json.loads(cache_file.read_text(encoding="utf-8"))
            raw = whole.get("oob") or {}
        except Exception:  # noqa: BLE001 缓存坏了就当没有，别炸
            return {}
        if not raw.get("ok"):
            return {}

        table: dict[str, dict] = {}
        # 集团军群下辖的集团军 + 直属集团军（第一版只取第一个群，丢了 194 个师）
        rows = [a for g in (raw.get("army_groups") or []) for a in g.get("armies") or []]
        rows += list(raw.get("direct_armies") or [])
        for a in rows:
            ids = [int(d["id"]) for d in (a.get("divisions") or []) if d.get("id")]
            if not ids:
                continue
            table[str(a.get("name") or f"集团军#{a.get('id')}")] = {
                "army_id": a.get("id"),
                "ids": ids,
                "count": len(ids),
            }
        if table:
            self._armies_memo = (_t.time(), table)
            return table
        return {}

    def find_army(self, text: str) -> tuple[str, dict] | None:
        """按用户说法找集团军。先精确，再按编号唯一匹配；多个候选返回 None。"""
        table = self.armies()
        if not table:
            return None
        want = _norm(text)
        # 1) 全名精确
        for name, info in table.items():
            if _norm(name) == want:
                return name, info
        # 2) 编号匹配（「9」「第9」「9军」「第九集团军」）
        key = _army_key(text)
        if key:
            hits = [(n, i) for n, i in table.items() if _army_key(n) == key]
            if len(hits) == 1:
                return hits[0]
            # 3) 唯一包含
            hits = [(n, i) for n, i in table.items() if key in _army_key(n)]
            if len(hits) == 1:
                return hits[0]
        return None

    def candidates(self, limit: int = 12) -> list[str]:
        return sorted(self.armies().keys())[:limit]

    # ── 指挥权状态 ────────────────────────────

    def _read_state(self) -> dict:
        if not self._state_path.is_file():
            return {}
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _write_state(self, st: dict) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(self._state_path)
        except OSError as exc:
            self._log(f"指挥状态写不进去：{exc}")

    def state(self) -> dict:
        """当前指挥权。换了局（pid/base 变了）自动作废。"""
        st = self._read_state()
        if not st:
            return {}
        try:
            pid, base = int(st.get("pid") or 0), int(st.get("base") or 0)
            ag = self._live.connect()
            try:
                if ag.pid != pid or ag.base != base:
                    self._write_state({})
                    return {}
            finally:
                ag.close()
        except Exception:  # noqa: BLE001 游戏没开：状态留着，但标记离线
            return st
        return st

    def claim(self, army_name: str, player_addr: int) -> dict:
        """接管一个集团军。返回接管到的师（含现在位置）。"""
        found = self.find_army(army_name)
        if found is None:
            raise CommandError(
                "no_army",
                f"没找到「{army_name}」这个集团军。",
                "把候选念给用户：" + "、".join(self.candidates()) if self.candidates()
                else "存档编组还没解析出来，等一下再试，或者直接说「所有师」。",
            )
        name, info = found
        ids = list(info["ids"])
        units = self._live.units_by_id(player_addr, ids)
        if not units:
            raise CommandError(
                "no_units",
                f"「{name}」名册上有 {len(ids)} 个师，但内存里一个都没对上——"
                "多半是内存偏移变了或这局还没读到部队。",
                "先让用户确认游戏在地图界面，再试一次；**绝对不要**瞎调兵。",
            )
        ag = self._live.connect()
        try:
            st = {
                "pid": ag.pid, "base": ag.base,
                "army": name, "army_id": info.get("army_id"),
                "ids": [u["uid"] for u in units],
                "named_count": len(ids),
                "since": time.time(),
                # 接管那一刻的位置：交回时用得上，也用来做冲突检测的基线
                "origin": {str(u["uid"]): int(u["prov"]) for u in units},
                "plan": {"front": [], "targets": [], "holds": []},
            }
        finally:
            ag.close()
        self._write_state(st)
        return {"army": name, "units": units, "state": st}

    def plan_state(self, player_addr: int) -> dict:
        """读接管部队的**集团军作战计划状态**（纯只读）。

        0.9.16 逆出来的链路：`CArmy+0xA0 → COrdersGroup`，组内
        `+0x170 execution_type`（0=待命，其余=正在执行某个计划）、
        `+0x174 cohesion_type`、`+0x040 motorization_level`、
        `+0x060/0x064` 成员容量与实际师数、`+0x140` 内联番号。
        这几项都用 20 个集团军与存档逐字段全等验证过（见 §10.23）。
        """
        st = self._read_state()
        if not st:
            return {}
        out = {"army": st.get("army")}
        try:
            obs = self.observe(player_addr)
            units = obs["units"]
            out["units"] = len(units)
            if not units:
                return out
            ag = self._live.connect()
            try:
                addr = int(units[0]["addr"])
                g = struct.unpack("<Q", ag.read(addr + OFF_GROUP_PTR, 8))[0]
                if not g:
                    out["group"] = 0
                    out["note"] = "这支部队不在任何集团军里"
                    return out
                out["group"] = g
                out["exec"] = struct.unpack("<i", ag.read(g + OFF_GROUP_EXEC, 4))[0]
                out["exec_name"] = EXEC_NAMES.get(out["exec"], f"类型{out['exec']}")
                out["cohesion"] = struct.unpack("<i", ag.read(g + OFF_GROUP_COHESION, 4))[0]
                out["motor"] = struct.unpack("<i", ag.read(g + OFF_GROUP_MOTOR, 4))[0]
                out["cap"] = struct.unpack("<i", ag.read(g + OFF_GROUP_CAP, 4))[0]
                out["size"] = struct.unpack("<i", ag.read(g + OFF_GROUP_SIZE, 4))[0]
                out["theatre"] = struct.unpack("<Q", ag.read(g + OFF_GROUP_THEATRE, 8))[0]
                raw = ag.read(g + OFF_GROUP_NAME, 64)
                nm = raw.split(b"\x00")[0]
                try:
                    out["name"] = nm.decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    out["name"] = ""
            finally:
                ag.close()
        except Exception as exc:  # noqa: BLE001 读计划状态失败不能影响指挥
            out["error"] = f"{type(exc).__name__}: {exc}"
        return out

    def release(self, restore: bool = False, player_addr: int = 0) -> dict:
        """交回指挥权。restore=True 时把师调回接管时记下的原省。"""
        st = self._read_state()
        if not st:
            return {"released": False, "reason": "现在没有接管任何部队"}
        out = {"released": True, "army": st.get("army"), "restored": 0, "failed": 0}
        if restore and player_addr:
            origin = st.get("origin") or {}
            units = self._live.units_by_id(player_addr, [int(k) for k in origin])
            by_id = {u["uid"]: u for u in units}
            for uid_s, prov in origin.items():
                u = by_id.get(int(uid_s))
                if u is None:
                    out["failed"] += 1
                    continue
                if int(u["prov"]) == int(prov):
                    continue
                try:
                    self._live.move_many(player_addr, [u["addr"]], int(prov),
                                         replace=True)
                    out["restored"] += 1
                except Exception:  # noqa: BLE001
                    out["failed"] += 1
        self._write_state({})
        return out

    # ── 目标 / 计划 ───────────────────────────

    def save_plan(self, plan: dict) -> dict:
        st = self._read_state()
        if not st:
            raise CommandError("not_claimed", "现在没接管部队，没法下计划。")
        st["plan"] = plan
        st["plan_at"] = time.time()
        self._write_state(st)
        return st

    def plan(self) -> dict:
        return (self._read_state().get("plan") or {})

    # ── 观察：接管部队现在什么样 ──────────────────

    def observe(self, player_addr: int, player_short: str = "") -> dict:
        """读接管部队的实时状态 + 分布表 + 邻接。只读，0.3 秒级。"""
        st = self._read_state()
        if not st:
            raise CommandError("not_claimed", "现在没接管任何部队。")
        ids = [int(i) for i in st.get("ids") or []]
        if not ids:
            raise CommandError("not_claimed", "接管状态里没有师，重新接管一次。")
        units = self._live.units_by_id(player_addr, ids)
        age, dist = self._live.peek_dist(max_age=300)
        if not dist:
            # 0.9.16：分布表冷的时候现扫一次兜底。不这么做的话，玩家问
            # 「能打哪」会得到一个错的答案——"周围没有口子"（其实是没数据）。
            # 哨兵平时每 90 秒养一次表，这条路只在刚开局/哨兵卡住时走。
            try:
                dist = self._live.units_by_prov(ttl=120)
                age = 0.0
            except Exception as exc:  # noqa: BLE001 扫不动就照实说没数据
                dist = {}
                age = -1.0
                self._log(f"分布表现扫失败：{type(exc).__name__}: {exc}")
        rows = self._live.peek_named(max_age=900)
        addr_short = {int(r["addr"]): str(r["short"])
                      for r in rows if r.get("addr") and r.get("short")}
        return {
            "state": st, "units": units, "dist": dist,
            "addr_short": addr_short, "dist_age": age,
            "adj": self._live.adjacency(),
        }

    def assess(self, player_addr: int, player_short: str = "",
               want: int = 5, enemies: set[str] | None = None,
               allies: set[str] | None = None) -> dict:
        """找弱点：接管部队的邻省里，谁在守、值不值得推。**只读，不下命令。**

        `enemies` / `allies` 是 warscope 认出来的交战双方短名集合。
        0.9.16 起**不再拿"有没有敌对国名单"当硬闸**：HOI4 引擎层面不允许对
        盟友开火（也不需要宣战），所以"师开进盟友省"最多是驻防，不会造成
        误伤。名单只用来**标注**这一处是敌方还是友方，不决定能不能推。
        """
        obs = self.observe(player_addr, player_short)
        units, dist, adj = obs["units"], obs["dist"], obs["adj"]
        addr_short = obs["addr_short"]
        mine_provs = {int(u["prov"]) for u in units}
        short_of = lambda a: addr_short.get(int(a), "")
        is_mine = lambda a: player_short and short_of(a) == player_short
        hostile = {str(e) for e in (enemies or ()) if e}
        friend = {str(a) for a in (allies or ()) if a}

        # 我方在每省的师数
        mine_at: dict[int, int] = {}
        for u in units:
            mine_at[int(u["prov"])] = mine_at.get(int(u["prov"]), 0) + 1

        # 前沿：邻接我方师的省，里面有别人家的兵
        frontier: dict[int, dict] = {}
        for p in mine_provs:
            for n in adj.get(p, ()) or ():
                n = int(n)
                if n in mine_provs:
                    continue
                slot = dist.get(n) or {}
                foreign = {a: c for a, c in slot.items() if not is_mine(a)}
                if not foreign:
                    continue
                row = frontier.setdefault(n, {"foreign": {}, "near": 0,
                                              "allies": {}})
                for a, c in foreign.items():
                    nm = short_of(a) or f"国家@{int(a):#x}"
                    # 盟友只记一笔，不当作进攻目标（引擎也不会让你打）
                    if friend and nm in friend and nm not in hostile:
                        row["allies"][nm] = row["allies"].get(nm, 0) + c
                        continue
                    row["foreign"][nm] = row["foreign"].get(nm, 0) + c
                # 我方在 n 的邻省有几个师（能马上压上去的）
                near = 0
                for nn in adj.get(n, ()) or ():
                    near += mine_at.get(int(nn), 0)
                row["near"] = max(row["near"], near)

        scored = []
        for p, row in frontier.items():
            enemies_n = sum(row["foreign"].values())
            if not enemies_n:
                continue          # 只有盟友守着，不算目标
            # 弱点分：我方压上去的师 - 2×守军。守军少、我能压的师多 → 分高
            score = row["near"] - 2 * enemies_n
            scored.append({
                "prov": p, "score": score, "enemies": enemies_n,
                "near": row["near"], "who": row["foreign"],
                "allies_near": row["allies"],
            })
        scored.sort(key=lambda r: (-r["score"], r["enemies"]))
        return {
            "army": obs["state"].get("army"),
            "units": len(units),
            "provs": sorted(mine_provs),
            "dist_age": obs["dist_age"],
            "targets": scored[:want],
            "frontier": len(scored),
            # 0.9.16：有目标就能推；敌对名单只影响"打谁"的标注
            "auto_ok": bool(scored),
            "enemies": sorted(hostile),
            "allies": sorted(friend),
        }

    # ── 行动 ─────────────────────────────────

    def brief(self, player_addr: int, player_short: str = "") -> dict:
        """战况简报：**只陈述事实**——不给分数、不排序、不建议（M4' 铁律）。

        为什么要这么小心：0.9.17 之前这里有个 `score = near - 2*enemies`，
        那是代码在替她做战术判断。用户要她自己学打仗，所以这一层只许输出
        "哪里有什么、我手上能调多少、目标够不够得着"，判断留给她。
        `front` 按省号升序排列（客观顺序，不代表优先级）。
        """
        obs = self.observe(player_addr, player_short)
        units, dist, adj = obs["units"], obs["dist"], obs["adj"]
        addr_short = obs["addr_short"]
        # 0.9.18 修：判断"这是不是我方"必须按**国籍**，不能按"在不在接管
        # 名单里"——接管名单只有这一支部队的师，本国别处的师会被错算成敌军
        # （真机出现过"德国 42 个师"当敌人的乌龙）。用 addr_short 这张
        # "地址 → 国家短名"表，跟 assess 一直是同一套判据。
        # 0.9.22 修：player_addr 无条件算自己人——addr_short 是 900 秒 TTL 的
        # 缓存，冷的时候是空的，那时"不是我的全算敌人"会把每个有兵的省都判成
        # 交火，`free` 恒空，命令永远下不出去。player_addr 是调用方给的，稳。
        mine_all = {int(player_addr)}
        mine_all |= {int(a) for a, s in addr_short.items()
                     if player_short and s == player_short}
        enemy_names = self._live.enemy_names()
        mine = {int(u["uid"]) for u in units}

        pos: dict[int, int] = {}
        for u in units:
            pos[int(u["prov"])] = pos.get(int(u["prov"]), 0) + 1
        my_provs = set(pos)
        # 正在交火的省：本省有**敌国**的兵。盟友/仆从同驻不算（1939 德国+斯洛伐克）
        fighting = {p for p in my_provs
                    if any(enemy_names and addr_short.get(int(a)) in enemy_names
                           for a in (dist.get(p) or {}))}

        def reach(t: int) -> int:
            """从我的邻省能压到 t 的师数（按国籍算，含没接管的友邻部队）。"""
            n = 0
            for nb in adj.get(t) or ():
                for a, c in (dist.get(int(nb)) or {}).items():
                    if int(a) in mine_all:
                        n += c
            return n

        front = []
        for p in my_provs:
            for nb in adj.get(p) or ():
                slot = dist.get(int(nb)) or {}
                if any(int(a) not in mine_all for a in slot):
                    front.append(p)
                    break
        front.sort()
        rows = []
        for t in front:
            enemy: dict[str, int] = {}
            for nb in adj.get(t) or ():
                for a, c in (dist.get(int(nb)) or {}).items():
                    a = int(a)
                    if a in mine_all:
                        continue
                    nm = addr_short.get(a) or f"国家@{a:#x}"
                    enemy[nm] = enemy.get(nm, 0) + c
            # 本省里如果有别人家的兵，那是正在打，也要报
            for a, c in (dist.get(t) or {}).items():
                a = int(a)
                if a in mine_all:
                    continue
                nm = addr_short.get(a) or f"国家@{a:#x}"
                enemy[nm] = enemy.get(nm, 0) + c
            rows.append({
                "prov": t,
                "mine_here": pos.get(t, 0),
                "enemy": enemy,
                "enemy_total": sum(enemy.values()),
                "reach": reach(t),
                "at_war_here": t in fighting,
            })

        st = self._read_state() or {}
        out = {
            "army": st.get("army"),
            "units": len(units),
            "at": time.time(),
            "pos": dict(sorted(pos.items())),
            "front": rows,
            "rear": {p: n for p, n in sorted(pos.items()) if p not in set(front)},
            "front_provs": front,
            "no_data": not dist,
            # M4c：上几道令现在怎么样了（她看不见这个就等于永远在盲下命令）
            "aftermath": self.aftermath(player_addr, player_short, obs=obs),
            # 明确留空：这一层永远不填"建议"
            "advice": None,
        }
        try:
            ps = self.plan_state(player_addr)
            out["exec"] = ps.get("exec")
            out["exec_name"] = ps.get("exec_name")
        except Exception:  # noqa: BLE001 计划状态读不到不影响简报
            out["exec"] = None
        return out

    def aftermath(self, player_addr: int, player_short: str = "",
                  limit: int = 6, obs: dict | None = None) -> list[dict]:
        """复盘：**她之前下的令，现在怎么样了**（M4c）。

        每条令都记着"哪些师、去哪个省、什么时候下的"。这里回去看这些师此刻
        在哪：到了 / 还在路上 / 被别的事调走了 / 找不到了（被打散）。
        只陈述结果，不打分、不说"你错了"——她要自己从结果里学，这也是
        "把脑子还给她"的一部分：不给结论，给她看得见的因果。
        """
        st = self._read_state() or {}
        issued = list((st.get("plan") or {}).get("issued") or [])[-limit:]
        if not issued:
            return []
        if obs is None:
            obs = self.observe(player_addr)
        by_addr = {int(u["addr"]): u for u in obs["units"]}
        now = time.time()
        out = []
        for rec in issued:
            dst = int(rec["dst"])
            addrs = [int(a) for a in rec.get("addrs") or []]
            arrived = moving = elsewhere = gone = 0
            where: dict[int, int] = {}
            for a in addrs:
                u = by_addr.get(a)
                if not u:
                    gone += 1          # 师没了：被打散、被歼灭，或已经改编制
                    continue
                p = int(u["prov"])
                t = int(u["target"] or 0)
                if p == dst:
                    arrived += 1       # 到了
                elif t == dst:
                    moving += 1        # 还在路上
                else:
                    elsewhere += 1     # 被调走了（玩家手动 / 被别的令顶掉）
                    where[p] = where.get(p, 0) + 1
            at = float(rec.get("at") or 0)
            out.append({
                "dst": dst,
                "n": len(addrs),
                "at": at,
                "ago": max(0.0, now - at) if at else 0.0,
                "arrived": arrived,
                "moving": moving,
                "elsewhere": elsewhere,
                "gone": gone,
                "where": dict(sorted(where.items())),
            })
        return out

    def hold(self, player_addr: int, line_provs: list[int],
             max_units: int = 3, dry_run: bool = True,
             player_short: str = "") -> dict:
        """守她点名的那条线（M4'）：**守哪些省她定，怎么摆按引擎规则**。

        `line_provs` 是她从简报里挑的省号。代码不替她挑、不替她判断该不该
        守——只检查"够不够得着"，然后按 `_quotas` 的引擎规则把兵摆过去。
        """
        if not line_provs:
            raise CommandError("bad_input", "要守的那条线一个省都没有。")
        return self._fill(player_addr, player_short, [int(p) for p in line_provs],
                          max_units=max_units, dry_run=dry_run, what="守线")

    def front(self, player_addr: int) -> dict:
        """前线省列表（我方控制且挨着交战国）。返回 `{"provs": [...], "why": ...}`。

        **不抢全服扫描**：控制图由版图哨兵每 ~85 秒在后台养一次，这里只读缓存。
        直接扫一次要 14~19 秒，还会跟哨兵抢 DLL 的全局命令锁，一撞就超时
        （`str(TimeoutError)` 是空串，用户只会看到一片空白）。

        `why` 把"算不出来"的两种原因分开——0.9.23 之前只会笼统说"还没认出跟谁在打"，
        真机上其实是**控制图被工具查询挤掉了**，说错了原因会让人往错的方向查：

        - `no_enemy`：warscope 还没认出交战国（先读一次前线就能解决）
        - `map_cold`：控制图正在被后台重扫，这一刻读不到（等十几秒）
        - `""`：正常
        """
        ag = self._live.connect()
        try:
            out = self._live.front_provinces(ag, player_addr, allow_scan=False)
            if out:
                return {"provs": out, "why": ""}
            if not self._live.enemy_names():
                try:
                    self._live.frontline(player_addr)
                except Exception as exc:  # noqa: BLE001 认不出来就如实报空
                    self._log(f"读前线失败（为了认交战国）："
                              f"{type(exc).__name__}: {exc}")
                out = self._live.front_provinces(ag, player_addr,
                                                 allow_scan=False)
                if out:
                    return {"provs": out, "why": ""}
                if not self._live.enemy_names():
                    return {"provs": [], "why": "no_enemy"}
            # 交战国已经认出来了，那算不出来只能是控制图这一会儿是空的
            return {"provs": [], "why": "map_cold"}
        finally:
            ag.close()

    def advance(self, player_addr: int, target: int, max_units: int = 0,
                dry_run: bool = False, player_short: str = "") -> dict:
        """**推一格**：让与 target 相邻的我方部队进攻 target。

        为什么要有这个原语（0.9.23）：以前只有 `hold`（摆防线）和 `move`（调到某省），
        没有"进攻"这个动作。于是模型把进攻实现成"把师调到敌方远处的省"，
        四个毛病全从这一个缺口派生：

        - 只有"没在守线"的师才敢动（守线的师已经挂在线上，再 move 出去线就空了）；
        - 一进攻就把线抽空，因为它的动法就是 move；
        - 目标选得很远（省 362、省 3295），因为没有一个"旁边那一格"的概念；
        - 单个师被扔到纵深，后路没人接应。

        这里的语义就是人在地图上做的那一下：**从相邻的我方省，往那一格打**。
        - 只动与 target 相邻、且站在那儿的我方师；别处的守线部队**一个都不动**；
        - 目标必须与我方相邻（隔一格就打不了，得先推进）；
        - 报告里同时列出"相邻我方省里哪几个没人"——那是防线的空档，事实给她，要不要补她自己定。
        """
        st = self._read_state()
        if not st:
            return {"active": False}
        ids = {int(i) for i in st.get("ids") or []}
        if not ids:
            raise CommandError("not_claimed", "现在没接管任何部队。")

        target = int(target)
        obs = self.observe(player_addr, player_short)
        units, adj = obs["units"], obs["adj"]
        mine = [u for u in units if int(u["uid"]) in ids]

        neighbours = set(adj.get(target) or ())
        if not neighbours:
            raise CommandError("bad_input",
                               f"省 {target} 一个陆地邻省都没有，不是能打的地方。")

        # 出发省：既要是 target 的邻省，又得真有我们接管的师站在那儿。
        from_provs = sorted({int(u["prov"]) for u in mine
                             if int(u["prov"]) in neighbours})
        if not from_provs:
            where = sorted({int(u["prov"]) for u in mine})
            raise CommandError(
                "too_far",
                f"接管的师没有一个挨着省 {target}，这一格现在打不了"
                f"（我们的师在 {where[:12]}）。要打得先推进到它旁边。",
                "先把部队往前推一格到 target 的邻省，下一轮再打 target。")

        attackers = [u for u in mine if int(u["prov"]) in set(from_provs)]
        # **每个出发省留 1 个师守家**（0.9.23）：一开始这里是把该省的师全派走，
        # 于是 6590 那 12 个全扑上去、那一格空了——又变成"进攻把线抽空"。
        # 按引擎的守线规则（每省保底一个），出发省至少留 1。
        by_prov: dict[int, list] = {}
        for u in attackers:
            by_prov.setdefault(int(u["prov"]), []).append(u)
        keep: list[int] = []
        send: list = []
        for p, us in sorted(by_prov.items()):
            if len(us) <= 1:
                send.extend(us)          # 就一个师，没有可留的
                continue
            send.extend(us[:-1])         # 留最后一个在家
            keep.append(us[-1]["uid"])
        attackers = send
        # 相邻但我方没人驻守的省：防线空档，如实报给她
        idle_src = sorted(neighbours - set(from_provs))

        if max_units and len(attackers) > max_units:
            attackers = attackers[:max_units]

        rows = [{"dst": target,
                 "addrs": [u["addr"] for u in attackers]}]
        out = {"active": True, "army": st.get("army"), "target": target,
               "from_provs": from_provs, "attackers": len(attackers),
               "kept_home": keep, "idle_neighbours": idle_src,
               "moved": 0, "dry_run": dry_run}
        if dry_run:
            out["note"] = (f"要从 {len(from_provs)} 个邻省出 {len(attackers)} 个师推省 "
                           f"{target}，出发省各留 {len(keep)} 个守家（试运行，没动手）")
            return out

        r = self._apply(player_addr, rows, f"推省 {target}")
        out["moved"] = r["done"]
        out["failed"] = r["failed"]
        out["note"] = r["note"]
        if r.get("reasons"):
            out["reasons"] = r["reasons"]

        nid = [p for p in idle_src]
        if nid:
            out["note"] += (f"；顺带说一句：挨着 {target} 的我方邻省里还有 "
                            f"{len(nid)} 个没有我们的师（{nid[:8]}），"
                            f"那些口子要不要补你自己定。")
        return out

    def _apply(self, player_addr: int, rows: list[dict],
               what: str) -> dict:
        """真正下命令。逐省一组，记录下达目标用于下一轮冲突检测。"""
        done, failed = 0, 0
        issued = []
        reasons: list[str] = []
        unchecked = 0
        for row in rows:
            dst = int(row["dst"])
            addrs = [int(a) for a in row["addrs"]]
            if not addrs:
                continue
            try:
                res = self._live.move_many(player_addr, addrs, dst, replace=True)
                part = res.get("results") or []
                ok = sum(1 for r in part if r.get("ok"))
                done += ok
                failed += len(addrs) - ok
                if any(r.get("checked") is False for r in part):
                    unchecked += 1
                for r in part:
                    if not r.get("ok") and r.get("error"):
                        msg = str(r["error"])
                        if msg not in reasons:
                            reasons.append(msg)
                if ok:
                    # at 是复盘用的：下一份简报要能说"上次你让 3 个师去 1889，
                    # 那是 10:12 的事"。没有时间戳就分不清"刚到"和"几天前下的令"。
                    issued.append({"dst": dst, "addrs": addrs, "at": time.time()})
            except Exception as exc:  # noqa: BLE001 一单失败不能拖垮整轮
                failed += len(addrs)
                self._log(f"{what}：调到 {dst} 失败 {type(exc).__name__}: {exc}")
                reasons.append(f"调到 {dst} 出错：{type(exc).__name__}: {exc}")
        st = self._read_state()
        plan = st.get("plan") or {}
        # 0.9.17：issued 要累积——守线和进攻可能在同一轮各下一单，
        # 后一单不能把前一单的记录冲掉（冲掉就等于那批师没人核对）。
        prev = list(plan.get("issued") or [])
        plan["issued"] = (prev + issued)[-80:]
        plan["at"] = time.time()
        st["plan"] = plan
        self._write_state(st)

        note = f"{what}：下达 {done} 个师，失败 {failed} 个"
        if reasons:
            # 0.9.22：失败原因要浮上来。只报"失败 N 个"等于把
            # "要穿过波兰但你还没宣战"这种可操作的信息吞掉了。
            note += "。原因：" + "；".join(reasons[:3])
        if unchecked:
            note += (f"（另有 {unchecked} 处没读到国家归属，路线没做通行权检查——"
                     f"走没走到以游戏里为准）")
        return {"ok": True, "dry_run": False, "done": done, "failed": failed,
                "reasons": reasons, "rows": issued, "note": note}

    # ── 摊兵：照引擎的摆法 ────────────────────

    @staticmethod
    def _quotas(n_units: int, front: list[int], adj: dict,
                dist: dict, mine: set) -> dict[int, int]:
        """每省该放几个师 —— **照引擎的行为规则，不是拟合某个快照**。

        0.9.17 真机采样得到两条硬事实：
          ① 第12集团军 10 省 10 师，**每省正好 1 个**——不管该省本省敌师是
             1 个还是 7 个。"每省保底 1 个、不许空防"是引擎的底线。
          ② 第5集团军 22 师，12809 一省堆了 17 个（本省敌 20，与 5075 并列
             最高）——富余兵力往**最激烈的战线**堆，不是平均摊。
        但实际分布还掺了战斗宽度排队、补给、玩家手动这些历史因素（第9集团军
        1986 只留 2 个、7982 堆 15 个，无法用任何一次性公式反推），所以这里
        实现的是**可解释的反应式规则**，而不是去拟合快照：
          1) 每个接敌省保底 1 个（师不够时按"本省敌师多"的省优先）；
          2) 富余的师优先补"打这一省还差几个"（本省敌师数 +1 = 够打的量）；
          3) 都打够了还有富余 → 全堆给本省敌师最多的省（最激烈战线）；
          4) 绝不从已经满足配额的省抽人（避免来回搬兵）。
        """
        if not front or n_units <= 0:
            return {}
        enemy_of: dict[int, int] = {}
        for p in front:
            own = 0
            for a, c in (dist.get(p) or {}).items():
                if int(a) not in mine:
                    own += c
            enemy_of[p] = own
        order = sorted(front, key=lambda p: (-enemy_of[p], p))

        q = {p: 0 for p in front}
        left = n_units
        # 1) 保底：每省 1 个，本省敌师多的省先拿
        for p in order:
            if left <= 0:
                break
            q[p] = 1
            left -= 1
        # 2) 富余：补"够打"的缺口，其次堆给最激烈战线
        while left > 0:
            best_p = None
            best_demand = 0
            for p in front:
                demand = max(0, enemy_of[p] + 1 - q[p])
                if demand > best_demand:
                    best_demand = demand
                    best_p = p
            if best_p is None:      # 每省都够打了，剩下全给最激烈的那条线
                best_p = max(front, key=lambda x: (enemy_of[x], -x))
            q[best_p] += 1
            left -= 1
        return q

    def _fill(self, player_addr: int, player_short: str = "",
              front_provs: list[int] | None = None,
              max_units: int = 3, dry_run: bool = False,
              what: str = "守线") -> dict:
        """把兵补到这条线上 —— **只执行，不做决策**（M4'）。

        `front_provs` 是谁给的，决定这是谁的决定：
        - 给了 → **YUI/玩家点名要守的那几个省**，原样用，代码不判断该不该守；
        - 没给 → 兜底的"所有接敌省"（只有内部 dispatch 会这么调）。
        摆法（每省几个师、从哪抽人）按引擎实测规则（`_quotas`），这是执行细节。
        """
        st = self._read_state()
        if not st:
            return {"active": False}
        obs = self.observe(player_addr, player_short)
        units, dist, adj = obs["units"], obs["dist"], obs["adj"]
        addr_short = obs["addr_short"]
        ids = {int(i) for i in st.get("ids") or []}
        mine = set(ids)

        # 冲突检测：师的行军目标被我改过 → 玩家动过 → 让出
        issued = {int(a): int(r["dst"])
                  for r in (st.get("plan") or {}).get("issued") or []
                  for a in r["addrs"]}
        released = []
        for u in units:
            want = issued.get(int(u["addr"]))
            if want is not None and int(u["target"] or 0) not in (0, want):
                released.append(u["uid"])
                ids.discard(int(u["uid"]))
                mine.discard(int(u["uid"]))
        if released:
            st["ids"] = sorted(ids)
            self._write_state(st)

        units = [u for u in units if int(u["uid"]) in ids]
        # 我方控制/驻扎的省
        my_provs = {int(u["prov"]) for u in units}
        # 0.9.18 修：判断敌我要按**国籍**（接管名单只有这一支部队的师，
        # 本国别处的师不能被当成敌军——真机出现过"德国 42"的乌龙）。
        #
        # 0.9.22 修：**player_addr 必须无条件算自己人**。以前只认 addr_short
        # 里名字等于 player_short 的地址，而 addr_short 来自 900 秒 TTL 的
        # 缓存——缓存一冷它就是空的，于是"不是我的全算敌人"，每个有部队的省
        # 都被判成交火中，`free` 恒为空，**任何守线命令都下不出去**（真机表现：
        # 「手上的师都在路上或正在打，先不动」）。player_addr 是调用方直接给的，
        # 不依赖任何缓存，拿它兜底就不会再有这个死锁。
        mine_all = {int(player_addr)}
        mine_all |= {int(a) for a, s in addr_short.items()
                     if player_short and s == player_short}
        # 「在打」要按**已知敌国**判，不是"不是我就算敌人"：
        # 盟友/仆从同驻一个省是常态（1939 德国+斯洛伐克），那不算交火。
        # 敌国名单来自这局战争的参战范围；读不到就当没有敌人——
        # 引擎本来就不会把交火中的师调走，放宽只会更安全。
        enemy_names = self._live.enemy_names()
        if front_provs:
            # 她点名的线：合法就照用，不合法就**如实说**哪几个到不了。
            #
            # 「够得着」的基准是 **我方控制的全部省**，不是已接管部队脚下那几块：
            # 部队走的是本国领土。0.9.22 之前只认「已接管部队的邻省」，
            # 1939 德国铺波兰前线时，正面 34 个省里 27 个是德国自己的，
            # 但接管的第 4 集团军在南边斯洛伐克，于是 25 个省被判"够不着"，
            # 整条线一个兵都不摆——她看到的就是"连部队驻扎的省也不认"。
            my_reach = set(my_provs)
            # 工具路径下全服控制图会被"让位"机制放弃（避免 SCANALL 饿死工具
            # 查询），所以这儿再按需直读她点名的这几个省的控制者：
            # 几十次 RD，毫秒级，跟任何缓存无关。0.9.22 真机就是栽在这——
            # 探针里够得着，一进插件控制图为空，25 个省又全"够不着"。
            # **含友邦**：军事通行权下盟友/仆从的省也走得进去。
            # 友邦名单在 controlled_provs 内部解析（也走只吃缓存的路径）。
            try:
                here = self._live.controlled_provs(
                    player_addr, list(front_provs))
                if here:
                    my_reach |= here
            except Exception as exc:  # noqa: BLE001 读不到就退回缓存判定
                self._log(f"按需读省控制者失败（{type(exc).__name__}: {exc}）")
            # 够得着 = 自己控制/驻军，或者和我方控制的省相邻（往前推一格）
            touch: set[int] = set()
            for q in my_reach:
                touch.update(adj.get(q) or ())
            ok_set = my_reach | touch

            front = []
            bad = []
            for p in front_provs:
                p = int(p)
                (front if p in ok_set else bad).append(p)
            front = sorted(set(front))
            if bad and not front:
                # 一个都够不着才真的没法动，这时候照实说
                return {"active": True, "army": st.get("army"),
                        "units": len(units), "front": [], "bad": bad,
                        "moved": 0, "released": released,
                        "note": f"这 {len(bad)} 个省我一个都够不着"
                                f"（不和我方领土相邻），没法守：{bad}。"}
        else:
            front = []
            for p in my_provs:
                for nb in adj.get(p) or ():
                    slot = dist.get(int(nb)) or {}
                    if any(enemy_names and addr_short.get(int(a)) in enemy_names
                           for a in slot):
                        front.append(p)
                        break
            front.sort()
        q = self._quotas(len(units), front, adj, dist, mine_all)

        cur: dict[int, int] = {}
        inbound: dict[int, int] = {}
        # 正在打的省：本省就有**敌国**的兵。这些师抽不得（引擎也不会把
        # 交火中的师调走），否则等于临阵脱逃。
        # 判据必须是"已知敌国"，不能是"不是我就算敌人"——后者在
        # addr_short 缓存冷掉时会把每个省都判成交火，`free` 恒为空，
        # 于是**任何守线命令都下不出去**（0.9.22 真机死锁）。
        fighting = {p for p in my_provs
                    if any(enemy_names and addr_short.get(int(a)) in enemy_names
                           for a in (dist.get(p) or {}))}
        free = []
        for u in units:
            p = int(u["prov"])
            # 「在不在行军」要看 **path**，不是 target。
            # 0.9.22 真机实测：下了移动令之后 order 从 1 变 2、path 是一整条
            # 路线（终点就是目标省），但 +0x378 的 target 和 +0x610 的 remain
            # 都是 0——只看 target 会把正在行军的师当成空闲，重复下令来回折腾。
            route = list(u.get("path") or [])
            t = int(u["target"] or 0) or (int(route[-1]) if route else 0)
            if t:
                inbound[t] = inbound.get(t, 0) + 1
            elif p not in fighting:
                free.append(u)
            cur[p] = cur.get(p, 0) + 1

        need = {p: q[p] - cur.get(p, 0) - inbound.get(p, 0) for p in q}
        out = {"active": True, "army": st.get("army"), "units": len(units),
               "front": front, "quotas": q, "need": need,
               "released": released, "moved": 0, "dry_run": dry_run}
        if front_provs and bad:
            # 够得着的照守，够不着的如实列出来——以前是"有一个够不着就整条
            # 线不动"，那等于她点了 34 个省、结果一个兵都不摆。
            out["bad"] = bad
            out["note"] = (f"够得着的 {len(front)} 个省我摆上了；"
                           f"另有 {len(bad)} 个够不着（不和我方领土相邻），"
                           f"这几个没摆：{bad}。")
        elif front_provs:
            out["note"] = f"这条线 {len(front)} 个省都够得着。"
        if released:
            out["note"] = (f"这 {len(released)} 个师你手动动过，我交回指挥权。"
                           + (out.get("note") or ""))
        if not front:
            out["note"] = (out.get("note") or "") + "没有要守的省。"
            return out
        if not free:
            out["note"] = (out.get("note") or "") + "手上的师都在路上或正在打，先不动。"
            return out

        rows = []
        used = 0
        for p in sorted(need, key=lambda x: -need[x]):
            k = min(need[p], len(free), max_units - used)
            if k <= 0 or need[p] <= 0:
                continue
            # 就近：已在邻省的师优先，其余随便（引擎也是就近抽）
            near = [u for u in free if int(u["prov"]) in (adj.get(p) or ())]
            got = (near + [u for u in free if u not in near])[:k]
            rows.append({"dst": p, "addrs": [u["addr"] for u in got]})
            free = [u for u in free if u not in got]
            used += len(got)
            if used >= max_units:
                break
        if not rows:
            out["note"] = out.get("note") or "线上不缺人。"
            return out
        if dry_run:
            out["rows"] = rows
            out["note"] = (f"要补 {len(rows)} 处，共 {used} 个师"
                           f"（试运行，没动手）")
            return out
        r = self._apply(player_addr, rows, what)
        out["moved"] = r["done"]
        out["rows"] = r["rows"]
        out["note"] = r["note"]
        return out

    def dispatch(self, player_addr: int, player_short: str = "",
                 enemies: set[str] | None = None,
                 allies: set[str] | None = None,
                 max_units: int = 3, dry_run: bool = False) -> dict:
        """（内部兜底）算"所有接敌省"当线，再交给 `_fill`。

        M4' 之后循环里**不再自动调它**——守哪些省由 YUI 说，走 `hold()`。
        """
        return self._fill(player_addr, player_short, None, max_units, dry_run, "守线")

    def tick(self, player_addr: int, player_short: str = "",
             auto_attack: bool = False, enemies: set[str] | None = None,
             max_units: int = 3, allies: set[str] | None = None) -> dict:
        """**只观察，不再自己派兵**（0.9.18 / M4'）。

        0.9.16 之前这里会自动找最弱的省冲、0.9.17 改成自动守线——都是代码
        在替她做战术判断。用户要的是让她自己学，所以这个入口现在只剩观察：
        读一遍状态、返回师数和位置，一个字都不下。

        决策走这两条路：
        - 她说"守这几个省" → `hold()`（守哪些省她定，怎么摆按引擎规则）；
        - 她说"派 N 个师去 X" → 工具层的 move。
        """
        st = self._read_state()
        if not st:
            return {"active": False}
        # 0.9.18（M4'）：tick **不再自己派兵**。决策权已经交给 YUI——守哪
        # 条线、往哪推由她说，代码只在她明确下令时（hold/move）动手。
        # 这里保留只为兼容旧调用点，它现在是一个纯观察函数，永远 moved=0。
        obs = self.observe(player_addr)
        return {"active": True, "army": st.get("army"),
                "units": len(obs["units"]), "released": [], "moved": 0,
                "note": "（只观察，不动兵）"}
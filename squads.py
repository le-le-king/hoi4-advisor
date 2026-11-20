"""YUI 接管的部队登记表。

为什么要有这张表：用户按 F7 说"这几支部队交给你"，之后 YUI 调兵、划线
就该**只动这几支**。没有登记表，她一动就是全军的师——那不是代手，那是接管存档。

存的是 `CArmy*` 地址。地址在同一局游戏里是稳定的（对象不搬家），
换一局、重开游戏就全变了，所以每次用之前都要拿当前师列表核一遍，
对不上的直接作废，不许猜。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

__all__ = ["Squads", "SquadError"]

FILE_NAME = "yui_squads.json"


class SquadError(RuntimeError):
    pass


class Squads:
    def __init__(self, data_dir: Path, log=None) -> None:
        self._path = Path(data_dir) / FILE_NAME
        self._log = log or (lambda msg: None)

    # ── 存取 ──────────────────────────────────

    def load(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("next_id", 1)
        data.setdefault("squads", [])
        if not isinstance(data["squads"], list):
            data["squads"] = []
        return data

    def save(self, data: dict) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf8")
        except OSError as exc:
            self._log(f"写接管登记表失败：{exc}")
            raise SquadError(f"接管登记表存不下去：{exc}") from None

    # ── 增删 ──────────────────────────────────

    def add(self, country_addr: int, country_name: str, units: list[dict],
            label: str = "") -> dict:
        """登记一批师。同一批师重复注册会并进去，不会出现两份。"""
        if not units:
            raise SquadError("没有可登记的师。")
        data = self.load()
        addrs = {int(u["addr"]) for u in units}

        # 已经挂着的，先从原批次摘掉——不然一支师会在两个批次里各算一次
        for sq in data["squads"]:
            sq["units"] = [u for u in sq.get("units") or [] if int(u.get("addr") or 0) not in addrs]
        data["squads"] = [sq for sq in data["squads"] if sq.get("units")]

        sid = int(data.get("next_id") or 1)
        data["next_id"] = sid + 1
        squad = {
            "id": sid,
            "name": label or f"第 {sid} 批",
            "country": country_name,
            "country_addr": int(country_addr),
            "at": int(time.time()),
            "units": [
                {"addr": int(u["addr"]), "prov": int(u.get("prov") or 0),
                 "type": int(u.get("type") or 0)}
                for u in units
            ],
        }
        data["squads"].append(squad)
        self.save(data)
        self._log(f"接管登记：第 {sid} 批 {len(units)} 个师（{country_name}）")
        return squad

    def release(self, squad_id: int = 0, addrs: set[int] | None = None,
                all_of: bool = False) -> dict:
        """放回去。给了 squad_id 就放那一批，给了 addrs 就放那几个师。"""
        data = self.load()
        before = sum(len(sq.get("units") or []) for sq in data["squads"])
        if all_of:
            data["squads"] = []
        elif squad_id:
            data["squads"] = [sq for sq in data["squads"] if int(sq.get("id") or 0) != int(squad_id)]
        elif addrs:
            for sq in data["squads"]:
                sq["units"] = [u for u in sq.get("units") or []
                               if int(u.get("addr") or 0) not in addrs]
            data["squads"] = [sq for sq in data["squads"] if sq.get("units")]
        else:
            raise SquadError("要说清放回哪一批（或者 all_of=True 全放）。")
        after = sum(len(sq.get("units") or []) for sq in data["squads"])
        self.save(data)
        return {"released": before - after, "left": after}

    # ── 核对 ──────────────────────────────────

    def refresh(self, live_units: list[dict]) -> dict:
        """拿当前这局的师列表核一遍。

        地址对不上的（换局、重开游戏）直接作废，并且明说作废了几个——
        留着一个不存在的地址，下次调兵会写到别人身上或者直接崩。
        """
        alive = {int(u["addr"]): u for u in live_units}
        data = self.load()
        dropped = 0
        for sq in data["squads"]:
            keep = []
            for u in sq.get("units") or []:
                a = int(u.get("addr") or 0)
                if a in alive:
                    u["prov"] = int(alive[a].get("prov") or 0)
                    u["type"] = int(alive[a].get("type") or 0)
                    keep.append(u)
                else:
                    dropped += 1
            sq["units"] = keep
        data["squads"] = [sq for sq in data["squads"] if sq.get("units")]
        if dropped:
            self.save(data)
            self._log(f"接管登记表作废了 {dropped} 个对不上的师")
        total = sum(len(sq.get("units") or []) for sq in data["squads"])
        return {"dropped": dropped, "total": total, "squads": len(data["squads"])}

    def unit_addrs(self) -> list[int]:
        data = self.load()
        return [int(u["addr"]) for sq in data["squads"]
                for u in sq.get("units") or [] if u.get("addr")]

    def total(self) -> int:
        return len(self.unit_addrs())

    # ── 给人看的 ──────────────────────────────

    @staticmethod
    def describe(data: dict) -> list[str]:
        squads = data.get("squads") or []
        if not squads:
            return ["现在没有部队归我指挥。"]
        lines = [f"我手上有 {sum(len(sq.get('units') or []) for sq in squads)} 个师，"
                 f"分 {len(squads)} 批："]
        for sq in squads:
            units = sq.get("units") or []
            provs: dict[int, int] = {}
            for u in units:
                provs[int(u.get("prov") or 0)] = provs.get(int(u.get("prov") or 0), 0) + 1
            spread = "，".join(f"省 {p}×{n}" for p, n in list(provs.items())[:5])
            lines.append(f"  [{sq.get('id')}] {sq.get('name')}：{len(units)} 个师（{spread}）")
        return lines

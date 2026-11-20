"""兵力架构（OOB）：战区 → 集团军群 → 集团军 → 师（名字 + 位置）。

存档里这一层的真实字段（在 1.14.8 真档上逐项核对过，跟 HOI4 Wiki 的
"Land warfare / Command group" 一致）：

    countries.<TAG>.theatres.theatre[]
      ├─ theater_group[]       战区，name="德国第1战区"，orders_group 里是编组 id
      ├─ field_marshal_group[] **集团军群**，name="第1集团军群"，
      │                        orders_group[] 是它下辖的集团军 id
      └─ orders_group[]        **集团军**，name="第1集团军"，
                               member[].unit.id 就是师 id

`member` 里的 id 与 `countries.<TAG>.units.division[].id` 完全一致
（德国 153/153、卢森堡 16/16 全对得上），所以师的 `location` 就是位置。
`type=4713` / `type=51` 只是 id 的类型码，别拿它判断是不是师。

师名不是明文：division 里只有 `division_name={type=0 name_order=2}`，
要靠 `division_template_id` → `division_template.division_names_group` →
`common/units/names_divisions/*.txt` 的 `ordered[2]` / `fallback_name`
拼出来（HOI4 Wiki「Division modding」「Namelist modding」），再走
`localisation.zh_division_name` 翻成中文番号。

这一层只在**存档**里，内存通道没有逆出编组指针。所以调用方必须先看
`save_match.is_current`：不是当前局就别把上一局的编制端出来。
"""
from __future__ import annotations

from pathlib import Path

from . import localisation as locmod
from . import pdx_parser as P

__all__ = ["build", "render", "brief"]


def _first(obj, key):
    """重复 key 会被解析器收成 Multi，取第一个定义。"""
    val = obj.get(key) if isinstance(obj, dict) else None
    if isinstance(val, P.Multi):
        return val[0] if val else None
    return val


def _id_of(obj) -> int | None:
    """id 有两种写法：`obj.id={id=1 type=51}`（两层）和引用块直接是
    `{id=265 type=52}`（一层，如 division_template_id）。两种都得认。"""
    raw = _first(obj or {}, "id")
    if isinstance(raw, dict):
        raw = raw.get("id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _text(val) -> str:
    if isinstance(val, (list, P.Multi)):
        val = val[0] if val else ""
    if isinstance(val, dict):
        return ""
    return str(val or "").strip()


def _name_groups(game_dir) -> dict[str, dict]:
    """common/units/names_divisions/*.txt -> {组名: 定义}。"""
    if not game_dir:
        return {}
    folder = Path(game_dir) / "common" / "units" / "names_divisions"
    if not folder.is_dir():
        return {}
    out: dict[str, dict] = {}
    for f in sorted(folder.glob("*.txt")):
        try:
            tree = P.parse(f.read_text(encoding="utf-8-sig", errors="replace"))
        except Exception:  # noqa: BLE001 单个 mod 文件坏了不影响别的组
            continue
        for key, val in (tree or {}).items():
            if isinstance(val, dict):
                out[key] = val
    return out


def _division_name(div: dict, templates: dict, groups: dict,
                   loc: locmod.Localisation | None) -> str:
    # 玩家自己改过名字的师，存档里直接有 name，中文原样保留
    own = _text(div.get("name"))
    if own:
        return own
    tpl_id = _id_of(_first(div, "division_template_id") or {})
    tpl = templates.get(tpl_id) or {} if tpl_id is not None else {}
    grp = groups.get(_text(tpl.get("division_names_group"))) or {}
    dn = _first(div, "division_name") or {}
    try:
        order = int(P.get_int(dn, "name_order", 0))
    except Exception:  # noqa: BLE001
        order = 0
    ordered = grp.get("ordered") if isinstance(grp, dict) else None
    raw = ""
    if isinstance(ordered, dict):
        raw = _text(ordered.get(str(order)) or ordered.get(order))
    if not raw:
        raw = _text(grp.get("fallback_name")) if isinstance(grp, dict) else ""
    if not raw:
        raw = _text(tpl.get("name"))
    if not raw:
        return f"第{order}师" if order else "未命名师"
    return locmod.zh_division_name(raw, order, loc)


def build(root: dict, game_dir=None, loc: locmod.Localisation | None = None) -> dict:
    """从**已经解析好的**存档树组装编制，不再重复解析那 40 MB。"""
    empty = {"ok": False, "error": "存档树里没有玩家国家", "player": ""}
    if not isinstance(root, dict):
        return empty
    player = _text(root.get("player"))
    countries = root.get("countries") or {}
    node = countries.get(player) if isinstance(countries, dict) else None
    if not isinstance(node, dict):
        return {**empty, "player": player}

    # 模板挂在顶层 `division_templates={ division_template={...} ... }` 里，
    # 不是散在 root 下——第一版在这儿读了个空，师名全退化成「第N师」。
    templates: dict[int, dict] = {}
    box = root.get("division_templates")
    tpl_rows = P.as_list(box.get("division_template") if isinstance(box, dict) else None)
    if not tpl_rows:
        tpl_rows = P.as_list(root.get("division_template"))   # 老档兜底
    for tpl in tpl_rows:
        if isinstance(tpl, dict):
            tid = _id_of(tpl)
            if tid is not None:
                templates[tid] = tpl
    groups = _name_groups(game_dir)

    divisions: dict[int, dict] = {}
    for div in P.as_list((node.get("units") or {}).get("division")):
        if not isinstance(div, dict):
            continue
        did = _id_of(div)
        if did is None:
            continue
        prov = P.get_int(div, "location", 0)
        divisions[did] = {
            "id": did,
            "name": _division_name(div, templates, groups, loc),
            "prov": prov,
        }

    armies: dict[int, dict] = {}
    army_groups: list[dict] = []
    theatres: list[dict] = []
    assigned: set[int] = set()
    for th in P.as_list((node.get("theatres") or {}).get("theatre")):
        if not isinstance(th, dict):
            continue
        th_name = ""
        for tg in P.as_list(th.get("theater_group")):
            if isinstance(tg, dict) and _text(tg.get("name")):
                th_name = _text(tg.get("name"))
                break
        theatre = {"name": th_name or "未命名战区",
                   "army_group_names": [], "army_names": []}
        for grp in P.as_list(th.get("orders_group")):
            if not isinstance(grp, dict):
                continue
            gid = _id_of(grp)
            if gid is None:
                continue
            members = []
            for m in P.as_list(grp.get("member")):
                uid = None
                if isinstance(m, dict):
                    unit = _first(m, "unit")
                    uid = unit.get("id") if isinstance(unit, dict) else None
                try:
                    uid = int(uid) if uid is not None else None
                except (TypeError, ValueError):
                    uid = None
                if uid in divisions:
                    members.append(divisions[uid])
                    assigned.add(uid)
            armies[gid] = {
                "id": gid,
                "name": _text(grp.get("name")) or f"集团军#{gid}",
                "count": len(members),
                "divisions": members,
            }
        for fg in P.as_list(th.get("field_marshal_group")):
            if not isinstance(fg, dict):
                continue
            sub = []
            for ref in P.as_list(fg.get("orders_group")):
                aid = ref.get("id") if isinstance(ref, dict) else None
                try:
                    aid = int(aid) if aid is not None else None
                except (TypeError, ValueError):
                    aid = None
                if aid in armies:
                    sub.append(armies[aid])
            rec = {
                "id": _id_of(fg),
                "name": _text(fg.get("name")) or "未命名集团军群",
                "army_count": len(sub),
                "army_names": [a["name"] for a in sub],
                "divisions": sum(a["count"] for a in sub),
                "armies": sub,
            }
            army_groups.append(rec)
            theatre["army_group_names"].append(rec["name"])
        in_groups = {a["id"] for g in army_groups for a in g["armies"]}
        theatre["army_names"] = [a["name"] for a in armies.values()
                                 if a["id"] not in in_groups]
        theatres.append(theatre)

    in_groups = {a["id"] for g in army_groups for a in g["armies"]}
    direct = [a for a in armies.values() if a["id"] not in in_groups]
    unassigned = [d for did, d in divisions.items() if did not in assigned]

    result = {
        "ok": True,
        "player": player,
        "player_name": (loc.country(player) if loc else "") or player,
        "date": _text(root.get("date")),
        "totals": {
            "divisions": len(divisions),
            "armies": len(armies),
            "army_groups": len(army_groups),
            "theatres": len(theatres),
            "unassigned": len(unassigned),
        },
        "theatres": theatres,
        "army_groups": army_groups,
        "direct_armies": direct,
        "unassigned": unassigned,
        "name_groups": len(groups),
        "templates": len(templates),
        "loc_keys": len(loc.table) if loc else 0,
        "error": "",
    }
    if loc is not None:
        tags = [player]
        for t in (countries.keys() if isinstance(countries, dict) else []):
            if len(t) == 3 and t not in tags:
                tags.append(t)
            if len(tags) >= 8:
                break
        result["loc_digest"] = loc.digest(tags)
    return result


def _place(d: dict, label_fn) -> str:
    prov = int(d.get("prov") or 0)
    if label_fn and prov:
        try:
            return label_fn(prov)
        except Exception:  # noqa: BLE001
            pass
    return f"省 {prov}" if prov else "位置未知"


def _army_line(army: dict, label_fn, max_divs: int) -> list[str]:
    divs = army.get("divisions") or []
    out = [f"  {army['name']}：{army['count']} 个师"]
    shown = divs[:max_divs]
    if shown:
        out.append("    " + "；".join(
            f"{d['name']}（{_place(d, label_fn)}）" for d in shown))
    if len(divs) > max_divs:
        out.append(f"    ……另有 {len(divs) - max_divs} 个师，问一句就能展开。")
    return out


def render(oob: dict, label_fn=None, loc: locmod.Localisation | None = None,
           max_divs: int = 40, date_text: str = "",
           is_current: bool = True) -> str:
    """给模型 / 用户看的中文编制段落。"""
    if not isinstance(oob, dict) or not oob.get("ok"):
        return ""
    tot = oob.get("totals") or {}
    head = (date_text or oob.get("date") or "存档")
    tag = oob.get("player_name") or oob.get("player") or "?"
    lines = [f"【兵力架构｜{head} 的 {tag} 存档】"]
    if not is_current:
        lines.append("※ 这份存档不是当前这局的，编制只作参考，别当成现在的部队。")
    lines.append(
        f"全军 {tot.get('divisions', 0)} 个师，编成 {tot.get('armies', 0)} 个集团军、"
        f"{tot.get('army_groups', 0)} 个集团军群，分在 {tot.get('theatres', 0)} 个战区。")

    for th in oob.get("theatres") or []:
        lines.append(f"战区「{th['name']}」：")
        groups_here = [g for g in (oob.get("army_groups") or [])
                       if g["name"] in (th.get("army_group_names") or [])]
        if not groups_here:
            groups_here = oob.get("army_groups") or []
        for g in groups_here:
            names = "、".join(g["army_names"][:12]) or "（没读到下辖集团军）"
            lines.append(f"  集团军群「{g['name']}」下辖 {g['army_count']} 个集团军：{names}"
                         f"（共 {g['divisions']} 个师）")
            for army in g["armies"]:
                lines.extend(_army_line(army, label_fn, max_divs))
        if not (oob.get("army_groups") or []):
            direct = [a for a in (oob.get("direct_armies") or [])
                      if a["name"] in (th.get("army_names") or [])]
            for army in direct:
                lines.extend(_army_line(army, label_fn, max_divs))
    if not (oob.get("army_groups") or []) and not (oob.get("direct_armies") or []):
        lines.append("  （这份存档里没读到集团军编组，师可能是散编的。）")
    unassigned = oob.get("unassigned") or []
    if unassigned:
        lines.append(f"另有没有编进集团军的师 {len(unassigned)} 个：" + "；".join(
            f"{d['name']}（{_place(d, label_fn)}）" for d in unassigned[:12]))
    digest = oob.get("loc_digest") or (loc.digest([oob.get("player", "")]) if loc else "")
    if digest:
        lines.extend(["", digest])
    return "\n".join(lines)


def brief(oob: dict) -> str:
    """降级播报用的一句。"""
    if not isinstance(oob, dict) or not oob.get("ok"):
        return ""
    t = oob.get("totals") or {}
    return (f"另外读到兵力架构：{t.get('divisions', 0)} 个师、"
            f"{t.get('armies', 0)} 个集团军、{t.get('army_groups', 0)} 个集团军群，"
            f"哪个集团军带哪些师、都在哪，问一句就能展开。")

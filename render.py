"""把 snapshot 排版成能直接说出口的话。

为什么在这里排版而不是把字段丢给模型：
实测过（围棋插件那一轮），把结构化字段交给模型，它会自己做取舍，
经常把最关键的那一项省掉。所以这里排好版，模型负责转述而不是搬运。

三条规矩（来自开发思路第七节，写进排版本身就是最硬的约束）：
1. 只说她该说的 —— 用户自己能看到的数字不复述，重点在他看不到的。
2. 给判断不给清单 —— 所以"要注意"那几行是结论，不是报表。
3. 不确定就说不确定 —— 日期和版本必须出现在第一行和最后一行。
"""

from __future__ import annotations

__all__ = [
    "brief",
    "army_detail",
    "economy_detail",
    "stale_note",
    "bridge_report",
    "act_report",
    "verify_report",
]


def _num(v) -> str:
    """大数字humanize。参谋要的是量级，不是个位。"""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(n) >= 100_000_000:
        return f"{n / 100_000_000:.1f}亿"
    if abs(n) >= 10_000:
        return f"{n / 10_000:.1f}万"
    if n == int(n):
        return str(int(n))
    return f"{n:.1f}"


def brief(snap: dict, live_army: dict | None = None) -> str:
    """局势简报。控制在 15 行内，模型才不会照抄成清单。

    `live_army`（0.9.14）：{"age": 秒, "total": n, "top": [(地名, 师数)…]}。
    游戏在跑且缓存齐时由调用方算好传入，「主力在」一行就换成实时分布；
    传 None 照旧用存档里的分布。
    """
    meta = snap.get("meta") or {}
    player = snap.get("player") or {}
    army = snap.get("army") or {}
    lines: list[str] = []

    date_text = meta.get("date_text") or "未知日期"
    pname = meta.get("player_name") or meta.get("player") or "?"
    tag = meta.get("player") or ""
    lines.append(f"【钢四局势】{date_text}存档 · {pname}（{tag}）")

    if player:
        lines.append(
            f"政治点 {_num(player.get('政治点', 0))} · "
            f"稳定 {player.get('稳定度', 0):.0f}% · "
            f"战支 {player.get('战争支持', 0):.0f}% · "
            f"人力 {_num(player.get('人力', 0))}"
        )
        lines.append(
            f"民工 {player.get('民工', 0)} · 军工厂 {player.get('军工厂', 0)} · "
            f"船坞 {player.get('船坞', 0)} · 研究槽 {player.get('研究槽', 0)}"
        )

    if army:
        extra = []
        if army.get("行军中"):
            extra.append(f"行军 {army['行军中']}")
        if army.get("缺编严重"):
            extra.append(f"缺编 {army['缺编严重']}")
        tail = f"（{'，'.join(extra)}）" if extra else ""
        lines.append(f"师 {army.get('师总数', 0)} 个{tail}")

    focus = snap.get("focus") or {}
    if focus.get("当前"):
        prog = focus.get("进度")
        prog_text = f"，进度 {prog:.0f}%" if isinstance(prog, (int, float)) else ""
        lines.append(f"当前国策：{focus['当前']}{prog_text}")

    # 师分布只在人多的地方点一下，不铺开成表
    used_live = bool(live_army and live_army.get("top"))
    if used_live:
        top = list(live_army["top"])[:5]
        age = live_army.get("age")
        ago = f"，{age} 秒前读" if isinstance(age, (int, float)) and age >= 0 else ""
        lines.append("主力在（实时" + ago + "）："
                     + "、".join(f"{k} {v}" for k, v in top))
    else:
        dist = (army.get("分布") or {})
        if dist:
            top = list(dist.items())[:5]
            lines.append("主力在：" + "、".join(f"{k} {v}" for k, v in top))

    items = [a for a in (snap.get("anomalies") or []) if a.get("level") in ("warn", "doubt")]
    infos = [a for a in (snap.get("anomalies") or []) if a.get("level") == "info"]
    if items or infos:
        lines.append("")
        lines.append("要注意：")
        for a in (items + infos)[:6]:
            mark = "!" if a.get("level") == "warn" else ("?" if a.get("level") == "doubt" else "·")
            lines.append(f"{mark} {a.get('text', '')}")
    else:
        lines.append("")
        lines.append("没查出明显漏洞。")

    lines.append("")
    if used_live:
        lines.append(f"除「主力在（实时）」一行是刚从游戏内存读的，"
                     f"其余都是 {date_text} 存档那一刻的局面。")
    else:
        lines.append(f"以上是 {date_text} 存档那一刻的局面，不是实时。")
    return "\n".join(lines)


def army_detail(snap: dict) -> str:
    army = snap.get("army") or {}
    if not army:
        return "这份存档里没读到部队数据。"
    lines = [f"师 {army.get('师总数', 0)} 个，行军 {army.get('行军中', 0)} 个，"
             f"缺编（满编不足80%）{army.get('缺编严重', 0)} 个"]
    if army.get("满编未知"):
        lines.append(f"（另有 {army['满编未知']} 个师没读到满编数据，没算进缺编里）")
    dist = army.get("分布") or {}
    if dist:
        lines.append("按州分布：")
        for name, n in dist.items():
            lines.append(f"  {name}：{n} 个师")
    return "\n".join(lines)


def economy_detail(snap: dict) -> str:
    p = snap.get("player") or {}
    if not p:
        return "这份存档里没读到经济数据。"
    lines = [
        f"民工 {p.get('民工', 0)} · 军工厂 {p.get('军工厂', 0)} · 船坞 {p.get('船坞', 0)}",
        f"政治点 {_num(p.get('政治点', 0))} · 指挥点 {p.get('指挥点', 0)}",
        f"陆军经验 {_num(p.get('陆军经验', 0))} · 海军经验 {_num(p.get('海军经验', 0))} · "
        f"空军经验 {_num(p.get('空军经验', 0))}",
    ]
    res = p.get("资源") or {}
    if res:
        lines.append("资源产出：" + "、".join(f"{k} {v}" for k, v in res.items()))
    fuel = p.get("燃料")
    if fuel is not None:
        lines.append(f"燃料 {_num(fuel)} / 上限 {_num(p.get('燃料上限', 0))}（存档原值）")
    return "\n".join(lines)


def stale_note(minutes: float) -> str:
    if minutes < 2:
        return "存档刚存过，几乎是实时的。"
    if minutes < 60:
        return f"这份存档是 {minutes:.0f} 分钟前存的，局面可能已经变了。"
    hours = minutes / 60
    if hours < 24:
        return f"这份存档是 {hours:.0f} 小时前存的，先手动存一次档再问更准。"
    return f"这份存档是 {hours / 24:.0f} 天前存的，基本只能当历史参考。"


# ── 写侧（路线 C：只做白名单那一小块） ────────────────


def bridge_report(st: dict) -> str:
    """桥接状态。说清"现在能不能动手"，以及不能的话要用户做什么。"""
    state = str(st.get("state") or "unknown")
    port = st.get("port") or 0

    head = {
        "ready": f"桥接已连（端口 {port}）。可以发命令了，游戏不用切到前台。",
        "no_bridge": "桥接没连上：DLL 还没注进游戏。你开一局，我这边注入一下就行。",
        "no_game": "游戏没在跑。等你开了局再叫我，现在没法动手。",
        "denied": "打不开游戏进程，是权限问题（下面这行是原因）。",
        "dll_missing": "插件包里的 neko_hoi4_bridge.dll 不见了，插件得重装一次。",
        "no_port": "DLL 注进去了，但端口没起来。",
        "unsupported": "这套桥接只在 Windows 上能用。",
        "failed": "注入失败。",
    }.get(state, f"桥接状态不明：{state}。")

    lines = [head]
    detail = str(st.get("detail") or "").strip()
    # 这几种状态 head 已经说完了，再贴 detail 就是复读
    redundant = {"ready", "no_bridge", "no_game", "unsupported"}
    if detail and state not in redundant:
        lines.append(detail)

    tail = str(st.get("log_tail") or "").strip()
    if tail:
        lines.append("游戏那边记的最后几行：")
        lines.extend("  " + ln for ln in tail.splitlines()[-4:])
    return "\n".join(lines)


def act_report(label: str, amount: int, cmd: str, note: str = "") -> str:
    """投递成功。**不许说"已生效"**——游戏不回报，这句只能这么说。"""
    if amount:
        first = f"已投递：{label} +{amount}（控制台命令 {cmd}）"
    else:
        first = f"已投递：{label}（控制台命令 {cmd}）"
    lines = [
        first,
        "游戏不会回报执行结果，所以这条只能算「发出去了」。"
        "等你下次存档（手动存一份也行），我读一遍就能告诉你到底有没有加上。",
    ]
    if note:
        lines.append(f"提醒：{note}")
    return "\n".join(lines)


def verify_report(rec: dict) -> str:
    """上次投递的核对结果。这是写侧唯一能说"确实生效"的场合。"""
    label = rec.get("label") or rec.get("action") or "上次那个操作"
    amount = rec.get("amount") or 0
    state = rec.get("state") or ""
    delta = rec.get("result_delta")
    field = rec.get("field") or ""
    save_date = rec.get("checked_save_date") or ""

    head = f"上次加的 {label}" + (f" +{amount}" if amount else "")
    if isinstance(delta, (int, float)):
        move = f"{field} 从 {rec.get('baseline')} 变成 {rec.get('result_value')}（{delta:+.0f}）"
    else:
        move = ""

    if state == "confirmed":
        return f"{head}，{save_date} 那份存档里确认到账：{move}。"
    if state == "partial":
        return f"{head}，存档里只到了一部分：{move}。可能是被上限截了，也可能命令只打了一半。"
    if state == "capped":
        return f"{head}，{field} 已经顶到 100 上限，看不全效果：{move}。"
    if state == "none":
        return (
            f"{head}，{save_date} 那份存档里没看到变化（{move}）。两种可能：命令没生效"
            "（控制台状态不同步），或者这份档是在我发命令之前存的。"
        )
    return f"{head}，这类操作没法用存档核对，只能靠你在游戏里看一眼。"

"""前线哨兵：开局总览（全量给模型）+ 交火/推进变化主动汇报。

用户要的是"她主动知道前线在发生什么"，不是每次都要问。

- **开局**：游戏一进来（代理能应答），扫一遍正在进行的陆战和**全服省份控制图**，
  但**只把参与玩家这局战争的国家**（本国、同场盟友、直接敌对国）之间的交火、
  以及这些国家的控制省数整理成全量情报交给模型，让她做一段汇报（不截断、不逐条念）。
  全服扫描仍要做（不扫就认不出谁跟谁在打），但**别国的仗不进情报、不进播报**——
  0.9.9 之前就是这里把东亚的兵力调动画成了玩家的前线。
  模型通道不通时降级成精简播报，但全量情报仍留在 `context` 里，随时能问出来。
- **之后**每 `INTERVAL` 秒看一次：
  - **省份易主（只看参战国）** → "德国 推进到了 华沙（123，波兰）"
    用全服控制图 diff，交火结束后才发生的占领也跑不掉；两边都跟这局无关的
    整条丢掉，连"另有 N 处易主"都不汇总。
  - 新出现的交火省 → "X 省打起来了"
  - 结束的交火 → "X 省的交火结束了"
  - 交火省里出现新国家的部队 / 增兵

线程模型：独立守护线程，只读内存、只发 HTTP，不碰游戏主线程。
"""
from __future__ import annotations

import time

from . import livegame, nekoagent, warscope

__all__ = ["FrontWatch"]

INTERVAL = 30.0          # 秒；全服部队分布缓存 90 秒，30 秒看一次刚好
STARTUP_DELAY = 8.0      # 游戏刚起来时先等它把地图加载完
CONTROL_TTL = 60.0       # 全服控制图最多 60 秒扫一次（13376 个省，别每轮都扫）
DIST_TTL = 90.0          # 0.9.14：「省→师分布」常驻表的保鲜期，过期由哨兵重扫
SLOW_SCAN_EVERY = 3      # 每 3 轮才做一次 15 秒的全服部队扫描，其余轮只读缓存
BRIEF_ROWS = 8           # 降级播报只念前 8 处；给模型的那份**不截断**
MAX_ADVANCE = 3          # 一次最多逐条报 3 处易主，剩下的汇总，免得刷屏
BRIEF_EVERY = 60.0       # 0.9.18：战况简报最短推送间隔（秒），免得刷她一屏


class FrontWatch:
    def __init__(self, plugin, log=None, interval: float = INTERVAL) -> None:
        self._plugin = plugin
        self._log = log or (lambda msg: None)
        self._interval = interval
        self._stop = False
        self._thread = None
        self._started_at = 0.0
        self._last: dict | None = None
        self._last_ctrl: dict[int, int] | None = None   # 省号 → 控制国序号
        self._reported = False
        self._waiting_logged = False
        self._errors = 0
        self._ticks = 0
        # 0.9.18（M4'）：战况简报推给她的节流状态
        self._last_brief_at = 0.0
        self._last_brief_key = None
        # 0.9.18（M4'）：战况简报推给她的节流状态
        self._last_brief_at = 0.0
        self._last_brief_key = None

    # ── 生命周期 ──────────────────────────────

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = False
        import threading
        self._thread = threading.Thread(target=self._run, name="hoi4-frontwatch",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop = True

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> dict:
        return {
            "running": self.is_alive(),
            "reported": self._reported,
            "last_combats": (self._last or {}).get("count", 0),
            "control_provs": len(self._last_ctrl or {}),
            "errors": self._errors,
        }

    # ── 主循环 ────────────────────────────────

    def _run(self) -> None:
        time.sleep(STARTUP_DELAY)
        while not self._stop:
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 哨兵绝不能把插件搞死
                self._errors += 1
                if self._errors <= 3:
                    self._log(f"前线哨兵：{type(exc).__name__}: {exc}")
                time.sleep(min(60.0, self._interval))
                continue
            time.sleep(self._interval)

    def _tick(self) -> None:
        if not nekoagent.find_port():
            self._last = None          # 游戏退出后重新进来要再报一次总览
            self._last_ctrl = None
            self._reported = False
            try:
                # 0.9.14：分布表随游戏退出立刻作废，别让"主力在（实时）"
                # 拿上一局的分布冒充这一局
                self._plugin._live().invalidate_dist()
            except Exception:  # noqa: BLE001
                pass
            return
        plugin = self._plugin
        live = plugin._live()
        # 0.9.7：工具正在查就整轮跳过。哨兵的 frontline() 会做一次 15 秒的
        # 全服部队扫描，control_map() 又是一次 SCANALL + 几百条读；DLL 端
        # g_cmd_lock 把每条命令都串行了，两边一起跑会把工具排队到 30 秒超时。
        # 漏一轮（30 秒）比把查询挤爆划算，下一轮补上。
        if live.tool_active():
            return
        # 0.9.14：先养「省→师分布」常驻表，再管别的。这张表不等"确认玩哪国"
        # ——"哪个省有哪些师"跟玩家身份无关，units_near/army_positions/局势
        # 简报都只读它。未过期时这里直接命中缓存（零成本），过期才重扫
        # （15 秒，扫描途中工具插队会主动收手，下一轮补上）。
        try:
            live.units_by_prov(ttl=DIST_TTL)
        except Exception:  # noqa: BLE001 连不上（刚启动/注入中），下一轮再试
            pass
        # 只认"用户亲口确认过"的国家。存档很可能是上一局的，自动认人会认错，
        # 报到错国家的战况比不报更糟。用户说一句"我玩的是德国"就会记下来。
        player = live.confirmed_country()
        if not player:
            if not self._waiting_logged:
                self._log("前线哨兵：还没确认玩家国家，先不报（说一句「我玩的是德国」即可）")
                self._waiting_logged = True
            return
        self._waiting_logged = False
        # 一轮同时做 15 秒全服部队扫描 + 20 秒全服省份扫描，会比 30 秒的
        # INTERVAL 还长，哨兵就等于永远占着 DLL 的 g_cmd_lock，工具查一次
        # 排一次队。省份控制者 / 攻守方不依赖那份师列表，所以 2/3 的轮次
        # 走 fast=True 只读缓存；每 3 轮才养热一次（约 90 秒）。
        self._ticks += 1
        snap = live.frontline(int(player["addr"]), self._ticks % SLOW_SCAN_EVERY != 0)
        ctl = live.control_map(ttl=CONTROL_TTL)
        if not self._reported or self._last is None:
            self._report_overview(snap, live, ctl)
            self._reported = True
            self._last = snap
            self._last_ctrl = ctl.get("prov_ctrl") if ctl.get("ok") else None
            return
        self._diff(snap, live, ctl)
        self._last = snap
        # 扫失败就保留上一份，别清空（清空会把下一轮全量误报成"推进"）
        if ctl.get("ok"):
            self._last_ctrl = ctl["prov_ctrl"]
        # 0.9.15：接管了部队就每轮带着它走一步（默认只观察，mode=auto 才派兵）
        self._command_loop(snap, live, player)

    def _command_loop(self, snap: dict, live: livegame.LiveGame,
                      player: dict) -> None:
        """接管态下：**只看、只报，绝不动手**（0.9.18 / M4' 方案 A）。

        每轮把战况压成事实清单推给她——她说判断，**玩家点头后**她才调工具
        动手。节流：战况指纹（位置+敌情+可调兵力）没变化就不重复推，且两次
        至少隔 `BRIEF_EVERY` 秒。这条路上**一行下令代码都没有**（0.9.17 及
        之前这里会自己 dispatch 摆兵，那是在替她做决定）。
        """
        try:
            cmd = self._plugin._commander()
            if not cmd.state():
                return
            b = cmd.brief(int(player["addr"]), snap.get("player") or "")
        except Exception as exc:  # noqa: BLE001 观察失败不能拖垮哨兵
            self._log(f"战况观察跳过：{type(exc).__name__}: {exc}")
            return

        now = time.time()
        key = (tuple(sorted((b.get("pos") or {}).items())),
               tuple((r["prov"], r["enemy_total"], r["reach"])
                     for r in (b.get("front") or [])))
        if key == self._last_brief_key:
            return
        if now - self._last_brief_at < BRIEF_EVERY:
            return
        self._last_brief_at = now
        self._last_brief_key = key
        text = self._brief_text(b)
        self._log(f"战况简报：{b.get('army')} {b.get('units')} 个师，"
                  f"接邻 {len(b.get('front') or [])} 处 → 推给她")
        try:
            self._plugin._brief_push(text, meta={"army": b.get("army") or ""})
        except Exception as exc:  # noqa: BLE001 推不出去记一笔，下轮再试
            self._last_brief_key = None
            self._log(f"战况简报没推出去：{type(exc).__name__}: {exc}")

    def _brief_text(self, b: dict) -> str:
        """把简报结构渲染成**事实清单**：不排序、不评分、不写建议。"""
        spots = "、".join(f"{self._label(p)}×{n}"
                          for p, n in (b.get("pos") or {}).items())
        lines = [f"【{b.get('army')} · {b.get('units')} 个师】",
                 f"位置：{spots or '没有'}"]
        if b.get("exec_name"):
            lines.append(f"计划状态：{b['exec_name']}")
        # M4c 复盘：她上次点的令，现在到哪一步了。只看得到"什么令 → 什么结果"
        # 她才学得会打仗；代码这里只把结果摆出来，不说好不说坏。
        for r in list(b.get("aftermath") or [])[-3:]:
            when = (time.strftime("%H:%M", time.localtime(r["at"]))
                    if r.get("at") else "刚才")
            parts = []
            if r["arrived"]:
                parts.append(f"{r['arrived']} 个到了")
            if r["moving"]:
                parts.append(f"{r['moving']} 个还在路上")
            if r["elsewhere"]:
                parts.append(f"{r['elsewhere']} 个被调去别处")
            if r["gone"]:
                parts.append(f"{r['gone']} 个找不到了")
            lines.append(f"上次（{when}）你让 {r['n']} 个师去 "
                         f"{self._label(r['dst'])}：" + "、".join(parts) + "。")
        rows = b.get("front") or []
        if rows:
            lines.append(f"接邻的省（{len(rows)} 个，按省号排，不代表优先级）：")
            for r in rows:
                who = "、".join(f"{k} {v} 个师"
                               for k, v in r["enemy"].items()) or "无"
                lines.append(
                    f"  {self._label(r['prov'])}：我方在此 {r['mine_here']} 个师，"
                    f"邻省敌情 {who}（共 {r['enemy_total']}），"
                    f"我方邻省可调 {r['reach']} 个师")
        else:
            lines.append("接邻的省：无（周围没有别人家的兵）")
        rear = b.get("rear") or {}
        if rear:
            lines.append("后方不接敌：" + "、".join(
                f"{self._label(p)}×{n}" for p, n in rear.items()))
        lines.append("（这是事实清单：没有建议、没有排序、没有分数）")
        return "\n".join(lines)

    # ── 汇报 ──────────────────────────────────

    @staticmethod
    def _nations(d) -> str:
        return "、".join(f"{k} {v} 个师" for k, v in
                        sorted((d or {}).items(), key=lambda kv: -kv[1])[:4]) or "没读到"

    @staticmethod
    def _scope_note(snap: dict) -> str:
        """给模型的硬边界：只说这局参战国，别把别处的仗补进来。"""
        player = snap.get("player") or "你"
        allies = snap.get("allies") or []
        enemies = snap.get("enemies") or []
        bits = [f"本国 {player}"]
        if allies:
            bits.append("盟友 " + "、".join(allies[:8]))
        if enemies:
            bits.append("敌对国 " + "、".join(enemies[:8]))
        if not allies and not enemies:
            return ("这次还没认出与你交战的国家，先不报其他国家的战况；"
                    "别拿别处的交火或调动补成自己的战况。")
        return ("只报参与你这局战争的国家：" + "；".join(bits) + "。"
                "其他国家的交火、兵力调动和省份易主都不在情报里，"
                "不要推断、不要补述、不要拿它们当背景。")

    def _side_text(self, row: dict) -> str:
        """优先报「这块地归谁 + 攻守双方」，读不到才降级到省内兵力。"""
        atk, dfs = row.get("attacker") or {}, row.get("defender") or {}
        ctrl = row.get("controller") or ""
        head = f"{ctrl}的地盘，" if ctrl else ""
        if atk or dfs:
            return f"{head}攻 {self._nations(atk)}；守 {self._nations(dfs)}"
        sides = []
        for s in row.get("sides") or []:
            tag = "（你）" if s.get("mine") else ""
            sides.append(f"{s.get('country')}{tag} {s.get('units')} 个师")
        return head + ("、".join(sides) if sides else "省内兵力没读出来")

    def _label(self, pid: int) -> str:
        try:
            return self._plugin._place_label(int(pid))
        except Exception:  # noqa: BLE001 拿不到名字就报省号
            return f"省 {pid}"

    def _control_text(self, snap: dict, ctl: dict) -> str:
        """我国 / 盟友 / 敌对国各控制多少省。

        0.9.9：以前这里顺手报了「全服多少个省、分属多少个控制国」。那串数字
        对玩家没用，而且会让她以为自己看到了整张地图。现在只列参战国。
        """
        player = snap.get("player") or "你"
        pid = int(snap.get("player_id") or 0)
        names = ctl.get("names") or {}
        counts = ctl.get("counts") or {}
        if not ctl.get("ok"):
            return ("控制图这次没读到（" + (ctl.get("error") or "扫描失败")
                    + "），下面的控制者只覆盖交火省，别当成全局版图。")
        by_name = {v: k for k, v in names.items()}
        allies = snap.get("allies") or []
        enemies = snap.get("enemies") or []
        out = [f"版图（只列与这局有关的国家）：我国（{player}）控制 "
               f"{counts.get(pid, 0)} 个省。"]
        friendly = [f"{nm} {counts.get(by_name[nm], 0)} 个省"
                    for nm in allies if nm in by_name]
        if friendly:
            out.append("盟友控制：" + "、".join(friendly[:8]) + "。")
        enemy_bits = [f"{nm} {counts.get(by_name[nm], 0)} 个省"
                      for nm in enemies if nm in by_name]
        if enemy_bits:
            out.append("交火对手控制：" + "、".join(enemy_bits[:8]) + "。")
        elif enemies:
            out.append("交火对手：" + "、".join(enemies[:8]) + "（控制省数没对上国名表）。")
        lost = [f"{self._label(r['prov'])}（现由{r.get('controller') or '他国'}控制）"
                for r in (snap.get("combats") or [])
                if r.get("owner") == player and r.get("controller")
                and r.get("controller") != player]
        if lost:
            out.append("交火省里法理属我国、现在被占的：" + "、".join(lost[:6]) + "。")
        return "\n".join(out)

    def _context(self, snap: dict, ctl: dict, facts: str,
                 control_text: str) -> dict:
        """留给模型的全量情报。宿主 chat 端点不通时靠 `action=context` 拉出来。"""
        names = ctl.get("names") or {}
        relevant = set((snap.get("war_scope") or {}).get("relevant") or [])
        # 0.9.9：全服控制国统计不再进上下文。她要的是自己这局的盘面，
        # 不是一张她根本看不到、也不该知道的世界版图。
        counts = {nm: v for k, v in (ctl.get("counts") or {}).items()
                  for nm in [names.get(k, f"国家#{k}")]
                  if not relevant or nm in relevant}
        try:
            oob = self._plugin._oob_text() or ""   # 0.9.9 兵力架构
        except Exception:  # noqa: BLE001 没做出来也不能挡住前线情报
            oob = ""
        return {
            "ts": time.time(),
            "player": snap.get("player") or "",
            "player_id": snap.get("player_id") or 0,
            "count": snap.get("count") or 0,
            "allies": snap.get("allies") or [],
            "enemies": snap.get("enemies") or [],
            # 0.9.9 补丁：范围字典里的全服交火计数只是诊断用的，不能进上下文
            "war_scope": warscope.public(snap.get("war_scope")),
            "scope_note": self._scope_note(snap),
            "oob": oob,
            "combats": [
                {"prov": r.get("prov"), "label": self._label(r["prov"]),
                 "controller": r.get("controller") or "",
                 "owner": r.get("owner") or "",
                 "attacker": r.get("attacker") or {},
                 "defender": r.get("defender") or {},
                 "sides": r.get("sides") or []}
                for r in (snap.get("combats") or [])],
            "control_ok": bool(ctl.get("ok")),
            "control_counts": counts,
            "control_summary": control_text,
            "text": facts,
        }

    def _report_overview(self, snap: dict, live: livegame.LiveGame,
                         ctl: dict) -> None:
        """开局：相关交火的全量 facts + 兵力架构交给模型，brief 只用于降级播报。"""
        n = int(snap.get("count") or 0)
        player = snap.get("player") or "你"
        scope_note = self._scope_note(snap)
        control_text = self._control_text(snap, ctl)
        rows = snap.get("combats") or []
        try:
            oob = self._plugin._oob_text() or ""
        except Exception:  # noqa: BLE001 编制没读出来不影响前线
            oob = ""
        if not n:
            facts = (f"前线总览：现在没有与{player}这局战争有关的陆上交火。\n"
                     + scope_note + "\n" + control_text)
            if oob:
                facts += "\n\n" + oob
            self._log("前线通报（overview）：" + facts.replace("\n", " ")[:400])
            self._plugin._frontline_report(
                facts, brief=facts,
                context=self._context(snap, ctl, facts, control_text))
            return
        lines = [f"前线总览：与你这局有关的交火 {n} 处。", scope_note,
                 control_text, "", "这些交火地区（全量，一处不落）："]
        for row in rows:                  # ← 相关交火全量，一处不落
            lines.append(f"  {self._label(row['prov'])}：{self._side_text(row)}")
        if oob:
            lines.extend(["", oob])
        facts = "\n".join(lines)
        brief_lines = [lines[0], scope_note, ""]
        for row in rows[:BRIEF_ROWS]:
            brief_lines.append(f"  {self._label(row['prov'])}：{self._side_text(row)}")
        if n > BRIEF_ROWS:
            brief_lines.append(f"另外还有 {n - BRIEF_ROWS} 处，问一句「前线怎么样」就能展开。")
        if oob:
            brief_lines.append("（另外读到了兵力架构，问一句「我的集团军怎么编的」就能展开。）")
        brief = "\n".join(brief_lines)
        total = (snap.get("war_scope") or {}).get("total_combats", n)
        self._log(f"前线通报（overview）：相关交火 {n} 处 / 全服 {total} 处，"
                  f"已交给模型")
        self._plugin._frontline_report(
            facts, brief=brief,
            context=self._context(snap, ctl, facts, control_text))

    def _report_advances(self, snap: dict, ctl: dict) -> None:
        """省份易主 → "XX 推进到了某省"，但**只报这局参战国的**。

        0.9.9 修的就是这里：全服控制图 diff 会把每个国家的占领都当成
        "前线变化"播出去。玩家在欧洲，东亚两个 AI 互啃也会被报成兵力调动。
        现在两边都跟参战范围无关就整条丢掉，连"另有 N 处易主"都不汇总。
        """
        new_map = ctl.get("prov_ctrl") or {}
        old_map = self._last_ctrl or {}
        names = ctl.get("names") or {}
        player = snap.get("player") or ""
        player_id = int(snap.get("player_id") or 0)
        allies = set(snap.get("allies") or [])
        enemies = set(snap.get("enemies") or [])
        relevant = set((snap.get("war_scope") or {}).get("relevant") or [])
        if not relevant:
            self._log("前线通报：还没认出参战国，这次省份易主先不报（别处的仗不报）")
            return
        by_country: dict[int, list[tuple[int, int]]] = {}
        skipped = 0
        for pid, cid in new_map.items():
            prev = old_map.get(pid)
            if prev is None or prev == cid:
                continue                 # 新扫进来的省不算"推进"
            nm = names.get(cid) or f"国家#{cid}"
            prev_nm = names.get(prev) or f"国家#{prev}"
            if not warscope.advance_relevant(nm, prev_nm, relevant):
                skipped += 1
                continue
            by_country.setdefault(cid, []).append((pid, prev))
        if skipped:
            self._log(f"前线通报：另有 {skipped} 处省份易主跟这局无关，已略去")
        if not by_country:
            return
        # 我方优先，其次按这次拿下的省数排
        order = sorted(by_country, key=lambda c: (c != player_id,
                                                  -len(by_country[c])))
        events = [(cid, pid, prev) for cid in order
                  for pid, prev in by_country[cid]]
        for cid, pid, prev in events[:MAX_ADVANCE]:
            nm = names.get(cid) or f"国家#{cid}"
            if cid == player_id or nm == player:
                tag = "（我方）"
            elif nm in allies:
                tag = "（盟友）"
            elif nm in enemies:
                tag = "（敌方）"
            else:
                tag = ""
            prev_nm = names.get(prev) or f"国家#{prev}"
            self._notify(f"前线变化：{nm}{tag}推进到了{self._label(pid)}"
                         f"（原属{prev_nm}）", kind="advance")
        if len(events) > MAX_ADVANCE:
            tally: dict[str, int] = {}
            for cid, _pid, _prev in events[MAX_ADVANCE:]:
                nm = names.get(cid) or f"国家#{cid}"
                tally[nm] = tally.get(nm, 0) + 1
            summary = "、".join(f"{k} {v} 处" for k, v in
                               sorted(tally.items(), key=lambda kv: -kv[1])[:5])
            self._notify(f"前线变化：另有 {len(events) - MAX_ADVANCE} 处省份易主"
                         f"（{summary}）。", kind="advance")

    def _diff(self, snap: dict, live: livegame.LiveGame, ctl: dict) -> None:
        old = self._last or {}
        old_by = {int(r["prov"]): r for r in (old.get("combats") or [])}
        new_by = {int(r["prov"]): r for r in (snap.get("combats") or [])}

        # 1) 全服易主（最像用户要的"推进到某省"），先报
        if ctl.get("ok") and self._last_ctrl:
            self._report_advances(snap, ctl)

        # 2) 交火开始 / 结束 / 增兵
        for pid, row in new_by.items():
            if pid not in old_by:
                self._notify(f"前线变化：{self._label(pid)}打起来了——{self._side_text(row)}",
                             kind="combat_start")
                continue
            old_side = {s.get("addr"): s for s in (old_by[pid].get("sides") or [])}
            for s in row.get("sides") or []:
                before = old_side.get(s.get("addr"))
                if before is None:
                    who = s.get("country")
                    self._notify(f"前线变化：{who} 的部队进入{self._label(pid)}"
                                 f"（现在 {s.get('units')} 个师）",
                                 kind="advance")
                elif int(s.get("units") or 0) > int(before.get("units") or 0):
                    who = s.get("country")
                    self._notify(f"前线变化：{who} 在{self._label(pid)}增兵到 {s.get('units')} 个师",
                                 kind="reinforce")

        for pid, row in old_by.items():
            if pid not in new_by:
                self._notify(f"前线变化：{self._label(pid)}的交火结束了。", kind="combat_end")

    def _notify(self, text: str, kind: str = "note") -> None:
        text = str(text or "").strip()
        if not text:
            return
        self._log(f"前线通报（{kind}）：{text.replace(chr(10), ' ')}")
        # 0.9.20：**不再往聊天窗播报**。用户要的是"战况汇报别在聊天框告诉我，
        # 直接告诉 YUI 就行"，所以这类变化只记进她的笔记；等她自己的简报
        # （`_command_loop` → `_brief_push`）或用户开口问的时候再带出来。
        self._plugin._front_remember(text, kind=kind)

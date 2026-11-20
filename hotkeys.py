"""游戏内热键：F7 把这支部队交给我，F8 放回去。

## 为什么热键得在进程里做

HOI4 的 mod 脚本没有"按键触发效果"这个接口。`interface/*.gui` 里
`buttonType` 确实能写 `shortcut = "z"`（作战计划那几个按钮就是这么绑的），
但那只对游戏自己认识的按钮名生效——我们加的按钮没有对应的代码分支，
按了也不会做事。所以只能在游戏进程里轮询键盘。

## 这个线程干什么

1. 每 0.3 秒问一次代理："有没有新按键"
2. 有 F7 就找出玩家国家、列出师、算出鼠标现在指着哪个省
3. 那个省里的师登记进"接管表"，之后 YUI 调兵只动这些
4. 把这次动作记成一句话，YUI 下次回话时能说出来（不然用户按了没反应会以为坏了）
5. 成败都响一声：成功一声高音，失败两声低音

## 已知断链（2026-11-08）

"鼠标指着的省"要靠地图标定换算，而 0.5.0 之后调兵划线全走进程内，
再没有任何功能会做标定——所以 F7 现在永远走"算不出鼠标指哪"的失败分支。
临时出路：让用户直接跟猫娘说「把 XX 的部队交给你」（hoi4_squad assign，按地名，不碰鼠标）。

## 终局方案（等运行时验证后切换）

用户要的语义是"**选中的**部队给我"：选中一个集团军给一个集团军，
选中一个集团军群给一个集团军群。静态逆向已经定位到
`CGameIdler + 0x508 = CSelectionList`（vtable 0x23EF1B8，构造器 0x25A110），
下一步在运行时 dump 这个对象、配合"游戏里点选一个军"做差分，
就能把 `_cursor_province()` 换成 `_selected_units()`，上层 `_assign/_release`
只换取数那一行。"""


from __future__ import annotations

import threading
import time

from . import cache as cache_mod, livegame, mapview, nekoagent, prov_index, squads as squads_mod

__all__ = ["HotkeyWatch"]

CODE_NAME = {1: "F7", 2: "F8"}


class HotkeyWatch(threading.Thread):
    def __init__(self, data_dir, log=None, interval: float = 0.3) -> None:
        super().__init__(daemon=True, name="hoi4-hotkeys")
        self._data_dir = data_dir
        self._log = log or (lambda msg: None)
        self._interval = float(interval)
        self._stop_evt = threading.Event()
        self._seq = 0
        self._idx = None
        self._idx_wid = ""
        self._ag = None

    # ── 生命周期 ──────────────────────────────

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        # 代理还没注入时 ensure() 会注一次；游戏没开就慢慢等，别刷日志
        while not self._stop_evt.is_set():
            try:
                if self._ag is None:
                    self._ag = nekoagent.ensure(wait=6.0)
                resp = self._ag.cmd(f"HOTKEYS {self._seq}")
                cur, events = self._parse(resp)
                if cur:
                    self._seq = cur
                for code in events:
                    self._on_key(code)
            except nekoagent.AgentError as exc:
                # 旧版代理不认 HOTKEYS。这不是"偶尔断了"，重连一万次也一样，
                # 所以直接停掉线程并留一句话，别每两秒刷一次日志。
                if "unknown" in str(exc).lower():
                    self._note(
                        "游戏里跑的代理是旧版（还没有热键功能），所以 F7/F8 现在不生效。"
                        "把游戏重启一次就好了——新版 DLL 会跟着插件一起注进去。",
                        ok=False)
                    self._stop_evt.set()
                    return
                self._log(f"热键轮询断了：{exc}")
                self._drop()
                self._stop_evt.wait(2.0)
                continue
            except Exception as exc:  # noqa: BLE001 任何错误都只是"这轮没轮询到"
                self._log(f"热键轮询断了：{type(exc).__name__}: {exc}")
                self._drop()
                self._stop_evt.wait(2.0)
                continue
            self._stop_evt.wait(self._interval)

    def _drop(self) -> None:
        try:
            if self._ag is not None:
                self._ag.close()
        except Exception:  # noqa: BLE001
            pass
        self._ag = None

    @staticmethod
    def _parse(resp: str) -> tuple[int, list[int]]:
        """OK cur=7 n=2 1@123456 2@123999 -> (7, [1, 2])"""
        parts = resp.split()
        if not parts or parts[0] != "OK":
            return 0, []
        cur = 0
        out: list[int] = []
        for tok in parts[1:]:
            if tok.startswith("cur="):
                try:
                    cur = int(tok[4:])
                except ValueError:
                    cur = 0
            elif "@" in tok:
                try:
                    out.append(int(tok.split("@", 1)[0]))
                except ValueError:
                    continue
        return cur, out

    # ── 单次处理 ──────────────────────────────

    def _on_key(self, code: int) -> None:
        self._log(f"热键：{CODE_NAME.get(code, code)}")
        try:
            if code == 1:
                self._assign()
            elif code == 2:
                self._release()
        except livegame.LiveError as exc:
            self._note(exc.message, ok=False)
        except nekoagent.AgentError as exc:
            self._note(exc.message, ok=False)
        except Exception as exc:  # noqa: BLE001
            self._note(f"按了键但没办成：{type(exc).__name__}: {exc}", ok=False)

    def _assign(self) -> None:
        prov = self._cursor_province()
        if not prov:
            self._note(
                "按了 F7，但我看不出鼠标现在指着哪个省。地图标定这条路已经废了，"
                "换个办法：直接跟猫娘说「把柏林的部队交给你」（说地名就行）。"
                "下一版会改成读你选中的部队，到时候选中谁就给谁。",
                ok=False)
            return
        live = livegame.LiveGame(self._data_dir, log=self._log)
        player = live.confirmed_country() or live.locate(*self._player_hint())
        units = live.units(player["addr"])
        group = [u for u in units if int(u["prov"]) == int(prov)]
        if not group:
            self._note(f"省 {prov} 里没有你的部队，所以没接管。"
                       + ("" if not units else f"（你现在共 {len(units)} 个师）"),
                       ok=False)
            return
        sq = squads_mod.Squads(self._data_dir, log=self._log).add(
            player["addr"], player["short"], group,
            label=f"{player['short']}·省 {prov}·{len(group)} 师")
        self._note(
            f"接管了 {len(group)} 个师（省 {prov}，第 {sq['id']} 批）。"
            f"我现在手上共 {len(squads_mod.Squads(self._data_dir).unit_addrs())} 个师。")

    def _release(self) -> None:
        prov = self._cursor_province()
        sqs = squads_mod.Squads(self._data_dir, log=self._log)
        data = sqs.load()
        if not data.get("squads"):
            self._note("本来就没有部队归我指挥。", ok=False)
            return
        if not prov:
            got = sqs.release(all_of=True)
            self._note(f"鼠标不在图上，就把全部 {got['released']} 个师都放回去了。")
            return
        targets = {int(u["addr"]) for sq in data["squads"] for u in sq.get("units") or []
                   if int(u.get("prov") or 0) == int(prov)}
        if not targets:
            self._note(f"省 {prov} 里没有我指挥的部队，什么都没改。", ok=False)
            return
        got = sqs.release(addrs=targets)
        self._note(f"放回去 {got['released']} 个师（省 {prov}），我手上还剩 {got['left']} 个。")

    # ── 工具 ──────────────────────────────────

    def _player_hint(self) -> tuple[str, str]:
        snap = cache_mod.read_cache(self._data_dir) or {}
        meta = snap.get("meta") or {}
        return str(meta.get("player") or ""), str(meta.get("player_name") or "")

    def _cursor_province(self) -> int:
        """鼠标现在指着哪个省。指不到（在上班/海面/界面外）返回 0。"""
        if self._ag is None:
            return 0
        resp = self._ag.cmd("CURSOR")
        if "cursor=" not in resp:
            return 0
        try:
            body = resp.split("cursor=", 1)[1].split()[0]
            sx, sy = (int(v) for v in body.split(","))
        except (IndexError, ValueError):
            return 0

        view = mapview.load(self._data_dir)
        if view is None:
            self._log("热键：还没有地图标定，鼠标指哪算不出来")
            return 0
        try:
            from . import panel as _panel
            wid = _panel.current_world_id()
        except Exception:
            wid = ""
        if self._idx is None or self._idx_wid != wid:
            self._idx = prov_index.load()
            self._idx_wid = wid
        if self._idx is None:
            return 0
        bx, by = view.to_bmp(sx, sy)
        if not (0 <= bx < self._idx.map_w and 0 <= by < self._idx.map_h):
            return 0
        return self._idx.nearest(bx, by)

    def _note(self, text: str, ok: bool = True) -> None:
        self._log(f"热键：{text}")
        # 成败都响：成功一声高音，失败两声低音。不然按了没动静，
        # 用户分不清"没生效"和"坏了"。
        try:
            import ctypes

            if ok:
                ctypes.windll.kernel32.Beep(880, 90)
            else:
                ctypes.windll.kernel32.Beep(400, 70)
                ctypes.windll.kernel32.Beep(400, 70)
        except Exception:  # noqa: BLE001 响不了就算了，别把线程搞崩
            pass
        try:
            sq = squads_mod.Squads(self._data_dir, log=self._log)
            data = sq.load()
            data["last"] = {"text": text, "ok": bool(ok), "at": int(time.time())}
            sq.save(data)
        except Exception:  # noqa: BLE001 记不上就算了，别把线程搞崩
            pass

"""存档监听：变了就后台解析，解析完落缓存。

为什么必须是后台线程（而不是工具里现解析）：
实测一份 41.6 MB 的存档要 15～25 秒（同一份文件测三次：13.2 / 21.3 / 24.7）。
放进对话路径就是对话卡半分钟。所以解析只发生在"存档变了"那一刻，
工具永远只读缓存（毫秒级）。

三个必须处理的现实问题：
1. **游戏正在写存档**时 mtime 已经变了，但内容不完整。所以要求两次 stat 一致才动手。
2. **解析失败不能把状态锁死**，否则一次失败之后永远不再重试。mtime 只在成功后更新。
3. **日志要自己写**。插件自己的 logger 不进 N.E.K.O 的日志文件（实测过）。
"""

from __future__ import annotations

import threading
import time
import traceback
from pathlib import Path
from typing import Callable

from . import act_log, logs, save_meta
from .cache import cache_matches_save, read_cache, write_cache
from .config import pick_game_dir
from .save_state import load_state

__all__ = ["SaveWatcher"]


class SaveWatcher(threading.Thread):
    """轮询存档目录，变化后解析并落缓存。"""

    def __init__(
        self,
        resolve_paths: Callable[[], dict],
        data_dir: Path,
        interval: float = 4.0,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(name="hoi4-save-watcher", daemon=True)
        self._resolve = resolve_paths
        self._data_dir = Path(data_dir)
        self._interval = max(1.0, float(interval))
        self._logger = logger or (lambda msg: None)
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._status: dict = {
            "state": "starting",
            "save_file": "",
            "save_date": "",
            "parsed_at": 0.0,
            "parse_seconds": 0.0,
            "error": "",
            "attempts": 0,
            # 0.9.8：头部指纹，不用解析整份就能回答"这是哪份档"
            "save_player": "",
            "save_version": "",
            "save_age_days": -1,
            "save_match": "unknown",
            # 0.9.11：最新那份档是二进制（读不了）时的详情 + 自愈结果
            "binary_blocker": None,
        }
        self._last_ok_key: tuple[str, int, float] | None = None
        # 同一份读不了的档（二进制/残档）只在首次或换档时记一次，别每 4 秒刷屏
        self._last_head_fail_key: tuple[str, int, float] | None = None
        # 只对某个 userdir 自愈一次，别每 4 秒重写 settings.txt
        self._healed_dirs: set[str] = set()

    # ── 对外状态 ──────────────────────────────────

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def _set(self, **kwargs) -> None:
        with self._lock:
            self._status.update(kwargs)

    # ── 线程主体 ──────────────────────────────────

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        # 先把已有缓存认下来，插件重启后别白等一轮
        self._adopt_existing_cache()
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001 线程绝不能死
                self._set(state="error", error=f"{type(exc).__name__}: {exc}")
                self._log(f"轮询异常：{exc}\n{traceback.format_exc()}")
            self._stop_event.wait(self._interval)

    def _adopt_existing_cache(self) -> None:
        try:
            paths = self._resolve()
        except Exception:  # noqa: BLE001
            return
        save_file = paths.get("save_file")
        cache = read_cache(self._data_dir)
        if save_file and cache_matches_save(cache, save_file):
            meta = cache.get("meta") or {}
            head = meta.get("save_head") or save_meta.head_meta(save_file)
            judge = meta.get("save_match") or {}
            # 0.9.8 之前的缓存没有 save_head / save_match，0.9.9 之前没有 oob。
            # 认下旧缓存可以马上回答，但必须**重解析一次**把新字段补进去，
            # 否则 _last_ok_key 一设，后面每轮都直接 return，永远停在旧格式
            # （编制就一直读不出来）。
            fresh = bool(meta.get("save_head") and meta.get("save_match")
                         and cache.get("oob"))
            self._set(
                state="ready" if fresh else "parsing",
                save_file=str(save_file),
                save_date=meta.get("date_text") or meta.get("save_date", ""),
                parsed_at=float(meta.get("parsed_at") or 0),
                parse_seconds=float(meta.get("parse_seconds") or 0),
                save_player=head.get("player", ""),
                save_version=head.get("version_num", ""),
                save_age_days=head.get("age_days", -1),
                save_match=judge.get("match", "unknown"),
            )
            if fresh:
                try:
                    st = Path(save_file).stat()
                    self._last_ok_key = (str(save_file).lower(), st.st_size, st.st_mtime)
                except OSError:
                    pass

    def _handle_binary_blocker(self, paths: dict) -> None:
        """最新档是**读不了的**二进制时：记详情 + 游戏没运行就把存档格式改回文本。

        0.9.12 起插件能直接解析 HOI4bin（pdx_binary），正常二进制档不再触发
        这里。只剩头部指纹都读不出来的档（未来新版本、字段表没覆盖到）才算
        blocker——那种档退回文本格式肯定能读，所以自愈保留为兜底。
        （0.9.11 前的故障：便携版默认 save_as_binary=yes，所有新档都读不了，
        插件退回去读几个月前另一局的文本档、把旧数据当现状报。）
        自愈只在游戏**没运行**时做——游戏退出会用内存里的设置覆盖 settings.txt，
        运行中改等于白改。改完之后用户下次进游戏、存一次档就是文本，能读了。
        """
        blocker = paths.get("binary_blocker")
        self._set(binary_blocker=blocker)
        if not blocker:
            return
        game = paths.get("game") or save_meta.game_process_cached()
        if game.get("pid"):
            # 游戏在跑，改不动（退出时会用内存里的设置覆盖 settings.txt）。
            # 清空"已自愈"记录：万一上次自愈后用户又秒开游戏把它写回了 yes，
            # 这次退出还能再修一遍。
            if self._healed_dirs:
                self._healed_dirs.clear()
            return
        userdir = blocker.get("userdir") or ""
        if not userdir or userdir in self._healed_dirs:
            return
        try:
            from . import config as _cfg

            if not _cfg.load_config().get("auto_text_saves", True):
                return
        except Exception:  # noqa: BLE001 读不到配置就按默认（开）处理
            pass
        res = save_meta.disable_binary_saves(userdir)
        self._healed_dirs.add(userdir)
        if res.get("changed"):
            self._log(f"存档格式自愈：{res.get('reason')}")
        elif res.get("reason"):
            self._log(f"存档格式没改：{res.get('reason')}")

    def _tick(self) -> None:
        paths = self._resolve()
        save_file = paths.get("save_file")
        game_dir = paths.get("game_dir")

        # 0.9.12：最新那份档若是读不了的二进制，记详情；游戏没运行时顺手自愈。
        # （可读的二进制档直接走正常解析，不算 blocker。）
        self._handle_binary_blocker(paths)

        if save_file is None:
            self._set(state="no_save", error="没找到存档文件")
            return
        if game_dir is None:
            self._set(state="no_game", error="没找到游戏安装目录，州名会是 id")
            # 没有游戏目录也能解析（只是没有州名），所以继续往下走

        try:
            st = Path(save_file).stat()
        except OSError as exc:
            self._set(state="error", error=f"读不到存档：{exc}")
            return

        key = (str(save_file).lower(), st.st_size, st.st_mtime)
        if key == self._last_ok_key:
            if self.status().get("state") != "ready":
                self._set(state="ready")
            return
        # 这份档上一轮已经判定读不了（二进制/残档），state 也设过了。别再每 4 秒
        # sleep 1.5s + 重读头部 + 刷日志；等它 mtime 变了（用户重新存盘）再处理。
        if key == self._last_head_fail_key:
            if self.status().get("state") != "error":
                self._set(state="error")
            return

        # 游戏可能正在写盘：等一个间隔再确认一次
        time.sleep(1.5)
        try:
            st2 = Path(save_file).stat()
        except OSError:
            return
        if (st2.st_size, st2.st_mtime) != (st.st_size, st.st_mtime):
            self._log("存档还在写，等下一轮")
            return

        # 0.9.8：解析 40 MB 之前，先用 8 KB 文件头确认"这是哪份档"。
        # 读不了的二进制档（0.9.12 起正常二进制档头部可直读）、写了一半的残档、
        # 伪装成 .hoi4 的别的文件，以前要等二十多秒解析失败才知道，
        # 现在直接给出能照做的提示。
        head = paths.get("save_head") or save_meta.head_meta(save_file)
        game = paths.get("game") or save_meta.game_process_cached()
        if head.get("error") or not head.get("player"):
            err = head.get("error") or "存档头部读不到 player/date，可能正写到一半"
            self._set(state="error", save_file=str(save_file), error=err)
            self._last_head_fail_key = key
            self._log(f"存档头部不通过 {Path(save_file).name}：{err}")
            return

        self._set(
            state="parsing", save_file=str(save_file), error="",
            save_player=head.get("player", ""),
            save_version=head.get("version_num", ""),
            save_age_days=head.get("age_days", -1),
        )
        started = time.perf_counter()
        try:
            # 按**存档**版本先挑对数据目录。老流程是"先随便挑一个解析完再换"，
            # 第一次的名称映射白做一遍，手动指定过 game_dir 时还会一直错。
            candidates = paths.get("game_dirs_all") or []
            if len(candidates) > 1 and head.get("version_num"):
                best = pick_game_dir(candidates, head["version_num"])
                if best is not None:
                    game_dir = best
            state = load_state(save_file, game_dir, self._data_dir / "cache")
            snap = state.snapshot()

            # 存档版本只有解析完才知道（.hoi4 是 zip，头部读不到）。
            # 数据目录版本对不上时，州名/部队位置会整体错位。
            # 所以这里按存档版本重挑一次数据目录，再跑一遍名称映射——
            # 存档树已经在内存里，不用重新解析那 40 MB。
            candidates = paths.get("game_dirs_all") or []
            if len(candidates) > 1 and state.version:
                best = pick_game_dir(candidates, state.version)
                if best is not None and str(best) != str(state.game_dir or ""):
                    state.switch_game_dir(best)
                    snap = state.snapshot()

            elapsed = time.perf_counter() - started
            meta = snap.setdefault("meta", {})
            meta["save_mtime"] = st2.st_mtime
            meta["save_size"] = st2.st_size
            meta["parsed_at"] = time.time()
            meta["parse_seconds"] = round(elapsed, 1)
            meta["save_path"] = str(Path(save_file).resolve())

            # 头部指纹 + "是不是当前这局"的判定，一起落进缓存。
            # 工具侧只读缓存，不再去查进程、不再碰游戏内存。
            judge = save_meta.judge_live(head, game=game)
            label = meta.get("player_name") or head.get("player") or "?"
            meta["save_head"] = {
                k: head.get(k)
                for k in (
                    "format", "player", "date", "date_text", "version", "version_num",
                    "ideology", "difficulty", "game_unique_id", "session",
                    "mtime", "size", "age_days",
                )
            }
            meta["save_match"] = judge
            meta["save_banner"] = save_meta.banner(head, judge, label)

            # 0.9.9：兵力架构（集团军群→集团军→师）直接从**这棵已解析的树**
            # 组装，零额外解析成本。模板/名字组/汉化表都在游戏目录里，
            # 换过数据目录就用换过的那份。
            try:
                from . import localisation as loc_mod
                from . import oob as oob_mod

                used_dir = state.game_dir or game_dir
                loc = loc_mod.load_localisation(used_dir, self._data_dir / "cache")
                snap["oob"] = oob_mod.build(state.root, used_dir, loc)
                totals = snap["oob"].get("totals") or {}
                self._log(
                    f"兵力架构：{totals.get('divisions', 0)} 个师 / "
                    f"{totals.get('armies', 0)} 个集团军 / "
                    f"{totals.get('army_groups', 0)} 个集团军群"
                )
            except Exception as exc:  # noqa: BLE001 编制读不到不影响存档解析
                snap["oob"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                self._log(f"兵力架构没读出来：{type(exc).__name__}: {exc}")

            if not write_cache(self._data_dir, snap):
                raise OSError("缓存写入失败")

            # 上次投递的命令能不能在这份档里看到——写侧唯一的验证手段
            try:
                judged = act_log.reconcile(self._data_dir, snap)
                for rec in judged:
                    self._log(
                        f"核对投递 {rec.get('action')} {rec.get('amount')} → "
                        f"{act_log.label_of(rec.get('state', ''))} "
                        f"（{rec.get('result_delta')} / 存于 {rec.get('checked_save_date')}）"
                    )
            except Exception as exc:  # noqa: BLE001 核对失败不能影响解析结果
                self._log(f"核对投递记录失败：{type(exc).__name__}: {exc}")

            self._last_ok_key = key
            self._last_head_fail_key = None
            self._set(
                state="ready",
                save_date=meta.get("date_text", ""),
                parsed_at=meta["parsed_at"],
                parse_seconds=meta["parse_seconds"],
                error="",
                attempts=self.status().get("attempts", 0) + 1,
                save_player=head.get("player", ""),
                save_version=head.get("version_num", ""),
                save_age_days=head.get("age_days", -1),
                save_match=judge.get("match", "unknown"),
            )
            self._log(
                f"解析完成 {Path(save_file).name} "
                f"{meta.get('save_date')} 用时 {elapsed:.1f}s "
                f"{meta.get('player')} "
                f"版本 {meta.get('save_version')}"
            )
            self._log(
                f"存档识别：{head.get('date_text')} {head.get('player')} "
                f"{head.get('version_num')}，文件距今 {head.get('age_days', 0):.0f} 天，"
                f"判定 {judge.get('match')}（{'；'.join(judge.get('reasons') or []) or '无冲突'}）"
            )
        except Exception as exc:  # noqa: BLE001 解析失败要能重试
            elapsed = time.perf_counter() - started
            self._set(state="error", error=f"{type(exc).__name__}: {exc}", parse_seconds=round(elapsed, 1))
            self._log(f"解析失败（{elapsed:.1f}s）：{exc}\n{traceback.format_exc()}")

    # ── 日志 ──────────────────────────────────────

    def _log(self, msg: str) -> None:
        # 自己落盘：宿主的 logger 不进 N.E.K.O 的日志文件（实测）
        logs.write(self._data_dir, msg)
        try:
            self._logger(msg)
        except Exception:  # noqa: BLE001
            pass

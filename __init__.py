"""钢四参谋插件。

定位：不是外挂，是参谋。核心价值是"她能看见你漏看的东西"——
存档里有 334 个国家的完整数据，游戏界面只给你看很少一部分。

两条腿：
- 读（P0）：存档变了 → 后台解析（15～30 秒，不进对话路径）→ 裁剪落缓存 → 工具毫秒级读
- 写（P2）：DLL 注入进程内 → PostMessage 投递控制台命令 → **只做白名单里的低风险加成**

写侧的三条自我约束（越线就从参谋变成外挂了）：
1. 不做部队调动（moveunit / tp）。最容易毁档，给建议、让用户自己点。
2. 命令只能确认"投递了"，不能确认"生效了"。生效与否靠下一次存档对比，不许假装成功。
3. 控制台状态不可知。用户手动按过 ` 之后可能全打空——所以每条命令都要留下核对记录。

两条硬规矩（写在代码里防止以后自己忘）：
1. 工具的返回路径上绝对不能解析存档。
2. 任何结论都要带上"这是哪一天的存档"，否则她会拿三周前的兵力给你出主意。
"""

# ── import 探针 ──────────────────────────────
# 2026-11-08：插件进程曾连第一行日志都来不及写就死（start_plugin failed，
# 无 traceback）。这段在每个 import 节点落盘打点，死在哪一步一眼可见。
#
# 开关：环境变量 `HOI4_TRACE` 指定文件路径才启用，不给就整段空转。
# 早先这里写死过一台机器的绝对路径，发布版不能带这种私货。

import os as _os_probe
import time as _time_probe

_TRACE_PATH = _os_probe.environ.get("HOI4_TRACE", "")


def _trace(msg: str) -> None:
    if not _TRACE_PATH:
        return
    try:
        with open(_TRACE_PATH, "a", encoding="utf-8") as _f:
            _f.write(f"{_time_probe.strftime('%H:%M:%S')} {msg}\n")
    except Exception:  # noqa: BLE001 探针绝不影响主流程
        pass


_trace("0 模块开始 import")

import asyncio
import json
import os
import re
import threading
import time

_trace("1 标准库 OK")

from plugin.sdk.plugin import (
    NekoPluginBase,
    Ok,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
    ui,
)

_trace("2 SDK OK")

# 探针第二层：把真 SDK 的装饰器包一层，注册时异常直接落 traceback。
# 硬杀（os._exit / C 层崩）则表现为"进入有、成功无、异常无"。
import functools as _ft_probe
import traceback as _tb_probe

_llm_tool_raw = llm_tool
_plugin_entry_raw = plugin_entry


def llm_tool(**kw):  # noqa: F811 探针期临时替换，定位后删
    def _deco(fn):
        _trace(f"3.x llm_tool[{kw.get('name')}] 装饰器进入")
        try:
            out = _llm_tool_raw(**kw)(fn)
        except BaseException as exc:  # noqa: BLE001 探针要抓一切
            _trace(f"!! llm_tool[{kw.get('name')}] 异常 {type(exc).__name__}: {exc}\n"
                   + _tb_probe.format_exc())
            raise
        _trace(f"3.x llm_tool[{kw.get('name')}] 注册成功")
        return out
    return _deco


def plugin_entry(**kw):  # noqa: F811
    def _deco(fn):
        _trace(f"3.x plugin_entry[{kw.get('id')}] 装饰器进入")
        eid = str(kw.get("id") or "")
        if eid and _needs_startup(eid):
            fn = _gate(fn, eid)
        try:
            out = _plugin_entry_raw(**kw)(fn)
        except BaseException as exc:  # noqa: BLE001
            _trace(f"!! plugin_entry[{kw.get('id')}] 异常 {type(exc).__name__}: {exc}\n"
                   + _tb_probe.format_exc())
            raise
        _trace(f"3.x plugin_entry[{kw.get('id')}] 注册成功")
        return out
    return _deco


# ── 启动闸门 ─────────────────────────────────────────────────────
# 用户要的：面板里选存档 → 进度条注入代理 → **全部成功才能用**。
# 面板自己的入口（世界资料库/开始页）和体检不受限制，否则接不上就没法自救。

_GATE_EXEMPT_PREFIX = ("world_", "startup_", "panel_", "hoi4_panel")
_GATE_EXEMPT = {"hoi4_health", "hoi4_voice", "hoi4_link", "hoi4_refresh"}


def _needs_startup(entry_id: str) -> bool:
    if entry_id in _GATE_EXEMPT:
        return False
    return not entry_id.startswith(_GATE_EXEMPT_PREFIX)


def _startup_ready() -> bool:
    """问面板那边接没接上。问不到就当没接上——宁可挡一下，不放它去动部队。"""
    try:
        from . import panel as _p
        return bool(_p.startup_ready())
    except Exception:  # noqa: BLE001
        return False


_GATE_TEXT = (
    "还没接上这局游戏。打开插件面板的「开始」页：选一个存档 → 点「开始启动」→ "
    "等进度条走完（读大存档要 30~60 秒）。四步全绿之前我不会动你的部队。"
)


def _gate(fn, entry_id: str):
    """给入口套一层闸门。异步和同步都支持。"""
    import asyncio as _aio

    if _aio.iscoroutinefunction(fn):
        @_ft_probe.wraps(fn)
        async def _agate(*a, **k):
            if not _startup_ready():
                raise UiError(_GATE_TEXT)
            return await fn(*a, **k)
        return _agate

    @_ft_probe.wraps(fn)
    def _sgate(*a, **k):
        if not _startup_ready():
            raise UiError(_GATE_TEXT)
        return fn(*a, **k)
    return _sgate

from . import act_log, actions, cache as cache_mod, config, logs, render, save_meta
from . import livegame, mapops, nekoagent, places as places_mod, prov_index, screen
from . import battleplan as battleplan_mod

# 0.10.0 世界资料库：atlas 负责读游戏/mod 生成数据，panel 负责给 UI 供数据
from . import atlas as atlas_mod, panel as panel_mod
from . import hotkeys as hotkeys_mod, squads as squads_mod
from . import nekohost, mic as mic_mod, ptt as ptt_mod
from . import asr as asr_mod, gameguard as gameguard_mod
from . import hostroute as hostroute_mod, voiceorder as voiceorder_mod
from . import frontwatch as frontwatch_mod
from . import oob as oob_mod, localisation as loc_mod, warscope as warscope_mod
from . import zone as zone_mod, command as command_mod
# orders 是 0.4.x 的鼠标版划线，0.5.0 起已废弃（改走 battleplan），不再 import
from .bridge import BridgeService
from .ui import UiDriver, UiError
from .watcher import SaveWatcher

_trace("3 子模块 OK，开始类定义")

__all__ = ["HoI4AdvisorPlugin"]


def _s(x) -> str:
    """把参数安全地变成字符串。

    0.9.22 真机踩过：schema 里 `from_place` 写的是 string，模型却把省号直接
    发成数字（`from_place=6576`），接着 `.strip()` 就抛
    `'int' object has no attribute 'strip'`，整个 entry 500——用户看到的是
    "插件报错"，而不是"我帮你调兵了"。入口处一律过一遍这个函数。
    """
    if x is None:
        return ""
    return x if isinstance(x, str) else str(x)


@neko_plugin
class HoI4AdvisorPlugin(NekoPluginBase):
    """钢四参谋。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._watcher: SaveWatcher | None = None
        self._bridge_svc: BridgeService | None = None
        self._cfg: dict = {}
        self._start_error: str = ""
        self._ui_svc: UiDriver | None = None
        self._index = None  # ProvIndex，首次用到地图时加载
        self._index_wid = ""  # 缓存的是哪个世界的坐标，切世界要重读
        # 0.9.13：(tag→中文国名, tag→本土省列表) 备忘，读的是 watcher 落的缓存
        self._core_memo: tuple | None = None
        self._live_svc: livegame.LiveGame | None = None
        self._squads_svc: squads_mod.Squads | None = None
        self._hotkey_watch: hotkeys_mod.HotkeyWatch | None = None
        self._ptt: ptt_mod.PushToTalk | None = None
        self._asr: asr_mod.LocalAsr | None = None
        self._guard: gameguard_mod.GameGuard | None = None
        # 说话走宿主的"游戏路由"：她的话、她回的话都落在聊天窗里
        self._route: hostroute_mod.HostRoute | None = None
        # 语音调令要在一个**自己的**事件循环里跑（见 _order_loop 的注释）
        self._order_loop_box: asyncio.AbstractEventLoop | None = None
        # 上一条因为"认不出你玩哪国"没办成的调令，等用户回一句国家名
        self._pending_order: dict | None = None
        # 前线哨兵：开局总览 + 交火/推进通报
        self._front_watch: frontwatch_mod.FrontWatch | None = None
        self._front_notes: list[str] = []
        # 0.9.4：开局那份**全量**情报（所有交火省 + 全服控制版图）。
        # 宿主 chat 端点不通时，模型仍能用 hoi4_frontline action=context 拉到它。
        self._front_context: dict = {}
        # 0.9.20：开局总览改成静默留档（不生成、不播报），失败计数没意义了。

    # ── 启动 ──────────────────────────────────────

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        """起后台 watcher + 预热桥接。注意：钩子里绝不能 return Ok(...)。"""
        _trace("4 startup 钩子进入")
        try:
            self._ensure_watcher()
        except Exception as exc:  # noqa: BLE001 记录原因，别静默吞掉
            self._start_error = f"{type(exc).__name__}: {exc}"
            logs.write(config.data_dir(), f"启动失败：{self._start_error}")
        try:
            self._warm_bridge()
        except Exception as exc:  # noqa: BLE001
            logs.write(config.data_dir(), f"桥接预热失败：{type(exc).__name__}: {exc}")
        try:
            self._ensure_hotkeys()
        except Exception as exc:  # noqa: BLE001
            logs.write(config.data_dir(), f"热键没起来：{type(exc).__name__}: {exc}")
        try:
            self._ensure_game_guard()
        except Exception as exc:  # noqa: BLE001
            logs.write(config.data_dir(), f"游戏闸门没起来：{type(exc).__name__}: {exc}")
        try:
            self._ensure_ptt()
        except Exception as exc:  # noqa: BLE001
            logs.write(config.data_dir(), f"按住说话没起来：{type(exc).__name__}: {exc}")
        try:
            self._ensure_frontwatch()
        except Exception as exc:  # noqa: BLE001
            logs.write(config.data_dir(), f"前线哨兵没起来：{type(exc).__name__}: {exc}")
        # 本地识别模型 216 MB，加载要 6 秒左右，绝不能在启动钩子里等它
        try:
            threading.Thread(target=self._warm_asr, name="hoi4-asr-warm",
                             daemon=True).start()
        except Exception as exc:  # noqa: BLE001
            logs.write(config.data_dir(), f"识别预热没起来：{type(exc).__name__}: {exc}")
        _trace("5 startup 钩子返回")
        return None

    @lifecycle(id="shutdown")
    async def on_shutdown(self, **_):
        if self._front_watch is not None:
            self._front_watch.stop()
            self._front_watch = None
        if self._ptt is not None:
            self._ptt.stop()
            self._ptt = None
        if self._route is not None:
            # 把"游戏接管输入"还回去，聊天窗才恢复正常输入
            self._route.end()
            self._route = None
        if self._guard is not None:
            self._guard.stop()
            self._guard = None
        if self._hotkey_watch is not None:
            self._hotkey_watch.stop()
            self._hotkey_watch = None
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
        return None

    def _ensure_watcher(self) -> SaveWatcher:
        """按需启动 —— 不只依赖 startup 钩子（钩子失败过一次就再也不会起）。"""
        if self._watcher is not None and self._watcher.is_alive():
            return self._watcher
        self._cfg = config.load_config()
        self._watcher = SaveWatcher(
            resolve_paths=self._resolve,
            data_dir=config.data_dir(),
            interval=float(self._cfg.get("watch_interval", 4.0)),
        )
        self._watcher.start()
        return self._watcher

    def _resolve(self) -> dict:
        return config.resolve_paths(self._cfg or config.load_config())

    # ── 桥接（写侧） ──────────────────────────────

    def _bridge(self) -> BridgeService:
        """按需建桥接服务。不在这里注入——注入要两三秒，只在真要用时做。"""
        if self._bridge_svc is None:
            data_dir = config.data_dir()
            self._bridge_svc = BridgeService(
                data_dir=data_dir, log=lambda msg: logs.write(data_dir, msg)
            )
        return self._bridge_svc

    def _warm_bridge(self) -> None:
        """启动时在后台连一次。连不上就记一笔，不打扰用户。

        为什么值得做：游戏已经在跑但 DLL 没注的情况很常见（用户先开游戏再开 N.E.K.O），
        后台连一次，等他问"帮我加点政治点"时就不用再等两三秒注入了。
        """
        cfg = self._cfg or config.load_config()
        if not cfg.get("auto_inject", True):
            return

        def _worker() -> None:
            data_dir = config.data_dir()
            try:
                st = self._bridge().connect(force=False)
                logs.write(
                    data_dir,
                    f"启动预热桥接：{st.get('state')} {st.get('detail')}",
                )
            except Exception as exc:  # noqa: BLE001
                logs.write(data_dir, f"启动预热桥接异常：{type(exc).__name__}: {exc}")

        threading.Thread(target=_worker, name="hoi4-bridge-warm", daemon=True).start()

    # ── 接管表 / 游戏内热键 ──────────────────────

    def _squads(self) -> squads_mod.Squads:
        if self._squads_svc is None:
            data_dir = config.data_dir()
            self._squads_svc = squads_mod.Squads(
                data_dir, log=lambda msg: logs.write(data_dir, msg)
            )
        return self._squads_svc

    def _ensure_hotkeys(self) -> hotkeys_mod.HotkeyWatch:
        """起热键轮询线程（F7 接管 / F8 放开，DLL 里写死的）。

        起来的是守护线程，游戏没开也不会卡住启动——它自己在里面等。

        **按住说话开着时默认不起这套。** 两套都在抢 F7/F8：按住说话一按 F7，
        老的接管逻辑也会跟着响（"看不出鼠标指着哪个省"两声低音），按一次吵一次。
        老这条本来就是半废的（要地图标定，0.5.0 之后没人写了），
        而"把 XX 的部队交给你"用 `hoi4_squad assign` 按地名就能做。
        真要留着老的，配置里写 `"legacy_hotkeys": true`。
        """
        cfg = self._cfg or config.load_config()
        if cfg.get("ptt_enabled", False) and not cfg.get("legacy_hotkeys", False):
            logs.write(config.data_dir(),
                       "老的 F7/F8 接管热键已停用（按住说话占着这些键，且它本来就缺地图标定）；"
                       "接管部队请用 hoi4_squad assign 或直接跟我说地名。")
            return None  # type: ignore[return-value]
        if self._hotkey_watch is not None and self._hotkey_watch.is_alive():
            return self._hotkey_watch
        data_dir = config.data_dir()
        self._hotkey_watch = hotkeys_mod.HotkeyWatch(
            data_dir, log=lambda msg: logs.write(data_dir, msg)
        )
        self._hotkey_watch.start()
        return self._hotkey_watch

    # ── 按住说话（语音转文字直接发给猫娘） ──────────

    def _ensure_game_guard(self) -> gameguard_mod.GameGuard | None:
        """游戏一开就让出宿主的全部全局快捷键（配合宿主里的那段补丁）。

        这是**独立于按住说话**的功能：哪怕用户没开语音，只要游戏在跑，
        宿主的 F2–F8 也不该抢键。所以默认就起。
        """
        cfg = self._cfg or config.load_config()
        if not cfg.get("game_guard_enabled", True):
            return None
        if self._guard is not None and self._guard.is_alive():
            return self._guard
        data_dir = config.data_dir()
        self._guard = gameguard_mod.GameGuard(
            log=lambda msg: logs.write(data_dir, msg),
            enabled=True,
            exe_name=str(cfg.get("game_exe", gameguard_mod.EXE_NAME)),
        )
        self._guard.start()
        logs.write(data_dir, "游戏闸门已开：游戏运行时宿主的全局快捷键会让出来")
        return self._guard

    def _ensure_asr(self) -> asr_mod.LocalAsr:
        """本地识别器（单例）。模型默认在 data/models/paraformer-zh。"""
        if self._asr is not None:
            return self._asr
        cfg = self._cfg or config.load_config()
        data_dir = config.data_dir()
        model = cfg.get("ptt_model_dir") or os.path.join(
            str(data_dir), "models", asr_mod.DEFAULT_MODEL_NAME)
        self._asr = asr_mod.LocalAsr(
            model,
            vendor_dir=os.path.join(os.path.dirname(__file__), "asrlib"),
            log=lambda msg: logs.write(data_dir, msg),
            threads=int(cfg.get("ptt_asr_threads", 2)),
        )
        return self._asr

    def _warm_asr(self) -> None:
        cfg = self._cfg or config.load_config()
        if not cfg.get("ptt_asr_preload", True):
            return
        try:
            self._ensure_asr().preload()
        except Exception as exc:  # noqa: BLE001
            logs.write(config.data_dir(), f"识别预热失败：{type(exc).__name__}: {exc}")

    def _ensure_ptt(self, force: bool = False) -> ptt_mod.PushToTalk | None:
        """起"按住说话"线程。

        `force=True` 时按最新配置重建（用户改了键位/麦克风后不用重启 N.E.K.O.）。
        默认是关着的——它要占用一个按键，得用户自己说一声才开。
        """
        cfg = self._cfg or config.load_config()
        self._cfg = cfg
        if not force:
            if self._ptt is not None and self._ptt.is_alive():
                return self._ptt
            if not cfg.get("ptt_enabled", False):
                return None
        if self._ptt is not None:
            self._ptt.stop()
            self._ptt = None
        if not cfg.get("ptt_enabled", False):
            return None
        data_dir = config.data_dir()
        engine = str(cfg.get("ptt_engine", "local")).lower()
        asr_inst = self._ensure_asr() if engine == "local" else None
        self._ptt = ptt_mod.PushToTalk(
            data_dir,
            log=lambda msg: logs.write(data_dir, msg),
            vk=ptt_mod.parse_vk(cfg.get("ptt_key", "F7")),
            require_game=bool(cfg.get("ptt_require_game", True)),
            mic_device=int(cfg.get("ptt_mic_device", mic_mod.WAVE_MAPPER)),
            enabled=True,
            asr=asr_inst,
            engine=engine,
            # 游戏内浮层默认关掉：说的话直接进聊天窗，用户开着对话框打游戏
            overlay=bool(cfg.get("ptt_overlay", False)),
            overlay_width=int(cfg.get("ptt_overlay_width", 720)),
            overlay_height=int(cfg.get("ptt_overlay_height", 108)),
            overlay_margin=int(cfg.get("ptt_overlay_margin", 190)),
            route=self._ensure_route(),
            order=(self._voice_order if bool(cfg.get("ptt_voice_order", True)) else None),
        )
        self._ptt.start()
        logs.write(data_dir,
                   f"按住说话已启动：{self._ptt.status()['key']}（识别 {engine}，"
                   f"说的话和她的回答都进聊天窗；语音调令 "
                   f"{'开' if cfg.get('ptt_voice_order', True) else '关'}）")
        return self._ptt

    def _ensure_route(self) -> hostroute_mod.HostRoute:
        """宿主的"游戏路由"客户端（HTTP）。一个插件一个就够，别每轮新建。"""
        if self._route is None:
            data_dir = config.data_dir()
            self._route = hostroute_mod.HostRoute(
                log=lambda msg: logs.write(data_dir, msg))
        return self._route

    def _ensure_frontwatch(self) -> frontwatch_mod.FrontWatch | None:
        """起前线哨兵：游戏一进来先报开局总览，之后报交火/推进。

        **默认关**（`front_watch_enabled`）。它会在游戏一进来时全内存扫一遍，
        2026-11-08 游戏在逆向探针里崩过一次，这类"自动扫描"先不默认开。
        手动查前线用 `hoi4_frontline`（不会自己启动线程）。
        """
        cfg = self._cfg or config.load_config()
        if not cfg.get("front_watch_enabled", False):
            logs.write(config.data_dir(),
                       "前线哨兵没启动（front_watch_enabled=false，默认关；"
                       "手动查前线用 hoi4_frontline）")
            return None
        if self._front_watch is not None and self._front_watch.is_alive():
            return self._front_watch
        data_dir = config.data_dir()
        self._front_watch = frontwatch_mod.FrontWatch(
            self, log=lambda msg: logs.write(data_dir, msg))
        self._front_watch.start()
        logs.write(data_dir, "前线哨兵已启动（开局总览 + 交火/推进通报）")
        return self._front_watch

    def _front_remember(self, text: str, kind: str = "note") -> None:
        """悄悄记一条前线变化：**不说进聊天窗**，只留在她的笔记里。

        0.9.20 用户要求：战况汇报（哪儿打起来了、哪儿推过去了）不要刷在
        聊天框里，「直接告诉 YUI 就行」。所以这类消息只进 `_front_notes`，
        等她自己的简报、或者用户开口问的时候再带出来。
        """
        text = str(text or "").strip()
        if not text:
            return
        self._front_notes.append(text)
        if len(self._front_notes) > 50:
            self._front_notes = self._front_notes[-50:]

    def _frontline_notify(self, text: str, kind: str = "note") -> None:
        """把一条前线通报说进聊天窗（她说的气泡）+ 留档。

        **只用于她自己要说的话**（比如 `_brief_push` 里她做出判断之后）。
        系统侧的"哪儿打起来了"这种播报走 `_front_remember`，不进聊天窗。
        """
        text = str(text or "").strip()
        if not text:
            return
        # 0.9.21：模型偶尔把**工具调用标记**当正文吐出来。真机 15:52 出现过
        # 整段「＜｜｜DSML｜｜ calls＞ ＜｜｜DSML｜｜ invoke name="hoi4_move_units"＞…」
        # 直接落在聊天窗里（用户报的"工具调用标识泄露"）。这里兜一层：
        # 认出来就不播、也不进笔记，只落一条日志方便追。
        if any(m in text for m in ("｜DSML｜", "＜｜", "<|tool", "｟", "|DSML")):
            logs.write(config.data_dir(),
                       f"她这次吐成了工具调用标记，没播（{kind}）：{text[:300]}")
            return
        self._front_notes.append(text)
        if len(self._front_notes) > 50:
            self._front_notes = self._front_notes[-50:]
        try:
            self._ensure_route().speak(text)
        except Exception as exc:  # noqa: BLE001 说不出来也得留档
            logs.write(config.data_dir(),
                       f"前线通报没播出来（{kind}）：{type(exc).__name__}: {exc}")

    def _brief_push(self, text: str, meta: dict | None = None) -> None:
        """把一份战况**事实清单主动推给她**（0.9.18 / M4' 方案 A）。

        走 `chat_event`：宿主用她的模型把清单组织成她自己的一段判断，
        说出来给你听。**不占用你的输入气泡**，也**不会替她下命令**——
        她说"我想派三个师去某处"，要不要动手还是你点头。

        为什么不用 `push`：`push` 会把文本当成"你说的话"塞进对话，虽然能
        触发完整工具回合让她直接动手，但聊天窗里会多出一条假用户气泡。
        方案 A 选了干净的那条。
        """
        text = str(text or "").strip()
        if not text:
            return
        route = self._ensure_route()
        prompt = (
            "这是你的参谋部送来的战况事实清单，不是玩家说的话，也不要当成提问。"
            "请用你自己的口吻简短说出三件事：你现在看到什么、你打算怎么办、为什么。"
            "只依据清单里列出的事实说话，不许编数字、不许补背景、不许替玩家下命令。"
            "如果你觉得该动手，就把你想要的**守线**说清楚（哪些省由你点名），"
            "然后问一句要不要现在就动——玩家点头之后你会用 hoi4_take_command 的 hold 去摆，"
            "不要提「一个个把师搬过去」这种法子。"
            "清单里写的是事实，怎么打由你自己判断——不要说「值得打」「建议」这类结论。"
            # 0.9.21：模型在这个通道里忘了自己只能说话，会吐整段工具调用标记
            # （＜｜｜DSML｜｜ calls＞…），原样落到聊天窗就是"工具调用标识泄露"。
            "**这一段只是说话**：不要输出任何工具调用格式（不要出现 ＜｜｜DSML｜｜ 这类标记、"
            "不要写 invoke/parameter），你在这里不能调工具，只能说话。"
        )
        event = {"type": "army_brief", "text": text,
                 "army": str((meta or {}).get("army") or "")}
        was_active = bool(route.status().get("route_active"))
        line = ""
        try:
            for attempt in (0, 1):
                if attempt and was_active:
                    break
                try:
                    if not route.start(force=(attempt == 1)):
                        raise hostroute_mod.RouteError(
                            route.status().get("route_error") or "游戏路由没接上")
                    line = route.chat_event(event, author_prompt=prompt)
                    if line:
                        break
                except Exception as exc:  # noqa: BLE001 两次都失败就下一轮再推
                    logs.write(config.data_dir(),
                               f"战况简报第 {attempt + 1} 次没生成模型判断："
                               f"{type(exc).__name__}: {exc}")
        finally:
            if not was_active:
                try:
                    route.end()
                except Exception:  # noqa: BLE001 end 失败不影响播报
                    pass
        if line:
            self._frontline_notify(line, kind="brief")
            logs.write(config.data_dir(), "战况简报：已推给她（chat 通了）")
        else:
            self._last_brief_fail = getattr(self, "_last_brief_fail", 0) + 1
            logs.write(config.data_dir(),
                       f"战况简报：模型没生成判断（连续 {self._last_brief_fail} 次），"
                       f"这一份只留档：{text.replace(chr(10), ' ')[:200]}")

    def _frontline_report(self, facts: str, brief: str = "",
                          context: dict | None = None) -> None:
        """前线情报：**只留档，不播报**（0.9.20）。

        用户要求：「战况汇报，哪里有什么战斗这种，不要在聊天框里告诉我，
        直接告诉 YUI 就行。」

        所以这里不再走 `chat_event` 让她现场生成一段汇报——那段话最终会挤进
        聊天窗，正是用户不想看的。现在只把事实记进 `_front_notes`：她自己的
        简报（`_brief_push`）会带上这些变化；用户开口问「前线怎么样」时，
        `hoi4_look` 也能从笔记里取出来。

        注意和 `_brief_push` 的区别：
        - 这里 = **系统观察**，安静记录，不出声；
        - `_brief_push` = 她**要开口说话**，走 chat_event，显示在聊天窗。
        """
        if context:
            self._front_context = dict(context)
        text = str(facts or "").strip()
        if not text:
            return
        kind = (context or {}).get("kind") or "frontline"
        self._front_remember(text[:1200], kind=kind)
        logs.write(config.data_dir(),
                   f"前线情报（{kind}）：只留档不播报 —— "
                   f"{text.replace(chr(10), ' ')[:160]}")

    # 被"认不出国家"挡下的调令，记住这么多秒——够用户回答一句"德国"
    PENDING_ORDER_TTL = 180.0

    def _order_loop(self) -> asyncio.AbstractEventLoop:
        """语音调令专用的事件循环（常驻后台线程）。

        **为什么不能用 startup 钩子里那个 loop**：N.E.K.O. 给生命周期钩子和
        工具调用用的都是**临时事件循环**，钩子一返回就关掉了。0.9.0 的
        `self._main_loop = asyncio.get_running_loop()` 在 14:37 还能拿到 loop，
        14:42 用时 `loop.is_closed()` 已经是 True——语音调令 100% 走
        "插件主循环没跑起来，这道令我没敢动"，兵当然不会动。
        （同样的坑围棋插件踩过：临时 loop 里起的 aiohttp 服务一关就没。）

        自己开一个 run_forever 的循环，和谁的生命周期都解耦。
        """
        loop = self._order_loop_box
        if loop is not None and loop.is_running() and not loop.is_closed():
            return loop
        loop = asyncio.new_event_loop()
        th = threading.Thread(target=loop.run_forever, name="hoi4-order-loop",
                              daemon=True)
        th.start()
        self._order_loop_box = loop
        logs.write(config.data_dir(), "语音调令循环已启动（独立线程）")
        return loop

    def _run_on_order_loop(self, coro, timeout: float = 45.0):
        """在一个自己的循环里跑协程，同步拿结果。可以从事意线程调用。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            # 兜底：真有人从事件循环里调进来（语音线程不会），开线程跑，别死锁
            box: dict = {}

            def _worker() -> None:
                try:
                    box["v"] = asyncio.run(asyncio.wait_for(coro, timeout))
                except BaseException as exc:  # noqa: BLE001 原样抛回调用方
                    box["e"] = exc

            th = threading.Thread(target=_worker, name="hoi4-order-sync", daemon=True)
            th.start()
            th.join(timeout + 10)
            if "e" in box:
                raise box["e"]
            return box.get("v")

        async def _timed(c):
            return await asyncio.wait_for(c, timeout)

        fut = asyncio.run_coroutine_threadsafe(_timed(coro), self._order_loop())
        return fut.result(timeout=timeout + 10)

    def _voice_order(self, text: str) -> dict | None:
        """听出来的话如果是道调令，就直接动插件自己的手，不等模型。

        返回值：{"ok": bool, "say": "念给她听的一句"}；
        不是调令返回 None，交给正常对话。
        """
        # 上一条调令被"认不出你玩哪国"挡下了？先看这句是不是在回答国家
        pend = self._pending_order
        if pend is not None:
            if time.time() - float(pend.get("at") or 0) > self.PENDING_ORDER_TTL:
                self._pending_order = None
            else:
                reply = voiceorder_mod.parse_country_reply(text)
                if reply:
                    how, name = reply
                    country = str(pend.get("guess") or "") if how == "confirm" else name
                    if country:
                        self._pending_order = None
                        logs.write(config.data_dir(),
                                   f"语音调令续办：国家={country}（{how}）")
                        return self._run_voice_order(
                            str(pend.get("kind") or ""), dict(pend.get("args") or {}),
                            text, country)

        got = voiceorder_mod.parse(text)
        if not got:
            return None
        # 用户换了新令：上一条等国家的旧令作废，别等会儿答句话把旧令翻出来
        self._pending_order = None
        return self._run_voice_order(str(got.get("kind") or ""),
                                     dict(got.get("args") or {}), text, "")

    def _run_voice_order(self, kind: str, args: dict, text: str,
                         country: str) -> dict:
        """真正把一条语音调令变成动作。`country` 非空 = 用户刚说了国名。"""
        try:
            if kind == "move":
                if not args.get("from_place"):
                    return {"ok": False, "ask": True,
                            "say": "从哪个省调？说「把华沙的师调到柏林」这样。"}
                coro = self._order_move(args.get("from_place", ""),
                                        args.get("to_place", ""),
                                        int(args.get("count") or 0), country,
                                        str(args.get("unit_type") or ""))
            elif kind == "defence":
                coro = self.hoi4_battle_plan(action="defence",
                                             army_at=args.get("army_at", ""))
            elif kind == "near":
                coro = self.hoi4_units_near(where=args.get("where", ""))
            elif kind == "look":
                coro = self.hoi4_look(area=args.get("area", ""))
            elif kind == "take":
                coro = self.hoi4_take_command(action="claim",
                                              army=args.get("army", ""))
            elif kind == "auto":
                coro = self.hoi4_take_command(action="auto")
            elif kind == "pause":
                coro = self.hoi4_take_command(action="pause")
            else:
                return None
            res = self._run_on_order_loop(coro, timeout=45.0)
        except Exception as exc:  # noqa: BLE001 调令失败要说出来，不能闷着
            return {"ok": False,
                    "say": f"这道令没做成：{type(exc).__name__}: {exc}"}

        if not isinstance(res, dict):
            res = {"ok": True, "display": str(res)}
        meta = res.get("meta") or {}
        ok = bool(res.get("ok", True))
        say = _speakable(res.get("display") or res.get("message") or "")
        if not say:
            say = "办好了。" if ok else "没办成，具体原因看日志。"
        if meta.get("need_country"):
            # 不是失败，是缺一句"你玩的是哪国"——挂起来，下一句回答就接着办
            self._pending_order = {
                "kind": kind, "args": dict(args), "at": time.time(),
                "guess": str(meta.get("country") or ""),
            }
            logs.write(config.data_dir(), f"语音调令挂起（等用户说国名）：{text}")
            return {"ok": False, "ask": True, "say": say}
        logs.write(config.data_dir(), f"语音调令：{text} → {kind} ok={ok}")
        return {"ok": ok, "say": say}

    async def _order_move(self, from_place: str, to_place: str, count: int,
                          country: str = "", unit_type: str = "") -> dict:
        """语音调兵专用：先把"上次亲口认过的国家"补上再调。

        `_do_move_live` 在"存档不新鲜 + 没确认过国家"时会先反问一句而不动手。
        嘴上说完就等结果，反问一轮体验很差——所以这里先摸一下已确认的国家，
        有就直接带进去。用户刚用嘴回答过国名时（`country` 非空）优先用它。
        """
        hint = (country or "").strip()
        if not hint:
            try:
                conf = await asyncio.to_thread(self._live().confirmed_country)
                if conf:
                    hint = str(conf.get("short") or conf.get("tag") or "")
            except Exception:  # noqa: BLE001 认不出来就按存档认，_do_move_live 自己会兜
                hint = ""
        return await self._do_move_live(from_place, to_place, count, hint, False,
                                        unit_type)

    def _ptt_status(self) -> dict:
        ptt = self._ptt
        cfg = self._cfg or config.load_config()
        common = {
            "engine": str(cfg.get("ptt_engine", "local")),
            "guard_enabled": bool(cfg.get("game_guard_enabled", True)),
            "guard": (self._guard.status() if self._guard is not None
                      else {"guard_active": False, "note": "闸门线程没起来"}),
            "asr": (self._asr.status() if self._asr is not None
                    else {"state": "未初始化", "model_ok": False}),
        }
        if ptt is None:
            return {
                "running": False,
                "enabled": bool(cfg.get("ptt_enabled", False)),
                "key": str(cfg.get("ptt_key", "F7")),
                "host": nekohost.base_url(),
                "catgirl": nekohost.catgirl_name() or "（拿不到）",
                "conflicts": nekohost.conflicts(),
                "mic_devices": mic_mod.list_devices(),
                "mic_device": int(cfg.get("ptt_mic_device", mic_mod.WAVE_MAPPER)),
                **common,
            }
        st = ptt.status()
        st["running"] = True
        st["conflicts"] = nekohost.conflicts()
        st["mic_devices"] = mic_mod.list_devices()
        st.update(common)
        return st

    # ── 地图操作（点地图，以玩家身份） ──────────────
    def _ui(self) -> UiDriver:
        if self._ui_svc is None:
            data_dir = config.data_dir()
            self._ui_svc = UiDriver(
                self._bridge(), log=lambda msg: logs.write(data_dir, msg)
            )
        return self._ui_svc

    def _prov_idx(self):
        """省坐标索引。缺失就是打包不全，不能静默降级成"点不了"。"""
        try:
            from . import panel as _panel
            wid = _panel.current_world_id()
        except Exception:
            wid = ""
        if self._index is None or self._index_wid != wid:
            idx = prov_index.load()
            if idx is None:
                raise UiError("插件包里缺少省坐标索引 prov_px.json，要重装一次插件。")
            self._index = idx
            self._index_wid = wid
        return self._index

    def _places(self):
        idx = self._prov_idx()
        found = places_mod.load(config.data_dir(), idx)
        if found is None:
            raise UiError(
                "还没有地名表（州名/省→州映射）。需要先让插件读一次存档——"
                "进游戏存个档，或者跟我说「看看我这局」。"
            )
        return found

    def _player_view(self):
        """桥接 + 标定都就绪，才允许点地图。返回 (ui, view, index)。"""
        idx = self._prov_idx()
        ui = self._ui()
        data_dir = config.data_dir()

        st = self._bridge().status(refresh=True)
        if st.get("state") != "ready":
            st = self._bridge().connect(force=False)
        if st.get("state") != "ready":
            raise UiError(
                f"桥接没连上（{st.get('state')}）：{st.get('detail') or ''} "
                "游戏要开着、进到地图界面。"
            )

        view = mapops.ensure_view(
            ui, idx, data_dir, log=lambda m: logs.write(data_dir, m)
        )
        return ui, view, idx

    # ── 写侧核心：下一个命令 ──────────────────────

    async def _do_act(self, action: str, amount: int, extra: str) -> dict:
        """白名单校验 → 投递 → 记一条待核对。返回值永远是 dict。"""
        try:
            spec = actions.resolve(action, amount, extra)
        except actions.ActError as exc:
            return {
                "ok": False,
                "display": exc.message,
                "hint": exc.hint or "原样转发，不要换个说法重试同一个操作。",
            }

        data_dir = config.data_dir()
        snap = cache_mod.read_cache(data_dir)
        baseline = None
        save_date = ""
        if snap:
            player = snap.get("player") or {}
            field = spec["field"]
            if field and isinstance(player.get(field), (int, float)):
                baseline = float(player[field])
            save_date = (snap.get("meta") or {}).get("save_date", "")

        svc = self._bridge()
        result = await asyncio.to_thread(svc.run, spec["cmd"])

        # 桥没连上时自动补一次：用户说"帮我加 100 政治点"，这时候不该让他先去点菜单
        if not result["ok"] and result["reason"] == "no_bridge":
            st = await asyncio.to_thread(svc.connect, False)
            if st.get("state") == "ready":
                result = await asyncio.to_thread(svc.run, spec["cmd"])
            else:
                return {
                    "ok": False,
                    "display": render.bridge_report(st)
                    + "\n\n所以这条命令没发出去。",
                    "hint": "原样转发。绝对不要说已经加上了。",
                    "meta": {"bridge": st.get("state")},
                }

        if not result["ok"]:
            detail = result.get("detail") or "原因不明。"
            if result["reason"] == "no_window":
                head = "命令没发出去：游戏还没到地图界面。"
                tail = "先回到地图（菜单、弹窗、加载界面都不收命令），再让我发一次。"
            elif result["reason"] == "protocol":
                head = "命令没发出去：桥接版本对不上。"
                tail = detail
            else:
                head = "命令没发出去。"
                tail = detail
            return {
                "ok": False,
                "display": f"{head}\n{tail}",
                "hint": "原样转发。不要说已经加上了。",
                "meta": {"bridge": result.get("reason")},
            }

        rec = act_log.add(
            data_dir,
            {
                "action": spec["key"],
                "label": spec["label"],
                "amount": spec["amount"],
                "cmd": spec["cmd"],
                "field": spec["field"],
                "baseline": baseline,
                "save_date": save_date,
                "port": svc.port,
            },
        )

        display = render.act_report(spec["label"], spec["amount"], spec["cmd"], spec["note"])
        if not spec["field"]:
            display += "\n\n（这类操作没法用存档核对，你在游戏里看一眼就知道有没有生效。）"
        elif baseline is None:
            display += "\n\n（我手上还没有存档数据，这次没法核对；等你存一次档再问我。）"
        else:
            display += f"\n\n（发之前存档里是 {baseline:g}，存完档我再对一遍。）"

        # 上一次的核对结论要带出来——这是唯一能说"确实生效"的场合
        for old in act_log.recent(data_dir, 6):
            if old.get("id") == rec.get("id"):
                continue
            if old.get("state") in ("confirmed", "partial", "none", "capped", "unverifiable"):
                display += "\n\n" + render.verify_report(old)
                break

        return {
            "ok": True,
            "display": display,
            "hint": (
                "display 是投递结果，请原样发给用户。"
                "**不要说「已经加上了」或「已经生效」**——游戏不回报执行结果，"
                "只能算发出去了。要说清等他下次存档我再核对。"
            ),
            "meta": {
                "action": spec["cmd"],
                "amount": spec["amount"],
                "port": svc.port,
                "baseline": baseline,
                "verify": rec.get("state"),
            },
        }

    # ── 核心：读缓存拼结果 ────────────────────────

    def _snapshot_or_error(self) -> tuple[dict | None, dict | None]:
        """返回 (snap, err_payload)。只在没缓存/还在解析时给 err。"""
        data_dir = config.data_dir()
        snap = cache_mod.read_cache(data_dir)
        watcher = self._ensure_watcher()
        st = watcher.status()

        # 0.9.11：当前玩的是读不了的存档时，不管手里有没有旧缓存，
        # 都别把另一局/几个月前的数据当现状端出去——给用户能照做的下一步。
        # （0.9.12 起正常二进制档可直接解析，binary_blocker 只剩读不了头的档。）
        blocker = st.get("binary_blocker")
        if blocker and st.get("state") == "error":
            return None, self._binary_blocker_payload(blocker)

        if snap is None:
            if st.get("state") == "parsing":
                return None, {
                    "ok": False,
                    "display": "正在读你的存档，大概要十几二十秒，等我一下。",
                    "hint": "原样转发即可，不要编造任何局势数据。",
                }
            if st.get("state") in ("no_save", "no_game"):
                return None, {
                    "ok": False,
                    "display": (
                        "我没找到钢四的存档。"
                        "要么游戏还没存过档，要么存档目录不在默认位置。"
                        "你先在游戏里手动存一次档，或者告诉我存档放在哪个文件夹。"
                    ),
                    "hint": "原样转发。不要凭猜测说局势。",
                }
            if st.get("state") == "error":
                return None, {
                    "ok": False,
                    "display": f"存档读失败了：{st.get('error', '原因不明')}。"
                    "你在游戏里重新手动存一次档再问我；还失败的话，"
                    "把上面这句原因原样告诉我。",
                    "hint": "原样转发。",
                }
            return None, {
                "ok": False,
                "display": "还没准备好，正在找存档，过一会儿再问我。",
                "hint": "原样转发。",
            }

        # 缓存比存档旧（比如刚存了档还没解析完）时提示一句
        age = cache_mod.age_seconds(snap)
        meta = snap.get("meta") or {}
        if st.get("state") == "error":
            # 0.9.8：新档读不成（二进制存档 / 写了一半）时别静悄悄端出上一份。
            snap.setdefault(
                "_notice",
                f"最近这份存档没读成：{st.get('error', '原因不明')}。"
                "下面还是上一次读成功的那份。",
            )
        elif st.get("state") == "parsing":
            snap.setdefault("_notice", "他刚存了新档，我正在读，下面这份还是上一次的。")
        elif age > 3600 and age > 0:
            snap.setdefault("_notice", render.stale_note(age / 60))
        return snap, None

    def _binary_blocker_payload(self, blocker: dict) -> dict:
        """最新存档是**读不了的**二进制时给用户的说明 + 能照做的下一步。

        0.9.12 起正常 HOI4bin 直接解析，走到这里的只剩头部指纹都读不出的档
        （游戏大版本更新、字段表没覆盖到之类）。插件会在游戏**关掉**时自动把
        设置改成文本（watcher 的自愈），所以这里按"游戏是否还开着"给不同的话。
        """
        try:
            game = save_meta.game_process_cached()
        except Exception:  # noqa: BLE001
            game = {}
        when = blocker.get("when") or "刚才"
        name = blocker.get("name") or "这份存档"
        if game.get("pid"):
            action = (
                "你把游戏关掉一次，插件会自动把存档格式改回文本；"
                "再开游戏、手动存一次档，我就能读这局了。"
            )
        else:
            action = (
                "我已经把存档格式改回文本了，你开游戏后手动存一次档，我就能读这局。"
            )
        return {
            "ok": False,
            "display": (
                f"你最新那份存档（{name}，{when}）是二进制格式，而且我连头部都读不出"
                f"（可能是游戏更新了新格式），所以存档里的经济/人力/编制这些数据"
                f"现在给不出。{action}"
                "不过实时战况——前线交火、部队位置、师编制图标——不依赖存档，你现在就能问我。"
            ),
            "hint": (
                "原样转发，别编存档里的数字。用户要实时情况就改用 "
                "hoi4_frontline / hoi4_units_near / hoi4_divisions。"
            ),
            "meta": {"binary_blocker": blocker},
        }

    def _save_vintage_block(self, snap: dict) -> str:
        """给存档快照加横幅，说清楚「这是哪一局、隔了多久」。

        0.9.8 以前只说一句"游戏正在运行，这份不是实时"，实测这不够：
        盘上最新档是 2026-05-26 的卢森堡 1.14.8，而玩家 11-09 开的是
        德国 1.16.9，中间隔 167 天，连游戏版本都不是一个。那不是"旧一点"，
        是**根本不是同一局**。现在按存档头 + 游戏进程启动时间判定，
        横幅直接把差多少天、哪个版本写明白。

        只重算 8 KB 文件头和进程信息（带缓存），不解析存档、不读游戏内存。
        """
        meta = snap.get("meta") or {}
        cached_head = meta.get("save_head") or {}
        path = meta.get("save_path") or cached_head.get("path")
        head: dict = {}
        if path:
            try:
                head = save_meta.head_meta(path)
            except Exception:  # noqa: BLE001 探测失败不影响出结果
                head = {}
        if not head.get("player"):
            head = dict(cached_head)
        if not head:
            # 很老的缓存，连头部指纹都没有，退回旧文案，别把猜测说成事实
            return (
                "※ 这是存档快照、不是实时；存档是哪一天、哪一局没能确认，"
                "别直接当成现在的局面。"
            )

        try:
            game = save_meta.game_process_cached()
            judge = save_meta.judge_live(head, game=game)
        except Exception:  # noqa: BLE001
            game = {}
            judge = meta.get("save_match") or {"match": "unknown"}
        # 让 _compose 的 meta 和横幅用同一次判定，别一个说旧档一个说是当前
        snap["_live_judge"] = judge
        who = meta.get("player_name") or head.get("player") or "?"
        text = save_meta.banner(head, judge, who)
        # 游戏没开、档也不旧：存档就是全部，不打扰（沿用旧行为）
        if not game.get("pid") and judge.get("match") == "probable_current":
            return ""
        return text

    def _compose(self, snap: dict, detail: str) -> dict:
        meta = snap.get("meta") or {}
        # 0.9.14：游戏在跑且缓存齐时，简报里的「主力在」一行换成实时分布，
        # 消灭"简报说师在 A、附近查询说在 B"的割裂。全走缓存窥视，零内存读。
        live_army = self._live_main_forces(snap)
        if detail == "army":
            body = render.army_detail(snap)
        elif detail == "economy":
            body = render.economy_detail(snap)
        elif detail == "full":
            body = "\n\n".join(
                [render.brief(snap, live_army), render.army_detail(snap),
                 render.economy_detail(snap)]
            )
        else:
            body = render.brief(snap, live_army)

        notice = snap.get("_notice")
        if notice:
            body = f"{body}\n\n（{notice}）"

        vintage = self._save_vintage_block(snap)
        if vintage:
            body = f"{vintage}\n\n{body}"

        return {
            "ok": True,
            "display": body,
            "hint": (
                "display 是排好版的局势简报，请原样发给用户，不要改数字、不要漏行。"
                "必须让用户知道这是哪一天的存档。"
                "开头带「※」的提示必须放在回复最前面原样保留，一个字都别删。"
                "如果提示里说『这局还没存过盘』『不是同一局』『版本不同』，"
                "下面这些数字就是旧档的，**禁止**用它回答『现在』『当前』的战况；"
                "这时要告诉用户先在游戏里存一次档，或者改用 hoi4_frontline / "
                "hoi4_units_near 看实时。"
            ),
            "meta": {
                "save_date": meta.get("date_text") or meta.get("save_date"),
                "player": meta.get("player"),
                "parse_seconds": meta.get("parse_seconds"),
                "age_seconds": int(cache_mod.age_seconds(snap)),
                "save_age_days": (meta.get("save_head") or {}).get("age_days", -1),
                "save_version": (meta.get("save_head") or {}).get("version_num", ""),
                "save_match": (snap.get("_live_judge") or meta.get("save_match") or {}).get("match", "unknown"),
                "is_current": bool((snap.get("_live_judge") or meta.get("save_match") or {}).get("is_current")),
                "data_quality": snap.get("data_quality", {}),
                "version_ok": not bool((snap.get("version") or {}).get("warn")),
                "bridge": self._bridge().peek().get("state", "unknown"),
            },
        }

    # ── 工具 ──────────────────────────────────────

    @llm_tool(
        name="hoi4_situation",
        description=(
            "查《钢铁雄心4》存档里的局势。用户问「我这局怎么样」「现在什么情况」"
            "「我该干什么」「部队在哪」「工厂够不够」「有没有漏看什么」时调用。"
            "返回的是存档快照（不是实时），会标出存档日期、玩家国、版本和距今多久。"
            "开头「※」横幅会说明这份档是不是当前这局："
            "如果写着『这局还没存过盘』『不是同一局』『版本不同』，"
            "就绝不能把这些数字当成当前战况来回答；"
            "要看实时部队用 hoi4_units_near / hoi4_frontline。"
            "detail=army 只看部队分布，detail=economy 只看经济，默认 auto 给综合简报。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "detail": {
                    "type": "string",
                    "enum": ["auto", "army", "economy", "full"],
                    "description": "想看哪一块。auto=综合简报，army=部队分布，economy=经济盘面，full=全部",
                }
            },
            "required": [],
        },
    )
    async def hoi4_situation(self, *, detail: str = "auto"):
        snap, err = self._snapshot_or_error()
        if err is not None:
            return err
        return self._compose(snap, detail)

    @llm_tool(
        name="hoi4_anomalies",
        description=(
            "只返回《钢铁雄心4》这局里「用户可能没注意到」的几条问题，"
            "比如盟友部队赖在他国土上、政治点攒到溢出、某州堆了太多师补给跟不上。"
            "用户问「有什么要注意的」「我漏了什么」时调用。"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
    )
    async def hoi4_anomalies(self):
        snap, err = self._snapshot_or_error()
        if err is not None:
            return err
        vintage = self._save_vintage_block(snap)
        items = snap.get("anomalies") or []
        if not items:
            head = f"{vintage}\n\n" if vintage else ""
            return {
                "ok": True,
                "display": f"{head}这份存档里没查出明显漏洞。",
                "hint": "原样转发。开头带「※」的提示要原样保留。",
            }
        lines = []
        if vintage:
            lines.append(vintage)
        lines.append(f"【{snap.get('meta', {}).get('date_text', '存档')} 存档里的问题】")
        for a in items:
            mark = "!" if a.get("level") == "warn" else ("?" if a.get("level") == "doubt" else "·")
            lines.append(f"{mark} {a.get('text', '')}")
        return {
            "ok": True,
            "display": "\n".join(lines),
            "hint": ("原样转发，不要合并同类项，也不要自己加新的问题。"
                     "开头带「※」的提示要原样保留在最前面。"),
        }

    # ── 命令入口 ──────────────────────────────────

    @plugin_entry(
        id="hoi4_brief",
        name="看钢四这局的局势",
        description=(
            "读出《钢铁雄心4》最近一次存档的局势简报。"
            "当用户说「看看我这局」「读一下我的存档」「钢四什么情况」「我现在该干什么」时使用。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "detail": {
                    "type": "string",
                    "enum": ["auto", "army", "economy", "full"],
                    "description": "auto=综合，army=部队，economy=经济，full=全部",
                }
            },
            "required": [],
        },
        llm_result_fields=["ok", "display"],
    )
    async def entry_brief(self, detail: str = "auto", **_):
        snap, err = self._snapshot_or_error()
        if err is not None:
            return Ok(err)
        return Ok(self._compose(snap, detail))

    @plugin_entry(
        id="hoi4_refresh",
        name="重新读一遍钢四存档",
        description=(
            "强制重新解析《钢铁雄心4》的存档。"
            "当用户说「重新读一下存档」「刚存了档，再看一遍」「数据不对，刷新一下」时使用。"
        ),
        input_schema={"type": "object", "properties": {}, "required": []},
        llm_result_fields=["ok", "display"],
    )
    async def entry_refresh(self, **_):
        watcher = self._ensure_watcher()
        before = watcher.status().get("parsed_at", 0)
        start = time.time()
        saw_parsing = False
        while time.time() - start < 45:
            st = watcher.status()
            if st.get("state") == "error":
                return Ok(
                    {
                        "ok": False,
                        "display": f"重读失败：{st.get('error')}",
                        "hint": "原样转发。",
                    }
                )
            if st.get("state") == "parsing":
                saw_parsing = True
            elif st.get("state") == "ready" and st.get("parsed_at", 0) > before:
                break
            elif not saw_parsing and time.time() - start > 7:
                # 存档没变，watcher 不会重解析。别在这儿死等 45 秒。
                break
            await asyncio.sleep(0.5)

        snap, err = self._snapshot_or_error()
        if err is not None:
            return Ok(err)
        out = self._compose(snap, "auto")
        if not saw_parsing:
            out["display"] = "存档没有变化，还是这一份：\n\n" + out["display"]
        return Ok(out)

    # ── 写侧入口（路线 C：只做白名单里那一小块） ──────

    @llm_tool(
        name="hoi4_act",
        description=(
            "给正在玩的《钢铁雄心4》这局发一条控制台命令。**只有白名单里的低风险加成**，"
            "比如加政治点、加人力、加陆军经验、加指挥点、加燃料、加稳定度/战争支持、"
            "研究加速、瞬间建造、瞬间训练、国策自动完成、加民族精神。"
            "用户说「给我加 100 政治点」「补 10 万人力」「研究加速一下」时调用。"
            "**部队调动、瞬移、宣战、吞并、切国家一律不做**——那些会毁档，直接告诉他这块自己点。"
            "注意：发出去只算「投递」，游戏不回报结果，要等下次存档才能核对有没有生效。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": (
                        "动作名：add_political_power / add_manpower / add_army_xp / "
                        "add_command_power / add_fuel / add_stability / add_war_support / "
                        "research_fast / instant_construction / instant_training / "
                        "focus_autocomplete / add_ideas"
                    ),
                },
                "amount": {"type": "integer", "description": "加多少。research_fast 这类开关动作不用填。"},
                "extra": {
                    "type": "string",
                    "description": "add_ideas 要的 idea id（小写字母开头）。其它动作留空。",
                },
            },
            "required": ["action"],
        },
    )
    async def hoi4_act(self, *, action: str, amount: int = 0, extra: str = ""):
        return await self._do_act(action, amount, extra)

    @llm_tool(
        name="hoi4_bridge_status",
        description=(
            "查桥接状态：现在能不能给《钢铁雄心4》发命令。"
            "用户问「你能帮我操作游戏吗」「为什么加不上」「连上游戏了吗」时调用。"
            "返回会说明是已连上、游戏没开、还是权限不够，以及要用户做什么。"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
    )
    async def hoi4_bridge_status(self):
        st = await asyncio.to_thread(self._bridge().status, True)
        return {
            "ok": st.get("state") == "ready",
            "display": render.bridge_report(st),
            "hint": (
                "display 原样转发。如果没连上，把里面的原因和要用户做的事说清楚，"
                "不要含糊成「连不上」。"
            ),
            "meta": {"state": st.get("state"), "port": st.get("port")},
        }

    @plugin_entry(
        id="hoi4_apply",
        name="给钢四下一个控制台命令",
        description=(
            "给《钢铁雄心4》这局发力所能及的控制台加成（白名单：加政治点/人力/经验/指挥点/燃料、"
            "加稳定度或战争支持、研究加速、瞬间建造、瞬间训练、国策自动完成、加民族精神）。"
            "当用户用命令口气说「给我加 200 政治点」「补 20 万人力」「研究全加速」时使用。"
            "部队调动、宣战、吞并这类会被明确拒绝。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": (
                        "动作名：add_political_power / add_manpower / add_army_xp / "
                        "add_command_power / add_fuel / add_stability / add_war_support / "
                        "research_fast / instant_construction / instant_training / "
                        "focus_autocomplete / add_ideas"
                    ),
                },
                "amount": {"type": "integer", "description": "加多少（开关类动作不用填）"},
                "extra": {"type": "string", "description": "add_ideas 要的 idea id，其它留空"},
            },
            "required": ["action"],
        },
        llm_result_fields=["ok", "display"],
    )
    async def entry_act(self, action: str = "", amount: int = 0, extra: str = "", **_):
        if not (action or "").strip():
            return Ok(
                {
                    "ok": False,
                    "display": "要给哪个加成？我能做的有：" + actions.action_list(),
                    "hint": "原样转发。",
                }
            )
        return Ok(await self._do_act(action, amount, extra))

    @plugin_entry(
        id="hoi4_link",
        name="连上钢四（注入桥接）",
        description=(
            "把桥接 DLL 注入正在运行的《钢铁雄心4》，连不上时说明原因。"
            "当用户说「连上游戏」「把桥接打开」「为什么你操作不了我的游戏」时使用。"
            "游戏没开、权限不一致、端口占用这几种情况会分别给不同的处理办法。"
        ),
        input_schema={"type": "object", "properties": {}, "required": []},
        llm_result_fields=["ok", "display"],
    )
    async def entry_link(self, **_):
        st = await asyncio.to_thread(self._bridge().connect, False)
        return Ok(
            {
                "ok": st.get("state") == "ready",
                "display": render.bridge_report(st),
                "hint": "原样转发。没连上就说清要用户做什么，别含糊。",
                "meta": {"state": st.get("state"), "port": st.get("port")},
            }
        )

    # ── 真·点地图：调兵 / 看部队在哪 / 标定 ──────────

    @llm_tool(
        name="hoi4_army_positions",
        description=(
            "列出玩家每个师在地图上的确切位置（按省分组，带地名和省编号）。"
            "游戏在跑时是**实时**从内存读的（含行军状态和编制）；游戏没开才退回最新存档。"
            "用户问「我的部队都在哪」「哪些师闲着」「3 个师在哪个省」时调用。"
            "调兵前先用它确认出发地——不然可能把一整个集团军调走。"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
    )
    async def hoi4_army_positions(self):
        # 0.9.14：游戏在跑 → 直接读内存（实时、含行军状态）；游戏没开/读不到
        # → 退回存档快照。位置只认一个来源，不再出现"简报说师在 A、
        # 附近查询说在 B"的割裂。
        live = self._live()
        player = None
        try:
            player = await self._locate_player("")
        except Exception:  # noqa: BLE001 游戏没开/没认出来都走存档兜底
            player = None
        if player is not None:
            live.mark_tool(20.0)
            try:
                units = await asyncio.wait_for(
                    asyncio.to_thread(live.units, int(player["addr"])),
                    timeout=18.0)
            except Exception:  # noqa: BLE001 读失败就退回存档，不硬撑
                units = []
            if units:
                return self._army_positions_live(player, units)
        snap, err = self._snapshot_or_error()
        if err is not None:
            return err
        army = snap.get("army") or {}
        rows = army.get("按省") or []
        if not rows:
            return {
                "ok": False,
                "display": "存档里没读到你的师。可能这局你还没有陆军，或者还没点开过陆军界面。",
                "hint": "原样转发。",
            }
        lines = [
            f"存档 {snap.get('meta', {}).get('date_text') or '?'}（游戏没在跑或内存没读到，"
            f"这是存档数据）：共 {army.get('师总数', 0)} 个师，"
            f"行军中 {army.get('行军中', 0)}，缺编严重 {army.get('缺编严重', 0)}。",
            "",
            "按省分布（省编号 / 州名 / 师数）：",
        ]
        for r in rows[:24]:
            lines.append(f"  {r['prov']:>6} / {r['state']} / {r['n']} 个师")
        if len(rows) > 24:
            lines.append(f"  ……还有 {len(rows) - 24} 个省未列出")
        return {
            "ok": True,
            "display": "\n".join(lines),
            "hint": (
                "原样转发。要调兵就用 hoi4_move_units，出发地和目的地都可以直接说州名。"
            ),
            "meta": {"source": "save", "count": len(rows),
                     "total": army.get("师总数")},
        }

    def _army_positions_live(self, player: dict, units: list[dict]) -> dict:
        """0.9.14：实时版布防表。units 来自 live.units()（含 path/target/编制）。"""
        by_prov: dict[int, list[dict]] = {}
        for u in units:
            try:
                pid = int(u.get("prov") or 0)
            except (TypeError, ValueError):
                continue
            if pid:
                by_prov.setdefault(pid, []).append(u)
        marching = sum(1 for u in units if u.get("path"))
        rows = sorted(by_prov.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        who = player.get("short") or "你"
        lines = [
            f"实时（刚从内存读的，不是存档）：{who}共 {len(units)} 个师，"
            f"行军中 {marching}。",
            "",
            "按省分布（地点 / 师数）：",
        ]
        for prov, group in rows[:24]:
            moving = [u for u in group if u.get("path")]
            tail = f"，{len(moving)} 个在走" if moving else ""
            tpl = livegame.livegame_template_line(group)
            t = f" —— {tpl}" if tpl else ""
            lines.append(f"  {self._place_label(prov)} / {len(group)} 个师{tail}{t}")
        if len(rows) > 24:
            lines.append(f"  ……还有 {len(rows) - 24} 个省未列出")
        return {
            "ok": True,
            "display": "\n".join(lines),
            "hint": (
                "原样转发。要调兵就用 hoi4_move_units，出发地和目的地都可以直接说州名。"
            ),
            "meta": {"source": "live", "count": len(rows), "total": len(units),
                     "marching": marching},
        }

    _trace("3.1 army_positions 注册完成")

    @llm_tool(
        name="hoi4_units_near",
        description=(
            "看**某个地方附近的部队**，敌我都报：说国名（波兰/苏联/法国）就以那个国家的"
            "国土和部队为中心（被占领的国家也按它的国土圈范围），说地名（柏林/华沙）"
            "就以那个省为中心，往外扩几省邻接范围，"
            "把范围内每个国家各有多少师、驻在哪些省都列出来。"
            "用户问「波兰附近有什么部队」「我东边边境兵力怎么样」「苏联人在哪集结」"
            "「挪威海岸线有什么」时调用。"
            "**只读，不改任何东西。**"
        ),
        parameters={
            "type": "object",
            "properties": {
                "where": {"type": "string",
                          "description": "国名（波兰、苏联）或地名（柏林、华沙、省编号）"},
                "depth": {"type": "integer",
                          "description": "往外扩几省邻接。0 = 自动：国名 1 跳，地名 2 跳。"},
                "country": {"type": "string", "description": "玩哪国，平时留空"},
            },
            "required": ["where"],
        },
    )
    async def hoi4_units_near(self, *, where: str = "", depth: int = 0,
                              country: str = ""):
        """某地附近的部队（敌我都算）。国名 → 以该国部队为种子；地名 → 以该省为种子。"""
        raw = (where or "").strip()
        if not raw:
            return {"ok": False, "display": "要看哪里附近？说个国名（波兰）或地名（柏林）。",
                    "hint": "问用户看哪里。"}

        player, err = await self._player_only(country, guard=False)
        if err is not None:
            return err
        live = self._live()
        # 0.9.13：这是个可能很长的查询（冷启动要认一遍全世界国家名），先让
        # 后台的版图/部队扫描让位，别跟工具抢 DLL 的全局命令锁把超时撞穿。
        live.mark_tool(25.0)

        names: dict[int, str] = {}
        try:
            names = self._prov_idx().city_names()
        except Exception:  # noqa: BLE001
            names = {}

        def label(pid: int) -> str:
            nm = names.get(int(pid))
            return f"{nm}（{pid}）" if nm else str(pid)

        # ── 先当国名试：波兰、苏联这种；再当地名试 ──────────
        # 0.9.13：合并进一次后台线程。以前"认国名"（冷扫 406 国要 5~8 秒）
        # 和"扫部队"是两段独立超时，地名查询（柏林/华沙）全死在认国名的
        # 8 秒闸上——用户看到的永远是"先不报了"。国家名行现在带缓存，
        # 热查询 ~0 秒；冷查询也只扫一遍（units_near 复用同一份缓存）。
        def _resolve() -> tuple:
            """返回 (target, seeds, d, head)。ValueError = 国名地名都不是。"""
            target = live.locate_named(raw)
            if target is not None:
                unit_provs = live.country_unit_provs(target["addr"])
                # 国名分支按「国土 + 部队」圈范围：挪威这种被占领国家，
                # 残余可能只剩一个师躲在孤岛上，只拿部队当种子会把用户
                # 真正想看的、德国布在挪威海岸的十几个驻军全省略。
                core = self._country_core_seeds(target["short"])
                seeds = sorted(set(unit_provs) | set(core))
                d = depth if depth > 0 else 1
                head = f"以 {target['short']} 的国土和部队为中心，往外 {d} 省"
                return target, seeds, d, head
            places = self._places()
            pid, lab = places.resolve(raw)  # 认不出来抛 ValueError
            seeds = [int(pid)]
            d = depth if depth > 0 else 2
            return None, seeds, d, f"以 {lab} 为中心，往外 {d} 省"

        try:
            target, seeds, d, head = await asyncio.wait_for(
                asyncio.to_thread(_resolve), timeout=12.0)
        except asyncio.TimeoutError:
            return {"ok": True, "display": "这会儿认地名读得太久，先不报了，等一下再问一次。",
                    "hint": "原样转发。别编一份兵力分布出来。"}
        except ValueError as exc:
            return {"ok": False, "display": str(exc),
                    "hint": "把候选说出来让用户确认。国名和地名都没有。"}
        except Exception as exc:  # noqa: BLE001
            return self._agent_error(exc)

        if target is not None and not seeds:
            return {
                "ok": True,
                "display": f"{target['short']}现在一个陆军师都没有（或者都在海上），国土省也一个没读到。",
                "hint": "原样转发。",
            }

        try:
            res = await asyncio.wait_for(
                asyncio.to_thread(live.units_near, seeds, d), timeout=14.0)
        except asyncio.TimeoutError:
            return {"ok": True, "display": "这会儿扫附近部队读得太久，先不报了，等一下再问一次。",
                    "hint": "原样转发。别编一份兵力分布出来。"}
        except Exception as exc:  # noqa: BLE001
            return self._agent_error(exc)

        rows = res.get("countries") or []
        if not rows:
            return {
                "ok": True,
                "display": f"{head}的范围内（{res.get('range')} 个省）现在没有任何国家的陆军。",
                "hint": "原样转发。",
            }

        lines = [f"{head}的范围内（{res.get('range')} 个省）：", ""]
        player_addr = int(player["addr"])
        shown = 0
        for r in rows:
            if shown >= 8:
                lines.append(f"  ……还有 {len(rows) - shown} 个国家没列")
                break
            shown += 1
            tag = "（你）" if int(r["addr"]) == player_addr else ""
            by_prov = [(int(p), int(n)) for p, n in (r.get("provs") or [])]
            top = by_prov[:5]
            provs = "、".join(f"{label(p)} {n}" for p, n in top)
            more = "" if len(by_prov) <= 5 else f" 等 {len(by_prov)} 个省"
            lines.append(f"  {r['name']}{tag}：{r['count']} 个师 —— {provs}{more}")
        lines.append("")
        if res.get("source") == "cache":
            lines.append(
                f"这是实时位置（后台 {res.get('age', '?')} 秒前扫的内存分布表），不是存档。")
        else:
            lines.append("这是刚从内存里现扫的实时位置，不是存档。")
        return {
            "ok": True, "display": "\n".join(lines),
            "hint": "原样转发。别说成宣战建议，只报兵力位置。",
            "meta": {"range": res.get("range"), "countries": len(rows),
                     "seeds": seeds[:20]},
        }

    # ── 编制 / 图标 / 前线 ──────────────────────

    def _country_core_seeds(self, short_name: str) -> list[int]:
        """内存里认到的国名（"挪威"）→ 静态本土省列表。0.9.13。

        数据是 watcher 解析存档时落的缓存（country_names.json 反查 tag，
        country_cores.json 给 tag→本土省）。缓存还没建好就返回空列表，
        调用方退回"只按部队圈范围"的老行为，不阻塞查询。
        """
        want = (short_name or "").strip()
        if not want:
            return []
        memo = self._core_memo
        if memo is None:
            names_map: dict[str, str] = {}
            cores_map: dict[str, list[int]] = {}
            try:
                cdir = config.data_dir() / "cache"
                raw = json.loads((cdir / "country_names.json").read_text(encoding="utf-8"))
                names_map = {str(k): str(v) for k, v in (raw.get("map") or {}).items()}
                raw2 = json.loads((cdir / "country_cores.json").read_text(encoding="utf-8"))
                cores_map = {str(k): [int(p) for p in v]
                             for k, v in (raw2.get("map") or {}).items()}
            except Exception:  # noqa: BLE001 缓存没建好/坏了都退回老行为
                pass
            if names_map and cores_map:
                # 只在两份缓存都齐了才备忘；没齐的话下次再试（文件很小）
                self._core_memo = memo = (names_map, cores_map)
        if memo is None:
            return []
        names_map, cores_map = memo
        tag = ""
        for t, zh in names_map.items():
            if zh == want or t == want:
                tag = t
                break
        if not tag:
            hits = [t for t, zh in names_map.items()
                    if zh and (zh in want or want in zh)]
            tag = hits[0] if len(hits) == 1 else ""
        return list(cores_map.get(tag, [])) if tag else []

    def _live_main_forces(self, snap: dict) -> dict | None:
        """局势简报「主力在」一行的实时覆盖层（0.9.14）。

        只窥两份缓存：常驻「省→师分布」表（后台哨兵约 90 秒养一轮）和
        国家名行——零连接、零内存读、毫秒级。任何一环缺（游戏没开、
        哨兵还没扫完一轮、玩家国名对不上）都返回 None，简报照用存档
        分布，宁可不覆盖也不覆盖错。
        """
        try:
            live = self._live()
            age, dist = live.peek_dist(max_age=300.0)
            if age < 0 or not dist:
                return None
            rows = live.peek_named()
            if not rows:
                return None
            meta = snap.get("meta") or {}
            addr = None
            for want in (str(meta.get("player_name") or "").strip(),
                         str(meta.get("player") or "").strip()):
                if not want:
                    continue
                for r in rows:
                    if want == r.get("short") or want == r.get("full"):
                        addr = int(r["addr"])
                        break
                if addr is not None:
                    break
            if addr is None:
                return None
            per: dict[int, int] = {}
            for prov, slot in dist.items():
                n = slot.get(addr)
                if n:
                    per[int(prov)] = int(n)
            if not per:
                return None
            top = sorted(per.items(), key=lambda kv: -kv[1])[:5]
            return {"age": round(age),
                    "total": sum(per.values()),
                    "top": [(self._place_label(p), n) for p, n in top]}
        except Exception:  # noqa: BLE001 覆盖层拿不到就用存档，绝不拖累简报
            return None

    def _place_label(self, pid: int) -> str:
        """省 id -> "柏林（6521，勃兰登堡）" 这种能念的名字。"""
        pid = int(pid)
        city = ""
        state = ""
        try:
            city = self._prov_idx().name(pid) or ""
        except Exception:  # noqa: BLE001
            city = ""
        try:
            places = self._places()
            sid = places._prov_to_state.get(pid)  # noqa: SLF001
            state = places.state_name(sid) if sid else ""
        except Exception:  # noqa: BLE001
            state = ""
        if city and state and city != state:
            return f"{city}（{pid}，{state}）"
        if city or state:
            return f"{city or state}（{pid}）"
        return f"省 {pid}"

    def _oob_text(self) -> str:
        """开局总览顺带交给模型的兵力架构（集团军群→集团军→师）。

        编制这一层只在存档里，内存通道没逆出编组指针。所以必须先看
        `save_match.is_current`：这局没存过盘就给一句能照做的说明，
        绝不拿上一局的集团军名单冒充现在的部队。
        """
        try:
            snap = cache_mod.read_cache(config.data_dir())
        except Exception:  # noqa: BLE001 缓存读不到就不打扰
            return ""
        if not snap:
            return ""
        data = snap.get("oob") or {}
        if not data.get("ok"):
            return ""
        meta = snap.get("meta") or {}
        judge = meta.get("save_match") or {}
        date_text = meta.get("date_text") or ""
        who = meta.get("player_name") or meta.get("player") or "上一局"
        if not judge.get("is_current"):
            return (
                f"※ 兵力架构：这局还没存过盘（手上只有 {date_text} 的 {who} 存档），"
                "现在读不到集团军群 / 集团军 / 师的隶属和位置。"
                "想让她讲编制，先在游戏里存一次档。"
            )
        try:
            return oob_mod.render(data, label_fn=self._place_label,
                                  date_text=date_text, is_current=True)
        except Exception:  # noqa: BLE001 渲染失败不影响前线
            return ""

    # ── 0.9.15：看区域 / 接管部队 ─────────────────

    def _zone(self):
        """区域解析器。沿海表在包里，核心省/国名表在 data 目录。"""
        try:
            cfg = config.load_config()
            game_dir = cfg.get("game_dir") or ""
        except Exception:  # noqa: BLE001
            game_dir = ""
        return zone_mod.Zone(config.data_dir(), game_dir=game_dir or None)

    def _commander(self):
        from . import command as command_mod
        return command_mod.Commander(self._live(), config.data_dir(),
                                     log=lambda m: logs.write(config.data_dir(), m))

    def _place_namer(self):
        """省 → 好读的名字：城市名优先，退州名，最后「省N」。

        州数据读 data/cache 里的两份小表（prov_to_state / state_names，
        watcher 解析存档时写的），不碰存档树。
        """
        import json as _json

        city: dict[int, str] = {}
        try:
            city = self._prov_idx().city_names()
        except Exception:  # noqa: BLE001
            city = {}

        def _load(name: str) -> dict:
            p = config.data_dir() / "cache" / name
            try:
                return {int(k): v for k, v in
                        (_json.loads(p.read_text(encoding="utf-8")).get("map") or {}).items()}
            except Exception:  # noqa: BLE001
                return {}

        state_of = _load("prov_to_state.json")
        state_names = _load("state_names.json")

        def name(pid: int) -> str:
            pid = int(pid)
            nm = city.get(pid)
            if nm:
                return nm
            sid = state_of.get(pid)
            if sid and state_names.get(sid):
                return state_names[sid]
            return f"省{pid}"
        return name

    @llm_tool(
        name="hoi4_look",
        description=(
            "**看一片区域**：哪里有哪些省、我方和别国各有多少师、哪一段空着。"
            "认「欧洲西部海岸线 / 北海海岸 / 波罗的海 / 地中海海岸 / 法国海岸 / "
            "德国的海岸线」这种区域说法，也认地名（诺曼底、布雷斯特）。"
            "用户说「你看一下欧洲西部海岸线」「法国海岸怎么样」「北海那边有兵吗」"
            "「哪段海岸没人守」时调用。"
            "**只读，不改任何东西。**"
        ),
        parameters={
            "type": "object",
            "properties": {
                "area": {"type": "string",
                         "description": "区域说法：欧洲西部海岸线 / 北海海岸 / 法国海岸 / 诺曼底"},
                "country": {"type": "string", "description": "玩哪国，平时留空"},
            },
            "required": ["area"],
        },
    )
    async def hoi4_look(self, *, area: str = "", country: str = ""):
        """看一片区域的兵力态势（海岸线/边境）。"""
        raw = (area or "").strip()
        if not raw:
            return {"ok": False,
                    "display": "要看哪片？说「欧洲西部海岸线」「法国海岸」都行。",
                    "hint": "问用户看哪片。"}

        player, err = await self._player_only(country, guard=False)
        if err is not None:
            return err
        live = self._live()
        live.mark_tool(10.0)
        zone = self._zone()

        def _resolve():
            places = None
            try:
                places = self._places()
            except Exception:  # noqa: BLE001 地名表缺了也能认区域
                places = None
            return zone.resolve(raw, places=places, adjacency=live.adjacency())

        try:
            z = await asyncio.wait_for(asyncio.to_thread(_resolve), timeout=12.0)
        except asyncio.TimeoutError:
            return {"ok": True, "display": "这会儿认区域读得有点久，等一下再问一次。",
                    "hint": "原样转发。别编一份海岸线出来。"}
        except zone_mod.ZoneError as exc:
            return {"ok": False, "display": exc.msg, "hint": exc.hint or "原样转发。"}
        except Exception as exc:  # noqa: BLE001
            return self._agent_error(exc)

        label = self._place_namer()
        try:
            rep = await asyncio.wait_for(
                asyncio.to_thread(zone.report, live, z,
                                  player.get("short") or "", label), timeout=10.0)
        except asyncio.TimeoutError:
            return {"ok": True, "display": "这会儿统计这片区域的兵力有点慢，等一下再问。",
                    "hint": "原样转发。"}
        except Exception as exc:  # noqa: BLE001
            return self._agent_error(exc)

        return {
            "ok": True,
            "display": rep["text"],
            "hint": "原样转发；别补充没报的内容，别自己加「我建议」长段分析。",
            "meta": {"provs": rep["provs"], "empty": rep["empty"],
                     "mine": rep["mine"], "dist_age": rep["dist_age"]},
        }

    @llm_tool(
        name="hoi4_take_command",
        description=(
            "**接管一个集团军并指挥它**（你真的在指挥游戏里的部队）。"
            "action=claim 接管（要 army，比如「第9集团军」）；"
            "status 看现在接管的是谁、多少师、在哪；"
            "brief 看一份**事实清单**（接邻的省、敌情、可调多少师、你上几道令的结果）；"
            "hold **把整支部队铺到一条线上守**（line 说省名/省号，逗号隔开；"
            "哪些省你定，怎么摆由代码按引擎规则执行）；"
            "release 交回指挥权。"
            "**要守线、要摆开、要挡住敌人，一律用 hold 一次说完，不要用 "
            "hoi4_move_units 一个一个搬师**——那是手操单个师用的，搬十几个师既慢又摆不成一条线。"
            "用户说「我把第X集团军指挥权给你」「你来指挥他」「在这条线守好」「停，还给我」时调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "description": "claim / status / assess / hold / attack / release"},
                "army": {"type": "string",
                         "description": "接管哪个集团军（第9集团军 / 9军 / 第九集团军）"},
                "line": {"type": "string",
                         "description": "要守的线：地名或省号，好几个用逗号隔开"},
                "country": {"type": "string", "description": "玩哪国，平时留空"},
            },
            "required": ["action"],
        },
    )
    async def hoi4_take_command(self, *, action: str = "status", army: str = "",
                                line: str = "", country: str = "",
                                target: str = "", units: str = ""):
        """接管/观察/守线/推进/交回。"""
        from . import command as command_mod

        action, army, line, country = _s(action), _s(army), _s(line), _s(country)
        target = _s(target)
        units = _s(units)
        act = action.strip().lower()
        player, err = await self._player_only(country, guard=False)
        if err is not None:
            return err
        live = self._live()
        live.mark_tool(20.0)
        cmd = self._commander()
        short = player.get("short") or ""
        addr = player["addr"]

        try:
            if act in ("claim", "接管", "take", "给你"):
                if not army.strip():
                    table = cmd.armies()
                    if not table:
                        return {"ok": False,
                                "display": "我还不知道你这局有哪些集团军——存档还没解析出来。"
                                           "先存一次档，或者过一分钟再问。",
                                "hint": "让用户存个档，别编集团军名。"}
                    return {"ok": False,
                            "display": "接管哪一个？你现在有：" +
                                       "、".join(f"{n}（{i['count']} 师）"
                                                for n, i in list(table.items())[:10]),
                            "hint": "把候选念给用户，等他说哪个军。"}
                res = await asyncio.wait_for(
                    asyncio.to_thread(cmd.claim, army, addr), timeout=20.0)
                label = self._place_namer()
                by_prov: dict[int, int] = {}
                for u in res["units"]:
                    by_prov[int(u["prov"])] = by_prov.get(int(u["prov"]), 0) + 1
                spots = "、".join(f"{label(p)} {n} 师"
                                  for p, n in sorted(by_prov.items(),
                                                     key=lambda kv: -kv[1])[:6])
                return {"ok": True,
                        "display": f"好，{res['army']} 我接下了：{len(res['units'])} 个师，"
                                   f"现在驻在 {spots}。",
                        "hint": "原样转发。问一句要不要我先看弱点。",
                        "meta": {"army": res["army"], "units": len(res["units"])}}

            if act in ("status", "状态", "看"):
                st = cmd.state()
                if not st:
                    return {"ok": True, "display": "现在没接管任何部队。",
                            "hint": "原样转发。"}
                obs = await asyncio.wait_for(
                    asyncio.to_thread(cmd.observe, addr), timeout=15.0)
                plan_st = {}
                try:
                    plan_st = await asyncio.wait_for(
                        asyncio.to_thread(cmd.plan_state, addr), timeout=15.0)
                except Exception:  # noqa: BLE001 计划状态读不到不影响主报
                    plan_st = {}
                label = self._place_namer()
                spots = sorted({int(u["prov"]) for u in obs["units"]})
                line = (f"我指挥着 {st.get('army')}：{len(obs['units'])} 个师，"
                        f"分布在 {len(spots)} 个省（"
                        + "、".join(label(p) for p in spots[:6]) + "）。")
                if plan_st.get("group"):
                    line += (f"\n这支部队现在{plan_st.get('exec_name') or '状态未知'}"
                             f"（执行类型 {plan_st.get('exec')}，"
                             f"集团军满编 {plan_st.get('size')} 个师席位）")
                return {"ok": True, "display": line,
                        "hint": "原样转发。",
                        "meta": {"exec": plan_st.get("exec"),
                                 "group": plan_st.get("group")}}

            if act in ("brief", "简报", "战况", "看看能打哪", "assess",
                          "弱点", "弱点在哪"):
                # 0.9.18（M4'）：这里原来返回"值得打 / 啃不动"的评分排序——
                # 那是代码替她做判断。现在只给事实，怎么打交回给她。
                b = await asyncio.wait_for(
                    asyncio.to_thread(cmd.brief, addr, short), timeout=28.0)
                label = self._place_namer()
                hhmm = time.strftime("%H:%M:%S", time.localtime(b.get("at") or 0))
                lines = [f"{b['army']} · {b['units']} 个师 · {hhmm}"
                         + (f" · 计划状态：{b['exec_name']}"
                            if b.get("exec_name") else "")]
                spots = "、".join(f"{label(p)}×{n}" for p, n in b["pos"].items())
                lines.append(f"位置：{spots or '没有'}")
                if b.get("no_data"):
                    lines.append("（这轮分布表没读到，下面的敌情是空的，先别下判断）")
                if b["front"]:
                    lines.append(f"接邻的省（{len(b['front'])} 个，按省号排，不代表优先级）：")
                    for r in b["front"]:
                        who = "、".join(f"{k} {v}" for k, v in r["enemy"].items()) or "无"
                        lines.append(
                            f"  {label(r['prov'])}：我方在此 {r['mine_here']} 个师，"
                            f"邻省敌情 {who}（共 {r['enemy_total']}），"
                            f"我方邻省可调 {r['reach']} 个师")
                else:
                    lines.append("没有接邻的省（周围没有别人家的兵）。")
                if b["rear"]:
                    lines.append("后方不接敌：" + "、".join(
                        f"{label(p)}×{n}" for p, n in b["rear"].items()))
                lines.append("我能做的事：调师去某个省 / 按你点名的省守线 / 原地不动。")
                return {"ok": True, "display": "\n".join(lines),
                        "hint": "原样转发或自己判断。**这是事实清单，里面没有建议、"
                                "没有排序、没有分数**——怎么打你自己定。",
                        "meta": {"front": len(b["front"]), "units": b["units"],
                                 "has_advice": False}}

            if act in ("advance", "push", "推", "推进", "推一格", "进攻一格",
                       "attack_one", "打一格"):
                # 推一格：只让挨着目标的那几省部队进攻，别处的守线部队不动。
                # 0.9.23 新加——以前没有"进攻"原语，模型只好用 move 把师调到
                # 远处的敌省，结果线抽空了、师被扔进纵深、后路没人接。
                raw = (line or target or "").strip()
                if not raw:
                    return {"ok": False,
                            "display": "往哪推？给我一个省号或地名（就是紧挨着我们的那一格）。",
                            "hint": "问用户推进目标；一次只推一格。"}
                places = self._places()
                try:
                    pid, lab = places.resolve(raw.split(",")[0].strip())
                except Exception:  # noqa: BLE001
                    return {"ok": False,
                            "display": f"「{raw}」这个地名我没认出来。给我省号也行。",
                            "hint": "让用户给省号，或者说州名/城市名。"}
                try:
                    res = await asyncio.wait_for(
                        asyncio.to_thread(cmd.advance, addr, int(pid), 0,
                                          False, short, units),
                        timeout=45.0)
                except Exception as exc:  # noqa: BLE001
                    return self._agent_error(exc)
                if not res.get("active"):
                    return {"ok": False,
                            "display": "现在没接管任何部队，先说清楚把哪个集团军交给我。",
                            "hint": "先 claim 一个集团军。"}
                return {"ok": True, "display": res.get("note") or "",
                        "hint": "原样转发。推一格不等于打得下来，别替她下结论。",
                        "meta": {"target": int(pid), "label": lab,
                                 "moved": res.get("moved", 0),
                                 "from_provs": res.get("from_provs") or [],
                                 "idle_neighbours": res.get("idle_neighbours") or []}}

            if act in ("offensive", "进攻线", "画线", "推线", "attack_line",
                       "offensive_stop", "撤线", "取消进攻线"):
                stop = act in ("offensive_stop", "撤线", "取消进攻线")
                raw = (target or line or "").strip()
                tid = 0
                if not stop and raw:
                    places = self._places()
                    try:
                        tid, _lab2 = places.resolve(raw.split(",")[0].strip())
                    except Exception:  # noqa: BLE001
                        return {"ok": False,
                                "display": f"「{raw}」这个目标我没认出来。给我省号也行。",
                                "hint": "让用户给目标省号或地名。"}
                try:
                    res = await asyncio.wait_for(
                        asyncio.to_thread(cmd.offensive, addr, int(tid),
                                          units, short, stop, True),
                        timeout=45.0)
                except Exception as exc:  # noqa: BLE001
                    return self._agent_error(exc)
                if not res.get("active"):
                    return {"ok": False,
                            "display": "现在没接管任何部队，先说清楚把哪个集团军交给我。",
                            "hint": "先 claim 一个集团军。"}
                return {"ok": True, "display": res.get("note") or "",
                        "hint": ("原样转发。进攻线是**她画的**，代码只负责一格一格执行；"
                                 "推了一格不等于打下来了，别说成已经拿下。"),
                        "meta": {"line": res.get("line") or {},
                                 "stepped": res.get("stepped"),
                                 "waiting": res.get("waiting"),
                                 "done": res.get("done")}}

            if act in ("hold", "守", "守线", "防线"):
                if not line.strip():
                    return {"ok": False, "display": "守哪条线？说地名，比如「加来、布鲁塞尔、鹿特丹」。",
                            "hint": "问用户守哪里。"}
                # 「前线 / 接敌 / 整条线」这种说法**不用再反过来问用户要省号**：
                # 机械地取「我方控制且挨着交战国」的省。0.9.23 加——真机聊天记录里
                # 模型连着三次回「你把要守的省号给本喵」，因为它手上没有这个集合。
                _front_words = ("前线", "接敌", "整条", "全部", "所有", "前线全部",
                                "front", "all", "自动")
                _lw = line.strip().lower()
                if any(w in _lw for w in _front_words):
                    try:
                        _f = await asyncio.wait_for(
                            asyncio.to_thread(cmd.front, addr), timeout=25.0)
                    except asyncio.TimeoutError:
                        return {"ok": False,
                                "display": "算前线超时了——版图哨兵正在刷新全服控制图"
                                           "（一次十几秒），这会儿抢不上。"
                                           "等十几秒再说一次「铺前线」，"
                                           "或者直接点几个地名让我守。",
                                "hint": "过一会儿重试，或者改成点名几个省。"}
                    except Exception as exc:  # noqa: BLE001
                        return self._agent_error(exc)
                    fprovs = list(_f.get("provs") or [])
                    if not fprovs:
                        if (_f.get("why") or "") == "no_enemy":
                            return {"ok": False,
                                    "display": "还没认出跟谁在打，所以算不出前线在哪。"
                                               "先让我看一眼前线（hoi4_frontline），"
                                               "认出来再铺。",
                                    "hint": "先调 hoi4_frontline 让 warscope 认出交战国。"}
                        return {"ok": False,
                                "display": "版图这会儿是空的——后台正在重扫全服控制图"
                                           "（每 ~85 秒一遍，一次十几秒，转身让给工具查询时"
                                           "就读不到）。等十几秒再说一次「铺前线」就行，"
                                           "不是你的命令有问题。",
                                "hint": "过十几秒重试；或者点名几个省让我守。"}
                    places0 = self._places()
                    labels = []
                    for p in fprovs[:10]:
                        try:
                            labels.append(places0.label(p))
                        except Exception:  # noqa: BLE001
                            labels.append(str(p))
                    _cap0 = max(3, min(len(fprovs), 60))
                    _r0 = await asyncio.wait_for(
                        asyncio.to_thread(cmd.hold, addr, fprovs, _cap0, False, short, units),
                        timeout=45.0)
                    note0 = _r0.get("note") or ""
                    return {"ok": True,
                            "display": (f"前线（我方挨着交战国的省）一共 {len(fprovs)} 个："
                                        + "、".join(labels)
                                        + ("…" if len(fprovs) > 10 else "")
                                        + "\n\n" + note0),
                            "hint": "原样转发。这是机械算出来的接敌省，不是我挑的重点。",
                            "meta": {"front": fprovs, "moved": _r0.get("moved", 0),
                                     "bad": _r0.get("bad") or []}}
                places = self._places()
                provs: list[int] = []
                bad: list[str] = []
                for piece in line.replace("，", ",").split(","):
                    name = piece.strip()
                    if not name:
                        continue
                    try:
                        pid, _lab = places.resolve(name)
                        provs.append(int(pid))
                    except Exception:  # noqa: BLE001
                        bad.append(name)
                if not provs:
                    return {"ok": False,
                            "display": "这些地名我一个都没认出来：" + "、".join(bad),
                            "hint": "让用户换个说法（州名/城市名）。"}
                # max_units 按线的长度给：她一次点名了整条线，就该一次铺完。
                # 以前硬编码 3，铺 34 个省的前线要来回十几趟——「一键铺前线」
                # 根本做不到。上限仍然由 _quotas（每省该摆几个）和手上空闲
                # 师数兜住，不会超发。
                _cap = max(3, min(len(provs), 60))
                res = await asyncio.wait_for(
                    asyncio.to_thread(cmd.hold, addr, provs, _cap, False, short, units),
                    timeout=45.0)
                note = res.get("note") or (
                    "现在没接管任何部队（游戏重开过、或者接管状态过期了）。"
                    "先接管一个集团军我再下守线的令。")
                return {"ok": True, "display": note,
                        "hint": "原样转发。" + ("没认出来的：" + "、".join(bad) if bad else ""),
                        # _fill 把实际下达数放在 moved（以前这里读 done，
                        # 那个键不存在，于是永远报 moved=0）
                        "meta": {"moved": res.get("moved", 0),
                                 "front": res.get("front") or [],
                                 "bad": res.get("bad") or []}}

            if act in ("attack", "推", "进攻", "auto", "开始进攻", "自动",
                       "持续进攻"):
                # 0.9.18（M4'）：这个"让她自己持续推进"的开关关掉了。
                # 用户要的是她自己学打仗，所以往哪推必须她说出来。
                return {"ok": False,
                        "display": "这个自动进攻的开关我关掉了——现在往哪推、派谁，"
                                   "得你自己定。先把战况调出来看，再点名目标省。",
                        "hint": "不要自己替玩家做决定。改用 brief 看事实，"
                                "然后由你（或玩家）指定目标省再动兵。"}

            if act in ("pause", "暂停", "停手"):
                return {"ok": True,
                        "display": "我本来就没在自动跑，不会擅自动你的兵。"
                                   "你说动我才动。",
                        "hint": "原样转发。"}

            if act in ("release", "交回", "还给你", "停"):
                res = await asyncio.wait_for(
                    asyncio.to_thread(cmd.release, False, addr), timeout=30.0)
                if not res.get("released"):
                    return {"ok": True, "display": "我没在指挥任何部队。",
                            "hint": "原样转发。"}
                return {"ok": True,
                        "display": f"{res.get('army')} 还给你了，我不动了。",
                        "hint": "原样转发。"}

            return {"ok": False,
                    "display": f"我不认识「{action}」这个动作。能做的：接管/状态/弱点/守线/进攻/交回。",
                    "hint": "让用户挑一个。"}
        except asyncio.TimeoutError:
            return {"ok": True, "display": "这步读得有点久，先不报了，等一下再试。",
                    "hint": "原样转发。"}
        except command_mod.CommandError as exc:
            return {"ok": False, "display": exc.msg, "hint": exc.hint or "原样转发。"}
        except Exception as exc:  # noqa: BLE001
            return self._agent_error(exc)

    @staticmethod
    def _stale_country_guard(player: dict) -> dict | None:
        """存档不新鲜、又没确认过国家时，只读工具也要先问一句。

        0.9.1 只在调兵里挡了；0.9.2 的编制/前线工具同样会被旧存档带错国家，
        报错国家的战况比不报更糟。
        """
        hint = player.get("hint") or {}
        if hint.get("fresh") or player.get("confirmed"):
            return None
        stale = hint.get("save_date") or "上一份旧存档"
        return {
            "ok": False,
            "display": (f"我这边的存档是 {stale} 那一局的，可能不是你现在这局。"
                        f"你这局玩的是哪国？说一句「我玩的是德国」这样，"
                        f"我就按你说的认下来（这一局不再问）。"),
            "hint": ("先让用户确认国家。确认后重新调用，把 country 填成那个国名。"
                     "不要在这句话前面自己下结论。"),
            "meta": {"need_country": True, "country": player.get("short") or ""},
        }

    @llm_tool(
        name="hoi4_divisions",
        description=(
            "看玩家**有哪些师编制 / 兵种**：每种编制叫什么、**图标是什么兵种**"
            "（坦克 / 伞兵 / 步兵 / 摩托化 / 机械化 / 山地 / 海军陆战）、由哪些营组成（几个坦克营、几个摩步营）、"
            "有几个师在用、现在都在哪几个省。"
            "用户说「我有哪些兵种」「查一下编制」「这个坦克师是什么编成」「那个图标是什么意思」时调用。"
            "**只读。**"
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "show"],
                           "description": "list 列全部；show 看某一个（配合 name）"},
                "name": {"type": "string",
                         "description": "编制名或兵种片段，如「坦克」「伞兵」「1941A」"},
                "country": {"type": "string", "description": "用户明确说玩哪国时填，平时留空"},
            },
            "required": [],
        },
    )
    async def hoi4_divisions(self, action: str = "list", name: str = "",
                             country: str = ""):
        player, err = await self._player_only(country)
        if err is not None:
            return err
        live = self._live()
        try:
            rows = await asyncio.wait_for(
                asyncio.to_thread(live.templates, player["addr"]), timeout=20.0)
        except asyncio.TimeoutError:
            return {"ok": True, "display": "这会儿读编制模板有点慢，先不报了，等一下再问一次。",
                    "hint": "原样转发。别编一份编制出来。"}
        except Exception as exc:  # noqa: BLE001
            return self._agent_error(exc)
        if not rows:
            return {"ok": True,
                    "display": f"{player['short']}现在一个陆军师都没有，也没有编制可查。",
                    "hint": "原样转发。"}
        want = (name or "").strip()
        if want:
            rows = [r for r in rows
                    if want in str(r["template"]) or want in str(r["kind"])]

        if not rows:
            return {"ok": False,
                    "display": f"没找到编制里带「{want}」的师。",
                    "hint": "把现有的图标/编制名列出来让用户挑。"}

        # 0.9.13：按师数降序。以前按扫描顺序取前 12 种，德国这种编制多的
        # 国家，用户点名的坦克师排在后面就整个看不见
        rows = sorted(rows, key=lambda r: -int(r.get("count") or 0))
        lines = [f"{player['short']} 的师编制（图标=游戏里单位牌上的兵种图标）：", ""]
        for r in rows[:18]:
            merged: dict[str, int] = {}
            for i in (r.get("comp") or []):
                zh = livegame.TYPE_ZH.get(str(i["type"]), str(i["type"]))
                merged[zh] = merged.get(zh, 0) + int(i["count"])
            comp = "、".join(f"{k}×{v}" for k, v in merged.items())
            provs = "、".join(self._place_label(p) for p, _n in (r.get("provinces") or []))
            lines.append(f"  图标：{r['kind']}｜{r['template']} —— {r['count']} 个师")
            if comp:
                lines.append(f"      编成：{comp}")
            if provs:
                lines.append(f"      在：{provs}")
        if len(rows) > 18:
            lines.append(f"  ……还有 {len(rows) - 18} 种编制")
        lines.append("")
        lines.append("图标对照：坦克=装甲营为主，伞兵=空降营，步兵=步兵营，"
                     "摩托化/机械化=乘车步兵，山地=山地营，海军陆战=陆战营。")
        return {
            "ok": True, "display": "\n".join(lines),
            "hint": "原样转发。说兵种时就用图标那一栏的词（坦克/伞兵/步兵…）。",
            "meta": {"count": len(rows),
                     "kinds": sorted({str(r["kind"]) for r in rows})},
        }

    @llm_tool(
        name="hoi4_order_of_battle",
        description=(
            "**兵力架构 / 集团军编制**：第几集团军群下辖几个集团军、分别叫什么名字；"
            "第几集团军下辖多少个师、每个师叫什么名字、在哪个省；"
            "全军一共多少师、多少集团军、多少集团军群，分在哪些战区。"
            "用户问「我的集团军怎么编的」「第几集团军有多少师」「哪个集团军群」"
            "「每个师在哪」「兵力架构」「编制」时调用。"
            "数据来自存档（会标日期），不是实时内存；这局没存过盘时会明确说读不到。"
            "**只读。**"
        ),
        parameters={
            "type": "object",
            "properties": {
                "detail": {"type": "string", "enum": ["full", "summary"],
                           "description": "full=完整名单（默认）；summary=只要一句总数"},
            },
            "required": [],
        },
    )
    async def hoi4_order_of_battle(self, detail: str = "full"):
        snap, err = self._snapshot_or_error()
        if err is not None:
            return err
        data = snap.get("oob") or {}
        meta = snap.get("meta") or {}
        if not data.get("ok"):
            return {"ok": True,
                    "display": "这份存档里没读到集团军编组（可能是旧版本存档或解析没成功），"
                               "先在游戏里存一次档再问我。",
                    "hint": "原样转发，别编一份编制出来。"}
        judge = meta.get("save_match") or {}
        is_current = bool(judge.get("is_current"))
        date_text = meta.get("date_text") or ""
        if detail == "summary":
            body = oob_mod.brief(data)
        else:
            body = oob_mod.render(data, label_fn=self._place_label,
                                  date_text=date_text, is_current=is_current)
        notice = "" if is_current else (
            f"※ 这是 {date_text} 的存档编制，不是当前这局的实时部队。\n\n")
        return {
            "ok": True, "display": notice + body,
            "hint": ("原样转发。集团军群 / 集团军 / 师的名字、数量和位置都照 display 说，"
                     "不要编、不要拿别国的部队补；标了存档日期就要说明不是实时。"),
            "meta": {"is_current": is_current, "save_date": date_text,
                     "totals": data.get("totals") or {},
                     "loc_keys": data.get("loc_keys", 0)},
        }

    @llm_tool(
        name="hoi4_frontline",
        description=(
            "**前线 / 交火总览**：只报**参与玩家这局战争的国家**（本国、盟友、敌对国）"
            "之间的陆上交火——在哪些省（一处不落）、"
            "**这块地现在归谁控制（以及法理上归谁）**、"
            "**攻方和守方分别是哪些国家、各几个师**，"
            "以及我国/盟友/交火对手各控制多少省。"
            "别的国家在别处打的仗不在结果里，也不要向用户补述。"
            "用户问「前线怎么样」「哪里在打」「交火地区有哪些」「战况」"
            "「这块地归谁」「谁在进攻」「开局那份情报」时调用。"
            "action=context 是开局时存下的全量快照（含兵力架构）；"
            "action=notes 是后台哨兵的通报。"
            "数据来自实时内存，不是存档；兵力架构那段是存档数据，会标日期。**只读。**"
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["overview", "notes", "context"],
                           "description": "overview=现在的交火（全部）；"
                                          "context=开局那份全量情报；"
                                          "notes=后台哨兵最近的通报"},
                "country": {"type": "string", "description": "用户明确说玩哪国时填，平时留空"},
            },
            "required": [],
        },
    )
    async def hoi4_frontline(self, action: str = "overview", country: str = ""):
        if action == "context":
            ctx = dict(self._front_context)
            if not ctx:
                return {"ok": True,
                        "display": "开局总览还没生成（游戏没开，或者还没说「我玩的是德国」）。",
                        "hint": "原样转发。别编一份前线情况出来。"}
            return {"ok": True,
                    "display": "这是开局时记下的全量前线情报（快照，不是实时数据）：\n\n"
                               + str(ctx.get("text") or ""),
                    "hint": "原样转发；这是开局那一刻的快照，别当成现在的战况。"
                            "只说里面列出的本国/盟友/敌对国，别拿别国的仗补背景。",
                    "meta": {"count": ctx.get("count", 0),
                             "control_ok": ctx.get("control_ok", False)}}
        if action == "notes":
            notes = list(self._front_notes)
            if not notes:
                return {"ok": True, "display": "后台哨兵还没发过通报（游戏没开或者还没到开局总览）。",
                        "hint": "原样转发。"}
            lines = ["后台哨兵最近的通报：", ""]
            lines.extend(f"  · {n}" for n in notes[-12:])
            return {"ok": True, "display": "\n".join(lines), "hint": "原样转发。"}

        live = self._live()
        live.mark_tool(25.0)
        # 认人的 12 秒超时在 _player_only() 里统一兜住，这里别再套一层。
        player, err = await self._player_only(country)
        if err is not None:
            return err
        try:
            # 宿主对每个 entry 有 30 秒看门狗。`fast=True` 让省内兵力只读缓存，
            # 战斗列表 / 控制者 / 攻守方都照读；哨兵用默认参数把缓存养热。
            res = await asyncio.wait_for(
                asyncio.to_thread(live.frontline, player["addr"], True), timeout=20.0)
        except asyncio.TimeoutError:
            logs.write(config.data_dir(),
                       "hoi4_frontline：前线数据超过 20 秒，先返回不让宿主砍掉整个调用")
            return {"ok": True,
                    "display": "这次前线数据读得太久（游戏里交火多，全服扫描还没完），"
                               "先不报了，等一下再问一次。",
                    "hint": "原样转发。别编一份战况出来。"}
        except Exception as exc:  # noqa: BLE001
            return self._agent_error(exc)
        try:
            # 只读缓存。全服版图由哨兵每 30 秒在后台养热；工具绝不自己起扫描，
            # 否则工具的几百条 RD 会和后台扫描在 DLL 的 g_cmd_lock 上互相排队。
            ctl = live.control_map(peek_only=True)
        except Exception as exc:  # noqa: BLE001 控制图挂了不影响交火列表
            logs.write(config.data_dir(),
                       f"hoi4_frontline 取全服控制图失败：{type(exc).__name__}: {exc}")
            ctl = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        names = ctl.get("names") or {}
        counts = ctl.get("counts") or {}

        def _control_lines() -> list[str]:
            if not ctl.get("ok"):
                if ctl.get("pending"):
                    return ["全服版图还在后台更新，这次先报交火省的控制者；"
                            "过一会儿再问就有全局版图了。"]
                return ["全服控制图这次没读到（" + str(ctl.get("error") or "扫描失败")
                        + "）；下面只报交火省的控制者，别当成全局版图。"]
            mine = counts.get(int(res.get("player_id") or 0), 0)
            # 0.9.9：不再报"全服多少省、多少个控制国"。那不是玩家能知道的，
            # 也不是她要的——只列本国 / 盟友 / 敌对国。
            out = [f"版图（只列与这局有关的国家）：我国（{res['player']}）控制 "
                   f"{mine} 个省。"]
            by_name = {v: k for k, v in names.items()}
            friendly = [f"{nm} {counts.get(by_name[nm], 0)} 个省"
                        for nm in (res.get("allies") or []) if nm in by_name]
            if friendly:
                out.append("盟友控制：" + "、".join(friendly[:8]) + "。")
            bits = [f"{nm} {counts.get(by_name[nm], 0)} 个省"
                    for nm in (res.get("enemies") or []) if nm in by_name]
            if bits:
                out.append("交火对手控制：" + "、".join(bits[:10]) + "。")
            return out

        scope_note = frontwatch_mod.FrontWatch._scope_note(res)
        if not res["count"]:
            return {
                "ok": True,
                "display": (f"{res['player']}：现在没有与你这局有关的陆上交火。\n"
                            + scope_note + "\n" + "\n".join(_control_lines())),
                "hint": "原样转发。没读到相关交火时别说「整条前线很安静」，"
                        "更不要拿别的国家的仗来补。",
                "meta": {"combats": 0, "control_ok": bool(ctl.get("ok")),
                         "allies": res.get("allies") or [],
                         "war_scope": warscope_mod.public(res.get("war_scope"))},
            }
        lines = [f"{res['player']}：与你这局有关的交火 {res['count']} 处。",
                 scope_note]
        lines.extend(_control_lines())
        lines.extend(["", "这些交火地区："])

        def _nations(d):
            return "、".join(
                f"{k} {v} 个师" for k, v in
                sorted((d or {}).items(), key=lambda kv: -kv[1])[:4]) or "没读到"

        for row in res["combats"]:            # ← 全量，一处不落
            label = self._place_label(row["prov"])
            ctrl = row.get("controller") or "控制者没读出来"
            owner = row.get("owner") or ""
            note = f"（法理属{owner}）" if owner and owner != ctrl else ""
            atk, dfs = row.get("attacker") or {}, row.get("defender") or {}
            if atk or dfs:
                body = f"攻 {_nations(atk)}；守 {_nations(dfs)}"
            else:
                body = "、".join(
                    f"{s['country']}{'（你）' if s['mine'] else ''} {s['units']} 个师"
                    for s in (row.get("sides") or [])) or "省内兵力没读出来"
            lines.append(f"  {label}：{ctrl}{note}｜{body}")
        lines.append("")
        lines.append("实时内存里的战斗列表，不是存档，而且只列参与你这局战争的国家。"
                     "「控制」是这块地现在归谁，括号里是法理所有者；"
                     "攻/守按参战师反查所属国家，跟「省里恰好有哪些师」不是一回事。")
        try:
            oob = self._oob_text()
        except Exception:  # noqa: BLE001 编制读不到不影响前线
            oob = ""
        if oob:
            lines.extend(["", oob])
        return {
            "ok": True, "display": "\n".join(lines),
            "hint": ("原样转发。按省报，不要说成已经赢了/输了。"
                     "只说这局参战国的事，别提其他国家的交火或兵力调动；"
                     "兵力架构那段如果标了存档日期，要照着说清它不是实时数据。"),
            "meta": {"combats": res["count"], "control_ok": bool(ctl.get("ok")),
                     "allies": res.get("allies") or [],
                     "enemies": res.get("enemies") or [],
                     "war_scope": warscope_mod.public(res.get("war_scope"))},
        }

    @llm_tool(
        name="hoi4_move_units",
        description=(
            "**真的到游戏里把部队调走**——以玩家身份下单，等同于你右键地图，"
            "不是控制台作弊，存档和成就都不受影响。"
            "会先按陆地邻接算出整条行军路线，再用引擎自己的下单函数写进部队，"
            "所以走的是你平时看见的那条路，不会穿海、不会瞬移。"
            "用户说「把柏林的部队调到华沙」「3 个师挪到东边」「部队回防」时调用。"
            "出发地和目的地都能直接说州名或城市名（柏林/华沙/莫斯科），也可以给省编号。"
            "说州名（勃兰登堡）时算的是**全州**的师；说城市名（柏林）时只算那一个省。"
            "**这是手操一两个师用的**。要守一条线、要摆开整支部队、要挡住敌人，"
            "用 hoi4_take_command 的 action=hold 一次说完（它按引擎规则摆，不会撞车）。"
            "用户点了兵种（「那个伞兵师」「装甲师」）就用 unit_type 指定，"
            "不指定就按列表顺序挑（容易挑错）。"
            "默认把出发地里的师全调走；只想调一部分就填 count。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "from_place": {"type": "string", "description": "出发地：城市名（只算那个省）、州名（算全州）或省编号"},
                "to_place": {"type": "string", "description": "目的地：城市名、州名或省编号"},
                "count": {"type": "integer", "description": "只调几个师。留 0 表示这个出发地里的师全调走。"},
                "unit_type": {"type": "string",
                              "description": "只调这个兵种：伞兵/空降/装甲/坦克/步兵/摩托化/机械化/骑兵/山地/海军陆战。用户没说兵种就留空。"},
                "only_mine": {
                    "type": "boolean",
                    "description": "只动 YUI 接管的那几个师（用户按过 F7 的那几支）。他没特别说「只动你的」就留空。",
                },
                "country": {"type": "string", "description": "用户明确说「我玩的是德国」时填国名，用来认玩家国家。平时留空。"},
            },
            "required": ["from_place", "to_place"],
        },
    )
    async def hoi4_move_units(self, *, from_place: str, to_place: str,
                              count: int = 0, country: str = "",
                              only_mine: bool = False, unit_type: str = ""):
        return await self._do_move_live(from_place, to_place, count, country,
                                        only_mine, unit_type)

    @llm_tool(
        name="hoi4_map_check",
        description=(
            "检查「能不能替你点地图」：连桥接、把视角缩到最小、量出地图在屏幕上的位置，"
            "报出标定结果和几个省算出来的屏幕坐标。"
            "用户问「你能操作我的游戏吗」「为什么点不动」「点歪了」时调用。"
            "**不会点任何东西**，纯检查。"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
    )
    async def hoi4_map_check(self):
        def _run():
            ui, view, idx = self._player_view()
            samples = []
            for pid in (1, 64, 100, 3000, 9000):
                if idx.has(pid):
                    samples.append((pid, view.to_screen(*idx.px(pid))))
            return view, idx, samples

        try:
            view, idx, samples = await asyncio.to_thread(_run)
        except UiError as exc:
            return {"ok": False, "display": str(exc), "hint": "原样转发，别含糊。"}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "display": f"检查时出错：{type(exc).__name__}: {exc}",
                "hint": "原样转发。",
            }

        lines = [
            "地图坐标对上了，现在能替你点地图。",
            "",
            f"  {view.describe()}",
            f"  {idx.describe()}",
            "",
            "抽样几个省算出来的屏幕坐标：",
        ]
        for pid, (sx, sy) in samples:
            lines.append(f"  省 {pid:>5} -> ({sx}, {sy})")
        return {
            "ok": True,
            "display": "\n".join(lines),
            "hint": "原样转发。如果用户说点歪了，让他看看这些坐标对不对。",
            "meta": {"scale": round(view.scale, 4), "source": view.source},
        }

    # ── 进程内：真·操作游戏（国策 / 调兵） ──────────────
    #
    # 这两件事都不走控制台、也不模拟鼠标：DLL 住在游戏进程里，
    # 直接读写对象、调引擎自己的函数。好处是后台就能干、不会点歪；
    # 代价是偏移绑版本，所以写之前必须过 _guard_write 那道闸。

    def _live(self) -> livegame.LiveGame:
        """按需建进程内客户端。不预热——注入要两三秒，只在真要用时做。"""
        if self._live_svc is None:
            cfg = self._cfg or config.load_config()
            self._live_svc = livegame.LiveGame(
                data_dir=config.data_dir(),
                log=lambda msg: logs.write(config.data_dir(), msg),
                allow_unknown_build=bool(cfg.get("allow_unknown_build", False)),
            )
        return self._live_svc

    def _plan(self, country_addr: int) -> battleplan_mod.BattlePlan:
        """进程内划线通道。跟调兵共用同一个 agent，只是读的是战区前线。"""
        ag = nekoagent.ensure()
        build = self._live()._build()
        return battleplan_mod.BattlePlan(
            ag, int(country_addr), ag.base, int(build.get("rva_new", 0)))

    def _player_hint(self) -> dict:
        """从最近一份存档快照里取「你是谁」。

        存档这边的 watcher 一直在跑，meta 里就有玩家 tag 和中文名。
        但快照可能很旧（换了一局没存过档），所以要把"多旧"一起带出来——
        拿一个月前的存档去认现在的国家，会认错人。
        """
        snap = cache_mod.read_cache(config.data_dir()) or {}
        meta = snap.get("meta") or {}
        mtime = float(meta.get("save_mtime") or 0)
        age = (time.time() - mtime) if mtime else -1.0
        return {
            "tag": str(meta.get("player") or ""),
            "name": str(meta.get("player_name") or ""),
            "save_date": meta.get("date_text") or "",
            "age": age,
            "fresh": 0 <= age < 600,
        }

    async def _locate_player(self, country_hint: str = "") -> dict:
        """找出玩家国家。找不到抛 LiveError，里面写着下一步该做什么。

        第一次用的时候 data/ 里还没有存档快照（watcher 才刚起步），
        这时不要凭空猜，先把"去读存档"这件事说出来——猜错国家比晚十秒严重得多。
        """
        hint = self._player_hint()
        live = self._live()
        explicit = (country_hint or "").strip()

        # 先看"用户上一句亲口认过的那个"——它比旧存档里的玩家名可信。
        # 0.9.7：**带 explicit 也要先比这一下**。模型每轮都会把用户说过的
        # 「德国」原样填进 country，旧代码一见 explicit 就跳过快路径去
        # live.locate() 全服扫 406 个 CCountry（日志里的"国名精确匹配"），
        # 一次十几秒，正好把 30 秒看门狗撞穿。确认过的国家没必要再扫。
        confirmed = await asyncio.to_thread(live.confirmed_country)
        if confirmed:
            want = explicit
            got_short = str(confirmed.get("short") or "")
            got_full = str(confirmed.get("full") or "")
            if (not want or want == got_short or want == got_full
                    or (got_short and (want in got_short or got_short in want))
                    or (got_full and (want in got_full or got_full in want))):
                confirmed["hint"] = hint
                return confirmed
        if not hint["name"] and not explicit:
            watcher = self._ensure_watcher()
            st = watcher.status()
            if st.get("state") == "parsing":
                raise livegame.LiveError(
                    "no_country",
                    "我正在读你的存档，还要十几二十秒——读完就知道你这局玩的是哪国了。",
                    "原样转发，让用户等一会儿再问。别催、也别猜。",
                )
            # 没在解析，说明要么没找到存档，要么这局还没存过
            raise livegame.LiveError(
                "no_country",
                "我还不知道你这局玩的是哪一国。"
                "手上没有能用的存档（第一次用要先读一遍），我也没打算瞎猜——"
                "认错国家就会去动别人的部队。"
                "你在游戏里按一下保存，我读到就知道；或者直接告诉我你玩哪国。",
                "先让用户存个档，或者直接问他「你这局玩的是哪国」。"
                "确认后重调工具，把 country 填成那个国名。",
            )
        want = explicit or hint["name"]
        # 0.9.22 修：模型常把存档 meta 里的国家 **TAG**（GER）原样填进 country，
        # 而内存里存的是**本地化国名**（德国），拿 "GER" 去对当然对不上，
        # 报出来就是「没在内存里认出「GER」」——真机就是这么卡住的。
        # 三个大写字母一律先当 TAG，用资料库的 tag→国名 表翻成中文再匹配。
        if re.fullmatch(r"[A-Za-z]{3}", want or ""):
            try:
                from . import panel as _panel
                _names = (_panel.current_names() or {}).get("countries") or {}
                got = _names.get(want.upper()) or _names.get(want)
                if got:
                    _trace(f"3.11 国名 TAG {want} -> {got}")
                    want = str(got)
            except Exception:  # noqa: BLE001 翻不出来就原样试，别把路堵死
                pass
        info = await asyncio.to_thread(live.locate, hint["tag"], want, 0, False)
        # 用户亲口说了国名 → 记下来，这一局里不再问第二遍
        if (country_hint or "").strip():
            try:
                live.confirm(info["addr"], info["short"])
                info["confirmed"] = True
            except Exception:  # noqa: BLE001 记不上就当没记住，不影响这次操作
                pass
        info["hint"] = hint
        return info

    @staticmethod
    def _agent_error(exc: Exception) -> dict:
        """把进程内客户端的失败翻译成用户能照做的话。"""
        msg = getattr(exc, "message", "") or str(exc)
        if not msg:
            # 空消息的坑：`asyncio.TimeoutError` 的 str() 就是空串，直接透传
            # 会让用户看到一片空白（0.9.23 真机踩到）。
            msg = (f"{type(exc).__name__}：拿不到数据。"
                   f"多半是后台正在全服扫描，等十几秒再试一次。")
        hint = getattr(exc, "hint", "") or "原样转发，别含糊成「出错了」。"
        return {"ok": False, "display": msg, "hint": hint}

    async def _do_move_live(self, from_place: str, to_place: str,
                            count: int = 0, country: str = "",
                            mine: bool = False, unit_type: str = "",
                            unit_spec: str = "") -> dict:
        """调兵全流程：认地名 → 认人 → 找师 → 算路 → 下单 → 报告。"""
        from_place, to_place = _s(from_place), _s(to_place)
        try:
            places = self._places()
            from_pid, from_label, from_sid, from_is_state = places.resolve_detail(from_place)
            to_pid, to_label, _to_sid, _to_is_state = places.resolve_detail(to_place)
        except ValueError as exc:
            return {"ok": False, "display": str(exc), "hint": "把候选说出来让用户确认。"}
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "display": f"地名表读不出来（{type(exc).__name__}: {exc}）。",
                "hint": "让用户直接说省编号，或者说清州名。",
            }

        try:
            player = await self._locate_player(country)
        except livegame.LiveError as exc:
            return {"ok": False, "display": exc.message, "hint": exc.hint}
        except Exception as exc:  # noqa: BLE001
            return self._agent_error(exc)

        live = self._live()
        try:
            units = await asyncio.to_thread(live.units, player["addr"])
        except Exception as exc:  # noqa: BLE001
            return self._agent_error(exc)

        # 认人是猜的，就得先说清猜的是谁——认错国家比调不动严重得多。
        # 读 units 是为了让这句问话有依据：猜出来的国家一个师都没有，
        # 那就不是"确认一下"，而是"这份旧存档根本不是你这一局"。
        if (not player["hint"]["fresh"] and not player.get("confirmed")
                and not (country or "").strip()):
            guess = str(player["short"] or "?")
            stale = str(player["hint"].get("save_date") or "上一份旧存档")
            # 第一行必须是一句能直接念出来的问话：语音播报只念第一行
            if units:
                msg = (f"我认到你玩的是「{guess}」——对吗？对就说「是我」，"
                       f"不对就说「我玩的是德国」这样。\n"
                       f"（是按 {stale} 那份旧存档认的，它名下 {len(units)} 个师；"
                       f"可能不是你现在这局。）")
            else:
                msg = (f"你这局玩的是哪国？说一句「我玩的是德国」这样，"
                       f"我马上把这条令办了。\n"
                       f"（手上只有 {stale} {guess} 那局的旧存档，那国一个师都没有，"
                       f"跟你现在这局对不上。）")
            return {
                "ok": False, "display": msg,
                "hint": ("先让用户确认国家。确认后重新调用，把 country 填成那个国名。"
                         "不要在这句话前面自己下结论说已经调好了。"),
                "meta": {"need_country": True, "country": guess},
            }

        # 出发地说的是"州"时，要覆盖全州的省。只按一个代表省过滤，
        # "把勃兰登堡的师调到柏林"会因为那个师站在同州别的省而报"没部队"。
        if from_is_state and from_sid is not None:
            _prov_set = set(places.provinces_of_state(from_sid))
        else:
            _prov_set = {from_pid}

        group = [u for u in units if u["prov"] in _prov_set]
        if unit_type:
            # 用户说"那个伞兵师/坦克师"时，按图标兵种+编成挑，别按列表顺序抓第一个
            picked = [u for u in group if livegame.unit_matches(u, unit_type)]
            if not picked:
                have = livegame.LiveGame.template_summary(group)
                return {
                    "ok": False,
                    "display": (f"{from_label} 里没有「{unit_type}」。\n"
                                + (f"那里现有：{have}" if have else "那里一个师都没有。")),
                    "hint": "把现有的编制名/图标列出来让用户挑，别自己换一个兵种去调。",
                    "meta": {"unit_type": unit_type, "available": have},
                }
            group = picked
        if (unit_spec or "").strip():
            # 微操：她点名要动的是**哪几个师**（番号/兵种/uid）。
            # 以前只能按"出发地"整省抓，想单独把一个师绕出去做包围做不到。
            from . import command as command_mod
            _picked = command_mod.pick_from(
                unit_spec, group, command_mod.load_unit_names(config.data_dir()))
            if not _picked["units"]:
                have = livegame.LiveGame.template_summary(group)
                return {
                    "ok": False,
                    "display": (f"{from_label} 里没有你说的那几个师：「{unit_spec}」。\n"
                                + (f"那里现有：{have}" if have else "那里一个师都没有。")),
                    "hint": "把现有的番号/编制名列出来让用户挑，别自己换一个师去调。",
                    "meta": {"units": unit_spec, "available": have},
                }
            group = _picked["units"]
            if _picked["miss"]:
                self._log(f"调兵点名：没认出 {'、'.join(_picked['miss'])}")
        if mine:
            # 只动"归我指挥"的那几个师。用户按 F7 交过来的那几支，
            # 跟"这个省里所有的师"经常不是一回事——不分清就是抢兵权。
            booked = set(self._squads().unit_addrs())
            group = [u for u in group if int(u["addr"]) in booked]
            if not group:
                return {
                    "ok": False,
                    "display": f"{from_label} 里的师都不归我指挥（我手上的都不在这个省）。"
                               "要我先接管，就在游戏里把鼠标放上去按 F7，"
                               "或者说一句「把 X 的部队交给你」。",
                    "hint": "原样转发；不要自作主张改成调全部。",
                }
        if not group:
            lines = [f"{from_label} 里没有你的部队。"]
            if units:
                lines.append("")
                lines.append(f"你现在 {len(units)} 个师在这几个省：")
                lines.extend(livegame.LiveGame.describe_units(units, 8))
            return {
                "ok": False,
                "display": "\n".join(lines),
                "hint": "让用户从上面这些省里挑一个，或者确认出发地说错了。",
            }
        if count and count > 0:
            group = group[:count]

        try:
            res = await asyncio.to_thread(
                live.move_many, player["addr"], [u["addr"] for u in group], to_pid
            )
        except livegame.LiveError as exc:
            return {"ok": False, "display": exc.message, "hint": exc.hint}
        except Exception as exc:  # noqa: BLE001
            return self._agent_error(exc)

        ok_n = sum(1 for r in res["results"] if r["ok"])
        paths = res.get("paths") or ({res["from"]: res["path"]} if res.get("path") else {})
        lines = [
            f"已经把 {from_label} 的 {ok_n}/{len(group)} 个师调往 {to_label}。",
            "",
        ]
        moved_tpl = livegame.LiveGame.template_summary(group, 3)
        if moved_tpl:
            lines.append(f"  动的是：{moved_tpl}")
        if len(paths) <= 1:
            one = next(iter(paths.values()), [])
            lines.append(f"  路线：{' → '.join(str(p) for p in one[:8])}"
                         + (" → …" if len(one) > 8 else ""))
            lines.append(f"  共 {len(one)} 个省，{one[-1] if one else '?'} 是终点")
        else:
            lines.append(f"  分了 {len(paths)} 个出发省，每个省走自己的路线：")
            for src, one in list(paths.items())[:6]:
                lines.append(f"    省 {src}：{' → '.join(str(p) for p in one[:6])}"
                             + (" → …" if len(one) > 6 else ""))
            if len(paths) > 6:
                lines.append(f"    ……还有 {len(paths) - 6} 个出发省")
        lines.extend([
            "",
            "这是引擎自己的下单函数，跟你右键地图走的是同一条路，存档会正常记。",
            "**我只能确认命令写进部队了，不能确认它已经走到。**想确认就过一会儿看部队在不在路上。",
        ])
        if not res.get("dst_ours", True):
            # 目标不是我们的地盘 = 这一步是进攻。引擎收订单，但没宣战不会执行。
            # 0.9.22 真机：用户看到"已下令"却一直不动，必须当场说清楚。
            owner = res.get("dst_owner") or "那个国家"
            lines.extend([
                "",
                f"⚠ 注意：{to_label} **现在不是你的领土**（归 {owner}）。",
                f"   命令进了引擎，但只有你已经对 {owner} **宣战时才会真的开进去**；"
                f"没宣战的话它会一直挂着不动 —— 那不是命令丢了，是目标还不合法。",
            ])
        # 跨省太远的提醒（0.9.23）：以前模型用 move 当进攻，把师从线上扔到纵深
        # 好几个省外的敌省，后路没人接。这里只陈述事实并指向正确工具，不替她决定。
        longest = max((len(p) for p in paths.values()), default=0)
        if longest >= 3:
            lines.extend([
                "",
                f"⚠ 这一单跨了 {longest} 个省（最长那条路线）。中途没有我方部队接应，"
                f"师单独扎进纵深容易被切断后路。",
                "   **推进的正规做法是 `take_command action=advance`，一次推一格**："
                "它只动挨着目标那一格的部队，别处的守线部队不动，也不会把线抽空。",
            ])
        if ok_n < len(group):
            bad = [r for r in res["results"] if not r["ok"]]
            lines.append("")
            lines.append(f"有 {len(bad)} 个师没下成：{bad[0].get('error') or '原因不明'}")
        return {
            "ok": ok_n > 0,
            "display": "\n".join(lines),
            "hint": "原样转发。说「已经下令」，不要说「已经走到了」。",
            "meta": {
                "from": from_pid, "to": to_pid, "count": ok_n,
                "path": (res.get("path") or [])[:40], "player": player["short"],
                "paths": {str(k): v[:40] for k, v in list(paths.items())[:20]},
                "unit_type": unit_type,
            },
        }


    @plugin_entry(
        id="hoi4_move",
        name="调兵",
        description=(
            "把《钢铁雄心4》里一支部队从 A 调到 B。**不是控制台作弊**，"
            "是用引擎自己的下单函数把行军路线写进部队，跟你右键地图一样。"
            "**这是调动，不是进攻**：跨好几个省把师送进敌方纵深，后路没人接应，很容易被切断。"
            "要往前打，用 `hoi4_take_command action=advance`，一次推一格——"
            "它只动挨着目标那一格的部队，守线的师不用撤下来，线也不会空。"
            "这里只用来：把预备队送去某个我方省、回防、换防、上岸后集结，"
            "**以及微操**——想单独把某一个师绕到某个省（做包围、堵口子），"
            "用 units 点名那个师。"
            "用户说「把柏林的部队调到华沙」「让第3山地师绕到但泽」时使用。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "from_place": {"type": "string", "description": "出发地：州名或省编号"},
                "to_place": {"type": "string", "description": "目的地：州名或省编号"},
                "units": {"type": "string",
                          "description": "只动这几个师：番号「第3山地师」/兵种「装甲」/"
                                         "uid「96,97」，逗号分隔。留空=整个出发地都动"},
            },
            "required": ["from_place", "to_place"],
        },
        llm_result_fields=["ok", "display"],
    )
    async def entry_move(self, from_place: str = "", to_place: str = "",
                         units: str = "", **_):
        fp, tp = _s(from_place).strip(), _s(to_place).strip()
        if not fp or not tp:
            return Ok({
                "ok": False,
                "display": "要说清从哪调到哪。比如「把柏林的部队调到华沙」。",
                "hint": "原样转发。",
            })
        return Ok(await self._do_move_live(fp, tp, unit_spec=_s(units)))

    @plugin_entry(
        id="hoi4_units_near",
        name="看某地附近的部队",
        description=(
            "看《钢铁雄心4》里某个地方附近的部队，敌我都报。"
            "说国名（波兰）就以那个国家的部队为中心，说地名（柏林）就以那个省为中心。"
            "用户说「波兰附近有什么部队」「东边边境兵力怎么样」时使用。只读。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "where": {"type": "string", "description": "国名（波兰）或地名（柏林）"},
                "depth": {"type": "integer", "description": "往外扩几省，0 = 自动"},
            },
            "required": ["where"],
        },
        llm_result_fields=["ok", "display"],
    )
    async def entry_units_near(self, where: str = "", depth: int = 0,
                               country: str = "", **_):
        return Ok(await self.hoi4_units_near(where=where, depth=depth,
                                             country=country))

    _trace("3.2 entry_units_near 注册完成")

    @plugin_entry(
        id="hoi4_look",
        name="看一片海岸线/区域",
        description=(
            "看《钢铁雄心4》里一片区域（海岸线/边境）的兵力：哪里有哪些省、"
            "我方和别国各有多少师、哪一段空着。认「欧洲西部海岸线」「法国海岸」"
            "「北海海岸」「波罗的海」「地中海海岸」这些说法。只读。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "area": {"type": "string",
                         "description": "区域：欧洲西部海岸线 / 法国海岸 / 北海海岸"},
                "country": {"type": "string", "description": "玩哪国，平时留空"},
            },
            "required": ["area"],
        },
        llm_result_fields=["ok", "display"],
    )
    async def entry_look(self, area: str = "", country: str = "", **_):
        return Ok(await self.hoi4_look(area=area, country=country))

    @plugin_entry(
        id="hoi4_take_command",
        name="接管一个集团军",
        description=(
            "接管《钢铁雄心4》里的一个集团军并指挥它。"
            "action=brief 返回**战况事实清单**（我方几个师在哪、每个接邻的省里有"
            "哪国几个师、我方邻省能调几个师），里面没有建议也没有排序，打哪由你自己判断；"
            "action=hold 用 line 点名要守的省，代码按游戏的摆兵规则把部队摆过去；"
            "line 直接写「前线」「接敌」也可以——那会机械地取「我方挨着交战国的省」，"
            "**不要反过来问用户要省号**；"
            "action=advance 推一格（微操的基本动作）：用 target(line) 点名"
            "**紧挨着我们的那一格**，只让挨着它的我方部队进攻它，别处的守线部队不动；"
            "action=offensive **画进攻线**（仿原游戏那条）：给 target（目标省）+ "
            "units（派哪几个师），**整条前线上的部队同时向前压**——战线是一整条"
            "连续的横线整体前移，不是一个师从一个省宽的缺口往里钻；"
            "每个出发省各留 1 个填线，后方省让部队整个跟上新战线；"
            "上一轮还在打的省不重复下令。"
            "action=offensive_stop 撤掉进攻线。"
            "**units 用来点名具体哪几个师**（点谁守哪、派谁去打），"
            "三种写法都认、可混用逗号分隔：番号「第3山地师」、兵种「装甲/山地/SS/摩托化」、uid「96,97」。"
            "**推进就用 advance 或 offensive，不要用 move 把师调到远处的敌省**——"
            "move 会让那支师离开防线、跨好几个省扎进纵深，后路没人接，很容易被切断。"
            "**守线的部队照样能进攻**：它们就在线上挨着敌人，直接就能打，"
            "不需要先撤下来，也不会把线抽空。"
            "用户说「我把第X集团军指挥权给你」「看看战况」「守加来、布鲁塞尔」"
            "「让第3山地师打克拉科夫」「朝华沙画一条进攻线」「还给我」时使用。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "description": "claim / status / brief / hold / advance / "
                                          "offensive / offensive_stop / release"},
                "army": {"type": "string", "description": "第9集团军"},
                "line": {"type": "string",
                         "description": "要守的地名，逗号隔开；或写「前线」（hold 用）"},
                "target": {"type": "string",
                           "description": "要推的那一格 / 进攻线的目标省：省号或地名"},
                "units": {"type": "string",
                          "description": "派哪几个师：番号「第3山地师」/兵种「装甲」/uid「96,97」，"
                                         "逗号分隔（hold / advance / offensive 用）"},
                "country": {"type": "string", "description": "玩哪国，平时留空"},
            },
            "required": ["action"],
        },
        llm_result_fields=["ok", "display"],
    )
    async def entry_take_command(self, action: str = "status", army: str = "",
                                 line: str = "", country: str = "",
                                 target: str = "", units: str = "", **_):
        return Ok(await self.hoi4_take_command(
            action=action, army=army, line=line, country=country,
            target=target, units=units))

    _trace("3.2 entry_look / entry_take_command 注册完成")

    @plugin_entry(
        id="hoi4_health",
        name="插件体检",
        description=(
            "检查这条链路通没通：游戏开没开、代理注入没有、代理版本新旧、偏移配不配。"
            "四步每步给结论和下一步指引。游戏没开也能查。"
        ),
        input_schema={"type": "object", "properties": {}, "required": []},
        llm_result_fields=["ok", "display"],
    )
    async def entry_health(self, **_):
        return Ok(await self.hoi4_health())

    _trace("3.3 entry_health 注册完成")

    # ── 国策（国家焦点） ──────────────────────────

    @llm_tool(
        name="hoi4_focus",
        description=(
            "看和选《钢铁雄心4》的国家焦点（国策）。"
            "action=list 列出现在能点的国策和正在研究的那个；now 只看当前进度；"
            "pick 选一个（name 填序号或名字片段，比如「四年计划」「莱茵兰」）；cancel 取消当前的。"
            "**以玩家身份在国策树里点选，不是控制台作弊**：写进去之后引擎自己推进、"
            "自己完成、自己解锁后续节点，跟你手动点一下一模一样。"
            "用户说「下一个国策研究XX」「现在在研什么」「这个别研究了换一个」时调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "list / now / pick / cancel"},
                "name": {"type": "string", "description": "pick 时填：列表序号或国策名片段"},
                "country": {"type": "string", "description": "用户明确说自己玩哪国时填国名。平时留空。"},
            },
            "required": ["action"],
        },
    )
    async def hoi4_focus(self, *, action: str = "list", name: str = "", country: str = ""):
        act = (action or "list").strip().lower()
        try:
            player = await self._locate_player(country)
        except livegame.LiveError as exc:
            return {"ok": False, "display": exc.message, "hint": exc.hint}
        except Exception as exc:  # noqa: BLE001
            return self._agent_error(exc)

        live = self._live()
        try:
            st = await asyncio.to_thread(live.focus_state, player["addr"])
        except Exception as exc:  # noqa: BLE001
            return self._agent_error(exc)

        who = player["short"] or "你这局的国家"
        stale = (not player["hint"]["fresh"]
                 and not player.get("confirmed")
                 and not (country or "").strip())

        # 身份没确认之前，连"列国策"都先问一句。
        # 不是多此一举：按旧存档认成卢森堡的话，列出来的是**卢森堡**的国策树，
        # 用户会以为自己在研"专注轰炸机"。错的信息比没有信息更麻烦。
        if stale:
            return {
                "ok": False,
                "display": (
                    f"我按手上那份存档认到你玩的是「{who}」"
                    + (f"（{player['hint']['save_date']} 的档）" if player["hint"]["save_date"] else "")
                    + "，但这可能不是现在这局。\n"
                    "认错了国策树就全看错人了，所以先跟你确认一句："
                    "你这局玩的是哪国？"
                ),
                "hint": (
                    "先问用户这局玩的是哪国。他说了之后重调本工具，把 country 填成那个国名。"
                    "如果他说「就是 X」，也照样填上再调一次，别自己跳过这一步。"
                ),
            }

        if act == "now":
            if not st["current"]:
                body = f"{who}现在没有在研究的国策。"
            else:
                body = f"{who}正在研究：{st['current_name'] or '（名字没读到）'}（进度累积 {st['progress']}）"
                body += "\n进度是引擎里的累积值，不是百分比——游戏内一天涨一点，具体几天完成看国策本身。"
            if st.get("notice"):
                body += "\n\n" + st["notice"]
            return {"ok": True, "display": body, "hint": "原样转发，别自己换算成百分比。"}

        if act == "pick":
            try:
                got = await asyncio.to_thread(live.focus_pick, player["addr"], name)
            except livegame.LiveError as exc:
                return {"ok": False, "display": exc.message, "hint": exc.hint}
            except Exception as exc:  # noqa: BLE001
                return self._agent_error(exc)
            body = f"选上了：{who}开始研究「{got['name']}」。"
            if got["replaced"]:
                body += f"\n（原来是「{got['replaced']}」，被我换掉了）"
            body += (
                "\n\n这一下跟在国策树里点它一样：时间一走动进度就涨，"
                "满了会自己完成、自己解锁后面的节点。"
                "\n不过引擎下一次校验（前置、政治点、互斥）要是不过，它会自己把这一条丢掉——"
                "你在国策树里点一个灰的节点，也是这个结果。真被丢了下次我一看就知道。"
            )
            return {
                "ok": True, "display": body,
                "hint": "原样转发。用户如果要它快点完成，那是游戏时间的事，不是我能加速的。",
                "meta": {"name": got["name"], "country": who},
            }

        if act == "cancel":
            try:
                got = await asyncio.to_thread(live.focus_cancel, player["addr"])
            except livegame.LiveError as exc:
                return {"ok": False, "display": exc.message, "hint": exc.hint}
            except Exception as exc:  # noqa: BLE001
                return self._agent_error(exc)
            if not got["cancelled"]:
                return {"ok": True, "display": f"{who}本来就没有在研究的国策。", "hint": "原样转发。"}
            return {
                "ok": True,
                "display": f"取消了「{got['cancelled']}」，{who}现在没有在研究的国策了。"
                           "进度也清零了，重新选会从零开始。",
                "hint": "原样转发。",
            }

        # list（默认）
        lines = [f"{who}现在能点的国策（{len(st['choices'])} 个）："]
        for it in st["choices"]:
            mark = "   ← 正在研究" if st["current"] and it["addr"] == st["current"] else ""
            lines.append(f"  [{it['i']}] {it['name'] or '（名字没读到）'}{mark}")
        if st["current"] and st["current_name"]:
            lines.append(f"\n正在研究：{st['current_name']}（进度 {st['progress']}）")
        if st.get("notice"):
            lines.append("")
            lines.append(st["notice"])
        if stale:
            lines.append("\n（注意：我是按一份旧存档认的国，如果认错了告诉我。）")
        return {
            "ok": True,
            "display": "\n".join(lines),
            "hint": (
                "原样转发。要选就把序号或名字给 hoi4_focus 的 name 参数。"
                "这里只列当前可选的前沿节点，跳节点不行。"
            ),
            "meta": {"count": len(st["choices"]), "current": st["current_name"]},
        }

    # ── 接管哪些部队（F7 / F8） ─────────────────────

    async def _player_only(self, country: str = "", guard: bool = True):
        """只认出玩家国，不拉全师列表。

        `_feed()` 里的 `live.units()` 真机要 14 秒，可编制 / 前线 / 附近部队 /
        划线这几个工具根本不用那份列表，却都复用了 _feed，等于每次查询先白等
        十几秒，再把宿主 30 秒看门狗撞穿（18:44 的超时就是这么来的）。

        0.9.7：入口打个时间戳，让后台全服版图扫描主动让路。DLL 端
        `g_cmd_lock` 把每条命令都串行了，扫描不排后的话光排队就能吃满 30 秒。
        """
        self._live().mark_tool(20.0)
        try:
            # 认人也可能撞上一次冷启动的 CCountry 全服扫描；统一在这里收口，
            # 别让任何一个只读工具被宿主 30 秒看门狗砍掉。
            #
            # 0.9.21：12 秒改 24 秒。真机上冷启动那次扫描要 20 秒左右，12 秒
            # 一到就返回"读数据有点慢"，她转头就跟用户说「代理没注进去」
            # 「内存里认不出德国」——用户看到的"注入失败"其实全是这个超时。
            # 24 秒仍比宿主 30 秒看门狗短，留出返回的时间。
            player = await asyncio.wait_for(
                self._locate_player(country), timeout=24.0)
        except asyncio.TimeoutError:
            logs.write(config.data_dir(),
                       "_player_only：认人超过 24 秒，先返回不让宿主砍掉整个调用")
            return None, {"ok": True,
                          "display": "这会儿读游戏数据有点慢（刚开局还在扫全服），"
                                     "先没敢报，等一下再问一次。",
                          "hint": "原样转发。别编一份战况出来。"}
        except livegame.LiveError as exc:
            return None, {"ok": False, "display": exc.message, "hint": exc.hint}
        except Exception as exc:  # noqa: BLE001
            return None, self._agent_error(exc)
        if guard:
            g = self._stale_country_guard(player)
            if g is not None:
                return None, g
        return player, None

    async def _feed(self, country: str = ""):
        """认人 + 拉当前师列表 + 核对接管表。

        返回 (player, units, kept, err)。err 不为 None 就是不能继续。
        """
        self._live().mark_tool(20.0)
        try:
            player = await self._locate_player(country)
        except livegame.LiveError as exc:
            return None, [], {}, {"ok": False, "display": exc.message, "hint": exc.hint}
        except Exception as exc:  # noqa: BLE001
            return None, [], {}, self._agent_error(exc)

        try:
            units = await asyncio.to_thread(self._live().units, player["addr"])
        except Exception as exc:  # noqa: BLE001
            return None, [], {}, self._agent_error(exc)

        kept: dict = {}
        try:
            kept = await asyncio.to_thread(self._squads().refresh, units)
        except Exception as exc:  # noqa: BLE001 核不上不影响主流程
            logs.write(config.data_dir(), f"接管表核对失败：{type(exc).__name__}: {exc}")
        return player, units, kept, None

    @llm_tool(
        name="hoi4_squad",
        description=(
            "管「哪些部队归 YUI 指挥」。"
            "action=list 看现在手里有哪几批；assign 把某个省里的师接管过来；"
            "release 放回去（给 squad_id 放那一批，给 place 放那个省的，都不给就全放）。"
            "**接管只是记账，不动游戏**——之后调兵、划线默认只碰这些部队，"
            "不会一下子把全军拖走。"
            "用户在游戏里按 F7（鼠标指着哪支部队就接管哪支）/ F8（放回去）时，"
            "也会往这张表里写，这里是给他复核和用嘴改的入口。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "assign", "release"]},
                "place": {"type": "string", "description": "assign/release 时用：城市名、州名或省编号"},
                "squad_id": {"type": "integer", "description": "release 时用：放回第几批"},
                "country": {"type": "string", "description": "用户明确说玩哪国时填。平时留空。"},
            },
            "required": ["action"],
        },
    )
    async def hoi4_squad(self, *, action: str = "list", place: str = "",
                         squad_id: int = 0, country: str = ""):
        act = (action or "list").strip().lower()
        svc = self._squads()

        if act == "list":
            data = svc.load()
            lines = squads_mod.Squads.describe(data)
            last = data.get("last") or {}
            if last:
                lines.append("")
                lines.append(f"游戏里最近一次按键：{last.get('text')}")
            lines.append("")
            lines.append("游戏里 F7 把鼠标指着的部队交给我，F8 放回去。"
                         "这两个键是我在游戏进程里读的，不用切窗口、也不用说话。")
            return {
                "ok": True, "display": "\n".join(lines),
                "hint": "原样转发。别说成「我已经控制了」，说成「这几支我记下了」。",
                "meta": {"total": svc.total()},
            }

        player, units, kept, err = await self._feed(country)
        if err is not None:
            return err

        if act == "assign":
            if not (place or "").strip():
                return {
                    "ok": False,
                    "display": "要接管哪个省的部队？说个地名（比如「柏林」），或者让用户在游戏里按 F7。",
                    "hint": "原样转发。",
                }
            try:
                pid, label = self._places().resolve(place)
            except ValueError as exc:
                return {"ok": False, "display": str(exc), "hint": "把候选说出来让用户确认。"}
            group = [u for u in units if int(u["prov"]) == int(pid)]
            if not group:
                return {
                    "ok": False,
                    "display": f"{label}（省 {pid}）里没有你的部队。",
                    "hint": "让用户从 hoi4_army_positions 里挑一个省。",
                }
            try:
                sq = await asyncio.to_thread(
                    svc.add, player["addr"], player["short"], group,
                    f"{player['short']}·{label}·{len(group)} 师")
            except squads_mod.SquadError as exc:
                return {"ok": False, "display": str(exc), "hint": "原样转发。"}
            lines = [f"记下了：{label} 的 {len(group)} 个师归我指挥（第 {sq['id']} 批）。"]
            lines.extend(squads_mod.Squads.describe(svc.load()))
            lines.append("")
            lines.append("这只是记账，游戏里什么都没动。之后我调兵、划线默认只碰这些部队。")
            return {
                "ok": True, "display": "\n".join(lines),
                "hint": "原样转发。别说得像已经接管了游戏控制权。",
                "meta": {"squad": sq["id"], "units": len(group)},
            }

        # release
        try:
            if squad_id:
                got = svc.release(squad_id=int(squad_id))
                what = f"第 {squad_id} 批"
            elif (place or "").strip():
                pid, label = self._places().resolve(place)
                targets = {int(u["addr"]) for u in units if int(u["prov"]) == int(pid)
                           and int(u["addr"]) in set(svc.unit_addrs())}
                if not targets:
                    return {"ok": True, "display": f"{label} 里没有我指挥的部队，没动。",
                            "hint": "原样转发。"}
                got = svc.release(addrs=targets)
                what = label
            else:
                got = svc.release(all_of=True)
                what = "全部"
        except (squads_mod.SquadError, ValueError) as exc:
            return {"ok": False, "display": str(exc), "hint": "原样转发。"}

        lines = [f"放回去了 {got['released']} 个师（{what}），我手上还剩 {got['left']} 个。"]
        return {
            "ok": True, "display": "\n".join(lines),
            "hint": "原样转发。",
            "meta": {"released": got["released"], "left": got["left"]},
        }

    @plugin_entry(
        id="hoi4_squad",
        name="YUI 管哪几支部队",
        description=(
            "看/改「哪些部队归 YUI 指挥」。用户说「这几支部队交给你」「你看看你手上有哪些」"
            "「都还给我」时使用。游戏里也能直接按 F7 / F8。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "assign", "release"]},
                "place": {"type": "string", "description": "地名或省编号"},
                "squad_id": {"type": "integer", "description": "放回第几批"},
                "country": {"type": "string", "description": "玩哪国，平时留空"},
            },
            "required": ["action"],
        },
        llm_result_fields=["ok", "display"],
    )
    async def entry_squad(self, action: str = "list", place: str = "",
                          squad_id: int = 0, country: str = "", **_):
        return Ok(await self.hoi4_squad(action=action, place=place,
                                        squad_id=squad_id, country=country))

    # ── 划线（作战计划） ──────────────────────────

    @llm_tool(
        name="hoi4_battle_plan",
        description=(
            "**在游戏里真的改作战前线**，而且不碰鼠标：直接改游戏进程里战区前线的省"
            "份范围，改完调引擎自己的重建函数，跟你在地图上画线效果一样，但不会抢你的前台。"
            "action=list 先看现在有几条线；defence 改某一条（要说 front 序号和 army_at 地方）；"
            "clear 撤掉某一条；不传 front 就是新加一条。"
            "army_at 可以一次说好几个地方，用逗号隔开。"
            "用户说「守住东边那一片」「这条线改成守柏林」「再给我加一条防线」时调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "list 看现状 / defence 改一条 / clear 撤一条（也认中文：列表、防线、删除）",
                },
                "army_at": {"type": "string",
                            "description": "要守的地方：城市名、州名或省编号，好几个用逗号隔开"},
                "front": {"type": "integer",
                          "description": "第几条线；不填或 -1 表示新加一条。list 时不用"},
                "country": {"type": "string", "description": "玩哪国，平时留空"},
            },
            "required": ["action"],
        },
    )
    async def hoi4_battle_plan(self, *, action: str = "defence", army_at: str = "",
                               front: int = -1, country: str = ""):
        """进程内划线：直接改战区前线的省份列表，不碰鼠标。

        action: list 看现状 / defence 改某条线 / clear 撤某条线 / new 新加一条
        army_at: 要守的地方，可以用逗号隔开好几个
        front:   第几条线（-1 表示新加一条）
        """
        action, army_at, country = _s(action), _s(army_at), _s(country)
        act = action.strip().lower()

        player, err = await self._player_only(country, guard=False)
        if err is not None:
            return err

        try:
            bp = await asyncio.wait_for(
                asyncio.to_thread(self._plan, player["addr"]), timeout=20.0)
        except asyncio.TimeoutError:
            return {"ok": True, "display": "这会儿读战区划线数据有点慢，先不报了，等一下再问一次。",
                    "hint": "原样转发。别编一份防线出来。"}
        except battleplan_mod.PlanError as exc:
            return {"ok": False, "display": exc.msg, "hint": exc.hint or "原样转发。"}
        except Exception as exc:  # noqa: BLE001
            return self._agent_error(exc)

        names: dict[int, str] = {}
        try:
            names = self._prov_idx().city_names()
        except Exception:  # noqa: BLE001 名字只是锦上添花，读不到不影响改
            names = {}

        def label(pid: int) -> str:
            nm = names.get(int(pid))
            return f"{nm}（{pid}）" if nm else str(pid)

        # ── 看现状 ──────────────────────────
        if act in ("list", "列表", "看看", "现状"):
            try:
                fr = await asyncio.to_thread(bp.fronts)
                n = await asyncio.to_thread(bp.units_here)
            except battleplan_mod.PlanError as exc:
                return {"ok": False, "display": exc.msg, "hint": exc.hint or ""}
            except Exception as exc:  # noqa: BLE001
                return self._agent_error(exc)
            lines = [f"你这个战区有 {len(fr)} 条前线，{n} 个师归它调度。"]
            for f in fr:
                ps = f["provinces"]
                head = "、".join(label(x) for x in ps[:6])
                more = "" if len(ps) <= 6 else f" 等 {len(ps)} 个省"
                lines.append(f"  [{f['idx']}] {len(ps)} 省：{head or '（空）'}{more}")
            lines.append("")
            lines.append("要改哪条，跟我说序号和地方就行。")
            return {"ok": True, "display": "\n".join(lines),
                    "hint": "原样转发，别加渲染。",
                    "meta": {"fronts": len(fr), "units": n}}

        # ── 撤一条 ──────────────────────────
        if act in ("clear", "delete", "删除", "撤了", "清空"):
            if front < 0:
                return {"ok": False,
                        "display": "要撤哪一条？给我个序号（先说一声现在有几条也行）。",
                        "hint": "先问序号，或者先调 action=list。"}
            try:
                r = await asyncio.to_thread(bp.clear_front, front)
            except battleplan_mod.PlanError as exc:
                return {"ok": False, "display": exc.msg, "hint": exc.hint or ""}
            except Exception as exc:  # noqa: BLE001
                return self._agent_error(exc)
            ok = bool(r.get("ok"))
            return {"ok": ok,
                    "display": (f"第 {front} 条已经撤空了。" if ok
                                else f"写进去了但读回来还有省份：{r.get('now')}"),
                    "hint": "原样转发。", "meta": r}

        # ── 改 / 新加 ───────────────────────
        raw = (army_at or "").strip()
        if not raw:
            return {"ok": False,
                    "display": "要守哪里？给我地名或省编号，好几个可以用逗号隔开。",
                    "hint": "问用户守哪些省。"}

        try:
            places = self._places()
        except Exception as exc:  # noqa: BLE001
            return self._agent_error(exc)

        ids: list[int] = []
        labels: list[str] = []
        for part in re.split(r"[,，、;；/\s]+", raw):
            part = part.strip()
            if not part:
                continue
            try:
                pid, lab = places.resolve(part)
            except ValueError as exc:
                return {"ok": False, "display": str(exc),
                        "hint": "把候选说出来让用户确认。"}
            if int(pid) not in ids:
                ids.append(int(pid))
                labels.append(lab)

        if not ids:
            return {"ok": False, "display": "没解析出省份。", "hint": "问用户换个地名。"}

        try:
            if front < 0:
                r = await asyncio.to_thread(bp.add_front, ids)
            else:
                r = await asyncio.to_thread(bp.set_front, front, ids)
        except battleplan_mod.PlanError as exc:
            return {"ok": False, "display": exc.msg, "hint": exc.hint or ""}
        except Exception as exc:  # noqa: BLE001
            return self._agent_error(exc)

        ok = bool(r.get("ok", True))
        idx = r.get("idx", front)
        lines = []
        if front < 0:
            lines.append(f"新加了一条前线，守：{'、'.join(labels)}。")
            lines.append(f"  第 {idx} 条（对象 {int(r.get('addr', 0)):#x}）。")
        else:
            lines.append(f"第 {idx} 条改成守：{'、'.join(labels)}。")
        if r.get("truncated"):
            lines.append(f"  地方太多了，这条线最多放 {r.get('cap')} 个省，后面的截掉了。")
        if r.get("rebuilt"):
            lines.append("  游戏已经按新范围重算了这条线。")
        else:
            lines.append("  线写进去了，但重算那步没成功——游戏下一拍应该会自己补。")
        now = r.get("now")
        if now:
            lines.append(f"  现在这条线管 {len(now)} 个省。")
            if not ok:
                lines.append(f"  读回来跟写进去的不完全一样：{now[:10]}")
        lines.append("")
        lines.append("这一步没动鼠标，也没动你的存档。")
        return {"ok": ok, "display": "\n".join(lines),
                "hint": "原样转发；别打包票说部队一定守得住——线是范围，打不打得看敌人。",
                "meta": {"action": act, "front": idx, "provinces": ids, "result": r}}

    @plugin_entry(
        id="hoi4_battle_plan",
        name="给钢四划线（防线）",
        description=(
            "改游戏里的战区前线：看现在有几条、把某一条改成守哪些地方、撤掉一条、新加一条。"
            "不抢鼠标，直接改游戏进程。用户说「画条防线」「守住东边」「这条线撤了」时使用。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "description": "list 看现状 / defence 改一条 / clear 撤一条"},
                "army_at": {"type": "string", "description": "要守的地方（地名或省编号，可逗号隔开）"},
                "front": {"type": "integer", "description": "第几条线；-1 表示新加一条"},
                "country": {"type": "string", "description": "玩哪国，平时留空"},
            },
            "required": ["action"],
        },
        llm_result_fields=["ok", "display"],
    )
    async def entry_battle_plan(self, action: str = "list", army_at: str = "",
                                front: int = -1, country: str = "", **_):
        return Ok(await self.hoi4_battle_plan(
            action=action, army_at=army_at, front=front, country=country))

    # ── 自检 ──────────────────────────────────────

    _trace("3.4 battle_plan 注册完成，准备 health llm_tool")

    @llm_tool(
        name="hoi4_health",
        description=(
            "体检：游戏开没开、进程内代理注入没有、代理版本新旧、偏移跟这版游戏配不配。"
            "四步都查，每步给结论和下一步指引，用户不用猜。"
            "用户说「体检」「检测一下」「没反应」「按了没动静」「连上了吗」时调用。"
            "**只读，零风险，游戏没开也能查。**"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
    )
    async def hoi4_health(self):
        st = await asyncio.to_thread(nekoagent.status)
        ver = await asyncio.to_thread(nekoagent.probe_ver) if st["state"] == "ready" \
            else {"ver": "", "build": ""}

        checks: list[dict] = []
        lines: list[str] = []

        def add(name: str, ok: bool, detail: str) -> None:
            checks.append({"name": name, "ok": ok, "detail": detail})
            lines.append(("✓ " if ok else "✗ ") + f"{name}：{detail}")

        if st["state"] == "ready":
            add("游戏进程", True, f"在跑（PID {st['pid']}）")
            old = ver["ver"] in ("", "0.5.0-")
            add("代理注入", not old,
                f"已注入，代理版本 {ver['ver']}（{ver['build']}）"
                if not old else
                "已注入，但游戏里跑的是**旧版代理**（没有多客户端、没有 VER）")
            if not old:
                try:
                    live = self._live()
                    build_ok = await asyncio.to_thread(live._build_ok)
                except Exception:  # noqa: BLE001
                    build_ok = True
                add("偏移匹配", build_ok,
                    "游戏版本跟我这套偏移对得上" if build_ok else
                    "**对不上**（游戏更新过），写操作会自动锁住，只读不受影响")
        elif st["state"] == "no_port":
            add("游戏进程", True, "在跑，但我还没进去")
            add("代理注入", False, "还没注入")
        else:
            add("游戏进程", False, "没找到 hoi4.exe")
            add("代理注入", False, "游戏不在，谈不上")

        all_ok = bool(checks) and all(c["ok"] for c in checks)
        if all_ok:
            lines.append("")
            lines.append("一切就绪。可以直接让我调兵、划线，或按 F7 接管部队。")
        elif st["state"] == "ready":
            lines.append("")
            if ver["ver"] in ("", "0.5.0-"):
                lines.append("游戏里是旧版代理。把 N.E.K.O. **完全退出**再重开，"
                             "游戏也退掉重开——新代理会跟着插件自动注进去。")
        elif st["state"] == "no_port":
            lines.append("")
            lines.append("就差一步：游戏里随便干一件事都行——跟我说「看看前线」，"
                         "我第一次干活时会自动把代理注进去。"
                         "如果刚进游戏，先走到地图界面（读档或开新局之后）再试。")
        else:
            lines.append("")
            lines.append("先开《钢铁雄心4》，进到地图界面（读档或开新局都行），"
                         "然后跟我说一声「体检」，我再查一遍。")

        display = "\n".join(lines)
        return {
            "ok": all_ok,
            "display": display,
            "hint": ("一切正常，原样转发即可。" if all_ok else
                     "原样转发体检结果和下一步指引，别让用户自己猜。"),
            "meta": {"agent": st, "dll": ver, "checks": checks},
        }

    _trace("3.5 health llm_tool 注册完成，准备 live_status")

    @llm_tool(
        name="hoi4_live_status",
        description=(
            "检查「现在能不能真的操作这局游戏」：进程内代理连上没有、认没认出你的国家、"
            "你有多少个师、国策在研什么、版本对不对得上。"
            "用户问「你能动我的部队吗」「为什么点不了国策」「连上游戏了吗」时调用。"
            "**只读，不会改动任何东西。**"
        ),
        parameters={"type": "object", "properties": {
            "country": {"type": "string",
                        "description": "用户明确说了「我玩的是德国」时填国名，用来认玩家国家。平时留空。"},
        }, "required": []},
    )
    async def hoi4_live_status(self, country: str = ""):
        live = self._live()
        try:
            st = await asyncio.to_thread(live.status)
        except Exception as exc:  # noqa: BLE001
            return self._agent_error(exc)

        lines: list[str] = []
        if st.get("state") != "ready":
            lines.append(f"还没连上游戏：{st.get('detail') or st.get('state')}")
            lines.append("")
            lines.append("这一步要开游戏、进到地图界面，我才能进去。")
            return {"ok": False, "display": "\n".join(lines),
                    "hint": "原样转发，把「要做什么」说清楚。", "meta": st}

        lines.append(f"连上了：游戏进程 {st.get('pid')}，模块基址 {st.get('base'):#x}。")
        if not st.get("build_ok"):
            lines.append("")
            lines.append(
                "但**游戏版本跟我这套偏移对不上**，选国策和调兵我先不会做——"
                "硬写会把存档写坏。看局势、读国策列表还能用。"
            )

        hint = self._player_hint()
        units: list[dict] = []
        try:
            player = await self._locate_player(country)
        except Exception as exc:  # noqa: BLE001
            lines.append("")
            lines.append(f"国家没认出来：{getattr(exc, 'message', str(exc))}")
            return {"ok": True, "display": "\n".join(lines),
                    "hint": "原样转发；问用户这局玩的是哪国。", "meta": st}

        lines.append(f"认到的国家：{player['short']}（{player['source']}）")
        if not hint["fresh"] and not player.get("confirmed"):
            lines.append(
                f"  这条是按「{hint['save_date'] or '未知日期'}」那份存档认的，可能不是现在这局，"
                "写操作前我会先跟你确认一次。"
            )

        try:
            units = await asyncio.to_thread(live.units, player["addr"])
            lines.append(f"你的师：{len(units)} 个")
            if units:
                lines.extend(livegame.LiveGame.describe_units(units, 6))
            elif not hint["fresh"] and not player.get("confirmed"):
                lines.append(
                    "  ——这个国家一个师都没有，八成不是你这一局。"
                    "直接说「我玩的是德国」这样，我就按你说的认下来（这一局不再问）。"
                )
        except Exception as exc:  # noqa: BLE001
            lines.append(f"部队没读出来：{exc}")

        try:
            fs = await asyncio.to_thread(live.focus_state, player["addr"])
            if fs["current"]:
                lines.append(f"国策：{fs['current_name']}（进度 {fs['progress']}）")
            else:
                lines.append(f"国策：没在研（现在能选 {len(fs['choices'])} 个）")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"国策没读出来：{exc}")

        lines.append("")
        lines.append("选国策和调兵都能做，都是玩家身份，不走控制台。")
        return {
            "ok": True, "display": "\n".join(lines),
            "hint": ("原样转发。这里面每个数字都是刚读出来的，不是估的。"
                     "如果用户这句话里说了他玩哪国（「我玩的是德国」），"
                     "立刻重新调用本工具并填 country=那国名，把他认下来。"),
            "meta": {"pid": st.get("pid"), "player": player["short"], "units": len(units)},
        }

    # ── 按住说话（游戏内语音直接说给她听） ──────────

    @llm_tool(
        name="hoi4_voice",
        description=(
            "管游戏里的「按住说话」：按住一个键说话，松开后**本地识别**成文字，"
            "直接出现在猫娘**聊天窗**里（不用切出来开聊天框，识别不经过宿主），"
            "她的回答也进聊天窗、声音由她念；说一句「把华沙的师调到柏林」这种调令还能直接动手。"
            "用户说「游戏里怎么跟你说话」「按住说话开一下/关掉」「F7 一按就弹聊天框」"
            "「按空格暂停失效」「识别不准」「语音识别装好了吗」「能不能用嘴调兵」时调用。"
            "action=status 看全状态；on/off 开关；"
            "check 只看本地识别模型和游戏闸门是否就绪；"
            "guard 看游戏闸门（游戏运行时宿主让出全局快捷键）；"
            "fix_hotkeys 走老办法把宿主的 F7/F8 永久挪到 Ctrl 组合键；"
            "devices 列出录音设备。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": ("status / on / off / test / check / guard / fix_hotkeys / devices。test = 在聊天窗里说一句测试话，验证整条链路"),
                },
            },
            "required": ["action"],
        },
    )
    async def hoi4_voice(self, *, action: str = "status"):
        act = (action or "status").strip().lower()
        cfg = self._cfg or config.load_config()
        self._cfg = cfg

        if act in ("on", "enable", "开"):
            cfg["ptt_enabled"] = True
            config.save_config(cfg)
            ptt = self._ensure_ptt(force=True)
            if ptt is None:
                return {"ok": False,
                        "display": "按住说话没起来——配置存下了但线程没启动，看一眼 advisor.log。"}
            return {
                "ok": True,
                "display": (f"按住 {ptt.status()['key']} 说话，松开后**本地识别**成文字，"
                            "直接出现在**聊天窗**里（她那句回答也一样，声音由她自己念）。\n"
                            "这个键在游戏运行时会被插件**独占**（RegisterHotKey）——\n"
                            "游戏根本收不到它，不会再出现「按 F7 说话的同时还给游戏下了道 F7 指令」。\n"
                            "语音调令也开着：说「把华沙的师调到柏林」「在柏林划一条防线」"
                            "「柏林附近有什么部队」这种，认得出来就直接动手，不等模型。\n"
                            "游戏内不再浮字幕条了（ptt_overlay=false）——你开着对话框就能看见双方的话。"),
                "hint": "告诉她：按住说话开好了，游戏里按住那个键说话就行，聊天窗里能看到你说的和她回的。",
            }

        if act in ("test", "测试", "试一下", "试音"):
            # 一条命令验证整条链路：路由起得来 → 聊天窗能不能出气泡 → 声音出不出来
            route = self._ensure_route()
            if not route.start(force=True):
                return {"ok": False,
                        "display": ("游戏路由没接上："
                                    + (route.status().get("route_error") or "原因不明")
                                    + "\n确认 N.E.K.O. 开着（宿主 48911 端口通）。")}
            line = ("语音通道通了。这句是我自己念的，你能在聊天窗里看到，也能听见。"
                    "接下来你按住说话，就照这条路走。")
            try:
                await asyncio.to_thread(route.speak, line)
            except hostroute_mod.RouteError as exc:
                route.end()
                return {"ok": False,
                        "display": f"路由通了，但这句没念出来：{exc.message}"}
            route.end()   # 试完就把输入还回聊天窗
            return {"ok": True,
                    "display": ("已经在**聊天窗**里说了一句测试话——气泡和声音都是她。\n"
                                "看到这条就说明「我说的 / 她说的」都落在对话框里了。"),
                    "hint": "问她：聊天窗里有没有多出一句测试话。"}

        if act in ("check", "检查", "自检"):
            asr_st = (self._asr.status() if self._asr is not None
                      else {"state": "未初始化", "model_ok": False,
                            "model_dir": "（还没建过识别器）"})
            guard_st = (self._guard.status() if self._guard is not None
                        else {"guard_active": False, "note": "闸门线程没起来"})
            lines = ["本地语音识别："]
            lines.append(f"  状态：{asr_st.get('state')}")
            lines.append(f"  模型目录：{asr_st.get('model_dir')}")
            lines.append("  模型文件：" + ("在" if asr_st.get("model_ok") else "**缺**"))
            if asr_st.get("load_seconds"):
                lines.append(f"  上次加载：{asr_st['load_seconds']}s")
            if asr_st.get("error"):
                lines.append(f"  错误：{asr_st['error']}")
            lines.append("")
            lines.append("游戏闸门（游戏运行时让出宿主全局快捷键）：")
            lines.append("  线程：" + ("在跑" if self._guard is not None else "没起"))
            lines.append("  现在：" + ("游戏在跑，宿主热键已让出"
                                       if guard_st.get("guard_active") else "没接管（游戏没开）"))
            lines.append(f"  心跳文件：{guard_st.get('file')}")
            lines.append("")
            lines.append("宿主补丁：")
            patched = False
            try:
                from . import hostpatch
                patched = hostpatch.patched()
            except Exception:  # noqa: BLE001
                patched = None
            lines.append("  hotkey-manager 补丁：" +
                         ("已打" if patched else ("**没打**（闸门不会生效）" if patched is False else "查不到")))
            return {"ok": True, "display": "\n".join(lines),
                    "hint": "原样转发。缺什么就直说缺什么，别含糊。",
                    "meta": {"asr": asr_st, "guard": guard_st, "patched": patched}}

        if act in ("guard", "闸门"):
            st = (self._guard.status() if self._guard is not None
                  else {"guard_active": False, "note": "闸门线程没起来"})
            lines = ["游戏闸门："]
            lines.append("  游戏 PID：" + (str(st.get("game_pid")) if st.get("game_pid") else "（没找到游戏进程）"))
            lines.append("  当前：" + ("接管中，宿主全局快捷键已让出"
                                       if st.get("guard_active") else "没接管"))
            lines.append(f"  心跳：{st.get('file')}（{st.get('file_age_s')}s 前更新）")
            if st.get("error"):
                lines.append(f"  错误：{st['error']}")
            return {"ok": True, "display": "\n".join(lines), "meta": st}

        if act in ("off", "disable", "关"):
            cfg["ptt_enabled"] = False
            config.save_config(cfg)
            if self._ptt is not None:
                self._ptt.stop()
                self._ptt = None
            return {"ok": True, "display": "按住说话已关掉，那个键不再录音了。"}

        if act in ("devices", "mic", "麦克风"):
            devs = mic_mod.list_devices()
            if not devs:
                return {"ok": False, "display": "系统里没枚举到录音设备（驱动没装好？）。"}
            cur = int(cfg.get("ptt_mic_device", mic_mod.WAVE_MAPPER))
            lines = ["录音设备："] + [f"  {d}" for d in devs]
            lines.append("")
            lines.append("现在用的是系统默认设备。" if cur < 0 else f"现在指定的是 {cur} 号。")
            return {"ok": True, "display": "\n".join(lines),
                    "meta": {"devices": devs, "current": cur}}

        if act in ("fix_hotkeys", "hotkey", "改键", "冲突"):
            got = nekohost.free_function_keys(
                keys=("F7", "F8"), backup_dir=config.data_dir(),
                log=lambda msg: logs.write(config.data_dir(), msg))
            if not got.get("ok"):
                return {"ok": False, "display": str(got.get("reason") or "没改成。")}
            moved = got.get("moved") or []
            if not moved:
                return {"ok": True, "display": str(got.get("reason") or "不用改。"),
                        "meta": got}
            lines = ["宿主那边占着 F7/F8 的快捷键已经被挪开了："]
            for m in moved:
                lines.append(f"  {m['label']}：{m['from']} → {m['to']}")
            lines.append("")
            lines.append("**要重启一次 N.E.K.O. 才生效**（宿主只在启动时读一次键位）。")
            lines.append("重启后 F7/F8 就归游戏了，按下去不会再弹聊天框。")
            if got.get("backup"):
                lines.append(f"原配置备份在：{got['backup']}")
            return {"ok": True, "display": "\n".join(lines), "meta": got,
                    "hint": "原样转发，特别是「要重启一次」这句不能省。"}

        # status
        st = self._ptt_status()
        lines = []
        if st.get("running"):
            lines.append(f"按住说话：运行中（{st.get('key')}）")
            lines.append(f"  当前状态：{st.get('state')}")
        else:
            lines.append("按住说话：没在跑"
                         + ("（配置里是开着的，但线程没起来，看日志）"
                            if st.get("enabled") else "（没开过）"))
        if st.get("require_game", True):
            key_note = ("（游戏在跑，**已独占**：游戏收不到这个键）" if st.get("key_exclusive")
                        else "（游戏没开 / 还没抢到，按下去仍归游戏）")
        else:
            key_note = "（**已独占**，任何程序里按都归插件）" if st.get("key_exclusive") else ""
        lines.append(f"  键位：{st.get('key')}{key_note}")
        if st.get("key_error"):
            lines.append(f"  抢键失败：{st['key_error']}")
        lines.append(f"  宿主：{st.get('host')}　角色：{st.get('catgirl')}")
        lines.append("  去向：说的话和她的回答都进**聊天窗**（宿主游戏路由）"
                     + ("，已接上" if st.get("route_active") else "，还没接上"))
        if st.get("route_error"):
            lines.append(f"    上次没接上：{st['route_error']}")
        lines.append("  语音调令：" + (
            "开（调兵 / 划线 / 查部队，认得出来就直接动手）"
            if st.get("voice_order") else "关（全部交给对话）"))
        if st.get("overlay_enabled"):
            lines.append("  游戏内浮层：开着（ptt_overlay=true 才开）")
        conf = st.get("conflicts") or []
        if conf:
            lines.append("")
            lines.append("⚠ 按键冲突：宿主把这个键做成了全局快捷键——**游戏运行时插件抢不到它**，")
            for c in conf:
                lines.append(f"  {c['key']} = {c['label']}")
            lines.append("  办法：确认宿主补丁打了（游戏一开宿主会让出），或 fix_hotkeys 挪到 Ctrl 组合键。")
        if st.get("presses"):
            lines.append("")
            lines.append(f"  按过 {st['presses']} 次：成功 {st.get('turns_ok', 0)}、"
                         f"没声音 {st.get('empty_audio', 0)}、失败 {st.get('turns_failed', 0)}")
            if st.get("last_text"):
                lines.append(f"  上一次她听到：{st['last_text']}")
            if st.get("last_reply"):
                lines.append(f"  上一次她回答：{st['last_reply']}")
        if st.get("last_note"):
            lines.append(f"  最近一条提示：{st['last_note']}")
        devs = st.get("mic_devices") or []
        if devs:
            lines.append("")
            lines.append("录音设备：" + "；".join(devs))
        return {"ok": True, "display": "\n".join(lines),
                "hint": "原样转发。状态里的数字是实测的，别改写成「应该没问题」。",
                "meta": st}

    @plugin_entry(
        id="hoi4_voice",
        name="按住说话",
        description=("游戏里按住一个键跟她说话，松开自动变文字发过去，不用开聊天框。"
                     "顺手能查按键冲突、把宿主占着的 F7/F8 挪走。"),
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "description": "status / on / off / fix_hotkeys / devices"},
            },
            "required": ["action"],
        },
        llm_result_fields=["ok", "display"],
    )
    async def entry_voice(self, action: str = "status", **_):
        return Ok(await self.hoi4_voice(action=action))

    _trace("3.9 entry_voice 注册完成")

    # ══════════════ 世界资料库面板（static/ 下的引导页） ══════════════
    # 这些是「给人点」的入口，不是给她用的工具：不出现在她的工具列表里，
    # 只有面板前端会调。世界数据本体在 atlas.py，面板逻辑在 panel.py。

    _trace("3.9a ui.context 前")
    try:
        @ui.context(id="hoi4_advisor")
        async def world_context(self, **_):
            """面板首屏要的一整包数据（同步逻辑丢线程池，别卡住事件循环）。"""
            return await asyncio.to_thread(panel_mod.snapshot)
        _trace("3.9b ui.context 注册完成")
    except Exception as _ui_err:  # noqa: BLE001 面板注册失败不能让整个插件起不来
        _trace(f"3.9b ui.context 注册失败: {_ui_err!r}")

    _trace("3.9c1 world_snapshot 装饰前")
    @plugin_entry(
        id="world_snapshot",
        name="资料库状态",
        description="给资料库面板用：当前找到的游戏、启用的 mod、已有的资料库、生成进度。",
        input_schema={
            "type": "object",
            "properties": {
                "game_dir": {"type": "string", "description": "可选：手动指定的游戏目录"},
            },
            "additionalProperties": False,
        },
    )
    async def entry_world_snapshot(self, game_dir: str = "", **_):
        return Ok(await asyncio.to_thread(panel_mod.snapshot, _s(game_dir).strip()))

    _trace("3.9c2 world_preview 装饰前")
    @plugin_entry(
        id="world_preview",
        name="清点候选世界",
        description="给资料库面板用：列出这个游戏目录能生成哪些世界（原版 / 带 mod），各自多少省多少州。",
        input_schema={
            "type": "object",
            "properties": {"game_dir": {"type": "string"}},
            "required": ["game_dir"],
            "additionalProperties": False,
        },
    )
    async def entry_world_preview(self, game_dir: str = "", **_):
        return Ok(await asyncio.to_thread(panel_mod.preview_worlds, _s(game_dir).strip()))

    _trace("3.9c3 world_build 装饰前")
    @plugin_entry(
        id="world_build",
        name="读取世界",
        description="给资料库面板用：开始生成资料库（后台线程跑，这个调用立刻返回）。",
        input_schema={
            "type": "object",
            "properties": {
                "game_dir": {"type": "string"},
                "mod_indexes": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "用前几个启用的 mod（1 开始）；不给就是全部",
                },
                "mod_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "按名字读指定的 mod（从全量清单里挑，不必先在启动器里启用）",
                },
            },
            "required": ["game_dir"],
            "additionalProperties": False,
        },
    )
    async def entry_world_build(self, game_dir: str = "", mod_indexes=None,
                                mod_names=None, **_):
        return Ok(await asyncio.to_thread(
            panel_mod.start_build, _s(game_dir).strip(), mod_indexes,
            [str(x) for x in mod_names] if mod_names else None))

    _trace("3.9c4b startup_status 装饰前")
    @plugin_entry(
        id="startup_status",
        name="启动闸门状态",
        description="给面板用：这局游戏接上没有（选存档 → 注入代理 → 读档 → 核世界）。"
                    "没接上之前活体工具一律拒绝执行。",
        input_schema={"type": "object", "properties": {},
                      "additionalProperties": False},
    )
    async def entry_startup_status(self, **_):
        return Ok(panel_mod.startup_status())

    _trace("3.9c4c startup_saves 装饰前")
    @plugin_entry(
        id="startup_saves",
        name="列存档",
        description="给面板用：列出能读的存档（日期/玩家/版本/大小），供开始页挑选。",
        input_schema={
            "type": "object",
            "properties": {
                "game_dir": {"type": "string",
                             "description": "游戏目录，省略就自动找"},
            },
            "additionalProperties": False,
        },
    )
    async def entry_startup_saves(self, game_dir: str = "", **_):
        return Ok(await asyncio.to_thread(
            panel_mod.startup_saves, _s(game_dir).strip()))

    _trace("3.9c4d startup_run 装饰前")
    @plugin_entry(
        id="startup_run",
        name="开始启动",
        description="给面板用：按选定的存档跑一次完整启动（找游戏 → 注入代理 → "
                    "校验存档 → 核世界）。后台线程跑，用 startup_progress 看进度。",
        input_schema={
            "type": "object",
            "properties": {
                "save_path": {"type": "string", "description": "存档完整路径"},
                "game_dir": {"type": "string", "description": "游戏目录，省略就自动找"},
            },
            "required": ["save_path"],
            "additionalProperties": False,
        },
    )
    async def entry_startup_run(self, save_path: str, game_dir: str = "", **_):
        return Ok(panel_mod.startup_run(_s(save_path).strip(),
                                        _s(game_dir).strip()))

    _trace("3.9c4e startup_progress 装饰前")
    @plugin_entry(
        id="startup_progress",
        name="启动进度",
        description="给面板用：查启动到哪一步了（percent/phase/steps/error）。",
        input_schema={"type": "object", "properties": {},
                      "additionalProperties": False},
    )
    async def entry_startup_progress(self, **_):
        return Ok(panel_mod.startup_progress())

    _trace("3.9c4 world_progress 装饰前")
    @plugin_entry(
        id="world_progress",
        name="资料库生成进度",
        description="给资料库面板用：查后台生成到哪一步了。",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def entry_world_progress(self, **_):
        return Ok(panel_mod.progress())

    _trace("3.9c5 world_switch 装饰前")
    @plugin_entry(
        id="world_switch",
        name="切换资料库",
        description="给资料库面板用：把某个世界设为当前使用的世界。",
        input_schema={
            "type": "object",
            "properties": {"world_id": {"type": "string"}},
            "required": ["world_id"],
            "additionalProperties": False,
        },
    )
    async def entry_world_switch(self, world_id: str = "", **_):
        return Ok(await asyncio.to_thread(panel_mod.switch_world, world_id))

    _trace("3.9c6 world_delete 装饰前")
    @plugin_entry(
        id="world_delete",
        name="删除资料库",
        description="给资料库面板用：删掉一个不再需要的世界。正在用的删不掉。",
        input_schema={
            "type": "object",
            "properties": {"world_id": {"type": "string"}},
            "required": ["world_id"],
            "additionalProperties": False,
        },
    )
    async def entry_world_delete(self, world_id: str = "", **_):
        return Ok(await asyncio.to_thread(panel_mod.delete_world, world_id))

    _trace("3.10 世界资料库面板注册完成")


# 探针：类定义（含全部装饰器注册）跑完了
_trace("6 类定义+装饰器注册完成，模块 import 结束")


def _speakable(text: str, limit: int = 140) -> str:
    """把工具返回的 display 压成"能念出来的一句"：去 markdown、取第一段、截断。

    调兵工具会吐带表格、带星号的长报告，那是给眼睛看的。这里只留第一句，
    播报和聊天窗气泡都用它——不然语音会念到一半就跑了题。
    """
    s = str(text or "").strip()
    if not s:
        return ""
    s = re.sub(r"```[\s\S]*?```", "", s)
    s = re.sub(r"[*_`>#]", "", s)
    lines = [ln.strip() for ln in s.split("\n") if ln.strip()]
    if not lines:
        return ""
    head = lines[0]
    if len(head) > limit:
        cut = max(head.rfind("。", 0, limit), head.rfind("！", 0, limit),
                  head.rfind("？", 0, limit))
        if cut > 40:
            head = head[: cut + 1]
        else:
            cut = head.rfind("，", 0, limit)
            head = (head[: cut + 1] if cut > 40 else head[:limit] + "…")
    return head

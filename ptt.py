"""按住说话（push-to-talk）：游戏里按住一个键说话，松开后本地识别，把它送进聊天窗。

## 0.9.0 又改一次（用户的原话）

> "把这个框调没，输入转到对话里面，我要开着对话框打游戏，
>  就是说，我要在这个里面看到我说的内容和她说的内容，同时再给个语音调兵的功能"

三件事：

1. **游戏内浮层不要了**。0.8.0 是在游戏画面上浮一条字幕，用户实际的用法是
   把猫娘聊天窗开在游戏边上——那字幕就是多余的。`ptt_overlay` 默认关掉，
   说话的去向改成聊天窗。
2. **说的和回的都进聊天窗**。不再自己连 WebSocket，改走宿主的"游戏路由"HTTP：
   `voice-transcript` 让聊天窗自己长出"我：…"气泡，`speak` 让她长出"她：…"
   气泡并把声音交给宿主 TTS。细节见 `hostroute.py` 顶部。
3. **语音调令**。听出来的话先过一遍 `voiceorder.parse`，是"把华沙的师调到柏林"
   这种就直接调插件自己的工具，不等模型；不是就当普通聊天送进对话。

## 0.8.0 的按键独占保留不动

`RegisterHotKey` 系统级独占这一层是对的：按住的时候这个键根本不进系统输入队列，
游戏收不到、也不会变成它自己的指令。**注册不上就不干活**，绝不退回"只偷看"
的 `GetAsyncKeyState` 轮询 —— 宁可没反应，也不能让按键漏进游戏。

## 为什么必须在"游戏运行"时才注册

宿主 Electron 的 `globalShortcut` 也注册着 F7/F8。同一时刻只能有一个赢家：

- 游戏没开：宿主占着（按 F7 弹聊天框，这是原来的正常行为）→ 我们注册会失败，
  所以干脆不注册；
- 游戏开了：`gameguard.py` 让宿主把全局快捷键让出来 → 我们这时候注册才成功。

两边靠 `%APPDATA%/N.E.K.O/neko_game_guard.json` 这个心跳文件对齐，最坏情况
（插件崩了）宿主 15 秒后自己把热键收回去。

## 状态机

```
idle ──按下──> recording ──松开──> transcribing ──识别完──>
    ├─ 是调令 ──> 直接执行 ──> speak 播结果 ──> idle
    └─ 不是 ───> 进对话 ──> 等她的 line ──> speak ──> idle
```

上一轮没结束就再按会**直接拒绝**：同一时刻两轮会在 drain 里串答案。

## 失败一律留痕

注册失败、录音打不开、模型没装、宿主没开、识别出空串、她没回话、路由过期——
每种都写进 `data/advisor.log` 和 `data/voice_log.jsonl`。**绝不静默降级**。
"""

from __future__ import annotations

import ctypes
import json
import threading
import time
from ctypes import wintypes
from pathlib import Path

from . import asr as asr_mod
from . import gameguard, hostroute, mic as mic_mod, nekohost, voiceorder
from .overlay import GameOverlay

__all__ = ["PushToTalk", "VK_NAMES", "parse_vk"]

VK_NAMES = {
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
    "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    "CAPSLOCK": 0x14, "TAB": 0x09, "SPACE": 0x20, "ALT": 0x12,
    "RALT": 0xA5, "LALT": 0xA4, "RCONTROL": 0xA3, "LCONTROL": 0xA2,
    "SCROLLLOCK": 0x91, "PAUSE": 0x13, "INSERT": 0x2D, "HOME": 0x24,
    "`": 0xC0, "TILDE": 0xC0,
}

MAX_HOLD_SECONDS = 60          # 单次最长录多久，超了截断（防止按住忘了松）
MIN_HOLD_SECONDS = 0.30        # 短于这个当误触
SILENCE_PEAK = 60              # 振幅峰值低于这个当没说话

WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000
PM_REMOVE = 0x0001
HOTKEY_ID = 0xB17
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

_uf = ctypes.WinDLL("user32", use_last_error=True)
_uf.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                             ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
_uf.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
_uf.RegisterHotKey.restype = wintypes.BOOL
_uf.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
_uf.UnregisterHotKey.restype = wintypes.BOOL
_uf.GetForegroundWindow.restype = wintypes.HWND


def parse_vk(name: str) -> int:
    key = str(name or "F7").strip().upper()
    return VK_NAMES.get(key, 0x76)


class _State:
    idle = "idle"
    recording = "recording"
    transcribing = "transcribing"
    thinking = "thinking"


class PushToTalk(threading.Thread):
    """独占按键 → 录音 → 本地识别 → 发文字。守护线程，游戏没开就空转。"""

    def __init__(self, data_dir, log=None, vk: int = 0x76, require_game: bool = True,
                 mic_device: int = mic_mod.WAVE_MAPPER, enabled: bool = True,
                 asr: "asr_mod.LocalAsr | None" = None, engine: str = "local",
                 overlay: bool = False, overlay_width: int = 720,
                 overlay_height: int = 108, overlay_margin: int = 190,
                 route: "hostroute.HostRoute | None" = None,
                 order=None) -> None:
        super().__init__(daemon=True, name="hoi4-ptt")
        self._data_dir = Path(data_dir)
        self._log = log or (lambda msg: None)
        self._vk = int(vk)
        self._require_game = bool(require_game)
        self._mic_device = int(mic_device)
        self._enabled = bool(enabled)
        self._asr = asr
        self._engine = (engine or "local").lower()
        # 说话走宿主的"游戏路由"：她说的话、她回的话都落在聊天窗里
        self._route = route
        # 语音调令：text -> {"ok":bool,"say":str} 或 None（不是调令）
        self._order = order
        self._overlay_on = bool(overlay)
        self._stop_evt = threading.Event()
        self._state = _State.idle
        self._last_note = ""
        self._last_note_at = 0.0
        self._uf = _uf
        self._rec: mic_mod.Recorder | None = None
        self._buf = bytearray()
        self._start_error = ""
        self._pid_cache = (0, 0.0)
        self._press_at = 0.0
        self._pressed = False       # 热键这一轮按下还没处理过
        self._hotkey_on = False     # 当前线程是否持有这个键
        self._hotkey_try_at = 0.0
        self._hotkey_error = ""
        self._hotkey_err_logged = ""
        self._never_foreground = False
        self._ov = GameOverlay(log=self._log, enabled=bool(overlay),
                               width=int(overlay_width), height=int(overlay_height),
                               bottom_margin=int(overlay_margin))
        self._stats = {
            "presses": 0, "turns_ok": 0, "turns_failed": 0, "empty_audio": 0,
            "asr_empty": 0, "last_text": "", "last_reply": "",
            "asr_ms": 0.0, "asr_state": "",
        }
        self._lock = threading.Lock()

    # ── 对外 ────────────────────────────────────

    @property
    def state(self) -> str:
        return self._state

    @property
    def hotkey_held(self) -> bool:
        return self._hotkey_on

    def status(self) -> dict:
        if self._asr is not None:
            self._stats["asr_state"] = self._asr.state
        st = {
            "state": self._state,
            "enabled": self._enabled,
            "key": _vk_name(self._vk),
            "engine": self._engine,
            "require_game": self._require_game,
            "mic_device": self._mic_device,
            "host": nekohost.base_url(),
            "catgirl": nekohost.catgirl_name() or "（拿不到）",
            "ws": nekohost.ws_url() or "（拿不到，宿主可能没开）",
            "asr": self._asr.status() if self._asr is not None else None,
            "last_note": self._last_note,
            # 按键是"独占"还是"没接上"，用户一眼要看出来的东西
            "key_exclusive": self._hotkey_on,
            "key_error": self._hotkey_error,
            **self._stats,
        }
        st.update(self._ov.status())
        st["chat_window"] = True          # 说话和回话都走聊天窗，不再有游戏内浮层
        st["voice_order"] = self._order is not None
        if self._route is not None:
            st.update(self._route.status())
        return st

    def set_enabled(self, on: bool) -> None:
        self._enabled = bool(on)
        if not self._enabled:
            self._force_idle()

    def stop(self) -> None:
        self._stop_evt.set()
        self._force_idle()
        self._ov.stop()

    def _force_idle(self) -> None:
        rec = self._rec
        self._rec = None
        self._buf.clear()
        if rec is not None:
            try:
                rec.stop()
            except Exception:  # noqa: BLE001
                pass
        self._state = _State.idle
        self._pressed = False

    # ── 主循环 ──────────────────────────────────

    def run(self) -> None:
        if self._overlay_on:
            self._ov.start()
        try:
            while not self._stop_evt.is_set():
                try:
                    if not self._enabled:
                        self._release_hotkey()
                        self._stop_evt.wait(0.4)
                        continue

                    pid = self._game_pid()
                    if not pid and self._require_game:
                        # 游戏没开：把键还给宿主，按 F7 照旧弹聊天框
                        if self._state == _State.recording:
                            self._end()
                        self._release_hotkey()
                        self._stop_evt.wait(0.4)
                        continue

                    if not self._ensure_hotkey():
                        # 没抢到就一点也别碰：绝不能让按键漏进游戏
                        if self._state == _State.recording:
                            self._end()
                        self._stop_evt.wait(0.2)
                        continue

                    if self._route is not None:
                        self._route.heartbeat()

                    if self._pump_hotkey():
                        self._begin()
                    self._pump_audio()
                    if self._state == _State.recording and not self._key_down():
                        self._end()
                except Exception as exc:  # noqa: BLE001 线程不能死
                    self._note(f"按住说话线程出错：{type(exc).__name__}: {exc}", err=True)
                    self._force_idle()
                    self._stop_evt.wait(0.5)
                    continue
                self._stop_evt.wait(0.02)
        finally:
            self._release_hotkey()

    # ── 按键独占 ────────────────────────────────

    def _ensure_hotkey(self) -> bool:
        """把按键从游戏手里拿过来。成功一次就一直拿着，直到游戏退出。"""
        if self._hotkey_on:
            return True
        now = time.time()
        if now - self._hotkey_try_at < 2.0:
            return False
        self._hotkey_try_at = now

        # 先把线程消息队列建出来，否则 RegisterHotKey 收不到 WM_HOTKEY
        msg = wintypes.MSG()
        self._uf.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)

        ok = bool(self._uf.RegisterHotKey(None, HOTKEY_ID, MOD_NOREPEAT, self._vk))
        if not ok:
            err = ctypes.get_last_error()
            if err == ERROR_HOTKEY_ALREADY_REGISTERED:
                self._hotkey_error = (
                    f"{_vk_name(self._vk)} 已经被别的程序占着（多半是宿主还没让出来）")
            else:
                self._hotkey_error = f"RegisterHotKey 失败，错误码 {err}"
            # 同一个原因只吵一次，但状态里一直挂着
            if self._hotkey_error != self._hotkey_err_logged:
                self._hotkey_err_logged = self._hotkey_error
                self._note(f"语音键没接管成功：{self._hotkey_error}", err=True)
                self._show("error", "语音键没接上", self._hotkey_error, hold=6.0)
            return False

        self._hotkey_on = True
        self._hotkey_error = ""
        self._hotkey_err_logged = ""
        self._note(f"已独占 {_vk_name(self._vk)}：这个键现在只在插件手里，"
                   f"游戏收不到，不会当成指令")
        self._show("idle", f"{_vk_name(self._vk)} 语音就绪",
                      "按住说话，松开自动发给她", hold=4.0)
        return True

    def _release_hotkey(self) -> None:
        """只能在注册它的那个线程里调（UnregisterHotKey 认线程）。"""
        if not self._hotkey_on:
            return
        try:
            self._uf.UnregisterHotKey(None, HOTKEY_ID)
        except Exception:  # noqa: BLE001
            pass
        self._hotkey_on = False
        self._pressed = False
        self._hide()
        self._note(f"{_vk_name(self._vk)} 已还给系统（游戏退出、语音关掉或插件重载时会走这里）")

    def _pump_hotkey(self) -> bool:
        """把线程消息队列里的 WM_HOTKEY 全掏出来。返回"这一轮按下了没有"。"""
        msg = wintypes.MSG()
        hit = False
        while self._uf.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            if int(msg.message) == WM_HOTKEY and int(msg.wParam) == HOTKEY_ID:
                hit = True
        return hit

    def _key_down(self) -> bool:
        """只用来判断"松开了没有"。按下是 WM_HOTKEY 通知的，不靠这个。"""
        try:
            return bool(self._uf.GetAsyncKeyState(self._vk) & 0x8000)
        except Exception:  # noqa: BLE001
            return False

    # ── 录音 ────────────────────────────────────

    def _pump_audio(self) -> None:
        """把录音队列里的块挪到内存缓冲。按住期间每 20ms 抽一次。"""
        rec = self._rec
        if rec is None:
            return
        got = False
        while True:
            chunk = rec.read(0.0)
            if not chunk:
                break
            self._buf.extend(chunk)
            got = True
        if got:
            self._trim()

    def _trim(self) -> None:
        limit = mic_mod.SAMPLE_RATE * 2 * MAX_HOLD_SECONDS
        if len(self._buf) > limit:
            del self._buf[:len(self._buf) - limit]

    # ── 一次按下 ────────────────────────────────

    def _begin(self) -> None:
        if self._state != _State.idle:
            self._note("上一句还在处理，这句先没录（等一下再按）。", err=True)
            self._beep(300, 60)
            return
        self._press_at = time.time()
        self._stats["presses"] += 1
        self._start_error = ""
        self._buf.clear()

        if self._engine == "local" and self._asr is not None and not self._asr.model_present():
            self._note("本地识别模型没装（data/models/paraformer-zh），这轮没录。", err=True)
            self._show("error", "识别模型没装",
                          "把 model.int8.onnx + tokens.txt 放到 data/models/paraformer-zh", hold=7.0)
            self._beep(300, 120)
            return

        rec = mic_mod.Recorder(device=self._mic_device, buffers=16)
        try:
            rec.start()
        except mic_mod.MicError as exc:
            self._note(f"按了{_vk_name(self._vk)}，但开不了麦克风：{exc.message}", err=True)
            self._show("error", "开不了麦克风", exc.message, hold=6.0)
            self._beep(300, 90)
            return
        self._rec = rec
        self._state = _State.recording
        self._beep(880, 40)
        self._show("recording", "正在听", "松开就发给她", pinned=True)
        self._log(f"按住 {_vk_name(self._vk)} 开始说话（引擎 {self._engine}，麦克风 {self._mic_device}）")

    def _end(self) -> None:
        rec = self._rec
        if rec is None and self._state != _State.recording:
            return
        held = max(0.0, time.time() - self._press_at)
        # 收尾前先把队列里剩下的抽干（这时 self._rec 还没清空，_pump_audio 才干活）
        if rec is not None:
            while True:
                chunk = rec.read(0.0)
                if not chunk:
                    break
                self._buf.extend(chunk)
        self._rec = None
        peak = 0
        if rec is not None:
            try:
                peak = int(rec.peak or 0)
            except Exception:  # noqa: BLE001
                peak = 0
            try:
                rec.stop()
            except Exception:  # noqa: BLE001
                pass
        self._trim()
        pcm = bytes(self._buf)
        self._buf.clear()
        self._beep(620, 40)

        if held < MIN_HOLD_SECONDS or peak < SILENCE_PEAK or len(pcm) < mic_mod.SAMPLE_RATE:
            self._stats["empty_audio"] += 1
            self._state = _State.idle
            msg = f"这次没听到声音（按了 {held:.2f}s，峰值 {peak}）"
            self._note(msg, err=True)
            self._show("error", "没听到声音",
                          f"麦克风只录到 {peak}（按了 {held:.1f} 秒）。"
                          "看看是不是没插好、或者选错了录音设备。", hold=5.0)
            self._beep(300, 70)
            return

        self._state = _State.transcribing
        self._show("transcribing", "识别中…", f"录了 {len(pcm) / 2 / mic_mod.SAMPLE_RATE:.1f} 秒",
                      pinned=True)
        threading.Thread(target=self._run_turn, args=(pcm, held, peak),
                         name="hoi4-ptt-turn", daemon=True).start()

    # ── 一轮的后续（识别 + 提问） ────────────────

    def _run_turn(self, pcm: bytes, held: float, peak: int) -> None:
        text = ""
        err = ""
        asr_ms = 0.0
        try:
            if self._engine == "local":
                if self._asr is None:
                    err = "本地识别没初始化。"
                else:
                    t0 = time.time()
                    text = self._asr.transcribe(pcm)
                    asr_ms = (time.time() - t0) * 1000.0
                    self._stats["asr_ms"] = round(asr_ms, 1)
            else:
                # 旧链路（宿主识别）留着以防万一，但它要实时上行，这里只有一段 PCM 直接不管
                err = "宿主识别模式已停用，请在配置里把 ptt_engine 设为 local。"
        except asr_mod.AsrError as exc:
            err = exc.message
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"

        if not text and not err:
            self._stats["asr_empty"] += 1
            self._state = _State.idle
            self._note(f"识别出来是空的（录了 {len(pcm)/2/mic_mod.SAMPLE_RATE:.1f}s，峰值 {peak}）", err=True)
            self._show("error", "没听清", "再说一遍试试（贴麦克风近一点）", hold=5.0)
            self._beep(300, 70)
            self._write_log({
                "at": int(time.time()), "engine": self._engine, "held_s": round(held, 2),
                "peak": peak, "asr_ms": round(asr_ms, 1), "text": "", "reply": "",
                "audio_seconds": 0.0, "error": "识别为空",
            })
            return

        if err:
            self._stats["turns_failed"] += 1
            self._state = _State.idle
            self._note(err, err=True)
            self._show("error", "识别失败了", err, hold=6.0)
            self._beep(300, 90)
            self._write_log({
                "at": int(time.time()), "engine": self._engine, "held_s": round(held, 2),
                "peak": peak, "asr_ms": round(asr_ms, 1), "text": "", "reply": "",
                "audio_seconds": 0.0, "error": err,
            })
            return

        self._stats["last_text"] = text
        self._log(f"本地识别：{text}（{asr_ms:.0f}ms）")
        self._state = _State.thinking
        self._show("thinking", f"我：{text}", "她正在想…", pinned=True)
        self._ask(text, held, peak, asr_ms)

    def _ask(self, text: str, held: float, peak: int, asr_ms: float) -> None:
        """一句话的后续：先看是不是一道调令，不是就走宿主对话窗。"""
        reply = ""
        err = ""
        kind = "chat"

        order = None
        if self._order is not None:
            try:
                order = self._order(text)
            except voiceorder.OrderError as exc:
                order = {"ok": False, "say": exc.message}
            except Exception as exc:  # noqa: BLE001 解析出错不许拖垮语音
                self._log(f"语音调令解析出错（当普通聊天处理）："
                          f"{type(exc).__name__}: {exc}")

        if order is not None:
            # 调令：插件自己动手，不等模型
            kind = "order"
            reply = str(order.get("say") or "").strip()
            # `ask` = 缺一句话（"你玩的是哪国"），不是失败，别记成 error
            if not order.get("ok") and not order.get("ask"):
                err = reply or "这道令没做成"
            if reply and self._route is not None:
                try:
                    self._route.speak(reply)
                except hostroute.RouteError as exc:
                    # 令确实执行了，只是没念出来：算这一轮成功，别把统计搞脏
                    self._note(f"结果没能播出来（{exc.message}）", err=True)
            self._state = _State.idle
        else:
            # 普通聊天：塞进宿主对话窗（她会自动出现"我：…"气泡），取回她的回答
            if self._route is None:
                err = "宿主的游戏路由没接上（插件初始化问题）"
            else:
                try:
                    self._route.push(text)
                    got = self._route.wait_reply()
                    reply = str(got.get("line") or "")
                except hostroute.RouteError as exc:
                    err = exc.message
                except Exception as exc:  # noqa: BLE001
                    err = f"{type(exc).__name__}: {exc}"
                # 说完就把输入还回聊天窗：路由一直挂着会让对话框以为"游戏接管中"，
                # 用户想打字就打不了了。实测 end 之后 speak 照样能播、照样出气泡。
                self._route.end()
                if reply and not err:
                    try:
                        self._route.speak(reply)
                    except hostroute.RouteError as exc:
                        # 文字已经进聊天窗了，只是没念出来——照样算这一轮成功
                        self._note(f"她的回答没能念出来（{exc.message}）", err=True)
            self._state = _State.idle

        if reply and not err:
            self._stats["turns_ok"] += 1
            self._stats["last_reply"] = reply
            self._log(f"我说：{text}｜" +
                      (f"照做了：{reply}" if kind == "order" else f"她说：{reply}"))
            self._show("reply", "她说", reply, hold=8.0)
        elif err:
            self._stats["turns_failed"] += 1
            self._note(f"这轮没成：{err}", err=True)
            self._show("error", "没接上", err, hold=7.0)
        else:
            self._stats["turns_failed"] += 1
            self._note("她没回话（drain 里没拿到 line）", err=True)
            self._show("error", "她没回话", "看日志 data/advisor.log", hold=7.0)

        self._write_log({
            "at": int(time.time()), "engine": self._engine, "held_s": round(held, 2),
            "peak": peak, "asr_ms": round(asr_ms, 1), "text": text, "reply": reply,
            "kind": kind, "error": err,
        })

    # ── 工具 ────────────────────────────────────

    def _show(self, state: str, title: str, body: str = "",
              hold: float = 0.0, pinned: bool = False) -> None:
        """游戏内浮层默认关掉了（用户要"把那个框调没"）。要开：把 ptt_overlay 设成 true。"""
        if not self._overlay_on:
            return
        try:
            self._ov.show(state, title, body, hold=hold, pinned=pinned)
        except Exception:  # noqa: BLE001 浮层出错绝不能影响说话
            pass

    def _hide(self) -> None:
        if not self._overlay_on:
            return
        try:
            self._ov.hide()
        except Exception:  # noqa: BLE001
            pass

    def _game_pid(self) -> int:
        now = time.time()
        pid, at = self._pid_cache
        if now - at > 2.0:
            pid = gameguard._find_pid()
            self._pid_cache = (pid, now)
        return pid

    def _beep(self, freq: int, ms: int) -> None:
        try:
            ctypes.windll.kernel32.Beep(int(freq), int(ms))
        except Exception:  # noqa: BLE001 没声卡就算了
            pass

    def _note(self, text: str, err: bool = False) -> None:
        self._last_note = text
        self._last_note_at = time.time()
        self._log(("按住说话：✗ " if err else "按住说话：") + text)

    def _write_log(self, rec: dict) -> None:
        try:
            path = self._data_dir / "voice_log.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001 日志写不进去不影响说话
            self._log(f"写 voice_log 失败：{exc}")


def _vk_name(vk: int) -> str:
    for name, code in VK_NAMES.items():
        if code == vk:
            return name
    return f"VK_{vk:#x}"

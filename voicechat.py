"""直连宿主的语音 WebSocket：把麦克风音频灌进去，把她的文字和声音拿回来。

## 为什么走宿主自己的 WS，而不是自己找 ASR

猫娘本体 `:48911` 上的 `/ws/<角色名>` 就是她「语音通话」用的那条连接，协议在
`docs/api_websocket-audio-streaming.md` 里写得很细。走它有三个好处：

1. **不用另外准备语音识别**。识别、理解、合成全在宿主后台跑，用的是她平时
   那套配置；插件这边只要能收能发就行，不额外花钱、不额外下模型。
2. **音色和记忆是同一个人**。绕开宿主另找 ASR，等于把她拆成两半。
3. 麦克风租约（MicLease）是宿主自己管的，它会在我们连着的时候把别处的
   抢音断开——按住说话期间不会两边同时录。

## 协议要点（踩过的坑）

- 音频帧不是 base64，是二进制：`NEKO`（4 字节）+ 采样率（uint32 小端）
  + 单声道 PCM16 小端。单帧 **不许超过 120 ms**。
- `start_session` 之后必须等到 `session_started` 才能灌音频，
  `session_preparing` 不算就绪。
- 服务端语音是「一个 JSON 头 + 一个二进制帧」成对发：`audio_chunk` 带上
  `speech_id`，紧跟着的二进制帧才是那段声音。**不能把没配对的二进制帧当成新的一句。**
- 文本走 `gemini_response`（名字是历史遗留，实际是各家模型的流式文本），
  `isNewMessage=false` 的块要往同一句上追加。

## 回复什么时候算说完

文本有 `system / "turn end"`，声音有 `audio_done`。这里以 `audio_done` 为准，
因为没声音的时候（用户把 TTS 关了）它会一直等不到——所以两条路都留了超时。
"""

from __future__ import annotations

import asyncio
import io
import queue
import struct
import threading
import time
import wave

__all__ = ["VoiceTurn", "TextTurn", "VoiceError", "play_pcm", "wav_bytes"]

MAX_AUDIO_MS = 120            # 单帧上限，超了服务端直接丢
TURN_TIMEOUT = 45.0           # 一整轮（说完话到她说出口）最多等这么久
IDLE_TIMEOUT = 10.0           # 说完话后一点动静都没有 → 判定没听清，早点收场
SILENCE_TAIL = 1.5            # 松开键后补送的静音长度（秒），逼服务端判定"说完了"
NEKO_MAGIC = b"NEKO"


class VoiceError(RuntimeError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def wav_bytes(pcm: bytes, rate: int = 48000) -> bytes:
    """PCM16 单声道 → 带头的 WAV。winsound 只认带头的。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


# winsound 的 SND_MEMORY|SND_ASYNC 要求缓冲区在播放期间一直活着，
# 所以这里留一个引用表，别让 GC 提前回收。
_keep_alive: list[bytes] = []
_play_lock = threading.Lock()


def play_pcm(pcm: bytes, rate: int = 48000) -> float:
    """播放一段 PCM16。**同步**，调用方自己放后台线程。

    返回时长（秒）。没声音设备也不抛，只是返回 0——不能因为放不出来
    就把整轮语音判成失败。
    """
    if not pcm:
        return 0.0
    dur = len(pcm) / 2 / float(rate)
    try:
        import winsound

        data = wav_bytes(pcm, rate)
        with _play_lock:
            _keep_alive.append(data)
            if len(_keep_alive) > 8:
                del _keep_alive[:-4]
            try:
                winsound.PlaySound(data, winsound.SND_MEMORY)
            finally:
                pass
        return dur
    except Exception:  # noqa: BLE001 没有声卡/没有 winsound 就当放完了
        return 0.0


class _AudioHeader:
    __slots__ = ("speech_id", "rate", "pcm")

    def __init__(self, speech_id: str) -> None:
        self.speech_id = speech_id
        self.rate = 48000
        self.pcm = bytearray()


class VoiceTurn:
    """一次「按住说话」。生命周期：start() → push()* → finish() → join()。

    每次按下都新开一条 WS。看着浪费，其实是最稳的——宿主那句
    "每开一条新连接就把麦克风租约重置一次" 反而帮我们拿到独占录音权。
    """

    def __init__(self, ws_url: str, log=None, on_partial=None) -> None:
        self._url = ws_url
        self._log = log or (lambda msg: None)
        self._on_partial = on_partial or (lambda text: None)
        self._q: "queue.Queue[bytes]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._finish_evt: asyncio.Event | None = None
        # 松开按键的时刻可能比"连上宿主"还早（点一下就松）。这时候 asyncio 那边
        # 还没建好 Event，光调 finish() 会丢信号，最后卡到整轮超时。
        # 所以先落一个线程级标记，_main 建好 Event 之后自己补一次。
        self._finish_req = threading.Event()
        self._aborted = threading.Event()
        self._ready = threading.Event()
        self._start_error: str = ""
        self._done = threading.Event()
        self._result = {
            "transcript": "", "reply": "", "audio_seconds": 0.0,
            "error": "", "mic_route": "", "frames_sent": 0, "audio_sent": False,
        }
        self._release_now = 0.0

    # ── 对外 ────────────────────────────────────

    @property
    def result(self) -> dict:
        return dict(self._result)

    def start(self, ready_timeout: float = 10.0) -> None:
        self._thread = threading.Thread(target=self._run, name="hoi4-voice",
                                        daemon=True)
        self._thread.start()
        if not self._ready.wait(ready_timeout):
            raise VoiceError("no_session", "宿主没在 10 秒内把语音通道准备好。")
        if self._start_error:
            raise VoiceError("no_session", self._start_error)

    def push(self, pcm: bytes) -> None:
        if pcm:
            self._q.put(pcm)

    def finish(self) -> None:
        """松开按键：不再送音频，但连接留着等她把话说完。"""
        self._finish_req.set()
        if self._loop is not None and self._finish_evt is not None:
            try:
                self._loop.call_soon_threadsafe(self._finish_evt.set)
            except RuntimeError:
                pass

    def join(self, timeout: float = TURN_TIMEOUT) -> dict:
        self._done.wait(timeout)
        return self.result

    def abort(self) -> None:
        """放弃这一轮（用户没说话、或者出事要收场）：立刻断开，不等她回答。"""
        self._aborted.set()
        self._q.put(b"")
        self.finish()

    # ── 线程里跑的 asyncio ───────────────────────

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except VoiceError as exc:
            self._result["error"] = exc.message
            self._ready.set()
        except Exception as exc:  # noqa: BLE001 线程里没人接异常，必须自己落
            self._result["error"] = f"{type(exc).__name__}: {exc}"
            self._log(f"语音通道异常：{self._result['error']}")
            self._ready.set()
        finally:
            self._done.set()

    async def _main(self) -> None:
        import aiohttp

        self._loop = asyncio.get_running_loop()
        self._finish_evt = asyncio.Event()
        if self._finish_req.is_set():
            self._finish_evt.set()
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=8, sock_read=None)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            try:
                ws = await sess.ws_connect(self._url, heartbeat=20, max_msg_size=0)
            except Exception as exc:  # noqa: BLE001
                raise VoiceError(
                    "connect", f"连不上猫娘的语音通道（{self._url}）：{exc}") from None

            try:
                await ws.send_json({
                    "action": "voice_input_control", "event": "lease_sync",
                    "engaged": True, "owner": "core", "hard_muted": False,
                    "focus_suppressed": False, "lease_generation": 1,
                })
                await ws.send_json({"action": "start_session", "input_type": "audio"})
                if not await self._await_ready(ws):
                    self._ready.set()
                    return
                self._ready.set()
                await asyncio.gather(
                    self._sender(ws),
                    self._receiver(ws),
                )
            finally:
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001
                    pass

    async def _await_ready(self, ws) -> bool:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if msg.type != 1:  # 1 = TEXT
                continue
            try:
                d = msg.json()
            except Exception:  # noqa: BLE001
                continue
            t = d.get("type")
            if t == "session_started":
                self._result["mic_route"] = str(d.get("microphone_route") or "")
                self._log(f"语音通道就绪（麦克风路由：{self._result['mic_route']}）")
                return True
            if t == "session_failed":
                self._start_error = "宿主那边语音会话没起来（session_failed）。"
                return False
            if t == "status":
                code = self._status_code(d)
                if code in ("VOICE_INPUT_LEASE_REQUIRED", "VOICE_INPUT_CONTROL_REJECTED"):
                    self._start_error = (
                        "猫娘的麦克风被别人占着（宿主拒绝了这条连接的录音租约）：" + code)
                    return False
        self._start_error = "等了 10 秒也没等到语音会话就绪。"
        return False

    @staticmethod
    def _status_code(d: dict) -> str:
        raw = d.get("message")
        if isinstance(raw, str):
            try:
                import json

                return str(json.loads(raw).get("code") or "")
            except Exception:  # noqa: BLE001
                return raw[:60]
        return ""

    async def _sender(self, ws) -> None:
        """把队列里的 PCM 一帧帧送出去，直到收到"说完了"的标记。

        **松开之后要补一段静音**（`SILENCE_TAIL`）。这是踩出来的：
        只把音频断掉不发，服务端那套实时 VAD 收不到后续帧，就一直在等"这句
        还没说完"，日志里只有一句 `Speech detected` 然后就没了——
        表现成"她好像听见了但一直不说话"。补 1.5 秒静音，它才会判定说完、
        去跑识别和回答。
        """
        rate = 16000
        max_chunk = rate * 2 * MAX_AUDIO_MS // 1000
        try:
            while True:
                if self._aborted.is_set():
                    return
                if self._finish_evt is not None and self._finish_evt.is_set():
                    # 收尾：把剩下的都送完
                    rest = []
                    while True:
                        try:
                            item = self._q.get_nowait()
                        except queue.Empty:
                            break
                        if item:
                            rest.append(item)
                    for chunk in rest:
                        await self._send_audio(ws, chunk, rate, max_chunk)
                    # 再补一段静音，告诉服务端"我说完了"
                    tail = b"\x00\x00" * int(rate * SILENCE_TAIL)
                    for i in range(0, len(tail), 1024):
                        await self._send_audio(ws, tail[i:i + 1024], rate, max_chunk)
                        await asyncio.sleep(0.032)
                    return
                try:
                    item = self._q.get(timeout=0.02)
                except queue.Empty:
                    await asyncio.sleep(0.005)
                    continue
                if not item:
                    return
                await self._send_audio(ws, item, rate, max_chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 送不出去就算了，让接收侧超时收场
            self._log(f"送音频断了：{type(exc).__name__}: {exc}")

    async def _send_audio(self, ws, chunk: bytes, rate: int, max_chunk: int) -> None:
        for i in range(0, len(chunk), max_chunk):
            part = chunk[i:i + max_chunk]
            if len(part) % 2:
                part = part[:-1]
            if not part:
                continue
            await ws.send_bytes(NEKO_MAGIC + struct.pack("<I", rate) + part)
            self._result["frames_sent"] += 1

    async def _receiver(self, ws) -> None:
        """收文字、收声音。以 audio_done 或 turn end 为一轮的终点。

        超时分两道：
        - **静默超时**（说完话 10 秒还一点动静都没有）→ 多半是没听清/没检测到人声，
          早点收场，别让用户以为坏了；
        - **硬超时**（整轮 45 秒）→ 防止 TTS 卡住或模型不吐字时永远挂着。
        收到任何回应都会把硬超时往后顺延，长回答不会被误杀。
        """
        pending: _AudioHeader | None = None
        text = ""
        reply = ""
        finished_at = 0.0
        idle_deadline = 0.0
        hard_deadline = 0.0
        audio_done_seen = False
        turn_end_seen = False

        while True:
            if self._aborted.is_set():
                return
            now = time.monotonic()
            if self._finish_evt is not None and self._finish_evt.is_set():
                if not finished_at:
                    finished_at = now
                    idle_deadline = now + IDLE_TIMEOUT
                    hard_deadline = now + TURN_TIMEOUT
            if finished_at:
                if now > hard_deadline:
                    self._result["error"] = self._result["error"] or "等回复超时了。"
                    return
                if not reply and not audio_done_seen and now > idle_deadline:
                    self._result["error"] = (
                        self._result["error"] or "她没接话——多半是没听清（或者麦克风没声音）。")
                    return
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=0.5)
            except asyncio.TimeoutError:
                if audio_done_seen and turn_end_seen:
                    return
                continue
            if msg.type == 2:  # BINARY
                if pending is None:
                    # 没配对的二进制帧：不能当新的一句（协议里明说了）
                    continue
                pending.pcm += msg.data
                continue
            if msg.type != 1:
                return
            try:
                d = msg.json()
            except Exception:  # noqa: BLE001
                continue
            t = d.get("type")

            if t == "audio_chunk":
                if pending is not None and pending.pcm:
                    self._consume_audio(pending)
                pending = _AudioHeader(str(d.get("speech_id") or ""))
                if finished_at:
                    hard_deadline = max(hard_deadline, time.monotonic() + 20.0)
                continue

            if t in ("user_transcript", "user_activity"):
                piece = _pick_text(d)
                if piece:
                    text = piece
                    self._result["transcript"] = text
                    self._on_partial(text)
                    if finished_at:
                        idle_deadline = time.monotonic() + IDLE_TIMEOUT
                continue

            if t == "gemini_response":
                piece = str(d.get("text") or "")
                if piece:
                    reply += piece
                    self._result["reply"] = reply
                    if finished_at:
                        idle_deadline = time.monotonic() + IDLE_TIMEOUT
                        hard_deadline = max(hard_deadline, time.monotonic() + 20.0)
                continue

            if t == "audio_done":
                if pending is not None and pending.pcm:
                    self._consume_audio(pending)
                pending = None
                audio_done_seen = True
                self._result["audio_sent"] = True
                if turn_end_seen:
                    return
                continue

            if t == "system":
                if str(d.get("data") or "") == "turn end":
                    turn_end_seen = True
                    if audio_done_seen:
                        return
                continue

            if t == "session_ended_by_server":
                return

            if t == "status":
                code = self._status_code(d)
                if code == "VOICE_INPUT_LEASE_RESYNC_REQUIRED":
                    try:
                        await ws.send_json({
                            "action": "voice_input_control", "event": "lease_sync",
                            "engaged": True, "owner": "core", "hard_muted": False,
                            "focus_suppressed": False, "lease_generation": 2,
                        })
                    except Exception:  # noqa: BLE001
                        pass
                elif code and "REJECTED" in code:
                    self._result["error"] = self._result["error"] or f"宿主拒绝了音频：{code}"
                continue

    def _consume_audio(self, header: _AudioHeader) -> None:
        pcm = bytes(header.pcm)
        if not pcm:
            return
        dur = len(pcm) / 2 / float(header.rate)
        self._result["audio_seconds"] = float(self._result["audio_seconds"]) + dur
        threading.Thread(target=play_pcm, args=(pcm, header.rate),
                         name="hoi4-speak", daemon=True).start()


def _pick_text(d: dict) -> str:
    """`user_transcript` 的字段名在几个版本里改过，都认一遍。"""
    for key in ("text", "transcript", "content", "display_text", "textRaw"):
        v = d.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    inner = d.get("transcript")
    if isinstance(inner, dict):
        for key in ("text", "content", "final", "value"):
            v = inner.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


class TextTurn:
    """只发一行文字，收她的回复和 TTS 声音。

    本地识别（`asr.py`）拿到文字之后走这条，**音频完全不经过宿主**——
    宿主那套实时识别是用户明确说"不好"的部分，我们只借它的对话和声音。

    协议（`_p31_ws_text.py` 探出来的）：

        lease_sync → start_session{input_type:"text"} → 等 session_started
        → stream_data{input_type:"text", data:"…", request_id:"…"}
        → gemini_response 累加 → audio_chunk+BINARY 播放 → audio_done / turn end
    """

    def __init__(self, ws_url: str, text: str, log=None, request_id: str = "") -> None:
        self._url = ws_url
        self._text = str(text or "")
        self._log = log or (lambda msg: None)
        self._rid = request_id or f"hoi4-{int(time.time() * 1000)}"
        self._thread: threading.Thread | None = None
        self._done = threading.Event()
        self._ready = threading.Event()
        self._start_error = ""
        self._result = {"reply": "", "audio_seconds": 0.0, "error": "", "sent_text": self._text}

    # ── 对外 ────────────────────────────────────

    @property
    def result(self) -> dict:
        return dict(self._result)

    def start(self, ready_timeout: float = 12.0) -> None:
        self._thread = threading.Thread(target=self._run, name="hoi4-text",
                                        daemon=True)
        self._thread.start()
        if not self._ready.wait(ready_timeout):
            raise VoiceError("no_session", "宿主没在 12 秒内准备好这一轮文字对话。")
        if self._start_error:
            raise VoiceError("no_session", self._start_error)

    def join(self, timeout: float = TURN_TIMEOUT) -> dict:
        self._done.wait(timeout)
        return self.result

    # ── 线程内 ──────────────────────────────────

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except VoiceError as exc:
            self._result["error"] = exc.message
            self._ready.set()
        except Exception as exc:  # noqa: BLE001
            self._result["error"] = f"{type(exc).__name__}: {exc}"
            self._log(f"文字对话异常：{self._result['error']}")
            self._ready.set()
        finally:
            self._done.set()

    async def _main(self) -> None:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=None, sock_connect=8, sock_read=None)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            try:
                ws = await sess.ws_connect(self._url, heartbeat=20, max_msg_size=0)
            except Exception as exc:  # noqa: BLE001
                raise VoiceError("connect", f"连不上猫娘的通道（{self._url}）：{exc}") from None
            try:
                await ws.send_json({
                    "action": "voice_input_control", "event": "lease_sync",
                    "engaged": True, "owner": "core", "hard_muted": False,
                    "focus_suppressed": False, "lease_generation": 1,
                })
                await ws.send_json({"action": "start_session", "input_type": "text"})
                if not await self._await_ready(ws):
                    self._ready.set()
                    return
                await ws.send_json({
                    "action": "stream_data", "input_type": "text",
                    "data": self._text, "request_id": self._rid,
                })
                self._ready.set()
                await self._receiver(ws)
            finally:
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001
                    pass

    async def _await_ready(self, ws) -> bool:
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if msg.type != 1:
                continue
            try:
                d = msg.json()
            except Exception:  # noqa: BLE001
                continue
            t = d.get("type")
            if t == "session_started":
                return True
            if t == "session_failed":
                self._start_error = "宿主那边会话没起来（session_failed）。"
                return False
            if t == "status":
                code = VoiceTurn._status_code(d)
                if code and "REJECTED" in code:
                    self._start_error = f"宿主拒绝了这条连接：{code}"
                    return False
        self._start_error = "等了 12 秒也没等到会话就绪。"
        return False

    async def _receiver(self, ws) -> None:
        pending: _AudioHeader | None = None
        reply = ""
        started = time.monotonic()
        idle_deadline = started + 15.0
        hard_deadline = started + TURN_TIMEOUT
        audio_done_seen = False
        turn_end_seen = False
        # 有些模型的回合不给 "turn end"，只给 audio_done。所以音频发完之后
        # 给 2.5 秒宽限：这期间没有新东西就收场，别死等 45 秒把自己等成超时。
        tail_deadline = 0.0

        while True:
            now = time.monotonic()
            if audio_done_seen and (turn_end_seen or (reply and now > tail_deadline)):
                return
            if now > hard_deadline:
                if reply:
                    return  # 话已经说完了，只是没等到收尾信号，别报错
                self._result["error"] = self._result["error"] or "等回复超时了。"
                return
            if not reply and now > idle_deadline:
                self._result["error"] = self._result["error"] or "她没接话（模型没吐字）。"
                return
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if msg.type == 2:
                if pending is not None:
                    pending.pcm += msg.data
                continue
            if msg.type != 1:
                return
            try:
                d = msg.json()
            except Exception:  # noqa: BLE001
                continue
            t = d.get("type")

            if t == "audio_chunk":
                if pending is not None and pending.pcm:
                    self._consume(pending)
                pending = _AudioHeader(str(d.get("speech_id") or ""))
                hard_deadline = max(hard_deadline, time.monotonic() + 25.0)
                continue
            if t == "gemini_response":
                piece = str(d.get("text") or "")
                if piece:
                    reply += piece
                    self._result["reply"] = reply
                    idle_deadline = time.monotonic() + 15.0
                    hard_deadline = max(hard_deadline, time.monotonic() + 25.0)
                continue
            if t == "audio_done":
                if pending is not None and pending.pcm:
                    self._consume(pending)
                pending = None
                audio_done_seen = True
                tail_deadline = time.monotonic() + 2.5
                continue
            if t == "system":
                if str(d.get("data") or "") == "turn end":
                    turn_end_seen = True
                continue
            if t == "session_ended_by_server":
                return

    def _consume(self, header: _AudioHeader) -> None:
        pcm = bytes(header.pcm)
        if not pcm:
            return
        dur = len(pcm) / 2 / float(header.rate)
        self._result["audio_seconds"] = float(self._result["audio_seconds"]) + dur
        threading.Thread(target=play_pcm, args=(pcm, header.rate),
                         name="hoi4-speak", daemon=True).start()

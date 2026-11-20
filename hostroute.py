"""说话和回话都走宿主的"游戏路由"HTTP 协议 —— 让它们出现在聊天窗里。

## 为什么不再自己连 WebSocket（0.8.0 的做法被推翻）

0.8.0 是插件自己连 `ws://127.0.0.1:<port>/ws/YUI` 发 `stream_data{input_type:"text"}`。
实测两个硬伤：

1. **那条连接独占会话**。插件一连上，聊天窗那条连接就被挤掉（WS 里直接收到
   `status: CHARACTER_SWITCHING_TERMINAL`）。用户开着对话框打游戏，会变成
   反复断线重连。
2. **后端只把 `gemini_response` 回给发起请求的那条连接**。实测开一个旁观连接，
   自己发文字 → 旁观者一条都收不到。所以聊天窗里只有"她说的"，没有"我说的"。

## 游戏路由这套 HTTP 协议正好是给这个场景准备的

宿主自己就有一条"外部游戏接管主输入"的链路（`/api/game/{game_type}/route/*`）：

```
route/start             声明游戏接管（15 秒不 heartbeat 就自动清理）
route/heartbeat         保活
route/voice-transcript  把一段"独立 STT 出来的最终文本"当成用户说的话塞进对话
                        → 后端广播 user_transcript
                        → 前端 app-websocket.js 收到它直接 appendMessage(text,'user')
                        → 聊天窗出现"我：…"气泡（不用我们改前端一个字）
route/drain             取后端产出的结果，里面 game_llm_result.result.line 就是她的回答
game/{type}/speak       把这条 line 交给宿主 TTS
                        → 后端广播 gemini_response + audio_chunk
                        → 聊天窗出现"她：…"气泡，声音由宿主自己播（音色就是她）
```

实测：`voice-transcript` 之后旁观连接收到 `user_transcript`；`speak` 之后收到
`gemini_response` 和整段音频。所以插件既不用碰音频，也不用碰 WS。

## 一个坑

`route/start` 之后 **15 秒没有 heartbeat 就会被判过期**，过期后
`voice-transcript` 直接回 `{"ok":false,"reason":"game_route_inactive"}`。
所以主循环里每 6 秒续一次，比过期时间短一半。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

from . import nekohost

__all__ = ["HostRoute", "RouteError"]

GAME_TYPE = "hoi4"
HEARTBEAT_EVERY = 6.0        # 秒；后端 15 秒过期，留一倍余量
REPLY_TIMEOUT = 30.0         # 等 drain 出结果最多等多久
DRAIN_INTERVAL = 0.30


class RouteError(Exception):
    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class HostRoute:
    """一个很薄的 HTTP 客户端。线程安全：内部一把锁，串行发请求。"""

    def __init__(self, log=None, game_type: str = GAME_TYPE,
                 lanlan: str = "") -> None:
        self._log = log or (lambda msg: None)
        self._game_type = game_type
        self._lanlan = lanlan
        self._lock = threading.Lock()
        self._active = False
        self._last_hb = 0.0
        self._last_error = ""
        self._turns = 0
        self._session_id = ""

    # ── 基础 ────────────────────────────────────

    def status(self) -> dict:
        return {
            "route_active": self._active,
            "route_game": self._game_type,
            "route_turns": self._turns,
            "route_error": self._last_error,
        }

    def _post(self, path: str, body: dict, timeout: float = 20.0) -> dict:
        base = nekohost.base_url()
        if not base:
            raise RouteError("找不到猫娘宿主（N.E.K.O. 没开？）",
                             "先开 N.E.K.O. 再打游戏")
        url = base + path
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                pass
            raise RouteError(f"宿主回了 {exc.code}：{detail or exc.reason}",
                             "看宿主日志；接口可能跟着版本变了") from None
        except urllib.error.URLError as exc:
            raise RouteError(f"连不上宿主：{exc.reason}",
                             "确认 N.E.K.O. 开着、端口没变") from None
        except Exception as exc:  # noqa: BLE001
            raise RouteError(f"{type(exc).__name__}: {exc}") from None
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            raise RouteError(f"宿主返回的不是 JSON：{raw[:120]}") from None

    def _name(self) -> str:
        if not self._lanlan:
            self._lanlan = nekohost.catgirl_name() or ""
        return self._lanlan

    # ── 路由 ────────────────────────────────────

    def start(self, force: bool = False) -> bool:
        with self._lock:
            if self._active and not force:
                return True
            try:
                got = self._post(f"/api/game/{self._game_type}/route/start",
                                 {"lanlan_name": self._name()})
            except RouteError as exc:
                self._active = False
                self._last_error = exc.message
                self._log(f"游戏路由起不来：{exc.message}")
                return False
            if not got.get("ok"):
                self._active = False
                self._last_error = f"route/start 没成功：{json.dumps(got, ensure_ascii=False)[:160]}"
                self._log(self._last_error)
                return False
            self._active = True
            self._last_hb = time.time()
            self._last_error = ""
            state = got.get("state") or {}
            # chat 端点的 session_id 必须跟这个一致。0.9.4/0.9.5 自造
            # "hoi4_<时间戳>"，后端一律判 route_not_active（真机 19:09 复现）。
            self._session_id = str(state.get("session_id") or "")
            self._log(f"游戏路由已接上（{self._game_type}，"
                      f"输入模式 {state.get('game_input_mode')}，"
                      f"会话 {self._session_id or '没给'}）")
            return True

    def heartbeat(self, force: bool = False) -> None:
        now = time.time()
        if not self._active:
            return
        if not force and now - self._last_hb < HEARTBEAT_EVERY:
            return
        try:
            with self._lock:
                self._post(f"/api/game/{self._game_type}/route/heartbeat", {})
            self._last_hb = now
        except RouteError as exc:
            # 续不上就当掉了，下一轮会重新 start
            self._active = False
            self._last_error = exc.message
            self._log(f"游戏路由续命失败：{exc.message}")

    def end(self) -> None:
        if not self._active:
            return
        try:
            with self._lock:
                self._post(f"/api/game/{self._game_type}/route/end", {})
            self._log("游戏路由已结束（把输入还给聊天窗）")
        except RouteError as exc:
            self._log(f"结束游戏路由没成功：{exc.message}")
        self._active = False

    # ── 一轮 ────────────────────────────────────

    def chat_event(self, event: dict, timeout: float = 30.0,
                   author_prompt: str = "", session_id: str = "") -> str:
        """让宿主按一条"游戏事件"生成她的一句话（不占用用户的输入气泡）。

        用于开局总览：插件把交火数据塞成 event，宿主用她的模型组织成一句汇报。
        需要先 `route/start()`（后端会拒绝 route_not_active），拿到 line 后
        再 `speak(line)` 就是聊天窗里她的气泡 + 声音。

        0.9.4：后端 chat 请求体里除了 `session_id` / `event` 还有 `author_prompt`
        （从宿主后端符号里翻出来的）。带上它才能明确要求"做个汇报"，
        否则模型只会把 event 当普通游戏事件随便接一句。
        0.9.6：`session_id` 必须是 `route/start` 返回的那个（真机是 `"default"`），
        自造时间戳会被判 `route_not_active`；成功响应也**没有 `ok` 字段**，
        只能以 `line` 为准。这两条都修了，19:09 的失败才收口。
        """
        body = {"session_id": session_id or self._route_session_id(),
                "event": event}
        if author_prompt:
            body["author_prompt"] = author_prompt
            # 有些版本的后端只读 event 里的字段，双保险
            if isinstance(event, dict) and "author_prompt" not in event:
                body["event"] = dict(event, author_prompt=author_prompt)
        got = self._post(
            f"/api/game/{self._game_type}/chat", body, timeout=timeout)
        # 成功响应是 {"line": "...", "control": {}, "metrics": ...}，
        # **没有 ok 字段**。旧代码 `not got.get("ok")` 会把成功回答也判成失败。
        line = str(got.get("line") or "").strip()
        if not line:
            reason = got.get("reason") or got.get("skipped") or "没返回 line"
            raise RouteError(f"宿主没生成汇报（{reason}）",
                             "游戏路由没接上时就直接念数据")
        return line

    def _route_session_id(self) -> str:
        """拿当前游戏路由的会话 id；拿不到就用宿主默认的 "default"。"""
        if self._session_id:
            return self._session_id
        base = nekohost.base_url()
        if base:
            try:
                with urllib.request.urlopen(base + "/api/game/route/active",
                                           timeout=5.0) as resp:
                    got = json.loads(resp.read().decode("utf-8", "replace"))
                sid = str(got.get("session_id") or "")
                if sid:
                    self._session_id = sid
                    return sid
            except Exception:  # noqa: BLE001 拿不到就走默认值，不阻塞汇报
                pass
        return "default"

    def push(self, text: str) -> dict:
        """把"我说的这句话"塞进对话。成功 = 聊天窗里已经出现这条用户气泡。"""
        if not text.strip():
            raise RouteError("空文本，没什么可送的")
        if not self._active and not self.start():
            raise RouteError(self._last_error or "游戏路由没接上",
                             "点一下插件里的「开启按住说话」重试")
        try:
            got = self._post(f"/api/game/{self._game_type}/route/voice-transcript",
                             {"text": text}, timeout=25.0)
        except RouteError:
            self._active = False
            raise
        if not got.get("ok") or not got.get("handled"):
            reason = got.get("reason") or json.dumps(got, ensure_ascii=False)[:120]
            self._active = False
            raise RouteError(f"这句没送进对话（{reason}）",
                             "游戏路由过期了，重开一次按住说话")
        self._last_hb = time.time()
        return got

    def wait_reply(self, timeout: float = REPLY_TIMEOUT) -> dict:
        """轮询 drain 直到拿到 her line。返回 {"line": …, "llm_ms": …}。"""
        deadline = time.time() + timeout
        last_err = ""
        while time.time() < deadline:
            try:
                with self._lock:
                    got = self._post(f"/api/game/{self._game_type}/route/drain", {})
            except RouteError as exc:
                last_err = exc.message
                time.sleep(DRAIN_INTERVAL)
                continue
            for out in got.get("outputs") or []:
                if out.get("type") != "game_llm_result":
                    continue
                result = out.get("result") or {}
                line = str(result.get("line") or "").strip()
                if line:
                    return {"line": line,
                            "llm_ms": out.get("llm_elapsed_ms") or 0,
                            "source": (result.get("llm_source") or {}).get("label", "")}
                # 有结果但没话（比如她判定无需回应），继续等下一份
            time.sleep(DRAIN_INTERVAL)
        raise RouteError(last_err or "她这一轮没出声（drain 超时）",
                         "看 data/advisor.log；模型可能没接上")

    def speak(self, line: str, timeout: float = 45.0) -> dict:
        """让宿主把这句话"说"出来：TTS + 聊天窗气泡 + 音频广播，全在宿主里。"""
        if not line.strip():
            raise RouteError("没什么可说的")
        got = self._post(f"/api/game/{self._game_type}/speak",
                         {"line": line}, timeout=timeout)
        if not got.get("ok"):
            raise RouteError(f"宿主没念出来：{json.dumps(got, ensure_ascii=False)[:160]}")
        self._turns += 1
        return got

"""鼠标 / 键盘操作层：把"点一下"变成一条能失败的调用。

这一层只做三件事：
1. 把 DLL 的字符串响应翻译成 bool / 异常，别让上层去解析 "OK click"。
2. 挡住"窗口没找到"这类错误，换成一句人话。
3. **点之前检查坐标安不安全**——掉在 topbar 上就是给用户点开一个面板，
   跟意图完全无关，这种错用户只会觉得"猫娘乱点"。

通道说明：
- `click()` 走 PostMessage，不抢焦点，不动物理光标。优先用它。
- `real_click()` 走 SendInput，会动光标、抢前台。只在 PostMessage 被游戏无视时用。
  两条通道都留着是因为 HOI4 是 SDL：它可能只认其中一条，而这个只有实测才知道。
"""

from __future__ import annotations

import time

__all__ = ["UiDriver", "UiError"]


class UiError(RuntimeError):
    """界面操作失败。message 是能直接说给用户听的话。"""


class UiDriver:
    def __init__(self, bridge, log=None) -> None:
        self._bridge = bridge
        self._log = log or (lambda msg: None)
        self._client: tuple[int, int] | None = None
        self._origin: tuple[int, int] = (0, 0)

    # ── 基本收发 ──────────────────────────────────

    def _send(self, line: str, timeout: float = 3.0) -> str:
        resp = self._bridge.send(line, timeout=timeout)
        if not resp:
            raise UiError("桥接没应答。DLL 掉线了，或者游戏已经关掉。")
        low = resp.lower()
        if low.startswith("err"):
            if "no window" in low:
                raise UiError(
                    "游戏窗口没找到。多半是还在加载、或者停在菜单/主界面——"
                    "要进到地图界面才能操作。"
                )
            if "unknown" in low:
                raise UiError(
                    "桥接不认这条指令，说明游戏里跑的是旧版 DLL。"
                    "重启游戏让它重新加载插件目录里的 neko_hoi4_bridge.dll。"
                )
            raise UiError(f"桥接回了错误：{resp}")
        return resp

    # ── 窗口 ──────────────────────────────────────

    def client(self, refresh: bool = False) -> tuple[int, int]:
        """客户区尺寸。缓存住，窗口尺寸不会中途变。"""
        if self._client is not None and not refresh:
            return self._client
        resp = self._send("CLIENT")
        # OK client=1920x1200 origin=0,0
        try:
            body = resp.split("client=", 1)[1].split()[0]
            w, h = body.split("x")
            self._client = (int(w), int(h))
        except (IndexError, ValueError):
            raise UiError(f"读窗口尺寸失败：{resp}") from None
        if "origin=" in resp:
            try:
                org = resp.split("origin=", 1)[1].split()[0]
                ox, oy = org.split(",")
                self._origin = (int(ox), int(oy))
            except (IndexError, ValueError):
                pass
        return self._client

    def origin(self) -> tuple[int, int]:
        if self._client is None:
            self.client()
        return self._origin

    def foreground(self) -> bool:
        resp = self._send("STATUS")
        return "fg=1" in resp

    def hwnd(self) -> int:
        """游戏窗口句柄。截图要用它。"""
        resp = self._send("STATUS")
        for token in resp.split():
            if token.startswith("hwnd=0x"):
                try:
                    return int(token[7:], 16)
                except ValueError:
                    return 0
        return 0

    def focus(self) -> bool:
        resp = self._send("FOCUS")
        return "fg=1" in resp

    # ── 鼠标 ──────────────────────────────────────

    def move(self, x: int, y: int) -> None:
        self._send(f"MOVE {int(x)} {int(y)}")

    def click(self, x: int, y: int, button: str = "l", n: int = 1) -> None:
        btn = "r" if button.lower().startswith("r") else "l"
        self._send(f"CLICK {int(x)} {int(y)} {btn} {int(n)}")

    def real_click(self, x: int, y: int, button: str = "l", n: int = 1) -> None:
        btn = "r" if button.lower().startswith("r") else "l"
        self._send(f"REAL {int(x)} {int(y)} {btn} {int(n)}", timeout=6.0)

    def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self._send(f"DRAG {int(x1)} {int(y1)} {int(x2)} {int(y2)}", timeout=6.0)

    def wheel(self, x: int, y: int, delta: int = 120, n: int = 1) -> None:
        """滚轮。delta 正 = 放大，负 = 缩小。"""
        self._send(f"WHEEL {int(x)} {int(y)} {int(delta)} {int(n)}", timeout=8.0)

    def probe(self, x: int, y: int) -> dict:
        resp = self._send(f"PROBE {int(x)} {int(y)}")
        out: dict = {"raw": resp}
        for token in resp.split():
            if "=" in token:
                k, v = token.split("=", 1)
                out[k] = v
        return out

    # ── 按键 ──────────────────────────────────────

    def key(self, vk: int, scan: int) -> None:
        self._send(f"KEY {vk:02X} {scan:02X}")

    def modkey(self, vk: int, scan: int, mods: int = 0) -> None:
        """组合键。mods：1=Shift 2=Ctrl 4=Alt。

        走 PostMessage 就够——键盘这条通道游戏两个都收（鼠标才只认 SendInput）。
        作战计划的按钮快捷键全靠它，比找按钮坐标稳。
        """
        self._send(f"MODKEY {int(mods)} {vk:02X} {scan:02X}")

    def cursor(self) -> tuple[int, int] | None:
        """物理屏幕坐标的鼠标位置。取的是游戏进程的 GetCursorPos，
        所以跟地图标定用的是同一套坐标系（都是真实像素）。"""
        resp = self._send("CURSOR")
        if "cursor=" not in resp:
            return None
        try:
            body = resp.split("cursor=", 1)[1].split()[0]
            x, y = body.split(",")
            return int(x), int(y)
        except (IndexError, ValueError):
            return None

    def home(self) -> None:
        """回到首都。相机锚定的唯一可靠手段。"""
        self.key(0x24, 0x47)

    def escape(self) -> None:
        self.key(0x1B, 0x01)

    # ── 组合动作 ──────────────────────────────────

    def click_at(self, x: int, y: int, button: str = "l", channel: str = "post") -> str:
        """按指定通道点一个点。

        `post` 走 PostMessage：不抢焦点、不动物理光标，用户正在自己动鼠标时
        也不会被打断。默认用它。
        `real` 走 SendInput：游戏无视 PostMessage 时的退路，代价是要抢前台。

        选哪条由上层按实测结论定，这一层不猜。
        """
        if channel == "real":
            self.real_click(x, y, button)
            return "real"
        self.click(x, y, button)
        return "post"

    def settle(self, seconds: float = 0.35) -> None:
        time.sleep(seconds)

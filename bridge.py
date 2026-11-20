"""桥接服务层：状态缓存 + 投递一条命令 + 把失败翻译成人话。

三条设计决定，都有踩坑依据：

1. **DLL 只当管道，节奏由这里控制**。v1 的教训是在 DLL 里做长阻塞
   （200 次退格 × 100ms），一条命令卡 20 秒然后超时。v2 每个请求都极短。

2. **控制台开关状态由客户端记**。DLL 是无状态的，它不知道控制台开没开。
   这里记 `_console_open`，并且**只关自己开的**——用户自己按开的控制台不替他关。

3. **失败要分类**。`ERR no window` 和 `ERR unknown` 是两种完全不同的病：
   前者是"还没进地图界面"，后者是"包里的 DLL 版本不对"。
   一律回一句"桥接出错了"，用户只能干瞪眼。
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

from . import bridge_launcher

__all__ = ["BridgeService", "VK_OEM_3"]

# 键码（vk, scan）
VK_OEM_3 = (0xC0, 0x29)   # ` 控制台开关

STATUS_TTL = 3.0


class BridgeService:
    def __init__(self, data_dir: Path, log=None) -> None:
        self._data_dir = Path(data_dir)
        self._log = log or (lambda msg: None)
        self._lock = threading.Lock()
        self._port = 0
        self._console_open = False
        self._status: dict = {
            "state": "unknown",
            "port": 0,
            "pid": 0,
            "detail": "",
            "checked_at": 0.0,
        }

    # ── 状态 ──────────────────────────────────────

    def status(self, refresh: bool = False) -> dict:
        """桥在不在。带 3 秒缓存——工具一次对话可能被问两三遍，别每次扫五个端口。"""
        now = time.time()
        st = dict(self._status)
        if not refresh and st["state"] != "unknown" and now - st["checked_at"] < STATUS_TTL:
            return st

        port = bridge_launcher.find_port(timeout=0.4)
        self._port = port
        if port:
            st = {"state": "ready", "port": port, "pid": 0,
                  "detail": f"端口 {port}", "checked_at": now}
        else:
            st = {"state": "no_bridge", "port": 0, "pid": 0,
                  "detail": "端口上没有桥接在应答。", "checked_at": now}
        st["console_open"] = self._console_open
        self._status = st
        return dict(st)

    def peek(self) -> dict:
        """只看上次探测结果，不发任何网络请求。给"顺手带一句状态"的场合用。"""
        return dict(self._status)

    def connect(self, force: bool = False) -> dict:
        """没有桥就注入一个。可能花两三秒（要等端口起来），别在对话主线程里直接调。"""
        st = self.status(refresh=True)
        if st["state"] == "ready" and not force:
            return st

        result = bridge_launcher.ensure(force=force)
        self._port = int(result.get("port") or 0)
        out = {
            "state": result.get("state", "failed"),
            "port": self._port,
            "pid": int(result.get("pid") or 0),
            "detail": result.get("detail", ""),
            "log_tail": result.get("log_tail", ""),
            "checked_at": time.time(),
            "console_open": self._console_open,
        }
        self._status = out
        if out["state"] != "ready":
            self._log(f"桥接没连上：{out['state']} {out['detail']}")
        else:
            self._log(f"桥接就绪，端口 {out['port']}")
        return dict(out)

    @property
    def port(self) -> int:
        return self._port

    # ── 底层收发 ──────────────────────────────────

    def _talk(self, port: int, line: str, timeout: float = 2.0) -> str:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
                s.settimeout(timeout)
                s.sendall((line + "\n").encode("utf-8"))
                return s.recv(1024).decode("utf-8", errors="replace").strip()
        except OSError:
            return ""

    def _key(self, port: int, key: tuple[int, int]) -> None:
        self._talk(port, f"KEY {key[0]:X} {key[1]:X}")

    def send(self, line: str, timeout: float = 2.0) -> str:
        """发一条原始协议行，给 UI 层（鼠标/滚轮/窗口查询）用。

        没桥接时返回空字符串，不抛异常——上层用一句话报"没连上"就够了，
        这里抛异常会把"没连上"和"这条指令写错了"混成一种错。
        """
        port = self._port
        if not port:
            port = bridge_launcher.find_port(timeout=0.4)
            if port:
                self._port = port
        if not port:
            return ""
        return self._talk(port, line, timeout)

    # ── 投递 ──────────────────────────────────────

    def run(self, cmd: str) -> dict:
        """发一条命令。返回 {"ok", "reason", "detail", "raw"}。

        只保证"投递出去了"，**不保证生效**——游戏不会回报结果。
        """
        if not cmd or "\n" in cmd or "\r" in cmd:
            return {"ok": False, "reason": "bad_cmd", "detail": "命令为空或含换行。", "raw": ""}

        with self._lock:
            port = self._port if self._port else bridge_launcher.find_port(timeout=0.4)
            if not port:
                self._status = {"state": "no_bridge", "port": 0, "pid": 0,
                                "detail": "端口上没有桥接在应答。", "checked_at": time.time()}
                return {
                    "ok": False,
                    "reason": "no_bridge",
                    "detail": "桥接没连上（DLL 没注入，或者游戏关了）。",
                    "raw": "",
                }

            opened_here = False
            try:
                if not self._console_open:
                    self._key(port, VK_OEM_3)
                    time.sleep(0.45)
                    self._console_open = True
                    opened_here = True

                self._talk(port, "BACK 20")      # 清掉命令行里可能残留的字
                time.sleep(0.08)
                resp = self._talk(port, f"TEXT {cmd}")
                if not resp:
                    raise OSError("TEXT 无响应")
                time.sleep(0.12)
                self._talk(port, "ENTER")
            except OSError as exc:
                self._port = 0
                self._console_open = False
                self._status = {"state": "no_bridge", "port": 0, "pid": 0,
                                "detail": f"投递时掉线：{exc}", "checked_at": time.time()}
                return {"ok": False, "reason": "no_bridge",
                        "detail": f"投递时桥接掉线了：{exc}", "raw": ""}
            finally:
                if opened_here:
                    # 只关自己开的：用户本来开着的控制台不替他关
                    self._key(port, VK_OEM_3)
                    time.sleep(0.3)
                    self._console_open = False

            reason, detail = self._classify(resp)
            if reason:
                return {"ok": False, "reason": reason, "detail": detail, "raw": resp}
            return {"ok": True, "reason": "", "detail": "已投递", "raw": resp}

    @staticmethod
    def _classify(resp: str) -> tuple[str, str]:
        """把 DLL 的响应翻译成用户能懂的话。返回 ("", "") 表示没毛病。"""
        text = (resp or "").strip()
        if not text:
            return "protocol", "桥接没回话。"
        if not text.upper().startswith("ERR"):
            return "", ""

        low = text.lower()
        if "no window" in low:
            return (
                "no_window",
                "游戏窗口没找到。多半是还在加载、或者停在菜单/弹窗界面——"
                "要让游戏在地图界面才能收命令。",
            )
        if "unknown" in low:
            return (
                "protocol",
                "桥接不认这条指令，说明游戏里跑的是旧版 DLL（包里这份是 v2 协议）。"
                "重启游戏，让它重新加载插件目录里的 neko_hoi4_bridge.dll。",
            )
        return "command", f"桥接回了错误：{text}"

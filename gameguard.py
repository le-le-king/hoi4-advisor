"""游戏闸门：游戏一开，就把宿主 Electron 的全局快捷键让出去。

## 为什么需要这个模块

宿主的 F7/F8 是 Electron `globalShortcut`（系统级 RegisterHotKey）。它在系统层
把按键吃掉，游戏收不到、插件也收不到；按 F7 还会把 YUI 窗口拉到前台，
之后你按空格暂停游戏就没反应了。

`globalShortcut` 只能在宿主主进程里注销，插件进程管不着。所以做法是：

    插件（本模块）写心跳文件 → 宿主里打的一段小补丁每 700ms 看一眼 → 注销/恢复

补丁在 `<游戏目录>/resources/app.asar` 的 `src/main/hotkey-manager.js` 里，
用 `Q:/neko开发/_probe/patch_host_hotkey_guard.py` 打/还原。

## 心跳协议（重要，防止热键被永久关掉）

- 文件：`%APPDATA%/N.E.K.O/neko_game_guard.json`
- 内容：`{"hotkeys_disabled": true, "pid": 4880, ...}`
- 节奏：游戏在跑时每 2 秒重写一次（顺带刷新 mtime）
- 宿主判定：**mtime 超过 15 秒就把闸门当成过期，自动恢复热键**

所以插件崩了、被禁用、宿主重启到一半，最坏情况热键只会被关 15 秒。
反过来，游戏退出时这里会立刻写 `hotkeys_disabled: false`，宿主 700ms 内恢复。

## 判定标准

用户的原话是"游戏运行的时候优先级在游戏之下"，所以判据是**游戏进程存在**，
不是"游戏在前台"——切到桌面查攻略、看网页时按键照样归游戏，不会突然被猫娘抢走。
"""

from __future__ import annotations

import ctypes
import json
import os
import threading
import time
from ctypes import wintypes
from pathlib import Path

__all__ = ["GameGuard", "guard_path"]

GUARD_NAME = "neko_game_guard.json"
HEARTBEAT_SECONDS = 2.0
EXE_NAME = "hoi4.exe"

_TH32CS_SNAPPROCESS = 0x00000002


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def _find_pid(exe_name: str = EXE_NAME) -> int:
    """枚举进程找游戏。比 tasklist 子进程快两个数量级，2 秒一次也不心疼。"""
    if os.name != "nt":
        return 0
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snap = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if snap in (0, wintypes.HANDLE(-1).value):
            return 0
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            if not k32.Process32FirstW(snap, ctypes.byref(entry)):
                return 0
            target = exe_name.lower()
            while True:
                if str(entry.szExeFile).lower() == target:
                    return int(entry.th32ProcessID)
                if not k32.Process32NextW(snap, ctypes.byref(entry)):
                    return 0
        finally:
            k32.CloseHandle(snap)
    except Exception:  # noqa: BLE001 枚举失败就当游戏没开，不影响别的功能
        return 0


def guard_path() -> Path:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return Path(base) / "N.E.K.O" / GUARD_NAME


class GameGuard(threading.Thread):
    """守护线程：游戏在跑就持续写心跳，退出就立刻放开。"""

    def __init__(self, log=None, enabled: bool = True, exe_name: str = EXE_NAME) -> None:
        super().__init__(daemon=True, name="hoi4-game-guard")
        self._log = log or (lambda msg: None)
        self._enabled = bool(enabled)
        self._exe = exe_name
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._active = False
        self._pid = 0
        self._writes = 0
        self._last_error = ""

    # ── 对外 ────────────────────────────────────

    @property
    def active(self) -> bool:
        return self._active

    def status(self) -> dict:
        p = guard_path()
        try:
            age = round(time.time() - p.stat().st_mtime, 1)
        except OSError:
            age = -1
        return {
            "guard_active": self._active,
            "game_pid": self._pid,
            "exe": self._exe,
            "file": str(p),
            "file_age_s": age,
            "writes": self._writes,
            "error": self._last_error,
        }

    def stop(self) -> None:
        self._stop_evt.set()
        try:
            self._write(False, 0)
        except Exception:  # noqa: BLE001
            pass

    # ── 主循环 ──────────────────────────────────

    def run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                if not self._enabled:
                    self._stop_evt.wait(1.0)
                    continue
                pid = _find_pid(self._exe)
                want = pid > 0
                if want != self._active or want:
                    # 一直写着，保持 mtime 新鲜（就是心跳本身）
                    self._write(want, pid)
                if want != self._active:
                    self._active = want
                    self._pid = pid
                    self._log("游戏闸门：" + (f"接管中（{self._exe} pid={pid}），宿主全局快捷键已让出"
                                              if want else "游戏退出，宿主全局快捷键恢复"))
            except Exception as exc:  # noqa: BLE001 线程不能死
                self._last_error = f"{type(exc).__name__}: {exc}"
                self._log(f"游戏闸门出错了：{self._last_error}")
                self._stop_evt.wait(1.0)
                continue
            self._stop_evt.wait(HEARTBEAT_SECONDS)

    def _write(self, disabled: bool, pid: int) -> None:
        p = guard_path()
        payload = {
            "hotkeys_disabled": bool(disabled),
            "pid": int(pid),
            "source": "hoi4_advisor",
            "updated_at": int(time.time()),
        }
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with self._lock:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, p)
            self._writes += 1

"""把桥接 DLL 送进 hoi4.exe。

为什么必须注入 DLL，而不是模拟按键（别回头改）：用户的红线是"不能抢键盘鼠标，
我自己开着一局在测"。只有进程内 DLL + PostMessage 能同时做到"游戏后台响应"
和"不打扰用户"。SendInput + SetForegroundWindow 那条路已经否掉了。

失败必须分类。这里踩过的最贵的一课是"注入失败"这四个字什么用都没有：
- 游戏没开 → 等用户开
- 游戏以管理员启动、N.E.K.O 不是 → 用户得把两个都提权，说清楚他才会做
- DLL 丢了 → 包没打全
- 注入了但端口没起来 → 去看游戏目录的 neko_bridge.log

所以这个模块不抛裸异常给上层，只返回一个带 state 的 dict。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import socket
import subprocess
import time
from pathlib import Path

__all__ = [
    "DLL_NAME",
    "BridgeInjectError",
    "dll_path",
    "find_pid",
    "image_path",
    "probe",
    "find_port",
    "ensure",
]

DLL_NAME = "neko_hoi4_bridge.dll"
PORT_RANGE = range(7788, 7793)
EXE_NAME = "hoi4.exe"

PROCESS_ALL_ACCESS = 0x1F0FFF
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
PAGE_READWRITE = 0x04

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class BridgeInjectError(RuntimeError):
    """kind: denied（权限）/ dll_missing / failed（其它）。"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def dll_path() -> Path:
    """DLL 跟着插件包走，不引用源码树里的路径。"""
    return Path(__file__).resolve().parent / "dll" / DLL_NAME


# ── 进程 ────────────────────────────────────────────────


def find_pid(exe_name: str = EXE_NAME) -> int:
    """找游戏进程 PID。用 tasklist 而不是 WMI：不引第三方依赖，快。"""
    if os.name != "nt":
        return 0
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="gbk",
            errors="replace",
            timeout=8,
            creationflags=_CREATE_NO_WINDOW,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    for line in out.splitlines():
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == exe_name.lower():
            try:
                return int(parts[1])
            except ValueError:
                continue
    return 0


def image_path(pid: int) -> Path | None:
    """拿游戏 exe 的完整路径。只要 QUERY_LIMITED 权限，不需要管理员。"""
    if os.name != "nt" or pid <= 0:
        return None
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wt.HANDLE
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.QueryFullProcessImageNameW.argtypes = [
        wt.HANDLE,
        wt.DWORD,
        wt.LPWSTR,
        ctypes.POINTER(wt.DWORD),
    ]
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return None
        return Path(buf.value)
    finally:
        k32.CloseHandle(h)


# ── 端口 ────────────────────────────────────────────────


def probe(port: int, timeout: float = 1.0) -> str:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(b"PING\n")
            return s.recv(256).decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def find_port(ports=PORT_RANGE, timeout: float = 0.6) -> int:
    """扫端口。v2 的 PING 回 `OK pong <port>`，**必须是三段**——
    老版 DLL 回的是两段的 `OK pong`，认错了会把老协议当新协议用。"""
    for p in ports:
        resp = probe(p, timeout=timeout)
        if resp.startswith("OK pong") and len(resp.split()) >= 3:
            return p
    return 0


# ── 注入 ────────────────────────────────────────────────


def _kernel32():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wt.HANDLE
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.VirtualAllocEx.restype = ctypes.c_void_p
    k32.VirtualAllocEx.argtypes = [
        wt.HANDLE,
        ctypes.c_void_p,
        ctypes.c_size_t,
        wt.DWORD,
        wt.DWORD,
    ]
    k32.WriteProcessMemory.restype = wt.BOOL
    k32.WriteProcessMemory.argtypes = [
        wt.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    k32.GetModuleHandleW.restype = wt.HMODULE
    k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
    k32.GetProcAddress.restype = ctypes.c_void_p
    k32.GetProcAddress.argtypes = [wt.HMODULE, wt.LPCSTR]
    k32.CreateRemoteThread.restype = wt.HANDLE
    k32.CreateRemoteThread.argtypes = [
        wt.HANDLE,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.c_void_p,
    ]
    k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    k32.GetExitCodeThread.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    k32.CloseHandle.argtypes = [wt.HANDLE]
    return k32


def _inject(pid: int, dll: Path, name: str = "") -> None:
    """把任意 DLL 送进目标进程。name 只用于报错文案（agent 也走这条）。"""
    dll_name = name or DLL_NAME
    if not dll.is_file():
        raise BridgeInjectError("dll_missing", f"插件包里的 {dll_name} 不见了：{dll}")

    k32 = _kernel32()
    data = str(dll).encode("utf-16-le") + b"\x00\x00"

    handle = k32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not handle:
        err = ctypes.get_last_error()
        if err == 5:
            raise BridgeInjectError(
                "denied",
                "打不开游戏进程（拒绝访问）。游戏大概是「以管理员身份运行」启动的，"
                "而 N.E.K.O 是普通权限——两边权限不一样，系统不允许注入。"
                "把 N.E.K.O 也用管理员身份启动一次就行（或者游戏用普通方式启动）。",
            )
        raise BridgeInjectError("failed", f"OpenProcess 失败，错误码 {err}")

    try:
        addr = k32.VirtualAllocEx(handle, None, len(data), MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
        if not addr:
            raise BridgeInjectError("failed", f"VirtualAllocEx 失败 {ctypes.get_last_error()}")

        written = ctypes.c_size_t(0)
        if not k32.WriteProcessMemory(handle, addr, data, len(data), ctypes.byref(written)):
            raise BridgeInjectError("failed", f"WriteProcessMemory 失败 {ctypes.get_last_error()}")

        k32.GetModuleHandleW.restype = wt.HMODULE
        h_kernel = k32.GetModuleHandleW("kernel32.dll")
        load_lib = k32.GetProcAddress(h_kernel, b"LoadLibraryW")
        if not load_lib:
            raise BridgeInjectError("failed", "找不到 LoadLibraryW")

        h_thread = k32.CreateRemoteThread(handle, None, 0, load_lib, addr, 0, None)
        if not h_thread:
            raise BridgeInjectError("failed", f"CreateRemoteThread 失败 {ctypes.get_last_error()}")

        try:
            k32.WaitForSingleObject(h_thread, 5000)
            code = wt.DWORD(0)
            k32.GetExitCodeThread(h_thread, ctypes.byref(code))
            # LoadLibraryW 返回 0 就是加载失败（多半是位数不对或缺依赖）
            if code.value == 0:
                raise BridgeInjectError(
                    "failed", "DLL 送进去了，但游戏拒绝了它（LoadLibrary 返回 0）。"
                )
        finally:
            k32.CloseHandle(h_thread)
    finally:
        k32.CloseHandle(handle)


def _log_tail(pid: int, limit: int = 600) -> str:
    exe = image_path(pid)
    if exe is None:
        return ""
    log = exe.parent / "neko_bridge.log"
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:].strip()


# ── 对外：保证有一条能用的桥 ────────────────────────────


def ensure(dll: Path | None = None, force: bool = False, wait: float = 6.0) -> dict:
    """保证 DLL 在游戏里、端口能连。

    返回 {"state", "port", "pid", "detail", "log_tail"}
    state: ready / no_game / denied / dll_missing / no_port / failed / unsupported
    """
    if os.name != "nt":
        return {"state": "unsupported", "port": 0, "pid": 0,
                "detail": "这套桥接只有 Windows 能用。", "log_tail": ""}

    if not force:
        port = find_port()
        if port:
            return {"state": "ready", "port": port, "pid": 0,
                    "detail": "桥接已经在跑。", "log_tail": ""}

    pid = find_pid()
    if not pid:
        return {"state": "no_game", "port": 0, "pid": 0,
                "detail": "没找到 hoi4.exe——游戏没开，或者刚启动还在加载。", "log_tail": ""}

    target = Path(dll) if dll else dll_path()
    try:
        _inject(pid, target)
    except BridgeInjectError as exc:
        return {"state": exc.kind, "port": 0, "pid": pid,
                "detail": exc.message, "log_tail": _log_tail(pid)}

    deadline = time.time() + max(1.0, wait)
    while time.time() < deadline:
        port = find_port(timeout=0.4)
        if port:
            return {"state": "ready", "port": port, "pid": pid,
                    "detail": "刚注入完成，桥接已就绪。", "log_tail": ""}
        time.sleep(0.4)

    return {
        "state": "no_port",
        "port": 0,
        "pid": pid,
        "detail": "DLL 注入成功了，但端口没起来。多半是端口被上一个实例占着（DLL 卸载不掉，"
        "重启游戏才能清干净），也可能是防火墙拦了本机回环。",
        "log_tail": _log_tail(pid),
    }

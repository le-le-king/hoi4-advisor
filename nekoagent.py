"""进程内代理：把 neko_hoi4_agent.dll 送进 hoi4.exe，然后直接跟游戏内存说话。

为什么非要住在进程里（别回头改成外部 ReadProcessMemory）：

1. 外部读一次内存要一次系统调用，全内存扫一遍四十秒；住进程里是一次 memcpy。
2. **引擎函数只能在游戏主线程调**。在外面调 `0xc12280`（下单）会直接崩。
   代理 DLL 用窗口过程钩子把调用搬到主线程，才敢碰游戏对象。
3. 不抢键盘鼠标。用户的红线是"我自己开着一局在测"，注入 + TCP 回环比
   模拟按键干净得多——按键那条路连后台响应都做不到。

协议（一行一条，ASCII）::

    PING                            -> OK pong <pid> <base>
    RD <addr> <n>                   -> OK <hexbytes>
    WR <addr> <hexbytes>            -> OK <n>
    CALL <addr> <a1> <a2> <a3> <a4> -> OK <rax>     (在主线程执行)
    SCANALL <value>                 -> OK <addr,addr,...>

失败分类沿用 bridge_launcher 那套：游戏没开 / 权限不够 / DLL 丢了 / 注入了没端口。
一律回人话，不抛裸异常给工具层——"连不上"三个字对用户没有任何用。
"""

from __future__ import annotations

import os
import socket
import struct
import time
from pathlib import Path

from . import bridge_launcher

__all__ = [
    "DLL_NAME",
    "PORT",
    "AgentError",
    "Agent",
    "dll_path",
    "find_port",
    "probe",
    "status",
    "ensure",
]

DLL_NAME = "neko_hoi4_agent.dll"
PORT = 7801

# 读的步长：一条命令一个 TCP 行，太大两端都会截断。0x8000 = 32 KB 原始 → 64 KB hex
STEP_READ = 0x8000
# 写的步长：命令行走的是同一个 socket，实测 0x200 最稳
STEP_WRITE = 0x200

CONNECT_TIMEOUT = 20.0


class AgentError(RuntimeError):
    """kind: no_game / denied / dll_missing / no_port / failed / protocol."""

    def __init__(self, kind: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.hint = hint


# ── DLL 定位 ────────────────────────────────────────────


def dll_path() -> Path:
    """DLL 跟着插件包走。

    开发期会有 neko_hoi4_agent_r2..r16 一堆：被注入的 DLL 会被 Windows 锁住、
    覆盖不了，所以每次重建都换个序号。运行时优先用固定名，没有再挑最新的。
    """
    d = Path(__file__).resolve().parent / "dll"
    fixed = d / DLL_NAME
    if fixed.is_file():
        return fixed
    cands = sorted(d.glob("neko_hoi4_agent_r*.dll"), key=lambda p: p.stat().st_mtime)
    if cands:
        return cands[-1]
    return fixed


# ── 端口 ────────────────────────────────────────────────


def probe(port: int = PORT, timeout: float = 0.6) -> str:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(b"PING\n")
            return s.recv(256).decode("ascii", "replace").strip()
    except OSError:
        return ""


def port_open(port: int = PORT, timeout: float = 0.6) -> bool:
    """端口通不通（不做协议握手）。用来区分"代理没起来"和"代理在忙"。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def find_port(retry_timeout: float = 20.0) -> int:
    """代理在不在。PING 必须是 `OK pong <pid> <base>` 三段。

    第一次只等 0.6 秒——平时代理是微秒级回话，快速路径不该慢。
    没回就再等一次（默认 20 秒）：DLL 端 `g_cmd_lock` 把**每条命令**串行化，
    后台版图扫描一条 SCANALL 能占住 7~18 秒，这期间 PING 只能排队。
    只试 0.6 秒就会把"忙"误报成"没起来"——真机上猫娘就是这么告诉用户
    「代理不回应，端口是空的」，而那时代理其实好着。
    """
    resp = probe(PORT, timeout=0.6)
    if resp.startswith("OK pong") and len(resp.split()) >= 4:
        return PORT
    if retry_timeout > 0 and port_open(PORT, timeout=1.0):
        resp = probe(PORT, timeout=retry_timeout)
        if resp.startswith("OK pong") and len(resp.split()) >= 4:
            return PORT
    return 0


def probe_ver(timeout: float = 1.5) -> dict:
    """游戏里跑的代理是什么版本。0.5.1 起代理支持 VER 命令。

    返回 {ver, build}；旧版代理不认识 VER，ver 为 "0.5.0-"（0.5.0 及更早，
    只会原样回声或空）。
    """
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(b"VER\n")
            resp = s.recv(256).decode("ascii", "replace").strip()
    except OSError:
        return {"ver": "", "build": ""}
    parts = resp.split()
    if resp.startswith("OK ") and len(parts) >= 3:
        return {"ver": parts[1], "build": " ".join(parts[2:])}
    return {"ver": "0.5.0-", "build": ""}


def status(refresh: bool = True) -> dict:
    """桥在不在、连的哪个进程、模块基址多少。"""
    if not refresh:
        return {"state": "unknown", "port": 0, "pid": 0, "base": 0, "detail": ""}
    port = find_port()
    if not port:
        pid = bridge_launcher.find_pid()
        if pid and port_open(PORT, timeout=1.0):
            # 端口是通的但 PING 一直没排上：DLL 端命令全局串行，后台扫描
            # 一条 SCANALL 能占住十几秒。**代理活着**，别报成"没起来"。
            return {
                "state": "busy",
                "port": PORT,
                "pid": pid,
                "base": 0,
                "detail": "代理在忙（多半正在扫全服），端口是通的，过几秒再试。",
            }
        return {
            "state": "no_port" if pid else "no_game",
            "port": 0,
            "pid": pid,
            "base": 0,
            "detail": "代理没在应答。" if pid else "没找到 hoi4.exe。",
        }
    resp = probe(port)
    parts = resp.split()
    try:
        pid = int(parts[2])
        base = int(parts[3], 16)
    except (IndexError, ValueError):
        return {"state": "protocol", "port": port, "pid": 0, "base": 0,
                "detail": f"代理回了看不懂的东西：{resp!r}"}
    return {"state": "ready", "port": port, "pid": pid, "base": base, "detail": "已连上。"}


# ── 客户端 ──────────────────────────────────────────────


class Agent:
    """一条命令一个往返，几十微秒。用完记得 close（或者用 with）。"""

    def __init__(self, port: int = PORT, timeout: float = CONNECT_TIMEOUT) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.settimeout(timeout)
        info = self.cmd("PING")
        parts = info.split()
        self.pid = int(parts[2])
        self.base = int(parts[3], 16)

    # ── 生命周期 ──────────────────────────────

    def close(self) -> None:
        # 安全网：断开前摘掉所有断点。BP 一旦留在进程里，下次注入会连环崩。
        for line in (b"BPOFF\n", b"HWBPOFF\n"):
            try:
                self.sock.sendall(line)
                data = b""
                while not data.endswith(b"\n"):
                    chunk = self.sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            except OSError:
                break
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> "Agent":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── 协议 ──────────────────────────────────

    def cmd(self, line: str) -> str:
        try:
            self.sock.sendall((line + "\n").encode())
            data = b""
            while not data.endswith(b"\n"):
                part = self.sock.recv(65536)
                if not part:
                    break
                data += part
        except OSError as exc:
            raise AgentError("failed", f"跟代理说话时断线了：{exc}",
                             "重新连一次；还不行就让用户重启游戏（DLL 卸不掉）。") from None
        text = data.decode("ascii", "replace").strip()
        if text.startswith("ERR"):
            raise AgentError("protocol", f"代理拒绝了这条指令：{text}",
                             "多半是包里的 DLL 跟代码版本对不上，重装插件。")
        return text

    # ── 内存 ──────────────────────────────────

    def read(self, addr: int, n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            chunk = min(STEP_READ, n - len(out))
            resp = self.cmd(f"RD {addr + len(out):#x} {chunk}")
            body = resp[3:].strip()
            try:
                out += bytes.fromhex(body)
            except ValueError:
                raise AgentError("protocol", f"读 {addr + len(out):#x} 时回了非 hex：{body[:40]!r}") from None
            if not body:
                break
        return bytes(out)

    def write(self, addr: int, data: bytes) -> int:
        done = 0
        for i in range(0, len(data), STEP_WRITE):
            part = data[i:i + STEP_WRITE]
            r = self.cmd(f"WR {addr + i:#x} {part.hex()}")
            try:
                done += int(r.split()[1])
            except (IndexError, ValueError):
                raise AgentError("protocol", f"写入回了看不懂的东西：{r!r}") from None
        return done

    def q(self, addr: int) -> int:
        return struct.unpack("<Q", self.read(addr, 8))[0]

    def d(self, addr: int) -> int:
        return struct.unpack("<I", self.read(addr, 4))[0]

    def f(self, addr: int) -> float:
        return struct.unpack("<f", self.read(addr, 4))[0]

    def scan_all(self, value: int) -> list[int]:
        """一次往返扫完整个地址空间（DLL 里做的，别循环 SCAN）。"""
        old = self.sock.gettimeout()
        self.sock.settimeout(180)
        try:
            resp = self.cmd(f"SCANALL {value:#x}")
        finally:
            self.sock.settimeout(old)
        body = resp[3:].strip()
        if body == "none":
            return []
        return [int(x, 16) for x in body.split(",") if x]

    def find_instances(self, vt_rva: int, limit: int = 0) -> list[int]:
        hits = self.scan_all(self.base + vt_rva)
        return hits[:limit] if limit else hits

    # ── 执行 ──────────────────────────────────

    def call(self, fn: int, *args: int) -> int:
        a = list(args) + [0] * (4 - len(args))
        return int(self.cmd(f"CALL {fn:#x} {a[0]:#x} {a[1]:#x} {a[2]:#x} {a[3]:#x}").split()[1], 16)

    def malloc(self, size: int, rva_new: int) -> int:
        """用游戏自己的堆分配（`operator new`）。不要用 VirtualAllocEx：
        引擎内部会拿这些块去做扩容/释放，堆不一致照样崩。"""
        return int(self.cmd(f"CALL {self.base + rva_new:#x} {size:#x} 0 0 0").split()[1], 16)


# ── 对外入口 ────────────────────────────────────────────


def ensure(wait: float = 12.0) -> Agent:
    """有代理就直连，没有就注入一次。失败一律 AgentError，带能照做的建议。"""
    if os.name != "nt":
        raise AgentError("failed", "这套功能只在 Windows 上能用。")

    try:
        return Agent()
    except OSError:
        pass

    pid = bridge_launcher.find_pid()
    if not pid:
        raise AgentError(
            "no_game",
            "没找到 hoi4.exe——游戏没开，或者还在启动加载。",
            "让用户先开游戏、进到地图界面再试。",
        )

    target = dll_path()
    try:
        bridge_launcher._inject(pid, target, name=DLL_NAME)
    except bridge_launcher.BridgeInjectError as exc:
        raise AgentError(exc.kind, exc.message,
                         "按 message 里说的做，别让用户反复重启游戏。") from None

    deadline = time.time() + max(2.0, wait)
    while time.time() < deadline:
        # 这里只要快速判断，别用 find_port 的 20 秒重试——外层本来就在轮询
        if find_port(retry_timeout=2.0):
            try:
                return Agent()
            except OSError:
                pass
        time.sleep(0.4)

    raise AgentError(
        "no_port",
        "DLL 送进去了，但代理端口没起来。多半是这个进程里已经有一份老的代理"
        "占着 7801（DLL 卸载不掉），重启游戏才能清干净。",
        "让用户重启游戏；如果反复如此，把错误原样说出来。",
    )


def signature() -> dict:
    """当前游戏进程的 exe 指纹。写操作的版本闸用它。

    为什么不直接读版本号：exe 的版本资源要从文件解析，还要管多语言；
    **大小 + 修改时间** 对"换了个 exe"这件事够敏感，而且零依赖。
    """
    pid = bridge_launcher.find_pid()
    if not pid:
        return {}
    exe = bridge_launcher.image_path(pid)
    if exe is None or not exe.is_file():
        return {}
    st = exe.stat()
    return {"pid": pid, "path": str(exe), "size": st.st_size, "mtime": int(st.st_mtime)}

"""检查宿主 app.asar 里有没有打"游戏闸门"补丁。

## 为什么要检查

游戏闸门（`gameguard.py`）只是写心跳文件；真正执行
`globalShortcut.unregisterAll()` 的是宿主 `hotkey-manager.js` 里的一小段代码。
那段代码是我们用 `_probe/patch_host_hotkey_guard.py` 打进 app.asar 的。
如果宿主升级（Steam 校验 / 手动更新）把 asar 覆盖回原版，闸门就失效了——
**这时候按键又会弹聊天框**，用户会觉得"插件坏了"。

所以每次查状态都读一遍 asar 里的标记字符串，一眼看出补丁还在不在。

## 为什么不能在这里自动补

宿主正在运行时，`app.asar` 被 Electron 打开着（无共享删除权限），
`os.replace` 会 `PermissionError`。要替换必须**先完全退出 N.E.K.O.**
——那是 `patch_host_hotkey_guard.py` 干的事，得在插件外面做。
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

__all__ = ["asar_path", "host_exe", "patched", "status"]

TARGET = "src/main/hotkey-manager.js"
MARK = b"[game-guard]"


def _self_exe() -> Path | None:
    """当前进程的可执行文件全路径。嵌入式 Python 里 sys.executable 有时是空的，
    但 GetModuleFileNameW(NULL) 一定是宿主那个 exe。"""
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(2048)
        n = ctypes.windll.kernel32.GetModuleFileNameW(None, buf, 2048)
        if n:
            p = Path(buf.value)
            if p.is_file():
                return p
    except Exception:  # noqa: BLE001 非 Windows / 调用失败
        pass
    return None


def host_exe() -> Path | None:
    """宿主可执行文件。插件就跑在它的进程里，`sys.executable` 就在 bin 目录。"""
    cands: list[str] = []
    if sys.executable:
        cands.append(sys.executable)
    selfp = _self_exe()
    if selfp is not None:
        cands.append(str(selfp))
    if sys.argv and sys.argv[0]:
        cands.append(sys.argv[0])
    for cand in cands:
        try:
            p = Path(cand).resolve()
        except OSError:
            continue
        if p.is_file() and p.name.lower().endswith(".exe"):
            return p
    return None


def asar_path() -> Path | None:
    exe = host_exe()
    if exe is None:
        return None
    # <安装目录>/resources/bin/projectneko_server.exe → <安装目录>/resources/app.asar
    p = exe.parent.parent / "app.asar"
    return p if p.is_file() else None


def _read_from_asar(asar: Path, rel_target: str) -> bytes | None:
    """只解头部，按 offset 精确读一个文件。不建临时解包目录。"""
    try:
        with asar.open("rb") as f:
            head = f.read(16)
            if len(head) < 16 or struct.unpack("<I", head[0:4])[0] != 4:
                return None
            json_len = struct.unpack("<I", head[12:16])[0]
            header = json.loads(f.read(json_len).decode("utf-8", "replace"))
            data_start = 16 + json_len
            node = header
            for part in rel_target.split("/"):
                node = (node.get("files") or {}).get(part)
                if node is None:
                    return None
            if "files" in node:
                return None
            off = int(node.get("offset") or 0)
            size = int(node.get("size") or 0)
            f.seek(data_start + off)
            return f.read(size)
    except Exception:  # noqa: BLE001 读不了就是读不了，别炸
        return None


def patched() -> bool | None:
    """True 已打 / False 没打 / None 查不到（找不到 asar 或读失败）。"""
    asar = asar_path()
    if asar is None:
        return None
    body = _read_from_asar(asar, TARGET)
    if body is None:
        return None
    return MARK in body


def status() -> dict:
    asar = asar_path()
    exe = host_exe()
    return {
        "host_exe": str(exe) if exe else "",
        "asar": str(asar) if asar else "",
        "patched": patched(),
        "restart_hint": "改 asar 后必须完全退出 N.E.K.O. 再打开才生效",
    }

"""跟猫娘宿主（N.E.K.O. 本体）打交道的那一层小工具。

只做三件事：找到她在哪个端口、她现在是谁、以及处理**按键冲突**。

## 按键冲突是怎么回事（2026-11-08 查清）

用户按 F7 想叫参谋，结果弹出猫娘聊天框。原因是宿主的 Electron 主进程
在 `src/main/hotkey-manager.js` 里把 F7 注册成了**全局快捷键**
（`focusReactChatInput: 'F7'`，F8 是 `toggleAllUI`）。全局快捷键是系统级的，
只要 N.E.K.O. 在跑，按 F7 就一定是它先收到、游戏根本拿不到——
所以这事"插件里改"根本改不了，只能把宿主的键位挪开。

宿主的键位存在 `%APPDATA%\\N.E.K.O\\hotkey_config.json`，**只在启动时读一次**，
改完要重启 N.E.K.O. 才生效。这里提供 `free_function_keys()`：
备份 → 把占用 F7/F8 的动作挪到 Ctrl+F7 / Ctrl+F8 → 写回。

## 顺带说明为什么不是"改成钩子拦截"
低级键盘钩子（WH_KEYBOARD_LL）确实能在 Electron 之前拿到按键，但那等于
跟宿主抢输入，谁先装谁赢，而且游戏里再叠一层钩子容易掉帧、还容易被反作弊误会。
用户要的是"游戏优先、猫娘别抢"，那就把猫娘的键位让出去，各用各的。
"""

from __future__ import annotations

import json
import os
import shutil
import time
import urllib.parse
import urllib.request
from pathlib import Path

__all__ = [
    "DEFAULT_HOTKEYS",
    "base_url",
    "catgirl_name",
    "ws_url",
    "hotkey_config_path",
    "conflicts",
    "free_function_keys",
]

# 与 hotkey-manager.js 的 DEFAULT_HOTKEYS 保持一致（顺序也照抄）
DEFAULT_HOTKEYS = {
    "toggleVoiceSession": "F2",
    "toggleScreenShare": "F3",
    "triggerScreenshot": "F4",
    "toggleMute": "F5",
    "toggleReactChatWindow": "F6",
    "focusReactChatInput": "F7",
    "selectGalgameChoiceA": "",
    "selectGalgameChoiceB": "",
    "selectGalgameChoiceC": "",
    "toggleAllUI": "F8",
}

HOTKEY_LABELS = {
    "toggleVoiceSession": "开关语音",
    "toggleScreenShare": "共享屏幕",
    "triggerScreenshot": "截图",
    "toggleMute": "静音",
    "toggleReactChatWindow": "开关聊天窗",
    "focusReactChatInput": "聚焦聊天输入框",
    "toggleAllUI": "隐藏/显示全部界面",
}

_CACHE: dict = {"catgirl": "", "at": 0.0}


# ── 端口 / 地址 ─────────────────────────────────────


def _roaming() -> Path:
    return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))


def port_config_path() -> Path:
    return _roaming() / "N.E.K.O" / "port_config.json"


def main_port() -> int:
    """宿主 HTTP 端口。读 Electron 写的那份配置，读不到退回 48911。"""
    try:
        data = json.loads(port_config_path().read_text(encoding="utf-8"))
        return int(data.get("MAIN_SERVER_PORT") or 48911)
    except Exception:  # noqa: BLE001 读不到就按默认来，不要因此炸掉
        return 48911


def base_url() -> str:
    return f"http://127.0.0.1:{main_port()}"


def _get_json(path: str, timeout: float = 3.0):
    req = urllib.request.Request(base_url() + path, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 本地回环
        return json.loads(resp.read().decode("utf-8", "replace"))


def catgirl_name(refresh: bool = False) -> str:
    """当前角色名。WS 路径要它，缓存 60 秒——她换人不会一秒一次。"""
    now = time.time()
    if not refresh and _CACHE["catgirl"] and now - _CACHE["at"] < 60:
        return str(_CACHE["catgirl"])
    name = ""
    try:
        d = _get_json("/api/characters/current_catgirl")
        name = str(d.get("current_catgirl") or "")
    except Exception:  # noqa: BLE001 拿不到就返回空，让上层去报"宿主没开"
        name = ""
    if name:
        _CACHE["catgirl"] = name
        _CACHE["at"] = now
    return name


def ws_url(refresh: bool = False) -> str:
    name = catgirl_name(refresh=refresh)
    if not name:
        return ""
    return f"ws://127.0.0.1:{main_port()}/ws/{urllib.parse.quote(name)}"


def host_alive(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(base_url() + "/health", timeout=timeout) as r:
            return int(getattr(r, "status", 200)) == 200
    except Exception:  # noqa: BLE001
        return False


# ── 按键冲突 ────────────────────────────────────────


def hotkey_config_path() -> Path:
    return _roaming() / "N.E.K.O" / "hotkey_config.json"


def load_hotkeys() -> tuple[dict, bool]:
    """返回 (键位表, 是否启用)。文件不存在就是宿主的默认值。"""
    path = hotkey_config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 没配过 = 用默认
        return dict(DEFAULT_HOTKEYS), True
    enabled = True
    hotkeys = raw
    if isinstance(raw, dict) and isinstance(raw.get("hotkeys"), dict):
        hotkeys = raw["hotkeys"]
        enabled = bool(raw.get("enabled", True))
    merged = dict(DEFAULT_HOTKEYS)
    merged.update({k: v for k, v in (hotkeys or {}).items() if k in DEFAULT_HOTKEYS})
    return merged, enabled


def conflicts(keys=("F7", "F8")) -> list[dict]:
    """现在有哪些动作用了这几个键。"""
    hotkeys, enabled = load_hotkeys()
    out = []
    for action, vk in hotkeys.items():
        if str(vk).upper() in {k.upper() for k in keys}:
            out.append({
                "action": action,
                "label": HOTKEY_LABELS.get(action, action),
                "key": vk,
                "enabled": enabled,
            })
    return out


def free_function_keys(keys=("F7", "F8"), backup_dir: Path | None = None,
                       log=None) -> dict:
    """把占用 keys 的动作挪到 `Ctrl+原键`，让游戏先拿到这几个键。

    - 原文件先备份（默认放到插件 data 目录，不往宿主目录里塞垃圾）
    - 只动冲突的那几个动作，其余原样保留
    - **必须重启 N.E.K.O. 才生效**（宿主只在启动时读一次）
    """
    say = log or (lambda msg: None)
    hotkeys, enabled = load_hotkeys()
    moved: list[dict] = []
    want = {k.upper() for k in keys}
    for action, vk in list(hotkeys.items()):
        if str(vk).upper() in want:
            new = f"Control+{vk}"
            hotkeys[action] = new
            moved.append({
                "action": action,
                "label": HOTKEY_LABELS.get(action, action),
                "from": vk,
                "to": new,
            })

    path = hotkey_config_path()
    backup = ""
    if path.is_file():
        try:
            if backup_dir is not None:
                backup_dir.mkdir(parents=True, exist_ok=True)
                dst = backup_dir / f"host_hotkey_config.{time.strftime('%Y%m%d-%H%M%S')}.json"
                shutil.copy2(path, dst)
                backup = str(dst)
        except Exception as exc:  # noqa: BLE001 备份失败就别写了，免得没法回滚
            return {"ok": False, "reason": f"备份宿主键位文件失败，没敢改：{exc}"}

    if not moved:
        return {"ok": True, "moved": [], "backup": backup,
                "reason": "本来就没有动作占用这几个键，不用改。"}

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"enabled": enabled, "hotkeys": hotkeys}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"写宿主键位文件失败：{exc}", "moved": moved}

    say(f"已把宿主的 {len(moved)} 个快捷键挪到 Ctrl 组合键："
        + "、".join(f"{m['label']} {m['from']}→{m['to']}" for m in moved))
    return {"ok": True, "moved": moved, "backup": backup, "restart_required": True}

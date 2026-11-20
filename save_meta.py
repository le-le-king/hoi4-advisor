"""存档头部指纹：只读 8 KB 就知道"这是哪一局"（0.9.8）。

以前要认一份存档就得把 40～130 MB 整份解析一遍（实测 48.8 MB 要 21.9 秒），
而"哪一局"这件事的答案其实全在文件开头两百字节里：

    HOI4txt
    player="LUX"
    date="1939.10.1.1"
    version="Bolivar v1.14.8.6eb9 (9d66)"
    ...
    game_unique_id="6b7107a9-5e2d-418e-96d0-655961f651f0"
    session=3558

游戏开着、而这局还没存过盘时，目录里那份"最新的"往往是几个月前另一局的档。
拿它当现状报，就是最典型的认错人。所以这里除了读头部，还查两件事：

1. **游戏进程什么时候启动的**（GetProcessTimes）。存档比这次开游戏还早，
   就说明这次开的游戏往前推的进度，这份档根本没记下来。
2. **游戏 exe 是哪个版本**（changelog.txt）。存档 1.14.8、游戏 1.16.9，
   连省/州编号都不是一套，肯定不是同一局。

全程只读文件头和进程信息，不读游戏内存、不调引擎函数、不解析整份存档。
"""
from __future__ import annotations

import ctypes
import os
import re
import time
import zipfile
from ctypes import wintypes
from pathlib import Path
from typing import Any

__all__ = [
    "read_head", "head_meta", "version_num", "date_text",
    "game_process", "game_process_cached", "judge_live", "banner", "clear_cache",
    "disable_binary_saves",
]

HEAD_BYTES = 8192
_HEAD_FIELDS = ("player", "ideology", "date", "difficulty", "version", "tutorial")
_HEAD_CACHE: dict[tuple[str, int, float], dict] = {}
_CACHE_MAX = 256


class _FILETIME(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]


# ── 小工具 ──────────────────────────────────────────────

def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def version_num(text: str) -> str:
    """`Bolivar v1.14.8.6eb9 (9d66)` → `1.14.8.6`。

    老代码用 `version\\s*=\\s*\"?([^\\s\"\\n]+)` 抠，带引号的版本串
    只会拿到 `Bolivar` 这个代号，后面的数字全丢，结果挑错了游戏数据目录。
    """
    m = re.search(r"\d+(?:\.\d+)+", text or "")
    if not m:
        return ""
    # 存档里是 1.14.8.6eb9 这种带 build 号的，主版本只要前三段，
    # 才好和游戏目录 changelog 里的 1.14.8 直接比。
    return ".".join(m.group(0).split(".")[:3])


def date_text(raw: str) -> str:
    parts = str(raw or "").split(".")
    if len(parts) >= 3:
        try:
            return f"{int(parts[0])}年{int(parts[1])}月{int(parts[2])}日"
        except ValueError:
            pass
    return str(raw or "")


def _gap_text(seconds: float) -> str:
    if seconds < 3600:
        return f"{max(1, int(seconds / 60))} 分钟"
    if seconds < 86400 * 2:
        return f"{seconds / 3600:.1f} 小时"
    return f"{seconds / 86400:.0f} 天"


# ── 头部 ────────────────────────────────────────────────

def _fill_binary_head(info: dict, raw: bytes) -> dict:
    """0.9.12: 二进制存档头部用 pdx_binary.head_fields 提取指纹。

    与文本路径产出同构的字段（player/date/version/ideology/difficulty/
    tutorial/game_unique_id/session，date 已解码成 "Y.M.D.H"）。读不出
    关键项时写 error —— 那种档才会触发 watcher 的「改回文本存档」自愈。
    """
    try:
        from .pdx_binary import head_fields
        fields = head_fields(raw)
    except Exception as exc:  # noqa: BLE001
        info["error"] = f"二进制存档头部解析失败：{type(exc).__name__}: {exc}"
        return info
    for key in _HEAD_FIELDS:
        value = fields.get(key, "")
        info[key] = value if isinstance(value, str) else str(value)
    info["game_unique_id"] = str(fields.get("game_unique_id", "") or "")
    info["session"] = str(fields.get("session", "") or "")
    info["version_num"] = version_num(info["version"])
    info["date_text"] = date_text(info["date"])
    if not info["player"] or not info["date"]:
        info["error"] = (
            "二进制存档头部没读到 player/date，可能是更新版本的新字段。"
            "进游戏把设置里的 save_as_binary 改成 no 再存一次档就能读了。"
        )
    return info


def read_head(path: str | Path, nbytes: int = HEAD_BYTES) -> dict:
    """读一份存档的头部指纹。**不抛异常**，失败在 error 里写清楚。"""
    p = Path(path)
    info: dict[str, Any] = {
        "path": str(p), "name": p.name, "size": 0, "mtime": 0.0,
        "age_days": -1.0, "format": "unknown", "error": "",
        "player": "", "date": "", "date_text": "", "version": "",
        "version_num": "", "ideology": "", "difficulty": "",
        "game_unique_id": "", "session": "",
    }
    try:
        st = p.stat()
    except OSError as exc:
        info["error"] = f"读不到存档：{exc}"
        return info
    info["size"] = st.st_size
    info["mtime"] = st.st_mtime
    info["age_days"] = max(0.0, (time.time() - st.st_mtime) / 86400.0)
    if st.st_size < 64:
        info["error"] = "文件太小，可能正写到一半"
        return info

    try:
        with open(p, "rb") as fh:
            raw = fh.read(nbytes)
    except OSError as exc:
        info["error"] = f"读不了文件头：{exc}"
        return info

    if raw[:2] == b"PK":
        info["format"] = "zip"
        try:
            with zipfile.ZipFile(p) as archive:
                names = archive.namelist()
                target = next((n for n in names if "gamestate" in n.lower()), None) or \
                    next((n for n in names if n.lower().endswith(".txt")), None)
                if target is None:
                    info["error"] = f"压缩包里没找到 gamestate：{names[:6]}"
                    return info
                with archive.open(target) as fh:
                    raw = fh.read(nbytes)
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            info["error"] = f"压缩包打不开：{type(exc).__name__}: {exc}"
            return info
    elif raw.startswith(b"HOI4txt"):
        info["format"] = "text"
    elif raw.startswith(b"HOI4bin"):
        info["format"] = "binary"
        return _fill_binary_head(info, raw)
    else:
        info["error"] = "文件头不是 HOI4 存档（既不是 HOI4txt 也不是 zip）"
        return info

    txt = _decode(raw)
    for key in _HEAD_FIELDS:
        quoted = re.search(rf'^\s*{key}\s*=\s*"([^"\r\n]*)"', txt, re.M)
        bare = re.search(rf"^\s*{key}\s*=\s*([^\s\"\r\n]+)", txt, re.M)
        value = quoted.group(1) if quoted else (bare.group(1) if bare else "")
        info[key] = value.strip()
    if raw[:7] == b"HOI4bin":
        info["format"] = "binary"
        return _fill_binary_head(info, raw)

    uid = re.search(r'^\s*game_unique_id\s*=\s*"([^"\r\n]+)"', txt, re.M)
    if uid:
        info["game_unique_id"] = uid.group(1)
    ses = re.search(r"^\s*session\s*=\s*(\d+)", txt, re.M)
    if ses:
        info["session"] = ses.group(1)
    info["version_num"] = version_num(info["version"])
    info["date_text"] = date_text(info["date"])
    if not info["player"] or not info["date"]:
        info["error"] = "文件头里没读到 player/date，可能不是完整存档"
    return info


def head_meta(path: str | Path) -> dict:
    """带缓存的头部指纹。key 是 (路径, size, mtime)，文件一变自动重读。

    watcher 每 4 秒问一次，目录里可能有 49 份档，不缓存就是每轮 49 次开文件。
    """
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return read_head(p)
    key = (str(p).lower(), st.st_size, st.st_mtime)
    hit = _HEAD_CACHE.get(key)
    if hit is not None:
        return dict(hit)
    value = read_head(p)
    if len(_HEAD_CACHE) >= _CACHE_MAX:
        _HEAD_CACHE.clear()
    _HEAD_CACHE[key] = value
    return dict(value)


def clear_cache() -> None:
    _HEAD_CACHE.clear()


# ── 游戏进程 ────────────────────────────────────────────

def game_process(exe_name: str = "hoi4.exe") -> dict:
    """返回 {pid, exe, started_at, game_dir, game_version}；游戏没开返回 {}。

    启动时间用 GetProcessTimes 的创建时间，比"存档 mtime vs 现在"可靠得多：
    这次开游戏之前写的档，一定不是这次开的游戏存出来的。
    """
    if os.name != "nt":
        return {}
    try:
        from .gameguard import _find_pid

        pid = _find_pid(exe_name)
    except Exception:  # noqa: BLE001
        return {}
    if not pid:
        return {}

    out: dict[str, Any] = {"pid": pid, "exe": "", "started_at": 0.0,
                           "game_dir": "", "game_version": ""}
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        # HANDLE 是 64 位；不声明 argtypes 的话 ctypes 按 C int 传，
        # 句柄值一大就被截断，GetProcessTimes 静默失败 → 判定退回"没冲突"。
        k32.GetProcessTimes.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME),
        ]
        k32.GetProcessTimes.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return out
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            k32.QueryFullProcessImageNameW.argtypes = [
                wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                ctypes.POINTER(wintypes.DWORD),
            ]
            if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                out["exe"] = buf.value
            created, exited, kernel, user = (_FILETIME() for _ in range(4))
            if k32.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(kernel), ctypes.byref(user),
            ):
                ft = (created.high << 32) | created.low
                if ft:
                    out["started_at"] = ft / 1e7 - 11644473600.0  # FILETIME → Unix 秒
        finally:
            k32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 探测失败只影响"是不是当前局"的判断
        return out

    if out["exe"]:
        game_dir = Path(out["exe"]).parent
        out["game_dir"] = str(game_dir)
        try:
            from .config import game_dir_version

            out["game_version"] = version_num(game_dir_version(game_dir))
        except Exception:  # noqa: BLE001
            pass
    return out


_GAME_TTL = 10.0
_game_cache: tuple[float, dict] = (0.0, {})


def game_process_cached(exe_name: str = "hoi4.exe") -> dict:
    """10 秒缓存。watcher 每 4 秒问一次，不必每轮枚举进程。"""
    global _game_cache
    now = time.time()
    hit_at, hit = _game_cache
    if now - hit_at < _GAME_TTL:
        return dict(hit)
    value = game_process(exe_name)
    _game_cache = (now, value)
    return dict(value)


# ── 是不是同一局 ────────────────────────────────────────

def judge_live(
    head: dict,
    *,
    live_tag: str = "",
    game: dict | None = None,
) -> dict:
    """判断这份存档和"现在开着的这局"是什么关系。

    match 取值：
    - `no_save_this_session`：存档比这次开游戏还早，这局没存过盘（最强判据）
    - `different_game`：存档玩家国和内存里的玩家国不是一个
    - `version_diff`：存档版本和正在跑的 exe 版本不是一个
    - `old_save`：存档很久没动过，游戏又没开
    - `probable_current`：没发现冲突（不等于 100% 是实时，存档永远是快照）
    - `unknown`：信息不够
    """
    game = game or {}
    reasons: list[str] = []
    match = "unknown"
    mtime = float(head.get("mtime") or 0)
    started = float(game.get("started_at") or 0)
    age_days = float(head.get("age_days") or 0)
    player = str(head.get("player") or "")
    save_ver = str(head.get("version_num") or "")
    game_ver = str(game.get("game_version") or "")

    if game.get("pid") and started > 0 and mtime > 0 and started - mtime > 300:
        match = "no_save_this_session"
        reasons.append(f"这份存档比这次开游戏还早 {_gap_text(started - mtime)}")
    if match == "unknown" and live_tag and player and live_tag.upper() != player.upper():
        match = "different_game"
        reasons.append(f"游戏里是 {live_tag}，存档里是 {player}，不是同一个国家")
    if match == "unknown" and game_ver and save_ver and game_ver != save_ver:
        match = "version_diff"
        reasons.append(f"游戏是 {game_ver}，存档是 {save_ver}，省/州编号都不是一套")
    if match == "unknown" and age_days > 30:
        match = "old_save"
        reasons.append(f"这份存档 {age_days:.0f} 天没动过了")
    if match == "unknown" and (player or head.get("date")):
        # 进程在，但启动时间和版本一个都没读到 —— 这时候说"没冲突"是瞎乐观，
        # 宁可老实说"没能确认是不是同一局"。
        if not (game.get("pid") and not started and not game_ver):
            match = "probable_current"

    if game.get("pid"):
        ts = time.strftime("%m月%d日 %H:%M", time.localtime(started)) if started else "刚才"
        reasons.insert(0, f"游戏是 {ts} 启动的" + (f"（{game_ver}）" if game_ver else ""))
    return {
        "match": match,
        "is_current": match == "probable_current",
        "reasons": reasons,
        "live_tag": live_tag,
        "game_version": game_ver,
        "game_started_at": started,
        "save_age_days": round(age_days, 1),
    }


def banner(head: dict, judge: dict, player_label: str = "", live_label: str = "") -> str:
    """给用户看的横幅。**把"不是同一局"说到能听懂**，别只说"不是实时"。"""
    match = judge.get("match", "unknown")
    who = player_label or head.get("player") or "?"
    when = head.get("date_text") or head.get("date") or "?"
    ver = head.get("version_num") or ""
    age = float(head.get("age_days") or 0)
    stamp = time.strftime("%Y年%m月%d日", time.localtime(head.get("mtime") or 0)) \
        if head.get("mtime") else "?"
    base = f"数据来自 {when} 的 {who} 存档" + (f"（{ver}）" if ver else "")
    if age >= 1:
        base += f"，文件是 {stamp} 存的，距今 {age:.0f} 天"

    if match == "no_save_this_session":
        live = live_label or judge.get("game_version") or "现在这局"
        started = float(judge.get("game_started_at") or 0)
        gap = _gap_text(started - (head.get("mtime") or 0)) if started else ""
        return (
            f"※ {base}。这次开的是 {live}（{time.strftime('%m月%d日 %H:%M', time.localtime(started)) if started else '刚才'}启动），"
            f"存档比它早 {gap}，说明你这局还没存过盘——"
            "下面这些数字全是旧档的，不是现在的局面，"
            "别拿它判断当前战况；要看实时情况直接问我前线/部队，或者先在游戏里存一次档。"
        )
    if match == "different_game":
        return (
            f"※ {base}。游戏里玩的是 {live_label or judge.get('live_tag') or '另一个国家'}，"
            "和这份存档不是同一局。下面的数字只能当旧档看，别当成现在的局面。"
        )
    if match == "version_diff":
        return (
            f"※ {base}，而正在运行的游戏是 {judge.get('game_version') or '另一个版本'}。"
            "版本不同，州名和部队位置可能对不上；这份是旧档，不是实时。"
        )
    if match == "old_save":
        return f"※ {base}，已经很久没更新，是存档快照不是实时。"
    if match == "probable_current":
        return (
            f"※ 数据来自 {when} 的 {who} 存档，不是实时；"
            "游戏里的进度只要往前走了，就和这份档不一样。"
        )
    return (
        f"※ {base}，是存档快照不是实时；"
        "这次开的游戏和它是不是同一局没能确认，别当成当前战况。"
    )


# ── 关掉二进制存档（自愈） ──────────────────────────────

_SETTINGS_RE = re.compile(
    r'(?m)^(\s*save_as_binary\s*=\s*)("?)(yes|true|1)\2(\s*)$',
    re.IGNORECASE,
)


def disable_binary_saves(userdir: str | Path) -> dict:
    """把游戏用户目录里的 `save_as_binary` 从 yes 改成 no，让下次存档变文本。

    便携版游戏默认 `save_as_binary=yes`，存出来的是 HOI4bin。0.9.12 起正常
    HOI4bin 能直接解析，这个自愈只在**头部都读不出**的二进制档（游戏更新了
    新格式之类）时兜底触发。这里只在**能安全改**的时候动手：

    - 只在值确实是 yes/true/1 时改，已经是 no 就什么都不做；
    - 改之前备份成 `settings.txt.bak-hoi4advisor`；
    - 先写临时文件再 `os.replace`，不会写一半。

    返回 {"changed": bool, "reason": str, "path": str}。
    **调用方必须自己确保游戏没在运行**——游戏退出时会用内存里的设置
    覆盖 settings.txt，运行中改等于白改（实测过）。
    """
    settings = Path(userdir) / "settings.txt"
    out = {"changed": False, "reason": "", "path": str(settings)}
    try:
        text = settings.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        out["reason"] = f"读不到 settings.txt：{exc}"
        return out

    new_text, n = _SETTINGS_RE.subn(r"\1\2no\2\4", text)
    if n == 0:
        if re.search(r"(?m)^\s*save_as_binary\s*=", text, re.IGNORECASE):
            out["reason"] = "save_as_binary 已经是文本档设置，无需改"
        else:
            out["reason"] = "settings.txt 里没有 save_as_binary 这一项（游戏用的是默认值）"
        return out

    try:
        backup = settings.with_name(settings.name + ".bak-hoi4advisor")
        if not backup.exists():
            backup.write_text(text, encoding="utf-8")
        tmp = settings.with_name(settings.name + ".tmp")
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, settings)
    except OSError as exc:
        out["reason"] = f"写 settings.txt 失败：{exc}"
        return out

    out["changed"] = True
    out["reason"] = f"已把 save_as_binary 改成 no（备份在 {backup.name}）；下次进游戏存档就是文本，能读了"
    return out

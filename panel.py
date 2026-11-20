"""资料库面板后端：给引导界面提供数据，负责重建 / 切换世界。

前端在 `static/` 下（纯 HTML+JS），通过 `plugin_entry` 调这里的函数：

    world_snapshot   面板当前状态（游戏、mod、已有世界、进度）
    world_scan       重新找游戏、列启用中的 mod
    world_preview    数一下各个候选世界的规模（省/州/国名，秒出）
    world_build      开始生成资料库（后台线程，不阻塞宿主）
    world_progress   查生成进度
    world_switch     切换当前使用的世界
    world_delete     删掉一个世界

**为什么不在这里做重活**：生成一个世界要扫 34 MB 的 provinces.bmp，20~40 秒。
宿主对单次工具调用有看门狗，所以「生成」只负责起线程，「查进度」负责轮询。
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from . import atlas, config, save_meta

__all__ = [
    "current_world_id",
    "delete_world",
    "detect_game",
    "load_current",
    "preview_worlds",
    "progress",
    "set_current",
    "snapshot",
    "start_build",
    "startup_progress",
    "startup_ready",
    "startup_reset",
    "startup_run",
    "startup_saves",
    "startup_status",
    "switch_world",
    "world_root",
]

# 后台生成任务的状态（模块级单例：同一时刻只允许一个生成任务）
_BUILD: dict = {
    "running": False,
    "percent": 0.0,
    "phase": "",
    "error": "",
    "world_id": "",
    "started": 0.0,
    "finished": 0.0,
    "ok": False,
    "warnings": [],
    "result": {},
}
_LOCK = threading.Lock()


def world_root() -> Path:
    """资料库根目录：data/atlas/。"""
    return Path(config.data_dir()) / "atlas"


def _current_file() -> Path:
    return world_root() / "current.json"


def current_world_id() -> str:
    p = _current_file()
    if not p.is_file():
        return ""
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(raw.get("id") or "") if isinstance(raw, dict) else ""


def set_current(world_id: str) -> None:
    """把某个世界设为当前。世界不存在就不写——免得 current.json 指向空气。"""
    if world_id and not (world_root() / world_id / "meta.json").is_file():
        return
    root = world_root()
    root.mkdir(parents=True, exist_ok=True)
    _current_file().write_text(
        json.dumps({"id": world_id, "at": time.time()}, ensure_ascii=False),
        encoding="utf-8")


def current_adjacency() -> dict[int, list[int]] | None:
    """当前世界的邻接表（键已转成 int）。没建过资料库就返回 None。

    只读 adjacency.json 一个文件——别为了邻接把整个世界（几 MB）都读进来。
    """
    wid = current_world_id()
    if not wid:
        return None
    p = world_root() / wid / "adjacency.json"
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not raw:
        return None
    try:
        return {int(k): list(v) for k, v in raw.items()}
    except (TypeError, ValueError):
        return None


_NAMES_CACHE: dict = {"wid": None, "data": None}


def current_names() -> dict | None:
    """当前世界的地名表（州名 / 国名 / 城市名 / 省→州）。没有资料库返回 None。

    一次读三个小文件，比让调用方各读各的省事，也保证它们来自同一个世界。
    按世界 id 缓存——_places() 每次调用都会进来，不缓存的话每次都读几百 KB。
    """
    wid = current_world_id()
    if not wid:
        return None
    if _NAMES_CACHE["wid"] == wid and _NAMES_CACHE["data"]:
        return _NAMES_CACHE["data"]
    d = world_root() / wid
    if not (d / "meta.json").is_file():
        return None
    states: dict[int, str] = {}
    prov_to_state: dict[int, int] = {}
    try:
        raw = json.loads((d / "states.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    for sid, st in (raw or {}).items():
        try:
            states[int(sid)] = str((st or {}).get("name") or "")
            for pid in (st or {}).get("provs") or []:
                prov_to_state.setdefault(int(pid), int(sid))
        except (TypeError, ValueError, AttributeError):
            continue
    countries: dict[str, str] = {}
    try:
        countries = {str(k): str(v) for k, v in json.loads(
            (d / "countries.json").read_text(encoding="utf-8")).items()}
    except (OSError, ValueError, AttributeError):
        pass
    cities: dict[str, str] = {}
    try:
        cities = {str(k): str(v) for k, v in json.loads(
            (d / "cities.json").read_text(encoding="utf-8")).items()}
    except (OSError, ValueError, AttributeError):
        pass
    if not states and not countries:
        return None
    out = {"states": states, "countries": countries, "cities": cities,
           "prov_to_state": prov_to_state, "world": wid}
    _NAMES_CACHE["wid"] = wid
    _NAMES_CACHE["data"] = out
    return out


def load_current() -> dict | None:
    """读当前生效的那个世界的完整数据。没有就返回 None。"""
    wid = current_world_id()
    if not wid:
        return None
    return atlas.load_world(world_root(), wid)


# ──────────────────────────── 发现游戏 / mod ────────────────────────────


def detect_game(game_dir_override: str = "") -> dict:
    """找游戏安装目录，并列出每个目录下启用的 mod。"""
    dirs: list[Path] = []
    override = (game_dir_override or "").strip()
    if override:
        p = Path(override)
        if p.is_dir():
            dirs = [p]
    if not dirs:
        try:
            dirs = list(config.detect_game_dirs())
        except Exception:  # noqa: BLE001 探测失败不该把面板打挂
            dirs = []

    out = []
    for d in dirs:
        try:
            version = config.game_dir_version(d)
        except Exception:  # noqa: BLE001
            version = ""
        try:
            mods = atlas.discover_mods(d)
        except Exception:  # noqa: BLE001
            mods = []
        out.append({
            "dir": str(d),
            "version": version,
            "mods": [m.as_dict() for m in mods],
            "mod_names": [m.name for m in mods],
        })
    return {"ok": bool(out), "game_dirs": out}


def _count_side(files: atlas.WorldFiles) -> dict:
    """快速估一个世界的规模：数 definition.csv 行 + history/states 的州。"""
    csv_path = files.find("map/definition.csv")
    provinces = 0
    if csv_path is not None:
        try:
            text = csv_path.read_text(encoding="utf-8", errors="replace")
            provinces = sum(1 for ln in text.splitlines() if ln.count(";") >= 4)
        except OSError:
            provinces = 0
    states = 0
    for f in files.glob("history/states", "*.txt"):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        states += atlas.count_states_in(text)
    return {"provinces": provinces, "states": states}


def preview_worlds(game_dir: str, mod_indexes: list[int] | None = None) -> dict:
    """列出这个游戏目录能生成哪些世界。

    `mod_indexes` 指「用前 N 个启用的 mod」——None 表示全用。
    第一项永远是原版（不带 mod），方便对比。
    """
    game = Path(game_dir) if game_dir else None
    if game is None or not game.is_dir():
        return {"ok": False, "error": "游戏目录不对", "options": []}
    try:
        version = config.game_dir_version(game)
    except Exception:  # noqa: BLE001
        version = ""
    try:
        rep = atlas.discover_mods_report(game)
        mods = list(rep["mods"])
        skipped = list(rep["skipped"])
    except Exception:  # noqa: BLE001
        rep, mods, skipped = {}, [], []
    try:
        pool = atlas.all_mods(game)
    except Exception:  # noqa: BLE001
        pool = []

    options: list[dict] = []

    def _add(label: str, subset: list[atlas.ModInfo]) -> None:
        try:
            files = atlas.WorldFiles(game, subset)
            size = _count_side(files)
        except Exception as exc:  # noqa: BLE001
            options.append({"label": label, "error": f"{type(exc).__name__}: {exc}",
                            "mods": [m.name for m in subset]})
            return
        options.append({
            "label": label,
            "version": version,
            "mods": [m.as_dict() for m in subset],
            "mod_names": [m.name for m in subset],
            "provinces": size["provinces"],
            "states": size["states"],
            "files": files.describe(),
        })

    _add("原版", [])
    if mods:
        if mod_indexes is None:
            _add("原版 + " + " + ".join(m.name for m in mods), list(mods))
        else:
            for n in mod_indexes:
                n = int(n)
                if 1 <= n <= len(mods):
                    _add("前 %d 个 mod" % n, mods[:n])
    # 0.11.1：把**全量清单**也交出去，让用户能勾"我要读哪几个"，
    # 不必先在启动器里打开某个 mod 才能建它的数据。
    return {"ok": True, "game_dir": str(game), "version": version,
            "options": options,
            "user_dir": rep.get("user_dir") or "",
            "enabled_mods": [m.as_dict() for m in mods],
            "all_mods": [m.as_dict() for m in pool],
            "skipped": skipped}


# ──────────────────────────── 生成（后台） ────────────────────────────


def progress() -> dict:
    with _LOCK:
        return dict(_BUILD)


def _runner(game_dir: Path, mods: list[atlas.ModInfo], version: str) -> None:
    def _p(pct: float, phase: str) -> None:
        with _LOCK:
            _BUILD["percent"] = round(float(pct) * 100.0, 1)
            _BUILD["phase"] = str(phase or "")

    try:
        data = atlas.build_world(game_dir, mods, version, progress=_p)
        _p(0.98, "落盘")
        out = atlas.write_world(world_root(), data)
        meta = data["meta"]
        meta["id"] = out.name
        world_id = out.name
        with _LOCK:
            _BUILD.update({
                "running": False, "ok": True, "percent": 100.0, "phase": "完成",
                "world_id": world_id, "finished": time.time(),
                "warnings": list(meta.get("warnings") or [])
                            + [f"没读进来的 mod：{s.get('name')}——{s.get('reason')}"
                               for s in (_BUILD.get("skipped") or [])],
                "result": {"id": world_id, "provinces": meta.get("provinces"),
                           "states": meta.get("states"),
                           "countries": meta.get("countries"),
                           "build_seconds": meta.get("build_seconds")},
            })
        # 第一次生成成功就自动设为当前世界——用户点完引导就能直接用
        if not current_world_id():
            set_current(world_id)
    except Exception as exc:  # noqa: BLE001 后台任务要把错误带回面板
        with _LOCK:
            _BUILD.update({
                "running": False, "ok": False, "percent": 0.0,
                "phase": "失败", "finished": time.time(),
                "error": f"{type(exc).__name__}: {exc}",
            })


def start_build(game_dir: str, mod_indexes: list[int] | None = None,
                mod_names: list[str] | None = None) -> dict:
    """开始生成。已经在跑就拒绝，避免两个线程同时抢写。

    `mod_names` 是 0.11.1 加的：**按名字从全量清单里挑**，不必先在启动器里
    把 mod 打开。想给 TFR 单独建一份数据，直接在插件里点它就行。
    没给 `mod_names` / `mod_indexes` 就按启动器当前启用的那套（旧行为）。
    """
    with _LOCK:
        if _BUILD.get("running"):
            return {"ok": False, "error": "上一次生成还没结束，等它跑完"}
        _BUILD.update({
            "running": True, "ok": False, "percent": 0.0, "phase": "准备",
            "error": "", "world_id": "", "started": time.time(),
            "finished": 0.0, "warnings": [], "result": {},
        })

    game = Path(game_dir) if game_dir else None
    if game is None or not game.is_dir():
        with _LOCK:
            _BUILD.update({"running": False, "ok": False, "phase": "失败",
                           "error": "游戏目录不对"})
        return {"ok": False, "error": "游戏目录不对"}

    try:
        version = config.game_dir_version(game)
    except Exception as exc:  # noqa: BLE001
        with _LOCK:
            _BUILD.update({"running": False, "ok": False, "phase": "失败",
                           "error": f"{type(exc).__name__}: {exc}"})
        return {"ok": False, "error": str(exc)}

    skipped: list[dict] = []
    try:
        if mod_names:
            mods, miss = atlas.mods_by_names(game, list(mod_names))
            skipped = [{"name": n, "reason": "这个 mod 没找到（名字对不上）"}
                       for n in miss]
        else:
            rep = atlas.discover_mods_report(game)
            mods = list(rep["mods"])
            skipped = list(rep["skipped"])
    except Exception as exc:  # noqa: BLE001
        with _LOCK:
            _BUILD.update({"running": False, "ok": False, "phase": "失败",
                           "error": f"{type(exc).__name__}: {exc}"})
        return {"ok": False, "error": str(exc)}

    if mod_indexes is not None:
        picked2: list[atlas.ModInfo] = []
        for n in mod_indexes:
            n = int(n)
            if 1 <= n <= len(mods):
                picked2.append(mods[n - 1])
        mods = picked2

    # 读不进来的 mod 一定要说出来：世界 id 是按 mod 集合算的，
    # 少一个就是另一个世界，界面上一个字不说等于骗人。
    if skipped:
        with _LOCK:
            _BUILD["skipped"] = skipped

    threading.Thread(target=_runner, args=(game, mods, version),
                     name="hoi4-atlas-build", daemon=True).start()
    return {"ok": True, "started": True, "mods": [m.name for m in mods],
            "skipped": skipped}


# ──────────────────────────── 切换 / 删除 ────────────────────────────


def _safe_id(world_id: str) -> str:
    wid = str(world_id or "").strip()
    if not wid or "/" in wid or "\\" in wid or wid.startswith("."):
        raise ValueError("世界 id 不合法")
    return wid


def switch_world(world_id: str) -> dict:
    wid = _safe_id(world_id)
    meta = atlas.read_world_meta(world_root() / wid)
    if not meta:
        return {"ok": False, "error": f"资料库里没有「{wid}」"}
    set_current(wid)
    return {"ok": True, "id": wid, "meta": meta}


def delete_world(world_id: str) -> dict:
    wid = _safe_id(world_id)
    d = world_root() / wid
    meta = atlas.read_world_meta(d)
    if not meta:
        return {"ok": False, "error": f"资料库里没有「{wid}」"}
    if current_world_id() == wid:
        return {"ok": False, "error": "这个正在用，先切到别的世界再删"}
    for f in d.iterdir():
        f.unlink(missing_ok=True)
    d.rmdir()
    return {"ok": True, "id": wid}


# ──────────────────────────── 面板状态 ────────────────────────────


def _is_stale(meta: dict) -> bool:
    """资料库是不是过期了：当初读的那些文件，现在 mtime/size 变了。

    老数据没有 `sources` 字段，一律当"不过期"——宁可少报，不误报。
    """
    src = meta.get("sources") or []
    if not src:
        return False
    for ent in src:
        p = ent.get("path") or ""
        if not p:
            continue
        try:
            st = Path(p).stat()
        except OSError:
            return True          # 文件没了，肯定过期
        if st.st_size != ent.get("size") or int(st.st_mtime) != ent.get("mtime"):
            return True
    return False


def snapshot(game_dir_override: str = "") -> dict:
    """一次性给全前端要的东西。"""
    game = detect_game(game_dir_override)
    worlds = []
    cur = current_world_id()
    for meta in atlas.list_worlds(world_root()):
        wid = str(meta.get("id") or "")
        worlds.append({
            "id": wid,
            "label": ("原版" if not meta.get("mods")
                      else " + ".join(meta.get("mod_names") or [])),
            "game_version": meta.get("game_version") or "",
            "provinces": meta.get("provinces") or 0,
            "states": meta.get("states") or 0,
            "countries": meta.get("countries") or 0,
            "cities": meta.get("cities") or 0,
            "adjacency": meta.get("adjacency") or 0,
            "built_at": meta.get("built_at") or "",
            "build_seconds": meta.get("build_seconds") or 0,
            "warnings": meta.get("warnings") or [],
            "stale": _is_stale(meta),
            "current": wid == cur,
        })
    return {
        "ok": True,
        "game": game,
        "worlds": worlds,
        "current": cur,
        "build": progress(),
        "atlas_dir": str(world_root()),
    }


# ──────────────────────── 开始引导（启动闸门） ────────────────────────
#
# 用户要的流程：**开游戏 → 面板里选读哪个存档 → 进度条注入代理 → 全绿才能用**。
#
# 为什么要有这个闸门：以前第一次用的时候各步散着来——先要体检看代理注没注，
# 再去选存档，再去建世界，任何一步失败都只在某个工具里冒一句，用户根本不知道
# "我现在到底能不能用"。这里把四件事串成一次带进度的启动，**没跑完就不让用**。
#
# PRC_2020（TFR 那局）实测：存档 116 MB，全量解析 33 秒 —— 这种长度放进工具里
# 必然撞穿宿主 30 秒看门狗。所以启动流程跑在后台线程，前端轮询进度，
# 不在任何一次工具调用里做重活。

_STARTUP: dict = {
    "running": False,
    "ok": False,
    "ready": False,
    "percent": 0.0,
    "phase": "",
    "steps": [],          # [{name, ok, note}] 已完成的步骤
    "error": "",
    "save": "",
    "save_label": "",
    "game_dir": "",
    "world": "",
    "agent": "",
    "warnings": [],
    "started": 0.0,
    "finished": 0.0,
}
_START_LOCK = threading.Lock()
_START_FILE = "startup.json"


def _startup_path() -> Path:
    return config.data_dir() / _START_FILE


def _startup_set(**kw) -> None:
    with _START_LOCK:
        _STARTUP.update(kw)


def _startup_step(name: str, ok: bool, note: str = "") -> None:
    with _START_LOCK:
        _STARTUP["steps"] = list(_STARTUP.get("steps") or []) + [
            {"name": name, "ok": bool(ok), "note": str(note or "")}]


def startup_ready() -> bool:
    """**门禁**：只有一次完整成功的启动之后才允许用工具。

    从磁盘读：插件重载后闸门状态得还在，否则用户刚启动完、一重载又要重来。
    """
    with _START_LOCK:
        if _STARTUP.get("ready"):
            return True
    try:
        raw = json.loads(_startup_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(raw.get("ready"))


def startup_status() -> dict:
    """当前闸门状态。前端每次进来先问这个，决定是显示引导还是直接用。"""
    with _START_LOCK:
        cur = dict(_STARTUP)
    try:
        saved = json.loads(_startup_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        saved = {}
    if not cur.get("ready") and saved.get("ready"):
        # 磁盘上有"已完成"的记录，但内存里没有（刚重载）——以磁盘为准，
        # 顺手把内存补上，免得面板显示"未就绪"却能用。
        with _START_LOCK:
            _STARTUP.update({
                "ready": True, "ok": True, "percent": 100.0, "phase": "就绪",
                "save": saved.get("save", ""),
                "save_label": saved.get("save_label", ""),
                "game_dir": saved.get("game_dir", ""),
                "world": saved.get("world", ""),
                "agent": saved.get("agent", ""),
            })
            cur = dict(_STARTUP)
    return {
        "ok": True,
        "ready": bool(cur.get("ready")),
        "running": bool(cur.get("running")),
        "percent": float(cur.get("percent") or 0.0),
        "phase": cur.get("phase") or "",
        "steps": cur.get("steps") or [],
        "error": cur.get("error") or "",
        "save": cur.get("save") or "",
        "save_label": cur.get("save_label") or "",
        "game_dir": cur.get("game_dir") or "",
        "world": cur.get("world") or "",
        "agent": cur.get("agent") or "",
        "warnings": cur.get("warnings") or [],
    }


def startup_saves(game_dir: str = "", limit: int = 40) -> dict:
    """列出能读的存档，给引导页挑。

    每一项都带**够不够用来判断**的字段：日期、玩家、版本、大小、是不是二进制、
    以及它大致属于哪个世界（按启用 mod 猜）。
    """
    dirs: list[Path] = []
    try:
        if game_dir:
            from . import atlas as _atlas
            u = _atlas.game_user_dir(Path(game_dir))
            if u is not None:
                dirs.append(Path(u) / "save games")
        if not dirs:
            dirs = list(config.detect_save_dirs())
    except Exception:  # noqa: BLE001
        dirs = []

    out: list[dict] = []
    for d in dirs:
        if not Path(d).is_dir():
            continue
        for f in sorted(Path(d).glob("*.hoi4"), key=lambda p: -p.stat().st_mtime):
            if len(out) >= int(limit):
                break
            try:
                h = save_meta.head_meta(f)
                st = f.stat()
            except Exception as exc:  # noqa: BLE001 单个档坏了不能拖垮列表
                out.append({"path": str(f), "name": f.name, "error":
                            f"{type(exc).__name__}: {exc}"})
                continue
            out.append({
                "path": str(f),
                "name": f.name,
                "dir": str(d),
                "player": h.get("player") or "",
                "date_text": h.get("date_text") or "",
                "version": h.get("version_num") or "",
                "format": h.get("format") or "",
                "size_mb": round(st.st_size / 1024 / 1024, 1),
                "mtime": st.st_mtime,
                "error": "",
            })
    out.sort(key=lambda x: -(x.get("mtime") or 0))
    return {"ok": True, "saves": out[: int(limit)],
            "current": (current_world_id() or "")}


def startup_run(save_path: str, game_dir: str = "") -> dict:
    """开始一次启动流程（后台线程跑，立刻返回）。"""
    with _START_LOCK:
        if _STARTUP.get("running"):
            return {"ok": False, "error": "上一次启动还没跑完"}
        _STARTUP.update({
            "running": True, "ok": False, "ready": False, "percent": 0.0,
            "phase": "准备", "steps": [], "error": "", "warnings": [],
            "save": str(save_path or ""), "save_label": "",
            "game_dir": str(game_dir or ""), "world": "", "agent": "",
            "started": time.time(), "finished": 0.0,
        })
    threading.Thread(target=_startup_runner,
                     args=(str(save_path or ""), str(game_dir or "")),
                     name="hoi4-startup", daemon=True).start()
    return {"ok": True, "started": True}


def _startup_runner(save_path: str, game_dir: str) -> None:
    """四步：找游戏 → 注入代理 → 校验存档 → 对上世界。任一步失败就停。"""
    def bad(phase: str, msg: str) -> None:
        _startup_set(running=False, ok=False, ready=False, phase=phase,
                     error=msg, finished=time.time())

    try:
        # ── 1. 游戏目录 ──
        _startup_set(percent=4.0, phase="找游戏目录")
        game = Path(game_dir) if game_dir else None
        if game is None or not game.is_dir():
            try:
                dirs = list(config.detect_game_dirs())
            except Exception:  # noqa: BLE001
                dirs = []
            game = dirs[0] if dirs else None
        if game is None or not game.is_dir():
            _startup_step("找游戏目录", False, "没找到")
            return bad("找游戏目录", "没找到游戏目录。点「手动指定」选到 Hearts of Iron IV 那一层。")
        _startup_set(game_dir=str(game), percent=10.0)
        _startup_step("找游戏目录", True, str(game))

        # ── 2. 游戏进程 ──
        _startup_set(percent=16.0, phase="等游戏进程")
        try:
            proc = save_meta.game_process_cached()
        except Exception:  # noqa: BLE001
            proc = {}
        if not (proc or {}).get("running"):
            _startup_step("等游戏进程", False, "hoi4.exe 没在跑")
            return bad("等游戏进程",
                       "游戏没开（或者还在启动画面）。先把游戏开到地图界面，再点重试。")
        _startup_step("等游戏进程", True,
                      f"pid={proc.get('pid')} 启动于 {proc.get('started_at_text') or ''}")

        # ── 3. 版本 / 偏移 ──
        _startup_set(percent=26.0, phase="校验版本偏移")
        try:
            from . import livegame as _lg
            lg = _lg.LiveGame(config.data_dir(), log=lambda m: None)
            build = lg._build()               # noqa: SLF001 偏移表自检
            ver = config.game_dir_version(game)
            _startup_step("校验版本偏移", True,
                          f"游戏 {ver}，order_type={build.get('order_type')}")
        except Exception as exc:  # noqa: BLE001
            _startup_step("校验版本偏移", False, f"{type(exc).__name__}: {exc}")
            return bad("校验版本偏移",
                       f"偏移对不上（{type(exc).__name__}: {exc}）。"
                       f"多半是游戏版本和我这套偏移不一致，只能看不能动。")
        _startup_set(percent=36.0)

        # ── 4. 注入代理 ──
        _startup_set(percent=44.0, phase="注入代理")
        try:
            from . import nekoagent as _na
            ag = _na.ensure(wait=25.0)
            try:
                pong = ag.ping() if hasattr(ag, "ping") else ""
            finally:
                try:
                    ag.close()
                except Exception:  # noqa: BLE001
                    pass
            _startup_set(percent=58.0, agent=(pong or "已注入"))
            _startup_step("注入代理", True, pong or "已注入")
        except Exception as exc:  # noqa: BLE001
            _startup_step("注入代理", False, f"{type(exc).__name__}: {exc}")
            return bad("注入代理",
                       f"代理注不进去（{type(exc).__name__}: {exc}）。"
                       f"杀毒软件可能拦了 DLL；看插件日志里「注入」那几行。")

        # ── 5. 校验存档 ──
        _startup_set(percent=66.0, phase="校验存档")
        sp = Path(save_path) if save_path else None
        if sp is None or not sp.is_file():
            _startup_step("校验存档", False, "没选存档")
            return bad("校验存档", "还没选存档。在上面的列表里点一个。")
        try:
            head = save_meta.head_meta(sp)
        except Exception as exc:  # noqa: BLE001
            _startup_step("校验存档", False, f"{type(exc).__name__}: {exc}")
            return bad("校验存档", f"这个档读不了（{type(exc).__name__}: {exc}）。")
        if (head.get("format") or "") == "binary":
            _startup_step("校验存档", False, "二进制档")
            return bad("校验存档",
                       "这是二进制存档，插件解析不了。在游戏设置里把「压缩存档」"
                       "关掉，然后重新存一次。")
        _startup_set(
            percent=82.0,
            save=str(sp),
            save_label=f"{head.get('player') or '?'} · "
                       f"{head.get('date_text') or head.get('date') or '?'}",
        )
        _startup_step("校验存档", True,
                      f"{head.get('player')} {head.get('date_text')}")

        # ── 6. 世界数据要跟当前 mod 集合对得上 ──
        _startup_set(percent=88.0, phase="核对世界数据")
        warns: list[str] = []
        cur = current_world_id()
        meta = {}
        try:
            if cur:
                meta = read_world_meta(world_root() / cur) or {}
        except Exception:  # noqa: BLE001
            meta = {}
        want_mods: set[str] = set()
        try:
            from . import atlas as _atlas2
            want_mods = {m.name for m in _atlas2.discover_mods(game)}
        except Exception:  # noqa: BLE001
            want_mods = set()
        have_mods = set()
        try:
            have_mods = {str((m or {}).get("name") or "")
                         for m in (meta.get("mods") or [])}
        except Exception:  # noqa: BLE001
            have_mods = set()
        if not cur:
            warns.append("还没有任何世界数据 —— 到「世界资料库」页读一份，"
                         "否则地名/邻接会退回原版快照。")
            _startup_step("核对世界数据", False, "没有世界数据")
        elif want_mods - have_mods:
            miss = "、".join(sorted(x for x in (want_mods - have_mods) if x))
            warns.append(f"当前世界（{cur}）没包含这些启用的 mod：{miss}。"
                         f"到「世界资料库」页按当前 mod 重建一份。")
            _startup_step("核对世界数据", False, f"缺 mod：{miss}")
        else:
            _startup_step("核对世界数据", True,
                          f"{cur}（{meta.get('provinces')} 省）")

        # ── 7. 就绪 ──
        _startup_set(percent=100.0, phase="就绪", running=False, ok=True,
                     ready=True, world=cur or "", warnings=warns,
                     finished=time.time())
        try:
            _startup_path().parent.mkdir(parents=True, exist_ok=True)
            _startup_path().write_text(json.dumps({
                "ready": True, "save": str(sp),
                "save_label": _STARTUP.get("save_label") or "",
                "game_dir": str(game), "world": cur or "",
                "agent": _STARTUP.get("agent") or "",
                "at": time.time(),
            }, ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001 落盘失败不影响本次使用
            pass
    except Exception as exc:  # noqa: BLE001 兜底：后台线程不能静默死
        import traceback as _tb
        _startup_set(running=False, ok=False, ready=False, phase="失败",
                     error=f"{type(exc).__name__}: {exc}",
                     finished=time.time())
        _startup_step("未预期错误", False, f"{type(exc).__name__}: {exc}")
        print("[startup]", _tb.format_exc())


def startup_progress() -> dict:
    with _START_LOCK:
        return dict(_STARTUP)


def startup_reset() -> dict:
    """把闸门关掉（换游戏/换存档时用）。"""
    with _START_LOCK:
        if _STARTUP.get("running"):
            return {"ok": False, "error": "正在启动中，等它跑完"}
        _STARTUP.update({
            "running": False, "ok": False, "ready": False, "percent": 0.0,
            "phase": "", "steps": [], "error": "", "save": "",
            "save_label": "", "world": "", "agent": "", "warnings": [],
        })
    try:
        _startup_path().unlink()
    except OSError:
        pass
    return {"ok": True, "reset": True}

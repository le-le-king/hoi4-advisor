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

from . import atlas, config

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
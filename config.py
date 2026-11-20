"""路径探测与配置。

规矩（从围棋插件那一轮定下的）：
- 所有落盘（配置、缓存、日志）以**插件目录为根**，不写 TEMP、不写用户目录。
- 不硬编码任何本机路径。换台机器要能自己找。

存档目录为什么不能写死：这台机器同时存在
`<Documents>\\Paradox Interactive\\Hearts of Iron IV\\save games` 和
`<PerfLogs>\\Paradox Interactive\\...` 两份（Documents 被重定向过），
而且玩家常把存档目录改到别的盘。所以按"候选打分"挑，挑不到再让用户手动指。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import save_meta

__all__ = [
    "plugin_dir",
    "data_dir",
    "load_config",
    "save_config",
    "detect_save_dirs",
    "find_latest_save",
    "detect_game_dirs",
    "resolve_paths",
]

CONFIG_NAME = "config.json"


def plugin_dir() -> Path:
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    d = plugin_dir() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── 配置 ────────────────────────────────────────────────

_DEFAULT_CONFIG = {
    # 用户指定的存档目录（留空 = 自动探测）
    "save_dirs": [],
    # 用户指定的具体存档文件（留空 = 自动挑最新的）
    "save_file": "",
    # 用户指定的游戏安装目录（留空 = 自动探测）
    "game_dir": "",
    # 只认这些存档名前缀的优先级更高（自动存档 + 玩家国 tag）
    "prefer_autosave": True,
    # 存档轮询间隔（秒）
    "watch_interval": 4.0,
    # 同一份存档只在 mtime+size 都变了才重解析
    "allow_subprocess_parse": True,
    # 启动时自动把桥接 DLL 注进在跑的游戏（关掉就只在你开口时注入）
    "auto_inject": True,
    # ── 按住说话（0.8.0：按键系统级独占） ─────────────
    # 开关。默认关着——它要占一个按键，得明确打开
    "ptt_enabled": False,
    # 按住说话的键。**这个键会被插件独占**（RegisterHotKey），游戏收不到，
    # 所以不会再出现"按 F7 说话的同时给游戏下了一道 F7 指令"。
    # 代价是这个键在游戏里不能再用作别的绑定，挑个游戏里用不到的。
    "ptt_key": "F7",
    # 只在钢四进程存在时接管这个键。关掉就变成任何程序里按都会录音，
    # 而且会跟宿主的 globalShortcut 硬碰硬（谁先注册谁赢）
    "ptt_require_game": True,
    # ── 游戏内浮层（0.8.0，0.9.0 默认关） ───────────
    # 在游戏画面上显示"正在听 / 识别中 / 她说了什么"。
    # **0.9.0 起默认关掉**：说话和她的回答都直接进猫娘聊天窗，
    # 用户开着对话框打游戏，不需要画面上再浮一条。想要浮层再改 True。
    "ptt_overlay": False,
    # 浮层宽度 / 高度 / 离屏幕底边的距离（像素，未算 DPI 缩放）
    # 钢四底部 HUD 大概占 150，放 190 刚好在她头顶上方
    "ptt_overlay_width": 720,
    "ptt_overlay_height": 108,
    "ptt_overlay_margin": 190,
    # 语音调令：说"把华沙的师调到柏林""在柏林划一条防线"就直接动手，
    # 不用等模型。认不出来的话照旧当普通聊天。关掉 = 全部交给对话
    "ptt_voice_order": True,
    # 录音设备号，-1 = 系统默认。用 hoi4_voice action=devices 看可选值
    "ptt_mic_device": -1,
    # 识别引擎：local = 插件自带的 paraformer（默认，离线、不经过宿主）
    "ptt_engine": "local",
    # 模型目录。留空 = data/models/paraformer-zh
    "ptt_model_dir": "",
    # 识别线程数。i5 级 CPU 上 2 就够了（1.7 秒音频只要 0.07 秒）
    "ptt_asr_threads": 2,
    # 插件启动后后台预热模型（加载 216 MB 约 6 秒，别等用户第一次按键才加载）
    "ptt_asr_preload": True,
    # ── 游戏闸门（0.7.0） ──────────────────────────
    # 游戏进程存在时，让宿主注销全部 Electron 全局快捷键（F2–F8 不再被抢）
    "game_guard_enabled": True,
    # ── 前线哨兵（0.9.2） ──────────────────────────
    # 只读内存：开局报到一次总览，之后每 30 秒报交火/部队进入。
    # **不调用任何引擎函数**（0.9.1 那次崩游戏是逆向探针乱调 C1F6D0，不是它）。
    # 只认"用户亲口确认过"的国家——存档可能是上一局的，自动认人会报错国家。
    "front_watch_enabled": True,
    # 认哪个进程算"游戏在跑"
    "game_exe": "hoi4.exe",
    # 检测到最新存档是**读不了的**二进制档（HOI4bin 头部解析失败，通常是游戏
    # 更新了新格式）、且游戏没在运行时，自动把游戏用户目录 settings.txt 里的
    # save_as_binary 改成 no（改前备份）。0.9.12 起正常二进制档能直接读，
    # 这只是兜底。不想让插件动游戏设置就关掉。
    "auto_text_saves": True,
    # 老的双击类热键（F9 接管 / F10 放开）。按住说话开着时默认不起，冲突
    "legacy_hotkeys": False,
}


def load_config() -> dict:
    path = plugin_dir() / CONFIG_NAME
    cfg = dict(_DEFAULT_CONFIG)
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg.update({k: v for k, v in raw.items() if k in _DEFAULT_CONFIG})
        except (OSError, ValueError):
            pass
    return cfg


def save_config(cfg: dict) -> None:
    """写配置。先写临时文件再替换，避免写一半被读到。"""
    path = plugin_dir() / CONFIG_NAME
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        # 插件目录只读时退到数据目录
        alt = data_dir() / CONFIG_NAME
        alt.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Documents 真实位置 ──────────────────────────────────

def _known_folders() -> list[Path]:
    """Windows 上 Documents 可能被重定向（OneDrive、别的盘），优先问注册表。"""
    out: list[Path] = []
    if os.name != "nt":
        home = Path.home()
        out.append(home / "Documents")
        return out

    try:
        import winreg

        key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as h:
            raw, _ = winreg.QueryValueEx(h, "Personal")
            expanded = os.path.expandvars(raw)
            if expanded:
                out.append(Path(expanded))
    except (OSError, ImportError):
        pass

    home = Path(os.environ.get("USERPROFILE") or Path.home())
    out.append(home / "Documents")
    out.append(home / "OneDrive" / "Documents")
    return out


# ── 存档目录 ────────────────────────────────────────────

_PARADOX_REL = Path("Paradox Interactive") / "Hearts of Iron IV" / "save games"


_PROBE_TTL = 120.0
_probe_cache: dict[str, tuple[float, list[Path]]] = {}


def _cached_probe(key: str, fn) -> list[Path]:
    """目录探测带 TTL 缓存。

    watcher 每 4 秒问一次"存档在哪"，而游戏目录探测要浅扫各盘符，
    不缓存的话每 4 秒扫一遍全盘，风扇会叫。
    120 秒一次足够了——游戏目录和存档目录都不常变。
    """
    now = time.time()
    hit = _probe_cache.get(key)
    if hit is not None and now - hit[0] < _PROBE_TTL:
        return list(hit[1])
    value = fn()
    _probe_cache[key] = (now, value)
    return value


def detect_save_dirs() -> list[Path]:
    """返回所有看起来像 HOI4 存档目录的路径（存在的、非空的）。"""
    return _cached_probe("save_dirs", _detect_save_dirs_raw)


def _detect_save_dirs_raw() -> list[Path]:
    cands: list[Path] = []

    # 便携版（绿色版）游戏把用户数据写在**安装目录下**的 Paradox Interactive。
    # 实测这台机器：1.16.9 那份装在 D:\Games\Hearts of Iron IV，
    # 存档全写在自己的 <安装目录>\Paradox Interactive\Hearts of Iron IV\save games，
    # Documents 里只有另一个 1.14.8 安装留下的半年前文本档。
    # 不扫这里就永远拿旧游戏的数据报当前战况（0.9.11 之前的实际故障）。
    # 正在跑的进程的安装目录排最前——它才是正在存盘的那个。
    try:
        running = save_meta.game_process_cached()
        if running.get("game_dir"):
            cands.append(Path(running["game_dir"]) / _PARADOX_REL)
    except Exception:  # noqa: BLE001 进程探测失败不影响后面的候选
        pass
    try:
        for gd in detect_game_dirs():
            cands.append(Path(gd) / _PARADOX_REL)
    except Exception:  # noqa: BLE001
        pass

    for doc in _known_folders():
        cands.append(doc / _PARADOX_REL)

    # Paradox 有时把日志/存档写到盘根（PerfLogs 这种重定向残留）
    if os.name == "nt":
        for drive in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            root = Path(f"{drive}:/")
            if not root.exists():
                continue
            for sub in ("PerfLogs", "Documents", "Users/Public/Documents"):
                cands.append(root / sub / _PARADOX_REL)

    seen: set[str] = set()
    out: list[Path] = []
    for p in cands:
        try:
            key = str(p.resolve()).lower()
        except OSError:
            continue
        if key in seen or not p.is_dir():
            continue
        seen.add(key)
        out.append(p)
    return out


def find_latest_save(save_dir: Path, prefer_autosave: bool = True) -> Path | None:
    """目录里最新的存档。

    只按 mtime 挑是不够的：玩家手动存一个 `CHI_1940_05_01.hoi4` 之后，
    autosave 的 mtime 可能更旧。所以先看 mtime 最新的前 5 个，
    里面优先 autosave / 玩家国 tag 命名的。
    """
    if not save_dir.is_dir():
        return None
    files: list[tuple[float, Path]] = []
    try:
        for p in save_dir.glob("*.hoi4"):
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size < 1024:
                continue
            files.append((st.st_mtime, p))
    except OSError:
        return None
    if not files:
        return None

    files.sort(key=lambda kv: -kv[0])
    top = files[:5]
    if prefer_autosave:
        for _, p in top:
            if "autosave" in p.name.lower():
                return p
    return top[0][1]


# 存档文件名里出现这些词的是副本/快捷方式，不是正在玩的档
_SKIP_MARKERS = ("副本", "快捷方式", " - copy", "_copy")


def find_best_save(save_dirs: list[Path], explicit: str | None = None) -> Path | None:
    """跨目录挑"最近在玩的那一份"。

    为什么不能按目录顺序取第一个：这台机器有多个存档目录
    （Documents、便携版游戏自己的 Paradox Interactive、盘根 PerfLogs 残留），
    各有一批档。按目录顺序会拿到几个月前另一局的档，
    然后一本正经地分析一局早就不存在的游戏。所以按 mtime 全局比。

    0.9.11 修的核心 bug：**mtime 最新的档就是"现在玩的这局"，哪怕它读不了**。
    旧逻辑是"优先挑最新的*可读文本*档，二进制档只当 fallback"——
    结果便携版游戏 save_as_binary=yes 时，11 月的德国二进制档
    永远输给 5 月的卢森堡文本档，插件一直拿半年前另一局的数据报"当前局势"。

    0.9.12 起二进制档能直接解析（pdx_binary），可读的二进制档和文本档
    一视同仁进"最新可读"桶。现在的规则：
    - 最新的档可读（文本或二进制）→ 直接用；
    - 最新的档是读不了的二进制（未来版本等）→ 还是返回它（让 watcher
      明确报错，binary_blocker 会给用户能照做的提示），**绝不**退回
      旧文本档冒充现状；
    - 最新的档是写坏的残档（非二进制、头部读不出）→ 才退回最新的可读档。
    """
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p

    newest_readable: tuple[float, Path] | None = None
    newest_other: tuple[float, Path, dict] | None = None
    for d in save_dirs:
        if not d.is_dir():
            continue
        try:
            entries = list(d.glob("*.hoi4"))
        except OSError:
            continue
        for f in entries:
            low = f.name.lower()
            if any(mark in low for mark in _SKIP_MARKERS):
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            if st.st_size < 1024:
                continue
            head = save_meta.head_meta(f)
            if not head.get("error") and head.get("player"):
                if newest_readable is None or st.st_mtime > newest_readable[0]:
                    newest_readable = (st.st_mtime, f)
            else:
                if newest_other is None or st.st_mtime > newest_other[0]:
                    newest_other = (st.st_mtime, f, head)

    if newest_other is not None:
        o_mt, o_path, o_head = newest_other
        is_binary = o_head.get("format") == "binary"
        newer_than_readable = newest_readable is None or o_mt > newest_readable[0]
        # 读不了的二进制档：它才是"现在玩的这局"，返回它让上层明确报错，
        # 别拿旧档冒充现状。
        if is_binary and newer_than_readable:
            return o_path
        # 非二进制的坏档（写到一半等）比文本档新 → 信不过，退回文本档。
    if newest_readable is not None:
        return newest_readable[1]
    if newest_other is not None:
        return newest_other[1]
    return None


def find_binary_blocker(save_dirs: list[Path], chosen: Path | None = None) -> dict | None:
    """全盘最新的存档如果是**读不了的**二进制档，返回它的情况；否则 None。

    为什么单独立一个函数：`find_best_save` 会跳过头部不合法的文件，
    于是"最新档读不了"这个事实被静悄悄吞掉，端出来的是几个月前的旧文本档，
    横幅还说"你这局没存过盘"——用户明明刚存过。0.9.11 实测就是这个组合：
    便携版游戏 save_as_binary=yes，所有新档都是 HOI4bin。

    0.9.12 起插件能直接解析 HOI4bin（pdx_binary），正常的二进制档不再算
    阻塞；只有头部指纹都读不出来的（未来新版本、字段表没覆盖到）才返回，
    触发 watcher 的「改回文本存档」自愈兜底。

    返回 {"path", "name", "mtime", "when", "userdir", "is_chosen"}，
    userdir 是 settings.txt 所在的游戏用户目录（save games 的上一级）。
    """
    newest: tuple[float, Path] | None = None
    for d in save_dirs:
        if not d.is_dir():
            continue
        try:
            entries = list(d.glob("*.hoi4"))
        except OSError:
            continue
        for f in entries:
            low = f.name.lower()
            if any(mark in low for mark in _SKIP_MARKERS):
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            if st.st_size < 1024:
                continue
            if newest is None or st.st_mtime > newest[0]:
                newest = (st.st_mtime, f)
    if newest is None:
        return None
    mtime, path = newest
    head = save_meta.head_meta(path)
    if head.get("format") != "binary" or not head.get("error"):
        return None
    is_chosen = False
    if chosen is not None:
        try:
            is_chosen = Path(chosen).resolve() == path.resolve()
        except OSError:
            is_chosen = str(chosen).lower() == str(path).lower()
    return {
        "path": str(path),
        "name": path.name,
        "mtime": mtime,
        "when": time.strftime("%m月%d日 %H:%M", time.localtime(mtime)),
        "userdir": str(path.parent.parent),
        "is_chosen": is_chosen,
    }


# ── 游戏安装目录 ────────────────────────────────────────

_GAME_MARKERS = ("history/states", "common/countries", "localisation")


def _looks_like_game_dir(p: Path) -> bool:
    return p.is_dir() and all((p / m).is_dir() for m in _GAME_MARKERS)


def detect_game_dirs() -> list[Path]:
    """找游戏安装目录（带 TTL 缓存）。"""
    return _cached_probe("game_dirs", _detect_game_dirs_raw)


def _detect_game_dirs_raw() -> list[Path]:
    """找游戏安装目录。

    优先级：环境变量 > Steam 库 > 盘符浅扫。
    浅扫只到 3 层，且只在配置缺失时跑一次，结果会被写进配置缓存。
    """
    cands: list[Path] = []

    env = os.environ.get("HOI4_GAME_DIR")
    if env:
        cands.append(Path(env))

    # Steam 库清单
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as h:
                steam = Path(winreg.QueryValueEx(h, "SteamPath")[0])
            cands.append(steam / "steamapps" / "common" / "Hearts of Iron IV")
            vdf = steam / "steamapps" / "libraryfolders.vdf"
            if vdf.is_file():
                text = vdf.read_text(encoding="utf-8", errors="replace")
                for line in text.splitlines():
                    if '"path"' in line:
                        parts = line.split('"')
                        if len(parts) >= 4:
                            cands.append(
                                Path(parts[3]) / "steamapps" / "common" / "Hearts of Iron IV"
                            )
        except (OSError, ImportError, IndexError):
            pass

    seen: set[str] = set()
    out: list[Path] = []
    for p in cands:
        if _looks_like_game_dir(p):
            key = str(p).lower()
            if key not in seen:
                seen.add(key)
                out.append(p)

    if out:
        return out

    # 盘符浅扫（代价大，放最后）。这里**不能提前 break**：
    # 这台机器两个盘都有游戏目录，两份版本还不一样，
    # 谁先被扫到纯看字典序，必须全收回来再按存档版本挑。
    for drive in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = Path(f"{drive}:/")
        if not root.exists():
            continue
        try:
            for sub in root.iterdir():
                if not sub.is_dir():
                    continue
                if "hearts" in sub.name.lower() and _looks_like_game_dir(sub):
                    key = str(sub).lower()
                    if key not in seen:
                        seen.add(key)
                        out.append(sub)
                    continue
                try:
                    for sub2 in list(sub.iterdir())[:200]:
                        if not sub2.is_dir() or "hearts" not in sub2.name.lower():
                            continue
                        if not _looks_like_game_dir(sub2):
                            continue
                        key = str(sub2).lower()
                        if key not in seen:
                            seen.add(key)
                            out.append(sub2)
                except OSError:
                    continue
        except OSError:
            continue
    return out


def game_dir_version(game_dir: Path) -> str:
    """从 changelog 头里抠版本号。抠不到返回空串。"""
    changelog = game_dir / "changelog.txt"
    if not changelog.is_file():
        return ""
    try:
        head = changelog.read_text(encoding="utf-8", errors="replace")[:2000]
    except OSError:
        return ""
    m = re.search(r"(\d+\.\d+\.\d+)", head)
    return m.group(1) if m else ""


def _ver_tuple(text: str) -> tuple[int, ...]:
    """从任意版本串里抠数字部分。

    存档里长这样：`Stella Polaris v1.14.8.0fd4 (6f76)`，
    不能按空白切（第一个 token 是代号），得先正则抠连贯数字段。
    """
    m = re.search(r"\d+(?:\.\d+)+", text or "")
    if not m:
        return ()
    return tuple(int(p) for p in m.group().split(".") if p.isdigit())


def pick_game_dir(candidates: list[Path], save_version: str) -> Path | None:
    """多份游戏目录时，挑和存档版本最接近的那份。

    为什么在意：省/州编号在版本之间会重排。用 1.16 的数据去读 1.14 的存档，
    部队位置和州名会整体错位（实测过，中国的师被标到"九州"）。
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    target = _ver_tuple(save_version)
    if not target:
        return candidates[0]

    best: tuple[int, Path] | None = None
    for p in candidates:
        ver = _ver_tuple(game_dir_version(p))
        if not ver:
            continue
        span = max(len(ver), len(target))
        a = ver + (0,) * (span - len(ver))
        b = target + (0,) * (span - len(target))
        dist = sum(abs(x - y) for x, y in zip(a, b))
        if best is None or dist < best[0]:
            best = (dist, p)
    if best is None:
        return candidates[0]
    return best[1]


def resolve_paths(cfg: dict | None = None) -> dict:
    """把配置和自动探测合成一份可用路径。

    返回 {"save_dir", "save_file", "game_dir", "save_dirs_all", "game_dirs_all",
          "save_head", "game"}
    任何一项都可能是 None / 空列表，由调用方决定怎么提示。

    0.9.8 修了一个隐蔽的错位：老代码用
    `version\\s*=\\s*\"?([^\\s\"\\n]+)` 抠头 2048 字节，
    遇到 `version="Bolivar v1.14.8.6eb9 (9d66)"` 只会拿到代号 `Bolivar`，
    数字全丢 → `pick_game_dir` 直接返回候选第一个（1.16.9 的数据），
    拿 1.16 的州名去读 1.14 的存档，部队位置整体错位。
    现在统一走 save_meta.head_meta 的 version_num。
    """
    cfg = cfg or load_config()

    save_dirs: list[Path] = []
    for raw in cfg.get("save_dirs") or []:
        p = Path(raw)
        if p.is_dir():
            save_dirs.append(p)
    if not save_dirs:
        save_dirs = detect_save_dirs()

    save_file = find_best_save(save_dirs, (cfg.get("save_file") or "").strip() or None)
    save_dir = save_file.parent if save_file else None
    head = save_meta.head_meta(save_file) if save_file else {}
    game = save_meta.game_process_cached(str(cfg.get("game_exe") or "hoi4.exe"))
    blocker = find_binary_blocker(save_dirs, save_file)

    game_candidates = detect_game_dirs()
    raw_game = (cfg.get("game_dir") or "").strip()
    if raw_game and _looks_like_game_dir(Path(raw_game)):
        game_dir = Path(raw_game)
    else:
        # 解析用的是**这份存档**对应的数据目录，不是正在跑的游戏版本。
        # 拿 1.16.9 的州名去读 1.14.8 的档会整体错位；
        # 正在跑的版本只用于判断"是不是同一局"，不参与挑目录。
        game_dir = pick_game_dir(game_candidates, head.get("version_num") or "")

    return {
        "save_dir": save_dir,
        "save_file": save_file,
        "game_dir": game_dir,
        "save_dirs_all": save_dirs,
        "game_dirs_all": game_candidates,
        "save_head": head,
        "game": game,
        "binary_blocker": blocker,
    }

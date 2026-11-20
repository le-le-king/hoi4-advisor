"""世界资料库（Atlas）：把"这局游戏的世界长什么样"变成可生成、可切换的数据。

## 为什么要有这个东西

插件以前把地图知识写死在一个 `prov_px.json` 里（按**原版** `provinces.bmp`
生成）。换个大 mod（TFR 把整张地图、州划分、省份编号全换了）就整体错位：
她说"去华沙"，落点在一个毫不相干的地块上。

现在每个"世界"= 游戏版本 + 一组启用的 mod，单独生成一份资料库。代码只认
数据格式、不认具体是哪个 mod——**换 mod 只是重建一份数据，不用改代码**。

## 一个世界存什么（`data/atlas/<world_id>/`）

    meta.json        指纹、游戏版本、mod 清单、规模、警告、生成耗时
    provinces.json   pid -> {"x", "y", "area", "state"}   （地图像素坐标）
    states.json      sid -> {"name", "provs": [...]}
    countries.json   tag -> 中文名
    cities.json      pid -> 城市名（"柏林" 这种，用户真正会说的词）
    adjacency.json   pid -> [邻省]

## mod 覆盖规则（照 HOI4 自己的语义来）

- **普通文件**：同一个相对路径，后加载的 mod 覆盖先加载的、覆盖原版。
- **`replace_path="xxx"`**：这个目录整个不用原版的，只从 mod 里找。

TFR 就是 `replace_path="map"` + `replace_path="history/states"`，所以它的
地图和州划分全用自己的，而 `localisation` 没被替换、原版州名继续生效。

## 不长第三只眼

只用标准库：`provinces.bmp` 自己解（24bpp 未压缩，格式简单），不依赖 PIL——
宿主那套 Python 环境里有没有装 Pillow 不该成为插件能不能用的前提。
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "ModInfo",
    "WorldFiles",
    "all_mods",
    "build_world",
    "discover_mods",
    "discover_mods_report",
    "game_user_dir",
    "list_worlds",
    "load_world",
    "mod_from_descriptor",
    "mods_by_names",
    "read_world_meta",
    "world_fingerprint",
    "write_world",
]

# 降采样步长：重心精度 ±2px，够点中一个省，扫描快 4 倍。
STEP = 2

_SUFFIX_LANG = ("simp_chinese", "english", "braz_por", "french", "german",
                "polish", "russian", "spanish", "japanese", "korean")

# `state = { id = 1 ... }` 里我们要的三样
_RE_STATE_ID = re.compile(r"(?m)^\s*id\s*=\s*(\d+)")
_RE_STATE_NAME = re.compile(r'(?m)^\s*name\s*=\s*"([^"]*)"\s*(?:#\s*(.*))?$')
_RE_STATE_PROVS = re.compile(r"provinces\s*=\s*\{([^}]*)\}", re.S)
_RE_STATE_OPEN = re.compile(r"(?m)^\s*state\s*=\s*\{")

# 两种写法都得认：
#   KEY:0 "值"   （较新的 yml）
#   KEY: "值"    （老的 state_names 常见）
_RE_LOC_KEY = re.compile(
    r'^\s*([A-Za-z0-9_.\-]+)\s*:\s*\d*\s*"((?:[^"\\]|\\.)*)"\s*(?:#.*)?$'
)
_RE_FMT = re.compile(r"§[A-Za-z0-9!+\-=*%]|£[A-Za-z0-9_]+|\$[A-Za-z0-9_.\-]+\$")
_RE_TAG = re.compile(r"^[A-Z]{3}$")
_RE_STATEKEY = re.compile(r"^STATE_(\d+)$")
_RE_VP = re.compile(r"^VICTORY_POINTS_(\d+)$")


# ────────────────────────────── mod 发现 ──────────────────────────────


@dataclass
class ModInfo:
    """一个读得到的 mod。"""

    name: str
    version: str
    path: Path
    mod_file: Path
    replace_paths: list[str] = field(default_factory=list)
    enabled: bool = True          # 启动器当前启用的；all_mods() 里会有 False 的
    remote_file_id: str = ""      # 创意工坊 id，报错和兜底找目录时用得上

    def as_dict(self) -> dict:
        return {"name": self.name, "version": self.version,
                "path": str(self.path), "replace_paths": self.replace_paths,
                "enabled": self.enabled, "mod_file": str(self.mod_file),
                "remote_file_id": self.remote_file_id}


def game_user_dir(game_dir: Path) -> Path | None:
    """游戏的用户目录（放 dlc_load.json / mod/ 的那个）。

    便携版 HOI4 把用户目录放在安装目录里面：
        <安装目录>/Paradox Interactive/Hearts of Iron IV/
    标准安装则在 `文档/Paradox Interactive/Hearts of Iron IV/`。
    """
    cand = [game_dir / "Paradox Interactive" / "Hearts of Iron IV",
            Path.home() / "Documents" / "Paradox Interactive" / "Hearts of Iron IV"]
    for c in cand:
        if (c / "dlc_load.json").is_file() or (c / "mod").is_dir():
            return c
    return cand[0]


def _parse_mod_descriptor(path: Path) -> dict:
    """读 `.mod` 文件里的 name / version / path / replace_path。"""
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return {}
    out: dict = {"replace_path": []}
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r'^(name|version|path|tags)\s*=\s*"([^"]*)"', line)
        if m:
            out[m.group(1)] = m.group(2)
            continue
        m = re.match(r'^replace_path\s*=\s*"([^"]*)"', line)
        if m:
            out["replace_path"].append(m.group(1).replace("\\", "/").strip("/"))
    return out


def _resolve_mod_dir(desc: dict, mod_file: Path,
                     user_dir: Path | None) -> tuple[Path | None, str]:
    """把 `.mod` 里的 `path=` 解成一个真实存在的目录。返回 (目录, 失败原因)。

    三个坑，都真机上踩过：

    1. **相对路径按"用户目录"解，不是进程工作目录。**
       `yui_command.mod` 里写的是 `path="mod/yui_command"`（插件自己也这么发），
       Paradox 的惯例是相对 `<用户目录>`。按 cwd 解的话，插件跑在宿主目录里，
       这个 mod 会被静默丢掉（真机实测：解到 `Q:\\N.E.K.O...\\mod\\yui_command`，
       不存在）。
    2. **盘符/目录搬过家**：绝对路径指的盘不在（换个盘装游戏）→ 用
       "用户目录 + 相对尾部" 再试一次。
    3. 目录名就是 `.mod` 同名（没有 `path=` 时的惯例）。
    """
    raw = (desc.get("path") or "").strip()
    cands: list[Path] = []
    if raw:
        p = Path(raw.replace("\\", "/"))
        if p.is_absolute():
            cands.append(p)
            # 换盘兜底：`D:/Games/HOI4/mod/xxx` → 保留 `mod/xxx` 接到用户目录下
            parts = p.parts
            if len(parts) > 2:
                cands.append((user_dir / Path(*parts[-2:]))
                             if user_dir else Path(*parts[-2:]))
            cands.append((user_dir / "mod" / p.name) if user_dir else p)
        else:
            if user_dir:
                cands.append(user_dir / p)
            cands.append(mod_file.parent / p)
    cands.append(mod_file.with_suffix(""))
    if user_dir:
        cands.append(user_dir / "mod" / mod_file.stem)
    if user_dir:
        cands.append(user_dir)
    for c in cands:
        try:
            if c.is_dir():
                return c, ""
        except OSError:
            continue
    return None, (f"path= 指的目录找不到：{raw!r}" if raw
                  else "没写 path=，也没有同名目录")


def mod_from_descriptor(mod_file: Path,
                        user_dir: Path | None = None) -> ModInfo | None:
    """从一个 `.mod` 文件建 ModInfo。给「手动指定某个 mod」和探针用。"""
    mod_file = Path(mod_file)
    desc = _parse_mod_descriptor(mod_file) if mod_file.is_file() else {}
    if user_dir is None:
        # `.mod` 惯例放在 `<用户目录>/mod/` 下
        user_dir = mod_file.parent.parent if mod_file.parent.name == "mod" else None
    mpath, _why = _resolve_mod_dir(desc, mod_file, user_dir)
    if mpath is None:
        return None
    return ModInfo(
        name=desc.get("name") or mpath.name,
        version=desc.get("version") or "",
        path=mpath,
        mod_file=mod_file,
        replace_paths=list(desc.get("replace_path") or []),
        remote_file_id=str(desc.get("remote_file_id") or ""),
    )


def _enabled_mod_files(user: Path) -> list[Path]:
    """`dlc_load.json` 里启用的那些 `.mod`（按启用顺序，后加载的排后面）。"""
    load = user / "dlc_load.json"
    rels: list[str] = []
    if load.is_file():
        try:
            raw = json.loads(load.read_text(encoding="utf-8-sig"))
            rels = [str(x) for x in (raw.get("enabled_mods") or [])]
        except (OSError, ValueError):
            rels = []
    out: list[Path] = []
    for rel in rels:
        f = user / rel.replace("\\", "/")
        if not f.is_file():
            alt = Path(rel)
            if alt.is_file():
                f = alt
        if f.is_file():
            out.append(f)
        else:
            out.append(f)          # 留给调用方报"文件找不到"
    return out


def discover_mods_report(game_dir: Path) -> dict:
    """启用的 mod + **读不进来的那些及原因**。

    0.11.1 加 `skipped`：以前路径解不出来就 `continue` 静默跳过，
    用户启用了 5 个 mod、世界只按 4 个建出来，界面上一个字都不说。
    「差在哪」必须说出来（世界 id 是按 mod 集合算的，少一个就是另一个世界）。
    """
    user = game_user_dir(game_dir)
    if user is None:
        return {"ok": False, "user_dir": "", "mods": [],
                "skipped": [{"name": "", "mod_file": "",
                             "reason": "找不到用户目录（没有 dlc_load.json / mod/）"}]}
    mods: list[ModInfo] = []
    skipped: list[dict] = []
    for f in _enabled_mod_files(user):
        if not f.is_file():
            skipped.append({"name": f.stem, "mod_file": str(f),
                            "reason": "dlc_load.json 里启用了，但这个 .mod 文件不在"})
            continue
        desc = _parse_mod_descriptor(f)
        mpath, why = _resolve_mod_dir(desc, f, user)
        if mpath is None:
            skipped.append({"name": desc.get("name") or f.stem,
                            "mod_file": str(f), "reason": why})
            continue
        mods.append(ModInfo(
            name=desc.get("name") or mpath.name,
            version=desc.get("version") or "",
            path=mpath, mod_file=f,
            replace_paths=list(desc.get("replace_path") or []),
            remote_file_id=str(desc.get("remote_file_id") or ""),
        ))
    return {"ok": True, "user_dir": str(user), "mods": mods,
            "skipped": skipped}


def discover_mods(game_dir: Path) -> list[ModInfo]:
    """按 `dlc_load.json` 里启用的顺序返回 mod 列表（后加载的排在后面）。

    读不进来的那些看 `discover_mods_report()`。
    """
    return list(discover_mods_report(game_dir)["mods"])


def all_mods(game_dir: Path) -> list[ModInfo]:
    """用户目录里**所有**能读到的 mod，`enabled` 标出启动器当前启用没有。

    为什么要有这个：`dlc_load.json` 只列**当前启用**的。想给 TFR 单独建一份
    世界数据，就得先在启动器里把 TFR 打开、建完再关掉——纯粹是折腾。
    有了全量清单，面板可以让用户直接在插件里勾哪几个 mod 参与建世界。
    """
    user = game_user_dir(game_dir)
    if user is None:
        return []
    enabled_files = {f.resolve() for f in _enabled_mod_files(user) if f.is_file()}
    mod_dir = user / "mod"
    out: list[ModInfo] = []
    if not mod_dir.is_dir():
        return out
    for f in sorted(mod_dir.glob("*.mod")):
        desc = _parse_mod_descriptor(f)
        mpath, _why = _resolve_mod_dir(desc, f, user)
        if mpath is None:
            continue
        try:
            is_on = f.resolve() in enabled_files
        except OSError:
            is_on = False
        out.append(ModInfo(
            name=desc.get("name") or mpath.name,
            version=desc.get("version") or "",
            path=mpath, mod_file=f,
            replace_paths=list(desc.get("replace_path") or []),
            enabled=is_on,
            remote_file_id=str(desc.get("remote_file_id") or ""),
        ))
    return out


def mods_by_names(game_dir: Path, names: list[str]) -> tuple[list[ModInfo], list[str]]:
    """按名字从**全量清单**里挑 mod（给"我要读这几个"用）。返回 (选中的, 没找到的)。"""
    pool = all_mods(game_dir)
    by_name = {m.name: m for m in pool}
    by_stem = {m.mod_file.stem: m for m in pool}
    picked: list[ModInfo] = []
    miss: list[str] = []
    for raw in names:
        key = str(raw).strip()
        if not key:
            continue
        m = by_name.get(key) or by_stem.get(key)
        if m is None:
            low = key.lower()
            hit = [x for x in pool if low in x.name.lower()
                   or low in x.mod_file.stem.lower()]
            m = hit[0] if len(hit) == 1 else None
        if m is None:
            miss.append(key)
            continue
        m.enabled = True
        picked.append(m)
    return picked, miss


# ────────────────────────────── 文件视图 ──────────────────────────────


class WorldFiles:
    """按 HOI4 的覆盖规则拼出来的只读文件视图。"""

    def __init__(self, game_dir: Path, mods: list[ModInfo]) -> None:
        self.game_dir = Path(game_dir)
        # 先原版，再按加载顺序叠 mod；查找时从后往前
        self.layers: list[Path] = [self.game_dir] + [m.path for m in mods]
        self.mod_layers: list[Path] = [m.path for m in mods]
        self.replace_paths: set[str] = set()
        for m in mods:
            self.replace_paths.update(m.replace_paths)

    def _replaced(self, rel: str) -> bool:
        return any(rel == rp or rel.startswith(rp + "/") for rp in self.replace_paths)

    def find(self, rel: str) -> Path | None:
        """找一个文件。"""
        rel = rel.replace("\\", "/").lstrip("/")
        if self._replaced(rel):
            for layer in reversed(self.mod_layers):
                p = layer / rel
                if p.is_file():
                    return p
            return None
        for layer in reversed(self.layers):
            p = layer / rel
            if p.is_file():
                return p
        return None

    def find_dir(self, rel: str) -> Path | None:
        """找一个目录（同样的覆盖规则）。"""
        rel = rel.replace("\\", "/").strip("/")
        if self._replaced(rel):
            for layer in reversed(self.mod_layers):
                p = layer / rel
                if p.is_dir():
                    return p
            return None
        for layer in reversed(self.layers):
            p = layer / rel
            if p.is_dir():
                return p
        return None

    def glob(self, rel_dir: str, pattern: str) -> list[Path]:
        """列一个目录下的文件，同名按覆盖规则只留一份。

        返回顺序稳定（按相对路径排序），方便测试对比。
        """
        rel_dir = rel_dir.replace("\\", "/").strip("/")
        seen: dict[str, Path] = {}
        layers = list(reversed(self.mod_layers)) if self._replaced(rel_dir) else list(reversed(self.layers))
        for layer in layers:
            d = layer / rel_dir
            if not d.is_dir():
                continue
            for f in sorted(d.glob(pattern)):
                key = f.name
                if key not in seen:
                    seen[key] = f
        return [seen[k] for k in sorted(seen)]

    def describe(self) -> str:
        more = f" + {len(self.mod_layers)} 个 mod" if self.mod_layers else ""
        return f"原版{more}（替换目录 {len(self.replace_paths)} 个）"


# ────────────────────────────── 位图 / 解析 ──────────────────────────────


def _clean_loc(value: str) -> str:
    v = (value.replace("\\n", " ").replace("\\t", " ")
         .replace('\\"', '"').replace("\\\\", "\\"))
    v = _RE_FMT.sub("", v)
    return re.sub(r"\s+", " ", v).strip()


def _scan_map(path: Path, colors: dict[int, int], land: set[int],
              progress=None) -> dict:
    """一次扫完地图：省质心 / 面积 / bbox + 陆地邻接。

    **全分辨率**扫，不降采样——邻接必须逐像素算，漏掉一个像素就可能把两块
    本该相连的地判成不相连，调兵会绕远路甚至报「没有陆路」。

    自己解 24bpp 未压缩 BMP：文件头 14 字节 + 信息头 40 字节，像素自下而上、
    每行 4 字节对齐、BGR 顺序。不用 PIL。

    返回 {w, h, centers: {pid: [x, y, area]}, boxes: {...}, edges: set[(a,b)]}
    """
    with path.open("rb") as fh:
        head = fh.read(54)
        if len(head) < 54 or head[:2] != b"BM":
            raise ValueError(f"{path.name} 不是 BMP")
        off = struct.unpack("<I", head[10:14])[0]
        w, h = struct.unpack("<ii", head[18:26])
        bpp = struct.unpack("<H", head[28:30])[0]
        if bpp != 24:
            raise ValueError(f"{path.name} 是 {bpp}bpp，只支持 24bpp")
        row = ((w * 3 + 3) // 4) * 4

        sx: dict[int, int] = {}
        sy: dict[int, int] = {}
        cnt: dict[int, int] = {}
        box: dict[int, list[int]] = {}
        edges: set[tuple[int, int]] = set()
        prev: list[int] = [0] * w
        get = colors.get
        tick = max(1, h // 80)

        for y in range(h):
            fh.seek(off + (h - 1 - y) * row)
            raw = fh.read(row)
            if len(raw) < w * 3:
                break
            # BMP 像素是 BGR 顺序，definition 的颜色是 RGB——拼的时候别忘了倒过来
            ids = [get((c << 16) | (b << 8) | a, 0)
                   for a, b, c in zip(raw[0::3], raw[1::3], raw[2::3])]

            left = 0
            for x in range(w):
                pid = ids[x]
                if pid:
                    sx[pid] = sx.get(pid, 0) + x
                    sy[pid] = sy.get(pid, 0) + y
                    cnt[pid] = cnt.get(pid, 0) + 1
                    bx = box.get(pid)
                    if bx is None:
                        box[pid] = [x, y, x, y]
                    else:
                        if x < bx[0]:
                            bx[0] = x
                        if y < bx[1]:
                            bx[1] = y
                        if x > bx[2]:
                            bx[2] = x
                        if y > bx[3]:
                            bx[3] = y
                    # 水平邻接（和左边那格）
                    if left and left != pid:
                        edges.add((pid, left) if pid < left else (left, pid))
                    # 垂直邻接（和上一行同一列）
                    above = prev[x]
                    if above and above != pid:
                        edges.add((pid, above) if pid < above else (above, pid))
                prev[x] = pid
                left = pid

            if progress and y % tick == 0:
                progress(0.08 + 0.55 * y / max(1, h), "扫地图")

    # 只留陆地之间的边。海洋也是省，不滤掉的话调兵路线会穿海。
    if land:
        edges = {(a, b) for a, b in edges if a in land and b in land}

    if progress:
        progress(0.66, "算邻接")
    centers = {pid: [sx[pid] // n, sy[pid] // n, n] for pid, n in cnt.items()}
    return {"w": w, "h": h, "centers": centers, "boxes": box, "edges": edges}


def _edges_to_adjacency(edges: set[tuple[int, int]]) -> dict[str, list[int]]:
    """边集合 → {省id: [邻省...]}，两边都建、按省号排序，方便对比和 diff。"""
    adj: dict[int, set[int]] = {}
    for a, b in edges:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    return {str(k): sorted(v) for k, v in sorted(adj.items())}


def _source_stamp(path: Path) -> dict:
    """记一个文件的 mtime + size，用来判断资料库是不是过期了。"""
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return {"path": str(p), "missing": True}
    return {"path": str(p), "mtime": int(st.st_mtime), "size": st.st_size}


def _read_definition(path: Path) -> tuple[dict[int, int], set[int]]:
    """definition.csv → (颜色→省id, 陆地省id 集合)。

    第 5 列是类型：`land` / `sea` / `lake`。邻接只认陆地——不然调兵路线会
    穿海直线过去，玩家一眼就能看出不对。
    """
    colors: dict[int, int] = {}
    land: set[int] = set()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return colors, land
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split(";")
        if len(parts) < 4:
            continue
        try:
            pid, r, g, b = int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
        except ValueError:
            continue
        colors[(r << 16) | (g << 8) | b] = pid
        if len(parts) >= 5 and parts[4].strip().lower() == "land":
            land.add(pid)
    return colors, land


def _iter_state_bodies(text: str):
    """按大括号配平切出每个 `state = { ... }` 的内容。

    不能靠非贪婪正则：`history = { ... }` 里有嵌套大括号，非贪婪会在内层
    `}` 就停下，后面的 `provinces` 整段被丢掉（0.10.0 第一版就踩了这个，
    结果每个州的省列表都是空的）。
    """
    for m in _RE_STATE_OPEN.finditer(text):
        i = text.find("{", m.start())
        if i < 0:
            continue
        depth = 0
        for j in range(i, len(text)):
            c = text[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    yield text[i + 1:j]
                    break


def count_states_in(text: str) -> int:
    """数一个文件里有几个州块（给预览用，比解析便宜）。"""
    return len(_RE_STATE_OPEN.findall(text))


def _parse_states(files: list[Path], progress=None) -> dict[int, dict]:
    """history/states/*.txt → {sid: {name_key, comment, provs}}。"""
    out: dict[int, dict] = {}
    for n, f in enumerate(files):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for body in _iter_state_bodies(text):
            m_id = _RE_STATE_ID.search(body)
            if not m_id:
                continue
            sid = int(m_id.group(1))
            m_name = _RE_STATE_NAME.search(body)
            key = (m_name.group(1) if m_name else "") or f"STATE_{sid}"
            comment = (m_name.group(2) if m_name and m_name.group(2) else "").strip()
            provs: list[int] = []
            m_p = _RE_STATE_PROVS.search(body)
            if m_p:
                for tok in re.split(r"[\s,]+", m_p.group(1).strip()):
                    if tok.isdigit():
                        provs.append(int(tok))
            out[sid] = {"key": key, "comment": comment, "provs": provs}
        if progress and files:
            progress(0.7 + 0.1 * (n + 1) / len(files), "解析州划分")
    return out


def _read_localisation(files: list[Path], limit: int = 400) -> dict[str, str]:
    """读 yml，只留我们真用得上的 key（国名 / 州名 / 城市名）。"""
    out: dict[str, str] = {}
    for f in files:
        try:
            text = f.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if len(line) > limit:
                continue
            m = _RE_LOC_KEY.match(line)
            if not m:
                continue
            k = m.group(1)
            if not (_RE_TAG.match(k) or _RE_STATEKEY.match(k) or _RE_VP.match(k)
                    or k.endswith("_DEF") or k.endswith("_ADJ")):
                continue
            v = _clean_loc(m.group(2))
            if v:
                out.setdefault(k, v)
    return out


# ────────────────────────────── 生成 / 读写 ──────────────────────────────


def world_fingerprint(game_dir: Path, mods: list[ModInfo], version: str = "") -> str:
    """世界的指纹：游戏目录 + 版本 + 每个 mod 的名字/版本/路径。

    只对 `.mod` 描述文件算 hash 不做全量校验（一个 mod 几千个文件，太慢）；
    改 mod 内容想让它重建，删掉资料库重生成即可。
    """
    h = hashlib.sha256()
    h.update(str(Path(game_dir).resolve()).lower().encode("utf-8", "replace"))
    h.update(version.encode("utf-8"))
    for m in mods:
        h.update(m.name.lower().encode("utf-8", "replace"))
        h.update(m.version.encode("utf-8", "replace"))
        h.update(str(m.path).lower().encode("utf-8", "replace"))
    return h.hexdigest()[:8]


def _short_tag(mods: list[ModInfo]) -> str:
    if not mods:
        return ""
    names = []
    for m in mods:
        raw = re.sub(r"[^0-9A-Za-z]+", "", m.name)
        names.append((raw or "mod")[:6].lower())
    return "_".join(names)[:24]


def build_world(game_dir: Path, mods: list[ModInfo], version: str = "",
                progress=None) -> dict:
    """生成一个世界的资料库内容（不落盘）。"""
    t0 = time.perf_counter()
    files = WorldFiles(game_dir, mods)
    warnings: list[str] = []
    # 指纹：游戏目录 + 版本 + 每个 mod 的名字/版本/路径。写进 meta 后，世界 id
    # 也带上它——两个同名不同版本的 mod 不会互相覆盖。
    fp = world_fingerprint(game_dir, mods, version)

    def pct(p: float, phase: str) -> None:
        if progress:
            progress(max(0.0, min(1.0, p)), phase)

    pct(0.02, "合并文件视图")

    csv_path = files.find("map/definition.csv")
    if csv_path is None:
        raise ValueError("找不到 map/definition.csv（游戏目录指错，或者 mod 里没有地图）")
    colors, land = _read_definition(csv_path)
    if not colors:
        raise ValueError("definition.csv 读不出省份颜色")

    bmp = files.find("map/provinces.bmp")
    provinces: dict[str, dict] = {}
    map_w = map_h = 0
    px: dict[int, list[int]] = {}          # 没有 bmp 时保持空，别让后面 NameError
    adjacency: dict[str, list[int]] = {}
    if bmp is None:
        warnings.append("找不到 provinces.bmp，坐标和邻接都没生成（调兵会不准）")
        pct(0.6, "跳过地图")
    else:
        pct(0.08, "扫地图")
        scan = _scan_map(bmp, colors, land, progress=lambda p, ph: pct(p, ph))
        map_w, map_h = scan["w"], scan["h"]
        px = scan["centers"]
        adjacency = _edges_to_adjacency(scan["edges"])

    state_files = files.glob("history/states", "*.txt")
    states_raw = _parse_states(state_files, progress=lambda p, ph: pct(p, ph))
    if not states_raw:
        warnings.append("history/states 里没解析出州，州名会退化成「州<id>」")

    # 省 -> 州
    prov_state: dict[int, int] = {}
    for sid, st in states_raw.items():
        for pid in st["provs"]:
            prov_state.setdefault(pid, sid)

    # 名字表：简中优先，没有就英文
    loc: dict[str, str] = {}
    for lang in ("simp_chinese", "english"):
        loc_files = files.glob(f"localisation/{lang}", "*.yml")
        if loc_files:
            loc.update(_read_localisation(loc_files))
        if loc:
            break
    pct(0.85, "读名字")

    for pid in px:
        ent = {"x": px[pid][0], "y": px[pid][1],
               "area": px[pid][2], "state": prov_state.get(pid, 0)}
        provinces[str(pid)] = ent

    states: dict[str, dict] = {}
    missing_names = 0
    for sid, st in states_raw.items():
        name = loc.get(st["key"]) or ""
        if not name and st["comment"]:
            # TFR 这类 mod 不翻译州名：`name = "STATE_1" #Corsica`
            name = st["comment"]
        if not name:
            missing_names += 1
            name = f"州{sid}"
        states[str(sid)] = {"name": name, "provs": st["provs"]}
    if missing_names:
        warnings.append(f"{missing_names} 个州在汉化文件里没有名字，已用地图文件里的注释兜底")

    countries: dict[str, str] = {}
    for k, v in loc.items():
        if _RE_TAG.match(k):
            countries[k] = v
        elif k.endswith("_DEF") or k.endswith("_ADJ"):
            tag = k[:-4]
            if _RE_TAG.match(tag):
                countries.setdefault(tag, v)

    cities: dict[str, str] = {}
    for k, v in loc.items():
        m = _RE_VP.match(k)
        if m:
            cities[m.group(1)] = v

    # 记下这次读的关键文件：下次打开面板时好判断"这份是不是过期了"
    sources = [_source_stamp(csv_path)]
    if bmp is not None:
        sources.append(_source_stamp(bmp))
    states_dir = files.find_dir("history/states")
    if states_dir is not None:
        sources.append(_source_stamp(states_dir))
    for m in mods:
        sources.append(_source_stamp(m.mod_file))

    pct(1.0, "完成")
    return {
        "meta": {
            "game_dir": str(Path(game_dir).resolve()),
            "game_version": version,
            "fingerprint": fp,
            "mods": [m.as_dict() for m in mods],
            "mod_names": [m.name for m in mods],
            "files": files.describe(),
            "map_w": map_w, "map_h": map_h,
            "provinces": len(provinces),
            "states": len(states),
            "countries": len(countries),
            "cities": len(cities),
            "adjacency": len(adjacency),
            "sources": sources,
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "build_seconds": round(time.perf_counter() - t0, 1),
            "warnings": warnings,
        },
        "provinces": provinces,
        "states": states,
        "countries": countries,
        "cities": cities,
        "adjacency": adjacency,
    }


def world_id_for(meta: dict) -> str:
    """世界 id：一眼能看出是哪套东西。"""
    ver = (meta.get("game_version") or "unknown").replace(".", "_")
    mods = meta.get("mods") or []
    tag = "_".join(
        re.sub(r"[^0-9A-Za-z]+", "", str(m.get("name") or ""))[:6].lower() or "mod"
        for m in mods
    )[:24]
    base = f"{tag}_{ver}" if tag else f"vanilla_{ver}"
    return f"{base}_{meta.get('fingerprint') or ''}".strip("_")


def _json_dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def write_world(root: Path, data: dict) -> Path:
    """把生成结果落到 root/<world_id>/。原子写：先写临时目录再改名。"""
    meta = dict(data["meta"])
    wid = world_id_for(meta)
    out = Path(root) / wid
    tmp = Path(root) / f".tmp_{wid}"
    if tmp.exists():
        for f in tmp.iterdir():
            f.unlink(missing_ok=True)
        tmp.rmdir()
    tmp.mkdir(parents=True, exist_ok=True)
    for name, key in (("provinces.json", "provinces"), ("states.json", "states"),
                      ("countries.json", "countries"), ("cities.json", "cities"),
                      ("adjacency.json", "adjacency")):
        (tmp / name).write_text(_json_dump(data.get(key) or {}), encoding="utf-8")
    meta["id"] = wid
    (tmp / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    if out.exists():
        for f in out.iterdir():
            f.unlink(missing_ok=True)
        out.rmdir()
    tmp.rename(out)
    return out


def list_worlds(root: Path) -> list[dict]:
    """资料库里现有的世界，按生成时间倒序。"""
    root = Path(root)
    if not root.is_dir():
        return []
    out: list[dict] = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        meta = read_world_meta(d)
        if meta:
            out.append(meta)
    out.sort(key=lambda m: str(m.get("built_at") or ""), reverse=True)
    return out


def read_world_meta(d: Path) -> dict | None:
    p = Path(d) / "meta.json"
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    raw.setdefault("id", Path(d).name)
    return raw


def load_world(root: Path, world_id: str) -> dict | None:
    """完整读一个世界（省/州/国名/城市）。"""
    d = Path(root) / world_id
    meta = read_world_meta(d)
    if not meta:
        return None
    out = {"meta": meta}
    for name, key in (("provinces", "provinces"), ("states", "states"),
                      ("countries", "countries"), ("cities", "cities"),
                      ("adjacency", "adjacency")):
        p = d / f"{name}.json"
        if not p.is_file():
            out[key] = {}
            continue
        try:
            out[key] = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out[key] = {}
    return out
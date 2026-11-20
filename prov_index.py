"""省 / 州 → 地图位图像素坐标。

数据来自 `prov_px.json`，由 `tools/build_prov_index.py` 离线生成：
拿 provinces.bmp 逐像素反查 definition.csv 的颜色，算每个省的重心。

为什么坐标要预生成而不是运行时算：
- 算一遍要扫 1100 万像素，插件跑在宿主进程里，不能卡这个
- 宿主环境的 Python 不保证有 PIL
- 地图是只读的，除非换游戏版本，否则不用重算

版本很重要：省编号在游戏版本之间会重排（1.13 的 4001 和 1.14 的 4001
可能根本不是同一个地方）。所以索引带版本号，和存档版本对不上就报错，
不能拿旧索引硬点——点错省比不点更糟。
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = ["ProvIndex", "load"]

INDEX_NAME = "prov_px.json"


class ProvIndex:
    """省坐标查询。所有坐标都是 5632x2048 那套位图像素。"""

    def __init__(self, data: dict, prov_to_state: dict[int, int] | None = None) -> None:
        px_raw = data.get("px") or {}
        area_raw = data.get("area") or {}
        self._px: dict[int, tuple[int, int]] = {}
        for k, v in px_raw.items():
            try:
                self._px[int(k)] = (int(v[0]), int(v[1]))
            except (TypeError, ValueError, IndexError):
                continue
        self._area: dict[int, int] = {}
        for k, v in area_raw.items():
            try:
                self._area[int(k)] = int(v)
            except (TypeError, ValueError):
                continue
        self.map_w = int(data.get("map_w") or 5632)
        self.map_h = int(data.get("map_h") or 2048)
        self.version = str(data.get("game_version") or "")
        # 省 id -> 城市名（如 6521 -> 柏林）。用户说的是城市名，不是州名。
        self._names: dict[int, str] = {}
        for k, v in (data.get("names") or {}).items():
            try:
                self._names[int(k)] = str(v)
            except (TypeError, ValueError):
                continue
        self._state_px: dict[int, tuple[int, int]] | None = None
        self._prov_to_state = prov_to_state

    # ── 查询 ──────────────────────────────────────

    def __len__(self) -> int:
        return len(self._px)

    def px(self, province_id: int) -> tuple[int, int] | None:
        return self._px.get(int(province_id))

    def area(self, province_id: int) -> int:
        return self._area.get(int(province_id), 0)

    def has(self, province_id: int) -> bool:
        return int(province_id) in self._px

    def name(self, province_id: int) -> str:
        """省里的城市名（柏林/华沙这类）。没有就返回空串。"""
        return self._names.get(int(province_id), "")

    def find_by_name(self, text: str) -> int | None:
        """按城市名找省。先精确，再唯一包含。"""
        key = (text or "").strip()
        if not key:
            return None
        exact: list[int] = []
        fuzzy: list[int] = []
        for pid, name in self._names.items():
            if name == key:
                exact.append(pid)
            elif key in name:
                fuzzy.append(pid)
        if len(exact) == 1:
            return exact[0]
        if not exact and len(fuzzy) == 1:
            return fuzzy[0]
        # 多个同名（HOI4 里重名城市不少），交给调用方处理
        return min(exact) if exact else (min(fuzzy) if fuzzy else None)

    def city_names(self) -> dict[int, str]:
        return dict(self._names)

    def nearest(self, bx: float, by: float, tol: float = 45.0) -> int:
        """位图像素坐标 -> 最近的省。找出鼠标指着哪个省用。

        只有省的中心点，没有多边形，所以是"最近邻"而不是"点在省内"。
        缩得越远省越小、中心点越密，反而越准；tol 挡住"指着海面"那种情况。
        """
        best, bd = 0, float(tol) * float(tol)
        for pid, (x, y) in self._px.items():
            dx = x - bx
            dy = y - by
            d = dx * dx + dy * dy
            if d < bd:
                best, bd = pid, d
        return best

    def state_px(self, state_id: int) -> tuple[int, int] | None:
        """州的代表点：州内面积最大的那个省的重心。

        为什么不用"所有省的平均"：沿海州经常拖着一堆小岛，平均值会落到海里。
        取面积最大的省，落点一定在陆地主体上。
        """
        if self._prov_to_state is None:
            return None
        if self._state_px is None:
            best: dict[int, tuple[int, int, int]] = {}
            for pid, sid in self._prov_to_state.items():
                pxy = self._px.get(pid)
                if pxy is None:
                    continue
                a = self._area.get(pid, 0)
                cur = best.get(sid)
                if cur is None or a > cur[2]:
                    best[sid] = (pxy[0], pxy[1], a)
            self._state_px = {sid: (v[0], v[1]) for sid, v in best.items()}
        return self._state_px.get(int(state_id))

    def update_prov_to_state(self, mapping: dict[int, int]) -> None:
        """存档解析出省→州映射后接进来，重新算州坐标。"""
        self._prov_to_state = mapping
        self._state_px = None

    # ── 版本 ──────────────────────────────────────

    def version_ok(self, save_version: str) -> bool:
        """索引版本和存档版本是否一致。

        两边都拿不到版本号时放行（宁可试一把，也不要因为读不到版本就废掉功能）；
        但只要有一边能读到，就必须一致。
        """
        a = (self.version or "").strip()
        b = (save_version or "").strip()
        if not a or not b:
            return True
        return a == b

    def describe(self) -> str:
        return f"省坐标索引 {len(self._px)} 省 / 地图 {self.map_w}x{self.map_h} / 版本 {self.version or '未知'}"


_CACHE: dict = {"wid": None, "data": None}


def _read_legacy(root: Path | None) -> dict | None:
    base = Path(root) if root else Path(__file__).resolve().parent
    path = base / INDEX_NAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _from_current_world() -> dict | None:
    """把当前世界资料库转成 ProvIndex 认的结构。

    名字表（省 → 城市名）资料库里还没有，由 load() 从旧索引补。
    """
    try:
        from . import atlas as _atlas
        from . import panel
        wid = panel.current_world_id()
    except Exception:
        return None
    if not wid:
        return None
    d = panel.world_root() / wid
    p = d / "provinces.json"
    if not p.is_file():
        return None
    try:
        provs = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(provs, dict) or not provs:
        return None
    meta = _atlas.read_world_meta(d) or {}
    px: dict[str, list[int]] = {}
    area: dict[str, int] = {}
    for k, v in provs.items():
        try:
            px[k] = [int(v.get("x") or 0), int(v.get("y") or 0)]
            area[k] = int(v.get("area") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
    if not px:
        return None
    return {
        "px": px,
        "area": area,
        "map_w": meta.get("map_w") or 5632,
        "map_h": meta.get("map_h") or 2048,
        "game_version": meta.get("game_version") or "",
        "_world": wid,
    }


def load(root: Path | None = None) -> ProvIndex | None:
    """读坐标索引：优先用当前世界资料库（跟着 mod 走），
    没有资料库就退回插件自带的原版索引。"""
    legacy = _read_legacy(root)
    data = _from_current_world()
    if data is not None:
        # 名字表暂时还只有旧索引有，补上，别让城市名解析退化
        if legacy and legacy.get("names"):
            data["names"] = legacy["names"]
        wid = data.get("_world") or ""
        if _CACHE["wid"] != wid:
            _CACHE["data"] = data
            _CACHE["wid"] = wid
        else:
            data = _CACHE["data"]
    else:
        data = legacy
    if not isinstance(data, dict) or not data.get("px"):
        return None
    return ProvIndex(data)

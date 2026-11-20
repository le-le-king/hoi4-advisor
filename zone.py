"""看一片区域（海岸线/边境/某国某侧）：把「欧洲西部海岸线」这种说法落到具体省份，
再统计那片区域上谁有几个师、哪段空着（0.9.15）。

## 区域怎么解析（三级，绝不猜）

1. **内置区域表**：`欧洲西部海岸线`、`北海海岸`、`波罗的海`、`地中海`…… 每个
   区域绑定一组国家 tag（tag 稳定，显示名走 country_names.json）。
2. **国家 + 海岸**：`法国海岸`、`德国的海岸线` → 该国的**核心省 ∩ 沿海省**。
3. **地名**：`诺曼底`、`布雷斯特` → places.resolve 拿到省，再取它和邻接。

`沿海省` 来自 `assets/coastal.json.gz`（由 map/definition.csv 第 6 列生成：
6115 奥斯陆=true、6521 柏林=false，2333 个省）。

## 为什么用核心省而不用控制图

控制图一次 20 秒，看海岸线不需要它：**沿海省是谁本土的**是静态数据，
谁在上面有兵是实时分布表。两样都快。
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

__all__ = ["ZoneError", "Zone", "REGIONS"]

# 区域名 → 国家 tag 集合。说法覆盖用户常用的几种（都是 tag，不随汉化变）
REGIONS: dict[str, tuple[str, ...]] = {
    "欧洲西部海岸": ("FRA", "BEL", "HOL"),
    "西欧海岸": ("FRA", "BEL", "HOL"),
    "英吉利海峡": ("FRA", "ENG", "BEL", "HOL"),
    "加来海峡": ("FRA", "ENG", "BEL"),
    "北海海岸": ("HOL", "GER", "DEN", "NOR", "ENG"),
    "大西洋海岸": ("FRA", "SPR", "POR"),
    "比斯开湾": ("FRA", "SPR"),
    "法国海岸": ("FRA",),
    "德国海岸": ("GER",),
    "挪威海岸": ("NOR",),
    "丹麦海岸": ("DEN",),
    "地中海海岸": ("SPR", "FRA", "ITA", "GRE", "TUR"),
    "意大利海岸": ("ITA",),
    "西班牙海岸": ("SPR",),
    "巴尔干海岸": ("GRE", "YUG", "ALB", "BUL"),
    "波罗的海": ("GER", "POL", "SWE", "FIN", "SOV", "LIT", "LAT", "EST", "DEN"),
    "波罗的海海岸": ("GER", "POL", "SWE", "FIN", "SOV", "LIT", "LAT", "EST", "DEN"),
    "黑海": ("TUR", "SOV", "ROM", "BUL"),
    "黑海海岸": ("TUR", "SOV", "ROM", "BUL"),
    "英伦三岛": ("ENG", "SCO", "WLS", "IRE", "NIR"),
    "日本海岸": ("JAP",),
    "中国海岸": ("CHI",),
    "中东海岸": ("SAU", "OMA", "IRQ", "PER"),
}

_STRIP_WORDS = ("海岸线", "沿岸", "沿海", "一带", "地区", "区域", "那边", "这边",
                "附近", "的", "看看", "看一下", "看", "查", "帮我看")


class ZoneError(RuntimeError):
    def __init__(self, code: str, msg: str, hint: str = ""):
        super().__init__(msg)
        self.code = code
        self.msg = msg
        self.hint = hint


def _norm(text: str) -> str:
    s = (text or "").strip()
    for sep in (" ", "　", "，", ",", "。", "！", "?"):
        s = s.replace(sep, "")
    for w in _STRIP_WORDS:
        s = s.replace(w, "")
    return s


class Zone:
    """区域解析 + 态势汇总。数据全部来自本地缓存文件，不连游戏。"""

    def __init__(self, data_dir: Path, assets_dir: Path | None = None,
                 game_dir: Path | None = None) -> None:
        self._dir = Path(data_dir)
        self._assets = Path(assets_dir) if assets_dir else Path(__file__).parent / "assets"
        self._game_dir = Path(game_dir) if game_dir else None
        self._coastal: set[int] | None = None
        self._cores: dict[str, list[int]] | None = None
        self._names: dict[str, str] | None = None

    # ── 静态数据 ─────────────────────────────

    def coastal(self) -> set[int]:
        if self._coastal is None:
            self._coastal = set()
            p = self._assets / "coastal.json.gz"
            try:
                with gzip.open(p, "rt", encoding="utf-8") as fh:
                    self._coastal = {int(x) for x in (json.load(fh).get("coastal") or [])}
            except (OSError, ValueError):
                self._coastal = set()
        return self._coastal

    def cores(self) -> dict[str, list[int]]:
        if self._cores is None:
            self._cores = {}
            for p in (self._dir / "cache" / "country_cores.json",
                      self._dir / "country_cores.json"):
                if not p.is_file():
                    continue
                try:
                    raw = json.loads(p.read_text(encoding="utf-8"))
                    self._cores = {str(k): [int(x) for x in v]
                                   for k, v in (raw.get("map") or {}).items()}
                    break
                except (OSError, ValueError):
                    continue
            # 缓存没有（宿主还没跑过按国土圈范围的查询）：自己从游戏目录算一遍。
            # history/states 是静态游戏数据，跟存档无关，0.4 秒。
            if not self._cores and self._game_dir:
                try:
                    from .save_state import scan_cores
                    got = scan_cores(self._game_dir)
                    self._cores = {t: sorted(s) for t, s in got.items()}
                    p = self._dir / "cache" / "country_cores.json"
                    try:
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_text(json.dumps(
                            {"game_version": "", "map": self._cores},
                            ensure_ascii=False), encoding="utf-8")
                    except OSError:
                        pass
                except Exception:  # noqa: BLE001 算不出来就当没有，别炸
                    self._cores = {}
        return self._cores

    def names(self) -> dict[str, str]:
        """tag → 中文名。"""
        if self._names is None:
            self._names = {}
            for p in (self._dir / "cache" / "country_names.json",
                      self._dir / "country_names.json"):
                if not p.is_file():
                    continue
                try:
                    raw = json.loads(p.read_text(encoding="utf-8"))
                    self._names = {str(k): str(v)
                                   for k, v in (raw.get("map") or {}).items()}
                    break
                except (OSError, ValueError):
                    continue
        return self._names

    def label_of(self, tag: str) -> str:
        return self.names().get(tag.upper(), tag.upper())

    def tag_of(self, text: str) -> str:
        """中文国名 → tag（精确优先，其次唯一包含）。"""
        want = _norm(text)
        if not want:
            return ""
        names = self.names()
        for t, zh in names.items():
            if zh == want:
                return t
        hits = [t for t, zh in names.items() if zh and (zh in want or want in zh)]
        return hits[0] if len(hits) == 1 else ""

    # ── 区域解析 ─────────────────────────────

    def resolve(self, text: str, places=None, adjacency=None) -> dict:
        """区域文本 → {label, provs, tags, kind}。认不出抛 ZoneError。"""
        raw = (text or "").strip()
        if not raw:
            raise ZoneError("bad_input", "要看哪片区域？比如「欧洲西部海岸线」「法国海岸」")
        norm = _norm(raw)
        coastal = self.coastal()

        # 1) 内置区域表（先精确，再包含）
        tags: tuple[str, ...] = ()
        for name, ts in REGIONS.items():
            if _norm(name) == norm:
                tags = ts
                break
        if not tags:
            for name, ts in REGIONS.items():
                n = _norm(name)
                if n and (n in norm or norm in n):
                    tags = ts
                    break

        # 2) 国家 + 海岸/边境
        if not tags:
            tag = self.tag_of(norm)
            if tag:
                tags = (tag,)

        if tags:
            cores = self.cores()
            provs: set[int] = set()
            used: list[str] = []
            want_coast = ("海岸" in raw or "沿" in raw or "海" in raw)
            for t in tags:
                ps = cores.get(t) or []
                if not ps:
                    continue
                if want_coast and coastal:
                    ps = [p for p in ps if p in coastal]
                provs.update(int(p) for p in ps)
                used.append(t)
            if not provs:
                if want_coast:
                    raise ZoneError(
                        "no_data",
                        f"知道你说的是{self.label_of(tags[0]) if tags else raw}，"
                        f"但它的沿海核心省还没解析出来（存档还没解析过，或者这个国家没有海岸）。",
                        "先让用户存一次档（或者等一下），再问一次。",
                    )
                raise ZoneError("no_data", "这片区域没解析出省份。", "换个说法再试。")
            label = "、".join(self.label_of(t) for t in used) + \
                    ("的沿海" if want_coast else "全境")
            return {"label": label, "provs": sorted(provs),
                    "tags": list(used), "kind": "region"}

        # 3) 地名（诺曼底/布雷斯特）
        if places is not None:
            try:
                pid, lab = places.resolve(raw)
            except Exception:  # noqa: BLE001
                pid = 0
                lab = ""
            if pid:
                provs = {int(pid)}
                # 地名扩一跳，报「那一带」
                if adjacency:
                    provs.update(int(n) for n in adjacency.get(int(pid), ()))
                return {"label": f"{lab}一带", "provs": sorted(provs),
                        "tags": [], "kind": "place"}

        raise ZoneError(
            "unknown_area",
            f"「{raw}」我不敢瞎认成一片区域。",
            "可以这样说：欧洲西部海岸线 / 法国海岸 / 北海海岸 / 波罗的海 / "
            "地中海海岸；或者说地标（诺曼底、布雷斯特）。",
        )

    # ── 态势汇总 ─────────────────────────────

    def report(self, live, zone: dict, player_short: str = "",
               label=None, max_units: int = 6) -> dict:
        """把一片区域上的兵力摊开：谁在上面、哪段空着。只读分布表，零额外扫描。"""
        provs = [int(p) for p in zone.get("provs") or []]
        if not provs:
            return {"ok": False, "text": "这片区域没有省份数据。"}
        age, dist = live.peek_dist(max_age=300)
        rows = live.peek_named(max_age=900)
        addr_to_short = {}
        for r in rows:
            a, s = r.get("addr"), r.get("short")
            if a and s:
                addr_to_short[int(a)] = str(s)

        mine: dict[int, int] = {}
        others: dict[str, int] = {}
        occupied = 0
        empty: list[int] = []
        for p in provs:
            slot = dist.get(p) or {}
            if not slot:
                empty.append(p)
                continue
            occupied += 1
            for caddr, n in slot.items():
                short = addr_to_short.get(int(caddr), "")
                if short and player_short and short == player_short:
                    mine[p] = mine.get(p, 0) + n
                elif short:
                    others[short] = others.get(short, 0) + n

        lab = label or (lambda p: str(p))
        lines = [f"{zone.get('label', '这片区域')}：{len(provs)} 个省"
                 f"（{occupied} 个有驻军，{len(empty)} 个空着）"]
        if age >= 0:
            lines.append(f"（兵是按 {age:.0f} 秒前的实时分布表算的）")
        else:
            lines.append("（实时分布表还没热起来，这一版只报省份，兵数等下一秒再问）")

        my_total = sum(mine.values())
        if my_total:
            top = sorted(mine.items(), key=lambda kv: -kv[1])[:max_units]
            lines.append(f"  我方 {player_short or '自己'}：{my_total} 个师，"
                         f"分布在 {len(mine)} 个省 —— " +
                         "、".join(f"{lab(p)} {n} 师" for p, n in top))
        elif age >= 0:
            lines.append(f"  我方 {player_short or '自己'}：这段一个师都没有。")

        if others:
            top = sorted(others.items(), key=lambda kv: -kv[1])[:max_units]
            lines.append("  别国在岸上的兵：" +
                         "、".join(f"{self.label_of(t)} {n} 师" for t, n in top))

        if empty:
            head = "、".join(lab(p) for p in empty[:max_units])
            more = "" if len(empty) <= max_units else f" 等 {len(empty)} 个省"
            lines.append(f"  空防段：{head}{more}")

        return {
            "ok": True,
            "text": "\n".join(lines),
            "provs": len(provs),
            "empty": len(empty),
            "mine": my_total,
            "others": others,
            "dist_age": age,
        }
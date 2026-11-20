"""地名解析：把用户说的"柏林"换成能点的省 id。

用户不会说省 id。他会说"调到柏林""往莫斯科方向"。而地图上点的是**省**，
存档里的师也记在**省**，中间还夹着一层**州**。

所以这里做三件事：
1. 州名 → 州 id
2. 州 id → 该州里最值得点的那个省（面积最大，落点一定在陆地主体上）
3. 数字直接当省 id 用（给"我知道自己在干什么"的场合）

名字来源是插件缓存里的 `state_names.json`（州名）和 `prov_to_state.json`
（省→州），两个都是存档解析时顺手生成的，跟游戏版本绑定。
"""

from __future__ import annotations

import json
from pathlib import Path

from .prov_index import ProvIndex

__all__ = ["Places", "load", "strip_place_suffix"]

# 地名后缀：用户/语音常把"勃兰登堡"说成"勃兰登堡省/州/市"
_SUFFIXES = ("省份", "省", "州", "城市", "城", "市", "地区", "大区")


def _strip_suffix(key: str) -> str:
    s = (key or "").strip()
    for suf in _SUFFIXES:
        if s.endswith(suf) and len(s) > len(suf):
            return s[: -len(suf)].strip()
    return s


def strip_place_suffix(text: str) -> str:
    """公开版：给 voiceorder 用，去掉"省/州/市"这类后缀。"""
    return _strip_suffix(text)


def _similar(a: str, b: str) -> float:
    """两个地名的"像不像"，按共同汉字占比。

    只做最后一道兜底，所以门槛定得高：两边都至少 3 个字才比，
    相似度按集合重合算（"博兰登堡"/"勃兰登堡" = 3/4 = 0.75）。
    两个字的地名（柏林/华沙）不比——差一个字就成另一个地方了。
    """
    sa, sb = set(a or ""), set(b or "")
    if len(sa) < 3 or len(sb) < 3:
        return 0.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(len(sa), len(sb))


class Places:
    def __init__(
        self,
        state_names: dict[int, str],
        prov_to_state: dict[int, int],
        index: ProvIndex,
    ) -> None:
        self._state_names = state_names
        self._prov_to_state = prov_to_state
        self._index = index
        # 州名 -> 州 id（同名州取 id 小的那个，HOI4 里同名州极少）
        self._by_name: dict[str, int] = {}
        for sid, name in sorted(state_names.items()):
            key = (name or "").strip()
            if key and key not in self._by_name:
                self._by_name[key] = sid

    def state_name(self, state_id: int) -> str:
        return self._state_names.get(int(state_id)) or ""

    def label(self, prov_id: int) -> str:
        """省的中文标签：用所属州名，认不出州就退化成「省 <id>」。

        只给省号看不出是哪儿（真机聊天记录里模型报「省 362」，用户得自己去地图上找）。
        """
        pid = int(prov_id)
        sid = self._prov_to_state.get(pid)
        nm = (self.state_name(sid) if sid else "") or ""
        return f"{nm}（{pid}）" if nm else f"省 {pid}"

    def provinces_of_state(self, state_id: int) -> list[int]:
        return [p for p, s in self._prov_to_state.items() if s == int(state_id)]

    def main_province(self, state_id: int) -> int | None:
        """州里面积最大的那个省。点它就等于点这个州。"""
        best: int | None = None
        best_area = -1
        for pid in self.provinces_of_state(state_id):
            a = self._index.area(pid)
            if a > best_area:
                best, best_area = pid, a
        return best

    def find_state(self, text: str) -> int | None:
        """按州名找州 id。

        顺序：去掉"省/州/市"后缀 → 精确 → 包含（"勃兰登"）→ 容错（"博兰登堡"）。
        最后一档是给语音识别用的：ASR 常把"勃"听成"博/波"，差一个字就整条令作废
        太亏。但它只在**唯一最佳**且相似度够高时才认，认不准还是返回 None。
        """
        key = _strip_suffix((text or "").strip())
        if not key:
            return None
        if key in self._by_name:
            return self._by_name[key]
        hits = [sid for name, sid in self._by_name.items() if key in name]
        if len(hits) == 1:
            return hits[0]
        if hits:
            # 多个命中时取 id 最小的，并且**不猜**——让调用方把候选报给用户
            return min(hits)
        best, best_score = None, 0.0
        tied = False
        for name, sid in self._by_name.items():
            score = _similar(key, name)
            if score > best_score + 1e-9:
                best, best_score, tied = sid, score, False
            elif abs(score - best_score) < 1e-9:
                tied = True
        if best is not None and not tied and best_score >= 0.66:
            return best
        return None

    def resolve(self, text: str) -> tuple[int, str]:
        """把用户说的地方换成"能点的省 id"。

        顺序是：数字省号 → 城市名 → 州名。

        为什么城市名排在州名前：用户说"柏林"，而 HOI4 那个州叫"勃兰登堡"。
        先查城市名才能对上他的直觉；州名只在他真的说"勃兰登堡"时才用得上。

        返回 (province_id, 给人看的说明)。认不出来抛 ValueError。
        """
        pid, label, _sid, _is_state = self.resolve_detail(text)
        return pid, label

    def resolve_detail(self, text: str) -> tuple[int, str, int | None, bool]:
        """同 resolve，但多告诉调用方"这是州还是省"。

        返回 (province_id, label, state_id, is_state)。`is_state=True` 时
        province_id 只是这个州的代表省（面积最大那个）——调兵要找**全州**的师，
        不能只按这一个省过滤，否则"把勃兰登堡的师调到柏林"会因为那个师站在
        同州的另一个省而报"这里没有你的部队"。
        """
        raw = (text or "").strip()
        if not raw:
            raise ValueError("没说是哪里。")

        if raw.isdigit():
            pid = int(raw)
            if not self._index.has(pid):
                raise ValueError(f"省 {pid} 不在坐标索引里（可能是海域，或者版本对不上）。")
            sid = self._prov_to_state.get(pid)
            label = self.state_name(sid) if sid else ""
            return pid, f"省 {pid}" + (f"（{label}）" if label else ""), sid, False

        key = _strip_suffix(raw) or raw
        city = self._index.find_by_name(key)
        if city is not None:
            sid = self._prov_to_state.get(city)
            state = self.state_name(sid) if sid else ""
            label = f"{self._index.name(city)}（省 {city}" + (f"，{state}" if state else "") + "）"
            return city, label, sid, False

        sid = self.find_state(key)
        if sid is None:
            raise ValueError(f"没找到「{raw}」。城市名和州名里都没有。")
        pid = self.main_province(sid)
        if pid is None:
            raise ValueError(f"州「{raw}」里没有可点的省（坐标索引和存档版本可能不一致）。")
        return pid, f"{self.state_name(sid) or raw}（州，代表省 {pid}）", sid, True

    def describe(self) -> str:
        return f"地名表 {len(self._by_name)} 个州 / {len(self._prov_to_state)} 个省"


def _read_map(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    m = raw.get("map") if isinstance(raw, dict) else None
    return m if isinstance(m, dict) else {}


def load(data_dir: Path, index: ProvIndex) -> Places | None:
    """优先用当前世界资料库里的州名（跟着 mod 走），
    没有资料库再退回插件缓存（缓存来自存档）。"""
    try:
        from . import panel
        names = panel.current_names()
    except Exception:
        names = None
    if names and names.get("states") and names.get("prov_to_state"):
        return Places(names["states"], names["prov_to_state"], index)
    cache = Path(data_dir) / "cache"
    names_raw = _read_map(cache / "state_names.json")
    prov_raw = _read_map(cache / "prov_to_state.json")
    if not names_raw or not prov_raw:
        return None
    try:
        names = {int(k): str(v) for k, v in names_raw.items()}
        prov = {int(k): int(v) for k, v in prov_raw.items()}
    except (TypeError, ValueError):
        return None
    return Places(names, prov, index)

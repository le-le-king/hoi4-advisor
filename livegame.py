"""真·操作游戏：选国策、给师下移动命令。

跟存档那条线（save_state.py）的分工：
- 存档线是"看见"——读了才知道局势，慢但深。
- 这条线是"动手"——直接读写进程内对象，快、当场生效，所以每一步都要能解释。

两条命令的来路（都是逆向出来的，见开发文档附录 H / I）：

**国策**：不调任何引擎函数。把 `CNationalFocusProgress+0x18` 写成某个
`CNationalFocus*`，引擎下一 tick 自己推进、自己完成、自己解锁后续。
跟玩家在国策树里点一下完全等价。

**调兵**：调引擎自己的下单函数 `0xc12280(unit, &pathVec, replace, orderType)`。
它内部会自己扩容 `unit->path`、自己 new 订单对象。**绝对不能手写** `+0x2a8`
那个路径容器——它是内联 P 社容器 `{data, cap, size, alloc}`，手工改 size
会让引擎读到空指针，实测崩过两次。

三个必须守的边界（越线就从参谋变外挂了）：
1. 国策只接受"当前可选列表里"的指针，不允许跳节点。
2. 调兵只对**用户自己的师**下发，且只走陆地邻接路径，不传送。
3. 版本一变就只读。写操作的偏移是从 1.16.9 逆出来的，换版本硬写会崩存档。
"""

from __future__ import annotations

import contextlib
import gzip
import json
import struct
import threading
import time
from collections import deque
from pathlib import Path

from . import nekoagent, warscope

__all__ = ["LiveError", "LiveGame"]


# ── 偏移（1.16.9，实测） ────────────────────────────────

OFF_UNITS = 0x2F8          # CCountry -> vec<CArmy*>
OFF_UNIT_ID = 0x24         # CArmy -> 师 id（**0.9.15 真机确认**：与存档
#                            countries.<TAG>.units.division[].id 完全一致，
#                            228/228 命中、无重复、位置一致率 100%）。
#                            这是「存档编组 → 内存单位」的桥：存档里有
#                            「第9集团军 = 这 17 个师 id」，内存里按 id
#                            就能拿到那 17 个单位的地址。
OFF_PROV = 0x38            # CArmy -> 当前省 ID
OFF_TYPE = 0x08            # CArmy -> 单位类型
OFF_SUBCOUNT = 0x254       # CArmy -> 下属子单位数
OFF_PATH = 0x2A8           # CArmy -> 路径容器 data（内联容器，不是指针对象）
OFF_PCAP = 0x2B0
OFF_PSIZE = 0x2B4
OFF_ORDER_TYPE = 0x2EC
OFF_TARGET = 0x378
OFF_REMAIN = 0x610

OFF_PROGRESS = 0x14E8      # CCountry -> CNationalFocusProgress*
OFF_CUR = 0x18             # 当前正在进行的国策指针
OFF_DAYS = 0x40            # 进度累积值
OFF_LIST = 0x80            # vec<CNationalFocus*> 当前可选

OFF_NAME_FULL = 0x20       # CCountry -> std::string 全名（德意志国）
OFF_NAME_SHORT = 0x40      # CCountry -> std::string 短名（德国），跟本地化表对得上
OFF_CAPITAL = 0x10A8       # CCountry -> 首都省 ID（0.9.3 起存疑：实测是小整数）
OFF_PROV_ID = 0xAC         # CProvince -> 省份 ID
VT_PROVINCE = 0x25E6F08    # CProvince vtable RVA（find_instances 用）

# ── 0.9.3：省份控制者 / 陆战攻守双方（1.16.9 真机验证，见开发文档 10.12） ──
# 关键：控制者存的是 **CCountryTag 的整数值**（= CCountry+0x18），不是 CCountry*。
# 之前找了很久都没找到，就是因为一直在对象里搜国家指针。
OFF_COUNTRY_ID = 0x18        # CCountry -> 国家序号（CCountryTag）
OFF_PROV_CTRL_ID = 0x160     # CProvince -> 控制国序号
OFF_PROV_CTRL_AREA = 0xD8    # CProvince -> CControllerArea*（同一控制国共用一个）
OFF_PROV_OWNER_AREA = 0xE0   # CProvince -> COwnerArea*
OFF_PROV_STATE = 0xC8        # CProvince -> CState*
OFF_PROV_UNITS = 0xE8        # CProvince -> CPdxArray<CArmy*> 驻军（cap@+0xF0 / size@+0xF4）
OFF_STATE_OWNER = 0xD0       # CState -> 法理所有者国家序号
OFF_STATE_CTRL = 0xD4        # CState -> 实际控制者国家序号
OFF_COMBAT_ATTACKER = 0x30   # CLandCombat -> CLandCombatant* 攻方
OFF_COMBAT_DEFENDER = 0x38   # CLandCombat -> CLandCombatant* 守方
OFF_COMBATANT_ARMIES = 0x20  # CLandCombatant -> CPdxArray<CArmy*> 参战师
OFF_COMBATANT_CAP = 0x28
OFF_COMBATANT_SIZE = 0x2C

# CArmy -> CReferencedDivisionTemplate*；那个对象 +0x30 是编制名字符串
# 实测值：'空降伞兵师1941A型' / '步兵师1939A型'（2026-11-08，1.16.9）
OFF_TEMPLATE = 0x450
OFF_TPL_NAME = 0x30
# 模板 +0x80 -> 编成数组：每项 16 字节 {CSubUnitDefinition* def, u32 营数, u32 ?}
OFF_TPL_COMP = 0x80
# CSubUnitDefinition +0x18 -> std::string 兵种名（infantry / medium_armor / paratrooper…）
OFF_SUB_TYPE = 0x18

# 兵种名（游戏内部英文）-> 中文。图标就是按这个分的：坦克/伞兵/步兵…
TYPE_ZH: dict[str, str] = {
    "infantry": "步兵",
    "motorized": "摩托化步兵",
    "mechanized": "机械化步兵",
    "cavalry": "骑兵",
    "mountaineer": "山地兵",
    "mountaineers": "山地兵",
    "marine": "海军陆战",
    "marines": "海军陆战",
    "paratrooper": "伞兵",
    "paratroopers": "伞兵",
    "light_armor": "轻型坦克",
    "medium_armor": "中型坦克",
    "heavy_armor": "重型坦克",
    "super_heavy_armor": "超重型坦克",
    "modern_armor": "现代坦克",
    "amphibious_armor": "两栖坦克",
    "amphibious": "两栖",
    "artillery": "炮兵",
    "artillery_brigade": "炮兵",
    "rocket_artillery": "火箭炮",
    "anti_tank": "反坦克",
    "anti_tank_brigade": "反坦克",
    "anti_air": "防空",
    "motorized_rocket": "自行火箭炮",
    "armored_car": "装甲车",
    "armored_car_recon": "装甲侦察车",
    "mot_recon": "摩托化侦察",
    "recon": "侦察",
    "engineer": "工兵",
    "logistics_company": "后勤",
    "support": "支援",
}

# 线列兵种归类（支援连不参与"图标是什么"的投票）
_LINE_CATEGORY: dict[str, str] = {
    "light_armor": "坦克", "medium_armor": "坦克", "heavy_armor": "坦克",
    "super_heavy_armor": "坦克", "modern_armor": "坦克", "amphibious_armor": "坦克",
    "infantry": "步兵",
    "motorized": "摩托化",
    "mechanized": "机械化",
    "cavalry": "骑兵",
    "mountaineer": "山地", "mountaineers": "山地",
    "marine": "海军陆战", "marines": "海军陆战",
    "paratrooper": "伞兵", "paratroopers": "伞兵",
    "artillery": "炮兵", "artillery_brigade": "炮兵",
}

# 编制名里的中文关键词优先级高于编成（用户给模板起名会写"坦克师"）
_NAME_HINTS = (
    ("坦克", "坦克"), ("装甲", "坦克"), ("伞兵", "伞兵"), ("空降", "伞兵"),
    ("山地", "山地"), ("骑兵", "骑兵"), ("摩托化", "摩托化"),
    ("机械化", "机械化"), ("陆战", "海军陆战"), ("步兵", "步兵"),
)

ARMOR_TYPES = ("light_armor", "medium_armor", "heavy_armor", "super_heavy_armor",
               "modern_armor", "amphibious_armor")

# 用户嘴里的兵种 → 真正要匹配的内部兵种名/中文名。
# 说"坦克"要能匹配 medium_armor；说"装甲"同理。
UNIT_TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    "伞兵": ("paratrooper", "伞兵", "空降"),
    "空降": ("paratrooper", "空降", "伞兵"),
    "坦克": ("armor", "坦克", "装甲"),
    "装甲": ("armor", "装甲", "坦克"),
    "步兵": ("infantry", "步兵"),
    "摩托化": ("motorized", "摩托化"),
    "机械化": ("mechanized", "机械化"),
    "骑兵": ("cavalry", "骑兵"),
    "山地": ("mountaineer", "山地"),
    "海军陆战": ("marine", "海军陆战", "陆战"),
    "陆战队": ("marine", "海军陆战", "陆战"),
    "炮兵": ("artillery", "炮兵"),
    "反坦克": ("anti_tank", "反坦克"),
    "防空": ("anti_air", "防空"),
}


def icon_of(comp: list[dict], name: str = "") -> str:
    """编成 + 编制名 -> 游戏里那个图标是什么兵种。

    先看名字（用户/游戏给模板起的"中型坦克师"就是最直接的线索），
    名字里没有中文兵种词再看编成：按**类别**汇总营数（轻/中/重坦都算坦克），
    平手时坦克优先——"6 摩步 + 6 坦克"的师，单位牌上画的还是坦克。
    """
    n = str(name or "")
    for kw, kind in _NAME_HINTS:
        if kw in n:
            return kind
    counts: dict[str, int] = {}
    for item in comp:
        cat = _LINE_CATEGORY.get(str(item.get("type") or ""))
        if not cat:
            continue
        counts[cat] = counts.get(cat, 0) + int(item.get("count") or 0)
    if not counts:
        return ""
    best = max(counts.items(), key=lambda kv: (kv[1], kv[0] == "坦克"))
    return best[0]


def template_matches(template: str, want: str) -> bool:
    """编制名（"空降伞兵师1941A型"）算不算用户说的兵种（"伞兵"）。"""
    tpl = str(template or "")
    key = str(want or "").strip()
    if not tpl or not key:
        return False
    keys = UNIT_TYPE_ALIASES.get(key, (key,))
    return any(k in tpl for k in keys)


def unit_matches(unit: dict, want: str) -> bool:
    """一个师算不算用户说的兵种：名字、图标、编成三样都看。

    为什么不能只看名字：用户可以给模板起任意名字（甚至图标和名字不一致）。
    编成里的内部兵种名（medium_armor/paratrooper）才是图标真正依据。
    """
    key = str(want or "").strip()
    if not key:
        return True
    keys = UNIT_TYPE_ALIASES.get(key, (key,))
    name = str(unit.get("template") or "")
    kind = str(unit.get("kind") or "")
    comp = unit.get("comp") or []
    hay = name + " " + kind + " " + " ".join(str(c.get("type") or "") for c in comp)
    for k in keys:
        if k == "armor":
            if kind == "坦克" or any(t in hay for t in ARMOR_TYPES) or "坦克" in hay:
                return True
        elif k in hay:
            return True
    return False

HEAP_LO = 0x10000
HEAP_HI = 0x7FFFFFFFFFFF

# 每帧重置的临时分配区：扫到的对象落在这里就是临时的，不能当实例用
ARENA_LO = 0xB9540000000
ARENA_HI = 0xB9550000000


class LiveError(RuntimeError):
    """kind: version / no_game / no_country / bad_input / denied / failed."""

    def __init__(self, kind: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.hint = hint


# 通行权判定缓存多久失效。省界一分钟内基本不变，但也不能隔夜复用。
PASS_TTL = 60.0


def _heap(addr: int) -> bool:
    return HEAP_LO < addr < HEAP_HI and addr % 8 == 0


def _pool_runs(addrs: list[int]) -> tuple[int, list[tuple[int, int]], list[int]]:
    """把实例地址拆成「等步长连续段」+ 零散地址，供整段批量读。

    真机（1.16.9）：CProvince 是 0x1E0 步长的对象池，13376 个实例里
    13373 个落在 34 个连续段上，每段 554 个。逐省一次 RD 要 26 秒；
    整段读只发 300 来次 RD（nekoagent.read 内部按 0x8000 自动分块）。
    步长从相邻差分的众数**自动推断**，不写死，版本变了也能自适应；
    没有明显池结构就全部退回逐省读。
    """
    if len(addrs) < 16:
        return 0, [], list(addrs)
    diffs: dict[int, int] = {}
    for a, b in zip(addrs, addrs[1:]):
        d = b - a
        if 0 < d <= 0x1000:
            diffs[d] = diffs.get(d, 0) + 1
    if not diffs:
        return 0, [], list(addrs)
    stride = max(diffs, key=lambda d: diffs[d])
    if diffs[stride] < len(addrs) * 0.8:     # 不构成池，别硬套
        return 0, [], list(addrs)
    runs: list[tuple[int, int]] = []
    singles: list[int] = []
    start, prev, count = addrs[0], addrs[0], 1
    for a in addrs[1:]:
        if a - prev == stride:
            count += 1
        else:
            if count >= 8:
                runs.append((start, count))
            else:
                singles.extend(start + i * stride for i in range(count))
            start, prev, count = a, a, 1
        prev = a
    if count >= 8:
        runs.append((start, count))
    else:
        singles.extend(start + i * stride for i in range(count))
    return stride, runs, singles


class LiveGame:
    """一个连接 + 一份国家地址缓存。别在多线程里共享 Agent。"""

    def __init__(self, data_dir: Path, log=None, allow_unknown_build: bool = False) -> None:
        self._data_dir = Path(data_dir)
        self._log = log or (lambda msg: None)
        self._allow_unknown_build = allow_unknown_build
        self._assets = Path(__file__).resolve().parent / "assets"
        self._sym = json.loads((self._assets / "live_symbols.json").read_text(encoding="utf8"))
        self._adj: dict[int, list[int]] | None = None
        self._adj_wid = ""          # 缓存的是哪个世界的邻接，换世界要重读
        self._lock = threading.Lock()
        self._cache_path = self._data_dir / "live_cache.json"
        # 编制指针 -> {name, kind, comp}。同一批师共用几个模板，缓存一下
        self._tpl_cache: dict[int, dict] = {}
        # 国家列表 / 全服部队分布：都是十几秒的扫描，缓存一会儿
        self._countries_cache: tuple[float, list[int]] = (0.0, [])
        # 0.9.14：分布表带上 (pid, base) 会话键——它是常驻数据源（哨兵养、
        # 工具只读），游戏重开时绝不能把上一局的分布当这一局的报出去
        self._units_by_prov_cache: tuple[float, int, int, dict] = (0.0, 0, 0, {})
        # 0.9.3：国家序号→名字、师→国家。都按 (pid, base) 失效，换局不会带旧数据
        self._country_names_cache: tuple[float, int, int, dict] = (0.0, 0, 0, {})
        # 通行权判定缓存（省 -> 是不是我控制的），key 是 (pid, base, 我的国家序号)
        self._pass_cache: tuple[float, dict[int, bool]] = (0.0, {})
        self._pass_key: tuple = ()
        self._army_countries_cache: tuple[float, int, int, dict] = (0.0, 0, 0, {})
        self._control_map_cache: tuple[float, int, int, dict] = (0.0, 0, 0, {})
        self._control_scan_failures = 0
        # 0.9.13：全部国家的 (addr, short, full) 行。真机实测认一遍全世界
        # 406 国要 5~8 秒（每国两次字符串读），而 units_near 一次查询要认两遍
        # （先当国名试、再扫部队），8 秒超时形同虚设——地名查询全军覆没。
        # 名字和地址一局内不变，按 (pid, base) + TTL 缓存，热查询归零。
        self._named_rows_cache: tuple[float, int, int, list] = (0.0, 0, 0, [])
        # 0.9.9：玩家这局战争的参战范围（本国+盟友+敌对国）。
        # 全服陆战里绝大部分跟玩家没关系，必须过滤掉再交给模型/播报。
        self._war_scope: warscope.Scope | None = None
        # 0.9.7：DLL 端用 g_cmd_lock 串行化**每一条**命令（SCANALL 一条能占住
        # 锁好几秒），后台版图扫描和工具查询会互相排队，把 30 秒看门狗撞穿。
        # 工具一来就打个时间戳，后台扫描在每个检查点主动让位；时间戳会自己过期，
        # 工具异常退出也不会把扫描永久锁死。
        self._tool_until = 0.0
        self._session_key: tuple[int, int] = (0, 0)
        # 0.9.5：版图扫描挪到后台，工具只读缓存（宿主每个 entry 只有 30 秒）
        # 独立锁：别让 8~15 秒的全服扫描把工具的 frontline() 堵在门口
        self._ctl_lock = threading.Lock()
        self._ctl_bg_lock = threading.Lock()
        self._ctl_bg_running = False
        self._name_cache: dict[int, str] = {}

    # ── 资源 ──────────────────────────────────

    def _vt(self, name: str) -> int:
        return int(self._sym["classes"][name]["vtable"])

    def _build(self) -> dict:
        return self._sym.get("build") or {}

    def adjacency(self) -> dict[int, list[int]]:
        """省 → 邻省。优先用当前世界资料库里的（跟着 mod 走），
        没建过资料库就退回打包进来的原版快照。"""
        try:
            from . import panel as _panel
        except Exception:
            _panel = None
        wid = ""
        if _panel is not None:
            try:
                wid = _panel.current_world_id()
            except Exception:
                wid = ""
        if self._adj is None or self._adj_wid != wid:
            got = _panel.current_adjacency() if _panel is not None else None
            if got:
                self._adj = got
            else:
                path = self._assets / "adjacency.json.gz"
                with gzip.open(path, "rt", encoding="utf8") as f:
                    self._adj = {int(k): v for k, v in json.load(f).items()}
            self._adj_wid = wid
        return self._adj

    # ── 连接 ──────────────────────────────────

    def connect(self) -> nekoagent.Agent:
        ag = nekoagent.ensure()
        # 换局 / 重开游戏：所有按地址缓存的表都要作废。之前只给控制图、
        # 国家名、师→国家这三张表做了 (pid, base) 校验，国家地址表和全服
        # 部队分布没有，换局后会拿着上一局的 CCountry* 去读，既慢又可能读错。
        key = (ag.pid, ag.base)
        if key != self._session_key:
            self._session_key = key
            self._countries_cache = (0.0, [])
            self._units_by_prov_cache = (0.0, 0, 0, {})
            self._country_names_cache = (0.0, 0, 0, {})
            self._army_countries_cache = (0.0, 0, 0, {})
            self._control_map_cache = (0.0, 0, 0, {})
            self._named_rows_cache = (0.0, 0, 0, [])
            self._tpl_cache = {}
            self._war_scope = None      # 换局了，上一局的敌人/盟友不能带过来
        return ag

    def mark_tool(self, seconds: float = 20.0) -> None:
        """工具正在查：让后台全服扫描让路（DLL 端所有命令串行）。"""
        self._tool_until = max(self._tool_until, time.monotonic() + seconds)

    def tool_active(self) -> bool:
        return time.monotonic() < self._tool_until

    def status(self) -> dict:
        st = nekoagent.status()
        st["build_ok"] = self._build_ok()
        return st

    def _build_ok(self) -> bool:
        want = int(self._build().get("exe_size") or 0)
        if not want:
            return True
        sig = nekoagent.signature()
        size = int(sig.get("size") or 0)
        return not size or size == want

    def _guard_write(self, what: str) -> None:
        """版本闸。写之前必须过。

        为什么这么严：偏移和函数 RVA 都是从 1.16.9 逆出来的。换个版本，
        这些数字要么指到别的字段，要么指到别的函数——按下回车就是存档报废。
        所以"只读"是可接受的降级，"写错"不行。
        """
        if self._build_ok() or self._allow_unknown_build:
            return
        want = int(self._build().get("exe_size") or 0)
        sig = nekoagent.signature()
        raise LiveError(
            "version",
            f"这个游戏的 exe 大小是 {sig.get('size')} 字节，我这套偏移是按 "
            f"{want} 字节那一版（1.16.9）逆出来的，对不上。"
            f"{what}这类写操作我先不做了，免得把存档写坏。看局势、读国策都还能用。",
            "原样告诉用户版本不匹配；不要自己试着写。",
        )

    # ── 读 std::string ────────────────────────

    @staticmethod
    def _sstr(ag: nekoagent.Agent, addr: int, limit: int = 256) -> str:
        """读 P 社的 std::string：<=15 字节内联（SSO），否则首 8 字节是堆指针。"""
        head = ag.read(addr, 32)
        size = struct.unpack_from("<Q", head, 16)[0]
        cap = struct.unpack_from("<Q", head, 24)[0]
        if size == 0 or size > limit:
            return ""
        if cap <= 15:
            raw = head[:min(size, 16)]
        else:
            ptr = struct.unpack_from("<Q", head, 0)[0]
            if not _heap(ptr):
                return ""
            raw = ag.read(ptr, size)
        return raw.decode("utf-8", "replace")

    # ── 找玩家国家 ────────────────────────────

    def _cache_read(self) -> dict:
        try:
            return json.loads(self._cache_path.read_text(encoding="utf8"))
        except (OSError, ValueError):
            return {}

    def _cache_write(self, data: dict) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf8")
        except OSError as exc:
            self._log(f"写 live_cache 失败：{exc}")

    def _countries(self, ag: nekoagent.Agent) -> list[int]:
        """全部 CCountry 实例。扫一次约 14 秒，所以结果要缓存。"""
        hits = ag.find_instances(self._vt("CCountry"))
        return [h for h in hits if not (ARENA_LO <= h < ARENA_HI)]

    def locate(
        self,
        player_tag: str = "",
        country_name: str = "",
        capital: int = 0,
        force: bool = False,
    ) -> dict:
        """定位玩家国家。返回 {"addr","name","source","candidates"}。

        定位顺序（从便宜到贵）：
        1. 上次缓存的地址还活着、名字还一样 → 直接用（毫秒级）
        2. 扫一遍 CCountry，按短名/全名对本地化名（德国 / 德意志国 都能对上）
        3. 都对不上就用首都省 ID 交叉验证
        4. 还不行就把候选名单交出去，让上层问用户——**绝不瞎猜一个**
        """
        with self._lock:
            ag = self.connect()
            try:
                if not force:
                    got = self._from_cache(ag, player_tag, country_name)
                    if got:
                        return got
                return self._scan(ag, player_tag, country_name, capital)
            finally:
                ag.close()

    def _from_cache(self, ag: nekoagent.Agent, player_tag: str, country_name: str) -> dict | None:
        c = self._cache_read()
        addr = int(c.get("country") or 0)
        if not addr or c.get("pid") != ag.pid or int(c.get("base") or 0) != ag.base:
            return None
        if player_tag and c.get("tag") and c["tag"] != player_tag:
            return None
        try:
            short = self._sstr(ag, addr + OFF_NAME_SHORT)
            full = self._sstr(ag, addr + OFF_NAME_FULL)
        except (nekoagent.AgentError, RuntimeError):
            return None
        if not (short or full):
            return None

        # 缓存只能证明"上次这个地址是这个国家"，不能证明"用户现在要的就是它"。
        # 用户明确说了国名时，名字对不上就重扫——这里错过一次就会去动别人的部队。
        want = (country_name or "").strip()
        if want:
            names = (short, full, str(c.get("short") or ""))
            if not any(want == n or (n and (want in n or n in want)) for n in names):
                return None
        confirmed = (c.get("confirmed") or {})
        return {"addr": addr, "short": short, "full": full,
                "source": "缓存 / 已确认" if confirmed.get("addr") == addr else "缓存",
                "confirmed": bool(confirmed.get("addr") == addr),
                "candidates": []}

    def _scan(self, ag: nekoagent.Agent, player_tag: str, country_name: str,
              capital: int) -> dict:
        # 走 _countries_cached：一次 SCANALL 约 14 秒，缓存 180 秒。
        # 之前这里直接 _countries()，用户每换一次国名就再扫一遍全服。
        hits = self._countries_cached(ag)
        rows: list[dict] = []
        for addr in hits:
            try:
                short = self._sstr(ag, addr + OFF_NAME_SHORT)
                full = self._sstr(ag, addr + OFF_NAME_FULL) if not short else ""
            except (nekoagent.AgentError, RuntimeError):
                continue
            if not short and not full:
                continue
            rows.append({"addr": addr, "short": short or full, "full": full or short})

        want = (country_name or "").strip()
        exact = [r for r in rows if want and (r["short"] == want or r["full"] == want)]
        loose = [r for r in rows if want and not exact and
                 (want in r["short"] or want in r["full"] or
                  r["short"] in want or r["full"] in want)]

        picked = None
        source = ""
        if exact:
            picked, source = exact[0], "国名精确匹配"
        elif loose:
            picked, source = loose[0], "国名近似匹配"
        elif capital:
            for r in rows:
                try:
                    if ag.d(r["addr"] + OFF_CAPITAL) == capital:
                        picked, source = r, "首都匹配"
                        break
                except (nekoagent.AgentError, RuntimeError):
                    continue

        if picked is None:
            if not want:
                # 没名字就别乱猜，更别把 400 个国家名糊到用户脸上——
                # 这时候正确的动作是"去读存档"，或者直接问他玩哪国。
                raise LiveError(
                    "no_country",
                    "我还不知道你这局玩的是哪个国家——手上没有一份能用的存档。",
                    "让用户在游戏里存一次档（或者等我把存档读完），"
                    "也可以直接问他「你这局玩的是哪国」。",
                )
            sample = "、".join(sorted({r["short"] for r in rows if r["short"]})[:14])
            raise LiveError(
                "no_country",
                f"没在内存里认出「{want}」。内存里读到的国家有：{sample}…",
                "问用户他这局玩的是哪国；或者先存个档让我读一下。",
            )

        self._cache_write({
            "pid": ag.pid, "base": ag.base, "country": picked["addr"],
            "tag": player_tag, "short": picked["short"], "at": int(time.time()),
        })
        self._log(f"玩家国家定位：{picked['short']} @ {picked['addr']:#x}（{source}）")
        c = self._cache_read() or {}
        confirmed = (c.get("confirmed") or {}).get("addr") == picked["addr"]
        return {**picked, "source": source, "confirmed": confirmed, "candidates": []}

    def confirmed_country(self) -> dict | None:
        """上次用户亲口认过、而且还在同一局里（pid + base 没变）的那个国家。

        为什么它比"存档里的玩家名"优先级高：存档可能是上一局的，
        而用户上一句话刚说过"我玩的是德国"。

        0.9.7：**不拿 `_lock`**。这里只读一份 JSON 缓存 + 两条内存读，
        缓存都是整体替换（原子赋值）；DLL 端本来就全局串行。
        拿锁的代价是：上一次工具 `wait_for` 超时后，那个取消不了的
        `to_thread` 线程还握着锁，后面所有工具连认人都得排队 12 秒。
        """
        with contextlib.nullcontext():
            try:
                ag = self.connect()
            except (nekoagent.AgentError, OSError):
                return None
            try:
                c = self._cache_read()
                conf = c.get("confirmed") or {}
                addr = int(conf.get("addr") or 0)
                if not addr or c.get("pid") != ag.pid or int(c.get("base") or 0) != ag.base:
                    return None
                short = self._sstr(ag, addr + OFF_NAME_SHORT)
                if not short:
                    return None
                return {"addr": addr, "short": short, "full": "",
                        "source": "上次你确认过的", "confirmed": True, "candidates": []}
            except (nekoagent.AgentError, RuntimeError):
                return None
            finally:
                ag.close()

    def confirm(self, addr: int, name: str) -> None:
        """用户亲口认了这个国家。同一局内不再问第二遍。

        只在"用户说了国名"时调用——缓存不能自我确认，那等于让猜的变成真的。
        """
        c = self._cache_read() or {}
        c["confirmed"] = {"addr": addr, "name": name, "at": int(time.time())}
        self._cache_write(c)

    # ── 国策 ──────────────────────────────────

    def _progress(self, ag: nekoagent.Agent, country: int) -> int:
        p = ag.q(country + OFF_PROGRESS)
        if not _heap(p):
            raise LiveError("failed", "读不到国策进度对象（这个国家可能还没有国策树）。",
                            "换个存档或者等游戏加载完再试。")
        return p

    def focus_state(self, country: int) -> dict:
        with self._lock:
            ag = self.connect()
            try:
                p = self._progress(ag, country)
                cur = ag.q(p + OFF_CUR)
                cur = cur if _heap(cur) else 0
                days = ag.d(p + OFF_DAYS)

                data = ag.q(p + OFF_LIST)
                size = ag.d(p + OFF_LIST + 0x0C)
                items: list[dict] = []
                if _heap(data) and 0 < size < 512:
                    ptrs = struct.unpack_from(f"<{size}Q", ag.read(data, size * 8))
                    for i, o in enumerate(ptrs):
                        if not _heap(o):
                            continue
                        try:
                            items.append({"i": i, "addr": o, "name": self._focus_name(ag, o)})
                        except (nekoagent.AgentError, RuntimeError):
                            items.append({"i": i, "addr": o, "name": ""})

                cur_name = ""
                if cur:
                    try:
                        cur_name = self._focus_name(ag, cur)
                    except (nekoagent.AgentError, RuntimeError):
                        cur_name = ""

                # 上一次 pick 有没有被引擎采纳？
                # 写 +0x18 之后引擎会在下一次 tick 校验条件（前置、政治点、互斥），
                # 不满足就把指针清掉。**暂停状态下看不到这件事**，所以只能下次来的时候对账。
                notice = ""
                last = (self._cache_read() or {}).get("last_pick") or {}
                if (not cur and last.get("addr")
                        and time.time() - float(last.get("at") or 0) < 1800
                        and int(last.get("pid") or 0) == ag.pid):
                    if any(it["addr"] == int(last["addr"]) for it in items):
                        notice = (
                            f"上次选的「{last.get('name') or '那个国策'}」没留下来——"
                            "节点还在可选列表里，说明是引擎自己把它丢掉的"
                            "（前置没满足、政治点不够，或者跟你现在的局势冲突）。"
                            "换一个能点的吧。"
                        )
                return {
                    "current": cur, "current_name": cur_name,
                    "progress": days, "choices": items, "notice": notice,
                }
            finally:
                ag.close()

    def _focus_name(self, ag: nekoagent.Agent, p: int) -> str:
        """国策名是中文 std::string，里面还有 [GetAutobahnName] 这种 loc 变量。

        短名在 +0x50（内联），长名 +0x60 是堆指针。直接读 +0x50 的 std::string
        两种都能覆盖：cap>15 时首 8 字节就是堆指针。
        """
        import re
        name = self._sstr(ag, p + 0x50, limit=512)
        if not name:
            # 有的版本把名字放在 +0x60，兜一下
            name = self._sstr(ag, p + 0x60, limit=512)
        return re.sub(r"\[[^\]]+\]", "…", name).strip()

    def focus_pick(self, country: int, want: str) -> dict:
        """选一个国策。want 可以是列表序号，也可以是名字片段。"""
        self._guard_write("选国策")
        with self._lock:
            ag = self.connect()
            try:
                p = self._progress(ag, country)
                data = ag.q(p + OFF_LIST)
                size = ag.d(p + OFF_LIST + 0x0C)
                if not (_heap(data) and 0 < size < 512):
                    raise LiveError("failed", "读不到当前可选的国策列表。",
                                    "让用户切到国策树看一眼，或者等游戏加载完。")
                ptrs = struct.unpack_from(f"<{size}Q", ag.read(data, size * 8))
                items = []
                for i, o in enumerate(ptrs):
                    if not _heap(o):
                        continue
                    try:
                        items.append((o, self._focus_name(ag, o)))
                    except (nekoagent.AgentError, RuntimeError):
                        items.append((o, ""))

                hit = None
                want = (want or "").strip()
                if want.isdigit() and int(want) < len(items):
                    hit = items[int(want)]
                elif want:
                    for addr, nm in items:
                        if want in nm or nm in want:
                            hit = (addr, nm)
                            break
                if hit is None:
                    listing = "、".join(f"{i}.{nm or '(无名)'}" for i, (_a, nm) in enumerate(items))
                    raise LiveError(
                        "bad_input",
                        f"现在能选的国策里没有「{want}」。可选的是：{listing}。",
                        "把这份列表给用户，让他挑一个；不要自己编名字重试。",
                    )

                addr, nm = hit
                old = ag.q(p + OFF_CUR)
                old = old if _heap(old) else 0
                ag.write(p + OFF_CUR, struct.pack("<Q", addr))
                ag.write(p + OFF_DAYS, struct.pack("<I", 0))

                got = ag.q(p + OFF_CUR)
                if got != addr:
                    raise LiveError("failed", "写进去了但马上被引擎改回去了——多半是这个国策不满足前置条件。",
                                    "让用户看看国策树里这个节点是不是灰的。")
                old_name = ""
                if old:
                    try:
                        old_name = self._focus_name(ag, old)
                    except (nekoagent.AgentError, RuntimeError):
                        old_name = ""

                c = self._cache_read() or {}
                c["last_pick"] = {"addr": addr, "name": nm,
                                  "at": int(time.time()), "pid": ag.pid}
                self._cache_write(c)
                return {"name": nm, "addr": addr, "replaced": old_name}
            finally:
                ag.close()

    def focus_cancel(self, country: int) -> dict:
        self._guard_write("取消国策")
        with self._lock:
            ag = self.connect()
            try:
                p = self._progress(ag, country)
                old = ag.q(p + OFF_CUR)
                old = old if _heap(old) else 0
                name = ""
                if old:
                    try:
                        name = self._focus_name(ag, old)
                    except (nekoagent.AgentError, RuntimeError):
                        name = ""
                ag.write(p + OFF_CUR, struct.pack("<Q", 0))
                ag.write(p + OFF_DAYS, struct.pack("<I", 0))
                return {"cancelled": name}
            finally:
                ag.close()

    # ── 军队 ──────────────────────────────────

    def units(self, country: int) -> list[dict]:
        with self._lock:
            ag = self.connect()
            try:
                return self._units(ag, country)
            finally:
                ag.close()

    def _units(self, ag: nekoagent.Agent, country: int) -> list[dict]:
        data = ag.q(country + OFF_UNITS)
        _cap, size = struct.unpack("<II", ag.read(country + OFF_UNITS + 8, 8))
        if not _heap(data) or not 0 < size < 4096:
            return []
        ptrs = struct.unpack_from(f"<{size}Q", ag.read(data, size * 8))
        out = []
        for idx, u in enumerate(ptrs):
            if not _heap(u):
                continue
            try:
                out.append(self._describe(ag, u, idx))
            except (nekoagent.AgentError, RuntimeError):
                continue
        return out

    # ── 编制 / 兵种 ────────────────────────────

    def templates(self, country: int) -> list[dict]:
        """玩家所有编制：名字、图标兵种、编成、几个师、在哪几个省。"""
        with self._lock:
            ag = self.connect()
            try:
                return self._templates(ag, country)
            finally:
                ag.close()

    def _templates(self, ag: nekoagent.Agent, country: int) -> list[dict]:
        by: dict[int, dict] = {}
        for u in self._units(ag, country):
            key = int(u.get("tpl") or 0)
            row = by.get(key)
            if row is None:
                row = by[key] = {
                    "tpl": key,
                    "template": u.get("template") or "（读不到编制）",
                    "kind": u.get("kind") or "未知",
                    "comp": u.get("comp") or [],
                    "count": 0,
                    "provinces": {},
                }
            row["count"] += 1
            p = int(u["prov"])
            row["provinces"][p] = row["provinces"].get(p, 0) + 1
        rows = sorted(by.values(), key=lambda r: -r["count"])
        for r in rows:
            r["provinces"] = sorted(r["provinces"].items(),
                                    key=lambda kv: -kv[1])[:6]
        return rows

    # ── 交火 ──────────────────────────────────

    def combats(self) -> list[dict]:
        """正在进行的陆战：每场一个 {addr, prov}。"""
        with self._lock:
            ag = self.connect()
            try:
                return self._combats(ag)
            finally:
                ag.close()

    def _combats(self, ag: nekoagent.Agent) -> list[dict]:
        mgr = self._combat_manager(ag)
        if not mgr:
            raise LiveError("failed",
                            "这一局没读到战斗管理器：可能是刚进游戏还没开打、"
                            "人在主菜单，或者这版游戏的偏移对不上。",
                            "先按「体检」看版本闸；刚开局就等一会儿再问，别信任何前线数据。")
        try:
            data, cap, size = struct.unpack("<QII", ag.read(mgr + 0x10, 16))
        except (nekoagent.AgentError, RuntimeError):
            return []
        if not _heap(data) or not (0 < size <= 512) or size > cap:
            return []
        try:
            ptrs = struct.unpack(f"<{size}Q", ag.read(data, size * 8))
        except (nekoagent.AgentError, RuntimeError):
            return []
        # +0x10 是「所有战斗」的数组，CLandCombat 和 CNavalCombat 混着放。
        # 海战对象没有 +0x40 的省份指针，硬读会拿到垃圾省号冒充交火地区。
        land_vt = ag.base + self._vt("CLandCombat")
        out = []
        for c in ptrs:
            if not _heap(c):
                continue
            try:
                if ag.q(c) != land_vt:
                    continue
                prov = ag.q(c + 0x40)
                pid = ag.d(prov + OFF_PROV_ID) if _heap(prov) else 0
            except (nekoagent.AgentError, RuntimeError):
                continue
            if pid:
                out.append({"addr": c, "prov": pid, "prov_ptr": prov})
        return out

    def _combat_manager(self, ag: nekoagent.Agent) -> int:
        """找出真正的 CCombatManager（堆上只有一个，另一个是栈上的假阳性）。"""
        c = self._cache_read()
        addr = int(c.get("combat_mgr") or 0)
        if (addr and c.get("pid") == ag.pid and int(c.get("base") or 0) == ag.base
                and self._looks_like_combat_manager(ag, addr)):
            return addr
        for h in ag.find_instances(self._vt("CCombatManager")):
            if h <= 0x10000000000:
                continue
            if self._looks_like_combat_manager(ag, h):
                c.update({"combat_mgr": h, "pid": ag.pid, "base": ag.base})
                self._cache_write(c)
                return h
        return 0

    def _looks_like_combat_manager(self, ag: nekoagent.Agent, addr: int) -> bool:
        """只验结构，不验第一个战斗的类型。

        0.9.4 踩过一次：`+0x10` 装的是**全部战斗**，CLandCombat（陆战）和
        CNavalCombat（海战）混在一起，顺序还会随战斗增删变化。真机上 27 场里
        7 场是海战，第 0 个恰好是海战时，原来那句「first_vt == CLandCombat」
        会把整个管理器否掉，然后报出「偏移可能变了」这种误导性结论。
        挑陆战交给 `_combats()` 按 vtable 过滤，这里只认「像不像一个数组」。
        """
        try:
            data, cap, size = struct.unpack("<QII", ag.read(addr + 0x10, 16))
            if not _heap(data) or not (0 <= size <= 512) or size > cap or cap > 4096:
                return False
            if size == 0:
                return True
            n = min(size, 4)
            sample = struct.unpack(f"<{n}Q", ag.read(data, n * 8))
            return any(_heap(p) for p in sample)
        except (nekoagent.AgentError, RuntimeError):
            return False

    # ── 全服部队分布 / 前线快照 ─────────────────

    def _countries_cached(self, ag: nekoagent.Agent, ttl: float = 180.0) -> list[int]:
        stamp, addrs = self._countries_cache
        if addrs and time.time() - stamp < ttl:
            return addrs
        addrs = self._countries(ag)
        self._countries_cache = (time.time(), addrs)
        return addrs

    def units_by_prov(self, ttl: float = 90.0,
                      peek_only: bool = False) -> dict[int, dict[int, int]]:
        """省 -> {国家地址: 师数}。一次全服扫描，缓存 ttl 秒。

        `peek_only` 给只读工具用：冷缓存时宁可让 `sides` 留空，也别为了
        「省里恰好有哪些师」再花 15 秒去扫全服，把宿主 30 秒看门狗撞穿。
        攻守方、控制者都不依赖这张表，缺了不影响主线结论。
        """
        if peek_only:
            stamp, _pid, _base, cached = self._units_by_prov_cache
            return cached if (cached and time.time() - stamp < ttl) else {}
        with self._lock:
            ag = self.connect()      # 0.9.14：先连接——换局时 connect 会清缓存，
            try:                     # 再校验 (pid, base)，重开游戏不会吃到上一局的分布
                stamp, pid, base, cached = self._units_by_prov_cache
                if (cached and pid == ag.pid and base == ag.base
                        and time.time() - stamp < ttl):
                    return cached
                out: dict[int, dict[int, int]] = {}
                for i, caddr in enumerate(self._countries_cached(ag)):
                    # 工具查询期间主动收手：DLL 端所有命令串行，这份扫描
                    # 真机 15 秒，会把工具每条 RD 都堵在后面。
                    if (i & 7) == 0 and self.tool_active():
                        return {}          # 半成品不写缓存，下一轮重扫
                    try:
                        for _u, prov in self._units_light(ag, caddr):
                            slot = out.setdefault(int(prov), {})
                            slot[caddr] = slot.get(caddr, 0) + 1
                    except (nekoagent.AgentError, RuntimeError):
                        continue
                self._units_by_prov_cache = (time.time(), ag.pid, ag.base, out)
                return out
            finally:
                ag.close()

    def peek_dist(self, max_age: float = 300.0) -> tuple[float, dict]:
        """只窥常驻分布表、不连游戏（0.9.14）。

        返回 (数据年龄秒, 省→{国家addr:师数})；缓存冷/过期返回 (-1, {})。
        给局势简报的「主力在（实时）」覆盖层用——那条路必须零内存读，
        不能因为要标一行实时数据就把整个简报查询拖进 15 秒扫描。
        """
        stamp, _pid, _base, cached = self._units_by_prov_cache
        if cached:
            age = time.time() - stamp
            if age <= max_age:
                return age, cached
        return -1.0, {}

    def peek_named(self, max_age: float = 900.0) -> list[dict]:
        """只窥国家名行缓存、不连游戏（0.9.14）。冷/过期返回 []。"""
        stamp, _pid, _base, rows = self._named_rows_cache
        if rows and time.time() - stamp <= max_age:
            return rows
        return []

    def invalidate_dist(self) -> None:
        """分布表立刻作废，不等 TTL（0.9.14）。

        哨兵发现游戏退出时调——peek_dist 不连接、没法校验会话键，
        游戏关掉后这张表必须马上清掉，别让局势简报把上一局的分布
        当"实时"报出去。
        """
        self._units_by_prov_cache = (0.0, 0, 0, {})

    # ── 控制者 / 攻守双方（0.9.3） ──────────────

    def _country_names(self, ag: nekoagent.Agent, ttl: float = 300.0) -> dict[int, str]:
        """国家序号 → 短名。CCountry+0x18 就是 CCountryTag 的整数值。

        堆上 406 个实例里混着极少量 vtable 残留，但实测同一序号不会撞名字；
        过滤掉没名字/没序号的即可。拿不到的序号交给上层报「国家#N」，不猜。
        """
        stamp, pid, base, table = self._country_names_cache
        if table and time.time() - stamp < ttl and pid == ag.pid and base == ag.base:
            return table
        out: dict[int, str] = {}
        for c in self._countries_cached(ag):
            try:
                nm = self._sstr(ag, c + OFF_NAME_SHORT)
                cid = ag.d(c + OFF_COUNTRY_ID)
            except (nekoagent.AgentError, RuntimeError):
                continue
            if nm and 0 < cid < 10000:
                out.setdefault(cid, nm)
        self._country_names_cache = (time.time(), ag.pid, ag.base, out)
        return out

    def _army_countries(self, ag: nekoagent.Agent, ttl: float = 180.0) -> dict[int, tuple]:
        """CArmy* → (国家地址, 短名, 国家序号)。走 CCountry+0x2F8 的全师表。

        别用 CArmy+0x350 → CTheatre → +0x50 → CCountry 那条链反查，真机上
        读出来是空的；全师表才是验证过的那条。
        """
        stamp, pid, base, table = self._army_countries_cache
        if table and time.time() - stamp < ttl and pid == ag.pid and base == ag.base:
            return table
        out: dict[int, tuple] = {}
        for c in self._countries_cached(ag):
            try:
                nm = self._sstr(ag, c + OFF_NAME_SHORT)
                cid = ag.d(c + OFF_COUNTRY_ID)
                data, cap, size = struct.unpack("<QII", ag.read(c + OFF_UNITS, 16))
            except (nekoagent.AgentError, RuntimeError, struct.error):
                continue
            if not _heap(data) or not (0 < size <= 20000) or size > cap:
                continue
            try:
                ptrs = struct.unpack(f"<{size}Q", ag.read(data, size * 8))
            except (nekoagent.AgentError, RuntimeError, struct.error):
                continue
            for a in ptrs:
                if _heap(a):
                    out[a] = (c, nm or f"国家#{cid}", cid)
        self._army_countries_cache = (time.time(), ag.pid, ag.base, out)
        return out

    def _controller_of(self, ag: nekoagent.Agent, prov: int,
                       names: dict[int, str]) -> dict:
        """CProvince* → 控制者 / 法理所有者。读不到就留空，绝不猜。"""
        info = {"controller": "", "controller_id": 0, "owner": "", "owner_id": 0}
        if not _heap(prov):
            return info
        try:
            cid = ag.d(prov + OFF_PROV_CTRL_ID)
            st = ag.q(prov + OFF_PROV_STATE)
            oid = ag.d(st + OFF_STATE_OWNER) if _heap(st) else 0
        except (nekoagent.AgentError, RuntimeError):
            return info
        if 0 < cid < 10000:
            info["controller_id"] = cid
            info["controller"] = names.get(cid, "")
        if 0 < oid < 10000:
            info["owner_id"] = oid
            info["owner"] = names.get(oid, "")
        return info

    def _combat_sides(self, ag: nekoagent.Agent, combat: int,
                      army_countries: dict[int, tuple]) -> dict:
        """CLandCombat* → {attacker: {国名: 师数}, defender: {...}}。

        +0x30 攻 / +0x38 守：控制国 9 次全落在 +0x38、0 次在 +0x30（真机 17 场）。
        参战师在 combatant+0x20 指向的数组里，不在对象内联区域——之前在对象里
        直接找 CArmy* 所以一直以为「CLandCombatant 里没有国家」。
        """
        out: dict[str, dict[str, int]] = {}
        for key, off in (("attacker", OFF_COMBAT_ATTACKER),
                         ("defender", OFF_COMBAT_DEFENDER)):
            tally: dict[str, int] = {}
            try:
                cb = ag.q(combat + off)
                if not _heap(cb):
                    out[key] = tally
                    continue
                data = ag.q(cb + OFF_COMBATANT_ARMIES)
                cap = ag.d(cb + OFF_COMBATANT_CAP)
                size = ag.d(cb + OFF_COMBATANT_SIZE)
                if not (_heap(data) and 0 < size <= 256 and size <= cap):
                    out[key] = tally
                    continue
                ptrs = struct.unpack(f"<{size}Q", ag.read(data, size * 8))
            except (nekoagent.AgentError, RuntimeError, struct.error):
                out[key] = tally
                continue
            for a in ptrs:
                got = army_countries.get(a)
                if got:
                    tally[got[1]] = tally.get(got[1], 0) + 1
            out[key] = tally
        return out

    # ── 全服控制版图（0.9.4） ───────────────────

    def control_map_background(self) -> bool:
        """起一个后台线程刷新全服版图；已经在扫就别再起一个。

        工具不该为 8 秒的扫描干等。哨兵每 30 秒也会扫一次把缓存养热，
        这里只是工具第一次问、缓存还空时的补位。
        """
        if self.tool_active():
            return False
        with self._ctl_bg_lock:
            if self._ctl_bg_running:
                return False
            self._ctl_bg_running = True

        def _run() -> None:
            try:
                self.control_map()
            finally:
                with self._ctl_bg_lock:
                    self._ctl_bg_running = False

        threading.Thread(target=_run, daemon=True, name="hoi4-control-map").start()
        return True

    def control_map(self, ttl: float = 45.0, peek_only: bool = False) -> dict:
        """全服：省号 → 控制国序号 + 各国控制省数 + 序号→国名。

        `CProvince+0x160` 是控制国 id（10.12 真机验证：13376 个省、277 个
        控制区，区内零不一致）。省号在 `+0xAC`、控制国在 `+0x160`，
        每省需要 `[+0xAC, +0x164)` 共 0xB8 字节。

        **CProvince 是 0x1E0 步长的连续对象池**（`_pool_runs` 自动推断步长），
        所以按整段一次读、回来再切片；逐省一次 RD 实测 26 秒，整段读能压到
        1 秒级。段里有不可读页就只把那一段退回逐省读，不影响别的段。

        缓存按 `(游戏 pid, exe 基址)` 失效 —— 重开一局不会带着旧版图。
        扫描失败返回 `ok=False`，**不返回上一份缓存**，免得把旧局当现在。
        只读内存，不调用任何引擎函数。
        """
        empty = {"ok": False, "total": 0, "prov_ctrl": {}, "counts": {},
                 "names": {}, "skipped": 0, "elapsed": 0.0, "error": ""}

        def yield_to_tool(where: str) -> dict:
            """工具在查就主动放弃本轮扫描：不写缓存、不算失败，下一轮再扫。

            DLL 的 g_cmd_lock 把每条命令都串行了，一次 SCANALL 能占住好几秒。
            后台扫描不让位，工具的每条 RD 都得排在几百条命令后面。
            """
            out = dict(empty)
            out["error"] = f"让位给工具查询（{where}）"
            out["pending"] = True
            return out

        if peek_only:
            # 给工具用：全服扫描 8 秒，跟 frontline() 的 9 秒串起来会撞宿主
            # 30 秒看门狗。这里只读缓存，没扫好就让工具先报交火、后台补版图。
            # **不拿 _ctl_lock**：后台扫描会握着它 20 多秒，四元组赋值本身是
            # 原子的，没必要让工具为读一份缓存排到扫描结束（0.9.7 修的超时）。
            ag = self.connect()
            try:
                stamp, cpid, cbase, cached = self._control_map_cache
                valid = max(ttl, float((cached or {}).get("_effective_ttl", 0)))
                if (cached and cached.get("ok") and time.time() - stamp < valid
                        and cpid == ag.pid and cbase == ag.base):
                    return cached
            finally:
                ag.close()
            out = dict(empty)
            out["error"] = "全服版图还在后台更新"
            out["pending"] = True
            return out

        with self._ctl_lock:
            if self.tool_active():
                return yield_to_tool("扫描开始前")
            ag = self.connect()
            try:
                stamp, cpid, cbase, cached = self._control_map_cache
                valid = max(ttl, float((cached or {}).get("_effective_ttl", 0)))
                if (cached and cached.get("ok") and time.time() - stamp < valid
                        and cpid == ag.pid and cbase == ag.base):
                    return cached
                if self.tool_active():
                    return yield_to_tool("取省列表前")
                t0 = time.time()
                addrs = sorted(p for p in ag.find_instances(VT_PROVINCE)
                               if _heap(p))
                span = OFF_PROV_CTRL_ID - OFF_PROV_ID + 4      # 0xB8
                off_cid = OFF_PROV_CTRL_ID - OFF_PROV_ID        # 0xB4
                prov_ctrl: dict[int, int] = {}
                skipped = 0

                def take(pid: int, cid: int) -> None:
                    nonlocal skipped
                    if 0 < pid < 100000 and 0 < cid < 10000:
                        prov_ctrl[pid] = cid
                    else:
                        skipped += 1       # vtable 残留 / 海省等无主省

                stride, runs, singles = _pool_runs(addrs)
                batched = 0
                for start, count in runs:
                    if self.tool_active():
                        return yield_to_tool("整段读途中")
                    need = (count - 1) * stride + span
                    try:
                        buf = ag.read(start + OFF_PROV_ID, need)
                    except (nekoagent.AgentError, RuntimeError):
                        buf = b""
                    if len(buf) < need:
                        # 整段读失败时按 64 个一组重读。旧代码直接把整段
                        # 554 个对象退回逐省读，一次失败就多几百条命令：
                        # 11-09 20:54 实测零散 840 个、控制图耗时 66.94 秒。
                        group = 64
                        for g0 in range(0, count, group):
                            n = min(group, count - g0)
                            gneed = (n - 1) * stride + span
                            try:
                                gbuf = ag.read(start + g0 * stride + OFF_PROV_ID, gneed)
                            except (nekoagent.AgentError, RuntimeError):
                                gbuf = b""
                            if len(gbuf) < gneed:
                                singles.extend(start + (g0 + j) * stride
                                               for j in range(n))
                                continue
                            for j in range(n):
                                off = j * stride
                                take(struct.unpack_from("<I", gbuf, off)[0],
                                     struct.unpack_from("<I", gbuf, off + off_cid)[0])
                            batched += n
                        continue
                    for i in range(count):
                        off = i * stride
                        take(struct.unpack_from("<I", buf, off)[0],
                             struct.unpack_from("<I", buf, off + off_cid)[0])
                    batched += count
                for i, p in enumerate(singles):
                    if (i & 15) == 0 and self.tool_active():
                        return yield_to_tool("零散读途中")
                    try:
                        b = ag.read(p + OFF_PROV_ID, span)
                    except (nekoagent.AgentError, RuntimeError):
                        skipped += 1
                        continue
                    if len(b) < span:
                        skipped += 1
                        continue
                    take(struct.unpack_from("<I", b, 0)[0],
                         struct.unpack_from("<I", b, off_cid)[0])
                if not prov_ctrl:
                    raise LiveError("empty", "全服控制图一个省都没读到",
                                    "八成是版本偏移变了，别信任何控制者结论")
                if self.tool_active():
                    return yield_to_tool("收尾前")
                names = self._country_names(ag)
                counts: dict[int, int] = {}
                for cid in prov_ctrl.values():
                    counts[cid] = counts.get(cid, 0) + 1
                out = {
                    "ok": True, "total": len(prov_ctrl), "prov_ctrl": prov_ctrl,
                    "counts": counts, "names": names, "skipped": skipped,
                    "elapsed": round(time.time() - t0, 2), "error": "",
                    "pid": ag.pid, "base": ag.base,
                    "batched": batched, "runs": len(runs), "stride": stride,
                    "singles": len(singles),
                    # 真机上扫描若比 ttl 还慢，自动把下次间隔放宽到两倍耗时，
                    # 免得每轮 tick 都在扫、把交火通报全挤掉
                    "_effective_ttl": max(ttl, (time.time() - t0) * 2),
                }
                self._control_map_cache = (time.time(), ag.pid, ag.base, out)
                self._control_scan_failures = 0
                self._log(f"全服控制图：{len(prov_ctrl)} 个省、{len(counts)} 个控制国，"
                          f"跳过 {skipped}；整段读 {batched} 个（{len(runs)} 段，"
                          f"步长 {stride:#x}）、零散 {len(singles)} 个，"
                          f"耗时 {out['elapsed']} 秒")
                return out
            except Exception as exc:  # noqa: BLE001 哨兵不能死，但要留计数
                self._control_scan_failures += 1
                lvl = "警告" if self._control_scan_failures >= 3 else "记一笔"
                self._log(f"{lvl}：全服控制图扫描失败（连续 {self._control_scan_failures} 次）"
                          f"{type(exc).__name__}: {exc}")
                out = dict(empty)
                out["error"] = f"{type(exc).__name__}: {exc}"
                return out
            finally:
                ag.close()

    def frontline(self, country: int, fast: bool = False) -> dict:
        """交火总览：省、这块地归谁、法理所有者、攻守双方，外加省内兵力。

        `sides` 保留旧格式（省内有哪些国家的师）给哨兵的 diff 用，
        `attacker` / `defender` 才是真正的战斗双方。
        `fast=True` 时 `sides` 只读缓存（工具用，秒回）；哨兵用默认值把缓存养热。
        """
        up = self.units_by_prov(peek_only=fast)
        # 0.9.7：只读查询不拿 `_lock`。这段里全是只读 + 缓存整体替换，
        # DLL 的 g_cmd_lock 已经保证命令串行；拿锁反而会在上一次工具
        # wait_for 超时（线程取消不了）之后，把后续所有工具堵在门口。
        with contextlib.nullcontext():
            ag = self.connect()
            try:
                combats = self._combats(ag)
                short = self._sstr(ag, country + OFF_NAME_SHORT)
                names = self._country_names(ag)
                army_countries = self._army_countries(ag)
                rows = []
                for c in combats:
                    pid = c["prov"]
                    holders = up.get(pid, {})
                    sides = []
                    for caddr, n in sorted(holders.items(), key=lambda kv: -kv[1])[:4]:
                        nm = self._sstr(ag, caddr + OFF_NAME_SHORT) or f"国家@{caddr:#x}"
                        sides.append({"country": nm, "units": n,
                                      "mine": caddr == country, "addr": caddr})
                    row = {"prov": pid, "sides": sides}
                    row.update(self._controller_of(ag, c.get("prov_ptr", 0), names))
                    row.update(self._combat_sides(ag, c["addr"], army_countries))
                    rows.append(row)
                # 0.9.9：全服陆战先收进「参战范围」，只把本国 + 盟友 + 敌对国
                # 之间的战斗交出去。以前这里直接 return rows，玩家在欧洲
                # 也会收到东亚那边的交火和兵力调动——那不是"她知道不了的
                # 内容"，是根本跟这局没关系，报出来只会让她拿别处的仗当
                # 自己的战况。攻守双方都来自 `_combat_sides()` 反查的国家名。
                scope = warscope.derive(short, rows, prev=self._war_scope)
                self._war_scope = scope
                if short:
                    rows = warscope.filter_rows(rows, scope)
                else:
                    # 连玩家短名都读不出来时别把整份前线吞掉，维持旧行为；
                    # 但标出来源，免得"没过滤"看起来像过滤已经生效。
                    scope.source = "no_player_name"
                try:
                    player_id = ag.d(country + OFF_COUNTRY_ID)
                except (nekoagent.AgentError, RuntimeError):
                    player_id = 0
                return {"player": short, "player_addr": country,
                        "player_id": player_id,
                        "enemies": scope.enemies, "allies": scope.allies,
                        "war_scope": scope.as_dict(),
                        "units_cached": bool(up),
                        "combats": rows, "count": len(rows)}
            finally:
                ag.close()

    def controlled_by(self, short_name: str, ttl: float = 45.0,
                      allow_scan: bool = True, also_names=None) -> set[int]:
        """短名 → 这个国家**及其友邦**控制的省号集合。判"部队走不走得到"用它。

        为什么不能用"部队脚下那几个省"当基准：0.9.22 真机踩过——1939 德国
        铺波兰前线，正面 34 个省全是德国自己的，但接管的第 4 集团军在南边
        斯洛伐克，旧判定只认同已接管部队邻接的 9 个省，剩下 25 个判成
        "够不着"，**整条线一个兵都不摆**。部队走的是本国领土，所以基准要
        是"我方阵营控制的所有省"。

        `also_names` 是友邦短名（盟友/仆从）。有军事通行权，它们的省我们的
        兵也走得进去——波兰正面有 7 个省的控制者是斯洛伐克，不认这一条就
        只能靠"恰好有兵站在那儿"侥幸通过。

        缓存冷的时候允许现扫一次（7~18 秒，调用方有 45 秒预算）。
        """
        short_name = (short_name or "").strip()
        if not short_name:
            return set()
        cm = self.control_map(ttl=ttl, peek_only=True)
        ctrl = cm.get("prov_ctrl") or {}
        names = cm.get("names") or {}
        if (not ctrl or not names) and allow_scan:
            cm = self.control_map(ttl=ttl, peek_only=False)
            ctrl = cm.get("prov_ctrl") or {}
            names = cm.get("names") or {}
        if not ctrl or not names:
            return set()
        want = {short_name} | {str(n) for n in (also_names or ()) if n}
        ids = {cid for cid, n in names.items() if str(n) in want}
        if not ids:
            return set()
        return {int(p) for p, c in ctrl.items() if c in ids}

    def _cached_country_names(self, ag: nekoagent.Agent) -> dict[int, str]:
        """国家序号 → 短名。**只读缓存，绝不触发 SCANALL。**

        工具只有 30 秒预算，而 `_country_names()` 冷的时候要认一遍全服 405 国
        （5~14 秒）——0.9.22 真机实测 `hoi4_move` 和 `hold` 都是这么 TIMEOUT 的。
        版图哨兵每 ~85 秒扫一次控制图，顺手把这张表养热，正常路径下都是热的。
        两份缓存都冷就返回空表，调用方按"查不了"处理。
        """
        try:
            names = self.control_map(ttl=900.0, peek_only=True).get("names") or {}
            if names:
                return names
        except Exception:  # noqa: BLE001
            pass
        stamp, pid, base, table = self._country_names_cache
        if table and pid == ag.pid and base == ag.base:
            return table
        return {}

    def friendly_country_ids(self, ag: nekoagent.Agent,
                             player_addr: int) -> set[int] | None:
        """我方阵营的国家序号：自己 + 同阵营 + 附庸。只吃缓存。

        读不到就返回 None——调用方按"不知道谁是友邦"处理，
        **不要**退化成"只有我自己"（那会把斯洛伐克那几个省判成够不着）。
        """
        friendly = self._friendly_names()
        if not friendly:
            return None
        names = self._cached_country_names(ag)
        if not names:
            self._log("友邦判定跳过：国家名表冷了，不为主这次扫描")
            return None
        try:
            pcid = ag.d(player_addr + OFF_COUNTRY_ID)
        except (nekoagent.AgentError, RuntimeError):
            return None
        if not (0 < pcid < 10000):
            return None
        return {pcid} | {cid for cid, nm in names.items() if nm in friendly}

    def controlled_provs(self, player_addr: int,
                         provs: list[int]) -> set[int]:
        """这些省里，哪些**现在**是玩家自己控制的。逐省直读 `CProvince+0x160`。

        **纯内存、实时、不看存档。** 存档一个月才写一次，拿它判"这块地现在归谁"
        误差太大（0.9.22 用存档法被判过）。控制权会随着占领随时变，必须读内存。

        判据只有一条：`CProvince+0x160`（控制国序号）== `CCountry+0x18`（我自己）。
        这条路**不需要"国家序号→国名"表**，所以不会因为那张表冷掉而全盘失效
        （之前正是栽在这：表一冷，判成"德国自己的省全够不着"）。
        只读不写，几十个省 = 几十次 RD，毫秒级。

        读不到就返回空集，调用方按"这些省不确定"处理。
        """
        want = [int(p) for p in (provs or ())]
        if not want:
            return set()
        try:
            from .battleplan import RVA_GET_PROVINCE   # 懒导入，免循环
        except Exception:
            return set()
        with self._lock:
            ag = self.connect()
            try:
                try:
                    pcid = ag.d(player_addr + OFF_COUNTRY_ID)
                except (nekoagent.AgentError, RuntimeError):
                    return set()
                if not (0 < pcid < 10000):
                    return set()
                fn = ag.base + RVA_GET_PROVINCE
                out: set[int] = set()
                for pid in want:
                    try:
                        p = ag.call(fn, player_addr, pid)
                        if _heap(p) and ag.d(p + OFF_PROV_CTRL_ID) == pcid:
                            out.add(pid)
                    except (nekoagent.AgentError, RuntimeError):
                        continue
                return out
            finally:
                ag.close()

    def ally_names(self) -> set[str]:
        """这局战争里跟玩家同阵营的国家短名。同 enemy_names，读不到就是空集。"""
        sc = self._war_scope
        if sc is None:
            return set()
        return {str(x) for x in (getattr(sc, "allies", None) or ()) if x}

    def enemy_names(self) -> set[str]:
        """这局战争里跟玩家交战的国家短名。读不到就返回空集。

        `_war_scope` 是前线/交火那条链路算出来的（warscope.derive）。没算过
        就是 None——调用方要按"没有敌人"处理，别反过来当成"人人都是敌人"：
        0.9.22 的死锁就是这么来的（addr_short 缓存一冷，全世界的兵都被当成
        敌人，于是没有任何师是"空闲"的，守线命令一条都下不出去）。
        """
        sc = self._war_scope
        if sc is None:
            return set()
        return {str(x) for x in (getattr(sc, "enemies", None) or ()) if x}

    def country_name(self, addr: int) -> str:
        """国家地址 -> 短名（带缓存）。"""
        try:
            if addr in self._name_cache:
                return self._name_cache[addr]
            ag = self.connect()
            try:
                nm = self._sstr(ag, addr + OFF_NAME_SHORT)
            finally:
                ag.close()
            nm = nm or f"国家@{addr:#x}"
            self._name_cache[addr] = nm
            return nm
        except (nekoagent.AgentError, RuntimeError):
            return f"国家@{addr:#x}"

    def _describe(self, ag: nekoagent.Agent, u: int, idx: int) -> dict:
        path: list[int] = []
        data = ag.q(u + OFF_PATH)
        size = ag.d(u + OFF_PSIZE)
        if _heap(data) and 0 < size < 64:
            try:
                path = list(struct.unpack(f"<{size}I", ag.read(data, size * 4)))
            except (nekoagent.AgentError, RuntimeError):
                path = []
        info = self._template_info(ag, u)
        return {
            "idx": idx,
            "addr": u,
            "prov": ag.d(u + OFF_PROV),
            "type": ag.d(u + OFF_TYPE),
            "template": info["name"],
            "kind": info["kind"],
            "tpl": info["ptr"],
            "comp": info["comp"],
            "sub": ag.d(u + OFF_SUBCOUNT),
            "order": ag.d(u + OFF_ORDER_TYPE),
            "path": path,
            "target": ag.d(u + OFF_TARGET),
            "remain": ag.f(u + OFF_REMAIN),
        }

    def _template_info(self, ag: nekoagent.Agent, u: int) -> dict:
        """这个师的编制：名字、图标兵种、编成（营种×营数）。

        读不到就返回空壳，不影响别的字段——编制是 0.9.1 之后才有的东西。
        """
        empty = {"ptr": 0, "name": "", "kind": "", "comp": []}
        try:
            tpl = ag.q(u + OFF_TEMPLATE)
            if not _heap(tpl):
                return empty
            if tpl in self._tpl_cache:
                return self._tpl_cache[tpl]
            name = self._sstr(ag, tpl + OFF_TPL_NAME, 96)
            comp = self._template_comp(ag, tpl)
            info = {"ptr": tpl, "name": name, "kind": icon_of(comp, name), "comp": comp}
            self._tpl_cache[tpl] = info
            return info
        except (nekoagent.AgentError, RuntimeError):
            return empty

    def _template_comp(self, ag: nekoagent.Agent, tpl: int) -> list[dict]:
        """模板 +0x80 的编成数组：[{type: 'medium_armor', count: 6}, …]。"""
        try:
            data, _cap, size = struct.unpack("<QII", ag.read(tpl + OFF_TPL_COMP, 16))
        except (nekoagent.AgentError, RuntimeError):
            return []
        if not _heap(data) or not (0 < size <= 64):
            return []
        try:
            raw = ag.read(data, size * 16)
        except (nekoagent.AgentError, RuntimeError):
            return []
        out = []
        for i in range(size):
            def_ptr, count = struct.unpack_from("<QI", raw, i * 16)
            if not _heap(def_ptr):
                continue
            try:
                t = self._sstr(ag, def_ptr + OFF_SUB_TYPE, 48)
            except (nekoagent.AgentError, RuntimeError):
                t = ""
            if not t:
                continue
            out.append({"type": t, "count": int(count)})
        return out

    # ── 按名字找国家 / 某片范围的部队 ────────

    def _named_rows(self, ag: nekoagent.Agent, ttl: float = 300.0) -> list[dict]:
        """全部国家 (addr, short, full)。只读，不碰玩家缓存。

        0.9.13：结果缓存 ttl 秒（按 pid+base 失效）。真机实测冷扫一遍
        406 国要 5~8 秒，units_near 一次要扫两遍，8 秒超时形同虚设。
        国家地址和名字一局内不变，缓存后热查询 ~0 秒。
        """
        stamp, pid, base, rows = self._named_rows_cache
        if rows and time.time() - stamp < ttl and pid == ag.pid and base == ag.base:
            return rows
        out: list[dict] = []
        for addr in self._countries_cached(ag):
            try:
                short = self._sstr(ag, addr + OFF_NAME_SHORT)
                full = self._sstr(ag, addr + OFF_NAME_FULL) if not short else ""
            except (nekoagent.AgentError, RuntimeError):
                continue
            if not short and not full:
                continue
            out.append({"addr": addr, "short": short or full, "full": full or short})
        # 半成品不写缓存：全世界一个国家都没认到多半是连接出问题，
        # 缓存住会把"认不出任何国家"钉死 5 分钟
        if out:
            self._named_rows_cache = (time.time(), ag.pid, ag.base, out)
        return out

    def locate_named(self, name: str) -> dict | None:
        """按国名找任意一个国家（波兰、苏联都行）。

        和 locate() 的区别：**绝不写玩家缓存**——把"波兰"记成玩家国家，
        之后调兵就会去动波兰的部队，这个错一次都不能犯。
        """
        want = (name or "").strip()
        if not want:
            return None
        with self._lock:
            ag = self.connect()
            try:
                rows = self._named_rows(ag)
            finally:
                ag.close()
        exact = [r for r in rows if want == r["short"] or want == r["full"]]
        if exact:
            return exact[0]
        loose = [r for r in rows if want in r["short"] or want in r["full"]
                 or r["short"] in want or r["full"] in want]
        return loose[0] if loose else None

    def _units_light(self, ag: nekoagent.Agent, country: int) -> list[tuple[int, int]]:
        """只读 (army_addr, prov)，比 _describe 少一个数量级的往返。
        遍历全世界国家时用的就是它。"""
        data = ag.q(country + OFF_UNITS)
        _cap, size = struct.unpack("<II", ag.read(country + OFF_UNITS + 8, 8))
        if not _heap(data) or not 0 < size < 4096:
            return []
        ptrs = struct.unpack_from(f"<{size}Q", ag.read(data, size * 8))
        out: list[tuple[int, int]] = []
        for u in ptrs:
            if not _heap(u):
                continue
            try:
                out.append((u, ag.d(u + OFF_PROV)))
            except (nekoagent.AgentError, RuntimeError):
                continue
        return out

    def country_unit_provs(self, addr: int) -> list[int]:
        """某个国家的部队都驻在哪些省（去重、升序）。问「波兰附近」时用它当种子。"""
        with self._lock:
            ag = self.connect()
            try:
                return sorted({p for _a, p in self._units_light(ag, addr)})
            finally:
                ag.close()

    # ── 师 id 桥（0.9.15） ─────────────────────
    # 存档里有「第9集团军 = 这 17 个师 id」，内存 CArmy+0x24 就是同一个 id。
    # 于是集团军认领不需要逆向 CArmy→ArmyGroup 的指针链（0.9.9 试过，读出来是空）。

    def unit_ids(self, country: int) -> dict[int, int]:
        """玩家所有师：师 id → 单位地址。0.22 秒/228 个，可以每次 tick 读。"""
        with self._lock:
            ag = self.connect()
            try:
                return self._unit_ids(ag, country)
            finally:
                ag.close()

    def _unit_ids(self, ag: nekoagent.Agent, country: int) -> dict[int, int]:
        data = ag.q(country + OFF_UNITS)
        _cap, size = struct.unpack("<II", ag.read(country + OFF_UNITS + 8, 8))
        if not _heap(data) or not 0 < size < 4096:
            return {}
        ptrs = struct.unpack_from(f"<{size}Q", ag.read(data, size * 8))
        out: dict[int, int] = {}
        bad = 0
        for u in ptrs:
            if not _heap(u):
                continue
            try:
                uid = ag.d(u + OFF_UNIT_ID)
            except (nekoagent.AgentError, RuntimeError):
                bad += 1
                continue
            if 0 < uid < 2_000_000:
                out[int(uid)] = u
            else:
                bad += 1
        if bad and not out:
            raise LiveError("failed",
                            "师 id 一个都没读到——这版游戏的 CArmy 偏移可能变了，"
                            "别在这个时候接管部队。")
        return out

    def units_by_id(self, country: int, ids: list[int]) -> list[dict]:
        """按师 id 取那批师的实时信息（地址/省/编制/行军）。认领集团军用。"""
        want = {int(i) for i in ids}
        with self._lock:
            ag = self.connect()
            try:
                addr_of = self._unit_ids(ag, country)
                out: list[dict] = []
                for uid in sorted(want):
                    u = addr_of.get(uid)
                    if u is None:
                        continue
                    try:
                        info = self._describe(ag, u, -1)
                    except (nekoagent.AgentError, RuntimeError):
                        continue
                    info["uid"] = uid
                    out.append(info)
                return out
            finally:
                ag.close()

    def units_near(self, seed_provs: list[int], depth: int = 2,
                   dist_max_age: float = 300.0) -> dict:
        """种子省份往外 depth 跳邻接范围内的部队，敌我都算。

        0.9.14：**优先吃常驻「省→师分布」表**（后台哨兵约 90 秒养一轮，
        工具只做本地过滤，热路径毫秒级）；表冷（刚开局/哨兵没跑起来）才
        退回现扫全世界。返回 {"range", "countries", "source", "age"}，
        countries: [{"name","addr","count","provs":[(省,数)…降序]}]。
        玩家和其他国家一视同仁，调用方负责标"你"。
        """
        adj = self.adjacency()
        scope = {int(p) for p in seed_provs}
        frontier = list(scope)
        for _ in range(max(0, int(depth))):
            nxt: list[int] = []
            for p in frontier:
                for nb in adj.get(p, ()):
                    if nb not in scope:
                        scope.add(nb)
                        nxt.append(nb)
            frontier = nxt

        with self._lock:
            ag = self.connect()      # 换局时 connect 清缓存，下面再校验会话键
            try:
                stamp, pid, base, cached = self._units_by_prov_cache
                age = (time.time() - stamp
                       if cached and pid == ag.pid and base == ag.base else -1.0)
                rows = self._named_rows(ag)      # 缓存命中≈0 秒；phase1 通常已养热
                by_addr = {int(r["addr"]): r for r in rows}
                out: list[dict] = []
                if cached and 0.0 <= age <= dist_max_age:
                    acc: dict[int, dict[int, int]] = {}   # 国家addr -> 省 -> 师数
                    for prov in scope:
                        slot = cached.get(int(prov))
                        if not slot:
                            continue
                        for caddr, n in slot.items():
                            acc.setdefault(int(caddr), {})[int(prov)] = int(n)
                    for caddr, per in acc.items():
                        r = by_addr.get(caddr)
                        out.append({
                            "name": (r or {}).get("short") or f"国家#{caddr:x}",
                            "addr": caddr,
                            "count": sum(per.values()),
                            "provs": sorted(per.items(), key=lambda kv: -kv[1]),
                        })
                    out.sort(key=lambda x: -x["count"])
                    return {"range": len(scope), "countries": out,
                            "source": "cache", "age": round(age)}
                # 冷缓存：现扫（老路径），结果整形成同一套 "provs" 契约。
                # 0.9.14：这一遍本来就把全世界每个单位都读了一次，范围外的
                # 计数顺手落进常驻分布表（零额外读取）——开局第一问之后，
                # 后面的查询全走 cache 路径，不用等哨兵养第一轮。
                dist: dict[int, dict[int, int]] = {}
                for r in rows:
                    per: dict[int, int] = {}
                    for _a, p in self._units_light(ag, r["addr"]):
                        slot = dist.setdefault(int(p), {})
                        slot[r["addr"]] = slot.get(r["addr"], 0) + 1
                        if p in scope:
                            per[int(p)] = per.get(int(p), 0) + 1
                    if per:
                        out.append({
                            "name": r["short"], "addr": r["addr"],
                            "count": sum(per.values()),
                            "provs": sorted(per.items(), key=lambda kv: -kv[1]),
                        })
                if dist:
                    self._units_by_prov_cache = (time.time(), ag.pid, ag.base, dist)
                out.sort(key=lambda x: -x["count"])
                return {"range": len(scope), "countries": out,
                        "source": "fresh", "age": 0}
            finally:
                ag.close()

    # ── 路径 ──────────────────────────────────

    def find_path(self, src: int, dst: int, passable=None) -> list[int]:
        """陆地邻接 BFS。邻接表是本地从 provinces.bmp 算的，不打扰游戏。

        `passable` 是个**可调用对象** `pid -> bool`（通行权判定），给了就只走
        返回 True 的省。为什么不收一个"省号集合"：省号要按控制者筛，而控制者
        得逐省读内存才知道——传集合就得先把全服控制图扫一遍（十几到二十几秒，
        工具路径下还会主动让位）。传判定函数就能**边搜边读、读到才花钱**，
        一条十几段的路线通常只需探几十到几百个省。

        0.9.22 真机踩过：只按邻接 BFS 的话，从摩拉维亚去东普鲁士的路线会一头
        扎进**中立的波兰**（实测 5/22 个师、28 段波兰领土），部队走到边境就卡住，
        等于下了一条走不通的令。波兰不宣战进不去，这种路线必须判"走不通"。

        终点不受 `passable` 限制：打进去的那一格本来就是敌国领土。
        """
        if src == dst:
            return [src]
        adj = self.adjacency()
        prev = {src: -1}
        q = deque([src])
        while q:
            cur = q.popleft()
            for nb in adj.get(cur, ()):
                if nb in prev:
                    continue
                if passable is not None and nb != dst and not passable(nb):
                    continue
                prev[nb] = cur
                if nb == dst:
                    out = [dst]
                    while out[-1] != src:
                        out.append(prev[out[-1]])
                    return list(reversed(out))
                q.append(nb)
        return []

    def make_passable(self, ag: nekoagent.Agent, player_addr: int,
                      from_provs=None, dst: int = 0):
        """造一个 `pid -> bool` 的通行权判定（纯内存）。

        判据（任一成立即可走）：
          1. 这个省**现在**的控制者就是我自己（`+0x160 == CCountry+0x18`）；
          2. 它的控制者**正是我们部队此刻所在省的控制者**——兵能站在那儿，
             就说明那国领土对我们通行（仆从国、盟友、军事通行权都算），
             否则部队一旦驻在别人家就再也规划不出路来（0.9.22 真机：12 个
             师驻在斯洛伐克，结果连回德国的路都算不出来）；
          3. 它的控制者**就是目的地的控制者**——我们能下令打进去的那一格，
             那个国家就已经是交战国，穿过它的领土是合法的。开战之后想把兵
             从本土调到东普鲁士（要横穿波兰走廊）就靠这一条。

        为什么不看存档：存档一个月写一次，控制权随占领随时变，差值太大。
        为什么不查"国家序号→国名"表：那张表要认全服 405 国（5~14 秒），
        工具只有 30 秒预算；而这里根本不需要名字——比序号就够了。

        **缓存挂在实例上、60 秒有效**：`_fill` 会给每个目的地各调一次
        `move_many`，各建闭包就会把几千个省重读一遍，撞穿 30 秒看门狗。
        开缓存时**先用内存控制图批量预填**，缺的省再逐省读。

        读不到控制者时**放行**：宁可漏拦，也别因为一次读失败把路全封死。
        """
        try:
            from .battleplan import RVA_GET_PROVINCE
            fn = ag.base + RVA_GET_PROVINCE
        except Exception:  # noqa: BLE001
            return None
        try:
            pcid = ag.d(player_addr + OFF_COUNTRY_ID)
        except (nekoagent.AgentError, RuntimeError):
            return None
        if not (0 < pcid < 10000):
            return None

        def ctrl(pid: int) -> int:
            try:
                ptr = ag.call(fn, player_addr, int(pid))
                return ag.d(ptr + OFF_PROV_CTRL_ID) if _heap(ptr) else 0
            except (nekoagent.AgentError, RuntimeError):
                return 0

        # 部队脚下那几个省的控制者 + 目的地的控制者：它们的领土对我们通行。
        ok_cids = {pcid}
        for p in (from_provs or ()):
            c = ctrl(p)
            if 0 < c < 10000:
                ok_cids.add(c)
        if dst:
            c = ctrl(dst)
            if 0 < c < 10000:
                ok_cids.add(c)

        # 交战国领土也可以穿：**开战之后从本土去东普鲁士必须横穿波兰走廊**，
        # 不认这一条，东普鲁士那一段永远"走不通"（0.9.22 真机）。
        # 敌国名单来自 warscope（实战交火认出来的），再用**已缓存**的
        # 序号→国名表翻成序号；表冷就算了，绝不为它触发全服扫描。
        hostile = self.enemy_names()
        if hostile:
            try:
                cn = self._cached_country_names(ag)
                for cid, nm in cn.items():
                    if nm in hostile and 0 < cid < 10000:
                        ok_cids.add(cid)
            except Exception:  # noqa: BLE001 拿不到就按"没有敌国"处理
                pass

        key = (ag.pid, ag.base, pcid, tuple(sorted(ok_cids)))
        stamp, cached = self._pass_cache
        if key != self._pass_key or time.time() - stamp > PASS_TTL:
            table: dict[int, bool] = {}
            # 先用**内存控制图**批量预填：它读的就是同一个 `CProvince+0x160`，
            # 只是哨兵已经整段读过一遍（比逐省 CALL 快两个数量级）。
            # 图是内存快照、~85 秒一轮，判"能不能走"完全够；缺的省再逐省补。
            try:
                cm = self.control_map(ttl=180.0, peek_only=True)
                for p, c in (cm.get("prov_ctrl") or {}).items():
                    table[int(p)] = (c in ok_cids)
                if table:
                    self._log(f"通行权用控制图预填 {len(table)} 个省")
            except Exception:  # noqa: BLE001 图不可用就全靠逐省读
                pass
            self._pass_cache = (time.time(), table)
            self._pass_key = key
        else:
            table = cached

        def ok(pid: int) -> bool:
            k = int(pid)
            got = table.get(k)
            if got is not None:
                return got
            res = True
            try:
                ptr = ag.call(fn, player_addr, k)
                if _heap(ptr):
                    res = ag.d(ptr + OFF_PROV_CTRL_ID) in ok_cids
            except (nekoagent.AgentError, RuntimeError):
                res = True                    # 读不到就放行
            table[k] = res
            return res

        return ok

    def _country_label(self, ag: nekoagent.Agent, cid: int) -> str:
        """国家序号 → 名字，**只用来写错误信息**。表冷就退回 "#序号"。

        名字对判定没影响（判定只比序号），所以这里允许退化，绝不为它扫描。
        """
        if not cid:
            return "第三方"
        stamp, pid, base, table = self._country_names_cache
        if table and pid == ag.pid and base == ag.base:
            nm = table.get(cid)
            if nm:
                return str(nm)
        try:
            names = self._cached_country_names(ag)
            if names.get(cid):
                return str(names[cid])
        except Exception:  # noqa: BLE001
            pass
        return f"国家#{cid}"

    def enemy_cids(self, ag: nekoagent.Agent, player_addr: int,
                   allow_scan: bool = False) -> set[int]:
        """交战国的**国家序号**。warscope 给的是国名，用序号表翻过来。

        `allow_scan=False`（默认）时表冷就返回空集——绝不为了它触发全服扫描
        （5~14 秒，撞穿看门狗）。`allow_scan=True` 是给「一键铺前线」这种
        用户主动发起、只做一次的动作用的：宁可慢一次，也不能算不出前线
        然后反过来问用户要省号（0.9.22 真机聊天记录里连问了三次）。
        """
        hostile = self.enemy_names()
        if not hostile:
            return set()
        try:
            cn = self._cached_country_names(ag)
        except Exception:  # noqa: BLE001
            cn = {}
        if not cn and allow_scan:
            try:
                cn = self._country_names(ag)
            except Exception:  # noqa: BLE001
                cn = {}
        return {cid for cid, nm in cn.items()
                if nm in hostile and 0 < cid < 10000}

    def front_provinces(self, ag: nekoagent.Agent, player_addr: int,
                        allow_scan: bool = False) -> list[int]:
        """**前线**＝我方控制、且与交战国控制的省相邻的省。

        纯机械集合：不比大小、不排序、不挑重点，就是把"挨着敌人的那些自家省"
        列出来。0.9.23 加——用户说「铺前线」时，模型以前只能反过来问用户要省号
        （真机聊天记录里连着问了三次），因为它手上没有"前线在哪"这个集合。

        交战国认不出来就返回空表，调用方如实说"还没认出跟谁在打"。
        """
        ecids = self.enemy_cids(ag, player_addr, allow_scan=allow_scan)
        if not ecids:
            return []
        try:
            pcid = ag.d(player_addr + OFF_COUNTRY_ID)
        except (nekoagent.AgentError, RuntimeError):
            return []
        if not (0 < pcid < 10000):
            return []
        try:
            cm = self.control_map(ttl=180.0, peek_only=True)
        except Exception:  # noqa: BLE001
            return []
        ctrl = cm.get("prov_ctrl") or {}
        if not ctrl:
            return []
        adj = self.adjacency()
        out: list[int] = []
        for p, c in ctrl.items():
            if c != pcid:
                continue
            for nb in adj.get(int(p)) or ():
                if ctrl.get(nb) in ecids:
                    out.append(int(p))
                    break
        return sorted(out)

    def _blocking_countries(self, ag: nekoagent.Agent, player_addr: int,
                            path: list[int]) -> list[str]:
        """一条（不合法的）路线里，哪些国家的地盘挡了道。只用来解释原因。"""
        try:
            from .battleplan import RVA_GET_PROVINCE
            fn = ag.base + RVA_GET_PROVINCE
        except Exception:  # noqa: BLE001
            return []
        try:
            pcid = ag.d(player_addr + OFF_COUNTRY_ID)
        except (nekoagent.AgentError, RuntimeError):
            pcid = 0
        out: list[str] = []
        for p in path[:-1]:                 # 终点是打进去的那格，不算挡路
            try:
                ptr = ag.call(fn, player_addr, int(p))
                cid = ag.d(ptr + OFF_PROV_CTRL_ID) if _heap(ptr) else 0
            except (nekoagent.AgentError, RuntimeError):
                continue
            if cid and cid != pcid:
                nm = self._country_label(ag, cid)
                if nm not in out:
                    out.append(nm)
        return out

    # ── 下单 ──────────────────────────────────

    def move_many(self, country: int, unit_addrs: list[int], dst: int,
                  replace: bool = True) -> dict:
        """给一批师下同一个目的地。

        **按出发省分组**：同省的师路径一样，只算一次；不同省必须各算各的。
        0.9.1 起"说州名调全州"会一次带出好几个省的师——以前只有一个出发省的
        假设不再成立，拿第一个师的路径去套别的省就是把令下错地方。

        输入容器每组分配一次反复复用——引擎在下单时会把内容拷走，
        所以同一个 vec 连着下十次是安全的（实测过 5 个师同省）。
        """
        if not unit_addrs:
            raise LiveError("bad_input", "没给我要调的部队。")
        self._guard_write("调兵")
        with self._lock:
            ag = self.connect()
            try:
                by_prov: dict[int, list[int]] = {}
                for u in unit_addrs:
                    info = self._describe(ag, u, -1)
                    by_prov.setdefault(int(info["prov"]), []).append(u)
                if not by_prov:
                    raise LiveError("failed", "一个师的当前位置都没读出来。")

                build = self._build()
                rva_new = int(build["rva_new"])
                rva_apply = int(build["rva_apply"])
                order_type = int(build.get("order_type", 2))

                # 目的地现在归谁？**不是我们的地盘 = 这是一步进攻**：引擎会
                # 收下订单，但只有对控制国宣战之后才会真的开进去；没宣战它就
                # 一直挂着不动。0.9.22 真机：用户看到"已下令"却一直没动，正是
                # 这个原因——必须当场说清，别让他以为命令丢了。
                dst_ours, dst_owner = True, ""
                try:
                    from .battleplan import RVA_GET_PROVINCE as _GP
                    pc = ag.d(country + OFF_COUNTRY_ID)
                    ptr = ag.call(ag.base + _GP, country, int(dst))
                    if _heap(ptr) and pc:
                        cid = ag.d(ptr + OFF_PROV_CTRL_ID)
                        dst_ours = (cid == pc)
                        if not dst_ours:
                            dst_owner = self._country_label(ag, cid)
                except (nekoagent.AgentError, RuntimeError, ImportError):
                    pass

                results: list[dict] = []
                paths: dict[int, list[int]] = {}
                # 通行权：只走**现在对我们通行**的省（读内存，实时）。
                # 部队脚下那些省的控制者也算——兵站在人家家里，就说明走得进去。
                # 终点豁免——打进去的那一格本来就该是别人的地盘。
                checker = self.make_passable(ag, country, list(by_prov.keys()), dst)
                if checker is None:
                    self._log("通行权判定跳过：读不到自己的国家序号，路线只按邻接算")
                for src, units in by_prov.items():
                    full = self.find_path(src, dst, checker)
                    if not full:
                        # 走不通：看看到底是哪国挡着，别只说"没有通路"
                        why = "没有陆地通路"
                        if checker is not None:
                            free = self.find_path(src, dst)
                            if free:
                                blocked = self._blocking_countries(
                                    ag, country, free)
                                who = "、".join(blocked) or "第三方"
                                why = (f"没有合法陆路（最近的一条要穿过{who}的领土，"
                                       f"没有军事通行权；先宣战或改走别的目的地）")
                        for u in units:
                            results.append({"unit": u, "ok": False,
                                            "error": f"从省 {src} 到省 {dst} {why}"})
                        continue
                    paths[src] = full
                    targets = full[1:] or [dst]

                    # 输入容器：{data, capacity, size, allocator}
                    cell = ag.malloc(4 * len(targets), rva_new)
                    vec = ag.malloc(0x20, rva_new)
                    if not (_heap(cell) and _heap(vec)):
                        for u in units:
                            results.append({"unit": u, "ok": False,
                                            "error": "引擎的内存分配返回了空指针，这一单不敢下"})
                        continue
                    ag.write(cell, struct.pack(f"<{len(targets)}I", *targets))
                    ag.write(vec, struct.pack("<QIIQ", cell, len(targets), len(targets), 0))

                    for u in units:
                        try:
                            ag.call(ag.base + rva_apply, u, vec,
                                    1 if replace else 0, order_type)
                            after = self._describe(ag, u, -1)
                            results.append({
                                "unit": u,
                                "ok": after["order"] != 0 or bool(after["path"]),
                                "path": after["path"],
                                "order": after["order"],
                            })
                        except (nekoagent.AgentError, RuntimeError) as exc:
                            results.append({"unit": u, "ok": False, "error": str(exc)})

                first_src = next(iter(paths), 0)
                return {"from": first_src, "to": dst,
                        "path": paths.get(first_src, []),
                        "paths": paths, "results": results,
                        "dst_ours": dst_ours, "dst_owner": dst_owner}
            finally:
                ag.close()

    def move(self, country: int, unit_addr: int, dst: int, replace: bool = True) -> dict:
        """单个师。kept 成单独方法是因为调试时一次一个更好读。"""
        r = self.move_many(country, [unit_addr], dst, replace)
        one = r["results"][0]
        return {"from": r["from"], "to": r["to"], "path": r["path"],
                "ok": one["ok"], "after": one}

    def clear_order(self, unit_addr: int) -> dict:
        """撤掉一个师的移动命令（把路径长度写 0）。

        只清路径容器，不动订单对象——实测这样部队会停在原地，
        不需要碰 `+0x2e8` 那些进度字段。工具层不用它，是给调试和"改主意"用的。
        """
        self._guard_write("撤单")
        with self._lock:
            ag = self.connect()
            try:
                before = self._describe(ag, unit_addr, -1)
                ag.write(unit_addr + OFF_PSIZE, struct.pack("<I", 0))
                after = self._describe(ag, unit_addr, -1)
                return {"before": before, "after": after}
            finally:
                ag.close()

    # ── 人类可读的说明 ────────────────────────

    @staticmethod
    def template_summary(units: list[dict], limit: int = 5) -> str:
        """把一批师的编制压成一句："坦克·中型坦克师1941A型 ×13、伞兵·空降伞兵师1941A型 ×1"。"""
        counts: dict[str, int] = {}
        for u in units:
            kind = str(u.get("kind") or "")
            name = str(u.get("template") or "（读不到编制）")
            key = f"{kind}·{name}" if kind else name
            counts[key] = counts.get(key, 0) + 1
        parts = [f"{t} ×{n}" for t, n in
                 sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
        if len(parts) > limit:
            parts = parts[:limit] + [f"……还有 {len(parts) - limit} 种"]
        return "、".join(parts)

    @staticmethod
    def describe_units(units: list[dict], limit: int = 30) -> list[str]:
        rows: list[str] = []
        by_prov: dict[int, list[dict]] = {}
        for u in units:
            by_prov.setdefault(u["prov"], []).append(u)
        for prov in sorted(by_prov, key=lambda p: (-len(by_prov[p]), p)):
            group = by_prov[prov]
            moving = [u for u in group if u["path"]]
            line = f"  省 {prov:>5}：{len(group)} 个"
            if moving:
                tgt = moving[0]["target"] or (moving[0]["path"][-1] if moving[0]["path"] else 0)
                line += f"（{len(moving)} 个在走，目标省 {tgt}）"
            tpl = livegame_template_line(group)
            if tpl:
                line += f" —— {tpl}"
            rows.append(line)
            if len(rows) >= limit:
                rows.append("  ……")
                break
        return rows


def livegame_template_line(group: list[dict]) -> str:
    """省一行里附带的编制名（只在种类不多时显示，免得一行太长）。"""
    names = {f"{u.get('kind')}·{u.get('template')}" if u.get("kind") else str(u.get("template") or "")
             for u in group if u.get("template")}
    if not names or len(names) > 3:
        return ""
    return " / ".join(sorted(names))

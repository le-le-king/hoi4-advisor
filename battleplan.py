"""进程内划线：直接改战区前线（CFront）的省份列表。

不抢鼠标、不按键，和调兵一样只读写进程内对象 + 调引擎函数。

数据结构（1.16.9 实测，全部逐个验证过）：

    CCountry  + 0x180 -> CPdxArray<CTheatre*>    战区  (data/cap/size)
    CTheatre  + 0x58  -> CPdxArray<CFront*>      前线  (cap@+0x60, size@+0x64)
    CTheatre  + 0x70  -> CPdxArray<CArmy*>       该战区的师 (cap@+0x78, size@+0x7C)
    CFront    + 0x20  -> CTheatre*               owner（构造时要用）
    CFront    + 0x28  -> CPdxArray<CProvince*>   省  (cap@+0x30, size@+0x34)
    CProvince + 0xAC  -> 省份 ID

引擎函数：
    0xC1F6D0(country, provId) -> CProvince*      省 ID 换指针
    0xDDD0A0(this=CFront, int, bool)             CFront::BuildSections —— 改完省份重建几何
    0xDD8E50(this)                               CFront 构造（本模块暂不直接用）

为什么改 CFront 而不是玩家手画的作战计划：
    作战计划（COrdersGroup/COrderInstance）是"给某个集团军下命令"，前提是玩家先建了集团军。
    战区前线（CFront）是"这段边境归这个战区守"，游戏自己按它分兵 —— 玩家没有集团军也存在。
    实测这台机器上玩家国家 0 个集团军、1 个战区、10 条前线，所以只有这条路是通的：
    改完之后游戏会自己把师调过去，效果和玩家手画防线一样，且不需要任何鼠标操作。

边界（越线就从参谋变外挂了）：
1. 只改**玩家自己战区**的前线。
2. 省份数不允许超过容器现有容量（cap）。超了就退回"截断"，绝不自己 realloc ——
   手工改 P 社容器的 cap 会让引擎下次扩容时踩到坏指针。
3. 版本不符（exe 字节数变了）就只读，不写。
"""

from __future__ import annotations

import struct

from . import nekoagent

__all__ = ["PlanError", "BattlePlan"]

# ── 偏移（1.16.9，逐个实测） ──────────────────────────
OFF_THEATRES = 0x180        # CCountry -> CPdxArray<CTheatre*>
OFF_THEATRE_FRONTS = 0x58   # CTheatre -> CPdxArray<CFront*>
OFF_THEATRE_UNITS = 0x70    # CTheatre -> CPdxArray<CArmy*>
OFF_FRONT_OWNER = 0x20      # CFront -> CTheatre*
OFF_FRONT_PROVS = 0x28      # CFront -> CPdxArray<CProvince*>
OFF_PROV_ID = 0xAC          # CProvince -> 省份 ID

RVA_GET_PROVINCE = 0xC1F6D0   # (country, provId) -> CProvince*
RVA_BUILD_SECTIONS = 0xDDD0A0  # CFront::BuildSections(int, bool)

MAX_FRONTS = 64
MAX_PROVS = 200


def _heap(x: int) -> bool:
    """看起来像用户态堆/映像指针。0x10000 ~ 0x7FFF_FFFF_FFFF。"""
    return 0x10000 < x < 0x7FFFFFFFFFFF


class PlanError(RuntimeError):
    def __init__(self, code: str, msg: str, hint: str = ""):
        super().__init__(msg)
        self.code = code
        self.msg = msg
        self.hint = hint


class BattlePlan:
    """一条通往玩家战区前线的读写通道。"""

    def __init__(self, agent: nekoagent.Agent, country: int, base: int,
                 rva_new: int = 0):
        self._ag = agent
        self._country = country
        self._base = base
        self._rva_new = rva_new

    # ── 底层读 ────────────────────────────────

    def _arr(self, addr: int) -> tuple[int, int]:
        """读 P 社容器 {data, cap, size}，返回 (data, size)。"""
        data, cap, size = struct.unpack("<Qii", self._ag.read(addr, 16))
        if not _heap(data) or size < 0 or size > 0x10000:
            return 0, 0
        return data, size

    def _cap(self, addr: int) -> tuple[int, int, int]:
        data, cap, size = struct.unpack("<Qii", self._ag.read(addr, 16))
        return data, cap, size

    # ── 战区 / 前线 ───────────────────────────

    def theatre(self) -> int:
        data, size = self._arr(self._country + OFF_THEATRES)
        if not data or size < 1:
            raise PlanError(
                "no_theatre",
                "你这个国家还没有战区对象，游戏里应该也看不到战区面板。",
                "先让游戏跑一帧（切到地图界面），再试一次。",
            )
        return struct.unpack("<Q", self._ag.read(data, 8))[0]

    def fronts(self) -> list[dict]:
        """玩家战区的所有前线，含每条负责的省份。"""
        th = self.theatre()
        addr = th + OFF_THEATRE_FRONTS
        data, cap, size = self._cap(addr)
        out: list[dict] = []
        if not _heap(data) or size <= 0:
            return out
        size = min(size, MAX_FRONTS)
        ptrs = struct.unpack(f"<{size}Q", self._ag.read(data, size * 8))
        for i, f in enumerate(ptrs):
            if not _heap(f):
                continue
            pdata, pcap, psize = self._cap(f + OFF_FRONT_PROVS)
            provs: list[int] = []
            if _heap(pdata) and 0 < psize <= MAX_PROVS:
                try:
                    pps = struct.unpack(f"<{psize}Q", self._ag.read(pdata, psize * 8))
                    for p in pps:
                        if _heap(p):
                            pid = self._ag.d(p + OFF_PROV_ID)
                            if 0 < pid < 200000:
                                provs.append(pid)
                except (nekoagent.AgentError, RuntimeError):
                    provs = []
            out.append({
                "idx": i,
                "addr": f,
                "cap": pcap,
                "size": psize,
                "provinces": provs,
            })
        return out

    def units_here(self) -> int:
        """这个战区现在挂了多少个师（看效果用）。"""
        th = self.theatre()
        data, size = self._arr(th + OFF_THEATRE_UNITS)
        return size if data else 0

    # ── 省 ID -> 指针 ─────────────────────────

    def province_ptrs(self, ids: list[int]) -> list[int]:
        out: list[int] = []
        fn = self._base + RVA_GET_PROVINCE
        for pid in ids:
            p = self._ag.call(fn, self._country, pid)
            if not _heap(p):
                raise PlanError(
                    "bad_province",
                    f"省 {pid} 换不出指针，可能不是有效省份 ID。",
                    "检查地名解析出来的省 ID，或者换个地方。",
                )
            got = self._ag.d(p + OFF_PROV_ID)
            if got != pid:
                raise PlanError(
                    "bad_province",
                    f"省 {pid} 查回来的对象自称是省 {got}，对不上，不敢写。",
                )
            out.append(p)
        return out

    # ── 写 ───────────────────────────────────

    def set_front(self, idx: int, ids: list[int], rebuild: bool = True) -> dict:
        """把第 idx 条前线的省份换成 ids。

        省份数不能超过这条线的现有容量；超了会截断并如实报告。
        """
        if not ids:
            raise PlanError("bad_input", "没给我省份。")
        info = self.fronts()
        if not 0 <= idx < len(info):
            raise PlanError("bad_input", f"没有第 {idx} 条前线（一共 {len(info)} 条）。")
        f = info[idx]
        data, cap, size = self._cap(f["addr"] + OFF_FRONT_PROVS)
        if not _heap(data) or cap <= 0:
            raise PlanError("failed", "这条前线的省份容器读不出来，不敢动。")

        truncated = False
        if len(ids) > cap:
            ids = ids[:cap]
            truncated = True

        ptrs = self.province_ptrs(ids)
        # 先只覆盖用到的部分，再改 size —— 不动 cap，引擎下次扩容才安全
        self._ag.write(data, struct.pack(f"<{len(ptrs)}Q", *ptrs))
        self._ag.write(f["addr"] + OFF_FRONT_PROVS + 0xC, struct.pack("<i", len(ptrs)))

        rebuilt = False
        if rebuild:
            rebuilt = self._rebuild(f["addr"])

        after = self.fronts()
        now = after[idx]["provinces"] if idx < len(after) else []
        return {
            "idx": idx,
            "wrote": ids,
            "truncated": truncated,
            "cap": cap,
            "rebuilt": rebuilt,
            "now": now,
            "ok": now == ids,
        }

    def clear_front(self, idx: int) -> dict:
        """把一条前线的省份清空（等价于这条防守线撤了）。"""
        info = self.fronts()
        if not 0 <= idx < len(info):
            raise PlanError("bad_input", f"没有第 {idx} 条前线（一共 {len(info)} 条）。")
        f = info[idx]
        self._ag.write(f["addr"] + OFF_FRONT_PROVS + 0xC, struct.pack("<i", 0))
        rebuilt = self._rebuild(f["addr"])
        after = self.fronts()
        now = after[idx]["provinces"] if idx < len(after) else []
        return {"idx": idx, "rebuilt": rebuilt, "now": now, "ok": not now}

    def add_front(self, ids: list[int], rebuild: bool = True) -> dict:
        """新建一条前线并挂到战区。

        新对象的内存由引擎自己的分配器出（CFront 构造），不手工拼结构体。
        """
        if not ids:
            raise PlanError("bad_input", "没给我省份。")
        th = self.theatre()
        data, cap, size = self._cap(th + OFF_THEATRE_FRONTS)
        if not _heap(data):
            raise PlanError("failed", "战区的前线容器读不出来。")
        if size >= cap:
            raise PlanError(
                "full",
                f"这个战区的前线已经满了（{size}/{cap}），没法再塞一条。",
                "先删一条现有的，或者改现有那条。",
            )
        ptrs = self.province_ptrs(ids)
        front = self._make_front(th, ptrs)
        self._ag.write(data + size * 8, struct.pack("<Q", front))
        self._ag.write(th + OFF_THEATRE_FRONTS + 0xC, struct.pack("<i", size + 1))
        rebuilt = self._rebuild(front) if rebuild else False
        return {"addr": front, "count": len(ptrs), "rebuilt": rebuilt}

    # ── 新建 CFront ──────────────────────────

    CF_SIZE = 0x200  # CFront 对象大小（保守值，构造只碰前 0x90）

    def _make_front(self, theatre: int, ptrs: list[int]) -> int:
        """分配 + 构造一个 CFront，塞好省份，返回地址。

        顺序不能反：构造函数 0xDD8E50 第一件事就是读 `this+0x20`（owner 战区）
        再顺着它取国家，所以必须先写 owner 再 call；整块先清零，
        免得构造里 `cmp [this+0x34], 0` 读到堆上的旧垃圾。
        """
        ag = self._ag
        if not self._rva_new:
            raise PlanError("failed", "没拿到引擎的分配函数地址，建不了新前线。")
        front = ag.malloc(self.CF_SIZE, self._rva_new)
        if not _heap(front):
            raise PlanError("failed", "引擎没给出内存。")
        ag.write(front, b"\x00" * self.CF_SIZE)
        ag.write(front + OFF_FRONT_OWNER, struct.pack("<Q", theatre))

        pdata = ag.malloc(8 * len(ptrs), self._rva_new)
        if not _heap(pdata):
            raise PlanError("failed", "省份数组分配失败。")
        ag.write(pdata, struct.pack(f"<{len(ptrs)}Q", *ptrs))

        ag.call(self._base + 0xDD8E50, front)
        ag.write(front + OFF_FRONT_PROVS,
                 struct.pack("<QiiQ", pdata, len(ptrs), len(ptrs), 0))
        return front

    def _rebuild(self, front: int) -> bool:
        """调 CFront::BuildSections，让引擎按新省份重算线段。"""
        try:
            self._ag.call(self._base + RVA_BUILD_SECTIONS, front, 0, 0)
            return True
        except (nekoagent.AgentError, RuntimeError):
            return False

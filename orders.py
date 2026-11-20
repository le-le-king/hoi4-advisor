"""【已废弃 · 0.5.0 起不要用】

划线现在走 `battleplan.py`：直接改战区前线的省份列表，再叫引擎自己重算。
不抢鼠标、不抢键盘、不拉视角，跟调兵是同一条链路。

这个文件留着只作历史参考——它当年靠"打 Z/X/C 键 + 拖地图"来借玩家的手，
问题是会抢前台、把视角拉到最小缩放，而且玩家没选军队时根本画不出来。
`hoi4_battle_plan` 已经不再 import 这里的任何东西。

---（以下为 0.4.x 的原始说明，仅供考古）---

在游戏里划线：防线 / 进攻线 / 撤退线 / 区域防守。

## 为什么不自己造一个"线"

HOI4 的前线不是画在屏幕上的几条线，是引擎里的作战计划对象：它会自己补位、
自己推进、自己判断哪段被突破。手写这种对象要逆向一大串容器和虚表，
写完还得跟上版本——不划算，也不稳。

## 所以走玩家的入口

玩家划线其实就是三步：选中部队 → 按那个按钮 → 在地图上点一下 / 拖一下。
这三步我们都会做，做出来的就是**真的作战计划**，跟玩家自己划的一模一样。

按键这一步有个现成的便宜可捡：`interface/battleplantools.gui` 里那些按钮
本来就绑了快捷键——

    Z        防线        X      进攻线      Shift+X  闪电战
    C        撤退线      V      区域防守    DEL      删除计划

按钮的屏幕位置会随分辨率变、随选中几个军变（军队条是居中排的），键不会。
所以这里一律打键，不去猜按钮坐标。
"""

from __future__ import annotations

__all__ = ["Planner", "PlanError", "PLANS", "plan_list"]


class PlanError(RuntimeError):
    """划线失败。message 是能直接说给用户听的话。"""


# vk / scan 都是 Windows 扫描码，跟 bridge 的 MODKEY 对得上。
PLANS: dict[str, dict] = {
    "defence":  {"vk": 0x5A, "scan": 0x2C, "mods": 0, "drag": False,
                 "label": "防线", "note": "沿线铺开防守，敌人撞上来会自动顶住"},
    "offence":  {"vk": 0x58, "scan": 0x2D, "mods": 0, "drag": True,
                 "label": "进攻线", "note": "从防线往目标方向拖一条箭头，会自己往前推"},
    "blitz":    {"vk": 0x58, "scan": 0x2D, "mods": 1, "drag": True,
                 "label": "闪电战进攻线", "note": "同上，但推进更凶、更不管侧翼"},
    "fallback": {"vk": 0x43, "scan": 0x2E, "mods": 0, "drag": False,
                 "label": "撤退线", "note": "往后撤到这条线上"},
    "area":     {"vk": 0x56, "scan": 0x2F, "mods": 0, "drag": False,
                 "label": "区域防守", "note": "驻防一片区域（要点/港口/海岸）"},
    "delete":   {"vk": 0x2E, "scan": 0x53, "mods": 0, "drag": False,
                 "label": "删除计划", "note": "把选中的那条线删掉"},
}

ALIASES = {
    "front": "defence", "frontline": "defence", "防守": "defence", "前线": "defence",
    "attack": "offence", "进攻": "offence", "突击": "offence",
    "闪电": "blitz", "闪击": "blitz",
    "retreat": "fallback", "撤退": "fallback",
    "garrison": "area", "区域": "area", "驻防": "area",
    "del": "delete", "删除": "delete", "取消": "delete",
}


def resolve_kind(text: str) -> str:
    key = (text or "").strip().lower()
    if key in PLANS:
        return key
    return ALIASES.get(key, "")


def plan_list() -> str:
    return "、".join(f"{v['label']}({k})" for k, v in PLANS.items())


class Planner:
    """把"选中 → 打键 → 点/拖"这三步串起来。

    必须用真实的 UiDriver：PostMessage 的鼠标游戏不认，只有 SendInput 认，
    但键盘两条通道都收——所以按键用 post，点地图用 real。
    """

    def __init__(self, ui, view, index, log=None) -> None:
        self._ui = ui
        self._view = view
        self._idx = index
        self._log = log or (lambda msg: None)

    # ── 坐标 ──────────────────────────────────

    def point(self, province_id: int) -> tuple[int, int]:
        px = self._idx.px(int(province_id))
        if px is None:
            raise PlanError(f"省 {province_id} 在地图表里没有坐标，点不到。")
        sx, sy = self._view.to_screen(*px)
        if not self._view.is_onscreen(sx, sy):
            raise PlanError(
                f"省 {province_id} 现在不在屏幕上（视角没对准）。"
                "先把地图移到那一带，或者把视角缩小一点再划。"
            )
        if not self._view.is_safe(sx, sy):
            raise PlanError(
                f"省 {province_id} 的位置被界面挡住了（顶部栏/右侧栏/底部条），"
                "这一下点不到地图上。挪一下视角再试。"
            )
        return int(sx), int(sy)

    # ── 单步 ──────────────────────────────────

    def focus_game(self) -> bool:
        """把游戏拉到前台。SendInput 的点击需要它是前台窗口。"""
        try:
            return self._ui.focus()
        except Exception:  # noqa: BLE001 拉不到就照点，失败会在后面体现
            return False

    def select_units(self, province_id: int) -> None:
        """点一下这个省 = 选中停在这里的师。

        这是玩家最常用的选兵方式，也是我们唯一不依赖内存的选兵方式。
        """
        x, y = self.point(province_id)
        self.focus_game()
        self._ui.real_click(x, y, "l")
        self._ui.settle(0.55)
        self._log(f"划线：点省 {province_id} 选兵 @ {x},{y}")

    def press(self, kind: str) -> dict:
        spec = PLANS[kind]
        self._ui.modkey(spec["vk"], spec["scan"], spec["mods"])
        self._ui.settle(0.45)
        return spec

    def click_at(self, province_id: int) -> tuple[int, int]:
        x, y = self.point(province_id)
        self._ui.real_click(x, y, "l")
        self._ui.settle(0.5)
        return x, y

    def drag_to(self, from_province: int, to_province: int) -> tuple[tuple[int, int], tuple[int, int]]:
        a = self.point(from_province)
        b = self.point(to_province)
        self._ui.drag(a[0], a[1], b[0], b[1])
        self._ui.settle(0.7)
        return a, b

    def cancel(self) -> None:
        """退出"正在画"的状态。画错了、或者点歪了就用它。"""
        try:
            self._ui.escape()
        except Exception:  # noqa: BLE001
            pass

    # ── 一整条 ────────────────────────────────

    def draw(self, kind: str, anchor_province: int,
             target_province: int = 0, select_province: int = 0) -> dict:
        """画一条计划线。

        - select_province：先用哪个省的部队（0 = 用 anchor 那个省）
        - anchor_province：线挂在哪（防线/撤退线/区域防守点这里）
        - target_province：进攻线往哪儿推（只有 offence/blitz 用）
        """
        spec = PLANS[kind]
        sel = int(select_province or anchor_province)
        self.select_units(sel)
        self.press(kind)
        if spec["drag"]:
            if not target_province:
                self.cancel()
                raise PlanError(
                    f"{spec['label']}要拖一条箭头出来，得给出往哪个省推。"
                )
            a, b = self.drag_to(anchor_province, target_province)
            return {"kind": kind, "label": spec["label"],
                    "action": "drag", "from": anchor_province, "to": target_province,
                    "at": a, "to_at": b, "note": spec["note"]}
        x, y = self.click_at(anchor_province)
        return {"kind": kind, "label": spec["label"],
                "action": "click", "at_province": anchor_province,
                "at": (x, y), "note": spec["note"]}

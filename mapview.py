"""地图投影：位图像素 ↔ 游戏客户区坐标。

## 为什么是"缩放 + 平移"这么简单

provinces.bmp 本身就是**等距投影渲染好的 2D 图**（5632x2048，斜着的那种）。
游戏把这个平面纹理贴上去，相机是正交的、朝向固定、不能旋转。
所以「位图像素 → 屏幕像素」只剩三件事：缩放 s、平移 tx/ty。

    screen_x = s * bmp_x + tx
    screen_y = s * bmp_y + ty

三个未知数，两个对应点就够解（四个方程，最小二乘）。

## 两个用它的人要注意的地方

1. **用户一动相机，tx/ty 就废了**。所以每次操作前都要重新锚定，
   或者确认相机没动过。`View.source` 记着这份标定是怎么来的。
2. **缩放 s 会变**。用户滚一格滚轮，s 就变了。锚定的办法是
   "缩到最小" —— 那是唯一一个能靠狂滚滚轮精确复现的状态。
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, asdict
from pathlib import Path

__all__ = ["View", "from_two_points", "from_fit", "load", "save", "UI_TOP", "UI_BOTTOM"]

# HOI4 顶部 topbar 的高度（1920 宽下约 58px）。投影出来的点掉在这条带子里
# 就会点到按钮上，所以判安全区时要排除。
UI_TOP = 66
UI_BOTTOM = 8


@dataclass(frozen=True)
class View:
    scale: float
    tx: float
    ty: float
    client_w: int
    client_h: int
    source: str = ""
    saved_at: float = 0.0

    # ── 换算 ──────────────────────────────────────

    def to_screen(self, bx: float, by: float) -> tuple[int, int]:
        return (int(round(self.scale * bx + self.tx)), int(round(self.scale * by + self.ty)))

    def to_bmp(self, sx: float, sy: float) -> tuple[float, float]:
        return ((sx - self.tx) / self.scale, (sy - self.ty) / self.scale)

    # ── 可用性 ────────────────────────────────────

    def matches(self, cw: int, ch: int) -> bool:
        """窗口尺寸变了（改分辨率/换显示器），旧标定就作废。"""
        return self.client_w == cw and self.client_h == ch

    def is_onscreen(self, sx: int, sy: int) -> bool:
        return 0 <= sx < self.client_w and 0 <= sy < self.client_h

    def is_safe(self, sx: int, sy: int) -> bool:
        """这个点能不能放心点。

        排除顶部 topbar 和底部边缘。右下小地图没排除——它只在缩放很小时
        才盖住地图，而那时本来也点不准，宁可让它点错再纠。
        """
        return (
            8 <= sx < self.client_w - 8
            and UI_TOP <= sy < self.client_h - UI_BOTTOM
        )

    def px_per_bmp(self) -> float:
        return self.scale

    def describe(self) -> str:
        return (
            f"缩放 {self.scale:.4f} / 偏移 ({self.tx:.0f},{self.ty:.0f}) / "
            f"窗口 {self.client_w}x{self.client_h} / 来源 {self.source or '未知'}"
        )


def from_two_points(
    bmp_a: tuple[float, float], scr_a: tuple[float, float],
    bmp_b: tuple[float, float], scr_b: tuple[float, float],
    client: tuple[int, int], source: str = "两点标定",
) -> View:
    """两个对应点解 scale/tx/ty。

    两点距离太近时缩放算不准（误差被放大几十倍），所以低于 80 位图像素就报错，
    让调用方去要更远的两个点。
    """
    bdx = bmp_b[0] - bmp_a[0]
    bdy = bmp_b[1] - bmp_a[1]
    sdx = scr_b[0] - scr_a[0]
    sdy = scr_b[1] - scr_a[1]
    db = math.hypot(bdx, bdy)
    ds = math.hypot(sdx, sdy)
    if db < 80:
        raise ValueError(f"两个标定点在地图上只差 {db:.0f} 像素，太近，缩放会算歪")
    if ds < 20:
        raise ValueError(f"两个标定点在屏幕上只差 {ds:.0f} 像素，太近")

    scale = ds / db
    tx = ((scr_a[0] - scale * bmp_a[0]) + (scr_b[0] - scale * bmp_b[0])) / 2.0
    ty = ((scr_a[1] - scale * bmp_a[1]) + (scr_b[1] - scale * bmp_b[1])) / 2.0
    return View(scale, tx, ty, client[0], client[1], source, time.time())


def from_fit(
    map_w: int, map_h: int, client: tuple[int, int],
    center_bmp: tuple[float, float], source: str = "适配窗口假设",
) -> View:
    """假设"缩到最小时整张地图适配窗口，且相机中心在 center_bmp"。

    这是个**假设**，不是事实：游戏到底按宽还是按高适配，没验证过。
    用它只是为了有一个能立刻试的起点——`describe()` 里会写明来源，
    点歪了要能一眼看出是假设错了，而不是标定算法错了。
    """
    cw, ch = client
    scale = min(cw / map_w, ch / map_h)
    tx = cw / 2.0 - scale * center_bmp[0]
    ty = ch / 2.0 - scale * center_bmp[1]
    return View(scale, tx, ty, cw, ch, source, time.time())


# ── 存盘 ──────────────────────────────────────────

FILE_NAME = "view_calib.json"


def save(data_dir: Path, view: View) -> bool:
    path = Path(data_dir) / FILE_NAME
    tmp = path.with_suffix(".tmp")
    try:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(asdict(view), ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError:
        return False


def load(data_dir: Path) -> View | None:
    path = Path(data_dir) / FILE_NAME
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return View(
            scale=float(raw["scale"]), tx=float(raw["tx"]), ty=float(raw["ty"]),
            client_w=int(raw["client_w"]), client_h=int(raw["client_h"]),
            source=str(raw.get("source") or ""), saved_at=float(raw.get("saved_at") or 0),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None

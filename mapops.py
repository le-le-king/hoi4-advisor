"""地图上的实际操作：标定视角、点省、调兵。

## 整套东西的地基只有一条假设

HOI4 的相机是正交的、朝向固定、不能转。provinces.bmp 本身就是等距渲染好的
2D 图。所以「位图像素 → 屏幕像素」是缩放 + 平移，三个未知数。

## 缩放和平移怎么定：缩到最小，量矩形

用户滚一格滚轮缩放就变一次，这个值读不到。但有一个状态能精确复现：
**一路滚到最小**。那时整张世界地图都在视野里，地图占据的矩形可以直接从
截图里量出来。矩形左上角就是位图原点 (0,0)：

    s  = 矩形宽 / 5632
    tx, ty = 矩形左上角

Home 键只当保险（万一相机没居中）。**不用 Home 也能成立**，
因为整图可见时地图原点就在矩形的角上。

## 代价（必须让用户知道）

每次操作前都要滚到最小，用户的视野会被拉走。而且最小缩放下一个省只有
十来个屏幕像素，点得中但没余量。这是读不到相机参数时的唯一办法。
"""

from __future__ import annotations

import time

from . import mapview, screen
from .prov_index import ProvIndex
from .ui import UiDriver, UiError

__all__ = [
    "calibrate", "ensure_view", "point_of_province", "point_of_state",
    "click_province", "move_units",
]

MIN_ZOOM_WHEEL = 26   # 往下滚这么多次一定到最小（HOI4 缩放下限不到 20 格）
FIT_TOLERANCE = 6     # 矩形和窗口边缘差几个像素才算"贴边"


def calibrate(driver: UiDriver, index: ProvIndex, log=None) -> mapview.View:
    """缩到最小 → 截图 → 量地图矩形 → 反解投影。

    任何一步不对就抛 UiError，把"哪一步不对"说清楚——
    "标定失败"四个字对排查毫无用处。
    """
    log = log or (lambda msg: None)
    cw, ch = driver.client(refresh=True)
    cx, cy = cw // 2, ch // 2

    driver.wheel(cx, cy, -120, MIN_ZOOM_WHEEL)
    time.sleep(0.6)
    driver.home()          # 保险：万一最小缩放时相机没居中
    time.sleep(0.6)

    hwnd = driver.hwnd()
    if not hwnd:
        raise UiError("拿不到游戏窗口句柄，截图做不了。")

    shot = screen.grab(hwnd)
    if shot is None:
        raise UiError(
            "截不到游戏画面。独占全屏抓不到，改成无边框窗口；窗口最小化也会这样。"
        )
    if screen.is_mostly_dark(shot):
        raise UiError("画面几乎是纯色——还停在加载页，或者没进地图界面。")

    # 截图尺寸跟窗口差太多 = 这个进程没声明 DPI 感知，系统把图缩放了。
    # 不拦住的话后面量出来的比例全是错的，点击会整体偏出去。
    if cw and ch and (abs(shot.w - cw) > cw * 0.03 or abs(shot.h - ch) > ch * 0.03):
        raise UiError(
            f"截出来的图是 {shot.w}x{shot.h}，窗口是 {cw}x{ch}——对不上，"
            "说明截图被系统按缩放比例缩过了。这一步做不了标定，也就点不准地图。"
        )

    rect = screen.find_map_rect(shot)
    if rect is None:
        raise UiError(
            "截图里没认出地图。多半是停在了菜单、国策树、外交这类整屏面板上，"
            "先按 Esc 回到地图。"
        )

    left, top, right, bottom = rect
    rect_w = right - left + 1
    rect_h = bottom - top + 1
    log(f"地图矩形 {rect_w}x{rect_h} @ ({left},{top}) 窗口 {cw}x{ch}")

    fits_w = rect_w < cw - FIT_TOLERANCE
    fits_h = rect_h < ch - FIT_TOLERANCE

    if fits_w and fits_h:
        # 地图四周都有留白 = 整张图都在视野里，左上角就是位图原点
        scale = rect_w / index.map_w
        check = rect_h / index.map_h
        if abs(scale - check) / scale > 0.06:
            raise UiError(
                f"地图矩形长宽比不对（宽算 {scale:.4f}，高算 {check:.4f}）。"
                "多半是截图里有别的整块色区被当成了地图，或者地图被界面切掉一角。"
            )
        tx, ty = float(left), float(top)
        how = "整图可见，矩形左上角"
    elif fits_w and not fits_h:
        # 上下贴边、左右留白：地图被垂直裁掉了一段，只能靠宽度定位
        scale = rect_w / index.map_w
        tx = float(left)
        ty = ch / 2.0 - scale * index.map_h / 2.0
        how = "按宽定位（上下被裁）"
    elif fits_h and not fits_w:
        scale = rect_h / index.map_h
        ty = float(top)
        tx = cw / 2.0 - scale * index.map_w / 2.0
        how = "按高定位（左右被裁）"
    else:
        raise UiError(
            "地图铺满了整个窗口，量不出边界——说明没缩到最小。"
            "先在地图上滚轮往下滚到底再试。"
        )

    view = mapview.View(
        scale=scale, tx=tx, ty=ty, client_w=cw, client_h=ch,
        source=f"最小缩放+矩形（{how}）", saved_at=time.time(),
    )
    log(f"标定完成 {view.describe()}")

    # 自检：地图原点应该落在窗口内，而且整张图的右下角也在窗口内（整图可见时）
    ox, oy = view.to_screen(0, 0)
    if not (0 <= ox < cw and 0 <= oy < ch):
        raise UiError(f"标定结果不合理：地图原点算到窗口外了 ({ox},{oy})。")
    return view


def ensure_view(driver: UiDriver, index: ProvIndex, data_dir, log=None) -> mapview.View:
    """拿一份能用的投影。能用旧的就不重标——每次重标都要抢用户视野。

    旧标定只在本局有效：用户一拖地图 tx/ty 就变了。所以这里**默认不信任旧值**，
    除非它刚刚才标过（30 秒内且窗口尺寸没变）。
    """
    log = log or (lambda msg: None)
    cw, ch = driver.client(refresh=True)

    old = mapview.load(data_dir)
    if old is not None and old.matches(cw, ch) and time.time() - old.saved_at < 30:
        log(f"30 秒内标过，沿用 {old.describe()}")
        return old

    view = calibrate(driver, index, log=log)
    mapview.save(data_dir, view)
    return view


def point_of_province(
    view: mapview.View, index: ProvIndex, province_id: int
) -> tuple[int, int] | None:
    pxy = index.px(province_id)
    if pxy is None:
        return None
    return view.to_screen(*pxy)


def point_of_state(
    view: mapview.View, index: ProvIndex, state_id: int
) -> tuple[int, int] | None:
    pxy = index.state_px(state_id)
    if pxy is None:
        return None
    return view.to_screen(*pxy)


def click_province(
    driver: UiDriver,
    view: mapview.View,
    index: ProvIndex,
    province_id: int,
    button: str = "l",
    channel: str = "post",
) -> tuple[int, int]:
    """点一个省。返回点到的屏幕坐标。"""
    pt = point_of_province(view, index, province_id)
    if pt is None:
        raise UiError(f"省 {province_id} 没有坐标（索引里查不到，可能是海域或版本不符）。")
    sx, sy = pt
    if not view.is_onscreen(sx, sy):
        raise UiError(
            f"省 {province_id} 算出来在屏幕外 ({sx},{sy})。"
            "缩到最小时整个世界都在视野里，出现这种情况说明标定偏了。"
        )
    if not view.is_safe(sx, sy):
        raise UiError(f"省 {province_id} 落在界面条上 ({sx},{sy})，点了会点到按钮。")
    driver.click_at(sx, sy, button, channel=channel)
    return (sx, sy)


def move_units(
    driver: UiDriver,
    view: mapview.View,
    index: ProvIndex,
    from_province: int,
    to_province: int,
    channel: str = "post",
    settle: float = 0.4,
) -> dict:
    """调兵：先点起始省选中部队，再右键目标省。

    顺序不能反：HOI4 是先选部队、再右键目的地。反过来右键就是给
    "当前选中的那支部队"下令，而当前选中的可能是上一次留下的。
    """
    if from_province == to_province:
        raise UiError("出发地和目的地是同一个省，不用动。")

    a = click_province(driver, view, index, from_province, "l", channel=channel)
    time.sleep(settle)
    b = click_province(driver, view, index, to_province, "r", channel=channel)
    return {
        "from_xy": a, "to_xy": b,
        "from_province": from_province, "to_province": to_province,
        "channel": channel,
    }

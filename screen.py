"""游戏窗口截图 + 地图区域检测。纯 ctypes，不依赖 PIL。

为什么不用 PIL：插件跑在宿主进程里，那个 Python 是打包过的，
有什么库完全不可控。这里只用到 user32/gdi32，一定有。

## 用它来干什么

**自动标定。** HOI4 缩到最小时整张世界地图都在视野内，
所以屏幕上"地图占据的矩形"可以直接量出来：

    缩放 s = 矩形宽 / 5632（位图宽）
    平移 tx, ty = 矩形左上角

再配合 Home 键（相机中心 = 首都），缩放和平移就全都确定了，
不需要用户拿鼠标点任何东西。

## 一个必须知道的限制

BitBlt 抓的是窗口 DC。**独占全屏直接抓不到**（返回全黑）。
无边框窗口没问题——本机 settings.txt 里就是 borderless=yes。
抓不到时返回 None，上层要能降级，别当成"地图是黑的"。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

__all__ = ["grab", "find_map_rect", "Shot"]

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

SRCCOPY = 0x00CC0020
BI_RGB = 0
DIB_RGB_COLORS = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


class Shot:
    """一张 BGRA 截图。像素用 at(x, y) 取，避免每次算偏移。"""

    __slots__ = ("w", "h", "buf")

    def __init__(self, w: int, h: int, buf: bytes) -> None:
        self.w = w
        self.h = h
        self.buf = buf

    def at(self, x: int, y: int) -> tuple[int, int, int]:
        i = (y * self.w + x) * 4
        b = self.buf
        return (b[i + 2], b[i + 1], b[i])  # BGRA -> RGB

    def row_is_bg(self, y: int, bg: tuple[int, int, int], tol: int) -> bool:
        return _row_all_bg(self.buf, self.w, y, bg, tol)

    def col_is_bg(self, x: int, bg: tuple[int, int, int], tol: int) -> bool:
        return _col_all_bg(self.buf, self.w, self.h, x, bg, tol)


def _row_all_bg(buf: bytes, w: int, y: int, bg: tuple[int, int, int], tol: int) -> bool:
    base = y * w * 4
    br, bgc, bb = bg
    for x in range(0, w, 4):  # 抽样 1/4，够判断
        i = base + x * 4
        if abs(buf[i + 2] - br) > tol or abs(buf[i + 1] - bgc) > tol or abs(buf[i] - bb) > tol:
            return False
    return True


def _col_all_bg(buf: bytes, w: int, h: int, x: int, bg: tuple[int, int, int], tol: int) -> bool:
    br, bgc, bb = bg
    step = w * 4 * 4
    i = x * 4
    limit = w * h * 4
    while i < limit:
        if abs(buf[i + 2] - br) > tol or abs(buf[i + 1] - bgc) > tol or abs(buf[i] - bb) > tol:
            return False
        i += step
    return True


def grab(hwnd: int) -> Shot | None:
    """抓客户区。抓不到（独占全屏 / 窗口最小化）返回 None。"""
    rc = wt.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rc)):
        return None
    w, h = rc.right - rc.left, rc.bottom - rc.top
    if w <= 0 or h <= 0:
        return None

    hdc = user32.GetDC(hwnd)
    if not hdc:
        return None
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    try:
        if not gdi32.BitBlt(mem, 0, 0, w, h, hdc, 0, 0, SRCCOPY):
            return None
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h  # 负数 = 自上而下，at() 的 y 才和屏幕方向一致
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = BI_RGB
        size = w * h * 4
        buf = ctypes.create_string_buffer(size)
        if not gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), DIB_RGB_COLORS):
            return None
        return Shot(w, h, buf.raw[:size])
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(hwnd, hdc)


def find_map_rect(shot: Shot, tol: int = 16) -> tuple[int, int, int, int] | None:
    """找地图占据的矩形 (left, top, right, bottom)。

    地图外是均匀的深色（黑或深蓝），地图内是各国颜色+地形，反差极大。
    以四角像素的中位色当背景色，然后从四边往里找第一行/列不是背景的。

    找不到（整个窗口一个色 = 抓黑了 / 还在加载）返回 None。
    """
    corners = [
        shot.at(1, 1), shot.at(shot.w - 2, 1),
        shot.at(1, shot.h - 2), shot.at(shot.w - 2, shot.h - 2),
    ]
    # 取中位色当中背景：有一角正好压着地图也不会误判
    bg = tuple(sorted(c[i] for c in corners)[1] for i in range(3))

    top = None
    for y in range(0, shot.h, 2):
        if not shot.row_is_bg(y, bg, tol):
            top = y
            break
    bottom = None
    for y in range(shot.h - 1, top or 0, -2):
        if not shot.row_is_bg(y, bg, tol):
            bottom = y
            break
    left = None
    for x in range(0, shot.w, 2):
        if not shot.col_is_bg(x, bg, tol):
            left = x
            break
    right = None
    for x in range(shot.w - 1, left or 0, -2):
        if not shot.col_is_bg(x, bg, tol):
            right = x
            break

    if None in (top, bottom, left, right):
        return None
    height = bottom - top
    width = right - left
    # 太小的矩形是 UI 残留（比如底部一个按钮），不是地图
    if width < shot.w * 0.35 or height < shot.h * 0.25:
        return None
    return (left, top, right, bottom)


def is_mostly_dark(shot: Shot, tol: int = 10) -> bool:
    """整张图几乎一个色 —— 说明 BitBlt 抓空了，或者游戏停在纯黑加载页。"""
    first = shot.at(5, 5)
    for y in range(5, shot.h, 37):
        for x in range(5, shot.w, 37):
            c = shot.at(x, y)
            if abs(c[0] - first[0]) + abs(c[1] - first[1]) + abs(c[2] - first[2]) > tol * 3:
                return False
    return True

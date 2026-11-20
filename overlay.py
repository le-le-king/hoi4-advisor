"""游戏内浮层：把"正在听 / 识别中 / 她说了什么"直接显示在游戏画面上。

## 为什么自己用 ctypes 拼窗口

宿主自带的嵌入式 Python 里只有 `_tkinter.pyd` + `tcl86t.dll` / `tk86t.dll`，
**没有 tcl8.6 / tk8.6 的脚本目录**，`import tkinter` 会直接报
`Can't find a usable init.tcl`。所以这里全部走 user32 / gdi32，零依赖、不用打包。

## 五个扩展样式，缺一个就会打断游戏

| 样式 | 作用 | 缺了会怎样 |
|---|---|---|
| `WS_EX_NOACTIVATE` | 显示/点击都不抢焦点 | 游戏切到后台 → 空格暂停失效 |
| `WS_EX_TRANSPARENT` | 鼠标穿透 | 浮层挡住的地方点不到，兵团点不动 |
| `WS_EX_TOOLWINDOW` | 不进 Alt-Tab、不占任务栏 | 切窗口时多一个莫名其妙的条目 |
| `WS_EX_LAYERED` | 半透明 | 一块不透明的板子糊在战略地图上 |
| `WS_EX_TOPMOST` | 盖在游戏上面 | 看不见（游戏得是"全屏窗口/无边框"，独占全屏盖不住） |

## 线程模型

窗口和消息循环跑在自己的线程里，别的线程只往窗口 PostMessage 一个
`WM_OVERLAY_UPDATE`，绘制全在窗口线程做 —— GDI 句柄不跨线程用，省掉一堆同步问题。

状态文字和倒计时放在实例属性里，`WM_TIMER`（120ms）负责重绘 + 淡入淡出 +
到点自己消失，所以调用方 `show()` 之后不用管收尾。
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

__all__ = ["GameOverlay"]

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
HANDLE = ctypes.c_void_p
WNDPROCTYPE = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, ctypes.c_uint, WPARAM, LPARAM)


class PAINTSTRUCT(ctypes.Structure):
    """wintypes 里没有这个，必须自己定义（少一个字段 EndPaint 就会读到野指针）。"""
    _fields_ = [
        ("hdc", HANDLE),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", WNDPROCTYPE),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", HANDLE),
        ("hIcon", HANDLE),
        ("hCursor", HANDLE),
        ("hbrBackground", HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


# ── 函数签名（不声明的话 64 位下句柄会被截成 32 位，窗口直接创建失败） ──

user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, HANDLE, HANDLE, HANDLE,
]
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint, WPARAM, LPARAM]
user32.BeginPaint.restype = HANDLE
user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.FillRect.argtypes = [HANDLE, ctypes.POINTER(wintypes.RECT), HANDLE]
user32.DrawTextW.argtypes = [HANDLE, wintypes.LPCWSTR, ctypes.c_int,
                             ctypes.POINTER(wintypes.RECT), ctypes.c_uint]
user32.GetDC.restype = HANDLE
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, HANDLE]

gdi32.CreateCompatibleDC.restype = HANDLE
gdi32.CreateCompatibleDC.argtypes = [HANDLE]
gdi32.CreateCompatibleBitmap.restype = HANDLE
gdi32.CreateCompatibleBitmap.argtypes = [HANDLE, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = HANDLE
gdi32.SelectObject.argtypes = [HANDLE, HANDLE]
gdi32.DeleteObject.argtypes = [HANDLE]
gdi32.DeleteDC.argtypes = [HANDLE]
gdi32.CreateSolidBrush.restype = HANDLE
gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
gdi32.CreateFontW.restype = HANDLE
gdi32.CreateFontW.argtypes = [
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR,
]
gdi32.SetBkMode.argtypes = [HANDLE, ctypes.c_int]
gdi32.SetTextColor.argtypes = [HANDLE, wintypes.COLORREF]
gdi32.BitBlt.argtypes = [HANDLE, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         HANDLE, ctypes.c_int, ctypes.c_int, wintypes.DWORD]

# ── 常量 ──

CLASS_NAME = "NekoHoi4VoiceOverlay"
WM_OVERLAY_UPDATE = 0x8001
WM_PAINT = 0x000F
WM_TIMER = 0x0113
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_ERASEBKGND = 0x0014

WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
LWA_ALPHA = 0x02

DT_LEFT = 0x0000
DT_WORDBREAK = 0x0010
DT_SINGLELINE = 0x0020
DT_VCENTER = 0x0004
DT_NOPREFIX = 0x0800
DT_END_ELLIPSIS = 0x8000
DT_EDITCONTROL = 0x2000

TRANSPARENT = 1
SRCCOPY = 0x00CC0020
WM_TIMER_TICK_MS = 120


def _rgb(r: int, g: int, b: int) -> int:
    """COLORREF 是 0x00BBGGRR，别按 RGB 顺序写。"""
    return (int(r) & 0xFF) | ((int(g) & 0xFF) << 8) | ((int(b) & 0xFF) << 16)


BG = _rgb(16, 17, 21)
BAR = _rgb(70, 72, 84)
TITLE = _rgb(168, 170, 184)
BODY = _rgb(240, 241, 246)

# 状态 → 左侧色条颜色。红=在录，橙=识别，蓝=她在想，绿=她在说
STATE_COLORS = {
    "recording": _rgb(255, 86, 86),
    "transcribing": _rgb(255, 186, 66),
    "thinking": _rgb(108, 170, 255),
    "reply": _rgb(96, 214, 152),
    "error": _rgb(255, 120, 120),
    "idle": _rgb(120, 200, 190),
}


class GameOverlay:
    """游戏画面上的一个小横条。全透明→淡入→显示→淡出，全程不抢焦点。"""

    def __init__(self, log=None, enabled: bool = True, width: int = 720,
                 height: int = 108, bottom_margin: int = 190, alpha: int = 252) -> None:
        self._log = log or (lambda msg: None)
        self._enabled = bool(enabled)
        self._w0 = int(width)
        self._h0 = int(height)
        self._margin = int(bottom_margin)
        self._target_alpha = int(alpha)

        self._hwnd = 0
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop_evt = threading.Event()
        self._wndproc_ref = None  # 必须留引用，否则回调被 GC → 崩

        self._lock = threading.Lock()
        self._state = "idle"
        self._title = ""
        self._body = ""
        self._t0 = time.time()
        self._expire_at = 0.0
        self._pinned = False  # 常驻（录音中/识别中），不自动消失

        self._alpha = 0
        self._fading_out = False
        self._w = self._w0
        self._h = self._h0
        self._scale = 1.0
        self._font_title = 0
        self._font_body = 0
        self._error = ""

    # ── 对外 ────────────────────────────────────

    @property
    def available(self) -> bool:
        return bool(self._hwnd)

    def start(self) -> None:
        if not self._enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="hoi4-overlay")
        self._thread.start()
        if not self._ready.wait(4.0):
            self._log(f"游戏内浮层没起来（{'/'.join([self._error]) or '超时'}），不影响说话本身")
        elif self._hwnd:
            self._log("游戏内浮层已就绪（不抢焦点、鼠标穿透，盖在游戏上面）")

    def stop(self) -> None:
        self._stop_evt.set()
        hwnd = self._hwnd
        if hwnd:
            try:
                user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            except Exception:  # noqa: BLE001
                pass

    def show(self, state: str, title: str = "", body: str = "",
             hold: float = 0.0, pinned: bool = False) -> None:
        """给浮层换一句话。hold>0 时到点自动淡出，pinned=True 则一直留着等下一条。"""
        if not self._enabled:
            return
        with self._lock:
            self._state = state or "idle"
            self._title = title or ""
            self._body = body or ""
            self._t0 = time.time()
            self._pinned = bool(pinned) or hold <= 0
            self._expire_at = 0.0 if self._pinned else time.time() + float(hold)
        self._pulse()

    def hide(self) -> None:
        if not self._enabled:
            return
        with self._lock:
            self._pinned = False
            self._expire_at = time.time()
        self._pulse()

    def status(self) -> dict:
        return {
            "overlay_enabled": self._enabled,
            "overlay_hwnd": self._hwnd,
            "overlay_state": self._state,
            "overlay_error": self._error,
        }

    # ── 窗口线程 ────────────────────────────────

    def _pulse(self) -> None:
        hwnd = self._hwnd
        if hwnd:
            try:
                user32.PostMessageW(hwnd, WM_OVERLAY_UPDATE, 0, 0)
            except Exception:  # noqa: BLE001
                pass

    def _run(self) -> None:
        try:
            self._create_window()
        except Exception as exc:  # noqa: BLE001
            self._error = f"{type(exc).__name__}: {exc}"
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        while not self._stop_evt.is_set():
            got = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if got in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self._hwnd = 0

    def _create_window(self) -> None:
        hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
        self._wndproc_ref = WNDPROCTYPE(self._wndproc)
        wc = WNDCLASSW()
        wc.style = 0
        wc.lpfnWndProc = self._wndproc_ref
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = hinst
        wc.hIcon = None
        wc.hCursor = None
        wc.hbrBackground = None
        wc.lpszMenuName = None
        wc.lpszClassName = CLASS_NAME
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            err = ctypes.get_last_error()
            # 1410 = 类已注册（热重载时会碰到），不算错
            if err != 1410:
                raise OSError(err, "RegisterClassW 失败")

        self._measure()

        ex = (WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_LAYERED
              | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
        x = max(0, (self._screen_w() - self._w) // 2)
        y = max(0, self._screen_h() - self._margin - self._h)
        hwnd = user32.CreateWindowExW(
            ex, CLASS_NAME, "N.E.K.O 语音", WS_POPUP,
            x, y, self._w, self._h, None, None, hinst, None,
        )
        if not hwnd:
            raise OSError(ctypes.get_last_error(), "CreateWindowExW 失败")
        self._hwnd = hwnd

        self._font_title = gdi32.CreateFontW(
            -int(16 * self._scale), 0, 0, 0, 400, 0, 0, 0, 134, 4, 0, 5, 0,
            "Microsoft YaHei UI",
        )
        self._font_body = gdi32.CreateFontW(
            -int(20 * self._scale), 0, 0, 0, 500, 0, 0, 0, 134, 4, 0, 5, 0,
            "Microsoft YaHei UI",
        )

        # 圆角：裁掉四个角，配半透明才像浮层而不是糊了块砖
        rgn = gdi32.CreateRoundRectRgn(0, 0, self._w + 1, self._h + 1,
                                       int(14 * self._scale), int(14 * self._scale))
        if rgn:
            user32.SetWindowRgn(hwnd, rgn, True)  # region 交给系统，不要自己删

        user32.SetLayeredWindowAttributes(hwnd, 0, 0, LWA_ALPHA)
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
        user32.SetTimer(hwnd, 1, WM_TIMER_TICK_MS, None)

    def _screen_w(self) -> int:
        return int(user32.GetSystemMetrics(0))

    def _screen_h(self) -> int:
        return int(user32.GetSystemMetrics(1))

    def _measure(self) -> None:
        try:
            dpi = int(user32.GetDpiForSystem())
            self._scale = max(1.0, dpi / 96.0)
        except Exception:  # noqa: BLE001 Win7/早期 Win10 没这个函数
            self._scale = 1.0
        self._w = int(self._w0 * self._scale)
        self._h = int(self._h0 * self._scale)

    # ── 消息处理 ────────────────────────────────

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_PAINT:
                self._on_paint(hwnd)
                return 0
            if msg == WM_ERASEBKGND:
                return 1  # 全部自己画，别让系统先刷一遍白底
            if msg == WM_OVERLAY_UPDATE:
                self._fading_out = False
                if self._alpha <= 0:
                    self._alpha = 0
                self._repaint(hwnd)
                return 0
            if msg == WM_TIMER:
                self._on_tick(hwnd)
                return 0
            if msg == WM_CLOSE:
                user32.KillTimer(hwnd, 1)
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception as exc:  # noqa: BLE001 窗口过程里抛出会把整个进程带崩
            self._error = f"{type(exc).__name__}: {exc}"
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _on_tick(self, hwnd) -> None:
        with self._lock:
            expire = self._expire_at
            pinned = self._pinned
        want = 0
        if pinned or time.time() < expire:
            want = self._target_alpha
        else:
            self._fading_out = True

        if want != self._alpha:
            step = max(24, self._target_alpha // 6)
            if want > self._alpha:
                self._alpha = min(want, self._alpha + step)
            else:
                self._alpha = max(0, self._alpha - step)
            user32.SetLayeredWindowAttributes(hwnd, 0, self._alpha, LWA_ALPHA)
        # 录音中要一直刷新计时，所以 pinned 时也重画
        if pinned or self._alpha:
            self._repaint(hwnd)

    def _repaint(self, hwnd) -> None:
        user32.InvalidateRect(hwnd, None, False)
        user32.UpdateWindow(hwnd)

    def _on_paint(self, hwnd) -> None:
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
        if not hdc:
            return
        try:
            mem = gdi32.CreateCompatibleDC(hdc)
            bmp = gdi32.CreateCompatibleBitmap(hdc, self._w, self._h)
            old = gdi32.SelectObject(mem, bmp)
            try:
                self._render(mem)
                gdi32.BitBlt(hdc, 0, 0, self._w, self._h, mem, 0, 0, SRCCOPY)
            finally:
                gdi32.SelectObject(mem, old)
                gdi32.DeleteObject(bmp)
                gdi32.DeleteDC(mem)
        finally:
            user32.EndPaint(hwnd, ctypes.byref(ps))

    def _render(self, dc) -> None:
        with self._lock:
            state = self._state
            title = self._title
            body = self._body
            t0 = self._t0
            pinned = self._pinned

        w, h = self._w, self._h
        full = wintypes.RECT(0, 0, w, h)
        brush = gdi32.CreateSolidBrush(BG)
        user32.FillRect(dc, ctypes.byref(full), brush)
        gdi32.DeleteObject(brush)

        # 左侧 4px 状态条
        bar = wintypes.RECT(0, 0, max(4, int(6 * self._scale)), h)
        color = STATE_COLORS.get(state, BAR)
        brush = gdi32.CreateSolidBrush(color)
        user32.FillRect(dc, ctypes.byref(bar), brush)
        gdi32.DeleteObject(brush)

        pad = int(18 * self._scale)
        left = pad
        right = w - pad

        gdi32.SetBkMode(dc, TRANSPARENT)

        line = title
        if state == "recording" and pinned:
            # 录音中把秒数打出来，让人知道确实在录
            line = f"{title}  {time.time() - t0:.1f}s"

        if line:
            gdi32.SelectObject(dc, self._font_title)
            gdi32.SetTextColor(dc, TITLE)
            r = wintypes.RECT(left, int(12 * self._scale), right,
                              int(12 * self._scale) + int(24 * self._scale))
            user32.DrawTextW(dc, line, -1, ctypes.byref(r),
                             DT_LEFT | DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX | DT_END_ELLIPSIS)

        if body:
            gdi32.SelectObject(dc, self._font_body)
            gdi32.SetTextColor(dc, BODY)
            top = int(40 * self._scale)
            r = wintypes.RECT(left, top, right, h - int(10 * self._scale))
            user32.DrawTextW(dc, body.replace("\r", ""), -1, ctypes.byref(r),
                             DT_LEFT | DT_WORDBREAK | DT_NOPREFIX | DT_EDITCONTROL)

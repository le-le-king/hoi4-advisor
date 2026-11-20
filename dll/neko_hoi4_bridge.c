/*
 * neko_hoi4_bridge.dll v3 — HOI4 进程内 TCP 桥（键盘 + 鼠标）
 *
 * v2 只有键盘。v3 加鼠标，因为"以玩家身份调兵/点国策/点建筑"必须点地图，
 * 而点地图只有两条路：PostMessage（不抢焦点）和 SendInput（真实输入）。
 * 两条都实现，哪条能过由实测决定。
 *
 * 设计原则沿用 v2：
 *   1. 每个请求极短，不做长 Sleep，节奏由客户端控制。
 *   2. 不维护游戏状态，只做投递。
 *   3. 端口自适应 7788→7792。
 *
 * 协议：TCP 127.0.0.1:<port>，一行一答，\n 结尾。
 *   PING                       -> OK pong <port>
 *   STATUS                     -> OK hwnd=0x.. pid=.. port=.. uptime=.. fg=0/1
 *   CLIENT                     -> OK client=<w>x<h> origin=<x>,<y>
 *   KEY <vk> <scan>            -> 投递按键（十六进制）
 *   TEXT <串>                   -> 逐字符 WM_CHAR
 *   ENTER                      -> 回车
 *   BACK <n>                   -> n 次退格
 *   CMD <串>                    -> TEXT + ENTER
 *
 *   鼠标（PostMessage，窗口不必在前台）：
 *   MOVE <x> <y>               -> 移到客户区坐标
 *   CLICK <x> <y> [l|r] [n]    -> 点击，默认左键 1 次
 *   DRAG <x1> <y1> <x2> <y2>   -> 按下、分步移动、抬起
 *   WHEEL <x> <y> <delta> <n>  -> 滚轮 n 次，delta 正=放大。缩放靠它复位
 *
 *   鼠标（SendInput，真实输入，会移动物理光标 + 抢前台）：
 *   REAL <x> <y> [l|r] [n]     -> 客户区坐标 -> 屏幕坐标 -> SendInput
 *   FOCUS                      -> 把游戏窗口拉到前台
 *
 *   PROBE <x> <y>              -> 探测该客户区坐标上盖着的是哪个窗口，
 *                                 用来判断这个位置是地图还是 UI 面板
 *
 * 编译：
 *   zig cc -target x86_64-windows-gnu -shared -O2 -o neko_hoi4_bridge.dll neko_hoi4_bridge.c -lws2_32
 */

#define WIN32_LEAN_AND_MEAN
#define _WIN32_WINNT 0x0601

#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <stdarg.h>

#define FIRST_PORT 7788
#define PORT_TRIES 5

static HWND g_hwnd = NULL;
static SOCKET g_listen = INVALID_SOCKET;
static int g_port = 0;
static volatile LONG g_running = 0;
static DWORD g_start_tick = 0;

/* ── 日志 ── */
static void blog(const char *fmt, ...)
{
    char path[MAX_PATH];
    char msg[1024];
    va_list ap;
    FILE *f;
    SYSTEMTIME st;
    char *p;

    va_start(ap, fmt);
    vsnprintf(msg, sizeof(msg), fmt, ap);
    va_end(ap);

    if (!GetModuleFileNameA(NULL, path, MAX_PATH - 32)) return;
    p = strrchr(path, '\\');
    if (!p) return;
    *(p + 1) = 0;
    strncat(path, "neko_bridge.log", MAX_PATH - strlen(path) - 1);

    f = fopen(path, "a");
    if (!f) return;
    GetLocalTime(&st);
    fprintf(f, "[%02d:%02d:%02d.%03d] %s\n",
            st.wHour, st.wMinute, st.wSecond, st.wMilliseconds, msg);
    fclose(f);
}

/* ── 找自己的 SDL 窗口 ── */
typedef struct { DWORD pid; HWND found; } FIND_CTX;

static BOOL CALLBACK enum_cb(HWND hwnd, LPARAM lp)
{
    FIND_CTX *c = (FIND_CTX *)lp;
    DWORD pid = 0;
    WCHAR cls[64];

    GetWindowThreadProcessId(hwnd, &pid);
    if (pid != c->pid || !IsWindowVisible(hwnd)) return TRUE;

    cls[0] = 0;
    GetClassNameW(hwnd, cls, 64);
    if (wcscmp(cls, L"SDL_app") == 0) {
        c->found = hwnd;
        return FALSE;
    }
    return TRUE;
}

static HWND find_window(void)
{
    FIND_CTX c;
    c.pid = GetCurrentProcessId();
    c.found = NULL;
    EnumWindows(enum_cb, (LPARAM)&c);
    return c.found;
}

static HWND hwnd_now(void)
{
    if (g_hwnd && IsWindow(g_hwnd)) return g_hwnd;
    g_hwnd = find_window();
    return g_hwnd;
}

/* ── 键盘（沿用 v2） ── */
static void post_vk(HWND h, WORD vk, WORD scan)
{
    LPARAM down = 1 | ((LPARAM)(scan & 0xFF) << 16);
    LPARAM up = down | (1LL << 30) | (1LL << 31);
    PostMessageW(h, WM_KEYDOWN, vk, down);
    PostMessageW(h, WM_KEYUP, vk, up);
    Sleep(8);
}

static void post_chars(HWND h, const char *s)
{
    WCHAR wide[1024];
    int n = MultiByteToWideChar(CP_ACP, 0, s, -1, wide, 1023);
    int i;
    if (n <= 0) return;
    for (i = 0; wide[i]; i++) {
        PostMessageW(h, WM_CHAR, (WPARAM)wide[i], 1);
        Sleep(6);
    }
}

/* ── 鼠标：PostMessage 通道 ──
 * lParam 低 16 位 x、高 16 位 y（客户区坐标）。
 * SDL2 直接读 lParam，所以不需要移动物理光标。
 */
static LPARAM mouse_lparam(int x, int y)
{
    return (LPARAM)(((y & 0xFFFF) << 16) | (x & 0xFFFF));
}

static void post_mouse_move(HWND h, int x, int y, WPARAM buttons)
{
    PostMessageW(h, WM_MOUSEMOVE, buttons, mouse_lparam(x, y));
}

static void post_mouse_click(HWND h, int x, int y, int right)
{
    WPARAM downFlag = right ? MK_RBUTTON : MK_LBUTTON;
    UINT downMsg = right ? WM_RBUTTONDOWN : WM_LBUTTONDOWN;
    UINT upMsg = right ? WM_RBUTTONUP : WM_LBUTTONUP;

    /* 先移到位再按：很多游戏只在收到 MOVE 后才更新 hover 状态，
       直接发 DOWN 会用上一次的鼠标位置。 */
    post_mouse_move(h, x, y, 0);
    Sleep(30);
    PostMessageW(h, downMsg, downFlag, mouse_lparam(x, y));
    Sleep(45);
    PostMessageW(h, upMsg, 0, mouse_lparam(x, y));
    Sleep(30);
}

/* ── 鼠标：SendInput 通道（真实输入） ── */
static void set_foreground(HWND h)
{
    if (!h) return;
    if (GetForegroundWindow() == h) return;
    /* 前台锁：不先按一下 Alt，SetForegroundWindow 常被系统拒绝 */
    keybd_event(VK_MENU, 0, 0, 0);
    keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0);
    SetForegroundWindow(h);
    Sleep(120);
}

/* ── 组合键（PostMessage） ──
 * HOI4 里"画防线/画进攻线"这些按钮在 battleplantools.gui 里绑了快捷键
 * （Z 防线 / X 进攻线 / Shift+X 闪电战 / C 撤退线 / V 区域防守），
 * 走快捷键比找按钮坐标可靠得多：按钮位置随分辨率、随选中几个军而变，
 * 快捷键不会。
 *
 * mods 是位掩码：1=Shift 2=Ctrl 4=Alt。发送顺序是 按下修饰键 → 主键 → 松开修饰键。
 */
static void post_vk_mods(HWND h, WORD vk, WORD scan, int mods)
{
    WORD mvk[3], msc[3];
    int nmod = 0, i;
    LPARAM base, upf = (1LL << 30) | (1LL << 31);

    if (mods & 1) { mvk[nmod] = VK_SHIFT;   msc[nmod] = 0x2A; nmod++; }
    if (mods & 2) { mvk[nmod] = VK_CONTROL; msc[nmod] = 0x1D; nmod++; }
    if (mods & 4) { mvk[nmod] = VK_MENU;    msc[nmod] = 0x38; nmod++; }

    for (i = 0; i < nmod; i++) {
        base = 1 | ((LPARAM)(msc[i] & 0xFF) << 16);
        PostMessageW(h, WM_KEYDOWN, mvk[i], base);
        Sleep(10);
    }
    base = 1 | ((LPARAM)(scan & 0xFF) << 16);
    PostMessageW(h, WM_KEYDOWN, vk, base);
    Sleep(20);
    PostMessageW(h, WM_KEYUP, vk, base | upf);
    Sleep(10);
    for (i = nmod - 1; i >= 0; i--) {
        base = 1 | ((LPARAM)(msc[i] & 0xFF) << 16);
        PostMessageW(h, WM_KEYUP, mvk[i], base | upf);
        Sleep(6);
    }
    Sleep(10);
}

static void send_mouse(HWND h, int cx, int cy, int right)
{
    POINT pt;
    INPUT in;
    LONG sw = GetSystemMetrics(SM_CXSCREEN);
    LONG sh = GetSystemMetrics(SM_CYSCREEN);

    pt.x = cx;
    pt.y = cy;
    if (!ClientToScreen(h, &pt)) return;

    /* SendInput 用 0..65535 归一化的虚拟桌面坐标 */
    memset(&in, 0, sizeof(in));
    in.type = INPUT_MOUSE;
    in.mi.dx = (LONG)((double)pt.x * 65535.0 / (double)(sw > 1 ? sw - 1 : 1));
    in.mi.dy = (LONG)((double)pt.y * 65535.0 / (double)(sh > 1 ? sh - 1 : 1));
    in.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE;
    SendInput(1, &in, sizeof(INPUT));
    Sleep(40);

    memset(&in, 0, sizeof(in));
    in.type = INPUT_MOUSE;
    in.mi.dwFlags = right ? MOUSEEVENTF_RIGHTDOWN : MOUSEEVENTF_LEFTDOWN;
    SendInput(1, &in, sizeof(INPUT));
    Sleep(45);

    memset(&in, 0, sizeof(in));
    in.type = INPUT_MOUSE;
    in.mi.dwFlags = right ? MOUSEEVENTF_RIGHTUP : MOUSEEVENTF_LEFTUP;
    SendInput(1, &in, sizeof(INPUT));
    Sleep(30);
}

/* ── 处理一条请求 ── */
static void handle(char *line, char *resp, size_t cap)
{
    char *p = line;
    char *end;
    HWND h;

    while (*p == ' ' || *p == '\t') p++;
    end = p + strlen(p);
    while (end > p && (end[-1] == '\r' || end[-1] == '\n' || end[-1] == ' ')) end--;
    *end = 0;

    if (!*p) { snprintf(resp, cap, "ERR empty"); return; }

    h = hwnd_now();

    if (_stricmp(p, "PING") == 0) {
        snprintf(resp, cap, "OK pong %d", g_port);
    } else if (_stricmp(p, "STATUS") == 0) {
        snprintf(resp, cap, "OK hwnd=0x%llX pid=%lu port=%d uptime=%lu fg=%d",
                 (unsigned long long)(uintptr_t)h,
                 (unsigned long)GetCurrentProcessId(),
                 g_port,
                 (unsigned long)((GetTickCount() - g_start_tick) / 1000),
                 (h && GetForegroundWindow() == h) ? 1 : 0);
    } else if (_stricmp(p, "CLIENT") == 0) {
        RECT rc;
        POINT org;
        if (!h) { snprintf(resp, cap, "ERR no window"); return; }
        GetClientRect(h, &rc);
        org.x = 0; org.y = 0;
        ClientToScreen(h, &org);
        snprintf(resp, cap, "OK client=%ldx%ld origin=%ld,%ld",
                 (long)(rc.right - rc.left), (long)(rc.bottom - rc.top),
                 (long)org.x, (long)org.y);
    } else if (_stricmp(p, "FOCUS") == 0) {
        if (!h) { snprintf(resp, cap, "ERR no window"); return; }
        set_foreground(h);
        snprintf(resp, cap, "OK focus fg=%d", GetForegroundWindow() == h ? 1 : 0);
    } else if (_strnicmp(p, "MOVE ", 5) == 0) {
        int x = 0, y = 0;
        if (!h) { snprintf(resp, cap, "ERR no window"); return; }
        if (sscanf(p + 5, "%d %d", &x, &y) != 2) { snprintf(resp, cap, "ERR args"); return; }
        post_mouse_move(h, x, y, 0);
        snprintf(resp, cap, "OK move %d %d", x, y);
    } else if (_strnicmp(p, "CLICK ", 6) == 0) {
        int x = 0, y = 0, n = 1;
        char btn[8] = "l";
        int i, right;
        if (!h) { snprintf(resp, cap, "ERR no window"); return; }
        if (sscanf(p + 6, "%d %d %7s %d", &x, &y, btn, &n) < 2) {
            snprintf(resp, cap, "ERR args");
            return;
        }
        right = (btn[0] == 'r' || btn[0] == 'R');
        if (n < 1) n = 1;
        if (n > 5) n = 5;
        for (i = 0; i < n; i++) {
            /* 双击：间隔要小于系统双击时间（默认 500ms） */
            post_mouse_click(h, x, y, right);
            if (n > 1) Sleep(90);
        }
        snprintf(resp, cap, "OK click %d %d %s x%d", x, y, right ? "right" : "left", n);
    } else if (_strnicmp(p, "DRAG ", 5) == 0) {
        int x1 = 0, y1 = 0, x2 = 0, y2 = 0, i;
        if (!h) { snprintf(resp, cap, "ERR no window"); return; }
        if (sscanf(p + 5, "%d %d %d %d", &x1, &y1, &x2, &y2) != 4) {
            snprintf(resp, cap, "ERR args");
            return;
        }
        post_mouse_move(h, x1, y1, 0);
        Sleep(30);
        PostMessageW(h, WM_LBUTTONDOWN, MK_LBUTTON, mouse_lparam(x1, y1));
        Sleep(60);
        /* 分 8 步走，一次跳到位很多游戏认不出来是拖拽 */
        for (i = 1; i <= 8; i++) {
            int x = x1 + (x2 - x1) * i / 8;
            int y = y1 + (y2 - y1) * i / 8;
            post_mouse_move(h, x, y, MK_LBUTTON);
            Sleep(25);
        }
        PostMessageW(h, WM_LBUTTONUP, 0, mouse_lparam(x2, y2));
        snprintf(resp, cap, "OK drag %d,%d -> %d,%d", x1, y1, x2, y2);
    } else if (_strnicmp(p, "WHEEL ", 6) == 0) {
        int x = 0, y = 0, delta = 120, n = 1, i;
        POINT pt;
        if (!h) { snprintf(resp, cap, "ERR no window"); return; }
        if (sscanf(p + 6, "%d %d %d %d", &x, &y, &delta, &n) < 3) {
            snprintf(resp, cap, "ERR args");
            return;
        }
        if (n < 1) n = 1;
        if (n > 60) n = 60;
        /* WM_MOUSEWHEEL 的 lParam 是**屏幕坐标**，不是客户区坐标。
           这里写成客户区会让滚轮落到别的窗口上，或者干脆不生效。 */
        pt.x = x; pt.y = y;
        ClientToScreen(h, &pt);
        for (i = 0; i < n; i++) {
            PostMessageW(h, WM_MOUSEWHEEL, (WPARAM)(((WORD)delta) << 16),
                         (LPARAM)(((pt.y & 0xFFFF) << 16) | (pt.x & 0xFFFF)));
            Sleep(45);
        }
        snprintf(resp, cap, "OK wheel %d x%d", delta, n);
    } else if (_strnicmp(p, "REAL ", 5) == 0) {        int x = 0, y = 0, n = 1;
        char btn[8] = "l";
        int i, right;
        if (!h) { snprintf(resp, cap, "ERR no window"); return; }
        if (sscanf(p + 5, "%d %d %7s %d", &x, &y, btn, &n) < 2) {
            snprintf(resp, cap, "ERR args");
            return;
        }
        right = (btn[0] == 'r' || btn[0] == 'R');
        if (n < 1) n = 1;
        if (n > 5) n = 5;
        set_foreground(h);
        for (i = 0; i < n; i++) {
            send_mouse(h, x, y, right);
            if (n > 1) Sleep(90);
        }
        snprintf(resp, cap, "OK real %d %d %s x%d", x, y, right ? "right" : "left", n);
    } else if (_strnicmp(p, "PROBE ", 6) == 0) {
        int x = 0, y = 0;
        POINT pt;
        HWND under;
        WCHAR cls[128];
        DWORD pid = 0;
        if (!h) { snprintf(resp, cap, "ERR no window"); return; }
        if (sscanf(p + 6, "%d %d", &x, &y) != 2) { snprintf(resp, cap, "ERR args"); return; }
        pt.x = x; pt.y = y;
        ClientToScreen(h, &pt);
        under = WindowFromPoint(pt);
        cls[0] = 0;
        if (under) {
            GetClassNameW(under, cls, 128);
            GetWindowThreadProcessId(under, &pid);
        }
        snprintf(resp, cap, "OK under=0x%llX cls=%ls pid=%lu same=%d",
                 (unsigned long long)(uintptr_t)under, cls, (unsigned long)pid,
                 under == h ? 1 : 0);
    } else if (_stricmp(p, "CURSOR") == 0) {
        POINT pt;
        if (!GetCursorPos(&pt)) { snprintf(resp, cap, "ERR cursor"); return; }
        snprintf(resp, cap, "OK cursor=%ld,%ld", (long)pt.x, (long)pt.y);
    } else if (_strnicmp(p, "MODKEY ", 7) == 0) {
        int mods = 0;
        unsigned int vk = 0, scan = 0;
        if (!h) { snprintf(resp, cap, "ERR no window"); return; }
        if (sscanf(p + 7, "%d %x %x", &mods, &vk, &scan) != 3) { snprintf(resp, cap, "ERR args"); return; }
        post_vk_mods(h, (WORD)vk, (WORD)scan, mods);
        snprintf(resp, cap, "OK modkey mods=%d vk=%x", mods, vk);
    } else if (_stricmp(p, "ESC") == 0) {
        if (!h) { snprintf(resp, cap, "ERR no window"); return; }
        post_vk(h, VK_ESCAPE, 0x01);
        snprintf(resp, cap, "OK esc");
    } else if (_strnicmp(p, "KEY ", 4) == 0) {
        unsigned int vk = 0, scan = 0;
        if (!h) { snprintf(resp, cap, "ERR no window"); return; }
        if (sscanf(p + 4, "%x %x", &vk, &scan) != 2) { snprintf(resp, cap, "ERR args"); return; }
        post_vk(h, (WORD)vk, (WORD)scan);
        snprintf(resp, cap, "OK key");
    } else if (_stricmp(p, "ENTER") == 0) {
        if (!h) { snprintf(resp, cap, "ERR no window"); return; }
        post_vk(h, VK_RETURN, 0x1C);
        snprintf(resp, cap, "OK enter");
    } else if (_strnicmp(p, "BACK ", 5) == 0) {
        int n = atoi(p + 5), i;
        if (!h) { snprintf(resp, cap, "ERR no window"); return; }
        if (n < 0) n = 0;
        if (n > 200) n = 200;
        for (i = 0; i < n; i++) post_vk(h, VK_BACK, 0x0E);
        snprintf(resp, cap, "OK back %d", n);
    } else if (_strnicmp(p, "TEXT ", 5) == 0) {
        if (!h) { snprintf(resp, cap, "ERR no window"); return; }
        post_chars(h, p + 5);
        snprintf(resp, cap, "OK text %d", (int)strlen(p + 5));
    } else if (_strnicmp(p, "CMD ", 4) == 0) {
        if (!h) { snprintf(resp, cap, "ERR no window"); return; }
        post_chars(h, p + 4);
        Sleep(60);
        post_vk(h, VK_RETURN, 0x1C);
        blog("CMD %s", p + 4);
        snprintf(resp, cap, "OK cmd");
    } else {
        snprintf(resp, cap,
                 "ERR unknown (PING/STATUS/CLIENT/FOCUS/MOVE/CLICK/DRAG/WHEEL/REAL/PROBE/"
                 "KEY/TEXT/ENTER/BACK/CMD)");
    }
}

/* ── 服务器线程 ── */
static DWORD WINAPI server(LPVOID arg)
{
    WSADATA wsa;
    struct sockaddr_in addr;
    int attempt;

    (void)arg;
    g_start_tick = GetTickCount();

    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        blog("WSAStartup 失败 %d", WSAGetLastError());
        return 1;
    }

    for (attempt = 0; attempt < PORT_TRIES; attempt++) {
        int port = FIRST_PORT + attempt;
        SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);

        if (s == INVALID_SOCKET) continue;
        /* 不设 SO_REUSEADDR：Windows 上它允许抢占已占用端口，
           会和旧实例撞车，导致 bind 成功却收不到连接。让 bind 直接失败更干净。 */

        memset(&addr, 0, sizeof(addr));
        addr.sin_family = AF_INET;
        addr.sin_port = htons((u_short)port);
        addr.sin_addr.s_addr = inet_addr("127.0.0.1");

        if (bind(s, (struct sockaddr *)&addr, sizeof(addr)) == 0 && listen(s, 8) == 0) {
            g_listen = s;
            g_port = port;
            break;
        }
        closesocket(s);
    }

    if (g_listen == INVALID_SOCKET) {
        blog("所有端口都占用了，桥接未启动");
        WSACleanup();
        return 1;
    }

    blog("=== bridge v3 监听 127.0.0.1:%d (pid %lu) ===", g_port, (unsigned long)GetCurrentProcessId());

    while (g_running) {
        fd_set rfds;
        struct timeval tv;
        SOCKET client;

        FD_ZERO(&rfds);
        FD_SET(g_listen, &rfds);
        tv.tv_sec = 1;
        tv.tv_usec = 0;

        if (select(0, &rfds, NULL, NULL, &tv) <= 0) continue;

        client = accept(g_listen, NULL, NULL);
        if (client == INVALID_SOCKET) continue;

        {
            /* 一个连接处理一条请求就关，简单可靠 */
            char buf[2048];
            int total = 0;

            for (;;) {
                int n = recv(client, buf + total, (int)sizeof(buf) - 1 - total, 0);
                if (n <= 0) break;
                total += n;
                buf[total] = 0;
                if (strchr(buf, '\n')) break;
                if (total >= (int)sizeof(buf) - 2) break;
            }

            if (total > 0) {
                char resp[2048];
                char out[2200];
                int len;
                buf[total] = 0;
                resp[0] = 0;
                handle(buf, resp, sizeof(resp));
                len = snprintf(out, sizeof(out), "%s\n", resp);
                send(client, out, len, 0);
            }
        }
        closesocket(client);
    }

    closesocket(g_listen);
    WSACleanup();
    blog("=== bridge 退出 ===");
    return 0;
}

BOOL WINAPI DllMain(HINSTANCE hinst, DWORD reason, LPVOID reserved)
{
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hinst);
        g_running = 1;
        blog("DLL v3 attach pid=%lu", (unsigned long)GetCurrentProcessId());
        CreateThread(NULL, 0, server, NULL, 0, NULL);
    } else if (reason == DLL_PROCESS_DETACH) {
        g_running = 0;
    }
    return TRUE;
}

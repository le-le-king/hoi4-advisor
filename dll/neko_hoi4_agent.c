/*
 * neko_hoi4_agent.dll -- in-process agent for HOI4.
 *
 * Why this exists:
 *   Reading/writing game memory and calling engine functions from an external
 *   Python process is slow (ReadProcessMemory round trips, 40s full scans) and
 *   cannot run arbitrary engine code on the game's main thread. This DLL lives
 *   inside hoi4.exe, so it can:
 *     - read/write memory with plain memcpy
 *     - scan the whole heap safely (copies 1 MB chunks, never reads a page
 *       the game may have just released — an earlier direct memchr sweep
 *       could crash the game with C0000005)
 *     - run a call on the GAME MAIN THREAD via a window-proc hook
 *       (calling engine code from the pipe thread would corrupt state / crash)
 *
 * Protocol: line based ASCII over TCP 127.0.0.1:7799, or a named pipe.
 *   PING                      -> OK pong <pid> <base>
 *   BASE                      -> OK <hex>
 *   RD <addr> <n>             -> OK <hexbytes...>          n <= 0x100000
 *   WR <addr> <hexbytes>      -> OK <n>
 *   SCAN <addr> <size> <hexq> -> OK <hexaddr>[,<hexaddr>...]
 *   CALL <addr> <a1> <a2> <a3> <a4>   (main thread) -> OK <rax>
 *   CALLF <addr> <fa1> <fa2>  (floats in xmm0/xmm1, main thread) -> OK <rax>
 *   MAIN                      -> OK <threadid>   (is current thread the game main thread?)
 *   QUIT                      -> OK bye
 *
 * Addresses, function pointers and args are HEX (0x prefix optional).
 * Lengths/sizes are DECIMAL (0x prefix optional). Do not mix these up:
 * feeding a bare hex address to a decimal parser truncates it at the first
 * a-f digit and silently reads the wrong memory.
 *
 * Build:
 *   zig cc -target x86_64-windows-gnu -shared -O2 -o neko_hoi4_agent.dll \
 *          neko_hoi4_agent.c -lws2_32 -luser32
 */

#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <tlhelp32.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <setjmp.h>

/*
 * 扫描时一次 memchr 的块大小，也是 setjmp 的保护粒度：一个块里踩到被游戏
 * 收回的页，就整块跳过接着扫。
 *
 * 历史：0.9.19 曾用 ReadProcessMemory 把每块复制进自己的缓冲来躲崩溃（安全，
 * 但慢了 3~5 倍，认国家 20 秒、撞上 12 秒超时）；0.9.21 改回直接扫 + VEH 兜底。
 */
#define SCAN_CHUNK (1u << 20)

/* 7801: 7799 got hijacked by a crashed-but-lingering process (Windows lets two
   sockets share an address with SO_REUSEADDR), so we moved and now bind
   exclusively. */
#define AGENT_PORT 7801
#define AGENT_VERSION "0.5.1"
#define AGENT_BUILD "2026-11-08 06:50"
#define MAX_LINE 4096
#define MAX_RESP (4u << 20)

typedef uint64_t u64;
typedef uint32_t u32;

static volatile LONG g_inited = 0;
static HWND g_game_hwnd = NULL;
static DWORD g_main_tid = 0;
static UINT g_msg_id = 0;
static WNDPROC g_orig_proc = NULL;
static char* g_resp = NULL;          /* 兼容保留：多客户端后每线程自带缓冲 */
static CRITICAL_SECTION g_cmd_lock;  /* 串行化 handle()：CALL 任务/热键缓冲是共享状态 */
static int g_cmd_lock_ready = 0;
static DWORD g_pid = 0;
static u64 g_base = 0;

/* ---- 游戏内热键 ----
 *
 * 为什么要 DLL 来轮询按键：HOI4 的 mod 脚本没有"按键触发效果"这个接口。
 * interface 目录下 battleplantools.gui 里的 buttonType 可以写 shortcut = "z"，但那只对游戏自己
 * 认识的按钮名生效（battleplantools.gui 里 Z/X/C/V 就是靠这个绑的），
 * 我们自己加的按钮没有对应的代码分支，按了也不会做事。
 *
 * 所以热键只能在进程里读。GetAsyncKeyState 是会话级的，游戏在后台也会响应，
 * 因此额外要求"前台窗口属于本进程"，否则用户在别的窗口打字也会触发。
 */
#define MAX_HOT 8
typedef struct {
    volatile LONG seq;
    volatile LONG code;      /* 1 = F7 接管, 2 = F8 放开 */
    volatile DWORD tick;
} HotEv;

static HotEv g_hot[MAX_HOT];
static volatile LONG g_hot_seq = 0;
static volatile LONG g_hot_on = 0;

static void hot_record(int code)
{
    LONG seq = InterlockedIncrement(&g_hot_seq);
    HotEv* e = &g_hot[seq & (MAX_HOT - 1)];
    e->code = code;
    e->tick = GetTickCount();
    e->seq = seq;
}

static DWORD WINAPI hotkey_thread(LPVOID p)
{
    struct { int vk; int code; int down; } k[2];
    (void)p;
    k[0].vk = VK_F7; k[0].code = 1; k[0].down = 0;
    k[1].vk = VK_F8; k[1].code = 2; k[1].down = 0;

    while (InterlockedCompareExchange(&g_hot_on, 1, 1) == 1) {
        HWND fg = GetForegroundWindow();
        DWORD pid = 0;
        int i;
        if (fg) GetWindowThreadProcessId(fg, &pid);
        if (pid != g_pid) {
            /* 不在游戏前台：清掉按下状态，免得切出去时被按过又切回来算成一次 */
            k[0].down = 0;
            k[1].down = 0;
            Sleep(60);
            continue;
        }
        for (i = 0; i < 2; i++) {
            int down = (GetAsyncKeyState(k[i].vk) & 0x8000) ? 1 : 0;
            if (down && !k[i].down) {
                hot_record(k[i].code);
            }
            k[i].down = down;
        }
        Sleep(25);
    }
    return 0;
}

/* ---- one-slot job used to run engine calls on the game main thread ---- */
typedef struct {
    volatile LONG state;      /* 0 idle, 1 posted, 2 running, 3 done */
    volatile LONG faulted;    /* 1 = the call raised an exception; see call_guard_veh */
    u64 fn;
    u64 a1, a2, a3, a4;
    float f1, f2;
    u64 ret;
    int uses_float;
} Job;

static Job g_job;

/* ---------------- helpers ---------------- */

static int hexval(int c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static u64 parse_num(const char* s, const char** endp)
{
    u64 v = 0;
    const char* p = s;
    while (*p == ' ') p++;
    if (p[0] == '0' && (p[1] == 'x' || p[1] == 'X')) {
        p += 2;
        while (hexval((unsigned char)*p) >= 0) { v = (v << 4) | (u64)hexval((unsigned char)*p); p++; }
    } else {
        while (*p >= '0' && *p <= '9') { v = v * 10 + (u64)(*p - '0'); p++; }
    }
    if (endp) *endp = p;
    return v;
}

/*
 * Addresses and function pointers are always hex.
 * parse_num() treats a bare "7ff685f90000" as decimal and stops at the first
 * 'f', which silently turned every RD into a read at address 7.
 */
static u64 parse_hex(const char* s, const char** endp)
{
    u64 v = 0;
    const char* p = s;
    while (*p == ' ') p++;
    if (p[0] == '0' && (p[1] == 'x' || p[1] == 'X')) p += 2;
    while (hexval((unsigned char)*p) >= 0) { v = (v << 4) | (u64)hexval((unsigned char)*p); p++; }
    if (endp) *endp = p;
    return v;
}

static int hex_to_bytes(const char* s, unsigned char* out, int max)
{
    int n = 0;
    while (s[0] && s[1] && n < max) {
        int hi = hexval((unsigned char)s[0]);
        int lo = hexval((unsigned char)s[1]);
        if (hi < 0 || lo < 0) break;
        out[n++] = (unsigned char)((hi << 4) | lo);
        s += 2;
    }
    return n;
}

static void bytes_to_hex(const unsigned char* in, int n, char* out)
{
    static const char* D = "0123456789abcdef";
    int i;
    for (i = 0; i < n; i++) {
        out[i * 2] = D[in[i] >> 4];
        out[i * 2 + 1] = D[in[i] & 15];
    }
    out[n * 2] = 0;
}

static u64 module_base(DWORD pid)
{
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid);
    MODULEENTRY32W me;
    u64 base = 0;
    if (snap == INVALID_HANDLE_VALUE) return 0;
    ZeroMemory(&me, sizeof(me));
    me.dwSize = sizeof(me);
    if (Module32FirstW(snap, &me)) {
        do {
            /* first module in the snapshot is the exe itself */
            base = (u64)(uintptr_t)me.modBaseAddr;
            break;
        } while (0);
    }
    CloseHandle(snap);
    return base;
}

/*
 * Page-level guard.
 *
 * Every read/write/scan goes through this first. Without it, poking a bad
 * address is an instant access violation inside hoi4.exe -- the game dies and
 * the user loses the session. VirtualQuery is cheap (one syscall per region),
 * so we check the whole span before touching it.
 */
static int span_ok(u64 addr, u64 len, int for_write)
{
    u64 cur = addr;
    u64 end = addr + len;
    MEMORY_BASIC_INFORMATION mbi;
    if (end < addr) return 0;
    while (cur < end) {
        DWORD need = PAGE_READONLY | PAGE_READWRITE | PAGE_WRITECOPY |
                     PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY;
        if (VirtualQuery((LPCVOID)(uintptr_t)cur, &mbi, sizeof(mbi)) != sizeof(mbi)) return 0;
        if (mbi.State != MEM_COMMIT) return 0;
        if (!(mbi.Protect & need)) return 0;
        if (mbi.Protect & PAGE_GUARD) return 0;
        if (for_write && !(mbi.Protect & (PAGE_READWRITE | PAGE_WRITECOPY |
                                          PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY)))
            return 0;
        cur = (u64)(uintptr_t)mbi.BaseAddress + mbi.RegionSize;
        if (cur <= addr) return 0;
    }
    return 1;
}

/* ---------------- call on main thread ---------------- */

typedef u64 (*fn4_t)(u64, u64, u64, u64);
typedef u64 (*fnf_t)(float, u64, u64, u64);

/*
 * Crash guard for engine calls.
 *
 * A wrong argument -- or calling the wrong function -- raises an access
 * violation inside hoi4.exe and takes the whole game down with it. The user
 * already lost four sessions to breakpoint experiments, so every main-thread
 * call is wrapped: a VEH grabs the first-chance exception while a job is
 * running and longjmps back into run_job, which reports the fault instead of
 * dying.
 *
 * Caveat: longjmp skips unwinding, so any lock the callee held is NOT
 * released. This is a last resort that keeps the session alive, not a licence
 * to call arbitrary addresses. One fault and treat the save as suspect.
 */
static jmp_buf g_job_jmp;
static volatile LONG g_job_guard = 0;
static volatile LONG g_guard_installed = 0;
/* 哪个线程在这套 guard 里跑。VEH 是**进程级**的：不认线程的话，扫描线程踩到
 * 一页会被 CALL 的 handler 接走，longjmp 回到主线程的栈上——那才是真灾难。 */
static volatile DWORD g_job_tid = 0;
static volatile DWORD g_scan_tid = 0;

static LONG CALLBACK call_guard_veh(EXCEPTION_POINTERS* ep)
{
    if (ep->ExceptionRecord->ExceptionCode == EXCEPTION_ACCESS_VIOLATION &&
        g_job_tid == GetCurrentThreadId() &&
        InterlockedCompareExchange(&g_job_guard, 1, 1) == 1) {
        InterlockedExchange(&g_job_guard, 0);
        longjmp(g_job_jmp, 1);
    }
    return EXCEPTION_CONTINUE_SEARCH;
}

/*
 * 扫描的保护（0.9.21）。
 *
 * 0.9.19 为了不把游戏崩掉（C0000005，栈顶 memchr），把 SCAN 改成
 * ReadProcessMemory 把每 1MB 复制进自己的缓冲再找。安全是安全了，但慢了
 * 3~5 倍：真机上认国家从几秒变成 20 秒，撞上 `_player_only` 的 12 秒超时，
 * 她就开始跟用户说「代理没注进去」「内存里认不出德国」——用户报的"注入失败"
 * 其实是我自己把扫描拖慢了。
 *
 * 现在改回直接 memchr（一个 region 一次调用，向量化、全速），用 VEH +
 * longjmp 兜住越界读：踩到被游戏还回去的页就跳过这一块，继续扫。
 * 和 run_job 同一个套路——memchr 是叶子函数，跳掉它的栈帧是安全的。
 */
static jmp_buf g_scan_jmp;
static volatile LONG g_scan_guard = 0;
static volatile LONG g_scan_veh_installed = 0;

static LONG CALLBACK scan_guard_veh(EXCEPTION_POINTERS* ep)
{
    if (ep->ExceptionRecord->ExceptionCode == EXCEPTION_ACCESS_VIOLATION &&
        g_scan_tid == GetCurrentThreadId() &&
        InterlockedCompareExchange(&g_scan_guard, 1, 1) == 1) {
        InterlockedExchange(&g_scan_guard, 0);
        longjmp(g_scan_jmp, 1);
    }
    return EXCEPTION_CONTINUE_SEARCH;
}

static void run_job(void)
{
    if (InterlockedCompareExchange(&g_guard_installed, 1, 1) != 1) {
        if (AddVectoredExceptionHandler(1, call_guard_veh) != NULL)
            InterlockedExchange(&g_guard_installed, 1);
    }
    InterlockedExchange(&g_job.faulted, 0);
    g_job_tid = GetCurrentThreadId();
    InterlockedExchange(&g_job_guard, 1);
    if (setjmp(g_job_jmp) == 0) {
        if (g_job.uses_float) {
            fnf_t f = (fnf_t)(uintptr_t)g_job.fn;
            g_job.ret = f(g_job.f1, g_job.a2, g_job.a3, g_job.a4);
        } else {
            fn4_t f = (fn4_t)(uintptr_t)g_job.fn;
            g_job.ret = f(g_job.a1, g_job.a2, g_job.a3, g_job.a4);
        }
        InterlockedExchange(&g_job_guard, 0);
    } else {
        g_job.ret = 0;
        InterlockedExchange(&g_job.faulted, 1);
    }
}

static BOOL CALLBACK enum_windows(HWND h, LPARAM lp)
{
    DWORD pid = 0;
    (void)lp;
    GetWindowThreadProcessId(h, &pid);
    if (pid == g_pid && IsWindowVisible(h) && GetWindow(h, GW_OWNER) == NULL) {
        char cls[64];
        GetClassNameA(h, cls, sizeof(cls));
        if (strstr(cls, "SDL_app") || strstr(cls, "hoi4") || GetWindowTextLengthA(h) > 0) {
            g_game_hwnd = h;
            return FALSE;
        }
    }
    return TRUE;
}

static LRESULT CALLBACK hook_proc(HWND h, UINT msg, WPARAM wp, LPARAM lp)
{
    if (msg == g_msg_id) {
        InterlockedExchange(&g_job.state, 2);
        run_job();
        InterlockedExchange(&g_job.state, 3);
        return 0;
    }
    if (g_orig_proc) return CallWindowProcA(g_orig_proc, h, msg, wp, lp);
    return DefWindowProcA(h, msg, wp, lp);
}

static int install_hook(void)
{
    if (g_game_hwnd == NULL) EnumWindows(enum_windows, 0);
    if (g_game_hwnd == NULL) return 0;
    g_msg_id = RegisterWindowMessageA("NEKO_HOI4_AGENT_JOB");
    if (!g_msg_id) return 0;
    g_orig_proc = (WNDPROC)SetWindowLongPtrA(g_game_hwnd, GWLP_WNDPROC, (LONG_PTR)hook_proc);
    /* the window's owner thread IS the game main thread; GetCurrentThreadId()
       here would just report our own socket server thread. */
    g_main_tid = GetWindowThreadProcessId(g_game_hwnd, NULL);
    return g_orig_proc != NULL;
}

/* Post one call to the main thread and wait for it. Timeout -> failure. */
static int call_main(u64 fn, int uses_float, u64 a1, u64 a2, u64 a3, u64 a4,
                     float f1, u64* out)
{
    DWORD start;
    if (g_game_hwnd == NULL && !install_hook()) return 0;
    if (g_msg_id == 0) return 0;

    InterlockedExchange(&g_job.state, 1);
    g_job.fn = fn;
    g_job.a1 = a1; g_job.a2 = a2; g_job.a3 = a3; g_job.a4 = a4;
    g_job.f1 = f1; g_job.f2 = 0.0f;
    g_job.ret = 0;
    g_job.uses_float = uses_float;

    if (!PostMessageA(g_game_hwnd, g_msg_id, 0, 0)) {
        InterlockedExchange(&g_job.state, 0);
        return 0;
    }
    start = GetTickCount();
    while (InterlockedCompareExchange(&g_job.state, 3, 3) != 3) {
        if (GetTickCount() - start > 8000) return 0;
        Sleep(1);
    }
    *out = g_job.ret;
    return 1;
}

/* ---------------- int3 breakpoints (VEH) ----------------
 *
 * Polling for live CMassMoveCommand objects failed: the object is consumed
 * within the same frame it is constructed (the on-map arrow lives in unit
 * state, not in a surviving CCommand). So we watch the CONSTRUCTION instead:
 * patch an 0xCC at a function entry, catch the breakpoint in a vectored
 * exception handler, capture the registers + the object bytes, restore the
 * original byte, single-step over it, re-arm. No instruction relocation
 * needed, works regardless of how short the object lives.
 *
 *   BP <rva>    arm a breakpoint at base+rva
 *   BPOFF       disarm all
 *   BPHITS      fetch captured hits (clears them)
 */

#define MAX_BP 4
#define MAX_BP_HITS 32
#define BP_DUMP 0x70

typedef struct {
    volatile LONG state;      /* 0 empty, 1 armed, 2 single-stepping */
    u64 rva;
    unsigned char orig;
    volatile DWORD tid;       /* thread that is stepping over this bp */
    volatile LONG hits;
} Bp;

typedef struct {
    u64 bp_rva;
    u64 rcx, rdx, r8, r9, rax, rsp;
    unsigned char mem[BP_DUMP];   /* bytes at rcx when trapped */
} BpHit;

static Bp g_bp[MAX_BP];
static BpHit g_bp_hits[MAX_BP_HITS];
static volatile LONG g_bp_head_placeholder = 0;
static volatile LONG g_bp_hit_head = 0;   /* next write slot */
static volatile LONG g_bp_hit_count = 0;
static volatile LONG g_bp_dropped = 0;

/* hardware breakpoint state + helpers (defined further down, declared here
   because the VEH below needs them) */
#define MAX_HW 2
static volatile u64 g_hw_addr[MAX_HW];
static volatile LONG g_hw_on[MAX_HW];
static volatile LONG g_hw_hits[MAX_HW];
static void hw_foreach_thread(int slot, u64 addr, int enable);
static void record_hit(u64 tag_rva, u64 masquerade, CONTEXT* c);

/* patch one byte of code memory (.text is EXECUTE_READ) */
static int patch_code_byte(u64 addr, unsigned char byte)
{
    DWORD old = 0;
    if (!VirtualProtect((LPVOID)(uintptr_t)addr, 1, PAGE_EXECUTE_READWRITE, &old)) return 0;
    *(unsigned char*)(uintptr_t)addr = byte;
    VirtualProtect((LPVOID)(uintptr_t)addr, 1, old, &old);
    FlushInstructionCache(GetCurrentProcess(), (LPCVOID)(uintptr_t)addr, 1);
    return 1;
}

static LONG CALLBACK bp_veh(EXCEPTION_POINTERS* ep)
{
    DWORD code = ep->ExceptionRecord->ExceptionCode;
    u64 rip = ep->ContextRecord->Rip;

    if (code == EXCEPTION_BREAKPOINT) {
        /* rip points AFTER the int3 */
        u64 at = rip - 1;
        int i;
        for (i = 0; i < MAX_BP; i++) {
            Bp* b = &g_bp[i];
            if (b->state != 1) continue;
            if (at != g_base + b->rva) continue;
            /* capture */
            {
                LONG slot = InterlockedIncrement(&g_bp_hit_head) - 1;
                BpHit* h;
                if (g_bp_hit_count < MAX_BP_HITS) {
                    slot = slot % MAX_BP_HITS;
                    h = &g_bp_hits[slot];
                    InterlockedIncrement(&g_bp_hit_count);
                } else {
                    InterlockedIncrement(&g_bp_dropped);
                    slot = slot % MAX_BP_HITS;
                    h = &g_bp_hits[slot];
                }
                h->bp_rva = b->rva;
                h->rcx = ep->ContextRecord->Rcx;
                h->rdx = ep->ContextRecord->Rdx;
                h->r8  = ep->ContextRecord->R8;
                h->r9  = ep->ContextRecord->R9;
                h->rax = ep->ContextRecord->Rax;
                h->rsp = ep->ContextRecord->Rsp;
                if (span_ok(h->rcx, BP_DUMP, 0))
                    memcpy(h->mem, (const void*)(uintptr_t)h->rcx, BP_DUMP);
                else
                    memset(h->mem, 0, BP_DUMP);
            }
            /* restore original byte, re-execute it with TF set */
            ep->ContextRecord->Rip = at;
            if (InterlockedIncrement(&b->hits) > 500) {
                /* fuse: too hot -- disarm instead of re-stepping forever */
                patch_code_byte(at, b->orig);
                InterlockedExchange(&b->state, 0);
                ep->ContextRecord->EFlags &= ~(DWORD)0x100;
                return EXCEPTION_CONTINUE_EXECUTION;
            }
            ep->ContextRecord->EFlags |= 0x100;
            patch_code_byte(at, b->orig);
            b->tid = GetCurrentThreadId();
            InterlockedExchange(&b->state, 2);
            return EXCEPTION_CONTINUE_EXECUTION;
        }
    } else if (code == EXCEPTION_SINGLE_STEP) {
        /*
         * (1) hardware breakpoint hit: ExceptionAddress is the instruction we
         *     armed. Record and keep running -- nothing to undo, which is why
         *     this path cannot corrupt the game.
         */
        {
            u64 at = (u64)(uintptr_t)ep->ExceptionRecord->ExceptionAddress;
            int i;
            for (i = 0; i < MAX_HW; i++) {
                if (!g_hw_on[i]) continue;
                if (at != g_hw_addr[i]) continue;
                if (InterlockedIncrement(&g_hw_hits[i]) > 2000) {
                    hw_foreach_thread(i, 0, 0);
                    InterlockedExchange(&g_hw_on[i], 0);
                    return EXCEPTION_CONTINUE_EXECUTION;
                }
                record_hit(at - g_base, (u64)(i + 1), ep->ContextRecord);
                return EXCEPTION_CONTINUE_EXECUTION;
            }
        }
        /*
         * (2) stepping over an int3 patch (legacy path, unused since the
         *     hardware breakpoints work).
         */
        DWORD tid = GetCurrentThreadId();
        int i;
        for (i = 0; i < MAX_BP; i++) {
            Bp* b = &g_bp[i];
            if (b->state != 2) continue;
            if (b->tid != tid) continue;
            patch_code_byte(g_base + b->rva, 0xCC);
            b->tid = 0;
            InterlockedExchange(&b->state, 1);
            /*
             * MUST clear TF here. The context saved by a single-step trap
             * still has TF set; continuing with it set makes the CPU raise a
             * single-step exception after EVERY following instruction, and
             * once none of our breakpoints is in stepping state those
             * unhandled traps kill the process. (This is what crashed the
             * game twice on 2026-11-07.)
             */
            ep->ContextRecord->EFlags &= ~(DWORD)0x100;
            return EXCEPTION_CONTINUE_EXECUTION;
        }
        /*
         * Safety net: anyone's stray single-step with TF set will terminate the
         * process if nobody handles it. Clear the flag and keep going -- a
         * missed debug stop is far cheaper than a dead game.
         */
        if (ep->ContextRecord->EFlags & 0x100) {
            ep->ContextRecord->EFlags &= ~(DWORD)0x100;
            return EXCEPTION_CONTINUE_EXECUTION;
        }
    }
    return EXCEPTION_CONTINUE_SEARCH;
}

static int bp_arm(u64 rva)
{
    int i;
    Bp* free_slot = NULL;
    for (i = 0; i < MAX_BP; i++) {
        if (g_bp[i].state && g_bp[i].rva == rva) return 1;  /* already armed */
        if (!g_bp[i].state && !free_slot) free_slot = &g_bp[i];
    }
    if (!free_slot) return 0;
    if (!span_ok(g_base + rva, 1, 0)) return 0;
    free_slot->rva = rva;
    free_slot->orig = *(unsigned char*)(uintptr_t)(g_base + rva);
    if (free_slot->orig == 0xCC) return 0;
    if (!patch_code_byte(g_base + rva, 0xCC)) return 0;
    InterlockedExchange(&free_slot->state, 1);
    return 1;
}

static void bp_disarm_all(void)
{
    int i;
    for (i = 0; i < MAX_BP; i++) {
        if (g_bp[i].state) {
            patch_code_byte(g_base + g_bp[i].rva, g_bp[i].orig);
            InterlockedExchange(&g_bp[i].state, 0);
        }
    }
}

/* ---------------- hardware breakpoints (Dr0/Dr1) ----------------
 *
 * int3 patching crashed hoi4.exe three times (2026-11-07) even with the TF
 * handling fixed, so we stopped touching game code. Hardware breakpoints live
 * in the CPU debug registers: no code bytes change, no single-step, no page
 * protection games. A hit is just an exception the kernel hands to our VEH;
 * we record the registers and return CONTINUE_EXECUTION.
 *
 *   HWBP <slot 0-1> <rva>   arm on every thread of the process
 *   HWBPOFF                 clear both slots
 */

#define MAX_HW_UNUSED 2
static void hw_apply(DWORD tid, int slot, u64 addr, int enable)
{
    HANDLE th = OpenThread(THREAD_SUSPEND_RESUME | THREAD_GET_CONTEXT |
                           THREAD_SET_CONTEXT, FALSE, tid);
    if (!th) return;
    if (SuspendThread(th) != (DWORD)-1) {
        CONTEXT c;
        ZeroMemory(&c, sizeof(c));
        c.ContextFlags = CONTEXT_DEBUG_REGISTERS;
        if (GetThreadContext(th, &c)) {
            if (slot == 0) c.Dr0 = addr; else c.Dr1 = addr;
            if (enable) {
                c.Dr7 |= (DWORD)(1u << (slot * 2));   /* local enable */
                c.Dr7 |= 0x400;                       /* reserved bit */
            } else {
                c.Dr7 &= (DWORD)~(1u << (slot * 2));
            }
            c.Dr6 = 0;
            SetThreadContext(th, &c);
        }
        ResumeThread(th);
    }
    CloseHandle(th);
}

static void hw_foreach_thread(int slot, u64 addr, int enable)
{
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
    THREADENTRY32 te;
    DWORD self = GetCurrentThreadId();
    if (snap == INVALID_HANDLE_VALUE) return;
    ZeroMemory(&te, sizeof(te));
    te.dwSize = sizeof(te);
    if (Thread32First(snap, &te)) {
        do {
            /* NEVER suspend ourselves: the loop would die at the first
               SuspendThread(self) and the agent thread hangs forever
               (it cost a whole game session on 2026-11-07). */
            if (te.th32OwnerProcessID == g_pid && te.th32ThreadID != self)
                hw_apply(te.th32ThreadID, slot, addr, enable);
        } while (Thread32Next(snap, &te));
    }
    CloseHandle(snap);
}

/* record a hit into the shared buffer (also used by the int3 path) */
static void record_hit(u64 tag_rva, u64 masquerade, CONTEXT* c)
{
    LONG slot = InterlockedIncrement(&g_bp_hit_head) - 1;
    BpHit* h;
    if (g_bp_hit_count < MAX_BP_HITS) {
        InterlockedIncrement(&g_bp_hit_count);
    } else {
        InterlockedIncrement(&g_bp_dropped);
    }
    h = &g_bp_hits[slot % MAX_BP_HITS];
    h->bp_rva = tag_rva;
    h->rcx = c->Rcx;
    h->rdx = c->Rdx;
    h->r8  = c->R8;
    h->r9  = c->R9;
    h->rax = c->Rax;
    h->rsp = c->Rsp;
    (void)masquerade;
    if (span_ok(h->rcx, BP_DUMP, 0))
        memcpy(h->mem, (const void*)(uintptr_t)h->rcx, BP_DUMP);
    else
        memset(h->mem, 0, BP_DUMP);
}

/* ---------------- command handling ---------------- */

static void handle(char* line, char* resp, size_t cap)
{
    char cmd[32];
    const char* p;
    int n;

    n = 0;
    while (line[n] && line[n] != ' ' && n < 31) { cmd[n] = line[n]; n++; }
    cmd[n] = 0;
    p = line + n;
    while (*p == ' ') p++;

    if (!strcmp(cmd, "PING")) {
        snprintf(resp, cap, "OK pong %lu %llx", (unsigned long)g_pid, (unsigned long long)g_base);
    } else if (!strcmp(cmd, "VER")) {
        /* 面板体检用：游戏里跑的代理是哪个版本、什么时候编的。
           __DATE__ __TIME__ 是编译时刻，够分辨新旧了。 */
        snprintf(resp, cap, "OK %s %s", AGENT_VERSION, AGENT_BUILD);
    } else if (!strcmp(cmd, "BASE")) {
        snprintf(resp, cap, "OK %llx", (unsigned long long)g_base);
    } else if (!strcmp(cmd, "MAIN")) {
        snprintf(resp, cap, "OK %lu hwnd=%llx hooked=%d",
                 (unsigned long)g_main_tid, (unsigned long long)(uintptr_t)g_game_hwnd,
                 g_orig_proc ? 1 : 0);
    } else if (!strcmp(cmd, "CURSOR")) {
        /* 真实屏幕坐标。本进程是 DPI 感知的，所以读出来的跟窗口客户区是同一套
           像素——地图标定也是按这套算的，直接用。 */
        POINT pt;
        if (!GetCursorPos(&pt)) {
            snprintf(resp, cap, "ERR cursor");
        } else {
            snprintf(resp, cap, "OK cursor=%ld,%ld", (long)pt.x, (long)pt.y);
        }
    } else if (!strcmp(cmd, "HOTKEYS")) {
        /* HOTKEYS [since_seq] -> OK cur=<seq> n=<k> <code>@<tick> ...
         * 客户端记住上次的 seq，只取新增的；丢了也不影响，seq 会跳。 */
        LONG since = (LONG)parse_num(p, &p);
        LONG cur = g_hot_seq;
        LONG n = cur - since;
        int i, used;
        if (n < 0) n = 0;
        if (n > MAX_HOT - 1) n = MAX_HOT - 1;
        used = snprintf(resp, cap, "OK cur=%ld n=%ld", (long)cur, (long)n);
        for (i = (int)(cur - n) + 1; i <= (int)cur && used < (int)cap - 40; i++) {
            HotEv* e = &g_hot[i & (MAX_HOT - 1)];
            if (e->seq != i) continue;
            used += snprintf(resp + used, cap - used, " %ld@%lu",
                             (long)e->code, (unsigned long)e->tick);
        }
    } else if (!strcmp(cmd, "RD")) {
        u64 addr = parse_hex(p, &p);
        u64 len = parse_num(p, &p);
        if (len > 0x100000) len = 0x100000;
        if (len * 2 + 8 > cap) len = (cap - 8) / 2;
        if (!span_ok(addr, len, 0)) {
            snprintf(resp, cap, "ERR unreadable %llx", (unsigned long long)addr);
        } else {
            bytes_to_hex((const unsigned char*)(uintptr_t)addr, (int)len, resp);
            memmove(resp + 3, resp, (size_t)len * 2 + 1);
            memcpy(resp, "OK ", 3);
        }
    } else if (!strcmp(cmd, "WR")) {
        u64 addr = parse_hex(p, &p);
        static unsigned char buf[0x100000];
        int got;
        while (*p == ' ') p++;
        got = hex_to_bytes(p, buf, (int)sizeof(buf));
        if (got <= 0) {
            snprintf(resp, cap, "ERR bad hex");
        } else if (!span_ok(addr, (u64)got, 1)) {
            snprintf(resp, cap, "ERR unwritable %llx", (unsigned long long)addr);
        } else {
            memcpy((void*)(uintptr_t)addr, buf, (size_t)got);
            snprintf(resp, cap, "OK %d", got);
        }
    } else if (!strcmp(cmd, "CALL")) {
        u64 fn = parse_hex(p, &p);
        u64 a1 = parse_hex(p, &p);
        u64 a2 = parse_hex(p, &p);
        u64 a3 = parse_hex(p, &p);
        u64 a4 = parse_hex(p, &p);
        u64 ret = 0;
        if (!call_main(fn, 0, a1, a2, a3, a4, 0.0f, &ret))
            snprintf(resp, cap, "ERR main thread call failed");
        else if (g_job.faulted)
            snprintf(resp, cap, "ERR fault in call to %llx (game survived, state may be dirty)",
                     (unsigned long long)fn);
        else
            snprintf(resp, cap, "OK %llx", (unsigned long long)ret);
    } else if (!strcmp(cmd, "CALLF")) {
        /* CALLF <fn> <float1> <a2> <a3> <a4> : first arg passed in xmm0 */
        u64 fn = parse_hex(p, &p);
        while (*p == ' ') p++;
        float f1 = (float)atof(p);
        while (*p && *p != ' ') p++;
        u64 a2 = parse_hex(p, &p);
        u64 a3 = parse_hex(p, &p);
        u64 a4 = parse_hex(p, &p);
        u64 ret = 0;
        if (!call_main(fn, 1, 0, a2, a3, a4, f1, &ret))
            snprintf(resp, cap, "ERR main thread call failed");
        else if (g_job.faulted)
            snprintf(resp, cap, "ERR fault in call to %llx (game survived, state may be dirty)",
                     (unsigned long long)fn);
        else
            snprintf(resp, cap, "OK %llx", (unsigned long long)ret);
    } else if (!strcmp(cmd, "SCAN") || !strcmp(cmd, "SCANALL")) {
        /*
         * Walk the range region by region, skipping anything unreadable.
         * A blind memcmp loop over [addr, addr+size) would hit guard pages and
         * kill the game; VirtualQuery tells us what is actually mapped.
         */
        u64 addr = 0, size = 0, want;
        unsigned char pat[8];
        u64 end;
        u64 cur = addr;
        int k;
        int overflow = 0;
        int writable_only = (cmd[4] == 'A');
        /*
         * SCANALL walks the whole address space in ONE command. Doing it as
         * 14k separate SCANs (one per region) cost 80s of pure TCP round-trip
         * latency even though the memory work itself is under a second.
         */
        if (cmd[4] == 'A') {   /* SCANALL */
            want = parse_hex(p, &p);
            addr = 0;
            size = 0x7FFFFFFF0000ULL;
        } else {
            addr = parse_hex(p, &p);
            size = parse_num(p, &p);
            want = parse_hex(p, &p);
        }
        for (k = 0; k < 8; k++) pat[k] = (unsigned char)((want >> (8 * k)) & 0xFF);
        snprintf(resp, cap, "OK ");
        end = addr + size;
        if (end < addr) end = 0x7FFFFFFF0000ULL;
        /*
         * 0.9.21：直接 memchr，靠 VEH + setjmp 兜住越界读（见 scan_guard_veh）。
         *
         * 每次 setjmp 管 1MB，踩到被游戏还回去的页就整块跳过。setjmp 的开销
         * （9GB / 1MB ≈ 9000 次）在总耗时里可以忽略；比起 0.9.19 的
         * ReadProcessMemory 方案，省掉了"复制进去再扫"那一倍的内存流量。
         */
        if (InterlockedCompareExchange(&g_scan_veh_installed, 1, 1) != 1) {
            if (AddVectoredExceptionHandler(1, scan_guard_veh) != NULL)
                InterlockedExchange(&g_scan_veh_installed, 1);
        }
        g_scan_tid = GetCurrentThreadId();
        while (cur < end) {
            MEMORY_BASIC_INFORMATION mbi;
            u64 rbase, rsize, stop, i;
            if (VirtualQuery((LPCVOID)(uintptr_t)cur, &mbi, sizeof(mbi)) != sizeof(mbi)) break;
            rbase = (u64)(uintptr_t)mbi.BaseAddress;
            rsize = (u64)mbi.RegionSize;
            if (rsize == 0) break;
            stop = rbase + rsize;
            if (stop > end) stop = end;
            if (writable_only && !(mbi.Protect & (PAGE_READWRITE | PAGE_WRITECOPY |
                    PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY))) {
                cur = stop;
                continue;
            }
            if (mbi.State == MEM_COMMIT && (mbi.Protect & (PAGE_READONLY | PAGE_READWRITE |
                    PAGE_WRITECOPY | PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE |
                    PAGE_EXECUTE_WRITECOPY)) && !(mbi.Protect & PAGE_GUARD)) {
                u64 lo = cur > rbase ? cur : rbase;
                lo = (lo + 7) & ~(u64)7;
                /*
                 * 一次 setjmp 管 1 MB：直接 memchr 游戏内存，踩到被还回去的页
                 * 就整块跳过。SIZE 向下 8 对齐，保证下一块起点还对得上。
                 */
                for (i = lo; i + 8 <= stop;) {
                    u64 n = stop - i;
                    u64 kk;
                    if (n > SCAN_CHUNK) n = SCAN_CHUNK;
                    n &= ~(u64)7;
                    if (n < 8) break;
                    InterlockedExchange(&g_scan_guard, 1);
                    if (setjmp(g_scan_jmp) == 0) {
                        for (kk = 0; kk + 8 <= n;) {
                            const unsigned char* hit = (const unsigned char*)memchr(
                                (const void*)(uintptr_t)(i + kk), pat[0],
                                (size_t)(n - kk - 7));
                            u64 off;
                            if (!hit) break;
                            off = (u64)(uintptr_t)hit - i;
                            if ((i + off) & 7) { kk = (off + 8) & ~(u64)7; continue; }
                            if (!memcmp(hit, pat, 8)) {
                                char tmp[24];
                                snprintf(tmp, sizeof(tmp), "%llx,", (unsigned long long)(i + off));
                                if (strlen(resp) + strlen(tmp) + 4 >= cap - 4) { overflow = 1; break; }
                                strncat(resp, tmp, cap - strlen(resp) - 1);
                            }
                            kk = off + 8;
                        }
                        InterlockedExchange(&g_scan_guard, 0);
                    }
                    /* setjmp 回来 = 这一块里有页被判了死刑，跳过它继续 */
                    if (overflow) break;
                    i += n;
                }
            }
            if (overflow) break;
            cur = stop;
        }
        {
            size_t L = strlen(resp);
            if (L > 3 && resp[L - 1] == ',') resp[L - 1] = 0;
            if (L <= 3) snprintf(resp, cap, "OK none");
        }
    } else if (!strcmp(cmd, "REGIONS")) {
        /*
         * List committed writable regions: addr:size,addr:size,...
         *
         * No fixed count cap -- an earlier "max 200" version silently returned
         * only the first 15 MB of a multi-GB process, so every scan missed the
         * actual game heap. Stop on buffer space instead, and skip tiny
         * regions to keep the list small.
         */
        MEMORY_BASIC_INFORMATION mbi;
        u64 addr = 0;
        /* REGIONS [min_size] -- default 0x1000; pass e.g. 100000 (hex) to get
         * only big regions, otherwise tens of thousands of 4KB heap slivers
         * overflow the response buffer before we ever reach the high heap. */
        u64 min_size = 0x1000;
        if (*p) min_size = parse_hex(p, &p);
        resp[0] = 0;
        strncat(resp, "OK ", cap - 2);
        while (addr < 0x7FFFFFFEFFFFULL) {
            if (VirtualQuery((LPCVOID)(uintptr_t)addr, &mbi, sizeof(mbi)) != sizeof(mbi)) break;
            if (mbi.State == MEM_COMMIT && mbi.RegionSize >= min_size &&
                (mbi.Protect & (PAGE_READWRITE | PAGE_WRITECOPY | PAGE_EXECUTE_READWRITE))) {
                char tmp[48];
                snprintf(tmp, sizeof(tmp), "%llx:%llx,", (unsigned long long)(uintptr_t)mbi.BaseAddress,
                         (unsigned long long)mbi.RegionSize);
                if (strlen(resp) + strlen(tmp) + 4 >= cap - 4) break;
                strncat(resp, tmp, cap - strlen(resp) - 1);
            }
            addr = (u64)(uintptr_t)mbi.BaseAddress + mbi.RegionSize;
        }
        {
            size_t L = strlen(resp);
            if (L && resp[L - 1] == ',') resp[L - 1] = 0;
        }
    } else if (!strcmp(cmd, "HWBP") || !strcmp(cmd, "BP")) {
        /*
         * HARD DISABLED (2026-11-07, after four killed game processes).
         *
         * Both paths end in SuspendThread + SetThreadContext across every
         * thread of a multi-threaded game. If a thread is suspended while it
         * owns the heap lock or a critical section, the process deadlocks and
         * gets terminated. HOI4 1.16.9 produced no crash dump and no log entry
         * for any of these kills -- it is not an access violation, the process
         * simply ends.
         *
         * The code is kept for reference but is unreachable by design. Do not
         * re-enable: breakpoints are not worth a dead game session.
         */
        snprintf(resp, cap, "ERR disabled: breakpoints kill this build (use memory-bank diffs instead)");
    } else if (!strcmp(cmd, "HWBPOFF")) {
        snprintf(resp, cap, "OK hw off");
    } else if (!strcmp(cmd, "BPOFF")) {
        snprintf(resp, cap, "OK disarmed");
    } else if (!strcmp(cmd, "BPHITS")) {
        LONG n = InterlockedExchange(&g_bp_hit_count, 0);
        LONG dropped = InterlockedExchange(&g_bp_dropped, 0);
        int i;
        snprintf(resp, cap, "OK %ld dropped=%ld", (long)n, (long)dropped);
        for (i = 0; i < n && i < MAX_BP_HITS; i++) {
            BpHit* h = &g_bp_hits[i];
            char hex[BP_DUMP * 2 + 1];
            char tmp[BP_DUMP * 2 + 160];
            bytes_to_hex(h->mem, BP_DUMP, hex);
            snprintf(tmp, sizeof(tmp), "\nhit bp=%llx rcx=%llx rdx=%llx r8=%llx r9=%llx rax=%llx rsp=%llx mem=%s",
                     (unsigned long long)h->bp_rva, (unsigned long long)h->rcx,
                     (unsigned long long)h->rdx, (unsigned long long)h->r8,
                     (unsigned long long)h->r9, (unsigned long long)h->rax,
                     (unsigned long long)h->rsp, hex);
            strncat(resp, tmp, cap - strlen(resp) - 1);
        }
    } else if (!strcmp(cmd, "QUIT")) {
        snprintf(resp, cap, "OK bye");
    } else {
        snprintf(resp, cap, "ERR unknown cmd '%s'", cmd);
    }
}

/* ---------------- server ---------------- */

static SOCKET listen_socket(void)
{
    WSADATA wsa;
    SOCKET s;
    struct sockaddr_in addr;
    int one = 1;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) return INVALID_SOCKET;
    s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s == INVALID_SOCKET) return INVALID_SOCKET;
    setsockopt(s, SOL_SOCKET, SO_EXCLUSIVEADDRUSE, (const char*)&one, sizeof(one));
    ZeroMemory(&addr, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(AGENT_PORT);
    addr.sin_addr.s_addr = inet_addr("127.0.0.1");
    if (bind(s, (struct sockaddr*)&addr, sizeof(addr)) != 0) {
        closesocket(s);
        return INVALID_SOCKET;
    }
    if (listen(s, 4) != 0) {
        closesocket(s);
        return INVALID_SOCKET;
    }
    return s;
}

static DWORD WINAPI client_thread(LPVOID param);

static DWORD WINAPI server_thread(LPVOID param)
{
    SOCKET ls;
    (void)param;

    ls = listen_socket();
    if (ls == INVALID_SOCKET) return 0;

    InitializeCriticalSection(&g_cmd_lock);
    g_cmd_lock_ready = 1;

    /* give the game a moment, then hook the window proc */
    Sleep(300);
    install_hook();

    /* 多客户端：热键轮询是长连接，单连接模型会把别的客户端全挡在门外
     * （2026-11-08 实测：N.E.K.O 一连上，逆向脚本就永远连不进来）。
     * 每个连接一个线程；命令本身进一把锁，CALL/热键缓冲这些共享状态不会被并发踩。 */
    for (;;) {
        SOCKET c = accept(ls, NULL, NULL);
        HANDLE h;
        if (c == INVALID_SOCKET) { Sleep(50); continue; }
        h = CreateThread(NULL, 0, client_thread, (LPVOID)(uintptr_t)c, 0, NULL);
        if (!h) { closesocket(c); continue; }
        CloseHandle(h);
    }
}

static DWORD WINAPI client_thread(LPVOID param)
{
    SOCKET c = (SOCKET)(uintptr_t)param;
    char* line = (char*)malloc(MAX_LINE);
    char* resp = (char*)malloc(MAX_RESP);
    {
        int one = 1, idle_ms = 60000;
        setsockopt(c, IPPROTO_TCP, TCP_NODELAY, (const char*)&one, sizeof(one));
        /* 空闲 60 秒就断。曾有客户端只握手不发数据（Test-NetConnection），
         * 把无超时的 recv 卡死成永久占用。 */
        setsockopt(c, SOL_SOCKET, SO_RCVTIMEO, (const char*)&idle_ms, sizeof(idle_ms));
    }
    if (!line || !resp) {
        if (line) free(line);
        if (resp) free(resp);
        closesocket(c);
        return 0;
    }
    for (;;) {
        int n = 0;
        char ch;
        int got = 1;
        int quit;
        size_t L;
        while (n < MAX_LINE - 1) {
            got = recv(c, &ch, 1, 0);
            if (got <= 0) break;
            if (ch == '\n') break;
            if (ch != '\r') line[n++] = ch;
        }
        line[n] = 0;
        if (got <= 0) break;   /* 断开或空闲超时，半行也放弃 */
        quit = !strcmp(line, "QUIT");
        resp[0] = 0;
        EnterCriticalSection(&g_cmd_lock);
        handle(line, resp, MAX_RESP - 2);
        LeaveCriticalSection(&g_cmd_lock);
        L = strlen(resp);
        resp[L] = '\n';
        resp[L + 1] = 0;
        send(c, resp, (int)(L + 1), 0);
        if (quit) break;
    }
    closesocket(c);
    free(line);
    free(resp);
    return 0;
}

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID reserved)
{
    (void)inst; (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        if (InterlockedExchange(&g_inited, 1) != 0) return TRUE;
        DisableThreadLibraryCalls(inst);
        g_pid = GetCurrentProcessId();
        g_base = module_base(g_pid);
        AddVectoredExceptionHandler(1, bp_veh);
        InterlockedExchange(&g_hot_on, 1);
        CreateThread(NULL, 0, hotkey_thread, NULL, 0, NULL);
        CreateThread(NULL, 0, server_thread, NULL, 0, NULL);
    }
    return TRUE;
}
